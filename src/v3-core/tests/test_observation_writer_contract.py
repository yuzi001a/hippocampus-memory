from __future__ import annotations

import v3core.embedding as embedding_mod
import v3core.observation_chunks as observation_chunks_mod
import v3core.observer as observer_mod


class _Cursor:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.statements.append((sql, params))

    def fetchone(self):
        return ("v-test",)


class _Pg:
    def __init__(self):
        self.cursor_obj = _Cursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_planner_error_fails_closed_without_bare_full_content_request(monkeypatch):
    pg = _Pg()
    recorded = []
    calls = []

    def bad_plan(*args, **kwargs):
        raise RuntimeError("synthetic planner defect")

    monkeypatch.setattr(observation_chunks_mod, "plan_observation_chunks", bad_plan)
    monkeypatch.setattr(
        observer_mod,
        "_record_observation_derived_failure",
        lambda *args, **kwargs: recorded.append((args, kwargs)) or True,
        raising=False,
    )
    monkeypatch.setattr(
        embedding_mod,
        "embed_batch",
        lambda *args, **kwargs: calls.append(args) or [[1.0, 0.0]],
    )

    observer_mod._backfill_note_embedding(
        pg,
        42,
        "x" * 20000,
        cfg={"model": "test", "_fingerprint": "fp", "max_input_tokens": 8192},
    )

    assert calls == []
    assert len(recorded) == 1
    assert recorded[0][0][2] == "observation_long_plan"


def test_parent_merge_uses_max_child_score_and_full_parent():
    from v3core.observation_chunks import merge_observation_recall_hits

    full = "head\n" + ("tail " * 1000)
    merged = merge_observation_recall_hits([
        {"observation_id": 1, "observation_version": "v1", "kind": "child", "cosine": 0.4, "parent_content": full},
        {"observation_id": 1, "observation_version": "v1", "kind": "parent", "cosine": 0.8, "content": full},
    ])
    assert len(merged) == 1
    assert merged[0]["cosine"] == 0.8
    assert merged[0]["content"] == full
