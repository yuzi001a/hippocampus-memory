"""P0-A isolated probe — RED/GREEN evidence driver (2026-10-04).

Runs the REAL V3Core.sync_turn / flush / recovery code paths against:
  * an isolated tmp basePath (no production file is ever touched),
  * a deterministic in-memory fake PG modelling ``qa_pairs`` rows,
  * a real LiveBuffer with the writer thread disabled (durable markers still
    written to the isolated dir, so duplicate-status probing is real).

Scenarios (task-book sec.18):
  S1 baseline            Q -> A normally                      (PASS on main)
  S2 post-flush          Q -> A(progress) -> flush -> late A  (RED on main)
  S3 after-next-user     Q1 -> flush -> Q2 -> late A1         (RED on main)
  S4 interleaved         session A/B interleaved, late A_A    (RED on main)
  S5 replay x3           late A replayed 3x, no triplication  (RED on main)
  S6 restart             late A after a fresh core instance   (RED on main)

No LLM, no embedding, no network, no production DB.
Usage:
    python p0a_probe.py [--out result.json]
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
# deterministic in-memory fake PG (qa_pairs model only)
# ────────────────────────────────────────────────────────────────
class FakeQaDb:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._next = 1

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
               embed_model=""):
        if self.find_by_source(source_id):
            return False
        self.rows.append({
            "id": self._next, "source_id": source_id, "session_id": session_id,
            "turn_id": turn_id, "question": question, "answer": answer,
            "tool_calls": tool_calls, "tool_results": tool_results,
            "timestamp": timestamp, "source": source,
            "embedding": embedding, "embed_model": embed_model,
        })
        self._next += 1
        return True

    def update_late_merge(self, row_id, content, msg_id):
        """Model the atomic guarded UPDATE implemented by the fix."""
        raise RuntimeError(
            "late merge attempted but this build has no P0-A UPDATE path")


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
        if u.startswith("SELECT ID, (EMBEDDING IS NOT NULL) FROM QA_PAIRS"):
            if "WHERE SOURCE_ID" in u:
                row = self.db.find_by_source(p[0])
            else:
                row = self.db.find_by_session_qa(p[0], p[1], p[2])
            self._one = (row["id"], bool(row.get("embedding"))) if row else None
        elif u.startswith("INSERT INTO QA_PAIRS"):
            if len(p) >= 11:
                source_id, session_id, turn_id, q, a, tc, tr, ts, src, emb, fp = p[:11]
            else:
                source_id, session_id, turn_id, q, a, tc, tr, ts, src = p[:9]
                emb, fp = None, ""
            ok = self.db.insert(
                source_id=source_id, session_id=session_id, turn_id=turn_id,
                question=q, answer=a, tool_calls=tc, tool_results=tr,
                timestamp=ts, source=src, embedding=emb, embed_model=fp)
            self.rowcount = 1 if ok else 0
        elif u.startswith("SELECT ID FROM QA_PAIRS WHERE SOURCE_ID"):
            row = self.db.find_by_source(p[0])
            self._one = (row["id"],) if row else None
        elif u.startswith("UPDATE QA_PAIRS"):
            # P0-A late merge path (guard: NOT merged_event_ids @> [msg_id])
            self._one = None
            self.db.update_late_merge(params[3] if len(p) > 3 else None,
                                      params[0], "?")
            self.rowcount = 1
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


S1 = "sess-p0a-s1"; S2 = "sess-p0a-s2"; S3 = "sess-p0a-s3"
SA = "sess-p0a-sA"; SB = "sess-p0a-sB"; S6 = "sess-p0a-s6"


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
    ok = bool(row) and "A3-FINAL-LATE" in ans and "A1-PROGRESS" in ans
    return ok, {"rows": len(ctx.db.rows), "answer": ans,
                "late_receipt": {k: r.get(k) for k in ("accepted", "duplicate", "held")}}


def s3_after_next_user(ctx):
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
                "late_receipt": {k: r.get(k) for k in ("accepted", "duplicate", "held")}}


def s4_interleaved(ctx):
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
    ok = (bool(qa) and "A_A-LATE" in (qa["answer"] or "")
          and (qb2 is None) and "A_A-LATE" not in json.dumps(ctx.db.rows))
    return ok, {"Q_A": (qa or {}).get("answer"), "Q_B2_row": qb2,
                "all_rows": [(r0["question"][:20], (r0["answer"] or "")[:30]) for r0 in ctx.db.rows]}


def s5_replay(ctx):
    sync(ctx, S2, [msg("u1", "user", "Q1 replay", TS[0])])
    ctx.core._flush_all_pending_qa()
    drain(ctx)
    receipts = []
    for i in range(3):
        r = sync(ctx, S2, [msg("u1", "user", "Q1 replay", TS[0]),
                           msg("a1", "assistant", "A1-LATE-MARKER", TS[3])])
        receipts.append({k: r.get(k) for k in ("accepted", "duplicate", "held")})
        drain(ctx)
    rows = [r for r in ctx.db.rows if r["question"] == "Q1 replay"]
    ans = rows[0]["answer"] if rows else ""
    marker_count = (ans or "").count("A1-LATE-MARKER")
    ok = len(rows) == 1 and marker_count == 1
    return ok, {"qa_rows": len(rows), "marker_count": marker_count,
                "answer": ans, "receipts": receipts}


def s6_restart(ctx):
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
    ok = bool(row) and "A9-FINAL-AFTER-RESTART" in ans
    return ok, {"rows": len(ctx.db.rows), "answer": ans,
                "late_receipt": {k: r.get(k) for k in ("accepted", "duplicate", "held")}}


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

    red_ok = all(
        (r["verdict"] == "PASS") == (r["expected_on_unfixed_main"] == "PASS")
        for r in results.values())
    summary = {"when": datetime.now(timezone.utc).isoformat(),
               "scenarios": results,
               "red_signature_as_expected": red_ok,
               "fs_sentinel": {"added": len(fs_diff["added"]),
                               "removed": len(fs_diff["removed"]),
                               "changed": len(fs_diff["changed"]),
                               "suspicious": suspicious,
                               "diff_sample": {k: fs_diff[k][:20] for k in fs_diff}}}
    print(f"\nRED signature as expected: {red_ok}")
    if args.out:
        Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"written: {args.out}")
    return 0 if (red_ok and not suspicious) else 1


if __name__ == "__main__":
    sys.exit(main())
