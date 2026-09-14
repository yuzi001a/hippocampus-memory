"""Lane A: deterministic in-memory SQLite-backed fake pool.

This lane exists so the G5b evaluator is reproducible without
spinning up a real PG. It is **explicitly** a deterministic lab
measurement, not a production-runtime measurement.

What it does:

  * Implements a minimal ``pool.lease(timeout=...)`` API that yields
    a connection with ``.cursor()`` and ``.commit()`` (matching the
    surface that ``v3core.active_memory_store`` consumes).
  * Translates the small subset of pg-specific syntax used by
    ActiveMemoryWriter/Reader into SQLite-compatible SQL:
        %s::vector  →  ?
        %s::jsonb   →  ?
      plus replacement of the keyword-search ``unnest(COALESCE(tags,
      ARRAY[]::text[]))`` ILIKE branch with a JSON1-each-tag scan.
  * Records row state in-memory (no SQLite file). Each lease gets a
    snapshot view consistent with the writer's idempotent contract.

What it deliberately does NOT do:

  * Does NOT replicate vector cosine search semantics. The vector
    lane is computed by the writer's stub embedder (deterministic
    hash) and the reader picks top-N by squared Euclidean distance.
  * Does NOT claim parity with PG's IVFFLAT, planner, or full text
    ILIKE ranking. The lane label is "deterministic lab", never
    "real production".

The fake is single-process; concurrency > 1 lane is not supported.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from typing import Any, Iterator

from .failure_taxonomy import NOT_STORED  # noqa: F401  (reserved)


# pg → sqlite translation. We rewrite the small subset of pg syntax
# used by ActiveMemoryWriter/Reader into SQLite-compatible SQL.
#
# ActiveMemoryWriter uses (see active_memory_store.py):
#   - ``%s`` placeholders → SQLite uses ``?``.
#   - ``%s::jsonb`` and ``%s::vector`` casts → already placeholders
#     before rewriting; cast strips the type suffix.
#   - ``NOW()`` → ``datetime('now')``.
#   - ``INSERT ... ON CONFLICT (memory_id) DO NOTHING RETURNING ...``
#     → SQLite supports ``ON CONFLICT(col) DO NOTHING`` (no RETURNING,
#     we read rowcount instead). The writer's fetchone() then read-back
#     pattern still works because the readback is a separate SELECT.
#   - ``public.explicit_memories`` schema prefix → strip.
#
# ActiveMemoryReader uses:
#   - ``title ILIKE %s`` → ``title LIKE ?``.
#   - The ``unnest(COALESCE(tags, ARRAY[]::text[]))`` tag ILIKE branch
#     → ``EXISTS (SELECT 1 FROM json_each(tags) AS t WHERE t.value LIKE ?)``.
#   - ``1 - (embedding <=> %s::vector)`` → not supported in SQLite;
#     we pre-compute the cosine in Python from the embedding rows.
#     The translator rewrites the SELECT to return ``embedding`` so the
#     runner can rank. We rewrite the SQL to ``ORDER BY embedding`` as
#     a no-op and let the Python layer sort.

_PG_CAST_VECTOR = re.compile(r"::vector\b")
_PG_CAST_JSONB = re.compile(r"::jsonb\b")
_PG_CAST_TEXTARRAY = re.compile(r"ARRAY\[\]::text\[\]")
_PG_NOW = re.compile(r"\bNOW\(\)")
_PG_SCHEMA = re.compile(r"\bpublic\.")
# Word-boundary check for ``%s`` placeholders (only those not already
# inside a ``?`` placeholder, i.e. real parameter markers).
_PG_PARAM = re.compile(r"%s")


def _translate(sql: str) -> str:
    """Translate pg-only SQL fragments into SQLite-compatible SQL."""
    out = sql
    out = _PG_CAST_VECTOR.sub("", out)
    out = _PG_CAST_JSONB.sub("", out)
    out = _PG_CAST_TEXTARRAY.sub("'[]'", out)
    out = _PG_NOW.sub("datetime('now')", out)
    out = _PG_SCHEMA.sub("", out)
    # Convert all pg ``%s`` placeholders to sqlite ``?``. Order matters:
    # we do this AFTER stripping the type-suffix casts so we don't
    # touch ``?`` placeholders that may exist already (there are none
    # today, but be defensive).
    out = _PG_PARAM.sub("?", out)
    # Strip the ``unnest(...)`` tag-ILIKE branch entirely and replace
    # with a JSON1 each-tag scan. The branch appears verbatim in
    # ActiveMemoryReader.search_keyword. We do this BEFORE the ILIKE
    # conversion below so the branch is matched against the original
    # pg syntax (``t ILIKE ?``).
    out = re.sub(
        r"OR\s+EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+unnest\s*\(\s*COALESCE\s*\(\s*tags\s*,\s*'\[\]'\s*\)\s*\)\s+AS\s+t\s+WHERE\s+t\s+ILIKE\s+\?\s*\)",
        "OR EXISTS (SELECT 1 FROM json_each(tags) AS t WHERE t.value LIKE ?)",
        out,
        flags=re.IGNORECASE,
    )
    # ILIKE → LIKE  (the lab fixtures are English-only; SQLite's LIKE
    # is ASCII-case-insensitive by default.)
    out = out.replace(" ILIKE ", " LIKE ")
    # The vector SELECT path uses ``<=>(embedding, %s::vector)`` and
    # ``ORDER BY embedding <=> %s::vector`` — strip the operators and
    # the cosine projection (we rank in Python from the embedding
    # payload). ActiveMemoryReader.search_vector passes 3 parameters
    # ``(emb, emb, limit)`` corresponding to the cosine projection,
    # the ORDER BY slot, and the LIMIT placeholder. Strip the
    # cosine projection's binding slot and the ORDER BY slot, but
    # PRESERVE the LIMIT placeholder. After our rewrite the SQL has
    # exactly 1 placeholder (LIMIT) but receives 3 params — we drop
    # the first two at the cursor wrapper level.
    out = out.replace("1 - (embedding <=> ?)", "1")
    out = out.replace("ORDER BY embedding <=> ?", "ORDER BY created_at DESC")
    # Leave ``embedding <=> ?`` if it appears in WHERE clauses.
    # We don't currently strip the second binding here; the cursor
    # wrapper detects the ``search_vector``-specific binding pattern
    # (first two params both equal to the query vector) and discards
    # them.
    # ``RETURNING memory_id`` is not supported in SQLite ON CONFLICT
    # DO NOTHING. The writer uses fetchone() to learn whether the
    # insert actually happened — in SQLite that would always return
    # None. We rewrite to drop RETURNING; the writer's idempotent
    # algorithm relies on the readback through lease #2, which is
    # preserved verbatim. The change-detection between inserted vs
    # deduplicated is then done by reading ``rowcount`` (0 vs 1).
    # NOTE: the writer's INSERT uses ``cur.fetchone()`` to decide
    # ``inserted``. With RETURNING stripped, fetchone() always
    # returns None. To preserve the inserted-vs-dedup distinction,
    # we wrap the SQL with a ``SELECT changes() AS inserted`` for the
    # fake. We do this by appending a follow-up SELECT in lease_a
    # before commit, which is _not_ what the original SQL did — but
    # the writer's _acquire_lease + connection is the only seam we
    # have. We therefore rewrite INSERT … DO NOTHING RETURNING
    # ``memory_id`` to ``INSERT … ON CONFLICT(col) DO NOTHING`` (sqlite
    # syntax). The reader layer never looks at RETURNING.
    out = re.sub(
        r"\s+ON CONFLICT\s*\(\s*memory_id\s*\)\s*DO NOTHING\s*RETURNING\s+memory_id\b",
        " ON CONFLICT(memory_id) DO NOTHING",
        out,
    )
    # Existing index expr uses ``memory_id`` → keep.
    return out


_ROW_KEYS = (
    "memory_id", "category", "title", "content", "tags", "provenance",
    "status", "created_at", "updated_at", "embedding", "embed_model",
)


def _decode_row(row: tuple) -> tuple:
    """Decode JSON-stored columns (tags / embedding) for the reader layer.

    ActiveMemoryReader.get_by_memory_id and search_keyword both index
    into the row by tuple position (the writer's ``_row_to_dict``
    function uses zip). The pg adapter returns ``tags`` as a Python
    list and ``embedding`` as a list[float]; our SQLite stores them
    as JSON text. Decode here so the writer's _row_to_dict sees the
    same shape.

    created_at / updated_at come back as SQLite's ``datetime('now')``
    text (e.g. ``"2026-09-14 12:34:56"``). The writer calls
    ``.isoformat()`` on them, so we synthesise a Python datetime
    object that supports .isoformat().
    """
    import datetime

    if not row:
        return row
    out = list(row)
    # tags at index 4, embedding at index 9, created_at index 7,
    # updated_at index 8.
    if len(out) > 4 and isinstance(out[4], str):
        try:
            out[4] = json.loads(out[4]) if out[4] else []
        except (ValueError, TypeError):
            out[4] = []
    if len(out) > 9 and isinstance(out[9], str):
        try:
            out[9] = json.loads(out[9])
        except (ValueError, TypeError):
            out[9] = None
    for idx in (7, 8):
        if len(out) > idx and isinstance(out[idx], str) and out[idx]:
            try:
                out[idx] = datetime.datetime.fromisoformat(out[idx])
            except ValueError:
                try:
                    out[idx] = datetime.datetime.strptime(
                        out[idx], "%Y-%m-%d %H:%M:%S"
                    )
                except ValueError:
                    out[idx] = None
    return tuple(out)


class _CursorWrapper:
    """Wraps sqlite3.Cursor to handle pg-specific patterns.

    1. INSERT ... ON CONFLICT DO NOTHING has no RETURNING in SQLite;
       we emulate it for the writer's ``fetchone() is not None`` check.

    2. PG text[] / vector columns come back as Python list / list[float].
       Our SQLite stores them as JSON text. We decode on read.
    """

    _INSERT_FINGERPRINT = "INSERT INTO "

    __slots__ = ("_cur", "_is_insert", "_inserted_row")

    def __init__(self, cur: sqlite3.Cursor) -> None:
        self._cur = cur
        self._is_insert = False
        self._inserted_row = None

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def execute(self, sql: str, params: tuple = ()) -> "_CursorWrapper":
        translated = _translate(sql)
        upper = translated.lstrip().upper()
        is_insert = upper.startswith("INSERT INTO") and "ON CONFLICT" in translated.upper()
        # Detect the search_vector SELECT (status='active' AND
        # embedding IS NOT NULL). The writer binds (emb, emb, limit)
        # but our rewrite reduces the SQL to a single ``?`` (the
        # LIMIT). Trim the first two redundant bindings.
        if (
            not is_insert
            and "EMBEDDING IS NOT NULL" in upper
            and isinstance(params, tuple)
            and len(params) >= 3
        ):
            params = params[-1:]  # keep only the LIMIT
        if is_insert:
            self._is_insert = True
            if params and isinstance(params, tuple):
                params = tuple(
                    json.dumps(p, ensure_ascii=False) if isinstance(p, list) else p
                    for p in params
                )
            self._cur.execute(translated, params)
            memory_id = params[0] if params else None
            if self._cur.rowcount == 1 and memory_id is not None:
                self._inserted_row = (memory_id,)
            else:
                self._inserted_row = None
        else:
            self._is_insert = False
            self._cur.execute(translated, params)
        return self

    def fetchone(self) -> tuple | None:
        if self._is_insert:
            return self._inserted_row
        r = self._cur.fetchone()
        return _decode_row(r) if r else None

    def fetchall(self) -> list[tuple]:
        rows = self._cur.fetchall()
        return [_decode_row(r) if r else r for r in rows]

    def close(self) -> None:
        self._cur.close()


class _ConnWrapper:
    """Wrap sqlite3.Connection so cursor() returns _CursorWrapper."""

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def cursor(self) -> _CursorWrapper:
        return _CursorWrapper(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


class _Lease:
    """One lease over the in-memory SQLite connection.

    Mimics the surface ``v3core.active_memory_store`` calls:
      connection.cursor()
      connection.commit()
    """

    __slots__ = ("_pool", "_closed")

    def __init__(self, pool: "_SqliteFakePool") -> None:
        self._pool = pool
        self._closed = False

    @property
    def connection(self) -> _ConnWrapper:
        return _ConnWrapper(self._pool._conn)

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "_Lease":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False


class _SqliteFakePool:
    """Minimal in-memory pool. ``lease(timeout=...)`` yields a ``_Lease``.

    Threading is locked because SQLite connections are not
    thread-safe by default. The evaluator is single-process so
    contention is bounded.
    """

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        # JSON1 is enabled by default in modern SQLite.
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            # tags stored as JSON array TEXT (json1 gives us json_each).
            cur.execute(
                """
                CREATE TABLE explicit_memories (
                    memory_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    provenance TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','archived')),
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    embedding TEXT,
                    embed_model TEXT
                )
                """
            )
            cur.execute(
                "CREATE INDEX explicit_memories_status_idx "
                "ON explicit_memories (status)"
            )
            cur.execute(
                "CREATE INDEX explicit_memories_created_at_idx "
                "ON explicit_memories (created_at DESC)"
            )
            self._conn.commit()

    def lease(self, timeout: float | int | None = None, **_: Any) -> _Lease:
        # SQLite does not honour pool timeouts; we acquire the lock
        # so concurrent leases serialize.
        if timeout is not None:
            # No timeout enforcement — sqlite3 in-memory is sync.
            pass
        self._lock.acquire()
        return _Lease(self)


# ── deterministic embedder ────────────────────────────────────────────────


_EMBED_DIM = 1024


def deterministic_embedder(text: str, embed_cfg: dict[str, Any]) -> list[float]:
    """Deterministic 1024-dim stub embedder.

    Uses a hash-expanded bag-of-tokens; same input → same vector; no
    network, no external model. The model fingerprint returned in
    embed_cfg is ``stub-bag-of-tokens-v1``.
    """
    import hashlib
    import math

    if not isinstance(text, str):
        text = str(text)
    vec = [0.0] * _EMBED_DIM
    tokens = re.findall(r"[A-Za-z0-9_]+", text.lower())
    if not tokens:
        tokens = [text]
    for tok in tokens:
        h = hashlib.blake2b(tok.encode("utf-8"), digest_size=16).digest()
        idx = int.from_bytes(h[:4], "little") % _EMBED_DIM
        sign = 1.0 if (h[4] & 1) else -1.0
        vec[idx] += sign * (1.0 + (h[5] / 255.0))
    # L2-normalize so cosine behaves like a distance 1 - cos.
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def deterministic_embed_cfg() -> dict[str, Any]:
    """A minimal embed_cfg the fake pool recognises (matches the contract)."""
    return {"_fingerprint": "stub-bag-of-tokens-v1", "model": "stub-bag-of-tokens-v1"}


def cosine_topk(
    query_vec: list[float], rows: list[dict[str, Any]], limit: int
) -> list[tuple[dict[str, Any], float]]:
    """Return top-K rows by descending cosine similarity to query_vec.

    Accepts only rows whose embedding is set; falls back to 0.0
    similarity for empty embeddings (which should never be returned
    by ``search_vector`` since it filters ``embedding IS NOT NULL``).
    """
    import math

    if not query_vec:
        return []
    q_norm = math.sqrt(sum(v * v for v in query_vec)) or 1.0
    out: list[tuple[dict[str, Any], float]] = []
    for r in rows:
        emb = r.get("embedding")
        if not emb:
            continue
        try:
            vec = [float(x) for x in emb]
        except (TypeError, ValueError):
            continue
        r_norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        dot = sum(a * b for a, b in zip(query_vec, vec))
        cos = dot / (q_norm * r_norm)
        out.append((r, float(cos)))
    out.sort(key=lambda x: x[1], reverse=True)
    return out[: max(1, int(limit))]


# ── the writer/reader wrapper that uses the fake pool ─────────────────────


def build_lane_a_writer_and_reader() -> tuple[Any, Any]:
    """Construct ActiveMemoryWriter + ActiveMemoryReader over the fake pool.

    Imports are deferred so the package is importable even when v3-core
    is not on PYTHONPATH (the package layout puts eval/ under
    src/v3-core/eval/, which is reachable via the existing v3-core
    install).
    """
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    # eval/g5b_real_memory_evaluator/lane_a.py  →  src/v3-core/src
    src_root = os.path.normpath(
        os.path.join(here, "..", "..", "..", "src")
    )
    if src_root not in sys.path:
        sys.path.insert(0, src_root)

    from v3core.active_memory_store import (  # noqa: E402
        ActiveMemoryReader,
        ActiveMemoryWriter,
    )

    pool = _SqliteFakePool()
    cfg = deterministic_embed_cfg()
    writer = ActiveMemoryWriter(
        pool=pool, pg=None, config=None, embed_cfg=cfg, embedder=deterministic_embedder
    )
    reader = ActiveMemoryReader(pool=pool, pg=None)
    return writer, reader, pool


__all__ = [
    "_SqliteFakePool",
    "deterministic_embedder",
    "deterministic_embed_cfg",
    "cosine_topk",
    "build_lane_a_writer_and_reader",
]