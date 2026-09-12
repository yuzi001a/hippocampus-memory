"""v2_topic_edit — 手工纠正 LLM 主题提炼内容"""
from __future__ import annotations
import json
import logging
import os


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

logger = logging.getLogger("v3core.tools.topic_edit")

V2_TOPIC_EDIT_SCHEMA = {
    "name": "v2_topic_edit",
    "description": "手工纠正主题内容 — 如果 LLM 对主题的提炼有误，用此工具修正",
    "parameters": {
        "type": "object",
        "properties": {
            "topic_id": {
                "type": "string",
                "description": "要编辑的主题 ID (例如 't_2287cc5e')，可从 v3_prefetch 或 v3_search 结果中获得",
            },
            "field": {
                "type": "string",
                "enum": ["title", "summary", "body"],
                "description": "要编辑的字段: title=标题, summary=摘要, body=正文内容",
            },
            "new_content": {
                "type": "string",
                "description": "新的内容（替换整个字段）",
            },
        },
        "required": ["topic_id", "field", "new_content"],
    },
}


def handle_v2_topic_edit(args: dict, **kw) -> str:
    """Edit a topic block field"""
    import json

    topic_id = (args or {}).get("topic_id", "")
    field = (args or {}).get("field", "")
    new_content = (args or {}).get("new_content", "")

    if not topic_id or not field or not new_content:
        return json.dumps(
            {"success": False, "error": "缺少必填参数 topic_id / field / new_content"},
            ensure_ascii=False,
        )

    # 兼容格式: "topic_xx"（来自 prefetch）→ 直接用 "t_xx"（数据库ID）
    raw_id = topic_id.replace("topic_", "").strip()

    try:
        from ..config import _resolve_data_dir
        from ..topic_store import TopicStore

        db_path = str(_resolve_data_dir() / 'v3_topic_full.db')
        store = TopicStore(db_path)

        # 验证主题存在
        row = store.conn.execute(
            "SELECT id, title FROM topic_blocks WHERE id=?", (raw_id,)
        ).fetchone()
        if not row:
            store.close()
            return json.dumps(
                {"success": False, "error": f"主题 {raw_id} 不存在"},
                ensure_ascii=False,
            )

        old_title = row[1]
        valid_fields = {"title": "title", "summary": "summary", "body": "body"}
        col = valid_fields.get(field)
        if not col:
            store.close()
            return json.dumps(
                {"success": False, "error": f"不支持字段: {field}"},
                ensure_ascii=False,
            )

        store.conn.execute(
            f'UPDATE topic_blocks SET {col}=?, updated_at=datetime("now") WHERE id=?',
            (new_content[:5000], raw_id),
        )
        store.conn.commit()
        
        # 同步到 v3_cards.db（主题卡数据桥接）
        _sync_edit_to_cards(raw_id, store)
        
        # 如果改了标题，需要重新算 embedding
        if field == "title":
            from ..embedding import call_embedding, safe_embed_cfg
            from ..config import resolve_config, _resolve_data_dir

            cfg = resolve_config()
            embed_cfg = safe_embed_cfg(cfg)

            row2 = store.conn.execute(
                "SELECT summary, body FROM topic_blocks WHERE id=?", (raw_id,)
            ).fetchone()
            summary = row2[0] or ""
            body = (row2[1] or "")[:200]
            vec_text = f"{new_content} {summary} {body}"[:1000]
            emb = call_embedding(vec_text, embed_cfg) if embed_cfg is not None else None
            if emb is not None:
                store.conn.execute(
                    "UPDATE topic_blocks SET embedding=? WHERE id=?",
                    (json.dumps(emb), raw_id),
                )
                store.conn.commit()

        store.close()

        return json.dumps(
            {
                "success": True,
                "message": f"主题 '{old_title}' 的 {field} 已更新",
                "topic_id": raw_id,
                "field": field,
            },
            ensure_ascii=False,
        )

    except ValueError:
        raise
    except Exception as e:
        return json.dumps(
            {"success": False, "error": f"编辑主题失败: {_safe_err(e)}"},
            ensure_ascii=False,
        )


def _sync_edit_to_cards(topic_id: str, store):
    """编辑主题后同步到 v3_cards.db"""
    try:
        row = store.conn.execute(
            'SELECT id, title, body, summary FROM topic_blocks WHERE id=?',
            (topic_id,)
        ).fetchone()
        if not row or not row[2]:
            return
        body = row[2]
        if len(body.strip()) < 50:
            return
        
        import sqlite3 as _s3
        from pathlib import Path
        
        cards_db = str(_resolve_data_dir() / 'v3_cards.db')
        conn = _s3.connect(cards_db)
        sid = f'topic_{topic_id}'
        now = __import__('datetime').datetime.now().isoformat()
        
        existing = conn.execute('SELECT source_id FROM cards WHERE source_id=?', (sid,)).fetchone()
        if existing:
            conn.execute('UPDATE cards SET content=?, updated_at=? WHERE source_id=?', (body, now, sid))
        else:
            conn.execute('''INSERT INTO cards (source_id,category,title,content,source,created_at,updated_at)
                VALUES (?, 'topic', ?, ?, 'topic_pipeline', ?, ?)''', (sid, row[1], body, now, now))
        conn.commit()
        conn.close()
    except Exception:
        pass  # 非关键路径


def _old_function_placeholder():
    pass