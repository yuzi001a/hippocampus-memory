"""Independent DB ground-truth readback for the P0-C1 E2E run.

Reads ONLY the disposable database and prints a structured JSON summary:
per-session qa_pairs rows (question / answer / merged_event_ids) and
conversation_stream source row counts.  Also lists the databases visible on the
disposable cluster so the evidence records exactly what was touched.
"""
import json
import os
import sys

import psycopg2

HOST = "127.0.0.1"
PORT = 55432
USER = "f2e2e"
DB = "p0c1e2e_20261004"
PW = os.environ["PGPASSWORD"]


def mev(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    if isinstance(v, str):
        try:
            p = json.loads(v)
        except Exception:
            return []
        return [str(x) for x in p] if isinstance(p, list) else []
    return []


def main():
    c = psycopg2.connect(host=HOST, port=PORT, user=USER, dbname=DB, password=PW)
    out = {"database": DB, "host": HOST, "port": PORT, "user": USER,
           "password_in_report": False}
    cur = c.cursor()
    cur.execute("select datname from pg_database order by datname")
    out["databases_visible"] = [r[0] for r in cur.fetchall()]

    cur.execute("select count(*) from qa_pairs")
    out["qa_pairs_total"] = int(cur.fetchone()[0])
    cur.execute("select count(*) from conversation_stream")
    out["conversation_stream_total"] = int(cur.fetchone()[0])

    cur.execute(
        "select session_id, id, question, answer, merged_event_ids "
        "from qa_pairs order by session_id, id")
    qa = []
    for sid, rid, q, a, m in cur.fetchall():
        qa.append({"session_id": sid, "id": rid, "question": q,
                   "answer": a, "merged_event_ids": mev(m)})
    out["qa_pairs"] = qa

    cur.execute(
        "select session_id, role, event_id, count(*) "
        "from conversation_stream group by session_id, role, event_id "
        "order by session_id, event_id")
    out["conversation_stream_counts"] = [
        {"session_id": r[0], "role": r[1], "event_id": r[2], "count": int(r[3])}
        for r in cur.fetchall()]

    cur.execute(
        "select session_id, role, event_id, left(content, 60) "
        "from conversation_stream where event_id in ('oob','async','ctx','sys','failed') "
        "order by session_id, event_id")
    out["excluded_event_source_rows"] = [
        {"session_id": r[0], "role": r[1], "event_id": r[2], "content_head": r[3]}
        for r in cur.fetchall()]

    c.close()
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)


if __name__ == "__main__":
    sys.exit(main())
