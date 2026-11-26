# -*- coding: utf-8 -*-
"""OpenCode Go / Zen 适配契约。

覆盖这条通道真会踩的坑（都是线上遇到过的）：
1. 缺 x-opencode-session -> 400 MissingSessionID
2. api_mode 与模型不匹配（gpt/Claude/qwen/deepseek 走三条不同的线）
3. base_url 与 api_mode 不成对（anthropic 线要去掉 /v1）
4. 模型 id 带 family 前缀
5. 自定义 family 名字（opencode-go-bridge）要享受同样待遇
6. 自建代理端点不许被改写
"""
from __future__ import annotations

import pytest

from v3core import providers, providers_opencode as oc


# ── 1. family 判定 ───────────────────────────────────────────────────────
@pytest.mark.parametrize("name,expected", [
    ("opencode-go", "opencode-go"),
    ("opencode-zen", "opencode-zen"),
    ("opencode-go-bridge", "opencode-go"),      # 自定义名字挂同一 family
    ("OpenCode-Go", "opencode-go"),             # 大小写
    ("minimax-cn", None),
])
def test_family_of(name, expected):
    assert oc.family_of(name) == expected


def test_family_from_base_url_when_name_is_neutral():
    assert oc.family_of("myrelay", "https://opencode.ai/zen/go/v1") == "opencode-go"
    assert oc.family_of("myrelay", "https://opencode.ai/zen/v1") == "opencode-zen"
    assert oc.family_of("myrelay", "https://api.deepseek.com/v1") is None


# ── 2. 按模型选传输 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("model,mode", [
    ("gpt-6.1-terra", "responses"),
    ("grok-4", "responses"),
    ("muse-spark-1.3", "responses"),
    ("claude-sonnet-5-5", "anthropic_messages"),
    ("minimax-m3.1", "anthropic_messages"),
    ("qwen3.8-flash", "anthropic_messages"),
    ("deepseek-v4.1-flash", "chat_completions"),
])
def test_model_api_mode_go(model, mode):
    assert oc.model_api_mode("opencode-go", model) == mode


def test_zen_routes_qwen_to_anthropic():
    assert oc.model_api_mode("opencode-zen", "qwen3.8-flash") == "anthropic_messages"


# ── 3. 端点与传输成对 ────────────────────────────────────────────────────
def test_base_url_keeps_v1_for_chat():
    assert oc.normalize_base_url("opencode-go", "chat_completions",
                                 "https://opencode.ai/zen/go") == "https://opencode.ai/zen/go/v1"


def test_base_url_strips_v1_for_anthropic():
    assert oc.normalize_base_url("opencode-go", "anthropic_messages",
                                 "https://opencode.ai/zen/go/v1") == "https://opencode.ai/zen/go"


def test_base_url_heals_family_path():
    # 从 Zen 切到 Go 时，历史 base_url 不能继续用
    assert oc.normalize_base_url("opencode-go", "chat_completions",
                                 "https://opencode.ai/zen/v1") == "https://opencode.ai/zen/go/v1"


def test_custom_proxy_url_untouched():
    url = "https://my-proxy.test/zen/go/v1"
    assert oc.normalize_base_url("opencode-go", "anthropic_messages", url) == url


# ── 4. 模型 id 归一 ──────────────────────────────────────────────────────
def test_model_id_prefix_stripped():
    assert oc.normalize_model_id("opencode-go", "opencode-go/deepseek-v4.1-flash") == "deepseek-v4.1-flash"
    assert oc.normalize_model_id("opencode-go", "deepseek-v4.1-flash") == "deepseek-v4.1-flash"


# ── 5. 画像补全（配置可以只写 family） ──────────────────────────────────
CFG_MIN = {
    "providers": {"opencode-go": {"api_mode": "responses", "base_url": "https://opencode.ai/zen/go/v1"}},
    "models": {"deepseek-v4.1-flash": {"provider": "opencode-go"}},
}


def test_minimal_config_gets_session_header_and_ua():
    profile = providers.resolve_profile("deepseek-v4.1-flash", CFG_MIN, api_key="k")
    headers = providers.build_headers(profile, "k", session_id="sid-1")
    assert headers["x-opencode-session"] == "sid-1"
    assert headers["User-Agent"].startswith("hippocampus-memory/")


def test_defaults_fill_missing_api_mode_from_model():
    cfg = {
        "providers": {"opencode-go": {}},                    # 什么都没写
        "models": {"claude-sonnet-5-5": {"provider": "opencode-go"}},
    }
    profile = providers.resolve_profile("claude-sonnet-5-5", cfg, api_key="k")
    assert profile.api_mode == "anthropic_messages"
    assert profile.base_url == "https://opencode.ai/zen/go"   # anthropic 线不带 /v1
    assert profile.session_header == "x-opencode-session"


def test_defaults_respect_explicit_override():
    cfg = {
        "providers": {"opencode-go": {"api_mode": "chat_completions",
                                      "base_url": "https://opencode.ai/zen/go/v1",
                                      "session_header": "x-custom-session"}},
        "models": {"claude-sonnet-5-5": {"provider": "opencode-go"}},
    }
    profile = providers.resolve_profile("claude-sonnet-5-5", cfg, api_key="k")
    assert profile.api_mode == "chat_completions"
    assert profile.session_header == "x-custom-session"


# ── 6. 错误翻译 ──────────────────────────────────────────────────────────
def test_missing_session_error_is_explained():
    msg = oc.explain_error(400, '{"error":{"type":"MissingSessionID","message":"Request is missing x-opencode-session"}}')
    assert "x-opencode-session" in msg and "会话" in msg


def test_unknown_error_falls_back_to_body():
    assert "boom" in oc.explain_error(500, "boom")
