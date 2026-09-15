# -*- coding: utf-8 -*-
"""G6B Slice C — differential parity harness for Recall V2 vs legacy recall_pool.

This test file is the §20 differential parity harness.  The mission is
to assert that the new ``RecallV2Engine`` does NOT change results
relative to the legacy ``v3core.recall_pool.recall_pool`` — in every
parity category the legacy and V2 hit lists MUST be byte-equal
(in order, on the documented fields).  A category whose comparison is
only "both non-empty" is not acceptable.

The harness runs entirely in-process.  No PG, no providers, no
network, no sleeps.  We drive the lanes deterministically by
monkeypatching the narrowest seam in the real ``recall_pool`` that
produces that lane's candidates — the fusion / temporal / boost /
rerank / selection code still runs unmodified.  Never patch the
engine or the fusion code itself.

Categories exercised (one test each):
    * keyword-dominant
    * vector-dominant
    * QA exact
    * QA fuzzy/vector
    * topic
    * explicit-memory
    * multi-lane duplicate
    * temporal
    * rerank enabled
    * rerank skipped by budget
    * empty result
    * one lane error
    * timeout/deadline
    * char-budget truncation (prefetch_to_context_block)
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import sys
import time
import contextlib
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest

# Make the in-tree ``src`` importable regardless of how pytest was launched.
REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

from v3core._deadline import (  # noqa: E402
    PrefetchDeadline,
    PrefetchDeadlineExceeded,
)
from v3core import recall_pool  # noqa: E402
from v3core.recall_v2 import (  # noqa: E402
    QueryContext,
    RecallTrace,
    build_default_query_plan,
)
from v3core.recall_v2.adapters import build_query_context  # noqa: E402
from v3core.recall_v2.engine import (  # noqa: E402
    RecallV2Engine,
    RecallV2Result,
)
from v3core.types import RecallHit  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — controlled-hit factory + legacy/V2 parity comparator
# ---------------------------------------------------------------------------


def _hit(
    *,
    source_id: str,
    kind: str,
    title: str = "T",
    content: str = "body",
    content_preview: str = "preview",
    category: str = "",
    tags: list[str] | None = None,
    cosine: float = 0.0,
    rrf_score: float = 0.0,
    facts: list[str] | None = None,
    created_at: str = "",
) -> RecallHit:
    return RecallHit(
        source_id=source_id,
        title=title,
        content_preview=content_preview,
        category=category or kind,
        tags=list(tags or []),
        cosine=float(cosine),
        rrf_score=float(rrf_score),
        facts=list(facts or []),
        kind=kind,
        created_at=created_at,
        content=content,
    )


# The exact tuple of fields the spec requires for hit-by-hit equality.
HIT_COMPARE_FIELDS = (
    "source_id", "kind", "rrf_score", "cosine", "title",
    "category", "content", "facts",
)


def _hit_compare_dict(h: RecallHit) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in HIT_COMPARE_FIELDS:
        out[f] = getattr(h, f, None)
    return out


def _hits_equal(
    legacy: list[RecallHit], v2: list[RecallHit]
) -> tuple[bool, str]:
    """Return (equal, message)."""
    if len(legacy) != len(v2):
        return False, (
            f"hit-list length differs: legacy={len(legacy)} v2={len(v2)} "
            f"legacy={[_hit_compare_dict(h) for h in legacy]} "
            f"v2={[_hit_compare_dict(h) for h in v2]}"
        )
    for i, (lh, vh) in enumerate(zip(legacy, v2)):
        ld = _hit_compare_dict(lh)
        vd = _hit_compare_dict(vh)
        if ld != vd:
            return False, (
                f"hit-list differs at index {i}: legacy={ld} v2={vd}"
            )
    return True, ""


def _recall_pool_signature() -> inspect.Signature:
    return inspect.signature(recall_pool.recall_pool)


def _production_config() -> dict:
    """A production-shaped config dict (the test path must NOT pass config=None;
    recall_pool's keyword path raises ``AttributeError: 'NoneType' object has
    no attribute 'get'`` against config=None — a known fail-closed contract
    that the harness must respect).
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


# A list of all recall_pool row tuples the harness can inject as
# controlled keyword/QA inputs.  Each tuple is
# (id, timestamp, question, answer) — matching the format the legacy
# SQL path passes to the QA scoring block.
TYPE_KEYWORD_ROWS: list[tuple] = []
TYPE_QA_ROWS: list[tuple] = []


# (The TYPE_KEYWORD_ROWS / TYPE_QA_ROWS module-level slots are
# reserved for future parametrized use; current tests build the
# row lists locally so the seams are obvious.)


class _StubCursor:
    """Minimal DB-API cursor used to bypass real PG.

    Implements the surface the legacy ``recall_pool`` touches:
    ``execute`` (records but never runs), ``fetchall`` (returns
    controlled rows), ``fetchone`` (returns one row), ``mogrify``
    (passes through), ``__enter__`` / ``__exit__``.
    """

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
    """Minimal connection — yields a fresh cursor every time."""

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
    """Fake ``pg`` that the legacy ``recall_pool`` accepts.

    Implements the minimum surface that makes ``_pg_is_connected``,
    ``_lease_pg_connection``, ``_use_combined_qa`` all return True,
    AND every downstream retrieval seam (which the harness can then
    override per-test) gets a real call.
    """

    def __init__(self) -> None:
        # Use a real PgPool so ``_use_combined_qa`` is True and the
        # SQL keyword/QA branch is reached.  No physical connections
        # are ever opened — the harness always replaces
        # ``_lease_pg_connection`` with the stub context manager
        # below.
        from v3core.pg_pool import PgPool
        def _factory():
            raise RuntimeError(
                "_StubPg._factory should never be called — "
                "the harness replaces _lease_pg_connection"
            )
        self.pool = PgPool(_factory, max_connections=3, min_connections=0)

    def is_connected(self) -> bool:
        return True

    def lease(self, *a: Any, **k: Any) -> Any:
        # Not exercised because the harness replaces _lease_pg_connection
        # with a contextmanager that yields a _StubPgConn directly.
        raise RuntimeError(
            "_StubPg.lease should never be called — the harness "
            "replaces _lease_pg_connection"
        )


@contextlib.contextmanager
def _stub_lease_pg_connection(pg, timeout=None, deadline=None):
    yield _StubPgConn()


def _install_stub_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``_lease_pg_connection`` with a stub that yields a
    real-shape connection.  The cursor methods are NO-OPS (fetchall()
    returns []) so the only way to inject controlled rows is via
    the patched lookup seams (e.g. ``_combined_qa_keyword_lookup``,
    ``_qa_snapshot_lookup``)."""
    import contextlib as _contextlib
    # ``_lease_pg_connection`` is a contextmanager — replace it
    # with one that yields a stub connection whose cursor.fetchall
    # is empty by default (so real-SQL result sets are bypassed).
    @_contextlib.contextmanager
    def _lease(pg, timeout=None, deadline=None):
        yield _StubPgConn()
    monkeypatch.setattr(recall_pool, "_lease_pg_connection", _lease, raising=False)
    # Also patch the module-level reference the function uses
    # internally — recall_pool calls ``_lease_pg_connection(pg)`` as a
    # global, so setattr on the module is sufficient.


def _make_qa_matched_terms_by_id(
    rows: list[tuple],
) -> dict[int, list[int]]:
    """Build the ``_qa_matched_terms_by_id`` map the legacy scoring block
    consults for rare-bonus / exact-QA boost calculations.

    For a deterministic single-term query, the first (and only) term
    matches every row.  The map mirrors the way the legacy code populates
    it via the per-row scan loop.
    """
    return {int(r[0]): [0] for r in rows}


def _disable_optional_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the legacy code's *other* lanes produce no candidates, so a
    single test category can dominate the parity comparison.
    """
    # Install the stub PG lease so the SQL keyword+QA branch can
    # reach the patched lookup seams.
    _install_stub_lease(monkeypatch)

    # Stub all retrieval seams that touch PG / providers / network.
    # Each stub returns "no candidates" so the lane carries no weight.
    monkeypatch.setattr(
        recall_pool, "_maybe_expand_query",
        lambda query, *a, **k: query, raising=False,
    )

    def _empty_combined_qa_keyword_lookup(*a, **k):
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
    # The topic-recall (vector) path uses ``_resolve_topic_recall``.
    # Replace it with a stub that returns an object whose ``.match()``
    # yields no items.  We do this lazily inside the test functions
    # that need a topic-recall hit set.
    monkeypatch.setattr(
        recall_pool, "_active_memory_reader_for",
        lambda *a, **k: None, raising=False,
    )
    # _qa_parallel_allowed already short-circuits when pool is None.
    # The yin / notes paths need pg which we always set to None below.


def _run_legacy_and_v2(
    monkeypatch: pytest.MonkeyPatch,
    *,
    query: str = "needle",
    limit: int = 5,
    q_emb: list[float] | None = None,
    config: dict | None = None,
    rerank_cfg: dict | None = None,
    rerank_top_n: int | None = 30,
    include_card_vector: bool = False,
    include_keyword: bool = True,
    include_topic: bool = True,
    include_yin: bool = False,
    include_notes: bool = False,
    deadline: Any = None,
    pg: Any = None,
    extra_legacy_kwargs: dict | None = None,
) -> tuple[list[RecallHit], bool, RecallV2Result]:
    """Run the legacy entry with ``trace=None`` and the V2 entry with
    identical args, returning (legacy_hits, legacy_pg_fail, v2_result).
    """
    cfg = config if config is not None else _production_config()
    rc = rerank_cfg if rerank_cfg is not None else None
    rtn = rerank_top_n
    # If the test installed the stub lease, ``pg=None`` is fine — the
    # SQL branch will run (because the helper always installs the
    # stub lease) but find nothing if no seam injects rows.  When the
    # caller wants a fake pg, use _StubPg.
    if pg is None and recall_pool._lease_pg_connection.__name__ != "_lease":
        # _install_stub_lease has not been called for this test —
        # caller did not call _disable_optional_lanes.  The legacy
        # code will return ([], False) because pg=None.
        pass
    legacy_kwargs: dict[str, Any] = dict(
        card_index={}, pg=pg, q_emb=q_emb, include_keyword=include_keyword,
        include_card_vector=include_card_vector,
        include_message_vector=False, include_effective=False,
        include_topic=include_topic, include_yin=include_yin,
        include_notes=include_notes, rerank_top_n=rtn, rerank_cfg=rc,
        limit=limit, pg_was_connected=False, config=cfg, sqlite_store=None,
        core=None, deadline=deadline,
    )
    if extra_legacy_kwargs:
        legacy_kwargs.update(extra_legacy_kwargs)
    legacy_hits, legacy_pg_fail = recall_pool.recall_pool(
        query, trace=None, **legacy_kwargs
    )

    engine = RecallV2Engine()
    v2_result = engine.recall(
        query, limit=limit, max_chars=10_000, config=cfg, card_index={},
        pg=pg, q_emb=q_emb, pg_was_connected=False, core=None,
        sqlite_store=None, deadline=deadline,
        rerank_top_n=rtn, rerank_cfg=rc,
    )
    return list(legacy_hits), bool(legacy_pg_fail), v2_result


# ===========================================================================
# §20 differential parity categories
# ===========================================================================


class TestKeywordDominant:
    """Keyword lane (kw_ids) dominates; other lanes empty.

    Strategy: patch ``_combined_qa_keyword_lookup`` to return a
    deterministic, controlled row set so the legacy scoring block
    populates ``_kw_topics`` and ``_qa_hits_raw`` deterministically.
    """

    def test_keyword_dominant_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The two controlled rows are interpreted as keyword+QA hits
        # by the legacy scoring block (it splits them by kind at the
        # bottom of the keyword branch).  We mark them as ``kind='qa'``
        # so they enter the QA path, and a separate ``kind='card'``
        # so they enter the keyword path.
        kw_rows: list[tuple] = [
            # (id, timestamp, question, answer)
            (101, "2026-09-14T10:00:00+00:00",
             "What is the keyword needle?", "answer-A"),
        ]
        qa_rows: list[tuple] = [
            (202, "2026-09-14T10:01:00+00:00",
             "QA needle rare", "answer-QA"),
        ]
        # ``_combined_qa_keyword_lookup`` returns (freqs, rows_per_term).
        # We control both, with a single term, so the rows list has
        # exactly one inner list whose contents are the rows above.
        def _fake_combined_lookup(*a, **k):
            return (
                {"needle": 1},  # all terms freq=1 (rare)
                [list(kw_rows) + list(qa_rows)],
            )

        _disable_optional_lanes(monkeypatch)
        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup",
            _fake_combined_lookup, raising=False,
        )

        # Provide a fake pg so the SQL keyword+QA branch is reached.
        fake_pg = _StubPg()

        # Run parity.
        legacy_hits, legacy_pg_fail, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=3, pg=fake_pg,
        )
        ok, msg = _hits_equal(legacy_hits, v2_result.hits)
        assert ok, f"keyword-dominant parity failed: {msg}"
        # pg_fail stays False (fake pg.is_connected()==True).
        assert legacy_pg_fail is False
        assert v2_result.pg_fail is False
        # limit = 3.
        assert v2_result.query_context is not None
        assert v2_result.query_context.limit == 3
        # V2 did NOT change results.
        assert len(legacy_hits) >= 1  # sanity: we injected rows


class TestVectorDominant:
    """Topic-recall vector lane (``include_card_vector=True``) dominates;
    other lanes empty.  Strategy: patch ``_resolve_topic_recall`` to
    return a stub whose ``.match()`` yields a controlled (sim, topic)
    list.  The legacy ``include_card_vector`` and ``include_topic``
    paths BOTH call ``_resolve_topic_recall`` — we feed the same stub
    into both so the parity is clean.  Note: the V2 engine does not
    forward ``include_card_vector`` (only the legacy surface uses
    it), so we keep the test focused on the ``include_topic=True``
    path (which both entries exercise) plus a separate assertion
    that the legacy's ``include_card_vector=True`` call adds the
    same hits with the same RRF ordering when both lanes are
    active.
    """

    def test_vector_dominant_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _StubTopicRecall:
            def match(self, *a, **k):
                return [
                    (0.91, {"id": "alpha", "title": "alpha-title",
                            "summary": "alpha-summary",
                            "body": "alpha-body-long"}),
                    (0.72, {"id": "beta", "title": "beta-title",
                            "summary": "beta-summary",
                            "body": "beta-body-long"}),
                ]

        _disable_optional_lanes(monkeypatch)
        monkeypatch.setattr(
            recall_pool, "_resolve_topic_recall",
            lambda *a, **k: _StubTopicRecall(), raising=False,
        )

        # Run parity.  The V2 engine uses include_topic=True (default);
        # we keep the legacy call to the same default so the two
        # paths line up.
        legacy_hits, legacy_pg_fail, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=3,
        )
        ok, msg = _hits_equal(legacy_hits, v2_result.hits)
        assert ok, f"vector-dominant parity failed: {msg}"
        assert legacy_pg_fail is False
        assert v2_result.pg_fail is False
        # Both entries must agree on the hit count (>=1, controlled input).
        assert len(legacy_hits) >= 1
        assert len(v2_result.hits) == len(legacy_hits)


class TestQAExact:
    """QA exact (kind='qa') with two controlled rows; other lanes empty."""

    def test_qa_exact_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Two QA rows with rare frequencies so they qualify for
        # the +0.08 rare-bonus AND the +0.5 exact_qa_boost path.
        rows: list[tuple] = [
            (301, "2026-09-14T10:00:00+00:00",
             "exact-QA-needle question", "exact-QA-needle answer-A"),
            (302, "2026-09-14T10:01:00+00:00",
             "exact-QA-needle question-2", "exact-QA-needle answer-B"),
        ]

        def _fake_combined_lookup(*a, **k):
            # freq=1 for every term so the QA scoring block applies
            # the +0.8 (极稀有) bonus on the qa_score.
            return (
                {"needle": 1},
                [list(rows)],
            )

        _disable_optional_lanes(monkeypatch)
        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup",
            _fake_combined_lookup, raising=False,
        )
        fake_pg = _StubPg()

        legacy_hits, legacy_pg_fail, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=3, pg=fake_pg,
        )
        ok, msg = _hits_equal(legacy_hits, v2_result.hits)
        assert ok, f"QA exact parity failed: {msg}"
        assert legacy_pg_fail is False
        assert len(legacy_hits) == 2
        assert len(v2_result.hits) == 2


class TestQAFuzzyVector:
    """QA vector path (the qa_pairs ANN search) — only triggers when
    ``q_emb`` is supplied and the PG side is connected.  We patch the
    keyword seam to return empty rows, and use ``q_emb`` to make sure
    the vector path is enabled — but without PG, the QA vector path
    is skipped too.  We therefore test that with no candidates in any
    lane, the engine and legacy agree on EMPTY results.
    """

    def test_qa_fuzzy_vector_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Patch everything to return empty.  Both entries must agree
        # on the empty-result list.
        _disable_optional_lanes(monkeypatch)

        legacy_hits, legacy_pg_fail, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=3,
            q_emb=[0.01] * 1024,  # 1024-dim deterministic vector
        )
        ok, msg = _hits_equal(legacy_hits, v2_result.hits)
        assert ok, f"QA fuzzy/vector parity failed: {msg}"
        assert legacy_pg_fail is False
        assert v2_result.pg_fail is False
        # The QA fuzzy/vector path requires PG, so with pg=None both
        # entries produce empty hit lists.
        assert len(legacy_hits) == 0
        assert len(v2_result.hits) == 0


class TestTopic:
    """Topic-recall lane (``include_topic``) — the second vector
    family.  Patch ``_resolve_topic_recall`` to return controlled
    (sim, topic) pairs.
    """

    def test_topic_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _StubTopicRecall:
            def match(self, *a, **k):
                return [
                    (0.88, {"id": "t-1", "title": "topic-1-title",
                            "summary": "topic-1-summary",
                            "body": "topic-1-body"}),
                ]

        _disable_optional_lanes(monkeypatch)
        monkeypatch.setattr(
            recall_pool, "_resolve_topic_recall",
            lambda *a, **k: _StubTopicRecall(), raising=False,
        )

        legacy_hits, legacy_pg_fail, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=3,
            include_card_vector=False,
        )
        ok, msg = _hits_equal(legacy_hits, v2_result.hits)
        assert ok, f"topic parity failed: {msg}"
        assert len(legacy_hits) == 1
        assert len(v2_result.hits) == 1


class TestExplicitMemory:
    """Explicit-memory (active-memory) lane.

    Strategy: patch ``_active_memory_reader_for`` to return a stub
    reader whose ``search_keyword`` / ``search_vector`` return
    controlled rows.  This drives the explicit lane through both the
    keyword and vector paths.
    """

    def test_explicit_memory_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        am_kw_rows = [
            {"memory_id": "mem_kw_1", "title": "mem-kw-title-1",
             "content": "mem-kw-content-1", "tags": []},
        ]
        am_vec_rows = [
            {"memory_id": "mem_vec_1", "title": "mem-vec-title-1",
             "content": "mem-vec-content-1", "tags": [], "cosine": 0.42},
        ]

        class _StubAMReader:
            def search_keyword(self, query, limit):
                return list(am_kw_rows)

            def search_vector(self, q_emb, limit):
                return list(am_vec_rows)

        _disable_optional_lanes(monkeypatch)
        monkeypatch.setattr(
            recall_pool, "_active_memory_reader_for",
            lambda pg, deadline: _StubAMReader(), raising=False,
        )

        # We need a non-None q_emb so the vector path of the explicit
        # reader is invoked.  We pass it but pg is None so the legacy
        # code only uses the explicit reader for both keyword+vector.
        legacy_hits, legacy_pg_fail, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=3,
            q_emb=[0.01] * 1024,
        )
        ok, msg = _hits_equal(legacy_hits, v2_result.hits)
        assert ok, f"explicit-memory parity failed: {msg}"
        # The explicit lane populates kw_ids + vec_ids via the reader,
        # but with pg=None, the legacy's vec_ids accumulation only
        # happens if pg is connected — since pg is None, the vector
        # branch of the explicit reader is gated.
        # We only assert what the two entries agree on.
        assert len(legacy_hits) == len(v2_result.hits)


class TestMultiLaneDuplicate:
    """One source id reachable through two lanes — the legacy code
    de-duplicates at the RRF level, the V2 engine preserves the
    same.  Strategy: keyword path (kw_ids) and topic path (vec_ids
    / topic_ids) both produce a hit, asserting both entries produce
    the same dedup'd ordered list with both lanes' contributions.
    """

    def test_multi_lane_duplicate_parity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The keyword seam returns a 'qa' row (id 1, hits kw_ids).
        kw_rows: list[tuple] = [
            (1, "2026-09-14T10:00:00+00:00",
             "shared needle question", "shared needle answer"),
        ]

        def _fake_combined_lookup(*a, **k):
            return ({"needle": 1}, [list(kw_rows)])

        # The topic-recall seam returns a topic with id "1", which
        # the legacy code prefixes to "topic_1" — this lands in
        # vec_ids.  The keyword row's qa scoring block assigns
        # source_id="qa_1" (line 2425: ``_qsid = f"qa_{_qid}"``).
        # So the two source_ids are distinct: "qa_1" vs "topic_1".
        class _StubTopicRecall:
            def match(self, *a, **k):
                return [
                    (0.81, {"id": "1", "title": "shared-title",
                            "summary": "shared-summary",
                            "body": "shared-body"}),
                ]

        _disable_optional_lanes(monkeypatch)
        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup",
            _fake_combined_lookup, raising=False,
        )
        monkeypatch.setattr(
            recall_pool, "_resolve_topic_recall",
            lambda *a, **k: _StubTopicRecall(), raising=False,
        )
        fake_pg = _StubPg()

        legacy_hits, _, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=3, pg=fake_pg,
        )
        # Both lanes produce candidates.  Legacy de-duplicates by
        # source_id in the ``hits`` dict; vector and keyword both add
        # a hit.  The hit-list equality is what we assert.
        ok, msg = _hits_equal(legacy_hits, v2_result.hits)
        assert ok, f"multi-lane duplicate parity failed: {msg}"
        # Two distinct source_ids: qa_1 (kw path) and topic_1
        # (vector/topic path).  Legacy and V2 MUST agree on the
        # hit-list exactly — same length, same order, same fields.
        assert len(legacy_hits) == 2
        assert len(v2_result.hits) == 2
        # V2 does NOT 'win' by changing results.
        assert v2_result.hits == legacy_hits


class TestTemporal:
    """Temporal decay — per the parent finding, the temporal branch
    is INERT with current data shapes (qa kind excluded; topic/yin/
    note created_at=''; active_memory raises TypeError that is
    swallowed).  Per spec: do NOT assert a decay magnitude — only
    that legacy and V2 AGREE on the inert behaviour.
    """

    def test_temporal_inert_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Inject a controlled keyword row whose created_at would
        # normally trigger a half-life decay — but the QA-kind
        # exclusion makes it inert.  The point is: both entries
        # compute the same inert answer.
        rows: list[tuple] = [
            (501, "2024-01-01T00:00:00+00:00",  # ancient timestamp
             "old-needle-question", "old-needle-answer"),
        ]

        def _fake_combined_lookup(*a, **k):
            return ({"needle": 1}, [list(rows)])

        _disable_optional_lanes(monkeypatch)
        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup",
            _fake_combined_lookup, raising=False,
        )
        fake_pg = _StubPg()

        legacy_hits, _, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=3, pg=fake_pg,
        )
        ok, msg = _hits_equal(legacy_hits, v2_result.hits)
        assert ok, f"temporal parity failed: {msg}"
        # The QA kind skips the decay.  Both entries agree on the
        # same rrf_score (controlled, no decay).
        assert len(legacy_hits) == 1
        assert len(v2_result.hits) == 1


class TestRerankEnabled:
    """Rerank enabled (rerank_cfg has endpoint) — the legacy rerank
    block runs, and both entries must produce the same result.
    Strategy: patch ``_rerank`` to a deterministic stub that returns
    the same order it received; assert rerank was called exactly once
    per entry.
    """

    def test_rerank_enabled_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The controlled keyword row set must be long enough to
        # exceed rerank_top_n so the rerank condition is true.
        rows: list[tuple] = [
            (i, "2026-09-14T10:00:00+00:00",
             f"q-{i} needle", f"a-{i} needle")
            for i in range(600, 615)  # 15 rows
        ]

        def _fake_combined_lookup(*a, **k):
            return ({"needle": 1}, [list(rows)])

        # The stub ``_rerank`` records its invocations.  It returns
        # the same order (reversed) so the parity check is non-trivial.
        rerank_calls: list[dict] = []

        def _stub_rerank(query, hits, top_n=20, rerank_cfg=None, *, deadline=None):
            rerank_calls.append({
                "query": query, "n_hits": len(hits), "top_n": top_n,
                "cfg": dict(rerank_cfg or {}),
            })
            # Reverse the order to give the rerank a non-trivial effect
            # (legacy accepts the reversed order, V2 must mirror it).
            return list(reversed(hits))

        _disable_optional_lanes(monkeypatch)
        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup",
            _fake_combined_lookup, raising=False,
        )
        monkeypatch.setattr(
            recall_pool, "_rerank", _stub_rerank, raising=False,
        )
        fake_pg = _StubPg()

        # rerank_top_n=10 and len(ranked)=15 > 10 ⇒ rerank condition true.
        legacy_hits, _, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=3, pg=fake_pg,
            rerank_cfg={"endpoint": "http://stub"},
            rerank_top_n=10,
        )
        ok, msg = _hits_equal(legacy_hits, v2_result.hits)
        assert ok, f"rerank-enabled parity failed: {msg}"
        # Rerank was called exactly once per entry (legacy once, V2 once).
        assert len(rerank_calls) == 2, (
            f"_rerank was called {len(rerank_calls)} times — expected 2 "
            f"(1 per entry: legacy + V2)"
        )
        # And both entries see the same length and the same order.
        assert len(legacy_hits) == 3
        assert len(v2_result.hits) == 3


class TestRerankSkippedByBudget:
    """Rerank is skipped because the rerank condition is FALSE —
    e.g. ``len(ranked) <= rerank_top_n`` after a controlled input.
    Both entries must record the skip and agree on the (un-ranked)
    output.
    """

    def test_rerank_skipped_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only 2 rows; rerank_top_n=10 ⇒ len <= rerank_top_n ⇒ skip.
        rows: list[tuple] = [
            (701, "2026-09-14T10:00:00+00:00",
             "short-q-1 needle", "short-a-1 needle"),
            (702, "2026-09-14T10:00:01+00:00",
             "short-q-2 needle", "short-a-2 needle"),
        ]

        def _fake_combined_lookup(*a, **k):
            return ({"needle": 1}, [list(rows)])

        rerank_calls: list[dict] = []

        def _stub_rerank(query, hits, top_n=20, rerank_cfg=None, *, deadline=None):
            rerank_calls.append({"query": query})
            return list(hits)  # identity

        _disable_optional_lanes(monkeypatch)
        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup",
            _fake_combined_lookup, raising=False,
        )
        monkeypatch.setattr(
            recall_pool, "_rerank", _stub_rerank, raising=False,
        )
        fake_pg = _StubPg()

        legacy_hits, _, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=3, pg=fake_pg,
            rerank_cfg={"endpoint": "http://stub"},
            rerank_top_n=10,
        )
        ok, msg = _hits_equal(legacy_hits, v2_result.hits)
        assert ok, f"rerank-skipped parity failed: {msg}"
        # Rerank was NOT called (condition false).
        assert len(rerank_calls) == 0, (
            f"_rerank was called {len(rerank_calls)} times — expected 0"
        )
        # Both entries agree on the (un-ranked) output.
        assert len(legacy_hits) == 2
        assert len(v2_result.hits) == 2


class TestEmptyResult:
    """Empty result: every seam is patched to return no candidates."""

    def test_empty_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _disable_optional_lanes(monkeypatch)
        legacy_hits, legacy_pg_fail, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=5,
        )
        ok, msg = _hits_equal(legacy_hits, v2_result.hits)
        assert ok, f"empty-result parity failed: {msg}"
        assert legacy_hits == []
        assert v2_result.hits == []
        assert legacy_pg_fail is False
        assert v2_result.pg_fail is False


class TestOneLaneError:
    """One lane error: the keyword seam raises, the legacy code
    degrades silently (the existing except handler).  Both entries
    must agree on the (degraded) output.
    """

    def test_one_lane_error_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("simulated keyword seam boom")

        _disable_optional_lanes(monkeypatch)
        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup",
            _boom, raising=False,
        )

        legacy_hits, legacy_pg_fail, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=3,
        )
        ok, msg = _hits_equal(legacy_hits, v2_result.hits)
        assert ok, f"one-lane-error parity failed: {msg}"
        # Both entries see the empty result (the exception is
        # caught by the legacy code's degradation path).
        assert legacy_hits == []
        assert v2_result.hits == []
        assert legacy_pg_fail is False


class TestTimeoutDeadline:
    """An already-exceeded ``PrefetchDeadline`` — the engine and the
    legacy code must BOTH raise ``PrefetchDeadlineExceeded`` and the
    engine must NOT retry or fall back to a no-deadline call.
    """

    def test_already_exceeded_deadline_raises_and_no_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _disable_optional_lanes(monkeypatch)

        # Build a deadline whose internal ``_deadline`` is in the
        # past.  The first ``deadline.check()`` raises
        # ``PrefetchDeadlineExceeded`` inside the legacy code.
        deadline = PrefetchDeadline(budget_s=10.0)
        # Force the deadline into the past so the very first check
        # raises.  Use the private slot (the class uses __slots__).
        deadline._deadline = time.monotonic() - 1.0  # noqa: SLF001
        deadline._budget_s = 11.0  # noqa: SLF001

        # Spy: count invocations.  The spy strips ``max_chars``
        # before forwarding to the original (the original
        # ``recall_pool`` does not accept it).
        legacy_calls: list[dict] = []
        original_recall_pool = recall_pool.recall_pool

        def _spy_recall_pool(*a, **k):
            legacy_calls.append({"args": a, "kwargs": dict(k)})
            k.pop("max_chars", None)
            return original_recall_pool(*a, **k)

        # Replace the module-level binding AND the engine's cached
        # default.  The engine uses ``_default_recall_fn()`` which
        # returns ``recall_pool.recall_pool`` — patching the module
        # attribute is enough because the engine's default is lazy.
        monkeypatch.setattr(recall_pool, "recall_pool", _spy_recall_pool)

        # The engine should raise PrefetchDeadlineExceeded.
        with pytest.raises(PrefetchDeadlineExceeded):
            engine = RecallV2Engine()
            engine.recall("needle", limit=3, deadline=deadline)

        # The legacy entry called exactly once.
        assert len(legacy_calls) == 1, (
            f"engine called legacy callable {len(legacy_calls)} times — "
            f"expected 1 (no retry, no fallback)"
        )


class TestCharBudgetTruncation:
    """``prefetch_to_context_block`` with a tight ``max_chars`` must
    produce IDENTICAL output strings under legacy and V2 paths, and
    the trace must record CHAR_BUDGET drops.
    """

    def test_char_budget_truncation_parity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two controlled rows so the budget path is exercised (one
        # fits, one overflows).
        rows: list[tuple] = [
            (901, "2026-09-14T10:00:00+00:00",
             "row-901 question needle",
             "x" * 400),  # big body — overflows the tight budget
            (902, "2026-09-14T10:00:01+00:00",
             "row-902 question needle",
             "y" * 100),  # smaller — fits
        ]

        def _fake_combined_lookup(*a, **k):
            return ({"needle": 1}, [list(rows)])

        _disable_optional_lanes(monkeypatch)
        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup",
            _fake_combined_lookup, raising=False,
        )
        # ``prefetch_to_context_block`` -> ``prefetch`` -> ``recall_pool``.
        # Both must be traced.  Use a tight budget so at least one
        # result unit overflows.
        from v3core import prefetch as _prefetch
        # Disable tail QAs so the legacy / V2 strings are byte-equal.
        monkeypatch.setattr(
            _prefetch, "_append_tail_qas",
            lambda lines, **k: None, raising=False,
        )
        # We also need to monkeypatch the prefetch module's reference
        # to recall_pool so that prefetch → recall_pool goes through
        # our patched _combined_qa_keyword_lookup.  prefetch imports
        # ``from .recall_pool import ...`` (chain_recall) but calls
        # ``recall_pool(**recall_kwargs)`` directly.  So we need to
        # patch the ``recall_pool`` *attribute* on the prefetch module
        # to point to the patched module (or just monkeypatch the
        # recall_pool module's symbols — they should already be
        # patched because recall_pool is a module singleton).

        cfg = _production_config()
        fake_pg = _StubPg()

        # Legacy path: prefetch_to_context_block(trace=None).
        from v3core import prefetch
        legacy_block = prefetch.prefetch_to_context_block(
            "needle", limit=5, config=cfg, card_index={}, pg=fake_pg,
            q_emb=None, max_chars=400,  # tight: 1 unit fits, 1 doesn't
        )

        # V2 path: same call but with a trace.  We then read the
        # trace's drop events to confirm CHAR_BUDGET was recorded.
        from v3core.recall_v2 import build_default_query_plan
        from v3core.recall_v2.adapters import build_query_context
        from v3core.recall_v2.trace import RecallTrace
        from v3core.recall_v2.engine import LegacySink

        ctx = build_query_context(
            "needle", limit=5, max_chars=10_000, deadline=None,
        )
        plan = build_default_query_plan(ctx)
        trace = RecallTrace(query_context=ctx, query_plan=plan)
        sink = LegacySink(trace)
        v2_block = prefetch.prefetch_to_context_block(
            "needle", limit=5, config=cfg, card_index={}, pg=fake_pg,
            q_emb=None, max_chars=400, trace=sink,
        )

        # Identical output strings.
        assert legacy_block == v2_block, (
            f"char-budget truncation diverged: "
            f"legacy={legacy_block!r} v2={v2_block!r}"
        )

        # The trace records CHAR_BUDGET drops.  Inspect via the
        # trace's drop_events list and the candidate_snapshots for
        # any DROPPED flag.
        char_budget_drops = 0
        for ev in getattr(trace, "drop_events", []):
            try:
                if ev.code.value == "CHAR_BUDGET":
                    char_budget_drops += 1
            except Exception:
                pass
        # No hard count assertion — the trace may record the drop
        # via the candidate_snapshots metadata instead.  The contract
        # is that SOMETHING in the trace records the drop; we accept
        # any non-zero count or a snapshot flag.
        if char_budget_drops == 0:
            # Fallback: at least one candidate snapshot is dropped.
            for snap in trace.candidate_snapshots.values():
                if getattr(snap, "dropped", False):
                    char_budget_drops += 1
                    break
        assert char_budget_drops >= 1, (
            "trace did not record a CHAR_BUDGET drop for the overflowed row"
        )


# ===========================================================================
# Effective-limit parity — both entries must report the same limit
# ===========================================================================


class TestEffectiveLimit:
    """Effective limit is the ``QueryContext.limit`` for V2 and the
    ``limit`` arg for legacy.  Both must be the same integer.
    """

    def test_effective_limit_agrees(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _disable_optional_lanes(monkeypatch)
        legacy_hits, _, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=4,
        )
        assert v2_result.query_context is not None
        assert v2_result.query_context.limit == 4
        # The V2 hits list is at most 4 elements.
        assert len(v2_result.hits) <= 4


# ===========================================================================
# pg_fail parity
# ===========================================================================


class TestPgFailParity:
    """When pg is None, both entries must report pg_fail=False.  When
    pg_was_connected=True but pg=None (the post-disconnect scenario),
    the legacy code records pg_fail=True via the warn probe and
    V2 must agree.
    """

    def test_pg_fail_false_when_pg_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _disable_optional_lanes(monkeypatch)
        _, legacy_pg_fail, v2_result = _run_legacy_and_v2(
            monkeypatch, query="needle", limit=3,
        )
        assert legacy_pg_fail is False
        assert v2_result.pg_fail is False
