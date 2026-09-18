"""Unit tests for ``v3core.reliability.models``.

Pins the JSON-safety of ``to_dict()``, the three-state aggregation rule,
and the field completeness of ``Diagnosis`` / ``RepairAction`` (the next
batch's placeholders).
"""
from __future__ import annotations

import json

import pytest

from v3core.reliability.models import (
    OVERALL_DEGRADED,
    OVERALL_HEALTHY,
    OVERALL_UNHEALTHY,
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


def _cr(status: str, *, check_id: str = "X", section: str = "test", duration_ms: int = 0) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        section=section,
        status=status,
        summary=f"{check_id}:{status}",
        evidence={"k": "v"},
        duration_ms=duration_ms,
    )


# ── to_dict / JSON safety ──


def test_check_result_to_dict_is_json_safe():
    cr = CheckResult(
        check_id="RT01_import_source",
        section="runtime",
        status=STATUS_OK,
        summary="ok",
        evidence={"kind": "site_packages", "label": {"kind": "module_file", "leaf": "__init__.py", "hash12": "abc123abc123"}},
        duration_ms=12,
    )
    d = cr.to_dict()
    assert d == {
        "check_id": "RT01_import_source",
        "section": "runtime",
        "status": "ok",
        "summary": "ok",
        "evidence": {
            "kind": "site_packages",
            "label": {"kind": "module_file", "leaf": "__init__.py", "hash12": "abc123abc123"},
        },
        "duration_ms": 12,
    }
    # JSON-roundtrip — proves pure primitives (no datetimes / Decimals).
    encoded = json.dumps(d, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded == d


def test_health_report_to_dict_serializes_checks_as_list_of_dicts():
    report = HealthReport(
        overall="healthy",
        generated_at="2026-09-19T12:00:00+00:00",
        profile={"label": "default"},
        runtime={"python": {"version": "3.11.16"}},
        storage={"reachable": True},
        memory_write={"recent_qa": 4},
        failure_accounting={"total": 0},
        derived_memory={"topics_total": 10},
        providers={"configured": {"embed": True}},
        metrics={"check_count": 6},
        checks=[
            _cr(STATUS_OK, check_id="RT01"),
            _cr(STATUS_OK, check_id="ST01"),
            _cr(STATUS_OK, check_id="MW01"),
            _cr(STATUS_OK, check_id="FA01"),
            _cr(STATUS_OK, check_id="DM01"),
            _cr(STATUS_OK, check_id="PR01"),
        ],
        window_hours=24,
        deep=False,
    )
    d = report.to_dict()
    assert isinstance(d["checks"], list)
    assert all(isinstance(c, dict) for c in d["checks"])
    assert [c["check_id"] for c in d["checks"]] == ["RT01", "ST01", "MW01", "FA01", "DM01", "PR01"]
    # JSON-roundtrip — confirms the full dataclass tree is pure JSON.
    encoded = json.dumps(d, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["overall"] == "healthy"
    assert decoded["window_hours"] == 24
    assert decoded["deep"] is False
    assert decoded["schema_version"] == "1"


def test_health_report_to_dict_is_key_sorted():
    """The contract says key order is fixed and sort_keys=True applies."""
    report = HealthReport(
        overall="healthy",
        runtime={"z_last": 1, "a_first": 2},
        storage={"y": 1, "b": 2},
        checks=[_cr(STATUS_OK)],
    )
    d = report.to_dict()
    assert list(d["runtime"].keys()) == ["a_first", "z_last"]
    assert list(d["storage"].keys()) == ["b", "y"]


def test_health_report_defaults_are_sane():
    r = HealthReport()
    assert r.schema_version == "1"
    assert r.overall == "healthy"
    assert r.window_hours == 24
    assert r.deep is False
    assert r.checks == []
    d = r.to_dict()
    assert d["checks"] == []
    json.dumps(d)  # raises on non-JSON; passing = OK.


# ── aggregate_overall ──


def test_aggregate_all_ok_is_healthy():
    assert aggregate_overall([_cr(STATUS_OK), _cr(STATUS_OK)]) == OVERALL_HEALTHY


def test_aggregate_any_fail_is_unhealthy():
    checks = [_cr(STATUS_OK), _cr(STATUS_FAIL, check_id="MW01"), _cr(STATUS_WARN)]
    assert aggregate_overall(checks) == OVERALL_UNHEALTHY


def test_aggregate_warn_only_is_degraded():
    checks = [_cr(STATUS_OK), _cr(STATUS_WARN, check_id="MW03")]
    assert aggregate_overall(checks) == OVERALL_DEGRADED


def test_aggregate_skip_does_not_affect_verdict():
    """``skip`` must not lower the verdict (DESIGN §5)."""
    checks = [_cr(STATUS_OK), _cr(STATUS_SKIP), _cr(STATUS_OK)]
    assert aggregate_overall(checks) == OVERALL_HEALTHY


def test_aggregate_empty_is_healthy():
    assert aggregate_overall([]) == OVERALL_HEALTHY


def test_aggregate_fail_takes_precedence_over_warn():
    checks = [_cr(STATUS_WARN), _cr(STATUS_FAIL), _cr(STATUS_UNKNOWN)]
    assert aggregate_overall(checks) == OVERALL_UNHEALTHY


# ── Diagnosis / RepairAction fields ──


def test_diagnosis_fields_round_trip():
    d = Diagnosis(
        code="RECENT_EMBEDDING_FAILURE",
        severity="error",
        scope="memory_write",
        summary="recent null embeddings",
        evidence={"recent_null": 3},
        repairable=True,
    )
    out = d.to_dict()
    assert out == {
        "code": "RECENT_EMBEDDING_FAILURE",
        "severity": "error",
        "scope": "memory_write",
        "summary": "recent null embeddings",
        "evidence": {"recent_null": 3},
        "repairable": True,
    }
    json.dumps(out)  # JSON-safe


def test_repair_action_fields_round_trip():
    ra = RepairAction(
        action_id="REBUILD_EMBEDDING_FOR_QA_IDS",
        issue_code="RECENT_EMBEDDING_FAILURE",
        target_count=5,
        risk="low",
        reversible=False,
        requires_provider=True,
        estimated_remote_calls=5,
        estimated_cost={"usd": 0.001},
        writes_database=True,
        automatic_safe=False,
        reason="v1: manual review required",
    )
    out = ra.to_dict()
    assert out == {
        "action_id": "REBUILD_EMBEDDING_FOR_QA_IDS",
        "issue_code": "RECENT_EMBEDDING_FAILURE",
        "target_count": 5,
        "risk": "low",
        "reversible": False,
        "requires_provider": True,
        "estimated_remote_calls": 5,
        "estimated_cost": {"usd": 0.001},
        "writes_database": True,
        "automatic_safe": False,
        "reason": "v1: manual review required",
    }
    json.dumps(out)


# ── status enum presence ──


@pytest.mark.parametrize("status", [STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIP, STATUS_UNKNOWN])
def test_status_enum_members_are_recognized(status):
    """The five status values must all be round-trippable."""
    cr = _cr(status)
    assert cr.to_dict()["status"] == status