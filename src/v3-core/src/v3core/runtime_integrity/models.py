"""Data models for the Hippocampus runtime-integrity layer.

Frozen v1 schema, additive-only. Pure dataclasses with deterministic
JSON-safe serializers (``sort_keys`` compatible). Style mirrors
``v3core.reliability.models``.

Path policy (deliberate divergence from reliability's ``path_label``):
runtime-integrity reports carry REAL filesystem paths for interpreters,
package roots, and process command lines, because "which exact path wins"
is the diagnostic. Live-process ENV output remains strictly white-listed
(§40 of the round's task book): PYTHONPATH, VIRTUAL_ENV, PATH-presence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── install types (task book §5) ──────────────────────────────────────────
INSTALL_TYPE_OFFICIAL_RELEASE = "official_release"
INSTALL_TYPE_WHEEL_UNKNOWN = "wheel_unknown"
INSTALL_TYPE_EDITABLE = "editable"
INSTALL_TYPE_SOURCE_TREE = "source_tree"
INSTALL_TYPE_DUPLICATE_SHADOWED = "duplicate_shadowed"
INSTALL_TYPE_LEGACY_INSTALL = "legacy_install"
INSTALL_TYPE_UNKNOWN = "unknown"

INSTALL_TYPES: tuple[str, ...] = (
    INSTALL_TYPE_OFFICIAL_RELEASE,
    INSTALL_TYPE_WHEEL_UNKNOWN,
    INSTALL_TYPE_EDITABLE,
    INSTALL_TYPE_SOURCE_TREE,
    INSTALL_TYPE_DUPLICATE_SHADOWED,
    INSTALL_TYPE_LEGACY_INSTALL,
    INSTALL_TYPE_UNKNOWN,
)

# ── copy states ───────────────────────────────────────────────────────────
COPY_STATE_ACTIVE = "active"          # wins the live resolution
COPY_STATE_SHADOWED = "shadowed"      # loses to another copy under live env
COPY_STATE_INACTIVE = "inactive"      # present on disk, not on any live path
COPY_STATE_DUPLICATE = "duplicate"    # additional copy, same content as active

# ── verdicts (§3 of RUNTIME-INTEGRITY.md) ─────────────────────────────────
VERDICT_HEALTHY = "HEALTHY"
VERDICT_SHADOWED = "SHADOWED_APPROVED_INSTALL"
VERDICT_MISMATCH = "RUNTIME_RELEASE_MISMATCH"
VERDICT_EDITABLE = "RUNTIME_EDITABLE_ACTIVE"
VERDICT_DUPLICATE_ONLY = "DUPLICATE_ONLY"
VERDICT_PROCESS_UNVERIFIED = "RUNTIME_PROCESS_UNVERIFIED"
VERDICT_UNKNOWN = "UNKNOWN"

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_ERROR = "error"

# ── evidence scopes (§41) ─────────────────────────────────────────────────
SCOPE_UNIT = "unit"
SCOPE_INTEGRATION = "integration"
SCOPE_PROCESS_REPRODUCED = "process_reproduced"
SCOPE_LIVE_PROCESS = "live_process"
SCOPE_PRODUCTION_READONLY = "production_readonly"


@dataclass
class ApprovedArtifact:
    """Layer A — the distribution the user chose to install."""

    source: str  # official_release | local_build | unknown
    wheel_path: str | None = None
    wheel_filename: str | None = None
    wheel_sha256: str | None = None
    tag: str | None = None
    content_fingerprint: str | None = None  # computed from the wheel's code files
    capabilities: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "wheel_path": self.wheel_path,
            "wheel_filename": self.wheel_filename,
            "wheel_sha256": self.wheel_sha256,
            "tag": self.tag,
            "content_fingerprint": self.content_fingerprint,
            "capabilities": dict(self.capabilities),
            "notes": list(self.notes),
        }


@dataclass
class InstallCopy:
    """Layer B — one on-disk copy of a distribution."""

    package: str  # "v3core" | "v3hermes"
    package_root: str
    import_path: str | None
    root_origin: str  # venv_site_packages | hermes_runtime_site_packages | user_site | source_tree | editable_finder | unknown
    version: str | None
    install_type: str
    state: str = COPY_STATE_INACTIVE
    fingerprint: str | None = None
    fingerprint_scope: str | None = None  # "critical" | "full"
    file_count: int = 0
    mtime_iso: str | None = None
    editable_target: str | None = None
    dist_info_path: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "package_root": self.package_root,
            "import_path": self.import_path,
            "root_origin": self.root_origin,
            "version": self.version,
            "install_type": self.install_type,
            "state": self.state,
            "fingerprint": self.fingerprint,
            "fingerprint_scope": self.fingerprint_scope,
            "file_count": self.file_count,
            "mtime_iso": self.mtime_iso,
            "editable_target": self.editable_target,
            "dist_info_path": self.dist_info_path,
            "capabilities": dict(self.capabilities),
            "notes": list(self.notes),
        }


@dataclass
class ResolutionResult:
    """Layer C — what a given environment would actually import."""

    python_executable: str
    pythonpath: str
    cwd: str | None
    v3core_file: str | None = None
    v3hermes_file: str | None = None
    v3core_version: str | None = None
    v3hermes_version: str | None = None
    v3core_package_root: str | None = None
    fingerprint: str | None = None
    fingerprint_scope: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    sys_path_head: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "python_executable": self.python_executable,
            "pythonpath": self.pythonpath,
            "cwd": self.cwd,
            "v3core_file": self.v3core_file,
            "v3hermes_file": self.v3hermes_file,
            "v3core_version": self.v3core_version,
            "v3hermes_version": self.v3hermes_version,
            "v3core_package_root": self.v3core_package_root,
            "fingerprint": self.fingerprint,
            "fingerprint_scope": self.fingerprint_scope,
            "capabilities": dict(self.capabilities),
            "sys_path_head": list(self.sys_path_head),
            "error": self.error,
        }


@dataclass
class ProcessInfo:
    """Layer D — a live role process (env white-listed, §40)."""

    role: str  # serve | gateway | worker | unknown
    pid: int
    ppid: int | None
    name: str
    executable: str | None
    cmdline: str
    cwd: str | None
    pythonpath: str
    virtual_env: str
    path_present: bool
    path_entry_count: int
    started_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "pid": self.pid,
            "ppid": self.ppid,
            "name": self.name,
            "executable": self.executable,
            "cmdline": self.cmdline,
            "cwd": self.cwd,
            "pythonpath": self.pythonpath,
            "virtual_env": self.virtual_env,
            "path_present": self.path_present,
            "path_entry_count": self.path_entry_count,
            "started_at": self.started_at,
        }


@dataclass
class ProcessResolution:
    """A live process plus the resolution computed under its exact env."""

    process: ProcessInfo
    resolution: ResolutionResult | None
    verdict: str  # one of the VERDICT_* constants
    severity: str  # info | warn | error
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "process": self.process.to_dict(),
            "resolution": self.resolution.to_dict() if self.resolution else None,
            "verdict": self.verdict,
            "severity": self.severity,
            "notes": list(self.notes),
        }


@dataclass
class IntegrityReport:
    """Aggregate verdict. Deterministic serialization (sort_keys-ready)."""

    schema_version: str = "1"
    generated_at: str = ""
    host: dict[str, Any] = field(default_factory=dict)
    approved: ApprovedArtifact | None = None
    copies: list[InstallCopy] = field(default_factory=list)
    live_processes: list[ProcessResolution] = field(default_factory=list)
    verdict: str = VERDICT_UNKNOWN
    severity: str = SEVERITY_INFO
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    evidence_scopes: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "host": dict(self.host),
            "approved": self.approved.to_dict() if self.approved else None,
            "copies": [c.to_dict() for c in self.copies],
            "live_processes": [p.to_dict() for p in self.live_processes],
            "verdict": self.verdict,
            "severity": self.severity,
            "summary": self.summary,
            "details": dict(self.details),
            "evidence_scopes": list(self.evidence_scopes),
            "degraded": list(self.degraded),
        }


SEVERITY_ORDER = {SEVERITY_INFO: 0, SEVERITY_WARN: 1, SEVERITY_ERROR: 2}


def worst_severity(values: list[str]) -> str:
    """Return the highest severity in ``values`` (info < warn < error)."""
    worst = SEVERITY_INFO
    for v in values:
        if SEVERITY_ORDER.get(v, 0) > SEVERITY_ORDER.get(worst, 0):
            worst = v
    return worst
