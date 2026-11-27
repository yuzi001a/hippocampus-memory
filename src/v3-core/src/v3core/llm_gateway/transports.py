# -*- coding: utf-8 -*-
"""Protocol transport adapters for OpenAI Chat, OpenAI Responses, and Anthropic Messages."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping


def _normalize_model_for_profile(profile: Any, model: str) -> str:
    try:
        from .. import providers_opencode

        return providers_opencode.normalize_model_id(
            getattr(profile, "name", None),
            model,
            getattr(profile, "base_url", None),
        )
    except Exception:
        return str(model or "").strip()


class BaseTransport(ABC):
    """Protocol-level payload serializer and response parser."""

    api_mode: str

    @abstractmethod
    def endpoint_url(self, profile: Any) -> str:
        raise NotImplementedError

    @abstractmethod
    def build_payload(
        self,
        profile: Any,
        model: str,
        *,
        prompt: str = "",
        system: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        max_tokens: int = 16384,
        temperature: float | None = 0.3,
        schema: Mapping[str, Any] | None = None,
        schema_name: str = "output",
        reasoning_effort: str | None = None,
        thinking: Mapping[str, Any] | None = None,
        reasoning_split: bool = False,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def parse_response(self, data: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class OpenAIChatTransport(BaseTransport):
    """OpenAI-compatible ``/chat/completions`` transport."""

    api_mode = "chat_completions"

    def endpoint_url(self, profile: Any) -> str:
        return (getattr(profile, "base_url", "") or "").rstrip("/") + "/chat/completions"

    def build_payload(
        self,
        profile: Any,
        model: str,
        *,
        prompt: str = "",
        system: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        max_tokens: int = 16384,
        temperature: float | None = 0.3,
        schema: Mapping[str, Any] | None = None,
        schema_name: str = "output",
        reasoning_effort: str | None = None,
        thinking: Mapping[str, Any] | None = None,
        reasoning_split: bool = False,
    ) -> dict[str, Any]:
        norm_model = _normalize_model_for_profile(profile, model)
        payload: dict[str, Any] = {"model": norm_model}
        if messages is not None:
            payload["messages"] = (
                ([{"role": "system", "content": system}] if system else [])
                + list(messages)
            )
        else:
            msgs: list[dict[str, Any]] = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": prompt})
            payload["messages"] = msgs
        payload[profile.max_tokens_field] = max_tokens
        if temperature is not None and profile.supports_cap("temperature"):
            payload["temperature"] = temperature
        if schema is not None and profile.supports_cap("response_format"):
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": dict(schema),
                },
            }
        if thinking is not None and profile.supports_cap("thinking"):
            payload["thinking"] = dict(thinking)
        if reasoning_effort and profile.supports_cap("reasoning_effort"):
            payload["reasoning_effort"] = reasoning_effort
        if reasoning_split and profile.supports_cap("reasoning_split"):
            payload["reasoning_split"] = True
        return payload

    def parse_response(self, data: Mapping[str, Any]) -> dict[str, Any]:
        choices = data.get("choices") or []
        first = (choices[0] if choices else {}) or {}
        msg = first.get("message") or {}
        text = str(msg.get("content") or "")
        reasoning = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
        usage = dict(data.get("usage") or {})
        finish = str(first.get("finish_reason") or "")
        return {
            "text": text,
            "reasoning": reasoning,
            "usage": usage,
            "finish_reason": finish,
        }


class OpenAIResponsesTransport(BaseTransport):
    """OpenAI Responses ``/responses`` transport."""

    api_mode = "responses"

    def endpoint_url(self, profile: Any) -> str:
        return (getattr(profile, "base_url", "") or "").rstrip("/") + "/responses"

    def build_payload(
        self,
        profile: Any,
        model: str,
        *,
        prompt: str = "",
        system: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        max_tokens: int = 16384,
        temperature: float | None = 0.3,
        schema: Mapping[str, Any] | None = None,
        schema_name: str = "output",
        reasoning_effort: str | None = None,
        thinking: Mapping[str, Any] | None = None,
        reasoning_split: bool = False,
    ) -> dict[str, Any]:
        norm_model = _normalize_model_for_profile(profile, model)
        eff_system = system
        eff_input = prompt
        if messages is not None and not prompt:
            sys_parts = [str(system)] if system else []
            conv_lines: list[str] = []
            for m in messages:
                role = m.get("role", "user")
                content = m.get("content", "")
                if role == "system":
                    if content:
                        sys_parts.append(str(content))
                else:
                    conv_lines.append(f"{role}: {content}")
            eff_system = "\n\n".join(p for p in sys_parts if p) or None
            eff_input = "\n\n".join(conv_lines)
        payload: dict[str, Any] = {"model": norm_model, "input": eff_input}
        if eff_system:
            payload["instructions"] = eff_system
        payload[profile.max_tokens_field] = max_tokens
        if schema is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": dict(schema),
                }
            }
        if reasoning_effort and profile.supports_cap("reasoning_effort"):
            payload["reasoning"] = {"effort": reasoning_effort}
        return payload

    def parse_response(self, data: Mapping[str, Any]) -> dict[str, Any]:
        text = ""
        reasoning = ""
        for item in data.get("output") or []:
            itype = (item or {}).get("type")
            if itype == "reasoning":
                for part in (item or {}).get("summary") or []:
                    reasoning += str((part or {}).get("text") or "")
                continue
            for part in (item or {}).get("content") or []:
                ptype = (part or {}).get("type")
                if ptype in ("output_text", "text"):
                    text += str((part or {}).get("text") or "")
        usage = dict(data.get("usage") or {})
        finish = str(data.get("status") or "")
        if data.get("incomplete_details"):
            finish = "incomplete"
        return {
            "text": text,
            "reasoning": reasoning,
            "usage": usage,
            "finish_reason": finish,
        }


class AnthropicMessagesTransport(BaseTransport):
    """Anthropic Messages ``/messages`` transport."""

    api_mode = "anthropic_messages"

    def endpoint_url(self, profile: Any) -> str:
        return (getattr(profile, "base_url", "") or "").rstrip("/") + "/messages"

    def build_payload(
        self,
        profile: Any,
        model: str,
        *,
        prompt: str = "",
        system: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        max_tokens: int = 16384,
        temperature: float | None = 0.3,
        schema: Mapping[str, Any] | None = None,
        schema_name: str = "output",
        reasoning_effort: str | None = None,
        thinking: Mapping[str, Any] | None = None,
        reasoning_split: bool = False,
    ) -> dict[str, Any]:
        norm_model = _normalize_model_for_profile(profile, model)
        raw_msgs = (
            list(messages)
            if messages is not None
            else [{"role": "user", "content": prompt}]
        )
        sys_parts = [str(system)] if system else []
        non_system_msgs: list[dict[str, Any]] = []
        for m in raw_msgs:
            if m.get("role") == "system":
                content = m.get("content")
                if content:
                    sys_parts.append(str(content))
            else:
                non_system_msgs.append(m)
        payload: dict[str, Any] = {
            "model": norm_model,
            "messages": non_system_msgs,
        }
        eff_system = "\n\n".join(p for p in sys_parts if p)
        if eff_system:
            payload["system"] = eff_system
        payload[profile.max_tokens_field] = max_tokens
        if temperature is not None and profile.supports_cap("temperature"):
            payload["temperature"] = temperature
        if thinking is not None and profile.supports_cap("thinking"):
            payload["thinking"] = dict(thinking)
        return payload

    def parse_response(self, data: Mapping[str, Any]) -> dict[str, Any]:
        text = ""
        reasoning = ""
        for block in data.get("content") or []:
            btype = (block or {}).get("type")
            if btype == "text":
                text += str((block or {}).get("text") or "")
            elif btype == "thinking":
                reasoning += str((block or {}).get("thinking") or "")
        usage = dict(data.get("usage") or {})
        finish = str(data.get("stop_reason") or "")
        return {
            "text": text,
            "reasoning": reasoning,
            "usage": usage,
            "finish_reason": finish,
        }


TRANSPORT_REGISTRY: dict[str, BaseTransport] = {
    "chat_completions": OpenAIChatTransport(),
    "responses": OpenAIResponsesTransport(),
    "anthropic_messages": AnthropicMessagesTransport(),
}


def get_transport(api_mode: str) -> BaseTransport:
    """Lookup protocol transport by ``api_mode`` (defaults to chat_completions)."""
    key = str(api_mode or "").strip().lower()
    transport = TRANSPORT_REGISTRY.get(key)
    if transport is None:
        return TRANSPORT_REGISTRY["chat_completions"]
    return transport
