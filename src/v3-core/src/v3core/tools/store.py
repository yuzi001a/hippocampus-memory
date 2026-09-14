"""v3_store tool — auto-register in hand account only for shou_zhang cards"""
from __future__ import annotations
import json
import logging
from pathlib import Path


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

logger = logging.getLogger("v3core.tools.store")

# 手帐机制 A（主动记忆）：这些类别的卡属于"当前状态"型，写卡时提醒登记手帐。
# 提醒式不硬拒绝——agent 看到提醒后决定是否调用 hm_handbook_set 登记。
_HANDBOOK_REMIND_CATEGORIES = {"decisions", "projects", "system", "shou_zhang"}

# explicit-memory boundary: ``v3_store`` 不再自动把手帐卡
# 写到 filesystem ``cards/<cat>/<id>.md``. 它委托给
# ``V3Core.store_card`` → ``ActiveMemoryWriter``, 真值落到
# ``public.explicit_memories``. 旧版描述 "(hand帐: only
# category='shou_zhang' auto-registers in hand account)" 已废弃
# — ``_auto_register`` 仅在 legacy ``durable_store`` 路径触发.
V3_STORE_SCHEMA = {
    "name": "v3_store",
    # A0 explicit-memory opt-in contract:
    # `public.explicit_memories` is opt-in user/host-owned canonical memory.
    # Every call here = an authorized durable write (DML into
    # explicit_memories). Allowed ONLY when the caller has explicit
    # authorization — user asks to remember/store/save/retain a specific
    # durable item, OR an explicitly authorized host workflow (handbook
    # sync, seed import, etc.) requests it. NOT allowed for:
    # development experience / reviewer findings / debugging notes /
    # task status or summary / implementation decisions / inferred
    # preferences or facts / generic lessons / "summarize tonight" /
    # any passive observation derived from session traffic — those are
    # passive/automatic paths (sync_turn, observer, E1, topics) and must
    # never be silently promoted to explicit memory.
    "description": (
        "[2-写卡] OPT-IN explicit-memory write. Each call commits a row into "
        "public.explicit_memories (explicit-memory boundary: delegates to "
        "V3Core.store_card → ActiveMemoryWriter; durable_store='explicit_memories'). "
        "Allowed ONLY when the user explicitly asks to remember / store / save / retain "
        "a specific durable item, or an explicitly authorized host workflow (handbook "
        "sync, seed import, etc.) requests it. NOT authorization: dev experience, "
        "reviewer findings, debugging notes, task status / summary, implementation "
        "decisions, inferred preferences / facts, generic lessons, or 'summarize tonight'. "
        "Passive paths (sync_turn / observer / E1 / topics) own their own derived state "
        "and must not silently promote into explicit memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "category ID"},
            "title": {"type": "string", "description": "title (max 20 chars)"},
            "content": {"type": "string", "description": "body (100-300 chars)"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "optional tags"},
            # explicit-memory boundary SOL-review C: 公共 tool schema 收窄 —
            # ``source_id`` 保留 (caller 显式身份), ``source`` /
            # ``source_j_ids`` 是内部 provenance 字段, 不再作为公共 v3_store
            # property 暴露. 内部兼容: handler (handle_v3_store) 仍走
            # ``_PROVENANCE_KEYS`` 白名单透传给 ``core.store_card`` — caller
            # 显式传 ``source`` / ``source_j_ids`` 时不被丢弃, 但工具
            # schema 不再把这些字段写在公共契约上.
            "source_id": {"type": "string", "description": "optional caller-provided source id (forwarded to core; do not fabricate)"},
        },
        "required": ["category", "title", "content"],
    },
}

# explicit-memory boundary: 可透传给 core.store_card 的"来源溯源"kwargs。
# 工具层绝不伪造这些字段 — caller 没传就不传, 让 core 走确定性的请求身份兜底.
_PROVENANCE_KEYS = ("source_id", "source", "source_j_ids", "when", "where",
                    "who", "why", "confidence", "observation_count")


def _extract_provenance(args: dict) -> dict:
    """从 args 中摘出可透传的来源溯源 kwargs (不修改 args).

    仅挑白名单键且值非空 — 避免把 caller 没传的字段当 None 传下去
    让 core 误以为 caller 显式声明了空值.
    """
    provenance = {}
    for k in _PROVENANCE_KEYS:
        if k in args:
            v = args[k]
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            provenance[k] = v
    return provenance


def _build_receipt(result, *, remind: str = "", auto_warning: str | None = None) -> dict:
    """Build a receipt while tolerating older injected core result shapes."""
    success = bool(getattr(result, "success", False))
    path = str(getattr(result, "path", "") or "")
    warnings = list(getattr(result, "warnings", []) or [])
    if auto_warning:
        warnings.append(auto_warning)
    payload = {
        "success": success,
        "path": path,
        "card": getattr(result, "card", None),
        "message": f"written: {path}{remind}" if success else "",
        "source_id": str(getattr(result, "source_id", "") or ""),
        "durable": bool(getattr(result, "durable", success)),
        "durable_store": str(getattr(result, "durable_store", "") or ""),
        "status": str(getattr(result, "status", "DURABLE_COMMITTED" if success else "") or ""),
        "warnings": warnings,
    }
    if not success:
        payload["message"] = ""
        payload["error"] = str(getattr(result, "error", "") or "")
    return payload


def _auto_register(cat: str, title: str, content: str, result_path: str,
                   config=None, source_id: str = "") -> str | None:
    """Register card in hand account index.

    explicit-memory boundary: 返回 warning 字符串而非静默 swallow. None = 成功; str = 警告文本
    (调用方把它挂到 receipt.warnings 上, 不擦 durable 真值).
    """
    try:
        from ..moc import MOCManager
        from ..config import resolve_config
        cfg = config if config is not None else resolve_config()
        base = cfg.get("basePath", "") or str(Path.home() / ".v3-core" / "profiles" / "default")
        moc = MOCManager(base)
        # Handle mixed path separators (both backslash and forward slash)
        normalized = result_path.replace("\\", "/")
        path_source_id = normalized.rsplit("/", 1)[-1]
        full_source_id = source_id or f"{cat}/{path_source_id}"
        moc.register(
            title=title,
            category=cat,
            pointer=full_source_id,
            summary=content[:150].strip(),
        )
        return None
    except Exception as e:
        logger.warning("auto_register failed (non-fatal): %s", _safe_err(e))
        return f"hand_account auto_register failed: {_safe_err(e)}"


def handle_v3_store(args: dict, **kw) -> str:
    """Delegate to V3Core.store_card, then auto-register in hand account if category is shou_zhang

    explicit-memory boundary 工具层契约:
      - success=True 仅当 PG 真值写入成功 (result.durable=True).
      - 失败时返回 success=False/durable=False/error, 绝不伪装 SQLite-only 成功.
      - 派生副作用 (SQLite/topic/cache/auto_register) 失败只走 warnings,
        不影响 durable 真值.
      - 透传 caller 提供的来源溯源 kwargs (source_id/source/source_j_ids/...);
        caller 没传就让 core 走 filename 兜底, 不伪造 QA id.

    explicit-memory boundary:
      - **保留完整 content**: 不对 ``title`` / ``content`` 做 ``.strip()``
        之外的"规范化" — 全角空格、换行、Markdown 标记、用户刻意保留的
        前后空白都原值透传给 ``core.store_card``. strip 已经够用,
        再做 .replace / .lstrip 会破坏 caller 元数据.
      - 委托给 ``core.store_card`` → ActiveMemoryWriter.
      - 新 writer 路径下 ``durable_store == 'explicit_memories'``:
        **不**再调 ``_auto_register`` 即使 category='shou_zhang'.
        旧版 shou_zhang 走的是 filesystem cards/<cat>/<id>.md, ``_auto_register``
        是文件存在性的派生索引; 新 writer 写的是 ``public.explicit_memories``
        表, 召回走 ``recall_pool`` + ActiveMemoryReader, 不再需要
        MOCManager.register 把 memory_id 重复登记进手帐.
        ``_auto_register`` 保留作为 legacy ``durable_store`` (SQLite / PG cards
        表) 的兼容路径; 新路径直接跳过.
    """
    try:
        cat = args.get("category", "")
        title = args.get("title", "")
        content = args.get("content", "")
        # explicit-memory boundary: title / content 只做 ``.strip()`` 不再修
        # 改任何字符. .strip() 仅用于"前后空白 → 空串"判定非空; 原值
        # (含中间空白 / 换行 / 全角空格 / markdown) 全量透传给 core.
        # ActiveMemoryWriter 内部 canonical id 派生使用 raw content.
        tags = args.get("tags") or []
        provenance = _extract_provenance(args)

        if not cat.strip() or not title.strip() or not content.strip():
            return json.dumps({
                "success": False,
                "durable": False,
                "durable_store": "",
                "source_id": "",
                "status": "INVALID_INPUT",
                "error": "category/title/content required",
            }, ensure_ascii=False)

        from .. import V3Core
        core = kw.get("core") or V3Core(
            effective_config=kw.get("effective_config"),
            pg_pool=kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool"),
        )
        result = core.store_card(cat, title, content, tags=tags, **provenance)

        # 手帐机制 A: 提醒式登记（不硬拒绝）— 但仅在 legacy 写入路径生效.
        remind = ""
        if cat in _HANDBOOK_REMIND_CATEGORIES:
            remind = "；此卡属于主动记忆类({})，建议用 hm_handbook_set 登记手帐保持'当前状态'可被态势总览覆盖".format(cat)

        # explicit-memory boundary: 新 writer 路径 ``durable_store ==
        # 'explicit_memories'`` 跳过 ``_auto_register`` (包括
        # shou_zhang), ``_auto_register`` 是 legacy filesystem /
        # SQLite / PG cards 表的派生索引, 在 explicit_memories 路径
        # 下没有"文件存在"语义可以同步.
        auto_warning: str | None = None
        is_new_writer_path = str(getattr(result, "durable_store", "") or "") == "explicit_memories"
        if not is_new_writer_path and cat == "shou_zhang" and result.success:
            # legacy 路径仍允许 _auto_register — 但前提是 category 仍
            # 是 shou_zhang, 调用层契约不变.
            auto_warning = _auto_register(
                cat, title, content, str(result.path),
                config=kw.get("effective_config"),
                source_id=str(result.source_id or ""),
            )

        payload = _build_receipt(result, remind=remind, auto_warning=auto_warning)
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:
        # 异常路径 — 工具层崩了也要诚实返回失败 receipt, 不留 success=True 半成品
        logger.exception("handle_v3_store 未捕获异常")
        return json.dumps({
            "success": False,
            "durable": False,
            "durable_store": "",
            "source_id": "",
            "status": "TOOL_EXCEPTION",
            "error": _safe_err(e),
        }, ensure_ascii=False)
