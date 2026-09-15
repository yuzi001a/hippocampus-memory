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
    """Unmapped gold dia IDs contribute to unmapped_count, NOT to
    mapped_gold_count.  Cases with mapped-only evidence get hit
    scoring; cases with unmapped-only evidence skip hit scoring.
    """
    _ensure_module()
    cases = [
        _case(case_id="u|0",
              gold_source_ids=(),  # no mapped evidence
              unresolved_evidence=("u1", "u2"),
              ranked_source_ids=("a", "b")),
    ]
    snap = metrics_module.compute_metrics(cases)
    assert snap.mapped_gold_count == 0
    assert snap.unmapped_gold_count == 2
    assert snap.questions_with_mapped_evidence == 0
    assert snap.questions_with_unmapped_evidence == 1
    assert snap.status == "no_mapped_evidence"
    # Hit/MRR stay zero — the only gold dia IDs are unmapped, so
    # there is nothing to score against.
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