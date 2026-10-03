#!/usr/bin/env python3
"""I01 isolated integration — real hosts, real formats, disposable PG.

Scope discipline (mirrors the B04 driver):
  * runs only on the dedicated test laptop (hostname gate)
  * touches only 127.0.0.1:55432 (disposable PG) and the isolated --root
  * creates a fresh database with an ``i01e2e_`` prefix; never reuses/drops
  * no LLM, no embedding, no external endpoint (keyword-only core config)
  * credentials: process env only, never argv, never printed

What it proves (task book §27):
  AUTO DISCOVERY, Hermes/DSH/pi/memory-md import, dry-run zero-write,
  raw preservation + native identity, self-memory exclusion, rerun
  idempotency, deterministic recall-ready derivation, historical fact →
  new-session recall (bridge /prefetch), source trace, source host files
  unchanged, production untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOST_GATE = "DESKTOP-EQP3OBU"

# Fixture markers used for recall / trace checks.
HERMES_MARKER = "银杏"
DSH_MARKER = "b04-isolated-marker-7f3a"
PI_MARKER = "盘位核对表"
MEM_MARKER = "I01-MEM-MARKER"

# Expected counts for the shipped fixtures (see tests/fixtures/i01/README.md).
EXPECT = {
    "hermes": {"messages": 9, "qa": 3},
    "dsh": {"messages": 6, "qa": 3},
    "pi": {"messages": 6, "qa": 3},
}


def log(key: str, value: object) -> None:
    print(f"{key}={value}", flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            out[str(p.relative_to(root))] = sha256_file(p)
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http_json(url: str, payload: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except Exception:
                return resp.status, {"_raw": body[:2000]}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"_raw": body[:2000]}
    except Exception as exc:
        return 0, {"_unreachable": f"{type(exc).__name__}: {exc}"}


def wait_http(url: str, seconds: float = 40.0) -> dict:
    deadline = time.time() + seconds
    last: dict = {"_unreachable": "not attempted"}
    while time.time() < deadline:
        status, payload = http_json(url, timeout=5.0)
        if status == 200:
            return payload
        last = payload
        time.sleep(0.5)
    return {"_unreachable": last.get("_unreachable") or f"last status={last}"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="fresh, empty run root")
    parser.add_argument("--source-root", required=True, help="i01-src checkout root")
    parser.add_argument("--env-python", required=True, help="isolated venv python.exe")
    parser.add_argument("--secrets", required=True)
    parser.add_argument("--db-name", required=True)
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=55432)
    parser.add_argument("--pg-user", default="f2e2e")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    source = Path(args.source_root).resolve()
    out_path = Path(args.out).resolve()
    python = str(Path(args.env_python).resolve())

    report: dict = {
        "scope": "I01 isolated integration; real host formats + disposable PG; "
                 "keyword-only core; no LLM; no embedding; no external endpoint",
        "root": str(root),
        "checks": {},
        "failures": [],
    }
    reasons: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        report["checks"][name] = {"ok": bool(ok), "observed": detail}
        if not ok:
            reasons.append(f"{name}: {detail}")
            report["failures"].append({"check": name, "observed": detail})

    # ── 0. gates ───────────────────────────────────────────────────────────
    machine = (os.environ.get("COMPUTERNAME") or socket.gethostname()).strip().upper()
    log("HOST", machine)
    if machine != HOST_GATE:
        print(f"REFUSE: hostname {machine!r} is not {HOST_GATE!r}", file=sys.stderr)
        return 91
    report["host"] = machine
    if args.pg_host != "127.0.0.1" or args.pg_port != 55432:
        print("REFUSE: only 127.0.0.1:55432 is allowed", file=sys.stderr)
        return 92
    if not args.db_name.startswith("i01e2e_"):
        print("REFUSE: --db-name must start with 'i01e2e_'", file=sys.stderr)
        return 93
    if not root.is_dir():
        print(f"REFUSE: --root {root} must exist and be empty", file=sys.stderr)
        return 94
    if any(root.iterdir()):
        print(f"REFUSE: --root {root} is not empty", file=sys.stderr)
        return 95

    # ── 1. secrets (env only) ──────────────────────────────────────────────
    secret = Path(args.secrets).read_text(encoding="utf-8").strip()
    if not secret:
        print("REFUSE: secrets file is empty", file=sys.stderr)
        return 96
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}
    env["PGPASSWORD"] = secret
    env["V3CORE_PG_PASSWORD"] = secret
    report["credentials"] = {"keys_loaded": ["PGPASSWORD"], "printed": False, "in_argv": False}

    # ── 2. isolated roots ──────────────────────────────────────────────────
    home = root / "home"
    project = root / "project"
    runs = root / "runs"
    profile_dir = root / "profile"
    data_dir = root / "data"
    for d in (home, project, runs, profile_dir, data_dir):
        d.mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "V3CORE_HOME": str(home), "V3CORE_TEST_MODE": "1",
        "PYTHONPATH": "", "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
    })
    report["roots"] = {
        "home": str(home), "project": str(project), "data": str(data_dir),
        "isolated_from_production": True,
    }

    fixtures = source / "src" / "v3-core" / "tests" / "fixtures" / "i01"
    if not fixtures.is_dir():
        print(f"REFUSE: fixtures not found under {fixtures}", file=sys.stderr)
        return 97

    # ── 3. host data staging (default discovery locations) ────────────────
    # hermes: synthetic state.db on the real schema, generated in place so the
    # platform-default path (USERPROFILE/AppData/Local/hermes) is what discovery
    # actually exercises.
    hermes_dir = home / "AppData" / "Local" / "hermes"
    hermes_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fixtures / "hermes" / "make_fixture.py", hermes_dir / "make_fixture.py")
    gen = subprocess.run([python, "make_fixture.py"], cwd=str(hermes_dir), env=env,
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    check("hermes_fixture_generated", gen.returncode == 0 and (hermes_dir / "state.db").is_file(),
          f"rc={gen.returncode}; out={gen.stdout.strip()[:120]}")

    # pi: the official-SDK session, placed at the platform-default root.
    pi_src = fixtures / "pi" / "--C--work--migration-project--"
    pi_dst = home / ".pi" / "agent" / "sessions" / "--C--work--migration-project--"
    pi_dst.mkdir(parents=True, exist_ok=True)
    pi_files = sorted(pi_src.glob("*.jsonl"))
    for f in pi_files:
        shutil.copy2(f, pi_dst / f.name)
    check("pi_fixture_staged", bool(pi_files), f"copied {len(pi_files)} pi session file(s)")

    # dsh: the real sessions produced by the installed DSH CLI (B04 home),
    # byte-compared against the shipped fixture copy below.
    dsh_src = fixtures / "dsh"
    dsh_root = home / ".dsh" / "sessions"
    dsh_files: list[Path] = []
    for f in sorted(dsh_src.rglob("*.jsonl.zstd")):
        rel = f.relative_to(dsh_src)
        # The shipped fixture carries the real `sessions/` layer; strip it —
        # dsh_root already IS the sessions directory.
        if rel.parts and rel.parts[0] == "sessions":
            rel = Path(*rel.parts[1:])
        target = dsh_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        dsh_files.append(target)
    check("dsh_fixture_staged", len(dsh_files) == 2, f"copied {len(dsh_files)} dsh session file(s)")

    # dsh fixture provenance cross-check: the shipped fixture bytes must equal
    # the original B04-home files on this machine (fixture was not altered).
    b04_home = Path(r"C:\hp-testbed\b04-integration-20261002\dsh-home\sessions")
    if b04_home.is_dir():
        match = 0
        total = 0
        for f in sorted(b04_home.rglob("*.jsonl.zstd")):
            total += 1
            for staged in dsh_files:
                if staged.name == f.name and sha256_file(staged) == sha256_file(f):
                    match += 1
        report["dsh_provenance"] = {"b04_files": total, "byte_identical": match}
        check("dsh_fixture_byte_identical_to_b04_originals", total > 0 and match == total,
              f"{match}/{total} files byte-identical to C:\\hp-testbed\\b04-integration-20261002\\dsh-home")
    else:
        report["dsh_provenance"] = {"b04_files": 0, "note": "b04 home not present; skipped cross-check"}

    # memory-md: a user-curated notes file at the import cwd.
    (project / "MEMORY.md").write_text(
        "# MEMORY\n\n"
        f"- {MEM_MARKER}: 用户的存储迁移项目内部代号是「银杏」。\n"
        "- 盘位核对表保存在 NAS 的共享目录里。\n",
        encoding="utf-8",
    )

    # snapshot hashes of every staged source file (must not change)
    before = tree_hashes(home) | tree_hashes(project)
    report["source_snapshot"] = {"files": len(before), "sha256": before}

    # ── 4. isolated core config (keyword-only, credential-free) ───────────
    config_path = profile_dir / "config.yaml"
    config_path.write_text(
        "basePath: %s\n"
        "mode: cloud\n"
        "storage:\n"
        "  pg:\n    host: %s\n    port: %d\n    database: %s\n    user: %s\n"
        "  embed:\n    endpoint: ''\n    model: ''\n    dim: 1024\n"
        "    apiKey: ''\n    api_key: ''\n    proxy: ''\n"
        "  rerank:\n    endpoint: ''\n    model: ''\n    proxy: ''\n    timeout: 30\n"
        "    apiKey: ''\n    api_key: ''\n"
        "e1:\n  enabled: false\nobserver:\n  enabled: false\ntkg:\n  enabled: false\n"
        "llm:\n  provider: ''\n  model: ''\n  api_key: ''\n  base_url: ''\n  thinking: true\n  max_tokens: 0\n"
        % (json.dumps(str(data_dir)), args.pg_host, args.pg_port, args.db_name, args.pg_user),
        encoding="utf-8",
    )
    env["V3CORE_CONFIG"] = str(config_path)
    report["core_config"] = {"path": str(config_path), "embedding_mode": "keyword_only_disabled",
                             "credential_fields_empty": True}

    # confirm the isolated venv resolves the I01 source (not any other copy)
    which = subprocess.run([python, "-c", "import v3core;print(v3core.__file__)"],
                           env=env, capture_output=True, text=True, timeout=60)
    core_file = (which.stdout or "").strip()
    report["v3core_resolved"] = core_file
    check("v3core_resolves_to_i01_source", str(source) in core_file,
          f"import v3core -> {core_file}")

    # ── 5. fresh database + bootstrap ─────────────────────────────────────
    import psycopg2  # noqa: PLC0415 (after env set-up)

    admin = psycopg2.connect(host=args.pg_host, port=args.pg_port, database="postgres",
                             user=args.pg_user, password=secret, connect_timeout=10)
    try:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (args.db_name,))
            if cur.fetchone() is not None:
                print(f"REFUSE: database {args.db_name!r} already exists", file=sys.stderr)
                return 98
            cur.execute(f'CREATE DATABASE "{args.db_name}"')
    finally:
        admin.close()
    report["db_created_fresh"] = True

    dsn = f"postgresql://{args.pg_user}@{args.pg_host}:{args.pg_port}/{args.db_name}"  # no password
    boot = subprocess.run([python, "-m", "v3core.distribution_cli", "bootstrap", "--target", dsn],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, cwd=str(project), timeout=300)
    try:
        payload = json.loads(boot.stdout)
    except Exception:
        payload = {}
    inner = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    report["bootstrap"] = {"returncode": boot.returncode, "applied": inner.get("applied"),
                           "stdout_tail": (boot.stdout or "")[-300:]}
    check("schema_bootstrapped", boot.returncode == 0 and inner.get("applied") is True,
          f"rc={boot.returncode}, applied={inner.get('applied')}")
    if not report["checks"]["schema_bootstrapped"]["ok"]:
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return 1

    def pg_rows(sql: str, params: tuple = ()) -> list[tuple]:
        conn = psycopg2.connect(host=args.pg_host, port=args.pg_port, database=args.db_name,
                                user=args.pg_user, password=secret, connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            conn.close()

    def run_import(tag: str, *, dry_run: bool) -> dict:
        argv = [python, "-m", "v3core.distribution_cli", "import", "auto", "--json",
                "--profile-dir", str(profile_dir)]
        if dry_run:
            argv.append("--dry-run")
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, cwd=str(project), timeout=600)
        (runs / f"import-{tag}.stdout.json").write_text(proc.stdout or "", encoding="utf-8")
        (runs / f"import-{tag}.stderr.txt").write_text(proc.stderr or "", encoding="utf-8")
        try:
            parsed = json.loads(proc.stdout)
        except Exception:
            parsed = {"_unparsed": (proc.stdout or "")[:2000]}
        parsed["_rc"] = proc.returncode
        return parsed

    # ── 6. dry-run (zero writes) ──────────────────────────────────────────
    dry = run_import("dry-run", dry_run=True)
    report["dry_run_report"] = dry
    check("dry_run_rc0", dry.get("_rc") == 0, f"rc={dry.get('_rc')}")
    dry_sources = {s.get("host"): s for s in (dry.get("sources") or [])}
    for host, want in EXPECT.items():
        s = dry_sources.get(host) or {}
        check(f"dry_run_found_{host}", bool(s.get("found")),
              f"found={s.get('found')} root={s.get('root')} reason={s.get('reason')}")
        check(f"dry_run_{host}_messages", s.get("messages") == want["messages"],
              f"messages={s.get('messages')} (expected {want['messages']})")
    mem = dry_sources.get("memory-md") or {}
    check("dry_run_found_memory-md", bool(mem.get("found")),
          f"found={mem.get('found')} root={mem.get('root')}")
    check("dry_run_zero_raw_written", (dry.get("totals") or {}).get("raw_imported") == 0,
          f"raw_imported={((dry.get('totals') or {}).get('raw_imported'))}")

    counts_after_dry = pg_rows("SELECT count(*) FROM conversation_stream")[0][0]
    check("dry_run_wrote_nothing", counts_after_dry == 0,
          f"conversation_stream rows after dry-run = {counts_after_dry}")

    # ── 7. live import ────────────────────────────────────────────────────
    live = run_import("live-1", dry_run=False)
    report["live_report"] = live
    check("live_rc0", live.get("_rc") == 0, f"rc={live.get('_rc')}")
    totals = live.get("totals") or {}
    check("live_raw_imported", totals.get("raw_imported") == 21,
          f"raw_imported={totals.get('raw_imported')} (expected 21 = 9+6+6)")
    check("live_qa_pairs_derived", totals.get("qa_pairs_derived") == 9,
          f"qa_pairs_derived={totals.get('qa_pairs_derived')} (expected 9 = 3+3+3)")

    # SQL verification — raw stream by host
    for host, want in EXPECT.items():
        n = pg_rows("SELECT count(*) FROM conversation_stream WHERE host=%s", (host,))[0][0]
        check(f"stream_rows_{host}", n == want["messages"], f"conversation_stream rows = {n}")
    identity_null = pg_rows(
        "SELECT count(*) FROM conversation_stream WHERE host IS NULL OR event_id IS NULL OR event_id=''"
    )[0][0]
    check("native_identity_present", identity_null == 0,
          f"rows with missing host/event_id = {identity_null}")
    # native ids preserved verbatim (spot checks from the fixtures)
    pi_native = pg_rows(
        "SELECT content FROM conversation_stream WHERE host='pi' AND event_id=%s", ("9921d832",))
    check("pi_native_event_id", bool(pi_native),
          "pi row with event_id='9921d832' (native entry id) found" if pi_native else "not found")
    dsh_native = pg_rows(
        "SELECT count(*) FROM conversation_stream WHERE host='dsh' AND event_id ~ %s",
        ("^[0-9a-f]{8}-[0-9a-f]{4}-",))
    check("dsh_native_event_ids", dsh_native[0][0] == 6,
          f"dsh rows with uuid event_id = {dsh_native[0][0]}")

    # QA pairs
    qa_total = pg_rows("SELECT count(*) FROM qa_pairs")[0][0]
    check("qa_pairs_total", qa_total == 9, f"qa_pairs rows = {qa_total}")
    qa_fmt = pg_rows(
        "SELECT count(*) FROM qa_pairs WHERE source_id LIKE 'qa_import/%%'")[0][0]
    check("qa_source_id_format", qa_fmt == 9, f"qa_import/ prefixed source_ids = {qa_fmt}")
    for marker, host in ((HERMES_MARKER, "hermes"), (DSH_MARKER, "dsh"), (PI_MARKER, "pi")):
        n = pg_rows(
            "SELECT count(*) FROM qa_pairs WHERE source_id LIKE %s AND (question LIKE %s OR answer LIKE %s)",
            (f"qa_import/{host}/%%", f"%{marker}%", f"%{marker}%"),
        )[0][0]
        check(f"qa_content_{host}", n >= 1, f"{host} QA rows containing {marker!r} = {n}")

    # memory-md curated notes -> explicit_memories (user_curated channel)
    mem_live = next((s for s in (live.get("sources") or []) if s.get("host") == "memory-md"), {})
    report["memory_md_live"] = {"user_curated_imported": mem_live.get("user_curated_imported"),
                                "errors": mem_live.get("errors")}
    check("memory_md_curated_written", (mem_live.get("user_curated_imported") or 0) >= 1,
          f"user_curated_imported={mem_live.get('user_curated_imported')} errors={mem_live.get('errors')}")
    mem_rows = pg_rows("SELECT count(*) FROM explicit_memories WHERE content LIKE %s",
                       (f"%{MEM_MARKER}%",))[0][0]
    check("memory_md_row_in_explicit_memories", mem_rows >= 1,
          f"explicit_memories rows containing {MEM_MARKER!r} = {mem_rows}")

    # self-memory exclusion: injected memory text must NOT appear in the stream
    injected = pg_rows(
        "SELECT count(*) FROM conversation_stream WHERE content LIKE %s OR content LIKE %s",
        ("%[长期记忆]%", "%hippocampus-memory%"),
    )[0][0]
    check("self_memory_excluded", injected == 0,
          f"rows containing injected-memory markers = {injected}")

    # ── 8. rerun idempotency ──────────────────────────────────────────────
    stream_before = pg_rows("SELECT count(*) FROM conversation_stream")[0][0]
    qa_before = pg_rows("SELECT count(*) FROM qa_pairs")[0][0]
    rerun = run_import("live-2", dry_run=False)
    report["rerun_report"] = rerun
    stream_after = pg_rows("SELECT count(*) FROM conversation_stream")[0][0]
    qa_after = pg_rows("SELECT count(*) FROM qa_pairs")[0][0]
    check("rerun_stream_unchanged", stream_after == stream_before,
          f"conversation_stream {stream_before} -> {stream_after}")
    check("rerun_qa_unchanged", qa_after == qa_before,
          f"qa_pairs {qa_before} -> {qa_after}")
    rerun_totals = rerun.get("totals") or {}
    check("rerun_deduped_reported", (rerun_totals.get("duplicates_skipped") or 0) >= 30,
          f"rerun duplicates_skipped={rerun_totals.get('duplicates_skipped')} (expected >= 30)")

    # ── 9. new-session recall via the bridge ──────────────────────────────
    bridge_port = free_port()
    bridge_log = (runs / "bridge.log").open("wb")
    bridge = subprocess.Popen(
        [python, "-B", "-m", "v3core", "serve", "--host", "127.0.0.1", "--port", str(bridge_port)],
        env=env, cwd=str(project), stdout=bridge_log, stderr=subprocess.STDOUT)
    try:
        health = wait_http(f"http://127.0.0.1:{bridge_port}/health")
        report["bridge"] = {"port": bridge_port,
                            "health": {k: health.get(k) for k in ("ok", "pg", "embed")}}
        check("bridge_health_ok", bool(health.get("ok")), f"health={health.get('ok')}")
        recall = {}
        for label, query in (("hermes", HERMES_MARKER), ("dsh", DSH_MARKER), ("pi", PI_MARKER)):
            status, payload = http_json(
                f"http://127.0.0.1:{bridge_port}/prefetch",
                {"query": query, "session_id": f"i01-new-session-{label}"})
            block = str(payload.get("block") or "")
            recall[label] = {"status": status, "chars": len(block), "hit": query in block}
            check(f"recall_{label}", query in block,
                  f"prefetch({query!r}) block chars={len(block)}, marker_present={query in block}")
        report["recall"] = recall
    finally:
        bridge.terminate()
        try:
            bridge.wait(timeout=15)
        except Exception:
            bridge.kill()
        bridge_log.close()

    # ── 10. source trace (qa_pairs.source_id -> conversation_stream) ──────
    row = pg_rows(
        "SELECT source_id, question FROM qa_pairs WHERE source_id LIKE 'qa_import/pi/%%' LIMIT 1")
    if row:
        source_id, question = row[0]
        parts = source_id.split("/")
        host, session_id, q_event_id = parts[1], parts[2], parts[3]
        traced = pg_rows(
            "SELECT content FROM conversation_stream WHERE host=%s AND session_id=%s AND event_id=%s",
            (host, session_id, q_event_id))
        ok = bool(traced) and str(traced[0][0]).strip() == str(question).strip()
        report["source_trace"] = {"source_id": source_id, "host": host,
                                  "session_id": session_id, "q_event_id": q_event_id,
                                  "question_matches_stream": ok}
        check("source_trace_complete", ok,
              f"{source_id} -> conversation_stream row matched verbatim={ok}")
    else:
        check("source_trace_complete", False, "no pi qa_pairs row to trace")

    # ── 11. source host files unchanged ───────────────────────────────────
    after = tree_hashes(home) | tree_hashes(project)
    changed = [k for k in before if before.get(k) != after.get(k)]
    added = [k for k in after if k not in before]
    report["source_unchanged"] = {"files": len(before), "changed": changed, "added": added}
    check("source_files_unchanged", not changed and not added,
          f"changed={changed[:5]} added={added[:5]}")

    # ── 12. verdict ───────────────────────────────────────────────────────
    report["reasons"] = reasons
    report["verdict"] = "PASS" if not reasons else "FAIL"
    report["check_count"] = len(report["checks"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log("CHECKS", len(report["checks"]))
    log("FAILURES", len(reasons))
    log("VERDICT", report["verdict"])
    return 0 if not reasons else 1


if __name__ == "__main__":
    sys.exit(main())
