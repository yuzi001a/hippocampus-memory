"""I01 — deterministic QA pairing (raw → recall-ready, no LLM).

The state machine merges consecutive user rows into one question and
consecutive assistant rows into one answer. Orphans never become QA pairs
("宁可只保存 raw"). Source identity survives into qa_pairs.source_id.
"""
from __future__ import annotations

import importlib

import pytest

from v3core.importers import KIND_RAW, ImportItem


def _pairing():
    return importlib.import_module("v3core.importers.qa_pairing")


def _item(role: str, text: str, *, sid: str = "s1", eid: str = "e1",
          host: str = "hermes", ts: str = "2026-06-01T00:00:00Z") -> ImportItem:
    return ImportItem(
        kind=KIND_RAW,
        source_system=host,
        source_ref=sid,
        text=text,
        role=role,
        occurred_at=ts,
        provenance={},
        host=host,
        event_id=eid,
        identity_kind="native",
    )


def _items(seq, sid="s1", host="hermes"):
    return [
        _item(role, text, sid=sid, eid=f"e{i}", host=host)
        for i, (role, text) in enumerate(seq, start=1)
    ]


# ── the table ─────────────────────────────────────────────────────────


def test_single_exchange():
    p = _pairing().pair_items(_items([("user", "q1"), ("assistant", "a1")]))
    assert len(p) == 1
    assert p[0].question == "q1"
    assert p[0].answer == "a1"


def test_consecutive_users_merge_into_one_question():
    p = _pairing().pair_items(
        _items([("user", "q1"), ("user", "q2"), ("assistant", "a1")])
    )
    assert len(p) == 1
    assert p[0].question == "q1\n\nq2"


def test_consecutive_assistants_merge_into_one_answer():
    p = _pairing().pair_items(
        _items([("user", "q1"), ("assistant", "a1"), ("assistant", "a2")])
    )
    assert len(p) == 1
    assert p[0].answer == "a1\n\na2"


def test_two_turns_two_pairs():
    p = _pairing().pair_items(
        _items(
            [
                ("user", "q1"),
                ("assistant", "a1"),
                ("user", "q2"),
                ("assistant", "a2"),
            ]
        )
    )
    assert [x.question for x in p] == ["q1", "q2"]


def test_orphan_user_no_pair():
    p = _pairing().pair_items(_items([("user", "q1"), ("assistant", "a1"), ("user", "q2")]))
    assert len(p) == 1  # q2 has no answer → raw only


def test_orphan_assistant_no_pair():
    p = _pairing().pair_items(_items([("assistant", "a0"), ("user", "q1"), ("assistant", "a1")]))
    assert len(p) == 1
    assert p[0].question == "q1"


def test_no_items_no_pairs():
    assert _pairing().pair_items([]) == []


def test_groups_are_per_session():
    a = _items([("user", "q1"), ("assistant", "a1")], sid="s1")
    b = _items([("user", "q1"), ("assistant", "a1")], sid="s2", host="dsh")
    p = _pairing().pair_items(a + b)
    assert len(p) == 2
    assert {x.session_id for x in p} == {"s1", "s2"}


# ── identity ──────────────────────────────────────────────────────────


def test_source_id_format_and_traceability():
    p = _pairing().pair_items(_items([("user", "q1"), ("assistant", "a1")]))
    assert p[0].source_id == "qa_import/hermes/s1/e1"
    assert p[0].q_event_id == "e1"
    assert p[0].host == "hermes"
    assert p[0].session_id == "s1"


def test_turn_id_ordinal():
    p = _pairing().pair_items(
        _items([("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")])
    )
    assert [x.turn_id for x in p] == [1, 2]


def test_question_timestamp_from_first_user():
    items = _items(
        [
            ("user", "q1"),
            ("user", "q2"),
            ("assistant", "a1"),
        ]
    )
    items[0].occurred_at = "2026-06-01T10:00:00Z"
    items[1].occurred_at = "2026-06-01T10:05:00Z"
    items[2].occurred_at = "2026-06-01T10:10:00Z"
    p = _pairing().pair_items(items)
    assert p[0].occurred_at == "2026-06-01T10:00:00Z"


# ── real fixture integration ──────────────────────────────────────────


def test_hermes_fixture_pairing_end_to_end():
    from v3core.importers.hermes_sessions import HermesSessionImporter

    imp = HermesSessionImporter()
    imp.reset_excluded_counts()
    from pathlib import Path

    db = Path(__file__).resolve().parent / "fixtures" / "i01" / "hermes" / "state.db"
    items = list(imp.parse(db))
    pairs = _pairing().pair_items(items)
    # s1: q(银杏) → a(记住了 + 上下文继续 + 盘位表第3版) merged
    #     q(好，继续) → a(部署 + 归档) merged
    # s2: q(steer 先停一下) → a(好，停了)
    assert len(pairs) == 3
    ginkgo = next(x for x in pairs if "银杏" in x.question)
    assert "记住了" in ginkgo.answer
    assert "上下文继续" in ginkgo.answer
    assert "盘位表更新到第 3 版" in ginkgo.answer  # consecutive asst merge
    cont = next(x for x in pairs if "好，继续" in x.question)
    assert "部署" in cont.answer
    assert "归档" in cont.answer
    steer = next(x for x in pairs if "先停一下" in x.question)
    assert steer.answer == "好，停了。"
