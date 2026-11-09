"""捕获规则：全量通读（568 片 / 161,275 条标注）实测出来的缺陷回归。

每条都对应一个真实数据形态，数字来自 2026-11-09 的标注结果：
  * `_CRON_RE` 用 search 时，正文里**提到** `[cron:` 的分析消息被判成机器前言
    —— 实测误伤 39 条（A 16 / D 10 / E 13），包括柚自己写的过滤逻辑讨论。
  * `[SILENT]` 1,628 条、`ANNOUNCE_SKIP` 55 条、`Operation interrupted` 170 条
    这类确定性机器状态此前没有被拦。
  * 外壳消息里的真人正文（OOB 84 条、standing goal 47 条、skill 调用尾部的真人
    指令）此前要么整条丢，要么整段当提问。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v3core.importers import KIND_RAW, ImportItem  # noqa: E402
from v3core.importers.legacy_sources import (  # noqa: E402
    is_machine_preamble,
    unwrap_injected,
)
from v3core.importers.qa_pairing import pair_items  # noqa: E402


def _item(role: str, text: str, *, eid: str, ts: str) -> ImportItem:
    return ImportItem(
        kind=KIND_RAW,
        source_system="hermes",
        source_ref="hermes:s1",
        text=text,
        role=role,
        occurred_at=ts,
        host="hermes",
        event_id=eid,
    )


# ── 1. _CRON_RE 必须只在开头命中 ────────────────────────────────────────


def test_cron_marker_inside_body_is_not_machine_preamble():
    """柚分析 "[cron:] 怎么识别" 的那条 assistant 分析不是机器前言。"""
    body = "原因找到了：early 源格式是 `- 👤 [cron:...]`——👤 后没冒号，修一下。"
    assert is_machine_preamble(body) is False
    body2 = "查清了。**实际 34 条**。形态分三类：`[cron:...]` 定时任务 ~30 条。"
    assert is_machine_preamble(body2) is False


def test_cron_marker_at_start_is_machine_preamble():
    assert is_machine_preamble("[cron:0 4 * * *] 每天四点跑 E1") is True
    # OpenClaw 时代带时间戳前缀，剥掉后仍应命中
    assert is_machine_preamble("[Wed 2026-05-06 00:42 GMT+8] [cron:x] 跑维护") is True


# ── 2. 确定性机器状态必须拦 ──────────────────────────────────────────────


def test_deterministic_machine_markers():
    for text in (
        "[SILENT]",
        "ANNOUNCE_SKIP",
        "Operation interrupted.",
        "Operation interrupted: waiting for model response (160.1s elapsed).",
        "[ASYNC DELEGATION BATCH COMPLETE — deleg_15e13189] results below",
        "[STILL IN PROGRESS — this is the active request, restated after the compaction",
        "You just executed tool calls but returned an empty response. Please process",
        "Your previous final response was rejected by the output contract validator.",
        "[System: The active model for this chat has changed to deepseek-v4.1-flash",
    ):
        assert is_machine_preamble(text) is True, text


def test_real_user_sentence_still_passes():
    for text in (
        "你说的那个判官prompt是不是混进qa了",
        "好",
        "先别动，我看看",
        "把 4 个技能改写为情景→步骤→完的主文草稿。",
    ):
        assert is_machine_preamble(text) is False, text


# ── 3. 外壳剥壳 ─────────────────────────────────────────────────────────


def test_out_of_band_wrapper_keeps_user_text():
    raw = (
        "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at "
        "this position; not tool output and not a new delivery when replayed from "
        "conversation history] 你这要这轮回复不结束就一直触发不了吧 [/OUT-OF-BAND USER MESSAGE]"
    )
    body, kind = unwrap_injected(raw)
    assert kind == "out_of_band"
    assert body == "你这要这轮回复不结束就一直触发不了吧"


def test_standing_goal_keeps_goal_text():
    raw = (
        "[Continuing toward your standing goal] Goal: 把迁移跑完，不许动生产库。 "
        "Continue working toward this goal. Take the next concrete step. If you believe "
        "the goal is complete, state so explicitly and stop."
    )
    body, kind = unwrap_injected(raw)
    assert kind == "standing_goal"
    assert body == "把迁移跑完，不许动生产库。"


def test_skill_invocation_keeps_instruction_but_shell_alone_is_dropped():
    with_instruction = (
        "[IMPORTANT: The user has invoked the \"plan\" skill, indicating they want you to "
        "follow its instructions. The full skill content is loaded below.]\n\n"
        "# Plan\n...\n\nThe user has provided the following instruction alongside the "
        "skill invocation: 同意你的核心结论，短期记忆压缩是短板。出一个方案吧"
    )
    body, kind = unwrap_injected(with_instruction)
    assert kind == "skill_invocation"
    assert body.startswith("同意你的核心结论")

    shell_only = (
        "[IMPORTANT: The user has invoked the \"linuxdo-digest\" skill, indicating they "
        "want you to follow its instructions. The full skill content is loaded below.]\n\n"
        "name: linuxdo-digest\n..."
    )
    body2, kind2 = unwrap_injected(shell_only)
    assert kind2 == "skill_invocation"
    assert body2 == ""


def test_plain_text_is_not_a_wrapper():
    assert unwrap_injected("今天把重建跑完了") is None


# ── 4. 配对层：剥壳后的问题才是真问题 ────────────────────────────────────


def test_pairing_uses_unwrapped_user_text():
    raw = (
        "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at "
        "this position] 别动生产库 [/OUT-OF-BAND USER MESSAGE]"
    )
    items = [
        _item("user", raw, eid="1", ts="2026-11-09T10:00:00+00:00"),
        _item("assistant", "收到，只读。", eid="2", ts="2026-11-09T10:00:05+00:00"),
    ]
    pairs = pair_items(items)
    assert len(pairs) == 1
    assert pairs[0].question == "别动生产库"


def test_pairing_drops_pure_skill_shell():
    """只有 skill 全文、没有真人指令的注入不产生 QA。"""
    shell = (
        "[IMPORTANT: The user has invoked the \"forum-intelligence\" skill, indicating "
        "they want you to follow its instructions. The full skill content is loaded below.]\n\n"
        "name: forum-intelligence\n..."
    )
    items = [
        _item("user", shell, eid="1", ts="2026-11-09T10:00:00+00:00"),
        _item("assistant", "[SILENT]", eid="2", ts="2026-11-09T10:00:05+00:00"),
    ]
    assert pair_items(items) == []
