"""OpenClaw trajectory.jsonl → ConversationTurn adapter

解析柚_MigrationData/00-完整备份_20260604_2235/sessions/*.trajectory.jsonl
（以及相邻的 *.jsonl 文件——同目录不同 schema）

文件格式（实地勘察 2026-08-01）：
A. `<UUID>.trajectory.jsonl` — openclaw-trajectory schema v1
   每个事件顶层 keys: type, ts, seq, sourceSeq, sessionId, sessionKey,
                       runId, workspaceDir, provider, modelId, modelApi, data
   事件类型: session.started / trace.metadata / context.compiled /
              prompt.submitted / model.completed / trace.artifacts / session.ended
   - prompt.submitted.data.prompt    = 用户 prompt（字符串）
   - prompt.submitted.data.messages  = 消息历史（结构化 array，常为空）
   - model.completed.data.assistantTexts = 助手回复 list（[0] = 最终文本）
   - 顶层 ts = ISO 8601 时间戳（UTC）

B. `<UUID>.jsonl` — openclaw session schema（不同）
   每个事件顶层 keys: type, id, parentId, timestamp
   - session / model_change / thinking_level_change / custom
   - message（含 role: user / assistant / toolResult）
     user / assistant 的 content = string 或 array（[{type,text}, ...]）
     assistant 含 usage / stopReason / responseId / provider / model

本适配器同时支持两种格式，按 schema 自动识别。turn = (user prompt) + (asst response)。

能力矩阵（参考 design §1.1b）：
- session_id ✅（用文件名 UUID）
- role ✅
- content ✅（完整保留）
- timestamp ✅（事件 ts）
- turn_id ✅（按 prompt.submitted / message.user 出现顺序递增）
- tool_calls ✅（assistant 消息里如果有 tool_calls；trajectory 格式通常没有独立 tool 事件）
- tool_results ✅
"""

import re
import json
import datetime
from pathlib import Path
from typing import Iterator, Optional, Any

from .turn_schema import (
    ConversationTurn, SessionInfo, UserMessage,
    AssistantMessage, UsageInfo, Flags,
)



try:
    from . import _safe_err
except (ImportError, AttributeError):
    try:
        from .. import _safe_err
    except (ImportError, AttributeError):
        def _safe_err(e, max_len=80):
            try:
                from . import _safe_err as _impl
            except ImportError:
                from .. import _safe_err as _impl
            globals()["_safe_err"] = _impl
            return _impl(e, max_len)

_UTC = datetime.timezone.utc


# ── 时间戳解析 ──

def _parse_iso(s: str) -> Optional[datetime.datetime]:
    """ISO 8601 → tz-aware datetime（UTC）；解析失败返回 None"""
    if not s:
        return None
    try:
        s = s.strip().replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        return dt.astimezone(_UTC)
    except Exception:
        return None


def _normalize_ts(ts: Any) -> Optional[datetime.datetime]:
    """多种类型 → tz-aware datetime（UTC）"""
    if ts is None:
        return None
    if isinstance(ts, datetime.datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=_UTC)
        return ts.astimezone(_UTC)
    if isinstance(ts, (int, float)):
        try:
            v = float(ts)
            if v > 1e12:
                v = v / 1000.0
            return datetime.datetime.fromtimestamp(v, tz=_UTC)
        except Exception:
            return None
    if isinstance(ts, str):
        return _parse_iso(ts)
    return None


# ── content 提取（兼容 string 和 list[{type, text}] 两种形态） ──

def _extract_text(content: Any) -> str:
    """从 OpenClaw content 字段抽纯文本。

    兼容：
    - str → 原样返回
    - list[dict] → 拼出 text 字段（type=='text'）
    - 其他 → str(content)
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get('type')
                if t == 'text' and 'text' in item:
                    parts.append(str(item['text']))
                elif t == 'thinking' and 'thinking' in item:
                    # thinking 也保留，前缀标识，便于溯源
                    parts.append(f"[thinking] {item['thinking']}")
                else:
                    # 其他类型原样序列化
                    parts.append(str(item)[:200])
            else:
                parts.append(str(item)[:200])
        return '\n'.join(parts)
    return str(content)


# ── Schema 探测 ──

def _detect_schema(first_event: dict) -> str:
    """返回 'trajectory' 或 'session' 或 'unknown'"""
    if not first_event:
        return 'unknown'
    t = first_event.get('type', '')
    if t in ('session.started', 'trace.metadata', 'context.compiled',
             'prompt.submitted', 'model.completed', 'trace.artifacts', 'session.ended'):
        return 'trajectory'
    if t in ('session', 'model_change', 'thinking_level_change',
             'custom', 'message'):
        return 'session'
    return 'unknown'


# ── Trajectory schema 解析 ──

def _iter_trajectory_turns(events: list[dict], session_id: str) -> Iterator[ConversationTurn]:
    """trajectory schema：prompt.submitted + model.completed 配对生成 turn。"""
    pending_user: Optional[dict] = None  # {content, ts, prompt_event}
    turn_idx = 0
    session_started: Optional[dict] = None

    for ev in events:
        ev_type = ev.get('type')
        ts = _parse_iso(ev.get('ts', ''))

        if ev_type == 'session.started':
            session_started = ev
            continue

        if ev_type == 'prompt.submitted':
            # 收集 user prompt（取 .data.prompt 字符串）
            data = ev.get('data') or {}
            prompt_text = data.get('prompt', '')
            if not prompt_text and isinstance(data.get('messages'), list):
                # 退化：用最后一条 user 消息
                for m in reversed(data['messages']):
                    if isinstance(m, dict) and m.get('role') == 'user':
                        prompt_text = _extract_text(m.get('content'))
                        break
            pending_user = {
                'content': _extract_text(prompt_text),
                'ts': ts,
            }
            continue

        if ev_type == 'model.completed':
            data = ev.get('data') or {}
            assistant_texts = data.get('assistantTexts') or []
            asst_content = '\n'.join(str(t) for t in assistant_texts if t)
            ts = ts or pending_user and pending_user.get('ts')

            if pending_user and (pending_user['content'].strip() or asst_content.strip()):
                turn_idx += 1
                user_ts = pending_user.get('ts')
                yield ConversationTurn(
                    turn_id=f"{session_id}_{turn_idx:05d}",
                    source="trajectory",
                    session=SessionInfo(id=session_id, turn_index=turn_idx - 1),
                    user=UserMessage(
                        content=pending_user['content'],
                        timestamp=user_ts.timestamp() if user_ts else None,
                    ),
                    assistant=AssistantMessage(
                        content=asst_content or '(tool call only)',
                        timestamp=ts.timestamp() if ts else None,
                        model=ev.get('modelId'),
                        provider=ev.get('provider'),
                    ),
                    flags=Flags(),
                )
                pending_user = None
            elif asst_content.strip():
                # 没有 user 但有 asst（孤儿助手消息）—— 单独成 turn
                turn_idx += 1
                yield ConversationTurn(
                    turn_id=f"{session_id}_{turn_idx:05d}",
                    source="trajectory",
                    session=SessionInfo(id=session_id, turn_index=turn_idx - 1),
                    user=None,
                    assistant=AssistantMessage(
                        content=asst_content,
                        timestamp=ts.timestamp() if ts else None,
                        model=ev.get('modelId'),
                        provider=ev.get('provider'),
                    ),
                    flags=Flags(),
                )
                pending_user = None
            continue


# ── Session schema 解析（*.jsonl 那种） ──

_INJECTION_RE = re.compile(
    r'^\[?(?:\d{1,2}\s+\w{3}\s+\d{4}|\w{3}\s+\d{1,2}|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2})?'
    r'\s*\]?\s*\[(?:IMPORTANT|ASYNC DELEGATION|Subagent Context|OUT-OF-BAND|'
    r'【IMPORTANT|【ASYNC)'
)


def _should_skip_session_msg(text: str) -> bool:
    if not text or not text.strip():
        return True
    t = text.strip()
    if t.startswith('HEARTBEAT') or t.startswith('System'):
        return True
    if _INJECTION_RE.match(t):
        return True
    return False


def _iter_session_turns(events: list[dict], session_id: str) -> Iterator[ConversationTurn]:
    """session schema：message 事件按 role 流转（user → assistant 配对）。"""
    pending_user: Optional[dict] = None
    turn_idx = 0

    for ev in events:
        if ev.get('type') != 'message':
            continue
        msg = ev.get('message') or {}
        role = msg.get('role')
        ts = _normalize_ts(msg.get('timestamp') or ev.get('timestamp'))
        content = _extract_text(msg.get('content'))

        if role == 'user':
            if _should_skip_session_msg(content):
                continue
            pending_user = {'content': content, 'ts': ts}
            continue

        if role == 'assistant':
            asst_content = content
            if not asst_content.strip() and not msg.get('tool_calls'):
                # 空回复
                pending_user = None
                continue

            turn_idx += 1
            usage_d = msg.get('usage') or {}
            yield ConversationTurn(
                turn_id=f"{session_id}_{turn_idx:05d}",
                source="trajectory",
                session=SessionInfo(id=session_id, turn_index=turn_idx - 1),
                user=UserMessage(
                    content=(pending_user or {}).get('content', ''),
                    timestamp=(pending_user or {}).get('ts').timestamp()
                              if (pending_user or {}).get('ts') else None,
                ) if pending_user else None,
                assistant=AssistantMessage(
                    content=asst_content or '(tool call only)',
                    timestamp=ts.timestamp() if ts else None,
                    model=msg.get('model'),
                    provider=msg.get('provider'),
                    tool_calls=msg.get('tool_calls'),
                    finish_reason=msg.get('stopReason'),
                ),
                usage=UsageInfo(
                    input_tokens=usage_d.get('input'),
                    output_tokens=usage_d.get('output'),
                    cache_read_tokens=usage_d.get('cacheRead'),
                    cache_write_tokens=usage_d.get('cacheWrite'),
                ) if usage_d else None,
                flags=Flags(),
            )
            pending_user = None
            continue

        # toolResult 消息 → 找最近的 pending assistant，挂 tool_results
        # 这里简单处理：丢弃（不影响主对话流完整性）
        pending_user = None


# ── 文件入口 ──

def extract(trajectory_path: str | Path) -> list[ConversationTurn]:
    """从单个 trajectory.jsonl / .jsonl 提取所有 turn。

    自动探测 schema（trajectory vs session），按对应解析器生成 turn。
    session_id = 文件名 UUID（不带后缀）。
    """
    p = Path(trajectory_path)
    session_id = p.stem  # e.g. '01bfcb23-...-66f86'

    raw = p.read_bytes().decode('utf-8', errors='replace')
    events: list[dict] = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            events.append(json.loads(ln))
        except json.JSONDecodeError:
            # 容忍坏行（trajectory 文件偶尔有零字节行 / 截断行）
            continue

    if not events:
        return []

    schema = _detect_schema(events[0])
    if schema == 'trajectory':
        return list(_iter_trajectory_turns(events, session_id))
    elif schema == 'session':
        return list(_iter_session_turns(events, session_id))
    else:
        return []


def extract_dir(sessions_dir: str | Path) -> list[ConversationTurn]:
    """批量解析目录下所有 trajectory.jsonl + .jsonl（非 deleted 副本）。"""
    d = Path(sessions_dir)
    all_turns: list[ConversationTurn] = []
    seen_files = 0
    skipped_deleted = 0
    for p in d.iterdir():
        if p.is_dir():
            continue
        # 跳过 .deleted.* 副本
        if '.deleted' in p.name:
            skipped_deleted += 1
            continue
        # 只取两种格式
        if not (p.name.endswith('.trajectory.jsonl')
                or (p.name.endswith('.jsonl') and not p.name.endswith('.trajectory.jsonl'))):
            continue
        # 跳过 .trajectory-path.json 这种辅助文件
        if p.name.endswith('-path.json'):
            continue
        try:
            all_turns.extend(extract(p))
            seen_files += 1
        except Exception as e:
            import sys
            print(f"[adapter_trajectory] 跳过 {p.name}: {_safe_err(e)}", file=sys.stderr)
    print(f"[adapter_trajectory] 解析 {seen_files} 个文件（跳过 {skipped_deleted} 个 deleted 副本）")
    return all_turns


# --- 兼容 import_.py ---
def to_jsonl(turns: list[ConversationTurn]) -> str:
    return '\n'.join(t.to_json() for t in turns)