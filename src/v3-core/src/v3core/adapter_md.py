"""对话记录完整版_Apr7-14.md → ConversationTurn adapter

解析 .hermes/desktop-attachments/ 对话记录完整版_Apr7-14.md。
源文件 = OpenClaw 早期多日对话（4/7 - 4/14）恢复版。

源格式样本：
    ## 2026-04-07（56条消息，9条用户）
    ### 22:00
    - 🤖 **22:02**: Hey！我是刚刚上线的AI助手...
    - 🤖 **22:02**: 让我查看一下进程状态：
    - 👤 **00:00**: Sender (untrusted metadata): ...

识别规则：
- 日期：`## YYYY-MM-DD（...）` 标题（Asia/Shanghai 时区 → 转 UTC 存）
- 时间：`### HH:MM` 大块标题（兜底，没有时尝试从 `- emoji **HH:MM**` 提取）
- 角色：`- 👤 ` = user；`- 🤖 ` = assistant；其他 emoji 跳过
- 行内时间戳：`- 🤖 **22:02**: ...` 这种格式，行首的 **HH:MM** 比大块时间更精确，优先用
- 跳过：空、HEARTBEAT、注入前缀（[IMPORTANT: / [Subagent Context] / [OUT-OF-BAND）

session_id = f"md_2026-04-{DD}"（一天一个会话）
turn_id = 文件内序号

能力矩阵：同 chat_md 适配器（chat_md/md 同属 markdown 源，无 tool_calls/tool_results）
"""

import re
import datetime
from pathlib import Path
from typing import Iterator, Optional

from .turn_schema import (
    ConversationTurn, SessionInfo, UserMessage,
    AssistantMessage, Flags,
)


# 源数据时区 — 默认 Asia/Shanghai, 可通过环境变量 V3CORE_SOURCE_TZ 覆盖 (如 Asia/Tokyo)
def _get_source_tz():
    """读 V3CORE_SOURCE_TZ 环境变量决定源时区.

    有 tzdata 时用 zoneinfo,无 tzdata 时回退固定偏移(+8).
    """
    import os
    name = os.environ.get("V3CORE_SOURCE_TZ", "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        # 无 tzdata 时回退固定偏移 (+8, 适用于 Asia/Shanghai/Tokyo/Seoul)
        return datetime.timezone(datetime.timedelta(hours=8))


_CST = _get_source_tz()
_UTC = datetime.timezone.utc
_ROLE_USER = "user"
_ROLE_ASST = "assistant"

# 日期标题：`## 2026-04-07（...）` 或 `## 2026-04-07(...)`
_DATE_HEAD_RE = re.compile(r'^##\s+(\d{4}-\d{2}-\d{2})')

# 大块时间：`### 22:00` 或 `### HH:MM`
_BLOCK_TIME_RE = re.compile(r'^###\s+(\d{1,2}):(\d{2})\s*$')

# 行内时间：`- 🤖 **22:02**:` 或 `- 👤 **22:02**:` —— 行首 5-9 字符 **HH:MM**
_INLINE_TIME_RE = re.compile(r'^\*\*(?P<h>\d{1,2}):(?P<m>\d{2})(?:\*\*|\*\*:)')

# 注入前缀
_INJECTION_RE = re.compile(
    r'^\s*\[?(?:\d{1,2}\s+\w{3}\s+\d{4}|\w{3}\s+\d{1,2}|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2})?'
    r'\s*\]?\s*\[(?:IMPORTANT|ASYNC DELEGATION|Subagent Context|OUT-OF-BAND|'
    r'【IMPORTANT|【ASYNC)'
)


def _should_skip(text: str) -> bool:
    if not text or not text.strip():
        return True
    t = text.strip()
    if t.startswith('HEARTBEAT') or t.startswith('System'):
        return True
    if _INJECTION_RE.match(t):
        return True
    return False


def _iter_turns(text: str) -> Iterator[ConversationTurn]:
    """从对话记录 md 流式 yield turn。

    状态机：
    - 跟踪当前 session_date + block_time
    - 每行 `- emoji **HH:MM**: ...` 创建一个 turn（用户消息没有 **HH:MM** 时用 block_time 兜底）
    - 跳过空、注入、HEARTBEAT
    - 解析多行内容：`- xxx:` 后的换行继续内容（直到下一个 `- ` 行 / 空行块结束）
    """
    current_date: Optional[datetime.date] = None
    block_time: Optional[datetime.time] = None
    turn_idx = 0

    # 先把多行 markdown 内容按行处理
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        # 日期标题
        m = _DATE_HEAD_RE.match(line)
        if m:
            try:
                current_date = datetime.date.fromisoformat(m.group(1))
            except ValueError:
                current_date = None
            i += 1
            continue

        # 大块时间
        m = _BLOCK_TIME_RE.match(line)
        if m:
            block_time = datetime.time(int(m.group(1)), int(m.group(2)))
            i += 1
            continue

        # 消息行
        if line.startswith('- 👤'):
            role = _ROLE_USER
            rest = line[len('- 👤'):].lstrip()
        elif line.startswith('- 🤖'):
            role = _ROLE_ASST
            rest = line[len('- 🤖'):].lstrip()
        else:
            i += 1
            continue

        # 提取行内时间（**HH:MM**: 或 **HH:MM**）
        inline_t = None
        m = _INLINE_TIME_RE.match(rest)
        if m:
            inline_t = datetime.time(int(m.group('h')), int(m.group('m')))
            # 剥掉 **HH:MM**: 前缀
            cut = m.end()
            rest = rest[cut:].lstrip()
            if rest.startswith(':'):
                rest = rest[1:].lstrip()

        # 收集多行内容：本行 + 后续非 `- ` 起始的连续行
        body_parts = [rest]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip():
                # 空行 → 内容终止
                break
            if nxt.lstrip().startswith(('- ', '#', '---', '```')):
                break
            body_parts.append(nxt.strip())
            j += 1
        body = '\n'.join(body_parts).strip()

        if _should_skip(body):
            i = j
            continue

        # 时间戳：行内时间优先（更精确），否则用 block_time
        ts_time = inline_t or block_time
        ts: Optional[datetime.datetime] = None
        if current_date is not None and ts_time is not None:
            ts = datetime.datetime.combine(
                current_date, ts_time, tzinfo=_CST
            ).astimezone(_UTC)

        session_id = f"md_{current_date.isoformat()}" if current_date else "md_unknown"
        turn_idx += 1
        turn_id = f"{session_id}_{turn_idx:04d}"

        if role == _ROLE_USER:
            user_msg = UserMessage(
                content=body,
                timestamp=ts.timestamp() if ts else None,
            )
            yield ConversationTurn(
                turn_id=turn_id,
                source="md",
                session=SessionInfo(id=session_id, turn_index=turn_idx - 1),
                user=user_msg,
                assistant=None,
                flags=Flags(),
            )
        else:
            asst_msg = AssistantMessage(
                content=body,
                timestamp=ts.timestamp() if ts else None,
            )
            yield ConversationTurn(
                turn_id=turn_id,
                source="md",
                session=SessionInfo(id=session_id, turn_index=turn_idx - 1),
                user=None,
                assistant=asst_msg,
                flags=Flags(),
            )

        i = j  # 跳到已读末尾


def extract(md_path: str | Path) -> list[ConversationTurn]:
    """从 .md 文件提取所有 turn（覆盖所有 ## 日期段）"""
    p = Path(md_path)
    raw = p.read_bytes().decode('utf-8', errors='replace')
    return list(_iter_turns(raw))


# --- 兼容 import_.py ---
def to_jsonl(turns: list[ConversationTurn]) -> str:
    import json as _json
    return '\n'.join(t.to_json() for t in turns)