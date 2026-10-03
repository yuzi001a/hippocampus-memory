"""v3core.derived_invalidation — M03 deterministic invalidation of derived memory.

Scope (docs/M03-CORRECTION-PROPAGATION.md, docs/M03-DERIVED-LINEAGE-MAP.md):

  After an M01/M02 correction ``A -> B`` commits durably, anything the system
  DERIVED from A can keep being injected as if the user had never spoken. This
  module is the deterministic invalidation layer that closes exactly that gap:

      CORRECTION FIRST  →  INVALIDATION SECOND  →  REBUILD THIRD (not here)

  It is pure and bounded: no LLM, no embedding, no body-text matching, no
  vector similarity, no "closest" heuristics. Work is bounded by the resolved
  ``qa_pairs`` ids and the structurally linked derived rows.

SOURCE IDENTITY — TWO ENTRY POINTS, ONE IDENTITY
------------------------------------------------
An explicit memory carries a free-form ``provenance`` JSONB. M03 reads exactly
two keys from it and accepts two entry forms:

  ``source_conversation_stream_id``  a real ``public.conversation_stream.id``
                                     of a ``role='user'`` row
  ``source_qa_id``                   a real ``public.qa_pairs.id``

Accepting two entry points does **NOT** mean accepting two identities. The one
canonical internal identity is always ``qa_pairs.id``; both forms are
normalised to it before anything is matched, and the normalisation is never
allowed to enter ``invalidation_id`` (that id is computed from
``correction_id|derived_kind|derived_id`` only, so there is exactly one derived
identity per artifact).

Resolution rules (frozen contract):

  * ``conversation_stream_id``: read the row's real ``(role, host, session_id,
    event_id)``. Missing row, or ``role <> 'user'`` → ``SOURCE_NOT_MAPPED``.
    Build the two EXACT candidate strings from that identity and require
    EXACTLY ONE ``qa_pairs.source_id`` equality hit:

        ``qa_sync/<host>/<session_id>/<event_id>``     (live sync, __init__.py:4053)
        ``qa_import/<host>/<session_id>/<event_id>``   (I01 import, qa_pairing.py:97)

    0 hits → ``SOURCE_NOT_MAPPED``; >1 hit → ``SOURCE_AMBIGUOUS``; exactly 1 →
    that ``qa_pairs.id``.
  * ``qa_pairs_id``: must exist → else ``SOURCE_NOT_MAPPED``.
  * Both supplied: resolve each independently and require the SAME canonical
    ``qa_pairs.id``; a mismatch is ``SOURCE_IDENTITY_CONFLICT`` (refused — one
    side is never silently preferred).
  * Neither supplied → ``NO_SOURCE``.

Recorded, unresolved boundaries (reported truthfully, never worked around):

  * The legacy live form ``qa_sync/<session_id>/<turn>/<msg_id[:16]>`` needs
    ``q_turn``, which ``conversation_stream`` does not carry as a canonical
    truth, so such rows are ``SOURCE_NOT_MAPPED``. A turn is never inferred and
    a "nearest" QA is never picked.
  * I01 merges consecutive user messages into ONE question and only the FIRST
    user event becomes that QA's identity (qa_pairing.py:104-109), so a
    ``role='user'`` row does NOT guarantee a QA row exists. That is
    ``SOURCE_NOT_MAPPED`` — never "pick the most recent QA for this session".

FORBIDDEN RESOLUTION METHODS (must not appear in this module):
  text/body matching, timestamp proximity, "nearest QA in the session",
  approximate turn inference, embedding / cosine similarity guessing. Only
  structural equality on real ids/columns is allowed.

WHAT IS INVALIDATED
-------------------
  ``topic``          via ``topic_entries.source_qa_id`` (whole topic)
  ``observer_note``  via ``observation_notes.source_qa_range @> qa_id``
  ``yin_paragraph``  never — the table has no structural source column, so the
                     list stays empty instead of being guessed

INVALIDATION IS A SIDECAR FACT, NEVER AN EDIT
---------------------------------------------
``public.derived_memory_invalidations`` records one row per
``(correction_id, derived_kind, derived_id)``. The old artifact keeps its body
verbatim and stays readable by id / history / source trace; it only stops being
eligible for current automatic injection. No derived body is rewritten and
nothing is ever DELETEd. A deterministic ``invalidation_id`` plus the
``UNIQUE (correction_id, derived_kind, derived_id)`` constraint make propagation
idempotent: replaying it inserts ZERO new rows. A missing sidecar table is a
truthful ``MIGRATION_REQUIRED`` — never a silent fallback.

State: a correction with a live replacement leaves its dependents
``pending_rebuild`` (a rebuild toward B is pending); a withdrawal — or a chain
that now terminates with nothing current — leaves them ``stale`` with
``replacement_memory_id`` NULL — no replacement fact is ever invented. This
layer only INVALIDATES: it never runs a rebuild, so ``rebuilt`` is always 0 here
and a row reaches ``rebuilt`` only when the rebuild stage runs.

Receipt ``success`` is True for a completed propagation AND for a truthful
"nothing to do" outcome (``NO_LINEAGE``: no source, or no structurally linked
derived artifact). It is False only when the request is refused
(``SOURCE_NOT_MAPPED`` / ``SOURCE_AMBIGUOUS`` / ``SOURCE_IDENTITY_CONFLICT``) or
the propagation itself failed — in which case ``derived_propagation`` is
``pending`` and the correction stays durable.

Transaction ownership mirrors ``active_memory_store``: every sidecar write opens
and commits its OWN transaction on a leased connection (never inside an M02
correction transaction), and a ``pg`` that exposes ``open_side_connection`` but
yields no connection fails closed instead of borrowing a shared connection.
"""
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from typing import Any, Optional

from . import active_memory_store as _canonical

logger = logging.getLogger("v3core.derived_invalidation")

# ── derived kinds ─────────────────────────────────────────────────────────

DERIVED_KIND_TOPIC = "topic"
DERIVED_KIND_OBSERVER_NOTE = "observer_note"
DERIVED_KIND_YIN_PARAGRAPH = "yin_paragraph"
DERIVED_KINDS = (
    DERIVED_KIND_TOPIC,
    DERIVED_KIND_OBSERVER_NOTE,
    DERIVED_KIND_YIN_PARAGRAPH,
)

# ── invalidation states ───────────────────────────────────────────────────

STATE_STALE = "stale"
STATE_PENDING_REBUILD = "pending_rebuild"
STATE_REBUILT = "rebuilt"
STATE_UNRESOLVED = "unresolved"
STATES = (STATE_STALE, STATE_PENDING_REBUILD, STATE_REBUILT, STATE_UNRESOLVED)

# ── propagation outcome codes ─────────────────────────────────────────────

PROPAGATION_APPLIED = "PROPAGATION_APPLIED"
PROPAGATION_DEDUPLICATED = "PROPAGATION_DEDUPLICATED"
PROPAGATION_NO_LINEAGE = "PROPAGATION_NO_LINEAGE"
PROPAGATION_MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
PROPAGATION_FAILED = "PROPAGATION_FAILED"

#: ``derived_propagation`` receipt values.
DERIVED_PROPAGATION_APPLIED = "applied"
DERIVED_PROPAGATION_DEDUPLICATED = "deduplicated"
DERIVED_PROPAGATION_PENDING = "pending"
DERIVED_PROPAGATION_NONE = "none"

# ── source-resolution codes (frozen contract) ─────────────────────────────

RESOLVE_OK = "OK"
NO_SOURCE = "NO_SOURCE"
SOURCE_NOT_MAPPED = "SOURCE_NOT_MAPPED"
SOURCE_AMBIGUOUS = "SOURCE_AMBIGUOUS"
SOURCE_IDENTITY_CONFLICT = "SOURCE_IDENTITY_CONFLICT"
SOURCE_CODES = (
    RESOLVE_OK,
    NO_SOURCE,
    SOURCE_NOT_MAPPED,
    SOURCE_AMBIGUOUS,
    SOURCE_IDENTITY_CONFLICT,
)

INPUT_SOURCE_KIND_CONVERSATION_STREAM = "conversation_stream"
INPUT_SOURCE_KIND_QA_PAIRS = "qa_pairs"

PROV_KEY_CONVERSATION_STREAM_ID = "source_conversation_stream_id"
PROV_KEY_QA_ID = "source_qa_id"

# ── table / column surface ────────────────────────────────────────────────

_INVALIDATION_TABLE = "public.derived_memory_invalidations"
_RELATION_TABLE = "public.memory_relations"
_STREAM_TABLE = "public.conversation_stream"
_QA_TABLE = "public.qa_pairs"
_TOPIC_ENTRIES_TABLE = "public.topic_entries"
_OBSERVATION_TABLE = "public.observation_notes"
_TOPICS_TABLE = "public.topics"
_YIN_TABLE = "public.yin_paragraphs"

_INVALIDATION_COLUMNS = (
    "invalidation_id", "correction_id", "relation_id", "derived_kind",
    "derived_id", "source_memory_id", "replacement_memory_id", "state",
    "reason", "canonical_qa_id", "input_source_kind", "input_source_id",
    "created_at", "resolved_at", "replacement_derived_id",
)
#: The columns bound by the INSERT (the rest keep their SQL defaults / NULL).
_INVALIDATION_INSERT_COLUMNS = _INVALIDATION_COLUMNS[:12]

# The two exact canonical source_id templates. Both are built from the SAME
# real (host, session_id, event_id) tuple read out of conversation_stream.
_QA_SYNC_PREFIX = "qa_sync"
_QA_IMPORT_PREFIX = "qa_import"


class _MissingInvalidationTable(RuntimeError):
    """The sidecar table (or a required relation) is absent.

    Raised so a missing migration is never mistaken for "nothing to do".
    """


# ── public identity ───────────────────────────────────────────────────────


def invalidation_id(correction_id: str, derived_kind: str, derived_id: str) -> str:
    """``'inv_' + sha256('{correction_id}|{derived_kind}|{derived_id}')``.

    Deterministic and recomputable. Source-identity normalisation is
    deliberately NOT part of the payload: one artifact has exactly one derived
    identity regardless of which entry form the caller used.
    """
    raw = f"{correction_id}|{derived_kind}|{derived_id}"
    return "inv_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── lease / transaction plumbing (mirrors active_memory_store) ────────────


@contextmanager
def _lease(pg: Any):
    """One lease from the injected pg/pool, with canonical cleanup.

    Reuses ``active_memory_store._acquire_lease`` so the ``open_side_connection``
    fail-closed rule is identical: a store that exposes the seam but yields no
    connection raises instead of silently borrowing the shared connection.
    """
    lease = _canonical._acquire_lease(None, pg)
    try:
        yield lease.connection
    finally:
        try:
            _canonical._end_open_transaction(lease.connection)
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            logger.warning("derived_invalidation 读租约事务清理失败: %r", exc)
        lease.close()


def _is_missing_table(exc: BaseException) -> bool:
    """True only for a genuinely absent relation / table."""
    if isinstance(exc, _MissingInvalidationTable):
        return True
    text = str(exc).lower()
    if "undefinedtable" in text or "undefined_table" in text:
        return True
    if "does not exist" in text and (
        "relation" in text or "derived_memory_invalidations" in text
    ):
        return True
    return False


def _classify_db_error(exc: BaseException) -> str:
    return (
        PROPAGATION_MIGRATION_REQUIRED if _is_missing_table(exc)
        else PROPAGATION_FAILED
    )


# ── provenance input ──────────────────────────────────────────────────────


def _as_provenance_dict(provenance: Any) -> dict:
    prov = provenance
    if isinstance(prov, str):
        try:
            prov = json.loads(prov)
        except ValueError:
            prov = {}
    if not isinstance(prov, dict):
        return {}
    return prov


def _prov_raw(prov: dict, key: str) -> Any:
    """The caller's literal value, or None when the key is absent / null.

    ``None`` means ABSENT. An explicitly empty string is a present, invalid
    value (refused, never normalised into a real row).
    """
    if key not in prov:
        return None
    value = prov[key]
    return None if value is None else value


def source_input_identity(provenance: Any) -> tuple[Optional[str], Optional[str]]:
    """``(input_source_kind, input_source_id)`` — the caller's verbatim input.

    ``input_source_id`` is the literal the caller stored, never repaired or
    renormalised. When both keys are present the stream form is recorded (it is
    the richer identity); the resolution still requires both to agree.
    """
    prov = _as_provenance_dict(provenance)
    raw_stream = _prov_raw(prov, PROV_KEY_CONVERSATION_STREAM_ID)
    raw_qa = _prov_raw(prov, PROV_KEY_QA_ID)
    if raw_stream is not None:
        return INPUT_SOURCE_KIND_CONVERSATION_STREAM, str(raw_stream)
    if raw_qa is not None:
        return INPUT_SOURCE_KIND_QA_PAIRS, str(raw_qa)
    return None, None


# ── source resolution ─────────────────────────────────────────────────────


def resolve_source_qa_ids(
    pg: Any, provenance: dict
) -> tuple[list[int], str]:
    """Normalise a memory's provenance to canonical ``qa_pairs.id`` values.

    Returns ``(qa_ids, status)`` with ``status`` one of ``OK`` / ``NO_SOURCE`` /
    ``SOURCE_NOT_MAPPED`` / ``SOURCE_AMBIGUOUS`` / ``SOURCE_IDENTITY_CONFLICT``.

    Only structural equality on real ids/columns is used. Text matching,
    timestamp proximity, "nearest QA in the session", approximate turn
    inference and embedding similarity are explicitly forbidden here.
    """
    prov = _as_provenance_dict(provenance)
    raw_stream = _prov_raw(prov, PROV_KEY_CONVERSATION_STREAM_ID)
    raw_qa = _prov_raw(prov, PROV_KEY_QA_ID)

    if raw_stream is None and raw_qa is None:
        return [], NO_SOURCE

    stream_ids: Optional[list[int]] = None
    qa_ids: Optional[list[int]] = None

    with _lease(pg) as conn:
        cur = conn.cursor()
        if raw_stream is not None:
            stream_ids, code = _resolve_stream_identity(cur, raw_stream)
            if code != RESOLVE_OK:
                return [], code
        if raw_qa is not None:
            qa_ids, code = _resolve_qa_identity(cur, raw_qa)
            if code != RESOLVE_OK:
                return [], code

    if stream_ids is not None and qa_ids is not None:
        if sorted(set(stream_ids)) != sorted(set(qa_ids)):
            # Two entry points, one identity: a disagreement is refused. One
            # side is never silently preferred over the other.
            return [], SOURCE_IDENTITY_CONFLICT
        return sorted(set(stream_ids)), RESOLVE_OK

    resolved = stream_ids if stream_ids is not None else qa_ids
    return sorted(set(resolved or [])), RESOLVE_OK


def _resolve_stream_identity(cur: Any, raw_stream: Any) -> tuple[list[int], str]:
    """One ``conversation_stream.id`` → exactly one canonical ``qa_pairs.id``."""
    if not _canonical._is_source_id_literal(raw_stream):
        return [], SOURCE_NOT_MAPPED
    cur.execute(
        f"SELECT role, host, session_id, event_id FROM {_STREAM_TABLE} "
        f"WHERE id = %s",
        (int(raw_stream),),
    )
    row = cur.fetchone()
    if row is None:
        return [], SOURCE_NOT_MAPPED
    role, host, session_id, event_id = row[0], row[1], row[2], row[3]
    if role != "user":
        return [], SOURCE_NOT_MAPPED

    candidates = _exact_source_candidates(host, session_id, event_id)
    if not candidates:
        # The legacy live form needs q_turn, which conversation_stream does not
        # carry as a canonical truth. Such rows are truthfully NOT MAPPED: a
        # turn is never inferred and a "nearest" QA is never selected.
        return [], SOURCE_NOT_MAPPED

    cur.execute(
        f"SELECT id FROM {_QA_TABLE} WHERE source_id = ANY(%s)",
        (candidates,),
    )
    hits = sorted({int(r[0]) for r in cur.fetchall() if r and r[0] is not None})
    if not hits:
        # role='user' does NOT guarantee a QA row: I01 merges consecutive user
        # messages into one question and only the FIRST user event becomes that
        # QA's identity. Absent → NOT MAPPED, never "the most recent QA".
        return [], SOURCE_NOT_MAPPED
    if len(hits) > 1:
        return [], SOURCE_AMBIGUOUS
    return [hits[0]], RESOLVE_OK


def _resolve_qa_identity(cur: Any, raw_qa: Any) -> tuple[list[int], str]:
    """One ``qa_pairs.id`` literal, validated against a real row."""
    if not _canonical._is_source_id_literal(raw_qa):
        return [], SOURCE_NOT_MAPPED
    cur.execute(f"SELECT 1 FROM {_QA_TABLE} WHERE id = %s", (int(raw_qa),))
    if cur.fetchone() is None:
        return [], SOURCE_NOT_MAPPED
    return [int(raw_qa)], RESOLVE_OK


def _exact_source_candidates(
    host: Any, session_id: Any, event_id: Any
) -> list[str]:
    """The two EXACT canonical source_id templates for one real identity tuple.

    Both are equality candidates — nothing is inferred or fuzzed. An
    incomplete identity yields no candidate (→ NOT MAPPED).
    """
    h = str(host or "").strip()
    s = str(session_id or "").strip()
    e = str(event_id or "").strip()
    if not (h and s and e):
        return []
    return [
        f"{_QA_SYNC_PREFIX}/{h}/{s}/{e}",
        f"{_QA_IMPORT_PREFIX}/{h}/{s}/{e}",
    ]


# ── discovery ─────────────────────────────────────────────────────────────


def _discover(pg: Any, qa_ids: Any) -> tuple[dict, set]:
    """``(affected, covered_qa_ids)`` — structural discovery only.

    ``covered_qa_ids`` is the subset of resolved qa ids for which at least one
    derived artifact was found; it is what makes the ``unmapped`` count a
    runtime value rather than an estimate.
    """
    affected: dict[str, list[str]] = {kind: [] for kind in DERIVED_KINDS}
    covered: set[int] = set()
    ids: list[int] = []
    for value in (qa_ids or []):
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    ids = sorted(set(ids))
    if not ids:
        return affected, covered

    with _lease(pg) as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT DISTINCT topic_id FROM {_TOPIC_ENTRIES_TABLE} "
            f"WHERE source_qa_id = ANY(%s)",
            (ids,),
        )
        affected[DERIVED_KIND_TOPIC] = sorted(
            {str(r[0]) for r in cur.fetchall() if r and r[0] is not None}
        )
        cur.execute(
            f"SELECT DISTINCT source_qa_id FROM {_TOPIC_ENTRIES_TABLE} "
            f"WHERE source_qa_id = ANY(%s)",
            (ids,),
        )
        covered |= {int(r[0]) for r in cur.fetchall() if r and r[0] is not None}

        notes: set[str] = set()
        for qa in ids:
            cur.execute(
                f"SELECT id FROM {_OBSERVATION_TABLE} "
                f"WHERE source_qa_range @> %s",
                (qa,),
            )
            found = [r[0] for r in cur.fetchall() if r and r[0] is not None]
            if found:
                covered.add(qa)
                notes |= {str(x) for x in found}
        affected[DERIVED_KIND_OBSERVER_NOTE] = sorted(notes)

    # yin_paragraphs has no structural source column: it is never guessed and
    # therefore always empty here.
    return affected, covered


def discover_affected(pg: Any, qa_ids: list[int]) -> dict[str, list[str]]:
    """``{'topic': [...], 'observer_note': [...], 'yin_paragraph': []}``."""
    affected, _covered = _discover(pg, qa_ids)
    return affected


# ── sidecar writes (own transaction) ──────────────────────────────────────


def _write_invalidations(pg: Any, rows: list[dict]) -> tuple[int, int]:
    """Insert the invalidation rows in ONE own transaction.

    ``ON CONFLICT (invalidation_id) DO NOTHING`` plus the deterministic id make
    the write idempotent, so a replay inserts nothing. Returns
    ``(inserted, pending_rebuild_written)``.

    Transaction ownership mirrors ``active_memory_store.ActiveMemoryWriter``:
    when the leased connection arrives in autocommit mode it is taken out of it
    BEFORE the explicit BEGIN (a BEGIN under autocommit=True never commits and
    leaks a live transaction to the next lease), and the delivered baseline is
    restored before the lease is released.
    """
    lease = _canonical._acquire_lease(None, pg)
    conn = lease.connection
    took_txn_ownership = False
    inserted = 0
    pending = 0
    try:
        try:
            if getattr(conn, "autocommit", False) is True:
                conn.autocommit = False
                took_txn_ownership = True
            cur = conn.cursor()
            cur.execute("BEGIN")
            try:
                for row in rows:
                    cur.execute(
                        f"""
                        INSERT INTO {_INVALIDATION_TABLE}
                            ({", ".join(_INVALIDATION_INSERT_COLUMNS)})
                        VALUES
                            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                             NOW(), NULL, NULL)
                        ON CONFLICT (invalidation_id) DO NOTHING
                        RETURNING invalidation_id
                        """,
                        (
                            row["invalidation_id"], row["correction_id"],
                            row["relation_id"], row["derived_kind"],
                            row["derived_id"], row["source_memory_id"],
                            row["replacement_memory_id"], row["state"],
                            row["reason"], row["canonical_qa_id"],
                            row["input_source_kind"], row["input_source_id"],
                        ),
                    )
                    if cur.fetchone() is not None:
                        inserted += 1
                        if row["state"] == STATE_PENDING_REBUILD:
                            pending += 1
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception as rollback_exc:  # pragma: no cover
                    logger.warning(
                        "derived_invalidation 侧车 rollback 失败: %r",
                        rollback_exc,
                    )
                raise
        finally:
            if took_txn_ownership:
                try:
                    conn.autocommit = True
                except Exception as restore_exc:  # pragma: no cover
                    logger.warning(
                        "恢复 autocommit 基线失败: %r", restore_exc,
                    )
            lease.close()
    except Exception as exc:
        if _is_missing_table(exc):
            raise _MissingInvalidationTable(str(exc)) from exc
        raise
    return inserted, pending


def _existing_invalidation_ids(pg: Any, ids: list[str]) -> set:
    if not ids:
        return set()
    with _lease(pg) as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT invalidation_id FROM {_INVALIDATION_TABLE} "
            f"WHERE invalidation_id = ANY(%s)",
            (list(ids),),
        )
        return {str(r[0]) for r in cur.fetchall() if r and r[0] is not None}


def _read_correction_edge(pg: Any, correction_id: str) -> Optional[dict]:
    with _lease(pg) as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT relation_id, relation_type, from_memory_id, to_memory_id, "
            f"correction_id FROM {_RELATION_TABLE} WHERE correction_id = %s",
            (correction_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "relation_id": row[0],
        "relation_type": row[1],
        "from_memory_id": row[2],
        "to_memory_id": row[3],
        "correction_id": row[4],
    }


# ── receipt ───────────────────────────────────────────────────────────────


def _empty_counts() -> dict:
    return {
        "mapped": 0, "invalidated": 0, "unmapped": 0,
        "pending_rebuild": 0, "rebuilt": 0,
    }


def _receipt(
    *,
    status: str,
    success: bool,
    correction_id: str,
    relation_id: Optional[str] = None,
    source_memory_id: Optional[str] = None,
    replacement_memory_id: Optional[str] = None,
    resolved_qa_ids: Optional[list] = None,
    canonical_qa_id: Optional[int] = None,
    input_source_kind: Optional[str] = None,
    input_source_id: Optional[str] = None,
    resolution_status: Optional[str] = None,
    counts: Optional[dict] = None,
    derived: Optional[dict] = None,
    derived_propagation: str = DERIVED_PROPAGATION_NONE,
    dry_run: bool = False,
    error: Optional[str] = None,
) -> dict:
    return {
        "status": status,
        "success": success,
        "correction_id": correction_id,
        "relation_id": relation_id,
        "source_memory_id": source_memory_id,
        "replacement_memory_id": replacement_memory_id,
        "resolved_qa_ids": list(resolved_qa_ids or []),
        "canonical_qa_id": canonical_qa_id,
        "input_source_kind": input_source_kind,
        "input_source_id": input_source_id,
        "resolution_status": resolution_status,
        "counts": counts if counts is not None else _empty_counts(),
        "derived": derived if derived is not None else {k: [] for k in DERIVED_KINDS},
        "derived_propagation": derived_propagation,
        "dry_run": bool(dry_run),
        "error": error,
    }


# ── propagation ───────────────────────────────────────────────────────────


def propagate_correction(
    pg: Any, *, correction_id: str, dry_run: bool = False
) -> dict:
    """Deterministically invalidate the derived artifacts of one correction.

    Reads the committed M01 edge, resolves the source memory's provenance to
    canonical ``qa_pairs.id`` values, discovers structurally linked derived
    artifacts and writes the idempotent invalidation sidecar. The correction
    itself is never touched; a failure here leaves it durable and reports
    ``derived_propagation = pending``.

    The source-resolution status (``SOURCE_NOT_MAPPED`` / ``SOURCE_AMBIGUOUS`` /
    ``SOURCE_IDENTITY_CONFLICT`` / ``NO_SOURCE``) is surfaced verbatim in both
    ``status`` (for the refusal codes) and ``resolution_status`` — it is never
    swallowed into a generic failure.
    """
    cid = correction_id if isinstance(correction_id, str) else ""
    if not cid.strip():
        return _receipt(
            status=PROPAGATION_FAILED, success=False, correction_id="",
            derived_propagation=DERIVED_PROPAGATION_PENDING, dry_run=dry_run,
            error="correction_id 必须为非空字符串",
        )

    # 1) the committed correction edge
    try:
        edge = _read_correction_edge(pg, cid)
    except Exception as exc:
        return _receipt(
            status=_classify_db_error(exc), success=False, correction_id=cid,
            derived_propagation=DERIVED_PROPAGATION_PENDING, dry_run=dry_run,
            error=f"读取 {_RELATION_TABLE} 失败: {exc!r}",
        )
    if edge is None:
        return _receipt(
            status=PROPAGATION_NO_LINEAGE, success=True, correction_id=cid,
            derived_propagation=DERIVED_PROPAGATION_NONE, dry_run=dry_run,
            error=f"{cid} 没有对应的纠正关系；无可传播的血缘",
        )

    relation_id = edge["relation_id"]
    source_memory_id = edge["from_memory_id"]

    # 2) the source memory's provenance, through the canonical read path
    try:
        reader = _canonical.ActiveMemoryReader(pool=None, pg=pg)
        record = reader.get_by_memory_id(source_memory_id)
    except Exception as exc:
        return _receipt(
            status=_classify_db_error(exc), success=False, correction_id=cid,
            relation_id=relation_id, source_memory_id=source_memory_id,
            derived_propagation=DERIVED_PROPAGATION_PENDING, dry_run=dry_run,
            error=f"读取 {source_memory_id} 的 provenance 失败: {exc!r}",
        )
    if record is None:
        return _receipt(
            status=PROPAGATION_FAILED, success=False, correction_id=cid,
            relation_id=relation_id, source_memory_id=source_memory_id,
            derived_propagation=DERIVED_PROPAGATION_PENDING, dry_run=dry_run,
            error=f"源记忆 {source_memory_id} 不存在",
        )
    provenance = record.get("provenance") or {}
    input_kind, input_id = source_input_identity(provenance)

    # 3) resolve the source reference (status surfaced verbatim)
    try:
        qa_ids, resolution = resolve_source_qa_ids(pg, provenance)
    except Exception as exc:
        return _receipt(
            status=_classify_db_error(exc), success=False, correction_id=cid,
            relation_id=relation_id, source_memory_id=source_memory_id,
            input_source_kind=input_kind, input_source_id=input_id,
            derived_propagation=DERIVED_PROPAGATION_PENDING, dry_run=dry_run,
            error=f"解析 source reference 失败: {exc!r}",
        )

    if resolution == NO_SOURCE:
        return _receipt(
            status=PROPAGATION_NO_LINEAGE, success=True, correction_id=cid,
            relation_id=relation_id, source_memory_id=source_memory_id,
            input_source_kind=input_kind, input_source_id=input_id,
            resolution_status=resolution,
            derived_propagation=DERIVED_PROPAGATION_NONE, dry_run=dry_run,
            error=None,
        )
    if resolution != RESOLVE_OK:
        return _receipt(
            status=resolution, success=False, correction_id=cid,
            relation_id=relation_id, source_memory_id=source_memory_id,
            input_source_kind=input_kind, input_source_id=input_id,
            resolution_status=resolution,
            derived_propagation=DERIVED_PROPAGATION_PENDING, dry_run=dry_run,
            error=f"source reference 解析失败: {resolution}",
        )

    canonical_qa_id = qa_ids[0] if len(qa_ids) == 1 else None

    # 4) structural discovery
    try:
        affected, covered = _discover(pg, qa_ids)
    except Exception as exc:
        return _receipt(
            status=_classify_db_error(exc), success=False, correction_id=cid,
            relation_id=relation_id, source_memory_id=source_memory_id,
            resolved_qa_ids=qa_ids, canonical_qa_id=canonical_qa_id,
            input_source_kind=input_kind, input_source_id=input_id,
            resolution_status=resolution,
            derived_propagation=DERIVED_PROPAGATION_PENDING, dry_run=dry_run,
            error=f"发现受影响派生失败: {exc!r}",
        )

    # 5) the CURRENT successor, resolved through the canonical chain walk
    #    (ActiveMemoryReader.read_version → read_chain). A→B→C resolves to C.
    try:
        version = reader.read_version(source_memory_id, mode="current")
    except Exception as exc:
        return _receipt(
            status=_classify_db_error(exc), success=False, correction_id=cid,
            relation_id=relation_id, source_memory_id=source_memory_id,
            resolved_qa_ids=qa_ids, canonical_qa_id=canonical_qa_id,
            input_source_kind=input_kind, input_source_id=input_id,
            resolution_status=resolution,
            derived_propagation=DERIVED_PROPAGATION_PENDING, dry_run=dry_run,
            error=f"读取 {source_memory_id} 的当前版本失败: {exc!r}",
        )
    if not version.get("success"):
        return _receipt(
            status=PROPAGATION_FAILED, success=False, correction_id=cid,
            relation_id=relation_id, source_memory_id=source_memory_id,
            resolved_qa_ids=qa_ids, canonical_qa_id=canonical_qa_id,
            input_source_kind=input_kind, input_source_id=input_id,
            resolution_status=resolution,
            derived_propagation=DERIVED_PROPAGATION_PENDING, dry_run=dry_run,
            error=f"读取 {source_memory_id} 的当前版本失败: {version.get('error')}",
        )
    replacement_memory_id = version.get("resolved_memory_id")

    # A live replacement means "rebuild toward B is still pending"; nothing
    # current (a withdrawal, or a chain that now terminates withdrawn) means
    # the dependents are simply stale — no replacement fact is invented.
    state = (
        STATE_PENDING_REBUILD if replacement_memory_id is not None
        else STATE_STALE
    )
    rows = _build_invalidation_rows(
        correction_id=cid,
        relation_id=relation_id,
        source_memory_id=source_memory_id,
        replacement_memory_id=replacement_memory_id,
        state=state,
        reason=edge.get("relation_type"),
        canonical_qa_id=canonical_qa_id,
        input_source_kind=input_kind,
        input_source_id=input_id,
        affected=affected,
    )
    unmapped = len([q for q in qa_ids if q not in covered])

    if not rows:
        counts = _empty_counts()
        counts["unmapped"] = unmapped
        return _receipt(
            status=PROPAGATION_NO_LINEAGE, success=True, correction_id=cid,
            relation_id=relation_id, source_memory_id=source_memory_id,
            replacement_memory_id=replacement_memory_id,
            resolved_qa_ids=qa_ids, canonical_qa_id=canonical_qa_id,
            input_source_kind=input_kind, input_source_id=input_id,
            resolution_status=resolution, counts=counts, derived=affected,
            derived_propagation=DERIVED_PROPAGATION_NONE, dry_run=dry_run,
            error=None,
        )

    if dry_run:
        try:
            existing = _existing_invalidation_ids(
                pg, [r["invalidation_id"] for r in rows]
            )
        except Exception as exc:
            return _receipt(
                status=_classify_db_error(exc), success=False, correction_id=cid,
                relation_id=relation_id, source_memory_id=source_memory_id,
                replacement_memory_id=replacement_memory_id,
                resolved_qa_ids=qa_ids, canonical_qa_id=canonical_qa_id,
                input_source_kind=input_kind, input_source_id=input_id,
                resolution_status=resolution,
                derived_propagation=DERIVED_PROPAGATION_PENDING, dry_run=True,
                error=f"dry-run 读取侧车失败: {exc!r}",
            )
        would = len(rows) - len(existing)
        counts = {
            "mapped": len(rows), "invalidated": 0, "unmapped": unmapped,
            "pending_rebuild": 0, "rebuilt": 0, "would_invalidate": would,
        }
        return _receipt(
            status=(PROPAGATION_APPLIED if would else PROPAGATION_DEDUPLICATED),
            success=True, correction_id=cid, relation_id=relation_id,
            source_memory_id=source_memory_id,
            replacement_memory_id=replacement_memory_id,
            resolved_qa_ids=qa_ids, canonical_qa_id=canonical_qa_id,
            input_source_kind=input_kind, input_source_id=input_id,
            resolution_status=resolution, counts=counts, derived=affected,
            derived_propagation=DERIVED_PROPAGATION_PENDING, dry_run=True,
            error=None,
        )

    # 6) idempotent sidecar write in its OWN transaction
    try:
        inserted, pending_written = _write_invalidations(pg, rows)
    except Exception as exc:
        return _receipt(
            status=_classify_db_error(exc), success=False, correction_id=cid,
            relation_id=relation_id, source_memory_id=source_memory_id,
            replacement_memory_id=replacement_memory_id,
            resolved_qa_ids=qa_ids, canonical_qa_id=canonical_qa_id,
            input_source_kind=input_kind, input_source_id=input_id,
            resolution_status=resolution,
            derived_propagation=DERIVED_PROPAGATION_PENDING, dry_run=False,
            error=f"写入 {_INVALIDATION_TABLE} 失败: {exc!r}",
        )

    counts = {
        "mapped": len(rows), "invalidated": inserted, "unmapped": unmapped,
        "pending_rebuild": pending_written, "rebuilt": 0,
    }
    applied = inserted > 0
    return _receipt(
        status=PROPAGATION_APPLIED if applied else PROPAGATION_DEDUPLICATED,
        success=True, correction_id=cid, relation_id=relation_id,
        source_memory_id=source_memory_id,
        replacement_memory_id=replacement_memory_id,
        resolved_qa_ids=qa_ids, canonical_qa_id=canonical_qa_id,
        input_source_kind=input_kind, input_source_id=input_id,
        resolution_status=resolution, counts=counts, derived=affected,
        derived_propagation=(
            DERIVED_PROPAGATION_APPLIED if applied
            else DERIVED_PROPAGATION_DEDUPLICATED
        ),
        dry_run=False, error=None,
    )


def _build_invalidation_rows(
    *,
    correction_id: str,
    relation_id: Optional[str],
    source_memory_id: str,
    replacement_memory_id: Optional[str],
    state: str,
    reason: Optional[str],
    canonical_qa_id: Optional[int],
    input_source_kind: Optional[str],
    input_source_id: Optional[str],
    affected: dict,
) -> list[dict]:
    rows: list[dict] = []
    for kind in DERIVED_KINDS:
        for derived_id in affected.get(kind) or []:
            rows.append({
                "invalidation_id": invalidation_id(
                    correction_id, kind, str(derived_id)
                ),
                "correction_id": correction_id,
                "relation_id": relation_id,
                "derived_kind": kind,
                "derived_id": str(derived_id),
                "source_memory_id": source_memory_id,
                "replacement_memory_id": replacement_memory_id,
                "state": state,
                "reason": reason,
                "canonical_qa_id": canonical_qa_id,
                "input_source_kind": input_source_kind,
                "input_source_id": input_source_id,
            })
    rows.sort(key=lambda r: (r["derived_kind"], r["derived_id"]))
    return rows


# ── reads ─────────────────────────────────────────────────────────────────


def list_invalidations(
    pg: Any,
    *,
    correction_id: Optional[str] = None,
    derived_kind: Optional[str] = None,
) -> list[dict]:
    """Debug / history read of the invalidation sidecar.

    Each row exposes the derived id, its best-effort ``content`` (read from the
    artifact's own table — the artifact is NOT modified), ``state``,
    ``invalidated_by`` (the correction), ``source_memory`` and ``replacement``.
    """
    clauses: list[str] = []
    params: list = []
    if correction_id is not None:
        clauses.append("correction_id = %s")
        params.append(correction_id)
    if derived_kind is not None:
        clauses.append("derived_kind = %s")
        params.append(derived_kind)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        f"SELECT {', '.join(_INVALIDATION_COLUMNS)} "
        f"FROM {_INVALIDATION_TABLE}{where} "
        f"ORDER BY derived_kind, derived_id"
    )
    with _lease(pg) as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    out = [_invalidation_row_to_dict(r) for r in rows]
    _attach_content(pg, out)
    return out


def _invalidation_row_to_dict(row: Any) -> dict:
    if isinstance(row, dict):
        raw = dict(row)
    else:
        raw = dict(zip(_INVALIDATION_COLUMNS, row))
    return {
        "invalidation_id": raw.get("invalidation_id"),
        "correction_id": raw.get("correction_id"),
        "relation_id": raw.get("relation_id"),
        "derived_kind": raw.get("derived_kind"),
        "derived_id": raw.get("derived_id"),
        "content": None,
        "state": raw.get("state"),
        "invalidated_by": raw.get("correction_id"),
        "source_memory": raw.get("source_memory_id"),
        "replacement": raw.get("replacement_memory_id"),
        "reason": raw.get("reason"),
        "canonical_qa_id": raw.get("canonical_qa_id"),
        "input_source_kind": raw.get("input_source_kind"),
        "input_source_id": raw.get("input_source_id"),
        "created_at": _iso(raw.get("created_at")),
        "resolved_at": _iso(raw.get("resolved_at")),
        "replacement_derived_id": raw.get("replacement_derived_id"),
    }


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _attach_content(pg: Any, rows: list[dict]) -> None:
    """Best-effort body read for debug/history output. Never a mutation."""
    wanted: dict[str, set] = {}
    for row in rows:
        kind = row.get("derived_kind")
        if kind in DERIVED_KINDS and row.get("derived_id") is not None:
            wanted.setdefault(kind, set()).add(str(row["derived_id"]))
    if not wanted:
        return
    mapping: dict[tuple, Any] = {}
    try:
        with _lease(pg) as conn:
            cur = conn.cursor()
            _content_for(cur, DERIVED_KIND_TOPIC, wanted.get(DERIVED_KIND_TOPIC),
                         f"SELECT topic_id, title FROM {_TOPICS_TABLE} "
                         f"WHERE topic_id = ANY(%s)", mapping)
            _content_for(cur, DERIVED_KIND_OBSERVER_NOTE,
                         wanted.get(DERIVED_KIND_OBSERVER_NOTE),
                         f"SELECT id, content FROM {_OBSERVATION_TABLE} "
                         f"WHERE id = ANY(%s)", mapping)
            _content_for(cur, DERIVED_KIND_YIN_PARAGRAPH,
                         wanted.get(DERIVED_KIND_YIN_PARAGRAPH),
                         f"SELECT id, content FROM {_YIN_TABLE} "
                         f"WHERE id = ANY(%s)", mapping)
    except Exception as exc:
        logger.warning("derived content 读取失败（非致命）: %r", exc)
        return
    for row in rows:
        row["content"] = mapping.get((row.get("derived_kind"), row.get("derived_id")))


def _content_for(cur: Any, kind: str, ids: Optional[set], sql: str,
                 mapping: dict) -> None:
    if not ids:
        return
    keys: list = []
    for value in sorted(ids):
        try:
            keys.append(int(value))
        except (TypeError, ValueError):
            keys.append(value)
    try:
        cur.execute(sql, (keys,))
        for row in cur.fetchall():
            if row and row[0] is not None:
                mapping[(kind, str(row[0]))] = row[1]
    except Exception as exc:
        logger.warning("derived content(%s) 读取失败（非致命）: %r", kind, exc)


def invalidated_ids(pg: Any, derived_kind: str) -> set:
    """The derived ids currently suppressed for ``derived_kind``.

    Only rows whose state is NOT ``rebuilt`` are returned: a rebuilt artifact
    has been replaced by a new version, so the OLD id must stop being
    suppressed. The returned set is empty on any read failure — a caller that
    must fail closed on a missing sidecar table should treat an empty result as
    "unknown", not as "nothing invalidated".
    """
    if derived_kind not in DERIVED_KINDS:
        return set()
    try:
        with _lease(pg) as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT derived_id FROM {_INVALIDATION_TABLE} "
                f"WHERE derived_kind = %s AND state <> %s",
                (derived_kind, STATE_REBUILT),
            )
            rows = cur.fetchall()
    except Exception as exc:
        logger.warning("invalidated_ids(%s) 读取失败: %r", derived_kind, exc)
        return set()
    return {str(r[0]) for r in rows if r and r[0] is not None}


__all__ = [
    "DERIVED_KIND_TOPIC",
    "DERIVED_KIND_OBSERVER_NOTE",
    "DERIVED_KIND_YIN_PARAGRAPH",
    "DERIVED_KINDS",
    "STATE_STALE",
    "STATE_PENDING_REBUILD",
    "STATE_REBUILT",
    "STATE_UNRESOLVED",
    "STATES",
    "PROPAGATION_APPLIED",
    "PROPAGATION_DEDUPLICATED",
    "PROPAGATION_NO_LINEAGE",
    "PROPAGATION_MIGRATION_REQUIRED",
    "PROPAGATION_FAILED",
    "DERIVED_PROPAGATION_APPLIED",
    "DERIVED_PROPAGATION_DEDUPLICATED",
    "DERIVED_PROPAGATION_PENDING",
    "DERIVED_PROPAGATION_NONE",
    "RESOLVE_OK",
    "NO_SOURCE",
    "SOURCE_NOT_MAPPED",
    "SOURCE_AMBIGUOUS",
    "SOURCE_IDENTITY_CONFLICT",
    "SOURCE_CODES",
    "INPUT_SOURCE_KIND_CONVERSATION_STREAM",
    "INPUT_SOURCE_KIND_QA_PAIRS",
    "PROV_KEY_CONVERSATION_STREAM_ID",
    "PROV_KEY_QA_ID",
    "invalidation_id",
    "source_input_identity",
    "resolve_source_qa_ids",
    "discover_affected",
    "propagate_correction",
    "list_invalidations",
    "invalidated_ids",
]
