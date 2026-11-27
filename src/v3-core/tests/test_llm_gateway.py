# -*- coding: utf-8 -*-
"""Unified llm_gateway contract tests: transports, policy mutex, retries, and caller routing."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v3core import llm_gateway, providers, topic_cluster, topic_fusion, topic_maintain
from v3core.llm_gateway import (
    AnthropicMessagesTransport,
    ModelRequest,
    OpenAIChatTransport,
    OpenAIResponsesTransport,
    get_transport,
    invoke,
    invoke_simple,
    resolve_simple_llm_cfg,
    resolve_thinking_policy,
)


class _FakeResp:
    def __init__(self, status_code: int, payload=None, text: str = "", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or (json.dumps(self._payload) if payload is not None else "")
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


def test_transport_registry_and_payload_shapes():
    chat_prof = providers.build_profile(
        "c", {"api_mode": "chat_completions", "base_url": "https://x.test/v1", "supports": {"temperature": True, "response_format": True}}
    )
    t_chat = get_transport(chat_prof.api_mode)
    assert isinstance(t_chat, OpenAIChatTransport)
    assert t_chat.endpoint_url(chat_prof) == "https://x.test/v1/chat/completions"
    b_chat = t_chat.build_payload(chat_prof, "m1", prompt="hi", system="sys", max_tokens=50)
    assert b_chat["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

    resp_prof = providers.build_profile(
        "r", {"api_mode": "responses", "base_url": "https://x.test/v1", "supports": {"reasoning_effort": True}}
    )
    t_resp = get_transport(resp_prof.api_mode)
    assert isinstance(t_resp, OpenAIResponsesTransport)
    b_resp = t_resp.build_payload(
        resp_prof, "m2", messages=[{"role": "user", "content": "q"}], system="sys", max_tokens=60, reasoning_effort="none"
    )
    assert b_resp["input"] == "user: q"
    assert b_resp["instructions"] == "sys"
    assert b_resp["reasoning"] == {"effort": "none"}

    ant_prof = providers.build_profile(
        "a", {"api_mode": "anthropic_messages", "base_url": "https://x.test", "supports": {"temperature": True}}
    )
    t_ant = get_transport(ant_prof.api_mode)
    assert isinstance(t_ant, AnthropicMessagesTransport)
    b_ant = t_ant.build_payload(
        ant_prof,
        "claude-sonnet-5-5",
        messages=[{"role": "system", "content": "extra"}, {"role": "user", "content": "hello"}],
        system="sys",
        max_tokens=70,
        reasoning_effort="low",
        reasoning_split=True,
    )
    assert b_ant["system"] == "sys\n\nextra"
    assert b_ant["messages"] == [{"role": "user", "content": "hello"}]
    assert "reasoning_effort" not in b_ant and "reasoning_split" not in b_ant


def test_thinking_policy_mutual_exclusion_for_legacy_and_adaptive():
    minimax_prof = providers.resolve_profile("MiniMax-M3", {}, base_url="https://api.minimaxi.com/v1")
    t_field, effort = resolve_thinking_policy(
        "MiniMax-M3", {}, minimax_prof, thinking_intent="disabled", effort_source="table", default_adaptive_effort="low"
    )
    assert t_field == {"type": "disabled"}
    assert effort is None

    t_field_31, effort_31 = resolve_thinking_policy(
        "MiniMax-M3.1-Flash", {}, minimax_prof, thinking_intent="disabled", effort_source="table", default_adaptive_effort="low"
    )
    assert t_field_31 is None
    assert effort_31 == "low"

    oc_prof = providers.resolve_profile(
        "deepseek-v4.1-flash",
        {"providers": {"opencode-go": {}}, "models": {"deepseek-v4.1-flash": {"provider": "opencode-go"}}},
    )
    t_oc, e_oc = resolve_thinking_policy(
        "deepseek-v4.1-flash", {}, oc_prof, thinking_intent="disabled", effort_source="table", default_adaptive_effort="low"
    )
    assert t_oc is None
    assert e_oc == "none"


def test_gateway_invoke_retries_503_and_handles_adaptive_400():
    calls = []
    sleeps = []
    responses = [
        _FakeResp(503, text="busy"),
        _FakeResp(200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}),
    ]

    def fake_post(url, **kw):
        calls.append(kw["json"])
        return responses.pop(0)

    res = invoke(
        ModelRequest(model="MiniMax-M3", prompt="hi", base_url="https://example.test/v1", thinking_intent="disabled"),
        post_fn=fake_post,
        sleep_fn=sleeps.append,
    )
    assert res.ok and res.text == "ok" and res.attempts == 2 and len(sleeps) == 1

    adaptive_calls = []
    adaptive_resps = [
        _FakeResp(400, text="model requires adaptive thinking"),
        _FakeResp(200, {"choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}], "usage": {}}),
    ]
    res2 = invoke(
        ModelRequest(
            model="MiniMax-M3",
            prompt="hi",
            base_url="https://api.minimaxi.com/v1",
            thinking_intent="enabled",
            adaptive_retry_effort_fallback="low",
        ),
        post_fn=lambda url, **kw: (adaptive_calls.append(kw["json"]), adaptive_resps.pop(0))[1],
        sleep_fn=sleeps.append,
    )
    assert res2.ok and res2.text == "recovered" and res2.adaptive_fallback_used is True
    assert "thinking" in adaptive_calls[0]
    assert "thinking" not in adaptive_calls[1] and adaptive_calls[1]["reasoning_effort"] == "low"


def test_invoke_simple_supports_opencode_responses_and_topic_helpers(monkeypatch):
    seen = []

    def fake_post(url, json=None, headers=None, timeout=None, proxies=None):
        seen.append({"url": url, "body": json, "headers": headers})
        if url.endswith("/responses"):
            return _FakeResp(200, {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "主题X"}]}]})
        return _FakeResp(200, {"choices": [{"message": {"content": "融合摘要"}}]})

    monkeypatch.setattr(requests, "post", fake_post)
    cfg = {
        "providers": {"opencode-go": {}},
        "models": {"gpt-6.1-terra": {"provider": "opencode-go"}},
    }
    res = invoke_simple(
        {"model": "gpt-6.1-terra", "api_key": "sk-1", "endpoint": "https://opencode.ai/zen/go/v1"},
        [{"role": "user", "content": "归类"}],
        cfg=cfg,
    )
    assert res.ok and res.text == "主题X"
    assert seen[-1]["url"].endswith("/responses")
    assert seen[-1]["headers"]["x-opencode-session"]

    fused = topic_fusion._call_llm(
        [{"role": "user", "content": "总结"}],
        {"endpoint": "https://example.test/v1", "model": "m", "api_key": "sk"},
    )
    assert fused == "融合摘要"

    name = topic_maintain._name_cluster(
        "一段内容",
        {"endpoint": "https://example.test/v1", "model": "m", "api_key": "sk"},
    )
    assert name == "融合摘要"


def test_topic_modules_preserve_siliconflow_and_custom_provider_configs(monkeypatch, tmp_path):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None, proxies=None):
        sent.append({"url": url, "body": json, "headers": headers})
        return _FakeResp(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"topic_name":"主题A","summary":"摘要","keywords":["k1"],"category":"技术"}'
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(requests, "post", fake_post)

    # 1) SiliconFlow 必须走 OpenAI 兼容 max_tokens 协议，且不得下发 MiniMax 历史字段
    sf_cfg = {
        "endpoint": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen3-32B",
        "api_key": "sk-sf",
    }
    res_cluster = topic_cluster.name_cluster(
        [{"qa_pairs": [{"user": "问", "assistant": "答"}], "turn_count": 1, "file": "/a/b"}],
        0,
        sf_cfg,
    )
    assert res_cluster["topic_name"] == "主题A"
    topic_fusion._call_llm([{"role": "user", "content": "汇总"}], sf_cfg)
    assert topic_maintain._name_cluster("片段", sf_cfg).startswith("{")
    assert len(sent) == 3
    for call in sent:
        body = call["body"]
        assert "max_tokens" in body
        assert "max_completion_tokens" not in body
        assert "thinking" not in body

    # 2) resolve_simple_llm_cfg 保留完整 cfg (providers/models)，主题模块按自定义通道配置发包
    sent.clear()
    custom_cfg = {
        "llm": {
            "provider": "custom-gw",
            "model": "custom-model-v1",
            "api_key": "sk-custom",
            "base_url": "https://gw.example.internal/v1",
        },
        "providers": {
            "custom-gw": {
                "api_mode": "chat_completions",
                "base_url": "https://gw.example.internal/v1",
                "max_tokens_field": "max_tokens",
                "headers": {"X-Custom-Tenant": "tenant-42"},
                "supports": {"temperature": True},
            }
        },
        "models": {},
    }
    resolved = resolve_simple_llm_cfg(custom_cfg, env_path=str(tmp_path / "missing.env"))
    assert resolved is not None and resolved["config"] is custom_cfg
    assert resolved["provider"] == "custom-gw"

    topic_cluster.name_cluster(
        [{"qa_pairs": [{"user": "问", "assistant": "答"}], "turn_count": 1, "file": "/a/b"}],
        1,
        resolved,
    )
    topic_fusion._call_llm([{"role": "user", "content": "汇总"}], resolved)
    topic_maintain._name_cluster("片段", resolved)
    assert len(sent) == 3
    for call in sent:
        assert call["url"] == "https://gw.example.internal/v1/chat/completions"
        assert call["headers"]["X-Custom-Tenant"] == "tenant-42"
        assert "max_tokens" in call["body"]
        assert "max_completion_tokens" not in call["body"]
        assert "thinking" not in call["body"]
