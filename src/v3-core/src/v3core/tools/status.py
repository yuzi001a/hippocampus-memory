"""v3_status 工具"""
from __future__ import annotations
import json
import logging
from typing import Any


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

logger = logging.getLogger("v3core.tools.status")

V3_STATUS_SCHEMA = {
    "name": "v3_status",
    "description": "[3-读取] 查看记忆系统状态：卡库统计(按类别)、pg 服务状态",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "可选：限定类别ID"},
        },
    },
}


CURRENT_TABLES = (
    "topics", "conversation_stream", "qa_pairs",
    "topic_entries", "observation_notes", "yin_paragraphs",
)


def _probe_pg(pg_cfg: dict | None = None, pool: Any = None) -> dict:
    """短连接或连接池测试 PG，并探测六张当前数据面核心表；失败不抛出。"""
    pg_cfg = pg_cfg or {}
    result = {
        "pg_connected": False,
        "pg_host": pg_cfg.get("host", "localhost"),
        "pg_port": pg_cfg.get("port", 5433),
        "pg_database": pg_cfg.get("database", "v3core"),
        "total_embeddings": None,
        "current_table_count": None,
        "table_counts": {},
        "probed_tables": [],
        "missing_tables": [],
        "error": None,
    }
    lease = None
    conn = None
    try:
        if pool is not None:
            lease = pool.lease(timeout=3)
            conn = lease.connection
        else:
            import psycopg2
            conn = psycopg2.connect(
                host=pg_cfg.get("host", "localhost"),
                port=pg_cfg.get("port", 5433),
                dbname=pg_cfg.get("database", "v3core"),
                user=pg_cfg.get("user", "v3user"),
                password=pg_cfg.get("password", ""),
                connect_timeout=3,
            )
        try:
            cur = conn.cursor()
            for table in CURRENT_TABLES:
                try:
                    cur.execute(f"SELECT count(*) FROM {table}")
                    row = cur.fetchone()
                    result["table_counts"][table] = int(row[0]) if row else 0
                    result["probed_tables"].append(table)
                except Exception as e:
                    result["missing_tables"].append(table)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            result["current_table_count"] = result["table_counts"].get("topics")
            result.update({table: result["table_counts"].get(table) for table in CURRENT_TABLES})
            result["pg_connected"] = not result["missing_tables"]
            if result["missing_tables"]:
                result["error"] = "missing/unreadable current tables: " + ", ".join(result["missing_tables"])
        finally:
            if lease is not None:
                try:
                    lease.close()
                except Exception:
                    pass
            elif conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception as e:
        result["error"] = _safe_err(e)[:200]
        logger.warning("v3_status pg_probe 失败: %s", result["error"])
    return result


def handle_v3_status(args: dict | None = None, **kw) -> str:
    try:
        from ..card_store import DeepStore
        from ..config import resolve_config
        cfg = kw.get("effective_config")
        if cfg is None:
            cfg = resolve_config()
        store = DeepStore(cfg)
        category = args.get("category") if isinstance(args, dict) else None
        s = store.status(category)

        # PG 状态探测 — 用 cfg 里 storage.pg 的配置
        pg_cfg = cfg.get("storage", {}).get("pg", {}) or {}
        pool = kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool")
        if kw.get("runtime_context") and pool is None:
            raise RuntimeError("Runtime-backed status requires its PgPool owner")
        pg_status = _probe_pg(pg_cfg, pool=pool)

        try:
            from ..llmstatus import build_llm_check
            llm_info = build_llm_check(cfg)
        except Exception as e:
            llm_info = {"error": _safe_err(e)}

        return json.dumps(
            {"success": True, **s, "pg_status": pg_status, "llm": llm_info},
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)
