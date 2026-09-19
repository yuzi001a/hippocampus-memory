"""Runtime-integrity core tests.

Covers: content identity (directory vs wheel, same-version-different-code),
copy enumeration + classification, same-environment resolution, shadow /
duplicate / editable detection, and the EXACT 2026-09-19 incident fixture
(shadowed venv runtime) including the post-upgrade PASS case.

The live-process probes run as real subprocesses against fabricated fake
site-packages rooted in tmp_path; no global environment is touched.
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

from v3core.runtime_integrity import (
    SCOPE_CRITICAL,
    SCOPE_FULL,
    approved_from_wheel,
    build_report,
    fingerprint_directory,
    fingerprint_wheel,
)
from v3core.runtime_integrity.models import (
    ProcessInfo,
    VERDICT_EDITABLE,
    VERDICT_HEALTHY,
    VERDICT_PROCESS_UNVERIFIED,
    VERDICT_SHADOWED,
)

# ── helpers ───────────────────────────────────────────────────────────────


def _write_package(root: Path, marker: str) -> Path:
    """Create a fake v3core package directly under ``root``.

    NOTE: ``newline="\\n"`` is explicit — Windows text mode would otherwise
    translate \\n to \\r\\n and break byte-identity with the wheel contents.
    Real installers (pip/uv) do not translate newlines; the fixture must not
    either or the fingerprints legitimately differ.
    """
    pkg = root / "v3core"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        "# fake v3core\nMARKER = %r\n" % marker, encoding="utf-8", newline="\n"
    )
    (pkg / "embed_chunks.py").write_text(
        "# chunk helper\n", encoding="utf-8", newline="\n"
    )
    (pkg / "embedding.py").write_text(
        "# embedding\n", encoding="utf-8", newline="\n"
    )
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


def _fake_process(pythonpath: str, *, role: str = "serve", pid: int = 4001) -> ProcessInfo:
    return ProcessInfo(
        role=role,
        pid=pid,
        ppid=1,
        name="python.exe",
        executable=sys.executable,
        cmdline="python -m hermes_cli.main serve",
        cwd=None,
        pythonpath=pythonpath,
        virtual_env="",
        path_present=True,
        path_entry_count=5,
        started_at="2026-09-19 09:00:00",
    )


# ── content identity ──────────────────────────────────────────────────────


def test_fingerprint_directory_matches_wheel(tmp_path):
    wheel = _make_wheel(tmp_path / "rel" / "v3_core-4.0.0-py3-none-any.whl", "AAA")
    site = tmp_path / "sp1"
    _write_package(site, "AAA")
    fp_dir, n_dir = fingerprint_directory(site / "v3core", "v3core", scope=SCOPE_CRITICAL)
    fp_whl, n_whl = fingerprint_wheel(wheel, "v3core", scope=SCOPE_CRITICAL)
    assert fp_dir == fp_whl
    assert n_dir == n_whl and n_dir > 0


def test_fingerprint_full_scope_also_matches(tmp_path):
    wheel = _make_wheel(tmp_path / "rel" / "v3_core-4.0.0-py3-none-any.whl", "AAA")
    site = tmp_path / "sp1"
    _write_package(site, "AAA")
    fp_dir, n_dir = fingerprint_directory(site / "v3core", "v3core", scope=SCOPE_FULL)
    fp_whl, n_whl = fingerprint_wheel(wheel, "v3core", scope=SCOPE_FULL)
    assert fp_dir == fp_whl
    assert n_dir == n_whl


def test_same_version_different_code_identified(tmp_path):
    """Two builds both claim 4.0.0 in metadata but differ in content:
    the fingerprint must distinguish them (version numbers cannot)."""
    a = tmp_path / "a"
    _write_package(a, "OLD-BUILD")
    b = tmp_path / "b"
    _write_package(b, "NEW-BUILD")
    fa, _ = fingerprint_directory(a / "v3core", "v3core", scope=SCOPE_CRITICAL)
    fb, _ = fingerprint_directory(b / "v3core", "v3core", scope=SCOPE_CRITICAL)
    assert fa is not None and fb is not None
    assert fa != fb


def test_approved_from_wheel_records_identity(tmp_path):
    wheel = _make_wheel(tmp_path / "rel" / "v3_core-4.0.0-py3-none-any.whl", "AAA")
    appr = approved_from_wheel(wheel, tag="vX")
    assert appr.wheel_sha256 and len(appr.wheel_sha256) == 64
    assert appr.content_fingerprint
    assert appr.tag == "vX"


# ── §23 exact incident fixture: shadowed venv runtime ────────────────────


def test_exact_incident_shadowed_venv_runtime(tmp_path):
    """Recreation of the 2026-09-19 production incident:

    venv holds a LEGACY build, .hermes-runtime holds the APPROVED build,
    and the live process's PYTHONPATH lists venv first — so the process
    loads legacy content while the approved copy is shadowed.
    Expected: SHADOWED_APPROVED_INSTALL (error), explicit who-shadows-whom.
    """
    venv_sp = tmp_path / "venv_sp"
    _write_package(venv_sp, "LEGACY-VENV-BUILD")
    rt_sp = tmp_path / "runtime_sp"
    _write_package(rt_sp, "APPROVED-BUILD")
    approved = approved_from_wheel(
        _make_wheel(tmp_path / "rel" / "v3_core-4.0.0-py3-none-any.whl", "APPROVED-BUILD"),
        tag="v0.2.1",
    )
    proc = _fake_process(pythonpath=f"{venv_sp}{os.pathsep}{rt_sp}")
    report = build_report(
        extra_roots=[venv_sp, rt_sp],
        approved=approved,
        processes=[proc],
    )
    assert report.verdict == VERDICT_SHADOWED
    assert report.severity == "error"
    pr = report.live_processes[0]
    assert pr.verdict == VERDICT_SHADOWED
    joined = " ".join(pr.notes)
    assert "shadow" in joined.lower()
    # the approved copy must be reported as shadowed, the venv one as active
    by_root = {c.package_root: c for c in report.copies if c.package == "v3core"}
    venv_copy = next(c for r, c in by_root.items() if "venv_sp" in r)
    rt_copy = next(c for r, c in by_root.items() if "runtime_sp" in r)
    assert venv_copy.state == "active"
    assert rt_copy.state == "shadowed"


def test_incident_resolved_after_upgrade(tmp_path):
    """After upgrading the venv copy to the approved content, the exact
    same fixture must PASS."""
    venv_sp = tmp_path / "venv_sp"
    _write_package(venv_sp, "LEGACY-VENV-BUILD")
    rt_sp = tmp_path / "runtime_sp"
    _write_package(rt_sp, "APPROVED-BUILD")
    approved = approved_from_wheel(
        _make_wheel(tmp_path / "rel" / "v3_core-4.0.0-py3-none-any.whl", "APPROVED-BUILD"),
        tag="v0.2.1",
    )
    # first: shadowed
    proc = _fake_process(pythonpath=f"{venv_sp}{os.pathsep}{rt_sp}")
    r1 = build_report(extra_roots=[venv_sp, rt_sp], approved=approved, processes=[proc])
    assert r1.verdict == VERDICT_SHADOWED

    # upgrade: venv copy replaced with approved content
    _write_package(venv_sp, "APPROVED-BUILD")
    r2 = build_report(extra_roots=[venv_sp, rt_sp], approved=approved, processes=[proc])
    assert r2.verdict == VERDICT_HEALTHY
    assert r2.severity == "info"
    pr = r2.live_processes[0]
    assert pr.verdict == VERDICT_HEALTHY


def test_duplicate_approved_is_info_not_error(tmp_path):
    """Two copies, BOTH matching approved content: benign duplicate."""
    a = tmp_path / "sp_a"
    _write_package(a, "APPROVED-BUILD")
    b = tmp_path / "sp_b"
    _write_package(b, "APPROVED-BUILD")
    approved = approved_from_wheel(
        _make_wheel(tmp_path / "rel" / "v3_core-4.0.0-py3-none-any.whl", "APPROVED-BUILD"),
        tag="v0.2.1",
    )
    proc = _fake_process(pythonpath=f"{a}{os.pathsep}{b}")
    report = build_report(extra_roots=[a, b], approved=approved, processes=[proc])
    assert report.verdict == VERDICT_HEALTHY
    assert report.severity == "info"
    states = sorted(c.state for c in report.copies if c.package == "v3core")
    assert "active" in states and "duplicate" in states


# ── editable / source tree ────────────────────────────────────────────────


def test_editable_source_tree_is_error(tmp_path):
    src = tmp_path / "repo" / "src" / "v3-core" / "src"
    _write_package(src, "DEV")
    (tmp_path / "repo" / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "repo" / "src").mkdir(exist_ok=True)
    proc = _fake_process(pythonpath=str(src))
    report = build_report(extra_roots=[src], processes=[proc])
    pr = report.live_processes[0]
    assert pr.verdict == VERDICT_EDITABLE
    assert pr.severity == "error"


# ── no-approved / degraded modes ─────────────────────────────────────────


def test_no_approved_yields_unverified_not_error(tmp_path):
    sp = tmp_path / "sp"
    _write_package(sp, "X")
    report = build_report(extra_roots=[sp], processes=[_fake_process(pythonpath=str(sp))])
    assert report.verdict == VERDICT_PROCESS_UNVERIFIED
    assert report.severity == "warn"


def test_malformed_pythonpath_does_not_crash(tmp_path):
    sp = tmp_path / "sp"
    _write_package(sp, "X")
    pp = f";;{sp};;C:\\definitely-not-here;;"
    report = build_report(extra_roots=[sp], processes=[_fake_process(pythonpath=pp)])
    assert report.live_processes
    res = report.live_processes[0].resolution
    assert res is not None
    # the resolution still finds the fake package via the surviving segment
    assert res.v3core_file and "sp" in res.v3core_file.replace("\\", "/")


def test_process_without_python_is_unverified(tmp_path):
    proc = _fake_process(pythonpath="x")
    proc.executable = None
    report = build_report(extra_roots=[], processes=[proc])
    pr = report.live_processes[0]
    assert pr.verdict == VERDICT_PROCESS_UNVERIFIED


# ── resolution faithfulness ──────────────────────────────────────────────


def test_resolution_uses_exact_pythonpath(tmp_path):
    """The probe must resolve under the SUPPLIED pythonpath — the venv-first
    ordering is what makes the incident reproducible at all."""
    sp1 = tmp_path / "one"
    _write_package(sp1, "ONE")
    sp2 = tmp_path / "two"
    _write_package(sp2, "TWO")
    from v3core.runtime_integrity import probe_environment

    res = probe_environment(sys.executable, pythonpath=f"{sp1}{os.pathsep}{sp2}")
    assert res.v3core_file and "one" in res.v3core_file.replace("\\", "/")
    res2 = probe_environment(sys.executable, pythonpath=f"{sp2}{os.pathsep}{sp1}")
    assert res2.v3core_file and "two" in res2.v3core_file.replace("\\", "/")
