"""长链任务配对：过程 vs 结论。

用户定的语义：长链任务里 assistant 的中间行是「过程」，只有最后一轮
是「结论」，记忆里要留的是结论。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v3core.importers import KIND_RAW, ImportItem  # noqa: E402
from v3core.importers.qa_pairing import pair_items  # noqa: E402


def _item(role: str, text: str, *, eid: str, ts: str, tools: bool = False) -> ImportItem:
    return ImportItem(
        kind=KIND_RAW,
        source_system="hermes",
        source_ref="hermes:s1",
        text=text,
        role=role,
        occurred_at=ts,
        host="hermes",
        event_id=eid,
        provenance={"tool_calls": '[{"name": "read_file"}]'} if tools else None,
    )


def test_tool_chain_keeps_only_final_answer():
    """中间行带 tool_calls，是工具调用前的旁白 —— 丢弃；只留最后的纯文本结论。"""
    items = [
        _item("user", "把重建跑完", eid="1", ts="2026-11-08T10:00:00+00:00"),
        _item("assistant", "我先看一下仓库状态。", eid="2", ts="2026-11-08T10:00:05+00:00", tools=True),
        _item("assistant", "现在开始导入。", eid="3", ts="2026-11-08T10:01:00+00:00", tools=True),
        _item("assistant", "跑完了：191,961 条消息、19,638 对 QA。", eid="4", ts="2026-11-08T10:30:00+00:00"),
    ]
    pairs = pair_items(items)
    assert len(pairs) == 1
    assert pairs[0].answer == "跑完了：191,961 条消息、19,638 对 QA。"


def test_multipart_reply_without_tool_calls_still_merges():
    """没有 tool_calls 的多行 = 同一条回复被宿主拆段，必须照旧合并。"""
    items = [
        _item("user", "解释一下", eid="1", ts="2026-11-08T10:00:00+00:00"),
        _item("assistant", "第一段。", eid="2", ts="2026-11-08T10:00:05+00:00"),
        _item("assistant", "第二段。", eid="3", ts="2026-11-08T10:00:06+00:00"),
    ]
    pairs = pair_items(items)
    assert len(pairs) == 1
    assert "第一段" in pairs[0].answer and "第二段" in pairs[0].answer


def test_adjacent_duplicate_rows_collapse():
    """宿主会把同一条消息重复落库（实测占 23%），相邻重复必须折叠。"""
    items = [
        _item("user", "继续", eid="1", ts="2026-11-08T10:00:00+00:00"),
        _item("assistant", "做完了。", eid="2", ts="2026-11-08T10:00:05+00:00"),
        _item("assistant", "做完了。", eid="3", ts="2026-11-08T10:00:05+00:00"),
        _item("assistant", "做完了。", eid="4", ts="2026-11-08T10:00:05+00:00"),
    ]
    pairs = pair_items(items)
    assert len(pairs) == 1
    assert pairs[0].answer == "做完了。"


def test_non_adjacent_repeats_are_real_turns():
    """非相邻的重复是真实的第二次发言（「继续」说两遍），不能折叠。"""
    items = [
        _item("user", "继续", eid="1", ts="2026-11-08T10:00:00+00:00"),
        _item("assistant", "第一步做完了。", eid="2", ts="2026-11-08T10:00:05+00:00"),
        _item("user", "继续", eid="3", ts="2026-11-08T10:05:00+00:00"),
        _item("assistant", "第二步做完了。", eid="4", ts="2026-11-08T10:05:05+00:00"),
    ]
    pairs = pair_items(items)
    assert len(pairs) == 2
