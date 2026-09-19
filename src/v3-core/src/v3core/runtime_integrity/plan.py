"""Install / upgrade planning for the runtime-integrity layer.

Implements the PLAN half of the task-book model (§11):

    discover Hermes -> discover live runtime -> discover import precedence
    -> determine actual target environment -> detect conflicting copies
    -> plan install -> show plan -> (execute*) -> (restart*) -> (verify*)

(*) Execution, restart, and post-verify are operator-gated and NOT part of
this module; the plan describes them precisely instead.

Hard rule: the target environment is derived from LIVE PROCESS RESOLUTION,
never hard-coded. If live resolution is unavailable the plan degrades to an
explicit `unknown` target with a note — it never guesses.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contract import build_report
from .models import ApprovedArtifact, SEVERITY_INFO, worst_severity

PLAN_FRESH_INSTALL = "fresh_install"
PLAN_ALREADY_INSTALLED = "already_installed"
PLAN_UPGRADE_REQUIRED = "upgrade_required"
PLAN_SHADOWED = "shadowed_active"
PLAN_EDITABLE_ACTIVE = "editable_active"
PLAN_UNKNOWN = "unknown"


@dataclass
class InstallPlan:
    schema_version: str = "1"
    generated_at: str = ""
    host: dict[str, Any] = field(default_factory=dict)
    approved: dict[str, Any] | None = None
    live_processes: list[dict[str, Any]] = field(default_factory=list)
    actual_target: dict[str, Any] = field(default_factory=dict)
    current_state: str = PLAN_UNKNOWN
    duplicates: list[str] = field(default_factory=list)
    shadow_risks: list[str] = field(default_factory=list)
    replace_distributions: list[dict[str, Any]] = field(default_factory=list)
    restart_required: list[str] = field(default_factory=list)
    rollback_suggestion: str = ""
    expected_post_install: dict[str, Any] = field(default_factory=dict)
    db_writes: bool = False
    steps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    severity: str = SEVERITY_INFO

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "host": dict(self.host),
            "approved": self.approved,
            "live_processes": list(self.live_processes),
            "actual_target": dict(self.actual_target),
            "current_state": self.current_state,
            "duplicates": list(self.duplicates),
            "shadow_risks": list(self.shadow_risks),
            "replace_distributions": list(self.replace_distributions),
            "restart_required": list(self.restart_required),
            "rollback_suggestion": self.rollback_suggestion,
            "expected_post_install": dict(self.expected_post_install),
            "db_writes": self.db_writes,
            "steps": list(self.steps),
            "notes": list(self.notes),
            "severity": self.severity,
        }


def _norm(p: str | None) -> str:
    return (p or "").replace("\\", "/").lower()


def _expected_site_packages(python_exe: str) -> str | None:
    """Best-effort: the site-packages a given python executable installs to."""
    e = _norm(python_exe)
    if not e.endswith("python.exe") and not e.endswith("python"):
        return None
    # <env>/Scripts/python.exe -> <env>/Lib/site-packages  (Windows layout)
    parent = Path(python_exe).parent
    if parent.name.lower() == "scripts":
        return _norm(str(parent.parent / "Lib" / "site-packages"))
    # <env>/bin/python -> <env>/lib/pythonX.Y/site-packages (POSIX best effort)
    return None


def build_install_plan(
    *,
    hermes_home: str | Path | None = None,
    checkout: str | Path | None = None,
    extra_roots: list[str | Path] | None = None,
    approved_wheel: str | Path | None = None,
    tag: str | None = None,
    processes: Any | None = None,
) -> InstallPlan:
    """Produce a deterministic install/upgrade plan for the live host."""
    from .contract import approved_from_wheel

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    approved: ApprovedArtifact | None = None
    if approved_wheel is not None:
        approved = approved_from_wheel(str(approved_wheel), tag=tag)

    report = build_report(
        hermes_home=hermes_home,
        checkout=checkout,
        extra_roots=extra_roots,
        approved=approved,
        processes=processes,
    )

    plan = InstallPlan(generated_at=now)
    plan.host = dict(report.host)
    plan.approved = approved.to_dict() if approved else None

    proc_rows: list[dict[str, Any]] = []
    resolved_roots: list[str] = []
    for pr in report.live_processes:
        res = pr.resolution
        row = {
            "role": pr.process.role,
            "pid": pr.process.pid,
            "executable": pr.process.executable,
            "pythonpath": pr.process.pythonpath,
            "resolved_v3core": (res.v3core_file if res else None),
            "verdict": pr.verdict,
        }
        proc_rows.append(row)
        if res and res.v3core_package_root:
            resolved_roots.append(_norm(res.v3core_package_root))
    plan.live_processes = proc_rows

    v3core_copies = [c for c in report.copies if c.package == "v3core"]
    active = [c for c in v3core_copies if c.state == "active"]
    duplicates = [c for c in v3core_copies if c.state == "duplicate"]

    # ── actual target: the environment the LIVE processes actually load ──
    target_root: str | None = None
    target_source: str = "unavailable"
    if active:
        t = active[0]
        target_root = t.package_root
        target_source = "live_resolution"
    elif resolved_roots:
        target_root = resolved_roots[0]
        target_source = "live_resolution"
    plan.actual_target = {
        "v3core_root": target_root,
        "source": target_source,
        "install_python": None,
        "note": None,
    }
    if target_root is None:
        plan.notes.append(
            "no live resolution available; target environment unknown — "
            "run with live Hermes processes present or pass roots explicitly"
        )

    # Which python owns that environment? Match each live process's expected
    # site-packages against the resolved root.
    if target_root:
        for pr in report.live_processes:
            exe = pr.process.executable or ""
            sp = _expected_site_packages(exe)
            if sp and (_norm(target_root).startswith(sp) or sp.startswith(_norm(target_root))):
                plan.actual_target["install_python"] = exe
                break

    # ── state machine ──
    if not v3core_copies:
        if resolved_roots:
            # A live process resolved a copy that discovery did not enumerate:
            # roots are incomplete — never call this a clean fresh install.
            plan.current_state = PLAN_UNKNOWN
            plan.severity = "warn"
            plan.notes.append(
                "live resolution found a v3core copy that discovery did not "
                "enumerate; roots may be incomplete — inspect before installing"
            )
        else:
            plan.current_state = PLAN_FRESH_INSTALL
    elif report.verdict == "RUNTIME_EDITABLE_ACTIVE":
        plan.current_state = PLAN_EDITABLE_ACTIVE
        plan.severity = "error"
    elif report.verdict == "SHADOWED_APPROVED_INSTALL":
        plan.current_state = PLAN_SHADOWED
        plan.severity = "error"
    elif approved is not None and report.verdict == "HEALTHY":
        plan.current_state = PLAN_ALREADY_INSTALLED
    elif approved is not None:
        plan.current_state = PLAN_UPGRADE_REQUIRED
        plan.severity = "warn"
    else:
        plan.current_state = PLAN_UNKNOWN
        plan.notes.append("approved artifact not supplied; install target check is heuristic only")

    plan.duplicates = [c.package_root for c in duplicates]

    # ── shadow risks ──
    # After installing into ``target_root`` and restarting, each live process
    # must resolve to ``target_root``. Any process currently resolving
    # ELSEWHERE signals a split-brain risk.
    for pr in report.live_processes:
        res = pr.resolution
        if res and res.v3core_package_root and target_root:
            cur = _norm(res.v3core_package_root)
            if not (cur == _norm(target_root)):
                plan.shadow_risks.append(
                    f"pid {pr.process.pid} ({pr.process.role}) currently resolves to "
                    f"{res.v3core_package_root}; after install it must resolve to {target_root}"
                )
    for c in v3core_copies:
        if c.state == "shadowed":
            plan.shadow_risks.append(
                f"copy {c.package_root} is shadowed today; confirm it will not win "
                f"after the target environment is upgraded and processes restart"
            )

    # ── distributions to replace ──
    if active:
        a = active[0]
        plan.replace_distributions = [
            {
                "distribution": "v3-core",
                "current_version": a.version,
                "current_fingerprint": a.fingerprint,
                "location": a.package_root,
            },
            {
                "distribution": "v3-hermes-plugin",
                "current_version": None,
                "current_fingerprint": None,
                "location": str(Path(a.package_root).parent / "v3hermes"),
            },
        ]

    # ── restart requirements ──
    roles = sorted({pr.process.role for pr in report.live_processes if pr.process.role in ("serve", "gateway")})
    plan.restart_required = roles or ["serve", "gateway"]
    plan.notes.append(
        "restart count: every role that loads the v3hermes plugin must be "
        "restarted — both serve and gateway load it on this host"
    )

    # ── rollback + expected resolution ──
    if target_root:
        parent = Path(target_root).parent
        plan.rollback_suggestion = (
            f"backup {target_root} and {parent / 'v3hermes'} (+ dist-info) before install; "
            f"rollback = restore the backup and restart {', '.join(plan.restart_required)}"
        )
        plan.expected_post_install = {
            "v3core_root": target_root,
            "fingerprint": (approved.content_fingerprint if approved else None),
            "verify_with": (
                "hippocampus doctor --runtime"
                + (f" --wheel {approved.wheel_path}" if approved and approved.wheel_path else "")
            ),
        }

    # ── steps ──
    py = plan.actual_target.get("install_python") or "<venv python>"
    whl = approved.wheel_path if approved else "<v3_core wheel> <v3_hermes_plugin wheel>"
    steps = [
        "1. Snapshot current live identity (doctor --runtime --json) and save it.",
        f"2. Backup the target environment packages: v3core, v3hermes, both dist-info.",
        f"3. Install the approved wheels into the ACTUAL loaded environment: "
        f"uv pip install --python {py} --force-reinstall --no-deps {whl} <plugin wheel>.",
        f"4. Restart every affected component: {', '.join(plan.restart_required)}.",
        "5. Verify live provenance: doctor --runtime --wheel <approved wheel> must report HEALTHY.",
        "6. On failure: restore the backup and restart again (rollback).",
    ]
    plan.steps = steps
    if plan.current_state == PLAN_ALREADY_INSTALLED:
        plan.notes.append(
            "live content already matches the approved artifact; no install "
            "needed — re-run doctor --runtime after any restart to confirm"
        )
        plan.severity = worst_severity([plan.severity, SEVERITY_INFO])

    return plan
