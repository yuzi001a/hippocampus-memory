# -*- coding: utf-8 -*-
"""v3core.derived_suppression — M03 injection-layer staleness suppression.

Scope (docs/M03-CORRECTION-PROPAGATION.md §"Current recall must recognise
staleness", docs/M03-DERIVED-LINEAGE-MAP.md §4):

  Writing invalidation rows is not M03. The product gate is that the REAL
  injection paths stop injecting derived content that a committed correction
  ``A -> B`` has invalidated. This module is that gate. It is deliberately thin:

      * it only ever READS ``public.derived_memory_invalidations`` (through the
        canonical :func:`v3core.derived_invalidation.invalidated_ids` plus one
        bounded readability probe);
      * it never writes, never rewrites a derived body, never touches
        ``topics`` / ``observation_notes`` / ``yin_paragraphs``;
      * it only ever DROPS candidates at the injection layer. RRF, rerank and
        every non-invalidated candidate's recall behaviour are unchanged, and
        Recall V2 is not rewritten.

The four injection lanes and their ids:

  ==============  =====================================================
  lane            id used for suppression
  ==============  =====================================================
  topic           ``topic_<topic_id>`` source_id (query recall) or the
                  ``topic_id`` carried by the first-round recall dict
  observer_note   ``observation_notes.id`` (chain head; walk ``prev_id``)
  yin_paragraph   ``yin_paragraphs.id``
  ==============  =====================================================

SUPPRESSION IS PERMANENT PER SIDECAR ROW
----------------------------------------
Every row of ``public.derived_memory_invalidations`` permanently suppresses its
``derived_id``. The row's ``state`` records only the rebuild lifecycle
(``stale`` / ``pending_rebuild`` / ``rebuilt`` / ``unresolved``) and is NEVER an
admissibility gate: a ``rebuilt`` row means the replacement artifact exists, not
that the old id may re-enter injection. Task book §31 freezes ``old D1 stale`` /
``new D2 active`` / ``replacement_derived_id = D2`` → ``D2 allowed``,
``D1 suppressed``.

DEGRADED READS — FAIL OPEN, BUT VISIBLE
---------------------------------------
``invalidated_ids`` returns an EMPTY set on any read failure and documents that
as "unknown, not nothing". A caller that cannot tell "nothing invalidated" from
"could not read the sidecar" would silently treat an unreadable sidecar as a
clean bill of health. This module refuses that: an empty result is disambiguated
with one bounded readability probe on the same table, and when the sidecar truly
cannot be read the result is ``degraded=True``.

Callers must then FAIL OPEN (keep injecting — a broken sidecar must never turn a
live correction into a recall outage) and SURFACE the marker via
:func:`log_degraded` (a WARNING carrying the literal ``suppression_degraded=True``)
and, where the lane has a structured result, a ``suppression_degraded`` field.

    cannot determine  !=  invalidated

The observer chain head is the one place where the semantics are richer than a
filter: an invalidated note must NEVER be used as the chain head. The walk
follows the existing ``prev_id`` chain to the nearest non-invalidated note, and a
fully-invalidated chain yields NO candidate (the lane injects nothing) rather
than falling back to the stale note.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .derived_invalidation import (
    DERIVED_KINDS,
    DERIVED_KIND_OBSERVER_NOTE,
    DERIVED_KIND_TOPIC,
    DERIVED_KIND_YIN_PARAGRAPH,
    _INVALIDATION_TABLE,
    _lease,
    invalidated_ids,
)

logger = logging.getLogger("v3core.derived_suppression")

#: Topic candidates are keyed ``topic_<topic_id>`` everywhere in recall_pool.
TOPIC_SOURCE_PREFIX = "topic_"

#: Chain-head SELECT. ``prev_id`` is selected so the walk can follow the chain;
#: this only ADDS a column — the row filter/order is unchanged.
NOTE_HEAD_SQL = (
    "SELECT id, version, content, source_qa_range, links, prev_id "
    "FROM observation_notes "
    "WHERE version LIKE 'v%' "
    "ORDER BY id DESC LIMIT 1"
)

#: Follow one link of the existing ``prev_id`` chain.
NOTE_BY_ID_SQL = (
    "SELECT id, version, content, source_qa_range, links, prev_id "
    "FROM observation_notes WHERE id = %s"
)


@dataclass(frozen=True)
class DerivedSuppression:
    """The suppressed-id set for one derived kind, plus its readability."""

    kind: str
    ids: frozenset = frozenset()
    degraded: bool = False
    reason: str = ""

    @property
    def known(self) -> bool:
        """False when the sidecar could not be read ('unknown, not nothing')."""
        return not self.degraded

    def suppresses(self, derived_id: Any) -> bool:
        if derived_id is None:
            return False
        return str(derived_id) in self.ids


def _empty(kind: str) -> DerivedSuppression:
    return DerivedSuppression(kind, frozenset(), False, "")


def _read_ids(cur: Any, kind: str) -> tuple:
    """One sidecar SELECT through an open cursor → ``(ids, error)``.

    EVERY row of ``public.derived_memory_invalidations`` marks a derived artifact
    that a committed correction made PERMANENTLY stale, so ALL rows of ``kind``
    are returned: ``state`` is deliberately NOT a filter. ``state`` only tracks
    the rebuild lifecycle (``stale`` / ``pending_rebuild`` / ``rebuilt`` /
    ``unresolved``) and NEVER lifts suppression — a rebuild succeeding must not
    re-activate stale content.

    Authority (task book §31):

        old D1 stale
        new D2 active
        replacement_derived_id = D2

        current recall:
        D2 allowed
        D1 suppressed

    ``D1`` stays suppressed for every state; ``D2`` is the new, admissible id and
    is never in this set.

    ``error`` is ``None`` on success; a non-empty string means the read failed
    and the caller must treat the result as unknown, not empty.
    """
    try:
        cur.execute(
            f"SELECT derived_id FROM {_INVALIDATION_TABLE} "
            f"WHERE derived_kind = %s",
            (kind,),
        )
        rows = cur.fetchall()
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]
    return frozenset(
        str(r[0]) for r in rows if r and r[0] is not None
    ), None


def read_suppression_cursor(cur: Any, kind: str) -> DerivedSuppression:
    """Read suppression through an ALREADY-OPEN cursor (single read).

    Used where the lane holds a raw connection cursor (the direct-psycopg2
    observer branch) rather than a store/pool with ``lease()``. Exceptions are
    visible here, so a failure is reported as ``degraded`` and never as "empty".
    """
    if kind not in DERIVED_KINDS:
        return _empty(kind)
    ids, err = _read_ids(cur, kind)
    if err is not None:
        return DerivedSuppression(kind, frozenset(), True, err)
    return DerivedSuppression(kind, ids or frozenset(), False, "")


def read_suppression(pg: Any, kind: str) -> DerivedSuppression:
    """Read suppression ids from a store/pool exposing ``lease()``.

    Primary reader is the canonical ``derived_invalidation.invalidated_ids``.
    Because that function returns an EMPTY set on failure, an empty result is
    disambiguated with one bounded readability probe on the same sidecar table;
    if that probe also fails the result is ``degraded=True``.

    ``pg is None`` means no PG store is in play (offline / SQLite fallback), where
    M02 corrections and the M03 sidecar do not exist — that is reported as
    not-degraded (nothing to determine), not as a failure.
    """
    if kind not in DERIVED_KINDS:
        return _empty(kind)
    if pg is None:
        return _empty(kind)

    try:
        ids = invalidated_ids(pg, kind)
    except Exception as exc:  # pragma: no cover - invalidated_ids swallows internally
        return DerivedSuppression(kind, frozenset(), True, f"invalidated_ids: {exc}"[:200])
    if ids:
        return DerivedSuppression(kind, frozenset(str(x) for x in ids), False, "")

    # Empty: either genuinely nothing invalidated, or the read failed.
    try:
        with _lease(pg) as conn:
            probe = read_suppression_cursor(conn.cursor(), kind)
    except Exception as exc:
        return DerivedSuppression(kind, frozenset(), True, f"{type(exc).__name__}: {exc}"[:200])
    if probe.degraded:
        return probe
    return DerivedSuppression(kind, probe.ids, False, "")


def log_degraded(suppression: Optional[DerivedSuppression], where: str,
                 log: Optional[logging.Logger] = None) -> bool:
    """Emit the visible degradation marker. Returns True when degraded.

    The message carries the literal ``suppression_degraded=True`` so the marker
    is greppable in logs and assertable in tests. It is emitted exactly once per
    read, at WARNING, and states plainly that staleness CANNOT be determined.
    """
    if suppression is None or not suppression.degraded:
        return False
    (log or logger).warning(
        "M03 suppression_degraded=True (%s kind=%s): %s — derived staleness "
        "CANNOT be determined (cannot determine != invalidated); failing OPEN, "
        "recall continues unchanged",
        where, suppression.kind, suppression.reason or "sidecar read failed",
    )
    return True


# ─────────────────────────────────────────────────────────────────────────────
# lane filters — pure, additive, drop-only
# ─────────────────────────────────────────────────────────────────────────────
def topic_id_from_source_id(source_id: Any) -> Optional[str]:
    """``'topic_<topic_id>'`` → ``'<topic_id>'``; anything else → ``None``."""
    if isinstance(source_id, str) and source_id.startswith(TOPIC_SOURCE_PREFIX):
        return source_id[len(TOPIC_SOURCE_PREFIX):]
    return None


def suppress_topic_hits(hits: Any, suppression: Optional[DerivedSuppression]) -> list:
    """Drop invalidated ``topic_<topic_id>`` entries from a ``hits`` mapping.

    Mutates ``hits`` in place (the convergence point owns the dict) and returns
    the removed source_ids. Non-topic hits and non-invalidated topics are left
    exactly as they were.
    """
    if not hits or suppression is None or not suppression.ids:
        return []
    removed: list = []
    for sid in list(hits.keys()):
        tid = topic_id_from_source_id(sid)
        if tid is not None and tid in suppression.ids:
            del hits[sid]
            removed.append(sid)
    return removed


def suppress_topic_candidates(candidates: Any,
                              suppression: Optional[DerivedSuppression]) -> list:
    """Return ``candidates`` minus the invalidated ``topic_id`` entries."""
    if not candidates:
        return list(candidates or [])
    if suppression is None or not suppression.ids:
        return list(candidates)
    out: list = []
    for cand in candidates:
        tid = cand.get("topic_id") if isinstance(cand, dict) else None
        if tid is not None and str(tid) in suppression.ids:
            continue
        out.append(cand)
    return out


def suppress_yin_hits(hits: Any, suppression: Optional[DerivedSuppression]) -> list:
    """Return ``hits`` minus the invalidated yin paragraph ``id`` entries.

    Paragraph-level suppression requires ``search_effective`` to expose the
    paragraph ``id``; hits without one are always kept (never guessed).
    """
    if not hits:
        return list(hits or [])
    if suppression is None or not suppression.ids:
        return list(hits)
    out: list = []
    for hit in hits:
        hid = hit.get("id") if isinstance(hit, dict) else None
        if hid is not None and str(hid) in suppression.ids:
            continue
        out.append(hit)
    return out


def select_note_head(cur: Any, suppression: Optional[DerivedSuppression]) -> Any:
    """Chain-head selection that honours M03 invalidation.

    Returns the current note row ``(id, version, content, source_qa_range, links,
    prev_id)``, or ``None`` when there is no admissible candidate.

    An invalidated note is NEVER returned as a fallback: the walk follows the
    existing ``prev_id`` chain to the nearest non-invalidated note, and a
    fully-invalidated chain yields ``None`` (the lane injects nothing).
    """
    suppressed = suppression.ids if suppression is not None else frozenset()

    cur.execute(NOTE_HEAD_SQL)
    row = cur.fetchone()
    seen: set = set()
    while row is not None:
        nid = row[0]
        if nid is None or str(nid) not in suppressed:
            return row
        key = str(nid)
        if key in seen:  # defensive cycle guard; a cycle is never a candidate
            return None
        seen.add(key)
        prev_id = row[5] if len(row) > 5 else None
        if prev_id is None:
            return None
        cur.execute(NOTE_BY_ID_SQL, (prev_id,))
        row = cur.fetchone()
    return None


__all__ = [
    "DerivedSuppression",
    "TOPIC_SOURCE_PREFIX",
    "NOTE_HEAD_SQL",
    "NOTE_BY_ID_SQL",
    "read_suppression",
    "read_suppression_cursor",
    "log_degraded",
    "topic_id_from_source_id",
    "suppress_topic_hits",
    "suppress_topic_candidates",
    "suppress_yin_hits",
    "select_note_head",
    "DERIVED_KIND_TOPIC",
    "DERIVED_KIND_OBSERVER_NOTE",
    "DERIVED_KIND_YIN_PARAGRAPH",
]
