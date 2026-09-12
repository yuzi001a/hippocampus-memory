"""v3_get — 统一查入口 (v3_status / v3_search / v3_moc_overview / v3_moc_get / v3_prefetch / v3_get_message_context / hm_handbook_get / hm_get)"""
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

logger = logging.getLogger("v3core.tools.api_get")

V3_GET_SCHEMA = {
    "name": "v3_get",
    "description": "状态/搜索/手帐总览/手帐条目/prefetch/消息原文/handbook条目/hm统一读",
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "enum": ["status", "search", "overview", "hand帐", "prefetch", "message", "handbook", "hm"],
                "description": "查询目标: status=系统状态, search=搜索卡库, overview=手帐总览, hand帐=手帐条目, prefetch=召回, message=消息原文, handbook=handbook单条(key必填), hm=hm统一读(source_id必填)",
            },
            "query": {"type": "string", "description": "搜索关键词 (search/prefetch mode)"},
            "key": {"type": "string", "description": "手帐键名 (hand帐/handbook mode)"},
            "source_id": {"type": "string", "description": "消息ID (message mode) 或 hm 统一 source_id (hm mode)"},
            "category": {"type": "string", "description": "类别筛选 (search mode)"},
            "limit": {"type": "integer", "description": "返回条数, 默认 10"},
            "format": {
                "type": "string",
                "enum": ["json", "context", "ids"],
                "description": "prefetch 输出格式 (prefetch mode)",
            },
        },
        "required": ["target"],
    },
}


def handle_v3_get(args: dict, **kw) -> str:
    """Unified read: delegate to various tools based on target"""
    target = args.get("target", "status")
    try:
        if target == "status":
            from .status import handle_v3_status
            return handle_v3_status({}, **kw)
        elif target == "search":
            from .search import handle_v3_search
            return handle_v3_search({
                "query": args.get("query", ""),
                "category": args.get("category"),
                "limit": args.get("limit", 10),
            }, **kw)
        elif target == "overview":
            from .moc_tools import handle_v3_moc_overview
            return handle_v3_moc_overview({}, **kw)
        elif target == "hand帐":
            key = args.get("key", "")
            if not key:
                return json.dumps({"success": False, "error": "key required for hand帐 mode"}, ensure_ascii=False)
            from .moc_tools import handle_v3_moc_get
            return handle_v3_moc_get({"key": key}, **kw)
        elif target == "prefetch":
            from . import handle_v3_prefetch
            return handle_v3_prefetch({
                "query": args.get("query", ""),
                "limit": args.get("limit", 5),
                "format": args.get("format", "json"),
            }, **kw)
        elif target == "message":
            from . import handle_v3_get_message_context
            return handle_v3_get_message_context({"source_id": args.get("source_id", "")}, **kw)
        elif target == "handbook":
            key = args.get("key", "")
            if not key:
                return json.dumps({"success": False, "error": "key required for handbook mode"}, ensure_ascii=False)
            from .handbook_tools import handle_hm_handbook_get
            return handle_hm_handbook_get({"key": key}, **kw)
        elif target == "hm":
            source_id = args.get("source_id", "")
            if not source_id:
                return json.dumps({"success": False, "error": "source_id required for hm mode"}, ensure_ascii=False)
            from .get_tool import handle_hm_get
            return handle_hm_get({"source_id": source_id}, **kw)
        else:
            return json.dumps({"success": False, "error": f"unknown target: {target}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)