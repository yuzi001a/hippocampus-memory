"""v3_extract tool"""
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

logger = logging.getLogger("v3core.tools.extract")

V3_EXTRACT_SCHEMA = {
    "name": "v3_extract",
    "description": "[1-自动] extract cards from conversation text. Pass experts=['decisions','lessons',...] for multi-expert mode (one card per expert, distinct category)",
    "parameters": {
        "type": "object",
        "properties": {
            "raw_text": {"type": "string", "description": "conversation text"},
            "write": {"type": "boolean", "description": "True=persist, False=dry-run only"},
            "experts": {
                "type": "array",
                "items": {"type": "string", "enum": ["decisions", "lessons", "projects", "system"]},
                "description": "多专家模式: 同一段对话走多个专家 prompt 各产一张卡. 可选 experts ∈ {decisions, lessons, projects, system}. 不传/空数组 = 通用模式.",
            },
        },
        "required": ["raw_text"],
    },
}

def handle_v3_extract(args: dict, **kw) -> str:
    """Delegate to V3Core.extract_from_session for both dry-run and write paths"""
    try:
        raw_text = args.get("raw_text", "").strip()
        write = bool(args.get("write", False))
        experts = args.get("experts")  # list[str] | None

        if not raw_text or len(raw_text) < 200:
            return json.dumps({"success": False, "error": "raw_text needs at least 200 chars"}, ensure_ascii=False)

        from .. import V3Core
        core = kw.get("core") or V3Core(
            effective_config=kw.get("effective_config"),
            pg_pool=kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool"),
        )
        result = core.extract_from_session(raw_text, write=write, experts=experts)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)
