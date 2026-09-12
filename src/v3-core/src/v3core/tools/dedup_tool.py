"""v3_dedup_daily tool — title exact match + cosine clustering"""
from __future__ import annotations
import json
import logging


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

logger = logging.getLogger("v3core.tools.dedup")

V3_DEDUP_DAILY_SCHEMA = {
    "name": "v3_dedup_daily",
    "description": "[4-管理] 每日去重（标题精确匹配 + cosine 聚类）",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

def handle_v3_dedup_daily(args: dict, **kw) -> str:
    """Two-stage dedup: title exact match then cosine clustering"""
    try:
        from ..dedup import dedup_all
        from ..card_store import DeepStore
        from ..config import resolve_config
        cfg = kw.get("effective_config")
        if cfg is None:
            cfg = resolve_config()
        store = DeepStore(cfg)
        pool = kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool")
        if kw.get("runtime_context") and pool is None:
            return json.dumps({
                "success": False,
                "error": "Runtime-backed dedup requires its PgPool owner",
            }, ensure_ascii=False)
        pg = None
        if pool is not None:
            from ..pg_store import PgEmbedStore
            pg = PgEmbedStore(config=cfg, pool=pool)
        result = dedup_all(store, cfg, dry_run=False, pg=pg)
        return json.dumps({"success": True, **result}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)
