# -*- coding: utf-8 -*-
"""P0-B4-A — recall trace diagnostics regression tests.

The P0-B4 lane-count investigation needs to tell four failure modes apart
from the trace alone:

* **admission miss**  — a retrieval lane never returned the candidate;
* **fusion miss**     — the candidate was returned but its fused score
  ranked it too low;
* **rerank miss**     — the candidate entered rerank but got a low score;
* **final-limit miss**— the candidate survived fusion/rerank but landed
  outside the returned ``limit``.

To do that the trace must carry, per candidate: its wire ``kind``; per
retrieval lane its ``present`` flag, raw rank, raw score and additive RRF
contribution (plus the lane vote count); its pre-/post-fusion positions;
its rerank input position, the provider's returned ``index`` and rerank
score; and its final position.

These tests pin that contract.  They are RED against the pre-change
revision (the diagnostic fields and the ``LegacySink`` diagnostic protocol
do not exist there) and GREEN once the additive instrumentation lands.
Nothing here asserts a weight / ordering / algorithm change — the
instrumentation is strictly observability.

The integration test drives the real ``recall_pool`` with the same
in-process stub harness used by ``test_recall_v2_injection_trace.py``
(PG leases, providers and the non-keyword lanes are stubbed); no PG,
network or provider is touched.
"""
from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest


# Make the in-tree ``src`` importable regardless of how pytest was launched.
REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))


from v3core import recall_pool  # noqa: E402
from v3core.recall_v2 import (  # noqa: E402
    CandidateSnapshot,
    LaneEvidence,
    QueryContext,
    RecallTrace,
    build_default_query_plan,
)
from v3core.recall_v2.engine import LegacySink  # noqa: E402


# ===========================================================================
# Helpers
# ===========================================================================


def _unit_trace(candidate_ids: tuple[str, ...] = ("c1",)) -> RecallTrace:
    """Build a ``RecallTrace`` with a snapshot per requested candidate id.

    Pure stdlib — no PG / providers / facade.
    """
    ctx = QueryContext(
        query_id="q-b4a-unit", query_text="hello",
        deadline_monotonic=time.monotonic() + 5.0,
        budget_ms=1000, limit=4, max_chars=2000,
    )
    plan = build_default_query_plan(ctx)
    trace = RecallTrace(query_context=ctx, query_plan=plan)
    for cid in candidate_ids:
        trace.candidate_snapshots[cid] = CandidateSnapshot(
            candidate_id=cid, lane="keyword",
            source_type="qa", source_id=cid,
        )
    return trace


# ===========================================================================
# 1. Trace-level recorders
# ===========================================================================


class TestTraceLaneEvidence:
    def test_multi_lane_evidence_fields_and_vote_count(self) -> None:
        trace = _unit_trace(("c1",))
        trace.record_lane_evidence(
            "c1", "keyword", raw_rank=2, raw_score=0.5,
            rrf_contribution=0.5 / 62,
        )
        trace.record_lane_evidence(
            "c1", "qa", raw_rank=1, raw_score=0.9,
            rrf_contribution=1.0 / 61,
        )
        trace.record_lane_evidence(
            "c1", "vector", raw_rank=3, raw_score=0.7,
            rrf_contribution=1.0 / 63,
        )
        snap = trace.candidate_snapshots["c1"]

        assert snap.lane_vote_count == 3, (
            f"lane_vote_count={snap.lane_vote_count}, expected 3 distinct lanes"
        )
        kw = snap.lane_evidence_for("keyword")
        assert kw is not None and kw.present is True
        assert kw.raw_rank == 2
        assert kw.raw_score == pytest.approx(0.5)
        assert kw.rrf_contribution == pytest.approx(0.5 / 62)

        qa = snap.lane_evidence_for("qa")
        assert qa is not None and qa.present is True
        assert qa.raw_rank == 1
        assert qa.raw_score == pytest.approx(0.9)
        assert qa.rrf_contribution == pytest.approx(1.0 / 61)

        vec = snap.lane_evidence_for("vector")
        assert vec is not None and vec.present is True
        assert vec.raw_rank == 3

        # Serialized additively.
        d = snap.to_dict()
        assert d["lane_vote_count"] == 3
        by_lane = {ev["lane"]: ev for ev in d["lane_evidence"]}
        assert set(by_lane) == {"keyword", "qa", "vector"}
        assert by_lane["keyword"]["raw_rank"] == 2
        assert by_lane["qa"]["rrf_contribution"] == pytest.approx(1.0 / 61)
        # Explicit presence over the five canonical lanes: the two lanes the
        # candidate never reached are reported absent (admission miss).
        presence = d["lane_presence"]
        assert presence == {
            "keyword": True, "vector": True, "topic": False,
            "qa": True, "explicit": False,
        }, f"unexpected lane presence map: {presence!r}"
        assert snap.lane_presence() == presence

    def test_distinct_rrf_subpaths_remain_distinct_votes(self) -> None:
        """QA keyword and vector are separate RRF votes, not one aggregate."""
        trace = _unit_trace(("c1", "topic1"))
        trace.record_lane_evidence(
            "c1", "qa", path="qa_keyword", raw_rank=5, raw_score=0.4,
            rrf_contribution=1.0 / 65,
        )
        trace.record_lane_evidence(
            "c1", "qa", path="qa_vector", raw_rank=2, raw_score=0.8,
            rrf_contribution=1.0 / 62,
        )
        trace.record_lane_evidence(
            "c1", "explicit", path="explicit_keyword", raw_rank=1,
            raw_score=None, rrf_contribution=0.0,
        )
        trace.record_lane_evidence(
            "topic1", "keyword", path="keyword", raw_rank=3,
            raw_score=0.2, rrf_contribution=0.5 / 63,
        )
        trace.record_lane_evidence(
            "topic1", "vector", path="vector", raw_rank=1,
            raw_score=0.9, rrf_contribution=1.0 / 61,
        )
        trace.record_lane_evidence(
            "topic1", "topic", path="topic_vector", raw_rank=2,
            raw_score=0.8, rrf_contribution=2.0 / 62,
        )
        snap = trace.candidate_snapshots["c1"]
        qa_entries = [e for e in snap.lane_evidence if e.lane == "qa"]
        assert len(qa_entries) == 2
        assert snap.lane_vote_count == 2  # explicit provenance is not an RRF vote
        qa_kw = snap.lane_evidence_for("qa", "qa_keyword")
        qa_vec = snap.lane_evidence_for("qa", "qa_vector")
        assert qa_kw is not None and qa_kw.raw_rank == 5
        assert qa_kw.raw_score == pytest.approx(0.4)
        assert qa_kw.rrf_contribution == pytest.approx(1.0 / 65)
        assert qa_vec is not None and qa_vec.raw_rank == 2
        assert qa_vec.raw_score == pytest.approx(0.8)
        assert qa_vec.rrf_contribution == pytest.approx(1.0 / 62)
        assert snap.lane_presence()["qa"] is True
        assert snap.lane_presence()["explicit"] is True
        assert snap.to_dict()["lane_vote_count"] == 2
        topic_snap = trace.candidate_snapshots["topic1"]
        topic_paths = {ev.path for ev in topic_snap.lane_evidence if ev.present}
        assert topic_paths == {"keyword", "vector", "topic_vector"}
        assert topic_snap.lane_vote_count == 3

    def test_absent_lane_present_false_not_counted(self) -> None:
        trace = _unit_trace(("c1",))
        trace.record_lane_evidence("c1", "topic", present=False)
        snap = trace.candidate_snapshots["c1"]
        ev = snap.lane_evidence_for("topic")
        assert ev is not None and ev.present is False
        assert snap.lane_vote_count == 0, (
            "a lane recorded as absent must not count toward the vote count"
        )

    def test_malformed_numeric_inputs_are_ignored(self) -> None:
        trace = _unit_trace(("c1",))
        trace.record_lane_evidence(
            "c1", "keyword", raw_rank="not-an-int",
            raw_score=object(), rrf_contribution="nope",
        )
        ev = trace.candidate_snapshots["c1"].lane_evidence_for("keyword")
        assert ev is not None
        assert ev.raw_rank is None
        assert ev.raw_score is None
        assert ev.rrf_contribution is None

    def test_unknown_candidate_raises_keyerror(self) -> None:
        trace = _unit_trace(("c1",))
        with pytest.raises(KeyError):
            trace.record_lane_evidence("missing", "keyword", raw_rank=1)


class TestTraceKindAndPositions:
    def test_kind_recorded_and_serialized(self) -> None:
        trace = _unit_trace(("c1",))
        trace.record_kind("c1", "qa")
        snap = trace.candidate_snapshots["c1"]
        assert snap.kind == "qa"
        assert snap.to_dict()["kind"] == "qa"

    def test_fusion_positions_recorded_and_serialized(self) -> None:
        trace = _unit_trace(("c1",))
        trace.record_fusion_position(
            "c1", pre_fusion_position=5, post_fusion_position=2,
        )
        snap = trace.candidate_snapshots["c1"]
        assert snap.pre_fusion_position == 5
        assert snap.post_fusion_position == 2
        d = snap.to_dict()
        assert d["pre_fusion_position"] == 5
        assert d["post_fusion_position"] == 2

    def test_final_position_recorded_and_serialized(self) -> None:
        trace = _unit_trace(("c1",))
        trace.record_final_position("c1", 3)
        snap = trace.candidate_snapshots["c1"]
        assert snap.final_position == 3
        assert snap.to_dict()["final_position"] == 3

    def test_rerank_evidence_records_all_three_fields(self) -> None:
        trace = _unit_trace(("c1",))
        trace.record_rerank_evidence(
            "c1", input_position=4, provider_index=1, score=0.42,
        )
        snap = trace.candidate_snapshots["c1"]
        assert snap.rerank_input_position == 4
        assert snap.rerank_provider_index == 1
        assert snap.rerank_score == pytest.approx(0.42)
        d = snap.to_dict()
        assert d["rerank_input_position"] == 4
        assert d["rerank_provider_index"] == 1
        assert d["scores"]["rerank_score"] == pytest.approx(0.42)

    def test_to_json_deterministic_with_diagnostics(self) -> None:
        trace = _unit_trace(("c1", "c2"))
        trace.record_lane_evidence("c1", "keyword", raw_rank=1, raw_score=0.5)
        trace.record_final_position("c1", 1)
        j1 = trace.to_json()
        j2 = trace.to_json()
        assert j1 == j2
        obj = json.loads(j1)
        assert json.dumps(obj, sort_keys=True) == j1
        cand = obj["candidates"][0]
        assert cand["lane_evidence"][0]["lane"] == "keyword"


# ===========================================================================
# 2. Sink-level protocol (duck-typed diagnostics)
# ===========================================================================


class TestLegacySinkDiagnostics:
    def test_lane_evidence_via_sink(self) -> None:
        trace = _unit_trace(("c1",))
        sink = LegacySink(trace)
        sink.lane_evidence(
            "c1", "keyword", present=True, raw_rank=2,
            raw_score=0.5, rrf_contribution=0.5 / 62,
        )
        sink.lane_evidence(
            "c1", "qa", present=True, raw_rank=1,
            raw_score=0.9, rrf_contribution=1.0 / 61,
        )
        snap = trace.candidate_snapshots["c1"]
        assert snap.lane_vote_count == 2
        assert snap.lane_evidence_for("keyword").raw_rank == 2
        assert snap.lane_evidence_for("qa").raw_rank == 1

    def test_sink_rejects_unknown_lane_silently(self) -> None:
        trace = _unit_trace(("c1",))
        sink = LegacySink(trace)
        sink.lane_evidence("c1", "not-a-lane", raw_rank=1)
        snap = trace.candidate_snapshots["c1"]
        assert snap.lane_evidence == [], (
            "an unknown lane must be ignored, not recorded"
        )

    def test_sink_creates_snapshot_for_unknown_id_without_raising(self) -> None:
        trace = _unit_trace(())
        sink = LegacySink(trace)
        # Every diagnostic method must be exception-safe: it may lazily
        # create the snapshot (like score/drop/select already do) but must
        # never raise into the legacy caller.
        sink.candidate_kind("new", "qa")
        sink.lane_evidence("new", "keyword", raw_rank=1, rrf_contribution=1.0 / 61)
        sink.fusion_position("new", pre_fusion_position=1, post_fusion_position=1)
        sink.rerank_evidence("new", input_position=1, provider_index=0, score=0.5)
        sink.final_position("new", 1)
        snap = trace.candidate_snapshots.get("new")
        assert snap is not None
        assert snap.kind == "qa"
        assert snap.lane_vote_count == 1
        assert snap.post_fusion_position == 1
        assert snap.rerank_provider_index == 0
        assert snap.final_position == 1

    def test_rerank_evidence_maps_out_of_order_provider_index(self) -> None:
        """The rerank provider returns its results ranked, not
        position-aligned; each score is written back by the provider's own
        ``index``.  The trace must keep the *input position* and the
        *provider index* as independent fields and never conflate them.

        Here the provider's index order (0, 2, 1) is a permutation that
        differs from the input order (3, 1, 2) — a naive positional zip
        would mis-map every score.
        """
        trace = _unit_trace(("cA", "cB", "cC"))
        sink = LegacySink(trace)
        sink.rerank_evidence("cA", input_position=3, provider_index=0, score=0.91)
        sink.rerank_evidence("cB", input_position=1, provider_index=2, score=0.13)
        sink.rerank_evidence("cC", input_position=2, provider_index=1, score=0.55)

        a = trace.candidate_snapshots["cA"]
        b = trace.candidate_snapshots["cB"]
        c = trace.candidate_snapshots["cC"]
        assert (a.rerank_input_position, a.rerank_provider_index,
                a.rerank_score) == (3, 0, 0.91)
        assert (b.rerank_input_position, b.rerank_provider_index,
                b.rerank_score) == (1, 2, 0.13)
        assert (c.rerank_input_position, c.rerank_provider_index,
                c.rerank_score) == (2, 1, 0.55)

    def test_rerank_evidence_via_trace_direct_keeps_fields_separate(self) -> None:
        trace = _unit_trace(("c1",))
        trace.record_rerank_evidence(
            "c1", input_position=2, provider_index=0, score=0.9,
        )
        snap = trace.candidate_snapshots["c1"]
        assert snap.rerank_input_position == 2
        assert snap.rerank_provider_index == 0, (
            "provider_index must be recorded verbatim, not derived from the "
            "input position"
        )


# ===========================================================================
# 3. recall_pool instrumentation (in-process stub harness)
# ===========================================================================


class _StubCursor:
    def execute(self, sql: str, params: Any = None) -> None:
        return None

    def fetchall(self) -> list[tuple]:
        return []

    def fetchone(self) -> tuple | None:
        return None

    def mogrify(self, sql: str, params: Any = None) -> bytes:
        return b""

    def __enter__(self) -> "_StubCursor":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def close(self) -> None:
        return None


class _StubPgConn:
    def cursor(self) -> _StubCursor:
        return _StubCursor()

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> "_StubPgConn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None


class _StubPg:
    def __init__(self) -> None:
        from v3core.pg_pool import PgPool

        def _factory() -> None:
            raise RuntimeError(
                "_StubPg._factory should never be called — the harness "
                "replaces _lease_pg_connection"
            )

        self.pool = PgPool(_factory, max_connections=3, min_connections=0)

    def is_connected(self) -> bool:
        return True

    def lease(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError(
            "_StubPg.lease should never be called — the harness replaces "
            "_lease_pg_connection"
        )


def _disable_optional_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every retrieval seam that touches PG / providers / network."""

    @contextlib.contextmanager
    def _lease(pg: Any, timeout: Any = None, deadline: Any = None) -> Any:
        yield _StubPgConn()

    monkeypatch.setattr(recall_pool, "_lease_pg_connection", _lease, raising=False)
    monkeypatch.setattr(
        recall_pool, "_maybe_expand_query",
        lambda query, *a, **k: query, raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_combined_qa_keyword_lookup_parallel",
        lambda *a, **k: ({}, [[] for _ in (k.get("terms") or [])]), raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_qa_keyword_cache_lookup",
        lambda *a, **k: ({}, [[] for _ in (k.get("terms") or [])]), raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_qa_snapshot_load", lambda *a, **k: None, raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_qa_snapshot_lookup", lambda *a, **k: ({}, []), raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_topic_snapshot_load", lambda *a, **k: None, raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_topic_snapshot_lookup", lambda *a, **k: ([], []), raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_fetch_facts", lambda *a, **k: {}, raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_active_memory_reader_for", lambda *a, **k: None, raising=False,
    )


def _run_keyword_only_recall(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: list[tuple],
    limit: int = 2,
) -> tuple[list, RecallTrace]:
    """Drive ``recall_pool`` with only the QA-keyword lane live.

    Returns the raw hits plus the ``RecallTrace`` the sink wrote into.
    Mirrors the proven stub harness of ``test_recall_v2_injection_trace.py``.
    """
    _disable_optional_lanes(monkeypatch)

    def _fake_combined_lookup(*a: Any, **k: Any) -> tuple[dict, list]:
        return ({"needle": 1}, [list(rows)])

    monkeypatch.setattr(
        recall_pool, "_combined_qa_keyword_lookup",
        _fake_combined_lookup, raising=False,
    )

    trace = _unit_trace(())
    sink = LegacySink(trace)
    hits, _pg_fail = recall_pool.recall_pool(
        "needle",
        card_index={},
        pg=_StubPg(),
        q_emb=None,
        include_keyword=True,
        include_card_vector=False,
        include_message_vector=False,
        include_effective=False,
        include_topic=False,
        include_yin=False,
        include_notes=False,
        rerank_top_n=10,
        rerank_cfg=None,
        limit=limit,
        pg_was_connected=False,
        config={
            "recall": {"qa_per_term_min": 10, "qa_freq_limit": 40},
            "half_life": 30,
        },
        trace=sink,
    )
    return list(hits or []), trace


class TestRecallPoolDiagnosticProbes:
    def test_recall_pool_emits_diagnostic_probes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rows = [
            (
                2100 + i,
                "2026-09-14T10:00:00+00:00",
                f"q-{i} needle",
                f"a-{i} " + ("needle " * 8),
            )
            for i in range(4)
        ]
        hits, trace = _run_keyword_only_recall(monkeypatch, rows=rows, limit=2)

        expected_ids = {f"qa_{2100 + i}" for i in range(4)}
        got_ids = {str(getattr(h, "source_id", "")) for h in hits}
        # Sanity: the harness returned ``limit`` QA hits drawn from the fed rows.
        assert len(got_ids) == 2, f"expected 2 returned hits, got {got_ids!r}"
        assert got_ids <= expected_ids, (
            f"unexpected returned hit ids {got_ids - expected_ids!r}"
        )
        snaps = trace.candidate_snapshots
        assert set(snaps) == expected_ids, (
            f"trace snapshots {sorted(snaps)} != fed ids {sorted(expected_ids)}"
        )

        for cid, snap in snaps.items():
            # (a) candidate id + kind.
            assert snap.candidate_id == cid
            assert snap.kind == "qa", (
                f"{cid}: kind={snap.kind!r}, expected the legacy wire kind 'qa'"
            )
            # (b) multi-lane evidence: the QA keyword hits are ranked by both
            #     the keyword lane and the qa lane.
            lanes = {ev.lane for ev in snap.lane_evidence if ev.present}
            assert {"keyword", "qa"} <= lanes, (
                f"{cid}: lane evidence {sorted(lanes)} missing keyword/qa"
            )
            assert len(lanes) >= 2
            paths = {(ev.lane, ev.path) for ev in snap.lane_evidence if ev.present}
            assert {("keyword", "keyword"), ("qa", "qa_keyword")} <= paths, (
                f"{cid}: physical RRF paths {sorted(paths)} missing keyword/QA votes"
            )
            assert snap.lane_vote_count >= 2
            assert snap.lane_vote_count == sum(
                1 for ev in snap.lane_evidence
                if ev.present and ev.rrf_contribution is not None
                and ev.rrf_contribution > 0
            )

            presence = snap.lane_presence()
            assert presence["keyword"] is True and presence["qa"] is True
            assert presence["topic"] is False, (
                f"{cid}: topic lane must be reported absent (admission miss)"
            )
            for ev in snap.lane_evidence:
                if not ev.present:
                    continue
                assert ev.raw_rank is not None and ev.raw_rank >= 1
                assert ev.rrf_contribution is not None and ev.rrf_contribution > 0
            # (c) pre-/post-fusion positions present.
            assert snap.pre_fusion_position is not None
            assert snap.post_fusion_position is not None

        post = sorted(s.post_fusion_position for s in snaps.values())
        assert post == [1, 2, 3, 4], (
            f"post_fusion_position values {post!r} are not a 1..4 permutation"
        )

        # (d) final positions: exactly the returned hits, contiguous 1..limit.
        final_positions = {
            cid: s.final_position for cid, s in snaps.items()
            if s.final_position is not None
        }
        assert len(final_positions) == 2, (
            f"expected 2 hits with a final position, got {final_positions!r}"
        )
        assert sorted(final_positions.values()) == [1, 2]
        for h in hits:
            sid = str(getattr(h, "source_id", ""))
            assert snaps[sid].final_position is not None, (
                f"returned hit {sid!r} has no final_position on the trace"
            )
            assert snaps[sid].fusion_score == pytest.approx(h.rrf_score), (
                f"{sid}: trace fusion_score does not match the score used for ranking"
            )

        # (e) the two hits outside the limit were dropped OUTSIDE_LIMIT and
        #     carry no final position — the final-limit-miss signal.
        outside = [
            cid for cid, s in snaps.items() if s.final_position is None
        ]
        assert len(outside) == 2
        outside_drops = {
            de.candidate_id for de in trace.drop_events
            if getattr(de, "code", None) is not None
            and getattr(de.code, "value", None) == "OUTSIDE_LIMIT"
        }
        assert set(outside) == outside_drops, (
            f"outside-limit ids {sorted(outside)} != OUTSIDE_LIMIT drops "
            f"{sorted(outside_drops)}"
        )

    def test_recall_pool_diagnostics_do_not_change_hits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attaching a trace must not change the returned hits or their order.

        The diagnostic probes are read-only; the same run with ``trace=None``
        must return the identical id sequence.
        """
        rows = [
            (
                2200 + i,
                "2026-09-14T10:00:00+00:00",
                f"q-{i} needle",
                f"a-{i} " + ("needle " * 8),
            )
            for i in range(5)
        ]
        traced_hits, _trace = _run_keyword_only_recall(
            monkeypatch, rows=rows, limit=3,
        )
        traced_ids = [str(getattr(h, "source_id", "")) for h in traced_hits]

        # Re-run the identical scenario with NO trace attached.
        _disable_optional_lanes(monkeypatch)

        def _fake_combined_lookup(*a: Any, **k: Any) -> tuple[dict, list]:
            return ({"needle": 1}, [list(rows)])

        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup",
            _fake_combined_lookup, raising=False,
        )
        untraced_hits, _pg_fail = recall_pool.recall_pool(
            "needle",
            card_index={},
            pg=_StubPg(),
            q_emb=None,
            include_keyword=True,
            include_card_vector=False,
            include_message_vector=False,
            include_effective=False,
            include_topic=False,
            include_yin=False,
            include_notes=False,
            rerank_top_n=10,
            rerank_cfg=None,
            limit=3,
            pg_was_connected=False,
            config={
                "recall": {"qa_per_term_min": 10, "qa_freq_limit": 40},
                "half_life": 30,
            },
        )
        untraced_ids = [str(getattr(h, "source_id", "")) for h in untraced_hits]

        assert traced_ids == untraced_ids, (
            f"trace attachment changed the returned hits: "
            f"{traced_ids!r} != {untraced_ids!r}"
        )


def test_rerank_callback_reports_validated_provider_indices_in_response_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture the provider's actual ``index`` values, not inferred positions."""
    from v3core import rerank as rerank_module

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            # Ranked response order is deliberately 2, 0, 1. The API client
            # must still return an input-aligned score vector.
            return {
                "results": [
                    {"index": 2, "relevance_score": 0.33},
                    {"index": 0, "relevance_score": 0.11},
                    {"index": 1, "relevance_score": 0.22},
                ]
            }

    monkeypatch.setattr(rerank_module.requests, "post", lambda *a, **k: _Response())
    observed: list[tuple[int, float]] = []
    scores = rerank_module.rerank(
        "q", ["doc-0", "doc-1", "doc-2"], {"endpoint": "https://example.invalid"},
        on_result=lambda index, score: observed.append((index, score)),
    )

    assert scores == pytest.approx([0.11, 0.22, 0.33])
    assert observed == [(2, 0.33), (0, 0.11), (1, 0.22)]


def test_recall_pool_does_not_report_a_provider_score_on_rerank_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prepared rerank input is not evidence that the provider scored it."""
    _disable_optional_lanes(monkeypatch)
    rows = [
        (
            2300 + i,
            "2026-09-14T10:00:00+00:00",
            f"q-{i} needle",
            f"a-{i} " + ("needle " * 8),
        )
        for i in range(4)
    ]
    monkeypatch.setattr(
        recall_pool,
        "_combined_qa_keyword_lookup",
        lambda *a, **k: ({"needle": 1}, [list(rows)]),
        raising=False,
    )

    def _provider_fallback(
        query: str,
        hits: list,
        top_n: int = 20,
        rerank_cfg: dict | None = None,
        *,
        deadline: Any = None,
        on_input: Any = None,
        on_result: Any = None,
    ) -> list:
        for pos, hit in enumerate(hits[:top_n], start=1):
            if on_input is not None:
                on_input(pos, hit)
        # Model the existing timeout/error fallback: return RRF order and do
        # not call on_result because no validated provider response exists.
        return hits

    monkeypatch.setattr(recall_pool, "_rerank", _provider_fallback)
    trace = _unit_trace(())
    hits, _pg_fail = recall_pool.recall_pool(
        "needle",
        card_index={},
        pg=_StubPg(),
        q_emb=None,
        include_keyword=True,
        include_card_vector=False,
        include_message_vector=False,
        include_effective=False,
        include_topic=False,
        include_yin=False,
        include_notes=False,
        rerank_top_n=2,
        rerank_cfg={"endpoint": "https://example.invalid"},
        limit=4,
        pg_was_connected=False,
        config={
            "recall": {"qa_per_term_min": 10, "qa_freq_limit": 40},
            "half_life": 30,
        },
        trace=LegacySink(trace),
    )

    assert len(hits) == 4
    snapshots = trace.candidate_snapshots
    input_snapshots = [
        snap for snap in snapshots.values()
        if snap.rerank_input_position is not None
    ]
    assert len(input_snapshots) == 2
    assert sorted(snap.rerank_input_position for snap in input_snapshots) == [1, 2]
    for snap in input_snapshots:
        assert snap.rerank_provider_index is None
        assert snap.rerank_score is None
    for snap in snapshots.values():
        if snap.rerank_input_position is None:
            assert snap.rerank_provider_index is None
            assert snap.rerank_score is None


def test_recall_pool_maps_provider_indices_and_scores_to_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carry the provider's index/score mapping through to candidate traces."""
    from v3core import rerank as rerank_module

    _disable_optional_lanes(monkeypatch)
    rows = [
        (
            2400 + i,
            "2026-09-14T10:00:00+00:00",
            f"q-{i} needle",
            f"a-{i} " + ("needle " * 8),
        )
        for i in range(4)
    ]
    monkeypatch.setattr(
        recall_pool,
        "_combined_qa_keyword_lookup",
        lambda *a, **k: ({"needle": 1}, [list(rows)]),
        raising=False,
    )

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "results": [
                    {"index": 2, "relevance_score": 0.33},
                    {"index": 0, "relevance_score": 0.11},
                    {"index": 1, "relevance_score": 0.22},
                ]
            }

    monkeypatch.setattr(rerank_module.requests, "post", lambda *a, **k: _Response())
    trace = _unit_trace(())
    recall_pool.recall_pool(
        "needle",
        card_index={},
        pg=_StubPg(),
        q_emb=None,
        include_keyword=True,
        include_card_vector=False,
        include_message_vector=False,
        include_effective=False,
        include_topic=False,
        include_yin=False,
        include_notes=False,
        rerank_top_n=3,
        rerank_cfg={"endpoint": "https://example.invalid"},
        limit=4,
        pg_was_connected=False,
        config={
            "recall": {"qa_per_term_min": 10, "qa_freq_limit": 40},
            "half_life": 30,
        },
        trace=LegacySink(trace),
    )

    by_input_position = {
        snap.rerank_input_position: snap
        for snap in trace.candidate_snapshots.values()
        if snap.rerank_input_position is not None
    }
    assert set(by_input_position) == {1, 2, 3}
    expected = {1: (0, 0.11), 2: (1, 0.22), 3: (2, 0.33)}
    for position, (provider_index, score) in expected.items():
        snap = by_input_position[position]
        assert snap.rerank_provider_index == provider_index
        assert snap.rerank_score == pytest.approx(score)


def test_recall_pool_records_explicit_lane_without_inventing_rrf_contribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active-memory provenance is explicit, but not a separate RRF term."""
    _disable_optional_lanes(monkeypatch)

    class _Reader:
        def search_keyword(self, query: str, limit: int) -> list[dict[str, Any]]:
            return [
                {"memory_id": "mem-a", "title": "A", "content": "needle A"},
                {"memory_id": "mem-b", "title": "B", "content": "needle B"},
            ]

    monkeypatch.setattr(
        recall_pool, "_active_memory_reader_for", lambda *a, **k: _Reader(),
    )
    monkeypatch.setattr(
        recall_pool,
        "_combined_qa_keyword_lookup",
        lambda *a, **k: ({}, [[] for _ in (k.get("terms") or [])]),
        raising=False,
    )
    trace = _unit_trace(())
    recall_pool.recall_pool(
        "needle",
        card_index={},
        pg=_StubPg(),
        q_emb=None,
        include_keyword=True,
        include_card_vector=False,
        include_message_vector=False,
        include_effective=False,
        include_topic=False,
        include_yin=False,
        include_notes=False,
        rerank_top_n=10,
        rerank_cfg=None,
        limit=10,
        pg_was_connected=False,
        config={
            "recall": {"qa_per_term_min": 10, "qa_freq_limit": 40},
            "half_life": 30,
        },
        trace=LegacySink(trace),
    )

    for candidate_id, expected_rank in (("mem-a", 1), ("mem-b", 2)):
        snap = trace.candidate_snapshots[candidate_id]
        explicit = snap.lane_evidence_for("explicit")
        assert explicit is not None and explicit.present
        assert explicit.path == "explicit_keyword"
        assert explicit.raw_rank == expected_rank
        assert explicit.raw_score is None  # keyword reader exposes no score
        assert explicit.rrf_contribution == 0.0  # folded into existing KW path
        assert snap.lane_presence()["explicit"] is True
        assert snap.lane_vote_count == 1  # explicit provenance is not an extra RRF vote
