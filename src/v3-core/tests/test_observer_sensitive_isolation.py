"""敏感项隔离 (2026-11-12) — 批内确定性内容过滤的二分归因与拉黑。

背景: 一批 30 条 QA 里只要有一条触发平台输入侧过滤 (422 new_sensitive/1026),
整批就废; 旧逻辑只能自适应减半直到 1-3 条, note 被稀释成几十字碎片。
本模块的契约: 被拒批必须能定位到具体 QA, 拉黑后按原批次粒度重新取批。

这些测试不连 PG、不打网络: call_m3 全部用 stub 替换。
"""
from __future__ import annotations

import json

import pytest

from v3core import observer as obs


def _row(qa_id: int, q: str, a: str = "answer") -> tuple:
    """构造与 _observe_worker #3 步 SELECT 同序的行 (id, session, ts, q, a, chars)。"""
    return (qa_id, "sess", None, q, a, len(q) + len(a))


@pytest.fixture()
def state_cfg(tmp_path):
    return {"state_path": str(tmp_path / "observer_state.json")}


def test_find_sensitive_rows_isolates_only_poison(monkeypatch):
    """二分归因: 只返回触发过滤的行, 干净行不被误伤。"""
    poison_ids = {7, 19}

    def fake_call_m3(cfg, api_key, prompt, **kwargs):
        hit = any(f"Q: q{i}" in prompt for i in poison_ids)
        if hit:
            return {"error": "HTTP 422: input new_sensitive (1026)"}
        return {"text": "ok"}

    monkeypatch.setattr(obs, "call_m3", fake_call_m3)

    rows = [_row(i, f"q{i}") for i in range(1, 31)]
    offenders = obs._find_sensitive_rows(rows, {"model": "m"}, "k")
    assert sorted(int(r[0]) for r in offenders) == [7, 19]


def test_find_sensitive_rows_empty_when_batch_context_is_the_trigger(monkeypatch):
    """整批都"不敏感"时返回空 → 调用方必须回落旧的自适应减半。

    这是防死循环的契约: 若触发源是上一版印而非具体 QA, 二分不能凭空造出替罪羊。
    """
    def fake_call_m3(cfg, api_key, prompt, **kwargs):
        return {"text": "ok"}

    monkeypatch.setattr(obs, "call_m3", fake_call_m3)

    rows = [_row(i, f"q{i}") for i in range(1, 11)]
    assert obs._find_sensitive_rows(rows, {"model": "m"}, "k") == []


def test_probe_treats_call_exception_as_not_hit(monkeypatch):
    """探测调用异常不能升级成"命中", 否则会把干净 QA 误拉黑。"""
    def boom(cfg, api_key, prompt, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(obs, "call_m3", boom)
    assert obs._probe_qa_rows_sensitive([_row(1, "q1")], {"model": "m"}, "k") is False


def test_blocked_ids_round_trip_and_dedupe(state_cfg):
    """黑名单持久化: 去重、追加、可读回。"""
    obs._add_blocked_qa_ids([3, 5], reason="r1", cfg=state_cfg)
    obs._add_blocked_qa_ids([5, 9], reason="r2", cfg=state_cfg)

    ids = obs._load_blocked_qa_ids(state_cfg)
    assert ids == [3, 5, 9]

    payload = json.loads(
        (obs._blocked_qa_path(state_cfg)).read_text(encoding="utf-8")
    )
    assert payload["count"] == 3
    assert payload["last_reason"] == "r2"


def test_load_blocked_ids_absent_returns_empty(state_cfg):
    assert obs._load_blocked_qa_ids(state_cfg) == []


def test_blocked_path_lives_next_to_state_file(state_cfg):
    """黑名单与游标同目录 — 回放/隔离 profile 各自独立, 不串味。"""
    state = obs._state_path(state_cfg)
    assert obs._blocked_qa_path(state_cfg).parent == state.parent
