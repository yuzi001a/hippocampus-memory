"""Focused tests for the G5b Track-E corpus expansion.

These tests verify the EXPANDED v2 corpus (40 v1 ∪ 46 expansion = 86
total) and the 25-scenario GOLD REGRESSION subset. They do NOT
modify the existing 46-test surface in ``test_g5b_evaluator.py``;
they only ADD assertions about the new corpus structure.

The test guarantees are:

  1. The v2 corpus contains 80-100 scenarios (Track-E band).
  2. The v2 corpus covers all 12 allowed categories.
  3. The v2 corpus is a superset of v1 (no scenario IDs lost).
  4. Every expansion scenario has a valid scenario_id, category,
     and at least one query.
  5. The GOLD REGRESSION subset contains 20-30 scenarios (band).
  6. The GOLD REGRESSION subset is an exact subset of the v2 corpus.
  7. Every gold scenario is unique (no duplicate IDs).
  8. The GOLD REGRESSION subset covers all 12 allowed categories.
  9. The GOLD REGRESSION subset loads cleanly through the schema.
  10. The GOLD REGRESSION subset runs end-to-end through Lane A
      without pipeline errors.
  11. The v2 corpus runs end-to-end through Lane A with zero
      pipeline errors (algorithm/runner wiring still works).
  12. The v2 corpus category histogram is per-category ≥ 1.
"""
from __future__ import annotations

import os
import sys

import pytest

# Make ``src/v3-core/src`` importable so ``v3core`` resolves.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
_SRC_ROOT = os.path.join(_PKG_ROOT, "src")
_EVAL_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in (_SRC_ROOT, _EVAL_ROOT, _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


from eval.g5b_real_memory_evaluator.gold_scenarios import (  # noqa: E402
    GOLD_IDS,
    GOLD_VERSION,
    all_gold_scenarios,
    gold_by_category,
    gold_count,
)
from eval.g5b_real_memory_evaluator.runner import run_scenarios  # noqa: E402
from eval.g5b_real_memory_evaluator.scenario_schema import (  # noqa: E402
    ALLOWED_CATEGORIES,
    count_by_category,
    scenario_from_dict,
)
from eval.g5b_real_memory_evaluator.scenarios import (  # noqa: E402
    scenarios_v1,
    scenarios_v2,
)
from eval.g5b_real_memory_evaluator.metrics import compute_metrics  # noqa: E402


# ── v2 corpus size + coverage ────────────────────────────────────────────


def test_v2_corpus_size_in_target_band():
    """v2 corpus must have 80-100 scenarios (Track-E target band)."""
    total = len(scenarios_v2.all_scenarios())
    assert 80 <= total <= 100, (
        f"v2 corpus has {total} scenarios; expected 80-100. "
        "Expand or contract the expansion set without changing v1."
    )


def test_v2_corpus_is_superset_of_v1():
    """v2 must contain every v1 scenario_id; nothing lost."""
    v1_ids = {s["scenario_id"] for s in scenarios_v1.all_scenarios()}
    v2_ids = {s["scenario_id"] for s in scenarios_v2.all_scenarios()}
    missing = v1_ids - v2_ids
    assert not missing, f"v2 lost v1 scenarios: {sorted(missing)}"


def test_v2_corpus_covers_all_allowed_categories():
    """v2 must cover every category in the schema's allowed set."""
    cats = {s["category"] for s in scenarios_v2.all_scenarios()}
    assert cats == set(ALLOWED_CATEGORIES), (
        f"v2 missing categories: {set(ALLOWED_CATEGORIES) - cats}; "
        f"v2 has extra: {cats - set(ALLOWED_CATEGORIES)}"
    )


def test_v2_corpus_per_category_count_at_least_one():
    """Each of the 12 categories must have ≥1 scenario in v2."""
    scenarios = [scenario_from_dict(s) for s in scenarios_v2.all_scenarios()]
    counts = count_by_category(scenarios)
    assert all(counts[c] >= 1 for c in ALLOWED_CATEGORIES), counts


def test_v2_corpus_scenario_ids_unique():
    """v2 scenario IDs must be unique (no accidental duplicates)."""
    ids = [s["scenario_id"] for s in scenarios_v2.all_scenarios()]
    assert len(ids) == len(set(ids)), (
        f"duplicate scenario_ids in v2: "
        f"{sorted([i for i in ids if ids.count(i) > 1])}"
    )


def test_v2_corpus_each_scenario_parses():
    """Every v2 scenario must parse through the schema cleanly."""
    for raw in scenarios_v2.all_scenarios():
        sc = scenario_from_dict(raw)
        assert sc.scenario_id, f"empty scenario_id: {raw}"
        assert sc.category in ALLOWED_CATEGORIES, (
            f"{sc.scenario_id} category {sc.category!r} not in allowed"
        )
        assert sc.queries, f"{sc.scenario_id} has no queries"


def test_v2_expansion_count_matches_v2_minus_v1():
    """expansion_only() must equal v2 ∖ v1 in scenario_id terms."""
    v1_ids = {s["scenario_id"] for s in scenarios_v1.all_scenarios()}
    expansion_ids = {s["scenario_id"] for s in scenarios_v2.expansion_only()}
    assert expansion_ids.isdisjoint(v1_ids), (
        f"expansion overlaps v1: {expansion_ids & v1_ids}"
    )
    v2_ids = {s["scenario_id"] for s in scenarios_v2.all_scenarios()}
    assert expansion_ids == (v2_ids - v1_ids), (
        "expansion_only() does not match v2 ∖ v1"
    )


def test_v2_runs_end_to_end_with_zero_pipeline_errors():
    """The v2 corpus must run through Lane A with zero pipeline errors.

    This is the regression guard that the expansion didn't introduce
    any scenario that crashes the runner or its Lane A contract.
    """
    scenarios = [scenario_from_dict(s) for s in scenarios_v2.all_scenarios()]
    results, query_records = run_scenarios(scenarios, lane="a", limit=5)
    metrics = compute_metrics(results, query_records)
    assert metrics.scenario_count == len(scenarios)
    assert metrics.pipeline_errors == 0, (
        f"v2 has {metrics.pipeline_errors} pipeline errors; "
        "an expansion scenario broke the runner."
    )


# ── gold regression subset ───────────────────────────────────────────────


def test_gold_count_in_target_band():
    """Gold subset must have 20-30 scenarios (Track-E target band)."""
    n = gold_count()
    assert 20 <= n <= 30, f"gold has {n} scenarios; expected 20-30"


def test_gold_version_is_pinned():
    """Gold subset version must be a non-empty pinned string."""
    assert isinstance(GOLD_VERSION, str) and GOLD_VERSION, GOLD_VERSION


def test_gold_ids_unique():
    """Gold IDs must be unique (no duplicate regression anchors)."""
    assert len(GOLD_IDS) == len(set(GOLD_IDS)), (
        f"duplicate gold ids: "
        f"{sorted([i for i in GOLD_IDS if GOLD_IDS.count(i) > 1])}"
    )


def test_gold_subset_is_subset_of_v2_corpus():
    """Every gold ID must exist in the v2 corpus (no orphans)."""
    v2_ids = {s["scenario_id"] for s in scenarios_v2.all_scenarios()}
    missing = [sid for sid in GOLD_IDS if sid not in v2_ids]
    assert not missing, f"gold IDs not in v2 corpus: {missing}"


def test_gold_subset_covers_all_allowed_categories():
    """Gold must cover all 12 allowed categories (no category holes)."""
    cats = gold_by_category()
    missing = [c for c in ALLOWED_CATEGORIES if cats.get(c, 0) < 1]
    assert not missing, f"gold has no scenarios for: {missing}"


def test_gold_subset_loads_through_schema():
    """all_gold_scenarios() must return parsed Scenario objects."""
    scens = all_gold_scenarios()
    assert len(scens) == gold_count()
    for s in scens:
        assert s.scenario_id in GOLD_IDS
        assert s.category in ALLOWED_CATEGORIES
        assert s.queries, f"{s.scenario_id} parsed with no queries"


def test_gold_subset_runs_end_to_end_with_zero_pipeline_errors():
    """The gold subset must run through Lane A with zero pipeline errors."""
    scens = all_gold_scenarios()
    results, query_records = run_scenarios(scens, lane="a", limit=5)
    metrics = compute_metrics(results, query_records)
    assert metrics.scenario_count == len(scens)
    assert metrics.pipeline_errors == 0, (
        f"gold has {metrics.pipeline_errors} pipeline errors"
    )


def test_gold_subset_metrics_invariants():
    """The gold subset metrics must satisfy the standard runner invariants."""
    scens = all_gold_scenarios()
    results, query_records = run_scenarios(scens, lane="a", limit=5)
    metrics = compute_metrics(results, query_records)
    # Each scenario contributes exactly one failure label.
    assert sum(metrics.per_label_count.values()) == metrics.scenario_count
    assert 0.0 <= metrics.scenario_pass_rate <= 1.0
    assert 0.0 <= metrics.mrr <= 1.0


# ── CLI corpus selection ─────────────────────────────────────────────────


def test_run_module_default_corpus_is_v1():
    """run.py must default to the v1 corpus (backward compat)."""
    # Smoke-test the parser only — full run needs an output dir.
    import argparse
    # We don't run main(); we just exercise the module import path.
    import eval.g5b_real_memory_evaluator.run as run_mod
    assert run_mod._scenarios_from_argv(["--out-dir", "outputs/_test_default"]) is not None


def test_run_module_v2_corpus_loads_86_scenarios(tmp_path):
    """``--corpus v2`` must load 80-100 scenarios through the CLI path."""
    import eval.g5b_real_memory_evaluator.run as run_mod

    scenarios, label = run_mod._scenarios_from_argv(
        ["--corpus", "v2", "--out-dir", str(tmp_path / "out")]
    )
    assert label == "scenarios_v2"
    assert 80 <= len(scenarios) <= 100


def test_run_module_gold_corpus_loads_25_scenarios(tmp_path):
    """``--corpus gold`` must load 20-30 scenarios through the CLI path."""
    import eval.g5b_real_memory_evaluator.run as run_mod

    scenarios, label = run_mod._scenarios_from_argv(
        ["--corpus", "gold", "--out-dir", str(tmp_path / "out")]
    )
    assert label.startswith("gold_scenarios[")
    assert 20 <= len(scenarios) <= 30
    # Gold must be a subset of v2.
    gold_ids = {s.scenario_id for s in scenarios}
    v2_ids = {
        s["scenario_id"] for s in scenarios_v2.all_scenarios()
    }
    assert gold_ids.issubset(v2_ids)


def test_run_module_explicit_scenarios_takes_precedence_over_corpus(tmp_path):
    """``--scenarios`` (explicit file) must win over ``--corpus``."""
    import json

    import eval.g5b_real_memory_evaluator.run as run_mod

    # Build a tiny JSON file with one synthetic scenario.
    payload = [
        {
            "scenario_id": "CLI-OVR-001",
            "category": "STABLE_FACT",
            "fixtures": [
                {
                    "category": "fact",
                    "title": "override probe",
                    "content": "synthetic single-scenario override",
                    "tags": ["test"],
                }
            ],
            "queries": [{"text": "override", "lane": "keyword"}],
        }
    ]
    json_path = tmp_path / "override.json"
    json_path.write_text(json.dumps(payload), encoding="utf-8")

    scenarios, label = run_mod._scenarios_from_argv(
        [
            "--scenarios",
            str(json_path),
            "--corpus",
            "v2",  # should be ignored
            "--out-dir",
            str(tmp_path / "out"),
        ]
    )
    assert label.startswith("argv:")
    assert len(scenarios) == 1
    assert scenarios[0].scenario_id == "CLI-OVR-001"
