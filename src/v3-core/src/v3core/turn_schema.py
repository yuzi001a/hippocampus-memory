"""海马插件基础数据结构定义

ConversationTurn — 所有来源对话数据的统一格式。
每个 turn = 一个完整的对话回合（用户提问 + 助手完整响应链条）。
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, Any
import json

@dataclass
class SessionInfo:
    """会话上下文"""
    id: str                             # 来源会话 ID
    turn_index: int = 0                 # 在该会话中的序号
    title: Optional[str] = None         # 会话标题
    model: Optional[str] = None         # 会话默认模型
    provider: Optional[str] = None      # 会话默认服务商

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class UserMessage:
    """用户消息"""
    content: str                        # 用户消息全文（Q）
    timestamp: Optional[float] = None   # Unix 时间戳（秒）

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class AssistantMessage:
    """助手回复（含完整响应链条）"""
    content: str                        # 最终回复文本（A）
    timestamp: Optional[float] = None   # Unix 时间戳（秒）
    model: Optional[str] = None         # 实际模型，覆盖 session.model
    provider: Optional[str] = None      # 实际服务商
    thinking: Optional[str] = None      # 推理过程
    tool_calls: Optional[Any] = None    # 工具调用原始 JSON
    finish_reason: Optional[str] = None # stop / tool_calls / length

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class UsageInfo:
    """Token 消耗统计"""
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Flags:
    """状态标记"""
    active: bool = True                 # 是否活跃
    compacted: bool = False             # 是否被上下文压缩

    def to_dict(self):
        return asdict(self)


@dataclass
class ConversationTurn:
    """一个对话回合"""
    turn_id: str                        # 全局唯一 ID
    source: str                         # 来源标识
    version: int = 1                    # 结构版本号
    session: Optional[SessionInfo] = None
    user: Optional[UserMessage] = None
    assistant: Optional[AssistantMessage] = None
    usage: Optional[UsageInfo] = None
    flags: Flags = field(default_factory=Flags)
    extras: dict = field(default_factory=dict)

    def to_dict(self):
        d = {
            "turn_id": self.turn_id,
            "source": self.source,
            "version": self.version,
        }
        if self.session:
            d["session"] = self.session.to_dict()
        if self.user:
            d["user"] = self.user.to_dict()
        if self.assistant:
            d["assistant"] = self.assistant.to_dict()
        if self.usage:
            d["usage"] = self.usage.to_dict()
        d["flags"] = self.flags.to_dict()
        if self.extras:
            d["extras"] = self.extras
        return d

    def to_json(self, **kw):
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)

    def to_qatext(self) -> str:
        """返回用于聚簇的 Q&A 文本"""
        q = self.user.content[:500] if self.user else ""
        a = self.assistant.content[:1000] if self.assistant else ""
        return f"Q: {q}\nA: {a}"

    @classmethod
    def from_dict(cls, d: dict) -> "ConversationTurn":
        session = SessionInfo(**d["session"]) if d.get("session") else None
        user = UserMessage(**d["user"]) if d.get("user") else None
        assistant = AssistantMessage(**d["assistant"]) if d.get("assistant") else None
        usage = UsageInfo(**d["usage"]) if d.get("usage") else None
        flags = Flags(**d.get("flags", {}))
        extras = d.get("extras", {})
        return cls(
            turn_id=d["turn_id"],
            source=d["source"],
            version=d.get("version", 1),
            session=session,
            user=user,
            assistant=assistant,
            usage=usage,
            flags=flags,
            extras=extras,
        )


# --- 工具函数 ---

def parse_iso_timestamp(ts_str: str) -> Optional[float]:
    """ISO 8601 → Unix float"""
    import datetime
    try:
        # ISO 8601: 2026-06-05T06:53:59.403Z
        ts_str = ts_str.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(ts_str)
        return dt.timestamp()
    except:
        return None


def parse_unix_ms(ts_ms: int) -> float:
    """毫秒级 Unix → 秒级 float"""
    return ts_ms / 1000.0
