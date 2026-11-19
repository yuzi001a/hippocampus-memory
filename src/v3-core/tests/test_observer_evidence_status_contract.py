"""Observer「证据归属 + 状态」契约 (2026-11-19)。

生产实测 (617 条 ``observation_notes``)：

- ``关键决策`` 2905 条 bullet 里只有 447 条 (15.4%) 能看出是用户拍板，217 条
  自带「尚未 / 未实施 / 计划」字样 —— 助手提案落进了「决策」。
- ``进行中状态（未完事项）`` 2585 条 bullet 里 533 条带完成标记（已完成 / 已升级
  / 已验证）—— 已实施、已验证的结果落进了「未完事项」。

根因不是模型不会写，而是 prompt 里没有这两层约定：v4 第三轮为了阻止模型「把 8 个
section 全填满」，把维度注册表里"该节收什么"的定义连同 ``##`` 槽位一起删了，模型
只剩节名可依。本文件把三件事钉成契约，防止下一次"压缩 prompt"又把它们顺手删掉：

1. 每个默认维度都必须把 ``define``（该节收什么）渲染进 prompt；
2. 「证据归属与状态」规则块必须在 prompt 里（归属 / 状态词 / 纠正 / 偏好边界 / 不确定）；
3. 用户自定义维度只有 ``prompt`` 时，仍要能落成一句话定义（积木化契约不退化）。

Hermetic: 纯字符串，无网络、无模型、无 PostgreSQL、不读写 profile。
"""

from __future__ import annotations

import v3core.observer as observer_mod

QA = "--- QA #1 [2026-11-19 00:00 UTC] ---\nQ: 用户问题 [#1]\nA: 助手回答 [#1]\n"
PREV = "## 关键决策\n- 旧事实 [#1]"


def _prompt(*, dims=None, qa_text: str = QA, identity_name: str = "") -> str:
    return observer_mod._observer_prompt(
        prev_note_text=PREV,
        qa_text=qa_text,
        candidates_text="- id=t_1 title='x' sim=0.9",
        now_str="2026-11-19 00:00 UTC",
        qa_time_range="(从 QA 头部时间戳读取)",
        identity_name=identity_name,
        dims=dims,
        identity_layer="",
    )


# ── 1. 维度定义必须进 prompt ────────────────────────────────────────────────

def test_every_default_dimension_renders_its_section_and_definition():
    prompt = _prompt()
    for name, spec in observer_mod.OBSERVER_DIMENSIONS.items():
        assert spec["section"] in prompt, f"节名缺失: {name}"
        assert spec["define"] in prompt, f"节定义缺失: {name}"


def test_disabled_dimension_disappears_from_prompt():
    dims = dict(observer_mod.OBSERVER_DIMENSIONS)
    dropped = dims.pop("lessons")
    prompt = _prompt(dims=dims)
    assert dropped["define"] not in prompt
    assert dropped["section"] not in prompt


def test_custom_dimension_with_only_prompt_still_gets_a_definition():
    dims = {
        "custom": {
            "section": "自定义节",
            "prompt": (
                "**仅在本段 QA 出现该类变化时输出**\n"
                "只写 X 相关的变化。"
            ),
            "default": True,
        }
    }
    prompt = _prompt(dims=dims)
    assert "- 自定义节: 只写 X 相关的变化" in prompt
    # 「按需输出」口径只由渲染处追加一次，不重复
    assert prompt.count("仅在本段 QA 出现该类变化时输出") == 1


def test_collapse_helper_strips_on_demand_clause_from_custom_prompt():
    text = "**仅在本段 QA 出现新的拍板决策时输出**, 没新决策就省略该节。\n拍板 + 理由"
    assert observer_mod._collapse_dim_prompt(text) == "拍板 + 理由"


# ── 2. 证据归属 / 状态 / 纠正 / 偏好边界 / 不确定 ───────────────────────────

def test_status_vocabulary_is_present():
    prompt = _prompt()
    for tag in ("〔已实施〕", "〔已验证〕", "〔计划中〕", "〔未完成〕", "〔已推翻〕"):
        assert tag in prompt, f"状态词缺失: {tag}"


def test_user_decision_is_separated_from_assistant_suggestion():
    prompt = _prompt()
    assert "建议未拍板" in prompt
    assert "用户原话支撑" in prompt


def test_supersede_rule_keeps_the_superseded_value_marked():
    prompt = _prompt()
    assert "用户推翻" in prompt
    assert "原为 X → 改为 Y" in prompt


def test_task_scope_is_not_promoted_to_long_term_preference():
    prompt = _prompt()
    assert "长期偏好" in prompt
    assert "本轮要求" in prompt


def test_uncertainty_must_be_preserved():
    prompt = _prompt()
    assert "存疑" in prompt or "待核" in prompt
    assert "缺失的证据不等于" in prompt


# ── 3. 输入与 JSON 契约没有被新块破坏 (f-string 双花括号回归) ────────────────

def test_inputs_are_still_injected_verbatim():
    prompt = _prompt()
    assert QA in prompt
    assert PREV in prompt


def test_json_contract_still_uses_the_historical_brace_rendering():
    # 这份 prompt 是 f-string, JSON 契约里的花括号靠 ``{{{{``→``{{`` 转义; 改动
    # prompt 正文时最容易被顺手破坏的是这里, 所以按实际渲染结果钉住。
    prompt = _prompt()
    assert '"note": {{' in prompt
    assert '"topic_updates": [' in prompt
    # 新增的「证据归属与状态」块本身不得引入任何花括号
    start = prompt.index("## 证据归属与状态")
    end = prompt.index("## 时间锚点")
    assert "{" not in prompt[start:end] and "}" not in prompt[start:end]


def test_identity_name_is_rendered_in_the_evidence_block():
    prompt = _prompt(identity_name="小柚")
    assert "与小柚 (助手) 说过的" in prompt
