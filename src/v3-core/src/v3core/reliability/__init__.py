"""Hippocampus reliability layer — public API re-exports.

This subpackage ships the read-only ``health`` half of the
``reliability-recovery-v1`` feature branch. The interactive layers
(``diagnose``, ``repair``, ``cli``) are added in the next dispatch.

The contract frozen here:

  - ``HealthReport`` — the data structure that every section feeds.
  - ``CheckResult`` — the unit row inside ``HealthReport.checks``.
  - ``Diagnosis`` / ``RepairAction`` — placeholders for the next dispatch;
    defined here so callers can already type their code against them.
  - ``aggregate_overall`` — the canonical reducer; rules in DESIGN §5.
  - ``FailureReader`` — the marker-ledger reader.
  - ``HealthService`` — the collector.
  - redaction helpers + the production-target predicate.
  - contract constants from DESIGN §2.
"""
from __future__ import annotations

from .failure_reader import (
    ALL_STATUSES,
    FailureReader,
    STATUS_IN_FLIGHT,
    STATUS_MALFORMED,
    STATUS_PENDING,
    STATUS_PENDING_DB,
    STATUS_POISONED,
    STATUS_RECOVERED,
    STATUS_RETRYABLE,
    STATUS_RETRY_DUE,
    STATUS_STALE_IN_FLIGHT,
    STATUS_STALE_PENDING_DB,
    STATUS_UNRESOLVED,
)
from .health import (
    AUTH_TIMEOUT_SECONDS,
    CANONICAL_INDEXES,
    CANONICAL_TABLES,
    EXPECTED_SCHEMA_VERSION,
    HEALTH_WINDOW_HOURS_DEFAULT,
    HealthService,
    REPORT_SCHEMA_VERSION,
    STALE_MARKER_HOURS,
)
from .models import (
    OVERALL_DEGRADED,
    OVERALL_HEALTHY,
    OVERALL_UNHEALTHY,
    OVERALLS,
    STATUSES,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_UNKNOWN,
    STATUS_WARN,
    CheckResult,
    Diagnosis,
    HealthReport,
    RepairAction,
    aggregate_overall,
)
from .redaction import (
    LOOPBACK_HOSTS,
    PROD_LOCAL_DB,
    PROD_PORT,
    is_production_target,
    path_label,
    sanitize_text,
)

__all__ = [
    # constants
    "REPORT_SCHEMA_VERSION",
    "HEALTH_WINDOW_HOURS_DEFAULT",
    "STALE_MARKER_HOURS",
    "AUTH_TIMEOUT_SECONDS",
    "CANONICAL_TABLES",
    "CANONICAL_INDEXES",
    "EXPECTED_SCHEMA_VERSION",
    "LOOPBACK_HOSTS",
    "PROD_PORT",
    "PROD_LOCAL_DB",
    # status / overall enums
    "STATUSES",
    "STATUS_OK",
    "STATUS_WARN",
    "STATUS_FAIL",
    "STATUS_SKIP",
    "STATUS_UNKNOWN",
    "OVERALLS",
    "OVERALL_HEALTHY",
    "OVERALL_DEGRADED",
    "OVERALL_UNHEALTHY",
    # models
    "CheckResult",
    "HealthReport",
    "Diagnosis",
    "RepairAction",
    "aggregate_overall",
    # readers / services
    "FailureReader",
    "HealthService",
    # marker-status names (re-exported so callers don't import internals)
    "ALL_STATUSES",
    "STATUS_MALFORMED",
    "STATUS_RECOVERED",
    "STATUS_POISONED",
    "STATUS_RETRYABLE",
    "STATUS_RETRY_DUE",
    "STATUS_STALE_IN_FLIGHT",
    "STATUS_IN_FLIGHT",
    "STATUS_STALE_PENDING_DB",
    "STATUS_PENDING_DB",
    "STATUS_PENDING",
    "STATUS_UNRESOLVED",
    # redaction helpers
    "path_label",
    "sanitize_text",
    "is_production_target",
]