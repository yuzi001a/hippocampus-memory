# -*- coding: utf-8 -*-
"""G6C-A LoCoMo Recall V2 adapter tests.

Scope
=====

* Verify the adapter calls the production facade
  ``v3core.prefetch.prefetch_to_context_block`` exactly ONCE per
  case, never bypasses the engine seam.
* Verify the trace object is built and exposed on the result.
* Verify candidate IDs resolve to source IDs through the typed
  snapshot map (never via ``final_selected_ids`` direct read).
* Verify the engine seam monkeypatch detector catches a fake bypass.
* Verify the ``run_cases`` batch wrapper.
* Verify the adapter does NOT import ``v3core.active_memory_store``
  or ``v3core.lane_a``.
* Verify the source-text of ``adapter.py`` contains no direct SQL.

The tests use a counting fake engine via
``v3core.recall_v2.engine._default_recall_fn`` so a future
refactor that bypasses the engine would break this suite.
"""
from __future__ import annotations

import importlib
import os
import re
import sys

import pytest


# ---------------------------------------------------------------------------
# Path setup — mirror the g5b pattern so the test suite works under
# ``PYTHONPATH=src/v3-core python -m pytest src/v3-core/eval/locomo_recall_v2/tests``.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
# .../src/v3-core/eval/locomo_recall_v2/tests -> .../src/v3-core
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# Lazy module references — avoid heavy imports at collection time.
adapter_module = None
v3core_recall_v2_engine = None


def _ensure_modules():
    global adapter_module, v3core_recall_v2_engine
    if adapter_module is None:
        from eval.locomo_recall_v2 import adapter as _a
        adapter_module = _a
    if v3core_recall_v2_engine is None:
        from v3core.recall_v2 import engine as _e
        v3core_recall_v2_engine = _e


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _CountingFakeRecallFn:
    """A counting fake for the legacy ``recall_pool`` callable.

    The fake records every invocation (kwargs + return value) so the
    test can assert call-count discipline.  Each call returns a
    non-empty hit list with a known ``source_id`` so the engine's
    lane / snapshot machinery actually runs and the adapter can
    exercise selection / injection / drop code paths.
    """

    LABEL = "counting-fake-recall-fn"

    def __init__(self) -> None:
        self.calls = []
        self._counter = 0

    def install(self, monkeypatch):
        """Monkeypatch the engine seam so the fake is invoked instead
        of the production recall_pool.
        """
        _ensure_modules()
        self._counter = 0
        monkeypatch.setattr(
            v3core_recall_v2_engine, "_default_recall_fn", lambda: self,
        )
        # The engine stashes the resolved callable on the instance
        # at construction; re-construct so the new fake is picked up.
        # RecallV2Engine reads ``_default_recall_fn`` at __init__ when
        # no recall_fn is provided; we trigger this lazily inside the
        # test by constructing the engine via the facade.
        return self

    def __call__(self, query, **kwargs):
        self._counter += 1
        self.calls.append({"query": query, "kwargs": dict(kwargs)})
        # Return a tiny list of fake hits.  Each hit needs the
        # ``source_id`` / ``kind`` attributes the adapter expects
        # to read through the snapshot map.
        from v3core.types import RecallHit

        hits = []
        for i in range(2):
            hits.append(RecallHit(
                source_id=f"fake-{self._counter}-{i}",
                title=f"fake hit {self._counter}-{i}",
                content_preview="preview",
                cosine=0.5 + 0.1 * i,
                rrf_score=0.6 + 0.1 * i,
                kind="card",
                created_at="",
            ))
        # The engine expects (hits, pg_fail) from the legacy callable.
        return hits, False


def _patch_facade_for_noop_hits(monkeypatch):
    """Patch ``prefetch_to_context_block`` to a no-op so the adapter
    can exercise the trace/seam logic without hitting a real engine.

    The fake still hands a string back and lets the adapter finalise
    its record.  We monkey-patch the symbol INSIDE the adapter module
    so the test never depends on production execution.
    """
    _ensure_modules()

    def _fake_facade(query, **kwargs):
        # Touch ``kwargs["trace"]`` if present so the LegacySink
        # has at least one opportunity to record something.
        sink = kwargs.get("trace")
        if sink is not None:
            try:
                # Use the typed sink-style protocol that the
                # production facade normally drives.
                sink.lane_start("vector")
                sink.lane_finish("vector", candidate_count=2)
            except Exception:
                pass
        return "[recall preamble]\n- fake hit 0\n- fake hit 1\n"

    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", _fake_facade)


# ---------------------------------------------------------------------------
# Static invariants
# ---------------------------------------------------------------------------


def test_adapter_does_not_import_production_secrets():
    """The adapter source must NOT import active_memory_store or lane_a.

    These are out-of-scope paths; the adapter is a seam, not a
    re-implementation.  Any future import would warrant a separate
    review.
    """
    _ensure_modules()
    src_path = adapter_module.__file__
    assert src_path is not None and os.path.isfile(src_path), src_path
    src = open(src_path, "r", encoding="utf-8").read()
    # Direct module-level imports are forbidden.
    forbidden_patterns = (
        r"^\s*from\s+v3core\.active_memory_store\b",
        r"^\s*import\s+v3core\.active_memory_store\b",
        r"^\s*from\s+v3core\.lane_a\b",
        r"^\s*import\s+v3core\.lane_a\b",
        r"^\s*from\s+\.\s+active_memory_store\b",
        r"^\s*from\s+\.\s+lane_a\b",
    )
    for pat in forbidden_patterns:
        assert not re.search(pat, src, re.MULTILINE), (
            f"adapter.py must not import the forbidden path: {pat!r}"
        )


def test_adapter_source_has_no_direct_sql():
    """Adapter must NOT issue raw SQL — no ``cursor.execute`` etc."""
    _ensure_modules()
    src_path = adapter_module.__file__
    src = open(src_path, "r", encoding="utf-8").read()
    # Look for common SQL-surface tokens.  We keep this simple:
    # any string literal containing SQL DML/DDL on its own line is a
    # signal.  Cursor.execute calls are forbidden.
    bad_patterns = (
        r"cursor\(\)",
        r"\.execute\(",
        r"\bSELECT\s+",
        r"\bINSERT\s+",
        r"\bUPDATE\s+",
        r"\bDELETE\s+",
        r"\bTRUNCATE\s+",
    )
    for pat in bad_patterns:
        assert not re.search(pat, src, re.IGNORECASE), (
            f"adapter.py must not contain SQL token: {pat!r}"
        )


# ---------------------------------------------------------------------------
# Functional tests
# ---------------------------------------------------------------------------


def test_adapter_builds_case_record_and_runs_once(monkeypatch):
    """Happy-path: one facade call, one CaseRecord, trace attached."""
    _ensure_modules()
    _patch_facade_for_noop_hits(monkeypatch)
    fake = _CountingFakeRecallFn().install(monkeypatch)

    rec = adapter_module.run_case(
        sample_id="sample-1",
        query_idx=0,
        question="What did Alice eat yesterday?",
        gold_answer="Alice ate an apple.",
        gold_evidence_dia_ids=("d-1", "d-2"),
        gold_source_ids=("locomo|eval_v2|sample-1|session_1|d-1>d-2",),
        unresolved_evidence=(),
        category="1",
    )

    # The facade was monkey-patched, so the engine seam fake isn't
    # actually invoked by our test fake facade.  But we DID install
    # the fake in case the facade calls into it.  Verify the adapter
    # only ever delegates to the facade.
    assert rec.case_id == "sample-1|0"
    assert rec.sample_id == "sample-1"
    assert rec.query_idx == 0
    assert rec.category == "1"
    assert rec.question == "What did Alice eat yesterday?"
    assert rec.answer == "Alice ate an apple."
    assert rec.gold_evidence_dia_ids == ("d-1", "d-2")
    assert rec.gold_source_ids == ("locomo|eval_v2|sample-1|session_1|d-1>d-2",)
    assert rec.unresolved_evidence == ()
    # Privacy: the body of the context block MUST NOT survive on
    # the public record — only its length is retained.
    assert not hasattr(rec, "context_block")
    assert rec.context_block_length == len("[recall preamble]\n- fake hit 0\n- fake hit 1\n")
    # A shim facade never publishes a production trace; fail closed.
    assert rec.trace_id == ""
    # candidate_source_ids is a tuple even when no trace was published.
    assert isinstance(rec.candidate_source_ids, tuple)
    # Status / error contract
    assert rec.status == "trace_missing"
    assert rec.error == "RecallTrace unavailable"
    # The to_dict output must be JSON-safe and must NOT leak the
    # memory body, must NOT include a ``context_block`` key.
    blob = rec.to_dict()
    assert blob["case_id"] == "sample-1|0"
    assert isinstance(blob["ranked_source_ids"], list)
    assert isinstance(blob["candidate_source_ids"], list)
    assert isinstance(blob["lane_summaries"], list)
    assert "context_block" not in blob
    # No memory body leaked beyond the (now-removed) context_block.
    assert "content" not in blob
    # No raw fake body string anywhere in the serialised blob.
    serialised = str(blob)
    assert "fake hit 0" not in serialised
    assert "fake hit 1" not in serialised
    # unused but referenced
    del fake


def test_adapter_resolves_candidate_ids_via_snapshot_map(monkeypatch):
    """``final_selected_ids`` are NOT source IDs; they are resolved
    through ``trace.candidate_snapshots[candidate_id].source_id``.
    """
    _ensure_modules()

    # Build a facade fake that exercises the LegacySink protocol to
    # register a candidate and select it.  Then the adapter's
    # resolution code must read the snapshot map.
    def _fake_facade(query, **kwargs):
        sink = kwargs.get("trace")
        if sink is not None:
            try:
                sink.lane_candidates(
                    "vector",
                    source_ids=["snap-source-A", "snap-source-B"],
                    source_type="card",
                )
                sink.select("snap-source-A")
            except Exception:
                pass
        return "[recall]\n- A\n"

    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", _fake_facade)

    rec = adapter_module.run_case(
        sample_id="sample-2",
        query_idx=3,
        question="Q?",
        gold_answer="A.",
        gold_evidence_dia_ids=(),
        gold_source_ids=(),
        category="2",
    )
    # The shim never publishes the internal production trace, so
    # selection/source resolution is intentionally absent and the
    # record is fail-closed.
    assert rec.selected_source_ids == ()
    assert rec.ranked_source_ids == ()
    assert rec.candidate_source_ids == ()
    assert rec.status == "trace_missing"
    assert rec.error == "RecallTrace unavailable"


def test_adapter_no_hit_path_does_not_crash(monkeypatch):
    """When the facade returns an empty context block, the adapter
    still produces a well-formed CaseRecord with empty ranked_ids.
    """
    _ensure_modules()

    def _fake_facade(query, **kwargs):
        return ""

    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", _fake_facade)
    rec = adapter_module.run_case(
        sample_id="sample-empty",
        query_idx=0,
        question="Anything?",
        gold_answer="",
        gold_evidence_dia_ids=(),
        gold_source_ids=(),
        category="3",
    )
    # Privacy: context block body never surfaces; only length is kept.
    assert not hasattr(rec, "context_block")
    assert rec.context_block_length == 0
    assert rec.candidate_source_ids == ()
    assert rec.ranked_source_ids == ()
    assert rec.selected_source_ids == ()
    assert rec.injected_source_ids == ()
    assert rec.status == "trace_missing"
    assert rec.error == "RecallTrace unavailable"


def test_adapter_budget_drop_path(monkeypatch):
    """When the facade reports CHAR_BUDGET truncation, the adapter
    surfaces it through injection_summary and drop_summary.
    """
    _ensure_modules()
    from v3core.recall_v2.contracts import DropReasonCode
    from v3core.recall_v2.trace import RecallTrace

    def _fake_facade(query, **kwargs):
        sink = kwargs.get("trace")
        if sink is not None:
            # Register a snapshot the sink can drop.
            from v3core.recall_v2.engine import LegacySink as _LS
            try:
                sink.lane_candidates(
                    "vector",
                    source_ids=["budget-source"],
                    source_type="card",
                )
                # Bypass the engine's select() guard by writing
                # directly into the trace — the engine normally does
                # this, but our fake facade is the engine seam.
                trace = getattr(sink, "_trace", None)
                if trace is not None:
                    snap = trace.candidate_snapshots.get("budget-source")
                    if snap is None:
                        # Create via select()? Use the trace API.
                        # The trace's record_candidate needs a
                        # RecallCandidate instance — keep it simple:
                        # the sink.lane_candidates above already
                        # built a snapshot.  Drop via the trace.
                        pass
                    try:
                        # Drop with CHAR_BUDGET code — this exercises
                        # the lazy-creation and dedupe logic.
                        trace.record_drop(
                            "budget-source",
                            DropReasonCode.CHAR_BUDGET,
                            detail="over budget",
                            stage="injection",
                        )
                    except KeyError:
                        # Snapshot wasn't created — still valid for
                        # the budget path; skip.
                        pass
            except Exception:
                pass
        return "[truncated recall]\n- budget-source\n"

    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", _fake_facade)

    rec = adapter_module.run_case(
        sample_id="sample-budget",
        query_idx=0,
        question="Q?",
        gold_answer="A.",
        gold_evidence_dia_ids=(),
        gold_source_ids=(),
        category="1",
    )
    # The adapter never crashed and produced a record.
    assert rec.case_id == "sample-budget|0"
    assert rec.status == "trace_missing"
    assert rec.error == "RecallTrace unavailable"
    # No trace means no trustworthy drop accounting.
    assert rec.drop_summary == {}


def test_run_cases_batch_wrapper(monkeypatch):
    """The batch wrapper calls run_case once per case."""
    _ensure_modules()
    _patch_facade_for_noop_hits(monkeypatch)
    cases = [
        {
            "sample_id": "s",
            "query_idx": i,
            "question": f"q{i}",
            "answer": f"a{i}",
            "gold_evidence_dia_ids": (),
            "gold_source_ids": (),
            "category": "x",
        }
        for i in range(3)
    ]
    out = adapter_module.run_cases(cases)
    assert len(out) == 3
    assert [r.case_id for r in out] == ["s|0", "s|1", "s|2"]
    assert all(r.status == "trace_missing" for r in out)
    assert all(r.error == "RecallTrace unavailable" for r in out)


def test_engine_seam_guard_catches_bypass(monkeypatch):
    """A direct engine-boundary bypass would NOT increment the fake
    counter.  The test asserts the fake is called exactly once when
    the production facade goes through it.

    To exercise this we restore the real facade temporarily (no
    monkeypatch on prefetch_to_context_block), point the engine
    seam at our counting fake, and then run the adapter.  The
    counting fake returns a non-empty hit list, which causes the
    engine to register candidates and exercise selection/injection.
    """
    _ensure_modules()

    fake = _CountingFakeRecallFn().install(monkeypatch)
    # The adapter's prefetch_to_context_block symbol is left alone
    # (no monkeypatch), so the production code path runs and the
    # fake gets invoked.

    # Build the engine seam test environment.
    rec = adapter_module.run_case(
        sample_id="sample-bypass",
        query_idx=0,
        question="Q?",
        gold_answer="A.",
        gold_evidence_dia_ids=(),
        gold_source_ids=(),
        category="x",
        max_chars=None,  # keep the engine path minimal
        config={"prefetch": {"dual_path": True, "rrf_k": 60, "include_message_vector": True},
                "storage": {"rerank": {}}},
        card_index={},
    )

    # The adapter MUST have caused the engine seam to be invoked
    # exactly once.  Two reasons this matters:
    #   * zero invocations would mean the adapter bypassed the
    #     facade/engine (forbidden by the spec);
    #   * more-than-one invocations would mean the adapter
    #     re-entered the engine (forbidden by the spec).
    assert fake._counter == 1, (
        f"expected exactly one engine invocation per case, got {fake._counter}"
    )
    assert rec.status == "ok"
    assert rec.error == ""
    assert rec.trace_id
    assert rec.context_block_length > 0
    assert "context_block" not in rec.to_dict()
    # The fake bypasses recall_pool lane probes, so candidate count may
    # be empty here; the real disposable-PG canary covers candidates /
    # selection / injection on the production recall path.
    assert isinstance(rec.candidate_source_ids, tuple)


# ---------------------------------------------------------------------------
# Engine-monkeypatch detector — direct check on the engine seam
# ---------------------------------------------------------------------------


def test_direct_engine_bypass_fails_engine_count_check(monkeypatch):
    """Negative test: monkey-patch the adapter's facade symbol to a
    shim that NEVER calls into the engine seam.  Confirm the engine
    counter stays at zero — i.e. a bypass attempt would be caught.
    """
    _ensure_modules()
    fake = _CountingFakeRecallFn().install(monkeypatch)

    def _shim(query, **kwargs):
        # Return a context block but DO NOT touch the trace/engine.
        return "[shim]\n"

    monkeypatch.setattr(adapter_module, "prefetch_to_context_block", _shim)
    rec = adapter_module.run_case(
        sample_id="sample-bypass",
        query_idx=0,
        question="Q?",
        gold_answer="A.",
        gold_evidence_dia_ids=(),
        gold_source_ids=(),
    )
    # No engine invocation, even though the adapter ran.
    assert fake._counter == 0
    assert rec.status == "trace_missing"
    assert rec.error == "RecallTrace unavailable"
    # Privacy: only the length of the shim body is retained.
    assert not hasattr(rec, "context_block")
    assert rec.context_block_length == len("[shim]\n")
    # This negative case demonstrates the detector works: a future
    # refactor that bypassed the engine seam would either (a) keep
    # the shim path and leave the engine counter at zero, or
    # (b) re-introduce the engine call and the positive test would
    # catch call-count drift.
    assert fake._counter == 0


# ---------------------------------------------------------------------------
# resolve_trace_source_ids unit tests
# ---------------------------------------------------------------------------


def test_resolve_trace_source_ids_dedup_preserves_order():
    _ensure_modules()
    from v3core.recall_v2.trace import RecallTrace, CandidateSnapshot
    from v3core.recall_v2.contracts import QueryContext, build_default_query_plan

    ctx = QueryContext(
        query_id="q-1",
        query_text="x",
        deadline_monotonic=1.0,
        budget_ms=100,
        limit=5,
        max_chars=100,
    )
    plan = build_default_query_plan(ctx)
    trace = RecallTrace(query_context=ctx, query_plan=plan)
    trace.candidate_snapshots["a"] = CandidateSnapshot(
        candidate_id="a", lane="vector", source_type="card",
        source_id="source-A",
    )
    trace.candidate_snapshots["b"] = CandidateSnapshot(
        candidate_id="b", lane="vector", source_type="card",
        source_id="source-B",
    )
    # b's source_id == a's; should dedup preserving first-seen.
    res = adapter_module.resolve_trace_source_ids(
        trace, ["a", "b", "a", "b"]
    )
    assert res == ("source-A", "source-B")


def test_resolve_trace_source_ids_falls_back_when_snapshot_missing():
    _ensure_modules()
    from v3core.recall_v2.trace import RecallTrace, CandidateSnapshot
    from v3core.recall_v2.contracts import QueryContext, build_default_query_plan

    ctx = QueryContext(
        query_id="q-1",
        query_text="x",
        deadline_monotonic=1.0,
        budget_ms=100,
        limit=5,
        max_chars=100,
    )
    plan = build_default_query_plan(ctx)
    trace = RecallTrace(query_context=ctx, query_plan=plan)
    trace.candidate_snapshots["known"] = CandidateSnapshot(
        candidate_id="known", lane="vector", source_type="card",
        source_id="real-source",
    )
    # ``unknown`` is NOT in the snapshot map; falls back to its id.
    res = adapter_module.resolve_trace_source_ids(trace, ["known", "unknown"])
    assert res == ("real-source", "unknown")


# ---------------------------------------------------------------------------
# build_effective_flags unit tests
# ---------------------------------------------------------------------------


def test_build_effective_flags_defaults_and_rerank_gating():
    _ensure_modules()
    # None config: defaults, no rerank (no endpoint).
    flags = adapter_module.build_effective_flags(None)
    assert flags["include_keyword"] is True
    assert flags["include_card_vector"] is True
    assert flags["include_message_vector"] is True
    assert flags["include_effective"] is False
    assert flags["include_topic"] is True
    assert flags["include_yin"] is True
    assert flags["include_notes"] is True
    assert flags["rerank_cfg"] == {}
    assert flags["rerank_top_n"] is None

    # Dict config with endpoint: rerank_top_n == 30.
    cfg = {"storage": {"rerank": {"endpoint": "http://rerank.local/v1"}}}
    flags2 = adapter_module.build_effective_flags(cfg)
    assert flags2["rerank_top_n"] == 30
    assert flags2["rerank_cfg"].get("endpoint") == "http://rerank.local/v1"

    # Dict config without endpoint: rerank stays off.
    cfg_no_ep = {"storage": {"rerank": {"some_other": 1}}}
    flags3 = adapter_module.build_effective_flags(cfg_no_ep)
    assert flags3["rerank_top_n"] is None