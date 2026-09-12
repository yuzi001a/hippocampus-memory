"""v3_update — 统一整理入口 (v3_organize_memory / v3_dedup_daily / v3_moc_sync / v3_delete / v3_topic_maintain / v3_affinity)"""
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

logger = logging.getLogger("v3core.tools.api_update")

V3_UPDATE_SCHEMA = {
    "name": "v3_update",
    "description": "整理记忆 — organize组织MEMORY/USER/SOUL、dedup去重、sync同步手帐、delete按source_id归档/删除卡、topic_maintain主题维护、affinity共鸣度",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["organize", "dedup", "sync", "delete", "topic_maintain", "affinity"],
                "description": "整理动作: organize=读写MEMORY/USER/SOUL, dedup=去重, sync=同步手帐, delete=按source_id删除/归档, topic_maintain=主题维护, affinity=共鸣度",
            },
            "organize_action": {
                "type": "string",
                "enum": ["preview", "apply"],
                "default": "preview",
                "description": "organize 的子动作 (仅 organize action 有效)",
            },
            "memory_md": {"type": "string", "description": "新 MEMORY.md 内容 (organize/apply only)"},
            "user_md": {"type": "string", "description": "新 USER.md 内容 (organize/apply only)"},
            "soul_md": {"type": "string", "description": "新 SOUL.md 内容 (organize/apply optional)"},
            "shou_zhang_append": {"type": "string", "description": "追加手帐内容 (organize/apply optional)"},
            "source_id": {"type": "string", "description": "要删除/归档的卡 source_id (delete action 必填)"},
            "dry_run": {"type": "boolean", "description": "topic_maintain 预览开关 (默认 True)", "default": True},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def handle_v3_update(args: dict, **kw) -> str:
    """Unified update: delegate to organize / dedup / moc_sync / delete / topic_maintain / affinity"""
    action = args.get("action", "sync")
    try:
        if action == "organize":
            from .organize import handle_v3_organize_memory
            return handle_v3_organize_memory({
                "action": args.get("organize_action", "preview"),
                "memory_md": args.get("memory_md", ""),
                "user_md": args.get("user_md", ""),
                "soul_md": args.get("soul_md", ""),
                "shou_zhang_append": args.get("shou_zhang_append", ""),
            }, **kw)
        elif action == "dedup":
            from .dedup_tool import handle_v3_dedup_daily
            return handle_v3_dedup_daily({}, **kw)
        elif action == "sync":
            from .moc_tools import handle_v3_moc_sync
            return handle_v3_moc_sync({}, **kw)
        elif action == "delete":
            from .api_delete import handle_v3_delete
            return handle_v3_delete({
                "source_id": args.get("source_id", ""),
                "yes": True,
            }, **kw)
        elif action == "topic_maintain":
            from .topic_maintain_tool import handle_v3_topic_maintain
            return handle_v3_topic_maintain({
                "dry_run": args.get("dry_run", True),
            }, **kw)
        elif action == "affinity":
            from .affinity import handle_v3_affinity
            return handle_v3_affinity({}, **kw)
        else:
            return json.dumps({"success": False, "error": f"unknown action: {action}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)