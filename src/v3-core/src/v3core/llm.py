"""LLM 客户端抽象 — MiniMax / OpenAI"""
from __future__ import annotations
import json
import logging
import os
import time
import requests
from typing import Any


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


class LLMClient:
    """统一 LLM 客户端"""

    def __init__(self, config):
        """接受 V3Config | dict | LLMConfig 任意形态, 提取 llm 子配置."""
        llm_cfg = self._extract_llm_cfg(config)
        self.provider = llm_cfg.get("provider", "") or ""
        self.model = llm_cfg.get("model", "")
        # thinking 开关: config llm.thinking (bool, 默认 True) 或构造参数
        self._thinking = bool(llm_cfg.get("thinking", True))
        # Output cap. 131072 assumed a 128k-output model; most OpenAI-compatible
        # providers reject it outright, so it is configurable per profile.
        try:
            self._max_tokens = int(llm_cfg.get("max_tokens") or 131072)
        except (TypeError, ValueError):
            self._max_tokens = 131072
        self.api_key = (
            llm_cfg.get("api_key", "")
            or os.environ.get("MINIMAX_CN_API_KEY", "")
            or os.environ.get("MINIMAX_API_KEY", "")
        )
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
            for opt in ("thinking", "max_tokens", "temperature", "timeout"):
                value = getattr(config.llm, opt, None)
                if value is not None:
                    out[opt] = value
            return out
        if hasattr(config, "provider") and hasattr(config, "model"):
            return {
                "provider": config.provider,
                "model": config.model,
                "api_key": getattr(config, "api_key", ""),
                "base_url": getattr(config, "base_url", "") or "",
            }
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
    
    def _chat_minimax(self, system: str, messages: list[dict], temperature: float,
                      max_output_tokens: int | None = None) -> str:
        # OpenAI 兼容端点 — 优先用 self.base_url（来自 config.yaml llm.base_url），
        # 空则回退到官方默认 https://platform.minimaxi.com/docs/api-reference/text-openai-api
        url = (
            f"{self.base_url.rstrip('/')}/chat/completions"
            if self.base_url
            else "https://api.minimaxi.com/v1/chat/completions"
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Per-call override wins; otherwise the historical literal 131072 is
        # kept verbatim. Deliberately NOT self._max_tokens — that would change
        # what every existing caller sends (llm.max_tokens is honoured on the
        # openai path only, see _chat_openai).
        _output_budget = 131072 if max_output_tokens is None else max_output_tokens
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": temperature,
            "max_completion_tokens": _output_budget,  # M3 铁律: max_tokens 已弃用被忽略, 必须用 max_completion_tokens
            "thinking": {"type": "disabled"},  # 稳定 M3 输出
        }
        proxies = {"https": self._proxy} if self._proxy else None
        prompt_len = len(json.dumps(body, ensure_ascii=False))
        logger.info("LLM call: provider=minimax model=%s prompt_len=%d temperature=%.2f "
                    "max_completion_tokens=%d%s",
                    self.model, prompt_len, temperature, _output_budget,
                    "" if max_output_tokens is None else " (per-call override)")

        last_err = None
        for attempt in range(_MAX_RETRIES):
            try:
                t0 = time.time()
                resp = requests.post(url, json=body, headers=headers, timeout=600, proxies=proxies)
                latency = time.time() - t0

                if resp.status_code == 429:
                    wait = _RETRY_BACKOFF[attempt] if attempt < len(_RETRY_BACKOFF) else 10
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = int(retry_after)
                        except ValueError:
                            pass
                    logger.warning("LLM 429 (attempt %d/%d): waiting %ds", attempt + 1, _MAX_RETRIES, wait)
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if not content.strip():
                    # deepseek 思考模式把输出放 reasoning_content — 兜底提取
                    rc = data.get("choices", [{}])[0].get("message", {}).get("reasoning_content", "") or ""
                    if rc.strip():
                        content = rc
                logger.info("LLM success: tokens_in=%s tokens_out=%s latency=%.1fs",
                            data.get("usage", {}).get("prompt_tokens", "?"),
                            data.get("usage", {}).get("completion_tokens", "?"),
                            latency)
                return content

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as _net_err:
                last_err = _net_err
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF[attempt]
                    logger.warning("LLM net error (attempt %d/%d): %s — retry in %ds",
                                   attempt + 1, _MAX_RETRIES, str(_net_err)[:100], wait)
                    time.sleep(wait)

            except requests.exceptions.HTTPError as _http_err:
                last_err = _http_err
                # BUGFIX (truthiness trap): requests.Response.__bool__ returns
                # False for any 4xx/5xx, so `if _http_err.response:` masks
                # a real 401/500 as status=0 and silently breaks the 5xx
                # retry branch. Use an explicit `is not None` test instead.
                _resp = _http_err.response
                if _resp is not None:
                    status = _resp.status_code
                    body_text = (_resp.text or "")[:300]
                else:
                    status = 0
                    body_text = ""
                status_class = classify_status(status if status else None)
                # 5xx 重试, 4xx 不重试（429 已在上面处理）
                if 500 <= status < 600 and attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF[attempt]
                    logger.warning("LLM 5xx (attempt %d/%d): status=%d class=%s — retry in %ds",
                                   attempt + 1, _MAX_RETRIES, status, status_class, wait)
                    time.sleep(wait)
                else:
                    logger.error("LLM HTTP failed: status=%d class=%s body=%s",
                                 status, status_class, body_text[:2000])
                    raise

        logger.error("LLM failed after %d attempts: %s", _MAX_RETRIES, str(last_err)[:200])
        raise RuntimeError(f"LLM failed after {_MAX_RETRIES} attempts: {last_err}") from last_err
    
    def _chat_openai(self, system: str, messages: list[dict], temperature: float,
                     max_output_tokens: int | None = None) -> str:
        import openai
        client_kwargs = {"api_key": self.api_key}
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
        t0 = time.time()
        try:
            # 思考链: 默认开启 (deepseek reasoning 提升长输入覆盖完整性, 8/6 实测
            # 131 条输入关闭思考 2/2 只中 1 次惩罚线 → 开启后 2/2 全中)。
            # 通过 LLMClient(thinking=True/False) 或 config llm.thinking 控制。
            extra = {}
            try:
                if getattr(self, "_thinking", True):
                    extra = {"thinking": {"type": "enabled", "budget_tokens": 32768}}
                else:
                    extra = {"thinking": {"type": "disabled"}}
            except Exception:
                pass
            call_kwargs = {
                "model": self.model,
                "messages": [{"role": "system", "content": system}] + messages,
                "temperature": temperature,
                "max_tokens": _output_budget,
            }
            if extra:
                call_kwargs["extra_body"] = extra
            try:
                resp = client.chat.completions.create(**call_kwargs)
            except Exception as first_err:
                # A provider that does not understand the optional thinking
                # parameter answers 4xx here. Retry exactly once WITHOUT it
                # instead of failing the memory call for the whole install.
                if not extra or not _looks_like_rejected_extra(first_err):
                    raise
                logger.warning(
                    "LLM provider rejected the optional thinking parameter; retrying without it "
                    "(model=%s): %s", self.model, _safe_err(first_err)[:160],
                )
                retry_kwargs = dict(call_kwargs)
                retry_kwargs.pop("extra_body", None)
                resp = client.chat.completions.create(**retry_kwargs)
            latency = time.time() - t0
            content = resp.choices[0].message.content or ""
            if not content.strip():
                # deepseek 思考模式把输出放 reasoning_content — 兜底提取
                rc = getattr(resp.choices[0].message, "reasoning_content", None) or ""
                if rc.strip():
                    content = rc
            logger.info("LLM success: provider=openai tokens_in=%s tokens_out=%s latency=%.1fs",
                        resp.usage.prompt_tokens if resp.usage else "?",
                        resp.usage.completion_tokens if resp.usage else "?",
                        latency)
            return content
        except Exception as e:
            latency = time.time() - t0
            logger.error("LLM failed: provider=openai model=%s latency=%.1fs error=%s",
                         self.model, latency, _safe_err(e)[:200])
            raise
