# -*- coding: utf-8 -*-
"""OpenCode Go / Zen 适配层 —— 这条通道有自己的规矩，集中在这里。

为什么不塞进 ``providers.py`` 的通用形状
----------------------------------------
OpenCode 的 Zen 与 Go 是两套中转，同一个 provider 底下混着三种传输：

* ``gpt-*`` / ``grok-*`` / ``muse-spark``  走 ``/responses``
* ``claude-*`` / ``minimax-*`` / ``qwen*`` / ``union-alpha`` 走 Anthropic 线 ``/messages``
* 其余（如 ``deepseek-*``）走 ``/chat/completions``

而且：

1. **会话头是硬要求**：缺 ``x-opencode-session`` 直接 400 MissingSessionID（线上遇过）。
   值必须「每个会话稳定」，官方文档写明；我们的会话 id 见 ``agent_identity``。
2. **base_url 与 api_mode 必须成对**：anthropic 线要把 ``/v1`` 去掉，chat/responses 要加回来，
   两个中转的路径段也不同（``/zen`` vs ``/zen/go``）。带错路径报的是 401「Model 不支持」，
   看起来像没权限，实际是路径错。
3. **模型 id 可能带 family 前缀**（``opencode-go/deepseek-v4.1-flash``），发请求前要剥掉。

这一层只做「按模型把通道画像补全」，不碰提示词、不碰数据。

配置上的效果：``providers.<name>`` 里只写 ``family: opencode-go``（或让名字含 ``opencode-go``）
就能用，``api_mode`` / ``base_url`` / ``session_header`` 都自动补；显式写了就以显式为准。
"""
from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

# family -> 官方中转路径段
FAMILY_PATHS = {"opencode-zen": "/zen", "opencode-go": "/zen/go"}

# 每个 family 的「模型前缀 -> 传输」，按顺序匹配
API_MODE_PREFIXES: dict[str, tuple[tuple[tuple[str, ...], str], ...]] = {
    "opencode-go": (
        (("gpt-", "grok-", "muse-spark"), "responses"),
        (("claude-", "minimax-", "qwen", "union-alpha"), "anthropic_messages"),
    ),
    "opencode-zen": (
        (("claude-", "union-alpha", "qwen"), "anthropic_messages"),
        (("gpt-", "grok-", "muse-spark"), "responses"),
    ),
}

SESSION_HEADER = "x-opencode-session"

# 各传输默认支持的能力（显式 supports: 会覆盖）
DEFAULT_SUPPORTS: dict[str, frozenset[str]] = {
    "responses": frozenset({"json_schema", "reasoning_effort", "temperature"}),
    "chat_completions": frozenset({"response_format", "reasoning_effort", "temperature"}),
    "anthropic_messages": frozenset({"temperature", "reasoning_effort"}),
}


def family_of(provider_name: str | None, base_url: str | None = None) -> str | None:
    """provider 名或端点属于哪个 family；都不是就 None。

    允许自定义名字（``opencode-go-bridge``）挂在同一 family 下 —— 与 Hermes 的判定保持一致。
    """
    raw = str(provider_name or "").strip().lower()
    for family in FAMILY_PATHS:
        if raw == family or raw.startswith(family):
            return family
    url = str(base_url or "").strip().lower()
    if "opencode.ai" in url:
        path = urlparse(url).path or ""
        if "/zen/go" in path:
            return "opencode-go"
        if "/zen" in path:
            return "opencode-zen"
    return None


def normalize_model_id(provider_name: str | None, model: str | None, base_url: str | None = None) -> str:
    """``opencode-go/deepseek-v4.1-flash`` -> ``deepseek-v4.1-flash``。"""
    family = family_of(provider_name, base_url)
    current = str(model or "").strip()
    if not current or family is None:
        return current
    lowered = current.lower()
    for prefix in (f"{str(provider_name or family).strip().lower()}/", f"{family}/"):
        if lowered.startswith(prefix):
            return current[len(prefix):]
    return current


def model_api_mode(provider_name: str | None, model: str | None, base_url: str | None = None) -> str:
    """按模型前缀决定传输；认不出来就 chat/completions（与官方表一致）。"""
    family = family_of(provider_name, base_url)
    normalized = normalize_model_id(provider_name, model, base_url).lower()
    if family and normalized:
        for prefixes, mode in API_MODE_PREFIXES.get(family, ()):
            if normalized.startswith(prefixes):
                return mode
    return "chat_completions"


def normalize_base_url(provider_name: str | None, api_mode: str, base_url: str | None) -> str:
    """把端点修成与传输匹配的形态。

    * 官方 opencode.ai 主机：路径段按 family 纠正（``/zen`` vs ``/zen/go``），
      ``/v1`` 按传输增删（anthropic 线不带，其余带）。
    * 自建代理/自定义端点：一个字符都不动。
    """
    url = str(base_url or "").strip().rstrip("/")
    family = family_of(provider_name, url)
    if not url or family is None:
        return url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    official = host == "opencode.ai" or host.endswith(".opencode.ai")
    if not official:
        return url
    path = (parsed.path or "").rstrip("/")
    if re.fullmatch(r"/zen(/go)?(/v1)?", path):
        path = FAMILY_PATHS[family] + ("/v1" if path.endswith("/v1") else "")
    if api_mode == "anthropic_messages":
        path = re.sub(r"/v1$", "", path)
    elif not path.endswith("/v1"):
        path += "/v1"
    return urlunparse(parsed._replace(path=path))


def apply_defaults(provider_name: str, spec: Mapping[str, Any], model: str | None) -> dict[str, Any]:
    """给 OpenCode family 的 provider 段补默认值；显式配置永远优先。"""
    family = family_of(provider_name, spec.get("base_url"))
    if family is None:
        return dict(spec)
    out = dict(spec)
    api_mode = str(out.get("api_mode") or "").strip().lower() or model_api_mode(provider_name, model, out.get("base_url"))
    out["api_mode"] = api_mode
    base_url = str(out.get("base_url") or "").strip() or f"https://opencode.ai{FAMILY_PATHS[family]}/v1"
    out["base_url"] = normalize_base_url(provider_name, api_mode, base_url)
    out.setdefault("session_header", SESSION_HEADER)
    if not isinstance(out.get("supports"), Mapping):
        out["supports"] = {name: True for name in DEFAULT_SUPPORTS.get(api_mode, frozenset())}
    return out


def explain_error(status: int, body: str) -> str:
    """把这条通道的典型错误翻译成人话；认不出就原样返回摘要。"""
    text = str(body or "")
    lower = text.lower()
    if "missingsessionid" in lower or "x-opencode-session" in lower:
        return ("通道要求 x-opencode-session 请求头（每个会话一个稳定值），本次请求没带上。"
                "检查 providers 段的 session_header 与 agent_identity 的会话 id 解析。")
    if status == 401 and ("not supported" in lower or "model" in lower):
        return "端点路径与模型不匹配：检查 base_url 是否与 api_mode 成对（anthropic 线不带 /v1）。"
    if status == 403:
        return "免费档模型只允许 OpenCode 官方客户端，其他 harness 会被拒。"
    return text[:200]
