"""topic_recall.py — 层次化主题召回

替换当前 lessons/decisions 卡级别搜索
查询流程：
  query → embedding → 匹配主题(cosine ≥0.6)
  ├─ 命中 → 沿 edges 找子簇 → 返回主题块+子簇列表
  └─ 未命中 → fallback 到现有卡级别搜索

冷启动优化（v3-fix-cold-start 2026-07-31）:
  - topic_matrix.npz + topic_meta.json 落盘缓存 → 第二次加载 < 200ms
  - _ensure_loaded 加 threading.Lock → 三个并发调用方只触发一次全量加载
  - 校验逻辑: topic_meta.json 中 source_count 与 PG COUNT(*) 一致才用缓存

用法:
  from .topic_recall import hierarchical_recall
  result = hierarchical_recall(query, top_k=3)
"""
import contextlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .embedding import call_embedding
from .topic_store import TopicStore
from .config import resolve_config, _resolve_data_dir
from .pg_pool import DEFAULT_LEASE_TIMEOUT
from ._deadline import PrefetchDeadlineExceeded, coerce_deadline


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

logger = logging.getLogger("v3core.topic_recall")

# ── 默认参数（实验验证） ──
MATCH_THRESHOLD = 0.5
MAX_TOPICS = 3
MAX_SUBTOPICS = 3
MAX_CONTEXT_CHARS = 3000

# 模块级加载锁 — 跨实例互斥。
# 并发来源是不同 TopicRecall 实例 (prefetch 主线程 / SWR 刷新线程 / recall_pool 各自构造),
# 实例级锁管不住跨实例并发; 共享锁让同一时刻只有一个实例做 PG 全量加载,
# 后到的实例等锁后走 npz 缓存快路径 (毫秒级)。
_LOAD_LOCK = threading.Lock()

DB_PATH = lambda: str(_resolve_data_dir() / 'v3_topic_full.db')  # 生产 db; 历史 8286.db 实验名已废弃

# 落盘缓存文件名（按 profile 区分） — npz 存矩阵, json 存元信息
TOPIC_MATRIX_NAME = "topic_matrix.npz"
TOPIC_META_NAME = "topic_meta.json"


def _get_pg_conn():
    """从配置获取 PG 连接，失败返回 None"""
    try:
        cfg = resolve_config()
        if cfg and cfg.pg:
            import psycopg2
            return psycopg2.connect(
                host=cfg.pg.host,
                port=cfg.pg.port,
                dbname=cfg.pg.database,
                user=cfg.pg.user,
                password=cfg.pg.password,
            )
    except Exception as e:
        logger.debug("PG connect failed: %s", _safe_err(e)[:100])
    return None


class TopicRecall:
    """层次化主题召回 — 支持 PG topics 表（优先） + SQLite 兜底"""

    def __init__(self, embed_cfg: dict = None, pool: Any = None):
        # None is an explicit disabled state; never turn it into a hand-built {}.
        self._embed_cfg = embed_cfg
        self._pool = pool
        self._store: Optional[TopicStore] = None
        self._pg_conn = None
        self._topics = []   # 缓存: [{id, title, summary, emb, body}]
        self._edges = []    # 缓存: [(source_id, target_id, weight)]
        # 优化: 矩阵化匹配 — match() 一次性算所有 cosine，避免 535 次 Python 循环 + 重算 norm
        self._emb_matrix: Optional[np.ndarray] = None   # shape (N, 1024)
        self._emb_norms: Optional[np.ndarray] = None    # shape (N,)
        # 冷启动并发保护: 三个调用方(prefetch 主线程 / SWR 刷新线程 / recall_pool)
        # 全部可能同时进入 _ensure_loaded; 锁保证只触发一次全量加载
        self._load_lock = threading.Lock()
        # 缓存落盘目录（走 config.base_path） — 用于 npz/json 快速恢复
        self._cache_dir = _resolve_data_dir()

    @property
    def pool(self) -> Any:
        """返回注入的连接池（如有）。"""
        return getattr(self, "_pool", None)

    @contextlib.contextmanager
    def lease(self, timeout: float | None = DEFAULT_LEASE_TIMEOUT):
        """获取一个 PostgreSQL 连接 lease 的上下文管理器。

        - 若注入了连接池 (_pool): 向池申请 lease, yield lease.connection, 并在 finally 中调用 lease.close()。
        - 若未注入连接池: 调用模块级 _get_pg_conn() 获取临时直连, yield 并在 finally 中关闭非 None 连接。
        """
        pool = getattr(self, "_pool", None)
        if pool is not None:
            lease = pool.lease(timeout=timeout)
            try:
                yield lease.connection
            finally:
                lease.close()
        else:
            conn = _get_pg_conn()
            try:
                yield conn
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def _ensure_loaded(self):
        # 锁外快路径 — 已加载直接返回 (冷启动之后的常态)
        if self._topics:
            return

        # 锁内慢路径 — 跨实例互斥, 三个并发调用方只有一个会真正加载
        with _LOAD_LOCK:
            # 持锁线程可能已完成加载 — 二次检查
            if self._topics:
                return

            # 优先尝试 npz 缓存快速恢复 (ms 级)
            if self._try_load_npz_cache():
                return

            # 缓存失效 / 不存在 — 走原 PG / SQLite 全量加载路径
            self._load_from_source()

            # 全量加载成功后落盘 npz 缓存 (供下次冷启动快速恢复)
            if self._topics:
                self._save_npz_cache()

    def _try_load_npz_cache(self) -> bool:
        """尝试从 npz 缓存恢复 — 失败返回 False 让上层走全量加载

        校验逻辑:
          1. topic_matrix.npz + topic_meta.json 两个文件都存在
          2. 两者 mtime 一致 (避免半写状态)
          3. topic_meta.json 中的 source_count 与 PG COUNT(*) 一致
          4. npz 中矩阵行数与 meta 中 len(ids) 一致

        返回 True 表示已成功从缓存恢复, self._topics / _emb_matrix 已填充.
        """
        npz_path = self._cache_dir / TOPIC_MATRIX_NAME
        meta_path = self._cache_dir / TOPIC_META_NAME
        if not (npz_path.exists() and meta_path.exists()):
            return False
        try:
            # mtime 一致性检查 — 避免半写文件
            if abs(npz_path.stat().st_mtime - meta_path.stat().st_mtime) > 1.0:
                logger.debug("topic_recall: cache mtime mismatch, fallback")
                return False

            # 读 meta json
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            cached_count = int(meta.get("source_count", -1))
            ids = meta.get("ids", [])
            titles = meta.get("titles", [])
            summaries = meta.get("summaries", [])
            bodies = meta.get("bodies", [])
            if cached_count < 0 or len(ids) != cached_count:
                return False

            # 与 PG COUNT(*) 校验 — 数量变化则缓存失效
            pg_count = self._pg_count_active()
            if pg_count is not None and pg_count != cached_count:
                logger.info(
                    "topic_recall: cache stale (pg=%d, cached=%d), fallback",
                    pg_count, cached_count,
                )
                return False

            # 读 npz 矩阵
            with np.load(npz_path) as data:
                emb_matrix = data["emb_matrix"]
                emb_norms = data["emb_norms"]
            if emb_matrix.shape[0] != cached_count:
                return False

            # 恢复 self._topics (顺序与矩阵行严格对应)
            self._topics = []
            for i, tid in enumerate(ids):
                emb_row = emb_matrix[i]
                self._topics.append({
                    "id": tid,
                    "title": titles[i] if i < len(titles) else "",
                    "summary": summaries[i] if i < len(summaries) else "",
                    "emb": emb_row,
                    "body": bodies[i] if i < len(bodies) else "",
                })
            self._emb_matrix = emb_matrix
            self._emb_norms = emb_norms
            logger.info(
                "topic_recall: loaded %d topics from npz cache (fast path)",
                len(self._topics),
            )
            return True
        except PrefetchDeadlineExceeded:
            raise
        except Exception as e:
            logger.warning("topic_recall: npz cache load failed, fallback: %s", _safe_err(e)[:100])
            return False

    def _save_npz_cache(self) -> None:
        """落盘 npz 缓存 (供下次冷启动快速恢复)

        文件结构:
          topic_matrix.npz: emb_matrix (N, D) float32 + emb_norms (N,) float32
          topic_meta.json:  source_count / ids / titles / summaries / bodies

        写文件策略: 先写 .tmp 再 os.replace 原子替换, 避免半写文件被误读.
        """
        if not self._topics:
            return
        if self._emb_matrix is None or self._emb_norms is None:
            self._build_matrix()
            if self._emb_matrix is None:
                return
        try:
            import time as _time
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            npz_path = self._cache_dir / TOPIC_MATRIX_NAME
            meta_path = self._cache_dir / TOPIC_META_NAME

            # 收集元信息 — id 可能是 int 或 str (如 "cl_00000000_744df649"),
            # JSON 只能存一种类型, 这里统一转 str 存, 读回时再恢复
            ids = [str(t["id"]) for t in self._topics]
            titles = [t.get("title", "") for t in self._topics]
            summaries = [t.get("summary", "") for t in self._topics]
            bodies = [t.get("body", "") for t in self._topics]
            meta = {
                "source_count": len(self._topics),
                "ids": ids,
                "titles": titles,
                "summaries": summaries,
                "bodies": bodies,
                "dim": int(self._emb_matrix.shape[1]) if self._emb_matrix.ndim == 2 else 0,
                "saved_at": _time.time(),
            }

            # 先写 .tmp (npz 后缀), 然后原子 rename
            # 注意: np.savez 要求文件名以 .npz 结尾 (否则静默失败不写文件),
            #       所以临时名是 *.tmp.npz (保留 .npz 后缀), 不是 *.npz.tmp
            tmp_npz = str(npz_path)[:-4] + ".tmp.npz"
            tmp_meta = str(meta_path) + ".tmp"
            np.savez(
                tmp_npz,
                emb_matrix=self._emb_matrix,
                emb_norms=self._emb_norms,
            )
            with open(tmp_meta, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            os.replace(tmp_npz, str(npz_path))
            os.replace(tmp_meta, str(meta_path))
            logger.info(
                "topic_recall: saved npz cache (%d topics, %s)",
                len(self._topics), npz_path,
            )
        except Exception as e:
            logger.warning("topic_recall: npz cache save failed: %s", _safe_err(e)[:100])

    def _pg_count_active(self):
        """查询 PG topics 表的有效 (status=active, embedding IS NOT NULL) 行数.

        失败返回 None — 调用方应将 None 视为"无法校验"并继续使用缓存.
        """
        try:
            with self.lease() as pg:
                if not pg:
                    return None
                with pg.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM topics "
                        "WHERE status='active' AND embedding IS NOT NULL"
                    )
                    row = cur.fetchone()
                    if row is not None:
                        return int(row[0])
                    return None
        except PrefetchDeadlineExceeded:
            raise
        except Exception:
            return None

    def _load_from_source(self):
        """全量加载 topics — 包含原 _ensure_loaded 的 PG + SQLite 主体逻辑.

        加载流程 (按优先级):
          1. PG topics 表 (主路径)
          2. SQLite topic_blocks / topic_edges (兜底)
          3. 矩阵索引构建 (_build_matrix)

        失败仅记录日志, 不抛异常 — _ensure_loaded 仍会继续到 save cache 步骤.
        """

        # 优先从 PG topics 表加载
        try:
            with self.lease() as pg:
                if pg:
                    with pg.cursor() as cur:
                        cur.execute(
                            "SELECT topic_id, title, COALESCE(summary, ''), "
                            "COALESCE(body, ''), embedding "
                            "FROM topics WHERE status='active' AND embedding IS NOT NULL"
                        )
                        rows = cur.fetchall()
                        if rows:
                            for tid, title, summary, body, emb_vec in rows:
                                if emb_vec is not None:
                                    # pgvector returns list-like; try numeric array
                                    try:
                                        emb_arr = np.array(emb_vec, dtype=np.float32)
                                    except (ValueError, TypeError):
                                        # if stored as JSON string, parse
                                        import json as _j
                                        if isinstance(emb_vec, str) and emb_vec.startswith('['):
                                            emb_arr = np.array(_j.loads(emb_vec), dtype=np.float32)
                                        else:
                                            continue
                                    self._topics.append({
                                        'id': tid,
                                        'title': title or '',
                                        'summary': summary or '',
                                        'emb': emb_arr,
                                        'body': (body or '')[:500],
                                    })
                            if self._topics:
                                logger.info("topic_recall: loaded %d topics from PG", len(self._topics))
                                # 优化: 构建矩阵索引（match() 矩阵化用）
                                self._build_matrix()
                                return
        except PrefetchDeadlineExceeded:
            raise
        except Exception as e:
            logger.warning("topic_recall PG load failed, fallback: %s", _safe_err(e)[:100])

        # 兜底：从 SQLite 加载
        db = DB_PATH()
        if os.path.exists(db):
            self._store = TopicStore(db)
        if not self._store:
            logger.warning("topic_recall: no data source (PG failed, SQLite not found)")
            return

        # 加载主题
        for t in self._store.get_all_topics():
            row = self._store.conn.execute(
                'SELECT embedding, body FROM topic_blocks WHERE id=?', (t['id'],)
            ).fetchone()
            if row and row[0]:
                emb = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                self._topics.append({
                    'id': t['id'],
                    'title': t['title'],
                    'summary': t.get('summary', ''),
                    'emb': np.array(emb),
                    'body': (row[1] or '')[:500],
                })

        # 加载边
        edges = self._store.conn.execute(
            'SELECT source_id, target_id, weight FROM topic_edges'
        ).fetchall()
        self._edges = [(e[0], e[1], e[2] or 0.5) for e in edges]

        # 优化: 构建矩阵索引（match() 矩阵化用）
        self._build_matrix()

        logger.info("topic_recall loaded: %d topics, %d edges",
                    len(self._topics), len(self._edges))

    def _build_matrix(self):

        """优化: 构建矩阵索引

        将所有 topic 的 embedding 堆聚为 (N, 1024) numpy 矩阵，并预计算 L2 范数 (N,) 。
        match() 只要一次矩阵乘法就可以计算所有 cosine，避免 535 次 Python 循环＋重复计算 norm，快十倍。
        """
        if not self._topics:
            self._emb_matrix = None
            self._emb_norms = None
            return
        try:
            self._emb_matrix = np.stack([t['emb'] for t in self._topics]).astype(np.float32)
            self._emb_norms = np.linalg.norm(self._emb_matrix, axis=1)
        except Exception as e:
            logger.warning("topic_recall: _build_matrix failed: %s", _safe_err(e)[:100])
            self._emb_matrix = None
            self._emb_norms = None

    def match(self, query: str, threshold: float = MATCH_THRESHOLD,
              top_k: int = MAX_TOPICS,
              query_embedding: Optional[list] = None,
              *,
              deadline=None) -> list:
        """匹配查询 → 返回 (sim, topic) 列表

        优化: 矩阵化匹配 — 一次矩阵乘法算所有 cosine，快十倍于 535 次 Python 循环＋np.linalg.norm。

        query_embedding: 上游已经算好的 query 向量 (如 V3Core.prefetch_to_context_block
        通过 call_embedding(query, ..., cache=True) 算出的结果)。传入时直接复用，
        避免长 query (>1000) 因 cache key 与上游不同而再次调 call_embedding
        (实测多耗 0.5-1.9s 的 provider 延迟)。

        deadline (2026-09-04 P1.3.1 deadline-closure): optional
        ``PrefetchDeadline`` (or absolute monotonic float) that bounds the
        foreground ``call_embedding`` fallback path only.  Behaviour:

          * ``deadline=None`` (legacy path): call_embedding(query[:1000],
            ..., timeout=2.5, retries=1) — preserved byte-for-byte.
          * ``deadline`` set: check the bound *before* the call, clamp the
            per-call timeout to ``max(0.001, min(2.5, remaining))``, force
            ``retries=0`` (a hard prefetch budget must not retry inside an
            exhausted window), and re-check after the call.  An already-
            expired deadline raises ``PrefetchDeadlineExceeded`` *before*
            the network call so the foreground prefetch cannot block on a
            doomed provider round-trip.

        PrefetchDeadlineExceeded is propagated; never swallowed.
        """
        if query_embedding is None and self._embed_cfg is None:
            return []
        self._ensure_loaded()
        if not self._topics:
            return []

        if query_embedding is not None:
            # 上游已算好 — 跳过 call_embedding, 直接用
            emb = list(query_embedding)
        else:
            bound = coerce_deadline(deadline)
            if bound is not None:
                # Fail fast: an exhausted budget must not trigger a doomed call.
                bound.check(context="topic_recall.match embedding")
            try:
                if bound is None:
                    # 2026-08-02: 实测单次 embedding 0.5-1.9s (avg 0.76s) — timeout 2.5s + 1 次重试
                    # 最坏 5s (重试成功率高, 救回瞬时抖动); 再失败返回空 (不落旧路)
                    emb = call_embedding(query[:1000], self._embed_cfg,
                                          timeout=2.5, retries=1)
                else:
                    # Deadline-bound: clamp the per-call timeout to the
                    # remaining budget (capped at the legacy 2.5s ceiling,
                    # floored at 1ms) and force retries=0 so a hard
                    # prefetch budget is honoured exactly.
                    timeout = max(0.001, min(2.5, bound.remaining()))
                    emb = call_embedding(query[:1000], self._embed_cfg,
                                          timeout=timeout, retries=0)
                    # Post-call re-check: a successful network round-trip
                    # inside an already-tight budget must still surface the
                    # exhaustion signal to the caller.
                    bound.check(context="topic_recall.match embedding post-call")
            except PrefetchDeadlineExceeded:
                raise
            except ValueError:
                raise
            except Exception as _e:
                # 2026-08-01: network/provider failure → return empty without old file scan.
                logger.warning("topic_recall: query embedding 失败 (%s), 返回空", str(_e)[:60])
                return []
        emb_arr = np.asarray(emb, dtype=np.float32)
        emb_norm = float(np.linalg.norm(emb_arr))

        # 矩阵化计算 cosine
        if self._emb_matrix is not None and self._emb_norms is not None and len(self._emb_matrix) == len(self._topics):
            sims = self._emb_matrix @ emb_arr / (emb_norm * self._emb_norms + 1e-10)
            # numpy 返回 float32, argsort 取 top_k 上部分
            order = np.argsort(-sims)[:top_k]
            results = []
            for i in order:
                sim = float(sims[i])
                if sim >= threshold:
                    results.append((sim, self._topics[int(i)]))
                else:
                    # 已排序，后面必 < threshold，提前 break
                    break
            return results

        # 兑浑路径\uff08矩阵未构建 / dim 不匹配\uff09\uff1a原 Python 循环
        results = []
        for t in self._topics:
            sim = float(np.dot(emb_arr, t['emb']) / (
                emb_norm * np.linalg.norm(t['emb']) + 1e-10))
            if sim >= threshold:
                results.append((sim, t))
        results.sort(key=lambda x: x[0], reverse=True)
        return results[:top_k]

    def get_related_topics(self, topic_id: int, max_count: int = MAX_SUBTOPICS) -> list:
        """沿边找关联主题"""
        related = []
        for src, tgt, w in self._edges:
            if src == topic_id:
                t = self._find_topic(tgt)
                if t:
                    related.append((w, t))
            elif tgt == topic_id:
                t = self._find_topic(src)
                if t:
                    related.append((w, t))
        related.sort(key=lambda x: x[0], reverse=True)
        return related[:max_count]

    def _find_topic(self, tid: int) -> dict:
        for t in self._topics:
            if t['id'] == tid:
                return t
        return None

    def format_context(self, matches: list) -> str:
        """格式化召回的 topic → 可注入 context 的文本块"""
        if not matches:
            return ''

        parts = []
        total_chars = 0

        for sim, topic in matches:
            if total_chars > MAX_CONTEXT_CHARS:
                break
            block = self._format_single_topic(topic, sim)
            parts.append(block)
            total_chars += len(block)

        if parts:
            # 用 == 包裹成上下文块
            return '\n\n'.join(parts)
        return ''

    def _format_single_topic(self, topic: dict, sim: float) -> str:
        title = topic['title']
        summary = topic['summary']
        body = topic['body']

        # 找关联子簇
        related = self.get_related_topics(topic['id'])
        related_text = ''
        if related:
            related_text = '\n  关联主题: ' + ', '.join(
                [r[1]['title'] for r in related[:3]]
            )

        lines = [
            f"━━━ {title} ━━━",
            f"摘要: {summary}",
            f"置信度: {sim:.2f}",
        ]
        if body:
            lines.append(f"内容: {body}")
        if related_text:
            lines.append(related_text)

        return '\n'.join(lines)

    def hierarchical_recall(self, query: str) -> str:
        """层次化召回主入口 — 纯 topic 卡匹配

        1. 查询主题匹配
        2. 沿边找关联子簇
        3. 格式化为 context 块
        """
        matches = self.match(query)
        if not matches:
            return ''

        return self.format_context(matches)

    def chain_recall(self, query: str) -> dict:
        """链式召回 — topic 不足时走印 → topic 条目

        1. topic 卡匹配（同 match()）
        2. 如果匹配不足（最高 cos < 0.65）：读最新印做上下文（PG yin 优先，文件兜底）
        3. 对命中的 topic 取 entries
        """
        base = _resolve_data_dir()
        result = {
            'cards': [],
            'yin_context': '',
            'entries': [],
            'raw_messages': [],
            'source': 'topic',
        }

        # Step 1: topic 卡匹配
        matches = self.match(query)
        if matches:
            result['cards'] = [(float(sim), {
                'id': t['id'], 'title': t['title'],
                'summary': t['summary'], 'body': t['body'],
            }) for sim, t in matches]

        # Step 2: 如果匹配弱，读最新印做上下文（PG 优先）
        best_sim = matches[0][0] if matches else 0.0
        if not matches or best_sim < 0.65:
            yin_text = ''
            try:
                with self.lease() as pg:
                    if pg:
                        with pg.cursor() as cur:
                            cur.execute(
                                "SELECT content FROM yin WHERE is_current=true ORDER BY id DESC LIMIT 1"
                            )
                            r = cur.fetchone()
                            if r:
                                yin_text = r[0]
            except PrefetchDeadlineExceeded:
                raise
            except Exception:
                pass

            if not yin_text:
                # 文件兜底
                y_dir = base / 'y'
                y_files = sorted(y_dir.glob('y_*.md'))
                if y_files:
                    try:
                        yin_text = y_files[-1].read_text(encoding='utf-8')
                    except Exception:
                        pass

            if yin_text:
                import re
                yin_text = re.sub(r'<think>.*?</think>', '', yin_text, flags=re.DOTALL).strip()
                result['yin_context'] = yin_text[:1500]
                result['source'] = 'yin_topic' if yin_text else 'topic'

        # Step 3: 对命中的 topic 取 entries（PG 优先，SQLite 兜底）
        # 2026-08-22 G1C: 相关性优先 — 有 query 向量时按 entry embedding cosine 排序;
        # 带 source_qa_id 的条目回读 qa_pairs 原文 (full=True), 按 qa id 去重;
        # source_qa_id 列不存在/值为空 → 回退旧语义 (seq LIMIT 5 截断文本)。
        if matches:
            # 复用 match 已算好的 query 向量, 避免二次 embedding 调用
            q_emb = None
            try:
                if self._embed_cfg is not None:
                    q_emb = call_embedding(query[:1000], self._embed_cfg,
                                           timeout=2.5, retries=1)
            except Exception as _e:
                logger.debug("chain_recall: query embedding 失败, 降级 seq 排序: %s",
                             str(_e)[:60])
                q_emb = None
            topic_ids = [t['id'] for _, t in matches[:2]]
            for tid in topic_ids:
                entries_data = []
                # PG 相关性路径: source_qa_id JOIN qa_pairs 原文 + cosine 排序
                try:
                    with self.lease() as _pg:
                        if _pg:
                            if q_emb is not None:
                                emb_str = "[" + ",".join(str(float(x)) for x in q_emb) + "]"
                                try:
                                    with _pg.cursor() as _cur:
                                        _cur.execute(
                                            "SELECT te.seq, te.question, te.answer, "
                                            "te.source_qa_id, qp.question, qp.answer, "
                                            "1 - (te.embedding <=> %s::vector) AS cosine "
                                            "FROM topic_entries te "
                                            "LEFT JOIN qa_pairs qp ON qp.id = te.source_qa_id "
                                            "WHERE te.topic_id=%s AND te.embedding IS NOT NULL "
                                            "ORDER BY cosine DESC LIMIT 5",
                                            (emb_str, tid)
                                        )
                                        for r in _cur.fetchall():
                                            seq_v, q_trunc, a_trunc, sqaid, q_full, a_full, _cos = r
                                            if sqaid and q_full is not None:
                                                entries_data.append({
                                                    'seq': seq_v,
                                                    'q': q_full or '',
                                                    'a': a_full or '',
                                                    'source_qa_id': int(sqaid),
                                                    'full': True,
                                                })
                                            else:
                                                entries_data.append({
                                                    'seq': seq_v,
                                                    'q': (q_trunc or '')[:200],
                                                    'a': (a_trunc or '')[:200],
                                                })
                                except PrefetchDeadlineExceeded:
                                    raise
                                except Exception as e_col:
                                    if 'source_qa_id' not in str(e_col).lower() \
                                       and 'does not exist' not in str(e_col).lower():
                                        raise
                                    _pg.rollback()
                                    entries_data = []  # 列缺失 → 走旧 SQL
                            if not entries_data:
                                # 旧语义兜底 (无向量 / 无 source_qa_id 列): seq LIMIT 5
                                with _pg.cursor() as _cur:
                                    _cur.execute(
                                        "SELECT seq, question, answer, timestamp "
                                        "FROM topic_entries WHERE topic_id=%s ORDER BY seq LIMIT 5",
                                        (tid,)
                                    )
                                    for r in _cur.fetchall():
                                        entries_data.append({
                                            'seq': r[0], 'q': (r[1] or '')[:200],
                                            'a': (r[2] or '')[:200]
                                        })
                except PrefetchDeadlineExceeded:
                    raise
                except Exception as e:
                    logger.debug("chain_recall PG entries 失败: %s", _safe_err(e)[:100])
                # SQLite 兜底（仅当 PG 没数据且 self._store 可用）
                if not entries_data and self._store:
                    try:
                        rows = self._store.conn.execute(
                            'SELECT seq, question, answer, timestamp '
                            'FROM topic_entries WHERE topic_id=? ORDER BY seq LIMIT 5',
                            (tid,)
                        ).fetchall()
                        entries_data = [{'seq': r[0], 'q': (r[1] or '')[:200], 'a': (r[2] or '')[:200]} for r in rows]
                    except Exception as e:
                        logger.debug("chain_recall SQLite entries 失败: %s", _safe_err(e)[:100])
                # 2026-08-22 G1C: 按 source_qa_id 去重 (同一 QA 只出现一次);
                # 无 source_qa_id 的条目按 (seq,q) 键去重。
                _seen = set()
                _deduped = []
                for e in entries_data:
                    k = e.get('source_qa_id') or (e.get('seq'), e.get('q', ''))
                    if k in _seen:
                        continue
                    _seen.add(k)
                    _deduped.append(e)
                entries_data = _deduped
                if entries_data:
                    result['entries'].append({
                        'topic_id': tid,
                        'entries': entries_data,
                    })

        return result


def hierarchical_recall(query: str, embed_cfg: dict = None) -> str:
    """便捷函数 — 直接调层次化主题召回"""
    recall = TopicRecall(embed_cfg)
    return recall.hierarchical_recall(query)
