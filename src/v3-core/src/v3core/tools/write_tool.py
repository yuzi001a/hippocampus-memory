"""hm_write — unified write tool for the v3-core (hippocampus) memory system.
Writes cards to the file system with optional embedding, PG upsert, and handbook integration.
"""
from __future__ import annotations
import inspect
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

logger = logging.getLogger("v3core.tools.write")

HM_WRITE_SCHEMA = {
    "name": "hm_write",
    # A0 explicit-memory opt-in contract: same ownership rule
    # as V3_STORE_SCHEMA / V3_ADD_SCHEMA. Every hm_write → durable write
    # into public.explicit_memories (and optionally also a handbook
    # entry when to_handbook=True). Allowed ONLY when the caller has
    # explicit authorization (user asks to remember / store / save /
    # retain a specific durable item, OR an explicitly authorized host
    # workflow requests it). NOT authorization: dev experience, reviewer
    # findings, debugging notes, task status / summary, implementation
    # decisions, inferred preferences / facts, generic lessons, or
    # 'summarize tonight'.
    "description": (
        "[2-写卡] OPT-IN explicit-memory write with optional handbook "
        "integration (replaces v3_store for new development). Each call "
        "commits a row into public.explicit_memories (canonical durable "
        "store); allowed ONLY when the user explicitly asks to remember / "
        "store / save / retain a specific durable item, or an explicitly "
        "authorized host workflow (handbook sync, seed import, etc.) "
        "requests it. NOT authorization: dev experience, reviewer "
        "findings, debugging notes, task status / summary, implementation "
        "decisions, inferred preferences / facts, generic lessons, or "
        "'summarize tonight'. to_handbook=True adds a derived handbook "
        "entry; that handbook write is non-canonical and follows the same "
        "explicit user/host authorization."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Card category (lessons, decisions, \u72b6\u6001, shou_zhang, ...)",
            },
            "title": {
                "type": "string",
                "description": "Card title (explicit-memory boundary: no length cap; full content preserved)",
            },
            "content": {
                "type": "string",
                "description": "Card body (explicit-memory boundary: no length cap; full content preserved)",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for search",
            },
            "to_handbook": {
                "type": "boolean",
                "description": "Also write to handbook",
            },
            "handbook_key": {
                "type": "string",
                "description": "Key for handbook entry (required if to_handbook=True)",
            },
        },
        "required": ["category", "title", "content"],
    },
}


def _handbook_manager_supports_config() -> bool:
    """explicit-memory boundary: HandbookManager(config=None) 接受 config kwarg — 这里做轻量探测.

    防止硬绑到构造签名 — 万一未来 HandbookManager 改回不带参, 这里也不会
    把 effective_config 错传过去 (inspect 比 try/except 更精确, 不会因为
    其他 ImportError 误判).
    """
    try:
        from ..handbook import HandbookManager
    except Exception:
        return False
    try:
        sig = inspect.signature(HandbookManager.__init__)
    except (TypeError, ValueError):
        return False
    return "config" in sig.parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )


# explicit-memory boundary: 启动期一次探测, 进程内稳定.
_HANDBOOK_SUPPORTS_CONFIG = _handbook_manager_supports_config()


def _build_handbook_manager(effective_config):
    """按构造签名选择传参: HandbookManager(config=effective_config) 或 HandbookManager().

    不动 Hermes MemoryProvider (那是 v3-hermes-plugin 的事, 本任务无权改).
    """
    from ..handbook import HandbookManager
    if _HANDBOOK_SUPPORTS_CONFIG and effective_config is not None:
        return HandbookManager(config=effective_config)
    return HandbookManager()


def handle_hm_write(args: dict, **kw) -> str:
    """Unified write entry point.

    Internal flow (in order, each step non-blocking):
      1. Write card file via V3Core.store_card()  (PG 真值路径)
         -> handles file writing, embedding (try/except), PG upsert (try/except)
      2. If to_handbook, write handbook entry via HandbookManager.set()
         (派生副作用 — 失败走 warning, 不动 durable 真值)
      3. Return combined JSON result

    explicit-memory boundary 工具层契约:
      - success=True 仅当 PG 真值写入成功 (card_result.durable=True).
      - 失败时 success=False/durable=False/error, 绝不伪装 SQLite-only 成功.
      - handbook 写失败只追加 warnings + handbook="failed", durable/status
        保持原状 (PG 真值不被派生副作用覆盖).
      - 透传 effective_config 给 HandbookManager (若其构造签名支持 config=).

    explicit-memory boundary:
      - **保留完整 content**: 不再对 ``title`` / ``content`` 强加任意长度
        上下界 (旧版 ``title > 100`` / ``content ∉ [100, 500]`` 是经验
        启发式, 与 ActiveMemoryWriter canonical 算法无关, 真值落
        ``public.explicit_memories`` 不依赖这些边界). 只校验"非空" —
        实际存储宽度由 schema 决定.
      - **canonical core write 必须先成功** — ``store_card`` 返回
        ``durable=True`` 才继续 handbook 派生; 派生失败绝不擦 PG 真值
        (handbook="failed" 只挂 warning, status 升级为 DERIVED_WARNING).
    """
    try:
        cat = args.get("category", "")
        # explicit-memory boundary: title / content 原值保留, 仅在判空时
        # ``.strip()``. 不再拒 >3000 / 不再强制 [100, 500] 区间.
        title = args.get("title", "")
        content = args.get("content", "")
        tags = args.get("tags") or []
        to_handbook = args.get("to_handbook", False)
        handbook_key = args.get("handbook_key", "").strip()

        # -- validation --

        if not cat.strip() or not title.strip() or not content.strip():
            return json.dumps(
                {
                    "success": False,
                    "durable": False,
                    "durable_store": "",
                    "source_id": "",
                    "status": "INVALID_INPUT",
                    "error": "category/title/content required",
                },
                ensure_ascii=False,
            )

        # explicit-memory boundary: 不再对 title (>100) / content (100..500)
        # 强加任意长度上限. ActiveMemoryWriter / schema 才是真值存储
        # 边界的最终决定者, 工具层做长度门会与 caller 期望的"全文保留"
        # 契约冲突. 这里只保留 handbook_key 的语义校验 (它不是存储宽度,
        # 而是 handbook 自身的 key 必填项).
        if to_handbook and not handbook_key:
            return json.dumps(
                {
                    "success": False,
                    "durable": False,
                    "durable_store": "",
                    "source_id": "",
                    "status": "INVALID_INPUT",
                    "error": "handbook_key required when to_handbook=True",
                },
                ensure_ascii=False,
            )

        # -- Step 1: Write card via V3Core.store_card() (PG 真值路径) --

        from .. import V3Core
        # explicit-memory boundary: 从 args 白名单透传 caller 提供的来源溯源 kwargs,
        # 与 store._PROVENANCE_KEYS 同语义 — caller 没传就不传, 不生成 QA id.
        # 让 core 走 filename 兜底.
        from .store import _extract_provenance
        provenance = _extract_provenance(args)

        core = kw.get("core") or V3Core(
            effective_config=kw.get("effective_config"),
            pg_pool=kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool"),
        )
        card_result = core.store_card(cat, title, content, tags=tags, **provenance)

        card_success = bool(getattr(card_result, "success", False))
        card_durable = bool(getattr(card_result, "durable", card_success))
        if not card_success or not card_durable:
            # explicit-memory boundary: PG 真值失败 — 整 receipt 都标失败, 不写 handbook,
            # 不让派生副作用覆盖源真值.
            return json.dumps(
                {
                    "success": False,
                    "durable": False,
                    "durable_store": str(getattr(card_result, "durable_store", "") or ""),
                    "source_id": str(getattr(card_result, "source_id", "") or ""),
                    "status": str(getattr(card_result, "status", "DURABLE_FAILED") or "DURABLE_FAILED"),
                    "error": str(getattr(card_result, "error", "PG canonical write failed") or "PG canonical write failed"),
                    "path": str(getattr(card_result, "path", "") or ""),
                    "card": getattr(card_result, "card", None),
                    "warnings": list(getattr(card_result, "warnings", []) or []),
                },
                ensure_ascii=False,
            )

        # PG 真值成功 — 构造成功 receipt
        warnings: list[str] = list(getattr(card_result, "warnings", []) or [])
        response = {
            "success": True,
            "durable": True,
            "durable_store": str(getattr(card_result, "durable_store", "") or ""),
            "source_id": str(getattr(card_result, "source_id", "") or ""),
            "status": str(getattr(card_result, "status", "DURABLE_COMMITTED") or "DURABLE_COMMITTED"),
            "warnings": warnings,
            "path": str(getattr(card_result, "path", "") or ""),
            "card": getattr(card_result, "card", None),
            "title": title,
            "preview": content[:200],
        }

        # -- Step 2: Optionally write to handbook (派生副作用 — 失败仅 warning) --

        if to_handbook:
            try:
                h = _build_handbook_manager(kw.get("effective_config"))
                h_result = h.set(handbook_key, title, content, tags=tags)
                if isinstance(h_result, dict) and h_result.get("success"):
                    response["handbook"] = "updated"
                else:
                    response["handbook"] = "failed"
                    warnings.append(
                        f"handbook write returned non-success: {h_result!r}"
                    )
            except Exception as h_e:
                logger.warning("handbook write failed (non-fatal): %s", str(h_e)[:200])
                response["handbook"] = "failed"
                warnings.append(f"handbook write failed: {_safe_err(h_e)}")

            # explicit-memory boundary: handbook 派生副作用失败 — PG durable 已成功,
            # status 升级为 DERIVED_WARNING, warnings 保留, success/durable
            # 不降级 (PG 真值不被派生副作用覆盖). 若 caller 已经拿到更严重
            # 的信号 (如 DEDUPLICATED 表示 PG 已存在), 不强行覆盖.
            if response.get("handbook") == "failed":
                if str(response.get("status") or "") in ("", "DURABLE_COMMITTED"):
                    response["status"] = "DERIVED_WARNING"

        return json.dumps(response, ensure_ascii=False)

    except Exception as e:
        logger.exception("handle_hm_write 未捕获异常")
        return json.dumps(
            {
                "success": False,
                "durable": False,
                "durable_store": "",
                "source_id": "",
                "status": "TOOL_EXCEPTION",
                "error": _safe_err(e),
            },
            ensure_ascii=False,
        )
