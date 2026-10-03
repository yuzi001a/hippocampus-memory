# -*- coding: utf-8 -*-
"""M03 injection-layer suppression of invalidated derived memory — RED→GREEN.

Contract (docs/M03-CORRECTION-PROPAGATION.md §"Current recall must recognise
staleness", docs/M03-DERIVED-LINEAGE-MAP.md §4):

  * Suppression happens at the INJECTION layer only. Recall for non-invalidated
    candidates and the RRF / rerank ordering logic are byte-identical to before;
    Recall V2 is not rewritten and no derived body is ever rewritten.
  * The four real lanes are covered end-to-end:
      - Topic query recall   -> ``recall_pool.recall_pool`` single convergence
                                point before RRF/rerank (``source_id =
                                'topic_<topic_id>'``)
      - Topic first round    -> ``injector.MemoryInjector._format_first_round_block``
                                (the recall dict carries ``topic_id``)
      - Observer note        -> ``derived_suppression.select_note_head`` used by
                                ``observer.recall_for_new_session`` (chain head)
      - Yin paragraph        -> ``V3Core._recall_yin_segments`` (paragraph ``id``)
  * An invalidated observer note is NEVER used as the chain head: the walk
    follows the existing ``prev_id`` chain to the nearest non-invalidated note,
    and a fully-invalidated chain yields NO candidate (nothing is injected).
  * A sidecar read failure FAILS OPEN but stays VISIBLE: ``degraded=True`` plus a
    WARNING log carrying ``suppression_degraded=True``. "Cannot determine" is
    never reported as "nothing invalidated".

No real PostgreSQL: the P0-A conftest hard-blocks ``psycopg2.connect``, so every
lane is exercised against deterministic in-memory fakes that model leases, the
sidecar read and the note ``prev_id`` chain.
"""
from __future__ import annotations

import contextlib
import importlib
import logging
from typing import Any, Optional

import pytest

from v3core.derived_invalidation import (
    DERIVED_KIND_OBSERVER_NOTE,
    DERIVED_KIND_TOPIC,
    DERIVED_KIND_YIN_PARAGRAPH,
)

MODULE = "v3core.derived_suppression"


def _mod():
    return importlib.import_module(MODULE)


# ─────────────────────────────────────────────────────────────────────────────
# fakes: a pg store exposing ``lease()`` (canonical ``invalidated_ids`` path)
# ─────────────────────────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, db: "_FakePg") -> None:
        self.db = db
        self._many: list = []

    def execute(self, sql: Any, params: Any = None) -> None:
        self.db.queries.append((str(sql), params))
        if self.db.fail_reads:
            raise RuntimeError("injected sidecar read failure")
        upper = " ".join(str(sql).split()).upper()
        if "DERIVED_MEMORY_INVALIDATIONS" in upper and "SELECT DERIVED_ID" in upper:
            # Mirror the real SQL faithfully: filter by state ONLY when the
            # statement actually carries a state predicate. The sidecar's state
            # column tracks the rebuild lifecycle and never lifts suppression.
            kind = params[0] if params else None
            rows = [row for row in self.db.invalidations
                    if row["derived_kind"] == kind]
            if "STATE <> %S" in upper:
                state = params[1] if params and len(params) > 1 else None
                rows = [row for row in rows if row["state"] != state]
            self._many = [(row["derived_id"],) for row in rows]
        elif "DERIVED_MEMORY_INVALIDATIONS" in upper:
            self._many = [(1,)]  # readability probe
        else:
            self._many = []

    def fetchone(self) -> Optional[tuple]:
        return self._many[0] if self._many else None

    def fetchall(self) -> list:
        return list(self._many)

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_a: Any) -> bool:
        return False


class _FakeConn:
    def __init__(self, db: "_FakePg") -> None:
        self.db = db

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.db)

    def get_transaction_status(self) -> int:
        return 0

    def close(self) -> None:
        return None


class _FakeLease:
    def __init__(self, db: "_FakePg") -> None:
        self.connection = _FakeConn(db)
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _FakeConn:
        return self.connection

    def __exit__(self, *_a: Any) -> bool:
        self.close()
        return False


class _FakePg:
    """Store-like: only ``lease()`` + optional ``search_effective``/``is_connected``."""

    def __init__(self, invalidations: Any = (), *, fail_reads: bool = False,
                 yin_hits: Any = None, connected: bool = True) -> None:
        self.invalidations = [dict(r) for r in invalidations]
        self.fail_reads = fail_reads
        self.queries: list = []
        self._yin_hits = list(yin_hits or [])
        self._connected = connected

    def lease(self, timeout: Optional[float] = None) -> _FakeLease:
        return _FakeLease(self)

    def is_connected(self) -> bool:
        return self._connected

    def search_effective(self, query_emb, pool_role=None, limit=10):
        return list(self._yin_hits)


def _invalidation(kind: str, derived_id: str, state: str = "stale") -> dict:
    return {"derived_kind": kind, "derived_id": derived_id, "state": state}


# ─────────────────────────────────────────────────────────────────────────────
# Lane 1 — Topic query recall (recall_pool convergence before RRF/rerank)
# ─────────────────────────────────────────────────────────────────────────────
class _FakeTopicMatcher:
    def __init__(self, matches: list) -> None:
        self._matches = matches

    def match(self, query, top_k=None, query_embedding=None, deadline=None):
        return list(self._matches)


class _FakeCore:
    def __init__(self, matcher, pool=None) -> None:
        self._topic_recall = matcher
        self._pg_pool = pool
        self.config: dict = {}

    @property
    def pg_pool(self):
        return self._pg_pool


def test_topic_query_recall_suppresses_invalidated_hit():
    mod = _mod()
    from v3core.types import RecallHit

    hits = {
        "topic_t_bad": RecallHit("topic_t_bad", "bad", "A", kind="topic"),
        "topic_t_ok": RecallHit("topic_t_ok", "ok", "B", kind="topic"),
        "qa_1": RecallHit("qa_1", "q", "qa", kind="qa"),
    }
    sup = mod.DerivedSuppression(DERIVED_KIND_TOPIC, frozenset({"t_bad"}), False, "")
    removed = mod.suppress_topic_hits(hits, sup)

    assert removed == ["topic_t_bad"]
    assert "topic_t_bad" not in hits
    assert set(hits) == {"topic_t_ok", "qa_1"}


def test_topic_query_recall_keeps_everything_when_nothing_invalidated():
    mod = _mod()
    from v3core.types import RecallHit

    hits = {
        "topic_t_a": RecallHit("topic_t_a", "a", "A", kind="topic"),
        "topic_t_b": RecallHit("topic_t_b", "b", "B", kind="topic"),
    }
    sup = mod.DerivedSuppression(DERIVED_KIND_TOPIC, frozenset(), False, "")
    removed = mod.suppress_topic_hits(hits, sup)

    assert removed == []
    assert set(hits) == {"topic_t_a", "topic_t_b"}


def test_recall_pool_topic_lane_drops_invalidated_before_rrf(monkeypatch):
    mod = _mod()
    import v3core.recall_pool as rp

    monkeypatch.setattr(rp, "_maybe_expand_query", lambda *a, **k: a[0] if a else "")

    pg = _FakePg([_invalidation(DERIVED_KIND_TOPIC, "t_bad")])
    matcher = _FakeTopicMatcher([
        (0.90, {"id": "t_bad", "title": "bad", "body": "A is current"}),
        (0.80, {"id": "t_ok", "title": "ok", "body": "B is current"}),
    ])
    core = _FakeCore(matcher, pool=None)

    final, _pg_fail = rp.recall_pool(
        "anything",
        pg=pg,
        include_keyword=False,
        include_card_vector=False,
        include_message_vector=False,
        include_effective=False,
        include_topic=True,
        include_yin=False,
        include_notes=False,
        core=core,
        config=None,
        limit=10,
    )
    ids = {h.source_id for h in final}
    assert "topic_t_bad" not in ids, "invalidated topic must be dropped before RRF/rerank"
    assert "topic_t_ok" in ids, "non-invalidated topic recall must be unchanged"


# ─────────────────────────────────────────────────────────────────────────────
# Lane 2 — Topic first round (injector unit split)
# ─────────────────────────────────────────────────────────────────────────────
def test_topic_first_round_suppresses_invalidated_candidate():
    mod = _mod()
    topics = [
        {"topic_id": "t_bad", "title": "Bad", "body": "A is current", "sim": 0.9},
        {"topic_id": "t_ok", "title": "Ok", "body": "B is current", "sim": 0.8},
    ]
    sup = mod.DerivedSuppression(DERIVED_KIND_TOPIC, frozenset({"t_bad"}), False, "")
    kept = mod.suppress_topic_candidates(topics, sup)
    assert [t["topic_id"] for t in kept] == ["t_ok"]


def test_topic_first_round_keeps_unaffected_candidates():
    mod = _mod()
    topics = [{"topic_id": "t_a", "title": "A", "body": "x", "sim": 0.9}]
    sup = mod.DerivedSuppression(DERIVED_KIND_TOPIC, frozenset(), False, "")
    kept = mod.suppress_topic_candidates(topics, sup)
    assert kept == topics


def test_injector_first_round_block_drops_invalidated_topic(monkeypatch):
    mod = _mod()
    import v3core.observer as observer
    from v3core.injector import MemoryInjector

    recall_payload = {
        "note": {},
        "tail": [],
        "topics": [
            {"topic_id": "t_bad", "title": "Bad", "body": "A is current", "sim": 0.9},
            {"topic_id": "t_ok", "title": "Ok", "body": "B is current", "sim": 0.8},
        ],
    }

    def _fake_recall(**kwargs):
        return dict(recall_payload)

    monkeypatch.setattr(observer, "recall_for_new_session", _fake_recall)

    pg = _FakePg([_invalidation(DERIVED_KIND_TOPIC, "t_bad")])
    core = _FakeCore(_FakeTopicMatcher([]), pool=pg)
    injector = MemoryInjector(core, strategy="identity_first")

    header, units = injector._format_first_round_block("sess-1")
    blob = "\n".join(units)
    assert "A is current" not in blob, "invalidated topic must not be injected"
    assert "Bad" not in blob
    assert "B is current" in blob, "non-invalidated topic must still be injected"


# ─────────────────────────────────────────────────────────────────────────────
# Lane 3 — Observer note chain head (prev_id walk)
# ─────────────────────────────────────────────────────────────────────────────
class _NoteCursor:
    """Fake cursor for the two note SELECTs (chain head + by-id)."""

    def __init__(self, notes: dict, *, fail: bool = False) -> None:
        self.notes = notes  # id -> (id, version, content, range, links, prev_id)
        self.fail = fail
        self.executed: list = []
        self._one = None

    def execute(self, sql: Any, params: Any = None) -> None:
        self.executed.append((" ".join(str(sql).split()), params))
        if self.fail:
            raise RuntimeError("injected note read failure")
        s = " ".join(str(sql).split())
        if "ORDER BY id DESC" in s:
            self._one = max(self.notes.values(), key=lambda r: r[0]) if self.notes else None
        elif "WHERE id = %s" in s:
            self._one = self.notes.get(params[0])
        else:
            self._one = None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return []


def _note(nid, *, prev=None, content="c", version="v1"):
    return (nid, version, content, None, [], prev)


def test_observer_chain_head_unchanged_when_not_invalidated():
    mod = _mod()
    cur = _NoteCursor({3: _note(3, prev=2), 2: _note(2, prev=1), 1: _note(1)})
    sup = mod.DerivedSuppression(DERIVED_KIND_OBSERVER_NOTE, frozenset(), False, "")
    row = mod.select_note_head(cur, sup)
    assert row is not None and row[0] == 3


def test_observer_chain_head_walks_to_nearest_non_invalidated():
    mod = _mod()
    cur = _NoteCursor({3: _note(3, prev=2), 2: _note(2, prev=1), 1: _note(1)})
    sup = mod.DerivedSuppression(DERIVED_KIND_OBSERVER_NOTE, frozenset({"3"}), False, "")
    row = mod.select_note_head(cur, sup)
    assert row is not None and row[0] == 2, "must walk prev_id to the nearest non-invalidated note"
    assert row[0] != 3, "the invalidated head must never be returned"


def test_observer_chain_head_walks_past_multiple_invalidated():
    mod = _mod()
    cur = _NoteCursor({5: _note(5, prev=4), 4: _note(4, prev=3), 3: _note(3)})
    sup = mod.DerivedSuppression(
        DERIVED_KIND_OBSERVER_NOTE, frozenset({"5", "4"}), False, "")
    row = mod.select_note_head(cur, sup)
    assert row is not None and row[0] == 3


def test_observer_chain_head_all_invalidated_yields_no_candidate():
    mod = _mod()
    cur = _NoteCursor({2: _note(2, prev=1), 1: _note(1)})
    sup = mod.DerivedSuppression(
        DERIVED_KIND_OBSERVER_NOTE, frozenset({"1", "2"}), False, "")
    assert mod.select_note_head(cur, sup) is None


def test_observer_chain_head_no_notes_yields_no_candidate():
    mod = _mod()
    cur = _NoteCursor({})
    sup = mod.DerivedSuppression(DERIVED_KIND_OBSERVER_NOTE, frozenset(), False, "")
    assert mod.select_note_head(cur, sup) is None


# ─────────────────────────────────────────────────────────────────────────────
# Lane 4 — Yin paragraph (paragraph id)
# ─────────────────────────────────────────────────────────────────────────────
def test_yin_suppresses_invalidated_paragraph():
    mod = _mod()
    hits = [
        {"id": 5, "source_id": "v1", "title": "p5", "content": "A", "cosine": 0.9},
        {"id": 6, "source_id": "v1", "title": "p6", "content": "B", "cosine": 0.8},
    ]
    sup = mod.DerivedSuppression(DERIVED_KIND_YIN_PARAGRAPH, frozenset({"5"}), False, "")
    kept = mod.suppress_yin_hits(hits, sup)
    assert [h["id"] for h in kept] == [6]


def test_yin_keeps_unaffected_paragraphs():
    mod = _mod()
    hits = [{"id": 9, "source_id": "v1", "title": "p9", "content": "A", "cosine": 0.9}]
    sup = mod.DerivedSuppression(DERIVED_KIND_YIN_PARAGRAPH, frozenset(), False, "")
    assert mod.suppress_yin_hits(hits, sup) == hits


def test_recall_yin_segments_drops_invalidated_paragraph():
    mod = _mod()
    from v3core import V3Core

    pg = _FakePg(
        [_invalidation(DERIVED_KIND_YIN_PARAGRAPH, "5")],
        yin_hits=[
            {"id": 5, "source_id": "v1", "title": "p5", "content": "A is current",
             "cosine": 0.9},
            {"id": 6, "source_id": "v1", "title": "p6", "content": "B is current",
             "cosine": 0.8},
        ],
    )
    core = V3Core.__new__(V3Core)
    core._pg = pg
    core._pg_pool = None

    out = core._recall_yin_segments("q", {}, limit=2, q_emb=[0.1, 0.2, 0.3])
    assert "A is current" not in out, "invalidated yin paragraph must not be injected"
    assert "B is current" in out


def test_search_effective_yin_branch_selects_and_returns_id():
    """The yin SELECT must carry ``id`` so paragraph-level suppression is possible."""
    from v3core.pg_store import PgEmbedStore

    captured: dict = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(str(sql).split())
            captured["params"] = params

        def fetchall(self):
            return [("v1", "sec", "body", 0.5, 42)]

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Lease:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *_a):
            return False

    store = PgEmbedStore.__new__(PgEmbedStore)
    store.lease = lambda *a, **k: _Lease()

    rows = store.search_effective([0.1, 0.2], pool_role="yin_segment", limit=2)
    assert " id" in captured["sql"].lower(), "yin SELECT must include the paragraph id column"
    assert rows and rows[0]["id"] == 42, "search_effective must expose the yin paragraph id"


# ─────────────────────────────────────────────────────────────────────────────
# Degraded (sidecar read failure) — fail open but visible
# ─────────────────────────────────────────────────────────────────────────────
def test_read_suppression_degraded_on_store_read_failure(caplog):
    mod = _mod()
    pg = _FakePg([_invalidation(DERIVED_KIND_TOPIC, "t_bad")], fail_reads=True)
    with caplog.at_level(logging.WARNING, logger="v3core.derived_suppression"):
        sup = mod.read_suppression(pg, DERIVED_KIND_TOPIC)
        mod.log_degraded(sup, "unit-test")
    assert sup.degraded is True
    assert sup.ids == frozenset()
    assert sup.known is False
    assert "suppression_degraded=True" in caplog.text


def test_read_suppression_cursor_degraded(caplog):
    mod = _mod()
    cur = _FakeCursor(_FakePg([], fail_reads=True))
    sup = mod.read_suppression_cursor(cur, DERIVED_KIND_TOPIC)
    assert sup.degraded is True and sup.ids == frozenset()


def test_degraded_fails_open_and_never_drops_candidates(caplog):
    mod = _mod()
    from v3core.types import RecallHit

    pg = _FakePg([_invalidation(DERIVED_KIND_TOPIC, "t_bad")], fail_reads=True)
    with caplog.at_level(logging.WARNING, logger="v3core.derived_suppression"):
        sup = mod.read_suppression(pg, DERIVED_KIND_TOPIC)
        mod.log_degraded(sup, "fail-open")
    hits = {
        "topic_t_bad": RecallHit("topic_t_bad", "bad", "A", kind="topic"),
        "topic_t_ok": RecallHit("topic_t_ok", "ok", "B", kind="topic"),
    }
    removed = mod.suppress_topic_hits(hits, sup)
    assert removed == [], "a degraded read must FAIL OPEN (keep injecting)"
    assert set(hits) == {"topic_t_bad", "topic_t_ok"}
    assert "suppression_degraded=True" in caplog.text


def test_read_suppression_healthy_empty_is_not_degraded():
    mod = _mod()
    pg = _FakePg([])
    sup = mod.read_suppression(pg, DERIVED_KIND_TOPIC)
    assert sup.degraded is False
    assert sup.ids == frozenset()


def test_read_suppression_returns_ids_for_every_state_including_rebuilt():
    """Sidecar rows permanently suppress their derived_id — state is NOT a filter.

    Authority (task book §31): ``old D1 stale`` / ``new D2 active`` /
    ``replacement_derived_id = D2`` → current recall keeps ``D2 allowed`` and
    ``D1 suppressed``. A row reaching ``state='rebuilt'`` only records rebuild
    lifecycle progress; it must NEVER lift suppression of the old id.
    """
    mod = _mod()
    pg = _FakePg([
        _invalidation(DERIVED_KIND_TOPIC, "t_stale", "stale"),
        _invalidation(DERIVED_KIND_TOPIC, "t_pending", "pending_rebuild"),
        _invalidation(DERIVED_KIND_TOPIC, "t_rebuilt", "rebuilt"),
        _invalidation(DERIVED_KIND_TOPIC, "t_unresolved", "unresolved"),
    ])
    sup = mod.read_suppression(pg, DERIVED_KIND_TOPIC)
    assert sup.ids == frozenset(
        {"t_stale", "t_pending", "t_rebuilt", "t_unresolved"}
    ), "every sidecar row suppresses its derived_id regardless of state"
    assert sup.degraded is False


def test_read_suppression_rebuilt_row_never_reopens_injection():
    """§31: the old id stays suppressed after its replacement is recorded.

    ``t_d1`` is invalidated with ``state='rebuilt'`` and
    ``replacement_derived_id='t_d2'``; ``t_d2`` is the new, admissible artifact
    and is never in the suppression set.
    """
    from v3core.types import RecallHit

    mod = _mod()
    pg = _FakePg([
        {
            "derived_kind": DERIVED_KIND_TOPIC,
            "derived_id": "t_d1",
            "state": "rebuilt",
            "replacement_derived_id": "t_d2",
        },
    ])
    sup = mod.read_suppression(pg, DERIVED_KIND_TOPIC)
    assert sup.suppresses("t_d1") is True, "D1 must stay suppressed forever"
    assert sup.suppresses("t_d2") is False, "D2 is the new artifact, not suppressed"

    hits = {
        "topic_t_d1": RecallHit("topic_t_d1", "old", "A", kind="topic"),
        "topic_t_d2": RecallHit("topic_t_d2", "new", "B", kind="topic"),
    }
    removed = mod.suppress_topic_hits(hits, sup)
    assert removed == ["topic_t_d1"], "D1 must not re-enter current injection"
    assert set(hits) == {"topic_t_d2"}


def test_injector_first_round_degraded_fails_open_and_marks(monkeypatch, caplog):
    mod = _mod()
    import v3core.observer as observer
    from v3core.injector import MemoryInjector

    monkeypatch.setattr(observer, "recall_for_new_session", lambda **kw: {
        "note": {}, "tail": [],
        "topics": [
            {"topic_id": "t_bad", "title": "Bad", "body": "A is current", "sim": 0.9},
            {"topic_id": "t_ok", "title": "Ok", "body": "B is current", "sim": 0.8},
        ],
    })
    pg = _FakePg([_invalidation(DERIVED_KIND_TOPIC, "t_bad")], fail_reads=True)
    injector = MemoryInjector(_FakeCore(_FakeTopicMatcher([]), pool=pg),
                              strategy="identity_first")

    with caplog.at_level(logging.WARNING, logger="v3core.derived_suppression"):
        _header, units = injector._format_first_round_block("sess-1")
    blob = "\n".join(units)
    assert "A is current" in blob, "degraded sidecar must FAIL OPEN (keep injecting)"
    assert "B is current" in blob
    assert injector.last_suppression_degraded is True
    assert "suppression_degraded=True" in caplog.text


def test_recall_pool_degraded_fails_open(monkeypatch, caplog):
    mod = _mod()
    import v3core.recall_pool as rp

    monkeypatch.setattr(rp, "_maybe_expand_query", lambda *a, **k: a[0] if a else "")
    pg = _FakePg([_invalidation(DERIVED_KIND_TOPIC, "t_bad")], fail_reads=True)
    core = _FakeCore(_FakeTopicMatcher([
        (0.90, {"id": "t_bad", "title": "bad", "body": "A is current"}),
        (0.80, {"id": "t_ok", "title": "ok", "body": "B is current"}),
    ]), pool=None)

    with caplog.at_level(logging.WARNING, logger="v3core.derived_suppression"):
        final, _pg_fail = rp.recall_pool(
            "anything", pg=pg, include_keyword=False, include_card_vector=False,
            include_message_vector=False, include_effective=False, include_topic=True,
            include_yin=False, include_notes=False, core=core, config=None, limit=10,
        )
    ids = {h.source_id for h in final}
    assert "topic_t_bad" in ids, "degraded sidecar must FAIL OPEN in recall_pool"
    assert "suppression_degraded=True" in caplog.text


# ─────────────────────────────────────────────────────────────────────────────
# Wiring: the real injection sites must reference the suppression helpers
# ─────────────────────────────────────────────────────────────────────────────
def test_injection_sites_are_wired_to_suppression():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "v3core"
    recall_pool = (root / "recall_pool.py").read_text(encoding="utf-8")
    injector = (root / "injector.py").read_text(encoding="utf-8")
    observer = (root / "observer.py").read_text(encoding="utf-8")
    init = (root / "__init__.py").read_text(encoding="utf-8")
    pg_store = (root / "pg_store.py").read_text(encoding="utf-8")
    suppression = (root / "derived_suppression.py").read_text(encoding="utf-8")

    assert "suppress_topic_hits" in recall_pool
    assert "suppress_topic_candidates" in injector
    assert "select_note_head" in observer
    assert "suppress_yin_hits" in init
    # observer chain-head SELECT must carry prev_id for the walk
    assert "links, prev_id" in suppression
    # yin SELECT must expose id
    assert "AS cosine, id" in pg_store
