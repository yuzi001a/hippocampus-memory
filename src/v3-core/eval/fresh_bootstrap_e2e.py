"""FRESH_DB_BOOTSTRAP_DEPENDENCY_ORDER — packaged-path E2E.

RED (on the defective build): a brand-new database + the installed wheel +
`hippocampus bootstrap` must fail with
``UndefinedTable: relation "public.observation_notes" does not exist``.

GREEN (after the ordering fix): rc = 0, every bootstrap-truth table present,
the long-observation FK really exists, and a SECOND bootstrap on the same
database is still rc = 0 without duplicating or damaging schema.

Deliberately does not rely on: repo cwd, editable install, PYTHONPATH,
hand-ordered SQL, or the B01 E2E bootstrap fallback.

Run:  <repo>/.venv/Scripts/python.exe src/v3-core/eval/fresh_bootstrap_e2e.py
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve()
REPO = HERE.parents[3]                     # C:/hp-testbed  (<repo>/src/v3-core/eval/x.py)
PKG_ROOT = REPO / "src" / "v3-core"        # wheel build root
EVIDENCE = REPO / "evidence" / "fresh-bootstrap-e2e.json"

WORK = pathlib.Path(os.environ.get(
    "FRESHBOOT_WORK",
    r"C:/Users/servi/workspace/backups/fresh-bootstrap-e2e",
))
CONTAINER = "freshboot-e2e-pg"
PORT = 55505
DB = "freshboot"
PGUSER = "v3user"
PGPASS = "freshbootpw"

# What the packaged bootstrap is REQUIRED to produce (verified against
# distribution_cli doctor's required_tables + the alpha/upgrade artifacts).
BOOTSTRAP_TRUTH_TABLES = [
    "explicit_memories",
    "qa_pairs",
    "qa_embedding_chunks",
    "conversation_stream",
    "topics",
    "topic_entries",
    "observation_notes",
    "observation_embedding_chunks",
    "yin_paragraphs",
    "embedding_failures",
    "schema_versions",
]
# F3 (2026-10-02): embedding_failures was recorded here as "present as a
# packaged artifact but applied by NO install path". That classification was
# the defect itself — the ledger DDL shipped only at the repo schema root, so
# no fresh install ever created the table. It is now packaged and spliced by
# alpha_bootstrap.sql, so it belongs to the bootstrap truth set above.
# Nothing is left on the unreachable list.
UNREACHABLE_ARTIFACT_TABLES: list[str] = []

FK_EXPECTATIONS = {
    "observation_embedding_chunks": ("observation_id", "observation_notes"),
    "qa_embedding_chunks": ("qa_id", "qa_pairs"),
}

S: dict = {"steps": {}, "summary": {}}


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def docker(*args):
    return run(["docker", *args])


def _host_tcp_ok(port: int = None) -> bool:
    """True when the published PG port really accepts a host-side TCP connect."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port or PORT), timeout=2):
            return True
    except OSError:
        return False


def psql(sql: str) -> str:
    r = docker("exec", CONTAINER, "psql", "-U", PGUSER, "-d", DB, "-tAc", sql)
    return (r.stdout or "").strip()


def psql_rc(sql: str):
    r = docker("exec", CONTAINER, "psql", "-U", PGUSER, "-d", DB, "-tAc", sql)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


# ── 1. build the wheel from the tree (record provenance) ────────────────────

def build_wheel() -> pathlib.Path:
    provided = os.environ.get("FRESHBOOT_WHEEL")
    if provided:
        whl = pathlib.Path(provided)
        assert whl.exists(), f"FRESHBOOT_WHEEL not found: {whl}"
        import hashlib
        S["steps"]["build"] = {
            "source": "provided_wheel (public release asset / installer artifact)",
            "wheel": whl.name,
            "wheel_bytes": whl.stat().st_size,
            "sha256": hashlib.sha256(whl.read_bytes()).hexdigest(),
        }
        log(f"[1] using provided wheel {whl.name} ({whl.stat().st_size} bytes)")
        return whl

    head = run(["git", "-C", str(REPO), "rev-parse", "HEAD"]).stdout.strip()
    dirty = run(["git", "-C", str(REPO), "status", "--porcelain",
                 "--", "src/v3-core/src"]).stdout.strip()
    dist = WORK / "dist"
    if dist.exists():
        for p in dist.iterdir():
            p.unlink()
    dist.mkdir(parents=True, exist_ok=True)
    r = run(["uv", "build", "--wheel", "--out-dir", str(dist)], cwd=str(PKG_ROOT))
    assert r.returncode == 0, f"wheel build failed: {r.stderr[-800:]}"
    whl = next(dist.glob("*.whl"))
    S["steps"]["build"] = {
        "head": head,
        "packaged_source_dirty": dirty or None,
        "wheel": whl.name,
        "wheel_bytes": whl.stat().st_size,
        "build_rc": r.returncode,
    }
    log(f"[1] wheel {whl.name} ({whl.stat().st_size} bytes) from HEAD {head[:8]}"
        f" packaged_source_dirty={bool(dirty)}")
    return whl


# ── 2. fresh venv OUTSIDE the repo + install the wheel ─────────────────────

def fresh_venv(whl: pathlib.Path) -> pathlib.Path:
    venv = WORK / "venv"
    if venv.exists():
        subprocess.run(["rm", "-rf", str(venv)], check=False)
    r = run(["uv", "venv", str(venv), "--python", "3.11"])
    assert r.returncode == 0, r.stderr[-500:]
    py = venv / "Scripts" / "python.exe"
    r = run(["uv", "pip", "install", "--python", str(py), str(whl)])
    assert r.returncode == 0, f"install failed: {r.stderr[-1500:]}"
    cli = venv / "Scripts" / "hippocampus.exe"
    assert cli.exists(), "hippocampus console script missing from a fresh install"
    ver = run([str(py), "-c",
               "import importlib.metadata as m; print(m.version('v3-core'))"]).stdout.strip()
    S["steps"]["fresh_install"] = {"venv": str(venv), "v3_core_version": ver,
                                   "console_script": cli.name}
    log(f"[2] fresh venv install OK (v3-core {ver}), outside the repo: {venv}")
    return venv


# ── 3. disposable fresh PostgreSQL ─────────────────────────────────────────

def fresh_pg() -> None:
    docker("rm", "-f", CONTAINER)
    r = docker("run", "-d", "--name", CONTAINER,
               "-p", f"127.0.0.1:{PORT}:5432",
               "-e", f"POSTGRES_USER={PGUSER}",
               "-e", f"POSTGRES_PASSWORD={PGPASS}",
               "-e", f"POSTGRES_DB={DB}",
               "pgvector/pgvector:pg17")
    assert r.returncode == 0, r.stderr[-500:]
    # pg_isready is not enough: it answers OK during image init while the
    # published TCP port still refuses connections. Require a real query plus a
    # host-side TCP connect, otherwise a readiness race looks like a defect.
    for _ in range(90):
        probe = docker("exec", CONTAINER, "psql", "-U", PGUSER, "-d", DB, "-tAc",
                       "SELECT version()")
        if probe.returncode == 0 and (probe.stdout or "").strip() and _host_tcp_ok():
            break
        time.sleep(1)
    else:
        raise AssertionError("fresh PG never became ready (real query + host TCP)")
    tables = psql("SELECT count(*) FROM information_schema.tables "
                  "WHERE table_schema='public'")
    ver = psql("SELECT version()").split(",")[0]
    S["steps"]["pg"] = {"container": CONTAINER, "port": PORT, "database": DB,
                        "version": ver, "public_tables_before": int(tables or 0)}
    log(f"[3] fresh PG {ver}; public tables before bootstrap = {tables}")


DSN = f"postgresql://{PGUSER}:{PGPASS}@127.0.0.1:{PORT}/{DB}"


def clean_env() -> dict:
    """No PYTHONPATH / no V3CORE_* / no repo cwd leakage."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("V3CORE", "PYTHON"))}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def bootstrap_once(venv: pathlib.Path, label: str) -> dict:
    cli = venv / "Scripts" / "hippocampus.exe"
    neutral = WORK / "neutral-cwd"
    neutral.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([str(cli), "bootstrap", "--dsn", DSN],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(neutral), env=clean_env())
    out = (r.stdout or "") + (r.stderr or "")
    rec = {
        "rc": r.returncode,
        "tail": out[-700:],
        "cwd": str(neutral),
        "pythonpath_set": "PYTHONPATH" in clean_env(),
        "undefined_notes": ("observation_notes" in out and "does not exist" in out),
        "undefinedtable": "UndefinedTable" in out,
    }
    S["steps"][label] = rec
    log(f"[4] bootstrap({label}) rc={rec['rc']} "
        f"undefined_notes={rec['undefined_notes']} undefinedtable={rec['undefinedtable']}")
    return rec


# ── table / FK truth ───────────────────────────────────────────────────────

def table_report() -> dict:
    rep = {}
    for t in BOOTSTRAP_TRUTH_TABLES + UNREACHABLE_ARTIFACT_TABLES:
        rep[t] = bool(psql(
            "SELECT 1 FROM information_schema.tables "
            f"WHERE table_schema='public' AND table_name='{t}'"))
    return rep


def fk_report() -> dict:
    out = {}
    for table, (col, ref) in FK_EXPECTATIONS.items():
        rows = psql(
            "SELECT conname FROM pg_constraint "
            f"WHERE conrelid='public.{table}'::regclass AND contype='f' "
            f"AND confrelid='public.{ref}'::regclass")
        out[f"{table}.{col} -> {ref}"] = rows or None
    return out


def index_count() -> dict:
    return {
        t: int(psql(f"SELECT count(*) FROM pg_indexes WHERE schemaname='public' "
                    f"AND tablename='{t}'") or 0)
        for t in ("qa_embedding_chunks", "observation_embedding_chunks")
    }


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)

    whl = build_wheel()
    venv = fresh_venv(whl)
    fresh_pg()

    # ── RED anchor: current build must fail on a brand-new database ──
    r1 = bootstrap_once(venv, "bootstrap_1")
    S["summary"]["red_reproduced"] = (
        r1["rc"] != 0 and r1["undefined_notes"] and r1["undefinedtable"])
    if r1["rc"] != 0:
        S["steps"]["tables_after_failure"] = table_report()
        S["summary"]["fix_required"] = "FRESH_DB_BOOTSTRAP_DEPENDENCY_ORDER"
        log("[RED] packaged bootstrap FAILS on a brand-new database")
        save(red=True)
        return 1

    tables = table_report()
    fks = fk_report()
    idx1 = index_count()
    missing = [t for t in BOOTSTRAP_TRUTH_TABLES if not tables.get(t)]
    fk_missing = [k for k, v in fks.items() if not v]
    S["steps"]["tables_after_bootstrap_1"] = tables
    S["steps"]["fks_after_bootstrap_1"] = fks
    S["steps"]["indexes_after_bootstrap_1"] = idx1
    S["steps"]["unreachable_artifact_tables"] = {
        t: tables.get(t) for t in UNREACHABLE_ARTIFACT_TABLES}

    # ── idempotency: a second bootstrap on the same database must pass ──
    r2 = bootstrap_once(venv, "bootstrap_2")
    tables2 = table_report()
    fks2 = fk_report()
    idx2 = index_count()
    S["steps"]["tables_after_bootstrap_2"] = tables2
    S["steps"]["fks_after_bootstrap_2"] = fks2
    S["steps"]["indexes_after_bootstrap_2"] = idx2
    schema_rows = psql("SELECT count(*) FROM schema_versions")

    S["summary"].update({
        "packaged_fresh_bootstrap": "PASS" if not missing else "FAIL",
        "missing_tables": missing or None,
        "fk_present": not fk_missing,
        "fk_missing": fk_missing or None,
        "second_bootstrap_rc": r2["rc"],
        "idempotent_tables_stable": tables == tables2,
        "idempotent_indexes_stable": idx1 == idx2,
        "schema_versions_rows": int(schema_rows or 0),
        "fallback_used": False,
    })
    log(f"[5] tables={sum(1 for v in tables.values() if v)}/{len(tables)} "
        f"missing={missing} fk_ok={not fk_missing}")
    log(f"[6] bootstrap#2 rc={r2['rc']} tables_stable={tables == tables2} "
        f"indexes_stable={idx1 == idx2} schema_versions={schema_rows}")
    save()
    ok = (not missing and not fk_missing and r2["rc"] == 0
          and tables == tables2 and idx1 == idx2)
    print(f"PACKAGED_FRESH_BOOTSTRAP = {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 2


def save(red: bool = False) -> None:
    path = EVIDENCE.with_suffix(".red.json") if red else EVIDENCE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(S, indent=2, ensure_ascii=False),
                    encoding="utf-8", newline="\n")
    log(f"evidence -> {path}")


if __name__ == "__main__":
    sys.exit(main())
