"""PostgreSQL + pgvector + hnsw 存储层"""
from __future__ import annotations
import contextlib
import json
import logging
import math
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .pg_pool import DEFAULT_LEASE_TIMEOUT


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

logger = logging.getLogger("v3core.pg_store")


def _normalize_timestamp(value: Any) -> datetime | None:
    """Normalize supported timestamp inputs for PostgreSQL timestamptz."""
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = Decimal(text)
        except InvalidOperation:
            try:
                iso_text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
                parsed = datetime.fromisoformat(iso_text)
                return _normalize_timestamp(parsed)
            except (TypeError, ValueError):
                logger.warning("Invalid timestamp %r; using database NOW()", text[:120])
                return None

    if isinstance(value, bool):
        logger.warning("Invalid boolean timestamp %r; using database NOW()", value)
        return None

    try:
        number = float(value if isinstance(value, (int, float, Decimal)) else Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        logger.warning("Invalid timestamp %r; using database NOW()", str(value)[:120])
        return None
    if not math.isfinite(number):
        logger.warning("Non-finite timestamp %r; using database NOW()", value)
        return None

    magnitude = abs(number)
    if magnitude >= 1e17:
        divisor = 1_000_000_000
    elif magnitude >= 1e14:
        divisor = 1_000_000
    elif magnitude >= 1e11:
        divisor = 1_000
    else:
        divisor = 1
    try:
        return datetime.fromtimestamp(number / divisor, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        logger.warning("Out-of-range timestamp %r; using database NOW()", value)
        return None


def _resolve_embed_cfg(embed_cfg):
    """内部 helper — 解析写入路径传入的 embed_cfg, 返 ``(fingerprint, raw_dict)``.

    契约:
    * ``embed_cfg`` 为 None / 空 / 不是 dict → ``ValueError("embed_cfg 缺 _fingerprint ...")``,
      fail-closed, 禁止静默伪造. 调用方必须经 build_embed_cfg(cfg) 工厂构造.
    * ``embed_cfg`` 是 dict 但缺 ``_fingerprint`` → 同上 ValueError.
    * 正常 cfg → 返 ``(_fingerprint, embed_cfg)``.

    不做 HTTP / 不连 PG. 测试可 monkeypatch ``v3core.embedding.build_embed_cfg`` 拦截.

    注: 该 helper 是 fail-closed 入口 — 调用方必须显式决定传不传 cfg.
    如果写入路径要走"无 cfg"分支, 调用方在调用前自己判断, 不要让该 helper
    静默放过.
    """
    if not embed_cfg:
        raise ValueError(
            "embed_cfg 缺失或为空 — 必须经 build_embed_cfg(cfg) 工厂构造, "
            "禁止手拼 dict / 写空值占位"
        )
    if not isinstance(embed_cfg, dict):
        raise ValueError(
            "embed_cfg 必须是 dict (来自 build_embed_cfg(cfg) 工厂), "
            f"实际类型={type(embed_cfg).__name__}"
        )
    fp = embed_cfg.get("_fingerprint") or ""
    if not fp:
        raise ValueError(
            "embed_cfg 缺 _fingerprint — 必须经 build_embed_cfg(cfg) 工厂构造, "
            "禁止手拼 dict"
        )
    return fp, embed_cfg


def _is_undefined_column_error(err: Exception) -> bool:
    """仅判 psycopg2 "undefined_column" / "column ... does not exist" 错误.

    旧 schema 兼容: 当 PG 表缺 ``embed_model`` 列时, 该分支必须回退旧 SQL 而
    **不是**把列写入报错冒泡出去. 但禁止用 catch-all 静默吞掉配置错误.
    """
    msg = str(err).lower()
    if "undefined_column" in msg:
        return True
    if "does not exist" in msg and "column" in msg:
        return True
    return False


class PgEmbedStore:
    """pgvector 存储 — 消息/卡/事实 embedding + hnsw 索引"""

    def __init__(self, config=None, pool=None):
        self._config = config
        self._pool = pool
        self._conn = None
        self._connected = False
        self._closed = False  # facade fence: after close(), lease/_connect must be rejected

    @property
    def pool(self) -> Any:
        """返回注入的连接池（如有）。"""
        return getattr(self, "_pool", None)

    @contextlib.contextmanager
    def lease(self, timeout: float | None = DEFAULT_LEASE_TIMEOUT):
        """获取一个 PostgreSQL 连接 lease 的上下文管理器。

        如果配置了连接池 (_pool)，向池请求 lease 并在退出时确保释放；
        如果是遗留模式 (无 _pool)，使用 _connect() 并在退出时不主动关闭共享的 _conn。
        """
        if getattr(self, "_closed", False):
            raise RuntimeError("PgEmbedStore is closed; lease rejected (fenced)")
        pool = getattr(self, "_pool", None)
        if pool is not None:
            lease = pool.lease(timeout=timeout)
            try:
                yield lease.connection
            finally:
                lease.close()
        else:
            conn = self._connect()
            yield conn

    def _get_pg_config(self) -> dict:
        """兼容 V3Config / dict / None. 总返回 raw dict 给下游 psycopg2.connect.
        如果 V3Config.pg 为 None, 仍返回默认空 dict, _connect 会自然失败并记日志."""
        cfg = self._config
        # V3Config dataclass
        if cfg is not None and hasattr(cfg, "pg") and not isinstance(cfg, dict):
            pgc = getattr(cfg, "pg", None)
            if pgc is None:
                return {}
            return {
                "host": pgc.host,
                "port": pgc.port,
                "database": pgc.database,
                "user": pgc.user,
                "password": pgc.password,
            }
        if isinstance(cfg, dict):
            return cfg.get("storage", {}).get("pg", {}) or {}
        return {}

    def _connect(self):
        """懒连接 pg (遗留直连路径)"""
        if getattr(self, "_closed", False):
            raise RuntimeError("PgEmbedStore is closed; _connect rejected (fenced)")
        if getattr(self, "_pool", None) is not None:
            raise RuntimeError(
                "PgEmbedStore is configured with a connection pool; "
                "direct _connect() is disabled. Callers must use store.lease() instead."
            )
        if self._connected and self._conn:
            return self._conn
        pg_cfg = self._get_pg_config()
        try:
            import psycopg2
            self._conn = psycopg2.connect(
                host=pg_cfg.get("host", "localhost"),
                port=pg_cfg.get("port", 5433),
                dbname=pg_cfg.get("database", "v3embeddings"),
                user=pg_cfg.get("user", "v3user"),
                password=pg_cfg.get("password", ""),
                connect_timeout=5,
            )
            self._conn.autocommit = True
            self._connected = True
            self._ensure_schema()
            return self._conn
        except Exception as e:
            logger.warning("pg 连接失败: %s", _safe_err(e)[:200])
            return None

    def is_connected(self) -> bool:
        try:
            with self.lease() as conn:
                if conn:
                    cur = conn.cursor()
                    cur.execute("SELECT 1")
                    return True
        except Exception:
            pass
        return False

    def open_side_connection(self):
        """Open an INDEPENDENT connection for rare side-writes.

        Failure accounting (``public.embedding_failures``) must be able to record a
        marker even when this store is pool-backed — in which case ``_connect()``
        deliberately refuses and callers are required to ``lease()``. Borrowing a
        lease and holding it across a 10s × 3 embedding attempt would be worse than
        opening one short-lived connection. Callers own the returned connection and
        must close it.

        Returns None (never raises) if a connection cannot be established; the
        caller is responsible for reporting that it could not record the failure.
        """
        try:
            import psycopg2
        except Exception:
            logger.error("open_side_connection: psycopg2 不可用", exc_info=True)
            return None
        pg_cfg = self._get_pg_config()
        try:
            conn = psycopg2.connect(
                host=pg_cfg.get("host", "localhost"),
                port=pg_cfg.get("port", 5433),
                dbname=pg_cfg.get("database", "v3embeddings"),
                user=pg_cfg.get("user", "v3user"),
                password=pg_cfg.get("password", ""),
                connect_timeout=5,
            )
            conn.autocommit = True
            return conn
        except Exception as e:
            logger.warning("open_side_connection 失败: %s", _safe_err(e)[:200])
            return None

    def _ensure_schema(self):
        """建表 + hnsw 索引"""
        if not self._conn:
            return
        cur = self._conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # 2026-08-06: v3_messages/v3_cards/v3_facts/v3_effective 已归档
        # (v3_*_archived_20260806) — 观察者 v2 后 topics/topic_entries/observation_notes
        # 是唯一数据面, 不再重建旧表。旧 search/insert 方法保留仅作兜底 (调用者 try/except)。

    def search(self, query_emb: list[float], kind: str = "card", limit: int = 20) -> list[dict]:
        """hnsw ANN 搜索 -- 返回 [{source_id, title, content, cosine, ...}]"""
        with self.lease() as conn:
            if not conn:
                return []
            table_spec = {
                "card": ("topics", "topic_id", "title", "body", "created_at"),
                "fact": ("topics", "topic_id", "title", "body", "created_at"),
                "message": ("conversation_stream", "id", "content", "content", "timestamp"),
            }
            table, id_col, title_col, preview_col, ts_col = table_spec.get(
                kind, table_spec["card"]
            )
            cur = conn.cursor()
            emb_str = "[" + ",".join(str(x) for x in query_emb) + "]"
            # 2026-08-06 P0-1: SQL 参数化 — 表名/列名来自内部白名单 dict (无用户输入),
            # 用 psycopg2.sql.Identifier 安全拼接; emb_str 走参数绑定, 不再字符串内插。
            from psycopg2 import sql as _psql
            _id_col = id_col
            _ts_col = ts_col
            _stmt = _psql.SQL(
                "SELECT {idc}::text, COALESCE({tcol}, ''), {pcol},"
                " 1 - (embedding <=> %s::vector) AS cosine,"
                " COALESCE({tsc}::text, '')"
                " FROM {tbl} WHERE embedding IS NOT NULL"
                " ORDER BY embedding <=> %s::vector LIMIT %s"
            ).format(
                idc=_psql.Identifier(_id_col),
                tcol=_psql.Identifier(title_col),
                pcol=_psql.Identifier(preview_col),
                tsc=_psql.Identifier(_ts_col),
                tbl=_psql.Identifier(table),
            )
            cur.execute(_stmt, (emb_str, emb_str, limit))
            results = []
            for row in cur.fetchall():
                results.append({
                    "source_id": row[0],
                    "title": row[1],
                    "content_preview": (row[2] or "")[:500],  # 列表展示用短版
                    "content": row[2] or "",  # 2026-08-08: 全文通道 (不再只给 500 字)
                    "cosine": float(row[3]) if row[3] else 0.0,
                    "created_at": row[4] if len(row) > 4 else "",
                    "kind": kind,
                })
            return results

    def insert_message(self, source_id: str, content: str,
                        embedding: list[float] | None = None,
                        metadata: dict | None = None):
        """写入 conversation_stream（2026-08-08 融合：原 v3_messages 已归档，改目标表）。

        字段映射:
          session_id <- metadata.session_id
          role       <- metadata.role
          turn_id    <- metadata.turn_id (int)
          trigger    <- 'live_buffer' (来源标识)
          source     <- 'live_buffer'
          timestamp  <- NOW() (LiveBuffer 批量路径无原始时间戳; sync_turn 直写段保留真实 ts)
          embedding  <- 计算好的向量 (可选)
          tool_calls / tool_results <- metadata 里的 (可选)

        2026-09-07 compression-aware durability (D):
          when ``timestamp`` is a valid value (not None, not fallback to NOW),
          the INSERT is rewritten as
            ``INSERT ... SELECT ... WHERE NOT EXISTS
               (matching session_id + role + timestamp)``
          so a re-attempted live-buffer PG insert for the same durable
          row is a no-op rather than a duplicate row.  Existing-row
          no-op is treated as durable success and committed.

          ``timestamp is None`` keeps the legacy INSERT shape (no dedupe
          predicate), preserving the existing batch path.
        """
        with self.lease() as conn:
            if not conn:
                return
            cur = conn.cursor()
            emb_str = None
            if embedding:
                emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
            metadata = metadata or {}
            role = str(metadata.get("role", ""))
            session_id = str(metadata.get("session_id", ""))
            turn_id = metadata.get("turn_id")
            tc_json = json.dumps(metadata.get("tool_calls") or [])
            tr_json = json.dumps(metadata.get("tool_results") or [])
            # Normalize all supported input forms before binding to timestamptz.
            ts_value = _normalize_timestamp(metadata.get("timestamp"))
            # 2026-09-07 (D): decide whether timestamp is "real" (not the
            # NOW() fallback).  ``_normalize_timestamp`` returns None for
            # unknown / unset inputs; only then do we fall back to NOW()
            # and skip dedupe.  Any other value (datetime / str / epoch)
            # is treated as authoritative and enables the
            # WHERE-NOT-EXISTS dedupe predicate.
            ts_authoritative = ts_value is not None
            if emb_str:
                if ts_authoritative:
                    cur.execute("""
                        INSERT INTO conversation_stream
                            (session_id, role, content, trigger, turn_id, timestamp, source,
                             embedding, tool_calls, tool_results)
                        SELECT %s, %s, %s, 'live_buffer', %s, %s::timestamptz, 'live_buffer',
                               %s::vector, %s::jsonb, %s::jsonb
                        WHERE NOT EXISTS (
                            SELECT 1 FROM conversation_stream
                             WHERE session_id = %s
                               AND role = %s
                               AND timestamp = %s::timestamptz
                        )
                    """, (session_id, role, content, turn_id, ts_value,
                          emb_str, tc_json, tr_json,
                          session_id, role, ts_value))
                else:
                    cur.execute("""
                        INSERT INTO conversation_stream
                            (session_id, role, content, trigger, turn_id, timestamp, source,
                             embedding, tool_calls, tool_results)
                        VALUES (%s, %s, %s, 'live_buffer', %s,
                                COALESCE(%s::timestamptz, NOW()), 'live_buffer',
                                %s::vector, %s::jsonb, %s::jsonb)
                    """, (session_id, role, content, turn_id, ts_value, emb_str, tc_json, tr_json))
            else:
                if ts_authoritative:
                    cur.execute("""
                        INSERT INTO conversation_stream
                            (session_id, role, content, trigger, turn_id, timestamp, source,
                             tool_calls, tool_results)
                        SELECT %s, %s, %s, 'live_buffer', %s, %s::timestamptz, 'live_buffer',
                               %s::jsonb, %s::jsonb
                        WHERE NOT EXISTS (
                            SELECT 1 FROM conversation_stream
                             WHERE session_id = %s
                               AND role = %s
                               AND timestamp = %s::timestamptz
                        )
                    """, (session_id, role, content, turn_id, ts_value,
                          tc_json, tr_json,
                          session_id, role, ts_value))
                else:
                    cur.execute("""
                        INSERT INTO conversation_stream
                            (session_id, role, content, trigger, turn_id, timestamp, source,
                             tool_calls, tool_results)
                        VALUES (%s, %s, %s, 'live_buffer', %s,
                                COALESCE(%s::timestamptz, NOW()), 'live_buffer',
                                %s::jsonb, %s::jsonb)
                    """, (session_id, role, content, turn_id, ts_value, tc_json, tr_json))
            conn.commit()

    def _fire_topics_commit(self, on_topics_commit=None) -> None:
        cb = on_topics_commit
        if cb is None:
            cb = getattr(self, "_on_topics_commit", None)
        if not callable(cb):
            return
        try:
            cb()
        except Exception:
            logger.debug("on_topics_commit callback failed", exc_info=True)

    def insert_card(self, source_id: str, title: str, content: str, category: str,
                    tags: list[str] | None = None, embedding: list[float] | None = None,
                    embed_cfg=None, on_topics_commit=None):
        """写 topics.

        embed_cfg: ``build_embed_cfg(cfg)`` 的工厂结果 — **有 embedding 必须传**,
        fingerprint 从 ``embed_cfg['_fingerprint']`` 取得后写到 ``embed_model`` 列.
        未传 embed_cfg 但传了 embedding → fail-closed, 立即 ValueError, 禁止
        手拼 fingerprint / 硬编码默认 model / 零向量兜底.

        旧 schema 兼容: 表缺 ``embed_model`` 列时, INSERT 走 fallback 旧 SQL,
        静默退化为不写该列 (列 DEFAULT '' 由迁移后 schema 接住).
        """
        committed = False
        with self.lease() as conn:
            if not conn:
                return
            cur = conn.cursor()
            emb_str = None
            fp = ""
            if embedding:
                emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
                fp, _ = _resolve_embed_cfg(embed_cfg)
            if emb_str:
                try:
                    cur.execute("""
                        INSERT INTO topics
                            (topic_id, title, summary, body, keywords, note_ref,
                             embedding, embed_model, status, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s,
                                'active', NOW(), NOW())
                        ON CONFLICT (topic_id) DO UPDATE SET
                            title = EXCLUDED.title,
                            summary = EXCLUDED.summary,
                            body = EXCLUDED.body,
                            keywords = EXCLUDED.keywords,
                            note_ref = EXCLUDED.note_ref,
                            embedding = COALESCE(EXCLUDED.embedding, topics.embedding),
                            embed_model = EXCLUDED.embed_model,
                            status = 'active',
                            updated_at = NOW()
                    """, (source_id, title, content, content, tags or [],
                          category, emb_str, fp))
                except Exception as _e_ins:
                    if _is_undefined_column_error(_e_ins):
                        # 2026-08-22: 先 rollback 再兜底 — psycopg2 事务已 abort,
                        # 直接跑旧 SQL 必然 InFailedSqlTransaction (生产实证的死 fallback).
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        # 旧 schema 兼容: 表缺 embed_model 列, 走不带该列的 fallback
                        logger.warning(
                            "pg_store.insert_card: 表缺 embed_model 列, 走旧 SQL "
                            "(DEFAULT '' 由迁移后 schema 接住): %s", _safe_err(_e_ins)[:120]
                        )
                        cur.execute("""
                            INSERT INTO topics
                                (topic_id, title, summary, body, keywords, note_ref,
                                 embedding, status, created_at, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s::vector,
                                    'active', NOW(), NOW())
                            ON CONFLICT (topic_id) DO UPDATE SET
                                title = EXCLUDED.title,
                                summary = EXCLUDED.summary,
                                body = EXCLUDED.body,
                                keywords = EXCLUDED.keywords,
                                note_ref = EXCLUDED.note_ref,
                                embedding = COALESCE(EXCLUDED.embedding, topics.embedding),
                                status = 'active',
                                updated_at = NOW()
                        """, (source_id, title, content, content, tags or [],
                              category, emb_str))
                    else:
                        raise
            else:
                cur.execute("""
                    INSERT INTO topics
                        (topic_id, title, summary, body, keywords, note_ref, status, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'active', NOW(), NOW())
                    ON CONFLICT (topic_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        summary = EXCLUDED.summary,
                        body = EXCLUDED.body,
                        keywords = EXCLUDED.keywords,
                        note_ref = EXCLUDED.note_ref,
                        status = 'active',
                        updated_at = NOW()
                """, (source_id, title, content, content, tags or [], category))
            conn.commit()
            committed = True
        if committed:
            self._fire_topics_commit(on_topics_commit)

    def delete_card(self, source_id: str, on_topics_commit=None):
        """删除主题卡及其 topic_entries，返回删除的总行数。"""
        removed = 0
        committed = False
        with self.lease() as conn:
            if not conn:
                return 0
            cur = conn.cursor()
            cur.execute("DELETE FROM topic_entries WHERE topic_id = %s", (source_id,))
            removed_entries = cur.rowcount or 0
            cur.execute("DELETE FROM topics WHERE topic_id = %s", (source_id,))
            removed_topics = cur.rowcount or 0
            conn.commit()
            removed = removed_entries + removed_topics
            committed = True
        if committed:
            self._fire_topics_commit(on_topics_commit)
        return removed

    def get_message_context(self, source_id: str) -> dict | None:
        """按 source_id/id 从 conversation_stream 获取消息原文。"""
        try:
            with self.lease() as conn:
                if not conn:
                    return None
                with conn.cursor() as cur:
                    # 主路径: conversation_stream（按 id 或 session_id+content 前缀兜底）
                    # source_id 可能是纯数字 id 或 live/session/msg_id 格式
                    _id = None
                    if source_id.isdigit():
                        _id = int(source_id)
                    if _id is not None:
                        cur.execute(
                            "SELECT content, role, session_id, turn_id, timestamp, tool_calls "
                            "FROM conversation_stream WHERE id = %s LIMIT 1",
                            (_id,),
                        )
                    else:
                        cur.execute(
                            "SELECT content, role, session_id, turn_id, timestamp, tool_calls "
                            "FROM conversation_stream "
                            "WHERE session_id = %s ORDER BY id DESC LIMIT 1",
                            (source_id,),
                        )
                    row = cur.fetchone()
                    if row:
                        return {
                            "content": row[0],
                            "metadata": {
                                "role": row[1],
                                "session_id": row[2],
                                "turn_id": row[3],
                                "timestamp": str(row[4]) if row[4] else None,
                                "tool_calls": row[5],
                            },
                        }
                    return None
        except Exception as e:
            logger.error("get_message_context(%r) 失败: %s", source_id, e)
            return None

    def get_card_by_source_id(self, source_id: str) -> dict | None:
        """按 source_id 从 v3_cards 取卡片内容"""
        try:
            with self.lease() as conn:
                if not conn:
                    return None
                with conn.cursor() as cur:
                    cur.execute("SELECT COALESCE(body, summary, '') FROM topics WHERE topic_id = %s", (source_id,))
                    row = cur.fetchone()
                    if row:
                        return {"content": row[0] if row[0] else ""}
                    return None
        except Exception as e:
            logger.error("get_card_by_source_id(%r) 失败: %s", source_id, e)
            return None

    def close(self):
        self._closed = True
        if getattr(self, "_pool", None) is not None:
            self._conn = None
            self._connected = False
            return
        if self._conn:
            self._conn.close()
            self._conn = None
            self._connected = False

    def get_all_card_embeddings(self) -> tuple[list[str], list[str], list]:
        """获取全部 v3_cards 的 embedding + title (用于聚类去重)

        Returns:
            (source_ids, titles, embeddings_matrix)
            embeddings_matrix 是 list[list[float]]，每行一个 card 的 embedding
            无 embedding 的 card 不返回
        """
        try:
            with self.lease() as conn:
                if not conn:
                    return [], [], []
                cur = conn.cursor()
                cur.execute(
                    "SELECT topic_id, COALESCE(title, ''), embedding::text "
                    "FROM topics WHERE embedding IS NOT NULL"
                )
                sids, titles, embs = [], [], []
                for row in cur.fetchall():
                    sids.append(row[0])
                    titles.append(row[1])
                    emb_str = row[2].strip("[]") if row[2] else ""
                    if emb_str:
                        try:
                            embs.append([float(x) for x in emb_str.split(",")])
                        except Exception:
                            continue
                return sids, titles, embs
        except Exception as e:
            logger.warning("get_all_card_embeddings 失败: %s", _safe_err(e)[:200])
            return [], [], []

    def search_keyword(self, query: str, limit: int = 20) -> list[dict]:
        """关键词搜索 v3_cards — SQL ILIKE，不走文件扫描

        Args:
            query: 搜索词（中文直接子串匹配）
            limit: 返回上限

        Returns:
            [{source_id, title, category, tags, content_preview, cosine=0, rrf_score}, ...]
        """
        terms = [t.strip() for t in query.split() if t.strip() and len(t.strip()) > 1]
        if not terms and query.strip():
            terms = [query.strip()]
        if not terms:
            return []

        try:
            with self.lease() as conn:
                if not conn:
                    return []
                cur = conn.cursor()
                # 用参数化 LIKE 模式避免 psycopg2 的 % 转义问题
                like_conditions = []
                params = []
                for t in terms:
                    pat = f"%{t}%"
                    like_conditions.append(
                        "(title ILIKE %s OR COALESCE(note_ref, status, '') ILIKE %s"
                        " OR EXISTS (SELECT 1 FROM unnest(COALESCE(keywords, ARRAY[]::text[])) AS kw WHERE kw ILIKE %s)"
                        " OR COALESCE(body, '') ILIKE %s)"
                    )
                    params.extend([pat, pat, pat, pat])

                where = " AND ".join(like_conditions) if len(like_conditions) > 1 else like_conditions[0]

                # ORDER BY 也参数化
                title_pat = f"%{terms[0]}%"
                sql = f"""
                    SELECT topic_id, COALESCE(title, ''), COALESCE(note_ref, status, ''),
                           keywords::text, LEFT(COALESCE(body, summary, ''), 200),
                           COALESCE(created_at::text, '')
                    FROM topics
                    WHERE {where}
                    ORDER BY
                        CASE WHEN title ILIKE %s THEN 0 ELSE 1 END,
                        created_at DESC
                    LIMIT %s
                """
                params.append(title_pat)
                params.append(limit)
                cur.execute(sql, params)

                results = []
                for row in cur.fetchall():
                    source_id, title, category, tags_str, preview = row[0], row[1], row[2], row[3], row[4]
                    created_at = row[5] if len(row) > 5 else ""
                    tags = []
                    if tags_str and tags_str.startswith("{"):
                        tags = [t.strip('"') for t in tags_str.strip("{}").split(",") if t.strip()]
                    score = 0.0
                    q = query.lower()
                    tl = title.lower() if title else ""
                    if q in tl:
                        score = 0.8
                    elif tags and any(q in (t.lower() if t else "") for t in tags):
                        score = 0.6
                    else:
                        score = 0.4
                    results.append({
                        "source_id": source_id,
                        "title": title,
                        "category": category,
                        "tags": tags,
                        "content_preview": preview,
                        "cosine": 0.0,
                        "rrf_score": score,
                        "created_at": created_at,
                        "kind": "card",
                    })
                return results
        except Exception as e:
            logger.warning("search_keyword 失败: %s", _safe_err(e)[:200])
            return []

    def insert_effective(self, source_id, pool_role, title, content, embedding, scope_id="", metadata=None,
                         embed_cfg=None):
        """插入 effective_pool

        metadata: 可选 dict — 存关联信息 (如 yin_segment 的 topic_ids)。

        embed_cfg: ``build_embed_cfg(cfg)`` 工厂结果. **有 embedding 必须传**:
        fingerprint 从 ``embed_cfg['_fingerprint']`` 取得后写到 ``embed_model`` 列.
        未传 embed_cfg 但传了 embedding → fail-closed, 立即 ValueError, 禁止
        手拼 fingerprint / 硬编码默认 model / 零向量兜底.

        旧 schema 兼容: yin_paragraphs / observation_notes 表缺 ``embed_model`` 列时,
        INSERT/UPDATE 走 fallback 旧 SQL, 静默退化为不写该列 (列 DEFAULT '' 由迁移后
        schema 接住). 仅 catch psycopg2 "undefined_column" / "column does not exist"
        错误, 不静默吞配置 / 指纹错误.
        """
        with self.lease() as conn:
            if not conn:
                return
            emb_str = "[" + ",".join(str(x) for x in embedding) + "]" if embedding else None
            fp = ""
            if embedding:
                fp, _ = _resolve_embed_cfg(embed_cfg)
            import json as _json
            meta_str = _json.dumps(metadata, ensure_ascii=False) if metadata else None
            # source_qa_range 是 int8range；历史调用方也会传 "all" 等标签。
            # 非范围值写 NULL，原始标签保留在 links，避免类型错误和语义丢失。
            if isinstance(scope_id, str):
                scope_value = scope_id if scope_id[:1] in ("[", "(") and "," in scope_id else None
            else:
                scope_value = scope_id
            with conn.cursor() as cur:
                if pool_role == "yin_segment":
                    try:
                        cur.execute(
                            "UPDATE yin_paragraphs SET section = %s, content = %s, "
                            "embedding = %s::vector, embed_model = %s "
                            "WHERE yin_version = %s",
                            (title or "", content[:5000], emb_str, fp, str(source_id)),
                        )
                        if cur.rowcount == 0:
                            cur.execute(
                                "INSERT INTO yin_paragraphs "
                                "(yin_version, section, content, embedding, "
                                "embed_model, created_at) "
                                "VALUES (%s, %s, %s, %s::vector, %s, NOW())",
                                (str(source_id), title or "", content[:5000], emb_str, fp),
                            )
                    except Exception as _e_ins:
                        if _is_undefined_column_error(_e_ins):
                            # 2026-08-22: 先 rollback 再兜底 (同 insert_card, 防 InFailedSqlTransaction).
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            logger.warning(
                                "pg_store.insert_effective: yin_paragraphs 缺 embed_model 列, "
                                "走旧 SQL: %s", _safe_err(_e_ins)[:120]
                            )
                            cur.execute(
                                "UPDATE yin_paragraphs SET section = %s, content = %s, "
                                "embedding = %s::vector "
                                "WHERE yin_version = %s",
                                (title or "", content[:5000], emb_str, str(source_id)),
                            )
                            if cur.rowcount == 0:
                                cur.execute(
                                    "INSERT INTO yin_paragraphs "
                                    "(yin_version, section, content, embedding, created_at) "
                                    "VALUES (%s, %s, %s, %s::vector, NOW())",
                                    (str(source_id), title or "", content[:5000], emb_str),
                                )
                        else:
                            raise
                else:
                    meta_str = _json.dumps({**(metadata or {}), "title": title, "pool_role": pool_role, "scope_id": scope_id}, ensure_ascii=False)
                    try:
                        cur.execute(
                            "UPDATE observation_notes SET content = %s, source_qa_range = %s, "
                            "links = %s::jsonb, embedding = %s::vector, "
                            "embed_model = %s WHERE version = %s",
                            (content[:5000], scope_value, meta_str, emb_str, fp, str(source_id)),
                        )
                        if cur.rowcount == 0:
                            cur.execute(
                                "INSERT INTO observation_notes "
                                "(version, content, source_qa_range, links, "
                                "embedding, embed_model, created_at) "
                                "VALUES (%s, %s, %s, %s::jsonb, %s::vector, %s, NOW())",
                                (str(source_id), content[:5000], scope_value, meta_str,
                                 emb_str, fp),
                            )
                    except Exception as _e_ins:
                        if _is_undefined_column_error(_e_ins):
                            # 2026-08-22: 先 rollback 再兜底 (同 insert_card, 防 InFailedSqlTransaction).
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            logger.warning(
                                "pg_store.insert_effective: observation_notes 缺 embed_model 列, "
                                "走旧 SQL: %s", _safe_err(_e_ins)[:120]
                            )
                            cur.execute(
                                "UPDATE observation_notes SET content = %s, source_qa_range = %s, "
                                "links = %s::jsonb, embedding = %s::vector WHERE version = %s",
                                (content[:5000], scope_value, meta_str, emb_str, str(source_id)),
                            )
                            if cur.rowcount == 0:
                                cur.execute(
                                    "INSERT INTO observation_notes "
                                    "(version, content, source_qa_range, links, "
                                    "embedding, created_at) "
                                    "VALUES (%s, %s, %s, %s::jsonb, %s::vector, NOW())",
                                    (str(source_id), content[:5000], scope_value, meta_str,
                                     emb_str),
                                )
                        else:
                            raise

            conn.commit()

    def search_effective(self, query_emb, pool_role=None, limit=10):
        """cosine 召回 effective_pool"""
        with self.lease() as conn:
            if not conn:
                return []
            emb_str = "[" + ",".join(str(x) for x in query_emb) + "]"
            with conn.cursor() as cur:
                if pool_role == "yin_segment":
                    sql = (
                        "SELECT yin_version, section, content, "
                        "1 - (embedding <=> %s::vector) AS cosine "
                        "FROM yin_paragraphs WHERE embedding IS NOT NULL "
                    )
                    params = [emb_str, emb_str]
                    sql += "ORDER BY embedding <=> %s::vector LIMIT %s"
                    params.append(limit)
                elif pool_role:
                    sql = (
                        "SELECT version, COALESCE(links->>'title', ''), content, "
                        "1 - (embedding <=> %s::vector) AS cosine "
                        "FROM observation_notes WHERE embedding IS NOT NULL "
                    )
                    params = [emb_str, emb_str]
                    sql += "ORDER BY embedding <=> %s::vector LIMIT %s"
                    params.append(limit)
                else:
                    sql = (
                        "SELECT source_id, title, content, cosine FROM ("
                        "SELECT yin_version AS source_id, section AS title, content, "
                        "1 - (embedding <=> %s::vector) AS cosine "
                        "FROM yin_paragraphs WHERE embedding IS NOT NULL "
                        "UNION ALL "
                        "SELECT version AS source_id, COALESCE(links->>'title', ''), content, "
                        "1 - (embedding <=> %s::vector) AS cosine "
                        "FROM observation_notes WHERE embedding IS NOT NULL"
                        ") effective ORDER BY cosine DESC LIMIT %s"
                    )
                    params = [emb_str, emb_str, limit]
                cur.execute(sql, params)
                return [{"source_id": r[0], "title": r[1], "content": r[2], "cosine": float(r[3])}
                        for r in cur.fetchall()]
