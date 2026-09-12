"""hm_handbook_* tools — CRUD handbook entries (manually maintained curated data)."""
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

logger = logging.getLogger("v3core.tools.handbook")

HM_HANDBOOK_SET_SCHEMA = {
    "name": "hm_handbook_set",
    "description": "Write or overwrite a handbook entry (NOT append). Manually maintained curated reference data.",
    "parameters": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Entry key (unique identifier)"},
            "title": {"type": "string", "description": "Entry title"},
            "content": {"type": "string", "description": "Entry content body"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags",
            },
        },
        "required": ["key", "title", "content"],
    },
}

HM_HANDBOOK_GET_SCHEMA = {
    "name": "hm_handbook_get",
    "description": "Get a handbook entry by key.",
    "parameters": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Entry key to retrieve"},
        },
        "required": ["key"],
    },
}

HM_HANDBOOK_LIST_SCHEMA = {
    "name": "hm_handbook_list",
    "description": "List all handbook entries (title + updated_at only).",
    "parameters": {"type": "object", "properties": {}},
}

HM_HANDBOOK_DEL_SCHEMA = {
    "name": "hm_handbook_del",
    "description": "Delete a handbook entry by key.",
    "parameters": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Entry key to delete"},
        },
        "required": ["key"],
    },
}


def handle_hm_handbook_set(args: dict, **kw) -> str:
    try:
        from ..handbook import HandbookManager

        h = HandbookManager()
        key = args.get("key", "").strip()
        title = args.get("title", "").strip()
        content = args.get("content", "").strip()
        tags = args.get("tags") or []
        if not key or not title or not content:
            return json.dumps(
                {"success": False, "error": "key/title/content required"},
                ensure_ascii=False,
            )
        result = h.set(key, title, content, tags=tags)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)


def handle_hm_handbook_get(args: dict, **kw) -> str:
    try:
        from ..handbook import HandbookManager

        h = HandbookManager()
        key = args.get("key", "").strip()
        if not key:
            return json.dumps(
                {"success": False, "error": "key required"}, ensure_ascii=False
            )
        entry = h.get(key)
        if entry is None:
            return json.dumps(
                {"success": False, "error": f"entry not found: {key}"},
                ensure_ascii=False,
            )
        return json.dumps(
            {"success": True, "key": key, "entry": entry}, ensure_ascii=False
        )
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)


def handle_hm_handbook_list(args: dict, **kw) -> str:
    try:
        from ..handbook import HandbookManager

        h = HandbookManager()
        entries = h.list_all()
        return json.dumps(
            {
                "success": True,
                "entries": entries,
                "count": len(entries),
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)


def handle_hm_handbook_del(args: dict, **kw) -> str:
    try:
        from ..handbook import HandbookManager

        h = HandbookManager()
        key = args.get("key", "").strip()
        if not key:
            return json.dumps(
                {"success": False, "error": "key required"}, ensure_ascii=False
            )
        deleted = h.delete(key)
        return json.dumps(
            {"success": deleted, "key": key, "found": deleted}, ensure_ascii=False
        )
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)
