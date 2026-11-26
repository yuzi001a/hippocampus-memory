# -*- coding: utf-8 -*-
"""印的机械契约 + 身份块结尾完整性回归 (2026-11-26)。

两个真实缺陷:
1. 全量补织把印写成项目清单，底色特质被淹没 —— 契约要能拦；
   同时叙事体（空行分段的整段话）必须能通过，不能被"数短横线"判 0 条。
2. 一份 ~100 字、断在「……因为」的身份块通过了全部校验，从 16:53 起被每个
   新会话注入 —— 长度闸门不管"没写完"，要单独判结尾。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v3core import e1  # noqa: E402
from v3core.generated_context_contract import (  # noqa: E402
    GeneratedContextError,
    validate_identity_block,
)

_FILLER = ("这一条讲的是习惯而不是某个任务的处置办法：换一个场景、换一个人来问，它照样成立；"
           "真正会变的是具体处境和手上的工具，不会变的是我在这里划的那条线。"
           "我吃过把具体做法当成原则的亏，也见过别人这么做之后付出的代价，所以宁可写得慢一点，"
           "也要把线划在能迁移的那一层上，让下一次遇到同类问题时还用得上。"
           "这条线不是一次定下来的，是每次踩坑之后往前挪一点，挪到现在这个位置。"
           "挪的过程里我学到的另一件事是：写清楚一条边界比写十条注意事项有用，因为边界能被检验，"
           "注意事项只能被记住。")


def _bullet_yin() -> str:
    """条目体：每条一行、连续无空行（旧格式，必须继续兼容）。"""
    items = [
        ("我是谁", "注意力压在「模型输出能不能被工程约束住」这一类问题上：契约、严格 schema、通道形态、判据(来源: 以代码约束模型输出)"),
        ("我是谁", "守着自家整套基础设施：网络出口、远程卡死、NAS 登记、硬盘功耗，不让任何一层成为黑箱(来源: 多网络出口优化)"),
        ("我是谁", "把「先读懂材料」当成独立阶段：档案先分层、先通读再决定灌不灌(来源: 导出档案的定位与消化方式)"),
        ("我是谁", "对内容成品有规范与审美双重标准：栏目框架定死、视觉收敛，不合规就是没做完(来源: 周刊采编框架)"),
        ("我是谁", "愿意做枯燥的验证活，但厌恶汇报腔，要口语直说、说人话(来源: 与用户沟通风格)"),
        ("怎么判断", "遇到陌生问题先读源码、找真实现场，不先猜(来源: 主题卡提示词实验)"),
        ("怎么判断", "动手前先只读盘点，划出边界，不把侦察和动手混在一起(来源: 记忆重建质量评估规格)"),
        ("怎么判断", "严格区分声明与执行：配置里写了不等于生效(来源: 召回权重实证)"),
        ("怎么判断", "用实测推翻旧判断，并显式标注「已推翻」(来源: 观察者提示词重写实验)"),
        ("怎么判断", "决策优先最小变量与可回滚，先想退路再想收益(来源: 隔离库重跑前置条件)"),
        ("行为基线", "不在生产和基础设施上擅自动手，重启与切换都要取得同意(来源: 服务健康巡检)"),
        ("行为基线", "改动前先留退路：dump、备份、原库保留，备份完当场验证(来源: 派生层搬库回滚三件套)"),
        ("行为基线", "写入必须带可核实来源，无来源记缺口而不是编一个(来源: 证据归属与状态识别)"),
        ("行为基线", "验收看真实路径跑通与行为可观察(来源: 事件通道验收)"),
        ("行为基线", "参数按实测分布定、时间报北京时间、额度按计划用完(来源: 观察者回放与订阅额度调度)"),
    ]
    out, cur = [], None
    for sec, line in items:
        if sec != cur:
            out.append("\n## " + sec + "\n")
            cur = sec
        out.append("- " + line + _FILLER)
    return "\n".join(out).strip() + "\n"


def _narr_yin() -> str:
    """叙事体：空行分段的整段话（新格式）。"""
    blocks = [
        ("我是谁", "我最早盯的是模型听不听话，后来发现那是伪命题：真正要管的是输出能不能被代码挡住。" + _FILLER),
        ("我是谁", "我一直守着自家那套基础设施，因为任何一层变黑箱，后面所有判断都要重新称重。" + _FILLER),
        ("我是谁", "我先读懂材料再决定灌不灌，这个习惯是整包倒入吃过大亏之后才改的。" + _FILLER),
        ("我是谁", "成品我按规范和审美两把尺子量，版式不合规就是没做完。" + _FILLER),
        ("我是谁", "我说话直，讨厌汇报腔；跳过和做完一样要报。" + _FILLER),
        ("怎么判断", "遇到陌生问题先读源码找现场，推断只用来提假设。" + _FILLER),
        ("怎么判断", "动手前只读盘点划边界，不把侦察和动手混在一起。" + _FILLER),
        ("怎么判断", "声明和执行分两件事，没拿到真实请求体就不算生效。" + _FILLER),
        ("怎么判断", "我允许实测推翻自己，并且要求把推翻写出来。" + _FILLER),
        ("怎么判断", "决策先问能不能局部改、能不能回滚、污染面多大。" + _FILLER),
        ("行为基线", "生产和基础设施上不擅自动手，等同意再动。" + _FILLER),
        ("行为基线", "改动前先留退路，备份完当场解开验证。" + _FILLER),
        ("行为基线", "写入必须带可核实来源，查不到就记缺口。" + _FILLER),
        ("行为基线", "验收看真实路径跑通，不看「改完了」。" + _FILLER),
        ("行为基线", "时间报北京时间，参数按实测分布定，额度按计划用完。" + _FILLER),
    ]
    out, cur = [], None
    for sec, text in blocks:
        if sec != cur:
            out.append("\n## " + sec + "\n")
            cur = sec
        out.append(text + "\n")
    return "\n".join(out).strip() + "\n"


def test_bullet_yin_passes():
    v = e1._validate_yin_content(_bullet_yin())
    assert v["ok"] is True, v["reasons"]
    assert len(v["anchors"]) >= 10


def test_narrative_yin_passes():
    v = e1._validate_yin_content(_narr_yin())
    assert v["ok"] is True, v["reasons"]
    assert len(v["anchors"]) >= 10


def test_narrative_blocks_counted_not_bullets():
    """叙事版没有短横线，仍要按段落数出来 —— 否则被自己的闸门判 0 条。"""
    text = _narr_yin()
    assert "- " not in text
    v = e1._validate_yin_content(text)
    assert not any("只有 0 条" in r for r in v["reasons"]), v["reasons"]


def test_project_list_rejected():
    body = _narr_yin().replace("守着自家那套基础设施", "跟进 P0-B3 与 P0-B5 的合并")
    body = body.replace("我说话直，讨厌汇报腔；跳过和做完一样要报", "推进 v4.0.3 与 H18R 的试用包")
    v = e1._validate_yin_content(body + "\n- 时间换算 v4.0.1 P0-B3 H18R v2.2.2\n")
    assert v["ok"] is False
    joined = "; ".join(v["reasons"])
    assert ("项目清单味" in joined or "底色特质" in joined
            or "核心特质覆盖" in joined), joined


def test_short_yin_is_rejected():
    v = e1._validate_yin_content("## 我是谁\n- 一句话(来源: x)\n")
    assert v["ok"] is False
    assert any("过短" in r or "小节" in r for r in v["reasons"])


def test_project_tokens_ignored_inside_source_marks():
    """来源标注里的项目名是溯源，不算项目味。"""
    body = _narr_yin().replace(
        "(来源: 多网络出口优化)", "(来源: P0-B3 v4.0.3 H18R 多网络出口优化)")
    v = e1._validate_yin_content(body)
    assert v["ok"] is True, v["reasons"]


def test_truncated_identity_rejected():
    """真实事故产物：断在「……因为」。"""
    broken = ("我是站在运维者位置上的人，不是使用者。我和用户的关系不是交付功能，"
              "而是守住边界：东西怎么被拉起来、资源归谁、出事谁先知道、日志落在哪，"
              "这些含糊了，功能再多我也不认。我长期盯的是基础层的确定性和可回滚性，因为")
    try:
        validate_identity_block(broken)
    except GeneratedContextError as e:
        assert e.reason == "TRAILING_INCOMPLETE", e.reason
    else:
        raise AssertionError("截断的身份块竟然通过了")


def test_short_but_complete_identity_passes():
    """短不等于坏 —— 「暂无新数据。」是合法产物。"""
    assert validate_identity_block("暂无新数据。").endswith("。")


def test_complete_identity_passes():
    text = ("我是柚。我做事的顺序是先划边界再动手，边界没划清之前不碰生产。"
            "我看重可回滚，改动前先留退路；也看重可核实，写入必须带来源。"
            "说话直，跳过和做完一样要报。")
    assert validate_identity_block(text)
