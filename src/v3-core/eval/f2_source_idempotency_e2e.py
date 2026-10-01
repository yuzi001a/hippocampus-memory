"""F2-C/D — canonical source idempotency, real-HTTP W0-W4 destructive harness.

TRACKED acceptance harness. Everything it needs at run time lives in this
module: there is no dependency on the ignored ``scripts/`` tree, and no
helper, proxy, barrier or child role is imported from anywhere else. Run it
with the interpreter that has ``v3-core`` installed:

    python -m eval.f2_source_idempotency_e2e make \\
        --root C:/hp-testbed/f2-overnight-20261002/run \\
        --pg-port 55432 --output <evidence.json>

    python -m eval.f2_source_idempotency_e2e make \\
        --root <run-dir> --output <evidence.json>      # DSN from $F2_PG_DSN

Child roles (``child-serve`` / ``child-client``) are spawned by this module
and are tracked with it.

=============================================================================
MATRIX — mission §6, verbatim
=============================================================================

Canonical identity is ``(host, session_id, event_id)``. §6 defines the five
cases as crash windows and recovery cycles. The earlier, differently
labelled results (transport-fault, multi-host, concurrency) are NOT
relabelled to match — this file implements §6's own definitions, and the
``W0``/``W3`` pair keeps its asserted distinction.

The write order that makes the windows observable, in
:meth:`LiveBuffer._flush`:

    PG commit  →  accepted tombstone  →  pending marker removed

  W0  caller gets NO ACK; the serving process dies BEFORE the PG commit,
      then a fresh process retries the identical identity.
      Expected final state: exactly 1 row.
      Barriering: a test-only seam inside the CHILD blocks the live-writer
      flush immediately before the insert. The harness observes the
      barrier, reads PG (must be 0 rows) and hard-kills the tree. Phase
      recorded as ``before_commit``.

  W1  the PG commit lands BEFORE the accepted tombstone.  The process dies
      in the window where the row is durably committed and no identity
      tombstone exists yet; a fresh process must converge to 1 row.
      Barriering: a test-only seam parks ``_ack_live_item`` AFTER the PG
      commit. Phase recorded as ``pre_tombstone`` (1 row, no tombstone).

  W2  the accepted tombstone IS persisted but a pending outbox item for
      the SAME identity SURVIVES. This is the torn-transport state; a fresh
      process must converge to 1 row with no surviving pending item.
      Barriering: a child-only post-accepted seam calls the REAL
      ``_persist_live_accepted`` and then parks the matching item BEFORE the
      pending unlink that ``_ack_live_item`` performs immediately afterwards
      (``ingest.py:455-467``). Because that unlink is a single statement
      after the tombstone write, a release-then-race is not a guarantee; the
      seam HOLDS the item between the two steps, so PG=1 / accepted present /
      pending present is an observed state, not a hoped-for one. No marker is
      ever written by the harness — both writes are the product's own. The
      tree is then hard-killed while parked, and a fresh process must reach
      PG=1 with accepted present and pending ABSENT.

  W3  the row is really committed in PG, and only then is the caller left
      without an ACK; the process is killed and a fresh process retries.
      Expected final state: exactly 1 row, and the pre-crash read-back must
      already show 1 row. Phase recorded as ``after_commit``.

  W4  REPEATED crash/recover, at least 3 cycles, one identity. Each cycle
      crashes the serving tree, proves the crash was clean (no residual
      pid), starts a fresh process and retries the identical identity. The
      final state must be exactly 1 row.

§6 states six end-state conditions for the whole run. They are evaluated
per case by :func:`evaluate_case_section6`, not merely narrated:

    canonical source rows = 1
    no duplicate QA caused by same event
    pending/accepted transport converges
    no foreign data root
    no production config/profile
    no production DB

and the provider-call count is recorded only — §6 explicitly says so, and
says it is not an F2 blocker. What forbids a REAL provider call is the
structural isolation (I2–I6), which IS gated.

A retry from the SAME process proves nothing about the database: B01 keeps
two dedupe layers ABOVE PostgreSQL (the in-process delta cursor and the
durable markers). Every case here therefore restarts into a fresh process,
which is also the realistic case (crash + restart, reinstall, new machine).

=============================================================================
ISOLATION INVARIANTS (fail-closed, every one of them enforced here)
=============================================================================

I1  MACHINE GATE. Destructive fault injection runs only on the dedicated
    Windows test laptop ``DESKTOP-EQP3OBU``. ``DESKTOP-MU2RQIU`` (the
    production WinNAS) is named explicitly and refused, as is any other host.

I2  NO PRODUCTION CONFIG IS READ. The child profile is built from a literal
    dict. There is no parameter by which a production config could be passed
    in, and no code path here opens a file under ``Path.home()/.v3-core``
    other than the read-only production sentinel fingerprint.

I3  NO CREDENTIAL ON DISK. The generated YAML carries no ``storage.pg.password``
    and no non-empty provider field. The PG password travels only as a child
    environment variable (``V3CORE_PG_PASSWORD`` / ``PGPASSWORD``) and only
    as a ``psycopg2.connect`` keyword. It is never printed, never placed on
    a command line, and never written to evidence; every text that could
    carry it is redacted on the way out.

I4  PROVIDER CALLS ARE STRUCTURALLY IMPOSSIBLE. Every provider field in the
    generated config is empty and the child environment blanks every key
    ``LLMClient`` / ``safe_embed_cfg`` can fall back to, so no request is
    reachable. That is still verified from the logs: the provider-marker
    count must be zero or the run is invalid.

I5  EVERY CHILD SELF-REPORTS ITS RESOLVED IDENTITY. Each child prints a
    ``provenance`` record naming the config path it resolved, the config's
    declared basePath, the resolved data dir, the core base path, the topic
    SQLite mirror path, the pending/accepted outbox dirs and its PG
    host/port/database/user (never the password). The harness asserts every
    child agrees, that all of those paths are under the ONE isolated root,
    and that the database is on the F2 allowlist.

I6  TEST MODE ON EVERY CHILD ROLE. ``V3CORE_TEST_MODE=1`` plus an explicit
    ``V3CORE_CONFIG`` are set for serve and client children alike, so
    ``v3core.config`` refuses rather than falling back to a default profile.

I7  CONFIG LIFECYCLE. The generated config exists for the whole run and is
    deleted only after every owned child has been proven dead. Deleting it
    earlier is exactly the sequence that made ``_find_config`` fall through
    to production, so the deletion is the last act and is asserted.

I8  OWNED PROCESSES ONLY. A child is identified by ``(pid, creation time,
    command line)``. Termination re-reads all three and refuses to signal a
    process whose identity does not match (pid reuse). Nothing is ever
    matched or killed by image name. Leftovers are a hard abort.

I9  PRODUCTION SENTINEL. A recursive fingerprint of the production profile
    (config, observer cursor, topic NPZ/meta cache, topic SQLite, cards DB,
    j/ outbox) is taken before the first child and after the last one, and
    compared. Any change is a failed run.

=============================================================================
ACCEPTANCE SEAL (the only thing that sets the exit code)
=============================================================================

``run_make`` gathers evidence; it does NOT judge it. The single verdict and
the single exit code come from :func:`evaluate_acceptance`, a pure function of
the evidence file, so the rule is unit-tested rather than asserted in prose:

  exit 0  PASS   F2 ACCEPTED — the exact full matrix (W0-W4) all PASS, every
                    mission §6 end-state condition passed in EVERY case,
                    every mandatory guard phase present and PASS, no
                    ``run_error``, every child agreed on one isolated
                    identity, residual owned pids zero, the production
                    sentinel unchanged, the generated config removed and
                    only after every child was dead, and no teardown error.
                    The provider-marker total is recorded as telemetry, not
                    gated (mission §6: provider call counts are 记录即可,
                    explicitly not an F2 blocker; the proof that no REAL
                    provider call happened is the structural isolation,
                    which IS gated).
  exit 1  FAIL   F2 REJECTED — at least one of the above did not hold. The
                    offending conditions are listed explicitly; the W summary
                    being all-PASS is NOT sufficient.
  exit 3  PARTIAL every selected case and every guard passed, but ``--only``
                    restricted the matrix. A subset is never F2 DONE.
  exit 2  no evidence at all.

Missing proof is a failure, not a pass: an unrecorded guard phase, a missing
W result, an aborted run, a surviving child, a moved sentinel, a config that
outlived its expected deletion, or ANY teardown exception (including a
``stop_all`` fault, which is recorded rather than swallowed) rejects the run.
When teardown fails, the generated config is PRESERVED on disk on purpose — it
is the one artefact that must not be deleted while a child might still be
alive and able to rebind to production.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# ── module-local, side-effect free ────────────────────────────────────────
# Nothing below runs at import time: product-ci imports this module on Linux
# to unit-test the guard and cleanup logic, so the Windows-only machinery is
# only touched from ``make`` / the child roles.

EVAL_DIR = Path(__file__).resolve().parent
PKG_DIR = EVAL_DIR.parent                 # src/v3-core
REPO_ROOT = PKG_DIR.parent.parent         # <repo>

#: The one host allowed to run destructive fault injection.
TEST_LAPTOP_HOSTNAME = "DESKTOP-EQP3OBU"
#: The production WinNAS. Named so the failure message is unambiguous.
FORBIDDEN_HOSTNAMES = frozenset({"DESKTOP-MU2RQIU"})

#: Disposable F2 databases. These tests DROP/CREATE and DELETE rows, so the
#: allowlist is closed: a mistyped DSN must refuse, not clean up some other
#: local database.
ALLOWED_PG_DATABASES = frozenset({"f2e2e", "f2src"})
#: Production PG port. Never admissible.
PROD_PG_PORT = 5433
#: Production database name. Never admissible.
PROD_PG_DATABASE = "v3embeddings"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "localhost.localdomain"})

IDENTITY_INDEX = "conversation_stream_host_session_event_uniq"
F2_TEST_MODE_ENV = "V3CORE_TEST_MODE"
F2_CONFIG_ENV = "V3CORE_CONFIG"

# ══════════════════════════════════════════════════════════════════════════
# launch handshake (I8) — the f2-04 W4 child-launch repair
#
# A spawned child is only allowed to begin its workload AFTER the parent has
# captured and verified its live OS identity triple. ``process_identity``
# shells out to PowerShell, so the window between ``Popen`` and the identity
# is seconds wide — wide enough for a fast child-client to finish an immediate
# duplicate POST and exit (f2-04: PID3356). The handshake makes that window
# deterministic instead of lucky: the child parks on a per-spawn-unique,
# run-local release file and the parent creates it only once the OwnedChild
# exists and is registered. The release is an EXISTENCE gate — its content
# authenticates nothing and is never verified.
#
# These bounds are deliberately generous (a slow PowerShell inventory is the
# whole problem) and are NOT a substitute for the handshake: the child refuses
# to run its workload if the release never arrives, so a timeout can only
# ever fail the run, never fake a pass.
# ══════════════════════════════════════════════════════════════════════════
#: How long a child waits for the parent to publish its identity release.
IDENTITY_RELEASE_TIMEOUT = 600.0
#: How long the parent waits for the parked child's ``child_start`` line.
IDENTITY_START_TIMEOUT = 300.0

#: Minted ONCE per process, so every run-local release path is unique even
#: when a run reuses a root and a child tag. Overwritten by :func:`run_make`
#: only in the sense that a fresh ``make`` process is a fresh import.
_RUN_NONCE = uuid.uuid4().hex

#: Refusal prefix emitted by ``v3core.config`` in TEST mode. The harness
#: matches on it so a T1-style refusal is reported as a refusal, not a crash.
TEST_MODE_REFUSAL_PREFIX = "V3CORE_TEST_MODE fail-closed"

#: Log lines that prove a provider request HAPPENED. Non-zero invalidates the
#: run — it is not "passing with noise".
PROVIDER_CALL_MARKERS = (
    "LLM call:",
    "LLM success:",
    "LLM net error",
    "LLM 429",
    "embedding 调用失败",
    "EMBEDDING_AUTH_FAILED",
    "embedding failed",
    "dreamer: boosted=",
    "e1 合成印",
)
#: ``embed_for_write`` logs one line per item. With embedding unconfigured it
#: returns DEGRADED/NO_INPUT with attempts=0 — that is the branch that issued
#: NO request, so it is positive evidence of isolation, not a violation.
_NO_REQUEST_RE = re.compile(
    r"LiveBuffer embedding degraded:.*class=NO_INPUT.*attempts=0")
_REAL_ATTEMPT_RE = re.compile(r"LiveBuffer embedding (?!degraded)")

#: Provider fields whose non-empty value would be a credential or a live
#: endpoint. The written config is re-read from disk and refused if any of
#: them is present-and-non-empty.
CREDENTIAL_FIELDS = (
    ("storage", "pg", "password"),
    ("storage", "embed", "apiKey"),
    ("storage", "embed", "api_key"),
    ("storage", "embed", "endpoint"),
    ("storage", "embed", "model"),
    ("storage", "embed", "proxy"),
    ("storage", "rerank", "apiKey"),
    ("storage", "rerank", "api_key"),
    ("storage", "rerank", "endpoint"),
    ("storage", "rerank", "model"),
    ("storage", "rerank", "proxy"),
    ("llm", "api_key"),
    ("llm", "base_url"),
    ("llm", "provider"),
    ("llm", "model"),
)

#: Environment variables blanked in every child so no ambient host-level key
#: can hand a child a working provider credential.
BLANKED_PROVIDER_ENV = (
    "MINIMAX_CN_API_KEY",
    "MINIMAX_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "V3_LLM_API_KEY",
    "V3_LLM_BASE_URL",
    "V3_LLM_PROVIDER",
    "V3CORE_BOOTSTRAP_DSN",
    "V3CORE_DOTENV",
    "HTTPS_PROXY",
    "HTTP_PROXY",
)

#: Relative paths inside the production profile that are fingerprinted. A
#: recursive walk of the whole profile is taken; these names only document
#: what the fingerprint is expected to cover (config, observer cursor, topic
#: NPZ/meta cache, topic SQLite, cards DB, j/ outbox).
PRODUCTION_SENTINEL_HIGHLIGHTS = (
    "config.yaml",
    "observer_state.json",
    "state.json",
    "topic_matrix.npz",
    "topic_meta.json",
    "v3_topic.db",
    "v3_cards.db",
    "j",
)


# ══════════════════════════════════════════════════════════════════════════
# errors
# ══════════════════════════════════════════════════════════════════════════
class F2HarnessError(RuntimeError):
    """Base class: every harness refusal is a RuntimeError with a reason."""


class HostGateError(F2HarnessError):
    """Raised when the machine is not the dedicated destructive-test host."""


class IsolationError(F2HarnessError):
    """Raised when a child, config, path or database is not isolated."""


class LeftoverProcessError(F2HarnessError):
    """Raised when an owned child survived cleanup. Always a hard abort."""


class PgTargetError(F2HarnessError):
    """Raised when the requested PostgreSQL target is not admissible."""


class ChildCleanupError(F2HarnessError):
    """Raised when teardown could not stop every owned child.

    Carries whatever *was* collected so the failure report can still say
    which pids were and were not proven dead. A cleanup fault is never
    downgraded to a warning: I8 is what makes deleting the config safe.
    """

    def __init__(self, message: str, records: Sequence[dict] | None = None,
                 errors: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.records = list(records or [])
        self.errors = list(errors or [])


# ══════════════════════════════════════════════════════════════════════════
# small pure helpers
# ══════════════════════════════════════════════════════════════════════════
def log(message: str) -> None:
    print(message, flush=True)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _norm(path: str | os.PathLike[str]) -> str:
    """Absolute, separator-normalised, case-normalised form for comparisons.

    ``os.path.normcase`` is the portable primitive here: on Windows it also
    lowercases and folds ``/`` to ``\\``, on POSIX it is the identity. Hard
    coding a separator here would have made every isolation check wrong on
    the other platform.
    """
    try:
        return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))
    except (TypeError, ValueError):
        return ""


def is_under(child: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """True when ``child`` is ``root`` or lives beneath it.

    Uses ``os.path.relpath`` on the already-normalised pair so the separator
    convention follows the platform instead of being assumed.
    """
    c, r = _norm(child), _norm(root)
    if not c or not r:
        return False
    if c == r:
        return True
    try:
        rel = os.path.relpath(c, r)
    except ValueError:  # different drives on Windows
        return False
    return rel != os.pardir and not rel.startswith(os.pardir + os.sep)


def assert_all_under(paths: Iterable[tuple[str, str]], root: str | os.PathLike[str],
                     what: str) -> dict:
    """Assert every (label, path) pair lives under ``root``.

    Returns a record for the evidence file. Raises :class:`IsolationError`
    naming the offender — a foreign data root is never tolerated, because
    that is precisely how a test run reaches production state.
    """
    checked: list[dict] = []
    offenders: list[str] = []
    for label, value in paths:
        inside = is_under(value, root)
        checked.append({"label": label, "path": str(value), "under_root": inside})
        if not inside:
            offenders.append(f"{label}={value}")
    if offenders:
        raise IsolationError(
            f"{what}: these resolved paths are OUTSIDE the isolated root "
            f"{root}: {offenders}. Refusing — a foreign data root means the "
            f"child was not isolated.")
    return {"root": str(root), "checked": checked, "all_under_root": True}


def redact(secret: str, text: Any) -> Any:
    """Strip ``secret`` from ``text`` before it can reach a log or evidence."""
    if not secret:
        return text
    if not isinstance(text, str):
        return text
    return text.replace(secret, "<redacted>")


def scrub_dsn(target: dict) -> dict:
    """DSN without its password — the only shape that may be persisted."""
    return {k: v for k, v in target.items() if k != "password"}


def _dig(raw: dict, path: Sequence[str]):
    cur: Any = raw
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


# ══════════════════════════════════════════════════════════════════════════
# I1 — machine gate
# ══════════════════════════════════════════════════════════════════════════
def current_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception as exc:  # pragma: no cover - defensive
        raise HostGateError(f"cannot determine hostname: {exc}") from exc


def assert_test_host(hostname: str | None = None) -> dict:
    """I1. Destructive fault injection is only legal on the test laptop.

    Raises :class:`HostGateError` naming the host that refused. The
    production WinNAS is named explicitly so a mis-set expectation reads as
    a refusal rather than as a mystery.
    """
    # Strict on whitespace: a sentinel that is *almost* the test laptop is
    # not the test laptop, and this gate decides whether to kill processes.
    host = hostname or current_hostname() or ""
    if host in FORBIDDEN_HOSTNAMES:
        raise HostGateError(
            f"hostname {host!r} is the PRODUCTION WinNAS and is explicitly "
            f"forbidden for destructive F2 fault injection. Run on "
            f"{TEST_LAPTOP_HOSTNAME}.")
    if host.upper() != TEST_LAPTOP_HOSTNAME.upper():
        raise HostGateError(
            f"hostname {host!r} is not the dedicated Windows test laptop "
            f"({TEST_LAPTOP_HOSTNAME}). Refusing to inject crashes / ACK loss "
            f"on an unrecognised host.")
    return {"hostname": host, "allowed_host": TEST_LAPTOP_HOSTNAME,
            "forbidden_hosts": sorted(FORBIDDEN_HOSTNAMES), "verdict": "PASS"}


# ══════════════════════════════════════════════════════════════════════════
# I3/I5 — the isolated profile (literals only)
# ══════════════════════════════════════════════════════════════════════════
def minimal_profile_config(base: Path, database: str, port: int, user: str) -> dict:
    """Build the isolated child profile from literals.

    I2: nothing is read from ``Path.home()``. The provider sections are
    present but empty so the code under test takes its "unconfigured" branch
    (no request) rather than inheriting a real endpoint or key.
    I3: ``storage.pg`` carries no ``password`` key at all — the password
    reaches the child only through ``V3CORE_PG_PASSWORD``.
    """
    if database not in ALLOWED_PG_DATABASES:
        raise PgTargetError(
            f"refusing database {database!r}; allowed: {sorted(ALLOWED_PG_DATABASES)}")
    return {
        "basePath": str(base),
        "mode": "cloud",
        "storage": {
            "pg": {"host": "127.0.0.1", "port": int(port),
                   "database": database, "user": user},
            # Blank -> safe_embed_cfg() returns None (disabled) and
            # embed_for_write() returns DEGRADED/NO_INPUT without any HTTP.
            "embed": {"endpoint": "", "model": "", "dim": 1024,
                      "apiKey": "", "api_key": "", "proxy": ""},
            "rerank": {"endpoint": "", "model": "", "proxy": "", "timeout": 30,
                       "apiKey": "", "api_key": ""},
        },
        # Disabled so the E1Scheduler that V3Core.initialize() starts cannot
        # synthesize anything before it would touch LLMClient.
        "e1": {"enabled": False},
        "llm": {"provider": "", "model": "", "api_key": "", "base_url": "",
                "thinking": True, "max_tokens": 0},
        "observer": {"enabled": False},
        "tkg": {"enabled": False},
    }


def _write_yaml(path: Path, data: dict) -> None:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is a hard dep
        raise F2HarnessError(f"PyYAML unavailable: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


def write_profile(base: Path, database: str, port: int, user: str) -> Path:
    """Create ``<base>/config.yaml`` from :func:`minimal_profile_config`."""
    base.mkdir(parents=True, exist_ok=True)
    path = base / "config.yaml"
    _write_yaml(path, minimal_profile_config(base, database, port, user))
    return path


def assert_no_credentials(config_path: Path, secret: str = "") -> dict:
    """I3 read-back: re-read the WRITTEN file and refuse on any credential."""
    import yaml
    text = Path(config_path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    offenders: list[str] = []
    present: list[str] = []
    for path in CREDENTIAL_FIELDS:
        value = _dig(raw, path)
        dotted = ".".join(path)
        if value is None:
            continue
        present.append(dotted)
        if str(value).strip():
            offenders.append(dotted)
    if offenders:
        raise IsolationError(
            f"generated config {config_path} carries non-empty credential "
            f"fields: {offenders}")
    if secret and secret in text:
        raise IsolationError(
            f"generated config {config_path} contains the PG password verbatim")
    return {"config": str(config_path),
            "all_credential_fields_empty": True,
            "credential_fields_checked": [".".join(p) for p in CREDENTIAL_FIELDS],
            "credential_fields_present_as_empty": present,
            "pg_password_in_file": False,
            "pg_password_channel": "V3CORE_PG_PASSWORD environment variable"}


def assert_profile_isolated(config_path: Path, root: Path, database: str,
                            port: int, user: str) -> dict:
    """I5 read-back: the config a child will load IS this isolated one.

    ``V3CORE_CONFIG`` is consulted once at startup in TEST mode. A missing
    path, a path outside this run's root, a foreign database or a non-empty
    password in the file must all fail loudly here rather than silently
    rebinding the harness somewhere else.
    """
    import yaml
    config_path = Path(config_path)
    if not config_path.is_file():
        raise IsolationError(
            f"isolated config is missing: {config_path}. The child would "
            f"refuse to boot (TEST mode) — not start.")
    if not is_under(config_path, root):
        raise IsolationError(
            f"isolated config {config_path} is outside the run root {root}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    pg = ((raw.get("storage") or {}).get("pg") or {})
    problems: list[str] = []
    if str(pg.get("host", "")) != "127.0.0.1":
        problems.append(f"pg.host={pg.get('host')!r}")
    if int(pg.get("port", 0) or 0) != int(port):
        problems.append(f"pg.port={pg.get('port')!r} (want {port})")
    if str(pg.get("database", "")) != database:
        problems.append(f"pg.database={pg.get('database')!r} (want {database})")
    if str(pg.get("user", "")) != user:
        problems.append(f"pg.user={pg.get('user')!r} (want {user})")
    if str(pg.get("password", "") or "").strip():
        problems.append("storage.pg.password is non-empty")
    declared_base = str(raw.get("basePath") or raw.get("base_path") or "")
    if not is_under(declared_base, root):
        problems.append(f"basePath={declared_base!r} is outside the run root")
    if problems:
        raise IsolationError(
            f"isolated profile {config_path} does not describe the disposable "
            f"target: {problems}")
    return {"profile_config": str(config_path), "under_f2_root": True,
            "declared_base_path": declared_base,
            "pg_host": pg.get("host"), "pg_port": pg.get("port"),
            "pg_database": pg.get("database"), "pg_user": pg.get("user"),
            "verdict": "PASS"}


# ══════════════════════════════════════════════════════════════════════════
# I6 — the child environment
# ══════════════════════════════════════════════════════════════════════════
def child_env(config_path: Path, base: Path, pg_password: str,
              extra: dict | None = None) -> dict:
    """Environment for EVERY child role (serve and client alike).

    I6: ``V3CORE_TEST_MODE=1`` + an explicit ``V3CORE_CONFIG`` make
    ``v3core.config`` fail-closed, so a child that cannot see this config
    refuses to boot instead of falling back to the default profile.
    I3: the password is passed here and nowhere else.
    I4: every provider key is blanked, so even a host-level ``.env`` cannot
    supply one.
    """
    env = dict(os.environ)
    env[F2_TEST_MODE_ENV] = "1"
    env[F2_CONFIG_ENV] = str(config_path)
    # Defence in depth: TEST mode already refuses ambient dotenv lookups, but
    # a stray V3CORE_HOME/.env would otherwise still be a candidate.
    env["V3CORE_HOME"] = str(base)
    env["HERMES_HOME"] = str(base)
    env["V3CORE_PG_PASSWORD"] = pg_password
    env["PGPASSWORD"] = pg_password
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    for name in BLANKED_PROVIDER_ENV:
        env[name] = ""
    if extra:
        env.update(extra)
    return env


def child_argv(role: str, *args: str) -> list[str]:
    """Command line for a child role.

    Uses ``sys.executable -m eval.f2_source_idempotency_e2e`` so the child
    always runs under the interpreter that has v3-core installed. No console
    script, no hardcoded venv path, no shim process in the middle.
    """
    return [sys.executable, "-m", "eval.f2_source_idempotency_e2e", role,
            *[str(a) for a in args]]


# ══════════════════════════════════════════════════════════════════════════
# I8 — process inventory and owned-process cleanup
# ══════════════════════════════════════════════════════════════════════════
def _cim_process_table() -> list[dict]:
    """(pid, ppid, creation, cmdline) for every process on the host.

    Win32 only, via CIM. Kept as one place so the identity triple used for
    verification always comes from the same source.
    """
    script = (
        "$ErrorActionPreference='Stop';"
        "Get-CimInstance Win32_Process | ForEach-Object {"
        "  [pscustomobject]@{"
        "    p=[int]$_.ProcessId;"
        "    pp=[int]$_.ParentProcessId;"
        "    c=$_.CreationDate.ToUniversalTime().ToString('o');"
        "    cmd=[string]$_.CommandLine } }"
        " | ConvertTo-Json -Compress -Depth 3"
    )
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                           "-Command", script],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    if proc.returncode != 0:
        raise F2HarnessError(
            f"process inventory failed (rc={proc.returncode}): "
            f"{(proc.stderr or '')[-200:]}")
    raw = (proc.stdout or "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    out: list[dict] = []
    for row in data:
        try:
            out.append({"pid": int(row["p"]), "ppid": int(row["pp"]),
                        "created": str(row.get("c") or ""),
                        "cmdline": str(row.get("cmd") or "")})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def process_snapshot() -> dict:
    """``{pid: identity}`` for the whole host."""
    return {int(r["pid"]): {"pid": int(r["pid"]), "ppid": int(r["ppid"]),
                            "created": r["created"], "cmdline": r["cmdline"]}
            for r in _cim_process_table()}


def process_identity(pid: int) -> dict | None:
    """Identity triple for one pid, or None when it is gone."""
    for row in _cim_process_table():
        if int(row["pid"]) == int(pid):
            return {"pid": int(row["pid"]), "ppid": int(row["ppid"]),
                    "created": row["created"], "cmdline": row["cmdline"]}
    return None


def identity_matches(expected: dict, observed: dict | None) -> tuple[bool, str]:
    """Compare ``(pid, creation, cmdline)``. All three, never just the pid.

    A recycled pid is the classic way a "safe" cleanup kills a stranger, so
    creation time is mandatory and the command line must still carry this
    run's marker.
    """
    if observed is None:
        return False, "process is gone"
    if int(observed.get("pid", -1)) != int(expected.get("pid", -2)):
        return False, "pid mismatch"
    if str(observed.get("created") or "") != str(expected.get("created") or ""):
        return False, "creation time mismatch (pid was recycled)"
    want = str(expected.get("cmdline") or "")
    got = str(observed.get("cmdline") or "")
    if want and got and want[:120] != got[:120]:
        return False, "command line mismatch"
    return True, "identity verified"


def _default_killer(pid: int) -> dict:
    """Hard-kill one pid and its tree via taskkill /T /F."""
    proc = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    return {"rc": proc.returncode,
            "out": (proc.stdout or "")[-400:],
            "err": (proc.stderr or "")[-400:]}


def terminate_owned(identity: dict, timeout: float = 30.0,
                    killer: Callable[[int], dict] | None = None,
                    lookup: Callable[[int], dict | None] | None = None,
                    sleep: Callable[[float], None] = time.sleep) -> dict:
    """I8: terminate one OWNED pid, verified.

    The identity triple is re-read immediately before signalling. A mismatch
    (recycled pid, foreign command line) is never signalled — the run fails
    instead, because an unverified kill is indistinguishable from killing
    something that is not ours.
    """
    kill = killer or _default_killer
    look = lookup or process_identity
    pid = int(identity["pid"])
    ok, why = identity_matches(identity, look(pid))
    if not ok:
        return {"pid": pid, "signalled": False, "reason": why,
                "verdict": "NOT-KILLED (identity unverified)",
                "clean": ok is not None and why == "process is gone"}
    result = kill(pid)
    deadline = time.time() + timeout
    residual: list[int] = []
    while time.time() < deadline:
        if look(pid) is None:
            break
        sleep(0.2)
    if look(pid) is not None:
        residual = [pid]
    return {"pid": pid, "signalled": True, "identity_check": why,
            "taskkill": result, "residual_after": residual,
            "clean": not residual,
            "verdict": "PASS" if not residual else "FAIL"}


class OwnedChild:
    """A spawned child, tracked by its verified identity triple.

    Never terminated by image name and never by bare pid: :meth:`stop`
    re-verifies first, and :meth:`verify` is what the leftover gate uses.
    """

    def __init__(self, role: str, proc: subprocess.Popen, identity: dict,
                 meta: dict | None = None,
                 lookup: Callable[[int], dict | None] | None = None,
                 killer: Callable[[int], dict] | None = None) -> None:
        self.role = role
        self.proc = proc
        self.identity = identity
        self.meta = dict(meta or {})
        self._lookup = lookup or process_identity
        self._killer = killer or _default_killer
        self.events: list[dict] = []
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._stdout = None
        self._stderr = None
        self._stderr_path: Path | None = None
        self._reader: threading.Thread | None = None
        self.stopped = False
        self.stop_record: dict | None = None

    # -- lifecycle --------------------------------------------------------
    def start(self, stderr_path: Path | None = None) -> "OwnedChild":
        self._stderr_path = Path(stderr_path) if stderr_path else None
        if self._stderr_path is not None:
            self._stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr = open(self._stderr_path, "w", encoding="utf-8",
                                errors="replace")
        return self._start_pump()

    def _start_pump(self) -> "OwnedChild":
        """Start the stdout reader. Idempotent.

        Split out from :meth:`start` because the launch handshake needs the
        pump running BEFORE the child's identity has been captured: the child
        announces ``child_start`` and parks, and that announcement is only
        observable through the reader. The handshake then fills in the real
        identity triple before the child is registered or released.
        """
        if self._reader is not None:
            return self
        self._stdout = self.proc.stdout
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        return self

    def _pump(self) -> None:
        stream = self._stdout
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                text = line.strip()
                with self._lock:
                    self._lines.append(text)
                event = self._parse(text)
                if event is not None:
                    with self._lock:
                        self.events.append(event)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    @staticmethod
    def _parse(text: str) -> dict | None:
        if not text.startswith("{"):
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def wait_event(self, name: str, timeout: float = 180.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for event in self.events:
                    if event.get("event") == name:
                        return event
                # ``stopped`` is the flag ``__init__`` initialises (line 783)
                # and ``stop()`` sets on its successful path. Reading the
                # never-assigned ``_stopped`` made this branch raise
                # AttributeError instead of the F2HarnessError it exists to
                # raise, so an exited child surfaced with the wrong failure
                # type and the suppression for a self-stopped child was dead.
                exited = self.proc.poll() is not None
                stopped = self.stopped
                tail = list(self._lines)[-25:]
            if exited and not stopped:
                # The process is gone, but its stdout reader may still be
                # draining a result it already received. Exiting on the first
                # observation of the exit reports a MISSING event for a result
                # that is merely queued (the f2-04 W4 race). So join the
                # reader ONCE, bounded, with the lock released — joining while
                # holding it would deadlock against the very append we are
                # waiting for. Then re-check under the lock before deciding.
                self._drain_reader(remaining=max(0.0, deadline - time.time()))
                with self._lock:
                    for event in self.events:
                        if event.get("event") == name:
                            return event
                    if self.stopped or self.proc.poll() is None:
                        # We stopped it, or it is somehow still running: not an
                        # un-announced exit, so keep waiting on the normal bound.
                        tail = list(self._lines)[-25:]
                        break
                    tail = list(self._lines)[-25:]
                raise F2HarnessError(
                    f"child role={self.role} exited (rc="
                    f"{self.proc.returncode}) before emitting {name!r}. "
                    f"Last lines: {tail}")
            time.sleep(0.05)
        raise F2HarnessError(
            f"child role={self.role} never emitted {name!r} within {timeout}s")

    def _drain_reader(self, remaining: float = 5.0) -> bool:
        """Join the stdout reader once, bounded, WITHOUT holding ``_lock``.

        Returns True when the reader finished. A reader that is still blocked
        (child alive, or the pipe held open by a grandchild) is simply left
        running — the caller then decides from the events, which is the
        fail-closed outcome.
        """
        reader = self._reader
        if reader is None:
            return True
        remaining = max(0.0, float(remaining))
        # Never block forever: a short bounded join is enough for a reader that
        # has already consumed the pipe, and a live child must never stall the
        # caller here.
        reader.join(timeout=min(remaining, 5.0) if remaining else 0.0)
        return not reader.is_alive()

    def record(self) -> dict:
        """Identity/provenance record for the evidence file."""
        with self._lock:
            events = list(self.events)
        return {"role": self.role, "pid": self.identity["pid"],
                "ppid": self.identity.get("ppid"),
                "created": self.identity.get("created"),
                "cmdline": self.identity.get("cmdline"),
                "returncode": self.proc.returncode,
                "stderr_log": str(self._stderr_path) if self._stderr_path else None,
                "events": [e.get("event") for e in events],
                **self.meta}

    def verify(self) -> dict:
        ok, why = identity_matches(self.identity, self._lookup(self.identity["pid"]))
        return {"role": self.role, "pid": self.identity["pid"], "alive": ok,
                "reason": why}

    def stop(self, timeout: float = 30.0, force: bool = True) -> dict:
        """Terminate this child (and its tree) after re-verifying identity.

        ``stopped`` is set only on the SUCCESSFUL path. A stop that raised
        has not proven the process is gone, and marking it stopped anyway
        would hide the leftover from :meth:`ChildRegistry.alive` — i.e. turn a
        cleanup fault into a clean pass, which is exactly the failure mode the
        acceptance seal exists to prevent.
        """
        if self.stopped:
            return self.stop_record or {}
        record = terminate_owned(self.identity, timeout=timeout,
                                 killer=self._killer, lookup=self._lookup)
        try:
            self.proc.wait(timeout=timeout)
        except Exception:
            pass
        for handle in (self._stdout, self._stderr):
            try:
                if handle is not None:
                    handle.close()
            except Exception:
                pass
        self.stopped = True
        self.stop_record = record
        return record

    def stderr_text(self) -> str:
        if self._stderr_path is None:
            return ""
        try:
            return Path(self._stderr_path).read_text(encoding="utf-8",
                                                     errors="replace")
        except Exception:
            return ""


class ChildRegistry:
    """Every child this run spawned, and the leftover gate over them."""

    def __init__(self, lookup: Callable[[int], dict | None] | None = None,
                 killer: Callable[[int], dict] | None = None) -> None:
        self._children: list[OwnedChild] = []
        self._lookup = lookup or process_identity
        self._killer = killer or _default_killer

    def add(self, child: OwnedChild) -> OwnedChild:
        child._lookup = self._lookup
        child._killer = self._killer
        self._children.append(child)
        return child

    def alive(self) -> list[dict]:
        out: list[dict] = []
        for child in self._children:
            if not child.stopped and child.verify()["alive"]:
                out.append({"role": child.role, "pid": child.identity["pid"]})
        return out

    def stop_all(self, timeout: float = 30.0) -> list[dict]:
        """Stop every owned child. EVERY child is attempted, even if one throws.

        A single child whose termination blows up must not leave its siblings
        running, so the loop continues — and then raises, because a stop that
        could not be completed is a failure, not a silent pass. The partial
        records ride on the exception so the caller can still report which
        pids were and were not proven dead.
        """
        records: list[dict] = []
        errors: list[str] = []
        for child in self._children:
            try:
                records.append(child.stop(timeout=timeout))
            except Exception as exc:  # noqa: BLE001 — keep stopping the rest
                errors.append(f"{child.role} pid={child.identity.get('pid')}: "
                              f"{type(exc).__name__}: {exc}")
        if errors:
            raise ChildCleanupError(
                "stop_all could not stop every owned child: " + "; ".join(errors),
                records=records, errors=errors)
        return records

    def assert_no_leftovers(self) -> dict:
        """I8: leftovers are a hard abort, never a warning."""
        alive = self.alive()
        if alive:
            raise LeftoverProcessError(
                f"F2 harness children survived cleanup: {alive}. Refusing to "
                f"report a result, delete the config, or reuse the port/DB.")
        return {"owned_children": len(self._children), "alive_after": 0,
                "clean": True, "verdict": "PASS"}


# ══════════════════════════════════════════════════════════════════════════
# I9 — production sentinel
# ══════════════════════════════════════════════════════════════════════════
def production_profile_dir(home: str | os.PathLike[str] | None = None) -> Path:
    root = Path(home) if home is not None else Path.home()
    return root / ".v3-core" / "profiles" / "default"


def fingerprint_tree(root: Path, max_files: int = 20000) -> dict:
    """Recursive, read-only ``{relpath: {size, sha256, mtime_ns}}``."""
    root = Path(root)
    out: dict[str, dict] = {"__root__": {"exists": root.is_dir()}}
    if not root.is_dir():
        return out
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if count >= max_files:
                out["__truncated__"] = {"max_files": max_files}
                return out
            full = Path(dirpath) / name
            rel = str(full.relative_to(root)).replace("\\", "/")
            try:
                data = full.read_bytes()
                st = full.stat()
            except OSError as exc:
                out[rel] = {"unreadable": str(exc)[:120]}
                count += 1
                continue
            out[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
                        "sha256": hashlib.sha256(data).hexdigest()}
            count += 1
    out["__file_count__"] = count
    return out


def diff_fingerprints(before: dict, after: dict) -> list[dict]:
    changed: list[dict] = []
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        if b != a:
            changed.append({"path": key, "before": b, "after": a})
    return changed


def assert_production_unchanged(before: dict, after: dict) -> dict:
    changed = diff_fingerprints(before, after)
    if changed:
        raise IsolationError(
            f"production profile changed during the F2 run — the run is "
            f"INVALID: {changed[:12]}")
    return {"changed": [], "unchanged": True, "verdict": "PASS"}


def production_sentinel(home: str | os.PathLike[str] | None = None) -> dict:
    """I9 read-back snapshot of the production profile."""
    root = production_profile_dir(home)
    fingerprint = fingerprint_tree(root)
    return {"profile_dir": str(root), "highlights": list(PRODUCTION_SENTINEL_HIGHLIGHTS),
            "file_count": fingerprint.get("__file_count__", 0),
            "exists": fingerprint.get("__root__", {}).get("exists", False),
            "fingerprint": fingerprint}


# ══════════════════════════════════════════════════════════════════════════
# I5 — the child's self-report
# ══════════════════════════════════════════════════════════════════════════
def collect_child_provenance() -> dict:
    """Everything a child resolved, gathered from inside the child.

    Deliberately asks the RESOLVERS rather than the environment: the point is
    to prove what the process actually bound, not what we hoped it would.
    """
    import yaml
    from v3core import config as v3config

    env_config = os.environ.get(F2_CONFIG_ENV, "")
    # Resolving the config and the data root is EXACTLY where a TEST-mode
    # refusal fires (a deleted V3CORE_CONFIG is the T1 sequence), and that
    # refusal is a SystemExit — not an Exception. So both calls live inside
    # the handler below: an un-guarded call here would kill the child with a
    # bare exit code and the harness would see a crash instead of a refusal.
    try:
        resolved_config = str(v3config._find_config() or "")
        data_dir = str(v3config._resolve_data_dir())
    except SystemExit as exc:
        return {"refused": True, "reason": str(exc),
                "refusal_prefix_ok": str(exc).startswith(TEST_MODE_REFUSAL_PREFIX),
                "stage": "resolve_config",
                "config_path_env": env_config, "config_path_resolved": "",
                "declared_base_path": "", "data_dir": "", "pid": os.getpid(),
                "test_mode": os.environ.get(F2_TEST_MODE_ENV, "")}
    except BaseException as exc:  # noqa: BLE001 — must not escape as a crash
        return {"refused": True,
                "reason": f"{type(exc).__name__}: {exc}"[:300],
                "refusal_prefix_ok": False, "stage": "resolve_config",
                "config_path_env": env_config, "config_path_resolved": "",
                "declared_base_path": "", "data_dir": "", "pid": os.getpid(),
                "test_mode": os.environ.get(F2_TEST_MODE_ENV, "")}

    declared = ""
    try:
        raw = yaml.safe_load(Path(resolved_config).read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            declared = str(raw.get("basePath") or raw.get("base_path") or "")
    except Exception as exc:
        declared = f"<unreadable: {type(exc).__name__}>"

    outbox = Path(data_dir) / "j"
    pg_info: dict = {}
    core_base = ""
    topic_db = ""
    try:
        from v3core import V3Core
        from v3core import topic_store
        core = V3Core(profile=os.environ.get("F2_PROFILE", "default"))
        cfg = core.config
        core_base = str(core._get_base_path())
        if getattr(cfg, "pg", None) is not None:
            pg_info = {"host": cfg.pg.host, "port": int(cfg.pg.port or 0),
                       "database": cfg.pg.database, "user": cfg.pg.user,
                       "password_present": bool(cfg.pg.password)}
        topic_db = str(topic_store._default_db_path())
    except SystemExit as exc:
        # TEST-mode refusal. Report it verbatim so the harness can classify
        # it instead of reporting a mysterious crash.
        return {"refused": True, "reason": str(exc),
                "refusal_prefix_ok": str(exc).startswith(TEST_MODE_REFUSAL_PREFIX),
                "config_path_env": env_config, "config_path_resolved": resolved_config,
                "declared_base_path": declared, "data_dir": data_dir}
    except Exception as exc:
        pg_info = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    return {
        "refused": False,
        "config_path_env": env_config,
        "config_path_resolved": resolved_config,
        "declared_base_path": declared,
        "data_dir": data_dir,
        "core_base_path": core_base,
        "topic_sqlite_path": topic_db,
        "topic_cache_dir": data_dir,
        "pending_dir": str(outbox / "pending_live_buffer"),
        "accepted_dir": str(outbox / "accepted_live_buffer"),
        "pg": pg_info,
        "pid": os.getpid(),
        "test_mode": os.environ.get(F2_TEST_MODE_ENV, ""),
    }


def assert_child_provenance(prov: dict, root: Path, database: str, port: int,
                            user: str) -> dict:
    """I5: one child's self-report must be inside the isolated root only."""
    if prov.get("refused"):
        raise IsolationError(
            f"child refused to boot in TEST mode: {prov.get('reason')}")
    problems: list[str] = []
    if prov.get("test_mode") != "1":
        problems.append(f"{F2_TEST_MODE_ENV}={prov.get('test_mode')!r} (want '1')")
    if not prov.get("config_path_env"):
        problems.append(f"{F2_CONFIG_ENV} not set in the child")
    assert_all_under(
        [("config_path_resolved", prov.get("config_path_resolved", "")),
         ("data_dir", prov.get("data_dir", "")),
         ("core_base_path", prov.get("core_base_path", "")),
         ("topic_sqlite_path", prov.get("topic_sqlite_path", "")),
         ("pending_dir", prov.get("pending_dir", "")),
         ("accepted_dir", prov.get("accepted_dir", ""))],
        root, "child resolved identity")
    pg = prov.get("pg") or {}
    if str(pg.get("database", "")) != database:
        problems.append(f"pg.database={pg.get('database')!r} (want {database})")
    if int(pg.get("port", 0) or 0) != int(port):
        problems.append(f"pg.port={pg.get('port')!r} (want {port})")
    if str(pg.get("user", "")) != user:
        problems.append(f"pg.user={pg.get('user')!r} (want {user})")
    if pg.get("password_present"):
        # EXPECTED, not a failure: ``config._apply_pg_password_env`` fills
        # storage.pg.password from the V3CORE_PG_PASSWORD channel, so a
        # resolved config legitimately has one. What must never happen is the
        # password existing in the WRITTEN file, which
        # :func:`assert_no_credentials` re-reads and refuses. The child only
        # ever reports the boolean, never the value.
        prov = {**prov, "pg_password_channel": "V3CORE_PG_PASSWORD environment variable"}
    if problems:
        raise IsolationError(f"child provenance is not isolated: {problems}")
    return {**prov, "isolated": True, "verdict": "PASS"}


def assert_all_children_agree(records: Sequence[dict], root: Path,
                              database: str, port: int, user: str) -> dict:
    """I5 across every child: one root, one database, one resolved identity."""
    fields = ("data_dir", "core_base_path", "topic_sqlite_path")
    checked: list[dict] = []
    for rec in records:
        prov = rec.get("provenance") or {}
        assert_child_provenance(prov, root, database, port, user)
        checked.append({"role": rec.get("role"), "pid": rec.get("pid"),
                        **{f: prov.get(f) for f in fields},
                        "pg_database": (prov.get("pg") or {}).get("database")})
    keys = {tuple(str(item.get(f, "")) for f in fields) for item in checked}
    if len(keys) > 1:
        raise IsolationError(
            f"children resolved DIFFERENT data roots — the run is not "
            f"isolated: {checked}")
    return {"children": checked, "agreed": True, "root": str(root),
            "database": database, "verdict": "PASS"}


# ══════════════════════════════════════════════════════════════════════════
# controlled barriers (test-only seam, harness side — host contract untouched)
# ══════════════════════════════════════════════════════════════════════════
def classify_barrier_phase(rows: int) -> str:
    """Pure: which side of the commit a barrier observation sits on.

    W0 requires ``before_commit`` (zero rows) and W3 requires
    ``after_commit`` (one row). Making this a pure function keeps the
    distinction asserted rather than narrated.
    """
    if rows <= 0:
        return "before_commit"
    return "after_commit"


def classify_marker_phase(accepted: bool, rows: int) -> str:
    """Pure: which side of the ACCEPTED-TOMBSTONE write an observation is on.

    This is the discriminator for the mission §6 transport windows. The
    canonical write order in :meth:`LiveBuffer._flush` is

        PG commit  →  accepted tombstone  →  pending marker removed

    so the three observable states are named here rather than being
    re-derived (and mis-inferred) at each call site:

    ==================  ===========  =========================
    phase                PG rows      on-disk markers
    ==================  ===========  =========================
    ``pre_commit``       0            pending only
    ``pre_tombstone``    1            pending only   ← W1 window
    ``converged``        1            accepted only  ← correct end state
    ==================  ===========  =========================

    ``pre_tombstone`` is the crash window W1 exists to cover: the row is
    durably committed but the identity tombstone that would let a fresh
    process recognise the event as already-accepted is not yet written.
    """
    if rows <= 0:
        return "pre_commit"
    if not accepted:
        return "pre_tombstone"
    return "converged"


class FlushBarrier:
    """Blocks the live-writer flush immediately before the PG insert.

    This is a seam inside the CHILD, installed by replacing the flush method
    on the LiveBuffer instance the core already built. It adds no failpoint
    to production code and changes no host contract: the buffer, the pg
    store, the config and the outbox are all the real ones.
    """

    def __init__(self, host: str, session_id: str, event_id: str,
                 marker_path: Path, release_path: Path | None = None,
                 timeout: float = 900.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.host = str(host or "")
        self.session_id = str(session_id)
        self.event_id = str(event_id)
        self.marker_path = Path(marker_path)
        self.release_path = Path(release_path) if release_path else None
        self.timeout = float(timeout)
        self._sleep = sleep
        self.reached = False
        self.released = False

    def matches(self, item: Sequence) -> bool:
        try:
            item_host = str(item[8]) if len(item) > 8 and item[8] else ""
            return (str(item[0]) == self.session_id
                    and str(item[1]) == self.event_id
                    and item_host == self.host)
        except (IndexError, TypeError):
            return False

    def wait(self) -> bool:
        """Announce arrival, then block until released or the timeout lapses."""
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps({
            "event": "flush_barrier_reached",
            "host": self.host, "session_id": self.session_id,
            "event_id": self.event_id, "at": now_iso(),
            "pid": os.getpid(),
        }), encoding="utf-8")
        self.reached = True
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self.release_path is not None and self.release_path.exists():
                self.released = True
                return True
            self._sleep(0.1)
        return False

    def wrap(self, buffer: Any) -> Any:
        """Install the barrier on a LiveBuffer instance, returning it."""
        original = buffer._flush

        def _gated(_self, batch_size: int = 0, items=None):  # noqa: ANN001
            # Bound as a method, so _self is the buffer. On a timeout the
            # item is handed back untouched rather than silently inserted:
            # an un-witnessed barrier must never become a committed event.
            target = items or []
            if items is not None and any(self.matches(it) for it in target):
                self.wait()
                if not self.released:
                    return
            return original(batch_size=batch_size, items=items)

        buffer._flush = types.MethodType(_gated, buffer)
        self._buffer = buffer
        return buffer


class PostAcceptedBarrier:
    """Parks BETWEEN the accepted tombstone and the pending unlink (§6 W2).

    The product's two writes live in one method
    (:meth:`LiveBuffer._ack_live_item`, ``ingest.py:455-467``):

        if not self._persist_live_accepted(item):
            return                                  # <- tombstone lands HERE
        path.unlink(missing_ok=True)                # <- pending removed HERE

    There is no observable state between them, and the unlink is a single
    statement, so "release the tombstone barrier, then race the kill against
    the unlink" only SOMETIMES produces the torn state. That was W2's real
    defect: the state was hoped for, not observed.

    This seam closes that gap without touching the product. It wraps
    ``_ack_live_item`` on the served buffer, and for the matching item it:

      1. calls the REAL ``_persist_live_accepted`` first — the tombstone is
         the product's own write, made through the product's own method, so
         the harness never authors a marker;
      2. parks the item, holding it between step 1 and the unlink;
      3. on release, defers to the REAL ``_ack_live_item``, which unlinks
         the pending marker and clears the in-process slot.

    The harness kills the tree while parked, so the unlink never runs in
    this process: the on-disk state at the crash is genuinely "PG=1,
    accepted present, pending present", produced by the product's own
    ordering. On timeout it defers to the product rather than dropping the
    item, so an unwitnessed barrier cannot manufacture a converged state.
    """

    def __init__(self, host: str, session_id: str, event_id: str,
                 marker_path: Path, timeout: float = 900.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.host = str(host or "")
        self.session_id = str(session_id)
        self.event_id = str(event_id)
        self.marker_path = Path(marker_path)
        self.timeout = float(timeout)
        self._sleep = sleep
        self.reached = False
        self.released = False
        self.accepted_persisted = False

    def matches(self, item: Sequence) -> bool:
        try:
            item_host = str(item[8]) if len(item) > 8 and item[8] else ""
            return (str(item[0]) == self.session_id
                    and str(item[1]) == self.event_id
                    and item_host == self.host)
        except (IndexError, TypeError):
            return False

    def wait(self) -> bool:
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps({
            "event": "post_accepted_barrier_reached",
            "host": self.host, "session_id": self.session_id,
            "event_id": self.event_id, "at": now_iso(),
            "pid": os.getpid(),
            "accepted_persisted": self.accepted_persisted,
        }), encoding="utf-8")
        self.reached = True
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self.marker_path.with_name(
                    self.marker_path.name + ".release").exists():
                self.released = True
                return True
            self._sleep(0.1)
        return False

    def wrap(self, buffer: Any) -> Any:
        # Both originals are captured BEFORE the wrapper is installed: the
        # real _ack_live_item is what performs the pending unlink, and the
        # real _persist_live_accepted is what writes the tombstone. Calling
        # either of them here keeps every durable write in the product.
        original_ack = buffer._ack_live_item
        original_persist = buffer._persist_live_accepted

        def _gated(_self, item):  # noqa: ANN001
            if not self.matches(item):
                return original_ack(item)
            # 1. the product's own tombstone write, first and for real.
            try:
                self.accepted_persisted = bool(original_persist(item))
            except Exception:
                self.accepted_persisted = False
            # 2. hold the item here: the pending unlink has NOT happened.
            self.wait()
            # 3. release (or timeout) — the product unlinks, nobody else does.
            return original_ack(item)

        # Bound as a method because the product calls
        # ``self._ack_live_item(item)`` — a plain function attribute would
        # swallow the ``item`` argument.
        buffer._ack_live_item = types.MethodType(_gated, buffer)
        self._buffer = buffer
        self._original_ack = original_ack
        self._original_persist = original_persist
        return buffer


class TombstoneBarrier:
    """Parks the accepted-tombstone write, AFTER the PG commit.

    ``LiveBuffer._flush`` commits to PG, then calls ``_ack_live_item`` for
    every acked item, and ``_ack_live_item`` persists the accepted
    tombstone *before* unlinking the pending outbox marker
    (``ingest.py:906-911`` → ``455-467``). This seam wraps
    ``_ack_live_item`` on the LiveBuffer the HTTP handler actually writes
    to, so the barrier sits precisely in the mission §6 **W1** window:

        PG row committed (1 row)  +  accepted tombstone NOT yet written

    Like :class:`FlushBarrier` it is a child-side test seam: it adds no
    failpoint to product code, changes no host contract, and leaves the
    real buffer, PG store, config and outbox in place. On timeout it
    hands the item BACK to the original ack rather than dropping it, so an
    unwitnessed barrier can never silently manufacture a converged state.
    """

    def __init__(self, host: str, session_id: str, event_id: str,
                 marker_path: Path, timeout: float = 900.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.host = str(host or "")
        self.session_id = str(session_id)
        self.event_id = str(event_id)
        self.marker_path = Path(marker_path)
        self.timeout = float(timeout)
        self._sleep = sleep
        self.reached = False
        self.released = False

    def matches(self, item: Sequence) -> bool:
        try:
            item_host = str(item[8]) if len(item) > 8 and item[8] else ""
            return (str(item[0]) == self.session_id
                    and str(item[1]) == self.event_id
                    and item_host == self.host)
        except (IndexError, TypeError):
            return False

    def wait(self) -> bool:
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps({
            "event": "tombstone_barrier_reached",
            "host": self.host, "session_id": self.session_id,
            "event_id": self.event_id, "at": now_iso(),
            "pid": os.getpid(),
        }), encoding="utf-8")
        self.reached = True
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self.marker_path.with_name(
                    self.marker_path.name + ".release").exists():
                self.released = True
                return True
            self._sleep(0.1)
        return False

    def wrap(self, buffer: Any) -> Any:
        original = buffer._ack_live_item

        def _gated(_self, item):  # noqa: ANN001
            if self.matches(item):
                self.wait()
                if not self.released:
                    # Never reached a real release (harness died / timeout):
                    # defer to the product so the tombstone still lands.
                    return original(item)
            return original(item)

        # Bound as a method because the product calls ``self._ack_live_item(it)``
        # — a plain function attribute would swallow the ``item`` argument.
        buffer._ack_live_item = types.MethodType(_gated, buffer)
        self._buffer = buffer
        return buffer


def arm_tombstone_barrier_on_served_core(
        spec: dict) -> "types.SimpleNamespace":
    """Class-level seam so the barrier lands on the SERVED core's buffer.

    Mirrors :func:`arm_barrier_on_served_core`: ``serve()`` builds its own
    ``V3Core`` internally, so the wrapper must be installed on the CLASS
    property, inside the child only, or it would instrument a buffer the
    HTTP handler never writes through.
    """
    from v3core import V3Core

    barriers: list[TombstoneBarrier] = []
    buffers: list[Any] = []
    original_property = V3Core.live_buffer

    def _property(core_self):
        buffer = original_property.fget(core_self)
        if not any(b is buffer for b in buffers):
            buffers.append(buffer)
            barrier = TombstoneBarrier(
                spec["host"], spec["session_id"], spec["event_id"],
                Path(spec["marker_path"]), timeout=float(spec.get("timeout", 900)))
            barriers.append(barrier)
            barrier.wrap(buffer)
        return buffer

    V3Core.live_buffer = property(_property)

    def _restore() -> None:
        V3Core.live_buffer = original_property

    return types.SimpleNamespace(barriers=barriers, buffers=buffers,
                                 restore=_restore,
                                 installed_on=lambda: len(buffers) > 0)


def arm_post_accepted_barrier_on_served_core(
        spec: dict) -> "types.SimpleNamespace":
    """Class-level seam for §6 W2, on the SERVED core's buffer.

    Same reasoning as :func:`arm_tombstone_barrier_on_served_core`:
    ``serve()`` builds its own ``V3Core``, so the seam has to be installed on
    the CLASS property inside the child, or it would instrument a buffer the
    HTTP handler never writes through. What differs is the PARKED PHASE: the
    item is held AFTER the product has written the accepted tombstone and
    BEFORE it unlinks the pending marker, which is the torn state W2 needs.
    """
    from v3core import V3Core

    barriers: list[PostAcceptedBarrier] = []
    buffers: list[Any] = []
    original_property = V3Core.live_buffer

    def _property(core_self):
        buffer = original_property.fget(core_self)
        if not any(b is buffer for b in buffers):
            buffers.append(buffer)
            barrier = PostAcceptedBarrier(
                spec["host"], spec["session_id"], spec["event_id"],
                Path(spec["marker_path"]), timeout=float(spec.get("timeout", 900)))
            barriers.append(barrier)
            barrier.wrap(buffer)
        return buffer

    V3Core.live_buffer = property(_property)

    def _restore() -> None:
        V3Core.live_buffer = original_property

    return types.SimpleNamespace(barriers=barriers, buffers=buffers,
                                 restore=_restore,
                                 installed_on=lambda: len(buffers) > 0)


# ══════════════════════════════════════════════════════════════════════════
# test-only TCP proxies (the ACK-loss / request-loss fault)
# ══════════════════════════════════════════════════════════════════════════
class DirectionProxy:
    """Test-only TCP proxy that can drop or blackhole ONE direction.

    ``blackhole`` names the direction whose bytes are read, counted and never
    forwarded:

      * ``"server->client"`` — the server's HTTP ACK is produced and
        destroyed in here, so the caller receives zero HTTP bytes. That is
        genuine, deterministic ACK loss (W0 / W3).
      * ``"client->server"`` — the request never reaches the server and the
        client's socket is reset or simply never answered, so the client
        raises a real transport error (W1).

    The serving process and the upstream connection are never touched by a
    reset: only the client-facing socket is closed with SO_LINGER(0).
    """

    def __init__(self, server_port: int, blackhole: str = "server->client",
                 accept_timeout: float = 120.0) -> None:
        self.server_port = int(server_port)
        self.blackhole = blackhole
        self.accept_timeout = float(accept_timeout)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = int(self.listener.getsockname()[1])
        self.forwarded = {"client->server": 0, "server->client": 0}
        self._stop = threading.Event()
        self._accepted = threading.Event()
        self.client_sock: socket.socket | None = None
        self._threads: list[threading.Thread] = []

    def start(self) -> "DirectionProxy":
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _pump(self, src, dst, forward: bool) -> None:
        try:
            while not self._stop.is_set():
                try:
                    chunk = src.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                key = "client->server" if forward else "server->client"
                self.forwarded[key] += len(chunk)
                if not forward:
                    continue  # blackhole: read, counted, never delivered
                try:
                    dst.sendall(chunk)
                except OSError:
                    break
        except OSError:
            pass

    def _run(self) -> None:
        import struct
        try:
            self.listener.settimeout(self.accept_timeout)
            client, _ = self.listener.accept()
        except OSError:
            return
        self.client_sock = client
        self._accepted.set()
        try:
            upstream = socket.create_connection(("127.0.0.1", self.server_port),
                                                timeout=self.accept_timeout)
        except OSError:
            try:
                client.close()
            except OSError:
                pass
            return
        c2s_forward = self.blackhole != "client->server"
        s2c_forward = self.blackhole != "server->client"
        for src, dst, fwd in ((client, upstream, c2s_forward),
                              (upstream, client, s2c_forward)):
            t = threading.Thread(target=self._pump, args=(src, dst, fwd),
                                 daemon=True)
            t.start()
            self._threads.append(t)
        for t in self._threads:
            t.join(timeout=self.accept_timeout)

    def wait_accepted(self, timeout: float = 30.0) -> bool:
        return self._accepted.wait(timeout=timeout)

    def reset_client(self, timeout: float = 30.0) -> bool:
        """RST only the client-facing socket (test-only fault)."""
        import struct
        if not self._accepted.wait(timeout=timeout):
            return False
        sock = self.client_sock
        if sock is None:
            return False
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            struct.pack("ii", 1, 0))
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        return True

    def stop(self) -> None:
        self._stop.set()
        for sock in (self.listener, self.client_sock):
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass

    def record(self) -> dict:
        return {"port": self.port, "blackholed": self.blackhole,
                "client_to_server_bytes": self.forwarded["client->server"],
                "server_to_client_bytes": self.forwarded["server->client"]}


# ══════════════════════════════════════════════════════════════════════════
# PostgreSQL target resolution
# ══════════════════════════════════════════════════════════════════════════
def parse_dsn(dsn: str) -> dict:
    from urllib.parse import unquote, urlparse
    url = urlparse(dsn)
    return {"host": url.hostname or "", "port": url.port or 0,
            "database": (url.path or "").lstrip("/"),
            "user": unquote(url.username or ""),
            "password": unquote(url.password or "")}


def assert_disposable_target(target: dict) -> dict:
    """Closed allowlist. These tests DROP/CREATE databases."""
    host = str(target.get("host", "")).lower()
    port = int(target.get("port", 0) or 0)
    database = str(target.get("database", ""))
    if host not in LOOPBACK_HOSTS:
        raise PgTargetError(f"pg host {host!r} is not loopback — refused")
    if port == PROD_PG_PORT:
        raise PgTargetError(f"pg port {port} is the production PG port — refused")
    if database == PROD_PG_DATABASE:
        raise PgTargetError(f"pg database {database!r} is production — refused")
    if database not in ALLOWED_PG_DATABASES:
        raise PgTargetError(
            f"pg database {database!r} is not an allowed F2 experiment database "
            f"{sorted(ALLOWED_PG_DATABASES)} — refused (this harness DROPs it)")
    return {"host": host, "port": port, "database": database,
            "user": target.get("user", ""), "verdict": "PASS"}


def resolve_pg_target(dsn: str = "", pg_port: int = 0, database: str = "f2e2e",
                      user: str = "", password: str = "") -> dict:
    """Build the target from ``--dsn`` / ``$F2_PG_DSN`` or ``--pg-port``.

    I3: the password is only ever read from the environment here, and only
    ever leaves this function through a child env var or a psycopg2 keyword.
    """
    dsn = (dsn or os.environ.get("F2_PG_DSN", "") or "").strip()
    if dsn:
        target = parse_dsn(dsn)
        if not target["password"]:
            target["password"] = password or os.environ.get(
                "F2_PG_PASSWORD", "") or os.environ.get("PGPASSWORD", "") or ""
    else:
        if not pg_port:
            raise PgTargetError(
                "no PG target: pass --pg-port <port> or set F2_PG_DSN")
        target = {"host": "127.0.0.1", "port": int(pg_port),
                  "database": database or "f2e2e",
                  "user": user or os.environ.get("F2_PG_USER", "") or "v3user",
                  "password": password or os.environ.get(
                      "F2_PG_PASSWORD", "") or os.environ.get("PGPASSWORD", "") or ""}
    assert_disposable_target(target)
    return target


def pg_connect(target: dict, dbname: str | None = None, timeout: int = 10):
    import psycopg2
    return psycopg2.connect(
        host=target["host"], port=int(target["port"]),
        database=dbname or target["database"], user=target["user"],
        password=target.get("password", "") or "", connect_timeout=timeout)


def recreate_database(target: dict) -> dict:
    """Drop + recreate the allowlisted DB so no stale row can back a PASS."""
    assert_disposable_target(target)
    name = target["database"]
    admin = dict(target)
    for candidate in ("postgres", "template1"):
        try:
            pg_connect(admin, dbname=candidate).close()
            admin["maintenance_db"] = candidate
            break
        except Exception:
            continue
    else:
        raise PgTargetError(
            f"cannot reach a maintenance database on {target['host']}:"
            f"{target['port']} to drop/create {name}")
    conn = pg_connect(admin, dbname=admin["maintenance_db"])
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=%s AND pid <> pg_backend_pid()", (name,))
        terminated = len(cur.fetchall() or [])
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    return {"database": name, "dropped_and_created": True,
            "backends_terminated": terminated,
            "maintenance_db": admin["maintenance_db"], "verdict": "PASS"}


def bootstrap_schema(target: dict, repo_root: Path = REPO_ROOT) -> dict:
    """Apply the packaged fresh bootstrap to the disposable database.

    Uses the repo's own ``schema/alpha_bootstrap.sql`` with its include
    markers expanded, so the harness proves the SHIPPED DDL, not a private
    copy of it.
    """
    sql = load_bootstrap_sql(repo_root)
    conn = pg_connect(target)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql)
    finally:
        conn.close()
    return {"database": target["database"],
            "sql_source": str(schema_dir(repo_root) / "alpha_bootstrap.sql"),
            "sql_bytes": len(sql.encode("utf-8")), "applied": True,
            "verdict": "PASS"}


def schema_dir(repo_root: Path = REPO_ROOT) -> Path:
    return Path(repo_root) / "src" / "v3-core" / "schema"


def _packaged_schema_dir() -> Path | None:
    try:
        import v3core.schema as pkg
        return Path(pkg.__file__).resolve().parent
    except Exception:
        return None


def load_bootstrap_sql(repo_root: Path = REPO_ROOT) -> str:
    """The shipped ``alpha_bootstrap.sql`` with include markers expanded."""
    directory = schema_dir(repo_root)
    path = directory / "alpha_bootstrap.sql"
    if not path.is_file():
        packaged = _packaged_schema_dir()
        if packaged is None or not (packaged / "alpha_bootstrap.sql").is_file():
            raise F2HarnessError(
                f"cannot locate alpha_bootstrap.sql under {directory} or in the "
                f"installed v3core package")
        directory = packaged
        path = directory / "alpha_bootstrap.sql"
    text = path.read_text(encoding="utf-8")

    def _expand(match: re.Match) -> str:
        return (directory / Path(match.group(1)).name).read_text(encoding="utf-8")

    return re.sub(r"^--\s*>>>\s*ALPHA_BOOTSTRAP_INCLUDE:\s*(\S+)\s*<<<\s*$",
                  _expand, text, flags=re.M)


def schema_readback(target: dict) -> dict:
    """Read conversation_stream's identity columns + index back out of PG."""
    conn = pg_connect(target)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='conversation_stream' ORDER BY 1")
        cols = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT indexdef FROM pg_indexes WHERE tablename="
                    "'conversation_stream' AND indexname=%s", (IDENTITY_INDEX,))
        row = cur.fetchone()
        idx = (row[0] if row else "") or ""
    finally:
        conn.close()
    ok = "host" in cols and "event_id" in cols and bool(idx)
    return {"database": target["database"], "columns": cols,
            "has_host": "host" in cols, "has_event_id": "event_id" in cols,
            "identity_index_present": bool(idx), "identity_index_definition": idx or None,
            "verdict": "PASS" if ok else "FAIL"}


def identity_readback(target: dict, host: str, session_id: str,
                      event_id: str) -> dict:
    """Row count + the actual rows for one canonical identity."""
    conn = pg_connect(target)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM conversation_stream "
                    "WHERE session_id=%s AND host=%s AND event_id=%s",
                    (session_id, host, event_id))
        count = int(cur.fetchone()[0])
        cur.execute("SELECT id, role, source, timestamp FROM conversation_stream "
                    "WHERE session_id=%s AND host=%s AND event_id=%s "
                    "ORDER BY id", (session_id, host, event_id))
        rows = [{"id": r[0], "role": r[1], "source": r[2],
                 "timestamp": str(r[3])} for r in cur.fetchall()]
    finally:
        conn.close()
    return {"identity": {"host": host, "session_id": session_id,
                         "event_id": event_id},
            "row_count": count, "rows": rows}


def session_row_count(target: dict, session_id: str) -> int:
    conn = pg_connect(target)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM conversation_stream WHERE session_id=%s",
                    (session_id,))
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def marker_state(root: Path, host: str, session_id: str,
                 event_id: str) -> dict:
    """Read the durable transport markers for ONE canonical identity off disk.

    The accepted tombstone path is derived exactly as the product derives it
    (``ingest.py:263-279``): ``sha256("<session>\\x00<event>")``, namespaced
    under the host when the host is real and in the legacy namespace
    otherwise. The namespace is part of the PATH, so it is part of the
    lookup: a marker written under one host must never satisfy the lookup
    for another, which is precisely what the F2 host-scoping contract
    guarantees. (A recursive scan here would silently collapse that and let
    a legacy marker vouch for a real host — so the accepted lookup is by
    exact path, not by glob.)

    The pending outbox marker is content-addressed (``_live_job_id``), so it
    is matched BY PAYLOAD (``session_id``/``msg_id``/``host``) rather than by
    filename — the same discipline the W3 pristine-base probe uses, so no
    file is missed and no unrelated identity is swept in.

    This is the observation mission §6 W1 and W2 are written against:
    whether the accepted tombstone exists, and whether a pending outbox
    item for the same identity survives alongside it.
    """
    ident = hashlib.sha256(
        f"{session_id}\x00{event_id}".encode("utf-8", errors="ignore")
    ).hexdigest()
    host_key = str(host or "").strip()
    try:
        from v3core.bridge_contract import LEGACY_HOST
    except Exception:  # pragma: no cover — the constant is "legacy"
        LEGACY_HOST = "legacy"

    accepted_dir = Path(root) / "profile" / "j" / "accepted_live_buffer"
    pending_dir = Path(root) / "profile" / "j" / "pending_live_buffer"

    # Exact path, mirroring _live_accepted_path's two branches.
    if host_key and host_key != LEGACY_HOST:
        safe_host = host_key.replace("..", "_").replace("/", "_").replace("\\", "_")
        expected = accepted_dir / safe_host / f"{ident}.json"
        namespace = "namespaced"
    else:
        expected = accepted_dir / f"{ident}.json"
        namespace = "legacy"
    accepted_present = expected.is_file()

    pending_files: list[str] = []
    if pending_dir.is_dir():
        for candidate in pending_dir.rglob("*.json"):
            if candidate.name.startswith("."):
                continue  # in-flight temp file, not a durable marker
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (str(data.get("session_id") or "") == str(session_id)
                    and str(data.get("msg_id") or "") == str(event_id)
                    and str(data.get("host") or "") == host_key):
                pending_files.append(str(candidate))

    accepted_files = [str(expected)] if accepted_present else []
    return {
        "identity": {"host": host, "session_id": session_id, "event_id": event_id},
        "identity_hash": ident,
        "host_namespace": namespace,
        "expected_accepted_path": str(expected),
        "accepted_present": accepted_present,
        "accepted_files": sorted(accepted_files),
        "pending_present": bool(pending_files),
        "pending_files": sorted(pending_files),
        # The mission §6 "pending/accepted transport converges" condition:
        # the healthy end state is accepted WITHOUT a surviving pending item.
        "converged": accepted_present and not pending_files,
    }


def qa_rows_for_session(target: dict, session_id: str) -> int:
    """Count derived QA rows for a session (mission §6 duplicate-QA check)."""
    conn = pg_connect(target)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM qa_pairs WHERE session_id=%s",
                    (session_id,))
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def evaluate_case_section6(case: str, *, identity_rows: int | None,
                           duplicate_qa_rows: int,
                           marker: dict | None,
                           root: Path) -> dict:
    """The six mission §6 end-state conditions, as ONE pure function.

    §6 states them once for the whole W0–W4 run:

        canonical source rows = 1
        no duplicate QA caused by same event
        pending/accepted transport converges
        no foreign data root
        no production config/profile
        no production DB

    Evaluating them per case (rather than only narrating them in the module
    docstring) is what makes the acceptance scope *enforced*: a case that
    leaves a surviving pending marker, or a second QA row, is a FAIL even
    when its own headline count is right.

    ``expected_rows`` is 1 for every §6 case; W2's "accepted persisted but
    pending survives" is judged on marker convergence, not on row count.
    """
    problems: list[str] = []
    checks: dict[str, Any] = {}

    # 1. canonical source rows = 1
    if identity_rows is None:
        problems.append("canonical source row count was not measured")
        checks["canonical_rows"] = False
    else:
        checks["canonical_rows"] = identity_rows == 1
        if identity_rows != 1:
            problems.append(f"canonical source rows = {identity_rows}, want 1")

    # 2. no duplicate QA caused by the same event
    checks["no_duplicate_qa"] = duplicate_qa_rows <= 1
    if duplicate_qa_rows > 1:
        problems.append(
            f"the same event produced {duplicate_qa_rows} qa_pairs rows "
            f"(want at most 1)")

    # 3. pending/accepted transport converges
    if marker is None:
        checks["transport_converged"] = False
        problems.append("durable marker state was not read back")
    else:
        checks["transport_converged"] = bool(marker.get("converged"))
        if not marker.get("converged"):
            problems.append(
                f"transport did not converge: accepted="
                f"{marker.get('accepted_present')} "
                f"pending_survives={marker.get('pending_present')}")

    # 4. no foreign data root. The marker readback above is itself the
    # evidence: every marker path it reported was resolved by walking the
    # ONE isolated run root, so an observed marker is by construction under
    # it. Assert that explicitly rather than assuming it.
    marker_paths = list((marker or {}).get("accepted_files") or []) + list(
        (marker or {}).get("pending_files") or [])
    checks["isolated_data_root"] = bool(
        marker is not None
        and all(is_under(p, root) for p in marker_paths))
    if not checks["isolated_data_root"]:
        problems.append("a durable marker resolved outside the isolated root")
    return {"case": case, "checks": checks,
            "section6_ok": not problems, "section6_problems": problems}


def wait_identity_count(target: dict, host: str, session_id: str, event_id: str,
                        want: int, timeout: float = 420.0) -> int:
    """Drain-wait for one identity's row count to reach ``want``."""
    deadline = time.time() + timeout
    count = identity_readback(target, host, session_id, event_id)["row_count"]
    while count < want and time.time() < deadline:
        time.sleep(1.0)
        count = identity_readback(target, host, session_id, event_id)["row_count"]
    return count


# ══════════════════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════════════════
def post_event(port: int, payload: dict, timeout: float = 120.0) -> tuple[int, dict]:
    """Real ``POST /events``. Every W case goes through this or a child."""
    url = f"http://127.0.0.1:{int(port)}/events"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return int(exc.code), json.loads(body)
        except Exception:
            return int(exc.code), {"raw": body[:300]}


def get_health(port: int, timeout: float = 30.0) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{int(port)}/health", timeout=timeout) as resp:
            return int(resp.status), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return int(exc.code), {}
    except Exception as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"[:200]}


def raw_post_through_proxy(proxy_port: int, payload: dict,
                           read_timeout: float = 600.0) -> dict:
    """POST via a raw socket, optionally never reading a real ACK.

    Returns the byte count actually received, so "the caller got no ACK" is
    a measurement rather than a claim.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    head = (f"POST /events HTTP/1.1\r\nHost: 127.0.0.1:{int(proxy_port)}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n").encode("ascii")
    sock = socket.create_connection(("127.0.0.1", int(proxy_port)), timeout=30)
    received = b""
    error: str | None = None
    try:
        sock.sendall(head + body)
        sock.settimeout(read_timeout)
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout as exc:
                error = f"socket.timeout: {exc}"
                break
            except OSError as exc:
                error = f"{type(exc).__name__}: {exc}"[:200]
                break
            if not chunk:
                break
            received += chunk
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return {"bytes_received": len(received),
            "got_http_ack": received.startswith(b"HTTP"),
            "error": error,
            "head": received[:120].decode("utf-8", "replace")}


# ══════════════════════════════════════════════════════════════════════════
# I4 — provider-call proof
# ══════════════════════════════════════════════════════════════════════════
def scan_provider_calls(log_paths: Iterable[str | Path]) -> dict:
    """Count provider-call markers. Non-zero means the run is invalid."""
    per_file: dict[str, list[str]] = {}
    no_request = 0
    total = 0
    scanned: list[str] = []
    for item in log_paths:
        path = Path(item)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        scanned.append(str(path))
        hits: list[str] = []
        for line in text.splitlines():
            if _NO_REQUEST_RE.search(line):
                no_request += 1
                continue
            if _REAL_ATTEMPT_RE.search(line) or any(m in line for m in PROVIDER_CALL_MARKERS):
                hits.append(line.strip()[:200])
        if hits:
            per_file[str(path)] = hits[:20]
            total += len(hits)
    return {"provider_call_count": total, "files_scanned": scanned,
            "no_request_confirmations": no_request,
            "no_request_note": "embed_for_write DEGRADED/NO_INPUT attempts=0 "
                               "lines — the branch that issued NO request",
            "markers_checked": list(PROVIDER_CALL_MARKERS),
            "files_with_markers": per_file,
            "verdict": "PASS-ZERO-PROVIDER-CALLS" if total == 0 else "FAIL"}


# ══════════════════════════════════════════════════════════════════════════
# child roles
# ══════════════════════════════════════════════════════════════════════════
def arm_barrier_on_served_core(barrier: dict) -> "types.SimpleNamespace":
    """B1 seam: instrument the LiveBuffer the HTTP handler ACTUALLY writes to.

    ``v3core.serve.serve()`` does not take a core — it constructs its OWN
    ``V3Core`` and calls ``build_handler(core, ...)`` (serve.py:399-401). A
    harness that pre-creates a core, wraps ``core.live_buffer`` and then calls
    ``serve()`` therefore wraps a buffer that will never receive an HTTP
    event: the barrier is unreachable and W0 waits out its full timeout.

    So the seam goes in at the CLASS level, inside the CHILD only: every
    ``V3Core`` built in this process gets its LiveBuffer wrapped before the
    handler can see it. The product is untouched — no failpoint, no changed
    host contract, no extra attribute — and the real buffer, the real pg
    store, the real config and the real outbox are all still what is
    instrumented.
    """
    from v3core import V3Core

    specs: list[FlushBarrier] = []
    live_buffers: list[Any] = []
    original_property = V3Core.live_buffer

    def _property(core_self):
        buffer = original_property.fget(core_self)
        if not any(b is buffer for b in live_buffers):
            live_buffers.append(buffer)
            spec = FlushBarrier(barrier["host"], barrier["session_id"],
                                barrier["event_id"], Path(barrier["marker_path"]),
                                Path(barrier["release_path"])
                                if barrier.get("release_path") else None,
                                timeout=float(barrier.get("timeout", 900)))
            specs.append(spec)
            spec.wrap(buffer)
        return buffer

    V3Core.live_buffer = property(_property)

    def _restore() -> None:
        V3Core.live_buffer = original_property

    return types.SimpleNamespace(barriers=specs, buffers=live_buffers,
                                 restore=_restore,
                                 installed_on=lambda: len(live_buffers) > 0)


def _run_serve_child(args: argparse.Namespace) -> int:
    """``child-serve``: self-report, optionally arm a barrier, then serve.

    The barrier is armed as a CLASS-level seam BEFORE ``serve()`` runs (see
    :func:`arm_barrier_on_served_core`), so it lands on the LiveBuffer the
    HTTP handler actually writes through. It is then verified: the child
    reports whether a LiveBuffer was really instrumented, and a barrier that
    could not be installed is a hard refusal rather than a 600 s timeout.
    """
    barrier: dict | None = None
    if args.barrier_host or args.barrier_event:
        barrier = {
            "host": args.barrier_host or "",
            "session_id": args.barrier_session,
            "event_id": args.barrier_event,
            "marker_path": args.barrier_marker,
            "release_path": args.barrier_release,
            "timeout": float(args.barrier_timeout),
        }
    # W1 (§6) needs the seam on the OTHER side of the PG commit, so it gets
    # its own arm and its own marker. Only one barrier can be armed per
    # child: the two park different methods of the same buffer.
    tombstone: dict | None = None
    if args.tombstone_host or args.tombstone_event:
        tombstone = {
            "host": args.tombstone_host or "",
            "session_id": args.tombstone_session,
            "event_id": args.tombstone_event,
            "marker_path": args.tombstone_marker,
            "timeout": float(args.tombstone_timeout),
        }
    # W2 (§6) parks a THIRD phase: after the accepted tombstone has really
    # been persisted and before the pending marker is unlinked. It is
    # mutually exclusive with the other two for the same reason: one parked
    # phase per child.
    post_accepted: dict | None = None
    if args.post_accepted_host or args.post_accepted_event:
        post_accepted = {
            "host": args.post_accepted_host or "",
            "session_id": args.post_accepted_session,
            "event_id": args.post_accepted_event,
            "marker_path": args.post_accepted_marker,
            "timeout": float(args.post_accepted_timeout),
        }
    armed = [name for name, spec in (("--barrier-*", barrier),
                                      ("--tombstone-*", tombstone),
                                      ("--post-accepted-*", post_accepted))
             if spec is not None]
    if len(armed) > 1:
        raise F2HarnessError(
            "a child can arm only one barrier: --barrier-* parks the pre-commit "
            "flush, --tombstone-* parks the post-commit accepted tombstone, "
            f"--post-accepted-* parks the post-accepted pending unlink (got "
            f"{armed})")
    print(json.dumps({"event": "child_start", "role": "child-serve",
                      "pid": os.getpid(), "argv_role": "child-serve",
                      "barrier": bool(barrier),
                      "tombstone_barrier": bool(tombstone),
                      "post_accepted_barrier": bool(post_accepted)},
                     ensure_ascii=False), flush=True)
    provenance = collect_child_provenance()
    print(json.dumps({"event": "provenance", "role": "child-serve",
                      "pid": os.getpid(), **provenance}, ensure_ascii=False),
          flush=True)
    if provenance.get("refused"):
        return 97  # distinguished from a crash: a TEST-mode refusal

    from v3core.serve import serve

    # Seam installed BEFORE serve() constructs its own V3Core. serve() builds a
    # second core internally (serve.py:399-401), so wrapping a core created
    # here would instrument a buffer the HTTP handler never uses.
    arm = arm_barrier_on_served_core(barrier) if barrier else None
    tomb = arm_tombstone_barrier_on_served_core(tombstone) if tombstone else None
    post = (arm_post_accepted_barrier_on_served_core(post_accepted)
            if post_accepted else None)
    arms = (arm, tomb, post)
    try:
        serve(host="127.0.0.1", port=int(args.port),
              profile=args.profile or "default", ready_json=True)
    except BaseException:
        for a in arms:
            if a is not None:
                a.restore()
        raise
    for a in arms:
        if a is not None:
            a.restore()
    return 0


def _run_client_child(args: argparse.Namespace) -> int:
    """``child-client``: a separate client PROCESS driving real HTTP.

    Modes:
      * ``deliver``      — normal POST; report the ACK.
      * ``no-ack``       — POST through a proxy that blackholes the ACK and
                           then RSTs; report how many bytes actually arrived.
      * ``fail-reset``   — the request never leaves this process; the peer
                           resets, so a real transport exception is raised.
      * ``fail-timeout`` — the request is swallowed by the proxy and the
                           socket read times out, so a real timeout is raised.
    """
    print(json.dumps({"event": "child_start", "role": "child-client",
                      "pid": os.getpid(), "mode": args.mode,
                      "start_barrier": args.start_barrier,
                      "identity_release": args.identity_release},
                     ensure_ascii=False),
          flush=True)
    # The identity-release handshake runs BEFORE the start barrier, before the
    # payload is parsed and before any socket is opened. A client that is
    # allowed to reach its workload first can finish an immediate duplicate
    # HTTP POST and exit while the parent is still inside the (slow) OS
    # identity capture — which is exactly how PID3356 died on f2-04. Parking
    # here makes the child's window non-empty for as long as the parent needs.
    #
    # A FAILED handshake is refused HERE, before the workload gate is even
    # waited on: waiting out a ``--start-barrier`` for a child whose identity
    # the parent never verified would spend the gate's whole timeout doing
    # nothing, then run the workload anyway. Refuse first, gate second.
    #
    # This is NOT the ``--start-barrier`` workload/concurrency gate: that one
    # is armed per W case to hold the REQUEST, and it is preserved as-is for
    # a child that DID complete its identity handshake.
    release_path = str(getattr(args, "identity_release", "") or "")
    identity_note: dict = {}
    if release_path:
        identity_note = await_identity_release(release_path,
                                               float(args.identity_timeout))
    if identity_note.get("released") is False:
        # Never released: the parent could not capture/verify an identity, so
        # this child refuses to run its workload at all — and refuses BEFORE
        # the workload gate, before the payload is parsed and before any
        # socket is opened. Reporting a synthetic result would be the same
        # unverified-pass failure the handshake exists to prevent.
        print(json.dumps({"event": "client_result", "role": "child-client",
                          "pid": os.getpid(),
                          "outcome": "identity-release-timeout",
                          "error": identity_note.get("error")},
                         ensure_ascii=False), flush=True)
        return 4
    if args.start_barrier:
        marker = Path(args.start_barrier)
        deadline = time.time() + float(args.start_timeout)
        while time.time() < deadline and not marker.exists():
            time.sleep(0.01)
        if not marker.exists():
            print(json.dumps({"event": "client_result", "role": "child-client",
                              "pid": os.getpid(), "outcome": "start-barrier-timeout"},
                             ensure_ascii=False), flush=True)
            return 3
    payload = json.loads(args.payload)
    result: dict = {"mode": args.mode, "pid": os.getpid(), "url_port": int(args.port)}
    try:
        if args.mode == "deliver":
            status, body = post_event(int(args.port), payload,
                                      timeout=float(args.http_timeout))
            result.update({"outcome": "delivered", "http": status, "ack": body})
        elif args.mode == "no-ack":
            result.update({"outcome": "no-ack", **raw_post_through_proxy(
                int(args.port), payload, read_timeout=float(args.http_timeout))})
        elif args.mode in ("fail-reset", "fail-timeout"):
            url = f"http://127.0.0.1:{int(args.port)}/events"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=float(args.http_timeout)) as resp:
                    result.update({"outcome": "unexpected-success",
                                   "http": int(resp.status)})
            except Exception as exc:
                result.update({"outcome": "transport-error",
                               "error_type": type(exc).__name__,
                               "error": str(exc)[:200]})
        else:
            result.update({"outcome": "unknown-mode", "mode": args.mode})
    except Exception as exc:  # noqa: BLE001 — a transport failure IS the datum
        result.update({"outcome": "client-exception",
                       "error_type": type(exc).__name__, "error": str(exc)[:200]})
    print(json.dumps({"event": "client_result", "role": "child-client", **result},
                     ensure_ascii=False), flush=True)
    return 0


def await_identity_release(release_path: str, timeout: float = 600.0,
                           sleep: Callable[[float], None] = time.sleep) -> dict:
    """Park until the parent releases this child's IDENTITY HANDSHAKE.

    Distinct from the ``--start-barrier`` workload gate: this one exists only
    so the parent can capture and verify a live OS identity triple before the
    child is allowed to open a socket. Bounded, poll-based, and it never
    fabricates a pass: on expiry it returns ``released=False`` and the caller
    refuses to run the workload.

    The release is an EXISTENCE gate on a per-spawn-unique, run-local path —
    the file's CONTENT is never read and authenticates nothing (see
    :func:`identity_release_path`). What makes it safe is that the path
    cannot be shared: a per-spawn UUID is in the name, and the parent clears
    the path before spawning, so neither an earlier run nor an earlier spawn
    of the same tag can release this child.
    """
    token = str(release_path or "").strip()
    if not token:
        # No handshake requested: an un-gated spawn is unchanged behaviour.
        return {"released": True, "reason": "no identity-release armed",
                "path": None}
    deadline = time.time() + float(timeout)
    while True:
        if Path(token).exists():
            return {"released": True, "reason": "identity release present",
                    "path": token}
        if time.time() >= deadline:
            return {"released": False, "path": token,
                    "error": (f"identity release {token!r} never appeared within "
                              f"{timeout}s — refusing to run the workload")}
        sleep(0.01)


def identity_release_path(run_root: Path, run_nonce: str, label: str,
                          spawn_id: str = "") -> Path:
    """The run-local release path for ONE child spawn.

    What this is, stated honestly: an EXISTENCE gate. The child waits for the
    file to appear and reads nothing from it, and the parent writes whatever
    it likes into it — so this function must not claim the content
    authenticates anything, and it does not. The spec does not require token
    authentication, and inventing one here would be a claim the code does not
    enforce.

    What it DOES guarantee is that a stale file cannot release the wrong
    child. Three independent properties compose into a per-spawn-unique name:

    * ``run_nonce`` is minted once per ``make`` run, so a file left behind by
      an earlier run under the same root is never this run's file;
    * ``label`` is unique per child tag within a run;
    * ``spawn_id`` is a fresh UUID minted for THIS spawn, so even the same tag
      spawned twice in the same run resolves to two different paths.

    :func:`_clear_stale_release` then removes the path before the spawn, so
    existence is caused by this spawn's publish and by nothing else.
    """
    parts = ["identity_release", str(run_nonce or "no-nonce"),
             str(label or "unlabelled")]
    if spawn_id:
        parts.append(str(spawn_id))
    return (Path(run_root) / "barriers" /
            (".".join(parts) + ".token"))


def _mint_identity_spawn_id() -> str:
    """The per-spawn unique suffix that makes a release path single-use."""
    return uuid.uuid4().hex


def _clear_stale_release(release: Path) -> None:
    """Remove any pre-existing release file so existence is this spawn's.

    With a per-spawn UUID in the name this should never find anything; it is
    the belt to that braces, and a path that CANNOT be cleared is a hard
    error rather than a release the child might inherit.
    """
    try:
        Path(release).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise F2HarnessError(
            f"cannot clear a stale identity release {release}: {exc}") from exc


def _identity_release_file(path: Path, spawn_id: str) -> None:
    """Publish the release: create the file, write the spawn id into it.

    The gate is ``exists()``; the content is a human-readable record of which
    spawn this release belongs to, NOT an authentication token, and nothing
    in the protocol verifies it. The write is a single small ``write_text``
    and is not claimed to be atomic: if a reader ever saw this file
    half-written it would still see it EXIST, which is the only thing the
    child acts on.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(spawn_id, encoding="utf-8")


def spawn_child(role: str, args: Sequence[str], env: dict, workdir: Path,
                stderr_path: Path,
                registry: ChildRegistry | None = None) -> OwnedChild:
    """Spawn one child and record its verified identity triple.

    I8: the identity is captured from the OS immediately after the spawn and
    is what every later cleanup re-verifies.

    DETERMINISTIC LAUNCH HANDSHAKE. ``process_identity`` shells out to
    PowerShell and costs seconds, while a fast child (an immediate duplicate
    HTTP POST) can print its result and exit inside that window — f2-04 W4 lost
    PID3356 exactly there. So the identity handshake is done HERE, at the one
    boundary every spawn path passes through, rather than at each call site:

      1. mint a per-spawn unique release path for this spawn;
      2. append ``--identity-release`` to the child's argv;
      3. Popen;
      4. start the stdout reader (gated children only — the child's
         ``child_start`` announcement, and its entire window, are only
         observable through it);
      5. for a gated child: wait for that ``child_start`` (it parks before any
         workload), then capture the REAL live OS identity triple;
      6. construct, start and REGISTER the OwnedChild;
      7. only then publish the release.

    Steps 5-7 are fail-closed, and the ORDER is the contract: the identity is
    captured BEFORE the OwnedChild is registered, and the release is
    published only AFTER registration — inside the same protected ``try`` —
    so a release-publish failure cleans the (already verified) child through
    the ordinary verified route instead of parking an orphan nobody tracks.

    ``child-serve`` is NOT gated this way: it blocks in ``serve()`` for the
    whole run, so its window is already non-empty and its existing
    ``provenance``-then-``ready`` sequence is left exactly as it was. Only the
    short-lived, result-emitting ``child-client`` needs the park, and it is
    applied here so the raw W0/W1/W2/W3/W4 ``spawn_child`` calls and
    ``RunContext.client_child`` are all covered by construction.
    """
    args = [str(a) for a in args]
    run_root = Path(stderr_path).parent.parent      # <root>/logs/<file> -> <root>
    label = f"{role}.{Path(stderr_path).stem}"
    gated = role == "child-client"
    spawn_id = _mint_identity_spawn_id() if gated else ""
    release = (identity_release_path(run_root, _RUN_NONCE, label, spawn_id)
               if gated else None)
    if gated:
        # Existence is this spawn's alone: clear any leftover at this exact
        # path before the child can possibly observe it.
        _clear_stale_release(release)
        args += ["--identity-release", str(release),
                 "--identity-timeout", str(IDENTITY_RELEASE_TIMEOUT)]

    proc = subprocess.Popen(child_argv(role, *args), cwd=str(workdir),
                            env=env, stdout=subprocess.PIPE,
                            stderr=open(stderr_path, "w", encoding="utf-8",
                                        errors="replace"),
                            text=True, encoding="utf-8", errors="replace")
    # Constructed with a PLACEHOLDER identity purely so the reader can be
    # started; the placeholder is overwritten by the real OS triple before the
    # child is registered or released. It is marked ``identity_pending`` and
    # is never what cleanup verifies.
    child = OwnedChild(role, proc,
                       {"pid": int(proc.pid), "ppid": None, "created": "",
                        "cmdline": "", "identity_pending": True})
    try:
        if gated:
            # The reader must run before anything is waited on: ``child_start``
            # is only observable through it, and the child then parks so the
            # window the identity capture needs stays open. This early pump is
            # the ONE thing the client branch needs before its identity.
            child.start(Path(stderr_path))
            child.wait_event("child_start", timeout=IDENTITY_START_TIMEOUT)
            identity = process_identity(proc.pid)
            if identity is None:
                raise F2HarnessError(
                    f"spawned {role} pid={proc.pid} but the OS reports no such "
                    f"process — refusing to continue without a verifiable "
                    f"identity")
            identity = dict(identity, marker="f2-source-idempotency")
            child.identity = identity
            child.meta.update({"argv_args": args,
                               "stderr_log": str(stderr_path),
                               "identity_handshake": {
                                   "gated": True,
                                   "release_spawn_id": spawn_id,
                                   "release_path": str(release),
                                   "child_start_seen": True}})
            if registry is not None:
                registry.add(child)
        else:
            # Un-gated (child-serve): the original f2-04 lifecycle — capture
            # the live identity FIRST, then start/register. serve() blocks for
            # the whole run, so its window is already non-empty and it needs
            # no pre-identity pump placeholder.
            identity = process_identity(proc.pid)
            if identity is None:
                raise F2HarnessError(
                    f"spawned {role} pid={proc.pid} but the OS reports no such "
                    f"process — refusing to continue without a verifiable "
                    f"identity")
            identity = dict(identity, marker="f2-source-idempotency")
            child.identity = identity
            child.start(Path(stderr_path))
            child.meta.update({"argv_args": args, "stderr_log": str(stderr_path),
                               "identity_handshake": {"gated": False,
                                                      "child_start_seen": False}})
            if registry is not None:
                registry.add(child)
        if gated:
            # Registered AND identity-verified: now, and only now, may the
            # child do any work. A publish failure below is a cleanup of an
            # already-verified owned child through the ordinary route.
            _identity_release_file(release, spawn_id)
    except BaseException as exc:
        # Never leave a child behind. The release is never written on a path
        # that fails before this point, so a gated child cannot proceed to its
        # workload unverified.
        _abort_spawn(role, proc, stderr_path, exc, child, registry)
        raise
    return child


def _abort_spawn(role: str, proc: subprocess.Popen, stderr_path: Path,
                 cause: BaseException, child: OwnedChild | None = None,
                 registry: "ChildRegistry | None" = None) -> None:
    """Teardown for a spawn that failed its launch handshake.

    I8 governs this too. There is no bare-PID kill anywhere on this path:

    * if the live OS identity triple was ALREADY captured, the child is a
      verified ``OwnedChild`` — optionally already in the registry — so it is
      stopped through the ordinary verified route (:meth:`OwnedChild.stop` →
      :func:`terminate_owned`, which re-reads the triple before signalling and
      does a tree kill). It stays in the registry until proven dead, so a
      cleanup fault surfaces as a leftover instead of vanishing;
    * if the identity was NEVER captured, no verified identity exists and the
      child has NOT been released to its workload, so the only owned handle
      is the parent's own ``Popen``: ``terminate()``/``wait()`` on that handle,
      with no process-table lookup and no PID-based tree kill.

    Never masks ``cause`` and never claims success: a child that cannot be
    proven dead is reported as such, and the caller re-raises the original
    failure either way.
    """
    stopped: dict | None = None
    if child is not None and not child.identity.get("identity_pending"):
        # Verified identity exists: use the ordinary owned route.
        try:
            stopped = child.stop()
        except Exception as exc:  # noqa: BLE001 — report, never swallow
            log(f"[spawn] {role} pid={proc.pid} could not be stopped through "
                f"the verified route: {type(exc).__name__}: {exc}")
    else:
        # No verified identity. The child was never released, so the parent's
        # own Popen handle is the only thing it owns — terminate it there.
        try:
            proc.terminate()
        except Exception as exc:  # noqa: BLE001 — report, never swallow
            log(f"[spawn] {role} pid={proc.pid} Popen.terminate failed: "
                f"{type(exc).__name__}: {exc}")
        try:
            proc.wait(timeout=10)
        except Exception as exc:  # noqa: BLE001 — reported below
            log(f"[spawn] {role} pid={proc.pid} Popen.wait did not confirm "
                f"exit: {type(exc).__name__}: {exc}")
    try:
        detail = Path(stderr_path).read_text(encoding="utf-8",
                                            errors="replace")[-200:]
    except Exception:  # noqa: BLE001
        detail = ""
    clean = (stopped or {}).get("clean") if stopped is not None else \
        (proc.poll() is not None)
    log(f"[spawn] {role} pid={proc.pid} launch handshake FAILED: "
        f"{type(cause).__name__}: {cause} | proven_dead={bool(clean)} "
        f"| child stderr: {detail}")


# ══════════════════════════════════════════════════════════════════════════
# the run
# ══════════════════════════════════════════════════════════════════════════
class RunContext:
    """Mutable state for one ``make`` run."""

    def __init__(self, root: Path, target: dict, output: Path | None,
                 repo_root: Path) -> None:
        self.root = Path(root)
        self.target = target
        self.database = target["database"]
        self.port = int(target["port"])
        self.user = target["user"]
        self.password = target.get("password", "") or ""
        self.config_path = self.root / "profile" / "config.yaml"
        self.base = self.root / "profile"
        self.logs = self.root / "logs"
        self.barriers = self.root / "barriers"
        self.output = Path(output) if output else None
        self.repo_root = Path(repo_root)
        self.registry = ChildRegistry()
        self.evidence: dict[str, Any] = {
            "harness": "eval/f2_source_idempotency_e2e.py",
            "matrix": {
                "W0": "crash BEFORE PG commit, fresh process retries -> 1",
                "W1": "PG commit BEFORE the accepted tombstone, fresh "
                      "process retries -> 1",
                "W2": "accepted tombstone persisted, pending outbox item "
                      "SURVIVES, fresh process converges -> 1, no pending",
                "W3": "real commit, then no ACK, fresh process retries -> 1",
                "W4": "REPEATED crash/recover >= 3 cycles, one identity -> 1",
            },
            "section6_conditions": [
                "canonical source rows = 1",
                "no duplicate QA caused by same event",
                "pending/accepted transport converges",
                "no foreign data root",
                "no production config/profile",
                "no production DB",
            ],
            "started_at": now_iso(),
            "steps": {},
        }
        self.children: list[dict] = []

    # -- evidence helpers -------------------------------------------------
    def step(self, name: str, record: dict) -> dict:
        self.evidence["steps"][name] = record
        return record

    def emit(self) -> None:
        if self.output is None:
            return
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(
            json.dumps(self.evidence, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def safe(self, value: Any) -> Any:
        return redact(self.password, value)

    # -- profile lifecycle ------------------------------------------------
    def prepare(self) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.barriers.mkdir(parents=True, exist_ok=True)
        write_profile(self.base, self.database, self.port, self.user)
        record = assert_profile_isolated(self.config_path, self.root,
                                         self.database, self.port, self.user)
        record.update(assert_no_credentials(self.config_path, self.password))
        self.step("isolated_profile", record)
        return record

    def env(self, extra: dict | None = None) -> dict:
        return child_env(self.config_path, self.base, self.password, extra)

    def serve_child(self, tag: str, barrier: dict | None = None,
                    tombstone: dict | None = None,
                    post_accepted: dict | None = None) -> OwnedChild:
        args = ["--port", "0", "--profile", "default"]
        if barrier:
            args += ["--barrier-host", barrier["host"],
                     "--barrier-session", barrier["session_id"],
                     "--barrier-event", barrier["event_id"],
                     "--barrier-marker", str(barrier["marker"]),
                     "--barrier-timeout", str(barrier.get("timeout", 900))]
            if barrier.get("release"):
                args += ["--barrier-release", str(barrier["release"])]
        if tombstone:
            args += ["--tombstone-host", tombstone["host"],
                     "--tombstone-session", tombstone["session_id"],
                     "--tombstone-event", tombstone["event_id"],
                     "--tombstone-marker", str(tombstone["marker"]),
                     "--tombstone-timeout", str(tombstone.get("timeout", 900))]
        if post_accepted:
            args += ["--post-accepted-host", post_accepted["host"],
                     "--post-accepted-session", post_accepted["session_id"],
                     "--post-accepted-event", post_accepted["event_id"],
                     "--post-accepted-marker", str(post_accepted["marker"]),
                     "--post-accepted-timeout",
                     str(post_accepted.get("timeout", 900))]
        child = spawn_child("child-serve", args, self.env(), self.repo_root / "src" / "v3-core",
                            self.logs / f"serve_{tag}.stderr.log", self.registry)
        prov = child.wait_event("provenance", timeout=180)
        # A child that refused to boot exits 97 right after its self-report.
        # Report that as the isolation failure it is, rather than waiting for
        # a ready event that can never arrive and calling it a timeout.
        if prov.get("refused"):
            child.stop()
            raise IsolationError(
                f"serve child {tag} refused to boot under {F2_TEST_MODE_ENV}: "
                f"{prov.get('reason')}. Refusing to continue — the run would "
                f"otherwise have no isolated child at all.")
        ready = child.wait_event("ready", timeout=300)
        prov = assert_child_provenance(prov, self.root, self.database,
                                       self.port, self.user)
        self.children.append({**child.record(), "tag": tag, "provenance": prov,
                              "port": int(ready.get("port") or 0)})
        self.step(f"serve_ready_{tag}", {"tag": tag, "port": ready.get("port"),
                                         "pid": ready.get("pid"),
                                         "provenance": prov})
        return child

    def client_child(self, tag: str, mode: str, port: int, payload: dict,
                     start_barrier: Path | None = None,
                     http_timeout: float = 120.0) -> OwnedChild:
        args = ["--mode", mode, "--port", str(int(port)),
                "--payload", json.dumps(payload, ensure_ascii=False),
                "--http-timeout", str(http_timeout)]
        if start_barrier is not None:
            args += ["--start-barrier", str(start_barrier),
                     "--start-timeout", "600"]
        child = spawn_child("child-client", args, self.env(),
                            self.repo_root / "src" / "v3-core",
                            self.logs / f"client_{tag}.stderr.log", self.registry)
        result = child.wait_event("client_result", timeout=900)
        child.meta["client_result"] = result
        child.stop()
        self.children.append({**child.record(), "tag": f"client_{tag}",
                              "client_result": result})
        self.step(f"client_{tag}", {"tag": tag, "mode": mode, "result": result,
                                    "pid": child.identity["pid"]})
        return child

    def record_children(self) -> None:
        self.evidence["children"] = self.children

    def assert_agreement(self) -> dict:
        serves = [c for c in self.children if c["role"] == "child-serve"]
        record = assert_all_children_agree(serves, self.root, self.database,
                                           self.port, self.user)
        self.step("child_identity_agreement", record)
        return record

    def dispose(self) -> dict:
        """I8: every owned child proven dead, or the run aborts.

        ``stop_all`` is called exactly once here — the teardown used to call it
        a second time in a bare ``try/except: pass`` that could hide exactly
        the fault I8 exists to catch. When it raises, the partial per-child
        records are still recorded so the rejection names the real pids.
        """
        records: list[dict] = []
        errors: list[str] = []
        try:
            records = self.registry.stop_all()
        except ChildCleanupError as exc:
            records = list(exc.records)
            errors = list(exc.errors)
            leftovers = self.registry.alive()
            self.step("process_cleanup",
                      {"per_child": records, "errors": errors,
                       "leftovers": leftovers, "alive_after": len(leftovers),
                       "clean": False, "teardown_error": True,
                       "verdict": "FAIL"})
            raise
        leftover = self.registry.assert_no_leftovers()
        record = {"per_child": records, "errors": errors, **leftover}
        self.step("process_cleanup", record)
        return record

    def dispose_configs(self) -> dict:
        """I7: the config outlives every child and is deleted last.

        The alive check is a hard precondition, not an advisory one: a
        deleted config is what makes ``_find_config`` fall through to
        production, so this refuses to run at all while a child lives.
        """
        alive = self.registry.alive()
        if alive:
            raise LeftoverProcessError(
                f"refusing to delete the isolated config while {len(alive)} "
                f"child(ren) is/are still alive: {alive}")
        removed: list[str] = []
        for path in sorted(self.root.rglob("config.yaml")):
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                pass
        remaining = [str(p) for p in self.root.rglob("config.yaml")]
        record = {"removed": removed, "config_yaml_remaining": remaining,
                  "deleted_after_all_children_dead": True,
                  "verdict": "PASS" if not remaining else "FAIL"}
        self.step("config_lifecycle", record)
        return record


# ── the W cases ───────────────────────────────────────────────────────────
def run_w0(ctx: RunContext) -> dict:
    """Caller gets no ACK; process dies BEFORE the commit; restart; retry."""
    host, session, event = "f2-w0-host", "f2-w0-session", "w0-event"
    payload = {"host": host, "session_id": session, "event_id": event,
               "role": "user", "content": "F2-W0 caller never gets an ACK",
               "turn_id": "1"}
    marker = ctx.barriers / "w0.reached"
    release = ctx.barriers / "w0.release"
    barrier = {"host": host, "session_id": session, "event_id": event,
               "marker": marker, "release": release, "timeout": 900}

    victim = ctx.serve_child("w0_a", barrier=barrier)
    port = ctx.children[-1]["port"]
    # The request goes through a proxy that destroys the ACK. The serving
    # process is untouched by the fault — only the client's socket is reset.
    proxy = DirectionProxy(port, blackhole="server->client").start()
    client = spawn_child("child-client",
                         ["--mode", "no-ack", "--port", str(proxy.port),
                          "--payload", json.dumps(payload, ensure_ascii=False),
                          "--http-timeout", "900"],
                         ctx.env(), ctx.repo_root / "src" / "v3-core",
                         ctx.logs / "client_w0.stderr.log", ctx.registry)

    deadline = time.time() + 600
    while time.time() < deadline and not marker.exists():
        time.sleep(0.1)
    if not marker.exists():
        proxy.stop()
        raise F2HarnessError(
            "W0: the child's pre-commit barrier was never reached — the case "
            "cannot be reported either way")
    # The flush is parked immediately BEFORE the insert, so the row count here
    # is the commit state at the barrier. W0 requires zero.
    at_barrier = identity_readback(ctx.target, host, session, event)
    phase = classify_barrier_phase(at_barrier["row_count"])

    killed = victim.stop(force=True)  # hard kill of the verified tree
    proxy.reset_client(timeout=30)
    proxy.stop()
    no_ack = client.wait_event("client_result", timeout=300)
    client.stop()
    ctx.step("w0_barrier", {"phase": phase, "readback_at_barrier": at_barrier,
                            "barrier_marker": str(marker),
                            "interpretation":
                                "W0 is only valid if the barrier observation is "
                                "before_commit (zero rows)"})

    # Restart on the same profile and retry the identical identity.
    revived = ctx.serve_child("w0_b")
    port2 = ctx.children[-1]["port"]
    status, ack = post_event(port2, payload)
    rows = wait_identity_count(ctx.target, host, session, event, 1)
    final = identity_readback(ctx.target, host, session, event)
    revived.stop()

    problems: list[str] = []
    if phase != "before_commit":
        problems.append(f"barrier phase was {phase}, want before_commit")
    if at_barrier["row_count"] != 0:
        problems.append(f"{at_barrier['row_count']} rows at the barrier, want 0")
    if int(no_ack.get("bytes_received", -1)) != 0:
        problems.append(
            f"caller received {no_ack.get('bytes_received')} bytes; W0 requires "
            f"no ACK at all")
    if not killed.get("clean"):
        problems.append(f"crash left residual pids {killed.get('residual_after')}")
    if status != 200 or not (ack.get("ok") and (ack.get("accepted") or ack.get("duplicate"))):
        problems.append(f"post-restart retry did not accept: http={status} ack={ack}")
    if final["row_count"] != 1:
        problems.append(f"final row count {final['row_count']}, want exactly 1")
    final_markers = marker_state(ctx.root, host, session, event)
    qa_rows = qa_rows_for_session(ctx.target, session)
    section6 = evaluate_case_section6(
        "W0", identity_rows=final["row_count"], duplicate_qa_rows=qa_rows,
        marker=final_markers, root=ctx.root)
    problems.extend(section6["section6_problems"])

    return {"verdict": "PASS" if not problems else "FAIL",
            "problems": problems,
            "barrier_phase": phase, "rows_at_barrier": at_barrier["row_count"],
            "caller_bytes_received": no_ack.get("bytes_received"),
            "caller_got_http_ack": no_ack.get("got_http_ack"),
            "caller_result": no_ack,
            "proxy": proxy.record(),
            "crash": killed,
            "retry": {"http": status, "ack": ack},
            "final_readback": final, "identity_rows": final["row_count"],
            "final_markers": final_markers, "qa_rows": qa_rows,
            "section6": section6}


def run_w1(ctx: RunContext) -> dict:
    """§6 W1 — the PG commit lands BEFORE the accepted tombstone.

    The window is the one between the two writes in ``_flush``:

        PG commit  →  accepted tombstone  →  pending marker removed

    ``TombstoneBarrier`` parks the middle step, so at the barrier the row is
    durably committed and NO identity tombstone exists yet. The serving
    process is then hard-killed in exactly that state, and a FRESH process
    is started to see whether recovery converges to one row with a
    tombstone and no surviving pending marker.

    A retry from the SAME process would prove nothing (B01 keeps two dedupe
    layers above PG), so the restart is mandatory, not decorative.
    """
    host, session, event = "f2-w1-host", "f2-w1-session", "w1-event"
    payload = {"host": host, "session_id": session, "event_id": event,
               "role": "user", "content": "F2-W1 commit before accepted tombstone",
               "turn_id": "1"}
    marker = ctx.barriers / "w1.tombstone_reached"
    tombstone = {"host": host, "session_id": session, "event_id": event,
                 "marker": marker, "timeout": 900}

    victim = ctx.serve_child("w1_a", tombstone=tombstone)
    port = ctx.children[-1]["port"]
    # The caller's ACK is destroyed, so the process learns nothing from the
    # response and the crash is the only thing it can react to.
    proxy = DirectionProxy(port, blackhole="server->client").start()
    client = spawn_child("child-client",
                         ["--mode", "no-ack", "--port", str(proxy.port),
                          "--payload", json.dumps(payload, ensure_ascii=False),
                          "--http-timeout", "900"],
                         ctx.env(), ctx.repo_root / "src" / "v3-core",
                         ctx.logs / "client_w1.stderr.log", ctx.registry)

    deadline = time.time() + 600
    while time.time() < deadline and not marker.exists():
        time.sleep(0.1)
    if not marker.exists():
        proxy.stop()
        raise F2HarnessError(
            "W1: the accepted-tombstone barrier was never reached — the case "
            "cannot be reported either way")

    # The observation that makes this W1 and not W0/W3: the row IS committed
    # while the tombstone is NOT yet written.
    at_barrier = identity_readback(ctx.target, host, session, event)
    markers_at_barrier = marker_state(ctx.root, host, session, event)
    phase = classify_marker_phase(markers_at_barrier["accepted_present"],
                                  at_barrier["row_count"])

    killed = victim.stop(force=True)
    proxy.reset_client(timeout=30)
    proxy.stop()
    no_ack = client.wait_event("client_result", timeout=300)
    client.stop()
    ctx.step("w1_barrier", {
        "phase": phase, "readback_at_barrier": at_barrier,
        "markers_at_barrier": markers_at_barrier,
        "interpretation": "W1 is only valid if the barrier observation is "
                          "pre_tombstone: one committed row and NO accepted "
                          "tombstone yet",
    })

    # Fresh process on the same profile. Startup recovery replays the
    # surviving pending marker, so convergence must come from recovery plus
    # the partial unique index, not from anything the dead process did.
    revived = ctx.serve_child("w1_b")
    port2 = ctx.children[-1]["port"]
    status, ack = post_event(port2, payload)
    wait_identity_count(ctx.target, host, session, event, 1)
    final = identity_readback(ctx.target, host, session, event)
    revived.stop()

    final_markers = marker_state(ctx.root, host, session, event)
    qa_rows = qa_rows_for_session(ctx.target, session)
    section6 = evaluate_case_section6(
        "W1", identity_rows=final["row_count"], duplicate_qa_rows=qa_rows,
        marker=final_markers, root=ctx.root)

    problems: list[str] = []
    if phase != "pre_tombstone":
        problems.append(f"barrier phase was {phase}, want pre_tombstone")
    if at_barrier["row_count"] != 1:
        problems.append(
            f"{at_barrier['row_count']} rows at the barrier, want 1 "
            f"(W1 is the window AFTER the PG commit)")
    if markers_at_barrier["accepted_present"]:
        problems.append(
            "an accepted tombstone already existed at the barrier, so the "
            "crash was not inside the W1 window")
    if int(no_ack.get("bytes_received", -1)) != 0:
        problems.append(f"caller received {no_ack.get('bytes_received')} bytes; "
                        f"W1 requires no ACK at all")
    if not killed.get("clean"):
        problems.append(f"crash left residual pids {killed.get('residual_after')}")
    if status != 200 or not (ack.get("ok") and (ack.get("accepted") or ack.get("duplicate"))):
        problems.append(f"post-restart retry did not accept: http={status} ack={ack}")
    if final["row_count"] != 1:
        problems.append(f"final row count {final['row_count']}, want exactly 1")
    problems.extend(section6["section6_problems"])

    return {"verdict": "PASS" if not problems else "FAIL", "problems": problems,
            "barrier_phase": phase, "rows_at_barrier": at_barrier["row_count"],
            "accepted_at_barrier": markers_at_barrier["accepted_present"],
            "pending_at_barrier": markers_at_barrier["pending_present"],
            "caller_bytes_received": no_ack.get("bytes_received"),
            "caller_result": no_ack, "crash": killed,
            "retry": {"http": status, "ack": ack},
            "final_readback": final, "identity_rows": final["row_count"],
            "final_markers": final_markers, "qa_rows": qa_rows,
            "section6": section6}


def run_w2(ctx: RunContext) -> dict:
    """§6 W2 — the accepted tombstone is persisted but a pending item SURVIVES.

    This is the torn-transport case: PG says accepted, the accepted identity
    tombstone is on disk, and yet the outbox still holds a pending marker for
    the same canonical identity. Left alone that is a silent duplicate risk —
    a later startup replay would re-drive an already-accepted event.

    The window is produced DETERMINISTICALLY, by parking the one step that
    separates the two writes. ``_ack_live_item`` persists the accepted
    tombstone and then unlinks the pending marker as the very next statement
    (``ingest.py:455-467``), so there is no instant a reader could catch.
    ``PostAcceptedBarrier`` calls the REAL ``_persist_live_accepted`` and then
    HOLDS the item right there, before the unlink, and the harness kills the
    tree while it is parked. Both markers are therefore the product's own
    writes — the harness authors neither — and the state is observed, not
    raced for.

    A fresh process then has to converge: PG=1, accepted present, pending
    ABSENT.
    """
    host, session, event = "f2-w2-host", "f2-w2-session", "w2-event"
    payload = {"host": host, "session_id": session, "event_id": event,
               "role": "user", "content": "F2-W2 accepted persisted, pending survives",
               "turn_id": "1"}
    marker = ctx.barriers / "w2.post_accepted_reached"
    post_accepted = {"host": host, "session_id": session, "event_id": event,
                     "marker": marker, "timeout": 900}

    serve = ctx.serve_child("w2_a", post_accepted=post_accepted)
    port = ctx.children[-1]["port"]
    proxy = DirectionProxy(port, blackhole="server->client").start()
    client = spawn_child("child-client",
                         ["--mode", "no-ack", "--port", str(proxy.port),
                          "--payload", json.dumps(payload, ensure_ascii=False),
                          "--http-timeout", "900"],
                         ctx.env(), ctx.repo_root / "src" / "v3-core",
                         ctx.logs / "client_w2.stderr.log", ctx.registry)

    deadline = time.time() + 600
    while time.time() < deadline and not marker.exists():
        time.sleep(0.1)
    if not marker.exists():
        proxy.stop()
        raise F2HarnessError(
            "W2: the post-accepted barrier was never reached — the case "
            "cannot be reported either way")

    # The observation that makes this W2 and not W1: the item is parked AFTER
    # the tombstone write and BEFORE the unlink, so all three of these hold
    # simultaneously — which is impossible to observe by racing a release.
    torn = marker_state(ctx.root, host, session, event)
    torn_row_count = identity_readback(ctx.target, host, session, event)["row_count"]
    torn_phase = classify_marker_phase(torn["accepted_present"], torn_row_count)

    # No release: the tree is killed while parked, so the pending unlink never
    # runs in this process and the surviving marker is the real torn state.
    killed = serve.stop(force=True)
    proxy.reset_client(timeout=30)
    proxy.stop()
    no_ack = client.wait_event("client_result", timeout=300)
    client.stop()

    # Re-read AFTER the kill, so the evidence is the on-disk state the fresh
    # process will actually start from, not a mid-flight observation.
    at_crash = marker_state(ctx.root, host, session, event)
    rows_at_crash = identity_readback(ctx.target, host, session, event)["row_count"]
    ctx.step("w2_torn_transport", {
        "phase": torn_phase, "torn_phase": torn_phase,
        "torn_markers": torn, "torn_row_count": torn_row_count,
        "markers_at_crash": at_crash, "rows_at_crash": rows_at_crash,
        "barrier_marker": str(marker),
        "parked_phase": "post_accepted_pending_unlink",
        "interpretation": "W2 requires the torn state to be OBSERVED: the "
                          "accepted tombstone IS durable while a pending "
                          "outbox item for the same identity SURVIVES. The "
                          "item is parked between the product's own two "
                          "writes, so this is deterministic, not a race.",
    })

    # A fresh process sees exactly that torn state on disk and must converge
    # without adding a row.
    revived = ctx.serve_child("w2_b")
    port2 = ctx.children[-1]["port"]
    status, ack = post_event(port2, payload)
    wait_identity_count(ctx.target, host, session, event, 1)
    final = identity_readback(ctx.target, host, session, event)
    revived.stop()

    final_markers = marker_state(ctx.root, host, session, event)
    qa_rows = qa_rows_for_session(ctx.target, session)
    section6 = evaluate_case_section6(
        "W2", identity_rows=final["row_count"], duplicate_qa_rows=qa_rows,
        marker=final_markers, root=ctx.root)

    problems: list[str] = []
    # The torn state must genuinely have been observed, otherwise the case
    # proved nothing about a surviving pending marker.
    if not at_crash["accepted_present"]:
        problems.append("the accepted tombstone never became durable, so the "
                        "§6 torn state was never reached")
    if not at_crash["pending_present"]:
        problems.append(
            "no pending outbox item survived alongside the accepted "
            "tombstone, so this run did not exercise the §6 W2 state")
    if torn_row_count != 1:
        problems.append(f"torn-state row count is {torn_row_count}, want 1")
    if rows_at_crash != 1:
        problems.append(f"row count at the crash is {rows_at_crash}, want 1")
    if int(no_ack.get("bytes_received", -1)) != 0:
        problems.append(f"caller received {no_ack.get('bytes_received')} bytes; "
                        f"the caller must be blind to the transport")
    if not killed.get("clean"):
        problems.append(f"crash left residual pids {killed.get('residual_after')}")
    if status != 200 or not (ack.get("ok") and (ack.get("accepted") or ack.get("duplicate"))):
        problems.append(f"post-restart retry did not accept: http={status} ack={ack}")
    if final["row_count"] != 1:
        problems.append(f"final row count {final['row_count']}, want exactly 1")
    # Convergence is the point of W2: the surviving pending item must be gone
    # after recovery, with the accepted tombstone intact. A recovered run that
    # still holds the pending marker has NOT converged, whatever the row count
    # says.
    if not final_markers["accepted_present"]:
        problems.append("the accepted tombstone did not survive recovery")
    if final_markers["pending_present"]:
        problems.append(
            f"a pending outbox item still survives after recovery: "
            f"{final_markers['pending_files']}")
    problems.extend(section6["section6_problems"])

    return {"verdict": "PASS" if not problems else "FAIL", "problems": problems,
            "torn_phase": torn_phase, "torn_markers": at_crash,
            "torn_row_count": rows_at_crash,
            "parked_phase": "post_accepted_pending_unlink",
            "accepted_persisted": at_crash["accepted_present"],
            "pending_survived": at_crash["pending_present"],
            "caller_bytes_received": no_ack.get("bytes_received"),
            "caller_result": no_ack, "crash": killed,
            "retry": {"http": status, "ack": ack},
            "final_readback": final, "identity_rows": final["row_count"],
            "final_markers": final_markers, "qa_rows": qa_rows,
            "section6": section6}


def run_w3(ctx: RunContext) -> dict:
    """Row really committed, THEN the caller is left without an ACK."""
    host, session, event = "f2-w3-host", "f2-w3-session", "w3-event"
    payload = {"host": host, "session_id": session, "event_id": event,
               "role": "user", "content": "F2-W3 commit succeeded, caller blind",
               "turn_id": "1"}
    serve = ctx.serve_child("w3_a")
    port = ctx.children[-1]["port"]
    proxy = DirectionProxy(port, blackhole="server->client").start()
    client = spawn_child("child-client",
                         ["--mode", "no-ack", "--port", str(proxy.port),
                          "--payload", json.dumps(payload, ensure_ascii=False),
                          "--http-timeout", "900"],
                         ctx.env(), ctx.repo_root / "src" / "v3-core",
                         ctx.logs / "client_w3.stderr.log", ctx.registry)

    # No barrier here: wait for the real commit, with the ACK still trapped.
    rows = wait_identity_count(ctx.target, host, session, event, 1)
    committed = identity_readback(ctx.target, host, session, event)
    phase = classify_barrier_phase(committed["row_count"])
    killed = serve.stop(force=True)
    proxy.reset_client(timeout=30)
    proxy.stop()
    no_ack = client.wait_event("client_result", timeout=300)
    client.stop()
    ctx.step("w3_commit", {"phase": phase, "readback_at_crash": committed,
                           "interpretation":
                               "W3 is only valid if the crash-side read-back is "
                               "after_commit (exactly one row)"})

    revived = ctx.serve_child("w3_b")
    port2 = ctx.children[-1]["port"]
    status, ack = post_event(port2, payload)
    wait_identity_count(ctx.target, host, session, event, 1)
    final = identity_readback(ctx.target, host, session, event)
    revived.stop()

    problems: list[str] = []
    if phase != "after_commit":
        problems.append(f"crash-side phase was {phase}, want after_commit")
    if committed["row_count"] != 1:
        problems.append(f"{committed['row_count']} rows before the crash, want 1")
    if int(no_ack.get("bytes_received", -1)) != 0:
        problems.append(f"caller received {no_ack.get('bytes_received')} bytes; "
                        f"the caller must be blind to the commit")
    if not killed.get("clean"):
        problems.append(f"crash left residual pids {killed.get('residual_after')}")
    # The retry must be ACCEPTED and must leave exactly one durable row — that
    # is the source-of-truth outcome. Whether the ACK carries
    # ``duplicate=True`` is a RACE, not a correctness property: the commit is
    # observed, then the process is killed, and if the durable marker lands
    # after that kill the retry legitimately reports "accepted". PG is the
    # authority on the identity; the caller's boolean is recorded, not demanded.
    if status != 200 or not (ack.get("ok") and (ack.get("accepted") or ack.get("duplicate"))):
        problems.append(f"post-restart retry was not accepted: {status} {ack}")
    if final["row_count"] != 1:
        problems.append(f"final row count {final['row_count']}, want exactly 1")
    final_markers = marker_state(ctx.root, host, session, event)
    qa_rows = qa_rows_for_session(ctx.target, session)
    section6 = evaluate_case_section6(
        "W3", identity_rows=final["row_count"], duplicate_qa_rows=qa_rows,
        marker=final_markers, root=ctx.root)
    problems.extend(section6["section6_problems"])
    return {"verdict": "PASS" if not problems else "FAIL", "problems": problems,
            "phase": phase, "rows_before_crash": committed["row_count"],
            "caller_bytes_received": no_ack.get("bytes_received"),
            "caller_result": no_ack, "proxy": proxy.record(), "crash": killed,
            "retry": {"http": status, "ack": ack},
            "retry_accepted": bool(ack.get("accepted") or ack.get("duplicate")),
            "retry_reported_duplicate": bool(ack.get("duplicate")),
            "duplicate_note": "duplicate=True races the crash; PG is the "
                              "authority on the identity, so the flag is "
                              "recorded rather than demanded",
            "final_readback": final, "identity_rows": final["row_count"],
            "final_markers": final_markers, "qa_rows": qa_rows,
            "section6": section6}


def run_w4(ctx: RunContext) -> dict:
    """§6 W4 — REPEATED crash/recover, at least three cycles, one identity.

    One crash proves a single window. §6 asks for the fault to be survived
    repeatedly, because each cycle re-enters the same window from a
    different starting marker state. Every cycle:

      * crash the serving tree (ACK destroyed, so the caller is blind);
      * prove the crash was clean — a residual pid is a leaked writer, not
        a survived fault;
      * start a FRESH process and retry the identical identity;
      * re-read PG and the durable markers.

    Three cycles is the §6 floor, and the identity is the same one
    throughout: the final state must be exactly one row, one accepted
    tombstone, no surviving pending item, and no duplicate QA.
    """
    host, session, event = "f2-w4-host", "f2-w4-session", "w4-event"
    payload = {"host": host, "session_id": session, "event_id": event,
               "role": "user", "content": "F2-W4 repeated crash/recover",
               "turn_id": "1"}
    cycles_required = 3
    cycles: list[dict] = []

    for index in range(cycles_required):
        cycle: dict = {"index": index}
        try:
            serve = ctx.serve_child(f"w4_{index}")
        except (IsolationError, F2HarnessError) as exc:
            # A refusal or a boot failure on cycle N+1 means the previous
            # crash did not release its resources. Record it against this
            # cycle rather than letting it look like a later pass.
            cycle.update({"outcome": "restart-refused",
                          "error": f"{type(exc).__name__}: {exc}"[:300]})
            cycles.append(cycle)
            break
        port = ctx.children[-1]["port"]

        # Alternate which side of the commit the crash lands on, so the
        # repeats cover both windows W1 and W3 rather than re-testing one.
        if index % 2 == 0:
            victim = serve
            proxy = DirectionProxy(port, blackhole="server->client").start()
            mode = "no-ack"
        else:
            # No proxy: the request is delivered and committed, then the tree
            # is killed immediately, so the caller learns nothing from the
            # response and the window is after the commit.
            victim = serve
            proxy = None
            mode = "deliver"

        if proxy is not None:
            client = spawn_child(
                "child-client",
                ["--mode", mode, "--port", str(proxy.port),
                 "--payload", json.dumps(payload, ensure_ascii=False),
                 "--http-timeout", "900"],
                ctx.env(), ctx.repo_root / "src" / "v3-core",
                ctx.logs / f"client_w4_{index}.stderr.log", ctx.registry)
        else:
            client = ctx.client_child(f"w4_{index}", mode, port, payload,
                                      http_timeout=900)

        # Let the writer drain to PG, then crash the tree.
        rows_before = wait_identity_count(ctx.target, host, session, event, 1,
                                          timeout=180)
        markers_before = marker_state(ctx.root, host, session, event)
        killed = victim.stop(force=True)
        if proxy is not None:
            proxy.reset_client(timeout=30)
            proxy.stop()
        if proxy is not None:
            no_ack = client.wait_event("client_result", timeout=300)
            client.stop()
            cycle["caller_bytes_received"] = no_ack.get("bytes_received")
        else:
            no_ack = client.meta.get("client_result") or {}
            cycle["caller_bytes_received"] = no_ack.get("bytes_received")

        rows_after_crash = identity_readback(ctx.target, host, session, event)
        markers_after_crash = marker_state(ctx.root, host, session, event)
        cycle.update({
            "rows_before_crash": rows_before,
            "markers_before_crash": markers_before,
            "crash": killed,
            "crash_clean": bool(killed.get("clean")),
            "residual_after": killed.get("residual_after"),
            "rows_after_crash": rows_after_crash["row_count"],
            "markers_after_crash": markers_after_crash,
        })
        ctx.step(f"w4_cycle_{index}", dict(cycle, interpretation=(
            "each cycle must crash clean and leave exactly one committed "
            "row for the identity")))

        # Fresh process for the next cycle. Startup recovery re-drives
        # whatever survived on disk, so the next cycle's crash window is
        # entered from a genuinely recovered state rather than a reset one.
        revived = ctx.serve_child(f"w4_{index}_recovered")
        port2 = ctx.children[-1]["port"]
        status, ack = post_event(port2, payload)
        wait_identity_count(ctx.target, host, session, event, 1)
        recovered_rows = identity_readback(ctx.target, host, session, event)
        revived.stop()
        # A recovery that did not actually ACK the identity is a failed
        # recovery, even when the row count happens to be right: 200 without
        # ok, ok without accepted/duplicate, and any non-200 status all mean
        # the fresh process never told the caller the event was durable. It is
        # recorded per cycle AND rejected below, so a cycle can never be
        # counted as completed on the strength of its row count alone.
        recovery_ok = bool(status == 200
                           and ack.get("ok")
                           and (ack.get("accepted") or ack.get("duplicate")))
        cycle.update({"recovery_http": status, "recovery_ack": ack,
                      "recovery_ok": recovery_ok,
                      "rows_after_recovery": recovered_rows["row_count"]})
        cycles.append(cycle)

        if recovered_rows["row_count"] != 1:
            # Stop early: a second row is the defect this case exists to
            # catch, and continuing would only add noise.
            break

    completed = [c for c in cycles if c.get("outcome") != "restart-refused"]
    final = identity_readback(ctx.target, host, session, event)
    final_markers = marker_state(ctx.root, host, session, event)
    qa_rows = qa_rows_for_session(ctx.target, session)
    section6 = evaluate_case_section6(
        "W4", identity_rows=final["row_count"], duplicate_qa_rows=qa_rows,
        marker=final_markers, root=ctx.root)

    problems: list[str] = []
    if len(cycles) < cycles_required:
        problems.append(
            f"only {len(cycles)} of {cycles_required} required crash/recover "
            f"cycles ran; §6 W4 needs repeated crash/recover >= 3")
    for c in cycles:
        if c.get("outcome") == "restart-refused":
            problems.append(
                f"cycle {c.get('index')} could not restart: {c.get('error')}")
            continue
        if not c.get("crash_clean"):
            problems.append(
                f"cycle {c.get('index')} crash left residual pids "
                f"{c.get('residual_after')}")
        if c.get("rows_after_crash") != 1:
            problems.append(
                f"cycle {c.get('index')} left {c.get('rows_after_crash')} rows "
                f"after the crash, want exactly 1")
        if c.get("rows_after_recovery") != 1:
            problems.append(
                f"cycle {c.get('index')} left "
                f"{c.get('rows_after_recovery')} rows after recovery, want 1")
        # A row count of 1 is not proof that the recovery ACKed the identity.
        # A non-200 status, ok=False, or an ACK that is neither accepted nor
        # duplicate means the fresh process never confirmed the event was
        # durable, so the cycle is rejected.
        if not c.get("recovery_ok"):
            problems.append(
                f"cycle {c.get('index')} recovery was not accepted: "
                f"http={c.get('recovery_http')} ack={c.get('recovery_ack')}")
    if final["row_count"] != 1:
        problems.append(f"final row count {final['row_count']}, want exactly 1")
    problems.extend(section6["section6_problems"])

    return {"verdict": "PASS" if not problems else "FAIL", "problems": problems,
            "cycles_required": cycles_required,
            "cycles_completed": len(completed),
            "cycles": cycles,
            "final_readback": final, "identity_rows": final["row_count"],
            "final_markers": final_markers, "qa_rows": qa_rows,
            "section6": section6}


W_CASES: dict[str, Callable[[RunContext], dict]] = {
    "W0": run_w0, "W1": run_w1, "W2": run_w2, "W3": run_w3, "W4": run_w4,
}


# ══════════════════════════════════════════════════════════════════════════
# ACCEPTANCE SEAL — the ONE place a run's verdict and exit code are decided
# ══════════════════════════════════════════════════════════════════════════
# The exit code used to be computed from the W summary alone, which meant a run
# could exit 0 while ``run_error`` was recorded, a guard had failed, a child
# disagreed, the provider scan had found a live call, the production sentinel
# had moved, or the teardown had thrown. The seal below is fail-closed: it is
# a PURE function of the evidence file, so the rule is unit-tested instead of
# being asserted in prose, and it is the only thing that may set the rc.

#: The exact full matrix. A run that does not cover every one of these can
#: never be reported as full acceptance, whatever else it proved.
FULL_MATRIX: tuple[str, ...] = ("W0", "W1", "W2", "W3", "W4")

#: Phases whose ABSENCE is itself a failure. "Not recorded" is not "fine" —
#: it is unproven, and unproven is not a pass. The provider-marker total is
#: deliberately NOT here: see :data:`RECORD_ONLY_GUARDS`.
MANDATORY_GUARD_PHASES: tuple[str, ...] = (
    "isolated_profile",          # I2/I3 the generated profile is isolated
    "database_recreate",         # no stale row can back a PASS
    "fresh_bootstrap",           # the SHIPPED DDL was applied
    "schema_readback",           # the identity index really exists
    "child_identity_agreement",  # I5 one root, one database, one identity
    "process_cleanup",           # I8 every owned child is dead, residual zero
    "production_sentinel_before",
    "production_sentinel_after",  # I9 the production profile did not move
    "config_lifecycle",          # I7 deleted, and only after every child died
)

#: Phases that are MEASURED AND RECORDED but do not gate acceptance. Mission
#: §6: the provider-marker total is telemetry, not an F2 blocker — a repeated
#: event legitimately produces one more ``embed_for_write`` line, and a
#: synthetic/test run may trip a marker without any real external call. What
#: actually forbids a real provider call is STRUCTURAL and IS gated: an empty
#: provider config, no credential on disk, every provider env var blanked, and
#: every child proven to have resolved that isolated config.
RECORD_ONLY_GUARDS: tuple[str, ...] = ("provider_calls",)

# Exit codes are kept DISTINCT so a caller can tell "the run was rejected"
# from "the run proved part of the matrix": both are nonzero, but only one of
# them is a rejection.
EXIT_ACCEPTED = 0
EXIT_REJECTED = 1
EXIT_NO_EVIDENCE = 2
EXIT_PARTIAL = 3

VERDICT_ACCEPTED = "PASS"
VERDICT_PARTIAL = "PARTIAL"
VERDICT_REJECTED = "FAIL"


def _phase_ok(record: Any) -> bool:
    """A guard phase passes only if it recorded a PASS-flavoured verdict."""
    return (isinstance(record, dict)
            and str(record.get("verdict") or "").startswith("PASS"))


def evaluate_acceptance(evidence: dict | None,
                        selected: Sequence[str] | None = None) -> dict:
    """Turn an evidence file into exactly ONE verdict and ONE exit code.

    Pure: no I/O, no clock, no process table. The acceptance rule for a
    destructive, production-adjacent harness must be checkable without the one
    machine that is allowed to run it.

    ``PASS`` (== F2 DONE, exit 0) requires ALL of:

    1. every case of the full matrix ran and reported ``PASS``;
    2. every mandatory guard phase is present AND PASS;
    3. no ``run_error`` step was recorded;
    4. the children agreed on one isolated identity and none is residual;
    5. the production sentinel is unchanged;
    6. the generated config was removed, and only after every child was dead;
    7. no teardown error was reported.

    The provider-marker total is RECORDED, not gated (mission §6): a repeated
    event legitimately produces one more ``embed_for_write`` line, so a
    non-zero count is telemetry. What gates a real provider call is the
    STRUCTURAL isolation, which is enforced above: an empty provider config,
    no credential on disk, every provider env var blanked, and every child
    proven to have resolved that isolated config.

    Anything else is ``FAIL`` with a nonzero exit code and the offending
    condition named. A run restricted with ``--only`` can never satisfy (1):
    it is labelled ``PARTIAL`` — its own verdict, its own exit code — and is
    never F2 DONE, so a subset of the matrix can never be quoted as full
    acceptance.
    """
    evidence = evidence or {}
    steps: dict = dict(evidence.get("steps") or {})
    summary: dict = dict(evidence.get("summary") or {})
    if selected is None:
        chosen = list(FULL_MATRIX)
    else:
        chosen = [str(n).strip().upper() for n in selected]
    chosen_set = {n for n in chosen if n}
    full_matrix = chosen_set == set(FULL_MATRIX)

    failures: list[str] = []
    withheld: list[str] = []

    # ── 1. the matrix ──────────────────────────────────────────────────
    case_report: dict[str, dict] = {}
    for name in FULL_MATRIX:
        record = steps.get(name)
        if not isinstance(record, dict):
            if name in chosen_set:
                failures.append(
                    f"matrix case {name}: no result recorded (missing proof)")
                withheld.append(f"matrix case {name}: missing")
            else:
                withheld.append(f"matrix case {name}: not run (--only "
                                f"{sorted(chosen_set)})")
            case_report[name] = {"selected": name in chosen_set,
                                 "present": False, "verdict": None, "ok": False}
            continue
        verdict = record.get("verdict")
        ok = verdict == "PASS"
        case_report[name] = {
            "selected": name in chosen_set, "present": True, "verdict": verdict,
            "ok": ok,
            "rows": record.get("identity_rows", record.get("session_total_rows")),
            "problems": record.get("problems"),
        }
        if not ok:
            failures.append(
                f"matrix case {name}: verdict={verdict!r} "
                f"problems={record.get('problems')}")

    # ── 1b. mission §6 per-case end-state conditions ─────────────────────
    # §6 states six conditions for the run as a whole. Each case now records
    # its own verdict for them (``evaluate_case_section6``), and a case whose
    # §6 checks failed is a REJECTION even when its own headline count is
    # right. Without this the §6 conditions would be prose in a docstring
    # rather than part of the acceptance rule.
    for name in FULL_MATRIX:
        record = steps.get(name)
        if not isinstance(record, dict):
            continue
        section6 = record.get("section6")
        if not isinstance(section6, dict):
            failures.append(
                f"matrix case {name}: mission §6 end-state conditions were not "
                f"evaluated (missing 'section6')")
            continue
        if not section6.get("section6_ok"):
            failures.append(
                f"matrix case {name}: mission §6 conditions failed: "
                f"{section6.get('section6_problems')} "
                f"checks={section6.get('checks')}")
        case_report.setdefault(name, {})["section6"] = section6.get("checks")

    if not full_matrix:
        withheld.append(
            f"the full matrix was not run: selected {sorted(chosen_set)} != "
            f"{list(FULL_MATRIX)} — this run can never be reported as F2 DONE")

    # ── 2. the mandatory guard phases (provider markers are NOT here) ──
    guards: dict[str, dict] = {}
    for phase in MANDATORY_GUARD_PHASES:
        record = steps.get(phase)
        present = isinstance(record, dict)
        verdict = record.get("verdict") if present else None
        ok = _phase_ok(record)
        guards[phase] = {"present": present, "verdict": verdict, "ok": ok}
        if not present:
            failures.append(
                f"guard {phase!r}: no proof recorded (mandatory phase missing)")
        elif not ok:
            failures.append(f"guard {phase!r}: verdict={verdict!r} "
                            f"error={record.get('error')}")

    # ── 3. an aborted run is never a pass ──────────────────────────────
    if "run_error" in steps:
        err = steps.get("run_error") or {}
        failures.append(
            f"run_error: {err.get('error_type', 'Error')}: {err.get('error')}")

    # ── 4. child identity agreement + residual zero ───────────────────
    agreement = steps.get("child_identity_agreement")
    if isinstance(agreement, dict) and agreement.get("agreed") is False:
        failures.append("children did not agree on one isolated identity")
    cleanup = steps.get("process_cleanup")
    if isinstance(cleanup, dict):
        if cleanup.get("clean") is False:
            failures.append("process cleanup reported clean=False")
        if cleanup.get("alive_after"):
            failures.append(
                f"{cleanup.get('alive_after')} owned children still alive")
        residual = [c.get("pid") for c in (cleanup.get("per_child") or [])
                    if isinstance(c, dict) and not c.get("clean")]
        if residual:
            failures.append(f"residual owned pids after cleanup: {residual}")

    # ── 5. provider markers are RECORD-ONLY (mission §6) ──────────────
    # A repeated event legitimately produces one more embed_for_write line, and
    # a synthetic run can trip a marker without any real external call, so the
    # total is telemetry. The gating proof that no real provider call happened
    # is structural and lives in the guarded phases above: empty provider
    # config, no credential on disk, provider env blanked, child resolved the
    # isolated config.
    provider = steps.get("provider_calls")
    if isinstance(provider, dict):
        guards["provider_calls"] = {
            "present": True,
            "verdict": provider.get("verdict"),
            "ok": None,                       # recorded, not gated
            "record_only": True,
            "provider_call_count": provider.get("provider_call_count"),
        }

    # ── 6. the production sentinel must not have moved ─────────────────
    sentinel = steps.get("production_sentinel_after")
    if isinstance(sentinel, dict) and sentinel.get("unchanged") is False:
        failures.append("the production profile changed during the run (I9)")

    # ── 7. config removed, and only after every child was dead ────────
    lifecycle = steps.get("config_lifecycle")
    if isinstance(lifecycle, dict):
        if lifecycle.get("config_yaml_remaining"):
            failures.append(
                f"config left on disk: {lifecycle.get('config_yaml_remaining')}")
        if not lifecycle.get("deleted_after_all_children_dead"):
            failures.append("the config was not proven removed after all "
                            "children died (I7)")

    # ── 8. any teardown fault at all rejects the run ──────────────────
    teardown = steps.get("teardown")
    if isinstance(teardown, dict) and teardown.get("errors"):
        failures.append(f"teardown reported errors: {teardown['errors']}")

    if not evidence or not steps:
        return {"verdict": VERDICT_REJECTED, "label": "F2 NO EVIDENCE",
                "exit_code": EXIT_NO_EVIDENCE, "f2_done": False,
                "reason": "the run produced no evidence at all",
                "full_matrix": full_matrix, "selected": sorted(chosen_set),
                "matrix": {"full_matrix": full_matrix,
                           "selected": sorted(chosen_set), "cases": case_report,
                           "case_verdicts": summary},
                "guards": guards, "failures": failures, "withheld": withheld,
                "mandatory_guard_phases": list(MANDATORY_GUARD_PHASES)}

    if failures:
        verdict, exit_code = VERDICT_REJECTED, EXIT_REJECTED
        label, done = "F2 REJECTED", False
        reason = f"{len(failures)} acceptance condition(s) failed"
    elif not full_matrix:
        verdict, exit_code = VERDICT_PARTIAL, EXIT_PARTIAL
        label, done = "F2 PARTIAL (NOT F2 DONE)", False
        reason = "every selected case and every guard passed, but the run " \
                 "covered only a subset of the full matrix"
    else:
        verdict, exit_code = VERDICT_ACCEPTED, EXIT_ACCEPTED
        label, done = "F2 ACCEPTED", True
        reason = "full matrix PASS with every mandatory guard proven"

    return {"verdict": verdict, "label": label, "exit_code": exit_code,
            "f2_done": done, "reason": reason,
            "full_matrix": full_matrix, "selected": sorted(chosen_set),
            "matrix": {"full_matrix": full_matrix, "selected": sorted(chosen_set),
                       "cases": case_report, "case_verdicts": summary},
            "guards": guards, "failures": failures, "withheld": withheld,
            "mandatory_guard_phases": list(MANDATORY_GUARD_PHASES)}


def finalize_teardown(ctx: "RunContext",
                      before_sentinel: dict | None) -> dict:
    """Stop every child, prove every guard, dispose the config LAST.

    Teardown is the phase that used to be able to hide a failure, so it is
    written fail-closed:

    * an exception from ``stop_all`` / leftover detection is RECORDED as a
      failed ``process_cleanup`` — never swallowed into a pass;
    * if the teardown reported anything at all, the generated config is
      PRESERVED on disk (and recorded as such) so the failure can be
      investigated and so nothing deleted a config while a child might still
      be alive and rebinding to production;
    * the provider scan and the sentinel comparison are both guarded, so a
      failure to MEASURE them is a failure, not a silent pass.
    """
    report: dict[str, Any] = {"errors": [], "config_preserved": False}
    cleanup_ok = True

    def _fail(step: str, exc: BaseException) -> None:
        report["errors"].append(f"{step}: {type(exc).__name__}: {exc}")
        ctx.step(step, {"error": ctx.safe(f"{type(exc).__name__}: {exc}"),
                        "clean": False, "teardown_error": True,
                        "verdict": "FAIL"})
        log(f"[teardown] FAIL {step}: {type(exc).__name__}: {exc}")

    # I8 — every owned child proven dead, or the run is rejected.
    try:
        ctx.dispose()
    except Exception as exc:  # noqa: BLE001 — any teardown error is a failure
        cleanup_ok = False
        leftover: list = []
        try:
            leftover = ctx.registry.alive()
        except Exception as exc2:  # noqa: BLE001
            report["errors"].append(f"alive: {type(exc2).__name__}: {exc2}")
        # ``dispose`` already recorded the failed process_cleanup step with
        # the per-child records; only overwrite it when it did not (e.g. the
        # failure came from assert_no_leftovers rather than stop_all).
        recorded = ctx.evidence["steps"].get("process_cleanup")
        if not (isinstance(recorded, dict) and recorded.get("teardown_error")):
            _fail("process_cleanup", exc)
        else:
            report["errors"].append(
                f"process_cleanup: {type(exc).__name__}: {exc}")
            log(f"[teardown] FAIL process_cleanup: "
                f"{type(exc).__name__}: {exc}")
        step = ctx.evidence["steps"]["process_cleanup"]
        step["leftovers"] = leftover
        step["alive_after"] = len(leftover)
        report["stop_all_error"] = ctx.safe(f"{type(exc).__name__}: {exc}")

    try:
        ctx.record_children()
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"record_children: {type(exc).__name__}: {exc}")

    # I4 — the provider scan must be measured; an unreadable log set is not
    # a zero count.
    try:
        log_files = sorted(ctx.logs.glob("*.log"))
    except Exception:  # noqa: BLE001
        log_files = []
    try:
        prov = scan_provider_calls(log_files)
    except Exception as exc:  # noqa: BLE001
        prov = {"provider_call_count": -1, "files_scanned": [],
                "error": ctx.safe(f"{type(exc).__name__}: {exc}"),
                "verdict": "FAIL"}
        report["errors"].append(f"provider_calls: {type(exc).__name__}: {exc}")
    ctx.step("provider_calls", prov)
    if int(prov.get("provider_call_count") or 0) != 0:
        log(f"[provider] FAIL count={prov.get('provider_call_count')}")

    # I9 — the production profile must not have moved.
    if before_sentinel is not None:
        try:
            ctx.step("production_sentinel_after",
                     assert_production_unchanged(before_sentinel["fingerprint"],
                                                production_sentinel()["fingerprint"]))
        except Exception as exc:  # noqa: BLE001 — IsolationError OR a failed
            # measurement: both invalidate the run.
            ctx.step("production_sentinel_after",
                     {"error": ctx.safe(f"{type(exc).__name__}: {exc}"),
                      "unchanged": False, "verdict": "FAIL"})
            report["errors"].append(
                f"production_sentinel_after: {type(exc).__name__}: {exc}")
            log(f"[sentinel] FAIL {exc}")

    # I7 — the config outlives every child and is deleted last. A teardown
    # that failed leaves it in place, on purpose.
    if report["errors"] or not cleanup_ok:
        try:
            remaining = [str(p) for p in ctx.root.rglob("config.yaml")]
        except Exception:  # noqa: BLE001
            remaining = ["<unreadable>"]
        ctx.step("config_lifecycle", {
            "verdict": "FAIL", "deleted": False, "config_preserved": True,
            "removed": [], "config_yaml_remaining": remaining,
            "deleted_after_all_children_dead": False,
            "reason": "teardown reported an error — the generated config is "
                      "PRESERVED on disk for investigation and was NOT deleted"})
        report["config_preserved"] = True
    else:
        try:
            ctx.dispose_configs()
        except Exception as exc:  # noqa: BLE001
            ctx.step("config_lifecycle",
                     {"error": ctx.safe(f"{type(exc).__name__}: {exc}"),
                      "deleted": False, "verdict": "FAIL"})
            report["errors"].append(f"config_lifecycle: {type(exc).__name__}: {exc}")
    return report


# ══════════════════════════════════════════════════════════════════════════
# entry points
# ══════════════════════════════════════════════════════════════════════════
def run_make(args: argparse.Namespace) -> int:
    """The whole run. The verdict is :func:`evaluate_acceptance`, nothing else.

    This function gathers evidence — cases, guards, teardown — and then asks
    the acceptance seal for a single verdict and exit code. It never decides
    on its own, which is the whole point: a run that proved every W case can
    still be rejected by a guard, by a ``run_error``, or by a teardown fault.
    """
    evidence: dict[str, Any] = {}
    ctx: RunContext | None = None
    before_sentinel: dict | None = None
    # Bound before the try so a refusal in the gate still has a selection to
    # evaluate against. A run that never got this far proves nothing, and the
    # missing guard phases say so.
    selected: list[str] = list(FULL_MATRIX)
    try:
        gate = assert_test_host()
        log(f"[gate] hostname={gate['hostname']} verdict=PASS")
        target = resolve_pg_target(dsn=args.dsn, pg_port=args.pg_port,
                                   database=args.pg_database, user=args.pg_user)
        ctx = RunContext(Path(args.root), target, Path(args.output) if args.output else None,
                         Path(args.repo_root) if args.repo_root else REPO_ROOT)
        ctx.evidence["machine"] = {**gate, "pg_target": scrub_dsn(target),
                                   "root": str(ctx.root),
                                   "python": sys.executable}
        before_sentinel = production_sentinel()
        ctx.step("production_sentinel_before",
                 {**{k: v for k, v in before_sentinel.items()
                     if k != "fingerprint"},
                  "verdict": "PASS"})

        ctx.prepare()
        log(f"[profile] {ctx.config_path} (db={ctx.database} port={ctx.port})")
        ctx.step("database_recreate", recreate_database(target))
        ctx.step("fresh_bootstrap", bootstrap_schema(target, ctx.repo_root))
        readback = schema_readback(target)
        ctx.step("schema_readback", readback)
        if readback["verdict"] != "PASS":
            raise F2HarnessError(f"disposable DB lacks the F2 identity schema: {readback}")

        selected = [w.strip().upper() for w in (args.only or "W0,W1,W2,W3,W4").split(",")
                    if w.strip()]
        unknown = [w for w in selected if w not in W_CASES]
        if unknown:
            raise F2HarnessError(f"unknown case(s): {unknown}; known: {sorted(W_CASES)}")

        for name in selected:
            log(f"[{name}] start")
            try:
                record = W_CASES[name](ctx)
            except Exception as exc:  # noqa: BLE001 — one case must not hide the rest
                record = {"verdict": "ERROR", "problems": [f"{type(exc).__name__}: {exc}"],
                          "error": redact(ctx.password, str(exc))}
            ctx.step(name, record)
            ctx.emit()
            log(f"[{name}] {record.get('verdict')} "
                f"rows={record.get('identity_rows', record.get('session_total_rows'))} "
                f"problems={record.get('problems')}")

        ctx.assert_agreement()
    except Exception as exc:  # noqa: BLE001
        if ctx is not None:
            ctx.step("run_error", {"error_type": type(exc).__name__,
                                   "error": ctx.safe(str(exc))})
        log(f"[fatal] {type(exc).__name__}: {redact(ctx.password if ctx else '', exc)}")
    finally:
        if ctx is not None:
            teardown = finalize_teardown(ctx, before_sentinel)
            try:
                ctx.step("teardown", teardown)
            except Exception:  # noqa: BLE001
                pass
            evidence = ctx.evidence
            try:
                ctx.emit()
            except Exception as exc:  # noqa: BLE001 — evidence must be readable
                log(f"[evidence] FAIL writing {ctx.output}: "
                    f"{type(exc).__name__}: {exc}")
                teardown["errors"].append(f"emit: {type(exc).__name__}: {exc}")

    summary = {}
    if evidence:
        for name in FULL_MATRIX:
            if name in evidence.get("steps", {}):
                summary[name] = evidence["steps"][name].get("verdict")
    evidence["summary"] = summary
    if ctx is not None:
        ctx.evidence["summary"] = summary

    # THE ONLY exit-code decision. Everything above is evidence; this is the
    # seal. A W summary that happens to be all-PASS is not sufficient on its
    # own — see evaluate_acceptance.
    acceptance = evaluate_acceptance(evidence, selected)
    if evidence:
        evidence["acceptance"] = acceptance
        if ctx is not None:
            ctx.evidence["acceptance"] = acceptance
            ctx.emit()
    log(f"[summary] {json.dumps(summary, ensure_ascii=False)}")
    for line in acceptance["withheld"]:
        log(f"[withheld] {line}")
    for line in acceptance["failures"]:
        log(f"[reject] {line}")
    log(f"[verdict] {acceptance['verdict']} label={acceptance['label']} "
        f"exit={acceptance['exit_code']} reason={acceptance['reason']}")
    return int(acceptance["exit_code"])


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", default=str(REPO_ROOT),
                        help="repo checkout that provides schema/ and src/")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval.f2_source_idempotency_e2e",
        description="F2 canonical source idempotency — real-HTTP W0-W4 harness "
                    "for the dedicated Windows test laptop.")
    sub = parser.add_subparsers(dest="command", required=True)

    make = sub.add_parser("make", help="run the W0-W4 matrix")
    make.add_argument("--root", required=True,
                      help="isolated run root; every generated path lives here")
    make.add_argument("--dsn", default="",
                      help="explicit PG DSN (default: $F2_PG_DSN)")
    make.add_argument("--pg-port", type=int, default=0,
                      help="disposable PG port when no DSN is supplied")
    make.add_argument("--pg-database", default="f2e2e")
    make.add_argument("--pg-user", default="")
    make.add_argument("--output", default="", help="evidence JSON path")
    make.add_argument("--only", default="",
                      help="comma-separated subset of W0,W1,W2,W3,W4")
    _add_common(make)

    serve = sub.add_parser("child-serve", help="internal: one isolated serve child")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--profile", default="default")
    serve.add_argument("--barrier-host", default="")
    serve.add_argument("--barrier-session", default="")
    serve.add_argument("--barrier-event", default="")
    serve.add_argument("--barrier-marker", default="")
    serve.add_argument("--barrier-release", default="")
    serve.add_argument("--barrier-timeout", type=float, default=900.0)
    serve.add_argument("--tombstone-host", default="")
    serve.add_argument("--tombstone-session", default="")
    serve.add_argument("--tombstone-event", default="")
    serve.add_argument("--tombstone-marker", default="")
    serve.add_argument("--tombstone-timeout", type=float, default=900.0)
    # §6 W2: park the pending unlink, i.e. the phase AFTER the accepted
    # tombstone is really persisted and BEFORE the pending marker is removed.
    serve.add_argument("--post-accepted-host", default="")
    serve.add_argument("--post-accepted-session", default="")
    serve.add_argument("--post-accepted-event", default="")
    serve.add_argument("--post-accepted-marker", default="")
    serve.add_argument("--post-accepted-timeout", type=float, default=900.0)
    _add_common(serve)

    client = sub.add_parser("child-client", help="internal: one client process")
    client.add_argument("--mode", default="deliver",
                        choices=["deliver", "no-ack", "fail-reset", "fail-timeout"])
    client.add_argument("--port", type=int, required=True)
    client.add_argument("--payload", required=True)
    client.add_argument("--http-timeout", type=float, default=120.0)
    # The workload/concurrency gate: holds the REQUEST until the W case says
    # so. Preserved unchanged, and distinct from the launch handshake.
    client.add_argument("--start-barrier", default="")
    client.add_argument("--start-timeout", type=float, default=600.0)
    # The launch handshake: the parent appends this; the child parks here
    # before any HTTP/workload so its live OS identity can be captured first.
    client.add_argument("--identity-release", default="",
                        help="internal: path the parent creates to release "
                             "this child's identity handshake")
    client.add_argument("--identity-timeout", type=float,
                        default=IDENTITY_RELEASE_TIMEOUT)
    _add_common(client)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "make":
        return run_make(args)
    if args.command == "child-serve":
        return _run_serve_child(args)
    if args.command == "child-client":
        return _run_client_child(args)
    raise SystemExit(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
