"""Hermes state.db → ConversationTurn adapter

从 Hermes state.db 的 sessions + messages 表提取对话回合，
按标准 ConversationTurn 格式输出，保留所有可用元信息。
"""

import os
import sqlite3
import json
from typing import Iterator

from .turn_schema import (
    ConversationTurn, SessionInfo, UserMessage,
    AssistantMessage, UsageInfo, Flags,
)


def _parse_tool_calls(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except:
        return raw


def _iter_turns_from_messages(conn: sqlite3.Connection, session_id: str):
    """从一个 session 的消息序列中提取回合（turn）
    
    处理 Hermes 的多步链条：
        user → asst(tool_call) → tool_result → asst(final)
    合并为一个回合，assistant 包含 tool_calls + 最终回复
    """
    rows = conn.execute("""
        SELECT id, role, content, timestamp, tool_calls, tool_name,
               finish_reason, token_count, active, compacted, reasoning_content
        FROM messages
        WHERE session_id = ? AND active = 1
          AND (content IS NULL OR TRIM(content) NOT LIKE '[CONTEXT COMPACTION%')
        ORDER BY id
    """, (session_id,)).fetchall()

    turn_idx = 0
    i = 0
    while i < len(rows):
        row = rows[i]
        
        # 找 user 消息
        if row[1] != 'user':
            i += 1
            continue
        user_content = (row[2] or '').strip()
        if not user_content or len(user_content) < 5:
            i += 1
            continue
        if user_content.startswith('HEARTBEAT') or user_content.startswith('System'):
            i += 1
            continue
        
        user_ts = row[3]
        
        # 收集这个 user 之后的所有 assistant 消息（直到下一个 user 或结束）
        asst_contents = []
        asst_ts = None
        asst_thinking = None
        asst_tool_calls = None
        asst_finish = None
        asst_model = None
        
        j = i + 1
        while j < len(rows) and rows[j][1] == 'assistant':
            a = rows[j]
            content = (a[2] or '').strip()
            if content:
                asst_contents.append(content)
            if asst_ts is None:
                asst_ts = a[3]
            tc = _parse_tool_calls(a[4])  # tool_calls (col index 4)
            if tc:
                asst_tool_calls = tc
            if a[6]:  # finish_reason (col index 6)
                asst_finish = a[6]
            if a[10]:  # reasoning_content (col index 10)
                asst_thinking = (asst_thinking or '') + '\n' + a[10]
            j += 1
        
        if not asst_contents and not asst_tool_calls:
            # 没有有效回复的回合跳过
            i = j
            continue
        
        yield ConversationTurn(
            turn_id=f"hermes_state_{session_id}_{turn_idx:05d}",
            source="hermes_state",
            session=SessionInfo(id=session_id, turn_index=turn_idx),
            user=UserMessage(
                content=user_content,
                timestamp=user_ts,
            ),
            assistant=AssistantMessage(
                content='\n'.join(asst_contents) if asst_contents else '(tool call only)',
                timestamp=asst_ts,
                finish_reason=asst_finish,
                tool_calls=asst_tool_calls,
                thinking=asst_thinking,
            ),
            flags=Flags(active=bool(row[8]), compacted=bool(row[9])),
        )
        turn_idx += 1
        i = j  # 跳到下一个 user 消息


def extract(state_db_path: str) -> list[ConversationTurn]:
    """从 Hermes state.db 提取所有 session 的回合"""
    conn = sqlite3.connect(state_db_path)
    sessions = conn.execute("""
        SELECT id, title, model, billing_provider, started_at, message_count
        FROM sessions WHERE source = 'tui'
        ORDER BY started_at
    """).fetchall()

    all_turns = []
    for sess in sessions:
        sess_id, title, model, provider, started, msg_cnt = sess
        turns = list(_iter_turns_from_messages(conn, sess_id))
        
        # 填充分会话信息
        for t in turns:
            t.session.title = title
            t.session.model = model
            t.session.provider = provider
        
        all_turns.extend(turns)
    
    conn.close()
    return all_turns


def save(turns: list[ConversationTurn], output_path: str):
    """保存为 JSONL（每行一个 turn）"""
    import datetime
    with open(output_path, 'w', encoding='utf-8') as f:
        for t in turns:
            f.write(t.to_json() + '\n')
    
    # 写统计信息
    meta = {
        "total": len(turns),
        "source": "hermes_state",
        "exported_at": datetime.datetime.now().isoformat(),
        "sessions": len(set(t.session.id for t in turns if t.session)),
        "version": 1,
    }
    with open(output_path.replace('.jsonl', '_meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    
    print(f"✅ 导出 {len(turns)} 个回合到 {output_path}")
