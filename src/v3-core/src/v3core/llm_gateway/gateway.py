# -*- coding: utf-8 -*-
"""Unified LLM gateway: single execution path for retries, adaptive fallback, and transport."""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Mapping

import requests

from .. import _safe_err, providers
from .policy import explain_http_error, resolve_session_id, resolve_thinking_policy
from .transports import get_transport
from .types import ModelRequest, ModelResponse

logger = logging.getLogger("v3core.llm_gateway")


def _build_proxies(request: ModelRequest) -> dict[str, str] | None:
    if request.proxies is not None:
        return dict(request.proxies) if request.proxies else None
    if request.proxy:
        return {"http": request.proxy, "https": request.proxy}
    return None


def _compute_retry_delay(
    attempt_idx: int,
    request: ModelRequest,
    response: Any = None,
) -> float:
    if response is not None:
        try:
            raw = getattr(response, "headers", {}).get("Retry-After")
            if raw:
                return max(0.0, min(float(str(raw).strip()), 30.0))
        except (TypeError, ValueError):
            pass
    if request.retry_backoff_schedule:
        sched = request.retry_backoff_schedule
        return float(sched[min(attempt_idx, len(sched) - 1)])
    return providers.retry_delay(attempt_idx, request.retry_backoff_s, response)


def invoke(
    request: ModelRequest,
    *,
    post_fn: Callable[..., Any] | None = None,
    sleep_fn: Callable[[float], Any] | None = None,
) -> ModelResponse:
    """Execute one LLM request through policy resolution, retry middleware, and transport."""
    model = str(request.model or "").strip()
    if not model and request.require_model:
        return ModelResponse(
            error="config_error: 未配置模型 (observer.model / llm.model)",
            latency=0.0,
        )

    profile = request.profile
    if profile is None:
        profile = providers.resolve_profile(
            model,
            request.cfg,
            api_key=request.api_key,
            base_url=request.base_url,
        )

    transport = get_transport(profile.api_mode)
    thinking_field, reasoning_effort = resolve_thinking_policy(
        model,
        request.cfg,
        profile,
        thinking_intent=request.thinking_intent,
        effort_source=request.effort_source,
        explicit_effort=request.explicit_effort,
        default_adaptive_effort=request.default_adaptive_effort,
        enabled_budget_tokens=request.enabled_budget_tokens,
        force_none_when_disabled=request.force_none_when_disabled,
    )
    schema = (
        request.response_schema
        if (request.response_schema is not None and providers.supports_schema(profile))
        else None
    )
    payload = transport.build_payload(
        profile,
        model,
        prompt=request.prompt,
        system=request.system,
        messages=request.messages,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        schema=schema,
        schema_name=request.response_schema_name,
        reasoning_effort=reasoning_effort,
        thinking=thinking_field,
        reasoning_split=request.reasoning_split,
    )
    headers = providers.build_headers(
        profile,
        request.api_key,
        session_id=resolve_session_id(request.cfg),
        extra=request.extra_headers,
    )
    url = transport.endpoint_url(profile)
    proxies = _build_proxies(request)
    post = post_fn or requests.post
    sleeper = sleep_fn or time.sleep

    attempts = max(1, int(request.retry_attempts or 1))
    t0 = time.time()
    resp: Any = None
    actual_attempts = 0
    adaptive_used = False

    try:
        for idx in range(attempts):
            actual_attempts = idx + 1
            try:
                resp = post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=request.timeout,
                    proxies=proxies,
                )
            except Exception as exc:
                if idx + 1 < attempts and providers.is_retryable_exception(exc):
                    delay = _compute_retry_delay(idx, request)
                    logger.warning(
                        "llm_gateway: 传输失败, 重试 %d/%d (%.1fs 后): %s",
                        idx + 1,
                        attempts,
                        delay,
                        _safe_err(exc)[:120],
                    )
                    sleeper(delay)
                    continue
                return ModelResponse(
                    error=f"请求失败: {_safe_err(exc)[:200]}",
                    latency=time.time() - t0,
                    exception=exc,
                    attempts=actual_attempts,
                    provider_name=profile.name,
                    api_mode=profile.api_mode,
                )

            if providers.is_retryable_status(resp.status_code) and idx + 1 < attempts:
                delay = _compute_retry_delay(idx, request, resp)
                logger.warning(
                    "llm_gateway: HTTP %s, 重试 %d/%d (%.1fs 后): %s",
                    resp.status_code,
                    idx + 1,
                    attempts,
                    delay,
                    (getattr(resp, "text", "") or "")[:100],
                )
                sleeper(delay)
                continue
            break

        if (
            resp is not None
            and resp.status_code == 400
            and "requires adaptive thinking" in (getattr(resp, "text", "") or "").lower()
            and "thinking" in payload
        ):
            adaptive_used = True
            logger.warning(
                "llm_gateway: 服务端拒绝 thinking 参数(requires adaptive thinking), 去掉该字段重试一次 model=%s",
                model,
            )
            retry_body = {k: v for k, v in payload.items() if k != "thinking"}
            fallback_effort = request.adaptive_retry_effort_fallback
            if fallback_effort:
                allow_effort = True
                if request.adaptive_retry_effort_only_minimax:
                    allow_effort = (
                        "minimax" in (profile.name or "").lower()
                        or "minimaxi" in (url or "").lower()
                    )
                if allow_effort:
                    retry_body.setdefault("reasoning_effort", str(fallback_effort))
            resp = post(
                url,
                json=retry_body,
                headers=headers,
                timeout=request.timeout,
                proxies=proxies,
            )
        latency = time.time() - t0
    except Exception as exc:
        return ModelResponse(
            error=f"请求失败: {_safe_err(exc)[:200]}",
            latency=time.time() - t0,
            exception=exc,
            attempts=actual_attempts,
            adaptive_fallback_used=adaptive_used,
            provider_name=profile.name,
            api_mode=profile.api_mode,
        )

    if resp is None:
        return ModelResponse(
            error="请求失败: empty response",
            latency=latency,
            attempts=actual_attempts,
            adaptive_fallback_used=adaptive_used,
            provider_name=profile.name,
            api_mode=profile.api_mode,
        )

    if resp.status_code != 200:
        body_text = getattr(resp, "text", "") or ""
        return ModelResponse(
            error=f"HTTP {resp.status_code}: {explain_http_error(resp.status_code, body_text)}",
            status_code=int(resp.status_code),
            raw_response=resp,
            latency=latency,
            attempts=actual_attempts,
            adaptive_fallback_used=adaptive_used,
            provider_name=profile.name,
            api_mode=profile.api_mode,
        )

    try:
        data = resp.json()
    except Exception as exc:
        return ModelResponse(
            error=f"响应非 JSON: {_safe_err(exc)[:200]}",
            status_code=int(resp.status_code),
            raw_response=resp,
            latency=latency,
            exception=exc,
            attempts=actual_attempts,
            adaptive_fallback_used=adaptive_used,
            provider_name=profile.name,
            api_mode=profile.api_mode,
        )

    parsed = transport.parse_response(data)
    text = str(parsed.get("text") or "")
    reasoning = str(parsed.get("reasoning") or "")
    if request.allow_reasoning_fallback_text and not text.strip() and reasoning.strip():
        text = reasoning

    return ModelResponse(
        text=text,
        reasoning=reasoning,
        usage=dict(parsed.get("usage") or {}),
        finish_reason=str(parsed.get("finish_reason") or ""),
        latency=latency,
        status_code=200,
        raw_response=resp,
        attempts=actual_attempts,
        adaptive_fallback_used=adaptive_used,
        provider_name=profile.name,
        api_mode=profile.api_mode,
    )


_INFERRED_HOSTS = ("opencode.ai", "minimaxi.com", "minimax", "siliconflow.cn", "siliconflow.com")


def _extract_llm_section(cfg: Any) -> dict[str, Any]:
    if cfg is None:
        return {}
    llm_obj = cfg.get("llm") if isinstance(cfg, Mapping) else getattr(cfg, "llm", None)
    if llm_obj is None:
        return {}
    if isinstance(llm_obj, Mapping):
        return dict(llm_obj)
    out: dict[str, Any] = {}
    for key in (
        "provider",
        "model",
        "api_key",
        "base_url",
        "proxy",
        "reasoning_effort",
        "thinking_mode",
    ):
        val = getattr(llm_obj, key, None)
        if val is not None:
            out[key] = val
    return out


def resolve_simple_llm_cfg(cfg: Any = None, *, env_path: str | None = None) -> dict[str, Any] | None:
    """Resolve unified topic/helper LLM configuration while preserving full ``cfg``."""
    if cfg is None:
        try:
            from ..config import resolve_config

            cfg = resolve_config()
        except Exception:
            cfg = None

    llm_sec = _extract_llm_section(cfg)
    provider = str(llm_sec.get("provider") or "").strip()
    endpoint = str(llm_sec.get("base_url") or llm_sec.get("endpoint") or "").strip()
    model = str(llm_sec.get("model") or "").strip()
    proxy = str(llm_sec.get("proxy") or "").strip()
    api_key = str(llm_sec.get("api_key") or llm_sec.get("apiKey") or "").strip()

    if not api_key:
        target_env = env_path or os.path.expanduser("~/AppData/Local/hermes/.env")
        if target_env and os.path.exists(target_env):
            try:
                with open(target_env, encoding="utf-8") as fh:
                    for line in fh:
                        stripped = line.strip()
                        if stripped.startswith("LLM_API_KEY="):
                            api_key = stripped.split("=", 1)[1].strip()
                            break
                        if stripped.startswith("MINIMAX_CN_API_KEY="):
                            api_key = stripped.split("=", 1)[1].strip()
                            break
            except Exception:
                pass
    if not api_key:
        api_key = (
            os.environ.get("LLM_API_KEY", "").strip()
            or os.environ.get("MINIMAX_CN_API_KEY", "").strip()
        )

    if not endpoint:
        endpoint = os.environ.get("LLM_BASE_URL", "").strip()
    if not model:
        model = os.environ.get("LLM_MODEL", "").strip()
    if not proxy:
        proxy = os.environ.get("V3CORE_LLM_PROXY", "").strip()

    if not api_key:
        return None

    return {
        "provider": provider,
        "endpoint": endpoint,
        "model": model,
        "api_key": api_key,
        "proxy": proxy,
        "reasoning_effort": llm_sec.get("reasoning_effort"),
        "thinking_mode": llm_sec.get("thinking_mode"),
        "config": cfg,
    }


def _has_declared_provider_binding(model: str, cfg: Any, provider_hint: str) -> bool:
    if cfg is not None:
        bound_provider, _ = providers._model_binding(model, cfg)
        if bound_provider:
            return True
    if not provider_hint and cfg is not None:
        provider_hint = str(_extract_llm_section(cfg).get("provider") or "").strip()
    if not provider_hint:
        return False
    from .. import providers_opencode as _oc

    if _oc.family_of(provider_hint) is not None:
        return True
    provs = providers._cfg_section(cfg, "providers")
    return provider_hint in provs


def invoke_simple(
    llm_cfg: Mapping[str, Any],
    messages: list[dict[str, Any]],
    *,
    system: str | None = None,
    max_tokens: int = 800,
    temperature: float | None = 0.3,
    timeout: float = 60.0,
    cfg: Any = None,
    proxies: Mapping[str, str] | None = None,
) -> ModelResponse:
    """Convenience entrypoint for lightweight callers (topic_cluster / topic_fusion / topic_maintain)."""
    model = str(llm_cfg.get("model") or "").strip()
    base_url = str(llm_cfg.get("base_url") or llm_cfg.get("endpoint") or "").strip()
    api_key = str(llm_cfg.get("apiKey") or llm_cfg.get("api_key") or "").strip()
    proxy = str(llm_cfg.get("proxy") or "").strip() or None
    provider_hint = str(llm_cfg.get("provider") or "").strip()
    effective_cfg = cfg if cfg is not None else llm_cfg.get("config")

    host = providers._host_of(base_url) if base_url else ""
    inferred_known = any(marker in host for marker in _INFERRED_HOSTS)
    if _has_declared_provider_binding(model, effective_cfg, provider_hint) or inferred_known:
        profile = providers.resolve_profile(
            model,
            effective_cfg,
            api_key=api_key,
            base_url=base_url,
            provider=provider_hint,
        )
    elif base_url:
        profile = providers.ProviderProfile(
            name="openai-compat(simple)",
            api_mode=providers.API_CHAT,
            base_url=base_url,
            max_tokens_field="max_tokens",
            supports=frozenset({"temperature"}),
        )
    else:
        profile = None

    req = ModelRequest(
        model=model,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        thinking_intent=str(llm_cfg.get("thinking_mode") or "auto"),
        effort_source="explicit",
        explicit_effort=llm_cfg.get("reasoning_effort"),
        force_none_when_disabled=False,
        api_key=api_key,
        base_url=base_url,
        proxy=proxy,
        proxies=proxies,
        timeout=timeout,
        retry_attempts=int(llm_cfg.get("retry_attempts", 2) or 2),
        cfg=effective_cfg,
        profile=profile,
    )
    return invoke(req)
