"""task_events — 任务记忆通道的契约与身份规则。

重点证明三件事：
  1. "Agent 声称完成" 与 "测试/日志证明完成" 是不同的状态，且不能互相冒充。
  2. 任务身份只来自确定性来源；同一任务的多次注入落在同一 task_id。
  3. 状态是事件流的投影：后来的失败/更正会改状态，历史事件不丢。
"""
from __future__ import annotations

import pytest

from v3core.task_events import (
    TaskEvent,
    TaskEventError,
    TaskEventStore,
    derive_task_id,
)
from v3core.task_ingest import extract_task_events


class _Item:
    """Minimal ImportItem stand-in (结构字段够用)."""

    def __init__(self, text, *, role="user", host="hermes", sess="s1", event_id="e1",
                 occurred_at="2026-06-24T22:05:00Z", provenance=None):
        self.kind = "raw_message"
        self.text = text
        self.role = role
        self.host = host
        self.source_system = host
        self.source_ref = sess
        self.event_id = event_id
        self.occurred_at = occurred_at
        self.provenance = provenance or {}


RECEIPT = """[ASYNC DELEGATION BATCH COMPLETE — deleg_5276b9f0] A background fan-out of 1 subagent(s) you dispatched earlier has finished.

Dispatched: 2026-06-24 21:53:00 (12m1s ago)
Role: leaf   Model: MiniMax-M3   Total duration: 722.41s

---
✓ TASK 1/1: 修改 v3 prefetch 系统 4 个位置，实现 3 项目标：1) 加动作指令到 preamble 2) 砍召回数量 12→5 3) session_summary 超 3 天不召  (status=completed, api_calls=100, 721.8s)
---
# 任务完成总结
改动落点（5 处全部写入 + 验证通过）
"""


# ── 状态语义 ──────────────────────────────────────────────────────────


def test_completed_without_evidence_is_only_claimed():
    ev = TaskEvent(
        task_id="deleg:abc",
        event_type="completed",
        summary="改完了",
        source_id="conv:1",
        occurred_at="2026-06-24T22:05:00Z",
    )
    assert ev.status == "claimed_done"


def test_verified_done_requires_evidence():
    with pytest.raises(TaskEventError) as exc:
        TaskEvent(
            task_id="deleg:abc",
            event_type="completed",
            status="verified_done",
            summary="测试通过",
            source_id="conv:1",
            occurred_at="2026-06-24T22:05:00Z",
        )
    assert exc.value.code == "VERIFICATION_EVIDENCE_REQUIRED"

    ok = TaskEvent(
        task_id="deleg:abc",
        event_type="completed",
        status="verified_done",
        summary="153 passed / 2 skipped",
        source_id="conv:2",
        occurred_at="2026-06-24T22:10:00Z",
        provenance={"verification": "pytest tests/ -q: 153 passed"},
    )
    assert ok.status == "verified_done"


def test_invalid_type_and_missing_source_rejected():
    with pytest.raises(TaskEventError):
        TaskEvent(task_id="t", event_type="whatever", summary="x", source_id="s",
                  occurred_at="2026-01-01T00:00:00Z")
    with pytest.raises(TaskEventError):
        TaskEvent(task_id="t", event_type="progress", summary="x", source_id="",
                  occurred_at="2026-01-01T00:00:00Z")


def test_event_id_is_stable_per_source():
    a = TaskEvent(task_id="deleg:abc", event_type="progress", summary="x",
                  source_id="conv:1", occurred_at="2026-01-01T00:00:00Z")
    b = TaskEvent(task_id="deleg:abc", event_type="progress", summary="y",
                  source_id="conv:1", occurred_at="2026-01-02T00:00:00Z")
    c = TaskEvent(task_id="deleg:abc", event_type="completed", summary="x",
                  source_id="conv:1", occurred_at="2026-01-01T00:00:00Z")
    assert a.event_id == b.event_id      # 同任务+同类型+同来源 ⇒ 幂等
    assert a.event_id != c.event_id      # 类型不同 ⇒ 不同事件


# ── 身份规则 ──────────────────────────────────────────────────────────


def test_task_identity_priority():
    assert derive_task_id(provenance={"task_id": "T-9"}) == "task:T-9"
    assert derive_task_id(provenance={"delegation_id": "deleg_5276b9f0"}) == "deleg:5276b9f0"
    assert derive_task_id(text="见 [ASYNC DELEGATION BATCH COMPLETE — deleg_5276b9f0]") == "deleg:5276b9f0"
    assert derive_task_id(text="This is a scheduled cron job (job id a65d3c46)") == "cron:a65d3c46"
    assert derive_task_id(text="随便一段没有任务身份的话") is None


# ── 抽取 ──────────────────────────────────────────────────────────────


def test_extract_receipt_creates_goal_and_claim():
    events = extract_task_events([_Item(RECEIPT)])
    created = [e for e in events if e.event_type == "created"]
    done = [e for e in events if e.event_type == "completed"]
    assert len(created) == 1 and len(done) == 1
    assert created[0].task_id == "deleg:5276b9f0"
    assert "修改 v3 prefetch 系统" in created[0].summary
    # 派发时间来自回执正文，不是回执落库时间
    assert created[0].occurred_at.startswith("2026-06-24T21:53")
    assert done[0].status == "claimed_done"          # 子代理自报 ≠ 已验证
    assert done[0].provenance["evidence"] == "subagent_self_report"
    assert done[0].task_id == created[0].task_id


def test_extract_multi_task_batch_keeps_tasks_apart():
    two = RECEIPT.replace("TASK 1/1", "TASK 1/2") + \
        "\n✓ TASK 2/2: 补回滚脚本 (status=failed, api_calls=3, 12.0s)\n"
    events = extract_task_events([_Item(two)])
    ids = sorted({e.task_id for e in events})
    assert ids == ["deleg:5276b9f0:1", "deleg:5276b9f0:2"]
    failed = [e for e in events if e.event_type == "failed"]
    assert len(failed) == 1 and failed[0].status == "failed"


def test_extract_ignores_messages_without_task_identity():
    assert extract_task_events([_Item("今天天气不错，聊聊别的。")]) == []


def test_extract_is_scoped_to_the_delegation_not_the_session():
    """跨会话：同一 deleg id 在不同会话里出现，任务身份不变。"""
    a = extract_task_events([_Item(RECEIPT, sess="sessA", event_id="e1")])
    b = extract_task_events([_Item(RECEIPT, sess="sessB", event_id="e2")])
    assert a[0].task_id == b[0].task_id == "deleg:5276b9f0"


# ── 状态投影（回放式） ────────────────────────────────────────────────


def _store_with(events):
    store = TaskEventStore(pool=None)
    rows = [
        {
            "event_id": e.event_id,
            "task_id": e.task_id,
            "project": e.project,
            "event_type": e.event_type,
            "status": e.status,
            "summary": e.summary,
            "source_id": e.source_id,
            "occurred_at": e.occurred_at,
            "recorded_at": e.occurred_at,
            "provenance": e.provenance,
        }
        for e in events
    ]
    store.timeline = lambda task_id: [r for r in rows if r["task_id"] == task_id]  # type: ignore[assignment]
    return store


def test_state_is_projected_from_events_and_corrections_win():
    base = TaskEvent(task_id="deleg:x", event_type="created", summary="目标：修 profile 加载",
                     source_id="s1", occurred_at="2026-01-01T00:00:00Z")
    claim = TaskEvent(task_id="deleg:x", event_type="completed", summary="改完了",
                      source_id="s2", occurred_at="2026-01-01T01:00:00Z")
    correction = TaskEvent(task_id="deleg:x", event_type="corrected",
                           summary="复核发现配置并未生效",
                           source_id="s3", occurred_at="2026-01-01T02:00:00Z")
    store = _store_with([base, claim, correction])
    st = store.state("deleg:x")
    assert st is not None
    assert st.status == "open"           # 更正把状态拉回未完成
    assert st.goal.startswith("目标：修 profile")
    assert len(st.events) == 3           # 声称完成的那条事件仍然保留
    assert st.is_finished is False


def test_missing_table_is_reported_not_swallowed():
    class _Cur:
        def execute(self, *a, **k):
            raise RuntimeError('relation "public.task_events" does not exist')

        def fetchall(self):
            return []

    class _Lease:
        class connection:
            @staticmethod
            def cursor():
                return _Cur()

            @staticmethod
            def commit():
                pass

            @staticmethod
            def rollback():
                pass

        def close(self):
            pass

    class _Pool:
        def lease(self, timeout=None):
            return _Lease()

    store = TaskEventStore(pool=_Pool())
    with pytest.raises(TaskEventError) as exc:
        store.append(TaskEvent(task_id="t", event_type="created", summary="x",
                               source_id="s", occurred_at="2026-01-01T00:00:00Z"))
    assert exc.value.code == "MIGRATION_REQUIRED"
