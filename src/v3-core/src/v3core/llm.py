"""LLM 客户端抽象 — MiniMax / OpenAI"""
from __future__ import annotations
import json
import logging
import os
import time
import requests
from typing import Any

from . import model_params


try:
    from . import _safe_err
except (ImportError, AttributeError):
    try:
        from .. import _safe_err
    except (ImportError, AttributeError):
        def _safe_err(e, max_len=80):
            try:
                from . import _safe_err as _impl
            except ImportError:
                from .. import _safe_err as _impl
            globals()["_safe_err"] = _impl
            return _impl(e, max_len)

logger = logging.getLogger("v3core.llm")

_MAX_RETRIES = 3
_RETRY_BACKOFF = [1, 4, 10]  # 秒: 1st→2nd→3rd

# ──────────────────────────────────────────────────────────────────────────────
# HTTP status classification
#   requests.Response.__bool__ returns False for ANY 4xx/5xx response —
#   so `if resp:` is a trap. The old line 202 used `_http_err.response if
#   _http_err.response else 0`, which silently masked a real 401/500 as
#   status=0 and broke the 5xx-retry branch. The helpers below are the
#   explicit `is not None` form plus a stable status→class mapping used in
#   the log line.
# ──────────────────────────────────────────────────────────────────────────────

_STATUS_CLASS_MAP: dict[int, str] = {
    401: "auth_failed",
    402: "quota_or_plan_limit",
    429: "rate_limited",
    500: "upstream_error",
    503: "upstream_unavailable",
}


def classify_status(status_code: int | None) -> str:
    """Map an HTTP status code to a stable human-readable class.

    Returns ``"http_{status}"`` for recognised codes, ``"unknown"`` for
    anything else, and ``"none"`` when the response was missing entirely
    (so the log line still makes sense in a ConnectionError-style case).
    """
    if status_code is None:
        return "none"
    if status_code in _STATUS_CLASS_MAP:
        return _STATUS_CLASS_MAP[status_code]
    return f"http_{status_code}"

def _looks_like_rejected_extra(err: Exception) -> bool:
    """True when a 4xx looks like the provider choking on an optional field.

    Deliberately narrow: only 400/422-class responses whose text mentions the
    thinking parameter, an unknown/extra field, or a missing field. Auth,
    quota and rate-limit errors must NOT trigger the retry.
    """
    text = f"{type(err).__name__}: {err}".lower()
    if not any(tok in text for tok in ("400", "422", "bad request", "unprocessable")):
        return False
    if any(tok in text for tok in ("api key", "unauthorized", "401", "quota", "balance", "rate limit", "429")):
        return False
    return any(tok in text for tok in ("thinking", "extra", "unknown field",
                                       "unexpected", "field required", "invalid_request"))


def _thinking_payload(model: str, mode: str, cfg: Any = None) -> dict[str, Any] | None:
    """Resolve the ``thinking`` field without making a provider request.

    模型形状 (哪些族要求 adaptive / 能不能发 disabled) 由 ``model_params`` 表决定;
    这里只负责 payload 形状。返回 ``None`` = 省略该字段。
    """
    params = model_params.resolve(model, cfg)
    field = model_params.thinking_field(params, mode)
    if field is not None and field.get("type") == "enabled":
        # 历史形状: 显式开启时带思考预算 (与 _chat_minimax 的旧行为一致)
        field = {"type": "enabled", "budget_tokens": 32768}
    return field


def _classify_openai_error(err: Exception) -> str:
    """Classify an OpenAI SDK error without including sensitive error text."""
    if isinstance(err, (TimeoutError, requests.exceptions.Timeout)) or "timeout" in type(err).__name__.lower():
        return "llm_timeout"
    response = getattr(err, "response", None)
    status = getattr(err, "status_code", None) or getattr(response, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError, OverflowError):
        status = None
    if status is not None and 400 <= status < 500:
        return "http_4xx"
    if status is not None and 500 <= status < 600:
        return "http_5xx"
    return "other"


# ──────────────────────────────────────────────────────────────────────────────
# Per-call output budget (additive; default path unchanged)
#
# ``chat(..., max_output_tokens=N)`` exists so a caller that KNOWS its answer is
# short (e.g. the E1 300-500 字 系统态势总览) cannot leave the model a six-figure
# token budget to overrun its output contract and keep generating into a second
# chat turn. ``None`` means "exactly the historical behaviour" — the provider
# default below is untouched for every existing caller.
#
# The provider field mapping is INTERNAL to this module:
#   minimax (OpenAI-compatible endpoint) -> "max_completion_tokens"
#   openai  (openai SDK)                 -> "max_tokens"
# Callers never see which field is used.
# ──────────────────────────────────────────────────────────────────────────────

_MIN_OUTPUT_TOKENS = 1
# 1M is already far beyond any model's output window; anything above it is a
# unit/typo error (e.g. passing a character count as a token count).
_MAX_OUTPUT_TOKENS = 1_000_000


def validate_output_tokens(value: int | None) -> int | None:
    """Validate an optional per-call output budget; return it or ``None``.

    ``None`` is legal and means "use the provider default". Anything else must
    be a real ``int`` in ``[_MIN_OUTPUT_TOKENS, _MAX_OUTPUT_TOKENS]``.
    ``bool`` is rejected explicitly: ``isinstance(True, int)`` is True in
    Python, and a stray flag must not silently become a 1-token budget.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"max_output_tokens 必须是 int 或 None, 收到 {type(value).__name__}: {value!r}"
        )
    if value < _MIN_OUTPUT_TOKENS:
        raise ValueError(
            f"max_output_tokens 必须 >= {_MIN_OUTPUT_TOKENS}, 收到 {value}"
            "（传 None 表示使用 provider 默认上限）"
        )
    if value > _MAX_OUTPUT_TOKENS:
        raise ValueError(
            f"max_output_tokens 超过合理上限 {_MAX_OUTPUT_TOKENS}, 收到 {value}"
            "（传 None 表示使用 provider 默认上限）"
        )
    return value


def _default_base_url() -> str:
    """配置层唯一的 MiniMax 端点默认值 — 调用点不再各写一份字面量。"""
    from .config import DEFAULT_LLM_BASE_URL
    return DEFAULT_LLM_BASE_URL


_GATEWAY_DEFAULT_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "ollama": "http://127.0.0.1:11434/v1",
}


def _gateway_default_base_url(prov_low: str) -> str:
    if prov_low == "minimax-cn":
        return _default_base_url()
    return _GATEWAY_DEFAULT_BASE_URLS.get(prov_low, "")

_PROVIDER_ENV_KEY_MAP: dict[str, tuple[str, ...]] = {
    "anthropic": ("V3_LLM_API_KEY", "ANTHROPIC_API_KEY"),
    "deepseek": ("V3_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
    "siliconflow": ("V3_LLM_API_KEY", "SILICONFLOW_API_KEY", "OPENAI_API_KEY"),
    "openrouter": ("V3_LLM_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"),
    "openai": ("V3_LLM_API_KEY", "OPENAI_API_KEY"),
    "openai-compat": ("V3_LLM_API_KEY", "OPENAI_API_KEY"),
}


def _resolve_client_api_key(llm_cfg: dict, provider: str) -> str:
    direct = str(llm_cfg.get("api_key") or llm_cfg.get("apiKey") or "").strip()
    if direct:
        return direct
    prov = (provider or "").strip().lower()
    env_candidates = _PROVIDER_ENV_KEY_MAP.get(
        prov,
        ("V3_LLM_API_KEY", "MINIMAX_CN_API_KEY", "MINIMAX_API_KEY", "OPENAI_API_KEY"),
    )
    for env_name in env_candidates:
        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    if prov == "ollama":
        return "ollama"
    return ""


class LLMClient:
    """统一 LLM 客户端"""

    def __init__(self, config):
        """接受 V3Config | dict | LLMConfig 任意形态, 提取 llm 子配置."""
        llm_cfg = self._extract_llm_cfg(config)
        # 原始配置留着查 models: 表 (模型参数形状), 不是 llm 子段的一部分
        self._raw_config = config
        self.provider = llm_cfg.get("provider", "") or ""
        self.model = llm_cfg.get("model", "")
        # thinking 开关: config llm.thinking (bool, 默认 True) 或构造参数
        self._thinking = bool(llm_cfg.get("thinking", True))
        self._thinking_mode = llm_cfg.get("thinking_mode", "auto") or "auto"
        # None = 用 models: 表声明的档位; 显式配置优先
        self._reasoning_effort_cfg = llm_cfg.get("reasoning_effort")
        try:
            raw_timeout = llm_cfg.get("timeout", 600)
            if isinstance(raw_timeout, bool):
                raise ValueError("boolean is not a timeout")
            self._timeout = float(raw_timeout)
            if self._timeout <= 0:
                self._timeout = 600
        except (TypeError, ValueError, OverflowError):
            self._timeout = 600
        # Output cap. 131072 assumed a 128k-output model; most OpenAI-compatible
        # providers reject it outright, so it is configurable per profile.
        try:
            self._max_tokens = int(llm_cfg.get("max_tokens") or 131072)
        except (TypeError, ValueError):
            self._max_tokens = 131072
        self.api_key = _resolve_client_api_key(llm_cfg, self.provider)
        # 自定义端点（支持 config.yaml 中 llm.base_url）— 空 = 按 provider 用官方默认
        self.base_url = llm_cfg.get("base_url", "") or ""
        # 提取代理: 优先 llm.proxy, 回退 storage.{embed,rerank}.proxy, 再回退 env
        self._proxy = (
            llm_cfg.get("proxy")
            or self._extract_proxy(config)
            or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
            or None
        )

    @staticmethod
    def _extract_llm_cfg(config) -> dict:
        """兼容 V3Config / dict / LLMConfig 三种来源."""
        # V3Config / LLMConfig dataclass
        if hasattr(config, "llm") and not isinstance(config, type) and hasattr(config.llm, "provider"):
            out = {
                "provider": config.llm.provider,
                "model": config.llm.model,
                "api_key": config.llm.api_key,
                "base_url": getattr(config.llm, "base_url", "") or "",
            }
            # Optional provider-shaped keys must survive, otherwise a profile that
            # states `thinking: false` / `max_tokens: 8192` silently inherits
            # defaults meant for another vendor (131072 got rejected with
            # "exceeded max_seq_len" on SiliconFlow).
            for opt in ("thinking", "thinking_mode", "max_tokens", "temperature", "timeout",
                        "reasoning_effort"):
                value = getattr(config.llm, opt, None)
                if value is not None:
                    out[opt] = value
            return out
        if hasattr(config, "provider") and hasattr(config, "model"):
            out = {
                "provider": config.provider,
                "model": config.model,
                "api_key": getattr(config, "api_key", ""),
                "base_url": getattr(config, "base_url", "") or "",
            }
            for opt in ("thinking", "thinking_mode", "max_tokens", "timeout",
                        "reasoning_effort"):
                value = getattr(config, opt, None)
                if value is not None:
                    out[opt] = value
            return out
        # raw dict — 保持原行为
        if isinstance(config, dict):
            return config.get("llm", {}) or {}
        return {}

    @staticmethod
    def _extract_proxy(config) -> str | None:
        """从 V3Config/dict 提取 proxy, 优先 llm, 回退 storage.embed / storage.rerank"""
        if isinstance(config, dict):
            # llm proxy
            llm_block = config.get("llm", {}) or {}
            proxy = llm_block.get("proxy")
            if proxy:
                return proxy
            # storage.* proxy
            storage_block = config.get("storage", {}) or {}
            for key in ("embed", "rerank"):
                sub = storage_block.get(key, {}) or {}
                proxy = sub.get("proxy")
                if proxy:
                    return proxy
        return None
        
    def chat(self, system: str, messages: list[dict], temperature: float = 0.7,
             max_output_tokens: int | None = None) -> str:
        """调 LLM chat

        ``max_output_tokens`` — OPTIONAL per-call output budget.
        ``None`` (default) = exactly the historical behaviour, i.e. the
        provider default set by this module/config; every existing caller is
        unaffected. The caller never names a provider field: this method maps
        the value to ``max_completion_tokens`` (minimax) or ``max_tokens``
        (openai). Validated here, before any network I/O.
        """
        # Validate the budget FIRST: a bad caller value must fail locally
        # (no request built, no credential touched) instead of being sent.
        _output_override = validate_output_tokens(max_output_tokens)
        try:
            from . import llmstatus as _llmstatus
        except ImportError:
            try:
                from .llmstatus import record_success as _rs, record_error as _re, classify_llm_error as _cl  # type: ignore
                import types as _t
                _llmstatus = _t.SimpleNamespace(record_success=_rs, record_error=_re, classify_llm_error=_cl)
            except ImportError:
                _llmstatus = None  # type: ignore
        if not self.api_key:
            if _llmstatus is not None:
                try:
                    _llmstatus.record_error("credential_missing")
                except Exception:
                    pass
            raise RuntimeError("LLM api_key 缺失 (credential_missing)")
        try:
            if self.provider == "minimax":
                result = self._chat_minimax(system, messages, temperature, _output_override)
            elif self.provider == "openai":
                result = self._chat_openai(system, messages, temperature, _output_override)
            elif self._profile_routed():
                result = self._chat_via_profile(system, messages, temperature, _output_override)
            else:
                raise ValueError(f"不支持的 LLM provider: {self.provider}")
            if _llmstatus is not None:
                try:
                    _llmstatus.record_success()
                except Exception:
                    pass
            return result
        except Exception as e:
            if _llmstatus is not None:
                try:
                    _llmstatus.record_error(_llmstatus.classify_llm_error(exc=e))
                except Exception:
                    pass
            raise
    
    def _profile_routed(self) -> bool:
        """Route via llm_gateway for OpenCode, declared providers:, standard gateway vendors, or custom base_url."""
        if self._opencode_family():
            return True
        prov = str(self.provider or "").strip()
        prov_low = prov.lower()
        if prov_low in _GATEWAY_DEFAULT_BASE_URLS or prov_low in ("minimax-cn", "openai-compat"):
            return True
        if self.base_url and prov_low not in ("minimax", "openai"):
            return True
        raw_cfg = self._raw_config
        if not prov or raw_cfg is None:
            return False
        from collections.abc import Mapping as _Mapping
        provs = raw_cfg.get("providers") if isinstance(raw_cfg, _Mapping) else getattr(raw_cfg, "providers", None)
        return isinstance(provs, _Mapping) and prov in provs

    def _opencode_family(self) -> bool:
        """这个 provider 是不是 OpenCode Zen/Go 家族（含自定义同族名字）。"""
        try:
            from . import providers_opencode
        except Exception:
            return False
        raw = str(self.provider or "").strip().lower()
        if raw in ("opencode", "opencode-go", "opencode-zen"):
            return True
        if raw.startswith("opencode"):
            return True
        return providers_opencode.family_of(raw, self.base_url) is not None

    def _resolve_gateway_profile(self, cfg: Any, model: str) -> Any:
        from collections.abc import Mapping as _Mapping
        from . import providers

        prov = str(self.provider or "").strip()
        prov_low = prov.lower()
        raw_provs = (
            cfg.get("providers") if isinstance(cfg, _Mapping) else getattr(cfg, "providers", None)
        ) if cfg is not None else None
        is_declared = isinstance(raw_provs, _Mapping) and prov in raw_provs
        eff_base_url = (self.base_url or _gateway_default_base_url(prov_low)).strip()

        if is_declared or self._opencode_family() or prov_low == "minimax-cn":
            return providers.resolve_profile(
                model,
                cfg,
                api_key=self.api_key,
                base_url=eff_base_url,
                provider=prov,
            )
        if prov_low == "anthropic":
            return providers.ProviderProfile(
                name="anthropic",
                api_mode=providers.API_ANTHROPIC,
                base_url=eff_base_url or _gateway_default_base_url("anthropic"),
                max_tokens_field="max_tokens",
                api_key_header="x-api-key",
                api_key_prefix="",
                anthropic_version="2023-06-01",
                supports=frozenset({"temperature"}),
            )
        if eff_base_url:
            return providers.ProviderProfile(
                name=f"{prov_low or 'custom'}(openai-compat)",
                api_mode=providers.API_CHAT,
                base_url=eff_base_url,
                max_tokens_field="max_tokens",
                supports=frozenset({"temperature", "response_format"}),
            )
        return None

    def _chat_via_profile(self, system: str, messages: list[dict], temperature: float,
                          max_output_tokens: int | None = None) -> str:
        """按通道画像发请求：统一委托给 ``v3core.llm_gateway.invoke``。"""
        from .llm_gateway import ModelRequest, explain_http_error, invoke as _gw_invoke

        cfg = self._raw_config
        model = str(self.model or "")
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        budget = self._max_tokens if max_output_tokens is None else max_output_tokens
        effort = (
            self._resolved_reasoning_effort()
            if hasattr(self, "_resolved_reasoning_effort")
            else self._reasoning_effort_cfg
        )
        thinking_intent = (
            "disabled"
            if (not getattr(self, "_thinking", True) and self._thinking_mode == "auto")
            else self._thinking_mode
        )
        profile = self._resolve_gateway_profile(cfg, model)
        eff_base_url = profile.base_url if profile is not None else self.base_url
        res = _gw_invoke(
            ModelRequest(
                model=model,
                messages=messages,
                system=system,
                max_tokens=budget,
                temperature=temperature,
                thinking_intent=thinking_intent,
                effort_source="explicit",
                explicit_effort=effort,
                force_none_when_disabled=True,
                api_key=self.api_key,
                base_url=eff_base_url,
                proxy=getattr(self, "_proxy", None) or None,
                timeout=self._timeout,
                retry_attempts=_MAX_RETRIES,
                retry_backoff_schedule=tuple(_RETRY_BACKOFF),
                allow_reasoning_fallback_text=True,
                cfg=cfg,
                profile=profile,
            ),
            post_fn=requests.post,
            sleep_fn=time.sleep,
        )
        if not res.ok:
            if res.status_code is not None:
                body_text = getattr(res.raw_response, "text", "") or ""
                prefix = "OpenCode 调用失败" if self._opencode_family() else f"LLM({self.provider}) 调用失败"
                raise RuntimeError(
                    f"{prefix} HTTP {res.status_code}: {explain_http_error(res.status_code, body_text)}"
                )
            raise RuntimeError(res.error or f"LLM({self.provider}) 调用失败")
        return res.text

    def _chat_minimax(self, system: str, messages: list[dict], temperature: float,
                      max_output_tokens: int | None = None) -> str:
        from . import providers
        from .llm_gateway import ModelRequest, invoke as _gw_invoke

        base_url = self.base_url.rstrip("/") if self.base_url else _default_base_url().rstrip("/")
        _output_budget = 131072 if max_output_tokens is None else max_output_tokens
        _raw_cfg = getattr(self, "_raw_config", None)
        proxies = {"https": self._proxy} if self._proxy else None
        logger.info(
            "LLM call: provider=minimax model=%s temperature=%.2f max_completion_tokens=%d%s",
            self.model, temperature, _output_budget,
            "" if max_output_tokens is None else " (per-call override)",
        )
        profile = providers.resolve_profile(self.model, _raw_cfg, api_key=self.api_key, base_url=base_url)
        _retry_effort = str(getattr(self, "_reasoning_effort_cfg", "") or "low")
        res = _gw_invoke(
            ModelRequest(
                model=self.model,
                messages=messages,
                system=system,
                max_tokens=_output_budget,
                temperature=temperature,
                thinking_intent=self._thinking_mode,
                effort_source="explicit",
                explicit_effort=getattr(self, "_reasoning_effort_cfg", None),
                enabled_budget_tokens=32768,
                force_none_when_disabled=False,
                reasoning_split=True,
                api_key=self.api_key,
                base_url=base_url,
                proxies=proxies,
                timeout=600,
                retry_attempts=_MAX_RETRIES,
                retry_backoff_schedule=tuple(_RETRY_BACKOFF),
                allow_reasoning_fallback_text=True,
                adaptive_retry_effort_fallback=_retry_effort,
                adaptive_retry_effort_only_minimax=True,
                cfg=_raw_cfg,
                profile=profile,
                require_model=False,
            ),
            post_fn=requests.post,
            sleep_fn=time.sleep,
        )
        if res.ok:
            logger.info(
                "LLM success: tokens_in=%s tokens_out=%s latency=%.1fs",
                res.usage.get("prompt_tokens", "?"),
                res.usage.get("completion_tokens", "?"),
                res.latency,
            )
            return res.text
        if res.status_code == 429:
            # 429 耗尽重试后与历史契约一致：抛 RuntimeError("LLM failed after ...")
            # （需额外补齐最后一次等待，保持历史行为）。
            sleeper_wait = _RETRY_BACKOFF[-1] if _RETRY_BACKOFF else 10
            time.sleep(sleeper_wait)
            raise RuntimeError(f"LLM failed after {_MAX_RETRIES} attempts: HTTP 429")
        if res.raw_response is not None and res.status_code is not None and res.status_code >= 400:
            status_class = classify_status(res.status_code)
            body_text = (getattr(res.raw_response, "text", "") or "")[:300]
            logger.error("LLM HTTP failed: status=%d class=%s body=%s", res.status_code, status_class, body_text[:2000])
            res.raw_response.raise_for_status()
        if res.exception is not None:
            raise res.exception
        raise RuntimeError(f"LLM failed after {_MAX_RETRIES} attempts: {res.error}")
    
    def _chat_openai(self, system: str, messages: list[dict], temperature: float,
                     max_output_tokens: int | None = None) -> str:
        import openai
        client_kwargs = {
            "api_key": self.api_key,
            # The SDK's default timeout/retries can block for many minutes; use
            # the configured deadline and let this method own the bounded retry loop.
            "timeout": self._timeout,
            "max_retries": 0,
        }
        if self.base_url:
            # 用户在 config.yaml llm.base_url 配置自定义端点（兼容硅基流动/自建）
            client_kwargs["base_url"] = self.base_url
        client = openai.OpenAI(**client_kwargs)
        # Per-call override wins; otherwise self._max_tokens (llm.max_tokens,
        # default 131072) — the field this path has always sent.
        _output_budget = self._max_tokens if max_output_tokens is None else max_output_tokens
        prompt_len = sum(len(m.get("content", "") or "") for m in messages)
        logger.info("LLM call: provider=openai model=%s prompt_len=%d temperature=%.2f "
                    "max_tokens=%d%s",
                    self.model, prompt_len, temperature, _output_budget,
                    "" if max_output_tokens is None else " (per-call override)")
        # 思考链: 默认开启 (deepseek reasoning 提升长输入覆盖完整性, 8/6 实测
        # 131 条输入关闭思考 2/2 只中 1 次惩罚线 → 开启后 2/2 全中)。
        # 通过 LLMClient(thinking=True/False) 或 config llm.thinking 控制。
        extra = {}
        if getattr(self, "_thinking", True):
            extra = {"thinking": {"type": "enabled", "budget_tokens": 32768}}
        else:
            extra = {"thinking": {"type": "disabled"}}
        call_kwargs = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": temperature,
            "max_tokens": _output_budget,
        }
        if extra:
            call_kwargs["extra_body"] = extra

        call_started = time.monotonic()
        thinking_fallback_used = False
        current_kwargs = call_kwargs
        last_error = None
        for attempt in range(1, _MAX_RETRIES + 1):
            attempt_started = time.monotonic()
            try:
                resp = client.chat.completions.create(**current_kwargs)
                content = resp.choices[0].message.content or ""
                if not content.strip():
                    # deepseek 思考模式把输出放 reasoning_content — 兜底提取
                    rc = getattr(resp.choices[0].message, "reasoning_content", None) or ""
                    if rc.strip():
                        content = rc
                latency = time.monotonic() - attempt_started
                logger.info(
                    "LLM attempt: provider=openai model=%s attempt=%d/%d elapsed=%.1fs result=success",
                    self.model, attempt, _MAX_RETRIES, latency,
                )
                logger.info("LLM success: provider=openai tokens_in=%s tokens_out=%s latency=%.1fs",
                            resp.usage.prompt_tokens if resp.usage else "?",
                            resp.usage.completion_tokens if resp.usage else "?",
                            latency)
                return content
            except Exception as err:
                last_error = err
                latency = time.monotonic() - attempt_started
                classification = _classify_openai_error(err)
                logger.warning(
                    "LLM attempt: provider=openai model=%s attempt=%d/%d elapsed=%.1fs result=%s",
                    self.model, attempt, _MAX_RETRIES, latency, classification,
                )

                if (extra and not thinking_fallback_used
                        and _looks_like_rejected_extra(err)):
                    thinking_fallback_used = True
                    current_kwargs = dict(call_kwargs)
                    current_kwargs.pop("extra_body", None)
                    logger.warning(
                        "LLM provider rejected optional thinking; retrying once without it (model=%s)",
                        self.model,
                    )
                    if attempt < _MAX_RETRIES:
                        continue

                retryable = classification in ("llm_timeout", "http_5xx") or "connection" in type(err).__name__.lower()
                if retryable and attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
                    logger.warning(
                        "LLM retry scheduled: provider=openai attempt=%d/%d result=%s wait=%ds",
                        attempt, _MAX_RETRIES, classification, wait,
                    )
                    time.sleep(wait)
                    continue

                total_elapsed = time.monotonic() - call_started
                logger.error(
                    "LLM failed: provider=openai model=%s attempt=%d/%d elapsed=%.1fs result=%s",
                    self.model, attempt, _MAX_RETRIES, total_elapsed, classification,
                )
                raise RuntimeError(
                    f"LLM provider=openai model={self.model} failed: classification={classification} "
                    f"elapsed={total_elapsed:.1f}s attempts={attempt}"
                ) from err

        # The loop always returns or raises. Retain a defensive bounded failure.
        total_elapsed = time.monotonic() - call_started
        classification = _classify_openai_error(last_error) if last_error else "other"
        raise RuntimeError(
            f"LLM provider=openai model={self.model} failed: classification={classification} "
            f"elapsed={total_elapsed:.1f}s attempts={_MAX_RETRIES}"
        ) from last_error
