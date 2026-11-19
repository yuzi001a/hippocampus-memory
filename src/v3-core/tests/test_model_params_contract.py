"""模型参数形状表契约 (2026-11-19)。

背景: 模型名 → 参数形状 (收不收 ``thinking`` / ``reasoning_effort``) 过去写死在
``llm.py`` 与 ``observer.py`` 各一份, 用模型名子串判断。厂商改契约 (M3 → M3.1
要求 adaptive thinking) 就只能改代码, 而且漏改一处就是"某条路径静默用错参数"。

本文件钉住四件事:
1. 形状由 ``models:`` 表决定 (内置 + 用户覆盖, 最长前缀), 调用点不再自己判断;
2. 表里没有的模型 → 保守默认 + 显式 WARNING, 不静默回落字面量;
3. 两条真实调用路径 (``llm.LLMClient.chat`` / ``observer.call_m3``) 都按表拼 payload;
4. 端点默认值只有一个来源, 调用点不再各写 ``api.minimaxi.com``。

Hermetic: 无网络 (``requests.post`` 被替换), 无 PostgreSQL, 不读写真实 profile。
"""

from __future__ import annotations

import inspect
import json
import logging

import pytest
import requests

from v3core import llm
from v3core import model_params
from v3core import observer
from v3core.config_model import V3Config, from_legacy_dict, validate_config


class _FakeResponse:
    def __init__(self, status_code=200, text="", data=None):
        self.status_code = status_code
        self.text = text
        self._data = data or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _ok(content="ok"):
    return _FakeResponse(200, data={"choices": [{"message": {"content": content}}], "usage": {}})


# ── 1. 表解析 ───────────────────────────────────────────────────────────────

def test_builtin_table_covers_known_families():
    m31 = model_params.resolve("MiniMax-M3.1-Flash")
    assert m31.thinking == model_params.THINKING_ADAPTIVE
    assert m31.reasoning_effort == "low"
    assert m31.declared is True
    # 最长前缀: M3.1 不能被 M3 抢走
    assert m31.matched == "minimax-m3.1"

    m3 = model_params.resolve("MiniMax-M3")
    assert m3.thinking == model_params.THINKING_CLASSIC
    assert m3.reasoning_effort is None
    assert m3.matched == "minimax-m3"


def test_user_models_section_overrides_and_extends():
    cfg = {"models": {"MiniMax-M3": {"thinking": "adaptive", "reasoning_effort": "medium"},
                      "acme-local": {"thinking": "classic"}}}
    m3 = model_params.resolve("MiniMax-M3", cfg)
    assert (m3.thinking, m3.reasoning_effort) == (model_params.THINKING_ADAPTIVE, "medium")

    acme = model_params.resolve("acme-local-v2", cfg)
    assert acme.declared is True
    assert acme.thinking == model_params.THINKING_CLASSIC


def test_v3config_object_is_accepted_as_table_source():
    cfg = from_legacy_dict({"llm": {"model": "acme-1"},
                            "models": {"acme-1": {"thinking": "classic"}}})
    assert isinstance(cfg, V3Config)
    assert model_params.resolve("acme-1", cfg).declared is True


def test_unknown_model_falls_back_conservatively_and_warns_once(caplog):
    model_params._WARNED.clear()
    with caplog.at_level(logging.WARNING, logger="v3core.model_params"):
        first = model_params.resolve("totally-new-model")
        second = model_params.resolve("totally-new-model")

    assert first.declared is False and first.matched == ""
    # 未声明模型保持历史行为 (classic + 显式 thinking), 不发 effort:
    # 宁可 400 立刻可见, 也不要悄悄退回 adaptive 思考 (变慢/吃满预算)
    assert first.thinking == model_params.THINKING_CLASSIC
    assert first.reasoning_effort is None
    assert first == second
    warnings = [r for r in caplog.records if "未在 models:" in r.getMessage()]
    assert len(warnings) == 1, "未声明模型只应警告一次, 不刷日志"


# ── 2. thinking / effort 折叠 ───────────────────────────────────────────────

@pytest.mark.parametrize("mode,expected", [
    ("disabled", None),
    ("auto", None),
    ("enabled", {"type": "enabled"}),
])
def test_thinking_field_for_adaptive_family(mode, expected):
    params = model_params.resolve("MiniMax-M3.1-Flash")
    assert model_params.thinking_field(params, mode) == expected


@pytest.mark.parametrize("mode,expected", [
    ("disabled", {"type": "disabled"}),
    ("auto", {"type": "disabled"}),
    ("enabled", {"type": "enabled"}),
])
def test_thinking_field_for_classic_family(mode, expected):
    params = model_params.resolve("MiniMax-M3")
    assert model_params.thinking_field(params, mode) == expected


def test_reasoning_effort_precedence():
    params = model_params.resolve("MiniMax-M3.1-Flash")
    # 显式配置 > 表声明 > default
    assert model_params.reasoning_effort_for(params, {"reasoning_effort": "high"}, default="low") == "high"
    assert model_params.reasoning_effort_for(params, {}, default="low") == "low"
    classic = model_params.resolve("MiniMax-M3")
    assert model_params.reasoning_effort_for(classic, {}, default=None) is None


# ── 3. 两条真实调用路径 ─────────────────────────────────────────────────────

def test_llm_chat_sends_effort_and_no_thinking_for_adaptive(monkeypatch):
    seen = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: (seen.append(kw["json"]), _ok())[1])

    llm.LLMClient({"llm": {"provider": "minimax", "model": "MiniMax-M3.1-Flash",
                           "api_key": "k", "base_url": "https://example.test/v1"}}).chat(
        "sys", [{"role": "user", "content": "hi"}])

    body = seen[0]
    assert "thinking" not in body
    assert body["reasoning_effort"] == "low"


def test_llm_chat_keeps_thinking_for_classic_and_never_adds_effort(monkeypatch):
    seen = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: (seen.append(kw["json"]), _ok())[1])

    llm.LLMClient({"llm": {"provider": "minimax", "model": "MiniMax-M3",
                           "api_key": "k", "base_url": "https://example.test/v1"}}).chat(
        "sys", [{"role": "user", "content": "hi"}])

    body = seen[0]
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body


def test_llm_reasoning_effort_is_configurable(monkeypatch):
    seen = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: (seen.append(kw["json"]), _ok())[1])

    llm.LLMClient({"llm": {"provider": "minimax", "model": "MiniMax-M3.1-Flash",
                           "api_key": "k", "base_url": "https://example.test/v1",
                           "reasoning_effort": "medium"}}).chat(
        "sys", [{"role": "user", "content": "hi"}])

    assert seen[0]["reasoning_effort"] == "medium"


def test_observer_call_m3_uses_table_effort(monkeypatch):
    seen = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: (seen.append(kw["json"]), _ok())[1])

    result = observer.call_m3({"model": "MiniMax-M3.1-Flash",
                               "base_url": "https://example.test/v1"}, "k", "hello")

    assert result["text"] == "ok"
    assert "thinking" not in seen[0]
    assert seen[0]["reasoning_effort"] == "low"


def test_observer_call_m3_honours_user_models_override(monkeypatch):
    """只改 config 的 models: 段就能换行为 — 不需要改代码。"""
    seen = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: (seen.append(kw["json"]), _ok())[1])

    observer.call_m3(
        {"model": "acme-x", "base_url": "https://example.test/v1",
         "models": {"acme-x": {"thinking": "adaptive", "reasoning_effort": "high"}}},
        "k", "hello",
    )

    assert "thinking" not in seen[0]
    assert seen[0]["reasoning_effort"] == "high"


def test_observer_call_m3_without_model_fails_loudly(monkeypatch):
    called = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: called.append(url) or _ok())

    result = observer.call_m3({"model": "", "base_url": "https://example.test/v1"}, "k", "hello")

    assert "error" in result and "config_error" in result["error"]
    assert called == [], "没有模型名时不得发请求, 更不得回落到字面量模型"


# ── 4. 端点/模型的字面量不再散布在调用点 ─────────────────────────────────────

def test_no_endpoint_or_model_literals_in_call_sites():
    for mod in (llm, observer):
        src = inspect.getsource(mod)
        assert "api.minimaxi.com" not in src, f"{mod.__name__} 仍内联端点字面量"
        assert 'or "MiniMax-M3"' not in src, f"{mod.__name__} 仍回落到字面量模型名"


# ── 5. 配置校验暴露未声明模型 ────────────────────────────────────────────────

def test_validate_config_warns_for_undeclared_model():
    cfg = from_legacy_dict({"llm": {"model": "mystery-model"}})
    warns = validate_config(cfg)
    assert any("mystery-model" in w and "models:" in w for w in warns)


def test_validate_config_is_quiet_for_declared_model():
    cfg = from_legacy_dict({"llm": {"model": "MiniMax-M3.1-Flash"}})
    assert not any("MiniMax-M3.1-Flash" in w for w in validate_config(cfg))
