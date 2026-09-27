# -*- coding: utf-8 -*-
"""E1「系统态势总览」生成产物的输出契约校验器（共享，纯函数，无 I/O）。

背景（P0，2026-09-21）
---------------------
E1 定时任务生成 ``situation_overview.md``（"系统态势总览"）。该文件被 prefetch
**逐字注入每个新 session** 的上下文（固定 ``## 系统态势`` 块），因此一次生成越界
= 污染所有后续对话。

真实事故形态：LLM 越过了输出契约、逃出 chat 模板，吐出一个新的 assistant turn
并自称另一个模型名。写路径当时**唯一的闸门是非空**，于是坏产物经 ``os.replace``
原子覆盖了上一份好文件 —— 结构性污染被持久化。

本模块把「输出契约」从 prompt 里的一句口号变成**可执行、可复用的校验器**，
写侧（``e1.py``）与读/注入侧（``__init__.py::_read_situation_overview``）共用
同一份实现，两侧口径不可能漂移。

契约原文（``e1.py`` 的 ``SITUATION_OVERVIEW_PROMPT``）：:

    【结构化输出契约】
    - 三段顺序固定："## 活跃主题" → "## 当前关注与待办" → "## 状态速记"。
    - 字数 300-500 字（含标点）。
    - 第三人称（"系统"/"近期"/"上次"，不要用"我"）。

同级产物：``identity_block.md``（``validate_identity_block``，2026-09-21 同批补齐）
--------------------------------------------------------------------------------
``identity_block.md`` 是同一缺陷类的第二个注入面（同一 ``e1.py`` 写路径、同一
provider、同一 chat 模板、同样被**逐字注入每个新 session**），此前同样是"非空即
原子写入 + 读侧零校验"。本模块对两者共用**同一套规则族**，但**不共用同一份契约**：

======================  ==========================  ==============================
规则族                  态势总览                    身份块
======================  ==========================  ==============================
长度天花板              1500 字 / 4500 B            1500 字 / 4500 B（同一定标）
结构（固定标题）        必需三段、顺序固定          **不适用** —— 身份块是自由散文，
                                                    无固定标题，强行要求标题会误杀
模板/provider 标记      拦截                        拦截（同一份 shape）
角色行 / 模型自报       拦截                        拦截（自报规则按身份语义收窄）
第二轮确认话术          拦截                        拦截（同一份 shape，收窄 meta 族）
======================  ==========================  ==============================

**为什么身份块不套用三段标题**：身份块的 prompt（``IDENTITY_SHORT_PROMPT``）只
要求"三个维度按顺序组织、可分三段、不要编号列表"，从未规定标题文本；生产实测
的合法产物是**五段自由第一人称中文**。拿态势总览的标题契约去卡它 = 把合法产物
全部拒掉（功能被弄死，而不是变安全）。

**为什么身份块的自报规则更窄**：身份块的内容**本身就是第一人称自我描述**，
"我是 <拉丁名>" 在身份块里是**合法语义**（例如自述运行身份），所以裸
``我是 + 拉丁词`` 在态势总览里可疑、在身份块里不可疑。身份块只拦**模型身份
声明**这一形态：问候语 + 自报（``你好！我是 X``），或自报后紧跟**模型类别词**
（语言模型 / 大模型 / LLM / 由…开发/训练）。裸自述放行 —— 这是刻意的取舍，
见 ``tests/test_e1_identity_block_boundary.py`` 的过拒守卫。

设计原则：**fail-closed**。校验器只做"接受 / 拒绝"，从不截断、从不猜结束位置、
从不保留"开头看起来还行"的前缀 —— 半个坏产物仍然是坏产物。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# 契约常量
# ────────────────────────────────────────────────────────────────────────

REQUIRED_SECTIONS: tuple[str, ...] = (
    "## 活跃主题",
    "## 当前关注与待办",
    "## 状态速记",
)
"""三段固定顺序的 level-2 标题（含 ``## `` 前缀，与产物逐字比对）。"""


# ────────────────────────────────────────────────────────────────────────
# 硬长度上限 —— 数字来自**生产实测**，不是拍脑袋
# ────────────────────────────────────────────────────────────────────────
#
# ⚠️ 2026-09-21 重新定标：初版取 600 字，其依据是"600 < 事故产物 651 字"。
# 该依据已被生产数据推翻 —— 模型对这份 prompt 的真实输出**稳定超过 600**，
# 600 会把一半的合法产物拒掉（把功能弄死，而不是变安全）。
#
# 取证（真实 MiniMax 调用，14 个样本：PHASE 18 五连 + 定标六连 + 事故当晚两件）：
#   * 合法产物实测区间 = **451-825 字 / 1206-2109 B**（p50≈570 字）
#   * 事故产物        = **627 字 / 1427 B**（三段短"暂无新数据" + 模板标记 + 伪造 turn）
#   * 6 次定标采样中，597/556/599 通过旧限 600，**709/615 被误拒** → 假拒率 50%
#
# 关键结论：**事故产物比合法产物更短**，长度维度上两者不可分。
# 因此长度闸门**不承担识别越界续写的职责**，只做"跑飞"兜底；
# 真正的防线是结构（规则 3）、模板标记（规则 4）、角色边界（规则 5）。
# 事故产物是被规则 4 拦下的（reason=TEMPLATE_MARKER），不是长度。
#
# 取值：
#   * ``_MAX_CHARS = 1500`` —— 观测上界 825 字的 ~1.8 倍余量：任何正常产物
#     都不可能被误拒；而"模型写完后继续生成第二个回合"这种跑飞（通常再叠
#     400-800 字）大概率越界，作为兜底生效。
#   * ``_MAX_BYTES = 4500`` —— 825 字纯中文 ≈ 2109 B，取 ~2.1 倍余量；
#     同时兜住 4 字节字符（emoji / 罕见汉字扩展区）灌水。
#
# 下限决策：**不设 300 字下限**。理由是证据驱动的 ——
#   * 真实事故文件里"三段各一句短'暂无新数据'"本身是**合法**产物（prompt 明确
#     要求"输入为空时如实写'暂无新数据'"），设下限会误杀它；
#   * 契约里"短"的兜底是**语义**的（三段标题都在、都有内容），不是数字的。
# 因此非空即通过长度下限；结构规则（3）（4）（5）承担主要防御。
_MAX_CHARS = 1500
_MAX_BYTES = 4500

# 契约目标区间 —— 仅供调用方/测试引用，校验器本身不对它做拒绝
TARGET_MIN_CHARS = 300
TARGET_MAX_CHARS = 500


# ────────────────────────────────────────────────────────────────────────
# identity_block 的硬长度上限（同一定标，同一理由）
# ────────────────────────────────────────────────────────────────────────
#
# 与态势总览共用同一组上限，依据：
#   * 两者的 prompt 契约**是同一句话**：300-500 字、同 provider、同 chat 模板；
#   * 生产实测合法身份块 = **616 字 / 1695 B**（五段自由第一人称中文，2026-09-21
#     父代理独立核实）。1500 字 = 实测值的 ~2.4 倍余量、4500 B = ~2.65 倍余量 ——
#     合法产物不可能被误拒；
#   * 同时兜住"模型写完 500 字后继续生成第二个回合"（通常再叠 400-800 字）。
#
# ⚠️ **下限依然是"非空"**：身份块 prompt 的硬约束是 300-500 字，但生产实测的合法
# 产物已经**超出**该区间（616 字）。任何按 300-500 字卡的下限都会把当前生产文件
# 判成非法（读侧会把合法身份块从注入里摘掉）—— 这正是"把功能弄死，而不是变安全"。
# 长度维度上"合法 616 字"与"跑飞产物"同样不可分，识别越界靠标记 / 角色边界 /
# 续写话术三条形状规则，长度闸只做跑飞兜底。
_IDENTITY_MAX_CHARS = 1500
_IDENTITY_MAX_BYTES = 4500

# 契约目标区间（仅供调用方/测试引用，不参与拒绝）
IDENTITY_TARGET_MIN_CHARS = 300
IDENTITY_TARGET_MAX_CHARS = 500


# ────────────────────────────────────────────────────────────────────────
# 规则 4：chat 模板 / provider 控制标记
# ────────────────────────────────────────────────────────────────────────
#
# 全部写成**形状（shape）**而不是字面量，这样新 provider 的同形标记无需改代码
# 就能被拦住：
#   1. ``<|im_start|>`` / ``<|end|>`` / ``<|assistant|>`` … 任意 ``<|name|>``；
#   2. ``]<]minimax[>[`` —— 本次事故的形态；``]<]`` + 任意 provider 名 + ``[>[``。
#      名字部分用 ``[^\]\n]{0,64}?`` 兜住，所以 ``]<]someotherprovider[>[``
#      这类**不在任何字面清单里**的新标记同样命中；
#   3. ``<<SYS>>`` / ``<</SYS>>`` 任意 ``<<name>>``；
#   4. ``[INST]`` / ``[/INST]`` 以及同族全大写方括号 tag（≥3 个字符，
#      避免误伤 ``[PG]``/``[E1]``/``[QA]`` 这类 2 字符系统缩写）。
_TEMPLATE_MARKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pipe_token",
        re.compile(r"<\|[A-Za-z_][A-Za-z0-9_.\-]{0,31}\|>"),
    ),
    (
        "bracket_marker",
        re.compile(r"\]\s*<\s*\]\s*[^\]\n]{0,64}?\[\s*>\s*\["),
    ),
    (
        "double_angle",
        re.compile(r"<<\s*/?\s*[A-Za-z_][A-Za-z0-9_.\-]{0,31}\s*>>"),
    ),
    (
        "bracket_tag",
        re.compile(r"\[/?(?:[A-Z][A-Z0-9_]{2,15})\]"),
    ),
)

# 规则 4 里"裸行标记"：整行就是标记（常见于模板逃逸的收尾/开场）
_MARKER_LINE_RE = re.compile(r"(?m)^[ \t]*(?:\]<\]|\[\]>|<\||\|>)")


# ────────────────────────────────────────────────────────────────────────
# 规则 5：角色边界（解析器，不是子串搜索）
# ────────────────────────────────────────────────────────────────────────
#
# 只认「行首 + 英文角色词 + 冒号 + 有正文」这一形状。
# 中文散文里的 系统 / 用户 一律**不**触发（它们在句中，且没有 ASCII 角色名 +
# 行首冒号这个组合）；英文摘要句中出现的 'system' 同理不触发。
_ROLE_TURN_RE = re.compile(
    r"(?m)^[ \t>*\-•·]*"
    r"(?:assistant|user|system|tool|human|ai)"
    r"[ \t]*[:：][ \t]*\S",
    re.IGNORECASE,
)

# 第一人称自报模型身份 —— 通用形状，不是 'Qwen' 字面量：
#   中文/英文「问候 + 我是/我叫/I'm/I am + 名字」
_SELF_ID_GREETING_RE = re.compile(
    r"(?:你好|您好|大家好|嗨|哈喽|哈啰|hello|hi|hey|greetings)"
    r"[ \t]*[，,、!！.。:：\-]*[ \t]*"
    r"(?:我(?:是|叫|名叫|的名字是)|i[ \t]*(?:'m|am))"
    r"[ \t]*[^\s，。！？,.!?\n]{1,32}",
    re.IGNORECASE,
)
# 无问候的裸自报（"我是 Qwen" / "I am Claude"）：名字段必须是拉丁字母开头，
# 否则中文散文里的"我是……"会大面积误伤（本规则只针对**模型身份声明**）。
_SELF_ID_MODEL_RE = re.compile(
    r"(?:我(?:是|叫)|i[ \t]*(?:'m|am))[ \t]*[A-Za-z][A-Za-z0-9._\-]{1,31}",
    re.IGNORECASE,
)

# 身份块专用：**模型身份声明**（自报 + 模型类别词）。
#
# 身份块的内容本身就是第一人称自我描述，所以裸 "我是 <拉丁名>" 不能当越界证据
# （自述运行身份是合法语义）。真正可疑的是"自称另一个模型"这一形态 ——
# 自报之后紧跟模型类别词，或自称由某方开发/训练：
#   "我是 Qwen，一个大型语言模型。"      → 拒绝
#   "我是 Claude，由 Anthropic 开发。"   → 拒绝
#   "我是 Hermes，负责与用户长期协作。"   → 放行（裸自述，合法身份语义）
_MODEL_SELF_DESC_RE = re.compile(
    r"(?:我(?:是|叫)|i[ \t]*(?:'m|am))[ \t]*[A-Za-z][A-Za-z0-9._\-]{1,31}"
    r"[^。！？\n]{0,24}?"
    r"(?:语言模型|大模型|LLM|language[ \t]+model|"
    r"由[^。！？\n]{0,12}?(?:开发|训练|推出|研发))",
    re.IGNORECASE,
)


# ────────────────────────────────────────────────────────────────────────
# 规则 6：第三段之后的"第二轮对话"（启发式，模糊时**故意 fail-open**）
# ────────────────────────────────────────────────────────────────────────
#
# 本规则全部是启发式：只要能构造出合法中文散文反例，就选择**不拒绝**。
# 主要防御是规则 3（结构）与规则 4（模板标记）—— 它们不依赖语义猜测。
#
# 两个 pattern 拆成**具名对象**，因为身份块要复用同一形状、但收窄 meta 族
# （身份块本身就是"我作为 AI/系统"式的自我描述，"作为 AI" 在那里不是收尾话术）。
_CONFIRMATION_REQUEST_RE = re.compile(
    r"(?:是否需要|需要我|要我(?:继续|帮|做|补|来)|"
    r"请(?:确认|回复|告知|告诉|提供|选择)|"
    r"\b(?:let me know|would you like|do you want|shall i|should i|want me to)\b)",
    re.IGNORECASE,
)
_META_DIALOGUE_RE = re.compile(
    r"(?:以上(?:是|为)|希望对(?:你|您)(?:有)?帮助|如需(?:要)?|供(?:你|您)参考|"
    r"作为(?:一个)?(?:AI|人工智能)|"
    r"\b(?:i hope this helps|as an ai|let me know if|feel free to ask)\b)",
    re.IGNORECASE,
)
# 身份块版本：去掉 "作为 AI"（身份块里那是合法自我描述，不是收尾话术）。
_META_DIALOGUE_IDENTITY_RE = re.compile(
    r"(?:以上(?:是|为)|希望对(?:你|您)(?:有)?帮助|如需(?:要)?|供(?:你|您)参考|"
    r"\b(?:i hope this helps|let me know if|feel free to ask)\b)",
    re.IGNORECASE,
)

_CONTINUATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("confirmation_request", _CONFIRMATION_REQUEST_RE),
    ("meta_dialogue", _META_DIALOGUE_RE),
)

# 身份块的续写规则族：同一 confirmation 形状 + 收窄的 meta 族。
_IDENTITY_CONTINUATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("confirmation_request", _CONFIRMATION_REQUEST_RE),
    ("meta_dialogue", _META_DIALOGUE_IDENTITY_RE),
)
# 面向读者的提问：行尾是问号 **且** 该行有第二人称。
# 两个条件同时满足才拒绝 —— 单看问号会把合法的"待办疑问句"误杀。
_QUESTION_TAIL_RE = re.compile(r"[？?][ \t]*$")
_SECOND_PERSON_RE = re.compile(r"(?:你|您|\byou\b)", re.IGNORECASE)


class GeneratedContextError(ValueError):
    """生成产物未通过输出契约。

    Attributes:
        reason: 稳定的机器可读类别（见模块 docstring 与 README 枚举）。
        detail: 人类可读的补充（命中位置、标题名、计数等），可为空。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _reject(reason: str, detail: str = "") -> "GeneratedContextError":
    return GeneratedContextError(reason, detail)


def _check_length(
    text: str,
    *,
    max_chars: int = _MAX_CHARS,
    max_bytes: int = _MAX_BYTES,
    target_min: int = TARGET_MIN_CHARS,
    target_max: int = TARGET_MAX_CHARS,
) -> None:
    """规则 2：硬长度天花板（字数 + 字节双闸）。

    默认参数 = 态势总览的定标；身份块传入自己的常量（数值相同，但**具名**，
    这样任一侧重新定标时不会静默串味）。
    """
    n_chars = len(text)
    n_bytes = len(text.encode("utf-8", errors="replace"))
    if n_chars > max_chars:
        raise _reject(
            "LENGTH_TOO_LONG",
            f"{n_chars} 字 > 硬上限 {max_chars} 字（契约目标 {target_min}-{target_max} 字）",
        )
    if n_bytes > max_bytes:
        raise _reject(
            "LENGTH_TOO_LONG",
            f"{n_bytes} B > 硬上限 {max_bytes} B（{n_chars} 字）",
        )


def _check_sections(lines: list[str]) -> int:
    """规则 3：三段结构。返回第一段标题所在行号。"""
    headings: list[tuple[int, str]] = [
        (i, line.strip()) for i, line in enumerate(lines) if line.strip().startswith("## ")
    ]
    positions: dict[str, list[int]] = {}
    for idx, heading in headings:
        if heading in REQUIRED_SECTIONS:
            positions.setdefault(heading, []).append(idx)

    missing = [s for s in REQUIRED_SECTIONS if s not in positions]
    if missing:
        raise _reject("SECTION_MISSING", "缺少必需标题: " + ", ".join(missing))

    duplicated = [s for s in REQUIRED_SECTIONS if len(positions[s]) > 1]
    if duplicated:
        raise _reject("SECTION_DUPLICATED", "必需标题重复: " + ", ".join(duplicated))

    extras = [h for _, h in headings if h not in REQUIRED_SECTIONS]
    if extras:
        raise _reject("UNEXPECTED_SECTION", "出现契约外的 level-2 标题: " + ", ".join(extras))

    first_line_of = [positions[s][0] for s in REQUIRED_SECTIONS]
    if first_line_of != sorted(first_line_of):
        raise _reject(
            "SECTION_OUT_OF_ORDER",
            "标题顺序为 " + " → ".join(
                sorted(REQUIRED_SECTIONS, key=lambda s: positions[s][0])
            ),
        )

    first_idx = first_line_of[0]
    preamble = "\n".join(lines[:first_idx]).strip()
    if preamble:
        raise _reject(
            "PREAMBLE_BEFORE_FIRST_SECTION",
            f"首个必需标题前有 {len(preamble)} 字非空内容: {preamble[:60]!r}",
        )
    return first_idx


def _check_template_markers(text: str) -> None:
    """规则 4：模板 / provider 控制标记。"""
    for name, pattern in _TEMPLATE_MARKER_PATTERNS:
        m = pattern.search(text)
        if m:
            raise _reject(
                "TEMPLATE_MARKER",
                f"命中 {name} 形状: {m.group(0)[:60]!r}",
            )
    m = _MARKER_LINE_RE.search(text)
    if m:
        raise _reject(
            "TEMPLATE_MARKER",
            f"裸标记行: {text[m.start():m.start() + 40]!r}",
        )


def _check_role_boundary(text: str) -> None:
    """规则 5：疑似新的 chat turn / 模型身份自报。"""
    m = _ROLE_TURN_RE.search(text)
    if m:
        raise _reject("ROLE_BOUNDARY", f"疑似角色行: {m.group(0)[:60]!r}")
    m = _SELF_ID_GREETING_RE.search(text)
    if m:
        raise _reject("ROLE_BOUNDARY", f"疑似第一人称身份自报: {m.group(0)[:60]!r}")
    m = _SELF_ID_MODEL_RE.search(text)
    if m:
        raise _reject("ROLE_BOUNDARY", f"疑似第一人称身份自报: {m.group(0)[:60]!r}")


def _check_identity_role_boundary(text: str) -> None:
    """规则 5（身份块版）：疑似新的 chat turn / **模型身份声明**。

    与态势总览共用角色行与"问候 + 自报"两条形状；**不**套用裸 ``我是 <拉丁词>``
    （身份块里那是合法的第一人称自我描述），改用 ``_MODEL_SELF_DESC_RE``
    （自报 + 模型类别词 / 自称由某方开发训练）。
    """
    m = _ROLE_TURN_RE.search(text)
    if m:
        raise _reject("ROLE_BOUNDARY", f"疑似角色行: {m.group(0)[:60]!r}")
    m = _SELF_ID_GREETING_RE.search(text)
    if m:
        raise _reject(
            "ROLE_BOUNDARY", f"疑似带问候的第一人称身份自报: {m.group(0)[:60]!r}"
        )
    m = _MODEL_SELF_DESC_RE.search(text)
    if m:
        raise _reject("ROLE_BOUNDARY", f"疑似模型身份声明: {m.group(0)[:60]!r}")


def _check_unexpected_continuation(
    tail: str,
    patterns: tuple[tuple[str, re.Pattern[str]], ...] = _CONTINUATION_PATTERNS,
) -> None:
    """规则 6：最后一节之后的第二轮对话。tail 为空时直接通过。

    ``patterns`` 可替换：身份块没有"最后一节"锚点，因此整篇都是 tail，并传入
    收窄过的 ``_IDENTITY_CONTINUATION_PATTERNS``。
    """
    if not tail.strip():
        return
    for name, pattern in patterns:
        m = pattern.search(tail)
        if m:
            raise _reject(
                "UNEXPECTED_CONTINUATION",
                f"命中 {name}: {m.group(0)[:60]!r}",
            )
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        if _QUESTION_TAIL_RE.search(line) and _SECOND_PERSON_RE.search(line):
            raise _reject(
                "UNEXPECTED_CONTINUATION",
                f"面向读者的提问: {line[:60]!r}",
            )


def validate_situation_overview(text: str) -> str:
    """校验一份 E1「系统态势总览」候选产物。

    Args:
        text: LLM 原始输出（写侧）或磁盘文件内容（读/注入侧）。

    Returns:
        通过校验的文本（已 strip）。

    Raises:
        GeneratedContextError: 任一条契约规则被违反。**绝不**返回部分内容 ——
            调用方必须整体丢弃候选并保留上一份已验证产物。

    纯函数：无 I/O、无全局状态；唯一的副作用是可选的 ``logger.debug``。
    """
    if text is None or not isinstance(text, str):
        raise _reject("EMPTY", f"非字符串输入: {type(text).__name__}")

    stripped = text.strip()
    if not stripped:
        raise _reject("EMPTY", "空 / 全空白产物")

    logger.debug(
        "[generated_context_contract] 校验态势总览: %d 字 / %d B",
        len(stripped),
        len(stripped.encode("utf-8", errors="replace")),
    )

    _check_length(stripped)

    lines = stripped.splitlines()
    first_idx = _check_sections(lines)

    _check_template_markers(stripped)
    _check_role_boundary(stripped)

    # 第三段之后的所有内容 = 潜在的"第二轮对话"落点
    third_idx = max(
        i for i, line in enumerate(lines) if line.strip() == REQUIRED_SECTIONS[-1]
    )
    _check_unexpected_continuation("\n".join(lines[third_idx + 1:]))

    logger.debug("[generated_context_contract] 通过: %d 字", len(stripped))
    return stripped


def validate_identity_block(text: str) -> str:
    """校验一份 E1「身份核心」（``identity_block.md``）候选产物。

    与 :func:`validate_situation_overview` 共用**同一套规则族**（长度天花板 /
    模板标记 / 角色边界 / 续写话术），但**不套用三段标题契约**：身份块是自由
    第一人称散文，prompt 从未规定标题文本，生产实测的合法产物是五段无标题中文。

    模型无关、纯函数、无 I/O；fail-closed —— 只做"接受 / 拒绝"，绝不截断。

    Args:
        text: LLM 原始输出（写侧）或磁盘文件内容（读/注入侧），也用于校验
            缺文件时的 legacy 印截断兜底。

    Returns:
        通过校验的文本（已 strip）。

    Raises:
        GeneratedContextError: 任一条规则被违反。调用方必须整体丢弃候选：
            写侧保留上一份已验证文件（没有则保持缺失），读侧返回空串且
            **不回退到更旧的缓存块**。
    """
    if text is None or not isinstance(text, str):
        raise _reject("EMPTY", f"非字符串输入: {type(text).__name__}")

    stripped = text.strip()
    if not stripped:
        raise _reject("EMPTY", "空 / 全空白产物")

    logger.debug(
        "[generated_context_contract] 校验身份核心: %d 字 / %d B",
        len(stripped),
        len(stripped.encode("utf-8", errors="replace")),
    )

    # 规则 2：长度天花板（身份块专属常量；**无** 300-500 字下限，理由见模块注释）
    _check_length(
        stripped,
        max_chars=_IDENTITY_MAX_CHARS,
        max_bytes=_IDENTITY_MAX_BYTES,
        target_min=IDENTITY_TARGET_MIN_CHARS,
        target_max=IDENTITY_TARGET_MAX_CHARS,
    )

    # 规则 3：**不适用** —— 身份块无固定标题，任何标题要求都会误杀合法产物。

    # 规则 4：模板 / provider 控制标记（与态势总览同一份 shape）
    _check_template_markers(stripped)

    # 规则 5：角色行 + 模型身份声明（身份块收窄版）
    _check_identity_role_boundary(stripped)

    # 规则 6：第二轮对话 / 面向读者的确认话术。身份块没有"最后一节"锚点，
    # 因此整篇都是潜在落点。
    _check_unexpected_continuation(stripped, _IDENTITY_CONTINUATION_PATTERNS)

    logger.debug("[generated_context_contract] 身份核心通过: %d 字", len(stripped))
    return stripped


__all__ = [
    "REQUIRED_SECTIONS",
    "GeneratedContextError",
    "validate_situation_overview",
    "validate_identity_block",
]
