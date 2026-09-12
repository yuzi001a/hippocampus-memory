"""v3_topic_maintain — 手动调用的工具包装"""
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

logger = logging.getLogger("v3core.tools.topic_maintain")

V3_TOPIC_MAINTAIN_SCHEMA = {
    "name": "v3_topic_maintain",
    "description": "主题自动维护 — 孤儿 buffer 二次聚簇 + 休眠标记。dry_run=True 预览不执行。",
    "parameters": {
        "type": "object",
        "properties": {
            "dry_run": {
                "type": "boolean",
                "description": "True=仅预览不做改动，False=实际执行",
                "default": True,
            },
        },
    },
}


def handle_v3_topic_maintain(args: dict, **kw) -> str:
    dry_run = (args or {}).get("dry_run", True)
    try:
        from ..topic_maintain import run_maintenance
        pool = kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool")
        if kw.get("runtime_context") and pool is None:
            return json.dumps({
                "success": False,
                "error": "Runtime-backed topic maintenance requires its PgPool owner",
            }, ensure_ascii=False)
        result = run_maintenance(
            dry_run=dry_run,
            pool=pool,
            config=kw.get("effective_config"),
        )
        if not dry_run:
            core = kw.get("core")
            notify = getattr(core, "_notify_topic_recall_invalidation", None)
            if callable(notify):
                try:
                    notify()
                except Exception as e:
                    logger.warning(
                        "topic_maintain on_topics_commit 失败 (非阻塞): %s",
                        _safe_err(e)[:120],
                    )
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": _safe_err(e)}, ensure_ascii=False)
