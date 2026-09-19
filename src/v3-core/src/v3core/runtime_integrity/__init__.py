"""Hippocampus runtime-integrity layer — public API.

See ``docs/RUNTIME-INTEGRITY.md`` for the contract. Read-only; answers:
"is the code production actually loads the code you approved?"
"""
from __future__ import annotations

from .contract import approved_from_wheel, build_report, discover_processes
from .discovery import candidate_roots, discover_copies
from .plan import InstallPlan, build_install_plan
from .identity import (
    SCOPE_CRITICAL,
    SCOPE_FULL,
    capabilities_from_dir,
    capabilities_from_wheel,
    fingerprint_directory,
    fingerprint_wheel,
    wheel_file_sha256,
)
from .models import (
    INSTALL_TYPES,
    ApprovedArtifact,
    IntegrityReport,
    InstallCopy,
    ProcessInfo,
    ProcessResolution,
    ResolutionResult,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARN,
    VERDICT_DUPLICATE_ONLY,
    VERDICT_EDITABLE,
    VERDICT_HEALTHY,
    VERDICT_MISMATCH,
    VERDICT_PROCESS_UNVERIFIED,
    VERDICT_SHADOWED,
    VERDICT_UNKNOWN,
    worst_severity,
)
from .resolver import probe_environment
from .shadow import (
    annotate_states,
    compute_precedence_reason,
    find_copy_for_resolution,
    verdict_for_process,
)

__all__ = [
    # constants
    "SCOPE_CRITICAL",
    "SCOPE_FULL",
    "INSTALL_TYPES",
    "SEVERITY_INFO",
    "SEVERITY_WARN",
    "SEVERITY_ERROR",
    "VERDICT_HEALTHY",
    "VERDICT_SHADOWED",
    "VERDICT_MISMATCH",
    "VERDICT_EDITABLE",
    "VERDICT_DUPLICATE_ONLY",
    "VERDICT_PROCESS_UNVERIFIED",
    "VERDICT_UNKNOWN",
    # models
    "ApprovedArtifact",
    "InstallCopy",
    "ResolutionResult",
    "ProcessInfo",
    "ProcessResolution",
    "IntegrityReport",
    "worst_severity",
    # identity
    "fingerprint_directory",
    "fingerprint_wheel",
    "capabilities_from_dir",
    "capabilities_from_wheel",
    "wheel_file_sha256",
    # discovery
    "candidate_roots",
    "discover_copies",
    # resolver
    "probe_environment",
    # shadow / verdicts
    "find_copy_for_resolution",
    "compute_precedence_reason",
    "verdict_for_process",
    "annotate_states",
    # orchestration
    "approved_from_wheel",
    "discover_processes",
    "build_report",
    # planning
    "build_install_plan",
    "InstallPlan",
]
