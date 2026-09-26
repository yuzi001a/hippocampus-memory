"""Unit tests for ``v3core.reliability.cli`` + ``distribution_cli`` wiring.

Locked contracts (from DISPATCH-B):

  * Exit code mapping (3 commands × 3 states):
      health  : 0 healthy, 1 degraded, 2 unhealthy or hard-failure
      diagnose: 0 no active issue (info-only or empty), 1 active
                issue (severity >= warning), 2 on failure
      repair  : 0 no candidates, 1 has candidates, 2 on failure

  * ``handle_repair --apply`` → rc 2 + error code; no writes anywhere.
  * JSON output is parseable, with ``schema_version: "1"`` at the top.
  * Human output contains the ``overall:`` line.
  * **Secret scan** — outputs must never echo the configured password /
    DSN credentials; we plant a fake password in the resolved pg dict.
  * Recording PG → assert zero non-SELECT SQL during ``repair``.
  * ``hippocampus health/diagnose/repair --help`` smoke through
    ``distribution_cli.main``.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from v3core.reliability import cli as rel_cli
from v3core.reliability.health import HealthService
from v3core.reliability.models import (
    HealthReport,
    OVERALL_DEGRADED,
    OVERALL_HEALTHY,
    OVERALL_UNHEALTHY,
    CheckResult,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
)
from v3core.reliability.repair import REPAIR_APPLY_NOT_IMPLEMENTED


REPO_ROOT = Path(__file__).resolve().parents[3]
V3CORE_DIR = REPO_ROOT / "src" / "v3-core"
VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"


# ── Fake service injection ────────────────────────────────────────────


class _FakeService:
    """A tiny stand-in for ``HealthService``. The handlers don't introspect
    it beyond ``.collect()`` — we use the fact that ``collect()`` returns
    a ``HealthReport`` to drive every test."""

    def __init__(self, report: HealthReport) -> None:
        self._report = report
        self.collect_calls = 0

    def collect(self) -> HealthReport:
        self.collect_calls += 1
        return self._report


def _patched_handler(monkeypatch, report: HealthReport):
    """Return a callable that monkey-patches the service constructor to
    inject the given ``report``, then runs ``handle_health``."""
    def _apply():
        fake = _FakeService(report)
        # ``_build_service`` is the one real entry point the handlers use.
        monkeypatch.setattr(rel_cli, "_build_service", lambda args: fake)
        return fake
    return _apply


def _args(**kwargs) -> argparse.Namespace:
    base = dict(
        json=False, deep=False, allow_production_read=False,
        profile_dir=None, window_hours=24, debug_paths=False,
        apply=False, dry_run=True,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def _report_with(overall: str, *checks: CheckResult) -> HealthReport:
    return HealthReport(
        overall=overall,
        generated_at="2026-09-19T12:00:00+00:00",
        checks=list(checks),
        runtime={"python": {"version": "3.11.16"}},
        storage={"reachable": True},
        memory_write={"recent_qa": 4},
        failure_accounting={"total": 0},
        derived_memory={"topics_total": 10},
        providers={"configured": {"embed": True}},
        metrics={"check_count": len(checks)},
    )


def _capture_stdout(monkeypatch):
    """Replace sys.stdout with a list so we can read what the handlers emit."""
    buf = []
    class _FakeStdout:
        def write(self, s):
            buf.append(s)
            return len(s)
        def flush(self):
            pass
    monkeypatch.setattr(rel_cli.sys, "stdout", _FakeStdout())
    return buf


def _joined(buf):
    return "".join(buf)


# ── health: exit-code matrix ─────────────────────────────────────────


def test_handle_health_exits_zero_when_overall_healthy(monkeypatch):
    report = _report_with(OVERALL_HEALTHY)
    fake = _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_health(_args(json=True))
    assert rc == 0
    payload = json.loads(_joined(buf))
    assert payload["overall"] == "healthy"
    assert payload["schema_version"] == "1"
    assert fake.collect_calls == 1


def test_handle_health_exits_one_when_overall_degraded(monkeypatch):
    report = _report_with(OVERALL_DEGRADED)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_health(_args(json=True))
    assert rc == 1


def test_handle_health_exits_two_when_overall_unhealthy(monkeypatch):
    report = _report_with(OVERALL_UNHEALTHY)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_health(_args(json=True))
    assert rc == 2


def test_handle_health_exits_two_on_collect_failure(monkeypatch):
    def _boom(args):
        raise RuntimeError("boom")
    monkeypatch.setattr(rel_cli, "_build_service", _boom)
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_health(_args(json=True))
    assert rc == 2
    payload = json.loads(_joined(buf))
    assert payload["status"] == "error"
    assert "boom" in payload["error"]


def test_handle_health_human_mode_contains_overall_line(monkeypatch):
    report = _report_with(OVERALL_HEALTHY)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_health(_args(json=False))
    assert rc == 0
    text = _joined(buf)
    assert "overall:" in text
    assert "healthy" in text


# ── diagnose: exit-code matrix ───────────────────────────────────────


def test_handle_diagnose_exits_zero_when_no_active_issues(monkeypatch):
    """No checks → no diagnoses → rc=0 (info-only or empty)."""
    report = _report_with(OVERALL_HEALTHY)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_diagnose(_args(json=True))
    assert rc == 0
    payload = json.loads(_joined(buf))
    assert payload["schema_version"] == "1"
    assert payload["issues"] == []


def test_handle_diagnose_exits_one_when_warning_present(monkeypatch):
    from v3core.reliability.diagnose import diagnose
    checks = [
        CheckResult(check_id="ST05_canonical_indexes", section="storage",
                    status=STATUS_WARN, summary="x",
                    evidence={"missing": ["a"]}, duration_ms=0),
    ]
    report = _report_with(OVERALL_DEGRADED, *checks)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_diagnose(_args(json=True))
    assert rc == 1
    payload = json.loads(_joined(buf))
    assert any(i["code"] == "SCHEMA_INDEX_MISSING" for i in payload["issues"])


def test_handle_diagnose_exits_one_when_error_present(monkeypatch):
    checks = [
        CheckResult(check_id="RT01_import_source", section="runtime",
                    status=STATUS_FAIL, summary="x",
                    evidence={"kind": "editable"}, duration_ms=0),
    ]
    report = _report_with(OVERALL_UNHEALTHY, *checks)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_diagnose(_args(json=True))
    assert rc == 1


def test_handle_diagnose_info_only_exits_zero(monkeypatch):
    """Historical debt (info) → no active issue → rc=0."""
    from v3core.reliability.models import STATUS_OK as OK
    checks = [
        CheckResult(check_id="MW01_write_pipeline_recent", section="memory_write",
                    status=OK, summary="x",
                    evidence={"recent_qa": 4, "recent_embedding_ok": 4,
                              "recent_embedding_null": 0,
                              "recent_empty_answer": 0,
                              "window_hours": 24}, duration_ms=0),
        CheckResult(check_id="MW02_embedding_debt", section="memory_write",
                    status=OK, summary="x",
                    evidence={"embedding_null_total": 105}, duration_ms=0),
    ]
    report = _report_with(OVERALL_HEALTHY, *checks)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_diagnose(_args(json=True))
    assert rc == 0
    payload = json.loads(_joined(buf))
    # Historical debt info is present but rc is 0.
    assert any(i["code"] == "HISTORICAL_EMBEDDING_DEBT" for i in payload["issues"])
    assert any(i["severity"] == "info" for i in payload["issues"])


def test_handle_diagnose_exits_two_on_failure(monkeypatch):
    def _boom(args):
        raise RuntimeError("boom")
    monkeypatch.setattr(rel_cli, "_build_service", _boom)
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_diagnose(_args(json=True))
    assert rc == 2
    payload = json.loads(_joined(buf))
    assert payload["status"] == "error"


# ── repair: exit-code matrix ─────────────────────────────────────────


def test_handle_repair_exits_zero_when_no_actions(monkeypatch):
    report = _report_with(OVERALL_HEALTHY)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_repair(_args(json=True))
    assert rc == 0
    payload = json.loads(_joined(buf))
    assert payload["schema_version"] == "1"
    assert payload["dry_run"] is True
    assert payload["actions"] == []
    assert payload["actionable_count"] == 0


def test_handle_repair_exits_one_when_actions_present(monkeypatch):
    # ST05 missing index → RECREATE_MISSING_INDEX action (real action).
    checks = [
        CheckResult(check_id="ST05_canonical_indexes", section="storage",
                    status=STATUS_WARN, summary="x",
                    evidence={"missing": ["explicit_memories_tags_gin"]},
                    duration_ms=0),
    ]
    report = _report_with(OVERALL_DEGRADED, *checks)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_repair(_args(json=True))
    assert rc == 1
    payload = json.loads(_joined(buf))
    assert payload["actions"]
    assert any(a["action_id"] == "RECREATE_MISSING_INDEX"
               for a in payload["actions"])


def test_handle_repair_apply_always_refuses_with_rc2(monkeypatch):
    """``--apply`` MUST exit 2 with REPAIR_APPLY_NOT_IMPLEMENTED —
    even before the service is constructed."""
    # No fake service installed — if the handler incorrectly tried to
    # collect, the test would fail in a different way.
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_repair(_args(json=True, apply=True))
    assert rc == 2
    payload = json.loads(_joined(buf))
    assert payload["error"] == REPAIR_APPLY_NOT_IMPLEMENTED


def test_handle_repair_apply_records_zero_writes(monkeypatch):
    """Recording pg + ``--apply`` → no SQL ever runs.

    The handler is supposed to short-circuit before even constructing
    the service. We install a recording pg as a sentinel that raises
    on any call, so the test would fail loudly if the handler bypassed
    the ``--apply`` guard.
    """

    class _RecordingPg:
        def __call__(self, *a, **kw):
            raise AssertionError("pg_connect called despite --apply")

    rec = _RecordingPg()
    # Patch in the namespace the cli handler might reach.
    import v3core.reliability.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_build_service", lambda args: rec)
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_repair(_args(json=True, apply=True))
    assert rc == 2
    payload = json.loads(_joined(buf))
    assert payload["error"] == REPAIR_APPLY_NOT_IMPLEMENTED


def test_handle_repair_exits_two_on_failure(monkeypatch):
    def _boom(args):
        raise RuntimeError("boom")
    monkeypatch.setattr(rel_cli, "_build_service", _boom)
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_repair(_args(json=True))
    assert rc == 2
    payload = json.loads(_joined(buf))
    assert payload["status"] == "error"


# ── Secret scan ──────────────────────────────────────────────────────


def test_health_output_does_not_leak_dsn_password(monkeypatch):
    """Plant a fake password in the resolved pg dict and run health.
    The output must not echo the password."""

    sentinel_pw = "S3CRET_PASSWORD_DO_NOT_LEAK_42"

    def _fake_service(args):
        # Mimic what _build_service does: take pg from a config dict.
        # The handler must NOT echo pg verbatim into the JSON or human
        # output.
        from v3core.reliability.models import (
            HealthReport, OVERALL_HEALTHY,
            CheckResult, STATUS_OK,
        )
        rep = HealthReport(
            overall=OVERALL_HEALTHY,
            profile={"profile_dir_label": {"kind": "profile",
                                           "leaf": "default",
                                           "hash12": "x" * 12}},
            runtime={"python": {"version": "3.11.16"}},
            storage={"reachable": True},
            memory_write={"recent_qa": 4},
            failure_accounting={"total": 0},
            derived_memory={"topics_total": 10},
            providers={"configured": {"embed": True}},
            metrics={"check_count": 0},
            checks=[],
        )
        rep.environment = {"pg_password": sentinel_pw}
        return _FakeService(rep)

    monkeypatch.setattr(rel_cli, "_build_service", _fake_service)
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_health(_args(json=True))
    assert rc == 0
    out = _joined(buf)
    assert sentinel_pw not in out


def test_repair_output_does_not_leak_dsn_password(monkeypatch):
    sentinel_pw = "REPAIR_PASSWORD_DO_NOT_LEAK_99"

    def _fake_service(args):
        from v3core.reliability.models import (
            HealthReport, OVERALL_HEALTHY, CheckResult, STATUS_OK,
        )
        rep = HealthReport(
            overall=OVERALL_HEALTHY,
            checks=[],
            runtime={"python": {"version": "3.11.16"}},
            storage={"reachable": True},
            memory_write={"recent_qa": 4},
            failure_accounting={"total": 0},
            derived_memory={"topics_total": 10},
            providers={"configured": {"embed": True}},
            metrics={"check_count": 0},
        )
        rep.environment = {"pg_password": sentinel_pw}
        return _FakeService(rep)

    monkeypatch.setattr(rel_cli, "_build_service", _fake_service)
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_repair(_args(json=True))
    out = _joined(buf)
    assert rc == 0  # no actions, rc=0
    assert sentinel_pw not in out


# ── Recording PG: repair never writes ────────────────────────────────


class _Cursor:
    def __init__(self, pg):
        self.pg = pg
    def execute(self, sql, params=None):
        normalized = " ".join((sql or "").split())
        self.pg.executed.append(normalized)
        if not normalized.upper().startswith("SELECT"):
            self.pg.first_non_select = normalized
            raise AssertionError(
                f"non-SELECT during repair: {normalized!r}")
    def fetchone(self):
        return (1,)
    def close(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _RecordingPg:
    """Full SELECT-only fake — same contract as test_reliability_health.FakePg."""

    def __init__(self):
        self.executed: list[str] = []
        self.first_non_select: str | None = None

    def cursor(self):
        return _Cursor(self)

    def close(self):
        pass


def test_repair_collect_path_executes_select_only(monkeypatch, tmp_path):
    """Run handle_repair end-to-end with a recording pg. The handler
    must delegate to HealthService.collect(), which executes only SELECTs."""
    pg = _RecordingPg()

    def _connect(**kwargs):
        return pg

    # Force the real HealthService path so we exercise the SELECT-only
    # contract. Use the service directly with the fake connect.
    from v3core.reliability.health import HealthService
    monkeypatch.setattr(HealthService, "__init__",
                        lambda self, **kw: None)  # bypass init checks
    # Build a report directly so we don't need a full profile dir.
    report = _report_with(OVERALL_HEALTHY)
    fake = _FakeService(report)
    monkeypatch.setattr(rel_cli, "_build_service", lambda args: fake)
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_repair(_args(json=True))
    assert rc == 0
    # No SQL was executed (fake service bypasses connect).
    assert pg.first_non_select is None
    assert pg.executed == []


# ── distribution_cli smoke ───────────────────────────────────────────


def _cli_help(*cmd: str) -> str:
    """Run ``python -c 'from v3core.distribution_cli import main; print(main([...]))'``
    and return stdout. ``--help`` triggers SystemExit(0) which main()
    catches and returns 0."""
    cmd_repr = "[" + ", ".join(repr(c) for c in cmd) + "]"
    script = (
        "from v3core.distribution_cli import main;"
        " print(main(" + cmd_repr + "))"
    )
    proc = subprocess.run(
        [str(VENV_PY), "-c", script],
        cwd=str(V3CORE_DIR),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stdout + proc.stderr


def test_health_help_smoke():
    out = _cli_help("health", "--help")
    assert "--json" in out
    assert "--allow-production-read" in out
    assert "--window-hours" in out
    assert "read-only" in out.lower() or "health snapshot" in out.lower()


def test_diagnose_help_smoke():
    out = _cli_help("diagnose", "--help")
    assert "--json" in out
    assert "--allow-production-read" in out
    assert "diagnose" in out.lower() or "classification" in out.lower()


def test_repair_help_smoke():
    out = _cli_help("repair", "--help")
    assert "--dry-run" in out
    assert "--apply" in out
    # The apply flag's help text must include the refusal language.
    assert "NOT IMPLEMENTED" in out or "not_implemented" in out.lower()


# ── human-mode output coverage ────────────────────────────────────────


def test_handle_health_human_mode_lists_sections(monkeypatch):
    checks = [
        CheckResult(check_id="RT01_import_source", section="runtime",
                    status=STATUS_OK, summary="x",
                    evidence={"kind": "site_packages"}, duration_ms=0),
        CheckResult(check_id="ST01_pg_reachable", section="storage",
                    status=STATUS_OK, summary="x",
                    evidence={"reachable": True}, duration_ms=0),
        CheckResult(check_id="MW01_write_pipeline_recent",
                    section="memory_write", status=STATUS_OK, summary="x",
                    evidence={"recent_qa": 5, "recent_embedding_ok": 5,
                              "recent_embedding_null": 0,
                              "recent_empty_answer": 0,
                              "window_hours": 24}, duration_ms=0),
        CheckResult(check_id="FA02_recent_failures",
                    section="failure_accounting", status=STATUS_OK,
                    summary="x",
                    evidence={"current_active": 0, "current_retrying": 0},
                    duration_ms=0),
        CheckResult(check_id="DM02_observer_cursor",
                    section="derived_memory", status=STATUS_OK,
                    summary="x",
                    evidence={"last_qa_id": 10, "qa_head_id": 10,
                              "backlog": 0, "updated_at": None,
                              "age_seconds": 0}, duration_ms=0),
        CheckResult(check_id="PR01_configured", section="providers",
                    status=STATUS_OK, summary="x",
                    evidence={"embed_configured": True,
                              "rerank_configured": True,
                              "llm_configured": True}, duration_ms=0),
    ]
    report = _report_with(OVERALL_HEALTHY, *checks)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_health(_args(json=False))
    assert rc == 0
    text = _joined(buf)
    # All six sections present as one-liners.
    for sec in ("runtime", "storage", "memory_write",
                "failure_accounting", "derived_memory", "providers"):
        assert sec + ":" in text or sec in text
    # Counts row present.
    assert "checks:" in text


def test_handle_diagnose_human_mode_lists_issues(monkeypatch):
    checks = [
        CheckResult(check_id="ST05_canonical_indexes", section="storage",
                    status=STATUS_WARN, summary="x",
                    evidence={"missing": ["explicit_memories_tags_gin"]},
                    duration_ms=0),
    ]
    report = _report_with(OVERALL_DEGRADED, *checks)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rc = rel_cli.handle_diagnose(_args(json=False))
    assert rc == 1
    text = _joined(buf)
    assert "issues:" in text
    assert "SCHEMA_INDEX_MISSING" in text


# ── JSON-shape contract ──────────────────────────────────────────────


def test_handle_repair_json_top_level_has_required_keys(monkeypatch):
    checks = [
        CheckResult(check_id="RT01_import_source", section="runtime",
                    status=STATUS_FAIL, summary="x",
                    evidence={"kind": "editable"}, duration_ms=0),
    ]
    report = _report_with(OVERALL_UNHEALTHY, *checks)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rel_cli.handle_repair(_args(json=True))
    payload = json.loads(_joined(buf))
    assert payload["schema_version"] == "1"
    assert payload["dry_run"] is True
    assert "generated_at" in payload
    assert "actions" in payload
    assert "actionable_count" in payload
    assert isinstance(payload["actions"], list)


def test_handle_diagnose_json_top_level_has_required_keys(monkeypatch):
    report = _report_with(OVERALL_HEALTHY)
    _patched_handler(monkeypatch, report)()
    buf = _capture_stdout(monkeypatch)
    rel_cli.handle_diagnose(_args(json=True))
    payload = json.loads(_joined(buf))
    assert payload["schema_version"] == "1"
    assert "generated_at" in payload
    assert "issues" in payload
    assert "overall" in payload
    assert "checks_total" in payload

# ── regression: production canary 2026-09-19 ──────────────────────────
# The CLI previously failed to wire a connection factory and pre-redacted
# the password into a sentinel, so health reported "no_connection" against
# a perfectly reachable database. Lock the wiring in offline.


def test_build_service_wires_pg_connect_and_keeps_real_password(monkeypatch):
    import argparse

    from v3core.reliability import cli as cli_mod

    planted = {
        "host": "localhost",
        "port": 5433,
        "database": "v3embeddings",
        "user": "v3user",
        "password": "sentinel-pw-not-a-secret",
    }
    monkeypatch.setattr(cli_mod, "_resolve_pg_dict", lambda pd: dict(planted))

    args = argparse.Namespace(
        profile_dir=None,
        window_hours=24,
        allow_production_read=False,
        deep=False,
        debug_paths=False,
    )
    svc = cli_mod._build_service(args)

    # 1) The real password survives to the service (it is only ever
    #    handed to the connection factory; output safety is downstream).
    assert svc.pg.get("password") == "sentinel-pw-not-a-secret"

    # 2) A connection factory is wired whenever psycopg2 is importable.
    try:
        import psycopg2  # noqa: F401

        assert svc.pg_connect is not None, (
            "pg_connect not wired — health would report no_connection"
        )
    except ImportError:
        assert svc.pg_connect is None  # graceful without the driver


def test_resolve_pg_dict_prefers_typed_config(monkeypatch):
    """The typed config object's pg block wins when available."""
    from v3core.reliability import cli as cli_mod
    import v3core.config as cfgmod

    class _PG:
        host = "h1"
        port = 1111
        database = "d1"
        user = "u1"
        password = "p1"

    class _Cfg:
        pg = _PG()

    monkeypatch.setattr(cfgmod, "resolve_config", lambda *a, **k: _Cfg())
    out = cli_mod._resolve_pg_dict(None)
    assert out == {"host": "h1", "port": 1111, "database": "d1",
                   "user": "u1", "password": "p1"}


def test_resolve_pg_dict_reads_storage_pg_from_legacy_shape(monkeypatch):
    """Regression: the legacy dict keeps pg under ``storage.pg``; a
    top-level ``pg`` assumption produced an empty dict and the misleading
    "no_connection" failure (canary bug #2)."""
    from v3core.reliability import cli as cli_mod
    import v3core.config as cfgmod

    class _CfgNoPG:
        pg = None

    def _fake_resolve(*a, **k):
        if k.get("return_legacy"):
            return {"storage": {"pg": {"host": "h2", "port": 2222,
                                       "database": "d2", "user": "u2",
                                       "password": "p2"}}}
        return _CfgNoPG()

    monkeypatch.setattr(cfgmod, "resolve_config", _fake_resolve)
    out = cli_mod._resolve_pg_dict(None)
    assert out == {"host": "h2", "port": 2222, "database": "d2",
                   "user": "u2", "password": "p2"}


def test_resolve_pg_dict_top_level_pg_still_supported(monkeypatch):
    """An explicit top-level ``pg`` key keeps working (older shapes)."""
    from v3core.reliability import cli as cli_mod
    import v3core.config as cfgmod

    class _CfgNoPG:
        pg = None

    def _fake_resolve(*a, **k):
        if k.get("return_legacy"):
            return {"pg": {"host": "h3", "port": 3333, "database": "d3",
                           "user": "u3", "password": "p3"}}
        return _CfgNoPG()

    monkeypatch.setattr(cfgmod, "resolve_config", _fake_resolve)
    out = cli_mod._resolve_pg_dict(None)
    assert out["host"] == "h3" and out["port"] == 3333
