from types import SimpleNamespace

import pytest


def test_observer_note_section_marks_v_rows_as_delta_not_complete_state():
    from v3core import observer

    note = observer._build_note_section(
        (
            815,
            "v815",
            "M03 is the latest incremental update.",
            None,
            [],
            814,
        )
    )

    assert note["snapshot_semantics"] == "delta"
    assert note["complete_state"] is False
    assert note["note_id"] == 815
    assert note["content"] == "M03 is the latest incremental update."


def test_observer_note_section_preserves_seed_content_and_range_end():
    from v3core import observer

    note = observer._build_note_section(
        (
            813,
            "v809",
            "full raw delta",
            SimpleNamespace(upper=456),
            [{"kind": "seed", "content": "seed representation"}],
            812,
        )
    )

    assert note["content"] == "seed representation"
    assert note["range_end"] == 456
    assert note["snapshot_semantics"] == "delta"


def test_first_round_labels_delta_as_incomplete_and_warns_omission_is_not_closure(
    monkeypatch: pytest.MonkeyPatch,
):
    from v3core import observer
    from v3core.injector import MemoryInjector

    delta_note = {
        "note_id": 815,
        "version": "v815",
        "content": "M03 is the latest incremental update.",
        "range_end": 0,
        "snapshot_semantics": "delta",
        "complete_state": False,
    }
    monkeypatch.setattr(
        observer,
        "recall_for_new_session",
        lambda **kwargs: {"note": delta_note, "tail": [], "topics": []},
    )

    injector = MemoryInjector(
        SimpleNamespace(config={}, pg_pool=None, _pg_pool=None),
        strategy="identity_first",
    )
    header, units = injector._format_first_round_block("session-b")
    rendered = "\n".join(units)

    assert header == "[新会话记忆召回]"
    assert "[记忆增量快照 v815" in rendered
    assert "非全量当前状态" in rendered
    assert "未提及事项不代表不存在、完成或被取代" in rendered
    assert "M03 is the latest incremental update." in rendered
