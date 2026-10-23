"""Schema contract: one description of the schema, shared by every surface.

Why these tests exist
---------------------
On 2026-10-19 a running install wrote ``qa_pairs`` rows that included
``merged_event_ids``; the database had never received the column, so every
flush failed with 42703 — 126 logged attempts, no row — while

  * ``schema_versions`` held a ``v0.2`` row (bookkeeping looked fine),
  * ``hippocampus doctor`` reported the schema OK, and
  * ``reliability.health`` reported the canonical tables present.

Each surface carried its own hand-written list of "the schema" and each list
was incomplete in a different way. These tests pin the invariant that makes the
drift detectable: the shipped DDL is the single source, and every diagnostic is
a superset of what the writer actually needs.

All offline — ``tests/conftest.py`` hard-blocks ``psycopg2.connect``.
"""
from __future__ import annotations

import pytest

from v3core import schema_contract as sc


# ---------------------------------------------------------------------------
# Fakes — a connection that answers only the two probe statements.
# ---------------------------------------------------------------------------
class _Cur:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        norm = " ".join((sql or "").split()).lower()
        if "from information_schema.columns" in norm:
            self._rows = list(self._conn.column_rows)
            return
        if "from schema_versions" in norm:
            if self._conn.ledger_error:
                raise RuntimeError(self._conn.ledger_error)
            self._rows = [(v,) for v in self._conn.ledger]
            return
        raise RuntimeError(f"unmodelled SQL: {sql}")

    def fetchall(self):
        return list(getattr(self, "_rows", []))


class FakeConn:
    def __init__(self, columns=None, ledger=(), ledger_error=""):
        self.column_rows = list(columns or [])
        self.ledger = tuple(ledger)
        self.ledger_error = ledger_error
        self.rolled_back = False

    def cursor(self):
        return _Cur(self)

    def rollback(self):
        self.rolled_back = True


def _full_schema(contract=None):
    contract = contract or sc.load_contract()
    return [
        (table, column)
        for table, columns in contract.required_columns().items()
        for column in columns
    ]


# ---------------------------------------------------------------------------
# The contract itself
# ---------------------------------------------------------------------------
def test_contract_is_derived_from_the_shipped_ddl():
    contract = sc.load_contract()
    assert contract.version == sc.CONTRACT_VERSION
    # Every table the DDL declares is required ...
    assert "qa_pairs" in contract.required_tables()
    assert "assistant_deliveries" in contract.required_tables()
    assert "derived_memory_invalidations" in contract.required_tables()
    assert "embedding_failures" in contract.required_tables()
    # ... and the writer's column really is in it. This single assertion is the
    # one that would have failed on 2026-10-18.
    assert "merged_event_ids" in contract.required_columns()["qa_pairs"]


def test_optional_tables_are_never_required():
    contract = sc.load_contract()
    for table in sc.OPTIONAL_TABLES:
        assert table not in contract.required_tables()
    # note_segments is the concrete case: no shipped DDL creates it, and the
    # reader degrades on UndefinedTable — it must not be demanded of installs.
    assert "note_segments" not in contract.required_tables()


def test_every_required_table_is_reachable_on_an_old_install():
    """A table must be either a v0.2 base table or created by the upgrade."""
    contract = sc.load_contract()
    base_tables = set(sc.V0_2_BASE_COLUMNS)
    created = set(contract.upgrade_created_tables)
    unreachable = [
        t for t in contract.required_tables()
        if t not in base_tables and t not in created
    ]
    assert not unreachable, (
        "these tables exist in the DDL but no upgrade path creates them: "
        f"{unreachable}"
    )


def test_every_column_is_reachable_on_an_old_install():
    """Every DDL column is base (pre-existing) or brought by an upgrade guard.

    This is the anti-drift gate: adding a column to alpha_bootstrap.sql without
    a matching ``ADD COLUMN IF NOT EXISTS`` in upgrade_v0_2.sql fails here,
    instead of shipping a schema existing installs cannot reach.
    """
    contract = sc.load_contract()
    unreachable: dict[str, list[str]] = {}
    for table in contract.required_tables():
        if table in contract.upgrade_created_tables:
            continue  # CREATE TABLE carries every column
        base = set(sc.V0_2_BASE_COLUMNS.get(table, ()))
        altered = set(contract.upgrade_added_columns.get(table, ()))
        gap = [c for c in contract.tables[table].columns if c not in base | altered]
        if gap:
            unreachable[table] = gap
    assert not unreachable, (
        "these columns exist in the DDL but neither the v0.2 baseline nor the "
        f"upgrade provides them: {unreachable}"
    )


def test_v0_2_baseline_contains_no_phantom_columns():
    contract = sc.load_contract()
    for table, columns in sc.V0_2_BASE_COLUMNS.items():
        ddl = set(contract.tables[table].columns)
        phantom = [c for c in columns if c not in ddl]
        assert not phantom, f"{table}: baseline names columns the DDL lacks: {phantom}"


def test_write_requirements_are_part_of_the_contract():
    contract = sc.load_contract()
    required = contract.required_columns()
    for table, columns in sc.WRITE_REQUIRED_COLUMNS.items():
        assert table in required, f"{table} is a write target but not in the contract"
        missing = [c for c in columns if c not in required[table]]
        assert not missing, f"{table}: writer needs columns outside the contract: {missing}"


def test_diagnostics_are_never_weaker_than_the_writer():
    """doctor / upgrade / health must cover at least the write requirement."""
    from v3core import distribution_cli as dc
    from v3core.reliability import health

    contract = sc.load_contract()
    required = contract.required_columns()

    # upgrade dry-run diff
    for table, columns in required.items():
        assert table in dc._UPGRADE_TABLE_REQUIRED_COLUMNS, (
            f"upgrade dry-run does not look at {table}"
        )
        assert set(columns) <= set(dc._UPGRADE_TABLE_REQUIRED_COLUMNS[table])

    # health canonical tables / ledger revision
    assert set(contract.required_tables()) <= set(health.CANONICAL_TABLES)
    assert health.EXPECTED_SCHEMA_VERSION == sc.CONTRACT_VERSION

    # the write-critical columns specifically
    for table, columns in sc.WRITE_REQUIRED_COLUMNS.items():
        assert set(columns) <= set(dc._UPGRADE_TABLE_REQUIRED_COLUMNS[table])


# ---------------------------------------------------------------------------
# probe(): the negative controls this incident needed
# ---------------------------------------------------------------------------
def test_probe_ready_when_everything_is_present():
    contract = sc.load_contract()
    conn = FakeConn(_full_schema(contract), ledger=contract.ledger_versions)
    status = sc.probe(conn, contract=contract)
    assert status.ready
    assert not status.write_blocked
    assert status.missing_tables == ()
    assert status.missing_columns == {}
    assert status.missing_ledger_versions == ()
    assert "satisfied" in status.summary()


def test_probe_only_merged_event_ids_missing_blocks_writes():
    """The production shape: every table and column except one write column."""
    contract = sc.load_contract()
    columns = [
        (t, c) for t, c in _full_schema(contract)
        if not (t == "qa_pairs" and c == "merged_event_ids")
    ]
    conn = FakeConn(columns, ledger=contract.ledger_versions)
    status = sc.probe(conn, contract=contract)

    assert not status.ready
    assert status.write_blocked
    assert status.missing_columns == {"qa_pairs": ("merged_event_ids",)}
    assert not status.missing_tables
    # The message an operator sees names the exact column.
    assert "qa_pairs.merged_event_ids" in status.summary()
    assert status.as_dict()["missing_columns"]["qa_pairs"] == ["merged_event_ids"]


def test_probe_stale_ledger_with_complete_structure_is_not_migrated():
    """A v0.2 row alone is bookkeeping, not proof of a current schema."""
    contract = sc.load_contract()
    conn = FakeConn(_full_schema(contract), ledger=("v0.2",))
    status = sc.probe(conn, contract=contract)

    assert status.ready  # structure is genuinely complete
    assert status.ledger_behind
    assert status.missing_ledger_versions == ("v0.3", "v0.4")
    assert not status.write_blocked


def test_probe_missing_qa_pairs_table_blocks_writes():
    contract = sc.load_contract()
    columns = [(t, c) for t, c in _full_schema(contract) if t != "qa_pairs"]
    conn = FakeConn(columns, ledger=contract.ledger_versions)
    status = sc.probe(conn, contract=contract)
    assert status.write_blocked
    assert "qa_pairs" in status.missing_tables


def test_probe_missing_ledger_table_is_reported_not_fatal():
    contract = sc.load_contract()
    conn = FakeConn(_full_schema(contract), ledger_error="UndefinedTable")
    status = sc.probe(conn, contract=contract)
    assert status.ready  # structure is what decides readiness
    assert not status.ledger_table_present
    assert status.missing_ledger_versions == contract.ledger_versions


def test_probe_failure_never_blocks_writes():
    """A broken diagnostic must not become a memory outage."""
    contract = sc.load_contract()

    class Exploding(FakeConn):
        def cursor(self):
            raise RuntimeError("connection lost")

    status = sc.probe(Exploding(), contract=contract)
    assert not status.ready
    assert not status.write_blocked
    assert "connection lost" in status.probe_error


def test_no_sql_keyword_is_parsed_as_a_column():
    """A multi-line CHECK clause must not contribute columns.

    ``explicit_memories`` ends with a CHECK whose continuation line starts with
    ``AND``; recording that as a column makes every install look permanently
    un-migrated.
    """
    contract = sc.load_contract()
    keywords = {"AND", "OR", "NOT", "CHECK", "CONSTRAINT", "PRIMARY", "UNIQUE",
                "FOREIGN", "REFERENCES", "DEFAULT"}
    offenders = [
        (table, column)
        for table, spec in contract.tables.items()
        for column in spec.columns
        if column.upper() in keywords
    ]
    assert not offenders, offenders
    assert "embed_model" in contract.required_columns()["explicit_memories"]


def test_contract_manifest_matches_the_contract():
    manifest = sc.contract_manifest()
    contract = sc.load_contract()
    assert manifest["contract_version"] == contract.version
    assert manifest["digest"] == contract.digest()
    assert set(manifest["required_tables"]) == set(contract.required_tables())
