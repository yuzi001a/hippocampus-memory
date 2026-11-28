# -*- coding: utf-8 -*-
"""Offline G0 vs G1 + A->B Stale-Memory Ablation Benchmark.

Measures real production ``prefetch_to_context_block -> RecallV2Engine -> recall_pool``
behavior across three explicit layer profiles without requiring PostgreSQL or external APIs:

* ``G0_qa_only``: ``include_topic=False, include_notes=False, include_yin=False``
* ``G1_obs``:     ``include_topic=False, include_notes=True,  include_yin=False``
* ``G1_full``:    ``include_topic=True,  include_notes=True,  include_yin=True``
"""
from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

# Make src/v3-core/src importable when run directly as a module from anywhere.
_HERE = Path(__file__).resolve()
_PKG_ROOT = _HERE.parents[1]
_SRC_ROOT = _PKG_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")
_ZH_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
EMBED_DIM = 64


def embed_text(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic bilingual bag-of-ngrams hashed unit vector (zero network I/O)."""
    vec = [0.0] * dim
    raw = str(text or "").strip().lower()
    if not raw:
        return vec

    features: list[tuple[str, float]] = []
    for tok in _ASCII_TOKEN_RE.findall(raw):
        features.append((f"w:{tok}", 1.5))
    for run in _ZH_RUN_RE.findall(raw):
        if len(run) == 1:
            features.append((f"c:{run}", 0.8))
        else:
            for ch in run:
                features.append((f"c:{ch}", 0.4))
            for i in range(len(run) - 1):
                features.append((f"bg:{run[i:i+2]}", 2.0))

    if not features:
        return vec

    for feat, weight in features:
        digest = hashlib.sha1(feat.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:2], "big") % dim
        sign = 1.0 if (digest[2] & 1) == 0 else -1.0
        vec[bucket] += sign * weight

    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0.0:
        return vec
    return [round(v / norm, 6) for v in vec]


def _parse_vec(raw: Any) -> list[float]:
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    s = str(raw or "").strip()
    if s.startswith("[") and s.endswith("]"):
        return [float(x) for x in json.loads(s)]
    return []


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float(dot / (na * nb))


def _ilike_match(text: str, pattern: str) -> bool:
    needle = str(pattern or "").strip("%").lower()
    if not needle:
        return False
    return needle in str(text or "").lower()


class _AblationFakePool:
    """Minimal duck-typed pool for ``TopicRecall.lease()`` (not a ``PgPool`` subclass)."""

    def __init__(self, store: "_AblationFakePg") -> None:
        self._store = store

    def lease(self, timeout: float | None = 5.0):
        conn = self._store._connect()

        class _Lease:
            def __init__(self, c) -> None:
                self.connection = c

            def close(self) -> None:
                pass

        return _Lease(conn)


class _AblationFakeCursor:
    def __init__(self, store: "_AblationFakePg") -> None:
        self._store = store
        self._rows: list[tuple] = []

    def __enter__(self) -> "_AblationFakeCursor":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        q = " ".join(str(sql or "").split())
        q_low = q.lower()
        p = list(params) if isinstance(params, (list, tuple)) else ([] if params is None else [params])

        if "to_regclass" in q_low:
            self._rows = [(None,)]
            return

        if q_low.startswith("select exists"):
            if "from yin_paragraphs" in q_low:
                self._rows = [(bool(self._store.yin_paragraphs),)]
            elif "from qa_pairs" in q_low:
                self._rows = [(bool(self._store.qa_pairs),)]
            elif "from topics" in q_low:
                self._rows = [(bool(self._store.topics),)]
            elif "from observation_notes" in q_low:
                self._rows = [(bool(self._store.observation_notes),)]
            else:
                self._rows = [(False,)]
            return

        if "select count(*)" in q_low and "from qa_pairs" in q_low:
            qas = self._store.qa_pairs
            max_id = max((int(r["id"]) for r in qas), default=0)
            self._rows = [(len(qas), max_id)]
            return

        if "select count(*)" in q_low and "from topics" in q_low:
            active_emb = sum(
                1 for t in self._store.topics
                if t.get("status", "active") == "active" and t.get("embedding") is not None
            )
            self._rows = [(active_emb,)]
            return

        if "select p, count(*) from qa_pairs" in q_low and "unnest" in q_low:
            patterns = list(p[0] or []) if p else []
            out = []
            for pat in patterns:
                cnt = sum(
                    1
                    for r in self._store.qa_pairs
                    if _ilike_match(r.get("question", ""), pat)
                    or _ilike_match(r.get("answer", ""), pat)
                )
                out.append((pat, cnt))
            self._rows = out
            return

        # PG keyword lookup for qa_pairs
        if "from qa_pairs" in q_low and "ilike" in q_low and "embedding <=>" not in q_low:
            term_pat = p[0] if p else ""
            lim = int(p[-1]) if p and isinstance(p[-1], int) else 20
            matched = []
            for r in self._store.qa_pairs:
                if r.get("answer") is None:
                    continue
                sid = str(r.get("session_id") or "")
                if ".trajectory" in sid:
                    continue
                if _ilike_match(r.get("question", ""), term_pat) or _ilike_match(r.get("answer", ""), term_pat):
                    matched.append((int(r["id"]), r.get("timestamp"), r.get("question", ""), r.get("answer", "")))
            matched.sort(key=lambda row: row[0])
            self._rows = matched[: max(lim, 0)]
            return

        # PG vector lookup for qa_pairs
        if "from qa_pairs" in q_low and "embedding <=>" in q_low:
            q_vec = _parse_vec(p[0] if p else [])
            lim = int(p[-1]) if p and isinstance(p[-1], int) else 20
            scored = []
            for r in self._store.qa_pairs:
                emb = r.get("embedding")
                if emb is None or r.get("answer") is None:
                    continue
                if ".trajectory" in str(r.get("session_id") or ""):
                    continue
                cos = _cosine(q_vec, emb)
                scored.append((cos, int(r["id"]), r.get("timestamp"), r.get("question", ""), r.get("answer", "")))
            scored.sort(key=lambda item: item[0], reverse=True)
            self._rows = [(qid, ts, qq, qa, cos) for cos, qid, ts, qq, qa in scored[: max(lim, 0)]]
            return

        # TopicRecall PG load: SELECT topic_id, title, COALESCE(summary, ''), COALESCE(body, ''), embedding FROM topics
        if "from topics" in q_low and "embedding" in q_low and "ilike" not in q_low:
            self._rows = [
                (
                    t["topic_id"],
                    t.get("title", ""),
                    t.get("summary", ""),
                    t.get("body", ""),
                    t.get("embedding"),
                )
                for t in self._store.topics
                if t.get("status", "active") == "active" and t.get("embedding") is not None
            ]
            return

        # Topics keyword lookup: SELECT topic_id, title, summary, body FROM topics WHERE status='active' AND (...) LIMIT %s
        if "from topics" in q_low and "ilike" in q_low:
            lim = int(p[-1]) if p and isinstance(p[-1], int) else 20
            pats = [str(x) for x in p[:-1]]
            matched_topics = []
            for t in self._store.topics:
                if t.get("status", "active") != "active":
                    continue
                kws = " ".join(t.get("keywords") or [])
                haystack = f"{t.get('title', '')}\n{t.get('summary', '')}\n{t.get('body', '')}\n{kws}".lower()
                if any(str(pat).strip("%").lower() in haystack for pat in pats if str(pat).strip("%")):
                    matched_topics.append((t["topic_id"], t.get("title", ""), t.get("summary", ""), t.get("body", "")))
            self._rows = matched_topics[: max(lim, 0)]
            return

        # observation_notes vector lookup
        if "from observation_notes" in q_low and "embedding <=>" in q_low:
            q_vec = _parse_vec(p[0] if p else [])
            lim = int(p[-1]) if p and isinstance(p[-1], int) else 10
            by_day: dict[str, tuple[float, int, str, str, datetime, Any]] = {}
            for n in self._store.observation_notes:
                emb = n.get("embedding")
                if emb is None:
                    continue
                cos = _cosine(q_vec, emb)
                dt = n.get("created_at")
                day_key = dt.strftime("%Y-%m-%d") if isinstance(dt, datetime) else str(dt or "")[:10]
                cand = (
                    cos,
                    int(n["id"]),
                    str(n.get("version") or ""),
                    str(n.get("content") or ""),
                    dt,
                    n.get("source_qa_range"),
                )
                prev = by_day.get(day_key)
                if prev is None or cand[0] > prev[0]:
                    by_day[day_key] = cand
            ordered = sorted(by_day.values(), key=lambda x: (str(x[4] or ""), x[0]), reverse=True)
            self._rows = [
                (nid, ver, content, cos, dt, qrange)
                for cos, nid, ver, content, dt, qrange in ordered[: max(lim, 0)]
            ]
            return

        # yin_paragraphs vector/keyword lookup
        if "from yin_paragraphs" in q_low and "embedding <=>" in q_low:
            q_vec = _parse_vec(p[0] if p else [])
            lim = int(p[-1]) if p and isinstance(p[-1], int) else 3
            scored_y = []
            for y in self._store.yin_paragraphs:
                emb = y.get("embedding")
                if emb is None:
                    continue
                scored_y.append((_cosine(q_vec, emb), y["yin_version"], y["section"], y["content"]))
            scored_y.sort(key=lambda item: item[0], reverse=True)
            self._rows = [(yv, sec, cont, cos) for cos, yv, sec, cont in scored_y[: max(lim, 0)]]
            return

        if "from yin_paragraphs" in q_low and "ilike" in q_low:
            pat = p[0] if p else ""
            lim = int(p[-1]) if p and isinstance(p[-1], int) else 3
            rows_y = [
                (y["yin_version"], y["section"], y["content"])
                for y in self._store.yin_paragraphs
                if _ilike_match(y.get("content", ""), pat)
            ]
            self._rows = rows_y[: max(lim, 0)]
            return

        self._rows = []

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _AblationFakeConn:
    def __init__(self, store: "_AblationFakePg") -> None:
        self._store = store

    def cursor(self) -> _AblationFakeCursor:
        return _AblationFakeCursor(self._store)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class _AblationFakePg:
    """In-memory deterministic fake PG supporting ``recall_pool`` + ``TopicRecall`` + ``search_yin``."""

    def __init__(
        self,
        *,
        qa_pairs: list[dict[str, Any]],
        topics: list[dict[str, Any]],
        observation_notes: list[dict[str, Any]],
        yin_paragraphs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.qa_pairs = qa_pairs
        self.topics = topics
        self.observation_notes = observation_notes
        self.yin_paragraphs = yin_paragraphs or []
        self.pool = _AblationFakePool(self)

    def is_connected(self) -> bool:
        return True

    def _connect(self) -> _AblationFakeConn:
        return _AblationFakeConn(self)

    @contextlib.contextmanager
    def lease(self, timeout: float | None = 5.0):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()


@dataclass(frozen=True)
class AblationScenario:
    scenario_id: str
    category: str
    description: str
    query: str
    qa_pairs: list[dict[str, Any]]
    topics: list[dict[str, Any]]
    observation_notes: list[dict[str, Any]]
    yin_paragraphs: list[dict[str, Any]] = field(default_factory=list)
    must_include: tuple[str, ...] = ()
    stale_tokens: tuple[str, ...] = ()
    expect_empty: bool = False


PROFILE_ORDER: tuple[str, ...] = (
    "G0_qa_only",
    "G1_obs",
    "G1_full",
    "G2_freshness_only",
    "G2_structured",
)

PROFILES: dict[str, dict[str, Any]] = {
    "G0_qa_only": {
        "include_topic": False,
        "include_notes": False,
        "include_yin": False,
        "topic_v2_mode": False,
        "consolidate_v2": False,
    },
    "G1_obs": {
        "include_topic": False,
        "include_notes": True,
        "include_yin": False,
        "topic_v2_mode": False,
        "consolidate_v2": False,
    },
    "G1_full": {
        "include_topic": True,
        "include_notes": True,
        "include_yin": True,
        "topic_v2_mode": False,
        "consolidate_v2": False,
    },
    "G2_freshness_only": {
        "include_topic": True,
        "include_notes": True,
        "include_yin": True,
        "topic_v2_mode": True,
        "consolidate_v2": False,
    },
    "G2_structured": {
        "include_topic": True,
        "include_notes": True,
        "include_yin": True,
        "topic_v2_mode": True,
        "consolidate_v2": True,
    },
}


def _make_qa(qid: int, days_ago: int, question: str, answer: str, session_id: str = "s1") -> dict[str, Any]:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    ts = now - timedelta(days=days_ago)
    return {
        "id": qid,
        "timestamp": ts,
        "question": question,
        "answer": answer,
        "session_id": session_id,
        "embedding": embed_text(f"{question}\n{answer}"),
    }


def _make_topic(
    tid: str,
    title: str,
    summary: str,
    body: str,
    keywords: Sequence[str],
) -> dict[str, Any]:
    return {
        "topic_id": tid,
        "title": title,
        "summary": summary,
        "body": body,
        "keywords": list(keywords),
        "status": "active",
        "embedding": embed_text(f"{title}\n{summary}\n{body}\n{' '.join(keywords)}"),
    }


def _make_note(
    nid: int,
    days_ago: int,
    version: str,
    content: str,
    *,
    source_qa_range: str | None = None,
) -> dict[str, Any]:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    dt = now - timedelta(days=days_ago)
    return {
        "id": nid,
        "version": version,
        "content": content,
        "created_at": dt,
        "source_qa_range": source_qa_range,
        "embedding": embed_text(content),
    }


def _make_yin(section: str, content: str) -> dict[str, Any]:
    return {
        "yin_version": "y_20261119T0400.md",
        "section": section,
        "content": content,
        "embedding": embed_text(f"{section}\n{content}"),
    }


def build_scenarios() -> list[AblationScenario]:
    """Build deterministic 18-case benchmark across 5 diagnostic categories."""
    scenarios: list[AblationScenario] = []

    # ── Category 1: A -> B evolution (6 cases) ──
    evolution_specs = [
        (
            "ab_01_db_backend",
            "核心存储后端目前决定使用哪个数据库方案？",
            "SQLite-Embedded",
            "PostgreSQL-pgvector",
            "核心存储数据库后端选型",
            "核心存储后端采用 SQLite-Embedded 本地单文件模式，早期讨论记录完整，暂不引入外部服务。",
            "本地测试发现 SQLite-Embedded 在多端并发写入时频繁锁库",
        ),
        (
            "ab_02_queue_broker",
            "异步任务总线现在采用什么消息队列？",
            "Redis-PubSub-v1",
            "NATS-JetStream-v2",
            "异步任务总线消息队列选型",
            "异步事件与任务分发总线选定 Redis-PubSub-v1 方案，结构简单，适合快速广播事件。",
            "线上观察到 Redis-PubSub-v1 断线重连后不持久化积压消息",
        ),
        (
            "ab_03_auth_scheme",
            "本地服务鉴权机制目前用哪种方式？",
            "Legacy-Static-Cookie",
            "Per-Host-Bearer-Token",
            "本地服务接口鉴权机制",
            "本地 HTTP 服务继续沿用 Legacy-Static-Cookie 进行状态校验，配置成本最低。",
            "浏览器跨标签共享时 Legacy-Static-Cookie 触发严格 CSRF 误拦",
        ),
        (
            "ab_04_realtime_sync",
            "实时状态推送通道当前采用哪种通信协议？",
            "Short-Polling-5s",
            "Server-Sent-Events",
            "实时状态推送通信协议",
            "前端状态同步使用 Short-Polling-5s 定时轮询接口，兼容性最好，无需长连接管理。",
            "多面板并行下 Short-Polling-5s 产生大量无效空轮询请求",
        ),
        (
            "ab_05_worker_pool",
            "后台向量补全执行器目前使用什么并发模型？",
            "Single-Thread-Loop",
            "Bounded-Async-Pool",
            "后台向量补全执行器并发模型",
            "后台向量任务由 Single-Thread-Loop 串行循环执行，防止上游并发超限。",
            "Single-Thread-Loop 导致大批量导入排队阻塞超过 40 分钟",
        ),
        (
            "ab_06_deploy_pack",
            "插件打包分发格式当前确定采用哪种方式？",
            "Raw-Zip-Manual-Copy",
            "Standard-Wheel-Bundle",
            "插件打包与分发方式",
            "插件分发现阶段采用 Raw-Zip-Manual-Copy 手动解压部署到站点目录，步骤透明直观。",
            "Raw-Zip-Manual-Copy 在不同操作系统上经常漏拷隐藏依赖文件",
        ),
    ]
    for sid, query, token_a, token_b, title, stale_desc, issue_desc in evolution_specs:
        qas = [
            _make_qa(1, 20, f"关于{title}，一开始决定用什么方案？", f"初始确定方案为 {token_a}，先按该架构落地。", "s-old"),
            _make_qa(2, 10, f"{token_a} 上线后遇到了什么问题？", f"{issue_desc}，已无法满足当前需求。", "s-mid"),
            _make_qa(3, 1, f"经过评估，{title}最新确认改用什么方案？", f"正式废弃 {token_a}，改为 {token_b} 作为唯一当前方案。", "s-new"),
        ]
        topic = _make_topic(
            f"t_{sid}",
            title,
            stale_desc,
            f"项目早期经过讨论后确定：## 方案说明\n{stale_desc}\n后期曾出现补充讨论但未淘汰旧描述，仍保留 {token_a} 主叙事。",
            [title, token_a],
        )
        note = _make_note(
            1,
            2,
            "v_obs_stale",
            f"近期观察总结（{title}）：延续此前结论，目前重点维护 {token_a}，相关实践继续围绕 {token_a} 展开。",
            source_qa_range="[1,3)",
        )
        yin = _make_yin(title, f"工作基线中 {title} 长期沿用 {token_a}。")
        scenarios.append(
            AblationScenario(
                scenario_id=sid,
                category="A_TO_B_EVOLUTION",
                description=f"{title}: evolved {token_a} -> {token_b}",
                query=query,
                qa_pairs=qas,
                topics=[topic],
                observation_notes=[note],
                yin_paragraphs=[yin],
                must_include=(token_b,),
                stale_tokens=(token_a,),
            )
        )

    # ── Category 2: Historical Trace (3 cases) ──
    hist_specs = [
        (
            "hist_01_why_drop_sqlite",
            "以前为什么放弃了 Legacy-FlatFile-V0 存储格式？",
            "Legacy-FlatFile-V0",
            "不支持原子事务与并发索引",
            "旧存储格式 Legacy-FlatFile-V0 复盘",
        ),
        (
            "hist_02_why_drop_v1_router",
            "早期为什么淘汰了 Regex-Router-99 规则路由？",
            "Regex-Router-99",
            "正则冲突率高且无法处理多语言分词",
            "旧规则路由 Regex-Router-99 复盘",
        ),
        (
            "hist_03_why_drop_timer",
            "之前为什么移除了 Blind-Heartbeat-3s 定时器？",
            "Blind-Heartbeat-3s",
            "无活动时反复唤醒磁盘造成显著电量损耗",
            "旧心跳定时器 Blind-Heartbeat-3s 复盘",
        ),
    ]
    for sid, query, token_a, reason, title in hist_specs:
        qas = [
            _make_qa(1, 25, f"{token_a} 当初为什么要下线？", f"因为 {token_a} {reason}，因此在第 4 版彻底下线。", "s-hist"),
            _make_qa(2, 3, "当前系统已经换成新版模块了吗？", "是的，现网已全部切换到 v4 新模块运行。", "s-now"),
        ]
        topic = _make_topic(
            f"t_{sid}",
            title,
            f"当前稳定运行新版架构。",
            f"系统现已全面运行在全新架构上，历史遗留已经清理完毕。",
            ["架构升级", "v4"],
        )
        note = _make_note(1, 3, "v_hist", "近期观察到新版模块运行稳定，未保留早期实现原因细节。")
        scenarios.append(
            AblationScenario(
                scenario_id=sid,
                category="HISTORICAL_TRACE",
                description=f"Trace historical failure reason for {token_a}",
                query=query,
                qa_pairs=qas,
                topics=[topic],
                observation_notes=[note],
                must_include=(token_a, reason),
            )
        )

    # ── Category 3: Cross-session Synthesis where Topics/Notes legitimately bridge vocabulary (4 cases) ──
    # Here QA uses fragmented project codenames; Topics/Notes attach the high-level search keywords + answer token.
    synth_specs = [
        (
            "syn_01_atlas_latency",
            "跨端同步管线 Atlas 之前的超时根因是什么？",
            "DNS-Lookup-Stall-Fix",
            "子模块 M-7 握手阶段发生命名解析卡顿。",
            "Atlas 跨端同步管线超时问题复盘：跨多天排查子模块 M-7 后确认，超时根因已定位并修复为 DNS-Lookup-Stall-Fix。",
            ["Atlas", "跨端同步", "超时", "根因"],
        ),
        (
            "syn_02_orion_budget",
            "Orion 工作流里的上下文注入预算上限是多少字？",
            "BudgetCap-6400-Chars",
            "经过三轮调整，最终窗口参数已调定并锁定。",
            "Orion 工作流上下文注入规范总结：最终预算上限确定为 BudgetCap-6400-Chars，防止过长挤占推理窗口。",
            ["Orion", "上下文注入", "预算上限"],
        ),
        (
            "syn_03_zephyr_fallback",
            "Zephyr 网关遇到限流 429 时的降级策略是什么？",
            "RetryAfter-Backoff-3x",
            "遇到上游返回繁忙状态码时按既定次数退避。",
            "Zephyr 网关限流 429 降级策略整理：统一执行 RetryAfter-Backoff-3x 退避机制，避免重试风暴。",
            ["Zephyr", "限流", "429", "降级策略"],
        ),
        (
            "syn_04_kestrel_isolation",
            "Kestrel 沙盒测试时如何防止写入生产目录？",
            "FailClosed-Sentinel-Guard",
            "测试前校验环境变量与路径前缀一致性。",
            "Kestrel 沙盒测试隔离规范汇总：通过 FailClosed-Sentinel-Guard 门禁确保测试失败即退出而不回落生产目录。",
            ["Kestrel", "沙盒", "生产目录", "隔离"],
        ),
    ]
    for sid, query, answer_tok, vague_qa, synth_text, kws in synth_specs:
        qas = [
            _make_qa(1, 12, "子模块 M-7 这段状态日志什么含义？", f"{vague_qa} 具体代号见后台追踪条目。", "s-a"),
            _make_qa(2, 6, "后来那次验证把问题解决了吗？", f"已确认解决，核心开关为 {answer_tok}。", "s-b"),
        ]
        topic = _make_topic(f"t_{sid}", " ".join(kws[:2]), synth_text, synth_text, kws)
        note = _make_note(1, 4, "v_syn", synth_text)
        scenarios.append(
            AblationScenario(
                scenario_id=sid,
                category="CROSS_SESSION_SYNTHESIS",
                description=f"Synthesized topic/note bridges vocabulary for {answer_tok}",
                query=query,
                qa_pairs=qas,
                topics=[topic],
                observation_notes=[note],
                must_include=(answer_tok,),
            )
        )

    # ── Category 4: Correction Residual (2 cases) ──
    corr_specs = [
        (
            "corr_01_reply_style",
            "用户对代码审查回复格式现在有什么要求？",
            "Verbose-Essay-Format",
            "Bullet-Diff-Only",
            "用户代码审查回复习惯",
        ),
        (
            "corr_02_test_runner",
            "运行核心单测时现在默认允许联网测试吗？",
            "Allow-Live-Network",
            "Offline-Mock-Default",
            "核心单测网络策略",
        ),
    ]
    for sid, query, old_rule, new_rule, title in corr_specs:
        qas = [
            _make_qa(1, 18, f"关于{title}最初怎么约定？", f"早期习惯采用 {old_rule}。", "s-c1"),
            _make_qa(2, 1, f"我纠正一下{title}：以后一律改为什么？", f"明确纠正：禁止 {old_rule}，必须严格遵守 {new_rule}。", "s-c2"),
        ]
        topic = _make_topic(
            f"t_{sid}",
            title,
            f"用户长期偏好为 {old_rule}。",
            f"习惯档案记录：{title}一直采用 {old_rule}，尚未感知新纠正。",
            [title, old_rule],
        )
        note = _make_note(
            1,
            2,
            "v_corr",
            f"观察笔记记录：用户在{title}方面倾向 {old_rule}。",
            source_qa_range="[1,2)",
        )
        yin = _make_yin(title, f"长期互动基线：{title}保持 {old_rule}。")
        scenarios.append(
            AblationScenario(
                scenario_id=sid,
                category="CORRECTION_RESIDUAL",
                description=f"User corrected {old_rule} -> {new_rule}; derived stores still hold {old_rule}",
                query=query,
                qa_pairs=qas,
                topics=[topic],
                observation_notes=[note],
                yin_paragraphs=[yin],
                must_include=(new_rule,),
                stale_tokens=(old_rule,),
            )
        )

    # ── Category 5: False Recall Abstention (3 cases) ──
    abstention_queries = [
        ("abs_01_quicksort", "Write a standard Python quicksort partition function."),
        ("abs_02_flexbox", "How do I center a child element with CSS Flexbox?"),
        ("abs_03_regex_date", "Give me an ISO-8601 date validation regex pattern."),
    ]
    shared_unrelated_qas = [
        _make_qa(1, 5, "周末搬家公司电话是多少？", "已预约顺通搬家，周六上午九点到达小区东门。", "s-life"),
        _make_qa(2, 3, "报销发票抬头写的公司名称对吗？", "抬头全称为星图云科技有限公司，税号核对无误。", "s-work"),
    ]
    shared_unrelated_topic = _make_topic(
        "t_unrelated",
        "周末搬家与行政报销事项",
        "包含搬家预约时间与公司开票信息。",
        "周六上午九点搬家，报销抬头星图云科技有限公司。",
        ["搬家", "发票", "报销"],
    )
    shared_unrelated_note = _make_note(1, 2, "v_life", "近期观察到用户在处理搬家物流与行政报销流程。")
    for sid, query in abstention_queries:
        scenarios.append(
            AblationScenario(
                scenario_id=sid,
                category="FALSE_RECALL_ABSTENTION",
                description="Completely unrelated coding question should abstain from injecting personal memory",
                query=query,
                qa_pairs=shared_unrelated_qas,
                topics=[shared_unrelated_topic],
                observation_notes=[shared_unrelated_note],
                expect_empty=True,
            )
        )

    return scenarios


def _hit_contains_any(hit_text: str, tokens: Sequence[str]) -> bool:
    low = hit_text.lower()
    return any(tok.lower() in low for tok in tokens if tok)


def _extract_trace_summary(engine_trace: Any) -> dict[str, Any]:
    if engine_trace is None:
        return {
            "trace_id": "",
            "candidate_ids": [],
            "selected_ids": [],
            "injected_ids": [],
        }
    from eval.locomo_recall_v2.adapter import resolve_trace_source_ids

    try:
        engine_trace.finish()
    except Exception:
        pass
    snaps = getattr(engine_trace, "candidate_snapshots", {}) or {}
    cand_sids = resolve_trace_source_ids(engine_trace, list(snaps.keys()))
    selected_cids = list(getattr(engine_trace, "final_selected_ids", []) or [])
    selected_sids = resolve_trace_source_ids(engine_trace, selected_cids)
    injected_sids: list[str] = []
    seen_inj: set[str] = set()
    # Preserve real prompt-ranking order from final_selected_ids first (matching
    # context_block top-to-bottom order), then any remaining injected snapshots.
    selected_set = set(selected_cids)
    ordered_cids = list(selected_cids) + [cid for cid in snaps.keys() if cid not in selected_set]
    for cid in ordered_cids:
        snap = snaps.get(cid)
        if snap is not None and bool(getattr(snap, "injected", False)):
            sid = str(getattr(snap, "source_id", "") or cid)
            if sid and sid not in seen_inj:
                seen_inj.add(sid)
                injected_sids.append(sid)
    return {
        "trace_id": str(getattr(engine_trace, "trace_id", "") or ""),
        "candidate_ids": cand_sids,
        "selected_ids": selected_sids,
        "injected_ids": injected_sids,
    }


def prefetch_with_trace(
    query: str,
    *,
    limit: int = 5,
    config: Mapping[str, Any],
    pg: Any,
    q_emb: list[float],
    max_chars: int = 4000,
) -> tuple[str, dict[str, Any]]:
    """Call real ``prefetch_to_context_block`` and capture the engine's ``RecallTrace``."""
    import v3core.prefetch as _prefetch_module

    observed_traces: list[Any] = []
    original_prefetch = _prefetch_module.prefetch

    def _observe_prefetch(*args: Any, **kwargs: Any) -> Any:
        result = original_prefetch(*args, **kwargs)
        holder = kwargs.get("trace_out")
        if isinstance(holder, list) and holder:
            observed_traces.append(holder[0])
        return result

    _prefetch_module.prefetch = _observe_prefetch
    try:
        context_block = _prefetch_module.prefetch_to_context_block(
            query,
            limit=limit,
            config=dict(config),
            card_index={},
            pg=pg,
            q_emb=q_emb,
            is_new_session=False,
            pg_was_connected=True,
            core=None,
            max_chars=max_chars,
        )
    finally:
        _prefetch_module.prefetch = original_prefetch

    engine_trace = observed_traces[0] if observed_traces else None
    return str(context_block or ""), _extract_trace_summary(engine_trace)


def _evaluate_case(
    scenario: AblationScenario,
    profile_name: str,
    context_block: str,
    trace_summary: Mapping[str, Any],
    latency_ms: float,
    active_topics: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    from v3core.topic_v2 import strip_topic_v2_header

    cand_ids = list(trace_summary.get("candidate_ids") or [])
    selected_ids = list(trace_summary.get("selected_ids") or [])
    injected_ids = list(trace_summary.get("injected_ids") or [])

    if scenario.expect_empty:
        false_recall = bool(injected_ids or selected_ids)
        return {
            "scenario_id": scenario.scenario_id,
            "category": scenario.category,
            "profile": profile_name,
            "passed": not false_recall,
            "current_in_context": True,
            "stale_in_context": false_recall,
            "stale_outranks_current": False,
            "false_recall": false_recall,
            "selected_ids": selected_ids,
            "injected_ids": injected_ids,
            "context_chars": len(context_block),
            "latency_ms": round(latency_ms, 3),
            "trace_id": trace_summary.get("trace_id", ""),
        }

    low_ctx = context_block.lower()
    current_in_context = all(tok.lower() in low_ctx for tok in scenario.must_include)
    stale_in_context = any(tok.lower() in low_ctx for tok in scenario.stale_tokens) if scenario.stale_tokens else False

    # Inspect actual prompt injection ranking order (fallback to selected_ids)
    # for stale-only artifact outranking current source.
    stale_outranks_current = False
    ranking_ids = injected_ids if injected_ids else selected_ids
    topics_for_map = active_topics if active_topics is not None else scenario.topics
    if scenario.stale_tokens and ranking_ids:
        # Map source_id -> raw stored text in scenario to determine whether that source carries stale-only vs current
        source_text: dict[str, str] = {}
        for q in scenario.qa_pairs:
            source_text[f"qa_{q['id']}"] = f"{q.get('question', '')}\n{q.get('answer', '')}"
        for t in topics_for_map:
            clean_body = strip_topic_v2_header(str(t.get("body", "") or ""))
            source_text[f"topic_{t['topic_id']}"] = f"{t.get('title', '')}\n{t.get('summary', '')}\n{clean_body}"
        for n in scenario.observation_notes:
            note_txt = str(n.get("content") or "")
            source_text[f"obs_{n['id']}"] = note_txt
            source_text[f"note:{n['version']}"] = note_txt
        for y in scenario.yin_paragraphs:
            source_text[f"yin:{y['yin_version']}#{y['section']}"] = str(y.get("content") or "")

        first_current_rank: int | None = None
        first_stale_only_rank: int | None = None
        for rank_idx, sid in enumerate(ranking_ids):
            txt = source_text.get(sid, "")
            has_cur = _hit_contains_any(txt, scenario.must_include)
            has_stale = _hit_contains_any(txt, scenario.stale_tokens)
            if has_cur and first_current_rank is None:
                first_current_rank = rank_idx
            if has_stale and not has_cur and first_stale_only_rank is None:
                first_stale_only_rank = rank_idx
        if first_stale_only_rank is not None:
            if first_current_rank is None or first_stale_only_rank < first_current_rank:
                stale_outranks_current = True

    if scenario.category == "HISTORICAL_TRACE":
        source_fidelity = any(sid.startswith("qa_") for sid in injected_ids)
        passed = current_in_context and source_fidelity
    elif scenario.stale_tokens:
        passed = current_in_context and (not stale_outranks_current)
    else:
        passed = current_in_context

    return {
        "scenario_id": scenario.scenario_id,
        "category": scenario.category,
        "profile": profile_name,
        "passed": passed,
        "current_in_context": current_in_context,
        "stale_in_context": stale_in_context,
        "stale_outranks_current": stale_outranks_current,
        "false_recall": False,
        "selected_ids": selected_ids,
        "injected_ids": injected_ids,
        "candidate_count": len(cand_ids),
        "context_chars": len(context_block),
        "latency_ms": round(latency_ms, 3),
        "trace_id": trace_summary.get("trace_id", ""),
    }


def _aggregate_profile(profile_name: str, case_results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(case_results)
    passed = sum(1 for r in case_results if r["passed"])
    evo = [r for r in case_results if r["category"] == "A_TO_B_EVOLUTION"]
    corr = [r for r in case_results if r["category"] == "CORRECTION_RESIDUAL"]
    synth = [r for r in case_results if r["category"] == "CROSS_SESSION_SYNTHESIS"]
    hist = [r for r in case_results if r["category"] == "HISTORICAL_TRACE"]
    absn = [r for r in case_results if r["category"] == "FALSE_RECALL_ABSTENTION"]

    by_category: dict[str, dict[str, Any]] = {}
    for cat in (
        "A_TO_B_EVOLUTION",
        "HISTORICAL_TRACE",
        "CROSS_SESSION_SYNTHESIS",
        "CORRECTION_RESIDUAL",
        "FALSE_RECALL_ABSTENTION",
    ):
        subset = [r for r in case_results if r["category"] == cat]
        cat_pass = sum(1 for r in subset if r["passed"])
        by_category[cat] = {
            "count": len(subset),
            "passed": cat_pass,
            "pass_rate": round(cat_pass / len(subset), 4) if subset else 0.0,
        }

    stale_outrank_cases = [r for r in (evo + corr) if r["stale_outranks_current"]]
    stale_in_ctx_cases = [r for r in (evo + corr) if r["stale_in_context"]]
    false_recall_cases = [r for r in absn if r["false_recall"]]
    avg_chars = round(sum(r["context_chars"] for r in case_results) / total, 1) if total else 0.0
    avg_latency = round(sum(r["latency_ms"] for r in case_results) / total, 2) if total else 0.0
    trace_cov = round(sum(1 for r in case_results if r.get("trace_id")) / total, 4) if total else 0.0

    return {
        "profile": profile_name,
        "scenario_count": total,
        "pass_count": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "trace_coverage": trace_cov,
        "evolution_pass_rate": by_category["A_TO_B_EVOLUTION"]["pass_rate"],
        "synthesis_pass_rate": by_category["CROSS_SESSION_SYNTHESIS"]["pass_rate"],
        "history_pass_rate": by_category["HISTORICAL_TRACE"]["pass_rate"],
        "correction_pass_rate": by_category["CORRECTION_RESIDUAL"]["pass_rate"],
        "abstention_pass_rate": by_category["FALSE_RECALL_ABSTENTION"]["pass_rate"],
        "stale_outranks_current_rate": round(len(stale_outrank_cases) / len(evo + corr), 4) if (evo + corr) else 0.0,
        "stale_in_context_rate": round(len(stale_in_ctx_cases) / len(evo + corr), 4) if (evo + corr) else 0.0,
        "false_recall_rate": round(len(false_recall_cases) / len(absn), 4) if absn else 0.0,
        "avg_context_chars": avg_chars,
        "avg_latency_ms": avg_latency,
        "by_category": by_category,
    }


def _render_markdown_report(summary: Mapping[str, Any]) -> str:
    profiles = summary["profiles"]
    lines = [
        "# Topic / Observer Offline Ablation Benchmark (`G0` / `G1` vs `G2`)",
        "",
        f"- Generated UTC: `{summary['timestamp_utc']}`",
        f"- Scenarios: `{summary['scenario_count']}` across 5 categories",
        "",
        "| Profile | Overall Pass | A->B Evolution | Cross-Session Synth | Correction Residual | Historical Trace | Stale Outranks Current | Stale In Context | False Recall | Avg Context Chars |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for pname in PROFILE_ORDER:
        m = profiles[pname]
        lines.append(
            f"| `{pname}` | {m['pass_count']}/{m['scenario_count']} ({m['pass_rate']:.1%}) "
            f"| {m['evolution_pass_rate']:.1%} "
            f"| {m['synthesis_pass_rate']:.1%} "
            f"| {m['correction_pass_rate']:.1%} "
            f"| {m['history_pass_rate']:.1%} "
            f"| {m['stale_outranks_current_rate']:.1%} "
            f"| {m['stale_in_context_rate']:.1%} "
            f"| {m['false_recall_rate']:.1%} "
            f"| {m['avg_context_chars']} |"
        )
    lines.append("")
    return "\n".join(lines)


def run_ablation(out_dir: str | Path | None = None) -> dict[str, Any]:
    """Execute all five profiles across every scenario via real ``prefetch_to_context_block``."""
    import v3core.config as v3_config
    import v3core.topic_recall as v3_topic_recall
    from v3core.topic_v2 import TopicConsolidatorV2

    scenarios = build_scenarios()
    orig_cfg_resolver = v3_config._resolve_data_dir
    orig_tr_resolver = v3_topic_recall._resolve_data_dir

    profile_summaries: dict[str, Any] = {}
    all_cases: dict[str, list[dict[str, Any]]] = {pname: [] for pname in PROFILE_ORDER}

    with tempfile.TemporaryDirectory(
        prefix="v3core_ablation_", ignore_cleanup_errors=True
    ) as tmp_root:
        tmp_path = Path(tmp_root)
        try:
            for s_idx, scenario in enumerate(scenarios):
                pg_legacy = _AblationFakePg(
                    qa_pairs=scenario.qa_pairs,
                    topics=scenario.topics,
                    observation_notes=scenario.observation_notes,
                    yin_paragraphs=scenario.yin_paragraphs,
                )
                consolidator = TopicConsolidatorV2(embed_fn=embed_text)
                seeds = consolidator.seed_from_legacy_topics(scenario.topics)
                dossiers, _ = consolidator.consolidate_qa_stream(
                    scenario.qa_pairs,
                    existing_dossiers=seeds,
                )
                consolidated_topics = [d.to_topic_row(embed_text) for d in dossiers]
                pg_v2 = _AblationFakePg(
                    qa_pairs=scenario.qa_pairs,
                    topics=consolidated_topics,
                    observation_notes=scenario.observation_notes,
                    yin_paragraphs=scenario.yin_paragraphs,
                )
                q_emb = embed_text(scenario.query)

                for p_idx, pname in enumerate(PROFILE_ORDER):
                    flags = PROFILES[pname]
                    use_v2 = bool(flags.get("consolidate_v2", False))
                    active_pg = pg_v2 if use_v2 else pg_legacy
                    active_topics = consolidated_topics if use_v2 else scenario.topics

                    case_dir = tmp_path / f"{s_idx}_{p_idx}_{pname}"
                    case_dir.mkdir(parents=True, exist_ok=True)
                    v3_config._resolve_data_dir = lambda cfg=None, _d=case_dir: _d
                    v3_topic_recall._resolve_data_dir = lambda cfg=None, _d=case_dir: _d

                    cfg = {
                        "basePath": str(case_dir),
                        "prefetch": {
                            "dual_path": True,
                            "rrf_k": 60,
                            "include_message_vector": False,
                            "include_topic": bool(flags["include_topic"]),
                            "include_notes": bool(flags["include_notes"]),
                            "include_yin": bool(flags["include_yin"]),
                            "topic_v2_mode": bool(flags.get("topic_v2_mode", False)),
                        },
                    }
                    t0 = time.perf_counter()
                    context_block, trace_summary = prefetch_with_trace(
                        scenario.query,
                        limit=5,
                        config=cfg,
                        pg=active_pg,
                        q_emb=q_emb,
                        max_chars=4000,
                    )
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    all_cases[pname].append(
                        _evaluate_case(
                            scenario,
                            pname,
                            context_block,
                            trace_summary,
                            elapsed_ms,
                            active_topics=active_topics,
                        )
                    )
        finally:
            v3_config._resolve_data_dir = orig_cfg_resolver
            v3_topic_recall._resolve_data_dir = orig_tr_resolver
            gc.collect()

    for pname in PROFILE_ORDER:
        profile_summaries[pname] = _aggregate_profile(pname, all_cases[pname])

    summary = {
        "version": "topic-ablation-v2",
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scenario_count": len(scenarios),
        "profiles": profile_summaries,
        "cases": all_cases,
    }
    report_md = _render_markdown_report(summary)

    if out_dir is not None:
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        (out_path / "ablation_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out_path / "ablation_report.md").write_text(report_md, encoding="utf-8")

    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run offline G0/G1 vs G2 + A->B stale-memory ablation benchmark."
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="External output directory for ablation_summary.json and ablation_report.md.",
    )
    args = parser.parse_args(argv)
    summary = run_ablation(out_dir=args.out_dir)
    for pname in PROFILE_ORDER:
        m = summary["profiles"][pname]
        print(
            f"[topic-ablation] profile={pname} pass={m['pass_count']}/{m['scenario_count']} "
            f"({m['pass_rate']:.1%}) evo={m['evolution_pass_rate']:.1%} "
            f"synth={m['synthesis_pass_rate']:.1%} stale_outranks={m['stale_outranks_current_rate']:.1%} "
            f"false_recall={m['false_recall_rate']:.1%} avg_chars={m['avg_context_chars']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
