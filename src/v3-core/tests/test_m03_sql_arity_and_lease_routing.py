# -*- coding: utf-8 -*-
"""M03 — SQL-arity and lease-routing contracts a fake PG cannot see.

The Y400 real-PostgreSQL E2E caught three P0 defects in the SHIPPED M03 code
that the in-memory fake PG (and therefore every unit test) was blind to,
because the fake never asks PostgreSQL to validate the SQL or the lease:

  A. ``derived_invalidation._discover`` ran ``int8range @> %s`` with a Python
     int. psycopg2 adapts that as ``integer`` (int4); ``int8range @> integer``
     does not exist, so real PG raises ``UndefinedFunction`` and propagation
     never writes a single invalidation row under any config.
  B. ``derived_invalidation._lease`` / ``_write_invalidations`` and
     ``derived_rebuild._write_rebuilt`` passed the single injected target into
     ``_acquire_lease(None, pg)``. A real ``PgPool`` therefore landed in the
     ``pg`` slot, took the ``_PgStoreLeaseAdapter`` path, and got a ``PgLease``
     where a DBAPI connection was expected (``PgLease.__enter__`` returns the
     lease) — every ``.cursor()`` raised and suppression failed OPEN.
  C. The invalidation INSERT listed 12 target columns but 15 VALUES
     expressions (12 ``%s`` + ``NOW(), NULL, NULL``), so real PG raised
     ``INSERT has more expressions than target columns`` — the shipped writer
     had never once succeeded against a real table.

These assertions judge the ACTUAL SQL the shipped code renders (captured off a
recording lease) and the ACTUAL lease routing decision, so arity drift, a
missing cast, or a slot regression fails in CI instead of only on real PG.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any, Optional

MODULE = "v3core.derived_invalidation"
CANONICAL_MODULE = "v3core.active_memory_store"
PG_POOL_MODULE = "v3core.pg_pool"
REBUILD_MODULE = "v3core.derived_rebuild"

REPO_ROOT = Path(__file__).resolve().parents[3]
V3CORE_SRC = REPO_ROOT / "src" / "v3-core" / "src" / "v3core"

_INVALIDATION_TABLE = "public.derived_memory_invalidations"


# ── recording lease (captures the exact SQL the shipped code renders) ─────

class _RecordingCursor:
    def __init__(self, conn: "_RecordingConn") -> None:
        self._conn = conn
        self.rowcount = -1

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *_a: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = ()) -> None:
        self._conn.sql.append((sql, tuple(params or ())))
        self.rowcount = 1

    def fetchone(self) -> Optional[tuple]:
        if self._conn.next_row is None:
            return None
        return (self._conn.next_row,)

    def fetchall(self) -> list:
        return []


class _RecordingConn:
    """Minimal DBAPI-shaped connection that records every executed SQL."""

    def __init__(self) -> None:
        self.sql: list = []
        self.autocommit = True
        self.closed = False
        self.next_row: Any = "inv_recording"

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self)

    def get_transaction_status(self) -> int:
        return 0

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _RecordingLease:
    def __init__(self, conn: _RecordingConn) -> None:
        self.connection = conn
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> _RecordingConn:
        return self.connection

    def __exit__(self, *_a: Any) -> bool:
        self.close()
        return False


class _RecordingStore:
    """Store-shaped target (NOT a ``PgPool``): ``lease()`` yields the conn."""

    def __init__(self) -> None:
        self.conn = _RecordingConn()
        self.leases = 0

    def lease(self, timeout: Optional[float] = None, deadline: Any = None) -> _RecordingLease:
        self.leases += 1
        return _RecordingLease(self.conn)


# ── SQL parsing helpers ────────────────────────────────────────────────────

_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+(?P<table>[\w\.]+)\s*\((?P<cols>[^)]*)\)\s*"
    r"VALUES\s*\((?P<vals>.*?)\)\s*ON\s+CONFLICT",
    re.IGNORECASE | re.DOTALL,
)


def _top_level_split(inner: str) -> list:
    """Split a VALUES list on commas that are NOT inside parentheses."""
    parts: list = []
    depth = 0
    current = ""
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


def _mod():
    return importlib.import_module(MODULE)


def _capture_insert_sql() -> tuple:
    """Drive ``_write_invalidations`` and return ``(match, sql)``."""
    mod = _mod()
    store = _RecordingStore()
    row = {
        "invalidation_id": "inv_recording",
        "correction_id": "cor_recording",
        "relation_id": "rel_recording",
        "derived_kind": "topic",
        "derived_id": "t_d1",
        "source_memory_id": "mem_a",
        "replacement_memory_id": "mem_b",
        "state": mod.STATE_STALE,
        "reason": None,
        "canonical_qa_id": 42,
        "input_source_kind": "conversation_stream",
        "input_source_id": "11",
    }
    mod._write_invalidations(store, [row])
    for sql, _params in store.conn.sql:
        match = _INSERT_RE.search(sql)
        if match:
            return match, sql
    raise AssertionError("no invalidation INSERT was rendered")


def _capture_observation_sql() -> str:
    """Drive ``_discover`` and return the containment query it rendered."""
    mod = _mod()
    store = _RecordingStore()
    mod._discover(store, [42])
    for sql, _params in store.conn.sql:
        if "source_qa_range" in sql.lower():
            return sql
    raise AssertionError("no observation_notes containment query was rendered")


# ── defect C: INSERT columns and VALUES expressions must have equal arity ──

def test_M03_invalidation_insert_has_one_value_per_target_column():
    match, sql = _capture_insert_sql()
    assert match.group("table").lower() == _INVALIDATION_TABLE
    columns = [c.strip() for c in match.group("cols").split(",") if c.strip()]
    values = _top_level_split(match.group("vals"))
    assert columns, f"no target columns parsed from:\n{sql}"
    assert len(values) == len(columns), (
        f"INSERT lists {len(columns)} target columns but {len(values)} VALUES "
        f"expressions — real PostgreSQL rejects this with "
        f"'INSERT has more expressions than target columns':\n{sql}"
    )
    # Every value must be a bound placeholder: no hidden literal (NOW(), NULL)
    # may stand in for a column the caller is expected to bind.
    assert values == ["%s"] * len(columns), (
        f"VALUES must bind exactly one %s per column, got {values!r}:\n{sql}"
    )


def test_M03_invalidation_insert_binds_every_declared_insert_column():
    match, sql = _capture_insert_sql()
    mod = _mod()
    declared = list(mod._INVALIDATION_INSERT_COLUMNS)
    columns = [c.strip() for c in match.group("cols").split(",") if c.strip()]
    assert columns == declared, (
        f"the INSERT must bind exactly _INVALIDATION_INSERT_COLUMNS "
        f"({declared!r}), got {columns!r}"
    )


# ── defect A: int8range containment needs an explicit bigint cast ──────────

def test_M03_observation_containment_casts_the_qa_id_to_bigint():
    sql = _capture_observation_sql()
    normalised = " ".join(sql.split())
    assert "observation_notes" in normalised.lower()
    assert "source_qa_range @>" in normalised.lower().replace(" ", " "), (
        f"containment query changed shape unexpectedly:\n{sql}"
    )
    assert "%s::bigint" in normalised.lower(), (
        "source_qa_range is int8range; a bare int4 %s makes real PostgreSQL "
        "raise 'operator does not exist: int8range @> integer'. The bound "
        f"parameter must be cast:\n{sql}"
    )


# ── defect B: lease routing must send one target to the right slot ─────────

def _pg_pool_class():
    return importlib.import_module(PG_POOL_MODULE).PgPool


def test_M03_lease_slots_sends_a_real_pgpool_to_the_pool_slot():
    canonical = importlib.import_module(CANONICAL_MODULE)
    pool = _pg_pool_class()(
        connect=lambda: _RecordingConn(), max_connections=2, min_connections=0,
    )
    assert canonical._lease_slots(pool) == (pool, None), (
        "a real PgPool must go to the pool slot — in the pg slot it takes the "
        "_PgStoreLeaseAdapter path and yields a PgLease as a connection"
    )


def test_M03_lease_slots_keeps_a_store_shaped_target_in_the_pg_slot():
    canonical = importlib.import_module(CANONICAL_MODULE)
    store = _RecordingStore()
    assert canonical._lease_slots(store) == (None, store), (
        "a store-shaped target must stay in the pg slot so the "
        "open_side_connection seam / adapter path is used"
    )


def test_M03_lease_slots_of_none_is_none_none():
    canonical = importlib.import_module(CANONICAL_MODULE)
    assert canonical._lease_slots(None) == (None, None)


def test_M03_store_with_open_side_connection_still_takes_the_seam_path():
    """A PgEmbedStore-shaped target keeps using its independent-connection seam."""
    mod = _mod()
    side_conn = _RecordingConn()
    leased_conn = _RecordingConn()

    class _StoreWithSeam:
        def __init__(self) -> None:
            self.seam_calls = 0
            self.lease_calls = 0

        def open_side_connection(self) -> _RecordingConn:
            self.seam_calls += 1
            return side_conn

        def lease(self, timeout: Optional[float] = None, deadline: Any = None) -> _RecordingLease:
            self.lease_calls += 1
            return _RecordingLease(leased_conn)

    store = _StoreWithSeam()
    with mod._lease(store) as conn:
        assert conn is side_conn, (
            "a store exposing open_side_connection must be leased through the "
            "seam, not the shared lease() connection"
        )
    assert store.seam_calls == 1
    assert store.lease_calls == 0


def test_M03_lease_on_a_real_pgpool_yields_a_cursorable_connection():
    """The defect-B mechanism, driven through the real PgPool/PgLease pair."""
    mod = _mod()
    pool = _pg_pool_class()(
        connect=_RecordingConn, max_connections=2, min_connections=0,
    )
    try:
        with mod._lease(pool) as conn:
            # Pre-fix this was a PgLease: AttributeError: 'PgLease' object
            # has no attribute 'cursor'.
            cur = conn.cursor()
            cur.execute("SELECT 1")
    finally:
        try:
            pool.shutdown(timeout=1)
        except Exception:  # pragma: no cover - teardown must not mask result
            pass


def test_M03_derived_modules_route_their_single_target_through_lease_slots():
    """Source-level guard: the three call sites must not reintroduce the bug."""
    for name in ("derived_invalidation.py", "derived_rebuild.py"):
        text = (V3CORE_SRC / name).read_text(encoding="utf-8")
        assert "_acquire_lease(None," not in text, (
            f"{name} still injects the single target into the pg slot"
        )
        assert "_lease_slots(" in text, (
            f"{name} must route its single target through _lease_slots"
        )
