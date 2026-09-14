# -*- coding: utf-8 -*-
"""G6B Slice H — regression test for QA-vector-only qa-lane provenance.

The bug (MEDIUM C): QA hits discovered by the QA-vector retrieval seam
were added to ``vec_ids`` but never to a qa-lane probe list.  The qa
lane's ``lane_candidates`` probe only carried keyword-found QA ids, and
the qa lane's ``lane_finish`` candidate_count was ``len(_qa_kw_ids)``.
The result: a QA hit reachable only through the QA-vector path
received NO qa-lane attribution in the trace at all.  Retrieval math
was unaffected (the engine fell back to ``lane_for_kind``), but the
trace was semantically wrong — such a hit's REAL lane is ``qa``, not
``vector``.

This regression test patches the narrowest seams in ``recall_pool``
(the same seam technique used by ``tests/test_recall_v2_differential.py``
— that file is read for the pattern but MUST NOT be modified):

  * QA-keyword seam (``_combined_qa_keyword_lookup`` and parallel)
    returns NO hits, so the keyword path contributes no QA rows.
  * QA-vector cursor's ``fetchall`` returns exactly one row whose
    cosine is >= 0.35, so the QA-vector path contributes one QA hit.
    All OTHER SQL queries (count(*), keyword ILIKE, topic_blocks
    SQLite fallback, etc.) return empty from the same stub cursor —
    the cursor is "smart" and inspects the SQL text.
  * All other lanes are disabled.

It then drives ``v3core.recall_pool.recall_pool(...)`` twice — once
with a real ``LegacySink`` attached (imported from
``v3core.recall_v2.engine``) and once with ``trace=None`` — and
asserts:

  1. The returned hit has ``kind == "qa"``.
  2. The trace snapshot for that id lists ``"qa"`` in its contributing
     lanes and carries EXACTLY ONE ``ProvenanceRecord`` for the qa lane
     (one entry per ``(lane, source_type, source_id)`` triple — the
     sink de-duplicates).
  3. The qa lane summary's ``candidate_count`` includes the QA-vector
     hit (combined collection, de-duplicated).
  4. There is NO fabricated keyword provenance and NO fabricated
     vector provenance for that id (only the qa lane attributed it).
  5. The same run with ``trace=None`` returns an identical hit list,
     proving the probe did not change retrieval results.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest


# Make the in-tree ``src`` importable regardless of how pytest was launched.
REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))


from v3core import recall_pool  # noqa: E402
from v3core.recall_v2 import build_default_query_plan  # noqa: E402
from v3core.recall_v2.adapters import build_query_context  # noqa: E402
from v3core.recall_v2.engine import LegacySink  # noqa: E402
from v3core.recall_v2.trace import RecallTrace  # noqa: E402
from v3core.types import RecallHit  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal seams — the same technique used by tests/test_recall_v2_differential.py
# (that file is read for the pattern but MUST NOT be modified).
# ---------------------------------------------------------------------------


class _SmartStubCursor:
    """DB-API cursor that returns rows ONLY for the QA-vector query.

    The QA-vector SQL is the only ``SELECT`` whose text contains both
    ``embedding <=>`` (the cosine distance operator) AND ``AS cosine``
    (the cosine projection alias).  Every other query the legacy
    ``recall_pool`` issues — ``count(*) FROM qa_pairs``, the
    keyword+QA ``ILIKE`` scan, the ``topic_blocks`` SQLite fallback
    via ``_lease_pg_connection``, etc. — returns ``[]`` from
    ``fetchall()`` and ``(0,)`` from ``fetchone()`` so those paths
    produce no candidates.  This is the narrowest seam: only the
    QA-vector block sees the controlled row.
    """

    def __init__(self, qa_vec_rows: Iterable[tuple]) -> None:
        self._rows = list(qa_vec_rows)
        self._last_sql: str = ""
        self._executed: list[tuple] = []

    def _is_qa_vector_query(self) -> bool:
        return (
            "AS cosine" in self._last_sql
            and "embedding <=>" in self._last_sql
        )

    def execute(self, sql: str, params: Any = None) -> None:
        self._executed.append((sql, params))
        self._last_sql = sql or ""

    def fetchall(self) -> list[tuple]:
        if self._is_qa_vector_query():
            return list(self._rows)
        return []

    def fetchone(self) -> tuple | None:
        if self._is_qa_vector_query():
            return self._rows[0] if self._rows else None
        # ``count(*) FROM qa_pairs`` -> (0,)
        return (0,)

    def mogrify(self, sql: str, params: Any = None) -> bytes:
        return sql.encode("utf-8") + b" -- " + repr(params).encode("utf-8")

    def __enter__(self) -> "_SmartStubCursor":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def close(self) -> None:
        return None


class _StubPgConn:
    """Minimal connection that hands out the configured cursor."""

    def __init__(self, cursor: _SmartStubCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _SmartStubCursor:
        return self._cursor

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
    """Fake ``pg`` that satisfies ``_pg_is_connected`` and exposes a real
    ``PgPool`` instance so ``_use_combined_qa`` is True and the
    keyword/QA branch is entered (then immediately empty because every
    keyword seam is patched to return empty / no rows).  No physical
    connections are ever opened — ``_lease_pg_connection`` is replaced
    with a context manager that yields a stub connection.
    """

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
            "_StubPg.lease should never be called — the harness "
            "replaces _lease_pg_connection"
        )


def _production_config() -> dict:
    """A production-shaped config dict (the test path must NOT pass config=None;
    recall_pool's keyword path raises ``AttributeError: 'NoneType' object has
    no attribute 'get'`` against config=None).
    """
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


def _disable_keyword_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the QA-keyword path to contribute NO hits.

    This is the narrowest seam: every seam that can return QA-keyword
    rows is patched to return empty so the only QA hits in this test
    come from the QA-vector cursor.
    """

    def _empty_combined(*a: Any, **k: Any) -> tuple[dict, list]:
        return {}, [[] for _ in (k.get("terms") or [])]

    monkeypatch.setattr(
        recall_pool, "_maybe_expand_query",
        lambda query, *a, **k: query, raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_combined_qa_keyword_lookup",
        _empty_combined, raising=False,
    )
    monkeypatch.setattr(
        recall_pool, "_combined_qa_keyword_lookup_parallel",
        _empty_combined, raising=False,
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
    # Active-memory reader is disabled so the explicit lane is empty.
    monkeypatch.setattr(
        recall_pool, "_active_memory_reader_for",
        lambda *a, **k: None, raising=False,
    )


def _install_qa_vector_stub_lease(
    monkeypatch: pytest.MonkeyPatch,
    qa_rows: Iterable[tuple],
) -> None:
    """Replace ``_lease_pg_connection`` with a context manager that
    yields a connection whose cursor is ``_SmartStubCursor``.  Only the
    QA-vector SQL (detected by text) gets back the controlled rows.
    Every other query (count, ILIKE, SQLite fallback, etc.) returns
    empty so the rest of the keyword/QA path produces no hits.
    """
    cursor = _SmartStubCursor(qa_rows)

    @contextlib.contextmanager
    def _lease(pg: Any, timeout: Any = None, deadline: Any = None) -> Any:
        yield _StubPgConn(cursor)

    monkeypatch.setattr(
        recall_pool, "_lease_pg_connection", _lease, raising=False,
    )


def _disable_topic_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the topic-recall vector lane (``include_topic``) to produce
    NO hits so the only vector hits come from the QA-vector cursor.
    """
    class _EmptyTopicRecall:
        def match(self, *a: Any, **k: Any) -> list:
            return []

    monkeypatch.setattr(
        recall_pool, "_resolve_topic_recall",
        lambda *a, **k: _EmptyTopicRecall(), raising=False,
    )


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------


class TestQAVectorOnlyProvenance:
    """QA-vector-only hit must be attributed to the qa lane.

    Strategy: patch the QA-keyword seams to return empty, install a
    smart stub PG lease whose QA-vector cursor returns exactly one row
    with cosine >= 0.35, and disable every other lane.  The QA-vector
    block will populate ``vec_ids.add('qa_777')`` and (after the Slice
    H fix) ``_qa_vec_ids_for_probe.append('qa_777')``.  The hit is then
    ranked through ``qa_vec_rank`` (since ``kind='qa'`` excludes it
    from ``vec_rank``) and surfaces as the only returned hit.
    """

    def test_qa_vector_only_records_qa_lane_provenance(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # One QA-vector row — id 777, cosine well above the 0.35 floor.
        qa_row = (
            777,                                              # id
            "2026-09-14T10:00:00+00:00",                       # timestamp
            "What is the QA-vector-only hit?",                # question
            "This hit was discovered by the QA-vector path",   # answer
            0.81,                                             # cosine
        )

        # QA-keyword path: zero hits.
        _disable_keyword_lanes(monkeypatch)
        # Topic-recall vector lane: zero hits.
        _disable_topic_recall(monkeypatch)
        # QA-vector cursor: one hit; everything else empty.
        _install_qa_vector_stub_lease(monkeypatch, [qa_row])

        # Need a non-None q_emb so the QA-vector block is entered.
        q_emb = [0.01] * 1024
        # ``_StubPg`` satisfies ``_pg_is_connected`` and ``_use_combined_qa``.
        fake_pg = _StubPg()
        cfg = _production_config()

        common_kwargs: dict[str, Any] = dict(
            card_index={},
            pg=fake_pg,
            q_emb=q_emb,
            include_keyword=True,
            include_card_vector=True,   # required so the QA-vector block runs
            include_message_vector=False,
            include_effective=False,
            include_topic=True,
            include_yin=False,
            include_notes=False,
            rerank_top_n=10,
            rerank_cfg=None,
            limit=5,
            pg_was_connected=True,
            config=cfg,
            sqlite_store=None,
            core=None,
        )

        # ---- Run 1: trace=None (baseline; probe is a no-op) ----
        none_hits, _ = recall_pool.recall_pool(
            "needle", trace=None, **common_kwargs,
        )

        # ---- Run 2: LegacySink attached to a real RecallTrace ----
        ctx = build_query_context("needle", limit=5, max_chars=10_000, deadline=None)
        plan = build_default_query_plan(ctx)
        trace = RecallTrace(query_context=ctx, query_plan=plan)
        sink = LegacySink(trace)
        hits_with_trace, _ = recall_pool.recall_pool(
            "needle", trace=sink, **common_kwargs,
        )

        # ---- Identical hit list across the two runs ----
        none_ids = [h.source_id for h in none_hits]
        traced_ids = [h.source_id for h in hits_with_trace]
        assert none_ids == traced_ids, (
            f"probe changed retrieval: trace=None={none_ids} "
            f"trace=sink={traced_ids}"
        )

        # Exactly one hit and it is QA.
        assert len(hits_with_trace) == 1, (
            f"expected exactly one hit, got {len(hits_with_trace)}: "
            f"{traced_ids}"
        )
        hit = hits_with_trace[0]
        assert hit.kind == "qa", f"expected kind=qa, got {hit.kind!r}"
        assert hit.source_id == "qa_777", (
            f"expected source_id=qa_777, got {hit.source_id!r}"
        )

        # ---- Trace snapshot for this id ----
        snap = trace.candidate_snapshots.get("qa_777")
        assert snap is not None, (
            "trace has no CandidateSnapshot for the QA-vector hit — "
            "the qa-lane probe is missing"
        )

        # qa is in the contributing lanes.
        assert "qa" in snap.contributing_lanes, (
            f"qa lane missing from contributing_lanes: {snap.contributing_lanes!r}"
        )

        # EXACTLY ONE provenance record for the qa lane — the sink
        # de-duplicates per (lane, source_type, source_id) so the
        # combined-list probe collapses to a single entry.  The
        # source_type is "qa" (the vector-side attribution); the
        # kw-only probe did NOT add an extra entry because the kw
        # path produced zero hits in this test.
        qa_provenance = [p for p in snap.provenance if getattr(p, "lane", None) == "qa"]
        assert len(qa_provenance) == 1, (
            f"expected exactly 1 qa-lane provenance record, got {len(qa_provenance)}: "
            f"{[(getattr(p, 'lane', None), getattr(p, 'source_type', None), getattr(p, 'source_id', None)) for p in qa_provenance]}"
        )
        prov = qa_provenance[0]
        assert getattr(prov, "source_type", None) == "qa", (
            f"expected source_type=qa on the qa-lane provenance record, "
            f"got {getattr(prov, 'source_type', None)!r}"
        )
        assert getattr(prov, "source_id", None) == "qa_777", (
            f"expected source_id=qa_777 on the qa-lane provenance record, "
            f"got {getattr(prov, 'source_id', None)!r}"
        )

        # ---- No fabricated keyword or vector provenance ----
        keyword_provenance = [p for p in snap.provenance if getattr(p, "lane", None) == "keyword"]
        vector_provenance = [p for p in snap.provenance if getattr(p, "lane", None) == "vector"]
        assert keyword_provenance == [], (
            f"fabricated keyword provenance for QA-vector hit: {keyword_provenance!r}"
        )
        assert vector_provenance == [], (
            f"fabricated vector provenance for QA-vector hit: {vector_provenance!r}"
        )

        # ---- qa lane summary's candidate_count includes the QA-vector hit ----
        qa_summary = trace.lane_summaries.get("qa")
        assert qa_summary is not None, "qa lane summary missing from trace"
        # The QA-vector hit must be counted in the qa lane.  Even with
        # the kw-only probe being empty, the final lane_finish (emitted
        # after the vector region with the combined collection) must
        # record candidate_count >= 1.
        assert qa_summary.candidate_count >= 1, (
            f"qa lane summary candidate_count={qa_summary.candidate_count} "
            "does not include the QA-vector hit"
        )

        # Sanity: the qa lane summary must NOT be marked skipped —
        # we ran the QA-vector path and it produced a hit.
        assert qa_summary.skipped is False, (
            f"qa lane summary marked skipped={qa_summary.skipped} "
            f"(reason={qa_summary.reason!r}); expected not-skipped because "
            "the QA-vector path produced a hit"
        )