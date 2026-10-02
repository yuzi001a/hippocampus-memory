"""v3core.importers.discovery — unified per-host auto discovery (I01).

Bounded, deterministic, read-only, host-specific (§5):

  * no recursion outside the known host roots
  * no fuzzy 'find any sqlite/json' behaviour
  * never prints tokens/keys; connection material is never read here

Resolution precedence for every host:

  1. explicit override (CLI ``--root``)
  2. explicit env var (HERMES_HOME / DSH_HOME / PI_CODING_AGENT_*)
  3. host official default path (platform-aware)
  4. memory-md only: explicit memory files in the current project dir
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

logger = logging.getLogger("v3core.importers.discovery")

HOSTS: tuple[str, ...] = ("hermes", "dsh", "pi", "memory-md")

# Memory files recognised for memory-md auto discovery (current project).
MEMORY_FILE_NAMES: tuple[str, ...] = ("MEMORY.md", "USER.md", "SOUL.md", "AGENTS.md")


@dataclass
class DiscoveredSource:
    """One detected (or missing) host source — §5 output field set."""

    host: str
    root: Path | None
    found: bool = False
    artifacts: list[Path] = field(default_factory=list)
    format: str = ""
    version: str | None = None
    sessions: int = 0
    messages: int | None = None
    oldest: str | None = None
    newest: str | None = None
    capability: str = "production"
    reason: str | None = None
    resolved_from: str = "default"


def _env(env: Mapping[str, str] | None) -> dict[str, str]:
    return dict(os.environ if env is None else env)


def _norm_platform(platform: str | None) -> str:
    if platform:
        return platform
    return "win32" if os.name == "nt" else "posix"


def resolve_host_root(
    host: str,
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> tuple[Path, str]:
    """Return ``(root, origin)`` for a host's default/extra-priority root.

    ``origin`` is 'env:<VAR>' when an env var supplied the root, or
    'default' for the platform default (or 'cwd' for memory-md).
    Raises ValueError for unknown hosts.
    """
    e = _env(env)
    plat = _norm_platform(platform)
    home = Path.home()

    if host == "hermes":
        v = (e.get("HERMES_HOME") or "").strip()
        if v:
            return Path(v), "env:HERMES_HOME"
        if plat == "win32":
            return home / "AppData" / "Local" / "hermes", "default"
        return home / ".hermes", "default"

    if host == "dsh":
        v = (e.get("DSH_HOME") or "").strip()
        if v:
            return Path(v), "env:DSH_HOME"
        return home / ".dsh", "default"

    if host == "pi":
        v = (e.get("PI_CODING_AGENT_SESSION_DIR") or "").strip()
        if v:
            return Path(v), "env:PI_CODING_AGENT_SESSION_DIR"
        v = (e.get("PI_CODING_AGENT_DIR") or "").strip()
        if v:
            return Path(v) / "sessions", "env:PI_CODING_AGENT_DIR"
        return home / ".pi" / "agent" / "sessions", "default"

    if host == "memory-md":
        return Path.cwd(), "cwd"

    raise ValueError(f"unknown host {host!r}; known: {list(HOSTS)!r}")


def _is_readable_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def _is_nonempty_file(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _discover_artifacts(host: str, root: Path) -> list[Path]:
    """Host-specific bounded artifact discovery.

    Always a single file, or a shallow glob inside the *known* layouts —
    never a recursive scan.
    """
    if host == "hermes":
        from .hermes_sessions import HermesSessionImporter

        return HermesSessionImporter().discover(root)

    if host == "dsh":
        from .dsh_sessions import DshSessionImporter

        return DshSessionImporter().discover(root)

    if host == "pi":
        from .pi_sessions import PiSessionImporter

        return PiSessionImporter().discover(root)

    if host == "memory-md":
        found: list[Path] = []
        if _is_readable_file(root) and root.name in MEMORY_FILE_NAMES:
            return [root]
        if root.is_dir():
            for name in MEMORY_FILE_NAMES:
                p = root / name
                if _is_readable_file(p):
                    found.append(p)
        return found

    raise ValueError(f"unknown host {host!r}")


def _dsh_version_from_artifacts(artifacts: list[Path]) -> str | None:
    """Derive the session format version from the file name (session.vN.*)."""
    for p in artifacts:
        name = p.name
        if name.startswith("session.v"):
            token = name[len("session.v"):].split(".", 1)[0]
            if token.isdigit():
                return f"v{token}"
    return None


def _pi_version_from_header(artifacts: list[Path]) -> str | None:
    """Cheap read of the first JSONL line (bounded: one line per file)."""
    import json

    for p in artifacts:
        try:
            with open(p, "r", encoding="utf-8") as f:
                line = f.readline()
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("type") == "session":
            v = obj.get("version")
            if isinstance(v, int):
                return f"v{v}"
    return None


def _hermes_quick_counts(root: Path, artifacts: list[Path]) -> tuple[int, str | None, str | None]:
    """Read-only quick session count + oldest/newest for a state.db.

    Uses a URI read-only connection; failures degrade to (0, None, None).
    """
    db = None
    for p in artifacts:
        if p.name in ("state.db", "hermes_state.db", "sessions.db") or p.suffix == ".db":
            db = p
            break
    if db is None:
        return 0, None, None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            cur = con.cursor()
            n = cur.execute("SELECT count(DISTINCT session_id) FROM messages").fetchone()[0]
            mm = cur.execute("SELECT min(timestamp), max(timestamp) FROM messages").fetchone()
        finally:
            con.close()
        import datetime

        def _iso(v):
            if v is None:
                return None
            try:
                v = float(v)
                if v > 1e12:
                    v /= 1000.0
                return (
                    datetime.datetime.fromtimestamp(v, tz=datetime.timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except Exception:
                return None

        return int(n or 0), _iso(mm[0]), _iso(mm[1])
    except Exception as exc:
        logger.warning("hermes quick counts failed for %s: %s", db, exc)
        return 0, None, None


def discover_host(
    host: str,
    *,
    root: Path | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> DiscoveredSource:
    """Resolve + discover one host. Never raises for missing roots."""
    if host not in HOSTS:
        raise ValueError(f"unknown host {host!r}; known: {list(HOSTS)!r}")

    if root is not None:
        resolved = Path(root)
        origin = "override"
    else:
        resolved, origin = resolve_host_root(host, env=env, platform=platform)

    src = DiscoveredSource(host=host, root=resolved, resolved_from=origin)

    # capability from the registry (honest reporting)
    try:
        from . import get_importer

        src.capability = get_importer(host).capability
    except Exception:
        src.capability = "production"

    if not resolved.exists():
        src.reason = f"root not found: {resolved}"
        return src

    try:
        artifacts = _discover_artifacts(host, resolved)
    except Exception as exc:
        src.reason = f"discovery failed: {exc!r}"
        return src

    # Empty files (0 bytes) are never valid artifacts — a 0-byte SQLite
    # store cannot carry rows, and a 0-byte JSONL carries no lines. Some
    # hosts leave such placeholders behind (observed: ~/.hermes/sessions.db).
    artifacts = [p for p in artifacts if _is_nonempty_file(p)]

    if not artifacts:
        src.reason = f"no readable artifacts under {resolved}"
        return src

    src.found = True
    src.artifacts = artifacts

    if host == "dsh":
        src.format = "jsonl.zstd"
        src.version = _dsh_version_from_artifacts(artifacts)
        src.sessions = len(artifacts)  # one session file = one session
    elif host == "pi":
        src.format = "jsonl"
        src.version = _pi_version_from_header(artifacts)
        src.sessions = len(artifacts)
    elif host == "hermes":
        src.format = "sqlite"
        has_db = any(
            p.name in ("state.db", "hermes_state.db", "sessions.db") for p in artifacts
        )
        if has_db:
            n, oldest, newest = _hermes_quick_counts(resolved, artifacts)
            src.sessions = n
            src.oldest = oldest
            src.newest = newest
        else:
            src.format = "jsonl"  # jsonl/json export path
            src.sessions = len(artifacts)
    elif host == "memory-md":
        src.format = "markdown"
        src.sessions = 0  # markdown files are not sessions
        src.messages = None

    return src


def discover_all(
    hosts: list[str] | None = None,
    *,
    overrides: Mapping[str, Path] | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> list[DiscoveredSource]:
    """Discover every requested host; one host failing never stops others."""
    requested = list(hosts or HOSTS)
    out: list[DiscoveredSource] = []
    for host in requested:
        if host not in HOSTS:
            raise ValueError(f"unknown host {host!r}; known: {list(HOSTS)!r}")
        ov = dict(overrides or {})
        try:
            out.append(
                discover_host(
                    host,
                    root=ov.get(host),
                    env=env,
                    platform=platform,
                )
            )
        except Exception as exc:  # defensive: never let one host abort all
            out.append(
                DiscoveredSource(
                    host=host,
                    root=None,
                    found=False,
                    reason=f"discovery error: {exc!r}",
                )
            )
    return out


__all__ = [
    "HOSTS",
    "MEMORY_FILE_NAMES",
    "DiscoveredSource",
    "resolve_host_root",
    "discover_host",
    "discover_all",
]
