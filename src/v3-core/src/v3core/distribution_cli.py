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
# `alpha_bootstrap.sql` to indicate where the packaged
# `explicit_memories.sql` body must be spliced in. The marker must be a
# standalone SQL comment line.
_ALPHA_INCLUDE_MARKER = re.compile(
    r"^--\s*>>>?\s*ALPHA_BOOTSTRAP_INCLUDE:[^\n]*<<<\s*$",
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
    """Replace the canonical alpha include marker with the packaged
    ``explicit_memories.sql`` body.

    The marker line is exactly::

        -- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/explicit_memories.sql <<<

    The combined result is what we hand to ``psycopg2.cursor.execute``,
    so the SQL never depends on a repo-relative path being present at
    run time.
    """
    explicit = _package_sql("explicit_memories.sql")
    expanded, n = _ALPHA_INCLUDE_MARKER.subn(lambda _m: explicit, sql_text)
    if n == 0:
        # No marker found — return the original text. This is the
        # contract: alpha_bootstrap.sql contains the marker; older
        # revisions may not. The doctor check below reports whether
        # the marker was present.
        return sql_text
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
    sql_check: dict[str, Any] = {}
    for name in ("alpha_bootstrap.sql", "explicit_memories.sql"):
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
        sql_check["alpha_bootstrap.sql"]["include_marker_found"] = bool(
            _ALPHA_INCLUDE_MARKER.search(alpha_text)
        )
        if not sql_check["alpha_bootstrap.sql"]["include_marker_found"]:
            report["warnings"].append(
                "alpha_bootstrap.sql is missing the ALPHA_BOOTSTRAP_INCLUDE "
                "marker; bootstrap will not splice explicit_memories.sql"
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        if getattr(args, "full", False):
            return _doctor_full(args)
        return _doctor(args)
    if args.command == "bootstrap":
        return _bootstrap(args)
    if args.command == "install":
        return _install(args)
    if args.command == "import":
        return _import(args)
    if args.command == "rebuild":
        return _rebuild(args)
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
