"""Lab matrix + simulation tests for the runtime-integrity layer.

Per the round's task book §22-27/§37: F1-F10 fixtures (the increments not
already covered by test_runtime_integrity*.py), restart simulation, rollback
simulation, failure injection, and the fresh-install / existing-install
canary shapes. All in-process against fabricated site-packages.
"""
from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

from v3core.runtime_integrity import (
    SCOPE_CRITICAL,
    approved_from_wheel,
    build_install_plan,
    build_report,
    discover_copies,
    fingerprint_directory,
)
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


def _proc(pythonpath: str, *, started: str = "2099-01-01 00:00:00", pid: int = 6001) -> ProcessInfo:
    return ProcessInfo(
        role="serve", pid=pid, ppid=1, name="python.exe",
        executable=sys.executable, cmdline="python -m hermes_cli.main serve",
        cwd=None, pythonpath=pythonpath, virtual_env="",
        path_present=True, path_entry_count=4, started_at=started,
    )


def _approved(tmp_path, marker: str):
    return approved_from_wheel(
        _make_wheel(tmp_path / "rel" / "v3_core-4.0.0-py3-none-any.whl", marker),
        tag="vLab",
    )


# ── F-matrix increments ───────────────────────────────────────────────────


def test_f1_fresh_install_no_copies(tmp_path):
    report = build_report(extra_roots=[], processes=[])
    assert report.verdict == "RUNTIME_PROCESS_UNVERIFIED"
    plan = build_install_plan(extra_roots=[], processes=[])
    assert plan.current_state == "fresh_install"


def test_f2_only_runtime_side_copy(tmp_path):
    rt = tmp_path / "runtime_sp"
    _write_package(rt, "APPROVED")
    approved = _approved(tmp_path, "APPROVED")
    report = build_report(extra_roots=[rt], approved=approved, processes=[_proc(str(rt))])
    assert report.verdict == "HEALTHY"


def test_f3_only_venv_side_copy(tmp_path):
    venv = tmp_path / "venv_sp"
    _write_package(venv, "APPROVED")
    approved = _approved(tmp_path, "APPROVED")
    report = build_report(extra_roots=[venv], approved=approved, processes=[_proc(str(venv))])
    assert report.verdict == "HEALTHY"


def test_f5_venv_new_runtime_old(tmp_path):
    """Reversed incident: venv holds APPROVED (and wins), runtime holds an
    old build. Live loads approved => HEALTHY; the stale copy is reported
    as shadowed-but-inactive, never as an error."""
    venv = tmp_path / "venv_sp"
    _write_package(venv, "APPROVED")
    rt = tmp_path / "runtime_sp"
    _write_package(rt, "OLD")
    approved = _approved(tmp_path, "APPROVED")
    report = build_report(
        extra_roots=[venv, rt],
        approved=approved,
        processes=[_proc(f"{venv}{os.pathsep}{rt}")],
    )
    assert report.verdict == "HEALTHY"
    by_root = {c.package_root: c for c in report.copies if c.package == "v3core"}
    old = next(c for r, c in by_root.items() if "runtime_sp" in r)
    assert old.state == "shadowed"  # not loaded, not approved — informational


def test_f10_same_version_different_content_vs_approved(tmp_path):
    """dist-info says 4.0.0 on both sides; content differs — the fingerprint
    comparison must catch it (the shadow verdict path)."""
    venv = tmp_path / "venv_sp"
    _write_package(venv, "OLD-BUT-VERSIONED-400")
    approved = _approved(tmp_path, "NEW-APPROVED")
    report = build_report(
        extra_roots=[venv],
        approved=approved,
        processes=[_proc(str(venv))],
    )
    # live content != approved and no approved copy on disk -> mismatch
    assert report.verdict == "RUNTIME_RELEASE_MISMATCH"


# ── §25 restart simulation ────────────────────────────────────────────────


def test_restart_simulation_disk_new_process_old(tmp_path):
    """A process that started BEFORE the package was upgraded cannot be a
    clean PASS even though the disk now matches the approved content."""
    venv = tmp_path / "venv_sp"
    _write_package(venv, "APPROVED")
    approved = _approved(tmp_path, "APPROVED")

    # process started long before "the upgrade"
    old_proc = _proc(str(venv), started="2000-01-01 00:00:00")
    report = build_report(extra_roots=[venv], approved=approved, processes=[old_proc])
    pr = report.live_processes[0]
    assert pr.verdict == "HEALTHY"
    assert pr.severity == "warn"          # NOT a clean pass
    assert any("restart" in n.lower() for n in pr.notes)
    assert report.severity == "warn"

    # after restart (process started after the package mtime) -> clean PASS
    new_proc = _proc(str(venv), started="2099-01-01 00:00:00")
    report2 = build_report(extra_roots=[venv], approved=approved, processes=[new_proc])
    pr2 = report2.live_processes[0]
    assert pr2.verdict == "HEALTHY"
    assert pr2.severity == "info"
    assert report2.severity == "info"


# ── §26 rollback simulation ───────────────────────────────────────────────


def test_rollback_simulation_restores_identity(tmp_path):
    sp = tmp_path / "sp"
    _write_package(sp, "V1-OLD")
    fp_v1, _ = fingerprint_directory(sp / "v3core", "v3core", scope=SCOPE_CRITICAL)

    # "upgrade"
    _write_package(sp, "V2-NEW")
    fp_v2, _ = fingerprint_directory(sp / "v3core", "v3core", scope=SCOPE_CRITICAL)
    assert fp_v2 != fp_v1

    # "rollback": restore V1 content
    _write_package(sp, "V1-OLD")
    fp_back, _ = fingerprint_directory(sp / "v3core", "v3core", scope=SCOPE_CRITICAL)
    assert fp_back == fp_v1


# ── §27 failure injection ─────────────────────────────────────────────────


def test_broken_wheel_fails_closed(tmp_path):
    bad = tmp_path / "bad-wheel.whl"
    bad.write_bytes(b"this is definitely not a zip archive")
    with pytest.raises(Exception):
        approved_from_wheel(bad)


def test_missing_dist_info_classified(tmp_path):
    sp = tmp_path / "sp"
    pkg = sp / "v3core"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    copies = discover_copies(extra_roots=[sp])
    c = next(c for c in copies if c.package == "v3core")
    assert c.install_type in ("legacy_install", "wheel_unknown")


def test_wheel_missing_package_fingerprint_none(tmp_path):
    empty = tmp_path / "empty.whl"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("README.txt", "nothing here")
    appr = approved_from_wheel(empty)
    assert appr.content_fingerprint is None
    assert "no v3core package" in " ".join(appr.notes)


# ── §37 canary shapes (A fresh / B existing) ──────────────────────────────


def test_canary_a_fresh_install_flow(tmp_path):
    """Fresh host: plan says fresh_install; after 'install' (materialize the
    approved content) and 'restart' (process started after), it verifies."""
    approved = _approved(tmp_path, "APPROVED")
    plan = build_install_plan(extra_roots=[], processes=[], approved_wheel=None)
    assert plan.current_state == "fresh_install"

    venv = tmp_path / "venv_sp"
    _write_package(venv, "APPROVED")
    report = build_report(
        extra_roots=[venv], approved=approved,
        processes=[_proc(str(venv), started="2099-01-01 00:00:00")],
    )
    assert report.verdict == "HEALTHY"
    assert report.severity == "info"


def test_canary_b_existing_install_healthy(tmp_path):
    venv = tmp_path / "venv_sp"
    _write_package(venv, "APPROVED")
    rt = tmp_path / "runtime_sp"
    _write_package(rt, "APPROVED")
    approved = _approved(tmp_path, "APPROVED")
    report = build_report(
        extra_roots=[venv, rt], approved=approved,
        processes=[_proc(str(venv), started="2099-01-01 00:00:00")],
    )
    assert report.verdict == "HEALTHY"
    # duplicate approved copy is informational only
    assert report.severity == "info"


# ── §40 env privacy ───────────────────────────────────────────────────────


def test_process_info_has_no_env_payload():
    """ProcessInfo carries only the white-listed summary — no env dump."""
    proc = _proc("py")
    d = proc.to_dict()
    assert set(d.keys()) == {
        "role", "pid", "ppid", "name", "executable", "cmdline", "cwd",
        "pythonpath", "virtual_env", "path_present", "path_entry_count",
        "started_at",
    }
    assert d["path_present"] is True and d["path_entry_count"] == 4
    blob = json.dumps(d)
    # no PATH VALUE can ever appear — only the count/presence summary exists
    assert "path_entry_count" in blob
