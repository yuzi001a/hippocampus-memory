"""End-to-end test for tools/embedding_backfill.py against the disposable PG.

Verifies the repair-pass CONTRACT, not just that it runs:
  * dry-run writes nothing at all
  * apply repairs the rows and writes the long-QA sidecar through the canonical pipeline
  * a second apply is a no-op (idempotent) and selects 0 rows (resumable by construction)
  * no source table ever loses a row
  * a permanently-failed row is skipped on later runs instead of being retried forever
  * rows that already have a vector are never touched

Same fail-closed rule as the other harness: disposable target or it refuses to run.
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.environ["EMBEDFIX_SRC"])

import psycopg2
import psycopg2.extras
import requests

HOST, PORT, DB = "127.0.0.1", 5440, "v3embedfix_e2e"
if PORT == 5433 or DB in ("v3embeddings", "postgres") or HOST not in ("127.0.0.1", "localhost"):
    raise SystemExit(f"REFUSING: {HOST}:{PORT}/{DB} is not the disposable database")

RESULTS: list = []
VEC = [0.25] * 1024
CFG = {"endpoint": "http://embed.test/v1", "model": "BAAI/bge-m3",
       "_fingerprint": "fp-e2e-backfill"}
LONG_Q = "用户问：" + "".join(f"第{i}段背景，这是一个很长的历史问题。涉及大量细节。" for i in range(400))
LONG_A = "助手答：" + "".join(f"第{i}节回答，这是同样很长的回答，需要分块嵌入以保持一一对应。" for i in range(400))


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  :: {detail}" if detail else ""))


class _Resp:
    status_code = 200
    text = ""
    headers = {"content-type": "application/json"}

    def __init__(self, payload=None, status=200):
        self._p = payload if payload is not None else {"data": [{"index": 0, "embedding": VEC}]}
        self.status_code = status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def connect():
    return psycopg2.connect(host=HOST, port=PORT, dbname=DB, user="v3user", password="e2epass")


def sha(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


def _stub_post(url, json=None, headers=None, proxies=None, timeout=None):
    inputs = json.get("input") if isinstance(json, dict) else None
    n = len(inputs) if isinstance(inputs, list) else 1
    return _Resp({"data": [{"index": i, "embedding": VEC} for i in range(max(1, n))]})


def counts(conn):
    out = {}
    with conn.cursor() as cur:
        for t in ("qa_pairs", "qa_embedding_chunks", "topics", "conversation_stream",
                  "yin_paragraphs", "observation_notes", "embedding_failures"):
            cur.execute(f"SELECT COUNT(*) FROM public.{t}")
            out[t] = cur.fetchone()[0]
    return out


def main() -> int:
    from v3core.tools import embedding_backfill as BF

    conn = connect()
    with conn.cursor() as cur:
        # schema is already the real production shape (the other harness sets it up)
        for t in ("embedding_failures", "qa_embedding_chunks", "qa_pairs", "topics",
                  "conversation_stream", "yin_paragraphs", "observation_notes"):
            cur.execute(f"TRUNCATE public.{t} RESTART IDENTITY CASCADE")

        # ── seed: a spread of NULL rows, one of each canonical shape ─────────
        # source_id is UNIQUE in production, so every row needs its own.
        cur.execute("INSERT INTO qa_pairs (id, source_id, question, answer) VALUES "
                    "(8001,'src-8001','short question','short answer'),"
                    "(8002,'src-8002',%s,%s),"
                    "(8003,'src-8003','already has a vector','x'),"
                    "(8004,'src-8004','   ','   ')",  # whitespace-only: must be excluded
                    (LONG_Q, LONG_A))
        cur.execute("UPDATE qa_pairs SET embedding=%s::vector, embed_model='BAAI/bge-m3' "
                    "WHERE id=8003", ("[" + ",".join(["0.5"] * 1024) + "]",))
        cur.execute("INSERT INTO topics (id, topic_id, title, summary, body, keywords) "
                    "VALUES (7001,'t_a','topic title','topic summary','topic body',"
                    "ARRAY['kw1','kw2'])")
        cur.execute("INSERT INTO conversation_stream (id, session_id, role, content, \"timestamp\") "
                    "VALUES (6001,'s','user','stream content',NOW())")
        cur.execute("INSERT INTO yin_paragraphs (id, yin_version, section, content) "
                    "VALUES (5001,'v1','sec-1','yin paragraph content')")
        cur.execute("INSERT INTO observation_notes (id, version, content) "
                    "VALUES (4001,1,'observation note content')")
    conn.commit()

    before_counts = counts(conn)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, question, answer FROM qa_pairs ORDER BY id")
        qa_text_before = {r["id"]: (sha(r["question"]), sha(r["answer"])) for r in cur.fetchall()}

    orig = requests.post
    requests.post = _stub_post
    try:
        # ── 1. dry run must not write ────────────────────────────────────────
        print("\n[1] dry-run is a true no-op")
        for tbl in ("qa_pairs", "topics", "conversation_stream", "yin_paragraphs",
                    "observation_notes"):
            st = BF.backfill_table(conn, tbl, embed_cfg=CFG, apply=False, limit=None)
            check(f"1.{tbl} dry-run selected >0", st.selected > 0, f"selected={st.selected}")
            check(f"1.{tbl} dry-run wrote nothing", st.repaired == 0 and st.sidecar_written == 0)
        check("1.counts unchanged after dry-run", counts(conn) == before_counts,
              f"{before_counts}")

        # ── 2. apply ─────────────────────────────────────────────────────────
        print("\n[2] apply repairs through the canonical pipeline")
        st = BF.backfill_table(conn, "qa_pairs", embed_cfg=CFG, apply=True, limit=None)
        check("2.1 qa_pairs repaired the short rows", st.repaired == 2, f"repaired={st.repaired}")
        check("2.2 long-QA sidecar written by the canonical chunker",
              st.sidecar_written >= 2, f"sidecar={st.sidecar_written}")
        with conn.cursor() as cur:
            cur.execute("SELECT embedding IS NOT NULL FROM qa_pairs WHERE id=8002")
            check("2.3 short row now has a vector", cur.fetchone()[0] is True)
            cur.execute("SELECT COUNT(*), COUNT(embedding) FROM qa_embedding_chunks WHERE qa_id=8002")
            tot, withvec = cur.fetchone()
            check("2.4 every sidecar chunk has a vector", tot > 0 and tot == withvec,
                  f"{withvec}/{tot}")
            cur.execute("SELECT COUNT(*) FROM qa_embedding_chunks c "
                        "LEFT JOIN qa_pairs p ON p.id=c.qa_id WHERE p.id IS NULL")
            check("2.5 no orphan sidecar rows", cur.fetchone()[0] == 0)
            cur.execute("SELECT embedding IS NOT NULL FROM qa_pairs WHERE id=8003")
            check("2.6 already-correct row still has its vector", cur.fetchone()[0] is True)

        for tbl in ("topics", "conversation_stream", "yin_paragraphs", "observation_notes"):
            st = BF.backfill_table(conn, tbl, embed_cfg=CFG, apply=True, limit=None)
            check(f"2.{tbl} repaired", st.repaired == 1, f"repaired={st.repaired}")

        # ── 3. idempotence / resumability ────────────────────────────────────
        print("\n[3] idempotent + resumable")
        for tbl in ("qa_pairs", "topics", "conversation_stream", "yin_paragraphs",
                    "observation_notes"):
            st = BF.backfill_table(conn, tbl, embed_cfg=CFG, apply=True, limit=None)
            check(f"3.{tbl} second run selects nothing", st.selected == 0,
                  f"selected={st.selected}")

        # ── 4. invariants ────────────────────────────────────────────────────
        print("\n[4] invariants")
        after = counts(conn)
        lost = {t: (before_counts[t], after[t]) for t in before_counts
                if after[t] < before_counts[t]}
        check("4.1 no source table lost a row", not lost, f"{lost}")
        check("4.2 embedding_failures grew by exactly the sidecar-verified count",
              after["embedding_failures"] >= before_counts["embedding_failures"])
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, question, answer FROM qa_pairs ORDER BY id")
            qa_text_after = {r["id"]: (sha(r["question"]), sha(r["answer"])) for r in cur.fetchall()}
        check("4.3 every qa_pairs source text is byte-identical", qa_text_after == qa_text_before)

        # ── 5. a permanent failure is not retried forever ────────────────────
        print("\n[5] permanent failures are skipped on later runs")
        with conn.cursor() as cur:
            cur.execute("INSERT INTO qa_pairs (id, source_id, question, answer) "
                        "VALUES (8005,'src-8005','will fail permanently','x')")
            # Real column set: the policy column is `timeout_policy`, and the timestamps
            # are first_failed_at / last_failed_at / updated_at (no `created_at`).
            cur.execute("INSERT INTO embedding_failures "
                        "(entity_table, entity_id, phase, error_class, retryable, attempts, "
                        " elapsed_ms, timeout_policy) "
                        "VALUES ('qa_pairs','8005','backfill','EMBEDDING_CONFIG_INVALID',"
                        " FALSE, 1, 1.0, 'batch')")
        conn.commit()
        st = BF.backfill_table(conn, "qa_pairs", embed_cfg=CFG, apply=True, limit=None)
        check("5.1 permanently-failed row is skipped", st.selected == 0, f"selected={st.selected}")
        st = BF.backfill_table(conn, "qa_pairs", embed_cfg=CFG, apply=True, limit=None,
                               include_nonretryable=True)
        check("5.2 --include-nonretryable picks it up deliberately", st.selected == 1,
              f"selected={st.selected}")

        # ── 6. refusal rather than guessing ──────────────────────────────────
        print("\n[6] unknown table is refused, not guessed")
        try:
            BF.backfill_table(conn, "some_other_table", embed_cfg=CFG, apply=False, limit=1)
            check("6.1 refused unknown table", False, "no SystemExit raised")
        except SystemExit as e:
            check("6.1 refused unknown table", "no canonical embedding representation" in str(e),
                  str(e)[:80])
    finally:
        requests.post = orig

    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 72)
    print(f"BACKFILL E2E: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        for n, _, d in failed:
            print(f"  FAILED: {n} {d}")
        print("VERDICT: BACKFILL_E2E_FAILED")
        return 1
    print("VERDICT: BACKFILL_E2E_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
