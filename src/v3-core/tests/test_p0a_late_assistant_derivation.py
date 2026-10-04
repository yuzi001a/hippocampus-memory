# -*- coding: utf-8 -*-
"""P0-A late-assistant QA derivation — regression tests (task-book §18).

Formalises the isolated GREEN probe
(``evidence/p0a-late-assistant-derivation-20261004/p0a_probe.py``) as pytest
regression coverage for the P0-A fix.

Locked behaviour: an assistant event that arrives AFTER its owning Q→A pair has
already been flushed must still be folded into the durable ``qa_pairs`` row of
the user turn it belongs to — exactly once — and must never leak into a later
turn or another session's row.  A late event is resolved to its owning durable
user turn via ``conversation_stream`` (host + host_turn_id), and the merge is an
atomic guarded UPDATE (``WHERE NOT merged_event_ids @> [msg_id]``) so replays
and concurrent writers cannot double-append.

Driven against:
  * the REAL ``V3Core.sync_turn`` / flush / late-derivation / recovery paths,
  * a deterministic in-memory fake PG modelling ``qa_pairs`` AND the v3
    ``conversation_stream`` source rows,
  * a real ``LiveBuffer`` with its writer thread disabled (durable markers are
    still written to the isolated dir, so duplicate-status probing is real),
  * an isolated tmp basePath — no production file is ever touched.

No network, no LLM, no embedding, no real PostgreSQL: ``tests/conftest.py``
defence 4 hard-blocks ``psycopg2.connect`` for the whole test process.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
V3CORE_SRC = REPO_ROOT / "src" / "v3-core" / "src"
if str(V3CORE_SRC) not in sys.path:
    sys.path.insert(0, str(V3CORE_SRC))

from v3core import V3Core  # noqa: E402
from v3core.ingest import LiveBuffer  # noqa: E402


# ────────────────────────────────────────────────────────────────
# deterministic in-memory fake PG
#   qa_pairs           (pre-existing model + merged_event_ids ledger)
#   conversation_stream (v3: durable source rows for the late path)
# ────────────────────────────────────────────────────────────────
def _coerce_mev(value):
    """Model PG's jsonb semantics for ``merged_event_ids``.

    Real psycopg2 hands a ``%s::jsonb`` parameter over as raw JSON text and
    reads the column back as a list.  The fake must do the same: a ``str``
    parameter is *parsed* (NOT iterated char-by-char — that was a probe bug
    that manufactured a fake duplicate), a list stays a list.
    """
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except Exception:
            return []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return []


class FakeQaDb:
    def __init__(self) -> None:
        self.rows: list[dict] = []        # qa_pairs
        self.cs_rows: list[dict] = []     # conversation_stream
        self._next = 1
        self._next_cs = 1

    # -- qa_pairs -------------------------------------------------
    def find_by_source(self, source_id: str):
        for r in self.rows:
            if r["source_id"] == str(source_id):
                return r
        return None

    def find_by_session_qa(self, session_id, q, a):
        for r in self.rows:
            if r["session_id"] == session_id and r["question"] == q and r["answer"] == a:
                return r
        return None

    def insert(self, *, source_id, session_id, turn_id, question, answer,
               tool_calls, tool_results, timestamp, source, embedding=None,
               embed_model="", merged_event_ids=None):
        if self.find_by_source(source_id):
            return False
        self.rows.append({
            "id": self._next, "source_id": source_id, "session_id": session_id,
            "turn_id": turn_id, "question": question, "answer": answer,
            "tool_calls": tool_calls, "tool_results": tool_results,
            "timestamp": timestamp, "source": source,
            "embedding": embedding, "embed_model": embed_model,
            "merged_event_ids": _coerce_mev(merged_event_ids),
        })
        self._next += 1
        return True

    def update_late_merge(self, row_id, content, msg_id):
        """Model the P0-A atomic guarded UPDATE (``WHERE NOT members @> [msg_id]``).

        Returns rowcount: 1 when this call appended, 0 when the guard shows the
        event is already a member (concurrent/duplicate writer).
        """
        for r in self.rows:
            if r["id"] == row_id:
                members = r.get("merged_event_ids") or []
                if msg_id in members:
                    return 0
                old = r.get("answer") or ""
                r["answer"] = content if not old else old + "\n" + content
                r["merged_event_ids"] = members + [msg_id]
                return 1
        return 0

    # -- conversation_stream (v3) ---------------------------------
    def insert_cs(self, *, session_id, role, host, host_turn_id, event_id,
                  turn_id, content, timestamp, tool_calls=None, tool_results=None):
        self.cs_rows.append({
            "id": self._next_cs, "session_id": session_id, "role": role,
            "host": host, "host_turn_id": host_turn_id, "event_id": event_id,
            "turn_id": turn_id, "content": content, "timestamp": timestamp,
            "tool_calls": tool_calls or [], "tool_results": tool_results or [],
        })
        self._next_cs += 1
        return True

    def find_u1(self, session_id, host, host_turn_id):
        hits = [r for r in self.cs_rows
                if r["session_id"] == session_id and r["role"] == "user"
                and r["host"] == host and r["host_turn_id"] == host_turn_id]
        return hits[-1] if hits else None


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn
        self.db: FakeQaDb = conn.db
        self._one = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def fetchone(self):
        return self._one

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        u = s.upper()
        p = params or ()
        self._one = None
        self.rowcount = 0

        if u.startswith("SELECT ID, EVENT_ID, TURN_ID, CONTENT, TIMESTAMP, "
                        "TOOL_CALLS, TOOL_RESULTS FROM CONVERSATION_STREAM"):
            # P0-A step 1: locate the owning durable user turn.
            row = self.db.find_u1(p[0], p[1], p[2])
            self._one = ((row["id"], row["event_id"], row["turn_id"],
                          row["content"], row["timestamp"],
                          row["tool_calls"], row["tool_results"])
                         if row else None)
        elif u.startswith("SELECT ID, (EMBEDDING IS NOT NULL) FROM QA_PAIRS"):
            if "WHERE SOURCE_ID" in u:
                row = self.db.find_by_source(p[0])
            else:
                row = self.db.find_by_session_qa(p[0], p[1], p[2])
            self._one = (row["id"], bool(row.get("embedding"))) if row else None
        elif u.startswith("SELECT ID, ANSWER, MERGED_EVENT_IDS, QUESTION FROM QA_PAIRS"):
            # P0-A step 2: the QA row that owns the user turn, if flushed.
            row = self.db.find_by_source(p[0])
            self._one = ((row["id"], row["answer"],
                          row.get("merged_event_ids") or [], row["question"])
                         if row else None)
        elif u.startswith("INSERT INTO QA_PAIRS"):
            _new_shape = "MERGED_EVENT_IDS" in u
            if _new_shape:
                if len(p) >= 12:      # with embedding
                    (source_id, session_id, turn_id, q, a, tc, tr, ts, src,
                     emb, fp, aev) = p[:12]
                else:                 # no embedding (10 params)
                    (source_id, session_id, turn_id, q, a, tc, tr, ts, src,
                     aev) = p[:10]
                    emb, fp = None, ""
            else:                     # legacy shapes (kept for compat runs)
                if len(p) >= 11:
                    (source_id, session_id, turn_id, q, a, tc, tr, ts, src,
                     emb, fp) = p[:11]
                else:
                    (source_id, session_id, turn_id, q, a, tc, tr, ts,
                     src) = p[:9]
                    emb, fp, aev = None, "", []
            ok = self.db.insert(
                source_id=source_id, session_id=session_id, turn_id=turn_id,
                question=q, answer=a, tool_calls=tc, tool_results=tr,
                timestamp=ts, source=src, embedding=emb, embed_model=fp,
                merged_event_ids=aev)
            self.rowcount = 1 if ok else 0
        elif u.startswith("SELECT ID FROM QA_PAIRS WHERE SOURCE_ID"):
            row = self.db.find_by_source(p[0])
            self._one = (row["id"],) if row else None
        elif u.startswith("UPDATE QA_PAIRS SET ANSWER = CASE WHEN"):
            # P0-A late merge: atomic guarded append (rowcount from the guard).
            if "EMBEDDING = " in u:
                content, _c2, members_json, _emb, _fp, qa_id, _mj2 = p[:7]
            else:
                content, _c2, members_json, qa_id, _mj2 = p[:5]
            mid = (json.loads(members_json) or [""])[0]
            self.rowcount = self.db.update_late_merge(qa_id, content, mid)
        else:
            raise RuntimeError(f"fake PG: unmodelled SQL: {s}")


class FakeConn:
    def __init__(self, db: FakeQaDb) -> None:
        self.db = db

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class FakePg:
    def __init__(self, db: FakeQaDb) -> None:
        self.db = db

    def _connect(self):
        return FakeConn(self.db)

    def is_connected(self):
        return True


# ────────────────────────────────────────────────────────────────
# harness
# ────────────────────────────────────────────────────────────────
class Ctx:
    def __init__(self, tmp: Path) -> None:
        self.db = FakeQaDb()
        self.tmp = Path(tmp)
        self.core = self._new_core()

    def _new_core(self):
        core = V3Core(profile="default")
        cfg = {"basePath": str(self.tmp)}
        core._config = cfg
        buf = LiveBuffer(pg=None, config=cfg)
        buf._start_writer = lambda: None       # no writer thread: durable only
        core._live_buffer = buf
        core._pg = FakePg(self.db)
        return core

    def restart(self):
        """Simulate a process restart: brand-new V3Core over the same dir/db."""
        try:
            self.core.shutdown(timeout=0.2)
        except Exception:
            pass
        self.core = self._new_core()

    def close(self):
        try:
            timer = getattr(self.core, "_qa_flush_timer", None)
            if timer:
                timer.cancel()
        except Exception:
            pass
        try:
            self.core.shutdown(timeout=0.2)
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """Fresh isolated Ctx per test; observer side-effects disabled."""
    import v3core.observer as obs_mod
    monkeypatch.setattr(obs_mod, "maybe_observe", lambda *a, **k: None)
    c = Ctx(tmp_path / "p0a-base")
    try:
        yield c
    finally:
        c.close()


def msg(mid, role, content, ts, turn="T1", host="dsh"):
    return {"id": mid, "role": role, "content": content,
            "timestamp": ts, "turn_id": turn, "host": host}


TS = ["2026-10-04T01:00:%02d+00:00" % i for i in range(60)]


def sync(ctx, session, messages):
    return ctx.core.sync_turn(session, messages, host="dsh")


def drain(ctx, timeout=8.0):
    ctx.core._drain_flush_queue(timeout=timeout)
    end = time.time() + timeout
    while time.time() < end:
        q = ctx.core._flush_queue
        if getattr(q, "unfinished_tasks", 0) == 0 and q.empty():
            break
        time.sleep(0.02)


def pending_of(ctx, session):
    try:
        c = ctx.core.get_session_context(session, create=False)
    except Exception:
        return None
    p = c.pending_qa if c else None
    if not p:
        return None
    return {"q": (p.get("q") or "")[:40], "a_len": len(p.get("a") or ""),
            "q_msg_id": p.get("q_msg_id")}


def seed_source(ctx, session, mid, content, ts, turn="T1", host="dsh"):
    """Model 'the source layer already accepted this user turn durably'.

    Mirrors what ``LiveBuffer._flush`` -> ``PgEmbedStore.insert_message``
    writes: ``event_id == msg_id`` (legacy alias), ``host_turn_id == turn_id``.
    The enqueue->PG write chain itself is covered by the data-path probe and
    the Y400 E2E; these tests only need the durable premise present.
    """
    return ctx.db.insert_cs(
        session_id=session, role="user", host=host, host_turn_id=turn,
        event_id=mid, turn_id=turn, content=content, timestamp=ts)


S1 = "sess-p0a-s1"; S2 = "sess-p0a-s2"; S3 = "sess-p0a-s3"
SA = "sess-p0a-sA"; SB = "sess-p0a-sB"; S5 = "sess-p0a-s5"; S6 = "sess-p0a-s6"
S7 = "sess-p0a-s7"


def qa_by_question(db, q):
    return next((r for r in db.rows if r["question"] == q), None)


def _rows_snapshot(db):
    return [(r["question"][:24], (r["answer"] or "")[:60]) for r in db.rows]


# ── scenarios ───────────────────────────────────────────────────
def test_baseline_q_a(ctx):
    """S1: plain Q→A pairing still lands one complete qa_pairs row."""
    sync(ctx, S1, [msg("u1", "user", "Q1 baseline", TS[0])])
    sync(ctx, S1, [msg("u1", "user", "Q1 baseline", TS[0]),
                   msg("a1", "assistant", "A1 immediate", TS[1])])
    sync(ctx, S1, [msg("u1", "user", "Q1 baseline", TS[0]),
                   msg("a1", "assistant", "A1 immediate", TS[1]),
                   msg("u2", "user", "Q2 next", TS[2], turn="T2")])
    drain(ctx)
    r = qa_by_question(ctx.db, "Q1 baseline")
    assert r is not None, f"expected Q1 baseline row; rows={_rows_snapshot(ctx.db)}"
    assert "A1 immediate" in (r["answer"] or ""), (
        f"expected 'A1 immediate' in answer; answer={r['answer']!r}")


def test_late_after_flush(ctx):
    """S2: Q→A(progress)→flush→late A: late content lands in QA exactly once."""
    seed_source(ctx, S2, "u1", "Q1 post-flush", TS[0])
    sync(ctx, S2, [msg("u1", "user", "Q1 post-flush", TS[0])])
    sync(ctx, S2, [msg("u1", "user", "Q1 post-flush", TS[0]),
                   msg("a1", "assistant", "A1-PROGRESS", TS[1])])
    ctx.core._flush_all_pending_qa()
    drain(ctx)
    r = sync(ctx, S2, [msg("u1", "user", "Q1 post-flush", TS[0]),
                       msg("a1", "assistant", "A1-PROGRESS", TS[1]),
                       msg("a3", "assistant", "A3-FINAL-LATE", TS[3])])
    drain(ctx)
    row = qa_by_question(ctx.db, "Q1 post-flush")
    ans = (row or {}).get("answer", "")
    assert row is not None, f"expected flushed Q1 row; rows={_rows_snapshot(ctx.db)}"
    assert "A3-FINAL-LATE" in ans, (
        f"late event must be folded into the QA row; answer={ans!r} "
        f"derivation={r.get('derivation')} events={r.get('events')}")
    assert "A1-PROGRESS" in ans, f"progress event must survive; answer={ans!r}"
    assert ans.count("A1-PROGRESS") == 1, (
        f"A1-PROGRESS must appear exactly once (no duplicate); answer={ans!r}")
    assert ans.count("A3-FINAL-LATE") == 1, (
        f"A3-FINAL-LATE must appear exactly once; answer={ans!r}")


def test_late_after_next_user(ctx):
    """S3: Q1→flush→Q2→late A1: A1 folds into Q1 only, Q2 pending intact."""
    seed_source(ctx, S3, "u1", "Q1 first", TS[0], turn="T1")
    sync(ctx, S3, [msg("u1", "user", "Q1 first", TS[0], turn="T1")])
    sync(ctx, S3, [msg("u1", "user", "Q1 first", TS[0], turn="T1"),
                   msg("u2", "user", "Q2 second", TS[2], turn="T2")])
    drain(ctx)
    r = sync(ctx, S3, [msg("u1", "user", "Q1 first", TS[0], turn="T1"),
                       msg("u2", "user", "Q2 second", TS[2], turn="T2"),
                       msg("a1", "assistant", "A1-LATE-FOR-T1", TS[3], turn="T1")])
    drain(ctx)
    q1 = qa_by_question(ctx.db, "Q1 first")
    q2 = qa_by_question(ctx.db, "Q2 second")
    pend = pending_of(ctx, S3)
    assert q1 is not None, f"expected Q1 row; rows={_rows_snapshot(ctx.db)}"
    assert "A1-LATE-FOR-T1" in (q1["answer"] or ""), (
        f"late A1 must fold into Q1; q1_answer={q1['answer']!r} "
        f"derivation={r.get('derivation')} events={r.get('events')}")
    assert q2 is None or "A1-LATE-FOR-T1" not in (q2["answer"] or ""), (
        f"late A1 must NOT leak into Q2; q2_answer={q2 and q2['answer']!r}")
    assert pend is not None and pend["q"] == "Q2 second", (
        f"Q2 pending must stay intact (not flushed/cleared); pending={pend} "
        f"rows={_rows_snapshot(ctx.db)}")


def test_interleaved_sessions(ctx):
    """S4: sessions A/B interleaved — late A resolves only to its own row."""
    seed_source(ctx, SA, "ua1", "Q_A", TS[0], turn="T_A1")
    sync(ctx, SA, [msg("ua1", "user", "Q_A", TS[0], turn="T_A1")])
    sync(ctx, SB, [msg("ub1", "user", "Q_B", TS[1], turn="T_B1")])
    sync(ctx, SA, [msg("ua1", "user", "Q_A", TS[0], turn="T_A1"),
                   msg("aa1", "assistant", "A_A-PROGRESS", TS[2], turn="T_A1")])
    ctx.core._flush_all_pending_qa()
    drain(ctx)
    sync(ctx, SB, [msg("ub1", "user", "Q_B", TS[1], turn="T_B1"),
                   msg("ub2", "user", "Q_B2", TS[3], turn="T_B2")])
    drain(ctx)
    r = sync(ctx, SA, [msg("ua1", "user", "Q_A", TS[0], turn="T_A1"),
                       msg("aa1", "assistant", "A_A-PROGRESS", TS[2], turn="T_A1"),
                       msg("aa9", "assistant", "A_A-LATE", TS[4], turn="T_A1")])
    drain(ctx)
    qa = qa_by_question(ctx.db, "Q_A")
    qb2 = qa_by_question(ctx.db, "Q_B2")
    others = [r0 for r0 in ctx.db.rows if r0["question"] != "Q_A"]
    assert qa is not None, f"expected Q_A row; rows={_rows_snapshot(ctx.db)}"
    assert "A_A-LATE" in (qa["answer"] or ""), (
        f"late A_A must fold into its own Q_A row; answer={qa['answer']!r} "
        f"derivation={r.get('derivation')} events={r.get('events')}")
    assert qb2 is None, (
        f"Q_B2 must not be created by A's late event; qb2={qb2!r}")
    assert "A_A-LATE" not in json.dumps(others, default=str), (
        f"late A_A must not leak into any other row; rows={_rows_snapshot(ctx.db)}")


def test_replay_idempotent(ctx):
    """S5: late event replayed x3 → one qa row, marker once (not tripled)."""
    seed_source(ctx, S5, "u1", "Q1 replay", TS[0])
    sync(ctx, S5, [msg("u1", "user", "Q1 replay", TS[0])])
    sync(ctx, S5, [msg("u1", "user", "Q1 replay", TS[0]),
                   msg("a1", "assistant", "A1-PROG", TS[1])])
    ctx.core._flush_all_pending_qa()
    drain(ctx)
    receipts = []
    for _ in range(3):
        r = sync(ctx, S5, [msg("u1", "user", "Q1 replay", TS[0]),
                           msg("a1", "assistant", "A1-PROG", TS[1]),
                           msg("a2", "assistant", "A2-LATE-MARKER", TS[3])])
        receipts.append({"derivation": (r.get("derivation") or {}),
                         "events": (r.get("events") or {})})
        drain(ctx)
    rows = [r for r in ctx.db.rows if r["question"] == "Q1 replay"]
    ans = rows[0]["answer"] if rows else ""
    marker_count = (ans or "").count("A2-LATE-MARKER")
    assert len(rows) == 1, (
        f"replay must not create extra qa rows; rows={_rows_snapshot(ctx.db)}")
    assert marker_count == 1, (
        f"A2-LATE-MARKER must appear exactly once (idempotent replay); "
        f"count={marker_count} answer={ans!r} receipts={receipts}")


def test_restart_recovery(ctx):
    """S6: flush→restart→late A: late lands in QA once; no duplicate progress."""
    seed_source(ctx, S6, "u1", "Q1 restart", TS[0])
    sync(ctx, S6, [msg("u1", "user", "Q1 restart", TS[0])])
    sync(ctx, S6, [msg("u1", "user", "Q1 restart", TS[0]),
                   msg("a1", "assistant", "A1-PROG", TS[1])])
    ctx.core._flush_all_pending_qa()
    drain(ctx)
    ctx.restart()
    r = sync(ctx, S6, [msg("u1", "user", "Q1 restart", TS[0]),
                       msg("a1", "assistant", "A1-PROG", TS[1]),
                       msg("a9", "assistant", "A9-FINAL-AFTER-RESTART", TS[5])])
    drain(ctx)
    row = qa_by_question(ctx.db, "Q1 restart")
    ans = (row or {}).get("answer", "")
    assert row is not None, f"expected Q1 row; rows={_rows_snapshot(ctx.db)}"
    assert "A9-FINAL-AFTER-RESTART" in ans, (
        f"late event after restart must fold into the pre-restart row; "
        f"answer={ans!r} derivation={r.get('derivation')} events={r.get('events')}")
    assert ans.count("A1-PROG") == 1, (
        f"A1-PROG must not be duplicated after restart; answer={ans!r}")
    assert ans.count("A9-FINAL-AFTER-RESTART") == 1, (
        f"A9-FINAL-AFTER-RESTART must appear exactly once; answer={ans!r}")


def test_source_enqueue_no_duplicate_identity(ctx):
    """S7: raw idempotent enqueue — same item twice writes one durable marker.

    LiveBuffer.enqueue must not manufacture a second durable trace for a
    re-enqueued identical item (same content-derived job_id).  Return-value
    semantics are environment-dependent (True / None legacy), so this test
    asserts only the durable marker count; observed returns are reported in
    the failure message for diagnosis.
    """
    lb = ctx.core._live_buffer
    args = dict(session_id=S7, msg_id="u7", content="Q7 raw idempotent",
                role="user", turn_id="T7", timestamp=TS[0],
                host="dsh", host_turn="T7")
    r1 = lb.enqueue(**args)
    r2 = lb.enqueue(**args)
    marker_dir = ctx.tmp / "j" / "pending_live_buffer"
    markers = sorted(p.name for p in marker_dir.glob("*.json")) \
        if marker_dir.exists() else []
    assert len(markers) == 1, (
        f"re-enqueue of the same item must leave exactly one durable marker; "
        f"got {len(markers)}: {markers} (returns: first={r1!r} second={r2!r})")
    # in-process dedupe cache must hold exactly one slot for this item
    pending_jobs = getattr(lb, "_pending_jobs", None)
    assert pending_jobs is None or len(pending_jobs) == 1, (
        f"expected one in-process pending job slot; "
        f"pending_jobs={pending_jobs!r} (returns: first={r1!r} second={r2!r})")
