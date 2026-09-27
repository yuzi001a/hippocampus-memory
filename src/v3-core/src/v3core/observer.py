# -*- coding: utf-8 -*-
"""v3 观察者核心模块 — 滚动观察者实现 (Phase 2)

设计要点:
- 攒批触发: 复制 maybe_compress 骨架 (debounce + import_lock + 后台线程 + alive-check)
- 读新 QA 范围 (id > 上次游标) + 召回候选主题 (TopicMatcher)
- 单次 LLM 调用 (M3) 双输出 JSON: note + topic_updates
- 写 observation_notes 表 + topics 表双更新 (note_ref / last_observer_ts / summary / body)
- 失败不抛, 静默记日志, 下次自动重试
- 所有配置 (endpoint / key / batch_size / model 调参) 走 config.yaml, 不硬编码

复用点 (按 phase2_task_spec.md):
1. compression_trigger.maybe_compress() 骨架 — debounce/import_lock/alive-check/PG DSN
2. topic_refine.TopicMatcher.match(text) — 候选主题召回
3. compression_engine._get_runtime_config() + _resolve_llm_api_key() — 配置 + key
4. phase0_m3_length_test.py call_m3() + _strip_think + _parse_json_lenient

调用契约:
- sync_turn 末尾 (旁路): maybe_observe(config)
- dry-run: maybe_observe(dry_run=True) — 不写库, 只打日志

DDL (幂等, 已通过 docker exec 验证):
  CREATE TABLE IF NOT EXISTS observation_notes (
      id BIGSERIAL PRIMARY KEY,
      version TEXT NOT NULL,
      content TEXT NOT NULL,
      source_qa_range INT8RANGE,
      prev_id BIGINT REFERENCES observation_notes(id),
      links JSONB DEFAULT '[]',
      created_at TIMESTAMPTZ DEFAULT now(),
      embedding VECTOR(1024)
  );
  ALTER TABLE topics ADD COLUMN IF NOT EXISTS note_ref TEXT;
  ALTER TABLE topics ADD COLUMN IF NOT EXISTS last_observer_ts TIMESTAMPTZ;

config.yaml 示例 (可选, 缺则用兜底默认):
  observer:
    enabled: false  # 默认 false — 分支验证期手动开
    batch_size: 10
    idle_minutes: 30
    max_qa_per_run: 20
    max_completion_tokens: 16384
    thinking: disabled  # phase0e 定案
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# v3core.__init__._safe_err 模块级函数, 同包内直接相对导入即可。
# 避免 daemon 线程异常时 NameError → 静默死 → 整条观察者链路断。
from . import _safe_err
from ._deadline import PrefetchDeadlineExceeded

logger = logging.getLogger("v3core.observer")


def _safe_notes_table(notes_table: str) -> str:
    """校验配置表名，避免动态 notes_table 形成 SQL 注入。"""
    value = str(notes_table)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", value):
        raise ValueError(f"非法 notes_table: {value!r}")
    return value

# ────────────────────────────────────────────────────────────
# v3 触发判断 — 纯函数 (无副作用, 便于回放验证)
# ────────────────────────────────────────────────────────────

def _decide_v3_trigger(
    rows: list[Any],
    *,
    known_sessions: set[str],
    last_update_ts: Any,
    now: datetime,
    cfg: dict[str, Any],
    adaptive_batch_active: bool = False,
    trigger_stats: dict[str, Any] | None = None,
    event_time_now: bool = True,
    wall_now: datetime | None = None,
) -> dict[str, Any]:
    """v3 四信号触发判断 — docs/observer-trigger-v2.md / phase8n_task.md

    纯函数: 输入 cursor+ 窗口内的 rows + 历史状态, 返回触发决策。

    Args:
        rows: 来自 qa_pairs 的行列表, 每行形态
            (id, session_id, timestamp, question, answer, chars)
            (即与 _observe_worker #3 步 SELECT 同序)
        known_sessions: 历史已知 session_id 集合 (持久化自 observer_state.json)
        last_update_ts: observation_notes 上一条 created_at
            (用作防抖; None 表示没有上一条)
        now: 当前时间 (datetime, tz-aware 推荐)
        cfg: observer 配置 (从 _load_observer_config() 拿)
        adaptive_batch_active: v4 (2026-08-19) — 当 observer_state.json 里残留
            ``adaptive_batch_size`` (即上一轮多 QA 输出超 8000 软目标, 落自动拆批
            状态) 时, 本轮是"重试批次"。重试批次必须无条件视为已触发 — 不再被
            count/token/new_session/debounce 任意信号挡住, 否则会连续 stall:
            下轮只取到 25 条 (小于 count_threshold=100), 已知 session 不再算
            new_session, 未达 token, 防抖可能未过 → reason=None → cursor 不推
            → 永久 stall。显式传 ``True`` 时直接返回 ``trigger=True`` /
            ``reason='adaptive_split'``, 可审计 (note.links.trigger.reason 一致
            写入)。默认 ``False`` 时旧四信号行为完全不变 — 显式默认值保证
            向后兼容 (旧测试 / 外部调用方零感知)。
        trigger_stats: v5 (2026-08-21) — cursor 之后【全局】未观察数据的统计,
            形态 {"unobs_count": int, "unobs_chars": int,
                  "unobs_by_sess": dict[str, int], "last_qa_ts": datetime|None}。
            设计文档 docs/observer-trigger-v2.md 明确四信号统计对象是"全局未观察
            数据", 而 rows 只是 max_qa_per_run 单次处理上限的批 — 用批内局部累积
            做阈值判断会让 token/count/new_session 在积压 > LIMIT 时永远够不着。
            显式传入时 token/count/new_session 改用 stats 全局值判断 (返回值里的
            unobs_* 也如实回填全局真值); v5.1 起 pause 也用 stats.last_qa_ts
            (cursor 后全局 MAX(timestamp)) — 设计文档规定 pause 是"距上一条
            【任何新 QA】≥ 4h", LIMIT 批之外可能有更新/更早的 QA。
            默认 ``None`` 时行为与旧版完全一致 (旧调用方/旧测试零感知)。
        event_time_now: v5 (2026-08-21) — 显式声明 now 的语义。
            ``True`` (默认) = 事件时间模式: now 原样参与 debounce/pause 判断 —
            与旧版行为逐字段一致 (旧调用方/回放 runner 零感知; 回放依赖此语义,
            且回放常用 debounce_minutes=0, 是显式回放兼容标识)。
            ``False`` = 生产实时模式: debounce/pause 改用 wall-clock
            (wall_now 可注入, None → datetime.now(UTC))。生产里上一版印
            created_at 可能晚于积压 QA 的历史 timestamp (如 cursor 积压两天),
            若拿事件时间当 now, (event_now - prev_created_at) < 0 会把 debounce
            永远压成 False → trigger=False 反复卡死。_observe_worker 实时路径
            必须显式传 event_time_now=False。

    Returns:
        dict {
            "trigger": bool,                                # 是否触发
            "reason": "token|count|new_session|pause|adaptive_split" | None,
            "unobs_chars": int,
            "unobs_count": int,
            "unobs_by_sess": dict[str, int],                 # 各 session 字符累积
            "new_sessions": set[str],                        # 触发判断用的未知 session (含全局)
            "batch_new_sessions": set[str],                  # 本批 rows 实际出现的未知 session
                                                             #  (成功路径写 known_sessions 只能用它)
            "debounce_ok": bool,                             # 是否通过防抖
            "last_qa_ts": datetime | None,                   # pause 实际消费的最新事件时间
            "adaptive_batch_active": bool,                  # 入参回显, 便于审计
        }
    """
    # v4 (2026-08-19) adaptive 重试短路 — adaptive 状态下重试批次必须无条件触发,
    # 否则 count/token/new_session/debounce 任意一信号挡掉都会永久 stall。
    # 短路在累积计算之前 — 即便 rows 为空 (理论上不会到这里, _observe_worker 前面
    # 已 return) 也要返 trigger=True; 同时把 rows 字段如实回填 0, 不撒谎。
    if adaptive_batch_active:
        _unobs_chars = 0
        _unobs_count = 0
        _unobs_by_sess: dict[str, int] = {}
        _last_qa_ts: datetime | None = None
        for r in rows:
            _chars = int(r[5] or 0)
            _unobs_chars += _chars
            _unobs_count += 1
            _sid = str(r[1] or "")
            if _sid:
                _unobs_by_sess[_sid] = _unobs_by_sess.get(_sid, 0) + _chars
            _ts = r[2]
            if _ts is not None and (_last_qa_ts is None or _ts > _last_qa_ts):
                _last_qa_ts = _ts
        # new_sessions 仍照实算 (审计/可观测需要), 但不影响 trigger。
        _new_sessions: set[str] = set()
        for r in rows:
            _sid = str(r[1] or "")
            if _sid and _sid not in known_sessions:
                _new_sessions.add(_sid)
        return {
            "trigger": True,
            "reason": "adaptive_split",
            "unobs_chars": _unobs_chars,
            "unobs_count": _unobs_count,
            "unobs_by_sess": _unobs_by_sess,
            "new_sessions": _new_sessions,
            # v5.1: adaptive 重试批的 rows 就是本批实际处理范围 — 两个集合一致。
            "batch_new_sessions": set(_new_sessions),
            # 防抖/绝对空闲信号对 adaptive 重试无意义; 显式 True 让审计日志一致。
            "debounce_ok": True,
            "last_qa_ts": _last_qa_ts,
            "adaptive_batch_active": True,
        }

    _token_thr = int(cfg.get("token_threshold_chars", 50000))
    _count_thr = int(cfg.get("count_threshold", 100))
    _ns_min = int(cfg.get("new_session_min_chars", 5000))
    _idle_hours = int(cfg.get("abs_idle_hours", 4))
    _debounce_min = int(cfg.get("debounce_minutes", 8))

    # 累积未观察 + 各 session 字符 + 最后一条 QA 时间戳
    # v5 (2026-08-21): trigger_stats 显式提供时, token/count/new_session 的判断
    # 对象是 cursor 后【全局】未观察数据 (docs/observer-trigger-v2.md), rows 只是
    # max_qa_per_run 单次处理批; unobs_* 回填全局真值。v5.1: pause 也用全局
    # MAX(timestamp) (stats.last_qa_ts) — "距上一条【任何新 QA】≥ 4h" 是 cursor 后
    # 全局概念, LIMIT 批之外可能有更新/更早的 QA。默认 None → 批内局部累积,
    # 与旧版逐字段一致。
    _unobs_chars = 0
    _unobs_count = 0
    _unobs_by_sess = {}
    _last_qa_ts = None
    for r in rows:
        _chars = int(r[5] or 0)
        _unobs_chars += _chars
        _unobs_count += 1
        _sid = str(r[1] or "")
        if _sid:
            _unobs_by_sess[_sid] = _unobs_by_sess.get(_sid, 0) + _chars
        _ts = r[2]
        if _ts is not None and (_last_qa_ts is None or _ts > _last_qa_ts):
            _last_qa_ts = _ts

    # v5.1: 本批 rows 实际出现的未知 session — 成功路径写 known_sessions 只能用它。
    # 触发判断可以看全局 stats (new_session 信号), 但 LIMIT 批之外的 session 尚未
    # 被处理, 提前标记 known 会吞掉它们后续的 new_session 观察机会。
    _batch_new_sessions = set()
    for r in rows:
        _sid = str(r[1] or "")
        if _sid and _sid not in known_sessions:
            _batch_new_sessions.add(_sid)

    if trigger_stats is not None:
        try:
            _unobs_chars = int(trigger_stats.get("unobs_chars", 0) or 0)
            _unobs_count = int(trigger_stats.get("unobs_count", 0) or 0)
            _stats_by_sess = trigger_stats.get("unobs_by_sess") or {}
            if isinstance(_stats_by_sess, dict):
                _unobs_by_sess = {
                    str(k): int(v or 0) for k, v in _stats_by_sess.items()
                }
            # v5.1: 全局最新 QA 时间戳 (MAX(timestamp)) — pause 判定基准。
            _stats_last_qa_ts = trigger_stats.get("last_qa_ts")
            if _stats_last_qa_ts is not None:
                _last_qa_ts = _stats_last_qa_ts
        except Exception:
            # stats 形态异常时按旧批内累积处理 — 不因审计字段炸掉触发链
            pass

    # new_session 检测
    # v5: 有全局 stats 时按 session 维度全局累积判断 (新 session 可能整段都在
    # LIMIT 批之外); 无 stats 时保持旧批内行为。
    # v5.1: new_sessions (审计+触发判断) 可含 global-only session;
    # batch_new_sessions 只含本批实际出现的未知 session, 供成功路径保存。
    _new_sessions = set(_batch_new_sessions)
    if trigger_stats is not None:
        for _sid in _unobs_by_sess:
            if _sid and _sid not in known_sessions:
                _new_sessions.add(_sid)
    _new_session_hit = any(
        _unobs_by_sess.get(_sid, 0) >= _ns_min for _sid in _new_sessions
    )

    # 防抖 (基于上一条 observation_notes created_at)
    # obs_notes.created_at 是 TIMESTAMPTZ → tz-aware UTC; 但兜底按 UTC 处理 naive
    # v5 (2026-08-21): event_time_now=False (生产实时) 时 now 必须是 wall-clock —
    # 积压 QA 的事件时间可能早于上一版印 created_at, 拿事件时间当 now 会算出负
    # 间隔把 debounce 永远压成 False (生产 cursor=246491 卡死根因之一)。
    # event_time_now=True (历史回放) 保持调用方传入的 now 原样 — 回放 runner
    # 依赖事件时间语义; debounce_minutes<=0 是回放兼容标识, 直接放行。
    # v5.2 (2026-08-21): 实时模式下 wall_now=None 时直接用 now 参数当墙钟 —
    # 不再偷偷 datetime.now(); wall_now 仍允许显式注入以做测试/审计覆写。
    _debounce_ok = True
    if _debounce_min <= 0:
        # Explicit zero disables debounce; replay event-time must not compare against wall-clock created_at.
        _debounce_ok = True
    elif last_update_ts is not None:
        try:
            _prev_dt = last_update_ts
            if _prev_dt.tzinfo is None:
                _prev_dt = _prev_dt.replace(tzinfo=timezone.utc)
            _now_for_debounce = now
            if not event_time_now:
                # 生产实时: 防抖一律用 wall-clock (now 或显式 wall_now)。
                # wall_now 优先 (便于测试/审计复现); None → 用 now 参数。
                if wall_now is not None:
                    _now_for_debounce = wall_now
            if _now_for_debounce.tzinfo is None:
                _now_aware = _now_for_debounce.replace(tzinfo=timezone.utc)
            else:
                _now_aware = _now_for_debounce
            _debounce_ok = (
                _now_aware - _prev_dt
            ).total_seconds() / 60 >= _debounce_min
        except Exception:
            _debounce_ok = True

    # 绝对空闲 pause: 距上一条【任何新 QA】≥ abs_idle_hours
    # PG qa_pairs.timestamp 是 UTC, 但有时从 connector 出来是 naive — 假设 UTC 兜底
    # v5: pause 用本批最新事件时间 (_last_qa_ts) 对比 now — 生产实时下 wall-clock
    # 只会拉大间隔 (真实空闲 ≥ 4h 判定更稳), 回放模式语义不变。
    # v5.1: trigger_stats 提供 last_qa_ts (全局 MAX(timestamp)) 时 _last_qa_ts 已被
    # 替换为全局值 — 设计文档规定 pause 基准是"上一条【任何新 QA】", 不限于本批。
    _pause_hit = False
    if _last_qa_ts is not None:
        try:
            _lq = _last_qa_ts
            if _lq.tzinfo is None:
                _lq = _lq.replace(tzinfo=timezone.utc)  # PG 时区兜底: UTC
            _now_for_pause = now
            if not event_time_now:
                # v5.2 (2026-08-21): wall_now=None 时用 now 参数当墙钟,
                # 不偷偷 datetime.now()。
                if wall_now is not None:
                    _now_for_pause = wall_now
            if _now_for_pause.tzinfo is None:
                _now_aware = _now_for_pause.replace(tzinfo=timezone.utc)
            else:
                _now_aware = _now_for_pause
            _pause_hit = (
                (_now_aware - _lq).total_seconds() / 3600 >= _idle_hours
                and _unobs_count > 0
            )
        except Exception:
            _pause_hit = False

    # 决策 (任一信号 + 防抖通过)
    _reason: str | None = None
    if _debounce_ok:
        if _unobs_chars >= _token_thr:
            _reason = "token"
        elif _unobs_count >= _count_thr:
            _reason = "count"
        elif _new_session_hit:
            _reason = "new_session"
        elif _pause_hit:
            _reason = "pause"

    return {
        "trigger": _reason is not None,
        "reason": _reason,
        "unobs_chars": _unobs_chars,
        "unobs_count": _unobs_count,
        "unobs_by_sess": _unobs_by_sess,
        "new_sessions": _new_sessions,
        # v5.1: 本批实际出现的未知 session — 成功路径写 known_sessions 只能用它,
        # global-only (LIMIT 批外) session 不能提前标记 known。
        "batch_new_sessions": _batch_new_sessions,
        "debounce_ok": _debounce_ok,
        "last_qa_ts": _last_qa_ts,
        "adaptive_batch_active": False,
    }


def _is_trajectory_session(session_id: str) -> bool:
    """判断是否为 trajectory 系统巡检 session (phase8m 定稿: %.trajectory% 过滤)."""
    return ".trajectory" in (session_id or "")




_DEFAULT_OBSERVER_CONFIG: dict[str, Any] = {
    "enabled": False,             # 默认禁用, 分支验证期手动开
    # ---- v3 四信号模型 (docs/observer-trigger-v2.md / phase8n_task.md) ----
    # ① token: 全局未观察字符 ≥ token_threshold_chars → 触发
    # ② count: 全局未观察条数 ≥ count_threshold → 触发
    # ③ new_session: 新 session 且该 session 累计字符 ≥ new_session_min_chars → 触发
    # ④ pause: 距上一条任何新 QA ≥ abs_idle_hours (绝对空闲) 且未观察 > 0 → 触发
    "token_threshold_chars": 50000,   # ① token (文档: 50k)
    "count_threshold": 100,           # ② count (文档: 100 条)
    "new_session_min_chars": 5000,    # ③ new_session (文档: 5k)
    "abs_idle_hours": 4,              # ④ 绝对空闲 (文档: 4h)
    "debounce_minutes": 8,            # v3 防抖 (文档: 8 分钟)
    # 单次最多读多少条 — 单次处理上限, 跟四信号阈值正交, 保留兼容
    "max_qa_per_run": 30,             # (phase8: 每次观察覆盖 25-30 条合理)
    # ---- 兼容旧键 (过渡期保留, 实际不消费) ----
    "batch_size": 25,             # v3 不再以条数为唯一信号, 仅作旧调用方兜底
    "idle_minutes": 45,           # v3 改用绝对空闲, 旧 idle 兜底保留
    # ---- LLM / prompt 配置 ----
    "max_completion_tokens": 32768,  # phase6 提至 32768: 8000字 输出 + tokens 余量
    "thinking": "disabled",       # phase0e 定案: 禁用思考
    "reasoning_split": True,      # phase0e 定案
    "debounce_seconds": 10,       # 触发节流, 防高频
    "model": None,                # None → 用 llm.model
    "base_url": None,             # None → 用 llm.base_url
    "temperature": 0.3,           # 与 phase0 一致
    "candidate_top_k": 5,         # TopicMatcher 返回前 N
    "candidate_threshold": 0.40,  # MATCH_LOW = 0.40 (topic_refine.py)
    # ---- v4 当轮快照 + 事件时间窗口 ----
    "prompt_note_min_chars": 1500,    # 兜底下限, 真正用软引导
    "prompt_note_max_chars": 8000,    # 正常目标/自动时间拆批触发线, 不是原子 QA 死亡线
    "observer_note_max_chars": 8000,  # 与上一字段同名冗余, 兼容旧 config schema
    "snapshot_window_days": 7,        # 近期连续性窗口, 按 QA 事件时间计算
    "snapshot_max_chars": 200000,     # 软安全阈值; worker 超过只告警、不截断
    "topic_store_strict": True,       # eval/observer 闭环不能让 SQLite 掩盖 PG 失败
    # ---- 种子压缩配置 (phase6+phase7) ----
    # 完整印 → 种子印: 500-800字硬约束, 每天日终走一次, 失败 fallback 完整印
    "observer_seed_min_chars": 500,    # 种子印下限
    "observer_seed_max_chars": 800,    # 种子印上限
    "seed_compress_enabled": False,    # v4: 种子压缩已退役, 默认关闭; 显式 True 才启用兼容分支
    "seed_max_completion_tokens": 4096,  # 种子压缩 max_tokens, 比主 M3 小
}

# 触发节流 (秒) — 每次 maybe_observe 调用前查这个时间戳
_LAST_ATTEMPT_KEY = "_last_observe_attempt"

# 后台 worker 引用 — alive-check 守卫 (P10b 同一模式)
_WORKER_THREAD: "threading.Thread | None" = None


# ────────────────────────────────────────────────────────────
# v4 hard cap — 观察者印单条内容上限 (字)
# ────────────────────────────────────────────────────────────
# v4 (2026-08-18) 当轮快照模式: 单条 observation_notes.content 强制 ≤ 8000 字。
# 调用方 (_observe_worker) 在写印前显式传 max_chars=V4_NOTE_MAX_CHARS,
# 超限 fail-closed (返 None, 不写库, 不推进 cursor)。
# 公共 writer (_write_observation_note 默认参数) 不受影响 — cold_start 等
# 调用方不传 max_chars, 默认 None = 不校验, 维持现有行为。
V4_NOTE_MAX_CHARS: int = 8000


# v4 snapshot 拼接溢出异常 — _load_recent_snapshots fail-closed 用
class _SnapshotOverflowError(RuntimeError):
    """v4: 拼接最近 snapshot 总长超过 max_chars 边界 → fail-closed 抛出。

    不允许 joined[-max_chars:] 静默截断 (会丢前段 snapshot、给 LLM 看半截上下文);
    不允许返 "前段已截断" 标记字符串 — 这是 v3 旧设计, 违反 v4 fail-closed 语义。
    调用方 (_observe_worker) 必须捕获此异常: warning + 关闭/回滚连接 + return,
    不能调 LLM, 不能写 note, 不能推进 cursor。
    """


# ────────────────────────────────────────────────────────────
# v4 event_time link 元数据 — 7 天窗口的历史回放锚点
# ────────────────────────────────────────────────────────────
# v4 (2026-08-19): ``links`` 数组里标记本 note 关联的 QA 事件时间窗的 link kind 常量。
# 旧 note 没有该字段时回退 ``created_at``; SQL 仍按 ``created_at`` 排序,
# 之后由 Python 按事件时间稳定排序 — 保留旧 schema/fake cursor 契约。
# 同时作为 ``_load_recent_snapshots`` 在历史回放时按 event_time 窗口过滤的依据。
EVENT_TIME_LINK_KIND: str = "event_time"


def _iso_utc(value: Any) -> str | None:
    """把 datetime / str 规范化为 ``ISO 8601 UTC 带时区`` 字符串。

    用途: 写 ``links[kind=event_time].start`` / ``.end`` 时, PG JSONB 持久化
    之前由 Python 端统一格式 (避免 naive/aware 混用导致 SQL 比较错位)。
    不接 ``ms / μs / 10 位 epoch``, 这些故意不支持 — 与 v3 ``_utc`` 解析
    路径保持对称, 写啥读啥。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str):
        # 已是字符串时不再二次解析, 信任调用方 (写路径控制格式只此一处)。
        return str(value)
    return str(value)


def _parse_iso_utc(value: Any) -> datetime | None:
    """ISO 8601 字符串 → UTC-aware datetime (失败返 None)。

    与 ``_load_recent_snapshots`` 内部 ``_utc`` 抽离拆分出来的纯函数,
    给 ``_parse_event_time_from_links`` 共用。任何解析失败返 None,
    不抛 — caller 容忍"没有 event_time link"的旧 note。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _parse_event_time_from_links(links: Any) -> tuple[datetime | None, datetime | None]:
    """从 ``links`` 纯数据里取 ``(event_start, event_end)`` — 排除 DB/IO 副作用。

    找 ``kind in {EVENT_TIME_LINK_KIND, "source_event"}`` (兼容旧 v3 source_event)
    的 link, 优先 start/end, 兜底 event_start/event_end。
    """
    if isinstance(links, str):
        try:
            links = json.loads(links)
        except Exception:
            return None, None
    if isinstance(links, dict):
        links = [links]
    if not isinstance(links, list):
        return None, None
    for link in links:
        if not isinstance(link, dict):
            continue
        if link.get("kind") not in {EVENT_TIME_LINK_KIND, "source_event"}:
            continue
        start = _parse_iso_utc(link.get("start") or link.get("event_start"))
        end = _parse_iso_utc(link.get("end") or link.get("event_end"))
        if start or end:
            return start or end, end or start
    return None, None


def _build_event_time_link(
    start: Any | None,
    end: Any | None,
) -> dict[str, Any] | None:
    """构造 ``{"kind": EVENT_TIME_LINK_KIND, start, end}`` link, 写 note 时调用。

    任何一侧为 None 时只写有值的那一侧; 双侧 None 返 None (表达"无法定事件时间")。
    """
    if start is None and end is None:
        return None
    return {
        "kind": EVENT_TIME_LINK_KIND,
        "start": _iso_utc(start),
        "end": _iso_utc(end),
    }


# ────────────────────────────────────────────────────────────
# v4 chunk_event_stream — 纯函数, 按 session/time/完整 QA 边界拆批
# ────────────────────────────────────────────────────────────
# v4 (2026-08-19): worker 在 _load_recent_snapshots 之前用本函数把 (id, session_id,
# timestamp, question, answer, chars) 形态的 rows 切成可注入 prompt 的批。
#
# 拆批原则:
# 1) 不切 question/answer — 完整 QA 原子保留, 不能从 Q 中间断开
# 2) 不切 session — 同一 session_id 必须在同一批 (跨 session 上下文会损害连续性)
# 3) 不切 time 段 — 同 session 内 time 段连续, 不能从相邻 QA 之间断开
# 4) 单条超长 (len(q)+len(a) > max_chars) — 原子保留, 不截断, 单独成批
# 5) overflow 拆批安全预览接点 — 调 ``safe_preview`` 估算本批大小, 避免 prompt 截断
#
# 调用方传入 ``max_chars`` (字符数, 上限) + 可选 ``safe_preview`` (返长度,
# 默认为 ``len("".join(...))`` 估算)。返回 list[list[row]] 外层是批, 内层是
# 完整 QA 列表。
def chunk_event_stream(
    rows: list[Any],
    *,
    max_chars: int,
    safe_preview: Any | None = None,
) -> list[list[Any]]:
    """按 session/time/完整 QA 边界切 rows, 不切 question/answer。

    Args:
        rows: 形如 ``(id, session_id, timestamp, question, answer, chars)`` 的行列表
            (与 ``_observe_worker`` 步骤 3 的 SELECT 同序); 支持任意可索引序列。
        max_chars: 单批字符上限 (软目标, 8000 由 ``prompt_note_max_chars`` 提供)。
        safe_preview: 接收 list[row] 返字符数 (int) 的可调用; 用来在塞 question/answer
            之前估算 prompt 注入长度 (避免溢出)。 默认为 ``sum_chars``。

    Returns:
        list[list[row]] — 拆分后的批列表。若 rows 为空, 返 ``[[]]`` 占位避免
        调用方除零。若 max_chars <= 0, 单条超长也视为合法 (1 row / 1 batch)。
    """
    if not rows:
        return [[]]
    try:
        _max = max(1, int(max_chars))
    except Exception:
        _max = 1

    def _sum_chars(batch: list[Any]) -> int:
        total = 0
        for r in batch:
            try:
                # chars=col 5, 兜底 len(question)+len(answer)
                c = r[5] if len(r) >= 6 else None
                if c is None:
                    q = r[3] if len(r) >= 4 else ""
                    a = r[4] if len(r) >= 5 else ""
                    total += len(str(q or "")) + len(str(a or ""))
                else:
                    total += int(c)
            except Exception:
                pass
        return total

    preview = safe_preview if callable(safe_preview) else _sum_chars

    batches: list[list[Any]] = []
    current: list[Any] = []
    current_size = 0
    current_session: Any = None

    for r in rows:
        try:
            r_session = r[1] if len(r) >= 2 else None
            r_chars = r[5] if len(r) >= 6 else None
        except Exception:
            r_session = None
            r_chars = None
        if r_chars is None:
            try:
                _q = r[3] if len(r) >= 4 else ""
                _a = r[4] if len(r) >= 5 else ""
                r_chars = len(str(_q or "")) + len(str(_a or ""))
            except Exception:
                r_chars = 0

        # 单条超长原子保留 — 单独成批, 不拆 QA
        if r_chars > _max:
            if current:
                batches.append(current)
                current = []
                current_size = 0
                current_session = None
            batches.append([r])
            continue

        # 跨 session 边界 — 当前批已满, 强制开新批
        if current and r_session is not None and current_session is not None \
                and r_session != current_session:
            batches.append(current)
            current = []
            current_size = 0
            current_session = None

        # 触发安全预览 — 若把本条加进去超 max_chars, 先 flush 当前批
        projected = preview(current + [r]) if current else r_chars
        if current and projected > _max:
            batches.append(current)
            current = []
            current_size = 0
            current_session = None
            projected = r_chars

        if not current:
            current_session = r_session
        current.append(r)
        current_size = projected if projected > current_size else current_size + r_chars

    if current:
        batches.append(current)
    return batches if batches else [[]]


# ────────────────────────────────────────────────────────────
# 配置解析 — 不硬编码, 全走 config.yaml
# ────────────────────────────────────────────────────────────

def _load_observer_config() -> dict[str, Any]:
    """从 v3-core config.yaml 读 observer 段, 缺失用 _DEFAULT_OBSERVER_CONFIG 兜底。

    走 resolve_config(return_legacy=True) — 这条路径只读 yaml 不依赖 PG 密码强校验。
    """
    cfg = dict(_DEFAULT_OBSERVER_CONFIG)  # 拷贝兜底默认
    try:
        from .config import resolve_config
        legacy = resolve_config(return_legacy=True)
        if isinstance(legacy, dict):
            obs = legacy.get("observer")
            if isinstance(obs, dict):
                cfg.update(obs)
    except Exception as e:
        logger.warning("读 observer 配置失败, 用兜底默认: %s", _safe_err(e)[:120])
    return cfg


# ────────────────────────────────────────────────────────────
# 游标持久化 — observer_state.json (跟 compression_trigger 共存目录)
# ────────────────────────────────────────────────────────────

def _state_path(cfg: dict[str, Any] | None = None):
    """观察状态文件路径 — 可配置 (回放/隔离时指向独立文件, 不污染生产游标)。

    路径纪律 (2026-08-12 事故后):
    - 优先 cfg.state_path, 必须绝对路径 (Windows Path 不展开 ~)
    - 无显式 cfg 时从 config.yaml observer.state_path 兜底
    - 禁止回落到源码树路径 — 运行时状态一律走数据目录
    """
    if cfg and cfg.get("state_path"):
        # Windows Path 不展开 ~ — 显式 expanduser, 防相对 cwd 的幽灵状态文件 (2026-08-12 事故根因)
        return Path(str(cfg["state_path"])).expanduser()
    try:
        from .config import resolve_config
        legacy = resolve_config(return_legacy=True) or {}
        obs = legacy.get("observer") or {}
        if obs.get("state_path"):
            return Path(str(obs["state_path"])).expanduser()
    except Exception:
        pass
    # 最终兜底: 生产数据目录 (非源码树)
    return Path.home() / ".v3-core" / "profiles" / "default" / "observer_state.json"


def _load_cursor(cfg: dict[str, Any] | None = None) -> int:
    """读 observer_state.json 的 last_qa_id 游标, 缺则返 0。"""
    path = _state_path(cfg)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return int(data.get("last_qa_id", 0))
    except Exception:
        pass
    return 0


def _save_cursor(last_qa_id: int, cfg: dict[str, Any] | None = None) -> None:
    """更新游标 (失败不抛, 下次重试)。

    v3: 同时累加 known_sessions 列表 (持久化到 cursor 同文件 key: known_sessions),
    用来在下一个 worker run 判断 new_session — 重启后不丢历史。
    """
    path = _state_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 读到现有 + 增量更新, 别直接覆盖 (留扩展位)
        old: dict[str, Any] = {}
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                old = {}
        old["last_qa_id"] = int(last_qa_id)
        old["updated_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(
            json.dumps(old, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("保存 observer 游标失败(下次重试): %s", _safe_err(e)[:120])


def _save_known_sessions(new_session_ids: set[str], cfg: dict[str, Any] | None = None) -> None:
    """v3: 累计保存 known_sessions, 用于 new_session 信号判断。

    写入同 observer_state.json, key=known_sessions (list[str])。
    失败不抛 — 仅下次 new_session 判定可能略多触发, 不影响数据正确性。"""
    path = _state_path(cfg)
    try:
        old: dict[str, Any] = {}
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                old = {}
        known = set(old.get("known_sessions", []) or [])
        known.update(new_session_ids)
        old["known_sessions"] = sorted(known)
        path.write_text(
            json.dumps(old, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("保存 known_sessions 失败(下次可能略多触发): %s", _safe_err(e)[:120])


# ────────────────────────────────────────────────────────────
# PG 连接构造 — 跟 compression_trigger 同一模式 (config.yaml 全走, 不硬编码)
# ────────────────────────────────────────────────────────────

def _build_pg_dsn() -> str:
    """从 config.yaml 的 pg 段拼 dsn, 缺字段走 compression_trigger 默认。

    缺 PG 配置抛 RuntimeError — 不抛就静默不报, 排查成本高。
    """
    try:
        from .config import resolve_config
        legacy = resolve_config(return_legacy=True)
    except Exception as e:
        raise RuntimeError(
            f"observer: 读 config.yaml 失败: {_safe_err(e)[:120]}"
        ) from e
    if not isinstance(legacy, dict):
        raise RuntimeError("observer: config.yaml 不是 dict 形态")
    # 2026-08-09: 兼容 V3Config 平铺 pg (顶层) — 旧 storage.pg 结构兼容保留
    pg = (legacy.get("storage", {}) or {}).get("pg", {}) or legacy.get("pg", {})
    if not pg:
        # 用默认 (跟 maybe_compress 一致)
        return (
            "host=localhost port=5433 dbname=v3embeddings "
            "user=v3user password="
        )
    return (
        f"host={pg.get('host', 'localhost')} "
        f"port={pg.get('port', 5433)} "
        f"dbname={pg.get('database', 'v3embeddings')} "
        f"user={pg.get('user', 'v3user')} "
        f"password={pg.get('password', '')}"
    )


def _observer_resolve_api_key() -> tuple[str, str | None]:
    """返回 (api_key, None|skip_reason)。只读, 不改变现有 key 优先级。

    Credential compatibility closure (P1.1): config api_key/apiKey 优先,
    回退 MINIMAX_CN_API_KEY -> MINIMAX_API_KEY, 与 LLMClient 同 precedence。
    """
    try:
        from .config import resolve_config
        _llm_cfg = resolve_config(return_legacy=True) or {}
        _llm = _llm_cfg.get("llm", {}) if isinstance(_llm_cfg, dict) else {}
        api_key = (
            _llm.get("api_key")
            or _llm.get("apiKey")
            or os.environ.get("MINIMAX_CN_API_KEY")
            or os.environ.get("MINIMAX_API_KEY")
            or ""
        )
    except Exception as e:
        logger.warning("observer: 读 LLM 配置失败: %s", _safe_err(e)[:120])
        return "", "config_error"
    if not api_key:
        return "", "credential_missing"
    return api_key, ""


# ────────────────────────────────────────────────────────────
# M3 调用 — 复制 phase0_m3_length_test.py 的 call_m3 (参数已实验定案)
# ────────────────────────────────────────────────────────────

def call_m3(
    cfg: dict[str, Any],
    api_key: str,
    prompt: str,
    *,
    max_completion_tokens: int = 16384,
    thinking: str = "disabled",
    reasoning_split: bool = True,
    timeout: int = 180,
) -> dict[str, Any]:
    """调 M3 Chat Completion, 返 {text, usage, latency}。

    实验定案参数 (phase0_m3_length_test.py L95 + phase0e 验证):
    - max_completion_tokens=16384 (旧 max_tokens 已弃用)
    - thinking={"type": "disabled"} — 跳过思考直接答
    - reasoning_split=True — thinking 拆到 reasoning_content, 不进 content
    """
    import requests
    base_url = cfg.get("base_url") or cfg.get("llm_base_url") or "https://api.minimaxi.com/v1"
    model = cfg.get("model") or cfg.get("llm_model") or "MiniMax-M3"
    proxy = cfg.get("proxy") or cfg.get("llm_proxy")
    # 2026-08-09: timeout 支持 cfg 配置 — 大输入+大输出 (印 2-3万字 + 50 QA) M3 处理慢,
    # 默认 180s 不够 (实测连续超时); 生产可配 timeout: 300
    timeout = int(cfg.get("timeout", timeout) or timeout)
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": cfg.get("temperature", 0.3),
        "max_completion_tokens": max_completion_tokens,
        "thinking": {"type": thinking},
        "reasoning_split": reasoning_split,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    t0 = time.time()
    try:
        if proxy:
            resp = requests.post(
                url, json=payload, headers=headers, timeout=timeout,
                proxies={"http": proxy, "https": proxy},
            )
        else:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        latency = time.time() - t0
    except Exception as e:
        return {"error": f"请求失败: {_safe_err(e)[:200]}", "latency": time.time() - t0}
    if resp.status_code != 200:
        return {
            "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
            "latency": latency,
        }
    try:
        data = resp.json()
    except Exception as e:
        return {"error": f"响应非 JSON: {_safe_err(e)[:200]}", "latency": latency}
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    text = msg.get("content", "") or ""
    reasoning = msg.get("reasoning_content", "") or ""
    usage = data.get("usage") or {}
    return {
        "text": text,
        "reasoning": reasoning,
        "usage": usage,
        "latency": latency,
    }


# ────────────────────────────────────────────────────────────
# JSON 容错解析 — 复用 phase0 的 _strip_think + _parse_json_lenient
# ────────────────────────────────────────────────────────────

def _strip_think(text: str) -> str:
    """剥离 M3 thinking 块 (<!-- ... -->) 和代码块围栏, 返回纯 JSON 文本。

    phase0 原始版只剥 XML 注释; M3 thinking 输出格式多变, 这里
    按"剥离后是否像 JSON"决定是否继续: 剥完不是 {/[, 就再剥一次。
    """
    t = text.strip()
    # 第一次: 常规 XML 注释剥离
    t = re.sub(r"<!--.*?-->", "", t, flags=re.DOTALL).strip()
    if not t.startswith(("{", "[")):
        # 剥完不像 JSON, 再从原文剥一次宽松版 (含跨行)
        t = re.sub(r"<!--[\s\S]*?-->", "", text.strip()).strip()
    # 去掉 ```json ... ``` 围栏
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _parse_json_lenient(text: str) -> tuple[dict[str, Any], bool]:
    """容错解析: 先标准 loads, 失败则逐层修复. 三层修复 (phase7 升级同步自 phase0).

    修复顺序:
    ① 控制字符清洗: 裸 0x00-0x1f 全清 (含裸换行 \\n \\r — M3 会在 JSON 字符串内输出)
    ② 逐字符反斜杠修复: 非法转义 (Windows 路径单反斜杠) → 双写; 合法转义 (\\" \\\\ \\/ \\b \\f \\n \\r \\t \\u) 透传
    ③ 逐字符裸引号修复: ASCII 双引号在字符串内 (后接非 ,}]:空格) → 中文「」

    Returns: (obj, used_lenient)
    """
    try:
        return json.loads(text), False
    except json.JSONDecodeError:
        pass
    # ① 清裸控制字符 — 包括 \\n \\r 真实字节 (M3 在长 content 里会输出裸换行)
    #    注意: 真实字节 \\x0a 会被清, 但 `\\n` 两字符转义序列 (反斜杠+n) 不受影响
    text = re.sub(r"[\x00-\x1f]", "", text)
    # ② + ③ 合并逐字符修复 (反斜杠 + 引号)
    fixed: list[str] = []
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if not in_str:
            if ch == '"':
                in_str = True
                fixed.append(ch)
            else:
                fixed.append(ch)
        else:
            if ch == "\\":
                nxt = text[i + 1] if i + 1 < len(text) else ""
                if nxt in ('"', "\\", "/", "b", "f", "n", "r", "t", "u"):
                    # 合法 JSON 转义, 原样透传
                    fixed.append(ch)
                    if i + 1 < len(text):
                        fixed.append(text[i + 1])
                        i += 1
                else:
                    # 非法转义 — Windows 路径 C:\\Users 的单反斜杠, 双写补救
                    fixed.append("\\\\")
            elif ch == '"':
                nxt = text[i + 1] if i + 1 < len(text) else ""
                if nxt in (",", "}", "]", ":", "\n", " ", "\t", "\r"):
                    # 字符串边界引号, 闭合
                    in_str = False
                    fixed.append(ch)
                else:
                    # 字符串内裸引号 — 转中文「」(避免与 JSON 结构引号冲突)
                    fixed.append("「")
            else:
                fixed.append(ch)
        i += 1
    fixed_text = "".join(fixed)
    return json.loads(fixed_text), True  # 失败让异常自然冒泡, _parse_observer_json 接住


def _parse_observer_json(text: str) -> dict[str, Any] | None:
    """解析 M3 输出 → {note: {...}, topic_updates: [...]}。失败返 None。

    三层恢复 (2026-08-20 stage-2 补洞):
    ① 严格 ``json.loads`` — 输入就是合法 JSON。
    ② ``_parse_json_lenient`` 容错 — 反斜杠/裸引号/控制字符修复。
    ③ ``json.JSONDecoder().raw_decode`` 仅恢复"第一个完整 JSON 对象" — 用于
       ``Extra data: line 1 column N (char N)`` 这类 M3 输出"前段是合法
       JSON 对象 + 尾部跟解释/第二段 JSON"的情形 (历史 cursor=1417 单条 stall
       已真实观察到此错误)。只接受 ``dict`` 且顶层必须含 ``note`` dict —
       其它形态 (list / 顶层无 note) 一律不接受。尾部"额外内容 / 第二段 JSON"
       仅作为可观测 warning 记录, 不影响判定。

    截断 / 缺 ``note`` / 顶层非 dict — 失败返 None, 不抛。
    """
    if not text:
        return None
    stripped = _strip_think(text)
    if not stripped:
        logger.warning("observer: M3 输出剥离后为空")
        return None
    obj: Any
    used_lenient = False
    try:
        obj, used_lenient = _parse_json_lenient(stripped)
    except Exception as e:
        obj = None
        # 进入第 ③ 层: raw_decode 恢复"前段 JSON + 后段多余文本"
        logger.warning(
            "observer: JSON 严格+容错解析失败, 尝试 raw_decode 恢复第一个有效对象: %s",
            _safe_err(e)[:120],
        )
    if obj is None:
        try:
            _decoder = json.JSONDecoder()
            _recovered, _end_idx = _decoder.raw_decode(stripped)
            if not (isinstance(_recovered, dict) and isinstance(_recovered.get("note"), dict)):
                logger.warning(
                    "observer: raw_decode 解析首段非 dict 或缺 note dict, 拒绝: %r",
                    list(_recovered.keys()) if isinstance(_recovered, dict) else type(_recovered).__name__,
                )
                return None
            # 检查尾部是否还有多余内容 (含空白/第二段 JSON/解释文本) — 只 warning, 不拒绝
            _tail = stripped[_end_idx:].strip()
            if _tail:
                logger.warning(
                    "observer: raw_decode 成功但尾部残留 %d 字符 (首个完整 JSON 对象后) — "
                    "接受首段, 丢弃尾部 (历史 cursor=1417 single-segment stall 根因之一): %s",
                    len(_tail), _tail[:80],
                )
            else:
                logger.warning("observer: raw_decode 成功 — 首段完整 JSON 对象, 无尾部残留")
            obj = _recovered
            used_lenient = True
        except Exception as _e_raw:
            logger.warning(
                "observer: JSON raw_decode 恢复也失败: %s", _safe_err(_e_raw)[:120],
            )
            # 三层恢复全失败 — 跟原实现一致, 把原始输出落盘排查
            try:
                from pathlib import Path as _P
                _dbg = _P(__file__).parent.parent.parent / "experiments" / "reimport" / "observer_failed_json.txt"
                _dbg.write_text(stripped[:20000], encoding="utf-8", errors="replace")
            except Exception:
                pass
            return None
    if used_lenient:
        logger.info("observer: JSON 走容错修复成功 (字符串内裸引号替换 / raw_decode 恢复)")
    if not isinstance(obj, dict):
        logger.warning("observer: 解析结果不是 dict 而是 %s", type(obj).__name__)
        return None
    if "note" not in obj or not isinstance(obj["note"], dict):
        logger.warning("observer: 缺 note 段或 note 非 dict: %r", list(obj.keys()))
        return None
    if "topic_updates" not in obj or not isinstance(obj["topic_updates"], list):
        logger.warning("observer: 缺 topic_updates 段或非 list, 置空: %r",
                       list(obj.keys()))
        obj["topic_updates"] = []
    return obj


# ────────────────────────────────────────────────────────────
# Prompt 骨架 — Hermes 式分节 + 8000 软引导 (phase7 升级)
#
# 设计基线:
# - phase6 STRUCTURED_PROMPT 验证有效 (10 天 9/10 成功, 长度甜点 ~8000)
# - 借鉴 Hermes context_compressor 分节构造: 固定 ## 节名 + 模型按槽填充
# - 长度从 1500-2500 硬约束 → 目标 ~8000 软引导 (信息量驱动)
# - 增加 PRESERVE/UPDATE/REMOVE 更新规则 — 承接上一版不重复
#
# 关键差异 vs 旧版:
# - 节化: 叙事更结构 (动机 / 决策 / 进行中 / 教训 / 操作知识 / 主题)
# - 长度: 软引导而非硬约束, 照顾内容实际信息量
# - 更新: 加 PRESERVE + 不重复, 滚动模式
# ────────────────────────────────────────────────────────────


# ── 观察者维度注册表 (2026-08-08 积木化 — 与 EXTRACTOR_PROMPTS 同构) ──
# 每个维度 = 印的一个 ## 节。default=True 默认启用; 用户可在 config.yaml
# observer.dimensions 增删维度 (新增 = {"section": "节名", "prompt": "指导语", "default": true})
OBSERVER_DIMENSIONS: dict[str, dict] = {
    # v4 第三轮 (2026-08-20) — 维度节改为「仅在本轮有相关变化时输出, 没变化就省略」。
    # 节名保持兼容; 每节 prompt 不再强制存在, 不再要求长期叙事更新,
    # 不再硬性要求每节必备。旧调用方按节名引用仍能命中。
    "story": {
        "section": "我的故事（时间线叙事, 动机优先）",
        "prompt": (
            "**仅在本段 QA 改变了{_id}的故事线时输出** (身份 / 关系 / 判断的延伸), "
            "没变化就省略该节。\n"
            "开头一句交代这段对话的动机, 然后**简短**写出「这一段 QA 让{_id}的故事线"
            "如何续了一笔」 — 从{_id}的视角写本轮变化, 事件内嵌 `[#N]` 标记。\n"
            "**不要**把本轮变化升级为长期叙事更新 — 人格层 (E1) 已经承载长期叙事, "
            "这里只写当轮变化。不要重写历史, 不要复述上一轮快照。"
        ),
        "default": True,
    },
    "decisions": {
        "section": "关键决策（拍板的决策 + 理由）",
        "prompt": (
            "**仅在本段 QA 出现新的拍板决策时输出**, 没新决策就省略该节。\n"
            "拍板的决策 + 决策理由; 每行一个 `[#N]` 来源。"
        ),
        "default": True,
    },
    "progress": {
        "section": "进行中状态（未完事项）",
        "prompt": (
            "**仅在本段 QA 改变进行中状态时输出**, 没变化就省略该节。\n"
            "未完成的 / 待验证的 / 下一步要做的, 按「问题-当前状态-下一步」三个角度写。"
        ),
        "default": True,
    },
    "lessons": {
        "section": "教训与踩坑",
        "prompt": (
            "**仅在本段 QA 出现新的踩坑 / 教训时输出**, 没新教训就省略该节。\n"
            "踩的坑 + 学到的教训, 每条 `[#N]`。"
        ),
        "default": True,
    },
    "ops": {
        "section": "操作知识（路径/命令/端口/容器名）",
        "prompt": (
            "**仅在本段 QA 出现新的可复用操作知识时输出**, 没新知识就省略该节。\n"
            "可复用的操作知识, 每行一个, 带 `[#N]` 来源。\n"
            "格式推荐: `类型: 具体值` 例: `命令: xray run -c /etc/xray/config.json`。"
        ),
        "default": True,
    },
    "topics": {
        "section": "主题变化",
        "prompt": (
            "**仅在本段 QA 影响主题卡内容时输出**, 没影响就省略该节。\n"
            "一段文字概括这段对话带来的主题更新; 但**结构化细节走 topic_updates**, "
            "不要在这里列 JSON。"
        ),
        "default": True,
    },
    "state": {
        "section": "系统状态摘要（状态卡输入源）",
        "prompt": (
            "**仅在本段 QA 改变系统/项目当前状态时输出**, 没系统状态变化就省略"
            "该节\n"
            "把本段对话涉及的**系统/项目当前状态**按主题切分成短段, 每段 100-200 字, "
            "段首加 `[状态卡: <主题名>]` 前缀。\n"
            "**这是给「状态卡召回」用的** — 承载「系统现在什么状态/怎么操作/关键配置/"
            "当前进度」, 语义密度高, 去噪, 不写关系/情感。\n"
            "格式: `[状态卡: <主题名>]` 换行 `<100-200 字: 当前状态 + 关键变化 + 操作要点>`。\n"
            "与「操作知识」节的区别: 操作知识是通用可复用知识; 状态摘要节是**当前时点的"
            "系统快照**。"
        ),
        "default": True,
    },
    "relationship": {
        "section": "关系记忆",
        "prompt": (
            "**仅在本段 QA 改变用户与{_id}的关系定义时输出**, 没变化就省略该节。\n"
            "写时保留对话里的原词原意 (双方约定怎么称呼就怎么写), 不抽象成「规则/机制」。"
        ),
        "default": True,
    },
}

def _resolve_observer_dimensions(cfg) -> dict[str, dict]:
    """解析观察者维度 — 默认注册表 + config.yaml observer.dimensions 增删。

    2026-08-08 积木化: 用户可加自定义维度 ({"section": 节名, "prompt": 指导语}),
    或把 default 维度设 enabled: false 关闭。
    """
    dims = dict(OBSERVER_DIMENSIONS)
    if cfg is None:
        return dims
    try:
        overrides = None
        if hasattr(cfg, "observer") and cfg.observer:
            overrides = getattr(cfg.observer, "dimensions", None)
        elif isinstance(cfg, dict):
            overrides = cfg.get("observer", {}).get("dimensions")
        if not overrides:
            return dims
        for name, spec in overrides.items():
            if spec is False or (isinstance(spec, dict) and spec.get("enabled") is False):
                dims.pop(name, None)
            elif isinstance(spec, dict):
                dims[name] = {
                    "section": spec.get("section", dims.get(name, {}).get("section", name)),
                    "prompt": spec.get("prompt", dims.get(name, {}).get("prompt", "")),
                    "default": True,
                }
    except Exception:
        pass  # 配置解析失败回落默认注册表
    return dims

def _build_dimensions_block(dims: dict[str, dict], _id: str) -> str:
    """把维度注册表拼成 prompt 的分节指令块（## 节 + 指导语）"""
    sections = []
    for name, spec in dims.items():
        sec = spec.get("section", name)
        prompt = spec.get("prompt", "")
        if prompt:
            sections.append(f"## {sec}\n{prompt.format(_id=_id)}")
        else:
            sections.append(f"## {sec}\n[按主题填写]")
    return "\n\n".join(sections)


def _observer_prompt(
    prev_note_text: str,
    qa_text: str,
    candidates_text: str,
    note_min: int = 1500,
    note_max: int = 8000,
    now_str: str = "",
    qa_time_range: str = "",
    identity_name: str = "",
    dims: dict[str, dict] | None = None,
    # v4 第二轮收口 (2026-08-20): 人格层仍注入, 7 天内观察者快照印 section 已删除
    # — snapshot_text 形参**仅作旧调用兼容**保留 (worker 显式传空字符串, 函数体
    # 不再消费)。人格层 + 候选主题 + QA + 上一轮快照 (prev_note_text) = 完整 4 路
    # 上下文来源, 没有 5th snapshot section。
    identity_layer: str = "",
    snapshot_text: str = "",  # 仅旧调用兼容; 函数体不消费, 见 docstring
) -> str:
    """观察者 prompt 骨架 — 双输出 JSON {note, topic_updates}.

    v4 第三轮 (2026-08-20) — "E1 长期人格层 + Observer 本轮变化快照" 设计收口。

    设计目标:
    - 人格层 (``identity_layer``, E1 y_*.md 合成) 提供长期身份背景;
    - Observer 每一轮只处理**当前 batch** 的新 QA, 生成一份**当轮变化快照**;
    - 上一轮观察快照 (``prev_note_text``) 仅作连续性/变更对照**基线**,
      不得作为本轮事实来源, 不得被整篇复述;
    - 候选主题仅用于 ``topic_updates`` 的 ``update`` / ``create`` 判断,
      不得直接写入 ``note.content``;
    - 输出长度按本轮变化量自然伸缩, 不为凑 note_min/note_max 注水, 也不
      为"覆盖完整性"无目标放大。

    输入清单:
    - 输入 0 = 人格层 (E1, 低频合成, 由 worker 注入)
    - 输入 1 = 上一轮观察快照 (最新一条 ``observation_notes.content``, prev_note_text)
    - 输入 2 = 本轮新 QA 对 (qa_text)
    - 输入 3 = 候选主题 (candidates_text)

    本轮新增事实只能来自当前 batch 的 qa_text (加 candidates 用来判断 update/create)。
    上一轮快照里出现的旧事件**不要**整篇复述, 只在对比 prev/curr 时点名变化;
    未在本轮出现的事件, 不得从 prev 中"延续"或"猜补"。

    唯一快照输入契约: ``prev_note_text`` 是 prompt 内唯一可消费的快照内容;
    ``snapshot_text`` 形参**仅作旧调用兼容**保留, 函数体不再把它渲染成任何
    7 天快照 section。禁止任何形式的 7 天 ``observation_notes`` 全文注入
    (没有 7 天窗口, 没有 snapshot_text section, 没有 _load_recent_snapshots
    拼装)。人格层 (``identity_layer``) 是唯一保留的额外 context section。

    维度注册表 (OBSERVER_DIMENSIONS) 节名与字段保持兼容; 各节指导语改为
    "仅在本轮有相关变化时输出, 没变化就省略" — 不再要求每个维度必备,
    不再要求把本轮变化升级为长期叙事更新。默认 8 节依然注册可用, 旧调用方按节名引用
    仍然能命中。

    长度策略:
    - 8000 是 ``writer`` 端的观测/超限标记 (oversize_batch), 不是 prompt 目标;
    - ``note_min`` / ``note_max`` 只是 writer 软参考, prompt 不要求为达到
      下限而填充, 也不要求为达到上限而扩展;
    - 信息量少自然写短, 信息量多自然写长;
    - 不要为填满章节把同一事实复述三遍, 也不要为凑完整而塞无关内容。

    Args:
        prev_note_text: 上一轮观察快照 (``observation_notes.content``)。
                        仅作连续性/变更对照基线, **不是本轮事实**。
                        空字符串 = 首次观察, 跳过 PREV/CURR 对照, 只写当轮变化。
        qa_text: 新对话 QA 对拼成的字符串。**本轮新事实唯一真值来源**。
        candidates_text: 候选主题列表字符串。仅作 ``update`` / ``create``
                        判断; 不得直接写入 ``note.content``。
        identity_name: 身份名 (开源中性化: 默认空 = 用"我"第一人称;
                       用户可在 config ``prompts.observer_identity`` 配置)。
        note_min: writer 软下限, 默认 1500 字。prompt 不要求为达到下限而填充;
                 仅作 writer 观测参考。
        note_max: writer 软上限, 默认 8000 字。prompt 不要求为达到上限而扩展;
                 仅作 writer 超限标记线 (超过会落 ``oversize_batch``,
                 不静默丢弃)。
        identity_layer: 人格层 (E1 y_*.md 合成) — prompt 末尾
                       ``_v4_context_block`` 唯一保留的额外 context section。
                       空字符串 = 首次观察或 E1 未跑。
        snapshot_text: **仅作旧调用兼容保留** (v4 第三轮收口)。函数体不再渲染
                      任何 7 天快照 section, 不再消费此参数。worker 必须显式传
                      空字符串 ``""``; 任何非空值会被静默丢弃。禁止往此参数注入
                      7 天 ``observation_notes`` 全文。
    """
    _id = identity_name or "我"  # 2026-08-08: 身份中性化 (开源)
    # 2026-08-08 积木化: 维度注册表动态组装分节块
    _dims = dims if dims is not None else OBSERVER_DIMENSIONS
    # v4 第三轮收口 (2026-08-20)：维度节按需输出 (按 evidence 选择, 没变化就省略),
    # 维度注册表 (OBSERVER_DIMENSIONS) 保留兼容, 但 prompt body 只渲染一段
    # 紧凑的"按需"提示, 不再用 ## 大块重复每节指导语 — 旧实现把 8 个 section
    # 逐一渲染成 ## 槽位, 模型即使看到"可选"也常常全量填充。
    _dim_block_lines = []
    for _dn, _ds in _dims.items():
        _sec = _ds.get("section", _dn)
        _dim_block_lines.append(
            f"- {_sec}: **仅在本段 QA 出现该类变化时输出**, 没变化就省略该节 (按需, 可选)。"
        )
    _dim_block = "\n".join(_dim_block_lines) or "- 本轮只写 QA 明确支持的变化; 没变化就省略该节 (按需, 可选)。"
    # v4 第三轮收口 (2026-08-20): _v4_context_block 只包含人格层 section。
    # 7 天内观察者快照印 section 已删除 — snapshot_text 形参仅作旧调用兼容
    # 保留 (函数体不再消费它)。LLM 看到的人格层 (identity_layer) 是连续
    # 身份状态, 与 7 天快照印无关; 上一轮快照承接由 prev_note_text 显式段
    # 负责 (见下方 return 模板)。
    _id_section = (
        identity_layer.strip()
        if (identity_layer and identity_layer.strip())
        else "(尚无人格层 — 首次观察或 E1 未跑)"
    )
    # snapshot_text 形参保留 (旧调用方兼容) 但函数体内不渲染任何 7 天 section
    _v4_context_block = (
        "\n\n## 【人格层 (v4 phase-1, identity_layer)】\n"
        f"{_id_section}"
    )
    return f"""你是{_id}的「本轮变化观察者 (当轮变化快照生成器)」。

## v4 第三轮 (2026-08-20) 设计契约要点 — 必须遵守
- 本轮新事实唯一真值来源 = 本轮新 QA (qa_text); prev/candidates/人格层 都不当事实来源, 不写入 note.content 的事实只能来自 qa_text。
- E1 人格层 = 长期身份/关系/判断基线 (identity_layer); 当轮变化走 Observer, 不向长期叙事升级。
- 上一轮观察快照 (prev_note_text) = 连续性参考, 不复制其中句子, 不当事实来源。
- 候选主题 (candidates_text) = 仅用于 topic_updates 的 update/create 判断, 不写入 note.content, 不当事实来源。
- 维度节 (OBSERVER_DIMENSIONS 维度注册表中的 ## 节) = 按需输出 (按本轮证据), 没变化就省略该节 (按需, 可选)。不允许为了凑 note_min/note_max 强制填满 8 节, 不允许覆盖完整性 (硬约束)。
- 输出长度按变化量 (按本轮信息量) 自然伸缩, 不为凑字数注水; 信息量少自然写短, 几百字到一两千字都合理; 长度不再被旧的固定目标值或冗余策略指令放大。
- note_min / note_max 只是 writer 软参考; 8000 是 writer 超限标记 (oversize_batch 观测), 不是本 prompt 目标; 不靠截断/删尾解决长度。

## 四路输入 (职责严格分开)
1. 【人格层 / identity_layer】长期身份背景。它帮助你理解本轮变化, 但不当本轮事实, 不要整段复述, 也不直接写入 note.content。
2. 【上一轮观察快照 / prev_note_text】连续性参考 (连续性对照材料, 连续性参考基线)。只用来判断本轮相对上一轮发生了什么, 不复制其中的句子/事实列表/段落; 没有本轮证据的内容不要再次写入; 不当事实来源。
3. 【本轮新 QA / qa_text】本轮新事实唯一真值来源 / 本轮事实唯一来源 / 本轮新事实唯一。所有事件、决策、教训、偏好、操作知识都必须能在这里找到依据。
4. 【候选主题 / candidates_text】只用于决定 topic_updates 是 update 还是 create; 候选主题不写入 note.content, 也不把候选主题本身当事实来源, 不写入 note.content。

## 时间锚点
当前时间: {now_str}
本次 QA 时间范围: {qa_time_range}
日期必须使用完整 YYYY-MM-DD; 不要编造 QA 中没有的时间。

## 输出目标
- note.content 是当轮变化快照 / 本轮变化 / 本轮事实 / 本轮新事实, 不是背景资料汇编。只写本轮 QA 支持的变化, 写清就停。
- 只输出有本轮证据的 section; 没有变化的 section 省略该节 (按需, 可选), 不输出空标题, 不为了凑数量把同一事实放进多个 section。
- 本轮信息少就写短; 本轮只有一两条变化时, 几句话或一两个小段完全足够。长度按变化量伸缩, 不要按 note_min / note_max 填充。
- 上一轮观察快照和人格层只用于理解变化, 不能替代本轮 QA; 候选主题只用于主题动作判断 (update/create)。
- 每条关键事实、决策、教训或偏好在原文旁紧跟 `[ #N ]` 的无空格形式 `[#N]`, N 是本轮 QA 序号。
- 主题卡只记录值得长期检索的变化: 可匹配已有候选时 update, 否则确有独立价值时 create; 纯寒暄和重复确认可以不建卡。
- new_facts 只写最重要的新事实并保留原词, tags 保留用户可能实际搜索的原词或专名; 不要把主题动作重复写成 note 正文, 不写入 note.content 的事实。

## 维度节 (按 evidence 输出, 没变化就省略该节, 按需, 可选)
{_dim_block}

## 长度与写入边界
`note_min` 和 `note_max` 是 writer 的软参考 (writer 软参考); 8000 字是 writer 的 oversize_batch 超限标记 (writer 超限标记, oversize), 不是本 prompt 的写作目标。有效内容应完整输出, 不靠截断或删尾解决长度问题。覆盖完整性不再硬约束 (按变化量伸缩)。

## JSON 输出契约
只输出一个 JSON 对象, 不要 markdown 围栏或额外解释。`topic_updates` 可以为空; 有主题变化时按下列字段填写:
{{{{
  "note": {{{{
    "content": "## <当轮变化 / 本轮确有变化的 section>\\n<当轮变化 + 来源 [#N]>",
    "source_qa_range": [起始id, 结束id]
  }}}},
  "topic_updates": [
    {{{{
      "action": "update 或 create",
      "topic_id": "已有候选的 id; create 时留空",
      "title": "主题标题",
      "summary": "当轮变化摘要 / 本轮变化摘要",
      "new_facts": "新增事实[#N]",
      "tags": ["原词标签"]
    }}}}
  ]
}}}}

## 人格层 (仅作长期背景, 不写入 note.content 的事实)
{_id_section}

## 上一版观察笔记（## 【上一版观察笔记】 — 连续性参考, 仅作对照, 不复制）
{prev_note_text or "(空: 首次观察, 只依据本轮 QA 输出)"}

## 本轮新 QA (本轮新事实唯一真值来源, 本轮事实唯一来源)
{qa_text}

## 候选主题 (只决定 topic_updates, 不写入 note.content)
{candidates_text}"""


# ────────────────────────────────────────────────────────────
# 候选主题召回 — TopicMatcher
# ────────────────────────────────────────────────────────────

class _PoolPgConnection:
    """Wrapper around a pool-leased PostgreSQL connection for observer operations.

    Delegates cursor/commit/rollback/autocommit/etc. to the underlying physical connection,
    and exposes _connect() to return the underlying connection if needed.
    Calling .close() on this wrapper is a no-op so that caller cleanup (such as early returns)
    never physically closes the shared connection owned by PgLease.
    """

    def __init__(self, raw_conn: Any, release: Any = None) -> None:
        self._raw_conn = raw_conn
        self._release = release
        self._released = False

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        return self._raw_conn.cursor(*args, **kwargs)

    def commit(self) -> None:
        if hasattr(self._raw_conn, "commit"):
            self._raw_conn.commit()

    def rollback(self) -> None:
        if hasattr(self._raw_conn, "rollback"):
            self._raw_conn.rollback()

    def close(self) -> None:
        # Never close the physical connection directly. In pool mode, release only
        # the lease; PgPool remains the owner of the backend lifecycle.
        if self._released:
            return
        self._released = True
        if self._release is not None:
            self._release()

    def _connect(self) -> Any:
        return self._raw_conn

    @property
    def autocommit(self) -> bool:
        return getattr(self._raw_conn, "autocommit", False)

    @autocommit.setter
    def autocommit(self, val: bool) -> None:
        if hasattr(self._raw_conn, "autocommit"):
            self._raw_conn.autocommit = val

    @property
    def closed(self) -> bool:
        return getattr(self._raw_conn, "closed", False)

    def is_connected(self) -> bool:
        return not getattr(self._raw_conn, "closed", False)

    def __enter__(self) -> Any:
        if hasattr(self._raw_conn, "__enter__"):
            return self._raw_conn.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
        if hasattr(self._raw_conn, "__exit__"):
            return self._raw_conn.__exit__(exc_type, exc_val, exc_tb)
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw_conn, name)


def _recall_candidates(
    qa_text: str,
    cfg: dict[str, Any],
    top_k: int = 5,
    threshold: float = 0.40,
    topics_table: str = "topics",
    pool: Any = None,
    pg: Any = None,
) -> list[dict[str, Any]]:
    """候选主题召回 — 2026-08-06: 改 PG 直查。

    SQLite topic_blocks 双写断链只剩 80 张 (PG topics 688 是真值) — 读 SQLite 会让
    观察者看不到 600+ 已有卡 → LLM 不知道重复 → 重复 create。
    PG 直查: qa_text embedding → topics hnsw 向量 top_k (cosine ≥ threshold)。
    失败兜底: 返回空列表, 不阻塞观察者主流程。
    """
    try:
        from .config import resolve_config
        from .embedding import call_embedding, safe_embed_cfg

        cfg_obj = resolve_config()
        # 阶段1 (2026-08-20): 唯一构造入口 — safe_embed_cfg 工厂.
        # 缺 model/endpoint → None (disabled), 缺端点导致 call_embedding 立即 raise.
        embed_cfg = safe_embed_cfg(cfg_obj)
        if embed_cfg is None:
            return []
        q_emb = call_embedding(qa_text[:1000], embed_cfg, timeout=2.5, retries=0)
        if not q_emb:
            return []
        emb_str = "[" + ",".join(str(x) for x in q_emb) + "]"

        out: list[dict[str, Any]] = []

        if pg is not None:
            conn = pg._connect() if hasattr(pg, "_connect") else pg
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT topic_id, title, COALESCE(body, ''), 1 - (embedding <=> %s::vector) AS cosine "
                    f"FROM {topics_table} WHERE status='active' AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (emb_str, emb_str, top_k),
                )
                for tid, title, body, cosine in cur.fetchall():
                    sim = float(cosine or 0)
                    if sim >= threshold:
                        out.append({"topic_id": tid, "title": title or "", "body": body or "", "sim": round(sim, 4)})
            return out

        if pool is not None:
            lease = pool.lease(timeout=5)
            try:
                conn = lease.connection
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT topic_id, title, COALESCE(body, ''), 1 - (embedding <=> %s::vector) AS cosine "
                        f"FROM {topics_table} WHERE status='active' AND embedding IS NOT NULL "
                        "ORDER BY embedding <=> %s::vector LIMIT %s",
                        (emb_str, emb_str, top_k),
                    )
                    for tid, title, body, cosine in cur.fetchall():
                        sim = float(cosine or 0)
                        if sim >= threshold:
                            out.append({"topic_id": tid, "title": title or "", "body": body or "", "sim": round(sim, 4)})
            finally:
                lease.close()
            return out

        # PG 连接参数（V3Config 平铺结构: cfg.pg — 2026-08-09 修复旧 cfg.storage.pg 访问,
        # 导致 pg_cfg 空 → 连 localhost:5433 → 候选召回永远 candidate_miss）
        pg_cfg = {}
        try:
            _pg_c = getattr(cfg_obj, "pg", None)
            if _pg_c is not None:
                if isinstance(_pg_c, dict):
                    pg_cfg = _pg_c
                else:
                    pg_cfg = {
                        "host": getattr(_pg_c, "host", "localhost"),
                        "port": getattr(_pg_c, "port", 5433),
                        "database": getattr(_pg_c, "database", "v3embeddings"),
                        "user": getattr(_pg_c, "user", "v3user"),
                        "password": getattr(_pg_c, "password", ""),
                    }
        except Exception:
            legacy = resolve_config(return_legacy=True) or {}
            pg_cfg = (legacy.get("storage", {}) or {}).get("pg", {}) or {}
        import psycopg2
        conn = psycopg2.connect(
            host=pg_cfg.get("host", "localhost"),
            port=pg_cfg.get("port", 5433),
            dbname=pg_cfg.get("database", "v3embeddings"),
            user=pg_cfg.get("user", "v3user"),
            password=pg_cfg.get("password", ""),
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT topic_id, title, COALESCE(body, ''), 1 - (embedding <=> %s::vector) AS cosine "
                    f"FROM {topics_table} WHERE status='active' AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (emb_str, emb_str, top_k),
                )
                for tid, title, body, cosine in cur.fetchall():
                    sim = float(cosine or 0)
                    if sim >= threshold:
                        out.append({"topic_id": tid, "title": title or "", "body": body or "", "sim": round(sim, 4)})
        finally:
            conn.close()
        return out
    except ValueError:
        raise
    except Exception as e:
        logger.warning("observer: 召回候选主题失败(非致命), 走空候选: %s",
                       _safe_err(e)[:120])
        # 2026-08-07: 回补机制 — 失败标记由调用方写入印的 links (candidate_miss=1),
        # 补盲脚本扫描该标记后重跑候选匹配。这里返回特殊标记便于调用方感知。
        return [{"__candidate_miss__": True}]


# ────────────────────────────────────────────────────────────
# DB 写入 — observation_notes + topics 更新
# ────────────────────────────────────────────────────────────

def _ensure_embedding_column() -> None:
    """观察笔记表已建好 (idempotent DDL), 这里只校验 vector 列可用。

    如果 pgvector 未启用, 后续写 embedding 会抛 — 兜底处理: embedding 写失败不阻塞主流程。
    """
    return None  # DDL 已通过 docker exec 验证, 此函数留作扩展位


def _write_observation_note(
    pg,
    version: str,
    content: str,
    qa_range_start: int | None,
    qa_range_end: int | None,
    prev_id: int | None,
    links: list[Any] | None = None,
    seed_text: str | None = None,
    notes_table: str = "observation_notes",
    max_chars: int | None = None,
    oversize_atomic: bool = False,
    cfg: dict | None = None,
) -> int | None:
    """写 observation_notes 表, 返新 id。失败返 None。

    phase7 新增 seed_text 参数: 种子印 (500-800 字) 直接存进 links JSONB 数组,
    形态为 {"kind": "seed", "content": "..."} — 不改 DDL, 走 links 复用通道。
    下次观察时: 若种子印可用, 用种子印做 prev_note; 否则退到完整印。

    v4 (2026-08-19) — max_chars + oversize_atomic 语义:
    - max_chars=None (默认) → 不做长度校验, 维持旧行为 (cold_start 等公共调用方不受影响)。
    - max_chars=int + oversize_atomic=False → content > max_chars 时 fail-closed: warning +
      直接返 None, 不 INSERT、不 commit、不调 embedding (旧契约)。
    - max_chars=int + oversize_atomic=True → 调用方明确允许超长写入 (单条 QA 原子保留
      场景), 跳过 max_chars 校验, 仍然不截断 content (4000 是软目标不是死亡线)。
      调用方负责在 links 中加 kind=oversize_atomic 标记, 便于后续观测。
    """
    if max_chars is not None and not oversize_atomic and len(content) > max_chars:
        logger.warning(
            "observer: _write_observation_note 拒绝写入 — content 长度 %d > max_chars %d (v4 soft target)",
            len(content), max_chars,
        )
        return None
    if qa_range_start is not None and qa_range_end is not None:
        # INT8RANGE 用 '[start, end)' 格式 (半开区间)
        range_text = f"[{qa_range_start},{qa_range_end})"
    else:
        range_text = None
    # 合并 links + seed_text (后者追加), 避免覆盖 caller 传入的 link
    merged_links = [x for x in (links or []) if x is not None]
    if seed_text:
        merged_links.append({
            "kind": "seed",
            "version": version,
            "content": seed_text,
            "char_count": len(seed_text),
        })
    try:
        from pgvector.psycopg2 import register_vector
        # P1.2-B: pgvector 0.5.0 只接受原生 connection/cursor — 解包 pool wrapper。
        _pg_raw = pg._connect() if hasattr(pg, "_connect") else pg
        register_vector(_pg_raw)
    except Exception:
        pass
    with pg.cursor() as cur:
        cur.execute(
            """
            INSERT INTO {notes_table}
                (version, content, source_qa_range, prev_id, links)
            VALUES (%s, %s, %s::int8range, %s, %s::jsonb)
            RETURNING id
            """.format(notes_table=notes_table),
            (
                version,
                content,
                range_text,
                prev_id,
                json.dumps(merged_links, ensure_ascii=False),
            ),
        )
        row = cur.fetchone()
        new_id = int(row[0]) if row else None
    pg.commit()
    # 2026-08-07 通读审查修复(风险1): INSERT 不含 embedding 列 → 观察者印永远 NULL,
    # recall_pool notes 路径 WHERE embedding IS NOT NULL 恒空。写印后立即回填,
    # 失败不阻塞主流程(回扫脚本 backfill_missing_embeddings.py 留作兜底)。
    if new_id and content:
        try:
            # 阶段1.5 (2026-08-20): 把 cfg 一并传给 backfill, 让 UPDATE 写 embed_model
            # 列. cfg 为 None 时 _backfill_note_embedding 会自取 resolve_config + safe_embed_cfg.
            _backfill_note_embedding(pg, new_id, content, notes_table=notes_table,
                                     cfg=cfg)
        except ValueError:
            raise
        except Exception as e:
            logger.warning("observer: 印 embedding 回填失败(不阻塞, 靠回扫补): %s",
                           _safe_err(e)[:120])
        return new_id


# ── Long-observation derived index v1 (docs/LONG-OBSERVATION-INDEX.md) ──
#
# The source row is already committed by _write_observation_note before any of
# this runs. Derived work must never roll back (or block) the source insert.
_OBSERVATION_CHUNK_TABLE = "observation_embedding_chunks"
_OBSERVATION_ENTITY_TABLE = "observation_notes"


def _is_absent_table_error(err: Exception) -> bool:
    """仅判“表不存在”类错误 — sidecar 预迁移 legacy-start 回退用。

    覆盖 psycopg2 UndefinedTable (pgcode 42P01) 与 sqlite
    OperationalError ("no such table")。禁止 catch-all。
    """
    if getattr(err, "pgcode", None) == "42P01":
        return True
    if type(err).__name__ == "UndefinedTable":
        return True
    msg = str(err).lower()
    if "no such table" in msg:
        return True
    if "relation" in msg and "does not exist" in msg:
        return True
    return False


def _observation_sidecar_present(pg) -> bool:
    """sidecar 表是否存在 — to_regclass 探针, 永不抛错 (报错按存在处理)。

    探针本身不在派生事务里: 预迁移 schema 下直接走 legacy 回退, 不 abort
    调用方事务 (e1 以 commit=False 复用外层事务, 一次 abort 就会毁掉 INSERT)。
    """
    try:
        with pg.cursor() as cur:
            cur.execute("SELECT to_regclass('observation_embedding_chunks')")
            row = cur.fetchone()
        return bool(row and row[0])
    except Exception:
        return True


def _record_observation_derived_failure(pg, note_id: int, phase: str,
                                        error: BaseException, embed_cfg: dict,
                                        fp: str, *, commit: bool = True) -> bool:
    """为 observation 派生失败记 durable marker — 只存分类, 永不存原文。

    record_embedding_failure 只持久化 error_class / retryable / provider
    status / model 指纹与 error 指纹 (sha256 截断), 不存 source 文本、API
    key 或完整 provider 响应。永不抛错。
    """
    try:
        from .embed_failures import record_embedding_failure
        return bool(record_embedding_failure(
            conn=pg,
            entity_table=_OBSERVATION_ENTITY_TABLE,
            entity_id=str(note_id),
            phase=phase,
            error=error,
            model=(embed_cfg.get("model") or "") if embed_cfg else "",
            model_fingerprint=fp or "",
            commit=commit,
        ))
    except Exception:
        logger.warning("observer: observation 派生失败 marker 记录异常 (note=%s phase=%s)",
                       note_id, phase, exc_info=True)
        return False


def _unwrap_embedding_error(err: BaseException):
    """沿 __cause__/__context__ 找内层 EmbeddingCallError (分类用)。"""
    try:
        from .embedding import EmbeddingCallError
    except Exception:
        return None
    seen: set[int] = set()
    cur: BaseException | None = err
    depth = 0
    while cur is not None and depth < 8 and id(cur) not in seen:
        if isinstance(cur, EmbeddingCallError):
            return cur
        seen.add(id(cur))
        nxt = cur.__cause__ if cur.__cause__ is not None else cur.__context__
        cur = nxt if isinstance(nxt, BaseException) else None
        depth += 1
    return None


def _update_observation_parent_vector(pg, note_id: int, vec: list,
                                      fp: str, table: str, *, commit: bool) -> None:
    """legacy parent-only UPDATE — 缺 embed_model 列时走旧 SQL。

    供 planner 缺席的 legacy 路径与 short 路径的 sidecar 缺席回退复用。
    """
    import json as _json
    try:
        with pg.cursor() as cur:
            cur.execute(
                f"UPDATE {table} SET embedding=%s::vector, embed_model=%s WHERE id=%s",
                (_json.dumps(vec), fp, note_id),
            )
        if commit:
            pg.commit()
        logger.info("observer: 印 id=%s embedding 已回填 (%d 维, fp=%s)",
                    note_id, len(vec), fp)
    except Exception as _e_ins:
        from .pg_store import _is_undefined_column_error
        if _is_undefined_column_error(_e_ins):
            try:
                pg.rollback()
            except Exception:
                pass
            logger.warning(
                "observer._backfill_note_embedding: %s 缺 embed_model 列, "
                "走旧 SQL (DEFAULT '' 由迁移后 schema 接住): %s",
                table, _safe_err(_e_ins)[:120],
            )
            with pg.cursor() as cur:
                cur.execute(
                    f"UPDATE {table} SET embedding=%s::vector WHERE id=%s",
                    (_json.dumps(vec), note_id),
                )
            if commit:
                pg.commit()
            logger.info("observer: 印 id=%s embedding 已回填 (%d 维, "
                        "embed_model 列由 DEFAULT '' 接住)",
                        note_id, len(vec))
        else:
            raise


def _update_short_observation_note(pg, note_id: int, vec: list, fp: str,
                                   *, notes_table: str, commit: bool) -> None:
    """short 路径: 同一事务内清 stale sidecar 子行 + 更新 parent 向量。

    sidecar 表缺席 (预迁移) 时走 parent-only legacy 回退, 不 abort 事务。
    """
    import json as _json
    table = _safe_notes_table(notes_table)
    if _observation_sidecar_present(pg):
        with pg.cursor() as cur:
            cur.execute(f"SELECT version FROM {table} WHERE id=%s", (note_id,))
            _vrow = cur.fetchone()
        _version = _vrow[0] if _vrow else None
        try:
            with pg.cursor() as cur:
                if _version is not None:
                    cur.execute(
                        f"DELETE FROM {_OBSERVATION_CHUNK_TABLE} "
                        "WHERE observation_id=%s AND observation_version=%s",
                        (note_id, _version),
                    )
                cur.execute(
                    f"UPDATE {table} SET embedding=%s::vector, embed_model=%s WHERE id=%s",
                    (_json.dumps(vec), fp, note_id),
                )
            if commit:
                pg.commit()
            logger.info("observer: 印 id=%s embedding 已回填 (%d 维, fp=%s, short)",
                        note_id, len(vec), fp)
            return
        except Exception as _e_tx:
            from .pg_store import _is_undefined_column_error
            if _is_absent_table_error(_e_tx):
                try:
                    pg.rollback()
                except Exception:
                    pass
                if not commit:
                    raise
                logger.warning("observer: sidecar 表缺席, short 印走 parent-only 回退 (id=%s)",
                               note_id)
            elif _is_undefined_column_error(_e_tx):
                # parent 缺 embed_model 列 (旧 schema): 重做 DELETE + 旧 parent SQL。
                try:
                    pg.rollback()
                except Exception:
                    pass
                try:
                    with pg.cursor() as cur:
                        if _version is not None:
                            cur.execute(
                                f"DELETE FROM {_OBSERVATION_CHUNK_TABLE} "
                                "WHERE observation_id=%s AND observation_version=%s",
                                (note_id, _version),
                            )
                        cur.execute(
                            f"UPDATE {table} SET embedding=%s::vector WHERE id=%s",
                            (_json.dumps(vec), note_id),
                        )
                    if commit:
                        pg.commit()
                    logger.info("observer: 印 id=%s embedding 已回填 (%d 维, short, "
                                "embed_model 列由 DEFAULT '' 接住)", note_id, len(vec))
                    return
                except Exception:
                    try:
                        pg.rollback()
                    except Exception:
                        pass
                    raise
            else:
                try:
                    pg.rollback()
                except Exception:
                    pass
                raise
    _update_observation_parent_vector(pg, note_id, vec, fp, table, commit=commit)


def _backfill_long_observation_note(pg, note_id: int, content: str, plan,
                                    embed_cfg: dict, fp: str,
                                    *, notes_table: str, commit: bool,
                                    target_tokens=None) -> None:
    """long 路径: 全子行逐个 embed → 一事务内 scoped 替换 + parent 聚合。

    1. plan 校验与全部 provider 调用都在派生事务之外; 任一子行失败则
       不写 parent、不提交、不留半成品 (all-child gate)。
    2. 提交事务只做: scoped DELETE → 全量 INSERT → parent UPDATE →
       该实体全部 unresolved phase 的 D5 entity-level resolve — 一次提交。
    3. 事务内任何异常都 rollback (source 行早已提交, 不受影响), 记
       observation_long_sidecar marker 后返回, 不抛错。
    """
    import json as _json
    from .embedding import embed_batch
    from .observation_chunks import (
        OBSERVATION_REPRESENTATION_VERSION,
        ObservationChunkPlanError,
        ObservationDerivedStateError,
        build_observation_derived_state,
        validate_observation_chunk_plan,
    )
    try:
        validate_observation_chunk_plan(
            content, plan, target_tokens, embed_cfg=embed_cfg
        )
    except ObservationChunkPlanError as _e_plan:
        _record_observation_derived_failure(
            pg, note_id, "observation_long_plan", _e_plan,
            embed_cfg, fp, commit=commit)
        return

    def _embed_one(text: str):
        # one provider call per planned chunk; 长 chunk 必须用完整文本
        # key (one-chunk-one-vector 契约, 见 embed_batch collision_safe_key)。
        _vecs = embed_batch([text], embed_cfg, retries=2,
                            collision_safe_key=True)
        return _vecs[0]

    try:
        derived = build_observation_derived_state(
            plan, _embed_one, model_fingerprint=fp)
    except ObservationDerivedStateError as _e_derived:
        _root = _unwrap_embedding_error(_e_derived) or _e_derived
        _phase = ("observation_long_sidecar"
                  if "parent aggregation" in str(_e_derived)
                  else "observation_long_child")
        _record_observation_derived_failure(
            pg, note_id, _phase, _root, embed_cfg, fp, commit=commit)
        return

    table = _safe_notes_table(notes_table)
    with pg.cursor() as cur:
        cur.execute(f"SELECT version FROM {table} WHERE id=%s", (note_id,))
        _vrow = cur.fetchone()
    _version = _vrow[0] if _vrow else None
    if _version is None:
        _record_observation_derived_failure(
            pg, note_id, "observation_long_sidecar",
            RuntimeError(f"observation note id={note_id} 缺失, 无法定位派生版本"),
            embed_cfg, fp, commit=commit)
        return
    if not _observation_sidecar_present(pg):
        # legacy-start: 表未迁移 — source 已持久, parent 置空, marker 可重试。
        _record_observation_derived_failure(
            pg, note_id, "observation_long_sidecar",
            RuntimeError("observation_embedding_chunks 表缺席 (预迁移), "
                         "long parent 延迟到迁移后"),
            embed_cfg, fp, commit=commit)
        return

    _pairs = list(zip(plan.chunks, derived.child_embeddings))
    _parent_json = _json.dumps(derived.parent_embedding)

    def _run_derived_statements(include_model: bool) -> None:
        with pg.cursor() as cur:
            # scoped 当前版本全量替换 — 4→3 等 stale 子行在此 DELETE。
            cur.execute(
                f"DELETE FROM {_OBSERVATION_CHUNK_TABLE} "
                "WHERE observation_id=%s AND observation_version=%s",
                (note_id, _version),
            )
            for _chunk, _vec in _pairs:
                if include_model:
                    cur.execute(
                        f"INSERT INTO {_OBSERVATION_CHUNK_TABLE} "
                        "(observation_id, observation_version, chunk_index, "
                        " source_start, source_end, source_sha256, chunk_sha256, "
                        " token_count, representation_version, embedding, "
                        " embed_model, content) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s)",
                        (note_id, _version, int(_chunk.chunk_index),
                         int(_chunk.source_start), int(_chunk.source_end),
                         plan.source_sha256, _chunk.source_sha256,
                         int(_chunk.token_count),
                         OBSERVATION_REPRESENTATION_VERSION,
                         _json.dumps(_vec), fp, _chunk.text),
                    )
                else:
                    cur.execute(
                        f"INSERT INTO {_OBSERVATION_CHUNK_TABLE} "
                        "(observation_id, observation_version, chunk_index, "
                        " source_start, source_end, source_sha256, chunk_sha256, "
                        " token_count, representation_version, embedding, "
                        " content) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s)",
                        (note_id, _version, int(_chunk.chunk_index),
                         int(_chunk.source_start), int(_chunk.source_end),
                         plan.source_sha256, _chunk.source_sha256,
                         int(_chunk.token_count),
                         OBSERVATION_REPRESENTATION_VERSION,
                         _json.dumps(_vec), _chunk.text),
                    )
            if include_model:
                cur.execute(
                    f"UPDATE {table} SET embedding=%s::vector, embed_model=%s "
                    "WHERE id=%s",
                    (_parent_json, fp, note_id),
                )
            else:
                cur.execute(
                    f"UPDATE {table} SET embedding=%s::vector WHERE id=%s",
                    (_parent_json, note_id),
                )
        # 成功派生即按 D5 entity-level 语义关闭该实体全部 unresolved phase
        # (live_ingest / j_import / backfill …), 与上面同属一个事务。
        from .embed_failures import resolve_embedding_failures_for_entity
        with pg.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM embedding_failures "
                "WHERE entity_table=%s AND entity_id=%s AND resolved_at IS NULL",
                (_OBSERVATION_ENTITY_TABLE, str(note_id)),
            )
            _unresolved_before = int((cur.fetchone() or [0])[0] or 0)
        _resolved = resolve_embedding_failures_for_entity(
            conn=pg,
            entity_table=_OBSERVATION_ENTITY_TABLE,
            entity_id=str(note_id),
            resolution="long_observation_derived",
            commit=False,
        )
        if _unresolved_before and not _resolved:
            raise RuntimeError(
                "D5 entity-level failure resolution returned no change for "
                f"{_unresolved_before} unresolved observation markers"
            )

    try:
        try:
            _run_derived_statements(True)
        except Exception as _e_first:
            from .pg_store import _is_undefined_column_error
            if not _is_undefined_column_error(_e_first):
                raise
            try:
                pg.rollback()
            except Exception:
                pass
            _run_derived_statements(False)
    except Exception as _e_tx:
        try:
            pg.rollback()
        except Exception:
            pass
        _record_observation_derived_failure(
            pg, note_id, "observation_long_sidecar", _e_tx,
            embed_cfg, fp, commit=commit)
        return
    try:
        if commit:
            pg.commit()
    except Exception as _e_commit:
        try:
            pg.rollback()
        except Exception:
            pass
        _record_observation_derived_failure(
            pg, note_id, "observation_long_sidecar", _e_commit,
            embed_cfg, fp, commit=commit)
        return
    logger.info("observer: 长印 id=%s 派生已提交 (%d 子行, parent %d 维, fp=%s)",
                note_id, len(_pairs), len(derived.parent_embedding), fp)


def _backfill_note_embedding(pg, note_id: int, content: str,
                             notes_table: str = "observation_notes",
                             cfg: dict | None = None,
                             *,
                             commit: bool = True,
                             tokenizer_override=None,
                             target_tokens: int | None = None) -> None:
    """写印后立即算 embedding 回填 — 2026-08-07 通读审查风险1修复。

    从 ``cfg`` (必须来自 ``build_embed_cfg`` 工厂) 拿 ``_fingerprint``, 写到
    ``embed_model`` 列. **有向量写库时必须有 fingerprint** — 严禁手拼 / 默认 /
    零向量兜底.

    旧 schema 兼容: 表缺 ``embed_model`` 列时, UPDATE 走 fallback 旧 SQL,
    仅 catch psycopg2 "undefined_column" / "column does not exist", 不静默吞
    配置 / 指纹错误.

    cfg 传 None 时, 自动尝试 ``resolve_config() + safe_embed_cfg`` 工厂.

    Long-observation derived index v1 (docs/LONG-OBSERVATION-INDEX.md §7):
    source 行已由调用方先行提交 — 本函数只做派生。planner 可用时按完整
    content 判定 short/long: short 走既有单请求全量 parent 路径 (加 scoped
    stale 子行清理, 无 sidecar 写入); long 对每个 planned 子行各发一次
    provider 调用, 全子行成功才聚合 parent, 并与 sidecar 替换、实体级
    ledger resolve 同一事务提交。派生失败只记 marker、不抛错 (source 不动);
    配置类 ValueError 仍 fail-closed 上抛 (旧契约)。

    ``tokenizer_override`` / ``target_tokens`` 仅供测试与运维显式覆盖;
    生产默认 None = planner 按 embed_cfg 解析 (tokenizer 不可用则 fail-closed)。
    """
    try:
        from .config import resolve_config
        from .embedding import embed_batch, safe_embed_cfg
        # 阶段1 (2026-08-20): 唯一构造入口 — safe_embed_cfg 工厂.
        # cfg 为 None / 缺 embed 子配置 → None (disabled), 走日志 + return, 不发请求.
        embed_cfg = cfg
        if embed_cfg is None:
            _cfg = resolve_config()
            embed_cfg = safe_embed_cfg(_cfg)
        if embed_cfg is None:
            logger.debug("observer: embed 未配置或缺 model/endpoint, 跳过印 embedding 回填")
            return
        # fingerprint 校验: 防止手拼 dict 落空
        fp = embed_cfg.get("_fingerprint") or ""
        if not fp:
            raise ValueError(
                "embed_cfg 缺 _fingerprint — 必须经 build_embed_cfg(cfg) 工厂构造, "
                "禁止手拼 dict"
            )
        # ── planner 可用时按完整 content 判定 short/long ──
        _plan = None
        try:
            from .observation_chunks import plan_observation_chunks
            from .embed_chunks import TokenizerUnavailableError as _TokUnavail
            try:
                _plan = plan_observation_chunks(
                    content, embed_cfg,
                    tokenizer_override=tokenizer_override,
                    target_tokens=target_tokens,
                )
            except _TokUnavail as _e_tok:
                # fail-closed: 不发超窗请求; source 已持久, marker 可重试。
                _record_observation_derived_failure(
                    pg, note_id, "observation_long_tokenizer", _e_tok,
                    embed_cfg, fp, commit=commit)
                return
            except ValueError:
                raise
            except Exception as _e_plan:
                # The candidate must never fall back to a bare full-content
                # request after the planner fails: that is the original long-
                # observation defect. Persist the failure and leave source
                # durable for a later retry.
                _record_observation_derived_failure(
                    pg, note_id, "observation_long_plan", _e_plan,
                    embed_cfg, fp, commit=commit)
                return
        except ValueError:
            raise
        except Exception as _e_import:
            _record_observation_derived_failure(
                pg, note_id, "observation_long_plan", _e_import,
                embed_cfg, fp, commit=commit)
            return
        if _plan is not None and _plan.is_long and (content or ""):
            _backfill_long_observation_note(
                pg, note_id, content, _plan, embed_cfg, fp,
                notes_table=notes_table, commit=commit,
                target_tokens=target_tokens)
            return
        if _plan is not None and not _plan.is_long and not (_plan.embed_text or ""):
            # 空 source: 不发请求, 不编造向量。
            return
        embs = embed_batch([content], embed_cfg, retries=2)
        if not embs or not embs[0] or not any(embs[0]):
            if _plan is not None:
                _record_observation_derived_failure(
                    pg, note_id, "observation_embed",
                    RuntimeError("embedding API 返回空向量"),
                    embed_cfg, fp, commit=commit)
            logger.warning("observer: 印 embedding 返回空向量, 跳过回填")
            return
        if _plan is not None:
            # short: 既有单请求全量 parent 向量 + scoped stale 子行清理。
            try:
                _update_short_observation_note(
                    pg, note_id, embs[0], fp,
                    notes_table=notes_table, commit=commit)
            except Exception as _e_short:
                _record_observation_derived_failure(
                    pg, note_id, "observation_embed", _e_short,
                    embed_cfg, fp, commit=commit)
                raise
            return
        try:
            _update_observation_parent_vector(
                pg, note_id, embs[0], fp,
                _safe_notes_table(notes_table), commit=commit)
        except Exception:
            raise
    except ValueError:
        raise
    except Exception:
        raise


def _mark_topic_updates_pending(pg, note_id: int, updates: list[dict[str, Any]]) -> None:
    """对抗审查修复 (2026-08-07): topic_updates 应用失败后把原始数据存进 note.links。

    标记形态: {"kind": "topic_updates_pending", "data": [...], "ts": ...}
    补偿任务扫描 links 含此标记的 note, 重放 topic_updates。
    """
    import json as _json
    import time as _t
    with pg.cursor() as cur:
        cur.execute(
            "SELECT links::text FROM observation_notes WHERE id=%s", (note_id,)
        )
        row = cur.fetchone()
        links = []
        if row and row[0]:
            try:
                links = _json.loads(row[0])
                if not isinstance(links, list):
                    links = []
            except Exception:
                links = []
        links.append({
            "kind": "topic_updates_pending",
            "data": updates,
            "ts": _t.time(),
        })
        cur.execute(
            "UPDATE observation_notes SET links=%s::jsonb WHERE id=%s",
            (_json.dumps(links, ensure_ascii=False), note_id),
        )
    pg.commit()


def _link_entries_by_vector(pg, topics_table: str, topic_id: str,
                            qa_rows: list[dict[str, Any]],
                            note_id: int, threshold: float = 0.55,
                            embed_cfg: dict | None = None) -> int:
    """2026-08-07 方案A: 代码向量匹配把本段 QA 原文挂到主题卡 entries。

    用 topic 的 embedding 与本段 QA 的 embedding 算 cosine,
    命中 (≥ threshold) 的 QA 原文 INSERT 到 topic_entries —
    补上观察者从不写 entry 的断链 (设计文档要求保留 topic_entries 回链)。

    qa_rows: [{"id", "question", "answer"}, ...] — 观察者/冷启动本段 QA。
    source 格式: obs:{note_id}:vector:{cos:.2f} (matched_by 可验证)。
    返回挂载条数。失败抛异常由调用方捕获 (不阻塞主流程)。

    embed_cfg: 可选 — ``build_embed_cfg(cfg)`` 工厂结果. 传了 embed_cfg 必须有合法
    fingerprint, 写到 topic_entries.embed_model 列 (混库检查).
    未传 embed_cfg → 自动尝试 ``resolve_config + safe_embed_cfg``.
    """
    if not qa_rows or not topic_id:
        return 0
    conn = pg._connect() if hasattr(pg, "_connect") else pg
    if conn is None:
        return 0
    import json as _json
    import numpy as _np
    # 1. 取 topic embedding
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT embedding::text FROM {topics_table} WHERE topic_id=%s AND embedding IS NOT NULL",
            (topic_id,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return 0
        topic_emb = _np.array(_json.loads(row[0]), dtype=_np.float32)
    # 2. 本段 QA embedding (批量取)
    qa_ids = [int(q["id"]) for q in qa_rows if q.get("id")]
    if not qa_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, question, answer, embedding::text FROM qa_pairs "
            "WHERE id = ANY(%s) AND embedding IS NOT NULL",
            (qa_ids,),
        )
        qa_emb_rows = cur.fetchall()
    if not qa_emb_rows:
        return 0
    # 3. cosine 匹配 + 挂载
    t_norm = float(_np.linalg.norm(topic_emb))
    if t_norm < 1e-10:
        return 0
    # 阶段1.5 (2026-08-20): 解析 fingerprint — embed_cfg 优先; 否则尝试 safe_embed_cfg 工厂.
    fp = ""
    try:
        if embed_cfg is not None:
            from .embedding import safe_embed_cfg as _sec
            # 不重复 factory 校验 — 直接信任传入 cfg
            fp = (embed_cfg or {}).get("_fingerprint") or ""
        if not fp:
            from .config import resolve_config as _rc
            from .embedding import safe_embed_cfg as _sec
            _cfg = _rc()
            _ec = _sec(_cfg)
            if _ec is not None:
                fp = _ec.get("_fingerprint") or ""
    except Exception:
        fp = ""
    if not fp:
        raise ValueError(
            "_link_entries_by_vector: 缺 fingerprint — 调式应传 embed_cfg 或 "
            "保证 resolve_config 返回的 embed 配置完整"
        )
    linked = 0
    for qid, q, a, emb_str in qa_emb_rows:
        if not emb_str:
            continue
        qa_emb = _np.array(_json.loads(emb_str), dtype=_np.float32)
        qa_norm = float(_np.linalg.norm(qa_emb))
        if qa_norm < 1e-10:
            continue
        cos = float(_np.dot(topic_emb, qa_emb) / (t_norm * qa_norm))
        if cos < threshold:
            continue
        # 2026-08-22 G1B: 单条 QA 直挂 — 带证据链回链 source_qa_id=qa_pairs.id,
        # 并原样复用该 QA 已有 embedding (零 embedding API 调用)。
        # 生产库缺 source_qa_id 列时: 先 rollback 再剥列降级 (可观测 warning),
        # entry 本体仍写入, 绝不让事务死在 InFailedSqlTransaction。
        source = f"obs:{note_id}:vector:{cos:.2f}"
        _cols = ["topic_id", "question", "answer", "source", "seq", "timestamp"]
        _vals: list = [topic_id, (q or "")[:2000], (a or "")[:4000], source,
                       topic_id]
        _ph = ["%s", "%s", "%s", "%s",
               "(SELECT COALESCE(MAX(seq), 0) + 1 FROM topic_entries WHERE topic_id = %s)",
               "NOW()"]
        if fp:
            _cols += ["embedding", "embed_model"]
            _ph += ["%s::vector", "%s"]
            _vals += [emb_str, fp]
        _cols.append("source_qa_id")
        _ph.append("%s")
        _vals.append(int(qid))
        # 2026-08-22 G1B e2e 修正: timestamp 列用 NOW() 字面占位, 不占 %s 参数位 —
        # 占位符数必须与参数数严格相等, 否则 psycopg2 报
        # "not all arguments converted during string formatting"。

        def _entry_sql(cols: list, ph: list) -> str:
            return (
                "\n                        INSERT INTO topic_entries (\n                            "
                + ", ".join(cols)
                + "\n                        )\n                        VALUES (\n                            "
                + ", ".join(ph)
                + "\n                        )\n                        ON CONFLICT DO NOTHING"
            )

        try:
            for _attempt in range(3):
                try:
                    with conn.cursor() as cur:
                        cur.execute(_entry_sql(_cols, _ph), tuple(_vals))
                    break
                except Exception as _e_ins:
                    from .pg_store import _is_undefined_column_error
                    if not _is_undefined_column_error(_e_ins):
                        raise
                    # 2026-08-22: 先 rollback 再降级 — 防 InFailedSqlTransaction 死 fallback.
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    # 2026-08-22 e2e 修正: 真 PG 报错文本会内嵌完整失败 SQL(含全部列名),
                    # 子串匹配会误中其它列 — 必须只认 column "X" 引用形态。
                    import re as _re
                    _quoted = set(_re.findall(
                        r'column "([a-z_0-9]+)"', str(_e_ins).lower()))
                    _missing = next(
                        (c for c in ("embed_model", "source_qa_id")
                         if c in _cols and c in _quoted),
                        None,
                    )
                    if _missing is None:
                        raise
                    logger.warning(
                        "observer._link_entries_by_vector: topic_entries 缺 %s 列, "
                        "降级不写该列 (已先 rollback): %s",
                        _missing, _safe_err(_e_ins)[:120],
                    )
                    # 2026-08-22 e2e 修正: vals 按"消耗 %s 的顺序"与 ph 对齐, 不与列下标
                    # 一一对应 (seq 子查询自带一个 %s、timestamp 用 NOW() 不占位)。
                    # 剥列 = 删 cols[i] / ph[i], 并从 vals 删掉该列占位符绑定的值 —
                    # 其下标 = 删除前 ph[0..i-1] 里 %s 的个数。
                    _ci = _cols.index(_missing)
                    _vi = sum(p.count("%s") for p in _ph[:_ci])
                    del _cols[_ci]
                    del _ph[_ci]
                    del _vals[_vi]
            conn.commit()
            linked += 1
        except Exception as e:
            logger.warning("observer: entry 挂载失败 (topic=%s qa=%s): %s",
                           topic_id, qid, _safe_err(e)[:100])
            try:
                conn.rollback()
            except Exception:
                pass
    if linked:
        logger.info("observer: 主题卡 %s 向量匹配挂载 %d 条 entries (阈值 %.2f)",
                    topic_id, linked, threshold)
    return linked


def _filter_keywords(tags):
    """2026-08-15: keywords 代码过滤兜底 (模型无关 — 不赌 LLM 遵从性)
    规则: 中文 2-8 字 / 英文 1-3 词; 超长丢弃; 去重; 上限 12"""
    if not tags:
        return None
    out = []
    seen = set()
    for t in tags:
        t = str(t).strip()
        if not t or t in seen:
            continue
        en_words = len([w for w in t.split() if w])
        cn_len = len([ch for ch in t if "\u4e00" <= ch <= "\u9fff"])
        if en_words > 3 and cn_len == 0:
            continue  # 英文超 3 词
        if cn_len > 8:
            continue  # 中文超 8 字
        if len(t) > 20:
            continue  # 总长兜底
        seen.add(t)
        out.append(t)
        if len(out) >= 12:
            break
    return out or None


def _apply_topic_updates(
    pg,
    note_id: int,
    updates: list[dict[str, Any]],
    cfg: dict[str, Any] | None = None,
    qa_rows: list[dict[str, Any]] | None = None,
) -> list[str]:
    """应用 topic_updates 到 PG topics 表 + 双写 sqlite topic_blocks。

    | action | topic_id        | 处理                                  |
    |--------|-----------------|---------------------------------------|
    | create | 空 / 新 id      | INSERT 新主题, note_ref 链回观察笔记   |
    | update | 现有 id         | 追加 body / 更新 summary / note_ref    |

    2026-08-07 方案A: qa_rows 非空时, 每条 topic 写完后做代码向量匹配,
    把本段 QA 原文挂到 topic_entries (matched_by=vector, source=obs:{note_id}:vector) —
    补上观察者从不写 entry 的断链 (设计文档要求保留 topic_entries 回链)。

    失败不抛 — 单条 update 失败不影响其他条目。

    v4.1 (2026-08-19): 返 list[str] of successfully applied topic_ids (create + update 都收集)。
    调用方 (_observe_worker 第 10 步) 用此 list 合并进 note.links 的 kind=topics,
    修复"候选为空但 topic_updates create 成功后, note links 不含新建 topic_id"的断链。
    失败分支 (TopicStore 加载失败 / 单条 update 异常) 跳过该 topic_id, 不进返回列表。
    """
    applied_ids: list[str] = []
    topics_table = str(cfg.get("topics_table", "topics")) if cfg else "topics"
    # 延迟 import, 避免 daemon 线程 sys.path 状态依赖
    try:
        from .topic_store import TopicStore
    except Exception as e:
        logger.warning("observer: 加载 TopicStore 失败, topic_updates 全跳: %s",
                       _safe_err(e)[:120])
        return applied_ids
    try:
        _topic_db = str(cfg.get("topic_store_db", "")) if cfg else ""
        _tt = str(cfg.get("topics_table", "topics")) if cfg else "topics"
        _strict = bool(cfg.get("topic_store_strict", True)) if cfg else True
        if _topic_db:
            store = TopicStore(db_path=_topic_db, topics_table=_tt, strict_pg=_strict)
        else:
            store = TopicStore(topics_table=_tt, strict_pg=_strict)
    except Exception as e:
        logger.warning("observer: 连接 TopicStore sqlite 失败, topic_updates 全跳: %s",
                       _safe_err(e)[:120])
        return applied_ids

    note_ref = f"obs://{note_id}"

    # ────────────────────────────────────────────────────────────────────
    # 2026-08-19 (eval_v2 entries embedding 缺位修复):
    # 观察者 new_facts 拆条事实级 entry 之前漏传 embedding, 32/55 条 NULL 全是
    # source='obs:fact:{note_id}'。修复: 不为每条 fact 新增 embedding API 调用;
    # 而是按本批 qa_rows 的 qa_pairs.embedding 复用 — 一次 PG 批量 SELECT,
    # 解析每条 fact 文本里的 [#N] / [ #N ] 引用 (N 是本批 QA 局部 1-based 序号,
    # 与 prompt "QA #1, #2, ..." 对齐), 把对应 QA 的 emb_str 直接传给
    # store.add_entry(..., embedding=...) — 既不新增 API 调用, 也不写 NULL。
    # 解析不到或对应 QA embedding 缺位的事实: 严格模式 (strict_pg=True, 生产默认)
    # 下跳过该 entry (避免"写了没向量"的断链残留), 仅 warning + 累计计数;
    # 非严格模式仍可写入 NULL 兼容 (旧 schema 不报错), 但同样 warning + 累计。
    # ────────────────────────────────────────────────────────────────────
    _qa_emb_by_local: dict[int, Any] = {}  # local 1-based ordinal → raw emb_str (or list)
    _fact_unresolved_total: int = 0        # 总 unresolvable 计数 (跨所有 updates)
    _fact_unresolved_examples: list[str] = []  # 头 5 条 unresolvable fact 文本 (审计用)
    if qa_rows:
        try:
            _qa_id_list = [int(q["id"]) for q in qa_rows if q.get("id") is not None]
            _local_idx_by_qa_id: dict[int, int] = {}
            for _local_i, _qrow in enumerate(qa_rows, 1):
                try:
                    _local_idx_by_qa_id[int(_qrow["id"])] = _local_i
                except Exception:
                    continue
            # 一次 PG 批量取本批 qa 全部 embedding (text 形态, 与 _link_entries_by_vector 同源)
            _pg_conn_qa = pg._connect() if hasattr(pg, "_connect") else pg
            with _pg_conn_qa.cursor() as _cur_qa:
                _cur_qa.execute(
                    "SELECT id, embedding::text FROM qa_pairs "
                    "WHERE id = ANY(%s) AND embedding IS NOT NULL",
                    (_qa_id_list,),
                )
                for _qid, _emb_text in _cur_qa.fetchall():
                    _local_i = _local_idx_by_qa_id.get(int(_qid))
                    if _local_i is None:
                        continue
                    _qa_emb_by_local[_local_i] = _emb_text
        except Exception as _e_qaemb:
            logger.warning(
                "observer: 批量读 qa_pairs.embedding 失败, fact entries 将全部走 NULL embedding "
                "(非致命, _link_entries_by_vector 也会受影响): %s",
                _safe_err(_e_qaemb)[:120],
            )

    # ────────────────────────────────────────────────────────────────────
    # 2026-08-20 (eval_v2 fact entry embed_cfg 缺位修复 v4.2):
    # 之前 store.add_entry(..., embedding=_fact_emb) 没传 embed_cfg, 导致 PG
    # ``_add_entry_pg._resolve_embed_cfg`` fail-closed 抛 ValueError, 而观察者内层
    # ``except Exception`` 把这条 ValueError 降级为 warning + 静默吞掉, 结果
    # topic_entries.embed_model 列永远 NULL, 形成"embedding 断链残留"。
    #
    # 修复: 在 per-update 循环前, 按本批 cfg 经唯一工厂构造一次 embed_cfg
    # (与 topic_store.upsert_topic line 218-219 同款路径 —
    # safe_embed_cfg(resolve_config())). 工厂抛 ValueError 必须原样冒泡, 严禁
    # 静默降级; 工厂返 None (没配 embed) 时, embed_cfg 留 None, 让内层
    # _resolve_embed_cfg 继续按既有 fail-closed 契约抛 — 任何"伪造 cfg 占位
    # 让 add_entry 通过"的路径都禁止. 一次构造, 整个调用复用 — 不在
    # per-fact 循环里重复构造 (避免额外开销).
    # ────────────────────────────────────────────────────────────────────
    embed_cfg: dict | None = None
    try:
        from .config import resolve_config as _rc_for_embed
        from .embedding import safe_embed_cfg as _sec_for_embed
        embed_cfg = _sec_for_embed(_rc_for_embed())
    except ValueError:
        # 配置契约错误 — 必须原样冒泡, 严禁吞掉伪装成 disabled.
        raise
    except Exception as _e_embedcfg:
        # resolve_config 自身故障 (无 hermes home / yaml 损坏等): 降级为 None,
        # 让 _resolve_embed_cfg 继续按 fail-closed 路径执行 (有 embedding → 抛).
        logger.debug(
            "observer: safe_embed_cfg(resolve_config()) 解析失败, embed_cfg=None: %s",
            _safe_err(_e_embedcfg)[:120],
        )
        embed_cfg = None

    # 单条 fact 文本里解析最近一个 [#N] / [ #N ] 引用; 多引用取第一个。
    # 兼容空格, 不区分全/半角, 不区分中英括号 — 与 prompt 内嵌风格保持一致。
    _CITATION_RE = re.compile(r"\[\s*#\s*(\d+)\s*\]")

    def _resolve_fact_embedding(fact_text: str) -> tuple[Any | None, str | None]:
        """从 fact 文本解析 [#N] 引用, 返回 (emb_value, reason_if_none).

        reason_if_none 仅在 emb_value 为 None 时填充, 用于审计与 warning 提示:
          - "no_citation" — 文本里没有 [#N]
          - "out_of_range" — N 超出本批 qa_rows 范围
          - "missing_embedding" — 引用 QA 在 qa_pairs 里 embedding 为 NULL
          - "no_qa_rows" — 调用方未传 qa_rows
        失败时不抛 — caller 走"skip + warning + 累计计数"路径。
        """
        if not qa_rows:
            return None, "no_qa_rows"
        m = _CITATION_RE.search(fact_text or "")
        if not m:
            return None, "no_citation"
        try:
            n = int(m.group(1))
        except Exception:
            return None, "no_citation"
        if n < 1 or n > len(qa_rows):
            return None, "out_of_range"
        emb = _qa_emb_by_local.get(n)
        if emb is None:
            return None, "missing_embedding"
        return emb, None
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        action = (upd.get("action") or "").strip().lower()
        title = (upd.get("title") or "").strip()[:200]
        summary = (upd.get("summary") or "").strip()[:500]
        new_facts = (upd.get("new_facts") or "").strip()[:8000]
        tags = upd.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        topic_id_in = (upd.get("topic_id") or "").strip()

        if not title:
            logger.warning("observer: 跳过一条 topic_update (title 空): %s",
                           json.dumps(upd, ensure_ascii=False)[:200])
            continue

        try:
            if action == "update" and topic_id_in:
                # 现有主题: 读旧 body（2026-08-06: 改 PG 读 — SQLite 双写断链只剩 80 张,
                # 读 SQLite 找不到 → 误转 create → 制造重复卡）; PG 无则转 create
                old_body = ""
                old_summary = ""
                _found = False
                if pg is not None:
                    try:
                        _conn_pg = pg._connect() if hasattr(pg, "_connect") else pg
                        with _conn_pg.cursor() as _cur:
                            _cur.execute(
                                f"SELECT body, summary FROM {topics_table} WHERE topic_id=%s",
                                (topic_id_in,),
                            )
                            _row = _cur.fetchone()
                            if _row:
                                old_body = _row[0] or ""
                                old_summary = _row[1] or ""
                                _found = True
                    except Exception:
                        _found = False
                if not _found:
                    cur = store.conn.execute(
                        "SELECT body, summary FROM topic_blocks WHERE id=?",
                        (topic_id_in,),
                    ).fetchone()
                    if cur:
                        old_body = cur[0] or ""
                        old_summary = cur[1] or ""
                        _found = True
                if _found:
                    # 2026-08-12 v2 修正: body 追加限量 — 第二轮事实级改造发现 new_facts 全追加
                    # 导致单卡膨胀 (6975字) 超注入预算 4000 → 截断 → 关键事实不可见 (conv-26 75%→64%)
                    # 小粒度主题卡覆盖更好 (对照组 31 卡 45.8% vs 实验组 23 卡 44.1%)
                    _new_body = (old_body + "\n\n" + new_facts).strip()
                    if len(_new_body) > 3000:
                        # body 超限: 不追加, 事实只走 entries (chain 链路), 保持小粒度
                        appended = old_body
                        _new_body = old_body
                    else:
                        appended = _new_body
                    new_summary = summary or old_summary
                    store.upsert_topic(
                        title=title,
                        summary=new_summary,
                        body=appended[:16000],
                        keywords=_filter_keywords(tags),
                        topic_id=topic_id_in,
                        pg_conn=pg,
                    )
                    applied_ids.append(str(topic_id_in))
                else:
                    # topic_id 没找到 → 转 create
                    action = "create"
            if action == "create":
                tid = store.upsert_topic(
                    title=title,
                    summary=summary,
                    body=new_facts[:16000],
                    keywords=_filter_keywords(tags),
                    pg_conn=pg,
                )
                topic_id_in = tid
                # v4.1 (2026-08-19): create 成功也必须 append 进 applied_ids,
                # 否则 worker 拿不到新建 topic_id, note.links 永远漏回链 —
                # 与 update 分支行 1439 的语义对齐 (失败分支不在这里, 由外层 except 跳过)。
                applied_ids.append(str(tid))
        except ValueError:
            raise
        except Exception as e:
            logger.warning("observer: 单条 topic_update 失败(不影响其他): %s",
                           _safe_err(e)[:120])
            continue

        # 在 PG topics 表上更新 note_ref + last_observer_ts (不管 create 还是 update 都标)
        if topic_id_in:
            # 2026-08-12: new_facts 逐条结构化落 topic_entries (事实级检索, 源 obs:fact)
            # 之前只追加 body 文本 — body 注入被 [:300] 截断, 事实在 body 中后段不可见;
            # 结构化 entry 让事实可被 topic_entries 链路召回。
            # 2026-08-19: 同时把 QA embedding 复用过来 — 见上面 _qa_emb_by_local 准备段。
            # 解析不到引用的事实走"skip + warning + 累计计数"; strict_pg 下绝不静默
            # 写 NULL embedding (避免断链残留); 非 strict 模式下允许 NULL 但仍记录。
            if (new_facts or '').strip():
                _fact_items = [f.strip() for f in re.split(r'[\n；;]+', new_facts) if f.strip()]
                for _fi in _fact_items[:20]:
                    _fact_emb, _reason = _resolve_fact_embedding(_fi)
                    if _fact_emb is None:
                        # 不可解析 — 不写 entry, 累计计数与示例, 留待审计
                        _fact_unresolved_total += 1
                        if len(_fact_unresolved_examples) < 5:
                            _fact_unresolved_examples.append(
                                f"[{_reason}] {_fi[:80]}"
                            )
                        continue
                    try:
                        store.add_entry(
                            topic_id_in,
                            source=f'obs:fact:{note_id}',
                            question=_fi[:300],
                            answer='',
                            pg_conn=pg,
                            embedding=_fact_emb,
                            embed_cfg=embed_cfg,
                        )
                    except ValueError:
                        raise
                    except Exception as _e_fact:
                        logger.warning(
                            "observer: fact entry 写入失败 (topic=%s, fact=%r): %s",
                            topic_id_in, _fi[:60], _safe_err(_e_fact)[:100],
                        )
                        try:
                            pg.rollback()
                        except Exception:
                            pass
            try:
                with pg.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE {topics_table}
                        SET note_ref = COALESCE(note_ref, %s),
                            last_observer_ts = NOW()
                        WHERE topic_id = %s
                        """.format(topics_table=topics_table),
                        (note_ref, topic_id_in),
                    )
                pg.commit()
            except Exception as e:
                logger.warning("observer: 更新 topics.note_ref 失败(非致命): %s",
                               _safe_err(e)[:120])
                try:
                    pg.rollback()
                except Exception:
                    pass

            # 2026-08-07 方案A: 代码向量匹配挂 entry (观察者从不写 entry 的断链修复)
            # 每条 topic 写成功后, 用 topic embedding 匹配本段 QA 原文, 命中挂 topic_entries。
            # 失败不阻塞 — 单条挂载异常仅 warning (后续可由补盲脚本覆盖)。
            if qa_rows:
                try:
                    _link_entries_by_vector(
                        pg, topics_table, topic_id_in, qa_rows, note_id,
                    )
                except ValueError:
                    # embedding/fingerprint 配置错误必须暴露，不能静默跳过 entry 写入。
                    raise
                except Exception as e:
                    logger.warning("observer: entry 向量匹配失败(非致命): %s",
                                   _safe_err(e)[:120])
                    try:
                        pg.rollback()
                    except Exception:
                        pass


    # 整个 _apply_topic_updates 结束时, 把本批 fact unresolved 总数汇总打印一次 —
    # strict_pg 生产模式下 unresolved > 0 表示这一批 fact entries 有缺失, 留待审计。
    if _fact_unresolved_total > 0:
        logger.warning(
            "observer: fact entries unresolved=%d (note_id=%s, examples=%r) — "
            "fact 来源缺失 embedding, 已跳过该 entry; strict_pg 下绝不静默写 NULL",
            _fact_unresolved_total, note_id, _fact_unresolved_examples,
        )

    return applied_ids


# ────────────────────────────────────────────────────────────
# 种子压缩 — phase7 日终压缩步骤
#
# 设计: 完整印 → 种子印 (500-800 字硬约束). 完整印存到 observation_notes.content,
#       种子印存到同一条 links JSONB 数组 ({kind:"seed", content:...}).
# 下次观察时: 优先从上次 links 抽种子印作为 prev_note, 失败 fallback 完整印.
#
# 借鉴 phase6 SEED_COMPRESS_PROMPT, 验证基线 6 月 10 天 9/10 稳定.
# ────────────────────────────────────────────────────────────

SEED_COMPRESS_PROMPT = """你是记忆观察者。今天是 {current_date}。给定今天的完整印（可能很长），压缩成**种子印**（500-800 字，硬约束）——供明天作为输入上下文。

种子印必须包含：
1. **关键决策**（决策是记忆的锚，不能丢，含 [#N] 来源）
2. **进行中状态**（未完事项、待验证、下一步）
3. **主题指针**（主题用 [t_xxxx] 引用，不展开细节）
4. **关键操作知识**（路径/命令/端口，一行一个）
5. **淡化**：已完结的细节、过程描述省略

【今天的完整印】
{full_note}

输出：纯文本种子印，不要 JSON，500-800 字。"""


def _seed_compress(
    full_note: str,
    cfg: dict[str, Any],
    api_key: str,
    *,
    thinking: str = "disabled",
    reasoning_split: bool = True,
    timeout: int = 300,
    seed_min: int = 500,
    seed_max: int = 800,
    max_completion_tokens: int = 4096,
    temperature: float = 0.3,
    dry_run: bool = False,
) -> str | None:
    """调 M3 把完整印压成种子印 (500-800 字).

    Args:
        full_note: 完整印正文
        cfg: 已合并的 runtime + observer cfg (含 base_url/model/proxy)
        api_key: LLM api_key
        thinking/reasoning_split: phase0e 定案参数
        timeout: 种子压缩超时
        seed_min/seed_max: 字数硬约束 (500-800)
        max_completion_tokens: 4096 比主 M3 小 (种子印短)
        temperature: 0.3 与 phase0 一致
        dry_run: True 时直接返回 None 不调 LLM

    Returns:
        种子印文本 (500-800 字附近), 失败返 None (失败时 caller fallback 完整印).
    """
    if dry_run:
        logger.info("observer: SEED DRY-RUN — 跳过 M3 种子压缩")
        return None
    if not full_note or len(full_note) < seed_min:
        # 完整印太短, 不压; 直接返 None (caller fallback 完整印做下一轮输入)
        logger.info(
            "observer: SEED — 完整印 %d 字 < 种子下限 %d, 跳过压缩",
            len(full_note), seed_min,
        )
        return None
    # prompt 可配置化: config.yaml prompts.observer_seed 可覆盖 (默认回落内置)
    from .config import _resolve_prompt as _cfg_prompt
    _seed_prompt = _cfg_prompt(cfg, "observer_seed", SEED_COMPRESS_PROMPT)
    prompt = _seed_prompt.format(
        full_note=full_note[:30000],
        current_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    result = call_m3(
        cfg, api_key, prompt,
        max_completion_tokens=max_completion_tokens,
        thinking=thinking,
        reasoning_split=reasoning_split,
        timeout=timeout,
    )
    if "error" in result:
        logger.warning(
            "observer: SEED 压缩失败(用完整印 fallback): %s",
            result["error"][:200],
        )
        return None
    seed_text = (result.get("text", "") or "").strip()
    if not seed_text:
        logger.warning("observer: SEED 压缩返空, fallback 完整印")
        return None
    # 字数轻度校验: 超上限截, 不够下限也返 (caller 也容忍)
    if len(seed_text) > seed_max * 2:
        # 极罕见: M3 输出过长, 截到 2x 上限保留多一点信息
        seed_text = seed_text[: seed_max * 2]
    logger.info(
        "observer: SEED 完成 %d 字 (seed_min=%d seed_max=%d, prompt_tokens=%s latency=%.1fs)",
        len(seed_text),
        seed_min, seed_max,
        (result.get("usage") or {}).get("prompt_tokens", "?"),
        result.get("latency", 0.0),
    )
    return seed_text


def _extract_prev_seed(links_jsonb: Any) -> str | None:
    """从 observation_notes.links (JSONB 数组) 抽最近一条 seed 印.

    形态约定 (phase7 _write_observation_note 写入):
        {"kind": "seed", "version": "v3", "content": "...", "char_count": N}

    Returns:
        种子印字符串, 无种子印返 None.
    """
    if not links_jsonb:
        return None
    try:
        # psycopg2 默认把 JSONB 转成 Python 列表; 字符串也能 parse
        if isinstance(links_jsonb, str):
            arr = json.loads(links_jsonb)
        else:
            arr = links_jsonb
        if not isinstance(arr, list):
            return None
        # 取最后一条 kind=seed (倒序扫, 容错多次写)
        for entry in reversed(arr):
            if isinstance(entry, dict) and entry.get("kind") == "seed":
                txt = entry.get("content") or ""
                if txt:
                    return str(txt)
    except Exception:
        return None
    return None


# v4 (2026-08-16): 人格层 + 7 天快照印读取器 — 失败兜底空字符串, 不阻塞观察者主流程。

_THINK_BLOCK_RE_OBS = re.compile(r"<think>[\s\S]*?</think>")


def _load_identity_layer(cfg: dict[str, Any] | None = None) -> str:
    """v4: 从 config 的 y_dir (默认 base_path/y/) 读最新 y_*.md 文件内容 (去 think 块).

    作为 identity_layer 传入 prompt — 给观察者提供{_id}的连续人格状态 (E1 合成)。
    失败 (无文件 / IO 错 / 配置缺失) → 返 "" 不阻塞。
    """
    try:
        # 复用 e1 的 DeepStore 拿 base_path — 与 E1 写 y 文件路径一致
        from .card_store import DeepStore
        store = DeepStore(cfg if isinstance(cfg, dict) else None)
        y_dir = store.base / "y"
        if not y_dir.exists():
            return ""
        y_files = sorted(y_dir.glob("y_*.md"), reverse=True)
        if not y_files:
            return ""
        # 读最新一份, 去 think 块, 全文 (人格层不会被印库拆分, 全文注入)
        raw = y_files[0].read_text(encoding="utf-8", errors="replace")
        raw = _THINK_BLOCK_RE_OBS.sub("", raw).strip()
        return raw
    except Exception as e:
        logger.debug(
            "observer: 读人格层失败 (兜底空字符串): %s", _safe_err(e)[:100]
        )
        return ""


def _load_recent_snapshots(
    pg,
    notes_table: str = "observation_notes",
    window_days: int = 7,
    max_chars: int = 200000,
    *,
    as_of_event_time: Any | None = None,
    hard_fail: bool | None = None,
    fail_closed: bool | None = None,
) -> str:
    """按 QA 事件时间读取最近 ``window_days`` 的完整快照序列。

    ``created_at`` 只表示程序何时处理 note；历史回放时不能拿它当记忆事件时间。
    新 note 的事件范围写在 links 的 ``kind=event_time`` 中，旧 note 没有该元数据时
    才回退到 created_at。SQL 仍按 created_at 排序，之后由 Python 按事件时间稳定排序，
    保留旧 schema/fake cursor 契约。

    双模式 (v4 2026-08-19) — 旧位置参数 ``pg/notes_table/window_days/max_chars``
    保持兼容；新增 keyword-only 三个开关:

    - ``hard_fail``: 明确值优先。``True`` 显式抛 ``_SnapshotOverflowError``;
      ``False``/``None`` 默认走软警告, 不抛, 不截断 (完整 snapshot 继续进入 prompt)。
    - ``fail_closed``: v3 旧 alias, 行为同 ``hard_fail``; 当 ``hard_fail=None`` 时
      接管判断, ``True`` 抛 / ``False``/``None`` 软警告。
    - ``as_of_event_time``: 7 天窗口的"现在"基准 (datetime / ISO 字符串); None 时
      走 wall clock, 历史回放必须显式传本批 QA 的 max timestamp。

    软警告路径 (默认/False) 永远不做任何字符切片 — 不调用 ``joined[-max_chars:]``,
    不返"前段已截断"标记字符串, 完整 snapshot 文本继续进入 prompt。
    真正溢出模型上下文由 worker 在 LLM 调用端处理 (拆批/降上下文)。
    """
    # 双模式优先级: hard_fail 明确值优先 (True/False 都算明确); 都为 None 时默认 False
    if hard_fail is not None:
        _hard_fail = bool(hard_fail)
    elif fail_closed is not None:
        _hard_fail = bool(fail_closed)
    else:
        _hard_fail = False  # v4 默认: 软警告, 不抛, 不截断
    rows: list[tuple] = []
    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, links, created_at
                FROM {notes_table}
                ORDER BY created_at ASC, id ASC
                """.format(notes_table=notes_table),
            )
            rows = cur.fetchall()
    except _SnapshotOverflowError:
        try:
            pg.rollback()
        except Exception:
            pass
        raise
    except Exception as e:
        logger.debug(
            "observer: 读 7 天快照印失败 (兜底空字符串): %s", _safe_err(e)[:100]
        )
        try:
            pg.rollback()
        except Exception:
            pass
        return ""
    if not rows:
        return ""

    def _utc(value: Any) -> datetime | None:
        # v4 (2026-08-19): 由 _parse_iso_utc 接管, _utc 保留为本地 alias 兼容旧代码路径
        return _parse_iso_utc(value)

    def _links(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                return []
        if isinstance(value, dict):
            value = [value]
        return [x for x in (value or []) if isinstance(x, dict)] if isinstance(value, list) else []

    def _event_bounds(raw_links: Any) -> tuple[datetime | None, datetime | None]:
        # v4 (2026-08-19): 委托给顶层纯函数 _parse_event_time_from_links, 行为等价
        # (兼容旧 v3 source_event kind); 不再就地 _utc。
        return _parse_event_time_from_links(raw_links)

    as_of = _utc(as_of_event_time) or datetime.now(timezone.utc)
    lower = as_of - timedelta(days=max(0, int(window_days)))
    prepared: list[tuple[datetime | None, datetime | None, int, str, str]] = []
    for row in rows:
        _id = int(row[0])
        _content = str(row[1] or "")
        _raw_links = row[2] if len(row) >= 4 else None
        _raw_created = row[3] if len(row) >= 4 else (row[2] if len(row) == 3 else None)
        _event_start, _event_end = _event_bounds(_raw_links)
        _created = _utc(_raw_created)
        _sort_time = _event_start or _created
        if as_of_event_time is not None:
            if _event_start or _event_end:
                if (_event_end or _event_start) < lower or (_event_start or _event_end) > as_of:
                    continue
            elif _created is not None and not (lower <= _created <= as_of):
                continue
        _label_time = _event_start or _created
        _label = _label_time.strftime("%Y-%m-%d %H:%M UTC") if _label_time else ""
        prepared.append((_sort_time, _event_end, _id, _content, _label))

    prepared.sort(key=lambda x: (x[0] or datetime.min.replace(tzinfo=timezone.utc), x[2]))
    parts: list[str] = []
    for _, _, _id, _content, _ts_str in prepared:
        _seg = f"--- snapshot id={_id} ({_ts_str}) ---\n{_content}"
        parts.append(_seg)
    joined = "\n\n".join(parts)
    total = len(joined)
    if max_chars > 0 and total > max_chars:
        # v4 双模式: 默认/False 软警告 + 完整 snapshot (不做任何字符切片),
        # 显式 True 才抛 _SnapshotOverflowError。绝对不允许 joined[-max_chars:]
        # 静默截断 — 会丢前段 observation, 给 LLM 看半截上下文。
        logger.warning(
            "observer: 7天 snapshot 超过软上限 — 总长 %d > ceiling %d (hard_fail=%s)",
            total, max_chars, _hard_fail,
        )
        if _hard_fail:
            try:
                pg.rollback()
            except Exception:
                pass
            raise _SnapshotOverflowError(
                f"observer: snapshot 拼接总长 {total} > max_chars {max_chars}"
            )
    return joined


def _load_adaptive_batch(cfg: dict[str, Any] | None = None) -> tuple[int | None, int | None]:
    """读取上次超长输出留下的自动拆批状态。

    返回 ``(batch_size, source_end_id)``。状态只用于恢复同一段时间流，
    不改变配置文件，也不把手工 30→15→10 变成隐形常量。
    """
    path = _state_path(cfg)
    try:
        if not path.exists():
            return None, None
        data = json.loads(path.read_text(encoding="utf-8")) or {}
        size = data.get("adaptive_batch_size")
        end = data.get("adaptive_source_end_id")
        size = int(size) if size is not None and int(size) > 0 else None
        end = int(end) if end is not None and int(end) > 0 else None
        return size, end
    except Exception:
        return None, None


def _save_adaptive_batch(batch_size: int, source_end_id: int,
                         cfg: dict[str, Any] | None = None) -> None:
    """记录一次超长输出后的时间流拆批计划，供下一轮自动恢复。"""
    path = _state_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        data["adaptive_batch_size"] = max(1, int(batch_size))
        data["adaptive_source_end_id"] = int(source_end_id)
        data["adaptive_reason"] = "note_output_over_target_time_split"
        data["adaptive_updated_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("保存自动拆批状态失败(本轮不推进, 下次仍会重试): %s", _safe_err(e)[:120])


def _clear_adaptive_batch(cfg: dict[str, Any] | None = None) -> None:
    """清除已经完整消费到 source_end_id 的自动拆批状态。"""
    path = _state_path(cfg)
    try:
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8")) or {}
        for key in ("adaptive_batch_size", "adaptive_source_end_id", "adaptive_reason",
                    "adaptive_updated_at"):
            data.pop(key, None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("清除自动拆批状态失败: %s", _safe_err(e)[:120])


# ────────────────────────────────────────────────────────────
# v4 stage-2 滚动管线失败状态机 (2026-08-20)
#
# 复用 _state_path (同 observer_state.json), 与 adaptive_batch 共存互不破坏。
# 三类持久化字段 (一份 JSON 文件, 三组键):
#   - failed_segments: list[dict]
#       每条: {segment_id, qa_range_start, qa_range_end, error_class,
#              reason, attempts, last_attempt_at, batch_size}
#       表示"上次观察这批 QA 失败, 但还没到 stalled 阈值, 下次可继续重试"。
#       segment_id 选 qa_range_end (last_qa_id) 字符串化, 避免重复累积同段。
#   - stalled_state: dict | None
#       {segment_id, qa_range_start, qa_range_end, error_class,
#        reason, attempts, last_attempt_at, written_at}
#       表示"同一段已尝试上限仍未推进, 不再无限重试 — supervisor 必须停止"。
#   - failed_segment_threshold: int (默认存默认; 实际默认走 _DEFAULT_STAGE2_MAX_RETRIES)
#       阈值可由 cfg.failed_segment_max_retries 覆盖 (但运行时只为可观测写入)。
#
# 关键铁律 (2026-08-20 路线):
#   1. 失败段不能跳 — stalled 写完后本轮只 return, 不自动推进 cursor;
#      下一次重启/触发仍由人工 review 后可手动 _clear_stalled_state 重置。
#   2. 单条 vs 多条分流不变: 多条走 adaptive_batch (现有逻辑), 单条走 failed_segment;
#      重复失败上限 _DEFAULT_STAGE2_MAX_RETRIES (默认 3) 触发 stalled 状态。
#   3. 成功路径必须显式清理对应 segment 的 failed_segment + stalled 状态
#      (cursor 推进等于该段已成功消费)。
#   4. 不动 embedding 链路。
#
# 函数列表:
#   _save_failed_segment(...)     单条 QA 失败时把 segment 写入 state
#   _load_failed_segments(...)    读所有 failed_segments (按 segment_id dedup)
#   _clear_failed_segment(...)    成功后按 qa_range_end 清理对应段
#   _save_stalled_state(...)      达到上限时写 stalled, 不再被 supervisor 重试
#   _load_stalled_state(...)      读 stalled (None 表示未 stall)
#   _clear_stalled_state(...)     人工审查/手动重置 (普通成功路径不动)
#   _default_stage2_max_retries   兜底阈值 (3 次失败 → stall)
# ────────────────────────────────────────────────────────────

# v4 stage-2 兜底阈值: 同段连续失败 N 次 → 写 stalled, supervisor 不再重试。
# 这是"防止失败段无限重试烧 LLM 额度"的安全阀, 不暴露给普通用户, 但任何
# 监控/告警可以读 stalled_state 触发人工 review。
_DEFAULT_STAGE2_MAX_RETRIES: int = 3


def _default_stage2_max_retries(cfg: dict[str, Any] | None = None) -> int:
    """读 cfg.failed_segment_max_retries (int), 缺失/非法用 _DEFAULT_STAGE2_MAX_RETRIES。"""
    try:
        if isinstance(cfg, dict):
            v = cfg.get("failed_segment_max_retries")
            if v is not None:
                iv = int(v)
                if iv > 0:
                    return iv
    except Exception:
        pass
    return _DEFAULT_STAGE2_MAX_RETRIES


# ──────────────────────────────────────────────────────────────────────
# v5.3 (2026-08-26) 确定性敏感拦截单条跳过 — 用户拍板「修吧」
#
# 缺陷: 单条 QA 撞 M3 确定性敏感 (1026/new_sensitive/422 Unprocessable) →
#   failed_segment attempts 到阈值 → stalled_state 写入 → 守卫挡死后续所有
#   触发 → 观察者整体静默死亡。重试必败 (同 payload 平台侧确定性拦截),
#   stalled 只是把「无限烧额度」换成了「永久卡死」, 两者都不是正确出口。
#
# 修复语义 (与 cold_start --max-filter-skips 同族):
#   - 单条 + 确定性敏感 → _record_skipped_sensitive 审计留档 → 游标推进越过
#     该条 → 继续消化后续 QA;
#   - 非敏感失败 (超时/5xx/网络) → 原语义完全不动: failed_segment 计数 +
#     stalled 兜底;
#   - 多条批仍走 adaptive 减半拆到单条, 再由本判定分流 — 拆批逻辑不变。
# ──────────────────────────────────────────────────────────────────────

_SKIPPED_SENSITIVE_AUDIT_CAP = 100


def _is_m3_sensitive_error(err_str: str) -> bool:
    """M3 平台确定性内容拦截判定 — 重试必败, 跳过是唯一正确出口。

    与 cold_start._is_sensitive_error 同一判据 (三形态):
      ① 显式标记: "1026" / "new_sensitive";
      ② 任何 "sensitive" 字样;
      ③ 裸 422 Unprocessable Entity (G1A 阶段三实锤的亲密内容拦截形态,
         无显式标记但同 payload 三次重试全 422)。
    """
    if not err_str:
        return False
    if "1026" in err_str or "new_sensitive" in err_str \
            or "sensitive" in err_str.lower():
        return True
    if "422" in err_str and "Unprocessable" in err_str:
        return True
    return False


def _record_skipped_sensitive(
    qa_range_start: int,
    qa_range_end: int,
    *,
    reason: str,
    cfg: dict[str, Any] | None = None,
) -> None:
    """把被跳过的毒条写入 observer_state.json 的 skipped_sensitive 审计列表。

    截断保留最近 _SKIPPED_SENSITIVE_AUDIT_CAP 条 (防状态文件膨胀);
    写失败只警告不抛 — 审计不能反过来阻塞主流程。
    """
    path = _state_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        lst = data.get("skipped_sensitive")
        if not isinstance(lst, list):
            lst = []
        lst.append({
            "segment_id": f"{int(qa_range_start)}:{int(qa_range_end)}",
            "qa_range_start": int(qa_range_start),
            "qa_range_end": int(qa_range_end),
            "error_class": "m3_sensitive_deterministic",
            "reason": str(reason)[:200],
            "skipped_at": datetime.now(timezone.utc).isoformat(),
        })
        if len(lst) > _SKIPPED_SENSITIVE_AUDIT_CAP:
            lst = lst[-_SKIPPED_SENSITIVE_AUDIT_CAP:]
        data["skipped_sensitive"] = lst
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(
            "记录 skipped_sensitive 审计失败 (不阻塞): %s", _safe_err(e)[:120])


def _segment_id_from_range(qa_range_start: int, qa_range_end: int) -> str:
    """生成 failed_segment / stalled_state 的稳定 segment_id。

    用 ``"<start>:<end>"`` 字符串; 与 cursor (last_qa_id) 1-1 对应, 便于
    后续成功路径按 cursor 直接定位清理。
    """
    return f"{int(qa_range_start)}:{int(qa_range_end)}"


def _save_failed_segment(
    qa_range_start: int,
    qa_range_end: int,
    *,
    error_class: str,
    reason: str,
    batch_size: int,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """把一段失败的观察 segment 持久化到 observer_state.json。

    - 同 segment_id 已存在 → 在原条目上累加 attempts + 更新 reason/last_attempt_at
    - 不同 segment 追加
    - 函数返回最新该 segment 的 dict (便于调用方立即判断是否到上限), 文件 IO
      失败仅警告 — 不阻塞主流程 (与 _save_adaptive_batch 同样的容错纪律)。

    Args:
        qa_range_start / qa_range_end: 本次失败批次的 QA id 范围 (左闭右开)。
        error_class: "json_parse" | "m3_primary" | "m3_fallback" | "snapshot_overflow"
            之类 — 不同分支的来源分类。
        reason: 来自 M3 / JSON 解析器的可读错误摘要 (≤ 200 字, 调用方截断)。
        batch_size: 失败时实际尝试的 batch 大小 (供下次重试参考)。
        cfg: 观察者配置 (与 _save_adaptive_batch 同形)。

    Returns:
        最新该 segment 的 dict (含 attempts+1 后的值); 写盘失败时返回 None。
    """
    path = _state_path(cfg)
    segment_id = _segment_id_from_range(qa_range_start, qa_range_end)
    now_iso = datetime.now(timezone.utc).isoformat()
    updated_entry: dict[str, Any] | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        existing_list = data.get("failed_segments") or []
        if not isinstance(existing_list, list):
            existing_list = []
        # 找同 segment_id 的旧条目, 累加 attempts; 找不到则新增
        found_idx: int | None = None
        for i, entry in enumerate(existing_list):
            if isinstance(entry, dict) and entry.get("segment_id") == segment_id:
                found_idx = i
                break
        if found_idx is not None:
            existing = existing_list[found_idx]
            existing["attempts"] = int(existing.get("attempts", 0)) + 1
            existing["reason"] = str(reason)[:200]
            existing["error_class"] = str(error_class)
            existing["last_attempt_at"] = now_iso
            existing["batch_size"] = max(1, int(batch_size))
            updated_entry = existing
        else:
            updated_entry = {
                "segment_id": segment_id,
                "qa_range_start": int(qa_range_start),
                "qa_range_end": int(qa_range_end),
                "error_class": str(error_class),
                "reason": str(reason)[:200],
                "attempts": 1,
                "first_attempt_at": now_iso,
                "last_attempt_at": now_iso,
                "batch_size": max(1, int(batch_size)),
            }
            existing_list.append(updated_entry)
        data["failed_segments"] = existing_list
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(
            "保存 failed_segment (segment=%s, error_class=%s) 失败 — 下次重启会丢失败原因: %s",
            segment_id, error_class, _safe_err(e)[:120],
        )
        return None
    return updated_entry


def _load_failed_segments(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """读 observer_state.json 的 ``failed_segments`` 列表。

    解析失败 / 类型不对 / 文件不存在 — 任何异常都返 ``[]``, 与
    _load_adaptive_batch 同样容错 (状态文件坏掉不能阻塞主流程)。
    """
    path = _state_path(cfg)
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return []
        lst = data.get("failed_segments") or []
        if not isinstance(lst, list):
            return []
        return [e for e in lst if isinstance(e, dict)]
    except Exception:
        return []


def _clear_failed_segment(
    qa_range_end: int,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """成功后清理对应 segment 的 failed_segment 记录。

    segment_id 用 ``"<start>:<qa_range_end>"`` 命名; 传入 ``qa_range_end`` 时,
    找出 ``segment_id.endswith(f":{qa_range_end}")`` 或解析后右端等于
    ``qa_range_end`` 的条目并移除。

    Returns:
        True 表示本次清理了至少一条; False 表示没找到 (无副作用)。
    """
    path = _state_path(cfg)
    end_id = int(qa_range_end)
    removed = False
    try:
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return False
        lst = data.get("failed_segments") or []
        if not isinstance(lst, list):
            return False
        new_list: list[dict[str, Any]] = []
        for entry in lst:
            if not isinstance(entry, dict):
                continue
            entry_end = entry.get("qa_range_end")
            try:
                if entry_end is not None and int(entry_end) == end_id:
                    removed = True
                    continue
            except Exception:
                pass
            new_list.append(entry)
        if not removed:
            return False
        if new_list:
            data["failed_segments"] = new_list
        else:
            data.pop("failed_segments", None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(
            "清理 failed_segment (qa_range_end=%s) 失败 (不阻塞): %s",
            end_id, _safe_err(e)[:120],
        )
        return False
    return removed


def _save_stalled_state(
    qa_range_start: int,
    qa_range_end: int,
    *,
    error_class: str,
    reason: str,
    attempts: int,
    cfg: dict[str, Any] | None = None,
) -> None:
    """把某 segment 标记为 stalled — 之后 ``_observe_worker`` 启动时
    必须先检查 _load_stalled_state, 若 stalled 命中当前 cursor 范围则直接
    return, 不调 LLM 不写库, 防失败段无限重试烧额度。

    同时清理该 segment 的 failed_segments 入口 (避免两份状态互相打架),
    保留历史后续审计可由监控端通过 ``stalled_state`` 读出。

    关键契约 (2026-08-20 stage-2 补洞):
    写入 stalled 时必须同步删除 ``adaptive_batch_size`` /
    ``adaptive_source_end_id`` / ``adaptive_reason`` / ``adaptive_updated_at``。
    否则旧 ``full_replay_supervisor`` (在 ``stalled 守卫`` 引入之前留下来的版本)
    看到 ``adaptive_batch_size`` 非空 + cursor 没动 → 仍会进入"重试 adaptive 批次"
    循环, 把失败段绑在 supervisor 上无限重发 — 这正是历史观察到 cursor=1417
    单条 stall 反反复复的根因之一。stalled 写完必须是"无 adaptive, 无 failed_segment,
    cursor 未推进"的三清状态, supervisor 立即退出。
    """
    path = _state_path(cfg)
    segment_id = _segment_id_from_range(qa_range_start, qa_range_end)
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        data["stalled_state"] = {
            "segment_id": segment_id,
            "qa_range_start": int(qa_range_start),
            "qa_range_end": int(qa_range_end),
            "error_class": str(error_class),
            "reason": str(reason)[:200],
            "attempts": max(1, int(attempts)),
            "last_attempt_at": now_iso,
            "written_at": now_iso,
        }
        # 同步清掉对应 failed_segment, 避免双源歧义
        lst = data.get("failed_segments") or []
        if isinstance(lst, list):
            data["failed_segments"] = [
                e for e in lst
                if not (isinstance(e, dict) and e.get("segment_id") == segment_id)
            ]
            if not data["failed_segments"]:
                data.pop("failed_segments", None)
        # 同步清掉 adaptive_batch_* 字段 — 旧 supervisor 看到 adaptive 会无限重试。
        # 必须 hard-pop, 不能保留 (失败段已被 stalled 接管, 自适应拆批对 supervisor
        # 无意义)。仅本场景需要; 其它非失败语境 (例如 JSON parse 失败) 走原
        # _save_adaptive_batch 不会经过此函数, 因此不会误清。
        for _adapt_key in (
            "adaptive_batch_size",
            "adaptive_source_end_id",
            "adaptive_reason",
            "adaptive_updated_at",
        ):
            data.pop(_adapt_key, None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.warning(
            "observer: 失败段 %s 达到重试上限 (attempts=%d, error_class=%s) — 写 stalled 状态, "
            "supervisor 不再重试, 需人工 review 后 _clear_stalled_state 重置",
            segment_id, attempts, error_class,
        )
    except Exception as e:
        logger.warning(
            "写 stalled_state (segment=%s) 失败 — 这是兜底中的兜底, 需要 review: %s",
            segment_id, _safe_err(e)[:120],
        )


def _load_stalled_state(cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """读 stalled_state: None / dict ; 非法值 / 文件不存在都返 None。"""
    path = _state_path(cfg)
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return None
        v = data.get("stalled_state")
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _clear_stalled_state(cfg: dict[str, Any] | None = None) -> bool:
    """手动清除 stalled_state — 普通成功路径不调 (它的写入即触发守卫)。

    Returns:
        True 表示清掉了; False 表示本来就没有 / 文件不存在。
    """
    path = _state_path(cfg)
    try:
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict) or "stalled_state" not in data:
            return False
        data.pop("stalled_state", None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        logger.warning("清除 stalled_state 失败: %s", _safe_err(e)[:120])
        return False


# ────────────────────────────────────────────────────────────
# v4 snapshot 预算 — 纯函数, 根据模型上下文容量反算可注入字符数
# ────────────────────────────────────────────────────────────

# 默认上下文容量取自 v4 项目设计 (MiniMax-M3 200k 上下文, 当前生产实测的可用区间)。
# 这些常量仅作缺省值; 任何用户/配置都可以覆盖, 不硬编码供应商/凭据/endpoint。
_DEFAULT_SNAPSHOT_MODEL_CONTEXT_TOKENS = 200_000
_DEFAULT_SNAPSHOT_PERSONA_RESERVE_TOKENS = 8_000        # 人格层 (E1 y_*.md)
_DEFAULT_SNAPSHOT_SYSTEM_RESERVE_TOKENS = 4_000          # 提示词骨架 + 用户自定义模板
_DEFAULT_SNAPSHOT_QA_RESERVE_TOKENS = 16_000            # 本轮新 QA + 历史候选 question/answer
_DEFAULT_SNAPSHOT_CANDIDATE_RESERVE_TOKENS = 4_000       # 召回候选主题列表
_DEFAULT_SNAPSHOT_OUTPUT_RESERVE_TOKENS = 32_000         # 模型输出预算 (v4 当轮快照 ≤ 4K 字)
_DEFAULT_SNAPSHOT_SAFETY_MARGIN_TOKENS = 4_000           # 余量, 防止编码/计数偏差击穿模型上限
_DEFAULT_SNAPSHOT_CHARS_PER_TOKEN = 4                   # 经验值: 1 token ≈ 4 个字符 (中英混合)


def _compute_snapshot_budget(
    cfg: dict[str, Any] | None,
    fallback_max_chars: int,
) -> int:
    """v4 (2026-08-19): 纯函数 — 根据模型上下文容量估算可注入 snapshot 字符数。

    设计意图:
    - 不再硬编码 200000/30000 字符阈值; 改为基于 cfg 中可配置的 model_context_tokens
      减去 system/persona/qa/candidate/output reserve 与 safety margin, 剩余 token 预算
      再按 chars_per_token 折算成 max_chars。
    - 不做截断, 只算上限; 超过上限由调用方决定是 fail-closed 还是 warn-and-continue。
    - 保留旧调用兼容: 若 cfg 缺新键, 完全走默认值, 行为与 200000 字符旧行为接近
      (默认 model_context=200000, reserve 总和=64_000, 余 136_000 token ≈ 544_000 chars)。
    - 所有阈值配置化, 不硬编码供应商/凭据/endpoint。

    Args:
        cfg: v3-core observer 配置 dict (允许 None, 走全部默认值)。
        fallback_max_chars: cfg 完全缺省时 (例如无 model_context_tokens) 的兜底字符数。
            通常等于 cfg["snapshot_max_chars"] 的当前解析值。

    Returns:
        max_chars (int), >= 0。当模型预算被 reserve 用尽时, 可能为 0 — 调用方必须
        处理 0/负值场景 (通常走"无 snapshot 注入"或 fail-closed)。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        ctx = int(cfg.get("snapshot_model_context_tokens",
                          _DEFAULT_SNAPSHOT_MODEL_CONTEXT_TOKENS))
    except Exception:
        ctx = _DEFAULT_SNAPSHOT_MODEL_CONTEXT_TOKENS
    reserves = 0
    for key, default in (
        ("snapshot_persona_reserve_tokens", _DEFAULT_SNAPSHOT_PERSONA_RESERVE_TOKENS),
        ("snapshot_system_reserve_tokens", _DEFAULT_SNAPSHOT_SYSTEM_RESERVE_TOKENS),
        ("snapshot_qa_reserve_tokens", _DEFAULT_SNAPSHOT_QA_RESERVE_TOKENS),
        ("snapshot_candidate_reserve_tokens", _DEFAULT_SNAPSHOT_CANDIDATE_RESERVE_TOKENS),
        ("snapshot_output_reserve_tokens", _DEFAULT_SNAPSHOT_OUTPUT_RESERVE_TOKENS),
        ("snapshot_safety_margin_tokens", _DEFAULT_SNAPSHOT_SAFETY_MARGIN_TOKENS),
    ):
        try:
            reserves += int(cfg.get(key, default))
        except Exception:
            reserves += default
    try:
        cpt = int(cfg.get("snapshot_chars_per_token",
                          _DEFAULT_SNAPSHOT_CHARS_PER_TOKEN))
    except Exception:
        cpt = _DEFAULT_SNAPSHOT_CHARS_PER_TOKEN
    if cpt <= 0:
        cpt = _DEFAULT_SNAPSHOT_CHARS_PER_TOKEN
    available = max(0, ctx - reserves)
    budget = available * cpt
    # 兜底: 旧 cfg["snapshot_max_chars"] 是硬约束, 不能被新公式静默压低。
    try:
        fb = int(fallback_max_chars) if fallback_max_chars is not None else 0
    except Exception:
        fb = 0
    if fb > 0:
        return max(fb, budget)
    return max(0, budget)




# ────────────────────────────────────────────────────────────
# 后台 worker — 核心流水线
# ────────────────────────────────────────────────────────────

def _observe_worker(
    cfg: dict[str, Any],
    pg_dsn: str,
    dry_run: bool = False,
    pool: Any = None,
    on_topics_commit=None,
    _pinned_lease: Any = None,
    _manage_lease: bool = True,
    _e1_service: Any = None,
) -> None:
    """后台线程: 读 QA → 召回候选 → 调 M3 → 写库 + 主题更新 + **种子压缩**.

    Phase7 升级 (vs 旧版):
    - 读 prev 时优先抽上一版 links JSONB 里的种子印, fallback 完整印
    - 写完完整印后, 立即调一次种子压缩, 种子印落到同一条 links
    - seed 失败不阻塞主流程, 也没种子 fallback 完整印 (与 phase6 一致)
    - 失败不抛, 下次自动重试 (游标未推进)
    """
    batch_size = int(cfg.get("batch_size", 10))
    max_qa_per_run = int(cfg.get("max_qa_per_run", 20))
    # v4 (2026-08-19): 自动拆批状态恢复 — 若上次因输出超 8000 软目标而留下 adaptive 状态,
    # 本轮强制用更小的 batch 处理同一时间流。下一轮 worker 用完 source_end_id 之后清掉。
    adaptive_batch_size, adaptive_source_end_id = _load_adaptive_batch(cfg)
    if adaptive_batch_size:
        max_qa_per_run = min(max_qa_per_run, adaptive_batch_size)
        logger.info(
            "observer: 恢复自动时间拆批 batch=%d, source_end=%s",
            max_qa_per_run, adaptive_source_end_id,
        )
    max_completion_tokens = int(cfg.get("max_completion_tokens", 32768))
    thinking = str(cfg.get("thinking", "disabled"))
    reasoning_split = bool(cfg.get("reasoning_split", True))
    temperature = float(cfg.get("temperature", 0.3))
    top_k = int(cfg.get("candidate_top_k", 5))
    threshold = float(cfg.get("candidate_threshold", 0.40))
    # v4: 8000 是正常目标/拆批触发线; 原子 QA 可形成更长快照 (worker 用 oversize_atomic 写入)。
    note_min = int(cfg.get("prompt_note_min_chars", 1500))
    note_max = int(cfg.get(
        "prompt_note_max_chars",
        cfg.get("observer_note_max_chars", 8000),
    ))
    # v4 (2026-08-18): seed_compress_enabled 默认 False — 种子压缩链路已退役
    # (token 净亏 + 信息有损, 防膨胀靠 E1 每天定时 + 膨胀触发); 显式 True 仍
    # 允许 legacy branch 走种子压缩, 显式 False 继续关闭 — 不删除 legacy 代码。
    seed_enabled = bool(cfg.get("seed_compress_enabled", False))
    seed_min = int(cfg.get("observer_seed_min_chars", 500))
    seed_max = int(cfg.get("observer_seed_max_chars", 800))
    seed_max_completion_tokens = int(cfg.get("seed_max_completion_tokens", 4096))
    dry_run = bool(dry_run)
    # v4 第二轮 (2026-08-20): snapshot_window_days / snapshot_max_chars 配置
    # 仍读取保留兼容 (用户 config.yaml 里这些 key 不报错), 但**主链路不再消费**
    # — 7 天全文快照注入已停用, 不再调 _load_recent_snapshots, 不再把它们当
    # 拼接预算传入。函数 _load_recent_snapshots / _compute_snapshot_budget 本体
    # 保留 (loader 不删除), 由外部脚本 / 后续阶段决定是否重启。
    snapshot_window_days = int(cfg.get("snapshot_window_days", 7))
    snapshot_max_chars = int(cfg.get("snapshot_max_chars", 200000))

    # 0.5 stage-2 (2026-08-20) stalled 守卫 — 在建立 PG 连接 / 调 LLM 前,
    # 先读 _load_stalled_state 与 cursor 比对, 决定是否直接退出。
    # 失败段必须硬卡住: 一旦写 stalled, 后面所有 trigger 都不许推进 cursor
    # 也不许自适应拆批 (否则历史 cursor=1417 已观察到的"单条 stall 反复出现"
    # 会复现)。具体语义:
    #   - stalled_state.qa_range_end > cursor → 仍在该范围内, 必须 return,
    #     警告日志明示"等人工 review + _clear_stalled_state 重置";
    #   - stalled_state.qa_range_end <= cursor → cursor 已越过该段, 视为
    #     stale (历史遗留), _clear_stalled_state 清掉再继续 — 否则会永久卡死
    #     后面所有 QA;
    #   - 没有 stalled_state → 继续往下走。
    # cursor 必须早读, 因为它只是 _state_path 上一份 JSON, 几乎免费。
    _cursor_for_guard = _load_cursor(cfg)
    _stalled_for_guard = _load_stalled_state(cfg)
    _pg_for_guard = None  # 用于回滚/关闭, 仅在极端 race 时用到
    if _stalled_for_guard is not None:
        try:
            _stalled_qa_end = int(_stalled_for_guard.get("qa_range_end", 0) or 0)
        except Exception:
            _stalled_qa_end = 0
        try:
            _stalled_qa_start = int(_stalled_for_guard.get("qa_range_start", 0) or 0)
        except Exception:
            _stalled_qa_start = 0
        if _stalled_qa_end > _cursor_for_guard:
            logger.warning(
                "observer: stalled_state 守卫触发 — 段 [%d, %d) (segment_id=%s) "
                "仍在 cursor=%d 之前, 不调 LLM / 不写库 / 不推进 cursor; "
                "需人工 review 后 _clear_stalled_state 重置 (error_class=%s, "
                "attempts=%s, reason=%s)",
                _stalled_qa_start, _stalled_qa_end,
                _stalled_for_guard.get("segment_id", "?"),
                _cursor_for_guard,
                _stalled_for_guard.get("error_class", "?"),
                _stalled_for_guard.get("attempts", "?"),
                str(_stalled_for_guard.get("reason", ""))[:100],
            )
            # 早于 PG 连接, 这里没有活跃连接可关; 仅日志兜底安全。
            if _pg_for_guard is not None:
                try:
                    _pg_for_guard.rollback()
                except Exception:
                    pass
                try:
                    _pg_for_guard.close()
                except Exception:
                    pass
            return
        # cursor 已越过该段 → stale, 显式清掉避免永久卡住后续 QA。
        try:
            _clear_stalled_state(cfg)
            logger.warning(
                "observer: stalled_state 已 stale (qa_range_end=%d ≤ cursor=%d), "
                "清掉守卫, 继续本轮 — 历史 _clear_stalled_state 后正确解锁后续 QA",
                _stalled_qa_end, _cursor_for_guard,
            )
        except Exception as _e_clr_stale:
            logger.warning(
                "observer: 清理 stale stalled_state 失败(不阻塞主流程): %s",
                _safe_err(_e_clr_stale)[:120],
            )

    # 1. 连接 PG
    _lease = _pinned_lease
    _owns_lease = _pinned_lease is None
    try:
        if _pinned_lease is not None:
            pg = _PoolPgConnection(_pinned_lease.connection, None)
        elif pool is not None:
            _lease = pool.lease(timeout=5)
            pg = _PoolPgConnection(_lease.connection, _lease.close)
        else:
            import psycopg2
            pg = psycopg2.connect(pg_dsn, connect_timeout=5)
    except Exception as e:
        logger.warning("observer: PG 连接失败(下次重试): %s", _safe_err(e)[:120])
        return


    # 1.4 v5.2 (2026-08-21) 全局触发统计 — 必须在任何 LLM (含 E1 压缩) 之前查。
    # 失败 fail-closed: 不调 LLM、不写 note、不推 cursor, 下轮重试。
    # docs/observer-trigger-v2.md: 四信号统计是 cursor 后全局未观察数据, max_qa_per_run 只是
    # 单次处理上限; 积压 > LIMIT 时 (生产 cursor=246491: 73 条/86456 字 vs 批内 30 条/37528 字)
    # 用批内局部累积做阈值判断会永远够不着 → 反复 stall。
    last_qa_id = _load_cursor(cfg)
    _trigger_stats: dict[str, Any] | None = None
    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS unobs_count,
                       COALESCE(SUM(
                           COALESCE(length(question), 0)
                           + COALESCE(length(answer), 0)
                       ), 0) AS unobs_chars,
                       MAX(timestamp) AS last_qa_ts
                FROM qa_pairs
                WHERE id > %s AND answer IS NOT NULL
                  AND session_id NOT LIKE '%%.trajectory%%'
                """,
                (last_qa_id,),
            )
            _stats_row = cur.fetchone()
        with pg.cursor() as cur:
            cur.execute(
                """
                SELECT session_id,
                       COALESCE(SUM(
                           COALESCE(length(question), 0)
                           + COALESCE(length(answer), 0)
                       ), 0) AS sess_chars
                FROM qa_pairs
                WHERE id > %s AND answer IS NOT NULL
                  AND session_id NOT LIKE '%%.trajectory%%'
                GROUP BY session_id
                """,
                (last_qa_id,),
            )
            _sess_rows = cur.fetchall()
        _trigger_stats = {
            "unobs_count": int(_stats_row[0] or 0) if _stats_row else 0,
            "unobs_chars": int(_stats_row[1] or 0) if _stats_row else 0,
            "unobs_by_sess": {
                str(r[0]): int(r[1] or 0) for r in (_sess_rows or [])
            },
            "last_qa_ts": _stats_row[2] if _stats_row else None,
        }
        logger.info(
            "observer: 全局未观察统计 count=%s chars=%s sessions=%d",
            _trigger_stats["unobs_count"],
            _trigger_stats["unobs_chars"],
            len(_trigger_stats["unobs_by_sess"]),
        )
    except Exception as e_stats:
        logger.warning(
            "observer: 全局触发统计查询失败 — fail-closed (不调 LLM/不写 note/"
            "不推进 cursor, 下轮重试): %s",
            _safe_err(e_stats)[:160],
        )
        try:
            pg.rollback()
        except Exception:
            pass
        try:
            pg.close()
        except Exception:
            pass
        return

    # 1.5 2026-08-06: 膨胀触发 — 上一轮完整印超阈值(默认 8000 字) → 观察前先跑 E1 复盘压缩
    # 事件驱动: 每轮观察调用前检查一次, 不轮询.
    # v4 阶段0 修复 (2026-08-20): observer_e1_before_run 配置开关, **默认关闭** —
    # 该热路径每轮额外调 LLM, 阶段0回放性能回归根因之一。关闭后只跳过该热路径, E1 独立能力 (定时任务) 仍
    # 完全保留 — 不删除 E1 函数, 不改 E1 调度入口, 只在 observer 每次尝试前加一个 gate。
    # 2026-08-21 闭锁: 严格路径 — E1 压缩失败必须短路:
    #   - 不调 synthesize_yin (E1 主印后续)
    #   - 不写本轮 observation_note
    #   - 不推进 cursor
    #   - 必须 _save_failed_segment(error_class="e1_compression_*") 让原始 QA 保留待重试
    # 任何 E1 失败路径都不允许 "logger.warning 继续走"。
    # 最新观察者印 SELECT 必须显式排除 session_summary_* 行 (version LIKE 'v%' 过滤,
    # 含 v*-compressed) — 之前 ORDER BY id DESC LIMIT 1 在生产把 id=356 (session_summary)
    # 当观察者印, 闭环断裂。
    # v5.2 (2026-08-21): E1 必须跑在 stats 之后 (1.4 步), stats 失败已在 1.4 fail-closed return,
    # 这里不需要再独立 fail-closed stats; E1 失败仍保留原 1.5 路径延迟到读 QA 后落失败段。
    notes_table = str(cfg.get("notes_table", "observation_notes"))
    _e1_compression_failure_reason: str | None = None
    try:
        _observer_cfg = cfg.get("observer") or {}
        if not isinstance(_observer_cfg, dict):
            _observer_cfg = {}
        _e1_before_run_enabled = bool(
            _observer_cfg.get("observer_e1_before_run",
                              cfg.get("observer_e1_before_run", False))
        )
        if not _e1_before_run_enabled:
            logger.debug(
                "observer: observer_e1_before_run=False, 跳过膨胀触发 E1 热路径 (E1 独立能力未删)"
            )
        else:
            _infl_threshold = int(
                _observer_cfg.get("inflation_threshold",
                                  cfg.get("inflation_threshold", 8000))
            )
            # 闭锁 (2026-08-21): 读最新观察者印必须走 version LIKE 'v%' 过滤;
            # 同时按 compressed 优先级排 — 让 prev_text 优先选已 accepted 的压缩版。
            with pg.cursor() as cur:
                cur.execute(
                    f"SELECT id, version, length(content) FROM "
                    f"{_safe_notes_table(notes_table)} "
                    "WHERE version LIKE 'v%' "
                    "ORDER BY id DESC LIMIT 1"
                )
                _row = cur.fetchone()
            if _row and _row[0] and _row[2] and _row[2] > _infl_threshold:
                logger.info(
                    "observer: 观察者印 %d 字 > 阈值 %d — 观察前先跑 E1 复盘压缩",
                    _row[0], _infl_threshold,
                )
                def _run_e1_work() -> None:
                    nonlocal _e1_compression_failure_reason
                    from . import e1 as _e1_mod
                    _e1_cfg = cfg
                    if _pinned_lease is not None:
                        from .runtime_services import _PinnedSchedulerPoolView
                        _e1_pool_kwargs = {
                            "pool": _PinnedSchedulerPoolView(_pinned_lease),
                        }
                    elif pool is not None:
                        _e1_pool_kwargs = {"pool": pool}
                    else:
                        _e1_pool_kwargs = {}
                    # Stage 7: forward the outer _observe_worker commit marker
                    # (passed in as on_topics_commit) into synthesize_yin only.
                    # compress_observer_note_strict does NOT accept the kwarg, so
                    # we must NOT spread it into that call. The marker is the
                    # same one ObserverService._execute_observer_worker_locked
                    # wires in as the 'callback_seen' trigger for the deferred
                    # cache invalidation that finally fires after Observer
                    # unlock + outer lease close.
                    _synth_kwargs = dict(_e1_pool_kwargs)
                    if callable(on_topics_commit):
                        _synth_kwargs["on_topics_commit"] = on_topics_commit
                    try:
                        _strict_result = _e1_mod.compress_observer_note_strict(
                            _e1_cfg,
                            dry_run=False,
                            notes_table=notes_table,
                            target_note_id=int(_row[0]),
                            **_e1_pool_kwargs,
                        )
                        logger.info(
                            "observer: E1 压缩结果 — accepted=%s new_id=%s reason=%s",
                            _strict_result.get("accepted"),
                            _strict_result.get("new_id"),
                            _strict_result.get("reason", ""),
                        )
                    except _e1_mod.E1CompressionFailure as _e1_exc:
                        _e1_compression_failure_reason = str(_e1_exc)
                        logger.warning(
                            "observer: E1 压缩失败，延迟到读 QA 后记录失败段: %s",
                            _safe_err(_e1_exc)[:300],
                        )
                    except Exception as _e1_exc:
                        _e1_compression_failure_reason = (
                            f"E1 压缩未预期异常: {_safe_err(_e1_exc)[:300]}"
                        )
                        logger.warning(
                            "observer: E1 压缩未预期异常，延迟到读 QA 后记录失败段: %s",
                            _e1_compression_failure_reason,
                        )

                    # 保留现有 E1 身份层能力；它不是压缩本身，且只有压缩成功/无需压缩时才调用。
                    if _e1_compression_failure_reason is None:
                        try:
                            from .e1 import synthesize_yin
                            _yin_result = synthesize_yin(_e1_cfg, dry_run=False, **_synth_kwargs)
                            logger.info(
                                "observer: E1 合成主印 (膨胀触发后) — %s",
                                (_yin_result or "")[:150],
                            )
                        except Exception as _e_yin:
                            logger.warning(
                                "observer: synthesize_yin 失败(不阻塞观察): %s",
                                _safe_err(_e_yin)[:120],
                            )

                if _e1_service is not None:
                    _e1_lock_result = _e1_service.run_business_locked(
                        _run_e1_work,
                        lease=_pinned_lease,
                    )
                    if not _e1_lock_result.get("ran"):
                        _e1_compression_failure_reason = (
                            "E1 advisory lock 未获得: "
                            f"{_e1_lock_result.get('reason', 'unknown')}"
                        )
                else:
                    _run_e1_work()

    except Exception as e:
        if locals().get("_e1_before_run_enabled", False):
            _e1_compression_failure_reason = (
                f"E1 压缩前置检查异常: {_safe_err(e)[:300]}"
            )
            logger.warning(
                "observer: E1 压缩前置检查失败，延迟到读 QA 后记录失败段: %s",
                _e1_compression_failure_reason,
            )
        else:
            logger.debug("observer: 膨胀检查跳过: %s", _safe_err(e)[:100])

    topics_mutated = False
    try:
        # 2. 读上一版笔记：只取观察者链当前链头，不能让历史 compressed 行越过更新的 v*。
        try:
            notes_table_sql = _safe_notes_table(notes_table)
            with pg.cursor() as cur:
                cur.execute(
                    f"SELECT id, version, content, source_qa_range, links, created_at "
                    f"FROM {notes_table_sql} "
                    "WHERE version LIKE 'v%' "
                    "ORDER BY id DESC LIMIT 1"
                )
                prev = cur.fetchone()
            prev_id: int | None = None
            prev_text: str = ""
            prev_version: str = ""
            prev_links_raw: Any = None
            prev_created_at: Any = None
            if prev:
                prev_id = int(prev[0])
                prev_version = str(prev[1] or "")
                prev_links_raw = prev[4] if len(prev) > 4 else None
                prev_created_at = prev[5] if len(prev) > 5 else None
                # 2026-08-06: 直接读上一轮完整印 — 种子压缩已退役（token 净亏 + 信息有损）。
                # 2026-08-21: 链路 A 对照实验确认当前 M3 参数可稳定处理完整输入，
                # 因此恢复完整承接；禁止在 prompt 输入侧 head/tail 静默丢中段记忆。
                # 防膨胀统一交给输出侧“本轮变化快照”契约 + E1 治理，不截断输入。
                prev_text = str(prev[2] or "")
                logger.info(
                    "observer: 上一版完整印注入 (id=%s, chars=%d)",
                    prev_id, len(prev_text),
                )
        except Exception as e:
            logger.warning("observer: 读 observation_notes 失败(本次当首次观察): %s",
                           _safe_err(e)[:120])
            try:
                pg.rollback()  # 失败后事务 aborted, 必须回滚否则后续查询全挂
            except Exception:
                pass
            prev_id = None
            prev_text = ""
            prev_version = ""

        # 3. 读新 QA (id > cursor, 最多 max_qa_per_run)
        # v3: 同时拉 (id, session_id, timestamp, chars) 以便做四信号累积判断
        # trajectory 系统巡检过滤: NOT LIKE '%.trajectory%' (phase8m 验证 393 条/30 天)
        # v4 (2026-08-19): SQL 改 `ORDER BY timestamp ASC NULLS LAST, id ASC` 而非
        # `id ASC` — 让多 session 混合流按真实事件时间排序, 避免 100-QA 批次里
        # 同 session 但时间倒序的撕裂。同时移除固定 `length(question) > 20` 过滤:
        # eval_v2 短 QA (例如 "hi" / "yes") 是合法观察对象, 静默漏掉会导致印
        # 抽取率低于 100% — 全量覆盖, 保留 `answer IS NOT NULL` + session 过滤。
        # v5.2 (2026-08-21): last_qa_id 已在 1.4 步加载, 这里不再重复 _load_cursor。
        try:
            with pg.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, session_id, timestamp,
                           question, answer,
                           COALESCE(length(question), 0) + COALESCE(length(answer), 0) AS chars
                    FROM qa_pairs
                    WHERE id > %s AND answer IS NOT NULL
                      AND session_id NOT LIKE '%%.trajectory%%'
                    ORDER BY timestamp ASC NULLS LAST, id ASC
                    LIMIT %s
                    """,
                    (last_qa_id, max_qa_per_run),
                )
                rows = cur.fetchall()
        except Exception as e:
            logger.warning("observer: 读 qa_pairs 失败: %s", _safe_err(e)[:120])
            try:
                pg.close()
            except Exception:
                pass
            return

        if not rows:
            logger.info("observer: 无新 QA (cursor=%s), 跳过", last_qa_id)
            try:
                pg.close()
            except Exception:
                pass
            return

        # v5.2 (2026-08-21): 全局触发统计已在 1.4 步完成 (stats 失败已在 1.4 fail-closed
        # return); 这里 _trigger_stats 必然非 None, 不再二次查询。
        # 原 3.5 步 stats 块删除 — 避免重复查询 (stats 已在 E1 之前跑过)。

        # E1 压缩失败必须在取得本轮真实 QA 范围后落案底；不能用 cursor,cursor
        # 伪造范围，也不能在这里继续候选召回/observer LLM/写 note/cursor。
        if _e1_compression_failure_reason:
            _failed_qa_ids = [int(r[0]) for r in rows if r and r[0] is not None]
            if _failed_qa_ids:
                _failed_start, _failed_end = min(_failed_qa_ids), max(_failed_qa_ids)
                _failed_entry = _save_failed_segment(
                    _failed_start,
                    _failed_end,
                    error_class="e1_compression_failure",
                    reason=_e1_compression_failure_reason,
                    batch_size=len(_failed_qa_ids),
                    cfg=cfg,
                )
                if (
                    _failed_entry
                    and int(_failed_entry.get("attempts", 0))
                    >= _default_stage2_max_retries(cfg)
                ):
                    _save_stalled_state(
                        _failed_start,
                        _failed_end,
                        error_class="e1_compression_failure",
                        reason=_e1_compression_failure_reason,
                        attempts=int(_failed_entry.get("attempts", 0)),
                        cfg=cfg,
                    )
            else:
                logger.warning(
                    "observer: E1 压缩失败但本轮 QA 行没有合法 id，无法建立失败段"
                )
            try:
                pg.rollback()
            except Exception:
                pass
            try:
                pg.close()
            except Exception:
                pass
            return

        # === v3 四信号触发判断 (docs/observer-trigger-v2.md) ============================
        # ① token: 全局未观察字符 ≥ token_threshold_chars (默认 50k)
        # ② count: 全局未观察条数 ≥ count_threshold (默认 100)
        # ③ new_session: cursor 之后第一次出现的 session 且其字符 ≥ new_session_min_chars (5k)
        # ④ pause: 距上一条【任何新 QA】≥ abs_idle_hours (绝对空闲, 默认 4h) 且未观察 > 0
        # 防抖: 触发后 debounce_minutes (默认 8min) 内吸收 (用 last_update_ts 检查)
        # v4 (2026-08-19): 历史回放时 _now 必须用本批 QA 的事件时间 (取 max) 而不是 wall clock —
        # 保持历史回放时 _now 语义稳定 (与 _event_time link / 触发判断一致);
        # v4 第二轮 (2026-08-20) 7 天全文快照注入已停用, _load_recent_snapshots 不再被
        # 主链路消费, 此处仅保留 _now 的事件时间语义, 不再被 window 过滤引用。
        # v5.2 (2026-08-21): 生产实时用 wall-clock (避免积压 QA 的历史事件时间把
        # debounce 压成 False); 回放/event_time_now=True 或旧 replay 兼容
        # debounce_minutes<=0 用事件时间 (max)。event_time_now 与 now 必须保持一致:
        # 实时 (event_time_now=False) → now=datetime.now(UTC); 回放 (True) → now=max(_event_times)。
        _event_times = []
        for _r in rows:
            _ts = _r[2]
            if isinstance(_ts, datetime):
                _ts = _ts if _ts.tzinfo else _ts.replace(tzinfo=timezone.utc)
                _event_times.append(_ts.astimezone(timezone.utc))
        # event_time_now 决策: 显式 cfg.observer.event_time_now / cfg.event_time_now 优先;
        # 否则按 debounce_minutes<=0 旧 replay 兼容规则推断; 默认生产 (debounce=8) → False。
        _observer_cfg_for_clock = cfg.get("observer") or {}
        if not isinstance(_observer_cfg_for_clock, dict):
            _observer_cfg_for_clock = {}
        _event_time_now = _observer_cfg_for_clock.get(
            "event_time_now",
            cfg.get("event_time_now", None),
        )
        _debounce_min_check = int(cfg.get("debounce_minutes", 8) or 0)
        if _event_time_now is None:
            # 旧 replay 兼容: debounce_minutes<=0 是回放 runner 显式标识。
            _event_time_now = _debounce_min_check <= 0
        else:
            _event_time_now = bool(_event_time_now)
        if _event_time_now:
            # 回放/event-time: now 必须用本批事件时间 max, 与旧版语义一致。
            _now = max(_event_times) if _event_times else datetime.now(timezone.utc)
        else:
            # 生产实时: now 是 wall-clock, 注入到 _decide_v3_trigger 当墙钟。
            _now = datetime.now(timezone.utc)
        # 读 known_sessions (持久化在 observer_state.json)
        _known_sessions: set[str] = set()
        try:
            _sp = _state_path(cfg)
            if _sp.exists():
                _data = json.loads(_sp.read_text(encoding="utf-8"))
                _known_sessions = set(_data.get("known_sessions", []) or [])
        except Exception:
            pass

        _decision = _decide_v3_trigger(
            rows,
            known_sessions=_known_sessions,
            last_update_ts=prev_created_at,
            now=_now,
            cfg=cfg,
            # v4 (2026-08-19) adaptive 重试短路 — 当 observer_state.json 残留
            # adaptive_batch_size (上一轮超 4000 软目标的拆批状态) 时, 把"重试
            # 批次"显式告知 _decide_v3_trigger: 无条件 trigger=True / reason=
            # "adaptive_split", 不再被 count/token/new_session/debounce 挡掉,
            # 防止连续 stall (cursor 不动 → 永远只取 25 条 → 永远不触发)。
            adaptive_batch_active=bool(adaptive_batch_size),
            # v5 (2026-08-21) 全局触发统计 — token/count/new_session 用 cursor 后
            # 全局未观察数据判断 (1.4 步聚合), 不再用 LIMIT 批局部累积; 查询失败
            # 已在 1.4 fail-closed return, 这里必然非 None。
            trigger_stats=_trigger_stats,
            # v5.2 (2026-08-21) 实时/回放时钟边界: 实时 (debounce>0 默认生产) →
            # event_time_now=False + now=wall-clock; 回放 (event_time_now=True 或
            # 旧兼容 debounce<=0) → event_time_now=True + now=max(_event_times)。
            # 两者必须保持一致 — 见 _now 选择逻辑。
            event_time_now=_event_time_now,
        )
        _reason = _decision["reason"]
        _unobs_count = _decision["unobs_count"]
        _unobs_chars = _decision["unobs_chars"]
        # v5.1: 两个集合分开 —
        #   _new_sessions: 触发判断/审计用的未知 session (含 global-only, LIMIT 批外);
        #   _batch_new_sessions: 本批 rows 实际出现的未知 session, 成功路径写
        #   known_sessions 只能用它 — 批外 session 尚未处理, 提前标记 known 会
        #   吞掉它们后续的 new_session 触发机会。
        _new_sessions = _decision["new_sessions"]
        _batch_new_sessions = _decision["batch_new_sessions"]
        _debounce_ok = _decision["debounce_ok"]

        if _reason is None:
            logger.info(
                "observer: 未触发 (新QA=%d, 字=%d, debounce=%s, 决策=%s)",
                _unobs_count, _unobs_chars, _debounce_ok, _decision,
            )
            try:
                pg.close()
            except Exception:
                pass
            return

        logger.info(
            "observer: v3 触发 reason=%s (新QA=%d, 字=%d, debounce=%s)",
            _reason, _unobs_count, _unobs_chars, _debounce_ok,
        )

        # 4. 召回候选主题
        qa_ids = [int(r[0]) for r in rows]
        # QA 文本完整喂入 (不截断 — 长回答信息密度高, 截断丢实质; M3 1M 上下文可承载)
        # 2026-08-09: 每条 QA 带时间戳 — 之前丢 r[2] (timestamp), LLM 看不到事件时间 → 印里只有相对时间
        qa_text = "\n".join(
            f"--- QA #{i} [{r[2].strftime('%Y-%m-%d %H:%M UTC') if r[2] else '时间未知'}] ---\nQ: {r[3]}\nA: {r[4]}\n"
            for i, r in enumerate(rows, 1)
        )
        first_qa_id, last_qa_id_now = qa_ids[0], qa_ids[-1]
        if pool is not None:
            candidates = _recall_candidates(
                qa_text=qa_text,
                cfg=cfg,
                top_k=top_k,
                threshold=threshold,
                pg=pg,
            )
        else:
            candidates = _recall_candidates(
                qa_text=qa_text,
                cfg=cfg,
                top_k=top_k,
                threshold=threshold,
            )
        candidates_text = (
            "\n".join(
                f"- id={c['topic_id']} title={c['title']!r} sim={c['sim']}"
                for c in candidates if not c.get('__candidate_miss__')
            )
            if candidates and not any(c.get('__candidate_miss__') for c in candidates)
            else "(无候选, 本次主题靠 create)"
        )
        # 2026-08-07: 回补标记 — 候选召回失败时记录, 写入印 links 供补盲脚本扫描
        _candidate_miss = bool(candidates) and any(c.get('__candidate_miss__') for c in candidates)
        if _candidate_miss:
            logger.warning("observer: 候选召回失败, 本次观察将标记 candidate_miss")
        logger.info(
            "observer: 处理 %d 条 QA (range %d-%d), %d 个候选主题",
            len(rows), first_qa_id, last_qa_id_now, len(candidates),
        )

        # 5. 组装 prompt (prompt 可配置化: config.yaml prompts.observer 可覆盖)
        # v4 (2026-08-20, 第二轮最小修复): 用户最新设计决策 — 7 天 observation_notes
        # 全文背景是 P0 设计偏移, 当前 observer 基线必须停用。但要保留上一轮快照
        # (prev_text, 即最新一条 observation_notes.content) — 它是连续性参考, 不是
        # 本轮事实。停用但不删除 loader: _load_recent_snapshots 函数本身仍在,
        # 由外部脚本 / 后续阶段决定是否重启, 不在主链路里伪装 window_days=0。
        # 主链路: 不调 _load_recent_snapshots / 不读 snapshot_window_days /
        #        不读 snapshot_max_chars / 不传 snapshot_text 给 prompt。
        # 人格层仍注入 (E1 y_*.md 合成) — 它是连续身份状态, 与 7 天快照印无关。
        from .config import _resolve_prompt as _cfg_prompt
        _now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _qa_ts_list = [r[2] for r in rows if r[2] is not None]
        _qa_range_str = (
            f"{min(_qa_ts_list).strftime('%Y-%m-%d %H:%M')} ~ {max(_qa_ts_list).strftime('%Y-%m-%d %H:%M')} UTC"
            if _qa_ts_list else "未知"
        )
        # v4 第二轮收口 (2026-08-20): 人格层 (E1 y_*.md) 仍注入; 7 天内快照印停用
        # — 主链路不再调 _load_recent_snapshots, 不再创建任何 _snapshot_text 局部
        # 变量传给默认 _observer_prompt。snapshot_text 形参仅在自定义 prompt format
        # 路径保留 (旧兼容占位符必须能替换, 值固定空字符串, 不注入历史快照来源)。
        _identity_layer = _load_identity_layer(cfg)
        # v4 第二轮: 不再传 snapshot_text 给默认 _observer_prompt — 形参仅作旧调用
        # 兼容保留, 函数体不消费, 注入会徒增 token 风险。下一轮读取的是上一轮单份
        # 快照 (prev_text, 来自最新一条 observation_notes.content), 7 天全文不再
        # 拼装、不再调 _load_recent_snapshots, 不再读 snapshot_window_days /
        # snapshot_max_chars 作为主链路预算。
        logger.info(
            "observer: v4 第二轮上下文注入 — 人格层 %d 字, 上一轮单份快照 %d 字 "
            "(连续性参考, 7 天全文已停用)",
            len(_identity_layer), len(prev_text),
        )
        _obs_prompt = _cfg_prompt(cfg, "observer", None) or _observer_prompt(
            prev_note_text=prev_text,  # v4 第二轮: 接回上一轮单份快照作连续性参考
            qa_text=qa_text,
            candidates_text=candidates_text,
            note_min=note_min,
            note_max=note_max,
            now_str=_now_str,
            qa_time_range=_qa_range_str,
            identity_name=_cfg_prompt(cfg, "observer_identity", "") or "",
            dims=_resolve_observer_dimensions(cfg),
            identity_layer=_identity_layer,
            # v4 第二轮收口: 不再传 snapshot_text 给默认 _observer_prompt
            # (函数体已不消费, 形参仅作旧调用兼容)
        )
        if _obs_prompt is not _observer_prompt and isinstance(_obs_prompt, str) and "{qa_text}" in _obs_prompt:
            try:
                # v4 第二轮收口: 自定义 prompt format 路径必须保留 snapshot_text
                # 占位符 (旧兼容, 第三方模板可能引用 {snapshot_text} 不应 KeyError);
                # 但传空字符串 — 禁止注入任何 7 天 observation_notes 全文来源。
                # prev_note_text 仍传入 (上一轮单份快照, 唯一快照承接来源)。
                prompt = _obs_prompt.format(
                    prev_note_text=prev_text,
                    qa_text=qa_text,
                    candidates_text=candidates_text,
                    note_min=note_min, note_max=note_max,
                    identity_layer=_identity_layer,
                    snapshot_text="",  # 旧占位符兼容, 固定空, 禁止 7 天全文
                )
            except Exception:
                prompt = _obs_prompt  # format 失败用原样 (用户自定义)
        else:
            prompt = _obs_prompt  # 默认 prompt 或自定义已含全部内容

        if dry_run:
            # dry-run: 打日志 + 不调 LLM + 不写库
            logger.info(
                "observer: DRY-RUN — prompt 已组装 (%d 字符), %d 条 QA, %d 候选, "
                "跳过 LLM 调用与 DB 写入",
                len(prompt), len(rows), len(candidates),
            )
            try:
                pg.close()
            except Exception:
                pass
            return

        # 6. 调 M3 — 2026-08-09: 改从 v3-core 自身 config 读 LLM key,
        #    不再依赖 compression_engine (死依赖, 停产模块, 导致标准安装不可用)
        api_key, _skip_reason = _observer_resolve_api_key()
        if _skip_reason:
            if _skip_reason == "credential_missing":
                logger.warning("observer: 未找到 LLM api_key, 本次跳过(下次自动重试, 游标不推进)")
            try:
                from . import llmstatus as _obs_ls
            except ImportError:
                _obs_ls = None  # type: ignore
            if _obs_ls is not None:
                try:
                    _obs_ls.record_observer_skip(_skip_reason)
                except Exception:
                    pass
            try:
                pg.close()
            except Exception:
                pass
            return
        # _llm for merged (re-derive since helper isolated extraction)
        try:
            from .config import resolve_config as _rc2
            _tmp_cfg = _rc2(return_legacy=True) or {}
            _llm = _tmp_cfg.get("llm", {}) if isinstance(_tmp_cfg, dict) else {}
        except Exception:
            _llm = {}

        # 合并 observer cfg (model/base_url/temperature) 到 llm cfg,
        # observer 字段优先级更高 (用户显式配置覆盖)
        merged: dict[str, Any] = dict(_llm if isinstance(_llm, dict) else {})
        for k, v in cfg.items():
            if v is not None:
                merged[k] = v
        # 兼容字段名
        merged.setdefault("temperature", temperature)
        merged.setdefault("max_completion_tokens", max_completion_tokens)

        t0 = time.time()
        result = call_m3(
            merged, api_key, prompt,
            max_completion_tokens=max_completion_tokens,
            thinking=thinking,
            reasoning_split=reasoning_split,
        )
        latency = result.get("latency", time.time() - t0)
        # v4 (2026-08-19): M3 error 状态机收口 — primary 失败 + (可选) fallback 失败
        # 都不能绕过 adaptive 拆批。任一失败都必须落到下面的统一失败处理段,
        # 否则 12 QA 全部超时/超限时 worker 直接 return, 第二天仍原 12 条重喂,
        # 永远 stall。这是当前 canonical observer.py 的缺口: 失败直接 close+return,
        # 只跳过, 永不拆。修复: 用统一的 _m3_failed_path 标志, 失败时一次进入
        # 自适应拆批逻辑 (近似 JSON 解析失败的分叉: 多条拆批, 单条只跳过)。
        _m3_failed_path: str | None = None  # None=成功; "primary"=主失败; "fallback"=主+fallback均失败
        if "error" in result:
            err_str = str(result["error"])
            try:
                from . import llmstatus as _obs_llmstatus_err
                _obs_llmstatus_err.record_observer_error(_obs_llmstatus_err.classify_llm_error(error_text=err_str))
            except Exception:
                pass
            # 2026-08-09: 敏感拦截 (422 new_sensitive 1026) 同模型重试必败 → 立即切 fallback 模型
            # (冷启动已有同款: cold_start.py 敏感拦截立即切 fallback, 不重试)
            _is_sensitive = ("422" in err_str) or ("new_sensitive" in err_str) or ("1026" in err_str)
            _fb_cfg = dict(cfg.get("fallback_llm") or {}) if isinstance(cfg.get("fallback_llm"), dict) else {}
            if _is_sensitive and _fb_cfg.get("model"):
                logger.warning("observer: 敏感拦截(1026), 切 fallback 模型 %s 重试", _fb_cfg.get("model"))
                _fb_key = _fb_cfg.get("api_key") or _fb_cfg.get("apiKey") or api_key
                _fb_result = call_m3(
                    {**merged, "model": _fb_cfg.get("model"),
                     "base_url": _fb_cfg.get("base_url") or merged.get("base_url"),
                     "proxy": _fb_cfg.get("proxy") or merged.get("proxy")},
                    _fb_key, prompt,
                    max_completion_tokens=max_completion_tokens,
                    thinking=thinking, reasoning_split=reasoning_split,
                )
                if "error" not in _fb_result:
                    result = _fb_result
                    latency = result.get("latency", time.time() - t0)
                    logger.info("observer: fallback 成功 (%s, %d 字)", _fb_cfg.get("model"), len(result.get("text", "") or ""))
                else:
                    _fb_err = str(_fb_result["error"])
                    logger.warning("observer: fallback 也失败: %s", _fb_err[:120])
                    # 不要 return — 落到下面的统一失败处理, 走 adaptive 拆批
                    _m3_failed_path = "fallback"
                    result = {"error": f"primary: {err_str[:80]} | fallback: {_fb_err[:80]}"}
            else:
                # 不要 return — 落到下面的统一失败处理, 走 adaptive 拆批
                _m3_failed_path = "primary"
                logger.warning("observer: M3 调用失败(下次重试): %s | latency=%.1fs",
                               err_str, latency)
        # 统一失败态分支: primary 失败 / fallback 失败 都过这里
        if _m3_failed_path is not None and "error" in result:
            if len(rows) > 1:
                # 多条 QA: 复用现有 _save_adaptive_batch (与 JSON 解析失败 / 多条超长
                # 输出走同一契约), 把 batch 减半 → 下次 worker 按 adaptive reason
                # 把范围缩小后重发 LLM。本批不写 note, 不推进 cursor (与其它失败
                # 路径语义一致)。
                new_batch = max(1, len(rows) // 2)
                _save_adaptive_batch(new_batch, last_qa_id_now, cfg=cfg)
                # v4 stage-2 (2026-08-20): 同时持久化 failed_segment 并检查 stalled
                # 阈值 — 多条本轮由 adaptive 拆批处理, 但 attempts 计数必须累加,
                # 否则永远 stall 而无任何记录 (审计盲区)。
                _seg_entry = _save_failed_segment(
                    first_qa_id, last_qa_id_now,
                    error_class=f"m3_{_m3_failed_path}",
                    reason=str(result.get("error", ""))[:200],
                    batch_size=len(rows),
                    cfg=cfg,
                )
                if _seg_entry is not None:
                    _max_retries = _default_stage2_max_retries(cfg)
                    if int(_seg_entry.get("attempts", 0)) >= _max_retries:
                        _save_stalled_state(
                            first_qa_id, last_qa_id_now,
                            error_class=f"m3_{_m3_failed_path}",
                            reason=str(_seg_entry.get("reason", ""))[:200],
                            attempts=int(_seg_entry.get("attempts", 0)),
                            cfg=cfg,
                        )
                        # stalled 写入后必须停 — supervisor 不能再 retry
                        try:
                            pg.rollback()
                        except Exception:
                            pass
                        try:
                            pg.close()
                        except Exception:
                            pass
                        return
                logger.warning(
                    "observer: M3 调用失败 (%s), 本批 %d 条 QA, 落 adaptive 状态 "
                    "batch=%d, source_end_id=%d, 不写 note, 不推进 cursor "
                    "(等下一轮按 adaptive 处理)",
                    _m3_failed_path, len(rows), new_batch, last_qa_id_now,
                )
                try:
                    pg.rollback()
                except Exception:
                    pass
                try:
                    pg.close()
                except Exception:
                    pass
                return
            # 单条 QA: v5.3 (2026-08-26) 先判确定性敏感 — 敏感重试必败, stalled
            # 只会把「无限烧额度」换成「永久卡死」; 正确出口 = 审计留档 + 游标
            # 推进越过该条 (与 cold_start --max-filter-skips 同语义)。非敏感
            # 失败保持原语义: failed_segment 计数 + stalled 兜底, 下轮重试。
            _is_sensitive_single = _is_m3_sensitive_error(
                str(result.get("error", "")))
            if _is_sensitive_single:
                _record_skipped_sensitive(
                    first_qa_id, last_qa_id_now,
                    reason=str(result.get("error", ""))[:200],
                    cfg=cfg,
                )
                logger.warning(
                    "observer: 单条 QA %d 确定性敏感拦截 → 跳过留档, "
                    "游标推进越过该条继续消化后续 QA",
                    last_qa_id_now,
                )
                # 游标推进越过毒条 — 这是「跳过」与「失败」的本质区别:
                # 不推进 = 下轮原样重喂 = 永远卡死; 推进 = 有界跳过。
                _save_cursor(last_qa_id_now, cfg=cfg)
                try:
                    pg.rollback()
                except Exception:
                    pass
                try:
                    pg.close()
                except Exception:
                    pass
                return
            _seg_entry = _save_failed_segment(
                first_qa_id, last_qa_id_now,
                error_class=f"m3_{_m3_failed_path}",
                reason=str(result.get("error", ""))[:200],
                batch_size=1,
                cfg=cfg,
            )
            _stalled_now = False
            if _seg_entry is not None:
                _max_retries = _default_stage2_max_retries(cfg)
                if int(_seg_entry.get("attempts", 0)) >= _max_retries:
                    _save_stalled_state(
                        first_qa_id, last_qa_id_now,
                        error_class=f"m3_{_m3_failed_path}",
                        reason=str(_seg_entry.get("reason", ""))[:200],
                        attempts=int(_seg_entry.get("attempts", 0)),
                        cfg=cfg,
                    )
                    _stalled_now = True
            logger.warning(
                "observer: M3 调用失败 (%s), 单条 QA 跳过不推进 (attempts=%s, stalled=%s)",
                _m3_failed_path,
                (_seg_entry or {}).get("attempts"),
                _stalled_now,
            )
            try:
                pg.close()
            except Exception:
                pass
            return

        try:
            from . import llmstatus as _obs_ls_ok
            _obs_ls_ok.record_observer_success()
        except Exception:
            pass
        text = result.get("text", "") or ""
        usage = result.get("usage") or {}
        logger.info(
            "observer: M3 返 %d 字 (prompt_tokens=%s completion_tokens=%s latency=%.1fs)",
            len(text),
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
            latency,
        )

        # 7. 解析双输出 JSON
        parsed = _parse_observer_json(text)
        if not parsed:
            # 恢复 primary 自身重试 (铁律: M3 偶发脏 JSON 必须重试≥3次, 不能只靠 fallback) —
            # 8/20 stage-2 把重试整段换成 fallback 一次, 导致 fallback key 失效时直接卡死整批。
            # primary 同 prompt 重发 1-3 次 (默认 2); 成功即走正常流程。
            _primary_retries = max(1, int(cfg.get("json_parse_primary_retries", 2) or 2))
            for _attempt in range(1, _primary_retries + 1):
                logger.warning(
                    "observer: JSON 解析失败, primary 重试 %d/%d (fallback 未启用或失败时仍可救回)",
                    _attempt, _primary_retries,
                )
                try:
                    time.sleep(2 * _attempt)
                except Exception:
                    pass
                try:
                    _retry_result = call_m3(
                        merged, api_key, prompt,
                        max_completion_tokens=max_completion_tokens,
                        thinking=thinking, reasoning_split=reasoning_split,
                    )
                except Exception as _e_retry:
                    logger.warning("observer: primary 重试调用异常: %s", _safe_err(_e_retry)[:120])
                    continue
                if "error" not in _retry_result:
                    _retry_text = _retry_result.get("text", "") or ""
                    _parsed_retry = _parse_observer_json(_retry_text)
                    if _parsed_retry:
                        parsed = _parsed_retry
                        logger.info("observer: primary 重试成功 (%d 字)", len(_retry_text))
                        break
                    logger.warning(
                        "observer: primary 重试返 %d 字仍不可解析, 继续重试",
                        len(_retry_text),
                    )
                else:
                    logger.warning(
                        "observer: primary 重试失败: %s",
                        str(_retry_result["error"])[:120],
                    )
            # v4 (2026-08-20) stage-2 修复: JSON 解析失败时, 在尝试持久化失败段
            # 之前先走一次 fallback_llm (LLM 层而非 embedding 层) — 因为当前 primary
            # 模型可能只是该次输出格式漂了, 一个不同模型常常能稳定产出可解析 JSON。
            # fallback 仍由 cfg.fallback_llm 显式配置; 配置缺失时直接走持久化路径。
            _json_secondary_fb_text: str | None = None
            _json_secondary_fb_model: str | None = None
            try:
                _fb_cfg = dict(cfg.get("fallback_llm") or {}) \
                    if isinstance(cfg.get("fallback_llm"), dict) else {}
                if _fb_cfg.get("model"):
                    _fb_key = _fb_cfg.get("api_key") or _fb_cfg.get("apiKey") or api_key
                    logger.warning(
                        "observer: JSON 解析失败, 切 fallback_llm (%s) 重试一次 (阶段 2 修复)",
                        _fb_cfg.get("model"),
                    )
                    _fb_result_2 = call_m3(
                        {**merged, "model": _fb_cfg.get("model"),
                         "base_url": _fb_cfg.get("base_url") or merged.get("base_url"),
                         "proxy": _fb_cfg.get("proxy") or merged.get("proxy")},
                        _fb_key, prompt,
                        max_completion_tokens=max_completion_tokens,
                        thinking=thinking, reasoning_split=reasoning_split,
                    )
                    if "error" not in _fb_result_2:
                        _json_secondary_fb_text = _fb_result_2.get("text", "") or ""
                        _json_secondary_fb_model = _fb_cfg.get("model")
                        logger.info(
                            "observer: fallback_llm 返 %d 字 (%s)",
                            len(_json_secondary_fb_text), _json_secondary_fb_model,
                        )
                        parsed = _parse_observer_json(_json_secondary_fb_text)
                        if parsed:
                            logger.info(
                                "observer: fallback_llm 输出解析成功, 走正常流程"
                            )
                    else:
                        logger.warning(
                            "observer: fallback_llm 也失败: %s",
                            str(_fb_result_2["error"])[:120],
                        )
            except Exception as _e_fb_parse:
                logger.warning(
                    "observer: fallback_llm 重试异常(走持久化路径): %s",
                    _safe_err(_e_fb_parse)[:120],
                )
                parsed = None
            if not parsed:
                # 解析失败恢复 (primary + fallback_llm 均失败): 多条 QA 批次尝试
                # 自适应拆批 (复用现有 _save_adaptive_batch), 把 batch 减半 → 下次
                # worker 按 adaptive reason 把范围缩小后重发 LLM; 同时按 stage-2
                # 路径持久化 failed_segment + 检查 stalled 阈值。
                if len(rows) > 1:
                    new_batch = max(1, len(rows) // 2)
                    _save_adaptive_batch(new_batch, last_qa_id_now, cfg=cfg)
                # 持久化 failed_segment: 多条 / 单条都写 (审计用); 单条必触发 stalled
                # 检查 (多条只写记录由 adaptive 拆批继续处理)。
                _seg_entry = _save_failed_segment(
                    first_qa_id, last_qa_id_now,
                    error_class="json_parse",
                    reason=(
                        f"json_parse_failed"
                        f"{'_fb_after_fallback=' + _json_secondary_fb_model if _json_secondary_fb_model else '_no_fallback'}"
                    ),
                    batch_size=len(rows),
                    cfg=cfg,
                )
                if _seg_entry is not None:
                    _max_retries = _default_stage2_max_retries(cfg)
                    if int(_seg_entry.get("attempts", 0)) >= _max_retries:
                        _save_stalled_state(
                            first_qa_id, last_qa_id_now,
                            error_class="json_parse",
                            reason=str(_seg_entry.get("reason", ""))[:200],
                            attempts=int(_seg_entry.get("attempts", 0)),
                            cfg=cfg,
                        )
                        # stalled 写入后必须停 — supervisor 不能再 retry
                        try:
                            pg.rollback()
                        except Exception:
                            pass
                        try:
                            pg.close()
                        except Exception:
                            pass
                        return
                logger.warning(
                    "observer: JSON 解析失败, 本批 %d 条 QA (primary + fallback_llm 均失败), "
                    "不写 note, 不推进 cursor",
                    len(rows),
                )
                try:
                    pg.rollback()
                except Exception:
                    pass
                try:
                    pg.close()
                except Exception:
                    pass
                return

        # 8. 提取字段 (容错：缺字段给空字符串, 不抛)
        note_obj = parsed.get("note") or {}
        note_content = str(note_obj.get("content") or "").strip()
        # source_qa_range 必须用代码真值 (first_qa_id/last_qa_id_now), 不信 LLM 输出
        # (M3 会编造 [1,30) 之类的默认值覆盖真实范围 — 2026-08-03 数据审计发现)
        qa_range_start = first_qa_id
        # source_qa_range 是 PG INT8RANGE '[start,end)' 半开区间; 上界必须 exclusive,
        # 真实最后一个 QA id 写到 links.last, 区间 end 必须是 last_qa_id_now + 1。
        # 旧版写 last_qa_id_now → end 与 last 重合, 单条 QA 触发时空区间文本变 "empty"。
        qa_range_end = last_qa_id_now + 1
        topic_updates = parsed.get("topic_updates") or []
        if not isinstance(topic_updates, list):
            topic_updates = []

        # v4 (2026-08-20, 第二轮最小修复): 把 oversize 变量初始化提到与
        # topic_updates 同层 — 上一轮把它嵌在 `if not isinstance(topic_updates,
        # list)` 内 (12 空格缩进), 正常 topic_updates=list 路径完全不执行, 后续
        # writer 调用 _writer_links / oversize_atomic= 时直接 UnboundLocalError。
        # 长度契约改为"事件多就写长, 不得漏":
        # - 8000 仍是软目标 (拆批触发线), 但**有效结构化输出不得因字符数被拒** —
        #   不论是单条 QA 还是多条 QA 批次, content 完整保留, 不截断, 推进 cursor。
        # - 单条 QA 超长 (first == last): kind=oversize_atomic link + oversize_atomic=True 写入。
        # - 多条 QA 批次超长 (first < last): kind=oversize_batch link + oversize_atomic=True
        #   写入; 显式可观测 (link + warning log), 与单条 oversize_atomic 区分语义, 不把
        #   "多条批次的事实多" 伪装成"单条原子保留"信号。
        # - 真正失控的输出 (解析失败 / M3 调用失败) 仍走原 adaptive 拆批路径, 不变。
        single_qa = (first_qa_id == last_qa_id_now)
        is_oversize = len(note_content) > V4_NOTE_MAX_CHARS
        is_oversize_batch = bool(is_oversize and not single_qa)
        oversize_atomic = bool(is_oversize and single_qa)
        if is_oversize_batch:
            # 不再 adaptive 拆批 + return — 有效结构化输出必须写入, 推进 cursor。
            # 标记: kind=oversize_batch link 供后续观测 (有别于单条 oversize_atomic);
            # oversize_atomic=True 让 _write_observation_note 跳过 max_chars 校验。
            logger.warning(
                "observer: 本批 %d 条 QA 有效 JSON 输出 %d 字 > 软目标 %d — "
                "按 v4 阶段0契约继续写入 (kind=oversize_batch), 不截断, "
                "不推进 adaptive 拆批, cursor 照常推进",
                len(rows), len(note_content), V4_NOTE_MAX_CHARS,
            )

        if not note_content:
            logger.warning("observer: 解析后 note.content 为空, 跳过写入")
            try:
                pg.close()
            except Exception:
                pass
            return

        # 9. 写 observation_notes (phase7: 种子压缩紧随其后, 落到同一条 links)
        # v4 (2026-08-16): content = note_content = LLM 输出的当轮快照 (2-4K 字)
        # **不拼接 prev 全量** — 每轮独立快照段, 印库 = 按时间排列的快照序列
        # v4 第二轮 (2026-08-20): 下一轮读取的是**上一轮单份**快照 (prev_text, 即
        # 最新一条 observation_notes.content), 由 _observe_worker 第 2 步直接
        # SELECT id DESC LIMIT 1 拿到, **不是** _load_recent_snapshots 7 天窗口
        # 拼接 (7 天全文已停用, _load_recent_snapshots 主链路不再调用)。
        version = (
            f"v{int(prev_id) + 1}" if prev_id else "v1"
        )
        seed_text: str | None = None  # 阶段赋值, 写完后塞 links
        # v4 (2026-08-19): 组装 links — kind=event_time 必带 (本批 QA 最早/最晚 timestamp UTC ISO,
        # naive datetime 视为 UTC); 与现有 qa/topics/candidate_miss/trigger/snapshot 共存,
        # 不覆盖任何 link (后续读取按 kind 区分, 旧 note 没 event_time 时回退 created_at)。
        # v4 (2026-08-19): 走纯函数 _build_event_time_link — 隔离 IO/字符串解析,
        # 隔离于 _parse_event_time_from_links (loader 内部, 不再被主链路消费)。
        _ts_min = min((r[2] for r in rows if r[2] is not None), default=None)
        _ts_max = max((r[2] for r in rows if r[2] is not None), default=None)
        _event_link = _build_event_time_link(_ts_min, _ts_max)
        # 9a. 先把完整印写下去 (id 拿到)
        # v3: links 里追加 {"kind": "trigger", "reason": "<token|count|new_session|pause>"}
        # v4 (2026-08-18):
        # - links 加 {"kind": "snapshot", "version": "v4"} 标识当轮快照段
        # - 加 {"kind": "event_time", start, end} 供历史回放窗口过滤
        # - 单条 QA 超长时加 {"kind": "oversize_atomic", char_count, max_chars}
        #   (8000 是软目标, 不是死亡线 — 单条原子保留, 不截断)
        # - max_chars 仍传 V4_NOTE_MAX_CHARS (8000), 但只有当单条 QA 显式允许 (oversize_atomic)
        #   才能写入; 多条批次超长已在阶段0修复后走 kind=oversize_batch link 写入
        #   (不截断, 不进 adaptive 拆批, cursor 照常推进) — oversize_batch 已写入
        #   路径, 不再是"被 adaptive 拦住"反事实。
        _writer_links: list[Any] = [
            {"kind": "qa", "first": first_qa_id, "last": last_qa_id_now},
            # 来源元数据: 本轮读了哪些主题卡 (代码真值, 不依赖 LLM 标注)
            {"kind": "topics", "topic_ids": [c["topic_id"] for c in candidates if not c.get('__candidate_miss__')]},
            # 2026-08-07: 候选召回失败标记 (回补机制) — 补盲脚本扫描此标记重跑候选
            ({"kind": "candidate_miss", "reason": "recall_failed"} if _candidate_miss else None),  # noqa: E501 — 下方统一过滤 None
            # v3 触发原因 + 决策数据 (可审计: 事后能回溯为何触发)
            {
                "kind": "trigger",
                "reason": _reason,
                "data": {
                    "unobs_chars": _unobs_chars,
                    "unobs_count": _unobs_count,
                    "new_sessions": len(_new_sessions),
                    "debounce_ok": _debounce_ok,
                },
            },
            # v4: 标识本条 note 是 v4 当轮快照段 (不拼接 prev 全量),
            # 与 qa / topics / candidate_miss / trigger links 共存, 不破坏已有 link。
            {"kind": "snapshot", "version": "v4"},
        ]
        if _event_link is not None:
            _writer_links.append(_event_link)
        if oversize_atomic:
            _writer_links.append({
                "kind": "oversize_atomic",
                "char_count": len(note_content),
                "max_chars": V4_NOTE_MAX_CHARS,
            })
        if is_oversize_batch:
            # v4 阶段0 (2026-08-20) — 多条 QA 批次超长标记, 与单条 oversize_atomic 区分
            # 语义 (前因是"事件多"而不是"单条原子保留"); 调用 _write_observation_note
            # 时同样以 oversize_atomic=True 跳过 max_chars 校验。
            _writer_links.append({
                "kind": "oversize_batch",
                "qa_count": len(rows),
                "char_count": len(note_content),
                "max_chars": V4_NOTE_MAX_CHARS,
            })
        try:
                    # 2026-08-20 容器 smoke (--max-qa 2 --once) 暴露: 多条 QA 超长分支
                    # is_oversize_batch=True 时, _writer_links 已追加 kind=oversize_batch 标记,
                    # 但传给 writer 的 oversize_atomic=oversize_atomic (单条侧) 在多条路径下
                    # 仍为 False → writer 走 max_chars 8000 fail-closed → cursor 不推进。
                    # 修复: 多条超长分支显式传 oversize_atomic=True (与 kind=oversize_batch
                    # link 配对, 真正允许完整 JSON 结构化输出写入)。
                    _writer_oversize_atomic = oversize_atomic or is_oversize_batch
                    new_note_id = _write_observation_note(
                        pg,
                        version=version,
                        content=note_content,  # v4: 直接 = 当轮快照, 不拼接 prev
                        qa_range_start=qa_range_start,
                        qa_range_end=qa_range_end,
                        prev_id=prev_id,  # v4: prev_id 仍保留 (溯源/链路用), 但 content 不拼接
                        links=_writer_links,
                        # 单条 QA 超长时, writer 必须显式允许才能写入; 多条 QA 超长分支
                        # 也必须显式允许 (kind=oversize_batch 配对), 否则按 max_chars=8000
                        # fail-closed。两者并联, 不影响 single_qa=True 的原子保留路径。
                        oversize_atomic=_writer_oversize_atomic,
                        max_chars=V4_NOTE_MAX_CHARS,  # writer 软目标硬封顶
                        # phase7: seed_text 阶段写空, 下面种子压缩完再单独 UPDATE links
                        seed_text=None,
                        notes_table=notes_table,
                    )
        except Exception as e:
            logger.warning("observer: 写 observation_notes 失败(下次重试): %s",
                           _safe_err(e)[:120])
            try:
                pg.close()
            except Exception:
                pass
            return

        if not new_note_id:
            try:
                pg.close()
            except Exception:
                pass
            return

        # 10. 应用 topic_updates (单条失败不影响整体)
        # v4.1 (2026-08-19): _apply_topic_updates 返成功应用的 topic_id 列表。
        # 合并进 note.links 的 kind=topics.topic_ids (候选 + 新建 ∪, 去重保序),
        # 修复"候选为空但 topic_updates create 成功后, note 不回链新建 topic"的断链。
        # 失败补偿语义 (topic_updates_pending) 完整保留: 异常分支仍调
        # _mark_topic_updates_pending, 不破坏既有契约。
        applied_topic_ids: list[str] = []
        if topic_updates:
            try:
                _applied = _apply_topic_updates(
                    pg, new_note_id, topic_updates, cfg=cfg,
                    qa_rows=[
                        {"id": int(r[0]), "question": r[3] or "", "answer": r[4] or ""}
                        for r in rows
                    ] if rows else None,
                )
                if isinstance(_applied, list):
                    applied_topic_ids = [str(t) for t in _applied if t]
                    if applied_topic_ids:
                        topics_mutated = True
            except Exception as e:
                logger.warning("observer: topic_updates 应用失败(非致命): %s",
                               _safe_err(e)[:120])
                try:
                    pg.rollback()
                except Exception:
                    pass
                # 2026-08-07 对抗审查修复: topic_updates 失败不能静默丢 —
                # 把原始 topic_updates 存进 note.links (pending 标记),
                # 由补偿任务/补盲脚本扫描重放, 避免"写印成功但主题索引永久缺失"。
                try:
                    _mark_topic_updates_pending(pg, new_note_id, topic_updates)
                except Exception as e2:
                    logger.warning("observer: 写 topic_updates pending 标记失败: %s",
                                   _safe_err(e2)[:120])

            # v4.1: 把 applied_topic_ids 合并进 note.links 的 kind=topics.topic_ids
            # 仅在 applied_topic_ids 非空时写 — 避免无意义的 UPDATE links (防 pg 写盘抖动)。
            # 注: 异常分支也会执行这段 (但 applied_topic_ids=[] 不会发 SQL, 无副作用)。
            if applied_topic_ids:
                try:
                    with pg.cursor() as cur:
                        cur.execute(
                            "SELECT links FROM observation_notes WHERE id=%s",
                            (new_note_id,),
                        )
                        _row = cur.fetchone()
                        _links = []
                        if _row and _row[0]:
                            try:
                                _links = _row[0] if isinstance(_row[0], list) else json.loads(_row[0])
                                if not isinstance(_links, list):
                                    _links = []
                            except Exception:
                                _links = []
                        # 找 kind=topics link — 合并 topic_ids (候选 ∪ 新建, 去重保序)
                        _merged = False
                        for _lk in _links:
                            if isinstance(_lk, dict) and _lk.get("kind") == "topics":
                                    _existing_ids = _lk.get("topic_ids") or []
                                    if not isinstance(_existing_ids, list):
                                        _existing_ids = []
                                    _seen = set()
                                    _new_ids: list[str] = []
                                    for _tid in list(_existing_ids) + list(applied_topic_ids):
                                        _tid = str(_tid)
                                        if _tid and _tid not in _seen:
                                            _seen.add(_tid)
                                            _new_ids.append(_tid)
                                    _lk["topic_ids"] = _new_ids
                                    _merged = True
                                    break
                        if not _merged:
                            _links.append({
                                "kind": "topics",
                                "topic_ids": list(applied_topic_ids),
                            })
                        cur.execute(
                            "UPDATE observation_notes SET links=%s::jsonb WHERE id=%s",
                            (json.dumps(_links, ensure_ascii=False), new_note_id),
                        )
                    pg.commit()
                    logger.info(
                        "observer: 回写 note id=%s 的 links.topic_ids (新增 %d 个 topic_id)",
                        new_note_id, len(applied_topic_ids),
                    )
                except Exception as e:
                    logger.warning(
                        "observer: 回写 note.links.topic_ids 失败(非致命, 新建 topic 已写入 topics 表): %s",
                        _safe_err(e)[:120],
                    )
                    try:
                        pg.rollback()
                    except Exception:
                        pass

        # 2026-08-06: 种子压缩已退役（token 净亏 + 信息有损 — 防膨胀靠 E1）。
        # 下一轮观察直接读完整印 (见 prev_text 读取)。此段保留仅作历史注释。
        if seed_enabled and not dry_run:
            try:
                seed_text = _seed_compress(
                    full_note=note_content,
                    cfg=merged,  # 已在 #6 步合并好 (含 model/base_url/proxy/temperature)
                    api_key=api_key,
                    thinking=thinking,
                    reasoning_split=reasoning_split,
                    timeout=300,
                    seed_min=seed_min,
                    seed_max=seed_max,
                    max_completion_tokens=seed_max_completion_tokens,
                    temperature=temperature,
                    dry_run=False,
                )
                if seed_text:
                    # UPDATE links 追加 seed (不再用 _write_observation_note 二写,
                    # 避免 prev_id/created_at 重置)
                    try:
                        with pg.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE {notes_table}
                                SET links = COALESCE(links, '[]'::jsonb) || %s::jsonb
                                WHERE id = %s
                                """.format(notes_table=notes_table),
                                (
                                    json.dumps(
                                        [{
                                            "kind": "seed",
                                            "version": version,
                                            "content": seed_text,
                                            "char_count": len(seed_text),
                                        }],
                                        ensure_ascii=False,
                                    ),
                                    new_note_id,
                                ),
                            )
                        pg.commit()
                        logger.info(
                            "observer: 种子印 %d 字追加到 obs_note id=%s 的 links",
                            len(seed_text), new_note_id,
                        )
                    except Exception as e:
                        logger.warning(
                            "observer: 种子印 UPDATE links 失败(种子已压缩但未持久化, "
                            "下一轮 fallback 完整印): %s",
                            _safe_err(e)[:120],
                        )
                        try:
                            pg.rollback()
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(
                    "observer: 种子压缩链路异常(整段跳过): %s",
                    _safe_err(e)[:120],
                )
                # seed_text 保持 None, 不阻塞主流程

        # 12. 推进游标 (写到 PG 成功后才推, 写失败下次重试同范围)
        _save_cursor(last_qa_id_now, cfg)
        # v4 (2026-08-19) adaptive 状态清理 — 仅在 note 写成功 + cursor 推进的
        # 路径调 _clear_adaptive_batch, 失败/oversize 未推进路径不调 (让 adaptive
        # 状态保留, 下轮继续重试)。_clear_adaptive_batch 只删 adaptive_* 字段,
        # 不动 known_sessions / last_qa_id — 这是它和 _save_cursor 的关键区别。
        if adaptive_batch_size:
            try:
                _clear_adaptive_batch(cfg)
                logger.info(
                    "observer: 成功路径清理 adaptive 状态 (batch=%d → cleared, cursor=%d)",
                    adaptive_batch_size, last_qa_id_now,
                )
            except Exception as _e_clr:
                # 清理失败不阻塞主流程 — adaptive 状态留了下次再清即可
                logger.warning(
                    "observer: 清理 adaptive 状态失败(不阻塞, 下次仍会重试清理): %s",
                    _safe_err(_e_clr)[:120],
                )
        # v4 stage-2 (2026-08-20): 成功路径必须清理同段的 failed_segment 记录,
        # 让之前的 attempts 计数归零 — 否则下次跑同一段时 attempts 已被上一轮
        # 失败抬高, 可能误触发 stalled (审核范围收窄)。stalled_state 也一并清
        # (本段已成功 = 该 stalled 守卫自动解除; 否则仍写在那会让 supervisor 误判
        # 后面的成功尝试为 stalled)。_clear_* 不抛, 失败仅 warning 不阻塞。
        try:
            if _clear_failed_segment(last_qa_id_now, cfg=cfg):
                logger.info(
                    "observer: 成功路径清理 failed_segment (cursor=%d, stall_guard 解除)",
                    last_qa_id_now,
                )
        except Exception as _e_clr_fs:
            logger.warning(
                "observer: 清理 failed_segment 失败(不阻塞): %s", _safe_err(_e_clr_fs)[:120]
            )
        try:
            if _clear_stalled_state(cfg=cfg):
                logger.info(
                    "observer: 成功路径清除 stalled_state — 段 %d 已成功消费",
                    last_qa_id_now,
                )
        except Exception as _e_clr_st:
            logger.warning(
                "observer: 清除 stalled_state 失败(不阻塞): %s", _safe_err(_e_clr_st)[:120]
            )
        # v3: 同步累加 known_sessions — 下次 worker 跑可见这些 session 不再"新"
        # v5.1: 只保存本批 rows 实际出现的未知 session (_batch_new_sessions)。
        # 触发判断的 _new_sessions 可能含 global-only (LIMIT 批外) session —
        # 它们的 QA 还没被处理, 提前写 known 会让后续轮次永远不再触发 new_session。
        try:
            _save_known_sessions(_batch_new_sessions, cfg)
        except Exception as e:
            logger.warning("保存 known_sessions 失败(下次可能略多触发): %s",
                           _safe_err(e)[:120])
        logger.info(
            "observer: 写入 obs_note id=%s (%d 字, %d 个 topic_updates, seed=%s), 游标推进到 %d",
            new_note_id, len(note_content), len(topic_updates),
            f"{len(seed_text)}字" if seed_text else "无(fallback)",
            last_qa_id_now,
        )
    finally:
        try:
            pg.close()
        except Exception:
            pass
        if _lease is not None and _owns_lease and _manage_lease:
            _lease.close()
        if topics_mutated and callable(on_topics_commit):
            try:
                on_topics_commit()
            except Exception as _e_cb:
                logger.warning(
                    "observer: on_topics_commit 失败 (非阻塞): %s",
                    _safe_err(_e_cb)[:120],
                )


# ────────────────────────────────────────────────────────────
# 触发入口 — maybe_observe (跟 maybe_compress 同一模式)
# ────────────────────────────────────────────────────────────

def maybe_observe(
    config: dict[str, Any] | None = None,
    *,
    dry_run: bool = False,
    pool: Any = None,
    on_topics_commit=None,
    _inline: bool = False,
    _runtime_service: Any = None,
) -> Any:
    """检查是否需要观察, 需要则在后台线程跑观察者流水线。

    与 maybe_compress 的对比:
    - 触发条件: not just "有新 QA", 而是"攒够 batch_size"或"idle 触发"
    - 后台线程: alive-check 守卫 (P10b 同一模式)
    - 失败不抛, 下次自动重试
    - dry_run: True 时不调 LLM 不写库, 仅 prompt 组装 + 日志 (验证用)

    Args:
        config: v3-core 传入的 V3Config / dict — 用于解析 PG DSN; 缺则走
                config.yaml 兜底 (再缺则连不上 PG)。
        dry_run: True 时所有副作用 (LLM 调用 + DB 写入) 都跳过, 仅日志。
        pool: 可选注入的 PgPool 实例 (Stage 3 pool mode)。

    调用契约:
    - sync_turn 末尾 (旁路, 不阻塞): maybe_observe()  ← main 集成时挂载
    - 手动验证: maybe_observe(dry_run=True)  ← 只看 prompt 是否合理
    """
    import time as _time

    cfg = _load_observer_config()
    if not cfg.get("enabled", False):
        # 默认禁用, 分支验证期手动开 (config.yaml observer.enabled=true)
        return

    # debounce — Runtime-backed inline calls use the service-owned monotonic guard;
    # legacy standalone calls retain the module-global wall-clock guard.
    debounce_seconds = int(cfg.get("debounce_seconds", 10))
    if _inline and _runtime_service is not None:
        claim_debounce = getattr(_runtime_service, "claim_debounce", None)
        if not callable(claim_debounce):
            logger.warning("observer: Runtime service lacks claim_debounce, skip")
            return
        try:
            if not claim_debounce(debounce_seconds):
                return
        except Exception as e:
            logger.warning("observer: Runtime debounce claim failed, skip: %s",
                           _safe_err(e)[:120])
            return
    else:
        _now = _time.time()
        _last = globals().get(_LAST_ATTEMPT_KEY, 0)
        if _now - _last < debounce_seconds:
            return
        globals()[_LAST_ATTEMPT_KEY] = _now

    # observer 锁守卫 — 独立于 compression 的 import_lock.flag
    # (8/5: compression 死循环止血锁长期存在, 会误挡观察者 → 观察者用自己的锁)
    try:
        from .config import _resolve_data_dir
        _lock_path = str(_resolve_data_dir(config) / "observer_lock.flag")
        if os.path.exists(_lock_path):
            logger.info("observer: 观察锁存在 (observer_lock.flag), 跳过")
            return
    except Exception:
        # config 拿不到不阻塞, 不导入就当没锁
        pass

    # PG DSN
    try:
        pg_dsn = _build_pg_dsn()
    except Exception as e:
        logger.warning("observer: 拿不到 PG DSN, 跳过本次: %s", _safe_err(e)[:120])
        return

    # 快速检查: 有没有新数据?
    last_qa_id = _load_cursor(cfg)
    latest_id = 0
    if pool is not None:
        try:
            _probe_lease = pool.lease(timeout=3)
            try:
                with _probe_lease.connection.cursor() as cur:
                    cur.execute("SELECT max(id) FROM qa_pairs")
                    row = cur.fetchone()
                latest_id = int(row[0]) if row and row[0] else 0
            finally:
                _probe_lease.close()
        except Exception as e:
            logger.warning("observer: 快速检查 qa_pairs 失败, 跳过本次: %s",
                           _safe_err(e)[:120])
            return
    else:
        try:
            import psycopg2
            pg = psycopg2.connect(pg_dsn, connect_timeout=3)
            try:
                with pg.cursor() as cur:
                    cur.execute("SELECT max(id) FROM qa_pairs")
                    row = cur.fetchone()
                latest_id = int(row[0]) if row and row[0] else 0
            finally:
                try:
                    pg.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning("observer: 快速检查 qa_pairs 失败, 跳过本次: %s",
                           _safe_err(e)[:120])
            return

    if latest_id <= last_qa_id:
        return  # 无新数据

    # alive-check 守卫: 旧 worker 在跑就跳过, 防重复触发 (跟 P10b 压缩同一形态)
    global _WORKER_THREAD
    if not _inline and _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
        return

    # observer 平铺配置只够做触发判断；E1 压缩还需要完整 legacy cfg 中的
    # storage.pg / llm / embed / basePath。启 worker 前合并两者，避免生产入口
    # 只传 observer 子段导致 E1 静默拿不到 PG/模型配置。
    worker_cfg = dict(cfg)
    try:
        from .config import resolve_config
        legacy_cfg = resolve_config(return_legacy=True)
        if isinstance(legacy_cfg, dict):
            worker_cfg = dict(legacy_cfg)
            worker_cfg.update(cfg)
            merged_observer = dict(legacy_cfg.get("observer") or {})
            merged_observer.update(cfg)
            worker_cfg["observer"] = merged_observer
    except Exception as _e_cfg:
        logger.warning(
            "observer: 合并完整 legacy cfg 失败，worker 退回 observer cfg: %s",
            _safe_err(_e_cfg)[:120],
        )

    worker_kwargs: dict[str, Any] = {}
    if pool is not None:
        worker_kwargs["pool"] = pool
    if on_topics_commit is not None:
        worker_kwargs["on_topics_commit"] = on_topics_commit
    if _inline and _runtime_service is not None:
        return _runtime_service._execute_observer_worker(
            worker_cfg,
            pg_dsn,
            dry_run,
            on_topics_commit,
        )
    t = threading.Thread(
        target=_observe_worker,
        args=(worker_cfg, pg_dsn, dry_run),
        kwargs=worker_kwargs,
        name="v3core-observer-worker",
        daemon=True,
    )
    _WORKER_THREAD = t
    t.start()
    logger.info("observer: 后台线程启动 (cursor=%s → latest=%s, dry_run=%s)",
                last_qa_id, latest_id, dry_run)


# ────────────────────────────────────────────────────────────
# 读路径 — recall_for_new_session (新 session 首轮召回)
# ────────────────────────────────────────────────────────────

# 纯本地匹配, 不挂 LLM/M3 — 安全压进 8s 实时预算
_FENCE_MARKER = "[以下是上一个会话结尾的片段, 相关就用, 无关忽略]"


def recall_for_new_session(
    config: dict[str, Any] | None = None,
    *,
    top_k: int = 5,
    session_id: str | None = None,
    pool: Any = None,
) -> dict[str, Any] | None:
    """读路径: 新 session 首轮召回记忆上下文。

    设计要点:
    - 写路径 (maybe_observe) 的对偶面 — 写完了现在能读回来
    - 严格无副作用: 纯 SELECT, 不写库, 不推进 cursor, 不调 maybe_observe
    - 严格无 LLM: 只走本地 TopicMatcher (max=几 ms — 8s 实时预算铁律)
    - 失败逐步降级:
        PG 连不上       → 返回 None
        印拿不到         → 只返 tail + topics
        tail 拿不到      → 只返 note + topics
        topics 拿不到    → 只返 note + tail
        全挂             → 返回 None

    Args:
        config: v3-core 传入的 V3Config / dict — 转发给 _load_observer_config。
                当前实现内部只用 _build_pg_dsn / _load_observer_config 自取, 保留
                接口对齐 maybe_observe, 暂不强制使用。
        top_k: 召回主题数上限, 默认 5 (与 _recall_candidates 默认一致)。
        session_id: 新 session id (目前用于日志可观测性, 不影响 SQL —
                    tail 是按 id > range_end 拿新 QA, 跨 session 通用)。
        pool: 可选注入的 PgPool 实例 (Stage 3 pool mode)。

    Returns:
        None — PG 不可达, 或三个段 (note/tail/topics) 全空。

        dict={
            "note": {"note_id", "version", "content", "range_end"},
                    缺种子印时 content 用全量 note.content 兜底。
                    range_end 是 int — 该印覆盖的 QA 上界, 用于拼 tail。
            "tail": [{"qa_id","session_id","timestamp","question","answer"}, ...]
                    时间升序 (id ASC), 默认 5-10 条。
            "topics": [{"topic_id","title","sim"}, ...]
                    来自 _recall_candidates, top_k 上限。
            "fence": 占位提示串 — caller 拼接 prompt 时用它分隔。
            "elapsed_ms": 本次召回 wall-clock ms,
        }

    降级示例:
        - 若 observation_notes 表为空 → note=None, tail/topics 仍尝试拿
        - 若 tail SQL 异常 → tail=[], 但 note + topics 仍返回
        - 若 _recall_candidates 抛错 → topics=[], note + tail 仍返回
        - 任意阶段失败 logger.warning + continue; 全挂 logger.warning 后返 None
    """
    import time as _time
    _t0 = _time.monotonic()

    note_section: dict[str, Any] | None = None
    tail_section: list[dict[str, Any]] = []
    topics_section: list[dict[str, Any]] = []
    qa_section: list[dict[str, Any]] = []

    cfg = _load_observer_config()

    pg_dsn: str | None
    try:
        pg_dsn = _build_pg_dsn()
    except Exception as e:
        logger.warning("recall_for_new_session: 拿不到 PG DSN, 全挂返 None: %s",
                       _safe_err(e)[:120])
        return None

    pg = None
    try:
        if pool is not None:
            # ── 1) & 2) 最新印与动态尾巴 (Pool 模式: 独立 bounded lease 1) ──
            try:
                _lease1 = pool.lease(timeout=5)
                try:
                    conn1 = _lease1.connection
                    with conn1.cursor() as cur:
                        # 1) 最新印
                        try:
                            cur.execute(
                                "SELECT id, version, content, source_qa_range, links "
                                "FROM observation_notes "
                                "WHERE version LIKE 'v%' "
                                "ORDER BY id DESC LIMIT 1"
                            )
                            row = cur.fetchone()
                            if row is not None:
                                note_id, version, full_content, qa_range, links = row
                                seed = _extract_prev_seed(links)
                                note_content = seed if seed else (full_content or "")
                                range_end = 0
                                try:
                                    if qa_range is not None:
                                        upper_val = qa_range.upper
                                        if upper_val is not None:
                                            range_end = int(upper_val)
                                except Exception:
                                    range_end = 0
                                note_section = {
                                    "note_id": int(note_id) if note_id is not None else 0,
                                    "version": str(version or ""),
                                    "content": str(note_content),
                                    "range_end": range_end,
                                }
                        except PrefetchDeadlineExceeded:
                            raise
                        except Exception as e:
                            logger.warning("recall_for_new_session: 取最新印失败, 跳过 note: %s",
                                           _safe_err(e)[:120])

                        # 2) 动态尾巴
                        try:
                            cur.execute("SELECT max(id) FROM qa_pairs")
                            _mx = cur.fetchone()
                            latest_id = int(_mx[0]) if _mx and _mx[0] is not None else 0

                            cursor_id = (note_section or {}).get("range_end", 0) or 0
                            uncovered = max(latest_id - cursor_id, 0)
                            tail_len = max(min(10, max(uncovered, 5)), 5)

                            cur.execute(
                                "SELECT id, session_id, timestamp, question, answer "
                                "FROM qa_pairs "
                                "WHERE id > %s AND answer IS NOT NULL "
                                "  AND session_id NOT LIKE '%%.trajectory%%' "
                                "ORDER BY id DESC LIMIT %s",
                                (cursor_id, tail_len),
                            )
                            desc_rows = cur.fetchall()
                            asc_rows = list(reversed(desc_rows))
                            for r in asc_rows:
                                qa_id, sess_id, ts, q, a = r
                                tail_section.append({
                                    "qa_id": int(qa_id) if qa_id is not None else 0,
                                    "session_id": str(sess_id or ""),
                                    "timestamp": str(ts) if ts is not None else "",
                                    "question": str(q or ""),
                                    "answer": str(a or ""),
                                })
                        except PrefetchDeadlineExceeded:
                            raise
                        except Exception as e:
                            logger.warning("recall_for_new_session: 取尾巴失败, 跳过 tail: %s",
                                           _safe_err(e)[:120])
                finally:
                    _lease1.close()
            except PrefetchDeadlineExceeded:
                raise
            except Exception as e_p1:
                logger.warning("recall_for_new_session: 取最新印/尾巴 lease 失败: %s",
                               _safe_err(e_p1)[:120])
        else:
            try:
                import psycopg2
                pg = psycopg2.connect(pg_dsn, connect_timeout=3)
            except Exception as e:
                logger.warning("recall_for_new_session: 连 PG 失败, 全挂返 None: %s",
                               _safe_err(e)[:120])
                return None

            try:
                with pg.cursor() as cur:
                    # ── 1) 最新印 ────────────────────────────────
                    # 闭锁 (2026-08-21): 读最新观察者印必须走 version LIKE 'v%' 过滤,
                    # 排除 session_summary_* 行; 同时按 compressed 优先级排, 优先选
                    # 已 accepted 的压缩版 — 与 observer 1.5 / e1.compress_observer_note
                    # 共用同一选择器语义, 保证全链路读路径一致.
                    try:
                        cur.execute(
                            "SELECT id, version, content, source_qa_range, links "
                            "FROM observation_notes "
                            "WHERE version LIKE 'v%' "
                            "ORDER BY id DESC LIMIT 1"
                        )
                        row = cur.fetchone()
                        if row is not None:
                            note_id, version, full_content, qa_range, links = row
                            # 优先抽种子印 (phase7 _write_observation_note 写入约定)
                            seed = _extract_prev_seed(links)
                            note_content = seed if seed else (full_content or "")
                            # range_end = int8range upper; 失败兜底 0
                            range_end = 0
                            try:
                                if qa_range is not None:
                                    upper_val = qa_range.upper
                                    if upper_val is not None:
                                        range_end = int(upper_val)
                            except Exception:
                                range_end = 0
                            note_section = {
                                "note_id": int(note_id) if note_id is not None else 0,
                                "version": str(version or ""),
                                "content": str(note_content),
                                "range_end": range_end,
                            }
                    except PrefetchDeadlineExceeded:
                        raise
                    except Exception as e:
                        logger.warning("recall_for_new_session: 取最新印失败, 跳过 note: %s",
                                       _safe_err(e)[:120])

                    # ── 2) 动态尾巴 ────────────────────────────────
                    try:
                        cur.execute("SELECT max(id) FROM qa_pairs")
                        _mx = cur.fetchone()
                        latest_id = int(_mx[0]) if _mx and _mx[0] is not None else 0

                        # 用 note.range_end 作 cursor; note 缺失时退 0 (全量)
                        cursor_id = (note_section or {}).get("range_end", 0) or 0
                        uncovered = max(latest_id - cursor_id, 0)
                        # clamp 到 [5, 10]
                        tail_len = max(min(10, max(uncovered, 5)), 5)

                        cur.execute(
                            "SELECT id, session_id, timestamp, question, answer "
                            "FROM qa_pairs "
                            "WHERE id > %s AND answer IS NOT NULL "
                            "  AND session_id NOT LIKE '%%.trajectory%%' "
                            "ORDER BY id DESC LIMIT %s",
                            (cursor_id, tail_len),
                        )
                        desc_rows = cur.fetchall()
                        # 反转成时间升序 (id ASC), 方便 caller 顺序读
                        asc_rows = list(reversed(desc_rows))
                        for r in asc_rows:
                            qa_id, sess_id, ts, q, a = r
                            # ts 可能是 datetime / str, str() 兜底
                            tail_section.append({
                                "qa_id": int(qa_id) if qa_id is not None else 0,
                                "session_id": str(sess_id or ""),
                                "timestamp": str(ts) if ts is not None else "",
                                "question": str(q or ""),
                                "answer": str(a or ""),
                            })
                    except PrefetchDeadlineExceeded:
                        raise
                    except Exception as e:
                        logger.warning("recall_for_new_session: 取尾巴失败, 跳过 tail: %s",
                                       _safe_err(e)[:120])
            finally:
                try:
                    if pg is not None:
                        pg.close()
                except Exception:
                    pass

        # ── 3) 主题卡 (向量匹配召回) ─────────────────────────
        try:
            if tail_section:
                parts = []
                for it in tail_section:
                    parts.append(it.get("question", ""))
                    parts.append(it.get("answer", ""))
                query_text = "\n".join(parts)[:1500]
            else:
                # tail 拿不到时, 用 note.content 作 query
                query_text = (note_section or {}).get("content", "")[:1500]
            if query_text.strip():
                if pool is not None:
                    topics_section = _recall_candidates(query_text, cfg, top_k=top_k, pool=pool)
                else:
                    topics_section = _recall_candidates(query_text, cfg, top_k=top_k)
            else:
                topics_section = []
        except PrefetchDeadlineExceeded:
            raise
        except Exception as e:
            logger.warning("recall_for_new_session: 召回主题失败, 走空 topics: %s",
                           _safe_err(e)[:120])
            topics_section = []

        # ── 3.5) 原文 QA 关键词匹配 (2026-08-09 补 — 用户: 最简单的原文关键字匹配就该兜住) ──
        # 五合一"原文向量召回"维: 观察者处理完所有 QA 后 tail=0, 原文信息全靠这一维兜底。
        # 用 tail/note 里的实体词做 ILIKE 关键词匹配, 命中即返回原文 QA (含时间戳)。
        qa_section = []
        try:
            # 从 tail 提取关键词 (无 tail 用 note content 前段)
            _kw_src = ""
            if tail_section:
                for it in tail_section[:3]:
                    _kw_src += " " + str(it.get("question", ""))
                    _kw_src += " " + str(it.get("answer", ""))
            else:
                _kw_src = (note_section or {}).get("content", "")[:800]
            # 提取英文/中文实体词 (排除停用词和短词)
            import re as _re
            _kw_candidates = _re.findall(r"[A-Za-z][A-Za-z\-']{4,}|[\u4e00-\u9fff]{2,}", _kw_src)
            _stop = {"there", "about", "would", "could", "should", "their", "they", "what",
                     "when", "where", "which", "with", "from", "have", "been", "that",
                     "this", "will", "your", "youre", "doesnt", "dont", "cant", "after",
                     "before", "because", "caroline", "melanie", "remember"}
            _kws = [w for w in _kw_candidates if w.lower() not in _stop][:8]
            if _kws:
                if pool is not None:
                    _lease3 = pool.lease(timeout=5)
                    try:
                        with _lease3.connection.cursor() as cur:
                            _like = " OR ".join(
                                [f"(question ILIKE '%%{_re.escape(k)}%%' OR answer ILIKE '%%{_re.escape(k)}%%')"
                                 for k in _kws[:4]]
                            )
                            cur.execute(
                                f"SELECT id, timestamp, question, answer FROM qa_pairs "
                                f"WHERE ({_like}) AND answer IS NOT NULL "
                                f"AND session_id NOT LIKE '%%.trajectory%%' "
                                f"ORDER BY id DESC LIMIT {int(top_k)}"
                            )
                            for qid, qts, qq, qa_ans in cur.fetchall():
                                qa_section.append({
                                    "qa_id": int(qid),
                                    "timestamp": str(qts) if qts else "",
                                    "question": str(qq or ""),
                                    "answer": str(qa_ans or ""),
                                })
                    finally:
                        _lease3.close()
                else:
                    import psycopg2 as _pg2
                    _qa_conn = _pg2.connect(_build_pg_dsn(), connect_timeout=3)
                    try:
                        with _qa_conn.cursor() as cur:
                            # psycopg2 execute 总会处理 % → ILIKE 的 % 必须写成 %%
                            _like = " OR ".join(
                                [f"(question ILIKE '%%{_re.escape(k)}%%' OR answer ILIKE '%%{_re.escape(k)}%%')"
                                 for k in _kws[:4]]
                            )
                            cur.execute(
                                f"SELECT id, timestamp, question, answer FROM qa_pairs "
                                f"WHERE ({_like}) AND answer IS NOT NULL "
                                f"AND session_id NOT LIKE '%%.trajectory%%' "
                                f"ORDER BY id DESC LIMIT {int(top_k)}"
                            )
                            for qid, qts, qq, qa_ans in cur.fetchall():
                                qa_section.append({
                                    "qa_id": int(qid),
                                    "timestamp": str(qts) if qts else "",
                                    "question": str(qq or ""),
                                    "answer": str(qa_ans or ""),
                                })
                    finally:
                        _qa_conn.close()
        except PrefetchDeadlineExceeded:
            raise
        except Exception as e:
            logger.warning("recall_for_new_session: 原文 QA 关键词匹配失败(不阻塞): %s",
                           _safe_err(e)[:120])
            qa_section = []

        elapsed_ms = round((_time.monotonic() - _t0) * 1000, 3)

        # 全挂判定: 四段都空
        if note_section is None and not tail_section and not topics_section and not qa_section:
            logger.warning("recall_for_new_session: 全空 (note=%s, tail=%d, topics=%d)",
                           note_section, len(tail_section), len(topics_section))
            return None

        logger.info(
            "recall_for_new_session: note=%s, tail=%d, topics=%d, elapsed_ms=%.1f",
            (note_section or {}).get("version", "-") if note_section else "-",
            len(tail_section),
            len(topics_section),
            elapsed_ms,
        )

        return {
            "note": note_section,
            "tail": tail_section,
            "topics": topics_section,
            "qa_hits": qa_section,  # 2026-08-09: 原文 QA 关键词匹配结果
            "fence": _FENCE_MARKER,
            "elapsed_ms": float(elapsed_ms),
        }
    except PrefetchDeadlineExceeded:
        raise
    except Exception as e:
        # 任何意料之外的顶层异常 — 兜底 None, 不抛出
        logger.warning("recall_for_new_session: 顶层异常, 返 None: %s",
                       _safe_err(e)[:120])
        try:
            if pg is not None:
                pg.close()
        except Exception:
            pass
        return None

