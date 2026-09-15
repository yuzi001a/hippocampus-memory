# -*- coding: utf-8 -*-
"""G6C-A LoCoMo Recall V2 runner tests.

Coverage:

  * :func:`runner.run_case` is a thin passthrough to the adapter.
  * :func:`runner.run_cases` preserves ``case_id`` stability
    (sample_id + query_idx, never row order).
  * :class:`runner.RunnerConfig` raises on a non-full mode.
  * :func:`runner.write_results_jsonl` emits valid JSONL.
  * :func:`runner.run_cases_with_metrics` returns records + metrics.
  * The runner does NOT switch to keyword-only or synthetic
    vectors in ``mode="full"``.
  * The runner does NOT commit, push, or open PG.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest


# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


runner_module = None
adapter_module = None


def _ensure_modules():
    global runner_module, adapter_module
    if runner_module is None:
        from eval.locomo_recall_v2 import runner as _r
        runner_module = _r
    if adapter_module is None:
        from eval.locomo_recall_v2 import adapter as _a
        adapter_module = _a


# ---------------------------------------------------------------------------
# Shared fake facade (reuses the pattern from test_adapter)
# ---------------------------------------------------------------------------


def _patch_facade_for_noop_hits(monkeypatch):
    _ensure_modules()

    def _fake_facade(query, **kwargs):
        return f"[recall for: {query}]\n"

    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", _fake_facade)


def _make_case(sample_id, query_idx, q="Q?", a="A."):
    return {
        "sample_id": sample_id,
        "query_idx": query_idx,
        "question": q,
        "answer": a,
        "gold_evidence_dia_ids": (),
        "gold_source_ids": (),
        "category": "x",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_case_is_passthrough_to_adapter(monkeypatch):
    _ensure_modules()
    _patch_facade_for_noop_hits(monkeypatch)
    rec = runner_module.run_case(
        sample_id="s1", query_idx=0,
        question="Q?", gold_answer="A.",
        gold_evidence_dia_ids=(), gold_source_ids=(),
    )
    assert rec.case_id == "s1|0"
    assert rec.question == "Q?"
    assert rec.answer == "A."
    assert rec.status == "trace_missing"
    assert rec.error == "RecallTrace unavailable"


def test_run_cases_case_id_stable_across_reorder(monkeypatch):
    """Stable case_id = sample_id + query_idx, never row order."""
    _ensure_modules()
    _patch_facade_for_noop_hits(monkeypatch)
    # Build the cases in a NON-sorted order to confirm the case_id
    # is independent of iteration order.
    cases = [
        _make_case("sampleB", 1),
        _make_case("sampleA", 5),
        _make_case("sampleA", 1),
    ]
    out = runner_module.run_cases(cases)
    assert [r.case_id for r in out] == [
        "sampleB|1", "sampleA|5", "sampleA|1",
    ]
    # Two cases with the SAME (sample_id, query_idx) — the runner
    # does NOT deduplicate; both are run, the second produces the
    # SAME case_id.  This is documented behaviour: deduplication is
    # the caller's responsibility.


def test_run_cases_requires_sample_id_and_query_idx(monkeypatch):
    """Missing keys raise — the runner never silently defaults."""
    _ensure_modules()
    _patch_facade_for_noop_hits(monkeypatch)
    with pytest.raises(ValueError):
        runner_module.run_cases([{"question": "q", "answer": "a"}])
    with pytest.raises(ValueError):
        runner_module.run_cases([{"sample_id": "x"}])


def test_run_cases_rejects_non_full_mode():
    """Only mode='full' is supported today."""
    _ensure_modules()
    with pytest.raises(NotImplementedError):
        runner_module.RunnerConfig(mode="offline")
    with pytest.raises(NotImplementedError):
        runner_module.RunnerConfig(mode="keyword-only")


def test_runner_config_validates_limits():
    _ensure_modules()
    with pytest.raises(ValueError):
        runner_module.RunnerConfig(limit=0)
    with pytest.raises(ValueError):
        runner_module.RunnerConfig(hit_at_k_limit=0)


def test_runner_config_accepts_full_mode():
    _ensure_modules()
    rc = runner_module.RunnerConfig(mode="full", limit=8)
    assert rc.mode == "full"
    assert rc.limit == 8


def test_runner_config_hit_at_k_limit_defaults_to_5():
    """Hit@K default mirrors the production facade ``limit=5``
    so the default ``run_cases_with_metrics`` call never reports
    an N/A ``hit_at_k`` slot (K <= ranking_limit).  The legacy
    ``30`` value is gone.
    """
    _ensure_modules()
    rc = runner_module.RunnerConfig()
    assert rc.hit_at_k_limit == 5
    assert rc.hit_at_k_limit == rc.limit  # default snap = default ranking depth


def test_run_cases_with_metrics_honors_metric_k_truth(monkeypatch):
    """``run_cases_with_metrics`` must pass the actual facade
    ``limit`` to ``compute_metrics`` as ``ranking_limit`` so the
    Hit@K slot never fabricates a fraction above the observable
    ranking depth.  This is the contract that ties runner →
    metrics to the metric-K truth invariant.
    """
    _ensure_modules()
    _patch_facade_for_noop_hits(monkeypatch)
    cases = [_make_case("s", 0), _make_case("s", 1)]
    # limit=8 facade — ranking_limit=8, default hit_at_k_limit=5
    records, snap = runner_module.run_cases_with_metrics(cases, limit=8)
    assert len(records) == 2
    assert snap.ranking_limit == 8
    assert snap.hit_at_k_k == 5
    # 5 <= 8 so the default hit_at_k slot is real (status=ok).
    assert snap.hit_at_k_status == "ok"
    # Hit@1/Hit@5 are also ok because depth 8 >= 5.
    assert snap.hit_at_1_status == "ok"
    assert snap.hit_at_5_status == "ok"


def test_run_cases_with_metrics_ranking_limit_under_k_is_na(monkeypatch):
    """When the facade ``limit`` is shallower than the default
    Hit@K, the ``hit_at_k`` slot is honestly N/A (None in JSON,
    status "n_a") — never a fabricated fraction over a shorter
    surface.
    """
    _ensure_modules()
    _patch_facade_for_noop_hits(monkeypatch)
    cases = [_make_case("s", 0)]
    # limit=2 facade but default hit_at_k_limit=5 — 5 > 2 → n_a.
    records, snap = runner_module.run_cases_with_metrics(cases, limit=2)
    assert len(records) == 1
    assert snap.ranking_limit == 2
    assert snap.hit_at_k_status == "n_a"
    blob = snap.to_dict() if hasattr(snap, "to_dict") else None
    # MetricSnapshot to_dict is exercised in test_metrics; here we
    # just verify the contract via attributes.
    assert snap.hit_at_5_status == "n_a"  # depth 2 < 5
    assert snap.hit_at_1_status == "ok"   # depth 2 >= 1


def test_write_results_jsonl_to_stream(monkeypatch):
    """JSONL writer emits one record per case."""
    _ensure_modules()
    _patch_facade_for_noop_hits(monkeypatch)
    cases = [_make_case("s", i) for i in range(3)]
    records = runner_module.run_cases(cases)
    buf = io.StringIO()
    n = runner_module.write_results_jsonl(records, stream=buf)
    assert n == 3
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    assert len(lines) == 3
    # Each line is a JSON object with case_id; round-trippable.
    for line in lines:
        d = json.loads(line)
        assert "case_id" in d
        assert "ranked_source_ids" in d
        assert "trace_id" in d


def test_write_results_jsonl_to_path(tmp_path, monkeypatch):
    _ensure_modules()
    _patch_facade_for_noop_hits(monkeypatch)
    cases = [_make_case("s", 0)]
    records = runner_module.run_cases(cases)
    out_path = tmp_path / "results.jsonl"
    n = runner_module.write_results_jsonl(records, path=str(out_path))
    assert n == 1
    text = out_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    line = text.strip().splitlines()[0]
    d = json.loads(line)
    assert d["case_id"] == "s|0"


def test_write_results_jsonl_requires_one_of_path_or_stream(monkeypatch):
    _ensure_modules()
    cases = []
    with pytest.raises(ValueError):
        runner_module.write_results_jsonl(cases)
    with pytest.raises(ValueError):
        runner_module.write_results_jsonl(cases, path="x", stream=io.StringIO())


def test_run_cases_with_metrics_returns_both(monkeypatch):
    _ensure_modules()
    _patch_facade_for_noop_hits(monkeypatch)
    cases = [
        _make_case("s", 0),
        _make_case("s", 1),
        _make_case("s", 2),
    ]
    records, snap = runner_module.run_cases_with_metrics(
        cases, engine_invocations=3,
    )
    assert len(records) == 3
    assert snap.questions_total == 3
    assert snap.coverage.engine_invocations == 3


def test_run_cases_does_not_silently_switch_to_keyword_only(monkeypatch):
    """The runner is contractually bound to the production facade.

    A monkeypatched facade fake IS allowed (the runner just calls
    the adapter); what we verify is that the runner does NOT
    install any keyword-only / synthetic-vector short-circuit in
    full mode.  The contract test:
      * call run_cases with no production config;
      * assert that the facade was invoked (not bypassed).
    """
    _ensure_modules()

    call_log = []

    def _fake_facade(query, **kwargs):
        call_log.append((query, sorted(kwargs.keys())))
        return "[fake]\n"

    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", _fake_facade)
    cases = [_make_case("s", i, q=f"Q{i}") for i in range(3)]
    records = runner_module.run_cases(cases)
    # Exactly one facade call per case.
    assert len(call_log) == 3
    assert len(records) == 3
    assert all(r.status == "trace_missing" for r in records)
    assert all(r.error == "RecallTrace unavailable" for r in records)


def test_runner_does_not_import_compare_or_modify_production(monkeypatch):
    """Static invariant: runner source must NOT touch sibling
    compare.py or production v3core files (other than via the
    adapter / metrics modules).
    """
    _ensure_modules()
    src_path = runner_module.__file__
    assert src_path is not None
    src = open(src_path, "r", encoding="utf-8").read()
    # Forbidden imports.
    forbidden = (
        "from .compare import",
        "from eval.locomo_recall_v2.compare import",
        "import psycopg2",
        "psycopg2.connect",
        "git checkout",
        "git reset",
        "git push",
    )
    for pat in forbidden:
        assert pat not in src, f"runner.py must not contain: {pat!r}"


def test_runner_re_exports_adapter_and_metrics():
    """The runner exposes the public surface of both siblings."""
    _ensure_modules()
    assert hasattr(runner_module, "CaseRecord")
    assert hasattr(runner_module, "MetricSnapshot")
    assert hasattr(runner_module, "CoverageAudit")
    assert hasattr(runner_module, "build_effective_flags")
    assert hasattr(runner_module, "resolve_trace_source_ids")


def test_runner_case_record_serialisable(monkeypatch):
    """JSON-roundtrip-safe shape — no custom objects in to_dict."""
    _ensure_modules()
    _patch_facade_for_noop_hits(monkeypatch)
    rec = runner_module.run_case(
        sample_id="s", query_idx=0,
        question="Q?", gold_answer="A.",
        gold_evidence_dia_ids=("d1",), gold_source_ids=("src1",),
    )
    blob = rec.to_dict()
    # json.dumps round-trip must succeed.
    s = json.dumps(blob, sort_keys=True, ensure_ascii=False)
    parsed = json.loads(s)
    assert parsed["case_id"] == "s|0"
    assert parsed["gold_evidence_dia_ids"] == ["d1"]
    assert parsed["gold_source_ids"] == ["src1"]
    assert isinstance(parsed["elapsed_ms"], (int, float))


# ---------------------------------------------------------------------------
# Objective-mode batch tests (run via runner.run_cases — fail-closed)
# ---------------------------------------------------------------------------


def _objective_vec(seed: int, dim: int = 1024) -> list[float]:
    """Deterministic non-zero vector of length ``dim``."""
    return [float((seed * 31 + i + 1) % 17) / 17.0 for i in range(dim)]


def _record_facade_q_embs():
    """Return a list that captures every ``q_emb`` the facade sees."""
    captured: list[list[float] | None] = []

    def _fake_facade(query, **kwargs):
        captured.append(
            list(kwargs.get("q_emb") or [])
            if kwargs.get("q_emb") is not None else None
        )
        return "[fake]\n"

    return _fake_facade, captured


def test_runner_objective_different_case_vectors_reach_facade(monkeypatch):
    """runner.run_cases in objective mode forwards ITS OWN vector
    to each case — never one shared fallback."""
    _ensure_modules()
    fake_facade, captured = _record_facade_q_embs()
    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", fake_facade)
    q_emb_by_case = {
        "s|0": _objective_vec(seed=1),
        "s|1": _objective_vec(seed=2),
        "s|2": _objective_vec(seed=3),
    }
    cases = [
        {"sample_id": "s", "query_idx": i, "question": f"Q{i}", "answer": "A"}
        for i in range(3)
    ]
    out = runner_module.run_cases(
        cases,
        adapter_mode=runner_module.MODE_OBJECTIVE,
        q_emb_by_case=q_emb_by_case,
        expected_dim=1024,
    )
    # One facade invocation per case.
    assert len(captured) == 3
    for i in range(3):
        assert captured[i] == q_emb_by_case[f"s|{i}"]
    assert [r.case_id for r in out] == ["s|0", "s|1", "s|2"]


def test_runner_objective_missing_mapping_raises(monkeypatch):
    """runner.run_cases in objective mode fails closed when a
    case_id is not in ``q_emb_by_case`` — the runner detects the
    missing mapping BEFORE invoking the adapter and raises
    ``ValueError`` so the caller sees the exact case_id that
    broke the contract.  This is the runner's explicit
    configuration-error path; the adapter's ``invalid_q_emb``
    RuntimeError path covers degenerate vectors, not missing
    keys.
    """
    _ensure_modules()
    fake_facade, captured = _record_facade_q_embs()
    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", fake_facade)
    q_emb_by_case = {"s|0": _objective_vec(seed=1)}
    cases = [
        {"sample_id": "s", "query_idx": 0, "question": "Q0", "answer": "A"},
        {"sample_id": "s", "query_idx": 1, "question": "Q1", "answer": "A"},
    ]
    with pytest.raises(ValueError):
        runner_module.run_cases(
            cases,
            adapter_mode=runner_module.MODE_OBJECTIVE,
            q_emb_by_case=q_emb_by_case,
            expected_dim=1024,
        )


def test_runner_objective_extra_mapping_keys_allowed(monkeypatch):
    """Extra keys in the mapping are ignored — only the case_id
    keys actually referenced by ``cases`` are validated."""
    _ensure_modules()
    fake_facade, captured = _record_facade_q_embs()
    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", fake_facade)
    q_emb_by_case = {
        "s|0": _objective_vec(seed=1),
        "s|1": _objective_vec(seed=2),
        "unused|something": _objective_vec(seed=99),
    }
    cases = [
        {"sample_id": "s", "query_idx": 0, "question": "Q0", "answer": "A"},
        {"sample_id": "s", "query_idx": 1, "question": "Q1", "answer": "A"},
    ]
    out = runner_module.run_cases(
        cases,
        adapter_mode=runner_module.MODE_OBJECTIVE,
        q_emb_by_case=q_emb_by_case,
        expected_dim=1024,
    )
    assert len(out) == 2
    assert [r.case_id for r in out] == ["s|0", "s|1"]
    assert len(captured) == 2


def test_runner_objective_zero_vector_rejected(monkeypatch):
    """An all-zero vector aborts the batch via RuntimeError
    (adapter.run_cases fail-closed hook)."""
    _ensure_modules()
    fake_facade, captured = _record_facade_q_embs()
    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", fake_facade)
    q_emb_by_case = {"s|0": [0.0] * 1024}
    cases = [{"sample_id": "s", "query_idx": 0, "question": "Q", "answer": "A"}]
    with pytest.raises(RuntimeError):
        runner_module.run_cases(
            cases,
            adapter_mode=runner_module.MODE_OBJECTIVE,
            q_emb_by_case=q_emb_by_case,
            expected_dim=1024,
        )


def test_runner_objective_dimension_mismatch_rejected(monkeypatch):
    """A vector of the wrong dimension aborts the batch via
    RuntimeError (adapter.run_cases fail-closed hook)."""
    _ensure_modules()
    fake_facade, captured = _record_facade_q_embs()
    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", fake_facade)
    q_emb_by_case = {"s|0": _objective_vec(seed=1, dim=512)}
    cases = [{"sample_id": "s", "query_idx": 0, "question": "Q", "answer": "A"}]
    with pytest.raises(RuntimeError):
        runner_module.run_cases(
            cases,
            adapter_mode=runner_module.MODE_OBJECTIVE,
            q_emb_by_case=q_emb_by_case,
            expected_dim=1024,
        )


def test_runner_structural_ignores_q_emb_by_case(monkeypatch):
    """Structural mode forwards the single ``q_emb`` to every case
    regardless of the mapping — backwards compatible."""
    _ensure_modules()
    fake_facade, captured = _record_facade_q_embs()
    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", fake_facade)
    shared = _objective_vec(seed=5)
    q_emb_by_case = {
        "s|0": _objective_vec(seed=99),
        "s|1": _objective_vec(seed=100),
    }
    cases = [
        {"sample_id": "s", "query_idx": 0, "question": "Q0", "answer": "A"},
        {"sample_id": "s", "query_idx": 1, "question": "Q1", "answer": "A"},
    ]
    runner_module.run_cases(cases, q_emb=shared, q_emb_by_case=q_emb_by_case)
    assert captured == [list(shared), list(shared)]


def test_runner_config_objective_requires_expected_dim():
    """RunnerConfig rejects objective mode without positive dim."""
    _ensure_modules()
    with pytest.raises(ValueError):
        runner_module.RunnerConfig(
            adapter_mode=runner_module.MODE_OBJECTIVE,
        )
    with pytest.raises(ValueError):
        runner_module.RunnerConfig(
            adapter_mode=runner_module.MODE_OBJECTIVE,
            expected_dim=0,
        )


def test_runner_config_objective_accepts_positive_dim():
    _ensure_modules()
    rc = runner_module.RunnerConfig(
        adapter_mode=runner_module.MODE_OBJECTIVE,
        expected_dim=1024,
    )
    assert rc.adapter_mode == "objective"
    assert rc.expected_dim == 1024


def test_runner_config_rejects_unknown_adapter_mode():
    _ensure_modules()
    with pytest.raises(ValueError):
        runner_module.RunnerConfig(adapter_mode="weird")


def test_runner_write_results_jsonl_preserves_three_evidence_fields(monkeypatch):
    """JSONL writer round-trips all three GoldEvidence fields per
    record — they remain distinct on disk."""
    _ensure_modules()
    _patch_facade_for_noop_hits(monkeypatch)
    cases = [{
        "sample_id": "s",
        "query_idx": 0,
        "question": "Q?",
        "answer": "A.",
        "gold_evidence_dia_ids": ("d-1", "d-2"),
        "gold_source_ids": ("src-mapped",),
        "unmapped_dia_ids": ("d-3",),
        "unresolved_evidence": ("d-1, d-2",),
    }]
    records = runner_module.run_cases(cases)
    buf = io.StringIO()
    n = runner_module.write_results_jsonl(records, stream=buf)
    assert n == 1
    parsed = json.loads(buf.getvalue().strip())
    assert parsed["gold_source_ids"] == ["src-mapped"]
    assert parsed["unmapped_dia_ids"] == ["d-3"]
    assert parsed["unresolved_evidence"] == ["d-1, d-2"]


def test_runner_objective_invalid_q_emb_status_triggers_runtimeerror(monkeypatch):
    """When the adapter produces an ``invalid_q_emb`` record the
    runner aborts the batch with ``RuntimeError`` (fail-closed)."""
    _ensure_modules()
    # Monkeypatch the runner's local alias (the import the runner
    # actually invokes) so the runner sees the synthetic record
    # rather than the production adapter.run_case.
    invalid = runner_module.CaseRecord(
        case_id="s|0",
        sample_id="s",
        query_idx=0,
        category="c",
        question="Q",
        answer="A",
        gold_evidence_dia_ids=(),
        gold_source_ids=(),
        unmapped_dia_ids=(),
        unresolved_evidence=(),
        context_block_length=0,
        trace_id="",
        ranked_source_ids=(),
        selected_source_ids=(),
        candidate_source_ids=(),
        injected_source_ids=(),
        lane_summaries=(),
        injection_summary=None,
        drop_summary={},
        elapsed_ms=0.0,
        status="invalid_q_emb",
        error="all-zero",
    )
    monkeypatch.setattr(
        runner_module, "_adapter_run_case",
        lambda **kwargs: invalid,
    )
    cases = [{"sample_id": "s", "query_idx": 0, "question": "Q", "answer": "A"}]
    with pytest.raises(RuntimeError):
        runner_module.run_cases(
            cases,
            adapter_mode=runner_module.MODE_OBJECTIVE,
            q_emb_by_case={"s|0": _objective_vec(seed=1)},
            expected_dim=1024,
        )