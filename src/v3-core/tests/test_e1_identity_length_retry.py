# -*- coding: utf-8 -*-
"""Regression tests for E1 identity-block LENGTH_TOO_LONG recovery."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import v3core.e1 as e1_mod
from v3core.generated_context_contract import (
    _IDENTITY_MAX_CHARS,
    validate_identity_block,
)


_FAKE_KEY = "test-key-not-a-real-credential"
_VALID_IDENTITY = (
    "我是合成占位的身份核心（测试专用）。我关注的是把长期脉络连成一条线，"
    "让新对话从已有认知开始，而不是从零重建。"
)
_VALID_YIN = (
    "## 关键决策\n这是一段用于测试的印内容，记录一次可复现的决策和验证结果。\n"
    "\n## 关键教训\n这是一段用于测试的印内容，记录边界、风险与回滚办法。\n"
)
_VALID_SITUATION = (
    "## 活跃主题\n- 暂无新数据。\n\n"
    "## 当前关注与待办\n- 暂无新数据。\n\n"
    "## 状态速记\n- 暂无新数据。\n"
)


class _FakeLLMClient:
    model = "M3-test"
    provider = "test-provider"

    def __init__(self, identity_outputs: list[str]):
        self.identity_outputs = list(identity_outputs)
        self.calls: list[dict] = []

    def chat(self, system, messages, temperature=0.7, max_output_tokens=None):
        self.calls.append({
            "system": system,
            "messages": messages,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        })
        if system.startswith(e1_mod.IDENTITY_SHORT_PROMPT):
            return self.identity_outputs.pop(0)
        if system == e1_mod.SITUATION_OVERVIEW_PROMPT:
            return _VALID_SITUATION
        return _VALID_YIN

    @property
    def identity_calls(self) -> list[dict]:
        return [
            call for call in self.calls
            if call["system"].startswith(e1_mod.IDENTITY_SHORT_PROMPT)
        ]


def _config(base: Path) -> dict:
    return {
        "basePath": str(base),
        "e1": {"enabled": True, "confidence_threshold": 0.3},
        "llm": {
            "provider": "minimax",
            "model": "M3-test",
            "api_key": _FAKE_KEY,
            "base_url": "https://llm.invalid/v1",
        },
    }


def _run_e1(monkeypatch, base: Path, identity_outputs: list[str]) -> tuple[str, _FakeLLMClient]:
    base.mkdir(parents=True, exist_ok=True)
    client = _FakeLLMClient(identity_outputs)
    monkeypatch.setattr(e1_mod, "LLMClient", lambda config: client)
    result = e1_mod.synthesize_yin(_config(base), pool=None)
    return result, client


def _oversized_identity(label: str) -> str:
    return f"我是{label}" + ("甲" * (_IDENTITY_MAX_CHARS + 8))


def test_length_too_long_retries_once_with_explicit_limit_and_accepts_only_valid_retry(
    tmp_path, monkeypatch
):
    base = tmp_path / "profile"
    too_long = _oversized_identity("首次超长")

    result, client = _run_e1(
        monkeypatch, base, [too_long, _VALID_IDENTITY]
    )

    assert "E1 完成" in result
    assert len(client.identity_calls) == 2, "LENGTH_TOO_LONG must trigger exactly one retry"
    assert str(_IDENTITY_MAX_CHARS) in client.identity_calls[1]["system"], (
        "retry system prompt must state the validator's hard character limit"
    )
    identity_path = base / "identity_block.md"
    assert identity_path.read_text(encoding="utf-8") == validate_identity_block(_VALID_IDENTITY)
    assert too_long not in identity_path.read_text(encoding="utf-8")


def test_two_oversized_candidates_degrade_state_without_losing_other_e1_outputs(
    tmp_path, monkeypatch, caplog
):
    base = tmp_path / "profile"
    first = _oversized_identity("第一次超长")
    second = _oversized_identity("第二次超长")

    result, client = _run_e1(monkeypatch, base, [first, second])

    assert "E1 完成" in result, "identity rejection must not abort the rest of E1"
    assert len(client.identity_calls) == 2, "the retry budget is exactly one extra call"
    assert not (base / "identity_block.md").exists(), "no rejected identity may be written"
    assert not (base / "identity_block.md.tmp").exists()
    for path in base.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert first not in text
            assert second not in text

    y_files = list((base / "y").glob("y_*.md"))
    assert len(y_files) == 1
    assert "关键决策" in y_files[0].read_text(encoding="utf-8")
    assert (base / "situation_overview.md").is_file()

    state = json.loads((base / "state.json").read_text(encoding="utf-8"))
    assert state["identity_block"] == "degraded"
    assert "LENGTH_TOO_LONG" in state["identity_block_reason"]
    assert str(_IDENTITY_MAX_CHARS) in state["identity_block_reason"]
    assert state.get("processed", {}).get("lastE1Run")

    errors = [record.getMessage() for record in caplog.records if record.levelname == "ERROR"]
    assert any(
        "M3-test" in message
        and "LENGTH_TOO_LONG" in message
        and str(_IDENTITY_MAX_CHARS) in message
        for message in errors
    ), "final rejection must log the model, reason, observed length, and limit"
