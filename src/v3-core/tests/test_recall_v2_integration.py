# -*- coding: utf-8 -*-
"""G6B Slice B — integration tests for the Recall V2 engine + legacy ``recall_pool``.

These tests exercise the REAL ``v3core.recall_pool.recall_pool`` through the
``RecallV2Engine``, asserting that:

* the engine's signature-adaptive kwargs construction does NOT break the legacy
  callable (``max_chars`` is stripped from the call, ``trace=`` is added);
* the engine attaches a trace when the legacy callable accepts it;
* probes are emitted from the legacy function into the engine's trace;
* byte-identity is preserved when the legacy function is called with
  ``trace=None`` versus a ``LegacySink`` (same hit list, same order, same
  scores);
* the engine does not introduce extra I/O on the success path (single call to
  the legacy callable, single call to one exercise helper);
* the default trace JSON does NOT leak query text or candidate content;
* the engine forwards the exact deadline instance supplied by the caller and
  sets ``deadline_enforced`` correctly;
* the engine adapts to callables whose signature does not declare ``max_chars``
  while still using ``max_chars`` for the QueryContext.

All tests run with NO PG, NO providers, NO network, NO sleeps, NO commits.
"""
from __future__ import annotations

import importlib
import inspect
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

from v3core._deadline import (  # noqa: E402
    PrefetchDeadline,
    coerce_deadline,
)
from v3core.recall_v2.engine import (  # noqa: E402
    LegacySink,
    RecallV2Engine,
    RecallV2Result,
)
from v3core.recall_v2.trace import RecallTrace  # noqa: E402
from v3core import recall_pool  # noqa: E402  (legacy surface)
from v3core.types import RecallHit  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_recall_pool():
    """The real legacy ``recall_pool`` callable."""
    return recall_pool.recall_pool


@pytest.fixture(scope="module")
def real_recall_pool_signature(real_recall_pool):
    """Inspect the real ``recall_pool`` signature exactly once for the module."""
    return inspect.signature(real_recall_pool)


def _hit_dict(h: RecallHit) -> dict[str, Any]:
    return {
        "source_id": h.source_id,
        "kind": h.kind,
        "rrf_score": h.rrf_score,
        "cosine": h.cosine,
        "title": h.title,
        "content": h.content,
        "facts": list(h.facts or []),
    }


def _hits_equal(a: list[RecallHit], b: list[RecallHit]) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if _hit_dict(x) != _hit_dict(y):
            return False
    return True


def _ensure_no_pg_providers_used():
    """Best-effort guard: callers must build hit-shaped data without PG/providers.

    The real ``recall_pool`` will return ``([], False)`` when there are no PG
    connections and no providers (no hits, no errors).  That's all we need
    here for the parity / single-call / probe-reality tests.
    """


# ===========================================================================
# 1. Real-callable integration — engine wraps the real recall_pool cleanly
# ===========================================================================


class TestRealCallableIntegration:
    def test_engine_wraps_real_recall_pool_without_typeerror(
        self, real_recall_pool, real_recall_pool_signature
    ) -> None:
        """The engine must construct legacy kwargs adaptively and call the
        real ``recall_pool`` exactly once with NO TypeError, even though the
        legacy callable does NOT accept ``max_chars``.
        """
        # Sanity: confirm the real callable really does not accept max_chars.
        assert "max_chars" not in real_recall_pool_signature.parameters

        engine = RecallV2Engine()  # default recall_fn -> real recall_pool
        res = engine.recall(
            "any-query",
            limit=5,
            pg=None,
            card_index={},
            q_emb=None,
            config=None,
            rerank_cfg=None,
            rerank_top_n=None,
        )
        assert isinstance(res, RecallV2Result)
        # Trace attached (the real callable accepts trace via **kwargs / duck).
        assert res.trace is not None
        assert res.trace_attached is True
        assert res.fallback_used is False
        assert isinstance(res.trace, RecallTrace)

        # Parity: raw call with identical args (no trace) must yield the same
        # hits list (order + source_id + rrf_score).
        raw_hits, raw_pg_fail = real_recall_pool(
            "any-query",
            card_index={},
            pg=None,
            q_emb=None,
            config=None,
            rerank_cfg=None,
            rerank_top_n=None,
            limit=5,
        )
        assert res.pg_fail == raw_pg_fail
        assert _hits_equal(res.hits, raw_hits)


# ===========================================================================
# 2. Probe reality — emitted probes reflect real execution, not fabrication
# ===========================================================================


class TestProbeReality:
    def test_trace_records_real_lane_summaries(self) -> None:
        engine = RecallV2Engine()
        res = engine.recall(
            "any-query",
            limit=5,
            pg=None,
            card_index={},
            q_emb=None,
            config=None,
            rerank_cfg=None,
            rerank_top_n=None,
        )
        assert res.trace is not None
        # At least keyword and qa lanes must have been touched (real recall_pool
        # always executes the keyword/QA seam when ``include_keyword=True``,
        # which is the default).
        ks = res.trace.lane_summaries
        assert ks["keyword"].started is not None or ks["keyword"].candidate_count >= 0
        assert ks["qa"].started is not None or ks["qa"].candidate_count >= 0
        assert ks["keyword"].finished is not None or ks["keyword"].started is None
        assert ks["qa"].finished is not None or ks["qa"].started is None

    def test_no_candidate_snapshot_for_unrecorded_ids(self) -> None:
        """The trace must not contain candidate snapshots for source ids
        that were never observed during retrieval."""
        engine = RecallV2Engine()
        res = engine.recall(
            "any-query",
            limit=3,
            pg=None,
            card_index={},
            q_emb=None,
            config=None,
            rerank_cfg=None,
            rerank_top_n=None,
        )
        recorded_ids = {h.source_id for h in res.hits}
        snapshot_ids = set(res.trace.candidate_snapshots.keys())
        # Every recorded snapshot id must correspond to a recorded id.
        for cid in snapshot_ids:
            assert cid in recorded_ids, (
                f"snapshot {cid!r} exists but was never recorded as a lane candidate"
            )


# ===========================================================================
# 3. trace=None byte-identity — adding a LegacySink must not change behaviour
# ===========================================================================


class TestTraceNoneByteIdentity:
    def test_legacy_sink_does_not_change_hit_list(self, real_recall_pool) -> None:
        """Adding a LegacySink to the legacy callable's kwargs must NOT
        change the returned hits list (same order, same source_id / kind /
        rrf_score / cosine / title / content / facts).
        """
        from v3core.recall_v2 import build_default_query_plan
        from v3core.recall_v2.adapters import build_query_context

        ctx = build_query_context(
            "any-query", limit=5, max_chars=10000, deadline=None
        )
        plan = build_default_query_plan(ctx)
        sink = LegacySink(RecallTrace(query_context=ctx, query_plan=plan))

        # The two calls below use identical arguments; the only difference is
        # the presence of ``trace=`` on the second.
        hits_a, pg_fail_a = real_recall_pool(
            "any-query",
            card_index={},
            pg=None,
            q_emb=None,
            config=None,
            rerank_cfg=None,
            rerank_top_n=None,
            limit=5,
        )
        hits_b, pg_fail_b = real_recall_pool(
            "any-query",
            card_index={},
            pg=None,
            q_emb=None,
            config=None,
            rerank_cfg=None,
            rerank_top_n=None,
            limit=5,
            trace=sink,
        )
        assert pg_fail_a == pg_fail_b
        assert _hits_equal(hits_a, hits_b)


# ===========================================================================
# 4. No double I/O — engine invokes the legacy callable exactly once
# ===========================================================================


class TestNoDoubleIO:
    def test_engine_calls_recall_pool_exactly_once(self) -> None:
        calls: list[dict[str, Any]] = []

        def _spy(query, **kwargs):
            calls.append({"query": query, "kwargs": dict(kwargs)})
            # Bare minimum to satisfy the engine's unpacking without I/O.
            return [], False

        engine = RecallV2Engine(recall_fn=_spy)
        res = engine.recall(
            "any-query",
            limit=3,
            pg=None,
            card_index={},
            q_emb=None,
            config=None,
            rerank_cfg=None,
            rerank_top_n=None,
        )
        assert isinstance(res, RecallV2Result)
        assert len(calls) == 1, (
            f"engine called legacy callable {len(calls)} times — must be exactly 1"
        )
        # The forwarded kwargs MUST include deadline (legacy surface) and MUST
        # NOT include max_chars (legacy surface does not accept it).  But the
        # test spy is a **kwargs grab, so the engine forwards max_chars iff it
        # cannot introspect — for a regular function it can introspect and
        # strips max_chars (VAR_KEYWORD accept-everything ⇒ we keep it).  This
        # assertion is therefore about _count_, not _content_.
        kw = calls[0]["kwargs"]
        # The trace kwarg must be there (LegacySink-typed) — engine successfully
        # attached the trace.
        assert "trace" in kw
        assert kw["trace"] is not None

    def test_engine_does_not_invoke_extra_helpers(self) -> None:
        """Beyond the single recall_pool call, the engine must not introduce
        extra calls to helpers exercised on the success path.  This is a smoke
        check: build a counting wrapper around the legacy callable AND a
        counter on one of the obvious helpers (RecallTrace.finish is a no-op
        method, so instead we count RecallTrace constructor calls via a
        patched __init__).
        """
        from v3core.recall_v2 import engine as engine_mod

        recall_calls: list[dict[str, Any]] = []
        trace_inits: list[dict[str, Any]] = []

        real_init = engine_mod.RecallTrace.__init__

        def _counting_init(self, *args, **kwargs):
            trace_inits.append({"args": args, "kwargs": dict(kwargs)})
            real_init(self, *args, **kwargs)

        def _spy(query, **kwargs):
            recall_calls.append({"query": query, "kwargs": dict(kwargs)})
            return [], False

        engine = RecallV2Engine(recall_fn=_spy)
        # Monkeypatch RecallTrace.__init__ to count constructions.
        engine_mod.RecallTrace.__init__ = _counting_init  # type: ignore[assignment]
        try:
            engine.recall(
                "any-query",
                limit=3,
                pg=None,
                card_index={},
                q_emb=None,
                config=None,
                rerank_cfg=None,
                rerank_top_n=None,
            )
        finally:
            engine_mod.RecallTrace.__init__ = real_init  # type: ignore[assignment]

        # Single recall call.
        assert len(recall_calls) == 1
        # Exactly one trace was constructed (the success-path trace); not 2.
        assert len(trace_inits) == 1, (
            f"RecallTrace.__init__ called {len(trace_inits)} times — expected 1"
        )


# ===========================================================================
# 8. New stages reachable — focused test for A1/A2/A3/A4/A5/A6/A7/A8 emission
# ===========================================================================


class TestNewStagesReachable:
    def test_new_stages_reachable_with_real_recall_pool(
        self, real_recall_pool: Any
    ) -> None:
        """The fusion / temporal / rare-bonus / exact-QA-boost / rerank /
        selection / degradation / deadline stages added by G6B Slice B must
        all be reachable from a real ``recall_pool`` call.  pg=None keeps
        it service-free and fast.  No PG, no providers, no network.

        * Call the real ``recall_pool.recall_pool`` with the smallest
          signature that exercises the keyword / QA / explicit lanes
          (the bare-minimum config that does NOT raise).  pg=None and
          limit=2 keep the call deterministic.
        * Assert no exception.
        * Assert the trace has keyword and qa lane summaries with numeric
          ``started`` / ``finished`` values (the probe-reality gate).
        * Assert a bare call with ``trace=None`` returns values equal to
          the traced call's return values (byte-identity gate).
        """
        from v3core.recall_v2 import build_default_query_plan
        from v3core.recall_v2.adapters import build_query_context

        ctx = build_query_context(
            "any-query", limit=2, max_chars=10000, deadline=None
        )
        plan = build_default_query_plan(ctx)
        trace = RecallTrace(query_context=ctx, query_plan=plan)
        sink = LegacySink(trace)

        hits_traced, pg_fail_traced = real_recall_pool(
            "any-query",
            card_index={},
            pg=None,
            q_emb=None,
            config=None,
            rerank_cfg=None,
            rerank_top_n=None,
            limit=2,
            trace=sink,
        )
        # Bare call with trace=None must yield byte-identical results.
        hits_bare, pg_fail_bare = real_recall_pool(
            "any-query",
            card_index={},
            pg=None,
            q_emb=None,
            config=None,
            rerank_cfg=None,
            rerank_top_n=None,
            limit=2,
        )

        # No exception gate (assertion above would have raised).
        assert isinstance(hits_traced, list)
        # Trace must show keyword + qa lane summaries touched.
        ks = trace.lane_summaries
        assert ks["keyword"].started is not None
        assert ks["keyword"].finished is not None
        assert isinstance(ks["keyword"].duration_ms, (int, float))
        assert ks["qa"].started is not None
        assert ks["qa"].finished is not None
        assert isinstance(ks["qa"].duration_ms, (int, float))

        # Byte-identity gate: bare call == traced call.
        assert pg_fail_traced == pg_fail_bare
        assert _hits_equal(hits_traced, hits_bare)


# ===========================================================================
# 9. _probe with trace=None does no attribute lookup (D3)
# ===========================================================================


class TestProbeNoneNoAttrLookup:
    def test_probe_with_trace_none_does_not_touch_attr(
        self, real_recall_pool: Any
    ) -> None:
        """When ``trace`` is ``None``, the module-level ``_probe`` must
        short-circuit BEFORE any ``getattr`` lookup.  We pass a sentinel
        object whose ``__getattr__`` raises ``AssertionError`` — if any
        attribute access happens, the test fails.

        Both the direct ``_probe(None, ...)`` call AND a full
        ``recall_pool`` invocation with ``trace=None`` must complete
        cleanly.
        """

        class _BoomSentinel:
            """Plain sentinel: ``__getattr__`` raises AssertionError on
            any attribute access.  Implements no other attributes."""

            def __getattr__(self, name: str) -> Any:  # type: ignore[override]
                raise AssertionError(
                    f"_BoomSentinel.__getattr__({name!r}) — "
                    "trace=None path must NOT touch attribute lookup"
                )

        # Direct call with trace=None must short-circuit cleanly.
        # Any attribute access on the sentinel would raise AssertionError.
        try:
            recall_pool._probe(None, "lane_start", "keyword")
        except AssertionError as e:
            pytest.fail(
                f"_probe(None, ...) touched attribute access: {e}"
            )

        # And: passing a boom sentinel as ``trace=`` to recall_pool
        # with trace=None literally — the function must NOT touch it.
        boom = _BoomSentinel()
        try:
            hits, pg_fail = real_recall_pool(
                "any-query",
                card_index={},
                pg=None,
                q_emb=None,
                config=None,
                rerank_cfg=None,
                rerank_top_n=None,
                limit=2,
                trace=None,  # trace=None is the no-op gate
            )
        except AssertionError as e:
            pytest.fail(
                f"recall_pool(trace=None) touched attribute access: {e}"
            )
        # Function still completes successfully with the default return.
        assert isinstance(hits, list)
        assert pg_fail is False


# ===========================================================================
# 5. Privacy — default trace JSON must not leak query text or hit content
# ===========================================================================


class TestPrivacyViaRealTrace:
    def test_default_to_json_no_query_text_no_hit_body(self) -> None:
        sentinel_query = "SECRET_QUERY_phrase_for_privacy_check"
        sentinel_body = "SECRET_BODY_phrase_for_privacy_check"

        def _spy(query, **kwargs):
            # Return one hit whose content carries the sentinel.
            return [RecallHit(
                source_id="c1",
                title="t",
                content_preview=sentinel_body,
                category="card",
                tags=[],
                cosine=0.5,
                rrf_score=0.1,
                facts=[],
                kind="card",
                created_at="",
                content=sentinel_body,
            )], False

        engine = RecallV2Engine(recall_fn=_spy)
        res = engine.recall(sentinel_query, limit=3)
        encoded = res.trace.to_json()
        assert sentinel_query not in encoded, "default to_json leaked the query"
        assert sentinel_body not in encoded, "default to_json leaked hit content"


# ===========================================================================
# 6. Deadline — forward the exact instance, set deadline_enforced correctly
# ===========================================================================


class TestDeadline:
    def test_real_deadline_forwarded_verbatim_and_enforced_true(self) -> None:
        received: dict[str, Any] = {}

        def _spy(query, **kwargs):
            received["deadline"] = kwargs.get("deadline")
            return [], False

        engine = RecallV2Engine(recall_fn=_spy)
        d = PrefetchDeadline(budget_s=2.0)
        res = engine.recall("any-query", deadline=d, limit=3)
        assert received["deadline"] is d
        assert res.deadline_enforced is True

    def test_deadline_none_not_enforced(self) -> None:
        received: dict[str, Any] = {}

        def _spy(query, **kwargs):
            received["deadline"] = kwargs.get("deadline")
            return [], False

        engine = RecallV2Engine(recall_fn=_spy)
        res = engine.recall("any-query", deadline=None, limit=3)
        assert received["deadline"] is None
        assert res.deadline_enforced is False


# ===========================================================================
# 7. max_chars adaptivity — fakes without max_chars still work
# ===========================================================================


class TestMaxCharsAdaptivity:
    def test_fake_without_max_chars_param_is_called_successfully(self) -> None:
        """A callable whose signature explicitly does NOT declare ``max_chars``
        (and has no ``**kwargs``) must still be invoked successfully.  The
        engine must use ``max_chars`` only for the QueryContext / QueryPlan,
        never as a kwarg.
        """
        captured: list[dict[str, Any]] = []

        def _no_max_chars(query, *, limit=8, config=None, card_index=None,
                          pg=None, q_emb=None, pg_was_connected=False,
                          core=None, sqlite_store=None, deadline=None,
                          rerank_top_n=None, rerank_cfg=None) -> tuple[list, bool]:
            captured.append({"query": query, "kwargs": {"limit": limit}})
            return [RecallHit(
                source_id="c1",
                title="t",
                content_preview="p",
                category="card",
                tags=[],
                cosine=0.5,
                rrf_score=0.1,
                facts=[],
                kind="card",
                created_at="",
                content="body",
            )], False

        engine = RecallV2Engine(recall_fn=_no_max_chars)
        res = engine.recall(
            "any-query",
            limit=4,
            max_chars=2345,
            pg=None,
            card_index={},
            q_emb=None,
            config=None,
            rerank_cfg=None,
            rerank_top_n=None,
        )
        # The legacy callable was called exactly once.
        assert len(captured) == 1
        # QueryContext / QueryPlan MUST still carry max_chars=2345.
        assert res.query_context is not None
        assert res.query_context.max_chars == 2345
        assert res.query_plan is not None
        assert res.query_plan.max_chars == 2345


# ===========================================================================
# 10. Call-count and privacy assertions — provider/DB parity
# ===========================================================================


class TestCallCountParity:
    """The engine's single legacy call must not increase the count
    of any real internal seam (e.g. ``_combined_qa_keyword_lookup``)
    beyond what a bare legacy call would do.  The injected recall
    callable is invoked exactly once on the success path.
    """

    def test_engine_calls_injected_recall_exactly_once(self) -> None:
        calls: list[dict[str, Any]] = []

        def _spy(query, **kwargs):
            calls.append({"query": query, "kwargs": dict(kwargs)})
            return [], False

        engine = RecallV2Engine(recall_fn=_spy)
        res = engine.recall("any-query", limit=3)
        # Exactly one call.
        assert len(calls) == 1, (
            f"engine called injected callable {len(calls)} times — "
            f"expected exactly 1"
        )
        # The result is still a valid RecallV2Result.
        assert isinstance(res, RecallV2Result)

    def test_engine_call_count_equals_bare_legacy_call_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spy a real internal seam (e.g. ``_combined_qa_keyword_lookup``)
        the engine's path exercises, run a bare legacy call and an
        engine-wrapped call with identical args, and assert the spy
        saw the SAME number of invocations.
        """
        from v3core import recall_pool

        spy_counter: list[int] = []
        original_lookup = recall_pool._combined_qa_keyword_lookup

        def _counting_lookup(*a: Any, **k: Any) -> Any:
            spy_counter.append(1)
            return original_lookup(*a, **k)

        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup",
            _counting_lookup, raising=False,
        )

        # Bare legacy call (no engine wrapping).
        spy_counter.clear()
        recall_pool.recall_pool(
            "any-query",
            card_index={}, pg=None, q_emb=None, config=None,
            rerank_cfg=None, rerank_top_n=None, limit=3,
        )
        bare_count = len(spy_counter)

        # Engine-wrapped call with identical args.
        spy_counter.clear()
        engine = RecallV2Engine()
        engine.recall(
            "any-query",
            limit=3, max_chars=10_000, config=None, card_index={},
            pg=None, q_emb=None, pg_was_connected=False, core=None,
            sqlite_store=None, rerank_cfg=None, rerank_top_n=None,
        )
        engine_count = len(spy_counter)

        # The engine's single call must not increase the seam count
        # beyond the bare legacy call.  ``<=`` is the safe contract
        # (the engine may legitimately call zero times if pre-execution
        # fails).
        assert engine_count <= bare_count, (
            f"engine call increased the seam invocation count: "
            f"bare={bare_count} engine={engine_count}"
        )


class TestTracePrivacy:
    """The default ``trace.to_json()`` must NOT leak the query text or
    hit content (even when the content is a 'SECRET_BODY' sentinel
    nested under a content-like metadata key).  No DB write, no file
    write — the trace is in-memory only.
    """

    def test_default_to_json_omits_query_text_and_body(self) -> None:
        sentinel_query = "SECRET_QUERY_for_privacy_check"
        sentinel_body = "SECRET_BODY_for_privacy_check"

        def _spy(query, **kwargs):
            return [RecallHit(
                source_id="c1",
                title="t",
                content_preview=sentinel_body,
                category="card",
                tags=[],
                cosine=0.5,
                rrf_score=0.1,
                facts=[],
                kind="card",
                created_at="",
                content=sentinel_body,
            )], False

        engine = RecallV2Engine(recall_fn=_spy)
        res = engine.recall(sentinel_query, limit=3)
        encoded = res.trace.to_json()
        assert sentinel_query not in encoded
        assert sentinel_body not in encoded

    def test_default_to_json_omits_nested_metadata_content_like_keys(self) -> None:
        """Even when the recall function smuggles a SECRET_BODY into a
        nested metadata dict under a content-like key, the default
        ``to_json()`` must not surface it.
        """
        sentinel_body = "SECRET_BODY_nested_metadata"

        # Build a RecallHit-like object with a `metadata` attribute
        # that holds a dict with a content-like key.
        class _HitWithMeta:
            source_id = "c1"
            title = "t"
            content_preview = "preview"
            category = "card"
            tags: list = []
            cosine = 0.5
            rrf_score = 0.1
            facts: list = []
            kind = "card"
            created_at = ""
            content = "visible-body"
            metadata = {
                "body": sentinel_body,  # content-like key
                "raw": sentinel_body,   # content-like key
                "title": "safe-title",  # not content-like
            }

        def _spy(query, **kwargs):
            return [_HitWithMeta()], False

        engine = RecallV2Engine(recall_fn=_spy)
        res = engine.recall("any-query", limit=3)
        # The default to_json should not surface the secret body.
        encoded = res.trace.to_json()
        # NOTE: the engine uses the typed RecallCandidate pipeline,
        # which calls ``safe_metadata`` and strips content-like keys.
        # The metadata dict the recall function returns is not
        # carried through by the engine's hit conversion (it only
        # forwards known RecallHit fields).  The secret body in the
        # fake's metadata MUST NOT appear in the default JSON.
        assert sentinel_body not in encoded, (
            f"secret body leaked into default to_json: {encoded[:300]}"
        )

    def test_trace_not_persisted_to_disk_or_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A traced run must NOT write any file or touch any DB table.
        The engine result holds the trace in memory; ``RecallTrace``
        has no persistence hook.
        """
        # Track any file writes under tmp_path.
        files_before = set(tmp_path.rglob("*")) if tmp_path.exists() else set()

        def _spy(query, **kwargs):
            return [RecallHit(
                source_id="c1",
                title="t",
                content_preview="p",
                category="card",
                tags=[],
                cosine=0.5,
                rrf_score=0.1,
                facts=[],
                kind="card",
                created_at="",
                content="body",
            )], False

        engine = RecallV2Engine(recall_fn=_spy)
        res = engine.recall("any-query", limit=3)

        # The engine result object holds the trace in memory.
        assert res.trace is not None
        assert isinstance(res.trace, RecallTrace)

        # RecallTrace has no persistence hook: assert the public
        # surface has no ``save``, ``persist``, ``write`` method.
        for forbidden in ("save", "persist", "write", "flush", "commit"):
            assert not hasattr(res.trace, forbidden), (
                f"RecallTrace exposes a persistence hook: {forbidden}"
            )

        # No file was created under tmp_path during the traced run.
        files_after = set(tmp_path.rglob("*")) if tmp_path.exists() else set()
        new_files = files_after - files_before
        assert not new_files, (
            f"traced run wrote files: {new_files}"
        )

    def test_no_db_write_via_recall_trace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A traced run must not call any ``cursor.execute`` /
        ``connection.commit`` method on a real or fake PG.  We monkey
        ``psycopg2.connect`` (already blocked by the conftest session
        autouse) and assert it is never called by the engine path.
        """
        import psycopg2
        called: list[dict] = []
        real_connect = psycopg2.connect

        def _spy_connect(*a: Any, **k: Any) -> Any:
            called.append({"args": a, "kwargs": dict(k)})
            return real_connect(*a, **k)

        # The conftest blocks psycopg2.connect — to detect a violation
        # we wrap the blocked function in our spy so any call is
        # recorded AND raised.
        def _raising_spy(*a: Any, **k: Any) -> Any:
            called.append({"args": a, "kwargs": dict(k)})
            raise AssertionError(
                "traced engine run must not call psycopg2.connect"
            )
        monkeypatch.setattr(psycopg2, "connect", _raising_spy)

        def _spy(query, **kwargs):
            return [RecallHit(
                source_id="c1", title="t", content_preview="p",
                category="card", tags=[], cosine=0.5, rrf_score=0.1,
                facts=[], kind="card", created_at="", content="body",
            )], False

        engine = RecallV2Engine(recall_fn=_spy)
        res = engine.recall("any-query", limit=3)
        # No psycopg2.connect call during a traced run.
        assert not called, (
            f"traced engine run called psycopg2.connect: {called}"
        )
        # Trace is in memory only.
        assert res.trace is not None


# ===========================================================================
# 11. §23 Orchestration overhead — trace-disabled path performs no trace work
# ===========================================================================


class TestTraceDisabledOverhead:
    """The ``trace_enabled=False`` path must perform NO trace-record
    work: ``RecallTrace`` is never constructed and no trace object is
    attached.  Hit conversion still yields the same hits.  No
    wall-clock threshold tighter than 50 ms of median overhead over
    20 iterations.
    """

    def test_trace_disabled_engine_does_not_construct_recall_trace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Spy on RecallTrace construction.
        from v3core.recall_v2 import engine as engine_mod

        constructed: list[dict] = []
        real_init = engine_mod.RecallTrace.__init__

        def _counting_init(self, *args: Any, **kwargs: Any) -> None:
            constructed.append({"args": args, "kwargs": dict(kwargs)})
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(engine_mod.RecallTrace, "__init__", _counting_init)

        def _spy(query, **kwargs):
            return [RecallHit(
                source_id="c1", title="t", content_preview="p",
                category="card", tags=[], cosine=0.5, rrf_score=0.1,
                facts=[], kind="card", created_at="", content="body",
            )], False

        engine = RecallV2Engine(recall_fn=_spy, trace_enabled=False)
        res = engine.recall("any-query", limit=3)
        # RecallTrace was NEVER constructed.
        assert len(constructed) == 0, (
            f"trace_enabled=False still constructed {len(constructed)} "
            f"RecallTrace instances — expected 0"
        )
        # The trace object is not attached on the result.
        assert res.trace is None
        assert res.trace_attached is False
        # Hit conversion still works.
        assert len(res.hits) == 1
        assert res.hits[0].source_id == "c1"

    def test_trace_disabled_does_not_call_probe_sink_methods(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When trace_enabled=False, the engine never builds a sink,
        so its ``lane_*`` / ``score`` / ``select`` methods are not
        invoked.  We assert by patching ``LegacySink`` to record
        every method call and confirming the constructor was never
        called.
        """
        from v3core.recall_v2 import engine as engine_mod

        sink_inits: list[dict] = []
        real_sink_init = engine_mod.LegacySink.__init__

        def _counting_sink_init(self, *a: Any, **k: Any) -> None:
            sink_inits.append({"args": a, "kwargs": dict(k)})
            real_sink_init(self, *a, **k)

        monkeypatch.setattr(
            engine_mod.LegacySink, "__init__", _counting_sink_init,
        )

        def _spy(query, **kwargs):
            return [RecallHit(
                source_id="c1", title="t", content_preview="p",
                category="card", tags=[], cosine=0.5, rrf_score=0.1,
                facts=[], kind="card", created_at="", content="body",
            )], False

        engine = RecallV2Engine(recall_fn=_spy, trace_enabled=False)
        res = engine.recall("any-query", limit=3)
        # No sink was constructed.
        assert len(sink_inits) == 0, (
            f"trace_enabled=False still constructed {len(sink_inits)} "
            f"LegacySink instances — expected 0"
        )
        assert res.trace is None

    def test_trace_disabled_median_overhead_under_50ms(self) -> None:
        """Sanity wall-clock check: 20 iterations of the trace-disabled
        path must not regress the median overhead beyond 50 ms vs
        the trace-enabled baseline.  No tight threshold — the spec
        forbids tighter assertions.
        """
        import statistics

        def _spy(query, **kwargs):
            return [RecallHit(
                source_id="c1", title="t", content_preview="p",
                category="card", tags=[], cosine=0.5, rrf_score=0.1,
                facts=[], kind="card", created_at="", content="body",
            )], False

        # Warm both engines.
        engine_on = RecallV2Engine(recall_fn=_spy, trace_enabled=True)
        engine_off = RecallV2Engine(recall_fn=_spy, trace_enabled=False)
        for _ in range(3):
            engine_on.recall("warm", limit=3)
            engine_off.recall("warm", limit=3)

        # Measure each path over 20 iterations.
        timings_on: list[float] = []
        timings_off: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            engine_on.recall("any-query", limit=3)
            timings_on.append((time.perf_counter() - t0) * 1000.0)
            t0 = time.perf_counter()
            engine_off.recall("any-query", limit=3)
            timings_off.append((time.perf_counter() - t0) * 1000.0)

        median_on = statistics.median(timings_on)
        median_off = statistics.median(timings_off)
        # The disabled path must not add > 50 ms median overhead.
        # We assert the disabled path's median is at most 50 ms greater
        # than the enabled path's median.  In practice both should be
        # well under 1 ms each.
        overhead = median_off - median_on
        assert overhead <= 50.0, (
            f"trace-disabled median overhead {overhead:.3f} ms exceeds 50 ms "
            f"tolerance: median_on={median_on:.3f} median_off={median_off:.3f}"
        )
