# -*- coding: utf-8 -*-
"""通道画像 — 把「模型」和「怎么调它」拆开。

问题
----
模型能力差异被写死在调用点：MiniMax 要 ``reasoning_split`` + ``max_completion_tokens``，
OpenCode Go 要求 ``x-opencode-session`` 头否则 400，DeepSeek 只认 ``max_tokens``。
每换一次模型就改一次热路径代码——这是本模块要消掉的东西。

现在的规则
----------
1. ``config.yaml`` 里有两段：

   ``providers:``  通道画像（端点 / 鉴权 / 头 / 字段形状 / 支持哪些能力）
   ``models:``     模型 → 通道绑定 + 思考形状（原来只描述思考，现在多一个 ``provider``）

2. 调用点只说「用哪个模型」，其余（URL、头、payload 字段名、响应解析）全部问本模块。

3. 没配 ``providers:`` 时按**历史行为**推断（MiniMax 兼容形状 / OpenCode 形状），
   保证老配置不改也能跑；推断不出就 fail-loud，不静默塞默认端点。

三种传输
--------
``chat_completions``   OpenAI 兼容 ``/chat/completions``（MiniMax / DeepSeek / 千问都支持）
``responses``          OpenAI Responses 风格 ``/responses``（DeepSeek 官方与 OpenCode Go 都支持，
                       且是这两家唯一真正强制 ``json_schema`` 的路子）
``anthropic_messages`` Anthropic 风格 ``/messages``（OpenCode Go 上的千问走这条）

响应解析统一折成 ``{text, reasoning, usage, finish_reason}``，调用点不再关心是哪家。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

logger = logging.getLogger("v3core.providers")

API_CHAT = "chat_completions"
API_RESPONSES = "responses"
API_ANTHROPIC = "anthropic_messages"
VALID_API_MODES = (API_CHAT, API_RESPONSES, API_ANTHROPIC)

#: 画像没声明某项支持时的保守默认（不发不认识的字段，比发出去吃 400 好）。
_DEFAULT_SUPPORTS = ("temperature",)


@dataclass(frozen=True)
class ProviderProfile:
    """一个通道的调用画像。"""

    name: str
    api_mode: str
    base_url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    max_tokens_field: str = "max_tokens"
    supports: frozenset[str] = frozenset(_DEFAULT_SUPPORTS)
    session_header: str | None = None
    user_agent: str | None = None
    client_headers: dict[str, str] = field(default_factory=dict)
    api_key_header: str = "Authorization"
    api_key_prefix: str = "Bearer "
    anthropic_version: str | None = None

    def supports_cap(self, cap: str) -> bool:
        return cap in self.supports

    def url(self) -> str:
        base = (self.base_url or "").rstrip("/")
        if self.api_mode == API_RESPONSES:
            return base + "/responses"
        if self.api_mode == API_ANTHROPIC:
            return base + "/messages"
        return base + "/chat/completions"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _cfg_section(cfg: Any, key: str) -> Mapping[str, Any]:
    if cfg is None:
        return {}
    if isinstance(cfg, Mapping):
        return _as_mapping(cfg.get(key))
    return _as_mapping(getattr(cfg, key, None))


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except (TypeError, ValueError):
        return ""


# ── 历史形状推断（没配 providers: 时用） ────────────────────────────────────
def _legacy_profile(base_url: str) -> ProviderProfile:
    """按端点推断历史形状 —— 只覆盖我们真实用过的两类，其余 fail-loud。"""
    host = _host_of(base_url)
    if "opencode.ai" in host:
        # OpenCode Zen/Go: 必须带会话头, 只认 max_tokens, 不发 thinking/reasoning_split
        return ProviderProfile(
            name="opencode(legacy-inferred)",
            api_mode=API_CHAT,
            base_url=base_url,
            max_tokens_field="max_tokens",
            supports=frozenset({"temperature", "reasoning_effort", "response_format"}),
            session_header="x-opencode-session",
        )
    if "minimaxi.com" in host or "minimax" in host:
        # MiniMax 兼容端点: max_completion_tokens + thinking + reasoning_split
        return ProviderProfile(
            name="minimax(legacy-inferred)",
            api_mode=API_CHAT,
            base_url=base_url,
            max_tokens_field="max_completion_tokens",
            # reasoning_effort 也在: M3.1 族不下发 thinking, 改发 effort 压思考
            supports=frozenset({"temperature", "thinking", "reasoning_split", "reasoning_effort"}),
        )
    # 未声明的端点: 保持历史形状 + 一次 WARNING。
    # 选警告而不是抛错, 是因为老配置在升级后就崩会直接打断生产; 而形状不对时
    # 服务端 400 一样看得见。新配置请显式写 providers: 段。
    _warn_unknown_endpoint(base_url)
    return ProviderProfile(
        name="unknown(legacy-shaped)",
        api_mode=API_CHAT,
        base_url=base_url,
        max_tokens_field="max_completion_tokens",
        supports=frozenset({"temperature", "thinking", "reasoning_split", "reasoning_effort"}),
    )


_WARNED_ENDPOINTS: set[str] = set()
_WARN_LOCK = threading.Lock()


def _warn_unknown_endpoint(base_url: str) -> None:
    host = _host_of(base_url) or base_url
    with _WARN_LOCK:
        if host in _WARNED_ENDPOINTS:
            return
        _WARNED_ENDPOINTS.add(host)
    logger.warning(
        "providers: 端点 %r 未在 providers: 段声明, 按历史 MiniMax 兼容形状调用 "
        "(max_completion_tokens + thinking + reasoning_split); 若该通道不是 OpenAI 兼容, "
        "请显式配置 providers: 段。", host,
    )


def build_profile(name: str, spec: Mapping[str, Any], *, api_key: str = "") -> ProviderProfile:
    """把 ``providers.<name>`` 的一段配置折成画像。字段写错直接报错，不猜。"""
    api_mode = str(spec.get("api_mode") or API_CHAT).strip().lower()
    if api_mode not in VALID_API_MODES:
        raise ValueError(
            f"providers.{name}.api_mode={api_mode!r} 不支持, 只认 {list(VALID_API_MODES)}"
        )
    base_url = str(spec.get("base_url") or "").strip()
    if not base_url:
        raise ValueError(f"providers.{name}.base_url 缺失")
    supports = spec.get("supports")
    if supports is None:
        supports_fs = frozenset(_DEFAULT_SUPPORTS)
    elif isinstance(supports, Mapping):
        supports_fs = frozenset(str(k).strip().lower() for k, v in supports.items() if v)
    else:
        supports_fs = frozenset(str(x).strip().lower() for x in supports)
    headers = {str(k): str(v) for k, v in _as_mapping(spec.get("headers")).items()}
    default_field = "max_output_tokens" if api_mode == API_RESPONSES else "max_tokens"
    return ProviderProfile(
        name=name,
        api_mode=api_mode,
        base_url=base_url,
        headers=headers,
        max_tokens_field=str(spec.get("max_tokens_field") or default_field),
        supports=supports_fs,
        session_header=(str(spec["session_header"]).strip() if spec.get("session_header") else None),
        user_agent=(str(spec["user_agent"]).strip() if spec.get("user_agent") else None),
        client_headers={str(k): str(v) for k, v in _as_mapping(spec.get("client_headers")).items()},
        api_key_header=str(spec.get("api_key_header") or ("x-api-key" if api_mode == API_ANTHROPIC else "Authorization")),
        api_key_prefix="" if api_mode == API_ANTHROPIC else str(spec.get("api_key_prefix") or "Bearer "),
        anthropic_version=(str(spec.get("anthropic_version") or "2023-06-01") if api_mode == API_ANTHROPIC else None),
    )


def _model_binding(model: str, cfg: Any) -> tuple[str, dict[str, Any]]:
    """模型 → (provider 名, models: 表里那条 spec)。复用 model_params 的最长前缀匹配。"""
    from . import model_params  # 延迟导入避免循环

    key = str(model or "").strip().lower()
    table = model_params.model_table(cfg)
    best = ""
    for candidate in model_params._candidates(key):
        for prefix in table:
            if prefix and candidate.startswith(prefix) and len(prefix) > len(best):
                best = prefix
        if best:
            break
    if not best:
        return "", {}
    return str(table[best].get("provider") or "").strip(), dict(table[best])


def resolve_profile(model: str, cfg: Any, *, api_key: str = "", base_url: str = "") -> ProviderProfile:
    """按模型绑定解析通道画像；未绑定则回落到历史推断。"""
    providers = _cfg_section(cfg, "providers")
    provider_name, _spec = _model_binding(model, cfg)
    if provider_name:
        raw = _as_mapping(providers.get(provider_name))
        if not raw:
            # OpenCode family 的画像可以全自动（端点/传输/会话头都由适配层补），
            # 所以空表是合法的；其他通道空表仍然是配置错误。
            from . import providers_opencode as _oc

            if _oc.family_of(provider_name) is None:
                raise RuntimeError(
                    f"模型 {model!r} 绑定到 provider {provider_name!r}, 但 providers: 段里没有这一项"
                )
        # OpenCode Zen/Go 有自己的规矩（会话头、按模型的传输、端点与传输成对）：
        # 缺省值在这里补齐，显式配置优先。
        try:
            from . import providers_opencode

            raw = providers_opencode.apply_defaults(provider_name, raw, model)
        except Exception as _exc:  # 补默认值失败不该拖垮调用
            logger.warning("providers: opencode 适配层补默认值失败 (%s)", _exc)
        profile = build_profile(provider_name, raw, api_key=api_key)
        # 端点允许调用点覆盖（同一通道挂不同入口时用），其余形状仍以画像为准
        if base_url:
            normalized_override = base_url
            try:
                from . import providers_opencode

                normalized_override = providers_opencode.normalize_base_url(
                    provider_name, profile.api_mode, base_url
                )
            except Exception:
                pass
            profile = ProviderProfile(**{**profile.__dict__, "base_url": normalized_override})
        return profile
    if base_url:
        return _legacy_profile(base_url)
    raise RuntimeError(
        f"模型 {model!r} 未在 models: 里绑定 provider, 且没有 base_url 可推断 —— "
        "请在 config.yaml 同时配置 providers: 与 models:"
    )


# ── 瞬态失败判定与退避 ──────────────────────────────────────────────────────
#: 值得重试的 HTTP 状态: 限流、上游抖动、网关超时。4xx 里除 408/425/429 外都不重试
#: —— 那是配置/鉴权/结构问题, 重试只会把错误拖长。
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: 值得重试的异常类型名 (requests 家族)。长请求跑到一半被掐断就是这个样子:
#: "HTTPSConnectionPool(host=..., port=443): Max retries exceeded"。
RETRYABLE_EXC_NAMES = frozenset({
    "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
    "ChunkedEncodingError", "ProtocolError", "SSLError", "IncompleteRead",
})


def is_retryable_status(code: int | None) -> bool:
    return int(code or 0) in RETRYABLE_STATUS


def is_retryable_exception(exc: BaseException) -> bool:
    """按异常链上的类型名判断 —— 不依赖调用方提前 import requests。"""
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ in RETRYABLE_EXC_NAMES:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def retry_delay(attempt: int, base: float = 2.0, response: Any = None, cap: float = 30.0) -> float:
    """第 attempt 次(0 起)重试前睡多久。服务端给了 Retry-After 就听它的。"""
    if response is not None:
        try:
            raw = getattr(response, "headers", {}).get("Retry-After")
            if raw:
                secs = float(str(raw).strip())
                return max(0.0, min(secs, cap))
        except (TypeError, ValueError):
            pass
    try:
        delay = float(base) * (2 ** max(0, attempt))
    except (TypeError, ValueError):
        delay = 2.0
    return max(0.0, min(delay, cap))


# ── 请求构造 ────────────────────────────────────────────────────────────────
def build_headers(
    profile: ProviderProfile,
    api_key: str,
    *,
    session_id: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    # 身份：通用库名（python-requests）会被中转当成匿名客户端，官方要求自报身份
    try:
        from .agent_identity import default_user_agent
        _ua_default = default_user_agent()
    except Exception:
        _ua_default = ""
    headers["User-Agent"] = profile.user_agent or _ua_default or "v3core"
    headers.update({str(k): str(v) for k, v in profile.client_headers.items()})
    if profile.api_key_header and api_key:
        headers[profile.api_key_header] = f"{profile.api_key_prefix}{api_key}"
    headers.update({str(k): str(v) for k, v in profile.headers.items()})
    if profile.session_header and session_id:
        headers[profile.session_header] = session_id
    if profile.anthropic_version:
        headers.setdefault("anthropic-version", profile.anthropic_version)
    if extra:
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


def _messages_from_prompt(prompt: str, system: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    out.append({"role": "user", "content": prompt})
    return out


def build_payload(
    profile: ProviderProfile,
    model: str,
    prompt: str,
    *,
    system: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    max_tokens: int,
    temperature: float | None = 0.3,
    schema: Mapping[str, Any] | None = None,
    schema_name: str = "output",
    reasoning_effort: str | None = None,
    thinking: Mapping[str, Any] | None = None,
    reasoning_split: bool = False,
) -> dict[str, Any]:
    """按画像拼请求体；统一委托给 ``v3core.llm_gateway.transports``。"""
    from .llm_gateway.transports import get_transport

    return get_transport(profile.api_mode).build_payload(
        profile,
        model,
        prompt=prompt,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        schema=schema,
        schema_name=schema_name,
        reasoning_effort=reasoning_effort,
        thinking=thinking,
        reasoning_split=reasoning_split,
    )


# ── 响应解析 ────────────────────────────────────────────────────────────────
def parse_response(profile: ProviderProfile, data: Mapping[str, Any]) -> dict[str, Any]:
    """把三家响应折成 ``{text, reasoning, usage, finish_reason}``。"""
    from .llm_gateway.transports import get_transport

    return get_transport(profile.api_mode).parse_response(data)


def supports_schema(profile: ProviderProfile) -> bool:
    """这条通道能不能真正强制 json_schema（responses 恒为真，chat 看画像声明）。"""
    if profile.api_mode == API_RESPONSES:
        return True
    return profile.supports_cap("response_format")


def usage_tokens(usage: Mapping[str, Any]) -> tuple[int, int]:
    """统一取 (input, output)；三家字段名不同。"""
    def _get(*keys: str) -> int:
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0
    return (_get("input_tokens", "prompt_tokens"), _get("output_tokens", "completion_tokens"))


__all__ = [
    "API_ANTHROPIC",
    "RETRYABLE_STATUS",
    "is_retryable_exception",
    "is_retryable_status",
    "retry_delay",
    "API_CHAT",
    "API_RESPONSES",
    "VALID_API_MODES",
    "ProviderProfile",
    "build_headers",
    "build_payload",
    "build_profile",
    "parse_response",
    "resolve_profile",
    "supports_schema",
    "usage_tokens",
]
