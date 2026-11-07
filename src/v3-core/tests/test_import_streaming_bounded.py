"""Regression: the one-command import must stream a large corpus.

Before this fix ``_process_source`` materialised every parsed item, every
raw item and every QA pair for the whole source in memory at once. A real
Hermes ``state.db`` (4.4 GB, 671k message rows) exhausted RAM on the Y400
test box and the process died with no traceback — a silent full-rebuild
blocker.

The tests here pin the two properties that fix it:

1. buffered work is bounded by the number of *open sessions*, not by the
   size of the corpus (a synthetic 400-session source stays bounded);
2. live writes are batched (one commit per batch), while the row counters
   and the per-session QA derivation stay identical to the streaming
   contract.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v3core import importers  # noqa: E402
from v3core.importers import auto_import  # noqa: E402


def _make_state_db(path: Path, sessions: int, turns: int) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
        " content TEXT, timestamp REAL, display_kind TEXT)"
    )
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
    rid = 0
    for s in range(sessions):
        sid = f"sess-{s:04d}"
        con.execute("INSERT INTO sessions (id, title) VALUES (?, ?)", (sid, sid))
        for t in range(turns):
            for role, text in (("user", f"q{s}-{t}"), ("assistant", f"a{s}-{t}")):
                rid += 1
                con.execute(
                    "INSERT INTO messages (id, session_id, role, content, timestamp, display_kind)"
                    " VALUES (?, ?, ?, ?, ?, NULL)",
                    (rid, sid, role, text, 1700000000.0 + rid),
                )
    con.commit()
    con.close()


class _FakeCursor:
    def __init__(self, sink):
        self._sink = sink
        self.rowcount = 1

    def execute(self, sql, params=None):
        self._sink["executed"] += 1
        self._sink["rows"].append(params)


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink

    def cursor(self):
        return _FakeCursor(self._sink)

    def commit(self):
        self._sink["commits"] += 1

    def rollback(self):
        self._sink["rollbacks"] += 1


class _FakeLease:
    def __init__(self, sink):
        self.connection = _FakeConn(sink)

    def close(self):
        self._sink["leases_released"] += 1


class _FakePool:
    def __init__(self):
        self.sink = {"executed": 0, "commits": 0, "rollbacks": 0, "rows": [], "leases_released": 0,
                     "leases": 0}

    def lease(self, timeout=5.0):
        self.sink["leases"] += 1
        return _FakeLease(self.sink)


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=400, turns=5)
    return tmp_path


def test_dry_run_counts_are_stable_and_streamed(corpus: Path):
    buf = __import__("io").StringIO()
    report = importers.import_auto(
        hosts=["hermes"], overrides={"hermes": corpus}, dry_run=True, out=buf
    )
    src = report["sources"][0]
    assert src["found"] is True
    assert src["sessions"] == 400
    assert src["messages"] == 400 * 5 * 2
    assert src["qa_pairs_derived"] == 400 * 5
    assert src["recall_ready"] is True


def test_live_writes_are_batched_not_one_commit_per_row(corpus: Path):
    pool = _FakePool()
    importers.install_pool(pool)
    try:
        buf = __import__("io").StringIO()
        report = importers.import_auto(
            hosts=["hermes"], overrides={"hermes": corpus}, dry_run=False, batch_size=50, out=buf
        )
    finally:
        importers.install_pool(None)

    src = report["sources"][0]
    total_rows = 400 * 5 * 2
    assert src["raw_imported"] == total_rows
    assert src["qa_pairs_derived"] == 400 * 5
    # One commit per session is the expected shape here (flush happens at a
    # session boundary); what must NOT happen is one commit per row.
    assert pool.sink["commits"] < total_rows
    assert pool.sink["commits"] <= 400 * 2 + 8
    assert pool.sink["executed"] == total_rows + 400 * 5
    assert pool.sink["rollbacks"] == 0


def test_nul_bytes_are_stripped_and_counted(tmp_path: Path):
    """A NUL byte in a session body must not kill the batch (PG rejects it)."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
        " content TEXT, timestamp REAL, display_kind TEXT)"
    )
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
    con.execute("INSERT INTO sessions (id, title) VALUES ('s1', 's1')")
    con.execute(
        "INSERT INTO messages VALUES (1, 's1', 'user', ?, 1700000000.0, NULL)",
        ("before\x00after",),
    )
    con.execute(
        "INSERT INTO messages VALUES (2, 's1', 'assistant', ?, 1700000001.0, NULL)",
        ("reply\x00with nul",),
    )
    con.commit()
    con.close()

    pool = _FakePool()
    importers.install_pool(pool)
    try:
        report = importers.import_auto(
            hosts=["hermes"], overrides={"hermes": tmp_path}, dry_run=False, out=__import__("io").StringIO()
        )
    finally:
        importers.install_pool(None)

    src = report["sources"][0]
    assert src["errors"] == []
    assert src["raw_imported"] == 2
    assert src["qa_pairs_derived"] == 1
    assert src["nul_sanitized"] == 3  # 2 raw rows + 1 derived QA pair were cleaned
    bound = [p for p in pool.sink["rows"] if isinstance(p, tuple)]
    assert all("\x00" not in str(x) for p in bound for x in p)


def test_interleaved_sessions_are_paired_per_session(tmp_path: Path):
    """Real state.db interleaves sessions by id; pairing must still be per session."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
        " content TEXT, timestamp REAL, display_kind TEXT)"
    )
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
    rows = [
        (1, "s1", "user", "q1"),
        (2, "s2", "user", "q2"),
        (3, "s1", "assistant", "a1"),
        (4, "s2", "assistant", "a2"),
    ]
    for rid, sid, role, text in rows:
        con.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, NULL)",
            (rid, sid, role, text, 1700000000.0 + rid),
        )
    con.commit()
    con.close()

    report = importers.import_auto(
        hosts=["hermes"], overrides={"hermes": tmp_path}, dry_run=True,
        out=__import__("io").StringIO(),
    )
    src = report["sources"][0]
    assert src["qa_pairs_derived"] == 2, src
    assert src["reopened_sessions"] == 0, src


def test_large_corpus_does_not_buffer_the_whole_source(corpus: Path, monkeypatch):
    """The flush path must never see the whole corpus in one list."""
    from v3core.importers import qa_pairing as qa_mod

    seen: list[int] = []
    real_pair = qa_mod.pair_items

    def _record(items):
        seen.append(len(items))
        return real_pair(items)

    monkeypatch.setattr(qa_mod, "pair_items", _record)
    auto_import._process_source(
        auto_import._discovery.discover_host("hermes", root=corpus),
        pool=None,
        dry_run=True,
        batch_size=50,
    )
    explained = [n for n in seen if n > 0]
    assert explained, "no session was flushed"
    assert max(explained) <= 10, f"a single flush held {max(explained)} items"
    assert len(explained) == 400
