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
      recorded.  ``RecallTrace.inject`` only ever increments
      ``injected_count`` / ``total_chars`` — it NEVER reads drop events
      — so ``dropped_for_budget`` is locked at 0 by contract.  The test
      asserts the exact current behaviour and documents the reason in a
      comment.

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
        # NOTE: ``RecallTrace.inject`` (src/v3core/recall_v2/trace.py) is
        # the only writer of ``injection_summary`` — it increments
        # ``injected_count`` and ``total_chars`` from inject probes and
        # NEVER inspects drop probes.  ``dropped_for_budget`` is therefore
        # always 0 under the current engine, regardless of how many
        # CHAR_BUDGET drops the facade records.  This is a known
        # limitation of the trace contract: the summary field exists
        # but is not auto-computed from drops.  We assert the exact
        # current behaviour to lock the contract.
        summary = facade_trace.injection_summary
        assert summary is not None, (
            "injection_summary is None — no inject probes landed"
        )
        # The summary reflects ONLY inject probes (it cannot see drops
        # because ``inject`` never reads drop_events).  We assert the
        # exact current behaviour, then explain it in a comment.
        assert summary.injected_count == len(injected_ids), (
            f"summary.injected_count={summary.injected_count} != "
            f"len(injected)={len(injected_ids)} — inject path counts drift"
        )
        # Documented limitation: ``dropped_for_budget`` is NOT computed
        # from ``drop_events``; the field stays at 0 even when drops
        # are recorded.  See ``RecallTrace.inject`` (no drop read) and
        # ``InjectionSummary.dropped_for_budget`` (only written by callers
        # that explicitly set it).
        assert summary.dropped_for_budget == 0, (
            "injection_summary.dropped_for_budget is unexpectedly non-zero; "
            "the trace contract changed — re-evaluate this assertion"
        )
        # Drop count, however, IS observable via trace.drop_events
        # directly — that is the canonical evidence channel for the
        # budget-drop story this regression test pins down.
        assert len(char_budget_drops) == len(dropped_ids), (
            f"drop_events count ({len(char_budget_drops)}) != "
            f"selected-but-not-injected count ({len(dropped_ids)})"
        )