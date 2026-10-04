#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-C1 Hermes host-event semantics — Y400 real-PostgreSQL E2E driver.

Task book §13 acceptance on a **disposable** PostgreSQL (isolated DB / port).
This driver exercises the REAL ``V3Core`` product paths introduced by P0-C1:

  * real ``V3Core.sync_turn`` — the classification-first loop, per-kind
    actions (CONTINUATION / CONTROL / TERMINAL transparency, ASYNC / NEW_ROOT
    boundary cut) and structural continuation (§4 of the design contract);
  * real ``LiveBuffer`` (durable outbox marker + real
    ``PgEmbedStore.insert_message`` -> ``conversation_stream`` source rows);
  * real ``core._flush_all_pending_qa()`` + ``core._drain_flush_queue()``
    (the production flush chain -> ``qa_pairs``);
  * real ``V3Core`` re-instantiation over the same DB (restart scenario);
  * real ``V3Config`` resolved from an ISOLATED profile directory with an
    EXPLICIT pg config dict (never production).

Scenarios (task book §13 — 10 scenarios)
---------------------------------------
  S1  normal QA            U1/A1 -> exactly 1 QA; source preserved.
  S2  multi assistant      U1/A1/A2/A3 -> 1 QA, each segment once; replay no-op.
  S3  compaction continuation  U1/A1/CTX/A2/A3 -> 1 QA, CTX transparent.
  S4  compaction after flush (BLOCKER)
                           flush U1/A1, then full snapshot U1/A1/CTX/A2 ->
                           still ONE QA; A2 durably appended via the snapshot
                           anchor; no second QA.
  S5  OOB user             U1/A1/OOB('改成 B')/A2 -> raw wrapper kept in
                           conversation_stream; derived Q = payload; A2 pairs
                           to the steer; A2 NOT folded into the pre-steer QA.
  S6  control transparency U1/A1/[System: …]/A2 -> 1 QA, control body excluded.
  S7  async contamination  U1/A1/ASYNC_BATCH_COMPLETE/A_async -> the previous
                           human QA is NOT polluted by the autonomous output.
  S8  failed_turn exclusion U1/A1/failed_turn -> failed notice never an answer.
  S9  replay               same snapshot x3 -> source / QA / merged_event_ids
                           no duplicate.
  S10 restart              new V3Core over the same DB, re-sync full snapshot ->
                           state consistent, no duplicate.

Baseline: NO LLM / NO EMBEDDING — the profile configures neither, so the whole
run must pass without any external API call (``safe_embed_cfg`` -> None,
``embed_for_write`` short-circuits with ``NO_INPUT``).

Isolation / safety (hard requirement)
-------------------------------------
1. ``--pg-host/--pg-port/--pg-db/--pg-user/--pg-password`` (or env
   ``P0C1_PG_*``; password also accepts ``PGPASSWORD``).
   Missing parameter -> explicit error + exit 2, NO default fallback.
   ``dbname`` contains ``v3embeddings`` -> REFUSE, exit 2.
   ``port == 5433`` -> REFUSE, exit 2.
2. A ``psycopg2.connect`` guard is installed for the whole process: any
   connection to the production port / production database name raises.
3. The run root (isolated HOME + profile + data dir + durable markers) is a
   fresh directory next to this script; nothing outside it is written.

Y400 usage (PowerShell)
-----------------------
    $py='C:/hp-testbed/m03-derived-propagation-20261003/venv/Scripts/python.exe'
    & $py evidence\\p0c1-hermes-event-semantics-20261004\\p0c1_e2e.py `
        --pg-host 127.0.0.1 --pg-port 55432 `
        --pg-db p0c1e2e_20261004 --pg-user f2e2e `
        --pg-password <DISPOSABLE_DB_PASSWORD> `
        --bootstrap --keep

Preconditions
-------------
The disposable database must be bootstrapped with the schema carrying
``conversation_stream.host_turn_id`` and ``qa_pairs.merged_event_ids``.
``--bootstrap`` runs ``python -m v3core.distribution_cli bootstrap --target``
through the same interpreter; without it the driver only CHECKS the schema and
fails loudly (``PRE.*``) if it is missing.

Exit code 0 = verdict PASS, 1 = verdict FAIL, 2 = refused before any run.
This driver never prints or writes the PG password.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ── paths ───────────────────────────────────────────────────────────────────
REPO = Path(__file__).resolve().parents[2]          # C:/hp-testbed/p0c1-...
SRC = REPO / "src" / "v3-core" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DRIVER_DIR = Path(__file__).resolve().parent

# ── frozen contract constants ───────────────────────────────────────────────
HOST = "p0c1e2e"                      # non-legacy host namespace (P0-C1 gate)
PROFILE = "p0c1e2e"                   # isolated profile name
LEGACY_HOST = "legacy"

#: Targets this driver must NEVER touch (defence in depth on top of the gate).
PRODUCTION_PORTS = (5433,)
PRODUCTION_DB_MARKERS = ("v3embeddings",)
SYSTEM_DBS = ("postgres", "template1", "template0")

#: The only values ``receipt["derivation"][event_id]`` may take (DESIGN §5.1/5.2).
DERIVATION_STATUSES = ("completed_late", "recovered_late", "already_merged",
                       "applied")

#: Env keys that could otherwise re-open a production bypass.
PRODUCTION_BYPASS_ENV = (
    "V3_HARNESS_ALLOW_LIVE_PROD_OUTBOX",
    "V3CORE_ALLOW_LIVE_PROD_OUTBOX",
    "PRODUCTION_BYPASS",
    "ALLOW_PRODUCTION",
    "GUARD_OVERRIDE",
)

# ── real, de-identified Hermes host-event shapes (taxonomy §1.2 / §12) ───────
CTX_COMPACTION = ("[CONTEXT COMPACTION — REFERENCE ONLY]\n"
                  "Earlier turns were summarised to save space; continue the task.")
CTX_TASKLIST = ("[Your active task list was preserved across context compression]\n"
                "1. [ ] finish the pending audit")
CTX_STILL_IN_PROGRESS = "[STILL IN PROGRESS — this is the active request, resume it]"
ASYNC_BATCH_COMPLETE = ("[ASYNC DELEGATION BATCH COMPLETE — deleg_abc123]\n"
                        "Subagent finished the delegated task.")
BG_PROC_COMPLETE = ("[IMPORTANT: Background process proc_abc123 completed]\n"
                    "exit code 0; see output above.")
SYS_MODEL_SWITCH = "[System: The active model for this chat has changed to gpt-x]"
FAILED_TURN_NOTICE = "Your request was not processed. Send it again to retry."
TITLE_GEN = "### Task:\nSuggest 3-5 relevant follow-up questions"
OOB_PAYLOAD = "改成 B"
OOB_WRAPPER = ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, "
               "delivered once at this position]\n"
               f"{OOB_PAYLOAD}\n"
               "[/OUT-OF-BAND USER MESSAGE]")


# ─────────────────────────────────────────────────────────────────────────────
# small utilities
# ─────────────────────────────────────────────────────────────────────────────
def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_err(exc: BaseException, limit: int = 400) -> str:
    return f"{type(exc).__name__}: {exc}"[:limit]


def _git_head() -> str:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=20)
        return (out.stdout or "").strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _mev(value) -> list:
    """Model PG jsonb -> list semantics for ``merged_event_ids``."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    return []


# ─────────────────────────────────────────────────────────────────────────────
# 1. fail-closed target gate (before ANY file or connection is created)
# ─────────────────────────────────────────────────────────────────────────────
def gate_target(host: str, port, db: str, user: str, password: str) -> str:
    missing = []
    if not str(host or "").strip():
        missing.append("pg-host")
    if not str(port or "").strip():
        missing.append("pg-port")
    if not str(db or "").strip():
        missing.append("pg-db")
    if not str(user or "").strip():
        missing.append("pg-user")
    if not str(password or ""):
        missing.append("pg-password")
    if missing:
        return (f"missing required target parameter(s): {', '.join(missing)} "
                f"— no default connection is attempted")
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return f"pg-port {port!r} is not an integer"
    if port_i <= 0 or port_i > 65535:
        return f"pg-port {port_i} is out of range"
    if port_i in PRODUCTION_PORTS:
        return f"refusing production PostgreSQL port {port_i}"
    low_db = str(db).strip().lower()
    for marker in PRODUCTION_DB_MARKERS:
        if marker in low_db:
            return f"refusing production database name (contains {marker!r})"
    if low_db in SYSTEM_DBS:
        return f"refusing system database {db!r}"
    return ""


def install_pg_guard() -> None:
    """Block any psycopg2 connection aimed at a production target."""
    import psycopg2

    original = psycopg2.connect

    def guarded(*args, **kwargs):
        dsn = str(args[0]) if args else ""
        db = str(kwargs.get("dbname") or kwargs.get("database") or "")
        port = kwargs.get("port")
        low_dsn = dsn.lower()
        if port is not None:
            try:
                if int(port) in PRODUCTION_PORTS:
                    raise AssertionError(
                        f"P0-C1 E2E guard: production port {port} refused")
            except (TypeError, ValueError):
                pass
        for marker in PRODUCTION_DB_MARKERS:
            if marker in db.lower() or marker in low_dsn:
                raise AssertionError(
                    f"P0-C1 E2E guard: production database refused ({marker})")
        for prod_port in PRODUCTION_PORTS:
            if f"port={prod_port}" in low_dsn:
                raise AssertionError(
                    f"P0-C1 E2E guard: production port {prod_port} refused (dsn)")
        return original(*args, **kwargs)

    psycopg2.connect = guarded  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
# 2. isolated profile + explicit pg config dict
# ─────────────────────────────────────────────────────────────────────────────
def isolate_environment(run_root: Path, pg: dict) -> dict:
    home = run_root / "home"
    data = run_root / "data"
    profile_dir = home / ".v3-core" / "profiles" / PROFILE
    profile_dir.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)

    dotenv = home / ".v3-core" / ".env"
    dotenv.write_text("", encoding="utf-8")

    config_path = profile_dir / "config.yaml"
    body = (
        "basePath: {base}\n"
        "mode: cloud\n"
        "storage:\n"
        "  pg:\n"
        "    host: {host}\n"
        "    port: {port}\n"
        "    database: {db}\n"
        "    user: {user}\n"
        "e1:\n  enabled: false\n"
        "observer:\n  enabled: false\n"
        "tkg:\n  enabled: false\n"
        "llm:\n  provider: ''\n  model: ''\n  api_key: ''\n  base_url: ''\n"
        "ingest:\n  live_buffer:\n    batch: 8\n    flush_sec: 0.2\n"
    ).format(base=json.dumps(str(data)), host=pg["host"], port=int(pg["port"]),
             db=pg["database"], user=pg["user"])
    config_path.write_text(body, encoding="utf-8")

    os.environ.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": os.environ.get("SystemDrive", "C:"),
        "HOMEPATH": "\\" + str(home).split("\\", 1)[-1] if "\\" in str(home) else "",
        "V3CORE_HOME": str(home),
        "HERMES_HOME": str(home),
        "V3CORE_CONFIG": str(config_path),
        "V3CORE_DOTENV": str(dotenv),
        "V3CORE_PG_PASSWORD": pg["password"],
        "PGPASSWORD": pg["password"],
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    for key in PRODUCTION_BYPASS_ENV:
        os.environ.pop(key, None)

    return {"home": str(home), "data": str(data), "profile_dir": str(profile_dir),
            "config_path": str(config_path), "dotenv": str(dotenv)}


def build_explicit_config(pg: dict, base_path: Path):
    from v3core.config_model import from_legacy_dict

    legacy = {
        "mode": "cloud",
        "basePath": str(base_path),
        "storage": {
            "pg": {
                "host": pg["host"],
                "port": int(pg["port"]),
                "database": pg["database"],
                "user": pg["user"],
                "password": pg["password"],
            },
        },
        "llm": {"provider": "", "model": "", "api_key": "", "base_url": ""},
        "e1": {"enabled": False},
        "tkg": {"enabled": False},
        "ingest": {"live_buffer": {"batch": 8, "flush_sec": 0.2}},
    }
    return from_legacy_dict(legacy)


# ─────────────────────────────────────────────────────────────────────────────
# 3. raw SQL helpers (independent reads — the artifact claim is the DB)
# ─────────────────────────────────────────────────────────────────────────────
class Pg:
    def __init__(self, pg: dict) -> None:
        self._pg = pg

    def connect(self):
        import psycopg2
        return psycopg2.connect(
            host=self._pg["host"], port=int(self._pg["port"]),
            dbname=self._pg["database"], user=self._pg["user"],
            password=self._pg["password"], connect_timeout=10,
        )

    def rows(self, sql: str, params: tuple = ()) -> list:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return []
                return cur.fetchall()
        finally:
            conn.close()

    def one(self, sql: str, params: tuple = ()):
        got = self.rows(sql, params)
        return got[0] if got else None

    def tables(self) -> set:
        return {r[0] for r in self.rows(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'")}

    def columns(self, table: str) -> set:
        return {r[0] for r in self.rows(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s", (table,))}


# ─────────────────────────────────────────────────────────────────────────────
# 4. harness over the REAL V3Core
# ─────────────────────────────────────────────────────────────────────────────
class Harness:
    """Owns one real V3Core + real LiveBuffer over the disposable PG."""

    def __init__(self, config, iso: dict, raw_pg: dict) -> None:
        from v3core import V3Core

        self.iso = iso
        self.pg = raw_pg
        self.config = config
        self.core = V3Core(profile=PROFILE, hermes_home=iso["home"],
                           effective_config=config)
        self.core._config = config          # pin the explicit config
        self.raw = Pg(raw_pg)

        store = self.core.pg
        self.core._pg = store
        self.pg_connected = bool(store and store.is_connected())

        # real LiveBuffer (source ingest); writer thread disabled so the
        # conversation_stream write is deterministic: flush_live drains the
        # queue and calls the REAL LiveBuffer._flush itself.
        self.buffer = self.core.live_buffer
        self.buffer._start_writer = lambda: None

    # -- source ingest --------------------------------------------------------
    def sync(self, session: str, messages: list) -> dict:
        return self.core.sync_turn(session, messages, host=HOST) or {}

    def flush_live(self) -> int:
        buf = self.buffer
        items = []
        while True:
            try:
                items.append(buf._q.get_nowait())
            except queue.Empty:
                break
        if not items:
            return 0
        buf._flush(items=items)
        for _ in items:
            try:
                buf._q.task_done()
            except Exception:
                pass
        return len(items)

    # -- QA derivation flush --------------------------------------------------
    def flush_qa(self, timeout: float = 20.0) -> None:
        self.core._flush_all_pending_qa(timeout=timeout)
        self.core._drain_flush_queue(timeout=timeout)
        end = time.time() + timeout
        while time.time() < end:
            q = getattr(self.core, "_flush_queue", None)
            if q is None:
                break
            if getattr(q, "unfinished_tasks", 0) == 0 and q.empty():
                break
            time.sleep(0.02)

    # -- durable reads --------------------------------------------------------
    def pending_of(self, session: str):
        try:
            ctx = self.core.get_session_context(session, create=False)
        except Exception:
            return None
        pend = ctx.pending_qa if ctx else None
        if not pend:
            return None
        return {
            "q": pend.get("q") or "",
            "a": pend.get("a") or "",
            "q_msg_id": pend.get("q_msg_id") or "",
            "q_event_turn": pend.get("q_event_turn") or "",
            "a_event_ids": list(pend.get("a_event_ids") or []),
        }

    def cs_count(self, session: str, event_id: str) -> int:
        row = self.raw.one(
            "SELECT count(*) FROM public.conversation_stream "
            "WHERE session_id=%s AND host=%s AND event_id=%s",
            (session, HOST, event_id))
        return int(row[0]) if row else 0

    def cs_content(self, session: str, event_id: str):
        row = self.raw.one(
            "SELECT content FROM public.conversation_stream "
            "WHERE session_id=%s AND host=%s AND event_id=%s ORDER BY id DESC LIMIT 1",
            (session, HOST, event_id))
        return str(row[0] or "") if row else None

    def qa_source_id(self, session: str, q_msg_id: str) -> str:
        return f"qa_sync/{HOST}/{session}/{q_msg_id}"

    def qa_row(self, session: str, q_msg_id: str):
        return self.raw.one(
            "SELECT id, answer, merged_event_ids, question FROM public.qa_pairs "
            "WHERE source_id=%s LIMIT 1",
            (self.qa_source_id(session, q_msg_id),))

    def qa_by_question(self, session: str, question: str):
        return self.raw.one(
            "SELECT id, answer, merged_event_ids, question FROM public.qa_pairs "
            "WHERE session_id=%s AND question=%s LIMIT 1",
            (session, question))

    def qa_count_for_session(self, session: str) -> int:
        row = self.raw.one(
            "SELECT count(*) FROM public.qa_pairs WHERE session_id=%s", (session,))
        return int(row[0]) if row else 0

    def qa_all(self, session: str) -> list:
        return self.raw.rows(
            "SELECT id, question, answer, merged_event_ids FROM public.qa_pairs "
            "WHERE session_id=%s ORDER BY id", (session,))

    def answers_blob(self, session: str) -> str:
        return "\n".join(str(r[2] or "") for r in self.qa_all(session))

    def close(self) -> None:
        timer = getattr(self.core, "_qa_flush_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        fn = getattr(self.core, "shutdown", None)
        if callable(fn):
            try:
                fn(timeout=1.0)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# 5. message builders
# ─────────────────────────────────────────────────────────────────────────────
_TS_BASE = "2026-10-04T02:%02d:%02d+00:00"


def _msg(mid: str, role: str, content: str, minute: int, sec: int,
         turn: str = "T1", display_kind=None, session_source=None) -> dict:
    m = {"id": mid, "role": role, "content": content,
         "timestamp": _TS_BASE % (minute, sec), "turn_id": turn, "host": HOST}
    if display_kind is not None:
        m["display_kind"] = display_kind
    if session_source is not None:
        m["session_source"] = session_source
    return m


def U(mid, content, minute, sec, turn="T1", display_kind=None):
    return _msg(mid, "user", content, minute, sec, turn=turn, display_kind=display_kind)


def A(mid, content, minute, sec, turn="T1", display_kind=None):
    return _msg(mid, "assistant", content, minute, sec, turn=turn,
                display_kind=display_kind)


# ─────────────────────────────────────────────────────────────────────────────
# 6. scenarios (task book §13)
# ─────────────────────────────────────────────────────────────────────────────
def _count(text: str, needle: str) -> int:
    return (text or "").count(needle)


def scenario_1_normal_qa(h: Harness, s: str, report: dict, check) -> None:
    scen = {"session": s, "receipts": {}}
    r1 = h.sync(s, [U("u1", "Q1 normal", 0, 0)])
    h.flush_live()
    r2 = h.sync(s, [U("u1", "Q1 normal", 0, 0), A("a1", "A1-normal-answer", 0, 1)])
    h.flush_live()
    r3 = h.sync(s, [U("u1", "Q1 normal", 0, 0), A("a1", "A1-normal-answer", 0, 1),
                    U("u2", "Q2 next", 0, 2, turn="T2")])
    h.flush_live()
    h.flush_qa()
    scen["receipts"] = {
        "sync_u1": {k: r1.get(k) for k in ("ok", "accepted", "duplicate", "held")},
        "sync_a1": {k: r2.get(k) for k in ("ok", "accepted", "duplicate", "held")},
        "sync_u2": {k: r3.get(k) for k in ("ok", "accepted", "duplicate", "held")},
    }
    row = h.qa_row(s, "u1")
    scen["qa_row"] = row
    check("S1.qa_count_one", h.qa_count_for_session(s) == 1,
          f"qa_pairs rows={h.qa_count_for_session(s)}")
    check("S1.question_is_u1", bool(row) and row[3] == "Q1 normal",
          f"question={row[3] if row else None!r}")
    check("S1.answer_contains_a1", bool(row) and "A1-normal-answer" in (row[1] or ""),
          f"answer={row[1] if row else None!r}")
    check("S1.source_u1_one_row", h.cs_count(s, "u1") == 1,
          f"conversation_stream u1={h.cs_count(s, 'u1')}")
    check("S1.source_a1_one_row", h.cs_count(s, "a1") == 1,
          f"conversation_stream a1={h.cs_count(s, 'a1')}")
    check("S1.source_u2_one_row", h.cs_count(s, "u2") == 1,
          f"conversation_stream u2={h.cs_count(s, 'u2')}")
    report["scenarios"]["S1_normal_qa"] = scen


def scenario_2_multi_assistant(h: Harness, s: str, report: dict, check) -> None:
    scen = {"session": s, "receipts": {}}
    segs = ["A1-seg-one", "A2-seg-two", "A3-seg-three"]
    snap = [U("u1", "Q1 multi", 1, 0), A("a1", segs[0], 1, 1),
            A("a2", segs[1], 1, 2), A("a3", segs[2], 1, 3)]
    h.sync(s, [U("u1", "Q1 multi", 1, 0)])
    h.flush_live()
    h.sync(s, snap)
    h.flush_live()
    r_replay = h.sync(s, list(snap))
    h.sync(s, snap + [U("u2", "Q2 next", 1, 4, turn="T2")])
    h.flush_live()
    h.flush_qa()
    scen["receipts"]["replay"] = {k: r_replay.get(k) for k in
                                  ("ok", "accepted", "duplicate", "held")}
    row = h.qa_row(s, "u1")
    scen["qa_row"] = row
    ans = (row[1] or "") if row else ""
    check("S2.qa_count_one", h.qa_count_for_session(s) == 1,
          f"qa_pairs rows={h.qa_count_for_session(s)}")
    check("S2.replay_accepted_nothing", r_replay.get("accepted") == [],
          f"replay accepted={r_replay.get('accepted')!r}")
    check("S2.replay_reported_duplicate", bool(r_replay.get("duplicate")),
          f"replay duplicate={r_replay.get('duplicate')!r}")
    for i, seg in enumerate(segs, 1):
        check(f"S2.seg{i}_appears_once", _count(ans, seg) == 1,
              f"count({seg})={_count(ans, seg)} answer={ans!r}")
    for mid in ("u1", "a1", "a2", "a3"):
        check(f"S2.source_{mid}_one_row", h.cs_count(s, mid) == 1,
              f"conversation_stream {mid}={h.cs_count(s, mid)}")
    report["scenarios"]["S2_multi_assistant"] = scen


def scenario_3_compaction_continuation(h: Harness, s: str, report: dict, check) -> None:
    scen = {"session": s, "receipts": {}}
    h.sync(s, [U("u1", "Q1 compaction", 2, 0)])
    h.flush_live()
    r = h.sync(s, [U("u1", "Q1 compaction", 2, 0),
                   A("a1", "A1-pre-compaction", 2, 1),
                   U("ctx", CTX_COMPACTION, 2, 2),
                   A("a2", "A2-post-compaction", 2, 3),
                   A("a3", "A3-post-compaction", 2, 4)])
    h.flush_live()
    h.flush_qa()
    scen["receipts"]["sync"] = {k: r.get(k) for k in
                                ("ok", "accepted", "duplicate", "held")}
    row = h.qa_row(s, "u1")
    scen["qa_row"] = row
    ans = (row[1] or "") if row else ""
    check("S3.qa_count_one", h.qa_count_for_session(s) == 1,
          f"qa_pairs rows={h.qa_count_for_session(s)}")
    check("S3.question_is_u1", bool(row) and row[3] == "Q1 compaction",
          f"question={row[3] if row else None!r}")
    for seg in ("A1-pre-compaction", "A2-post-compaction", "A3-post-compaction"):
        check(f"S3.answer_has_{seg}", seg in ans, f"answer={ans!r}")
    check("S3.ctx_marker_excluded", "[CONTEXT COMPACTION" not in ans,
          f"answer={ans!r}")
    check("S3.ctx_not_written_to_source", h.cs_count(s, "ctx") == 0,
          f"conversation_stream ctx={h.cs_count(s, 'ctx')}")
    report["scenarios"]["S3_compaction_continuation"] = scen


def scenario_4_compaction_after_flush(h: Harness, s: str, report: dict, check) -> None:
    """BLOCKER: flush, then full snapshot U1/A1/CTX/A2 -> ONE QA, A2 appended."""
    scen = {"session": s, "receipts": {}}
    h.sync(s, [U("u1", "Q1 flush-then-continue", 3, 0)])
    h.flush_live()
    h.sync(s, [U("u1", "Q1 flush-then-continue", 3, 0),
               A("a1", "A1-pre-flush", 3, 1)])
    h.flush_live()
    h.flush_qa()
    pre = h.qa_row(s, "u1")
    scen["pre_flush_qa"] = pre
    check("S4.precondition_one_qa_with_a1",
          bool(pre) and "A1-pre-flush" in (pre[1] or "") and h.qa_count_for_session(s) == 1,
          f"qa_row={pre!r} count={h.qa_count_for_session(s)}")
    r = h.sync(s, [U("u1", "Q1 flush-then-continue", 3, 0),
                   A("a1", "A1-pre-flush", 3, 1),
                   U("ctx", CTX_COMPACTION, 3, 2),
                   A("a2", "A2-post-compaction-late", 3, 3, turn="")])
    h.flush_live()
    h.flush_qa()
    scen["receipts"]["sync_after_flush"] = {
        k: r.get(k) for k in ("ok", "accepted", "duplicate", "held")}
    scen["receipts"]["sync_after_flush"]["derivation"] = r.get("derivation")
    row = h.qa_row(s, "u1")
    scen["qa_row_after"] = row
    ans = (row[1] or "") if row else ""
    check("S4.qa_count_still_one", h.qa_count_for_session(s) == 1,
          f"qa_pairs rows={h.qa_count_for_session(s)}")
    check("S4.question_still_u1", bool(row) and row[3] == "Q1 flush-then-continue",
          f"question={row[3] if row else None!r}")
    check("S4.existing_answer_survives", "A1-pre-flush" in ans, f"answer={ans!r}")
    check("S4.late_continuation_appended", "A2-post-compaction-late" in ans,
          f"BLOCKER: answer={ans!r} derivation={r.get('derivation')!r}")
    check("S4.late_continuation_once", _count(ans, "A2-post-compaction-late") == 1,
          f"count={_count(ans, 'A2-post-compaction-late')} answer={ans!r}")
    check("S4.late_source_one_row", h.cs_count(s, "a2") == 1,
          f"conversation_stream a2={h.cs_count(s, 'a2')}")
    deriv = r.get("derivation") or {}
    check("S4.receipt_reports_derivation",
          deriv.get("a2") in DERIVATION_STATUSES,
          f"receipt['derivation']={deriv!r}")
    report["scenarios"]["S4_compaction_after_flush"] = scen


def scenario_5_oob_user(h: Harness, s: str, report: dict, check) -> None:
    scen = {"session": s, "receipts": {}}
    h.sync(s, [U("u1", "Q1 original", 4, 0)])
    h.flush_live()
    r = h.sync(s, [U("u1", "Q1 original", 4, 0),
                   A("a1", "A1-original-answer", 4, 1),
                   U("oob", OOB_WRAPPER, 4, 2, display_kind="steer"),
                   A("a2", "A2-answer-to-steer", 4, 3)])
    h.flush_live()
    h.flush_qa()
    scen["receipts"]["sync"] = {k: r.get(k) for k in
                                ("ok", "accepted", "duplicate", "held")}
    q1 = h.qa_by_question(s, "Q1 original")
    q2 = h.qa_by_question(s, OOB_PAYLOAD)
    raw_oob = h.cs_content(s, "oob")
    scen["qa_u1"] = q1
    scen["qa_oob"] = q2
    scen["raw_oob_content"] = raw_oob
    check("S5.pre_steer_qa_preserved", q1 is not None, f"qa_u1={q1!r}")
    check("S5.pre_steer_answer_preserved",
          bool(q1) and "A1-original-answer" in (q1[1] or ""), f"qa_u1={q1!r}")
    check("S5.oob_opens_new_qa", q2 is not None,
          f"qa(oob)={q2!r} all={h.qa_all(s)!r}")
    check("S5.oob_question_is_payload", bool(q2) and q2[3] == OOB_PAYLOAD,
          f"question={q2[3] if q2 else None!r}")
    check("S5.oob_answer_pairs_to_steer",
          bool(q2) and "A2-answer-to-steer" in (q2[1] or ""), f"qa(oob)={q2!r}")
    check("S5.steer_reply_not_folded_into_pre_steer",
          bool(q1) and "A2-answer-to-steer" not in (q1[1] or ""), f"qa_u1={q1!r}")
    questions = " || ".join(str(r[1] or "") for r in h.qa_all(s))
    check("S5.wrapper_not_in_any_question", "OUT-OF-BAND" not in questions,
          f"questions={questions!r}")
    check("S5.raw_source_preserves_wrapper",
          bool(raw_oob) and "[OUT-OF-BAND USER MESSAGE" in raw_oob
          and OOB_PAYLOAD in raw_oob,
          f"conversation_stream oob content={raw_oob!r}")
    check("S5.oob_source_one_row", h.cs_count(s, "oob") == 1,
          f"conversation_stream oob={h.cs_count(s, 'oob')}")
    check("S5.qa_count_two", h.qa_count_for_session(s) == 2,
          f"qa_pairs rows={h.qa_count_for_session(s)}")
    report["scenarios"]["S5_oob_user"] = scen


def scenario_6_control_transparency(h: Harness, s: str, report: dict, check) -> None:
    scen = {"session": s, "receipts": {}}
    h.sync(s, [U("u1", "Q1 model-switch", 5, 0)])
    h.flush_live()
    r = h.sync(s, [U("u1", "Q1 model-switch", 5, 0),
                   A("a1", "A1-before-switch", 5, 1),
                   U("sys", SYS_MODEL_SWITCH, 5, 2, display_kind="model_switch"),
                   A("a2", "A2-after-switch", 5, 3)])
    h.flush_live()
    h.flush_qa()
    scen["receipts"]["sync"] = {k: r.get(k) for k in
                                ("ok", "accepted", "duplicate", "held")}
    row = h.qa_row(s, "u1")
    scen["qa_row"] = row
    ans = (row[1] or "") if row else ""
    q = (row[3] or "") if row else ""
    check("S6.qa_count_one", h.qa_count_for_session(s) == 1,
          f"qa_pairs rows={h.qa_count_for_session(s)}")
    check("S6.question_is_u1", bool(row) and q == "Q1 model-switch",
          f"question={q!r}")
    check("S6.answer_has_both_segments",
          "A1-before-switch" in ans and "A2-after-switch" in ans, f"answer={ans!r}")
    check("S6.control_body_excluded_from_qa",
          "[System:" not in ans and "[System:" not in q, f"answer={ans!r} q={q!r}")
    check("S6.control_not_written_to_source", h.cs_count(s, "sys") == 0,
          f"conversation_stream sys={h.cs_count(s, 'sys')}")
    report["scenarios"]["S6_control_transparency"] = scen


def scenario_7_async_contamination(h: Harness, s: str, report: dict, check) -> None:
    scen = {"session": s, "receipts": {}}
    h.sync(s, [U("u1", "Q1 async-boundary", 6, 0)])
    h.flush_live()
    r = h.sync(s, [U("u1", "Q1 async-boundary", 6, 0),
                   A("a1", "A1-human-answer", 6, 1),
                   U("async", ASYNC_BATCH_COMPLETE, 6, 2,
                     display_kind="async_delegation_complete"),
                   A("a_async", "A_ASYNC-autonomous-report", 6, 3, turn="")])
    h.flush_live()
    h.flush_qa()
    scen["receipts"]["sync"] = {k: r.get(k) for k in
                                ("ok", "accepted", "duplicate", "held")}
    scen["receipts"]["sync"]["derivation"] = r.get("derivation")
    q1 = h.qa_by_question(s, "Q1 async-boundary")
    scen["qa_u1"] = q1
    scen["all_qa"] = h.qa_all(s)
    check("S7.human_qa_exists", q1 is not None, f"qa_u1={q1!r}")
    check("S7.human_answer_preserved",
          bool(q1) and "A1-human-answer" in (q1[1] or ""), f"qa_u1={q1!r}")
    check("S7.async_output_not_in_any_qa",
          "A_ASYNC-autonomous-report" not in h.answers_blob(s),
          f"all_qa={h.qa_all(s)!r}")
    check("S7.async_source_durable", h.cs_count(s, "a_async") == 1,
          f"conversation_stream a_async={h.cs_count(s, 'a_async')}")
    check("S7.async_marker_not_written_to_source", h.cs_count(s, "async") == 0,
          f"conversation_stream async={h.cs_count(s, 'async')}")
    check("S7.qa_count_one_only_human_qa", h.qa_count_for_session(s) == 1,
          f"qa_pairs rows={h.qa_count_for_session(s)} (autonomous output must "
          f"not open its own QA)")
    # The autonomous output is source-durable but must NOT be derived into any
    # QA: the receipt may report it under accepted / duplicate / held, but its
    # derivation must never be a merge status (contract §3: "no QA pollution").
    _a_deriv = (r.get("derivation") or {}).get("a_async")
    _a_reported = ("a_async" in (r.get("accepted") or [])
                   or "a_async" in (r.get("held") or [])
                   or "a_async" in (r.get("duplicate") or []))
    check("S7.async_output_reported_but_not_derived",
          _a_reported and _a_deriv not in DERIVATION_STATUSES,
          f"accepted={r.get('accepted')!r} held={r.get('held')!r} "
          f"duplicate={r.get('duplicate')!r} derivation={r.get('derivation')!r}")
    report["scenarios"]["S7_async_contamination"] = scen


def scenario_8_failed_turn(h: Harness, s: str, report: dict, check) -> None:
    scen = {"session": s, "receipts": {}}
    h.sync(s, [U("u1", "Q1 failed-turn", 7, 0)])
    h.flush_live()
    h.sync(s, [U("u1", "Q1 failed-turn", 7, 0),
               A("a1", "A1-valid-answer", 7, 1)])
    h.flush_live()
    r = h.sync(s, [U("u1", "Q1 failed-turn", 7, 0),
                   A("a1", "A1-valid-answer", 7, 1),
                   A("failed", FAILED_TURN_NOTICE, 7, 2, turn="",
                     display_kind="failed_turn")])
    h.flush_live()
    h.flush_qa()
    scen["receipts"]["sync"] = {k: r.get(k) for k in
                                ("ok", "accepted", "duplicate", "held")}
    row = h.qa_row(s, "u1")
    scen["qa_row"] = row
    scen["all_qa"] = h.qa_all(s)
    ans = (row[1] or "") if row else ""
    blob = h.answers_blob(s)
    check("S8.qa_count_one", h.qa_count_for_session(s) == 1,
          f"qa_pairs rows={h.qa_count_for_session(s)}")
    check("S8.valid_answer_preserved", "A1-valid-answer" in ans, f"answer={ans!r}")
    check("S8.failed_notice_not_an_answer", FAILED_TURN_NOTICE not in blob,
          f"answers_blob={blob!r}")
    questions = " || ".join(str(r[1] or "") for r in h.qa_all(s))
    check("S8.failed_notice_not_a_question", FAILED_TURN_NOTICE not in questions,
          f"questions={questions!r}")
    check("S8.failed_not_written_to_source", h.cs_count(s, "failed") == 0,
          f"conversation_stream failed={h.cs_count(s, 'failed')}")
    report["scenarios"]["S8_failed_turn"] = scen


def scenario_9_replay(h: Harness, s: str, report: dict, check) -> None:
    scen = {"session": s, "receipts": []}
    h.sync(s, [U("u1", "Q1 replay-continuation", 8, 0)])
    h.flush_live()
    h.sync(s, [U("u1", "Q1 replay-continuation", 8, 0), A("a1", "A1-replay", 8, 1)])
    h.flush_live()
    h.flush_qa()
    snap = [U("u1", "Q1 replay-continuation", 8, 0), A("a1", "A1-replay", 8, 1),
            U("ctx", CTX_COMPACTION, 8, 2), A("a2", "A2-replay-late", 8, 3, turn="")]
    receipts = []
    for _ in range(3):
        r = h.sync(s, list(snap))
        h.flush_live()
        h.flush_qa()
        receipts.append({k: r.get(k) for k in
                         ("ok", "accepted", "duplicate", "held")})
    scen["receipts"] = receipts
    row = h.qa_row(s, "u1")
    scen["qa_row"] = row
    ans = (row[1] or "") if row else ""
    mev = _mev(row[2]) if row else []
    scen["merged_event_ids"] = mev
    check("S9.qa_count_one", h.qa_count_for_session(s) == 1,
          f"qa_pairs rows={h.qa_count_for_session(s)}")
    check("S9.a2_appears_once", _count(ans, "A2-replay-late") == 1,
          f"count={_count(ans, 'A2-replay-late')} answer={ans!r}")
    check("S9.a1_appears_once", _count(ans, "A1-replay") == 1,
          f"count={_count(ans, 'A1-replay')} answer={ans!r}")
    check("S9.merged_event_ids_no_dup", len(mev) == len(set(mev)),
          f"merged_event_ids={mev!r}")
    check("S9.merged_event_ids_has_a2", "a2" in mev, f"merged_event_ids={mev!r}")
    for mid in ("u1", "a1", "a2"):
        check(f"S9.source_{mid}_one_row", h.cs_count(s, mid) == 1,
              f"conversation_stream {mid}={h.cs_count(s, mid)}")
    for i, rec in enumerate(receipts[1:], start=2):
        check(f"S9.replay{i}_accepted_nothing", rec.get("accepted") == [],
              f"replay#{i} accepted={rec.get('accepted')!r}")
        check(f"S9.replay{i}_reported_duplicate", bool(rec.get("duplicate")),
              f"replay#{i} duplicate={rec.get('duplicate')!r}")
    report["scenarios"]["S9_replay"] = scen


def scenario_10_restart(h: Harness, make_harness, s: str, report: dict, check):
    """Partial flow, then a NEW V3Core over the same DB re-syncs the snapshot."""
    scen = {"session": s, "receipts": {}}
    h.sync(s, [U("u1", "Q1 restart-continuation", 9, 0)])
    h.flush_live()
    h.sync(s, [U("u1", "Q1 restart-continuation", 9, 0), A("a1", "A1-restart", 9, 1)])
    h.flush_live()
    h.flush_qa()
    pre = h.qa_row(s, "u1")
    scen["pre_restart_qa"] = pre
    check("S10.pre_restart_one_qa_with_a1",
          bool(pre) and "A1-restart" in (pre[1] or "") and h.qa_count_for_session(s) == 1,
          f"qa_row={pre!r} count={h.qa_count_for_session(s)}")

    # ── process restart: brand-new V3Core over the SAME DB ──
    h.close()
    h2 = make_harness()
    check("S10.restart_core_connected", h2.pg_connected,
          f"new core PgEmbedStore.is_connected()={h2.pg_connected}")

    r = h2.sync(s, [U("u1", "Q1 restart-continuation", 9, 0),
                    A("a1", "A1-restart", 9, 1),
                    U("ctx", CTX_COMPACTION, 9, 2),
                    A("a2", "A2-restart-late", 9, 3, turn="")])
    h2.flush_live()
    h2.flush_qa()
    scen["receipts"]["post_restart_sync"] = {
        k: r.get(k) for k in ("ok", "accepted", "duplicate", "held")}
    scen["receipts"]["post_restart_sync"]["derivation"] = r.get("derivation")
    row = h2.qa_row(s, "u1")
    scen["post_restart_qa"] = row
    ans = (row[1] or "") if row else ""
    mev = _mev(row[2]) if row else []
    scen["merged_event_ids"] = mev
    check("S10.qa_count_still_one_after_restart",
          h2.qa_count_for_session(s) == 1,
          f"qa_pairs rows={h2.qa_count_for_session(s)}")
    check("S10.question_still_u1",
          bool(row) and row[3] == "Q1 restart-continuation",
          f"question={row[3] if row else None!r}")
    check("S10.a1_once", _count(ans, "A1-restart") == 1,
          f"count(A1-restart)={_count(ans, 'A1-restart')} answer={ans!r}")
    check("S10.a2_appended_once", _count(ans, "A2-restart-late") == 1,
          f"count(A2-restart-late)={_count(ans, 'A2-restart-late')} answer={ans!r}")
    check("S10.merged_event_ids_no_dup", len(mev) == len(set(mev)),
          f"merged_event_ids={mev!r}")
    for mid in ("u1", "a1", "a2"):
        check(f"S10.source_{mid}_one_row", h2.cs_count(s, mid) == 1,
              f"conversation_stream {mid}={h2.cs_count(s, mid)}")
    report["scenarios"]["S10_restart"] = scen
    return h2


# ─────────────────────────────────────────────────────────────────────────────
# 7. main
# ─────────────────────────────────────────────────────────────────────────────
def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="P0-C1 Y400 real-PostgreSQL E2E driver (disposable DB only)")
    ap.add_argument("--pg-host", default=os.environ.get("P0C1_PG_HOST", ""))
    ap.add_argument("--pg-port", default=os.environ.get("P0C1_PG_PORT", ""))
    ap.add_argument("--pg-db", default=os.environ.get("P0C1_PG_DB", ""))
    ap.add_argument("--pg-user", default=os.environ.get("P0C1_PG_USER", ""))
    ap.add_argument("--pg-password",
                    default=(os.environ.get("P0C1_PG_PASSWORD")
                             or os.environ.get("PGPASSWORD", "")))
    ap.add_argument("--run-root", default="",
                    help="isolated run root (default: <driver dir>/e2e-runs/<ts>)")
    ap.add_argument("--out", default="",
                    help="report path (default: <driver dir>/p0c1-e2e-report-<ts>.json)")
    ap.add_argument("--bootstrap", action="store_true",
                    help="run the real distribution_cli bootstrap first")
    ap.add_argument("--keep", action="store_true",
                    help="keep the isolated run root after the run")
    return ap.parse_args(argv)


def run_bootstrap(pg: dict, cwd: Path) -> tuple:
    dsn = (f"postgresql://{pg['user']}@{pg['host']}:{int(pg['port'])}"
           f"/{pg['database']}")
    env = dict(os.environ)
    env["PGPASSWORD"] = pg["password"]
    env["V3CORE_PG_PASSWORD"] = pg["password"]
    prior_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC) + ((os.pathsep + prior_pp) if prior_pp else "")
    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "v3core.distribution_cli",
             "bootstrap", "--target", dsn],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=str(cwd), timeout=600)
    except Exception as exc:
        return False, _safe_err(exc)
    out = (proc.stdout or "").strip()
    parsed = {}
    try:
        parsed = json.loads(out) if out else {}
    except Exception:
        parsed = {}
    applied = parsed.get("applied")
    inner = parsed.get("result") if isinstance(parsed.get("result"), dict) else {}
    if applied is None:
        applied = inner.get("applied")
    detail = (f"rc={proc.returncode} applied={applied} "
              f"stderr={(proc.stderr or '')[-300:]!r}")
    return (proc.returncode == 0), detail


def main(argv=None) -> int:
    args = parse_args(argv)
    ts = _ts_slug()
    out_path = (Path(args.out).resolve() if args.out
                else DRIVER_DIR / f"p0c1-e2e-report-{ts}.json")

    pg = {
        "host": str(args.pg_host or "").strip(),
        "port": str(args.pg_port or "").strip(),
        "database": str(args.pg_db or "").strip(),
        "user": str(args.pg_user or "").strip(),
        "password": str(args.pg_password or ""),
    }

    reason = gate_target(pg["host"], pg["port"], pg["database"], pg["user"],
                         pg["password"])
    if reason:
        print(f"REFUSE: {reason}", file=sys.stderr)
        return 2

    pg["port"] = int(pg["port"])
    run_root = (Path(args.run_root).resolve() if args.run_root
                else DRIVER_DIR / "e2e-runs" / ts)

    checks: list = []
    failures: list = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "pass": bool(ok), "detail": detail})
        if not ok:
            failures.append({"check": name, "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" :: {detail}"),
              flush=True)
        return bool(ok)

    report: dict = {
        "scope": ("P0-C1 Hermes host-event semantics — real V3Core over a "
                  "disposable PostgreSQL; task book §13 (10 scenarios)"),
        "design": "docs/P0C1-HOST-EVENT-SEMANTICS.md",
        "when": _iso_now(),
        "timestamp": ts,
        "host_machine": (os.environ.get("COMPUTERNAME") or ""),
        "python": sys.version.split()[0],
        "repo": str(REPO),
        "git_head": _git_head(),
        "target": {
            "pg_host": pg["host"], "pg_port": pg["port"], "pg_db": pg["database"],
            "pg_user": pg["user"], "password_in_report": False,
        },
        "checks": checks,
        "failures": failures,
        "scenarios": {},
        "run_root": str(run_root),
        "baseline": {"llm_configured": False, "embedding_configured": False},
        "verdict": "FAIL",
    }

    harness = None
    try:
        install_pg_guard()

        run_root.mkdir(parents=True, exist_ok=True)
        iso = isolate_environment(run_root, pg)
        report["isolation"] = iso
        config = build_explicit_config(pg, Path(iso["data"]))

        if args.bootstrap:
            ok, detail = run_bootstrap(pg, Path(iso["home"]))
            check("PRE.bootstrap_cli_ok", ok, detail)
            report["bootstrap"] = {"ok": ok, "detail": detail}

        raw = Pg(pg)
        tables = raw.tables()
        check("PRE.schema_conversation_stream_present",
              "conversation_stream" in tables, f"public tables={sorted(tables)[:20]}")
        check("PRE.schema_qa_pairs_present",
              "qa_pairs" in tables, f"public tables={sorted(tables)[:20]}")
        cs_cols = raw.columns("conversation_stream") if "conversation_stream" in tables else set()
        qa_cols = raw.columns("qa_pairs") if "qa_pairs" in tables else set()
        check("PRE.schema_host_turn_id_present", "host_turn_id" in cs_cols,
              f"conversation_stream columns={sorted(cs_cols)}")
        check("PRE.schema_merged_event_ids_present", "merged_event_ids" in qa_cols,
              f"qa_pairs columns={sorted(qa_cols)}")
        if failures:
            report["verdict"] = "FAIL"
            return _finish(report, out_path, run_root, args.keep)

        from v3core.embedding import safe_embed_cfg
        embed_cfg = safe_embed_cfg(config)
        check("PRE.no_embedding_configured", embed_cfg is None,
              f"safe_embed_cfg(config)={embed_cfg!r} (must be None)")
        llm = getattr(config, "llm", None)
        check("PRE.no_llm_configured",
              llm is not None and not (llm.provider or llm.model or llm.base_url),
              f"llm.provider={getattr(llm, 'provider', None)!r} "
              f"llm.model={getattr(llm, 'model', None)!r}")
        report["baseline"] = {
            "llm_configured": bool(llm and (llm.provider or llm.model or llm.base_url)),
            "embedding_configured": embed_cfg is not None,
            "note": "no embed section -> safe_embed_cfg None -> no external API call",
        }

        def make_harness():
            return Harness(config, iso, pg)

        harness = make_harness()
        check("PRE.real_core_pg_connected", harness.pg_connected,
              f"PgEmbedStore.is_connected()={harness.pg_connected} "
              f"target={pg['host']}:{pg['port']}/{pg['database']}")
        check("PRE.live_buffer_embed_disabled",
              getattr(harness.buffer, "_embed_cfg", "unset") is None,
              f"LiveBuffer._embed_cfg={getattr(harness.buffer, '_embed_cfg', 'unset')!r}")
        if failures:
            report["verdict"] = "FAIL"
            return _finish(report, out_path, run_root, args.keep)

        # ── 10 scenarios ────────────────────────────────────────────────────
        scenario_1_normal_qa(harness, "p0c1e2e-S1", report, check)
        scenario_2_multi_assistant(harness, "p0c1e2e-S2", report, check)
        scenario_3_compaction_continuation(harness, "p0c1e2e-S3", report, check)
        scenario_4_compaction_after_flush(harness, "p0c1e2e-S4", report, check)
        scenario_5_oob_user(harness, "p0c1e2e-S5", report, check)
        scenario_6_control_transparency(harness, "p0c1e2e-S6", report, check)
        scenario_7_async_contamination(harness, "p0c1e2e-S7", report, check)
        scenario_8_failed_turn(harness, "p0c1e2e-S8", report, check)
        scenario_9_replay(harness, "p0c1e2e-S9", report, check)
        harness = scenario_10_restart(harness, make_harness, "p0c1e2e-S10",
                                      report, check)

    except Exception as exc:
        report["harness_error"] = _safe_err(exc, 2000)
        report["traceback"] = traceback.format_exc()[-3000:]
        check("harness.no_unhandled_exception", False, _safe_err(exc, 600))
    finally:
        if harness is not None:
            harness.close()

    return _finish(report, out_path, run_root, args.keep)


def _finish(report: dict, out_path: Path, run_root: Path, keep: bool) -> int:
    report["check_count"] = len(report.get("checks", []))
    report["verdict"] = "PASS" if not report.get("failures") else "FAIL"
    report["finished_at"] = _iso_now()

    payload = json.dumps(report, ensure_ascii=False, default=str, indent=2)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload, encoding="utf-8")

    print(f"\nchecks={report['check_count']} failures={len(report['failures'])} "
          f"verdict={report['verdict']}")
    print(f"report: {out_path}")

    if not keep:
        try:
            shutil.rmtree(run_root, ignore_errors=True)
        except Exception:
            pass
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
