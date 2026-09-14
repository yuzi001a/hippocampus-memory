# -*- coding: utf-8 -*-
"""G6A Recall Engine V2 — contracts.

Plain stdlib dataclasses; no I/O.  Frozen where the value is a static
contract; mutable where the value is a runtime object carried through
the recall pipeline.
"""
from __future__ import annotations

import re
import time
import types
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


# Constants

LANE_KEYWORD = "keyword"
LANE_VECTOR = "vector"
LANE_TOPIC = "topic"
LANE_QA = "qa"
LANE_EXPLICIT = "explicit"
ALL_LANES: tuple[str, ...] = (LANE_KEYWORD, LANE_VECTOR, LANE_TOPIC, LANE_QA, LANE_EXPLICIT)

DEFAULT_QUERY_LIMIT = 8
DEFAULT_INJECTION_MAX_CHARS = 10_000
DEFAULT_RERANK_LIMIT = 30

_META_MAX = 256
_META_LIST_MAX = 64
_META_DICT_MAX = 32
_CONTENT_LIKE = frozenset({
    "content", "body", "text", "raw", "full_text", "snippet", "preview",
})
_WS = re.compile(r"\s+")


# Enumerations

class DropReasonCode(str, Enum):
    DUPLICATE = "DUPLICATE"
    BELOW_THRESHOLD = "BELOW_THRESHOLD"
    OUTSIDE_LIMIT = "OUTSIDE_LIMIT"
    DEADLINE = "DEADLINE"
    RERANK_PRUNED = "RERANK_PRUNED"
    CHAR_BUDGET = "CHAR_BUDGET"
    INVALID = "INVALID"
    LANE_ERROR = "LANE_ERROR"


class CandidateEventType(str, Enum):
    FOUND = "FOUND"
    DEDUPED = "DEDUPED"
    SCORED = "SCORED"
    FUSED = "FUSED"
    RERANKED = "RERANKED"
    DROPPED = "DROPPED"
    SELECTED = "SELECTED"
    INJECTED = "INJECTED"


# Helpers

def _cap(v: Any) -> Any:
    if isinstance(v, str):
        return v[:_META_MAX] if len(v) > _META_MAX else v
    if isinstance(v, list):
        capped = [_cap(x) for x in v[:_META_LIST_MAX]]
        if len(v) > _META_LIST_MAX:
            capped.append(f"__truncated_{len(v) - _META_LIST_MAX}_more__")
        return capped
    if isinstance(v, tuple):
        capped = tuple(_cap(x) for x in v[:_META_LIST_MAX])
        if len(v) > _META_LIST_MAX:
            capped = capped + (f"__truncated_{len(v) - _META_LIST_MAX}_more__",)
        return capped
    if isinstance(v, dict):
        items = list(v.items())[:_META_DICT_MAX]
        out = {k: _cap(x) for k, x in items}
        if len(v) > _META_DICT_MAX:
            out["__truncated_keys__"] = len(v) - _META_DICT_MAX
        return out
    return v


def safe_metadata(m: Mapping[str, Any] | None, *, include_content: bool = False) -> dict[str, Any]:
    """Privacy-safe metadata snapshot — drops content-like keys (case-insensitive)
    and caps long values and unbounded collection sizes."""
    if not m:
        return {}
    out: dict[str, Any] = {}
    for k, v in m.items():
        key = str(k)
        if not include_content and key.lower() in _CONTENT_LIKE:
            continue
        out[key] = _cap(v)
        if isinstance(v, str) and len(v) > _META_MAX:
            out[f"{key}_truncated"] = True
    return out


def _check_nonneg(name: str, v: int | float) -> None:
    try:
        if float(v) < 0:
            raise ValueError(f"{name} must be >= 0 (got {v!r})")
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric")


def _freeze_mapping(m: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return an immutable ``MappingProxyType`` view of a mapping for frozen dataclasses.

    A ``MappingProxyType`` rejects item assignment / deletion with ``TypeError``,
    while still supporting read-only ``Mapping`` semantics for ``safe_metadata`` /
    ``to_dict`` and any other consumer that iterates the field.
    """
    if m is None:
        return types.MappingProxyType({})
    return types.MappingProxyType(dict(m))


# Immutable: QueryContext

@dataclass(frozen=True)
class QueryContext:
    """Immutable description of one recall request.

    ``deadline_monotonic`` is an absolute ``time.monotonic()`` value.
    ``budget_ms`` is the planned total budget.  The two combine to give
    the remaining-budget figure via :meth:`remaining_budget_ms`.
    """

    query_id: str
    query_text: str
    deadline_monotonic: float
    budget_ms: int
    limit: int
    max_chars: int
    created_at: float = field(default_factory=time.time)
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    profile: Optional[str] = None
    agent_id: Optional[str] = None
    query_embedding: Optional[tuple[float, ...]] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    normalized_query: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.query_id:
            raise ValueError("query_id must be non-empty")
        _check_nonneg("budget_ms", self.budget_ms)
        _check_nonneg("limit", self.limit)
        _check_nonneg("max_chars", self.max_chars)
        object.__setattr__(self, "normalized_query", _WS.sub(" ", self.query_text).strip() if self.query_text else "")
        if self.query_embedding is not None:
            object.__setattr__(self, "query_embedding", tuple(float(x) for x in self.query_embedding))
        # Treat metadata as immutable: copy to a fresh dict and bind via Mapping.
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})
        else:
            object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def remaining_budget_ms(self) -> float:
        """Remaining ms, capped at ``budget_ms`` and clamped at 0.0."""
        return max(0.0, min((self.deadline_monotonic - time.monotonic()) * 1000.0, float(self.budget_ms)))

    def has_budget_remaining(self, min_remaining_ms: int = 1) -> bool:
        return self.remaining_budget_ms() >= float(min_remaining_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query_text": self.query_text,
            "normalized_query": self.normalized_query,
            "created_at": self.created_at,
            "deadline_monotonic": float(self.deadline_monotonic),
            "budget_ms": int(self.budget_ms),
            "limit": int(self.limit),
            "max_chars": int(self.max_chars),
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "profile": self.profile,
            "agent_id": self.agent_id,
            "query_embedding": list(self.query_embedding) if self.query_embedding else None,
            "metadata": safe_metadata(self.metadata),
        }


# Immutable: canonical algorithm snapshot

@dataclass(frozen=True)
class CanonicalAlgorithmSnapshot:
    """Canonical algorithm-parameter snapshot — metadata only, not execution.

    Redlines (preserved exactly): rrf_k=60, half_life_days=30,
    lane_weights keyword=.5/vector=1/topic=2/qa=1, rare_boost=.08,
    exact_qa_boost=.5, max_exact_qa=2, qa_vector_threshold=.35,
    message_effective_threshold=.3, topic_vector_threshold=.4,
    topic_recall_weight=.5, max_topics=3, max_subtopics=3,
    topic_context_max_chars=3000, qa_frequency_candidate_multiplier=8,
    rerank_top_n=30, dual_prefetch=True, default_limit=8,
    injection_max_chars=10000, reranker_model='BAAI/bge-reranker-v2-m3',
    remote_rerank_min_remaining_ms=1500.
    """

    rrf_k: int = 60
    half_life_days: int = 30
    lane_weights: tuple[tuple[str, float], ...] = (
        (LANE_KEYWORD, 0.5), (LANE_VECTOR, 1.0), (LANE_TOPIC, 2.0), (LANE_QA, 1.0),
    )
    rare_boost: float = 0.08
    exact_qa_boost: float = 0.5
    max_exact_qa: int = 2
    qa_vector_threshold: float = 0.35
    message_effective_threshold: float = 0.3
    topic_vector_threshold: float = 0.4
    topic_recall_weight: float = 0.5
    max_topics: int = 3
    max_subtopics: int = 3
    topic_context_max_chars: int = 3000
    qa_frequency_candidate_multiplier: int = 8
    rerank_top_n: int = 30
    dual_prefetch: bool = True
    default_limit: int = 8
    injection_max_chars: int = 10000
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    remote_rerank_min_remaining_ms: int = 1500

    @classmethod
    def canonical(cls) -> "CanonicalAlgorithmSnapshot":
        return cls()

    def lane_weight(self, lane: str) -> float:
        for name, w in self.lane_weights:
            if name == lane:
                return w
        return 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rrf_k": self.rrf_k, "half_life_days": self.half_life_days,
            "lane_weights": dict(self.lane_weights),
            "rare_boost": self.rare_boost, "exact_qa_boost": self.exact_qa_boost,
            "max_exact_qa": self.max_exact_qa,
            "qa_vector_threshold": self.qa_vector_threshold,
            "message_effective_threshold": self.message_effective_threshold,
            "topic_vector_threshold": self.topic_vector_threshold,
            "topic_recall_weight": self.topic_recall_weight,
            "max_topics": self.max_topics, "max_subtopics": self.max_subtopics,
            "topic_context_max_chars": self.topic_context_max_chars,
            "qa_frequency_candidate_multiplier": self.qa_frequency_candidate_multiplier,
            "rerank_top_n": self.rerank_top_n,
            "dual_prefetch": self.dual_prefetch,
            "default_limit": self.default_limit,
            "injection_max_chars": self.injection_max_chars,
            "reranker_model": self.reranker_model,
            "remote_rerank_min_remaining_ms": self.remote_rerank_min_remaining_ms,
        }


def canonical_algorithm_snapshot() -> CanonicalAlgorithmSnapshot:
    return CanonicalAlgorithmSnapshot.canonical()


# Immutable: LanePlan / QueryPlan

@dataclass(frozen=True)
class LanePlan:
    name: str
    enabled: bool
    budget_ms: int
    deadline_monotonic: float
    candidate_limit: int
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in ALL_LANES:
            raise ValueError(f"unknown lane {self.name!r}")
        _check_nonneg("budget_ms", self.budget_ms)
        _check_nonneg("candidate_limit", self.candidate_limit)
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})
        else:
            # Treat metadata as immutable.
            object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "enabled": bool(self.enabled),
            "budget_ms": int(self.budget_ms),
            "deadline_monotonic": float(self.deadline_monotonic),
            "candidate_limit": int(self.candidate_limit),
            "reason": self.reason,
            "metadata": safe_metadata(self.metadata),
        }


@dataclass(frozen=True)
class QueryPlan:
    query_id: str
    lanes: tuple[LanePlan, ...]
    deadline_monotonic: float
    budget_ms: int
    rerank_enabled: bool
    rerank_limit: int
    final_limit: int
    max_chars: int
    algorithm: CanonicalAlgorithmSnapshot

    def __post_init__(self) -> None:
        names = [lp.name for lp in self.lanes]
        if set(names) != set(ALL_LANES):
            raise ValueError(f"QueryPlan must include all lanes exactly once (got {names})")
        _check_nonneg("budget_ms", self.budget_ms)
        _check_nonneg("rerank_limit", self.rerank_limit)
        _check_nonneg("final_limit", self.final_limit)
        _check_nonneg("max_chars", self.max_chars)

    def lane(self, name: str) -> LanePlan:
        for lp in self.lanes:
            if lp.name == name:
                return lp
        raise KeyError(name)

    def enabled_lanes(self) -> tuple[LanePlan, ...]:
        return tuple(lp for lp in self.lanes if lp.enabled)

    def lane_names(self) -> tuple[str, ...]:
        return tuple(lp.name for lp in self.lanes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id, "budget_ms": int(self.budget_ms),
            "deadline_monotonic": float(self.deadline_monotonic),
            "rerank_enabled": bool(self.rerank_enabled),
            "rerank_limit": int(self.rerank_limit),
            "final_limit": int(self.final_limit),
            "max_chars": int(self.max_chars),
            "lanes": [lp.to_dict() for lp in self.lanes],
            "algorithm": self.algorithm.to_dict(),
        }


def build_default_query_plan(context: QueryContext) -> QueryPlan:
    """Deterministic default plan — five lanes enabled, no I/O."""
    budget = max(1, int(context.budget_ms))
    final_limit = int(context.limit) if context.limit > 0 else DEFAULT_QUERY_LIMIT
    max_chars = int(context.max_chars) if context.max_chars > 0 else DEFAULT_INJECTION_MAX_CHARS
    slice_ = max(50, budget // 6)
    return QueryPlan(
        query_id=context.query_id,
        lanes=(
            LanePlan(LANE_KEYWORD, True, slice_, context.deadline_monotonic, max(20, final_limit * 4)),
            LanePlan(LANE_VECTOR, True, slice_, context.deadline_monotonic, max(20, final_limit * 4)),
            LanePlan(LANE_TOPIC, True, slice_, context.deadline_monotonic, max(15, final_limit * 3)),
            LanePlan(LANE_QA, True, slice_ + budget // 12, context.deadline_monotonic, max(30, final_limit * 8)),
            LanePlan(LANE_EXPLICIT, True, max(50, slice_ // 2), context.deadline_monotonic, final_limit),
        ),
        deadline_monotonic=context.deadline_monotonic,
        budget_ms=budget,
        rerank_enabled=True,
        rerank_limit=DEFAULT_RERANK_LIMIT,
        final_limit=final_limit,
        max_chars=max_chars,
        algorithm=canonical_algorithm_snapshot(),
    )


# Drop reason

@dataclass(frozen=True)
class DropReason:
    code: DropReasonCode
    detail: str = ""
    stage: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "detail": self.detail, "stage": self.stage}


# ---------------------------------------------------------------------
# Mutable record dataclasses — exported as ProvenanceRecord / ScoreRecord /
# CandidateEvent (keeping legacy underscore names as public aliases for
# backward compatibility with any external importers).
# ---------------------------------------------------------------------

@dataclass
class ProvenanceRecord:
    """One provenance entry: which lane produced which source identifier."""
    lane: str
    source_type: str
    source_id: str
    ts: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {"lane": self.lane, "source_type": self.source_type,
                "source_id": self.source_id, "ts": self.ts}


@dataclass
class ScoreRecord:
    """One immutable score stage value recorded in score history."""
    stage: str
    value: float
    operation: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "value": self.value, "operation": self.operation,
                "params": dict(self.params) if self.params else {}}


@dataclass
class CandidateEvent:
    """One typed event in a candidate's lifecycle."""
    event_type: CandidateEventType
    note: str = ""
    ts: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {"event_type": self.event_type.value, "note": self.note, "ts": self.ts}


# Legacy public aliases — old underscore names remain importable.
_Provenance = ProvenanceRecord
_Score = ScoreRecord
_Event = CandidateEvent


# Stage-transition / drop-event typed structured records (used by trace).

@dataclass
class StageTransition:
    """Structured record of a stage transition for a candidate."""
    candidate_id: str
    from_stage: str
    to_stage: str
    reason: str = ""
    ts: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "reason": self.reason,
            "ts": self.ts,
        }


@dataclass
class DropEvent:
    """Structured record of a drop event for a candidate."""
    candidate_id: str
    code: DropReasonCode
    detail: str = ""
    stage: str = ""
    ts: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "code": self.code.value,
            "detail": self.detail,
            "stage": self.stage,
            "ts": self.ts,
        }


_STAGE_FIELD = {
    "raw": "raw_score",
    "lane": "lane_score", "vector": "lane_score", "keyword": "lane_score",
    "topic": "lane_score", "qa": "lane_score",
    "fusion": "fusion_score", "rrf": "fusion_score",
    "temporal": "temporal_score", "half_life": "temporal_score",
    "rerank": "rerank_score",
    "final": "final_score",
}


@dataclass
class RecallCandidate:
    """One recall candidate carried through the pipeline.

    Mutable runtime object.  Every score stage is kept as an explicit
    field AND in the immutable ``score_history`` list — never collapsed
    into one float.  Construction creates one provenance record and a
    ``FOUND`` event.
    """

    candidate_id: str
    source_type: str
    source_id: str
    lane: str
    content: Optional[str] = None
    text_reference: str = ""
    timestamp: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_score: Optional[float] = None
    lane_score: Optional[float] = None
    fusion_score: Optional[float] = None
    temporal_score: Optional[float] = None
    rerank_score: Optional[float] = None
    final_score: Optional[float] = None
    score_history: list[ScoreRecord] = field(default_factory=list)
    provenance: list[ProvenanceRecord] = field(default_factory=list)
    events: list[CandidateEvent] = field(default_factory=list)
    dropped: bool = False
    drop_reason: Optional[DropReason] = None
    selected: bool = False
    injected: bool = False

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ValueError("candidate_id must be non-empty")
        if self.lane not in ALL_LANES:
            raise ValueError(f"unknown lane {self.lane!r}")
        self.provenance.append(ProvenanceRecord(self.lane, self.source_type, self.source_id))
        self.events.append(CandidateEvent(CandidateEventType.FOUND))

    def merge_provenance(self, lane: str, source_type: str, source_id: str) -> None:
        """Append a provenance record if (lane, source_type, source_id) is new."""
        for p in self.provenance:
            if p.lane == lane and p.source_type == source_type and p.source_id == source_id:
                return
        self.provenance.append(ProvenanceRecord(lane, source_type, source_id))

    @property
    def contributing_lanes(self) -> list[str]:
        seen: list[str] = []
        for p in self.provenance:
            if p.lane not in seen:
                seen.append(p.lane)
        return seen

    def record_score(self, stage: str, value: float, *, operation: str = "", **params: Any) -> None:
        try:
            v = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"score value must be numeric: {e}")
        s = (stage or "").strip().lower()
        if not s:
            raise ValueError("stage must be non-empty")
        fld = _STAGE_FIELD.get(s)
        if fld is not None:
            setattr(self, fld, v)
        self.score_history.append(ScoreRecord(s, v, operation, dict(params)))
        if s != "raw" and not any(e.event_type == CandidateEventType.SCORED for e in self.events):
            self.events.append(CandidateEvent(CandidateEventType.SCORED))

    def record_event(self, event_type: CandidateEventType, note: str = "") -> None:
        """Append a typed event to the candidate's event log."""
        if not isinstance(event_type, CandidateEventType):
            raise ValueError("event_type must be a CandidateEventType")
        self.events.append(CandidateEvent(event_type, note))

    def mark_dropped(self, reason: DropReason) -> None:
        if not isinstance(reason, DropReason) or not isinstance(reason.code, DropReasonCode):
            raise ValueError("reason must be a DropReason with DropReasonCode")
        self.dropped = True
        self.drop_reason = reason
        self.events.append(CandidateEvent(CandidateEventType.DROPPED, reason.code.value))

    def mark_selected(self) -> None:
        if self.dropped:
            raise ValueError("dropped candidate cannot be selected")
        self.selected = True
        self.events.append(CandidateEvent(CandidateEventType.SELECTED))

    def mark_injected(self) -> None:
        if not self.selected:
            raise ValueError("candidate must be selected before injected")
        if not self.injected:
            self.injected = True
            self.events.append(CandidateEvent(CandidateEventType.INJECTED))

    def snapshot(self, *, include_content: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "lane": self.lane,
            "contributing_lanes": list(self.contributing_lanes),
            "text_reference": self.text_reference,
            "timestamp": self.timestamp,
            "scores": {
                "raw_score": self.raw_score, "lane_score": self.lane_score,
                "fusion_score": self.fusion_score, "temporal_score": self.temporal_score,
                "rerank_score": self.rerank_score, "final_score": self.final_score,
            },
            "score_history": [h.to_dict() for h in self.score_history],
            "provenance": [p.to_dict() for p in self.provenance],
            "events": [e.to_dict() for e in self.events],
            "dropped": self.dropped,
            "drop_reason": self.drop_reason.to_dict() if self.drop_reason else None,
            "selected": self.selected,
            "injected": self.injected,
            "metadata": safe_metadata(self.metadata, include_content=include_content),
        }
        if include_content:
            d["content"] = self.content
        return d