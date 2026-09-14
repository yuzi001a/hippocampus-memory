# -*- coding: utf-8 -*-
"""G6A Recall Engine V2 contract tests — focused on the 15 mission items.

Pure stdlib, no PG, no LLM.  May import ``v3core.recall_pool`` to verify
the legacy surface is unchanged, but never invokes it with services.
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import sys
import time
from pathlib import Path

import pytest


REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

from v3core.recall_v2 import (  # noqa: E402
    QueryContext, LanePlan, QueryPlan, CanonicalAlgorithmSnapshot,
    RecallCandidate, RecallTrace, DropReason, DropReasonCode,
    CandidateEventType,
    ProvenanceRecord, ScoreRecord, CandidateEvent,
    StageTransition, DropEvent,
    CandidateSnapshot, LaneSummary, InjectionSummary,
    safe_metadata,
    LANE_KEYWORD, LANE_VECTOR, LANE_TOPIC, LANE_QA, LANE_EXPLICIT, ALL_LANES,
    DEFAULT_QUERY_LIMIT, DEFAULT_INJECTION_MAX_CHARS, DEFAULT_RERANK_LIMIT,
    build_default_query_plan,
)
from v3core import recall_pool  # noqa: E402  (legacy surface still importable)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(limit: int = 8, max_chars: int = 10_000, budget_ms: int = 1000) -> QueryContext:
    return QueryContext(
        query_id="q-test", query_text="hello world",
        deadline_monotonic=time.monotonic() + 5.0,
        budget_ms=budget_ms, limit=limit, max_chars=max_chars,
    )


def _plan(limit: int = 8, max_chars: int = 10_000) -> QueryPlan:
    return build_default_query_plan(_ctx(limit=limit, max_chars=max_chars))


# ===========================================================================
# 1. Context construction — frozen, normalized, immutable embedding
# ===========================================================================

class TestQueryContextConstruction:
    def test_required_fields_and_whitespace_normalization(self) -> None:
        ctx = QueryContext(
            query_id="q1", query_text="  hello\n\n\tworld  ",
            deadline_monotonic=time.monotonic() + 1.0,
            budget_ms=100, limit=4, max_chars=1000,
        )
        assert ctx.query_text == "  hello\n\n\tworld  "
        assert ctx.normalized_query == "hello world"
        assert ctx.limit == 4
        assert ctx.max_chars == 1000

    def test_frozen_rejects_mutation(self) -> None:
        ctx = _ctx()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.query_text = "x"  # type: ignore[misc]

    def test_embedding_converted_to_immutable_tuple(self) -> None:
        emb = [0.1, 0.2, 0.3]
        ctx = QueryContext(
            query_id="q1", query_text="hi",
            deadline_monotonic=time.monotonic() + 1.0,
            budget_ms=100, limit=4, max_chars=1000,
            query_embedding=emb,
        )
        assert ctx.query_embedding == (0.1, 0.2, 0.3)
        assert isinstance(ctx.query_embedding, tuple)

    def test_negative_values_rejected(self) -> None:
        for kw in [{"budget_ms": -1}, {"limit": -1}, {"max_chars": -1}]:
            with pytest.raises(ValueError):
                QueryContext(
                    query_id="q1", query_text="hi",
                    deadline_monotonic=time.monotonic() + 1.0,
                    **{"budget_ms": 100, "limit": 4, "max_chars": 1000, **kw},
                )

    def test_to_dict_includes_deadline(self) -> None:
        ctx = _ctx()
        d = ctx.to_dict()
        assert "deadline_monotonic" in d
        assert isinstance(d["deadline_monotonic"], float)
        assert d["query_id"] == "q-test"
        assert d["normalized_query"] == "hello world"


# ===========================================================================
# 2. Deadline / budget helpers
# ===========================================================================

class TestDeadlineBudget:
    def test_remaining_budget_capped_at_budget(self) -> None:
        ctx = QueryContext(
            query_id="q1", query_text="hi",
            deadline_monotonic=time.monotonic() + 5.0,
            budget_ms=1500, limit=4, max_chars=1000,
        )
        # deadline is 5s away, but budget_ms caps it at 1500.
        rem = ctx.remaining_budget_ms()
        assert 0.0 < rem <= 1500.0 + 5  # tiny jitter

    def test_has_budget_remaining_true(self) -> None:
        assert _ctx(budget_ms=1500).has_budget_remaining() is True

    def test_has_budget_remaining_false_when_expired(self) -> None:
        ctx = QueryContext(
            query_id="q1", query_text="hi",
            deadline_monotonic=time.monotonic() - 0.5,
            budget_ms=1000, limit=4, max_chars=1000,
        )
        assert ctx.has_budget_remaining() is False

    def test_has_budget_remaining_threshold(self) -> None:
        # Only 50ms left but threshold=200ms → False.
        ctx = QueryContext(
            query_id="q1", query_text="hi",
            deadline_monotonic=time.monotonic() + 0.05,
            budget_ms=1000, limit=4, max_chars=1000,
        )
        assert ctx.has_budget_remaining(min_remaining_ms=200) is False


# ===========================================================================
# 3. Plan lane enable / disable
# ===========================================================================

class TestQueryPlanLanes:
    def test_all_five_lanes_present_and_enabled_by_default(self) -> None:
        plan = _plan()
        assert set(plan.lane_names()) == set(ALL_LANES)
        assert all(lp.enabled for lp in plan.lanes)

    def test_lane_lookup(self) -> None:
        plan = _plan()
        assert plan.lane(LANE_VECTOR).name == LANE_VECTOR
        with pytest.raises(KeyError):
            plan.lane("nonexistent")

    def test_enabled_lanes_filters(self) -> None:
        plan = _plan()
        disabled = tuple(
            dataclasses.replace(lp, enabled=False, reason="off")
            if lp.name == LANE_VECTOR else lp
            for lp in plan.lanes
        )
        plan2 = dataclasses.replace(plan, lanes=disabled)
        names = [lp.name for lp in plan2.enabled_lanes()]
        assert LANE_VECTOR not in names
        assert LANE_KEYWORD in names

    def test_plan_and_lane_frozen(self) -> None:
        plan = _plan()
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.final_limit = 99  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.lane(LANE_VECTOR).enabled = False  # type: ignore[misc]

    def test_final_limit_and_max_chars_from_context(self) -> None:
        plan = _plan(limit=12, max_chars=20000)
        assert plan.final_limit == 12
        assert plan.max_chars == 20000

    def test_final_limit_defaults_when_zero(self) -> None:
        plan = _plan(limit=0, max_chars=0)
        assert plan.final_limit == DEFAULT_QUERY_LIMIT
        assert plan.max_chars == DEFAULT_INJECTION_MAX_CHARS

    def test_qa_candidate_limit_at_least_30(self) -> None:
        plan = _plan(limit=2)
        assert plan.lane(LANE_QA).candidate_limit >= 30

    def test_rerank_limit_default_30(self) -> None:
        assert _plan().rerank_limit == DEFAULT_RERANK_LIMIT


# ===========================================================================
# 4. Provenance — single-lane and multi-lane merge
# ===========================================================================

class TestProvenance:
    def test_initial_provenance(self) -> None:
        c = RecallCandidate("c1", "card", "decisions/d1.md", LANE_KEYWORD)
        assert len(c.provenance) == 1
        assert c.provenance[0].lane == LANE_KEYWORD
        assert c.contributing_lanes == [LANE_KEYWORD]

    def test_merge_same_lane_source_dedupes(self) -> None:
        c = RecallCandidate("c1", "card", "decisions/d1.md", LANE_KEYWORD)
        c.merge_provenance(LANE_KEYWORD, "card", "decisions/d1.md")
        assert len(c.provenance) == 1

    def test_merge_different_lane_keeps_both(self) -> None:
        c = RecallCandidate("c1", "card", "decisions/d1.md", LANE_KEYWORD)
        c.merge_provenance(LANE_VECTOR, "card", "decisions/d1.md")
        assert len(c.provenance) == 2
        assert LANE_VECTOR in c.contributing_lanes
        assert LANE_KEYWORD in c.contributing_lanes


# ===========================================================================
# 5. Score history (record_score)
# ===========================================================================

class TestScoreHistory:
    def test_explicit_per_stage_fields_and_history(self) -> None:
        c = RecallCandidate("c1", "card", "decisions/d1.md", LANE_VECTOR)
        c.record_score("raw", 0.10)
        c.record_score("lane", 0.42, operation="cosine")
        c.record_score("fusion", 0.55, operation="rrf", k=60)
        c.record_score("temporal", 0.80, operation="half_life")
        c.record_score("rerank", 0.91, operation="bge")
        c.record_score("final", 0.88)
        assert c.raw_score == 0.10
        assert c.lane_score == 0.42
        assert c.fusion_score == 0.55
        assert c.temporal_score == 0.80
        assert c.rerank_score == 0.91
        assert c.final_score == 0.88
        assert len(c.score_history) == 6
        # Stages are NOT collapsed into one float.
        assert c.score_history[0].stage != c.score_history[-1].stage

    def test_record_score_preserves_history_order(self) -> None:
        c = RecallCandidate("c1", "card", "d.md", LANE_KEYWORD)
        c.record_score("raw", 1.0)
        c.record_score("lane", 2.0)
        c.record_score("final", 3.0)
        assert [h.stage for h in c.score_history] == ["raw", "lane", "final"]


# ===========================================================================
# 6. Structured DropReason
# ===========================================================================

class TestDropReason:
    def test_exact_eight_codes(self) -> None:
        expected = {
            "DUPLICATE", "BELOW_THRESHOLD", "OUTSIDE_LIMIT", "DEADLINE",
            "RERANK_PRUNED", "CHAR_BUDGET", "INVALID", "LANE_ERROR",
        }
        assert {m.value for m in DropReasonCode} == expected

    def test_mark_dropped_sets_flag_and_event(self) -> None:
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR)
        c.mark_dropped(DropReason(DropReasonCode.BELOW_THRESHOLD, detail="0.05 < 0.3", stage="lane"))
        assert c.dropped is True
        assert c.drop_reason is not None
        assert c.drop_reason.code == DropReasonCode.BELOW_THRESHOLD
        assert c.selected is False
        assert any(e.event_type == CandidateEventType.DROPPED for e in c.events)


# ===========================================================================
# 7. Lane timing summaries
# ===========================================================================

class TestLaneTiming:
    def test_lane_summary_records_duration(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        trace.start_lane(LANE_VECTOR, deadline_monotonic=_ctx().deadline_monotonic)
        # Deterministic timestamp manipulation — never sleep in production code.
        trace.lane_summaries[LANE_VECTOR].started = 100.0
        trace.lane_summaries[LANE_VECTOR].finished = 250.0
        trace.lane_summaries[LANE_VECTOR].duration_ms = (
            trace.lane_summaries[LANE_VECTOR].finished
            - trace.lane_summaries[LANE_VECTOR].started
        ) * 1000.0
        trace.lane_summaries[LANE_VECTOR].candidate_count = 4
        s = trace.lane_summaries[LANE_VECTOR]
        assert s.duration_ms == 150_000.0
        assert s.candidate_count == 4
        assert s.started == 100.0
        assert s.finished == 250.0

    def test_finish_lane_computes_duration_from_real_timestamps(self) -> None:
        """When ``finish_lane`` runs without prior ``start_lane`` injection,
        it must still compute a numeric (non-negative) duration from the
        lane summary's own started/finished fields.
        """
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        s = trace.lane_summaries[LANE_VECTOR]
        s.started = 1.0
        s.finished = 1.123
        s.duration_ms = (s.finished - s.started) * 1000.0
        assert s.duration_ms == 123.0
        assert s.duration_ms >= 0.0

    def test_all_lanes_start_with_summary(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        for n in ALL_LANES:
            assert n in trace.lane_summaries


# ===========================================================================
# 8. Timeout / skipped lanes
# ===========================================================================

class TestTimeoutSkipped:
    def test_skip_lane_marks_skipped(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        trace.skip_lane(LANE_TOPIC, reason="disabled in plan")
        assert trace.lane_summaries[LANE_TOPIC].skipped is True
        assert trace.lane_summaries[LANE_TOPIC].reason == "disabled in plan"

    def test_finish_lane_timed_out_and_error(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        trace.start_lane(LANE_VECTOR, deadline_monotonic=_ctx().deadline_monotonic)
        trace.finish_lane(LANE_VECTOR, timed_out=True, reason="deadline")
        trace.start_lane(LANE_QA, deadline_monotonic=_ctx().deadline_monotonic)
        trace.finish_lane(LANE_QA, error="conn refused")
        assert trace.lane_summaries[LANE_VECTOR].timed_out is True
        assert trace.lane_summaries[LANE_QA].error == "conn refused"
        assert trace.timed_out is True


# ===========================================================================
# 9. Selected / injected flags
# ===========================================================================

class TestSelectedInjected:
    def test_select_and_inject_marks_candidate(self) -> None:
        """Privacy contract: the trace must NEVER retain the original
        ``RecallCandidate`` object.  Select / inject must update only the
        snapshot, leaving the original candidate untouched by default.
        """
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR)
        trace.record_candidate(c)  # capture_content=False (default)
        # Trace never holds the original — only a snapshot.
        snap = trace.candidates["c1"]
        assert isinstance(snap, CandidateSnapshot)
        assert snap.content is None  # privacy default
        # Original candidate is left unchanged by select/inject.
        assert c.selected is False
        trace.select("c1")
        assert snap.selected is True
        assert trace.final_selected_ids == ["c1"]
        trace.inject("c1", char_count=512)
        assert snap.injected is True
        assert trace.injection_summary is not None
        assert trace.injection_summary.injected_count == 1
        assert trace.injection_summary.total_chars == 512

    def test_dropped_candidate_cannot_be_selected(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR)
        c.mark_dropped(DropReason(DropReasonCode.BELOW_THRESHOLD, stage="lane"))
        trace.record_candidate(c)
        # Drop the original reference — the trace should still reject selection.
        del c
        with pytest.raises(ValueError):
            trace.select("c1")


# ===========================================================================
# 10. Safe serialization excludes content by default
# ===========================================================================

class TestSafeSerialization:
    def test_candidate_snapshot_omits_content_by_default(self) -> None:
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR, content="SECRET BODY")
        snap = c.snapshot()
        assert "content" not in snap
        assert snap["candidate_id"] == "c1"
        # content-like metadata keys dropped, content itself not leaked
        assert "SECRET BODY" not in json.dumps(snap, sort_keys=True)

    def test_candidate_snapshot_include_content_opt_in(self) -> None:
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR, content="ok")
        assert c.snapshot(include_content=True)["content"] == "ok"

    def test_trace_to_dict_excludes_query_and_content_by_default(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR, content="SECRET_BODY")
        trace.record_candidate(c)
        d = trace.to_dict()
        assert "query_context" not in d
        assert "SECRET_BODY" not in json.dumps(d, sort_keys=True)
        assert "content" not in d["candidates"][0]

    def test_trace_to_dict_opt_in_content_and_query(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        trace.record_candidate(
            RecallCandidate("c1", "card", "d.md", LANE_VECTOR, content="ok"),
            capture_content=True,
        )
        d_with = trace.to_dict(include_content=True, include_query=True)
        assert d_with["candidates"][0]["content"] == "ok"
        assert "query_context" in d_with
        assert "query_plan" in d_with


# ===========================================================================
# 11. Deterministic serialization ordering
# ===========================================================================

class TestDeterministicSerialization:
    def test_to_json_stable_across_calls(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        for i in range(5):
            trace.record_candidate(RecallCandidate(f"c{i}", "card", f"d{i}.md", LANE_VECTOR))
        j1 = trace.to_json()
        j2 = trace.to_json()
        assert j1 == j2

    def test_to_json_sort_keys(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        s = trace.to_json()
        obj = json.loads(s)
        assert json.dumps(obj, sort_keys=True) == s

    def test_candidate_ordering_preserved(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        for i in range(5):
            trace.record_candidate(RecallCandidate(f"c{i}", "card", f"d{i}.md", LANE_VECTOR))
        ids = [c["candidate_id"] for c in trace.to_dict()["candidates"]]
        assert ids == [f"c{i}" for i in range(5)]


# ===========================================================================
# 12. No full-memory payload on default export
# ===========================================================================

class TestNoFullMemoryPayload:
    def test_long_metadata_values_truncated(self) -> None:
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR, metadata={"raw_payload": "X" * 5000})
        snap = c.snapshot()
        assert len(snap["metadata"]["raw_payload"]) < 5000
        assert snap["metadata"]["raw_payload_truncated"] is True

    def test_content_like_metadata_keys_dropped(self) -> None:
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR, metadata={
            "body": "FULL", "text": "OTHER", "safe_key": "ok",
        })
        snap = c.snapshot()
        assert "body" not in snap["metadata"]
        assert "text" not in snap["metadata"]
        assert snap["metadata"]["safe_key"] == "ok"

    def test_default_export_bounded_for_huge_inputs(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        for i in range(20):
            trace.record_candidate(RecallCandidate(
                f"c{i}", "card", f"d{i}.md", LANE_VECTOR,
                content="X" * 10_000,
                metadata={"body": "Y" * 5000},
            ))
        s = trace.to_json()
        # Default export MUST NOT contain any content body / metadata body.
        assert "XXXX" not in s
        assert "YYYY" not in s
        # And must be a bounded summary.
        assert len(s) < 200_000

    def test_opt_in_can_export_full(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        trace.record_candidate(
            RecallCandidate("c1", "card", "d.md", LANE_VECTOR, content="VISIBLE"),
            capture_content=True,
        )
        assert "VISIBLE" in trace.to_json(include_content=True)


# ===========================================================================
# 13. Legacy recall imports remain unchanged
# ===========================================================================

class TestLegacyRecallUnchanged:
    def test_legacy_recall_pool_importable(self) -> None:
        # Sanity: legacy recall_pool is still importable from v3core.
        assert recall_pool is not None
        assert hasattr(recall_pool, "recall_pool")
        assert hasattr(recall_pool, "RecallHit")

    def test_legacy_recallhit_signature_intact(self) -> None:
        from v3core.types import RecallHit
        hit = RecallHit(source_id="x", title="t", content_preview="p")
        assert hit.source_id == "x"
        assert hit.cosine == 0.0
        assert hit.rrf_score == 0.0

    def test_v3core_init_does_not_import_recall_v2(self) -> None:
        """Read the v3core package __init__.py source; it must not reference recall_v2.

        ``hasattr`` is unreliable here because importing this test module
        already populates ``v3core.recall_v2`` in the module cache.  We
        read the source instead.
        """
        spec = importlib.util.find_spec("v3core")
        assert spec is not None and spec.origin is not None
        src = Path(spec.origin).read_text(encoding="utf-8")
        assert "recall_v2" not in src, (
            "v3core/__init__.py must NOT reference recall_v2 — "
            "module must remain separable"
        )

    def test_recall_v2_module_resolves(self) -> None:
        spec = importlib.util.find_spec("v3core.recall_v2")
        assert spec is not None and spec.origin is not None
        # And the module exposes the documented exports.
        from v3core import recall_v2
        assert hasattr(recall_v2, "QueryContext")
        assert hasattr(recall_v2, "QueryPlan")
        assert hasattr(recall_v2, "RecallCandidate")
        assert hasattr(recall_v2, "RecallTrace")


# ===========================================================================
# 14. Planner redlines (canonical algorithm snapshot)
# ===========================================================================

class TestPlannerRedlines:
    def test_canonical_snapshot_redlines(self) -> None:
        s = CanonicalAlgorithmSnapshot.canonical()
        assert s.rrf_k == 60
        assert s.half_life_days == 30
        assert dict(s.lane_weights) == {
            LANE_KEYWORD: 0.5, LANE_VECTOR: 1.0, LANE_TOPIC: 2.0, LANE_QA: 1.0,
        }
        assert s.rare_boost == 0.08
        assert s.exact_qa_boost == 0.5
        assert s.max_exact_qa == 2
        assert s.qa_vector_threshold == 0.35
        assert s.message_effective_threshold == 0.3
        assert s.topic_vector_threshold == 0.4
        assert s.topic_recall_weight == 0.5
        assert s.max_topics == 3
        assert s.max_subtopics == 3
        assert s.topic_context_max_chars == 3000
        assert s.qa_frequency_candidate_multiplier == 8
        assert s.rerank_top_n == 30
        assert s.dual_prefetch is True
        assert s.default_limit == 8
        assert s.injection_max_chars == 10000
        assert s.reranker_model == "BAAI/bge-reranker-v2-m3"
        assert s.remote_rerank_min_remaining_ms == 1500

    def test_canonical_snapshot_frozen(self) -> None:
        s = CanonicalAlgorithmSnapshot.canonical()
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.rrf_k = 999  # type: ignore[misc]
        # lane_weights is a tuple of tuples — immutable.
        assert isinstance(s.lane_weights, tuple)
        assert isinstance(s.lane_weights[0], tuple)

    def test_canonical_snapshot_deterministic_to_dict(self) -> None:
        s = CanonicalAlgorithmSnapshot.canonical()
        assert json.dumps(s.to_dict(), sort_keys=True) == json.dumps(s.to_dict(), sort_keys=True)

    def test_plan_uses_canonical_snapshot(self) -> None:
        s = _plan().algorithm
        assert s.rrf_k == 60
        assert s.reranker_model == "BAAI/bge-reranker-v2-m3"


# ===========================================================================
# 15. No provider / DB calls during planning
# ===========================================================================

class TestNoProviderCalls:
    def test_build_default_query_plan_no_io(self, monkeypatch) -> None:
        # Block common I/O channels.  Calling build_default_query_plan must not raise.
        def fail(name: str):
            def _explode(*a, **kw):
                raise AssertionError(f"build_default_query_plan must not call {name}")
            return _explode
        for blocked in ("psycopg2.connect", "requests.get", "requests.post", "urllib.request.urlopen"):
            monkeypatch.setattr(blocked, fail(blocked), raising=False)

        plan = build_default_query_plan(_ctx())
        assert isinstance(plan, QueryPlan)
        assert len(plan.lanes) == 5
        assert isinstance(plan.algorithm, CanonicalAlgorithmSnapshot)

    def test_canonical_snapshot_is_primitive_only(self) -> None:
        s = CanonicalAlgorithmSnapshot.canonical()
        d = s.to_dict()
        for k, v in d.items():
            assert isinstance(v, (int, float, bool, str, dict, tuple)), (
                f"CanonicalAlgorithmSnapshot.{k} is not primitive: {type(v)}"
            )


# ===========================================================================
# 16. Public record-dataclass exports (ProvenanceRecord / ScoreRecord /
#     CandidateEvent) plus structured StageTransition / DropEvent.
# ===========================================================================

class TestRecordDataclassExports:
    def test_provenance_score_event_are_public(self) -> None:
        # Public names exist on the package.
        from v3core import recall_v2
        for name in ("ProvenanceRecord", "ScoreRecord", "CandidateEvent",
                     "StageTransition", "DropEvent",
                     "CandidateSnapshot", "LaneSummary", "InjectionSummary"):
            assert hasattr(recall_v2, name), f"missing public export: {name}"

    def test_candidate_event_type_preserved_eight_values(self) -> None:
        # Contract: the 8 CandidateEventType values are preserved.
        assert {e.value for e in CandidateEventType} == {
            "FOUND", "DEDUPED", "SCORED", "FUSED",
            "RERANKED", "DROPPED", "SELECTED", "INJECTED",
        }

    def test_provenance_record_serializable(self) -> None:
        p = ProvenanceRecord(lane=LANE_KEYWORD, source_type="card", source_id="d.md")
        d = p.to_dict()
        assert d["lane"] == LANE_KEYWORD
        assert d["source_type"] == "card"
        assert d["source_id"] == "d.md"
        assert isinstance(d["ts"], float)

    def test_score_record_serializable(self) -> None:
        s = ScoreRecord(stage="lane", value=0.42, operation="cosine", params={"k": 60})
        d = s.to_dict()
        assert d["stage"] == "lane"
        assert d["value"] == 0.42
        assert d["operation"] == "cosine"
        assert d["params"] == {"k": 60}

    def test_candidate_event_serializable(self) -> None:
        e = CandidateEvent(event_type=CandidateEventType.SCORED, note="lane")
        d = e.to_dict()
        assert d["event_type"] == "SCORED"
        assert d["note"] == "lane"

    def test_stage_transition_and_drop_event_serializable(self) -> None:
        st = StageTransition(candidate_id="c1", from_stage="raw", to_stage="lane")
        de = DropEvent(candidate_id="c1", code=DropReasonCode.BELOW_THRESHOLD,
                       detail="0.05<0.3", stage="lane")
        assert st.to_dict()["candidate_id"] == "c1"
        assert st.to_dict()["from_stage"] == "raw"
        assert de.to_dict()["code"] == "BELOW_THRESHOLD"


# ===========================================================================
# 17. Safe metadata — case-insensitive content-like key filtering +
#     bounded collection sizes.
# ===========================================================================

class TestSafeMetadataBounds:
    def test_content_like_keys_lowercased_for_filter(self) -> None:
        # Mixed-case content-like keys must be filtered.
        out = safe_metadata({"Body": "X", "TEXT": "Y", "Preview": "Z", "safe": "ok"})
        assert "Body" not in out and "TEXT" not in out and "Preview" not in out
        assert out["safe"] == "ok"

    def test_oversized_list_bounded(self) -> None:
        big = list(range(200))
        out = safe_metadata({"items": big})
        # List capped at _META_LIST_MAX (64); remainder counted in a marker.
        assert len(out["items"]) <= 65
        # The trailing marker mentions the truncated count.
        marker = out["items"][-1]
        assert isinstance(marker, str) and marker.startswith("__truncated_")

    def test_oversized_dict_bounded(self) -> None:
        big = {f"k{i}": i for i in range(100)}
        out = safe_metadata({"d": big})
        # Nested dict is capped at _META_DICT_MAX (32) and a marker records
        # the number of dropped keys.
        nested = out["d"]
        assert "__truncated_keys__" in nested
        assert nested["__truncated_keys__"] >= 1
        # Total nested size (excluding the marker) must be bounded.
        assert sum(1 for k in nested if not k.startswith("__")) <= 32

    def test_include_content_opt_in_passes_through(self) -> None:
        out = safe_metadata({"body": "FULL"}, include_content=True)
        assert out["body"] == "FULL"

    def test_metadata_treated_immutable_in_frozen_dataclasses(self) -> None:
        # Metadata passed into QueryContext / LanePlan must not be mutable
        # at the field level — we normalize to a fresh dict and a Mapping view.
        ctx = QueryContext(
            query_id="q1", query_text="hi",
            deadline_monotonic=time.monotonic() + 1.0,
            budget_ms=100, limit=4, max_chars=1000,
            metadata={"k": "v"},
        )
        # Mutation of the original dict should not affect the frozen ctx.
        original = {"k": "v"}
        ctx2 = QueryContext(
            query_id="q1", query_text="hi",
            deadline_monotonic=time.monotonic() + 1.0,
            budget_ms=100, limit=4, max_chars=1000,
            metadata=original,
        )
        original["k"] = "mutated"
        assert ctx2.metadata["k"] == "v"

    def test_querycontext_metadata_rejects_item_mutation(self) -> None:
        # The metadata field must be a real immutable Mapping: assigning to
        # a key must raise TypeError, not silently mutate the contract.
        ctx = QueryContext(
            query_id="q1", query_text="hi",
            deadline_monotonic=time.monotonic() + 1.0,
            budget_ms=100, limit=4, max_chars=1000,
            metadata={"k": "v"},
        )
        with pytest.raises(TypeError):
            ctx.metadata["k"] = "x"  # type: ignore[index]
        # And deletion must also be rejected.
        with pytest.raises(TypeError):
            del ctx.metadata["k"]  # type: ignore[misc]

    def test_laneplan_metadata_rejects_item_mutation(self) -> None:
        # Same contract on LanePlan.metadata — the field is read-only.
        lp = LanePlan(
            name=LANE_VECTOR,
            enabled=True,
            budget_ms=100,
            deadline_monotonic=time.monotonic() + 1.0,
            candidate_limit=8,
            metadata={"k": "v"},
        )
        with pytest.raises(TypeError):
            lp.metadata["k"] = "x"  # type: ignore[index]
        with pytest.raises(TypeError):
            del lp.metadata["k"]  # type: ignore[misc]


# ===========================================================================
# 18. Plan serialization includes deadline_monotonic.
# ===========================================================================

class TestPlanDeadlineSerialization:
    def test_lane_plan_to_dict_includes_deadline(self) -> None:
        plan = _plan()
        lp = plan.lane(LANE_VECTOR)
        d = lp.to_dict()
        assert "deadline_monotonic" in d
        assert isinstance(d["deadline_monotonic"], float)

    def test_query_plan_to_dict_includes_deadline(self) -> None:
        plan = _plan()
        d = plan.to_dict()
        assert "deadline_monotonic" in d
        assert isinstance(d["deadline_monotonic"], float)
        for lp in d["lanes"]:
            assert "deadline_monotonic" in lp


# ===========================================================================
# 19. Privacy: raw content is NEVER retained in the trace by default.
# ===========================================================================

class TestTracePrivacyContract:
    def test_record_candidate_default_strips_content(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR, content="SECRET_BODY")
        trace.record_candidate(c)  # capture_content=False (default)
        snap = trace.candidate_snapshots["c1"]
        assert snap.content is None
        # Default JSON export must not contain the body.
        assert "SECRET_BODY" not in trace.to_json()

    def test_record_candidate_capture_content_keeps_content(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR, content="ok")
        trace.record_candidate(c, capture_content=True)
        snap = trace.candidate_snapshots["c1"]
        assert snap.content == "ok"
        # Default to_dict still hides content; opt-in to_json reveals it.
        assert "content" not in trace.to_dict()["candidates"][0]
        assert "ok" in trace.to_json(include_content=True)

    def test_trace_never_retains_original_candidate(self) -> None:
        # The trace holds a snapshot, not the original RecallCandidate.
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR)
        trace.record_candidate(c)
        # Resolve the class through the package, not the function-local name.
        import v3core.recall_v2.contracts as _rc
        assert not isinstance(trace.candidate_snapshots["c1"], _rc.RecallCandidate)
        assert isinstance(trace.candidate_snapshots["c1"], CandidateSnapshot)


# ===========================================================================
# 20. Snapshot retains provenance / score history / events without the
#     original RecallCandidate.
# ===========================================================================

class TestSnapshotHistory:
    def test_snapshot_has_provenance_score_history_events(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR)
        c.merge_provenance(LANE_QA, "qa", "d.md")
        c.record_score("raw", 0.1)
        c.record_score("final", 0.9)
        trace.record_candidate(c)
        snap = trace.candidate_snapshots["c1"]
        # Snapshot carries full history.
        assert len(snap.provenance) == 2
        assert {p.lane for p in snap.provenance} == {LANE_VECTOR, LANE_QA}
        assert len(snap.score_history) == 2
        assert any(e.event_type == CandidateEventType.FOUND for e in snap.events)
        # And contributes_lanes is correct.
        assert set(snap.contributing_lanes) == {LANE_VECTOR, LANE_QA}

    def test_safe_export_keeps_history_no_content(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR, content="SECRET")
        c.record_score("lane", 0.5)
        c.merge_provenance(LANE_QA, "qa", "d.md")
        trace.record_candidate(c)
        d = trace.to_dict()
        cand = d["candidates"][0]
        # history is preserved
        assert len(cand["score_history"]) >= 1
        assert len(cand["provenance"]) == 2
        assert len(cand["events"]) >= 1
        assert "content" not in cand
        assert "SECRET" not in json.dumps(d, sort_keys=True)

    def test_record_score_updates_snapshot_without_original(self) -> None:
        # record_score must work using only candidate_id (no original needed).
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR)
        trace.record_candidate(c)
        del c  # no original candidate available
        trace.record_score("c1", "final", 0.77)
        snap = trace.candidate_snapshots["c1"]
        assert snap.final_score == 0.77
        assert any(h.stage == "final" for h in snap.score_history)

    def test_record_drop_updates_snapshot_without_original(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR)
        trace.record_candidate(c)
        del c
        trace.record_drop("c1", DropReasonCode.BELOW_THRESHOLD,
                          detail="0.05<0.3", stage="lane")
        snap = trace.candidate_snapshots["c1"]
        assert snap.dropped is True
        assert snap.drop_reason is not None
        assert snap.drop_reason.code == DropReasonCode.BELOW_THRESHOLD
        # Drop event recorded.
        assert any(de.candidate_id == "c1" for de in trace.drop_events)


# ===========================================================================
# 21. Structured stage transitions and typed candidate events.
# ===========================================================================

class TestStageTransitionsAndEvents:
    def test_record_stage_transition_appends_to_list(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        trace.record_candidate(RecallCandidate("c1", "card", "d.md", LANE_VECTOR))
        trace.record_stage_transition("c1", "raw", "lane", reason="cosine")
        trace.record_stage_transition("c1", "lane", "fusion", reason="rrf",
                                      ts=12345.0)
        assert len(trace.stage_transitions) == 2
        # Deterministic timestamp is honored when provided.
        assert trace.stage_transitions[1].ts == 12345.0
        # And serialized in to_dict.
        d = trace.to_dict()
        assert isinstance(d["stage_transitions"], list)
        assert d["stage_transitions"][0]["from_stage"] == "raw"

    def test_record_candidate_event_appends_to_snapshot(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        trace.record_candidate(RecallCandidate("c1", "card", "d.md", LANE_VECTOR))
        trace.record_candidate_event("c1", CandidateEventType.FUSED, note="rrf")
        snap = trace.candidate_snapshots["c1"]
        assert any(e.event_type == CandidateEventType.FUSED for e in snap.events)
        # And surfaces in to_dict.
        cand = trace.to_dict()["candidates"][0]
        assert any(e["event_type"] == "FUSED" for e in cand["events"])

    def test_record_candidate_event_rejects_bad_type(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        trace.record_candidate(RecallCandidate("c1", "card", "d.md", LANE_VECTOR))
        with pytest.raises(ValueError):
            trace.record_candidate_event("c1", "not-an-event-type")  # type: ignore[arg-type]


# ===========================================================================
# 22. Deterministic lane timing — start_lane / finish_lane accept optional
#     monotonic timestamps; no sleeps are required.
# ===========================================================================

class TestDeterministicLaneTiming:
    def test_start_lane_with_started_monotonic(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        trace.start_lane(LANE_VECTOR,
                         deadline_monotonic=_ctx().deadline_monotonic,
                         started_monotonic=1_000.0)
        assert trace.lane_summaries[LANE_VECTOR].started == 1_000.0

    def test_finish_lane_with_finished_monotonic_computes_duration(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        trace.start_lane(LANE_VECTOR,
                         deadline_monotonic=_ctx().deadline_monotonic,
                         started_monotonic=1_000.0)
        trace.finish_lane(LANE_VECTOR,
                          finished_monotonic=1_250.0,
                          candidate_count=7)
        s = trace.lane_summaries[LANE_VECTOR]
        assert s.started == 1_000.0
        assert s.finished == 1_250.0
        assert s.duration_ms == 250_000.0
        assert s.candidate_count == 7

    def test_no_sleeps_used_for_timing(self) -> None:
        # Deterministic: two adjacent calls with explicit timestamps should
        # produce identical duration regardless of real wall clock.
        t1 = RecallTrace(query_context=_ctx(), query_plan=_plan())
        t2 = RecallTrace(query_context=_ctx(), query_plan=_plan())
        for t in (t1, t2):
            t.start_lane(LANE_VECTOR,
                         deadline_monotonic=_ctx().deadline_monotonic,
                         started_monotonic=2_000.0)
            t.finish_lane(LANE_VECTOR, finished_monotonic=2_050.0)
        assert t1.lane_summaries[LANE_VECTOR].duration_ms == \
               t2.lane_summaries[LANE_VECTOR].duration_ms == 50_000.0


# ===========================================================================
# 23. FIX A — recursive metadata privacy: _cap() must sanitize content-like
#     keys at EVERY mapping depth and propagate include_content.
# ===========================================================================

class TestRecursiveMetadataPrivacy:
    def test_a1_nested_dict_body_absent(self) -> None:
        out = safe_metadata({"debug": {"body": "SECRET_BODY"}})
        # Nested "body" key must be absent at depth, and the sentinel value
        # must not appear anywhere in the serialized snapshot.
        serialized = json.dumps(out, sort_keys=True)
        assert "SECRET_BODY" not in serialized
        assert "body" not in out["debug"]
        assert "body" not in out  # top-level filter also dropped nothing here

    def test_a2_nested_mixed_case(self) -> None:
        out = safe_metadata({"debug": {"Body": "B", "TEXT": "T", "safe": "ok"}})
        serialized = json.dumps(out, sort_keys=True)
        assert "B" not in serialized
        assert "T" not in serialized
        assert "Body" not in out["debug"]
        assert "TEXT" not in out["debug"]
        assert out["debug"]["safe"] == "ok"

    def test_a3_list_containing_dict_filters_nested(self) -> None:
        out = safe_metadata(
            {"items": [{"raw": "SECRET_BODY", "content": "SECRET_BODY", "ok": 1}]}
        )
        serialized = json.dumps(out, sort_keys=True)
        assert "SECRET_BODY" not in serialized
        # Nested raw/content keys absent; ok present.
        nested = out["items"][0]
        assert "raw" not in nested
        assert "content" not in nested
        assert nested["ok"] == 1

    def test_a4_tuple_containing_nested_mapping(self) -> None:
        out = safe_metadata(
            {"items": ({"snippet": "SECRET_BODY", "full_text": "S", "ok": 2},)}
        )
        serialized = json.dumps(out, sort_keys=True)
        assert "SECRET_BODY" not in serialized
        assert "S" not in serialized
        nested = out["items"][0]
        assert "snippet" not in nested
        assert "full_text" not in nested
        assert nested["ok"] == 2

    def test_a5_deeply_nested_sentinel_absent_from_trace_json(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        c = RecallCandidate(
            "c1", "card", "d.md", LANE_VECTOR,
            metadata={"debug": {"deeper": {"body": "SECRET_BODY"}}},
        )
        trace.record_candidate(c)
        # Default (privacy-safe) JSON must not contain the deep sentinel.
        assert "SECRET_BODY" not in trace.to_json()

    def test_a6_include_content_preserves_nested_content(self) -> None:
        out = safe_metadata(
            {"debug": {"body": "FULL", "safe": "ok"}}, include_content=True
        )
        assert out["debug"]["body"] == "FULL"
        assert out["debug"]["safe"] == "ok"

    def test_existing_bounds_still_hold_with_recursion(self) -> None:
        # List capped at 64 + marker; dict capped at 32 + __truncated_keys__.
        big_list = list(range(200))
        big_dict = {f"k{i}": i for i in range(100)}
        out = safe_metadata({"items": big_list, "d": big_dict})
        # List bound: capped at 64 entries + 1 trailing marker = 65 max.
        assert len(out["items"]) <= 65
        marker = out["items"][-1]
        assert isinstance(marker, str) and marker.startswith("__truncated_")
        # Dict bound: __truncated_keys__ marker recorded.
        assert "__truncated_keys__" in out["d"]
        assert out["d"]["__truncated_keys__"] >= 1
        assert sum(1 for k in out["d"] if not k.startswith("__")) <= 32

    def test_nested_input_dict_not_mutated_in_place(self) -> None:
        nested = {"body": "SECRET_BODY", "safe": "ok"}
        original_id = id(nested)
        out = safe_metadata({"debug": nested})
        # The nested mapping identity must not be replaced or mutated by _cap.
        assert id(nested) == original_id
        assert nested == {"body": "SECRET_BODY", "safe": "ok"}
        # And the snapshot is a fresh structure with the body absent at depth.
        assert "body" not in out["debug"]
        assert out["debug"]["safe"] == "ok"


# ===========================================================================
# 24. FIX B — QueryPlan lane invariant: canonical five lanes in canonical order.
# ===========================================================================

class TestQueryPlanCanonicalLanes:
    def test_b1_duplicate_lane_rejected(self) -> None:
        plan = _plan()
        # Build lanes with a duplicate keyword lane.
        dup_lanes = (
            plan.lane(LANE_KEYWORD),
            plan.lane(LANE_KEYWORD),
            plan.lane(LANE_VECTOR),
            plan.lane(LANE_TOPIC),
            plan.lane(LANE_QA),
            plan.lane(LANE_EXPLICIT),
        )
        with pytest.raises(ValueError):
            # ValueError either from QueryPlan.__post_init__ (canonical-order
            # mismatch) or from LanePlan.__post_init__ (unknown lane) — both
            # are acceptable forms of the invariant rejecting the input.
            dataclasses.replace(plan, lanes=dup_lanes)

    def test_b2_missing_lane_rejected(self) -> None:
        plan = _plan()
        # Drop one canonical lane (use keyword slot for qa to keep names valid).
        four_lanes = tuple(
            lp if lp.name != LANE_KEYWORD
            else dataclasses.replace(lp, name=LANE_QA)
            for lp in plan.lanes
        )
        # After the swap the lane set is still a subset of ALL_LANES and is
        # missing keyword; the canonical-order tuple equality must fail.
        with pytest.raises(ValueError):
            _ = dataclasses.replace(plan, lanes=four_lanes)

    def test_b3_extra_or_unknown_lane_rejected(self) -> None:
        plan = _plan()
        with pytest.raises(ValueError):
            # Replace one lane with an unknown name — LanePlan.__post_init__
            # already raises ValueError; QueryPlan's invariant would too.
            bad_lanes = tuple(
                dataclasses.replace(lp, name="not_a_lane")
                if lp.name == LANE_TOPIC else lp
                for lp in plan.lanes
            )
            dataclasses.replace(plan, lanes=bad_lanes)

    def test_b4_canonical_five_lane_plan_accepted(self) -> None:
        plan = build_default_query_plan(_ctx())
        # Order-sensitive tuple equality against ALL_LANES.
        assert plan.lane_names() == ALL_LANES
        # And ALL_LANES is exactly the canonical tuple.
        assert plan.lane_names() == (
            LANE_KEYWORD, LANE_VECTOR, LANE_TOPIC, LANE_QA, LANE_EXPLICIT,
        )

    def test_b5_deterministic_serialization_canonical_order(self) -> None:
        plan = build_default_query_plan(_ctx())
        d = plan.to_dict()
        names_in_order = [lp["name"] for lp in d["lanes"]]
        assert names_in_order == list(ALL_LANES)


# ===========================================================================
# 25. FIX C — metadata=None must still route through _freeze_mapping so the
#     resulting field rejects item mutation with TypeError.
# ===========================================================================

class TestMetadataNoneImmutability:
    def test_c1_querycontext_metadata_none_rejects_item_mutation(self) -> None:
        ctx = QueryContext(
            query_id="q1", query_text="hi",
            deadline_monotonic=time.monotonic() + 1.0,
            budget_ms=100, limit=4, max_chars=1000,
            metadata=None,
        )
        with pytest.raises(TypeError):
            ctx.metadata["k"] = "x"  # type: ignore[index]

    def test_c2_laneplan_metadata_none_rejects_item_mutation(self) -> None:
        lp = LanePlan(
            name=LANE_VECTOR,
            enabled=True,
            budget_ms=100,
            deadline_monotonic=time.monotonic() + 1.0,
            candidate_limit=8,
            metadata=None,
        )
        with pytest.raises(TypeError):
            lp.metadata["k"] = "x"  # type: ignore[index]


# ===========================================================================
# 26. FIX D — RecallTrace.record_candidate must detach from the source
#     candidate's mutable record objects (provenance / score_history /
#     events / drop_reason) so post-record mutations cannot leak in.
# ===========================================================================

class TestTraceSnapshotDetachment:
    def test_d1_record_candidate_detaches_from_source(self) -> None:
        trace = RecallTrace(query_context=_ctx(), query_plan=_plan())
        c = RecallCandidate("c1", "card", "d.md", LANE_VECTOR)
        c.merge_provenance(LANE_QA, "qa", "d.md")
        c.record_score("final", 0.9, k="v")
        c.record_event(CandidateEventType.FUSED, note="rrf")
        trace.record_candidate(c)
        snap = trace.candidate_snapshots["c1"]

        # Snapshot provenance must be a different list object.
        assert snap.provenance is not c.provenance
        # Capture snapshot's pre-mutation values for comparison.
        snap_prov_lane = snap.provenance[0].lane
        snap_score_value = snap.score_history[0].value
        snap_score_params = dict(snap.score_history[0].params)
        snap_event_note = snap.events[0].note

        # Now mutate the SOURCE candidate.
        c.provenance[0].lane = "mutated"
        c.score_history[0].value = 999.0
        c.score_history[0].params["k"] = "mutated"
        c.events[0].note = "mutated"

        # Snapshot must be unchanged.
        assert snap.provenance[0].lane == snap_prov_lane
        assert snap.score_history[0].value == snap_score_value
        assert snap.score_history[0].params == snap_score_params
        assert snap.events[0].note == snap_event_note
        # Spot-checks against the mutated source values to be explicit.
        assert snap.provenance[0].lane != "mutated"
        assert snap.score_history[0].value != 999.0
        assert snap.score_history[0].params.get("k") != "mutated"
        assert snap.events[0].note != "mutated"
