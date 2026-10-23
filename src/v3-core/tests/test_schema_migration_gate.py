"""The upgrade gate: a schema the writer cannot use must be visible everywhere.

Companion to ``test_schema_contract.py`` (which pins the shared contract). These
cases pin the *behaviour* of the three surfaces that reported "fine" during the
2026-10-19 incident, plus the runtime gate that stops a doomed INSERT from
being attempted 126 times:

  * doctor ``_check_schema_version`` must fail when only
    ``qa_pairs.merged_event_ids`` is missing;
  * doctor ``_check_migration_state`` must fail on a ``v0.2``-only ledger;
  * health ``_missing_columns`` must report the exact gap;
  * ``V3Core._flush_pending_qa`` must refuse to write, keep the pending
    marker, and not acknowledge it.

Offline: the psycopg2 connect guard stays intact; these fakes never open a
socket. Real-PostgreSQL coverage of the same rules (plus the upgrade itself)
lives in ``test_schema_old_install_upgrade.py`` and runs only on a disposable
database.
"""
from __future__ import annotations

import pytest

from v3core import schema_contract as sc
from v3core import doctor_full


# ---------------------------------------------------------------------------
# A connection that answers exactly the statements doctor issues.
# ---------------------------------------------------------------------------
class _Cur:
    def __init__(self, conn):
        self._conn = conn
        self._rows: list = []
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        norm = " ".join((sql or "").split()).lower()
        if "from schema_versions order by applied_at" in norm:
            self._one = self._conn.latest_row
            return
        if "select version from schema_versions" in norm:
            self._rows = [(v,) for v in self._conn.ledger]
            return
        if "from information_schema.columns" in norm:
            if params is None:
                # health._missing_columns scans every table in one statement.
                self._rows = [
                    (table, column)
                    for table, columns in self._conn.columns.items()
                    for column in columns
                ]
                return
            table = params[0]
            self._rows = [(c,) for c in self._conn.columns.get(table, ())]
            return
        if "from information_schema.tables" in norm:
            wanted = set(params[0])
            self._rows = [(t,) for t in wanted if t in self._conn.columns]
            return
        raise RuntimeError(f"unmodelled SQL: {sql}")

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._one

    def close(self):
        pass


class FakeConn:
    """``columns`` is {table: [columns]} for the tables that exist."""

    def __init__(self, columns, ledger=(), latest_row=None):
        self.columns = {t: list(c) for t, c in columns.items()}
        self.ledger = tuple(ledger)
        self.latest_row = latest_row or (
            (ledger[-1], "2026-09-18T15:38:36+00:00") if ledger else None
        )

    def cursor(self):
        return _Cur(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def patch_connect(monkeypatch):
    """Route doctor's psycopg2.connect to a supplied fake."""

    def _install(conn):
        import psycopg2
        monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: conn)
        return conn

    return _install


def _schema_map(*, drop_merged=False, drop_tables=()):
    contract = sc.load_contract()
    out = {
        table: list(cols)
        for table, cols in contract.required_columns().items()
        if table not in drop_tables
    }
    if drop_merged and "qa_pairs" in out:
        out["qa_pairs"] = [c for c in out["qa_pairs"] if c != "merged_event_ids"]
    return out


DSN = {"host": "127.0.0.1", "port": 55433, "database": "hpmig",
       "user": "ranger", "password": "x"}


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def test_doctor_schema_check_fails_when_only_merged_event_ids_is_missing(patch_connect):
    conn = FakeConn(_schema_map(drop_merged=True), ledger=sc.LEDGER_REQUIRED_VERSIONS)
    patch_connect(conn)

    result = doctor_full._check_schema_version(DSN, 2.0)

    assert result["status"] == "fail", result
    assert result["evidence"]["missing_columns"] == {
        "qa_pairs": ["merged_event_ids"]
    }
    assert not result["evidence"]["missing_tables"]
    # The operator is pointed at the additive upgrade, not at a rebuild.
    assert "upgrade" in result["evidence"]["upgrade_command"]


def test_doctor_schema_check_passes_with_the_full_contract(patch_connect):
    conn = FakeConn(_schema_map(), ledger=sc.LEDGER_REQUIRED_VERSIONS)
    patch_connect(conn)
    result = doctor_full._check_schema_version(DSN, 2.0)
    assert result["status"] == "ok", result


def test_doctor_migration_state_fails_on_a_v0_2_only_ledger(patch_connect):
    """The exact production shape: ledger says v0.2, everything later is absent."""
    conn = FakeConn(_schema_map(), ledger=("v0.2",))
    patch_connect(conn)

    result = doctor_full._check_migration_state(DSN, 2.0)

    assert result["status"] == "fail", result
    assert "v0.4" in result["detail"]
    assert result["evidence"]["missing"] == ["v0.3", "v0.4"]


def test_doctor_migration_state_passes_at_the_contract_version(patch_connect):
    conn = FakeConn(_schema_map(), ledger=sc.LEDGER_REQUIRED_VERSIONS)
    patch_connect(conn)
    result = doctor_full._check_migration_state(DSN, 2.0)
    assert result["status"] == "ok", result


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
def test_health_reports_the_missing_write_column():
    from v3core.reliability import health

    conn = FakeConn(_schema_map(drop_merged=True))
    contract = sc.load_contract()
    missing = health._missing_columns(conn, contract.required_columns())
    assert missing == {"qa_pairs": ["merged_event_ids"]}


def test_health_reports_nothing_on_a_complete_schema():
    from v3core.reliability import health

    conn = FakeConn(_schema_map())
    contract = sc.load_contract()
    assert health._missing_columns(conn, contract.required_columns()) == {}


def test_health_covers_every_contract_table():
    from v3core.reliability import health

    contract = sc.load_contract()
    assert set(contract.required_tables()) <= set(health.CANONICAL_TABLES)
    # The tables the old list forgot:
    for table in ("memory_relations", "derived_memory_invalidations",
                  "assistant_deliveries", "embedding_failures"):
        assert table in health.CANONICAL_TABLES


# ---------------------------------------------------------------------------
# runtime gate
# ---------------------------------------------------------------------------
class RecordingConn:
    def __init__(self):
        self.statements: list[str] = []

    def cursor(self):
        raise AssertionError("the gate must not issue any SQL")


class RecordingPg:
    def __init__(self):
        self.conn = RecordingConn()
        self.connects = 0

    def _connect(self):
        self.connects += 1
        return self.conn

    def is_connected(self):
        return True


def _make_core(tmp_path):
    from v3core import V3Core

    core = V3Core(profile="default")
    core._config = {}          # no embedding provider → skip the embed phase
    core._pg = RecordingPg()
    return core


def test_flush_is_blocked_and_the_marker_is_kept(tmp_path, monkeypatch):
    core = _make_core(tmp_path)
    blocked = sc.SchemaStatus(
        ready=False,
        contract_version=sc.CONTRACT_VERSION,
        missing_tables=(),
        missing_columns={"qa_pairs": ("merged_event_ids",)},
        write_blocked=True,
        blocked_write_columns={"qa_pairs": ("merged_event_ids",)},
        ledger_versions=("v0.2",),
        missing_ledger_versions=("v0.3", "v0.4"),
        ledger_table_present=True,
    )
    monkeypatch.setattr(core, "_schema_readiness", lambda conn, **kw: blocked)

    acked: list = []
    monkeypatch.setattr(core, "_ack_qa_pending", lambda *a, **k: acked.append(a))

    pending = {"q": "项目代号是什么", "a": "北极星", "q_turn": "T1",
               "q_msg_id": "m1", "q_ts": "2026-10-23T10:00:00+08:00"}
    core._flush_pending_qa("sess-gate", pending)

    assert acked == [], "a blocked write must never be acknowledged"
    assert core._pg.conn.statements == [], "no INSERT must be attempted"
    # The marker is still the caller's to retry: nothing was mutated.
    assert pending["q"] == "项目代号是什么"


def test_flush_does_not_spend_an_embedding_when_the_schema_is_blocked(tmp_path, monkeypatch):
    """A doomed INSERT must not cost an embedding call or an attempt count."""
    import v3core as v3mod

    core = _make_core(tmp_path)
    blocked = _blocked_status()
    monkeypatch.setattr(core, "schema_status", lambda **kw: blocked)

    calls: list = []
    import v3core.embedding as emb_mod
    monkeypatch.setattr(v3mod, "safe_embed_cfg", lambda cfg: {"model": "m", "endpoint": "e"})
    monkeypatch.setattr(emb_mod, "embed_batch", lambda *a, **k: calls.append(a) or [[0.1]])
    monkeypatch.setattr(core, "_mark_qa_embedding_started",
                        lambda *a, **k: pytest.fail("must not touch the marker"))

    pending = {"q": "项目代号", "a": "北极星", "q_turn": "T1", "q_msg_id": "m1",
               "q_ts": "2026-10-23T10:00:00+08:00"}
    core._flush_pending_qa("sess-early", pending)

    assert calls == [], "no embedding request may be sent for a row we cannot store"


def test_gate_caches_a_healthy_verdict_and_reprobes_a_blocked_one(monkeypatch):
    core = _make_core(None)
    calls: list[int] = []

    good = sc.SchemaStatus(
        ready=True, contract_version=sc.CONTRACT_VERSION, missing_tables=(),
        missing_columns={}, write_blocked=False, blocked_write_columns={},
        ledger_versions=sc.LEDGER_REQUIRED_VERSIONS, missing_ledger_versions=(),
        ledger_table_present=True,
    )
    bad = sc.SchemaStatus(
        ready=False, contract_version=sc.CONTRACT_VERSION, missing_tables=(),
        missing_columns={"qa_pairs": ("merged_event_ids",)},
        write_blocked=True,
        blocked_write_columns={"qa_pairs": ("merged_event_ids",)},
        ledger_versions=("v0.2",), missing_ledger_versions=("v0.3", "v0.4"),
        ledger_table_present=True,
    )

    def _probe(conn, **kw):
        calls.append(1)
        return bad if len(calls) == 1 else good

    monkeypatch.setattr(sc, "probe", _probe)

    first = core._schema_readiness(object(), max_age=60)
    assert first.write_blocked
    # A blocked verdict is re-probed once the max_age window elapses ...
    second = core._schema_readiness(object(), max_age=0)
    assert not second.write_blocked
    # ... and a healthy verdict is then cached for the process lifetime.
    third = core._schema_readiness(object(), max_age=60)
    assert third.ready
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# quarantined markers: a missing column is our fault, not the user's
# ---------------------------------------------------------------------------
def _blocked_status() -> "sc.SchemaStatus":
    return sc.SchemaStatus(
        ready=False, contract_version=sc.CONTRACT_VERSION, missing_tables=(),
        missing_columns={"qa_pairs": ("merged_event_ids",)},
        write_blocked=True,
        blocked_write_columns={"qa_pairs": ("merged_event_ids",)},
        ledger_versions=("v0.2",), missing_ledger_versions=("v0.3", "v0.4"),
        ledger_table_present=True,
    )


def _ready_status() -> "sc.SchemaStatus":
    return sc.SchemaStatus(
        ready=True, contract_version=sc.CONTRACT_VERSION, missing_tables=(),
        missing_columns={}, write_blocked=False, blocked_write_columns={},
        ledger_versions=sc.LEDGER_REQUIRED_VERSIONS, missing_ledger_versions=(),
        ledger_table_present=True,
    )


def _write_marker(tmp_path, *, error_type: str, status: str = "poisoned",
                  attempts: int = 3) -> "object":
    import json

    d = tmp_path / "j" / "pending_qa"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "marker.json"
    payload = {
        "version": 1, "job_id": "job-1", "session_id": "sess-q",
        "pending": {"q": "项目代号是什么", "a": "北极星", "q_turn": "T1",
                    "q_msg_id": "m1", "q_ts": "2026-10-23T10:00:00+08:00"},
        "embedding_status": status, "embedding_attempts": attempts,
        "retryable": True, "error_type": error_type,
        "error_class": "db_write_failure", "error_phase": "db_write",
        "failure_count": 2,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _core_with_dir(tmp_path):
    from v3core import V3Core

    core = V3Core(profile="default")
    core._config = {"basePath": str(tmp_path)}
    core._pg = RecordingPg()
    # The recovery path starts a worker; keep it from doing real work here.
    core._flush_worker_loop = lambda: None
    core._flush_queue = __import__("queue").Queue()
    return core


def test_a_structural_poison_is_revived_once_the_schema_is_repaired(tmp_path, monkeypatch):
    import json

    core = _core_with_dir(tmp_path)
    path = _write_marker(tmp_path, error_type="UndefinedColumn")
    monkeypatch.setattr(core, "schema_status", lambda **kw: _ready_status())

    core._recover_pending_qa()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["embedding_status"] == "failed", "the poison must be cleared"
    assert data["embedding_attempts"] == 0
    assert data["embedding_next_retry_at"] is None
    assert data["revived_from_error_type"] == "UndefinedColumn"
    assert data["revived_at"]
    # History is kept: we do not rewrite what happened.
    assert data["error_type"] == "UndefinedColumn"
    assert data["failure_count"] == 2


def test_a_structural_poison_is_not_revived_while_the_schema_still_blocks(tmp_path, monkeypatch):
    import json

    core = _core_with_dir(tmp_path)
    path = _write_marker(tmp_path, error_type="UndefinedTable")
    monkeypatch.setattr(core, "schema_status", lambda **kw: _blocked_status())

    core._recover_pending_qa()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["embedding_status"] == "poisoned"
    assert data["embedding_attempts"] == 3
    assert "revived_at" not in data


def test_a_non_structural_poison_stays_quarantined_even_when_the_schema_is_healthy(tmp_path, monkeypatch):
    import json

    core = _core_with_dir(tmp_path)
    path = _write_marker(tmp_path, error_type="DataError")
    monkeypatch.setattr(core, "schema_status", lambda **kw: _ready_status())

    core._recover_pending_qa()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["embedding_status"] == "poisoned"
    assert "revived_at" not in data
