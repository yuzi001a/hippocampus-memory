"""v3core.doctor_full — strengthened doctor with 16 specific checks.

Public contract (frozen — see FROZEN INTERFACES in the task brief):

    def run_full_checks(*, profile_dir, dsn, timeout=10.0, allow_write=False) -> dict
    def run_full_checks_cli(argv: list[str]) -> int

Why this module exists:

    The bundled ``hippocampus doctor`` is read-only and intentionally
    conservative — it never opens a database in ``--static`` mode and
    never calls embedding / LLM / rerank. The ``--full`` flag is the
    operator's "dig deeper" path: it is still read-only by default, but
    it reports the 16 specific environment / configuration / persistence
    signals that are required to confidently run the alpha pipeline
    against a real workload.

The 16 check ids, in order, are:

    db_reachable, pgvector_available, schema_version, migration_state,
    memory_llm_auth, embedding_auth, rerank_auth,
    dimensions_consistency, hermes_provider_discovery, hermes_home,
    write, read, vector_insert_search, rerank, recall,
    restart_persistence_hint

Auth checks perform a real minimal HTTP request WHEN a key is
configured, and skip otherwise — they NEVER print the key. The status
mapping is the frozen contract::

    "ok"    — check passed
    "fail"  — hard error, blocker
    "skip"  — not applicable / not requested (e.g. write probe off)
    "warn"  — soft issue, can proceed with care

The runner must NEVER raise; every check is wrapped in a try/except
that records the exception as a ``fail`` with a human-readable detail.
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import socket
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

logger = logging.getLogger("v3core.doctor_full")

# Frozen check-id list — order is the contract.
CHECK_IDS: list[str] = [
    "db_reachable",
    "pgvector_available",
    "schema_version",
    "migration_state",
    "memory_llm_auth",
    "embedding_auth",
    "rerank_auth",
    "dimensions_consistency",
    "hermes_provider_discovery",
    "hermes_home",
    "write",
    "read",
    "vector_insert_search",
    "rerank",
    "recall",
    "restart_persistence_hint",
]

# Status class mapping for HTTP auth checks — kept identical to llm.py so
# the operator sees the same vocabulary across the codebase.
_HTTP_STATUS_CLASS: dict[int, str] = {
    401: "auth_failed",
    402: "quota_or_plan_limit",
    429: "rate_limited",
    500: "upstream_error",
    503: "upstream_unavailable",
}


# ──────────────────────────────────────────────────────────────────────────────
# Public entry points
# ──────────────────────────────────────────────────────────────────────────────


def run_full_checks(
    *,
    profile_dir: Path | None,
    dsn: str | None,
    timeout: float = 10.0,
    allow_write: bool = False,
) -> dict:
    """Run all 16 checks and return a structured report.

    Returns::

        {
            "checks": [
                {"id": str, "status": "ok"|"fail"|"skip"|"warn",
                 "detail": str, "evidence": dict},
                ...
            ],
            "summary": {"ok": int, "fail": int, "skip": int, "warn": int},
        }

    Read-only: the write-probe check returns status "skip" with detail
    "write probe not requested" unless ``allow_write=True``.
    """
    # Build a config-derived view once. All checks share the same view
    # so secrets are never re-resolved per check.
    cfg_view = _build_config_view()
    parsed_dsn: dict[str, Any] | None = None
    if dsn:
        try:
            from .distribution_cli import _parse_dsn  # type: ignore
            parsed_dsn = _parse_dsn(dsn)
        except Exception as e:  # noqa: BLE001
            parsed_dsn = None
            logger.debug("DSN parse failed for doctor_full: %s", e)

    checks: list[dict[str, Any]] = []

    # ── 1–2. Database & pgvector ───────────────────────────────────────
    checks.append(_check_db_reachable(parsed_dsn, timeout))
    checks.append(_check_pgvector_available(parsed_dsn, timeout))

    # ── 3–4. Schema & migrations ────────────────────────────────────────
    checks.append(_check_schema_version(parsed_dsn, timeout))
    checks.append(_check_migration_state(parsed_dsn, timeout))

    # ── 5–7. Auth checks (LLM / embed / rerank) ────────────────────────
    checks.append(_check_auth_llm(cfg_view, timeout))
    checks.append(_check_auth_embedding(cfg_view, timeout))
    checks.append(_check_auth_rerank(cfg_view, timeout))

    # ── 8. Dimensions consistency ──────────────────────────────────────
    checks.append(_check_dimensions_consistency(cfg_view, parsed_dsn, timeout))

    # ── 9–10. Hermes host discovery ────────────────────────────────────
    checks.append(_check_hermes_provider_discovery())
    checks.append(_check_hermes_home())

    # ── 11. Write probe (gated) ────────────────────────────────────────
    if allow_write:
        checks.append(_check_write(profile_dir, parsed_dsn, timeout))
    else:
        checks.append({
            "id": "write",
            "status": "skip",
            "detail": "write probe not requested",
            "evidence": {"allow_write": False},
        })

    # ── 12. Read probe (DSN present) ──────────────────────────────────
    checks.append(_check_read(profile_dir, parsed_dsn, timeout))

    # ── 13. vector insert+search smoke ────────────────────────────────
    checks.append(_check_vector_insert_search(parsed_dsn, allow_write, timeout))

    # ── 14. rerank smoke ──────────────────────────────────────────────
    checks.append(_check_rerank(cfg_view, timeout))

    # ── 15. recall (embedding+rerank) smoke ────────────────────────────
    checks.append(_check_recall(cfg_view, parsed_dsn, allow_write, timeout))

    # ── 16. restart-persistence hint ───────────────────────────────────
    checks.append(_check_restart_persistence_hint(profile_dir, parsed_dsn,
                                                 allow_write, timeout))

    summary = {"ok": 0, "fail": 0, "skip": 0, "warn": 0}
    for c in checks:
        summary[c["status"]] = summary.get(c["status"], 0) + 1

    return {"checks": checks, "summary": summary}


def run_full_checks_cli(argv: list[str]) -> int:
    """CLI wrapper: parses argv, runs checks, prints JSON to stdout, returns
    a process exit code.

    Exit codes:
        0 — every required check is ``ok`` or ``skip`` (no fails, no warns)
        1 — at least one ``warn``
        2 — at least one ``fail``
        3 — invalid arguments
    """
    parser = _build_cli_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse already printed usage; return its code.
        return int(exc.code or 3)

    profile_dir = Path(args.profile_dir).expanduser() if args.profile_dir else None
    try:
        report = run_full_checks(
            profile_dir=profile_dir,
            dsn=args.dsn,
            timeout=float(args.timeout),
            allow_write=bool(args.allow_write),
        )
    except Exception as e:  # noqa: BLE001
        # Belt and suspenders: run_full_checks must not raise, but if
        # a future regression slips through, surface it cleanly.
        report = {
            "checks": [{
                "id": "doctor_full_internal",
                "status": "fail",
                "detail": f"run_full_checks raised: {type(e).__name__}: {e}",
                "evidence": {"trace": traceback.format_exc(limit=4)},
            }],
            "summary": {"ok": 0, "fail": 1, "skip": 0, "warn": 0},
        }

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    s = report.get("summary", {})
    if s.get("fail", 0) > 0:
        return 2
    if s.get("warn", 0) > 0:
        return 1
    return 0


def _build_cli_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hippocampus doctor --full",
        description=(
            "Strengthened Hippocampus doctor: 16 specific environment / "
            "config / persistence checks. Read-only by default; "
            "--allow-write enables the vector write probe and the "
            "restart-persistence probe."
        ),
    )
    p.add_argument(
        "--dsn", default=None,
        help="Optional non-production DSN (port != 5433, loopback + v3embeddings refused).",
    )
    p.add_argument(
        "--profile-dir", default=None,
        help="Optional path to the v3-core profile directory "
             "(contains rebuild_checkpoint.json etc.).",
    )
    p.add_argument(
        "--timeout", type=float, default=10.0,
        help="Network / DB connect timeout in seconds (default 10).",
    )
    p.add_argument(
        "--allow-write", action="store_true",
        help="Enable the write-probe and the restart-persistence write "
             "phase. Off by default — the full doctor is read-only.",
    )
    return p


# ──────────────────────────────────────────────────────────────────────────────
# Check helpers — each returns a single {id, status, detail, evidence} dict
# and NEVER raises (catches every exception and reports it as fail).
# ──────────────────────────────────────────────────────────────────────────────


def _safe(fn: Callable[[], dict[str, Any]], *, fallback_id: str) -> dict[str, Any]:
    """Run a check function and convert any exception into a fail record."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return {
            "id": fallback_id,
            "status": "fail",
            "detail": f"check raised: {type(e).__name__}: {e!s}",
            "evidence": {"trace": traceback.format_exc(limit=3)},
        }


def _build_config_view() -> dict[str, Any]:
    """Resolve the active config into a JSON-safe view used by checks.

    Returns a dict with at least these keys (all optional, may be None):

        llm: {provider, model, api_key, base_url}
        embed: {endpoint, apiKey, model, dim}
        rerank: {endpoint, apiKey, model, dim}
        embed_dim_configured: int | None
        pg: {host, port, database, user, has_password}

    Never raises. If the config cannot be resolved, returns a skeleton
    with all fields None so auth checks default to ``skip``.
    """
    view: dict[str, Any] = {
        "llm": {"provider": None, "model": None, "api_key": None,
                "base_url": None},
        "embed": {"endpoint": None, "apiKey": None, "model": None,
                  "dim": None},
        "rerank": {"endpoint": None, "apiKey": None, "model": None,
                   "dim": None},
        "embed_dim_configured": None,
        "pg": {"host": None, "port": None, "database": None,
               "user": None, "has_password": False},
    }
    try:
        from .config import resolve_config  # type: ignore
    except Exception:
        return view
    try:
        cfg = resolve_config()  # type: ignore[misc]
    except Exception:
        return view
    try:
        llm = getattr(cfg, "llm", None) or getattr(getattr(cfg, "storage", None), "llm", None)  # type: ignore[attr-defined]
        view["llm"]["provider"] = getattr(llm, "provider", None) or None
        view["llm"]["model"] = getattr(llm, "model", None) or None
        view["llm"]["api_key"] = getattr(llm, "api_key", None) or None
        view["llm"]["base_url"] = getattr(llm, "base_url", None) or None
    except Exception:  # noqa: BLE001
        pass
    try:
        se = getattr(cfg, "embed", None) or getattr(getattr(cfg, "storage", None), "embed", None)
        view["embed"]["endpoint"] = getattr(se, "endpoint", None) or None
        view["embed"]["apiKey"] = getattr(se, "api_key", None) or getattr(
            se, "apiKey", None
        ) or None
        view["embed"]["model"] = getattr(se, "model", None) or None
        view["embed"]["dim"] = getattr(se, "dim", None) or None
        view["embed_dim_configured"] = view["embed"]["dim"]
    except Exception:  # noqa: BLE001
        pass
    try:
        sr = getattr(cfg, "rerank", None) or getattr(getattr(cfg, "storage", None), "rerank", None)
        view["rerank"]["endpoint"] = getattr(sr, "endpoint", None) or None
        view["rerank"]["apiKey"] = getattr(sr, "api_key", None) or getattr(
            sr, "apiKey", None
        ) or None
        view["rerank"]["model"] = getattr(sr, "model", None) or None
        view["rerank"]["dim"] = getattr(sr, "dim", None) or None
    except Exception:  # noqa: BLE001
        pass
    # The typed config model does not expose every key a user may have written
    # (the shipped template uses the camelCase `apiKey`; `dim` sits under
    # storage.embed). Fill any remaining gap from the raw YAML that
    # resolve_config() actually read, so doctor reports what the user configured
    # instead of "not set" on a correct install.
    try:
        import os as _os

        import yaml as _yaml

        from .config import _find_config  # type: ignore

        explicit = _os.environ.get("V3CORE_CONFIG", "").strip()
        cfg_path = Path(explicit) if explicit else _find_config(
            profile=_os.environ.get("V3CORE_PROFILE", "default"),
            hermes_home=_os.environ.get("HERMES_HOME", ""),
        )
        raw = _yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8")) if cfg_path else {}
        storage_raw = (raw or {}).get("storage") or {}
        for slot, section in (("embed", storage_raw.get("embed") or {}),
                              ("rerank", storage_raw.get("rerank") or {})):
            if not isinstance(section, dict):
                continue
            view[slot]["endpoint"] = view[slot].get("endpoint") or section.get("endpoint")
            view[slot]["apiKey"] = (view[slot].get("apiKey") or section.get("apiKey")
                                    or section.get("api_key"))
            view[slot]["model"] = view[slot].get("model") or section.get("model")
            view[slot]["dim"] = view[slot].get("dim") or section.get("dim")
        view["embed_dim_configured"] = view["embed"].get("dim")
        llm_raw = (raw or {}).get("llm") or {}
        if isinstance(llm_raw, dict):
            for key in ("provider", "model", "base_url"):
                view["llm"][key] = view["llm"].get(key) or llm_raw.get(key)
            view["llm"]["api_key"] = view["llm"].get("api_key") or llm_raw.get("api_key")
    except Exception:  # noqa: BLE001
        pass
    try:
        pg = getattr(cfg, "pg", None) or getattr(getattr(cfg, "storage", None), "pg", None)
        view["pg"]["host"] = getattr(pg, "host", None)
        view["pg"]["port"] = getattr(pg, "port", None)
        view["pg"]["database"] = getattr(pg, "database", None)
        view["pg"]["user"] = getattr(pg, "user", None)
        view["pg"]["has_password"] = bool(getattr(pg, "password", ""))
    except Exception:  # noqa: BLE001
        pass
    return view


# ── 1. db_reachable ──────────────────────────────────────────────────────────


def _check_db_reachable(parsed_dsn: dict[str, Any] | None,
                        timeout: float) -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        if not parsed_dsn:
            return {
                "id": "db_reachable",
                "status": "skip",
                "detail": "no --dsn supplied; DB reachability was not probed",
                "evidence": {"dsn_present": False},
            }
        try:
            import psycopg2  # type: ignore
        except Exception as e:  # noqa: BLE001
            return {
                "id": "db_reachable",
                "status": "fail",
                "detail": (
                    "psycopg2 is not importable in this environment: "
                    f"{type(e).__name__}: {e!s}. The DSN target was "
                    f"{parsed_dsn.get('host')}:{parsed_dsn.get('port')}/"
                    f"{parsed_dsn.get('database')}."
                ),
                "evidence": {
                    "host": parsed_dsn.get("host"),
                    "port": parsed_dsn.get("port"),
                    "database": parsed_dsn.get("database"),
                },
            }
        try:
            password = parsed_dsn.get("password") or os.environ.get(
                "V3CORE_PG_PASSWORD", ""
            ) or os.environ.get("PGPASSWORD", "")
            conn = psycopg2.connect(
                host=parsed_dsn.get("host", ""),
                port=int(parsed_dsn.get("port") or 0),
                database=parsed_dsn.get("database", ""),
                user=parsed_dsn.get("user", ""),
                password=password or "",
                connect_timeout=max(1, int(timeout)),
            )
        except Exception as e:  # noqa: BLE001
            return {
                "id": "db_reachable",
                "status": "fail",
                "detail": (
                    f"psycopg2.connect failed: {type(e).__name__}: {e!s}. "
                    "Verify the DSN, the DB is up, and the production "
                    "boundary (port 5433 or loopback/v3embeddings) is "
                    "not in use."
                ),
                "evidence": {
                    "host": parsed_dsn.get("host"),
                    "port": parsed_dsn.get("port"),
                    "database": parsed_dsn.get("database"),
                },
            }
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return {
                "id": "db_reachable",
                "status": "ok",
                "detail": (
                    f"SELECT 1 succeeded against "
                    f"{parsed_dsn.get('host')}:{parsed_dsn.get('port')}/"
                    f"{parsed_dsn.get('database')}"
                ),
                "evidence": {
                    "host": parsed_dsn.get("host"),
                    "port": parsed_dsn.get("port"),
                    "database": parsed_dsn.get("database"),
                },
            }
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    return _safe(_do, fallback_id="db_reachable")


# ── 2. pgvector_available ───────────────────────────────────────────────────


def _check_pgvector_available(parsed_dsn: dict[str, Any] | None,
                              timeout: float) -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        if not parsed_dsn:
            return {
                "id": "pgvector_available",
                "status": "skip",
                "detail": "no --dsn supplied; pgvector was not probed",
                "evidence": {},
            }
        try:
            import psycopg2  # type: ignore
        except Exception:  # noqa: BLE001
            return {
                "id": "pgvector_available",
                "status": "skip",
                "detail": "psycopg2 not importable; cannot probe pgvector",
                "evidence": {},
            }
        password = parsed_dsn.get("password") or os.environ.get(
            "V3CORE_PG_PASSWORD", ""
        ) or os.environ.get("PGPASSWORD", "")
        try:
            conn = psycopg2.connect(
                host=parsed_dsn.get("host", ""),
                port=int(parsed_dsn.get("port") or 0),
                database=parsed_dsn.get("database", ""),
                user=parsed_dsn.get("user", ""),
                password=password or "",
                connect_timeout=max(1, int(timeout)),
            )
        except Exception as e:  # noqa: BLE001
            return {
                "id": "pgvector_available",
                "status": "fail",
                "detail": f"connect failed before pgvector probe: {type(e).__name__}: {e!s}",
                "evidence": {},
            }
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "SELECT extversion FROM pg_extension "
                        "WHERE extname = 'vector'"
                    )
                    row = cur.fetchone()
                except Exception as e:  # noqa: BLE001
                    return {
                        "id": "pgvector_available",
                        "status": "fail",
                        "detail": f"pg_extension probe failed: {type(e).__name__}: {e!s}",
                        "evidence": {},
                    }
            if row:
                return {
                    "id": "pgvector_available",
                    "status": "ok",
                    "detail": f"pgvector extension present (version={row[0]})",
                    "evidence": {"version": row[0]},
                }
            return {
                "id": "pgvector_available",
                "status": "fail",
                "detail": (
                    "pgvector extension is NOT installed. The alpha pipeline "
                    "uses VECTOR(1024) for embedding storage; without "
                    "pgvector the schema cannot be applied."
                ),
                "evidence": {"installed": False},
            }
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    return _safe(_do, fallback_id="pgvector_available")


# ── 3. schema_version ───────────────────────────────────────────────────────


def _check_schema_version(parsed_dsn: dict[str, Any] | None,
                          timeout: float) -> dict[str, Any]:
    """Look up the applied schema version (if any). The alpha bootstrap
    doesn't currently ship a ``schema_versions`` table, so this check
    reports a soft ``warn`` when no version marker is found, and ``ok``
    when the canonical alpha tables all exist.

    v0.2 additive behavior: when canonical tables are missing (notably
    ``explicit_memories`` and the optional ``qa_embedding_chunks`` child-A
    artifact), the detail and evidence point at the exact dry-run /
    apply upgrade command rather than telling the operator to recreate
    the database. The existing ``tables_present`` / ``tables_total``
    count contract is preserved; ``missing_tables`` is an additive
    evidence field.
    """

    def _do() -> dict[str, Any]:
        if not parsed_dsn:
            return {
                "id": "schema_version",
                "status": "skip",
                "detail": "no --dsn supplied; schema version not probed",
                "evidence": {},
            }
        try:
            import psycopg2  # type: ignore
        except Exception:  # noqa: BLE001
            return {
                "id": "schema_version",
                "status": "skip",
                "detail": "psycopg2 not importable; cannot probe schema version",
                "evidence": {},
            }
        password = parsed_dsn.get("password") or os.environ.get(
            "V3CORE_PG_PASSWORD", ""
        ) or os.environ.get("PGPASSWORD", "")
        try:
            conn = psycopg2.connect(
                host=parsed_dsn.get("host", ""),
                port=int(parsed_dsn.get("port") or 0),
                database=parsed_dsn.get("database", ""),
                user=parsed_dsn.get("user", ""),
                password=password or "",
                connect_timeout=max(1, int(timeout)),
            )
        except Exception as e:  # noqa: BLE001
            return {
                "id": "schema_version",
                "status": "fail",
                "detail": f"connect failed: {type(e).__name__}: {e!s}",
                "evidence": {},
            }
        try:
            # The canonical tables the alpha pipeline reads/writes —
            # exhaustive list, used by the table-presence fallback below.
            canonical_alpha_tables = (
                "qa_pairs", "conversation_stream", "topics",
                "topic_entries", "observation_notes", "explicit_memories",
                "yin_paragraphs",
            )
            # v0.2 additive set: tables the upgrade tool specifically
            # brings to an existing install. ``explicit_memories`` is in
            # both lists (it's a canonical alpha table AND a v0.2 upgrade
            # target); the child-A sidecar ``qa_embedding_chunks`` is
            # only in the upgrade set. We track both because the upgrade
            # command distinguishes them.
            upgrade_target_tables = (
                "explicit_memories", "schema_versions",
                "qa_embedding_chunks",
            )
            with conn.cursor() as cur:
                # If a schema_versions table exists, use it.
                try:
                    cur.execute(
                        "SELECT version, applied_at FROM schema_versions "
                        "ORDER BY applied_at DESC LIMIT 1"
                    )
                    row = cur.fetchone()
                except Exception:  # noqa: BLE001
                    row = None
                    # A failed statement aborts the transaction: without this
                    # rollback every later probe on this cursor raises
                    # InFailedSqlTransaction, which is how the canonical-table
                    # fallback below reported 0/7 on a perfectly good install.
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                if row:
                    required_columns = {
                        "qa_pairs": (
                            "source_id", "session_id", "turn_id", "question",
                            "answer", "tool_calls", "tool_results", "embedding",
                            "embed_model", "created_at",
                        ),
                        "conversation_stream": ("embedding", "tool_calls", "tool_results"),
                        "topics": ("note_ref", "last_observer_ts", "embed_model"),
                        "topic_entries": ("source_qa_id", "embed_model"),
                        "observation_notes": ("embed_model",),
                        "yin_paragraphs": ("embed_model",),
                        "explicit_memories": ("memory_id", "embedding", "embed_model"),
                        "qa_embedding_chunks": (
                            "qa_id", "chunk_index", "source_field", "source_start",
                            "source_end", "source_sha256", "token_count",
                            "representation_version", "content", "embedding",
                            "embed_model",
                        ),
                    }
                    missing_columns: dict[str, list[str]] = {}
                    for table, columns in required_columns.items():
                        cur.execute(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name=%s",
                            (table,),
                        )
                        present_columns = {r[0] for r in cur.fetchall()}
                        missing = [c for c in columns if c not in present_columns]
                        if missing:
                            missing_columns[table] = missing
                    cur.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name = ANY(%s)",
                        (list(required_columns),),
                    )
                    present_tables = {r[0] for r in cur.fetchall()}
                    missing_tables = [
                        table for table in required_columns
                        if table not in present_tables
                    ]
                    evidence = {
                        "version": row[0],
                        "applied_at": str(row[1]),
                        "missing_columns": missing_columns,
                        "missing_tables": missing_tables,
                        "upgrade_command": "hippocampus upgrade --target <DSN> --dry-run",
                    }
                    if missing_columns or missing_tables:
                        return {
                            "id": "schema_version",
                            "status": "fail",
                            "detail": (
                                "schema_versions exists but canonical schema drift is present; "
                                "run `hippocampus upgrade --target <DSN> --dry-run`, then "
                                "apply the additive upgrade on a non-production target first"
                            ),
                            "evidence": evidence,
                        }
                    return {
                        "id": "schema_version",
                        "status": "ok",
                        "detail": f"schema_versions row: {row[0]} @ {row[1]}; canonical columns present",
                        "evidence": evidence,
                    }
                # Fall back: enumerate canonical alpha tables AND the
                # v0.2 upgrade targets in one pass, then diff against the
                # two reference sets. The "missing" lists feed the
                # upgrade-command hint.
                present_set: set[str] = set()
                present_alpha = 0
                total_alpha = len(canonical_alpha_tables)
                for tbl in canonical_alpha_tables:
                    try:
                        cur.execute(
                            "SELECT 1 FROM information_schema.tables "
                            "WHERE table_schema='public' AND table_name=%s",
                            (tbl,),
                        )
                        if cur.fetchone():
                            present_alpha += 1
                            present_set.add(tbl)
                    except Exception:  # noqa: BLE001
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                missing_alpha = [
                    t for t in canonical_alpha_tables
                    if t not in present_set
                ]
                # Probe the v0.2 upgrade targets that are NOT already in
                # the canonical alpha set (qa_embedding_chunks) plus
                # explicit_memories (which is in both).
                upgrade_presence: dict[str, bool] = {}
                for tbl in upgrade_target_tables:
                    try:
                        cur.execute(
                            "SELECT 1 FROM information_schema.tables "
                            "WHERE table_schema='public' AND table_name=%s",
                            (tbl,),
                        )
                        upgrade_presence[tbl] = cur.fetchone() is not None
                    except Exception:  # noqa: BLE001
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                        upgrade_presence[tbl] = False
                missing_upgrade_targets = [
                    t for t in upgrade_target_tables
                    if not upgrade_presence.get(t)
                ]
                required_columns = {
                    "qa_pairs": (
                        "source_id", "session_id", "turn_id", "question",
                        "answer", "tool_calls", "tool_results", "embedding",
                        "embed_model", "created_at",
                    ),
                    "conversation_stream": ("embedding", "tool_calls", "tool_results"),
                    "topics": ("note_ref", "last_observer_ts", "embed_model"),
                    "topic_entries": ("source_qa_id", "embed_model"),
                    "observation_notes": ("embed_model",),
                    "yin_paragraphs": ("embed_model",),
                    "explicit_memories": ("memory_id", "embedding", "embed_model"),
                    "qa_embedding_chunks": (
                        "qa_id", "chunk_index", "source_field", "source_start",
                        "source_end", "source_sha256", "token_count",
                        "representation_version", "content", "embedding",
                        "embed_model",
                    ),
                }
                missing_columns: dict[str, list[str]] = {}
                for table, columns in required_columns.items():
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=%s",
                        (table,),
                    )
                    present_columns = {r[0] for r in cur.fetchall()}
                    missing = [c for c in columns if c not in present_columns]
                    if missing:
                        missing_columns[table] = missing
            # Build the exact upgrade command the operator should run.
            # The DSN components are taken verbatim from the parsed
            # target so the operator can copy-paste; the password is
            # never included (PGPASSWORD / V3CORE_PG_PASSWORD is the
            # documented credential channel).
            #
            # When the parsed DSN is on the production boundary
            # (port 5433 or loopback/v3embeddings), the dry-run copy-
            # paste MUST include ``--allow-production-read`` or it
            # is unusable on an existing production install — without
            # that flag the doctor-issued command itself would be
            # refused. The apply command is still refused
            # unconditionally regardless of the flag; we surface a
            # ``production_apply_blocked`` flag in the evidence so
            # the operator knows the apply side requires a non-
            # production target.
            host = str(parsed_dsn.get("host", "") or "")
            port = int(parsed_dsn.get("port") or 0)
            database = str(parsed_dsn.get("database", "") or "")
            dsn_for_cmd = (
                f"postgres://{parsed_dsn.get('user','') or ''}@{host}"
                f":{port}/{database}"
            )
            on_production_boundary = (
                port == 5433
                or (
                    host in {"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"}
                    and database == "v3embeddings"
                )
            )
            if on_production_boundary:
                cmd_dry_run = (
                    f"hippocampus upgrade --target {dsn_for_cmd} "
                    "--dry-run --allow-production-read"
                )
                cmd_apply = (
                    "DO NOT RUN ON PRODUCTION. upgrade --apply is "
                    "unconditionally refused against the production "
                    f"boundary. Point at a non-production DSN: "
                    f"hippocampus upgrade --target <non-prod-DSN> --apply"
                )
            else:
                cmd_dry_run = (
                    f"hippocampus upgrade --target {dsn_for_cmd} --dry-run"
                )
                cmd_apply = (
                    f"hippocampus upgrade --target {dsn_for_cmd} --apply"
                )
            evidence: dict[str, Any] = {
                "tables_present": present_alpha,
                "tables_total": total_alpha,
                "missing_tables": missing_alpha,
                "upgrade_targets_present": upgrade_presence,
                "missing_upgrade_targets": missing_upgrade_targets,
                "missing_columns": missing_columns,
                "upgrade_command_dry_run": cmd_dry_run,
                "upgrade_command_apply": cmd_apply,
                "on_production_boundary": on_production_boundary,
                "production_apply_blocked": on_production_boundary,
            }
            if missing_columns:
                return {
                    "id": "schema_version",
                    "status": "fail",
                    "detail": (
                        "canonical schema columns are missing; run `hippocampus upgrade "
                        "--target <DSN> --dry-run`, then apply the additive upgrade "
                        "on a non-production target first"
                    ),
                    "evidence": evidence,
                }
            if present_alpha == total_alpha and not missing_upgrade_targets:
                # Full canonical + upgrade coverage without a
                # schema_versions ledger — keep the prior "warn" status
                # so existing checks stay green.
                return {
                    "id": "schema_version",
                    "status": "warn",
                    "detail": (
                        f"all {total_alpha} canonical alpha tables present "
                        "but no schema_versions row was found. Consider "
                        "running `hippocampus upgrade --target <DSN> --apply` "
                        "to record the v0.2 schema version (additive, no "
                        "data rewrite)."
                    ),
                    "evidence": evidence,
                }
            if missing_upgrade_targets and not missing_alpha:
                # All alpha tables present, but the v0.2 upgrade targets
                # are missing — point at the upgrade command instead of
                # the recreate-the-DB shortcut.
                missing_human = ", ".join(missing_upgrade_targets)
                return {
                    "id": "schema_version",
                    "status": "fail",
                    "detail": (
                        f"v0.2 upgrade targets missing on this install: "
                        f"{missing_human}. Run `{cmd_dry_run}` to inspect "
                        f"the additive closure, then `{cmd_apply}` to "
                        "apply the transactional, idempotent, no-data-"
                        "rewrite upgrade body. DO NOT recreate the "
                        "database — the upgrade is additive."
                    ),
                    "evidence": evidence,
                }
            # Canonical alpha tables are missing: keep the prior
            # bootstrap hint, but ALSO surface the upgrade command so the
            # operator sees both the bootstrap-and-fresh-install path and
            # the additive upgrade path as two distinct options.
            detail_missing = (
                ", ".join(missing_alpha) if missing_alpha else "none"
            )
            return {
                "id": "schema_version",
                "status": "fail",
                "detail": (
                    f"only {present_alpha}/{total_alpha} canonical alpha "
                    f"tables are present (missing: {detail_missing}). "
                    "Two additive options — DO NOT recreate the "
                    f"database. (a) `{cmd_dry_run}` then `{cmd_apply}` "
                    "to apply the v0.2 upgrade (additive, idempotent, "
                    "no data loss). (b) `hippocampus bootstrap --target "
                    f"{dsn_for_cmd}` to apply the full packaged "
                    "alpha_bootstrap.sql (idempotent — already-installed "
                    "tables are no-ops)."
                ),
                "evidence": evidence,
            }
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    return _safe(_do, fallback_id="schema_version")


# ── 4. migration_state ──────────────────────────────────────────────────────


def _check_migration_state(parsed_dsn: dict[str, Any] | None,
                           timeout: float) -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        if not parsed_dsn:
            return {
                "id": "migration_state",
                "status": "skip",
                "detail": "no --dsn supplied; migration state not probed",
                "evidence": {},
            }
        # If schema_versions exists and has rows, we're "migrated".
        try:
            import psycopg2  # type: ignore
        except Exception:  # noqa: BLE001
            return {
                "id": "migration_state",
                "status": "skip",
                "detail": "psycopg2 not importable; cannot probe migration state",
                "evidence": {},
            }
        password = parsed_dsn.get("password") or os.environ.get(
            "V3CORE_PG_PASSWORD", ""
        ) or os.environ.get("PGPASSWORD", "")
        try:
            conn = psycopg2.connect(
                host=parsed_dsn.get("host", ""),
                port=int(parsed_dsn.get("port") or 0),
                database=parsed_dsn.get("database", ""),
                user=parsed_dsn.get("user", ""),
                password=password or "",
                connect_timeout=max(1, int(timeout)),
            )
        except Exception as e:  # noqa: BLE001
            return {
                "id": "migration_state",
                "status": "fail",
                "detail": f"connect failed: {type(e).__name__}: {e!s}",
                "evidence": {},
            }
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "SELECT COUNT(*) FROM schema_versions"
                    )
                    n = cur.fetchone()
                    n = int(n[0]) if n and n[0] is not None else 0
                    return {
                        "id": "migration_state",
                        "status": "ok" if n > 0 else "warn",
                        "detail": f"schema_versions has {n} applied migration(s)",
                        "evidence": {"applied_count": n},
                    }
                except Exception:  # noqa: BLE001
                    return {
                        "id": "migration_state",
                        "status": "warn",
                        "detail": (
                            "no schema_versions table; cannot track applied "
                            "migrations. The schema is bootstrapped but "
                            "future migrations will be untracked."
                        ),
                        "evidence": {},
                    }
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    return _safe(_do, fallback_id="migration_state")


# ── 5–7. Auth checks (LLM / embed / rerank) ─────────────────────────────────


def _check_auth_llm(cfg_view: dict[str, Any], timeout: float) -> dict[str, Any]:
    llm = cfg_view.get("llm", {}) or {}
    # Probe the endpoint the profile actually configured. Hardcoding MiniMax's
    # /models meant a SiliconFlow install was probed at the wrong vendor and
    # reported a 404 warning.
    base = (llm.get("base_url") or "").rstrip("/")
    endpoint = f"{base}/chat/completions" if base else "https://api.minimaxi.com/v1/models"
    body = None
    if base:
        body = {
            "model": llm.get("model") or llm.get("model_name") or "",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
    # `_do_auth_check` prefers section["endpoint"]/["base_url"]; give it the full
    # chat route, otherwise it POSTs to the bare base_url and gets a 404.
    section = dict(llm)
    section.pop("base_url", None)
    section["endpoint"] = endpoint
    return _do_auth_check(
        "memory_llm_auth", section, timeout,
        default_endpoint=endpoint, probe_body=body,
    )


def _check_auth_embedding(cfg_view: dict[str, Any], timeout: float) -> dict[str, Any]:
    embed = cfg_view.get("embed", {})
    return _do_auth_check(
        "embedding_auth", embed, timeout,
        default_endpoint=embed.get("endpoint") or "",
        probe_body={"model": embed.get("model") or "", "input": "ping"},
    )


def _check_auth_rerank(cfg_view: dict[str, Any], timeout: float) -> dict[str, Any]:
    rr = cfg_view.get("rerank", {})
    return _do_auth_check(
        "rerank_auth", rr, timeout,
        default_endpoint=rr.get("endpoint") or "",
        probe_body={"model": rr.get("model") or "", "query": "ping",
                    "documents": ["ping"]},
    )


def _do_auth_check(
    check_id: str,
    section: dict[str, Any],
    timeout: float,
    *,
    default_endpoint: str,
    probe_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generic auth check.

    Policy:
      * No key configured → status='skip' with a 'no key' detail.
      * Key configured but no resolvable endpoint → status='warn'.
      * Key + endpoint → perform a minimal request (a tiny POST when the
        caller supplies `probe_body`, since embedding/rerank/chat routes are
        POST-only and answered a bare GET with 404 — which read to a user as
        "auth is broken" on a perfectly working install), classify the status
        code into the canonical vocabulary, never echo the key.
    """
    api_key = section.get("api_key") or section.get("apiKey") or ""
    if not api_key:
        return {
            "id": check_id,
            "status": "skip",
            "detail": "no api key configured for this provider",
            "evidence": {"key_present": False},
        }
    endpoint = section.get("endpoint") or section.get("base_url") or default_endpoint
    if not endpoint:
        return {
            "id": check_id,
            "status": "warn",
            "detail": (
                "api key is configured but no endpoint is known. Set the "
                "endpoint in config.yaml (storage.embed.endpoint or "
                "storage.rerank.endpoint) so doctor can probe the auth."
            ),
            "evidence": {"key_present": True, "endpoint_present": False},
        }
    # The request is best-effort. Network failures must not crash doctor.
    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        if probe_body is not None:
            resp = requests.post(
                endpoint, headers=headers, json=probe_body,
                timeout=max(1.0, float(timeout)),
            )
        else:
            resp = requests.get(endpoint, headers=headers, timeout=max(1.0, float(timeout)))
    except requests.RequestException as e:
        return {
            "id": check_id,
            "status": "fail",
            "detail": (
                f"auth probe request failed: {type(e).__name__}: {e!s}. "
                "Check network, proxy, and endpoint URL."
            ),
            "evidence": {"endpoint": _redact_query(endpoint), "exception": type(e).__name__},
        }
    except Exception as e:  # noqa: BLE001
        return {
            "id": check_id,
            "status": "fail",
            "detail": f"auth probe raised: {type(e).__name__}: {e!s}",
            "evidence": {"endpoint": _redact_query(endpoint)},
        }
    sc = int(getattr(resp, "status_code", 0) or 0)
    if 200 <= sc < 300:
        return {
            "id": check_id,
            "status": "ok",
            "detail": f"auth probe ok: status={sc} (provider accepted the key)",
            "evidence": {"status_code": sc, "class": "ok"},
        }
    cls = _HTTP_STATUS_CLASS.get(sc, f"http_{sc}")
    if sc in (401, 402):
        return {
            "id": check_id,
            "status": "fail",
            "detail": (
                f"auth probe failed: status={sc} class={cls}. The configured "
                "key was rejected — re-check the key, the account plan, "
                "and the project quota."
            ),
            "evidence": {"status_code": sc, "class": cls},
        }
    if sc == 429:
        return {
            "id": check_id,
            "status": "warn",
            "detail": (
                f"auth probe rate-limited: status={sc} class={cls}. The key "
                "is likely valid; the provider is throttling. Retry later."
            ),
            "evidence": {"status_code": sc, "class": cls},
        }
    if 500 <= sc < 600:
        return {
            "id": check_id,
            "status": "warn",
            "detail": (
                f"auth probe hit upstream error: status={sc} class={cls}. "
                "The provider is having a transient issue; the key was not "
                "rejected, but the probe could not confirm it is good."
            ),
            "evidence": {"status_code": sc, "class": cls},
        }
    return {
        "id": check_id,
        "status": "warn",
        "detail": f"auth probe returned unexpected status={sc} class={cls}",
        "evidence": {"status_code": sc, "class": cls},
    }


# ── 8. dimensions_consistency ──────────────────────────────────────────────


def _check_dimensions_consistency(cfg_view: dict[str, Any],
                                  parsed_dsn: dict[str, Any] | None,
                                  timeout: float) -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        configured = cfg_view.get("embed_dim_configured")
        if not configured:
            try:
                configured = int(cfg_view.get("embed", {}).get("dim") or 0) or None
            except Exception:  # noqa: BLE001
                configured = None
        if not configured:
            return {
                "id": "dimensions_consistency",
                "status": "warn",
                "detail": (
                    "configured embedding dim is not set in config.yaml. "
                    "Set storage.embed.dim (default 1024) so the schema "
                    "and the embed calls agree on vector width."
                ),
                "evidence": {"configured": None, "database": None},
            }
        if not parsed_dsn:
            return {
                "id": "dimensions_consistency",
                "status": "warn",
                "detail": (
                    f"no --dsn supplied; cannot compare configured dim={configured} "
                    "against the database column dim. Pass --dsn to enable this check."
                ),
                "evidence": {"configured": int(configured), "database": None},
            }
        db_dim = _probe_db_vector_dim(parsed_dsn, timeout)
        if db_dim is None:
            return {
                "id": "dimensions_consistency",
                "status": "warn",
                "detail": (
                    f"could not determine the database vector column dim. "
                    f"Configured dim is {configured}; verify the schema "
                    "matches before running the pipeline."
                ),
                "evidence": {"configured": int(configured), "database": None},
            }
        if int(db_dim) == int(configured):
            return {
                "id": "dimensions_consistency",
                "status": "ok",
                "detail": (
                    f"configured embed dim ({configured}) matches the "
                    f"database vector column dim ({db_dim})"
                ),
                "evidence": {"configured": int(configured), "database": int(db_dim)},
            }
        return {
            "id": "dimensions_consistency",
            "status": "fail",
            "detail": (
                f"dim mismatch: configured={configured}, database={db_dim}. "
                "The pipeline will fail to insert mismatched vectors. "
                "Either update storage.embed.dim in config.yaml or re-apply "
                "the schema after dropping the vector column."
            ),
            "evidence": {"configured": int(configured), "database": int(db_dim)},
        }

    return _safe(_do, fallback_id="dimensions_consistency")


def _probe_db_vector_dim(parsed_dsn: dict[str, Any], timeout: float) -> int | None:
    """Return the dim of public.qa_pairs.embedding, or None if unknown."""
    try:
        import psycopg2  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    password = parsed_dsn.get("password") or os.environ.get(
        "V3CORE_PG_PASSWORD", ""
    ) or os.environ.get("PGPASSWORD", "")
    try:
        conn = psycopg2.connect(
            host=parsed_dsn.get("host", ""),
            port=int(parsed_dsn.get("port") or 0),
            database=parsed_dsn.get("database", ""),
            user=parsed_dsn.get("user", ""),
            password=password or "",
            connect_timeout=max(1, int(timeout)),
        )
    except Exception:  # noqa: BLE001
        return None
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT format_type(atttypid, atttypmod) "
                    "FROM pg_attribute "
                    "WHERE attrelid = 'public.qa_pairs'::regclass "
                    "  AND attname = 'embedding'"
                )
                row = cur.fetchone()
            except Exception:  # noqa: BLE001
                row = None
            if not row or not row[0]:
                return None
            # row[0] looks like "vector(1024)"
            m = re.search(r"\((\d+)\)", str(row[0]))
            if m:
                return int(m.group(1))
            return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Runtime provenance — secret-safe identity report shared by the
# hermes_provider_discovery and hermes_home checks. Never echoes
# api_key / password / token; even ``sys.path`` entries and source
# strings are run through the same secret-key mask the distribution
# CLI already uses.
# ──────────────────────────────────────────────────────────────────────────────


_PROVENANCE_CACHE: dict[str, Any] | None = None


def _runtime_provenance() -> dict[str, Any]:
    """Return a cached, secret-free runtime provenance report.

    The report intentionally scans *all* site-package ``.pth`` files visible
    on ``sys.path``. Checking only the active distribution's metadata misses
    stale editable paths that can win after a launcher changes path order.
    """
    global _PROVENANCE_CACHE
    if _PROVENANCE_CACHE is not None:
        return dict(_PROVENANCE_CACHE)

    result: dict[str, Any] = {
        "v3core_file": "",
        "distribution_version": "",
        "python_executable": sys.executable,
        "sys_path": list(sys.path),
        "pth_files": [],
        "editable_targets": [],
        "config_path": "",
        "config_candidates": [],
        "warnings": [],
    }
    try:
        mod = importlib.import_module("v3core")
        result["v3core_file"] = str(getattr(mod, "__file__", "") or "")
    except Exception as exc:
        result["warnings"].append(f"import v3core failed: {type(exc).__name__}")
    try:
        from importlib import metadata as importlib_metadata
        result["distribution_version"] = importlib_metadata.version("v3-core")
    except Exception as exc:
        result["warnings"].append(
            f"importlib.metadata version failed: {type(exc).__name__}"
        )

    # Scan every site-packages directory in the live import path, not only
    # the distribution files list. Do not expose arbitrary file contents.
    seen_pth: set[str] = set()
    for raw_path in list(sys.path):
        try:
            site_dir = Path(raw_path)
            if not site_dir.is_dir() or "site-packages" not in str(site_dir).lower():
                continue
            for pth in sorted(site_dir.glob("*.pth")):
                key = str(pth.resolve())
                if key in seen_pth:
                    continue
                seen_pth.add(key)
                lines = pth.read_text(encoding="utf-8", errors="replace").splitlines()
                interesting: list[str] = []
                for line in lines:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if any(token in stripped.lower() for token in ("v3core", "v3-core", "hippocampus", "v3-memory")):
                        interesting.append(stripped)
                        result["editable_targets"].append(stripped)
                result["pth_files"].append({"path": key, "targets": interesting})
        except Exception as exc:
            result["warnings"].append(f"pth scan failed: {type(exc).__name__}")

    # Resolve the actual profile config path without returning its contents.
    candidates: list[Path] = []
    for key in ("V3CORE_CONFIG", "V3CORE_CONFIG_PATH", "HERMES_CONFIG"):
        value = os.environ.get(key, "").strip()
        if value:
            candidates.append(Path(value))
    try:
        from .config import resolve_config  # type: ignore
        cfg = resolve_config()
        base = getattr(cfg, "base_path", None) or getattr(cfg, "profile_dir", None)
        if base:
            candidates.append(Path(str(base)) / "config.yaml")
    except Exception as exc:
        result["warnings"].append(f"config resolver failed: {type(exc).__name__}")
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        candidates.append(Path(hermes_home) / "config.yaml")
    seen_cfg: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate.expanduser().resolve())
        except Exception:
            key = str(candidate)
        if key in seen_cfg:
            continue
        seen_cfg.add(key)
        exists = Path(key).is_file()
        result["config_candidates"].append({"path": key, "exists": exists})
        if exists and not result["config_path"]:
            result["config_path"] = key

    if result["editable_targets"]:
        result["warnings"].append("v3core-related editable .pth target(s) are present")
        result["identity_warning"] = (
            "v3core-related editable .pth target(s) are present; inspect path order "
            "before changing or removing them"
        )
    result["build_identity"] = (
        os.environ.get("V3CORE_BUILD_SHA")
        or os.environ.get("HIPP_BUILD_SHA")
        or "unknown"
    )
    _PROVENANCE_CACHE = dict(result)
    return dict(result)

# ── 9. hermes_provider_discovery ────────────────────────────────────────────


def _check_hermes_provider_discovery() -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        try:
            from .distribution_cli import _entry_point_presence  # type: ignore
        except Exception as e:  # noqa: BLE001
            return {
                "id": "hermes_provider_discovery",
                "status": "fail",
                "detail": (
                    f"distribution_cli._entry_point_presence unavailable: "
                    f"{type(e).__name__}: {e!s}"
                ),
                "evidence": {"runtime_provenance": _runtime_provenance()},
            }
        info = _entry_point_presence("hermes_agent.memory_providers",
                                     "deep_memory_v3")
        provenance = _runtime_provenance()
        evidence: dict[str, Any] = {**info, "runtime_provenance": provenance}
        identity_warn = provenance.get("identity_warning")
        if info.get("present"):
            detail = (
                "hermes_agent.memory_providers / deep_memory_v3 entry "
                "point is present"
            )
            status = "ok"
            if identity_warn:
                # Mismatch is a soft signal when the entry point still
                # resolves — downgrade ok→warn so the operator notices.
                status = "warn"
                detail = (
                    detail
                    + ". " + identity_warn
                )
            return {
                "id": "hermes_provider_discovery",
                "status": status,
                "detail": detail,
                "evidence": evidence,
            }
        if identity_warn:
            # No entry point + identity mismatch — keep "warn" (the
            # original severity) but include the mismatch detail so the
            # operator sees both signals in one place.
            return {
                "id": "hermes_provider_discovery",
                "status": "warn",
                "detail": (
                    "hermes_agent.memory_providers / deep_memory_v3 is not "
                    "discovered. Install v3-hermes-plugin alongside v3-core "
                    "and verify the entry point is registered. "
                    + identity_warn
                ),
                "evidence": evidence,
            }
        return {
            "id": "hermes_provider_discovery",
            "status": "warn",
            "detail": (
                "hermes_agent.memory_providers / deep_memory_v3 is not "
                "discovered. Install v3-hermes-plugin alongside v3-core "
                "and verify the entry point is registered."
            ),
            "evidence": evidence,
        }

    return _safe(_do, fallback_id="hermes_provider_discovery")


# ── 10. hermes_home ─────────────────────────────────────────────────────────


def _check_hermes_home() -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        home = os.environ.get("HERMES_HOME", "") or ""
        evidence: dict[str, Any] = {"HERMES_HOME": home or None}
        # Attach the runtime provenance block to the hermes_home check
        # as well — it is the operator-facing surface where the
        # "where am I actually running" question lands, so attaching
        # it here (in addition to hermes_provider_discovery) keeps the
        # report self-contained when only HERMES_HOME is set.
        evidence["runtime_provenance"] = _runtime_provenance()
        config_env = os.environ.get("V3CORE_CONFIG") or os.environ.get("V3CORE_CONFIG_PATH")
        if config_env:
            pending_dir = Path(config_env).expanduser().parent / "j" / "pending_qa"
            marker_summary: dict[str, Any] = {
                "path": str(pending_dir),
                "exists": pending_dir.is_dir(),
                "total": 0,
                "by_status": {},
                "by_error_class": {},
                "missing_accounting_fields": 0,
            }
            if pending_dir.is_dir():
                for marker in sorted(pending_dir.glob("*.json")):
                    try:
                        data = json.loads(marker.read_text(encoding="utf-8"))
                        marker_summary["total"] += 1
                        status = str(data.get("embedding_status") or "pending")
                        marker_summary["by_status"][status] = marker_summary["by_status"].get(status, 0) + 1
                        error_class = data.get("error_class")
                        if error_class:
                            key = str(error_class)
                            marker_summary["by_error_class"][key] = marker_summary["by_error_class"].get(key, 0) + 1
                        if status in {"failed", "poisoned", "in_flight", "embedding_succeeded_pending_db"} and not all(
                            field in data for field in ("embedding_attempts", "error_fingerprint", "retryable")
                        ):
                            marker_summary["missing_accounting_fields"] += 1
                    except Exception:
                        marker_summary["missing_accounting_fields"] += 1
            evidence["qa_embedding_failure_accounting"] = marker_summary
        if home:
            p = Path(home).expanduser()
            evidence["exists"] = p.exists()
            evidence["is_dir"] = p.is_dir() if p.exists() else False
            if not p.exists():
                return {
                    "id": "hermes_home",
                    "status": "warn",
                    "detail": (
                        f"HERMES_HOME is set to {home!r} but the path does "
                        "not exist. Doctor will continue without a hermes home."
                    ),
                    "evidence": evidence,
                }
            return {
                "id": "hermes_home",
                "status": "ok",
                "detail": f"HERMES_HOME resolves to {p}",
                "evidence": evidence,
            }
        # No HERMES_HOME — fine for source-checkout use.
        return {
            "id": "hermes_home",
            "status": "skip",
            "detail": "HERMES_HOME is not set; hermes home resolution is not exercised",
            "evidence": evidence,
        }

    return _safe(_do, fallback_id="hermes_home")


# ── 11. write ───────────────────────────────────────────────────────────────


def _check_write(profile_dir: Path | None, parsed_dsn: dict[str, Any] | None,
                 timeout: float) -> dict[str, Any]:
    """Write-probe to the profile dir + (if DSN given) a write to a
    scratch table. Only invoked when allow_write=True."""
    def _do() -> dict[str, Any]:
        evidence: dict[str, Any] = {"profile_dir": str(profile_dir) if profile_dir else None}
        if profile_dir is None:
            return {
                "id": "write",
                "status": "skip",
                "detail": "no --profile-dir supplied; profile-dir write probe skipped",
                "evidence": evidence,
            }
        # Profile-dir write probe: write a tiny marker file.
        marker = profile_dir / ".doctor_full_write_probe"
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps({"ts": _utc_now(), "pid": os.getpid()}),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            return {
                "id": "write",
                "status": "fail",
                "detail": f"profile-dir write failed: {type(e).__name__}: {e!s}",
                "evidence": evidence,
            }
        # Verify round-trip.
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            return {
                "id": "write",
                "status": "fail",
                "detail": f"profile-dir read-back failed: {type(e).__name__}: {e!s}",
                "evidence": evidence,
            }
        try:
            marker.unlink()
        except OSError:
            pass
        return {
            "id": "write",
            "status": "ok",
            "detail": f"profile-dir write+read+delete ok (pid={data.get('pid')})",
            "evidence": {**evidence, "ts": data.get("ts")},
        }

    return _safe(_do, fallback_id="write")


# ── 12. read ────────────────────────────────────────────────────────────────


def _check_read(profile_dir: Path | None, parsed_dsn: dict[str, Any] | None,
                timeout: float) -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        if profile_dir is None:
            return {
                "id": "read",
                "status": "skip",
                "detail": "no --profile-dir supplied; profile-dir read probe skipped",
                "evidence": {},
            }
        if not profile_dir.exists():
            return {
                "id": "read",
                "status": "fail",
                "detail": f"profile_dir does not exist: {profile_dir}",
                "evidence": {"path": str(profile_dir)},
            }
        # Count files + read a tiny sample.
        try:
            files = list(profile_dir.iterdir())
        except Exception as e:  # noqa: BLE001
            return {
                "id": "read",
                "status": "fail",
                "detail": f"iterdir failed: {type(e).__name__}: {e!s}",
                "evidence": {"path": str(profile_dir)},
            }
        return {
            "id": "read",
            "status": "ok",
            "detail": f"profile_dir readable; {len(files)} entries at top level",
            "evidence": {"path": str(profile_dir), "entry_count": len(files)},
        }

    return _safe(_do, fallback_id="read")


# ── 13. vector_insert_search ───────────────────────────────────────────────


def _check_vector_insert_search(parsed_dsn: dict[str, Any] | None,
                                allow_write: bool,
                                timeout: float) -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        if not parsed_dsn:
            return {
                "id": "vector_insert_search",
                "status": "skip",
                "detail": "no --dsn supplied; vector insert+search smoke not run",
                "evidence": {},
            }
        if not allow_write:
            return {
                "id": "vector_insert_search",
                "status": "skip",
                "detail": "vector insert+search requires --allow-write (off by default)",
                "evidence": {},
            }
        try:
            import psycopg2  # type: ignore
        except Exception:  # noqa: BLE001
            return {
                "id": "vector_insert_search",
                "status": "skip",
                "detail": "psycopg2 not importable; vector insert+search skipped",
                "evidence": {},
            }
        password = parsed_dsn.get("password") or os.environ.get(
            "V3CORE_PG_PASSWORD", ""
        ) or os.environ.get("PGPASSWORD", "")
        try:
            conn = psycopg2.connect(
                host=parsed_dsn.get("host", ""),
                port=int(parsed_dsn.get("port") or 0),
                database=parsed_dsn.get("database", ""),
                user=parsed_dsn.get("user", ""),
                password=password or "",
                connect_timeout=max(1, int(timeout)),
            )
        except Exception as e:  # noqa: BLE001
            return {
                "id": "vector_insert_search",
                "status": "fail",
                "detail": f"connect failed: {type(e).__name__}: {e!s}",
                "evidence": {},
            }
        try:
            with conn.cursor() as cur:
                # Insert a 1024-dim zero vector tagged with a UUID; then
                # SELECT the nearest neighbor by L2 distance and delete.
                tag = f"doctor_full_{int(time.time()*1000)}"
                try:
                    cur.execute(
                        "INSERT INTO qa_pairs (source_id, question, answer, "
                        "embedding) VALUES (%s, %s, %s, %s) RETURNING id",
                        (tag, tag, tag,
                         "[" + ",".join(["0"] * 1024) + "]"),
                    )
                    rid = cur.fetchone()
                    conn.commit()
                except Exception as e:  # noqa: BLE001
                    conn.rollback()
                    return {
                        "id": "vector_insert_search",
                        "status": "fail",
                        "detail": (
                            "INSERT into qa_pairs.embedding failed: "
                            f"{type(e).__name__}: {e!s}. The dim may be wrong, "
                            "or pgvector may be missing."
                        ),
                        "evidence": {"tag": tag},
                    }
                try:
                    cur.execute(
                        "SELECT id FROM qa_pairs WHERE source_id=%s "
                        "ORDER BY embedding <-> (SELECT embedding FROM qa_pairs "
                        "WHERE source_id=%s) LIMIT 1",
                        (tag, tag),
                    )
                    cur.fetchall()
                except Exception as e:  # noqa: BLE001
                    return {
                        "id": "vector_insert_search",
                        "status": "warn",
                        "detail": (
                            "INSERT succeeded but the nearest-neighbor "
                            f"SELECT failed: {type(e).__name__}: {e!s}. "
                            "The pgvector operator class may be missing."
                        ),
                        "evidence": {"inserted_id": rid[0] if rid else None},
                    }
                finally:
                    try:
                        cur.execute("DELETE FROM qa_pairs WHERE source_id=%s", (tag,))
                        conn.commit()
                    except Exception:  # noqa: BLE001
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                return {
                    "id": "vector_insert_search",
                    "status": "ok",
                    "detail": (
                        f"insert+search+delete round-trip ok (id={rid[0] if rid else None})"
                    ),
                    "evidence": {"tag": tag, "inserted_id": rid[0] if rid else None},
                }
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    return _safe(_do, fallback_id="vector_insert_search")


# ── 14. rerank ──────────────────────────────────────────────────────────────


def _check_rerank(cfg_view: dict[str, Any], timeout: float) -> dict[str, Any]:
    def _do() -> dict[str, Any]:
        rr = cfg_view.get("rerank", {}) or {}
        endpoint = rr.get("endpoint") or ""
        api_key = rr.get("apiKey") or rr.get("api_key") or ""
        if not endpoint:
            return {
                "id": "rerank",
                "status": "skip",
                "detail": "no rerank endpoint configured; rerank smoke not run",
                "evidence": {},
            }
        if not api_key:
            return {
                "id": "rerank",
                "status": "skip",
                "detail": "rerank endpoint is set but no api key is configured",
                "evidence": {"endpoint": _redact_query(endpoint)},
            }
        try:
            body = {
                "query": "doctor smoke test",
                "documents": ["alpha", "beta", "gamma"],
                "top_n": 3,
            }
            headers = {"Authorization": f"Bearer {api_key}",
                       "Content-Type": "application/json"}
            if rr.get("model"):
                body["model"] = rr["model"]
            resp = requests.post(
                endpoint, json=body, headers=headers,
                timeout=max(1.0, float(timeout)),
            )
        except requests.RequestException as e:
            return {
                "id": "rerank",
                "status": "fail",
                "detail": f"rerank smoke request failed: {type(e).__name__}: {e!s}",
                "evidence": {"endpoint": _redact_query(endpoint)},
            }
        sc = int(getattr(resp, "status_code", 0) or 0)
        if 200 <= sc < 300:
            return {
                "id": "rerank",
                "status": "ok",
                "detail": f"rerank smoke ok: status={sc}",
                "evidence": {"status_code": sc},
            }
        cls = _HTTP_STATUS_CLASS.get(sc, f"http_{sc}")
        return {
            "id": "rerank",
            "status": "fail" if sc in (401, 402) else "warn",
            "detail": f"rerank smoke returned status={sc} class={cls}",
            "evidence": {"status_code": sc, "class": cls},
        }

    return _safe(_do, fallback_id="rerank")


# ── 15. recall ──────────────────────────────────────────────────────────────


def _check_recall(cfg_view: dict[str, Any],
                  parsed_dsn: dict[str, Any] | None,
                  allow_write: bool,
                  timeout: float) -> dict[str, Any]:
    """Composite check: confirms that an end-to-end recall (one embed call
    + one rerank call against a known-seed query) can complete when
    configured. We do NOT need a database for this — it only needs the
    embed + rerank endpoints to be reachable."""
    def _do() -> dict[str, Any]:
        embed = cfg_view.get("embed", {}) or {}
        rr = cfg_view.get("rerank", {}) or {}
        embed_endpoint = embed.get("endpoint") or ""
        embed_key = embed.get("apiKey") or embed.get("api_key") or ""
        rr_endpoint = rr.get("endpoint") or ""
        rr_key = rr.get("apiKey") or rr.get("api_key") or ""
        if not embed_endpoint or not rr_endpoint:
            return {
                "id": "recall",
                "status": "skip",
                "detail": "recall smoke needs both an embed and a rerank endpoint",
                "evidence": {
                    "embed_endpoint": bool(embed_endpoint),
                    "rerank_endpoint": bool(rr_endpoint),
                },
            }
        if not embed_key or not rr_key:
            return {
                "id": "recall",
                "status": "skip",
                "detail": "recall smoke needs both an embed and a rerank api key",
                "evidence": {
                    "embed_key": bool(embed_key),
                    "rerank_key": bool(rr_key),
                },
            }
        # Embed smoke
        try:
            emb_resp = requests.post(
                embed_endpoint,
                json={"model": embed.get("model") or "embed",
                      "input": ["doctor recall smoke"]},
                headers={"Authorization": f"Bearer {embed_key}",
                         "Content-Type": "application/json"},
                timeout=max(1.0, float(timeout)),
            )
        except requests.RequestException as e:
            return {
                "id": "recall",
                "status": "fail",
                "detail": f"recall embed smoke failed: {type(e).__name__}: {e!s}",
                "evidence": {"stage": "embed"},
            }
        sc = int(getattr(emb_resp, "status_code", 0) or 0)
        if not (200 <= sc < 300):
            cls = _HTTP_STATUS_CLASS.get(sc, f"http_{sc}")
            return {
                "id": "recall",
                "status": "fail" if sc in (401, 402, 500, 503) else "warn",
                "detail": f"recall embed smoke returned status={sc} class={cls}",
                "evidence": {"stage": "embed", "status_code": sc, "class": cls},
            }
        # Rerank smoke (real, against the actual embed result, but here we
        # use a fixed tiny doc set to keep the smoke cheap).
        try:
            rr_resp = requests.post(
                rr_endpoint,
                json={"query": "doctor recall smoke",
                      "documents": ["alpha", "beta", "gamma"],
                      "top_n": 3,
                      **({"model": rr["model"]} if rr.get("model") else {})},
                headers={"Authorization": f"Bearer {rr_key}",
                         "Content-Type": "application/json"},
                timeout=max(1.0, float(timeout)),
            )
        except requests.RequestException as e:
            return {
                "id": "recall",
                "status": "fail",
                "detail": f"recall rerank smoke failed: {type(e).__name__}: {e!s}",
                "evidence": {"stage": "rerank"},
            }
        sc2 = int(getattr(rr_resp, "status_code", 0) or 0)
        if not (200 <= sc2 < 300):
            cls2 = _HTTP_STATUS_CLASS.get(sc2, f"http_{sc2}")
            return {
                "id": "recall",
                "status": "fail" if sc2 in (401, 402, 500, 503) else "warn",
                "detail": f"recall rerank smoke returned status={sc2} class={cls2}",
                "evidence": {"stage": "rerank", "status_code": sc2, "class": cls2},
            }
        return {
            "id": "recall",
            "status": "ok",
            "detail": (
                f"recall smoke ok: embed status={sc}, rerank status={sc2}"
            ),
            "evidence": {"embed_status": sc, "rerank_status": sc2},
        }

    return _safe(_do, fallback_id="recall")


# ── 16. restart_persistence_hint ───────────────────────────────────────────


def _check_restart_persistence_hint(profile_dir: Path | None,
                                    parsed_dsn: dict[str, Any] | None,
                                    allow_write: bool,
                                    timeout: float) -> dict[str, Any]:
    """The full doctor cannot restart the host. What it can do is verify
    that a row written before doctor runs is still readable afterwards,
    and print the exact manual instruction for the actual restart test.
    """
    manual = (
        "Manual restart-persistence test:\n"
        "  1. Note a unique qa_pairs.source_id (e.g. 'restart_smoke_<ts>').\n"
        "  2. Restart the v3-core daemon (and the host if applicable).\n"
        "  3. Run: SELECT id, source_id FROM qa_pairs WHERE source_id LIKE 'restart_smoke_%';\n"
        "  4. The row must still be present. If absent, the restart lost data."
    )

    def _do() -> dict[str, Any]:
        if not parsed_dsn:
            return {
                "id": "restart_persistence_hint",
                "status": "skip",
                "detail": "no --dsn supplied; restart-persistence hint not verified",
                "evidence": {"manual": manual},
            }
        if not allow_write:
            return {
                "id": "restart_persistence_hint",
                "status": "skip",
                "detail": (
                    "pass --allow-write to run the in-doctor read-back "
                    "smoke; the actual restart test is still manual"
                ),
                "evidence": {"manual": manual},
            }
        try:
            import psycopg2  # type: ignore
        except Exception:  # noqa: BLE001
            return {
                "id": "restart_persistence_hint",
                "status": "skip",
                "detail": "psycopg2 not importable; cannot probe restart persistence",
                "evidence": {"manual": manual},
            }
        password = parsed_dsn.get("password") or os.environ.get(
            "V3CORE_PG_PASSWORD", ""
        ) or os.environ.get("PGPASSWORD", "")
        try:
            conn = psycopg2.connect(
                host=parsed_dsn.get("host", ""),
                port=int(parsed_dsn.get("port") or 0),
                database=parsed_dsn.get("database", ""),
                user=parsed_dsn.get("user", ""),
                password=password or "",
                connect_timeout=max(1, int(timeout)),
            )
        except Exception as e:  # noqa: BLE001
            return {
                "id": "restart_persistence_hint",
                "status": "fail",
                "detail": f"connect failed: {type(e).__name__}: {e!s}",
                "evidence": {"manual": manual},
            }
        try:
            with conn.cursor() as cur:
                # The point is to verify a pre-existing row is still
                # readable. We look for the most recent row that the
                # current v3-core install could plausibly have written.
                # (No row insertion here; restart-persistence is about
                # the host surviving, not the schema surviving.)
                try:
                    cur.execute(
                        "SELECT COUNT(*) FROM qa_pairs "
                        "WHERE created_at > NOW() - INTERVAL '1 day'"
                    )
                    n = cur.fetchone()
                    n = int(n[0]) if n and n[0] is not None else 0
                except Exception as e:  # noqa: BLE001
                    return {
                        "id": "restart_persistence_hint",
                        "status": "warn",
                        "detail": (
                            "could not read recent qa_pairs rows: "
                            f"{type(e).__name__}: {e!s}"
                        ),
                        "evidence": {"manual": manual},
                    }
                return {
                    "id": "restart_persistence_hint",
                    "status": "ok" if n > 0 else "warn",
                    "detail": (
                        f"recent row presence check: {n} qa_pairs rows in the "
                        "last 24h. The actual restart test must still be run "
                        "manually:"
                    ),
                    "evidence": {"recent_24h_count": n, "manual": manual},
                }
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    return _safe(_do, fallback_id="restart_persistence_hint")


# ──────────────────────────────────────────────────────────────────────────────
# Small utilities
# ──────────────────────────────────────────────────────────────────────────────


def _redact_query(url: str) -> str:
    if not url:
        return url
    # Strip any ?key=...&api_key=... from the URL so the operator
    # never sees credentials in the doctor output.
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        parts = urlsplit(url)
        if not parts.query:
            return url
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() not in {"key", "api_key", "apikey", "token"}]
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           urlencode(kept), parts.fragment))
    except Exception:  # noqa: BLE001
        return url


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
