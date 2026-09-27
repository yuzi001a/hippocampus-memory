"""Disposable-PG end-to-end test for the embedding write-reliability hotfix.

Runs ONLY against the disposable container on 127.0.0.1:5440. A fail-closed guard
refuses to proceed if the target looks like production (port 5433 / db v3embeddings),
so this script can never accidentally repair production.

Proves (PHASE 15):
  * source truth is preserved when an embedding fails
  * a failed request leaves a durable, explainable marker
  * a repair pass fills only the retryable NULLs and resolves their markers
  * non-target rows are never touched; nothing is ever DELETEd
  * the long-QA path rebuilds sidecar chunks with valid parent/child provenance

Usage:  EMBEDFIX_SRC=<src dir> EMBEDFIX_MIGRATION=<sql> python embedfix_e2e.py
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import psycopg2
import psycopg2.extras
import requests

PG = {
    "host": "127.0.0.1",
    "port": 5440,
    "dbname": "v3embedfix_e2e",
    "user": "v3user",
    "password": "e2epass",
}
PRODUCTION_PORTS = {5433}
PRODUCTION_DBS = {"v3embeddings", "postgres"}
CFG = {
    "endpoint": "http://embed.test/v1/embeddings",
    "model": "BAAI/bge-m3",
    "apiKey": "e2e-key-not-a-real-secret",
    "_fingerprint": "fp-e2e",
}
VEC = [0.25] * 1024

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  :: {detail}" if detail else ""))


def _assert_disposable() -> None:
    if PG["port"] in PRODUCTION_PORTS:
        sys.exit(f"REFUSING TO RUN: port {PG['port']} is production")
    if PG["dbname"] in PRODUCTION_DBS:
        sys.exit(f"REFUSING TO RUN: database {PG['dbname']} is production")
    if PG["host"] not in ("127.0.0.1", "localhost"):
        sys.exit(f"REFUSING TO RUN: host {PG['host']} is not loopback")
    print(f"[guard] target is DISPOSABLE: {PG['host']}:{PG['port']}/{PG['dbname']}")


def connect():
    c = psycopg2.connect(**PG)
    c.autocommit = True
    return c


def sha(t: str) -> str:
    return hashlib.sha256((t or "").encode("utf-8")).hexdigest()[:16]


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._p = payload if payload is not None else {"data": [{"embedding": VEC}]}
        self.text = text or json.dumps(self._p)

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            e = requests.HTTPError(f"HTTP {self.status_code}")
            e.response = self
            raise e


# The fixture schema is DERIVED FROM PRODUCTION, not hand-written.
#
# An earlier revision of this harness declared its own tables and invented column names
# (`parent_id` / `chunk_text` for qa_embedding_chunks, TEXT ids everywhere) where production
# has `qa_id` / `content`, six provenance columns, and BIGINT ids. That harness would have
# passed while validating a schema that does not exist — the failure mode the user calls
# "the test rig wiping the product's arse". Loading the real DDL removes the whole class of
# drift, and the conformance check below fails loudly if the two ever diverge again.
SCHEMA_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "production_schema_subset.sql")

# (table, column) pairs this harness depends on. If production renames one of these, the
# harness must stop, not silently keep testing the old shape.
REQUIRED_COLUMNS = {
    "conversation_stream": {"id", "content", "embedding"},   # NOTE: no embed_model column — production has none, and no production writer sets one
    "topics": {"id", "topic_id", "title", "embedding", "embed_model"},
    "qa_pairs": {"id", "source_id", "question", "answer", "embedding", "embed_model"},
    "qa_embedding_chunks": {"id", "qa_id", "chunk_index", "source_field", "source_start",
                            "source_end", "source_sha256", "token_count",
                            "representation_version", "embedding", "embed_model", "content"},
    "yin_paragraphs": {"id", "yin_version", "section", "content", "embedding", "embed_model"},
    "observation_notes": {"id", "content", "embedding", "embed_model"},
}

# Production `conversation_stream.id` is BIGINT and the failure marker joins on `t.id::text`,
# so the harness uses integer row ids and derives the marker id from them. The readable
# labels exist only in assertion output. (An earlier revision used TEXT row ids, which
# production does not have.)
SID = {f"live/s/{i}": 100 + i for i in range(1, 8)}
REV = {v: k for k, v in SID.items()}
QA_LONG_ID = 9001
TOPIC_UNRELATED_ID = 9002

# The long-QA gate is a real TOKENIZER count (`counter(short_text) <= target_tokens`,
# target ~7680 after the safety margin) — not a byte count. Chinese runs ~1 token/char,
# so a "long" fixture that quietly lands on the short path would make this section pass
# while testing nothing. 400 reps ≈ 15k chars ≈ well past the target.
LONG_Q = "用户问：" + "".join(f"第{i}段背景，这是一个很长的历史问题。涉及大量细节。" for i in range(400))
LONG_A = "助手答：" + "".join(f"第{i}节回答，这是同样很长的回答，需要分块嵌入以保持一一对应。" for i in range(400))


def main() -> int:
    _assert_disposable()
    sys.path.insert(0, os.environ["EMBEDFIX_SRC"])

    from v3core.embedding import (
        EmbeddingCallError, EmbedErrorClass, DURABLE_WRITE_EMBED_POLICY,
        BATCH_EMBED_POLICY, embed_batch,
    )
    from v3core.embed_chunks import build_qa_embedding_representation, aggregate_parent_embedding
    from v3core import embed_failures as EF

    print(f"[import] embed_failures from {EF.__file__}")
    print(f"[import] embed_chunks from {build_qa_embedding_representation.__module__}")

    conn = connect()
    with conn.cursor() as cur:
        # The fixture DDL is the real production DDL (no IF NOT EXISTS), so start from a
        # clean schema. Safe only because the target is a disposable container — which the
        # fail-closed guard above already proved.
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute(open(SCHEMA_SQL, encoding="utf-8").read())
        cur.execute(open(os.environ["EMBEDFIX_MIGRATION"], encoding="utf-8").read())
        # ── schema conformance: the fixture must BE production's shape ───────
        for tbl, need in REQUIRED_COLUMNS.items():
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=%s", (tbl,))
            have = {r[0] for r in cur.fetchall()}
            missing = need - have
            if missing:
                raise SystemExit(
                    f"SCHEMA DRIFT: {tbl} is missing {sorted(missing)} in the fixture "
                    f"(have {sorted(have)}). The fixture DDL is stale relative to "
                    f"production — regenerate production_schema_subset.sql."
                )
        print(f"[schema] conformance OK for {len(REQUIRED_COLUMNS)} tables "
              f"(fixture derived from production DDL)")
        for t in ("embedding_failures", "conversation_stream", "topics", "qa_pairs",
                  "yin_paragraphs", "observation_notes", "qa_embedding_chunks"):
            cur.execute(f"TRUNCATE public.{t} RESTART IDENTITY CASCADE")

    # ─── seed ────────────────────────────────────────────────────────────────
    ids = ["live/s/1", "live/s/2", "live/s/3", "live/s/4", "live/s/5", "live/s/6"]
    with conn.cursor() as cur:
        for sid in ids:
            # live/s/4 models a genuinely empty source: the ROW content is empty, which is
            # what makes "no embedding, no marker" correct rather than a hole. Giving it
            # non-empty content while passing "" to the embedder would have been an
            # unrealistic fixture that made the metric look broken.
            body = "" if sid == "live/s/4" else f"body of {sid}"
            cur.execute(
                "INSERT INTO conversation_stream "
                "(id, session_id, role, content, \"timestamp\") "
                "VALUES (%s, 'sess-e2e', 'user', %s, NOW())",
                (SID[sid], body))
        # a row that is already correct — must never be touched by repair
        cur.execute("UPDATE conversation_stream SET embedding=%s::vector WHERE id=%s",
                    ("[" + ",".join(["0.5"] * 1024) + "]", SID["live/s/5"]))
        # a long QA pair that requires chunked embedding
        cur.execute("INSERT INTO qa_pairs (id, source_id, question, answer) "
                    "VALUES (%s, 'src-e2e-long', %s, %s)", (QA_LONG_ID, LONG_Q, LONG_A))
        # an unrelated topic row — must never be touched
        cur.execute("INSERT INTO topics (id, topic_id, title, body) "
                    "VALUES (%s, 't_unrelated', 'x', 'y')", (TOPIC_UNRELATED_ID,))

    def snap():
        out = {}
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, content, embedding IS NOT NULL AS hv "
                        "FROM conversation_stream ORDER BY id")
            for r in cur.fetchall():
                # key by the readable label so assertions stay legible, while the row id
                # stays the real BIGINT production uses
                key = REV.get(r["id"], r["id"])
                out[key] = (sha(r["content"]), r["hv"])
        return out

    before = snap()
    counts_before = _table_counts(conn)

    # ─── scenario harness ────────────────────────────────────────────────────
    def run_embed(entity_id, behaviours, cfg=CFG, policy=DURABLE_WRITE_EMBED_POLICY):
        """Call embed_for_write with a scripted transport."""
        seq = {"i": 0}

        def fake_post(url, json=None, headers=None, proxies=None, timeout=None):
            b = behaviours[min(seq["i"], len(behaviours) - 1)]
            seq["i"] += 1
            if isinstance(b, BaseException):
                raise b
            return b

        orig = requests.post
        requests.post = fake_post
        try:
            # The marker id must equal `t.id::text` or the unexplained-NULL join breaks,
            # so translate the readable label into the row id production would actually use.
            return EF.embed_for_write(
                f"body of {entity_id}", cfg,
                entity_table="conversation_stream",
                entity_id=str(SID.get(entity_id, entity_id)),
                phase="live_ingest", conn=conn, policy=policy,
            )
        finally:
            requests.post = orig

    def write_vec(entity_id, vec, model="BAAI/bge-m3"):
        with conn.cursor() as cur:
            cur.execute("UPDATE conversation_stream SET embedding=%s::vector WHERE id=%s",
                        ("[" + ",".join(str(x) for x in vec) + "]",
                         SID.get(entity_id, entity_id)))

    def markers():
        """Keyed by the readable label so assertions read naturally."""
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM embedding_failures")
            out = {}
            for r in cur.fetchall():
                eid = r["entity_id"]
                key = REV.get(int(eid), eid) if str(eid).isdigit() else eid
                out[key] = dict(r)
            return out

    print("\n[1] normal success")
    o = run_embed("live/s/1", [_Resp()])
    check("1.1 status ok", o.status.value == "ok", o.status.value)
    check("1.2 vector returned", bool(o.vector))
    check("1.3 no marker written", "live/s/1" not in markers())
    write_vec("live/s/1", o.vector)

    print("\n[2] timeout -> retryable failure (retries exhausted)")
    o = run_embed("live/s/2", [requests.Timeout("read timeout=10.0")])
    check("2.1 status degraded", o.status.value == "degraded", o.status.value)
    check("2.2 marked retryable", o.retryable is True)
    check("2.3 error class TIMEOUT", o.error_class == EmbedErrorClass.TIMEOUT.value, o.error_class)
    check("2.4 attempts == retries+1", o.attempts == DURABLE_WRITE_EMBED_POLICY.retries + 1,
          str(o.attempts))
    m = markers().get("live/s/2")
    check("2.5 durable marker exists", m is not None)
    check("2.6 marker retryable", bool(m and m["retryable"]))
    check("2.7 marker policy is durable_write", bool(m and m["timeout_policy"] == "durable_write"),
          m["timeout_policy"] if m else "")
    check("2.8 no credential / raw text in marker",
          bool(m) and "e2e-key" not in json.dumps(m, default=str)
          and "body of" not in json.dumps(m, default=str))

    print("\n[3] config failure -> non-retryable, still explainable")
    bad_cfg = {"endpoint": CFG["endpoint"], "model": "", "_fingerprint": "fp-e2e"}
    o = run_embed("live/s/3", [_Resp()], cfg=bad_cfg)
    check("3.1 status failed", o.status.value == "failed", o.status.value)
    check("3.2 class CONFIG_INVALID", o.error_class == EmbedErrorClass.CONFIG.value, o.error_class)
    check("3.3 NOT retryable", o.retryable is False, "a config error must not look transient")
    m = markers().get("live/s/3")
    check("3.4 durable marker exists", m is not None)
    check("3.5 marker not retryable", bool(m and not m["retryable"]))

    print("\n[4] empty source -> no request, no marker")
    o = EF.embed_for_write("", CFG, entity_table="conversation_stream",
                           entity_id="live/s/4", phase="live_ingest", conn=conn,
                           policy=DURABLE_WRITE_EMBED_POLICY)
    check("4.1 class NO_INPUT", o.error_class == "NO_INPUT", str(o.error_class))
    check("4.2 no marker (no request happened)", "live/s/4" not in markers())

    print("\n[5] retry then success")
    o = run_embed("live/s/6", [requests.Timeout("t"), _Resp()])
    check("5.1 status ok on 2nd attempt", o.status.value == "ok", o.status.value)
    check("5.2 attempts == 2", o.attempts == 2, str(o.attempts))
    write_vec("live/s/6", o.vector)

    # ─── repair pass (UPDATE-only, resumable, no DELETE) ─────────────────────
    print("\n[6] repair pass over retryable NULLs")
    repaired = 0
    for eid, m in list(markers().items()):
        if not m["retryable"]:
            continue
        o = run_embed(eid, [_Resp()])
        if o.ok:
            write_vec(eid, o.vector)
            EF.resolve_embedding_failure(conn, entity_table="conversation_stream",
                                         entity_id=eid, phase="live_ingest",
                                         resolution="repaired")
            repaired += 1
    check("6.1 repaired exactly the retryable rows", repaired == 1, f"repaired={repaired}")
    m = markers().get("live/s/2")
    check("6.2 resolved_at set after repair", bool(m and m["resolved_at"]), str(m and m["resolved_at"]))
    check("6.3 non-retryable marker left alone",
          bool(markers().get("live/s/3", {}).get("resolved_at") is None))

    # ─── long-QA chunking (the pipeline the real backfill must reuse) ────────
    print("\n[7] long-QA chunked embedding + sidecar provenance")
    rep = build_qa_embedding_representation(LONG_Q, LONG_A, CFG,
                                           short_text_for_emb=f"{LONG_Q}\n{LONG_A}")
    check("7.1 representation classified long", bool(rep.is_long), f"is_long={rep.is_long}")
    check("7.2 has >1 chunk", len(rep.chunks) > 1, f"chunks={len(rep.chunks)}")
    chunk_texts = [c.embed_text for c in rep.chunks]
    orig = requests.post

    def _chunk_post(url, json=None, headers=None, proxies=None, timeout=None):
        # Faithful stub: one vector per input actually sent, each carrying its `index`.
        # embed_batch builds its result map BY INDEX, so omitting `index` collapses every
        # item onto 0 and looks like a partial response.
        inputs = json.get("input") if isinstance(json, dict) else None
        n = len(inputs) if isinstance(inputs, list) else 1
        return _Resp(payload={
            "data": [{"index": i, "embedding": VEC} for i in range(max(1, n))]
        })

    requests.post = _chunk_post
    try:
        child_vecs = embed_batch(chunk_texts, CFG, collision_safe_key=True)
    finally:
        requests.post = orig
    check("7.3 one vector per chunk", len(child_vecs) == len(rep.chunks),
          f"{len(child_vecs)} vs {len(rep.chunks)}")
    if not rep.chunks:
        check("7.4 fixture actually exercised the long path", False,
              "no chunks — the fixture landed on the short path, so this section proved nothing")
        parent = []
    else:
        parent = aggregate_parent_embedding(child_vecs)
        check("7.4 parent vector dimension preserved", len(parent) == len(VEC), str(len(parent)))
    if not rep.chunks:
        return _finish()
    with conn.cursor() as cur:
        cur.execute("UPDATE qa_pairs SET embedding=%s::vector, embed_model='BAAI/bge-m3' "
                    "WHERE id=%s",
                    ("[" + ",".join(str(x) for x in parent) + "]", QA_LONG_ID))
        for c, v in zip(rep.chunks, child_vecs):
            # Real production column set: qa_id + the six provenance columns. `content`
            # holds chunk.text (what the live path stores), not chunk.embed_text.
            cur.execute(
                "INSERT INTO qa_embedding_chunks "
                "(qa_id, chunk_index, source_field, source_start, source_end, source_sha256, "
                " token_count, representation_version, content, embedding, embed_model) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,'BAAI/bge-m3') "
                "ON CONFLICT (qa_id, chunk_index) DO UPDATE SET "
                "  source_field=EXCLUDED.source_field, source_start=EXCLUDED.source_start, "
                "  source_end=EXCLUDED.source_end, source_sha256=EXCLUDED.source_sha256, "
                "  token_count=EXCLUDED.token_count, "
                "  representation_version=EXCLUDED.representation_version, "
                "  content=EXCLUDED.content, embedding=EXCLUDED.embedding",
                (QA_LONG_ID, c.chunk_index, c.source_field, c.source_start, c.source_end,
                 c.source_sha256, c.token_count, c.representation_version, c.text,
                 "[" + ",".join(str(x) for x in v) + "]"))
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM qa_embedding_chunks WHERE qa_id=%s", (QA_LONG_ID,))
        n = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM qa_embedding_chunks WHERE qa_id=%s "
                    "AND embedding IS NOT NULL AND content <> ''", (QA_LONG_ID,))
        n_ok = cur.fetchone()[0]
    check("7.5 sidecar chunks written", n == len(rep.chunks), f"{n} vs {len(rep.chunks)}")
    check("7.6 every chunk has a vector + text (valid provenance)", n_ok == n, f"{n_ok}/{n}")

    # ─── invariants ──────────────────────────────────────────────────────────
    print("\n[8] invariants")
    after = snap()
    check("8.1 every source row still present", set(after) == set(before), f"{sorted(after)}")
    check("8.2 every source text hash unchanged",
          all(after[k][0] == before[k][0] for k in before))
    check("8.3 already-correct row untouched",
          after["live/s/5"] == before["live/s/5"], f"{before['live/s/5']} -> {after['live/s/5']}")
    counts_after = _table_counts(conn)
    check("8.4 no table lost rows (nothing DELETEd)",
          all(counts_after[t] >= counts_before[t] for t in counts_before),
          f"{counts_before} -> {counts_after}")
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM topics WHERE id=%s AND embedding IS NULL", (TOPIC_UNRELATED_ID,))
        check("8.5 unrelated topic row untouched", cur.fetchone()[0] == 1)

    # ─── the primary success metric, tested in BOTH directions ──────────────
    print("\n[9] primary metric: unexplained NULLs")
    # A metric that can only ever read 0 is worthless. Inject a genuine historical hole —
    # source text present, no vector, no marker, exactly what the old silent-swallow code
    # left behind — and require the metric to FIND it.
    with conn.cursor() as cur:
        cur.execute("INSERT INTO conversation_stream "
                    "(id, session_id, role, content, \"timestamp\") "
                    "VALUES (%s, 'sess-e2e', 'user', %s, NOW())",
                    (SID["live/s/7"],
                     "a historical hole: content present, vector lost, no marker"))
    unexplained = EF.count_unexplained_nulls(
        conn, table="conversation_stream", pk="id", text_col="content")
    check("9.1 metric DETECTS a genuine unexplained NULL", unexplained == 1, str(unexplained))

    # The empty-source row must NOT be counted: it never was supposed to have a vector.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM conversation_stream "
                    "WHERE embedding IS NULL AND COALESCE(btrim(content),'')=''")
        empty_rows = cur.fetchone()[0]
    check("9.2 empty-source NULL is EXCLUDED from the metric (it never needed a vector)",
          empty_rows == 1,
          f"empty-source NULLs={empty_rows} — must be 1 and must not appear in 9.1")

    # Repair the injected hole, then the metric must go to zero.
    o = run_embed("live/s/7", [_Resp()])
    if o.ok:
        write_vec("live/s/7", o.vector)
    unexplained = EF.count_unexplained_nulls(
        conn, table="conversation_stream", pk="id", text_col="content")
    check("9.3 metric returns to 0 after repair", unexplained == 0, str(unexplained))

    # Every remaining NULL must be explained by a NON-retryable marker (the config error).
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.id, f.error_class, f.retryable
              FROM conversation_stream t
              LEFT JOIN embedding_failures f
                ON f.entity_table='conversation_stream' AND f.entity_id=t.id::text
             WHERE t.embedding IS NULL AND COALESCE(btrim(t.content),'') <> ''
        """)
        leftover = cur.fetchall()
    check("9.4 the only remaining NULLs are explained by a non-retryable marker",
          len(leftover) == 1 and leftover[0][2] is False,
          f"leftover={leftover}")

    # ─── summary ────────────────────────────────────────────────────────────
    return _finish()


def _finish() -> int:
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 72)
    print(f"E2E RESULT: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        for n, _, d in failed:
            print(f"  FAILED: {n} {d}")
        print("VERDICT: EMBEDFIX_E2E_FAILED")
        return 1
    print("VERDICT: EMBEDFIX_E2E_PASSED")
    return 0


def _table_counts(conn):
    out = {}
    with conn.cursor() as cur:
        for t in ("conversation_stream", "topics", "qa_pairs", "yin_paragraphs",
                  "observation_notes", "embedding_failures"):
            cur.execute(f"SELECT COUNT(*) FROM public.{t}")
            out[t] = cur.fetchone()[0]
    return out


if __name__ == "__main__":
    raise SystemExit(main())
