"""hm_get — unified read tool + hm_status health tool for v3-core (hippocampus) system.

hm_get accepts ANY source_id (message, card, handbook, file) and returns full content.
hm_status returns basic system health info.
"""
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

logger = logging.getLogger("v3core.tools.get")

# ── hm_get ──────────────────────────────────────────────────────────────────

HM_GET_SCHEMA = {
    "name": "hm_get",
    "description": "[3-读取] Unified read tool — accepts any source_id (message, card, handbook, file) and returns full content.",
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "Source ID to look up. Handles: handbook/xxx keys, PG message IDs, PG card source_ids, and file-based messages.",
            },
        },
        "required": ["source_id"],
    },
}


def handle_hm_get(args: dict, **kw) -> str:
    """Unified read entry point.

    Internal flow:
      1. If source_id starts with 'handbook/' -> HandbookManager.get()
      2. Else -> V3Core().get_message_context() (PG messages -> PG cards -> file fallback)
      3. If step 2 fails, try handbook lookup as supplementary fallback
      4. Return unified JSON result
    """
    try:
        source_id = args.get("source_id", "").strip()
        if not source_id:
            return json.dumps(
                {"success": False, "error": "source_id required"},
                ensure_ascii=False,
            )

        # 1. Handbook path (explicit "handbook/" prefix)
        if source_id.startswith("handbook/"):
            key = source_id[len("handbook/"):]
            from ..handbook import HandbookManager
            h = HandbookManager()
            entry = h.get(key)
            if entry is None:
                return json.dumps(
                    {
                        "success": False,
                        "source_id": source_id,
                        "error": f"handbook entry not found: {key}",
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "success": True,
                    "source_id": source_id,
                    "content": entry.get("content", ""),
                    "source": "handbook",
                    "metadata": {
                        "title": entry.get("title", ""),
                        "tags": entry.get("tags", []),
                        "updated_at": entry.get("updated_at", ""),
                    },
                },
                ensure_ascii=False,
            )

        # 2. V3Core path (PG messages -> PG cards -> file fallback)
        from .. import V3Core

        core = kw.get("core") or V3Core(
            effective_config=kw.get("effective_config"),
            pg_pool=kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool"),
        )
        raw = core.get_message_context(source_id)
        result = json.loads(raw)

        if result.get("success"):
            # Determine source type from response shape
            if "session_id" in result and result.get("session_id"):
                source = "file"
            elif result.get("metadata") and isinstance(result["metadata"], dict) and len(result["metadata"]) > 0:
                source = "message"
            else:
                source = "card"

            return json.dumps(
                {
                    "success": True,
                    "source_id": source_id,
                    "content": result.get("content", ""),
                    "source": source,
                    "metadata": result.get("metadata", {}),
                },
                ensure_ascii=False,
            )

        # 3. Fallback: try handbook lookup for bare keys
        from ..handbook import HandbookManager

        h = HandbookManager()
        entry = h.get(source_id)
        if entry is not None:
            return json.dumps(
                {
                    "success": True,
                    "source_id": source_id,
                    "content": entry.get("content", ""),
                    "source": "handbook",
                    "metadata": {
                        "title": entry.get("title", ""),
                        "tags": entry.get("tags", []),
                        "updated_at": entry.get("updated_at", ""),
                    },
                },
                ensure_ascii=False,
            )

        # 4. Fallback: try reading card file from disk
        try:
            from ..card_store import DeepStore
            from ..config import resolve_config
            _cfg = kw.get("effective_config")
            if _cfg is None:
                _cfg = resolve_config()
            _store = DeepStore(_cfg)
            # Try with .md extension first, then without
            _card = _store.read_card(source_id + ".md")
            if _card is None:
                _card = _store.read_card(source_id)
            if _card is not None:
                return json.dumps(
                    {
                        "success": True,
                        "source_id": source_id,
                        "content": _card.content or "",
                        "source": "card",
                        "metadata": {
                            "title": _card.title or "",
                            "tags": _card.tags or [],
                            "category": _card.category or "",
                        },
                    },
                    ensure_ascii=False,
                )
        except Exception:
            pass

        # 5. All sources exhausted
        return json.dumps(
            {
                "success": False,
                "source_id": source_id,
                "error": f"source_id not found: {source_id}",
            },
            ensure_ascii=False,
        )

    except Exception as e:
        return json.dumps(
            {"success": False, "error": _safe_err(e)},
            ensure_ascii=False,
        )


# ── hm_status ────────────────────────────────────────────────────────────────

HM_STATUS_SCHEMA = {
    "name": "hm_status",
    "description": "[3-读取] 海马系统健康 status — check handbook entries, cards directory, and PG availability.",
    "parameters": {"type": "object", "properties": {}},
}


def handle_hm_status(args: dict, **kw) -> str:
    """Simple health status: handbook entries, cards directory, PG availability."""
    result = {"success": True, "checks": {}}

    # 1. Handbook
    try:
        from ..handbook import HandbookManager

        h = HandbookManager()
        entries = h.list_all()
        result["checks"]["handbook"] = {
            "available": True,
            "entry_count": len(entries),
        }
    except Exception as e:
        result["checks"]["handbook"] = {
            "available": False,
            "error": _safe_err(e)[:200],
        }

    # 2. Cards — SQLite 统计
    try:
        from ..sqlite_store import SqliteCardStore

        cfg = kw.get("effective_config")
        if isinstance(cfg, dict):
            configured_base = cfg.get("basePath", "")
        else:
            configured_base = getattr(cfg, "base_path", "") if cfg is not None else ""
        base = Path(configured_base or Path.home() / ".v3-core" / "profiles" / "default")
        store = SqliteCardStore(base)
        st = store.status()
        result["checks"]["cards"] = {
            "available": True,
            "total_cards": st["total_cards"],
            "categories": list(st["by_category"].keys()),
        }
    except Exception as e:
        result["checks"]["cards"] = {
            "available": False,
            "error": _safe_err(e)[:200],
        }

    # 3. PG availability — 当前数据面探针
    try:
        pool = kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool")
        if pool is not None:
            from ..config import resolve_config
            try:
                from .status import _probe_pg
            except (ImportError, AttributeError):
                from v3core.tools.status import _probe_pg

            cfg = kw.get("effective_config")
            if cfg is None:
                cfg = resolve_config()
            pg_cfg = (cfg.get("storage", {}) or {}).get("pg", {}) or {} if isinstance(cfg, dict) else {}
            pg_status = _probe_pg(pg_cfg, pool=pool)
            result["checks"]["pg"] = {
                "connected": bool(pg_status.get("pg_connected")),
                "counts": pg_status.get("table_counts", {}),
                "probed_tables": pg_status.get("probed_tables", []),
                "missing_tables": pg_status.get("missing_tables", []),
                "error": pg_status.get("error"),
            }
        else:
            if kw.get("runtime_context"):
                result["checks"]["pg"] = {
                    "connected": False,
                    "error": "Runtime-backed hm_status requires its PgPool owner",
                }
                return json.dumps(result, ensure_ascii=False)
            from ..config import resolve_config
            from ..pg_store import PgEmbedStore

            cfg = kw.get("effective_config")
            if cfg is None:
                cfg = resolve_config()
            pg = PgEmbedStore(config=cfg)
            conn = pg._connect()
            if conn:
                cur = conn.cursor()
                counts = {}
                probed = []
                missing = []
                current_tables = (
                    "topics", "conversation_stream", "qa_pairs",
                    "topic_entries", "observation_notes", "yin_paragraphs",
                )
                for table in current_tables:
                    try:
                        cur.execute(f"SELECT count(*) FROM {table}")
                        row = cur.fetchone()
                        counts[table] = int(row[0]) if row else 0
                        probed.append(table)
                    except Exception:
                        missing.append(table)
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                result["checks"]["pg"] = {
                    "connected": not missing,
                    "counts": counts,
                    "probed_tables": probed,
                    "missing_tables": missing,
                    "error": ("missing/unreadable current tables: " + ", ".join(missing)) if missing else None,
                }
            else:
                result["checks"]["pg"] = {"connected": False, "error": "PG not connected"}
    except Exception as e:
        result["checks"]["pg"] = {"connected": False, "error": _safe_err(e)[:200]}

    # 4. Overall health — handbook/cards 是信息段，不能掩盖 PG 断开。
    pg_check = result["checks"].get("pg", {})
    result["healthy"] = bool(pg_check.get("connected") and pg_check.get("probed_tables"))
    if not result["healthy"]:
        result["error"] = "PG current data plane unavailable"

    return json.dumps(result, ensure_ascii=False)
