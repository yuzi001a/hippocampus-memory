# -*- coding: utf-8 -*-
"""Unified model-calling adapter layer for ``v3core``."""
from __future__ import annotations

from .gateway import invoke, invoke_simple, resolve_simple_llm_cfg
from .policy import explain_http_error, resolve_session_id, resolve_thinking_policy
from .transports import (
    TRANSPORT_REGISTRY,
    AnthropicMessagesTransport,
    BaseTransport,
    OpenAIChatTransport,
    OpenAIResponsesTransport,
    get_transport,
)
from .types import ModelRequest, ModelResponse

__all__ = [
    "AnthropicMessagesTransport",
    "BaseTransport",
    "ModelRequest",
    "ModelResponse",
    "OpenAIChatTransport",
    "OpenAIResponsesTransport",
    "TRANSPORT_REGISTRY",
    "explain_http_error",
    "get_transport",
    "invoke",
    "invoke_simple",
    "resolve_session_id",
    "resolve_simple_llm_cfg",
    "resolve_thinking_policy",
]
