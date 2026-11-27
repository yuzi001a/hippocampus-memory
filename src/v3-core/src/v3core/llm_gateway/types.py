# -*- coding: utf-8 -*-
"""Unified request and response contracts for ``v3core.llm_gateway``."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ModelRequest:
    """Normalized invocation intent passed into :func:`v3core.llm_gateway.invoke`."""

    model: str
    messages: list[dict[str, Any]] | None = None
    prompt: str = ""
    system: str | None = None
    max_tokens: int = 16384
    temperature: float | None = 0.3
    thinking_intent: str = "auto"  # "auto" | "enabled" | "disabled"
    effort_source: str = "explicit"  # "explicit" | "table"
    explicit_effort: str | None = None
    default_adaptive_effort: str | None = None
    enabled_budget_tokens: int | None = None
    force_none_when_disabled: bool = True
    reasoning_split: bool = False
    response_schema: Mapping[str, Any] | None = None
    response_schema_name: str = "output"
    api_key: str = ""
    base_url: str = ""
    proxy: str | None = None
    proxies: Mapping[str, str] | None = None
    timeout: float = 180.0
    retry_attempts: int = 3
    retry_backoff_s: float = 2.0
    retry_backoff_schedule: tuple[float, ...] | None = None
    extra_headers: Mapping[str, str] | None = None
    allow_reasoning_fallback_text: bool = False
    adaptive_retry_effort_fallback: str | None = "low"
    adaptive_retry_effort_only_minimax: bool = False
    cfg: Any = None
    profile: Any = None
    require_model: bool = True


@dataclass
class ModelResponse:
    """Normalized response returned by :func:`v3core.llm_gateway.invoke`."""

    text: str = ""
    reasoning: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""
    latency: float = 0.0
    error: str | None = None
    status_code: int | None = None
    raw_response: Any = None
    exception: BaseException | None = None
    attempts: int = 0
    adaptive_fallback_used: bool = False
    provider_name: str = ""
    api_mode: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None
