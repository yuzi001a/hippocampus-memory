# -*- coding: utf-8 -*-
"""G6B Slice A — Recall V2 adapters.

Pure-stdlib glue between the legacy ``v3core.types.RecallHit`` /
``v3core._deadline`` surface and the new ``v3core.recall_v2`` contracts.
No I/O, no provider calls, no embedding, no DB.

The ``lane_for_kind`` mapping below is a **documented semantic mapping**
of legacy hit kinds onto the canonical five G6A lanes.  It is NOT a
claim about which lane actually found the item — real lane membership
must be recorded by the sink during retrieval and reflected back into
the candidate's ``lane`` field by the engine.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from v3core._deadline import (
    INTERNAL_PREFETCH_BUDGET_SECONDS,
    coerce_deadline,
)

from .contracts import (
    DEFAULT_INJECTION_MAX_CHARS,
    DEFAULT_QUERY_LIMIT,
    CandidateEvent,
    CandidateEventType,
    DropReason,
    DropReasonCode,
    QueryContext,
    RecallCandidate,
    ScoreRecord,
)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


# Documented semantic mapping of legacy ``RecallHit.kind`` onto canonical
# G6A lane names.  These names are the *fixed* canonical lanes declared in
# ``recall_v2.contracts.ALL_LANES``.  This mapping is used only as a FALLBACK
# when the sink has not recorded a real lane membership for a hit.  The real
# lane that produced a hit is whatever the sink observed during retrieval;
# the engine reads that back via ``LegacySink.recorded_lane`` and overrides
# this default.
_KIND_TO_LANE: dict[str, str] = {
    "qa": "qa",
    "topic": "topic",
    "card": "vector",
    "message": "vector",
    "note": "vector",
    # Real legacy kinds observed against a disposable PG.  ``active_memory``
    # is the canonical explicit-memory seam (an active-memory reader's row);
    # ``yin`` is a yin-pool (印) retrieval row.  Both map onto existing
    # canonical lanes — no new lanes, no new constants — preserving the
    # documented fallback-only semantics above.
    "active_memory": "explicit",
    "yin": "vector",
}
_UNKNOWN_LANE_FALLBACK = "vector"


# ---------------------------------------------------------------------------
# build_query_context
# ---------------------------------------------------------------------------


def _deterministic_query_id(query: str, session_id: Optional[str]) -> str:
    """Deterministic ``query_id`` for one (session_id, query) pair.

    The hash covers both inputs so different sessions or query strings
    produce different ids.  The output is ``"q-"`` + 12 hex chars of
    ``sha1``.  Stable for the same inputs; no UUIDs, no randomness.
    """
    payload = f"{(session_id or '')}|{(query or '')}".encode("utf-8")
    digest = hashlib.sha1(payload).hexdigest()
    return f"q-{digest[:12]}"


def build_query_context(
    query: str,
    *,
    limit: Optional[int] = None,
    max_chars: Optional[int] = None,
    deadline: Any = None,
    session_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    profile: Optional[str] = None,
    agent_id: Optional[str] = None,
    query_embedding: Any = None,
    metadata: Any = None,
) -> QueryContext:
    """Build an immutable :class:`QueryContext` without touching the DB.

    Behaviour:

    * ``limit`` defaults to :data:`DEFAULT_QUERY_LIMIT` (8).
    * ``max_chars`` defaults to :data:`DEFAULT_INJECTION_MAX_CHARS` (10000).
    * The deadline is normalised via
      :func:`v3core._deadline.coerce_deadline` — existing objects are
      reused as-is; we never construct a new enforcement timer.
    * ``deadline_monotonic`` is the absolute monotonic timestamp of the
      deadline (when present) or ``time.monotonic() +
      INTERNAL_PREFETCH_BUDGET_SECONDS`` as an informational value only.
    * ``budget_ms`` is derived from the deadline's remaining seconds
      (capped at 0) when a real deadline is present, otherwise equals
      ``int(INTERNAL_PREFETCH_BUDGET_SECONDS * 1000)``.
    * ``query_id`` is deterministic per (session_id, query).
    """
    bound = coerce_deadline(deadline)
    if bound is not None and bound.is_set():
        deadline_monotonic = float(bound.deadline)
        remaining = float(bound.remaining())
        if remaining < 0.0:
            remaining = 0.0
        budget_ms = int(remaining * 1000)
    else:
        deadline_monotonic = float(time.monotonic() + INTERNAL_PREFETCH_BUDGET_SECONDS)
        budget_ms = int(INTERNAL_PREFETCH_BUDGET_SECONDS * 1000)

    eff_limit = DEFAULT_QUERY_LIMIT if limit is None else int(limit)
    eff_max_chars = (
        DEFAULT_INJECTION_MAX_CHARS if max_chars is None else int(max_chars)
    )
    qid = _deterministic_query_id(query, session_id)

    return QueryContext(
        query_id=qid,
        query_text=query,
        deadline_monotonic=deadline_monotonic,
        budget_ms=budget_ms,
        limit=eff_limit,
        max_chars=eff_max_chars,
        session_id=session_id,
        conversation_id=conversation_id,
        profile=profile,
        agent_id=agent_id,
        query_embedding=query_embedding,
        metadata=metadata if metadata else {},
    )


# ---------------------------------------------------------------------------
# lane_for_kind
# ---------------------------------------------------------------------------


def lane_for_kind(kind: str) -> str:
    """Map a legacy ``RecallHit.kind`` to a canonical G6A lane name.

    Documented semantic mapping:

    * ``"qa"`` -> ``"qa"``
    * ``"topic"`` -> ``"topic"``
    * ``"card"`` / ``"message"`` / ``"note"`` -> ``"vector"``
    * unknown / empty -> ``"vector"`` (safe default)

    NOTE: this is **not** a claim about which lane actually produced the
    hit.  Real lane membership is recorded only from the sink (via the
    legacy ``lane_candidates`` hook).  The engine uses this mapping only
    as a fallback when the sink did not record a lane for a given hit.
    """
    if not kind:
        return _UNKNOWN_LANE_FALLBACK
    return _KIND_TO_LANE.get(kind, _UNKNOWN_LANE_FALLBACK)


# ---------------------------------------------------------------------------
# hit_to_candidate
# ---------------------------------------------------------------------------


def _parse_iso_timestamp(value: Any) -> Optional[float]:
    """Parse an ISO-8601 datetime string to a UTC POSIX timestamp.

    Returns ``None`` on any parse failure or for empty / non-string
    inputs.  Never raises.
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        # ``datetime.fromisoformat`` in Python 3.11 accepts trailing ``Z``.
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    try:
        if dt.tzinfo is None:
            # Treat naive timestamps as UTC — defensive, not authoritative.
            dt = dt.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def hit_to_candidate(
    hit: Any,
    *,
    lane: str,
    extra_lanes: Iterable[str] = (),
    capture_content: bool = False,
) -> RecallCandidate:
    """Convert one legacy ``RecallHit`` to a :class:`RecallCandidate`.

    Score mapping is conservative — only stages with real evidence are
    populated:

    * ``raw_score`` ← ``hit.cosine`` only when truthy.
    * ``fusion_score`` ← ``hit.rrf_score`` only when non-zero.
    * No fabricated stage values for stages we have no evidence for.

    Privacy: ``candidate.content`` is left as ``None`` unless
    ``capture_content=True``; the legacy hit body is never copied into
    metadata either.

    One provenance entry is recorded for ``lane`` plus one for each
    entry in ``extra_lanes`` via :meth:`RecallCandidate.merge_provenance`.
    """
    source_id = str(getattr(hit, "source_id", "") or "")
    source_type = str(getattr(hit, "kind", "") or "card")
    candidate_id = source_id

    # Text reference: source_id only (privacy default).
    text_reference = source_id

    # Timestamp: only if it parses cleanly as ISO datetime.
    ts = _parse_iso_timestamp(getattr(hit, "created_at", None))

    # Build the candidate WITHOUT auto-recording provenance (we will record
    # it ourselves in the exact order: primary lane first, then extras).
    candidate = RecallCandidate(
        candidate_id=candidate_id,
        source_type=source_type,
        source_id=source_id,
        lane=lane,
        content=(getattr(hit, "content", None) if capture_content else None),
        text_reference=text_reference,
        timestamp=ts,
        metadata={},  # never copy hit metadata; privacy default
    )

    # Map only the scores we actually have.
    cosine = getattr(hit, "cosine", None)
    if cosine is not None:
        try:
            c = float(cosine)
        except (TypeError, ValueError):
            c = 0.0
        if c:
            candidate.raw_score = c
            # Reflect in score_history for transparency.
            candidate.score_history.append(
                ScoreRecord("raw", c, "legacy.cosine", {})
            )

    rrf = getattr(hit, "rrf_score", None)
    if rrf is not None:
        try:
            r = float(rrf)
        except (TypeError, ValueError):
            r = 0.0
        if r != 0.0:
            candidate.fusion_score = r
            candidate.score_history.append(
                ScoreRecord("fusion", r, "legacy.rrf_score", {})
            )

    # Record provenance: primary lane + each extra_lanes entry.
    # The constructor already added one entry for ``lane``; we add extras.
    for extra in extra_lanes:
        if not extra:
            continue
        try:
            candidate.merge_provenance(extra, source_type, source_id)
        except (ValueError, TypeError):
            # Unknown lane name etc. — never raise from an adapter.
            continue

    return candidate


__all__ = [
    "build_query_context",
    "hit_to_candidate",
    "lane_for_kind",
]
