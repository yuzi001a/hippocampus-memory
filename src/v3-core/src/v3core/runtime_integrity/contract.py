"""Report assembly: process discovery + per-process resolution + verdict.

Orchestration only; all logic lives in the sibling modules. Read-only.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from .discovery import discover_copies
from .identity import SCOPE_CRITICAL, capabilities_from_wheel, wheel_file_sha256, fingerprint_wheel
from .models import (
    ApprovedArtifact,
    IntegrityReport,
    ProcessInfo,
    ProcessResolution,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARN,
    VERDICT_EDITABLE,
    VERDICT_HEALTHY,
    VERDICT_MISMATCH,
    VERDICT_PROCESS_UNVERIFIED,
    VERDICT_SHADOWED,
    worst_severity,
)
from .resolver import probe_environment
from .shadow import annotate_states, verdict_for_process

try:  # psutil is present on supported Hermes hosts; degrade gracefully otherwise.
    import psutil  # type: ignore
except Exception:  # pragma: no cover - environment dependent
    psutil = None


def approved_from_wheel(
    wheel_path: str | Path,
    *,
    tag: str | None = None,
    source: str = "official_release",
) -> ApprovedArtifact:
    """Compute the approved identity from a wheel file (no network needed)."""
    p = Path(wheel_path)
    fp, n = fingerprint_wheel(p, "v3core", scope=SCOPE_CRITICAL)
    return ApprovedArtifact(
        source=source,
        wheel_path=str(p),
        wheel_filename=p.name,
        wheel_sha256=wheel_file_sha256(p),
        tag=tag,
        content_fingerprint=fp,
        capabilities=capabilities_from_wheel(p, "v3core"),
        notes=[f"critical fingerprint over {n} files"] if fp else ["wheel has no v3core package"],
    )


def _role_for_cmdline(cmd: str) -> str | None:
    cl = cmd.lower()
    if "hermes_cli.main" not in cl and "hermes" not in cl:
        return None
    if " gateway " in cl or cl.rstrip().endswith("gateway run") or "gateway run" in cl:
        return "gateway"
    if " serve " in cl or cl.rstrip().endswith("serve") or "serve --host" in cl:
        return "serve"
    return None


def discover_processes(extra_pids: list[int] | None = None) -> tuple[list[ProcessInfo], list[str]]:
    """Discover Hermes production processes. Returns (processes, degraded_notes)."""
    degraded: list[str] = []
    out: list[ProcessInfo] = []
    if psutil is None:
        degraded.append("psutil unavailable: live process discovery skipped")
        return out, degraded
    wanted_extra = set(extra_pids or [])
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time", "exe"]):
        try:
            info = p.info
            name = (info.get("name") or "").lower()
            cmd = " ".join(info.get("cmdline") or [])
            role = _role_for_cmdline(cmd)
            if role is None and info["pid"] not in wanted_extra:
                continue
            if role is None:
                role = "worker"
            env: dict = {}
            cwd = None
            try:
                env = p.environ()
            except Exception:
                degraded.append(f"pid {info['pid']}: environment unreadable")
            try:
                cwd = p.cwd()
            except Exception:
                cwd = None
            path_val = env.get("PATH", "")
            out.append(ProcessInfo(
                role=role,
                pid=info["pid"],
                ppid=info.get("ppid"),
                name=info.get("name") or "",
                executable=info.get("exe"),
                cmdline=cmd[:400],
                cwd=cwd,
                pythonpath=env.get("PYTHONPATH", ""),
                virtual_env=env.get("VIRTUAL_ENV", ""),
                path_present=bool(path_val),
                path_entry_count=(path_val.count(os.pathsep) + 1) if path_val else 0,
                started_at=dt.datetime.fromtimestamp(info["create_time"]).strftime("%Y-%m-%d %H:%M:%S") if info.get("create_time") else None,
            ))
        except Exception:
            continue
    out.sort(key=lambda x: (x.role, x.pid))
    return out, degraded


def _probe_python_for(proc: ProcessInfo) -> str | None:
    exe = proc.executable
    if exe and "python" in exe.lower() and Path(exe).exists():
        return exe
    return None


def build_report(
    *,
    hermes_home: str | Path | None = None,
    checkout: str | Path | None = None,
    extra_roots: list[str | Path] | None = None,
    approved: ApprovedArtifact | None = None,
    scope: str = SCOPE_CRITICAL,
    extra_pids: list[int] | None = None,
    probe_timeout: float = 120.0,
    processes: list[ProcessInfo] | None = None,
) -> IntegrityReport:
    """Build the full integrity report. All paths optional; missing pieces
    are reported as degraded, never guessed."""
    degraded: list[str] = []
    hh = Path(hermes_home) if hermes_home else None
    co = Path(checkout) if checkout else None
    extra = [Path(p) for p in (extra_roots or [])]

    # Layer B — copies
    copies = discover_copies(hermes_home=hh, checkout=co, extra_roots=extra, approved=approved, scope=scope)

    # Layer D — live processes (or caller-supplied)
    if processes is None:
        procs, proc_degraded = discover_processes(extra_pids=extra_pids)
        degraded.extend(proc_degraded)
    else:
        procs = processes

    # Layer C per process — probe under each process's EXACT environment.
    process_resolutions: list[ProcessResolution] = []
    for proc in procs:
        py = _probe_python_for(proc)
        if py is None:
            process_resolutions.append(ProcessResolution(
                process=proc, resolution=None,
                verdict=VERDICT_PROCESS_UNVERIFIED, severity=SEVERITY_WARN,
                notes=["no usable python executable recorded for this process"],
            ))
            continue
        res = probe_environment(py, pythonpath=proc.pythonpath, cwd=proc.cwd, timeout=probe_timeout)
        verdict, severity, notes, details = verdict_for_process(res, copies, approved)
        pr = ProcessResolution(process=proc, resolution=res, verdict=verdict, severity=severity, notes=notes)
        if details:
            pr.notes = pr.notes + [f"{k}={v}" for k, v in sorted(details.items())]
        process_resolutions.append(pr)

    annotate_states(copies, process_resolutions, approved)

    # Aggregate
    verdicts = [pr.verdict for pr in process_resolutions]
    if VERDICT_EDITABLE in verdicts:
        verdict, severity = VERDICT_EDITABLE, SEVERITY_ERROR
    elif VERDICT_SHADOWED in verdicts:
        verdict, severity = VERDICT_SHADOWED, SEVERITY_ERROR
    elif VERDICT_MISMATCH in verdicts:
        verdict, severity = VERDICT_MISMATCH, SEVERITY_ERROR
    elif VERDICT_PROCESS_UNVERIFIED in verdicts:
        verdict, severity = VERDICT_PROCESS_UNVERIFIED, SEVERITY_WARN
    elif verdicts and all(v == VERDICT_HEALTHY for v in verdicts):
        verdict, severity = VERDICT_HEALTHY, SEVERITY_INFO
    else:
        verdict, severity = VERDICT_PROCESS_UNVERIFIED, SEVERITY_WARN
        if not verdicts:
            degraded.append("no live processes discovered to verify")

    v3core_copies = [c for c in copies if c.package == "v3core"]
    duplicate_approved = sum(1 for c in v3core_copies if c.state in ("duplicate",)) if approved else 0
    details = {
        "copy_count_v3core": len(v3core_copies),
        "duplicate_approved_copies": duplicate_approved,
        "editable_marker_copies": [c.package_root for c in copies if c.install_type in ("editable", "source_tree")],
    }
    summary = {
        VERDICT_HEALTHY: "runtime verified: live processes resolve to approved content",
        VERDICT_SHADOWED: "integrity violation: live processes resolve to non-approved content while an approved copy is shadowed",
        VERDICT_MISMATCH: "integrity violation: live content does not match the approved artifact",
        VERDICT_EDITABLE: "integrity violation: live processes load an editable/source-tree install",
        VERDICT_PROCESS_UNVERIFIED: "runtime verification incomplete (unverified processes or missing approved artifact)",
    }.get(verdict, "runtime verdict unknown")

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    report = IntegrityReport(
        generated_at=now,
        host={
            "platform": os.name,
            "python": os.sys.version.split()[0],
            "hermes_home": str(hh) if hh else None,
            "checkout": str(co) if co else None,
        },
        approved=approved,
        copies=copies,
        live_processes=process_resolutions,
        verdict=verdict,
        severity=severity,
        summary=summary,
        details=details,
        evidence_scopes=["live_process"] if process_resolutions else ["integration"],
        degraded=degraded,
    )
    return report
