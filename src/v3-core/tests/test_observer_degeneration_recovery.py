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


def test_template_shell_is_empty_payload():
    """真实样本: note 2342 全文 43 字, 是提示词里的格式样板。"""
    body = "## 当轮变化 / 本轮确有变化的 section>\n<当轮变化 + 来源 [#N]>"
    assert observer._empty_payload_reason(body) == "no_citation"


def test_note_without_citation_is_empty_payload():
    body = "## 关键决策\n- [用户] 做了很多事〔已实施〕\n- [用户] 又做了一些事〔已实施〕"
    assert observer._empty_payload_reason(body) == "no_citation"


def test_normal_note_passes_empty_payload_check():
    assert observer._empty_payload_reason(_normal_note()) is None
    assert observer._empty_payload_reason("") == "empty_content"


class _Profile:
    def __init__(self, caps):
        self._caps = set(caps)

    def supports_cap(self, name):
        return name in self._caps


def test_thinking_off_forces_explicit_none():
    """省略不等于关闭: classic 形状下 effort 为 None 时必须补成 "none"。"""
    got = observer._effective_reasoning_effort(None, "disabled", _Profile({"reasoning_effort"}))
    assert got == "none"


def test_explicit_effort_is_kept():
    got = observer._effective_reasoning_effort("low", "adaptive", _Profile({"reasoning_effort"}))
    assert got == "low"


def test_channel_without_cap_stays_quiet():
    got = observer._effective_reasoning_effort(None, "disabled", _Profile(set()))
    assert got is None


def test_usage_reasoning_tokens_reads_both_shapes():
    assert observer._usage_reasoning_tokens({"completion_tokens_details": {"reasoning_tokens": 163}}) == 163
    assert observer._usage_reasoning_tokens({"output_tokens_details": {"reasoning_tokens": 278}}) == 278
    assert observer._usage_reasoning_tokens({}) == 0


# --- 2026-11-26 全量体检补的两处校验漏子 ---

def _long_note(tag: str = "A", n: int = 40) -> str:
    lines = [f"- [用户] {tag} 第{i}项进展已完成〔已实施〕[#{i}]" for i in range(1, n + 1)]
    return "## 关键决策\n" + "\n".join(lines)


def test_markdown_link_is_not_anchor():
    """[#25](url) 是超链接不是问答锚点 — 否则 10 条问答的笔记会被误判"锚点越界"。"""
    body = ("## 关键决策\n- [用户] 修好了〔已实施〕[#3]，见 PR "
            "[#25](https://github.com/x/y/pull/25)")
    v = observer._validate_note_payload(body, 10)
    assert v["status"] == "ok", v.get("reasons")


def test_plain_anchor_still_bounds_checked():
    body = "## 关键决策\n- [用户] 修好了〔已实施〕[#25]"
    v = observer._validate_note_payload(body, 10)
    assert v["status"] == "invalid"
    assert "锚点越界" in v["reasons"][0]


def test_only_markdown_link_is_no_citation():
    body = "## 关键决策\n- 修好了〔已实施〕见 PR [#25](https://github.com/x/y/pull/25)"
    assert observer._empty_payload_reason(body) == "no_citation"


def test_whole_repeat_is_flagged_with_high_ratio():
    prev = _long_note("A")
    v = observer._validate_note_payload(prev, 40, prev_text=prev)
    assert v["status"] == "repetitive"
    assert v["prev_len_ratio"] >= 0.95
    assert observer._should_skip_repeat(v) is True


def test_short_delta_repeat_is_not_skipped():
    prev = _long_note("A")
    delta = "## 关键决策\n" + "\n".join(prev.splitlines()[1:4])
    v = observer._validate_note_payload(delta, 40, prev_text=prev)
    assert v["status"] == "repetitive"
    assert v["prev_len_ratio"] < observer.NOTE_REPEAT_WHOLE_MAX
    assert observer._should_skip_repeat(v) is False


def test_fresh_note_is_not_skipped():
    prev = _long_note("A")
    fresh = "## 操作知识\n" + "\n".join(
        f"- 命令 v{i} 的作用是查询索引状态与快照版本〔已验证〕[#{i}]" for i in range(1, 41))
    v = observer._validate_note_payload(fresh, 40, prev_text=prev)
    assert v["status"] == "ok", v.get("reasons")
    assert observer._should_skip_repeat(v) is False


# --- 2026-11-27 审查修复回归: 重试空壳拦截 / fallback 互斥 / _san_map 隔离 / 原子状态写 ---


def test_parse_non_empty_observer_json_rejects_empty_shell():
    import json
    shell = json.dumps({"note": {"content": "## 当轮变化\n<没有引用>"}, "topic_updates": []})
    parsed, reason = observer._parse_non_empty_observer_json(shell)
    assert parsed is None and reason == "no_citation"

    good = json.dumps({"note": {"content": _normal_note()}, "topic_updates": []})
    parsed2, reason2 = observer._parse_non_empty_observer_json(good)
    assert parsed2 is not None and reason2 is None


def _run_worker_stub(monkeypatch, tmp_path, *, qa_question: str, call_m3_fn):
    from datetime import datetime, timezone

    state_path = tmp_path / "observer_state.json"
    written_notes: list[str] = []
    ts = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)

    class _Cur:
        def __init__(self):
            self._mode = ""

        def execute(self, sql, params=None):
            s = " ".join(str(sql).split())
            if "SELECT COUNT(*)" in s:
                self._mode = "stats_count"
            elif "GROUP BY session_id" in s:
                self._mode = "stats_sess"
            elif "FROM qa_pairs" in s:
                self._mode = "qa_rows"
            else:
                self._mode = "prev"

        def fetchone(self):
            if self._mode == "stats_count":
                return (1, len(qa_question) + 20, ts)
            return None

        def fetchall(self):
            if self._mode == "stats_sess":
                return [("sess-1", len(qa_question) + 20)]
            if self._mode == "qa_rows":
                return [(1, "sess-1", ts, qa_question, "这是完整回答内容。", len(qa_question) + 20)]
            return []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

    class _Lease:
        connection = _Conn()

    monkeypatch.setattr(observer, "_recall_candidates", lambda **kw: [])
    monkeypatch.setattr(observer, "_observer_resolve_api_key", lambda: ("sk-test", None))
    monkeypatch.setattr(observer, "_load_identity_layer", lambda cfg: "")
    monkeypatch.setattr(observer, "_write_observation_note",
                        lambda pg, *, content, **kw: written_notes.append(content) or 101)
    monkeypatch.setattr(observer.time, "sleep", lambda *_: None)
    monkeypatch.setattr(observer, "call_m3", call_m3_fn)

    cfg = {
        "model": "primary-m",
        "state_path": str(state_path),
        "count_threshold": 1,
        "token_threshold_chars": 1,
        "debounce_minutes": 0,
        "json_parse_primary_retries": 1,
        "json_parse_sanitize_retries": 1,
        "fallback_llm": {"model": "fallback-m", "api_key": "sk-fb"},
    }
    observer._observe_worker(cfg, "", _pinned_lease=_Lease(), _manage_lease=False)
    return written_notes, state_path


def test_worker_rejects_empty_shell_in_all_retry_and_fallback_paths(monkeypatch, tmp_path):
    import json
    models_called: list[str] = []
    shell = json.dumps({"note": {"content": "## 当轮变化\n<没有引用>"}, "topic_updates": []})

    def fake_m3(cfg, api_key, prompt, **kw):
        models_called.append(cfg.get("model", ""))
        return {"text": shell, "usage": {}}

    written, state_path = _run_worker_stub(
        monkeypatch, tmp_path, qa_question="去掉 <think> 标签", call_m3_fn=fake_m3
    )
    assert written == []
    assert "primary-m" in models_called and "fallback-m" in models_called
    assert state_path.exists()


def test_worker_sanitized_retry_success_skips_fallback_and_restores_tokens(monkeypatch, tmp_path):
    import json
    calls: list[tuple[str, str]] = []
    good_sanitized_note = (
        "## 关键决策\n- [用户] 去掉 `< think>` 标签处理〔已实施〕[#1]\n"
        + "\n".join(f"- [用户] 第{i}条补充事实记录〔已实施〕[#1]" for i in range(2, 55))
    )

    def fake_m3(cfg, api_key, prompt, **kw):
        model = cfg.get("model", "")
        calls.append((model, prompt))
        if model == "primary-m" and "<think>" in prompt:
            return {"text": '{"note": {"content": "半截', "finish_reason": "stop", "usage": {}}
        if model == "primary-m" and "< think>" in prompt:
            return {"text": json.dumps({"note": {"content": good_sanitized_note}, "topic_updates": []}), "usage": {}}
        raise AssertionError(f"unexpected call_m3: {model}")

    written, _ = _run_worker_stub(
        monkeypatch, tmp_path, qa_question="去掉 <think> 标签", call_m3_fn=fake_m3
    )
    assert len(written) == 1
    assert "<think>" in written[0]
    assert "< think>" not in written[0]
    assert all(m == "primary-m" for m, _ in calls)


def test_worker_sanitized_fail_then_fallback_success_does_not_apply_san_map(monkeypatch, tmp_path):
    import json
    fb_note = (
        "## 关键决策\n- [用户] 原文保留 `< think>` 字面量〔已实施〕[#1]\n"
        + "\n".join(f"- [用户] 第{i}条补充事实记录〔已实施〕[#1]" for i in range(2, 55))
    )

    def fake_m3(cfg, api_key, prompt, **kw):
        model = cfg.get("model", "")
        if model == "primary-m":
            return {"text": '{"note": {"content": "半截', "finish_reason": "stop", "usage": {}}
        return {"text": json.dumps({"note": {"content": fb_note}, "topic_updates": []}), "usage": {}}

    written, _ = _run_worker_stub(
        monkeypatch, tmp_path, qa_question="去掉 <think> 标签", call_m3_fn=fake_m3
    )
    assert len(written) == 1
    assert "< think>" in written[0], "fallback 使用原 prompt，不得被上一级失败的 _san_map 误替换"


def test_record_repeat_skipped_writes_atomically_without_tmp_leftovers(tmp_path):
    import json
    state_path = tmp_path / "observer_state.json"
    cfg = {"state_path": str(state_path)}
    observer._record_repeat_skipped(1, 10, overlap=0.9, len_ratio=0.95, cfg=cfg)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(data["repeat_skipped"]) == 1
    assert data["repeat_skipped"][0]["qa_range"] == [1, 10]
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
