# -*- coding: utf-8 -*-
"""call_m3 有界重试契约 — 瞬态失败重试, 配置类 4xx 一次都不重试。

背景: 夜里跑样本时 23 发请求有 6 发被掐断 (Max retries exceeded / 5xx),
原来一次失败整批作废, 只能等下一个观察周期。
"""
from __future__ import annotations

import json

import pytest

from v3core import providers

CFG_BASE = {
    "model": "deepseek-v4.1-flash",
    "base_url": "https://opencode.ai/zen/go/v1",
    "timeout": 5,
    "retry_backoff_s": 0,
    "providers": {
        "opencode-go": {
            "api_mode": "responses",
            "base_url": "https://opencode.ai/zen/go/v1",
            "session_header": "x-opencode-session",
            "supports": {"reasoning_effort": True, "temperature": True},
        }
    },
    "models": {"deepseek-v4.1-flash": {"provider": "opencode-go", "reasoning_effort": "low"}},
}
PAYLOAD = {"status": "completed", "output": [{"type": "message", "content": [
    {"type": "output_text", "text": json.dumps({"note": {"content": "印"}, "topic_updates": []})}]}],
    "usage": {"input_tokens": 1, "output_tokens": 1}}


class _Resp:
    def __init__(self, status, payload=None, text="", headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)
        self.headers = headers or {}

    def json(self):
        return self._payload


def _run(monkeypatch, seq, cfg=None):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, proxies=None):
        calls.append(1)
        item = seq[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("requests.post", fake_post)
    from v3core import observer
    out = observer.call_m3({**CFG_BASE, **(cfg or {})}, "sk", "提示词", max_completion_tokens=64)
    return out, len(calls)


def _conn_error():
    import requests
    return requests.exceptions.ConnectionError("Max retries exceeded with url: /responses")


def test_retries_transient_connection_error(monkeypatch):
    out, n = _run(monkeypatch, [_conn_error(), _conn_error(), _Resp(200, PAYLOAD)])
    assert n == 3, "两次瞬态失败后应重试到成功"
    assert json.loads(out["text"])["note"]["content"] == "印"


def test_retries_5xx_then_success(monkeypatch):
    out, n = _run(monkeypatch, [_Resp(503, text="upstream busy"), _Resp(200, PAYLOAD)])
    assert n == 2


def test_does_not_retry_4xx_config_error(monkeypatch):
    out, n = _run(monkeypatch, [_Resp(400, text="requires adaptive thinking")] * 3)
    # 400 只走 adaptive-thinking 那一次特殊重试, 不消耗普通重试额度
    assert n <= 2, "配置类 4xx 不应被反复重试"
    assert "error" in out or "text" in out


def test_does_not_retry_auth_error(monkeypatch):
    out, n = _run(monkeypatch, [_Resp(401, text="bad key")] * 3)
    assert n == 1
    assert "401" in out["error"]


def test_attempts_configurable(monkeypatch):
    out, n = _run(monkeypatch, [_conn_error()] * 5, cfg={"retry_attempts": 1})
    assert n == 1
    assert "请求失败" in out["error"]


def test_gives_up_after_max_attempts(monkeypatch):
    out, n = _run(monkeypatch, [_conn_error()] * 5, cfg={"retry_attempts": 3})
    assert n == 3
    assert "请求失败" in out["error"]


def test_retryable_classification():
    import requests
    assert providers.is_retryable_exception(requests.exceptions.ConnectionError("x"))
    assert providers.is_retryable_exception(requests.exceptions.ReadTimeout("x"))
    assert not providers.is_retryable_exception(ValueError("x"))
    # 包在 __cause__ 里的也要认
    outer = RuntimeError("wrapper")
    outer.__cause__ = requests.exceptions.ConnectionError("inner")
    assert providers.is_retryable_exception(outer)
    assert providers.is_retryable_status(429) and providers.is_retryable_status(503)
    assert not providers.is_retryable_status(400) and not providers.is_retryable_status(401)


def test_retry_delay_honours_retry_after():
    resp = _Resp(429, headers={"Retry-After": "7"})
    assert providers.retry_delay(0, 2.0, resp) == 7.0
    assert providers.retry_delay(2, 2.0) == 8.0   # 指数退避
    assert providers.retry_delay(9, 2.0) == 30.0  # 有上限
