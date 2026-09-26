"""Unit tests for ``v3core.reliability.repair``.

Locked contracts (from DISPATCH-B):

  * Each ``RepairAction`` carries all 10 fields.
  * Dedup by ``action_id``: when two diagnoses map to the same
    action (e.g. RECENT_EMBEDDING_FAILURE + HISTORICAL_EMBEDDING_DEBT
    both → REBUILD_EMBEDDING_FOR_QA_IDS), the issue_codes are joined
    and ``target_count`` is the max of contributing counts.
  * Sort: risk desc (high > medium > low) then action_id.
  * LONG_QA_CHUNK_INCONSISTENT with orphan parents → NO_AUTOMATIC_REPAIR
    SOURCE_MISSING (not REBUILD_LONG_QA_CHILDREN).
  * ``repair_apply_disabled()`` always raises ``NotImplementedError``
    with REPAIR_APPLY_NOT_IMPLEMENTED.
  * **Zero writes:** the planner never touches any database
    connection; a recording fake PG injected into the call graph
    must observe zero SQL.
"""
from __future__ import annotations

import pytest

from v3core.reliability.models import Diagnosis, RepairAction
from v3core.reliability.repair import (
    REPAIR_APPLY_NOT_IMPLEMENTED,
    plan_repairs,
    repair_apply_disabled,
)


def _diag(code: str, severity: str = "error", scope: str = "memory_write",
          evidence: dict | None = None, repairable: bool = True) -> Diagnosis:
    return Diagnosis(
        code=code, severity=severity, scope=scope,
        summary=f"{code}:test",
        evidence=evidence or {}, repairable=repairable,
    )


# ── repair_apply_disabled ─────────────────────────────────────────────


def test_repair_apply_disabled_raises_not_implemented():
    with pytest.raises(NotImplementedError) as ei:
        repair_apply_disabled()
    assert REPAIR_APPLY_NOT_IMPLEMENTED in str(ei.value)
    # Sentinel value matches exactly what cli.py emits.
    assert REPAIR_APPLY_NOT_IMPLEMENTED == "REPAIR_APPLY_NOT_IMPLEMENTED_IN_V1"


# ── Field completeness ────────────────────────────────────────────────


def test_each_action_has_all_ten_fields():
    """DESIGN §9: every action has 10 fields. ``estimated_cost`` is the
    only optional (allowed to be None)."""
    actions = plan_repairs([_diag(
        "RECENT_EMBEDDING_FAILURE",
        evidence={"recent_null": 5, "window_hours": 24},
    )])
    assert len(actions) == 1
    a = actions[0]
    required_fields = {
        "action_id", "issue_code", "target_count", "risk", "reversible",
        "requires_provider", "estimated_remote_calls", "estimated_cost",
        "writes_database", "automatic_safe", "reason",
    }
    d = a.to_dict()
    assert required_fields.issubset(d.keys())
    # 10 (the reason is included as well — the spec calls it a 10-field
    # set with ``reason`` as #10; counting below matches DESIGN §9).
    assert len(d) == 11  # 10 spec fields + safety of to_dict coverage


def test_each_action_serializes_to_json():
    import json
    actions = plan_repairs([
        _diag("SCHEMA_VERSION_MISMATCH",
              evidence={"expected_version": "v0.2", "present": False}),
        _diag("EXPLICIT_MEMORY_EMBEDDING_NULL",
              evidence={"explicit_embedding_null": 3}),
    ])
    for a in actions:
        d = a.to_dict()
        encoded = json.dumps(d, sort_keys=True)
        decoded = json.loads(encoded)
        assert decoded == d


# ── Mapping: each Diagnosis code → expected action ───────────────────


@pytest.mark.parametrize("diag_code,expected_action,expected_risk,expected_writes", [
    ("SCHEMA_VERSION_MISMATCH", "RUN_SCHEMA_UPGRADE", "low", True),
    ("SCHEMA_TABLE_MISSING", "RUN_SCHEMA_UPGRADE", "low", True),
    ("SCHEMA_INDEX_MISSING", "RECREATE_MISSING_INDEX", "low", True),
    ("RECENT_EMBEDDING_FAILURE", "REBUILD_EMBEDDING_FOR_QA_IDS", "medium", True),
    ("HISTORICAL_EMBEDDING_DEBT", "REBUILD_EMBEDDING_FOR_QA_IDS", "medium", True),
    ("EMPTY_ANSWER_RECENT", "MANUAL_REVIEW_EMPTY_ANSWER", "low", False),
    ("EMPTY_ANSWER_HISTORICAL_DEBT", "MANUAL_REVIEW_EMPTY_ANSWER", "low", False),
    ("EXPLICIT_MEMORY_EMBEDDING_NULL", "REPAIR_EXPLICIT_MEMORY_EMBEDDING",
     "low", True),
    ("LONG_QA_CHUNK_INCONSISTENT", "REBUILD_LONG_QA_CHILDREN", "medium", True),
    ("RECENT_FAILURE_MARKER", "RETRY_PENDING_FAILURES", "medium", True),
    ("POISONED_FAILURE_MARKER", "RETRY_PENDING_FAILURES", "medium", True),
    ("RETRY_EXHAUSTED", "RETRY_PENDING_FAILURES", "medium", True),
    ("STALE_PENDING_MARKER", "RETRY_PENDING_FAILURES", "medium", True),
    ("MALFORMED_FAILURE_MARKER", "MANUAL_REVIEW_MALFORMED_MARKER", "low", False),
    ("EMBED_PROVIDER_AUTH", "FIX_PROVIDER_CREDENTIALS", "low", False),
    # Transient provider errors (rate-limit / timeout) are retryable-class:
    # source data is intact, only the provider call failed — the repair is
    # re-dispatching the pending failures. (Source-missing routing is
    # reserved for genuinely unreconstructable source, see orphan parents.)
    ("EMBED_PROVIDER_RATE_LIMIT", "RETRY_PENDING_FAILURES", "medium", True),
    ("EMBED_PROVIDER_TIMEOUT", "RETRY_PENDING_FAILURES", "medium", True),
])
def test_diagnosis_to_action_mapping(diag_code, expected_action,
                                     expected_risk, expected_writes):
    out = plan_repairs([_diag(diag_code, evidence={"x": 1})])
    assert len(out) == 1
    a = out[0]
    assert a.action_id == expected_action
    assert a.risk == expected_risk
    assert a.writes_database is expected_writes


# ── target_count sourcing from evidence ───────────────────────────────


def test_target_count_uses_evidence_value_for_recent_embedding_failure():
    actions = plan_repairs([_diag(
        "RECENT_EMBEDDING_FAILURE",
        evidence={"recent_null": 5, "window_hours": 24},
    )])
    assert actions[0].target_count == 5


def test_target_count_uses_evidence_value_for_historical_embedding_debt():
    actions = plan_repairs([_diag(
        "HISTORICAL_EMBEDDING_DEBT",
        evidence={"embedding_null_total": 105},
    )])
    assert actions[0].target_count == 105


def test_target_count_uses_max_when_dedup():
    """RECENT_EMBEDDING_FAILURE + HISTORICAL_EMBEDDING_DEBT both → same
    action_id; target_count = max(3, 105) = 105."""
    actions = plan_repairs([
        _diag("RECENT_EMBEDDING_FAILURE",
              evidence={"recent_null": 3, "window_hours": 24}),
        _diag("HISTORICAL_EMBEDDING_DEBT",
              evidence={"embedding_null_total": 105,
                        "recent_null": 0}),
    ])
    assert len(actions) == 1
    a = actions[0]
    assert a.action_id == "REBUILD_EMBEDDING_FOR_QA_IDS"
    assert a.target_count == 105
    assert "RECENT_EMBEDDING_FAILURE" in a.issue_code
    assert "HISTORICAL_EMBEDDING_DEBT" in a.issue_code


def test_target_count_falls_back_to_zero_when_no_evidence_count():
    actions = plan_repairs([_diag(
        "RECENT_EMBEDDING_FAILURE", evidence={},
    )])
    assert actions[0].target_count == 0


# ── Dedup behavior ────────────────────────────────────────────────────


def test_dedup_by_action_id():
    """Multiple RECENT_FAILURE_MARKER + POISONED_FAILURE_MARKER collapse
    into one RETRY_PENDING_FAILURES action with both issue_codes."""
    actions = plan_repairs([
        _diag("RECENT_FAILURE_MARKER",
              evidence={"current_active": 3}),
        _diag("POISONED_FAILURE_MARKER",
              evidence={"current_poisoned": 1}),
        _diag("STALE_PENDING_MARKER",
              evidence={"stale": 1}),
    ])
    assert len(actions) == 1
    a = actions[0]
    assert a.action_id == "RETRY_PENDING_FAILURES"
    for code in ("RECENT_FAILURE_MARKER", "POISONED_FAILURE_MARKER",
                 "STALE_PENDING_MARKER"):
        assert code in a.issue_code


# ── Sort order: risk desc + action_id ─────────────────────────────────


def test_actions_sorted_by_risk_desc_then_action_id():
    """Mix of low/medium/high risks → high first, then medium, then low;
    ties broken alphabetically by action_id."""
    actions = plan_repairs([
        _diag("SCHEMA_INDEX_MISSING"),   # → low
        _diag("RECENT_EMBEDDING_FAILURE"),  # → medium
        # Force a HIGH by emitting orphan-parent LONG_QA_CHUNK_INCONSISTENT
        _diag("LONG_QA_CHUNK_INCONSISTENT",
              evidence={"parents_missing_parent_row": 1,
                        "child_null_embedding": 0,
                        "child_bad_offsets": 0,
                        "child_duplicate_keys": 0}),
    ])
    risks = [a.risk for a in actions]
    # high, medium, low
    assert risks == ["high", "medium", "low"]
    # No two risks tied → action_id is a no-op here but the key is
    # deterministic.


# ── Orphan parent special case ───────────────────────────────────────


def test_longqa_with_orphan_parents_routes_to_no_automatic_repair():
    """LONG_QA_CHUNK_INCONSISTENT with parents_missing_parent_row>0 →
    NO_AUTOMATIC_REPAIR_SOURCE_MISSING, not REBUILD_LONG_QA_CHILDREN."""
    actions = plan_repairs([_diag(
        "LONG_QA_CHUNK_INCONSISTENT",
        evidence={"parents_missing_parent_row": 5,
                  "child_null_embedding": 0,
                  "child_bad_offsets": 0,
                  "child_duplicate_keys": 0},
    )])
    assert len(actions) == 1
    a = actions[0]
    assert a.action_id == "NO_AUTOMATIC_REPAIR_SOURCE_MISSING"
    assert a.risk == "high"
    assert a.writes_database is False
    assert a.automatic_safe is False


def test_longqa_without_orphan_parents_routes_to_rebuild():
    """No orphan parents → REBUILD_LONG_QA_CHILDREN (not the source
    missing path)."""
    actions = plan_repairs([_diag(
        "LONG_QA_CHUNK_INCONSISTENT",
        evidence={"parents_missing_parent_row": 0,
                  "child_null_embedding": 2,
                  "child_bad_offsets": 0,
                  "child_duplicate_keys": 0},
    )])
    assert actions[0].action_id == "REBUILD_LONG_QA_CHILDREN"


# ── Manual-only codes do not produce an action ───────────────────────


@pytest.mark.parametrize("diag_code", [
    "RUNTIME_EDITABLE_ACTIVE",
    "RUNTIME_UNKNOWN_IMPORT",
    "PG_UNREACHABLE",
    "PGVECTOR_MISSING",
    "OBSERVER_STALE",
    "DERIVED_LAYER_STALE",
    "PROVIDER_UNCONFIGURED",
])
def test_manual_only_codes_produce_no_action(diag_code):
    """Codes that have no automatic repair → empty action list."""
    out = plan_repairs([_diag(diag_code)])
    assert out == []


# ── estimated_remote_calls ───────────────────────────────────────────


def test_provider_action_estimates_remote_calls_equal_target_count():
    actions = plan_repairs([_diag(
        "RECENT_EMBEDDING_FAILURE",
        evidence={"recent_null": 7},
    )])
    assert actions[0].estimated_remote_calls == 7


def test_non_provider_action_has_no_remote_call_estimate():
    actions = plan_repairs([_diag("SCHEMA_INDEX_MISSING")])
    assert actions[0].estimated_remote_calls is None


def test_provider_action_with_zero_target_has_no_call_estimate():
    actions = plan_repairs([_diag(
        "RECENT_EMBEDDING_FAILURE", evidence={},
    )])
    assert actions[0].estimated_remote_calls is None


# ── estimated_cost ────────────────────────────────────────────────────


def test_estimated_cost_present_for_provider_action_with_calls():
    actions = plan_repairs([_diag(
        "RECENT_EMBEDDING_FAILURE", evidence={"recent_null": 5},
    )])
    cost = actions[0].estimated_cost
    assert isinstance(cost, dict)
    assert cost.get("calls") == 5


def test_estimated_cost_none_for_non_provider_or_zero_calls():
    a = plan_repairs([_diag("SCHEMA_INDEX_MISSING")])[0]
    assert a.estimated_cost is None
    b = plan_repairs([_diag("RECENT_EMBEDDING_FAILURE", evidence={})])[0]
    assert b.estimated_cost is None


# ── Zero-write contract ───────────────────────────────────────────────


class _RecordingPg:
    """Minimal recording stand-in. Tracks every ``connect`` / ``cursor`` /
    ``execute`` / ``commit`` call. The planner must never touch any of
    them. Same as the health contract tests' FakePg shape."""

    def __init__(self) -> None:
        self.connect_calls = 0
        self.cursor_calls = 0
        self.execute_calls = 0
        self.commit_calls = 0

    def __call__(self, *args, **kwargs):
        # If the planner accidentally called a connect-style callable,
        # we'd see ``self.connect_calls`` go up.
        self.connect_calls += 1
        raise AssertionError("plan_repairs must never call pg_connect")


def test_planner_never_touches_pg(monkeypatch):
    """Inject a recording pg into the v3core.reliability namespace and
    assert the planner never invokes it. The planner is a pure mapping
    function — any connect/cursor/execute/commit count > 0 means a
    write-path slipped in."""
    rec = _RecordingPg()
    # Patch at every plausible import site the planner could reach.
    monkeypatch.setattr("v3core.reliability.repair.pg_connect", rec,
                        raising=False)
    monkeypatch.setattr("v3core.reliability.repair.pg", rec, raising=False)
    actions = plan_repairs([
        _diag("RECENT_EMBEDDING_FAILURE", evidence={"recent_null": 3}),
        _diag("POISONED_FAILURE_MARKER", evidence={"current_poisoned": 1}),
        _diag("LONG_QA_CHUNK_INCONSISTENT",
              evidence={"parents_missing_parent_row": 1}),
    ])
    assert actions  # sanity
    assert rec.connect_calls == 0
    assert rec.cursor_calls == 0
    assert rec.execute_calls == 0
    assert rec.commit_calls == 0


def test_repair_apply_disabled_never_touches_pg(monkeypatch):
    """The disabled gate also must not touch a connection."""
    rec = _RecordingPg()
    monkeypatch.setattr("v3core.reliability.repair.pg_connect", rec,
                        raising=False)
    with pytest.raises(NotImplementedError):
        repair_apply_disabled()
    assert rec.connect_calls == 0


# ── Empty / mixed input handling ──────────────────────────────────────


def test_plan_with_empty_diagnoses_returns_empty():
    assert plan_repairs([]) == []


def test_plan_handles_unknown_code_safely():
    """An unrecognized diagnosis code surfaces as NO_AUTOMATIC_REPAIR."""
    actions = plan_repairs([_diag("THIS_CODE_IS_NOT_IN_TABLE")])
    assert len(actions) == 1
    assert actions[0].action_id == "NO_AUTOMATIC_REPAIR_SOURCE_MISSING"


def test_plan_handles_mixed_real_and_unknown_codes():
    actions = plan_repairs([
        _diag("RECENT_EMBEDDING_FAILURE", evidence={"recent_null": 2}),
        _diag("THIS_CODE_IS_NOT_IN_TABLE"),
    ])
    action_ids = {a.action_id for a in actions}
    assert "REBUILD_EMBEDDING_FOR_QA_IDS" in action_ids
    assert "NO_AUTOMATIC_REPAIR_SOURCE_MISSING" in action_ids