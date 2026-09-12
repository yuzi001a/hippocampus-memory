"""Standardized signal packets — the lingua franca of v3-core.

Every external input (conversation turn, session event, system signal)
is normalized into a Signal before entering the core pipeline.

设计哲学：核心不依赖任何外部平台。所有输入都通过标准化信号包进入，
适配器负责把平台事件转成 Signal/TurnPacket/SessionPacket。

零外部依赖：只 import stdlib。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnPacket:
    """一次对话轮次的标准感知单元

    Fields:
        session_id: 会话 id
        turn_id:    本轮编号（在同一 session 内自增）
        role:       user | assistant | tool
        content:    消息正文
        content_type: text | code | tool_call | tool_result | image
        timestamp:  ISO 字符串（可选）
        metadata:   扩展字段（tokens、model、latency 等）
    """
    session_id: str
    turn_id: int
    role: str
    content: str
    content_type: str = "text"
    timestamp: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "role": self.role,
            "content": self.content,
            "content_type": self.content_type,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata or {}),
        }


@dataclass
class SessionPacket:
    """一次完整对话会话的标准包

    Fields:
        session_id: 会话 id
        turns:      该会话的所有 TurnPacket
        platform:   hermes | openclaw | claude-code | 其他
        project:    所属项目（可选）
        start_time: ISO 字符串
        tags:       会话级标签
        metadata:   扩展字段
    """
    session_id: str
    turns: list[TurnPacket] = field(default_factory=list)
    platform: str = "hermes"
    project: str = ""
    start_time: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def add_turn(self, turn: TurnPacket) -> None:
        """追加一条轮次"""
        self.turns.append(turn)

    def user_assistant_pairs(self) -> list[tuple[TurnPacket, TurnPacket | None]]:
        """按 user → assistant 配对返回（用于提炼）"""
        pairs: list[tuple[TurnPacket, TurnPacket | None]] = []
        i = 0
        while i < len(self.turns):
            t = self.turns[i]
            if t.role == "user":
                nxt = self.turns[i + 1] if i + 1 < len(self.turns) and self.turns[i + 1].role == "assistant" else None
                pairs.append((t, nxt))
            i += 1
        return pairs

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "turns": [t.to_dict() for t in self.turns],
            "platform": self.platform,
            "project": self.project,
            "start_time": self.start_time,
            "tags": list(self.tags or []),
            "metadata": dict(self.metadata or {}),
        }


@dataclass
class Signal:
    """通用信号 — 覆盖对话、生命周期、系统事件

    Fields:
        source:   "hermes.sync_turn" | "hermes.session_end" | "hermes.feedback"
                  | "openclaw.sync_turn" | "system.cron" | ...
        type:     "turn" | "session_start" | "session_end" | "feedback"
                  | "maintenance" | "cron_tick"
        subject:  主体标识（session_id / card_id / cron_name）
        payload:  标准化数据（具体形状由 type 决定）
        timestamp: ISO 字符串
        metadata: 扩展字段
    """
    source: str
    type: str
    subject: str
    payload: dict
    timestamp: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "type": self.type,
            "subject": self.subject,
            "payload": dict(self.payload or {}),
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata or {}),
        }


__all__ = ["TurnPacket", "SessionPacket", "Signal"]