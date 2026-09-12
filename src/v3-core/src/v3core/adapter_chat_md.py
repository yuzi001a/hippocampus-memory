"""chat_*.md 早期对话 → ConversationTurn adapter

解析柚_MigrationData/02-会话数据/chat_records_extracted/ 下的 chat_YYYY-MM-DD.md
每个文件 = 一天的多人对话流（含 cron 触发 / 子代理任务 / 用户 / 助手回复）。

源格式样本（chat_2026-04-10.md）：
    ---
    date: 2026-04-10
    type: daily-chat
    ---
    # 对话记录 · 2026-04-10
    > 共 26 条消息
    ### 08:00
    - 👤 [cron:test-wechat-v7 ...] 发一条微信消息：...
    - 🤖 任务正在执行中——...

识别规则：
- 日期：YAML frontmatter `date` 字段（fallback: 文件名 chat_YYYY-MM-DD.md）
- 时间：`### HH:MM` 标题（Asia/Shanghai 时区 → 转 UTC 存）
- 角色：行首 `- 👤 ` = user；`- 🤖 ` = assistant；其他 emoji（🍬 等）= 跳过
- 内容：剥离前缀 `[cron:xxx]` / `[Sat 2026-04-11 01:10 GMT+8]`，但保留原内容便于追溯
- 跳过：空内容、HEARTBEAT、`[Subagent Context]` 注入前缀、`[OUT-OF-BAND`、`[IMPORTANT:`、
        `[ASYNC DELEGATION`、`【IMPORTANT` 等系统注入

session_id = f"chat_{YYYY-MM-DD}"（一天一个会话）
turn_id = 文件内序号（保证唯一，不依赖全局时间流）

能力矩阵（参考 design/clean_reimport_plan.md §1.1b）：
- session_id ✅  role ✅  content ✅  timestamp ✅（date+HH:MM 推断）
- turn_id ⚠️（文件内序号）
- tool_calls ❌ NULL  tool_results ❌ NULL
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

_CST = _get_source_tz()
_UTC = datetime.timezone.utc

# 行首角色映射：emoji → role
_ROLE_USER = "user"
_ROLE_ASST = "assistant"

# 注入前缀（统一过滤规则——参考 design §2）
_INJECTION_RE = re.compile(
    r'^\[?\s*(?:\d{1,2}\s+\w{3}\s+\d{4}|\w{3}\s+\d{1,2}|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2})?'
    r'\s*\]?\s*\[(?:IMPORTANT|ASYNC DELEGATION|Subagent Context|OUT-OF-BAND|'
    r'【IMPORTANT|【ASYNC)'
)

# 行首时间戳剥离（前导 [Sat ... GMT+8] 这种，让 user content 干净一些）
_LEADING_TS_RE = re.compile(
    r'^\s*\[(?:\d{1,2}\s+\w{3}\s+\d{4}\s+\d{1,2}:\d{2}\s+GMT[+\-]\d{1,2}|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)\]\s*'
)

# 文件名解析：chat_2026-04-10.md → (2026, 4, 10)
_FILENAME_DATE_RE = re.compile(r'chat_(\d{4})-(\d{2})-(\d{2})\.md$')


def _parse_filename_date(path: Path) -> Optional[datetime.date]:
    """从文件名 chat_YYYY-MM-DD.md 抽日期（找不到返回 None）"""
    m = _FILENAME_DATE_RE.search(path.name)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _parse_frontmatter_date(text: str) -> Optional[datetime.date]:
    """从 YAML frontmatter date: YYYY-MM-DD 抽日期"""
    m = re.search(r'^date:\s*(\d{4}-\d{2}-\d{2})', text, re.MULTILINE)
    if m:
        try:
            return datetime.date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None


def _strip_injection_prefix(text: str) -> str:
    """剥离常见系统注入前缀；返回原内容如果没匹配到"""
    return _LEADING_TS_RE.sub('', text).strip() if text else text


def _is_injection(text: str) -> bool:
    """判断是否是应跳过的注入消息（Subagent Context / OUT-OF-BAND 等）"""
    if not text:
        return True
    return bool(_INJECTION_RE.match(text))


def _should_skip(text: str) -> bool:
    """统一过滤：空 / HEARTBEAT / System 前缀 / 注入前缀"""
    if not text or not text.strip():
        return True
    t = text.strip()
    if t.startswith('HEARTBEAT') or t.startswith('System'):
        return True
    if _is_injection(t):
        return True
    return False


def _iter_turns(text: str, session_date: datetime.date) -> Iterator[ConversationTurn]:
    """从 chat md 文本流逐 turn yield。

    状态机：
    - 跟踪当前 time 块（`### HH:MM`）
    - 每行 `- emoji ...` 创建一个 turn（user/assistant 各占一行）
    - 跳过空、注入、HEARTBEAT
    """
    session_id = f"chat_{session_date.isoformat()}"
    current_time: Optional[datetime.time] = None
    turn_idx = 0

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue

        # 时间块标题：`### HH:MM`
        m = re.match(r'^###\s+(\d{1,2}):(\d{2})\s*$', line)
        if m:
            current_time = datetime.time(int(m.group(1)), int(m.group(2)))
            continue

        # 消息行：`- 👤 ...` 或 `- 🤖 ...`
        if line.startswith('- 👤'):
            role = _ROLE_USER
        elif line.startswith('- 🤖'):
            role = _ROLE_ASST
        else:
            # 其他 emoji（🍬/🌌 等）= 系统/子代理，保守跳过
            continue

        # 剥掉前缀 `- 👤 ` / `- 🤖 `
        body = line[len('- 👤'):].lstrip() if role == _ROLE_USER else line[len('- 🤖'):].lstrip()

        if _should_skip(body):
            continue

        body = _strip_injection_prefix(body)

        # 时间戳：CST 当天 + current_time → 转 UTC
        ts: Optional[datetime.datetime] = None
        if current_time is not None:
            ts = datetime.datetime.combine(
                session_date, current_time, tzinfo=_CST
            ).astimezone(_UTC)

        turn_idx += 1
        turn_id = f"{session_id}_{turn_idx:04d}"

        if role == _ROLE_USER:
            user_msg = UserMessage(
                content=body,
                timestamp=ts.timestamp() if ts else None,
            )
            yield ConversationTurn(
                turn_id=turn_id,
                source="chat_md",
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
                source="chat_md",
                session=SessionInfo(id=session_id, turn_index=turn_idx - 1),
                user=None,
                assistant=asst_msg,
                flags=Flags(),
            )


def extract(chat_md_path: str | Path) -> list[ConversationTurn]:
    """从一个 chat_YYYY-MM-DD.md 提取所有 turn。

    日期优先级：YAML frontmatter > 文件名。
    """
    p = Path(chat_md_path)
    raw = p.read_bytes().decode('utf-8', errors='replace')

    # 日期
    session_date = _parse_frontmatter_date(raw)
    if session_date is None:
        session_date = _parse_filename_date(p)
    if session_date is None:
        raise ValueError(f"无法确定 {p.name} 的日期（无 frontmatter date 也无法从文件名解析）")

    return list(_iter_turns(raw, session_date))


def extract_dir(chat_md_dir: str | Path) -> list[ConversationTurn]:
    """从目录批量提取所有 chat_*.md，按文件日期排序。"""
    d = Path(chat_md_dir)
    files = sorted(d.glob('chat_*.md'))
    all_turns: list[ConversationTurn] = []
    for f in files:
        try:
            all_turns.extend(extract(f))
        except Exception as e:
            # 单文件失败不阻塞整批
            import sys
            print(f"[adapter_chat_md] 跳过 {f.name}: {_safe_err(e)}", file=sys.stderr)
    return all_turns


# --- 兼容 import_.py 流水线 ---
def to_jsonl(turns: list[ConversationTurn]) -> str:
    """turns → JSONL 字符串（每行一个 turn）"""
    import json as _json
    return '\n'.join(t.to_json() for t in turns)