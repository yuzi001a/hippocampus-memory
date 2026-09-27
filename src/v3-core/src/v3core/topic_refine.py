"""topic_refine.py — 增量提炼核心

实时 + 定时两条路径：
- 实时：每轮 sync_turn 后调用 refine_turn()
- 定时：cron 调用 scan_new_sessions()

核心逻辑：累积文本匹配 + 稳定确认 + 三级兜底（实时/定时/cron）
"""
import json
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .embedding import call_embedding
from .event_adapter import HermesAdapter, SessionEvents
from .topic_store import TopicStore
from .config import resolve_config, _resolve_data_dir


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

logger = logging.getLogger("v3core.topic_refine")

# ── 默认参数（实验验证） ──
STABLE_CONFIRM = 2        # 连续几次同一主题才算稳定
MATCH_HIGH = 0.75         # 高置信度阈值（调高防误吞）
MATCH_MED = 0.55          # 中等置信度阈值（低门槛让更多内容进入候选）
MATCH_LOW = 0.4           # 弱匹配阈值
MIN_TURNS = 3             # 至少几轮才考虑处理
COOLDOWN_SEC = 300        # session 安静多久才算处理完毕（5分钟）


class SessionTracker:
    """session 处理状态追踪 — 存在 topic_store 同库"""

    def __init__(self, store: TopicStore):
        self.store = store
        self._ensure_table()

    def _ensure_table(self):
        self.store.conn.execute("""
            CREATE TABLE IF NOT EXISTS session_tracker (
                session_id TEXT PRIMARY KEY,
                status TEXT DEFAULT 'new',
                turn_count INTEGER DEFAULT 0,
                last_active_ts REAL DEFAULT 0,
                processed_ts REAL DEFAULT 0,
                topic_id INTEGER,
                last_processed_turn INTEGER DEFAULT 0
            )
        """)
        self.store.conn.commit()

    def get(self, session_id: str) -> dict:
        row = self.store.conn.execute(
            "SELECT session_id, status, turn_count, last_active_ts, processed_ts, topic_id, last_processed_turn FROM session_tracker WHERE session_id=?", (session_id,)
        ).fetchone()
        if row:
            return {
                'session_id': row[0], 'status': row[1], 'turn_count': row[2],
                'last_active_ts': row[3], 'processed_ts': row[4],
                'topic_id': row[5], 'last_processed_turn': row[6] or 0
            }
        return {'session_id': session_id, 'status': 'new', 'turn_count': 0,
                'last_active_ts': 0, 'processed_ts': 0, 'topic_id': None,
                'last_processed_turn': 0}

    def upsert(self, session_id: str, turn_count: int, last_active_ts: float = 0,
               status: str = 'new', topic_id: int = None, last_processed_turn: int = 0):
        self.store.conn.execute("""
            INSERT INTO session_tracker (session_id, status, turn_count, last_active_ts,
                                         processed_ts, topic_id, last_processed_turn)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                status=excluded.status, turn_count=excluded.turn_count,
                last_active_ts=excluded.last_active_ts,
                topic_id=excluded.topic_id,
                last_processed_turn=excluded.last_processed_turn
        """, (session_id, status, turn_count, last_active_ts,
              time.time(), topic_id, last_processed_turn))
        self.store.conn.commit()

    def get_unprocessed(self, limit: int = 50) -> list:
        """取未处理 / 有新增轮次的 session"""
        return self.store.conn.execute("""
            SELECT * FROM session_tracker
            WHERE status IN ('new', 'changed')
            ORDER BY last_active_ts DESC
            LIMIT ?
        """, (limit,)).fetchall()


class TopicMatcher:
    """主题匹配器 — 缓存主题向量，支持增量匹配"""

    def __init__(self, store: TopicStore, embed_cfg: dict = None, pg_conn=None):
        self.store = store
        # None is explicit disabled; never convert it to a hand-built config.
        self._embed_cfg = embed_cfg
        self.pg_conn = pg_conn  # 可选：供 _update_topic / _update_topic_block PG 双写
        self._topics = []
        self._refresh()

    def _refresh(self):
        self._topics = []
        for t in self.store.get_all_topics():
            row = self.store.conn.execute(
                'SELECT embedding FROM topic_blocks WHERE id=?', (t['id'],)
            ).fetchone()
            if row and row[0]:
                raw = row[0]
                # SQLite BLOB → 字节串; JSON 字符串 → 解析; 已有 list → 直接用
                if isinstance(raw, (bytes, bytearray)):
                    # SQLite BLOB 实际是 JSON 文本存成 BLOB（首字节 '['）
                    # 先 decode 试 JSON，失败再试原始二进制
                    try:
                        emb = np.array(json.loads(raw.decode('utf-8')), dtype=np.float32)
                    except Exception:
                        try:
                            emb = np.frombuffer(raw, dtype=np.float32)
                        except Exception:
                            continue
                elif isinstance(raw, str):
                    try:
                        emb = np.array(json.loads(raw), dtype=np.float32)
                    except Exception:
                        continue
                elif isinstance(raw, (list, tuple)):
                    emb = np.array(raw, dtype=np.float32)
                else:
                    continue
                if emb.ndim != 1 or emb.shape[0] < 2:
                    continue
                self._topics.append({
                    'id': t['id'], 'title': t['title'],
                    'summary': t.get('summary', ''),
                    'emb': emb,
                })

    def match(self, text: str, threshold: float = MATCH_LOW) -> list:
        """返回 [(sim, topic_id, title), ...] 按 similarity 降序"""
        if self._embed_cfg is None:
            return []
        emb = call_embedding(text[:1000], self._embed_cfg)
        results = []
        for t in self._topics:
            sim = float(np.dot(emb, t['emb']) / (
                np.linalg.norm(emb) * np.linalg.norm(t['emb']) + 1e-10))
            if sim >= threshold:
                results.append((sim, t['id'], t['title']))
        results.sort(key=lambda x: x[0], reverse=True)
        return results

    def check_stable_match(self, matches_history: list, topic_id: int) -> bool:
        """检查连续 N 次匹配结果中同一主题占比是否达标"""
        if len(matches_history) < STABLE_CONFIRM:
            return False
        recent = matches_history[-STABLE_CONFIRM:]
        hits = sum(1 for m in recent if m and m[0] == topic_id)
        return hits >= STABLE_CONFIRM


def build_session_text(session: SessionEvents, max_turns: int = 100) -> str:
    """用累积文本构建匹配用的文本（user + assistant）"""
    parts = []
    for qa in session.qa_pairs[:max_turns]:
        if qa.role in ('user', 'assistant', 'tool'):
            parts.append(qa.content[:300])
    return ' '.join(parts)


def get_cumulative_text(session: SessionEvents, start: int = 0, end: int = None) -> str:
    """取 session 中 start~end 轮的累积文本"""
    end = end or len(session.qa_pairs)
    return build_session_text(
        SessionEvents(
            session_id=session.session_id, source=session.source,
            qa_pairs=session.qa_pairs[start:end],
            title=session.title, time_start=session.time_start,
            time_end=session.time_end, turn_count=end - start
        )
    )


def refine_turn(
    session: SessionEvents,
    tracker: SessionTracker,
    matcher: TopicMatcher,
    match_history: dict = None
) -> str:
    """每轮 sync_turn 后调用—处理增量匹配

    返回: 'skip' / 'match' / 'buffer'
    """
    if match_history is None:
        match_history = {}

    # 1. 基本检查
    sid = session.session_id
    state = tracker.get(sid)
    turn_count = len(session.qa_pairs)

    if turn_count < MIN_TURNS:
        return 'skip'

    # 2. 检查是否有新增轮次
    last_processed = state.get('last_processed_turn', 0)
    if turn_count <= last_processed:
        return 'skip'

    # 3. 检查 session 是否还在活跃
    now = time.time()
    last_active = session.qa_pairs[-1].timestamp or 0
    time_since_active = now - last_active
    if time_since_active < COOLDOWN_SEC and last_processed > 0:
        # 在冷却期内且之前已处理过 → 跳过
        return 'skip'

    # 4. 用累积文本匹配
    text = get_cumulative_text(session, start=last_processed, end=turn_count)
    if len(text.strip()) < 30:
        return 'skip'

    matches = matcher.match(text, threshold=MATCH_LOW)

    # 5. 稳定匹配检查
    if sid not in match_history:
        match_history[sid] = []
    top_id = matches[0][1] if matches else None
    match_history[sid].append((top_id, matches[0][0] if matches else 0))

    # 保持最近 N 条记录
    if len(match_history[sid]) > STABLE_CONFIRM * 2:
        match_history[sid] = match_history[sid][-STABLE_CONFIRM * 2:]

    # 6. 决策
    if matches and matches[0][0] >= MATCH_HIGH:
        # 高置信度 → 立即归类
        tid = matches[0][1]
        tname = matches[0][2]
        _update_topic(session, tid, tname, tracker, matcher)
        tracker.upsert(sid, turn_count, last_active, 'processed', tid, turn_count)
        logger.info("refine: %s → %s (high, %.4f)", sid[:12], tname, matches[0][0])
        return 'match'

    if matches and matches[0][0] >= MATCH_MED:
        # 中等置信度 → 检查稳定
        if matcher.check_stable_match(match_history[sid], top_id):
            tid = matches[0][1]
            tname = matches[0][2]
            # 稳定但不直接归入——写 buffer + candidate 标记，给机会自然聚簇
            _buffer_candidate(session, tid, tname, tracker)
            tracker.upsert(sid, turn_count, last_active, 'candidate', tid, turn_count)
            logger.info("refine: %s → buffer(candidate=%s, %.4f)", sid[:12], tname, matches[0][0])
            return 'buffer'
        # 不稳定 → 标记 changed 等下次
        tracker.upsert(sid, turn_count, last_active, 'changed', None, last_processed)
        return 'buffer'

    # 7. 低匹配或不匹配 → 写 orphan buffer 供后续聚簇
    qa_dicts = [{"role": qa.role, "content": qa.content[:200]} for qa in session.qa_pairs[:5]]
    _buffer_orphan(sid, 0, qa_dicts, text, tracker)
    tracker.upsert(sid, turn_count, last_active, 'new', None, last_processed)
    return 'buffer'


def _update_topic(session: SessionEvents, topic_id: int, topic_name: str,
                  tracker: SessionTracker, matcher: TopicMatcher):
    """更新 topic 内容  ->  追加 body + 写条目"""
    store = tracker.store

    # 取当前 body
    row = store.conn.execute(
        'SELECT body, summary FROM topic_blocks WHERE id=?', (topic_id,)
    ).fetchone()

    # 取本轮的关键内容
    text = get_cumulative_text(session, end=min(5, len(session.qa_pairs)))

    if row and row[0]:
        # 追加新的摘要段落
        new_body = row[0] + '\n\n---\n' + text[:500]
        store.conn.execute(
            'UPDATE topic_blocks SET body=?, updated_at=datetime("now") WHERE id=?',
            (new_body[:5000], topic_id)
        )
    else:
        store.conn.execute(
            'UPDATE topic_blocks SET body=?, updated_at=datetime("now") WHERE id=?',
            (text[:2000], topic_id)
        )

    # 写条目
    store.add_entry(topic_id=topic_id, source=session.session_id,
                    question=session.qa_pairs[0].content[:200] if session.qa_pairs else '')

    store.conn.commit()

    # --- PG 分支：双写 PG（失败不阻塞 SQLite） ---
    if hasattr(matcher, 'pg_conn') and matcher.pg_conn:
        try:
            # 从 topic_blocks 取最新 body/summary（SQLite 刚 commit 完）
            row = store.conn.execute(
                'SELECT body, summary FROM topic_blocks WHERE id=?', (topic_id,)
            ).fetchone()
            body_text = row[0] if row and row[0] else text[:2000]
            summary = row[1] if row and row[1] is not None else ''
            store.upsert_topic(
                title=topic_name, summary=summary, body=body_text,
                keywords=[], topic_id=str(topic_id),
                pg_conn=matcher.pg_conn,
            )
            store.add_entry(
                topic_id=str(topic_id), source=session.session_id,
                question=session.qa_pairs[0].content[:200] if session.qa_pairs else '',
                pg_conn=matcher.pg_conn,
            )
        except Exception as e:
            logger.warning("PG _update_topic failed (SQLite still written): %s", _safe_err(e)[:100])
    # --- PG 分支结束 ---

    matcher._refresh()


def scan_new_sessions(
    adapter: HermesAdapter,
    tracker: SessionTracker,
    matcher: TopicMatcher,
    limit: int = 30,
    pool: Any = None,
) -> dict:
    """cron 调用的定时路径 — 优先从 PG 读（含连续块检测），PG 不可用或无新数据时回退 state.db"""
    stats = {'skipped': 0, 'matched': 0, 'buffered': 0, 'errors': 0,
             'source': 'pg', 'blocks': 0}

    pg_blocks_found = False
    try:
        if pool is not None:
            blocks = _fetch_blocks_from_pg(limit=limit, pool=pool)
        else:
            blocks = _fetch_blocks_from_pg(limit=limit)
        if blocks:
            pg_blocks_found = True
            stats['source'] = 'pg'
            stats['blocks'] = len(blocks)
            for fb in blocks:
                try:
                    result = _refine_block(fb, tracker, matcher)
                    stats[result] = stats.get(result, 0) + 1
                    if result == 'match':
                        stats['matched'] += 1
                except Exception as e:
                    logger.warning("pg_refine error %s: %s",
                                   fb.get('session_id','?')[:12], _safe_err(e)[:100])
                    stats['errors'] += 1
        else:
            # PG 连通但无新块 → 仍走 state.db 兜底（处理 PG 漏掉或未上报的 session）
            logger.info("pg_blocks 为空, 补充检查 state.db")
    except Exception as e:
        # PG 不可达 → 回退 state.db
        logger.warning("pg_fetch 失败, 回退 state.db: %s", _safe_err(e)[:100])

    # 回退：state.db（无论 PG 是否有数据，都跑一遍 tracker 中未标记的 session）
    # 注意：refine_turn 内部已经检查 turn_count<=last_processed → skip；
    # 这里再叠一层 tracker.get_status 守卫，避免无谓的匹配调用。
    stats['source'] = 'state_db' if not pg_blocks_found else stats['source'] + '+state_db'
    sessions = adapter.fetch_sessions(limit=limit, min_messages=MIN_TURNS)
    for s in sessions:
        # 性能守卫：tracker 已有标记的 session 跳过
        # SessionTracker.get() 对不存在的 session 返回默认值 (turn_count=0)；
        # 利用 rowcount 或 turn_count>0 判定是否已经在 tracker 里。
        existing = tracker.get(s.session_id)
        if existing.get('turn_count', 0) > 0:
            stats['skipped'] += 1
            continue
        try:
            result = refine_turn(s, tracker, matcher)
            stats[result] = stats.get(result, 0) + 1
            if result == 'match':
                stats['matched'] += 1
        except Exception as e:
            logger.warning("refine error %s: %s", s.session_id[:12], _safe_err(e)[:100])
            stats['errors'] += 1
    return stats


def _fetch_blocks_from_pg(limit: int = 30, min_turns: int = 3,
                          max_gap_sec: int = 300, pool: Any = None):
    """从 PG 读未处理的 session 消息 → 按时间间隙切连续块
    
    返回: list[dict], 每个 dict:
        session_id, block_idx, qa_pairs=[(role, content, timestamp), ...]
    """
    rows = []
    if pool is not None:
        lease = None
        try:
            lease = pool.lease(timeout=5)
            conn = lease.connection
            try:
                cur = conn.cursor()
                try:
                    cur.execute("""
            SELECT session_id as sid,
                   content, role,
                   timestamp
            FROM conversation_stream
            WHERE source IN ('live_buffer', 'live_sync')
              AND role IN ('user', 'assistant')
            ORDER BY timestamp ASC
        """)
                    rows = cur.fetchall()
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("pg query 失败: %s", _safe_err(e)[:80])
                return []
        except Exception as e:
            logger.warning("pg lease/query 失败: %s", _safe_err(e)[:80])
            return []
        finally:
            if lease is not None:
                try:
                    lease.close()
                except Exception:
                    pass
    else:
        try:
            import psycopg2
            from .config import resolve_config, _resolve_data_dir
            cfg = resolve_config()
            pg_cfg = cfg.pg
            conn = psycopg2.connect(
                host=pg_cfg.host, port=pg_cfg.port,
                user=pg_cfg.user, password=pg_cfg.password,
                dbname=pg_cfg.database, connect_timeout=3,
            )
            cur = conn.cursor()
        except Exception as e:
            logger.warning("pg connect 失败: %s", _safe_err(e)[:80])
            return []

        try:
            # 取最近的 live 消息（2026-08-08 融合：v3_messages 已归档，改查 conversation_stream source='live_buffer'）
            cur.execute("""
            SELECT session_id as sid,
                   content, role,
                   timestamp
            FROM conversation_stream
            WHERE source IN ('live_buffer', 'live_sync')
              AND role IN ('user', 'assistant')
            ORDER BY timestamp ASC
        """)
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            conn.close()
            logger.warning("pg query 失败: %s", _safe_err(e)[:80])
            return []
    
    # 按 session_id 分组
    sessions = {}
    for sid, content, role, ts in rows:
        if sid not in sessions:
            sessions[sid] = []
        sessions[sid].append({
            'content': content, 'role': role, 'timestamp': ts,
        })
    
    # 对每个 session: 切连续块
    blocks = []
    for sid, msgs in sessions.items():
        if len(msgs) < min_turns:
            continue
        
        blk_msgs = []
        prev_ts = None
        for m in msgs:
            ts = m['timestamp']
            if prev_ts and (ts - prev_ts).total_seconds() > max_gap_sec:
                # 切块 — 当前块足够大才保留
                if len(blk_msgs) >= min_turns:
                    blocks.append({
                        'session_id': sid,
                        'block_idx': len(blocks),
                        'qa_pairs': blk_msgs[:],
                    })
                blk_msgs = [m]
            else:
                blk_msgs.append(m)
            prev_ts = ts
        
        # 最后一块
        if len(blk_msgs) >= min_turns:
            blocks.append({
                'session_id': sid,
                'block_idx': len(blocks),
                'qa_pairs': blk_msgs[:],
            })
    
    # 按时间排序，取最近 limit 个块
    blocks.sort(key=lambda b: b['qa_pairs'][-1]['timestamp'], reverse=True)
    return blocks[:limit]


def _refine_block(block: dict, tracker: SessionTracker,
                  matcher: TopicMatcher) -> str:
    """对一个连续块做主题匹配
    
    返回: 'skip' / 'match' / 'buffer'
    """
    sid = block['session_id']
    qa_pairs = block['qa_pairs']
    block_idx = block['block_idx']
    turn_count = len(qa_pairs)
    
    state = tracker.get(sid)
    last_processed = state.get('last_processed_turn', 0)
    
    # 已处理过的 session 跳过（整 session 已处理，子块由 on_session_end 处理）
    if state['status'] == 'processed' and state['turn_count'] >= turn_count:
        return 'skip'
    
    # 拼文本
    parts = []
    for m in qa_pairs:
        prefix = 'Q: ' if m['role'] == 'user' else 'A: '
        parts.append(f"{prefix}{m['content'][:300]}")
    text = '\n'.join(parts)
    
    if len(text.strip()) < 30:
        return 'skip'
    
    # 匹配
    matches = matcher.match(text, threshold=0.4)
    
    if not matches:
        # 未匹配 → 进孤儿缓冲
        _buffer_orphan(sid, block_idx, qa_pairs, text, tracker)
        tracker.upsert(sid, turn_count, time.time(), 'new', None, last_processed)
        return 'buffer'
    
    tid = matches[0][1]
    tname = matches[0][2]
    cos = matches[0][0]
    
    if cos >= 0.6:
        # 中等以上 → 归类
        _update_topic_block(sid, tid, tname, qa_pairs, text, matcher)
        _sync_card_to_db(tid, tname, matcher)  # 同步到 v3_cards.db
        tracker.upsert(sid, turn_count, time.time(), 'processed', tid, turn_count)
        logger.info("pg_block: %s[%d] → %s (%.4f)", sid[:12], block_idx, tname, cos)
        return 'match'
    
    # cos < 0.6 → 缓冲等后续确认
    _buffer_orphan(sid, block_idx, qa_pairs, text, tracker)
    tracker.upsert(sid, turn_count, time.time(), 'buffer', None, last_processed)
    return 'buffer'


def _update_topic_block(sid: str, topic_id: int, topic_name: str,
                        qa_pairs: list, text: str, matcher: TopicMatcher):
    """更新主题（追加 body + 写条目）"""
    store = matcher.store
    first_q = qa_pairs[0]['content'][:200] if qa_pairs else ''
    
    row = store.conn.execute(
        'SELECT body FROM topic_blocks WHERE id=?', (topic_id,)
    ).fetchone()
    
    if row and row[0]:
        new_body = row[0] + '\n\n---\n' + text[:500]
        store.conn.execute(
            'UPDATE topic_blocks SET body=?, updated_at=datetime("now") WHERE id=?',
            (new_body[:5000], topic_id))
    else:
        store.conn.execute(
            'UPDATE topic_blocks SET body=?, updated_at=datetime("now") WHERE id=?',
            (text[:2000], topic_id))
    
    store.add_entry(topic_id=topic_id, source=f'pg_block_{sid}', question=first_q)
    store.conn.commit()

    # --- PG 分支：双写 PG（失败不阻塞 SQLite） ---
    if hasattr(matcher, 'pg_conn') and matcher.pg_conn:
        try:
            # 从 topic_blocks 取当前 summary（body 已经被 SQLite 写过了）
            row = store.conn.execute(
                'SELECT body, summary FROM topic_blocks WHERE id=?', (topic_id,)
            ).fetchone()
            body_text = row[0] if row and row[0] else text[:2000]
            summary = row[1] if row and row[1] is not None else ''
            store.upsert_topic(
                title=topic_name, summary=summary, body=body_text,
                keywords=[], topic_id=str(topic_id),
                pg_conn=matcher.pg_conn,
            )
            store.add_entry(
                topic_id=str(topic_id), source=f'pg_block_{sid}',
                question=first_q, pg_conn=matcher.pg_conn,
            )
        except Exception as e:
            logger.warning("PG _update_topic_block failed (SQLite still written): %s", _safe_err(e)[:100])
    # --- PG 分支结束 ---

    matcher._refresh()


def _buffer_candidate(session: SessionEvents, candidate_tid: int, candidate_tname: str,
                      tracker: SessionTracker):
    """稳定匹配但不确定 → 写 buffer + candidate 标记，不入 topic_entries"""
    store = tracker.store
    text = get_cumulative_text(session)
    try:
        sid = session.session_id
        first_q = session.qa_pairs[0].content[:200] if session.qa_pairs else ''
        store.conn.execute("""
            INSERT OR REPLACE INTO topic_buffer
                (turn_id, timestamp, summary, primary_topic_id, primary_action, qa_pairs, new_topic_draft)
            VALUES (?, ?, ?, ?, 'candidate', ?, ?)
        """, (
            f"candidate_{sid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            datetime.now().isoformat(),
            text[:500],
            str(candidate_tid) if candidate_tid else None,
            json.dumps([(qa.role, qa.content[:200]) for qa in session.qa_pairs[:10]], ensure_ascii=False, default=str),
            candidate_tname or '',
        ))
        store.conn.commit()
    except Exception as e:
        logger.warning("buffer_candidate error %s: %s", sid[:12], _safe_err(e)[:100])


def _buffer_orphan(sid: str, block_idx: int, qa_pairs: list,
                   text: str, tracker: SessionTracker):
    """未匹配块 → 写入 topic_buffer 等待二次聚簇"""
    store = tracker.store
    try:
        first_q = qa_pairs[0]['content'][:200] if qa_pairs else ''
        store.conn.execute("""
            INSERT OR REPLACE INTO topic_buffer
                (turn_id, timestamp, summary, qa_pairs, primary_action, new_topic_draft)
            VALUES (?, ?, ?, ?, 'orphan', ?)
        """, (
            f"pg_orphan_{sid}_b{block_idx:04d}",
            datetime.now().isoformat(),
            text[:500],
            json.dumps(qa_pairs[:10], ensure_ascii=False, default=str),
            first_q,
        ))
        store.conn.commit()
        # 写入后立即触发 seed 检查 — 甜点配对创建小主题卡
        try:
            from .topic_maintain import _try_seed_from_buffer
            _try_seed_from_buffer(dry_run=False)
        except Exception:
            pass  # seed 检查失败不阻塞 buffer 写入
    except Exception as e:
        logger.warning("buffer_orphan error %s: %s", sid[:12], _safe_err(e)[:100])


def _sync_card_to_db(topic_id: int, topic_name: str, matcher: TopicMatcher):
    """匹配后同步更新 v3_cards.db 中的对应主题卡"""
    store = matcher.store
    try:
        row = store.conn.execute(
            'SELECT id, title, body, summary, embedding FROM topic_blocks WHERE id=?',
            (topic_id,)
        ).fetchone()
        if not row or not row[2]:
            return  # 无 body 不更新

        body = row[2]
        title = row[1]
        summary = row[3] or ''
        topic_emb_json = row[4]  # topic_blocks 里是 JSON 字符串

        if len(body.strip()) < 50:
            return

        import sqlite3 as _sqlite3
        cards_db = _resolve_data_dir() / 'v3_cards.db'
        if not cards_db.exists():
            return

        # ── Auto-calc embedding: 修复幽灵卡 ─────────────────────
        # topic_blocks 没有 embedding → v3_cards 也不会有, 召回看到空卡.
        # 优先复用 topic_blocks 已有的; 没有就自动从 title+summary 算.
        emb_blob = None
        try:
            from .sqlite_store import _emb_to_blob
            emb_list = None
            if topic_emb_json:
                if isinstance(topic_emb_json, (bytes, bytearray)):
                    # 兜底: 如果哪天被改成 BLOB 也兼容
                    from .sqlite_store import _blob_to_emb as _b2e
                    emb_list = _b2e(bytes(topic_emb_json))
                elif isinstance(topic_emb_json, str):
                    emb_list = json.loads(topic_emb_json)
            if not emb_list:
                # 自动算
                from .embedding import BATCH_EMBED_POLICY, safe_embed_cfg
                from .embed_failures import embed_for_write
                from .config import resolve_config, _resolve_data_dir
                cfg = resolve_config()
                embed_cfg = safe_embed_cfg(cfg)
                if embed_cfg is None:
                    emb_list = None
                else:
                    # Derived state; the card row is the asset. This is a maintenance/
                    # sync path, not the 8s realtime budget, so it takes the batch policy
                    # and records a durable marker instead of only a warning.
                    text = f"{title}. {summary}".strip()[:1000]
                    _out = embed_for_write(
                        text, embed_cfg,
                        entity_table="topic_blocks", entity_id=str(topic_id),
                        phase="card_sync",
                        policy=BATCH_EMBED_POLICY,
                    )
                    emb_list = _out.vector
                    if not _out.ok:
                        logger.warning(
                            "sync_card embedding %s for %s: class=%s retryable=%s "
                            "marker_recorded=%s",
                            _out.status.value, str(topic_id)[:12], _out.error_class,
                            _out.retryable, _out.marker_recorded,
                        )
            if emb_list:
                emb_blob = _emb_to_blob(emb_list)
        except ValueError:
            raise
        except Exception as _emb_e:
            logger.warning("sync_card embedding auto-calc failed %s: %s",
                           str(topic_id)[:12], str(_emb_e)[:100])
            emb_blob = None

        conn = _sqlite3.connect(str(cards_db))
        source_id = f'topic_{topic_id}'
        now = datetime.now().isoformat()

        existing = conn.execute(
            'SELECT source_id FROM cards WHERE source_id=?', (source_id,)
        ).fetchone()

        if existing:
            # UPDATE 时如果之前没 embedding, 补上
            if emb_blob:
                conn.execute(
                    'UPDATE cards SET content=?, updated_at=?, embedding=? WHERE source_id=?',
                    (body, now, emb_blob, source_id))
            else:
                conn.execute(
                    'UPDATE cards SET content=?, updated_at=? WHERE source_id=?',
                    (body, now, source_id))
        else:
            # INSERT 时把 embedding 一起写进去
            if emb_blob:
                conn.execute('''
                    INSERT INTO cards (source_id, category, title, content, source,
                                       embedding, created_at, updated_at)
                    VALUES (?, 'topic', ?, ?, 'topic_pipeline', ?, ?, ?)
                ''', (source_id, topic_name, body, emb_blob, now, now))
            else:
                conn.execute('''
                    INSERT INTO cards (source_id, category, title, content, source, created_at, updated_at)
                    VALUES (?, 'topic', ?, ?, 'topic_pipeline', ?, ?)
                ''', (source_id, topic_name, body, now, now))

        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("sync_card error %s: %s", str(topic_id)[:12], _safe_err(e)[:100])


def scan_all_unprocessed(
    adapter: HermesAdapter,
    tracker: SessionTracker,
    matcher: TopicMatcher,
    max_sessions: int = 100
) -> dict:
    """全量扫描 — 处理所有未处理的 session（首次部署 / 重扫）"""
    stats = {'scanned': 0, 'matched': 0, 'buffered': 0, 'skipped': 0}

    sessions = adapter.fetch_sessions(limit=max_sessions, min_messages=MIN_TURNS)
    total = len(sessions)

    for i, s in enumerate(sessions):
        state = tracker.get(s.session_id)
        if state['status'] == 'processed' and state['turn_count'] >= len(s.qa_pairs):
            stats['skipped'] += 1
            continue

        result = refine_turn(s, tracker, matcher)
        stats[result] = stats.get(result, 0) + 1

        if (i + 1) % 20 == 0:
            logger.info("scan: %d/%d", i + 1, total)

    logger.info("scan done: matched=%d buffered=%d skipped=%d",
                stats['matched'], stats['buffered'], stats['skipped'])
    return stats
