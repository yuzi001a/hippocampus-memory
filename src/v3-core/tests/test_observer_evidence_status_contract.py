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
        # define 里的 {_id} 占位在渲染时会换成身份名 (默认 "我")
        assert spec["define"].replace("{_id}", "我") in prompt, f"节定义缺失: {name}"


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


def test_collapse_helper_only_strips_lines_that_start_with_the_clause():
    # 用户指导语里顺带提到口径词, 不能整行被吞掉
    text = "写作时不要照抄「仅在本段 QA 出现该类变化时输出」这句话。\n保留本节其余指导"
    collapsed = observer_mod._collapse_dim_prompt(text)
    assert "写作时不要照抄" in collapsed
    assert "保留本节其余指导" in collapsed


def test_collapse_helper_strips_on_demand_clause_from_custom_prompt():
    text = "**仅在本段 QA 出现新的拍板决策时输出**, 没新决策就省略该节。\n拍板 + 理由"
    assert observer_mod._collapse_dim_prompt(text) == "拍板 + 理由"


# ── 2. 证据归属 / 状态 / 纠正 / 偏好边界 / 不确定 ───────────────────────────

def test_format_example_is_present():
    prompt = _prompt()
    for tag in ("〔已实施〕", "〔已验证〕", "〔计划中〕", "〔未完成〕", "〔已推翻〕"):
        assert tag in prompt, f"状态词缺失: {tag}"
    # 词表不够, 必须给出可直接模仿的格式示例
    assert "- [用户] " in prompt and "- [建议] " in prompt


def test_attribution_labels_are_defined_and_never_justify_dropping_facts():
    prompt = _prompt()
    for label in ("`[用户]`", "`[建议]`", "`[共同]`"):
        assert label in prompt, f"归属标记缺失: {label}"
    # 归属判不准 → 只能标 [建议], 不许拿 [共同] 兜底; 也不是删条目
    assert "判不准归属时只能用" in prompt
    assert "不是删掉这条事实的理由" in prompt
    assert "不许拿 `[共同]` 兜底" in prompt


def test_supersede_rule_keeps_the_superseded_value_marked():
    prompt = _prompt()
    assert "原为 X → 改为 Y" in prompt
    # 旧值必须保留在同一行, 示例也得守自己的格式 (状态词在 [#N] 之前)
    assert "旧值保留在这一行里" in prompt
    assert "原为 X → 改为 Y〔已实施〕[#N]" in prompt


def test_task_scope_is_not_promoted_to_long_term_preference():
    prompt = _prompt()
    assert "一次性任务参数 (这次 / 这份 / 这个文件) 不记成长期偏好" in prompt
    # 反向约束: 用户的明确否定/肯定本身就是偏好, 不许按关键词过滤掉
    assert "不要求出现「以后 / 总是 / 一律」" in prompt


def test_uncertainty_is_preserved_instead_of_claimed():
    prompt = _prompt()
    assert "仅讨论" in prompt
    assert "不写「已完成」" in prompt


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


def test_identity_name_is_rendered_and_rules_stay_name_agnostic():
    prompt = _prompt(identity_name="小柚")
    assert prompt.startswith("你是小柚的「本轮变化观察者")
    # 规则块本身不写死身份名 — 换了身份名不该出现半替换的文案
    block = prompt[prompt.index("## 证据归属与状态"):prompt.index("## 时间锚点")]
    assert "小柚" not in block


def test_relationship_definition_uses_the_identity_name_not_a_hardcoded_pronoun():
    prompt = _prompt(identity_name="柚")
    assert "用户与「柚」的关系定义变化" in prompt
    assert "用户与「我」的关系定义变化" not in prompt


def test_custom_dimension_without_prompt_has_no_stray_period():
    dims = {"x": {"section": "自定义节"}}
    prompt = _prompt(dims=dims)
    assert "- 自定义节: **仅在本段 QA" in prompt
    assert "自定义节。" not in prompt


def test_collapse_helper_caps_runaway_custom_guidance():
    text = "这里写很长的用户指导语。" * 40
    collapsed = observer_mod._collapse_dim_prompt(text)
    assert len(collapsed) <= 121
    assert collapsed.endswith("…")


def test_status_markers_are_not_inherited_from_the_previous_note():
    # 上一版印就摆在 prompt 里且带状态词, 不写死"不继承"模型会顺手把〔计划中〕升格成〔已实施〕
    prompt = _prompt()
    assert "不从上一版印继承" in prompt
    assert "没有新证据就照写" in prompt


def test_preference_boundary_keeps_explicit_dislike_as_a_preference():
    prompt = _prompt()
    assert "我不喜欢这种写法" in prompt
    assert "不要求出现「以后 / 总是 / 一律」" in prompt


def test_decisions_section_is_written_even_when_there_is_only_a_proposal():
    prompt = _prompt()
    assert "只要本段出现拍板、提案或纠正之一, 本节就要写" in prompt
