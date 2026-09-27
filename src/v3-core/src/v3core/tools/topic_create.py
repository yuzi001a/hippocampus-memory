"""v2_topic_create — 手工新建主题"""
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

logger = logging.getLogger("v3core.tools.topic_create")

V2_TOPIC_CREATE_SCHEMA = {
    "name": "v2_topic_create",
    "description": "手工新建一个主题 — 想就某个方向做专项记录时使用",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "主题标题（简洁，2-8 字）",
            },
            "summary": {
                "type": "string",
                "description": "主题摘要（一句话概括）",
            },
            "body": {
                "type": "string",
                "description": "主题正文（可选，后续可通过增量提炼追加）",
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "关键词（可选，帮助向量匹配）",
            },
        },
        "required": ["title", "summary"],
    },
}


def handle_v2_topic_create(args: dict, **kw) -> str:
    """Create a new topic (or return existing if same title)"""
    import json

    title = (args or {}).get("title", "").strip()
    summary = (args or {}).get("summary", "").strip()
    body = (args or {}).get("body", "")
    keywords = (args or {}).get("keywords", [])

    if not title or not summary:
        return json.dumps(
            {"success": False, "error": "缺少必填参数 title / summary"},
            ensure_ascii=False,
        )

    try:
        from ..topic_store import TopicStore
        from ..embedding import call_embedding, safe_embed_cfg
        from ..config import resolve_config, _resolve_data_dir

        db_path = str(_resolve_data_dir() / 'v3_topic_full.db')
        store = TopicStore(db_path)

        cfg = resolve_config()
        embed_cfg = safe_embed_cfg(cfg)

        # 仅在 embedding 显式启用时计算；disabled 不手拼空配置。
        vec_text = f"{title} {summary} {' '.join(keywords)}"[:1000]
        emb = None
        if embed_cfg is not None:
            # Derived state; the card is the asset. Name the durable policy and record a
            # durable marker rather than letting a failure become an unexplained NULL.
            # The id is derived exactly as upsert_topic will derive it — same helper,
            # same already-truncated title — so the marker keys on the row that lands.
            from ..embedding import DURABLE_WRITE_EMBED_POLICY
            from ..embed_failures import embed_for_write
            _out = embed_for_write(
                vec_text, embed_cfg,
                entity_table="topic_blocks",
                entity_id=store._topic_id(title[:50]),
                phase="topic_create",
                policy=DURABLE_WRITE_EMBED_POLICY,
            )
            emb = _out.vector
            if not _out.ok:
                logger.warning(
                    "topic_create embedding %s for %r: class=%s retryable=%s "
                    "marker_recorded=%s",
                    _out.status.value, title[:30], _out.error_class,
                    _out.retryable, _out.marker_recorded,
                )

        # embedding 需要序列化为 JSON 字符串才能写 SQLite
        import json as _json
        emb_str = _json.dumps(emb)

        tid = store.upsert_topic(
            title=title[:50],
            summary=summary[:500],
            body=body[:3000],
            keywords=keywords or [],
            embedding=emb_str,
        )

        store.close()

        return json.dumps(
            {
                "success": True,
                "topic_id": tid,
                "title": title,
                "message": f"主题 '{title}' 已创建/更新",
            },
            ensure_ascii=False,
        )

    except ValueError:
        raise
    except Exception as e:
        return json.dumps(
            {"success": False, "error": f"创建失败: {_safe_err(e)}"}, ensure_ascii=False
        )
