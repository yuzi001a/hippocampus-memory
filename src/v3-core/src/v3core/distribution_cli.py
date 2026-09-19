"""v3core.distribution_cli — ``hippocampus`` console script (Gate 2 deliverable).

Subcommands:
  * ``doctor`` — read-only environment & install sanity check. Never
    opens a database connection in ``--static`` mode. The non-static
    mode (off by default) connects only to an *explicit* non-production
    target via ``--dsn`` (or ``V3CORE_BOOTSTRAP_DSN``) and runs a
    single ``SELECT 1`` plus a tiny pgvector / information_schema probe
    — never embedding / LLM / rerank. Output is JSON on stdout, all
    credentials redacted.
  * ``bootstrap`` — apply the packaged SQL artifacts against a target
    PostgreSQL. REQUIRES an explicit ``--target`` (or
    ``V3CORE_BOOTSTRAP_DSN`` env var). **Production safety is
    unconditional**: any target whose port is ``5433`` is refused;
    additionally, when the host is ``localhost`` / ``127.0.0.1`` /
    ``::1`` AND the database is ``v3embeddings``, the target is
    refused. There is no bypass flag — secrets never appear on argv.

  * ``upgrade`` — additive existing-install closure (v0.2). Dry-run
    emits a canonical plan (exact SQL + plan_sha256 + destructive
    scan + transaction-boundary check) without writing DDL. Apply is
    refused against the production boundary by default; an apply
    against the production boundary requires BOTH
    ``--allow-production-write`` AND ``--confirm-plan-sha <PLAN_SHA>``
    (a two-part confirmation; the sha comes from a matching dry-run).
    There is NO ``--force`` / ``--unsafe`` / ``--no-guard`` shortcut.

Design contract (frozen by Gate 0/1):
  * Does NOT modify any runtime provider / observer / recall / source
    code paths. Pure CLI + config read + packaged-resource load.
  * Does NOT add a hermes-agent dependency.
  * Does NOT call embedding / LLM / rerank.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from importlib import resources
from importlib import metadata as importlib_metadata
from typing import Any

LOG = logging.getLogger("v3core.distribution_cli")

# --- Production safety constants (unconditional — no bypass flag) -----------
# Any target whose port matches PROD_PORTS is refused, regardless of host.
PROD_PORTS = frozenset({5433})
# Local production database name on loopback hosts.
PROD_LOCAL_DB = "v3embeddings"
# Hosts considered loopback for the local-production DB check.
PROD_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"})

# Alpha bootstrap include marker — the canonical sentinel used by
# `alpha_bootstrap.sql` to indicate where a packaged schema artifact must be
# spliced in. The marker captures only a repo-relative `schema/*.sql` path;
# the loader resolves the basename inside the installed v3core package.
_ALPHA_INCLUDE_MARKER = re.compile(
    r"^--\s*>>>?\s*ALPHA_BOOTSTRAP_INCLUDE:\s*(schema/[A-Za-z0-9_.-]+\.sql)\s*<<<\s*$",
    re.MULTILINE,
)

# --- Secret-safety: key names + URI / key=value patterns ---------------------

_SECRET_KEYS = (
    "password",
    "passwd",
    "api_key",
    "apikey",
    "secret",
    "token",
    "bearer",
    "credential",
    "private_key",
)

# URI credential pattern: scheme://user:password@host
_RE_URI_CREDS = re.compile(r"(://[^:/@\s]+:)([^@\s]+)(@)")
# key=value secret pattern inside connection strings
_RE_KV_PASSWORD = re.compile(
    r"(?i)(\b(?:password|passwd)\s*=\s*)([^\s,;'\"\\]+)"
)


def _redact_string_value(value: str) -> str:
    """Redact all known secret patterns inside a free-form string."""
    if not value:
        return value
    out = _RE_URI_CREDS.sub(r"\1***\3", value)
    out = _RE_KV_PASSWORD.sub(r"\1***", out)
    return out


def _redact_value(key: str, value: Any) -> Any:
    """Recursively redact a value based on its parent key name.

    Secret-named keys are masked regardless of where they appear; non-
    scalar values are walked recursively so nested credentials (e.g.
    ``storage.pg.password``, ``storage.embed.apiKey``,
    ``llm.apiKey``) are all caught. URI credentials embedded in
    arbitrary string values are also masked.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value("", v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value("", v) for v in value)
    if isinstance(value, str):
        lkey = (key or "").lower()
        if any(s in lkey for s in _SECRET_KEYS):
            if not value:
                return value
            return "***"
        return _redact_string_value(value)
    return value


def _safe_summary(cfg: Any) -> dict[str, Any]:
    """Project ``cfg`` to a JSON-safe summary with secrets redacted."""
    if cfg is None:
        return {}
    if hasattr(cfg, "to_legacy_dict"):
        try:
            cfg = cfg.to_legacy_dict()
        except Exception:
            return {"_repr": _safe_repr(cfg)}
    if isinstance(cfg, dict):
        return {k: _redact_value(k, v) for k, v in cfg.items()}
    return {"_repr": _safe_repr(cfg)}


def _safe_repr(obj: Any) -> str:
    """Return ``repr(obj)`` with every known secret pattern masked.

    Used so that exceptions raised by ``resolve_config`` / ``psycopg2`` /
    config loaders never echo credentials.
    """
    try:
        text = repr(obj)
    except Exception:
        try:
            text = str(obj)
        except Exception:
            return "<unrepr>"
    text = _RE_URI_CREDS.sub(r"\1***\3", text)
    text = _RE_KV_PASSWORD.sub(r"\1***", text)
    return text


# ---------------------------------------------------------------------------
# Packaged-resource loaders
# ---------------------------------------------------------------------------


def _package_sql(name: str) -> str:
    """Load a packaged SQL artifact via importlib.resources.

    Resolves relative to the installed ``v3core`` package so the
    command works after ``pip install`` with no repo present.
    """
    try:
        return resources.files("v3core.schema").joinpath(name).read_text(
            encoding="utf-8"
        )
    except (ModuleNotFoundError, FileNotFoundError) as e:
        raise FileNotFoundError(
            f"packaged SQL resource v3core.schema/{name} not found: {e}"
        ) from e


def _package_sql_sha256(name: str) -> str:
    text = _package_sql(name)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _expand_alpha_include(sql_text: str) -> str:
    """Replace every canonical alpha include with its packaged SQL body."""

    def _replace(match: re.Match[str]) -> str:
        rel_path = match.group(1)
        if not rel_path.startswith("schema/"):
            raise ValueError(f"unsupported alpha SQL include: {rel_path}")
        return _package_sql(rel_path.removeprefix("schema/"))

    expanded, _ = _ALPHA_INCLUDE_MARKER.subn(_replace, sql_text)
    return expanded


def _plugin_yaml_text() -> str | None:
    """Load the packaged ``v3hermes/plugin.yaml`` if installed alongside."""
    try:
        return (
            resources.files("v3hermes")
            .joinpath("plugin.yaml")
            .read_text(encoding="utf-8")
        )
    except (ModuleNotFoundError, FileNotFoundError):
        return None


def _distribution_version() -> str | None:
    """Return the installed ``v3-core`` distribution version via
    ``importlib.metadata``. Returns ``None`` when not installed (e.g.
    running from a source checkout that hasn't been ``pip install``ed).
    """
    try:
        return importlib_metadata.version("v3-core")
    except importlib_metadata.PackageNotFoundError:
        return None


def _entry_point_lookup(group: str, name: str):
    """Look up a single named entry point by group/name. Returns
    ``None`` when importlib.metadata cannot enumerate entry points
    (very old Python) or when the entry point is absent.
    """
    try:
        eps = importlib_metadata.entry_points()
    except Exception:
        return None
    try:
        selected = list(eps.select(group=group))
    except AttributeError:
        # Python 3.8 fallback (very old); not expected in 3.10+.
        try:
            selected = list(eps.get(group, []))
        except Exception:
            return None
    for ep in selected:
        if getattr(ep, "name", None) == name:
            return ep
    return None


def _entry_point_presence(group: str, name: str) -> dict[str, Any]:
    """Return a JSON-safe summary of whether a given entry point is
    declared by *any* distribution — never invokes the target.
    """
    info: dict[str, Any] = {"present": False}
    ep = _entry_point_lookup(group, name)
    if ep is not None:
        info["present"] = True
        info["group"] = getattr(ep, "group", group)
        info["name"] = getattr(ep, "name", name)
        target = getattr(ep, "value", None) or getattr(ep, "target", None)
        if target is not None:
            info["target"] = target
        dist = getattr(ep, "dist", None)
        if dist is not None:
            try:
                info["distribution"] = getattr(dist, "name", None) or str(dist)
                info["version"] = getattr(dist, "version", None)
            except Exception:
                pass
    return info


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _doctor(args: argparse.Namespace) -> int:
    """Read-only install / environment check.

    Output is a single JSON object on stdout, with secrets redacted.
    Exit code 0 when every required check is OK; 1 when something is
    missing but non-fatal (warnings); 2 when a hard error is detected.

    The mission-report contract is:

      * ``distribution`` — version (via ``importlib.metadata``),
        Python, platform.
      * ``hermes_host`` — provider entry-point presence
        (``hermes_agent.memory_providers / deep_memory_v3``).
      * ``provider`` — provider name (``deep_memory_v3``) + packaged
        ``plugin.yaml`` sha256.
      * ``config`` — config path, profile, parse status, secret-safe
        summary. Skipped in ``--static``.
      * ``database`` — redacted DSN target summary; reachability via
        ``SELECT 1`` against a non-production explicit target only;
        pgvector extension status; required-table presence via
        ``information_schema``. Skipped in ``--static``.
      * ``providers`` — ``CONFIGURED`` / ``NOT CONFIGURED`` for
        embedding / LLM / rerank, derived from the resolved config
        (read-only — no actual call to those providers).

    No network request is made unless the operator explicitly passed a
    non-production DSN, and even then it is exactly one ``SELECT 1``
    plus a tiny introspection probe.
    """
    report: dict[str, Any] = {
        "command": "doctor",
        "static": bool(args.static),
        "checks": {},
        "warnings": [],
        "errors": [],
    }

    # 1. Distribution version + interpreter.
    report["checks"]["distribution"] = {
        "v3_core_version": _distribution_version(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }
    if report["checks"]["distribution"]["v3_core_version"] is None:
        report["warnings"].append(
            "v3-core distribution version not found via importlib.metadata "
            "(running from a source checkout?)"
        )

    # 2. Packaged SQL resources present, well-formed, and the alpha
    # include marker is intact (we expand it at bootstrap time).
    # v0.2 closing round: the packaged artifact set is FOUR files, not
    # two — qa_embedding_chunks.sql and upgrade_v0_2.sql ship in the
    # same package-data set and an install that is missing them cannot
    # repair an existing install. Report every one of them.
    sql_check: dict[str, Any] = {}
    for name in ("alpha_bootstrap.sql", "explicit_memories.sql",
                 "qa_embedding_chunks.sql", "upgrade_v0_2.sql"):
        try:
            text = _package_sql(name)
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            sql_check[name] = {
                "present": True,
                "sha256": sha,
                "size_bytes": len(text.encode("utf-8")),
            }
        except FileNotFoundError as e:
            sql_check[name] = {"present": False, "error": str(e)}
            report["errors"].append(f"missing packaged SQL: {name}")
    if sql_check.get("alpha_bootstrap.sql", {}).get("present"):
        alpha_text = _package_sql("alpha_bootstrap.sql")
        markers = [m.group(1) for m in _ALPHA_INCLUDE_MARKER.finditer(alpha_text)]
        sql_check["alpha_bootstrap.sql"]["include_marker_found"] = bool(markers)
        sql_check["alpha_bootstrap.sql"]["include_markers"] = markers
        if not markers:
            report["warnings"].append(
                "alpha_bootstrap.sql is missing the ALPHA_BOOTSTRAP_INCLUDE "
                "marker; bootstrap will not splice the sidecar artifacts"
            )
        # Every marker must point at a packaged artifact that is really present,
        # otherwise `hippocampus bootstrap` would splice nothing and leave the
        # install without that table.
        for rel in markers:
            base = rel.rsplit("/", 1)[-1]
            if not sql_check.get(base, {}).get("present"):
                report["warnings"].append(
                    f"alpha_bootstrap.sql includes {rel} but that artifact is "
                    f"not present in the installed package"
                )
    report["checks"]["packaged_sql"] = sql_check

    # 3. Packaged v3hermes plugin.yaml (optional but checked here so
    # the doctor contract is complete).
    plugin_yaml = _plugin_yaml_text()
    plugin_check: dict[str, Any] = {"present": plugin_yaml is not None}
    if plugin_yaml is not None:
        plugin_check["sha256"] = hashlib.sha256(
            plugin_yaml.encode("utf-8")
        ).hexdigest()
        m = re.search(r"^name:\s*(\S+)", plugin_yaml, re.MULTILINE)
        plugin_check["name"] = m.group(1) if m else None
        if plugin_check["name"] != "deep_memory_v3":
            report["warnings"].append(
                f"plugin.yaml name is {plugin_check['name']!r}, "
                "expected 'deep_memory_v3'"
            )
    else:
        report["warnings"].append(
            "v3hermes plugin.yaml not packaged — engine install verified "
            "but the Hermes host adapter is missing"
        )
    report["checks"]["plugin_yaml"] = plugin_check

    # 4. Host-side memory-provider entry-point presence (Hermes discovers
    # via ``hermes_agent.memory_providers``).
    report["checks"]["hermes_host"] = {
        "memory_providers": _entry_point_presence(
            "hermes_agent.memory_providers", "deep_memory_v3"
        ),
    }
    if not report["checks"]["hermes_host"]["memory_providers"]["present"]:
        report["warnings"].append(
            "hermes_agent.memory_providers / deep_memory_v3 entry point "
            "is not declared by any installed distribution"
        )

    # 5. Console-script entry points declared by v3-core.
    report["checks"]["entry_points"] = {
        "console_scripts": {
            name: _entry_point_presence("console_scripts", name)
            for name in ("hippocampus", "v3-core")
        }
    }

    # 6. Provider contract: derive CONFIGURED / NOT CONFIGURED from the
    # resolved config. Done BEFORE the config/db blocks so we have the
    # same secret-safe summary to reuse.
    cfg_summary: dict[str, Any] | None = None
    cfg_path: str | None = None
    cfg_profile = os.environ.get("V3CORE_PROFILE", "default")
    cfg_parse_ok: bool | None = None
    cfg_parse_detail: str | None = None

    if args.static:
        report["checks"]["config"] = {
            "skipped": "static mode",
            "path": None,
            "profile": cfg_profile,
            "parsed": None,
        }
    else:
        cfg_path, cfg_summary, cfg_parse_ok, cfg_parse_detail = (
            _doctor_resolve_config(cfg_profile)
        )
        report["checks"]["config"] = {
            "skipped": None,
            "path": cfg_path,
            "profile": cfg_profile,
            "parsed": cfg_parse_ok,
            "detail": cfg_parse_detail,
            "summary": cfg_summary,
        }
        if cfg_parse_ok is False:
            report["warnings"].append(f"config: {cfg_parse_detail or 'not configured'}")

    # 7. Provider CONFIGURED/NOT CONFIGURED.
    def _configured(provider_section: str) -> str:
        if cfg_summary is None:
            return "NOT CONFIGURED"
        # Walk the redacted summary; any value present under the
        # canonical provider section counts as configured.
        node = cfg_summary
        for part in provider_section.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return "NOT CONFIGURED"
        if isinstance(node, dict) and node:
            return "CONFIGURED"
        return "NOT CONFIGURED"

    report["checks"]["providers"] = {
        "embedding": _configured("storage.embed"),
        "llm": _configured("llm"),
        "rerank": _configured("storage.rerank"),
    }

    # 8. Database: redacted DSN target summary + reachability +
    # pgvector + required-table presence. Skipped in --static. Only
    # runs when an explicit non-production target is supplied; without
    # one, doctor reports the section as "not requested" and stays
    # read-only.
    if args.static:
        report["checks"]["database"] = {
            "skipped": "static mode",
        }
    else:
        report["checks"]["database"] = _doctor_probe_database(
            getattr(args, "dsn", None)
        )
        # Promote DB-level errors into the top-level errors list so the
        # exit-code logic sees them.
        db_check = report["checks"]["database"]
        if isinstance(db_check, dict) and db_check.get("error"):
            report["errors"].append(f"database: {db_check['error']}")

    # 9. Final status / exit code.
    if report["errors"]:
        report["status"] = "error"
        rc = 2
    elif report["warnings"]:
        report["status"] = "warn"
        rc = 1
    else:
        report["status"] = "ok"
        rc = 0
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return rc


def _doctor_resolve_config(profile: str) -> tuple[str | None, dict[str, Any] | None, bool, str | None]:
    """Read and parse the selected YAML without applying runtime defaults.

    Doctor must distinguish "no user config" from a resolver-generated
    default mapping; provider CONFIGURED status is based on what the user
    actually configured, not on runtime dataclass defaults.
    """
    try:
        from v3core.config import _find_config  # type: ignore
    except Exception:
        return (None, None, False, "config loader unavailable")
    try:
        cfg_file = _find_config(
            profile,
            hermes_home=os.environ.get("HERMES_HOME", "") or "",
        )  # type: ignore[attr-defined]
    except Exception:
        cfg_file = None
    if cfg_file is None:
        return (None, None, False, "config file not found")
    cfg_path = str(cfg_file)
    try:
        import yaml  # type: ignore
        raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    except Exception as e:
        return (cfg_path, None, False, f"config parse failed: {type(e).__name__}")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return (cfg_path, None, False, "config root must be a mapping")
    return (cfg_path, _safe_summary(raw), True, None)


def _doctor_probe_database(explicit_dsn: str | None) -> dict[str, Any]:
    """Run a tiny read-only probe against an explicit non-production DSN.

    Refuses the production boundary unconditionally; if no DSN is given,
    the section is reported as "not requested" and no connection is
    made.
    """
    if not explicit_dsn:
        return {
            "skipped": "no explicit --dsn supplied; doctor does not "
            "read the active profile's database",
            "target": None,
            "reachable": None,
            "pgvector": None,
            "required_tables": {},
        }
    parsed = _parse_dsn(explicit_dsn)
    try:
        _enforce_production_boundary(parsed)
    except SystemExit as e:
        return {
            "skipped": None,
            "target": _redact_dsn(parsed),
            "reachable": False,
            "pgvector": None,
            "required_tables": {},
            "error": _safe_repr(e),
        }
    redacted = _redact_dsn(parsed)
    try:
        import psycopg2  # type: ignore
    except ImportError as e:
        return {
            "skipped": None,
            "target": redacted,
            "reachable": None,
            "pgvector": None,
            "required_tables": {},
            "error": f"psycopg2 import failed: {e}",
        }
    password = parsed.get("password") or os.environ.get("PGPASSWORD", "") or ""
    try:
        conn = psycopg2.connect(
            host=parsed.get("host", ""),
            port=int(parsed.get("port") or 0),
            database=parsed.get("database", ""),
            user=parsed.get("user", ""),
            password=password,
            connect_timeout=5,
        )
    except Exception as e:
        return {
            "skipped": None,
            "target": redacted,
            "reachable": False,
            "pgvector": None,
            "required_tables": {},
            "error": _safe_repr(e),
        }
    try:
        info: dict[str, Any] = {
            "skipped": None,
            "target": redacted,
            "reachable": True,
            "pgvector": None,
            "required_tables": {},
        }
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            try:
                cur.execute(
                    "SELECT extversion FROM pg_extension "
                    "WHERE extname = 'vector'"
                )
                row = cur.fetchone()
                info["pgvector"] = {
                    "installed": bool(row),
                    "version": row[0] if row else None,
                }
            except Exception as e:
                info["pgvector"] = {"installed": False, "error": _safe_repr(e)}
            for table in ("explicit_memories", "qa_pairs", "topics",
                          "topic_entries", "observation_notes",
                          "conversation_stream", "yin_paragraphs"):
                try:
                    cur.execute(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = %s",
                        (table,),
                    )
                    info["required_tables"][table] = cur.fetchone() is not None
                except Exception as e:
                    info["required_tables"][table] = False
                    # Don't leak credentials via exception text either.
                    LOG.debug("required-table probe failed for %s: %s",
                              table, _safe_repr(e))
        # Only SELECT statements ran; do not commit from a read-only doctor.
        return info
    except Exception as e:
        return {
            "skipped": None,
            "target": redacted,
            "reachable": False,
            "pgvector": None,
            "required_tables": {},
            "error": _safe_repr(e),
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def _parse_dsn(dsn: str) -> dict[str, Any]:
    """Parse a PostgreSQL DSN in ``scheme://[user[:password]@]host[:port]/[database]`` form.

    Returns a dict with any subset of: ``user``, ``password``, ``host``,
    ``port``, ``database``. The password field is populated when
    present in the URL — callers must redact before printing.

    Empty / malformed input raises ``ValueError``; the password never
    appears in any error message.
    """
    if dsn is None:
        raise ValueError("DSN is required")
    raw = dsn.strip()
    if not raw:
        raise ValueError("DSN is empty")
    if any(c in raw for c in ("\n", "\r", "\t", "\x00")):
        raise ValueError("DSN contains illegal whitespace / control chars")
    # Strip scheme. Accept postgres://, postgresql://, or no scheme.
    m = re.match(r"^(?:postgres(?:ql)?://)?(.*)$", raw)
    body = m.group(1) if m else raw
    if not body:
        raise ValueError("DSN body is empty after scheme strip")
    out: dict[str, Any] = {}
    if "@" in body:
        creds, rest = body.rsplit("@", 1)
        if not rest:
            raise ValueError("DSN has credentials but no host")
        if ":" in creds:
            user, password = creds.split(":", 1)
            if not user:
                raise ValueError("DSN has empty user")
            out["user"] = user
            out["password"] = password
        else:
            if not creds:
                raise ValueError("DSN has empty user")
            out["user"] = creds
        body = rest
    if "/" in body:
        hostport, db = body.split("/", 1)
        # db may contain query string — drop it for our purposes.
        db = db.split("?", 1)[0]
        out["database"] = db or ""
    else:
        hostport = body
    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
        # Bracketed IPv6: [::1]:5432
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if not host:
            raise ValueError("DSN has empty host")
        out["host"] = host
        try:
            out["port"] = int(port)
        except ValueError:
            raise ValueError(f"DSN port is not an integer: {port!r}") from None
    else:
        if not hostport:
            raise ValueError("DSN has empty host")
        out["host"] = hostport
    if "port" in out and (out["port"] <= 0 or out["port"] > 65535):
        raise ValueError(f"DSN port out of range: {out['port']}")
    if not out.get("host"):
        raise ValueError("DSN is missing a host")
    return out


def _enforce_production_boundary(parsed: dict[str, Any]) -> None:
    """Refuse production-boundary targets unconditionally.

    Two checks, OR-ed together:
      1. Port is one of ``PROD_PORTS`` (currently ``5433``).
      2. Host is loopback AND database name is the known local
         production database ``v3embeddings``.

    There is no bypass flag — secrets never appear on argv, and
    production is never reachable from bootstrap by accident.
    """
    try:
        port = int(parsed.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if port in PROD_PORTS:
        raise SystemExit(
            f"ERROR: refusing production-boundary port {port}. "
            "Production ports are unconditionally refused by the "
            "hippocampus distribution CLI. Use a disposable port."
        )
    host = str(parsed.get("host", "")).lower().strip("[]")
    db = str(parsed.get("database", ""))
    if host in PROD_LOOPBACK_HOSTS and db == PROD_LOCAL_DB:
        raise SystemExit(
            f"ERROR: refusing local production target {host}/{db}. "
            "Loopback + v3embeddings is unconditionally refused by the "
            "hippocampus distribution CLI. Use a disposable database "
            "name (e.g. v3embeddings_alpha)."
        )


def _is_production_boundary(parsed: dict[str, Any]) -> bool:
    """Return ``True`` if the parsed DSN targets the production boundary.

    Mirrors the two OR-ed rules used by ``_enforce_production_boundary``:
      1. Port is one of ``PROD_PORTS``.
      2. Host is loopback (``PROD_LOOPBACK_HOSTS``) AND database is the
         local production database ``PROD_LOCAL_DB``.

    Pure predicate — never raises. Used by the upgrade subcommand to
    distinguish "default deny" from "explicit two-part confirmation"
    paths without coupling to the SystemExit side-effect of
    ``_enforce_production_boundary``.
    """
    try:
        port = int(parsed.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if port in PROD_PORTS:
        return True
    host = str(parsed.get("host", "")).lower().strip("[]")
    db = str(parsed.get("database", ""))
    if host in PROD_LOOPBACK_HOSTS and db == PROD_LOCAL_DB:
        return True
    return False


def _normalize_plan_target(parsed: dict[str, Any]) -> dict[str, Any]:
    """Return a credential-free, canonical projection of the parsed DSN
    suitable for embedding into a public plan document.

    Output contains only ``host`` / ``port`` / ``database``. The host is
    normalized: stripped of surrounding whitespace, lowercased, and
    unbracketed (``[::1]`` → ``::1``). The port is coerced to ``int``;
    a non-integer / missing port falls back to ``0``. Never raises.

    Critical contract: NEVER include user / password / any credential
    field — the plan is the audit artifact operators compare against,
    and a leaked password would be a security regression.
    """
    host_raw = str(parsed.get("host", "") or "")
    host = host_raw.strip().lower().strip("[]")
    port_raw = parsed.get("port", 0)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 0
    database = parsed.get("database", "")
    return {"host": host, "port": port, "database": database}


def _canonical_json_sha256(obj: Any) -> str:
    """Stable sha256 of a JSON-serializable object.

    Uses ``json.dumps(obj, sort_keys=True, separators=(",", ":"),
    ensure_ascii=False)`` → UTF-8 → sha256 hex. The sort + tight
    separator + ``ensure_ascii=False`` combo guarantees the hash
    depends only on the logical content, not on key ordering or
    Unicode escape policy.
    """
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_sql_comments(sql: str) -> str:
    """Strip ``/* ... */`` (non-nested) and ``-- ...`` line comments.

    Each comment is replaced by a single space (preserves token
    boundaries). ``--`` only counts at the start of a logical token
    (preceded by whitespace or start-of-string) so it does not eat
    substrings inside identifiers.
    """
    # /* ... */ block comments — non-nested (single pass).
    out = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # -- line comments to end-of-line.
    out = re.sub(r"(?m)(^|[\s])--[^\n]*", lambda m: m.group(1) + " ", out)
    return out


def _strip_sql_strings(sql: str) -> str:
    """Replace single-quoted string bodies with ``''`` (preserves the
    paired-empty-quote SQL escape). Handles ``''`` escapes inside the
    string literal. Double-quoted identifiers are left intact.

    State machine: walk the text character by character; on a single
    quote, consume until the matching closing single quote, collapsing
    any ``''`` escape back to ``''`` and replacing everything else
    with empty.
    """
    out_parts: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            # start of a single-quoted string
            out_parts.append("''")
            i += 1
            while i < n:
                if sql[i] == "'":
                    # check escape ''
                    if i + 1 < n and sql[i + 1] == "'":
                        out_parts.append("''")
                        i += 2
                        continue
                    # end of string
                    out_parts.append("'")
                    i += 1
                    break
                # skip string body char
                i += 1
            continue
        out_parts.append(ch)
        i += 1
    return "".join(out_parts)


# Statement whitelist patterns (re.I). See A6 in the dispatch.
_SQL_STMT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"^BEGIN$",
        r"^COMMIT$",
        r"^CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\s+\S+$",
        r"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+[\w.\"]+\s*\(",
        r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+[\w.\"]+\s+ON\s+[\w.\"]+\s+",
        r"^ALTER\s+TABLE\s+[\w.\"]+\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+[\w\"]+\s+",
        r"^INSERT\s+INTO\s+[\w.\"]+\s*\(.+\)\s*VALUES\s*\(.+\)\s+ON\s+CONFLICT\s+\(.+\)\s+DO\s+NOTHING$",
    )
)

# Forbidden keywords (case-insensitive, word-boundary). See A6.
_FORBIDDEN_KEYWORDS: tuple[str, ...] = (
    "DROP",
    "TRUNCATE",
    "DELETE",
    "UPDATE",
    "MERGE",
    "GRANT",
    "REVOKE",
    "COPY",
    "RENAME",
    "REPLACE",
    "ALTER COLUMN",
    "ALTER SYSTEM",
)


def _normalize_sql_stmt(stmt: str) -> str:
    """Collapse whitespace in a SQL statement for whitelist matching."""
    return re.sub(r"\s+", " ", stmt.strip())


def _scan_destructive_sql(sql: str) -> dict[str, Any]:
    """Static destructive-scan + whitelist check on the combined SQL.

    Returns ``{"clean": bool, "violations": [...], "statement_count": int}``.

    Two-stage analysis, both over the comment-stripped, string-stripped
    SQL (so identifiers / string literals cannot smuggle a forbidden
    keyword):

      1. Forbidden-keyword scan (word-boundary, case-insensitive).
         Each hit produces a violation with a 40-char context window
         around the match. ``ON DELETE`` / ``ON UPDATE`` (foreign-key
         referential actions inside CREATE TABLE) are stripped before
         the scan so they don't false-positive the standalone
         ``DELETE`` / ``UPDATE`` keyword detector.
      2. Whitelist scan — every non-empty statement must match one of
         the additive patterns in ``_SQL_STMT_PATTERNS``. Anything else
         is an ``unexpected_statement`` violation.

    ``statement_count`` is the number of non-empty statements seen.
    """
    cleaned = _strip_sql_strings(_strip_sql_comments(sql))
    # Strip referential-action clauses first (FK actions like
    # ``ON DELETE CASCADE``); these are not standalone DELETE/UPDATE
    # statements. Replace with a single space to preserve token boundaries.
    cleaned = re.sub(
        r"(?i)\bON\s+(?:DELETE|UPDATE)\s+(?:CASCADE|RESTRICT|SET\s+NULL|SET\s+DEFAULT|NO\s+ACTION)\b",
        " ",
        cleaned,
    )
    violations: list[dict[str, str]] = []

    # 1) Forbidden keyword scan.
    for kw in _FORBIDDEN_KEYWORDS:
        # word-boundary; for multi-word keys, use a non-capturing group
        # and treat internal whitespace flexibly.
        pattern = r"(?i)(?<!\w)(" + re.escape(kw).replace(r"\ ", r"\s+") + r")(?!\w)"
        for m in re.finditer(pattern, cleaned):
            start, end = m.span()
            ctx_start = max(0, start - 20)
            ctx_end = min(len(cleaned), end + 40)
            snippet = cleaned[ctx_start:ctx_end].strip()
            violations.append({
                "kind": "forbidden_keyword",
                "detail": f"{kw}: {snippet[:80]}",
            })

    # 2) Statement split + whitelist check.
    raw_stmts = [s for s in cleaned.split(";")]
    non_empty: list[str] = [s for s in raw_stmts if s.strip()]
    for stmt in non_empty:
        normalized = _normalize_sql_stmt(stmt)
        if not normalized:
            continue
        if any(p.match(normalized) for p in _SQL_STMT_PATTERNS):
            continue
        violations.append({
            "kind": "unexpected_statement",
            "detail": normalized[:120],
        })

    return {
        "clean": not violations,
        "violations": violations,
        "statement_count": len(non_empty),
    }


def _check_transaction_boundary(sql: str) -> dict[str, Any]:
    """Verify the combined SQL is one atomic BEGIN / COMMIT block.

    Returns ``{single_transaction, begin_count, commit_count,
    first_statement_is_begin, last_statement_is_commit}``.

    ``single_transaction`` is True iff: exactly 1 BEGIN, exactly 1
    COMMIT, the first non-empty statement is BEGIN, and the last
    non-empty statement is COMMIT.
    """
    cleaned = _strip_sql_strings(_strip_sql_comments(sql))
    raw_stmts = [s for s in cleaned.split(";")]
    non_empty: list[str] = [_normalize_sql_stmt(s) for s in raw_stmts if s.strip()]
    begin_count = sum(1 for s in non_empty if s.upper() == "BEGIN")
    commit_count = sum(1 for s in non_empty if s.upper() == "COMMIT")
    first_is_begin = bool(non_empty) and non_empty[0].upper() == "BEGIN"
    last_is_commit = bool(non_empty) and non_empty[-1].upper() == "COMMIT"
    single = (
        begin_count == 1
        and commit_count == 1
        and first_is_begin
        and last_is_commit
    )
    return {
        "single_transaction": single,
        "begin_count": begin_count,
        "commit_count": commit_count,
        "first_statement_is_begin": first_is_begin,
        "last_statement_is_commit": last_is_commit,
    }


def _parse_expected_objects(sql: str) -> dict[str, Any]:
    """Extract the canonical objects the upgrade is expected to add.

    Operates on the comment-stripped SQL WITH strings preserved
    (strings cannot introduce extra CREATE / ALTER statements because
    they are quoted, but we still strip comments so the regexes do not
    see commented-out lines).

    Returns:
      * ``tables`` — sorted unique list of
        ``CREATE TABLE IF NOT EXISTS <name>`` targets.
      * ``added_columns`` — ``{table: [cols...]}`` from
        ``ALTER TABLE <t> ADD COLUMN IF NOT EXISTS <col>`` matches,
        sorted within each table.
      * ``schema_versions_rows`` — list of captured group-1 values
        from the ``INSERT INTO [schema_versions] (...) VALUES ('X'...)``
        pattern (single-quoted string in column-1 position).
    """
    cleaned = _strip_sql_comments(sql)
    tables: set[str] = set()
    for m in re.finditer(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([\w.\"]+)",
        cleaned,
        flags=re.I,
    ):
        tables.add(m.group(1))
    added: dict[str, list[str]] = {}
    for m in re.finditer(
        r"ALTER\s+TABLE\s+([\w.\"]+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+([\w\"]+)",
        cleaned,
        flags=re.I,
    ):
        tbl = m.group(1)
        col = m.group(2)
        added.setdefault(tbl, []).append(col)
    added_sorted = {t: sorted(set(cols)) for t, cols in added.items()}
    schema_versions_rows: list[str] = []
    for m in re.finditer(
        r"INSERT\s+INTO\s+\S*schema_versions\S*\s*\([^)]*\)\s*VALUES\s*\('([^']+)'",
        cleaned,
        flags=re.I,
    ):
        schema_versions_rows.append(m.group(1))
    return {
        "tables": sorted(tables),
        "added_columns": added_sorted,
        "schema_versions_rows": schema_versions_rows,
    }


def _bootstrap_resolve_dsn(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve the explicit bootstrap target DSN.

    The documented path is ``--target <DSN>`` (or the alias
    ``--dsn <DSN>`` or ``V3CORE_BOOTSTRAP_DSN``). Component args
    (--host / --port / --database / --user) are retained for parity
    with ``scripts/bootstrap_alpha_db.py`` but are NOT the
    documented Gate 2 entry path. Production boundary is enforced
    before any other check; there is no override flag.
    """
    explicit = (args.target or args.dsn or "").strip()
    env_dsn = os.environ.get("V3CORE_BOOTSTRAP_DSN", "").strip()
    if explicit:
        parsed = _parse_dsn(explicit)
        _enforce_production_boundary(parsed)
        if not parsed.get("password"):
            parsed["password"] = os.environ.get("PGPASSWORD", "") or ""
        return parsed
    if env_dsn:
        parsed = _parse_dsn(env_dsn)
        _enforce_production_boundary(parsed)
        if not parsed.get("password"):
            parsed["password"] = os.environ.get("PGPASSWORD", "") or ""
        return parsed
    if args.host and args.database and args.user:
        try:
            port = int(args.port) if args.port else 0
        except (TypeError, ValueError):
            port = 0
        parsed = {
            "host": args.host,
            "port": port,
            "database": args.database,
            "user": args.user,
        }
        _enforce_production_boundary(parsed)
        parsed["password"] = os.environ.get("PGPASSWORD", "") or ""
        return parsed
    raise SystemExit(
        "ERROR: an explicit --target <DSN> (or --dsn / "
        "V3CORE_BOOTSTRAP_DSN) is required for bootstrap. Refusing "
        "to run without an explicit connection target."
    )


def _bootstrap_apply_sql(parsed: dict[str, Any]) -> dict[str, Any]:
    """Apply the packaged ``alpha_bootstrap.sql`` (with the explicit
    include marker replaced by the ``explicit_memories.sql`` body)
    against the parsed target.

    Connects with ``connect_timeout=5``; closes the connection in
    ``finally``; never logs or prints the password.
    """
    try:
        import psycopg2  # type: ignore
    except ImportError as e:
        return {
            "applied": False,
            "error": f"psycopg2 import failed: {e}",
        }
    try:
        alpha_text = _package_sql("alpha_bootstrap.sql")
        sql_text = _expand_alpha_include(alpha_text)
    except FileNotFoundError as e:
        return {"applied": False, "error": _safe_repr(e)}
    redacted = _redact_dsn(parsed)
    report: dict[str, Any] = {
        "applied": False,
        "target": redacted,
        "sql_bytes": len(sql_text.encode("utf-8")),
        "include_expanded": sql_text != alpha_text,
    }
    conn = None
    try:
        conn = psycopg2.connect(
            host=parsed.get("host", ""),
            port=int(parsed.get("port") or 0),
            database=parsed.get("database", ""),
            user=parsed.get("user", ""),
            password=parsed.get("password", "") or "",
            connect_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql_text)
            conn.commit()
            report["applied"] = True
            # v0.2 closing round: a FRESH install must end up at the current
            # schema level, not just at the alpha baseline. `upgrade_v0_2.sql`
            # creates `public.schema_versions` (the migration ledger the doctor
            # keys off) plus the idempotent ADD COLUMN IF NOT EXISTS guards, and
            # it carries its own BEGIN/COMMIT, so it cannot be spliced through
            # the include marker — it is applied as a second, separate step.
            # Without this, every brand-new install failed `doctor --full`
            # (`schema_version: fail`, "v0.2 upgrade targets missing").
            try:
                upgrade_text = _package_sql("upgrade_v0_2.sql")
            except FileNotFoundError as e:
                upgrade_text = ""
                report["upgrade_applied"] = False
                report["upgrade_error"] = _safe_repr(e)
            if upgrade_text:
                try:
                    with conn.cursor() as cur:
                        cur.execute(upgrade_text)
                    conn.commit()
                    report["upgrade_applied"] = True
                except Exception as e:  # noqa: BLE001
                    conn.rollback()
                    report["upgrade_applied"] = False
                    report["upgrade_error"] = _safe_repr(e)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        report["error"] = _safe_repr(e)
    return report


def _redact_dsn(parsed: dict[str, Any]) -> dict[str, Any]:
    out = dict(parsed)
    if out.get("password"):
        out["password"] = "***"
    return out


def _bootstrap(args: argparse.Namespace) -> int:
    """Apply packaged SQL to the explicit target.

    Requires either ``--target <DSN>`` (or ``--dsn <DSN>`` /
    ``V3CORE_BOOTSTRAP_DSN``) or the legacy component args. Refuses
    the production boundary unconditionally.
    """
    try:
        parsed = _bootstrap_resolve_dsn(args)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    try:
        sql_hash = _package_sql_sha256("alpha_bootstrap.sql")
    except FileNotFoundError as e:
        print(_safe_repr(e), file=sys.stderr)
        return 2
    result = _bootstrap_apply_sql(parsed)
    out = {
        "command": "bootstrap",
        "target": _redact_dsn(parsed),
        "alpha_bootstrap_sql_sha256": sql_hash,
        "result": result,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    if not result.get("applied"):
        return 1
    return 0


# -----------------------------------------------------------------------
# upgrade — additive existing-install closure (v0.2.1)
# -----------------------------------------------------------------------
#
# Contract (see existing_install_audit.json migration_contract):
#   * Read-only path: ``hippocampus upgrade --target <DSN> --dry-run`` —
#     connects (one SELECT, no writes), emits a canonical plan
#     (exact combined SQL + plan_sha256 + destructive scan +
#     transaction-boundary check), reports every missing additive
#     object, never executes DDL. Exit code 0 when the install is
#     fully covered, 1 when additions would happen (informational),
#     2 on hard error / refusal / destructive SQL detected.
#   * Apply path: ``hippocampus upgrade --target <DSN> --apply`` —
#     transactional, idempotent, refuses production by default. The
#     apply path builds the same canonical plan and re-verifies the
#     plan_sha when one is supplied via --confirm-plan-sha.
#   * Production-boundary apply (port 5433 or loopback+v3embeddings)
#     requires BOTH ``--allow-production-write`` AND
#     ``--confirm-plan-sha <PLAN_SHA>`` together — a two-part
#     confirmation; the sha comes from a matching dry-run. There is
#     no bypass shortcut in this command.
#   * No DROP / TRUNCATE / DELETE / data rewrite — enforced by the
#     static check in `_scan_destructive_sql` on the combined SQL.
#     The upgrade SQL body is `upgrade_v0_2.sql`, which is additive
#     only; any non-additive statement in the combined body makes
#     the plan dirty and the apply path refuses (exit 2, zero DDL).
#   * The qa_embedding_chunks artifact (child-A-owned) is consumed
#     when present in the installed package; the upgrade DOES NOT
#     silently succeed when it is missing on a v0.2 install —
#     dry-run reports the missing artifact and apply exits 2.
#
# The upgrade is intentionally a strict subset of alpha_bootstrap.sql:
# re-running bootstrap after upgrade is safe and a no-op for every
# statement in upgrade_v0_2.sql.

# Canonical additive objects the upgrade is expected to bring to an
# existing install. The dry-run report keys off this list.
_UPGRADE_REQUIRED_TABLES: tuple[str, ...] = (
    "explicit_memories",
    "schema_versions",
)
# Optional tables the upgrade checks for but never adds directly. The
# qa_embedding_chunks table is owned by child A; when child A's
# artifact (v3core.schema.qa_embedding_chunks.sql) is packaged, the
# upgrade consumes it. When it is missing on a v0.2 install, dry-run
# reports it as missing and apply refuses to proceed.
_UPGRADE_OPTIONAL_TABLES: tuple[str, ...] = (
    "qa_embedding_chunks",
)
# Column-level requirements the dry-run diff emits so the operator can
# see exactly what `bootstrap` would (re-)add via the same ALTER ADD
# COLUMN IF NOT EXISTS guards.
_UPGRADE_TABLE_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "qa_pairs": (
        "source_id", "turn_id", "source", "tool_calls", "tool_results",
        "embed_model", "created_at",
    ),
    "topics": ("note_ref", "last_observer_ts", "embed_model"),
    "topic_entries": ("source_qa_id", "embed_model"),
    "observation_notes": ("embed_model",),
    "yin_paragraphs": ("embed_model",),
}


def _package_optional_sql(name: str) -> tuple[bool, str | None, str | None]:
    """Try to load an optional packaged SQL resource (no exception on miss).

    Returns ``(present, text_or_None, sha256_or_None)``. The doctor
    contract is: when an artifact is missing, report it — never raise
    or silently succeed.
    """
    try:
        text = resources.files("v3core.schema").joinpath(name).read_text(
            encoding="utf-8"
        )
    except (ModuleNotFoundError, FileNotFoundError):
        return (False, None, None)
    return (True, text, hashlib.sha256(text.encode("utf-8")).hexdigest())


def _upgrade_required_column_diff(
    parsed: dict[str, Any],
) -> dict[str, list[str]]:
    """For each table in ``_UPGRADE_TABLE_REQUIRED_COLUMNS``, return the
    list of required columns that are missing on the live install.

    Pure information_schema probe — no DDL.
    """
    missing: dict[str, list[str]] = {}
    try:
        import psycopg2  # type: ignore
    except ImportError:
        # Without psycopg2 we can't probe; surface as fully-missing so
        # the operator sees the gap rather than a clean report.
        for tbl, cols in _UPGRADE_TABLE_REQUIRED_COLUMNS.items():
            missing[tbl] = list(cols)
        return missing
    password = parsed.get("password") or os.environ.get(
        "V3CORE_PG_PASSWORD", ""
    ) or os.environ.get("PGPASSWORD", "") or ""
    try:
        conn = psycopg2.connect(
            host=parsed.get("host", ""),
            port=int(parsed.get("port") or 0),
            database=parsed.get("database", ""),
            user=parsed.get("user", ""),
            password=password or "",
            connect_timeout=5,
        )
    except Exception:  # noqa: BLE001
        for tbl, cols in _UPGRADE_TABLE_REQUIRED_COLUMNS.items():
            missing[tbl] = list(cols)
        return missing
    try:
        with conn.cursor() as cur:
            for tbl, cols in _UPGRADE_TABLE_REQUIRED_COLUMNS.items():
                missing_cols: list[str] = []
                for col in cols:
                    try:
                        cur.execute(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_schema='public' "
                            "  AND table_name=%s "
                            "  AND column_name=%s",
                            (tbl, col),
                        )
                        if cur.fetchone() is None:
                            missing_cols.append(col)
                    except Exception:  # noqa: BLE001
                        # A failed SELECT on a missing table aborts the
                        # transaction — rollback so the next probe starts
                        # clean. Treat the row as fully-missing.
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                        missing_cols.append(col)
                if missing_cols:
                    missing[tbl] = missing_cols
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return missing


def _upgrade_table_presence(parsed: dict[str, Any]) -> dict[str, bool]:
    """Return {table_name: present_on_target} for the canonical upgrade
    tables. Never raises; a missing psycopg2 or a connect failure
    reports every table as missing so the operator still gets a clear
    signal.
    """
    out: dict[str, bool] = {}
    for tbl in _UPGRADE_REQUIRED_TABLES + _UPGRADE_OPTIONAL_TABLES:
        out[tbl] = False
    try:
        import psycopg2  # type: ignore
    except ImportError:
        return out
    password = parsed.get("password") or os.environ.get(
        "V3CORE_PG_PASSWORD", ""
    ) or os.environ.get("PGPASSWORD", "") or ""
    try:
        conn = psycopg2.connect(
            host=parsed.get("host", ""),
            port=int(parsed.get("port") or 0),
            database=parsed.get("database", ""),
            user=parsed.get("user", ""),
            password=password or "",
            connect_timeout=5,
        )
    except Exception:  # noqa: BLE001
        return out
    try:
        with conn.cursor() as cur:
            for tbl in list(out):
                try:
                    cur.execute(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema='public' "
                        "  AND table_name=%s",
                        (tbl,),
                    )
                    out[tbl] = cur.fetchone() is not None
                except Exception:  # noqa: BLE001
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    out[tbl] = False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _upgrade_schema_version_row(parsed: dict[str, Any]) -> dict[str, Any]:
    """Return ``{present, version, applied_at}`` for the v0.2 schema_versions
    row, or ``{present: False}`` when the table/row does not exist.
    """
    out: dict[str, Any] = {"present": False}
    try:
        import psycopg2  # type: ignore
    except ImportError:
        return out
    password = parsed.get("password") or os.environ.get(
        "V3CORE_PG_PASSWORD", ""
    ) or os.environ.get("PGPASSWORD", "") or ""
    try:
        conn = psycopg2.connect(
            host=parsed.get("host", ""),
            port=int(parsed.get("port") or 0),
            database=parsed.get("database", ""),
            user=parsed.get("user", ""),
            password=password or "",
            connect_timeout=5,
        )
    except Exception:  # noqa: BLE001
        return out
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT version, applied_at FROM public.schema_versions "
                    "WHERE version='v0.2' LIMIT 1",
                )
                row = cur.fetchone()
            except Exception:  # noqa: BLE001
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                row = None
            if row:
                out = {
                    "present": True,
                    "version": row[0],
                    "applied_at": str(row[1]),
                }
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _upgrade_resolve_dsn(args: argparse.Namespace) -> dict[str, Any]:
    """Mirror ``_bootstrap_resolve_dsn`` for the upgrade subcommand.

    Requires an explicit ``--target`` (or ``--dsn`` /
    ``V3CORE_UPGRADE_DSN``).

    Production-boundary policy (v0.2.1 — two-part confirmation contract):

      * ``--apply`` refuses the production boundary by default. An
        operator MAY authorize a production-boundary apply by passing
        BOTH ``--allow-production-write`` AND ``--confirm-plan-sha
        <PLAN_SHA>`` together. The two flags MUST appear together;
        one alone is a hard error (the operator is forced to read the
        plan from a previous dry-run, hash it, and pass the hash back
        in). No ``--force`` / ``--unsafe`` / ``--no-guard`` shortcut
        exists.

      * ``--dry-run --allow-production-read`` is the single explicit
        opt-in for a SELECT-only diagnostic against a production
        target. The flag is unmistakably named (the word
        "production" appears in the literal), applies ONLY to
        dry-run, and is logged in the dry-run output so the audit
        trail shows the operator chose it. The apply path never
        honors the read flag for authorization — passing it to
        ``--apply`` does not bypass the two-part confirmation.

      * Without ``--allow-production-write``, the apply path
        continues to refuse the production boundary the same way it
        always has (port 5433 or loopback+v3embeddings).

    The production boundary check is implemented by
    ``_is_production_boundary`` (predicate) /
    ``_enforce_production_boundary`` (raise on match).
    """
    explicit = (args.target or args.dsn or "").strip()
    env_dsn = os.environ.get("V3CORE_UPGRADE_DSN", "").strip()
    allow_prod_read = bool(getattr(args, "allow_production_read", False))
    allow_prod_write = bool(getattr(args, "allow_production_write", False))
    confirm_plan_sha = (
        getattr(args, "confirm_plan_sha", None) or ""
    ).strip().lower()
    is_apply = bool(getattr(args, "apply", False))
    parsed: dict[str, Any] | None = None
    if explicit:
        parsed = _parse_dsn(explicit)
    elif env_dsn:
        parsed = _parse_dsn(env_dsn)
    else:
        raise SystemExit(
            "ERROR: an explicit --target <DSN> (or --dsn / "
            "V3CORE_UPGRADE_DSN) is required for upgrade. Refusing "
            "to run without an explicit connection target."
        )
    # Original password fallback stays as-is (post-parse, pre-judgment).
    if not parsed.get("password"):
        parsed["password"] = os.environ.get("PGPASSWORD", "") or ""
    is_prod = _is_production_boundary(parsed)
    if is_apply:
        give_write, give_sha = allow_prod_write, bool(confirm_plan_sha)
        if give_write != give_sha:
            raise SystemExit(
                "ERROR: --allow-production-write and --confirm-plan-sha must be "
                "provided together (production write is a two-part confirmation). "
                "Nothing was written."
            )
        if is_prod and not give_write:
            raise SystemExit(
                "ERROR: refusing production-boundary target in apply mode. "
                "Production apply requires BOTH --allow-production-write AND "
                "--confirm-plan-sha <PLAN_SHA> (from a production dry-run). "
                "Nothing was written."
            )
        if allow_prod_read:
            LOG.warning(
                "upgrade --apply ignores --allow-production-read: "
                "production writes are not authorized by the read flag."
            )
        if give_write:
            parsed["_allow_production_write"] = True
            parsed["_confirm_plan_sha"] = confirm_plan_sha
        parsed["_is_production_target"] = is_prod
        return parsed
    # dry-run path
    if allow_prod_read:
        parsed["_allow_production_read"] = True
    else:
        _enforce_production_boundary(parsed)
    if allow_prod_write or confirm_plan_sha:
        LOG.warning(
            "--allow-production-write/--confirm-plan-sha are apply-mode flags; "
            "ignored in dry-run mode."
        )
    return parsed


def _upgrade_load_combined_sql(
    include_qa_chunks_artifact: bool,
) -> tuple[str, dict[str, Any]]:
    """Load and combine the upgrade SQL files into a single body.

    The upgrade body is ``upgrade_v0_2.sql`` (the additive DDL owned by
    this task) with the ``ALPHA_BOOTSTRAP_INCLUDE`` marker replaced by
    the packaged ``explicit_memories.sql`` body — same single-source
    pattern ``_expand_alpha_include`` already uses.

    When ``include_qa_chunks_artifact`` is True the optional
    ``qa_embedding_chunks.sql`` artifact (when packaged) is spliced in
    BEFORE the single final ``COMMIT;`` of ``upgrade_v0_2.sql`` so the
    entire upgrade — schema_versions row, explicit_memories body, the
    qa_embedding_chunks sidecar, every ADD COLUMN IF NOT EXISTS guard
    — runs as ONE atomic transaction. The previous implementation
    appended the chunk body AFTER ``COMMIT;``, which silently split
    the upgrade into two transactions; the v0.2 contract is one
    transaction per apply, so the schema_versions ledger reflects the
    whole upgrade, not half of it.

    The splicing rule is precise: locate the LAST standalone
    ``COMMIT;`` line in the upgrade body (the one closing the
    transaction) and insert the chunk body immediately before it. If
    no such COMMIT is found (which would mean the SQL file regressed
    away from the explicit BEGIN / COMMIT wrapper), the loader returns
    an error and the apply path refuses to run — fail-closed on a
    silent atomicity loss.
    """
    try:
        upgrade_text = _package_sql("upgrade_v0_2.sql")
    except FileNotFoundError as e:
        return ("", {"error": _safe_repr(e), "applied": False})
    expanded = _expand_alpha_include(upgrade_text)
    out: dict[str, Any] = {
        "upgrade_v0_2_sql_sha256": hashlib.sha256(
            upgrade_text.encode("utf-8")
        ).hexdigest(),
        "expanded_bytes": len(expanded.encode("utf-8")),
        "include_expanded": expanded != upgrade_text,
    }
    # A9: explicit_memories artifact sha256 for the canonical plan.
    # Load failure (FileNotFoundError) records None — the upgrade
    # body itself would have failed earlier in that case, so this is
    # a defensive null-state marker for plan consumers.
    try:
        explicit_text = _package_sql("explicit_memories.sql")
        out["explicit_memories_sql_sha256"] = hashlib.sha256(
            explicit_text.encode("utf-8")
        ).hexdigest()
    except FileNotFoundError:
        out["explicit_memories_sql_sha256"] = None
    if not include_qa_chunks_artifact:
        return (expanded, out)
    present, qa_text, qa_sha = _package_optional_sql(
        "qa_embedding_chunks.sql"
    )
    out["qa_embedding_chunks_sql_present"] = bool(present)
    if not (present and qa_text is not None):
        # Hard refusal: v0.2 install without the chunk artifact.
        # The caller maps this to exit 2 in apply mode and to a
        # missing-artifact signal in dry-run mode.
        out["qa_embedding_chunks_sql_missing"] = True
        return (expanded, out)
    # Splice the chunk body BEFORE the final COMMIT; line. Match a
    # standalone "COMMIT;" on its own line (allowing trailing
    # whitespace) so we don't accidentally hit the word inside a
    # string literal or a comment. The regex is intentionally strict
    # — atomicity is the contract.
    commit_re = re.compile(r"(?m)^[ \t]*COMMIT\s*;[ \t]*(?:\r\n|\n)?$")
    matches = list(commit_re.finditer(expanded))
    if not matches:
        # Fail closed: without an explicit COMMIT we cannot guarantee
        # the chunk splice is atomic with the rest of the upgrade.
        out["error"] = (
            "upgrade_v0_2.sql is missing the explicit COMMIT; "
            "marker; refusing to splice qa_embedding_chunks.sql "
            "without an atomic boundary. Restore the "
            "BEGIN ... COMMIT wrapper in upgrade_v0_2.sql."
        )
        return ("", out)
    last_commit = matches[-1]
    chunk_block = (
        "\n\n-- >>> BEGIN qa_embedding_chunks.sql (child-A artifact) <<<\n"
        + qa_text
        + "\n-- >>> END qa_embedding_chunks.sql <<<\n\n"
    )
    expanded = (
        expanded[: last_commit.start()]
        + chunk_block
        + expanded[last_commit.start():]
    )
    out["qa_embedding_chunks_sql_sha256"] = qa_sha
    out["qa_embedding_chunks_spliced_before_commit"] = True
    out["expanded_bytes"] = len(expanded.encode("utf-8"))
    # Final invariant: the LAST COMMIT; line in the combined body
    # must follow the chunk block, so the entire upgrade remains one
    # transaction. We re-check and surface it explicitly.
    last_commit_after = list(commit_re.finditer(expanded))[-1]
    chunk_end_after = expanded.find("-- >>> END qa_embedding_chunks.sql <<<")
    if chunk_end_after == -1 or last_commit_after.start() <= chunk_end_after:
        out["error"] = (
            "internal: failed to splice qa_embedding_chunks.sql "
            "before the final COMMIT; atomicity would be lost. "
            "Refusing to apply."
        )
        return ("", out)
    return (expanded, out)


def _upgrade_build_plan(
    parsed: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any], str | None]:
    """Build the canonical upgrade plan for ``parsed``.

    Returns ``(plan, meta, sql_text)``:

      * ``plan`` — full canonical plan dict with a stable ``plan_sha256``,
        or ``None`` when the combined SQL could not be loaded.
      * ``meta`` — the raw meta dict from ``_upgrade_load_combined_sql``
        (always populated; on load failure it carries ``{"error", "applied": False}``).
      * ``sql_text`` — the exact bytes that would be applied; ``None`` on
        load failure.

    The plan is deterministic: no timestamps, row counts, or random
    values — only the upgrade body, the live schema probe, and the
    distributable artifacts. Two plans with the same inputs hash the
    same.
    """
    try:
        sql_text, sql_meta = _upgrade_load_combined_sql(
            include_qa_chunks_artifact=True
        )
    except Exception as e:  # noqa: BLE001
        return (None, {"error": _safe_repr(e), "applied": False}, None)
    meta: dict[str, Any] = dict(sql_meta)
    if not sql_text:
        return (None, meta, None)
    # Reuse one probe pass for pre_state — three calls would multiply
    # the round-trip cost on real PG.
    table_presence = _upgrade_table_presence(parsed)
    scan = _scan_destructive_sql(sql_text)
    boundary = _check_transaction_boundary(sql_text)
    expected = _parse_expected_objects(sql_text)
    pre_state = {
        "missing_required_tables": [
            t for t in _UPGRADE_REQUIRED_TABLES if not table_presence.get(t)
        ],
        "missing_required_columns": _upgrade_required_column_diff(parsed),
        "qa_embedding_chunks_table_present": bool(
            table_presence.get("qa_embedding_chunks")
        ),
        "schema_versions_v0_2": _upgrade_schema_version_row(parsed),
    }
    plan: dict[str, Any] = {
        "plan_version": 1,
        "schema_upgrade_version": (
            expected["schema_versions_rows"][0]
            if expected["schema_versions_rows"]
            else None
        ),
        "target": _normalize_plan_target(parsed),
        "distribution": {
            "name": "v3-core",
            "version": _distribution_version() or "unknown",
        },
        "artifacts": {
            "upgrade_v0_2.sql": sql_meta.get("upgrade_v0_2_sql_sha256"),
            "explicit_memories.sql": sql_meta.get(
                "explicit_memories_sql_sha256"
            ),
            "qa_embedding_chunks.sql": sql_meta.get(
                "qa_embedding_chunks_sql_sha256"
            ),
        },
        "combined_sql": {
            "bytes": len(sql_text.encode("utf-8")),
            "sha256": hashlib.sha256(
                sql_text.encode("utf-8")
            ).hexdigest(),
        },
        "destructive_scan": scan,
        "transaction_boundary": boundary,
        "pre_state": pre_state,
        "expected_objects_after_apply": expected,
    }
    plan["plan_sha256"] = _canonical_json_sha256(plan)
    return (plan, meta, sql_text)


def _upgrade_dry_run(args: argparse.Namespace) -> int:
    """Read-only upgrade dry-run. Connects with one SELECT per table,
    reports every missing additive object, never executes DDL.

    Exit codes:
      0 — fully covered, upgrade would be a no-op
      1 — additions would happen (informational)
      2 — hard error / refusal / DSN parse failure / destructive SQL

    When ``--plan-out`` is passed, write the full canonical plan
    (including the exact combined SQL) to that file path before
    printing JSON. When ``--show-sql`` is passed, append the exact
    SQL body after the JSON output, framed by sentinel markers.
    """
    try:
        parsed = _upgrade_resolve_dsn(args)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    # Build the canonical plan first — it re-runs the schema probes,
    # so we reuse its findings to populate the legacy fields below
    # rather than calling each probe a second time.
    plan, plan_meta, plan_sql_text = _upgrade_build_plan(parsed)
    if plan is not None:
        pre = plan["pre_state"]
        # pre_state.missing_required_columns has shape {table: [cols...]}
        table_presence = {
            t: (t not in pre["missing_required_tables"])
            for t in _UPGRADE_REQUIRED_TABLES + _UPGRADE_OPTIONAL_TABLES
        }
        column_diff = pre["missing_required_columns"]
        schema_version = pre["schema_versions_v0_2"]
        # Mark the qa_embedding_chunks table from the live probe.
        table_presence["qa_embedding_chunks"] = bool(
            pre["qa_embedding_chunks_table_present"]
        )
        chunk_present = bool(plan["artifacts"].get("qa_embedding_chunks.sql"))
        chunk_sha = plan["artifacts"].get("qa_embedding_chunks.sql")
        scan_clean = bool(plan["destructive_scan"]["clean"])
    else:
        # Plan build failed — fall back to independent probes for the
        # legacy fields so the operator still sees the gap surface.
        table_presence = _upgrade_table_presence(parsed)
        column_diff = _upgrade_required_column_diff(parsed)
        schema_version = _upgrade_schema_version_row(parsed)
        chunk_present, _, chunk_sha = _package_optional_sql(
            "qa_embedding_chunks.sql"
        )
        scan_clean = False
    missing_required = [
        t for t in _UPGRADE_REQUIRED_TABLES if not table_presence.get(t)
    ]
    missing_columns_total = sum(len(v) for v in column_diff.values())
    chunk_artifact_missing = not chunk_present
    # Determine the recommended operator command (always emit verbatim
    # so it can be copy-pasted).
    redacted_target = _redact_dsn(parsed)
    cmd_dry_run = (
        f"hippocampus upgrade --target {redacted_target.get('host','')}"
        f":{redacted_target.get('port','')}/{redacted_target.get('database','')}"
        " --dry-run"
    )
    cmd_apply = (
        f"hippocampus upgrade --target {redacted_target.get('host','')}"
        f":{redacted_target.get('port','')}/{redacted_target.get('database','')}"
        " --apply"
    )
    plan_sha = plan["plan_sha256"] if plan else None
    out: dict[str, Any] = {
        "command": "upgrade",
        "mode": "dry-run",
        "target": redacted_target,
        "tables_present": table_presence,
        "missing_required_tables": missing_required,
        "missing_required_columns": column_diff,
        "schema_versions_v0_2": schema_version,
        "qa_embedding_chunks_artifact_present": bool(chunk_present),
        "qa_embedding_chunks_artifact_sha256": chunk_sha,
        "qa_embedding_chunks_table_present": bool(
            table_presence.get("qa_embedding_chunks")
        ),
        "would_apply": bool(
            missing_required
            or missing_columns_total > 0
            or not schema_version.get("present")
        ),
        "plan": plan,
        "plan_sha256": plan_sha,
    }
    if plan is not None:
        out["final_combined_sql_sha256"] = plan["combined_sql"]["sha256"]
        out["final_combined_sql_bytes"] = plan["combined_sql"]["bytes"]
    else:
        out["plan_error"] = plan_meta.get("error")
    # Recommended commands — production vs non-production target.
    is_prod = _is_production_boundary(parsed)
    plan_sha_literal = plan_sha if plan_sha else "<PLAN_SHA>"
    if is_prod or parsed.get("_allow_production_read"):
        # Production dry-run: the apply path requires two-part confirmation.
        # The plan_sha in the recommendation is the actual sha from this
        # run so the operator can copy-paste the apply verbatim.
        cmd_apply_confirm = (
            f"{cmd_apply} --allow-production-write "
            f"--confirm-plan-sha {plan_sha_literal}"
        )
        out["recommended_commands"] = {
            "dry_run": (
                f"{cmd_dry_run} --allow-production-read "
                "(running now — production SELECT only)"
            ),
            "apply": cmd_apply_confirm,
        }
    else:
        out["recommended_commands"] = {
            "dry_run": cmd_dry_run,
            "apply": cmd_apply,
            "apply_with_plan_confirm": (
                f"{cmd_apply} --allow-production-write "
                f"--confirm-plan-sha {plan_sha_literal}"
            ),
        }
    if parsed.get("_allow_production_read"):
        out["allow_production_read"] = True
    if chunk_artifact_missing:
        out["warning"] = (
            "qa_embedding_chunks.sql artifact is NOT packaged in this "
            "v3-core install. Apply mode will refuse to run; rebuild the "
            "child-A artifact and reinstall before applying."
        )
    # Plan-out side effect (must run before stdout so a write failure
    # aborts the operator-friendly output rather than hiding it after).
    plan_out_path = getattr(args, "plan_out", None)
    if plan_out_path:
        if plan is None or plan_sql_text is None:
            print(
                "ERROR: --plan-out requires a successful plan build; "
                f"plan_meta={plan_meta!r}",
                file=sys.stderr,
            )
            return 2
        plan_payload = {
            "plan": plan,
            "plan_sha256": plan["plan_sha256"],
            "final_combined_sql": plan_sql_text,
        }
        try:
            with open(plan_out_path, "w", encoding="utf-8") as f:
                json.dump(
                    plan_payload,
                    f,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
        except Exception as e:  # noqa: BLE001
            print(
                f"ERROR: failed to write --plan-out to {plan_out_path}: "
                f"{_safe_repr(e)}",
                file=sys.stderr,
            )
            return 2
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    # Show-SQL append after JSON output (so JSON parsing stays clean).
    if getattr(args, "show_sql", False):
        if plan_sql_text is not None:
            sha = plan["combined_sql"]["sha256"] if plan else ""
            print(
                f"\n# ===== FINAL COMBINED SQL (exact bytes, sha256={sha}) ====="
            )
            print(plan_sql_text, end=("" if plan_sql_text.endswith("\n") else "\n"))
            print("# ===== END FINAL COMBINED SQL =====")
    # Hard refusal on destructive scan / plan build failure.
    if plan is None:
        return 2
    if not scan_clean:
        return 2
    if chunk_artifact_missing:
        # Dry-run is informative — still report the missing artifact but
        # do not flag it as a hard error here. Apply is the hard gate.
        return 1 if out["would_apply"] else 0
    return 1 if out["would_apply"] else 0


def _upgrade_apply(args: argparse.Namespace) -> int:
    """Apply the additive existing-install upgrade.

    Transactional / idempotent: the entire body runs in one
    ``BEGIN ... COMMIT`` block (already wrapped in
    ``upgrade_v0_2.sql``). On any exception the transaction is rolled
    back; the schema_versions row is only written on a successful
    COMMIT.

    Refuses production by default (same ``PROD_PORTS`` /
    ``PROD_LOOPBACK_HOSTS`` policy as ``bootstrap``). When the
    qa_embedding_chunks artifact is missing on a v0.2 install, apply
    exits 2 — the operator must rebuild child A first.
    """
    try:
        parsed = _upgrade_resolve_dsn(args)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    chunk_present, _, chunk_sha = _package_optional_sql(
        "qa_embedding_chunks.sql"
    )
    if not chunk_present:
        out = {
            "command": "upgrade",
            "mode": "apply",
            "applied": False,
            "target": _redact_dsn(parsed),
            "error": (
                "qa_embedding_chunks.sql artifact is NOT packaged in this "
                "v3-core install. Refusing to apply the v0.2 upgrade "
                "without the child-A artifact. Reinstall a v3-core build "
                "that ships qa_embedding_chunks.sql, then re-run upgrade."
            ),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    # Build the canonical plan BEFORE touching psycopg2 — the
    # destructive scan + sha-verify check must short-circuit before
    # any connection is opened, so a malformed / mismatched / dirty
    # plan never reaches the write path.
    plan, plan_meta, sql_text = _upgrade_build_plan(parsed)
    confirm = parsed.get("_confirm_plan_sha") or ""
    # Plan-load failure (upgrade_v0_2.sql missing, splice error, etc.)
    if plan is None:
        out = {
            "command": "upgrade",
            "mode": "apply",
            "applied": False,
            "target": _redact_dsn(parsed),
            "error": plan_meta.get("error") or "upgrade_v0_2.sql not found",
            "sql_meta": plan_meta,
            "plan_sha256": None,
            "confirmed_plan_sha": confirm or None,
            "plan_verified": False,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    # Destructive scan refusal (zero DDL on this path).
    if not plan["destructive_scan"]["clean"]:
        out = {
            "command": "upgrade",
            "mode": "apply",
            "applied": False,
            "target": _redact_dsn(parsed),
            "error": (
                "destructive SQL detected; refusing to apply. "
                "Re-run dry-run for the full violation list."
            ),
            "destructive_scan": plan["destructive_scan"],
            "plan_sha256": plan["plan_sha256"],
            "confirmed_plan_sha": confirm or None,
            "plan_verified": False,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    # Plan-sha verification (only when a sha was provided).
    if confirm:
        if plan["plan_sha256"] != confirm:
            out = {
                "command": "upgrade",
                "mode": "apply",
                "applied": False,
                "target": _redact_dsn(parsed),
                "plan_mismatch": True,
                "expected_plan_sha": confirm,
                "current_plan_sha": plan["plan_sha256"],
                "error": (
                    "plan_sha mismatch: the upgrade body or schema "
                    "changed since this dry-run was produced. Re-run "
                    "dry-run and pass the new --confirm-plan-sha."
                ),
                "plan_sha256": plan["plan_sha256"],
                "confirmed_plan_sha": confirm,
                "plan_verified": False,
            }
            print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
            return 2
    plan_verified = bool(confirm) and (plan["plan_sha256"] == confirm)
    try:
        import psycopg2  # type: ignore
    except ImportError as e:
        out = {
            "command": "upgrade",
            "mode": "apply",
            "applied": False,
            "target": _redact_dsn(parsed),
            "error": f"psycopg2 import failed: {e}",
            "plan_sha256": plan["plan_sha256"],
            "confirmed_plan_sha": confirm or None,
            "plan_verified": plan_verified,
            "plan": plan,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    if sql_text is None:  # pragma: no cover — defensive, plan already None
        out = {
            "command": "upgrade",
            "mode": "apply",
            "applied": False,
            "target": _redact_dsn(parsed),
            "error": "upgrade_v0_2.sql not found",
            "sql_meta": plan_meta,
            "plan_sha256": plan["plan_sha256"],
            "confirmed_plan_sha": confirm or None,
            "plan_verified": plan_verified,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    redacted = _redact_dsn(parsed)
    result: dict[str, Any] = {
        "applied": False,
        "target": redacted,
        "sql_bytes": len(sql_text.encode("utf-8")),
    }
    result.update(plan_meta)
    conn = None
    try:
        conn = psycopg2.connect(
            host=parsed.get("host", ""),
            port=int(parsed.get("port") or 0),
            database=parsed.get("database", ""),
            user=parsed.get("user", ""),
            password=parsed.get("password", "") or "",
            connect_timeout=5,
        )
        try:
            # The upgrade body is already wrapped in BEGIN / COMMIT.
            # Re-running it is a no-op thanks to CREATE TABLE IF NOT
            # EXISTS / ADD COLUMN IF NOT EXISTS / INSERT ... ON CONFLICT
            # DO NOTHING. We disable autocommit so the transaction is
            # atomic.
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(sql_text)
            conn.commit()
            result["applied"] = True
        except Exception as e:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            result["error"] = _safe_repr(e)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        result["error"] = _safe_repr(e)
    out = {
        "command": "upgrade",
        "mode": "apply",
        "target": redacted,
        "result": result,
        "plan_sha256": plan["plan_sha256"],
        "confirmed_plan_sha": confirm or None,
        "plan_verified": plan_verified,
        "plan": plan,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("applied") else 1


def _upgrade(args: argparse.Namespace) -> int:
    """Dispatch ``upgrade`` subcommand to dry-run or apply.

    ``--dry-run`` is the default — the explicit ``--apply`` is the only
    path that writes DDL. Mutual exclusion is enforced by argparse
    (``add_mutually_exclusive_group``).
    """
    if getattr(args, "apply", False):
        return _upgrade_apply(args)
    return _upgrade_dry_run(args)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hippocampus",
        description=(
            "Hippocampus distribution console (Gate 2). "
            "Read-only `doctor`; explicit-target `bootstrap`. "
            "Production-boundary DSNs (port 5433 or "
            "loopback/v3embeddings) are refused unconditionally."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser(
        "doctor",
        help="Read-only install / environment sanity check",
    )
    doctor.add_argument(
        "--static",
        action="store_true",
        help="Skip config and database probes; only check packaged "
        "resources, provider entry points and plugin.yaml. Safe in "
        "packaging / CI smoke contexts.",
    )
    doctor.add_argument(
        "--dsn",
        default=None,
        help=(
            "Optional non-production DSN to probe via SELECT 1 / "
            "pgvector / information_schema. Implies a non-static run; "
            "the production boundary is enforced unconditionally."
        ),
    )

    bootstrap = sub.add_parser(
        "bootstrap",
        help=(
            "Apply packaged alpha_bootstrap.sql against an explicit "
            "target. Refuses production-boundary DSNs unconditionally."
        ),
    )
    bootstrap.add_argument(
        "--target",
        default=None,
        help=(
            "Explicit DSN string for the bootstrap target, e.g. "
            "scheme://<user>:<password>@<host>:<port>/<database>. "
            "Either this, --dsn, or V3CORE_BOOTSTRAP_DSN is required."
        ),
    )
    bootstrap.add_argument(
        "--dsn",
        default=None,
        help="Alias for --target (literal DSN).",
    )
    # Legacy component args — retained for parity with
    # scripts/bootstrap_alpha_db.py but NOT the documented Gate 2 path.
    bootstrap.add_argument("--host", default=None)
    bootstrap.add_argument("--port", default=None, type=int)
    bootstrap.add_argument("--database", default=None)
    bootstrap.add_argument("--user", default=None)

    upgrade = sub.add_parser(
        "upgrade",
        help=(
            "Additive existing-install upgrade (v0.2.1): dry-run reports "
            "missing canonical objects and emits a canonical plan (incl. "
            "exact SQL + plan_sha256); --apply runs the transactional, "
            "idempotent, no-data-rewrite upgrade body. Production-boundary "
            "DSNs (port 5433 or loopback/v3embeddings) are refused by "
            "default — an apply against the production boundary requires "
            "BOTH --allow-production-write AND --confirm-plan-sha "
            "<PLAN_SHA> (a two-part confirmation; the sha comes from a "
            "matching dry-run). --allow-production-read is dry-run-only "
            "and never authorizes writes."
        ),
    )
    upgrade.add_argument(
        "--target",
        default=None,
        help=(
            "Explicit DSN string for the upgrade target, e.g. "
            "scheme://<user>:<password>@<host>:<port>/<database>. "
            "Either this, --dsn, or V3CORE_UPGRADE_DSN is required."
        ),
    )
    upgrade.add_argument(
        "--dsn",
        default=None,
        help="Alias for --target (literal DSN).",
    )
    upgrade_mode = upgrade.add_mutually_exclusive_group()
    upgrade_mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help=(
            "Read-only path (default): connect, probe information_schema, "
            "emit a canonical plan (incl. exact combined SQL + "
            "plan_sha256), report every missing additive object, never "
            "execute DDL. Exit 0 when the install is fully covered, 1 "
            "when additions would happen (informational), 2 on hard "
            "refusal (including destructive SQL detected)."
        ),
    )
    upgrade_mode.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help=(
            "Apply the transactional, idempotent, no-data-rewrite "
            "upgrade body. Refuses to run against production-boundary "
            "DSNs by default; against the production boundary, apply "
            "requires BOTH --allow-production-write AND "
            "--confirm-plan-sha <PLAN_SHA> (two-part confirmation, sha "
            "from a matching dry-run). Refuses to run when the "
            "qa_embedding_chunks.sql artifact is missing in the "
            "installed v3-core package."
        ),
    )
    upgrade.add_argument(
        "--allow-production-read",
        dest="allow_production_read",
        action="store_true",
        help=(
            "DRY-RUN ONLY: explicit opt-in that allows a SELECT-only "
            "diagnostic against a production-boundary DSN (port 5433 "
            "or loopback/v3embeddings). The flag is REQUIRED for the "
            "dry-run to inspect an existing production install without "
            "any DDL. It is IGNORED by --apply: read authorization "
            "never authorizes writes. Use of this flag is recorded in "
            "the dry-run output so the audit trail shows the operator "
            "chose to point at production."
        ),
    )
    upgrade.add_argument(
        "--allow-production-write",
        dest="allow_production_write",
        action="store_true",
        help=(
            "APPLY ONLY: explicit opt-in (with --confirm-plan-sha) for "
            "a production-boundary additive apply. Never implied by "
            "--allow-production-read. Must be paired with "
            "--confirm-plan-sha <PLAN_SHA> or apply exits 2 with zero "
            "DDL."
        ),
    )
    upgrade.add_argument(
        "--confirm-plan-sha",
        dest="confirm_plan_sha",
        default=None,
        help=(
            "APPLY ONLY: the plan_sha256 from a dry-run; the apply "
            "path recomputes the plan and refuses (exit 2, zero DDL) "
            "unless it matches exactly. Must be paired with "
            "--allow-production-write for a production-boundary target."
        ),
    )
    upgrade.add_argument(
        "--plan-out",
        dest="plan_out",
        default=None,
        help=(
            "DRY-RUN ONLY: write the full canonical plan (including "
            "the exact combined SQL) to this file as JSON. A write "
            "failure exits 2 before any plan is reported."
        ),
    )
    upgrade.add_argument(
        "--show-sql",
        dest="show_sql",
        action="store_true",
        help=(
            "DRY-RUN ONLY: print the exact combined SQL after the JSON "
            "report, framed by '# ===== FINAL COMBINED SQL ... =====' "
            "sentinel markers."
        ),
    )

    # --- v0.2 First User Release surface ------------------------------------
    # These subcommands delegate to dedicated modules; each is imported lazily
    # inside its handler so a partially-updated install still gives a clear
    # message instead of an import error at CLI start-up.
    install = sub.add_parser(
        "install",
        help=(
            "First-user install: environment check, pgvector container, "
            "profile config, Hermes wiring, bootstrap, doctor, write+recall smoke."
        ),
    )
    install.add_argument(
        "--preset",
        default="siliconflow",
        choices=("siliconflow", "custom"),
        help="siliconflow = lowest-friction preset (one embed/rerank key + one LLM key).",
    )
    install.add_argument("--pg-port", type=int, default=55432)
    install.add_argument("--profile-dir", default=None)
    install.add_argument("--hermes-home", default=None)
    install.add_argument("--embed-key", default=None, help="Embedding/rerank API key (never echoed).")
    install.add_argument("--llm-key", default=None, help="Memory-LLM API key (never echoed).")
    install.add_argument("--llm-base-url", default=None)
    install.add_argument("--llm-model", default=None)
    install.add_argument("--skip-smoke", action="store_true")
    install.add_argument("--plugin-wheel", default=None,
                         help="Path to the v3-hermes-plugin wheel (or source directory) so the"
                              " installer can put the provider into the Hermes environment.")

    imp = sub.add_parser(
        "import",
        help="Import existing memory (raw history / user-curated notes / other systems).",
    )
    imp.add_argument(
        "source",
        choices=("list", "hermes", "memory-md", "openclaw", "hindsight"),
        help="'list' shows every registered importer and its honest capability level.",
    )
    imp.add_argument("--root", default=None, help="Directory or file to import from.")
    imp.add_argument("--profile-dir", default=None)
    imp.add_argument("--dry-run", action="store_true", help="Parse and report only; write nothing.")
    imp.add_argument("--limit", type=int, default=None, help="Import at most N items (smoke).")

    rb = sub.add_parser(
        "rebuild",
        help="Rebuild derived memory (QA / notes / topics) from imported source, with budget + resume.",
    )
    rb.add_argument("--estimate", action="store_true", help="Estimate tokens and cost only.")
    rb.add_argument("--budget", type=float, default=None, help="Budget cap in CNY.")
    rb.add_argument("--batch-size", type=int, default=20)
    rb.add_argument("--no-resume", action="store_true")
    rb.add_argument("--profile-dir", default=None)

    # --- reliability layer (feature/reliability-recovery-v1) -----------------
    # Read-only subcommands. health / diagnose / repair all share the same
    # flags for window sizing, production opt-in, and path-debug. ``repair``
    # only runs as ``--dry-run``; ``--apply`` is provided so the help text
    # can be explicit, but the handler always rejects it.

    _reliability_common_args = (
        ("--json", "json", "store_true",
         "Emit the full HealthReport as deterministic JSON (sort_keys=True, "
         "ensure_ascii=False). Default emits a one-line-per-section summary."),
        ("--deep", "deep", "store_true",
         "Run the deep provider auth probes (bounded HTTP, 10 s timeout each)."),
        ("--allow-production-read", "allow_production_read", "store_true",
         "Authorize SELECTs against the production boundary (port 5433 or "
         "loopback/v3embeddings). Without this flag, storage / memory_write / "
         "derived checks report skip and never lower the verdict."),
        ("--profile-dir", "profile_dir", "store",
         "Explicit profile directory to resolve the v3-core base path. "
         "Defaults to v3core.config._resolve_data_dir() or "
         "~/.v3-core/profiles/default."),
        ("--window-hours", "window_hours", "store_int",
         "Width of the 'recent' window for current-incident classification "
         "(default 24)."),
        ("--debug-paths", "debug_paths", "store_true",
         "Include raw filesystem paths in path labels (off by default; "
         "default output is a kind/leaf/hash12 triple)."),
    )

    health_p = sub.add_parser(
        "health",
        help=(
            "Read-only Hippocampus health snapshot (DESIGN §4-§5). "
            "Returns exit 0 healthy, 1 degraded, 2 unhealthy or hard-failure. "
            "Production reads require --allow-production-read."
        ),
        description=(
            "Read-only Hippocampus health snapshot (DESIGN §4-§5). "
            "Returns exit 0 healthy, 1 degraded, 2 unhealthy or hard-failure. "
            "Production reads require --allow-production-read."
        ),
    )
    diagnose_p = sub.add_parser(
        "diagnose",
        help=(
            "Read-only Hippocampus health classification (DESIGN §8). "
            "Returns exit 0 when there are no active issues (info-only or "
            "empty), 1 when there is at least one severity>=warning issue, "
            "2 on hard failure. Production reads require "
            "--allow-production-read."
        ),
        description=(
            "Read-only Hippocampus health classification (DESIGN §8). "
            "Returns exit 0 when there are no active issues (info-only or "
            "empty), 1 when there is at least one severity>=warning issue, "
            "2 on hard failure. Production reads require "
            "--allow-production-read."
        ),
    )
    repair_p = sub.add_parser(
        "repair",
        help=(
            "Read-only dry-run repair plan (DESIGN §9). Returns exit 0 when "
            "there are no candidate actions, 1 when at least one action is "
            "planned, 2 on hard failure. --dry-run is the default; "
            "--apply is NOT IMPLEMENTED IN v1 -- always refuses. "
            "Production reads require --allow-production-read."
        ),
        description=(
            "Read-only dry-run repair plan (DESIGN §9). Returns exit 0 when "
            "there are no candidate actions, 1 when at least one action is "
            "planned, 2 on hard failure. --dry-run is the default; "
            "--apply is NOT IMPLEMENTED IN v1 -- always refuses. "
            "Production reads require --allow-production-read."
        ),
    )

    for flag, dest, action, help_text in _reliability_common_args:
        kwargs = {"dest": dest, "help": help_text}
        if action == "store_true":
            kwargs["action"] = "store_true"
            kwargs["default"] = False
        elif action == "store_int":
            kwargs["action"] = "store"
            kwargs["type"] = int
            kwargs["default"] = 24
        elif action == "store":
            kwargs["action"] = "store"
            kwargs["type"] = str
            kwargs["default"] = None
        else:  # pragma: no cover - defensive
            raise ValueError(f"unknown reliability arg action: {action!r}")
        health_p.add_argument(flag, **kwargs)
        diagnose_p.add_argument(flag, **kwargs)
        repair_p.add_argument(flag, **kwargs)

    # Repair-only knobs. ``--dry-run`` is the default; ``--apply`` is a
    # documented trapdoor that the handler refuses with exit 2 + the
    # REPAIR_APPLY_NOT_IMPLEMENTED error code.
    repair_p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Default. Emit a deterministic dry-run plan; never write.",
    )
    repair_p.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help=(
            "NOT IMPLEMENTED IN v1 -- always refuses. The handler exits 2 "
            "with REPAIR_APPLY_NOT_IMPLEMENTED_IN_V1 before any work. "
            "Kept on the parser so the help text can document the "
            "intentional absence."
        ),
    )

    doctor.add_argument(
        "--full",
        action="store_true",
        help="Run the 16 extended first-user checks (DB / auth / write / read / recall).",
    )
    doctor.add_argument(
        "--writes",
        action="store_true",
        help="With --full: allow the write probe (writes one row into the target DB).",
    )
    doctor.add_argument(
        "--runtime",
        action="store_true",
        help=(
            "Runtime integrity section: verify which v3core content the LIVE "
            "Hermes processes actually load, against an approved Release "
            "artifact (--wheel). Exit 0 healthy, 1 unverified/degraded, "
            "2 integrity violation (shadow/mismatch/editable)."
        ),
    )
    doctor.add_argument(
        "--wheel",
        default=None,
        help="Path to the approved release wheel (enables exact content comparison).",
    )
    doctor.add_argument(
        "--tag",
        default=None,
        help="Release tag label for --wheel (e.g. v0.2.1).",
    )
    doctor.add_argument(
        "--json",
        dest="runtime_json",
        action="store_true",
        help="With --runtime: emit the machine-readable JSON report instead of the human summary.",
    )
    doctor.add_argument(
        "--deep",
        dest="runtime_deep",
        action="store_true",
        help="With --runtime: fingerprint every package file instead of the critical set.",
    )

    return parser


def _resolve_profile_dir(explicit: str | None):
    """Resolve the profile directory the same way the engine does."""
    from pathlib import Path as _Path

    if explicit:
        return _Path(explicit).expanduser().resolve()
    try:
        from v3core import config as _cfg

        cfg = _cfg.resolve_config()
        return _Path(_cfg._resolve_data_dir(cfg))
    except Exception:
        return _Path.home() / ".v3-core" / "profiles" / "default"


def _install(args) -> int:
    try:
        from v3core import first_run
    except Exception as exc:  # pragma: no cover - defensive
        print(json.dumps({"command": "install", "status": "error",
                          "detail": f"first_run module unavailable: {exc}"}, ensure_ascii=False))
        return 2
    return first_run.run_install(
        preset=args.preset,
        pg_port=args.pg_port,
        profile_dir=args.profile_dir,
        hermes_home=args.hermes_home,
        embed_key=args.embed_key,
        llm_key=args.llm_key,
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
        skip_smoke=args.skip_smoke,
        plugin_wheel=args.plugin_wheel,
    )


def _import(args) -> int:
    try:
        from v3core import importers
    except Exception as exc:  # pragma: no cover - defensive
        print(json.dumps({"command": "import", "status": "error",
                          "detail": f"importers module unavailable: {exc}"}, ensure_ascii=False))
        return 2
    if args.source == "list":
        rows = [
            {"name": cls.name, "capability": cls.capability, "description": cls.description}
            for cls in importers.IMPORTERS.values()
        ]
        print(json.dumps({"command": "import", "status": "ok", "importers": rows}, ensure_ascii=False, indent=2))
        return 0
    if not args.root:
        print(json.dumps({"command": "import", "status": "error",
                          "detail": "--root is required for a real import (a directory or a file)."},
                         ensure_ascii=False))
        return 2
    from pathlib import Path as _Path

    profile_dir = _resolve_profile_dir(args.profile_dir)

    # Live imports need a real connection pool: the importer deliberately refuses
    # to write without one (no silent no-op). Construct it from the resolved
    # profile config, exactly as the engine does for its own writes.
    if not args.dry_run:
        try:
            import psycopg2

            from v3core.config import resolve_config
            from v3core.pg_pool import PgPool

            cfg = resolve_config()
            # V3Config exposes flat attributes (pg / embed / rerank / llm); the
            # nested `storage` shape kept silently evaluating to None.
            pg = getattr(cfg, "pg", None) or getattr(getattr(cfg, "storage", None), "pg", None)
            if pg is None:
                raise RuntimeError("no storage.pg block in the resolved profile config")
            password = (os.environ.get("V3CORE_PG_PASSWORD")
                        or os.environ.get("PGPASSWORD") or "")

            def _connect():
                return psycopg2.connect(
                    host=pg.host, port=int(pg.port), dbname=pg.database,
                    user=pg.user, password=password, connect_timeout=10,
                )

            importers.install_pool(PgPool(connect=_connect, max_connections=4))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({
                "command": "import", "status": "error",
                "detail": f"could not construct a database pool for the import: "
                          f"{type(exc).__name__}: {exc}. Check the profile config and "
                          f"V3CORE_PG_PASSWORD, or run with --dry-run.",
            }, ensure_ascii=False))
            return 1
    try:
        stats = importers.import_source(
            source=args.source,
            root=_Path(args.root).expanduser().resolve(),
            profile_dir=profile_dir,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    except NotImplementedError as exc:
        print(json.dumps({"command": "import", "status": "not_implemented", "detail": str(exc)},
                         ensure_ascii=False))
        return 3
    except Exception as exc:
        print(json.dumps({"command": "import", "status": "error", "detail": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"command": "import", "status": "ok", "dry_run": stats.dry_run, "stats": stats.as_dict()},
                     ensure_ascii=False, indent=2))
    return 0


def _rebuild(args) -> int:
    try:
        from v3core import rebuild
    except Exception as exc:  # pragma: no cover - defensive
        print(json.dumps({"command": "rebuild", "status": "error",
                          "detail": f"rebuild module unavailable: {exc}"}, ensure_ascii=False))
        return 2
    profile_dir = _resolve_profile_dir(args.profile_dir)
    try:
        if args.estimate:
            est = rebuild.estimate(profile_dir=profile_dir, batch_size=args.batch_size)
            print(json.dumps({"command": "rebuild", "mode": "estimate", **est}, ensure_ascii=False, indent=2))
            return 0
        # The runner takes an injectable llm_fn (one rolling note per batch).
        # Without wiring it, a real rebuild could only ever fail closed — the
        # same "CLI never connected the dependency" gap as the import pool.
        def _llm_fn(system: str, messages: list) -> str:
            from v3core.config import resolve_config
            from v3core.llm import LLMClient

            return LLMClient(resolve_config()).chat(system, messages)

        res = rebuild.run_rebuild(
            profile_dir=profile_dir,
            batch_size=args.batch_size,
            budget_yuan=args.budget,
            resume=not args.no_resume,
            llm_fn=_llm_fn,
        )
    except Exception as exc:
        print(json.dumps({"command": "rebuild", "status": "failed", "detail": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"command": "rebuild", "mode": "run", **res}, ensure_ascii=False, indent=2))
    # Exit codes carry the outcome's meaning, because stopping on the budget the
    # user asked for is a SUCCESS, not a failure: `rebuild --budget 5 && next`
    # must not be reported as a broken run. The JSON `status` still distinguishes
    # them for anything that needs the detail.
    #   0   completed | budget_stopped   (intended outcomes)
    #   1   failed
    #   130 interrupted                 (conventional Ctrl-C)
    status = str(res.get("status") or "")
    if status in ("completed", "budget_stopped"):
        return 0
    if status == "interrupted":
        return 130
    return 1


def _doctor_full(args) -> int:
    try:
        from v3core import doctor_full
    except Exception as exc:  # pragma: no cover - defensive
        print(json.dumps({"command": "doctor", "status": "error",
                          "detail": f"doctor_full module unavailable: {exc}"}, ensure_ascii=False))
        return 2
    profile_dir = _resolve_profile_dir(None)
    result = doctor_full.run_full_checks(
        profile_dir=profile_dir,
        dsn=args.dsn,
        allow_write=getattr(args, "writes", False),
    )
    summary = result.get("summary", {})
    payload = {"command": "doctor", "full": True, "profile_dir": str(profile_dir), **result}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("fail", 0) == 0 else 1


def _doctor_runtime(args) -> int:
    """Runtime integrity: which v3core content do the LIVE processes load?

    Read-only: no environment writes, no process restarts, no DB access.
    Exit codes: 0 healthy, 1 unverified/degraded, 2 integrity violation
    (shadow / mismatch / editable-active).
    """
    try:
        from v3core.runtime_integrity import approved_from_wheel, build_report
    except Exception as exc:  # pragma: no cover - defensive
        print(json.dumps({"command": "doctor", "runtime": True, "status": "error",
                          "detail": f"runtime_integrity module unavailable: {exc}"},
                         ensure_ascii=False))
        return 2
    approved = None
    wheel = getattr(args, "wheel", None)
    if wheel:
        try:
            approved = approved_from_wheel(wheel, tag=getattr(args, "tag", None))
        except Exception as exc:
            print(json.dumps({"command": "doctor", "runtime": True, "status": "error",
                              "detail": f"approved wheel unreadable: {exc}"},
                             ensure_ascii=False))
            return 2
    import os as _os
    from pathlib import Path as _Path
    hermes_home = _os.environ.get("HERMES_HOME")
    if not hermes_home:
        hh = _Path.home() / "AppData" / "Local" / "hermes"
        hermes_home = str(hh) if hh.is_dir() else None
    scope = "full" if getattr(args, "runtime_deep", False) else "critical"
    report = build_report(hermes_home=hermes_home, approved=approved, scope=scope)
    payload = {"command": "doctor", "runtime": True, **report.to_dict()}
    if getattr(args, "runtime_json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    else:
        _render_runtime_human(payload)
    if report.severity == "error":
        return 2
    if report.severity == "warn":
        return 1
    return 0


def _render_runtime_human(payload: dict) -> None:
    """Compact human summary for ``doctor --runtime``."""
    out: list[str] = ["Hermes runtime integrity", "=" * 26]
    host = payload.get("host") or {}
    out.append(f"host: platform={host.get('platform')} python={host.get('python')}")
    appr = payload.get("approved")
    if appr and appr.get("wheel_filename"):
        sha = (appr.get("wheel_sha256") or "")[:12]
        out.append(f"approved: {appr.get('tag') or '?'} {appr['wheel_filename']} (sha256 {sha}...)")
        out.append(f"approved fingerprint: {str(appr.get('content_fingerprint'))[:16]}...")
    else:
        out.append("approved: not supplied (content comparison disabled; pass --wheel)")
    out.append("")
    out.append("live processes:")
    for pr in payload.get("live_processes") or []:
        proc = pr.get("process") or {}
        res = pr.get("resolution") or {}
        fp = str(res.get("fingerprint") or "")[:12]
        out.append(
            f"  {proc.get('role', '?'):8s} PID {proc.get('pid'):<7} -> {pr.get('verdict')}  fp={fp}"
        )
    active = [c for c in (payload.get("copies") or [])
              if c.get("package") == "v3core" and c.get("state") == "active"]
    if active:
        out.append("")
        out.append(f"loaded path: {active[0].get('package_root')}")
    others = [c for c in (payload.get("copies") or [])
              if c.get("package") == "v3core" and c.get("state") != "active"]
    if others:
        out.append("")
        out.append("other copies:")
        for c in others:
            out.append(f"  {c.get('package_root')}  [{c.get('state')} - {c.get('install_type')}]")
    if payload.get("degraded"):
        out.append("")
        out.append("degraded: " + "; ".join(payload["degraded"]))
    out.append("")
    out.append(f"runtime integrity: {payload.get('verdict')} ({payload.get('severity')})")
    out.append(f"summary: {payload.get('summary')}")
    print("\n".join(out))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        if getattr(args, "runtime", False):
            return _doctor_runtime(args)
        if getattr(args, "full", False):
            return _doctor_full(args)
        return _doctor(args)
    if args.command == "bootstrap":
        return _bootstrap(args)
    if args.command == "upgrade":
        return _upgrade(args)
    if args.command == "install":
        return _install(args)
    if args.command == "import":
        return _import(args)
    if args.command == "rebuild":
        return _rebuild(args)
    if args.command == "health":
        try:
            from v3core.reliability.cli import handle_health
        except Exception as exc:  # pragma: no cover - defensive
            print(json.dumps({"command": "health", "status": "error",
                              "detail": f"reliability module unavailable: {exc}"},
                             ensure_ascii=False))
            return 2
        return handle_health(args)
    if args.command == "diagnose":
        try:
            from v3core.reliability.cli import handle_diagnose
        except Exception as exc:  # pragma: no cover - defensive
            print(json.dumps({"command": "diagnose", "status": "error",
                              "detail": f"reliability module unavailable: {exc}"},
                             ensure_ascii=False))
            return 2
        return handle_diagnose(args)
    if args.command == "repair":
        try:
            from v3core.reliability.cli import handle_repair
        except Exception as exc:  # pragma: no cover - defensive
            print(json.dumps({"command": "repair", "status": "error",
                              "detail": f"reliability module unavailable: {exc}"},
                             ensure_ascii=False))
            return 2
        return handle_repair(args)
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
