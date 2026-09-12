"""15 + 5 API 工具 schema + dispatch 统一入口"""
from __future__ import annotations
from typing import Any, Callable
from .status import V3_STATUS_SCHEMA, handle_v3_status
from .store import V3_STORE_SCHEMA, handle_v3_store
from .search import V3_SEARCH_SCHEMA, handle_v3_search
from .extract_tool import V3_EXTRACT_SCHEMA, handle_v3_extract
from .import_ import (V3_IMPORT_SEED_SCHEMA, V3_IMPORT_FULL_SCHEMA,
                       handle_v3_import_seed, handle_v3_import_full)
from .moc_tools import (V3_MOC_OVERVIEW_SCHEMA, V3_MOC_GET_SCHEMA,
                         V3_MOC_SYNC_SCHEMA, handle_v3_moc_overview,
                         handle_v3_moc_get, handle_v3_moc_sync)
from .dedup_tool import V3_DEDUP_DAILY_SCHEMA, handle_v3_dedup_daily
from .organize import V3_ORGANIZE_MEMORY_SCHEMA, handle_v3_organize_memory
from .affinity import V3_AFFINITY_SCHEMA, handle_v3_affinity
from .health import V3_HEALTH_SCHEMA, handle_v3_health
from .topic_edit import V2_TOPIC_EDIT_SCHEMA, handle_v2_topic_edit
from .topic_create import V2_TOPIC_CREATE_SCHEMA, handle_v2_topic_create
from .topic_correct import V3_TOPIC_CORRECT_SCHEMA, handle_v3_topic_correct
from .topic_maintain_tool import V3_TOPIC_MAINTAIN_SCHEMA, handle_v3_topic_maintain
from .handbook_tools import (
    HM_HANDBOOK_SET_SCHEMA, HM_HANDBOOK_GET_SCHEMA,
    HM_HANDBOOK_LIST_SCHEMA, HM_HANDBOOK_DEL_SCHEMA,
    handle_hm_handbook_set, handle_hm_handbook_get,
    handle_hm_handbook_list, handle_hm_handbook_del,
)
from .write_tool import HM_WRITE_SCHEMA, handle_hm_write
from .get_tool import HM_GET_SCHEMA, handle_hm_get, HM_STATUS_SCHEMA, handle_hm_status
# API 五件套 — 统一核心接口
from .api_add import V3_ADD_SCHEMA, handle_v3_add
from .api_get import V3_GET_SCHEMA, handle_v3_get
from .api_update import V3_UPDATE_SCHEMA, handle_v3_update
from .api_delete import V3_DELETE_SCHEMA, handle_v3_delete
from .api_manage import V3_MANAGE_SCHEMA, handle_v3_manage



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

class ToolDef:
    def __init__(self, name: str, schema: dict, handler: Callable):
        self.name = name
        self.schema = schema
        self.handler = handler

TOOL_REGISTRY: dict[str, ToolDef] = {
    # 底层工具 (13 + prefetch)
    "v3_status": ToolDef("v3_status", V3_STATUS_SCHEMA, handle_v3_status),
    "v3_store": ToolDef("v3_store", V3_STORE_SCHEMA, handle_v3_store),
    "v3_search": ToolDef("v3_search", V3_SEARCH_SCHEMA, handle_v3_search),
    "v3_extract": ToolDef("v3_extract", V3_EXTRACT_SCHEMA, handle_v3_extract),
    "v3_import_seed": ToolDef("v3_import_seed", V3_IMPORT_SEED_SCHEMA, handle_v3_import_seed),
    "v3_import_full": ToolDef("v3_import_full", V3_IMPORT_FULL_SCHEMA, handle_v3_import_full),
    "v3_moc_overview": ToolDef("v3_moc_overview", V3_MOC_OVERVIEW_SCHEMA, handle_v3_moc_overview),
    "v3_moc_get": ToolDef("v3_moc_get", V3_MOC_GET_SCHEMA, handle_v3_moc_get),
    "v3_moc_sync": ToolDef("v3_moc_sync", V3_MOC_SYNC_SCHEMA, handle_v3_moc_sync),
    "v3_dedup_daily": ToolDef("v3_dedup_daily", V3_DEDUP_DAILY_SCHEMA, handle_v3_dedup_daily),
    "v3_organize_memory": ToolDef("v3_organize_memory", V3_ORGANIZE_MEMORY_SCHEMA, handle_v3_organize_memory),
    "v3_affinity": ToolDef("v3_affinity", V3_AFFINITY_SCHEMA, handle_v3_affinity),
    "v3_health": ToolDef("v3_health", V3_HEALTH_SCHEMA, handle_v3_health),
    # v2 主题工具 (3)
    "v2_topic_edit": ToolDef("v2_topic_edit", V2_TOPIC_EDIT_SCHEMA, handle_v2_topic_edit),
    "v2_topic_create": ToolDef("v2_topic_create", V2_TOPIC_CREATE_SCHEMA, handle_v2_topic_create),
    "v3_topic_correct": ToolDef("v3_topic_correct", V3_TOPIC_CORRECT_SCHEMA, handle_v3_topic_correct),
    "v3_topic_maintain": ToolDef("v3_topic_maintain", V3_TOPIC_MAINTAIN_SCHEMA, handle_v3_topic_maintain),
    # 核心 API 套件 (5)
    "v3_add": ToolDef("v3_add", V3_ADD_SCHEMA, handle_v3_add),
    "v3_get": ToolDef("v3_get", V3_GET_SCHEMA, handle_v3_get),
    "v3_update": ToolDef("v3_update", V3_UPDATE_SCHEMA, handle_v3_update),
    "v3_delete": ToolDef("v3_delete", V3_DELETE_SCHEMA, handle_v3_delete),
    "v3_manage": ToolDef("v3_manage", V3_MANAGE_SCHEMA, handle_v3_manage),
    # Handbook tools (4)
    "hm_handbook_set": ToolDef("hm_handbook_set", HM_HANDBOOK_SET_SCHEMA, handle_hm_handbook_set),
    "hm_handbook_get": ToolDef("hm_handbook_get", HM_HANDBOOK_GET_SCHEMA, handle_hm_handbook_get),
    "hm_handbook_list": ToolDef("hm_handbook_list", HM_HANDBOOK_LIST_SCHEMA, handle_hm_handbook_list),
    "hm_handbook_del": ToolDef("hm_handbook_del", HM_HANDBOOK_DEL_SCHEMA, handle_hm_handbook_del),
    "hm_write": ToolDef("hm_write", HM_WRITE_SCHEMA, handle_hm_write),
    "hm_get": ToolDef("hm_get", HM_GET_SCHEMA, handle_hm_get),
    "hm_status": ToolDef("hm_status", HM_STATUS_SCHEMA, handle_hm_status),
}

# Prefetch schemas + stubs
V3_PREFETCH_SCHEMA = {
    "name": "v3_prefetch",
    "description": "[3-读取] 手动调 prefetch 召回, format=json/context/ids(扁平) 或 chain(A路三層: 段-碑-迹; 失败回退 B路/文件)",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "limit": {"type": "integer", "description": "返回数量, 默认 5"},
            "format": {
                "type": "string",
                "enum": ["json", "context", "ids", "chain"],
                "description": (
                    "输出格式: json/context/ids = 扁平 B 路 (RRF 排序); "
                    "chain = A 路三層 (anchor -> cards -> traces), 失败自动回退 B 路, "
                    "PG 不可用时回退文件 keyword 搜索"
                ),
            },
        },
        "required": ["query"],
    },
}

V3_GET_MESSAGE_CONTEXT_SCHEMA = {
    "name": "v3_get_message_context",
    "description": "[3-读取] 按 source_id 取消息原文 (PG 优先, j_writer 兜底)",
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {"type": "string", "description": "消息 source_id"},
        },
        "required": ["source_id"],
    },
}

def handle_v3_prefetch(args: dict, **kw) -> str:
    """prefetch handler — topic 卡优先，不足时走链式召回（印→topic→原始对话）

    调试员 2026-07-10 拍板: 去掉 v1 静默回退, 两条路平行独立.
    v2 没有结果就返回空, 不偷偷用旧卡填充.

    fmt in {json, context, ids, chain}:
      json/context/ids: 纯 topic 卡召回（现有行为）
      chain: topic 卡为主 → 不足时读印 → 取 entries → 取原始消息
    """
    import json
    query = (args or {}).get("query", "")
    limit = int((args or {}).get("limit", 5))
    fmt = (args or {}).get("format", "json")
    if not query:
        return json.dumps(
            {"success": False, "error": "query 参数必填"},
            ensure_ascii=False,
        )
    try:
        from ..topic_recall import TopicRecall
        from ..config import resolve_config
        from ..embedding import safe_embed_cfg
        cfg = kw.get("effective_config")
        if cfg is None:
            cfg = resolve_config()
        pool = kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool")
        if kw.get("runtime_context") and pool is None:
            return json.dumps({
                "success": False,
                "error": "Runtime-backed prefetch requires its PgPool owner",
            }, ensure_ascii=False)
        embed_cfg = safe_embed_cfg(cfg)

        if embed_cfg is None:
            empty = {"success": True, "format": fmt, "source": "embedding_disabled"}
            if fmt == "chain":
                empty.update({"cards": [], "yin_context": "", "entries": [], "count": 0})
            elif fmt == "context":
                empty.update({"context": "", "count": 0})
            elif fmt == "ids":
                empty.update({"ids": [], "count": 0})
            else:
                empty.update({"cards": [], "count": 0})
            return json.dumps(empty, ensure_ascii=False)

        if fmt == "chain":
            # 链式召回: topic → 印上下文 → entries
            recall = TopicRecall(embed_cfg, pool=pool)
            chain = recall.chain_recall(query)
            return json.dumps({
                "success": True, "format": "chain",
                "cards": chain["cards"],
                "yin_context": chain["yin_context"],
                "entries": chain["entries"],
                "count": len(chain["cards"]),
                "source": chain["source"],
            }, ensure_ascii=False)

        # json / context / ids: 纯 topic 卡召回
        recall = TopicRecall(embed_cfg, pool=pool)
        topic_matches = recall.match(query, top_k=limit)
        if not topic_matches:
            return json.dumps({"success": True, "format": fmt,
                               "cards": [], "count": 0, "source": "v2_topic"},
                              ensure_ascii=False)

        if fmt == "context":
            text = recall.format_context(topic_matches)
            return json.dumps({"success": True, "format": "context",
                               "context": text, "count": len(topic_matches),
                               "source": "v2_topic"}, ensure_ascii=False)
        cards = [{"source_id": f"topic_{t['id']}",
                  "title": t['title'],
                  "rrf_score": sim,
                  "content_preview": t.get('body', '')[:200],
                  "source": "v2_topic"}
                 for sim, t in topic_matches]
        if fmt == "ids":
            return json.dumps({"success": True, "format": "ids",
                               "ids": [c["source_id"] for c in cards],
                               "count": len(cards), "source": "v2_topic"},
                              ensure_ascii=False)
        return json.dumps({"success": True, "format": "json",
                           "cards": cards, "count": len(cards),
                           "source": "v2_topic"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps(
            {"success": False, "error": "v2_prefetch 失败: %s" % _safe_err(e)},
            ensure_ascii=False,
        )

def handle_v3_get_message_context(args: dict, **kw) -> str:
    import json
    source_id = args.get("source_id", "")
    if not source_id:
        return json.dumps({"success": False, "error": "缺少 source_id"}, ensure_ascii=False)
    try:
        from .. import V3Core
        core = kw.get("core")
        if core is None:
            from ..config import resolve_config
            cfg = kw.get("effective_config")
            if cfg is None:
                cfg = resolve_config()
            pool = kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool")
            core = V3Core(effective_config=cfg, pg_pool=pool)
        return core.get_message_context(source_id)
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)

TOOL_REGISTRY["v3_prefetch"] = ToolDef("v3_prefetch", V3_PREFETCH_SCHEMA, handle_v3_prefetch)
TOOL_REGISTRY["v3_get_message_context"] = ToolDef("v3_get_message_context", V3_GET_MESSAGE_CONTEXT_SCHEMA, handle_v3_get_message_context)

def get_tool_schemas() -> list[dict]:
    return [td.schema for td in TOOL_REGISTRY.values()]

def handle_tool_call(name: str, args: dict, **kw) -> str:
    td = TOOL_REGISTRY.get(name)
    if not td:
        raise ValueError(f"Unknown tool: {name}")
    return td.handler(args, **kw)
