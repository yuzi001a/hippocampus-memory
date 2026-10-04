#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-A late-assistant QA derivation — Y400 real-PostgreSQL E2E driver.

Task book §19 / §20 acceptance on a **disposable** PostgreSQL (isolated DB /
port).  This driver exercises the REAL ``V3Core`` product paths:

  * real ``V3Core.sync_turn`` (source ingest + QA pairing + P0-A late
    resolution),
  * real ``LiveBuffer`` (durable outbox marker + real
    ``PgEmbedStore.insert_message`` -> ``conversation_stream``),
  * real ``core._flush_all_pending_qa()`` + ``core._drain_flush_queue()``
    (the production flush chain -> ``qa_pairs``),
  * real ``V3Config`` resolved from an ISOLATED profile directory with an
    EXPLICIT pg config dict (never production).

Scenarios
---------
  A (task book §19)  U1 -> durable -> progress(same turn) -> real flush
                     (pending cleared) -> late A1 (same turn, carrying
                     ``UNIQUE-LATE-DELIVERY-MARKER``).  Asserts:
                       * conversation_stream: U1 == 1 row, A1 == 1 row
                       * qa_pairs: exactly 1 row for Q(U1), answer carries
                         the marker
                       * resend A1 -> A1 still 1 row, qa_pairs still 1 row,
                         marker appears exactly once in answer
  B (task book §20)  U1 -> flush -> U2 becomes the current pending -> late
                     A1(T1).  Asserts: QA(U1).answer carries A1;
                     QA(U2) does NOT; U2's pending/derivation stays valid.

Baseline: NO LLM / NO EMBEDDING — the profile configures neither, so the whole
run must pass without any external API call (``safe_embed_cfg`` -> None,
``embed_for_write`` short-circuits with ``NO_INPUT``).

Isolation / safety (this is a hard requirement, not a convention)
----------------------------------------------------------------
1. ``--pg-host/--pg-port/--pg-db/--pg-user/--pg-password`` (or the same-named
   env vars ``P0A_PG_*``; password also accepts ``PGPASSWORD``).
   Missing parameter  -> explicit error + exit 2, NO default fallback
   connection.
   ``dbname`` contains ``v3embeddings``  -> REFUSE, exit 2.
   ``port == 5433``                      -> REFUSE, exit 2.
2. A ``psycopg2.connect`` guard is installed for the whole process: any
   connection to the production port / production database name raises, so a
   leaf that ignored its config cannot silently reach production.
3. The run root (isolated HOME + profile + data dir + durable markers) is a
   fresh directory next to this script; nothing outside it is written.

Y400 usage (PowerShell)
-----------------------
    cd C:\\hp-testbed
    .\\.venv\\Scripts\\python.exe evidence\\p0a-late-assistant-derivation-20261004\\p0a_e2e.py `
        --pg-host 127.0.0.1 --pg-port 55432 `
        --pg-db p0ae2e_20261004 --pg-user p0ae2e `
        --pg-password <DISPOSABLE_DB_PASSWORD> `
        --bootstrap            # optional: run the real bootstrap CLI first

Preconditions
-------------
The disposable database must be bootstrapped with the P0-A schema (the two new
columns ``conversation_stream.host_turn_id`` and ``qa_pairs.merged_event_ids``).
``--bootstrap`` runs ``python -m v3core.distribution_cli bootstrap --target``
through the same interpreter; without it the driver only CHECKS the schema and
fails loudly (``PRE.*`` checks) if it is missing or partially upgraded.

Exit code 0 = verdict PASS, 1 = verdict FAIL, 2 = refused before any run.

This driver never prints or writes the PG password; the report records
``password_in_report: false``.
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
REPO = Path(__file__).resolve().parents[2]          # C:/hp-testbed
SRC = REPO / "src" / "v3-core" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DRIVER_DIR = Path(__file__).resolve().parent

# ── frozen contract constants ───────────────────────────────────────────────
HOST = "p0ae2e"                       # non-legacy host namespace (P0-A gate)
PROFILE = "p0ae2e"                    # isolated profile name
LEGACY_HOST = "legacy"
MARKER = "UNIQUE-LATE-DELIVERY-MARKER"

#: Targets this driver must NEVER touch (defence in depth on top of the CLI gate).
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


# ─────────────────────────────────────────────────────────────────────────────
# small utilities
# ─────────────────────────────────────────────────────────────────────────────
def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_err(exc: BaseException, limit: int = 400) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:limit]


def _git_head() -> str:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=20)
        return (out.stdout or "").strip() if out.returncode == 0 else ""
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# 1. fail-closed target gate (runs before ANY file or connection is created)
# ─────────────────────────────────────────────────────────────────────────────
def gate_target(host: str, port, db: str, user: str, password: str) -> str:
    """Return an empty string when the target is admissible, else the reason.

    Never falls back to a default connection: a missing parameter is a refusal.
    """
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
    """Block any psycopg2 connection aimed at a production target.

    Installed before ``v3core`` is imported so even a leaf that resolved its own
    (wrong) config cannot reach production.  Disposable-target connections used
    by this driver pass through untouched.
    """
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
                        f"P0-A E2E guard: production port {port} refused")
            except (TypeError, ValueError):
                pass
        for marker in PRODUCTION_DB_MARKERS:
            if marker in db.lower() or marker in low_dsn:
                raise AssertionError(
                    f"P0-A E2E guard: production database refused ({marker})")
        for prod_port in PRODUCTION_PORTS:
            if f"port={prod_port}" in low_dsn:
                raise AssertionError(
                    f"P0-A E2E guard: production port {prod_port} refused (dsn)")
        return original(*args, **kwargs)

    psycopg2.connect = guarded  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
# 2. isolated profile + explicit pg config dict
# ─────────────────────────────────────────────────────────────────────────────
def isolate_environment(run_root: Path, pg: dict) -> dict:
    """Create an isolated HOME/profile tree and point the process at it.

    Layout::

        <run_root>/home/.v3-core/.env                      (empty, isolated)
        <run_root>/home/.v3-core/profiles/p0ae2e/config.yaml
        <run_root>/data/                                   (basePath: durable markers)

    The config.yaml carries NO credential (the password arrives through
    ``V3CORE_PG_PASSWORD``, the documented public-alpha env contract) and NO
    embed/llm/observer/e1 section, so the NO-EMBEDDING baseline holds.
    """
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
    """Build a V3Config from an EXPLICIT pg dict (no ambient resolution).

    The dict is the legacy shape ``from_legacy_dict`` consumes.  ``storage.embed``
    and ``storage.rerank`` are deliberately ABSENT: an absent section makes
    ``safe_embed_cfg`` return ``None`` (NO EMBEDDING baseline), whereas an empty
    present section is treated as configured-but-invalid.
    """
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
    """Thin raw-SQL reader against the disposable database."""

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
        self.core = V3Core(profile=PROFILE, hermes_home=iso["home"],
                           effective_config=config)
        self.core._config = config          # pin the explicit config
        self.raw = Pg(raw_pg)

        # real store, explicitly verified before anything runs
        store = self.core.pg
        self.core._pg = store
        self.pg_connected = bool(store and store.is_connected())

        # real LiveBuffer (source ingest).  The writer thread is disabled so the
        # conversation_stream write is deterministic: ``flush_live`` drains the
        # queue and calls the REAL ``LiveBuffer._flush`` itself.
        self.buffer = self.core.live_buffer
        self.buffer._start_writer = lambda: None

    # -- source ingest --------------------------------------------------------
    def sync(self, session: str, messages: list) -> dict:
        return self.core.sync_turn(session, messages, host=HOST) or {}

    def flush_live(self) -> int:
        """Drain the live queue and run the real ``LiveBuffer._flush``."""
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
        """Real watchdog flush: submit + bounded drain of the flush queue."""
        self.core._flush_all_pending_qa(timeout=timeout)
        self.core._drain_flush_queue(timeout=timeout)

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

    def cs_row(self, session: str, event_id: str):
        return self.raw.one(
            "SELECT id, role, content, host_turn_id FROM public.conversation_stream "
            "WHERE session_id=%s AND host=%s AND event_id=%s ORDER BY id DESC LIMIT 1",
            (session, HOST, event_id))

    def qa_source_id(self, session: str, q_msg_id: str) -> str:
        return f"qa_sync/{HOST}/{session}/{q_msg_id}"

    def qa_row(self, session: str, q_msg_id: str):
        return self.raw.one(
            "SELECT id, answer, merged_event_ids, question FROM public.qa_pairs "
            "WHERE source_id=%s LIMIT 1",
            (self.qa_source_id(session, q_msg_id),))

    def qa_count_for_session(self, session: str) -> int:
        row = self.raw.one(
            "SELECT count(*) FROM public.qa_pairs WHERE session_id=%s", (session,))
        return int(row[0]) if row else 0

    def close(self) -> None:
        # cancel the 60s watchdog timer started by sync_turn so no background
        # flush fires after the assertions.
        timer = getattr(self.core, "_qa_flush_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        for name in ("shutdown",):
            fn = getattr(self.core, name, None)
            if callable(fn):
                try:
                    fn(timeout=1.0)
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# 5. message builders
# ─────────────────────────────────────────────────────────────────────────────
_TS_BASE = "2026-10-04T02:00:%02d+00:00"


def _msg(mid: str, role: str, content: str, sec: int, turn: str) -> dict:
    return {"id": mid, "role": role, "content": content,
            "timestamp": _TS_BASE % sec, "turn_id": turn, "host": HOST}


def _marker_count(answer: str) -> int:
    return (answer or "").count(MARKER)


# ─────────────────────────────────────────────────────────────────────────────
# 6. scenarios
# ─────────────────────────────────────────────────────────────────────────────
def scenario_a(h: Harness, session: str, report: dict, check) -> None:
    """Task book §19 — late A1 after a real flush, plus replay idempotency."""
    u1_content = "请完成 P0-A 验收并给最终报告"
    prog_content = "P0-A PROGRESS: 正在核验（中间态）"
    a1_content = f"P0-A FINAL REPORT: 验收通过。{MARKER}"

    u1 = _msg("u1", "user", u1_content, 0, "T1")
    prog = _msg("aprog", "assistant", prog_content, 1, "T1")
    a1 = _msg("a1", "assistant", a1_content, 2, "T1")

    scen: dict = {"session": session, "receipts": {}}

    # -- step 1: U1 source durable -------------------------------------------
    r1 = h.sync(session, [u1])
    h.flush_live()
    scen["receipts"]["sync_u1"] = {k: r1.get(k) for k in
                                   ("ok", "accepted", "duplicate", "rejected", "held")}
    check("A.conversation_stream_u1_one_row", h.cs_count(session, "u1") == 1,
          f"conversation_stream rows for u1={h.cs_count(session, 'u1')}")
    row_u1 = h.cs_row(session, "u1")
    check("A.u1_row_carries_host_turn_provenance",
          bool(row_u1) and str(row_u1[3] or "") == "T1",
          f"u1 row host_turn_id={row_u1[3] if row_u1 else None!r} (expected 'T1')")

    # -- step 2: progress in the same turn (pending gets an answer) ----------
    r2 = h.sync(session, [u1, prog])
    h.flush_live()
    scen["receipts"]["sync_progress"] = {k: r2.get(k) for k in
                                         ("ok", "accepted", "duplicate", "rejected", "held")}
    pend = h.pending_of(session)
    check("A.progress_appended_to_pending",
          bool(pend) and prog_content in (pend or {}).get("a", ""),
          f"pending={ {k: (v if k != 'a' else v[:60]) for k, v in (pend or {}).items()} }")
    check("A.progress_source_row_present", h.cs_count(session, "aprog") == 1,
          f"conversation_stream rows for aprog={h.cs_count(session, 'aprog')}")

    # -- step 3: real flush -> QA row, pending cleared -----------------------
    h.flush_qa()
    pend_after = h.pending_of(session)
    check("A.pending_cleared_after_flush", pend_after is None,
          f"pending after _flush_all_pending_qa = {pend_after}")
    qa_prog = h.qa_row(session, "u1")
    check("A.qa_row_created_by_flush",
          bool(qa_prog) and prog_content in (qa_prog[1] or ""),
          f"qa_pairs(U1) row={qa_prog}")
    check("A.qa_row_single_before_late",
          h.qa_count_for_session(session) == 1,
          f"qa_pairs rows for session={h.qa_count_for_session(session)}")

    # -- step 4: late A1 (same host turn T1), source + late resolution -------
    r3 = h.sync(session, [u1, prog, a1])
    h.flush_live()
    scen["receipts"]["sync_late_a1"] = {
        k: r3.get(k) for k in ("ok", "accepted", "duplicate", "rejected", "held")}
    scen["receipts"]["sync_late_a1"]["derivation"] = r3.get("derivation")
    check("A.late_a1_source_one_row", h.cs_count(session, "a1") == 1,
          f"conversation_stream rows for a1={h.cs_count(session, 'a1')}")
    qa_late = h.qa_row(session, "u1")
    ans = (qa_late[1] or "") if qa_late else ""
    members = qa_late[2] if qa_late else None
    scen["late"] = {"qa_row": qa_late, "answer": ans, "merged_event_ids": members}
    check("A.qa_row_still_single_after_late",
          h.qa_count_for_session(session) == 1,
          f"qa_pairs rows for session={h.qa_count_for_session(session)}")
    check("A.qa_answer_contains_late_marker", _marker_count(ans) == 1,
          f"marker_count={_marker_count(ans)} answer={ans!r}")
    check("A.qa_answer_keeps_progress_and_late",
          prog_content in ans and a1_content in ans,
          f"progress_present={prog_content in ans} late_present={a1_content in ans}")
    check("A.merged_event_ids_records_a1",
          _jsonb_has(members, "a1"),
          f"merged_event_ids={members!r}")
    deriv = r3.get("derivation") or {}
    check("A.late_receipt_reports_derivation",
          isinstance(deriv, dict) and deriv.get("a1") in DERIVATION_STATUSES,
          f"receipt['derivation']={deriv!r}")

    # -- step 5: resend A1 -> nothing duplicates -----------------------------
    r4 = h.sync(session, [u1, prog, a1])
    h.flush_live()
    scen["receipts"]["resend_a1"] = {
        k: r4.get(k) for k in ("ok", "accepted", "duplicate", "rejected", "held")}
    scen["receipts"]["resend_a1"]["derivation"] = r4.get("derivation")
    check("A.resend_a1_source_still_one_row", h.cs_count(session, "a1") == 1,
          f"conversation_stream rows for a1={h.cs_count(session, 'a1')}")
    qa_re = h.qa_row(session, "u1")
    ans_re = (qa_re[1] or "") if qa_re else ""
    check("A.resend_qa_still_single", h.qa_count_for_session(session) == 1,
          f"qa_pairs rows for session={h.qa_count_for_session(session)}")
    check("A.resend_marker_count_one", _marker_count(ans_re) == 1,
          f"marker_count={_marker_count(ans_re)} answer={ans_re!r}")
    # §19 requires: A1 rows stay 1, qa rows stay 1, marker count 1 (checked
    # above).  The receipt's ``derivation`` key is optional by contract
    # (DESIGN-p0a §5.1: only "when there is new information"); an in-process
    # replay of an already-settled message is short-circuited by the
    # per-session delta cursor (B01 I3/I6) and legitimately reports no new
    # derivation.  Accept either: no new derivation (cursor replay), or an
    # explicit ``already_merged`` (if a future change adds a durable re-check).
    _r4_deriv = (r4.get("derivation") or {}).get("a1")
    check("A.resend_reports_no_false_derivation",
          _r4_deriv in (None, "already_merged"),
          f"receipt['derivation']['a1']={_r4_deriv!r}")

    report["scenarios"]["A"] = scen


def scenario_b(h: Harness, session: str, report: dict, check) -> None:
    """Task book §20 — late A1(T1) must heal Q(U1) and never touch Q(U2)."""
    u1_content = "P0-A 场景B：请处理第一项"
    u2_content = "P0-A 场景B：第二项，新的当前问题"
    a1_content = f"P0-A 场景B 迟到回答（归属 T1）。{MARKER}"

    u1 = _msg("u1", "user", u1_content, 0, "T1")
    u2 = _msg("u2", "user", u2_content, 1, "T2")
    a1 = _msg("a1", "assistant", a1_content, 2, "T1")

    scen: dict = {"session": session, "receipts": {}}

    # -- U1 durable, then U2 replaces it as the current pending --------------
    r1 = h.sync(session, [u1])
    h.flush_live()
    r2 = h.sync(session, [u1, u2])
    h.flush_live()
    h.flush_qa()
    scen["receipts"]["sync_u1"] = {k: r1.get(k) for k in
                                   ("ok", "accepted", "duplicate", "rejected", "held")}
    scen["receipts"]["sync_u2"] = {k: r2.get(k) for k in
                                   ("ok", "accepted", "duplicate", "rejected", "held")}
    check("B.u1_source_one_row", h.cs_count(session, "u1") == 1,
          f"conversation_stream rows for u1={h.cs_count(session, 'u1')}")
    check("B.u2_source_one_row", h.cs_count(session, "u2") == 1,
          f"conversation_stream rows for u2={h.cs_count(session, 'u2')}")

    pend = h.pending_of(session)
    check("B.u2_is_current_pending",
          bool(pend) and (pend or {}).get("q") == u2_content,
          f"pending={ {k: (v if k != 'a' else v[:60]) for k, v in (pend or {}).items()} }")
    qa_u1_pre = h.qa_row(session, "u1")
    check("B.qa_u1_row_exists_after_flush", bool(qa_u1_pre),
          f"qa_pairs(U1) row={qa_u1_pre}")

    # -- late A1 (turn T1) while U2 is pending ------------------------------
    r3 = h.sync(session, [u1, u2, a1])
    h.flush_live()
    scen["receipts"]["sync_late_a1"] = {
        k: r3.get(k) for k in ("ok", "accepted", "duplicate", "rejected", "held")}
    scen["receipts"]["sync_late_a1"]["derivation"] = r3.get("derivation")
    check("B.late_a1_source_one_row", h.cs_count(session, "a1") == 1,
          f"conversation_stream rows for a1={h.cs_count(session, 'a1')}")

    qa_u1 = h.qa_row(session, "u1")
    ans_u1 = (qa_u1[1] or "") if qa_u1 else ""
    qa_u2 = h.qa_row(session, "u2")
    ans_u2 = (qa_u2[1] or "") if qa_u2 else ""
    pend_after = h.pending_of(session)
    scen["qa_u1"] = {"answer": ans_u1, "merged_event_ids": qa_u1[2] if qa_u1 else None}
    scen["qa_u2"] = {"row": qa_u2, "answer": ans_u2}
    scen["pending_after_late"] = pend_after

    check("B.qa_u1_answer_contains_late_a1", _marker_count(ans_u1) == 1,
          f"marker_count={_marker_count(ans_u1)} answer={ans_u1!r}")
    check("B.qa_u2_does_not_contain_late_a1",
          MARKER not in ans_u2 and a1_content not in ans_u2,
          f"qa_u2={'absent' if qa_u2 is None else repr(ans_u2)}")
    check("B.u2_pending_intact_after_late",
          bool(pend_after) and (pend_after or {}).get("q") == u2_content,
          f"pending_after_late={ {k: (v if k != 'a' else v[:60]) for k, v in (pend_after or {}).items()} }")
    check("B.u2_derivation_not_polluted",
          (qa_u2 is None) or (not ans_u2.strip()),
          f"qa_pairs(U2) row={qa_u2}")
    deriv = r3.get("derivation") or {}
    check("B.late_receipt_reports_derivation",
          isinstance(deriv, dict) and deriv.get("a1") in DERIVATION_STATUSES,
          f"receipt['derivation']={deriv!r}")

    report["scenarios"]["B"] = scen


def _jsonb_has(value, event_id: str) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple)):
        return event_id in [str(x) for x in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return False
        return isinstance(parsed, list) and event_id in [str(x) for x in parsed]
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 7. main
# ─────────────────────────────────────────────────────────────────────────────
def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="P0-A Y400 real-PostgreSQL E2E driver (disposable DB only)")
    ap.add_argument("--pg-host", default=os.environ.get("P0A_PG_HOST", ""))
    ap.add_argument("--pg-port", default=os.environ.get("P0A_PG_PORT", ""))
    ap.add_argument("--pg-db", default=os.environ.get("P0A_PG_DB", ""))
    ap.add_argument("--pg-user", default=os.environ.get("P0A_PG_USER", ""))
    ap.add_argument("--pg-password",
                    default=(os.environ.get("P0A_PG_PASSWORD")
                             or os.environ.get("PGPASSWORD", "")))
    ap.add_argument("--run-root", default="",
                    help="isolated run root (default: <driver dir>/e2e-runs/<ts>)")
    ap.add_argument("--out", default="",
                    help="report path (default: <driver dir>/p0a-e2e-report-<ts>.json)")
    ap.add_argument("--bootstrap", action="store_true",
                    help="run the real distribution_cli bootstrap first")
    ap.add_argument("--keep", action="store_true",
                    help="keep the isolated run root after the run")
    return ap.parse_args(argv)


def run_bootstrap(pg: dict, cwd: Path) -> tuple:
    """Bootstrap the disposable DB through the real public CLI. Returns (ok, detail)."""
    dsn = (f"postgresql://{pg['user']}@{pg['host']}:{int(pg['port'])}"
           f"/{pg['database']}")
    env = dict(os.environ)
    env["PGPASSWORD"] = pg["password"]
    env["V3CORE_PG_PASSWORD"] = pg["password"]
    # The child must import the P0-A source tree, not whatever editable
    # install the interpreter's site-packages happens to carry.
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
                else DRIVER_DIR / f"p0a-e2e-report-{ts}.json")

    pg = {
        "host": str(args.pg_host or "").strip(),
        "port": str(args.pg_port or "").strip(),
        "database": str(args.pg_db or "").strip(),
        "user": str(args.pg_user or "").strip(),
        "password": str(args.pg_password or ""),
    }

    # ── 0. fail-closed gate: nothing is created before it passes ────────────
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
        "scope": ("P0-A late-assistant QA derivation — real V3Core over a "
                  "disposable PostgreSQL; task book §19/§20"),
        "design": "DESIGN-p0a.md §9.2",
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

        # ── isolated profile + explicit config ──────────────────────────────
        run_root.mkdir(parents=True, exist_ok=True)
        iso = isolate_environment(run_root, pg)
        report["isolation"] = iso
        config = build_explicit_config(pg, Path(iso["data"]))

        # ── optional bootstrap through the real CLI ─────────────────────────
        if args.bootstrap:
            ok, detail = run_bootstrap(pg, Path(iso["home"]))
            check("PRE.bootstrap_cli_ok", ok, detail)
            report["bootstrap"] = {"ok": ok, "detail": detail}

        # ── schema preconditions (fail loudly on a partial upgrade) ─────────
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
            return _finish(report, out_path, run_root, args.keep, harness)

        # ── NO LLM / NO EMBEDDING baseline ──────────────────────────────────
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

        # ── real core ───────────────────────────────────────────────────────
        harness = Harness(config, iso, pg)
        check("PRE.real_core_pg_connected", harness.pg_connected,
              f"PgEmbedStore.is_connected()={harness.pg_connected} "
              f"target={pg['host']}:{pg['port']}/{pg['database']}")
        check("PRE.live_buffer_embed_disabled",
              getattr(harness.buffer, "_embed_cfg", "unset") is None,
              f"LiveBuffer._embed_cfg={getattr(harness.buffer, '_embed_cfg', 'unset')!r}")
        if failures:
            report["verdict"] = "FAIL"
            return _finish(report, out_path, run_root, args.keep, harness)

        # ── scenarios ───────────────────────────────────────────────────────
        sess_a = f"p0ae2e-A-{ts}"
        sess_b = f"p0ae2e-B-{ts}"
        scenario_a(harness, sess_a, report, check)
        scenario_b(harness, sess_b, report, check)

    except Exception as exc:
        report["harness_error"] = _safe_err(exc, 2000)
        report["traceback"] = traceback.format_exc()[-3000:]
        check("harness.no_unhandled_exception", False, _safe_err(exc, 600))
    finally:
        if harness is not None:
            harness.close()

    return _finish(report, out_path, run_root, args.keep, harness)


def _finish(report: dict, out_path: Path, run_root: Path, keep: bool,
            harness) -> int:
    report["checks"] = report.get("checks", [])
    report["failures"] = report.get("failures", [])
    report["check_count"] = len(report["checks"])
    report["verdict"] = "PASS" if not report["failures"] else "FAIL"
    report["finished_at"] = _iso_now()

    # presence-only secret audit on the FINAL payload
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
