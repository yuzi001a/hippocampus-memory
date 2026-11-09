"""v3core.task_ingest — 从真实消息里抽取**有结构证据**的任务事件。

只认确定性结构，不做语义判断：

* ``[ASYNC DELEGATION BATCH COMPLETE — deleg_<id>]`` 回执（实测 783 条）
  - 头部 ``Dispatched: YYYY-MM-DD HH:MM:SS``  → 任务的 created 时间
  - ``✓ TASK n/m: <goal> (status=completed, api_calls=…, …s)`` → 每条子任务
  - ``status=completed`` 只算 **claimed_done**（子代理自己的说法），
    不算 verified_done；后者必须另有测试/日志证据。
* 调用方传入的 ``provenance['task_id']`` + ``provenance['task_status']``
  → 直接采纳（这是宿主自己的任务身份，不需要猜）。

抽不出身份的消息一律不产生任务事件 —— 宁可漏记，不可错并。
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable

from .task_events import (
    TaskEvent,
    TaskEventStore,
    derive_project,
    derive_task_id,
)

_RECEIPT_RE = re.compile(r"\[ASYNC DELEGATION BATCH COMPLETE\s*[—\-]\s*(deleg_[0-9a-f]{6,})\]", re.I)
_DISPATCHED_RE = re.compile(r"Dispatched:\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
_TASK_RE = re.compile(
    r"(?:✓|✗|✔|×)\s*TASK\s+(\d+)\s*/\s*(\d+)\s*:\s*(.*?)\s*\(status=([A-Za-z_]+)([^)]*)\)",
    re.S,
)
_STATUS_MAP = {
    "completed": "completed",
    "done": "completed",
    "failed": "failed",
    "error": "failed",
    "timeout": "failed",
    "cancelled": "failed",
}


def _tz_iso(day: str, clock: str, fallback: str | None) -> str:
    return f"{day}T{clock}" if day and clock else (fallback or "")


def _clip(text: str, n: int = 400) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def extract_task_events(items: Iterable, *, default_project: str | None = None) -> list[TaskEvent]:
    """Extract task events from raw import items (deterministic, no LLM)."""
    events: list[TaskEvent] = []
    for it in items:
        text = it.text or ""
        head = text.lstrip()
        prov = dict(it.provenance or {})
        source_id = f"conversation_stream:{it.host or it.source_system or '?'}:{it.source_ref}:{it.event_id or ''}"

        # 1) 显式任务身份（宿主自己标的）—— 最高优先级
        explicit_task = str(prov.get("task_id") or "").strip()
        explicit_status = str(prov.get("task_status") or "").strip()
        if explicit_task and explicit_status:
            etype = {
                "completed": "completed",
                "failed": "failed",
                "running": "progress",
                "created": "created",
            }.get(explicit_status, "progress")
            events.append(
                TaskEvent(
                    task_id=derive_task_id(provenance=prov) or f"task:{explicit_task}",
                    project=derive_project(provenance=prov) or default_project,
                    event_type=etype,
                    summary=_clip(prov.get("task_summary") or text),
                    source_id=source_id,
                    occurred_at=it.occurred_at or "",
                    provenance={**prov, "origin": "explicit"},
                )
            )
            continue

        # 2) 异步委派回执 —— 目标 / 状态 / 派发时间都在正文里
        m = _RECEIPT_RE.search(head)
        if m:
            delegation = m.group(1)
            task_prefix = f"deleg:{delegation.removeprefix('deleg_')}"
            dm = _DISPATCHED_RE.search(text)
            created_at = _tz_iso(dm.group(1), dm.group(2), it.occurred_at) if dm else it.occurred_at
            tasks = list(_TASK_RE.finditer(text))
            if not tasks:
                events.append(
                    TaskEvent(
                        task_id=task_prefix,
                        project=derive_project(text=text) or default_project,
                        event_type="progress",
                        summary=_clip(head.split("]")[-1]),
                        source_id=source_id,
                        occurred_at=it.occurred_at or "",
                        provenance={"origin": "async_receipt", "delegation": delegation},
                    )
                )
                continue
            multi = len(tasks) > 1
            for tm in tasks:
                idx, total, goal, status, extras = tm.groups()
                task_id = f"{task_prefix}:{idx}" if multi else task_prefix
                etype = _STATUS_MAP.get(status.lower(), "progress")
                events.append(
                    TaskEvent(
                        task_id=task_id,
                        project=derive_project(text=text) or default_project,
                        event_type="created",
                        summary=_clip(goal),
                        source_id=source_id,
                        occurred_at=created_at or "",
                        provenance={
                            "origin": "async_receipt",
                            "delegation": delegation,
                            "index": f"{idx}/{total}",
                        },
                    )
                )
                events.append(
                    TaskEvent(
                        task_id=task_id,
                        project=derive_project(text=text) or default_project,
                        event_type=etype,
                        summary=_clip(f"{goal} — status={status}"),
                        source_id=source_id,
                        occurred_at=it.occurred_at or "",
                        provenance={
                            "origin": "async_receipt",
                            "delegation": delegation,
                            "reported_status": status,
                            "extras": _clip(extras, 200),
                            # 子代理自报，不是验证证据 —— 显式标注，避免被当成事实
                            "evidence": "subagent_self_report",
                        },
                    )
                )
            continue

        # 3) 其它形态：只认调用方给的 task_id（已在上面的分支处理），
        #    正文里出现的 deleg_/cron 号不足以判定事件类型，不生成。
    return events


def ingest_task_events(
    store: TaskEventStore, events: Iterable[TaskEvent]
) -> dict:
    """Write extracted events. Returns counts by status (idempotent on event_id)."""
    written = 0
    by_status: dict[str, int] = {}
    for ev in events:
        store.append(ev)
        written += 1
        by_status[ev.status] = by_status.get(ev.status, 0) + 1
    return {"written": written, "by_status": by_status}


__all__ = ["extract_task_events", "ingest_task_events"]
