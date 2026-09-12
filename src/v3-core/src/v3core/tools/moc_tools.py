"""MOC 3 tools (note/entry removed: use v3_store category='shou_zhang')"""
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

logger = logging.getLogger("v3core.tools.moc")

V3_MOC_OVERVIEW_SCHEMA = {
    "name": "v3_moc_overview",
    "description": "[3-读取] 手帐总览 — 列表含标题/类别/日期/->卡片指针",
    "parameters": {"type": "object", "properties": {}},
}
V3_MOC_GET_SCHEMA = {
    "name": "v3_moc_get",
    "description": "[3-读取] 获取手帐条目（标题 + 摘要 + ->卡片指针）",
    "parameters": {
        "type": "object",
        "properties": {"key": {"type": "string", "description": "手帐键名（来自总览列表的条目名称）"}},
        "required": ["key"],
    },
}
V3_MOC_SYNC_SCHEMA = {
    "name": "v3_moc_sync",
    "description": "[4-管理] 同步手帐 — 扫描 cards/shou_zhang/, 补全未注册的条目到 MOC",
    "parameters": {"type": "object", "properties": {}},
}


def _get_cards_root(core=None):
    from pathlib import Path
    if core is None:
        from .. import V3Core
        core = V3Core()
    return Path(core._get_base_path()) / "cards"


def handle_v3_moc_overview(args: dict, **kw) -> str:
    try:
        from .. import V3Core
        core = kw.get("core") or V3Core(
            effective_config=kw.get("effective_config"),
            pg_pool=kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool"),
        )
        return json.dumps({"success": True, "overview": core.moc.overview()}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)


def handle_v3_moc_get(args: dict, **kw) -> str:
    try:
        from .. import V3Core
        core = kw.get("core") or V3Core(
            effective_config=kw.get("effective_config"),
            pg_pool=kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool"),
        )
        return json.dumps({"success": True, "entry": core.moc.get(args.get("key", ""))}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)


def handle_v3_moc_sync(args: dict, **kw) -> str:
    try:
        from .. import V3Core
        core = kw.get("core") or V3Core(
            effective_config=kw.get("effective_config"),
            pg_pool=kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool"),
        )
        cards_root = _get_cards_root(core)
        return core.moc.sync(cards_root=cards_root)
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)
