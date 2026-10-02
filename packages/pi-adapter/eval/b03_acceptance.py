"""B03 acceptance helper 2/2 — narrow orchestrator, DESKTOP-EQP3OBU only.

Scope: fresh disposable DB -> unchanged installed CLI bootstrap -> unchanged
bridge subprocess -> SIMULATED HOST (eval/isolated_host.mjs) -> independent PG
read-back plus an independent /tool v3_get source read. It does NOT run real
pi and does NOT run a model.

Refusal ladder (every rung is BEFORE a credential, a file write, a process or
a connection):
  1. hostname is exactly DESKTOP-EQP3OBU
  2. --db-name matches b03pi_* and --pg-port is 55432 on loopback
  3. every shared --secrets/--env-root/--node/--adapter path is inside the
     caller-declared dedicated root (--support-root, which defaults to --root)
  4. the secret file exists, is inside <support-root>/secrets, and is the only
     channel
  5. artifacts exist; --out must not already exist and must sit under the run
     root, which is either --support-root itself or an existing EMPTY child

The shared read-only inputs (the venv, Node, the extracted adapter, secrets/)
are resolved under --support-root, and every mutable artifact stays under the
run root. A retry therefore reads the original secret IN PLACE from a fresh,
empty --root instead of copying or moving it, and a failed run's evidence can
never be overwritten by reusing its root.

No DROP, no setval, no schema edits, no product monkeypatch, no fake core.
Cleanup covers EVERY post-spawn exit in one try/finally: only the owned Popen
handle is signalled, its exit is awaited with a bound, and a release timeout is
NOT_RELEASED + verdict FAIL. The source secrets file is read in place and never
copied; no second copy of the credentials is ever written. Evidence, the fresh
DB and the test rows are left; nothing is dropped.

The dedicated secret file is read in place and never copied. Exactly two
formats are accepted: a JSON OBJECT (the original contract) and a RAW-TEXT
password file whose non-empty contents become PGPASSWORD. Any other JSON
value is refused rather than reinterpreted as a password.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HOSTNAME = "DESKTOP-EQP3OBU"
PG_HOST, PG_PORT = "127.0.0.1", 55432
DB_PREFIX = "b03pi_"
READY_TIMEOUT, READBACK_TIMEOUT = 180.0, 120.0
#: Fixed expected native ids. The simulated host declares exactly these sets;
#: a mismatch or an empty set is a failure, never a vacuous pass.
EXPECT_RECORDABLE = ["b03A-0001", "b03A-0002", "b03A-0003", "b03A-0004"]
EXPECT_EXCLUDED = ["b03A-0005", "b03A-0006", "b03A-0007", "b03A-0008"]
STOP_TIMEOUT = 30.0
#: Recalled on a loopback bridge with no token and no external endpoint. Any
#: non-empty endpoint/credential is refused at write time.
# Empty embed model + endpoint selects v3core's documented keyword-only path.
MODEL_NAME = ""
SECRET_ENV_KEYS = ("PGPASSWORD", "V3CORE_PG_PASSWORD", "PGUSER")
DROP_PREFIXES = ("V3CORE_", "OPENAI_", "ANTHROPIC_", "EMBEDDING_", "HERMES_", "MINIMAX_")
DROP_SUFFIXES = ("_API_KEY", "_API_BASE", "_BASE_URL", "_PROXY", "_TOKEN")


def refuse(msg: str) -> None:
    sys.exit("REFUSED: " + msg)          # never carries credential material


#: Line-ending bytes stripped from a raw-text secret file. Nothing else is
#: trimmed: a password may legitimately begin or end with a space, and only the
#: file's trailing newline is transport noise, not part of the secret.
RAW_LINE_ENDINGS = "\r\n"


def load_secrets_map(secrets: Path, name: str | None = None) -> dict:
    """Read the ONE dedicated secret file in place and return {env key: value}.

    Two accepted formats, distinguished by the file itself — never by a flag,
    so a raw file and a JSON file are both consumable with the same --secrets:

      * a JSON OBJECT — the original contract. Only SECRET_ENV_KEYS are taken
        and only when truthy; a JSON value that is not an object (list, string,
        number, bool, null) is REFUSED rather than reinterpreted as a
        password, because silently reading "abc" as a bare password would
        accept a malformed secret file as a valid one.
      * RAW TEXT — anything else that is not valid JSON, e.g. a file holding
        only the password. Its non-empty contents become PGPASSWORD. An empty
        (or line-endings-only) raw file is REFUSED: an empty password would
        authenticate as nobody and turn a green check into a false pass.

    The value is used in memory and returned. It is never written to a derived
    file, never printed, never put in argv, and never hashed or logged — the
    source file stays the sole secret file and the sole channel. Refusal
    messages carry the file NAME and the failure type only, never contents.
    """
    label = name or getattr(secrets, "name", "<secrets>")
    try:
        text = secrets.read_text(encoding="utf-8")
    except Exception as exc:                       # unreadable/undecodable
        refuse(f"cannot read the dedicated secret file {label}: {type(exc).__name__}")
    try:
        secret_doc = json.loads(text)
    except Exception:
        secret_doc = None                          # not JSON: try the raw form
    else:
        if not isinstance(secret_doc, dict):
            refuse(f"{label} is a JSON {type(secret_doc).__name__}, not a JSON object "
                   "and not a raw-text password file; refusing to guess which it is")
    if secret_doc is None:
        # Only trailing line endings are removed. Leading and interior spaces
        # are part of the password and are preserved verbatim.
        password = text.rstrip(RAW_LINE_ENDINGS)
        if not password:
            refuse(f"{label} is a raw-text password file with no password in it "
                   "(empty, or line endings only)")
        return {"PGPASSWORD": password}
    secrets_map = {k: str(secret_doc[k]) for k in SECRET_ENV_KEYS if secret_doc.get(k)}
    if not secrets_map.get("PGPASSWORD"):
        refuse(f"{label} supplies no PGPASSWORD/V3CORE_PG_PASSWORD value")
    return secrets_map


def _apply_pg_password_alias(env: dict) -> None:
    """Mirror an in-memory PGPASSWORD into V3CORE_PG_PASSWORD. PURE: no I/O.

    The secret loader's raw-text contract is deliberately narrow — a raw file
    yields exactly PGPASSWORD — while v3core's runtime reads V3CORE_PG_PASSWORD.
    Without this alias the backend starts, reports pg=false and every query
    fails closed, so the two names must agree in the child's environment.

    Rules:
      * PGPASSWORD empty/absent      -> no alias is created at all. An empty
        password would authenticate as nobody, so it must never be published
        under a second name where it could look configured.
      * V3CORE_PG_PASSWORD non-empty  -> NEVER overridden; an explicit value
        always wins over the alias.
      * otherwise                     -> the same in-memory value is assigned
        to the second name. No second copy is written to disk, and the value
        is never printed, logged, added to a report or placed in argv.

    The Node-env filter already strips BOTH names (SECRET_ENV_KEYS), so the
    fixture harness still cannot persist either one.
    """
    pg = env.get("PGPASSWORD")
    if not pg:
        return
    if env.get("V3CORE_PG_PASSWORD"):
        return
    env["V3CORE_PG_PASSWORD"] = pg


def under(child: Path, root: Path) -> bool:
    c, r = os.path.normcase(os.path.abspath(str(child))), os.path.normcase(os.path.abspath(str(root)))
    if c == r:
        return True
    try:
        rel = os.path.relpath(c, r)
    except ValueError:
        return False
    return rel != os.pardir and not rel.startswith(os.pardir + os.sep)


def scrub(value, secrets: dict):
    """Remove every known secret string from anything headed for disk/stdout.

    A raw exception string (psycopg2, urllib, OSError) can embed a DSN, so it
    is sanitized before it reaches the report. Empty values are skipped so a
    blank secret cannot turn every string into '***'.
    """
    if isinstance(value, str):
        for secret in secrets.values():
            if secret:
                value = value.replace(secret, "***")
        return value
    if isinstance(value, dict):
        return {k: scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, secrets) for v in value]
    return value


def leak(text: str, secrets: dict) -> bool:
    return any(s and s in text for s in secrets.values())


def post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def prod_sentinel(home: Path) -> list[str]:
    """Read-only names-only fingerprint of the production profile tree."""
    root = home / ".v3-core"
    out: list[str] = []
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for n in sorted(files) + sorted(dirs):
            out.append(os.path.relpath(os.path.join(base, n), root).replace("\\", "/"))
    return sorted(out)


class Bail(Exception):
    """Stop the run but still emit evidence and release the owned subprocess."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def qa_reference_id(sid: str) -> int | None:
    """``qa_<n>`` -> n. Canonical ``qa_sync/...`` and every other id -> None."""
    s = str(sid or "")
    if s.startswith("qa_") and s[3:].isdigit():
        return int(s[3:])
    return None


def decide_source_trace(sid: str, pg_session_id, file_session_id, traced, trace_error,
                        readback_failed, expected_recordable=None,
                        expected_host: str = "pi",
                        qa_reference: int | None = None,
                        expected_fixture_key: str | None = None) -> dict:
    """Decide the PG source trace. PURE: no I/O, no DB, no network.

    Split out of main() so the fail-closed rules can be exercised against mocked
    query rows without a database or a psycopg2 connection.

    THREE id namespaces exist and none may be guessed: conversation_stream.id
    (numeric surrogate), the canonical identity triple (host,
    metadata.session_id, event_id), and qa_pairs (a recalled QA fact, reached
    by the ``qa_<row id>`` reference the recall engine itself printed).
    event_id ALONE is not a key here — it is unique per session, not globally —
    so it is never queried on its own.

    Rules, in order:
      * a failed general read-back  -> NOT_ATTEMPTED. The trace was never
        queried; the read-back failure is already an explicit FAIL, so this
        adds no second reason and never mislabels the trace as NOT_FOUND.
      * no PG metadata.session_id  -> NOT_AVAILABLE, with no query at all. This
        is the allowed file-backed {session_id} answer; there is no PG identity
        to look up, and nothing is substituted.
      * a query error               -> ERROR, an explicit FAIL.
      * a query that resolves no row -> NOT_FOUND, an explicit FAIL. An
        unresolved PG source reference fails CLOSED: without a row there is no
        independent DB identity trace, so reporting NOT_FOUND while adding no
        reason would let a valid /tool response yield an overall PASS.
      * a qa_pairs row that resolves -> the row's OWN embedded native identity
        is verified (its session_id, that it carries this run's fixture fact,
        and that its canonical qa_sync/host/session/event source_id splits into
        a full triple) instead of taking the core's word for it. Any gap fails
        CLOSED, so an id that resolved to a different row is a mismatch.
      * a conversation_stream row that resolves -> cross-checked against the
        fixed contract: host, the session the core itself reported, and the
        driver's fixed recordable id set. The event-id check applies to BOTH
        the numeric and the native id path, not just the numeric one.
    """
    want_recordable = list(EXPECT_RECORDABLE if expected_recordable is None
                           else expected_recordable)
    out: dict = {"db_trace": "NOT_ATTEMPTED", "namespace": None,
                 "native_identity": None, "reasons": []}

    if readback_failed:
        out["namespace"] = "not attempted (the general read-back already failed)"
        return out

    if not pg_session_id:
        out["db_trace"] = "NOT_AVAILABLE"
        out["namespace"] = "core file-backed source metadata"
        out["file_backed_session_id"] = str(file_session_id)[:200]
        return out

    out["namespace"] = ("conversation_stream.id" if str(sid).isdigit()
                        else "conversation_stream(host,session_id,event_id)")
    if trace_error:
        out["db_trace"] = "ERROR"
        out["db_trace_error"] = str(trace_error)[:200]
        out["reasons"].append(
            f"the PG source trace for {sid!r} errored: {out['db_trace_error']} — a trace "
            f"error is a FAIL, not an untraced success")
        return out

    if not traced:
        out["db_trace"] = "NOT_FOUND"
        out["reasons"].append(
            f"the PG source trace for {sid!r} returned no row in {out['namespace']}; the core "
            f"reported metadata.session_id {str(pg_session_id)[:200]!r} but the exact identity "
            f"does not resolve, so there is no independent DB identity trace — that fails closed")
        return out

    # ── qa_pairs 命名空间 ──────────────────────────────────────────────
    # qa_<n> 是引擎打印的 qa_pairs 引用（qa_pairs.id），不是 conversation_stream
    # 的 id：这一支解析到 qa_pairs 行，并核验**该行自己内嵌的**规范身份，
    # 而不是采信 core 报上来的 session_id。任一项不符即 fail closed。
    if qa_reference is not None:
        out["namespace"] = "qa_pairs.id (row) -> canonical qa_pairs.source_id native identity"
        row_source, row_session, row_question, row_answer = (
            traced[0][0], traced[0][1], traced[0][2], traced[0][3])
        qa_mismatch: list[str] = []
        if str(row_session) != str(pg_session_id):
            qa_mismatch.append(f"the qa row's session_id {row_session!r} is not the core-reported "
                               f"metadata.session_id {str(pg_session_id)[:200]!r}")
        if expected_fixture_key and expected_fixture_key not in f"{row_question} {row_answer}":
            qa_mismatch.append(f"the qa row resolved for {sid!r} does not carry this run's "
                               f"fixture fact {expected_fixture_key!r} — the id resolved to a "
                               f"different row")
        parts = str(row_source or "").split("/")
        if len(parts) < 4 or parts[0] != "qa_sync":
            qa_mismatch.append(f"the qa row's canonical source_id {row_source!r} embeds no "
                               f"qa_sync/host/session_id/event_id native identity triple")
        else:
            if parts[1] != expected_host:
                qa_mismatch.append(f"the qa row's source_id host part is {parts[1]!r}, not the "
                                   f"expected {expected_host!r}")
            if parts[2] != str(pg_session_id):
                qa_mismatch.append(f"the qa row's source_id session part is {parts[2]!r}, not the "
                                   f"core-reported metadata.session_id "
                                   f"{str(pg_session_id)[:200]!r}")
            if parts[3] not in want_recordable:
                qa_mismatch.append(f"the qa row's source_id event part {parts[3]!r} is not one of "
                                   f"this driver's fixed recordable ids {want_recordable}")
        if qa_mismatch:
            out["db_trace"] = "IDENTITY_MISMATCH"
            out["reasons"].append(
                f"the qa row traced for {sid!r} does not match the expected identity: "
                + "; ".join(qa_mismatch))
            return out
        out["db_trace"] = "FOUND"
        out["native_identity"] = {"host": parts[1], "session_id": parts[2],
                                  "event_id": parts[3]}
        out["core_reported_session_id"] = str(pg_session_id)[:200]
        return out

    row_host, row_session, row_event = traced[0][0], traced[0][1], traced[0][2]
    mismatch: list[str] = []
    if row_host != expected_host:
        mismatch.append(f"row host is {row_host!r}, not the expected {expected_host!r}")
    if str(row_session) != str(pg_session_id):
        mismatch.append(f"row session_id is {row_session!r}, not the core-reported "
                        f"metadata.session_id {pg_session_id!r}")
    # Applied to BOTH id namespaces. In the numeric path the row is found by
    # conversation_stream.id, and in the native path by the composite identity;
    # neither may land on an id outside the driver's fixed recordable set.
    if row_event not in want_recordable:
        mismatch.append(f"row event_id {row_event!r} is not one of this driver's fixed "
                        f"recordable ids {want_recordable}")
    out["native_identity"] = {"host": row_host, "session_id": row_session,
                              "event_id": row_event}
    out["core_reported_session_id"] = str(pg_session_id)[:200]
    if mismatch:
        out["db_trace"] = "IDENTITY_MISMATCH"
        out["reasons"].append(
            f"the row traced for {sid!r} does not match the expected identity: "
            + "; ".join(mismatch))
    else:
        out["db_trace"] = "FOUND"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="B03 acceptance orchestrator (SIMULATED HOST).")
    for flag in ("root", "secrets", "env-root", "node", "adapter", "db-name", "out",
                 "pg-user", "fixture-key", "fixture-decision"):
        ap.add_argument(f"--{flag}", default="" if flag in {"pg-user", "fixture-key",
                                                            "fixture-decision"} else None,
                        required=flag not in {"pg-user", "fixture-key", "fixture-decision"})
    ap.add_argument("--support-root", default=None,
                    help="dedicated root holding the SHARED read-only inputs "
                         "(secrets/, --env-root, --node, --adapter). Defaults to "
                         "--root for backwards compatibility. --root stays the "
                         "run-artifact root; --root may be --support-root itself "
                         "or an existing EMPTY child of it, so a retry can read "
                         "the original secret in place and never overwrite a "
                         "previous run's evidence.")
    ap.add_argument("--pg-port", type=int, default=PG_PORT)
    ap.add_argument("--timeout-ms", type=int, default=20000)
    ap.add_argument("--memory-budget-chars", type=int, default=8000)
    args = ap.parse_args()
    args.pg_user = args.pg_user or "f2e2e"
    args.fixture_key = args.fixture_key or "synthetic B03 fact: the deploy window is on Thursdays"
    args.fixture_decision = args.fixture_decision or "deploy window"

    # ── 1. machine gate (before anything else) ──────────────────────────────
    host = socket.gethostname().upper()
    if host != HOSTNAME:
        refuse(f"hostname {host!r} is not the dedicated laptop {HOSTNAME!r}; this "
               "orchestrator touches a disposable database and a spawned bridge")

    # ── 2. closed target allowlist ─────────────────────────────────────────
    if not args.db_name.startswith(DB_PREFIX):
        refuse(f"--db-name must start with {DB_PREFIX!r} (got {args.db_name!r})")
    if args.pg_port != PG_PORT:
        refuse(f"--pg-port must be {PG_PORT} (got {args.pg_port})")
    if len(args.db_name) <= len(DB_PREFIX) or not args.db_name.replace("_", "").isalnum():
        refuse(f"--db-name {args.db_name!r} is not a well-formed disposable name")

    # ── 3. dedicated root containment ──────────────────────────────────────
    # Two roots: support_root holds the SHARED read-only inputs (venv, Node,
    # extracted adapter, secrets/) and never changes between retries; root is
    # the RUN-ARTIFACT root and receives every mutable output. Same root is the
    # legacy single-root layout; a fresh empty child is the retry layout that
    # keeps a failed run's evidence intact.
    root = Path(args.root).resolve()
    support_root = Path(args.support_root).resolve() if args.support_root else root
    if not support_root.is_dir():
        refuse(f"--support-root {support_root} is not a directory")
    same_root = os.path.normcase(str(root)) == os.path.normcase(str(support_root))
    if not same_root:
        if os.path.normcase(str(root.parent)) != os.path.normcase(str(support_root)):
            refuse(f"--root {root} must be --support-root {support_root} itself or a "
                   f"direct child of it; a fresh run root elsewhere cannot reach the "
                   f"shared inputs or the in-place secret")
        if not root.is_dir():
            refuse(f"--root {root} must already exist as the fresh run root; this "
                   f"orchestrator never creates it, so a typo cannot silently "
                   f"scatter artifacts outside the declared root")
        try:
            occupied = sorted(p.name for p in root.iterdir())
        except OSError as exc:
            refuse(f"--root {root} is not readable: {type(exc).__name__}")
        if occupied:
            refuse(f"the fresh run root {root} is not empty (holds {occupied}); "
                   f"refusing to reuse or overwrite a previous run's evidence — "
                   f"supply a new empty --root")
    if not root.is_dir():
        refuse(f"--root {root} is not a directory")
    secrets_dir = (support_root / "secrets").resolve()
    secrets = Path(args.secrets).resolve()
    for label, path in (("--secrets", secrets), ("--env-root", Path(args.env_root).resolve()),
                        ("--node", Path(args.node).resolve()), ("--adapter", Path(args.adapter).resolve())):
        if not under(path, support_root):
            refuse(f"{label} {path} is OUTSIDE the caller-declared dedicated B03 root "
                   f"{support_root}")
    if not under(secrets, secrets_dir) or not secrets.is_file():
        refuse(f"--secrets must be an existing file inside {secrets_dir}")

    # ── 4. artifacts, no silent overwrite ──────────────────────────────────
    env_root, node_exe = Path(args.env_root).resolve(), Path(args.node).resolve()
    adapter = Path(args.adapter).resolve()
    host_script = (Path(__file__).resolve().parent / "isolated_host.mjs")
    cli = env_root / ("Scripts" if os.name == "nt" else "bin") / (
        "hippocampus.exe" if os.name == "nt" else "hippocampus")
    for label, path in (("--env-root", env_root), ("--node", node_exe), ("--adapter", adapter),
                        ("isolated_host.mjs", host_script), ("installed CLI", cli)):
        if not path.exists():
            refuse(f"{label} artifact {path} does not exist")
    out_path = Path(args.out).resolve()
    if not under(out_path, root):
        refuse(f"--out {out_path} must be inside the run root {root}; a report written "
               f"outside the run root is not this run's evidence and would silently "
               f"escape the dedicated root")
    if out_path.exists():
        refuse(f"report {out_path.name} already exists; refusing to overwrite evidence")
    if same_root:
        # Legacy single-root layout: the run root also holds the shared inputs,
        # so it cannot be required to be empty. Instead every RUN-OWNED path is
        # checked by name, which is what a retry must not clobber.
        run_owned = ("home", "data", "profile", "bridge.log", "hippocampus.json",
                     "isolated-host.json")
        stale = sorted(n for n in run_owned if (root / n).exists())
        if stale:
            refuse(f"the run root {root} already holds run-owned artifacts {stale}; "
                   f"refusing to overwrite previous evidence — use a fresh empty "
                   f"--root (a child of this --support-root) for the retry")

    # ── 5. credentials: this file only, never printed/argv/repo/copied ─────
    # JSON object or raw-text password file; read in place, never copied, and
    # the returned value is used in memory only. Rung 4 already proved this is
    # the sole secret file and the sole channel.
    secrets_map = load_secrets_map(secrets, secrets.name)
    report: dict = {
        "helper": "b03_acceptance",
        "scope": "SIMULATED HOST / REAL BACKEND — not a real pi E2E, not model-backed",
        "real_pi_started": False, "provider_calls": "none (no token, no external endpoint)",
        "hostname": host,
        "target": {"host": PG_HOST, "port": PG_PORT, "user": args.pg_user,
                   "database": args.db_name, "password_in_output": False},
        "root": str(root), "support_root": str(support_root),
        "secrets_file": str(secrets), "secret_read_in_place": True,
        "secret_copied": False,
        "credentials": {"keys_loaded": sorted(secrets_map), "printed": False,
                        "in_argv": False, "in_repo": False, "copied": False},
    }

    # ── 6. env isolation: own HOME/profile, no ambient provider path ───────
    home = (root / "home").resolve()
    data_dir = (root / "data").resolve()
    for d in (home, data_dir):
        d.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not (k.upper().startswith(DROP_PREFIXES) or k.upper().endswith(DROP_SUFFIXES))}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "HERMES_HOME": str(home),
                "V3CORE_HOME": str(home), "V3CORE_TEST_MODE": "1", "PYTHONPATH": "",
                "PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
    env.update(secrets_map)
    # v3core's runtime reads V3CORE_PG_PASSWORD; a raw-text secret file yields
    # only PGPASSWORD. Alias the SAME in-memory value under the second name
    # here, after the secrets merge and before any subprocess is spawned, so the
    # backend, the bootstrap CLI and the bridge all see one password. Nothing is
    # written, printed or added to argv; the Node filter still strips both names.
    _apply_pg_password_alias(env)
    report["roots"] = {"run_root": str(root), "support_root": str(support_root),
                       "same_root": same_root,
                       "shared_inputs_under_support_root": True,
                       "mutable_artifacts_under_run_root": True}
    report["environment"] = {"home": str(home), "data_dir": str(data_dir),
                             "isolated_from_production": True,
                             "pg_alias_applied": (
                                 bool(secrets_map.get("PGPASSWORD"))
                                 and not secrets_map.get("V3CORE_PG_PASSWORD")),
                             "cleared_prefixes": list(DROP_PREFIXES),
                             "cleared_suffixes": list(DROP_SUFFIXES)}

    # explicit config; basePath redirects every _resolve_data_dir under TEST
    # mode to <root>/data — the topic default, the outbox and the observer
    # state all follow it. No ambient .env, no production profile.
    profile_dir = (root / "profile").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    config_path = profile_dir / "config.yaml"
    import yaml
    config_path.write_text(yaml.safe_dump({
        "basePath": str(data_dir), "mode": "cloud",
        "storage": {
            "pg": {"host": PG_HOST, "port": PG_PORT, "database": args.db_name,
                   "user": args.pg_user},                       # no password key
            "embed": {"endpoint": "", "model": MODEL_NAME, "dim": 1024,
                      "apiKey": "", "api_key": "", "proxy": ""},
            "rerank": {"endpoint": "", "model": "", "proxy": "", "timeout": 30,
                       "apiKey": "", "api_key": ""},
        },
        "e1": {"enabled": False}, "observer": {"enabled": False}, "tkg": {"enabled": False},
        "llm": {"provider": "", "model": MODEL_NAME, "api_key": "", "base_url": "",
                "thinking": True, "max_tokens": 0},
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    env["V3CORE_CONFIG"] = str(config_path)
    report["core_config"] = {"path": str(config_path), "base_path": str(data_dir),
                             "under_root": under(config_path, root),
                             "credential_fields_empty": True,
                             "external_endpoint": False,
                             "embedding_mode": "keyword_only_disabled"}
    sentinel_before = prod_sentinel(Path.home())

    # ── 7. one never-existing database, then the unchanged installed CLI ───
    import psycopg2
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, database="postgres",
                            user=args.pg_user, password=secrets_map["PGPASSWORD"],
                            connect_timeout=10)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (args.db_name,))
            if cur.fetchone() is not None:
                refuse(f"database {args.db_name!r} already exists; no DROP, no reuse, "
                       "no alter — supply a fresh b03pi_* name")
            cur.execute(f'CREATE DATABASE "{args.db_name}"')     # the only DDL we issue
    finally:
        conn.close()
    report["db_created_fresh"] = True

    dsn = f"postgresql://{args.pg_user}@{PG_HOST}:{PG_PORT}/{args.db_name}"   # no password
    boot = subprocess.run([str(cli), "bootstrap", "--target", dsn], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env,
                          cwd=str(root))
    try:
        payload = json.loads(boot.stdout)
    except Exception:
        payload = {}
    inner = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    target = inner.get("target") if isinstance(inner.get("target"), dict) else {}
    report["bootstrap"] = {
        "argv": ["hippocampus", "bootstrap", "--target", dsn],
        "returncode": boot.returncode, "applied": inner.get("applied"),
        "upgrade_applied": inner.get("upgrade_applied"),
        "include_expanded": inner.get("include_expanded"),
        "sql_sha256": payload.get("alpha_bootstrap_sql_sha256"),
        "target_matches_guarded": (target.get("host") == PG_HOST
                                   and int(target.get("port") or 0) == PG_PORT
                                   and target.get("database") == args.db_name),
        "password_redacted_by_cli": target.get("password") == "***",
        "stderr": (boot.stderr or "")[-400:],
    }
    if not (boot.returncode == 0 and inner.get("applied") is True
            and report["bootstrap"]["target_matches_guarded"]):
        report["verdict"] = "FAIL"
        report["reasons"] = ["the official installed CLI bootstrap did not report a "
                             "guarded, applied result; the fresh DB has no schema to "
                             "read back, so the run stops here"]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(scrub(report, secrets_map), ensure_ascii=False,
                                       indent=2), encoding="utf-8")
        print(json.dumps({"verdict": "FAIL", "reasons": report["reasons"]}, indent=2))
        return 1

    # ── 8. unchanged bridge as an OWNED subprocess (port 0, ready_json) ────
    # From here to the end EVERY exit runs through the finally below: the owned
    # handle is the only thing ever signalled and its exit is always awaited.
    #
    # The bridge's stderr is SCRUBBED THROUGH THE PUMP, never redirected raw:
    # a psycopg2/urllib traceback can embed the DSN password, and a redirected
    # file descriptor writes those bytes to bridge.log the instant they arrive,
    # before any post-hoc scrub could ever see them. Raw text is never written.
    bridge_log = (root / "bridge.log").open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "v3core", "serve", "--host", PG_HOST, "--port", "0",
         "--profile", "b03", "--ready-json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(root),
        text=True, encoding="utf-8", errors="replace")
    reasons: list[str] = []
    node_returncode: int | None = None
    node_report: dict = {}
    base = ""
    try:
        ready: dict = {}
        bridge_stderr_scrubbed = 0

        def _pump() -> None:
            for line in proc.stdout:                     # type: ignore[union-attr]
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("event") == "ready" and not ready:
                    ready.update(data)

        def _pump_err() -> None:
            nonlocal bridge_stderr_scrubbed
            for line in proc.stderr:                     # type: ignore[union-attr]
                text = line if isinstance(line, str) else str(line)
                if leak(text, secrets_map):
                    bridge_stderr_scrubbed += 1
                bridge_log.write(scrub(text, secrets_map))   # scrubbed BEFORE the write

        threading.Thread(target=_pump, daemon=True).start()
        threading.Thread(target=_pump_err, daemon=True).start()
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline and not ready.get("port"):
            if proc.poll() is not None:
                raise Bail(f"the unchanged bridge exited (rc={proc.returncode}) before "
                           "reporting readiness; see bridge.log")
            time.sleep(0.1)
        if not ready.get("port"):
            # fail BEFORE any URL is formed from a None port
            raise Bail(f"the unchanged bridge did not report a ready port within "
                       f"{READY_TIMEOUT:.0f}s")
        base = f"http://{PG_HOST}:{ready['port']}"
        try:
            health = get(f"{base}/health", timeout=30)
        except Exception as exc:
            health = {"error": f"{type(exc).__name__}: {exc}"}
        report["bridge"] = {"pid": proc.pid, "url": base, "ready_port": ready.get("port"),
                            "profile": ready.get("profile"),
                            "bridge_protocol_version": health.get("bridge_protocol_version"),
                            "pg_ok": health.get("pg"), "embed_available": health.get("embed"),
                            "error": health.get("error")}
        if health.get("embed") is not False:
            raise Bail("keyword-only acceptance requires v3core health embed=false")
        if health.get("bridge_protocol_version") != "b01.1":
            raise Bail(f"bridge handshake bridge_protocol_version="
                       f"{health.get('bridge_protocol_version')!r} != 'b01.1'")

        # ── 9. external-mode adapter config + the Node fixture ─────────────
        # No secret copy: the CLI already read the caller's secrets file and the
        # bridge holds the password in its own env.
        adapter_cfg = root / "hippocampus.json"
        adapter_cfg.write_text(json.dumps({
            "enabled": True, "mode": "external", "bridgeUrl": base,
            "timeoutMs": int(args.timeout_ms),
            "memoryBudgetChars": int(args.memory_budget_chars),
        }, indent=2), encoding="utf-8")
        # The Node harness needs NO credential: it talks to the loopback bridge,
        # which holds the password in its own env. Stripping them here means the
        # harness's own JSON report cannot persist a PG secret even if the
        # adapter echoed its environment into an error message.
        node_env = {k: v for k, v in env.items() if k.upper() not in SECRET_ENV_KEYS}
        report["host_fixture_credentials"] = {
            "pg_keys_present_in_node_env": sorted(k for k in node_env if k.upper() in SECRET_ENV_KEYS),
            "stripped_keys": sorted(k for k in env if k.upper() in SECRET_ENV_KEYS),
        }
        node_out = root / "isolated-host.json"
        node = subprocess.run(
            [str(node_exe), str(host_script), "--adapter", str(adapter), "--config",
             str(adapter_cfg), "--out", str(node_out), "--fixture-key", args.fixture_key,
             "--fixture-decision", args.fixture_decision],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=node_env,
            cwd=str(root))                             # no shell, no pipe: rc is real
        node_returncode = node.returncode
        try:
            node_report = json.loads(node_out.read_text(encoding="utf-8"))
        except Exception:
            node_report = {"unreadable": True}
        report["host_fixture"] = {"returncode": node.returncode, "checks": node_report.get("checks"),
                                  "failures": node_report.get("failures"),
                                  "order": node_report.get("order"),
                                  "injections": {k: v.get("injections")
                                                 for k, v in (node_report.get("sessions") or {}).items()},
                                  "source_trace": (node_report.get("sessions") or {}).get(
                                      "b03-session-b", {}).get("source_trace"),
                                  "stdout": (node.stdout or "")[-600:],
                                  "stderr": (node.stderr or "")[-600:]}
        expected = node_report.get("expected") or {}
        node_recordable = list(expected.get("session_a", {}).get("recordable_event_ids") or [])
        node_excluded = list(expected.get("session_a", {}).get("must_be_absent_event_ids") or [])
        # The driver owns the expected sets; the host only mirrors them.
        if not EXPECT_RECORDABLE or not EXPECT_EXCLUDED:
            raise Bail("the driver's fixed expected id sets are empty; refusing to report a "
                       "vacuous pass")
        if node_recordable != EXPECT_RECORDABLE or node_excluded != EXPECT_EXCLUDED:
            reasons.append(f"the simulated host declared recordable={node_recordable} "
                           f"excluded={node_excluded} but this driver fixes "
                           f"recordable={EXPECT_RECORDABLE} excluded={EXPECT_EXCLUDED}")
        want_ids, deny_ids = set(EXPECT_RECORDABLE), set(EXPECT_EXCLUDED)
        sid = (node_report.get("source_reference") or "").strip()
        report["recall_scope"] = node_report.get("recall_scope")
        report["expected"] = {"recordable_event_ids": EXPECT_RECORDABLE,
                              "must_be_absent_event_ids": EXPECT_EXCLUDED}

        # ── 10. independent read-back: Node output is not PG success ───────
        def query(sql, params):
            c = psycopg2.connect(host=PG_HOST, port=PG_PORT, database=args.db_name,
                                 user=args.pg_user, password=secrets_map["PGPASSWORD"],
                                 connect_timeout=10)
            try:
                with c.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall()
            finally:
                c.close()

        rows, found, excluded, qa = [], set(), [], []
        readback_error: str | None = None
        try:
            rows = query("SELECT host, session_id, event_id, role FROM conversation_stream "
                         "WHERE session_id=%s ORDER BY event_id", ("b03-session-a",))
            found = {r[2] for r in rows}
            excluded = query("SELECT event_id FROM conversation_stream "
                             "WHERE event_id = ANY(%s)", (sorted(deny_ids),))
            qa = query("SELECT source_id, question, answer FROM qa_pairs "
                       "WHERE question LIKE %s", (f"%{args.fixture_decision}%",))
        except Exception as exc:
            readback_error = f"{type(exc).__name__}: {exc}"
        if readback_error:
            # a PG error is an explicit FAIL, never a silently optional field
            reasons.append(f"the independent PG read-back failed: {readback_error}")
            report["readback_error"] = readback_error
        report["readback"] = {
            "host": "pi", "session_id": "b03-session-a",
            "rows": [{"host": r[0], "session_id": r[1], "event_id": r[2], "role": r[3]}
                     for r in rows],
            "recordable_ids_present": sorted(want_ids & found),
            "recordable_ids_missing": sorted(want_ids - found),
            "foreign_host_rows": sorted({r[0] for r in rows} - {"pi"}),
            "excluded_ids_present": [r[0] for r in excluded],
            "qa_pairs_matching_fixture": [{"source_id": r[0], "question": (r[1] or "")[:200],
                                           "answer": (r[2] or "")[:200]} for r in qa],
        }

        # ── 10b. the source read, in the two id namespaces ─────────────────
        # conversation_stream.id (numeric) and the native identity triple
        # (host, session_id, event_id) are DIFFERENT things. An arbitrary id
        # returned by the core is NOT assumed to be an event_id: a numeric id
        # resolves conversation_stream.id, a native identity resolves only when
        # the source id IS that event id, and anything else is reported
        # NOT_AVAILABLE rather than silently substituted with another row.
        report["source_read"] = {"source_id": sid, "namespaces": {}, "db_trace": "NOT_ATTEMPTED"}
        if sid:
            core_response: dict | None = None
            core_error: str | None = None
            try:
                tool = post(f"{base}/tool",
                            {"name": "v3_get",
                             "args": {"target": "message", "source_id": sid}},
                            timeout=30)
                if tool.get("ok") is not True:
                    core_error = f"the bridge reported ok={tool.get('ok')!r}"
                else:
                    try:
                        core_response = json.loads(str(tool.get("result")))
                    except Exception as exc:
                        core_error = f"the result is not JSON: {type(exc).__name__}"
            except Exception as exc:
                core_error = f"{type(exc).__name__}: {exc}"
            report["source_read"]["core_response"] = core_response
            report["source_read"]["core_error"] = core_error
            if core_error:
                reasons.append(f"the core source read for {sid!r} failed: {core_error} — a "
                               "core read failure is a BLOCKER, not a green run")
            elif not isinstance(core_response, dict) or core_response.get("success") is not True:
                reasons.append(f"the core source read for {sid!r} did not report success:true "
                               f"(error={str((core_response or {}).get('error'))[:200]!r}) — a "
                               "core read failure is a BLOCKER")
            elif core_response.get("source_id") != sid:
                reasons.append(f"the core source read answered for "
                               f"{str(core_response.get('source_id'))!r} instead of {sid!r}")
            elif args.fixture_key not in str(core_response.get("content") or ""):
                reasons.append(f"the content the core returned for {sid!r} does not carry this "
                               "source's fixture fact")
            else:
                # Where does this source actually live? Two id namespaces exist
                # and neither may be guessed: conversation_stream.id (numeric
                # surrogate) and the canonical identity triple
                # (host, metadata.session_id, event_id). event_id ALONE is not a
                # key here — it is unique per session, not globally — so it is
                # never queried on its own. The decision itself, including every
                # fail-closed rule, lives in the pure decide_source_trace().
                traced: list = []
                metadata = core_response.get("metadata")
                pg_session_id = (metadata.get("session_id") if isinstance(metadata, dict) else None) or None
                file_session_id = core_response.get("session_id") or None
                trace_error: str | None = None
                qa_ref: int | None = None
                if readback_error is not None:
                    # the read-back already failed; the trace is not attempted at
                    # all, so the helper reports NOT_ATTEMPTED rather than a
                    # NOT_FOUND it never earned. No query is issued.
                    pass
                elif not pg_session_id:
                    # Allowed file-backed answer: {success, source_id, session_id,
                    # content} with no PG metadata. There is no PG identity to
                    # query, so NOTHING is substituted — no event_id-only probe.
                    pass
                else:
                    try:
                        qa_ref = qa_reference_id(sid)
                        if qa_ref is not None:
                            # qa_1 是 qa_pairs 引用（引擎打印的那个 id），
                            # 不是 conversation_stream 的 id：查 qa_pairs 自身，
                            # 不替换成别的行。
                            traced = query("SELECT source_id, session_id, question, answer "
                                           "FROM qa_pairs WHERE id = %s", (qa_ref,))
                        elif sid.isdigit():
                            # numeric ⇒ conversation_stream.id, and ONLY that.
                            traced = query("SELECT host, session_id, event_id, role "
                                           "FROM conversation_stream WHERE id = %s", (int(sid),))
                        else:
                            # native ⇒ the full composite identity, never event_id alone.
                            traced = query(
                                "SELECT host, session_id, event_id, role FROM conversation_stream "
                                "WHERE host=%s AND session_id=%s AND event_id=%s",
                                ("pi", pg_session_id, sid))
                    except Exception as exc:
                        # an explicit, sanitized FAIL — never an uncaught traceback
                        # and never a silent "unavailable" that reads like a pass.
                        trace_error = f"{type(exc).__name__}: {exc}"
                decision = decide_source_trace(
                    sid, pg_session_id, file_session_id, traced, trace_error,
                    readback_failed=readback_error is not None,
                    qa_reference=qa_ref,
                    expected_fixture_key=args.fixture_key or None)
                for key in ("db_trace", "namespace", "native_identity",
                            "file_backed_session_id", "core_reported_session_id",
                            "db_trace_error"):
                    if key in decision:
                        report["source_read"][key] = scrub(decision[key], secrets_map)
                reasons.extend(scrub(r, secrets_map) for r in decision["reasons"])
                report["source_read"]["native_identity_resolved"] = bool(traced)

        # ── 11. verdict ────────────────────────────────────────────────────
        if node_returncode != 0:
            reasons.append(f"the simulated-host helper exited {node_returncode}")
        for item in node_report.get("failures") or []:
            reasons.append(f"host check {item.get('check')} failed: {str(item.get('detail'))[:220]}")
        if report["readback"]["recordable_ids_missing"]:
            reasons.append(f"PG is missing native event ids: {report['readback']['recordable_ids_missing']}")
        if report["readback"]["excluded_ids_present"]:
            reasons.append(f"excluded entries reached PG: {report['readback']['excluded_ids_present']}")
        if report["readback"]["foreign_host_rows"]:
            reasons.append(f"rows with a non-pi host: {report['readback']['foreign_host_rows']}")
        if not qa:
            reasons.append("no qa_pairs row carries the fixture fact; recall cannot be claimed")
        if "keyword" not in str(report.get("recall_scope", "")):
            reasons.append(f"recall was not demonstrated in the keyword-only scope: "
                           f"{report.get('recall_scope')!r}")
        if prod_sentinel(Path.home()) != sentinel_before:
            reasons.append("the production profile tree changed during the run")
    except Bail as exc:
        reasons.append(exc.reason)
    finally:
        # owned-handle release only; bounded wait; no PID hunting, no restarts
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=STOP_TIMEOUT)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=STOP_TIMEOUT)
            except Exception:
                pass
        bridge_log.close()
        released = proc.poll() is not None
        report.setdefault("bridge", {"pid": proc.pid})
        report["bridge"].update({"stopped": released, "residual": [] if released
                                 else ["the owned bridge handle is still running"],
                                 "release": "RELEASED" if released else "NOT_RELEASED",
                                 "stderr_log": str((root / "bridge.log").name),
                                 "stderr_scrubbed_before_write": True,
                                 "stderr_lines_with_secrets_scrubbed": bridge_stderr_scrubbed})
        if not released:
            # never a PASS with an unreleased subprocess
            reasons.append("the owned bridge subprocess was NOT_RELEASED within the bound; "
                           "no other process was signalled")

    report["acceptance"] = {
        "simulated_host_real_backend": not reasons,
        "real_pi_a_to_b": "NOT_RUN — AUTH BLOCKED on the laptop's default pi install; "
                          "a named gate, not a pass",
        "model_backed_e2e": "NOT_RUN — no token, no external endpoint, by design",
    }
    report["verdict"] = "FAIL" if reasons else "PASS"
    if reasons:
        report["reasons"] = reasons

    # ── 12. never emit anything a secret could be hiding inside ────────────
    report = scrub(report, secrets_map)
    if leak(json.dumps(report, ensure_ascii=False), secrets_map):
        refuse("credential material would be written into the report; refusing to "
               "emit it (no value shown, report not written)")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "reasons": report.get("reasons", []),
                      "readback": report.get("readback", "NOT_REACHED"),
                      "source_read": report.get("source_read", "NOT_REACHED"),
                      "acceptance": report["acceptance"]}, ensure_ascii=False, indent=2))
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main())
