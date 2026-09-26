"""Data models for the Hippocampus reliability layer.

Pure dataclasses with ``to_dict()`` JSON-safe serializers. The schema is
frozen for v1: new keys are additive-only, key order is fixed, and
``sort_keys=True`` serialization is used everywhere so callers can rely
on deterministic string output.

Type policy (mirrors DESIGN.md §3):
  - timestamps: ISO8601 strings
  - durations: float seconds (``duration_ms`` is an int milliseconds field)
  - counts: int
  - "unknown" is always ``None`` or an explicit enum string — never mixed

``aggregate_overall`` lives here too because it is the canonical reducer
that every section feeds into (DESIGN §5). The reduce rule is:
  any ``fail`` → ``unhealthy``;
  elif any ``warn`` → ``degraded``;
  elif any ``unknown`` → ``degraded``  (an unresolved check lowers the verdict);
  else → ``healthy``.

The third rule is *not* in the dispatch — it is added here because
``unknown`` (e.g. ``recent_qa == 0``) is otherwise silently dropped on
the floor. Tests cover the three documented rules directly; this
extra branch is exercised through the synthetic MW01-recent-zero case.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# ── status enums (string-typed, never mixed with None for "status") ──
STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUS_UNKNOWN = "unknown"

STATUSES: tuple[str, ...] = (STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIP, STATUS_UNKNOWN)

OVERALL_HEALTHY = "healthy"
OVERALL_DEGRADED = "degraded"
OVERALL_UNHEALTHY = "unhealthy"

OVERALLS: tuple[str, ...] = (OVERALL_HEALTHY, OVERALL_DEGRADED, OVERALL_UNHEALTHY)


@dataclass
class CheckResult:
    """One row in the health report.

    ``evidence`` is JSON-safe already (callers are responsible) — keys are
    emitted verbatim. ``duration_ms`` is the only duration field that uses
    integer milliseconds (everywhere else durations are seconds).
    """

    check_id: str
    section: str
    status: str
    summary: str
    evidence: dict[str, Any]
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "section": self.section,
            "status": self.status,
            "summary": self.summary,
            "evidence": self.evidence,
            "duration_ms": self.duration_ms,
        }


@dataclass
class HealthReport:
    """Aggregate report. All fields have defaults so callers can build
    incrementally during section collection; ``to_dict`` serializes
    ``checks`` into a ``list[dict]`` and uses ``sort_keys=True``."""

    schema_version: str = "1"
    overall: str = OVERALL_HEALTHY
    generated_at: str = ""
    profile: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    runtime_integrity: dict[str, Any] = field(default_factory=dict)
    storage: dict[str, Any] = field(default_factory=dict)
    memory_write: dict[str, Any] = field(default_factory=dict)
    failure_accounting: dict[str, Any] = field(default_factory=dict)
    derived_memory: dict[str, Any] = field(default_factory=dict)
    providers: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    checks: list[CheckResult] = field(default_factory=list)
    window_hours: int = 24
    deep: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checks"] = [c.to_dict() for c in self.checks]
        # Force deterministic key order; sort_keys=True is also re-applied
        # by JSON encoders downstream, but baking it in here keeps dict
        # equality comparisons honest in tests.
        return _sort_keys_deep(d)


@dataclass
class Diagnosis:
    """Output of ``diagnose`` (placeholder shape — diagnose.py ships next
    batch). Fields are stable so the planner can consume them."""

    code: str
    severity: str  # info|warning|error
    scope: str  # runtime|storage|memory_write|failure_accounting|derived_memory|providers
    summary: str
    evidence: dict[str, Any]
    repairable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "scope": self.scope,
            "summary": self.summary,
            "evidence": self.evidence,
            "repairable": self.repairable,
        }


@dataclass
class RepairAction:
    """Output of ``repair --dry-run`` (placeholder shape — repair.py ships
    next batch). Field set mirrors DESIGN §9."""

    action_id: str
    issue_code: str
    target_count: int
    risk: str  # low|medium|high
    reversible: bool
    requires_provider: bool
    estimated_remote_calls: int | None
    estimated_cost: dict[str, Any] | None
    writes_database: bool
    automatic_safe: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "issue_code": self.issue_code,
            "target_count": self.target_count,
            "risk": self.risk,
            "reversible": self.reversible,
            "requires_provider": self.requires_provider,
            "estimated_remote_calls": self.estimated_remote_calls,
            "estimated_cost": self.estimated_cost,
            "writes_database": self.writes_database,
            "automatic_safe": self.automatic_safe,
            "reason": self.reason,
        }


def aggregate_overall(checks: list[CheckResult]) -> str:
    """Reduce a list of checks into the top-level verdict.

    Rules (DESIGN §5):
      - any ``fail``  → ``unhealthy``
      - elif any ``warn``  → ``degraded``
      - elif any ``unknown`` → ``degraded``  (an unresolved check lowers the verdict)
      - else ``healthy``

    ``skip`` never affects the verdict — its scenarios must surface as
    ``fail``/``warn`` at the check level when they actually matter.
    """
    statuses = {c.status for c in checks}
    if STATUS_FAIL in statuses:
        return OVERALL_UNHEALTHY
    if STATUS_WARN in statuses:
        return OVERALL_DEGRADED
    if STATUS_UNKNOWN in statuses:
        return OVERALL_DEGRADED
    return OVERALL_HEALTHY


def _sort_keys_deep(value: Any) -> Any:
    """Recursively sort dict keys. ``to_dict`` produces nested dicts from
    dataclasses — the field order is already the contract, but consumers
    that re-serialize (e.g. via ``json.dumps``) need deterministic ordering
    regardless. This helper produces a plain ``dict``/``list`` structure
    that any JSON encoder will emit stably."""
    if isinstance(value, dict):
        return {k: _sort_keys_deep(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_keys_deep(v) for v in value]
    return value