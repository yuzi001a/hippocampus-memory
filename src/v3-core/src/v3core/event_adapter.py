"""v3 事件适配器 — 多源输入 → 统一结构化输出

设计：这是海马插件的入口层，负责将所有原始对话数据
统一转换成结构化 Event 对象，供下游加工（主题聚类/增量提炼/召回）。

当前来源（按优先级）：Hermes state.db > PG v3_messages > j/ turns.md
"""
import sqlite3, json, os, re
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime


@dataclass
class QAEvent:
    """最小结构化单元——单条消息"""
    role: str                    # user / assistant / tool / system
    content: str                 # 消息正文
    timestamp: Optional[float] = None  # unix timestamp
    tool_name: Optional[str] = None    # 调用的工具名
    tool_calls: Optional[str] = None   # 工具调用 JSON
    source_session: str = ''    # 来源 session ID
    source_db: str = ''         # 来源标识


@dataclass
class SessionEvents:
    """一个完整 session 的结构化数据"""
    session_id: str
    source: str                  # 'hermes_state' / 'pg' / 'j_dir'
    qa_pairs: List[QAEvent]
    title: str = ''
    time_start: float = 0
    time_end: float = 0
    turn_count: int = 0

    def to_topic_text(self) -> str:
        """输出给主题聚类使用的文本（结构 + 元信息）"""
        parts = []
        for qa in self.qa_pairs[:8]:
            line = f"[{qa.role}] {qa.content[:200]}"
            if qa.tool_name:
                line += f"\n    [工具: {qa.tool_name}]"
            parts.append(line)
        return '\n'.join(parts)


# ─── Hermes state.db 适配器（主数据源） ───

class HermesAdapter:
    """从 Hermes state.db 读取结构化对话数据"""

    STATE_DB = os.path.expanduser('~/AppData/Local/hermes/state.db')

    def __init__(self, db_path: str = None):
        self.db_path = db_path or self.STATE_DB

    def fetch_sessions(self, limit: int = 200, min_messages: int = 3) -> List[SessionEvents]:
        """取最近的 session，每个 session 包含结构化 QA 对"""
        conn = sqlite3.connect(self.db_path)
        conn.execute('PRAGMA busy_timeout=3000')
        
        # 最近的 session
        sessions_raw = conn.execute(
            "SELECT id, title, started_at, message_count FROM sessions "
            "WHERE message_count >= ? ORDER BY started_at DESC LIMIT ?",
            (min_messages, limit)
        ).fetchall()
        
        results = []
        for sid, title, started_at, msg_cnt in sessions_raw:
            msgs = conn.execute(
                "SELECT role, content, tool_name, tool_calls, timestamp "
                "FROM messages WHERE session_id=? ORDER BY timestamp ASC",
                (sid,)
            ).fetchall()
            
            if len(msgs) < min_messages:
                continue
            
            qa_pairs = []
            for role, content, tname, tcalls, ts in msgs:
                qa_pairs.append(QAEvent(
                    role=role,
                    content=(content or '')[:1000],
                    timestamp=ts,
                    tool_name=tname or '',
                    tool_calls=tcalls or '',
                    source_session=sid
                ))
            
            results.append(SessionEvents(
                session_id=sid,
                source='hermes_state',
                qa_pairs=qa_pairs,
                title=title or '',
                time_start=started_at or 0,
                time_end=msgs[-1][4] if msgs else 0,
                turn_count=len(qa_pairs)
            ))
        
        conn.close()
        return results

    def fetch_session(self, session_id: str) -> Optional[SessionEvents]:
        """取单个 session"""
        conn = sqlite3.connect(self.db_path)
        conn.execute('PRAGMA busy_timeout=3000')
        
        s = conn.execute(
            "SELECT id, title, started_at, message_count FROM sessions WHERE id=?",
            (session_id,)
        ).fetchone()
        
        if not s:
            conn.close()
            return None
        
        msgs = conn.execute(
            "SELECT role, content, tool_name, tool_calls, timestamp "
            "FROM messages WHERE session_id=? ORDER BY timestamp ASC",
            (session_id,)
        ).fetchall()
        
        qa_pairs = [QAEvent(
            role=r, content=(c or '')[:1000],
            timestamp=ts, tool_name=tname or '',
            tool_calls=tcalls or '', source_session=session_id
        ) for r, c, tname, tcalls, ts in msgs]
        
        conn.close()
        return SessionEvents(
            session_id=session_id, source='hermes_state',
            qa_pairs=qa_pairs, title=s[1] or '',
            time_start=s[2] or 0, time_end=msgs[-1][4] if msgs else 0,
            turn_count=len(qa_pairs)
        )

    def stats(self) -> dict:
        """库统计"""
        conn = sqlite3.connect(self.db_path)
        conn.execute('PRAGMA busy_timeout=3000')
        msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        sess = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        roles = conn.execute(
            "SELECT role, COUNT(*) FROM messages GROUP BY role"
        ).fetchall()
        conn.close()
        return {'messages': msgs, 'sessions': sess, 'roles': dict(roles)}
