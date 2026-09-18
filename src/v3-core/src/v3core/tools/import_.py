"""v3_import_seed / v3_import_full 工具"""
from __future__ import annotations
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


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

logger = logging.getLogger("v3core.tools.import_")

V3_IMPORT_SEED_SCHEMA = {
    "name": "v3_import_seed",
    "description": (
        "[5-安装] 把已有记忆文件 (MEMORY.md / USER.md 等) 不调 LLM 直接导入 y/ 印层 + PG。"
        "每张印(y)卡 = 一个文件,整文件作为正文,无 LLM 提取/总结。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要导入的文件绝对路径列表,例如 ['C:/.../MEMORY.md', 'C:/.../USER.md']",
            },
            "category": {
                "type": "string",
                "description": "目标类别,默认 'memory' (印层默认落 memory/)",
                "default": "memory",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "附加到每张卡 tags 字段,默认 ['seed', 'import']",
            },
            "with_pg": {
                "type": "boolean",
                "description": "PG 可用时是否同步写 PG v3_cards 表,默认 true",
                "default": True,
            },
        },
        "required": ["memory_files"],
        "additionalProperties": False,
    },
}

# v3_import_full schema — 全量导入（基础框架,不调 LLM）
#
# 约束（当前实现）:
#   • source 支持 'hermes_state' / 'trajectory' / 'md'（开源收窄 2026-08-06:
#     个性化源 chat_md / jsonl / honcho / yjby 不对外 — 代码保留内部可用）
#   • target 仅支持 'b' (碑卡) ; 'j' (印卡) 当前返 unsupported
#   • 不调 LLM — extract.py / llm.py 完全不碰
#   • write=true 时, PG 可用才写 v3_cards ; PG 不可用则仅写盘并标注 pg_written=false
#   • max_cards 默认 100, 上限 10000 兜底
#   • 整体超时 1000s (软检查, 每个文件处理后比对 deadline)
#   • chat_md / md (adapter) / trajectory / hermes_state 都走对话流管线
#     (qa_pairs + conversation_stream)；md (旧) / jsonl 仍走 b/ 卡落盘
V3_IMPORT_FULL_SCHEMA = {
    "name": "v3_import_full",
    "description": (
        "[5-安装] 全量导入 — 把 source 指向的文件/目录按指定源类型解析, "
        "逐条写成碑(b)卡 + 可选写 PG v3_cards。chat_md/md/trajectory 三类"
        "adapter 走对话流管线 (qa_pairs + conversation_stream, 不调 LLM)；"
        "hermes_state 走 state.db 解析；md/jsonl 仍走 b/ 卡落盘。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "enum": ["hermes_state", "trajectory", "md"],
                "description": ("源类型: hermes_state (Hermes state.db 官方格式) / "
                                "trajectory (OpenClaw 会话轨迹) / md (通用 markdown 对话)"),
            },
                "source_path": {
                "type": "string",
                "description": "源文件或源目录绝对路径",
                },
            "target": {
                "type": "string",
                "enum": ["j", "b"],
                "default": "b",
                "description": "目标层: j=印卡(暂不支持) / b=碑卡(默认)",
            },
            "write": {
                "type": "boolean",
                "default": False,
                "description": "true=实际写盘+写 PG ; false=仅返回解析结果",
            },
            "max_cards": {
                "type": "integer",
                "default": 100,
                "description": "最多处理多少张卡, 默认 100",
            },
            "dry_run": {
                "type": "boolean",
                "default": True,
                "description": "true=不实际写, 仅返回将写入的内容预览",
            },
        },
        "required": ["source", "source_path"],
        "additionalProperties": False,
    },
}

_NOT_IMPLEMENTED = {
    "success": False,
    "error": "not_implemented",
    "message": "该工具尚未实现 — 调用方不应假设已成功",
}

_IMPORT_FULL_DEADLINE_SEC = 1000.0


def _safe_title_from_path(path: str) -> str:
    """从文件路径提取安全 title (后缀名去掉, 非法字符替换为 -)"""
    base = os.path.basename(path)
    stem = os.path.splitext(base)[0]
    if not stem:
        stem = "seed"
    safe = re.sub(r"[^a-zA-Z0-9一-龥]+", "-", stem).strip("-")
    return safe[:60] or "seed"


def _build_seed_filename(category: str, title: str) -> str:
    """印(y)卡命名: y_<category>_<date>_<ts>-<safe>.md — 与碑(b) 区分"""
    now = datetime.now()
    date_part = now.strftime("%Y-%m-%d")
    ts_part = str(int(now.timestamp() * 1000))
    safe = re.sub(r"[^a-zA-Z0-9一-龥]+", "-", title).strip("-")[:60] or "seed"
    return f"y_{category}_{date_part}_{ts_part}-{safe}.md"


def _build_full_filename(category: str, title: str) -> str:
    """碑(b)卡命名: b_<category>_<date>_<ts>-<safe>.md — 与 v3_import_seed (y_) 区分"""
    now = datetime.now()
    date_part = now.strftime("%Y-%m-%d")
    ts_part = str(int(now.timestamp() * 1000))
    safe = re.sub(r"[^a-zA-Z0-9一-龥]+", "-", title).strip("-")[:60] or "imported"
    return f"b_{category}_{date_part}_{ts_part}-{safe}.md"


def _discover_files(source_path: str, exts):
    """发现 source_path 下的所有匹配扩展名的文件

    source_path 是文件 -> 单元素列表
    source_path 是目录 -> 递归 glob 所有 exts
    其他 -> 空列表
    """
    p = Path(source_path)
    if p.is_file():
        return [str(p.resolve())]
    if p.is_dir():
        out = []
        for ext in exts:
            out.extend(str(x.resolve()) for x in p.rglob(f"*{ext}"))
        seen = set()
        uniq = []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq
    return []


def _parse_md_file(path: str):
    """md: 整文件作为一个 card; title 从文件名取; category 从 frontmatter 抓"""
    try:
        p = Path(path)
        text = p.read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.strip():
        return None
    title = _safe_title_from_path(path)
    category = None
    try:
        parts = text.split("---", 2)
        if len(parts) >= 2:
            for line in parts[1].splitlines():
                line = line.strip()
                if line.startswith("category:"):
                    category = line.split(":", 1)[1].strip().strip('"').strip("'") or None
                    break
    except Exception:
        pass
    return {
        "title": title,
        "content": text,
        "category": category,
        "tags": ["import_full", "md"],
        "_source_file": path,
    }


def _parse_jsonl_file(path: str):
    """jsonl: 逐行 JSON 解析, 每行一个对象

    支持字段: title (缺省从 content 抓首行), content, category (缺省 None),
    tags (缺省 ['import_full', 'jsonl']).
    解析失败的行跳过。
    """
    out = []
    parse_errors = 0
    try:
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    parse_errors += 1
                    continue
                if not isinstance(obj, dict):
                    parse_errors += 1
                    continue
                content = str(obj.get("content") or obj.get("text") or "")
                if not content.strip():
                    parse_errors += 1
                    continue
                title = str(obj.get("title") or "").strip()
                if not title:
                    first = content.splitlines()[0].strip() if content else ""
                    title = first.lstrip("#").strip()[:60] or f"jsonl-line-{lineno}"
                category = obj.get("category")
                if category is not None:
                    category = str(category).strip() or None
                tags = obj.get("tags") or ["import_full", "jsonl"]
                if not isinstance(tags, list):
                    tags = ["import_full", "jsonl"]
                out.append({
                    "title": title[:80],
                    "content": content,
                    "category": category,
                    "tags": list(tags) + ["import_full"],
                    "_source_file": path,
                    "_source_line": lineno,
                })
    except Exception as e:
        logger.warning("jsonl 读失败 (%s): %s", path, _safe_err(e)[:120])
        return []
    if parse_errors:
        logger.info("jsonl %s 有 %d 行解析失败", path, parse_errors)
    return out


_IMPORT_INJECTION_PATTERN = re.compile(
    r'^\s*\[?(?:\d{1,2}\s+\w{3}\s+\d{4}|\w{3}\s+\d{1,2}(?:st|nd|rd|th)?\s+\d{4}|'
    r'\w{3}\s+\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2})?'
    r'[^\]]*\]?\s*\[(?:IMPORTANT|ASYNC DELEGATION|Subagent Context|OUT-OF-BAND|【IMPORTANT|【ASYNC)'
)
_IMPORT_INJECTION_PREFIXES = (
    "[IMPORTANT:", "[ASYNC DELEGATION", "[Subagent Context]", "[OUT-OF-BAND",
    "[Inter-session message]",
)
# 系统噪音（非对话内容, 直接过滤）
_IMPORT_NOISE_SUBSTRINGS = (
    "maximum number of tool-calling iterations",
)


def _is_importable_qa(question: str, answer: str) -> bool:
    """导入层二次过滤，避免旧库中的控制消息和无正文回合进入消息河。"""
    q = (question or "").strip()
    a = (answer or "").strip()
    if len(q) < 5 or len(a) < 5:
        return False
    # Provider/token limits belong to derived embeddings. Importing a normal
    # user/assistant pair must preserve complete source text, including >24K.
    q_lower = q.lower()
    if q_lower.startswith("heartbeat") or q_lower.startswith("system"):
        return False
    if _IMPORT_INJECTION_PATTERN.match(q) or q.startswith(_IMPORT_INJECTION_PREFIXES):
        return False
    # 系统噪音子串（question/answer 任一命中即过滤）
    for _noise in _IMPORT_NOISE_SUBSTRINGS:
        if _noise in q_lower or _noise in a.lower():
            return False
    # adapter 用该占位符表示只有工具调用、没有可供召回的最终回答。
    if a == "(tool call only)":
        return False
    return True


def _timestamp_to_datetime(value):
    """Hermes 的 Unix 秒时间戳转换为 PG timestamptz 可接受的 UTC datetime。"""
    if value is None:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc)


def _existing_source_ids(conn, source_ids: list[str], batch_size: int = 500) -> set[str]:
    """按批查询已导入 source_id，避免一次 SQL 参数过多。"""
    existing: set[str] = set()
    if conn is None or not source_ids:
        return existing
    with conn.cursor() as cur:
        for start in range(0, len(source_ids), batch_size):
            batch = source_ids[start:start + batch_size]
            placeholders = ",".join(["%s"] * len(batch))
            cur.execute(
                f"SELECT source_id FROM qa_pairs WHERE source_id IN ({placeholders})",
                batch,
            )
            existing.update(row[0] for row in cur.fetchall())
    return existing


def _existing_content_keys(conn, qa_pairs: list[dict], batch_size: int = 500) -> set[str]:
    """内容级去重: 按 (question, answer) 全文匹配查现有库（跨 source 去重）。

    state.db 与 experiment/live_sync 时间重叠（6/8-7/29），source_id 不同但内容相同，
    全量导入必须按内容跳过，否则同一对话存两份。
    """
    existing: set[str] = set()
    if conn is None or not qa_pairs:
        return existing
    with conn.cursor() as cur:
        for start in range(0, len(qa_pairs), batch_size):
            batch = qa_pairs[start:start + batch_size]
            placeholders = ",".join(["%s"] * len(batch))
            cur.execute(
                f"SELECT question, answer FROM qa_pairs "
                f"WHERE (question, answer) IN ({placeholders})",
                [ (c["question"], c["answer"]) for c in batch ],
            )
            for row in cur.fetchall():
                existing.add(f"{row[0]}\x00{row[1]}")
    return existing


def _import_hermes_state(source_path, max_cards, dry_run, core, pg, embed_cfg):
    """state.db → ConversationTurn → QA + embedding → PG 消息河。

    max_cards 表示最多处理的有效 QA 对数；重复项也占该上限，确保同参数重跑
    检查的是同一批 source_id，而不会越过重复项继续导入下一批。
    """
    from ..adapter_hermes_state import _iter_turns_from_messages
    from ..embedding import call_embedding

    stats = {
        "parsed": 0,
        "filtered": 0,
        "paired": 0,
        "embedded": 0,
        "written": 0,
        "skipped_dup": 0,
        "errors": 0,
    }
    candidates = []

    # URI read-only 模式，避免误改正在使用的 Hermes state.db。
    db_uri = Path(source_path).resolve().as_uri() + "?mode=ro"
    state_conn = sqlite3.connect(db_uri, uri=True)
    try:
        sessions = state_conn.execute(
            """
            SELECT DISTINCT s.id
            FROM sessions s
            JOIN messages m ON m.session_id = s.id
            WHERE m.role = 'user' AND m.active = 1
            ORDER BY s.started_at, s.id
            """
        )
        for (session_id,) in sessions:
            # adapter 不暴露原始消息 id；按原始时间戳恢复 user message id。
            user_ids = {}
            for message_id, timestamp in state_conn.execute(
                """
                SELECT id, timestamp FROM messages
                WHERE session_id = ? AND role = 'user' AND active = 1
                ORDER BY id
                """,
                (session_id,),
            ):
                user_ids.setdefault(timestamp, []).append(message_id)

            for turn in _iter_turns_from_messages(state_conn, session_id):
                stats["parsed"] += 1
                question = turn.user.content.strip() if turn.user else ""
                answer = turn.assistant.content.strip() if turn.assistant else ""
                if not _is_importable_qa(question, answer):
                    stats["filtered"] += 1
                    continue

                ids = user_ids.get(turn.user.timestamp if turn.user else None) or []
                if not ids:
                    stats["errors"] += 1
                    logger.warning(
                        "hermes_state 无法恢复原始 user id (session=%s turn=%s)",
                        session_id, turn.turn_id,
                    )
                    continue
                message_id = ids.pop(0)
                candidates.append({
                    "source_id": f"qa_import/state/{message_id}",
                    "session_id": session_id,
                    "turn_id": str(turn.session.turn_index if turn.session else 0),
                    "turn_index": int(turn.session.turn_index if turn.session else 0),
                    "question": question,
                    "answer": answer,
                    "timestamp": _timestamp_to_datetime(turn.user.timestamp if turn.user else None),
                    "tool_calls": turn.assistant.tool_calls if turn.assistant else None,
                })
                stats["paired"] += 1
                if stats["paired"] >= max_cards:
                    break
            if stats["paired"] >= max_cards:
                break
    finally:
        state_conn.close()

    # 写库 + 去重 抽到 _write_candidates 供所有 source 复用
    write_stats = _write_candidates(
        pg_conn=pg._connect() if pg is not None else None,
        candidates=candidates,
        dry_run=dry_run,
        embed_cfg=embed_cfg,
        trigger="hermes_state",
        source_label="hermes_state",
    )
    stats["skipped_dup"] = write_stats["skipped_dup"]
    stats["written"] = write_stats["written"]
    stats["errors"] += write_stats["errors"]
    stats["embedded"] = write_stats["embedded"]
    return stats


def _write_candidates(pg_conn, candidates, dry_run, embed_cfg, trigger, source_label):
    """把 candidates 列表写入 PG：去重 → embedding → qa_pairs + conversation_stream。

    供 hermes_state / chat_md / md / trajectory 四个 source 共用。
    candidates 每条必须包含: source_id, session_id, turn_id, turn_index, question,
    answer, timestamp (datetime), tool_calls。
    trigger 写入 conversation_stream.trigger 字段 (按 source 区分, 便于回溯)。
    source_label 用于日志前缀。
    """
    # 内存级去重: 同批 candidates 内部的 (question, answer) 重复先剔除
    # （_existing_content_keys 只查库, 同批未写入的互相重复查不到）
    _seen: set[str] = set()
    _deduped: list[dict] = []
    for c in candidates:
        _k = f"{(c.get('question') or '').strip()}\x00{(c.get('answer') or '').strip()}"
        if _k in _seen:
            continue
        _seen.add(_k)
        _deduped.append(c)
    candidates = _deduped
    # 统一按 timestamp 排序 — id 顺序 = 时间流顺序（压缩引擎的基石）
    # trajectory 等源按文件名/UUID 遍历, 文件间时间乱序, 必须在此排序
    candidates.sort(key=lambda c: c.get("timestamp") or datetime.min)
    from ..embedding import call_embedding

    stats = {"written": 0, "skipped_dup": 0, "errors": 0, "embedded": 0}
    if not dry_run and pg_conn is None:
        raise RuntimeError(
            f"PG unavailable, {source_label} 导入无法写 conversation_stream/qa_pairs"
        )

    existing = _existing_source_ids(
        pg_conn, [c["source_id"] for c in candidates]
    )
    # 内容级去重 (跨 source): state.db / experiment/live_sync 内容重叠时跳过
    content_existing = _existing_content_keys(pg_conn, candidates)
    old_autocommit = getattr(pg_conn, "autocommit", None) if pg_conn is not None else None
    if pg_conn is not None and not dry_run:
        pg_conn.autocommit = False

    cur = pg_conn.cursor() if pg_conn is not None and not dry_run else None
    pending_writes = 0
    try:
        for candidate in candidates:
            source_id = candidate["source_id"]
            if source_id in existing:
                stats["skipped_dup"] += 1
                continue
            # 内容级去重: 与现有任意 source 的 (question, answer) 完全一致 → 跳过
            content_key = f"{candidate['question']}\x00{candidate['answer']}"
            if content_key in content_existing:
                stats["skipped_dup"] += 1
                continue

            try:
                if dry_run:
                    # dry-run 只统计配对, 不调 embedding API (避免全量统计跑数小时)
                    stats["embedded"] += 1
                    continue
                embedding = call_embedding(
                    candidate["question"][:1000], embed_cfg, cache=True,
                    timeout=10, retries=2,
                )
                if not embedding:
                    raise RuntimeError("embedding 返回空向量")
                stats["embedded"] += 1
            except ValueError:
                raise
            except Exception as exc:
                stats["errors"] += 1
                logger.warning(
                    "%s embedding 失败 (%s): %s",
                    source_label, source_id, _safe_err(exc)[:160],
                )
                continue

            if dry_run:
                continue

            emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
            tool_calls = json.dumps(candidate["tool_calls"] or [], ensure_ascii=False)
            savepoint = f"import_{source_label}_{stats['written'] + stats['errors']}"
            try:
                cur.execute(f"SAVEPOINT {savepoint}")
                # QA 先写；若并发导入已占 source_id，则不再写两条消息河记录。
                cur.execute(
                    """
                    INSERT INTO qa_pairs
                        (source_id, session_id, turn_id, question, answer,
                         tool_calls, tool_results, timestamp, source, embedding, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, '[]'::jsonb,
                            %s, 'import', %s::vector, NOW())
                    ON CONFLICT (source_id) DO NOTHING
                    """,
                    (
                        source_id, candidate["session_id"], candidate["turn_id"],
                        candidate["question"], candidate["answer"], tool_calls,
                        candidate["timestamp"], emb_str,
                    ),
                )
                if cur.rowcount == 0:
                    stats["skipped_dup"] += 1
                    cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                    continue

                cur.execute(
                    """
                    INSERT INTO conversation_stream
                        (session_id, role, content, trigger, turn_id, timestamp,
                         source, tool_calls, tool_results)
                    VALUES (%s, 'user', %s, %s, %s, %s,
                            'import', '[]'::jsonb, '[]'::jsonb)
                    """,
                    (
                        candidate["session_id"], candidate["question"],
                        trigger, candidate["turn_index"], candidate["timestamp"],
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO conversation_stream
                        (session_id, role, content, trigger, turn_id, timestamp,
                         source, tool_calls, tool_results)
                    VALUES (%s, 'assistant', %s, %s, %s, %s,
                            'import', %s::jsonb, '[]'::jsonb)
                    """,
                    (
                        candidate["session_id"], candidate["answer"],
                        trigger, candidate["turn_index"], candidate["timestamp"],
                        tool_calls,
                    ),
                )
                cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                stats["written"] += 1
                pending_writes += 1
                if pending_writes >= 500:
                    pg_conn.commit()
                    pending_writes = 0
                    logger.info(
                        "%s 导入进度: written=%d dup=%d errors=%d",
                        source_label, stats["written"], stats["skipped_dup"], stats["errors"],
                    )
            except Exception as exc:
                stats["errors"] += 1
                logger.warning(
                    "%s PG 写入失败 (%s): %s",
                    source_label, source_id, _safe_err(exc)[:160],
                )
                try:
                    cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                except Exception:
                    pg_conn.rollback()
                    pending_writes = 0

        if pg_conn is not None and not dry_run:
            pg_conn.commit()
    finally:
        if cur is not None:
            cur.close()
        if pg_conn is not None and not dry_run and old_autocommit is not None:
            pg_conn.autocommit = old_autocommit
    return stats


def _turn_to_candidate(turn, source_tag, source_path_label):
    """把 ConversationTurn 转成 candidate dict (chat_md / md / trajectory 共用)。

    source_tag ∈ {"chat_md", "md", "trajectory"} — 用于 source_id 前缀和 trigger 字段。
    source_path_label 用于日志标识 (不影响 source_id 唯一性)。
    配对规则 (与 adapter 设计匹配):
      • user+assistant 都在 → 成 1 个 QA
      • 只 user          → 跳过 (_is_importable_qa 要求 answer>=5)
      • 只 assistant     → 用 assistant 当 question, 空 answer 兜底, 但 _is_importable_qa
                            会过滤掉 (answer 太短)。仍生成 candidate 但走 importable 过滤。
    """
    question = (turn.user.content if turn.user else "") or ""
    answer = (turn.assistant.content if turn.assistant else "") or ""

    user_ts = turn.user.timestamp if turn.user else None
    asst_ts = turn.assistant.timestamp if turn.assistant else None
    # 优先 user 时间戳 (用户提问 → 助手响应的时间线以 user 为准)
    pick_ts = user_ts if user_ts is not None else asst_ts

    session_id = turn.session.id if turn.session else f"{source_tag}_unknown"
    return {
        "source_id": f"qa_import/{source_tag}/{turn.turn_id}",
        "session_id": session_id,
        "turn_id": str(turn.session.turn_index if turn.session else 0),
        "turn_index": int(turn.session.turn_index if turn.session else 0),
        "question": question.strip(),
        "answer": answer.strip(),
        "timestamp": _timestamp_to_datetime(pick_ts),
        "tool_calls": turn.assistant.tool_calls if turn.assistant else None,
    }


def _pair_md_turns(turns, source_tag):
    """把 chat_md / md adapter 产出的逐行 turn 配对成 user+assistant 完整回合。

    adapter_chat_md 与 adapter_md 是按 markdown 行 yield ConversationTurn 的, 每行要么
    是 user-only 要么是 assistant-only。本函数把它们配成 (user, assistant) 对, 与
    hermes_state adapter 的回合语义对齐 (这样 _is_importable_qa 就能正确判定)。

    配对规则:
      • 按 session_id 分组, 每组内按 turn_idx 顺序配对。
      • user 后跟着 assistant (直到下一个 user 或 session 结束) → 合并为 1 个 turn。
      • 孤儿 user (没有 assistant 跟进) → 跳过 (无法形成 QA)。
      • 孤儿 assistant (session 开头或 user 之间没有 user) → 跳过 (无法形成 QA)。

    适配 hermes_state/trajectory adapter 已经配对的 turn — 本函数对那些已经
    同时有 user + assistant 的 turn 直接保留 (跳过配对步骤)。
    """
    from ..turn_schema import (
        ConversationTurn, SessionInfo, AssistantMessage, UserMessage, Flags,
    )

    # 先把 turn 按 session_id 分组
    by_session: dict[str, list] = {}
    for turn in turns:
        sid = turn.session.id if turn.session else f"{source_tag}_unknown"
        by_session.setdefault(sid, []).append(turn)

    paired: list[ConversationTurn] = []
    for sid, group in by_session.items():
        i = 0
        idx = 0
        while i < len(group):
            turn = group[i]
            # 已经配对的 turn (trajectory adapter 风格) → 直接保留
            if turn.user is not None and turn.assistant is not None:
                paired.append(turn)
                i += 1
                continue
            # user-only → 找后续 assistant (同 session)
            if turn.user is not None:
                user_msg = turn.user
                # 收集从 i+1 开始, 直到下一个 user 或末尾的所有 assistant 内容
                asst_parts: list[str] = []
                asst_ts = None
                asst_tool_calls = None
                asst_model = None
                asst_provider = None
                j = i + 1
                while j < len(group) and group[j].user is None:
                    a_turn = group[j]
                    if a_turn.assistant:
                        content = (a_turn.assistant.content or "").strip()
                        if content:
                            asst_parts.append(content[:1000])
                        if asst_ts is None and a_turn.assistant.timestamp is not None:
                            asst_ts = a_turn.assistant.timestamp
                        if asst_tool_calls is None and a_turn.assistant.tool_calls:
                            asst_tool_calls = a_turn.assistant.tool_calls
                        if asst_model is None and a_turn.assistant.model:
                            asst_model = a_turn.assistant.model
                        if asst_provider is None and a_turn.assistant.provider:
                            asst_provider = a_turn.assistant.provider
                    j += 1
                if asst_parts:
                    idx += 1
                    asst_combined = "\n".join(asst_parts)
                    user_ts = user_msg.timestamp
                    paired.append(ConversationTurn(
                        turn_id=f"{sid}_paired_{idx:04d}",
                        source=source_tag,
                        session=SessionInfo(id=sid, turn_index=idx - 1),
                        user=UserMessage(
                            content=user_msg.content,
                            timestamp=user_ts,
                        ),
                        assistant=AssistantMessage(
                            content=asst_combined,
                            timestamp=asst_ts if asst_ts is not None else user_ts,
                            model=asst_model,
                            provider=asst_provider,
                            tool_calls=asst_tool_calls,
                        ),
                        flags=Flags(),
                    ))
                i = j
                continue
            # assistant-only 且前面没 user (孤儿) → 跳过
            i += 1
    return paired


def _import_chat_md(source_path, max_cards, dry_run, core, pg, embed_cfg):
    """chat_*.md → ConversationTurn → QA + embedding → PG 消息河。

    source_path 可以是单个 .md 文件, 也可以是目录 (glob chat_*.md 按文件名日期排序)。
    """
    from .. import adapter_chat_md

    stats = {
        "parsed": 0, "filtered": 0, "paired": 0,
        "embedded": 0, "written": 0, "skipped_dup": 0, "errors": 0,
    }
    candidates: list[dict] = []
    p = Path(source_path)
    if p.is_file():
        md_files = [p]
    elif p.is_dir():
        md_files = sorted(p.glob("chat_*.md"))
    else:
        md_files = []
    for fp in md_files:
        try:
            raw_turns = adapter_chat_md.extract(fp)
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("chat_md 解析失败 (%s): %s", fp, _safe_err(exc)[:160])
            continue
        # chat_md adapter 是按行 yield turn 的, 需配对
        turns = _pair_md_turns(raw_turns, "chat_md")
        for turn in turns:
            stats["parsed"] += 1
            candidate = _turn_to_candidate(turn, "chat_md", str(fp))
            if not _is_importable_qa(candidate["question"], candidate["answer"]):
                stats["filtered"] += 1
                continue
            candidates.append(candidate)
            stats["paired"] += 1
            if stats["paired"] >= max_cards:
                break
        if stats["paired"] >= max_cards:
            break

    write_stats = _write_candidates(
        pg_conn=pg._connect() if pg is not None else None,
        candidates=candidates,
        dry_run=dry_run,
        embed_cfg=embed_cfg,
        trigger="chat_md",
        source_label="chat_md",
    )
    stats["written"] = write_stats["written"]
    stats["skipped_dup"] = write_stats["skipped_dup"]
    stats["embedded"] = write_stats["embedded"]
    stats["errors"] += write_stats["errors"]
    return stats


def _import_md(source_path, max_cards, dry_run, core, pg, embed_cfg):
    """单个 .md (对话记录完整版_*.md) → ConversationTurn → QA + embedding → PG 消息河。

    source_path 是单个 .md 文件 (含多个 ## YYYY-MM-DD 段)。不接目录 (与 adapter_md
    extract 单文件语义匹配 — adapter_md 不提供 extract_dir)。
    """
    from .. import adapter_md

    stats = {
        "parsed": 0, "filtered": 0, "paired": 0,
        "embedded": 0, "written": 0, "skipped_dup": 0, "errors": 0,
    }
    candidates: list[dict] = []
    p = Path(source_path)
    if not p.is_file():
        stats["errors"] += 1
        logger.warning("md 源不是文件: %s", source_path)
    else:
        try:
            raw_turns = adapter_md.extract(p)
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("md 解析失败 (%s): %s", source_path, _safe_err(exc)[:160])
            raw_turns = []
        # md adapter 也是按行 yield turn 的, 需配对
        turns = _pair_md_turns(raw_turns, "md")
        for turn in turns:
            stats["parsed"] += 1
            candidate = _turn_to_candidate(turn, "md", source_path)
            if not _is_importable_qa(candidate["question"], candidate["answer"]):
                stats["filtered"] += 1
                continue
            candidates.append(candidate)
            stats["paired"] += 1
            if stats["paired"] >= max_cards:
                break

    write_stats = _write_candidates(
        pg_conn=pg._connect() if pg is not None else None,
        candidates=candidates,
        dry_run=dry_run,
        embed_cfg=embed_cfg,
        trigger="md",
        source_label="md",
    )
    stats["written"] = write_stats["written"]
    stats["skipped_dup"] = write_stats["skipped_dup"]
    stats["embedded"] = write_stats["embedded"]
    stats["errors"] += write_stats["errors"]
    return stats


def _import_trajectory(source_path, max_cards, dry_run, core, pg, embed_cfg):
    """trajectory.jsonl 目录 → ConversationTurn → QA + embedding → PG 消息河。

    source_path 是目录, 遍历 *.trajectory.jsonl + *.jsonl, 跳过 *-path.json 与
    *.deleted.* 副本 (filter 委托给 adapter_trajectory.extract_dir)。
    trajectory adapter 已经配对好 (user prompt + assistant response), 直接转 candidate。
    """
    from .. import adapter_trajectory

    stats = {
        "parsed": 0, "filtered": 0, "paired": 0,
        "embedded": 0, "written": 0, "skipped_dup": 0, "errors": 0,
    }
    candidates: list[dict] = []
    p = Path(source_path)
    if not p.is_dir():
        stats["errors"] += 1
        logger.warning("trajectory 源不是目录: %s", source_path)
    else:
        try:
            turns = adapter_trajectory.extract_dir(p)
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("trajectory 目录解析失败 (%s): %s", source_path, _safe_err(exc)[:160])
            turns = []
        for turn in turns:
            stats["parsed"] += 1
            candidate = _turn_to_candidate(turn, "trajectory", str(p))
            if not _is_importable_qa(candidate["question"], candidate["answer"]):
                stats["filtered"] += 1
                continue
            candidates.append(candidate)
            stats["paired"] += 1
            if stats["paired"] >= max_cards:
                break

    write_stats = _write_candidates(
        pg_conn=pg._connect() if pg is not None else None,
        candidates=candidates,
        dry_run=dry_run,
        embed_cfg=embed_cfg,
        trigger="trajectory",
        source_label="trajectory",
    )
    stats["written"] = write_stats["written"]
    stats["skipped_dup"] = write_stats["skipped_dup"]
    stats["embedded"] = write_stats["embedded"]
    stats["errors"] += write_stats["errors"]
    return stats


def handle_v3_import_seed(args: dict, **kw) -> str:
    """导入种子卡 — 读文件 → 写 y/<category>/<file>.md → 可选写 PG v3_cards

    不调 LLM: 整文件正文直接落盘, title 取自文件名, tags 默认 ['seed','import'].
    已存在的目标文件不会被覆盖 (write_card 写入新文件名, 时间戳不同 → 不冲突).
    """
    try:
        if kw.get("runtime_context"):
            return json.dumps({
                "success": False,
                "error": "import_seed is offline/migration-only and cannot run from a Runtime-backed provider",
            }, ensure_ascii=False)
        memory_files = args.get("memory_files") or []
        if not isinstance(memory_files, list) or not memory_files:
            return json.dumps(
                {"success": False, "error": "memory_files 必填且非空 (list[str])"},
                ensure_ascii=False,
            )

        category = (args.get("category") or "memory").strip() or "memory"
        extra_tags = args.get("tags") or []
        if not isinstance(extra_tags, list):
            extra_tags = []
        with_pg = bool(args.get("with_pg", True))

        from .. import V3Core
        from ..embedding import call_embedding, safe_embed_cfg

        core = V3Core()
        cfg = core.config
        embed_cfg = safe_embed_cfg(cfg)

        results = []
        ok_count = 0
        err_count = 0
        skip_count = 0

        for fpath in memory_files:
            entry = {"file": fpath}
            try:
                p = Path(fpath)
                if not p.is_file():
                    entry["success"] = False
                    entry["error"] = f"file not found: {fpath}"
                    err_count += 1
                    results.append(entry)
                    continue

                text = p.read_text(encoding="utf-8")
                if not text.strip():
                    entry["success"] = False
                    entry["error"] = "file is empty"
                    skip_count += 1
                    results.append(entry)
                    continue

                title = _safe_title_from_path(fpath)
                filename = _build_seed_filename(category, title)
                tags = list(dict.fromkeys(["seed", "import", *extra_tags]))

                # 1) 写文件 (cards/<category>/<file>.md)
                card_path = core.store.write_card(
                    category,
                    filename,
                    title,
                    text,
                    tags,
                    source="seed_import",
                    source_file=str(p.resolve()),
                )
                entry["path"] = card_path
                entry["title"] = title
                entry["category"] = category
                entry["tags"] = tags
                entry["bytes"] = len(text.encode("utf-8"))

                # 2) 可选写 PG v3_cards
                entry["pg_written"] = False
                if with_pg:
                    try:
                        pg = core.pg
                        if pg and pg.is_connected():
                            source_id = f"y/{category}/{filename}"
                            emb = None
                            if embed_cfg is not None:
                                try:
                                    emb = call_embedding(text[:2000], embed_cfg, cache=True)
                                except ValueError:
                                    raise
                                except Exception as e:
                                    logger.warning("seed import embedding 失败 (%s): %s", source_id, _safe_err(e)[:120])
                            pg.insert_card(
                                source_id=source_id,
                                title=title,
                                content=text,
                                category=category,
                                tags=tags,
                                embedding=emb,
                                embed_cfg=embed_cfg,
                            )
                            entry["pg_written"] = True
                            entry["pg_source_id"] = source_id
                    except ValueError:
                        raise
                    except Exception as e:
                        entry["pg_error"] = f"PG 写入跳过: {_safe_err(e)[:160]}"
                        logger.warning("seed import pg 失败 (%s): %s", fpath, _safe_err(e)[:200])

                entry["success"] = True
                ok_count += 1
            except ValueError:
                raise
            except Exception as e:
                entry["success"] = False
                entry["error"] = _safe_err(e)
                err_count += 1
                logger.exception("seed import 失败: %s", fpath)
            results.append(entry)

        return json.dumps({
            "success": err_count == 0,
            "tool": "v3_import_seed",
            "totals": {
                "requested": len(memory_files),
                "imported": ok_count,
                "skipped": skip_count,
                "errors": err_count,
            },
            "with_pg": with_pg,
            "category": category,
            "results": results,
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("handle_v3_import_seed 顶层异常")
        return json.dumps(
            {"success": False, "error": _safe_err(e), "tool": "v3_import_seed"},
            ensure_ascii=False,
        )


def handle_v3_import_full(args: dict, **kw) -> str:
    """全量导入基础框架 — 读源文件 -> 解析 -> 写碑(b)卡 -> 可选写 PG

    约束:
      • 不调 LLM (extract / llm 完全不碰)
      • source: 仅 'md' / 'jsonl' 完整实现, 'honcho' / 'yjby' 返 unsupported
      • target: 仅 'b' 实现, 'j' 返 unsupported
      • write=true 才会真正写盘; false/dry_run=true 仅扫描并报告预览
      • max_cards 上限 10000 兜底; 软超时 1000s

    返回 JSON:
      {
        "success": bool,
        "tool": "v3_import_full",
        "source": str,
        "source_path": str,
        "target": "b",
        "write": bool,
        "dry_run": bool,
        "totals": {discovered_files, parsed, truncated, written, skipped, errors},
        "results": [ {source_file, source_line?, success, error?, path?, title?, category?, tags?, bytes?, pg_written?, pg_source_id?} ... ],
        "timed_out": bool,
      }
    """
    t_start = time.monotonic()
    try:
        if kw.get("runtime_context"):
            return json.dumps({
                "success": False,
                "error": "import_full is offline/migration-only and cannot run from a Runtime-backed provider",
            }, ensure_ascii=False)
        # 1. 参数校验
        source = (args.get("source") or "").strip().lower()
        source_path = (args.get("source_path") or "").strip()
        target = (args.get("target") or "b").strip().lower()
        write = bool(args.get("write", False))
        dry_run = bool(args.get("dry_run", True))
        max_cards = int(args.get("max_cards", 100))

        if not source:
            return json.dumps(
                {"success": False, "tool": "v3_import_full", "error": "source 必填 (hermes_state/trajectory/md)"},
                ensure_ascii=False,
            )
        if not source_path:
            return json.dumps(
                {"success": False, "tool": "v3_import_full", "error": "source_path 必填"},
                ensure_ascii=False,
            )
        if target not in ("j", "b"):
            return json.dumps(
                {"success": False, "tool": "v3_import_full", "error": f"target 必须是 j/b, 收到 {target!r}"},
                ensure_ascii=False,
            )
        if max_cards <= 0:
            max_cards = 100
        max_cards = min(max_cards, 10000)

        # 2. target = j 当前不支持
        if target == "j":
            return json.dumps({
                "success": False,
                "tool": "v3_import_full",
                "error": "unsupported_target",
                "message": "target=j (印卡) 当前基础框架未实现, 仅支持 b (碑卡)",
                "source": source,
                "source_path": source_path,
                "target": target,
            }, ensure_ascii=False)

        # 3. source = honcho / yjby / jsonl / chat_md 已从对外 enum 收窄（2026-08-06 开源边界）
        #    代码保留（内部重建可用）— 直接调用时返回明确 unsupported
        if source in ("honcho", "yjby", "jsonl", "chat_md"):
            return json.dumps({
                "success": False,
                "tool": "v3_import_full",
                "error": "unsupported_source",
                "message": f"source={source!r} 已从开源收窄 (仅 hermes_state/trajectory/md 对外)",
                "source": source,
                "source_path": source_path,
                "target": target,
            }, ensure_ascii=False)

        # 4. 校验 source_path 存在性
        src_p = Path(source_path)
        if not src_p.exists():
            return json.dumps({
                "success": False,
                "tool": "v3_import_full",
                "error": "source_path_not_found",
                "message": f"source_path 不存在: {source_path}",
                "source": source,
                "source_path": source_path,
                "target": target,
            }, ensure_ascii=False)

        # hermes_state / chat_md / md (adapter) / trajectory 都走对话流管线。
        # 共用一段 dispatch 逻辑: 构造 core/pg/embed_cfg → 调对应 _import_*
        if source in ("hermes_state", "chat_md", "md", "trajectory"):
            from .. import V3Core
            from ..embedding import safe_embed_cfg

            core = V3Core()
            embed_cfg = safe_embed_cfg(core.config)
            pg = core.pg if core.pg and core.pg.is_connected() else None
            effective_dry_run = dry_run or not write

            _DISPATCH = {
                "hermes_state": _import_hermes_state,
                "chat_md": _import_chat_md,
                "md": _import_md,
                "trajectory": _import_trajectory,
            }
            try:
                stats = _DISPATCH[source](
                    source_path=source_path,
                    max_cards=max_cards,
                    dry_run=effective_dry_run,
                    core=core,
                    pg=pg,
                    embed_cfg=embed_cfg,
                )
                return json.dumps({
                    "success": stats["errors"] == 0,
                    "tool": "v3_import_full",
                    "source": source,
                    "source_path": source_path,
                    "target": target,
                    "write": bool(write and not effective_dry_run),
                    "dry_run": effective_dry_run,
                    "totals": stats,
                    "stats": stats,
                    "timed_out": False,
                }, ensure_ascii=False)
            except Exception as exc:
                logger.exception("%s 导入失败", source)
                return json.dumps({
                    "success": False,
                    "tool": "v3_import_full",
                    "source": source,
                    "source_path": source_path,
                    "target": target,
                    "write": bool(write and not effective_dry_run),
                    "dry_run": effective_dry_run,
                    "error": _safe_err(exc)[:300],
                }, ensure_ascii=False)

        # 5. 发现文件
        if source == "md":
            files = _discover_files(source_path, (".md",))
        elif source == "jsonl":
            files = _discover_files(source_path, (".jsonl",))
        else:
            files = []

        # 6. 解析所有候选 cards
        parsed = []
        for fp in files:
            if source == "md":
                one = _parse_md_file(fp)
                if one:
                    parsed.append(one)
            elif source == "jsonl":
                parsed.extend(_parse_jsonl_file(fp))
            # 软超时检查
            if (time.monotonic() - t_start) > _IMPORT_FULL_DEADLINE_SEC:
                return json.dumps({
                    "success": False,
                    "tool": "v3_import_full",
                    "error": "timeout_during_parse",
                    "message": f"解析阶段超时 (>{_IMPORT_FULL_DEADLINE_SEC:.0f}s)",
                    "source": source,
                    "source_path": source_path,
                    "target": target,
                    "totals": {"discovered_files": len(files), "parsed_so_far": len(parsed)},
                }, ensure_ascii=False)

        # 7. 应用 max_cards
        truncated = len(parsed) > max_cards
        parsed = parsed[:max_cards]

        # 8. dry_run / write=false -> 仅报告
        if dry_run or not write:
            previews = []
            for card in parsed:
                previews.append({
                    "source_file": card.get("_source_file"),
                    "source_line": card.get("_source_line"),
                    "title": card["title"],
                    "category": card.get("category"),
                    "tags": card.get("tags", []),
                    "bytes": len(card["content"].encode("utf-8")),
                    "preview": card["content"][:160],
                })
            return json.dumps({
                "success": True,
                "tool": "v3_import_full",
                "source": source,
                "source_path": source_path,
                "target": target,
                "write": write,
                "dry_run": True,
                "totals": {
                    "discovered_files": len(files),
                    "parsed": len(parsed),
                    "truncated": truncated,
                    "written": 0,
                    "skipped": 0,
                    "errors": 0,
                },
                "results": previews,
            }, ensure_ascii=False)

        # 9. 真正写盘
        from .. import V3Core

        core = V3Core()
        results = []
        ok_count = 0
        err_count = 0
        skip_count = 0

        pg = core.pg if core.pg and core.pg.is_connected() else None
        pg_note = None
        if pg is None:
            pg_note = "PG unavailable, 仅写盘"

        for card in parsed:
            entry = {
                "source_file": card.get("_source_file"),
                "source_line": card.get("_source_line"),
            }
            try:
                title = card["title"]
                content = card["content"]
                category = (card.get("category") or "memory").strip() or "memory"
                tags = list(dict.fromkeys(card.get("tags") or [] + ["import_full"]))

                filename = _build_full_filename(category, title)

                card_path = core.store.write_card(
                    category,
                    filename,
                    title,
                    content,
                    tags,
                    source=f"import_full_{source}",
                    source_file=card.get("_source_file") or source_path,
                )
                entry["path"] = card_path
                entry["title"] = title
                entry["category"] = category
                entry["tags"] = tags
                entry["bytes"] = len(content.encode("utf-8"))

                entry["pg_written"] = False
                if pg is not None:
                    try:
                        source_id = f"b/{category}/{filename}"
                        pg.insert_card(
                            source_id=source_id,
                            title=title,
                            content=content,
                            category=category,
                            tags=tags,
                            embedding=None,
                        )
                        entry["pg_written"] = True
                        entry["pg_source_id"] = source_id
                    except Exception as e:
                        entry["pg_error"] = f"PG 写入跳过: {_safe_err(e)[:160]}"
                        logger.warning("import_full pg 失败 (%s): %s", filename, _safe_err(e)[:200])

                entry["success"] = True
                ok_count += 1
            except Exception as e:
                entry["success"] = False
                entry["error"] = _safe_err(e)[:300]
                err_count += 1
                logger.exception("import_full 写卡失败: %s", card.get("_source_file"))
            results.append(entry)

            # 软超时检查 (每条后)
            if (time.monotonic() - t_start) > _IMPORT_FULL_DEADLINE_SEC:
                return json.dumps({
                    "success": False,
                    "tool": "v3_import_full",
                    "error": "timeout_during_write",
                    "message": f"写入阶段超时 (>{_IMPORT_FULL_DEADLINE_SEC:.0f}s), 已处理 {ok_count + err_count} 条",
                    "source": source,
                    "source_path": source_path,
                    "target": target,
                    "write": True,
                    "dry_run": False,
                    "totals": {
                        "discovered_files": len(files),
                        "parsed": len(parsed),
                        "truncated": truncated,
                        "written": ok_count,
                        "skipped": skip_count,
                        "errors": err_count,
                    },
                    "results": results,
                    "timed_out": True,
                }, ensure_ascii=False)

        out = {
            "success": err_count == 0,
            "tool": "v3_import_full",
            "source": source,
            "source_path": source_path,
            "target": target,
            "write": True,
            "dry_run": False,
            "totals": {
                "discovered_files": len(files),
                "parsed": len(parsed),
                "truncated": truncated,
                "written": ok_count,
                "skipped": skip_count,
                "errors": err_count,
            },
            "results": results,
            "timed_out": False,
        }
        if pg_note:
            out["pg_note"] = pg_note
        return json.dumps(out, ensure_ascii=False)
    except Exception as e:
        logger.exception("handle_v3_import_full 顶层异常")
        return json.dumps(
            {"success": False, "error": _safe_err(e), "tool": "v3_import_full"},
            ensure_ascii=False,
        )
