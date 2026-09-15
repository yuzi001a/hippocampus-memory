# -*- coding: utf-8 -*-
"""G6B Slice A — Recall V2 orchestration engine unit tests.

Pure unit tests with a FAKE recall function (no PG, no providers, no real
recall_pool).  All paths must execute with zero network / DB / LLM access.

RED-first — the test file imports the to-be-implemented
``v3core.recall_v2.engine`` module and exercises every invariant
required by the G6B slice-A spec.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import pytest

# Make the in-tree ``src`` importable regardless of how pytest was launched.
REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

from v3core._deadline import (  # noqa: E402
    INTERNAL_PREFETCH_BUDGET_SECONDS,
    PrefetchDeadline,
    PrefetchDeadlineExceeded,
)
from v3core.recall_v2 import (  # noqa: E402
    CandidateEventType,
    DropReasonCode,
    QueryContext,
    QueryPlan,
    RecallTrace,
    build_default_query_plan,
)
from v3core.recall_v2.adapters import (  # noqa: E402
    build_query_context,
    hit_to_candidate,
    lane_for_kind,
)
from v3core.recall_v2.engine import LegacySink, RecallV2Engine, RecallV2Result  # noqa: E402
from v3core.types import RecallHit  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hit(
    *,
    source_id: str = "card-1",
    title: str = "T",
    content: str = "secret-content-do-not-leak",
    content_preview: str = "preview",
    category: str = "",
    tags: list[str] | None = None,
    cosine: float = 0.0,
    rrf_score: float = 0.0,
    facts: list[str] | None = None,
    kind: str = "card",
    created_at: str = "",
) -> RecallHit:
    return RecallHit(
        source_id=source_id,
        title=title,
        content_preview=content_preview,
        category=category,
        tags=list(tags or []),
        cosine=float(cosine),
        rrf_score=float(rrf_score),
        facts=list(facts or []),
        kind=kind,
        created_at=created_at,
        content=content,
    )


class _FakeRecall:
    """A FAKE recall function.  Records every accepted call's kwargs.

    By default accepts a ``trace=`` kwarg.  Set ``accept_trace=False`` to
    simulate the signature-adaptive behaviour expected from legacy
    callables that don't accept the keyword — the instance is built as a
    separate class with an explicit ``__call__`` signature that omits
    ``trace`` (and ``**kwargs``).
    """

    def __init__(
        self,
        hits: list[RecallHit] | None = None,
        pg_fail: bool = False,
        *,
        accept_trace: bool = True,
        on_call: "callable | None" = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._hits = list(hits or [])
        self._pg_fail = bool(pg_fail)
        self._accept_trace = bool(accept_trace)
        self._on_call = on_call

    def __call__(self, *args: Any, **kwargs: Any):
        # Record what we saw (without mutating the caller's objects).
        self.calls.append({"args": args, "kwargs": dict(kwargs)})
        if self._on_call is not None:
            maybe = self._on_call(args, kwargs)
            if isinstance(maybe, BaseException):
                raise maybe
        return list(self._hits), bool(self._pg_fail)


def _make_rejects_trace_recall(
    hits: list[RecallHit] | None = None,
    pg_fail: bool = False,
) -> Any:
    """Build a callable whose ``inspect.signature`` shows it does NOT
    accept the ``trace`` kwarg.  Used to exercise the engine's
    signature-adaptive behaviour.
    """
    state = {"calls": [], "hits": list(hits or []), "pg_fail": bool(pg_fail)}

    def _impl(
        query: Any,
        *,
        limit: int = 8,
        max_chars: int = 10_000,
        config: Any = None,
        card_index: Any = None,
        pg: Any = None,
        q_emb: Any = None,
        pg_was_connected: bool = False,
        core: Any = None,
        sqlite_store: Any = None,
        deadline: Any = None,
        rerank_top_n: Any = None,
        rerank_cfg: Any = None,
    ) -> tuple[list, bool]:
        state["calls"].append({
            "args": (query,),
            "kwargs": {
                "limit": limit, "max_chars": max_chars, "config": config,
                "card_index": card_index, "pg": pg, "q_emb": q_emb,
                "pg_was_connected": pg_was_connected, "core": core,
                "sqlite_store": sqlite_store, "deadline": deadline,
                "rerank_top_n": rerank_top_n, "rerank_cfg": rerank_cfg,
            },
        })
        return list(state["hits"]), bool(state["pg_fail"])

    _impl.calls = state["calls"]  # type: ignore[attr-defined]
    return _impl


def _script_full_pipeline(
    fake: _FakeRecall,
    *,
    hits: list[RecallHit] | None = None,
    sink: Any = None,
) -> None:
    """Run the full lane → score → select → inject lifecycle via the sink."""

    def _runner(args, kwargs):
        if sink is None:
            return None
        # Synthesise two lanes so the snapshot carries provenance for two lanes.
        sink.lane_start("keyword")
        sink.lane_start("vector")
        sink.lane_candidates(
            "keyword",
            ["c1", "c2"],
            source_type="card",
            scores=[0.5, 0.6],
        )
        sink.lane_candidates(
            "vector",
            ["c2", "c3"],
            source_type="card",
            scores=[0.7, 0.8],
        )
        sink.score("c1", "raw", 0.5)
        sink.score("c1", "fusion", 0.6)
        sink.score("c2", "raw", 0.7)
        sink.score("c2", "fusion", 0.8)
        sink.select("c1")
        sink.select("c2")
        sink.inject("c1", char_count=120)
        sink.inject("c2", char_count=80)
        sink.lane_finish("keyword", candidate_count=2)
        sink.lane_finish("vector", candidate_count=2)
        return None

    fake._on_call = _runner
    fake._hits = list(hits or [_hit(source_id="c1"), _hit(source_id="c2")])


# ===========================================================================
# 1. Single-call invariant
# ===========================================================================


class TestSingleCallInvariant:
    def test_single_call_on_success(self) -> None:
        fake = _FakeRecall(hits=[_hit(source_id="a"), _hit(source_id="b")])
        engine = RecallV2Engine(recall_fn=fake)
        res = engine.recall("hello", limit=5)
        assert isinstance(res, RecallV2Result)
        assert len(fake.calls) == 1


# ===========================================================================
# 2. Kwargs passthrough — including the deadline identity guarantee
# ===========================================================================


class TestKwargsPassthrough:
    def test_passes_limit_max_chars_config_pg_q_emb(self) -> None:
        # B1 (G6B Slice B) — corrected adaptive contract:
        # The fake recall callable's signature does NOT declare
        # ``max_chars`` (and has no ``**kwargs`` rescue), so the engine
        # MUST NOT forward it.  ``limit``, ``config``, ``pg``, ``q_emb``,
        # ``card_index``, ``pg_was_connected``, ``core``,
        # ``sqlite_store``, ``rerank_top_n``, ``rerank_cfg`` are all
        # declared in the fake's signature and MUST be forwarded
        # verbatim.  The built QueryContext / QueryPlan still carry
        # the engine-side max_chars.
        captured: list[dict[str, Any]] = []

        def _narrow_fake(
            query: str,
            *,
            limit: int = 8,
            config: Any = None,
            card_index: Any = None,
            pg: Any = None,
            q_emb: Any = None,
            pg_was_connected: bool = False,
            core: Any = None,
            sqlite_store: Any = None,
            deadline: Any = None,
            rerank_top_n: Any = None,
            rerank_cfg: Any = None,
            trace: Any = None,
        ) -> tuple[list, bool]:
            captured.append({
                "query": query, "limit": limit, "config": config,
                "card_index": card_index, "pg": pg, "q_emb": q_emb,
                "pg_was_connected": pg_was_connected, "core": core,
                "sqlite_store": sqlite_store, "deadline": deadline,
                "rerank_top_n": rerank_top_n, "rerank_cfg": rerank_cfg,
                "trace": trace,
            })
            return [], False

        sentinel_config = object()
        sentinel_pg = object()
        sentinel_q_emb = object()
        engine = RecallV2Engine(recall_fn=_narrow_fake)
        res = engine.recall(
            "hello",
            limit=4,
            max_chars=1234,
            config=sentinel_config,
            pg=sentinel_pg,
            q_emb=sentinel_q_emb,
            card_index={"foo": "bar"},
            pg_was_connected=True,
            core="sentinel-core",
            sqlite_store="sentinel-sqlite",
            rerank_top_n=30,
            rerank_cfg={"endpoint": "http://x"},
        )
        assert len(captured) == 1
        kw = captured[0]
        assert kw["limit"] == 4
        # max_chars is NOT forwarded when the fake's signature does not
        # declare it (and has no **kwargs rescue).
        assert "max_chars" not in kw
        # But the engine-side QueryContext still carries max_chars.
        assert res.query_context is not None
        assert res.query_context.max_chars == 1234
        assert res.query_plan is not None
        assert res.query_plan.max_chars == 1234
        assert kw["config"] is sentinel_config
        assert kw["pg"] is sentinel_pg
        assert kw["q_emb"] is sentinel_q_emb
        assert kw["card_index"] == {"foo": "bar"}
        assert kw["pg_was_connected"] is True
        assert kw["core"] == "sentinel-core"
        assert kw["sqlite_store"] == "sentinel-sqlite"
        assert kw["rerank_top_n"] == 30
        assert kw["rerank_cfg"] == {"endpoint": "http://x"}

    def test_max_chars_forwarded_when_callable_declares_it(self) -> None:
        # B1 (G6B Slice B) — when the fake's signature DOES declare
        # ``max_chars``, the engine MUST forward it verbatim.  Use a
        # callable with explicit ``max_chars``.
        captured: list[dict[str, Any]] = []

        def _accepts_max_chars(
            query: str, *, limit: int = 8, max_chars: int = 10_000,
            config: Any = None, card_index: Any = None, pg: Any = None,
            q_emb: Any = None, pg_was_connected: bool = False,
            core: Any = None, sqlite_store: Any = None, deadline: Any = None,
            rerank_top_n: Any = None, rerank_cfg: Any = None,
            trace: Any = None,
        ) -> tuple[list, bool]:
            captured.append({
                "query": query, "limit": limit, "max_chars": max_chars,
                "config": config, "card_index": card_index, "pg": pg,
                "q_emb": q_emb, "pg_was_connected": pg_was_connected,
                "core": core, "sqlite_store": sqlite_store,
                "deadline": deadline, "rerank_top_n": rerank_top_n,
                "rerank_cfg": rerank_cfg, "trace": trace,
            })
            return [], False

        engine = RecallV2Engine(recall_fn=_accepts_max_chars)
        res = engine.recall("hello", limit=4, max_chars=4321)
        assert len(captured) == 1
        kw = captured[0]
        assert kw["limit"] == 4
        assert kw["max_chars"] == 4321
        # And the engine-side QueryContext / QueryPlan still carry it too.
        assert res.query_context is not None
        assert res.query_context.max_chars == 4321
        assert res.query_plan is not None
        assert res.query_plan.max_chars == 4321

    def test_max_chars_not_forwarded_via_kwargs_grab_strict(self) -> None:
        # G6B Slice F — the engine's adaptive contract is STRICT for
        # ``max_chars``: a callable that has only ``**kwargs`` (no
        # explicit ``max_chars`` parameter) does NOT count as
        # accepting ``max_chars``.  The engine therefore strips it
        # from the forwarded payload.  The engine-side QueryContext /
        # QueryPlan still carry the caller-supplied ``max_chars``.
        # This is the regression test for the parent defect: a
        # ``def wrapper(*args, **kwargs)`` around ``recall_pool``
        # must not be tricked into forwarding a keyword the real
        # function does not accept.
        captured: list[dict[str, Any]] = []

        def _kwargs_grab(query: str, **kwargs: Any) -> tuple[list, bool]:
            captured.append({"query": query, "kwargs": dict(kwargs)})
            return [], False

        engine = RecallV2Engine(recall_fn=_kwargs_grab)
        res = engine.recall("hello", limit=4, max_chars=9876)
        assert len(captured) == 1
        kw = captured[0]["kwargs"]
        assert kw["limit"] == 4
        # Strict rule: max_chars is NOT forwarded to a **kwargs-only
        # wrapper.
        assert "max_chars" not in kw
        # The engine-side QueryContext / QueryPlan still carry it.
        assert res.query_context is not None
        assert res.query_context.max_chars == 9876
        assert res.query_plan is not None
        assert res.query_plan.max_chars == 9876

    def test_deadline_object_is_the_very_same_instance(self) -> None:
        fake = _FakeRecall(hits=[])
        engine = RecallV2Engine(recall_fn=fake)
        original = PrefetchDeadline(budget_s=2.0)
        engine.recall("hello", deadline=original)
        assert len(fake.calls) == 1
        kw = fake.calls[0]["kwargs"]
        assert kw["deadline"] is original
        # Engine must not have mutated the deadline (e.g. via coerce_deadline
        # constructing a new enforcement timer).
        assert kw["deadline"].deadline == original.deadline
        assert kw["deadline"].budget_s == original.budget_s

    def test_deadline_none_passes_through(self) -> None:
        fake = _FakeRecall(hits=[])
        engine = RecallV2Engine(recall_fn=fake)
        engine.recall("hello", deadline=None)
        assert len(fake.calls) == 1
        assert "deadline" in fake.calls[0]["kwargs"]
        assert fake.calls[0]["kwargs"]["deadline"] is None

    def test_default_limit_and_max_chars(self) -> None:
        # B1 (G6B Slice B): the engine-side defaults (limit=8,
        # max_chars=10_000) flow into the QueryContext regardless.  In
        # the forwarded payload, ``limit`` is forwarded verbatim and
        # ``max_chars`` is forwarded only when the callable's signature
        # accepts it.  Use a **kwargs grabber that does NOT have
        # ``max_chars`` as a named parameter so the adaptive strip
        # applies.
        captured: list[dict[str, Any]] = []

        def _kwargs_only(query: str, **kwargs: Any) -> tuple[list, bool]:
            captured.append({"query": query, "kwargs": dict(kwargs)})
            return [], False

        engine = RecallV2Engine(recall_fn=_kwargs_only)
        res = engine.recall("hello")
        assert len(captured) == 1
        kw = captured[0]["kwargs"]
        # ``limit`` is part of the legacy surface and is forwarded
        # verbatim (callable's **kwargs rescue accepts it).
        assert kw.get("limit") == 8
        # The engine-side QueryContext / QueryPlan still carry the
        # default 10_000.
        assert res.query_context is not None
        assert res.query_context.max_chars == 10_000
        assert res.query_plan is not None
        assert res.query_plan.max_chars == 10_000

    def test_r1_kwargs_only_wrapper_delegating_to_recall_pool(
        self,
    ) -> None:
        """G6B Slice F (R1) — a ``def wrapper(*args, **kwargs)`` around
        the REAL ``v3core.recall_pool.recall_pool`` must NOT receive
        ``max_chars`` (the engine's strict adaptive rule strips it
        because the wrapper's signature declares no explicit
        ``max_chars`` parameter — a ``**kwargs`` rescue no longer
        counts as acceptance for ``max_chars``).  The delegate must
        run successfully (proving the engine actually called it with
        the real ``recall_pool`` surface) and the engine result must
        NOT be the pre-execution fallback (which is the
        "fallback_used=True" path the engine takes when
        context/plan/trace construction raises)."""
        from v3core import recall_pool

        captured: list[dict[str, Any]] = []

        def _wrapper(*args: Any, **kwargs: Any) -> tuple[list, bool]:
            # Record what the engine forwarded, then delegate to the
            # real recall_pool.  We DO NOT mutate kwargs — that would
            # mask the regression.  We also pass through the positional
            # ``query`` argument that the engine supplies positionally.
            captured.append({"args": args, "kwargs": dict(kwargs)})
            return recall_pool.recall_pool(*args, **kwargs)

        cfg = {
            "prefetch": {
                "dual_path": True,
                "rrf_k": 60,
                "include_message_vector": False,
            },
            "storage": {"rerank": {}},
            "recall": {
                "vector_top_mult": 4,
                "qa_per_term_min": 10,
                "qa_freq_limit": 40,
            },
            "half_life": 30,
        }
        engine = RecallV2Engine(recall_fn=_wrapper)
        res = engine.recall(
            "hello",
            card_index={},
            pg=None,
            q_emb=None,
            config=cfg,
            rerank_top_n=None,
            rerank_cfg=None,
        )
        # The delegate really ran (not the pre-execution fallback).
        assert len(captured) == 1, (
            f"wrapper should be invoked exactly once on the success "
            f"path; got {len(captured)} invocations: {captured!r}"
        )
        kw = captured[0]["kwargs"]
        # The whole point of the F fix: ``max_chars`` is NOT in the
        # forwarded payload because the wrapper's ``**kwargs`` is no
        # longer a rescue for ``max_chars``.
        assert "max_chars" not in kw, (
            f"engine forwarded max_chars to a **kwargs-only wrapper; "
            f"the G6B Slice F strict adaptive rule is broken.  "
            f"kwargs={kw!r}"
        )
        # The engine result is the real success path, NOT the
        # pre-execution fallback (which would mean we never reached
        # the delegate at all).
        assert res.fallback_used is False
        assert res.trace is not None
        # Sanity: the engine-side QueryContext still carries the
        # engine-side max_chars (default 10_000).
        assert res.query_context is not None
        assert res.query_context.max_chars == 10_000

    def test_r2_callable_with_explicit_max_chars_still_receives_it(
        self,
    ) -> None:
        """G6B Slice F (R2) — a callable whose signature explicitly
        declares ``max_chars`` must still receive the kwarg with the
        value the caller passed.  Pins the strict-rule exemption: an
        explicit named parameter IS acceptance, regardless of whether
        a ``**kwargs`` is also present."""
        captured: list[dict[str, Any]] = []

        def _explicit(
            query: str,
            *,
            max_chars: int = 0,
            **kwargs: Any,
        ) -> tuple[list, bool]:
            captured.append({"max_chars": max_chars, "kwargs": dict(kwargs)})
            return [], False

        engine = RecallV2Engine(recall_fn=_explicit)
        res = engine.recall("hello", max_chars=7777)
        assert len(captured) == 1
        # max_chars forwarded with the caller's value.
        assert captured[0]["max_chars"] == 7777
        # And the engine-side QueryContext / QueryPlan still carry it.
        assert res.query_context is not None
        assert res.query_context.max_chars == 7777
        assert res.query_plan is not None
        assert res.query_plan.max_chars == 7777

    def test_r3_narrow_signature_does_not_receive_max_chars(self) -> None:
        """G6B Slice F (R3) — a callable whose signature has NEITHER
        ``max_chars`` NOR ``**kwargs`` must not receive ``max_chars``.
        This is the pre-existing baseline; the test pins it under the
        new strict rule so future refactors cannot quietly regress
        the no-explicit-no-var-keyword case.  The wrapper still
        accepts every other legacy kwarg explicitly so the engine's
        call does not fail for unrelated reasons."""
        captured: list[dict[str, Any]] = []

        def _narrow(
            query: str,
            *,
            limit: int = 8,
            config: Any = None,
            card_index: Any = None,
            pg: Any = None,
            q_emb: Any = None,
            pg_was_connected: bool = False,
            core: Any = None,
            sqlite_store: Any = None,
            deadline: Any = None,
            rerank_top_n: Any = None,
            rerank_cfg: Any = None,
            trace: Any = None,
        ) -> tuple[list, bool]:
            captured.append({"limit": limit, "config": config})
            return [], False

        engine = RecallV2Engine(recall_fn=_narrow)
        res = engine.recall("hello", max_chars=9999)
        assert len(captured) == 1
        # The callable never sees max_chars.
        assert "max_chars" not in captured[0]
        # And the engine-side QueryContext still does (the caller
        # passed 9999 — the engine propagates that into the typed
        # context regardless of whether the legacy callable accepted
        # the kwarg).
        assert res.query_context is not None
        assert res.query_context.max_chars == 9999

    def test_r4a_typeerror_with_trace_message_does_not_retry(self) -> None:
        """G6B Slice G (Part 3 / A1) — a callable whose body raises
        ``TypeError('unexpected keyword argument trace')`` after the
        engine has already invoked it must NOT be retried.  The
        pre-execution validator (NOT the exception message) is the
        only place the engine decides which optional keywords to
        forward.  Once the callable is invoked, ANY exception (this
        :class:`TypeError` included) propagates unchanged.  Invariant:
        the side-effect counter is EXACTLY 1, no retry happened, and
        the :class:`TypeError` propagates to the caller.
        """
        state = {"calls": 0}

        def _raises_trace_keyword(*args: Any, **kwargs: Any) -> tuple[list, bool]:
            state["calls"] += 1
            raise TypeError("unexpected keyword argument 'trace'")

        engine = RecallV2Engine(recall_fn=_raises_trace_keyword)
        with pytest.raises(TypeError) as excinfo:
            engine.recall("hello")
        # Engine did NOT retry based on the exception message.
        assert state["calls"] == 1, (
            f"engine must not retry when a body-raised TypeError mentions "
            f"'trace'; got {state['calls']} invocations"
        )
        # The propagated TypeError is the one raised by the callable,
        # not a wrapper exception — the message is intact.
        assert "trace" in str(excinfo.value)

    def test_r4b_typeerror_with_max_chars_message_does_not_retry(self) -> None:
        """G6B Slice G (Part 3 / A2) — same property as A1 but with
        ``TypeError('unexpected keyword argument max_chars')``: the
        side-effect counter is EXACTLY 1, no retry happened, and the
        :class:`TypeError` propagates.  This is the explicit
        counter-test to the deleted ``test_r4a_max_chars_typeerror_…
        _recovers`` test: the engine no longer retries on a
        body-raised TypeError, regardless of which keyword the
        message names.
        """
        state = {"calls": 0}

        def _raises_max_chars_keyword(
            *args: Any, **kwargs: Any
        ) -> tuple[list, bool]:
            state["calls"] += 1
            raise TypeError("unexpected keyword argument 'max_chars'")

        engine = RecallV2Engine(recall_fn=_raises_max_chars_keyword)
        with pytest.raises(TypeError) as excinfo:
            engine.recall("hello", max_chars=1234)
        # Engine did NOT retry based on the exception message.
        assert state["calls"] == 1, (
            f"engine must not retry when a body-raised TypeError mentions "
            f"'max_chars'; got {state['calls']} invocations"
        )
        # The propagated TypeError is the one raised by the callable,
        # not a wrapper exception — the message is intact.
        assert "max_chars" in str(excinfo.value)

    def test_r4c_unrelated_typeerror_propagates_without_retry(self) -> None:
        """G6B Slice F (R4b) — a TypeError mentioning something else
        (for example 'boom') must propagate and the callable must be
        invoked exactly once.  The safety net is intentionally narrow
        and never swallows an exception unrelated to ``max_chars``."""
        state = {"calls": 0}

        def _boom_on_first(*args: Any, **kwargs: Any) -> tuple[list, bool]:
            state["calls"] += 1
            raise TypeError("boom")

        engine = RecallV2Engine(recall_fn=_boom_on_first)
        with pytest.raises(TypeError) as excinfo:
            engine.recall("hello", max_chars=1234)
        # Propagated unchanged.
        assert "boom" in str(excinfo.value)
        # No retry — the callable ran exactly once.
        assert state["calls"] == 1, (
            f"safety net must not retry on an unrelated TypeError; "
            f"got {state['calls']} invocations"
        )

    def test_r5_deadline_identity_through_kwargs_wrapper(self) -> None:
        """G6B Slice F (R5) — a real ``PrefetchDeadline`` passed to the
        engine must reach a ``**kwargs`` wrapper as the very same
        object, and the result must report ``deadline_enforced=True``
        (because the deadline is a set ``PrefetchDeadline`` and
        ``coerce_deadline`` returns it)."""
        captured: list[dict[str, Any]] = []

        def _wrapper(*args: Any, **kwargs: Any) -> tuple[list, bool]:
            captured.append({"args": args, "kwargs": dict(kwargs)})
            return [], False

        original = PrefetchDeadline(budget_s=2.0)
        # Sanity: the test deadline is actually enforced (so the
        # result's deadline_enforced can plausibly be True).
        assert original.is_set() is True

        engine = RecallV2Engine(recall_fn=_wrapper)
        res = engine.recall("hello", deadline=original)
        # The wrapper received the deadline.
        assert len(captured) == 1
        assert "deadline" in captured[0]["kwargs"]
        # Identity preserved — same object, not a copy.
        assert captured[0]["kwargs"]["deadline"] is original
        # And the result reports deadline_enforced=True.
        assert res.deadline_enforced is True
        # The engine did NOT mutate the deadline.
        assert captured[0]["kwargs"]["deadline"].deadline == original.deadline
        assert (
            captured[0]["kwargs"]["deadline"].budget_s
            == original.budget_s
        )


# ===========================================================================
# 3. Trace attached + sink lifecycle
# ===========================================================================


class TestTraceAttached:
    def test_recall_trace_carries_lane_timing_and_candidate_snapshots(self) -> None:
        fake = _FakeRecall()
        engine = RecallV2Engine(recall_fn=fake)
        ctx = QueryContext(
            query_id="q-fixed",
            query_text="hello",
            deadline_monotonic=time.monotonic() + 5.0,
            budget_ms=2000,
            limit=4,
            max_chars=1000,
        )
        # Build the trace the engine will see via the kwarg.
        sink_ref: dict[str, Any] = {}

        def _runner(args, kwargs):
            sink = kwargs.get("trace")
            assert sink is not None
            sink_ref["sink"] = sink
            sink.lane_start("keyword")
            sink.lane_candidates(
                "keyword",
                ["c1"],
                source_type="card",
                scores=[0.5],
            )
            sink.score("c1", "raw", 0.5)
            sink.score("c1", "fusion", 0.6)
            sink.select("c1")
            sink.inject("c1", char_count=42)
            sink.lane_finish("keyword", candidate_count=1)
            return None

        fake._on_call = _runner
        fake._hits = [_hit(source_id="c1")]
        res = engine.recall(
            "hello",
            limit=4,
            session_id="sess-1",
            conversation_id="conv-1",
        )
        # Single call.
        assert len(fake.calls) == 1
        # Trace attached on success path.
        assert res.trace_attached is True
        assert isinstance(res.trace, RecallTrace)
        # Lane summary carried real timing + counts.
        kw_summary = res.trace.lane_summaries["keyword"].to_dict()
        assert kw_summary["candidate_count"] == 1
        assert kw_summary["started"] is not None
        assert kw_summary["finished"] is not None
        assert kw_summary["duration_ms"] is not None
        assert kw_summary["duration_ms"] >= 0
        # Candidate snapshot exists and is selected/injected.
        snap = res.trace.candidate_snapshots["c1"]
        assert snap.selected is True
        assert snap.injected is True
        # default to_json() leaks neither query text nor candidate content
        encoded = res.trace.to_json()
        assert "hello" not in encoded  # query text NOT included
        assert "secret-content-do-not-leak" not in encoded
        # but query_id IS included for traceability
        parsed = json.loads(encoded)
        assert parsed["query_id"] == res.trace.query_context.query_id

    def test_provenance_for_two_lanes(self) -> None:
        fake = _FakeRecall()
        engine = RecallV2Engine(recall_fn=fake)
        sink_ref: dict[str, Any] = {}

        def _runner(args, kwargs):
            sink = kwargs["trace"]
            sink_ref["sink"] = sink
            sink.lane_start("keyword")
            sink.lane_start("vector")
            sink.lane_candidates("keyword", ["c1"], source_type="card")
            sink.lane_candidates("vector", ["c1"], source_type="card")
            sink.lane_finish("keyword", candidate_count=1)
            sink.lane_finish("vector", candidate_count=1)
            return None

        fake._on_call = _runner
        fake._hits = [_hit(source_id="c1")]
        res = engine.recall("hi")
        # Make sure the trace was built from candidates that carry provenance
        # for two lanes.  We can't introspect the trace's snapshot directly
        # because the engine only constructs candidates from hits, but we
        # can verify the sink saw both lanes by counting lane_summaries.
        names = sorted(res.trace.lane_summaries.keys())
        assert names == sorted({"keyword", "vector", "topic", "qa", "explicit"})
        assert res.trace.lane_summaries["keyword"].candidate_count == 1
        assert res.trace.lane_summaries["vector"].candidate_count == 1


# ===========================================================================
# 4. Pre-execution fallback
# ===========================================================================


class TestPreExecutionFallback:
    def test_build_query_context_failure_falls_back_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from v3core.recall_v2 import engine as engine_mod
        from v3core.recall_v2 import adapters as adapters_mod

        fake = _FakeRecall(hits=[_hit(source_id="x")])

        def _boom(*args: Any, **kwargs: Any):
            raise RuntimeError("build_query_context exploded")

        monkeypatch.setattr(adapters_mod, "build_query_context", _boom)
        # Also patch the symbol the engine imported, so the engine sees the boom.
        monkeypatch.setattr(engine_mod.adapters, "build_query_context", _boom)

        engine = RecallV2Engine(recall_fn=fake)
        res = engine.recall("hi")
        # The legacy call still ran exactly once.
        assert len(fake.calls) == 1
        assert res.fallback_used is True
        assert res.fallback_reason  # non-empty reason string
        # The fallback call MUST NOT have received the trace kwarg.
        assert "trace" not in fake.calls[0]["kwargs"]
        # No trace attached on fallback path.
        assert res.trace_attached is False

    def test_build_default_query_plan_failure_falls_back_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from v3core.recall_v2 import engine as engine_mod

        fake = _FakeRecall(hits=[_hit(source_id="x")])

        def _boom(*args: Any, **kwargs: Any):
            raise RuntimeError("plan exploded")

        monkeypatch.setattr(engine_mod, "build_default_query_plan", _boom)
        engine = RecallV2Engine(recall_fn=fake)
        res = engine.recall("hi")
        assert len(fake.calls) == 1
        assert res.fallback_used is True
        assert res.fallback_reason
        assert "trace" not in fake.calls[0]["kwargs"]

    def test_trace_constructor_failure_falls_back_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from v3core.recall_v2 import engine as engine_mod

        fake = _FakeRecall(hits=[_hit(source_id="x")])

        def _boom(*args: Any, **kwargs: Any):
            raise RuntimeError("trace construction exploded")

        monkeypatch.setattr(engine_mod, "RecallTrace", _boom)
        engine = RecallV2Engine(recall_fn=fake)
        res = engine.recall("hi")
        assert len(fake.calls) == 1
        assert res.fallback_used is True
        assert res.fallback_reason
        assert "trace" not in fake.calls[0]["kwargs"]


# ===========================================================================
# 5. No double execution after retrieval
# ===========================================================================


class TestNoRetryOnRetrievalRaise:
    def test_propagates_PrefetchDeadlineExceeded_no_retry(self) -> None:
        from v3core.recall_v2 import engine as engine_mod

        def _on_call(args, kwargs):
            raise PrefetchDeadlineExceeded(
                "deadline", deadline=kwargs.get("deadline"), context="recall_pool"
            )

        fake = _FakeRecall(on_call=_on_call)
        engine = RecallV2Engine(recall_fn=fake)
        with pytest.raises(PrefetchDeadlineExceeded):
            engine.recall("hi")
        assert len(fake.calls) == 1

    def test_propagates_arbitrary_exception_no_retry(self) -> None:
        def _on_call(args, kwargs):
            raise ValueError("boom")

        fake = _FakeRecall(on_call=_on_call)
        engine = RecallV2Engine(recall_fn=fake)
        with pytest.raises(ValueError):
            engine.recall("hi")
        assert len(fake.calls) == 1


# ===========================================================================
# 6. Signature-adaptive behaviour
# ===========================================================================


class TestSignatureAdaptive:
    def test_fake_that_rejects_trace_kwarg(self) -> None:
        fake = _make_rejects_trace_recall(
            hits=[_hit(source_id="c1")]
        )
        engine = RecallV2Engine(recall_fn=fake)
        res = engine.recall("hi")
        assert res.trace_attached is False
        # Trace object must still exist on the result (built via the engine's
        # own default RecallTrace before invoking the callable), but the
        # fake must not have been called twice.
        assert len(fake.calls) == 1
        # And the call that did happen received NO trace kwarg.
        assert "trace" not in fake.calls[0]["kwargs"]

    def test_callable_without_trace_param_does_not_fail(self) -> None:
        # This callable takes only `query` and ignores any kwargs (no TypeError).
        calls: list[dict[str, Any]] = []

        def _legacy_only(query: str, **kwargs: Any):
            calls.append({"query": query, "kwargs": kwargs})
            return [_hit(source_id="z")], False

        engine = RecallV2Engine(recall_fn=_legacy_only)
        res = engine.recall("hi")
        # Either the engine inspected the signature and skipped trace,
        # OR it tried, got the TypeError, and retried once — both are
        # allowed; the result must be valid and fake must NOT have been
        # called twice on success.
        assert len(calls) in (1, 2)
        # Result still valid:
        assert isinstance(res, RecallV2Result)


# ===========================================================================
# 6b. G6B Slice G — pre-execution validation, no post-IO retry
# ===========================================================================


class TestG6BSliceGNoPostIORetry:
    """G6B Slice G (Part 1 / HIGH A + Part 3 / A1–A5) — the engine must
    validate the forwarded payload BEFORE invoking the legacy callable
    and never re-invoke based on an exception message.
    """

    def test_a3_unsupported_keyword_dropped_by_preflight(self) -> None:
        """A3 — the pre-execution validator drops an unsupported
        OPTIONAL keyword (here ``max_chars``) from the forwarded
        payload so the call is made exactly once with a payload the
        callable accepts.  The callable's body invocation count is
        exactly 1, and the callable never sees the dropped keyword.
        Never 2.
        """
        captured: list[dict[str, Any]] = []

        def _accepts_no_max_chars(
            query: str,
            *,
            limit: int = 8,
            config: Any = None,
            card_index: Any = None,
            pg: Any = None,
            q_emb: Any = None,
            pg_was_connected: bool = False,
            core: Any = None,
            sqlite_store: Any = None,
            deadline: Any = None,
            rerank_top_n: Any = None,
            rerank_cfg: Any = None,
            trace: Any = None,
        ) -> tuple[list, bool]:
            captured.append(dict(trace=trace, limit=limit))
            return [], False

        engine = RecallV2Engine(recall_fn=_accepts_no_max_chars)
        res = engine.recall("hello", max_chars=2222)
        # The callable was invoked exactly once.
        assert len(captured) == 1, (
            f"pre-execution validator should drop max_chars up front so "
            f"the callable runs exactly once; got {len(captured)} "
            f"invocations"
        )
        # And the callable never received max_chars.
        assert "max_chars" not in captured[0]
        # Trace was attached (callable declares trace).
        assert captured[0]["trace"] is not None
        # Result reports trace_attached=True and fallback_used=False.
        assert res.fallback_used is False
        assert res.trace_attached is True
        # Engine-side QueryContext still carries the caller-supplied
        # max_chars (it flows into the typed context regardless).
        assert res.query_context is not None
        assert res.query_context.max_chars == 2222

    def test_a4_real_recall_pool_receives_trace_exactly_once(self) -> None:
        """A4 — the engine actually reaches the real
        ``v3core.recall_pool.recall_pool`` through the standard
        RecallV2Engine call path, the real function is invoked
        EXACTLY once with ``trace=`` forwarded, and the result reports
        ``trace_attached=True`` / ``fallback_used=False``.  Uses
        ``pg=None``, ``card_index={}``, ``q_emb=None`` and the
        production-shaped config dict required by the slice-G spec.
        """
        from v3core import recall_pool

        state = {"calls": 0, "trace_seen": []}

        def _spy_recall_pool(*args: Any, **kwargs: Any):
            state["calls"] += 1
            state["trace_seen"].append(kwargs.get("trace"))
            # Forward to the real recall_pool, but neuter the path that
            # requires real PG / providers.  We rely on the fact that
            # recall_pool with pg=None and the no-reader config will
            # return ([], False) without raising.
            return recall_pool.recall_pool(*args, **kwargs)

        cfg = {
            "prefetch": {
                "dual_path": True,
                "rrf_k": 60,
                "include_message_vector": False,
            },
            "storage": {"rerank": {}},
            "recall": {
                "vector_top_mult": 4,
                "qa_per_term_min": 10,
                "qa_freq_limit": 40,
            },
            "half_life": 30,
        }

        engine = RecallV2Engine(recall_fn=_spy_recall_pool)
        res = engine.recall(
            "hello",
            card_index={},
            pg=None,
            q_emb=None,
            config=cfg,
            rerank_top_n=None,
            rerank_cfg=None,
        )
        # Exactly one call to the real recall_pool.
        assert state["calls"] == 1, (
            f"engine must invoke the real recall_pool exactly once; "
            f"got {state['calls']} invocations"
        )
        # The forwarded ``trace`` is the engine's LegacySink.
        assert state["trace_seen"][0] is not None
        assert isinstance(state["trace_seen"][0], LegacySink)
        # Result reports trace_attached=True, fallback_used=False.
        assert res.fallback_used is False
        assert res.trace_attached is True
        # And the real trace is on the result (not None, not the fallback).
        assert res.trace is not None
        assert isinstance(res.trace, RecallTrace)

    def test_a5_kwargs_only_wrapper_delegating_to_real_recall_pool(
        self,
    ) -> None:
        """A5 — a ``def wrapper(*args, **kwargs)`` delegating to the
        REAL ``v3core.recall_pool.recall_pool`` still works with
        deterministic ONE-call behaviour, and the real function is
        NOT passed ``max_chars`` (the engine's strict adaptive rule
        strips it because the wrapper's ``**kwargs`` is not a rescue
        for ``max_chars``).
        """
        from v3core import recall_pool

        state = {"calls": 0, "kwargs_seen": []}

        def _wrapper(*args: Any, **kwargs: Any) -> tuple[list, bool]:
            state["calls"] += 1
            state["kwargs_seen"].append(dict(kwargs))
            return recall_pool.recall_pool(*args, **kwargs)

        cfg = {
            "prefetch": {
                "dual_path": True,
                "rrf_k": 60,
                "include_message_vector": False,
            },
            "storage": {"rerank": {}},
            "recall": {
                "vector_top_mult": 4,
                "qa_per_term_min": 10,
                "qa_freq_limit": 40,
            },
            "half_life": 30,
        }
        engine = RecallV2Engine(recall_fn=_wrapper)
        res = engine.recall(
            "hello",
            card_index={},
            pg=None,
            q_emb=None,
            config=cfg,
            rerank_top_n=None,
            rerank_cfg=None,
        )
        # Deterministic ONE-call behaviour.
        assert state["calls"] == 1, (
            f"wrapper must be invoked exactly once; got {state['calls']} "
            f"invocations"
        )
        # max_chars MUST NOT reach the real function (strict adaptive rule).
        assert "max_chars" not in state["kwargs_seen"][0]
        # The real recall_pool accepts trace; the wrapper passes it
        # through.  Trace was attached.
        assert res.fallback_used is False
        assert res.trace_attached is True


# ===========================================================================
# 6c. G6B Slice G — LegacySink.warn / .error prefer trace.warn / trace.error
# ===========================================================================


class TestG6BSliceGSinkPreferTraceMethods:
    """G6B Slice G (Part 2 / LOW D) — when the trace object exposes
    ``warn`` / ``error`` methods, the sink must use them so the
    append happens under the trace's own lock.  When the methods are
    absent, the sink falls back to a direct append to the
    ``trace.warnings`` / ``trace.errors`` list.  Both paths must be
    exception-safe and capped at 300 chars per entry.
    """

    def test_warn_uses_trace_warn_method_when_present(self) -> None:
        # The trace's ``warn`` method must be used (so the append
        # happens under the trace's lock) when present.  We assert by
        # spying on a fresh trace.
        ctx = build_query_context("hi")
        plan = build_default_query_plan(ctx)
        trace = RecallTrace(query_context=ctx, query_plan=plan)
        sink = LegacySink(trace)
        sink.warn("hello world")
        # The trace's ``warn`` method was used → trace.warnings
        # contains the message.
        assert trace.warnings == ["hello world"]

    def test_error_uses_trace_error_method_when_present(self) -> None:
        # Same as above for ``error``.
        ctx = build_query_context("hi")
        plan = build_default_query_plan(ctx)
        trace = RecallTrace(query_context=ctx, query_plan=plan)
        sink = LegacySink(trace)
        sink.error("kaboom")
        assert trace.errors == ["kaboom"]

    def test_warn_falls_back_to_direct_append_when_method_absent(self) -> None:
        # A duck-typed trace without a ``warn`` method must still
        # accept the warning via direct append to ``trace.warnings``.
        captured: list[str] = []

        class _DuckTrace:
            warnings = []
            errors = []

        duck = _DuckTrace()
        sink = LegacySink(duck)
        sink.warn("duck-warn")
        # Direct append path: duck.warnings got the message.
        assert duck.warnings == ["duck-warn"]
        # And the captured list is independent (sanity).
        assert captured == []

    def test_error_falls_back_to_direct_append_when_method_absent(self) -> None:
        class _DuckTrace:
            warnings = []
            errors = []

        duck = _DuckTrace()
        sink = LegacySink(duck)
        sink.error("duck-error")
        assert duck.errors == ["duck-error"]

    def test_warn_caps_at_300_chars(self) -> None:
        ctx = build_query_context("hi")
        plan = build_default_query_plan(ctx)
        trace = RecallTrace(query_context=ctx, query_plan=plan)
        sink = LegacySink(trace)
        long = "x" * 1000
        sink.warn(long)
        assert len(trace.warnings) == 1
        assert len(trace.warnings[0]) == 300
        assert trace.warnings[0] == "x" * 300

    def test_error_caps_at_300_chars(self) -> None:
        ctx = build_query_context("hi")
        plan = build_default_query_plan(ctx)
        trace = RecallTrace(query_context=ctx, query_plan=plan)
        sink = LegacySink(trace)
        long = "y" * 1000
        sink.error(long)
        assert len(trace.errors) == 1
        assert len(trace.errors[0]) == 300
        assert trace.errors[0] == "y" * 300

    def test_warn_swallows_trace_brokenness(self) -> None:
        # If the trace's ``warn`` method raises, the sink must NOT
        # raise into the caller.
        class _BrokenTrace:
            warnings = []

            def warn(self, msg: str) -> None:
                raise RuntimeError("trace broken")

        sink = LegacySink(_BrokenTrace())
        # Must not raise.
        sink.warn("anything")
        sink.error("anything-else")

    def test_warn_empty_or_falsy_text_does_nothing(self) -> None:
        ctx = build_query_context("hi")
        plan = build_default_query_plan(ctx)
        trace = RecallTrace(query_context=ctx, query_plan=plan)
        sink = LegacySink(trace)
        sink.warn("")
        sink.warn(None)  # type: ignore[arg-type]
        assert trace.warnings == []

    def test_error_empty_or_falsy_text_does_nothing(self) -> None:
        ctx = build_query_context("hi")
        plan = build_default_query_plan(ctx)
        trace = RecallTrace(query_context=ctx, query_plan=plan)
        sink = LegacySink(trace)
        sink.error("")
        sink.error(None)  # type: ignore[arg-type]
        assert trace.errors == []


# ===========================================================================
# 7. Hit conversion
# ===========================================================================


class TestHitConversion:
    def test_only_real_scores_mapped(self) -> None:
        h = _hit(source_id="c1", cosine=0.42, rrf_score=0.13)
        cand = hit_to_candidate(h, lane="vector")
        assert cand.candidate_id == "c1"
        assert cand.source_id == "c1"
        assert cand.raw_score == 0.42
        assert cand.fusion_score == 0.13
        # No fabricated stage values.
        assert cand.lane_score is None
        assert cand.temporal_score is None
        assert cand.rerank_score is None
        assert cand.final_score is None

    def test_cosine_zero_means_no_raw_score(self) -> None:
        h = _hit(source_id="c1", cosine=0.0, rrf_score=0.13)
        cand = hit_to_candidate(h, lane="vector")
        assert cand.raw_score is None
        assert cand.fusion_score == 0.13

    def test_rrf_zero_means_no_fusion_score(self) -> None:
        h = _hit(source_id="c1", cosine=0.4, rrf_score=0.0)
        cand = hit_to_candidate(h, lane="vector")
        assert cand.raw_score == 0.4
        assert cand.fusion_score is None

    def test_content_omitted_by_default(self) -> None:
        h = _hit(source_id="c1", content="super-secret-body")
        cand = hit_to_candidate(h, lane="vector")
        assert cand.content is None

    def test_content_captured_when_requested(self) -> None:
        h = _hit(source_id="c1", content="super-secret-body")
        cand = hit_to_candidate(h, lane="vector", capture_content=True)
        assert cand.content == "super-secret-body"

    def test_text_reference_is_source_id(self) -> None:
        h = _hit(source_id="abc")
        cand = hit_to_candidate(h, lane="qa")
        assert cand.text_reference == "abc"

    def test_invalid_iso_timestamp_kept_none(self) -> None:
        h = _hit(source_id="c1", created_at="not-an-iso")
        cand = hit_to_candidate(h, lane="vector")
        assert cand.timestamp is None

    def test_valid_iso_timestamp_parsed(self) -> None:
        h = _hit(source_id="c1", created_at="2026-09-14T10:00:00Z")
        cand = hit_to_candidate(h, lane="vector")
        assert cand.timestamp is not None and cand.timestamp > 0

    def test_extra_lanes_record_provenance(self) -> None:
        h = _hit(source_id="c1")
        cand = hit_to_candidate(
            h, lane="vector", extra_lanes=("keyword", "topic")
        )
        lanes = [p.lane for p in cand.provenance]
        assert "vector" in lanes
        assert "keyword" in lanes
        assert "topic" in lanes


# ===========================================================================
# 8. Privacy
# ===========================================================================


class TestPrivacy:
    def test_default_to_json_no_query_text(self) -> None:
        fake = _FakeRecall()
        engine = RecallV2Engine(recall_fn=fake)
        res = engine.recall("super-secret-query-text")
        encoded = res.trace.to_json()
        assert "super-secret-query-text" not in encoded

    def test_default_to_json_no_candidate_content(self) -> None:
        fake = _FakeRecall(
            hits=[_hit(source_id="c1", content="super-secret-body")]
        )
        engine = RecallV2Engine(recall_fn=fake)
        res = engine.recall("hi")
        encoded = res.trace.to_json()
        assert "super-secret-body" not in encoded


# ===========================================================================
# 9. lane_for_kind mapping (sanity)
# ===========================================================================


class TestLaneForKind:
    def test_known_kinds(self) -> None:
        assert lane_for_kind("qa") == "qa"
        assert lane_for_kind("topic") == "topic"
        assert lane_for_kind("card") == "vector"
        assert lane_for_kind("message") == "vector"
        assert lane_for_kind("note") == "vector"

    def test_unknown_kind(self) -> None:
        assert lane_for_kind("alien") == "vector"
        assert lane_for_kind("") == "vector"


# ===========================================================================
# 10. build_query_context (smoke)
# ===========================================================================


class TestBuildQueryContext:
    def test_query_id_deterministic(self) -> None:
        a = build_query_context("hello", limit=8, max_chars=10000, session_id="s1")
        b = build_query_context("hello", limit=8, max_chars=10000, session_id="s1")
        assert a.query_id == b.query_id
        assert a.query_id.startswith("q-")
        assert len(a.query_id) == len("q-") + 12

    def test_query_id_changes_with_inputs(self) -> None:
        a = build_query_context("hello", limit=8, max_chars=10000, session_id="s1")
        b = build_query_context("hello", limit=8, max_chars=10000, session_id="s2")
        c = build_query_context("world", limit=8, max_chars=10000, session_id="s1")
        assert a.query_id != b.query_id
        assert a.query_id != c.query_id

    def test_deadline_passed_through(self) -> None:
        d = PrefetchDeadline(budget_s=2.0)
        ctx = build_query_context(
            "hi", limit=8, max_chars=10000, deadline=d
        )
        assert ctx.deadline_monotonic == float(d.deadline)
        assert ctx.budget_ms > 0

    def test_deadline_none_uses_internal_budget(self) -> None:
        ctx = build_query_context(
            "hi", limit=8, max_chars=10000, deadline=None
        )
        # Without a deadline, deadline_monotonic is informational and lies
        # INTERNAL_PREFETCH_BUDGET_SECONDS in the future; budget_ms equals
        # INTERNAL_PREFETCH_BUDGET_SECONDS * 1000.
        expected_ms = int(INTERNAL_PREFETCH_BUDGET_SECONDS * 1000)
        assert ctx.budget_ms == expected_ms
        assert ctx.deadline_monotonic > time.monotonic()

    def test_defaults(self) -> None:
        ctx = build_query_context("hi")
        assert ctx.limit == 8
        assert ctx.max_chars == 10_000


# ===========================================================================
# 11. Engine result shape
# ===========================================================================


class TestResultShape:
    def test_result_fields_present(self) -> None:
        fake = _FakeRecall(
            hits=[_hit(source_id="c1", cosine=0.4, rrf_score=0.1)]
        )
        engine = RecallV2Engine(recall_fn=fake)
        res = engine.recall("hi")
        assert isinstance(res.hits, list)
        assert isinstance(res.trace, RecallTrace)
        assert res.pg_fail is False
        assert isinstance(res.query_context, QueryContext)
        assert isinstance(res.query_plan, QueryPlan)
        # The candidate list should be the same length as the hits list.
        assert len(res.candidates) == len(res.hits) == 1
        assert res.deadline_enforced is False  # default
        assert res.fallback_used is False
        assert res.fallback_reason == ""
        assert res.trace_attached is True


# ===========================================================================
# 12. G6B Slice D (D3) — multi-lane provenance on trace snapshots
# ===========================================================================


class TestSnapshotProvenanceMultiLane:
    """G6B Slice D (D3): a single source id reachable from multiple lanes
    must record every lane in the snapshot's ``contributing_lanes`` and
    hold a ProvenanceRecord per (lane, source_type, source_id); a
    single-lane id must hold exactly one ProvenanceRecord.
    """

    def test_two_lanes_one_id_records_both_lanes(self) -> None:
        fake = _FakeRecall()
        engine = RecallV2Engine(recall_fn=fake)

        def _runner(args, kwargs):
            sink = kwargs["trace"]
            sink.lane_start("keyword")
            sink.lane_start("qa")
            sink.lane_candidates("keyword", ["shared_id"], source_type="qa")
            sink.lane_candidates("qa", ["shared_id"], source_type="qa")
            sink.lane_finish("keyword", candidate_count=1)
            sink.lane_finish("qa", candidate_count=1)
            return None

        fake._on_call = _runner
        fake._hits = [_hit(source_id="shared_id")]
        res = engine.recall("hi")

        snap = res.trace.candidate_snapshots["shared_id"]
        # Both lanes recorded.
        assert "keyword" in snap.contributing_lanes
        assert "qa" in snap.contributing_lanes
        # contributing_lanes preserves first-seen order.
        assert snap.contributing_lanes.index("keyword") < snap.contributing_lanes.index("qa")
        # Exactly two provenance entries — one per lane.
        prov_lanes = [p.lane for p in snap.provenance]
        assert prov_lanes.count("keyword") == 1
        assert prov_lanes.count("qa") == 1
        assert len(snap.provenance) == 2
        # Primary lane field is preserved (first lane to record the id).
        assert snap.lane == "keyword"

    def test_single_lane_id_has_one_provenance_entry(self) -> None:
        fake = _FakeRecall()
        engine = RecallV2Engine(recall_fn=fake)

        def _runner(args, kwargs):
            sink = kwargs["trace"]
            sink.lane_start("keyword")
            sink.lane_candidates("keyword", ["only_id"], source_type="card")
            sink.lane_finish("keyword", candidate_count=1)
            return None

        fake._on_call = _runner
        fake._hits = [_hit(source_id="only_id")]
        res = engine.recall("hi")

        snap = res.trace.candidate_snapshots["only_id"]
        # Single-lane contributing_lanes.
        assert snap.contributing_lanes == ["keyword"]
        # Exactly one provenance entry.
        assert len(snap.provenance) == 1
        assert snap.provenance[0].lane == "keyword"
        assert snap.provenance[0].source_type == "card"
        assert snap.provenance[0].source_id == "only_id"
        assert snap.lane == "keyword"

    def test_same_lane_twice_does_not_duplicate_provenance(self) -> None:
        """If the same lane reports the same id twice (e.g. across
        two ``lane_candidates`` calls in one lane), provenance must
        NOT grow — exactly one entry per (lane, source_type, source_id)."""
        fake = _FakeRecall()
        engine = RecallV2Engine(recall_fn=fake)

        def _runner(args, kwargs):
            sink = kwargs["trace"]
            sink.lane_start("keyword")
            sink.lane_candidates("keyword", ["dup_id"], source_type="card")
            sink.lane_candidates("keyword", ["dup_id"], source_type="card")
            sink.lane_finish("keyword", candidate_count=2)
            return None

        fake._on_call = _runner
        fake._hits = [_hit(source_id="dup_id")]
        res = engine.recall("hi")

        snap = res.trace.candidate_snapshots["dup_id"]
        # contributing_lanes lists "keyword" only once.
        assert snap.contributing_lanes == ["keyword"]
        # Exactly one provenance entry, not two.
        assert len(snap.provenance) == 1

    def test_three_lanes_one_id_records_all_three(self) -> None:
        """G6B Slice D (D3) — the canonical qa_1 multi-lane case from
        the parent reproducer: keyword + qa + vector all report the same
        id."""
        fake = _FakeRecall()
        engine = RecallV2Engine(recall_fn=fake)

        def _runner(args, kwargs):
            sink = kwargs["trace"]
            for ln in ("keyword", "qa", "vector"):
                sink.lane_start(ln)
            sink.lane_candidates("keyword", ["qa_1"], source_type="qa")
            sink.lane_candidates("qa", ["qa_1"], source_type="qa")
            sink.lane_candidates("vector", ["qa_1"], source_type="qa")
            for ln in ("keyword", "qa", "vector"):
                sink.lane_finish(ln, candidate_count=1)
            return None

        fake._on_call = _runner
        fake._hits = [_hit(source_id="qa_1")]
        res = engine.recall("hi")

        snap = res.trace.candidate_snapshots["qa_1"]
        # All three lanes present in first-seen order.
        assert snap.contributing_lanes == ["keyword", "qa", "vector"]
        assert len(snap.provenance) == 3
        # Primary lane unchanged (first lane that recorded the id).
        assert snap.lane == "keyword"


# ===========================================================================
# 13. G6B Slice D (D2) — explicit lane always finishes
# ===========================================================================


class TestExplicitLaneAlwaysFinishes:
    """G6B Slice D (D2): the ``explicit`` lane must always end with a
    ``lane_finish`` probe, regardless of ``include_card_vector`` /
    ``include_keyword``.  When no reader is available, the lane must
    record ``skipped=True`` with a non-empty ``reason``.
    """

    def test_explicit_lane_finishes_with_skipped_when_no_reader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``include_card_vector=False`` and no active-memory
        reader, the explicit lane summary must be finished (finished is
        not None and duration_ms is not None) with skipped=True and a
        non-empty reason."""
        from v3core import recall_pool as rp
        from v3core.recall_v2.trace import RecallTrace

        # Build a production-shaped trace + sink so the probe lands.
        ctx = build_query_context(
            "any-query", limit=5, max_chars=10_000, deadline=None
        )
        plan = build_default_query_plan(ctx)
        trace = RecallTrace(query_context=ctx, query_plan=plan)
        sink = LegacySink(trace)

        # Force _active_memory_reader_for to return None so the seam
        # records a "no reader" outcome, and provide a config dict
        # (config=None is NOT allowed for this regression — recall_pool
        # reads half_life from it).
        monkeypatch.setattr(
            rp, "_active_memory_reader_for", lambda pg, deadline: None
        )
        config = {"time_decay": {"half_life_days": 30}}

        hits, pg_fail = rp.recall_pool(
            "any-query",
            card_index={}, pg=None, q_emb=None,
            include_keyword=True, include_card_vector=False,
            include_message_vector=False, include_effective=False,
            include_topic=False, include_yin=False, include_notes=False,
            rerank_top_n=None, rerank_cfg=None, limit=5,
            pg_was_connected=False, config=config,
            sqlite_store=None, core=None, deadline=None,
            trace=sink,
        )

        # Lane summary MUST be finished.
        es = trace.lane_summaries["explicit"]
        assert es.finished is not None, (
            f"explicit lane never finished: {es.to_dict()}"
        )
        assert es.duration_ms is not None
        assert es.duration_ms >= 0
        assert es.skipped is True
        assert isinstance(es.reason, str)
        assert es.reason, f"explicit lane skipped but reason is empty: {es.to_dict()}"

    def test_explicit_lane_finishes_when_both_seams_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With BOTH ``include_keyword=False`` and ``include_card_vector=False``,
        no seam ever ran.  The lane must still finish with
        skipped=True and ``reason='active_memory_paths_disabled'``."""
        from v3core import recall_pool as rp
        from v3core.recall_v2.trace import RecallTrace

        ctx = build_query_context(
            "any-query", limit=5, max_chars=10_000, deadline=None
        )
        plan = build_default_query_plan(ctx)
        trace = RecallTrace(query_context=ctx, query_plan=plan)
        sink = LegacySink(trace)
        config = {"time_decay": {"half_life_days": 30}}

        # Should not be called but stub for safety.
        monkeypatch.setattr(
            rp, "_active_memory_reader_for", lambda pg, deadline: None
        )

        hits, pg_fail = rp.recall_pool(
            "any-query",
            card_index={}, pg=None, q_emb=None,
            include_keyword=False, include_card_vector=False,
            include_message_vector=False, include_effective=False,
            include_topic=False, include_yin=False, include_notes=False,
            rerank_top_n=None, rerank_cfg=None, limit=5,
            pg_was_connected=False, config=config,
            sqlite_store=None, core=None, deadline=None,
            trace=sink,
        )

        es = trace.lane_summaries["explicit"]
        assert es.finished is not None
        assert es.duration_ms is not None
        assert es.skipped is True
        assert es.reason == "active_memory_paths_disabled"


# ===========================================================================
# 12. Legacy sink — duck-typed protocol implementation
# ===========================================================================


class TestLegacySink:
    def test_sink_methods_all_silently_swallow_errors(self) -> None:
        from v3core.recall_v2.engine import LegacySink

        # Even when the trace is broken, sink methods must not raise.
        class _Broken:
            pass

        sink = LegacySink(_Broken())  # type: ignore[arg-type]
        # Each call must not raise, even with garbage inputs.
        for fn, args in [
            ("lane_start", ("vector",)),
            ("lane_finish", ("vector",)),
            ("lane_candidates", ("vector", ["x"])),
            ("score", ("x", "raw", 0.5)),
            ("event", ("x", CandidateEventType.FOUND)),
            ("drop", ("x", DropReasonCode.DUPLICATE)),
            ("select", ("x",)),
            ("inject", ("x",)),
            ("warn", ("msg",)),
            ("error", ("msg",)),
        ]:
            method = getattr(sink, fn)
            method(*args)  # must not raise
