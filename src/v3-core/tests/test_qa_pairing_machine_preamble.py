"""回归：裸机器前言不许进 QA（2026-11-11 全量重建实测到的漏网）。

缺陷：`pair_items` 里 `is_machine_preamble` 只写在"剥壳成功"分支内，
没有外壳的 cron / 后台通知整条当成提问，实测漏进来 1,000+ 对。
"""
from __future__ import annotations

import pytest

from v3core.importers import ImportItem, KIND_RAW, qa_pairing as QA
from v3core.importers.legacy_sources import is_machine_preamble, unwrap_injected


def _item(text: str, role: str, ts: str, event_id: str) -> ImportItem:
    return ImportItem(
        kind=KIND_RAW, source_system="hermes", source_ref="s1", text=text,
        role=role, occurred_at=ts, host="hermes", event_id=event_id,
    )


@pytest.mark.parametrize("machinery", [
    "[IMPORTANT: You are running as a scheduled cron job. DELIVERY: …",
    "[Your active task list was preserved across …",
    "[IMPORTANT: Background process proc_1234 completed normally (exit code 0).",
    "[System: The previous response was cut off by a server error.",
    "[media attached: /home/x/a.png]",
    "[The user attached an image:\n# Description](…)",
    "[This response was interrupted by a user (cancelled).",
    '[{"type":"thinking","thinking":"Let me …"}]',
])
def test_bare_machine_preamble_never_becomes_a_question(machinery: str):
    items = [_item(machinery, "user", "2026-07-29T23:41:05+08:00", "e1"),
             _item("真实的回答应该在这里", "assistant", "2026-07-29T23:41:09+08:00", "e2")]
    assert is_machine_preamble(machinery) is True
    assert QA.pair_items(items) == []


def test_reply_quote_marker_is_stripped_not_dropped():
    """平台引用标记裹着真人原话：标记去掉，正文必须留下。"""
    raw = "[[reply_to_current]]\n\n那就按这个方案来。"
    body, kind = unwrap_injected(raw)
    assert kind == "reply_quote"
    assert body == "那就按这个方案来。"
    items = [_item(raw, "user", "2026-10-04T20:00:00+08:00", "e1"),
             _item("好，我这就开始。", "assistant", "2026-10-04T20:00:05+08:00", "e2")]
    pairs = QA.pair_items(items)
    assert len(pairs) == 1
    assert pairs[0].question == "那就按这个方案来。"


def test_real_question_still_pairs():
    items = [_item("现在该干啥了", "user", "2026-11-11T09:00:00+08:00", "e1"),
             _item("直接全量重建替换。", "assistant", "2026-11-11T09:00:04+08:00", "e2")]
    pairs = QA.pair_items(items)
    assert len(pairs) == 1
    assert pairs[0].question == "现在该干啥了"
    assert pairs[0].answer == "直接全量重建替换。"
