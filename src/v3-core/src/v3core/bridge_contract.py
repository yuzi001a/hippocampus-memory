"""Bridge/host contract — single source of truth for event identity, ingest
status semantics and the version/capability handshake.

B01 scope: transport/identity only.  No memory generation, no model calls,
no schema changes.  Keep this module free of PG/network imports so both the
HTTP (serve) and stdio (mcp) surfaces can depend on it.
"""
from __future__ import annotations

from typing import Any

BRIDGE_PROTOCOL_VERSION = "b01.1"

# Events without an explicit host keep the historical identity namespace.
LEGACY_HOST = "legacy"

STATUS_ACCEPTED = "accepted"
STATUS_DUPLICATE = "duplicate"
STATUS_RETRYABLE = "retryable"
STATUS_FAILED = "failed"

# Declared capabilities: only what actually exists today.  Do NOT declare
# automatic_recall / correction / multi_agent_share — those are not B01.
CAPABILITIES: dict[str, str] = {
    "events": "v1",
    "prefetch": "v1",
    "tools": "v1",
    "source_read": "v1",
    "status": "v1",
}

# Event payload fields.  ``session_id`` + an event id are required; every
# other field is provenance that must be preserved, not interpreted.
_OPTIONAL_FIELDS = (
    "project_id",
    "agent_id",
    "turn_id",
    "parent_event_id",
    "branch_id",
    "timestamp",
    "tool_calls",
    "tool_results",
)


def _non_empty(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_event(payload: dict) -> dict:
    """Normalize an external bridge event into the canonical shape.

    ``event_id`` is the canonical name; ``msg_id`` is accepted as the legacy
    alias, so no second identity system is created.

    Raises ``ValueError`` when session or event identity is missing.
    """
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a mapping")

    session_id = _non_empty(payload.get("session_id"))
    if not session_id:
        raise ValueError("event payload missing session_id")

    event_id = _non_empty(payload.get("event_id")) or _non_empty(payload.get("msg_id"))
    if not event_id:
        raise ValueError("event payload missing event_id (or legacy msg_id)")

    raw_host = _non_empty(payload.get("host"))
    host = raw_host or LEGACY_HOST

    event: dict[str, Any] = {
        "host": host,
        "session_id": session_id,
        "event_id": event_id,
        "legacy_host": host == LEGACY_HOST,
        "role": _non_empty(payload.get("role")),
        "content": payload.get("content") if payload.get("content") is not None else "",
    }
    for field_name in _OPTIONAL_FIELDS:
        event[field_name] = payload.get(field_name)
    return event


def event_identity(event: dict) -> tuple:
    """Canonical durable identity: (host, session_id, event_id)."""
    host = _non_empty(event.get("host")) or LEGACY_HOST
    return (host, _non_empty(event.get("session_id")), _non_empty(event.get("event_id")))


def qa_pairing_decision(*, role: str, event_turn_id, pending_turn_id,
                        pending_has_answer: bool) -> str:
    """Decide how one accepted event participates in QA pairing.

    Returns ``open`` / ``flush_open`` / ``append`` / ``hold_orphan`` / ``skip``.
    The rule that matters: never pair an answer with a question from another
    turn merely because it arrived.  With no turn identity we keep the
    historical arrival-order behaviour (backward compatibility); with turn
    identity present and disagreeing we hold instead of guessing.
    """
    role = (role or "").strip().lower()
    ev_turn = _non_empty(event_turn_id) or None
    pd_turn = _non_empty(pending_turn_id) or None
    has_pending = pd_turn is not None or bool(pending_has_answer)

    if role == "user":
        return "flush_open" if has_pending else "open"
    if role == "assistant":
        if not has_pending:
            return "hold_orphan" if ev_turn is not None else "skip"
        if ev_turn is None or pd_turn is None:
            return "append"  # legacy: no turn identity available
        return "append" if ev_turn == pd_turn else "hold_orphan"
    return "skip"


def ack_from_receipt(receipt: dict) -> dict:
    """Turn an ingest receipt into an honest ACK.

    Hard rule: a write that did not become durable must never be reported as
    success.
    """
    receipt = receipt or {}
    status = _non_empty(receipt.get("status")) or STATUS_FAILED
    if status not in (STATUS_ACCEPTED, STATUS_DUPLICATE, STATUS_RETRYABLE, STATUS_FAILED):
        status = STATUS_FAILED
    ack = {
        "ok": status in (STATUS_ACCEPTED, STATUS_DUPLICATE),
        "accepted": status == STATUS_ACCEPTED,
        "duplicate": status == STATUS_DUPLICATE,
        "status": status,
        "event_id": _non_empty(receipt.get("event_id")),
        "host": _non_empty(receipt.get("host")) or LEGACY_HOST,
    }
    source_id = receipt.get("source_id")
    if source_id:
        ack["source_id"] = source_id
    return ack


def get_bridge_capabilities() -> dict:
    """Version + capability handshake shared by HTTP /health and MCP v3_health.

    ``core_package_version`` is the installed package metadata (e.g. 4.0.0).
    The GitHub release tag (e.g. v0.2.2) is deliberately NOT hardcoded here.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            core_version = version("v3-core")
        except PackageNotFoundError:
            core_version = "unknown"
    except Exception:
        core_version = "unknown"

    return {
        "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
        "core_package_version": core_version,
        "server_version": "v3core-bridge/1.0",
        "capabilities": dict(CAPABILITIES),
    }


__all__ = [
    "BRIDGE_PROTOCOL_VERSION",
    "LEGACY_HOST",
    "STATUS_ACCEPTED",
    "STATUS_DUPLICATE",
    "STATUS_RETRYABLE",
    "STATUS_FAILED",
    "CAPABILITIES",
    "normalize_event",
    "event_identity",
    "qa_pairing_decision",
    "ack_from_receipt",
    "get_bridge_capabilities",
]
