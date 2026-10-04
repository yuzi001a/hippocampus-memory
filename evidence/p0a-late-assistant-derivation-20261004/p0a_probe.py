"""P0-A isolated probe — GREEN verification driver (2026-10-04, v3).

Runs the REAL V3Core.sync_turn / flush / late-derivation / recovery code paths
against:
  * an isolated tmp basePath (no production file is ever touched),
  * a deterministic in-memory fake PG modelling ``qa_pairs`` rows AND
    ``conversation_stream`` source rows (v3 addition, so the P0-A late path's
    durable queries + guarded UPDATE are exercised for real),
  * a real LiveBuffer with the writer thread disabled (durable markers still
    written to the isolated dir, so duplicate-status probing is real).

Scenarios (task-book sec.18):
  S1 baseline            Q -> A normally                       (PASS everywhere)
  S2 post-flush          Q -> A(progress) -> flush -> late A   (RED on main / GREEN with P0-A)
  S3 after-next-user     Q1 -> flush -> Q2 -> late A1          (RED on main / GREEN with P0-A)
  S4 interleaved         session A/B interleaved, late A_A     (RED on main / GREEN with P0-A)
  S5 replay x3           late A replayed 3x, no triplication   (RED on main / GREEN with P0-A)
  S6 restart             late A after a fresh core instance    (RED on main / GREEN with P0-A)

The v2 version of this probe (RED-evidence generator whose fake modelled only
the pre-fix qa_pairs shape) is preserved as ``p0a_probe_red_v2.py``; its RED
run is archived as p0a-probe-red-v2-20261004_115039.{json,log}.

Source rows: the probe seeds the owning user turn directly into the fake
conversation_stream (host_turn_id = the message's turn_id) to model "the
source layer already accepted U1 durably" — the enqueue->PG write chain
itself is covered by the data-path probe and the Y400 E2E.

No LLM, no embedding, no network, no production DB.
Usage:
    python p0a_probe.py [--expect green|red] [--out result.json]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src" / "v3-core" / "src"))

# ── SAFETY 1: hard-block real PostgreSQL from this probe ─────────────────────
# Found 2026-10-04: an earlier version of this probe (without this guard) let
# LiveBuffer._try_reconnect() fall through to PgEmbedStore's legacy defaults
# (localhost:5433/v3embeddings) and 4 probe rows reached the production
# conversation_stream table.  Same defence as tests/conftest.py "防线 4".
import psycopg2  # noqa: E402

_real_psycopg2_connect = psycopg2.connect


def _blocked_pg_connect(*a, **k):
    raise AssertionError(
        "P0-A probe: real PostgreSQL connections are blocked in this process "
        "(isolation guard — see FINDINGS-20261004.md)")


psycopg2.connect = _blocked_pg_connect  # type: ignore[assignment]

# ── SAFETY 2: production profile sentinel snapshot ───────────────────────────
PROD_PROFILE = Path.home() / ".v3-core" / "profiles" / "default"


def _snapshot(root: Path):
    out = {}
    if not root.exists():
        return out
    for dp, _dn, fn in os.walk(root):
        for n in fn:
            p = Path(dp) / n
            try:
                st = p.stat()
            except OSError:
                continue
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                rel = str(p)
            out[rel] = (st.st_size, int(st.st_mtime))
    return out


def _snapshot_diff(before: dict, after: dict):
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in (set(before) & set(after)) if before[k] != after[k])
    return {"added": added, "removed": removed, "changed": changed}


from v3core import V3Core  # noqa: E402
from v3core.ingest import LiveBuffer  # noqa: E402


# ────────────────────────────────────────────────────────────────
# deterministic in-memory fake PG
#   qa_pairs          (pre-existing model + merged_event_ids ledger)
#   conversation_stream (v3: durable source rows for the late path)
# ────────────────────────────────────────────────────────────────
def _coerce_mev(value):
    """Model PG's jsonb semantics for merged_event_ids.

    Real psycopg2 hands a ``%s::jsonb`` parameter over as the raw JSON text
    and reads the column back as a list.  The fake must do the same: a str
    parameter is parsed (NOT iterated char-by-char), a list stays a list."""
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
        """Model the P0-A atomic guarded UPDATE (WHERE NOT members @> [msg_id]).

        Returns rowcount: 1 when this call appended, 0 when the guard shows the
        event is already a member (concurrent/duplicate writer)."""
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
    def __init__(self) -> None:
        self.db = FakeQaDb()
        self.tmp = Path(tempfile.mkdtemp(prefix="p0a-probe-"))
        self.core = self._new_core(self.tmp)

    def _new_core(self, tmp):
        core = V3Core(profile="default")
        cfg = {"basePath": str(tmp)}
        core._config = cfg
        buf = LiveBuffer(pg=None, config=cfg)
        buf._start_writer = lambda: None
        core._live_buffer = buf
        core._pg = FakePg(self.db)
        import v3core.observer as _obs
        self._orig_maybe_observe = _obs.maybe_observe
        _obs.maybe_observe = lambda *a, **k: None
        return core

    def restart(self):
        """Simulate a process restart: brand-new V3Core over the same dir/db."""
        try:
            self.core.shutdown(timeout=0.2)
        except Exception:
            pass
        self.core = self._new_core(self.tmp)

    def close(self):
        try:
            self.core._qa_flush_timer and self.core._qa_flush_timer.cancel()
        except Exception:
            pass
        try:
            self.core.shutdown(timeout=0.2)
        except Exception:
            pass
        import v3core.observer as _obs
        _obs.maybe_observe = self._orig_maybe_observe
        shutil.rmtree(self.tmp, ignore_errors=True)


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

    Mirrors what LiveBuffer._flush -> PgEmbedStore.insert_message writes:
    event_id == msg_id (legacy alias), host_turn_id == the message's turn_id.
    The enqueue->PG write chain itself is covered by the data-path probe and
    the Y400 E2E; this probe only needs the durable premise to be present."""
    return ctx.db.insert_cs(
        session_id=session, role="user", host=host, host_turn_id=turn,
        event_id=mid, turn_id=turn, content=content, timestamp=ts)


S1 = "sess-p0a-s1"; S2 = "sess-p0a-s2"; S3 = "sess-p0a-s3"
SA = "sess-p0a-sA"; SB = "sess-p0a-sB"; S5 = "sess-p0a-s5"; S6 = "sess-p0a-s6"


def qa_by_question(db, q):
    return next((r for r in db.rows if r["question"] == q), None)


# ── scenarios ───────────────────────────────────────────────────
def s1_baseline(ctx):
    sync(ctx, S1, [msg("u1", "user", "Q1 baseline", TS[0])])
    sync(ctx, S1, [msg("u1", "user", "Q1 baseline", TS[0]),
                   msg("a1", "assistant", "A1 immediate", TS[1])])
    sync(ctx, S1, [msg("u1", "user", "Q1 baseline", TS[0]),
                   msg("a1", "assistant", "A1 immediate", TS[1]),
                   msg("u2", "user", "Q2 next", TS[2], turn="T2")])
    drain(ctx)
    r = qa_by_question(ctx.db, "Q1 baseline")
    ok = bool(r) and "A1 immediate" in (r["answer"] or "")
    return ok, {"rows": len(ctx.db.rows), "answer": (r or {}).get("answer", "")}


def s2_post_flush(ctx):
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
    ok = (bool(row) and "A3-FINAL-LATE" in ans and "A1-PROGRESS" in ans
          and ans.count("A1-PROGRESS") == 1 and ans.count("A3-FINAL-LATE") == 1)
    return ok, {"rows": len(ctx.db.rows), "answer": ans,
                "derivation": (r.get("derivation") or {}),
                "events": (r.get("events") or {})}


def s3_after_next_user(ctx):
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
    ok = (bool(q1) and "A1-LATE-FOR-T1" in (q1["answer"] or "")
          and (q2 is None or "A1-LATE-FOR-T1" not in (q2["answer"] or ""))
          and pend and pend["q"] == "Q2 second")
    return ok, {"q1": (q1 or {}).get("answer", None), "q2": q2 and q2["answer"],
                "pending": pend,
                "derivation": (r.get("derivation") or {}),
                "events": (r.get("events") or {})}


def s4_interleaved(ctx):
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
    ok = (bool(qa) and "A_A-LATE" in (qa["answer"] or "")
          and (qb2 is None) and "A_A-LATE" not in json.dumps(others, default=str))
    return ok, {"Q_A": (qa or {}).get("answer"), "Q_B2_row": qb2,
                "derivation": (r.get("derivation") or {}),
                "all_rows": [(r0["question"][:20], (r0["answer"] or "")[:30])
                             for r0 in ctx.db.rows]}


def s5_replay(ctx):
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
    ok = len(rows) == 1 and marker_count == 1
    return ok, {"qa_rows": len(rows), "marker_count": marker_count,
                "answer": ans, "receipts": receipts}


def s6_restart(ctx):
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
    ok = (bool(row) and "A9-FINAL-AFTER-RESTART" in ans
          and ans.count("A1-PROG") == 1 and ans.count("A9-FINAL-AFTER-RESTART") == 1)
    return ok, {"rows": len(ctx.db.rows), "answer": ans,
                "derivation": (r.get("derivation") or {}),
                "events": (r.get("events") or {})}


SCEN = [
    ("S1 baseline Q->A", s1_baseline, True),
    ("S2 Q->flush->late A", s2_post_flush, False),
    ("S3 Q1->flush->Q2->late A1", s3_after_next_user, False),
    ("S4 interleaved sessions", s4_interleaved, False),
    ("S5 replay x3", s5_replay, False),
    ("S6 restart recovery", s6_restart, False),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", choices=["green", "red"], default="green",
                    help="green: all scenarios must PASS; red: must match "
                         "the unfixed-main expectation signature")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    results = {}
    fs_before = _snapshot(PROD_PROFILE)
    print(f"branch HEAD: {__import__('subprocess').check_output(['git','-C',str(REPO),'rev-parse','HEAD']).decode().strip()}")
    print(f"production profile sentinel: {PROD_PROFILE} ({len(fs_before)} files)")
    for name, fn, expect_pass_on_main in SCEN:
        ctx = Ctx()
        try:
            ok, observed = fn(ctx)
        except Exception as e:
            ok, observed = False, {"error": f"{type(e).__name__}: {e}",
                                   "trace": traceback.format_exc()[-800:]}
        finally:
            ctx.close()
        verdict = "PASS" if ok else "FAIL"
        results[name] = {"verdict": verdict, "observed": observed,
                         "expected_on_unfixed_main": "PASS" if expect_pass_on_main else "FAIL"}
        print(f"[{verdict}] {name}")
        if not ok:
            print("     " + json.dumps(observed, ensure_ascii=False)[:400])

    fs_after = _snapshot(PROD_PROFILE)
    fs_diff = _snapshot_diff(fs_before, fs_after)
    # noise filter: the live gateway legitimately writes these while we run.
    # Anything carrying p0a markers, or touching sensitive sinks, is flagged.
    def _suspicious(p: str) -> bool:
        lp = p.lower()
        return ("p0a" in lp or "sess-p0a" in lp
                or lp.startswith("j/pending_qa") or lp.startswith("j/pending_live_buffer")
                or lp.startswith("j/_lost") or lp in ("state.json", "observer_state.json"))

    suspicious = ([p for p in fs_diff["added"] if _suspicious(p)]
                  + [p for p in fs_diff["changed"] if _suspicious(p)]
                  + [p for p in fs_diff["removed"] if _suspicious(p)])
    print(f"\nprod profile diff: +{len(fs_diff['added'])} -{len(fs_diff['removed'])} "
          f"~{len(fs_diff['changed'])} (gateway noise expected)")
    print(f"suspicious (p0a/sensitive sinks): {suspicious or 'NONE'}")

    if args.expect == "green":
        sig_ok = all(r["verdict"] == "PASS" for r in results.values())
        label = "GREEN signature (all scenarios PASS)"
    else:
        sig_ok = all(
            (r["verdict"] == "PASS") == (r["expected_on_unfixed_main"] == "PASS")
            for r in results.values())
        label = "RED signature (matches unfixed-main expectations)"
    summary = {"when": datetime.now(timezone.utc).isoformat(),
               "expect": args.expect,
               "scenarios": results,
               "signature_ok": sig_ok,
               "fs_sentinel": {"added": len(fs_diff["added"]),
                               "removed": len(fs_diff["removed"]),
                               "changed": len(fs_diff["changed"]),
                               "suspicious": suspicious,
                               "diff_sample": {k: fs_diff[k][:20] for k in fs_diff}}}
    print(f"\n{label}: {sig_ok}")
    if args.out:
        Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"written: {args.out}")
    return 0 if (sig_ok and not suspicious) else 1


if __name__ == "__main__":
    sys.exit(main())
