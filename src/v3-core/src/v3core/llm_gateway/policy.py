# -*- coding: utf-8 -*-
"""Unified thinking/reasoning policy, session affinity, and HTTP error translation."""
from __future__ import annotations

from typing import Any, Mapping

from .. import model_params, providers


def resolve_thinking_policy(
    model: str,
    cfg: Any,
    profile: Any,
    *,
    thinking_intent: str = "auto",
    effort_source: str = "explicit",
    explicit_effort: str | None = None,
    default_adaptive_effort: str | None = None,
    enabled_budget_tokens: int | None = None,
    force_none_when_disabled: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve ``(thinking_payload, reasoning_effort)`` with strict mutual exclusion.

    Invariant: when a channel supports ``thinking`` and an effective ``thinking``
    field is emitted (for example ``{"type": "disabled"}`` on classic models),
    ``reasoning_effort`` is ALWAYS ``None`` so legacy models never receive an
    unknown ``reasoning_effort`` parameter alongside ``thinking``.
    """
    params = model_params.resolve(model, cfg)
    thinking_field = model_params.thinking_field(params, thinking_intent or "auto")
    if (
        thinking_field is not None
        and enabled_budget_tokens
        and thinking_field.get("type") == "enabled"
    ):
        thinking_field = {
            "type": "enabled",
            "budget_tokens": int(enabled_budget_tokens),
        }

    supports_thinking = bool(profile is not None and profile.supports_cap("thinking"))
    if thinking_field is not None and supports_thinking:
        return thinking_field, None

    if effort_source == "table":
        if isinstance(cfg, Mapping):
            eff_cfg: Mapping[str, Any] = (
                cfg
                if explicit_effort is None
                else {**cfg, "reasoning_effort": explicit_effort}
            )
        else:
            eff_cfg = (
                {"reasoning_effort": explicit_effort}
                if explicit_effort is not None
                else {}
            )
        effort = model_params.reasoning_effort_for(
            params, eff_cfg, default=default_adaptive_effort
        )
    else:
        effort = model_params.explicit_reasoning_effort(
            {"reasoning_effort": explicit_effort}
        )

    intent_norm = str(thinking_intent or "").strip().lower()
    api_mode = getattr(profile, "api_mode", "") if profile is not None else ""
    if (
        not effort
        and force_none_when_disabled
        and intent_norm in ("disabled", "none", "off")
        and api_mode != providers.API_ANTHROPIC
        and profile is not None
        and profile.supports_cap("reasoning_effort")
    ):
        effort = "none"

    return None, effort


def resolve_session_id(cfg: Any) -> str | None:
    """Resolve the install-stable agent session identifier for sticky relays."""
    try:
        from ..agent_identity import resolve_session_id as _resolve

        return _resolve(cfg)
    except Exception:
        if isinstance(cfg, Mapping):
            val = cfg.get("session_id")
            if val:
                return str(val)
        return "v3core"


def explain_http_error(status: int, body: str) -> str:
    """Translate known provider HTTP errors into concise operator-facing text."""
    try:
        from .. import providers_opencode

        return providers_opencode.explain_error(status, body)
    except Exception:
        return str(body or "")[:200]
