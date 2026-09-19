"""Runtime-integrity integration tests: health checks, diagnose codes,
and the repair planner action — all driven through injected processes and
fake site-packages (no real production env is touched)."""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

from v3core.reliability.diagnose import diagnose
from v3core.reliability.health import HealthService
from v3core.reliability.models import STATUS_FAIL, STATUS_OK, STATUS_SKIP
from v3core.reliability.repair import plan_repairs
from v3core.runtime_integrity.models import ProcessInfo


def _write_package(root: Path, marker: str) -> Path:
    pkg = root / "v3core"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        "# fake v3core\nMARKER = %r\n" % marker, encoding="utf-8", newline="\n"
    )
    (pkg / "embed_chunks.py").write_text("# chunk helper\n", encoding="utf-8", newline="\n")
    (pkg / "embedding.py").write_text("# embedding\n", encoding="utf-8", newline="\n")
    di = root / "v3_core-4.0.0.dist-info"
    di.mkdir(parents=True, exist_ok=True)
    (di / "METADATA").write_text(
        "Name: v3-core\nVersion: 4.0.0\n", encoding="utf-8", newline="\n"
    )
    return pkg


def _make_wheel(dst: Path, marker: str) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w") as z:
        z.writestr("v3core/__init__.py", "# fake v3core\nMARKER = %r\n" % marker)
        z.writestr("v3core/embed_chunks.py", "# chunk helper\n")
        z.writestr("v3core/embedding.py", "# embedding\n")
        z.writestr("v3_core-4.0.0.dist-info/METADATA", "Name: v3-core\nVersion: 4.0.0\n")
    return dst


def _proc(pythonpath: str, pid: int = 5001, role: str = "serve") -> ProcessInfo:
    return ProcessInfo(
        role=role, pid=pid, ppid=1, name="python.exe",
        executable=sys.executable, cmdline="python -m hermes_cli.main serve",
        cwd=None, pythonpath=pythonpath, virtual_env="",
        path_present=True, path_entry_count=4, started_at="2099-01-01 00:00:00",
    )


def _svc(tmp_path, *, processes, extra_roots, approved_wheel=None, enabled=True) -> HealthService:
    return HealthService(
        pg=None,
        runtime_integrity_enabled=enabled,
        approved_wheel=str(approved_wheel) if approved_wheel else None,
        runtime_extra_roots=extra_roots,
        runtime_processes=processes,
        now=1_700_000_000.0,
    )


def _check(report, check_id):
    return next(c for c in report.checks if c.check_id == check_id)


# ── health integration ────────────────────────────────────────────────────


def test_health_runtime_integrity_healthy_match(tmp_path):
    sp = tmp_path / "venv_sp"
    _write_package(sp, "APPROVED")
    wheel = _make_wheel(tmp_path / "rel" / "v3_core-4.0.0-py3-none-any.whl", "APPROVED")
    svc = _svc(tmp_path, processes=[_proc(str(sp))], extra_roots=[sp], approved_wheel=wheel)
    report = svc.collect()
    assert _check(report, "RT07_runtime_approved_match").status == STATUS_OK
    assert _check(report, "RT08_runtime_shadow_detected").status == STATUS_OK
    assert _check(report, "RT09_runtime_live_processes").status == STATUS_OK
    ri = report.to_dict()["runtime_integrity"]
    assert ri["shadow_detected"] is False
    assert ri["live_processes_checked"] == 1
    assert ri["duplicate_install_count"] == 0


def test_health_runtime_integrity_shadowed_fails(tmp_path):
    venv_sp = tmp_path / "venv_sp"
    _write_package(venv_sp, "LEGACY")
    rt_sp = tmp_path / "runtime_sp"
    _write_package(rt_sp, "APPROVED")
    wheel = _make_wheel(tmp_path / "rel" / "v3_core-4.0.0-py3-none-any.whl", "APPROVED")
    svc = _svc(
        tmp_path,
        processes=[_proc(f"{venv_sp}{os.pathsep}{rt_sp}")],
        extra_roots=[venv_sp, rt_sp],
        approved_wheel=wheel,
    )
    report = svc.collect()
    assert _check(report, "RT07_runtime_approved_match").status == STATUS_FAIL
    assert _check(report, "RT08_runtime_shadow_detected").status == STATUS_FAIL
    ri = report.to_dict()["runtime_integrity"]
    assert ri["shadow_detected"] is True
    assert "verdict" in ri and ri["verdict"] == "SHADOWED_APPROVED_INSTALL"


def test_health_runtime_integrity_disabled_skips(tmp_path):
    svc = _svc(tmp_path, processes=[], extra_roots=[], enabled=False)
    report = svc.collect()
    for cid in ("RT05_runtime_duplicates", "RT06_runtime_active_install",
                "RT07_runtime_approved_match", "RT08_runtime_shadow_detected",
                "RT09_runtime_live_processes"):
        assert _check(report, cid).status == STATUS_SKIP


def test_health_no_processes_warns_rt09(tmp_path):
    sp = tmp_path / "sp"
    _write_package(sp, "X")
    svc = _svc(tmp_path, processes=[], extra_roots=[sp])
    report = svc.collect()
    assert _check(report, "RT09_runtime_live_processes").status != STATUS_OK


# ── diagnose integration ──────────────────────────────────────────────────


def test_diagnose_emits_shadowed_code(tmp_path):
    venv_sp = tmp_path / "venv_sp"
    _write_package(venv_sp, "LEGACY")
    rt_sp = tmp_path / "runtime_sp"
    _write_package(rt_sp, "APPROVED")
    wheel = _make_wheel(tmp_path / "rel" / "v3_core-4.0.0-py3-none-any.whl", "APPROVED")
    svc = _svc(
        tmp_path,
        processes=[_proc(f"{venv_sp}{os.pathsep}{rt_sp}")],
        extra_roots=[venv_sp, rt_sp],
        approved_wheel=wheel,
    )
    report = svc.collect()
    diags = diagnose(report)
    codes = {d.code for d in diags}
    assert "RUNTIME_SHADOWED_INSTALL" in codes
    d = next(d for d in diags if d.code == "RUNTIME_SHADOWED_INSTALL")
    assert d.severity == "error"
    assert d.repairable is True


def test_diagnose_emits_duplicate_info_only(tmp_path):
    a = tmp_path / "sp_a"
    _write_package(a, "APPROVED")
    b = tmp_path / "sp_b"
    _write_package(b, "APPROVED")
    wheel = _make_wheel(tmp_path / "rel" / "v3_core-4.0.0-py3-none-any.whl", "APPROVED")
    svc = _svc(tmp_path, processes=[_proc(f"{a}{os.pathsep}{b}")], extra_roots=[a, b], approved_wheel=wheel)
    diags = diagnose(svc.collect())
    codes = {d.code for d in diags}
    # healthy duplicate: no shadow/mismatch codes at all
    assert "RUNTIME_SHADOWED_INSTALL" not in codes
    assert "RUNTIME_RELEASE_MISMATCH" not in codes


# ── repair planner integration ────────────────────────────────────────────


def test_repair_planner_aligns_runtime(tmp_path):
    venv_sp = tmp_path / "venv_sp"
    _write_package(venv_sp, "LEGACY")
    rt_sp = tmp_path / "runtime_sp"
    _write_package(rt_sp, "APPROVED")
    wheel = _make_wheel(tmp_path / "rel" / "v3_core-4.0.0-py3-none-any.whl", "APPROVED")
    svc = _svc(
        tmp_path,
        processes=[_proc(f"{venv_sp}{os.pathsep}{rt_sp}")],
        extra_roots=[venv_sp, rt_sp],
        approved_wheel=wheel,
    )
    diags = diagnose(svc.collect())
    actions = plan_repairs(diags)
    align = [a for a in actions if a.action_id == "ALIGN_ACTIVE_RUNTIME_TO_APPROVED_RELEASE"]
    assert align, f"expected ALIGN action, got: {[a.action_id for a in actions]}"
    a = align[0]
    # the planner dedups by action_id: co-occurring codes are comma-joined
    # (shadow + mismatch both map to this action)
    assert "RUNTIME_SHADOWED_INSTALL" in a.issue_code
    assert a.writes_database is False
    assert a.reversible is True
    assert a.risk == "high"
    assert "restart" in a.reason.lower()
    assert "environment" in a.reason.lower()
