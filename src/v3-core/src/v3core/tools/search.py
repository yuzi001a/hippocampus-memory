"""v3_search — 优先搜主题 (v2), 回退旧卡 (v1)"""
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

logger = logging.getLogger("v3core.tools.search")

V3_SEARCH_SCHEMA = {
    "name": "v3_search",
    "description": "[3-读取] search cards (RRF: keyword + vector hnsw)",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "search query"},
            "category": {"type": "string", "description": "optional category filter"},
            "limit": {"type": "integer", "description": "results limit, default 10"},
        },
        "required": ["query"],
    },
}


def handle_v3_search(args: dict, **kw) -> str:
    """v2 主题搜索优先, 无结果时回退 v1 卡片搜索"""
    try:
        query = args.get("query", "").strip()
        cat = args.get("category")
        limit = int(args.get("limit") or 10)

        if not query:
            return json.dumps(
                {"success": False, "error": "query required"}, ensure_ascii=False
            )

        # v2 路径: 搜主题
        cfg = kw.get("effective_config")
        pool = kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool")
        try:
            from ..topic_recall import TopicRecall
            from ..config import resolve_config
            from ..embedding import safe_embed_cfg

            cfg = kw.get("effective_config")
            if cfg is None:
                cfg = resolve_config()
            pool = kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool")
            embed_cfg = safe_embed_cfg(cfg)
            if embed_cfg is None:
                raise RuntimeError("embedding 未配置，跳过主题向量路径")
            recall = TopicRecall(embed_cfg, pool=pool)
            topic_matches = recall.match(query, top_k=limit)

            if topic_matches:
                results = []
                for sim, topic in topic_matches:
                    results.append(
                        {
                            "source": "v2_topic",
                            "source_id": f"topic_{topic['id']}",
                            "title": topic["title"],
                            "summary": topic.get("summary", ""),
                            "cosine": round(sim, 4),
                            "body_preview": (topic.get("body", "") or "")[:300],
                        }
                    )
                return json.dumps(
                    {
                        "success": True,
                        "source": "v2_topic",
                        "query": query,
                        "category": cat,
                        "total": len(results),
                        "results": results,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
        except ValueError:
            raise
        except Exception:
            pass  # v2 不可用 → 回退 v1

        # v1 回退: 旧卡搜索
        from .. import V3Core

        core = kw.get("core") or V3Core(effective_config=cfg, pg_pool=pool)
        results = core.search_cards(query, category=cat, limit=limit)

        return json.dumps(
            {
                "success": True,
                "source": "v1_fallback",
                "query": query,
                "category": cat,
                "total": len(results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        return json.dumps(
            {"success": False, "error": _safe_err(e)}, ensure_ascii=False
        )
