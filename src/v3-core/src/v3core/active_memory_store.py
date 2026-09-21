"""v3core.active_memory_store — canonical active-memory boundary.

Sole entry point for writes into ``public.explicit_memories`` (P2a clean
boundary, branch ``p2a/active-memory-clean-boundary-20260909``).

Boundary rules:
  * All SQL goes through an injected ``pool`` (``PgPool.lease(timeout,
    deadline=...)``) or ``pg`` (``PgEmbedStore.lease(timeout)``). This module
    never opens a pool or DB driver connection on its own.
  * DDL lives in ``schema/explicit_memories.sql``; never auto-applied.

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
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from ._deadline import PrefetchDeadlineExceeded
from .pg_pool import DEFAULT_LEASE_TIMEOUT

logger = logging.getLogger("v3core.active_memory_store")

DURABLE_COMMITTED = "DURABLE_COMMITTED"
DEDUPLICATED = "DEDUPLICATED"
DERIVED_WARNING = "DERIVED_WARNING"
DURABLE_FAILED = "DURABLE_FAILED"

_TABLE = "public.explicit_memories"
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


def _acquire_lease(pool: Any, pg: Any, *, deadline: Any = None):
    """One lease from the injected pool/pg (PgPool preferred).

    Uses the canonical bounded lease timeout (DEFAULT_LEASE_TIMEOUT) so a
    missing deadline still admits a finite connect/wait bound instead of
    blocking indefinitely.  Deadline forwarding is preserved on the PgPool
    path; legacy/test pools that don't accept ``deadline`` fall back to the
    bounded timeout without dropping it.
    """
    timeout = DEFAULT_LEASE_TIMEOUT
    if pool is not None:
        try:
            return pool.lease(timeout=timeout, deadline=deadline)
        except TypeError:
            return pool.lease(timeout=timeout)
    if pg is not None:
        return _PgStoreLeaseAdapter(pg.lease(timeout=timeout))
    raise _PoolUnavailable("ActiveMemory requires an injected pool or pg.")


def _emb_str(vec: Iterable[float]) -> str:
    return "[" + ",".join(str(x) for x in vec) + "]"


def _jsonb(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


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
    "MemoryRecord",
    "MemoryWriteResult",
    "MemoryArchiveResult",
    "DURABLE_COMMITTED",
    "DEDUPLICATED",
    "DERIVED_WARNING",
    "DURABLE_FAILED",
]