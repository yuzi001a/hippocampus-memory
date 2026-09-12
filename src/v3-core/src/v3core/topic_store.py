"""topic_store.py — 主题聚合四张核心表

topic_blocks   主题块主表（ID, 标题, 摘要, 正文, 关键词, 向量, 置信度, 状态）
topic_entries  条目元数据（谁说了什么，关联哪个主题，工具调用结果）
topic_edges    主题间交叉引用
topic_buffer   写入缓冲（未 flush 的原始提炼）
"""
import sqlite3, json, time, os, hashlib, uuid, logging
from typing import Optional
from datetime import datetime


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

logger = logging.getLogger("v3core.topic_store")


def _resolve_embed_cfg(embed_cfg):
    """内部 helper — 与 pg_store 同语义: 解析 ``build_embed_cfg`` 工厂结果.

    * ``embed_cfg`` 为 None / 空 / 不是 dict → ``ValueError`` (fail-closed).
    * 缺 ``_fingerprint`` → ``ValueError`` (fail-closed).
    * 正常 cfg → 返 ``(_fingerprint, embed_cfg)``.

    调用方需在有 embedding 时才传 cfg; 不传 cfg 表示走"无 embedding 旧分支",
    不应走该 helper.

    不做 HTTP / 不连 PG. 测试可 monkeypatch ``v3core.embedding.build_embed_cfg`` 拦截.
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
    """旧 schema 兼容: 仅判 psycopg2 "undefined_column" / "column ... does not exist".

    严禁 catch-all 静默吞掉配置错误.
    """
    msg = str(err).lower()
    if "undefined_column" in msg:
        return True
    if "does not exist" in msg and "column" in msg:
        return True
    return False


# 默认 SQLite 路径 — 2026-08-08: 改走数据目录 (源码树禁止写运行时状态, P0 隔离修复)
# 旧值: os.path.dirname(os.path.dirname(os.path.dirname(__file__))) / 'v3_topic.db' → 源码树
# 生产/实验 bind 挂载共享源码树 → 状态文件必须放各自数据目录
def _default_db_path() -> str:
    try:
        from .config import _resolve_data_dir
        return str(_resolve_data_dir() / "v3_topic.db")
    except Exception:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "v3_topic.db",
        )

DEFAULT_DB = _default_db_path()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS topic_blocks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '[]',
    embedding BLOB,
    category TEXT DEFAULT '',
    confidence REAL DEFAULT 0.5,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_evidence_at TEXT
);

CREATE TABLE IF NOT EXISTS topic_entries (
    id TEXT PRIMARY KEY,
    topic_id TEXT REFERENCES topic_blocks(id),
    seq INTEGER NOT NULL DEFAULT 0,
    timestamp TEXT NOT NULL,
    source TEXT DEFAULT 'user',
    message_id TEXT,
    question TEXT DEFAULT '',
    answer TEXT DEFAULT '',
    tool_calls TEXT DEFAULT '[]',
    tool_results TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0.5,
    supersedes_id TEXT,
    is_qa INTEGER DEFAULT 0,
    qa_partner_id TEXT
);

CREATE TABLE IF NOT EXISTS topic_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES topic_blocks(id),
    target_id TEXT NOT NULL REFERENCES topic_blocks(id),
    relation TEXT DEFAULT 'related',
    weight REAL DEFAULT 0.5,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT,
    timestamp TEXT NOT NULL,
    summary TEXT DEFAULT '',
    primary_topic_id TEXT,
    primary_action TEXT,
    new_topic_draft TEXT,
    related_links TEXT DEFAULT '[]',
    qa_pairs TEXT DEFAULT '[]',
    raw_output TEXT
);

CREATE INDEX IF NOT EXISTS idx_entries_topic ON topic_entries(topic_id);
CREATE INDEX IF NOT EXISTS idx_edges_source ON topic_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON topic_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_buffer_time ON topic_buffer(timestamp);
"""


class TopicStore:
    def __init__(self, db_path: str = DEFAULT_DB, pg_conn=None, topics_table: str = "topics",
                 *, strict_pg: bool = False):
        self.db_path = db_path
        self.pg_conn = pg_conn  # psycopg2 connection or None
        self.topics_table = topics_table  # PG 表名 (隔离回放用)
        self.strict_pg = bool(strict_pg)  # 严格 PG 双写事务: PG 失败 → SQLite rollback + 抛
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=OFF")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def _now(self) -> str:
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _topic_id(self, name: str) -> str:
        return 't_' + hashlib.md5(name.encode()).hexdigest()[:8]

    # ─── topic_blocks ───

    def get_all_topics(self):
        """返回主题列表，兼容不同 topic_blocks schema（动态列检测）"""
        # 检测实际存在的列
        actual_cols = set(r[1] for r in self.conn.execute("PRAGMA table_info(topic_blocks)").fetchall())
        # 默认查询列（保证所有 schema 都有）
        base_cols = ['id', 'title', 'summary', 'keywords', 'created_at']
        query_cols = [c for c in base_cols if c in actual_cols]
        if not query_cols:
            return []
        order_col = 'updated_at' if 'updated_at' in actual_cols else 'created_at'
        rows = self.conn.execute(
            f"SELECT {', '.join(query_cols)} FROM topic_blocks ORDER BY {order_col} DESC"
        ).fetchall()
        return [dict(zip(query_cols, r)) for r in rows]

    def get_topic(self, topic_id: str):
        row = self.conn.execute(
            "SELECT * FROM topic_blocks WHERE id=?", (topic_id,)
        ).fetchone()
        if not row: return None
        cols = [d[0] for d in self.conn.execute("PRAGMA table_info(topic_blocks)").fetchall()]
        return dict(zip(cols, row))

    def upsert_topic(self, title: str, summary: str = '', body: str = '',
                     keywords: list = None, category: str = '',
                     embedding=None, topic_id: str = None,
                     pg_conn=None, embed_cfg=None) -> str:
        now = self._now()
        tid = topic_id or self._topic_id(title)
        kw = json.dumps(keywords or [], ensure_ascii=False)

        # ── Auto-calc embedding: 修复幽灵卡 ─────────────────────
        # 91/143 topic 的 embedding 为空, TopicRecall.match() 看不到.
        # 这里自动从 title + summary 算一个; 算不出来不阻塞写入.
        if not embedding:
            try:
                from .embedding import call_embedding, safe_embed_cfg
                from .config import resolve_config
                kw_str = ' '.join(keywords or []) if isinstance(keywords, list) else ''
                text = f"{title}. {summary or ''} {(body or '')[:800]} {kw_str}".strip()
                # 阶段1 (2026-08-20): 唯一构造入口 — safe_embed_cfg 工厂.
                # 缺 model/endpoint → None (disabled), 走日志 + return, 不发请求.
                _cfg = resolve_config()
                embed_cfg = safe_embed_cfg(_cfg)
                if embed_cfg is None:
                    logger.debug(
                        "topic_store.upsert_topic: embed 未配置或缺 model/endpoint, "
                        "跳过自动 embedding"
                    )
                else:
                    emb_list = call_embedding(text[:1000], embed_cfg)
                    if emb_list:
                        embedding = json.dumps(emb_list, ensure_ascii=False)
            except ValueError:
                # 配置/调用契约错误必须 fail-closed，不能伪装成 disabled。
                raise
            except Exception as _e:
                # 网络/服务故障仍保持历史不阻塞语义。
                import logging as _log
                _log.getLogger("v3core.topic_store").warning(
                    "auto-embedding failed for topic %r: %s",
                    (title or "")[:30], str(_e)[:120]
                )
                embedding = None

        existing = self.conn.execute("SELECT id FROM topic_blocks WHERE id=?", (tid,)).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE topic_blocks SET title=?, summary=?, body=?, keywords=?, "
                "category=?, embedding=?, updated_at=? WHERE id=?",
                (title, summary, body, kw, category, embedding, now, tid)
            )
        else:
            self.conn.execute(
                "INSERT INTO topic_blocks (id, title, summary, body, keywords, "
                "category, embedding, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, title, summary, body, kw, category, embedding, now, now)
            )

        # --- PG 分支: 双写 PG topics 表 ---
        _pg = pg_conn or self.pg_conn
        strict_active = bool(self.strict_pg) and _pg is not None

        if not strict_active:
            # 旧行为: 先提交 SQLite, PG 失败只 warning 不阻塞.
            self.conn.commit()
            if _pg is not None:
                try:
                    self._upsert_topic_pg(_pg, tid, title, summary, body, keywords,
                                          embedding, embed_cfg=embed_cfg, commit=True)
                except ValueError:
                    # embedding/fingerprint 契约错误不能被 PG 双写降级吞掉。
                    raise
                except Exception as e:
                    logger.warning(
                        "PG upsert_topic failed (SQLite still written): %s", _safe_err(e)[:100]
                    )
        else:
            # strict_pg 严格事务: SQLite 与 PG 必须原子提交, 失败则一起回滚.
            try:
                self._upsert_topic_pg(_pg, tid, title, summary, body, keywords,
                                      embedding, embed_cfg=embed_cfg, commit=False)
                _pg.commit()
                self.conn.commit()
            except ValueError:
                # embedding/fingerprint 契约错误必须 fail-closed, 显式回滚.
                try:
                    _pg.rollback()
                except Exception:
                    pass
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise
            except Exception as e:
                try:
                    _pg.rollback()
                except Exception:
                    pass
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise RuntimeError(
                    f"strict_pg upsert_topic failed: {type(e).__name__}: "
                    f"{_safe_err(e)[:200]}"
                ) from e
        # --- PG 分支结束 ---

        return tid

    def _upsert_topic_pg(self, pg, tid, title, summary, body, keywords, embedding,
                         embed_cfg=None, *, commit: bool = True):
        """写入 PG topics 表 (表名可配置, 隔离回放走 topics_exp).

        embedding 格式转换: SQLite 存 JSON 字符串 / list → PG vector。
        keywords 是 TEXT[]: psycopg2 自动把 Python list 转成 PG array。

        embed_cfg: ``build_embed_cfg(cfg)`` 工厂结果. **有 embedding 必须传**:
        fingerprint 从 ``embed_cfg['_fingerprint']`` 取得后写到 ``embed_model`` 列.
        未传 embed_cfg 但传了 embedding → fail-closed, 立即 ValueError.

        旧 schema 兼容: topics 表缺 ``embed_model`` 列时, INSERT 走 fallback 旧 SQL,
        静默退化为不写该列. 仅 catch psycopg2 "undefined_column" / "column does not exist".

        commit: 默认 True, 写完后自动 ``pg.commit()``。
            False 时不调 commit — 用于 strict_pg 严格事务路径 (与后续 PG 操作
            共用一个 PG 事务, 由调用方最终统一 commit 一次)。
        """
        # embedding 格式转换
        emb_for_pg = None
        if embedding:
            if isinstance(embedding, str):
                try:
                    emb_for_pg = json.loads(embedding)  # JSON string → list
                except (json.JSONDecodeError, TypeError):
                    emb_for_pg = None
            elif isinstance(embedding, (list, tuple)):
                emb_for_pg = list(embedding)
            # pgvector 适配器处理 list[float] → vector

        # fingerprint: 有 embedding 必须有合法 cfg; 缺 cfg → fail-closed.
        fp = ""
        if emb_for_pg:
            fp, _ = _resolve_embed_cfg(embed_cfg)

        from pgvector.psycopg2 import register_vector
        # P1.2-B: pgvector 0.5.0 的 register_vector 只接受原生 psycopg2
        # connection/cursor (isinstance(conn, connection) 或 conn.connection);
        # _PoolPgConnection 两者都不是 → 必须解包 raw, 不依赖 duck typing。
        _pg_raw = pg._connect() if hasattr(pg, "_connect") else pg
        register_vector(_pg_raw)

        if emb_for_pg:
            try:
                with pg.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO {topics_table} (
                            topic_id, title, summary, body, keywords, embedding,
                            embed_model, status, created_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', NOW(), NOW())
                        ON CONFLICT (topic_id) DO UPDATE SET
                            title=EXCLUDED.title,
                            summary=EXCLUDED.summary,
                            body=EXCLUDED.body,
                            keywords=EXCLUDED.keywords,
                            embedding=COALESCE(EXCLUDED.embedding, {topics_table}.embedding),
                            embed_model=EXCLUDED.embed_model,
                            updated_at=NOW()
                        """.format(topics_table=self.topics_table),
                        (
                            tid, title, summary or '', body or '',
                            keywords or None,  # TEXT[] — psycopg2 handles list→array
                            emb_for_pg,         # vector — pgvector handles list→vector
                            fp,
                        ),
                    )
            except Exception as _e_ins:
                if _is_undefined_column_error(_e_ins):
                    logger.warning(
                        "topic_store._upsert_topic_pg: 表缺 embed_model 列, 走旧 SQL "
                        "(DEFAULT '' 由迁移后 schema 接住): %s", _safe_err(_e_ins)[:120]
                    )
                    with pg.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO {topics_table} (
                                topic_id, title, summary, body, keywords, embedding,
                                status, created_at, updated_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, 'active', NOW(), NOW())
                            ON CONFLICT (topic_id) DO UPDATE SET
                                title=EXCLUDED.title,
                                summary=EXCLUDED.summary,
                                body=EXCLUDED.body,
                                keywords=EXCLUDED.keywords,
                                embedding=COALESCE(EXCLUDED.embedding, {topics_table}.embedding),
                                updated_at=NOW()
                            """.format(topics_table=self.topics_table),
                            (
                                tid, title, summary or '', body or '',
                                keywords or None,
                                emb_for_pg,
                            ),
                        )
                else:
                    raise
        else:
            with pg.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO {topics_table} (
                        topic_id, title, summary, body, keywords, status,
                        created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 'active', NOW(), NOW())
                    ON CONFLICT (topic_id) DO UPDATE SET
                        title=EXCLUDED.title,
                        summary=EXCLUDED.summary,
                        body=EXCLUDED.body,
                        keywords=EXCLUDED.keywords,
                        updated_at=NOW()
                    """.format(topics_table=self.topics_table),
                    (
                        tid, title, summary or '', body or '',
                        keywords or None,
                    ),
                )
        if commit:
            pg.commit()

    def delete_topic(self, topic_id: str):
        self.conn.execute("DELETE FROM topic_edges WHERE source_id=? OR target_id=?", (topic_id, topic_id))
        self.conn.execute("DELETE FROM topic_entries WHERE topic_id=?", (topic_id,))
        self.conn.execute("DELETE FROM topic_blocks WHERE id=?", (topic_id,))
        self.conn.commit()

    def merge_topic(self, source_id: str, target_id: str,
                    new_title: str = '', new_summary: str = '', new_body: str = '',
                    pg=None) -> tuple[bool, str]:
        """2026-08-06 E1 整理归类: 把 source 主题合并进 target（结构性操作，不调 LLM）。

        调用方（E1 Step 1）负责 LLM 判断 + 合成 new_body/new_summary；
        本方法只做: entries 迁移 → target 更新 → source 清理（SQLite + PG 双端）。
        """
        src = self.get_topic(source_id)
        tgt = self.get_topic(target_id)
        if not src:
            return False, f"source 不存在: {source_id}"
        if not tgt:
            return False, f"target 不存在: {target_id}"
        if source_id == target_id:
            return False, "source == target"
        _pg = pg or self.pg_conn

        # 1. entries 迁移（SQLite）
        moved = self.conn.execute(
            "UPDATE topic_entries SET topic_id=? WHERE topic_id=?", (target_id, source_id)
        ).rowcount
        # 2. PG entries 迁移（target 在 PG 已存在）
        if _pg is not None:
            try:
                with _pg.cursor() as cur:
                    cur.execute(
                        "UPDATE topic_entries SET topic_id=%s WHERE topic_id=%s",
                        (target_id, source_id),
                    )
            except Exception as e:
                logger.warning("merge: PG entries 迁移失败: %s", _safe_err(e)[:150])
        # 3. target 更新（合成内容 + keywords 合并 + embedding 自动重算）
        kw_tgt = set(
            json.loads(tgt.get("keywords", "[]"))
            if isinstance(tgt.get("keywords"), str)
            else (tgt.get("keywords") or [])
        )
        kw_src = set(
            json.loads(src.get("keywords", "[]"))
            if isinstance(src.get("keywords"), str)
            else (src.get("keywords") or [])
        )
        self.upsert_topic(
            title=new_title or tgt.get("title", ""),
            summary=new_summary or tgt.get("summary", ""),
            # 结构性合并: 未传 new_body 时拼接双方 body（source 内容不丢）
            body=new_body or (
                (tgt.get("body") or "") + "\n\n---\n" + (src.get("body") or "")
            ).strip(),
            keywords=sorted(kw_tgt | kw_src),
            topic_id=target_id,
            pg_conn=_pg,
        )
        # 4. source 清理（SQLite 全删 + PG 删主题与残留 entries）
        self.delete_topic(source_id)
        if _pg is not None:
            try:
                with _pg.cursor() as cur:
                    cur.execute("DELETE FROM topics WHERE topic_id=%s", (source_id,))
                    cur.execute("DELETE FROM topic_entries WHERE topic_id=%s", (source_id,))
            except Exception as e:
                logger.warning("merge: PG source 清理失败: %s", _safe_err(e)[:150])
        self.conn.commit()
        return True, f"merged {source_id} → {target_id} (entries moved: {moved})"

    # ─── topic_entries ───

    def add_entry(self, topic_id: str, source: str, question: str = '',
                  answer: str = '', tool_calls: list = None, tool_results: list = None,
                  message_id: str = '', confidence: float = 0.5,
                  pg_conn=None, embedding=None, embed_cfg=None,
                  source_qa_id=None):

        # 2026-08-22 G1B: source_qa_id 仅"单条 QA 直挂"由调用方显式传入;
        # 派生类 entry (obs:fact / 压缩段 / 多 QA 合成) 默认 None 不伪造来源。
        # 生产库缺该列时 _add_entry_pg 自动 rollback + 剥列降级 (见其 docstring)。
        now = self._now()
        eid = 'e_' + uuid.uuid4().hex[:12]
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0)+1 FROM topic_entries WHERE topic_id=?",
            (topic_id,)
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO topic_entries (id, topic_id, seq, timestamp, source, "
            "message_id, question, answer, tool_calls, tool_results, confidence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (eid, topic_id, seq, now, source, message_id, question, answer,
             json.dumps(tool_calls or []), json.dumps(tool_results or []), confidence)
        )

        # --- PG branch: dual-write to PG topic_entries ---
        _pg = pg_conn or self.pg_conn
        strict_active = bool(self.strict_pg) and _pg is not None

        if not strict_active:
            # Old behavior: commit SQLite first, PG failure logs warning only.
            self.conn.commit()
            if _pg is not None:
                try:
                    self._add_entry_pg(_pg, topic_id, source, question, answer, now,
                                       embedding=embedding, embed_cfg=embed_cfg,
                                       commit=True, source_qa_id=source_qa_id)
                except ValueError:
                    # embedding/fingerprint contract error must NOT be swallowed.
                    raise
                except Exception as e:
                    err_msg = _safe_err(e)[:150]
                    # FK violation -> topic missing in PG, auto-heal then retry.
                    if 'foreign key' in err_msg.lower() or 'violates' in err_msg.lower():
                        try:
                            topic = self.get_topic(topic_id)
                            if topic:
                                logger.info(
                                    "auto-heal: upsert missing topic %r to PG before add_entry",
                                    topic_id
                                )
                                self._upsert_topic_pg(
                                    _pg, topic_id,
                                    topic.get('title', ''),
                                    topic.get('summary', ''),
                                    topic.get('body', ''),
                                    json.loads(topic.get('keywords', '[]'))
                                    if isinstance(topic.get('keywords'), str) else
                                    (topic.get('keywords') or []),
                                    topic.get('embedding'),
                                    embed_cfg=embed_cfg,
                                    commit=True,
                                )
                                self._add_entry_pg(_pg, topic_id, source,
                                                   question, answer, now,
                                                   embedding=embedding,
                                                   embed_cfg=embed_cfg,
                                                   commit=True,
                                                   source_qa_id=source_qa_id)
                                logger.info("auto-heal OK for topic %r", topic_id)
                            else:
                                logger.warning(
                                    "auto-heal: topic %r not found in SQLite, "
                                    "PG entry skipped", topic_id
                                )
                        except ValueError:
                            # auto-heal cannot swallow embedding/fingerprint contract error.
                            raise
                        except Exception as e2:
                            logger.warning(
                                "PG add_entry failed (FK auto-heal also failed): %s",
                                str(e2)[:100]
                            )
                    else:
                        logger.warning(
                            "PG add_entry failed (SQLite still written): %s", err_msg
                        )
        else:
            # strict_pg: SQLite + PG must commit atomically, failures roll back together.
            # FK violation -> same PG transaction auto-heal then retry, single final commit.
            try:
                try:
                    self._add_entry_pg(_pg, topic_id, source, question, answer, now,
                                       embedding=embedding, embed_cfg=embed_cfg,
                                       commit=False,
                                       source_qa_id=source_qa_id)
                except Exception as first_err:
                    err_msg = _safe_err(first_err)[:150].lower()
                    is_fk = ('foreign key' in err_msg or 'violates' in err_msg)
                    if not is_fk:
                        raise
                    # auto-heal path within the same PG transaction.
                    topic = self.get_topic(topic_id)
                    if not topic:
                        # SQLite has no topic -> strict mode cannot silently skip, raise.
                        raise RuntimeError(
                            f"strict_pg add_entry FK auto-heal failed: "
                            f"topic {topic_id!r} not in SQLite, cannot upsert to PG"
                        ) from first_err
                    logger.info(
                        "strict_pg auto-heal: upsert missing topic %r to PG before add_entry",
                        topic_id
                    )
                    self._upsert_topic_pg(
                        _pg, topic_id,
                        topic.get('title', ''),
                        topic.get('summary', ''),
                        topic.get('body', ''),
                        json.loads(topic.get('keywords', '[]'))
                        if isinstance(topic.get('keywords'), str) else
                        (topic.get('keywords') or []),
                        topic.get('embedding'),
                        embed_cfg=embed_cfg,
                        commit=False,
                    )
                    self._add_entry_pg(_pg, topic_id, source, question, answer, now,
                                       embedding=embedding, embed_cfg=embed_cfg,
                                       commit=False,
                                       source_qa_id=source_qa_id)
                    logger.info("strict_pg auto-heal OK for topic %r", topic_id)
                # Success (incl. auto-heal): commit PG then SQLite.
                _pg.commit()
                self.conn.commit()
            except ValueError:
                # embedding/fingerprint contract error: fail-closed, rollback both.
                try:
                    _pg.rollback()
                except Exception:
                    pass
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise
            except Exception as e:
                try:
                    _pg.rollback()
                except Exception:
                    pass
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise RuntimeError(
                    f"strict_pg add_entry failed: {type(e).__name__}: "
                    f"{_safe_err(e)[:200]}"
                ) from e
        # --- PG branch end ---

        return eid

    def _add_entry_pg(self, pg, topic_id, source, question, answer, now,
                          embedding=None, embed_cfg=None, *, commit: bool = True,
                          source_qa_id=None):
            """写入 PG topic_entries 表。

            PG 表没有 TEXT id 字段（只有 BIGSERIAL），用 seq 自增去重。
            重复 (topic_id, seq) 走 ON CONFLICT DO NOTHING。

            embedding: 可选 list[float]. 传了 embedding 必须同时传 embed_cfg
            (来自 ``build_embed_cfg`` 工厂), fingerprint 写到 ``embed_model`` 列.
            未传 embed_cfg 但传了 embedding → fail-closed, 立即 ValueError.

            source_qa_id (2026-08-22 G1B): 可选 int — 单条 QA 直挂 entry 的
            证据链回链 (qa_pairs.id)。仅"直接来自单条 QA"的 entry 由调用方
            显式传入并复用该 QA 已有向量; 派生类 entry (obs:fact / 压缩段 /
            多 QA 合成) 保持默认 None, 不伪造单条来源。
            生产库缺 source_qa_id 列时: 先 rollback 再降级不写该列 (可观测
            warning), 绝不让事务死在 InFailedSqlTransaction (8/22 死 fallback 教训)。

            旧 schema 兼容: topic_entries 表缺 ``embed_model`` / ``source_qa_id``
            列时, 按 undefined-column 错误逐列降级 (rollback → 去掉缺失列重写)。
            仅 catch psycopg2 "undefined_column" / "column does not exist";
            其余异常原样冒泡。

            commit: 默认 True, 写完后自动 ``pg.commit()``。
                    False 时不调 commit — 用于 strict_pg 严格事务路径 (与 _upsert_topic_pg
                    在同一 PG 事务里, 由调用方最终统一 commit 一次)。
            """
            emb_for_pg = None
            fp = ""
            if embedding:
                if isinstance(embedding, str):
                    try:
                        emb_for_pg = json.loads(embedding)
                    except (json.JSONDecodeError, TypeError):
                        emb_for_pg = None
                elif isinstance(embedding, (list, tuple)):
                    emb_for_pg = list(embedding)
                if emb_for_pg:
                    fp, _ = _resolve_embed_cfg(embed_cfg)

            from pgvector.psycopg2 import register_vector
            # P1.2-B: 同上 — 解包 pool wrapper 为原生 connection。
            _pg_raw = pg._connect() if hasattr(pg, "_connect") else pg
            register_vector(_pg_raw)

            # 2026-08-22 G1B: 动态列构造 — source_qa_id 仅在显式传入时写入;
            # 缺列按错误信息逐列剥离降级, 替代嵌套 fallback 金字塔。
            _cols = ["topic_id", "question", "answer", "source", "seq", "timestamp"]
            _vals: list = [topic_id, question, answer, source, topic_id, now]
            _ph = ["%s", "%s", "%s", "%s",
                   "(SELECT COALESCE(MAX(seq), 0) + 1 FROM topic_entries WHERE topic_id = %s)",
                   "%s"]
            if emb_for_pg:
                _cols += ["embedding", "embed_model"]
                _ph += ["%s::vector", "%s"]
                _vals += [emb_for_pg, fp]
            if source_qa_id:
                _cols.append("source_qa_id")
                _ph.append("%s")
                _vals.append(source_qa_id)

            def _entry_insert_sql(cols: list, ph: list) -> str:
                return (
                    "INSERT INTO topic_entries (\n                                "
                    + ", ".join(cols)
                    + "\n                            )\n                            VALUES (\n                                "
                    + ", ".join(ph)
                    + "\n                            )\n                            ON CONFLICT DO NOTHING"
                )

            # 最多剥 2 次 (embed_model / source_qa_id 都可能缺); 第 3 次仍失败即真异常。
            for _attempt in range(3):
                try:
                    with pg.cursor() as cur:
                        cur.execute(_entry_insert_sql(_cols, _ph), tuple(_vals))
                    break
                except Exception as _e_ins:
                    if not _is_undefined_column_error(_e_ins):
                        raise
                    # 2026-08-22: 先 rollback 再降级 — 防 InFailedSqlTransaction 死 fallback.
                    try:
                        pg.rollback()
                    except Exception:
                        pass
                    # 2026-08-22 e2e 修正: 真 PG 报错内嵌完整 SQL(含全部列名),
                    # 子串匹配会误中其它列 — 只认 column "X" 引用形态。
                    import re as _re
                    _quoted = set(_re.findall(
                        r'column "([a-z_0-9]+)"', str(_e_ins).lower()))
                    _missing = next(
                        (c for c in ("embed_model", "source_qa_id")
                         if c in _cols and c in _quoted),
                        None,
                    )
                    if _missing is None:
                        raise
                    logger.warning(
                        "topic_store._add_entry_pg: topic_entries 缺 %s 列, "
                        "降级不写该列 (已先 rollback): %s",
                        _missing, _safe_err(_e_ins)[:120],
                    )
                    _idx = _cols.index(_missing)
                    # 2026-08-22 e2e 修正: vals 按"消耗 %s 的顺序"对齐 (seq 子查询自带
                    # %s、timestamp 字面值不占位), 该列绑定值的下标 = 删除前
                    # ph[0..i-1] 里 %s 的个数, 不能直接用列下标。
                    _vi = sum(p.count("%s") for p in _ph[:_idx])
                    del _cols[_idx]
                    del _ph[_idx]
                    del _vals[_vi]
            if commit:
                pg.commit()

    # ─── topic_edges ───

    def add_edge(self, source_id: str, target_id: str, relation: str = 'related',
                 weight: float = 0.5, description: str = ''):
        self.conn.execute(
            "INSERT INTO topic_edges (source_id, target_id, relation, weight, description, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (source_id, target_id, relation, weight, description, self._now())
        )
        self.conn.commit()

    def get_edges(self, topic_id: str):
        rows = self.conn.execute(
            "SELECT * FROM topic_edges WHERE source_id=? OR target_id=? ORDER BY weight DESC",
            (topic_id, topic_id)
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("PRAGMA table_info(topic_edges)").fetchall()]
        return [dict(zip(cols, r)) for r in rows]

    # ─── topic_buffer ───

    def buffer_append(self, turn_id: str, summary: str, primary_topic_id: str = None,
                      primary_action: str = None, new_topic_draft: str = None,
                      related_links: list = None, qa_pairs: list = None,
                      raw_output: str = ''):
        self.conn.execute(
            "INSERT INTO topic_buffer (turn_id, timestamp, summary, primary_topic_id, "
            "primary_action, new_topic_draft, related_links, qa_pairs, raw_output) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (turn_id, self._now(), summary, primary_topic_id, primary_action,
             new_topic_draft, json.dumps(related_links or []),
             json.dumps(qa_pairs or []), raw_output)
        )
        self.conn.commit()

    def buffer_flush(self, min_count: int = 5, min_tokens: int = 5000,
                     max_hours: int = 24) -> list:
        """获取待 flush 的 buffer 条目，按 primary_topic_id 分组"""
        rows = self.conn.execute(
            "SELECT * FROM topic_buffer ORDER BY timestamp ASC"
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("PRAGMA table_info(topic_buffer)").fetchall()]
        entries = [dict(zip(cols, r)) for r in rows]
        if not entries:
            return []

        # 按 topic 分组
        groups = {}
        for e in entries:
            tid = e.get('primary_topic_id') or '_new'
            if tid not in groups:
                groups[tid] = []
            groups[tid].append(e)
        return groups

    def buffer_clear(self, ids: list = None):
        if ids:
            self.conn.execute(f"DELETE FROM topic_buffer WHERE id IN ({','.join('?'*len(ids))})", ids)
        else:
            self.conn.execute("DELETE FROM topic_buffer")
        self.conn.commit()

    # ─── 状态统计 ───

    def stats(self) -> dict:
        blocks = self.conn.execute("SELECT COUNT(*) FROM topic_blocks").fetchone()[0]
        entries = self.conn.execute("SELECT COUNT(*) FROM topic_entries").fetchone()[0]
        edges = self.conn.execute("SELECT COUNT(*) FROM topic_edges").fetchone()[0]
        buffer = self.conn.execute("SELECT COUNT(*) FROM topic_buffer").fetchone()[0]
        return {'topics': blocks, 'entries': entries, 'edges': edges, 'buffer': buffer}

    def close(self):
        self.conn.close()
