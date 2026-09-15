# -*- coding: utf-8 -*-
"""G6B Slice A — Recall V2 orchestration engine.

The :class:`RecallV2Engine` is a thin orchestrator that:

1. Builds a :class:`QueryContext` and a :class:`QueryPlan` **before**
   any retrieval work happens.
2. Constructs a :class:`RecallTrace` and a duck-typed :class:`LegacySink`
   that translates the legacy ``getattr(trace, ...)`` hooks into the new
   typed trace API.
3. Calls the legacy recall function **exactly once** on the success
   path, passing the original deadline object unchanged plus the sink
   via a ``trace=`` keyword argument.
4. Converts each returned ``RecallHit`` into a
   :class:`RecallCandidate` (lane from the sink's recorded real lane
   membership when available, else :func:`lane_for_kind`) and bundles
   the whole result into a :class:`RecallV2Result`.

Pre-execution fallback:
    If :func:`build_query_context`, :func:`build_default_query_plan`,
    or :class:`RecallTrace` / :class:`LegacySink` construction raises,
    the engine falls back to calling the legacy function **once with no
    trace kwarg**, sets ``fallback_used=True`` and a non-empty
    ``fallback_reason``.  This is the **only** fallback path.  If the
    retrieval call itself raises, the exception propagates unchanged
    (no retry, no fallback).

Signature-adaptive behaviour:
    If the legacy callable does not accept a ``trace`` keyword argument
    (detected via :func:`inspect.signature`), the kwarg is skipped up
    front and ``trace_attached`` is set to ``False``.  ``max_chars`` is
    the one keyword that follows a STRICTER rule (only forwarded when
    the signature declares it explicitly — a ``**kwargs`` rescue does
    not count as acceptance).  G6B Slice G: validation happens BEFORE
    invocation; once the callable is invoked, ANY exception propagates
    unchanged — the engine never inspects an exception message to
    decide whether to call again.

No sleeps, no threads, no I/O beyond what the wrapped legacy call
performs.


Sink protocol (duck-typed)
--------------------------

The legacy code calls these methods on the trace via ``getattr`` and
``inspect``, without importing this module.  The :class:`LegacySink`
implements ALL of them and translates each call into the equivalent
typed :class:`RecallTrace` API.

Lane control::

    lane_start(lane: str) -> None
    lane_finish(lane: str, candidate_count: int = 0, skipped: bool = False,
                timed_out: bool = False, error: str = '', reason: str = '') -> None
    lane_candidates(lane: str, source_ids, source_type: str = '',
                    scores: Iterable | None = None) -> None

Per-candidate::

    score(candidate_id: str, stage: str, value: float,
          operation: str = '', **params: Any) -> None
    event(candidate_id: str, event_type: CandidateEventType,
          note: str = '') -> None
    drop(candidate_id: str, code: DropReasonCode,
         detail: str = '', stage: str = '') -> None

Selection / injection::

    select(candidate_id: str) -> None
    inject(candidate_id: str, char_count: int = 0) -> None

Free-form::

    warn(text: str) -> None
    error(text: str) -> None

Stage names accepted: ``raw`` / ``lane`` (or lane-specific synonyms) /
``fusion`` (or ``rrf``) / ``temporal`` (or ``half_life``) / ``rerank``
/ ``final``.  Event types accepted: :class:`CandidateEventType` members
(or their ``.value`` strings).  Drop codes accepted:
:class:`DropReasonCode` members (or their ``.value`` strings).  Unknown
or invalid inputs are ignored silently — the sink never raises into a
caller.
"""
from __future__ import annotations

import dataclasses
import functools
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

from . import adapters
from .contracts import (
    CandidateEventType,
    DropReasonCode,
    ProvenanceRecord,
    QueryContext,
    QueryPlan,
    RecallCandidate,
    build_default_query_plan,
    build_effective_query_plan,
)
from .trace import RecallTrace

# Re-export the build helpers at module scope so tests can monkey-patch
# ``engine.build_default_query_plan`` / ``engine.build_effective_query_plan``
# without going through ``adapters.build_query_context`` indirection.
# (noqa markers below keep linters quiet about the redundant import.)


# ---------------------------------------------------------------------------
# Lazy default-factory helper for the recall function.
# ---------------------------------------------------------------------------


def _default_recall_fn() -> Callable[..., Any]:
    """Resolve the legacy ``recall_pool`` lazily to avoid import cycles."""
    from v3core import recall_pool

    return recall_pool.recall_pool


# ---------------------------------------------------------------------------
# LegacySink
# ---------------------------------------------------------------------------


_VALID_LANES: frozenset[str] = frozenset({
    "keyword", "vector", "topic", "qa", "explicit",
})

_VALID_STAGES: frozenset[str] = frozenset({
    "raw", "lane", "vector", "keyword", "topic", "qa",
    "fusion", "rrf", "temporal", "half_life", "rerank", "final",
})

_VALID_DROP_CODES: frozenset[DropReasonCode] = frozenset(DropReasonCode)

_STAGE_TO_FIELD: dict[str, str] = {
    "raw": "raw_score",
    "lane": "lane_score", "vector": "lane_score",
    "keyword": "lane_score", "topic": "lane_score", "qa": "lane_score",
    "fusion": "fusion_score", "rrf": "fusion_score",
    "temporal": "temporal_score", "half_life": "temporal_score",
    "rerank": "rerank_score",
    "final": "final_score",
}


def _hit_kind(h: Any) -> str:
    """Safely read a ``kind`` attribute from a hit-like object."""
    try:
        return str(getattr(h, "kind", "") or "")
    except Exception:
        return ""


def _deadline_enforced(deadline: Any) -> bool:
    """Return ``True`` only when ``deadline`` is a set ``PrefetchDeadline``.

    The engine forwards ``deadline`` to the legacy callable unchanged.  The
    real ``v3core._deadline.coerce_deadline`` rules: ``None`` returns
    ``None``, an unset ``PrefetchDeadline`` (one with ``is_set() == False``)
    also returns ``None``.  We mirror that by requiring both
    ``coerce_deadline`` to return a non-``None`` value AND the original
    value to be a ``PrefetchDeadline`` with ``is_set() == True``.
    """
    if deadline is None:
        return False
    try:
        # Lazy import to avoid a circular reference at module load time.
        from v3core._deadline import PrefetchDeadline, coerce_deadline
    except Exception:
        return False
    if not isinstance(deadline, PrefetchDeadline):
        # Coerced numeric / no-deadline values map to "no enforcement".
        return False
    try:
        bound = coerce_deadline(deadline)
    except Exception:
        return False
    if bound is None:
        return False
    try:
        return bool(bound.is_set())
    except Exception:
        return False


class LegacySink:
    """Duck-typed adapter from the legacy ``getattr(trace, ...)`` hooks
    to a typed :class:`RecallTrace`.

    Every method is **exception-safe** (swallows internal errors, never
    raises into callers) and **cheap** (no serialization, no I/O, no
    sleeping).  Unknown lanes / stages / event types / drop codes are
    ignored silently.
    """

    __slots__ = ("_trace",)

    def __init__(self, trace: Any) -> None:
        # Defensive: never blow up at construction.
        try:
            self._trace = trace
        except Exception:
            self._trace = None

    # ---- helpers ----------------------------------------------------

    @staticmethod
    def _safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            # Sink must never raise.
            return

    def _candidate_snapshot(self, candidate_id: str) -> Any:
        if self._trace is None:
            return None
        try:
            return self._trace.candidate_snapshots.get(candidate_id)
        except Exception:
            return None

    def _ensure_snapshot_score_fields(self, snap: Any) -> None:
        """Ensure the snapshot has every score field attribute so
        ``setattr`` never fails on a fresh snapshot.  RecallTrace's
        ``CandidateSnapshot`` already declares all six fields, but be
        defensive against future schema drift."""
        if snap is None:
            return
        for fld in (
            "raw_score", "lane_score", "fusion_score",
            "temporal_score", "rerank_score", "final_score",
        ):
            try:
                if not hasattr(snap, fld):
                    setattr(snap, fld, None)
            except Exception:
                return

    # ---- public protocol -------------------------------------------

    def lane_start(self, lane: str) -> None:
        if lane not in _VALID_LANES or self._trace is None:
            return
        # ``start_lane`` requires ``deadline_monotonic``; pull from the
        # trace's ``query_context`` which always exists.
        try:
            start_lane = getattr(self._trace, "start_lane", None)
            if start_lane is None:
                return
            try:
                deadline_mono = float(self._trace.query_context.deadline_monotonic)
            except Exception:
                deadline_mono = 0.0
            self._safe(start_lane, lane, deadline_monotonic=deadline_mono)
        except Exception:
            return

    def lane_finish(
        self,
        lane: str,
        candidate_count: int = 0,
        skipped: bool = False,
        timed_out: bool = False,
        error: str = "",
        reason: str = "",
    ) -> None:
        if lane not in _VALID_LANES or self._trace is None:
            return
        try:
            cc = int(candidate_count)
        except (TypeError, ValueError):
            cc = 0
        try:
            finish_lane = getattr(self._trace, "finish_lane", None)
            if finish_lane is None:
                return
            self._safe(
                finish_lane,
                lane,
                candidate_count=cc,
                timed_out=bool(timed_out),
                skipped=bool(skipped),
                error=str(error or ""),
                reason=str(reason or ""),
            )
        except Exception:
            return

    def lane_candidates(
        self,
        lane: str,
        source_ids: Iterable[Any],
        source_type: str = "",
        scores: Optional[Iterable[Any]] = None,
    ) -> None:
        """Record that ``lane`` produced the listed ``source_ids``.

        For each (id, score) pair we build or update a CandidateSnapshot
        so that the engine can later look up the **real lane** membership
        via ``recorded_lane_for``.  G6B Slice D (D3): every distinct
        (lane, source_type, source_id) triple is appended to the
        snapshot's provenance list exactly once, and every distinct
        lane name is recorded in the snapshot's ``contributing_lanes``
        list in first-seen order.  The snapshot's primary ``lane``
        field stays the first lane that recorded the id.
        """
        if lane not in _VALID_LANES or self._trace is None:
            return
        try:
            ids = list(source_ids or ())
        except TypeError:
            ids = []
        try:
            scr_list = list(scores) if scores is not None else []
        except TypeError:
            scr_list = []

        # Bump the lane summary count to keep totals honest even if
        # ``lane_finish`` never gets called explicitly.
        try:
            self._trace.lane_summaries[lane].candidate_count += len(ids)
        except Exception:
            pass

        # Track ids per lane in a private dict so the engine can read
        # back which lane actually found a hit.  This is duck-typed and
        # lives on the trace object itself (we just slap on an attr —
        # trace is our own construction, so this is safe).
        bucket = self._recorded_lanes()
        for idx, sid in enumerate(ids):
            if not sid:
                continue
            cid = str(sid)
            existing = bucket.get(cid)
            if existing is None:
                bucket[cid] = lane
            else:
                # Multi-lane provenance: append.
                if lane not in existing:
                    bucket[cid] = tuple(existing) + (lane,)
            # Make sure a CandidateSnapshot exists so the trace is
            # self-consistent if the engine later looks it up.
            _score = scr_list[idx] if idx < len(scr_list) else None
            snap = self._ensure_snapshot(cid, lane=lane, source_type=source_type,
                                         score=_score)
            # G6B Slice D (D3): record provenance for every (lane, id)
            # the sink sees.  The snapshot's primary ``lane`` is preserved
            # (set by ``_ensure_snapshot`` only on first creation); we
            # only APPEND provenance entries here.
            if snap is not None:
                self._append_provenance(snap, lane, source_type, cid)

    def _recorded_lanes(self) -> dict[str, Any]:
        if self._trace is None:
            return {}
        d = getattr(self._trace, "_recorded_lanes", None)
        if d is None:
            d = {}
            try:
                self._trace._recorded_lanes = d
            except Exception:
                pass
        return d

    def recorded_lane_for(self, candidate_id: str) -> Optional[str]:
        """Return the *primary* lane recorded by the sink for a candidate.

        Returns the first lane seen for ``candidate_id`` (which is the
        lane whose ``lane_candidates`` invocation introduced the id),
        or ``None`` if the sink never recorded it.  Used by the engine
        when picking a ``lane`` for each converted
        :class:`RecallCandidate`.
        """
        if not candidate_id:
            return None
        rec = self._recorded_lanes().get(candidate_id)
        if rec is None:
            return None
        if isinstance(rec, (tuple, list)):
            return rec[0] if rec else None
        return rec

    def recorded_extra_lanes_for(self, candidate_id: str) -> tuple[str, ...]:
        """Return all *additional* lanes recorded for a candidate.

        This is used by the engine so each candidate's provenance can
        carry every contributing lane (the primary lane is recorded
        via the candidate's primary ``lane`` field).
        """
        if not candidate_id:
            return ()
        rec = self._recorded_lanes().get(candidate_id)
        if rec is None:
            return ()
        if isinstance(rec, (tuple, list)):
            return tuple(rec[1:])
        return ()

    def _ensure_snapshot(
        self,
        candidate_id: str,
        *,
        lane: str,
        source_type: str = "",
        score: Any = None,
    ) -> Optional[Any]:
        """Ensure a CandidateSnapshot exists for ``candidate_id``.

        Returns the snapshot object (existing or newly created) so the
        caller can record provenance against it, or ``None`` if the
        snapshot could not be created.  Never raises.
        """
        if self._trace is None:
            return None
        try:
            existing = self._trace.candidate_snapshots.get(candidate_id)
        except Exception:
            return None
        if existing is not None:
            return existing
        # Create a privacy-safe CandidateSnapshot directly.
        try:
            from .trace import CandidateSnapshot
        except Exception:
            return None
        try:
            snap = CandidateSnapshot(
                candidate_id=str(candidate_id),
                lane=lane,
                source_type=str(source_type or ""),
                source_id=str(candidate_id),
                content=None,
                text_reference=str(candidate_id),
            )
            self._trace.candidate_snapshots[str(candidate_id)] = snap
            return snap
        except Exception:
            return None

    def _append_provenance(
        self,
        snapshot: Any,
        lane: str,
        source_type: str,
        source_id: str,
    ) -> None:
        """Append a ProvenanceRecord + contributing_lanes entry.

        Exception-safe and cheap (no I/O, no serialization).  Appends a
        new ``ProvenanceRecord(lane, source_type, source_id)`` ONLY when
        no equivalent entry already exists (same lane + same source_type
        + same source_id).  Also updates ``contributing_lanes`` so every
        distinct lane is listed in first-seen order.
        """
        if snapshot is None or self._trace is None:
            return
        # De-duplicate: scan existing provenance list for an equivalent
        # (lane, source_type, source_id) triple.
        try:
            existing_prov = getattr(snapshot, "provenance", None)
            if existing_prov is None:
                snapshot.provenance = []
                existing_prov = snapshot.provenance
            for p in existing_prov:
                try:
                    if (
                        getattr(p, "lane", None) == lane
                        and getattr(p, "source_type", None) == source_type
                        and getattr(p, "source_id", None) == source_id
                    ):
                        # Also ensure contributing_lanes already lists
                        # this lane (it should if the entry was appended
                        # earlier by this same helper).
                        try:
                            cl = getattr(snapshot, "contributing_lanes", None)
                            if cl is not None and lane not in cl:
                                cl.append(lane)
                        except Exception:
                            pass
                        return
                except Exception:
                    continue
            # New provenance entry — append + update contributing_lanes.
            try:
                rec = ProvenanceRecord(
                    str(lane), str(source_type or ""), str(source_id)
                )
                existing_prov.append(rec)
            except Exception:
                return
            try:
                cl = getattr(snapshot, "contributing_lanes", None)
                if cl is None:
                    snapshot.contributing_lanes = []
                    cl = snapshot.contributing_lanes
                if lane not in cl:
                    cl.append(lane)
            except Exception:
                pass
        except Exception:
            # Sink must never raise.
            return

    def score(
        self,
        candidate_id: str,
        stage: str,
        value: Any,
        operation: str = "",
        **params: Any,
    ) -> None:
        if self._trace is None:
            return
        snap = self._candidate_snapshot(candidate_id)
        if snap is None:
            # Create a minimal snapshot so the score call is not silently
            # lost (the legacy trace may score before record_candidate).
            self._ensure_snapshot(candidate_id, lane="vector", source_type="")
            snap = self._candidate_snapshot(candidate_id)
        if snap is None:
            return
        # Use the trace's own record_score so all invariants hold.
        try:
            self._safe(
                self._trace.record_score,
                candidate_id,
                stage,
                value,
                operation=operation,
                **params,
            )
        except Exception:
            return
        # Also keep the typed field on the snapshot in sync — record_score
        # already does this, but only for known fields; we mirror it
        # defensively in case a custom stage slipped through.
        s = (stage or "").strip().lower()
        fld = _STAGE_TO_FIELD.get(s)
        if fld is not None:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return
            try:
                self._ensure_snapshot_score_fields(snap)
                setattr(snap, fld, v)
            except Exception:
                return

    def event(
        self,
        candidate_id: str,
        event_type: Any,
        note: str = "",
    ) -> None:
        if self._trace is None:
            return
        # Accept either a CandidateEventType member or its .value string.
        if not isinstance(event_type, CandidateEventType):
            if isinstance(event_type, str):
                try:
                    event_type = CandidateEventType(event_type)
                except ValueError:
                    return
            else:
                return
        record_evt = getattr(self._trace, "record_candidate_event", None)
        if record_evt is None:
            return
        self._safe(record_evt, candidate_id, event_type, str(note or ""))

    def drop(
        self,
        candidate_id: str,
        code: Any,
        detail: str = "",
        stage: str = "",
    ) -> None:
        if self._trace is None:
            return
        if not isinstance(code, DropReasonCode):
            if isinstance(code, str):
                try:
                    code = DropReasonCode(code)
                except ValueError:
                    return
            else:
                return
        # Make sure a snapshot exists.
        self._ensure_snapshot(candidate_id, lane="vector", source_type="")
        record_drop = getattr(self._trace, "record_drop", None)
        if record_drop is None:
            return
        self._safe(
            record_drop,
            candidate_id,
            code,
            detail=str(detail or ""),
            stage=str(stage or ""),
        )

    def select(self, candidate_id: str) -> None:
        if self._trace is None:
            return
        self._ensure_snapshot(candidate_id, lane="vector", source_type="")
        select_fn = getattr(self._trace, "select", None)
        if select_fn is None:
            return
        self._safe(select_fn, candidate_id)

    def inject(self, candidate_id: str, char_count: int = 0) -> None:
        if self._trace is None:
            return
        self._ensure_snapshot(candidate_id, lane="vector", source_type="")
        try:
            cc = int(char_count)
        except (TypeError, ValueError):
            cc = 0
        inject_fn = getattr(self._trace, "inject", None)
        if inject_fn is None:
            return
        self._safe(inject_fn, candidate_id, char_count=cc)

    def warn(self, text: str) -> None:
        if self._trace is None or not text:
            return
        # Prefer ``trace.warn`` (a method on :class:`RecallTrace` and any
        # duck-typed trace that exposes the same shape) when it exists —
        # that path appends under the trace's own lock so concurrent
        # writers cannot race with the trace's serialization.  Fall back
        # to a direct append to ``trace.warnings`` only when the method
        # is absent (arbitrary duck-typed trace object).  Either way the
        # call is exception-safe (a broken trace cannot raise into the
        # caller) and capped at 300 chars per entry to stay cheap.
        try:
            capped = str(text)[:300]
            warn_method = getattr(self._trace, "warn", None)
            if callable(warn_method):
                warn_method(capped)
                return
            lst = getattr(self._trace, "warnings", None)
            if lst is None:
                return
            lst.append(capped)
        except Exception:
            return

    def error(self, text: str) -> None:
        if self._trace is None or not text:
            return
        # Mirror ``warn``: prefer ``trace.error`` (a method on
        # :class:`RecallTrace` and any duck-typed trace that exposes the
        # same shape) so the append runs under the trace's own lock,
        # and only fall back to a direct append to ``trace.errors`` when
        # the method is absent.  Exception-safe + 300-char cap.
        try:
            capped = str(text)[:300]
            err_method = getattr(self._trace, "error", None)
            if callable(err_method):
                err_method(capped)
                return
            lst = getattr(self._trace, "errors", None)
            if lst is None:
                return
            lst.append(capped)
        except Exception:
            return


# ---------------------------------------------------------------------------
# RecallV2Result
# ---------------------------------------------------------------------------


@dataclass
class RecallV2Result:
    """One orchestrated recall attempt.

    ``hits`` are the **unchanged** legacy :class:`RecallHit` objects
    returned by the wrapped recall function.  ``candidates`` are the
    G6A :class:`RecallCandidate` conversions (one per hit).  ``trace``
    is the typed :class:`RecallTrace` for this request.  ``trace_attached``
    is ``False`` only when the legacy callable refused the ``trace=``
    kwarg.  ``fallback_used`` is ``True`` only when pre-execution
    context / plan / trace construction raised and the engine had to
    fall back to a no-trace call.
    """

    hits: list[Any] = field(default_factory=list)
    trace: Optional[RecallTrace] = None
    pg_fail: bool = False
    query_context: Optional[QueryContext] = None
    query_plan: Optional[QueryPlan] = None
    candidates: list[RecallCandidate] = field(default_factory=list)
    deadline_enforced: bool = False
    fallback_used: bool = False
    fallback_reason: str = ""
    trace_attached: bool = True


# ---------------------------------------------------------------------------
# RecallV2Engine
# ---------------------------------------------------------------------------


@dataclass
class _BuiltContext:
    """Internal container for the artefacts built BEFORE any retrieval."""

    query_context: QueryContext
    query_plan: QueryPlan
    trace: RecallTrace
    sink: LegacySink


class RecallV2Engine:
    """Thin orchestrator that wraps the legacy recall function.

    On the success path the legacy function is invoked **exactly once**.
    Pre-execution errors (context / plan / trace construction) fall
    back to a single no-trace invocation.  Retrieval-time errors
    propagate unchanged — no retry, no fallback.
    """

    def __init__(
        self,
        *,
        recall_fn: Optional[Callable[..., Any]] = None,
        trace_enabled: bool = True,
    ) -> None:
        self._trace_enabled = bool(trace_enabled)
        self._recall_fn: Callable[..., Any]
        if recall_fn is None:
            self._recall_fn = _default_recall_fn()
        else:
            self._recall_fn = recall_fn

    # ----- signature inspection helpers --------------------------------

    def _callable_accepts(
        self, kwarg_name: str, *, strict: bool = False
    ) -> Optional[bool]:
        """Inspect ``self._recall_fn`` to see whether it accepts ``kwarg_name``.

        Returns ``True``/``False`` deterministically, or ``None`` if the
        signature cannot be inspected (e.g. C builtins).

        By default (``strict=False``) a ``VAR_KEYWORD`` parameter
        (``**kwargs``) is treated as accepting EVERY keyword we forward —
        this is the lenient rule used for the legacy ``trace`` kwarg and
        every other forwarded keyword.

        When ``strict=True``, ONLY an explicit named parameter counts as
        accepting the keyword; a ``VAR_KEYWORD`` (``**kwargs``) rescue
        is NOT considered acceptance.  This stricter rule is reserved
        for keywords that we KNOW do not belong to the legacy callable
        (``max_chars`` is the only one today — it belongs to
        ``prefetch_to_context_block``, not ``recall_pool``).  Without
        this, a thin ``def wrapper(*a, **k)`` around ``recall_pool``
        (a spy, a tracing adapter, a decorator) would trick the engine
        into forwarding ``max_chars`` and the wrapped call would die
        with ``TypeError: recall_pool() got an unexpected keyword
        argument 'max_chars'``.
        """
        try:
            sig = inspect.signature(self._recall_fn)
        except (TypeError, ValueError):
            return None
        try:
            params = sig.parameters
        except Exception:
            return None
        if kwarg_name in params:
            return True
        if strict:
            # Strict mode: **kwargs is NOT a rescue for this keyword.
            return False
        for p in params.values():
            if p.kind == inspect.Parameter.VAR_KEYWORD:
                return True
        return False

    def _callable_accepts_trace(self) -> Optional[bool]:
        return self._callable_accepts("trace")

    # ----- pre-execution kwarg validation -------------------------------

    #: Keywords the engine treats as OPTIONAL droppables when the
    #: legacy callable's signature does not accept them.  These are the
    #: ONLY keywords the pre-execution validator is allowed to drop —
    #: every other forwarded keyword is part of the legacy contract
    #: that the engine must not silently strip.
    _DROPPABLE_OPTIONAL_KWARGS: frozenset[str] = frozenset({"trace", "max_chars"})

    def _validate_kwargs_for_callable(
        self,
        call_kwargs: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Pre-execution validation: drop unsupported OPTIONAL keywords.

        G6B Slice G (Part 1 / HIGH A) — the engine must never inspect
        an exception message to decide whether to call the legacy
        callable a second time.  Instead, BEFORE invoking, the engine
        uses :func:`inspect.signature` to determine which optional
        keywords (today: ``trace`` and ``max_chars``) the callable
        accepts, and drops the rest from the forwarded payload so the
        call is made exactly once with a payload the callable accepts.

        Return: ``(filtered_kwargs, signature_was_inspectable)``.

        Behaviour rules:

        * For each optional kwarg in :data:`_DROPPABLE_OPTIONAL_KWARGS`
          that is present in ``call_kwargs``:

          - If the signature is inspectable and the callable's
            signature accepts the keyword (per :meth:`_callable_accepts`),
            keep the keyword.
          - If the signature is inspectable and the callable does NOT
            accept the keyword, drop the keyword from the payload.
          - If the signature is NOT inspectable (``None`` from
            :meth:`_callable_accepts`), the conservative single-call
            path: drop BOTH ``trace`` and ``max_chars`` (when present)
            so a C builtin or unusual wrapper that cannot be introspected
            never receives a keyword it might reject.  Correctness
            beats tracing.

        * Never drop a keyword the legacy contract requires: every
          keyword outside :data:`_DROPPABLE_OPTIONAL_KWARGS` is left
          in the payload unchanged.  The :meth:`_build_legacy_kwargs`
          adaptive rules already strip the rest of the surface against
          the callable's signature; this validator only re-checks the
          two known-optional droppables and is intentionally narrow.

        * No exception ever propagates out of this method: it is a pure
          pre-flight inspection.
        """
        signature_was_inspectable = True
        filtered: dict[str, Any] = dict(call_kwargs)
        for kw in self._DROPPABLE_OPTIONAL_KWARGS:
            if kw not in filtered:
                continue
            try:
                # ``max_chars`` follows the STRICT rule (a ``**kwargs``
                # rescue does not count as acceptance); ``trace`` and
                # every other optional droppable follow the LENIENT
                # rule.  This mirrors the contract already enforced by
                # :meth:`_build_legacy_kwargs` so the validator cannot
                # disagree with the kwarg builder.
                strict = kw == "max_chars"
                accepted = self._callable_accepts(kw, strict=strict)
            except Exception:
                # Defensive: if introspection itself blows up, treat as
                # uninspectable and drop the keyword conservatively.
                accepted = None
            if accepted is None:
                # Uninspectable signature: conservative path.
                signature_was_inspectable = False
                filtered.pop(kw, None)
                continue
            if accepted is False:
                filtered.pop(kw, None)
        return filtered, signature_was_inspectable

    def _invoke_once(
        self,
        query: Any,
        call_kwargs: dict[str, Any],
    ) -> tuple[list[Any], bool]:
        """Invoke the legacy callable EXACTLY ONCE with the validated payload.

        G6B Slice G (Part 1 / HIGH A) — once the wrapped callable is
        invoked, ANY exception (including :class:`TypeError`) propagates
        unchanged.  The engine must never inspect an exception message to
        decide whether to call again.  This is the only post-IO safety
        surface; the actual drop / strip of unsupported optional kwargs
        happened in :meth:`_validate_kwargs_for_callable` BEFORE the
        invocation.
        """
        result = self._recall_fn(query, **call_kwargs)
        return result[0], result[1]

    # ----- core orchestration ------------------------------------------

    def _build_pre_execution(
        self,
        *,
        query: str,
        limit: int,
        max_chars: int,
        session_id: Optional[str],
        conversation_id: Optional[str],
        profile: Optional[str],
        agent_id: Optional[str],
        deadline: Any,
        effective_include_flags: Optional[Mapping[str, Any]] = None,
        rerank_cfg: Any = None,
        rerank_top_n: Any = None,
    ) -> _BuiltContext:
        ctx = adapters.build_query_context(
            query,
            limit=limit,
            max_chars=max_chars,
            deadline=deadline,
            session_id=session_id,
            conversation_id=conversation_id,
            profile=profile,
            agent_id=agent_id,
        )
        # PART 3 (G6B Slice I): when the caller (the facade) supplied
        # the include_* flags, build the plan from THOSE flags via
        # ``build_effective_query_plan`` so the typed plan can never
        # claim a lane is enabled while the real legacy call skipped
        # it.  Otherwise fall back to the legacy default plan.
        plan: QueryPlan
        if effective_include_flags:
            try:
                # G6B effective-plan extension: forward the live
                # facade rerank_cfg / rerank_top_n into the typed
                # builder so ``plan.rerank_enabled`` mirrors the
                # legacy recall_pool's pre-execution rerank intent.
                # No execution semantics are touched here — the
                # kwargs are forwarded to the legacy call separately
                # via ``_build_legacy_kwargs`` further below.
                plan = build_effective_query_plan(
                    ctx,
                    **dict(effective_include_flags),
                    rerank_cfg=rerank_cfg,
                    rerank_top_n=rerank_top_n,
                )
            except Exception:
                # Defensive: if the effective builder raises, fall back
                # to the default builder rather than failing the whole
                # request — the legacy path is unaffected by plan shape.
                plan = build_default_query_plan(ctx)
        else:
            plan = build_default_query_plan(ctx)
        trace = RecallTrace(query_context=ctx, query_plan=plan)
        sink = LegacySink(trace)
        return _BuiltContext(query_context=ctx, query_plan=plan, trace=trace, sink=sink)

    def recall(
        self,
        query: str,
        *,
        limit: int = 8,
        max_chars: int = 10_000,
        config: Any = None,
        card_index: Any = None,
        pg: Any = None,
        q_emb: Any = None,
        pg_was_connected: bool = False,
        core: Any = None,
        sqlite_store: Any = None,
        deadline: Any = None,
        session_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        profile: Optional[str] = None,
        agent_id: Optional[str] = None,
        rerank_top_n: Optional[int] = None,
        rerank_cfg: Any = None,
        # PART 3 (G6B Slice I): when the facade (prefetch) supplies
        # these explicitly, the engine builds the typed QueryPlan
        # from THOSE flags via ``build_effective_query_plan`` instead
        # of the all-enabled default.  All seven flags default to
        # ``None`` so existing callers that never set them continue
        # to use the default builder.  When the facade supplies at
        # least one non-None value, the plan mirrors the real legacy
        # execution shape — disabled lanes carry a truthful reason.
        include_keyword: Optional[bool] = None,
        include_card_vector: Optional[bool] = None,
        include_message_vector: Optional[bool] = None,
        include_effective: Optional[bool] = None,
        include_topic: Optional[bool] = None,
        include_yin: Optional[bool] = None,
        include_notes: Optional[bool] = None,
        # PART 4 (G6B Slice I): when the caller supplies a list here,
        # the engine appends the typed ``RecallTrace`` it built to
        # that list.  This is the internal channel the facade uses
        # to obtain the trace WITHOUT making the trace kwarg
        # contractually required, and WITHOUT forcing the caller to
        # pass ``trace=``.  If the caller supplied an existing
        # ``trace=`` (a sink), use THAT as the trace sink and leave
        # ``trace_out`` untouched.
        trace_out: Optional[list[Any]] = None,
        **passthrough: Any,
    ) -> RecallV2Result:
        """Run one orchestrated recall request.

        See module docstring for behaviour / invariants.  The function
        signature mirrors the legacy ``recall_pool`` keyword surface so
        callers can switch with minimal changes; all unknown kwargs are
        passed through verbatim on the success path.
        """
        # PART 3: determine whether the caller (facade) supplied the
        # include_* flags.  We treat "supplied" as: any one of them is
        # non-None.  When true, we build the plan from THOSE flags.
        effective_include_flags: Optional[dict[str, Any]] = None
        include_flags_seen = {
            "include_keyword": include_keyword,
            "include_card_vector": include_card_vector,
            "include_message_vector": include_message_vector,
            "include_effective": include_effective,
            "include_topic": include_topic,
            "include_yin": include_yin,
            "include_notes": include_notes,
        }
        if any(v is not None for v in include_flags_seen.values()):
            # Coerce any unset include_* to the legacy default so the
            # builder receives a fully-specified kwarg set.  The
            # legacy defaults mirror those declared on
            # ``v3core.recall_pool.recall_pool``.
            effective_include_flags = {
                "include_keyword": (
                    bool(include_keyword) if include_keyword is not None else True
                ),
                "include_card_vector": (
                    bool(include_card_vector) if include_card_vector is not None else False
                ),
                "include_message_vector": (
                    bool(include_message_vector) if include_message_vector is not None else False
                ),
                "include_effective": (
                    bool(include_effective) if include_effective is not None else False
                ),
                "include_topic": (
                    bool(include_topic) if include_topic is not None else True
                ),
                "include_yin": (
                    bool(include_yin) if include_yin is not None else True
                ),
                "include_notes": (
                    bool(include_notes) if include_notes is not None else True
                ),
            }
        # ---- Step 1: build context + plan + trace + sink (no retrieval yet)
        pre_exc: Optional[BaseException] = None
        built: Optional[_BuiltContext] = None
        if self._trace_enabled:
            try:
                built = self._build_pre_execution(
                    query=query,
                    limit=limit,
                    max_chars=max_chars,
                    session_id=session_id,
                    conversation_id=conversation_id,
                    profile=profile,
                    agent_id=agent_id,
                    deadline=deadline,
                    effective_include_flags=effective_include_flags,
                    rerank_cfg=rerank_cfg,
                    rerank_top_n=rerank_top_n,
                )
            except BaseException as e:  # noqa: BLE001 — pre-execution safety net
                pre_exc = e

        # PART 4 (G6B Slice I): if pre-execution built a trace and the
        # caller supplied ``trace_out`` AND did NOT supply an existing
        # ``trace=`` sink, publish the trace so the facade can attach
        # injection probes to the SAME instance.  When the caller
        # supplied ``trace=`` we leave ``trace_out`` untouched (the
        # caller already has its own sink).  Only happens on the
        # success path; the fallback and trace-disabled paths below
        # do NOT populate ``trace_out``.
        if (
            self._trace_enabled
            and pre_exc is None
            and built is not None
            and trace_out is not None
            and "trace" not in (passthrough or {})
            and built.trace is not None
        ):
            try:
                trace_out.append(built.trace)
            except Exception:
                # Defensive: a broken holder list cannot raise into the
                # caller; the engine still returns the trace on the
                # result object.
                pass

        if self._trace_enabled and pre_exc is not None:
            # Pre-execution fallback: call the legacy function ONCE with no
            # trace kwarg.  This is the ONLY fallback path.
            legacy_kwargs = self._build_legacy_kwargs(
                query=query,
                card_index=card_index,
                pg=pg,
                q_emb=q_emb,
                limit=limit,
                pg_was_connected=pg_was_connected,
                config=config,
                sqlite_store=sqlite_store,
                core=core,
                rerank_top_n=rerank_top_n,
                rerank_cfg=rerank_cfg,
                max_chars=max_chars,
                deadline=deadline,
                include_trace=False,
                passthrough=self._include_flags_passthrough(
                    effective_include_flags, passthrough
                ),
            )
            hits, pg_fail = self._call_legacy_no_trace(**legacy_kwargs)
            return RecallV2Result(
                hits=list(hits or []),
                trace=None,
                pg_fail=bool(pg_fail),
                query_context=None,
                query_plan=None,
                candidates=[],
                deadline_enforced=_deadline_enforced(deadline),
                fallback_used=True,
                fallback_reason=f"pre_execution: {type(pre_exc).__name__}: {pre_exc}",
                trace_attached=False,
            )

        # ---- Step 1b: trace-disabled path — skip pre-execution entirely.
        # Build a minimal result with no trace object and no
        # candidates conversion; just forward the legacy hits.
        if not self._trace_enabled:
            base_kwargs = self._build_legacy_kwargs(
                query=query,
                card_index=card_index,
                pg=pg,
                q_emb=q_emb,
                limit=limit,
                pg_was_connected=pg_was_connected,
                config=config,
                sqlite_store=sqlite_store,
                core=core,
                rerank_top_n=rerank_top_n,
                rerank_cfg=rerank_cfg,
                max_chars=max_chars,
                deadline=deadline,
                include_trace=False,
                passthrough=self._include_flags_passthrough(
                    effective_include_flags, passthrough
                ),
            )
            hits, pg_fail = self._recall_fn(query, **base_kwargs)
            return RecallV2Result(
                hits=list(hits or []),
                trace=None,
                pg_fail=bool(pg_fail),
                query_context=None,
                query_plan=None,
                candidates=[],
                deadline_enforced=_deadline_enforced(deadline),
                fallback_used=False,
                fallback_reason="",
                trace_attached=False,
            )

        # ---- Step 2: invoke the legacy function EXACTLY ONCE ---------
        base_kwargs = self._build_legacy_kwargs(
            query=query,
            card_index=card_index,
            pg=pg,
            q_emb=q_emb,
            limit=limit,
            pg_was_connected=pg_was_connected,
            config=config,
            sqlite_store=sqlite_store,
            core=core,
            rerank_top_n=rerank_top_n,
            rerank_cfg=rerank_cfg,
            max_chars=max_chars,
            deadline=deadline,
            include_trace=False,
            passthrough=self._include_flags_passthrough(
                effective_include_flags, passthrough
            ),
        )

        # Decide up front whether to attach the trace kwarg.
        accepts_trace = self._callable_accepts_trace() if self._trace_enabled else False
        # PART 4 (G6B Slice I): when the caller supplied their own
        # ``trace=`` sink via passthrough, the engine MUST respect it
        # and forward THAT sink to recall_pool (NOT the engine's
        # ``built.sink``).  This preserves the existing trace= contract
        # for callers that already manage a typed trace — the facade
        # uses ``trace_out`` instead so we only hit this branch when
        # an external caller (e.g. an integration test) explicitly
        # passes ``trace=``.  The engine's own trace remains in
        # ``built.trace`` and is exposed on the result so the engine's
        # typed contract is still satisfied.
        caller_supplied_trace = bool(
            self._trace_enabled and "trace" in (passthrough or {})
        )
        # Default: trace was attached iff the signature accepted it AND
        # the pre-execution validator did not have to drop it.
        trace_attached = bool(self._trace_enabled and accepts_trace is not False)
        if self._trace_enabled and accepts_trace is not False:
            call_kwargs = dict(base_kwargs)
            if caller_supplied_trace:
                # Forward the caller's sink verbatim.  ``passthrough``
                # is already merged into ``base_kwargs`` by
                # ``_build_legacy_kwargs`` (via ``kwargs.update(passthrough)``),
                # so the caller's ``trace=`` is already in
                # ``call_kwargs`` — do NOT override it.
                pass
            else:
                call_kwargs["trace"] = built.sink
        else:
            # Callable doesn't accept 'trace' (or trace disabled).  The
            # engine still uses the pre-execution validator below to drop
            # ``max_chars`` from the forwarded payload when the callable
            # does not declare it.
            call_kwargs = dict(base_kwargs)
        # G6B Slice G (Part 1 / HIGH A) — pre-execution validation.
        # Drop any OPTIONAL keyword (``trace`` and/or ``max_chars``) the
        # callable's signature does not accept, BEFORE invoking.  When
        # the signature is uninspectable we conservatively drop BOTH
        # optional keywords (correctness beats tracing) and report
        # ``trace_attached=False`` accordingly.  After this call the
        # payload is guaranteed to be accepted by the callable, so the
        # invocation happens exactly once and any exception propagates
        # unchanged — the engine never retries based on a message.
        validated_kwargs, sig_inspectable = self._validate_kwargs_for_callable(call_kwargs)
        if not sig_inspectable:
            # Uninspectable callable: conservative path omits trace.
            trace_attached = False
        if "trace" not in validated_kwargs:
            # Validator (or signature) dropped the trace kwarg.
            trace_attached = False
        hits, pg_fail = self._invoke_once(query, validated_kwargs)

        return self._finalize(
            built,
            hits=hits,
            pg_fail=pg_fail,
            trace_attached=trace_attached,
            fallback_used=False,
            fallback_reason="",
            deadline_for_enforce=deadline,
        )

    # ----- helpers ----------------------------------------------------

    @staticmethod
    def _include_flags_passthrough(
        effective_include_flags: Optional[Mapping[str, Any]],
        passthrough: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge the facade's effective include_* flags into passthrough.

        PART 1 / PART 3 (G6B Slice I): the facade (``prefetch``) passes
        ``include_keyword`` / ``include_card_vector`` /
        ``include_message_vector`` etc. as NAMED parameters to
        :meth:`recall`, so they do not flow through ``**passthrough``
        automatically.  The legacy ``recall_pool`` accepts those
        exact keyword names, so we copy them into the passthrough dict
        that ``_build_legacy_kwargs`` forwards verbatim.  When the
        caller did not supply the flags (``effective_include_flags is
        None``) we leave passthrough untouched.
        """
        if not effective_include_flags:
            return passthrough
        merged = dict(passthrough or {})
        for k, v in effective_include_flags.items():
            # Don't overwrite an explicit passthrough entry; the
            # passthrough dict carries the caller's verbatim values.
            merged.setdefault(k, v)
        return merged

    def _build_legacy_kwargs(
        self,
        *,
        query: str,
        card_index: Any,
        pg: Any,
        q_emb: Any,
        limit: int,
        pg_was_connected: bool,
        config: Any,
        sqlite_store: Any,
        core: Any,
        rerank_top_n: Any,
        rerank_cfg: Any,
        max_chars: Any,
        deadline: Any,
        include_trace: bool,
        passthrough: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the kwargs dict for the legacy callable, adaptively.

        For each kwarg that the legacy callable's signature does NOT
        accept (and that has no ``**kwargs`` rescue), the kwarg is
        silently dropped from the forwarded payload.  ``max_chars`` is
        therefore NEVER forwarded when the callable does not declare it,
        but the engine still uses ``max_chars`` for ``QueryContext`` /
        ``QueryPlan``.

        Behaviour rules (B1 + F):

        * Use ``inspect.signature`` to decide per-keyword acceptance.
        * ``VAR_KEYWORD`` (``**kwargs``) ⇒ accept everything (lenient
          rule) for the legacy ``trace`` kwarg and every other
          forwarded keyword.
        * ``max_chars`` is the ONE keyword that follows a STRICTER
          rule: it is forwarded only when the callable's signature
          declares ``max_chars`` explicitly.  A ``**kwargs`` rescue
          does NOT count as accepting ``max_chars`` — this prevents a
          thin ``def wrapper(*a, **k)`` around the real ``recall_pool``
          from being forwarded a keyword the real function does not
          accept, which would raise ``TypeError: recall_pool() got an
          unexpected keyword argument 'max_chars'``.
        * If the signature cannot be inspected at all (``None``), we
          do not inject our own ``max_chars`` (would risk a TypeError
          on C builtins); we keep whatever ``passthrough`` supplied
          (typically nothing for the real ``recall_pool``).
        """
        kwargs: dict[str, Any] = {
            "card_index": card_index,
            "pg": pg,
            "q_emb": q_emb,
            "limit": limit,
            "pg_was_connected": pg_was_connected,
            "config": config,
            "sqlite_store": sqlite_store,
            "core": core,
            "rerank_top_n": rerank_top_n,
            "rerank_cfg": rerank_cfg,
            "deadline": deadline,
        }
        # Apply passthrough overrides (e.g. include_* flags) first so the
        # adaptive filter still runs over the union.
        kwargs.update(passthrough)
        # G6B Slice F: ``max_chars`` follows a STRICTER adaptive
        # contract than every other forwarded keyword — only forward
        # when the callable's signature declares ``max_chars``
        # EXPLICITLY.  A ``**kwargs`` rescue is NOT considered
        # acceptance for ``max_chars`` (it is for ``trace`` and every
        # other keyword).  When the callable does NOT accept it,
        # ``max_chars`` is used ONLY for the typed QueryContext /
        # QueryPlan and is stripped from the forwarded payload.  When
        # the signature is uninspectable we conservatively strip
        # ``max_chars`` — the real ``recall_pool`` does not accept it
        # and we cannot prove any wrapper does either.
        accepts_max_chars = self._callable_accepts("max_chars", strict=True)
        if accepts_max_chars is False:
            kwargs.pop("max_chars", None)
        elif accepts_max_chars is True and "max_chars" not in kwargs:
            # Callable declares ``max_chars`` — supply the engine-side
            # value (which the caller passed via
            # ``engine.recall(..., max_chars=...)``) so the legacy code
            # can use it for its own budget logic when it wants to.
            kwargs["max_chars"] = max_chars
        # When accepts_max_chars is None (signature uninspectable) we
        # keep whatever ``passthrough`` supplied (typically nothing)
        # but do not inject our own ``max_chars`` (would risk a
        # TypeError on C builtins).
        # ``limit`` follows the standard lenient contract: the engine
        # supplies the caller-provided limit only when the callable's
        # signature accepts it (or has **kwargs); otherwise strip it.
        accepts_limit = self._callable_accepts("limit")
        if accepts_limit is False:
            kwargs.pop("limit", None)

        if not include_trace:
            kwargs.pop("trace", None)
            return kwargs

        # When the caller asked us to forward ``trace=``, keep it only
        # when the legacy signature accepts it (or has VAR_KEYWORD, or
        # the signature is uninspectable).
        accepts = self._callable_accepts("trace")
        if accepts is False:
            kwargs.pop("trace", None)
        return kwargs

    def _call_legacy_no_trace(self, **kwargs: Any) -> tuple[list[Any], bool]:
        """Single fallback invocation — never threaded, no retry."""
        return self._recall_fn(**kwargs)

    def _finalize(
        self,
        built: _BuiltContext,
        *,
        hits: Any,
        pg_fail: Any,
        trace_attached: bool,
        fallback_used: bool,
        fallback_reason: str,
        deadline_for_enforce: Any = None,
    ) -> RecallV2Result:
        """Convert hits → candidates, mark trace.finish, return result."""
        hit_list = list(hits or [])
        candidates: list[RecallCandidate] = []
        for h in hit_list:
            try:
                cid = str(getattr(h, "source_id", "") or "")
            except Exception:
                cid = ""
            real_lane = built.sink.recorded_lane_for(cid) if cid else None
            extras = built.sink.recorded_extra_lanes_for(cid) if cid else ()
            if not real_lane:
                real_lane = adapters.lane_for_kind(getattr(h, "kind", ""))
            try:
                cand = adapters.hit_to_candidate(
                    h,
                    lane=real_lane,
                    extra_lanes=extras,
                    capture_content=False,
                )
                candidates.append(cand)
            except Exception:
                # Defensive: never let a single bad hit kill the result.
                continue

        # Finish the trace.
        try:
            built.trace.finish()
        except Exception:
            pass

        return RecallV2Result(
            hits=hit_list,
            trace=built.trace,
            pg_fail=bool(pg_fail),
            query_context=built.query_context,
            query_plan=built.query_plan,
            candidates=candidates,
            deadline_enforced=_deadline_enforced(deadline_for_enforce),
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            trace_attached=trace_attached,
        )


__all__ = [
    "RecallV2Engine",
    "RecallV2Result",
    "LegacySink",
]
