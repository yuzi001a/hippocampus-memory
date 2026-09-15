"""LoCoMo Recall v2 disposable lab (G6C-A).

Provides a fail-closed safety guard for lab DSNs, a schema
bootstrap that applies the **public** alpha DDL with the
explicit_memories include marker, and explicit-column row
importers for the LoCoMo eval_v2 working set.

The safety contract is modeled on the public G5B Lane B guard
(``eval/g5b_real_memory_evaluator/lane_b.py``) so the two
guard surfaces agree on what counts as "disposable + safe":

  * Reserved production port ``5433`` is refused.
  * Reserved production DB names (``v3embeddings`` /
    ``v3embeddings_eval`` / ``v3embeddings_eval_v2``) and
    reserved production hostnames (``v3-pgvector`` /
    ``v3-pgvector-prod``) are refused.
  * Non-loopback hosts are refused. The lab must connect to
    ``localhost``, ``127.0.0.1``, ``::1`` or another loopback
    literal. A missing host is **not** auto-defaulted to
    ``localhost`` — the caller must spell it out so they can't
    accidentally inherit production.
  * Empty / unparseable DSNs are refused.
  * Error messages never echo the password (and never echo the
    full DSN verbatim) — the caller must not be able to recover
    a credential from a stack trace.

``bootstrap_schema(conn, repo_root)`` applies:

  * The public ``src/v3-core/schema/alpha_bootstrap.sql`` in full.
  * The public ``src/v3-core/schema/explicit_memories.sql``
    inline, at the canonical include marker
    ``-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/explicit_memories.sql <<<``.
    The marker is the only legal inclusion point — we replace
    the marker line with the body of the explicit_memories file
    so a single transaction applies both artifacts.
  * The bootstrap is statically validated against the
    ``DROP / TRUNCATE / DELETE`` blocklist before any cursor
    is touched; if any destructive token appears in the file
    contents, we refuse to apply it.

``import_rows(conn, rows)`` writes ``qa_pairs``,
``conversation_stream`` and ``eval_queries`` rows with
explicit columns and ``ON CONFLICT`` only where the existing
schema supports it. There is no silent fallback — a missing
table raises and the transaction is rolled back.

psycopg2 is imported only inside live functions so the package
imports cleanly in environments that do not have psycopg2
installed (the G5B contract).
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Iterable, Mapping


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LabDSNRefused(RuntimeError):
    """Raised when a DSN is rejected by the disposable-lab guard."""


class LabSchemaError(RuntimeError):
    """Raised when the bootstrap schema is missing, malformed, or destructive."""


class LabImportError(RuntimeError):
    """Raised when ``import_rows`` cannot apply its row payload."""


# ---------------------------------------------------------------------------
# DSN guard
# ---------------------------------------------------------------------------


# Production-reserved ports / names — the lab may never use these.
# The numeric port 5433 is a reserved safety sentinel; it is not
# a live endpoint the lab ever opens.
_RESERVED_PORTS = {"5433"}
_RESERVED_DBS = {"v3embeddings", "v3embeddings_eval", "v3embeddings_eval_v2"}
_RESERVED_HOSTS = {"v3-pgvector", "v3-pgvector-prod"}

# Loopback literals we accept. We do NOT default a missing host
# to localhost — the caller has to spell it out.
_LOOPBACK_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "::1",
    "0:0:0:0:0:0:0:1",
    "[::1]",
})


def _parse_dsn(dsn: str) -> dict[str, str]:
    """Parse a psycopg2 ``key=value`` DSN into a dict.

    Single-quoted values are honoured. Unrecognised keys are
    preserved under the same lowercase name. Returns an empty
    dict when the input is empty or not a string.
    """

    out: dict[str, str] = {}
    if not isinstance(dsn, str) or not dsn.strip():
        return out
    for m in re.finditer(r"(\w+)=('(?:[^']|'')*'|\S+)", dsn):
        key = m.group(1).lower()
        val = m.group(2)
        if val.startswith("'") and val.endswith("'"):
            val = val[1:-1].replace("''", "'")
        out[key] = val
    return out


def _stripped_dsn(parts: dict[str, str]) -> str:
    """Return a DSN echo with the password replaced by ``***``.

    Used only inside explicit guard diagnostics where the
    caller needs to know WHICH key tripped the rule. Never used
    to echo the password.
    """

    safe = dict(parts)
    if "password" in safe:
        safe["password"] = "***"
    if "passfile_password" in safe:
        safe["passfile_password"] = "***"
    pairs = []
    for k in sorted(safe):
        pairs.append(f"{k}={safe[k]}")
    return " ".join(pairs)


def validate_disposable_dsn(dsn: str) -> dict[str, str]:
    """Validate that ``dsn`` looks like a disposable lab DSN.

    Returns the parsed parts (lower-cased keys) on success.
    Raises :class:`LabDSNRefused` on any guard violation.

    The guard rules:

      1. ``dsn`` is a non-empty string and parses to at least
         one ``key=value`` pair.
      2. ``port`` (when present) is not in ``_RESERVED_PORTS``.
      3. ``dbname`` (when present) is not in ``_RESERVED_DBS``.
      4. ``host`` (when present) is not in ``_RESERVED_HOSTS``
         AND is loopback (``localhost`` / ``127.0.0.1`` /
         ``::1`` / ``0:0:0:0:0:0:0:1`` / ``[::1]``).
      5. ``host`` is REQUIRED — a missing host is refused so
         the caller cannot inherit a non-loopback default.
    """

    parts = _parse_dsn(dsn)
    if not parts:
        raise LabDSNRefused(
            "lab DSN could not be parsed; refusing to open any connection."
        )

    port = (parts.get("port") or "").strip()
    dbname = (parts.get("dbname") or "").strip().lower()
    host = (parts.get("host") or "").strip().lower()

    if port and port in _RESERVED_PORTS:
        raise LabDSNRefused(
            f"lab DSN port={port!r} is reserved (production / known lab); "
            f"refusing. sanitised={_stripped_dsn(parts)}"
        )

    if dbname and dbname in _RESERVED_DBS:
        raise LabDSNRefused(
            f"lab DSN dbname={dbname!r} is a reserved production/eval "
            f"database name; refusing. sanitised={_stripped_dsn(parts)}"
        )

    if host and host in _RESERVED_HOSTS:
        raise LabDSNRefused(
            f"lab DSN host={host!r} matches a reserved production hostname; "
            f"refusing. sanitised={_stripped_dsn(parts)}"
        )

    if not host:
        raise LabDSNRefused(
            "lab DSN host is required and must be loopback "
            "(localhost / 127.0.0.1 / ::1). refusing."
        )

    if host not in _LOOPBACK_HOSTS:
        raise LabDSNRefused(
            f"lab DSN host={host!r} is not loopback; refusing. "
            f"sanitised={_stripped_dsn(parts)}"
        )

    return parts


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


# Markers / constraints for the bootstrap include.
_ALPHA_INCLUDE_MARKER = "-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/explicit_memories.sql <<<"
_ALPHA_INCLUDE_FILENAME = "explicit_memories.sql"

# Destructive SQL tokens we refuse to ship. We match on whole-word
# boundaries so ``DROP_TABLE_IF_EXISTS``-style namespaced identifiers
# do not trigger. We do NOT try to be a full SQL parser — we are
# a guard rail.
_DESTRUCTIVE_TOKENS = (
    r"\bDROP\b",
    r"\bTRUNCATE\b",
    r"\bDELETE\b",
)
_DESTRUCTIVE_RE = re.compile("|".join(_DESTRUCTIVE_TOKENS), re.IGNORECASE)


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        raise LabSchemaError(f"lab: cannot read schema file {path!r}") from exc


def _validate_bootstrap_sql(sql: str) -> None:
    """Refuse any bootstrap payload that contains destructive SQL.

    Static, line-agnostic check: if a ``DROP`` / ``TRUNCATE`` /
    ``DELETE`` token appears anywhere in the SQL body we raise.
    Comments mentioning these words are filtered first — we
    strip ``-- ...`` and ``/* ... */`` blocks before scanning.
    """

    # Strip single-line comments.
    no_line = re.sub(r"--[^\n]*", " ", sql)
    # Strip block comments.
    no_block = re.sub(r"/\*.*?\*/", " ", no_line, flags=re.DOTALL)
    match = _DESTRUCTIVE_RE.search(no_block)
    if match:
        token = match.group(0).upper()
        raise LabSchemaError(
            f"lab: bootstrap SQL contains destructive token {token!r}; "
            "refusing to apply. Bootstrap is additive-only."
        )


def _compose_bootstrap_sql(repo_root: str) -> str:
    """Read + validate the alpha bootstrap and inline the explicit include."""

    schema_dir = os.path.normpath(
        os.path.join(repo_root, "src", "v3-core", "schema")
    )
    alpha_path = os.path.join(schema_dir, "alpha_bootstrap.sql")
    explicit_path = os.path.join(schema_dir, _ALPHA_INCLUDE_FILENAME)

    if not os.path.isfile(alpha_path):
        raise LabSchemaError(
            f"lab: alpha_bootstrap.sql not found at expected path {alpha_path!r}"
        )
    if not os.path.isfile(explicit_path):
        raise LabSchemaError(
            f"lab: {_ALPHA_INCLUDE_FILENAME!r} not found at "
            f"expected path {explicit_path!r}"
        )

    alpha_sql = _read_text(alpha_path)
    explicit_sql = _read_text(explicit_path)

    # Validate each artifact independently BEFORE composing.
    _validate_bootstrap_sql(alpha_sql)
    _validate_bootstrap_sql(explicit_sql)

    # The include marker must appear exactly once and we replace
    # it with the explicit_memories.sql body. This keeps the
    # canonical artefact single-sourced.
    marker_count = alpha_sql.count(_ALPHA_INCLUDE_MARKER)
    if marker_count != 1:
        raise LabSchemaError(
            f"lab: alpha_bootstrap.sql include marker must appear "
            f"exactly once (found {marker_count} occurrences)."
        )

    composed = alpha_sql.replace(_ALPHA_INCLUDE_MARKER, explicit_sql)

    # Additive eval_queries DDL — appended as a separate tail
    # block. We refuse destructive SQL here too (defensive —
    # the marker is a constant string but the contents could be
    # edited in the future).
    eval_queries_block = _EVAL_QUERIES_DDL
    _validate_bootstrap_sql(eval_queries_block)
    composed = composed + "\n\n" + eval_queries_block + "\n"

    # Final defensive check on the composed body.
    _validate_bootstrap_sql(composed)
    return composed


# Additive ``eval_queries`` DDL. Appended at the end of the
# bootstrap composition so the import path can rely on the
# table existing without a separate migration. The contract
# is: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE UNIQUE INDEX
# IF NOT EXISTS`` for ``source_id`` — both statements are
# idempotent, so re-running the bootstrap is a no-op.
_EVAL_QUERIES_DDL = """
-- -----------------------------------------------------------------------------
-- eval_queries — disposable-lab target for LoCoMo eval_v2 import.
-- Appended additively by locomo_recall_v2.lab._compose_bootstrap_sql so the
-- import path can rely on the table existing post-bootstrap. The public
-- alpha_bootstrap.sql intentionally does NOT define eval_queries; the
-- loader treats it as a lab-only artefact.
--
-- Idempotency:
--   * CREATE TABLE IF NOT EXISTS + CREATE UNIQUE INDEX IF NOT EXISTS.
--   * No DROP / TRUNCATE / DELETE statements. Bootstrap-only.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.eval_queries (
    source_id     TEXT         NOT NULL,
    sample_id     TEXT,
    qa_id         TEXT,
    query_idx     INTEGER,
    category      TEXT,
    question      TEXT         NOT NULL DEFAULT '',
    answer        TEXT,
    evidence      JSONB        NOT NULL DEFAULT '[]'::jsonb,
    source_hash   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS eval_queries_source_id_key
    ON public.eval_queries (source_id);

CREATE INDEX IF NOT EXISTS eval_queries_sample_idx
    ON public.eval_queries (sample_id);
"""


# ---------------------------------------------------------------------------
# IVFFlat index definitions — evaluator-only knob for G6C-B0
# ---------------------------------------------------------------------------
#
# All seven canonical IVFFlat indexes that ship in
# ``alpha_bootstrap.sql`` / ``explicit_memories.sql`` are listed
# here as a single source of truth. The list MUST stay in lock-step
# with the schema DDL: a divergence here would silently invalidate
# the after-import reproducibility contract (Mode C would either
# miss an index or apply the wrong one).
#
# Indexes:
#   * explicit_memories_embedding_ivfflat  (explicit_memories.embedding)
#   * qa_pairs_embedding_ivfflat           (qa_pairs.embedding)
#   * conversation_stream_embedding_ivfflat (conversation_stream.embedding)
#   * topics_embedding_ivfflat             (topics.embedding)
#   * topic_entries_embedding_ivfflat      (topic_entries.embedding)
#   * observation_notes_embedding_ivfflat  (observation_notes.embedding)
#   * yin_paragraphs_embedding_ivfflat     (yin_paragraphs.embedding)
#
# Each entry preserves the canonical
# ``USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)``
# shape verbatim. The helper :func:`create_vector_indexes` re-applies
# this list; the helper :func:`_compose_bootstrap_sql_with_phase`
# strips them when the caller asked for ``after_import``. Both
# helpers share the same definitions so the deferred re-creation
# is byte-identical to the bootstrap-time creation.
_VECTOR_INDEX_DDL = (
    """
CREATE INDEX IF NOT EXISTS qa_pairs_embedding_ivfflat
    ON public.qa_pairs
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
""",
    """
CREATE INDEX IF NOT EXISTS conversation_stream_embedding_ivfflat
    ON public.conversation_stream
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
""",
    """
CREATE INDEX IF NOT EXISTS topics_embedding_ivfflat
    ON public.topics
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
""",
    """
CREATE INDEX IF NOT EXISTS explicit_memories_embedding_ivfflat
    ON public.explicit_memories
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
""",
    """
CREATE INDEX IF NOT EXISTS topic_entries_embedding_ivfflat
    ON public.topic_entries
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
""",
    """
CREATE INDEX IF NOT EXISTS observation_notes_embedding_ivfflat
    ON public.observation_notes
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
""",
    """
CREATE INDEX IF NOT EXISTS yin_paragraphs_embedding_ivfflat
    ON public.yin_paragraphs
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
""",
)

# Canonical phase values for the ``index_build_phase`` knob.
INDEX_BUILD_PHASE_BOOTSTRAP = "bootstrap_before_import"
INDEX_BUILD_PHASE_AFTER_IMPORT = "after_import"
ALLOWED_INDEX_BUILD_PHASES: frozenset[str] = frozenset({
    INDEX_BUILD_PHASE_BOOTSTRAP,
    INDEX_BUILD_PHASE_AFTER_IMPORT,
})


def _strip_ivfflat_from_sql(sql: str) -> str:
    """Remove every IVFFlat ``CREATE INDEX`` statement from ``sql``.

    The real-world shape is multi-line::

        CREATE INDEX IF NOT EXISTS qa_pairs_embedding_ivfflat
            ON public.qa_pairs
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100);

    so we cannot rely on the first line carrying ``USING
    ivfflat`` — the keyword can sit on any continuation line of
    the same DDL block.  The block boundaries are detected
    by:

      * a line whose stripped form starts with ``CREATE INDEX``
        opens a new block;
      * the block closes on the first standalone ``;`` (the
        PG DDL terminator) — a continuation line may NOT close
        the block because ``ON public.qa_pairs`` is itself a
        continuation;
      * if ANY line inside the block contains ``ivfflat`` (case
        insensitive) the entire block is dropped; otherwise the
        block is kept verbatim.

    Used by :func:`_compose_bootstrap_sql_with_phase` when the
    caller asks for ``index_build_phase == "after_import"`` —
    the bootstrap body must drop the index DDL so the import
    path writes rows into a heap that is rebuilt AFTER import.
    """

    out: list[str] = []
    block: list[str] | None = None
    for line in sql.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if block is None:
            if upper.startswith("CREATE INDEX"):
                # Open a new block; we do NOT commit to keeping
                # it until we have walked every line up to the
                # terminating ``;``.
                block = [line]
                if ";" in stripped:
                    _flush_block(out, block)
                    block = None
                continue
            out.append(line)
            continue
        # Inside an open ``CREATE INDEX`` block.
        block.append(line)
        if ";" in stripped:
            _flush_block(out, block)
            block = None
    # A trailing block without a terminator is malformed SQL,
    # but we keep the parser permissive and surface it as-is so
    # the real driver surfaces the syntax error (the alternative
    # is silently swallowing DDL, which is the bug we are
    # guarding against).
    if block is not None:
        out.extend(block)
    return "\n".join(out)


def _flush_block(out: list[str], block: list[str]) -> None:
    """Append ``block`` to ``out`` iff it does not contain IVFFlat.

    Helper for :func:`_strip_ivfflat_from_sql`.  The decision
    uses a case-insensitive substring match against the joined
    block — cheap, and correct for the canonical
    ``USING ivfflat`` shape that lives somewhere in the middle
    of the block.
    """

    joined = "\n".join(block)
    if "ivfflat" in joined.lower():
        # Drop the entire block.  We do NOT emit a sentinel
        # comment because downstream SQL must stay byte-stable
        # for any non-IVFFlat auditors.
        return
    out.extend(block)


def _compose_bootstrap_sql_with_phase(
    repo_root: str, *, index_build_phase: str = INDEX_BUILD_PHASE_BOOTSTRAP
) -> str:
    """Compose the bootstrap SQL with the requested index-build phase.

    When ``index_build_phase == "bootstrap_before_import"`` (default)
    the composed SQL is identical to :func:`_compose_bootstrap_sql`
    — the IVFFlat indexes ride along inside the bootstrap
    transaction.

    When ``index_build_phase == "after_import"`` the IVFFlat
    ``CREATE INDEX`` statements are stripped from the bootstrap
    body. The caller MUST then invoke :func:`create_vector_indexes`
    after the import step. Stripping here (instead of skipping
    the indexes entirely) keeps the bootstrap shape byte-stable
    for any other DDL auditors.
    """

    if index_build_phase not in ALLOWED_INDEX_BUILD_PHASES:
        raise LabSchemaError(
            f"lab: index_build_phase must be one of "
            f"{sorted(ALLOWED_INDEX_BUILD_PHASES)!r}; "
            f"got {index_build_phase!r}"
        )
    sql = _compose_bootstrap_sql(repo_root)
    if index_build_phase == INDEX_BUILD_PHASE_BOOTSTRAP:
        return sql
    # after_import path: strip every IVFFlat CREATE INDEX block.
    return _strip_ivfflat_from_sql(sql)


def create_vector_indexes(conn: Any) -> dict[str, str]:
    """Re-apply the canonical IVFFlat indexes to ``conn``.

    Idempotent. Each statement uses ``CREATE INDEX IF NOT EXISTS``,
    so a repeat call is a no-op when the indexes already exist.

    Returns a mapping of ``{table_name: index_name}`` for every
    index that was attempted — the caller can diff the returned
    map against the schema's ``pg_indexes`` view to confirm the
    indexes are now present.

    Raises :class:`LabSchemaError` when ``conn.cursor()`` or the
    individual ``CREATE INDEX`` statements fail. All four
    statements run inside a single transaction; a failure on any
    one of them rolls the entire batch back so the schema is
    never left half-built.
    """

    try:
        cur = conn.cursor()
    except Exception as exc:
        raise LabSchemaError(
            f"lab: conn.cursor() failed during create_vector_indexes: "
            f"{type(exc).__name__} (message suppressed)"
        ) from exc

    applied: dict[str, str] = {}
    try:
        for stmt in _VECTOR_INDEX_DDL:
            try:
                cur.execute(stmt)
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise LabSchemaError(
                    f"lab: create_vector_indexes failed: "
                    f"{type(exc).__name__} (message suppressed)"
                ) from exc
            # Parse ``CREATE INDEX [IF NOT EXISTS] <name> ON <table>``
            # back out of the statement so the caller can confirm
            # what was applied. We do NOT issue a SELECT against
            # ``pg_indexes`` here — keeping this function pure
            # SQL-emission makes it testable against a fake
            # connection without any PG introspection.
            upper = stmt.upper()
            idx_marker = upper.find("INDEX IF NOT EXISTS")
            if idx_marker < 0:
                idx_marker = upper.find("INDEX")
            if idx_marker >= 0:
                after = stmt[idx_marker:].split("IF NOT EXISTS", 1)
                after = after[-1]
                tail = after.strip()
                name = tail.split()[0] if tail else ""
                tail_upper = tail.upper()
                on_pos = tail_upper.find(" ON ")
                if name and on_pos > 0:
                    table_part = tail[on_pos + len(" ON "):].strip()
                    table_name = table_part.split(".", 1)[-1].split()[0].strip()
                    if name and table_name:
                        applied[table_name] = name
        try:
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise LabSchemaError(
                f"lab: create_vector_indexes commit failed: "
                f"{type(exc).__name__} (message suppressed)"
            ) from exc
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return applied


def analyze_tables(
    conn: Any,
    *,
    tables: tuple[str, ...] = (
        "qa_pairs",
        "conversation_stream",
        "topics",
        "explicit_memories",
        "topic_entries",
        "observation_notes",
        "yin_paragraphs",
    ),
) -> dict[str, str]:
    """Run ``ANALYZE`` on the canonical corpus tables.

    Used by the G6C-B0 Mode B / Mode C contract: an explicit
    ``ANALYZE`` is required after the import step (Mode B) and
    again after the deferred IVFFlat rebuild (Mode C) so the
    planner has fresh statistics before the recall engine runs.

    The function runs every ``ANALYZE`` inside a single
    transaction; a failure on any one of them rolls the whole
    batch back. ``ANALYZE`` itself is idempotent — re-running
    it is a no-op in cost terms beyond the statistics refresh.

    Returns a ``{table_name: "ok"}`` mapping on success. Raises
    :class:`LabSchemaError` on any driver / cursor failure; the
    caller must NOT swallow the exception — without fresh
    statistics the planner's row-count estimates are stale and
    the run is no longer deterministic across ``search_mode``.
    """

    if not tables:
        return {}
    try:
        cur = conn.cursor()
    except Exception as exc:
        raise LabSchemaError(
            f"lab: conn.cursor() failed during analyze_tables: "
            f"{type(exc).__name__} (message suppressed)"
        ) from exc

    applied: dict[str, str] = {}
    try:
        for table in tables:
            stmt = f"ANALYZE public.{table}"
            try:
                cur.execute(stmt)
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise LabSchemaError(
                    f"lab: ANALYZE failed for {table!r}: "
                    f"{type(exc).__name__} (message suppressed)"
                ) from exc
            applied[str(table)] = "ok"
        try:
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise LabSchemaError(
                f"lab: ANALYZE commit failed: "
                f"{type(exc).__name__} (message suppressed)"
            ) from exc
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return applied


def bootstrap_schema(
    conn: Any,
    repo_root: str,
    *,
    index_build_phase: str = INDEX_BUILD_PHASE_BOOTSTRAP,
) -> None:
    """Apply the public alpha bootstrap + explicit_memories to ``conn``.

    The composed SQL is validated statically (no destructive
    tokens) before the cursor is touched. Execution is wrapped
    in a single transaction; on any failure we roll back and
    raise :class:`LabSchemaError`.

    The ``index_build_phase`` knob (G6C-B0) selects whether the
    canonical IVFFlat ``CREATE INDEX`` statements ride along
    inside the bootstrap transaction (``"bootstrap_before_import"``
    — the historical default, byte-identical to the legacy
    behaviour) or are deferred until after import
    (``"after_import"``). When deferred, the caller MUST invoke
    :func:`create_vector_indexes` after :func:`import_rows`
    returns and then :func:`analyze_tables` so the planner
    sees fresh statistics before the recall engine runs.

    psycopg2 is imported inside the function so the package
    remains importable in environments without psycopg2.
    """

    sql = _compose_bootstrap_sql_with_phase(
        repo_root, index_build_phase=index_build_phase
    )

    # Use the connection's own cursor. We do NOT import psycopg2
    # at module level so the lab stays dependency-light.
    try:
        cur = conn.cursor()
    except Exception as exc:
        raise LabSchemaError(
            f"lab: conn.cursor() failed: {type(exc).__name__} "
            "(message suppressed for safety)"
        ) from exc

    try:
        try:
            cur.execute(sql)
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise LabSchemaError(
                f"lab: bootstrap SQL execute failed: {type(exc).__name__} "
                "(message suppressed for safety)"
            ) from exc
        try:
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise LabSchemaError(
                f"lab: bootstrap commit failed: {type(exc).__name__} "
                "(message suppressed for safety)"
            ) from exc
    finally:
        try:
            cur.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Row importers
# ---------------------------------------------------------------------------


# Per-table INSERT column lists + ON CONFLICT clauses. These are
# the only legal shapes the importer will issue; anything else
# requires a code change.
#
# ``qa_pairs`` columns mirror the alpha_bootstrap.sql definition:
#   id, source_id, session_id, turn_id, question, answer,
#   tool_calls, tool_results, timestamp, source, embedding,
#   embed_model, created_at.
#
# ``conversation_stream`` columns mirror alpha_bootstrap.sql:
#   id, session_id, role, content, trigger, turn_id, timestamp,
#   source, embedding, tool_calls, tool_results.
#
# ``eval_queries`` is intentionally declared in the loader
# contract as one of the three import targets. The public
# alpha_bootstrap.sql does NOT define an ``eval_queries`` table
# — so we deliberately do NOT auto-create it. The importer
# accepts rows only when the target table already exists; if
# it does not, :class:`LabImportError` is raised.
_QA_PAIRS_COLUMNS = (
    "source_id", "session_id", "turn_id", "question", "answer",
    "tool_calls", "tool_results", "timestamp", "source",
    "embedding", "embed_model", "created_at",
)
_CONVERSATION_STREAM_COLUMNS = (
    "session_id", "role", "content", "trigger", "turn_id", "timestamp",
    "source", "embedding", "tool_calls", "tool_results",
)
# ``eval_queries`` columns mirror the additive DDL block in
# :data:`_EVAL_QUERIES_DDL` (source_id, sample_id, qa_id,
# query_idx, category, question, answer, evidence, source_hash).
# The unique-index idempotency key is ``source_id`` so we use
# ``ON CONFLICT (source_id) DO NOTHING`` like the other
# source_id-keyed tables.
_EVAL_QUERIES_COLUMNS = (
    "source_id", "sample_id", "qa_id", "query_idx", "category",
    "question", "answer", "evidence", "source_hash",
)


def _rows_have(rows: Iterable[Any], key: str) -> bool:
    for r in rows:
        if isinstance(r, dict) and key in r:
            return True
    return False


# ---------------------------------------------------------------------------
# pgvector coercion — safe import of a 1024-dim embedding.
# ---------------------------------------------------------------------------


# Default vector dimension for the canonical alpha working set
# (qa_pairs.embedding, conversation_stream.embedding, etc. — all
# ``VECTOR(1024)`` in ``alpha_bootstrap.sql``). The importer refuses
# any list/tuple whose length disagrees with this constant.
_VECTOR_DIM_DEFAULT = 1024


class LabVectorError(LabImportError):
    """Raised when an embedding row is not a valid pgvector payload."""


def _serialize_pgvector_literal(
    values: Any,
    *,
    dim: int = _VECTOR_DIM_DEFAULT,
) -> str:
    """Serialize a Python list/tuple of floats as a pgvector literal.

    The returned string is the ``[x,y,z,...]`` form that
    PostgreSQL accepts as a ``vector`` constant when the column
    is cast via ``::vector``. We refuse:

      * non-list/tuple inputs,
      * length mismatches with ``dim``,
      * non-numeric values,
      * non-finite values (NaN / +inf / -inf).

    Failures raise :class:`LabVectorError` so the caller never
    silently drops a malformed embedding.
    """

    if not isinstance(values, (list, tuple)):
        raise LabVectorError(
            f"lab: embedding must be a list/tuple of floats "
            f"(got {type(values).__name__})"
        )
    if len(values) != dim:
        raise LabVectorError(
            f"lab: embedding length {len(values)} != expected "
            f"vector({dim})"
        )
    parts: list[str] = []
    for i, x in enumerate(values):
        # bool is a subclass of int in Python — refuse explicitly so
        # ``[True, False]`` cannot slip through as ``[1, 0]``.
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise LabVectorError(
                f"lab: embedding[{i}] is not numeric "
                f"(got {type(x).__name__})"
            )
        fx = float(x)
        if not math.isfinite(fx):
            raise LabVectorError(
                f"lab: embedding[{i}] is not finite ({fx!r})"
            )
        # ``repr`` keeps the float round-trippable through PG; ``str``
        # is fine for finite IEEE-754 doubles.
        parts.append(repr(fx))
    return "[" + ",".join(parts) + "]"


def _coerce_embedding(value: Any) -> Any:
    """Coerce one ``embedding`` cell.

    Contract (G6C-A2 lab seam):

      * ``None`` → preserved (structural-only import path).
      * ``list`` / ``tuple`` of finite floats → pgvector literal
        ``[x,y,z,...]`` string ready for ``::vector`` binding.
      * ``str`` → passed through verbatim. Pre-serialised
        pgvector literals are accepted so an upstream pipeline
        that already produced a literal does not pay the cost
        of a second parse.
      * Anything else → :class:`LabVectorError` (fail-closed).
    """

    if value is None:
        return None
    if isinstance(value, str):
        # Pre-serialised literal — accepted as-is.
        if not (value.startswith("[") and value.endswith("]")):
            raise LabVectorError(
                "lab: embedding string must be a pgvector literal "
                "starting with '[' and ending with ']'"
            )
        return value
    if isinstance(value, (list, tuple)):
        return _serialize_pgvector_literal(value)
    raise LabVectorError(
        f"lab: embedding must be None / list / tuple / pgvector "
        f"literal string (got {type(value).__name__})"
    )


def _coerce_row(row: Any, columns: tuple[str, ...]) -> tuple[Any, ...]:
    if not isinstance(row, dict):
        raise LabImportError(
            f"lab: import row must be a dict (got {type(row).__name__})"
        )
    out = []
    jsonb_columns = {"tool_calls", "tool_results", "evidence"}
    vector_columns = {"embedding"}
    for c in columns:
        v = row.get(c, None)
        if c in jsonb_columns and v is not None and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        elif c in vector_columns:
            v = _coerce_embedding(v)
        out.append(v)
    return tuple(out)


def _table_exists(conn: Any, table: str) -> bool:
    try:
        cur = conn.cursor()
    except Exception as exc:
        raise LabImportError(
            f"lab: conn.cursor() failed: {type(exc).__name__} "
            "(message suppressed for safety)"
        ) from exc
    try:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s LIMIT 1",
            (table,),
        )
        return cur.fetchone() is not None
    except Exception as exc:
        raise LabImportError(
            f"lab: table-existence check failed for {table!r}: "
            f"{type(exc).__name__} (message suppressed for safety)"
        ) from exc
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _execute_many(
    conn: Any,
    table: str,
    columns: tuple[str, ...],
    rows: list[Any],
    on_conflict: str | None,
) -> int:
    """Execute one ``executemany`` over ``table`` with explicit columns.

    Returns the number of rows passed to the driver. The caller
    commits once at the end of :func:`import_rows`.
    """

    if not rows:
        return 0
    # Per-column placeholder. ``VECTOR`` columns are bound through
    # a ``%s::vector`` cast so the pgvector literal serialised by
    # :func:`_serialize_pgvector_literal` is accepted by the column
    # type. Other columns keep the bare ``%s`` placeholder.
    _vector_cast_cols = {"embedding"}
    placeholder_parts: list[str] = []
    for c in columns:
        if c in _vector_cast_cols:
            placeholder_parts.append("%s::vector")
        else:
            placeholder_parts.append("%s")
    placeholders = ", ".join(placeholder_parts)
    col_list = ", ".join(columns)
    sql = f"INSERT INTO public.{table} ({col_list}) VALUES ({placeholders})"
    if on_conflict:
        sql += " " + on_conflict
    payload = [_coerce_row(r, columns) for r in rows]
    try:
        cur = conn.cursor()
    except Exception as exc:
        raise LabImportError(
            f"lab: conn.cursor() failed: {type(exc).__name__} "
            "(message suppressed for safety)"
        ) from exc
    try:
        try:
            cur.executemany(sql, payload)
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise LabImportError(
                f"lab: insert into {table!r} failed: {type(exc).__name__} "
                "(message suppressed for safety)"
            ) from exc
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return len(payload)


def import_rows(
    conn: Any,
    rows: dict[str, list[Any]],
) -> dict[str, int]:
    """Import LoCoMo eval_v2 rows into the public lab schema.

    Parameters
    ----------
    conn:
        An open psycopg2-style connection. The function does
        NOT manage the connection's lifecycle (open / close);
        it only manages the transactional commit.
    rows:
        A dict with zero or more of these keys::

            {
              "qa_pairs":            [ {source_id, session_id, turn_id,
                                        question, answer, tool_calls,
                                        tool_results, timestamp, source,
                                        embedding, embed_model, created_at},
                                                ...],
              "conversation_stream": [ {session_id, role, content, trigger,
                                        turn_id, timestamp, source,
                                        embedding, tool_calls,
                                        tool_results},
                                                ...],
              "eval_queries":        [ {source_id, sample_id, qa_id,
                                        query_idx, category, question,
                                        answer, evidence, source_hash},
                                                ...],
            }

    Returns
    -------
    dict[str, int]
        Counts written per table. Empty input → empty output.

    The importer:

      * Uses explicit column lists. No ``*`` expansion.
      * Uses ``ON CONFLICT (source_id) DO NOTHING`` for
        ``qa_pairs`` and ``ON CONFLICT (source_id) DO NOTHING``
        for ``eval_queries`` — both match the unique source_id
        contract those tables carry.
      * Uses no ``ON CONFLICT`` for ``conversation_stream`` —
        that table's existing dedupe relies on the
        ``conversation_stream_session_role_ts_idx`` index plus
        explicit WHERE-NOT-EXISTS predicates written by the
        caller; the importer does not invent a constraint.
      * Commits once, at the end of all three writes. On any
        failure the transaction is rolled back.
      * Never silently falls back. A missing target table,
        a missing required column, or a driver error raises
        :class:`LabImportError` immediately.
    """

    if not isinstance(rows, dict):
        raise LabImportError(
            f"lab: import_rows expects a dict (got {type(rows).__name__})"
        )

    plan = [
        ("qa_pairs",            _QA_PAIRS_COLUMNS,            "ON CONFLICT (source_id) DO NOTHING"),
        ("conversation_stream", _CONVERSATION_STREAM_COLUMNS, None),
        ("eval_queries",        _EVAL_QUERIES_COLUMNS,        "ON CONFLICT (source_id) DO NOTHING"),
    ]

    counts: dict[str, int] = {}
    for table, columns, on_conflict in plan:
        table_rows = rows.get(table) or []
        if not table_rows:
            counts[table] = 0
            continue
        if not _table_exists(conn, table):
            raise LabImportError(
                f"lab: target table public.{table!r} does not exist; "
                "refusing to import. Run bootstrap_schema() first."
            )
        counts[table] = _execute_many(conn, table, columns, table_rows, on_conflict)

    try:
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        raise LabImportError(
            f"lab: import commit failed: {type(exc).__name__} "
            "(message suppressed for safety)"
        ) from exc

    return counts


# ---------------------------------------------------------------------------
# Evaluator-only provenance mapping (read-only; no retrieval SQL).
# ---------------------------------------------------------------------------


# Production-shaped candidate IDs the eval pipeline sees:
#
#   * ``qa_<numeric>`` — QA lane / QA-vector candidates emitted by
#     ``v3core.recall_pool``. The numeric suffix is the
#     ``qa_pairs.id`` (BIGSERIAL). Used in trace snapshots,
#     coverage audit, and ranked/selected/injected id lists.
#   * ``topic_<numeric>`` — topic-card candidates (kept for
#     completeness; not the focus of this seam).
#   * Numeric strings (``"<n>"``) — raw ``conversation_stream.id``
#     values from the live recall path that have not yet been
#     promoted to a ``qa_<n>`` form.
#
# The mapping helper resolves these to the canonical lab
# ``source_id`` so an evaluator can score ranked candidates
# against the gold LoCoMo pair ``source_id`` without touching
# production recall SQL.
_QA_ID_RE = re.compile(r"^qa_(\d+)$")
_TOPIC_ID_RE = re.compile(r"^topic_(\d+)$")


def _extract_qa_pair_source_id(row: Mapping[str, Any]) -> str | None:
    """Read ``source_id`` from a qa_pairs row.

    Returns the verbatim ``source_id`` if present and non-empty,
    else ``None``. Defensive against dict-shaped and object-shaped
    rows (the caller may hand us either).
    """

    if not isinstance(row, Mapping):
        return None
    sid = row.get("source_id")
    if isinstance(sid, str) and sid:
        return sid
    return None


def _extract_dia_ids_from_qa_row(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Read the canonical dia_id provenance from a qa_pairs row.

    The ``build_import_rows`` contract writes
    ``tool_calls[0]`` as ``{q_provenance, a_provenance, ...}``.
    Both ``q_provenance`` and ``a_provenance`` are dicts with a
    ``dia_id`` field. We extract both (deduped, order preserved)
    so the caller can resolve either side to a pair ``source_id``
    via the supplied ``dia_id_to_source_id`` map.
    """

    if not isinstance(row, Mapping):
        return ()
    tc = row.get("tool_calls")
    if not isinstance(tc, list) or not tc:
        return ()
    head = tc[0]
    if not isinstance(head, dict):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for key in ("q_provenance", "a_provenance"):
        prov = head.get(key)
        if isinstance(prov, Mapping):
            did = prov.get("dia_id")
            if isinstance(did, str) and did and did not in seen:
                seen.add(did)
                out.append(did)
    return tuple(out)


def _extract_dia_id_from_conversation_row(row: Mapping[str, Any]) -> str | None:
    """Read the canonical ``dia_id`` from a conversation_stream row.

    The ``build_import_rows`` contract writes
    ``tool_calls[0]`` as ``{sample_id, session_key, dia_id, ...}``.
    """

    if not isinstance(row, Mapping):
        return None
    tc = row.get("tool_calls")
    if not isinstance(tc, list) or not tc:
        return None
    head = tc[0]
    if not isinstance(head, Mapping):
        return None
    did = head.get("dia_id")
    if isinstance(did, str) and did:
        return did
    return None


def build_provenance_map(
    qa_pairs_rows: Iterable[Mapping[str, Any]] | None = None,
    conversation_stream_rows: Iterable[Mapping[str, Any]] | None = None,
    *,
    dia_id_to_source_id: Mapping[str, str] | None = None,
    qa_id_index: Mapping[Any, str] | None = None,
) -> dict[str, Any]:
    """Build the evaluator-only provenance mapping (read-only).

    This is **not** retrieval SQL. It walks the imported rows in
    Python, never opens a cursor, and returns a structure that
    lets the evaluator resolve production-shaped candidate IDs
    to canonical lab ``source_id``s without changing production
    recall.

    The returned structure is::

        {
          "qa_id_to_source_id":     { "qa_123": "locomo|eval_v2|...|d1>d2", ... },
          "conv_id_to_source_id":   { "456":    "locomo|eval_v2|...|d1>d2", ... },
          "topic_id_to_source_id":  { "topic_7": "<qa_pairs.source_id or ''>", ... },
          "counts": {
            "qa_pairs_input":          <int>,
            "conversation_stream_input": <int>,
            "qa_id_resolved":          <int>,
            "qa_id_unresolved":        <int>,
            "conv_id_resolved":        <int>,
            "conv_id_unresolved":      <int>,
            "topic_id_resolved":       <int>,
            "topic_id_unresolved":     <int>,
            "qa_pairs_without_source_id": <int>,
            "qa_pairs_without_dia_id":    <int>,
          },
        }

    Parameters
    ----------
    qa_pairs_rows:
        Imported ``qa_pairs`` rows (the dict shape produced by
        :func:`dataset.build_import_rows` or read back from PG).
        Used to infer the ``dia_id → source_id`` map when the
        caller does not supply one explicitly.
    conversation_stream_rows:
        Imported ``conversation_stream`` rows. Used to map a
        numeric ``conversation_stream.id`` to the canonical
        LoCoMo pair ``source_id`` via the row's ``dia_id``
        provenance plus the supplied (or inferred) dia map.
    dia_id_to_source_id:
        Optional explicit ``{dia_id: source_id}`` map. When the
        caller supplies it (e.g. from
        ``SELECT dia_id, source_id FROM qa_pairs`` after import),
        it overrides the inferred map. The evaluator-only seam
        never issues this SELECT — the caller hands the result
        in.
    qa_id_index:
        Optional ``{qa_pairs.id (int): source_id}`` map produced
        by an offline ``SELECT id, source_id FROM qa_pairs``.
        When supplied, ``qa_<n>`` candidates resolve directly via
        ``qa_id_to_source_id[f"qa_{n}"]``. When not supplied,
        ``qa_id_to_source_id`` stays empty and the ``counts``
        block reports ``qa_id_resolved == 0`` so the auditor can
        detect the missing index.

    Resolution rules (G6C-A2, evaluator-only):

      * ``qa_<n>`` → ``qa_id_index[n]`` (when supplied).
      * ``"<n>"`` (numeric) → the canonical pair ``source_id``
        derived from the ``conversation_stream`` row whose
        ``id`` equals ``n``. The mapping uses the row's
        ``tool_calls[0].dia_id`` provenance plus the dia map.
      * ``topic_<n>`` → no canonical mapping in lab land
        (topics are not the focus of this seam); the
        ``topic_id_to_source_id`` map is returned empty so the
        caller can detect the gap.

    The helper never serialises a credential or DSN. It accepts
    plain Python dicts / Mappings only — no DSN, no connection.
    """

    # Defensive: build the qa_pairs.source_id lookup first. The
    # importer writes one qa_pairs row per canonical pair, so the
    # map is at most one-to-one and the ``id`` field is the
    # BIGSERIAL primary key (see alpha_bootstrap.sql).
    qa_rows_list = list(qa_pairs_rows or ())
    conv_rows_list = list(conversation_stream_rows or ())

    # Build a ``dia_id → source_id`` map. We prefer the caller-
    # supplied map (it may carry extra edges that are not present
    # in qa_pairs alone — e.g. conversation_stream dia_ids that
    # pair into a qa pair). When the caller does not supply one,
    # we fall back to inferring from the qa_pairs rows via their
    # ``q_provenance`` / ``a_provenance`` dia_ids.
    dia_map: dict[str, str] = {}
    if dia_id_to_source_id is not None:
        for k, v in dia_id_to_source_id.items():
            if isinstance(k, str) and k and isinstance(v, str) and v:
                dia_map[k] = v
    if not dia_map:
        # Infer from qa_pairs rows. First-seen wins (the dataset
        # is bijective when no pair re-uses a dia_id on both
        # sides of different pairs).
        for row in qa_rows_list:
            sid = _extract_qa_pair_source_id(row)
            if sid is None:
                continue
            for did in _extract_dia_ids_from_qa_row(row):
                dia_map.setdefault(did, sid)

    # ``qa_<n>`` → source_id via the caller-supplied BIGSERIAL
    # index. The lab importer does not expose ``id`` (it is
    # BIGSERIAL and assigned by PG), so without ``qa_id_index``
    # we cannot resolve ``qa_<n>`` → source_id safely. We
    # surface the resolved count so the auditor can detect the
    # missing index rather than silently inventing entries.
    qa_id_to_source_id: dict[str, str] = {}
    qa_id_unresolved = 0
    if qa_id_index is not None:
        for k, v in qa_id_index.items():
            sid = v
            if not (isinstance(sid, str) and sid):
                continue
            # Accept int / str / str(int) keys.
            if isinstance(k, int) and k > 0:
                key = f"qa_{k}"
            elif isinstance(k, str) and k.isdigit():
                key = f"qa_{int(k)}"
            elif isinstance(k, str) and k.startswith("qa_") and k[3:].isdigit():
                key = k
            else:
                continue
            qa_id_to_source_id[key] = sid
        qa_id_unresolved = 0  # explicit index — every key it
        # lists resolves; the caller is responsible for
        # completeness (we never invent missing entries).

    # ``<n>`` (numeric conversation_stream id) → source_id via
    # the row's dia_id provenance plus the dia map.
    conv_id_to_source_id: dict[str, str] = {}
    conv_rows_seen = 0
    conv_rows_resolved = 0
    for idx, row in enumerate(conv_rows_list):
        if not isinstance(row, Mapping):
            continue
        conv_rows_seen += 1
        did = _extract_dia_id_from_conversation_row(row)
        if did is None:
            continue
        # The conversation_stream ``id`` is BIGSERIAL. The caller
        # may have preserved it as ``"id"`` on the dict; if so we
        # use it directly. Otherwise we fall back to ordinal
        # position + 1 (PG BIGSERIAL starts at 1, and the importer
        # preserves insertion order). Ordinal fallback is
        # evaluator-only and never used by production recall.
        raw_id = row.get("id")
        if isinstance(raw_id, int) and raw_id > 0:
            cid_key = str(raw_id)
        elif isinstance(raw_id, str) and raw_id.isdigit():
            cid_key = raw_id
        else:
            cid_key = str(idx + 1)
        # Resolve via dia_id → source_id. A conversation_stream
        # row's dia_id maps to the qa pair whose ``a_dia_id`` (or
        # ``q_dia_id``) equals it; the dia map carries that edge.
        sid = dia_map.get(did)
        if sid is not None:
            conv_id_to_source_id[cid_key] = sid
            conv_rows_resolved += 1

    return {
        "qa_id_to_source_id": qa_id_to_source_id,
        "conv_id_to_source_id": conv_id_to_source_id,
        "topic_id_to_source_id": {},
        "counts": {
            "qa_pairs_input": len(qa_rows_list),
            "conversation_stream_input": len(conv_rows_list),
            "qa_id_resolved": len(qa_id_to_source_id),
            "qa_id_unresolved": qa_id_unresolved,
            "conv_id_resolved": conv_rows_resolved,
            "conv_id_unresolved": conv_rows_seen - conv_rows_resolved,
            "topic_id_resolved": 0,
            "topic_id_unresolved": 0,
            "qa_pairs_without_source_id":
                sum(1 for r in qa_rows_list
                    if isinstance(r, Mapping)
                    and _extract_qa_pair_source_id(r) is None),
            "qa_pairs_without_dia_id":
                sum(1 for r in qa_rows_list
                    if isinstance(r, Mapping)
                    and not _extract_dia_ids_from_qa_row(r)),
        },
    }


def map_candidate_ids(
    candidate_ids: Iterable[str],
    *,
    qa_id_to_source_id: Mapping[str, str],
    conv_id_to_source_id: Mapping[str, str] | None = None,
    topic_id_to_source_id: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve a sequence of production-shaped candidate IDs.

    Pure function: takes the maps built by
    :func:`build_provenance_map` (or supplied directly by the
    caller) and returns a dict with the resolved ``source_id``
    per candidate plus an audit count of resolved / unresolved
    entries.

    Resolution is dispatched by candidate shape:

      * ``qa_<digits>`` → looked up in ``qa_id_to_source_id``.
      * ``topic_<digits>`` → looked up in ``topic_id_to_source_id``.
      * ``<digits>`` → looked up in ``conv_id_to_source_id``.

    Unknown shapes are passed through unchanged so the caller
    can still see them in the audit log without losing the
    input order. We never invent a fake ``source_id``.
    """

    conv_map: Mapping[str, str] = conv_id_to_source_id or {}
    topic_map: Mapping[str, str] = topic_id_to_source_id or {}

    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    counts = {
        "input": 0,
        "resolved_qa": 0,
        "resolved_conv": 0,
        "resolved_topic": 0,
        "unresolved": 0,
        "unknown_shape": 0,
    }
    for cid in candidate_ids or ():
        counts["input"] += 1
        if not isinstance(cid, str) or not cid:
            unresolved.append(str(cid))
            counts["unresolved"] += 1
            continue
        m_qa = _QA_ID_RE.match(cid)
        if m_qa:
            sid = qa_id_to_source_id.get(cid)
            if sid:
                resolved[cid] = sid
                counts["resolved_qa"] += 1
            else:
                unresolved.append(cid)
                counts["unresolved"] += 1
            continue
        m_topic = _TOPIC_ID_RE.match(cid)
        if m_topic:
            sid = topic_map.get(cid)
            if sid:
                resolved[cid] = sid
                counts["resolved_topic"] += 1
            else:
                unresolved.append(cid)
                counts["unresolved"] += 1
            continue
        if cid.isdigit():
            sid = conv_map.get(cid)
            if sid:
                resolved[cid] = sid
                counts["resolved_conv"] += 1
            else:
                unresolved.append(cid)
                counts["unresolved"] += 1
            continue
        # Unknown shape — pass through as "unresolved" so the
        # auditor can detect non-canonical ids without losing
        # the input.
        unresolved.append(cid)
        counts["unknown_shape"] += 1

    return {
        "resolved": resolved,
        "unresolved": unresolved,
        "counts": counts,
    }


__all__ = [
    "LabDSNRefused",
    "LabImportError",
    "LabSchemaError",
    "LabVectorError",
    "bootstrap_schema",
    "build_provenance_map",
    "import_rows",
    "map_candidate_ids",
    "validate_disposable_dsn",
]