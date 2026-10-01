"""F3 — fresh installed-wheel bootstrap probe for public.embedding_failures.

One narrowly scoped wheel/CLI/PG probe (F3 only). It runs OUTSIDE the repo,
against an installed wheel in a disposable venv, on the dedicated laptop's
throwaway PostgreSQL. It never rewrites runtime code and never lets a ledger
writer auto-DDL stand in for a missing bootstrap table.

Boundary (hard-coded, no override):
  * hostname must be DESKTOP-EQP3OBU, PostgreSQL must be 127.0.0.1:55432,
    role is fixed — none of these are configurable
  * the database must never have existed before this run; existing -> refuse
  * no DROP / no truncate / no reuse of any database
  * the role password is read ONLY from PGPASSWORD (process env) and is never
    printed, logged, or placed in argv, config, or output

Refusal ladder (every rung is BEFORE a connection or a mutation):
  1. hostname
  2. --db-name / --expect must match the hard-coded ACCEPTED_RUNS map
  3. PGPASSWORD must be present (its value is never echoed)
  4. artifacts exist; --out must NOT already exist (no silent overwrite)
  5. HOME/USERPROFILE isolation + clean env applied to THIS process
  6. sys.prefix == --env-root; imported v3core really inside --env-root
  7. artifact identity: GREEN needs the packaged ledger, byte-equal to the
     repo root artifact; RED refuses a baseline that already packages it
  then, and only then, the database is created.

Usage:
    python f3_fresh_bootstrap_e2e.py --wheel <whl> --env-root <venv> \
        --db-name f3bootstrap_20261002_parent_green --expect green \
        --root-ledger <repo>/schema/embedding_failures.sql --out <report.json>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

HOST = "127.0.0.1"
PORT = 55432
ROLE = "f2e2e"
MAINT = "postgres"
EXPECTED_HOSTNAME = "DESKTOP-EQP3OBU"
LEDGER = "embedding_failures"
LEDGER_INDEXES = (
    "embedding_failures_entity_phase_key",
    "embedding_failures_unresolved_idx",
    "embedding_failures_class_idx",
)

#: The ONLY (db-name -> expectation) pairs this probe will ever run. Anything
#: else — an unknown name, or a name paired with the other expectation — is
#: refused before a connection is opened. ``red2`` is a historical deviation
#: (an earlier run under a different baseline) kept so that run stays
#: replayable; it is not a licence to invent further names.
ACCEPTED_RUNS: dict[str, str] = {
    "f3bootstrap_20261002_red": "red",
    "f3bootstrap_20261002_red2": "red",
    "f3bootstrap_20261002_green": "green",
    "f3bootstrap_20261002_parent_red": "red",
    "f3bootstrap_20261002_parent_red_utf8": "red",
    "f3bootstrap_20261002_parent_green": "green",
}

#: Owned HOME/USERPROFILE for both this process and every CLI subprocess.
_HOME_DIRNAME = "f3-probe-home"
#: Provider/V3CORE configuration never leaks into the probe or the subprocesses.
_DROP_PREFIXES = ("V3CORE_", "OPENAI_", "ANTHROPIC_", "EMBEDDING_", "HERMES_")
_DROP_SUFFIXES = ("_API_KEY",)
#: Retained deliberately: libpq/psycopg2 supply the role password from here.
_KEEP_KEYS = ("PGPASSWORD",)


def _refuse(message: str) -> None:
    """Refuse loudly. ``message`` must never carry credential material."""
    sys.exit("REFUSED: " + message)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_relative_to(child: Path, root: Path) -> bool:
    """Path containment by component, never by string prefix.

    ``C:\\env2`` is a string prefix of ``C:\\env``'s sibling names but is NOT a
    parent of it; a ``startswith`` containment test would wave that through.
    """
    try:
        return child.relative_to(root) is not None
    except ValueError:
        return False


def _guard_host() -> str:
    name = socket.gethostname().upper()
    if name != EXPECTED_HOSTNAME:
        _refuse(f"hostname {name!r} != {EXPECTED_HOSTNAME!r}; this probe is "
                "bound to the dedicated laptop and connects nowhere else")
    return name


def _guard_run(args: argparse.Namespace) -> str:
    """Validate --db-name AND --expect against the hard-coded map (pre-DB)."""
    expected = ACCEPTED_RUNS.get(args.db_name)
    if expected is None:
        _refuse(f"database name {args.db_name!r} is not an authorised F3 run "
                f"name; authorised names are exactly: {sorted(ACCEPTED_RUNS)}")
    if args.expect != expected:
        _refuse(f"expectation {args.expect!r} contradicts the recorded "
                f"expectation {expected!r} for {args.db_name!r}")
    return expected


def _dsn(db: str) -> str:
    """Target DSN WITHOUT the password; PGPASSWORD supplies it."""
    return f"postgresql://{ROLE}@{HOST}:{PORT}/{db}"


def _psql(sql: str, db: str, params: tuple | None = None, fetch: bool = True):
    import psycopg2

    conn = psycopg2.connect(host=HOST, port=PORT, database=db, user=ROLE,
                            password=os.environ.get("PGPASSWORD", ""),
                            connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if fetch else None
    finally:
        conn.close()


def _create_db(db: str) -> None:
    import psycopg2

    conn = psycopg2.connect(host=HOST, port=PORT, database=MAINT, user=ROLE,
                            password=os.environ.get("PGPASSWORD", ""),
                            connect_timeout=5)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,))
            if cur.fetchone() is not None:
                _refuse(f"database {db!r} already exists; no DROP, no reuse, "
                        "no alter — use a fresh name")
            cur.execute(f'CREATE DATABASE "{db}"')
    finally:
        conn.close()


def _facts(db: str) -> dict:
    rows = _psql(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s", db, (LEDGER,))
    exists = bool(rows)
    idx = []
    if exists:
        idx = sorted(r[0] for r in _psql(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
            "AND tablename=%s", db, (LEDGER,)))
    return {"table_exists": exists, "indexes": idx}


def _clean_env(home: Path, password: str) -> tuple[dict, list[str]]:
    """Child/probe environment: isolated home, no provider config, no keys."""
    dropped: list[str] = []
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _KEEP_KEYS:
            env[key] = value
            continue
        if upper.startswith(_DROP_PREFIXES) or upper.endswith(_DROP_SUFFIXES):
            dropped.append(key)
            continue
        env[key] = value
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["HOMEDRIVE"] = home.drive
    env["HOMEPATH"] = str(home.anchor or os.sep)
    env["PGPASSWORD"] = password          # retained on purpose
    env["PYTHONPATH"] = ""               # empty, never inherited
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env, sorted(dropped)


def _apply_env(env: dict) -> None:
    """Apply the clean environment to THIS process (before importing v3core)."""
    os.environ.clear()
    os.environ.update(env)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def _run_cli(exe: Path, env_root: Path, child_env: dict, db: str,
             args: list[str], *, bootstrap: bool = False) -> dict:
    proc = subprocess.run([str(exe), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          cwd=str(env_root.parent), env=child_env)
    payload: object = None
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        payload = None
    top = payload if isinstance(payload, dict) else {}
    inner = top.get("result") if isinstance(top.get("result"), dict) else {}
    record = {
        "argv": ["hippocampus", *args],
        "executable": str(exe),
        "cwd": str(env_root.parent),
        "returncode": proc.returncode,
        "json": payload,
        "stdout": proc.stdout,
        "stderr": proc.stderr,   # full, never truncated
    }
    if bootstrap:
        # Verified against the ACTUAL installed shape: `applied` /
        # `upgrade_applied` / `target` live under json["result"], never at the
        # top level. The reported target must be the guarded one we asked for.
        target = inner.get("target")
        target = target if isinstance(target, dict) else {}
        record["applied"] = inner.get("applied")
        record["upgrade_applied"] = inner.get("upgrade_applied")
        record["result_target"] = {
            "host": target.get("host"),
            "port": target.get("port"),
            "user": target.get("user"),
            "database": target.get("database"),
            "password_redacted_by_cli": target.get("password") == "***",
        }
        record["target_matches_guarded"] = (
            target.get("host") == HOST
            and int(target.get("port") or 0) == PORT
            and target.get("user") == ROLE
            and target.get("database") == db
        )
        record["alpha_bootstrap_sql_sha256"] = top.get("alpha_bootstrap_sql_sha256")
        record["include_expanded"] = inner.get("include_expanded")
    return record


def _bootstrap_ok(record: dict) -> bool:
    return (record.get("returncode") == 0
            and record.get("applied") is True
            and record.get("upgrade_applied") is True
            and record.get("target_matches_guarded") is True)


def _writer_readback(db: str) -> dict:
    """Call the REAL failure writer on the disposable DB and read the row back."""
    import psycopg2

    from v3core.embed_failures import record_embedding_failure

    conn = psycopg2.connect(host=HOST, port=PORT, database=db, user=ROLE,
                            password=os.environ.get("PGPASSWORD", ""),
                            connect_timeout=5)
    try:
        ok = record_embedding_failure(
            conn, entity_table="qa_pairs", entity_id="f3probe-1",
            phase="f3_probe", error=TimeoutError("f3 probe: no provider call"))
        with conn.cursor() as cur:
            cur.execute("SELECT entity_table, entity_id, phase, error_class, "
                        "retryable, attempts FROM public.embedding_failures "
                        "WHERE entity_id = %s", ("f3probe-1",))
            row = cur.fetchone()
        return {"status": "RAN", "writer_returned": ok,
                "provider_calls": "none (synthetic TimeoutError; no network path)",
                "row": list(row) if row else None,
                "row_found": row is not None}
    finally:
        conn.close()


def _leak_check(texts: dict[str, str], password: str) -> None:
    """Refuse if the role password appears anywhere we are about to emit."""
    if not password:
        return
    for label, text in texts.items():
        if text and password in text:
            _refuse("credential material from the environment would be written "
                    f"into the {label} output; refusing to emit it "
                    "(no value shown, report not written)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="F3 fresh installed-wheel bootstrap probe (F3 only).")
    ap.add_argument("--wheel", required=True)
    ap.add_argument("--env-root", required=True)
    ap.add_argument("--db-name", required=True)
    ap.add_argument("--expect", choices=("red", "green"), required=True)
    ap.add_argument("--root-ledger", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    # ── 1. host (no connection possible before this) ────────────────────────
    hostname = _guard_host()

    # ── 2. authorised run identity (no connection before this either) ───────
    _guard_run(args)

    # ── 3. credential presence, never the value ────────────────────────────
    password = os.environ.get("PGPASSWORD", "")
    if not password:
        _refuse("PGPASSWORD is not set in this process environment; refusing "
                "before any connection (its value is never read into a report)")

    # ── 4. artifacts, and no silent overwrite of an existing report ─────────
    probe_path = Path(__file__).resolve()
    wheel = Path(args.wheel).resolve()
    root_ledger = Path(args.root_ledger).resolve()
    for label, path in (("probe", probe_path), ("--wheel", wheel),
                        ("--root-ledger", root_ledger)):
        if not path.is_file():
            _refuse(f"{label} artifact {path} does not exist")
    out_path = Path(args.out)
    if out_path.exists():
        _refuse(f"report path {out_path.name} already exists; refusing to "
                "overwrite an existing report before touching the database")

    report: dict = {
        "probe": "f3_fresh_bootstrap_e2e",
        "expectation": args.expect,
        "hostname": hostname,
        "target": {"host": HOST, "port": PORT, "user": ROLE,
                   "database": args.db_name, "password_in_output": False},
        "source": {
            "probe": probe_path.name,
            "probe_path": str(probe_path),
            "probe_sha256": _sha256(probe_path),
            "wheel": wheel.name,
            "wheel_path": str(wheel),
            "wheel_sha256": _sha256(wheel),
            "root_ledger_path": str(root_ledger),
            "root_ledger_sha256": _sha256(root_ledger),
        },
    }

    # ── 5. environment isolation, applied to THIS process first ────────────
    env_root = Path(args.env_root).resolve()
    if not env_root.is_dir():
        _refuse(f"--env-root {env_root} is not a directory")
    home = (env_root.parent / _HOME_DIRNAME)
    home_existed = home.exists()
    home.mkdir(parents=True, exist_ok=True)
    home = home.resolve()
    child_env, dropped = _clean_env(home, password)
    _apply_env(child_env)          # BEFORE importing v3core, on purpose

    try:
        import site
        usersite_dir = site.getusersitepackages()
    except Exception:
        site = None                # type: ignore[assignment]
        usersite_dir = ""
    exe = Path(sys.executable).resolve()
    prefix = Path(sys.prefix).resolve()
    # The CLI under test is the wheel's own console script; running bare
    # `python` would not exercise the installed entry point at all.
    bindir = env_root / ("Scripts" if os.name == "nt" else "bin")
    cli_exe = bindir / ("hippocampus.exe" if os.name == "nt" else "hippocampus")
    if not cli_exe.is_file():
        _refuse(f"installed console script {cli_exe} is missing from "
                "--env-root; refusing to fall back to the ambient checkout")
    report["environment"] = {
        "executable": str(exe),
        "executable_inside_env": _is_relative_to(exe, env_root),
        "cli_executable": str(cli_exe),
        "cli_executable_inside_env": _is_relative_to(cli_exe.resolve(), env_root),
        "sys_prefix": str(prefix),
        "sys_base_prefix": getattr(sys, "base_prefix", ""),
        "env_root": str(env_root),
        "prefix_matches_env_root": prefix == env_root,
        "home": str(home),
        "home_owned_by_probe": True,
        "home_existed_before": home_existed,
        "home_under": str(env_root.parent),
        "userprofile": os.environ.get("USERPROFILE", ""),
        "pythonpath": os.environ.get("PYTHONPATH", ""),
        "pythonpath_is_empty": os.environ.get("PYTHONPATH", "") == "",
        "pythonnousersite": os.environ.get("PYTHONNOUSERSITE", ""),
        "pythonnowritebytecode": os.environ.get("PYTHONDONTWRITEBYTECODE", ""),
        "no_user_site_flag": bool(getattr(sys.flags, "no_user_site", False)),
        "user_site_enabled": bool(getattr(site, "ENABLE_USER_SITE", False))
        if site is not None else None,
        "user_site_dir": usersite_dir,
        "user_site_inside_isolated_home": bool(
            usersite_dir and _is_relative_to(Path(usersite_dir), home)),
        "pgpassword_present": True,
        "pgpassword_in_output": False,
        "cleared_keys": dropped,
    }

    # ── 6. identity: this interpreter and this import, or nothing ──────────
    if prefix != env_root:
        _refuse(f"running interpreter prefix {prefix} != --env-root {env_root}; "
                "the probe must execute under the installed wheel's own "
                "environment, not the ambient checkout")

    import v3core  # noqa: E402  (after the env isolation above, deliberately)

    v3_file = Path(v3core.__file__).resolve()
    inside_env = _is_relative_to(v3_file, env_root)
    report["import"] = {"v3core_file": str(v3_file),
                        "inside_env": inside_env,
                        "env_root": str(env_root)}
    if not inside_env:
        _refuse(f"imported v3core resolves to {v3_file}, which is not inside "
                f"--env-root {env_root}; refusing to credit another "
                "environment's code to this wheel")

    # ── 7. artifact identity: GREEN needs the ledger, RED must lack it ─────
    ledger_path = v3_file.parent / "schema" / f"{LEDGER}.sql"
    packaged_present = ledger_path.is_file()
    packaged_sha = _sha256(ledger_path) if packaged_present else None
    report["import"].update({
        "packaged_ledger_present": packaged_present,
        "packaged_ledger_path": str(ledger_path),
        "packaged_ledger_sha256": packaged_sha,
        "root_ledger_sha256": report["source"]["root_ledger_sha256"],
        "packaged_equals_root": packaged_sha == report["source"]["root_ledger_sha256"],
    })
    if args.expect == "green":
        if not packaged_present:
            _refuse("GREEN baseline does not package "
                    f"schema/{LEDGER}.sql ({ledger_path}); the installed wheel "
                    "cannot bootstrap the ledger it is supposed to prove")
        if not report["import"]["packaged_equals_root"]:
            _refuse("GREEN refuses a packaged ledger whose sha256 differs from "
                    "the repo root artifact; the wheel under test is not the "
                    "artifact under review")
    else:
        if packaged_present:
            _refuse(f"RED baseline already packages schema/{LEDGER}.sql "
                    f"({ledger_path}); this is the wrong artifact for a red "
                    "run — a baseline without the ledger is required")

    # ── from here on, and only here, the database is touched ───────────────
    _create_db(args.db_name)
    report["db_created_fresh"] = True
    report["before"] = _facts(args.db_name)

    report["cli"] = {
        "bootstrap_1": _run_cli(cli_exe, env_root, child_env, args.db_name,
                                ["bootstrap", "--target", _dsn(args.db_name)],
                                bootstrap=True),
    }
    report["after"] = _facts(args.db_name)
    report["cli"]["bootstrap_2_idempotent"] = _run_cli(
        cli_exe, env_root, child_env, args.db_name,
        ["bootstrap", "--target", _dsn(args.db_name)], bootstrap=True)
    report["after_second"] = _facts(args.db_name)
    report["cli"]["doctor"] = _run_cli(
        cli_exe, env_root, child_env, args.db_name,
        ["doctor", "--dsn", _dsn(args.db_name)])

    missing_idx = [i for i in LEDGER_INDEXES if i not in report["after"]["indexes"]]
    missing_idx_2 = [i for i in LEDGER_INDEXES
                     if i not in report["after_second"]["indexes"]]
    report["observed"] = {
        "embedding_failures_present": report["after"]["table_exists"],
        "missing_indexes": missing_idx,
        "missing_indexes_after_second": missing_idx_2,
        "indexes_after": report["after"]["indexes"],
        "indexes_after_second": report["after_second"]["indexes"],
        "all_required_indexes": not missing_idx,
        "facts_stable_across_second_bootstrap":
            report["after"] == report["after_second"],
    }

    b1 = report["cli"]["bootstrap_1"]
    b2 = report["cli"]["bootstrap_2_idempotent"]
    reasons: list[str] = []
    if report["before"]["table_exists"]:
        reasons.append("the freshly created database already carried the ledger "
                       "table before any bootstrap ran")
    if not _bootstrap_ok(b1):
        reasons.append("first bootstrap did not succeed normally "
                       "(need rc=0 with applied=true and upgrade_applied=true)")
    if not _bootstrap_ok(b2):
        reasons.append("second bootstrap did not succeed normally "
                       "(need rc=0 with applied=true and upgrade_applied=true)")

    # doctor evidence is recorded for both expectations; only GREEN gates on it
    doc = report["cli"]["doctor"].get("json")
    doc = doc if isinstance(doc, dict) else {}
    db_check = (doc.get("checks") or {}).get("database")
    db_check = db_check if isinstance(db_check, dict) else {}
    required_tables = db_check.get("required_tables")
    required_tables = required_tables if isinstance(required_tables, dict) else {}
    report["doctor_check"] = {
        "returncode": report["cli"]["doctor"]["returncode"],
        "status": doc.get("status"),
        "errors": doc.get("errors"),
        "warnings": doc.get("warnings"),
        "required_tables": required_tables,
        "required_tables_embedding_failures": required_tables.get(LEDGER),
        "rc_acceptable": report["cli"]["doctor"]["returncode"] in (0, 1),
    }

    if args.expect == "red":
        # A red run is only meaningful when the base wheel bootstraps normally
        # and simply has no ledger. A bootstrap that FAILS proves nothing about
        # the ledger — it is an invalid run, never an expected red pass.
        if reasons:
            report["actual"] = "invalid"
            report["verdict"] = "INVALID"
            report["reasons"] = reasons
        elif (report["after"]["table_exists"]
                or report["after_second"]["table_exists"]):
            report["actual"] = "green"
            report["verdict"] = "FAIL"
            report["reasons"] = ["base wheel created the ledger table; this is "
                                 "not a red baseline"]
        else:
            report["actual"] = "red"
            report["verdict"] = "PASS"
    else:
        failures = list(reasons)
        present = report["after"]["table_exists"]
        if not present:
            failures.append("bootstrap did not create the ledger table")
        if missing_idx:
            failures.append(f"missing required indexes: {missing_idx}")
        if not report["observed"]["facts_stable_across_second_bootstrap"]:
            failures.append("table/index facts changed across the second "
                            "bootstrap; bootstrap is not idempotent here")

        if present and not missing_idx:
            report["writer_readback"] = _writer_readback(args.db_name)
        else:
            report["writer_readback"] = {
                "status": "NOT_RUN",
                "reason": "the first bootstrap did not create the ledger table "
                          "with every required index"
                          + (" (table missing)" if not present
                             else f" (missing indexes: {missing_idx})")
                          + "; the writer is never called to manufacture the "
                            "evidence it is meant to verify",
            }
        write = report["writer_readback"]
        if write.get("status") != "RAN" or write.get("writer_returned") is not True:
            failures.append("the real ledger writer did not report success")
        elif not write.get("row_found"):
            failures.append("no readable row after the writer call")
        else:
            row = dict(zip(("entity_table", "entity_id", "phase", "error_class",
                            "retryable", "attempts"), write["row"]))
            report["writer_readback"]["row_fields"] = row
            for field, wanted in (("entity_table", "qa_pairs"),
                                  ("entity_id", "f3probe-1"),
                                  ("phase", "f3_probe"),
                                  ("error_class", "EMBEDDING_TIMEOUT")):
                if row.get(field) != wanted:
                    failures.append(f"readback {field}={row.get(field)!r} != "
                                    f"{wanted!r}")
            if row.get("retryable") is not True:
                failures.append(f"readback retryable={row.get('retryable')!r} "
                                "!= True")
            report["writer_readback"]["attempts_measured"] = row.get("attempts")

        if report["doctor_check"]["required_tables_embedding_failures"] is not True:
            failures.append("doctor does not report the ledger as a required "
                            "table present")
        if report["doctor_check"]["errors"] != []:
            failures.append(f"doctor errors not empty: "
                            f"{report['doctor_check']['errors']!r}")
        if not report["doctor_check"]["rc_acceptable"]:
            failures.append("doctor rc is outside {0, 1}")

        report["actual"] = "green" if not failures else "red"
        report["verdict"] = "FAIL" if failures else "PASS"
        if failures:
            report["reasons"] = failures

    # ── never emit anything the password could be hiding inside ────────────
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    captured = "".join(
        f"{rec.get('stdout') or ''}\n{rec.get('stderr') or ''}"
        for rec in report["cli"].values())
    _leak_check({"serialized report": text, "captured CLI output": captured},
                password)
    out_path.write_text(text, encoding="utf-8")

    summary_keys = ("expectation", "actual", "verdict", "reasons", "observed",
                    "environment", "import", "doctor_check", "writer_readback",
                    "source")
    print(json.dumps({k: report[k] for k in summary_keys if k in report},
                     ensure_ascii=False, indent=2, sort_keys=True))
    if report["verdict"] == "PASS":
        return 0
    return 2 if report["verdict"] == "INVALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
