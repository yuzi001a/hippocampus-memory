# -*- coding: utf-8 -*-
"""llm.py 的 OpenCode 家族路径 — E1/抽取/摘要 不再报「不支持的 provider」。

背景：切到 opencode-go 之后 E1 立刻失败（不支持的 LLM provider: opencode-go），
因为 llm.py 只认 minimax/openai 两条硬编码分支。这里锁住修复：
调用点不判断模型名，URL/头/字段形状全部由通道画像决定。
"""
from __future__ import annotations

import json

import pytest

from v3core import llm


CFG = {
    "llm": {
        "provider": "opencode-go",
        "model": "deepseek-v4.1-flash",
        "api_key": "sk-test",
        "base_url": "",
        "timeout": 10,
    },
    "providers": {
        "opencode-go": {"api_mode": "responses", "base_url": "https://opencode.ai/zen/go/v1"},
    },
    "models": {"deepseek-v4.1-flash": {"provider": "opencode-go"}},
}


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _patch(monkeypatch, captured, payload):
    def fake_post(url, json=None, headers=None, timeout=None, proxies=None):
        captured.append({"url": url, "body": json, "headers": headers})
        return _Resp(payload)

    monkeypatch.setattr("requests.post", fake_post)


def test_opencode_provider_is_supported(monkeypatch):
    captured: list = []
    payload = {"status": "completed",
               "output": [{"type": "message", "content": [{"type": "output_text", "text": "摘要结果"}]}],
               "usage": {"input_tokens": 3, "output_tokens": 2}}
    _patch(monkeypatch, captured, payload)
    client = llm.LLMClient(CFG)
    out = client.chat("你是整理者", [{"role": "user", "content": "把这段整理一下"}], temperature=0.2)
    assert out == "摘要结果"
    sent = captured[0]
    assert sent["url"].endswith("/responses")
    assert sent["headers"]["x-opencode-session"]
    assert sent["headers"]["User-Agent"].startswith("hippocampus-memory/")
    # 多轮历史折进 input，system 走 instructions
    assert "把这段整理一下" in sent["body"]["input"]


def test_opencode_chat_mode_keeps_messages(monkeypatch):
    captured: list = []
    payload = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    _patch(monkeypatch, captured, payload)
    cfg = json.loads(json.dumps(CFG))
    cfg["providers"]["opencode-go"]["api_mode"] = "chat_completions"
    client = llm.LLMClient(cfg)
    assert client.chat("sys", [{"role": "user", "content": "hi"}]) == "ok"
    body = captured[0]["body"]
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["content"] == "hi"


def test_other_provider_still_rejected(monkeypatch):
    cfg = json.loads(json.dumps(CFG))
    cfg["llm"]["provider"] = "some-unknown-provider"
    client = llm.LLMClient(cfg)
    with pytest.raises(ValueError, match="不支持的 LLM provider"):
        client.chat("s", [{"role": "user", "content": "x"}])


def test_opencode_anthropic_mode_sends_top_level_system_and_strips_prefix(monkeypatch):
    captured: list = []
    payload = {"content": [{"type": "text", "text": "anthropic-ok"}],
               "stop_reason": "end_turn",
               "usage": {"input_tokens": 4, "output_tokens": 2}}
    _patch(monkeypatch, captured, payload)
    cfg = {
        "llm": {
            "provider": "opencode-go",
            "model": "opencode-go/claude-sonnet-5-5",
            "api_key": "sk-test",
            "base_url": "https://opencode.ai/zen/go/v1",
            "thinking": False,
            "timeout": 10,
        },
        "providers": {"opencode-go": {}},
        "models": {"claude-sonnet-5-5": {"provider": "opencode-go"}},
    }
    client = llm.LLMClient(cfg)
    out = client.chat("sys-prompt", [{"role": "user", "content": "hello"}], temperature=0.1)
    assert out == "anthropic-ok"
    sent = captured[0]
    assert sent["url"] == "https://opencode.ai/zen/go/messages"
    assert sent["headers"]["x-api-key"] == "sk-test"
    assert sent["headers"]["x-opencode-session"]
    assert sent["body"]["model"] == "claude-sonnet-5-5"
    assert sent["body"]["system"] == "sys-prompt"
    assert sent["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert "reasoning_effort" not in sent["body"]


@pytest.mark.parametrize("api_mode,expected_field", [
    ("responses", ("reasoning", {"effort": "none"})),
    ("chat_completions", ("reasoning_effort", "none")),
])
def test_opencode_thinking_off_explicitly_sends_none(monkeypatch, api_mode, expected_field):
    captured: list = []
    payload = (
        {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}], "usage": {}}
        if api_mode == "responses"
        else {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
    )
    _patch(monkeypatch, captured, payload)
    cfg = json.loads(json.dumps(CFG))
    cfg["llm"]["thinking"] = False
    cfg["providers"]["opencode-go"]["api_mode"] = api_mode
    client = llm.LLMClient(cfg)
    assert client.chat("sys", [{"role": "user", "content": "hi"}]) == "ok"
    key, val = expected_field
    assert captured[0]["body"][key] == val
