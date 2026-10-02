#!/usr/bin/env python
"""B04 isolated integration driver (parent-owned acceptance instrument).

Proves, against a REAL installed deepseek-harness CLI on the dedicated test
laptop, that the Hippocampus DSH adapter:

  A. recalls into the SAME turn and the SAME model request  (pre-step, additive)
  B. captures the durable user + assistant messages automatically (session/event)
  C. does not duplicate a durable source after a reload of the same session
  D. does not recapture its own injected memory message

Everything runs in an isolated HOME / DSH_HOME / data dir against a
disposable PostgreSQL database and a credential-free keyword-only core
config. No production system, no real model credential: the model endpoint is
a local stub that speaks the Anthropic Messages streaming protocol, and that
fact is recorded in the report as the honest scope of this run.

Nothing is ever printed that could carry a password, and no secret is passed
through argv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOST_GATE = "DESKTOP-EQP3OBU"
SEED_MARKER = "b04-isolated-marker-7f3a"
SEED_QUESTION = f"What is the {SEED_MARKER} deploy window?"
SEED_ANSWER = f"The {SEED_MARKER} deploy window is on Thursdays at 02:00."
DSH_QUERY = f"Please confirm the {SEED_MARKER} deploy window."
STUB_REPLY = f"acknowledged {SEED_MARKER}"


def log(key: str, value: object) -> None:
    print(f"{key}={value}", flush=True)


def fail(key: str, detail: str = "") -> None:
    log(key, detail or "FAIL")
    raise SystemExit(1)


def refuse(detail: str) -> None:
    log("REFUSED", detail)
    raise SystemExit(2)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http_json(url: str, payload: dict | None = None, timeout: float = 20.0) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, {"_raw": body}
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {"_raw": body}
    except Exception as error:  # noqa: BLE001 - transport failures are data here
        return 0, {"_transport_error": type(error).__name__}


def wait_http(url: str, seconds: float = 40.0) -> dict:
    deadline = time.time() + seconds
    last: dict = {}
    while time.time() < deadline:
        status, payload = http_json(url, timeout=5.0)
        if status == 200:
            return payload
        last = payload
        time.sleep(0.5)
    return {"_unreachable": True, "last": last}


def scrub(text: str, secret: str) -> str:
    return text.replace(secret, "[REDACTED]") if secret else text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--support-root", required=True)
    parser.add_argument("--secrets", required=True)
    parser.add_argument("--env-root", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--dsh-home", required=True)
    parser.add_argument("--adapter", required=True, help="directory holding the adapter package")
    parser.add_argument("--stub", required=True, help="stub-model-server.mjs path")
    parser.add_argument("--db-name", required=True)
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=55432)
    parser.add_argument("--pg-user", default="f2e2e")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    support = Path(args.support_root).resolve()
    out_path = Path(args.out).resolve()
    report: dict = {"scope": "isolated integration; real DSH CLI + real adapter + real bridge + "
                             "disposable PG; model endpoint is a local stub (no model credential)",
                    "root": str(root), "checks": {}, "failures": []}
    reasons: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        # ``detail`` is the observed-state note for this check; it is written the
        # same way whether the check passed or failed, so a passing check never
        # looks like a failure in the report.
        report["checks"][name] = {"ok": bool(ok), "observed": detail}
        if not ok:
            reasons.append(f"{name}: {detail}")
            report["failures"].append({"check": name, "observed": detail})

    # ── 0. hostname gate ───────────────────────────────────────────────────
    machine = (os.environ.get("COMPUTERNAME") or socket.gethostname()).strip().upper()
    log("HOST", machine)
    if machine != HOST_GATE:
        refuse(f"hostname {machine!r} is not the dedicated test laptop {HOST_GATE!r}")
    report["host"] = machine

    # Closed target allowlist (same discipline as the B03 orchestrator): this
    # driver only ever touches the dedicated laptop's disposable PostgreSQL.
    if args.pg_host != "127.0.0.1" or args.pg_port != 55432:
        refuse(f"refusing to target {args.pg_host}:{args.pg_port}; only 127.0.0.1:55432 is allowed")
    if not args.db_name.startswith("b04dsh_"):
        refuse(f"--db-name must start with 'b04dsh_' (got {args.db_name!r})")

    if not root.is_dir():
        refuse(f"--root {root} must already exist and start empty")
    if any(root.iterdir()):
        refuse(f"--root {root} is not empty")

    # ── 1. secrets: process env only, never argv, never printed ────────────
    secrets_path = Path(args.secrets)
    if not secrets_path.is_file():
        refuse("secrets file missing")
    secret = secrets_path.read_text(encoding="utf-8").strip()

    def pg_rows(sql: str) -> list[tuple]:
        """Read-only query against the disposable database (never the secret)."""
        conn = psycopg2.connect(host=args.pg_host, port=args.pg_port, database=args.db_name,
                                user=args.pg_user, password=secret, connect_timeout=10)
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                return cursor.fetchall()
        finally:
            conn.close()
    if not secret:
        refuse("secrets file is empty")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}
    env["PGPASSWORD"] = secret
    env["V3CORE_PG_PASSWORD"] = secret
    report["credentials"] = {"keys_loaded": ["PGPASSWORD"], "printed": False, "in_argv": False}

    # ── 2. isolated roots ──────────────────────────────────────────────────
    home = root / "home"
    data_dir = root / "data"
    project = root / "project"
    runs = root / "runs"
    profile_dir = root / "profile"
    for directory in (home, data_dir, project, runs, profile_dir):
        directory.mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "V3CORE_HOME": str(home), "V3CORE_TEST_MODE": "1",
        "DSH_HOME": str(Path(args.dsh_home).resolve()),
        "PYTHONPATH": "", "PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
    })
    report["roots"] = {"home": str(home), "data": str(data_dir), "project": str(project),
                       "dsh_home": env["DSH_HOME"], "isolated_from_production": True}

    # ── 3. isolated core config: keyword-only, no external endpoint ────────
    config_path = profile_dir / "config.yaml"
    config_path.write_text(
        "basePath: %s\n"
        "mode: cloud\n"
        "storage:\n"
        "  pg:\n    host: %s\n    port: %d\n    database: %s\n    user: %s\n"
        "  embed:\n    endpoint: ''\n    model: ''\n    dim: 1024\n    apiKey: ''\n    api_key: ''\n    proxy: ''\n"
        "  rerank:\n    endpoint: ''\n    model: ''\n    proxy: ''\n    timeout: 30\n    apiKey: ''\n    api_key: ''\n"
        "e1:\n  enabled: false\nobserver:\n  enabled: false\ntkg:\n  enabled: false\n"
        "llm:\n  provider: ''\n  model: ''\n  api_key: ''\n  base_url: ''\n  thinking: true\n  max_tokens: 0\n"
        % (json.dumps(str(data_dir)), args.pg_host, args.pg_port, args.db_name, args.pg_user),
        encoding="utf-8")
    env["V3CORE_CONFIG"] = str(config_path)
    report["core_config"] = {"path": str(config_path), "embedding_mode": "keyword_only_disabled",
                             "credential_fields_empty": True}

    # ── 4. one fresh database; never reuse, never drop ─────────────────────
    import psycopg2
    connection = psycopg2.connect(host=args.pg_host, port=args.pg_port, database="postgres",
                                  user=args.pg_user, password=secret, connect_timeout=10)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname=%s", (args.db_name,))
            if cursor.fetchone() is not None:
                refuse(f"database {args.db_name!r} already exists; supply a fresh name")
            cursor.execute(f'CREATE DATABASE "{args.db_name}"')
    finally:
        connection.close()
    report["db_created_fresh"] = True

    # ── 4b. schema bootstrap through the installed CLI (no password in argv) ─
    cli = Path(args.env_root) / "Scripts" / "hippocampus.exe"
    if not cli.is_file():
        fail("BOOTSTRAP_CLI_MISSING", f"{cli} not found")
    dsn = f"postgresql://{args.pg_user}@{args.pg_host}:{args.pg_port}/{args.db_name}"  # no password
    boot = subprocess.run([str(cli), "bootstrap", "--target", dsn], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env, cwd=str(root),
                          timeout=300)
    try:
        payload = json.loads(boot.stdout)
    except Exception:  # noqa: BLE001 - a non-JSON status is itself the failure
        payload = {}
    inner = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    target = inner.get("target") if isinstance(inner.get("target"), dict) else {}
    report["bootstrap"] = {
        "argv": ["hippocampus", "bootstrap", "--target", dsn],
        "returncode": boot.returncode,
        "applied": inner.get("applied"),
        "sql_sha256": payload.get("alpha_bootstrap_sql_sha256"),
        "target_matches_guarded": (target.get("host") == args.pg_host
                                   and int(target.get("port") or 0) == args.pg_port
                                   and target.get("database") == args.db_name),
        "password_redacted_by_cli": target.get("password") == "***",
    }
    check("schema_bootstrapped",
          boot.returncode == 0 and inner.get("applied") is True
          and report["bootstrap"]["target_matches_guarded"],
          f"CLI bootstrap rc={boot.returncode}, applied={inner.get('applied')}, "
          f"guarded_target={report['bootstrap']['target_matches_guarded']}")
    if not report["checks"]["schema_bootstrapped"]["ok"]:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report["verdict"] = "FAIL"
        report["reasons"] = reasons
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        fail("SCHEMA_MISSING", "the fresh database has no schema to read back; run stops here")

    python = str(Path(args.env_root) / "Scripts" / "python.exe")
    node = str(Path(args.node))

    # ── 5. bridge (the frozen B01 contract) ────────────────────────────────
    bridge_port = free_port()
    bridge_log = (runs / "bridge.log").open("wb")
    bridge = subprocess.Popen([python, "-B", "-m", "v3core", "serve",
                               "--host", "127.0.0.1", "--port", str(bridge_port)],
                              env=env, cwd=str(root), stdout=bridge_log, stderr=subprocess.STDOUT)
    health = wait_http(f"http://127.0.0.1:{bridge_port}/health")
    report["bridge"] = {"port": bridge_port, "health": {k: health.get(k) for k in ("ok", "pg", "embed")}}
    if health.get("_unreachable"):
        fail("BRIDGE_UNREACHABLE", "bridge did not answer /health")
    check("bridge_health_ok", bool(health.get("ok")), f"health={health.get('ok')}")
    check("bridge_embed_disabled", health.get("embed") is False,
          f"embed={health.get('embed')} (keyword-only scope requires false)")

    # ── 6. stub model endpoint (no credential anywhere) ────────────────────
    stub_port = free_port()
    record = runs / "stub-requests.jsonl"
    stub_log = (runs / "stub.log").open("wb")
    stub = subprocess.Popen([node, str(Path(args.stub)), "--port", str(stub_port),
                             "--record", str(record), "--text", STUB_REPLY],
                            env=env, cwd=str(root), stdout=stub_log, stderr=subprocess.STDOUT)
    stub_health = wait_http(f"http://127.0.0.1:{stub_port}/health", seconds=30)
    if stub_health.get("_unreachable"):
        fail("STUB_UNREACHABLE", "stub model server did not answer /health")
    report["stub"] = {"port": stub_port, "record": str(record), "credential_free": True}

    # ── 7. seed one fact through the real ingest path ──────────────────────
    seed_events = [
        {"host": "dsh", "session_id": "b04-seed-session", "event_id": "b04-seed-1",
         "role": "user", "content": SEED_QUESTION, "turn_id": "1"},
        {"host": "dsh", "session_id": "b04-seed-session", "event_id": "b04-seed-2",
         "role": "assistant", "content": SEED_ANSWER, "turn_id": "1"},
        # v3core flushes a user→assistant pair when the NEXT user message of the
        # same session arrives (sync_turn pairing rule, __init__.py:3548-3551).
        # A single-turn fixture would stay pending forever, so the fixture closes
        # its turn exactly the way a real session does.
        {"host": "dsh", "session_id": "b04-seed-session", "event_id": "b04-seed-3",
         "role": "user", "content": "Thanks, noted.", "turn_id": "2"},
    ]
    seed_status = []
    for event in seed_events:
        status, payload = http_json(f"http://127.0.0.1:{bridge_port}/events", event)
        seed_status.append({"event_id": event["event_id"], "status": status,
                            "ack": payload.get("status") or payload.get("ok")})
    report["seed"] = {"marker": SEED_MARKER, "events": seed_status}
    check("seed_ingested", all(item["status"] == 200 for item in seed_status),
          f"seed statuses={[item['status'] for item in seed_status]}")

    seeded_pairs = pg_rows("SELECT count(*) FROM qa_pairs WHERE question LIKE %s"
                           % ("'%" + SEED_MARKER + "%'"))
    report["seed"]["qa_pairs_with_marker"] = int(seeded_pairs[0][0]) if seeded_pairs else -1
    check("seed_pair_committed", bool(seeded_pairs) and int(seeded_pairs[0][0]) >= 1,
          f"qa_pairs rows carrying the marker = {report['seed']['qa_pairs_with_marker']}")

    status, prefetch = http_json(f"http://127.0.0.1:{bridge_port}/prefetch",
                                 {"query": DSH_QUERY, "session_id": "b04-seed-session"})
    block = str(prefetch.get("block") or "")
    report["seed_prefetch"] = {"status": status, "chars": len(block), "has_marker": SEED_MARKER in block}
    check("seed_recallable", SEED_MARKER in block,
          f"keyword prefetch returned {len(block)} chars, marker_present={SEED_MARKER in block}")

    # ── 8. mount the adapter into the DSH headless profile ─────────────────
    # The installed CLI owns the profile template: materialize it FIRST (a bare
    # --help creates $DSH_HOME/profiles/<name>/ from the shipped layer), then
    # write the user patch, so the template can never overwrite our row.
    dsh_pkg = Path(args.dsh_home).parent / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
    if not dsh_pkg.is_file():
        fail("DSH_NOT_INSTALLED", f"{dsh_pkg} not found")
    package = json.loads(dsh_pkg.read_text(encoding="utf-8"))
    bin_field = package.get("bin")
    entry = bin_field if isinstance(bin_field, str) else (bin_field or {}).get("dsh")
    if not entry:
        fail("DSH_BIN_MISSING", "installed package declares no dsh bin")
    dsh_bin = (dsh_pkg.parent / entry).resolve()
    report["dsh"] = {"version": package.get("version"), "package_json": str(dsh_pkg),
                     "bin": str(dsh_bin)}

    env["NO_COLOR"] = "1"
    materialize = subprocess.run([node, str(dsh_bin), "--profile", "headless", "--help"],
                                 env=env, cwd=str(project), capture_output=True, text=True,
                                 timeout=300, encoding="utf-8", errors="replace")
    report["profile_materialize"] = {"rc": materialize.returncode}

    dsh_home = Path(env["DSH_HOME"])
    profile = dsh_home / "profiles" / "headless"
    if not profile.is_dir():
        fail("PROFILE_MISSING", f"{profile} was not materialized by the installed CLI")
    adapter_dir = profile / "hippocampus-adapter"
    if adapter_dir.exists():
        refuse(f"{adapter_dir} already exists; the run root must be pristine")
    import shutil
    shutil.copytree(Path(args.adapter), adapter_dir)
    patch_path = profile / "cordis.patch.yml"
    existing = patch_path.read_text(encoding="utf-8") if patch_path.is_file() else ""
    # The shipped template materializes as an empty list ("[]", possibly with
    # comments). Replace ONLY that empty list; anything else is somebody's real
    # overlay and is never clobbered.
    stripped = "\n".join(line for line in existing.splitlines()
                         if line.strip() and not line.strip().startswith("#")).strip()
    if stripped not in ("", "[]"):
        refuse(f"{patch_path} already carries a non-empty overlay; refusing to clobber it")
    patch_path.write_text(
        "# B04 isolated integration: mounts the Hippocampus automatic-memory adapter.\n"
        "- insert:\n"
        "    - id: hippocampus\n"
        "      name: './hippocampus-adapter/src/index.js'\n"
        "      config:\n"
        "        enabled: true\n"
        "        mode: external\n"
        f"        bridgeUrl: 'http://127.0.0.1:{bridge_port}'\n"
        "        timeoutMs: 20000\n"
        "        memoryBudgetChars: 8000\n",
        encoding="utf-8")
    report["mount"] = {"patch": str(patch_path), "adapter_dir": str(adapter_dir),
                       "replaced_empty_template": True}

    # The composed tree is the proof that the row is really mounted.
    dump = subprocess.run([node, str(dsh_bin), "--profile", "headless", "--dump-config"],
                          env=env, cwd=str(project), capture_output=True, text=True,
                          timeout=300, encoding="utf-8", errors="replace")
    (runs / "dump-config.txt").write_text(dump.stdout or "", encoding="utf-8")
    report["mount"]["dump_config_rc"] = dump.returncode
    report["mount"]["dump_mentions_adapter"] = "hippocampus" in (dump.stdout or "")
    check("adapter_row_mounted", "hippocampus" in (dump.stdout or ""),
          f"dump-config mentions the adapter row = {'hippocampus' in (dump.stdout or '')}")

    # ── 9. real DSH runs ───────────────────────────────────────────────────
    env["DEEPSEEK_BASE_URL"] = f"http://127.0.0.1:{stub_port}"
    env["DEEPSEEK_API_KEY"] = "stub-local-not-a-credential"

    def run_dsh(tag: str, task: str, session_id: str | None) -> tuple[int, str, str]:
        argv = [node, str(dsh_bin), "--profile", "headless", "--json"]
        if session_id:
            argv += ["--session-id", session_id]
        argv.append(task)
        completed = subprocess.run(argv, env=env, cwd=str(project), capture_output=True, text=True,
                                   timeout=300, encoding="utf-8", errors="replace")
        (runs / f"{tag}.stdout.jsonl").write_text(completed.stdout or "", encoding="utf-8")
        (runs / f"{tag}.stderr.txt").write_text(scrub(completed.stderr or "", secret), encoding="utf-8")
        return completed.returncode, completed.stdout or "", completed.stderr or ""

    code1, stdout1, stderr1 = run_dsh("run1", DSH_QUERY, None)
    report["run1"] = {"rc": code1, "stdout_chars": len(stdout1),
                      "stderr_tail": scrub(stderr1[-400:], secret)}
    check("run1_exit_zero", code1 == 0, f"rc={code1}; stderr tail={scrub(stderr1[-300:], secret)}")

    session_id = ""
    for line in stdout1.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidate = event.get("sessionId") or event.get("session_id")
        if isinstance(candidate, str) and candidate:
            session_id = candidate
        data = event.get("data")
        if isinstance(data, dict):
            inner = data.get("sessionId") or data.get("session_id")
            if isinstance(inner, str) and inner:
                session_id = inner
    report["session_id"] = session_id
    check("run1_session_id", bool(session_id), f"session_id={session_id or '<none>'}")

    # B. capture: the durable user + assistant messages reached the bridge
    qid = "'" + session_id.replace("'", "''") + "'"
    rows = pg_rows("SELECT role, event_id, host FROM conversation_stream "
                   "WHERE session_id = %s ORDER BY id" % qid) if session_id else []
    roles = sorted({row[0] for row in rows})
    report["capture"] = {"rows": len(rows), "roles": roles,
                         "event_ids": [row[1] for row in rows],
                         "hosts": sorted({row[2] for row in rows})}
    check("capture_user_and_assistant", set(roles) >= {"user", "assistant"},
          f"captured roles={roles} rows={len(rows)}")
    check("capture_used_native_event_id", any(re.fullmatch(r"[0-9a-fA-F-]{36}", str(row[1] or "")) for row in rows),
          f"native event ids={[row[1] for row in rows]}")

    # D. the injected memory message must not be recaptured as user content.
    # conversation_stream carries no source_id column (schema: alpha_bootstrap.sql:146-183),
    # so the discriminator is the injected text itself plus the fact that every
    # captured row must carry a native DSH event id.
    memory_rows = pg_rows("SELECT count(*) FROM conversation_stream WHERE session_id = %s "
                          "AND content LIKE '%%hippocampus memory%%'"
                          % qid) if session_id else []
    report["self_recapture"] = {"rows_containing_memory_text":
                                int(memory_rows[0][0]) if memory_rows else -1,
                                "columns_checked": ["session_id", "content", "host", "event_id"]}
    check("injected_memory_not_recaptured",
          bool(memory_rows) and memory_rows[0][0] == 0,
          f"rows containing the injected memory text={memory_rows[0][0] if memory_rows else 'n/a'}")

    # C. reload the same session: no duplicate durable source
    before = len(rows)
    if session_id:
        code2, stdout2, stderr2 = run_dsh("run2", "Continue.", session_id)
        rows_after = pg_rows("SELECT role, event_id FROM conversation_stream WHERE session_id = %s ORDER BY id"
                             % qid)
    else:
        code2, stderr2, rows_after = 1, "no session id; reload was not attempted", []
    duplicated = len(rows_after) - len({(row[0], row[1]) for row in rows_after})
    report["reload"] = {"rc": code2, "rows_before": before, "rows_after": len(rows_after),
                        "duplicate_identity_rows": duplicated}
    check("reload_exit_zero", code2 == 0, f"rc={code2}; stderr tail={scrub(stderr2[-300:], secret)}")
    check("reload_no_duplicate_source", duplicated == 0,
          f"rows {before}->{len(rows_after)}, duplicate (role,event_id) rows={duplicated}")

    # ── 10. teardown ───────────────────────────────────────────────────────
    for process in (stub, bridge):
        try:
            process.terminate()
            process.wait(timeout=15)
        except Exception:  # noqa: BLE001 - teardown best effort, recorded below
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
    bridge_log.close()
    stub_log.close()
    report["teardown"] = {"bridge_stopped": bridge.poll() is not None,
                          "stub_stopped": stub.poll() is not None}

    # A. the injected memory reached the model request in the same turn.
    # Read only AFTER teardown: the stub's record is append-only, so the counts
    # below describe the completed run rather than a mid-flight snapshot.
    stub_records = []
    if record.is_file():
        for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                stub_records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    matched = [item.get("matchedText", "") for item in stub_records if item.get("textPresent")]
    report["model_request"] = {"requests": len(stub_records),
                               "requests_with_memory": sum(1 for i in stub_records if i.get("textPresent")),
                               "matched_has_marker": any(SEED_MARKER in text for text in matched),
                               "matched_chars": [len(text) for text in matched]}
    check("memory_in_same_request", bool(matched),
          f"{len(matched)} of {len(stub_records)} model requests carried a memory block")
    check("memory_carries_seeded_fact", any(SEED_MARKER in text for text in matched),
          f"seeded marker present in the injected block = {any(SEED_MARKER in t for t in matched)}")

    report["verdict"] = "PASS" if not reasons else "FAIL"
    report["reasons"] = reasons
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log("VERDICT", report["verdict"])
    log("OUT", str(out_path))
    return 0 if not reasons else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as error:  # noqa: BLE001 - a crash must still leave a record
        argv = sys.argv
        out = argv[argv.index("--out") + 1] if "--out" in argv else ""
        payload = {"verdict": "CRASH", "error_type": type(error).__name__, "out": out}
        if out:
            try:
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                Path(out).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        log("CRASH", f"{type(error).__name__}: {error}")
        sys.exit(3)
