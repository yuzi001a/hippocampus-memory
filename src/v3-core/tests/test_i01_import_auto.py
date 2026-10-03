"""I01 — import_auto end-to-end pipeline (hermetic FakePool).

Covers the §27 gate items that can be proven without a live PG:

  * dry-run = zero writes (no cursor is ever created)
  * live import writes conversation_stream with F2 identity
    (host, session_id, event_id) and qa_pairs with qa_import source_id
  * rerun dedupe is expressed in the SQL (WHERE NOT EXISTS on F2 identity)
  * report shape (§14 fields), written to <profile>/import_report.json only
    on live runs, overwritten (not appended) on re-runs
  * per-source failure isolation → status PARTIAL, others keep going
  * no LLM / no embedding anywhere in the path (no such calls exist here)
"""
from __future__ import annotations

import importlib
import io
import json
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parent / "fixtures" / "i01"
DSH_FIX = FIX / "dsh"
PI_FIX = FIX / "pi"
HERMES_FIX = FIX / "hermes"


# ── minimal FakePool (mirrors tests/test_importers_contract.py) ───────


class _FakeCursor:
    def __init__(self):
        self.statements: list[tuple[str, tuple]] = []
        self._rowcount = 0

    def execute(self, sql: str, params=None) -> None:
        self.statements.append((" ".join(sql.split()), params or ()))
        self._rowcount = 1

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    @property
    def rowcount(self):
        return self._rowcount

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConnection:
    def __init__(self, recorder):
        self._recorder = recorder
        self.commits = 0

    def cursor(self):
        cur = _FakeCursor()
        self._recorder.cursors.append(cur)
        return cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


class _FakeLease:
    def __init__(self, recorder):
        self._conn = _FakeConnection(recorder)

    @property
    def connection(self):
        return self._conn

    def close(self):
        pass


class FakePool:
    def __init__(self):
        self.cursors: list[_FakeCursor] = []

    def lease(self, timeout=None):
        return _FakeLease(self)


@pytest.fixture()
def fake_pool(monkeypatch):
    M = importlib.import_module("v3core.importers")
    pool = FakePool()
    M.install_pool(pool)
    yield pool
    M.install_pool(None)


def _auto():
    M = importlib.import_module("v3core.importers")
    return M


def _run(hosts, overrides, **kw):
    out = io.StringIO()
    report = _auto().import_auto(
        hosts=hosts, overrides=overrides, env={}, platform="win32",
        out=out, **kw,
    )
    return report, out.getvalue()


# ── dry-run ───────────────────────────────────────────────────────────


def test_dry_run_zero_writes(fake_pool, tmp_path):
    report, text = _run(["dsh", "pi"], {"dsh": DSH_FIX, "pi": PI_FIX}, dry_run=True)
    assert fake_pool.cursors == []  # never touched a cursor
    assert report["dry_run"] is True
    assert report["totals"]["raw_imported"] == 0
    dsh = next(s for s in report["sources"] if s["host"] == "dsh")
    assert dsh["messages"] == 6
    assert dsh["found"] is True


def test_dry_run_reports_undiscovered_source(fake_pool, tmp_path):
    report, _ = _run(["hermes"], {"hermes": tmp_path / "missing"}, dry_run=True)
    src = report["sources"][0]
    assert src["found"] is False
    assert src["reason"]


# ── live import ───────────────────────────────────────────────────────


def test_live_import_writes_f2_identity(fake_pool):
    report, _ = _run(["dsh"], {"dsh": DSH_FIX}, dry_run=False)
    assert report["totals"]["raw_imported"] == 6
    assert report["totals"]["qa_pairs_derived"] == 3

    stream_stmts = [
        (sql, params)
        for cur in fake_pool.cursors
        for sql, params in cur.statements
        if "INSERT INTO public.conversation_stream" in sql
    ]
    assert len(stream_stmts) == 6
    normalized = stream_stmts[0][0]
    assert "host" in normalized and "event_id" in normalized
    # F2 dedupe predicate present
    assert "NOT EXISTS" in normalized
    # every insert carries host='dsh' + a non-empty event id
    for sql, params in stream_stmts:
        assert "dsh" in params
        assert any(str(p).startswith("a8a64ad8") or str(p).startswith("0dccca7e")
                   or str(p).startswith("6cb74b85") or str(p).startswith("87ff216a")
                   or str(p).startswith("8850af44") or str(p).startswith("22bfdc0c")
                   for p in params)

    qa_stmts = [
        (sql, params)
        for cur in fake_pool.cursors
        for sql, params in cur.statements
        if "INSERT INTO public.qa_pairs" in sql
    ]
    assert len(qa_stmts) == 3
    for sql, params in qa_stmts:
        joined = " ".join(str(p) for p in params)
        assert "qa_import/dsh/session-" in joined


def test_live_import_hermes_and_pi(fake_pool):
    report, _ = _run(["hermes", "pi"], {"hermes": HERMES_FIX, "pi": PI_FIX}, dry_run=False)
    assert report["totals"]["raw_imported"] == 9 + 6
    assert report["totals"]["qa_pairs_derived"] == 3 + 3
    assert report["status"] == "ok"


# ── report ────────────────────────────────────────────────────────────


def test_report_written_and_overwritten(fake_pool, tmp_path):
    prof = tmp_path / "prof"
    report, _ = _run(["dsh"], {"dsh": DSH_FIX}, dry_run=False, profile_dir=prof)
    f = prof / "import_report.json"
    assert f.exists()
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["dry_run"] is False
    # §14 fields
    for key in ("sources", "totals", "status", "duration_s"):
        assert key in data
    t = data["totals"]
    for key in ("artifacts", "sessions", "messages", "raw_imported",
                "qa_pairs_derived", "duplicates_skipped", "unsupported_skipped",
                "oldest", "newest", "recall_ready"):
        assert key in t, f"missing report total {key}"
    # no secret-looking material
    blob = f.read_text(encoding="utf-8")
    assert "password" not in blob.lower()
    # overwrite, not append
    _run(["dsh"], {"dsh": DSH_FIX}, dry_run=False, profile_dir=prof)
    data2 = json.loads(f.read_text(encoding="utf-8"))
    assert data2["version"] == 1


def test_dry_run_does_not_write_report(fake_pool, tmp_path):
    prof = tmp_path / "prof2"
    _run(["dsh"], {"dsh": DSH_FIX}, dry_run=True, profile_dir=prof)
    assert not (prof / "import_report.json").exists()


# ── failure isolation ─────────────────────────────────────────────────


def test_partial_failure_isolated(fake_pool, tmp_path):
    broken = tmp_path / "state.db"
    broken.write_text("this is not a sqlite database", encoding="utf-8")
    report, _ = _run(
        ["hermes", "dsh"], {"hermes": broken, "dsh": DSH_FIX}, dry_run=False
    )
    assert report["status"] == "partial"
    hermes = next(s for s in report["sources"] if s["host"] == "hermes")
    assert hermes["errors"]  # reported, not silently skipped
    dsh = next(s for s in report["sources"] if s["host"] == "dsh")
    assert dsh["raw_imported"] == 6


def test_live_without_pool_refuses(tmp_path):
    M = _auto()
    M.install_pool(None)
    report, _ = _run(["dsh"], {"dsh": DSH_FIX}, dry_run=False)
    assert report["status"] == "failed"
    assert any("pool" in e.lower() for e in report["errors"])


# ── recall_ready flag ─────────────────────────────────────────────────


def test_recall_ready_true_when_qa_pairs_written(fake_pool):
    report, _ = _run(["dsh"], {"dsh": DSH_FIX}, dry_run=False)
    dsh = next(s for s in report["sources"] if s["host"] == "dsh")
    assert dsh["recall_ready"] is True


def test_recall_ready_false_for_empty_source(fake_pool, tmp_path):
    empty = tmp_path / "empty-pi"
    empty.mkdir()
    (empty / "--x--").mkdir()
    report, _ = _run(["pi"], {"pi": empty}, dry_run=False)
    src = report["sources"][0]
    assert src["qa_pairs_derived"] == 0
    assert src["recall_ready"] is False
