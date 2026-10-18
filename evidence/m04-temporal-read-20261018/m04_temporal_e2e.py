"""M04 — time-point read (``as_of``) acceptance against isolated real PostgreSQL.

Design notes
------------
* The **edges are produced by the real writer** (``ActiveMemoryWriter.correct``),
  so the constraint set, the ids and the ``effective_at`` column all come from
  production code. Only the *clock* is controlled: after each correction the
  test rewrites ``recorded_at`` (and the head's ``created_at``) to fixed
  instants. Adjusting fixture time is not the same as faking a correction.
* Every scenario runs in a freshly created database. Port 5433 (production) and
  any non-loopback host are refused before a single statement is sent.
* Nothing here writes to, or reads from, the production database.

Usage: m04_temporal_e2e.py <python-src-dir>
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone

SECRET = os.environ.get("M04_PGPW_FILE", "")
PG_HOST = os.environ.get("M04_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("M04_PG_PORT", "55433"))
PG_USER = os.environ.get("M04_PG_USER", "ranger")
DB_NAME = os.environ.get("M04_PG_DB", "m04_temporal_e2e")

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=10)
T2 = T0 + timedelta(days=20)
T3 = T0 + timedelta(days=30)

CHECKS: list[dict] = []
NOTES: list[str] = []


def check(name: str, ok: bool, detail=None) -> None:
    entry = {"check": name, "ok": bool(ok)}
    if detail is not None:
        entry["detail"] = detail if isinstance(detail, (str, int, float, bool)) else str(detail)
    CHECKS.append(entry)


def note(text: str) -> None:
    NOTES.append(text)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _guard() -> str:
    if str(PG_PORT) == "5433":
        raise SystemExit("REFUSED: production port 5433")
    if PG_HOST not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit(f"REFUSED: non-loopback host {PG_HOST!r}")
    if not os.path.isfile(SECRET):
        raise SystemExit("MISSING_SECRET_FILE")
    return open(SECRET, encoding="utf-8").read().strip()


def _connect(dbname: str, password: str):
    import psycopg2

    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=dbname, user=PG_USER,
        password=password, connect_timeout=10,
    )


def _apply_schema(conn, src_dir: str) -> None:
    """Apply the full add-only schema, in dependency order.

    Only the two M04-relevant tables would be enough for the read path, but a
    correction also touches the derived layer (M03 propagation). Leaving those
    tables out would make ``correct()`` fail for an unrelated reason and turn
    this suite into a schema test instead of a temporal-read test.
    """
    schema_dir = os.path.join(src_dir, "v3core", "schema")
    order = [
        "alpha_bootstrap.sql",          # qa_pairs, conversation_stream, topics, ...
        "explicit_memories.sql",
        "assistant_deliveries.sql",
        "derived_memory_invalidations.sql",
        "embedding_failures.sql",
        "qa_embedding_chunks.sql",
        "observation_embedding_chunks.sql",
        "memory_relations.sql",         # references explicit_memories -> last
    ]
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for name in order:
        path = os.path.join(schema_dir, name)
        if not os.path.isfile(path):
            check(f"schema_file_present_{name}", False, path)
            continue
        with open(path, encoding="utf-8") as fh:
            sql = fh.read()
        with conn.cursor() as cur:
            cur.execute(sql)
    conn.commit()


def _seed_memory(cur, memory_id: str, content: str, created: datetime) -> None:
    cur.execute(
        """
        INSERT INTO public.explicit_memories
            (memory_id, category, title, content, tags, provenance, status,
             created_at, updated_at)
        VALUES (%s, 'ops', %s, %s, ARRAY['m04']::text[], '{"origin":"m04-e2e"}'::jsonb,
                'active', %s, %s)
        """,
        (memory_id, memory_id, content, created, created),
    )


def _shift(cur, memory_id: str, recorded: datetime) -> None:
    """Rewrite the clock of the edge that leaves ``memory_id``."""
    cur.execute(
        "UPDATE public.memory_relations SET recorded_at = %s WHERE from_memory_id = %s",
        (recorded, memory_id),
    )


def _set_created(cur, memory_id: str, created: datetime) -> None:
    cur.execute(
        "UPDATE public.explicit_memories SET created_at = %s, updated_at = %s "
        "WHERE memory_id = %s",
        (created, created, memory_id),
    )


def _row_status(conn, memory_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM public.explicit_memories WHERE memory_id = %s",
            (memory_id,),
        )
        row = cur.fetchone()
    return row[0] if row else "<missing>"


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("USAGE: m04_temporal_e2e.py <python-src-dir>")
    src_dir = sys.argv[1]
    sys.path.insert(0, src_dir)

    password = _guard()
    from v3core.active_memory_store import ActiveMemoryReader, ActiveMemoryWriter
    from v3core.pg_pool import PgPool

    # ── fresh database ────────────────────────────────────────────────────
    admin = _connect("postgres", password)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (DB_NAME,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{DB_NAME}"')
    admin.close()

    conn = _connect(DB_NAME, password)
    _apply_schema(conn, src_dir)
    conn.close()
    check("database_created_and_schema_applied", True, DB_NAME)

    def _connect_pool():
        return _connect(DB_NAME, password)

    pool = PgPool(connect=_connect_pool, max_connections=6, min_connections=0,
                  connect_timeout=10)
    writer = ActiveMemoryWriter(pool=pool, config={})
    reader = ActiveMemoryReader(pool=pool)

    # ── chain 1: a -> b -> c produced by the real writer ──────────────────
    setup = _connect(DB_NAME, password)
    with setup.cursor() as cur:
        _seed_memory(cur, "m04_a", "a content", T0)
    setup.commit()

    rb = writer.correct("m04_a", "b content", authority="user_explicit",
                        effective_at=_iso(T1 + timedelta(days=5)))
    check("writer_correction_a_to_b", rb.get("success") is True
          and rb.get("new_status") == "active",
          {"code": rb.get("error_code"), "error": rb.get("error")})
    note(f"correct(a->b) receipt error: {rb.get('error')!r}")
    rc = writer.correct(rb.get("new_memory_id"), "c content",
                        authority="user_explicit",
                        effective_at=_iso(T2 + timedelta(days=5)))
    check("writer_correction_b_to_c", rc.get("success") is True,
          rc.get("error_code"))
    b_id, c_id = rb.get("new_memory_id"), rc.get("new_memory_id")

    with setup.cursor() as cur:
        _set_created(cur, "m04_a", T0)
        _shift(cur, "m04_a", T1)
        _shift(cur, b_id, T2)
    setup.commit()

    # ── recorded basis: three windows ─────────────────────────────────────
    out = reader.read_version("m04_a", as_of=_iso(T0 - timedelta(days=1)))
    check("floor_before_chain_is_absent",
          out.get("current_status") == "absent"
          and out.get("query_status") == "NO_VERSION_AT_AS_OF"
          and out.get("memory") is None, out.get("query_status"))

    # Windows: a->b is recorded at T1, b->c at T2.
    #   (T0, T1) -> a      [T1, T2) -> b      [T2, ...) -> c
    w1 = reader.read_version("m04_a", as_of=_iso(T1 - timedelta(days=1)))
    check("window_1_before_first_correction_resolves_a",
          w1.get("resolved_memory_id") == "m04_a" and w1.get("current_status") == "active",
          w1.get("resolved_memory_id"))

    w2 = reader.read_version("m04_a", as_of=_iso(T1 + timedelta(days=1)))
    check("window_2_between_corrections_resolves_b", w2.get("resolved_memory_id") == b_id,
          w2.get("resolved_memory_id"))
    check("window_2_payload_is_b", (w2.get("memory") or {}).get("content") == "b content")

    w3 = reader.read_version("m04_a", as_of=_iso(T3))
    check("window_3_after_second_correction_resolves_c",
          w3.get("resolved_memory_id") == c_id, w3.get("resolved_memory_id"))

    # Revision 4: the answer must come from the timeline, not from today's
    # status column. b is archived TODAY and must still be 'active' at T1+1d.
    check("today_status_is_archived_for_b", _row_status(setup, b_id) == "archived",
          _row_status(setup, b_id))
    check("as_of_status_ignores_todays_column",
          w2.get("current_status") == "active")

    check("basis_defaults_to_recorded_and_is_echoed",
          w2.get("basis") == "recorded" and w2.get("as_of") == _iso(T1 + timedelta(days=1)),
          w2.get("basis"))
    check("as_of_not_flagged_future", w2.get("as_of_future") is False)

    # ── effective basis: declared time wins over record time ──────────────
    probe = T1 + timedelta(days=1)  # after record, before declaration
    eff = reader.read_version("m04_a", as_of=_iso(probe), basis="effective")
    check("effective_basis_uses_declared_time",
          eff.get("resolved_memory_id") == "m04_a"
          and w2.get("resolved_memory_id") != "m04_a",
          {"effective": eff.get("resolved_memory_id"),
           "recorded": reader.read_version(
               "m04_a", as_of=_iso(probe)).get("resolved_memory_id")})

    # ── effective basis with a missing declaration ────────────────────────
    with setup.cursor() as cur:
        _seed_memory(cur, "m04_n1", "n1 content", T0)
    setup.commit()
    rn = writer.correct("m04_n1", "n2 content", authority="user_explicit")  # no effective_at
    n2_id = rn.get("new_memory_id")
    with setup.cursor() as cur:
        _shift(cur, "m04_n1", T1)
    setup.commit()

    ind = reader.read_version("m04_n1", as_of=_iso(T2), basis="effective")
    check("effective_missing_reports_indeterminate",
          ind.get("success") is False
          and ind.get("error_code") == "EFFECTIVE_TIME_INDETERMINATE"
          and ind.get("query_status") == "EFFECTIVE_TIME_INDETERMINATE"
          and ind.get("memory") is None,
          ind.get("error_code"))
    check("indeterminate_exposes_the_missing_edge",
          (ind.get("indeterminate_edge") or {}).get("from_memory_id") == "m04_n1",
          ind.get("indeterminate_edge"))
    rec = reader.read_version("m04_n1", as_of=_iso(T2))
    check("same_chain_is_answerable_under_recorded_basis",
          rec.get("success") is True and rec.get("resolved_memory_id") == n2_id,
          rec.get("resolved_memory_id"))

    # ── withdrawal before / after ────────────────────────────────────────
    with setup.cursor() as cur:
        _seed_memory(cur, "m04_w1", "w1 content", T0)
    setup.commit()
    rw = writer.correct("m04_w1", "w2 content", authority="user_explicit")
    w2_id = rw.get("new_memory_id")
    rwd = writer.correct(w2_id, None, mode="withdraw", authority="user_explicit")
    check("writer_withdraw_succeeded", rwd.get("success") is True, rwd.get("error_code"))
    with setup.cursor() as cur:
        _shift(cur, "m04_w1", T1)
        _shift(cur, w2_id, T2)
    setup.commit()

    before = reader.read_version("m04_w1", as_of=_iso(T1 + timedelta(days=1)))
    after = reader.read_version("m04_w1", as_of=_iso(T3))
    check("withdrawal_before_returns_old_version",
          before.get("resolved_memory_id") == w2_id
          and before.get("current_status") == "active", before.get("resolved_memory_id"))
    check("withdrawal_after_returns_nothing",
          after.get("success") is True
          and after.get("memory") is None
          and after.get("current_status") == "withdrawn",
          after.get("current_status"))

    # ── parameter contract against real PG (must not touch the database) ──
    bad = [
        ("as_of_plus_history_mode", {"as_of": _iso(T3), "mode": "history"}),
        ("as_of_plus_include_history", {"as_of": _iso(T3), "include_history": True}),
        ("basis_without_as_of", {"basis": "effective"}),
        ("as_of_naive", {"as_of": "2026-03-01T00:00:00"}),
        ("as_of_garbage", {"as_of": "yesterday"}),
        ("as_of_empty", {"as_of": ""}),
        ("basis_unknown", {"as_of": _iso(T3), "basis": "transaction_time"}),
    ]
    for name, kwargs in bad:
        try:
            out = reader.read_version("m04_a", **kwargs)
        except Exception as exc:  # noqa: BLE001
            check(name, False, repr(exc))
            continue
        check(name, out.get("success") is False
              and out.get("error_code") == "INVALID_REQUEST", out.get("error_code"))

    # ── future as_of ─────────────────────────────────────────────────────
    fut = reader.read_version("m04_a", as_of=_iso(datetime.now(timezone.utc) + timedelta(days=365)))
    check("future_as_of_returns_present_and_flags_itself",
          fut.get("resolved_memory_id") == c_id and fut.get("as_of_future") is True,
          {"resolved": fut.get("resolved_memory_id"), "future": fut.get("as_of_future")})

    # ── as_of=None zero regression ───────────────────────────────────────
    plain = reader.read_version("m04_a")
    expected_keys = {
        "success", "status", "error_code", "error", "mode", "memory_id",
        "requested", "memory", "resolved_memory_id", "is_current",
        "current_status", "relation",
    }
    check("as_of_none_keeps_exact_shape", set(plain) == expected_keys,
          sorted(set(plain) ^ expected_keys))
    check("as_of_none_still_resolves_current",
          plain.get("resolved_memory_id") == c_id
          and (plain.get("memory") or {}).get("content") == "c content")
    hist = reader.read_version("m04_a", mode="history")
    check("history_mode_unchanged",
          hist.get("success") is True
          and (hist.get("memory") or {}).get("content") == "a content"
          and "history" in hist)
    check("history_labels_unchanged",
          [v["label"] for v in hist["history"]["versions"]]
          == ["superseded", "superseded", "current"],
          [v["label"] for v in hist["history"]["versions"]])

    # M04-3: transition belongs to the edge, and is traceable to it.
    transitions = [v.get("transition") for v in hist["history"]["versions"]]
    check("m04_3_head_has_outgoing_transition",
          (transitions[0] or {}).get("relation_type") == "supersedes"
          and (transitions[0] or {}).get("effective_at") is not None,
          transitions[0])
    check("m04_3_terminal_has_no_transition", transitions[2] is None, transitions[2])

    # ── cycle must still fail closed with a time filter ──────────────────
    with setup.cursor() as cur:
        _seed_memory(cur, "m04_c1", "c1", T0)
        _seed_memory(cur, "m04_c2", "c2", T0)
        cur.execute(
            """
            INSERT INTO public.memory_relations
                (relation_id, relation_type, from_memory_id, to_memory_id,
                 correction_id, authority, provenance, recorded_at, request)
            VALUES ('cor_cycle_1', 'supersedes', 'm04_c1', 'm04_c2',
                    'cor_cycle_1', 'user_explicit', 'explicit_tool_request', %s, '{}'::jsonb),
                   ('cor_cycle_2', 'supersedes', 'm04_c2', 'm04_c1',
                    'cor_cycle_2', 'user_explicit', 'explicit_tool_request', %s, '{}'::jsonb)
            """,
            (T1, T2),
        )
    setup.commit()
    cyc = reader.read_version("m04_c1", as_of=_iso(T3))
    check("cycle_fails_closed_under_as_of",
          cyc.get("success") is False and cyc.get("error_code") == "DURABLE_FAILED",
          cyc.get("error_code"))

    # ── unknown id ───────────────────────────────────────────────────────
    miss = reader.read_version("m04_not_there", as_of=_iso(T3))
    check("unknown_id_is_not_found",
          miss.get("success") is False and miss.get("error_code") == "NOT_FOUND",
          miss.get("error_code"))

    # ── concurrency: every read must be internally consistent ────────────
    with setup.cursor() as cur:
        _seed_memory(cur, "m04_x1", "x1 content", T0)
    setup.commit()
    stop = threading.Event()
    errors: list[str] = []

    def _churn() -> None:
        target = "m04_x1"
        try:
            while not stop.is_set():
                res = writer.correct(target, f"x{datetime.now().timestamp()}",
                                     authority="user_explicit")
                if not res.get("success"):
                    errors.append(str(res.get("error_code")))
                    return
                target = res.get("new_memory_id")
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    worker = threading.Thread(target=_churn, daemon=True)
    worker.start()
    torn = 0
    observed = 0
    try:
        for _ in range(25):
            snap = reader.read_version("m04_x1", as_of=_iso(datetime.now(timezone.utc)))
            observed += 1
            if not snap.get("success"):
                torn += 1
                continue
            versions = snap["timeline"]["versions"]
            ids = [v["memory_id"] for v in versions]
            current = snap.get("resolved_memory_id")
            if current is not None and current not in ids:
                torn += 1
    finally:
        stop.set()
        worker.join(timeout=10)
    check("concurrent_reads_are_self_consistent", torn == 0,
          {"reads": observed, "torn": torn, "writer_errors": errors[:3]})
    note("Concurrency is a stress probe: it can only *observe* torn reads, not "
         "prove their absence. The structural guarantee is that one as_of read "
         "uses exactly ONE READ ONLY REPEATABLE READ snapshot (asserted in the "
         "unit suite), so a writer committing mid-read cannot mix two states.")

    setup.close()
    try:
        pool.close()
    except Exception:  # noqa: BLE001
        pass

    failures = [c for c in CHECKS if not c["ok"]]
    report = {
        "suite": "M04 temporal read",
        "db": DB_NAME,
        "port": PG_PORT,
        "checks_total": len(CHECKS),
        "checks_failed": len(failures),
        "verdict": "PASS" if not failures else "FAIL",
        "failures": failures,
        "checks": CHECKS,
        "notes": NOTES,
        "scenarios": {
            "chain": {"a": "m04_a", "b": b_id, "c": c_id},
            "timeline": {"T0": _iso(T0), "T1": _iso(T1), "T2": _iso(T2), "T3": _iso(T3)},
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(2)
