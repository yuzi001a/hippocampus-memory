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