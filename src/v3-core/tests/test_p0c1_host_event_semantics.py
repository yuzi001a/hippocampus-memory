# -*- coding: utf-8 -*-
"""P0-C1 — Hermes host event semantics: §11 acceptance matrix (T1-T11).

Design contract: ``docs/P0C1-HOST-EVENT-SEMANTICS.md`` (branch
``fix/p0c1-hermes-event-semantics``).  This module is the RED test matrix that
pins the required behaviour *before* the implementation lands.

What is driven (real code, no re-implementation):

  * the REAL ``V3Core.sync_turn`` pairing path (classification-first loop,
    per-kind actions, structural continuation) — the same entry point the
    Hermes adapter calls;
  * the REAL flush chain (``_submit_flush`` → ``_flush_pending_qa`` →
    ``_flush_all_pending_qa``) and the P0-A late-derivation path
    (``_resolve_late_assistant_derivation`` / ``_late_merge_into_qa``);
  * a deterministic in-memory fake PG modelling ``qa_pairs`` **and** the v3
    ``conversation_stream`` source rows (the same harness shape as
    ``tests/test_p0a_late_assistant_derivation.py``);
  * a real ``LiveBuffer`` with its writer thread disabled (durable markers are
    still written under the isolated tmp basePath);
  * an isolated tmp basePath — no production file is ever touched.

No network, no LLM, no embedding, no real PostgreSQL:
``tests/conftest.py`` defence 4 hard-blocks ``psycopg2.connect`` for the whole
test process.  Defence 3b snapshots the real production durable-outbox tree;
on a host with a live Hermes gateway writing to
``~/.v3-core/profiles/default/j`` set ``V3_HARNESS_ALLOW_LIVE_PROD_OUTBOX=1``
to downgrade that environment-specific comparison to a warning.

Expected RED state (implementation absent):
  T1/T2/T3/T6 are regression guards that already hold today.
  T4 (structural continuation after flush — the blocker), T5 (tasklist /
  still-in-progress continuation), T7 (OUT_OF_BAND real user), T8/T9 (async /
  background boundary), T10 (failed_turn terminal), T11 (replay of the
  blocker) fail until the classifier + per-kind actions land.

Dependencies written by sibling agents (contract §5):
  * ``v3core.host_events`` — classifier + ``extract_oob_payload`` (contract §2,
    interface names fixed).  Imported defensively: when absent, the fallback
    stubs below raise ``AssertionError`` so the classifier tests still fail
    with a legible reason instead of a bare collection error.
  * ``tests/fixtures/host_events_p0c1.py`` — real de-identified message shapes
    (§12).  Imported defensively; the local ``_*`` constants below are the real
    documented markers from the P0-C taxonomy report and are only a fallback
    until the fixture module exposes its shapes (see ``_fx``).
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
# dependency 1: the classifier module (contract §2 interface, fixed)
# ────────────────────────────────────────────────────────────────
_HOST_EVENTS_IMPORTED = True
_HOST_EVENTS_IMPORT_ERROR = ""
try:
    from v3core.host_events import (  # noqa: E402
        classify_host_event,
        extract_oob_payload,
        HOST_EVENT_REAL_USER,
        HOST_EVENT_CONTINUATION,
        HOST_EVENT_ASYNC_BOUNDARY,
        HOST_EVENT_NEW_ROOT,
        HOST_EVENT_CONTROL,
        HOST_EVENT_TERMINAL,
        HOST_EVENT_NORMAL_ASSISTANT,
        HOST_EVENT_NORMAL_TOOL,
    )
except Exception as _exc:  # pragma: no cover - RED path while impl absent
    _HOST_EVENTS_IMPORTED = False
    _HOST_EVENTS_IMPORT_ERROR = repr(_exc)

    # Contract §2 constant VALUES are fixed; the stubs below use them so the
    # classifier contract tests can still express their expectation.
    HOST_EVENT_REAL_USER = "REAL_USER"
    HOST_EVENT_CONTINUATION = "CONTINUATION"
    HOST_EVENT_ASYNC_BOUNDARY = "ASYNC_BOUNDARY"
    HOST_EVENT_NEW_ROOT = "NEW_ROOT"
    HOST_EVENT_CONTROL = "CONTROL"
    HOST_EVENT_TERMINAL = "TERMINAL"
    HOST_EVENT_NORMAL_ASSISTANT = "NORMAL_ASSISTANT"
    HOST_EVENT_NORMAL_TOOL = "NORMAL_TOOL"

    def classify_host_event(*, role, content, display_kind=None,
                            session_source=None):  # type: ignore[misc]
        raise AssertionError(
            "v3core.host_events.classify_host_event is not implemented "
            f"(import error: {_HOST_EVENTS_IMPORT_ERROR})"
        )

    def extract_oob_payload(content):  # type: ignore[misc]
        raise AssertionError(
            "v3core.host_events.extract_oob_payload is not implemented "
            f"(import error: {_HOST_EVENTS_IMPORT_ERROR})"
        )


# ────────────────────────────────────────────────────────────────
# dependency 2: real message shapes (§12 fixtures)
#   Fallback markers are the real documented strings from the P0-C taxonomy
#   report (§1.2) — NOT hand-invented.  Once fixtures/host_events_p0c1.py
#   lands and exposes a name, the fixture value wins.
# ────────────────────────────────────────────────────────────────
_FIXTURES_IMPORTED = True
_FIXTURES_IMPORT_ERROR = ""
try:
    from fixtures import host_events_p0c1 as FX  # type: ignore  # noqa: E402
except Exception as _exc:  # pragma: no cover - RED path while fixture absent
    FX = None
    _FIXTURES_IMPORTED = False
    _FIXTURES_IMPORT_ERROR = repr(_exc)


def _fx(*names, default):
    """Prefer a real fixture shape; fall back to the documented marker."""
    if FX is not None:
        for n in names:
            v = getattr(FX, n, None)
            if isinstance(v, str) and v.strip():
                return v
    return default


# Real prefixes / shapes (taxonomy §1.2, contract §2.1).
CTX_COMPACTION = _fx(
    "CTX_COMPACTION",
    default="[CONTEXT COMPACTION — REFERENCE ONLY]\n"
            "Earlier turns were summarised to save space; continue the task.",
)
CTX_TASKLIST = _fx(
    "CTX_TASKLIST",
    default="[Your active task list was preserved across context compression]\n"
            "1. [ ] finish the pending audit",
)
CTX_STILL_IN_PROGRESS = _fx(
    "CTX_STILL_IN_PROGRESS",
    default="[STILL IN PROGRESS — this is the active request, resume it]",
)
ASYNC_BATCH_COMPLETE = _fx(
    "ASYNC_BATCH_COMPLETE",
    default="[ASYNC DELEGATION BATCH COMPLETE — deleg_abc123]\n"
            "Subagent finished the delegated task.",
)
BG_PROC_COMPLETE = _fx(
    "BG_PROC_COMPLETE",
    default="[IMPORTANT: Background process proc_abc123 completed]\n"
            "exit code 0; see output above.",
)
OOB_PAYLOAD = "改成 B"
OOB_WRAPPER = _fx(
    "OUT_OF_BAND", "OOB_STEER",
    default=(
        "[OUT-OF-BAND USER MESSAGE — a direct message from the user, "
        "delivered once at this position]\n"
        f"{OOB_PAYLOAD}\n"
        "[/OUT-OF-BAND USER MESSAGE]"
    ),
)
SYS_MODEL_SWITCH = _fx(
    "SYS_MODEL_SWITCH",
    default="[System: The active model for this chat has changed to gpt-x]",
)
FAILED_TURN_NOTICE = _fx(
    "FAILED_TURN", "FAILED_TURN_NOTICE",
    default="Your request was not processed. Send it again to retry.",
)
HIDDEN_NOTICE = _fx(
    "HIDDEN",
    default="[This response was interrupted by a user correction.]",
)
TITLE_GEN = _fx(
    "TITLE_GEN",
    default="### Task:\nSuggest 3-5 relevant follow-up questions",
)
CRON_JOB = _fx(
    "CRON_JOB",
    default="[IMPORTANT: You are running as a scheduled cron job]",
)
SKILL_INVOKED = _fx(
    "SKILL_INVOKED",
    default='[IMPORTANT: The user has invoked the "demo" skill]',
)
JUDGE_PROMPT = _fx(
    "JUDGE_PROMPT",
    default="# 公文易 文档理解 —— 语义评审员（LLM Judge）",
)


# ────────────────────────────────────────────────────────────────
# deterministic in-memory fake PG
#   qa_pairs            (model + merged_event_ids ledger)
#   conversation_stream (durable source rows: P0-A + snapshot anchor lookup)
# ────────────────────────────────────────────────────────────────
def _coerce_mev(value):
    """Model PG's jsonb semantics for ``merged_event_ids``.

    A ``str`` parameter is parsed (never iterated char-by-char — that was a
    probe bug that manufactured a fake duplicate); a list stays a list.
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
            if (r["session_id"] == session_id and r["question"] == q
                    and r["answer"] == a):
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
        event is already a member (replay / concurrent writer).
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
                  turn_id, content, timestamp, tool_calls=None,
                  tool_results=None):
        self.cs_rows.append({
            "id": self._next_cs, "session_id": session_id, "role": role,
            "host": host, "host_turn_id": host_turn_id, "event_id": event_id,
            "turn_id": turn_id, "content": content, "timestamp": timestamp,
            "tool_calls": tool_calls or [], "tool_results": tool_results or [],
        })
        self._next_cs += 1
        return True

    def find_u1(self, session_id, host, host_turn_id):
        """P0-A anchor lookup: the durable user row by (host, host_turn_id)."""
        hits = [r for r in self.cs_rows
                if r["session_id"] == session_id and r["role"] == "user"
                and r["host"] == host and r["host_turn_id"] == host_turn_id]
        return hits[-1] if hits else None

    def find_u1_by_event(self, session_id, host, event_id):
        """Contract §4(b) anchor lookup: the durable user row by event_id."""
        hits = [r for r in self.cs_rows
                if r["session_id"] == session_id and r["role"] == "user"
                and r["event_id"] == event_id
                and (not host or r["host"] == host)]
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

        if "FROM CONVERSATION_STREAM" in u:
            # Two anchor shapes must both work:
            #   P0-A      : ... WHERE session_id=%s AND role='user' AND host=%s
            #                     AND host_turn_id=%s
            #   contract  : ... WHERE session_id=%s AND host=%s AND event_id=%s
            if "HOST_TURN_ID=" in u:
                row = self.db.find_u1(p[0], p[1], p[2])
            elif "EVENT_ID=" in u:
                if "HOST=" in u:
                    row = self.db.find_u1_by_event(p[0], p[1], p[2])
                else:
                    row = self.db.find_u1_by_event(p[0], "", p[1])
            else:
                raise RuntimeError(f"fake PG: unmodelled anchor SQL: {s}")
            self._one = (
                (row["id"], row["event_id"], row["turn_id"], row["content"],
                 row["timestamp"], row["tool_calls"], row["tool_results"])
                if row else None
            )
        elif u.startswith("SELECT ID, (EMBEDDING IS NOT NULL) FROM QA_PAIRS"):
            if "WHERE SOURCE_ID" in u:
                row = self.db.find_by_source(p[0])
            else:
                row = self.db.find_by_session_qa(p[0], p[1], p[2])
            self._one = (row["id"], bool(row.get("embedding"))) if row else None
        elif u.startswith("SELECT ID, ANSWER, MERGED_EVENT_IDS, QUESTION FROM QA_PAIRS"):
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
    c = Ctx(tmp_path / "p0c1-base")
    try:
        yield c
    finally:
        c.close()


HOST = "dsh"


def _m(mid, role, content, ts, turn="T1", display_kind=None):
    m = {"id": mid, "role": role, "content": content,
         "timestamp": ts, "turn_id": turn, "host": HOST}
    if display_kind is not None:
        m["display_kind"] = display_kind
    return m


def U(mid, content, ts, turn="T1", display_kind=None):
    return _m(mid, "user", content, ts, turn=turn, display_kind=display_kind)


def A(mid, content, ts, turn="T1", display_kind=None):
    return _m(mid, "assistant", content, ts, turn=turn, display_kind=display_kind)


TS = ["2026-10-04T01:00:%02d+00:00" % i for i in range(60)]


def sync(ctx, session, messages):
    return ctx.core.sync_turn(session, messages, host=HOST)


def drain(ctx, timeout=8.0):
    ctx.core._drain_flush_queue(timeout=timeout)
    end = time.time() + timeout
    while time.time() < end:
        q = ctx.core._flush_queue
        if getattr(q, "unfinished_tasks", 0) == 0 and q.empty():
            break
        time.sleep(0.02)


def flush(ctx):
    """Force-drain every open pending QA (watchdog / end-of-turn equivalent)."""
    ctx.core._flush_all_pending_qa()
    drain(ctx)


def pending_of(ctx, session):
    try:
        c = ctx.core.get_session_context(session, create=False)
    except Exception:
        return None
    p = c.pending_qa if c else None
    if not p:
        return None
    return {"q": (p.get("q") or "")[:60], "a_len": len(p.get("a") or ""),
            "q_msg_id": p.get("q_msg_id")}


def seed_source(ctx, session, mid, content, ts, turn="T1", host=HOST):
    """Model 'the source layer already accepted this user turn durably'.

    Mirrors what ``LiveBuffer._flush`` → ``PgEmbedStore.insert_message``
    writes: ``event_id == msg_id``, ``host_turn_id == turn_id``.  Contract
    §4(b) resolves the snapshot anchor against this row.
    """
    return ctx.db.insert_cs(
        session_id=session, role="user", host=host, host_turn_id=turn,
        event_id=mid, turn_id=turn, content=content, timestamp=ts)


def qa_rows(db, session=None):
    rows = list(db.rows)
    if session is not None:
        rows = [r for r in rows if r["session_id"] == session]
    return rows


def qa_by_question(db, q, session=None):
    return next((r for r in qa_rows(db, session) if r["question"] == q), None)


def snapshot(db, session=None):
    return [{"q": (r["question"] or "")[:48],
             "a": (r["answer"] or "")[:80],
             "mev": list(r.get("merged_event_ids") or [])}
            for r in qa_rows(db, session)]


def answers_blob(db, session=None):
    return "\n".join((r["answer"] or "") for r in qa_rows(db, session))


# ════════════════════════════════════════════════════════════════
# §11 matrix — T1..T11
# ════════════════════════════════════════════════════════════════
def test_t1_normal_u1_a1_single_qa(ctx):
    """T1: plain U1/A1 → exactly one QA (NORMAL QA REGRESSION = 0)."""
    s = "sess-p0c1-t1"
    sync(ctx, s, [U("u1", "Q1 normal", TS[0])])
    sync(ctx, s, [U("u1", "Q1 normal", TS[0]),
                  A("a1", "A1-normal-answer", TS[1])])
    sync(ctx, s, [U("u1", "Q1 normal", TS[0]),
                  A("a1", "A1-normal-answer", TS[1]),
                  U("u2", "Q2 next", TS[2], turn="T2")])
    drain(ctx)
    rows = qa_rows(ctx.db, s)
    assert len(rows) == 1, f"expected exactly one QA; rows={snapshot(ctx.db, s)}"
    assert rows[0]["question"] == "Q1 normal", rows[0]["question"]
    assert "A1-normal-answer" in (rows[0]["answer"] or ""), (
        f"answer missing A1; rows={snapshot(ctx.db, s)}")


def test_t2_multi_assistant_single_qa_replay_no_dup(ctx):
    """T2: U1/A1/A2/A3 → one QA with all three segments; replay no duplicate."""
    s = "sess-p0c1-t2"
    segs = ["A1-seg-one", "A2-seg-two", "A3-seg-three"]
    sync(ctx, s, [U("u1", "Q1 multi", TS[0])])
    sync(ctx, s, [U("u1", "Q1 multi", TS[0]),
                  A("a1", segs[0], TS[1]),
                  A("a2", segs[1], TS[2]),
                  A("a3", segs[2], TS[3])])
    # replay of the identical snapshot: nothing new may be accepted/derived
    r = sync(ctx, s, [U("u1", "Q1 multi", TS[0]),
                      A("a1", segs[0], TS[1]),
                      A("a2", segs[1], TS[2]),
                      A("a3", segs[2], TS[3])])
    assert r["accepted"] == [], f"replay must accept nothing; receipt={r}"
    sync(ctx, s, [U("u1", "Q1 multi", TS[0]),
                  A("a1", segs[0], TS[1]),
                  A("a2", segs[1], TS[2]),
                  A("a3", segs[2], TS[3]),
                  U("u2", "Q2 next", TS[4], turn="T2")])
    drain(ctx)
    rows = qa_rows(ctx.db, s)
    assert len(rows) == 1, f"expected one QA; rows={snapshot(ctx.db, s)}"
    ans = rows[0]["answer"] or ""
    for seg in segs:
        assert ans.count(seg) == 1, (
            f"{seg} must appear exactly once (multi-assistant merge, no dup); "
            f"answer={ans!r} rows={snapshot(ctx.db, s)}")


def test_t3_compaction_continuation_single_qa(ctx):
    """T3: U1/A1/CTX_COMPACTION/A2/A3 → one QA(Q=U1, A=A1+A2+A3), no orphan."""
    s = "sess-p0c1-t3"
    sync(ctx, s, [U("u1", "Q1 compaction", TS[0])])
    sync(ctx, s, [U("u1", "Q1 compaction", TS[0]),
                  A("a1", "A1-pre-compaction", TS[1]),
                  U("ctx", CTX_COMPACTION, TS[2]),
                  A("a2", "A2-post-compaction", TS[3]),
                  A("a3", "A3-post-compaction", TS[4])])
    flush(ctx)
    rows = qa_rows(ctx.db, s)
    assert len(rows) == 1, (
        f"compaction must not open a second QA or orphan the continuation; "
        f"rows={snapshot(ctx.db, s)}")
    assert rows[0]["question"] == "Q1 compaction", rows[0]["question"]
    ans = rows[0]["answer"] or ""
    for seg in ("A1-pre-compaction", "A2-post-compaction", "A3-post-compaction"):
        assert seg in ans, f"{seg} must be folded into the U1 QA; answer={ans!r}"
    assert "[CONTEXT COMPACTION" not in ans, (
        f"compaction marker must not enter the answer body; answer={ans!r}")


def test_t4_compaction_after_flush_structural_continuation(ctx):
    """T4 (BLOCKER): flush, then a full snapshot U1/A1/CTX/A2 → still ONE QA,
    and A2 is durably appended to it via the snapshot anchor.

    A2 deliberately carries **no turn identity**: the contract (§4b) resolves
    the continuation against the snapshot anchor (the U1 REAL_USER row), not
    against the assistant's own ``turn_id``.  This is the case today's code
    drops entirely (pending is gone → no pairing → content lost).
    """
    s = "sess-p0c1-t4"
    seed_source(ctx, s, "u1", "Q1 flush-then-continue", TS[0], turn="T1")
    sync(ctx, s, [U("u1", "Q1 flush-then-continue", TS[0])])
    sync(ctx, s, [U("u1", "Q1 flush-then-continue", TS[0]),
                  A("a1", "A1-pre-flush", TS[1])])
    flush(ctx)
    pre = qa_rows(ctx.db, s)
    assert len(pre) == 1 and "A1-pre-flush" in (pre[0]["answer"] or ""), (
        f"precondition: U1/A1 must be flushed into one QA; rows={snapshot(ctx.db, s)}")

    r = sync(ctx, s, [U("u1", "Q1 flush-then-continue", TS[0]),
                      A("a1", "A1-pre-flush", TS[1]),
                      U("ctx", CTX_COMPACTION, TS[2]),
                      A("a2", "A2-post-compaction-late", TS[3], turn="")])
    flush(ctx)   # end-of-turn watchdog equivalent
    rows = qa_rows(ctx.db, s)
    assert len(rows) == 1, (
        f"after-flush continuation must stay in the SAME QA (no second row, "
        f"no orphan); rows={snapshot(ctx.db, s)} receipt={r}")
    ans = rows[0]["answer"] or ""
    assert "A1-pre-flush" in ans, f"existing answer must survive; answer={ans!r}"
    assert "A2-post-compaction-late" in ans, (
        f"BLOCKER: the post-flush continuation assistant must be durably "
        f"appended to the U1 QA (snapshot anchor recovery); answer={ans!r} "
        f"receipt={r} rows={snapshot(ctx.db, s)}")
    assert ans.count("A2-post-compaction-late") == 1, (
        f"late continuation must land exactly once; answer={ans!r}")
    assert rows[0]["question"] == "Q1 flush-then-continue", rows[0]["question"]


@pytest.mark.parametrize("marker_name,marker", [
    ("tasklist", CTX_TASKLIST),
    ("still_in_progress", CTX_STILL_IN_PROGRESS),
])
def test_t5_tasklist_still_in_progress_continuation(ctx, marker_name, marker):
    """T5: tasklist / still-in-progress behave exactly like T3 (CONTINUATION)."""
    s = f"sess-p0c1-t5-{marker_name}"
    sync(ctx, s, [U("u1", "Q1 ctx-mgmt", TS[0])])
    sync(ctx, s, [U("u1", "Q1 ctx-mgmt", TS[0]),
                  A("a1", "A1-before-ctx", TS[1]),
                  U("ctx", marker, TS[2]),
                  A("a2", "A2-after-ctx", TS[3])])
    flush(ctx)
    rows = qa_rows(ctx.db, s)
    assert len(rows) == 1, (
        f"{marker_name} is CONTINUATION: must not flush/open a QA "
        f"(no second row, no orphan); rows={snapshot(ctx.db, s)}")
    assert rows[0]["question"] == "Q1 ctx-mgmt", rows[0]["question"]
    ans = rows[0]["answer"] or ""
    assert "A1-before-ctx" in ans and "A2-after-ctx" in ans, (
        f"both assistant segments belong to the U1 task chain; answer={ans!r}")
    assert marker[:40] not in ans, (
        f"context-management marker must not enter the answer body; answer={ans!r}")


def test_t6_model_switch_transparent(ctx):
    """T6: U1/A1/SYS_MODEL_SWITCH/A2 → same QA, A1+A2, SYS body not in QA."""
    s = "sess-p0c1-t6"
    sync(ctx, s, [U("u1", "Q1 model-switch", TS[0])])
    sync(ctx, s, [U("u1", "Q1 model-switch", TS[0]),
                  A("a1", "A1-before-switch", TS[1]),
                  U("sys", SYS_MODEL_SWITCH, TS[2], display_kind="model_switch"),
                  A("a2", "A2-after-switch", TS[3])])
    flush(ctx)
    rows = qa_rows(ctx.db, s)
    assert len(rows) == 1, (
        f"model switch is CONTROL/transparent: must not cut the chain; "
        f"rows={snapshot(ctx.db, s)}")
    assert rows[0]["question"] == "Q1 model-switch", rows[0]["question"]
    ans = rows[0]["answer"] or ""
    assert "A1-before-switch" in ans and "A2-after-switch" in ans, (
        f"both segments must pair to U1; answer={ans!r}")
    assert "[System:" not in ans and "[System:" not in rows[0]["question"], (
        f"SYS_MODEL_SWITCH body must not enter the QA; rows={snapshot(ctx.db, s)}")


def test_t7_out_of_band_real_user_opens_new_qa(ctx):
    """T7: U1/A1/OOB('改成 B')/A2 → U1/A1 history kept + NEW QA Q='改成 B', A=A2.

    The OUT-OF-BAND wrapper is a delivery envelope for a REAL user message; the
    payload (not the wrapper) becomes the question, and the previous human QA
    is flushed first (contract §2.2, §3).
    """
    s = "sess-p0c1-t7"
    sync(ctx, s, [U("u1", "Q1 original", TS[0])])
    sync(ctx, s, [U("u1", "Q1 original", TS[0]),
                  A("a1", "A1-original-answer", TS[1]),
                  U("oob", OOB_WRAPPER, TS[2], display_kind="steer"),
                  A("a2", "A2-answer-to-steer", TS[3])])
    flush(ctx)
    rows = qa_rows(ctx.db, s)
    q1 = qa_by_question(ctx.db, "Q1 original", s)
    q2 = qa_by_question(ctx.db, OOB_PAYLOAD, s)
    assert q1 is not None, (
        f"the pre-steer human QA must be preserved; rows={snapshot(ctx.db, s)}")
    assert "A1-original-answer" in (q1["answer"] or ""), (
        f"pre-steer answer must be preserved; q1={q1!r}")
    assert q2 is not None, (
        f"the OOB steer is a REAL user message → it must open its own QA with "
        f"the extracted payload as the question; rows={snapshot(ctx.db, s)}")
    assert q2["question"] == OOB_PAYLOAD, (
        f"the wrapper must never be the question; got {q2['question']!r}")
    assert "A2-answer-to-steer" in (q2["answer"] or ""), (
        f"the reply to the steer must pair to the steer question; q2={q2!r}")
    assert "OUT-OF-BAND" not in (q1["question"] or "") + (q2["question"] or ""), (
        f"wrapper text must not leak into any question; rows={snapshot(ctx.db, s)}")
    assert "A2-answer-to-steer" not in (q1["answer"] or ""), (
        f"the steer reply must NOT fold into the pre-steer QA; q1={q1!r}")


def test_t8_async_batch_complete_no_pollution(ctx):
    """T8: U1/A1/ASYNC_BATCH_COMPLETE/A_async → QA(U1) excludes A_async."""
    s = "sess-p0c1-t8"
    sync(ctx, s, [U("u1", "Q1 async-boundary", TS[0])])
    sync(ctx, s, [U("u1", "Q1 async-boundary", TS[0]),
                  A("a1", "A1-human-answer", TS[1]),
                  U("async", ASYNC_BATCH_COMPLETE, TS[2],
                    display_kind="async_delegation_complete"),
                  A("a_async", "A_ASYNC-autonomous-report", TS[3], turn="")])
    flush(ctx)
    rows = qa_rows(ctx.db, s)
    q1 = qa_by_question(ctx.db, "Q1 async-boundary", s)
    assert q1 is not None, f"the human QA must exist; rows={snapshot(ctx.db, s)}"
    assert "A1-human-answer" in (q1["answer"] or ""), (
        f"human answer must be preserved; q1={q1!r}")
    assert "A_ASYNC-autonomous-report" not in answers_blob(ctx.db, s), (
        f"an async-boundary output must NOT pollute any QA; "
        f"rows={snapshot(ctx.db, s)}")


def test_t9_background_process_complete_no_pollution(ctx):
    """T9: same boundary rule for [IMPORTANT: Background process … completed]."""
    s = "sess-p0c1-t9"
    sync(ctx, s, [U("u1", "Q1 bg-boundary", TS[0])])
    sync(ctx, s, [U("u1", "Q1 bg-boundary", TS[0]),
                  A("a1", "A1-human-answer-bg", TS[1]),
                  U("bg", BG_PROC_COMPLETE, TS[2],
                    display_kind="process_complete"),
                  A("a_bg", "A_BG-autonomous-report", TS[3], turn="")])
    flush(ctx)
    q1 = qa_by_question(ctx.db, "Q1 bg-boundary", s)
    assert q1 is not None, f"the human QA must exist; rows={snapshot(ctx.db, s)}"
    assert "A1-human-answer-bg" in (q1["answer"] or ""), (
        f"human answer must be preserved; q1={q1!r}")
    assert "A_BG-autonomous-report" not in answers_blob(ctx.db, s), (
        f"a background-completion output must NOT pollute any QA; "
        f"rows={snapshot(ctx.db, s)}")


def test_t10_failed_turn_never_an_answer(ctx):
    """T10: U1/failed_turn → the failed_turn notice must never be an answer."""
    s = "sess-p0c1-t10"
    sync(ctx, s, [U("u1", "Q1 failed-turn", TS[0])])
    sync(ctx, s, [U("u1", "Q1 failed-turn", TS[0]),
                  A("failed", FAILED_TURN_NOTICE, TS[1], turn="",
                    display_kind="failed_turn")])
    flush(ctx)
    for r in qa_rows(ctx.db, s):
        assert FAILED_TURN_NOTICE not in (r["answer"] or ""), (
            f"TERMINAL display-only notice must never become an answer; "
            f"rows={snapshot(ctx.db, s)}")
        assert FAILED_TURN_NOTICE not in (r["question"] or ""), (
            f"TERMINAL display-only notice must never become a question; "
            f"rows={snapshot(ctx.db, s)}")


def test_t11_replay_idempotent_structural_continuation(ctx):
    """T11: replay the T4 snapshot ×3 → source / QA / merged_event_ids no dup."""
    s = "sess-p0c1-t11"
    seed_source(ctx, s, "u1", "Q1 replay-continuation", TS[0], turn="T1")
    sync(ctx, s, [U("u1", "Q1 replay-continuation", TS[0])])
    sync(ctx, s, [U("u1", "Q1 replay-continuation", TS[0]),
                  A("a1", "A1-replay", TS[1])])
    flush(ctx)

    snapshot_msgs = [U("u1", "Q1 replay-continuation", TS[0]),
                     A("a1", "A1-replay", TS[1]),
                     U("ctx", CTX_COMPACTION, TS[2]),
                     A("a2", "A2-replay-late", TS[3], turn="")]
    receipts = []
    for _ in range(3):
        r = sync(ctx, s, list(snapshot_msgs))
        receipts.append(r)
        drain(ctx)
    flush(ctx)   # end-of-turn watchdog equivalent

    rows = qa_rows(ctx.db, s)
    assert len(rows) == 1, (
        f"replay must not create extra QA rows; rows={snapshot(ctx.db, s)}")
    ans = rows[0]["answer"] or ""
    assert ans.count("A2-replay-late") == 1, (
        f"replayed late event must be merged exactly once; answer={ans!r} "
        f"receipts={[r.get('derivation') for r in receipts]}")
    assert ans.count("A1-replay") == 1, (
        f"replayed progress event must appear exactly once; answer={ans!r}")
    mev = list(rows[0].get("merged_event_ids") or [])
    assert len(mev) == len(set(mev)), (
        f"merged_event_ids must not accumulate duplicates across replays; "
        f"merged_event_ids={mev!r} rows={snapshot(ctx.db, s)}")
    for i, r in enumerate(receipts[1:], start=2):
        assert r["accepted"] == [], (
            f"replay #{i}: the source delta must accept nothing new; receipt={r}")
        assert r["duplicate"], (
            f"replay #{i}: already-seen events must be reported duplicate; "
            f"receipt={r}")


# ════════════════════════════════════════════════════════════════
# §2 classifier contract (supporting coverage for the fixed interface)
# ════════════════════════════════════════════════════════════════
def test_c0_classifier_module_and_fixtures_available():
    """The classifier module (§2) and the shape fixtures (§12) must exist."""
    assert _HOST_EVENTS_IMPORTED, (
        "v3core.host_events is missing (contract §2). "
        f"import error: {_HOST_EVENTS_IMPORT_ERROR}")
    assert _FIXTURES_IMPORTED, (
        "tests/fixtures/host_events_p0c1.py is missing (contract §5/§12). "
        f"import error: {_FIXTURES_IMPORT_ERROR}")


@pytest.mark.parametrize("content", [
    CTX_COMPACTION, CTX_TASKLIST, CTX_STILL_IN_PROGRESS,
])
def test_c1_continuation_prefixes(content):
    """§2.1(4): CTX compaction / tasklist / still-in-progress → CONTINUATION."""
    assert classify_host_event(role="user", content=content) == HOST_EVENT_CONTINUATION


@pytest.mark.parametrize("content", [ASYNC_BATCH_COMPLETE, BG_PROC_COMPLETE])
def test_c2_async_boundary_prefixes(content):
    """§2.1(5): ASYNC DELEGATION / Background process → ASYNC_BOUNDARY."""
    assert classify_host_event(role="user", content=content) == HOST_EVENT_ASYNC_BOUNDARY


@pytest.mark.parametrize("content", [CRON_JOB, SKILL_INVOKED])
def test_c3_new_root_prefixes(content):
    """§2.1(6): cron job / skill invocation → NEW_ROOT."""
    assert classify_host_event(role="user", content=content,
                               session_source="cron") == HOST_EVENT_NEW_ROOT


@pytest.mark.parametrize("content", [
    TITLE_GEN,
    "You've reached the maximum number of tool-calling rounds",
    "Your previous final response was rejected by the operator",
    "[System: note",
])
def test_c4_control_prefixes(content):
    """§2.1(8): system/control injections → CONTROL (must not cut the chain)."""
    assert classify_host_event(role="user", content=content) == HOST_EVENT_CONTROL


def test_c5_oob_is_real_user_and_payload_extracted():
    """§2.1(3) + §2.2: OOB is a REAL_USER; the wrapper is stripped."""
    assert classify_host_event(role="user", content=OOB_WRAPPER,
                               display_kind="steer") == HOST_EVENT_REAL_USER
    payload = extract_oob_payload(OOB_WRAPPER)
    assert payload.strip() == OOB_PAYLOAD, (
        f"extract_oob_payload must strip the wrapper only; got {payload!r}")
    assert "OUT-OF-BAND" not in payload, (
        f"the wrapper must never be returned as the payload; got {payload!r}")


@pytest.mark.parametrize("display_kind,content", [
    ("failed_turn", FAILED_TURN_NOTICE),
    ("hidden", HIDDEN_NOTICE),
])
def test_c6_terminal_display_kinds(display_kind, content):
    """§2.1(2): display_kind failed_turn/hidden → TERMINAL."""
    assert classify_host_event(role="assistant", content=content,
                               display_kind=display_kind) == HOST_EVENT_TERMINAL


@pytest.mark.parametrize("content", [JUDGE_PROMPT, "@file:notes.md", "普通用户的一句话"])
def test_c7_ordinary_prose_stays_real_user(content):
    """§2.1(9) + §10: judge prompt / @file: / prose must stay REAL_USER."""
    assert classify_host_event(role="user", content=content) == HOST_EVENT_REAL_USER


def test_c8_assistant_and_tool_roles():
    """§2.1(1),(10): normal assistant / tool roles keep their own kinds."""
    assert classify_host_event(role="assistant",
                               content="a normal model answer") == HOST_EVENT_NORMAL_ASSISTANT
    assert classify_host_event(role="tool",
                               content="tool output") == HOST_EVENT_NORMAL_TOOL
