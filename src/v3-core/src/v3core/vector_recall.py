"""Helpers for reliable pgvector IVFFlat recall."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("v3core.vector_recall")

# SQL identifiers interpolated by query_with_exact_fallback must be selected from
# this fixed set. Callers use unqualified names and set their normal search_path.
_ALLOWED_TABLES = frozenset(
    {
        "explicit_memories",
        "qa_pairs",
        "conversation_stream",
        "topics",
        "topic_entries",
        "observation_notes",
        "yin_paragraphs",
    }
)
_LISTS_RE = re.compile(r"\blists\s*=\s*['\"]?([0-9]+)", re.IGNORECASE)


def _debug(log: Any, message: str, *args: Any, exc_info: bool = False) -> None:
    target = logger if log is None else log
    try:
        debug = getattr(target, "debug", None)
        if callable(debug):
            debug(message, *args, exc_info=exc_info)
    except Exception:
        # Diagnostic logging must never make connection setup fail.
        pass


def _connection_in_transaction(conn: Any) -> bool:
    """Conservatively detect a transaction without importing a DB driver."""
    status = getattr(conn, "status", None)
    in_transaction = getattr(status, "in_transaction", None)
    if in_transaction is not None:
        return bool(in_transaction)

    get_status = getattr(conn, "get_transaction_status", None)
    if callable(get_status):
        try:
            # psycopg2.extensions.TRANSACTION_STATUS_IDLE is zero.
            return int(get_status()) != 0
        except Exception:
            return True
    return False


def _ivfflat_stats(conn: Any) -> tuple[float, int]:
    """Return summed table row estimates and the largest declared IVFFlat lists."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tbl.oid, GREATEST(tbl.reltuples, 0)::double precision, "
            "pg_get_indexdef(idx.oid) "
            "FROM pg_index AS ix "
            "JOIN pg_class AS tbl ON tbl.oid = ix.indrelid "
            "JOIN pg_class AS idx ON idx.oid = ix.indexrelid "
            "JOIN pg_am AS am ON am.oid = idx.relam "
            "WHERE am.amname = 'ivfflat'"
        )
        records = cur.fetchall()

    rows_by_table: dict[Any, float] = {}
    list_limits: list[int] = []
    for table_oid, row_estimate, index_definition in records:
        rows_by_table[table_oid] = max(
            rows_by_table.get(table_oid, 0.0), float(row_estimate or 0.0)
        )
        match = _LISTS_RE.search(str(index_definition or ""))
        if match:
            list_limits.append(int(match.group(1)))
    return sum(rows_by_table.values()), max(list_limits, default=100)


def configure_connection_recall(
    conn: Any,
    *,
    small_rows: int = 20_000,
    small_probes: int | None = None,
    log: Any = None,
) -> None:
    """Set IVFFlat probes to the index-list ceiling for small indexed datasets.

    Catalog inspection and ``SET`` are best-effort: absent pgvector, unsupported
    connection wrappers, or any other setup error are debug-logged and ignored.
    Existing transactions are left completely untouched. On an idle DB-API
    connection with autocommit disabled, autocommit is temporarily enabled so the
    catalog read and session-level setting do not leave a transaction open.
    """
    if conn is None:
        return

    changed_autocommit = False
    try:
        if _connection_in_transaction(conn):
            _debug(log, "skip IVFFlat recall setup on connection with an open transaction")
            return

        if getattr(conn, "autocommit", None) is False:
            conn.autocommit = True
            changed_autocommit = True

        estimated_rows, list_limit = _ivfflat_stats(conn)
        if estimated_rows >= int(small_rows):
            _debug(
                log,
                "keep default IVFFlat probes: estimated_rows=%s threshold=%s",
                estimated_rows,
                small_rows,
            )
            return

        probes = list_limit if small_probes is None else int(small_probes)
        if probes < 1:
            raise ValueError("small_probes must be a positive integer")
        with conn.cursor() as cur:
            # probes is an integer derived from trusted catalog text or the
            # explicit integer argument; PostgreSQL SET cannot bind a parameter.
            cur.execute(f"SET ivfflat.probes = {probes}")
    except Exception:
        _debug(log, "best-effort IVFFlat recall setup failed", exc_info=True)
    finally:
        if changed_autocommit:
            try:
                conn.autocommit = False
            except Exception:
                _debug(log, "could not restore autocommit after IVFFlat setup", exc_info=True)


def with_recall_configuration(connect: Callable[[], Any]) -> Callable[[], Any]:
    """Wrap a PgPool connection factory with best-effort recall configuration."""
    def _connect() -> Any:
        conn = connect()
        configure_connection_recall(conn)
        return conn

    return _connect


def query_with_exact_fallback(
    cur: Any,
    sql: Any,
    params: Any,
    *,
    table: str,
    context: str = "vector",
) -> tuple[list[Any], bool]:
    """Run an ANN query and retry an empty result without index scans.

    When the cursor belongs to an autocommit connection, this helper starts and
    finishes a short transaction so SET LOCAL applies to the retry only. An
    existing caller transaction is never committed or rolled back here.
    """
    if not isinstance(table, str) or table not in _ALLOWED_TABLES:
        raise ValueError(f"unsupported vector fallback table: {table!r}")

    conn = getattr(cur, "connection", None)
    own_transaction = bool(getattr(conn, "autocommit", False))
    if own_transaction:
        try:
            own_transaction = not _connection_in_transaction(conn)
        except Exception:
            own_transaction = False
    transaction_started = False
    try:
        if own_transaction:
            cur.execute("BEGIN")
            transaction_started = True

        cur.execute(sql, params)
        rows = cur.fetchall()
        if rows:
            if transaction_started:
                conn.commit()
            return rows, False

        cur.execute("SET LOCAL enable_indexscan = off")
        cur.execute(sql, params)
        fallback_rows = cur.fetchall()
        cur.execute(f"SELECT EXISTS (SELECT 1 FROM {table})")
        table_has_rows = bool(cur.fetchone()[0])
        if table_has_rows:
            logger.warning(
                "vector ANN returned no rows for nonempty table; exact fallback used "
                "table=%s context=%s",
                table,
                context,
            )
        if transaction_started:
            conn.commit()
        return fallback_rows, True
    except Exception:
        if transaction_started:
            try:
                conn.rollback()
            except Exception:
                logger.debug("failed to rollback exact vector fallback transaction", exc_info=True)
        raise
