"""v3_delete — 删除卡 (按 source_id)

P2a (2026-09-09) clean-boundary:
  - **先查 explicit_memories 表**: 用 ``ActiveMemoryReader`` 按
    ``memory_id`` (== caller source_id) 精确查询, 包括 archived 状态.
  - 表不可用 / DB 错误 → truthful failure, **不**回退 legacy.
  - 命中且 ``hard=False``: 走 ``ActiveMemoryWriter.archive()`` 软归档,
    返回 success=True; 若 already archived → truthful no-op success.
  - ``hard=True``: 拒绝执行 (no DELETE), 返回 truthful hard_rejected;
    **不**回退 legacy.
  - 仅在 explicit_memories 表"确认无行"才走 legacy fallback (SQLite
    archive/delete + filesystem unlink) — 这是 P2a 边界, 防止 legacy
    路径误删 canonical explicit memory.
  - explicit memory 行不走 ``conversation_stream`` DELETE; conversation
    stream 由它自己的来源决定 (legacy 路径保留).

Legacy 路径 (保留): SQLite 归档/删除 → PG cards 表同步 → 文件系统
unlink. 当 explicit_memories 表"确认无匹配行"时, legacy 路径继续
处理 SQLite cards 表 / filesystem 残留.
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

logger = logging.getLogger("v3core.tools.api_delete")

V3_DELETE_SCHEMA = {
    "name": "v3_delete",
    "description": "[4-管理] 删除记忆卡 — 按 source_id (P2a clean-boundary: explicit_memories first, then SQLite/filesystem legacy fallback)",
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "要删除的卡 source_id (必填)",
            },
            "yes": {
                "type": "boolean",
                "description": "确认删除 (必须 true 才执行)",
                "default": False,
            },
            "hard": {
                "type": "boolean",
                "description": "硬删除 (直接从 SQLite 移除). 默认 false=软删除(归档)",
                "default": False,
            },
        },
        "required": ["source_id", "yes"],
    },
}


def _build_writer_and_pool(kw: dict):
    """Construct (ActiveMemoryWriter, ActiveMemoryReader) using the existing
    pool / pg / config from the caller-supplied kw. Does **not** open a
    second pool.

    Returns (writer, reader, pg_or_pool_ref) on success, or raises.

    The reader uses the same pool/pg the writer uses (no second pool).
    The pg reference (for the legacy path below) is the original
    ``self._pg`` or ``self.pg`` instance, never a new connection.
    """
    from .. import V3Core
    from ..active_memory_store import ActiveMemoryWriter, ActiveMemoryReader

    # cfg / pool 走 kw, 不开新 connection.
    cfg = kw.get("effective_config")

    # We need a pg-or-pool reference. Prefer the runtime-backed
    # ``PgEmbedStore`` if one was already constructed (avoid opening a
    # second connection just for explicit_memories lease).
    pg = None
    core = kw.get("core")
    if isinstance(core, V3Core):
        pg = getattr(core, "_pg", None)
        if pg is None:
            try:
                pg = core.pg
            except Exception:
                pg = None
        pool = getattr(core, "_pg_pool", None) or pg
        cfg = cfg if cfg is not None else core.config
    else:
        # No core handle — fall back to kw-provided pool.
        pool = kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool")

    writer = ActiveMemoryWriter(pool=pool, pg=pg, config=cfg)
    reader = ActiveMemoryReader(pool=pool, pg=pg)
    return writer, reader, pg


def handle_v3_delete(args: dict, **kw) -> str:
    """Unified delete: archive (soft) or delete (hard) card by source_id.

    P2a clean-boundary (branch p2a/active-memory-clean-boundary-20260909):

    1. 入口先校验 source_id / yes / hard 三参数语义 (空 / 未确认 /
       硬删除已被禁用 — P2a 边界明确).
    2. 用 ``ActiveMemoryReader.get_by_memory_id`` 查 explicit_memories,
       包括 archived 状态 (该方法不过滤 status). 表不可用 → truthful
       failure, 不走 legacy.
    3. 命中:
         - hard=True → 直接返回 hard_rejected, 不动数据, 不走 legacy.
         - already archived → truthful no-op success (不重复 UPDATE).
         - 其它 → ``ActiveMemoryWriter.archive()`` 软归档, 返回 success.
       命中后**绝不**触发 legacy fallback, 也**绝不**DELETE
       ``conversation_stream`` (P2a 边界 — explicit memory 与对话
       stream 是两个独立的源, 删除 explicit memory 不应连带抹掉原
       对话行).
    4. 未命中 (explicit_memories 表查询成功 + 0 行):
       走 legacy fallback — SQLite archive/delete → PG cards 表同步
       → filesystem unlink. 老路径保留, 不破坏 SQLite/PG cards 现有
       caller.

    Legacy path (Step 4 only):
      - SQLite 主路径 (SqliteCardStore.archive_card / delete_card).
      - PG cards 表同步 (PgEmbedStore.delete_card).
      - filesystem unlink (cards/<cat>/*.md, shou_zhang/, y/).
      - 错误一律非致命, 仅挂 warning.

    Returns JSON with ``success`` / ``durable_store`` / ``mode`` /
    ``source_id`` / ``pg_rows_removed`` / ``legacy_files_removed``.
    """
    raw_source_id = args.get("source_id", "")
    source_id = raw_source_id if isinstance(raw_source_id, str) else str(raw_source_id or "")
    confirmed = args.get("yes", False)
    hard = bool(args.get("hard", False))
    if not source_id:
        return json.dumps({"success": False, "error": "source_id required"}, ensure_ascii=False)
    if not confirmed:
        return json.dumps({"success": False, "error": "set yes=true to confirm deletion"}, ensure_ascii=False)
    try:
        from ..config import resolve_config
        from ..sqlite_store import SqliteCardStore
        from ..active_memory_store import (
            ActiveMemoryWriter,
            ActiveMemoryReader,
        )

        cfg = kw.get("effective_config")
        if cfg is None:
            cfg = resolve_config()
        base = cfg.get("basePath", "") or str(Path.home() / ".v3-core" / "profiles" / "default")
        b_dir = Path(base)

        # ── Step 1-3: explicit_memories strict lifecycle probe ──
        # archive() reads the row including archived status and reports table
        # availability. Only a confirmed no-row result may use legacy fallback.
        try:
            writer, _reader, _pg_ref = _build_writer_and_pool(kw)
            archive_result = writer.archive(source_id, hard=hard)
        except Exception as exc:
            logger.warning("v3_delete: explicit_memories probe failed: %s", exc)
            return json.dumps({
                "success": False,
                "mode": "explicit_memories_unavailable",
                "source_id": source_id,
                "durable_store": "explicit_memories",
                "error": f"explicit_memories probe failed: {_safe_err(exc)}",
            }, ensure_ascii=False)

        if not archive_result.table_available:
            return json.dumps({
                "success": False,
                "mode": "explicit_memories_unavailable",
                "source_id": source_id,
                "durable_store": "explicit_memories",
                "error": str(archive_result.error or "explicit_memories unavailable"),
            }, ensure_ascii=False)

        if archive_result.found:
            if archive_result.hard_rejected:
                return json.dumps({
                    "success": False,
                    "mode": "hard_rejected",
                    "source_id": source_id,
                    "durable_store": "explicit_memories",
                    "hard_rejected": True,
                    "already_archived": archive_result.already_archived,
                    "error": "explicit memory: hard delete is not permitted (use soft archive)",
                }, ensure_ascii=False)
            if archive_result.already_archived:
                return json.dumps({
                    "success": True,
                    "mode": "already_archived",
                    "source_id": source_id,
                    "durable_store": "explicit_memories",
                    "already_archived": True,
                    "message": "explicit memory already archived (no-op)",
                }, ensure_ascii=False)
            if archive_result.archived:
                return json.dumps({
                    "success": True,
                    "mode": "archived",
                    "source_id": source_id,
                    "durable_store": "explicit_memories",
                    "message": "explicit memory archived",
                }, ensure_ascii=False)
            return json.dumps({
                "success": False,
                "mode": "archive_failed",
                "source_id": source_id,
                "durable_store": "explicit_memories",
                "error": str(archive_result.error or "explicit memory archive failed"),
            }, ensure_ascii=False)

        # ── Step 4: explicit_memories 0 行 → 走 legacy fallback ──
        # ── 主路径: SQLite 归档/删除 ──
        sqlite_mode = ""
        sqlite_ok = False
        try:
            store = SqliteCardStore(b_dir)
            if hard:
                store.delete_card(source_id)
                sqlite_mode = "hard_delete"
            else:
                store.archive_card(source_id)
                sqlite_mode = "archive"
            sqlite_ok = True
        except Exception as db_err:
            logger.warning("v3_delete: SQLite 操作失败: %s", db_err)

        # ── PG 同步: 统一走 PgEmbedStore.delete_card，再删除消息流 ──
        pg_deleted = 0
        try:
            from ..pg_store import PgEmbedStore
            pool = kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool")
            if pool is not None:
                pg = PgEmbedStore(config=cfg, pool=pool)
                if pg:
                    pg_deleted += int(pg.delete_card(source_id) or 0)
                    with pg.lease(timeout=5) as conn:
                        if conn:
                            cur = conn.cursor()
                            if source_id.isdigit():
                                cur.execute("DELETE FROM conversation_stream WHERE id = %s", (int(source_id),))
                            else:
                                cur.execute("DELETE FROM conversation_stream WHERE session_id = %s", (source_id,))
                            pg_deleted += cur.rowcount or 0
                            conn.commit()
            else:
                if kw.get("runtime_context"):
                    raise RuntimeError("Runtime-backed v3_delete requires its PgPool owner")
                pg = PgEmbedStore(config=cfg)
                if pg:
                    pg_deleted += int(pg.delete_card(source_id) or 0)
                    conn = pg._connect()
                    if conn:
                        cur = conn.cursor()
                        if source_id.isdigit():
                            cur.execute("DELETE FROM conversation_stream WHERE id = %s", (int(source_id),))
                        else:
                            cur.execute("DELETE FROM conversation_stream WHERE session_id = %s", (source_id,))
                        pg_deleted += cur.rowcount or 0
                        conn.commit()
        except Exception as pg_err:
            logger.warning("v3_delete: PG delete error (non-fatal): %s", pg_err)

        # ── 老式 fallback: 如 SQLite 不可用, 才退到文件系统 unlink ──
        legacy_files_removed = 0
        if not sqlite_ok:
            for root in [b_dir / "cards", b_dir / "shou_zhang", b_dir / "y"]:
                card_path = root / source_id
                if card_path.exists() and card_path.is_file():
                    card_path.unlink(missing_ok=True)
                    legacy_files_removed += 1
                for f in root.rglob(f"*{source_id}*.md"):
                    name_stem = f.stem
                    if name_stem == source_id or name_stem.startswith(source_id + "_"):
                        f.unlink(missing_ok=True)
                        legacy_files_removed += 1

        return json.dumps({
            "success": sqlite_ok or legacy_files_removed > 0,
            "message": f"{sqlite_mode or 'noop'} source_id={source_id}",
            "mode": sqlite_mode or ("legacy_unlink" if legacy_files_removed else "noop"),
            "source_id": source_id,
            "durable_store": "sqlite_or_filesystem",
            "pg_rows_removed": pg_deleted,
            "legacy_files_removed": legacy_files_removed,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)