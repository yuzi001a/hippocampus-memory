# -*- coding: utf-8 -*-
"""退化判据与输入消毒的回归 (2026-11-23)。

背景: deepseek 系模型在特定输入下会"提前收尾"——声明关思考却先烧掉思考 token,
然后自己吐结束符, 留下半截 JSON。QA 2911 是这个退化的固定样本 (五发五断)。
这里锁住三件事: 判据形状、消毒形状、证据形状。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v3core import observer  # noqa: E402


def _normal_note() -> str:
    lines = [f"- [用户] 第{i}项决定〔已实施〕[#{i}]" for i in range(1, 81)]
    return "## 关键决策\n" + "\n".join(lines)


def test_short_stop_text_is_degenerate():
    r = {"text": '{"note": {"content": "## 关键决策\\n- [用户] 改吧', "finish_reason": "stop"}
    reason = observer._degeneration_reason(r)
    assert reason is not None and reason.startswith("early_stop_text=")


def test_normal_note_is_not_degenerate():
    assert observer._degeneration_reason({"text": _normal_note(), "finish_reason": "stop"}) is None


def test_llm_error_is_not_classified_as_degeneration():
    assert observer._degeneration_reason({"error": "HTTP 500"}) is None


def test_repetition_loop_detected():
    unit = "又流口水了，" * 8
    body = _normal_note() + "\n" + unit * 6
    assert observer._looks_like_loop(body) is True
    assert observer._degeneration_reason({"text": body}) == "repetition_loop"


def test_tag_sanitize_breaks_think_tag_but_keeps_content():
    src = "先修 `parse_card()` 去掉 `<think>` 标签"
    out, n = observer._sanitize_special_tokens(src)
    assert n == 1
    assert "< think>" in out
    assert "<think" not in out
    assert "parse_card" in out


def test_sanitize_ignores_plain_text():
    src = "这一段没有尖括号"
    out, n = observer._sanitize_special_tokens(src)
    assert n == 0 and out == src


def test_degeneration_evidence_shape():
    r = {
        "text": "abc",
        "reasoning": "思考",
        "finish_reason": "stop",
        "usage": {"completion_tokens_details": {"reasoning_tokens": 766}},
    }
    assert observer._degeneration_evidence(r) == {
        "chars": 3,
        "reasoning_tokens": 766,
        "reasoning_chars": 2,
        "finish": "stop",
    }


def test_degeneration_skips_same_prompt_retry():
    """用户定调: 一旦判定退化, 不原样重试, 直接换输入。"""
    assert observer._plan_parse_retries({}, "early_stop_text=459") == 0
    assert observer._plan_parse_retries({"json_parse_primary_retries": 3}, "repetition_loop") == 0


def test_format_drift_keeps_same_prompt_retry():
    """输出够长但 JSON 坏了 = 格式漂移, 原样重试仍然有意义。"""
    assert observer._plan_parse_retries({}, None) == 2
    assert observer._plan_parse_retries({"json_parse_primary_retries": 3}, None) == 3


def test_sanitize_map_records_restore_pairs():
    src = "先修 `parse_card()` 去掉 `<think>` 标签"
    out, mapping, hits = observer._sanitize_special_tokens_map(src)
    assert hits == 1
    assert "< think>" in out
    assert mapping == {"< think>": "<think>"}


def test_restore_turns_trace_back():
    """note 2189 的真实形态: 模型照抄了被垫开的 `< think>`。"""
    body = "- `parse_card()` 增加去除 `< think>` 标签的处理，防止再因 thinking 标签解析失败"
    out, n = observer._restore_sanitized_tokens(body, {"< think>": "<think>"})
    assert n == 1
    assert "<think>" in out
    assert "< think>" not in out


def test_restore_leaves_unrelated_text_alone():
    body = "a < b 这种比较符号不该被动，`<other>` 也不该被动"
    out, n = observer._restore_sanitized_tokens(body, {"< think>": "<think>"})
    assert n == 0 and out == body
