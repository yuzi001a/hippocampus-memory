"""Observer 的 M3 调用必须遵守与 llm.LLMClient 相同的 thinking 模型感知规则。

Y400 全量重建故障复盘：observer 走自己的 HTTP 路径（``observer.call_m3``），
历史上无条件发送 ``"thinking": {"type": "disabled"}``。MiniMax M3.1 家族对该
字段返回 HTTP 400 ``model ... requires adaptive thinking``，导致派生层第一轮
就把 30 条 QA 落成 adaptive 状态、游标不推进 —— 表现为「派生层跑不动」，
且失败点只出现在日志里一条 WARNING。
"""

import json

import pytest
import requests

import v3core.observer as observer_mod


@pytest.mark.parametrize(
    ("model", "thinking", "expected"),
    [
        # M3.1 家族：disabled 必须省略字段（服务端要求 adaptive thinking）
        ("MiniMax-M3.1-Flash", "disabled", None),
        ("MiniMax-M3.1-Flash-Preview", "disabled", None),
        ("minimax-m3.1-flash", "disabled", None),
        # 老模型保持历史行为
        ("MiniMax-M3", "disabled", {"type": "disabled"}),
        ("MiniMax-M2.5", "disabled", {"type": "disabled"}),
        # enabled 原样传递
        ("MiniMax-M3", "enabled", {"type": "enabled"}),
        ("MiniMax-M3.1-Flash", "enabled", {"type": "enabled"}),
        # 缺省即 disabled
        ("MiniMax-M3.1-Flash", "", None),
        ("MiniMax-M3", "", {"type": "disabled"}),
    ],
)
def test_observer_thinking_payload_is_model_aware(model, thinking, expected):
    assert observer_mod._m3_thinking_payload(model, thinking) == expected


class _FakeResponse:
    def __init__(self, status_code, text="", data=None):
        self.status_code = status_code
        self.text = text
        self._data = data or {}

    def json(self):
        return self._data


def test_call_m3_omits_thinking_for_m31_disabled(monkeypatch):
    seen = []

    def fake_post(url, **kwargs):
        seen.append(kwargs["json"])
        return _FakeResponse(200, data={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    monkeypatch.setattr(requests, "post", fake_post)

    result = observer_mod.call_m3(
        {"model": "MiniMax-M3.1-Flash", "base_url": "https://api.minimaxi.com/v1"},
        "test-secret-key",
        "hello",
        thinking="disabled",
    )

    assert result["text"] == "ok"
    assert len(seen) == 1
    assert "thinking" not in seen[0], "M3.1 收到 thinking=disabled 会 400，必须省略该字段"
    # 省略 thinking 后服务端默认 adaptive，必须显式压低 effort：实测 30 条 QA 批次
    # 不压时 reasoning 56,547 字符吃满输出预算、JSON 未闭合 → 每轮静默超时/写不出印。
    assert seen[0]["reasoning_effort"] == "low"
    # 其余历史字段不受影响
    assert seen[0]["model"] == "MiniMax-M3.1-Flash"
    assert seen[0]["reasoning_split"] is True
    assert seen[0]["max_completion_tokens"] == 16384


def test_call_m3_retries_once_without_thinking_on_adaptive_400(monkeypatch):
    seen = []

    def fake_post(url, **kwargs):
        seen.append(kwargs["json"])
        if len(seen) == 1:
            return _FakeResponse(
                400,
                text=json.dumps(
                    {
                        "type": "error",
                        "error": {
                            "type": "bad_request_error",
                            "message": 'invalid params, model "MiniMax-M3.1-Flash" requires adaptive thinking',
                        },
                    }
                ),
            )
        return _FakeResponse(200, data={"choices": [{"message": {"content": "recovered"}}], "usage": {}})

    monkeypatch.setattr(requests, "post", fake_post)

    result = observer_mod.call_m3(
        {"model": "MiniMax-M3", "base_url": "https://api.minimaxi.com/v1"},
        "test-secret-key",
        "hello",
        thinking="enabled",
    )

    assert result["text"] == "recovered"
    assert len(seen) == 2
    assert seen[0]["thinking"] == {"type": "enabled"}
    # 显式下发 thinking 的请求不加 effort（那个族有自己的开关）
    assert "reasoning_effort" not in seen[0]
    # 重试体去掉 thinking 后走 adaptive 默认档，必须补 effort
    assert "thinking" not in seen[1]
    assert seen[1]["reasoning_effort"] == "low"


def test_call_m3_does_not_retry_other_400(monkeypatch):
    seen = []

    def fake_post(url, **kwargs):
        seen.append(kwargs["json"])
        return _FakeResponse(400, text='{"error": "bad request: messages empty"}')

    monkeypatch.setattr(requests, "post", fake_post)

    result = observer_mod.call_m3(
        {"model": "MiniMax-M3.1-Flash"}, "test-secret-key", "hello", thinking="disabled"
    )

    assert "error" in result
    assert len(seen) == 1, "非 adaptive-thinking 的 4xx 不允许重试"


def test_call_m3_reasoning_effort_is_configurable(monkeypatch):
    """effort 是配置项，不是硬编码常量 — 换档位只改 config。"""
    seen = []

    def fake_post(url, **kwargs):
        seen.append(kwargs["json"])
        return _FakeResponse(200, data={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    monkeypatch.setattr(requests, "post", fake_post)

    observer_mod.call_m3(
        {"model": "MiniMax-M3.1-Flash", "reasoning_effort": "medium"},
        "test-secret-key",
        "hello",
        thinking="disabled",
    )

    assert seen[0]["reasoning_effort"] == "medium"


def test_legacy_models_never_receive_reasoning_effort(monkeypatch):
    """M3 等老模型走 thinking={type:disabled}，不应被塞 effort（未知参数风险）。"""
    seen = []

    def fake_post(url, **kwargs):
        seen.append(kwargs["json"])
        return _FakeResponse(200, data={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    monkeypatch.setattr(requests, "post", fake_post)

    observer_mod.call_m3(
        {"model": "MiniMax-M3"}, "test-secret-key", "hello", thinking="disabled"
    )

    assert seen[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in seen[0]
