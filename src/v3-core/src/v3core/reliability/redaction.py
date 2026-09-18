"""Secret / path redaction for the Hippocampus reliability layer.

Three primitives, used by every emitter:

  - ``path_label`` — collapse a filesystem path into a safe ``kind/leaf/hash12``
    triple. Full path only emitted under explicit ``debug_paths=True``.
  - ``sanitize_text`` — scrub DSN credentials and common ``key=value`` /
    bearer-token patterns from arbitrary text (logs, error messages,
    SQL bodies, ...).
  - ``is_production_target`` — predicate that mirrors
    ``distribution_cli._is_production_boundary`` exactly. The two rules are
    OR-ed: port ``5433``, *or* loopback host + database ``v3embeddings``.
    A cross-module test pins the equivalence so the upgrade CLI and the
    health CLI never disagree about whether a target is "production".

Why a *predicate* and not a reuse of ``distribution_cli._is_production_boundary``?
The dispatch explicitly says "不要 import distribution_cli" — coupling the
health module to the upgrade CLI's *implementation* would also couple it to
the upgrade CLI's *enforce* side effect (which raises SystemExit). This
file re-implements the predicate in 8 lines; the cross-module test ensures
both files agree.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


# Path labels.

# Loopback host names — kept in sync with distribution_cli.PROD_LOOPBACK_HOSTS
# (pinned by a cross-module test).
LOOPBACK_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"}
)

# Production port — also pinned against distribution_cli.PROD_PORTS = {5433}.
PROD_PORT: int = 5433

# Canonical production database name on loopback hosts.
PROD_LOCAL_DB: str = "v3embeddings"


def path_label(path: str | Path, kind: str, *, debug_paths: bool = False) -> dict[str, Any]:
    """Redact a filesystem path into a safe label triple.

    Default output::

        {"kind": kind, "leaf": "<basename>", "hash12": sha256(str(path))[:12]}

    ``debug_paths=True`` adds a ``"path"`` field with the raw string,
    only enabled when the operator passes an explicit flag.
    """
    p = Path(path)
    out: dict[str, Any] = {
        "kind": kind,
        "leaf": p.name or str(path),
        "hash12": hashlib.sha256(str(path).encode("utf-8", errors="replace")).hexdigest()[:12],
    }
    if debug_paths:
        out["path"] = str(path)
    return out


# Text sanitization.

# DSN userinfo:  scheme://user:password@host  ->  scheme://***@host
_DSN_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]+:[^/@\s]+@")

# Common key=value forms (password=, api_key=, token=, secret=, bearer ...).
# Captured key is kept; value is replaced. We don't try to be clever about
# nested quoting — operators who embed secrets in JSON bodies need a deeper
# fix, not a regex. This catches the 99% case of accidental log leak.
# Two forms are recognized:
#   bearer <space> VALUE       (HTTP Authorization header style)
#   password|api_key|... := VALUE  (URL / config-file style)
_KV_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"secret|token)\b\s*[:=]\s*([^\s,;'\"&)]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._\-+/=]+)")


def sanitize_text(text: str, max_len: int = 240) -> str:
    """Scrub DSN userinfo and ``key=value`` secret patterns.

    Truncates to ``max_len`` (default 240 chars) after scrubbing. Trailing
    ``...`` sentinel is appended to mark truncation, so callers can tell
    a full value from a clipped one.
    """
    if text is None:
        return ""
    s = str(text)
    s = _DSN_USERINFO_RE.sub(r"\1***@", s)
    s = _BEARER_RE.sub(r"bearer=<redacted>", s)
    s = _KV_RE.sub(r"\1=<redacted>", s)
    if len(s) > max_len:
        s = s[: max(0, max_len - 3)] + "..."
    return s


# Production-target predicate.


def is_production_target(host: Any, port: Any, database: Any) -> bool:
    """Return ``True`` if ``(host, port, database)`` describes a production
    target under the same two rules as ``distribution_cli._is_production_boundary``:

      1. ``int(port) == 5433`` — production port, regardless of host.
      2. ``host.lower().strip("[]")`` is one of ``loopback_hosts`` AND
         ``database == "v3embeddings"`` — loopback + canonical prod DB.

    Cross-module equivalence is pinned by ``test_redaction_is_production_*``
    in ``tests/test_reliability_redaction.py`` (the five case table from
    ``test_production_upgrade_contract.test_is_production_boundary_matches_enforce``).
    """
    try:
        p = int(port) if port is not None else 0
    except (TypeError, ValueError):
        p = 0
    if p == PROD_PORT:
        return True
    h = str(host or "").lower().strip("[]")
    d = str(database or "")
    if h in LOOPBACK_HOSTS and d == PROD_LOCAL_DB:
        return True
    return False


__all__ = [
    "LOOPBACK_HOSTS",
    "PROD_PORT",
    "PROD_LOCAL_DB",
    "path_label",
    "sanitize_text",
    "is_production_target",
]