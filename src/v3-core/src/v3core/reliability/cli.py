"""Thin CLI handlers for the reliability layer.

Three entry points:

  - ``handle_health(args) -> int``     — exit 0/1/2 (healthy/degraded/unhealthy or collect-fail)
  - ``handle_diagnose(args) -> int``   — exit 0/1/2 (no active / active / diagnose-fail)
  - ``handle_repair(args) -> int``     — exit 0/1/2 (no candidates / has candidates / repair-fail)

The handlers are pure orchestration: they construct a ``HealthService``,
drive the ``collect → diagnose → plan`` pipeline, and emit either JSON
(``args.json=True``) or a one-line-per-section human summary. **They
never touch the write pipeline.** ``handle_repair`` honors ``--apply``
only by refusing it with exit 2 and the
``REPAIR_APPLY_NOT_IMPLEMENTED_IN_V1`` error code.

Hard contracts:

  * **Secret safety** — every summary / detail string goes through
    ``redaction.sanitize_text``; the configured DSN password never
    appears in any output (the ``pg`` dict is never echoed verbatim).
  * **Lazy imports** — ``v3core.config`` and the service constructors
    are imported inside the handlers; a partial install (no
    ``v3core.config``) degrades to a clean exit-2 with an error JSON,
    not an import-time crash.
  * **Deterministic JSON** — ``ensure_ascii=False, indent=2, sort_keys=True``.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .diagnose import diagnose
from .models import (
    OVERALL_DEGRADED,
    OVERALL_HEALTHY,
    OVERALL_UNHEALTHY,
    HealthReport,
)
from .redaction import sanitize_text
from .repair import REPAIR_APPLY_NOT_IMPLEMENTED

logger = logging.getLogger(__name__)


# ── Service construction helpers ──────────────────────────────────────


def _resolve_base_path(profile_dir: str | None) -> Path:
    """Resolve the v3-core base path with graceful fallback.

    Tries ``v3core.config._resolve_data_dir`` first (the engine's
    authoritative resolver). If it raises or the module is missing,
    fall back to ``~/.v3-core/profiles/default``. ``profile_dir``
    overrides everything when present (used by tests + operators that
    point at an explicit install).
    """
    if profile_dir:
        try:
            return Path(profile_dir).expanduser().resolve()
        except Exception:
            return Path(profile_dir).expanduser()
    try:
        from v3core import config as _cfg
        try:
            return Path(_cfg._resolve_data_dir()).expanduser().resolve()
        except Exception:
            pass
    except Exception:
        pass
    return Path.home() / ".v3-core" / "profiles" / "default"


def _resolve_pg_dict(profile_dir: str | None) -> dict[str, Any]:
    """Build the ``pg`` dict for ``HealthService`` from the resolved config.

    Reads ``v3core.config.resolve_config()`` (legacy dict shape) and
    pulls the ``pg`` block. Returns an empty dict when the resolver
    raises — the service treats that as "no connection configured"
    (the storage/memory_write sections will skip cleanly).
    """
    try:
        from v3core import config as _cfg
        try:
            cfg = _cfg.resolve_config(return_legacy=True)
        except TypeError:
            cfg = _cfg.resolve_config()
        except Exception:
            cfg = {}
    except Exception:
        cfg = {}

    if not isinstance(cfg, dict):
        try:
            cfg = cfg.to_legacy_dict()  # type: ignore[union-attr]
        except Exception:
            cfg = {}

    pg = cfg.get("pg", {}) if isinstance(cfg, dict) else {}
    if not isinstance(pg, dict):
        pg = {}

    # Hard-redact any password leaking into the dict: we keep the key
    # but the *value* is replaced with a constant sentinel so that
    # even an accidental ``pg["password"]`` echo shows up as ``<redacted>``.
    out: dict[str, Any] = {}
    for k, v in pg.items():
        if k in ("password", "passwd", "pwd"):
            out[k] = "<redacted>"
        else:
            out[k] = v
    return out


def _build_service(args: argparse.Namespace) -> Any:
    """Construct a ``HealthService`` from the CLI args.

    Lazy-imports ``HealthService`` so a broken install never crashes
    the handlers at module load.
    """
    from .health import HealthService

    base_path = _resolve_base_path(getattr(args, "profile_dir", None))
    pg = _resolve_pg_dict(getattr(args, "profile_dir", None))
    # ``marker_dir`` defaults to ``base_path / 'j' / 'pending_qa'`` —
    # the engine's contract location for the failure ledger.
    marker_dir = base_path / "j" / "pending_qa"

    return HealthService(
        profile_dir=base_path,
        base_path=base_path,
        pg=pg,
        marker_dir=marker_dir,
        window_hours=int(getattr(args, "window_hours", 24) or 24),
        allow_production_read=bool(getattr(args, "allow_production_read", False)),
        deep=bool(getattr(args, "deep", False)),
        debug_paths=bool(getattr(args, "debug_paths", False)),
    )


# ── Output helpers ────────────────────────────────────────────────────


def _emit(payload: Any, *, json_mode: bool) -> None:
    """Emit ``payload`` either as JSON or as a one-line-per-section human summary."""
    if json_mode:
        sys.stdout.write(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        )
        sys.stdout.write("\n")
        return

    # Human summary — terse, no markdown decorations.
    if isinstance(payload, dict):
        overall = payload.get("overall") or payload.get("status") or "?"
        schema = payload.get("schema_version")
        if schema:
            sys.stdout.write(f"overall: {overall}  (schema_version={schema})\n")
        else:
            sys.stdout.write(f"status: {overall}\n")

        # Sections — emit one row per known section key.
        for sec in (
            "runtime",
            "storage",
            "memory_write",
            "failure_accounting",
            "derived_memory",
            "providers",
        ):
            if sec in payload:
                _emit_section_row(sec, payload[sec])

        # Counts row.
        if "checks" in payload and isinstance(payload["checks"], list):
            counts = _count_by_status(payload["checks"])
            sys.stdout.write(
                f"checks: total={len(payload['checks'])} "
                f"ok={counts.get('ok',0)} warn={counts.get('warn',0)} "
                f"fail={counts.get('fail',0)} skip={counts.get('skip',0)} "
                f"unknown={counts.get('unknown',0)}\n"
            )

        # Issues / actions rows.
        if "issues" in payload and isinstance(payload["issues"], list):
            sys.stdout.write(f"issues: {len(payload['issues'])}\n")
            for issue in payload["issues"]:
                if isinstance(issue, dict):
                    code = issue.get("code", "?")
                    sev = issue.get("severity", "?")
                    sys.stdout.write(f"  - [{sev}] {code}\n")
        if "actions" in payload and isinstance(payload["actions"], list):
            sys.stdout.write(f"actions: {len(payload['actions'])}\n")
            for act in payload["actions"]:
                if isinstance(act, dict):
                    aid = act.get("action_id", "?")
                    code = act.get("issue_code", "?")
                    sys.stdout.write(f"  - {aid}  (from {code})\n")

        if "error" in payload:
            sys.stdout.write(f"error: {sanitize_text(str(payload['error']))}\n")
    else:
        sys.stdout.write(str(payload) + "\n")


def _emit_section_row(name: str, value: Any) -> None:
    if isinstance(value, dict):
        # Pick the most informative one-liner for each section.
        if name == "storage":
            r = value.get("reachable")
            if r is False:
                sys.stdout.write(f"storage: reachable=false\n")
            else:
                sys.stdout.write(f"storage: reachable=true\n")
            return
        if name == "memory_write":
            sys.stdout.write(
                f"memory_write: recent_qa={value.get('recent_qa','?')} "
                f"recent_null={value.get('recent_embedding_null','?')} "
                f"null_total={value.get('embedding_null_total','?')} "
                f"empty_recent={value.get('empty_answer_recent','?')}\n"
            )
            return
        if name == "failure_accounting":
            cur = value.get("current_active", "?")
            retry = value.get("current_retrying", "?")
            poisoned = value.get("current_poisoned", "?")
            sys.stdout.write(
                f"failure_accounting: current_active={cur} "
                f"current_retrying={retry} current_poisoned={poisoned}\n"
            )
            return
        if name == "derived_memory":
            sys.stdout.write(
                f"derived_memory: topics_total={value.get('topics_total','?')} "
                f"backlog={value.get('backlog','?')}\n"
            )
            return
        if name == "providers":
            cfg = value.get("configured", {}) or {}
            sys.stdout.write(
                f"providers: embed={cfg.get('embed','?')} "
                f"rerank={cfg.get('rerank','?')} llm={cfg.get('llm','?')}\n"
            )
            return
        if name == "runtime":
            sys.stdout.write("runtime: collected\n")
            return
    sys.stdout.write(f"{name}: present\n")


def _count_by_status(checks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in checks:
        if not isinstance(c, dict):
            continue
        s = c.get("status")
        if isinstance(s, str):
            counts[s] = counts.get(s, 0) + 1
    return counts


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ── Health handler ─────────────────────────────────────────────────────


def handle_health(args: argparse.Namespace) -> int:
    """Run ``health.collect`` and emit JSON or human summary."""
    json_mode = bool(getattr(args, "json", False))
    try:
        service = _build_service(args)
        report = service.collect()
    except Exception as exc:  # noqa: BLE001 — surface as rc=2
        _emit({
            "command": "health",
            "schema_version": "1",
            "status": "error",
            "generated_at": _now_iso(),
            "error": sanitize_text(f"{type(exc).__name__}: {exc}"),
        }, json_mode=json_mode)
        return 2

    overall = report.overall or OVERALL_HEALTHY
    if overall == OVERALL_HEALTHY:
        return_code = 0
    elif overall == OVERALL_DEGRADED:
        return_code = 1
    elif overall == OVERALL_UNHEALTHY:
        return_code = 2
    else:
        # Unknown verdict — treat as failure for safety.
        return_code = 2

    if json_mode:
        payload = report.to_dict()
        payload["command"] = "health"
        _emit(payload, json_mode=True)
    else:
        _emit(report.to_dict(), json_mode=False)
    return return_code


# ── Diagnose handler ───────────────────────────────────────────────────


def handle_diagnose(args: argparse.Namespace) -> int:
    """Run ``collect → diagnose`` and emit JSON or human summary."""
    json_mode = bool(getattr(args, "json", False))
    try:
        service = _build_service(args)
        report = service.collect()
        issues = diagnose(report)
    except Exception as exc:  # noqa: BLE001
        _emit({
            "command": "diagnose",
            "schema_version": "1",
            "status": "error",
            "generated_at": _now_iso(),
            "error": sanitize_text(f"{type(exc).__name__}: {exc}"),
        }, json_mode=json_mode)
        return 2

    # Exit code: 0 = no active issue (info-only or empty); 1 = has
    # severity >= warning; 2 reserved for failures (above).
    has_active = any(d.severity in ("error", "warning") for d in issues)
    return_code = 1 if has_active else 0

    if json_mode:
        payload = {
            "command": "diagnose",
            "schema_version": "1",
            "generated_at": _now_iso(),
            "overall": report.overall,
            "issues": [d.to_dict() for d in issues],
            "checks_total": len(report.checks),
        }
        _emit(payload, json_mode=True)
    else:
        _emit({
            "command": "diagnose",
            "schema_version": "1",
            "overall": report.overall,
            "issues": [d.to_dict() for d in issues],
            "checks": [c.to_dict() for c in report.checks],
        }, json_mode=False)
    return return_code


# ── Repair handler ─────────────────────────────────────────────────────


def handle_repair(args: argparse.Namespace) -> int:
    """Run ``collect → diagnose → plan_repairs`` (dry-run only).

    ``--apply`` is hard-disabled: even when the operator passes it, we
    exit 2 with a JSON error. There is no write path.
    """
    json_mode = bool(getattr(args, "json", False))

    # Hard reject ``--apply`` *before* doing any work — never even
    # construct the service. Output is identical between JSON / human
    # modes here (the JSON envelope is the only thing humans need).
    if bool(getattr(args, "apply", False)):
        payload = {
            "command": "repair",
            "error": REPAIR_APPLY_NOT_IMPLEMENTED,
            "detail": "repair --apply is not implemented in v1. "
                      "Only --dry-run is supported.",
            "generated_at": _now_iso(),
        }
        _emit(payload, json_mode=True)
        return 2

    try:
        service = _build_service(args)
        report = service.collect()
        issues = diagnose(report)
        # Lazy import to keep handler call graphs narrow.
        from .repair import plan_repairs
        actions = plan_repairs(issues)
    except Exception as exc:  # noqa: BLE001
        _emit({
            "command": "repair",
            "schema_version": "1",
            "status": "error",
            "generated_at": _now_iso(),
            "error": sanitize_text(f"{type(exc).__name__}: {exc}"),
        }, json_mode=json_mode)
        return 2

    actionable_count = sum(1 for a in actions if a.target_count > 0)
    return_code = 1 if actions else 0

    if json_mode:
        payload = {
            "command": "repair",
            "schema_version": "1",
            "dry_run": True,
            "generated_at": _now_iso(),
            "actions": [a.to_dict() for a in actions],
            "actionable_count": actionable_count,
            "issues_total": len(issues),
        }
        _emit(payload, json_mode=True)
    else:
        _emit({
            "command": "repair",
            "schema_version": "1",
            "overall": report.overall,
            "actions": [a.to_dict() for a in actions],
            "issues": [d.to_dict() for d in issues],
        }, json_mode=False)
    return return_code


__all__ = [
    "handle_health",
    "handle_diagnose",
    "handle_repair",
    "REPAIR_APPLY_NOT_IMPLEMENTED",
]