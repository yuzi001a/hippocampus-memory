"""v3_topic_maintain — Topic V2 增量巩固工具包装"""
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
    "description": "Topic V2 增量主题巩固 — 结构化 dossier 提炼与新旧替代状态同步。dry_run=True 预览不执行。",
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
    dry_run = bool((args or {}).get("dry_run", True))
    try:
        from ..config import _resolve_data_dir, resolve_config
        from ..embedding import DURABLE_WRITE_EMBED_POLICY, call_embedding, safe_embed_cfg
        from ..pg_store import PgEmbedStore
        from ..topic_v2 import consolidate_pg_incremental, make_gateway_llm_fn

        pool = kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool")
        if kw.get("runtime_context") and pool is None:
            return json.dumps({
                "success": False,
                "error": "Runtime-backed topic maintenance requires its PgPool owner",
            }, ensure_ascii=False)

        core = kw.get("core")
        cfg = kw.get("effective_config")
        if cfg is None and core is not None:
            cfg = getattr(core, "_config", None) or getattr(core, "config", None)
        if cfg is None:
            try:
                cfg = resolve_config()
            except Exception:
                cfg = None

        eff_embed_fn = kw.get("embed_fn")
        if eff_embed_fn is None:
            embed_cfg = safe_embed_cfg(cfg) if cfg is not None else None
            if embed_cfg is None:
                return json.dumps({
                    "success": False,
                    "error": "Topic V2 maintenance requires embedding configuration",
                }, ensure_ascii=False)
            eff_embed_fn = (
                lambda text: call_embedding(
                    str(text or ""), embed_cfg, policy=DURABLE_WRITE_EMBED_POLICY
                )
                or []
            )

        eff_llm_fn = kw.get("llm_fn")
        if eff_llm_fn is None and "llm_fn" not in kw and cfg is not None:
            try:
                eff_llm_fn = make_gateway_llm_fn(cfg)
            except Exception:
                eff_llm_fn = None

        pg = pool
        if pg is None and core is not None:
            pg = getattr(core, "_pg", None) or getattr(core, "pg", None)
        if pg is None and cfg is not None:
            pg = PgEmbedStore(cfg)
        if pg is None:
            return json.dumps({
                "success": False,
                "error": "Topic V2 maintenance requires a PG store/pool",
            }, ensure_ascii=False)

        state_dir = _resolve_data_dir(cfg)
        result = consolidate_pg_incremental(
            pg,
            embed_fn=eff_embed_fn,
            llm_fn=eff_llm_fn,
            state_dir=state_dir,
            dry_run=dry_run,
        )
        result["engine"] = "topic_v2"
        if not dry_run and int(result.get("upserted_topics") or 0) > 0:
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
