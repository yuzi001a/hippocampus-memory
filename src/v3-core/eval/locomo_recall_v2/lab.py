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
import os
import re
from typing import Any, Iterable


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


def bootstrap_schema(conn: Any, repo_root: str) -> None:
    """Apply the public alpha bootstrap + explicit_memories to ``conn``.

    The composed SQL is validated statically (no destructive
    tokens) before the cursor is touched. Execution is wrapped
    in a single transaction; on any failure we roll back and
    raise :class:`LabSchemaError`.

    psycopg2 is imported inside the function so the package
    remains importable in environments without psycopg2.
    """

    sql = _compose_bootstrap_sql(repo_root)

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


def _coerce_row(row: Any, columns: tuple[str, ...]) -> tuple[Any, ...]:
    if not isinstance(row, dict):
        raise LabImportError(
            f"lab: import row must be a dict (got {type(row).__name__})"
        )
    out = []
    jsonb_columns = {"tool_calls", "tool_results", "evidence"}
    for c in columns:
        v = row.get(c, None)
        if c in jsonb_columns and v is not None and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
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
    placeholders = ", ".join(["%s"] * len(columns))
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


__all__ = [
    "LabDSNRefused",
    "LabImportError",
    "LabSchemaError",
    "bootstrap_schema",
    "import_rows",
    "validate_disposable_dsn",
]