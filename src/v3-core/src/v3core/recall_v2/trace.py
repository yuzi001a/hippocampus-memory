# -*- coding: utf-8 -*-
"""G6A Recall Engine V2 — trace (slim).

The trace is a first-class mutable typed object describing one recall
request: timing, per-lane summaries, candidate snapshots, drop events,
selection / injection summary, and warnings / errors.  Pure stdlib —
no I/O, no provider calls.

Privacy default: the original ``RecallCandidate`` object is NEVER
retained.  Only the privacy-safe ``CandidateSnapshot`` is stored.
``record_candidate(capture_content=False)`` (the default) leaves
``snapshot.content = None``.  Content appears in serialization only
when ``include_content=True`` is passed explicitly to ``to_dict`` /
``to_json``.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .contracts import (
    ALL_LANES,
    CandidateEvent, CandidateEventType,
    DropEvent, DropReason, DropReasonCode,
    ProvenanceRecord, QueryContext, QueryPlan,
    ScoreRecord, StageTransition,
    safe_metadata,
)


# ----- Per-lane / per-injection summaries --------------------------------

@dataclass
class LaneSummary:
    lane: str
    started: Optional[float] = None
    finished: Optional[float] = None
    duration_ms: Optional[float] = None
    candidate_count: int = 0
    timed_out: bool = False
    skipped: bool = False
    error: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "started": self.started, "finished": self.finished,
            "duration_ms": self.duration_ms,
            "candidate_count": int(self.candidate_count),
            "timed_out": bool(self.timed_out), "skipped": bool(self.skipped),
            "error": self.error, "reason": self.reason,
        }


@dataclass
class InjectionSummary:
    injected_count: int = 0
    total_chars: int = 0
    char_budget: int = 0
    truncated: bool = False
    dropped_for_budget: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "injected_count": int(self.injected_count),
            "total_chars": int(self.total_chars),
            "char_budget": int(self.char_budget),
            "truncated": bool(self.truncated),
            "dropped_for_budget": int(self.dropped_for_budget),
        }


@dataclass
class CandidateSnapshot:
    """Privacy-safe per-candidate snapshot stored on the trace.

    ``content`` is ``None`` unless ``record_candidate(capture_content=True)``
    was called explicitly.

    Snapshot carries enough state to drive ``record_score`` /
    ``record_drop`` / ``select`` / ``inject`` without ever needing the
    original ``RecallCandidate`` object — only its ``candidate_id``.
    """

    candidate_id: str
    lane: str
    source_type: str
    source_id: str
    final_score: Optional[float] = None
    raw_score: Optional[float] = None
    lane_score: Optional[float] = None
    fusion_score: Optional[float] = None
    temporal_score: Optional[float] = None
    rerank_score: Optional[float] = None
    selected: bool = False
    injected: bool = False
    dropped: bool = False
    drop_reason: Optional[DropReason] = None
    content: Optional[str] = None
    text_reference: str = ""
    timestamp: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: list[ProvenanceRecord] = field(default_factory=list)
    score_history: list[ScoreRecord] = field(default_factory=list)
    events: list[CandidateEvent] = field(default_factory=list)
    contributing_lanes: list[str] = field(default_factory=list)

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "lane": self.lane,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "contributing_lanes": list(self.contributing_lanes),
            "text_reference": self.text_reference,
            "timestamp": self.timestamp,
            "scores": {
                "raw_score": self.raw_score,
                "lane_score": self.lane_score,
                "fusion_score": self.fusion_score,
                "temporal_score": self.temporal_score,
                "rerank_score": self.rerank_score,
                "final_score": self.final_score,
            },
            "score_history": [h.to_dict() for h in self.score_history],
            "provenance": [p.to_dict() for p in self.provenance],
            "events": [e.to_dict() for e in self.events],
            "selected": bool(self.selected), "injected": bool(self.injected),
            "dropped": bool(self.dropped),
            "drop_reason": self.drop_reason.to_dict() if self.drop_reason else None,
            "metadata": safe_metadata(self.metadata, include_content=include_content),
        }
        if include_content and self.content is not None:
            d["content"] = self.content
        return d


# ----- Trace -------------------------------------------------------------

class RecallTrace:
    """Mutable typed trace for one recall request."""

    def __init__(
        self,
        *,
        query_context: QueryContext,
        query_plan: QueryPlan,
        trace_id: Optional[str] = None,
    ) -> None:
        self.trace_id: str = trace_id or f"trace-{uuid.uuid4().hex[:12]}"
        self.query_context: QueryContext = query_context
        self.query_plan: QueryPlan = query_plan
        self.start_monotonic: float = time.monotonic()
        self.start_wall: float = time.time()
        self.end_monotonic: Optional[float] = None
        self.duration_ms: Optional[float] = None
        self.deadline_monotonic: float = query_context.deadline_monotonic
        self.budget_ms: int = int(query_context.budget_ms)
        self.lane_summaries: dict[str, LaneSummary] = {
            name: LaneSummary(lane=name) for name in ALL_LANES
        }
        self._lane_started: dict[str, float] = {}
        # NOTE: we do NOT retain the original RecallCandidate object.
        # All scoring / drop / selection logic operates on the snapshot.
        self.candidate_snapshots: dict[str, CandidateSnapshot] = {}
        # ``candidates`` is exposed as a *proxy view* of snapshots for callers
        # that still expect ``trace.candidates[id]``.  It maps id → snapshot
        # and never holds the original mutable RecallCandidate.
        self.candidates: dict[str, CandidateSnapshot] = self.candidate_snapshots
        self.final_selected_ids: list[str] = []
        self.injection_summary: Optional[InjectionSummary] = None
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.events: list[tuple[float, str, str]] = []
        self.stage_transitions: list[StageTransition] = []
        self.drop_events: list[DropEvent] = []
        # G6B CHAR_BUDGET dedupe set: tracks distinct
        # ``(candidate_id, code, stage)`` triples so ``record_drop`` does
        # not double-count duplicate CHAR_BUDGET probes.  Lives on the
        # trace (not the summary) so it survives ``inject`` replacing the
        # summary object — internal contract detail.
        self._seen_char_budget_drops: set[tuple[str, DropReasonCode, str]] = set()
        self._lock = threading.RLock()

    @property
    def timed_out(self) -> bool:
        """True iff any lane finished with ``timed_out=True``."""
        return any(s.timed_out for s in self.lane_summaries.values())

    # ----- Lane control ----------------------------------------------

    def start_lane(
        self,
        lane: str,
        *,
        deadline_monotonic: float,
        started_monotonic: Optional[float] = None,
    ) -> None:
        if lane not in ALL_LANES:
            raise ValueError(f"unknown lane {lane!r}")
        with self._lock:
            now = float(started_monotonic) if started_monotonic is not None else time.monotonic()
            self.lane_summaries[lane].started = now
            self._lane_started[lane] = now

    def finish_lane(
        self,
        lane: str,
        *,
        candidate_count: int = 0,
        timed_out: bool = False,
        skipped: bool = False,
        error: str = "",
        reason: str = "",
        finished_monotonic: Optional[float] = None,
    ) -> None:
        if lane not in ALL_LANES:
            raise ValueError(f"unknown lane {lane!r}")
        with self._lock:
            now = float(finished_monotonic) if finished_monotonic is not None else time.monotonic()
            s = self.lane_summaries[lane]
            s.finished = now
            started = self._lane_started.get(lane, s.started)
            if started is not None:
                s.duration_ms = (now - started) * 1000.0
            s.candidate_count = max(0, int(candidate_count))
            s.timed_out = bool(timed_out)
            s.skipped = bool(skipped)
            s.error = error or ""
            s.reason = reason or ""
            if error:
                self.errors.append(f"{lane}: {error}")

    def skip_lane(self, lane: str, *, reason: str = "") -> None:
        if lane not in ALL_LANES:
            raise ValueError(f"unknown lane {lane!r}")
        with self._lock:
            now = time.monotonic()
            s = self.lane_summaries[lane]
            s.skipped = True
            s.finished = now
            s.reason = reason or ""

    # ----- Candidate & event recording -------------------------------

    def record_candidate(
        self,
        candidate: Any,
        *,
        capture_content: bool = False,
    ) -> None:
        """Record a candidate into the trace.

        The original ``RecallCandidate`` is NEVER retained.  Only its
        privacy-safe ``CandidateSnapshot`` is stored.
        ``capture_content=False`` (default) sets ``snapshot.content=None``.
        """
        from .contracts import RecallCandidate  # local import to avoid cycle

        if not isinstance(candidate, RecallCandidate):
            raise ValueError("candidate must be a RecallCandidate")
        with self._lock:
            snap = CandidateSnapshot(
                candidate_id=candidate.candidate_id,
                lane=candidate.lane,
                source_type=candidate.source_type,
                source_id=candidate.source_id,
                final_score=candidate.final_score,
                raw_score=candidate.raw_score,
                lane_score=candidate.lane_score,
                fusion_score=candidate.fusion_score,
                temporal_score=candidate.temporal_score,
                rerank_score=candidate.rerank_score,
                selected=candidate.selected,
                injected=candidate.injected,
                dropped=candidate.dropped,
                drop_reason=(DropReason(code=candidate.drop_reason.code, detail=candidate.drop_reason.detail, stage=candidate.drop_reason.stage) if candidate.drop_reason else None),
                content=candidate.content if capture_content else None,
                text_reference=candidate.text_reference,
                timestamp=candidate.timestamp,
                metadata=safe_metadata(candidate.metadata, include_content=False),
                provenance=[ProvenanceRecord(p.lane, p.source_type, p.source_id, ts=p.ts) for p in candidate.provenance],
                score_history=[ScoreRecord(s.stage, s.value, s.operation, dict(s.params) if s.params else {}) for s in candidate.score_history],
                events=[CandidateEvent(e.event_type, e.note, ts=e.ts) for e in candidate.events],
                contributing_lanes=list(candidate.contributing_lanes),
            )
            self.candidate_snapshots[candidate.candidate_id] = snap
            # Drop the reference to the original candidate — caller may still
            # hold it, but the trace itself does not.
            del candidate

    def record_score(
        self,
        candidate_id: str,
        stage: str,
        value: float,
        *,
        operation: str = "",
        **params: Any,
    ) -> None:
        """Record a score against the snapshot, without needing the original candidate."""
        with self._lock:
            snap = self.candidate_snapshots.get(candidate_id)
            if snap is None:
                raise KeyError(f"unknown candidate {candidate_id!r}")
            try:
                v = float(value)
            except (TypeError, ValueError) as e:
                raise ValueError(f"score value must be numeric: {e}")
            s = (stage or "").strip().lower()
            if not s:
                raise ValueError("stage must be non-empty")
            # Apply the value into the typed score-stage slot if known.
            _SCORE_FIELDS = {
                "raw": "raw_score",
                "lane": "lane_score", "vector": "lane_score", "keyword": "lane_score",
                "topic": "lane_score", "qa": "lane_score",
                "fusion": "fusion_score", "rrf": "fusion_score",
                "temporal": "temporal_score", "half_life": "temporal_score",
                "rerank": "rerank_score",
                "final": "final_score",
            }
            fld = _SCORE_FIELDS.get(s)
            if fld is not None:
                setattr(snap, fld, v)
            snap.score_history.append(ScoreRecord(s, v, operation, dict(params)))
            if s != "raw" and not any(
                e.event_type == CandidateEventType.SCORED for e in snap.events
            ):
                snap.events.append(CandidateEvent(CandidateEventType.SCORED))

    def record_drop(
        self,
        candidate_id: str,
        code: DropReasonCode,
        *,
        detail: str = "",
        stage: str = "",
    ) -> None:
        """Record a drop against the snapshot, without needing the original candidate.

        ``record_drop`` is the typed truth boundary for budget-drop
        bookkeeping on :attr:`injection_summary`:

        * When ``code is DropReasonCode.CHAR_BUDGET``, the trace
          guarantees ``injection_summary`` exists (creating it lazily
          even when no inject probe has landed yet),
          ``char_budget == int(query_plan.max_chars)``, every distinct
          ``(candidate_id, CHAR_BUDGET, stage)`` triple increments
          ``dropped_for_budget`` by exactly one, and ``truncated`` is
          ``True``.  Re-recording the same triple MUST NOT double-count
          — the typed identity is the existing typed drop triple and
          callers must NOT have to mutate trace internals.
        * Non-CHAR_BUDGET drops do NOT inflate ``dropped_for_budget``
          and do NOT create the budget summary (unless an inject
          probe had already created it).  ``inject``'s existing
          counters are preserved verbatim.

        Canonical ``drop_events`` semantics are unchanged — every call
        still appends one entry; the canonical evidence channel stays
        complete.
        """
        if not isinstance(code, DropReasonCode):
            raise ValueError("code must be a DropReasonCode")
        with self._lock:
            snap = self.candidate_snapshots.get(candidate_id)
            if snap is None:
                raise KeyError(f"unknown candidate {candidate_id!r}")
            reason = DropReason(code=code, detail=detail, stage=stage)
            snap.dropped = True
            snap.drop_reason = reason
            snap.events.append(CandidateEvent(CandidateEventType.DROPPED, code.value))
            self.drop_events.append(
                DropEvent(
                    candidate_id=candidate_id,
                    code=code,
                    detail=detail,
                    stage=stage,
                )
            )
            # G6B CHAR_BUDGET truth boundary.  Only CHAR_BUDGET drops
            # are budget-truth; every other code leaves injection_summary
            # alone (it may still be created later by an inject probe).
            if code is DropReasonCode.CHAR_BUDGET:
                # Lazy-create the summary on first CHAR_BUDGET drop,
                # even when injected_count is still 0.
                if self.injection_summary is None:
                    self.injection_summary = InjectionSummary(
                        char_budget=int(self.query_plan.max_chars)
                    )
                # Dedupe by typed identity (candidate_id, code, stage)
                # so duplicate record_drop calls do not double-count.
                seen_key = (candidate_id, code, stage)
                if seen_key not in self._seen_char_budget_drops:
                    self._seen_char_budget_drops.add(seen_key)
                    self.injection_summary.dropped_for_budget += 1
                # truncated is a one-way latch: once True, always True.
                self.injection_summary.truncated = True

    def record_stage_transition(
        self,
        candidate_id: str,
        from_stage: str,
        to_stage: str,
        *,
        reason: str = "",
        ts: Optional[float] = None,
    ) -> None:
        """Append a structured stage-transition record."""
        with self._lock:
            self.stage_transitions.append(
                StageTransition(
                    candidate_id=candidate_id,
                    from_stage=from_stage,
                    to_stage=to_stage,
                    reason=reason,
                    ts=float(ts) if ts is not None else time.monotonic(),
                )
            )

    def record_candidate_event(
        self,
        candidate_id: str,
        event_type: CandidateEventType,
        note: str = "",
    ) -> None:
        """Record a typed event against the candidate's snapshot."""
        if not isinstance(event_type, CandidateEventType):
            raise ValueError("event_type must be a CandidateEventType")
        with self._lock:
            snap = self.candidate_snapshots.get(candidate_id)
            if snap is None:
                raise KeyError(f"unknown candidate {candidate_id!r}")
            snap.events.append(CandidateEvent(event_type, note))

    def record_event(self, kind: str, note: str = "") -> None:
        if not kind:
            return
        with self._lock:
            self.events.append((time.monotonic(), kind, note))

    # ----- Selection & injection -------------------------------------

    def select(self, candidate_id: str) -> None:
        with self._lock:
            snap = self.candidate_snapshots.get(candidate_id)
            if snap is None:
                raise KeyError(f"unknown candidate {candidate_id!r}")
            if snap.dropped:
                raise ValueError(f"dropped candidate cannot be selected ({candidate_id})")
            snap.selected = True
            snap.events.append(CandidateEvent(CandidateEventType.SELECTED))
            if candidate_id not in self.final_selected_ids:
                self.final_selected_ids.append(candidate_id)

    def inject(self, candidate_id: str, *, char_count: int = 0) -> None:
        with self._lock:
            snap = self.candidate_snapshots.get(candidate_id)
            if snap is None:
                raise KeyError(f"unknown candidate {candidate_id!r}")
            if not snap.selected:
                raise ValueError(f"candidate must be selected before injected ({candidate_id})")
            snap.injected = True
            snap.events.append(CandidateEvent(CandidateEventType.INJECTED))
            if self.injection_summary is None:
                self.injection_summary = InjectionSummary(char_budget=int(self.query_plan.max_chars))
            self.injection_summary.injected_count += 1
            self.injection_summary.total_chars += max(0, int(char_count))
            if (
                self.injection_summary.char_budget > 0
                and self.injection_summary.total_chars > self.injection_summary.char_budget
            ):
                self.injection_summary.truncated = True

    def warn(self, message: str) -> None:
        if message:
            with self._lock:
                self.warnings.append(message)

    def error(self, message: str) -> None:
        if message:
            with self._lock:
                self.errors.append(message)

    def finish(self) -> None:
        with self._lock:
            if self.end_monotonic is None:
                self.end_monotonic = time.monotonic()
                self.duration_ms = (self.end_monotonic - self.start_monotonic) * 1000.0

    # ----- Serialization ---------------------------------------------

    def to_dict(
        self,
        *,
        include_content: bool = False,
        include_query: bool = False,
    ) -> dict[str, Any]:
        """Privacy-safe serialization.  Both flags default OFF."""
        with self._lock:
            if self.end_monotonic is None:
                self.end_monotonic = time.monotonic()
                self.duration_ms = (self.end_monotonic - self.start_monotonic) * 1000.0
            d: dict[str, Any] = {
                "trace_id": self.trace_id,
                "query_id": self.query_context.query_id,
                "plan_query_id": self.query_plan.query_id,
                "start_monotonic": self.start_monotonic,
                "end_monotonic": self.end_monotonic,
                "duration_ms": self.duration_ms,
                "deadline_monotonic": self.deadline_monotonic,
                "budget_ms": self.budget_ms,
                "lane_summaries": [self.lane_summaries[n].to_dict() for n in ALL_LANES],
                "candidates": [
                    self.candidate_snapshots[cid].to_dict(include_content=include_content)
                    for cid in list(self.candidate_snapshots.keys())
                ],
                "stage_transitions": [st.to_dict() for st in self.stage_transitions],
                "drop_events": [de.to_dict() for de in self.drop_events],
                "final_selected_ids": list(self.final_selected_ids),
                "injection_summary": (
                    self.injection_summary.to_dict() if self.injection_summary is not None else None
                ),
                "warnings": list(self.warnings),
                "errors": list(self.errors),
                "timed_out": self.timed_out,
            }
            if include_query:
                d["query_context"] = self.query_context.to_dict()
                d["query_plan"] = self.query_plan.to_dict()
            return d

    def to_json(self, *, include_content: bool = False, include_query: bool = False) -> str:
        return json.dumps(
            self.to_dict(include_content=include_content, include_query=include_query),
            sort_keys=True,
            ensure_ascii=False,
        )


def recall_trace_from_plan(
    context: QueryContext,
    plan: QueryPlan,
    *,
    trace_id: Optional[str] = None,
) -> RecallTrace:
    return RecallTrace(query_context=context, query_plan=plan, trace_id=trace_id)