# -*- coding: utf-8 -*-
"""G6B Slice J — injection-stage drop evidence regression test.

The bug (SLICE J): ``prefetch_to_context_block`` emits
``drop(source_id, 'CHAR_BUDGET', stage='injection')`` probes through a
local helper that resolves methods via ``getattr(trace, 'drop', None)``.
``RecallTrace`` (src/v3core/recall_v2/trace.py) does NOT expose a ``drop``
method (that name lives only on ``LegacySink`` in
src/v3core/recall_v2/engine.py, which forwards to the typed trace).
The facade therefore resolved ``getattr(trace, 'drop', None)`` to ``None``
and the budget-drop evidence was silently lost.

This regression test runs entirely in-process.  No PG, no providers, no
network.  It mirrors the seam technique used by
``tests/test_recall_v2_facade_parity.py`` and
``tests/test_recall_v2_qa_provenance.py`` — it patches the same narrow
seams (``_combined_qa_keyword_lookup`` for keyword/QA rows, every other
lane disabled, ``_lease_pg_connection`` stubbed, ``_append_tail_qas``
neutered) and feeds a result set into the budget path where at least two
result units cannot fit.

The test asserts (on the engine-built ``RecallTrace`` obtained by
monkey-patching the engine's ``recall`` method to capture the
``trace_out`` holder the facade forwards):

  (a) exactly one trace exists in the holder;
  (b) the ids that were SELECTED but not injected carry a CHAR_BUDGET
      drop record on that SAME trace;
  (c) the number of CHAR_BUDGET drops equals (number of selected results
      minus number of injected results);
  (d) every dropped id was also SELECTED on the same trace;
  (e) the trace's ``injection_summary`` is consistent with what was
      recorded.  ``RecallTrace.record_drop`` is the typed truth boundary
      for budget-drop bookkeeping: when ``DropReasonCode.CHAR_BUDGET`` is
      recorded, the trace guarantees ``injection_summary`` exists with
      ``char_budget == int(query_plan.max_chars)``, every distinct
      ``(candidate_id, CHAR_BUDGET, stage)`` triple adds one to
      ``dropped_for_budget``, and ``truncated`` is ``True``.  Duplicate
      ``record_drop`` calls for the same triple MUST NOT double-count.

A control assertion verifies that the same run with ``trace=None``
returns the IDENTICAL context-block string, proving the fix did not
change the emitted output.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Any

import pytest


# Make the in-tree ``src`` importable regardless of how pytest was launched.
REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))


from v3core import recall_pool  # noqa: E402
from v3core import prefetch as prefetch_mod  # noqa: E402
from v3core.recall_v2 import RecallTrace  # noqa: E402
from v3core.recall_v2.engine import RecallV2Engine  # noqa: E402


# ===========================================================================
# Stub seams (mirror test_recall_v2_facade_parity.py helpers)
# ===========================================================================


class _StubCursor:
    def __init__(self) -> None:
        self._executed: list[tuple] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self._executed.append((sql, params))

    def fetchall(self) -> list[tuple]:
        return []

    def fetchone(self) -> tuple | None:
        return None

    def mogrify(self, sql: str, params: Any = None) -> bytes:
        return sql.encode("utf-8") + b" -- " + repr(params).encode("utf-8")

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
    """Fake ``pg`` that ``recall_pool`` accepts."""

    def __init__(self) -> None:
        from v3core.pg_pool import PgPool

        def _factory() -> None:
            raise RuntimeError(
                "_StubPg._factory should never be called — "
                "the harness replaces _lease_pg_connection"
            )

        self.pool = PgPool(_factory, max_connections=3, min_connections=0)

    def is_connected(self) -> bool:
        return True

    def lease(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError(
            "_StubPg.lease should never be called — the harness "
            "replaces _lease_pg_connection"
        )


@contextlib.contextmanager
def _stub_lease_pg_connection(pg: Any, timeout: Any = None, deadline: Any = None) -> Any:
    yield _StubPgConn()


def _install_stub_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextlib.contextmanager
    def _lease(pg: Any, timeout: Any = None, deadline: Any = None) -> Any:
        yield _StubPgConn()

    monkeypatch.setattr(recall_pool, "_lease_pg_connection", _lease, raising=False)


def _production_config() -> dict:
    return {
        "recall": {
            "vector_top_mult": 4,
            "qa_per_term_min": 10,
            "qa_freq_limit": 40,
        },
        "prefetch": {
            "dual_path": True,
            "rrf_k": 60,
            "include_message_vector": False,
        },
        "storage": {
            "rerank": {},
        },
        "half_life": 30,
    }


def _disable_optional_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every retrieval seam that touches PG / providers / network."""
    _install_stub_lease(monkeypatch)

    monkeypatch.setattr(
        recall_pool, "_maybe_expand_query",
        lambda query, *a, **k: query, raising=False,
    )

    def _empty_combined_qa_keyword_lookup(*a: Any, **k: Any) -> tuple[dict, list]:
        return {}, [[] for _ in (k.get("terms") or [])]

    monkeypatch.setattr(
        recall_pool, "_combined_qa_keyword_lookup",
        _empty_combined_qa_keyword_lookup, raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_combined_qa_keyword_lookup_parallel",
        _empty_combined_qa_keyword_lookup, raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_qa_keyword_cache_lookup",
        lambda *a, **k: ({}, [[] for _ in (k.get("terms") or [])]),
        raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_qa_snapshot_load",
        lambda *a, **k: None, raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_qa_snapshot_lookup",
        lambda *a, **k: ({}, []), raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_topic_snapshot_load",
        lambda *a, **k: None, raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_topic_snapshot_lookup",
        lambda *a, **k: ([], []), raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_fetch_facts",
        lambda *a, **k: {}, raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_active_memory_reader_for",
        lambda *a, **k: None, raising=False,
    )
    # Neutralize tail-QA seam so the produced block is fully budget-driven.
    monkeypatch.setattr(
        prefetch_mod, "_append_tail_qas",
        lambda lines, **k: None, raising=False,
    )


# ===========================================================================
# Engine trace-out capture
# ===========================================================================


def _install_engine_trace_capture(monkeypatch: pytest.MonkeyPatch) -> list:
    """Monkey-patch ``RecallV2Engine.recall`` to capture the ``trace_out``
    holder the facade forwards, so the test can inspect the SAME trace
    instance the facade uses.

    The engine appends ``built.trace`` to ``trace_out`` on the success
    path.  We wrap ``recall`` to delegate normally but also stash the
    captured holder list on the engine instance — the test then reads
    the holder out of the engine instance.

    Returns a list that the engine writes its captured holder into.
    """
    captured_holders: list[list] = []

    real_recall = RecallV2Engine.recall

    def _spy_recall(self, query: str, *args: Any, **kwargs: Any) -> Any:
        holder = kwargs.get("trace_out")
        if holder is not None:
            # Save the holder reference so the test can inspect it.
            captured_holders.append(holder)
        return real_recall(self, query, *args, **kwargs)

    monkeypatch.setattr(RecallV2Engine, "recall", _spy_recall)
    return captured_holders


# ===========================================================================
# The regression test
# ===========================================================================


class TestInjectionStageTrace:
    """Budget-skipped ids must land a CHAR_BUDGET drop on the engine-built
    ``RecallTrace`` (the same trace the engine built for retrieval)."""

    def test_budget_skipped_ids_record_char_budget_drop_on_trace(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Five controlled QA-keyword rows; each row's ``kind`` becomes
        # ``qa`` in the hit list (recall_pool parses rows as QA-shape).
        rows: list[tuple] = [
            (
                i,
                "2026-09-14T10:00:00+00:00",
                f"q-{i} needle",
                # 60 chars body so each rendered unit is ~100 chars; with
                # max_chars=350 the preamble (~140 chars) leaves room for
                # exactly one unit, the rest get dropped with CHAR_BUDGET.
                f"a-{i} " + ("needle " * 8),
            )
            for i in range(2100, 2105)
        ]

        def _fake_combined_lookup(*a: Any, **k: Any) -> tuple[dict, list]:
            return ({"needle": 1}, [list(rows)])

        _disable_optional_lanes(monkeypatch)
        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup",
            _fake_combined_lookup, raising=False,
        )

        cfg = _production_config()
        # Use a tight max_chars so most result units cannot fit — the
        # budget path is the one under test.  preamble (~140 chars) +
        # first unit (~100 chars) ~= 240 chars → 350 leaves a tiny
        # sliver that the second unit cannot fit into → CHAR_BUDGET drop.
        max_chars = 350

        # Capture the engine trace_out holder on every RecallV2Engine.recall.
        captured = _install_engine_trace_capture(monkeypatch)

        # ---- Run A: drive prefetch_to_context_block ----
        # The facade always builds a fresh trace; the engine appends it
        # to the holder it constructs inside ``prefetch_to_context_block``.
        # The capture spy copies that holder reference into ``captured``.
        block_no_trace = prefetch_mod.prefetch_to_context_block(
            "needle", limit=4, config=cfg, card_index={}, pg=_StubPg(),
            q_emb=None, max_chars=max_chars,
        )

        assert len(captured) == 1, (
            f"engine spy saw {len(captured)} recalls, expected exactly 1"
        )
        facade_holder = captured[0]
        assert len(facade_holder) == 1, (
            f"facade's trace_out holder got {len(facade_holder)} traces, expected 1"
        )
        facade_trace = facade_holder[0]
        assert isinstance(facade_trace, RecallTrace), (
            f"expected RecallTrace, got {type(facade_trace).__name__}"
        )

        # ---- Run B: identical call with trace=None, same context-block ----
        block_with_explicit_none = prefetch_mod.prefetch_to_context_block(
            "needle", limit=4, config=cfg, card_index={}, pg=_StubPg(),
            q_emb=None, max_chars=max_chars,
            trace=None,
        )
        # ---- Control: identical context-block string regardless of trace ----
        assert block_with_explicit_none == block_no_trace, (
            "context-block string drifted between trace=None and the "
            "default-trace call — probe path altered output"
        )
        # The block itself is non-empty (we did inject at least one unit).
        assert block_no_trace != "", (
            "context-block string is empty — no unit was injected; "
            "tighten the budget or lengthen the rows so the test "
            "exercises the budget path"
        )

        # ---- (a) Exactly one trace exists in the holder ----
        assert len(facade_holder) == 1

        # ---- Selected vs injected counts ----
        # The block contains the first result unit (greedy fill under
        # the budget).  Every id with a SELECTED event is a candidate
        # that the budget path considered; any id with an INJECTED
        # event made it into the context block; the rest must carry a
        # CHAR_BUDGET drop event with stage='injection'.
        selected_ids: list[str] = []
        injected_ids: list[str] = []
        for snap in facade_trace.candidate_snapshots.values():
            ev_types = {e.event_type.value for e in snap.events}
            if "SELECTED" in ev_types:
                selected_ids.append(snap.candidate_id)
            if "INJECTED" in ev_types:
                injected_ids.append(snap.candidate_id)

        assert len(selected_ids) >= 2, (
            f"need >= 2 selected ids to exercise the budget path, got "
            f"{len(selected_ids)}: {selected_ids!r}"
        )
        # All selected ids must come from the rows we fed.
        expected_ids = {"qa_" + str(i) for i in range(2100, 2105)}
        assert set(selected_ids) <= expected_ids, (
            f"unexpected selected ids: {set(selected_ids) - expected_ids!r}"
        )

        dropped_ids = [
            sid for sid in selected_ids
            if sid not in set(injected_ids)
        ]
        # At least one id must have been budget-skipped (the test's
        # whole point).  We allow the budget to be exactly tight enough
        # to drop ≥ 1 unit.
        assert len(dropped_ids) >= 1, (
            "no selected id was skipped — budget was not tight enough "
            "to exercise the CHAR_BUDGET drop path. selected="
            f"{selected_ids!r} injected={injected_ids!r}"
        )

        # ---- (b) Skipped ids carry a CHAR_BUDGET drop record ----
        char_budget_drops = [
            de for de in facade_trace.drop_events
            if getattr(de, "code", None) is not None
            and getattr(de.code, "value", None) == "CHAR_BUDGET"
        ]
        dropped_record_ids = {de.candidate_id for de in char_budget_drops}

        for sid in dropped_ids:
            assert sid in dropped_record_ids, (
                f"selected-but-not-injected id {sid!r} has no CHAR_BUDGET "
                f"drop record on the trace. drop_events="
                f"{[(de.candidate_id, de.code.value if de.code else None) for de in facade_trace.drop_events]!r}"
            )

        # ---- (c) Number of CHAR_BUDGET drops == selected - injected ----
        assert len(char_budget_drops) == (
            len(selected_ids) - len(injected_ids)
        ), (
            f"CHAR_BUDGET drop count mismatch: drops={len(char_budget_drops)} "
            f"selected={len(selected_ids)} injected={len(injected_ids)}"
        )

        # ---- (d) Every dropped id was also SELECTED on the same trace ----
        for sid in dropped_record_ids:
            snap = facade_trace.candidate_snapshots.get(sid)
            assert snap is not None, f"no snapshot for dropped id {sid!r}"
            ev_types = {e.event_type.value for e in snap.events}
            assert "SELECTED" in ev_types, (
                f"dropped id {sid!r} was not SELECTED — selection and "
                f"drop evidence inconsistent"
            )

        # ---- (e) injection_summary consistency ----
        # ``RecallTrace.record_drop`` (src/v3core/recall_v2/trace.py) is
        # the typed truth boundary for budget-drop bookkeeping.  When a
        # CHAR_BUDGET drop is recorded, the trace guarantees
        # ``injection_summary`` exists (even if no inject probe landed
        # yet), ``char_budget`` mirrors the typed query plan, every
        # distinct ``(candidate_id, CHAR_BUDGET, stage)`` triple adds
        # one to ``dropped_for_budget``, and ``truncated`` is ``True``.
        summary = facade_trace.injection_summary
        assert summary is not None, (
            "injection_summary is None — no CHAR_BUDGET drop landed and "
            "no inject probe landed.  record_drop must create the "
            "summary on first CHAR_BUDGET even with injected_count=0"
        )
        assert summary.char_budget == int(max_chars), (
            f"summary.char_budget={summary.char_budget} != max_chars="
            f"{max_chars} — record_drop must mirror query_plan.max_chars"
        )
        # The summary reflects the inject probes it has actually seen.
        assert summary.injected_count == len(injected_ids), (
            f"summary.injected_count={summary.injected_count} != "
            f"len(injected)={len(injected_ids)} — inject path counts drift"
        )
        # The summary reflects the typed budget-drop records (one per
        # distinct (candidate_id, CHAR_BUDGET, stage) triple).
        assert summary.dropped_for_budget == len(char_budget_drops), (
            f"summary.dropped_for_budget={summary.dropped_for_budget} != "
            f"len(CHAR_BUDGET drops)={len(char_budget_drops)} — "
            f"record_drop bookkeeping drifted from drop_events"
        )
        # truncated is True once any CHAR_BUDGET drop has been recorded.
        assert summary.truncated is True, (
            "summary.truncated must be True once a CHAR_BUDGET drop is "
            "recorded; the typed budget boundary guarantees it"
        )
        # Consistency between selected, injected, dropped counts:
        # selected == injected + dropped_for_budget.
        assert len(selected_ids) == (
            len(injected_ids) + summary.dropped_for_budget
        ), (
            f"selected={len(selected_ids)} != injected={len(injected_ids)} "
            f"+ dropped_for_budget={summary.dropped_for_budget}"
        )
        # Drop count, however, IS observable via trace.drop_events
        # directly — that is the canonical evidence channel for the
        # budget-drop story this regression test pins down.
        assert len(char_budget_drops) == len(dropped_ids), (
            f"drop_events count ({len(char_budget_drops)}) != "
            f"len(dropped_ids) ({len(dropped_ids)})"
        )


# ===========================================================================
# Focused unit tests for record_drop as a typed truth boundary
# ===========================================================================


def _unit_trace(max_chars: int = 2000, candidate_ids: tuple[str, ...] = ("c1", "c2")):
    """Build a RecallTrace wired up with snapshots so record_drop can run.

    Pure stdlib, no PG / providers / facade.  Each candidate gets a
    minimal ``CandidateSnapshot`` already attached to the trace so
    :meth:`RecallTrace.record_drop` finds the snapshot by id (the typed
    truth boundary otherwise raises ``KeyError``).
    """
    import time
    from v3core.recall_v2 import (
        CandidateSnapshot,
        DropReasonCode,
        QueryContext,
        build_default_query_plan,
    )
    ctx = QueryContext(
        query_id="q-unit", query_text="hello",
        deadline_monotonic=time.monotonic() + 5.0,
        budget_ms=1000, limit=4, max_chars=max_chars,
    )
    plan = build_default_query_plan(ctx)
    trace = RecallTrace(query_context=ctx, query_plan=plan)
    for cid in candidate_ids:
        snap = CandidateSnapshot(
            candidate_id=cid, lane="keyword",
            source_type="qa", source_id=cid,
        )
        trace.candidate_snapshots[cid] = snap
    return trace, DropReasonCode


class TestRecordDropCharBudgetTruthBoundary:
    """Focused unit tests: ``record_drop`` is the typed truth boundary
    for CHAR_BUDGET bookkeeping on ``injection_summary``.

    Pre-fix behaviour: summary is only ever created by ``inject``,
    ``dropped_for_budget`` stays at 0, and ``truncated`` is never
    flipped on by ``record_drop``.  These tests pin the NEW contract.
    """

    def test_first_char_budget_drop_creates_summary_with_zero_injected(self) -> None:
        """The first CHAR_BUDGET drop must create the summary even when
        injected_count is still 0 (no inject probe has landed yet)."""
        trace, DropReasonCode = _unit_trace(max_chars=1234)
        trace.record_drop("c1", DropReasonCode.CHAR_BUDGET,
                           stage="injection", detail="x")
        s = trace.injection_summary
        assert s is not None, (
            "first CHAR_BUDGET drop did not create injection_summary; "
            "the typed budget boundary must create it eagerly"
        )
        assert s.injected_count == 0
        assert s.char_budget == 1234, (
            f"summary.char_budget={s.char_budget} != plan.max_chars=1234"
        )
        assert s.dropped_for_budget == 1
        assert s.truncated is True

    def test_each_unique_budget_drop_increments_summary(self) -> None:
        """Each distinct ``(candidate_id, CHAR_BUDGET, stage)`` triple
        must add exactly one to ``dropped_for_budget``."""
        trace, DropReasonCode = _unit_trace(
            max_chars=500, candidate_ids=("c1", "c2", "c3"),
        )
        trace.record_drop("c1", DropReasonCode.CHAR_BUDGET, stage="injection")
        trace.record_drop("c2", DropReasonCode.CHAR_BUDGET, stage="injection")
        trace.record_drop("c3", DropReasonCode.CHAR_BUDGET, stage="injection")
        assert trace.injection_summary is not None
        assert trace.injection_summary.dropped_for_budget == 3
        assert trace.injection_summary.char_budget == 500
        assert trace.injection_summary.truncated is True

    def test_same_budget_drop_is_not_double_counted(self) -> None:
        """Recording the same ``(candidate_id, CHAR_BUDGET, stage)``
        triple twice must NOT double-count ``dropped_for_budget``.

        The typed identity is the existing typed drop triple — callers
        must NOT have to manually mutate trace internals.  ``record_drop``
        keeps the canonical ``drop_events`` list semantics (one entry
        per recorded call) but only bumps ``dropped_for_budget`` for
        the first occurrence of each typed triple.
        """
        trace, DropReasonCode = _unit_trace(max_chars=200)
        trace.record_drop("c1", DropReasonCode.CHAR_BUDGET, stage="injection")
        # Same triple, recorded again.
        trace.record_drop("c1", DropReasonCode.CHAR_BUDGET, stage="injection")
        # The third time, still the same triple.
        trace.record_drop("c1", DropReasonCode.CHAR_BUDGET, stage="injection")
        assert trace.injection_summary is not None
        assert trace.injection_summary.dropped_for_budget == 1, (
            "duplicate CHAR_BUDGET drops were double-counted — "
            "record_drop must dedupe by typed identity"
        )
        # canonical drop_events still records every probe.
        char_budget_drops = [
            de for de in trace.drop_events
            if getattr(de, "code", None) is DropReasonCode.CHAR_BUDGET
        ]
        assert len(char_budget_drops) == 3, (
            "drop_events must keep one entry per record_drop call — "
            "canonical semantics preserved"
        )

    def test_non_char_budget_drops_do_not_bump_dropped_for_budget(self) -> None:
        """A non-CHAR_BUDGET drop must NOT inflate ``dropped_for_budget``
        and must NOT create the budget summary (unless an inject probe
        had already created it)."""
        trace, DropReasonCode = _unit_trace(
            max_chars=900, candidate_ids=("c1", "c2"),
        )
        # Select before drop (the canonical lifecycle: select then drop).
        trace.select("c1")
        trace.record_drop("c1", DropReasonCode.BELOW_THRESHOLD,
                           stage="fusion")
        trace.select("c2")
        trace.record_drop("c2", DropReasonCode.OUTSIDE_LIMIT,
                           stage="selection")
        assert trace.injection_summary is None, (
            "non-CHAR_BUDGET drop created a budget summary — must not "
            "happen unless an inject probe had landed"
        )
        # Now record an inject probe — it creates the summary.
        trace.inject("c1", char_count=10)
        s = trace.injection_summary
        assert s is not None
        # The summary's budget-related counters must still be 0/False.
        assert s.dropped_for_budget == 0
        assert s.truncated is False
        # A subsequent BELOW_THRESHOLD drop must not bump dropped_for_budget.
        trace.record_drop("c1", DropReasonCode.BELOW_THRESHOLD, stage="fusion")
        assert s.dropped_for_budget == 0

    def test_inject_then_budget_drop_keeps_injected_count_and_adds_drop(self) -> None:
        """inject() must keep its existing counters; a CHAR_BUDGET drop
        on a different candidate must add only to dropped_for_budget /
        truncated."""
        trace, DropReasonCode = _unit_trace(
            max_chars=400, candidate_ids=("kept", "dropped"),
        )
        trace.select("kept")
        trace.inject("kept", char_count=120)
        s_before = trace.injection_summary
        assert s_before is not None
        assert s_before.injected_count == 1
        assert s_before.total_chars == 120
        assert s_before.dropped_for_budget == 0
        # CHAR_BUDGET drop on the other candidate.
        trace.select("dropped")
        trace.record_drop("dropped", DropReasonCode.CHAR_BUDGET,
                           stage="injection")
        # inject() counters preserved; budget counters reflect the drop.
        s_after = trace.injection_summary
        assert s_after is s_before
        assert s_after.injected_count == 1
        assert s_after.total_chars == 120
        assert s_after.dropped_for_budget == 1
        assert s_after.truncated is True
        assert s_after.char_budget == 400

    def test_all_budget_dropped_zero_injected_full_closure(self) -> None:
        """Zero-injected / all-budget-dropped closure.

        Scenario: ONE selected candidate, NO inject probe, ONE CHAR_BUDGET
        drop with stage='injection'.  Every constraint from the Issue A
        contract must hold simultaneously on the SAME trace:

          * injection_summary exists (lazy-created by record_drop).
          * char_budget == int(query_plan.max_chars).
          * injected_count == 0  (no inject probe landed).
          * dropped_for_budget == 1  (exactly one budget drop).
          * truncated is True.
          * selected == injected + dropped_for_budget
            (here: 1 == 0 + 1).
        """
        max_chars = 777
        trace, DropReasonCode = _unit_trace(
            max_chars=max_chars, candidate_ids=("solo",),
        )
        # Select without injecting — the canonical "tried but budget-cut"
        # shape that this slice's facade emits.
        trace.select("solo")
        trace.record_drop(
            "solo", DropReasonCode.CHAR_BUDGET,
            stage="injection", detail="no_room_left",
        )
        s = trace.injection_summary
        assert s is not None, (
            "summary missing — record_drop must create it eagerly on "
            "the first CHAR_BUDGET drop"
        )
        # char_budget mirrors the typed query plan, NOT some other value.
        assert s.char_budget == max_chars, (
            f"char_budget={s.char_budget} != query_plan.max_chars={max_chars}"
        )
        # No inject probe landed, so injected_count / total_chars are 0.
        assert s.injected_count == 0, (
            f"injected_count must be 0 (no inject probe landed), got "
            f"{s.injected_count}"
        )
        assert s.total_chars == 0
        # Exactly one CHAR_BUDGET drop recorded → dropped_for_budget=1.
        assert s.dropped_for_budget == 1, (
            f"dropped_for_budget={s.dropped_for_budget}, expected 1"
        )
        # truncated is a one-way latch from the typed truth boundary.
        assert s.truncated is True
        # Closed ledger: selected == injected + dropped_for_budget.
        assert 1 == s.injected_count + s.dropped_for_budget, (
            "ledger not closed: selected=1 != injected=0 + dropped=1"
        )
        # drop_events still records exactly the one probe (canonical
        # semantics preserved).
        assert len(trace.drop_events) == 1
        de = trace.drop_events[0]
        assert de.code is DropReasonCode.CHAR_BUDGET
        assert de.candidate_id == "solo"
        assert de.stage == "injection"
        assert de.detail == "no_room_left"

    def test_many_selected_all_dropped_zero_injected_closure(self) -> None:
        """N selected, 0 injected, N CHAR_BUDGET drops.

        Mirror of the all-budget-dropped fixture, with multiple
        candidates.  The closed-ledger invariant scales:
        selected == injected + dropped_for_budget must hold for any N.
        """
        ids = tuple(f"c{i}" for i in range(5))
        trace, DropReasonCode = _unit_trace(
            max_chars=100, candidate_ids=ids,
        )
        for cid in ids:
            trace.select(cid)
            trace.record_drop(cid, DropReasonCode.CHAR_BUDGET,
                                stage="injection")
        s = trace.injection_summary
        assert s is not None
        assert s.char_budget == 100
        assert s.injected_count == 0
        assert s.dropped_for_budget == 5
        assert s.truncated is True
        # Closed ledger for N=5.
        assert len(ids) == s.injected_count + s.dropped_for_budget
        # canonical drop_events preserved.
        assert len(trace.drop_events) == 5
        char_budget_drops = [
            de for de in trace.drop_events
            if de.code is DropReasonCode.CHAR_BUDGET
        ]
        assert len(char_budget_drops) == 5

    def test_all_selected_all_injected_no_budget_drop(self) -> None:
        """All-injected / no-budget-drop case (the opposite extreme).

        Scenario: at least two candidates are ``select()``-ed AND every
        one of them is ``inject()``-ed.  No ``record_drop`` call lands.
        ``char_count`` per candidate is kept strictly below ``max_chars``
        so ``inject()`` does NOT cross the ``total_chars > char_budget``
        threshold that would latch ``truncated`` to ``True``.  This pins
        the symmetric complement of ``test_all_budget_dropped_zero_injected_full_closure``:

          * ``injection_summary`` exists (lazy-created by ``inject``).
          * ``injected_count == selected`` (every selected candidate
            was injected).
          * ``dropped_for_budget == 0`` (no CHAR_BUDGET drop recorded).
          * ``truncated is False`` (no drop latched it; total chars
            stayed within the budget so the overflow branch did NOT fire).
          * ``char_budget == int(query_plan.max_chars)`` (mirrors the
            typed query plan, not some other value).
        """
        max_chars = 800
        ids = ("alpha", "beta")
        trace, _DropReasonCode = _unit_trace(
            max_chars=max_chars, candidate_ids=ids,
        )
        # Per-candidate char_count must stay strictly under max_chars
        # (and sum under max_chars) so the inject() overflow latch does
        # not fire — we want truncated is False to be a true statement
        # about the absence of any budget pressure.
        char_counts = {"alpha": 100, "beta": 120}
        for cid in ids:
            trace.select(cid)
            trace.inject(cid, char_count=char_counts[cid])

        s = trace.injection_summary
        assert s is not None, (
            "injection_summary is None after a successful inject() — "
            "inject must create it lazily on the first call"
        )
        assert s.injected_count == len(ids), (
            f"injected_count={s.injected_count} != selected={len(ids)} — "
            "every selected candidate should have been injected in this "
            "all-injected fixture"
        )
        assert s.dropped_for_budget == 0, (
            f"dropped_for_budget={s.dropped_for_budget} != 0 — no "
            "CHAR_BUDGET drop was recorded in this all-injected fixture"
        )
        assert s.truncated is False, (
            "truncated is True in an all-injected/no-budget-drop fixture — "
            "neither a CHAR_BUDGET drop nor an overflow past char_budget "
            "should have latched it"
        )
        assert s.char_budget == max_chars, (
            f"char_budget={s.char_budget} != query_plan.max_chars={max_chars}"
        )
        # Closed ledger: every selected candidate was injected and none
        # were dropped — selected == injected + dropped_for_budget.
        assert len(ids) == s.injected_count + s.dropped_for_budget, (
            f"ledger drift: selected={len(ids)} != "
            f"injected={s.injected_count} + dropped={s.dropped_for_budget}"
        )
        # total_chars is the sum of the per-candidate char_counts we fed.
        assert s.total_chars == sum(char_counts.values())
        # No drop_events were recorded — the canonical drop channel
        # is empty for this all-injected fixture.
        assert len(trace.drop_events) == 0, (
            f"drop_events is non-empty ({len(trace.drop_events)}) in an "
            "all-injected/no-budget-drop fixture"
        )
        # Every snapshot is both selected and injected (no orphans).
        for cid in ids:
            snap = trace.candidate_snapshots[cid]
            assert snap.selected is True, (
                f"snapshot {cid!r} is not selected after select()"
            )
            assert snap.injected is True, (
                f"snapshot {cid!r} is not injected after inject()"
            )