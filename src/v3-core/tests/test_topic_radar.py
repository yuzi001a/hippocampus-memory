"""Hermetic unit tests for v3core.topic_radar.

No real PG, no real numpy matrix I/O beyond what the production module does
itself, no LLM, no network. The fake cursor / fake connection objects here
record every executed statement so the no-writes contract is assertable.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


# --- fake cursor / fake connection -----------------------------------------

class FakeCursor:
    """A context-manager cursor that records execute() calls and replays rows.

    Raises when ``raise_on_execute`` is set, so test #10 can exercise the
    DB/read-failure path without touching a real database.
    """

    def __init__(self, rows, raise_on_execute: bool = False):
        self._rows = rows
        self.raise_on_execute = raise_on_execute
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.statements.append(sql)
        if self.raise_on_execute:
            raise RuntimeError("synthetic execute failure")

    def fetchall(self):
        return list(self._rows)

    def close(self) -> None:
        pass


class FakeConnection:
    """A connection whose ``cursor()`` returns the supplied cursor factory."""

    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _enc(vec):
    """Encode a Python list[float] the way the SELECT casts it to text."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _topic_row(tid: str, title: str, embedding):
    """Build a fake DB row. ``embedding`` may be ``None`` / empty / unparsable."""
    if embedding is None:
        return (tid, title, None)
    if embedding == "":
        return (tid, title, "")
    if isinstance(embedding, str):
        # Caller wants a deliberately malformed text — pass it through.
        return (tid, title, embedding)
    return (tid, title, _enc(embedding))


def _pg(rows):
    """Build a FakeConnection whose cursor yields the given rows."""
    cur = FakeCursor(rows)
    return FakeConnection(cur), cur


# --- import under test ------------------------------------------------------

# v3core is editable-installed in the test venv, so a plain import works
# without manipulating sys.path here.
from v3core.topic_radar import radar_scan  # noqa: E402


# --- helpers ---------------------------------------------------------------

@pytest.fixture
def orthogonal_pair():
    """Two orthogonal vectors (cosine == 0.0)."""
    return [1.0, 0.0], [0.0, 1.0]


@pytest.fixture
def high_cosine_pair():
    """cosine == 1.0."""
    return [0.6, 0.8], [0.6, 0.8]


@pytest.fixture
def mid_cosine_pair():
    """cosine == ~0.7, built from [1,0] and [0.7, sqrt(1-0.49)]."""
    import math
    a = [1.0, 0.0]
    b = [0.7, math.sqrt(1.0 - 0.49)]
    return a, b


# --- tests ------------------------------------------------------------------

def test_zero_topics_returns_empty_report():
    conn, cur = _pg([])
    rep = radar_scan(conn)
    assert rep == {"duplicates": [], "related": [], "scanned": 0, "dup_total": 0, "rel_total": 0}


def test_one_topic_returns_empty_report():
    conn, cur = _pg([_topic_row("t1", "only", [1.0, 0.0])])
    rep = radar_scan(conn)
    assert rep["scanned"] == 1
    assert rep["duplicates"] == []
    assert rep["related"] == []
    assert rep["dup_total"] == 0
    assert rep["rel_total"] == 0


def test_duplicate_pair_above_threshold(high_cosine_pair):
    a, b = high_cosine_pair
    rows = [
        _topic_row("ta", "alpha", a),
        _topic_row("tb", "beta", b),
    ]
    conn, cur = _pg(rows)
    rep = radar_scan(conn)
    assert rep["scanned"] == 2
    assert rep["dup_total"] == 1
    assert rep["rel_total"] == 0
    assert rep["related"] == []
    assert len(rep["duplicates"]) == 1
    dup = rep["duplicates"][0]
    assert dup["source_id"] == "ta"
    assert dup["target_id"] == "tb"
    assert dup["source_title"] == "alpha"
    assert dup["target_title"] == "beta"
    assert dup["cosine"] == 1.0


def test_related_pair_between_thresholds(mid_cosine_pair):
    a, b = mid_cosine_pair
    rows = [
        _topic_row("ta", "alpha", a),
        _topic_row("tb", "beta", b),
    ]
    conn, cur = _pg(rows)
    rep = radar_scan(conn)
    assert rep["scanned"] == 2
    assert rep["dup_total"] == 0
    assert rep["rel_total"] == 1
    assert rep["duplicates"] == []
    rel = rep["related"][0]
    assert rel["source_id"] == "ta"
    assert rel["target_id"] == "tb"
    assert 0.0 < rel["cosine"] < 0.75


def test_unrelated_pair_below_threshold(orthogonal_pair):
    a, b = orthogonal_pair
    rows = [
        _topic_row("ta", "alpha", a),
        _topic_row("tb", "beta", b),
    ]
    conn, cur = _pg(rows)
    rep = radar_scan(conn)
    assert rep["duplicates"] == []
    assert rep["related"] == []
    assert rep["dup_total"] == 0
    assert rep["rel_total"] == 0


def test_lists_sorted_by_cosine_descending():
    # Build 5 topics with hand-crafted cosine bands so each pair lands in
    # the right bucket.
    # t0 == t1 -> 1.000 (dup)
    # t0 == t2 -> 1.000 (dup, same vector)
    # t0 vs t3 -> 0.90    (dup)
    # t0 vs t4 -> 0.70    (related)
    # t1 vs t3 -> 0.90    (dup)
    # t3 vs t4 -> 0.80    (dup)
    # t1 vs t4 -> 0.70    (related)
    # t2 vs t4 -> 0.70    (related)
    rows = [
        _topic_row("t0", "alpha", [1.0, 0.0]),
        _topic_row("t1", "beta", [1.0, 0.0]),
        _topic_row("t2", "gamma", [1.0, 0.0]),
        _topic_row("t3", "delta", [0.9, 0.43588989]),   # cos(t0, t3) ≈ 0.90
        _topic_row("t4", "epsilon", [0.7, 0.71414284]),  # cos(t0, t4) ≈ 0.70
    ]
    conn, cur = _pg(rows)
    rep = radar_scan(conn)
    dups_cosines = [d["cosine"] for d in rep["duplicates"]]
    rels_cosines = [r["cosine"] for r in rep["related"]]
    assert dups_cosines == sorted(dups_cosines, reverse=True)
    assert rels_cosines == sorted(rels_cosines, reverse=True)
    # Sanity: every duplicate is above dup_threshold and every related is in band.
    assert all(c > 0.75 for c in dups_cosines)
    assert all(0.60 < c <= 0.75 for c in rels_cosines)


def test_max_dups_bound_truncates_list_but_keeps_total():
    # 4 topics pairwise all give cosine == 1.0 -> 6 duplicate pairs.
    rows = [_topic_row(f"t{i}", f"title-{i}", [1.0, 0.0]) for i in range(4)]
    conn, cur = _pg(rows)
    rep = radar_scan(conn, max_dups=2)
    assert rep["dup_total"] == 6  # pre-truncation
    assert len(rep["duplicates"]) == 2
    # Ordering preserved: first two have the highest cosine (=1.0).
    assert rep["duplicates"][0]["cosine"] == 1.0
    assert rep["duplicates"][1]["cosine"] == 1.0


def test_max_rels_bound_truncates_list_but_keeps_total():
    import math
    # 4 topics pairwise:
    #   t0 vs t1 -> cos 0.0   (excluded)
    #   t0 vs t2 -> cos ~0.7071 (related)
    #   t0 vs t3 -> cos 0.70  (related)
    #   t1 vs t2 -> cos ~0.7071 (related)
    #   t1 vs t3 -> cos ~0.7141 (related)
    #   t2 vs t3 -> cos ~0.99995 (duplicate)
    # -> 4 related pairs and 1 duplicate pair before truncation.
    rows = [
        _topic_row("t0", "a", [1.0, 0.0]),
        _topic_row("t1", "b", [0.0, 1.0]),         # orthogonal to t0/t2
        _topic_row("t2", "c", [math.sqrt(0.5), math.sqrt(0.5)]),
        _topic_row("t3", "d", [0.7, math.sqrt(1 - 0.49)]),
    ]
    conn, cur = _pg(rows)
    rep = radar_scan(conn, max_rels=1)
    assert rep["dup_total"] == 1
    assert rep["rel_total"] == 4
    assert len(rep["related"]) == 1


def test_malformed_data_is_ignored_safely():
    # Mixed batch:
    #   t0: valid 2-dim
    #   t1: NULL embedding text -> skipped at parse (text is falsy)
    #   t2: empty embedding text  -> skipped at parse (text is falsy)
    #   t3: unparsable embedding  -> skipped at parse (ValueError caught)
    #   t4: valid 2-dim, identical to t0 -> duplicate candidate
    #   t5: 3-dim vector           -> kept by loader, dropped before matrix
    rows = [
        _topic_row("t0", "good-a", [1.0, 0.0]),
        _topic_row("t1", "null-emb", None),
        _topic_row("t2", "empty-emb", ""),
        _topic_row("t3", "bad-emb", "not,a,real,vector,text,not-numeric"),
        _topic_row("t4", "good-b", [1.0, 0.0]),
        _topic_row("t5", "wrong-dim", [1.0, 0.0, 0.0]),
    ]
    conn, cur = _pg(rows)
    rep = radar_scan(conn)
    # No exception raised, and the surviving same-length pair produced a
    # candidate. t0 and t4 are identical -> cosine 1.0 -> one duplicate.
    # t5 (3-dim) is dropped before the matrix, never reaches candidates.
    assert rep["dup_total"] == 1
    assert rep["rel_total"] == 0
    assert len(rep["duplicates"]) == 1
    assert rep["duplicates"][0]["source_id"] == "t0"
    assert rep["duplicates"][0]["target_id"] == "t4"
    # scanned reflects what _load_topic_embeddings returned (all rows whose
    # embedding text was non-empty AND parseable to at least one float);
    # t5 is included even though it was later dropped for length mismatch.
    # Of the 6 input rows: t1 (NULL) and t2 ("") are dropped at the falsy
    # guard, t3 (unparsable) is dropped at the parse except. The 3 survivors
    # are t0, t4 (both 2-dim) and t5 (3-dim, kept by loader, dropped later).
    assert rep["scanned"] == 3


def test_db_read_failure_fails_safely():
    cur = FakeCursor([], raise_on_execute=True)
    conn = FakeConnection(cur)
    rep = radar_scan(conn)
    assert rep == {"duplicates": [], "related": [], "scanned": 0, "dup_total": 0, "rel_total": 0}


def test_no_writes_issued():
    rows = [
        _topic_row("t0", "alpha", [1.0, 0.0]),
        _topic_row("t1", "beta", [0.0, 1.0]),
    ]
    conn, cur = _pg(rows)
    radar_scan(conn)
    # Only SELECT statements were ever executed.
    assert cur.statements, "expected the radar to issue at least one SELECT"
    for stmt in cur.statements:
        assert "select" in stmt.lower(), f"non-SELECT leaked: {stmt!r}"
    # No commit / rollback / close.
    assert conn.commits == 0
    assert conn.rollbacks == 0
    # E1 owns the connection lifecycle — radar_scan must never close it.
    assert conn.closed is False


def test_module_source_contains_no_private_path():
    # The on-disk source of the module under test — read via importlib so
    # this test is independent of CWD or sys.path layout.
    spec = importlib.util.find_spec("v3core.topic_radar")
    assert spec is not None and spec.origin is not None
    text = Path(spec.origin).read_text(encoding="utf-8")
    for forbidden in ("v3-memory-plugin", "C:\\Users", "AppData", "sys.path.insert"):
        assert forbidden not in text, f"forbidden token leaked: {forbidden!r}"


def test_pg_none_is_safe():
    rep = radar_scan(pg=None)
    assert rep == {"duplicates": [], "related": [], "scanned": 0, "dup_total": 0, "rel_total": 0}


def test_pg_lease_with_connect_callable_is_used():
    """A pg object exposing _connect() should have _connect() invoked."""
    rows = [_topic_row("t0", "a", [1.0, 0.0]), _topic_row("t1", "b", [0.0, 1.0])]
    cur = FakeCursor(rows)
    inner = FakeConnection(cur)
    lease = SimpleNamespace(_connect=lambda: inner)
    rep = radar_scan(lease)
    assert rep["scanned"] == 2
    assert cur.statements  # SELECT ran through the lease's connection


def test_titles_are_truncated_to_title_max_chars():
    long_title = "x" * 200
    rows = [
        _topic_row("t0", long_title, [1.0, 0.0]),
        _topic_row("t1", long_title, [1.0, 0.0]),
    ]
    conn, cur = _pg(rows)
    rep = radar_scan(conn)
    assert rep["dup_total"] == 1
    assert len(rep["duplicates"][0]["source_title"]) == 60
    assert len(rep["duplicates"][0]["target_title"]) == 60
