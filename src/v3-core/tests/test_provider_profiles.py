# -*- coding: utf-8 -*-
"""通道画像契约测试 — 换模型/换通道只改配置, 不改调用点。

覆盖:
1. 三种传输的 payload / header 形状
2. 响应解析统一折成 text/reasoning/usage
3. 模型 → 通道绑定解析 (含缺失绑定、未声明端点)
4. observer.call_m3 真的按画像发请求 (URL / 头 / 字段名 / schema)
"""
from __future__ import annotations

import json

import pytest

from v3core import model_params, providers

# ── 配置夹具 ────────────────────────────────────────────────────────────────
CFG = {
    "providers": {
        "opencode-go": {
            "api_mode": "responses",
            "base_url": "https://opencode.ai/zen/go/v1",
            "session_header": "x-opencode-session",
            "supports": {"reasoning_effort": True, "temperature": True},
        },
        "opencode-go-chat": {
            "api_mode": "chat_completions",
            "base_url": "https://opencode.ai/zen/go/v1",
            "session_header": "x-opencode-session",
            "max_tokens_field": "max_tokens",
            "supports": {"reasoning_effort": True, "response_format": True, "temperature": True},
        },
        "minimax-cn": {
            "api_mode": "chat_completions",
            "base_url": "https://api.minimaxi.com/v1",
            "max_tokens_field": "max_completion_tokens",
            "supports": {"thinking": True, "reasoning_split": True, "temperature": True},
        },
        "qwen-anthropic": {
            "api_mode": "anthropic_messages",
            "base_url": "https://opencode.ai/inference/go/anthropic/v1",
            "max_tokens_field": "max_tokens",
        },
    },
    "models": {
        "deepseek-v4.1-flash": {"provider": "opencode-go", "thinking": "none", "reasoning_effort": "low"},
        "minimax-m3.1": {"provider": "minimax-cn", "thinking": "adaptive", "reasoning_effort": "low"},
        "qwen3.8-flash": {"provider": "qwen-anthropic"},
    },
}


def test_model_binding_reads_provider_key():
    p = model_params.resolve("deepseek-v4.1-flash", CFG)
    assert p.provider == "opencode-go"
    assert p.declared is True


def test_resolve_profile_uses_bound_provider():
    prof = providers.resolve_profile("deepseek-v4.1-flash", CFG)
    assert prof.name == "opencode-go"
    assert prof.api_mode == providers.API_RESPONSES
    assert prof.url() == "https://opencode.ai/zen/go/v1/responses"


def test_resolve_profile_missing_provider_block_is_loud():
    cfg = {"models": {"ghost-model": {"provider": "nope"}}}
    with pytest.raises(RuntimeError, match="providers"):
        providers.resolve_profile("ghost-model", cfg)


def test_legacy_inference_keeps_opencode_shape():
    prof = providers.resolve_profile("unknown-model", {}, base_url="https://opencode.ai/zen/go/v1")
    assert prof.session_header == "x-opencode-session"
    assert prof.max_tokens_field == "max_tokens"
    assert not prof.supports_cap("reasoning_split")


def test_legacy_inference_keeps_minimax_shape():
    prof = providers.resolve_profile("unknown-model", {}, base_url="https://api.minimaxi.com/v1")
    assert prof.max_tokens_field == "max_completion_tokens"
    assert prof.supports_cap("thinking") and prof.supports_cap("reasoning_split")


def test_unknown_endpoint_warns_but_does_not_break_runtime():
    prof = providers.resolve_profile("x", {}, base_url="https://example.test/v1")
    assert prof.api_mode == providers.API_CHAT


# ── payload 形状 ────────────────────────────────────────────────────────────
SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}


def test_chat_payload_respects_supported_caps():
    prof = providers.build_profile("chatty", CFG["providers"]["opencode-go-chat"])
    body = providers.build_payload(prof, "m", "hi", max_tokens=99, temperature=0.2,
                                   schema=SCHEMA, reasoning_effort="low", thinking={"type": "disabled"},
                                   reasoning_split=True)
    assert body["max_tokens"] == 99
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["reasoning_effort"] == "low"
    assert "thinking" not in body          # 画像没声明 → 不发
    assert "reasoning_split" not in body


def test_minimax_payload_keeps_legacy_fields():
    prof = providers.build_profile("minimax-cn", CFG["providers"]["minimax-cn"])
    body = providers.build_payload(prof, "MiniMax-M3.1-Flash", "hi", max_tokens=10,
                                   thinking={"type": "disabled"}, reasoning_split=True)
    assert body["max_completion_tokens"] == 10
    assert body["thinking"] == {"type": "disabled"}
    assert body["reasoning_split"] is True
    assert "response_format" not in body   # 不支持 strict schema → 不发


def test_responses_payload_uses_text_format():
    prof = providers.build_profile("opencode-go", CFG["providers"]["opencode-go"])
    body = providers.build_payload(prof, "deepseek-v4.1-flash", "hi", max_tokens=1234,
                                   schema=SCHEMA, reasoning_effort="low")
    assert body["input"] == "hi"
    assert body["max_output_tokens"] == 1234
    assert body["text"]["format"]["schema"] == SCHEMA
    assert body["reasoning"] == {"effort": "low"}
    assert "messages" not in body


def test_anthropic_payload_and_headers():
    prof = providers.build_profile("qwen-anthropic", CFG["providers"]["qwen-anthropic"])
    body = providers.build_payload(prof, "qwen3.8-flash", "hi", max_tokens=64)
    headers = providers.build_headers(prof, "sk-test")
    assert body["max_tokens"] == 64 and body["messages"][0]["role"] == "user"
    assert headers["x-api-key"] == "sk-test"
    assert "Authorization" not in headers
    assert headers["anthropic-version"] == "2023-06-01"


def test_session_header_only_when_id_present():
    prof = providers.build_profile("opencode-go", CFG["providers"]["opencode-go"])
    assert "x-opencode-session" not in providers.build_headers(prof, "k")
    assert providers.build_headers(prof, "k", session_id="sid-1")["x-opencode-session"] == "sid-1"


# ── 响应解析 ────────────────────────────────────────────────────────────────
def test_parse_chat_response():
    prof = providers.build_profile("c", CFG["providers"]["opencode-go-chat"])
    out = providers.parse_response(prof, {"choices": [{"message": {"content": "X", "reasoning_content": "R"},
                                                       "finish_reason": "stop"}],
                                          "usage": {"prompt_tokens": 5, "completion_tokens": 7}})
    assert (out["text"], out["reasoning"], out["finish_reason"]) == ("X", "R", "stop")
    assert providers.usage_tokens(out["usage"]) == (5, 7)


def test_parse_responses_response():
    prof = providers.build_profile("r", CFG["providers"]["opencode-go"])
    out = providers.parse_response(prof, {"status": "completed",
                                          "output": [{"type": "reasoning", "summary": [{"text": "think"}]},
                                                     {"type": "message", "content": [{"type": "output_text", "text": "BODY"}]}],
                                          "usage": {"input_tokens": 11, "output_tokens": 3}})
    assert out["text"] == "BODY"
    assert out["reasoning"] == "think"
    assert providers.usage_tokens(out["usage"]) == (11, 3)


def test_parse_anthropic_response():
    prof = providers.build_profile("a", CFG["providers"]["qwen-anthropic"])
    out = providers.parse_response(prof, {"content": [{"type": "text", "text": "HI"}],
                                          "stop_reason": "end_turn",
                                          "usage": {"input_tokens": 2, "output_tokens": 1}})
    assert out["text"] == "HI" and out["finish_reason"] == "end_turn"


# ── observer.call_m3 端到端 (假 HTTP) ───────────────────────────────────────
class _FakeResp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _patch_post(monkeypatch, captured, payload):
    """call_m3 内部 ``import requests``, 所以必须打在真正的 requests 模块上。"""
    from v3core import observer

    def fake_post(url, json=None, headers=None, timeout=None, proxies=None):
        captured.append({"url": url, "body": json, "headers": headers})
        return _FakeResp(payload)

    monkeypatch.setattr("requests.post", fake_post)
    return observer


def test_call_m3_sends_responses_request_with_schema(monkeypatch):
    captured: list = []
    payload = {"status": "completed",
               "output": [{"type": "message", "content": [{"type": "output_text",
                                                           "text": json.dumps({"note": {"content": "印"}, "topic_updates": []})}]}],
               "usage": {"input_tokens": 9, "output_tokens": 4}}
    observer = _patch_post(monkeypatch, captured, payload)
    cfg = {"model": "deepseek-v4.1-flash", "base_url": "https://opencode.ai/zen/go/v1",
           "providers": CFG["providers"], "models": CFG["models"], "timeout": 5}
    out = observer.call_m3(cfg, "sk-x", "提示词", max_completion_tokens=2048)
    sent = captured[0]
    assert sent["url"].endswith("/responses")
    assert sent["body"]["max_output_tokens"] == 2048
    assert sent["body"]["text"]["format"]["schema"]["properties"]["note"]
    # 会话 id 必须存在且是安装级稳定值（历史实现带进程号，重启就变 —— 已改）
    sid = sent["headers"]["x-opencode-session"]
    assert sid and not sid.startswith("v3core-")
    assert sent["headers"]["User-Agent"].startswith("hippocampus-memory/")
    assert json.loads(out["text"])["note"]["content"] == "印"


def test_call_m3_minimax_path_unchanged(monkeypatch):
    captured: list = []
    payload = {"choices": [{"message": {"content": json.dumps({"note": {"content": "x"}, "topic_updates": []}),
                                        "reasoning_content": ""}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    observer = _patch_post(monkeypatch, captured, payload)
    cfg = {"model": "MiniMax-M3.1-Flash", "base_url": "https://api.minimaxi.com/v1", "timeout": 5}
    observer.call_m3(cfg, "sk-y", "提示词", max_completion_tokens=1024)
    sent = captured[0]
    assert sent["url"].endswith("/chat/completions")
    assert "max_completion_tokens" in sent["body"]
    assert "response_format" not in sent["body"]       # 不支持 strict → 不发
    assert "x-opencode-session" not in sent["headers"]
