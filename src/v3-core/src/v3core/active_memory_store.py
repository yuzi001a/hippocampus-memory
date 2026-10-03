"""v3core.active_memory_store — canonical active-memory boundary.

Sole entry point for writes into ``public.explicit_memories`` (P2a clean
boundary, branch ``p2a/active-memory-clean-boundary-20260909``).

Boundary rules:
  * All SQL goes through an injected ``pool`` (``PgPool.lease(timeout,
    deadline=...)``) or ``pg`` (``PgEmbedStore.lease(timeout)``). This module
    never opens a pool or DB driver connection on its own.
  * DDL lives in ``schema/explicit_memories.sql`` and
    ``schema/memory_relations.sql``; never auto-applied. A missing
    ``memory_relations`` table is a truthful MIGRATION_REQUIRED failure at
    runtime, never a hidden fallback.

Canonical algorithm (idempotent create):
  1. derive ``memory_id`` (caller-supplied nonempty wins unchanged; else
     ``mem_<sha256(canonical_json(category, title, content, tags))>``).
  2. lease #1 → INSERT ... ON CONFLICT (memory_id) DO NOTHING RETURNING
     memory_id; commit.
  3. lease #2 (fresh) → SELECT memory_id, category, title, content, tags;
     compare. Equal → DEDUPLICATED. Same id, different payload →
     DURABLE_FAILED (existing row preserved, no UPDATE on canonical fields).
  4. AFTER both canonical leases close, resolve embed_cfg; call injected
     embedder OUTSIDE the lease. Fresh lease #3 only for the post-commit
     UPDATE of (embedding, embed_model) — never updated_at or any canonical
     field. Failures here leave canonical row durable; fresh create returns
     DERIVED_WARNING, retry returns DEDUPLICATED + warning. Embed disabled
     → no warning, no UPDATE.

M01 — explicit memory correction (``ActiveMemoryWriter.correct`` /
``ActiveMemoryReader.read_version``):

    A correction is NEVER an UPDATE of the old payload. The old canonical
    payload is immutable. A correction has exactly TWO modes, both applied
    inside ONE transaction with row-count checks on every write and a
    rollback on every pre-commit failure:

        mode='replace' (default)
            BEGIN
              SELECT target ... FOR UPDATE
              (validate authority / target / existing outgoing edge)
              (validate the optional correction_source_id against a real user row)
              INSERT new explicit_memories row
              INSERT memory_relations edge ('supersedes', to = new row)
              UPDATE old row SET status='archived'
            COMMIT

        mode='withdraw'
            BEGIN
              SELECT target ... FOR UPDATE
              (same validations)
              INSERT memory_relations edge ('withdraws', to = NULL)
              UPDATE old row SET status='archived'
            COMMIT

    A withdrawal creates NO replacement and makes NO embedding call. It is
    refused outright if ANY replacement payload is supplied — including an
    explicitly empty '' / [] — because "the caller sent an empty title" is
    not the same statement as "the caller sent no title".

    Then a FRESH lease verifies the exact target / replacement / edge before
    success is claimed. A verification failure AFTER the commit is reported
    as durable=True with an explicit verification error, never as a rolled
    back correction. Embedding runs only after a successful verification,
    as a warning that never reverts a durable correction.

    * ``authority`` must be exactly ``user_explicit``. Inference, extraction
      and passive paths never obtain it.
    * ``correction_id`` / ``relation_id`` are deterministic and target-bound:
      ``cor_<sha256(canonical_json(normalized request))>`` over target +
      mode + full replacement payload + reason + optional source + caller
      ``effective_at`` (absence stays absence for retry identity). So an exact
      retry is DEDUPLICATED and ``A -> B -> A-content`` is a NEW version, not
      a cycle back to A's row.
    * An existing outgoing edge is NEVER trusted on its hash alone: the
      recorded request is compared with the actual one AND the chain is
      followed to its real terminal. An exact retry still reports ITS OWN
      new_memory_id with that row's ACTUAL status, while current_memory_id
      reports the chain's true terminal successor — possibly further along,
      possibly None after a withdrawal.
    * Concurrent corrections of ONE target converge deterministically. A
      target has a single outgoing edge, so exactly one writer commits; every
      loser is refused by the driver — either because its deterministic
      replacement id is already occupied (``ON CONFLICT DO NOTHING
      RETURNING`` yields no row) or because the outgoing-edge unique index
      raises. A loser re-reads the COMMITTED edge on a fresh lease and answers
      an exact repeat as CORRECTION_DEDUPLICATED (durable, deduplicated) and a
      divergent request as CORRECTION_CONFLICT pointing at the real current
      version. A race loser is never reported as a hard DURABLE_FAILED and a
      raw UniqueViolation is never surfaced; the same re-read/decide applies
      when the post-commit readback happens to observe an intermediate state
      of the race.
    * A cyclic or dangling chain fails closed in both modes: the unique
      indexes do not prevent cycles, so no partial walk is ever reported as
      success.
    * A supplied ``correction_source_id`` must be a plain ASCII decimal id
      resolving to a real ``public.conversation_stream.id`` row with
      ``role='user'``, and is stored literally. A malformed one is rejected,
      never stripped, plus-prefixed or unicode-normalised into a real row.
      Without one, provenance is ``explicit_tool_request`` and the source id
      is NULL — no fabricated host / event / QA reference.
    * ``effective_at`` records the declared valid-from time only. M01 does not
      schedule future activation and implements no as-of reasoning (M04).
    * ``read_version`` walks the chain on ONE leased connection inside a
      read-only REPEATABLE READ transaction, so a writer committing mid-walk
      can never mix two database states into one answer.
    """
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from ._deadline import PrefetchDeadlineExceeded
from .pg_pool import DEFAULT_LEASE_TIMEOUT

logger = logging.getLogger("v3core.active_memory_store")

DURABLE_COMMITTED = "DURABLE_COMMITTED"
DEDUPLICATED = "DEDUPLICATED"
DERIVED_WARNING = "DERIVED_WARNING"
DURABLE_FAILED = "DURABLE_FAILED"

# ── M01 correction contract constants ─────────────────────────────────────
#
# These mirror schema/memory_relations.sql verbatim. The authority and
# relation type are pinned there by CHECK constraints, so there is no
# ``model_inference`` / ``supersedes`` value a caller could smuggle in.

CORRECTION_COMMITTED = "CORRECTION_COMMITTED"
CORRECTION_DEDUPLICATED = "CORRECTION_DEDUPLICATED"
CORRECTION_DERIVED_WARNING = "CORRECTION_DERIVED_WARNING"
CORRECTION_CONFLICT = "CORRECTION_CONFLICT"

ERR_INVALID_REQUEST = "INVALID_REQUEST"
ERR_UNAUTHORIZED_AUTHORITY = "UNAUTHORIZED_AUTHORITY"
ERR_INVALID_CORRECTION_SOURCE = "INVALID_CORRECTION_SOURCE"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_TARGET_NOT_CURRENT = "TARGET_NOT_CURRENT"
ERR_MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
ERR_DURABLE_FAILED = "DURABLE_FAILED"

REQUIRED_CORRECTION_AUTHORITY = "user_explicit"

# ── relation types actually persisted ────────────────────────────────────
#
# A correction is one of exactly two relations:
#
#   supersedes  A was once true, B is currently true  → to_memory_id = B
#   withdraws   A was once true, A is NOT true now   → to_memory_id = NULL
#
# ``RELATION_TYPE_CORRECTS`` survives ONLY as a compatibility SYMBOL alias for
# 'supersedes' so existing importers keep working. It is not a third persisted
# relation type and the schema CHECK refuses it.
RELATION_TYPE_SUPERSEDES = "supersedes"
RELATION_TYPE_WITHDRAWS = "withdraws"
RELATION_TYPE_CORRECTS = RELATION_TYPE_SUPERSEDES  # compatibility symbol only

CORRECTION_MODE_REPLACE = "replace"
CORRECTION_MODE_WITHDRAW = "withdraw"
CORRECTION_MODES = (CORRECTION_MODE_REPLACE, CORRECTION_MODE_WITHDRAW)
CORRECTION_MODE_RELATION_TYPE = {
    CORRECTION_MODE_REPLACE: RELATION_TYPE_SUPERSEDES,
    CORRECTION_MODE_WITHDRAW: RELATION_TYPE_WITHDRAWS,
}

EXPLICIT_TOOL_REQUEST_PROVENANCE = "explicit_tool_request"

# ── M03 source-reference contract (frozen) ────────────────────────────────
#
# An explicit memory may declare the conversation source it was derived from
# through exactly TWO accepted input forms, normalised to ONE canonical
# identity (``qa_pairs.id``). The resolver is NOT duplicated here: every
# validation goes through ``derived_invalidation.resolve_source_qa_ids`` so the
# write side and the propagation side can never disagree. A reference that is
# present but unresolvable is REFUSED (fail closed) — never stripped,
# normalised into a real row, or downgraded to "no source".
SOURCE_PROV_KEY_CONVERSATION_STREAM_ID = "source_conversation_stream_id"
SOURCE_PROV_KEY_QA_ID = "source_qa_id"
SOURCE_PROV_KEYS = (
    SOURCE_PROV_KEY_CONVERSATION_STREAM_ID,
    SOURCE_PROV_KEY_QA_ID,
)

#: Audit keys written into ``provenance`` after a successful resolution.
PROV_KEY_CANONICAL_QA_ID = "canonical_qa_id"
PROV_KEY_INPUT_SOURCE_KIND = "input_source_kind"
PROV_KEY_INPUT_SOURCE_ID = "input_source_id"

_TABLE = "public.explicit_memories"
_RELATION_TABLE = "public.memory_relations"
_SOURCE_TABLE = "public.conversation_stream"
_MEMORY_COLUMNS = (
    "memory_id", "category", "title", "content", "tags", "provenance",
    "status", "created_at", "updated_at", "embedding", "embed_model",
)
_RELATION_COLUMNS = (
    "relation_id", "relation_type", "from_memory_id", "to_memory_id",
    "correction_id", "authority", "correction_reason", "correction_source_id",
    "provenance", "effective_at", "recorded_at", "request",
)
_VERSION_LABEL_CURRENT = "current"
_VERSION_LABEL_SUPERSEDED = "superseded"
_VERSION_LABEL_WITHDRAWN = "withdrawn"
_VERSION_LABEL_ARCHIVED = "archived"
_PREVIEW_LEN = 240
_OK_STATUSES = (DURABLE_COMMITTED, DEDUPLICATED, DERIVED_WARNING)


# ── canonical id ──────────────────────────────────────────────────────────


def derive_memory_id(
    category: str,
    title: str,
    content: str,
    tags: Iterable[str],
) -> str:
    """``"mem_" + sha256(utf-8(canonical_json))``; tag order preserved."""
    payload = {
        "category": category,
        "title": title,
        "content": content,
        "tags": list(tags) if tags is not None else [],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "mem_" + hashlib.sha256(canonical).hexdigest()


# ── result dataclasses ────────────────────────────────────────────────────


@dataclass
class MemoryRecord:
    memory_id: str
    category: str
    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    source_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    embedding: Optional[list[float]] = None
    embed_model: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class MemoryWriteResult:
    memory_id: str
    source_id: str
    durable: bool
    status: str
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None
    record: Optional[dict[str, Any]] = None

    @property
    def success(self) -> bool:
        return self.status in _OK_STATUSES


@dataclass
class MemoryArchiveResult:
    memory_id: str
    found: bool
    table_available: bool = True
    archived: bool = False
    hard_rejected: bool = False
    already_archived: bool = False
    status: str = ""
    error: Optional[str] = None


# ── lease / pool plumbing ─────────────────────────────────────────────────


class _PoolUnavailable(RuntimeError):
    """This module never creates its own pool/connection."""


class _PgStoreLeaseAdapter:
    """Adapt ``PgEmbedStore.lease`` (yields conn) to a PgLease-shaped surface."""

    __slots__ = ("_ctx", "_conn", "_closed")

    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._conn = ctx.__enter__()
        self._closed = False

    @property
    def connection(self) -> Any:
        return self._conn

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ctx.__exit__(None, None, None)
        except Exception as exc:  # pragma: no cover
            logger.warning("PgEmbedStore lease release failed: %s", exc)

    def __enter__(self) -> "_PgStoreLeaseAdapter":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


class _OwnedConnectionLease:
    """A lease over an INDEPENDENT connection this lease owns and closes.

    A pool-less ``PgEmbedStore`` hands out its ONE cached ``_conn`` to every
    ``lease()`` call (``PgEmbedStore._connect`` returns the cached connection),
    so concurrent operations would interleave their BEGIN / COMMIT / ROLLBACK
    on a single physical connection and a single transaction. A correction can
    therefore never treat such a lease as "fresh": a sibling request's
    ``rollback()`` would undo the winner's writes and the post-commit readback
    would read inside the shared, half-rolled-back transaction. When the
    injected store exposes its public independent-connection seam, a lease must
    be a real, independent checkout instead; this lease owns that connection
    and closes it on release.
    """

    __slots__ = ("_conn", "_closed")

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._closed = False

    @property
    def connection(self) -> Any:
        return self._conn

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Never leak a half-open transaction onto a connection that is about
        # to be closed; a failed cleanup must not mask the close itself.
        try:
            _end_open_transaction(self._conn)
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            logger.warning("owned lease transaction cleanup failed: %r", exc)
        try:
            self._conn.close()
        except Exception as exc:  # pragma: no cover
            logger.warning("owned lease close failed: %r", exc)

    def __enter__(self) -> "_OwnedConnectionLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


def _acquire_lease(pool: Any, pg: Any, *, deadline: Any = None):
    """One lease from the injected pool/pg (PgPool preferred).

    Uses the canonical bounded lease timeout (DEFAULT_LEASE_TIMEOUT) so a
    missing deadline still admits a finite connect/wait bound instead of
    blocking indefinitely.  Deadline forwarding is preserved on the PgPool
    path; legacy/test pools that don't accept ``deadline`` fall back to the
    bounded timeout without dropping it.

    A pool-less store is NOT a source of independent leases: ``PgEmbedStore``
    caches one ``_conn`` and yields it to every ``lease()``. When the store
    offers its public ``open_side_connection()`` seam, take a genuinely
    independent connection from it so a correction's transaction (and its
    "fresh" readback) can never be shared with a concurrent request. Stores
    that expose no such seam (the injected fakes, a caller-supplied stub) keep
    the legacy lease path unchanged. When the seam EXISTS but yields no
    connection (returns ``None``) or raises, fail closed with
    ``_PoolUnavailable``: falling back to the shared ``lease()`` connection
    would silently reintroduce the cross-request transaction corruption the
    seam exists to prevent.
    """
    timeout = DEFAULT_LEASE_TIMEOUT
    if pool is not None:
        try:
            return pool.lease(timeout=timeout, deadline=deadline)
        except TypeError:
            return pool.lease(timeout=timeout)
    if pg is not None:
        side = getattr(pg, "open_side_connection", None)
        if callable(side):
            try:
                owned = side()
            except Exception as exc:
                raise _PoolUnavailable(
                    "PgEmbedStore.open_side_connection failed; refusing to "
                    "borrow the store's shared connection"
                ) from exc
            if owned is None:
                raise _PoolUnavailable(
                    "PgEmbedStore.open_side_connection returned no connection; "
                    "refusing to borrow the store's shared connection"
                )
            return _OwnedConnectionLease(owned)
        return _PgStoreLeaseAdapter(pg.lease(timeout=timeout))
    raise _PoolUnavailable("ActiveMemory requires an injected pool or pg.")


def _emb_str(vec: Iterable[float]) -> str:
    return "[" + ",".join(str(x) for x in vec) + "]"


def _jsonb(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ── real transaction status / ownership ───────────────────────────────────
#
# psycopg2's ``commit()`` / ``rollback()`` are SILENT NO-OPS while a
# connection is in autocommit mode, and a pool (PgPool) hands out every
# connection with autocommit=True. Whether a transaction is REALLY open can
# therefore never be decided from the autocommit FLAG alone: a connection can
# be autocommit=True and still carry a transaction opened by an explicit
# BEGIN, where ``rollback()`` does nothing. The driver's real transaction
# status is the only truthful source.

_TXN_STATUS_IDLE = 0  # psycopg2.extensions.TRANSACTION_STATUS_IDLE


def _transaction_status(conn: Any) -> Optional[int]:
    """The driver's real transaction status, or None if it exposes none."""
    getter = getattr(conn, "get_transaction_status", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def _end_open_transaction(conn: Any, *, force: bool = False) -> None:
    """End a REALLY-open transaction with a mechanism that works in autocommit.

    ``conn.rollback()`` does nothing at all in autocommit mode, so a
    transaction opened there by an explicit BEGIN must be ended with an
    explicit ROLLBACK statement through a cursor. On a non-autocommit
    connection the driver call is real and is used instead. ``force=True``
    attempts cleanup even when the status looks idle, because a failed BEGIN
    can leave an aborted transaction the driver may report imprecisely.
    """
    status = _transaction_status(conn)
    if status is None:
        # No real status exposed: fall back to the legacy autocommit flag.
        if force or not getattr(conn, "autocommit", True):
            conn.rollback()
        return
    if not force and status == _TXN_STATUS_IDLE:
        return
    if getattr(conn, "autocommit", True):
        conn.cursor().execute("ROLLBACK")
    else:
        conn.rollback()


# ── M01 correction: request normalization / identity ──────────────────────


def _canonical_json(obj: Any) -> str:
    """The one canonical form used for every M01 identity hash."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_correction_request(
    memory_id: str,
    replacement_content: Optional[str],
    *,
    mode: str = CORRECTION_MODE_REPLACE,
    replacement_title: Optional[str] = None,
    replacement_tags: Optional[Iterable[str]] = None,
    correction_reason: Optional[str] = None,
    correction_source_id: Optional[str] = None,
    effective_at: Optional[str] = None,
) -> dict[str, Any]:
    """Canonical, hashable form of one correction request.

    Every optional value keeps its ABSENCE as ``None`` — never coerced to
    ``""`` / ``[]`` / a default timestamp — because the identity hash is what
    makes an exact retry recognisable. ``mode`` is part of the hashed payload
    so a withdrawal can never collide with a replacement of the same target.
    ``effective_at`` is normalized to UTC ISO-8601, so two spellings of the
    same instant dedupe against each other while a genuinely different
    declared time stays a different request.
    """
    return {
        "memory_id": memory_id,
        "mode": mode,
        "replacement_content": replacement_content,
        "replacement_title": replacement_title,
        "replacement_tags": (
            None if replacement_tags is None else list(replacement_tags)
        ),
        "correction_reason": correction_reason,
        "correction_source_id": correction_source_id,
        "effective_at": effective_at,
    }


def derive_correction_id(memory_id: str, request: dict[str, Any]) -> str:
    """``cor_`` + sha256(canonical_json(request)), with the target bound in.

    The target is part of the hashed payload even when the caller normalized
    the request without it, so the same replacement text corrected from a
    different memory can never collide with this one.
    """
    payload = dict(request or {})
    payload.setdefault("memory_id", memory_id)
    payload["memory_id"] = memory_id
    return "cor_" + _sha256_hex(_canonical_json(payload))


def derive_correction_memory_id(
    target_memory_id: str,
    category: str,
    title: str,
    content: str,
    tags: Iterable[str],
    correction_id: str,
) -> str:
    """Canonical id of the REPLACEMENT row — target-bound, not payload-only.

    ``derive_memory_id`` intentionally hashes the payload alone, because an
    ordinary create must dedupe on identical content. A replacement must not:
    correcting target X to content C and correcting target Y to the same
    content C are two DIFFERENT versions of two different subjects, and
    ``A -> B -> A-content`` is a new version rather than a return to A's row.

    So the replacement id hashes the target and the correction identity (which
    itself binds the full replacement payload, reason, optional source and
    caller effective_at) alongside the payload.
    """
    payload = {
        "corrected_from": target_memory_id,
        "category": category,
        "title": title,
        "content": content,
        "tags": list(tags) if tags is not None else [],
        "correction_id": correction_id,
    }
    return "mem_" + _sha256_hex(_canonical_json(payload))


def derive_relation_id(correction_id: str) -> str:
    """Stable edge identity derived from the correction identity."""
    return "rel_" + _sha256_hex(correction_id)


def _validate_correction_request(
    memory_id: str,
    replacement_content: Optional[str],
    mode: Any,
    replacement_title: Optional[str],
    replacement_tags: Optional[Iterable[str]],
    correction_reason: Optional[str],
    correction_source_id: Optional[str],
    effective_at: Any,
    authority: Any,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Validate a correction request before any DB work.

    Returns ``(normalized_request, error)``. Explicit empty strings, invalid
    tags and naive timestamps are REJECTED — never silently reinterpreted as
    "omitted", because "the caller said empty" and "the caller said nothing"
    must not produce the same canonical memory.

    ``mode`` is the ONE place a withdrawal is decided. A withdrawal has no
    replacement at all, so ANY replacement payload — including explicitly
    empty ``""`` / ``[]`` — is refused rather than reinterpreted as absent.
    """
    if not isinstance(memory_id, str) or not memory_id.strip():
        return None, _correction_error(
            ERR_INVALID_REQUEST, "memory_id 必须为非空字符串"
        )

    if not isinstance(mode, str) or mode not in CORRECTION_MODES:
        return None, _correction_error(
            ERR_INVALID_REQUEST,
            f"mode 必须是 {'/'.join(repr(m) for m in CORRECTION_MODES)}"
            f"（收到 {mode!r}）",
        )

    withdrawing = mode == CORRECTION_MODE_WITHDRAW

    if withdrawing:
        # A withdrawal MUST NOT create a replacement, and any replacement
        # payload is refused — even an explicitly empty one, because "the
        # caller sent an empty title" is not the same statement as "the
        # caller sent no title".
        for name, value in (
            ("replacement_content", replacement_content),
            ("replacement_title", replacement_title),
            ("replacement_tags", replacement_tags),
        ):
            if value is None:
                continue
            return None, _correction_error(
                ERR_INVALID_REQUEST,
                f"mode='withdraw' 不接受任何替代内容；收到 {name}="
                f"{value!r}。撤回只记录 withdraws 边并归档原记忆，"
                f"不会创建替代版本",
            )
    elif not isinstance(replacement_content, str) or not replacement_content.strip():
        return None, _correction_error(
            ERR_INVALID_REQUEST, "replacement_content 必须为非空字符串"
        )

    if replacement_title is not None and (
        not isinstance(replacement_title, str) or not replacement_title.strip()
    ):
        return None, _correction_error(
            ERR_INVALID_REQUEST, "replacement_title 若提供则必须为非空字符串"
        )

    tag_list: Optional[list[str]] = None
    if replacement_tags is not None:
        if isinstance(replacement_tags, str) or not isinstance(
            replacement_tags, (list, tuple)
        ):
            return None, _correction_error(
                ERR_INVALID_REQUEST, "replacement_tags 必须为非空 str 列表"
            )
        tag_list = list(replacement_tags)
        if not tag_list or any(
            not isinstance(t, str) or not t.strip() for t in tag_list
        ):
            return None, _correction_error(
                ERR_INVALID_REQUEST, "replacement_tags 若提供则元素必须为非空 str"
            )

    if correction_reason is not None and (
        not isinstance(correction_reason, str) or not correction_reason.strip()
    ):
        return None, _correction_error(
            ERR_INVALID_REQUEST, "correction_reason 若提供则必须为非空字符串"
        )

    if correction_source_id is not None and not _is_source_id_literal(
        correction_source_id
    ):
        # A source id must be a real conversation_stream.id. We validate the
        # SHAPE here and the EXISTENCE + role inside the transaction. The
        # value is kept LITERALLY — a malformed id is refused, never
        # stripped, plus-prefixed or unicode-normalised into a real one.
        return None, _correction_error(
            ERR_INVALID_REQUEST,
            "correction_source_id 若提供则必须为 ASCII 十进制数字形式的 "
            f"conversation_stream.id（收到 {correction_source_id!r}）",
        )

    effective_iso: Optional[str] = None
    if effective_at is not None:
        effective_iso = _normalize_effective_at(effective_at)
        if effective_iso is None:
            return None, _correction_error(
                ERR_INVALID_REQUEST,
                "effective_at 必须是带时区的 ISO-8601 时间戳",
            )

    # Authority is checked LAST and separately: it is not an input-shape
    # problem, and an unauthorized caller must never be told about the target.
    if authority != REQUIRED_CORRECTION_AUTHORITY:
        return None, _correction_error(
            ERR_UNAUTHORIZED_AUTHORITY,
            f"纠正必须显式声明 authority={REQUIRED_CORRECTION_AUTHORITY!r}；"
            f"模型推断 / 被动 / 普通写入路径不会自动获得该权限（收到 {authority!r}）",
        )

    request = normalize_correction_request(
        memory_id,
        replacement_content,
        mode=mode,
        replacement_title=(replacement_title if replacement_title else None),
        replacement_tags=tag_list,
        correction_reason=(correction_reason if correction_reason else None),
        correction_source_id=correction_source_id,   # kept LITERAL, never repaired
        effective_at=effective_iso,
    )
    return request, None


def _is_source_id_literal(value: Any) -> bool:
    """True only for a non-empty run of ASCII decimal digits.

    ``str.isdigit`` is too permissive — it accepts '١١' and '１' as well as
    superscripts — and a leading ``+`` or surrounding whitespace would mean
    the caller sent something that is not a plain id. Such a value is an
    explicit statement, not an absent one, so it is rejected rather than
    normalised into a real source row.
    """
    if not isinstance(value, str) or not value:
        return False
    return all("0" <= ch <= "9" for ch in value)


def _normalize_effective_at(value: Any) -> Optional[str]:
    """ISO-8601 with an explicit offset → UTC ISO-8601. Naive → None.

    A timestamp without a timezone is ambiguous (whose "now"?), so it is
    rejected instead of being read as local time.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _correction_error(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": message}


def _source_prov_declared(prov: Any) -> bool:
    """True when either frozen source key carries a value (``None`` = absent)."""
    if not isinstance(prov, dict):
        return False
    return any(prov.get(key) is not None for key in SOURCE_PROV_KEYS)


def _resolve_source_provenance(
    pg: Any, prov: Any
) -> tuple[dict[str, Any], Optional[str], Optional[str]]:
    """Validate the two frozen M03 source keys, fail closed.

    Returns ``(resolved_prov, error_code, error_message)``. No declared source
    key → ``(prov, None, None)`` unchanged, so every pre-existing caller keeps
    its exact behaviour. A declared reference is resolved through the ONE
    canonical resolver (``derived_invalidation.resolve_source_qa_ids``): the
    caller's literal input keys are preserved verbatim and the single canonical
    identity plus its audit fields are added. A reference that cannot be
    resolved — missing, malformed, ambiguous, or two keys disagreeing — is
    REFUSED with the frozen code, never fabricated or dropped.
    """
    if not _source_prov_declared(prov):
        return (prov if isinstance(prov, dict) else {}), None, None

    from . import derived_invalidation as _di

    try:
        qa_ids, status = _di.resolve_source_qa_ids(pg, prov)
    except Exception as exc:  # noqa: BLE001 - a resolver fault is a refusal
        return (
            dict(prov),
            _di.PROPAGATION_FAILED,
            f"source reference 解析失败（fail closed）: {exc!r}",
        )

    if status == _di.NO_SOURCE:
        # Keys were declared, so absence is impossible here; treating it as
        # "no source" would silently drop a declared reference.
        return (
            dict(prov),
            _di.SOURCE_NOT_MAPPED,
            "source reference 已声明但未解析到任何 qa_pairs 行",
        )
    if status != _di.RESOLVE_OK:
        return (
            dict(prov),
            status,
            f"source reference 解析失败（fail closed）: {status}",
        )

    out = dict(prov)
    kind, input_id = _di.source_input_identity(prov)
    out[PROV_KEY_CANONICAL_QA_ID] = qa_ids[0] if len(qa_ids) == 1 else None
    out[PROV_KEY_INPUT_SOURCE_KIND] = kind
    out[PROV_KEY_INPUT_SOURCE_ID] = input_id
    return out, None, None


def _source_ref_failure(
    memory_id: str, code: str, message: str
) -> MemoryWriteResult:
    """A fail-closed refusal carrying the frozen source code verbatim."""
    return MemoryWriteResult(
        memory_id=memory_id,
        source_id=memory_id,
        durable=False,
        status=code,
        warnings=[message],
        error=message,
        record=None,
    )


def _is_missing_relation_table(exc: BaseException) -> bool:
    """True when the driver says ``memory_relations`` (or a relation) is absent.

    Only the missing-table / missing-relation shapes map to
    MIGRATION_REQUIRED. Any other database error stays DURABLE_FAILED — we
    never relabel an unrelated fault as "please run the migration".
    """
    if isinstance(exc, _MissingRelationTable):
        return True
    text = str(exc)
    low = text.lower()
    if "memory_relations" in low and (
        "does not exist" in low or "undefinedtable" in low
        or "undefined_table" in low
    ):
        return True
    if "undefinedtable" in low or "undefined_table" in low:
        return True
    if "does not exist" in low and (
        "relation" in low or "public.memory_relations" in low
    ):
        return True
    return False


class _MissingRelationTable(RuntimeError):
    """Internal marker used by tests / drivers to signal an absent table."""


class _CorrectionRaceLost(Exception):
    """A concurrent correction of the same target won the race for its edge.

    The target has exactly ONE outgoing edge (unique index on
    ``from_memory_id``), so when corrections of the same target run
    concurrently one wins and the others are refused by the driver:
    ``INSERT ... ON CONFLICT (memory_id) DO NOTHING RETURNING`` yields no row
    for an identical request (the winner already committed that deterministic
    id) or the outgoing-edge unique index raises. That is NOT an internal
    failure — the loser's verdict is decided from the COMMITTED state.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_unique_violation(exc: BaseException) -> bool:
    """True only for a SQLSTATE 23505 unique-violation.

    psycopg2 exposes ``UniqueViolation`` / ``pgcode='23505'``; the
    deterministic fake exposes the same shape. The class name and the server
    message are accepted as fallbacks so no driver detail is required at
    import time (this module never imports a DB driver). Any OTHER database
    error stays a plain failure — never relabelled a race.
    """
    code = getattr(exc, "pgcode", None) or getattr(exc, "sqlstate", None)
    if code == "23505":
        return True
    if "UniqueViolation" in type(exc).__name__:
        return True
    return "duplicate key value violates unique constraint" in str(exc).lower()


# ── embedding helpers (post-commit only) ──────────────────────────────────


def _resolve_embed_cfg(config: Any) -> Optional[dict]:
    try:
        from .embedding import safe_embed_cfg as _safe_embed_cfg
    except ImportError:
        return None
    try:
        return _safe_embed_cfg(config)
    except ValueError:
        raise
    except Exception:
        return None


def _resolve_model(embed_cfg: dict) -> str:
    fp = (embed_cfg.get("_fingerprint") or "").strip()
    if fp:
        return fp
    model = (embed_cfg.get("model") or "").strip()
    if model:
        return model
    raise ValueError("embed_cfg 缺 _fingerprint/model — 无法记录 embed_model")


def _default_embedder(text: str, embed_cfg: dict) -> list[float]:
    from .embedding import call_embedding as _call_embedding
    from .embedding import DURABLE_WRITE_EMBED_POLICY

    # Explicit-memory writes are durable, not realtime: name the policy so they do not
    # inherit call_embedding's 3s/0 default. No marker is written here because this
    # module already surfaces failure explicitly to its caller (`return [msg], msg`)
    # rather than swallowing it — the accounting requirement is satisfied upstream.
    return _call_embedding(text, embed_cfg, policy=DURABLE_WRITE_EMBED_POLICY)


_EMBED_CFG_SENTINEL = object()


# ── writer ────────────────────────────────────────────────────────────────


class ActiveMemoryWriter:
    """Canonical writer for ``public.explicit_memories``."""

    def __init__(
        self,
        pool: Any = None,
        pg: Any = None,
        config: Any = None,
        embed_cfg: Any = _EMBED_CFG_SENTINEL,
        embedder: Optional[Callable[[str, dict], list[float]]] = None,
    ) -> None:
        if pool is None and pg is None:
            raise _PoolUnavailable(
                "ActiveMemoryWriter requires an injected pool or pg."
            )
        self._pool = pool
        self._pg = pg
        self._config = config
        self._embed_cfg_explicit = embed_cfg is not _EMBED_CFG_SENTINEL
        self._embed_cfg_value: Any = embed_cfg
        self._embedder = embedder if embedder is not None else _default_embedder

    # ── public API ────────────────────────────────────────────────────────

    def create(
        self,
        category: str,
        title: str,
        content: str,
        tags: Optional[Iterable[str]] = None,
        source_id: Optional[str] = None,
        memory_id: Optional[str] = None,
        provenance: Optional[dict[str, Any]] = None,
        embedder: Optional[Callable[[str, dict], list[float]]] = None,
    ) -> MemoryWriteResult:
        """Canonical create — see module docstring."""
        # input validation (cheap, no DB)
        for fld, val in (("category", category), ("title", title), ("content", content)):
            if not isinstance(val, str) or not val:
                return _failed("", "", f"{fld} 必须为非空字符串")

        tag_list = list(tags) if tags is not None else []
        if any(not isinstance(t, str) for t in tag_list):
            return _failed("", "", "tags 元素必须为 str")

        prov = dict(provenance) if provenance else {}

        # canonical id — preserve caller values unchanged (whitespace-only is
        # treated as empty for the "supplied" check, but the raw selected
        # memory_id is what we surface as source_id).
        explicit = memory_id if isinstance(memory_id, str) and memory_id else None
        alias = source_id if isinstance(source_id, str) and source_id else None

        if explicit is not None and alias is not None and explicit != alias:
            # Caller supplied both and they disagree — refuse to silently
            # rewrite; surface the conflict as a hard durable failure.
            return _failed(
                explicit,
                alias,
                "memory_id 和 source_id 不一致；两者必须相同或只提供一个",
            )

        if explicit is not None:
            mid = explicit
        elif alias is not None:
            mid = alias
        else:
            mid = derive_memory_id(category, title, content, tag_list)

        # M03: a declared source reference is validated BEFORE any write, through
        # the one canonical resolver. Unresolvable / ambiguous / conflicting →
        # refused with the frozen code, nothing is inserted, and the caller's
        # reference is never silently dropped or turned into "no source".
        resolved_prov, prov_code, prov_err = _resolve_source_provenance(
            self._pg if self._pg is not None else self._pool, prov
        )
        if prov_code is not None:
            return _source_ref_failure(mid, prov_code, prov_err or prov_code)
        prov = resolved_prov

        # INSERT ... ON CONFLICT DO NOTHING + commit
        try:
            lease_a = _acquire_lease(self._pool, self._pg)
        except Exception as exc:
            return _failed(mid, mid, f"INSERT lease 失败: {exc!r}")

        try:
            try:
                cur = lease_a.connection.cursor()
                cur.execute(
                    f"""
                    INSERT INTO {_TABLE}
                        (memory_id, category, title, content, tags, provenance,
                         status, created_at, updated_at)
                    VALUES
                        (%s, %s, %s, %s, %s, %s::jsonb, 'active', NOW(), NOW())
                    ON CONFLICT (memory_id) DO NOTHING
                    RETURNING memory_id
                    """,
                    (mid, category, title, content, tag_list, _jsonb(prov)),
                )
                inserted = cur.fetchone() is not None
                lease_a.connection.commit()
            finally:
                lease_a.close()
        except Exception as exc:
            return _failed(mid, mid, f"INSERT failed: {exc!r}")

        # fresh readback through lease #2
        try:
            lease_b = _acquire_lease(self._pool, self._pg)
        except Exception as exc:
            return _failed(mid, mid, f"readback lease 失败: {exc!r}")

        record: Optional[dict[str, Any]] = None
        try:
            try:
                cur = lease_b.connection.cursor()
                cur.execute(
                    f"""
                    SELECT memory_id, category, title, content, tags, provenance,
                           status, created_at, updated_at, embedding, embed_model
                      FROM {_TABLE}
                     WHERE memory_id = %s
                    """,
                    (mid,),
                )
                row = cur.fetchone()
            finally:
                lease_b.close()
        except Exception as exc:
            return _failed(mid, mid, f"readback failed: {exc!r}")

        if row is None:
            return _failed(mid, mid, "readback returned no row after INSERT")

        keys = (
            "memory_id", "category", "title", "content", "tags", "provenance",
            "status", "created_at", "updated_at", "embedding", "embed_model",
        )
        raw = dict(zip(keys, row))
        record = {
            "memory_id": raw["memory_id"],
            "source_id": raw["memory_id"],
            "category": raw["category"],
            "title": raw["title"],
            "content": raw["content"],
            "tags": list(raw["tags"] or []),
            "provenance": raw["provenance"] if isinstance(raw["provenance"], dict) else {},
            "status": raw["status"],
            "created_at": raw["created_at"].isoformat() if raw["created_at"] else None,
            "updated_at": raw["updated_at"].isoformat() if raw["updated_at"] else None,
            "embedding": list(raw["embedding"]) if raw["embedding"] is not None else None,
            "embed_model": raw["embed_model"],
        }

        same_canonical = (
            record["memory_id"] == mid
            and record["category"] == category
            and record["title"] == title
            and record["content"] == content
            and list(record["tags"]) == tag_list
        )
        if not same_canonical:
            return _failed(
                mid, mid,
                "memory_id 已被不同 canonical payload 占用；保留现有行，不更新",
                record=record,
            )

        # post-commit embedding (only after canonical durable readback)
        embed_warnings, embed_error = self._maybe_embed(
            mid=mid, content=content, embedder=embedder
        )

        if embed_error:
            return MemoryWriteResult(
                memory_id=mid,
                source_id=mid,
                durable=True,
                status=DERIVED_WARNING if inserted else DEDUPLICATED,
                warnings=embed_warnings,
                error=embed_error,
                record=record,
            )

        return MemoryWriteResult(
            memory_id=mid,
            source_id=mid,
            durable=True,
            status=DURABLE_COMMITTED if inserted else DEDUPLICATED,
            warnings=embed_warnings,
            record=record,
        )

    def write(self, *args: Any, **kwargs: Any) -> MemoryWriteResult:
        return self.create(*args, **kwargs)

    def archive(self, memory_id: str, hard: bool = False) -> MemoryArchiveResult:
        """Archive (soft) or reject (hard) one canonical row.

        Reads all statuses. ``hard=True`` → no DELETE. Soft path UPDATEs
        ``status='archived'`` and ``updated_at=NOW()`` explicitly (no
        trigger). Already-archived → truthful no-op. Missing → ``found=False``.
        Table missing / DB error → ``table_available=False``.
        """
        mid = memory_id if isinstance(memory_id, str) and memory_id else ""
        if not mid:
            return MemoryArchiveResult(
                memory_id="", found=False, error="memory_id 必须为非空字符串"
            )

        try:
            lease = _acquire_lease(self._pool, self._pg)
        except Exception as exc:
            return MemoryArchiveResult(
                memory_id=mid,
                found=False,
                table_available=False,
                error=f"archive lease 失败: {exc!r}",
            )

        try:
            try:
                cur = lease.connection.cursor()
                cur.execute(
                    f"SELECT memory_id, status FROM {_TABLE} WHERE memory_id = %s",
                    (mid,),
                )
                row = cur.fetchone()
            finally:
                lease.close()
        except Exception as exc:
            msg = str(exc)
            table_missing = (
                "does not exist" in msg
                or "UndefinedTable" in msg
                or "relation" in msg
            )
            return MemoryArchiveResult(
                memory_id=mid,
                found=False,
                table_available=False,
                error=msg if table_missing else f"archive SELECT failed: {msg}",
            )

        if row is None:
            return MemoryArchiveResult(memory_id=mid, found=False)

        current_status = row[1] if len(row) > 1 else ""

        if hard:
            return MemoryArchiveResult(
                memory_id=mid,
                found=True,
                hard_rejected=True,
                already_archived=(current_status == "archived"),
                status=current_status or "",
            )

        if current_status == "archived":
            return MemoryArchiveResult(
                memory_id=mid,
                found=True,
                already_archived=True,
                status="archived",
            )

        try:
            lease = _acquire_lease(self._pool, self._pg)
            try:
                cur = lease.connection.cursor()
                cur.execute(
                    f"""
                    UPDATE {_TABLE}
                       SET status = 'archived',
                           updated_at = NOW()
                     WHERE memory_id = %s
                       AND status = 'active'
                    """,
                    (mid,),
                )
                lease.connection.commit()
            finally:
                lease.close()
        except Exception as exc:
            return MemoryArchiveResult(
                memory_id=mid, found=True, error=f"archive UPDATE failed: {exc!r}"
            )

        return MemoryArchiveResult(
            memory_id=mid, found=True, archived=True, status="archived"
        )

    # ── M01: explicit memory correction ───────────────────────────────

    def correct(
        self,
        memory_id: str,
        replacement_content: Optional[str] = None,
        *,
        mode: str = CORRECTION_MODE_REPLACE,
        replacement_title: Optional[str] = None,
        replacement_tags: Optional[Iterable[str]] = None,
        correction_reason: Optional[str] = None,
        correction_source_id: Optional[str] = None,
        effective_at: Optional[str] = None,
        authority: Optional[str] = None,
        source_conversation_stream_id: Optional[str] = None,
        source_qa_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Correct one canonical memory: replace it, or withdraw it (M01).

        The old payload is never rewritten. In ONE transaction: lock the
        target, decide idempotency vs conflict, insert the new active row
        (replace only), insert the ``supersedes`` / ``withdraws`` edge, archive
        the old row — then commit, then verify with a fresh lease, then embed
        (warning only).

        ``mode='replace'`` (the default, so every existing caller is
        unchanged) inserts a new current version. ``mode='withdraw'`` inserts
        NO replacement and no embedding: it only records a ``withdraws`` edge
        with a NULL target and archives the row, which is how the caller
        states that what was once true no longer is.

        Returns a receipt dict carrying at least ``success``, ``mode``,
        ``old_memory_id``, ``new_memory_id``, ``relation``, ``old_status``,
        ``new_status``, ``correction_source`` and ``deduplicated``.
        """
        request, error = _validate_correction_request(
            memory_id,
            replacement_content,
            mode,
            replacement_title,
            replacement_tags,
            correction_reason,
            correction_source_id,
            effective_at,
            authority,
        )
        if error is not None:
            return {
                **error,
                "status": error["error_code"],
                "mode": mode if mode in CORRECTION_MODES else None,
                "durable": False,
                "deduplicated": False,
                "old_memory_id": memory_id if isinstance(memory_id, str) else None,
                "new_memory_id": None,
                "old_status": None,
                "new_status": None,
                "current_memory_id": None,
                "relation": None,
                "correction_source": _correction_source_view(
                    EXPLICIT_TOOL_REQUEST_PROVENANCE, None
                ),
                "warnings": [error["error"]],
            }

        target = str(request["memory_id"])
        withdrawing = request["mode"] == CORRECTION_MODE_WITHDRAW
        relation_type = CORRECTION_MODE_RELATION_TYPE[request["mode"]]

        # M03 source-reference reinforcement on the replacement path: a caller
        # may declare the replacement's conversation source through the same two
        # frozen keys. Validated through the ONE canonical resolver BEFORE any
        # write; a present-but-unresolvable reference is refused (fail closed),
        # never dropped or downgraded into "no source".
        declared_source = {
            key: value
            for key, value in (
                (SOURCE_PROV_KEY_CONVERSATION_STREAM_ID,
                 source_conversation_stream_id),
                (SOURCE_PROV_KEY_QA_ID, source_qa_id),
            )
            if value is not None
        }
        resolved_source: dict[str, Any] = {}
        if declared_source:
            resolved_source, src_code, src_err = _resolve_source_provenance(
                self._pg if self._pg is not None else self._pool,
                declared_source,
            )
            if src_code is not None:
                return _correction_failure(
                    src_code, target, None, request["mode"],
                    src_err or src_code,
                )

        correction_id = derive_correction_id(target, request)
        relation_id = derive_relation_id(correction_id)
        request_json = _canonical_json(request)
        source_id = request["correction_source_id"]
        effective_iso = request["effective_at"]
        reason = request["correction_reason"]

        try:
            lease = _acquire_lease(self._pool, self._pg)
        except Exception as exc:
            return _correction_failure(
                ERR_DURABLE_FAILED, target, None, request["mode"],
                f"纠正 lease 失败: {exc!r}",
            )

        committed = False
        commit_attempted = False
        race_lost: Optional[str] = None
        new_id: Optional[str] = None
        expected_old: dict[str, Any] = {}
        expected_new: dict[str, Any] = {}
        conn: Any = None
        took_txn_ownership = False
        try:
            try:
                conn = lease.connection
                # PART 1 — the correction owns its transaction. psycopg2's
                # commit()/rollback() are SILENT NO-OPS in autocommit mode, and
                # a pool (PgPool) hands out every connection with
                # autocommit=True. Running this explicit BEGIN ... COMMIT
                # transaction under autocommit would never commit and would
                # leave a live transaction open for the next lease (which then
                # breaks the read snapshot's BEGIN ISOLATION). Make the driver
                # own the transaction BEFORE the explicit BEGIN; the delivered
                # autocommit baseline is restored in the finally below, before
                # the lease is released, so the pool contract still holds.
                if getattr(conn, "autocommit", False) is True:
                    conn.autocommit = False
                    took_txn_ownership = True
                cur = conn.cursor()
                cur.execute("BEGIN")

                try:
                    # 1) lock the target row FOR UPDATE
                    cur.execute(
                        f"""
                        SELECT {", ".join(_MEMORY_COLUMNS)}
                          FROM {_TABLE}
                         WHERE memory_id = %s
                         FOR UPDATE
                        """,
                        (target,),
                    )
                    target_row = cur.fetchone()
                    if target_row is None:
                        conn.rollback()
                        return _correction_not_found(target)

                    # 2) existing outgoing edge decides dedupe vs conflict
                    cur.execute(
                        f"""
                        SELECT {", ".join(_RELATION_COLUMNS)}
                          FROM {_RELATION_TABLE}
                         WHERE from_memory_id = %s
                        """,
                        (target,),
                    )
                    existing_edge = cur.fetchone()
                    if existing_edge is not None:
                        # The edge must be resolved against the ACTUAL chain
                        # (a later follow-up may have moved it on), so hand
                        # the still-open connection over instead of opening a
                        # second lease inside the first one.
                        resolved = self._resolve_existing_edge(
                            conn, cur, target, existing_edge,
                            request, correction_id, request_json, mode,
                        )
                        conn.rollback()
                        return resolved

                    # 3) target must be current: no edge, so it must be active
                    if target_row[_MEMORY_COLUMNS.index("status")] != "active":
                        conn.rollback()
                        return _correction_failure(
                            ERR_TARGET_NOT_CURRENT, target, None, mode,
                            f"{target} 已是 archived 且没有纠正关系；"
                            f"不能对无当前版本的目标执行纠正",
                        )

                    # 4) a supplied source must be a real user message
                    if source_id is not None:
                        cur.execute(
                            f"""
                            SELECT id, role
                              FROM {_SOURCE_TABLE}
                             WHERE id = %s
                            """,
                            (int(source_id),),
                        )
                        source_row = cur.fetchone()
                        if source_row is None or source_row[1] != "user":
                            conn.rollback()
                            return _correction_failure(
                                ERR_INVALID_CORRECTION_SOURCE, target, None, mode,
                                f"correction_source_id={source_id!r} 不是 "
                                f"conversation_stream 中 role='user' 的真实消息 id",
                            )

                    # 5) the old immutable payload — category / title / content
                    #    / tags AND provenance — is captured BEFORE the branch,
                    #    for BOTH modes: a correction may never rewrite any of
                    #    them, and a withdrawal has no replacement row to
                    #    compare, so the old row is the only thing to verify.
                    old_provenance = target_row[_MEMORY_COLUMNS.index("provenance")]
                    if isinstance(old_provenance, str):
                        try:
                            old_provenance = json.loads(old_provenance)
                        except ValueError:
                            pass
                    expected_old = {
                        "category": target_row[_MEMORY_COLUMNS.index("category")],
                        "title": target_row[_MEMORY_COLUMNS.index("title")],
                        "content": target_row[_MEMORY_COLUMNS.index("content")],
                        "tags": list(target_row[_MEMORY_COLUMNS.index("tags")] or []),
                        "provenance": old_provenance,
                    }

                    # 6) a withdrawal inserts NO replacement row and no embedding
                    if not withdrawing:
                        # inherit omitted fields; category is always preserved
                        category = target_row[_MEMORY_COLUMNS.index("category")]
                        title = (
                            request["replacement_title"]
                            if request["replacement_title"] is not None
                            else target_row[_MEMORY_COLUMNS.index("title")]
                        )
                        tags = (
                            list(request["replacement_tags"])
                            if request["replacement_tags"] is not None
                            else list(target_row[_MEMORY_COLUMNS.index("tags")] or [])
                        )
                        new_provenance = _jsonb({
                            "corrected_from": target,
                            "correction_id": correction_id,
                            "relation_id": relation_id,
                            "authority": REQUIRED_CORRECTION_AUTHORITY,
                            "correction_source_id": source_id,
                            "provenance": EXPLICIT_TOOL_REQUEST_PROVENANCE,
                            **resolved_source,
                        })

                        # deterministic, target-bound replacement id
                        new_id = derive_correction_memory_id(
                            target, category, title, request["replacement_content"],
                            tags, correction_id,
                        )
                        if new_id == target:  # pragma: no cover - impossible
                            return _correction_failure(
                                ERR_DURABLE_FAILED, target, None, mode,
                                "replacement identity collided with the target row",
                            )

                        cur.execute(
                            f"""
                            INSERT INTO {_TABLE}
                                (memory_id, category, title, content, tags,
                                 provenance, status, created_at, updated_at)
                            VALUES
                                (%s, %s, %s, %s, %s, %s::jsonb, 'active',
                                 NOW(), NOW())
                            ON CONFLICT (memory_id) DO NOTHING
                            RETURNING memory_id
                            """,
                            (new_id, category, title, request["replacement_content"],
                             tags, new_provenance),
                        )
                        if cur.fetchone() is None:
                            # The deterministic replacement id is already
                            # occupied. That is NOT automatically an internal
                            # failure: a concurrent correction of this same
                            # target may have committed this very id first
                            # (an identical request has the same deterministic
                            # replacement). The verdict is decided from the
                            # COMMITTED edge after this transaction is rolled
                            # back — never fabricated here.
                            raise _CorrectionRaceLost(
                                "replacement memory_id 已被相同 payload 占用；"
                                "未覆盖任何行"
                            )
                        expected_new = {
                            "category": category,
                            "title": title,
                            "content": request["replacement_content"],
                            "tags": list(tags),
                        }

                    # 7) the edge — to_memory_id is NULL for a withdrawal
                    try:
                        cur.execute(
                            f"""
                            INSERT INTO {_RELATION_TABLE}
                                (relation_id, relation_type, from_memory_id,
                                 to_memory_id, correction_id, authority,
                                 correction_reason, correction_source_id,
                                 provenance, effective_at, request)
                            VALUES
                                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                 %s::jsonb)
                            RETURNING relation_id
                            """,
                            (
                                relation_id, relation_type, target, new_id,
                                correction_id, REQUIRED_CORRECTION_AUTHORITY,
                                reason, source_id,
                                EXPLICIT_TOOL_REQUEST_PROVENANCE,
                                effective_iso, request_json,
                            ),
                        )
                    except Exception as exc:
                        if _is_unique_violation(exc):
                            # The target's single outgoing-edge slot is
                            # already taken by a concurrent correction that
                            # committed first. The transaction is aborted by
                            # the server, so nothing of ours can be decided
                            # here: roll back whole and decide from the
                            # COMMITTED edge (idempotent replay vs conflict).
                            raise _CorrectionRaceLost(
                                f"纠正关系写入与并发纠正冲突（{exc}）"
                            ) from exc
                        raise
                    if cur.fetchone() is None:
                        conn.rollback()
                        return _correction_failure(
                            ERR_DURABLE_FAILED, target, new_id, mode,
                            "纠正关系写入失败（row count 检查未通过）",
                        )

                    # 8) archive the old row — payload columns untouched
                    cur.execute(
                        f"""
                        UPDATE {_TABLE}
                           SET status = 'archived',
                               updated_at = NOW()
                         WHERE memory_id = %s
                           AND status = 'active'
                        RETURNING memory_id
                        """,
                        (target,),
                    )
                    if cur.fetchone() is None:
                        conn.rollback()
                        return _correction_failure(
                            ERR_DURABLE_FAILED, target, new_id, mode,
                            "旧记忆归档失败（row count 检查未通过）",
                        )

                    # The COMMIT is the point of no return: once it is
                    # attempted, a raised exception may mean "did not persist"
                    # OR "persisted but the acknowledgement was lost". We can
                    # never tell the two apart, so we never claim a rollback.
                    commit_attempted = True
                    conn.commit()
                    committed = True
                except _CorrectionRaceLost as exc:
                    # A concurrent correction of the same target committed the
                    # target's single outgoing edge first, so the driver
                    # refused OUR write (ON CONFLICT DO NOTHING returned no
                    # row for the replacement, or the outgoing-edge unique
                    # index raised). Nothing of ours persists: roll the whole
                    # transaction back, and decide the verdict from the
                    # COMMITTED state AFTER this lease is released — never
                    # fabricate it inside the aborted transaction.
                    try:
                        conn.rollback()
                    except Exception:  # pragma: no cover - rollback itself failed
                        logger.warning("纠正 rollback 失败: %r", exc)
                    race_lost = exc.reason
                except Exception as exc:
                    try:
                        conn.rollback()
                    except Exception:  # pragma: no cover - rollback itself failed
                        logger.warning("纠正 rollback 失败: %r", exc)
                    if commit_attempted and not committed:
                        # An attempted COMMIT whose outcome is unknown dominates
                        # any textual/schema shape: the error merely LOOKS like
                        # a missing table, but we cannot tell whether the write
                        # persisted, so we never claim MIGRATION_REQUIRED (which
                        # would imply nothing was written).
                        return _correction_indeterminate(
                            target, new_id, mode,
                            f"纠正 COMMIT 结果不确定：提交阶段抛出 {exc!r}；"
                            f"无法判定事务是否已持久化，不声明回滚",
                        )
                    if _is_missing_relation_table(exc):
                        return _correction_failure(
                            ERR_MIGRATION_REQUIRED, target, new_id, mode,
                            f"{_RELATION_TABLE} 不存在；请先应用 "
                            f"schema/memory_relations.sql 迁移（{exc}）",
                        )
                    return _correction_failure(
                        ERR_DURABLE_FAILED, target, new_id, mode,
                        f"纠正事务失败: {exc!r}",
                    )
            finally:
                if took_txn_ownership:
                    # Restore the delivered autocommit baseline BEFORE the
                    # lease is released, so the pool contract (autocommit True
                    # on delivery/return) still holds. This must never mask
                    # the release itself.
                    try:
                        conn.autocommit = True
                    except Exception as restore_exc:  # pragma: no cover
                        logger.warning(
                            "恢复 autocommit 基线失败: %r", restore_exc,
                        )
                lease.close()
        except Exception as exc:
            # A failure releasing the lease (or any other plumbing fault) that
            # happens AFTER the commit returned cannot undo a durable write.
            if committed:
                return _post_commit_failure(
                    target, new_id, mode,
                    f"纠正已提交但连接释放失败: {exc!r}",
                )
            if commit_attempted:
                return _correction_indeterminate(
                    target, new_id, mode,
                    f"纠正 COMMIT 结果不确定：{exc!r}",
                )
            return _correction_failure(
                ERR_DURABLE_FAILED, target, new_id, mode,
                f"纠正执行失败: {exc!r}",
            )

        if race_lost is not None:
            # The loser of a concurrent-correction race: our write was refused
            # by the driver, nothing of ours persists. The verdict — an
            # idempotent replay of the winner's committed edge, or a
            # deterministic CORRECTION_CONFLICT — is decided on a FRESH lease
            # from what is ACTUALLY committed, and is never fabricated.
            resolved, why = self._resolve_from_committed_edge(
                target, request, correction_id, request_json, mode,
                reason=race_lost,
            )
            if resolved is not None:
                return resolved
            return _correction_failure(
                ERR_DURABLE_FAILED, target, new_id, mode, why,
            )

        if not committed:  # pragma: no cover - defensive
            return _correction_failure(
                ERR_DURABLE_FAILED, target, None, mode, "纠正事务未提交",
            )

        # 9) verified readback on a FRESH lease before claiming success
        verification = self._verify_correction(
            target, new_id, relation_id, request, mode,
            expected_old=expected_old, expected_new=expected_new,
        )
        if verification.get("error_code") is not None:
            return verification
        if verification.get("status") in (
            CORRECTION_DEDUPLICATED, CORRECTION_CONFLICT,
        ):
            # The readback observed an INTERMEDIATE state of a concurrent
            # correction of this target and the verdict was decided from the
            # committed edge (idempotent replay or conflict). That decision is
            # the answer — it must not be relabelled CORRECTION_COMMITTED.
            return verification

        # 10) post-commit embedding — warning only, never a revert, and
        #     never for a withdrawal (there is no new content to embed)
        embed_warnings: list[str] = []
        embed_error: Optional[str] = None
        if not withdrawing:
            embed_warnings, embed_error = self._maybe_embed(
                mid=new_id, content=request["replacement_content"], embedder=None,
            )

        status = (
            CORRECTION_DERIVED_WARNING if embed_error else CORRECTION_COMMITTED
        )

        # M03: after the correction is COMMITTED and verified, propagate it into
        # the derived layer once — bounded, no LLM. This is strictly AFTER the
        # point of no return: a propagation failure can never undo A -> B, and
        # is reported honestly as ``derived_propagation='pending'`` alongside the
        # propagation receipt so the caller sees "correction committed, derived
        # layer not caught up yet".
        #
        # The trigger is scoped to the M03 precondition: A must declare a
        # canonical source reference. Without one the resolver can only report
        # ``NO_SOURCE`` (nothing is mappable), so the verdict is the truthful
        # no-op ``none`` — reached without touching the correction transaction's
        # lease/readback discipline.
        derived_propagation, derived_receipt = (
            self._maybe_propagate_after_correction(
                correction_id, expected_old.get("provenance"),
            )
        )

        return {
            "success": True,
            "status": status,
            "mode": mode,
            "durable": True,
            "deduplicated": False,
            "old_memory_id": target,
            "new_memory_id": new_id,
            # ACTUAL committed facts from the verification snapshot — never a
            # hardcoded "active"/new_id that would relabel an archived B or
            # hide a legitimate concurrent successor.
            "old_status": verification["old_status"],
            "new_status": verification["new_status"],
            "current_memory_id": verification["current_memory_id"],
            "relation": verification["relation"],
            "correction_source": _correction_source_view(
                EXPLICIT_TOOL_REQUEST_PROVENANCE, source_id,
            ),
            "warnings": embed_warnings,
            "error": embed_error,
            "derived_propagation": derived_propagation,
            "derived_receipt": derived_receipt,
        }

    def _maybe_propagate_after_correction(
        self, correction_id: str, source_provenance: Any
    ) -> tuple[str, dict]:
        """Run M03 deterministic invalidation for a COMMITTED correction.

        Never raises and never rolls back the correction. When the corrected
        memory declares no M03 source reference there is provably nothing to map
        (the resolver would return ``NO_SOURCE``), so the honest no-op verdict
        ``none`` is reported without a round-trip. Otherwise a failure is turned
        into ``derived_propagation='pending'`` plus a truthful receipt.
        """
        from . import derived_invalidation as _di

        prov = source_provenance
        if isinstance(prov, str):
            try:
                prov = json.loads(prov)
            except ValueError:
                prov = {}
        if not _source_prov_declared(prov):
            return _di.DERIVED_PROPAGATION_NONE, {
                "status": _di.PROPAGATION_NO_LINEAGE,
                "success": True,
                "correction_id": correction_id,
                "resolution_status": _di.NO_SOURCE,
                "derived_propagation": _di.DERIVED_PROPAGATION_NONE,
                "error": None,
            }

        pg = self._pool if self._pool is not None else self._pg
        try:
            receipt = _di.propagate_correction(pg, correction_id=correction_id)
        except Exception as exc:  # noqa: BLE001 - derived fault, correction stays
            logger.warning(
                "M03 derived propagation failed for %s (correction stays durable): %r",
                correction_id, exc,
            )
            return _di.DERIVED_PROPAGATION_PENDING, {
                "status": _di.PROPAGATION_FAILED,
                "success": False,
                "correction_id": correction_id,
                "derived_propagation": _di.DERIVED_PROPAGATION_PENDING,
                "error": f"derived propagation raised: {exc!r}",
            }
        if not isinstance(receipt, dict):
            return _di.DERIVED_PROPAGATION_PENDING, {
                "status": _di.PROPAGATION_FAILED,
                "success": False,
                "correction_id": correction_id,
                "derived_propagation": _di.DERIVED_PROPAGATION_PENDING,
                "error": "derived propagation returned an unusable receipt",
            }
        value = receipt.get("derived_propagation") or _di.DERIVED_PROPAGATION_PENDING
        return value, receipt


    # ── M01 internals ────────────────────────────────────────────────

    def _resolve_existing_edge(
        self,
        conn: Any,
        cur: Any,
        target: str,
        existing_row: tuple,
        request: dict[str, Any],
        correction_id: str,
        request_json: str,
        mode: str,
    ) -> dict[str, Any]:
        """Target already has a successor: same request = retry, else conflict.

        An existing edge is NEVER trusted on its hash alone. The recorded
        canonical request is compared against the ACTUAL normalized request
        and the chain is followed to its real terminal version, so:

          * an exact retry still reports ITS OWN new_memory_id — with that
            row's ACTUAL status, which may no longer be 'active';
          * the reported current_memory_id is the chain's true terminal
            successor (possibly further along, possibly NONE after a
            withdrawal), never the stale first successor;
          * a cycle in the recorded chain fails closed instead of reporting
            a fabricated answer.
        """
        edge = _relation_row_to_dict(existing_row)
        recorded_request = edge.get("request")
        if not isinstance(recorded_request, dict):
            recorded_request = None
        same_request = (
            edge.get("correction_id") == correction_id
            and recorded_request == request
        )
        successor = edge.get("to_memory_id")

        actual_new_status, current_id, cycle = self._read_chain_facts(
            conn, cur, successor,
        )
        if cycle is not None:
            return _correction_failure(
                CORRECTION_CONFLICT, target, successor, mode, cycle,
            )

        if same_request:
            return {
                "success": True,
                "status": CORRECTION_DEDUPLICATED,
                "mode": mode,
                "durable": True,
                "deduplicated": True,
                "old_memory_id": target,
                "new_memory_id": successor,
                "old_status": "archived",
                "new_status": actual_new_status,
                "current_memory_id": current_id,
                "relation": edge,
                "correction_source": _correction_source_view(
                    edge["provenance"], edge["correction_source_id"],
                ),
                "warnings": [],
                "error": None,
            }
        return {
            "success": False,
            "status": CORRECTION_CONFLICT,
            "mode": mode,
            "durable": False,
            "deduplicated": False,
            "error_code": CORRECTION_CONFLICT,
            "error": (
                f"{target} 已被纠正为 {successor}；本次请求不同，拒绝建立第二个"
                f"当前版本。请对当前版本 {current_id} 发起纠正。"
                if current_id is not None else
                f"{target} 已被纠正为 {successor} 并撤回；本次请求不同，"
                f"拒绝为已撤回的记忆建立新版本。"
            ),
            "old_memory_id": target,
            "new_memory_id": None,
            "old_status": "archived",
            "new_status": None,
            "current_memory_id": current_id,
            "relation": edge,
            "correction_source": _correction_source_view(
                edge["provenance"], edge["correction_source_id"],
            ),
            "warnings": [],
        }

    def _resolve_from_committed_edge(
        self,
        target: str,
        request: dict[str, Any],
        correction_id: str,
        request_json: str,
        mode: str,
        *,
        reason: str,
    ) -> tuple[Optional[dict[str, Any]], str]:
        """Decide an already-committed correction from the COMMITTED state.

        Used when a concurrent correction of the same target won the race for
        the target's single outgoing edge, so OUR write was refused: the
        truthful verdict is the one already recorded by the winner. Re-reads
        the committed edge on a FRESH lease (never nested inside a held one)
        and applies the SAME deterministic decision the sequential retry path
        uses:

          * the committed edge IS our request → idempotent replay
            (CORRECTION_DEDUPLICATED, durable=True, deduplicated=True, with
            the winner's replacement id and the chain's real current);
          * the committed edge is a DIFFERENT request → CORRECTION_CONFLICT
            pointing at the real current version;
          * nothing is committed → ``(None, reason)``: the write really failed
            and the caller reports exactly that, never a fabricated verdict.

        Returns ``(receipt_or_None, reason)``.
        """
        try:
            lease = _acquire_lease(self._pool, self._pg)
        except Exception as exc:
            return None, f"{reason}；且无法重新读取已提交状态: {exc!r}"
        conn: Any = None
        try:
            try:
                conn = lease.connection
                cur = conn.cursor()
                cur.execute(
                    f"SELECT {', '.join(_RELATION_COLUMNS)} "
                    f"FROM {_RELATION_TABLE} WHERE from_memory_id = %s",
                    (target,),
                )
                committed_edge = cur.fetchone()
                if committed_edge is None:
                    return None, reason
                return self._resolve_existing_edge(
                    conn, cur, target, committed_edge,
                    request, correction_id, request_json, mode,
                ), reason
            finally:
                # Never leave this fresh lease's implicit transaction open.
                if conn is not None:
                    try:
                        _end_open_transaction(conn)
                    except Exception as exc:  # pragma: no cover
                        logger.warning("纠正 race 读取事务清理失败: %r", exc)
                lease.close()
        except Exception as exc:
            return None, f"{reason}；重新读取已提交状态失败: {exc!r}"

    def _read_chain_facts(
        self, conn: Any, cur: Any, start: Optional[str],
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """``(status_of_start, terminal_current_id, cycle_error)`` on ``conn``.

        Walks the recorded forward chain on the caller's still-open
        connection, so no second lease is taken while one is held. A cycle or
        an edge whose successor row is missing fails closed with a message
        instead of silently breaking the loop.
        """
        if start is None:
            return None, None, None
        status: Optional[str] = None
        seen: set[str] = set()
        cursor: Optional[str] = start
        terminal: Optional[str] = start
        terminal_status: Optional[str] = None
        while cursor is not None:
            if cursor in seen:
                return (status, None, (
                    f"纠正链出现环（重复版本 {cursor}）；拒绝基于损坏的链做幂等判定"
                ))
            seen.add(cursor)
            cur.execute(
                f"SELECT {', '.join(_MEMORY_COLUMNS)} "
                f"FROM {_TABLE} WHERE memory_id = %s", (cursor,),
            )
            row = cur.fetchone()
            if row is None:
                return (status, None, (
                    f"纠正链不完整：版本 {cursor} 不存在；拒绝基于损坏的链做幂等判定"
                ))
            if cursor == start:
                status = row[_MEMORY_COLUMNS.index("status")]
            terminal = cursor
            terminal_status = row[_MEMORY_COLUMNS.index("status")]
            cur.execute(
                f"SELECT {', '.join(_RELATION_COLUMNS)} "
                f"FROM {_RELATION_TABLE} WHERE from_memory_id = %s",
                (cursor,),
            )
            nxt = cur.fetchone()
            cursor = (
                nxt[_RELATION_COLUMNS.index("to_memory_id")]
                if nxt is not None else None
            )
        # the CURRENT version is the chain's real terminal, and only while it
        # is genuinely active — None after a withdrawal, not the first successor
        current = terminal if terminal_status == "active" else None
        return (status, current, None)

    def _verify_correction(
        self,
        target: str,
        new_id: Optional[str],
        relation_id: str,
        request: dict[str, Any],
        mode: str,
        expected_old: Optional[dict[str, Any]] = None,
        expected_new: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Fresh-lease verification of the committed chain.

        Returns ``{"error_code": None, "relation": <actual edge>,
        "old_status", "new_status", "current_memory_id", "current_status"}``
        when the committed state matches what was intended, and an explicit
        verification FAILURE receipt otherwise.

        A third shape exists for the CONCURRENT case: when the readback
        observes the target row still NOT archived (an intermediate state of a
        race with another correction of the same target), the committed
        outgoing edge is re-read on a fresh lease and the deterministic
        idempotency verdict is returned instead — ``CORRECTION_DEDUPLICATED``
        for our own request, ``CORRECTION_CONFLICT`` for a divergent one. A
        readback race is never reported as a durability failure.

        All of those facts come from ONE fresh, read-only REPEATABLE READ
        snapshot, so the payload/edge and the chain terminal can never be read
        from two different database states.

        A failure HERE happens AFTER the commit: the write is durable, so the
        receipt says ``durable=True`` with an explicit verification error
        rather than pretending the transaction was rolled back. Only an
        indeterminate COMMIT itself is unknown-durability.

        Verification compares the full expected old immutable fields and the
        full new canonical payload plus the whole edge against actual reads.
        A replacement that a legitimate concurrent follow-up has since
        superseded is NOT a failure: the actual status and current id are
        reported instead of falsely claiming the row is still active.
        """
        withdrawing = mode == CORRECTION_MODE_WITHDRAW
        chain: Optional[dict[str, Any]] = None
        try:
            # ONE fresh, read-only REPEATABLE READ snapshot covers the whole
            # readback: the committed payload, the edge AND the chain terminal
            # are read from a single coherent database state, so a writer
            # committing mid-verification can never splice two states into one
            # answer. ``_read_snapshot`` closes the read-only transaction
            # (rollback) on BOTH the success and the error path BEFORE the
            # lease is released — it never relies on a pool reset, which the
            # legacy no-pool PgEmbedStore.lease path does not perform.
            with _read_snapshot(self._pool, self._pg) as snap:
                old = snap.read_one(target)
                new = (
                    snap.read_one(new_id)
                    if (not withdrawing and new_id is not None) else None
                )
                actual_edge = snap.read_edge_by_relation(relation_id, target)
                if (not withdrawing) and new is not None and (
                    new.get("status") != "active"
                ):
                    # A legitimate concurrent follow-up may have superseded the
                    # row we just wrote. Resolve the FULL terminal on the SAME
                    # snapshot: a longer chain and a withdrawal-to-NULL are both
                    # legitimate, and the real status/current is reported.
                    chain = snap.read_chain(new_id)
        except _ChainCorrupt as exc:
            return _post_commit_failure(
                target, new_id, mode, f"纠正已提交但 readback 链损坏: {exc}",
            )
        except Exception as exc:
            code = (ERR_MIGRATION_REQUIRED if _is_missing_relation_table(exc)
                    else ERR_DURABLE_FAILED)
            return _post_commit_failure(
                target, new_id, mode, f"纠正已提交但 readback 失败: {exc!r}",
                code=code,
            )

        if old is None or old.get("status") != "archived":
            readback_msg = (
                f"纠正已提交但 readback 显示 {target} status="
                f"{(old or {}).get('status')!r}（期望 archived）"
            )
            if old is not None:
                # The target row EXISTS but does not read as archived. A
                # concurrent correction of the same target can commit between
                # our write and this read, so this may be an INTERMEDIATE
                # state of that race — not a durability failure. Re-read the
                # committed outgoing edge on a fresh lease and decide with the
                # same deterministic idempotency logic (idempotent replay vs
                # conflict) instead of reporting a readback race as a failure.
                resolved, _why = self._resolve_from_committed_edge(
                    target, request, derive_correction_id(target, request),
                    _canonical_json(request), mode, reason=readback_msg,
                )
                if resolved is not None:
                    return resolved
            return _post_commit_failure(target, new_id, mode, readback_msg)
        # the old payload must be exactly what it was — a correction is not
        # allowed to have rewritten any canonical field of the old row
        for col, expected in (expected_old or {}).items():
            if old.get(col) != expected:
                return _post_commit_failure(
                    target, new_id, mode,
                    f"纠正已提交但 readback 显示旧版本 {target} 的 {col}="
                    f"{old.get(col)!r}（期望 {expected!r}；纠正不得改写旧 payload）",
                )
        if not withdrawing:
            if new is None:
                return _post_commit_failure(
                    target, new_id, mode,
                    f"纠正已提交但 readback 未找到替代版本 {new_id}",
                )
            for col, expected in (expected_new or {}).items():
                if new.get(col) != expected:
                    return _post_commit_failure(
                        target, new_id, mode,
                        f"纠正已提交但 readback 显示替代版本 {new_id} 的 {col}="
                        f"{new.get(col)!r}（期望 {expected!r}）",
                    )
            if new.get("status") != "active":
                has_successor_edge = chain is not None and any(
                    e.get("from_memory_id") == new_id for e in chain["edges"]
                )
                if not has_successor_edge:
                    return _post_commit_failure(
                        target, new_id, mode,
                        f"纠正已提交但 readback 显示 {new_id} status="
                        f"{new.get('status')!r}（期望 active）且无法解释",
                    )
        if actual_edge is None:
            return _post_commit_failure(
                target, new_id, mode,
                f"纠正已提交但 readback 未找到关系 {relation_id}",
            )

        expected_edge = {
            "relation_id": relation_id,
            "relation_type": CORRECTION_MODE_RELATION_TYPE[mode],
            "from_memory_id": target,
            "to_memory_id": new_id,
            "correction_id": actual_edge.get("correction_id"),
            "authority": REQUIRED_CORRECTION_AUTHORITY,
            "correction_reason": request.get("correction_reason"),
            "correction_source_id": request.get("correction_source_id"),
            "provenance": EXPLICIT_TOOL_REQUEST_PROVENANCE,
            "effective_at": request.get("effective_at"),
            "request": request,
        }
        if expected_edge["correction_id"] != derive_correction_id(
            target, request
        ):
            return _post_commit_failure(
                target, new_id, mode,
                f"纠正已提交但 readback 的 correction_id "
                f"{expected_edge['correction_id']!r} 与本次请求不符",
            )
        for col, expected in expected_edge.items():
            if col == "recorded_at":
                continue  # server-side; returned AS READ, never fabricated
            if actual_edge.get(col) != expected:
                return _post_commit_failure(
                    target, new_id, mode,
                    f"纠正已提交但 readback 的关系 {col}="
                    f"{actual_edge.get(col)!r}（期望 {expected!r}）",
                )
        if actual_edge.get("recorded_at") is None:
            return _post_commit_failure(
                target, new_id, mode,
                f"纠正已提交但 readback 的关系 {relation_id} 缺少 recorded_at",
            )

        # The ACTUAL committed facts, read from the one coherent snapshot.
        if withdrawing:
            new_status: Optional[str] = None
            current_id: Optional[str] = None
            current_status: Optional[str] = "withdrawn"
        else:
            new_status = new.get("status")
            if new_status == "active":
                current_id = new_id
                current_status = "active"
            else:
                current_id = chain["current_memory_id"] if chain else None
                current_status = chain["current_status"] if chain else None
        return {
            "error_code": None,
            "relation": actual_edge,
            "old_status": old.get("status"),
            "new_status": new_status,
            "current_memory_id": current_id,
            "current_status": current_status,
        }

    # ── internals ─────────────────────────────────────────────────────────

    def _maybe_embed(
        self,
        *,
        mid: str,
        content: str,
        embedder: Optional[Callable[[str, dict], list[float]]],
    ) -> tuple[list[str], Optional[str]]:
        """Post-commit embedding; only after canonical durable readback.

        The canonical row is already durable at this point — any failure
        here (config resolution, embedder backend, vector conversion, model
        fingerprint, or the post-commit UPDATE) MUST be surfaced as a
        warning/error tuple and never propagate as an exception.
        """
        # Config resolution is the one step that historically raised out
        # of the helper; swallow anything it can throw here so a broken
        # config can never invalidate an already-durable row.
        try:
            cfg = (
                self._embed_cfg_value
                if self._embed_cfg_explicit
                else _resolve_embed_cfg(self._config)
            )
        except Exception as exc:
            msg = f"safe_embed_cfg 解析失败: {exc!r}"
            return [msg], msg
        if cfg is None:
            return [], None

        caller_embedder = embedder if embedder is not None else self._embedder

        try:
            vector = caller_embedder(content, cfg)
        except Exception as exc:
            msg = f"embedding 调用失败: {exc!r}"
            return [msg], msg
        if not vector:
            msg = "embedder 返回空向量"
            return [msg], msg

        try:
            model = _resolve_model(cfg)
        except Exception as exc:
            return [f"resolve_model 失败: {exc!r}"], f"resolve_model 失败: {exc!r}"

        try:
            lease = _acquire_lease(self._pool, self._pg)
        except Exception as exc:
            msg = f"embedding lease 失败: {exc!r}"
            return [msg], msg

        warning_text: Optional[str] = None
        try:
            try:
                cur = lease.connection.cursor()
                cur.execute(
                    f"""
                    UPDATE {_TABLE}
                       SET embedding = %s::vector,
                           embed_model = %s
                     WHERE memory_id = %s
                    """,
                    (_emb_str(vector), model, mid),
                )
                lease.connection.commit()
                # rowcount may be None (driver doesn't report) or -1 ("no statement
                # count", e.g. some SQLite/async configurations); accept None/-1/1
                # and only warn on any other confirmed count.
                rowcount = getattr(cur, "rowcount", None)
                if rowcount is not None and rowcount not in (-1, 1):
                    warning_text = (
                        f"embedding UPDATE rowcount={rowcount} (期望 1)；"
                        f"行可能已被并发 archive；canonical 行不受影响"
                    )
            finally:
                lease.close()
        except Exception as exc:
            msg = f"embedding UPDATE 失败: {exc!r}"
            return [msg], msg

        if warning_text:
            return [warning_text], warning_text
        return [], None


def _failed(
    memory_id: str,
    source_id: str,
    msg: str,
    record: Optional[dict[str, Any]] = None,
) -> MemoryWriteResult:
    return MemoryWriteResult(
        memory_id=memory_id,
        source_id=source_id if source_id else memory_id,
        durable=False,
        status=DURABLE_FAILED,
        warnings=[msg],
        error=msg,
        record=record,
    )


def _correction_failure(
    code: str,
    old_memory_id: str,
    new_memory_id: Optional[str],
    mode: Optional[str] = None,
    msg: Optional[str] = None,
) -> dict[str, Any]:
    """Pre-commit failure: nothing was written, so durable is False.

    ``msg`` is positional-last only for backwards compatibility with older
    call shapes; new callers pass ``(code, target, new_id, mode, msg)``.
    """
    if msg is None:  # legacy shape: (code, old_memory_id, new_memory_id, msg)
        mode, msg = None, mode
    return {
        "success": False,
        "status": code,
        "mode": mode,
        "durable": False,
        "deduplicated": False,
        "error_code": code,
        "error": msg,
        "old_memory_id": old_memory_id,
        "new_memory_id": new_memory_id,
        "old_status": None,
        "new_status": None,
        "current_memory_id": None,
        "relation": None,
        "correction_source": _correction_source_view(
            EXPLICIT_TOOL_REQUEST_PROVENANCE, None,
        ),
        "warnings": [msg] if msg else [],
    }


def _post_commit_failure(
    old_memory_id: str,
    new_memory_id: Optional[str],
    mode: str,
    msg: str,
    *,
    code: str = ERR_DURABLE_FAILED,
) -> dict[str, Any]:
    """Verification failed AFTER the transaction committed.

    The write IS durable — the commit already returned — so this must never
    be reported as a rolled-back correction. ``durable=True`` with an
    explicit verification error is the truthful state; only an indeterminate
    COMMIT itself would be unknown-durability.
    """
    return {
        "success": False,
        "status": code,
        "mode": mode,
        "durable": True,
        "deduplicated": False,
        "error_code": code,
        "error": msg,
        "verification_failed": True,
        "old_memory_id": old_memory_id,
        "new_memory_id": new_memory_id,
        "old_status": "archived",
        "new_status": None,
        "current_memory_id": None,
        "relation": None,
        "correction_source": _correction_source_view(
            EXPLICIT_TOOL_REQUEST_PROVENANCE, None,
        ),
        "warnings": [msg],
    }


def _correction_indeterminate(
    old_memory_id: str,
    new_memory_id: Optional[str],
    mode: Optional[str],
    msg: str,
) -> dict[str, Any]:
    """The COMMIT was ATTEMPTED and its outcome is unknown.

    A transport error around ``commit()`` can mean either "the server never
    committed" or "the server committed but the acknowledgement was lost".
    The two are indistinguishable, so this is NOT reported as durable=False
    (that would fabricate a rollback we never performed) and NOT as durable
    True (that would claim a persistence we never confirmed): ``durable`` is
    ``None`` with an explicit error, and the caller must re-read to decide.
    """
    return {
        "success": False,
        "status": ERR_DURABLE_FAILED,
        "mode": mode,
        "durable": None,
        "deduplicated": False,
        "error_code": ERR_DURABLE_FAILED,
        "error": msg,
        "commit_indeterminate": True,
        "old_memory_id": old_memory_id,
        "new_memory_id": new_memory_id,
        "old_status": None,
        "new_status": None,
        "current_memory_id": None,
        "relation": None,
        "correction_source": _correction_source_view(
            EXPLICIT_TOOL_REQUEST_PROVENANCE, None,
        ),
        "warnings": [msg],
    }


def _correction_not_found(memory_id: str) -> dict[str, Any]:
    return _correction_failure(
        ERR_NOT_FOUND, memory_id, None,
        f"{memory_id} 不在 public.explicit_memories 中；纠正只作用于已知记忆",
    )


def _correction_source_view(
    provenance: Optional[str], correction_source_id: Optional[str],
) -> dict[str, Any]:
    """Truthful provenance block.

    ``host`` / ``session_id`` / ``event_id`` are always null in M01: the writer
    stores a real ``conversation_stream.id`` when one was supplied and refuses
    to invent a host/event/QA reference when one was not.
    """
    return {
        "provenance": provenance or EXPLICIT_TOOL_REQUEST_PROVENANCE,
        "correction_source_id": correction_source_id,
        "host": None,
        "session_id": None,
        "event_id": None,
    }


def _read_version_error(
    code: str, memory_id: str, msg: str,
) -> dict[str, Any]:
    return {
        "success": False,
        "status": code,
        "error_code": code,
        "error": msg,
        "mode": None,
        "memory_id": memory_id,
        "requested": None,
        "resolved_memory_id": None,
        "is_current": False,
        "memory": None,
        "relation": None,
    }


def _relation_row_to_dict(row: Any) -> dict[str, Any]:
    """Normalize one relation row (tuple or mapping) into a plain dict."""
    if isinstance(row, dict):
        raw = dict(row)
    else:
        raw = dict(zip(_RELATION_COLUMNS, row))

    request = raw.get("request")
    if isinstance(request, str):
        try:
            request = json.loads(request)
        except ValueError:
            pass

    recorded = raw.get("recorded_at")
    effective = raw.get("effective_at")
    return {
        "relation_id": raw.get("relation_id"),
        "relation_type": raw.get("relation_type"),
        "from_memory_id": raw.get("from_memory_id"),
        "to_memory_id": raw.get("to_memory_id"),
        "correction_id": raw.get("correction_id"),
        "authority": raw.get("authority"),
        "correction_reason": raw.get("correction_reason"),
        "correction_source_id": raw.get("correction_source_id"),
        "provenance": raw.get("provenance"),
        "effective_at": effective.isoformat() if hasattr(effective, "isoformat")
        else effective,
        "recorded_at": recorded.isoformat() if hasattr(recorded, "isoformat")
        else recorded,
        "request": request,
        "correction_source": _correction_source_view(
            raw.get("provenance"), raw.get("correction_source_id"),
        ),
    }


# ── reader ─────────────────────────────────────────────────────────────────


class ActiveMemoryReader:
    """Read-side counterpart. Filters ``status='active'`` only."""

    def __init__(
        self,
        pool: Any = None,
        pg: Any = None,
        deadline: Any = None,
    ) -> None:
        if pool is None and pg is None:
            raise _PoolUnavailable(
                "ActiveMemoryReader requires an injected pool or pg."
            )
        self._pool = pool
        self._pg = pg
        self._deadline = deadline

    def search_keyword(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Title / content / tags ILIKE match; status='active' only."""
        if not query:
            return []
        limit = max(1, min(int(limit), 200))
        like = f"%{query}%"
        try:
            lease = _acquire_lease(self._pool, self._pg, deadline=self._deadline)
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            logger.warning("search_keyword lease failed: %s", exc)
            return []
        try:
            try:
                cur = lease.connection.cursor()
                cur.execute(
                    f"""
                    SELECT memory_id, category, title, content, tags,
                           provenance, status, created_at, updated_at,
                           embedding, embed_model
                      FROM {_TABLE}
                     WHERE status = 'active'
                       AND (title ILIKE %s
                            OR content ILIKE %s
                            OR EXISTS (
                                SELECT 1 FROM unnest(COALESCE(tags, ARRAY[]::text[])) AS t
                                 WHERE t ILIKE %s
                            ))
                     ORDER BY created_at DESC
                     LIMIT %s
                    """,
                    (like, like, like, limit),
                )
                rows = cur.fetchall()
            finally:
                lease.close()
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            logger.warning("search_keyword SQL failed: %s", exc)
            return []
        return [_row_to_dict(r) for r in rows]

    def search_vector(self, q_emb: Iterable[float], limit: int = 20) -> list[dict[str, Any]]:
        """Cosine-distance ANN; status='active' AND embedding IS NOT NULL."""
        vec = list(q_emb) if q_emb is not None else []
        if not vec:
            return []
        limit = max(1, min(int(limit), 200))
        emb = _emb_str(vec)
        try:
            lease = _acquire_lease(self._pool, self._pg, deadline=self._deadline)
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            logger.warning("search_vector lease failed: %s", exc)
            return []
        try:
            try:
                cur = lease.connection.cursor()
                cur.execute(
                    f"""
                    SELECT memory_id, category, title, content, tags,
                           provenance, status, created_at, updated_at,
                           embedding, embed_model,
                           1 - (embedding <=> %s::vector) AS cosine
                      FROM {_TABLE}
                     WHERE status = 'active'
                       AND embedding IS NOT NULL
                     ORDER BY embedding <=> %s::vector
                     LIMIT %s
                    """,
                    (emb, emb, limit),
                )
                rows = cur.fetchall()
            finally:
                lease.close()
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            logger.warning("search_vector SQL failed: %s", exc)
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            row = _row_to_dict(r)
            try:
                row["cosine"] = float(r[-1])
            except (TypeError, ValueError):
                row["cosine"] = 0.0
            out.append(row)
        return out

    def get_by_memory_id(self, memory_id: str) -> Optional[dict[str, Any]]:
        """One row (any status) or None — tests/lab only."""
        mid = memory_id if isinstance(memory_id, str) and memory_id else ""
        if not mid:
            return None
        try:
            lease = _acquire_lease(self._pool, self._pg, deadline=self._deadline)
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            logger.warning("get_by_memory_id lease failed: %s", exc)
            return None
        try:
            try:
                cur = lease.connection.cursor()
                cur.execute(
                    f"""
                    SELECT memory_id, category, title, content, tags,
                           provenance, status, created_at, updated_at,
                           embedding, embed_model
                      FROM {_TABLE}
                     WHERE memory_id = %s
                    """,
                    (mid,),
                )
                row = cur.fetchone()
            finally:
                lease.close()
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            logger.warning("get_by_memory_id SQL failed: %s", exc)
            return None
        return _row_to_dict(row) if row else None

    # ── M01: canonical version read ───────────────────────────────────

    def read_version(
        self,
        memory_id: str,
        *,
        mode: str = "current",
        include_history: bool = False,
    ) -> dict[str, Any]:
        """Read one memory through its correction chain (M01 public API).

        ``mode='current'``
            Follows replacement edges and returns the terminal active
            version, plus the requested id, the resolved id and is_current.
            A chain that ENDS in a withdrawal — or a row that is merely
            archived with no successor edge at all — has NO current version,
            so ``memory`` is None and ``resolved_memory_id`` is None. An
            archived payload is never labelled current just because no
            successor happens to exist.
        ``mode='history'``
            Returns the requested ORIGINAL payload/status as ``memory`` plus
            the full ordered version and edge history — ancestors AND
            descendants — each version labelled ``current`` / ``superseded``
            / ``withdrawn`` / ``archived`` explicitly.

        ``include_history=True`` attaches the history to a current-mode read.
        Payloads are returned in full — a superseded payload is never
        truncated and never silently substituted for the current one.

        The whole walk runs on ONE lease inside a read-only REPEATABLE READ
        transaction, so a writer committing mid-walk can never mix two
        database states into one answer.

        Unknown ids fail; a missing ``memory_relations`` table or any DB error
        fails truthfully. A cycle or a chain with a missing endpoint fails
        closed. This does NOT change ``get_by_memory_id``, which keeps its
        exact single-row historical semantics.
        """
        mid = memory_id if isinstance(memory_id, str) else ""
        if not mid.strip():
            return _read_version_error(
                ERR_INVALID_REQUEST, mid, "memory_id 必须为非空字符串")
        # A whitespace-padded id is NOT stripped: the literal-preservation
        # contract forbids resolving a different target than the caller named,
        # so a padded id is simply looked up literally and reported NOT_FOUND.
        if mode not in ("current", "history"):
            return _read_version_error(
                ERR_INVALID_REQUEST, mid,
                f"mode 必须为 'current' 或 'history'（收到 {mode!r}）",
            )
        if not isinstance(include_history, bool):
            return _read_version_error(
                ERR_INVALID_REQUEST, mid, "include_history 必须为 bool",
            )

        want_history = include_history or mode == "history"

        try:
            with _read_snapshot(self._pool, self._pg,
                                deadline=self._deadline) as snap:
                requested = snap.read_one(mid)
                if requested is None:
                    raise _ReadNotFound(mid)
                outgoing = snap.read_edge_by_from(mid)
                chain = snap.read_chain(mid)
        except _ReadNotFound:
            return _read_version_error(
                ERR_NOT_FOUND, mid,
                f"{mid} 不在 public.explicit_memories 中",
            )
        except _ChainCorrupt as exc:
            return _read_version_error(
                ERR_DURABLE_FAILED, mid, str(exc),
            )
        except Exception as exc:
            if _is_missing_relation_table(exc):
                return _read_version_error(
                    ERR_MIGRATION_REQUIRED, mid,
                    f"{_RELATION_TABLE} 不存在；请先应用 "
                    f"schema/memory_relations.sql 迁移（{exc}）",
                )
            return _read_version_error(
                ERR_DURABLE_FAILED, mid, f"读取 {mid} 的纠正链失败: {exc!r}",
            )

        if chain is None:  # pragma: no cover - read_chain raises instead
            return _read_version_error(
                ERR_DURABLE_FAILED, mid,
                f"{mid} 的纠正链损坏（成环或端点缺失）；拒绝给出可能错误的当前版本",
            )

        current_id = chain["current_memory_id"]
        current = chain["current_row"]

        if want_history:
            # mode='history' answers with the EXACT requested payload; a
            # current-mode read that merely attaches history keeps answering
            # with the CURRENT version.
            answered = requested if mode == "history" else current
            return {
                "success": True,
                "status": "OK",
                "error_code": None,
                "error": None,
                "mode": mode,
                "memory_id": mid,
                # the attached `current` never overwrites the requested payload
                "memory": answered,
                "requested": requested,
                "current": current,
                "resolved_memory_id": current_id,
                "is_current": current_id == mid,
                "current_status": chain["current_status"],
                "relation": outgoing,
                "history": {
                    "versions": chain["versions"],
                    "edges": chain["edges"],
                    "current_memory_id": current_id,
                },
            }

        return {
            "success": True,
            "status": "OK",
            "error_code": None,
            "error": None,
            "mode": mode,
            "memory_id": mid,
            "requested": requested,
            # None whenever nothing is current (withdrawn terminal, or an
            # archived row with no successor) — never the archived payload.
            "memory": current,
            "resolved_memory_id": current_id,
            "is_current": current_id == mid,
            "current_status": chain["current_status"],
            "relation": outgoing,
        }

    # ── M01 reader internals ─────────────────────────────────────────

    def _read_one(self, memory_id: str) -> Optional[dict[str, Any]]:
        """Exact single-row read on its own lease. Kept for existing callers."""
        with _read_snapshot(self._pool, self._pg,
                            deadline=self._deadline) as snap:
            return snap.read_one(memory_id)

    def _read_edge_by_from(self, memory_id: str) -> Optional[dict[str, Any]]:
        """Outgoing edge read on its own lease. Kept for existing callers."""
        with _read_snapshot(self._pool, self._pg,
                            deadline=self._deadline) as snap:
            return snap.read_edge_by_from(memory_id)


class _ReadNotFound(Exception):
    """The requested memory row does not exist at all."""

    def __init__(self, memory_id: str) -> None:
        super().__init__(memory_id)
        self.memory_id = memory_id


class _ChainCorrupt(Exception):
    """The recorded chain is a cycle or has a missing endpoint.

    The unique incoming/outgoing indexes do NOT prevent a cycle, so the walk
    fails closed here instead of breaking the loop and reporting a
    plausible-but-wrong current version.
    """


class _SnapshotCleanupFailed(RuntimeError):
    """A read-only snapshot transaction could not be ended before release."""


class _ReadSnapshot:
    """One leased, read-only REPEATABLE READ view of the chain tables.

    Every query of a single ``read_version`` runs through ONE of these, so a
    writer that commits between two of our reads can never splice two
    database states into one answer. REPEATABLE READ gives the whole walk a
    single coherent snapshot; READ ONLY makes an accidental write impossible.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._cur = conn.cursor()
        self._closed = False
        # psycopg2 does not send "BEGIN ISOLATION LEVEL ..." as one server
        # statement while it manages the transaction: it issues BEGIN and then
        # SET TRANSACTION ISOLATION LEVEL. PostgreSQL refuses that second
        # statement ("must be called before any query") once any query has
        # already run in the open transaction. Whether a transaction is
        # REALLY open must be decided from the driver's real transaction
        # status, never from the autocommit flag alone: a connection can be
        # autocommit=True and still carry a transaction opened by an explicit
        # BEGIN, where rollback() is a silent no-op. End any really-open
        # transaction first so the read-only REPEATABLE READ snapshot opens.
        _end_open_transaction(conn)
        self._cur.execute(
            "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )

    def close(self) -> None:
        """End the read-only transaction (rollback) exactly once.

        A failed rollback is NOT swallowed: a read-only transaction left open
        on a shared connection would leak into the next operation, and
        reporting success while it is still open would be a lie. The failure
        is surfaced (wrapped) so callers report an explicit failure. The
        transaction is ended only when it is REALLY open (real transaction
        status), with a mechanism that works in autocommit mode.
        """
        if self._closed:
            return
        self._closed = True
        try:
            _end_open_transaction(self._conn)
        except Exception as exc:
            raise _SnapshotCleanupFailed(
                f"read-only snapshot rollback failed: {exc!r}"
            ) from exc

    def __enter__(self) -> "_ReadSnapshot":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def read_one(self, memory_id: str) -> Optional[dict[str, Any]]:
        self._cur.execute(
            f"""
            SELECT {", ".join(_MEMORY_COLUMNS)}
              FROM {_TABLE}
             WHERE memory_id = %s
            """,
            (memory_id,),
        )
        row = self._cur.fetchone()
        return _row_to_dict(row) if row else None

    def read_edge_by_from(self, memory_id: str) -> Optional[dict[str, Any]]:
        self._cur.execute(
            f"""
            SELECT {", ".join(_RELATION_COLUMNS)}
              FROM {_RELATION_TABLE}
             WHERE from_memory_id = %s
            """,
            (memory_id,),
        )
        row = self._cur.fetchone()
        return _relation_row_to_dict(row) if row else None

    def read_edge_by_to(self, memory_id: str) -> Optional[dict[str, Any]]:
        self._cur.execute(
            f"""
            SELECT {", ".join(_RELATION_COLUMNS)}
              FROM {_RELATION_TABLE}
             WHERE to_memory_id = %s
            """,
            (memory_id,),
        )
        row = self._cur.fetchone()
        return _relation_row_to_dict(row) if row else None

    def read_edge_by_relation(
        self, relation_id: str, from_memory_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """The committed edge identified by ``relation_id``.

        ``from_memory_id`` pins the source endpoint when supplied, so a
        readback can never mistake a different edge for the one we wrote.
        """
        if from_memory_id is None:
            self._cur.execute(
                f"""
                SELECT {", ".join(_RELATION_COLUMNS)}
                  FROM {_RELATION_TABLE}
                 WHERE relation_id = %s
                """,
                (relation_id,),
            )
        else:
            self._cur.execute(
                f"""
                SELECT {", ".join(_RELATION_COLUMNS)}
                  FROM {_RELATION_TABLE}
                 WHERE relation_id = %s AND from_memory_id = %s
                """,
                (relation_id, from_memory_id),
            )
        row = self._cur.fetchone()
        return _relation_row_to_dict(row) if row else None

    def read_chain(self, memory_id: str) -> Optional[dict[str, Any]]:
        """Whole chain around ``memory_id``: ancestors, self, descendants.

        Raises ``_ChainCorrupt`` when the recorded chain is a cycle or an
        edge points at a row that does not exist. The unique indexes do NOT
        prevent a cycle, so this fails closed instead of breaking the loop
        and reporting a plausible-but-wrong current version.
        """
        incoming = self.read_edge_by_to(memory_id)

        ancestor_ids: list[str] = []
        seen: set[str] = {memory_id}
        while incoming is not None:
            prev = incoming["from_memory_id"]
            if prev in seen:
                raise _ChainCorrupt(
                    f"{memory_id} 的纠正链出现环（重复版本 {prev}）；"
                    f"拒绝给出可能错误的当前版本"
                )
            seen.add(prev)
            ancestor_ids.append(prev)
            incoming = self.read_edge_by_to(prev)

        forward_ids: list[str] = []
        seen.add(memory_id)
        cursor = memory_id
        while True:
            edge = self.read_edge_by_from(cursor)
            if edge is None:
                break
            nxt = edge["to_memory_id"]
            if nxt is None:            # a withdrawal terminates the chain
                break
            if nxt in seen:
                raise _ChainCorrupt(
                    f"{memory_id} 的纠正链出现环（重复版本 {nxt}）；"
                    f"拒绝给出可能错误的当前版本"
                )
            seen.add(nxt)
            forward_ids.append(nxt)
            cursor = nxt

        chain_ids = list(reversed(ancestor_ids)) + [memory_id] + forward_ids
        rows: dict[str, dict[str, Any]] = {}
        for vid in chain_ids:
            row = self.read_one(vid)
            if row is None:
                raise _ChainCorrupt(
                    f"{memory_id} 的纠正链不完整：关系指向的版本 {vid} 不存在；"
                    f"拒绝给出可能错误的当前版本"
                )
            rows[vid] = row

        edges = self._edges_for(chain_ids)
        terminal_id = chain_ids[-1]
        withdrawn_terminal = any(
            e.get("from_memory_id") == terminal_id
            and e.get("relation_type") == RELATION_TYPE_WITHDRAWS
            for e in edges
        )
        terminal_status = rows[terminal_id].get("status")
        if withdrawn_terminal or terminal_status != "active":
            # No current version: a withdrawal ends the chain truthfully, and
            # an archived row is never relabelled active just because it has
            # no outgoing edge.
            current_id: Optional[str] = None
            current_row: Optional[dict[str, Any]] = None
            current_status = "withdrawn" if withdrawn_terminal else "archived"
        else:
            current_id = terminal_id
            current_row = rows[terminal_id]
            current_status = "active"

        versions = []
        for vid in chain_ids:
            row = rows[vid]
            is_current = (current_id is not None and vid == current_id)
            is_withdrawn = (not is_current and any(
                e.get("from_memory_id") == vid
                and e.get("relation_type") == RELATION_TYPE_WITHDRAWS
                for e in edges
            ))
            superseded = (not is_current and not is_withdrawn and any(
                e.get("from_memory_id") == vid for e in edges
            ))
            if is_current:
                label = _VERSION_LABEL_CURRENT
            elif is_withdrawn:
                label = _VERSION_LABEL_WITHDRAWN
            elif superseded:
                label = _VERSION_LABEL_SUPERSEDED
            else:
                label = _VERSION_LABEL_ARCHIVED
            versions.append({
                "memory_id": vid,
                "label": label,
                "is_current": is_current,
                "status": row.get("status"),
                "category": row.get("category"),
                "title": row.get("title"),
                "content": row.get("content"),
                "tags": row.get("tags"),
                "provenance": row.get("provenance"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            })

        return {
            "versions": versions,
            "edges": edges,
            "current_memory_id": current_id,
            "current_row": current_row,
            "current_status": current_status,
        }

    def _edges_for(self, chain_ids: list[str]) -> list[dict[str, Any]]:
        """Every recorded edge on the chain, in ancestor→self→descendant order.

        A withdrawal edge (NULL target) is included; an edge leaving the
        chain is not, because it belongs to a different branch.
        """
        members = set(chain_ids)
        edges: list[dict[str, Any]] = []
        for vid in chain_ids:
            edge = self.read_edge_by_from(vid)
            if edge is None:
                continue
            to = edge["to_memory_id"]
            if to is None or to in members:
                edges.append(edge)
        return edges


@contextmanager
def _read_snapshot(pool: Any, pg: Any, *, deadline: Any = None):
    """Lease ONE connection and open ONE read-only repeatable-read view.

    Multi-query chain reads share this so they cannot mix a committed
    writer's state into a single answer. The lease is released before the
    caller can take another one — never nested.

    The read-only transaction is closed (rollback) on BOTH the success and
    the error path BEFORE the lease is released. If opening the view fails,
    transaction cleanup is still attempted and the lease is ALWAYS released;
    a cleanup failure is surfaced as an explicit failure, never swallowed.
    """
    lease = _acquire_lease(pool, pg, deadline=deadline)
    snap: Optional["_ReadSnapshot"] = None
    try:
        try:
            snap = _ReadSnapshot(lease.connection)
        except Exception:
            # BEGIN failed: the read-only transaction may be half-open. End it
            # with the status-aware mechanism (an explicit ROLLBACK statement
            # in autocommit mode) before the lease is released; a failed
            # cleanup is itself an explicit failure, never a silent success.
            try:
                _end_open_transaction(lease.connection, force=True)
            except Exception as cleanup_exc:
                raise _SnapshotCleanupFailed(
                    f"read-only snapshot BEGIN cleanup failed: {cleanup_exc!r}"
                ) from cleanup_exc
            raise
        try:
            yield snap
        finally:
            # Close the read-only REPEATABLE READ transaction (rollback) on
            # BOTH the success and the error path, BEFORE the lease is
            # released. ``_ReadSnapshot`` is entered here explicitly — yielding
            # a bare instance would leave its ``__exit__`` unrun.
            snap.close()
    finally:
        lease.close()


def _row_to_dict(row: tuple) -> dict[str, Any]:
    keys = (
        "memory_id", "category", "title", "content", "tags", "provenance",
        "status", "created_at", "updated_at", "embedding", "embed_model",
    )
    if len(row) > len(keys):
        keys = keys + ("__cosine__",)
    raw = dict(zip(keys, row))

    content = raw.get("content") or ""
    preview = content[:_PREVIEW_LEN]
    if len(content) > _PREVIEW_LEN:
        preview = preview + "…"

    prov = raw.get("provenance")
    if isinstance(prov, str):
        # A jsonb column read back as text (some drivers / the deterministic
        # fake) still IS a JSON object: parse it so callers and the
        # verification compare like for like, never an opaque {}.
        try:
            prov = json.loads(prov)
        except ValueError:
            prov = {}
    if not isinstance(prov, dict):
        try:
            prov = dict(prov) if prov else {}
        except Exception:
            prov = {}

    out: dict[str, Any] = {
        "memory_id": raw["memory_id"],
        "source_id": raw["memory_id"],
        "category": raw["category"],
        "title": raw["title"],
        "tags": list(raw["tags"] or []),
        "content": content,
        "content_preview": preview,
        "provenance": prov,
        "status": raw["status"],
        "created_at": raw["created_at"].isoformat() if raw["created_at"] else None,
        "updated_at": raw["updated_at"].isoformat() if raw["updated_at"] else None,
        "embedding": list(raw["embedding"]) if raw["embedding"] is not None else None,
        "embed_model": raw["embed_model"],
        "kind": "active_memory",
    }
    if "__cosine__" in raw:
        try:
            out["cosine"] = float(raw["__cosine__"])
        except (TypeError, ValueError):
            out["cosine"] = 0.0
    return out


__all__ = [
    "ActiveMemoryWriter",
    "ActiveMemoryReader",
    "derive_memory_id",
    "derive_correction_id",
    "derive_relation_id",
    "normalize_correction_request",
    "MemoryRecord",
    "MemoryWriteResult",
    "MemoryArchiveResult",
    "DURABLE_COMMITTED",
    "DEDUPLICATED",
    "DERIVED_WARNING",
    "DURABLE_FAILED",
    # M01 correction contract
    "CORRECTION_COMMITTED",
    "CORRECTION_DEDUPLICATED",
    "CORRECTION_DERIVED_WARNING",
    "CORRECTION_CONFLICT",
    "ERR_INVALID_REQUEST",
    "ERR_UNAUTHORIZED_AUTHORITY",
    "ERR_INVALID_CORRECTION_SOURCE",
    "ERR_NOT_FOUND",
    "ERR_TARGET_NOT_CURRENT",
    "ERR_MIGRATION_REQUIRED",
    "ERR_DURABLE_FAILED",
    "REQUIRED_CORRECTION_AUTHORITY",
    "RELATION_TYPE_SUPERSEDES",
    "RELATION_TYPE_WITHDRAWS",
    "RELATION_TYPE_CORRECTS",
    "CORRECTION_MODE_REPLACE",
    "CORRECTION_MODE_WITHDRAW",
    "CORRECTION_MODES",
    "CORRECTION_MODE_RELATION_TYPE",
    "EXPLICIT_TOOL_REQUEST_PROVENANCE",
]