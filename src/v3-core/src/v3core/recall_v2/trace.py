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
class LaneEvidence:
    """Diagnostic evidence for one retrieval lane's view of a candidate.

    Recorded additively by the recall sink when the legacy retrieval code
    reports that ``lane`` ranked ``candidate_id``.  This is a pure
    observability record — it never influences fusion / rerank / selection.

    ``present`` distinguishes "the lane returned this candidate" from a
    lane that did not (a caller may record ``present=False`` explicitly).
    ``raw_rank`` is the lane's own 1-based rank for the candidate;
    ``raw_score`` is the lane's raw score at fusion time (keyword-family
    lanes report the pre-fusion RRF score, vector-family lanes report the
    cosine; an explicit keyword result may expose no score). ``rrf_contribution``
    is the candidate's additive contribution from this
    retrieval path, i.e. ``weight / (K + raw_rank)``. A canonical lane may
    have multiple separately recorded RRF paths (for example ``qa_keyword``
    and ``qa_vector``); the explicit-memory source reuses keyword/vector RRF
    paths and therefore has no independent RRF term (its provenance paths
    carry contribution 0).
    """

    lane: str
    path: str = ""
    present: bool = False
    raw_rank: Optional[int] = None
    raw_score: Optional[float] = None
    rrf_contribution: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "path": self.path,
            "present": bool(self.present),
            "raw_rank": self.raw_rank,
            "raw_score": self.raw_score,
            "rrf_contribution": self.rrf_contribution,
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
    # ----- P0-B4-A diagnostic fields (additive, observability only) -----
    # ``kind`` is the legacy retrieval kind of the hit (``qa`` / ``topic`` /
    # ``card`` / ``message`` / ``note`` / ``active_memory`` / ``yin`` …),
    # i.e. the wire kind — distinct from ``source_type``, which is the
    # lane-recorded source classification.
    kind: str = ""
    # One entry per physical retrieval/RRF path; several paths may share one
    # canonical lane (e.g. QA keyword + QA vector).
    lane_evidence: list[LaneEvidence] = field(default_factory=list)
    # 1-based positions that let a reviewer tell an admission miss from a
    # fusion miss / rerank miss / final-limit miss.  ``None`` means the
    # candidate never reached that stage.
    pre_fusion_position: Optional[int] = None
    post_fusion_position: Optional[int] = None
    rerank_input_position: Optional[int] = None
    rerank_provider_index: Optional[int] = None
    final_position: Optional[int] = None

    @property
    def lane_vote_count(self) -> int:
        """Count distinct canonical retrieval lanes with positive RRF contribution."""
        return sum(
            1
            for lane in {
                ev.lane for ev in self.lane_evidence
                if ev.present and ev.rrf_contribution is not None and ev.rrf_contribution > 0
            }
        )

    @property
    def rrf_path_count(self) -> int:
        """Count distinct physical RRF paths with positive contribution."""
        return sum(
            1
            for ev in self.lane_evidence
            if ev.present and ev.rrf_contribution is not None and ev.rrf_contribution > 0
        )

    def lane_evidence_for(
        self, lane: str, path: Optional[str] = None
    ) -> Optional[LaneEvidence]:
        """Return lane evidence, optionally for one physical retrieval path."""
        for ev in self.lane_evidence:
            if ev.lane == lane and (path is None or ev.path == path):
                return ev
        return None

    def lane_presence(self) -> dict[str, bool]:
        """Explicit ``{lane: present}`` map over the five canonical lanes.

        A lane with no recorded evidence is ``False`` — i.e. it did not
        return this candidate (an *admission miss* for that lane).
        """
        return {
            lane: any(ev.present for ev in self.lane_evidence if ev.lane == lane)
            for lane in ALL_LANES
        }

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "lane": self.lane,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "kind": self.kind,
            "contributing_lanes": list(self.contributing_lanes),
            "lane_evidence": [ev.to_dict() for ev in self.lane_evidence],
            "lane_vote_count": self.lane_vote_count,
            "rrf_path_count": self.rrf_path_count,
            "lane_presence": self.lane_presence(),
            "pre_fusion_position": self.pre_fusion_position,
            "post_fusion_position": self.post_fusion_position,
            "rerank_input_position": self.rerank_input_position,
            "rerank_provider_index": self.rerank_provider_index,
            "final_position": self.final_position,
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
                # ``source_type`` on a legacy candidate is the hit's wire
                # kind (see adapters.hit_to_candidate); mirror it onto the
                # diagnostic ``kind`` field so the snapshot is self-describing.
                kind=str(candidate.source_type or ""),
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

    # ----- P0-B4-A diagnostic recorders ------------------------------
    # These are pure observability: they write diagnostic fields on the
    # candidate snapshot and never touch scores / ordering / selection.

    def record_kind(self, candidate_id: str, kind: str) -> None:
        """Record the legacy wire kind (``qa`` / ``topic`` / ``card`` …)."""
        with self._lock:
            snap = self.candidate_snapshots.get(candidate_id)
            if snap is None:
                raise KeyError(f"unknown candidate {candidate_id!r}")
            if kind:
                snap.kind = str(kind)

    def record_lane_evidence(
        self,
        candidate_id: str,
        lane: str,
        *,
        path: Optional[str] = None,
        present: bool = True,
        raw_rank: Any = None,
        raw_score: Any = None,
        rrf_contribution: Any = None,
    ) -> None:
        """Record one physical retrieval path under its canonical lane.

        Duplicate records for the same ``(lane, path)`` aggregate: presence
        is OR-ed, rank keeps the minimum, score keeps the maximum, and RRF
        contributions are summed. Different paths stay separate so each
        actual fusion vote remains inspectable.
        """
        if lane not in ALL_LANES:
            return
        path_name = str(path or lane)
        with self._lock:
            snap = self.candidate_snapshots.get(candidate_id)
            if snap is None:
                raise KeyError(f"unknown candidate {candidate_id!r}")
            ev = snap.lane_evidence_for(lane, path_name)
            if ev is None:
                ev = LaneEvidence(lane=lane, path=path_name, present=bool(present))
                snap.lane_evidence.append(ev)
            elif present:
                ev.present = True
            if raw_rank is not None:
                try:
                    r = int(raw_rank)
                except (TypeError, ValueError):
                    r = None
                if r is not None:
                    ev.raw_rank = r if ev.raw_rank is None else min(ev.raw_rank, r)
            if raw_score is not None:
                try:
                    v = float(raw_score)
                except (TypeError, ValueError):
                    v = None
                if v is not None:
                    ev.raw_score = v if ev.raw_score is None else max(ev.raw_score, v)
            if rrf_contribution is not None:
                try:
                    c = float(rrf_contribution)
                except (TypeError, ValueError):
                    c = None
                if c is not None:
                    ev.rrf_contribution = (
                        c if ev.rrf_contribution is None
                        else ev.rrf_contribution + c
                    )

    def record_fusion_position(
        self,
        candidate_id: str,
        *,
        pre_fusion_position: Any = None,
        post_fusion_position: Any = None,
        fusion_score: Any = None,
    ) -> None:
        """Record positions and the effective fused score used for sorting."""
        with self._lock:
            snap = self.candidate_snapshots.get(candidate_id)
            if snap is None:
                raise KeyError(f"unknown candidate {candidate_id!r}")
            if pre_fusion_position is not None:
                try:
                    snap.pre_fusion_position = int(pre_fusion_position)
                except (TypeError, ValueError):
                    pass
            if post_fusion_position is not None:
                try:
                    snap.post_fusion_position = int(post_fusion_position)
                except (TypeError, ValueError):
                    pass
            if fusion_score is not None:
                try:
                    snap.fusion_score = float(fusion_score)
                except (TypeError, ValueError):
                    pass

    def record_rerank_evidence(
        self,
        candidate_id: str,
        *,
        input_position: Any = None,
        provider_index: Any = None,
        score: Any = None,
    ) -> None:
        """Record rerank input position, provider index and rerank score.

        ``input_position`` is the 1-based position in the rerank input list;
        ``provider_index`` is the (0-based) document index the rerank
        provider assigned to the candidate; ``score`` is the rerank score.
        """
        with self._lock:
            snap = self.candidate_snapshots.get(candidate_id)
            if snap is None:
                raise KeyError(f"unknown candidate {candidate_id!r}")
            if input_position is not None:
                try:
                    snap.rerank_input_position = int(input_position)
                except (TypeError, ValueError):
                    pass
            if provider_index is not None:
                try:
                    snap.rerank_provider_index = int(provider_index)
                except (TypeError, ValueError):
                    pass
            if score is not None:
                try:
                    snap.rerank_score = float(score)
                except (TypeError, ValueError):
                    pass

    def record_final_position(self, candidate_id: str, position: Any) -> None:
        """Record the candidate's 1-based position in the final result list."""
        with self._lock:
            snap = self.candidate_snapshots.get(candidate_id)
            if snap is None:
                raise KeyError(f"unknown candidate {candidate_id!r}")
            try:
                snap.final_position = int(position)
            except (TypeError, ValueError):
                pass

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