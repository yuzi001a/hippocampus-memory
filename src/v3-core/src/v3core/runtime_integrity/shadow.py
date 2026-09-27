"""Shadow / duplicate / editable conflict detection and per-process verdicts.

Given discovered copies, live resolutions, and (optionally) an approved
artifact, decide:

  - which copy each live process actually resolves to;
  - whether that copy matches the approved content;
  - WHO shadows WHOM when it does not (with a precedence reason);
  - the per-process verdict and the copy states.

Never reports merely "two installs found".
"""
from __future__ import annotations

from pathlib import Path

from .models import (
    ApprovedArtifact,
    COPY_STATE_ACTIVE,
    COPY_STATE_DUPLICATE,
    COPY_STATE_INACTIVE,
    COPY_STATE_SHADOWED,
    INSTALL_TYPE_EDITABLE,
    INSTALL_TYPE_OFFICIAL_RELEASE,
    INSTALL_TYPE_SOURCE_TREE,
    InstallCopy,
    ResolutionResult,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARN,
    VERDICT_EDITABLE,
    VERDICT_HEALTHY,
    VERDICT_MISMATCH,
    VERDICT_PROCESS_UNVERIFIED,
    VERDICT_SHADOWED,
)


def _norm(p: str | None) -> str:
    return (p or "").replace("\\", "/").lower()


def find_copy_for_resolution(copies: list[InstallCopy], resolution: ResolutionResult) -> InstallCopy | None:
    """Match a resolution to the copy that owns the resolved package root."""
    root = _norm(resolution.v3core_package_root)
    if not root:
        core_file = _norm(resolution.v3core_file)
        if not core_file:
            return None
        root = core_file.rsplit("/", 1)[0]
    best: InstallCopy | None = None
    for c in copies:
        if c.package != "v3core":
            continue
        cr = _norm(c.package_root)
        if cr == root or root.startswith(cr + "/") or cr.startswith(root + "/"):
            if best is None or len(cr) > len(_norm(best.package_root)):
                best = c
    return best


def _copy_matches_approved(copy: InstallCopy | None, approved: ApprovedArtifact | None) -> bool:
    if copy is None or approved is None or approved.content_fingerprint is None:
        return False
    return (
        copy.fingerprint is not None
        and copy.fingerprint == approved.content_fingerprint
        and copy.fingerprint_scope == "critical"  # scope parity is enforced by callers
    ) or (copy.fingerprint is not None and copy.fingerprint == approved.content_fingerprint)


def _resolution_matches_approved(resolution: ResolutionResult, approved: ApprovedArtifact | None) -> bool:
    if approved is None or approved.content_fingerprint is None:
        return False
    return resolution.fingerprint is not None and resolution.fingerprint == approved.content_fingerprint


def compute_precedence_reason(resolution: ResolutionResult, active_root: str, other_root: str) -> str:
    """Explain why ``active_root`` won over ``other_root`` using the live
    sys.path head. Falls back to a PYTHONPATH explanation."""
    a = _norm(active_root)
    o = _norm(other_root)
    sys_path = [_norm(p) for p in resolution.sys_path_head]
    a_idx = next((i for i, p in enumerate(sys_path) if a and (a.startswith(p) or p.startswith(a))), None)
    o_idx = next((i for i, p in enumerate(sys_path) if o and (o.startswith(p) or p.startswith(o))), None)
    if a_idx is not None and o_idx is not None and a_idx != o_idx:
        if a_idx < o_idx:
            return f"sys.path order: {sys_path[a_idx]} precedes {sys_path[o_idx]}"
        return f"sys.path order: {sys_path[o_idx]} precedes {sys_path[a_idx]} (unexpected)"
    if resolution.pythonpath:
        return "PYTHONPATH precedence: the winning root appears earlier in PYTHONPATH than the other copy"
    return "resolution precedence not explainable from sys.path head; inspect PYTHONPATH"


def verdict_for_process(
    resolution: ResolutionResult,
    copies: list[InstallCopy],
    approved: ApprovedArtifact | None,
    process_started: str | None = None,
) -> tuple[str, str, list[str], dict]:
    """Return ``(verdict, severity, notes, details)`` for one process
    resolution. ``details`` may carry shadow evidence.

    ``process_started`` (ISO string) enables the stale-process guard: when
    the active package content is NEWER than the process start time, the
    process may still hold pre-upgrade modules in memory — the verdict
    stays HEALTHY but severity is raised to warn ("restart required"), so
    a disk-upgraded-but-not-restarted host cannot read as a clean PASS.
    """
    notes: list[str] = []
    details: dict = {}

    if resolution.error and resolution.v3core_file is None:
        return VERDICT_PROCESS_UNVERIFIED, SEVERITY_WARN, [f"probe error: {resolution.error}"], details

    active = find_copy_for_resolution(copies, resolution)
    if active is not None:
        details["active_path"] = active.package_root

    # Editable/source-tree always fails: production must not load dev trees.
    if active is not None and active.install_type in (INSTALL_TYPE_EDITABLE, INSTALL_TYPE_SOURCE_TREE):
        notes.append(f"live resolution loads a {active.install_type} copy")
        if active.editable_target:
            notes.append(f"editable target: {active.editable_target}")
        return VERDICT_EDITABLE, SEVERITY_ERROR, notes, details

    if approved is None or approved.content_fingerprint is None:
        notes.append("no approved artifact supplied; content match cannot be established")
        return VERDICT_PROCESS_UNVERIFIED, SEVERITY_WARN, notes, details

    if _resolution_matches_approved(resolution, approved):
        # Stale-process guard (§25): content changed after the process
        # started => the live process may still run the old modules.
        # Normalize the separator before comparing ("T" vs " " would sort
        # wrong at the first differing character).
        if active is not None and active.mtime_iso and process_started:
            norm_m = str(active.mtime_iso).replace("T", " ")[:19]
            norm_p = str(process_started).replace("T", " ")[:19]
            if norm_m > norm_p:
                notes.append(
                    "active package content is newer than this process's start time; "
                    "restart required before the running process can load it"
                )
                details["stale_process"] = True
                details["package_mtime"] = active.mtime_iso
                details["process_started"] = process_started
                return VERDICT_HEALTHY, SEVERITY_WARN, notes, details
        return VERDICT_HEALTHY, SEVERITY_INFO, notes, details

    # Not matching. Is there an approved copy elsewhere that is being shadowed?
    approved_copies = [c for c in copies if c.package == "v3core" and _copy_matches_approved(c, approved)]
    if approved_copies:
        # pick the approved copy closest in root_origin preference? deterministic: first sorted
        appr = sorted(approved_copies, key=lambda c: c.package_root.lower())[0]
        details["approved_path"] = appr.package_root
        details["active_path"] = (active.package_root if active else resolution.v3core_package_root)
        details["precedence_reason"] = compute_precedence_reason(
            resolution,
            active_root=details["active_path"] or "",
            other_root=appr.package_root,
        )
        details["active_install_type"] = active.install_type if active else None
        notes.append("live resolution does not match the approved content; an approved copy exists but is shadowed")
        return VERDICT_SHADOWED, SEVERITY_ERROR, notes, details

    notes.append("live resolution does not match the approved content and no approved copy was found on disk")
    return VERDICT_MISMATCH, SEVERITY_ERROR, notes, details


def annotate_states(
    copies: list[InstallCopy],
    process_resolutions: list,
    approved: ApprovedArtifact | None,
) -> None:
    """Set .state on every v3core copy:
      active    - wins for at least one live process
      shadowed  - approved content that loses to a non-approved active copy
      duplicate - same content class as the active copy
      inactive  - present but not on any live path
    """
    active_roots: set[str] = set()
    for pr in process_resolutions:
        res = pr.resolution
        if res is None:
            continue
        if res.v3core_package_root:
            active_roots.add(_norm(res.v3core_package_root))
        elif res.v3core_file:
            active_roots.add(_norm(res.v3core_file.rsplit("/", 1)[0]).replace("\\", "/"))

    # Pass 1 — mark every copy that wins for a live process (order-independent).
    for c in copies:
        if c.package != "v3core":
            continue
        cr = _norm(c.package_root)
        if any(cr == r or r.startswith(cr + "/") or cr.startswith(r + "/") for r in active_roots):
            c.state = COPY_STATE_ACTIVE

    # Pass 2 — classify the rest against the (now complete) set of actives.
    active_approved_match = any(
        _copy_matches_approved(a, approved)
        for a in copies
        if a.package == "v3core" and a.state == COPY_STATE_ACTIVE
    )
    for c in copies:
        if c.package != "v3core" or c.state == COPY_STATE_ACTIVE:
            continue
        approved_match = _copy_matches_approved(c, approved)
        if approved_match:
            # Another copy of the approved content that is not being loaded:
            # a benign duplicate when the active copy is also approved (or
            # when nothing is active yet); a shadowed approved copy when a
            # non-approved copy currently wins.
            c.state = COPY_STATE_SHADOWED if (active_roots and not active_approved_match) else COPY_STATE_DUPLICATE
        elif active_roots:
            c.state = COPY_STATE_SHADOWED
        else:
            c.state = COPY_STATE_INACTIVE
