# -*- coding: utf-8 -*-
"""P0-C2 — autonomous / async assistant delivery derivation.

A *delivery* is a derived, rebuildable representation of one non-human
triggered assistant output group::

    host boundary (ASYNC_BOUNDARY / NEW_ROOT)
      → contiguous non-empty NORMAL_ASSISTANT rows
      → next real boundary (boundary | REAL_USER)

Design invariants (P0-C2 contract):

* ``conversation_stream`` is the authoritative source.  A delivery is derived
  from it and can be rebuilt from it at any time; raw source is never lost
  because a derivation failed.
* Identity is deterministic and content-independent::

      delivery/<quote(host)>/<quote(session_id)>/<quote(trigger_event_id)>

  where ``trigger_event_id`` is the durable source event id of the boundary
  row.  Replaying the same snapshot therefore collapses to exactly one row.
* No synthetic question is ever fabricated and no prior human QA is touched.
* Embedding is derived / optional: the delivery row is durable with or without
  an embedding provider (the source rows are durable regardless).
* The trigger boundary row is itself durable in ``conversation_stream`` with
  ``role = "host_event:<KIND>"`` so a delivery is traceable back to source and
  rebuildable after a restart.

The pure grouping / identity helpers live here; the DB read/write helpers
accept the same duck-typed pg store ``V3Core`` uses, so tests can drive them
against the in-memory fake PG without a real PostgreSQL.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import quote

from .host_events import (
    HOST_EVENT_ASYNC_BOUNDARY,
    HOST_EVENT_NEW_ROOT,
    HOST_EVENT_NORMAL_ASSISTANT,
    HOST_EVENT_REAL_USER,
    classify_host_event,
)

#: durable ``conversation_stream.role`` prefix for a host boundary source row.
HOST_EVENT_ROLE_PREFIX = "host_event"

#: recall kind for a derived delivery (mapped onto the existing ``qa`` lane).
DELIVERY_KIND = "assistant_delivery"

#: boundary kinds that open an autonomous delivery.
BOUNDARY_KINDS = (HOST_EVENT_ASYNC_BOUNDARY, HOST_EVENT_NEW_ROOT)


def host_event_role(kind: str) -> str:
    """Durable ``conversation_stream`` role for a host boundary source row."""
    return f"{HOST_EVENT_ROLE_PREFIX}:{kind}"


def kind_from_role(role: str) -> str | None:
    """Recover the host-event kind from a durable role, else ``None``."""
    r = str(role or "")
    prefix = HOST_EVENT_ROLE_PREFIX + ":"
    if r.startswith(prefix):
        return r[len(prefix):] or None
    return None


def delivery_source_id(host: str, session_id: str, trigger_event_id: str) -> str:
    """Deterministic, content-independent, unambiguous delivery identity."""
    # Escape path separators (and percent signs) inside each F2 identity
    # component so distinct (host, session, event) tuples cannot collide.
    parts = (host or "", session_id or "", trigger_event_id or "")
    return "delivery/" + "/".join(quote(str(part), safe="") for part in parts)


def classify_source_row(role: str, content: str) -> str:
    """Classify one durable ``conversation_stream`` row into a host-event kind.

    The durable role wins for host boundary rows: a metadata-only async
    completion cannot be re-derived from its content, so its kind rides the
    stored ``host_event:<KIND>`` role.  Every other row is re-classified from
    content exactly as ``sync_turn`` classified it.
    """
    kind = kind_from_role(role)
    if kind:
        return kind
    return classify_host_event(role=role, content=content or "")


@dataclass
class SourceRow:
    """One ordered ``conversation_stream`` row used for derivation."""

    event_id: str
    role: str
    content: str
    kind: str = ""
    timestamp: Any = None
    host: str = ""


@dataclass
class DeliveryGroup:
    """One derived autonomous assistant delivery."""

    trigger_event_id: str
    trigger_kind: str
    host: str = ""
    session_id: str = ""
    event_ids: list[str] = field(default_factory=list)
    segments: list[tuple[str, str]] = field(default_factory=list)
    content: str = ""
    started_at: Any = None

    @property
    def source_id(self) -> str:
        return delivery_source_id(self.host, self.session_id, self.trigger_event_id)


def group_deliveries(
    rows: Sequence[SourceRow],
    *,
    host: str = "",
    session_id: str = "",
) -> list[DeliveryGroup]:
    """Group ordered source rows into deliveries (pure, deterministic).

    Rules (P0-C2 §C2.3):

      * a boundary opens a group;
      * non-empty ``NORMAL_ASSISTANT`` rows append to the open group in order;
      * empty assistant, ``CONTROL``, ``CONTINUATION``, ``TERMINAL`` and
        ``NORMAL_TOOL`` rows are transparent — they neither end the group nor
        add content;
      * a ``REAL_USER`` row or the next boundary closes the group;
      * a group with no assistant members is discarded.
    """
    groups: list[DeliveryGroup] = []
    open_group: DeliveryGroup | None = None

    def _close() -> None:
        nonlocal open_group
        if open_group is not None and open_group.event_ids:
            groups.append(open_group)
        open_group = None

    for row in rows:
        kind = row.kind
        if kind in BOUNDARY_KINDS:
            _close()
            open_group = DeliveryGroup(
                trigger_event_id=row.event_id,
                trigger_kind=kind,
                host=host or getattr(row, "host", "") or "",
                session_id=session_id or getattr(row, "session_id", "") or "",
                event_ids=[],
                content="",
                started_at=row.timestamp,
            )
            continue
        if kind == HOST_EVENT_REAL_USER:
            _close()
            continue
        if kind == HOST_EVENT_NORMAL_ASSISTANT and open_group is not None:
            if (row.content or "").strip():
                open_group.event_ids.append(row.event_id)
                open_group.segments.append((row.event_id, row.content))
                open_group.content = (
                    f"{open_group.content}\n{row.content}"
                    if open_group.content else row.content
                )
            continue
        # Transparent rows: CONTROL / CONTINUATION / TERMINAL / NORMAL_TOOL /
        # empty assistant — do not end the group, do not add content.
    _close()
    return groups


# ---------------------------------------------------------------------------
# DB helpers (duck-typed pg store; never raise)
# ---------------------------------------------------------------------------

_DELIVERY_INSERT_SQL = (
    "INSERT INTO assistant_deliveries "
    "(source_id, host, session_id, trigger_event_id, trigger_kind, content, "
    " event_ids, started_at, created_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, NOW()) "
    "ON CONFLICT (source_id) DO NOTHING"
)

_DELIVERY_LOCK_SQL = (
    "SELECT host, session_id, trigger_event_id, trigger_kind, content, event_ids, started_at "
    "FROM assistant_deliveries WHERE source_id=%s FOR UPDATE"
)

_DELIVERY_APPEND_SQL = (
    "UPDATE assistant_deliveries SET content=%s, event_ids=%s::jsonb "
    "WHERE source_id=%s"
)

_DELIVERY_REBUILD_SQL = (
    "UPDATE assistant_deliveries SET trigger_kind=%s, content=%s, "
    "event_ids=%s::jsonb, started_at=%s WHERE source_id=%s"
)

_SOURCE_ROWS_SQL = (
    "SELECT event_id, role, content, timestamp, host FROM conversation_stream "
    "WHERE session_id=%s ORDER BY host, id ASC"
)


def _connect(pg):
    """Lease a connection from the shared pg store (lazy import avoids cycle)."""
    from . import _lease_pg_store

    return _lease_pg_store(pg)


def write_delivery(
    pg,
    group: DeliveryGroup,
    *,
    strict: bool = False,
    authoritative: bool = False,
) -> bool:
    """Persist one delivery without allowing a replay to rewrite old source text.

    Live sync uses append-only membership: equal IDs are an idempotent no-op,
    and a longer snapshot may append only source segments after the stored
    prefix. ``rebuild_deliveries`` sets ``authoritative=True`` because its body
    is re-read from canonical ``conversation_stream`` and can repair a stale
    derived representation exactly.
    """
    valid_identity = bool(
        pg is not None and group.host and group.session_id
        and group.trigger_event_id and group.event_ids
        and all(str(event_id or "").strip() for event_id in group.event_ids)
    )
    valid_segments = (
        len(group.segments) == len(group.event_ids)
        and [str(segment[0]) for segment in group.segments] == list(group.event_ids)
        and all(str(segment[1] or "").strip() for segment in group.segments)
        and group.content == "\n".join(
            str(segment[1]) for segment in group.segments
        )
    )
    if not valid_identity or not valid_segments:
        if strict:
            raise ValueError("delivery persistence requires complete F2 identity and aligned source segments")
        return False
    try:
        with _connect(pg) as conn:
            if not conn:
                if strict:
                    raise RuntimeError("delivery database lease is unavailable")
                return False
            with conn.cursor() as cur:
                cur.execute(
                    _DELIVERY_INSERT_SQL,
                    (
                        group.source_id,
                        group.host,
                        group.session_id,
                        group.trigger_event_id,
                        group.trigger_kind,
                        group.content,
                        json.dumps(list(group.event_ids), ensure_ascii=False),
                        group.started_at,
                    ),
                )
                inserted = bool(cur.rowcount)
                if inserted:
                    conn.commit()
                    return True

                cur.execute(_DELIVERY_LOCK_SQL, (group.source_id,))
                old = cur.fetchone()
                if old is None:
                    raise RuntimeError("delivery conflict row disappeared before locked read")
                old_host, old_session, old_trigger, _old_kind, old_content, old_json_ids, old_started = old
                if (str(old_host or "") != group.host
                        or str(old_session or "") != group.session_id
                        or str(old_trigger or "") != group.trigger_event_id):
                    raise RuntimeError("delivery source_id conflicts with a different F2 identity")
                old_ids = _json_list(old_json_ids)
                new_ids = list(group.event_ids)

                if authoritative:
                    cur.execute(
                        _DELIVERY_REBUILD_SQL,
                        (
                            group.trigger_kind,
                            group.content,
                            json.dumps(new_ids, ensure_ascii=False),
                            group.started_at,
                            group.source_id,
                        ),
                    )
                    changed = bool(cur.rowcount)
                elif new_ids == old_ids:
                    # The same stable event ID may reappear with shortened or
                    # rewritten display text in a later Hermes snapshot. The
                    # raw source enqueue is idempotent, so preserve its first
                    # durable body instead of overwriting it from the snapshot.
                    conn.commit()
                    return True
                elif len(new_ids) > len(old_ids) and new_ids[:len(old_ids)] == old_ids:
                    suffix_segments = group.segments[len(old_ids):]
                    suffix_ids = [str(segment[0]) for segment in suffix_segments]
                    if suffix_ids != new_ids[len(old_ids):]:
                        raise RuntimeError("delivery append suffix is not aligned with event_ids")
                    suffix_content = "\n".join(str(segment[1]) for segment in suffix_segments)
                    previous_content = str(old_content or "")
                    content = (
                        f"{previous_content}\n{suffix_content}"
                        if previous_content and suffix_content else previous_content or suffix_content
                    )
                    cur.execute(
                        _DELIVERY_APPEND_SQL,
                        (content, json.dumps(new_ids, ensure_ascii=False), group.source_id),
                    )
                    changed = bool(cur.rowcount)
                else:
                    # A stale or reordered snapshot cannot shrink/reorder the
                    # already durable delivery. Canonical rebuild can repair it.
                    conn.commit()
                    return False
            conn.commit()
        return changed
    except Exception:
        if strict:
            raise
        return False


def write_deliveries(pg, groups: Sequence[DeliveryGroup], *,
                     strict: bool = False, authoritative: bool = False) -> int:
    """Persist groups idempotently; count inserts, updates, and no-op matches."""
    written = 0
    for group in groups:
        if write_delivery(
            pg, group, strict=strict, authoritative=authoritative
        ):
            written += 1
    return written


def load_source_rows(pg, session_id: str, host: str = "", *,
                     strict: bool = False) -> list[SourceRow]:
    """Read ordered source rows; strict mode distinguishes read failure from empty."""
    rows: list[SourceRow] = []
    if pg is None or not session_id:
        if strict:
            raise ValueError("source database or session scope is unavailable")
        return rows
    try:
        with _connect(pg) as conn:
            if not conn:
                if strict:
                    raise RuntimeError("database lease is unavailable")
                return rows
            with conn.cursor() as cur:
                if host:
                    cur.execute(
                        "SELECT event_id, role, content, timestamp, host "
                        "FROM conversation_stream "
                        "WHERE session_id=%s AND host=%s ORDER BY id ASC",
                        (session_id, host),
                    )
                else:
                    cur.execute(_SOURCE_ROWS_SQL, (session_id,))
                fetched = cur.fetchall()
            for r in fetched or []:
                rows.append(SourceRow(
                    event_id=str(r[0] or ""),
                    role=str(r[1] or ""),
                    content=str(r[2] or ""),
                    kind="",
                    timestamp=r[3] if len(r) > 3 else None,
                    host=str(r[4] or host or "") if len(r) > 4 else str(host or ""),
                ))
    except Exception:
        if strict:
            raise
        return rows
    for row in rows:
        row.kind = classify_source_row(row.role, row.content)
    return rows


def derive_deliveries(pg, session_id: str, host: str = "", *,
                      strict: bool = False) -> list[DeliveryGroup]:
    """Deterministically re-derive deliveries from durable source truth."""
    rows = load_source_rows(pg, session_id, host, strict=strict)
    if strict:
        for row in rows:
            if (row.kind in BOUNDARY_KINDS or
                    (row.kind == HOST_EVENT_NORMAL_ASSISTANT and row.content.strip())):
                if not row.event_id.strip():
                    raise ValueError("source row missing F2 event_id")
    if host:
        return group_deliveries(rows, host=host, session_id=session_id)
    by_host: dict[str, list[SourceRow]] = {}
    for row in rows:
        row_host = row.host.strip()
        if row_host:
            by_host.setdefault(row_host, []).append(row)
        elif strict:
            raise ValueError("source row is missing F2 host identity")
    return [
        group
        for row_host in sorted(by_host)
        for group in group_deliveries(
            by_host[row_host], host=row_host, session_id=session_id
        )
    ]


def rebuild_deliveries(pg, session_id: str, host: str = "", *,
                       strict: bool = True) -> int:
    """Rebuild groups from canonical source; return handled group count."""
    return write_deliveries(
        pg, derive_deliveries(pg, session_id, host, strict=strict),
        strict=strict, authoritative=True,
    )


def delivery_recall_hits(pg, *, session_id: str | None = None,
                         limit: int = 50) -> list[dict]:
    """Read derived deliveries as recall candidates (kind=assistant_delivery).

    The candidate carries the delivery content plus a source trace
    (``host``/``session_id``/``trigger_event_id``/``event_ids``) that resolves
    back to ``conversation_stream`` rows via the F2 composite identity.  No
    embedding provider is required — the candidate is keyword-representable.
    """
    hits: list[dict] = []
    if pg is None:
        return hits
    try:
        with _connect(pg) as conn:
            if not conn:
                return hits
            with conn.cursor() as cur:
                if session_id:
                    cur.execute(
                        "SELECT source_id, host, session_id, trigger_event_id, "
                        "trigger_kind, content, event_ids, created_at "
                        "FROM assistant_deliveries WHERE session_id=%s "
                        "ORDER BY id ASC LIMIT %s",
                        (session_id, int(limit)),
                    )
                else:
                    cur.execute(
                        "SELECT source_id, host, session_id, trigger_event_id, "
                        "trigger_kind, content, event_ids, created_at "
                        "FROM assistant_deliveries ORDER BY id ASC LIMIT %s",
                        (int(limit),),
                    )
                for r in cur.fetchall() or []:
                    hits.append({
                        "kind": DELIVERY_KIND,
                        "source_id": str(r[0] or ""),
                        "host": str(r[1] or ""),
                        "session_id": str(r[2] or ""),
                        "trigger_event_id": str(r[3] or ""),
                        "trigger_kind": str(r[4] or ""),
                        "content": str(r[5] or ""),
                        "event_ids": _json_list(r[6]),
                        "created_at": r[7],
                    })
    except Exception:
        return hits
    return hits


def keyword_delivery_candidates(cursor, terms: Sequence[str], *,
                                limit: int = 50) -> list[dict]:
    """Fetch bounded lexical candidates from delivery content for the QA lane.

    This deliberately searches only real assistant content.  A delivery has
    no human question, and its trigger metadata is provenance rather than a
    fabricated query surface.  The caller owns transaction/error handling.
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in terms or ():
        term = str(raw or "").strip()
        folded = term.casefold()
        if len(term) <= 2 or not folded or folded in seen:
            continue
        seen.add(folded)
        normalized.append(term)
    if not normalized:
        return []
    try:
        cap = min(max(int(limit), 1), 500)
    except (TypeError, ValueError, OverflowError):
        cap = 50
    patterns = [f"%{term}%" for term in normalized]
    score_expr = " + ".join(
        "CASE WHEN content ILIKE %s THEN 1 ELSE 0 END"
        for _ in patterns
    )
    predicate = " OR ".join("content ILIKE %s" for _ in patterns)
    sql = (
        "SELECT source_id, trigger_kind, content, created_at, ("
        f"{score_expr}) AS matched_terms "
        "FROM public.assistant_deliveries "
        f"WHERE content <> '' AND ({predicate}) "
        "ORDER BY matched_terms DESC, created_at DESC NULLS LAST, id DESC "
        "LIMIT %s"
    )
    cursor.execute(sql, [*patterns, *patterns, cap])
    candidates: list[dict] = []
    for row in cursor.fetchall() or []:
        content = str(row[2] or "")
        folded = content.casefold()
        candidates.append({
            "source_id": str(row[0] or ""),
            "trigger_kind": str(row[1] or ""),
            "content": content,
            "created_at": row[3],
            "matched_terms": int(row[4] or 0),
            # Which of the normalized query terms the content actually contains.
            # The SQL above already counts them; exposing the list lets the caller
            # weigh distinctiveness (rare vs generic terms) without a second query.
            "matched_term_list": [t for t in normalized if t.casefold() in folded],
        })
    return candidates


# ---------------------------------------------------------------------------
# Historical repair — READ-ONLY deterministic dry-run
# ---------------------------------------------------------------------------

def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return []


def _load_qa_rows(pg, session_id: str, *, strict: bool = False) -> list[dict]:
    rows: list[dict] = []
    if pg is None or not session_id:
        return rows
    try:
        with _connect(pg) as conn:
            if not conn:
                if strict:
                    raise RuntimeError("database lease is unavailable")
                return rows
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source_id, question, answer, merged_event_ids "
                    "FROM qa_pairs WHERE session_id=%s ORDER BY id ASC",
                    (session_id,),
                )
                for r in cur.fetchall() or []:
                    rows.append({
                        "source_id": str(r[0] or ""),
                        "question": str(r[1] or ""),
                        "answer": str(r[2] or ""),
                        "merged_event_ids": _json_list(r[3]),
                    })
    except Exception:
        if strict:
            raise
        return rows
    return rows


def _qa_source_scope(source_id: str, session_id: str, host: str) -> str:
    """Return current/foreign/unscoped from the canonical QA source identity."""
    try:
        from .bridge_contract import LEGACY_HOST
    except Exception:  # pragma: no cover - defensive bootstrap fallback
        LEGACY_HOST = "legacy"
    sid = str(source_id or "")
    host_key = str(host or "").strip()
    if host_key and host_key != LEGACY_HOST:
        prefix = f"qa_sync/{host_key}/{session_id}/"
    else:
        prefix = f"qa_sync/{session_id}/"
    if sid.startswith(prefix):
        return "current"
    # Modern non-legacy source IDs encode host before session. Rows whose
    # encoded host differs are provably foreign and must not be compared by
    # bare event_id, which is only unique inside the F2 composite identity.
    parts = sid.split("/", 3)
    if (len(parts) == 4 and parts[0] == "qa_sync"
            and parts[2] == session_id and parts[1] != host_key):
        return "foreign"
    return "unscoped"


def plan_delivery_repair(pg, session_id: str, host: str = "") -> dict:
    """Deterministic, READ-ONLY historical repair plan.  Never writes.

    Uses only durable identity, never semantic similarity or LLM guessing:

      * ``would_create_deliveries`` — the deliveries a rebuild would create;
      * ``would_rebuild_polluted_qa`` — QA rows whose ``merged_event_ids``
        ledger proves an autonomous assistant event was folded into a human
        QA (deterministic id overlap), with a rebuild proposal;
      * ``unknown`` — rows that *look* contaminated but whose provenance is
        not provable (no event ledger) — reported, never auto-repaired.
    """
    if not session_id or not host:
        return {
            "status": "unknown",
            "reason": "session_and_host_scope_required",
            "session_id": session_id,
            "host": host,
            "would_create_deliveries": [],
            "would_rebuild_polluted_qa": [],
            "unknown": [],
        }
    try:
        deliveries = derive_deliveries(pg, session_id, host, strict=True)
        qa_rows = _load_qa_rows(pg, session_id, strict=True)
    except Exception:
        return {
            "status": "unknown",
            "reason": "source_or_qa_read_failed",
            "session_id": session_id,
            "host": host,
            "would_create_deliveries": [],
            "would_rebuild_polluted_qa": [],
            "unknown": [],
        }
    autonomous_ids = {eid for g in deliveries for eid in g.event_ids}
    would_create = [
        {
            "source_id": g.source_id,
            "trigger_event_id": g.trigger_event_id,
            "trigger_kind": g.trigger_kind,
            "event_ids": list(g.event_ids),
            "content_length": len(g.content),
        }
        for g in deliveries
    ]
    polluted: list[dict] = []
    unknown: list[dict] = []
    for qa in qa_rows:
        scope = _qa_source_scope(qa.get("source_id"), session_id, host)
        if scope == "foreign":
            continue
        members = list(qa.get("merged_event_ids") or [])
        overlap = [m for m in members if m in autonomous_ids]
        answer_overlap = _answer_contains_autonomous(qa.get("answer") or "", deliveries)
        if scope == "unscoped":
            if overlap or answer_overlap:
                unknown.append({
                    "qa_source_id": qa["source_id"],
                    "reason": (
                        "merged_event_ids or answer text overlaps an autonomous "
                        "delivery, but the QA source_id does not prove the F2 host scope"
                    ),
                })
            continue
        if overlap:
            polluted.append({
                "qa_source_id": qa["source_id"],
                "contaminated_event_ids": overlap,
                "proposal": "rebuild answer without the autonomous assistant events",
            })
        elif answer_overlap:
            unknown.append({
                "qa_source_id": qa["source_id"],
                "reason": (
                    "answer text overlaps an autonomous delivery but no "
                    "merged_event_ids ledger proves membership"
                ),
            })
    return {
        "status": "complete",
        "session_id": session_id,
        "host": host,
        "would_create_deliveries": would_create,
        "would_rebuild_polluted_qa": polluted,
        "unknown": unknown,
    }


def _answer_contains_autonomous(answer: str, deliveries: Sequence[DeliveryGroup]) -> bool:
    """Exact substring test only — never semantic similarity."""
    if not answer:
        return False
    for group in deliveries:
        for segment in (group.content or "").split("\n"):
            seg = segment.strip()
            if seg and seg in answer:
                return True
    return False


__all__ = [
    "HOST_EVENT_ROLE_PREFIX",
    "DELIVERY_KIND",
    "BOUNDARY_KINDS",
    "host_event_role",
    "kind_from_role",
    "delivery_source_id",
    "classify_source_row",
    "SourceRow",
    "DeliveryGroup",
    "group_deliveries",
    "write_delivery",
    "write_deliveries",
    "load_source_rows",
    "derive_deliveries",
    "rebuild_deliveries",
    "delivery_recall_hits",
    "keyword_delivery_candidates",
    "plan_delivery_repair",
]
