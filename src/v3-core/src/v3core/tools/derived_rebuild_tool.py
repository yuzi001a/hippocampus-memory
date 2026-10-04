# -*- coding: utf-8 -*-
"""v3core.tools.derived_rebuild_tool — explicit M03 rebuild entry.

``v3_update(action='rebuild')`` runs the M03 THIRD stage for ONE committed
correction: it locally rebuilds a stale Topic as a NEW artifact and records
``replacement_derived_id`` on the old invalidation sidecar row.

It is deliberately the same bounded, on-demand shape as the propagation rerun
(``derived_propagation_tool``): no daemon, no service, no queue, and no full
historical rebuild. ``observer_note`` / ``yin_paragraph`` are pending/manual
this round and come back as ``REBUILD_NOT_SUPPORTED`` — truthfully, not hidden.

Fail-closed scope: the injected runtime scope is required exactly as it is for a
correction / propagation. A declared runtime with no pool/config refuses instead
of silently resolving the *default* profile's database. A missing sidecar table
is a truthful ``MIGRATION_REQUIRED``.

The provider is injectable through the host keyword ``rebuild_provider`` (a
deterministic stub in tests); it is NOT part of the model-facing schema, so a
model can never supply credentials or an endpoint. When nothing is injected the
default path is the project's configured LLM client.
"""
from __future__ import annotations

import json
import logging

try:
    from . import _safe_err
except (ImportError, AttributeError):
    try:
        from .. import _safe_err
    except (ImportError, AttributeError):
        def _safe_err(e, max_len=80):  # type: ignore[misc]
            text = repr(e)
            return text if len(text) <= max_len else text[: max_len - 3] + "..."

logger = logging.getLogger("v3core.tools.derived_rebuild_tool")

__all__ = ["handle_v3_rebuild"]

#: Machine-readable tool-layer failure codes (mirrors memory_correction).
INVALID_REQUEST = "INVALID_REQUEST"
SCOPE_UNAVAILABLE = "SCOPE_UNAVAILABLE"


def _fail(code: str, message: str, **extra) -> str:
    payload = {
        "success": False,
        "action": "rebuild",
        "error_code": code,
        "code": code,
        "error": message,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def handle_v3_rebuild(args: dict, **kw) -> str:
    """Rebuild the stale derived artifact(s) of one committed correction."""
    correction_id = args.get("correction_id")
    invalidation_id = args.get("invalidation_id")

    has_cid = isinstance(correction_id, str) and bool(correction_id.strip())
    has_iid = isinstance(invalidation_id, str) and bool(invalidation_id.strip())
    if not has_cid and not has_iid:
        return _fail(
            INVALID_REQUEST,
            "correction_id 或 invalidation_id is required and must be a "
            "non-empty string",
        )
    dry_run = bool(args.get("dry_run", False))

    try:
        from .memory_correction import resolve_scope
        pool, pg, _cfg = resolve_scope(kw)
    except Exception as exc:  # noqa: BLE001
        # A declared runtime with no pool/config must never fall through to the
        # default profile's database.
        return _fail(
            SCOPE_UNAVAILABLE,
            f"derived rebuild unavailable: {_safe_err(exc)}",
            correction_id=correction_id if has_cid else None,
            invalidation_id=invalidation_id if has_iid else None,
        )

    from .. import derived_rebuild as _dr

    target = pool if pool is not None else pg
    provider = kw.get("rebuild_provider")
    try:
        receipt = _dr.rebuild_topic(
            target,
            correction_id=correction_id if has_cid else None,
            invalidation_id=invalidation_id if has_iid else None,
            dry_run=dry_run,
            provider=provider,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("v3_update(rebuild) 未捕获异常")
        return _fail(
            _dr.REBUILD_FAILED,
            f"derived rebuild raised: {_safe_err(exc)}",
            correction_id=correction_id if has_cid else None,
            invalidation_id=invalidation_id if has_iid else None,
        )

    if not isinstance(receipt, dict):
        return _fail(
            _dr.REBUILD_FAILED,
            "derived rebuild returned an unusable receipt",
        )

    # The receipt's own truthful verdict is the answer — its status, counts,
    # lineage and generation_context are copied through verbatim.
    payload = dict(receipt)
    payload["action"] = "rebuild"
    return json.dumps(payload, ensure_ascii=False)
