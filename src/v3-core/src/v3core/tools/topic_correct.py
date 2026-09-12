"""v3_topic_correct — 主题卡手动校正工具

动作：
- edit:     编辑标题/摘要/正文（已有 v2_topic_edit 功能）
- merge:    将源主题合并到目标主题
- add_entry: 从对话消息添加条目到主题
- delete:   删除主题及关联条目

所有操作自动同步到 v3_cards.db
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime


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

logger = logging.getLogger("v3core.tools.topic_correct")

V3_TOPIC_CORRECT_SCHEMA = {
    "name": "v3_topic_correct",
    "description": "手动校正主题卡 — 编辑/合并/添加条目/删除/创建。对话中发现主题提炼不准确、需要合并相似主题、或者想把当前对话内容归入某个主题时使用。",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["edit", "merge", "add_entry", "delete", "create"],
                "description": "操作类型: edit=编辑字段, merge=合并主题, add_entry=添加条目, delete=删除主题, create=新建主题",
            },
            "topic_id": {
                "type": "string",
                "description": "目标主题 ID（如 't_2287cc5e'）。edit/add_entry/delete 必填，merge 时为目标主题；create 时不需要",
            },
            "field": {
                "type": "string",
                "enum": ["title", "summary", "body"],
                "description": "(edit 必填) 要编辑的字段",
            },
            "new_content": {
                "type": "string",
                "description": "(edit 必填) 新的字段内容",
            },
            "source_topic_id": {
                "type": "string",
                "description": "(merge 必填) 要合并到目标主题的源主题 ID，合并后源主题被删除",
            },
            "title": {
                "type": "string",
                "description": "(create 必填) 主题标题",
            },
            "summary": {
                "type": "string",
                "description": "(create 必填) 主题摘要",
            },
            "body": {
                "type": "string",
                "description": "(create 可选) 主题正文",
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "(create 可选) 主题关键词",
            },
            "entry": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "用户提问内容"},
                    "answer": {"type": "string", "description": "助手回复内容"},
                    "source": {"type": "string", "description": "来源标识，如 session_id", "default": ""},
                    "timestamp": {"type": "string", "description": "时间戳 ISO 格式", "default": ""},
                },
                "description": "(add_entry 必填) 要添加的对话条目",
            },
        },
        "required": ["action"],
    },
}


def _get_store():
    from ..config import _resolve_data_dir
    from ..topic_store import TopicStore
    db_path = str(_resolve_data_dir() / 'v3_topic_full.db')
    return TopicStore(db_path)


def _sync_to_cards(topic_id: str) -> None:
    """同步 topic_block 到 v3_cards.db"""
    try:
        import sqlite3 as _s3
        from ..config import _resolve_data_dir
        
        store = _get_store()
        row = store.conn.execute(
            "SELECT id, title, body, summary FROM topic_blocks WHERE id=?", (topic_id,)
        ).fetchone()
        if not row or not row[2] or len(row[2].strip()) < 50:
            store.close()
            return
        
        body = row[2]
        cards_db = str(_resolve_data_dir() / 'v3_cards.db')
        conn = _s3.connect(cards_db)
        sid = f'topic_{topic_id}'
        now = datetime.now().isoformat()
        
        existing = conn.execute("SELECT source_id FROM cards WHERE source_id=?", (sid,)).fetchone()
        if existing:
            conn.execute("UPDATE cards SET content=?, updated_at=? WHERE source_id=?", (body, now, sid))
        else:
            conn.execute("""INSERT INTO cards (source_id,category,title,content,source,created_at,updated_at)
                VALUES (?, 'topic', ?, ?, 'topic_pipeline', ?, ?)""", (sid, row[1], body, now, now))
        conn.commit()
        conn.close()
        store.close()
    except Exception:
        pass


def _edit(args: dict) -> dict:
    """编辑主题字段 — 复用 v2_topic_edit 逻辑"""
    from .topic_edit import handle_v2_topic_edit
    result = handle_v2_topic_edit({
        "topic_id": args.get("topic_id", ""),
        "field": args.get("field", ""),
        "new_content": args.get("new_content", ""),
    })
    return json.loads(result)


def _merge(args: dict) -> dict:
    """合并主题：source → target，删除 source"""
    target = args.get("topic_id", "").replace("topic_", "").strip()
    source = args.get("source_topic_id", "").replace("topic_", "").strip()
    
    if not source:
        return {"success": False, "error": "缺少 source_topic_id（源主题ID）"}
    if source == target:
        return {"success": False, "error": "源和目标不能是同一主题"}
    
    store = _get_store()
    
    # 验证两个主题都存在
    t_row = store.conn.execute("SELECT id, title, body FROM topic_blocks WHERE id=?", (target,)).fetchone()
    s_row = store.conn.execute("SELECT id, title, body FROM topic_blocks WHERE id=?", (source,)).fetchone()
    if not t_row:
        store.close()
        return {"success": False, "error": f"目标主题 {target} 不存在"}
    if not s_row:
        store.close()
        return {"success": False, "error": f"源主题 {source} 不存在"}
    
    now = datetime.now().isoformat()
    
    # 1. 把 source 的所有 entries 改挂到 target
    store.conn.execute("UPDATE topic_entries SET topic_id=? WHERE topic_id=?", (target, source))
    entry_count = store.conn.execute(
        "SELECT COUNT(*) FROM topic_entries WHERE topic_id=?", (target,)
    ).fetchone()[0]
    
    # 2. 合并 body
    s_body = (s_row[2] or "").strip()
    t_body = (t_row[2] or "").strip()
    if s_body and t_body:
        merged_body = t_body + "\n\n---\n\n合并来源 [" + s_row[1] + "]:\n" + s_body[:1000]
    elif s_body:
        merged_body = s_body
    else:
        merged_body = t_body
    
    store.conn.execute(
        "UPDATE topic_blocks SET body=?, updated_at=? WHERE id=?",
        (merged_body, now, target)
    )
    
    # 3. 删除 source
    store.conn.execute("DELETE FROM topic_blocks WHERE id=?", (source,))
    store.conn.commit()
    
    # 4. 同步到 v3_cards.db
    _sync_to_cards(target)
    # 删除旧的 source card
    try:
        import sqlite3 as _s3
        from ..config import _resolve_data_dir
        cards_db = str(_resolve_data_dir() / 'v3_cards.db')
        conn = _s3.connect(cards_db)
        conn.execute("DELETE FROM cards WHERE source_id=?", (f"topic_{source}",))
        conn.commit()
        conn.close()
    except Exception:
        pass
    
    store.close()
    
    return {
        "success": True,
        "message": f"已合并 '{s_row[1]}' → '{t_row[1]}'",
        "target_id": target,
        "target_title": t_row[1],
        "total_entries": entry_count,
    }


def _add_entry(args: dict) -> dict:
    """向主题添加一条对话条目"""
    topic_id = args.get("topic_id", "").replace("topic_", "").strip()
    entry = args.get("entry", {})
    
    if not entry or not entry.get("question", "").strip():
        return {"success": False, "error": "entry 缺少 question"}
    
    store = _get_store()
    
    # 验证主题存在
    row = store.conn.execute("SELECT id, title FROM topic_blocks WHERE id=?", (topic_id,)).fetchone()
    if not row:
        store.close()
        return {"success": False, "error": f"主题 {topic_id} 不存在"}
    
    # 获取当前最大 seq
    max_seq = store.conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM topic_entries WHERE topic_id=?", (topic_id,)
    ).fetchone()[0]
    
    import hashlib
    entry_id = hashlib.md5(
        (topic_id + entry.get("question", "") + entry.get("answer", "")).encode()
    ).hexdigest()[:16]
    
    ts = entry.get("timestamp", "") or datetime.now().isoformat()
    source = entry.get("source", "") or "manual_correction"
    
    store.conn.execute(
        """INSERT INTO topic_entries (id, topic_id, seq, timestamp, source, question, answer, is_qa)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
        (entry_id, topic_id, max_seq + 1, ts, source,
         entry.get("question", ""), entry.get("answer", ""))
    )
    store.conn.commit()
    store.close()
    
    return {
        "success": True,
        "message": f"已添加条目到 '{row[1]}'",
        "entry_id": entry_id,
        "topic_id": topic_id,
        "seq": max_seq + 1,
    }


def _delete(args: dict) -> dict:
    """删除主题及关联条目"""
    topic_id = args.get("topic_id", "").replace("topic_", "").strip()
    if not topic_id:
        return {"success": False, "error": "缺少 topic_id"}
    
    store = _get_store()
    row = store.conn.execute("SELECT id, title FROM topic_blocks WHERE id=?", (topic_id,)).fetchone()
    if not row:
        store.close()
        return {"success": False, "error": f"主题 {topic_id} 不存在"}
    
    # 计数
    entry_count = store.conn.execute(
        "SELECT COUNT(*) FROM topic_entries WHERE topic_id=?", (topic_id,)
    ).fetchone()[0]
    
    # 删除
    store.conn.execute("DELETE FROM topic_entries WHERE topic_id=?", (topic_id,))
    store.conn.execute("DELETE FROM topic_blocks WHERE id=?", (topic_id,))
    store.conn.commit()
    store.close()
    
    # 清理 v3_cards.db
    try:
        import sqlite3 as _s3
        from ..config import _resolve_data_dir
        cards_db = str(_resolve_data_dir() / 'v3_cards.db')
        conn = _s3.connect(cards_db)
        conn.execute("DELETE FROM cards WHERE source_id=?", (f"topic_{topic_id}",))
        conn.commit()
        conn.close()
    except Exception:
        pass
    
    return {
        "success": True,
        "message": f"已删除主题 '{row[1]}'（含 {entry_count} 条条目）",
        "topic_id": topic_id,
    }


def _create(args: dict) -> dict:
    """新建主题 — 转发到 v2_topic_create"""
    from .topic_create import handle_v2_topic_create
    title = (args or {}).get("title", "").strip()
    summary = (args or {}).get("summary", "").strip()
    body = (args or {}).get("body", "")
    keywords = (args or {}).get("keywords") or []

    if not title or not summary:
        return {"success": False, "error": "缺少必填参数 title / summary"}

    raw = handle_v2_topic_create({
        "title": title,
        "summary": summary,
        "body": body,
        "keywords": keywords,
    })
    try:
        result = json.loads(raw)
    except Exception:
        return {"success": False, "error": f"v2_topic_create 返回非 JSON: {raw[:200]}"}
    return result


def handle_v3_topic_correct(args: dict, **kw) -> str:
    """入口：根据 action 分发"""
    action = (args or {}).get("action", "")
    topic_id = (args or {}).get("topic_id", "").replace("topic_", "").strip()
    
    if not action:
        return json.dumps({"success": False, "error": "缺少 action"}, ensure_ascii=False)
    if not topic_id and action != "create":
        return json.dumps({"success": False, "error": "缺少 topic_id"}, ensure_ascii=False)
    
    handlers = {
            "edit": _edit,
            "merge": _merge,
            "add_entry": _add_entry,
            "delete": _delete,
            "create": _create,
        }
    
    handler = handlers.get(action)
    if not handler:
        return json.dumps({"success": False, "error": f"不支持的操作: {action}"}, ensure_ascii=False)
    
    try:
        result = handler(args)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": f"{action} 失败: {_safe_err(e)}"}, ensure_ascii=False)
