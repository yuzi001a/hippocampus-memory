"""v3-core 数据模型 — 零依赖 Hermes"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

# 7 标准类别
DEFAULT_CATEGORIES = [
    {"id": "memory", "label": "感情/关系"},
    {"id": "lessons", "label": "教训"},
    {"id": "decisions", "label": "决策"},
    {"id": "projects", "label": "项目"},
    {"id": "creative", "label": "创作"},
    {"id": "evolution", "label": "身份进化"},
    {"id": "system", "label": "系统状态"},
]
VALID_CATEGORY_IDS = {c["id"] for c in DEFAULT_CATEGORIES}


@dataclass
class DeepCard:
    """一张碑卡"""
    filename: str
    category: str
    date: str
    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    source: str = "extraction"
    path: str = ""
    embedding: list[float] | None = None
    when: str = ""
    where: str = ""
    who: str = ""
    why: str = ""
    source_j_ids: list[str] = field(default_factory=list)
    # Phase 2 confidence gating: single-extraction cards default to 0.3; cards
    # verified 3+ times are boosted to 0.7 by the dreamer module.
    confidence: float = 0.3
    observation_count: int = 1

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "category": self.category,
            "date": self.date,
            "title": self.title,
            "content": self.content[:200] + "...",
            "tags": self.tags,
            "source": self.source,
            "when": self.when,
            "where": self.where,
            "who": self.who,
            "why": self.why,
            "source_j_ids": self.source_j_ids,
            "confidence": self.confidence,
            "observation_count": self.observation_count,
        }


@dataclass
class RecallHit:
    """召回命中结果"""
    source_id: str
    title: str
    content_preview: str
    category: str = ""
    tags: list[str] = field(default_factory=list)
    cosine: float = 0.0
    rrf_score: float = 0.0
    facts: list[str] = field(default_factory=list)
    kind: str = "card"  # card | message | topic | note
    created_at: str = ""  # ISO datetime string for time-decay calculation
    # 2026-08-08: 全文通道 — 存储层全文完整返回, 注入时按类型给预算, 不再截 500/60/200
    content: str = ""

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "content_preview": self.content_preview,
            "category": self.category,
            "tags": self.tags,
            "cosine": self.cosine,
            "rrf_score": self.rrf_score,
            "facts": self.facts,
            "kind": self.kind,
            "created_at": self.created_at,
            "content": self.content,
        }


@dataclass
class ProcessedState:
    """处理状态跟踪"""
    rawSessions: list[str] = field(default_factory=list)
    deepExtracted: list[str] = field(default_factory=list)
    lastE1Run: str | None = None

    def to_dict(self) -> dict:
        return {
            "rawSessions": self.rawSessions,
            "deepExtracted": self.deepExtracted,
            "lastE1Run": self.lastE1Run,
        }


@dataclass
class CardResult:
    """写卡结果。

    P2a (2026-09-09): 向后兼容扩展 — 既有 ``success/path/error/card`` 字段
    保持不变, 新增 ``source_id/durable/durable_store/status/warnings``
    用于承载"PG 是否真写入"这条核心真值.

    ``status`` 仅允许以下冻结状态名:

        * ``"DURABLE_COMMITTED"`` — PG 真值写入成功 (回读比对一致), 派生
          副作用 (SQLite / 嵌入缓存 / topics 同步) 均无异常. ``durable=True``.
          本次写卡在 PG 上是新插入 (不是与已有 source_id 重复).
        * ``"DURABLE_FAILED"`` — PG 真值写入失败 (insert/upsert 抛错,
          readback 缺失/内容不一致, 或 PG 未连接). ``success=False,
          durable=False``. 派生副作用一律不执行.
        * ``"DERIVED_WARNING"`` — PG 真值已写入 (durable=True), 但某个
          派生副作用 (SQLite 写 / topics 同步 / 嵌入缓存) 失败. PG 真值
          不被覆盖, 失败信息进入 ``warnings``.
        * ``"DEDUPLICATED"`` — PG 真值已写入 (durable=True), 且本次
          写卡的 ``source_id`` 在 PG 上已经存在 canonical 行, 内容与本次
          写入一致 (PG upsert ON CONFLICT 走的是 idempotent update 路径).
          不伪造 QA id, 不改写 caller 元数据; ``warnings`` 为空 (除非
          派生侧另有失败). 用于把"retry 同 source_id 同一内容"与"全新
          写卡"区分开, 让上游 UI / 审计能识别 retry 命中.
        """
    success: bool
    path: str = ""
    error: str = ""
    card: dict | None = None
    # P2a 新增字段 — 全部带默认值, 老构造路径 (CardResult(success=..., ...))
    # 一行不传仍可工作.
    source_id: str = ""
    durable: bool = False
    durable_store: str = ""
    status: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-safe 序列化 — P2a 需要 dict 形式的字段回传到上层 (extract 摘要)."""
        return {
            "success": bool(self.success),
            "path": str(self.path),
            "error": str(self.error),
            "card": self.card,
            "source_id": str(self.source_id),
            "durable": bool(self.durable),
            "durable_store": str(self.durable_store),
            "status": str(self.status),
            "warnings": list(self.warnings),
        }


@dataclass
class ExtractResult:
    """提取结果"""
    success: bool
    count: int = 0
    written: int = 0
    skipped: int = 0
    cards: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    error: str = ""
