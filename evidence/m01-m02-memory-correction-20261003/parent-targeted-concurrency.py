#!/usr/bin/env python3
"""Bounded M01 concurrency-closure probe (SCRATCH — not shipped).

Reproduces ONLY the concurrent-identical-correction seam of the M01/M02
integration driver, on a disposable PostgreSQL, through the REAL public HTTP
``POST /tool`` ``v3_update`` surface. It reuses the driver's fail-closed setup
helpers (gates, isolated roots/env, Server, HTTP helpers) but runs NO other
driver case.

Two server launches are supported:
  * canonical   (--instrument-parent omitted): the unmodified source under
    --source-root; proves the before-fix RED.
  * instrumented (--instrument-parent <dir>): a staged v3core package on
    PYTHONPATH whose active_memory_store.py carries scratch-only JSONL
    diagnostics (thread id / backend pid / autocommit / transaction status /
    exception class / SQLSTATE). Same logic, extra observability.

A ``sitecustomize.py`` on PYTHONPATH logs every psycopg2.connect with its
backend pid and thread id, so the number of physical connections the serve
process actually opens is observed, not assumed.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg2


def load_driver(path: Path):
    spec = importlib.util.spec_from_file_location("m01m02_driver", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--env-python", required=True)
    ap.add_argument("--secrets", required=True)
    ap.add_argument("--db-name", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--driver", required=True)
    ap.add_argument("--instrument-parent", default="")
    ap.add_argument("--label", default="canonical")
    ap.add_argument("--pg-host", default="127.0.0.1")
    ap.add_argument("--pg-port", type=int, default=55432)
    ap.add_argument("--pg-user", default="f2e2e")
    args = ap.parse_args()

    D = load_driver(Path(args.driver).resolve())
    root = Path(args.root).resolve()
    source = Path(args.source_root).resolve()
    out_path = Path(args.out).resolve()
    python = str(Path(args.env_python).resolve())
    db_name = (args.db_name or "").strip()
    instr_parent = Path(args.instrument_parent).resolve() if args.instrument_parent else None

    report: dict = {"label": args.label, "checks": {}, "reasons": []}

    def rec(name, ok, detail=""):
        report["checks"][name] = {"ok": bool(ok), "observed": detail}
        if not ok:
            report["reasons"].append(f"{name}: {detail}")
        print(f"CHECK {'PASS' if ok else 'FAIL'} {name} :: {detail}", flush=True)
        return bool(ok)

    # ── fail-closed gates (reused verbatim from the driver) ─────────────────
    machine = (os.environ.get("COMPUTERNAME") or "").strip().upper()
    if not Path(python).is_file():
        return _refuse(report, out_path, "env_python", f"{python} missing")
    for label, reason in (
        ("hostname", D.gate_hostname(machine)),
        ("pg_target", D.gate_pg_target(args.pg_host, args.pg_port, db_name)),
        ("run_root", D.gate_root(root, must_exist=True)),
        ("run_root_location", D.gate_run_root_location(root, out_path)),
    ):
        if reason:
            return _refuse(report, out_path, label, reason)
    if not Path(args.secrets).is_file():
        return _refuse(report, out_path, "secrets", "secrets file missing")
    secret = Path(args.secrets).read_text(encoding="utf-8").strip()
    if not secret:
        return _refuse(report, out_path, "secrets", "secrets file empty")
    report["host"] = machine

    # ── isolated roots + env (reused) ───────────────────────────────────────
    home = root / "home"
    project = root / "project"
    runs = root / "runs"
    profiles = root / "profiles"
    data_dir = root / "data"
    for d in (home, project, runs, profiles, data_dir):
        d.mkdir(parents=True, exist_ok=True)
    isolated_dotenv = D.ensure_isolated_dotenv(home)
    if D.gate_isolated_dotenv(isolated_dotenv):
        return _refuse(report, out_path, "isolated_dotenv", "isolated dotenv invalid")

    env = D.sanitize_env(os.environ)
    env.update({
        "PGPASSWORD": secret,
        "V3CORE_PG_PASSWORD": secret,
        "HOME": str(home), "USERPROFILE": str(home),
        "HOMEDRIVE": os.environ.get("SystemDrive", "C:"),
        "HOMEPATH": "\\" + str(home).split("\\", 1)[-1] if "\\" in str(home) else "",
        "V3CORE_HOME": str(home), "HERMES_HOME": str(home),
        "V3CORE_TEST_MODE": "1",
        "V3CORE_DOTENV": str(isolated_dotenv),
        "PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
    })

    # scratch sitecustomize (connect log) + optional staged instrumented package
    pylib = root / "pylib"
    pylib.mkdir(parents=True, exist_ok=True)
    (pylib / "sitecustomize.py").write_text(CONNECT_LOGGER, encoding="utf-8")
    connect_log = root / "conn-log.jsonl"
    diag_file = root / "diag.jsonl"
    for p in (connect_log, diag_file):
        if p.exists():
            p.unlink()
    path_parts = [str(pylib)]
    if instr_parent is not None:
        path_parts.insert(0, str(instr_parent))
    env["PYTHONPATH"] = os.pathsep.join(path_parts)
    env["M01M02_CONN_LOG"] = str(connect_log)
    env["M01M02_DIAG_FILE"] = str(diag_file)

    def write_config(tag: str, database: str) -> Path:
        pdir = profiles / tag
        pdir.mkdir(parents=True, exist_ok=True)
        pdata = data_dir / tag
        pdata.mkdir(parents=True, exist_ok=True)
        body = (
            "basePath: {base}\nmode: cloud\nstorage:\n  pg:\n"
            "    host: {host}\n    port: {port}\n    database: {db}\n    user: {user}\n"
            "  embed:\n    endpoint: ''\n    model: ''\n    dim: 1024\n"
            "    api_key: ''\n    proxy: ''\n"
            "  rerank:\n    endpoint: ''\n    model: ''\n    proxy: ''\n"
            "    timeout: 30\n    api_key: ''\n"
            "e1:\n  enabled: false\nobserver:\n  enabled: false\ntkg:\n  enabled: false\n"
            "llm:\n  provider: ''\n  model: ''\n  api_key: ''\n  base_url: ''\n"
            "  thinking: true\n  max_tokens: 0\n"
        ).format(base=json.dumps(str(pdata)), host=args.pg_host,
                 port=args.pg_port, db=database, user=args.pg_user)
        cfg = pdir / "config.yaml"
        cfg.write_text(body, encoding="utf-8")
        return cfg

    cfg_main = write_config("main", db_name)
    env["V3CORE_CONFIG"] = str(cfg_main)

    # ── module resolution gate ──────────────────────────────────────────────
    which = subprocess.run(
        [python, "-c", "import v3core, sys; print(v3core.__file__); print(sys.executable)"],
        env=env, cwd=str(project), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    resolved = (which.stdout or "").strip().splitlines()
    resolved = resolved[0].strip() if resolved else ""
    report["v3core_resolved"] = resolved
    if instr_parent is not None:
        expect_under = str(instr_parent)
    else:
        expect_under = str(source)
    ok_res = bool(resolved) and str(Path(resolved).resolve()).startswith(str(Path(expect_under).resolve()))
    if not rec("module_resolves_under_expected_root", ok_res,
               f"resolved={resolved!r} expect_under={expect_under!r} rc={which.returncode}"):
        _write(report, out_path, secret)
        return 1

    # ── disposable database (fresh only) ────────────────────────────────────
    def pg_connect(database):
        return psycopg2.connect(host=args.pg_host, port=args.pg_port, database=database,
                                user=args.pg_user, password=secret, connect_timeout=10)

    def pg_rows(database, sql, params=()):
        c = pg_connect(database)
        try:
            with c.cursor() as cur:
                cur.execute(sql, params)
                return [] if cur.description is None else cur.fetchall()
        finally:
            c.close()

    D.assert_disposable_target(db_name)
    admin = psycopg2.connect(host=args.pg_host, port=args.pg_port, database="postgres",
                             user=args.pg_user, password=secret, connect_timeout=10)
    try:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db_name,))
            exists = cur.fetchone() is not None
            if exists:
                return _refuse(report, out_path, "db_fresh", f"{db_name} already exists")
            cur.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        admin.close()
    report["db"] = db_name

    dsn = D.dsn_without_password(args.pg_host, args.pg_port, db_name, args.pg_user)
    boot = subprocess.run(
        [python, "-B", "-m", "v3core.distribution_cli", "bootstrap", "--target", dsn],
        env=env, cwd=str(project), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    boot_out = D.scrub(boot.stdout or "", secret)
    (runs / "bootstrap.stdout.json").write_text(boot_out, encoding="utf-8")
    parsed = D._try_json(boot_out) or {}
    rec("schema_bootstrapped", boot.returncode == 0 and (parsed.get("applied") is True
        or (parsed.get("result") or {}).get("applied") is True),
        f"rc={boot.returncode} applied={parsed.get('applied')}")

    # ── server (reused Server class) ────────────────────────────────────────
    port = D.free_port()
    server = D.Server("conc", python, env, project, port, runs / "serve.log")
    info = server.start()
    report["server"] = info
    if not rec("server_health_ok", bool(info["health"].get("ok")) and bool(info["health"].get("pg")),
               f"health={info['health']} port={port} pid={info['pid']}"):
        server.stop()
        _write(report, out_path, secret)
        return 1

    def tool(name, payload, label):
        req = D.build_tool_request(name, payload)
        status, body, envelope = D.http_call(f"http://127.0.0.1:{port}/tool", req)
        out = {"_http": status, "_label": label, "request": payload}
        if isinstance(envelope, dict):
            out["_envelope"] = envelope
            inner = envelope.get("result")
            decoded = D._try_json(inner) if isinstance(inner, str) else inner
            if isinstance(decoded, dict):
                out.update(decoded)
            else:
                out["_undecoded"] = D.truncate(D.scrub(str(inner), secret))[0]
        else:
            out["_body"] = D.truncate(D.scrub(str(body), secret))[0]
        return out

    # ── fixture A ───────────────────────────────────────────────────────────
    marker = f"conc-closure-{db_name}"
    id_a = f"m01m02e2e-conc-{db_name}"
    repl = f"并发纠正替换内容。{marker}-B"
    store_a = tool("v3_store", {"category": "projects", "title": "并发纠正基线",
                                "content": f"并发纠正基线内容。{marker}-A",
                                "source_id": id_a}, "store-a")
    rec("fixture_a_committed", bool(store_a.get("success")), f"ok={store_a.get('success')}")

    def memory_count():
        return int(pg_rows(db_name, "SELECT count(*) FROM public.explicit_memories")[0][0])

    def relation_count():
        return int(pg_rows(db_name, "SELECT count(*) FROM public.memory_relations")[0][0])

    def a_state():
        rows = pg_rows(db_name, "SELECT memory_id, content, status FROM public.explicit_memories WHERE memory_id=%s", (id_a,))
        return dict(zip(("memory_id", "content", "status"), rows[0])) if rows else None

    def b_rows():
        rows = pg_rows(db_name, "SELECT memory_id, status, content FROM public.explicit_memories WHERE content=%s", (repl,))
        return [dict(zip(("memory_id", "status", "content"), r)) for r in rows]

    def edge_rows():
        rows = pg_rows(db_name, "SELECT relation_id, relation_type, from_memory_id, to_memory_id, correction_id, recorded_at::text FROM public.memory_relations WHERE from_memory_id=%s", (id_a,))
        return [dict(zip(("relation_id", "relation_type", "from_memory_id", "to_memory_id", "correction_id", "recorded_at"), r)) for r in rows]

    before_mem, before_rel = memory_count(), relation_count()
    payload = {"action": "correct", "memory_id": id_a, "replacement_content": repl,
               "correction_reason": "concurrent identical correction"}

    # ── 3 concurrent identical corrections (barrier, real HTTP) ─────────────
    barrier = threading.Barrier(3)
    t0 = time.monotonic()
    timings: dict = {}

    def one(i):
        barrier.wait(timeout=30)
        s = time.monotonic()
        r = tool("v3_update", dict(payload), f"conc-identical-t{i}")
        timings[f"t{i}"] = {"start_offset": round(s - t0, 6),
                            "end_offset": round(time.monotonic() - t0, 6)}
        return r

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(one, i) for i in range(3)]
        results = {}
        for i, fut in enumerate(futs):
            try:
                results[f"t{i}"] = fut.result(timeout=120)
            except BaseException as exc:  # noqa: BLE001
                results[f"t{i}"] = {"_http": 0, "_thread_error": repr(exc)}

    after_mem, after_rel = memory_count(), relation_count()
    a_after = a_state()
    b_after = b_rows()
    edges = edge_rows()

    per_thread = {}
    for k, r in results.items():
        per_thread[k] = {
            "http": r.get("_http"),
            "success": r.get("success"),
            "status": r.get("status"),
            "error_code": r.get("error_code"),
            "error": r.get("error"),
            "durable": r.get("durable"),
            "deduplicated": r.get("deduplicated"),
            "old_memory_id": r.get("old_memory_id"),
            "new_memory_id": r.get("new_memory_id"),
            "old_status": r.get("old_status"),
            "new_status": r.get("new_status"),
            "current_memory_id": r.get("current_memory_id"),
            "relation": r.get("relation"),
            "warnings": r.get("warnings"),
            "thread_error": r.get("_thread_error"),
            "timing": timings.get(k),
        }
    successes = sum(1 for r in results.values()
                    if r.get("success") is True and r.get("durable") is not False)
    report["concurrency"] = {
        "successes": successes,
        "per_thread": per_thread,
        "memory_delta": after_mem - before_mem,
        "relation_delta": after_rel - before_rel,
        "a_before": {"memory_id": id_a},
        "a_after": a_after,
        "b_rows": b_after,
        "edges_from_a": edges,
        "memory_before": before_mem, "memory_after": after_mem,
        "relation_before": before_rel, "relation_after": after_rel,
    }

    # raw connect/diag evidence
    report["connect_log"] = _read_jsonl(connect_log)
    report["diag"] = _read_jsonl(diag_file)
    report["connect_count"] = len(report["connect_log"])

    rec("canonical_reproduced", successes == 3 and after_mem - before_mem == 1
        and after_rel - before_rel == 1 and (a_after or {}).get("status") == "archived"
        and len(b_after) == 1, f"successes={successes} mem_delta={after_mem-before_mem} "
        f"rel_delta={after_rel-before_rel} a_status={(a_after or {}).get('status')} b_rows={len(b_after)}")

    # Sequential retry of the very SAME concurrent A->B, not a new fixture.
    m0, r0 = memory_count(), relation_count()
    retry = tool("v3_update", dict(payload), "same-a-to-b-after-concurrent")
    report["sequential"] = {"receipt": retry, "a_after": a_state(),
        "b_rows": b_rows(), "edges_from_a": edge_rows(),
        "memory_delta": memory_count()-m0, "relation_delta": relation_count()-r0}
    rec("sequential_idempotent", retry.get("success") is True
        and retry.get("durable") is True and retry.get("deduplicated") is True
        and memory_count()==m0 and relation_count()==r0
        and a_state()["status"]=="archived" and len(b_rows())==1
        and b_rows()[0]["status"]=="active" and len(edge_rows())==1,
        f"status={retry.get('status')} durable={retry.get('durable')} "
        f"dedup={retry.get('deduplicated')} mem_delta={memory_count()-m0} "
        f"rel_delta={relation_count()-r0}")

    server.stop()
    _write(report, out_path, secret)
    return 0 if not report["reasons"] else 1


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"_unparsed": line[:400]})
    return out


def _write(report, out_path, secret):
    payload = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if secret:
        payload = payload.replace(secret, "***REDACTED***")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload, encoding="utf-8")
    print(f"REPORT_WRITTEN {out_path} reasons={len(report['reasons'])}", flush=True)


def _refuse(report, out_path, label, reason):
    report["refused"] = {"stage": label, "reason": reason}
    print(f"REFUSE {label}: {reason}", flush=True)
    _write(report, out_path, "")
    return 90


CONNECT_LOGGER = '''\
"""Scratch sitecustomize: log every psycopg2.connect with backend pid + thread."""
import json
import os
import threading
import time

_LOG = os.environ.get("M01M02_CONN_LOG")
if _LOG:
    try:
        import psycopg2

        _orig = psycopg2.connect
        _lock = threading.Lock()

        def _patched(*a, **k):
            conn = _orig(*a, **k)
            try:
                pid = conn.get_backend_pid()
            except Exception:
                pid = None
            try:
                with _lock:
                    with open(_LOG, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({
                            "t": round(time.time(), 6),
                            "tid": threading.get_ident(),
                            "backend_pid": pid,
                            "autocommit": getattr(conn, "autocommit", None),
                            "dsn_db": k.get("dbname"),
                        }) + "\\n")
            except Exception:
                pass
            return conn

        psycopg2.connect = _patched
    except Exception:
        pass
'''


if __name__ == "__main__":
    sys.exit(main())
