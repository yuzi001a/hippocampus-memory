"""v3_cards.db — SQLite 存储层 (替代 cards/<cat>/*.md 碎片文件)

设计目标:
  - 每张卡 1 行, 单文件 v3_cards.db, 彻底消除碎片文件
  - DeepStore 接口保持不变, 外部调用无感
  - 嵌入 BLOB 列替代 card_embeddings.json
  - FTS5 全文索引替代 card_index.json rebuild_index()
  - 归档删除用 archived_at 字段替代 dedup archive/ 目录
"""

from __future__ import annotations
import json
import logging
import sqlite3
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
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

logger = logging.getLogger("v3core.sqlite_store")

# ── 嵌入序列化 ────────────────────────────────────────────
# vector(1024) float32 → BLOB (4096 bytes)
_EMB_DIM = 1024
_EMB_BYTE_COUNT = _EMB_DIM * 4  # float32 = 4 bytes


def _emb_to_blob(vec: list[float]) -> bytes:
    """float32 list → BLOB"""
    if not vec:
        return b""
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_emb(blob: bytes) -> list[float] | None:
    """BLOB → float32 list"""
    if not blob:
        return None
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


# ── 内存在线去重缓存 (旧 card_embeddings.json 语义) ──────
_LOCAL_CARD_EMB: dict[str, list[float]] = {}


# ================================================================
#  SqliteCardStore
# ================================================================

class SqliteCardStore:
    """SQLite 卡库 — 建表 / CRUD / 索引 / 嵌入缓存"""

    DB_NAME = "v3_cards.db"

    def __init__(self, base_path: Path | str):
        self._base = Path(base_path)
        self._db_path = self._base / self.DB_NAME
        self._conn: sqlite3.Connection | None = None
        # P2a follow-up (2026-09-09): serialize all access to the single
        # shared ``sqlite3.Connection`` (created lazily with
        # ``check_same_thread=False`` below). ``RLock`` (not ``Lock``)
        # because methods may legitimately call the ``conn`` property or
        # another locked helper reentrantly (e.g. ``write_card`` issues
        # INSERT + FTS DELETE + FTS INSERT + commit as one atomic unit —
        # all four must hold the same lock so a concurrent thread cannot
        # observe a half-applied card).
        self._lock = threading.RLock()
        self._ensure_schema()

    # ── 连接 ────────────────────────────────────────────────

    @property
    def conn(self) -> sqlite3.Connection:
        # P2a follow-up (2026-09-09): guarded by the instance ``RLock``
        # so the lazy connection is shared safely across threads. The
        # lock covers the ``if self._conn is None`` create path AND the
        # already-created return path — a second thread racing the first
        # during init must wait, not double-create. The created
        # connection uses ``check_same_thread=False`` because we own the
        # serialization ourselves; WAL + ``synchronous=NORMAL`` + row
        # factory are preserved verbatim from the pre-fix implementation.
        with self._lock:
            if self._conn is None:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(
                    str(self._db_path),
                    check_same_thread=False,
                )
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.row_factory = sqlite3.Row
            return self._conn

    # ── Schema ──────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        # Lock guards the schema bootstrap from racing a concurrent
        # reader/writer (the same connection is shared across threads).
        with self._lock:
            c = self.conn
            c.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                source_id       TEXT PRIMARY KEY,
                category        TEXT NOT NULL,
                title           TEXT NOT NULL,
                content         TEXT NOT NULL,
                tags            TEXT DEFAULT '[]',
                embedding       BLOB,

                -- Phase 2 置信度 (原 .meta.json 字段)
                confidence      REAL DEFAULT 0.3,
                observation_count INTEGER DEFAULT 1,
                last_verified_at TEXT,

                -- frontmatter 元数据
                source          TEXT DEFAULT 'extraction',
                source_j_ids    TEXT DEFAULT '[]',
                when_           TEXT DEFAULT '',
                where_          TEXT DEFAULT '',
                who             TEXT DEFAULT '',
                why             TEXT DEFAULT '',

                -- 时间戳
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                archived_at     TEXT           -- 去重归档时间
            )
        """)
        # FTS5 全文索引 (keyword 搜索兜底)
        try:
            c.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
                    title, content, category, tags,
                    content='cards', content_rowid='rowid',
                    tokenize='unicode61'
                )
            """)
        except Exception as e:
            logger.warning("FTS5 创建失败 (不影响核心功能): %s", _safe_err(e)[:100])
        # 索引
        c.execute("CREATE INDEX IF NOT EXISTS idx_cards_category ON cards(category)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cards_created_at ON cards(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cards_confidence ON cards(confidence)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cards_archived ON cards(archived_at)")
        c.commit()

    # ── CRUD ────────────────────────────────────────────────

    def write_card(self, source_id: str, category: str, title: str, content: str,
                   tags: list[str] | None = None, embedding: list[float] | None = None,
                   confidence: float = 0.3, observation_count: int = 1,
                   last_verified_at: str | None = None,
                   source: str = "extraction", source_j_ids: list[str] | None = None,
                   when_: str = "", where_: str = "", who: str = "", why: str = "",
                   archived_at: str | None = None,
                   ) -> str:
        """写/更新一张卡 — INSERT OR REPLACE + 同步 FTS

        P2a follow-up (2026-09-09): the entire multi-statement flow
        (INSERT + FTS DELETE + FTS INSERT + commit) runs inside one
        ``with self._lock`` block so a concurrent thread cannot observe
        a half-applied card and cannot interleave its own write between
        our INSERT and our commit. ``ProgrammingError`` (or any DB error)
        is **not** swallowed here — it propagates to the caller, who
        decides whether to demote to ``DERIVED_WARNING`` (the
        ``write_card_strict`` policy) or surface as a hard failure. On
        retry (``DEDUPLICATED`` outcome) the SQLite derived write is
        still executed — caller drives idempotent upsert over the same
        canonical row, and skipping it would silently diverge from the
        PG canonical source.
        """
        # All work under one lock so the per-card write is atomic from
        # any other thread's perspective on this store instance.
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            tags_json = json.dumps(tags or [], ensure_ascii=False)
            sj_json = json.dumps(source_j_ids or [], ensure_ascii=False)
            emb_blob = _emb_to_blob(embedding) if embedding else None

            c = self.conn
            c.execute("""
                INSERT OR REPLACE INTO cards
                    (source_id, category, title, content, tags, embedding,
                     confidence, observation_count, last_verified_at,
                     source, source_j_ids, when_, where_, who, why,
                     created_at, updated_at, archived_at)
                VALUES (?, ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        COALESCE((SELECT created_at FROM cards WHERE source_id=?), ?),
                        ?, ?)
            """, (
                source_id, category, title, content, tags_json, emb_blob,
                confidence, observation_count, last_verified_at,
                source, sj_json, when_, where_, who, why,
                source_id, now,  # created_at: keep original if exists
                now,             # updated_at: always now
                archived_at,
            ))

            # 同步 FTS
            try:
                # 删旧 FTS 条目然后 INSERT (FTS5 不支持 REPLACE)
                c.execute("DELETE FROM cards_fts WHERE rowid = (SELECT rowid FROM cards WHERE source_id=?)",
                          (source_id,))
                c.execute("""
                    INSERT INTO cards_fts(rowid, title, content, category, tags)
                    SELECT rowid, title, content, category, tags
                    FROM cards WHERE source_id=?
                """, (source_id,))
            except Exception as e:
                logger.debug("FTS 同步警告 (非致命): %s", _safe_err(e)[:100])

            # 更新本地去重缓存
            if embedding:
                rel_key = f"{category}/{source_id}"
                _LOCAL_CARD_EMB[rel_key] = embedding

            c.commit()
            return source_id

    def read_card(self, source_id: str) -> dict | None:
        """读一张卡 (按 source_id) — locked read."""
        with self._lock:
            c = self.conn
            row = c.execute(
                "SELECT * FROM cards WHERE source_id=? AND archived_at IS NULL",
                (source_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_dict(row)

    def read_card_by_rel(self, rel_path: str) -> dict | None:
        """兼容: 按 rel_path (category/filename) 读取 — 从 rel 反解 source_id"""
        # rel_path 格式: "category/filename" → source_id = filename
        parts = rel_path.replace("\\", "/").split("/")
        if len(parts) >= 2:
            source_id = parts[-1].replace(".md", "")
        else:
            source_id = parts[-1].replace(".md", "")
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM cards WHERE source_id=? AND archived_at IS NULL",
                (source_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_dict(row)

    def delete_card(self, source_id: str) -> bool:
        """删除一张卡 — 硬删除 + FTS 清理 — locked."""
        with self._lock:
            c = self.conn
            c.execute("DELETE FROM cards WHERE source_id=?", (source_id,))
            try:
                c.execute("DELETE FROM cards_fts WHERE rowid NOT IN (SELECT rowid FROM cards)")
            except Exception:
                pass
            # 清理去重缓存
            _LOCAL_CARD_EMB.pop(source_id, None)
            c.commit()
            return True

    def archive_card(self, source_id: str) -> bool:
        """软删除 — 替代 dedup archive/ 目录 — locked."""
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            c = self.conn
            c.execute("UPDATE cards SET archived_at=? WHERE source_id=?", (now, source_id))
            try:
                c.execute("DELETE FROM cards_fts WHERE rowid = (SELECT rowid FROM cards WHERE source_id=?)",
                          (source_id,))
            except Exception:
                pass
            c.commit()
            return True

    # ── 批量读取 ────────────────────────────────────────────

    def status(self, category: str | None = None) -> dict:
        """卡库统计 — 替代 iterdir + glob — locked read."""
        with self._lock:
            c = self.conn
            rows = c.execute(
                "SELECT category, COUNT(*) as cnt FROM cards WHERE archived_at IS NULL GROUP BY category"
            ).fetchall()
            by_cat = {r["category"]: r["cnt"] for r in rows}
            total = sum(by_cat.values())
            if category:
                return {"total_cards": by_cat.get(category, 0), "category": category, "by_category": by_cat}
            return {"total_cards": total, "by_category": by_cat}

    def get_index(self) -> dict:
        """带缓存的索引 — 替代 card_index.json

        返回格式兼容: {"files": {rel: {title, tags, category, content_preview, mtime, size}}}
        Locked: the read iterates over all rows; another thread writing a
        card mid-iteration could otherwise see a partial FTS / cards state.
        """
        with self._lock:
            files: dict[str, dict] = {}
            c = self.conn
            rows = c.execute(
                "SELECT source_id, category, title, content, tags, updated_at FROM cards WHERE archived_at IS NULL"
            ).fetchall()
            for r in rows:
                rel = f"{r['category']}/{r['source_id']}"
                tags_list = json.loads(r["tags"] or "[]")
                body_preview = (r["content"] or "")[:500].replace("\n", " ").strip()
                files[rel] = {
                    "title": r["title"],
                    "tags": tags_list,
                    "category": r["category"],
                    "content_preview": body_preview,
                    "mtime": 0,
                    "size": len(r["content"] or ""),
                }
            return {"files": files}

    def rebuild_index(self) -> dict:
        """全量重建索引 — 直接委托 get_index() (不再有文件扫描)"""
        return self.get_index()

    def search_keyword(self, query: str, limit: int = 20) -> list[dict]:
        """PG 离线时的 keyword 兜底 — 基于 FTS5 或 LIKE — locked.

        返回: [{source_id, title, category, tags, content_preview, created_at}]
        """
        terms = [t.strip() for t in query.split() if t.strip() and len(t.strip()) > 1]
        if not terms and query.strip():
            terms = [query.strip()]
        if not terms:
            return []

        results = []
        with self._lock:
            c = self.conn

            # 先试 FTS5
            try:
                fts_query = " OR ".join(f'"{t}"' for t in terms)
                sql = """
                    SELECT c.source_id, c.title, c.category, c.tags,
                           substr(c.content, 1, 200) as preview,
                           c.created_at
                    FROM cards_fts f JOIN cards c ON f.rowid = c.rowid
                    WHERE cards_fts MATCH ? AND c.archived_at IS NULL
                    ORDER BY rank LIMIT ?
                """
                rows = c.execute(sql, (fts_query, limit)).fetchall()
                if rows:
                    for r in rows:
                        results.append({
                            "source_id": r["source_id"],
                            "title": r["title"],
                            "category": r["category"],
                            "tags": json.loads(r["tags"] or "[]"),
                            "content_preview": r["preview"],
                            "cosine": 0,
                            "rrf_score": 0.4,
                            "created_at": r["created_at"],
                        })
                    return results
            except Exception as e:
                logger.debug("FTS5 查询失败, 回退 LIKE: %s", _safe_err(e)[:100])

            # FTS5 不可用 / 无结果时用 LIKE 兜底
            for t in terms:
                pat = f"%{t}%"
                rows = c.execute("""
                    SELECT source_id, title, category, tags,
                           substr(content, 1, 200) as preview,
                           created_at
                    FROM cards
                    WHERE (title LIKE ? OR category LIKE ? OR content LIKE ?)
                      AND archived_at IS NULL
                    ORDER BY
                        CASE WHEN title LIKE ? THEN 0 ELSE 1 END,
                        created_at DESC
                    LIMIT ?
                """, (pat, pat, pat, pat, limit)).fetchall()
                for r in rows:
                    sid = r["source_id"]
                    if not any(h["source_id"] == sid for h in results):
                        results.append({
                            "source_id": sid,
                            "title": r["title"],
                            "category": r["category"],
                            "tags": json.loads(r["tags"] or "[]"),
                            "content_preview": r["preview"],
                            "cosine": 0,
                            "rrf_score": 0.4,
                            "created_at": r["created_at"],
                        })
            return results[:limit]

    # ── 嵌入缓存 (替代 card_embeddings.json) ────────────────

    def load_emb_cache(self) -> None:
        """启动时从 SQLite 加载去重缓存到内存 — locked read."""
        with self._lock:
            _LOCAL_CARD_EMB.clear()
            c = self.conn
            rows = c.execute(
                "SELECT source_id, category, embedding FROM cards WHERE embedding IS NOT NULL"
            ).fetchall()
            for r in rows:
                emb = _blob_to_emb(r["embedding"])
                if emb:
                    rel_key = f"{r['category']}/{r['source_id']}"
                    _LOCAL_CARD_EMB[rel_key] = emb

    def save_emb_cache(self) -> None:
        """持久化嵌入缓存 — 已通过 write_card 实时写入, 此方法仅作保底"""
        pass  # 嵌入在 write_card 时已写 SQLite

    def dump_emb_to_dict(self) -> dict[str, list[float]]:
        """返回 {rel_key: embedding} dict — 供 card_store._LOCAL_CARD_EMB 同步用"""
        with self._lock:
            return dict(_LOCAL_CARD_EMB)

    # ── 工具 ────────────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """sqlite3.Row → dict"""
        d = dict(row)
        # BLOB 反序列化
        if d.get("embedding"):
            d["embedding"] = _blob_to_emb(d["embedding"])
        # JSON 反序列化
        for key in ("tags", "source_j_ids"):
            if isinstance(d.get(key), str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def update_card_embedding(self, source_id: str,
                              embedding: list[float]) -> bool:
        """P2a follow-up (2026-09-09): narrowly-named locked helper for the
        ``session_summary`` embedding backfill path.

        Replaces the prior direct ``store.sqlite.conn.execute(...)`` bypass
        that read the property without holding the instance lock — under
        cross-thread access that path could observe a half-applied card
        or raise ``sqlite3.ProgrammingError`` (``check_same_thread``).

        Contract:

        * Single targeted ``UPDATE cards SET embedding=? WHERE source_id=?``
          followed by commit, all under ``self._lock``.
        * Returns ``True`` if a row was updated, ``False`` otherwise.
        * Caller-driven behavior (no internal default embedding, no
          provenance fabrication): the caller computes the vector and hands
          it in; we just persist it atomically with respect to any other
          thread on this store instance.
        * ``ProgrammingError``/any DB error is **not** swallowed here —
          caller (the ``session_summary`` backfill path) catches and
          decides whether to warn-and-continue.
        """
        if not embedding:
            # No embedding to backfill; nothing to do — caller decides.
            return False
        with self._lock:
            c = self.conn
            cur = c.execute(
                "UPDATE cards SET embedding=? WHERE source_id=?",
                (_emb_to_blob(embedding), source_id),
            )
            c.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        """关闭连接 — locked lifecycle.

        P2a follow-up (2026-09-09): close must take the lock so it cannot
        run concurrently with a still-in-flight write/read on another
        thread (which would otherwise see ``ProgrammingError: Cannot
        operate on a closed database``). After close, ``_conn`` is reset
        to ``None`` so a future ``conn`` access lazily recreates.
        """
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
