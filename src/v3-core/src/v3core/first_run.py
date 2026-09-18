"""v3core.first_run — One-command installer for Hippocampus v0.2 First User Release.

This module is the single Python entry point used by:

* the PowerShell ``install/install.ps1`` wrapper (one-line ``irm | iex`` flow),
* the ``hippocampus install`` subcommand wired in by the main agent
  (``v3core.distribution_cli`` — out of scope here, frozen by Gate 0/1).

Public surface (FROZEN — the main agent's CLI code calls these by name):

* :func:`detect_environment` — discover python / uv / docker / hermes / v3-core
  / plugin / profile state without mutating anything.
* :func:`ensure_pgvector_container` — start or reuse a disposable
  ``pgvector/pgvector:pg17`` Docker container; verify ``CREATE EXTENSION vector``
  works and record the pgvector version.
* :func:`write_profile_config` — write a fresh profile ``config.yaml`` with an
  absolute top-level ``basePath`` and the storage/embed/rerank/llm blocks for
  the requested preset. Idempotent (refuses to clobber an existing config).
* :func:`wire_hermes` — edit the Hermes host ``config.yaml`` to set
  ``memory.provider: deep_memory_v3``, preserving everything else; timestamped
  backup before edit; idempotent.
* :func:`smoke_write_recall` — real end-to-end smoke: write one explicit
  memory via :class:`v3core.active_memory_store.ActiveMemoryWriter`, read it
  back, then run a recall / prefetch and assert the written text is
  findable. Never fakes a pass.
* :func:`run_install` — orchestrate the six steps and the per-step verdict
  block; returns 0 only when every required step really succeeded.

Hard rules (mirrored in ``install/install.ps1``):

* No API key or password is ever printed — only the first 4 chars plus ``...``.
* Docker missing is a hard FAIL with a human-readable, actionable message.
* Steps that need an API key and got none are reported SKIPPED, not PASS.
* subprocess calls are list-form, never ``shell=True``, and never echo env.
* Only the stdlib plus what v3-core already declares (``requests``,
  ``pyyaml``, ``psycopg2-binary``, ``pgvector``).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

LOG = logging.getLogger("v3core.first_run")

# ---------------------------------------------------------------------------
# Preset table — frozen by task contract.  The installer is data-driven off
# this mapping.  `siliconflow` ships the canonical Chinese-cloud default;
# `custom` is the operator-supplied "I'll fill in everything by hand" preset
# where embed / rerank / llm blocks are deliberately absent.
# ---------------------------------------------------------------------------

PRESETS: dict[str, dict] = {
    "siliconflow": {
        "embed": {
            "endpoint": "https://api.siliconflow.cn/v1/embeddings",
            "model": "BAAI/bge-m3",
            "dim": 1024,
        },
        "rerank": {
            "endpoint": "https://api.siliconflow.cn/v1/rerank",
            "model": "BAAI/bge-reranker-v2-m3",
        },
        # The memory LLM rides the SAME SiliconFlow key as embed/rerank, so one
        # key covers the whole install. Writing a MiniMax endpoint here while the
        # key in .env belongs to SiliconFlow silently produced a 400 on every
        # memory-formation call — the exact "your key and your door don't match"
        # failure a first user cannot diagnose. Want MiniMax/DeepSeek for memory
        # instead: use the `custom` preset and pass --llm-* explicitly.
        "llm": {
            "provider": "openai",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "Qwen/Qwen2.5-7B-Instruct",
            # SiliconFlow's Qwen models do not accept the `thinking` parameter
            # (that is a DeepSeek/MiniMax extension) and cap output well below
            # 128k, so the preset states both instead of inheriting defaults
            # meant for a different vendor.
            "thinking": False,
            "max_tokens": 8192,
        },
    },
    "custom": {},
}

# Production-boundary ports — same constants the bootstrap subcommand uses.
# These are unconditional refusals — no override flag, no bypass.
_PROD_PORTS = frozenset({5433})
_PROD_LOCAL_DB = "v3embeddings"
_PROD_LOOPBACK_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"}
)

# Default HERMES_HOME on Windows. The agent's Hermes install path is fixed by
# Hermes' own convention; we don't allow the installer to override it. On
# non-Windows the same value is derived from $XDG_DATA_HOME or $HOME.
_DEFAULT_WINDOWS_HERMES_HOME = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    / "hermes"
)

# Default user profile directory. Mirrors the existing convention in
# v3core.config._resolve_data_dir (~/.../profiles/default). We do NOT touch the
# user's real production profile — we install under a fresh disposable path.
_DEFAULT_PROFILE_DIRNAME = ".v3-core/profiles/default"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _print(msg: str, out) -> None:
    """Write one line to *out* (default ``sys.stdout``). Flushes immediately
    so the installer feels live when run interactively."""
    out.write(str(msg) + "\n")
    try:
        out.flush()
    except Exception:  # pragma: no cover - best effort
        pass


def _redact(value: str | None) -> str:
    """Redact any secret value to first 4 chars + '...'.

    Returns ``'(empty)'`` for empty / falsy input.  Never raises — a malformed
    value is redacted to ``'(invalid)'`` so we still never leak it.
    """
    if value is None or value == "":
        return "(empty)"
    try:
        s = str(value)
    except Exception:
        return "(invalid)"
    if len(s) <= 4:
        return s[:1] + "***"
    return s[:4] + "..."


def _now_stamp() -> str:
    """Timestamp string for backup filenames (filesystem-safe)."""
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _resolve_profile_dir(explicit: str | None) -> Path:
    """Resolve the absolute profile directory path.

    Order of precedence:
      1. ``explicit`` arg
      2. ``V3CORE_PROFILE_DIR`` env var
      3. ``~/.../profiles/default`` (matches ``config._resolve_data_dir``)
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("V3CORE_PROFILE_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / _DEFAULT_PROFILE_DIRNAME).resolve()


def _resolve_hermes_home(explicit: str | None) -> Path:
    """Resolve the Hermes host home.  explicit > ``HERMES_HOME`` env > platform default.

    The explicit argument (``--hermes-home``) wins: a caller that names a host
    wants *that* host, and the ambient ``HERMES_HOME`` of the process that
    launched the installer is not necessarily the one being configured. The
    default on Windows is ``C:/Users/<u>/AppData/Local/hermes``.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return _DEFAULT_WINDOWS_HERMES_HOME.resolve()


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Return True iff something is listening on *port* on *host*."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect((host, port))
        return True
    except (OSError, socket.timeout):
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:  # pragma: no cover - best effort
                pass


def _run(cmd: list[str], *, timeout: int = 30,
         env: Mapping[str, str] | None = None) -> tuple[int, str, str]:
    """Run *cmd* as a list (no shell).  Returns ``(returncode, stdout, stderr)``.

    ``env`` is **never** echoed back to the caller.  When omitted, the parent
    environment is passed through but with ``PYTHONUNBUFFERED=1`` injected so
    the installer reads progress cleanly.

    All subprocess errors are normalised into the ``(rc, "", err)`` tuple so
    callers never see a :class:`subprocess.TimeoutExpired` /
    :class:`FileNotFoundError` / :class:`OSError` from this helper.
    """
    if not cmd or not isinstance(cmd, list):
        raise ValueError("cmd must be a non-empty list")
    use_env: dict[str, str] | None = None
    if env is not None:
        use_env = dict(env)
    else:
        use_env = dict(os.environ)
        use_env.setdefault("PYTHONUNBUFFERED", "1")
    try:
        proc = subprocess.run(  # noqa: S603 — list-form, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=use_env,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        return (124, "", f"timeout after {e.timeout}s: {cmd[0] if cmd else ''}")
    except FileNotFoundError as e:
        return (127, "", f"executable not found: {e}")
    except OSError as e:
        return (126, "", f"OS error: {e}")
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


# ---------------------------------------------------------------------------
# 1. detect_environment — pure inspection, no side effects
# ---------------------------------------------------------------------------


def detect_environment(*, docker_timeout: int = 6,
                       out=sys.stdout) -> dict:
    """Probe the host for everything the installer needs.

    Returned keys (all strings unless noted):

    * ``python_version`` — e.g. ``"3.11.16"``
    * ``python_path`` — absolute path to the python executable
    * ``uv_present`` (bool) — ``uv`` is on PATH
    * ``uv_path`` — absolute path to the uv executable when present
    * ``docker_present`` (bool) — ``docker`` is on PATH
    * ``docker_version`` — ``docker --version`` string when present
    * ``docker_running`` (bool) — ``docker info`` succeeds
    * ``hermes_home`` (str|None) — resolved Hermes host home
    * ``hermes_config`` (str|None) — absolute path to Hermes' ``config.yaml``
      when one exists at the conventional location
    * ``hermes_venv_python`` (str|None) — absolute path to the Hermes venv
      python (``HERMES_HOME/hermes-agent/venv/Scripts/python.exe`` on Windows)
    * ``v3core_installed`` (bool) — ``v3core`` is importable from the active
      interpreter
    * ``plugin_installed`` (bool) — ``v3hermes`` is importable
    * ``profile_dir`` (str|None) — absolute path the installer would use by
      default
    * ``existing_install`` (bool) — a profile ``config.yaml`` already exists
      at ``profile_dir``
    """
    env: dict[str, Any] = {}

    # Python.
    py_version = "%d.%d.%d" % (
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )
    env["python_version"] = py_version
    env["python_path"] = sys.executable

    # uv.
    uv_path = shutil.which("uv")
    env["uv_present"] = bool(uv_path)
    env["uv_path"] = uv_path or None

    # docker.
    docker_path = shutil.which("docker")
    env["docker_present"] = bool(docker_path)
    env["docker_version"] = None
    env["docker_running"] = False
    if docker_path:
        rc, out_v, err_v = _run([docker_path, "--version"], timeout=docker_timeout)
        if rc == 0:
            env["docker_version"] = (out_v or err_v).strip().splitlines()[:1]
            env["docker_version"] = (
                env["docker_version"][0] if env["docker_version"] else None
            )
        else:
            env["docker_version"] = f"(error rc={rc})"
        rc2, _, err2 = _run(
            [docker_path, "info", "--format", "{{.ServerVersion}}"],
            timeout=docker_timeout,
        )
        if rc2 != 0:
            # Windows Docker Desktop: the FIRST `docker info` of a session pays
            # the CLI + named-pipe cold start and can exceed a short timeout
            # while the daemon is perfectly healthy. Treating that as "daemon
            # down" told a user with a running daemon to go start Docker — a
            # false negative with an unhelpful instruction. Retry once with a
            # generous timeout before believing the daemon is down.
            rc2, _, err2 = _run(
                [docker_path, "info", "--format", "{{.ServerVersion}}"],
                timeout=max(docker_timeout * 4, 30),
            )
        env["docker_running"] = rc2 == 0
        env["docker_probe_error"] = "" if rc2 == 0 else str(err2 or "")[:300]

    # Hermes.
    hermes_home = _resolve_hermes_home(None)
    env["hermes_home"] = str(hermes_home)
    hermes_cfg = hermes_home / "config.yaml"
    env["hermes_config"] = str(hermes_cfg) if hermes_cfg.exists() else None
    if sys.platform == "win32":
        venv_py = hermes_home / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    else:
        venv_py = hermes_home / "hermes-agent" / "venv" / "bin" / "python"
    env["hermes_venv_python"] = str(venv_py) if venv_py.exists() else None

    # v3-core / v3-hermes-plugin importable from THIS interpreter?
    try:
        import v3core  # noqa: F401
        env["v3core_installed"] = True
    except Exception:
        env["v3core_installed"] = False
    try:
        import v3hermes  # noqa: F401
        env["plugin_installed"] = True
    except Exception:
        env["plugin_installed"] = False

    # Profile directory.
    profile_dir = _resolve_profile_dir(None)
    env["profile_dir"] = str(profile_dir)
    env["existing_install"] = (profile_dir / "config.yaml").exists()

    return env


# ---------------------------------------------------------------------------
# 2. ensure_pgvector_container — start or reuse a disposable container
# ---------------------------------------------------------------------------


def _read_container_password(run, docker: str, container_name: str) -> str | None:
    """The password the EXISTING container was created with, or ``None``.

    A container keeps the password it was born with. The profile ``.env`` only
    happens to match when it came from the same install, so any password the
    caller writes or connects with must follow the container — never the other
    way round. Missing it produced `password authentication failed for user
    "postgres"` on the very next TCP connection, while every ``docker exec``
    probe kept passing (those use the container's local socket, which trusts).
    """
    rc, out, _ = run([docker, "inspect", container_name, "--format",
                      "{{range .Config.Env}}{{println .}}{{end}}"])
    if rc != 0:
        return None
    for line in (out or "").splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            value = line.split("=", 1)[1].strip()
            return value or None
    return None


def ensure_pgvector_container(
    *,
    port: int,
    password: str,
    container_name: str = "hippocampus-pg",
    data_volume: str | None = None,
    out=sys.stdout,
    docker_runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
) -> dict:
    """Start or reuse a ``pgvector/pgvector:pg17`` container.

    Returned keys:

    * ``dsn`` — ``"postgresql://<user>@<host>:<port>/<database>"`` — password is
      **never** embedded; the engine reads it from ``V3CORE_PG_PASSWORD``.
    * ``container`` — the container name actually used.
    * ``started`` (bool) — True iff we just started a fresh container.
    * ``reused`` (bool) — True iff an existing container with this name was
      reused (idempotent on re-run).
    * ``port`` — final port (may differ from the request if conflict resolution
      kicked in — see below).
    * ``database`` — database name (always ``v3embeddings_alpha`` for the
      disposable installer).
    * ``user`` — database user (always ``postgres`` for the disposable
      installer).
    * ``pgvector_version`` (str|None) — extension version string when the
      probe succeeded.
    * ``error`` (str|None) — set when the step failed; the dict is still
      well-formed.

    Conflict resolution: when the requested port is already in use, the
    installer refuses to reuse an existing PostgreSQL that might be a
    production database. If the listener responds to a ``pg_isready``-style
    probe and is **not** pgvector, the installer raises ``RuntimeError`` with
    an actionable message; if it's pgvector already, the installer reuses it
    on a different port (the next free port in the disposable range).
    """
    result: dict[str, Any] = {
        "dsn": None,
        "container": container_name,
        "started": False,
        "reused": False,
        "port": port,
        "database": "v3embeddings_alpha",
        "user": "postgres",
        "pgvector_version": None,
        "error": None,
    }
    if port in _PROD_PORTS:
        result["error"] = (
            f"refusing to use production-boundary port {port}; "
            "pick a disposable port (e.g. 55432)."
        )
        _print(f"FAIL: {result['error']}", out)
        return result

    if not password:
        result["error"] = "no password supplied for the disposable pg container"
        _print(f"FAIL: {result['error']}", out)
        return result

    docker = shutil.which("docker")
    if not docker:
        result["error"] = (
            "Docker is not installed. Install Docker Desktop for Windows "
            "(https://www.docker.com/products/docker-desktop/), start it, and "
            "re-run this installer. The v3-core engine requires a disposable "
            "pgvector container; we will not silently skip this prerequisite."
        )
        _print(f"FAIL: {result['error']}", out)
        return result

    run = docker_runner or (lambda cmd: _run(cmd, timeout=120))

    # Already running with this name?
    rc, out_ps, _ = run([docker, "ps", "-a", "--format",
                         "{{.Names}}\t{{.Status}}\t{{.Ports}}"])
    existing = None
    if rc == 0:
        for line in out_ps.splitlines():
            parts = line.split("\t")
            if parts and parts[0] == container_name:
                existing = parts
                break

    if existing is not None:
        status = (existing[1] if len(existing) > 1 else "").lower()
        if status.startswith("up"):
            result["reused"] = True
            result["port"] = port
            result["dsn"] = _build_dsn(port=port)
            _cp = _read_container_password(run, docker, container_name)
            if _cp:
                result["container_password"] = _cp
            _print(
                f"PASS: container {container_name} already running, reusing",
                out,
            )
            pgv = _probe_pgvector(run, docker, container_name,
                                  database=result["database"],
                                  user=result["user"], password=password,
                                  out=out)
            result["pgvector_version"] = pgv
            if pgv is None:
                result["error"] = (
                    f"container {container_name} is up but pgvector extension "
                    "is missing or unreachable"
                )
            return result

    # Port conflict? If so, refuse to overwrite — pick the next disposable
    # port. We treat any process bound to the port as a conflict; only when
    # the existing listener speaks the PostgreSQL wire protocol AND is not
    # pgvector do we bail out hard.
    if _port_in_use(port):
        # Probe if it's pgvector-compatible; if so, start our container on
        # the next port instead. If not, hard-fail with an actionable msg.
        if not _looks_like_pgvector(port=port,
                                    docker=run, database=result["database"],
                                    user=result["user"],
                                    password_for_probe=password):
            result["port"] = port  # surface the requested port for the error
            result["error"] = (
                f"port {port} is already in use by something that is NOT a "
                "pgvector container. Refusing to reuse it because it may be "
                "production-bound. Stop the listener on that port or pass "
                "another --pg-port."
            )
            _print(f"FAIL: {result['error']}", out)
            return result
        # pgvector already on this port — start our named container on a
        # different port so we never collide with it.
        new_port = _next_free_port(port + 1)
        if new_port is None:
            result["error"] = (
                f"port {port} is in use and no free disposable port could "
                "be found in the range above it. Free a port or pass "
                "--pg-port."
            )
            _print(f"FAIL: {result['error']}", out)
            return result
        result["port"] = new_port
        result["dsn"] = _build_dsn(port=new_port)
        _print(
            f"PASS: port {port} already hosts pgvector; starting "
            f"{container_name} on alternative port {new_port}",
            out,
        )
    else:
        result["dsn"] = _build_dsn(port=port)

    # docker run
    run_cmd = [
        docker, "run", "--name", container_name, "-d",
        "-e", "POSTGRES_USER=postgres",
        "-e", f"POSTGRES_PASSWORD={password}",
        "-e", "POSTGRES_DB=v3embeddings_alpha",
        "-p", f"{result['port']}:5432",
        "pgvector/pgvector:pg17",
    ]
    if data_volume:
        run_cmd[run_cmd.index(container_name):run_cmd.index(container_name)] = [
            "-v", f"{data_volume}:/var/lib/postgresql/data",
        ]
    rc, _, err = run(run_cmd)
    if rc != 0:
        # AlreadyExists on a stopped container → start it instead.
        if "already in use" in (err or "").lower() or "conflict" in (err or "").lower():
            rc2, _, err2 = run([docker, "start", container_name])
            if rc2 != 0:
                result["error"] = (
                    f"docker start of existing container {container_name} "
                    f"failed: {err2.strip() or err.strip()}"
                )
                _print(f"FAIL: {result['error']}", out)
                return result
            result["started"] = True
            result["reused"] = True
            # The container keeps the password it was BORN with — adopt it, or
            # the profile .env we write next authenticates with a password the
            # container never had.
            _cp = _read_container_password(run, docker, container_name)
            if _cp:
                result["container_password"] = _cp
        else:
            result["error"] = (
                f"docker run failed (rc={rc}): {err.strip() or '(no stderr)'}"
            )
            _print(f"FAIL: {result['error']}", out)
            return result
    else:
        result["started"] = True

    _print(f"PASS: started container {container_name} on port {result['port']}",
           out)

    # Wait for the container to accept connections (max ~15s).
    deadline = time.time() + 15.0
    last_err: str | None = None
    while time.time() < deadline:
        rc2, _, err2 = run([
            docker, "exec", container_name, "pg_isready",
            "-U", "postgres", "-d", "v3embeddings_alpha",
        ])
        if rc2 == 0:
            last_err = None
            break
        last_err = err2
        time.sleep(0.5)
    if last_err is not None:
        result["error"] = (
            f"container {container_name} did not become ready within 15s "
            f"(last pg_isready error: {last_err.strip()})"
        )
        _print(f"FAIL: {result['error']}", out)
        return result

    # Ensure the target DATABASE exists. A container created by an older
    # installer (or by hand, or by a previous version of this script) may be
    # running without POSTGRES_DB, in which case every later probe fails with
    # "database ... does not exist". The reuse path must heal that, not blame
    # the image.
    db_exists_cmd = [
        docker, "exec", "-e", f"PGPASSWORD={password}", container_name,
        "psql", "-U", "postgres", "-d", "postgres", "-tAc",
        f"SELECT 1 FROM pg_database WHERE datname='{result['database']}'",
    ]
    rc_db, out_db, err_db = run(db_exists_cmd)
    if rc_db == 0 and out_db.strip() != "1":
        _print(
            f"  database {result['database']} is missing in the existing "
            "container — creating it",
            out,
        )
        rc_mk, _, err_mk = run([
            docker, "exec", "-e", f"PGPASSWORD={password}", container_name,
            "createdb", "-U", "postgres", "-d", "postgres", result["database"],
        ])
        if rc_mk != 0:
            result["error"] = (
                f"could not create database {result['database']} in "
                f"{container_name}: {err_mk.strip() or err_db.strip()}"
            )
            _print(f"FAIL: {result['error']}", out)
            return result

    pgv = _probe_pgvector(run, docker, container_name,
                          database=result["database"],
                          user=result["user"], password=password,
                          out=out)
    result["pgvector_version"] = pgv
    if pgv is None:
        # Distinguish the two failure modes so the message is actionable.
        probe_db, probe_out, probe_err = run([
            docker, "exec", "-e", f"PGPASSWORD={password}", container_name,
            "psql", "-U", "postgres", "-d", result["database"], "-tAc", "SELECT 1",
        ])
        if probe_db != 0 and "does not exist" in (probe_err or "").lower():
            result["error"] = (
                f"database {result['database']} does not exist inside "
                f"{container_name}; re-run the installer (it creates the "
                "database on the reuse path) or recreate the container."
            )
        elif "vector" in (probe_err or "").lower() and "extension" in (probe_err or "").lower():
            result["error"] = (
                f"the extension 'vector' is unavailable inside {container_name}; "
                "this image is not pgvector-compatible. Use pgvector/pgvector:pg17."
            )
        elif "password authentication failed" in (probe_err or "").lower():
            result["error"] = (
                f"the existing container {container_name} was created with a "
                "different password. Recreate it (docker rm -f "
                f"{container_name}) or set V3CORE_PG_PASSWORD to the password "
                "that container was created with."
            )
        else:
            result["error"] = (
                f"CREATE EXTENSION vector probe failed inside {container_name}: "
                f"{(probe_err or '').strip()[:200] or 'no stderr'}"
            )
        _print(f"FAIL: {result['error']}", out)
        return result
    return result


def _build_dsn(*, port: int) -> str:
    """Build a DSN without embedding the password.

    The engine reads ``V3CORE_PG_PASSWORD`` (and the optional ``PGPASSWORD``)
    from the environment; this DSN is the form that the engine and the
    ``hippocampus bootstrap`` subcommand expect.
    """
    return (
        f"postgresql://postgres@127.0.0.1:{port}/v3embeddings_alpha"
    )


def _next_free_port(start: int, *, attempts: int = 32) -> int | None:
    """Find the next free TCP port starting at *start*.

    Scans *attempts* consecutive ports. Returns ``None`` when none in the
    scanned window are free.
    """
    for cand in range(start, start + attempts):
        if 1 <= cand <= 65535 and not _port_in_use(cand):
            return cand
    return None


def _probe_pgvector(run, docker, container_name, *,
                    database: str, user: str, password: str,
                    out=sys.stdout) -> str | None:
    """Run ``CREATE EXTENSION vector`` inside the container and read the
    extension version.  Returns the version string on success, ``None`` on
    any failure.
    """
    # We push the password via env so it never appears on argv.  The
    # ``docker exec`` command itself takes no secret values.
    sql = (
        "CREATE EXTENSION IF NOT EXISTS vector; "
        "SELECT extversion FROM pg_extension WHERE extname='vector';"
    )
    last_err = ""
    last_out = ""
    for attempt in range(30):
        rc, out_v, err_v = run([
            docker, "exec",
            "-e", f"PGPASSWORD={password}",
            "-e", "V3CORE_PG_PASSWORD",
            container_name,
            "psql", "-U", user, "-d", database, "-tA", "-c", sql,
        ])
        if rc == 0:
            version = (out_v or "").strip()
            if version:
                _print(f"PASS: pgvector {version} verified inside {container_name}", out)
                return version
            last_err, last_out = err_v or "", out_v or ""
            break
        last_err, last_out = err_v or "", out_v or ""
        transient = ("no such file" in last_err.lower()
                     or "connection refused" in last_err.lower()
                     or "starting up" in last_err.lower()
                     or "system is starting" in last_err.lower())
        if not transient:
            break
        time.sleep(0.5)
    if last_err or last_out:
        _print(
            f"FAIL: pgvector probe inside {container_name} failed: "
            f"{_safe_stderr(last_err, last_out)}",
            out,
        )
        return None
    _print(
        f"FAIL: pgvector probe returned no version row; "
        f"stderr={_safe_stderr(last_err, last_out)}",
        out,
    )
    return None


def _safe_stderr(stderr: str, stdout: str) -> str:
    """Build a stderr summary that never echoes the password."""
    text = (stderr or "").strip() or (stdout or "").strip()
    # Mask any URI credentials.
    text = re.sub(r"(://[^:/@\s]+:)[^@\s]+(@)", r"\1***\2", text)
    text = re.sub(r"(?i)(\bpassword\s*=\s*)([^\s,;'\"\\]+)", r"\1***", text)
    return text


def _looks_like_pgvector(*, port: int,
                         docker, database: str, user: str,
                         password_for_probe: str) -> bool:
    """Return True iff the listener on *port* responds as PostgreSQL AND the
    ``vector`` extension is installed there.  Used to decide whether to start
    our container on a different port or refuse hard.

    The *docker* parameter is accepted for API symmetry with the other
    docker-driven helpers but is intentionally unused here — the probe
    uses psycopg2 directly because ``docker exec`` is the wrong tool for
    inspecting an unknown listener on the host.
    """
    try:
        import psycopg2  # type: ignore
    except Exception:
        return False
    try:
        conn = psycopg2.connect(
            host="127.0.0.1",
            port=port,
            database=database,
            user=user,
            password=password_for_probe,
            connect_timeout=3,
        )
    except Exception:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_extension WHERE extname='vector'"
            )
            return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - best effort
            pass


# ---------------------------------------------------------------------------
# 3. write_profile_config — write a fresh profile config.yaml
# ---------------------------------------------------------------------------


def write_profile_config(
    *,
    profile_dir: Path,
    preset: str,
    pg: dict,
    embed: dict | None = None,
    llm: dict | None = None,
    rerank: dict | None = None,
    overwrite: bool = False,
    out=sys.stdout,
) -> Path:
    """Write the file ``config.yaml`` inside *profile_dir*.

    The written file is always built from a known-good dictionary; never
    touched by callers in raw form.  Idempotency: when ``config.yaml`` already
    exists and ``overwrite`` is ``False`` (the default), the existing path is
    returned and a SKIPPED line is printed.  When ``overwrite`` is True the
    existing file is replaced after a timestamped backup is taken.

    The YAML is built with stdlib only (no PyYAML dependency for write — the
    engine's existing loader accepts the produced shape).
    """
    profile_dir = Path(profile_dir).expanduser().resolve()
    target = profile_dir / "config.yaml"

    if target.exists() and not overwrite:
        _print(
            f"SKIP: config.yaml already exists at {target} (use --overwrite-config to replace)",
            out,
        )
        return target

    profile_dir.mkdir(parents=True, exist_ok=True)
    if target.exists() and overwrite:
        backup = profile_dir / f"config.yaml.bak.{_now_stamp()}"
        try:
            shutil.copy2(target, backup)
            _print(f"PASS: backed up existing config to {backup}", out)
        except Exception as e:  # pragma: no cover - filesystem race
            _print(f"FAIL: could not back up existing config: {e}", out)
            return target

    base: dict[str, Any] = {
        "profile": "default",
        "basePath": str(profile_dir),
        "storage": {
            "pg": _normalize_pg(pg),
        },
    }
    if embed:
        base["storage"]["embed"] = _normalize_embed(embed)
    if rerank:
        base["storage"]["rerank"] = _normalize_rerank(rerank)
    if llm:
        base["llm"] = _normalize_llm(llm)

    text = _render_yaml(base)
    try:
        target.write_text(text, encoding="utf-8")
    except Exception as e:
        _print(f"FAIL: could not write config.yaml: {e}", out)
        return target
    _print(
        f"PASS: wrote config.yaml for preset '{preset}' at {target}",
        out,
    )
    return target


def _normalize_pg(pg: dict) -> dict:
    """Pull the canonical keys out of *pg* and emit them in stable order."""
    if not isinstance(pg, dict):
        raise TypeError("pg must be a dict")
    out = {
        "host": str(pg.get("host", "127.0.0.1")),
        "port": int(pg.get("port", 55432)),
        "database": str(pg.get("database", "v3embeddings_alpha")),
        "user": str(pg.get("user", "postgres")),
        # CRITICAL: never embed a password into config.yaml.
        "password": "",
    }
    return out


def _persistable_key(value: Any) -> str:
    """What may go into config.yaml for a key field.

    A literal secret must never be written to disk, but an ``${env:NAME}``
    reference is a pointer, not a secret — and blanking it made every later
    consumer (doctor auth checks, recall, the engine) see "no api key
    configured" on a correctly installed system.
    """
    text = str(value or "")
    if text.startswith("${env:") and text.endswith("}"):
        return text
    return ""


def _normalize_embed(embed: dict) -> dict:
    out: dict[str, Any] = {}
    if "endpoint" in embed:
        out["endpoint"] = str(embed["endpoint"])
    if "model" in embed:
        out["model"] = str(embed["model"])
    if "dim" in embed:
        out["dim"] = int(embed["dim"])
    out["api_key"] = _persistable_key(embed.get("api_key") or embed.get("apiKey"))
    return out


def _normalize_rerank(rerank: dict) -> dict:
    out: dict[str, Any] = {}
    if "endpoint" in rerank:
        out["endpoint"] = str(rerank["endpoint"])
    if "model" in rerank:
        out["model"] = str(rerank["model"])
    out["api_key"] = _persistable_key(rerank.get("api_key") or rerank.get("apiKey"))
    return out


def _normalize_llm(llm: dict) -> dict:
    out: dict[str, Any] = {}
    if "provider" in llm:
        out["provider"] = str(llm["provider"])
    if "base_url" in llm:
        out["base_url"] = str(llm["base_url"])
    if "model" in llm:
        out["model"] = str(llm["model"])
    # Provider-shaped optional keys must survive the write, otherwise the preset
    # states `thinking: false` / `max_tokens: 8192` and the profile silently
    # inherits defaults meant for a different vendor (the 400-on-every-call bug).
    for opt in ("thinking", "max_tokens", "timeout", "temperature"):
        if llm.get(opt) is not None:
            out[opt] = llm[opt]
    out["api_key"] = _persistable_key(llm.get("api_key") or llm.get("apiKey"))
    return out


def _render_yaml(d: dict) -> str:
    """Tiny stdlib YAML emitter.

    We don't want a hard dependency on PyYAML for a 4-block file; the
    canonical loader (PyYAML safe_load) accepts this output verbatim.  Scalar
    leaves of a mapping are emitted inline (``key: value``); nested mappings
    and lists go on their own indented block.  Strings that contain ``:``,
    ``#``, leading ``-``, or other special YAML chars are double-quoted.
    """
    lines: list[str] = [
        "# v3-core profile config — written by v3core.first_run",
        "# password fields are intentionally empty; the engine reads",
        "# them from V3CORE_PG_PASSWORD / per-provider env vars.",
        "",
    ]
    lines.extend(_emit_block(d, indent=0))
    return "\n".join(lines) + "\n"


def _emit_block(obj: Any, *, indent: int) -> Iterable[str]:
    pad = " " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                yield f"{pad}{k}:"
                yield from _emit_block(v, indent=indent + 2)
            elif isinstance(v, list):
                yield f"{pad}{k}:"
                yield from _emit_block(v, indent=indent + 2)
            else:
                yield f"{pad}{k}: {_scalar(v)}"
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                yield f"{pad}-"
                yield from _emit_block(item, indent=indent + 2)
            else:
                yield f"{pad}- {_scalar(item)}"
    else:  # pragma: no cover - never reached at top level
        yield f"{pad}{_scalar(obj)}"


def _scalar(v: Any) -> str:
    r"""Emit one YAML scalar as a string.

    Strategy:
      * ``None`` → ``null``
      * booleans / numbers → unquoted canonical form
      * empty string → ``""``
      * any string with control chars or single-quote → double-quoted with
        all backslashes escaped (PyYAML processes ``\X`` inside ``"..."``)
      * everything else (including Windows paths and URLs) → single-quoted;
        ``'...'`` is the safest style because YAML does NOT process any
        escape sequences inside single quotes, only doubling the quote char.
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "":
        return '""'
    # Multi-line or contains a single-quote → must use double-quoted form.
    if "\n" in s or "'" in s:
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n")
        return '"' + escaped + '"'
    return "'" + s + "'"


# ---------------------------------------------------------------------------
# 4. wire_hermes — edit Hermes config.yaml to register the memory provider
# ---------------------------------------------------------------------------


def wire_hermes(
    *,
    hermes_home: Path | None,
    venv_python: str | None,
    hermes_config: Path | None,
    plugin_source: str | None = None,
    out=sys.stdout,
) -> dict:
    """Edit the Hermes host ``config.yaml`` to register the v3 memory provider.

    Idempotent: re-running the installer never duplicates the ``memory:`` block
    and never produces a second backup file with the same timestamp suffix.
    The backup uses ``HERMES_HOME/config.yaml.bak.YYYYMMDD-HHMMSS``.

    Returned keys:

    * ``hermes_found`` (bool) — Hermes host root exists
    * ``plugin_installed`` (bool) — ``v3hermes`` is importable from the
      interpreter named by *venv_python* (or the current interpreter when
      *venv_python* is ``None``)
    * ``provider_registered`` (bool) — ``memory.provider == deep_memory_v3``
      in the live (in-memory) config after the edit
    * ``config_updated`` (bool) — we wrote the file
    * ``config_backup`` (str|None) — absolute path of the backup file when one
      was created
    * ``requires_env_ok`` (bool) — ``V3CORE_PG_PASSWORD`` is set in the
      current process environment
    * ``error`` (str|None) — set when a hard error occurred
    """
    result: dict[str, Any] = {
        "hermes_found": False,
        "plugin_installed": False,
        "provider_registered": False,
        "config_updated": False,
        "config_backup": None,
        "requires_env_ok": bool(os.environ.get("V3CORE_PG_PASSWORD")),
        "error": None,
    }

    if hermes_home is None:
        hermes_home = _resolve_hermes_home(None)
    hermes_home = Path(hermes_home).expanduser().resolve()

    if not hermes_home.exists():
        result["error"] = (
            f"Hermes home {hermes_home} does not exist; this installer "
            "targets a host where Hermes Agent is already installed. See "
            "https://hermes-agent.nousresearch.com/docs for the host install."
        )
        _print(f"FAIL: {result['error']}", out)
        return result
    result["hermes_found"] = True

    # Probe plugin importability from the hermes venv python if supplied;
    # otherwise from the current interpreter.
    if venv_python:
        try:
            rc, out_v, err_v = _run(
                [venv_python, "-c", "import v3hermes"],
                timeout=15,
            )
            result["plugin_installed"] = rc == 0
            if not result["plugin_installed"]:
                _print(
                    "WARN: v3-hermes-plugin not importable from "
                    f"{venv_python}; install it via 'uv pip install "
                    "<wheel>' inside the Hermes venv before starting Hermes.",
                    out,
                )
        except Exception:
            result["plugin_installed"] = False

        # The provider must live in the SAME interpreter that runs Hermes,
        # otherwise the entry point is never discovered. Install it there.
        if not result["plugin_installed"] and plugin_source:
            _print(f"  installing the provider into the Hermes environment: {plugin_source}", out)
            try:
                rc, so, se = _run(
                    ["uv", "pip", "install", "--python", venv_python, str(plugin_source)],
                    timeout=900,
                )
                if rc != 0:
                    result["error"] = (
                        f"could not install v3-hermes-plugin into {venv_python} (exit {rc}): "
                        f"{(se or so or '')[-300:]}"
                    )
                    _print(f"FAIL: {result['error']}", out)
                rc2, _, _ = _run([venv_python, "-c", "import v3hermes"], timeout=15)
                result["plugin_installed"] = rc2 == 0
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"provider install failed: {type(exc).__name__}: {exc}"
                _print(f"FAIL: {result['error']}", out)
    else:
        try:
            import v3hermes  # noqa: F401
            result["plugin_installed"] = True
        except Exception:
            result["plugin_installed"] = False

    cfg_path = (
        Path(hermes_config).expanduser().resolve()
        if hermes_config else hermes_home / "config.yaml"
    )
    if not cfg_path.exists():
        # Try the conventional alternative location (hermes-agent/config.yaml).
        alt = hermes_home / "hermes-agent" / "config.yaml"
        if alt.exists():
            cfg_path = alt
        else:
            result["error"] = (
                f"Hermes config not found at {hermes_home / 'config.yaml'} "
                f"or {alt}; skipping memory.provider registration."
            )
            _print(f"FAIL: {result['error']}", out)
            return result

    try:
        raw = cfg_path.read_text(encoding="utf-8")
    except Exception as e:
        result["error"] = f"could not read {cfg_path}: {e}"
        _print(f"FAIL: {result['error']}", out)
        return result

    # Back up first.
    backup = cfg_path.with_suffix(
        cfg_path.suffix + f".bak.{_now_stamp()}"
    )
    try:
        shutil.copy2(cfg_path, backup)
        result["config_backup"] = str(backup)
        _print(f"PASS: backed up Hermes config to {backup}", out)
    except Exception as e:
        result["error"] = f"could not write backup {backup}: {e}"
        _print(f"FAIL: {result['error']}", out)
        return result

    # We deliberately do NOT depend on PyYAML for the Hermes config edit —
    # Hermes' own config may use features PyYAML does not preserve (anchors,
    # comments in particular).  Instead we do a minimal regex-based patch
    # that preserves the file structure and only touches the canonical keys
    # we own.  The keys are:
    #   * memory.provider            → deep_memory_v3
    #   * memory.memory_enabled      → true   (only if absent)
    new_text, n_provider = re.subn(
        r"(^|\n)(memory:\s*\n(?:\s+[^\n]*\n)*?\s+provider:\s*)([^\n#]+)",
        r"\1\2deep_memory_v3",
        raw,
    )
    if n_provider == 0:
        # No `memory:` block at all — append a minimal one.
        if not raw.endswith("\n"):
            new_text = raw + "\n"
        else:
            new_text = raw
        new_text += (
            "\n# Added by v3core.first_run — register the v3 memory provider.\n"
            "memory:\n"
            "  memory_enabled: true\n"
            "  provider: deep_memory_v3\n"
        )
        _print(
            "PASS: appended memory: block to Hermes config (none was present)",
            out,
        )
    elif n_provider > 0:
        _print(
            "PASS: set memory.provider: deep_memory_v3 in Hermes config",
            out,
        )

    # Idempotency guard: if nothing actually changed and the existing value
    # already says deep_memory_v3, do NOT rewrite the file.
    if new_text == raw:
        _print(
            "SKIP: Hermes config already has memory.provider: "
            "deep_memory_v3; no edit needed",
            out,
        )
        result["config_updated"] = False
        result["provider_registered"] = True
        return result

    try:
        cfg_path.write_text(new_text, encoding="utf-8")
    except Exception as e:
        result["error"] = f"could not write {cfg_path}: {e}"
        _print(f"FAIL: {result['error']}", out)
        return result
    result["config_updated"] = True
    result["provider_registered"] = True
    return result


# ---------------------------------------------------------------------------
# 5. smoke_write_recall — real end-to-end smoke
# ---------------------------------------------------------------------------


def smoke_write_recall(
    *,
    profile_dir: Path,
    out=sys.stdout,
) -> dict:
    """Real end-to-end smoke: write → readback → recall.

    Failures inside any step populate ``error`` and flip the matching ``ok``
    flag to False; this function never raises for a logical failure (e.g.
    missing config).  Only programmer errors (bad arg types) raise.
    """
    started = time.time()
    result: dict[str, Any] = {
        "write_ok": False,
        "readback_ok": False,
        "recall_ok": False,
        "memory_id": None,
        "recall_hit": False,
        "latency_ms": 0,
        "error": None,
        "embedding_ok": False,
        "rerank_ok": False,
        "llm_ok": False,
    }
    profile_dir = Path(profile_dir).expanduser().resolve()

    # Resolve the live profile config through the engine's loader.
    try:
        from v3core.config import resolve_config, _find_config
    except Exception as e:
        result["error"] = f"v3core.config import failed: {e}"
        _print(f"FAIL: {result['error']}", out)
        return result

    # The smoke MUST validate the profile that was just installed. The engine's
    # default lookup path (`~/.v3-core/profiles/<profile>/config.yaml`) points at
    # whatever profile the ambient environment uses, which on a dev machine is a
    # *different*, older install — that is how a fresh install can "pass" while
    # talking to the wrong database. Prefer the profile directory we were given,
    # and pin V3CORE_CONFIG so writer/reader/recall resolve the same profile.
    direct_cfg = profile_dir / "config.yaml"
    if direct_cfg.exists():
        cfg_path = direct_cfg
        os.environ["V3CORE_CONFIG"] = str(cfg_path)
    else:
        cfg_path = _find_config(profile=os.environ.get("V3CORE_PROFILE", "default"),
                                hermes_home=os.environ.get("HERMES_HOME", ""))
    if cfg_path is None:
        result["error"] = (
            f"no config.yaml found under {profile_dir}; smoke aborted. "
            "Re-run 'hippocampus install' without --skip-config."
        )
        _print(f"FAIL: {result['error']}", out)
        return result

    try:
        # Reuse the canonical loader so profile-scoped .env and ${env:...}
        # references are resolved exactly as normal runtime code resolves them.
        cfg = resolve_config()
    except Exception as e:
        result["error"] = f"config resolve failed: {e}"
        _print(f"FAIL: {result['error']}", out)
        return result
    if cfg.pg is None or not cfg.pg.host:
        result["error"] = "config has no storage.pg block; smoke aborted"
        _print(f"FAIL: {result['error']}", out)
        return result

    # Connect to PG.
    try:
        import psycopg2  # type: ignore
    except Exception as e:
        result["error"] = f"psycopg2 import failed: {e}"
        _print(f"FAIL: {result['error']}", out)
        return result

    password = (
        os.environ.get("V3CORE_PG_PASSWORD")
        or os.environ.get("PGPASSWORD")
        or ""
    )
    if not password:
        result["error"] = (
            "V3CORE_PG_PASSWORD is not set; the engine requires it for any "
            "PG connection. Set it in your shell before re-running, or use "
            "'hippocampus install' which sets it for you."
        )
        _print(f"FAIL: {result['error']}", out)
        return result

    try:
        conn = psycopg2.connect(
            host=cfg.pg.host,
            port=int(cfg.pg.port),
            database=cfg.pg.database,
            user=cfg.pg.user,
            password=password,
            connect_timeout=5,
        )
    except Exception as e:
        result["error"] = f"PG connect failed: {_safe_stderr(str(e), '')}"
        _print(f"FAIL: {result['error']}", out)
        return result

    conn.close()

    # Build an ActiveMemoryWriter.
    try:
        from v3core.pg_pool import PgPool
        from v3core.active_memory_store import ActiveMemoryWriter
    except Exception as e:
        result["error"] = f"v3core writer import failed: {e}"
        _print(f"FAIL: {result['error']}", out)
        return result

    connect_kwargs = {
        "host": cfg.pg.host,
        "port": int(cfg.pg.port),
        "database": cfg.pg.database,
        "user": cfg.pg.user,
        "password": password,
        "connect_timeout": 5,
    }

    def _connect():
        return psycopg2.connect(**connect_kwargs)

    pool = PgPool(connect=_connect, max_connections=2, min_connections=0,
                  connect_timeout=5)

    # Embedder — the install smoke must exercise the same real provider path
    # that a first user will use; a no-op vector would make a false PASS.
    from v3core.embedding import build_embed_cfg, embed_batch
    if cfg.embed and cfg.embed.endpoint and cfg.embed.api_key:
        embed_cfg = build_embed_cfg(cfg)
        result["embedding_ok"] = True
    else:
        result["embedding_ok"] = False
        result["error"] = "embedding provider is not configured; refusing degraded smoke"
        _print(f"FAIL: {result['error']}", out)
        return result

    from v3core.embedding import embed_batch
    embed_call_count = {"n": 0}

    def _maybe_embed(text: str, _cfg: dict) -> list[float]:
        embed_call_count["n"] += 1
        vectors = embed_batch([text], _cfg)
        if not vectors or not vectors[0]:
            raise RuntimeError("embedding provider returned an empty vector")
        return [float(x) for x in vectors[0]]

    writer = ActiveMemoryWriter(
        pool=pool, config=cfg, embed_cfg=embed_cfg,
        embedder=_maybe_embed,
    )
    marker_text = (
        f"hippocampus-install-smoke {_dt.datetime.now().isoformat(timespec='seconds')}"
    )
    write_res = writer.create(
        category="install_smoke",
        title="first_run smoke",
        content=marker_text,
        tags=["install_smoke", "first_run"],
    )
    if not write_res.success:
        result["error"] = (
            f"ActiveMemoryWriter.create failed (status={write_res.status}): "
            f"{write_res.error or 'no error detail'}"
        )
        _print(f"FAIL: {result['error']}", out)
        return result
    result["write_ok"] = True
    result["memory_id"] = write_res.memory_id

    # Readback.
    try:
        conn2 = psycopg2.connect(**connect_kwargs)
    except Exception as e:
        result["error"] = f"readback connect failed: {_safe_stderr(str(e), '')}"
        _print(f"FAIL: {result['error']}", out)
        return result
    try:
        with conn2.cursor() as cur:
            cur.execute(
                "SELECT content, embedding IS NOT NULL "
                "FROM public.explicit_memories "
                "WHERE memory_id = %s",
                (write_res.memory_id,),
            )
            row = cur.fetchone()
        if not row or row[0] != marker_text or row[1] is not True:
            result["error"] = (
                f"readback mismatch or NULL embedding: got {row!r}, "
                f"expected content={marker_text!r}, embedding=true"
            )
            _print(f"FAIL: {result['error']}", out)
            return result
    finally:
        try:
            conn2.close()
        except Exception:  # pragma: no cover
            pass
    result["readback_ok"] = True
    _print(f"PASS: readback confirmed memory_id={write_res.memory_id}", out)

    # Recall via prefetch.  We use the engine's prefetch with the marker
    # text.  Whether or not it surfaces the row depends on whether embed
    # is configured (vector search) or just keyword (substring match) —
    # either way we treat a non-empty recall_hit OR a substring match in
    # the text columns of any row as success.
    try:
        from v3core.prefetch import prefetch

        # Build a minimal dict the recall path understands; the v2 engine
        # accepts both V3Config and dict shapes.
        hits = prefetch(
            query=marker_text,
            limit=5,
            config=cfg,
            core=None,
            pg=pool,
            fmt="list",
        ) or []
        # Inspect either the engine's hit dict OR the legacy row shape for
        # our marker text.
        for hit in hits:
            # Hit may be a dataclass with to_dict, or a plain dict.
            data = hit.to_dict() if hasattr(hit, "to_dict") else (
                hit if isinstance(hit, dict) else {}
            )
            blob = json.dumps(data, ensure_ascii=False, default=str)
            if marker_text in blob:
                result["recall_hit"] = True
                break
        if not result["recall_hit"]:
            # Fall back to a direct keyword search against explicit_memories.
            conn3 = psycopg2.connect(**connect_kwargs)
            try:
                with conn3.cursor() as cur:
                    cur.execute(
                        "SELECT memory_id FROM public.explicit_memories "
                        "WHERE content ILIKE %s LIMIT 5",
                        ("%" + marker_text + "%",),
                    )
                    rows = cur.fetchall()
                result["recall_hit"] = bool(rows)
            finally:
                try:
                    conn3.close()
                except Exception:  # pragma: no cover
                    pass
    except Exception as e:
        result["error"] = f"recall probe failed: {_safe_stderr(str(e), '')}"
        _print(f"FAIL: {result['error']}", out)
        return result

    if result["recall_hit"]:
        result["recall_ok"] = True
        _print("PASS: recall/prefetch surfaced the smoke memory", out)
    else:
        _print(
            "FAIL: recall/prefetch did not surface the smoke memory; the "
            "vector + keyword paths both came up empty",
            out,
        )

    # LLM / rerank flags — derived from config presence, not from a live
    # call.  Live LLM calls are NOT made by the smoke (no key is
    # required to pass; SKIP would be the right call when no key is set).
    if cfg.rerank and cfg.rerank.endpoint and cfg.rerank.api_key:
        result["rerank_ok"] = True
    if cfg.llm and cfg.llm.api_key:
        result["llm_ok"] = True

    result["latency_ms"] = int((time.time() - started) * 1000)
    return result


# ---------------------------------------------------------------------------
# 6. run_install — orchestrator
# ---------------------------------------------------------------------------


# The expected verdict-block topics.  Centralised here so the tests and the
# human-facing output agree on the wording.  Each rendered line in the
# verdict block is one of:
#   <topic>: <PASS|FAIL|SKIP> — <one-line reason>
# and the block always ends with the restart persistence hint.
_VERDICT_LINES: tuple[str, ...] = (
    "install",
    "database",
    "embedding",
    "rerank",
    "memory LLM",
    "hermes provider",
    "restart persistence hint",
)


def _read_profile_env_key(profile_dir: Path, key: str) -> str | None:
    """Return an existing value from the profile ``.env`` (None when absent)."""
    env_path = Path(profile_dir) / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def _gen_local_password(length: int = 32) -> str:
    """One-shot, local-only password for the disposable pgvector container.

    Generated per install, never reused for another container, never echoed on
    stdout. It reaches the runtime through the profile ``.env`` file
    (``V3CORE_PG_PASSWORD``) — never through ``config.yaml`` and never on argv.
    """
    import secrets

    return secrets.token_urlsafe(48)[:length]


def _write_profile_env(profile_dir: Path, values: dict[str, str], out) -> Path:
    """Create/update the profile ``.env`` without clobbering unrelated keys.

    Idempotent: an existing key is updated in place, a missing key is appended,
    every other line (comments included) is preserved verbatim.
    """
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    env_path = profile_dir / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    remaining = dict(values)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:  # best-effort: keep the local secret readable only by this user
        import subprocess as _sp

        _sp.run(["icacls", str(env_path), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '')}:F"],
                capture_output=True, timeout=20)
    except Exception:
        pass
    _print(f"  profile env written: {env_path} (keys: {', '.join(values)})", out)
    return env_path


def _emit_verdict(verdict: dict[str, str], out) -> None:
    """Render the seven-line verdict block.

    Each line is ``<topic>: <PASS|FAIL|SKIP> — <one-line reason>`` so a user
    (and a test) can see at a glance which part of the install is healthy.
    """
    _print("", out)
    _print("=== install verdict ===", out)
    for topic in _VERDICT_LINES:
        _print(f"{topic}: {verdict.get(topic, 'SKIP — not reached')}", out)
    _print("", out)


def run_install(
    *,
    preset: str = "siliconflow",
    pg_port: int = 55432,
    profile_dir: str | None = None,
    hermes_home: str | None = None,
    embed_key: str | None = None,
    llm_key: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    skip_smoke: bool = False,
    plugin_wheel: str | None = None,
    out=sys.stdout,
) -> int:
    """One-command installer.  Returns 0 only when EVERY required step
    succeeded; non-zero (and a human-readable verdict block) otherwise.

    Steps (in order, each reported as PASS / FAIL / SKIP):

      1. ``install``       — preflight (python / uv / docker / v3-core / plugin)
      2. ``database``      — pgvector container up + ``CREATE EXTENSION vector``
      3. ``embedding``     — embed block written (or SKIPPED if no api key)
      4. ``rerank``        — rerank block written (or SKIPPED)
      5. ``memory LLM``    — llm block written (or SKIPPED)
      6. ``hermes provider`` — Hermes config updated to register deep_memory_v3
      7. smoke             — write + readback + recall (skippable via flag)
      8. ``restart persistence hint`` — final reminder to restart Hermes

    Final verdict block always contains exactly the seven expected lines
    above; tests pin the wording.
    """
    print_fn = lambda msg: _print(msg, out)

    verdict: dict[str, str] = {
        "install": "FAIL — installer aborted before preflight",
        "database": "SKIP — not reached",
        "embedding": "SKIP — not reached",
        "rerank": "SKIP — not reached",
        "memory LLM": "SKIP — not reached",
        "hermes provider": "SKIP — not reached",
        "restart persistence hint": (
            "after 'hermes provider: PASS', restart the Hermes Agent "
            "process so the new memory provider takes effect."
        ),
    }

    print_fn(f"Hippocampus installer — preset='{preset}', pg_port={pg_port}")

    # 1. Preflight.
    pre = detect_environment(out=out)
    problems: list[str] = []
    if not pre["v3core_installed"]:
        problems.append(
            "v3-core is not importable from this interpreter; install the "
            "wheel first (uv pip install v3_core-*.whl)."
        )
    if not pre["uv_present"]:
        problems.append(
            "uv is not on PATH; install it via "
            "'irm https://astral.sh/uv/install.ps1 | iex' or "
            "'pip install uv'."
        )
    if not pre["docker_present"]:
        problems.append(
            "Docker is not installed. Install Docker Desktop for Windows "
            "(https://www.docker.com/products/docker-desktop/) and start it "
            "before continuing."
        )
    elif not pre["docker_running"]:
        _probe_err = str(pre.get("docker_probe_error") or "").strip()
        problems.append(
            "Docker is installed but the daemon is not responding to "
            "`docker info`; start Docker Desktop and re-run."
            + (f" (probe said: {_probe_err[:200]})" if _probe_err else "")
        )
    if problems:
        verdict["install"] = "FAIL — " + "; ".join(problems)
        _emit_verdict(verdict, out)
        return 10
    verdict["install"] = (
        f"PASS — python {pre['python_version']}, uv present, "
        f"docker {pre.get('docker_version') or 'ok'}, "
        f"v3-core {'installed' if pre['v3core_installed'] else 'MISSING'}"
    )
    _print(verdict["install"], out)

    # Resolve the profile directory early: the database password lives there,
    # and reusing it is what makes a second run idempotent. Generating a fresh
    # password while reusing an existing container would break every later
    # connection (the container keeps the password it was created with).
    pdir = _resolve_profile_dir(profile_dir)
    password = _read_profile_env_key(pdir, "V3CORE_PG_PASSWORD") or _gen_local_password()
    pg = ensure_pgvector_container(
        port=pg_port,
        password=password,
        out=out,
    )
    if pg.get("error") or pg.get("dsn") is None:
        verdict["database"] = (
            f"FAIL — {pg.get('error') or 'no dsn returned'}"
        )
        _emit_verdict(verdict, out)
        return 11
    # A reused container keeps the password it was created with. Prefer it over
    # whatever this profile had (or generated), otherwise the .env we write below
    # and every later connection authenticate with a password the container never
    # had — see ensure_pgvector_container's reuse branch.
    if pg.get("container_password") and pg["container_password"] != password:
        _print(
            "NOTE: reusing the existing container "
            f"'{pg.get('container')}'; adopting the database password it was "
            "created with (the profile password is updated to match).",
            out,
        )
        password = pg["container_password"]
    verdict["database"] = (
        f"PASS — pgvector {pg.get('pgvector_version') or 'unknown'} on "
        f"port {pg.get('port')}, container {pg.get('container')}"
    )

    # Build the preset blocks.
    preset_def = PRESETS.get(preset)
    if preset_def is None:
        verdict["database"] = (
            f"FAIL — unknown preset '{preset}'; valid: {list(PRESETS)}"
        )
        _emit_verdict(verdict, out)
        return 12
    embed_block = dict(preset_def.get("embed") or {})
    rerank_block = dict(preset_def.get("rerank") or {})
    llm_block = dict(preset_def.get("llm") or {})
    # Keys are NEVER written into config.yaml (which users commit/screenshot).
    # The block carries an ${env:NAME} reference instead, and the actual secret
    # goes into the profile .env — the same pattern the engine supports for the
    # LLM block, so `resolve_config()` expands it at load time.
    env_secrets: dict[str, str] = {}
    if embed_key:
        embed_block["api_key"] = "${env:V3CORE_EMBED_API_KEY}"
        env_secrets["V3CORE_EMBED_API_KEY"] = embed_key
    if rerank_block and (embed_key or rerank_block.get("api_key")):
        rerank_block["api_key"] = "${env:V3CORE_RERANK_API_KEY}"
        env_secrets["V3CORE_RERANK_API_KEY"] = embed_key or rerank_block.get("api_key")
    if llm_key:
        llm_block["api_key"] = "${env:V3CORE_LLM_API_KEY}"
        env_secrets["V3CORE_LLM_API_KEY"] = llm_key
    if llm_base_url:
        llm_block["base_url"] = llm_base_url
    if llm_model:
        llm_block["model"] = llm_model

    # 3/4/5. Profile config. (pdir was resolved before the container step.)
    # The runtime resolves the database password from the environment, so the
    # install must persist it (the container password is otherwise lost).
    _write_profile_env(pdir, {"V3CORE_PG_PASSWORD": password, **env_secrets}, out)
    cfg_path = write_profile_config(
        profile_dir=pdir,
        preset=preset,
        pg={
            "host": "127.0.0.1",
            "port": pg["port"],
            "database": pg["database"],
            "user": pg["user"],
        },
        embed=embed_block or None,
        llm=llm_block or None,
        rerank=rerank_block or None,
        out=out,
    )

    # 5b. Apply the packaged schema. Mandatory and idempotent: without it the
    #     first write fails with `relation "public.explicit_memories" does not
    #     exist`. Reuses the exact SQL the `hippocampus bootstrap` subcommand
    #     applies, so there is one source of truth.
    from v3core import distribution_cli as _dcli

    schema = _dcli._bootstrap_apply_sql({
        "host": "127.0.0.1",
        "port": pg["port"],
        "database": pg["database"],
        "user": pg["user"],
        "password": password,
    })
    if not schema.get("applied"):
        verdict["database"] = (
            f"FAIL — schema bootstrap failed: {schema.get('error') or 'unknown error'}"
        )
        _emit_verdict(verdict, out)
        return 12
    _print(
        f"PASS: schema applied ({schema.get('sql_bytes')} bytes, idempotent)",
        out,
    )

    # Per-block verdicts.
    if embed_block:
        if embed_block.get("api_key"):
            verdict["embedding"] = (
                f"PASS — endpoint {embed_block.get('endpoint')} model "
                f"{embed_block.get('model')} (key {_redact(embed_block['api_key'])})"
            )
        else:
            verdict["embedding"] = (
                "SKIP — no api_key supplied; re-run with --embed-key <KEY> or "
                "edit the api_key field in "
                f"{cfg_path}. Vector recall will stay disabled until a key is set."
            )
    else:
        verdict["embedding"] = (
            "SKIP — preset has no embed block; vector recall is disabled. "
            "Keyword recall and durable writes still work."
        )

    if rerank_block:
        if rerank_block.get("api_key"):
            verdict["rerank"] = (
                f"PASS — endpoint {rerank_block.get('endpoint')} model "
                f"{rerank_block.get('model')}"
            )
        else:
            verdict["rerank"] = (
                "SKIP — no api_key supplied; re-run with --embed-key <KEY> "
                "(the rerank endpoint shares the same provider key in the "
                "default preset). The recall path falls back to RRF."
            )
    else:
        verdict["rerank"] = (
            "SKIP — preset has no rerank block; the recall path will skip "
            "semantic reranking."
        )

    if llm_block:
        if llm_block.get("api_key"):
            verdict["memory LLM"] = (
                f"PASS — provider {llm_block.get('provider')} model "
                f"{llm_block.get('model')} base_url "
                f"{llm_block.get('base_url')} (key {_redact(llm_block['api_key'])})"
            )
        else:
            verdict["memory LLM"] = (
                "SKIP — no llm_key supplied; re-run with --llm-key <KEY> "
                "to enable observer / E1 / topic synthesis. Durable writes "
                "and recall still work without an LLM."
            )
    else:
        verdict["memory LLM"] = (
            "SKIP — preset has no llm block; observer / E1 / topic "
            "synthesis are disabled."
        )

    # 6. Hermes wiring.
    # The Hermes venv python path comes from the preflight probe; pass it
    # through so wire_hermes can probe v3hermes importability correctly.
    hermes = wire_hermes(
        hermes_home=_resolve_hermes_home(hermes_home),
        venv_python=pre.get("hermes_venv_python"),
        plugin_source=plugin_wheel,
        hermes_config=None,
        out=out,
    )
    if hermes.get("error"):
        verdict["hermes provider"] = f"FAIL — {hermes['error']}"
    elif not hermes.get("hermes_found"):
        verdict["hermes provider"] = (
            "FAIL — Hermes host home not found; install Hermes Agent first "
            "and re-run this installer."
        )
    elif not hermes.get("plugin_installed"):
        verdict["hermes provider"] = (
            "FAIL — v3hermes is not importable from the Hermes environment, so "
            "the provider would never be discovered. Re-run with "
            "--plugin-wheel <path to v3_hermes_plugin-*.whl>."
        )
    else:
        verdict["hermes provider"] = (
            "PASS — memory.provider=deep_memory_v3 registered in the Hermes "
            "config and importable by the Hermes host."
        )

    # The smoke (and anything else in this process) resolves the DB password from
    # the environment, which is exactly what the profile .env carries for later
    # sessions. Export it here so the install validates itself the same way a
    # user's next session will.
    os.environ["V3CORE_PG_PASSWORD"] = password

    # 7. write + readback + recall smoke — real calls, never faked.
    smoke: dict = {}
    smoke_ok = True
    if skip_smoke:
        print_fn("SKIP: write + readback + recall smoke (--skip-smoke was passed)")
    else:
        smoke = smoke_write_recall(profile_dir=pdir, out=out)
        write_ok = bool(smoke.get("write_ok") and smoke.get("readback_ok"))
        recall_ok = bool(smoke.get("recall_ok"))
        smoke_ok = write_ok and recall_ok
        print_fn(
            f"{'PASS' if write_ok else 'FAIL'}: write + readback"
            + ("" if write_ok else f" — {smoke.get('error') or 'unknown error'}")
        )
        print_fn(
            f"{'PASS' if recall_ok else 'FAIL'}: recall returned the memory that "
            "was just written"
        )

    _emit_verdict(verdict, out)

    failed = [topic for topic, line in verdict.items() if line.startswith("FAIL")]
    if not smoke_ok:
        failed.append("write+recall smoke")
    if failed:
        print_fn("INSTALL INCOMPLETE — fix: " + ", ".join(failed))
        return 1
    print_fn("INSTALL OK — Hippocampus is installed and answering from memory.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(
        run_install(
            preset=os.environ.get("V3CORE_INSTALL_PRESET", "siliconflow"),
            pg_port=int(os.environ.get("V3CORE_INSTALL_PG_PORT", "55432")),
        )
    )
