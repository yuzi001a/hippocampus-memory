from types import SimpleNamespace

import openai
import pytest
import requests

import v3core.llm as llm_mod
from v3core.config_model import from_legacy_dict, validate_config


@pytest.mark.parametrize(
    ("model", "mode", "expected"),
    [
        ("MiniMax-M3.1-Flash", "auto", None),
        ("MiniMax-m3.1-flash-preview", "auto", None),
        ("MiniMax-M3", "auto", {"type": "disabled"}),
        ("MiniMax-M2.5", "auto", {"type": "disabled"}),
        ("MiniMax-M3.1-Flash", "disabled", None),
        ("MiniMax-M3", "enabled", {"type": "enabled", "budget_tokens": 32768}),
        ("MiniMax-M3.1-Flash", "enabled", {"type": "enabled", "budget_tokens": 32768}),
    ],
)
def test_thinking_payload_is_model_and_mode_aware(model, mode, expected):
    assert llm_mod._thinking_payload(model, mode) == expected


def test_thinking_mode_defaults_and_validation():
    assert from_legacy_dict({}).llm.thinking_mode == "auto"
    assert from_legacy_dict({"llm": {"thinking_mode": "disabled"}}).llm.thinking_mode == "disabled"
    invalid = from_legacy_dict({"llm": {"thinking_mode": "sometimes"}})
    assert any("thinking_mode" in warning for warning in validate_config(invalid))


def test_minimax_retries_once_without_thinking_for_adaptive_thinking_400(monkeypatch):
    requests_seen = []

    class FakeResponse:
        def __init__(self, status_code, text="", data=None):
            self.status_code = status_code
            self.text = text
            self.headers = {}
            self._data = data or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                error = requests.exceptions.HTTPError(f"{self.status_code} Client Error")
                error.response = self
                raise error

        def json(self):
            return self._data

    def fake_post(url, *, json, headers, timeout, proxies):
        requests_seen.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if len(requests_seen) == 1:
            return FakeResponse(
                400,
                text='invalid params: model requires adaptive thinking; thinking.type="enabled" is invalid',
            )
        return FakeResponse(
            200,
            data={"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )

    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    client = llm_mod.LLMClient({
        "llm": {
            "provider": "minimax",
            "model": "MiniMax-M3.1-Flash",
            "api_key": "test-secret-key",
            "thinking_mode": "enabled",
        }
    })

    assert client._chat_minimax("system", [{"role": "user", "content": "hello"}], 0.2) == "ok"
    assert len(requests_seen) == 2
    assert requests_seen[0]["json"]["thinking"]["type"] == "enabled"
    assert "thinking" not in requests_seen[1]["json"]


def test_openai_timeout_is_bounded_classified_and_logged_without_key(monkeypatch, caplog):
    constructor_kwargs = {}
    attempts = []

    def fake_create(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("simulated timeout")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )

    def fake_openai(**kwargs):
        constructor_kwargs.update(kwargs)
        return fake_client

    monkeypatch.setattr(openai, "OpenAI", fake_openai)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _: None)
    client = llm_mod.LLMClient({
        "llm": {
            "provider": "openai",
            "model": "test-model",
            "api_key": "test-secret-key",
            "timeout": 37,
        }
    })

    with pytest.raises(RuntimeError) as raised:
        client._chat_openai("system", [{"role": "user", "content": "hello"}], 0.2)

    assert constructor_kwargs["timeout"] == 37
    assert constructor_kwargs["max_retries"] == 0
    assert len(attempts) == llm_mod._MAX_RETRIES
    assert "llm_timeout" in str(raised.value)
    assert "elapsed=" in str(raised.value)
    assert "test-secret-key" not in caplog.text
    assert "attempt=" in caplog.text
    assert "llm_timeout" in caplog.text


def test_openai_config_timeout_defaults_to_600_and_is_typed():
    cfg = from_legacy_dict({})
    assert cfg.llm.timeout == 600
    assert from_legacy_dict({"llm": {"timeout": 21}}).llm.timeout == 21
