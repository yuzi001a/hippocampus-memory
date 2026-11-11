"""回归：宿主贴在正文前的标记必须剥掉，不能变成提问的一部分。

2026-11-11 全量重建实测：机器前言清掉后，剩下带 "[" 的提问几乎全是
`[Wed 2026-04-22 01:59 GMT+8] …` / `[message_id: om_…] …` 这类投递层标记
粘在真人原话前面——剥标记留正文，两条角色都适用。
"""
from __future__ import annotations

from v3core.importers import ImportItem, KIND_RAW, qa_pairing as QA
from v3core.importers.legacy_sources import strip_host_markers


def _item(text: str, role: str, ts: str, event_id: str) -> ImportItem:
    return ImportItem(kind=KIND_RAW, source_system="hermes", source_ref="s1", text=text,
                      role=role, occurred_at=ts, host="hermes", event_id=event_id)


def test_strip_host_markers_variants():
    assert strip_host_markers("[Wed 2026-04-22 01:59 GMT+8] 先歇一会儿") == "先歇一会儿"
    assert strip_host_markers("[message_id: om_x100b6ff2226da0] 这个我看过") == "这个我看过"
    assert strip_host_markers("[[reply_to_current]]\n\n收到") == "收到"
    assert strip_host_markers("普通一句话") == "普通一句话"


def test_question_and_answer_carry_no_host_markers():
    items = [
        _item("[message_id: om_abc123] 这个方案能落地吗", "user", "2026-04-22T01:59:00+08:00", "e1"),
        _item("[[reply_to_current]]\n\n能，先跑隔离库。", "assistant", "2026-04-22T01:59:30+08:00", "e2"),
    ]
    pairs = QA.pair_items(items)
    assert len(pairs) == 1
    assert pairs[0].question == "这个方案能落地吗"
    assert pairs[0].answer == "能，先跑隔离库。"
