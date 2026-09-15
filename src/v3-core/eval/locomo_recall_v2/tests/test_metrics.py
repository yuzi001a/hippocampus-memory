# -*- coding: utf-8 -*-
"""G6C-A LoCoMo Recall V2 metrics tests.

Coverage:

  * ``_hit_at_k`` / ``_first_relevant_rank`` invariants
  * Per-case scoring over mapped-only gold source IDs
  * Per-category aggregation
  * Top-level metrics: Hit@k, MRR, mean relevant rank
  * Coverage audit (questions_total, traces, candidates,
    selected, injection_outcomes, gold_evidence,
    gold_evidence_retrieved, bypasses)
  * Explicit ``status="no_mapped_evidence"`` flag when no
    mapped evidence exists
  * ``engine_invocations`` is honoured as a hint
  * Comparator: NOT implemented here — sibling ``compare.py``
    owns it.  Verify metrics.py does NOT re-export compare
    symbols.

The tests build tiny ``CaseRecord`` objects directly so they
don't depend on the adapter path.  This keeps the metrics tests
isolated from the engine seam and the production facade.
"""
from __future__ import annotations

import dataclasses
import os
import sys
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Path setup — mirror g5b pattern.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# Lazy module import.
metrics_module = None


def _ensure_module():
    global metrics_module
    if metrics_module is None:
        from eval.locomo_recall_v2 import metrics as _m
        metrics_module = _m


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _case(
    *,
    case_id: str,
    sample_id: str = "s1",
    query_idx: int = 0,
    category: str = "1",
    question: str = "Q?",
    answer: str = "A.",
    gold_evidence_dia_ids: tuple[str, ...] = (),
    gold_source_ids: tuple[str, ...] = (),
    unmapped_dia_ids: tuple[str, ...] = (),
    unresolved_evidence: tuple[str, ...] = (),
    ranked_source_ids: tuple[str, ...] = (),
    selected_source_ids: tuple[str, ...] = (),
    candidate_source_ids: tuple[str, ...] = (),
    injected_source_ids: tuple[str, ...] = (),
    context_block: str = "[recall]\n",
    trace_id: str = "trace-x",
    status: str = "ok",
    error: str = "",
    elapsed_ms: float = 1.0,
) -> Any:
    """Build a tiny CaseRecord-compatible object.

    We don't import the adapter here to keep this module pure;
    we just create a dataclass that mirrors the adapter's
    contract via duck typing.
    """
    @dataclasses.dataclass
    class _C:
        case_id: str
        sample_id: str
        query_idx: int
        category: str
        question: str
        answer: str
        gold_evidence_dia_ids: tuple[str, ...]
        gold_source_ids: tuple[str, ...]
        unmapped_dia_ids: tuple[str, ...]
        unresolved_evidence: tuple[str, ...]
        context_block_length: int
        trace_id: str
        ranked_source_ids: tuple[str, ...]
        selected_source_ids: tuple[str, ...]
        candidate_source_ids: tuple[str, ...]
        injected_source_ids: tuple[str, ...]
        lane_summaries: tuple
        injection_summary: Any
        drop_summary: dict
        elapsed_ms: float
        status: str
        error: str
    return _C(
        case_id=case_id,
        sample_id=sample_id,
        query_idx=query_idx,
        category=category,
        question=question,
        answer=answer,
        gold_evidence_dia_ids=gold_evidence_dia_ids,
        gold_source_ids=gold_source_ids,
        unmapped_dia_ids=unmapped_dia_ids,
        unresolved_evidence=unresolved_evidence,
        context_block_length=len(context_block),
        trace_id=trace_id,
        ranked_source_ids=ranked_source_ids,
        selected_source_ids=selected_source_ids,
        candidate_source_ids=candidate_source_ids,
        injected_source_ids=injected_source_ids,
        lane_summaries=(),
        injection_summary=None,
        drop_summary={},
        elapsed_ms=elapsed_ms,
        status=status,
        error=error,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hit_at_k_basic():
    _ensure_module()
    ranked = ["a", "b", "c"]
    gold = {"b"}
    assert metrics_module._hit_at_k(ranked, gold, 1) == 0.0
    assert metrics_module._hit_at_k(ranked, gold, 2) == 1.0
    assert metrics_module._hit_at_k(ranked, gold, 5) == 1.0


def test_first_relevant_rank_basic():
    _ensure_module()
    assert metrics_module._first_relevant_rank(["a", "b", "c"], {"b"}) == 2
    assert metrics_module._first_relevant_rank(["a", "b", "c"], {"z"}) is None


def test_no_mapped_evidence_status():
    _ensure_module()
    cases = [_case(case_id="s1|0", gold_source_ids=())]
    snap = metrics_module.compute_metrics(cases)
    assert snap.status == "no_mapped_evidence"
    assert snap.mapped_gold_count == 0
    assert snap.unmapped_gold_count == 0
    # Hit/MRR stay at 0.0; coverage audit still reports the truth.
    assert snap.hit_at_1 == 0.0
    assert snap.hit_at_5 == 0.0
    assert snap.coverage.questions_total == 1


def test_mapped_only_metrics():
    _ensure_module()
    cases = [
        _case(case_id="s1|0",
              gold_source_ids=("locomo|eval_v2|s1|session_1|d1>d2",),
              ranked_source_ids=("locomo|eval_v2|s1|session_1|d1>d2", "other"),
              selected_source_ids=("locomo|eval_v2|s1|session_1|d1>d2",),
              candidate_source_ids=("locomo|eval_v2|s1|session_1|d1>d2", "other"),
              injected_source_ids=("locomo|eval_v2|s1|session_1|d1>d2",),
              trace_id="t1"),
        _case(case_id="s1|1",
              gold_source_ids=("locomo|eval_v2|s1|session_1|d3>d4",),
              ranked_source_ids=("unrelated", "locomo|eval_v2|s1|session_1|d3>d4"),
              candidate_source_ids=("unrelated", "locomo|eval_v2|s1|session_1|d3>d4"),
              trace_id="t2"),
        _case(case_id="s1|2",
              gold_source_ids=("locomo|eval_v2|s1|session_1|d5>d6",),
              ranked_source_ids=("unrelated1", "unrelated2"),
              candidate_source_ids=("unrelated1", "unrelated2"),
              trace_id="t3"),
    ]
    snap = metrics_module.compute_metrics(cases, hit_at_k_limit=5)
    assert snap.status == "ok"
    assert snap.mapped_gold_count == 3
    assert snap.questions_with_mapped_evidence == 3
    # Hit@1 = 1/3 (only case 0 hits at rank 1), Hit@5 = 2/3
    # (cases 0 and 1 hit within top-5), MRR = (1 + 0.5 + 0)/3 = 0.5
    assert snap.hit_at_1 == pytest.approx(1 / 3)
    assert snap.hit_at_5 == pytest.approx(2 / 3)
    assert snap.mrr == pytest.approx(0.5)
    # Mean relevant rank over the two cases that hit:
    # (1 + 2) / 2 = 1.5
    assert snap.mean_relevant_rank == pytest.approx(1.5)
    # Coverage audit counts.
    cov = snap.coverage
    assert cov.questions_total == 3
    assert cov.traces == 3
    # ``candidates`` is sourced from ``candidate_source_ids`` (the
    # full snapshot list), not from the ranked tail — so we expect
    # 2 + 2 + 2 = 6 across the three cases.
    assert cov.candidates == 6
    assert cov.selected == 1
    assert cov.injection_outcomes == 1
    assert cov.gold_evidence == 3
    assert cov.gold_evidence_retrieved == 2  # cases 0 and 1 retrieved their gold


def test_per_category_breakdown():
    _ensure_module()
    cases = [
        _case(case_id="s|0", category="1", gold_source_ids=("g1",),
              ranked_source_ids=("g1",)),
        _case(case_id="s|1", category="2", gold_source_ids=("g2",),
              ranked_source_ids=("g2",)),
        _case(case_id="s|2", category="", gold_source_ids=("g3",),
              ranked_source_ids=("nope",)),
    ]
    snap = metrics_module.compute_metrics(cases, hit_at_k_limit=5)
    cats = dict(snap.per_category)
    assert set(cats.keys()) == {"1", "2", "__unlabelled__"}
    assert cats["1"]["hit_at_1"] == pytest.approx(1.0)
    assert cats["2"]["hit_at_1"] == pytest.approx(1.0)
    assert cats["__unlabelled__"]["hit_at_1"] == pytest.approx(0.0)


def test_coverage_audit_bypass_count():
    _ensure_module()
    cases = [
        _case(case_id="ok|0", trace_id="t1", status="ok",
              gold_source_ids=("g1",), ranked_source_ids=("g1",)),
        # Empty trace_id + non-ok status → counted as bypass.
        _case(case_id="bad|0", trace_id="", status="engine_error"),
    ]
    audit = metrics_module.compute_coverage_audit(cases, engine_invocations=1)
    assert audit.questions_total == 2
    assert audit.traces == 1  # only the ok case contributes a trace_id
    assert audit.bypasses == 1


def test_coverage_audit_engine_invocations_honour():
    _ensure_module()
    cases = [_case(case_id="s|0", trace_id="t", gold_source_ids=("g",))]
    a = metrics_module.compute_coverage_audit(cases, engine_invocations=3)
    assert a.engine_invocations == 3
    b = metrics_module.compute_coverage_audit(cases, engine_invocations=-1)
    assert b.engine_invocations == -1


def test_summarise_gold_evidence():
    _ensure_module()
    cases = [
        _case(case_id="a", gold_source_ids=("g1", "g2"), unresolved_evidence=("u1",)),
        _case(case_id="b", gold_source_ids=(), unresolved_evidence=("u2", "u3")),
    ]
    mapped, unmapped = metrics_module.summarise_gold_evidence(cases)
    assert mapped == 2
    assert unmapped == 3


def test_hit_at_k_limit_honoured():
    _ensure_module()
    cases = [
        _case(case_id="s|0", gold_source_ids=("g",),
              ranked_source_ids=("g", "a", "b", "c", "d", "e")),
    ]
    snap = metrics_module.compute_metrics(cases, hit_at_k_limit=2)
    # Hit@2 = 1.0 (g is at rank 1)
    assert snap.hit_at_k == 1.0
    # Hit@5 in the headline slot uses the limit we passed.
    # We didn't compute Hit@5 explicitly here; we compute it via
    # the explicit hit_at_5 field which is hardcoded to k=5.
    assert snap.hit_at_5 == 1.0


def test_metrics_does_not_re_export_compare():
    """Comparator lives in sibling compare.py; metrics.py must not
    re-export it.
    """
    _ensure_module()
    src_path = metrics_module.__file__
    assert src_path is not None
    src = open(src_path, "r", encoding="utf-8").read()
    # Forbidden imports / re-exports of the sibling compare.
    forbidden = (
        "from .compare import",
        "from eval.locomo_recall_v2.compare import",
        "from . import compare",
    )
    for pat in forbidden:
        assert pat not in src, (
            f"metrics.py must not import compare: {pat!r}"
        )


def test_metric_snapshot_to_dict_round_trip():
    _ensure_module()
    cases = [_case(case_id="s|0", gold_source_ids=("g",),
                   ranked_source_ids=("g",))]
    snap = metrics_module.compute_metrics(cases)
    blob = snap.to_dict()
    assert "status" in blob
    assert "hit_at_1" in blob
    assert "mrr" in blob
    assert "per_category" in blob
    assert "coverage" in blob
    # Numeric fields are plain floats.
    assert isinstance(blob["hit_at_1"], float)
    # Coverage is a plain dict.
    assert isinstance(blob["coverage"], dict)
    assert "questions_total" in blob["coverage"]
    assert "bypasses" in blob["coverage"]


def test_unmapped_count_separate_from_mapped():
    """Unmapped gold dia IDs contribute to unmapped_gold_count,
    NOT to mapped_gold_count.  Cases with mapped-only evidence
    get hit scoring; cases with unmapped-only evidence skip hit
    scoring.  Unresolved evidence (compound / malformed strings)
    is reported separately under unresolved_gold_count.
    """
    _ensure_module()
    cases = [
        # Case A: unmapped-only evidence — no mapped, but two
        # unmapped dia IDs that survived resolve_gold_evidence
        # without finding a source_id.
        _case(case_id="u|0",
              gold_source_ids=(),
              unmapped_dia_ids=("u1", "u2"),
              unresolved_evidence=(),
              ranked_source_ids=("a", "b")),
        # Case B: unresolved-only — the legacy "compound / malformed
        # string" category.  Never contributes to hit scoring.
        _case(case_id="r|0",
              gold_source_ids=(),
              unmapped_dia_ids=(),
              unresolved_evidence=("bad,entry", "also bad"),
              ranked_source_ids=()),
    ]
    snap = metrics_module.compute_metrics(cases)
    # Headline denominator (mapped) is zero → no_mapped_evidence.
    assert snap.mapped_gold_count == 0
    assert snap.unmapped_gold_count == 2
    assert snap.unresolved_gold_count == 2
    assert snap.questions_with_mapped_evidence == 0
    assert snap.questions_with_unmapped_evidence == 1
    assert snap.questions_with_unresolved_evidence == 1
    assert snap.status == "no_mapped_evidence"
    # Hit/MRR stay zero — there is no mapped gold to score against.
    assert snap.hit_at_1 == 0.0
    assert snap.mrr == 0.0


def test_injection_outcomes_counted_in_audit():
    """injection_outcomes reflects injected_source_ids length summed."""
    _ensure_module()
    cases = [
        _case(case_id="a", gold_source_ids=("g",),
              ranked_source_ids=("g",),
              selected_source_ids=("g",),
              candidate_source_ids=("g",),
              injected_source_ids=("g",)),
        _case(case_id="b", gold_source_ids=("g2",),
              ranked_source_ids=("g2",),
              selected_source_ids=("g2",),
              candidate_source_ids=("g2",),
              injected_source_ids=("g2",)),
    ]
    audit = metrics_module.compute_coverage_audit(cases, engine_invocations=2)
    assert audit.injection_outcomes == 2
    assert audit.selected == 2
    # candidates is sourced from candidate_source_ids.
    assert audit.candidates == 2


# ---------------------------------------------------------------------------
# Metric K truth — explicit tests for the new contract
# ---------------------------------------------------------------------------


def test_default_shape_reports_hit_at_5_equals_hit_at_k():
    """The default production shape (``ranking_limit=5`` and
    ``hit_at_k_limit=5``) reports Hit@5 as the ``hit_at_k``
    slot.  This is the deployment-default invariant.
    """
    _ensure_module()
    cases = [
        _case(case_id="s|0",
              gold_source_ids=("g",),
              ranked_source_ids=("g", "a", "b", "c", "d")),
    ]
    snap = metrics_module.compute_metrics(cases)
    assert snap.ranking_limit == 5
    assert snap.hit_at_k_k == 5
    assert snap.hit_at_k_status == "ok"
    assert snap.hit_at_k == snap.hit_at_5
    assert snap.hit_at_1 == 1.0
    assert snap.hit_at_5 == 1.0


def test_hit_at_k_k_above_ranking_limit_is_na_not_fabricated():
    """When the caller asks for K > ranking_limit, the
    ``hit_at_k`` slot is honestly N/A (None in JSON, status
    "n_a") — never a fabricated fraction over a shorter
    surface.
    """
    _ensure_module()
    cases = [
        _case(case_id="s|0",
              gold_source_ids=("g",),
              ranked_source_ids=("g", "a", "b", "c", "d")),
    ]
    snap = metrics_module.compute_metrics(
        cases, hit_at_k_limit=30, ranking_limit=5,
    )
    assert snap.ranking_limit == 5
    assert snap.hit_at_k_k == 30
    assert snap.hit_at_k_status == "n_a"
    # to_dict serialises unavailable metric as None, never NaN.
    blob = snap.to_dict()
    assert blob["hit_at_k"] is None
    assert blob["hit_at_k_status"] == "n_a"
    assert blob["hit_at_k_k"] == 30
    assert blob["ranking_limit"] == 5
    # Hit@1 / Hit@5 stay "ok" because the deployment did surface
    # up to 5 ranked candidates.
    assert blob["hit_at_1"] == 1.0
    assert blob["hit_at_1_status"] == "ok"
    assert blob["hit_at_5"] == 1.0
    assert blob["hit_at_5_status"] == "ok"


def test_hit_at_5_is_na_when_ranking_limit_is_below_5():
    """When the observable ranking depth is shallower than 5,
    the Hit@5 slot is N/A — it cannot compute "the first 5 of a
    shorter surface" because the surface is shorter.
    """
    _ensure_module()
    cases = [
        _case(case_id="s|0",
              gold_source_ids=("g",),
              ranked_source_ids=("g", "a", "b")),  # depth 3
    ]
    snap = metrics_module.compute_metrics(
        cases, hit_at_k_limit=3, ranking_limit=3,
    )
    assert snap.ranking_limit == 3
    # Hit@1 still works (K=1 <= depth 3).
    assert snap.hit_at_1_status == "ok"
    assert snap.hit_at_1 == 1.0
    # Hit@5 is N/A — the deployment did not surface 5 docs.
    assert snap.hit_at_5_status == "n_a"
    blob = snap.to_dict()
    assert blob["hit_at_5"] is None
    assert blob["hit_at_5_status"] == "n_a"
    # Hit@K (configured K=3) is OK.
    assert blob["hit_at_k"] == 1.0
    assert blob["hit_at_k_status"] == "ok"
    assert blob["hit_at_k_k"] == 3


def test_hit_at_1_is_na_when_ranking_limit_is_zero():
    """Edge case: a depth of zero means even Hit@1 is N/A."""
    _ensure_module()
    cases = [
        _case(case_id="s|0",
              gold_source_ids=("g",),
              ranked_source_ids=()),
    ]
    snap = metrics_module.compute_metrics(
        cases, hit_at_k_limit=5, ranking_limit=0,
    )
    assert snap.ranking_limit == 0
    assert snap.hit_at_1_status == "n_a"
    assert snap.hit_at_5_status == "n_a"
    assert snap.hit_at_k_status == "n_a"
    blob = snap.to_dict()
    assert blob["hit_at_1"] is None
    assert blob["hit_at_5"] is None
    assert blob["hit_at_k"] is None


def test_to_dict_round_trip_avoids_nan_for_na_slots():
    """JSON has no standard NaN literal; N/A slots surface as
    ``None`` so a downstream JSON consumer cannot trip on the
    non-standard literal.
    """
    _ensure_module()
    import json
    cases = [
        _case(case_id="s|0",
              gold_source_ids=("g",),
              ranked_source_ids=("g", "a")),
    ]
    snap = metrics_module.compute_metrics(
        cases, hit_at_k_limit=30, ranking_limit=5,
    )
    blob = snap.to_dict()
    # Strict JSON: NaN is not allowed; the standard json module
    # would raise.  allow_nan=False catches any leak.
    encoded = json.dumps(blob, allow_nan=False, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["hit_at_k"] is None
    assert decoded["hit_at_k_status"] == "n_a"


def test_mrr_observed_over_actual_ranking_depth():
    """MRR is always at the observed ranking depth — when
    K > ranking_limit, MRR is computed at ranking_limit (so a
    baseline remains comparable), and ``mrr_k`` reflects the
    depth used.
    """
    _ensure_module()
    cases = [
        _case(case_id="s|0",
              gold_source_ids=("g",),
              # g is at rank 3 within a 5-deep surface.
              ranked_source_ids=("a", "b", "g", "c", "d")),
    ]
    snap = metrics_module.compute_metrics(
        cases, hit_at_k_limit=30, ranking_limit=5,
    )
    # MRR is 1/3 — the observed rank — even though hit_at_k_k=30.
    assert snap.mrr == pytest.approx(1.0 / 3.0)
    assert snap.mrr_k == 5
    # mean_relevant_rank also reflects observed rank.
    assert snap.mean_relevant_rank == pytest.approx(3.0)


def test_three_gold_evidence_states_reported_separately():
    """``summarise_gold_evidence_extended`` returns three
    distinct counts so callers can audit evidence mapping
    without rerunning the dataset loader.
    """
    _ensure_module()
    cases = [
        # All three states represented in one row.
        _case(case_id="s|0",
              gold_source_ids=("g1", "g2"),        # 2 mapped
              unmapped_dia_ids=("u1",),            # 1 unmapped
              unresolved_evidence=("bad,entry",)),  # 1 unresolved
        _case(case_id="s|1",
              gold_source_ids=("g3",),
              unmapped_dia_ids=(),
              unresolved_evidence=()),
    ]
    mapped, unmapped, unresolved = (
        metrics_module.summarise_gold_evidence_extended(cases)
    )
    assert mapped == 3
    assert unmapped == 1
    assert unresolved == 1


def test_metric_snapshot_records_ranking_limit_and_hit_at_k_k():
    """``ranking_limit`` and ``hit_at_k_k`` MUST be recorded on
    the snapshot so an audit can verify the metric K truth
    contract after the fact.
    """
    _ensure_module()
    cases = [
        _case(case_id="s|0",
              gold_source_ids=("g",),
              ranked_source_ids=("g", "a", "b", "c", "d")),
    ]
    snap = metrics_module.compute_metrics(
        cases, hit_at_k_limit=10, ranking_limit=5,
    )
    blob = snap.to_dict()
    assert blob["ranking_limit"] == 5
    assert blob["hit_at_k_k"] == 10
    assert blob["hit_at_k_status"] == "n_a"
    assert blob["hit_at_k"] is None
    # Hit@1 / Hit@5 are still real (depth 5 surfaces them).
    assert blob["hit_at_1_status"] == "ok"
    assert blob["hit_at_5_status"] == "ok"
    assert blob["hit_at_1"] == 1.0
    assert blob["hit_at_5"] == 1.0


def test_to_dict_full_json_safe_for_all_na_slots():
    """Comprehensive JSON-safety check: when ALL hit slots are
    N/A, the entire ``to_dict()`` payload must be
    ``json.dumps(allow_nan=False)`` clean — no NaN literal can
    leak through ``hit_at_1`` / ``hit_at_5`` / ``hit_at_k`` /
    per_category / mrr / mean_relevant_rank / coverage.  This
    pins the contract that every N/A hit slot serializes JSON-
    safe ``None`` with a sibling ``status`` field.
    """
    _ensure_module()
    import json
    # depth 0 → every K slot is N/A, but cases still surface
    # ranked_source_ids (so mrr/mean_relevant_rank are well-
    # defined finite numbers, never NaN).
    cases = [
        _case(case_id="s|0",
              gold_source_ids=("g",),
              ranked_source_ids=()),
    ]
    snap = metrics_module.compute_metrics(
        cases, hit_at_k_limit=5, ranking_limit=0,
    )
    blob = snap.to_dict()
    # Strict JSON: NaN is not allowed; allow_nan=False catches leaks.
    encoded = json.dumps(blob, allow_nan=False, sort_keys=True)
    decoded = json.loads(encoded)
    # Every hit slot is N/A → None in JSON.
    assert decoded["hit_at_1"] is None
    assert decoded["hit_at_5"] is None
    assert decoded["hit_at_k"] is None
    assert decoded["hit_at_1_status"] == "n_a"
    assert decoded["hit_at_5_status"] == "n_a"
    assert decoded["hit_at_k_status"] == "n_a"
    # mrr / mean_relevant_rank are finite numbers (no NaN leak).
    assert isinstance(decoded['mrr'], float)
    assert decoded['mrr'] == 0.0
    assert isinstance(decoded['mean_relevant_rank'], float)
    assert decoded['mean_relevant_rank'] == 0.0
    # Per-category payload also JSON-safe — every hit slot is None.
    # ``to_dict()`` returns per_category as a list of dicts (one
    # per category, 'category' key inside the dict), so iterate
    # accordingly.
    for vals in decoded['per_category']:
        cat = vals.get("category")
        for slot in ("hit_at_1", "hit_at_5", "hit_at_k"):
            assert vals[slot] is None, f"per_category[{cat!r}].{slot} leaked non-None"
            assert vals[f"{slot}_status"] == "n_a"


def test_mrr_and_mean_relevant_rank_clamped_to_observed_k():
    """Per-case MRR and the headline ``mean_relevant_rank`` must
    never report a value derived from a rank beyond the observed
    ranking depth.  When the relevant doc is at rank 7 but the
    facade only surfaced 5 docs, the per-case MRR is 0.0 and
    ``mean_relevant_rank`` is reported over OBSERVED ranks only
    (the doc never scored → not in the mean).
    """
    _ensure_module()
    cases = [
        _case(case_id="s|0",
              gold_source_ids=("g",),
              # g is at rank 7 — beyond observable depth 5.
              ranked_source_ids=("a", "b", "c", "d", "e", "f", "g"),
              candidate_source_ids=("a", "b", "c", "d", "e", "f", "g")),
    ]
    snap = metrics_module.compute_metrics(
        cases, hit_at_k_limit=5, ranking_limit=5,
    )
    # Per-case: relevant doc is beyond observed depth (rank 7 > 5)
    # → MRR is 0.0, NOT 1/7.
    assert snap.mrr == 0.0
    assert snap.mrr_k == 5
    # mean_relevant_rank is 0.0 when no case has a relevant rank
    # within the observable depth.
    assert snap.mean_relevant_rank == 0.0
    # Hit@1 / Hit@5 stay at 0.0 (no hit in the top 5).
    assert snap.hit_at_1 == 0.0
    assert snap.hit_at_5 == 0.0


def test_mean_relevant_rank_only_over_observed_ranks():
    """``mean_relevant_rank`` only averages ranks that fall
    WITHIN the observable ranking depth.  Mixed input: case 0
    hits at rank 1 (within depth 5), case 1 hits at rank 8
    (beyond depth 5 → ignored from the mean).
    """
    _ensure_module()
    cases = [
        _case(case_id="s|0",
              gold_source_ids=("g1",),
              ranked_source_ids=("g1", "a", "b", "c", "d")),
        _case(case_id="s|1",
              gold_source_ids=("g2",),
              ranked_source_ids=("a", "b", "c", "d", "e", "f", "g", "g2")),
    ]
    snap = metrics_module.compute_metrics(
        cases, hit_at_k_limit=5, ranking_limit=5,
    )
    # Case 0 contributes rank 1; case 1's rank 8 is beyond
    # observable depth and excluded from the mean.  Mean is
    # over only observed ranks → 1.0 (single value), not
    # (1+8)/2 = 4.5.
    assert snap.mean_relevant_rank == pytest.approx(1.0)
    # Per-case MRR: case 0 → 1.0, case 1 → 0.0 (rank 8 > 5).
    # Mean = 0.5.
    assert snap.mrr == pytest.approx(0.5)
