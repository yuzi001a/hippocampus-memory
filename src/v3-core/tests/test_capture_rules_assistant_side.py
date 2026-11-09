"""捕获规则回归：assistant 侧的机器状态不能成为答案。

全量通读实测（2026-11-09）：`[SILENT]` 1,628 条、`Operation interrupted` 170 条、
`[ASYNC DELEGATION BATCH COMPLETE …]` 770 条都以 assistant 落库。生产管线只在
user 侧做机器前言过滤，这些会直接变成 QA 的答案。

判据是结构性的（固定前缀），不涉及内容长度或中文关键词。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v3core.importers import KIND_RAW, ImportItem  # noqa: E402
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


def test_silent_answer_never_becomes_qa():
    items = [
        _item("user", "[IMPORTANT: You are running as a scheduled cron job. 跑巡检",
              eid="1", ts="2026-11-09T10:00:00+00:00"),
        _item("assistant", "[SILENT]", eid="2", ts="2026-11-09T10:00:05+00:00"),
    ]
    assert pair_items(items) == []


def test_operation_interrupted_is_not_the_final_answer():
    """末条是中断标记时，答案应回退到真正的结论，而不是把中断写进记忆。"""
    items = [
        _item("user", "把迁移跑完，报告结果", eid="1", ts="2026-11-09T10:00:00+00:00"),
        _item("assistant", "迁移完成：1,234 个文件，0 失败。", eid="2", ts="2026-11-09T10:05:00+00:00"),
        _item("assistant", "Operation interrupted: waiting for model response (160.1s elapsed).",
              eid="3", ts="2026-11-09T10:06:00+00:00"),
    ]
    pairs = pair_items(items)
    assert len(pairs) == 1
    assert pairs[0].answer == "迁移完成：1,234 个文件，0 失败。"


def test_delegation_receipt_not_an_answer():
    items = [
        _item("user", "派两个子代理分别审 A 和 B", eid="1", ts="2026-11-09T10:00:00+00:00"),
        _item("assistant", "[ASYNC DELEGATION BATCH COMPLETE — deleg_abc123] 结果见下",
              eid="2", ts="2026-11-09T10:01:00+00:00"),
        _item("assistant", "两个都审完了：A 无问题，B 有 2 处缺陷。",
              eid="3", ts="2026-11-09T10:02:00+00:00"),
    ]
    pairs = pair_items(items)
    assert len(pairs) == 1
    assert "两个都审完了" in pairs[0].answer
    assert "ASYNC DELEGATION" not in pairs[0].answer
