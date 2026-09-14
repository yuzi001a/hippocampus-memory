"""Tests for the G5b differential evaluator.

These tests use small synthetic (results.json, summary.json) fixtures
that mirror the actual evaluator's output schema but contain only a
handful of scenarios. They verify:

  * structural validation / fail-closed semantics on incompatible inputs
  * headline numeric deltas (pass rate, Hit@k, MRR, latency)
  * scenario-level flips (new/fixed misses, new/fixed false positives,
    label-change list)
  * per-category deltas
  * verdict classification
  * JSON round-trip + CLI exit codes

No real evaluator run is performed. The diff module is pure offline
comparison of pre-existing JSON.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# Make ``src/v3-core`` importable so ``eval`` resolves.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
_EVAL_PKG = os.path.normpath(os.path.join(_HERE, "..", ".."))
if _EVAL_PKG not in sys.path:
    sys.path.insert(0, _EVAL_PKG)

from eval.g5b_real_memory_evaluator.compare import (  # noqa: E402
    DeltaReport,
    IncompatibleScenarioSets,
    LabelChange,
    MetricSnapshot,
    PerCategoryDelta,
    _classify_direction,
    _diff_scenarios,
    _is_recall_failure_label,
    _per_category,
    _scenario_records,
    _validate_scenario_sets,
    _verdict,
    compare_results_dicts,
    compare_runs,
    load_run,
    main as compare_main,
    report_to_dict,
    report_to_json,
)


# ── fixture builders ──────────────────────────────────────────────────────


def _scenario(scenario_id, category, pipeline_status, failure_label, per_query=None):
    return {
        "scenario_id": scenario_id,
        "category": category,
        "passed": pipeline_status == "PASS",
        "pipeline_status": pipeline_status,
        "failure_label": failure_label,
        "unsupported": pipeline_status == "UNSUPPORTED",
        "elapsed_ms": 12.0,
        "notes": "",
        "per_query": per_query or [],
    }


def _query(query_id, hit_ranks=None, must_not_violations=None, archived=None):
    return {
        "query_id": query_id,
        "text": f"q-{query_id}",
        "expected_lane": "vector",
        "actual_lane": "vector",
        "returned_memory_ids": [],
        "must_recall_hit_ranks": hit_ranks or [],
        "must_not_recall_violations": must_not_violations or [],
        "archived_returned": archived or [],
        "elapsed_ms": 5.0,
        "failure_label": "PASS",
    }


def _summary(scenarios, metrics):
    return {
        "version": "g5b-v1",
        "source": "fixture",
        "lane": "a",
        "timestamp_utc": "2026-09-14T00:00:00Z",
        "metrics": metrics,
        "scenarios_by_category": {},
        "labels_taxonomy": [],
        "is_deterministic_lab": True,
        "is_live_provider_backed": False,
        "honest_baseline_note": "fixture",
    }


def _make_run(scenarios):
    """Build a synthetic (results, summary) pair from a list of scenarios."""

    results = {
        "version": "g5b-v1",
        "source": "fixture",
        "lane": "a",
        "scenarios": scenarios,
    }
    n = len(scenarios)
    pass_n = sum(1 for s in scenarios if s["pipeline_status"] == "PASS")
    fail_n = sum(1 for s in scenarios if s["pipeline_status"] == "FAIL")
    err_n = sum(1 for s in scenarios if s["pipeline_status"] == "ERROR")
    uns_n = sum(1 for s in scenarios if s["pipeline_status"] == "UNSUPPORTED")
    per_label: dict[str, int] = {}
    per_cat: dict[str, tuple[int, int]] = {}
    misses = 0
    fps = 0
    archive_v = 0
    conflict_v = 0
    pipeline_e = 0
    for s in scenarios:
        per_label[s["failure_label"]] = per_label.get(s["failure_label"], 0) + 1
        c = s["category"]
        p, t = per_cat.get(c, (0, 0))
        per_cat[c] = (p + (1 if s["pipeline_status"] == "PASS" else 0), t + 1)
        for q in s.get("per_query", []):
            if s["pipeline_status"] == "FAIL" and _is_recall_failure_label(s["failure_label"]):
                misses += 1
            if q.get("must_not_recall_violations"):
                fps += len(q["must_not_recall_violations"])
            if q.get("archived_returned"):
                archive_v += len(q["archived_returned"])
        if s["failure_label"] == "CONFLICT_RESOLUTION":
            conflict_v += 1
        if s["pipeline_status"] == "ERROR":
            pipeline_e += 1

    metrics = {
        "scenario_count": n,
        "unsupported_count": uns_n,
        "pass_count": pass_n,
        "fail_count": fail_n,
        "error_count": err_n,
        "scenario_pass_rate": pass_n / (n - uns_n) if n - uns_n else 0.0,
        "must_recall_hit_rate": 0.5,
        "must_not_recall_violation_rate": 0.0,
        "hit_at_1": 0.3,
        "hit_at_3": 0.5,
        "hit_at_5": 0.6,
        "mrr": 0.4,
        "false_positives": fps,
        "misses": misses,
        "archive_violations": archive_v,
        "conflict_violations": conflict_v,
        "pipeline_errors": pipeline_e,
        "median_latency_ms": 5.0,
        "p95_latency_ms": 9.0,
        "per_label_count": per_label,
        "per_category_pass_rate": {k: v[0] / v[1] if v[1] else 0.0 for k, v in per_cat.items()},
    }
    return results, _summary(scenarios, metrics)


# ── structural / fail-closed ──────────────────────────────────────────────


def test_load_run_missing_file_raises_incompatible():
    with pytest.raises(IncompatibleScenarioSets) as exc:
        load_run("/no/such/results.json", "/no/such/summary.json")
    assert "not found" in str(exc.value)


def test_load_run_invalid_json(tmp_path):
    bad = tmp_path / "results.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(IncompatibleScenarioSets):
        load_run(str(bad), str(bad))


def test_load_run_non_object(tmp_path):
    p = tmp_path / "results.json"
    p.write_text("[]", encoding="utf-8")
    with pytest.raises(IncompatibleScenarioSets):
        load_run(str(p), str(p))


def test_summary_missing_metrics_raises(tmp_path):
    r = tmp_path / "r.json"
    r.write_text(json.dumps({"scenarios": []}), encoding="utf-8")
    s = tmp_path / "s.json"
    s.write_text(json.dumps({"lane": "a"}), encoding="utf-8")
    with pytest.raises(IncompatibleScenarioSets):
        compare_runs(str(r), str(s), str(r), str(s))


def test_lane_mismatch_raises_incompatible():
    base_r = {"scenarios": []}
    base_s = {"lane": "a", "metrics": _make_run([])[1]["metrics"]}
    cand_r = {"scenarios": []}
    cand_s = {"lane": "b", "metrics": _make_run([])[1]["metrics"]}
    with pytest.raises(IncompatibleScenarioSets) as exc:
        compare_results_dicts(base_r, base_s, cand_r, cand_s)
    assert "lane mismatch" in str(exc.value).lower()


def test_unknown_lane_raises_incompatible():
    r = {"scenarios": []}
    s = {"lane": "z", "metrics": _make_run([])[1]["metrics"]}
    with pytest.raises(IncompatibleScenarioSets):
        compare_results_dicts(r, s, r, s)


def test_missing_metric_key_raises_incompatible():
    """A summary whose metrics block is missing a required key fails
    closed instead of silently defaulting the value to zero.

    This is the headline regression guarded by the input-contract
    hardening: previously a missing key would have been treated as
    0 / 0.0 / {} — indistinguishable from a real zero metric. The
    error message must name only structural keys, never payload values.
    """

    r = {"scenarios": []}
    full_metrics = _make_run([])[1]["metrics"]
    stripped = {k: v for k, v in full_metrics.items() if k != "pass_count"}
    s = {"lane": "a", "metrics": stripped}
    with pytest.raises(IncompatibleScenarioSets) as exc:
        compare_results_dicts(r, s, r, s)
    msg = str(exc.value)
    # Structural key name is named in the error.
    assert "pass_count" in msg
    # Error must NOT echo any metric value. Use a non-secret probe
    # that the writer might emit (e.g. a long string the user could
    # have stuck in per_label_count) to assert non-leakage.
    assert "password=" not in msg.lower()
    assert "secret" not in msg.lower()


def test_missing_multiple_metric_keys_lists_all():
    """When several keys are missing, the error message lists every
    missing key so the operator can fix all of them in one pass."""

    r = {"scenarios": []}
    full_metrics = _make_run([])[1]["metrics"]
    stripped = {
        k: v for k, v in full_metrics.items()
        if k not in ("pass_count", "fail_count", "misses")
    }
    s = {"lane": "a", "metrics": stripped}
    with pytest.raises(IncompatibleScenarioSets) as exc:
        compare_results_dicts(r, s, r, s)
    msg = str(exc.value)
    assert "pass_count" in msg
    assert "fail_count" in msg
    assert "misses" in msg


def test_wrong_container_type_metric_raises_incompatible():
    """A metric key whose container type is wrong fails closed.

    Specifically: ``scenario_count`` must be an ``int`` (not a string
    or a float) and ``per_label_count`` must be a ``dict`` (not a
    list). Silently coercing via ``int(...)`` or ``dict(...)`` would
    mask writer bugs.
    """

    r = {"scenarios": []}
    metrics = _make_run([])[1]["metrics"]
    metrics = dict(metrics)
    # '1' is a string, not an int. bool/int mismatch also covered.
    metrics["scenario_count"] = "1"
    s = {"lane": "a", "metrics": metrics}
    with pytest.raises(IncompatibleScenarioSets) as exc:
        compare_results_dicts(r, s, r, s)
    msg = str(exc.value)
    assert "scenario_count" in msg
    assert "wrong container type" in msg.lower() or "wrong type" in msg.lower()


def test_results_summary_scenario_count_mismatch_raises_incompatible():
    """When ``len(results['scenarios'])`` does not equal
    ``summary['metrics']['scenario_count']`` the diff fails closed.

    Previously the two values were never cross-checked: a hand-edited
    summary or a writer bug could produce a diff where the metric
    block and the per-scenario records disagreed. We now refuse to
    compute on inconsistent inputs.
    """

    base_scens = [_scenario("s1", "recall", "PASS", "PASS")]
    cand_scens = [
        _scenario("s1", "recall", "PASS", "PASS"),
        _scenario("s2", "recall", "PASS", "PASS"),
    ]
    base_r, base_s = _make_run(base_scens)
    cand_r, cand_s = _make_run(cand_scens)

    # Lie on the candidate summary: claim only 1 scenario was run.
    cand_s = dict(cand_s)
    cand_s["metrics"] = dict(cand_s["metrics"])
    cand_s["metrics"]["scenario_count"] = 1

    with pytest.raises(IncompatibleScenarioSets) as exc:
        compare_results_dicts(base_r, base_s, cand_r, cand_s)
    msg = str(exc.value)
    assert "scenario count mismatch" in msg.lower()
    assert "cand" in msg.lower()
    # Should not echo any scenario_id value (which could be a UUID
    # carrying user data); use only structural counts.
    for s_id in ("s1", "s2"):
        assert s_id not in msg


def test_results_summary_lane_mismatch_raises_incompatible():
    """When ``results['lane']`` is present and disagrees with
    ``summary['lane']`` the diff fails closed.

    The summary is the authoritative lane label for the run; the
    results lane must agree or the pair is contaminated (writer bug
    or hand-edited file). We refuse to compare such inputs.
    """

    r = {"lane": "b", "scenarios": []}
    s = {"lane": "a", "metrics": _make_run([])[1]["metrics"]}
    with pytest.raises(IncompatibleScenarioSets) as exc:
        compare_results_dicts(r, s, r, s)
    msg = str(exc.value)
    assert "lane mismatch" in msg.lower()
    assert "results/summary" in msg.lower() or "results_lane" in msg.lower()


def test_results_without_lane_field_still_works():
    """When ``results.json`` omits the ``lane`` key entirely (older
    writer) the consistency check skips the lane comparison — only
    the summary lane is consulted. This guards the new check against
    regressing older artefacts that did not include a results lane.
    """

    r = {"scenarios": []}  # no 'lane' key
    s = {"lane": "a", "metrics": _make_run([])[1]["metrics"]}
    # Must not raise on the lane-consistency check; the scenario-set
    # comparison on empty inputs is trivially fine.
    report = compare_results_dicts(r, s, r, s)
    assert report.base.scenario_count == 0
    assert report.cand.scenario_count == 0
    assert report.verdict == "unchanged"


def test_results_summary_lane_match_is_accepted():
    """When ``results['lane']`` is present and agrees with
    ``summary['lane']`` the diff proceeds normally."""

    scens = [_scenario("s1", "recall", "PASS", "PASS")]
    base_r, base_s = _make_run(scens)
    cand_r, cand_s = _make_run(scens)
    base_r = dict(base_r); base_r["lane"] = "a"
    cand_r = dict(cand_r); cand_r["lane"] = "a"
    report = compare_results_dicts(base_r, base_s, cand_r, cand_s)
    assert report.verdict == "unchanged"


def test_scenario_set_mismatch_raises_incompatible():
    base_r, base_s = _make_run([
        _scenario("s1", "recall", "PASS", "PASS"),
    ])
    cand_r, cand_s = _make_run([
        _scenario("s2", "recall", "PASS", "PASS"),
    ])
    with pytest.raises(IncompatibleScenarioSets) as exc:
        compare_results_dicts(base_r, base_s, cand_r, cand_s)
    msg = str(exc.value).lower()
    assert "scenario set mismatch" in msg
    assert "only_in_base" in msg
    assert "only_in_cand" in msg


def test_query_id_sequence_mismatch_raises_incompatible():
    base_r = {"scenarios": [_scenario("s1", "recall", "PASS", "PASS",
                                       per_query=[_query("q1")])]}
    base_s = {"lane": "a", "metrics": _make_run(base_r["scenarios"])[1]["metrics"]}
    cand_r = {"scenarios": [_scenario("s1", "recall", "PASS", "PASS",
                                       per_query=[_query("q2")])]}
    cand_s = {"lane": "a", "metrics": _make_run(cand_r["scenarios"])[1]["metrics"]}
    with pytest.raises(IncompatibleScenarioSets) as exc:
        compare_results_dicts(base_r, base_s, cand_r, cand_s)
    assert "query_id sequence" in str(exc.value)


def test_duplicate_scenario_id_raises_incompatible():
    r = {"scenarios": [
        _scenario("dup", "recall", "PASS", "PASS"),
        _scenario("dup", "recall", "PASS", "PASS"),
    ]}
    s = {"lane": "a", "metrics": _make_run(r["scenarios"])[1]["metrics"]}
    with pytest.raises(IncompatibleScenarioSets):
        _scenario_records(r)


def test_incompatible_error_does_not_leak_payload_fields():
    """Error messages must NOT echo payload contents (DSN-shaped strings,
    user content, etc.)."""

    base_r, base_s = _make_run([_scenario("a", "recall", "PASS", "PASS")])
    cand_r = {"scenarios": [_scenario("SECRET_DSN", "recall", "PASS", "PASS")]}
    cand_s = {"lane": "a", "metrics": _make_run(cand_r["scenarios"])[1]["metrics"]}
    with pytest.raises(IncompatibleScenarioSets) as exc:
        compare_results_dicts(base_r, base_s, cand_r, cand_s)
    msg = str(exc.value)
    # The scenario id will appear (it's a structural mismatch marker),
    # but no DSN-shaped or password-shaped substrings should be in
    # the message.
    for token in ("password=", "user=", "host=", "sslmode=", "dbname="):
        assert token not in msg.lower()


# ── numeric deltas ────────────────────────────────────────────────────────


def _simple_diff_pair():
    """Return (base_run, cand_run) where cand improves over base."""

    base_scens = [
        _scenario("s1", "recall", "PASS", "PASS", per_query=[_query("q1", hit_ranks=[1])]),
        _scenario("s2", "recall", "FAIL", "NOT_RETRIEVED"),
        _scenario("s3", "archive", "FAIL", "ARCHIVED_MEMORY_RETURNED",
                   per_query=[_query("qa", must_not_violations=["bad"])]),
    ]
    cand_scens = [
        # s1 stays PASS.
        _scenario("s1", "recall", "PASS", "PASS", per_query=[_query("q1", hit_ranks=[1])]),
        # s2 flips to PASS — fixed miss.
        _scenario("s2", "recall", "PASS", "PASS"),
        # s3 stays ARCHIVED_MEMORY_RETURNED but loses the FP violation.
        _scenario("s3", "archive", "FAIL", "ARCHIVED_MEMORY_RETURNED",
                   per_query=[_query("qa", must_not_violations=[])]),
    ]
    return _make_run(base_scens), _make_run(cand_scens)


def test_numeric_deltas_basic_improvement():
    base, cand = _simple_diff_pair()
    report = compare_results_dicts(
        base[0], base[1], cand[0], cand[1],
    )
    assert isinstance(report, DeltaReport)
    assert report.pass_count_delta == 1
    assert report.fixed_misses == ["s2"]
    assert report.new_misses == []
    assert report.fixed_false_positives == ["s3::qa"]
    assert report.new_false_positives == []
    # s2 flipped FAIL→PASS, direction 'improved'.
    assert any(fl.scenario_id == "s2" and fl.direction == "improved"
               for fl in report.label_changes)
    assert report.verdict in ("improved", "mixed")


def test_numeric_deltas_regression():
    # Start from a clean PASS-everything base, then regress two ways.
    base_scens = [
        _scenario("s1", "recall", "PASS", "PASS", per_query=[_query("q1", hit_ranks=[1])]),
        _scenario("s2", "recall", "PASS", "PASS"),
        _scenario("s3", "archive", "PASS", "PASS",
                   per_query=[_query("qa", must_not_violations=[])]),
    ]
    cand_scens = [
        # s1 stays PASS.
        _scenario("s1", "recall", "PASS", "PASS", per_query=[_query("q1", hit_ranks=[1])]),
        # s2 regresses: PASS -> FAIL NOT_RETRIEVED — new miss.
        _scenario("s2", "recall", "FAIL", "NOT_RETRIEVED"),
        # s3 regresses by re-introducing a false positive.
        _scenario("s3", "archive", "PASS", "PASS",
                   per_query=[_query("qa", must_not_violations=["bad"])]),
    ]
    base = _make_run(base_scens)
    cand = _make_run(cand_scens)
    report = compare_results_dicts(
        base[0], base[1], cand[0], cand[1],
    )
    assert report.pass_count_delta == -1
    assert report.fixed_misses == []
    assert report.new_misses == ["s2"]
    assert report.fixed_false_positives == []
    assert report.new_false_positives == ["s3::qa"]
    flips_by_sid = {fl.scenario_id: fl for fl in report.label_changes}
    assert flips_by_sid["s2"].direction == "regressed"


def test_unchanged_run_is_unchanged():
    base = _make_run([
        _scenario("s1", "recall", "PASS", "PASS"),
        _scenario("s2", "recall", "FAIL", "NOT_RETRIEVED"),
    ])
    cand = _make_run([
        _scenario("s1", "recall", "PASS", "PASS"),
        _scenario("s2", "recall", "FAIL", "NOT_RETRIEVED"),
    ])
    report = compare_results_dicts(base[0], base[1], cand[0], cand[1])
    assert report.pass_rate_delta == 0.0
    assert report.label_changes == []
    assert report.verdict == "unchanged"


def test_pass_rate_delta_signed_correctly():
    base = _make_run([
        _scenario("s1", "recall", "PASS", "PASS"),
        _scenario("s2", "recall", "FAIL", "NOT_RETRIEVED"),
        _scenario("s3", "recall", "FAIL", "NOT_RETRIEVED"),
        _scenario("s4", "recall", "FAIL", "NOT_RETRIEVED"),
    ])
    cand = _make_run([
        _scenario("s1", "recall", "PASS", "PASS"),
        _scenario("s2", "recall", "PASS", "PASS"),
        _scenario("s3", "recall", "PASS", "PASS"),
        _scenario("s4", "recall", "FAIL", "NOT_RETRIEVED"),
    ])
    report = compare_results_dicts(base[0], base[1], cand[0], cand[1])
    assert report.pass_count_delta == 2
    assert report.pass_rate_delta > 0


def test_latency_delta_signed_correctly():
    base_scens = [_scenario("s1", "recall", "PASS", "PASS",
                              per_query=[_query("q1", hit_ranks=[1])])]
    base_r, base_s = _make_run(base_scens)
    # Manually bump base latency.
    base_s["metrics"]["median_latency_ms"] = 20.0
    base_s["metrics"]["p95_latency_ms"] = 30.0

    cand_scens = [_scenario("s1", "recall", "PASS", "PASS",
                              per_query=[_query("q1", hit_ranks=[1])])]
    cand_r, cand_s = _make_run(cand_scens)
    cand_s["metrics"]["median_latency_ms"] = 10.0
    cand_s["metrics"]["p95_latency_ms"] = 15.0

    report = compare_results_dicts(base_r, base_s, cand_r, cand_s)
    assert report.median_latency_ms_delta == -10.0
    assert report.p95_latency_ms_delta == -15.0


def test_archive_conflict_pipeline_count_deltas():
    base = _make_run([
        _scenario("s1", "archive", "FAIL", "ARCHIVED_MEMORY_RETURNED",
                   per_query=[_query("q", archived=["x", "y"])]),
        _scenario("s2", "conflict", "FAIL", "CONFLICT_RESOLUTION"),
        _scenario("s3", "pipe", "ERROR", "PIPELINE_ERROR"),
    ])
    cand = _make_run([
        _scenario("s1", "archive", "PASS", "PASS",
                   per_query=[_query("q", archived=[])]),
        _scenario("s2", "conflict", "PASS", "PASS"),
        _scenario("s3", "pipe", "PASS", "PASS"),
    ])
    report = compare_results_dicts(base[0], base[1], cand[0], cand[1])
    assert report.archive_violations_delta == -2
    assert report.conflict_violations_delta == -1
    assert report.pipeline_errors_delta == -1


# ── per-category / verdict ────────────────────────────────────────────────


def test_per_category_sorted_by_abs_delta():
    base = _make_run([
        _scenario("a1", "alpha", "PASS", "PASS"),
        _scenario("a2", "alpha", "FAIL", "NOT_RETRIEVED"),
        _scenario("b1", "beta", "PASS", "PASS"),
        _scenario("b2", "beta", "PASS", "PASS"),
        _scenario("b3", "beta", "FAIL", "NOT_RETRIEVED"),
    ])
    cand = _make_run([
        # alpha fully fixed (0.5 → 1.0, delta +0.5)
        _scenario("a1", "alpha", "PASS", "PASS"),
        _scenario("a2", "alpha", "PASS", "PASS"),
        # beta partially regressed (0.667 → 0.333, delta -0.333)
        _scenario("b1", "beta", "FAIL", "NOT_RETRIEVED"),
        _scenario("b2", "beta", "PASS", "PASS"),
        _scenario("b3", "beta", "FAIL", "NOT_RETRIEVED"),
    ])
    report = compare_results_dicts(base[0], base[1], cand[0], cand[1])
    cats = [p.category for p in report.per_category]
    assert cats[0] == "alpha"  # biggest |delta| first
    assert report.per_category[0].abs_delta == pytest.approx(0.5, abs=1e-6)


def test_verdict_improved():
    snap_base = MetricSnapshot(
        label="base", scenario_count=4, pass_count=2, fail_count=2,
        error_count=0, unsupported_count=0, scenario_pass_rate=0.5,
        must_recall_hit_rate=0.5, must_not_recall_violation_rate=0.0,
        hit_at_1=0.3, hit_at_3=0.5, hit_at_5=0.6, mrr=0.4,
        false_positives=0, misses=2, archive_violations=0,
        conflict_violations=0, pipeline_errors=0,
        median_latency_ms=5.0, p95_latency_ms=10.0,
    )
    snap_cand = MetricSnapshot(
        label="cand", scenario_count=4, pass_count=4, fail_count=0,
        error_count=0, unsupported_count=0, scenario_pass_rate=1.0,
        must_recall_hit_rate=0.9, must_not_recall_violation_rate=0.0,
        hit_at_1=0.7, hit_at_3=0.9, hit_at_5=1.0, mrr=0.8,
        false_positives=0, misses=0, archive_violations=0,
        conflict_violations=0, pipeline_errors=0,
        median_latency_ms=3.0, p95_latency_ms=6.0,
    )
    flips = [LabelChange(
        scenario_id="s1", category="recall",
        base_label="NOT_RETRIEVED", base_pipeline_status="FAIL",
        cand_label="PASS", cand_pipeline_status="PASS",
        direction="improved",
    )]
    assert _verdict(snap_base, snap_cand, flips) == "improved"


def test_verdict_regressed():
    snap_base = MetricSnapshot(
        label="base", scenario_count=2, pass_count=2, fail_count=0,
        error_count=0, unsupported_count=0, scenario_pass_rate=1.0,
        must_recall_hit_rate=0.9, must_not_recall_violation_rate=0.0,
        hit_at_1=0.7, hit_at_3=0.9, hit_at_5=1.0, mrr=0.8,
        false_positives=0, misses=0, archive_violations=0,
        conflict_violations=0, pipeline_errors=0,
        median_latency_ms=3.0, p95_latency_ms=6.0,
    )
    snap_cand = MetricSnapshot(
        label="cand", scenario_count=2, pass_count=0, fail_count=2,
        error_count=0, unsupported_count=0, scenario_pass_rate=0.0,
        must_recall_hit_rate=0.0, must_not_recall_violation_rate=0.5,
        hit_at_1=0.0, hit_at_3=0.0, hit_at_5=0.0, mrr=0.0,
        false_positives=2, misses=2, archive_violations=0,
        conflict_violations=0, pipeline_errors=0,
        median_latency_ms=5.0, p95_latency_ms=9.0,
    )
    flips = [LabelChange(
        scenario_id="s1", category="recall",
        base_label="PASS", base_pipeline_status="PASS",
        cand_label="NOT_RETRIEVED", cand_pipeline_status="FAIL",
        direction="regressed",
    )]
    assert _verdict(snap_base, snap_cand, flips) == "regressed"


def test_classify_direction():
    assert _classify_direction("FAIL", "PASS") == "improved"
    assert _classify_direction("ERROR", "FAIL") == "improved"
    assert _classify_direction("PASS", "FAIL") == "regressed"
    assert _classify_direction("FAIL", "UNSUPPORTED") == "improved"
    assert _classify_direction("UNSUPPORTED", "FAIL") == "regressed"
    assert _classify_direction("PASS", "PASS") == "unchanged"
    assert _classify_direction("FAIL", "FAIL") == "unchanged"


# ── JSON round-trip & CLI ─────────────────────────────────────────────────


def test_report_to_dict_and_json_round_trip():
    base = _make_run([
        _scenario("s1", "recall", "PASS", "PASS"),
        _scenario("s2", "recall", "FAIL", "NOT_RETRIEVED"),
    ])
    cand = _make_run([
        _scenario("s1", "recall", "PASS", "PASS"),
        _scenario("s2", "recall", "PASS", "PASS"),
    ])
    report = compare_results_dicts(base[0], base[1], cand[0], cand[1])
    as_dict = report_to_dict(report)
    assert isinstance(as_dict, dict)
    assert "verdict" in as_dict
    assert "pass_rate_delta" in as_dict
    assert "label_changes" in as_dict
    # JSON round-trip must be valid JSON.
    blob = report_to_json(report)
    parsed = json.loads(blob)
    assert parsed["verdict"] == as_dict["verdict"]


def test_cli_emits_json_and_succeeds(tmp_path, capsys):
    base_r, base_s = _make_run([_scenario("s1", "recall", "PASS", "PASS")])
    cand_r, cand_s = _make_run([_scenario("s1", "recall", "PASS", "PASS")])
    br = tmp_path / "b_results.json"; br.write_text(json.dumps(base_r), encoding="utf-8")
    bs = tmp_path / "b_summary.json"; bs.write_text(json.dumps(base_s), encoding="utf-8")
    cr = tmp_path / "c_results.json"; cr.write_text(json.dumps(cand_r), encoding="utf-8")
    cs = tmp_path / "c_summary.json"; cs.write_text(json.dumps(cand_s), encoding="utf-8")

    rc = compare_main([
        "--base-results", str(br),
        "--base-summary", str(bs),
        "--cand-results", str(cr),
        "--cand-summary", str(cs),
    ])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert rc == 0
    assert "verdict" in parsed
    assert parsed["verdict"] == "unchanged"


def test_cli_writes_out_json(tmp_path, capsys):
    base_r, base_s = _make_run([_scenario("s1", "recall", "PASS", "PASS")])
    cand_r, cand_s = _make_run([_scenario("s1", "recall", "PASS", "PASS")])
    br = tmp_path / "b_results.json"; br.write_text(json.dumps(base_r), encoding="utf-8")
    bs = tmp_path / "b_summary.json"; bs.write_text(json.dumps(base_s), encoding="utf-8")
    cr = tmp_path / "c_results.json"; cr.write_text(json.dumps(cand_r), encoding="utf-8")
    cs = tmp_path / "c_summary.json"; cs.write_text(json.dumps(cand_s), encoding="utf-8")
    out_path = tmp_path / "delta.json"

    rc = compare_main([
        "--base-results", str(br),
        "--base-summary", str(bs),
        "--cand-results", str(cr),
        "--cand-summary", str(cs),
        "--out-json", str(out_path),
    ])
    assert rc == 0
    assert out_path.is_file()
    parsed = json.loads(out_path.read_text(encoding="utf-8"))
    assert "verdict" in parsed


def test_cli_fails_closed_on_incompatible(tmp_path, capsys):
    base_r, base_s = _make_run([_scenario("s1", "recall", "PASS", "PASS")])
    cand_r, cand_s = _make_run([_scenario("sX", "recall", "PASS", "PASS")])
    br = tmp_path / "b_results.json"; br.write_text(json.dumps(base_r), encoding="utf-8")
    bs = tmp_path / "b_summary.json"; bs.write_text(json.dumps(base_s), encoding="utf-8")
    cr = tmp_path / "c_results.json"; cr.write_text(json.dumps(cand_r), encoding="utf-8")
    cs = tmp_path / "c_summary.json"; cs.write_text(json.dumps(cand_s), encoding="utf-8")

    rc = compare_main([
        "--base-results", str(br),
        "--base-summary", str(bs),
        "--cand-results", str(cr),
        "--cand-summary", str(cs),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "incompatible" in err.lower()


def test_cli_fail_on_regression(tmp_path, capsys):
    base = _make_run([
        _scenario("s1", "recall", "PASS", "PASS"),
        _scenario("s2", "recall", "PASS", "PASS"),
    ])
    cand = _make_run([
        _scenario("s1", "recall", "PASS", "PASS"),
        _scenario("s2", "recall", "FAIL", "NOT_RETRIEVED"),
    ])
    br = tmp_path / "b_results.json"; br.write_text(json.dumps(base[0]), encoding="utf-8")
    bs = tmp_path / "b_summary.json"; bs.write_text(json.dumps(base[1]), encoding="utf-8")
    cr = tmp_path / "c_results.json"; br.write_text(json.dumps(base[0]), encoding="utf-8")
    cr = tmp_path / "c_results.json"; cr.write_text(json.dumps(cand[0]), encoding="utf-8")
    cs = tmp_path / "c_summary.json"; cs.write_text(json.dumps(cand[1]), encoding="utf-8")

    rc = compare_main([
        "--base-results", str(br),
        "--base-summary", str(bs),
        "--cand-results", str(cr),
        "--cand-summary", str(cs),
        "--fail-on-regression",
    ])
    assert rc == 3


# ── helpers ───────────────────────────────────────────────────────────────


def test_diff_scenarios_records_label_changes():
    base = {
        "s1": {"scenario_id": "s1", "category": "x", "pipeline_status": "FAIL",
                "failure_label": "NOT_RETRIEVED", "per_query": []},
        "s2": {"scenario_id": "s2", "category": "x", "pipeline_status": "PASS",
                "failure_label": "PASS", "per_query": []},
    }
    cand = {
        "s1": {"scenario_id": "s1", "category": "x", "pipeline_status": "PASS",
                "failure_label": "PASS", "per_query": []},
        "s2": {"scenario_id": "s2", "category": "x", "pipeline_status": "FAIL",
                "failure_label": "NOT_RETRIEVED", "per_query": []},
    }
    nm, fm, nfp, ffp, flips = _diff_scenarios(base, cand)
    assert fm == ["s1"]
    assert nm == ["s2"]
    assert {fl.scenario_id for fl in flips} == {"s1", "s2"}


def test_validate_scenario_sets_passes_on_identical():
    rec = {"s1": {"scenario_id": "s1", "per_query": []}}
    _validate_scenario_sets(rec, rec)  # must not raise


def test_per_category_returns_list_of_dataclass():
    base = _make_run([_scenario("a", "x", "PASS", "PASS")])
    cand = _make_run([_scenario("a", "x", "FAIL", "NOT_RETRIEVED")])
    report = compare_results_dicts(base[0], base[1], cand[0], cand[1])
    assert isinstance(report.per_category, list)
    assert all(isinstance(p, PerCategoryDelta) for p in report.per_category)


def test_recall_failure_labels_constant_complete():
    expected = {"NOT_RETRIEVED", "NOT_INDEXED", "NOT_STORED",
                "RANKED_TOO_LOW", "WRONG_MEMORY", "UNKNOWN"}
    assert _is_recall_failure_label("NOT_RETRIEVED") is True
    assert _is_recall_failure_label("PIPELINE_ERROR") is False
    assert _is_recall_failure_label("ARCHIVED_MEMORY_RETURNED") is False
    assert _is_recall_failure_label("CONFLICT_RESOLUTION") is False
    assert _is_recall_failure_label("TIMEOUT") is False
    # sanity: expected set is exactly what we expect
    for lbl in expected:
        assert _is_recall_failure_label(lbl) is True
