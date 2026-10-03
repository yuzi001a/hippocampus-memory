#!/usr/bin/env python3
"""M01/M02 memory-correction isolated integration driver (fail-closed).

Scope discipline (mirrors evidence/i01-history-import-20261003/i01_integration.py):

  * runs ONLY on the dedicated test laptop (hostname gate, no override flag)
  * touches ONLY 127.0.0.1:55432 (disposable PostgreSQL) and a fresh, empty run root
  * every database it creates is named ``m01m02e2e_*`` and must NOT pre-exist
  * never connects to production: no port 5433, no ``v3embeddings``, no default profile
  * credentials: process environment only — never argv, never a config file, never the report
  * no LLM, no embedding endpoint, no paid model: keyword-only isolated profile
  * every DDL it drops is gated on :func:`assert_disposable_target`

What it proves (locked design docs/M01-M02-MEMORY-CORRECTION-DESIGN.md + task book §24-§29):

  1. fresh bootstrap reaches the current schema (relations artifact included)
  2. existing-install upgrade closure: dry-run plan -> apply -> replay, data preserved
  3. real public HTTP tool surface: v3_store -> v3_update(correct) -> v3_get(current/history/hm)
     -> v3_search, plus a fresh process/server/session ``/prefetch`` recall
  4. independent SQL verification: A bytes preserved + archived, B active, supersedes edge with
     provenance / effective_at / recorded_at, no fabricated source, raw/QA untouched
  5. blocker set: repeat A->B creates no new rows, A->C conflicts, B->C chains, same-content
     two-id targeting is distinct, unknown / archived targets refused, rollback under an
     injected failure, concurrent identical + divergent corrections, truthful
     migration-required failure without the relations table
  6. real concurrency and real failure injection (ephemeral trigger) — no fakes for the
     HTTP surface; the two writer-layer probes (authority refusal, embedding failure) are
     marked ``derived_only`` because they need a canonical injected stub

The driver targets the planned canonical APIs from the locked design
(``ActiveMemoryWriter.correct(...)`` receipt dict + ``read_version(...)`` reader dict).
When a receipt key required by the design is absent, the check FAILS — the driver never
downgrades a missing contract to a warning.

Not covered (recorded as coverage gaps, never as passes): M03 derived propagation, M04
temporal/as-of reasoning, semantic or fuzzy target selection, a live model answering the
fresh-session question, host adapters, and production activation.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
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

# ── hard gates ──────────────────────────────────────────────────────────────
HOST_GATE = "DESKTOP-EQP3OBU"
PG_HOST_ALLOWLIST = ("127.0.0.1",)
PG_PORT_ALLOWLIST = (55432,)
DB_PREFIX = "m01m02e2e_"
#: never a target, no matter what the caller passes
PRODUCTION_DATABASES = ("v3embeddings",)
PRODUCTION_PORTS = (5433,)

#: Ambient environment that must NEVER be inherited by an isolated child: a
#: model/embedding credential, a provider endpoint, a proxy, or a production
#: bypass switch. Only the PG password is re-supplied by the driver (runtime
#: env, never a file, never a config, never argv).
AMBIENT_SECRET_ENV_TOKENS = (
    "API_KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL",
    "OPENAI", "ANTHROPIC", "DEEPSEEK", "MINIMAX", "MOONSHOT", "DASHSCOPE",
    "QWEN", "GEMINI", "GOOGLE_API", "GOOGLE_APPLICATION", "AZURE", "AWS_",
    "COHERE", "MISTRAL", "GROQ", "TOGETHER", "OLLAMA", "SILICONFLOW",
    "ZHIPU", "BAIDU", "DOUBAO", "XAI_", "OPENROUTER", "HUGGINGFACE",
    "EMBED", "RERANK", "LLM_", "ENDPOINT", "BASE_URL", "BASEURL", "PROXY",
)
#: Production bypass / guard-override switches that are cleared, never inherited
#: into a disposable run.
PRODUCTION_BYPASS_ENV_TOKENS = (
    "ALLOW_LIVE_PROD", "PROD_OUTBOX", "ALLOW_PRODUCTION", "ALLOW_PROD",
    "PRODUCTION_BYPASS", "GUARD_OVERRIDE", "SKIP_GUARD", "BYPASS",
)
#: Modules whose INSTALLED bytes must equal the source-root bytes BEFORE any
#: database connection: package entry, writer, tool layer, distribution.
ARTIFACT_PROVENANCE_MODULES = (
    "__init__.py",
    "active_memory_store.py",
    "tools/api_update.py",
    "tools/get_tool.py",
    "tools/memory_correction.py",
    "tools/api_get.py",
    "distribution_cli.py",
)
#: Packaged schema artifacts that must be present in the installed wheel AND
#: match the repo source copy. ``bool(packaged_map)`` alone is not enough.
REQUIRED_PACKAGED_SQL = ("explicit_memories.sql", "memory_relations.sql")

#: The ONLY tree this driver may create a run root in or write evidence to.
#: A production-home symlink / junction pointing at it is refused: the run root
#: must be LEXICALLY inside this directory, not merely reach it through a link.
RUN_ROOT_PARENT = Path("C:/hp-testbed/m01-m02-integration-20261003")

READY_TIMEOUT_S = 90.0
HTTP_TIMEOUT_S = 90.0
MAX_RECORDED_CHARS = 6000

# Design §M01: the relations sidecar and the columns the driver independently reads.
RELATION_TABLE = "public.memory_relations"
RELATION_REQUIRED_COLUMNS = (
    "from_memory_id",
    "to_memory_id",
    "relation_type",
    "authority",
    "correction_source_id",
    "effective_at",
)
RELATION_TIMESTAMP_COLUMNS = ("recorded_at", "created_at")
RELATION_REQUEST_COLUMNS = (
    "request_json",
    "request",
    "canonical_request",
    "canonical_request_json",
    "request_body",
)
RELATION_CORRECTION_ID_COLUMNS = ("correction_id", "correction_key", "request_sha256")

# Design §M01: the canonical columns of an explicit-memories row, paired with the
# SQL expression that READS each one. ``memory_row`` keys its result dict by the
# FIRST element — never by the SQL text, because a cast such as
# ``provenance::text`` would otherwise leak in as a ``::``-suffixed key and every
# canonical access (``row["provenance"]``, ``row["created_at"]``, ...) would raise
# KeyError and abort the run. The expressions keep whatever casts the read needs.
MEMORY_ROW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("memory_id", "memory_id"),
    ("category", "category"),
    ("title", "title"),
    ("content", "content"),
    ("tags", "tags"),
    ("provenance", "provenance::text"),
    ("status", "status"),
    ("created_at", "created_at::text"),
    ("updated_at", "updated_at::text"),
)
#: The canonical key set ``memory_row`` returns, in SELECT order.
MEMORY_ROW_KEYS: tuple[str, ...] = tuple(name for name, _ in MEMORY_ROW_COLUMNS)

#: The writer-layer probes the driver REQUIRES the embedded probe process to
#: report. An absent field is a contract failure, not something to skip over:
#: a probe map missing any of these means the intended path never ran.
#: A syntactically valid conversation_stream.id-shaped STRING that is
#: guaranteed not to exist. Used for the "fabricated source" refusal so the
#: refusal can only come from the source lookup, never from type validation.
BOGUS_SOURCE_ID = "999999999"

#: The PG password, bound once the secrets file is read. A module global so the
#: crash handler can scrub a traceback that unwinds out of main() — a crash
#: report that leaks the credential is a failed run twice over.
_ACTIVE_SECRET = ""


DERIVED_PROBE_KEYS = (
    "no_authority_refused",
    "inference_authority_refused",
    "none_authority_refused",
    "unknown_source_refused",
    "embedding_failure_stays_durable",
    "unknown_target_refused",
)

# Design §13: a correction response must expose WHICH rows changed, not just success.
RECEIPT_ID_KEYS = {
    "old": ("old_memory_id", "target_memory_id", "from_memory_id", "superseded_memory_id"),
    "new": ("new_memory_id", "replacement_memory_id", "resolved_memory_id",
            "to_memory_id", "memory_id", "id"),
}


class Refused(RuntimeError):
    """A fail-closed gate refusal: nothing was created, nothing was written."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"[{code}] {reason}")
        self.code = int(code)
        self.reason = reason


def log(key: str, value: object) -> None:
    print(f"{key}={value}", flush=True)


# ── pure gate helpers (unit-tested offline by the guard contract test) ───────


def gate_hostname(machine: str) -> str:
    """Empty string when the host is the dedicated test laptop, else the reason."""
    name = (machine or "").strip().upper()
    if not name:
        return "hostname could not be determined"
    if name != HOST_GATE:
        return f"hostname {name!r} is not the Y400 test laptop {HOST_GATE!r}"
    return ""


def gate_pg_target(host: str, port: int, db_name: str) -> str:
    """Empty string when host/port/db are all inside the disposable sandbox."""
    if (host or "").strip() not in PG_HOST_ALLOWLIST:
        return f"PG host {host!r} is not in the allowlist {PG_HOST_ALLOWLIST}"
    try:
        port_num = int(port)
    except (TypeError, ValueError):
        return f"PG port {port!r} is not an integer"
    if port_num in PRODUCTION_PORTS:
        return f"PG port {port_num} is a production port; refusing"
    if port_num not in PG_PORT_ALLOWLIST:
        return f"PG port {port_num} is not in the allowlist {PG_PORT_ALLOWLIST}"
    name = (db_name or "").strip()
    if not name.startswith(DB_PREFIX):
        return f"database {name!r} must start with {DB_PREFIX!r}"
    if name in PRODUCTION_DATABASES:
        return f"database {name!r} is a production database; refusing"
    if ".." in name or any(ch in name for ch in "\"' ;\\/\n\r\t"):
        return f"database {name!r} contains characters that are not allowed in an identifier"
    return ""


def _is_lexically_inside(child: Path, parent: Path) -> bool:
    """True when ``child`` is a strict descendant of ``parent`` BOTH lexically
    and after symlink/junction resolution.

    Two independent component-wise (``parts``) containment checks, both
    required — a lexical match alone is NOT containment:

    * LEXICAL: the normalized ``child`` path extends ``parent``'s parts, so a
      string-prefix sibling like ``.../20261003-evil`` is never treated as a
      child of ``.../20261003``.
    * RESOLVED: ``Path.resolve()`` collapses symlinks/junctions on both sides,
      so a junction under the run root that routes OUT of it resolves outside
      and is refused, and a production-home link that merely RESOLVES into the
      run root is refused too (its lexical route is not confined).

    Strict descendant on both sides: equal paths are not "inside".
    """
    try:
        c = Path(os.path.normpath(str(child)))
        p = Path(os.path.normpath(str(parent)))
        c_resolved = c.resolve()
        p_resolved = p.resolve()
    except (TypeError, ValueError, OSError):
        return False

    def _strict(c_parts: tuple, p_parts: tuple) -> bool:
        if len(c_parts) <= len(p_parts):
            return False
        return c_parts[:len(p_parts)] == p_parts

    return _strict(c.parts, p.parts) and _strict(c_resolved.parts, p_resolved.parts)


def gate_run_root_location(root: Path, out: Path) -> str:
    """Empty string when root and out are both confined to RUN_ROOT_PARENT.

    Two independent confinement conditions, both fail-closed:

    1. the run root lexically resolves under :data:`RUN_ROOT_PARENT`;
    2. the evidence file is itself confined to that same run root, so a driver
       that passed the root gate cannot still write its report somewhere else.
    """
    if not _is_lexically_inside(root, RUN_ROOT_PARENT):
        return (f"run root {root} is not lexically inside the mandated run root parent "
                f"{RUN_ROOT_PARENT} (a symlink/junction route in or out is refused)")
    if not _is_lexically_inside(out, root):
        return (f"--out {out} is not inside the run root {root}; the driver may only "
                f"write evidence inside its own run root")
    return ""


def gate_env_artifact(resolved_v3core: str, source: Path, env_python: str) -> str:
    """Empty string when the interpreter really loads the artifact under test.

    A generic ``"site-packages" in path`` substring test is not enough: any
    venv on the machine satisfies it, so a run pointed at a stale or unrelated
    wheel would pass. ``__file__`` is resolved and must satisfy ONE of:

    * it is inside ``--source-root`` (a live source checkout), or
    * it is inside THIS ``--env-python``'s own ``site-packages`` (the exact
      isolated venv the caller named).

    Both are Path-containment checks on the resolved file, not string matching.
    """
    if not resolved_v3core:
        return "the interpreter did not report a resolved v3core.__file__"
    try:
        resolved = Path(resolved_v3core).resolve()
    except (TypeError, ValueError, OSError):
        return f"v3core.__file__ {resolved_v3core!r} is not a resolvable path"
    if not resolved.is_file():
        return f"v3core.__file__ {resolved} does not exist as a file"
    try:
        source_root = Path(source).resolve()
    except (TypeError, ValueError, OSError):
        return f"--source-root {source} is not a resolvable path"
    if _is_lexically_inside(resolved, source_root):
        return ""
    # Exact venv containment: the resolved file must live under THIS
    # --env-python's own site-packages. Both the Windows layout
    # (<venv>/Lib/site-packages) and the POSIX one
    # (<venv>/lib/pythonX.Y/site-packages) are enumerated, so a wheel in an
    # unrelated venv can never satisfy the check by a shared path substring.
    env_dir = Path(os.path.normpath(str(Path(env_python).parent.parent)))
    site_dirs = [env_dir / "site-packages", env_dir / "Lib" / "site-packages",
                 env_dir / "dist-packages", env_dir / "Lib" / "dist-packages"]
    lib_dir = env_dir / "lib"
    if lib_dir.is_dir():
        try:
            site_dirs.extend(
                child / "site-packages" for child in sorted(lib_dir.iterdir())
                if child.is_dir())
        except OSError:
            pass
    for candidate in site_dirs:
        if _is_lexically_inside(resolved, candidate) or resolved.parent == candidate:
            return ""
    return (f"v3core resolved to {resolved}, which is neither inside --source-root "
            f"{source_root} nor inside the named --env-python venv site-packages "
            f"({env_dir}); refusing to test a different artifact")


def gate_root(root: Path, *, must_exist: bool) -> str:
    """Empty string when the run root is a real, empty directory."""
    if must_exist and not root.is_dir():
        return f"run root {root} does not exist"
    if not root.is_dir():
        return f"run root {root} is not a directory"
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        return f"run root {root} is unreadable ({exc.strerror or exc})"
    if entries:
        names = sorted(p.name for p in entries)[:8]
        return f"run root {root} is not empty (contains {names})"
    return ""


def assert_disposable_target(db_name: str) -> None:
    """Guard for every DDL/DML statement that is destructive. Fails closed."""
    reason = gate_pg_target("127.0.0.1", PG_PORT_ALLOWLIST[0], db_name)
    if reason:
        raise Refused(97, f"refusing a destructive statement: {reason}")


def dsn_without_password(host: str, port: int, db_name: str, user: str) -> str:
    """CLI/DSN string with NO password — the password lives in the environment only."""
    return f"postgresql://{user}@{host}:{port}/{db_name}"


def sanitize_env(environ: dict) -> dict:
    """Drop ambient credentials/endpoints and production bypasses.

    The isolated child must not inherit a paid-model key, an embedding/rerank
    endpoint, a proxy or a production bypass from the developer shell. Only the
    PG password is re-supplied by the driver afterwards (runtime env only), so
    everything matching the credential/endpoint token list or the bypass token
    list is removed rather than passed through.
    """
    out: dict = {}
    for key, value in environ.items():
        name = str(key).upper()
        if name.startswith("PG"):
            continue
        if any(token in name for token in AMBIENT_SECRET_ENV_TOKENS):
            continue
        if any(token in name for token in PRODUCTION_BYPASS_ENV_TOKENS):
            continue
        out[key] = value
    return out


def isolated_dotenv_path(home) -> Path:
    """The isolated dotenv the driver binds ``V3CORE_DOTENV`` to.

    It lives INSIDE the isolated home so no run can reach an ambient ``.env``,
    and it is deliberately EMPTY: the product's ``v3core.config`` refuses a
    dangling explicit ``V3CORE_DOTENV`` (a dangling value is a fail-closed
    refusal, not a silent no-dotenv), so the path must EXIST, while emptiness
    guarantees no credential/endpoint can be imported from it.
    """
    return Path(os.path.normpath(str(Path(home) / ".env.isolated")))


def ensure_isolated_dotenv(home) -> Path:
    """Create the EMPTY isolated dotenv and return its path (fail-closed).

    Raises ``OSError`` when the file cannot be created or does not exist as a
    regular file afterwards, so the caller refuses before any server start.
    """
    path = isolated_dotenv_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Explicitly write EMPTY content; never leave creation to a later touch.
    path.write_bytes(b"")
    if not path.is_file():
        raise OSError(f"isolated dotenv {path} was not created as a file")
    return path


def gate_isolated_dotenv(path) -> str:
    """Empty string when ``path`` is an existing regular file (fail-closed).

    The product treats a non-existent explicit ``V3CORE_DOTENV`` as a refusal,
    so the driver refuses FIRST — before any DB connect and any server start —
    rather than letting a dangling path surface as an opaque ``/health``
    timeout.
    """
    try:
        candidate = Path(path)
    except (TypeError, ValueError):
        return f"isolated dotenv {path!r} is not a usable path"
    if not candidate.is_file():
        return (f"isolated dotenv {candidate} does not exist as a file "
                "(a dangling explicit V3CORE_DOTENV is refused by the product)")
    return ""


def artifact_provenance_mismatches(expected: dict, actual: dict,
                                   required) -> list[str]:
    """Required fingerprint keys whose INSTALLED hash is missing or different.

    ``gate_env_artifact`` proves the interpreter loads *a* v3core inside the
    named venv; it cannot tell a stale wheel from the source under test. This
    compares the actual installed bytes (module files + packaged SQL) against
    the source root. A key absent from either side, or whose sha256 differs, is
    a mismatch — never silently accepted.
    """
    problems: list[str] = []
    if not isinstance(actual, dict):
        return [f"installed fingerprint is not an object: {type(actual).__name__}"]
    if not isinstance(expected, dict):
        return ["source-root fingerprint is not an object"]
    for key in required:
        installed = actual.get(key)
        source = expected.get(key)
        if not installed:
            problems.append(f"{key}: missing from the installed artifact")
            continue
        if not source:
            problems.append(f"{key}: missing from the source-root artifact")
            continue
        if str(installed) != str(source):
            problems.append(
                f"{key}: installed {str(installed)[:12]} != source {str(source)[:12]}")
    return problems


def timestamps_equal(left, right) -> bool:
    """True when two timestamp renderings denote the same instant.

    The receipt carries the writer's ISO rendering while the independent SQL
    read carries PostgreSQL's ``timestamptz::text``; comparing the raw strings
    would fail on formatting alone, so both are parsed and compared as aware
    datetimes.
    """
    import datetime as _datetime

    def _parse(value):
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        # PostgreSQL renders a whole-hour offset as '+08'; fromisoformat wants
        # '+08:00' on older interpreters.
        if len(text) >= 3 and text[-3] in "+-" and text[-2:].isdigit():
            text = text + ":00"
        try:
            parsed = _datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
        return parsed

    a, b = _parse(left), _parse(right)
    return a is not None and b is not None and a == b


def first_key_value(obj, key: str):
    """Value stored under ``key`` (top level first, then nested objects).

    Unlike :func:`pick` this preserves an explicit ``None``: a canonical read
    that answers ``memory: null`` must be observable as null, not as "absent".
    """
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            if isinstance(value, dict):
                found = first_key_value(value, key)
                if found is not None:
                    return found
    return None


def _key_present(obj, key: str) -> bool:
    """True when ``key`` exists at all (top level first, then nested objects).

    :func:`first_key_value` returns ``None`` for both "absent" and "explicitly
    null", so it cannot prove a canonical field was actually reported. This
    distinguishes the two: a response that omits the field entirely is malformed
    and must be refused, while ``memory: null`` is a real no-current answer.
    """
    if isinstance(obj, dict):
        if key in obj:
            return True
        for value in obj.values():
            if isinstance(value, dict) and _key_present(value, key):
                return True
    return False


def reader_current_absent(payload: dict) -> tuple[bool, str]:
    """(no_current, detail) for a canonical current-mode read.

    A withdrawn chain (or an archived row with no edge) must answer with NO
    current memory: ``memory`` None, no resolved/current id, ``is_current``
    False. Returning the archived payload as if it were current is the exact
    failure this guards.

    The canonical fields must be PRESENT at the TOP LEVEL of the flattened tool
    payload: a malformed response that only carries ``is_current: false`` (no
    top-level ``memory`` key) is refused rather than read as no-current. An
    explicit ``memory: null`` is accepted. Only the top-level mapping is read —
    an unrelated metadata / ``diagnostic`` / raw ``_envelope`` sub-object can
    neither supply a missing canonical field nor splice sibling fields together.
    """
    if not isinstance(payload, dict):
        return False, f"payload is not an object: {type(payload).__name__}"
    if "memory" not in payload:
        return False, (
            "payload has no top-level canonical 'memory' field: "
            f"keys={sorted(payload)!r}")
    memory = payload["memory"]
    resolved = payload.get("resolved_memory_id")
    current = payload.get("current_memory_id")
    is_current = payload.get("is_current")
    no_current = (
        memory is None
        and resolved in (None, "")
        and current in (None, "")
        and is_current is False
    )
    return no_current, (
        f"memory={memory!r} resolved={resolved!r} current={current!r} "
        f"is_current={is_current!r}"
    )


def reader_read_mode(payload) -> str:
    """The read mode the canonical object reports, or '' when it has none.

    The product sets ``mode`` on every canonical read (``handle_memory_target``
    defaults it), so a payload with no usable mode is refused rather than
    guessed at.
    """
    if not isinstance(payload, dict):
        return ""
    mode = payload.get("mode")
    return mode if isinstance(mode, str) else ""


def reader_selected_row(payload) -> dict | None:
    """The nested row the reported MODE selects, in the SAME canonical object.

    * ``mode=current`` -> the ``memory`` row (the resolved current version);
    * ``mode=history`` -> the ``requested`` row (the exact version named);
    * any other / absent mode -> ``None`` (fail closed, never guessed).

    Only the mode-selected nested row of THIS object is read: a sibling such as
    ``requested`` is never substituted for the current payload, and no
    recursive key search runs, so ``requested.content`` can never win over the
    selected row's own value.
    """
    if not isinstance(payload, dict):
        return None
    mode = reader_read_mode(payload)
    if mode == "current":
        row = payload.get("memory")
    elif mode == "history":
        row = payload.get("requested")
    else:
        return None
    return row if isinstance(row, dict) else None


def reader_current_read(payload) -> dict:
    """A canonical current-mode read, each field from the right place.

    The payload fields (``content`` / ``memory_id`` / ``status`` / ``category``
    / ``title``) come from the mode-selected nested ``memory`` row; the chain
    fields (``resolved_memory_id`` / ``current_memory_id`` / ``is_current`` /
    ``current_status``) come from the same transport-decoded object's own
    top-level fields. Neither is ever borrowed from a sibling row, and a
    withdrawn chain (``memory=None``) reports ``has_current=False`` with an
    EMPTY payload instead of the archived ``requested`` row's content.

    ``is_current`` is the product's documented semantic — whether the REQUESTED
    id is itself the current version (``current_id == memory_id``) — so it is
    False for a read of a superseded id that resolves to a successor: the
    resolved row is current, the requested one is not. ``requested_memory_id``
    / ``requested_status`` are exposed for the echo check only.
    """
    row = reader_selected_row(payload) or {}
    requested = payload.get("requested") if isinstance(payload, dict) else None
    requested = requested if isinstance(requested, dict) else {}
    top = payload if isinstance(payload, dict) else {}

    def _text(value) -> str:
        return "" if value is None else str(value)

    return {
        "mode": reader_read_mode(payload),
        "has_current": bool(row),
        "memory": row or None,
        "content": row.get("content"),
        "memory_id": _text(row.get("memory_id")),
        "status": row.get("status"),
        "category": row.get("category"),
        "title": row.get("title"),
        "resolved_memory_id": _text(top.get("resolved_memory_id")),
        "current_memory_id": _text(top.get("current_memory_id")),
        "is_current": top.get("is_current"),
        "current_status": top.get("current_status"),
        "requested_memory_id": _text(requested.get("memory_id")),
        "requested_status": requested.get("status"),
    }


def reader_rendered_content(payload) -> tuple:
    """(present, content) for a FLATTENED canonical payload (the ``hm`` target).

    ``render_canonical_for_source`` flattens the MODE-SELECTED nested row into
    a top-level ``content`` — ``None`` when the mode has no payload, so a
    withdrawn chain never answers with the archived payload. The flattened
    field is read literally; the ``requested`` / ``memory`` siblings are never
    searched for a substitute.
    """
    if not isinstance(payload, dict) or "content" not in payload:
        return False, ""
    value = payload.get("content")
    return True, "" if value is None else str(value)


def reader_history_block(payload) -> dict:
    """The ``history`` object of a canonical history-mode read.

    The product nests the chain as an OBJECT (``versions`` / ``edges`` /
    ``current_memory_id``) — not as a bare top-level list, which is why a
    top-level-only reader saw an empty chain.
    """
    if not isinstance(payload, dict):
        return {}
    block = payload.get("history")
    return block if isinstance(block, dict) else {}


def reader_history_entries(payload) -> list:
    """The ordered version rows of a canonical history read (dicts only).

    Reads the nested ``history.versions`` list the product emits. A legacy
    top-level ``history`` list, or a top-level ``versions`` /
    ``version_history`` / ``timeline`` list, is still accepted for the older
    shapes. The ``requested`` / ``memory`` / ``current`` sibling rows are never
    read as the version list.
    """
    if not isinstance(payload, dict):
        return []
    block = payload.get("history")
    if isinstance(block, list):
        return [item for item in block if isinstance(item, dict)]
    if isinstance(block, dict):
        versions = block.get("versions")
        if isinstance(versions, list):
            return [item for item in versions if isinstance(item, dict)]
    for key in ("versions", "version_history", "timeline"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return [item for item in value if isinstance(item, dict)]
    return []


def reader_history_ids(payload: dict) -> list[str]:
    """Ordered ``memory_id`` values of a canonical history read."""
    return [str(item.get("memory_id") or "") for item in reader_history_entries(payload)]


def reader_history_statuses(payload: dict) -> list[str]:
    """Ordered status/label values of a canonical history read."""
    return [str(item.get("status") or item.get("label") or "")
            for item in reader_history_entries(payload)]


def reader_history_labels(payload) -> list[str]:
    """Ordered ``label`` values of a canonical history read, as emitted.

    The product labels every version explicitly (``current`` / ``superseded`` /
    ``withdrawn`` / ``archived``), so the labels are read verbatim rather than
    inferred from the row status.
    """
    return [str(item.get("label") or "") for item in reader_history_entries(payload)]


def reader_history_edges(payload) -> list:
    """The ordered edge rows of a canonical history read.

    ``history.edges`` is the product's full chain edge list (ancestors -> self
    -> descendants, a withdrawal edge included). The top-level ``relation``
    field is only the REQUESTED row's single outgoing edge and is never used as
    the chain.
    """
    if not isinstance(payload, dict):
        return []
    block = payload.get("history")
    if isinstance(block, dict) and isinstance(block.get("edges"), list):
        return [item for item in block["edges"] if isinstance(item, dict)]
    value = payload.get("edges")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def reader_history_current_memory_id(payload) -> str:
    """``history.current_memory_id`` — the chain's terminal current id.

    Empty string when the chain has no current version (a withdrawn terminal).
    """
    block = reader_history_block(payload)
    value = block.get("current_memory_id")
    return "" if value is None else str(value)


def reader_history_is_current(payload) -> list:
    """Ordered ``is_current`` flags of a canonical history read, as emitted.

    A withdrawn chain has no current version, so every version is False — the
    archived terminal is never labelled current just because the chain ends.
    """
    return [bool(item.get("is_current")) for item in reader_history_entries(payload)]


def reader_requested(payload: dict) -> dict:
    """The exact requested payload of a canonical history read."""
    if isinstance(payload, dict) and isinstance(payload.get("requested"), dict):
        return payload["requested"]
    return {}


def reader_relation_chain(payload: dict) -> list:
    """(relation_type, from_memory_id, to_memory_id) triples from a read.

    The canonical history result carries the ordered chain under
    ``history.edges``; that nested list IS the chain. The top-level
    ``relation`` / ``relations`` fields are only the requested row's own
    outgoing edge (or an older flat shape), so they are read ONLY when the
    nested history block carries no edges — never in place of it.
    """
    if not isinstance(payload, dict):
        return []
    nested = reader_history_edges(payload)
    if nested:
        candidates: list = list(nested)
    else:
        candidates = []
        for key in ("relations", "edges", "relation", "versions"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, dict):
                candidates.append(value)
    chain: list = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        rtype = item.get("relation_type") or item.get("edge_type") or item.get("type")
        frm = item.get("from_memory_id")
        to = item.get("to_memory_id")
        if rtype and frm:
            chain.append((str(rtype), str(frm), None if to is None else str(to)))
    return chain


def relation_chain_matches_exact(chain, expected) -> bool:
    """True only when ``chain`` equals ``expected`` EXACTLY, in order.

    A set/superset comparison silently accepts a reordered, duplicated or extra
    edge; the dedicated withdraw scenario must prove the chain is the exact
    ordered A->B->C->withdraw sequence, so the two sequences are compared
    element-wise.
    """
    return list(chain) == list(expected)


def history_versions_match_exact(ids, labels, statuses,
                                 expected_ids, expected_labels,
                                 expected_statuses) -> bool:
    """True only when a history read's ids/labels/statuses align EXACTLY.

    The dedicated chain's history read may be emitted ancestor-first or
    terminal-first; accept exactly those two orders, and require the label and
    status sequences to align index-for-index with the ids in whichever order
    was emitted. A set / length / substring comparison silently accepts a
    mislabelled or misaligned version, so the whole ordered triple is compared
    element-wise. This lets the PRE-withdraw read assert a CURRENT terminal and
    the POST-withdraw read assert a WITHDRAWN one.
    """
    ids = [str(x) for x in ids]
    labels = [str(x) for x in labels]
    statuses = [str(x) for x in statuses]
    expected_ids = [str(x) for x in expected_ids]
    expected_labels = [str(x) for x in expected_labels]
    expected_statuses = [str(x) for x in expected_statuses]
    if not (len(ids) == len(labels) == len(statuses) == len(expected_ids)):
        return False
    if ids == expected_ids:
        return labels == expected_labels and statuses == expected_statuses
    if ids == list(reversed(expected_ids)):
        return (labels == list(reversed(expected_labels))
                and statuses == list(reversed(expected_statuses)))
    return False


#: Fresh-session recall classifications. ``embeddings_unavailable`` is an
#: ENVIRONMENT verdict (a declared coverage gap), never a silent pass and never a
#: product failure; ``recall_missed_current_version`` is a real failure.
RECALL_CURRENT_RECALLED = "current_version_recalled"
RECALL_EMBEDDINGS_UNAVAILABLE = "embeddings_unavailable"
RECALL_MISSED_CURRENT = "recall_missed_current_version"


def embedding_service_configured_from_config(text: str) -> bool:
    """True when the profile's ``embed`` block names a real endpoint.

    The isolated profile this driver writes is keyword-only
    (``embed.endpoint: ''``), so its fresh-session recall cannot build a vector
    block. Reading the written config back makes that an observed fact instead
    of an assumption.
    """
    inside_embed = False
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped == "embed:":
            inside_embed = True
            continue
        if not inside_embed:
            continue
        if line.startswith("  ") and not line.startswith("    "):
            break                       # the next top-level key
        if stripped.startswith("endpoint:"):
            value = stripped.split("endpoint:", 1)[1].strip()
            return value not in ("", "''", '""')
    return False


def recall_environment(*, embedding_service_configured: bool, rows_total,
                       rows_with_embedding) -> dict:
    """Whether THIS environment can serve a vector recall at all.

    ``vector_recall_available`` requires BOTH a configured embedding service
    AND at least one canonical row that actually carries an embedding. The
    isolated keyword-only profile has neither, so an empty vector block here is
    an ENVIRONMENT fact, not a recall defect.
    """
    service = bool(embedding_service_configured)
    total = int(rows_total or 0)
    embedded = int(rows_with_embedding or 0)
    return {
        "embedding_service_configured": service,
        "canonical_rows": total,
        "rows_with_embedding": embedded,
        "vector_recall_available": bool(service and embedded > 0),
    }


def classify_recall(block, *, current_content: str, superseded_contents=(),
                    embeddings_available: bool) -> dict:
    """Classify a fresh-session recall block, never silently.

    * ``current_version_recalled`` — the block carries the current version.
    * ``embeddings_unavailable`` — it does not, AND this environment cannot
      serve a vector recall (no embedding service / no embedded rows): the
      empty block is an ENVIRONMENT fact, declared as a coverage gap. The
      caller must still fail on ``superseded_present``.
    * ``recall_missed_current_version`` — embeddings ARE available and the
      current version is still absent: a genuine recall failure.
    """
    text = str(block or "")
    superseded_present = [str(c) for c in superseded_contents
                          if c and str(c) in text]
    common = {"block_chars": len(text), "superseded_present": superseded_present}
    if current_content and str(current_content) in text:
        return {"classification": RECALL_CURRENT_RECALLED, **common}
    if not embeddings_available:
        return {"classification": RECALL_EMBEDDINGS_UNAVAILABLE, **common}
    return {"classification": RECALL_MISSED_CURRENT, **common}


#: Fingerprints the INSTALLED interpreter computes for the modules under test
#: plus every packaged schema artifact it ships. Kept as a literal probe so the
#: driver process never imports v3core.
_ARTIFACT_PROVENANCE_PROBE = r'''
import hashlib
import json
from pathlib import Path

import v3core

pkg = Path(v3core.__file__).resolve().parent


def _sha(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


modules = {}
for rel in %(modules)r:
    modules[rel] = _sha(pkg / rel)
sql = {}
sql_dir = pkg / "schema"
try:
    for child in sorted(sql_dir.iterdir()):
        if child.name.endswith(".sql"):
            sql[child.name] = _sha(child)
except OSError:
    pass
print(json.dumps({"package_dir": str(pkg), "modules": modules, "sql": sql}))
'''


def _fingerprint_source_artifact(source: Path, module_rels, sql_names) -> dict:
    """Source-root counterpart of the installed-artifact fingerprint map."""
    pkg = Path(source) / "src" / "v3-core" / "src" / "v3core"
    out: dict = {}
    for rel in module_rels:
        path = pkg / rel
        out[f"module:{rel}"] = sha256_file(path) if path.is_file() else ""
    for name in sql_names:
        path = pkg / "schema" / name
        out[f"sql:{name}"] = sha256_file(path) if path.is_file() else ""
    return out


def collect_artifact_provenance(python: str, env: dict, cwd: Path,
                                source: Path) -> dict:
    """Installed module + packaged-SQL fingerprints vs the source root.

    Returns ``{"actual": {...}, "expected": {...}, "required": [...],
    "package_dir": ..., "error": ...}``. Both sides are keyed identically
    (``module:<rel>`` / ``sql:<name>``) so a mismatch is a byte difference, not
    a path difference. A probe that cannot run leaves ``actual`` empty, and the
    required keys then fail — never a silent pass.
    """
    result: dict = {"actual": {}, "expected": {}, "required": []}
    try:
        probe = subprocess.run(
            [python, "-B", "-c",
             _ARTIFACT_PROVENANCE_PROBE % {"modules": tuple(ARTIFACT_PROVENANCE_MODULES)}],
            env=env, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = f"provenance probe could not run: {type(exc).__name__}: {exc}"
        result["required"] = ([f"module:{rel}" for rel in ARTIFACT_PROVENANCE_MODULES]
                              + [f"sql:{name}" for name in REQUIRED_PACKAGED_SQL])
        return result
    parsed = _try_json((probe.stdout or "").strip())
    result["required"] = ([f"module:{rel}" for rel in ARTIFACT_PROVENANCE_MODULES]
                          + [f"sql:{name}" for name in REQUIRED_PACKAGED_SQL])
    if not isinstance(parsed, dict):
        result["error"] = (
            f"provenance probe rc={probe.returncode} "
            f"stdout={(probe.stdout or '')[:200]!r} "
            f"stderr={(probe.stderr or '')[:200]!r}")
        return result
    result["package_dir"] = parsed.get("package_dir")
    actual: dict = {}
    for rel, sha in (parsed.get("modules") or {}).items():
        actual[f"module:{rel}"] = sha
    for name, sha in (parsed.get("sql") or {}).items():
        actual[f"sql:{name}"] = sha
    result["actual"] = actual
    result["expected"] = _fingerprint_source_artifact(
        source, (parsed.get("modules") or {}).keys(), (parsed.get("sql") or {}).keys())
    return result




def build_tool_request(name: str, args: dict) -> dict:
    """Body of the real public ``POST /tool`` route (see v3core.serve._handle_tool)."""
    if not name or not isinstance(args, dict):
        raise Refused(96, "tool request needs a non-empty name and an args object")
    return {"name": name, "args": args}


def build_prefetch_request(query: str, session_id: str) -> dict:
    if not (query or "").strip():
        raise Refused(96, "prefetch request needs a non-empty query")
    return {"query": query, "session_id": session_id}


def build_events_request(
    *,
    session_id: str,
    event_id: str,
    host: str,
    role: str,
    content: str,
    turn_id: int,
) -> dict:
    """Body of the real public ``POST /events`` route (B01 bridge contract)."""
    if not session_id or not event_id or not content:
        raise Refused(96, "events request needs session_id / event_id / content")
    return {
        "host": host,
        "session_id": session_id,
        "event_id": event_id,
        "role": role,
        "content": content,
        "turn_id": int(turn_id),
    }


def schema_copy_report(schema_dirs: list[Path]) -> dict:
    """Compare the repo schema artifact directory with the packaged copy.

    Generic over file names: a NEW artifact added by the M01/M02 work is compared
    automatically, so the single-source contract cannot silently fork.
    """
    report: dict = {"dirs": [str(d) for d in schema_dirs], "in_sync": True, "files": {}}
    if len(schema_dirs) < 2:
        report["in_sync"] = False
        report["reason"] = "need at least two schema directories to compare"
        return report
    primary = schema_dirs[0]
    others = schema_dirs[1:]
    for sql in sorted(primary.glob("*.sql")):
        name = sql.name
        entry = {
            "sha256": hashlib.sha256(sql.read_bytes()).hexdigest(),
            "size_bytes": sql.stat().st_size,
            "matches": {},
        }
        for other in others:
            peer = other / name
            if not peer.is_file():
                entry["matches"][str(other)] = False
                report["in_sync"] = False
                continue
            same = hashlib.sha256(peer.read_bytes()).hexdigest() == entry["sha256"]
            entry["matches"][str(other)] = same
            if not same:
                report["in_sync"] = False
        report["files"][name] = entry
    primary_names = {p.name for p in primary.glob("*.sql")}
    for other in others:
        extra = sorted({p.name for p in other.glob("*.sql")} - primary_names)
        if extra:
            report["in_sync"] = False
            report.setdefault("only_in_packaged", {})[str(other)] = extra
    if not report["files"]:
        report["in_sync"] = False
        report["reason"] = "no .sql artifacts found in the canonical schema directory"
    return report


def pick(payload: dict, *keys: str):
    """First present, non-empty value among ``keys`` (nested dicts included)."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    for value in payload.values():
        if isinstance(value, dict):
            found = pick(value, *keys)
            if found is not None:
                return found
    return None


def find_values(obj, key: str) -> list:
    """Every value stored under ``key`` anywhere in a nested JSON structure."""
    found: list = []
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k == key:
                    found.append(v)
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(x for x in cur if isinstance(x, (dict, list)))
    return found


def require_fields(payload: dict, canonical: dict, *, what: str) -> list[str]:
    """Missing required receipt fields, for a fail-closed check.

    ``canonical`` maps a concept to its accepted key aliases. A concept with no
    present alias is a contract violation (the design forbids a bare
    ``success: true`` answer), so the caller fails the check.
    """
    missing: list[str] = []
    if not isinstance(payload, dict):
        return [f"{what}: response is not an object"]
    for concept, aliases in canonical.items():
        if not any(isinstance(payload, dict) and a in payload for a in aliases):
            if pick(payload, *aliases) is None:
                missing.append(f"{what}.{concept} (any of {list(aliases)})")
    return missing


def receipt_contract_missing(payload: dict) -> list[str]:
    """Design §13: which rows changed + statuses + source + dedupe flag."""
    return require_fields(
        payload,
        {
            "old_memory_id": RECEIPT_ID_KEYS["old"],
            "new_memory_id": RECEIPT_ID_KEYS["new"],
            "relation": ("relation", "relation_type", "edge", "edge_type"),
            "old_status": ("old_status", "from_status", "superseded_status"),
            "new_status": ("new_status", "to_status", "replacement_status"),
            "correction_source": (
                "correction_source", "correction_source_id", "source", "provenance",
            ),
            "deduplicated": ("deduplicated", "dedup", "idempotent", "already_applied"),
        },
        what="receipt",
    )


def scrub(text: str, secret: str) -> str:
    """Remove the secret from any text before it is written to the report."""
    if not secret:
        return text
    return text.replace(secret, "***REDACTED***")


def truncate(text: str, limit: int = MAX_RECORDED_CHARS) -> tuple[str, bool]:
    if text is None:
        return "", False
    text = str(text)
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"...[truncated {len(text) - limit} chars]", True


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ── HTTP against the REAL serve process ─────────────────────────────────────


def http_call(url: str, payload: dict | None = None, timeout: float = HTTP_TIMEOUT_S):
    """Returns (status, body_text, parsed_or_None). Never raises."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body, _try_json(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body, _try_json(body)
    except Exception as exc:  # connection refused during startup, etc.
        return 0, f"{type(exc).__name__}: {exc}", None


def _try_json(body: str):
    try:
        return json.loads(body)
    except Exception:
        return None


def wait_http(url: str, seconds: float = READY_TIMEOUT_S):
    """Bounded readiness poll on the real /health route (no unbounded busy loop)."""
    deadline = time.monotonic() + seconds
    last = "not attempted"
    while time.monotonic() < deadline:
        status, body, parsed = http_call(url, timeout=5.0)
        if status == 200 and isinstance(parsed, dict):
            return parsed
        last = f"status={status} body={body[:200]}"
        time.sleep(0.5)
    return {"_unavailable": last}


def _args_out() -> Path | None:
    """The --out path, best-effort, so the crash handler can still write evidence."""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--out" and i + 1 < len(argv):
            try:
                return Path(argv[i + 1]).resolve()
            except Exception:
                return None
        if a.startswith("--out="):
            try:
                return Path(a.split("=", 1)[1]).resolve()
            except Exception:
                return None
    return None


def write_crash_report(exc: BaseException, out_path: Path | None) -> None:
    """A driver that dies without leaving evidence is indistinguishable from a
    driver that was never run. Any unexpected exception still produces a FAIL
    report carrying the bounded traceback."""
    import traceback
    payload = {
        "verdict": "FAIL",
        "crash": {
            "type": type(exc).__name__,
            # The REAL secret is scrubbed here — not "". A traceback can echo a
            # DSN or a config value, and a crash report is still evidence.
            "message": scrub(str(exc)[:2000], _ACTIVE_SECRET),
            "traceback": scrub(traceback.format_exc()[-4000:], _ACTIVE_SECRET),
        },
        "note": ("the driver aborted before finish() ran — every check after the "
                 "point of failure was NOT executed"),
    }
    log("CRASH", payload["crash"]["type"])
    if out_path is None:
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
        return
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                            encoding="utf-8")
    except Exception:
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)


class Server:
    """One owned `python -m v3core serve` process; always cleaned up."""

    def __init__(self, label: str, python: str, env: dict, cwd: Path, port: int,
                 log_path: Path, profile: str = "m01m02e2e") -> None:
        self.label = label
        self.port = port
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self._handle = None
        self._argv = [
            python, "-B", "-m", "v3core", "serve",
            "--host", "127.0.0.1", "--port", str(port),
            "--profile", profile, "--ready-json",
        ]
        self._env = env
        self._cwd = cwd

    def start(self) -> dict:
        self._handle = self.log_path.open("wb")
        self.proc = subprocess.Popen(
            self._argv, env=self._env, cwd=str(self._cwd),
            stdout=self._handle, stderr=subprocess.STDOUT,
        )
        health = wait_http(f"http://127.0.0.1:{self.port}/health")
        return {
            "label": self.label,
            "port": self.port,
            "pid": self.proc.pid,
            "argv_profile": self._argv[-3],
            "health": {k: health.get(k) for k in ("ok", "pg", "embed", "core")},
        }

    def stop(self) -> dict:
        """Graceful SIGTERM/terminate, then a hard kill; always closes the log.

        The handle is retained when the process is still alive, so
        :meth:`alive` and the final residual-process check observe the REAL
        termination state. Clearing ``self.proc`` unconditionally would make an
        unkillable server look exactly like a cleanly stopped one.
        """
        info: dict = {"label": self.label, "port": self.port}
        if self.proc is None:
            info["stopped"] = "never-started"
            return info
        info["pid"] = self.proc.pid
        try:
            self.proc.terminate()
        except Exception as exc:
            info["terminate_error"] = repr(exc)
        try:
            info["returncode"] = self.proc.wait(timeout=20)
            info["stopped"] = "graceful"
        except Exception:
            try:
                self.proc.kill()
                info["returncode"] = self.proc.wait(timeout=20)
                info["stopped"] = "killed"
            except Exception as exc:  # pragma: no cover - defensive
                info["stopped"] = f"unstoppable: {exc!r}"
        if self._handle is not None:
            try:
                self._handle.close()
            except Exception:
                pass
            self._handle = None
        # Truthful residual: a still-running process keeps its handle so the
        # caller (and the end-of-run residual check) sees the failure.
        still_alive = self.proc.poll() is None
        info["alive_after_stop"] = still_alive
        if still_alive:
            info["cleanup_failed"] = True
        else:
            self.proc = None
        return info

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


OWNED_SERVERS: list[Server] = []


# ── embedded derived probes (marked derived_only in the report) ──────────────

DERIVED_PROBE_SOURCE = r'''
"""M01/M02 derived writer-layer probe (NOT the public tool surface).

Run as its own process so it observes exactly the isolated environment the
serve processes get, and so the driver process never imports v3core.

Covers only what needs a canonical injected stub:
  * authority: a correction without authority / with a non-user authority is refused
  * no fabricated source: an unknown correction_source_id is refused
  * embedding failure after commit: the correction stays durable
"""
from __future__ import annotations

import json
import os
import sys

import psycopg2

from v3core.active_memory_store import ActiveMemoryWriter
from v3core.pg_pool import PgPool


def _cfg() -> dict:
    return {
        "storage": {
            "pg": {
                "host": os.environ["M01M02_PG_HOST"],
                "port": int(os.environ["M01M02_PG_PORT"]),
                "database": os.environ["M01M02_PG_DB"],
                "user": os.environ["M01M02_PG_USER"],
                "password": os.environ["PGPASSWORD"],
            },
            "embed": {"endpoint": "", "model": "", "dim": 1024},
        }
    }


def _connect():
    return psycopg2.connect(
        host=os.environ["M01M02_PG_HOST"],
        port=int(os.environ["M01M02_PG_PORT"]),
        dbname=os.environ["M01M02_PG_DB"],
        user=os.environ["M01M02_PG_USER"],
        password=os.environ["PGPASSWORD"],
        connect_timeout=10,
    )


def _ok(value) -> bool:
    if isinstance(value, dict):
        if value.get("success") is False or value.get("durable") is False:
            return False
        status = str(value.get("status") or "")
        if status in ("DURABLE_FAILED", "FAILED", "REJECTED"):
            return False
        return True
    if hasattr(value, "success"):
        return bool(value.success)
    return bool(value)


def _err(value) -> str:
    if isinstance(value, dict):
        return str(value.get("error") or value.get("reason") or value.get("code") or "")
    for attr in ("error", "reason", "code"):
        got = getattr(value, attr, None)
        if got:
            return str(got)
    return ""


def _code(value) -> str:
    """The machine-readable refusal code, if the receipt exposes one."""
    if isinstance(value, dict):
        return str(value.get("error_code") or value.get("code")
                   or value.get("status") or "")
    for attr in ("error_code", "code", "status"):
        got = getattr(value, attr, None)
        if got:
            return str(got)
    return ""


#: Refusal codes that specifically mean "this correction_source_id does not
#: resolve to a real user message". A generic not_ok would also be satisfied
#: by a permission rejection, a missing target, or a missing table, none of
#: which prove the source check ran.
SOURCE_REJECTION_CODES = ("INVALID_CORRECTION_SOURCE",)


def _source_specific(value) -> tuple:
    """(refused, source_specific) for a fabricated correction_source_id."""
    refused = not _ok(value)
    blob = (json.dumps(value, ensure_ascii=False, default=str) if
            isinstance(value, (dict, list)) else repr(value))
    code = _code(value).upper()
    specific = (
        any(marker in code for marker in SOURCE_REJECTION_CODES)
        or any(marker in blob.upper() for marker in SOURCE_REJECTION_CODES)
        or ("conversation_stream" in blob and "user" in blob)
    )
    return refused, specific


def main() -> int:
    out: dict = {"derived_only": True, "probes": {}}
    cfg = _cfg()
    pool = PgPool(connect=_connect, max_connections=6, min_connections=0,
                  connect_timeout=10)

    def writer(embedder=None, embed_cfg=None):
        kwargs = {"pool": pool, "config": cfg}
        if embedder is not None:
            kwargs["embedder"] = embedder
        if embed_cfg is not None:
            kwargs["embed_cfg"] = embed_cfg
        return ActiveMemoryWriter(**kwargs)

    # 1. authority is required and must be exactly user_explicit
    no_auth = writer().correct(
        os.environ["M01M02_TARGET"],
        "derived-probe replacement without authority",
        correction_reason="derived probe: missing authority",
    )
    out["probes"]["no_authority_refused"] = {
        "ok": not _ok(no_auth), "error": _err(no_auth)[:200],
    }
    inferred = writer().correct(
        os.environ["M01M02_TARGET"],
        "derived-probe replacement by inference",
        correction_reason="derived probe: inference authority",
        authority="model_inference",
    )
    out["probes"]["inference_authority_refused"] = {
        "ok": not _ok(inferred), "error": _err(inferred)[:200],
    }
    legacy = writer().correct(
        os.environ["M01M02_TARGET"],
        "derived-probe replacement by passive write",
        correction_reason="derived probe: passive authority",
        authority=None,
    )
    out["probes"]["none_authority_refused"] = {
        "ok": not _ok(legacy), "error": _err(legacy)[:200],
    }

    # 2. no fabricated source: an unknown correction_source_id is refused, and
    #    refused FOR THAT REASON. A correct() call without the required
    #    authority would be refused for permissions first and prove nothing
    #    about source validation, so this probe passes authority explicitly.
    fake_source = writer().correct(
        os.environ["M01M02_TARGET"],
        "derived-probe replacement with a fabricated source",
        correction_reason="derived probe: fabricated source",
        correction_source_id="999999999",
        authority="user_explicit",
    )
    refused, source_specific = _source_specific(fake_source)
    out["probes"]["unknown_source_refused"] = {
        "ok": refused and source_specific,
        "error": _err(fake_source)[:200],
        "code": _code(fake_source)[:120],
        "refused": refused,
        "source_specific": source_specific,
    }

    # 3. embedding failure must not revert an already-durable correction.
    #    authority is explicit here too: this probe asserts the DURABILITY of a
    #    committed correction, which requires the correction to actually be
    #    admitted, not to be bounced by the permission check.
    def _boom(_text, _cfg):
        raise RuntimeError("m01m02-e2e injected embedding failure")

    embed_res = writer(embedder=_boom, embed_cfg={"model": "m01m02-e2e-stub"}).correct(
        os.environ["M01M02_TARGET"],
        os.environ["M01M02_EMBED_REPLACEMENT"],
        correction_reason="derived probe: embedding failure must not revert",
        authority="user_explicit",
    )
    warnings: list = []
    if isinstance(embed_res, dict):
        warnings = list(embed_res.get("warnings") or [])
    else:
        warnings = list(getattr(embed_res, "warnings", []) or [])
    out["probes"]["embedding_failure_stays_durable"] = {
        "ok": _ok(embed_res),
        "error": _err(embed_res)[:200],
        "warnings": [str(w)[:200] for w in warnings][:6],
        "warning_count": len(warnings),
    }

    # 4. unknown target is a truthful not-found
    unknown = writer().correct(
        "mem_definitely_not_present_m01m02e2e",
        "derived-probe replacement of an unknown target",
        correction_reason="derived probe: unknown target",
        authority="user_explicit",
    )
    out["probes"]["unknown_target_refused"] = {
        "ok": not _ok(unknown), "error": _err(unknown)[:200],
    }

    try:
        pool.shutdown()
    except Exception:
        pass
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# ── driver ──────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="fresh, empty run root")
    parser.add_argument("--source-root", required=True, help="M01/M02 source checkout root")
    parser.add_argument("--env-python", required=True, help="isolated venv python.exe")
    parser.add_argument("--secrets", required=True, help="PG password file (read once, env only)")
    parser.add_argument("--db-name", required=True, help="must start with m01m02e2e_")
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=55432)
    parser.add_argument("--pg-user", default="f2e2e")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    source = Path(args.source_root).resolve()
    out_path = Path(args.out).resolve()
    python = str(Path(args.env_python).resolve())
    db_name = (args.db_name or "").strip()

    report: dict = {
        "scope": (
            "M01/M02 memory correction; real public HTTP tool surface (v3_store / v3_update "
            "correct / v3_get / v3_search / /prefetch) on disposable PostgreSQL; keyword-only "
            "isolated profile; no LLM, no embedding endpoint, no paid model"
        ),
        "design": "docs/M01-M02-MEMORY-CORRECTION-DESIGN.md (locked, base main 89e50a5)",
        "root": str(root),
        "checks": {},
        "failures": [],
        "coverage_gaps": [],
        "raw": {"tool_calls": [], "cli": [], "prefetch": [], "http": []},
        "derived_only": {},
        "cleanup": {},
    }
    reasons: list[str] = []
    secret_value = ""  # bound now; the gate below assigns it from the secrets file

    def check(name: str, ok: bool, detail: str = "") -> bool:
        report["checks"][name] = {"ok": bool(ok), "observed": detail}
        if not ok:
            reasons.append(f"{name}: {detail}")
            report["failures"].append({"check": name, "observed": detail})
        log(f"CHECK {'PASS' if ok else 'FAIL'}", name)
        return bool(ok)

    def gap(text: str) -> None:
        report["coverage_gaps"].append(text)

    def write_report() -> None:
        report["reasons"] = reasons
        report["verdict"] = "PASS" if not reasons else "FAIL"
        report["check_count"] = len(report["checks"])
        payload = json.dumps(report, indent=2, ensure_ascii=False)
        if secret_value:
            payload = scrub(payload, secret_value)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
        log("CHECKS", report["check_count"])
        log("FAILURES", len(reasons))
        log("VERDICT", report["verdict"])

    def finish(exit_code: int = 0) -> int:
        write_report()
        return exit_code if not reasons else 1

    def refuse(label: str, reason: str, code: int) -> int:
        """A gate refusal still produces a report: a refusal with no evidence is
        indistinguishable from a driver that never ran."""
        print(f"REFUSE: {label}: {reason}", file=sys.stderr)
        report["refused"] = {"stage": label, "reason": reason}
        report["verdict"] = "REFUSED"
        report["reasons"] = reasons
        report["check_count"] = len(report["checks"])
        report["coverage_gaps"].append(
            f"run refused at the {label} gate — no check below was executed")
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                encoding="utf-8")
        except Exception:
            pass
        log("VERDICT", "REFUSED")
        return code

    # ── 0. fail-closed gates (nothing is created before they all pass) ───────
    machine = (os.environ.get("COMPUTERNAME") or socket.gethostname() or "").strip().upper()
    log("HOST", machine)
    report["host"] = machine
    # The interpreter is checked BEFORE anything is created: a missing or
    # non-executable --env-python must refuse like any other unmet
    # precondition, and it must not be discovered only after the isolated
    # roots already exist on disk.
    if not Path(python).is_file():
        return refuse("env_python",
                      f"--env-python {python} is not an existing file", 98)
    for label, reason, code in (
        ("hostname", gate_hostname(machine), 91),
        ("pg_target", gate_pg_target(args.pg_host, args.pg_port, db_name), 92),
        ("run_root", gate_root(root, must_exist=True), 94),
        ("run_root_location", gate_run_root_location(root, out_path), 99),
    ):
        if reason:
            return refuse(label, reason, code)

    if not Path(args.secrets).is_file():
        return refuse("secrets", f"secrets file {args.secrets} is missing", 95)
    secret_value = Path(args.secrets).read_text(encoding="utf-8").strip()
    if not secret_value:
        return refuse("secrets", "secrets file is empty", 96)
    # Bind the crash handler's scrub target before any DB work: an unexpected
    # exception from here on must not write the credential into a FAIL report.
    global _ACTIVE_SECRET
    _ACTIVE_SECRET = secret_value
    report["credentials"] = {
        "keys_loaded": ["PGPASSWORD", "V3CORE_PG_PASSWORD"],
        "in_argv": False,
        "in_config": False,
        "printed": False,
    }

    # secondary disposable databases derived from the same prefixed name
    db_upgrade = f"{db_name}_upgrade"
    db_nomig = f"{db_name}_nomig"
    # The minimal (explicit_memories-only) baseline gets its OWN database, so the
    # realistic baseline in db_upgrade is never contaminated by it.
    db_upgrade_min = f"{db_name}_upgrademin"
    for extra in (db_upgrade, db_nomig, db_upgrade_min):
        reason = gate_pg_target(args.pg_host, args.pg_port, extra)
        if reason:
            return refuse("derived_database", f"{extra}: {reason}", 93)

    # ── 1. isolated roots + environment ─────────────────────────────────────
    home = root / "home"
    project = root / "project"
    runs = root / "runs"
    profiles = root / "profiles"
    data_dir = root / "data"
    for d in (home, project, runs, profiles, data_dir):
        d.mkdir(parents=True, exist_ok=True)

    # The isolated dotenv must EXIST (a dangling explicit V3CORE_DOTENV is a
    # fail-closed refusal in the product) and be EMPTY (no ambient credential
    # may be imported). Created here, before any server is launched and before
    # any DB connect.
    try:
        isolated_dotenv = ensure_isolated_dotenv(home)
    except OSError as exc:
        return refuse("isolated_dotenv",
                      f"isolated dotenv could not be created under {home}: "
                      f"{type(exc).__name__}: {exc}", 90)
    dotenv_reason = gate_isolated_dotenv(isolated_dotenv)
    if dotenv_reason:
        return refuse("isolated_dotenv", dotenv_reason, 90)
    dotenv_size = isolated_dotenv.stat().st_size if isolated_dotenv.is_file() else -1

    env = sanitize_env(os.environ)
    report["env_sanitized"] = {
        "ambient_credentials_inherited": False,
        "ambient_endpoints_inherited": False,
        "production_bypass_inherited": False,
        "password_runtime_env_only": True,
        # A real, EMPTY file inside the isolated home: the explicit path
        # satisfies the product's fail-closed contract, emptiness guarantees no
        # ambient credential/endpoint can be imported from it.
        "isolated_dotenv_path": str(isolated_dotenv),
        "isolated_dotenv_exists": isolated_dotenv.is_file(),
        "isolated_dotenv_size_bytes": dotenv_size,
        "isolated_dotenv_is_empty": dotenv_size == 0,
    }
    env.update({
        "PGPASSWORD": secret_value,
        "V3CORE_PG_PASSWORD": secret_value,
        # every ambient root is redirected; the isolated config is the only source
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": os.environ.get("SystemDrive", "C:"),
        "HOMEPATH": "\\" + str(home).split("\\", 1)[-1] if "\\" in str(home) else "",
        "V3CORE_HOME": str(home),
        "HERMES_HOME": str(home),
        "V3CORE_TEST_MODE": "1",
        "V3CORE_DOTENV": str(isolated_dotenv),
        "PYTHONPATH": "",
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    })
    report["roots"] = {
        "home": str(home),
        "project": str(project),
        "data": str(data_dir),
        "profiles": str(profiles),
        "isolated_from_production": True,
    }

    def write_config(tag: str, database: str) -> Path:
        """Keyword-only profile: embed/rerank/llm empty, observer + e1 disabled.

        No credential is written: the PG password arrives through
        ``V3CORE_PG_PASSWORD`` (documented public-alpha env contract).
        """
        pdir = profiles / tag
        pdir.mkdir(parents=True, exist_ok=True)
        pdata = data_dir / tag
        pdata.mkdir(parents=True, exist_ok=True)
        body = (
            "basePath: {base}\n"
            "mode: cloud\n"
            "storage:\n"
            "  pg:\n"
            "    host: {host}\n"
            "    port: {port}\n"
            "    database: {db}\n"
            "    user: {user}\n"
            "  embed:\n"
            "    endpoint: ''\n"
            "    model: ''\n"
            "    dim: 1024\n"
            "    api_key: ''\n"
            "    proxy: ''\n"
            "  rerank:\n"
            "    endpoint: ''\n"
            "    model: ''\n"
            "    proxy: ''\n"
            "    timeout: 30\n"
            "    api_key: ''\n"
            "e1:\n  enabled: false\n"
            "observer:\n  enabled: false\n"
            "tkg:\n  enabled: false\n"
            "llm:\n  provider: ''\n  model: ''\n  api_key: ''\n  base_url: ''\n"
            "  thinking: true\n  max_tokens: 0\n"
        ).format(
            base=json.dumps(str(pdata)),
            host=args.pg_host,
            port=args.pg_port,
            db=database,
            user=args.pg_user,
        )
        cfg = pdir / "config.yaml"
        cfg.write_text(body, encoding="utf-8")
        return cfg

    cfg_main = write_config("main", db_name)
    cfg_nomig = write_config("nomig", db_nomig)
    env["V3CORE_CONFIG"] = str(cfg_main)
    # The isolated profile this driver writes has NO embed endpoint/model (see
    # write_config), so this run cannot build a vector recall block. The written
    # file is re-read so the fact is OBSERVED, not assumed.
    embedding_service_configured = embedding_service_configured_from_config(
        cfg_main.read_text(encoding="utf-8"))
    report["core_config"] = {
        "embedding_service_configured": embedding_service_configured,
        "path": str(cfg_main),
        "base_path_absolute": True,
        "embedding_mode": "keyword_only_disabled",
        "rerank_disabled": True,
        "llm_disabled": True,
        "observer_disabled": True,
        "e1_disabled": True,
        "credential_fields_empty": True,
        "credentials_in_file": False,
    }

    # ── 2. module identity: the venv must resolve the shipped source/wheel ──
    runner = Path(__file__).resolve()
    report["identity"] = {
        "runner": str(runner),
        "runner_sha256": sha256_file(runner),
        "python": sys.version.split()[0],
        "source_root": str(source),
    }
    # The interpreter is a hard prerequisite: a missing or non-executable path
    # must refuse like any other unmet precondition, never raise out of main().
    # (The existence half already ran above, before any root was created.)
    try:
        which = subprocess.run(
            [str(python), "-c",
             "import v3core, sys; print(v3core.__file__); print(sys.executable)"],
            env=env, cwd=str(project), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return refuse("env_python", f"--env-python could not be executed: "
                                    f"{type(exc).__name__}: {exc}", 98)
    lines = (which.stdout or "").strip().splitlines()
    resolved = lines[0].strip() if lines else ""
    report["identity"]["v3core_resolved"] = resolved
    report["identity"]["isolated_python"] = lines[1].strip() if len(lines) > 1 else ""
    # A NON-ZERO rc is a refusal, not "no artifact_reason": continuing would
    # connect to PostgreSQL and write rows for an interpreter that cannot even
    # import the artifact under test.
    if which.returncode != 0:
        check("module_resolves_shipped_source_or_wheel", False,
              f"import v3core failed: rc={which.returncode} "
              f"stdout={truncate(scrub(which.stdout or '', secret_value))[0]!r} "
              f"stderr={truncate(scrub(which.stderr or '', secret_value))[0]!r}")
        gap("the interpreter could not import v3core; refused before any "
            "psycopg2.connect — no database was created and no behavioural "
            "check ran")
        return finish()
    artifact_reason = gate_env_artifact(resolved, source, python)
    check("module_resolves_shipped_source_or_wheel",
          not artifact_reason,
          f"import v3core -> {resolved!r}"
          + (f" refused: {artifact_reason}" if artifact_reason else "")
          + f" (rc={which.returncode})")
    if artifact_reason:
        gap("v3core did not resolve to the artifact under test; every behavioural "
            "check below is recorded as unverified evidence for the wrong artifact")
        return finish()

    # Exact installed-artifact provenance, BEFORE any database connection: a
    # resolved file can sit inside the named venv and still be a stale wheel, so
    # the installed module bytes AND packaged SQL are compared against the
    # source root here.
    provenance = collect_artifact_provenance(python, env, project, source)
    report["artifact_provenance"] = provenance
    provenance_problems = artifact_provenance_mismatches(
        provenance.get("expected", {}), provenance.get("actual", {}),
        provenance.get("required", []))
    if not check("installed_artifact_matches_source_root", not provenance_problems,
                 f"mismatches={provenance_problems} "
                 f"package_dir={provenance.get('package_dir')!r} "
                 f"actual_keys={sorted(provenance.get('actual', {}))} "
                 f"error={provenance.get('error')!r}"):
        gap("the interpreter's installed artifact does not match the source root "
            "(stale/foreign wheel); refused before any psycopg2.connect")
        return finish()

    # The git HEAD is advisory only: sibling writers may still be committing on
    # this branch, so it is NOT an artifact claim for this run.
    try:
        head = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        report["identity"]["source_head_advisory"] = (head.stdout or "").strip() or "unknown"
    except Exception as exc:
        report["identity"]["source_head_advisory"] = f"unavailable: {type(exc).__name__}"
    report["identity"]["source_head_is_artifact_claim"] = False

    # ── 3. disposable databases (refuse any pre-existing name) ──────────────
    import psycopg2  # noqa: PLC0415 (after the env is isolated)

    created_dbs: list[str] = []

    def pg_connect(database: str):
        return psycopg2.connect(
            host=args.pg_host, port=args.pg_port, database=database,
            user=args.pg_user, password=secret_value, connect_timeout=10,
        )

    def pg_rows(database: str, sql: str, params: tuple = ()) -> list[tuple]:
        conn = pg_connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return []
                return cur.fetchall()
        finally:
            conn.close()

    def pg_exec(database: str, sql: str, params: tuple = ()) -> int:
        conn = pg_connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                count = cur.rowcount
            conn.commit()
            return count
        finally:
            conn.close()

    def create_database(name: str) -> bool:
        assert_disposable_target(name)
        admin = psycopg2.connect(
            host=args.pg_host, port=args.pg_port, database="postgres",
            user=args.pg_user, password=secret_value, connect_timeout=10,
        )
        try:
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
                if cur.fetchone() is not None:
                    report.setdefault("refusals", []).append(
                        f"database {name!r} already exists — refused (never reused/dropped)")
                    return False
                cur.execute(f'CREATE DATABASE "{name}"')
            created_dbs.append(name)
            return True
        finally:
            admin.close()

    for name in (db_name, db_upgrade, db_nomig, db_upgrade_min):
        created_fresh = create_database(name)
        if not check(f"database_created_fresh_{name}", created_fresh,
                     f"create_database({name}) -> {created_fresh} "
                     f"(CREATE DATABASE {name})"):
            return finish()
    report["databases"] = {
        "main": db_name,
        "upgrade": db_upgrade,
        "upgrade_minimal": db_upgrade_min,
        "nomigration": db_nomig,
        "prefix": DB_PREFIX,
        "port": args.pg_port,
    }

    def run_cli(argv: list[str], label: str, *, env_override: dict | None = None,
                timeout: int = 600) -> dict:
        proc = subprocess.run(
            [python, "-B", "-m", "v3core.distribution_cli", *argv],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env_override or env, cwd=str(project), timeout=timeout,
        )
        # Scrub the FULL streams first, then derive every recorded view from the
        # redacted text. Writing raw stdout/stderr to the run root and only
        # redacting the report would leave a secret-bearing file on disk, and
        # recording only a truncated tail would make a truncated stderr
        # indistinguishable from a complete one.
        out_full = scrub(proc.stdout or "", secret_value)
        err_full = scrub(proc.stderr or "", secret_value)
        (runs / f"{label}.stdout.json").write_text(out_full, encoding="utf-8")
        (runs / f"{label}.stderr.txt").write_text(err_full, encoding="utf-8")
        parsed = _try_json(out_full) or {"_unparsed": out_full[:2000]}
        if isinstance(parsed, dict):
            parsed["_rc"] = proc.returncode
            parsed["_stderr_tail"] = err_full[-400:]
        out_shown, out_cut = truncate(out_full)
        err_shown, err_cut = truncate(err_full)
        record = {
            "label": label,
            "argv": ["-m", "v3core.distribution_cli", *[scrub(a, secret_value) for a in argv]],
            "returncode": proc.returncode,
            "stdout": out_shown,
            "stderr": err_shown,
            "stdout_truncated": out_cut,
            "stderr_truncated": err_cut,
            "stdout_chars": len(out_full),
            "stderr_chars": len(err_full),
            "stdout_full_file": str(runs / f"{label}.stdout.json"),
            "stderr_full_file": str(runs / f"{label}.stderr.txt"),
            "parsed": truncate(scrub(json.dumps(parsed, ensure_ascii=False),
                                     secret_value))[0],
            "parsed_truncated": len(scrub(json.dumps(parsed, ensure_ascii=False),
                                          secret_value)) > MAX_RECORDED_CHARS,
        }
        report["raw"]["cli"].append(record)
        return parsed

    # ── 4. fresh bootstrap (public CLI, no password on argv) ────────────────
    dsn = dsn_without_password(args.pg_host, args.pg_port, db_name, args.pg_user)
    boot = run_cli(["bootstrap", "--target", dsn], "bootstrap-main")
    inner = boot.get("result") if isinstance(boot.get("result"), dict) else {}
    check("schema_bootstrapped",
          boot.get("_rc") == 0 and (inner.get("applied") is True or boot.get("applied") is True),
          f"rc={boot.get('_rc')} applied={inner.get('applied') or boot.get('applied')}")

    tables = {
        r[0] for r in pg_rows(db_name,
                              "SELECT table_name FROM information_schema.tables "
                              "WHERE table_schema='public'")
    }
    check("bootstrap_created_explicit_memories", "explicit_memories" in tables,
          f"public tables include explicit_memories={('explicit_memories' in tables)}")
    check("relations_table_present_after_bootstrap", RELATION_TABLE.split(".")[-1] in tables,
          f"{RELATION_TABLE} present={RELATION_TABLE.split('.')[-1] in tables}")
    if RELATION_TABLE.split(".")[-1] not in tables:
        gap("relations sidecar missing after fresh bootstrap — the M01 schema is not "
            "shipped in the packaged artifacts, so no correction check can run")
        return finish()

    rel_cols = {
        r[0] for r in pg_rows(db_name,
                              "SELECT column_name FROM information_schema.columns "
                              "WHERE table_schema='public' AND table_name=%s",
                              (RELATION_TABLE.split(".")[-1],))
    }
    missing_rel_cols = [c for c in RELATION_REQUIRED_COLUMNS if c not in rel_cols]
    report["relation_schema"] = {
        "table": RELATION_TABLE,
        "columns": sorted(rel_cols),
        "required_columns_present": [c for c in RELATION_REQUIRED_COLUMNS if c in rel_cols],
        "timestamp_column": next((c for c in RELATION_TIMESTAMP_COLUMNS if c in rel_cols), None),
        "request_column": next((c for c in RELATION_REQUEST_COLUMNS if c in rel_cols), None),
        "correction_id_column": next(
            (c for c in RELATION_CORRECTION_ID_COLUMNS if c in rel_cols), None),
    }
    check("relations_columns_complete", not missing_rel_cols,
          f"missing={missing_rel_cols} present={sorted(rel_cols)}")
    check("relations_has_recorded_at",
          report["relation_schema"]["timestamp_column"] is not None,
          f"one of {list(RELATION_TIMESTAMP_COLUMNS)} present="
          f"{report['relation_schema']['timestamp_column']}")

    ts_col = report["relation_schema"]["timestamp_column"] or "recorded_at"
    req_col = report["relation_schema"]["request_column"]
    corr_id_col = report["relation_schema"]["correction_id_column"]

    def relation_count(where: str = "", params: tuple = ()) -> int:
        """Rows in the relation sidecar, as a plain int (already unwrapped)."""
        sql = f"SELECT count(*) FROM {RELATION_TABLE}"
        if where:
            sql += f" WHERE {where}"
        return int(pg_rows(db_name, sql, params)[0][0])

    def memory_row(memory_id: str) -> dict | None:
        # Keys come from MEMORY_ROW_COLUMNS (the canonical names), NEVER from
        # splitting the SQL text: a ``provenance::text`` cast must not become a
        # dict key. The expressions preserve the exact query semantics.
        cols = ", ".join(expr for _, expr in MEMORY_ROW_COLUMNS)
        rows = pg_rows(db_name,
                       f"SELECT {cols} FROM public.explicit_memories WHERE memory_id=%s",
                       (memory_id,))
        if not rows:
            return None
        return dict(zip(MEMORY_ROW_KEYS, rows[0]))

    def memory_count() -> int:
        return int(pg_rows(db_name, "SELECT count(*) FROM public.explicit_memories")[0][0])

    def table_snapshot() -> dict:
        """Every explicit-memory row as ``{id: (content, status, updated_at)}``.

        Used to prove a scenario changed no PRE-EXISTING row or payload: new
        rows are allowed, an altered old one is not.
        """
        rows = pg_rows(db_name,
                       "SELECT memory_id, content, status, updated_at::text "
                       "FROM public.explicit_memories ORDER BY memory_id")
        return {str(r[0]): (r[1], r[2], r[3]) for r in rows}

    def edge_row(from_id: str) -> dict | None:
        rows = pg_rows(db_name,
                       f"SELECT from_memory_id, to_memory_id, relation_type, authority, "
                       f"correction_source_id, effective_at::text, {ts_col}::text"
                       + (f", {req_col}::text" if req_col else "")
                       + (f", {corr_id_col}" if corr_id_col else "")
                       + f" FROM {RELATION_TABLE} WHERE from_memory_id=%s",
                       (from_id,))
        if not rows:
            return None
        out = {
            "from_memory_id": rows[0][0], "to_memory_id": rows[0][1],
            "relation_type": rows[0][2], "authority": rows[0][3],
            "correction_source_id": rows[0][4], "effective_at": rows[0][5],
            ts_col: rows[0][6],
        }
        idx = 7
        if req_col:
            out[req_col] = rows[0][idx]
            idx += 1
        if corr_id_col:
            out[corr_id_col] = rows[0][idx]
        return out

    def raw_snapshot() -> dict:
        return {
            "conversation_stream": int(pg_rows(db_name, "SELECT count(*) FROM public.conversation_stream")[0][0]),
            "qa_pairs": int(pg_rows(db_name, "SELECT count(*) FROM public.qa_pairs")[0][0]),
        }

    # ── 5. real public tool surface: server A ───────────────────────────────
    port_a = free_port()
    server_a = Server("A", python, env, project, port_a, runs / "serve-a.log")
    OWNED_SERVERS.append(server_a)
    info_a = server_a.start()
    report["server_a"] = info_a
    if not check("server_a_health_ok", bool(info_a["health"].get("ok")) and bool(info_a["health"].get("pg")),
                 f"health={info_a['health']} port={port_a} pid={info_a.get('pid')}"):
        return finish()

    base_a = f"http://127.0.0.1:{port_a}"

    def http_raw(url: str, payload: dict | None, label: str, timeout: float = HTTP_TIMEOUT_S):
        status, body, parsed = http_call(url, payload, timeout=timeout)
        body_full = scrub(body, secret_value)
        req_full = "" if payload is None else scrub(
            json.dumps(payload, ensure_ascii=False), secret_value)
        body_s, cut = truncate(body_full)
        # The full redacted request/response pair is written to its own file: the
        # report keeps a truncated view (and says so), while the run root keeps
        # the complete payload so a reader can verify what actually came back.
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in label)[:60]
        raw_path = runs / f"http-{safe}.json"
        try:
            raw_path.write_text(json.dumps(
                {"label": label, "url": url, "http_status": status,
                 "request": payload, "response_body": body_full,
                 "response_json": parsed, "response_chars": len(body_full),
                 "response_truncated_in_report": cut},
                ensure_ascii=False, default=str), encoding="utf-8")
        except OSError:
            raw_path = None
        report["raw"]["http"].append({
            "label": label, "url": url,
            "request": req_full[:2000],
            "request_chars": len(req_full),
            "http_status": status, "body": body_s, "truncated": cut,
            "body_chars": len(body_full),
            "full_redacted_file": str(raw_path) if raw_path else None,
        })
        return status, parsed

    def tool(port: int, name: str, args: dict, label: str, timeout: float = HTTP_TIMEOUT_S) -> dict:
        """Call the real public /tool route and unwrap the documented envelope.

        Returns the TOOL payload: ``{"_http": status, "_ok": bool, ...decoded...}``
        plus the raw envelope under ``_envelope`` for the evidence record.
        """
        request = build_tool_request(name, args)
        status, envelope = http_raw(f"http://127.0.0.1:{port}/tool", request, label,
                                    timeout=timeout)
        out: dict = {"_http": status}
        if not isinstance(envelope, dict):
            out["_ok"] = False
            out["error"] = f"non-JSON /tool response (http {status})"
            report["raw"]["tool_calls"].append(
                {"label": label, "tool": name, "args": args, "http_status": status,
                 "tool_payload": None})
            return out
        out["_envelope"] = envelope
        inner = envelope.get("result")
        if isinstance(inner, str):
            decoded = _try_json(inner)
        elif isinstance(inner, dict):
            decoded = inner
        else:
            decoded = None
        if isinstance(decoded, dict):
            out.update(decoded)
            out["_ok"] = bool(envelope.get("ok")) and decoded.get("success") is not False
        else:
            out["_ok"] = False
            out["error"] = envelope.get("error") or f"undecodable tool result (http {status})"
            out["_raw_result"] = truncate(scrub(str(inner), secret_value))[0]
        out["_http"] = status
        report["raw"]["tool_calls"].append({
            "label": label,
            "tool": name,
            "args": scrub(json.dumps(args, ensure_ascii=False), secret_value),
            "args_chars": len(scrub(json.dumps(args, ensure_ascii=False), secret_value)),
            "http_status": status,
            "envelope_ok": envelope.get("ok"),
            "tool_payload": scrub(json.dumps(
                {k: v for k, v in out.items() if k != "_envelope"},
                ensure_ascii=False), secret_value),
        })
        return out

    def tool_ok(payload: dict) -> bool:
        return bool(payload.get("_ok")) and payload.get("success") is not False

    # 5a. discoverability of the public surface (record what it really is)
    status_root, root_body = http_raw(f"{base_a}/", None, "root")
    status_tools, tools_body = http_raw(f"{base_a}/tools", None, "tools")
    tools_public = status_tools == 200 and "v3_update" in (tools_body or "")
    check("public_root_advertises_tool_route",
          isinstance(root_body, dict) and any("POST /tool" in str(e) for e in
                                              (root_body.get("endpoints") or [])),
          f"GET / -> endpoints={root_body.get('endpoints') if isinstance(root_body, dict) else None}")
    report["tools_endpoint"] = {
        "status": status_tools,
        "public_tool_discovery": bool(tools_public),
        "body": truncate(scrub(json.dumps(tools_body, ensure_ascii=False), secret_value))[0],
    }
    if not tools_public:
        gap(
            "GET /tools is not a schema-discovery route on this build "
            f"(http {status_tools}); the correction tool contract is verified through the "
            "real POST /tool route plus the shipped TOOL_REGISTRY, not through a "
            "handler-only proxy"
        )

    # 5b. a real user message through the public bridge -> real source id
    marker = f"m01m02e2e-{db_name}-user-correction-request"
    ev = build_events_request(
        session_id=f"m01m02e2e-session-{db_name}", event_id="evt-user-1",
        host="m01m02e2e", role="user", content=marker, turn_id=1,
    )
    status_ev, ev_body = http_raw(f"{base_a}/events", ev, "events-user-message")
    check("bridge_accepted_user_message", status_ev in (200, 503) and bool(ev_body),
          f"POST /events -> http {status_ev} ack={ev_body}")
    src_rows = pg_rows(db_name,
                       "SELECT id, role, content FROM public.conversation_stream "
                       "WHERE content=%s", (marker,))
    if not check("real_source_message_persisted", len(src_rows) == 1, f"rows={src_rows}"):
        return finish()
    real_source_id = src_rows[0][0]
    report["real_correction_source"] = {
        "conversation_stream_id": real_source_id,
        "role": src_rows[0][1],
        "content_marker": marker,
    }
    check("correction_source_is_a_user_row", src_rows[0][1] == "user",
          f"conversation_stream.role={src_rows[0][1]!r} id={real_source_id}")

    # 5c. v3_store — canonical explicit memory A (real public write path)
    content_a = f"迁移项目部署方案采用 A。{marker}-A"
    store_a = tool(port_a, "v3_store",
                   {"category": "projects", "title": "迁移项目部署方案", "content": content_a,
                    "tags": ["m01m02e2e", "deploy"],
                    "source_id": f"m01m02e2e-mem-a-{db_name}"},
                   "store-a")
    if not check("v3_store_committed_a", tool_ok(store_a)
                 and store_a.get("durable") is True
                 and store_a.get("durable_store") == "explicit_memories",
                 f"receipt={truncate(scrub(json.dumps(store_a, ensure_ascii=False), secret_value))[0]}"):
        return finish()
    mem_a = str(store_a.get("source_id") or "")
    check("store_receipt_exposes_memory_id", bool(mem_a), f"source_id={mem_a!r}")
    row_a_before = memory_row(mem_a)
    if not check("memory_a_row_exists", row_a_before is not None, f"row={row_a_before}"):
        return finish()
    check("memory_a_starts_active", row_a_before["status"] == "active",
          f"status={row_a_before['status']}")

    def search_ids(port: int, query: str, label: str) -> list[str]:
        payload = tool(port, "v3_search", {"query": query, "limit": 10}, label)
        results = payload.get("results") or []
        return [str(r.get("source_id") or "") for r in results if isinstance(r, dict)]

    found_a = search_ids(port_a, "迁移项目部署方案", "search-a-before")
    check("search_finds_a_before_correction", mem_a in found_a, f"source_ids={found_a}")

    # 5d. v3_update action=correct — the whole point of M02
    content_b = f"迁移项目部署方案改用 B。{marker}-B"
    correct_args = {
        "action": "correct",
        "memory_id": mem_a,
        "replacement_content": content_b,
        "replacement_title": "迁移项目部署方案",
        "replacement_tags": ["m01m02e2e", "deploy"],
        "correction_reason": "用户明确改方案：方案 A 已取消",
        # The public schema declares correction_source_id as a STRING (the
        # conversation_stream.id rendered as text). Passing the raw psycopg2 int
        # would be rejected by argument validation, not by the source check.
        "correction_source_id": str(real_source_id),
        "effective_at": "2026-10-01T09:00:00+08:00",
    }
    correct_1 = tool(port_a, "v3_update", correct_args, "correct-a-to-b")
    missing_contract = receipt_contract_missing(correct_1)
    check("correct_receipt_exposes_what_changed", tool_ok(correct_1) and not missing_contract,
          f"missing={missing_contract} receipt="
          f"{truncate(scrub(json.dumps(correct_1, ensure_ascii=False), secret_value))[0]}")
    if not check("correct_a_to_b_succeeded", tool_ok(correct_1),
                 f"error={correct_1.get('error')} status={correct_1.get('status')}"):
        return finish()
    new_id = str(pick(correct_1, *RECEIPT_ID_KEYS["new"]) or "")
    old_id = str(pick(correct_1, *RECEIPT_ID_KEYS["old"]) or "")
    check("correct_receipt_old_id_is_a", old_id == mem_a, f"old={old_id!r} expected={mem_a!r}")
    check("correct_receipt_new_id_is_distinct", bool(new_id) and new_id != mem_a,
          f"new={new_id!r}")
    report["chain"] = {"A": mem_a, "B": new_id}
    if not check("correct_receipt_new_id_resolves_to_a_row",
                 new_id and memory_row(new_id) is not None, f"new={new_id!r}"):
        return finish()

    # 5e. independent SQL verification of the durable truth
    row_a_after = memory_row(mem_a)
    check("old_a_bytes_preserved",
          row_a_after["content"] == row_a_before["content"]
          and row_a_after["title"] == row_a_before["title"]
          and row_a_after["tags"] == row_a_before["tags"]
          and row_a_after["provenance"] == row_a_before["provenance"]
          and row_a_after["created_at"] == row_a_before["created_at"],
          f"before={row_a_before} after={row_a_after}")
    check("old_a_archived_not_current", row_a_after["status"] == "archived",
          f"status={row_a_after['status']}")
    row_b = memory_row(new_id)
    check("new_b_is_active_and_current",
          row_b is not None and row_b["status"] == "active" and row_b["content"] == content_b,
          f"row={row_b}")
    check("new_b_keeps_original_category", row_b is not None and row_b["category"] == row_a_before["category"],
          f"category={row_b['category'] if row_b else None}")
    edge = edge_row(mem_a)
    if not check("supersedes_edge_a_to_b", edge is not None and edge["to_memory_id"] == new_id,
                 f"edge={edge}"):
        return finish()
    check("edge_relation_type_supersedes", edge["relation_type"] == "supersedes",
          f"relation_type={edge['relation_type']!r} (the restored contract persists "
          f"'supersedes', never the retired 'corrects')")
    check("receipt_relation_type_is_supersedes",
          str(pick(correct_1, "relation_type") or "") == "supersedes",
          f"receipt relation_type={pick(correct_1, 'relation_type')!r}")
    check("edge_authority_user_explicit", edge["authority"] == "user_explicit",
          f"authority={edge['authority']!r}")
    check("edge_records_effective_at", bool(edge["effective_at"]),
          f"effective_at={edge['effective_at']!r}")
    check("edge_records_recorded_at", bool(edge.get(ts_col)), f"{ts_col}={edge.get(ts_col)!r}")
    # The receipt must carry the ACTUAL server recorded_at, and it must equal the
    # independently-read DB value. A receipt that omits it (or fabricates None)
    # fails here rather than passing on "some recorded_at-ish field exists".
    receipt_recorded_at = pick(correct_1, "recorded_at")
    check("receipt_recorded_at_is_present_and_db_equal",
          bool(receipt_recorded_at) and timestamps_equal(receipt_recorded_at, edge.get(ts_col)),
          f"receipt recorded_at={receipt_recorded_at!r} db {ts_col}={edge.get(ts_col)!r}")
    check("edge_stores_real_source_literally", str(edge["correction_source_id"] or "") == str(real_source_id),
          f"correction_source_id={edge['correction_source_id']!r} real={real_source_id!r}")
    report["edge_a_b"] = scrub(json.dumps(edge, ensure_ascii=False), secret_value)
    if req_col:
        check("edge_keeps_canonical_request_json", bool(edge.get(req_col)),
              f"{req_col} present={bool(edge.get(req_col))}")
    else:
        gap(f"no canonical request-JSON column on {RELATION_TABLE} "
            f"(looked for {list(RELATION_REQUEST_COLUMNS)}) — exact replay identity is "
            "compared through the tool receipt instead")

    # 5f. no fabricated source when the caller supplies none
    mem_nosrc = f"m01m02e2e-mem-nosrc-{db_name}"
    nosrc: dict = {}
    store_nosrc = tool(port_a, "v3_store",
                       {"category": "projects", "title": "无来源纠正基线",
                        "content": f"无来源纠正基线内容。{marker}-NOSRC-A",
                        "source_id": mem_nosrc}, "store-nosrc")
    if check("store_nosrc_committed", tool_ok(store_nosrc), f"receipt ok={store_nosrc.get('_ok')}"):
        nosrc = tool(port_a, "v3_update",
                     {"action": "correct", "memory_id": mem_nosrc,
                      "replacement_content": f"无来源纠正基线内容已改。{marker}-NOSRC-B",
                      "correction_reason": "用户明确改写，且不提供真实来源"},
                     "correct-nosrc")
        if check("correct_without_source_succeeded", tool_ok(nosrc),
                 f"error={nosrc.get('error')}"):
            nosrc_new = str(pick(nosrc, *RECEIPT_ID_KEYS["new"]) or "")
            nosrc_edge = edge_row(mem_nosrc)
            check("edge_source_id_null_without_caller_source",
                  nosrc_edge is not None and nosrc_edge["correction_source_id"] is None,
                  f"correction_source_id={nosrc_edge['correction_source_id'] if nosrc_edge else None!r}")
            src_claim = ""
            if nosrc_edge:
                src_claim = f"{nosrc_edge.get('authority') or ''} {pick(nosrc_edge, 'provenance', 'source') or ''}"
            row_nosrc_new = memory_row(nosrc_new) if nosrc_new else None
            prov_raw = (row_nosrc_new or {}).get("provenance") or ""
            prov_text = prov_raw if isinstance(prov_raw, str) else json.dumps(
                prov_raw, ensure_ascii=False)
            try:
                prov_obj = json.loads(prov_text) if prov_text else {}
            except Exception:
                prov_obj = {}
            prov_keys = set(prov_obj) if isinstance(prov_obj, dict) else set()
            check("no_source_is_labelled_explicit_tool_request",
                  "explicit_tool_request" in (str(prov_text) + src_claim),
                  f"provenance={prov_text!r}")
            check("no_fabricated_host_or_event_provenance",
                  not ({"host", "event_id", "session_id", "qa_id"} & prov_keys),
                  f"provenance keys={sorted(prov_keys)} (a fabricated host/event/QA "
                  "reference would show up here)")

    # 5g. raw / QA untouched by the correction
    raw_after = raw_snapshot()
    check("correction_left_raw_and_qa_untouched",
          raw_after["conversation_stream"] == 1 and raw_after["qa_pairs"] == 0,
          f"conversation_stream={raw_after['conversation_stream']} qa_pairs={raw_after['qa_pairs']}")

    # 5h. v3_get — current / history / hm
    #
    # Every canonical field below is read from the MODE-SELECTED nested row of
    # the SAME transport-decoded object (``memory`` for mode=current,
    # ``requested`` for mode=history) or from that object's own top-level chain
    # fields — never by a recursive key search, which finds ``requested``'s
    # value first and reports the superseded payload as the answer.
    cur = tool(port_a, "v3_get", {"target": "memory", "memory_id": mem_a, "mode": "current"},
               "get-current-from-a")
    cur_read = reader_current_read(cur)
    check("get_current_returns_replacement_not_old",
          cur_read["content"] == content_b and cur_read["memory_id"] == new_id,
          f"memory row content={cur_read['content']!r} id={cur_read['memory_id']!r} "
          f"expected content={content_b!r} id={new_id!r} "
          f"(the requested row {cur_read['requested_memory_id']!r} is "
          f"{cur_read['requested_status']!r} — its payload is NOT the answer)")
    # ``is_current`` is the product's documented semantic: whether the REQUESTED
    # id is itself the current version (``current_id == memory_id``). A read of
    # the archived A that resolves to B truthfully reports False here while the
    # resolved row IS the current one, so the RESOLUTION is what must be
    # asserted — never a fabricated True.
    check("get_current_reports_is_current_true",
          cur_read["current_status"] == "active"
          and bool(cur_read["resolved_memory_id"])
          and cur_read["resolved_memory_id"] == cur_read["memory_id"],
          f"current_status={cur_read['current_status']!r} "
          f"resolved_memory_id={cur_read['resolved_memory_id']!r} "
          f"memory_id={cur_read['memory_id']!r} is_current={cur_read['is_current']!r}")
    check("get_current_reports_requested_row_is_not_current",
          cur_read["is_current"] is False
          and cur_read["requested_memory_id"] == mem_a
          and cur_read["requested_status"] == "archived",
          f"is_current={cur_read['is_current']!r} (about the REQUESTED id) "
          f"requested={cur_read['requested_memory_id']!r} "
          f"status={cur_read['requested_status']!r}")
    check("get_current_echoes_requested_id", cur_read["requested_memory_id"] == mem_a,
          f"requested_memory_id={cur_read['requested_memory_id']!r} expected={mem_a!r}")
    check("get_current_resolves_to_terminal_version",
          cur_read["resolved_memory_id"] == new_id,
          f"resolved_memory_id={cur_read['resolved_memory_id']!r} expected={new_id!r}")
    hist = tool(port_a, "v3_get", {"target": "memory", "memory_id": mem_a, "mode": "history"},
                "get-history-from-a")
    hist_versions = reader_history_entries(hist)
    hist_ids = reader_history_ids(hist)
    hist_statuses = reader_history_statuses(hist)
    check("get_history_returns_full_version_chain", len(hist_versions) >= 2,
          f"ids={hist_ids} labels={reader_history_labels(hist)} "
          f"edges={reader_relation_chain(hist)}")
    if hist_versions:
        check("get_history_labels_archived_and_current",
              any("archived" in s for s in hist_statuses) and any("active" in s for s in hist_statuses),
              f"statuses={hist_statuses}")
        check("get_history_contains_old_and_new", mem_a in hist_ids and new_id in hist_ids,
              f"ids={hist_ids}")
        # the nested chain block itself must be real: the terminal current id,
        # the ordered edges and the emitted is_current flags.
        check("get_history_chain_edges_and_current_id_are_real",
              reader_history_current_memory_id(hist) == new_id
              and reader_relation_chain(hist) == [("supersedes", mem_a, new_id)]
              and reader_history_is_current(hist) == [False, True],
              f"current_memory_id={reader_history_current_memory_id(hist)!r} "
              f"edges={reader_relation_chain(hist)} "
              f"is_current={reader_history_is_current(hist)}")
    else:
        gap("history mode did not return a >=2-entry version list; chain order unverified")

    hm = tool(port_a, "v3_get", {"target": "hm", "source_id": mem_a}, "get-hm-from-a")
    hm_present, hm_content = reader_rendered_content(hm)
    check("hm_read_uses_current_by_default",
          hm_present and hm_content == content_b,
          f"flattened content present={hm_present} content={hm_content!r} "
          f"(must not answer with the superseded payload)")

    found_after = search_ids(port_a, "迁移项目部署方案", "search-after-correction")
    check("search_after_correction_returns_b", new_id in found_after, f"source_ids={found_after}")
    check("search_after_correction_drops_a", mem_a not in found_after,
          f"A still returned as current: {found_after}")

    # 5i. repeat the identical request — the blocker "重复纠正制造重复版本"
    mem_before_repeat = memory_count()
    rel_before_repeat = relation_count()
    repeat = tool(port_a, "v3_update", correct_args, "correct-repeat-identical")
    check("repeat_identical_request_succeeds", tool_ok(repeat),
          f"error={repeat.get('error')} dedup={repeat.get('deduplicated')}")
    check("repeat_identical_request_creates_no_memory", memory_count() == mem_before_repeat,
          f"explicit_memories {mem_before_repeat} -> {memory_count()}")
    check("repeat_identical_request_creates_no_relation", relation_count() == rel_before_repeat,
          f"{RELATION_TABLE} {rel_before_repeat} -> {relation_count()}")
    repeat_new = str(pick(repeat, *RECEIPT_ID_KEYS["new"]) or "")
    check("repeat_identical_request_returns_same_version", repeat_new == new_id,
          f"new={repeat_new!r} expected={new_id!r}")

    # 5j. a different second successor must conflict
    conflict_args = dict(correct_args)
    conflict_args["replacement_content"] = f"迁移项目部署方案改用 C。{marker}-C"
    conflict_args.pop("correction_source_id", None)
    conflict_args.pop("effective_at", None)
    conflict = tool(port_a, "v3_update", conflict_args, "correct-conflicting-second-successor")
    conflict_text = json.dumps(conflict, ensure_ascii=False)
    check("second_successor_conflict_refused",
          not tool_ok(conflict) and "CORRECTION_CONFLICT" in conflict_text.upper(),
          f"ok={conflict.get('_ok')} error={conflict.get('error')!r} "
          f"code={pick(conflict, 'code', 'error_code')}")
    check("conflict_names_the_current_successor", new_id in conflict_text,
          f"current successor {new_id!r} present in receipt={new_id in conflict_text}")
    check("conflict_created_no_second_successor", relation_count() == rel_before_repeat,
          f"{RELATION_TABLE} rows={relation_count()} (expected {rel_before_repeat})")
    active_successors = pg_rows(db_name,
                                f"SELECT count(*) FROM {RELATION_TABLE} WHERE from_memory_id=%s",
                                (mem_a,))[0][0]
    check("exactly_one_successor_edge", int(active_successors) == 1,
          f"edges from A={active_successors}")

    # 5k. B -> C full chain
    content_c = f"迁移项目部署方案改用 C。{marker}-C"
    correct_bc = tool(port_a, "v3_update",
                      {"action": "correct", "memory_id": new_id,
                       "replacement_content": content_c,
                       "correction_reason": "用户再次明确改方案：B 也已取消",
                       "correction_source_id": str(real_source_id),
                       "effective_at": "2026-10-02T09:00:00+08:00"},
                      "correct-b-to-c")
    id_c = str(pick(correct_bc, *RECEIPT_ID_KEYS["new"]) or "")
    if check("chain_b_to_c_succeeded", tool_ok(correct_bc) and bool(id_c),
             f"error={correct_bc.get('error')} new={id_c!r}"):
        report["chain"]["C"] = id_c
        check("chain_b_archived_c_active",
              (memory_row(new_id) or {}).get("status") == "archived"
              and (memory_row(id_c) or {}).get("status") == "active",
              f"B status={(memory_row(new_id) or {}).get('status')} "
              f"C status={(memory_row(id_c) or {}).get('status')}")
        edge_bc = edge_row(new_id)
        check("chain_edge_b_to_c", edge_bc is not None and edge_bc["to_memory_id"] == id_c,
              f"edge={edge_bc}")
        check("chain_has_no_extra_edge_from_a", relation_count("from_memory_id=%s", (mem_a,)) == 1,
              f"A keeps exactly one outgoing edge (chain, not fan-out): "
              f"{relation_count('from_memory_id=%s', (mem_a,))}")
        cur_c = tool(port_a, "v3_get", {"target": "memory", "memory_id": mem_a, "mode": "current"},
                     "get-current-after-chain")
        cur_c_read = reader_current_read(cur_c)
        check("get_current_follows_chain_to_c",
              cur_c_read["content"] == content_c
              and cur_c_read["memory_id"] == id_c
              and cur_c_read["resolved_memory_id"] == id_c,
              f"memory row content={cur_c_read['content']!r} id={cur_c_read['memory_id']!r} "
              f"resolved_memory_id={cur_c_read['resolved_memory_id']!r} "
              f"expected content={content_c!r} id={id_c!r}")

    # 5l. same content, two distinct ids -> targeting is by id, not by similarity
    dup_base = f"同内容双 id 场景。{marker}-DUP"
    id_d1 = f"m01m02e2e-dup1-{db_name}"
    id_d2 = f"m01m02e2e-dup2-{db_name}"
    d1 = tool(port_a, "v3_store",
              {"category": "projects", "title": "同内容双 id 1", "content": dup_base,
               "source_id": id_d1}, "store-dup-1")
    d2 = tool(port_a, "v3_store",
              {"category": "projects", "title": "同内容双 id 2", "content": dup_base,
               "source_id": id_d2}, "store-dup-2")
    if check("same_content_two_ids_both_committed", tool_ok(d1) and tool_ok(d2),
             f"ok1={d1.get('_ok')} ok2={d2.get('_ok')}"):
        d1_new = tool(port_a, "v3_update",
                      {"action": "correct", "memory_id": id_d1,
                       "replacement_content": f"同内容双 id 场景已改。{marker}-DUP-B",
                       "correction_reason": "只改 id1"},
                      "correct-dup-1")
        d1_target = str(pick(d1_new, *RECEIPT_ID_KEYS["new"]) or "")
        check("same_content_targeting_uses_explicit_id", tool_ok(d1_new) and d1_target != id_d2,
              f"new={d1_target!r}")
        check("same_content_sibling_untouched",
              (memory_row(id_d2) or {}).get("status") == "active"
              and (memory_row(id_d2) or {}).get("content") == dup_base,
              f"id2 status={(memory_row(id_d2) or {}).get('status')}")
        check("same_content_no_edge_created_for_sibling",
              edge_row(id_d2) is None, f"edge for id2 = {edge_row(id_d2)}")

    # 5m. unknown target: truthful not-found, and no new standalone memory
    before_unknown = memory_count()
    unknown = tool(port_a, "v3_update",
                   {"action": "correct", "memory_id": "mem_no_such_memory_m01m02e2e",
                    "replacement_content": f"不存在的目标。{marker}-UNKNOWN",
                    "correction_reason": "unknown target"},
                   "correct-unknown-target")
    check("unknown_target_refused", not tool_ok(unknown),
          f"ok={unknown.get('_ok')} error={unknown.get('error')!r}")
    check("unknown_target_is_truthful_not_found",
          "NOT_FOUND" in json.dumps(unknown, ensure_ascii=False).upper()
          or "not found" in json.dumps(unknown, ensure_ascii=False).lower()
          or unknown.get("success") is False,
          f"payload={truncate(scrub(json.dumps(unknown, ensure_ascii=False), secret_value))[0]}")
    check("unknown_target_creates_no_memory", memory_count() == before_unknown,
          f"explicit_memories {before_unknown} -> {memory_count()}")

    # 5n. an archived, unrelated target must be refused
    id_arch = f"m01m02e2e-arch-{db_name}"
    arch_store = tool(port_a, "v3_store",
                      {"category": "projects", "title": "无关的已归档记忆",
                       "content": f"无关的已归档记忆内容。{marker}-ARCH",
                       "source_id": id_arch}, "store-arch")
    if check("archived_target_fixture_committed", tool_ok(arch_store), f"ok={arch_store.get('_ok')}"):
        archived = tool(port_a, "v3_update", {"action": "delete", "source_id": id_arch},
                        "archive-unrelated")
        check("unrelated_target_archived", tool_ok(archived)
              or "already_archived" in json.dumps(archived, ensure_ascii=False),
              f"receipt={truncate(scrub(json.dumps(archived, ensure_ascii=False), secret_value))[0]}")
        arch_correct = tool(port_a, "v3_update",
                            {"action": "correct", "memory_id": id_arch,
                             "replacement_content": f"不该写入的替换。{marker}-ARCH-B",
                             "correction_reason": "correct an archived unrelated target"},
                            "correct-archived-target")
        check("archived_unrelated_target_refused", not tool_ok(arch_correct),
              f"ok={arch_correct.get('_ok')} error={arch_correct.get('error')!r}")
        check("archived_unrelated_target_created_no_edge", edge_row(id_arch) is None,
              f"edge={edge_row(id_arch)}")

    # 5o. an explicit correction source that does not exist is refused
    bad_source = tool(port_a, "v3_update",
                      {"action": "correct", "memory_id": mem_a,
                       "replacement_content": f"伪造来源的替换。{marker}-BADSRC",
                       "correction_reason": "fabricated source",
                       # A syntactically valid, definitely non-existent STRING id:
                       # the refusal must come from the source lookup, not from
                       # argument-shape validation.
                       "correction_source_id": BOGUS_SOURCE_ID},
                      "correct-bogus-source")
    check("fabricated_correction_source_refused", not tool_ok(bad_source),
          f"ok={bad_source.get('_ok')} error={bad_source.get('error')!r}")

    # 5p. rollback under a real, injected failure
    id_roll = f"m01m02e2e-roll-{db_name}"
    roll_store = tool(port_a, "v3_store",
                      {"category": "projects", "title": "回滚验证基线",
                       "content": f"回滚验证基线内容。{marker}-ROLL",
                       "source_id": id_roll}, "store-rollback")
    if check("rollback_fixture_committed", tool_ok(roll_store), f"ok={roll_store.get('_ok')}"):
        assert_disposable_target(db_name)
        pg_exec(db_name, """
            CREATE OR REPLACE FUNCTION m01m02e2e_block_edge() RETURNS trigger
            LANGUAGE plpgsql AS $fn$
            BEGIN
              RAISE EXCEPTION 'm01m02-e2e injected failure on relation insert';
            END
            $fn$;
        """)
        pg_exec(db_name, f"""
            CREATE TRIGGER m01m02e2e_block BEFORE INSERT ON {RELATION_TABLE}
            FOR EACH ROW EXECUTE FUNCTION m01m02e2e_block_edge();
        """)
        report["failure_injection"] = {
            "kind": "ephemeral BEFORE INSERT trigger on the relation sidecar",
            "database": db_name,
            "drops_destructive_ddl": False,
        }
        mem_before_roll = memory_count()
        rel_before_roll = relation_count()
        rolled = tool(port_a, "v3_update",
                      {"action": "correct", "memory_id": id_roll,
                       "replacement_content": f"回滚验证替换内容。{marker}-ROLL-B",
                       "correction_reason": "must roll back under the injected trigger"},
                      "correct-rollback-injected")
        check("injected_failure_reported_as_failure", not tool_ok(rolled),
              f"ok={rolled.get('_ok')} error={truncate(str(rolled.get('error')))[0]}")
        check("rollback_left_target_active", (memory_row(id_roll) or {}).get("status") == "active",
              f"status={(memory_row(id_roll) or {}).get('status')!r}")
        check("rollback_created_no_replacement", memory_count() == mem_before_roll,
              f"explicit_memories {mem_before_roll} -> {memory_count()}")
        check("rollback_created_no_relation", relation_count() == rel_before_roll,
              f"{RELATION_TABLE} {rel_before_roll} -> {relation_count()}")
        check("rollback_created_no_edge_from_target", edge_row(id_roll) is None,
              f"edge={edge_row(id_roll)}")
        assert_disposable_target(db_name)
        pg_exec(db_name, f"DROP TRIGGER IF EXISTS m01m02e2e_block ON {RELATION_TABLE}")
        pg_exec(db_name, "DROP FUNCTION IF EXISTS m01m02e2e_block_edge()")
        report["failure_injection"]["removed"] = True
        recovered = tool(port_a, "v3_update",
                         {"action": "correct", "memory_id": id_roll,
                          "replacement_content": f"回滚验证替换内容。{marker}-ROLL-B",
                          "correction_reason": "after the trigger is removed"},
                         "correct-after-injection-cleared")
        check("correction_succeeds_after_injection_cleared", tool_ok(recovered),
              f"error={recovered.get('error')}")

    # 5q. REAL concurrency: identical and divergent corrections at the same time
    def concurrent_case(name: str, payloads: list[dict]) -> dict:
        """Run N corrections truly concurrently and return RESOLVED payloads.

        The submitted objects are futures; returning them would make every
        truthiness test below meaningless (a Future is always truthy, and
        ``tool_ok(future)`` would read as a successful tool receipt). Each
        future is therefore resolved here, and a raised exception is recorded
        as an explicit failure payload rather than being swallowed or mistaken
        for a refusal the product produced.
        """
        barrier = threading.Barrier(len(payloads))

        def one(args: dict) -> dict:
            barrier.wait(timeout=30)
            return tool(port_a, "v3_update", args, f"{name}-thread", timeout=HTTP_TIMEOUT_S)

        out: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(payloads)) as pool:
            futures = [pool.submit(one, a) for a in payloads]
            for i, fut in enumerate(futures):
                try:
                    out[f"t{i}"] = fut.result(timeout=HTTP_TIMEOUT_S + 30)
                except BaseException as exc:  # noqa: BLE001 — recorded, not hidden
                    out[f"t{i}"] = {
                        "_http": 0, "_ok": False, "_thread_error": repr(exc),
                        "error": f"concurrent worker raised {type(exc).__name__}: {exc}",
                    }
        return out

    id_conc = f"m01m02e2e-conc-{db_name}"
    conc_store = tool(port_a, "v3_store",
                      {"category": "projects", "title": "并发纠正基线",
                       "content": f"并发纠正基线内容。{marker}-CONC",
                       "source_id": id_conc}, "store-concurrency")
    if check("concurrency_fixture_committed", tool_ok(conc_store), f"ok={conc_store.get('_ok')}"):
        identical_payload = {
            "action": "correct", "memory_id": id_conc,
            "replacement_content": f"并发纠正替换内容。{marker}-CONC-B",
            "correction_reason": "concurrent identical correction",
        }
        mem_before_conc = memory_count()
        rel_before_conc = relation_count()
        results = concurrent_case("conc-identical", [dict(identical_payload)] * 3)
        oks = sum(1 for r in results.values() if tool_ok(r))
        new_ids = {str(pick(r, *RECEIPT_ID_KEYS["new"]) or "") for r in results.values()
                   if tool_ok(r)}
        # All three must succeed: the first commits and the other two must take
        # the idempotent replay path. ">=1 succeeded" would pass even if two
        # threads were refused or crashed, which is exactly the failure mode the
        # concurrency probe exists to catch.
        per_thread = {k: [r.get("_ok"), r.get("error"), r.get("_thread_error")]
                      for k, r in results.items()}
        check("concurrent_identical_all_three_succeed", oks == 3,
              f"successes={oks}/3 required=3; per-thread ok/error={per_thread}")
        distinct = {i for i in new_ids if i}
        check("concurrent_identical_dedupe_to_one_version", len(distinct) == 1,
              f"distinct new ids={sorted(distinct)} (exactly one version expected)")
        dedup_flags = {bool(r.get("deduplicated")) for r in results.values()
                       if tool_ok(r)}
        check("concurrent_identical_replay_flag_is_visible", True in dedup_flags,
              f"deduplicated flags={sorted(dedup_flags)} (the losers must report "
              f"an idempotent replay, not a fresh commit)")
        check("concurrent_identical_created_exactly_one_memory", memory_count() == mem_before_conc + 1,
              f"explicit_memories {mem_before_conc} -> {memory_count()} (expected +1)")
        check("concurrent_identical_created_exactly_one_relation",
              relation_count() == rel_before_conc + 1,
              f"{RELATION_TABLE} {rel_before_conc} -> {relation_count()} (expected +1)")
        report["concurrency"] = {
            "identical": {"successes": oks, "new_ids": sorted(distinct),
                          "deduplicated_flags": [bool(r.get("deduplicated"))
                                                 for r in results.values()],
                          "memory_delta": memory_count() - mem_before_conc,
                          "relation_delta": relation_count() - rel_before_conc},
        }

        id_conc2 = f"m01m02e2e-conc2-{db_name}"
        conc2 = tool(port_a, "v3_store",
                     {"category": "projects", "title": "并发分歧纠正基线",
                      "content": f"并发分歧纠正基线内容。{marker}-CONC2",
                      "source_id": id_conc2}, "store-concurrency-2")
        if check("divergent_concurrency_fixture_committed", tool_ok(conc2),
                 f"ok={conc2.get('_ok')}"):
            mem_before2 = memory_count()
            rel_before2 = relation_count()
            divergent = [
                {"action": "correct", "memory_id": id_conc2,
                 "replacement_content": f"并发分歧替换 X。{marker}-CONC2-X",
                 "correction_reason": "concurrent divergent correction X"},
                {"action": "correct", "memory_id": id_conc2,
                 "replacement_content": f"并发分歧替换 Y。{marker}-CONC2-Y",
                 "correction_reason": "concurrent divergent correction Y"},
            ]
            results2 = concurrent_case("conc-divergent", divergent)
            oks2 = sum(1 for r in results2.values() if tool_ok(r))
            refused2 = sum(1 for r in results2.values() if not tool_ok(r))
            new2 = {str(pick(r, *RECEIPT_ID_KEYS["new"]) or "") for r in results2.values()
                    if tool_ok(r)}
            report["concurrency"]["divergent"] = {
                "successes": oks2, "refused": refused2, "new_ids": sorted(new2),
                "memory_delta": memory_count() - mem_before2,
                "relation_delta": relation_count() - rel_before2,
            }
            check("concurrent_divergent_exactly_one_winner", oks2 == 1 and refused2 == 1,
                  f"successes={oks2} refused={refused2}")
            check("concurrent_divergent_single_successor", len(new2) == 1,
                  f"new ids={sorted(new2)}")
            check("concurrent_divergent_created_one_relation",
                  relation_count() == rel_before2 + 1,
                  f"{RELATION_TABLE} {rel_before2} -> {relation_count()} (expected +1)")

    # 5r. DEDICATED withdraw scenario (restored M01/M02 contract).
    #
    # An isolated A' -> B' -> C' -> withdraw chain on its OWN target marker, so
    # every count assertion above and the fresh-process recall below is
    # untouched. This is the contract the original mission requires and the
    # earlier design omitted: withdraw, exact retry, conflict, no-current reads
    # and a full ordered history. The public HTTP surface is exercised through
    # the real tool helper only.
    wd_marker = f"m01m02e2e-{db_name}-withdraw-dedicated"
    wd_a = f"m01m02e2e-wd-a-{db_name}"
    content_wd_a = f"撤回专用场景基线内容 A。{wd_marker}-A"
    content_wd_b = f"撤回专用场景替换内容 B。{wd_marker}-B"
    content_wd_c = f"撤回专用场景替换内容 C。{wd_marker}-C"
    content_wd_conflict = f"撤回专用场景冲突替换。{wd_marker}-CONFLICT"

    wd_pre_rows = table_snapshot()
    wd_pre_raw = raw_snapshot()
    wd_pre_mem = memory_count()
    wd_pre_rel = relation_count()

    wd_store = tool(port_a, "v3_store",
                    {"category": "projects", "title": "撤回专用场景基线",
                     "content": content_wd_a, "source_id": wd_a},
                    "withdraw-dedicated-store-a")
    if not check("dedicated_scenario_target_committed", tool_ok(wd_store),
                 f"ok={wd_store.get('_ok')} error={wd_store.get('error')!r}"):
        gap("the dedicated withdraw-scenario target could not be committed; the "
            "restored withdraw contract is unverified")
        return finish()

    wd_correct_ab_args = {
        "action": "correct",
        "memory_id": wd_a,
        "mode": "replace",
        "replacement_content": content_wd_b,
        "replacement_title": "撤回专用场景基线",
        "replacement_tags": ["m01m02e2e", "withdraw"],
        "correction_reason": "撤回专用场景：A 改为 B",
        "correction_source_id": str(real_source_id),
        "effective_at": "2026-10-03T09:00:00+08:00",
    }
    wd_ab = tool(port_a, "v3_update", wd_correct_ab_args, "withdraw-dedicated-a-to-b")
    if not check("dedicated_chain_a_to_b_succeeded", tool_ok(wd_ab),
                 f"error={wd_ab.get('error')!r} status={wd_ab.get('status')!r}"):
        return finish()
    wd_b = str(pick(wd_ab, *RECEIPT_ID_KEYS["new"]) or "")
    check("dedicated_chain_a_to_b_supersedes",
          str(pick(wd_ab, "relation_type") or "") == "supersedes",
          f"relation_type={pick(wd_ab, 'relation_type')!r}")
    wd_edge_ab = edge_row(wd_a)
    check("dedicated_edge_a_to_b_is_supersedes",
          wd_edge_ab is not None and wd_edge_ab["to_memory_id"] == wd_b
          and wd_edge_ab["relation_type"] == "supersedes",
          f"edge={wd_edge_ab}")

    wd_bc = tool(port_a, "v3_update",
                 {"action": "correct", "memory_id": wd_b, "mode": "replace",
                  "replacement_content": content_wd_c,
                  "correction_reason": "撤回专用场景：B 改为 C",
                  "correction_source_id": str(real_source_id),
                  "effective_at": "2026-10-03T10:00:00+08:00"},
                 "withdraw-dedicated-b-to-c")
    wd_c = str(pick(wd_bc, *RECEIPT_ID_KEYS["new"]) or "")
    if not check("dedicated_chain_b_to_c_succeeded", tool_ok(wd_bc) and bool(wd_c),
                 f"error={wd_bc.get('error')!r} new={wd_c!r}"):
        return finish()
    report["dedicated_withdraw_chain"] = {"A": wd_a, "B": wd_b, "C": wd_c}

    # history called on the TERMINAL C' must include its ANCESTORS A' and B'
    wd_hist = tool(port_a, "v3_get",
                   {"target": "memory", "memory_id": wd_c, "mode": "history"},
                   "withdraw-dedicated-history-from-c")
    wd_hist_ids = reader_history_ids(wd_hist)
    wd_hist_statuses = reader_history_statuses(wd_hist)
    wd_hist_labels = reader_history_labels(wd_hist)
    check("dedicated_history_from_c_includes_full_chain",
          set(wd_hist_ids) == {wd_a, wd_b, wd_c},
          f"ids={wd_hist_ids} statuses={wd_hist_statuses} labels={wd_hist_labels}")
    check("dedicated_history_chain_is_ordered",
          wd_hist_ids in ([wd_a, wd_b, wd_c], [wd_c, wd_b, wd_a]),
          f"ids={wd_hist_ids}")
    check("dedicated_history_from_c_preserves_exact_content",
          reader_requested(wd_hist).get("content") == content_wd_c
          and str(reader_requested(wd_hist).get("memory_id") or "") == wd_c,
          f"requested={reader_requested(wd_hist)}")
    # This read happens BEFORE C' is withdrawn, so the chain's terminal is the
    # CURRENT version: A'/B' are superseded/archived and C' is current/active.
    # The labels and statuses are read verbatim from ``history.versions`` and
    # must align index-for-index with the ordered ids (ancestor-first or
    # terminal-first); a withdrawn terminal belongs to the POST-withdraw read.
    check("dedicated_history_labels_are_accurate",
          history_versions_match_exact(
              wd_hist_ids, wd_hist_labels, wd_hist_statuses,
              [wd_a, wd_b, wd_c],
              ["superseded", "superseded", "current"],
              ["archived", "archived", "active"]),
          f"ids={wd_hist_ids} statuses={wd_hist_statuses} labels={wd_hist_labels}")

    # retry the FIRST correction after B'->C': it refers to its ORIGINAL B, with
    # B's ACTUAL status now archived, and reports the CURRENT terminal C.
    wd_retry_mem_before = memory_count()
    wd_retry_rel_before = relation_count()
    wd_ab_retry = tool(port_a, "v3_update", wd_correct_ab_args,
                       "withdraw-dedicated-retry-a-to-b")
    wd_retry_new = str(pick(wd_ab_retry, *RECEIPT_ID_KEYS["new"]) or "")
    wd_retry_current = str(pick(wd_ab_retry, "current_memory_id",
                                "resolved_memory_id") or "")
    check("dedicated_retry_first_correction_succeeds", tool_ok(wd_ab_retry),
          f"error={wd_ab_retry.get('error')!r} dedup={wd_ab_retry.get('deduplicated')}")
    check("dedicated_retry_refers_original_b",
          wd_retry_new == wd_b, f"new={wd_retry_new!r} expected={wd_b!r}")
    check("dedicated_retry_reports_b_archived_c_current",
          str(pick(wd_ab_retry, "new_status") or "") == "archived"
          and wd_retry_current == wd_c,
          f"new_status={pick(wd_ab_retry, 'new_status')!r} "
          f"current={wd_retry_current!r} expected current={wd_c!r}")
    check("dedicated_retry_creates_zero_changes",
          memory_count() == wd_retry_mem_before
          and relation_count() == wd_retry_rel_before,
          f"mem {wd_retry_mem_before}->{memory_count()} "
          f"rel {wd_retry_rel_before}->{relation_count()}")

    # withdraw C' — NO replacement fields at all (content/title/tags absent)
    wd_withdraw_args = {
        "action": "correct",
        "memory_id": wd_c,
        "mode": "withdraw",
        "correction_reason": "撤回专用场景：用户明确撤回 C",
    }
    wd_mem_before_withdraw = memory_count()
    wd_rel_before_withdraw = relation_count()
    wd_withdraw = tool(port_a, "v3_update", wd_withdraw_args, "withdraw-dedicated-c")
    check("dedicated_withdraw_c_succeeded", tool_ok(wd_withdraw),
          f"error={wd_withdraw.get('error')!r} status={wd_withdraw.get('status')!r}")
    check("dedicated_withdraw_creates_no_replacement",
          memory_count() == wd_mem_before_withdraw
          and relation_count() == wd_rel_before_withdraw + 1,
          f"mem {wd_mem_before_withdraw}->{memory_count()} "
          f"rel {wd_rel_before_withdraw}->{relation_count()}")
    wd_withdraw_edge = edge_row(wd_c)
    check("dedicated_withdraw_edge_has_null_target",
          wd_withdraw_edge is not None
          and wd_withdraw_edge["relation_type"] == "withdraws"
          and wd_withdraw_edge["to_memory_id"] is None,
          f"edge={wd_withdraw_edge}")
    check("dedicated_withdrawn_row_is_not_active",
          (memory_row(wd_c) or {}).get("status") in ("archived", "withdrawn"),
          f"status={(memory_row(wd_c) or {}).get('status')!r}")

    # an exact retry of the withdrawal is idempotent
    wd_mem_before_wd_retry = memory_count()
    wd_rel_before_wd_retry = relation_count()
    wd_withdraw_retry = tool(port_a, "v3_update", wd_withdraw_args,
                             "withdraw-dedicated-c-retry")
    check("dedicated_withdraw_exact_retry_idempotent",
          tool_ok(wd_withdraw_retry)
          and memory_count() == wd_mem_before_wd_retry
          and relation_count() == wd_rel_before_wd_retry,
          f"ok={wd_withdraw_retry.get('_ok')} "
          f"dedup={wd_withdraw_retry.get('deduplicated')} "
          f"mem {wd_mem_before_wd_retry}->{memory_count()} "
          f"rel {wd_rel_before_wd_retry}->{relation_count()}")

    # a conflicting REPLACE against the withdrawn target must be refused
    wd_conflict = tool(port_a, "v3_update",
                       {"action": "correct", "memory_id": wd_c, "mode": "replace",
                        "replacement_content": content_wd_conflict,
                        "correction_reason": "撤回后不应再替换"},
                       "withdraw-dedicated-conflicting-replace")
    wd_conflict_text = json.dumps(wd_conflict, ensure_ascii=False).upper()
    check("dedicated_conflicting_replace_refused",
          not tool_ok(wd_conflict)
          and any(token in wd_conflict_text for token in
                  ("CORRECTION_CONFLICT", "NOT_CURRENT", "WITHDRAWN")),
          f"ok={wd_conflict.get('_ok')} error={wd_conflict.get('error')!r} "
          f"code={pick(wd_conflict, 'code', 'error_code')!r}")

    # current reads of A' and C' must BOTH answer with NO current memory
    wd_cur_a = tool(port_a, "v3_get",
                    {"target": "memory", "memory_id": wd_a, "mode": "current"},
                    "withdraw-dedicated-current-a")
    wd_no_cur_a, wd_detail_a = reader_current_absent(wd_cur_a)
    check("dedicated_current_read_a_has_no_current", wd_no_cur_a,
          f"detail={wd_detail_a} status={wd_cur_a.get('status')!r}")
    wd_cur_c = tool(port_a, "v3_get",
                    {"target": "memory", "memory_id": wd_c, "mode": "current"},
                    "withdraw-dedicated-current-c")
    wd_no_cur_c, wd_detail_c = reader_current_absent(wd_cur_c)
    check("dedicated_current_read_c_has_no_current", wd_no_cur_c,
          f"detail={wd_detail_c} status={wd_cur_c.get('status')!r}")

    # history after the withdrawal still preserves the exact payload + chain,
    # now with the terminal C' labelled withdrawn and no current version: the
    # labels/statuses must align index-for-index with the ordered ids.
    wd_hist_after = tool(port_a, "v3_get",
                         {"target": "memory", "memory_id": wd_c, "mode": "history"},
                         "withdraw-dedicated-history-after-withdraw")
    wd_hist_after_ids = reader_history_ids(wd_hist_after)
    wd_hist_after_labels = reader_history_labels(wd_hist_after)
    wd_hist_after_statuses = reader_history_statuses(wd_hist_after)
    check("dedicated_history_after_withdraw_keeps_full_chain",
          history_versions_match_exact(
              wd_hist_after_ids, wd_hist_after_labels, wd_hist_after_statuses,
              [wd_a, wd_b, wd_c],
              ["superseded", "superseded", "withdrawn"],
              ["archived", "archived", "archived"]),
          f"ids={wd_hist_after_ids} statuses={wd_hist_after_statuses} "
          f"labels={wd_hist_after_labels}")
    check("dedicated_history_after_withdraw_preserves_exact_content",
          reader_requested(wd_hist_after).get("content") == content_wd_c,
          f"requested={reader_requested(wd_hist_after)}")
    # A withdrawn chain has NO current version: the nested chain block must say
    # so instead of naming the archived terminal as current.
    check("dedicated_history_after_withdraw_has_no_current_id",
          reader_history_current_memory_id(wd_hist_after) == ""
          and str(wd_hist_after.get("resolved_memory_id") or "") == ""
          and not any(reader_history_is_current(wd_hist_after)),
          f"history.current_memory_id="
          f"{reader_history_current_memory_id(wd_hist_after)!r} "
          f"resolved_memory_id={wd_hist_after.get('resolved_memory_id')!r} "
          f"is_current={reader_history_is_current(wd_hist_after)}")
    wd_rel_chain = reader_relation_chain(wd_hist_after)
    wd_expected_edges = [("supersedes", wd_a, wd_b),
                         ("supersedes", wd_b, wd_c),
                         ("withdraws", wd_c, None)]
    check("dedicated_history_relation_chain_ordered",
          relation_chain_matches_exact(wd_rel_chain, wd_expected_edges),
          f"chain={wd_rel_chain} expected_exact={wd_expected_edges} "
          f"edges_from_history_block={reader_history_edges(wd_hist_after)}")

    # the scenario wrote only its own rows: every PRE-EXISTING row/payload and
    # the raw/QA tables are byte-identical, and the deltas are exactly +3/+3.
    wd_post_rows = table_snapshot()
    wd_post_raw = raw_snapshot()
    wd_changed = [k for k, v in wd_pre_rows.items() if wd_post_rows.get(k) != v]
    check("dedicated_scenario_raw_qa_unchanged",
          wd_post_raw["conversation_stream"] == wd_pre_raw["conversation_stream"]
          and wd_post_raw["qa_pairs"] == wd_pre_raw["qa_pairs"],
          f"raw before={wd_pre_raw} after={wd_post_raw}")
    check("dedicated_scenario_left_existing_rows_unchanged", not wd_changed,
          f"changed pre-existing rows={wd_changed[:10]} (count={len(wd_changed)})")
    check("dedicated_scenario_wrote_exactly_its_own_rows",
          memory_count() == wd_pre_mem + 3 and relation_count() == wd_pre_rel + 3,
          f"mem {wd_pre_mem}->{memory_count()} rel {wd_pre_rel}->{relation_count()} "
          f"(expected +3/+3: A',B',C' and A'->B',B'->C',C'->withdraw)")

    # 5s. fresh process / fresh server / fresh session recall
    stop_info = server_a.stop()
    report["server_a"]["stop"] = stop_info
    check("server_a_shut_down_gracefully", not server_a.alive()
          and stop_info.get("stopped") in ("graceful", "never-started"),
          f"stop={stop_info}")

    port_b = free_port()
    server_b = Server("B", python, env, project, port_b, runs / "serve-b.log")
    OWNED_SERVERS.append(server_b)
    info_b = server_b.start()
    report["server_b"] = info_b
    if check("server_b_is_a_new_process", bool(info_b.get("pid"))
             and info_b.get("pid") != info_a.get("pid"),
             f"pid A={info_a.get('pid')} pid B={info_b.get('pid')} port={port_b}"):
        pass
    if not check("server_b_health_ok", bool(info_b["health"].get("ok"))
                 and bool(info_b["health"].get("pg")),
                 f"health={info_b['health']} port={port_b} pid={info_b.get('pid')}"):
        gap("fresh-process recall could not be executed: server B never became "
            "healthy, so no fresh-process / fresh-session check ran")
    if info_b["health"].get("ok"):
        def embedding_coverage() -> dict:
            """Whether the canonical rows really carry an embedding vector."""
            cols = {r[0] for r in pg_rows(
                db_name,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='explicit_memories'")}
            total = int(pg_rows(
                db_name, "SELECT count(*) FROM public.explicit_memories")[0][0])
            embedded = None
            if "embedding" in cols:
                embedded = int(pg_rows(
                    db_name,
                    "SELECT count(embedding) FROM public.explicit_memories")[0][0])
            return {"embedding_column_present": "embedding" in cols,
                    "rows_total": total, "rows_with_embedding": embedded}

        question = "迁移项目现在采用什么部署方案？"
        status_pf, pf_body = http_raw(f"http://127.0.0.1:{port_b}/prefetch",
                                      build_prefetch_request(question, f"m01m02e2e-fresh-{db_name}"),
                                      "prefetch-fresh-session")
        block = str((pf_body or {}).get("block") or "")
        report["raw"]["prefetch"].append({
            "query": question,
            "session_id": f"m01m02e2e-fresh-{db_name}",
            "http_status": status_pf,
            "block_chars": len(block),
            "block": truncate(scrub(block, secret_value))[0],
        })
        check("fresh_session_prefetch_ok", status_pf == 200, f"http {status_pf}")
        # The recall block is CLASSIFIED, never silently failed: in this isolated
        # environment the canonical rows carry embedding=null and the profile has
        # no embedding service, so a vector recall block cannot exist here. That
        # is declared as a coverage gap; the truth that CAN be checked (the block
        # never presents the superseded payload as current) is still asserted.
        coverage = embedding_coverage()
        recall_env = recall_environment(
            embedding_service_configured=embedding_service_configured,
            rows_total=coverage["rows_total"],
            rows_with_embedding=coverage["rows_with_embedding"],
        )
        recall = classify_recall(
            block, current_content=content_c,
            superseded_contents=(content_a, content_b),
            embeddings_available=recall_env["vector_recall_available"],
        )
        report["recall"] = {
            "environment": recall_env,
            "coverage": coverage,
            "classification": recall["classification"],
            "block_chars": recall["block_chars"],
            "superseded_present": recall["superseded_present"],
        }
        if recall["classification"] == RECALL_CURRENT_RECALLED:
            check("fresh_session_recall_contains_current_version", True,
                  f"classification={recall['classification']} "
                  f"block_chars={recall['block_chars']}")
        elif recall["classification"] == RECALL_EMBEDDINGS_UNAVAILABLE:
            # NOT a silent fail and NOT a pass: the check is recorded with a null
            # verdict and the gap is declared, because the environment — not the
            # product — is what makes the vector block empty.
            report["checks"]["fresh_session_recall_contains_current_version"] = {
                "ok": None,
                "observed": (
                    "NOT EVALUABLE BY ENVIRONMENT: the vector recall block is empty "
                    "because this isolated profile has no embedding service "
                    f"(embedding_service_configured="
                    f"{recall_env['embedding_service_configured']}) and the canonical "
                    f"rows carry embedding=null "
                    f"({recall_env['rows_with_embedding']}/"
                    f"{recall_env['canonical_rows']} embedded); "
                    f"block_chars={recall['block_chars']}"),
                "declared_coverage_gap": True,
            }
            gap("fresh-session vector recall block is EMPTY BY ENVIRONMENT "
                "(embedding=null, no embedding service configured): the current "
                "version cannot appear in a vector block here, so "
                "fresh_session_recall_contains_current_version is declared NOT "
                "EVALUABLE and recorded as a coverage gap — not a pass and not a "
                "silent failure")
            check("fresh_session_recall_classified_embeddings_unavailable",
                  recall["classification"] == RECALL_EMBEDDINGS_UNAVAILABLE
                  and not recall["superseded_present"],
                  f"classification={recall['classification']} "
                  f"block_chars={recall['block_chars']} "
                  f"superseded_present={recall['superseded_present']} (the block "
                  f"must never present the superseded payload as current)")
        else:
            check("fresh_session_recall_contains_current_version", False,
                  f"classification={recall['classification']} "
                  f"block_chars={recall['block_chars']} "
                  f"embeddings_available={recall_env['vector_recall_available']} — "
                  f"the embedding environment CAN serve a vector recall, so the "
                  f"current version must appear")
        check("fresh_session_recall_does_not_return_old_as_current", content_a not in block,
              f"block contains the superseded payload: {content_a[:40]!r}")
        b_ids = search_ids(port_b, "迁移项目部署方案", "search-fresh-process")
        # The chain ended at C, so the fresh process must surface C and must
        # NOT surface A (superseded) or B (corrected away). An empty result is a
        # failure, not a pass: "nothing found" would make the exclusion
        # vacuously true and prove nothing about fresh-process recall.
        check("fresh_process_search_finds_the_chain_terminal_version", bool(b_ids)
              and id_c in b_ids,
              f"expected the terminal version {id_c!r} in source_ids={b_ids}")
        check("fresh_process_search_returns_current_only",
              mem_a not in b_ids and new_id not in b_ids,
              f"source_ids={b_ids} must contain neither A={mem_a!r} nor B={new_id!r}")
        # The DEDICATED withdraw scenario: a fresh process must never surface a
        # withdrawn explicit target as current — neither by recall nor by search.
        wd_fresh_ids = search_ids(port_b, wd_marker, "search-fresh-process-withdrawn")
        check("fresh_process_search_excludes_withdrawn_target",
              wd_c not in wd_fresh_ids and wd_a not in wd_fresh_ids
              and wd_b not in wd_fresh_ids,
              f"source_ids={wd_fresh_ids} must exclude the withdrawn terminal "
              f"C'={wd_c!r} and its archived ancestors")
        wd_pf_status, wd_pf_body = http_raw(
            f"http://127.0.0.1:{port_b}/prefetch",
            build_prefetch_request(f"撤回专用场景现在采用什么方案？{wd_marker}",
                                   f"m01m02e2e-wd-fresh-{db_name}"),
            "prefetch-fresh-withdrawn")
        wd_block = str((wd_pf_body or {}).get("block") or "")
        check("fresh_session_recall_excludes_withdrawn_payload",
              wd_pf_status == 200
              and content_wd_c not in wd_block and content_wd_a not in wd_block,
              f"http {wd_pf_status} block contains a withdrawn/superseded payload="
              f"{content_wd_c in wd_block or content_wd_a in wd_block}")
    else:
        gap("server B never became healthy; fresh-session recall not executed")

    # ── 6. migration-required failure without the relations table ───────────
    assert_disposable_target(db_nomig)
    boot_nomig = run_cli(["bootstrap", "--target",
                          dsn_without_password(args.pg_host, args.pg_port, db_nomig, args.pg_user)],
                         "bootstrap-nomig")
    if check("nomig_database_bootstrapped", boot_nomig.get("_rc") == 0,
             f"rc={boot_nomig.get('_rc')}"):
        assert_disposable_target(db_nomig)
        pg_exec(db_nomig, f"DROP TABLE IF EXISTS {RELATION_TABLE}")
        nomig_tables = {
            r[0] for r in pg_rows(db_nomig,
                                  "SELECT table_name FROM information_schema.tables "
                                  "WHERE table_schema='public'")
        }
        report["nomigration_db"] = {
            "database": db_nomig,
            "relations_table_dropped": RELATION_TABLE.split(".")[-1] not in nomig_tables,
            "note": "DROP is gated on assert_disposable_target and only ever targets a "
                    "database this run created under the m01m02e2e_ prefix",
        }
        check("nomigration_db_has_no_relations_table",
              RELATION_TABLE.split(".")[-1] not in nomig_tables,
              f"tables include relations={RELATION_TABLE.split('.')[-1] in nomig_tables}")
        env_nomig = dict(env)
        env_nomig["V3CORE_CONFIG"] = str(cfg_nomig)
        port_n = free_port()
        server_n = Server("nomig", python, env_nomig, project, port_n, runs / "serve-nomig.log")
        OWNED_SERVERS.append(server_n)
        info_n = server_n.start()
        report["server_nomig"] = info_n
        if info_n["health"].get("ok"):
            nomig_store = tool(port_n, "v3_store",
                               {"category": "projects", "title": "无迁移基线",
                                "content": f"无迁移基线内容。{marker}-NOMIG",
                                "source_id": f"m01m02e2e-nomig-{db_nomig}"}, "nomig-store")
            nomig_mem = str(nomig_store.get("source_id") or "")
            nomig_before = int(pg_rows(db_nomig, "SELECT count(*) FROM public.explicit_memories")[0][0])
            nomig_correct = tool(port_n, "v3_update",
                                 {"action": "correct", "memory_id": nomig_mem,
                                  "replacement_content": f"无迁移基线替换。{marker}-NOMIG-B",
                                  "correction_reason": "must fail truthfully without the schema"},
                                 "nomig-correct")
            nomig_text = json.dumps(nomig_correct, ensure_ascii=False)
            check("missing_schema_fails_truthfully", not tool_ok(nomig_correct),
                  f"ok={nomig_correct.get('_ok')} error={truncate(str(nomig_correct.get('error')))[0]}")
            check("missing_schema_reports_migration_required",
                  "MIGRAT" in nomig_text.upper() or "SCHEMA" in nomig_text.upper()
                  or "RELATION" in nomig_text.upper(),
                  f"payload={truncate(scrub(nomig_text, secret_value))[0]}")
            nomig_after = int(pg_rows(
                db_nomig, "SELECT count(*) FROM public.explicit_memories")[0][0])
            check("missing_schema_created_no_hidden_fallback_row",
                  nomig_after == nomig_before,
                  f"explicit_memories before={nomig_before} after={nomig_after} "
                  f"identical={nomig_after == nomig_before}")
        else:
            gap("nomigration probe server never became healthy; truthful-failure check skipped")
        # the stop record is kept (not discarded): an unclean shutdown must be
        # visible in the report instead of vanishing with the process handle.
        report["server_nomig"]["stop"] = server_n.stop()

    # ── 7. existing-install upgrade: dry-run -> apply -> replay ─────────────
    # The PRIMARY pre-upgrade state must be a REALISTIC prior install, not an
    # empty database and not a one-table toy. The additive upgrade body really
    # does ALTER public.qa_pairs ADD COLUMN IF NOT EXISTS ..., so a baseline that
    # never had qa_pairs is not a v0.2 install and the apply truthfully refuses
    # it (atomically). The realistic baseline is therefore the FULL current base
    # schema built by ``bootstrap``, with EXACTLY the M01 sidecar removed
    # afterwards — the memory_relations table and the v0.3 schema_versions row —
    # plus a unique pre-existing row snapshotted before any dry-run.
    upgrade_dsn = dsn_without_password(args.pg_host, args.pg_port, db_upgrade, args.pg_user)
    plan_out = runs / "upgrade-plan.json"
    data_id = f"m01m02e2e-preexisting-{db_upgrade}"
    prior_content = f"升级前已存在的记忆内容。{marker}-UPGRADE"
    prior_sql_path = source / "src" / "v3-core" / "schema" / "explicit_memories.sql"
    assert_disposable_target(db_upgrade)
    prior_built = False
    prior_bootstrap_rc = None
    prior_bootstrap_applied = None
    prior_sidecar_removed = False
    prior_versions: set = set()
    # The shipped explicit_memories.sql is still REQUIRED: it is the DDL source
    # for the SEPARATE minimal baseline below, which pins the product's truthful
    # atomic refusal against a baseline that is not a real v0.2 install.
    if not check("prior_install_schema_artifact_is_shipped", prior_sql_path.is_file(),
                 f"prior-install DDL source={prior_sql_path} exists={prior_sql_path.is_file()}"):
        gap("the shipped explicit_memories.sql artifact is missing; the minimal-baseline "
            "truthful-refusal path could not be exercised")
    else:
        # (1) bootstrap the FULL current base schema into the upgrade database.
        prior_boot = run_cli(["bootstrap", "--target", upgrade_dsn],
                             "upgrade-prior-bootstrap")
        prior_bootstrap_rc = prior_boot.get("_rc")
        prior_boot_inner = (prior_boot.get("result")
                            if isinstance(prior_boot.get("result"), dict) else {})
        prior_bootstrap_applied = prior_boot_inner.get("applied")
        if prior_bootstrap_applied is None:
            prior_bootstrap_applied = prior_boot.get("applied")
        if check("prior_install_bootstrapped_full_base_schema",
                 prior_bootstrap_rc == 0 and prior_bootstrap_applied is not False,
                 f"bootstrap --target {db_upgrade} rc={prior_bootstrap_rc} "
                 f"applied={prior_bootstrap_applied}"):
            # (2) remove EXACTLY the M01 sidecar: the relation table AND its
            # schema-version row, nothing else.
            assert_disposable_target(db_upgrade)
            pg_exec(db_upgrade, f"DROP TABLE IF EXISTS {RELATION_TABLE}")
            pg_exec(db_upgrade,
                    "DELETE FROM public.schema_versions WHERE version='v0.3'")
            prior_sidecar_removed = True
        else:
            gap("the realistic prior install (full base schema via bootstrap) could "
                "not be built; the dry-run/apply/replay upgrade closure was not "
                "exercised against an existing install")
        if prior_bootstrap_rc == 0:
            # (3) a unique, run-scoped payload so the preservation check cannot
            # pass by comparing a row that some earlier step happened to create.
            pg_exec(db_upgrade,
                    "INSERT INTO public.explicit_memories "
                    "(memory_id, category, title, content, tags, provenance) "
                    "VALUES (%s, 'projects', '升级前既有记忆', %s, ARRAY['m01m02e2e-upgrade'], "
                    "'{}'::jsonb) ON CONFLICT (memory_id) DO NOTHING",
                    (data_id, prior_content))
        # (4) the baseline is asserted against the real catalog: the pre-existing
        # table and row exist, and the sidecar (table AND v0.3 version row) is
        # gone. The check is evaluated even when the bootstrap failed, so a broken
        # baseline is reported rather than silently skipped.
        prior_tables = {
            r[0] for r in pg_rows(db_upgrade,
                                  "SELECT table_name FROM information_schema.tables "
                                  "WHERE table_schema='public'")
        }
        if "schema_versions" in prior_tables:
            prior_versions = {
                str(r[0]) for r in pg_rows(db_upgrade,
                                           "SELECT version FROM public.schema_versions")
            }
        prior_seeded = (int(pg_rows(db_upgrade,
                                    "SELECT count(*) FROM public.explicit_memories "
                                    "WHERE memory_id=%s", (data_id,))[0][0])
                        if "explicit_memories" in prior_tables else 0)
        relations_present = RELATION_TABLE.split(".")[-1] in prior_tables
        prior_built = check(
            "prior_install_has_data_and_no_relations_table",
            "explicit_memories" in prior_tables
            and not relations_present
            and prior_seeded == 1
            and prior_sidecar_removed
            and "v0.3" not in prior_versions,
            f"tables={sorted(prior_tables)} relations_present={relations_present} "
            f"seeded_rows={prior_seeded} schema_versions={sorted(prior_versions)} "
            f"sidecar_removed={prior_sidecar_removed}")

    def explicit_schema_fingerprint(database: str) -> dict:
        """Full actual schema of explicit_memories + memory_relations.

        Columns alone are a weak parity signal (they hide type/default/constraint
        drift). This reads the real catalog: per-column type, nullability and
        default, plus every index and check constraint definition.
        """
        cols = pg_rows(
            database,
            "SELECT table_name, column_name, data_type, is_nullable, "
            "       coalesce(column_default, '') "
            "  FROM information_schema.columns "
            " WHERE table_schema='public' "
            "   AND table_name IN ('explicit_memories', 'memory_relations') "
            " ORDER BY table_name, ordinal_position")
        idx = pg_rows(
            database,
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            " WHERE schemaname='public' "
            "   AND tablename IN ('explicit_memories', 'memory_relations') "
            " ORDER BY tablename, indexname")
        cons = pg_rows(
            database,
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
            "  FROM pg_constraint "
            " WHERE connamespace = 'public'::regnamespace "
            "   AND conrelid::regclass::text IN ('explicit_memories', 'memory_relations') "
            " ORDER BY conrelid::regclass::text, conname")
        return {
            "columns": [list(r) for r in cols],
            "indexes": [list(r) for r in idx],
            "constraints": [list(r) for r in cons],
        }

    def prior_row_snapshot(database: str) -> dict:
        rows = pg_rows(database,
                       "SELECT memory_id, category, title, content, tags::text, "
                       "provenance::text, status, created_at::text, updated_at::text "
                       "FROM public.explicit_memories WHERE memory_id=%s", (data_id,))
        return {"rows": [list(r) for r in rows]}

    pre_mem = 0
    up_rel_cols: set[str] = set()
    sql_sha_1 = None
    if not prior_built:
        gap("the prior install could not be built; the dry-run/apply/replay upgrade "
            "closure was not exercised against an existing install")
    else:
        pre_snapshot = prior_row_snapshot(db_upgrade)
        pre_fingerprint = explicit_schema_fingerprint(db_upgrade)
        report["prior_install"] = {
            "database": db_upgrade,
            "baseline": ("bootstrap (full base schema), then DROP "
                         "public.memory_relations and DELETE schema_versions v0.3"),
            "bootstrap_rc": prior_bootstrap_rc,
            "sidecar_removed": prior_sidecar_removed,
            "schema_versions": sorted(prior_versions),
            "relations_table_present": any(
                c[0] == RELATION_TABLE.split(".")[-1] for c in pre_fingerprint["columns"]),
            "seeded_memory_id": data_id,
            "snapshot": pre_snapshot,
        }
        dry_1 = run_cli(["upgrade", "--target", upgrade_dsn, "--dry-run",
                         "--plan-out", str(plan_out)], "upgrade-dry-run-1")
        dry_tables = {
            r[0] for r in pg_rows(db_upgrade,
                                  "SELECT table_name FROM information_schema.tables "
                                  "WHERE table_schema='public'")
        }
        dry_rows_identical = pre_snapshot == prior_row_snapshot(db_upgrade)
        dry_schema_identical = explicit_schema_fingerprint(db_upgrade) == pre_fingerprint
        check("upgrade_dry_run_writes_no_ddl",
              dry_rows_identical and dry_schema_identical,
              f"dry_run_rows_identical={dry_rows_identical} "
              f"dry_run_schema_identical={dry_schema_identical} "
              f"dry_run_rc={dry_1.get('_rc')} tables={sorted(dry_tables)[:6]}")
        check("upgrade_dry_run_reports_missing_relations",
              "memory_relations" in json.dumps(dry_1, ensure_ascii=False),
              f"missing_required_tables="
              f"{(dry_1.get('plan') or {}).get('pre_state', {}).get('missing_required_tables')} "
              f"rc={dry_1.get('_rc')}")
        plan_sha_1 = dry_1.get("plan_sha256") or ((dry_1.get("plan") or {}).get("plan_sha256"))
        report["upgrade_plan_sha256_run1"] = plan_sha_1
        check("upgrade_dry_run_emits_plan_sha", bool(plan_sha_1), f"plan_sha256={plan_sha_1!r}")
        sql_sha_1 = dry_1.get("final_combined_sql_sha256") or (
            (dry_1.get("plan") or {}).get("combined_sql", {}) or {}).get("sha256")
        # The observed string is DERIVED from the real CLI result: the pre-fix
        # constant claimed rc=0 regardless of the outcome, while the raw entry
        # recorded returncode=1 and applied=false.
        apply_res = run_cli(["upgrade", "--target", upgrade_dsn, "--apply"], "upgrade-apply")
        apply_inner = (apply_res.get("result")
                       if isinstance(apply_res.get("result"), dict) else {})
        apply_rc = apply_res.get("_rc")
        apply_applied = apply_inner.get("applied")
        if apply_applied is None:
            apply_applied = apply_res.get("applied")
        apply_error = apply_inner.get("error")
        if apply_error is None:
            apply_error = apply_res.get("error")
        if check("upgrade_applied",
                 apply_rc == 0 and apply_applied is True,
                 f"upgrade --apply rc={apply_rc} applied={apply_applied} "
                 f"error={truncate(str(apply_error))[0] if apply_error else None}"):
            assert_disposable_target(db_upgrade)
            # The pre-existing row must be byte-identical after the apply: the
            # migration is additive and must not rewrite or re-key it.
            post_apply = prior_row_snapshot(db_upgrade)
            check("upgrade_apply_preserved_prior_row_exactly",
                  post_apply == pre_snapshot,
                  f"before={pre_snapshot} after={post_apply}")
            pre_mem = len(post_apply["rows"])
            up_rel_cols = {
                r[0] for r in pg_rows(db_upgrade,
                                      "SELECT column_name FROM information_schema.columns "
                                      "WHERE table_schema='public' AND table_name=%s",
                                      ("memory_relations",))
            }
            check("upgrade_created_relations_table",
                  set(RELATION_REQUIRED_COLUMNS) <= up_rel_cols,
                  f"missing={sorted(set(RELATION_REQUIRED_COLUMNS) - up_rel_cols)}")
            replay = run_cli(["upgrade", "--target", upgrade_dsn, "--apply"], "upgrade-apply-replay")
            check("upgrade_replay_is_idempotent", replay.get("_rc") == 0,
                  f"replay rc={replay.get('_rc')} error={replay.get('error')}")
            row_pre = pg_rows(db_upgrade,
                              "SELECT memory_id, content, status FROM public.explicit_memories "
                              "WHERE memory_id=%s", (data_id,))
            check("upgrade_preserved_existing_data",
                  bool(row_pre) and row_pre[0][1].endswith(f"{marker}-UPGRADE")
                  and row_pre[0][2] == "active",
                  f"row={row_pre}")
            # Schema parity against the real catalog, not just column names.
            upgrade_fingerprint = explicit_schema_fingerprint(db_upgrade)
            bootstrap_fingerprint = explicit_schema_fingerprint(db_name)
            for part in ("columns", "indexes", "constraints"):
                same = bootstrap_fingerprint[part] == upgrade_fingerprint[part]
                report.setdefault("upgrade_schema_parity", {})[part] = {
                    "identical": same,
                    "bootstrap_only": [r for r in bootstrap_fingerprint[part]
                                       if r not in upgrade_fingerprint[part]],
                    "upgrade_only": [r for r in upgrade_fingerprint[part]
                                     if r not in bootstrap_fingerprint[part]],
                }
                check(f"upgrade_schema_parity_{part}", same,
                      f"bootstrap={len(bootstrap_fingerprint[part])} rows, "
                      f"upgrade={len(upgrade_fingerprint[part])} rows, "
                      f"diff={report['upgrade_schema_parity'][part]}")
            check("upgrade_replay_preserved_prior_row_exactly",
                  prior_row_snapshot(db_upgrade) == pre_snapshot,
                  f"after replay={prior_row_snapshot(db_upgrade)}")
            dry_2 = run_cli(["upgrade", "--target", upgrade_dsn, "--dry-run",
                             "--plan-out", str(runs / "upgrade-plan-2.json")], "upgrade-dry-run-2")
            plan_sha_2 = dry_2.get("plan_sha256") or ((dry_2.get("plan") or {}).get("plan_sha256"))
            sql_sha_2 = dry_2.get("final_combined_sql_sha256") or (
                (dry_2.get("plan") or {}).get("combined_sql", {}) or {}).get("sha256")
            check("upgrade_dry_run_after_apply_is_a_noop",
                  not (dry_2.get("plan") or {}).get("pre_state", {}).get("missing_required_tables"),
                  f"missing={((dry_2.get('plan') or {}).get('pre_state', {}).get('missing_required_tables'))}")
            # SQL parity: the exact DDL body must be byte-identical before and after the
            # apply. The plan_sha256 legitimately changes (it encodes pre_state), so it is
            # compared only as "the second dry-run is also self-consistent".
            check("upgrade_sql_parity_identical_ddl_body", bool(sql_sha_1) and sql_sha_1 == sql_sha_2,
                  f"final_combined_sql_sha256 run1={sql_sha_1!r} run2={sql_sha_2!r}")
            check("upgrade_dry_run_after_apply_still_emits_plan_sha", bool(plan_sha_2),
                  f"plan_sha256 after apply={plan_sha_2!r}")
            report["upgrade"] = {
                "database": db_upgrade,
                "pre_existing_rows": pre_mem,
                "plan_sha256_run1": plan_sha_1,
                "plan_sha256_run2": plan_sha_2,
                "final_combined_sql_sha256": sql_sha_1,
            }

    # bootstrap / upgrade parity: both paths must expose the same relation columns
    check("bootstrap_and_upgrade_relations_schema_parity",
          {c for c in rel_cols if c in RELATION_REQUIRED_COLUMNS}
          == {c for c in up_rel_cols if c in RELATION_REQUIRED_COLUMNS},
          f"bootstrap={sorted(set(rel_cols) & set(RELATION_REQUIRED_COLUMNS))} "
          f"upgrade={sorted(set(up_rel_cols) & set(RELATION_REQUIRED_COLUMNS))}")

    # ── 7b. minimal baseline: the truthful atomic refusal is pinned ─────────
    # The old (unrealistic) one-table baseline is kept as a SEPARATE check. A
    # database that only ever had explicit_memories + the vector extension is not
    # a v0.2 install, so the additive upgrade body cannot run its ALTERs: the
    # product must refuse ATOMICALLY — applied=false, a real non-zero rc, no
    # relations table created and no partial DDL. The honest refusal is a product
    # behaviour worth pinning, not a failure.
    min_dsn = dsn_without_password(args.pg_host, args.pg_port, db_upgrade_min, args.pg_user)
    assert_disposable_target(db_upgrade_min)
    min_built = False
    if not prior_sql_path.is_file():
        gap("the shipped explicit_memories.sql is missing; the minimal-baseline "
            "truthful-refusal path was not exercised")
    else:
        min_sql = prior_sql_path.read_text(encoding="utf-8")
        pg_exec(db_upgrade_min, "CREATE EXTENSION IF NOT EXISTS vector")
        pg_exec(db_upgrade_min, min_sql)
        min_row_id = f"m01m02e2e-minimal-{db_upgrade_min}"
        pg_exec(db_upgrade_min,
                "INSERT INTO public.explicit_memories "
                "(memory_id, category, title, content, tags, provenance) "
                "VALUES (%s, 'projects', '最小基线记忆', %s, ARRAY['m01m02e2e-minimal'], "
                "'{}'::jsonb) ON CONFLICT (memory_id) DO NOTHING",
                (min_row_id, f"最小基线内容。{marker}-MINIMAL"))
        min_tables = {
            r[0] for r in pg_rows(db_upgrade_min,
                                  "SELECT table_name FROM information_schema.tables "
                                  "WHERE table_schema='public'")
        }
        min_built = check(
            "minimal_baseline_is_explicit_memories_only",
            "explicit_memories" in min_tables
            and RELATION_TABLE.split(".")[-1] not in min_tables,
            f"tables={sorted(min_tables)} "
            f"relations_present={RELATION_TABLE.split('.')[-1] in min_tables}")
    if min_built:
        min_apply = run_cli(["upgrade", "--target", min_dsn, "--apply"],
                            "upgrade-apply-minimal")
        min_inner = (min_apply.get("result")
                     if isinstance(min_apply.get("result"), dict) else {})
        min_rc = min_apply.get("_rc")
        min_applied = min_inner.get("applied")
        if min_applied is None:
            min_applied = min_apply.get("applied")
        min_error = min_inner.get("error")
        if min_error is None:
            min_error = min_apply.get("error")
        min_tables_after = {
            r[0] for r in pg_rows(db_upgrade_min,
                                  "SELECT table_name FROM information_schema.tables "
                                  "WHERE table_schema='public'")
        }
        min_relations_created = RELATION_TABLE.split(".")[-1] in min_tables_after
        min_versions_after = (
            {str(r[0]) for r in pg_rows(db_upgrade_min,
                                        "SELECT version FROM public.schema_versions")}
            if "schema_versions" in min_tables_after else set())
        min_qa_cols = {
            r[0] for r in pg_rows(db_upgrade_min,
                                  "SELECT column_name FROM information_schema.columns "
                                  "WHERE table_schema='public' AND table_name='qa_pairs'")
        }
        check("minimal_baseline_upgrade_refused_atomically",
              min_rc != 0 and min_applied is False
              and not min_relations_created
              and "v0.3" not in min_versions_after
              and not ({"source_id", "turn_id"} & min_qa_cols),
              f"upgrade --apply rc={min_rc} applied={min_applied} "
              f"error={truncate(str(min_error))[0] if min_error else None} "
              f"relations_created={min_relations_created} "
              f"schema_versions={sorted(min_versions_after)} "
              f"qa_pairs_has_upgrade_columns={bool({'source_id', 'turn_id'} & min_qa_cols)}")
        report["minimal_baseline_refusal"] = {
            "database": db_upgrade_min,
            "baseline": ("explicit_memories.sql + the vector extension only "
                         "(no schema_versions, no relations sidecar)"),
            "rc": min_rc,
            "applied": min_applied,
            "relations_table_created": min_relations_created,
            "schema_versions": sorted(min_versions_after),
        }

    # ── 8. packaged artifact reachability + single-source schema copies ────
    doctor = run_cli(["doctor", "--static"], "doctor-static")
    doctor_payload = doctor.get("result") if isinstance(doctor.get("result"), dict) else doctor
    doctor_checks = doctor_payload.get("checks") or {}
    doctor_sql = doctor_checks.get("packaged_sql") or {}
    report["doctor_static"] = {
        "rc": doctor.get("_rc"),
        "status": doctor_payload.get("status"),
        "errors": truncate(str(doctor_payload.get("errors") or []))[0],
        "warnings": truncate(str(doctor_payload.get("warnings") or []))[0],
        "packaged_sql_keys": sorted(doctor_sql) if isinstance(doctor_sql, dict) else None,
    }
    # `doctor --static` exits 1 on warnings by contract (a source checkout has no
    # installed distribution version), so the hard requirement is "no errors" plus a
    # present packaged_sql section — not rc==0.
    check("doctor_static_reports_no_errors",
          not (doctor_payload.get("errors") or []),
          f"status={doctor_payload.get('status')} rc={doctor.get('_rc')} "
          f"errors={truncate(str(doctor_payload.get('errors') or []))[0]}")
    if isinstance(doctor_sql, dict) and doctor_sql:
        missing_pkg = [k for k, v in doctor_sql.items()
                       if isinstance(v, dict) and v.get("present") is False]
        check("packaged_sql_artifacts_all_present", not missing_pkg,
              f"missing={missing_pkg} checked={sorted(doctor_sql)}")
        check("doctor_knows_the_relations_artifact",
              any("relation" in str(k).lower() for k in doctor_sql),
              f"doctor packaged-SQL tuple={sorted(doctor_sql)} (the relations artifact "
              "must be added to the doctor's required-artifact list by the M01 change)")
    else:
        gap("doctor --static did not emit a packaged_sql section on this build")

    packaged = subprocess.run(
        [python, "-c",
         "import json,hashlib;"
         "from v3core import distribution_cli as d;"
         "names=sorted(p.name for p in __import__('importlib').resources.files("
         "'v3core.schema').iterdir() if p.name.endswith('.sql'));"
         "print(json.dumps({n: d._package_sql_sha256(n) for n in names}))"],
        env=env, cwd=str(project), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    packaged_map = _try_json(packaged.stdout or "") or {}
    report["packaged_sql_sha256"] = packaged_map
    # Presence must be explicit: a non-empty map that simply omits
    # memory_relations.sql is not evidence the artifact ships.
    missing_required_packaged = [n for n in REQUIRED_PACKAGED_SQL if not packaged_map.get(n)]
    check("packaged_relations_artifact_reachable", not missing_required_packaged,
          f"required={list(REQUIRED_PACKAGED_SQL)} missing={missing_required_packaged} "
          f"packaged={sorted(packaged_map)} stderr="
          f"{truncate(packaged.stderr or '')[0]}")

    schema_dirs = [
        source / "src" / "v3-core" / "schema",
        source / "src" / "v3-core" / "src" / "v3core" / "schema",
    ]
    copies = schema_copy_report([d for d in schema_dirs if d.is_dir()])
    report["schema_copies"] = copies
    check("schema_copies_single_source", copies["in_sync"],
          f"out-of-sync artifacts="
          f"{[k for k, v in copies.get('files', {}).items() if not all(v['matches'].values())]}")
    mismatched_packaged = []
    missing_counterpart = []
    for name in REQUIRED_PACKAGED_SQL:
        meta = copies.get("files", {}).get(name)
        pkg_sha = packaged_map.get(name)
        if not meta:
            missing_counterpart.append(f"repo:{name}")
        if not pkg_sha:
            missing_counterpart.append(f"packaged:{name}")
        if meta and pkg_sha and pkg_sha != meta["sha256"]:
            mismatched_packaged.append(name)
    check("packaged_artifact_matches_repo_source",
          not mismatched_packaged and not missing_counterpart,
          f"mismatched={mismatched_packaged} missing={missing_counterpart} "
          f"(each required artifact needs BOTH a repo counterpart and a packaged hash)")

    # ── 9. derived-only writer-layer probes (injected stubs, no paid model) ─
    # A FRESH dedicated active memory is used as the probe target: the archived
    # B and the no-source base are both non-current/edge-bearing, so correcting
    # them would be refused for target reasons and the probe would silently
    # measure the wrong refusal instead of authority / source / embedding.
    derived_target = f"m01m02e2e-derived-{db_name}"
    # Server A is stopped by now; the dedicated target must be created through a
    # LIVE serve process (the last-started healthy one), never against a dead
    # port that would make the probe target silently absent.
    live_probe_port = None
    if info_b.get("health", {}).get("ok"):
        live_probe_port = port_b
    elif server_a.alive():
        live_probe_port = port_a
    if live_probe_port is None:
        derived_store = {
            "_ok": False,
            "error": "no live serve process available for the dedicated derived target",
        }
    else:
        derived_store = tool(live_probe_port, "v3_store",
                             {"category": "projects", "title": "派生探针基线",
                              "content": f"派生探针基线内容。{marker}-DERIVED-A",
                              "source_id": derived_target}, "store-derived-target")
    if not check("derived_probe_target_committed", tool_ok(derived_store)
                 and derived_target in [str(r) for r in find_values(derived_store, "source_id")],
                 f"source_id={derived_store.get('source_id')!r} "
                 f"ok={derived_store.get('_ok')} port={live_probe_port}"):
        gap("the dedicated derived-probe target could not be committed; the "
            "writer-layer probes are NOT run against a fallback archived or "
            "unrelated target — their refusals would measure the wrong cause")
        return finish()
    probe_path = runs / "derived_probe.py"
    probe_path.write_text(DERIVED_PROBE_SOURCE, encoding="utf-8")
    env_probe = dict(env)
    env_probe.update({
        "M01M02_PG_HOST": args.pg_host,
        "M01M02_PG_PORT": str(args.pg_port),
        "M01M02_PG_DB": db_name,
        "M01M02_PG_USER": args.pg_user,
        "M01M02_TARGET": derived_target,
        "M01M02_EMBED_REPLACEMENT": f"派生探针替换内容。{marker}-DERIVED",
    })
    probe = subprocess.run([python, "-B", str(probe_path)], env=env_probe, cwd=str(project),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
    probe_out = _try_json(probe.stdout or "") or {"_unparsed": (probe.stdout or "")[:2000]}
    report["derived_only"] = {
        "why": "these probes need a canonical injected stub; the public HTTP surface "
               "cannot inject one, so they are NOT evidence of the public contract",
        "probe_source": str(probe_path),
        "target": derived_target,
        "returncode": probe.returncode,
        "stderr_tail": scrub((probe.stderr or "")[-600:], secret_value),
        "results": probe_out,
    }
    # A crashed probe is a FAIL, not a gap: a non-zero exit or unparseable
    # stdout means the writer-layer contract was never exercised.
    check("derived_probe_process_exited_clean", probe.returncode == 0,
          f"rc={probe.returncode} stdout_parsed={not probe_out.get('_unparsed')} "
          f"stderr={truncate(scrub(probe.stderr or '', secret_value))[0]}")
    observed_probes = probe_out.get("probes") or {}
    missing_probes = [k for k in DERIVED_PROBE_KEYS if k not in observed_probes]
    check("derived_probe_reported_every_expected_probe", not missing_probes,
          f"expected={list(DERIVED_PROBE_KEYS)} observed={sorted(observed_probes)} "
          f"missing={missing_probes}")
    for name, result in observed_probes.items():
        report["checks"][f"derived::{name}"] = {
            "ok": bool(result.get("ok")), "observed": result, "derived_only": True,
        }
        if not result.get("ok"):
            reasons.append(f"derived::{name}: {truncate(str(result.get('error')))[0]}")
            report["failures"].append({"check": f"derived::{name}",
                                       "observed": result, "derived_only": True})
    if not observed_probes:
        gap("writer-layer derived probes produced no probe map (import or connection "
            "failure); authority refusal and embedding-failure durability are "
            "unverified — see derived_probe_process_exited_clean for the cause")

    # ── 9b. shut down every server this run still owns, and verify it ───────
    # Server A is stopped before server B starts; server B (and the nomigration
    # probe server) are still alive here, because the derived-probe target above
    # needed a LIVE serve process. They must be stopped BEFORE the residual
    # check, or no_leftover_server_process would report a process this run owned
    # as leftover — a driver cleanup defect, not a product one. A server that
    # cannot be stopped keeps its handle and is reported truthfully.
    stop_b = server_b.stop()
    report["server_b"]["stop"] = stop_b
    check("server_b_shut_down_gracefully",
          not server_b.alive() and stop_b.get("alive_after_stop") is not True,
          f"stop={stop_b}")
    extra_stops = []
    for srv in OWNED_SERVERS:
        if srv is server_b or srv.proc is None:
            continue
        extra_stops.append(srv.stop())
    report["server_cleanup"] = {
        "server_b": stop_b,
        "other_owned_servers": extra_stops,
        "note": ("every server this run owned is stopped before the residual "
                 "check; a process that cannot be stopped keeps its handle and "
                 "is reported truthfully by no_leftover_server_process"),
    }

    # ── 10. no secret anywhere in the report ────────────────────────────────
    probe_text = json.dumps(report, ensure_ascii=False)
    leaked = secret_value in probe_text
    check("no_secret_in_report", not leaked,
          f"secret_found_in_report={leaked} (the PG password must never appear in "
          f"the report, config, argv or raw payloads)")

    # ── 11. residual processes ─────────────────────────────────────────────
    residual = [
        {"label": s.label, "port": s.port, "pid": (s.proc.pid if s.proc else None),
         "alive": s.alive()}
        for s in OWNED_SERVERS
    ]
    report["residual_processes"] = residual
    check("no_leftover_server_process", all(not r["alive"] for r in residual),
          f"residual={residual}")

    # ── 12. explicit coverage gaps for what M01/M02 does not claim ──────────
    gap("no model answered the fresh-session question: the driver asserts on the recall "
        "block the bridge returns, not on generated text (no LLM / no paid model by design)")
    gap("M03 derived propagation (topic / observer / E1 / QA) is out of scope: a derived lane "
        "may still hold the superseded payload; only the explicit current state is proven")
    gap("M04 temporal / as-of reasoning is out of scope: effective_at is stored and read "
        "back, but no as-of query is implemented or proven")
    gap("no fuzzy / semantic target selection: unknown and archived targets are refused, "
        "there is no ambiguity resolver to prove")

    return finish()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Refused as exc:  # fail-closed refusal from a guard
        print(f"REFUSE: {exc.reason}", file=sys.stderr)
        sys.exit(exc.code or 97)
    except SystemExit:
        raise
    except BaseException as _unexpected:  # noqa: BLE001 — fail closed, never
        # vanish without evidence. KeyboardInterrupt included: a cancelled run
        # must not look like a green one.
        try:
            write_crash_report(_unexpected, _args_out())
            print("CRASH: the driver aborted before finish(); report written",
                  file=sys.stderr)
        finally:
            sys.exit(99)
    finally:
        for _srv in OWNED_SERVERS:
            try:
                _srv.stop()
            except Exception:
                pass
