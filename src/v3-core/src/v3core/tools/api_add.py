"""v3_add — unified write entry (forward to v3_store / hm_write)

默认转发到 v3_store（标准卡写入）。
如果 args.to_handbook=True 且 args.handbook_key 非空，转发到 hm_write（同写卡 + handbook 条目）。

explicit-memory boundary: 不引入新 schema, 仅在缺失 handbook_key 时把错误 receipt
补齐 explicit-memory boundary 字段 (durable=False/source_id=""...), 让上层 caller 拿到的失败
形状与成功形状一致, 方便 UI/审计按 success 字段分支.
"""
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

logger = logging.getLogger("v3core.tools.api_add")

V3_ADD_SCHEMA = {
    "name": "v3_add",
    # A0 explicit-memory opt-in contract: same ownership rule
    # as V3_STORE_SCHEMA. Every v3_add → durable write into
    # public.explicit_memories (unless to_handbook=True, in which case
    # it also mirrors a handbook entry). Allowed ONLY when the user
    # explicitly asks to remember / store / save / retain a specific
    # durable item, or an explicitly authorized host workflow (handbook
    # sync, seed import, etc.) requests it. NOT allowed for: dev
    # experience, reviewer findings, debugging notes, task status /
    # summary, implementation decisions, inferred preferences / facts,
    # generic lessons, or 'summarize tonight'.
    "description": (
        "[2-写卡] OPT-IN explicit-memory write — write a card into the "
        "specified category (optionally also to handbook when "
        "to_handbook=True). Delegates to v3_store / hm_write and therefore "
        "shares the public.explicit_memories opt-in contract: allowed ONLY "
        "when the user explicitly asks to remember / store / save / retain a "
        "specific durable item, or an explicitly authorized host workflow "
        "(handbook sync, seed import, etc.) requests it. NOT authorization: "
        "dev experience, reviewer findings, debugging notes, task status / "
        "summary, implementation decisions, inferred preferences / facts, "
        "generic lessons, or 'summarize tonight'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "卡类别"},
            "title": {"type": "string", "description": "标题"},
            "content": {"type": "string", "description": "正文"},
            "tags": {
                "type": "array",
                "items": {"type": "string", "description": "标签 (可选)"},
            },
            "to_handbook": {
                "type": "boolean",
                "description": "同时写入 handbook (需要 handbook_key)",
                "default": False,
            },
            "handbook_key": {
                "type": "string",
                "description": "handbook 条目 key (to_handbook=True 时必填)",
            },
            # explicit-memory boundary SOL-review C: 与 V3_STORE_SCHEMA 同口径
            # 收窄 — ``source_id`` 保留 (caller 显式身份), ``source`` /
            # ``source_j_ids`` 不再作为公共 v3_add property 暴露. 内部兼
            # 容由 handle_v3_add 透传给 handle_v3_store / handle_hm_write,
            # 仍走 ``_PROVENANCE_KEYS`` 白名单 (store._extract_provenance).
            "source_id": {"type": "string", "description": "optional caller-provided source id (forwarded to core; do not fabricate)"},
        },
        "required": ["category", "title", "content"],
    },
}


def handle_v3_add(args: dict, **kw) -> str:
    """Unified write: delegates to v3_store or hm_write based on to_handbook flag.

    explicit-memory boundary 工具层契约: 把 explicit-memory boundary 字段 (durable/source_id/durable_store/
    status/warnings) 一并透传, 不擦 source 真值. handbook 失败走 warning,
    不改写 PG 真值.
    """
    try:
        to_handbook = bool(args.get("to_handbook", False))
        handbook_key = (args.get("handbook_key") or "").strip()

        if to_handbook:
            if not handbook_key:
                # explicit-memory boundary: 补齐失败 receipt 形状, 避免上层 caller 收到 success=False
                # 还要重新 try/except 拿 durable 字段.
                return json.dumps(
                    {
                        "success": False,
                        "durable": False,
                        "durable_store": "",
                        "source_id": "",
                        "status": "INVALID_INPUT",
                        "error": "to_handbook=True 时必须提供 handbook_key",
                    },
                    ensure_ascii=False,
                )
            from .write_tool import handle_hm_write
            # explicit-memory boundary: 透传 caller 提供的来源溯源字段 (与 V3_STORE_SCHEMA 同语义,
            # 不伪造 QA id). 复用 store._PROVENANCE_KEYS 白名单, 只挑 caller 显式
            # 提供的非空值, 没传就让 core 走确定性的请求身份兜底.
            from .store import _extract_provenance
            provenance = _extract_provenance(args)
            payload = {
                "category": args.get("category", ""),
                "title": args.get("title", ""),
                "content": args.get("content", ""),
                "tags": args.get("tags") or [],
                "to_handbook": True,
                "handbook_key": handbook_key,
            }
            payload.update(provenance)
            return handle_hm_write(payload, **kw)

        from .store import handle_v3_store
        return handle_v3_store(args, **kw)
    except Exception as e:
        logger.exception("handle_v3_add 未捕获异常")
        return json.dumps({
            "success": False,
            "durable": False,
            "durable_store": "",
            "source_id": "",
            "status": "TOOL_EXCEPTION",
            "error": _safe_err(e),
        }, ensure_ascii=False)
