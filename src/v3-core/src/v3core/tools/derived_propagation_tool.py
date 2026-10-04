"""v3core.tools.derived_propagation_tool — explicit M03 propagation rerun entry.

``v3_update(action='propagate')`` replays the deterministic derived-memory
invalidation for ONE ``correction_id``. It exists so a propagation that failed
(or ran under an older version) after a committed correction can be re-run
without a daemon, a service or a queue: it is the same bounded, no-LLM
``derived_invalidation.propagate_correction`` the correction path calls, exposed
as a maintenance action.

Idempotency is inherited, not re-implemented: the deterministic
``invalidation_id`` plus ``UNIQUE (correction_id, derived_kind, derived_id)``
mean a second run inserts ZERO new rows and reports
``PROPAGATION_DEDUPLICATED``.

Fail-closed scope: the injected runtime scope is required exactly as it is for
a correction. A declared runtime with no pool/config refuses instead of
silently resolving the *default* profile's database. A missing sidecar table is
a truthful ``MIGRATION_REQUIRED`` (the receipt's own status), never a silent
fallback.
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

logger = logging.getLogger("v3core.tools.derived_propagation_tool")

__all__ = ["handle_v3_propagate"]

#: Machine-readable tool-layer failure codes (mirrors memory_correction).
INVALID_REQUEST = "INVALID_REQUEST"
SCOPE_UNAVAILABLE = "SCOPE_UNAVAILABLE"


def _fail(code: str, message: str, **extra) -> str:
    payload = {
        "success": False,
        "action": "propagate",
        "error_code": code,
        "code": code,
        "error": message,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def handle_v3_propagate(args: dict, **kw) -> str:
    """Re-run M03 propagation for one ``correction_id`` (idempotent)."""
    correction_id = args.get("correction_id")
    if not isinstance(correction_id, str) or not correction_id.strip():
        return _fail(
            INVALID_REQUEST,
            "correction_id is required and must be a non-empty string",
        )
    dry_run = bool(args.get("dry_run", False))

    try:
        from .memory_correction import resolve_scope
        pool, pg, cfg = resolve_scope(kw)
    except Exception as exc:  # noqa: BLE001
        # A declared runtime with no pool/config must never fall through to the
        # default profile's database.
        return _fail(
            SCOPE_UNAVAILABLE,
            f"derived propagation unavailable: {_safe_err(exc)}",
            correction_id=correction_id,
        )

    from .. import derived_invalidation as _di

    target = pool if pool is not None else pg
    try:
        receipt = _di.propagate_correction(
            target, correction_id=correction_id, dry_run=dry_run
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("v3_update(propagate) 未捕获异常")
        return _fail(
            _di.PROPAGATION_FAILED,
            f"derived propagation raised: {_safe_err(exc)}",
            correction_id=correction_id,
        )

    if not isinstance(receipt, dict):
        return _fail(
            _di.PROPAGATION_FAILED,
            "derived propagation returned an unusable receipt",
            correction_id=correction_id,
        )

    # The receipt's own truthful verdict is the answer — its status, counts and
    # derived_propagation are copied through verbatim, never re-derived.
    payload = dict(receipt)
    payload["action"] = "propagate"
    return json.dumps(payload, ensure_ascii=False)
