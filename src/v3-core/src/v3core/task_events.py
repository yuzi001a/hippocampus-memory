"""v3core.task_events — 最小任务事件存储（任务记忆通道）。

为什么需要它
------------
``qa_pairs`` 是问答二列，承载不了"同一个任务跨几十次注入、目标不变而状态在
变"这件事。把任务塞进 ``topics`` 会把主题卡变成状态数据库，也不是它的职责。
这里只做一件事：**按任务身份收集有来源的事件，状态由事件算出来。**

状态模型
--------
任务状态不是可变字段，而是事件流的投影::

    created / requirement / progress  → open / running
    completed (声明)                  → claimed_done     ← Agent 说完成了
    completed + verification 证据     → verified_done    ← 测试/日志证明完成
    failed                            → failed
    corrected                         → open（并保留此前事件）
    blocked                           → blocked

``claimed_done`` 与 ``verified_done`` 必须分开：前者只是助手的一句话，
后者必须带 ``provenance["verification"]``（测试输出、回执、命令结果）。
后续 ``corrected`` / ``failed`` 事件会改变当前状态，**旧事件不删**，
所以"曾经声称成功、后来被证伪"可以完整回放。

身份规则（不猜、不合并）
------------------------
``task_id`` 只来自确定性来源，按优先级：

1. 调用方显式给的 ``provenance["task_id"]``；
2. 委派 id ``deleg_<hex>``（同一委派 = 同一任务）；
3. 定时任务 ``job id <hex>`` / ``[cron:<id>``（同一 cron = 同一任务）；
4. 都没有 → **不产生任务事件**。

绝不用语义相似度把两条任务并成一条：宁可漏记，不可错并。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

EVENT_TYPES: tuple[str, ...] = (
    "created",
    "requirement",
    "progress",
    "completed",
    "failed",
    "corrected",
    "blocked",
)

STATUSES: tuple[str, ...] = (
    "open",
    "running",
    "claimed_done",
    "verified_done",
    "failed",
    "blocked",
)

_STATUS_BY_TYPE = {
    "created": "open",
    "requirement": "open",
    "progress": "running",
    "completed": "claimed_done",
    "failed": "failed",
    "corrected": "open",
    "blocked": "blocked",
}

MIGRATION_REQUIRED = "MIGRATION_REQUIRED"


class TaskEventError(RuntimeError):
    """任务事件写入/读取被拒绝（结构化，不静默降级）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ── identity ────────────────────────────────────────────────────────────

_DELEG_RE = re.compile(r"\bdeleg_([0-9a-f]{6,})\b")
_CRON_ID_RES = (
    re.compile(r"\[cron:([0-9a-f][0-9a-f-]{6,})", re.I),
    re.compile(r"scheduled cron job\s*\(?job id\s+([0-9a-f][0-9a-f-]{6,})", re.I),
    re.compile(r"job_id[\"'\s:=]+([0-9a-f][0-9a-f-]{6,})", re.I),
)


def derive_task_id(*, text: str | None = None, provenance: dict | None = None) -> str | None:
    """Return the task identity carried by a record, or None.

    Deterministic only — never inferred from content similarity.
    """
    prov = provenance or {}
    explicit = str(prov.get("task_id") or "").strip()
    if explicit:
        return f"task:{explicit}" if not explicit.startswith("task:") else explicit
    delegation = str(prov.get("delegation_id") or "").strip()
    if delegation:
        return f"deleg:{delegation.removeprefix('deleg_')}"
    body = text or ""
    m = _DELEG_RE.search(body)
    if m:
        return f"deleg:{m.group(1)}"
    for rx in _CRON_ID_RES:
        m = rx.search(body)
        if m:
            return f"cron:{m.group(1)}"
    return None


def derive_project(*, text: str | None = None, provenance: dict | None = None) -> str | None:
    prov = provenance or {}
    for key in ("project", "project_id", "workspace"):
        val = str(prov.get(key) or "").strip()
        if val:
            return val
    m = re.search(r"\bproject[=:\s]+([A-Za-z0-9._-]{2,40})", text or "", re.I)
    return m.group(1) if m else None


def event_id_for(task_id: str, event_type: str, source_id: str) -> str:
    """Stable event identity: same source ⇒ same event ⇒ idempotent re-import."""
    digest = hashlib.sha256(f"{task_id}|{event_type}|{source_id}".encode("utf-8")).hexdigest()
    return f"te_{digest[:32]}"


# ── records ─────────────────────────────────────────────────────────────


@dataclass
class TaskEvent:
    task_id: str
    event_type: str
    summary: str
    source_id: str
    occurred_at: str
    project: str | None = None
    status: str | None = None
    provenance: dict = field(default_factory=dict)
    event_id: str | None = None

    def __post_init__(self) -> None:
        if self.event_type not in EVENT_TYPES:
            raise TaskEventError(
                "INVALID_EVENT_TYPE",
                f"{self.event_type!r} not in {list(EVENT_TYPES)}",
            )
        if not (self.summary or "").strip():
            raise TaskEventError("EMPTY_SUMMARY", "task event needs a summary")
        if not (self.source_id or "").strip():
            raise TaskEventError("MISSING_SOURCE", "task event needs a source_id")
        if not (self.occurred_at or "").strip():
            raise TaskEventError("MISSING_OCCURRED_AT", "task event needs occurred_at")
        if self.event_type == "completed" and not self.status:
            self.status = (
                "verified_done"
                if (self.provenance or {}).get("verification")
                else "claimed_done"
            )
        if not self.status:
            self.status = _STATUS_BY_TYPE[self.event_type]
        if self.status not in STATUSES:
            raise TaskEventError("INVALID_STATUS", f"{self.status!r} not in {list(STATUSES)}")
        # 只有带证据的 completed 才允许 verified_done —— 不接受"我说验证过了"。
        if self.status == "verified_done" and not (self.provenance or {}).get("verification"):
            raise TaskEventError(
                "VERIFICATION_EVIDENCE_REQUIRED",
                "verified_done requires provenance['verification'] (test log / receipt)",
            )
        if not self.event_id:
            self.event_id = event_id_for(self.task_id, self.event_type, self.source_id)


@dataclass
class TaskState:
    task_id: str
    status: str
    latest_event: dict
    goal: str | None
    created_at: str | None
    events: list[dict]

    @property
    def is_finished(self) -> bool:
        return self.status in ("verified_done", "failed")

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "is_finished": self.is_finished,
            "goal": self.goal,
            "created_at": self.created_at,
            "latest_event": self.latest_event,
            "events": len(self.events),
        }


# ── store ───────────────────────────────────────────────────────────────

_INSERT_SQL = """
    INSERT INTO public.task_events
        (event_id, task_id, project, event_type, status, summary, source_id,
         occurred_at, provenance)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::jsonb)
    ON CONFLICT (event_id) DO NOTHING
"""


class TaskEventStore:
    """Thin PG adapter. One lease per call; no implicit schema creation."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # -- internals ------------------------------------------------------
    def _lease(self):
        if self._pool is None:
            raise TaskEventError(MIGRATION_REQUIRED, "no PG pool — task_events needs a database")
        return self._pool.lease(timeout=10.0)

    @staticmethod
    def _is_missing_table(exc: Exception) -> bool:
        text = str(exc).lower()
        return "task_events" in text and ("does not exist" in text or "undefined" in text)

    def append(self, event: TaskEvent) -> str:
        lease = self._lease()
        try:
            cur = lease.connection.cursor()
            cur.execute(
                _INSERT_SQL,
                (
                    event.event_id,
                    event.task_id,
                    event.project,
                    event.event_type,
                    event.status,
                    event.summary,
                    event.source_id,
                    event.occurred_at,
                    json.dumps(event.provenance or {}, ensure_ascii=False),
                ),
            )
            lease.connection.commit()
            return event.event_id  # type: ignore[return-value]
        except Exception as exc:
            try:
                lease.connection.rollback()
            except Exception:
                pass
            if self._is_missing_table(exc):
                raise TaskEventError(
                    MIGRATION_REQUIRED,
                    "public.task_events 不存在 —— 先执行 schema/task_events.sql",
                ) from exc
            raise
        finally:
            try:
                lease.close()
            except Exception:
                pass

    def append_many(self, events: Iterable[TaskEvent]) -> int:
        written = 0
        for ev in events:
            self.append(ev)
            written += 1
        return written

    # -- reads ----------------------------------------------------------
    def timeline(self, task_id: str) -> list[dict]:
        lease = self._lease()
        try:
            cur = lease.connection.cursor()
            cur.execute(
                """
                SELECT event_id, task_id, project, event_type, status, summary,
                       source_id, occurred_at, recorded_at, provenance
                  FROM public.task_events
                 WHERE task_id = %s
                 ORDER BY occurred_at ASC, recorded_at ASC, event_id ASC
                """,
                (task_id,),
            )
            rows = cur.fetchall() or []
        except Exception as exc:
            if self._is_missing_table(exc):
                raise TaskEventError(MIGRATION_REQUIRED, "public.task_events 不存在") from exc
            raise
        finally:
            try:
                lease.close()
            except Exception:
                pass
        return [_row_to_dict(r) for r in rows]

    def state(self, task_id: str) -> TaskState | None:
        events = self.timeline(task_id)
        if not events:
            return None
        goal_event = next(
            (e for e in events if e["event_type"] in ("created", "requirement")), events[0]
        )
        latest = events[-1]
        return TaskState(
            task_id=task_id,
            status=latest["status"],
            latest_event=latest,
            goal=goal_event["summary"],
            created_at=goal_event["occurred_at"],
            events=events,
        )

    def open_tasks(self, project: str | None = None, limit: int = 50) -> list[TaskState]:
        lease = self._lease()
        try:
            cur = lease.connection.cursor()
            sql = """
                WITH latest AS (
                    SELECT DISTINCT ON (task_id) task_id, status
                      FROM public.task_events
                     ORDER BY task_id, occurred_at DESC, recorded_at DESC
                )
                SELECT task_id FROM latest
                 WHERE status NOT IN ('verified_done', 'failed')
                 ORDER BY task_id
                 LIMIT %s
            """
            cur.execute(sql, (limit,))
            ids = [r[0] for r in (cur.fetchall() or [])]
        finally:
            try:
                lease.close()
            except Exception:
                pass
        out: list[TaskState] = []
        for tid in ids:
            st = self.state(tid)
            if st is not None and (project is None or any(e.get("project") == project for e in st.events)):
                out.append(st)
        return out


def _row_to_dict(row: Any) -> dict:
    if isinstance(row, dict):
        data = dict(row)
    else:
        keys = [
            "event_id", "task_id", "project", "event_type", "status", "summary",
            "source_id", "occurred_at", "recorded_at", "provenance",
        ]
        data = {k: row[i] for i, k in enumerate(keys) if i < len(row)}
    for key in ("occurred_at", "recorded_at"):
        val = data.get(key)
        if isinstance(val, (_dt.datetime, _dt.date)):
            data[key] = val.isoformat()
    prov = data.get("provenance")
    if isinstance(prov, str):
        try:
            data["provenance"] = json.loads(prov)
        except Exception:
            pass
    return data


__all__ = [
    "EVENT_TYPES",
    "STATUSES",
    "MIGRATION_REQUIRED",
    "TaskEvent",
    "TaskEventError",
    "TaskEventStore",
    "TaskState",
    "derive_task_id",
    "derive_project",
    "event_id_for",
]
