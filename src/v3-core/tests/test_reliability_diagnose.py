"""Unit tests for ``v3core.reliability.diagnose``.

Locked contracts (from DISPATCH-B):

  * **Historical-debt 双轨.** ``embedding_null_total>0 and recent==0`` →
    only ``HISTORICAL_EMBEDDING_DEBT (info)``; ``recent_null>0`` →
    ``RECENT_EMBEDDING_FAILURE (error)`` AND the debt info when
    ``total > recent``. Same for empty-answer MW03.
  * **Severity sort** is deterministic: ``error`` first, then
    ``warning``, then ``info``; ties broken alphabetically by code.
  * **No data collection.** The classifier never touches the DB /
    filesystem — it only reads ``report.checks``.
  * **Empty / all-skip reports** return ``[]``.
  * Covers ≥12 codes from DESIGN §8.
"""
from __future__ import annotations

import pytest

from v3core.reliability.diagnose import diagnose
from v3core.reliability.models import (
    CheckResult,
    HealthReport,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_UNKNOWN,
    STATUS_WARN,
)


# ── Synthetic-HealthReport helpers ────────────────────────────────────


def _check(check_id: str, status: str, evidence: dict | None = None,
           section: str = "test", summary: str = "") -> CheckResult:
    return CheckResult(
        check_id=check_id,
        section=section,
        status=status,
        summary=summary or f"{check_id}:{status}",
        evidence=evidence or {},
        duration_ms=0,
    )


def _report(*checks: CheckResult) -> HealthReport:
    return HealthReport(overall="healthy", checks=list(checks))


# ── Empty / skip-only inputs ──────────────────────────────────────────


def test_empty_report_returns_empty_list():
    assert diagnose(_report()) == []


def test_skip_only_report_returns_empty_list():
    """skip must never lower the verdict *and* never produce a diagnosis."""
    checks = [
        _check("ST01_pg_reachable", STATUS_SKIP,
               evidence={"skipped_reason": "production_boundary_no_opt_in"},
               section="storage"),
        _check("MW01_write_pipeline_recent", STATUS_SKIP,
               evidence={"skipped_reason": "production_boundary_no_opt_in"},
               section="memory_write"),
        _check("FA02_recent_failures", STATUS_SKIP,
               evidence={"skipped_reason": "no_marker_dir"},
               section="failure_accounting"),
    ]
    assert diagnose(_report(*checks)) == []


# ── RT01 import-source ────────────────────────────────────────────────


def test_rt01_editable_active_produces_error():
    r = _report(_check("RT01_import_source", STATUS_FAIL,
                       evidence={"kind": "editable"}, section="runtime"))
    out = diagnose(r)
    assert len(out) == 1
    assert out[0].code == "RUNTIME_EDITABLE_ACTIVE"
    assert out[0].severity == "error"
    assert out[0].scope == "runtime"
    assert out[0].repairable is False


def test_rt01_unknown_import_produces_warning():
    r = _report(_check("RT01_import_source", STATUS_WARN,
                       evidence={"kind": "unknown"}, section="runtime"))
    out = diagnose(r)
    assert [d.code for d in out] == ["RUNTIME_UNKNOWN_IMPORT"]
    assert out[0].severity == "warning"


# ── Storage: ST01..ST05 ───────────────────────────────────────────────


def test_st01_pg_unreachable_produces_error():
    r = _report(_check("ST01_pg_reachable", STATUS_FAIL,
                       evidence={"reachable": False, "error": "no_connection"},
                       section="storage"))
    assert [d.code for d in diagnose(r)] == ["PG_UNREACHABLE"]


def test_st02_pgvector_missing_produces_error():
    r = _report(_check("ST02_pgvector", STATUS_FAIL,
                       evidence={"present": False}, section="storage"))
    assert [d.code for d in diagnose(r)] == ["PGVECTOR_MISSING"]


def test_st03_schema_version_mismatch_produces_error_repairable():
    r = _report(_check("ST03_schema_ledger", STATUS_FAIL,
                       evidence={"expected_version": "v0.2", "present": False},
                       section="storage"))
    out = diagnose(r)
    assert [d.code for d in out] == ["SCHEMA_VERSION_MISMATCH"]
    assert out[0].repairable is True


def test_st04_canonical_table_missing_produces_error_repairable():
    r = _report(_check("ST04_canonical_tables", STATUS_FAIL,
                       evidence={"missing": ["explicit_memories"]},
                       section="storage"))
    out = diagnose(r)
    assert [d.code for d in out] == ["SCHEMA_TABLE_MISSING"]
    assert out[0].repairable is True


def test_st05_canonical_index_missing_produces_warning_repairable():
    r = _report(_check("ST05_canonical_indexes", STATUS_WARN,
                       evidence={"missing": ["explicit_memories_embedding_ivfflat"]},
                       section="storage"))
    out = diagnose(r)
    assert [d.code for d in out] == ["SCHEMA_INDEX_MISSING"]
    assert out[0].severity == "warning"
    assert out[0].repairable is True


# ── MW01 + MW02: embedding debt 双轨 ─────────────────────────────────


def test_mw01_recent_embedding_failure_produces_error_only_when_no_debt():
    """``recent_null>0`` and ``null_total == recent`` → only the error,
    no debt (no historical portion to report)."""
    r = _report(
        _check("MW01_write_pipeline_recent", STATUS_FAIL,
               evidence={"recent_qa": 5, "recent_embedding_ok": 2,
                         "recent_embedding_null": 3, "recent_empty_answer": 0,
                         "window_hours": 24},
               section="memory_write"),
        _check("MW02_embedding_debt", STATUS_OK,
               evidence={"embedding_null_total": 3,
                         "oldest_null_created_at": None,
                         "newest_null_created_at": None},
               section="memory_write"),
    )
    codes = [d.code for d in diagnose(r)]
    assert codes == ["RECENT_EMBEDDING_FAILURE"]


def test_mw01_recent_failure_with_historical_debt_keeps_both():
    """105 legacy + 3 recent → BOTH error and info (双轨)."""
    r = _report(
        _check("MW01_write_pipeline_recent", STATUS_FAIL,
               evidence={"recent_qa": 10, "recent_embedding_ok": 7,
                         "recent_embedding_null": 3, "recent_empty_answer": 0,
                         "window_hours": 24},
               section="memory_write"),
        _check("MW02_embedding_debt", STATUS_OK,
               evidence={"embedding_null_total": 108,
                         "oldest_null_created_at": "2025-08-01T00:00:00+00:00",
                         "newest_null_created_at": "2026-09-19T12:00:00+00:00"},
               section="memory_write"),
    )
    out = diagnose(r)
    codes = [d.code for d in out]
    assert "RECENT_EMBEDDING_FAILURE" in codes
    assert "HISTORICAL_EMBEDDING_DEBT" in codes
    # error before info (severity sort).
    sev = [d.severity for d in out]
    assert sev.index("error") < sev.index("info")
    # Historical debt evidence contains the *historical* count (108-3=105), not total.
    debt = next(d for d in out if d.code == "HISTORICAL_EMBEDDING_DEBT")
    assert debt.evidence["historical_null"] == 105
    assert debt.evidence["recent_null"] == 3


def test_mw02_historical_debt_only_emits_info_no_recent_failure():
    """``null_total>0 and recent==0`` → ONLY HISTORICAL_EMBEDDING_DEBT (info).
    Never RECENT_EMBEDDING_FAILURE. The critical 双轨 contract."""
    r = _report(
        _check("MW01_write_pipeline_recent", STATUS_OK,
               evidence={"recent_qa": 4, "recent_embedding_ok": 4,
                         "recent_embedding_null": 0, "recent_empty_answer": 0,
                         "window_hours": 24},
               section="memory_write"),
        _check("MW02_embedding_debt", STATUS_OK,
               evidence={"embedding_null_total": 105,
                         "oldest_null_created_at": "2024-01-01T00:00:00+00:00",
                         "newest_null_created_at": "2026-09-01T00:00:00+00:00"},
               section="memory_write"),
    )
    codes = [d.code for d in diagnose(r)]
    assert codes == ["HISTORICAL_EMBEDDING_DEBT"]
    out = diagnose(r)
    assert out[0].severity == "info"
    assert out[0].repairable is True
    # And RECENT_EMBEDDING_FAILURE must NOT appear.
    assert "RECENT_EMBEDDING_FAILURE" not in codes


# ── MW03: empty-answer 双轨 ──────────────────────────────────────────


def test_mw03_empty_answer_recent_emits_warning_and_maybe_historical_debt():
    """``empty_recent>0`` → EMPTY_ANSWER_RECENT (warning) AND debt info
    when total > recent."""
    r = _report(
        _check("MW01_write_pipeline_recent", STATUS_OK,
               evidence={"recent_qa": 10, "recent_embedding_ok": 10,
                         "recent_embedding_null": 0,
                         "recent_empty_answer": 3, "window_hours": 24},
               section="memory_write"),
        _check("MW03_empty_answer", STATUS_WARN,
               evidence={"empty_answer_total": 456, "empty_answer_recent": 3},
               section="memory_write"),
    )
    out = diagnose(r)
    codes = [d.code for d in out]
    assert "EMPTY_ANSWER_RECENT" in codes
    assert "EMPTY_ANSWER_HISTORICAL_DEBT" in codes
    recent = next(d for d in out if d.code == "EMPTY_ANSWER_RECENT")
    assert recent.severity == "warning"
    debt = next(d for d in out if d.code == "EMPTY_ANSWER_HISTORICAL_DEBT")
    assert debt.severity == "info"
    assert debt.evidence["historical_empty"] == 453


def test_mw03_empty_answer_historical_debt_only_no_recent():
    """``empty_total>0 and empty_recent==0`` → only EMPTY_ANSWER_HISTORICAL_DEBT."""
    r = _report(
        _check("MW03_empty_answer", STATUS_OK,
               evidence={"empty_answer_total": 453, "empty_answer_recent": 0},
               section="memory_write"),
    )
    codes = [d.code for d in diagnose(r)]
    assert codes == ["EMPTY_ANSWER_HISTORICAL_DEBT"]
    assert diagnose(r)[0].severity == "info"
    assert "EMPTY_ANSWER_RECENT" not in codes


# ── MW05 + MW06 ───────────────────────────────────────────────────────


def test_mw05_explicit_memory_embedding_null_produces_warning():
    r = _report(_check("MW05_explicit_memory_embedding", STATUS_WARN,
                       evidence={"explicit_total": 20, "explicit_embedding_null": 3,
                                 "explicit_embedding_set": 17},
                       section="memory_write"))
    out = diagnose(r)
    assert [d.code for d in out] == ["EXPLICIT_MEMORY_EMBEDDING_NULL"]
    assert out[0].severity == "warning"
    assert out[0].repairable is True


def test_mw06_longqa_child_inconsistent_with_orphan_parents_is_error():
    r = _report(_check("MW06_longqa_child_consistency", STATUS_FAIL,
                       evidence={"child_rows": 10, "distinct_parents": 3,
                                 "parents_missing_parent_row": 2,
                                 "child_null_embedding": 0,
                                 "child_bad_offsets": 0,
                                 "child_duplicate_keys": 0},
                       section="memory_write"))
    out = diagnose(r)
    assert [d.code for d in out] == ["LONG_QA_CHUNK_INCONSISTENT"]
    assert out[0].severity == "error"


def test_mw06_longqa_only_offset_anomalies_are_warning():
    """No orphan parents / null children → severity is warning, not error."""
    r = _report(_check("MW06_longqa_child_consistency", STATUS_FAIL,
                       evidence={"child_rows": 10, "distinct_parents": 3,
                                 "parents_missing_parent_row": 0,
                                 "child_null_embedding": 0,
                                 "child_bad_offsets": 1,
                                 "child_duplicate_keys": 0},
                       section="memory_write"))
    out = diagnose(r)
    assert out[0].code == "LONG_QA_CHUNK_INCONSISTENT"
    assert out[0].severity == "warning"


# ── Failure accounting: FA02 / FA03 / FA04 / FA05 ─────────────────────


def test_fa02_current_active_produces_error():
    r = _report(_check("FA02_recent_failures", STATUS_FAIL,
                       evidence={"current_active": 2, "current_retrying": 0,
                                 "effective_since": "2026-09-19T00:00:00+00:00"},
                       section="failure_accounting"))
    out = diagnose(r)
    assert [d.code for d in out] == ["RECENT_FAILURE_MARKER"]
    assert out[0].severity == "error"


def test_fa02_current_retrying_only_produces_retry_exhausted_warning():
    r = _report(_check("FA02_recent_failures", STATUS_WARN,
                       evidence={"current_active": 0, "current_retrying": 4,
                                 "effective_since": "2026-09-19T00:00:00+00:00"},
                       section="failure_accounting"))
    out = diagnose(r)
    assert [d.code for d in out] == ["RETRY_EXHAUSTED"]
    assert out[0].severity == "warning"


def test_fa03_poisoned_current_is_error():
    r = _report(_check("FA03_poisoned", STATUS_FAIL,
                       evidence={"total_poisoned": 4, "current_poisoned": 2,
                                 "by_error_class": {"provider_401": 4}},
                       section="failure_accounting"))
    out = diagnose(r)
    assert [d.code for d in out] == ["POISONED_FAILURE_MARKER"]
    assert out[0].severity == "error"


def test_fa03_poisoned_historical_only_is_info_with_historical_flag():
    r = _report(_check("FA03_poisoned", STATUS_OK,
                       evidence={"total_poisoned": 112, "current_poisoned": 0,
                                 "by_error_class": {"provider_401": 112}},
                       section="failure_accounting"))
    out = diagnose(r)
    assert [d.code for d in out] == ["POISONED_FAILURE_MARKER"]
    assert out[0].severity == "info"
    assert out[0].evidence.get("historical") is True


def test_fa04_malformed_produces_warning():
    r = _report(_check("FA04_malformed", STATUS_WARN,
                       evidence={"malformed": 2, "current_malformed": 0},
                       section="failure_accounting"))
    out = diagnose(r)
    assert [d.code for d in out] == ["MALFORMED_FAILURE_MARKER"]


def test_fa05_stale_produces_warning():
    r = _report(_check("FA05_stale", STATUS_WARN,
                       evidence={"stale": 1},
                       section="failure_accounting"))
    out = diagnose(r)
    assert [d.code for d in out] == ["STALE_PENDING_MARKER"]


# ── Providers: PR01 / PR02 ───────────────────────────────────────────


def test_pr01_embed_missing_produces_error():
    r = _report(_check("PR01_configured", STATUS_FAIL,
                       evidence={"embed_configured": False,
                                 "rerank_configured": True,
                                 "llm_configured": True},
                       section="providers"))
    out = diagnose(r)
    assert [d.code for d in out] == ["PROVIDER_UNCONFIGURED"]
    assert out[0].severity == "error"


def test_pr01_only_rerank_missing_produces_warning():
    r = _report(_check("PR01_configured", STATUS_WARN,
                       evidence={"embed_configured": True,
                                 "rerank_configured": False,
                                 "llm_configured": True},
                       section="providers"))
    out = diagnose(r)
    assert [d.code for d in out] == ["PROVIDER_UNCONFIGURED"]
    assert out[0].severity == "warning"


def test_pr02_recent_provider_401_produces_auth_error():
    r = _report(_check("PR02_failure_ledger", STATUS_FAIL,
                       evidence={"recent_provider_errors": {"provider_401": 5}},
                       section="providers"))
    out = diagnose(r)
    assert [d.code for d in out] == ["EMBED_PROVIDER_AUTH"]
    assert out[0].severity == "error"
    assert out[0].repairable is False


def test_pr02_recent_provider_429_produces_rate_limit_warning():
    r = _report(_check("PR02_failure_ledger", STATUS_WARN,
                       evidence={"recent_provider_errors": {"provider_429": 3}},
                       section="providers"))
    out = diagnose(r)
    assert [d.code for d in out] == ["EMBED_PROVIDER_RATE_LIMIT"]
    assert out[0].severity == "warning"
    assert out[0].repairable is True


def test_pr02_recent_provider_timeout_produces_timeout_warning():
    r = _report(_check("PR02_failure_ledger", STATUS_WARN,
                       evidence={"recent_provider_errors": {"provider_timeout": 2}},
                       section="providers"))
    out = diagnose(r)
    assert [d.code for d in out] == ["EMBED_PROVIDER_TIMEOUT"]


# ── Derived memory: DM02 / DM01 / DM04 ──────────────────────────────


def test_dm02_observer_stale_produces_warning():
    r = _report(_check("DM02_observer_cursor", STATUS_WARN,
                       evidence={"last_qa_id": 5, "qa_head_id": 200,
                                "backlog": 195, "updated_at": None,
                                "age_seconds": 86400 * 30},
                       section="derived_memory"))
    out = diagnose(r)
    assert [d.code for d in out] == ["OBSERVER_STALE"]


def test_dm04_derived_layer_stale_produces_warning():
    r = _report(_check("DM04_derived_last", STATUS_WARN,
                       evidence={"last_yin_paragraph_at": None,
                                 "last_topic_observer_at": None,
                                 "age_seconds": 86400 * 30},
                       section="derived_memory"))
    out = diagnose(r)
    assert [d.code for d in out] == ["DERIVED_LAYER_STALE"]


# ── Sort order: severity + code ──────────────────────────────────────


def test_output_is_sorted_by_severity_then_code():
    """``error`` first, then ``warning``, then ``info``; ties broken by code."""
    r = _report(
        _check("ST05_canonical_indexes", STATUS_WARN,
               evidence={"missing": ["x"]}, section="storage"),
        _check("RT01_import_source", STATUS_FAIL,
               evidence={"kind": "editable"}, section="runtime"),
        _check("MW03_empty_answer", STATUS_OK,
               evidence={"empty_answer_total": 453, "empty_answer_recent": 0},
               section="memory_write"),
        _check("ST01_pg_reachable", STATUS_FAIL,
               evidence={"reachable": False}, section="storage"),
        _check("PR02_failure_ledger", STATUS_FAIL,
               evidence={"recent_provider_errors": {"provider_401": 1}},
               section="providers"),
    )
    codes = [d.code for d in diagnose(r)]
    # Expected order: errors first (alphabetical), then warning, then info.
    # errors: EMBED_PROVIDER_AUTH, PG_UNREACHABLE, RUNTIME_EDITABLE_ACTIVE
    # warning: SCHEMA_INDEX_MISSING
    # info: EMPTY_ANSWER_HISTORICAL_DEBT
    assert codes == [
        "EMBED_PROVIDER_AUTH",
        "PG_UNREACHABLE",
        "RUNTIME_EDITABLE_ACTIVE",
        "SCHEMA_INDEX_MISSING",
        "EMPTY_ANSWER_HISTORICAL_DEBT",
    ]


def test_output_is_stable_across_calls():
    """Same input → same output (deterministic)."""
    r = _report(
        _check("ST05_canonical_indexes", STATUS_WARN,
               evidence={"missing": ["a", "b"]}, section="storage"),
        _check("FA02_recent_failures", STATUS_FAIL,
               evidence={"current_active": 1, "current_retrying": 0},
               section="failure_accounting"),
    )
    a = [d.code for d in diagnose(r)]
    b = [d.code for d in diagnose(r)]
    assert a == b


# ── Composite scenario: full production-shaped report ─────────────────


def test_full_production_shaped_report_emits_expected_codes():
    """Production-shaped: 105 historical NULLs, 0 recent; one index missing;
    one poisoned historical; one provider 401. Expected diagnoses:"""

    r = _report(
        # runtime ok
        _check("RT01_import_source", STATUS_OK,
               evidence={"kind": "site_packages"}, section="runtime"),
        # storage ok except one missing index
        _check("ST01_pg_reachable", STATUS_OK,
               evidence={"reachable": True}, section="storage"),
        _check("ST02_pgvector", STATUS_OK,
               evidence={"present": True, "version": "0.7.4"}, section="storage"),
        _check("ST03_schema_ledger", STATUS_OK,
               evidence={"expected_version": "v0.2", "present": True},
               section="storage"),
        _check("ST04_canonical_tables", STATUS_OK,
               evidence={"missing": [], "expected": []}, section="storage"),
        _check("ST05_canonical_indexes", STATUS_WARN,
               evidence={"missing": ["explicit_memories_tags_gin"]},
               section="storage"),
        # memory write: historical debt only
        _check("MW01_write_pipeline_recent", STATUS_OK,
               evidence={"recent_qa": 4, "recent_embedding_ok": 4,
                         "recent_embedding_null": 0,
                         "recent_empty_answer": 0,
                         "window_hours": 24},
               section="memory_write"),
        _check("MW02_embedding_debt", STATUS_OK,
               evidence={"embedding_null_total": 105,
                         "oldest_null_created_at": "2025-01-01T00:00:00+00:00",
                         "newest_null_created_at": "2026-08-01T00:00:00+00:00"},
               section="memory_write"),
        _check("MW03_empty_answer", STATUS_OK,
               evidence={"empty_answer_total": 453, "empty_answer_recent": 0},
               section="memory_write"),
        # failure accounting: 1 historical poisoned
        _check("FA03_poisoned", STATUS_OK,
               evidence={"total_poisoned": 112, "current_poisoned": 0,
                         "by_error_class": {"provider_401": 112}},
               section="failure_accounting"),
        # providers: 1 recent 401
        _check("PR02_failure_ledger", STATUS_FAIL,
               evidence={"recent_provider_errors": {"provider_401": 1}},
               section="providers"),
    )
    codes = [d.code for d in diagnose(r)]
    # No RECENT_EMBEDDING_FAILURE because recent==0.
    assert "RECENT_EMBEDDING_FAILURE" not in codes
    # Historical debt info IS present.
    assert "HISTORICAL_EMBEDDING_DEBT" in codes
    assert "EMPTY_ANSWER_HISTORICAL_DEBT" in codes
    assert "POISONED_FAILURE_MARKER" in codes
    assert "SCHEMA_INDEX_MISSING" in codes
    assert "EMBED_PROVIDER_AUTH" in codes


def test_unknown_check_status_can_fall_through_when_code_exists():
    """An unknown ST05 (broken read) still surfaces as the warning code."""
    r = _report(_check("ST05_canonical_indexes", STATUS_UNKNOWN,
                       evidence={"missing": [], "expected": []},
                       section="storage"))
    out = diagnose(r)
    assert [d.code for d in out] == ["SCHEMA_INDEX_MISSING"]
    assert out[0].severity == "warning"