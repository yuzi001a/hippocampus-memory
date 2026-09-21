"""v3_topic_maintain — 主题自动维护

用途：Phase 6.3
- 孤儿二次聚簇：topic_buffer 够 N 条后 HDBSCAN → 新主题
- 休眠标记：超过 DORMANT_DAYS 无新条目的主题标记为 dormant

可 cron 调用（建议每日一次），也可手动调。
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timezone
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

from .config import _resolve_data_dir
from .pg_pool import DEFAULT_LEASE_TIMEOUT

logger = logging.getLogger("v3core.topic_maintain")

SEED_COSINE = 0.65       # 甜点阈值 — 实验验证（2026-07-12）
BUFFER_CLUSTER_MIN = 3       # 至少 N 条孤儿才触发二次聚簇（2026-07-12 从 5 降到 3）
DORMANT_DAYS = 14             # 超过 N 天无新条目标记休眠
DB_PATH = lambda config=None: str(_resolve_data_dir(config) / 'v3_topic_full.db')


def _get_store(config=None):
    from .topic_store import TopicStore
    return TopicStore(DB_PATH(config))


def _llm_cfg():
    """从 .env 加载 LLM 配置（不隐式指向任何厂商，要求显式配置）"""
    env_path = os.path.expanduser("~/AppData/Local/hermes/.env")
    if not os.path.exists(env_path):
        return None
    api_key = ""
    for line in open(env_path, encoding="utf-8"):
        stripped = line.strip()
        if stripped.startswith("LLM_API_KEY="):
            api_key = stripped.split("=", 1)[1]
            break
        if stripped.startswith("MINIMAX_CN_API_KEY="):
            api_key = stripped.split("=", 1)[1]
            break
    if not api_key:
        return None
    return {
        "endpoint": os.environ.get("LLM_BASE_URL", ""),
        "model": os.environ.get("LLM_MODEL", ""),
        "api_key": api_key,
        "proxy": os.environ.get("V3CORE_LLM_PROXY", ""),
    }


def _try_seed_from_buffer(dry_run: bool = False, config=None) -> dict:
    """检查 orphan buffer 中 pairwise cos ≥ SEED_COSINE 的条目对，创建 seed topic

    种子主题（小主题卡）让相关条目立即成簇，不依赖 cron 批量聚簇。
    seed topic 与正式 topic 结构相同，但自信度低、不参与 recall 主力。

    返回: {'action': 'seeded'/'skip', 'seeds': N, 'seeded_ids': [id, ...]}
    """
    store = _get_store(config)
    result = {"action": "skip", "reason": "", "seeds": 0, "seeded_ids": []}

    try:
        buffers = store.conn.execute(
            "SELECT id, turn_id, summary, qa_pairs, timestamp FROM topic_buffer "
            "WHERE primary_action='orphan' ORDER BY id"
        ).fetchall()

        if len(buffers) < 2:
            result["reason"] = "不足 2 条 orphan"
            return result

        # 取文本 + 算 embedding
        import numpy as np
        from .embedding import call_embedding, safe_embed_cfg
        from .config import resolve_config

        cfg = config if config is not None else resolve_config()
        embed_cfg = safe_embed_cfg(cfg)
        if embed_cfg is None:
            result["reason"] = "embedding 未配置"
            return result

        items = []
        for row in buffers:
            text = (row[2] or "")[:300] or (row[3] or "")[:300]
            if text.strip():
                # Analysis-time embedding for in-memory clustering: it is never persisted
                # as an entity's vector, so there is no durable NULL to explain and no
                # marker is written. The batch policy still matters — inheriting the 3s/0
                # realtime default silently shrank the candidate set.
                from .embedding import BATCH_EMBED_POLICY
                emb = call_embedding(text[:1000], embed_cfg, policy=BATCH_EMBED_POLICY)
                if emb:
                    items.append({"id": row[0], "text": text, "turn_id": row[1], "emb": np.array(emb)})

        if len(items) < 2:
            result["reason"] = f"有效 embedding 不足 2"
            return result

        # 找所有 pairwise cos ≥ SEED_COSINE 的对
        seeded_ids = set()
        seeds = 0

        for i in range(len(items)):
            if items[i]["id"] in seeded_ids:
                continue
            for j in range(i + 1, len(items)):
                if items[j]["id"] in seeded_ids:
                    continue

                cos = float(items[i]["emb"] @ items[j]["emb"] /
                           (np.linalg.norm(items[i]["emb"]) * np.linalg.norm(items[j]["emb"])))
                if cos >= SEED_COSINE:
                    # 找到一对 → 创建种子主题
                    if dry_run:
                        seeded_ids.add(items[i]["id"])
                        seeded_ids.add(items[j]["id"])
                        seeds += 1
                        continue

                    title_text = items[i]["text"][:100]

                    # 用 LLM 命名
                    try:
                        llm_cfg = _llm_cfg()
                        if llm_cfg:
                            title = _name_cluster(f"{items[i]['text']} {items[j]['text']}", llm_cfg)
                        else:
                            title = None
                    except Exception:
                        title = None

                    if not title or title == "auto_cluster":
                        title = f"seed_{items[i]['text'][:20]}…"

                    body = f"小主题种子 — 聚合于时间片\n\n{items[i]['text'][:500]}\n\n{items[j]['text'][:500]}"

                    tid = store.upsert_topic(
                        title=title[:80],
                        summary=items[i]["text"][:200],
                        body=body,
                        category="topic",
                    )

                    # 写 entries
                    for it in [items[i], items[j]]:
                        store.add_entry(topic_id=tid, source=it["turn_id"], question=it["text"][:200])

                    # 标记清理
                    seeded_ids.add(items[i]["id"])
                    seeded_ids.add(items[j]["id"])
                    seeds += 1
                    break  # items[i] 已配对，不再找其他 j

        # 删除已配对的 buffer 条目
        if seeded_ids and not dry_run:
            ids_list = list(seeded_ids)
            placeholders = ", ".join("?" for _ in ids_list)
            store.conn.execute(f"DELETE FROM topic_buffer WHERE id IN ({placeholders})", ids_list)
            store.conn.commit()

        result = {
            "action": "seeded" if seeds > 0 else "skip",
            "seeds": seeds,
            "seeded_ids": list(seeded_ids),
            "pair_checked": len(items),
        }
        return result

    except ValueError:
        raise
    except Exception as e:
        logger.warning("seed 创建异常: %s", _safe_err(e)[:200])
        result["reason"] = _safe_err(e)[:100]
        return result


def cluster_orphans(dry_run: bool = False, config=None) -> dict:
    """topic_buffer 孤儿 → 二次聚簇 → 新主题

    步骤：
    1. 先跑 _try_seed_from_buffer() — 甜点阈值配对小簇
    2. 剩余 buffer 再进 HDBSCAN / _simple_cluster 批量聚簇
    3. 兜底：_drain_old_candidates() 24h+ candidate 回填

    副作用：无论 HDBSCAN 是否聚簇成功，调用 _drain_old_candidates() 把 24h 以上的
    candidate 兜底回填到候选 topic，避免稳定匹配结果被无限期搁置。
    """
    # Step 1: 种子创建（甜点阈值 0.65）
    seed_result = _try_seed_from_buffer(dry_run=dry_run, config=config)
    logger.info("seed check: %s", seed_result)

    store = _get_store(config)
    drained_count = 0
    final_result: dict = {"action": "skip", "reason": "no-op"}
    try:
        buffers = store.conn.execute(
            "SELECT id, turn_id, summary, qa_pairs, timestamp FROM topic_buffer ORDER BY id"
        ).fetchall()

        if len(buffers) < BUFFER_CLUSTER_MIN:
            n = len(buffers)
            final_result = {"action": "skip", "reason": f"buffer 不足 {BUFFER_CLUSTER_MIN} 条（当前 {n}）", "count": n}
            return final_result

        # 取所有 buffer 的文本
        texts = []
        ids = []
        for row in buffers:
            text = (row[2] or "")[:300] or (row[3] or "")[:300]
            texts.append(text)
            ids.append(row[0])

        if dry_run:
            final_result = {"action": "preview", "buffer_count": len(buffers), "texts_preview": texts[:3]}
            return final_result

        # 算 embedding → 聚簇
        try:
            import numpy as np
            from .embedding import call_embedding, safe_embed_cfg
            from .config import resolve_config
            from . import topic_cluster as tc

            cfg = config if config is not None else resolve_config()
            embed_cfg = safe_embed_cfg(cfg)
            if embed_cfg is None:
                final_result = {"action": "skip", "reason": "embedding 未配置"}
                return final_result

            embs = []
            valid_texts = []
            valid_ids = []
            for t, bid in zip(texts, ids):
                if t.strip():
                    # Same rationale as _try_seed_from_buffer: analysis-time only, not a
                    # persisted entity vector, so no marker — but it must not inherit the
                    # realtime 3s/0 default.
                    from .embedding import BATCH_EMBED_POLICY
                    emb = call_embedding(t[:1000], embed_cfg, policy=BATCH_EMBED_POLICY)
                    if emb:
                        embs.append(emb)
                        valid_texts.append(t)
                        valid_ids.append(bid)

            if len(embs) < 3:
                final_result = {"action": "skip", "reason": f"有效向量不足 3 条（{len(embs)}）"}
                return final_result

            # 小样本（<8条）直接用余弦相似度分组，避免 HDBSCAN 在 1024d 空间失效
            if len(embs) < 8:
                # _simple_cluster 返回 [{'label':'cluster_0', 'members':[texts]}], 转成统一格式
                groups = tc._simple_cluster(embs, valid_texts)
                clusters = {}
                for g in groups:
                    cid = len(clusters)
                    for mem in g.get('members', []):
                        idx = valid_texts.index(mem) if mem in valid_texts else -1
                        if idx >= 0:
                            clusters.setdefault(cid, []).append({
                                "id": valid_ids[idx],
                                "text": valid_texts[idx],
                            })
            else:
                # cluster_embeddings 返回 [{'label':'cluster_X','members':[texts]}]
                dict_groups = tc.cluster_embeddings(embs, valid_texts, min_cluster_size=3)
                if not dict_groups:
                    # HDBSCAN 在样本少时全标噪音 → 回退 _simple_cluster
                    logger.info("HDBSCAN 无结果, 回退 _simple_cluster")
                    dict_groups = tc._simple_cluster(embs, valid_texts)
                    if not dict_groups:
                        final_result = {"action": "skip", "reason": "聚类无结果"}
                        return final_result
                clusters = {}
                for g in dict_groups:
                    cls_id = len(clusters)
                    for mem in g.get('members', []):
                        idx = valid_texts.index(mem) if mem in valid_texts else -1
                        if idx >= 0:
                            clusters.setdefault(cls_id, []).append({
                                "id": valid_ids[idx],
                                "text": valid_texts[idx],
                            })

            if not clusters:
                final_result = {"action": "skip", "reason": "全部为噪音，无有效簇"}
                return final_result

            # 对每个簇创建新主题
            llm = _llm_cfg()
            now = datetime.now().isoformat()
            new_topics = 0

            for cluster_id, items in clusters.items():
                min_cluster = 2 if len(embs) < 8 else 3
                if len(items) < min_cluster:
                    continue

                # 用 LLM 命名（如果可用）
                cluster_text = " ".join(it["text"] for it in items[:5])
                title = _name_cluster(cluster_text, llm) if llm else f"orphan_cluster_{cluster_id}"
                # 避免兜底名导致多簇塌缩（_topic_id 从 title 生成确定性 ID）
                if not title or title == "auto_cluster":
                    title = f"topic_cluster_{cluster_id}"

                # 写入新 topic_block
                tid = store.upsert_topic(
                    title=title[:50],
                    summary=cluster_text[:300],
                    body=cluster_text[:2000],
                )

                # 把 buffer 条目挂到新主题（写入 topic_entries + 清理 buffer）
                for it in items:
                    buf_row = store.conn.execute(
                        "SELECT turn_id, summary, qa_pairs FROM topic_buffer WHERE id=?",
                        (it["id"],)
                    ).fetchone()
                    if buf_row:
                        source = buf_row[0] or f"orphan_{it['id']}"
                        question = (buf_row[1] or buf_row[2] or "")[:200]
                    else:
                        source = f"orphan_{it['id']}"
                        question = (it.get('text', '') or '')[:200]
                    store.add_entry(topic_id=tid, source=source, question=question)
                    store.conn.execute(
                        "DELETE FROM topic_buffer WHERE id=?", (it["id"],)
                    )
                new_topics += 1

            store.conn.commit()
            final_result = {
                "action": "cluster",
                "new_topics": new_topics,
                "clusters": len(clusters),
                "total_buffers": len(buffers),
            }
            return final_result

        except ImportError as e:
            final_result = {"action": "error", "reason": f"缺少依赖（numpy/topic_cluster）: {_safe_err(e)}"}
            return final_result
        except ValueError:
            raise
        except Exception as e:
            final_result = {"action": "error", "reason": _safe_err(e)[:200]}
            return final_result
    finally:
        # 无论上述路径成败，都尝试回填 24h+ 的 candidate 老项
        try:
            drained_count = _drain_old_candidates(store)
        except Exception as e:
            logger.warning("drain_old_candidates 失败: %s", _safe_err(e)[:200])
            drained_count = 0
        try:
            store.close()
        except Exception:
            pass
        # 把 drained_count 合并进最终结果（如果它是 dict）
        if isinstance(final_result, dict):
            final_result["drained_candidates"] = drained_count
        return final_result


def _drain_old_candidates(store) -> int:
    """24h 以上的 candidate 老项 → 回填到候选 topic（不丢数据的安全兜底）"""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    rows = store.conn.execute("""
        SELECT id, turn_id, summary, primary_topic_id, new_topic_draft
        FROM topic_buffer
        WHERE primary_action='candidate' AND timestamp < ?
    """, (cutoff,)).fetchall()

    drained = 0
    for row in rows:
        tid = row[3]
        if not tid:
            # candidate_topic_id 为空 → 彻底孤儿，跳过（让 orphan 流程继续管）
            continue
        try:
            # topic_blocks.id 是 TEXT 格式 "t_xxxxxx"，int() 会抛 ValueError
            tid_str = str(tid) if tid else None
            store.add_entry(topic_id=tid_str, source=row[1],
                            question=(row[2] or '')[:200])
            store.conn.execute("DELETE FROM topic_buffer WHERE id=?", (row[0],))
            drained += 1
        except Exception:
            continue
    if drained:
        store.conn.commit()
    return drained


def _name_cluster(text: str, llm_cfg: dict) -> str:
    """LLM 给聚类簇命名"""
    if not llm_cfg:
        return "auto_cluster"
    try:
        import requests
        resp = requests.post(
            f"{llm_cfg['endpoint']}/chat/completions",
            headers={"Authorization": f"Bearer {llm_cfg['api_key']}", "Content-Type": "application/json"},
            json={
                "model": llm_cfg["model"],
                "messages": [
                    {"role": "user", "content": f"以下是一组相关对话片段，请用 2-5 个字概括其共同主题：\n\n{text[:800]}"}
                ],
                "max_tokens": 200,
                "temperature": 0.3,
            },
            timeout=30,
        )
        data = resp.json()
        raw = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
        # 剥离 think 块
        import re
        raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        name = raw if raw else "auto_cluster"
        # 清理杂音：只取第一行，不超过 20 字
        name = name.split('\n')[0][:20].strip()
        return name if name else "auto_cluster"
    except Exception:
        return "auto_cluster"


def mark_dormant(dry_run: bool = False, pool=None, config=None) -> dict:
    """标记超过 DORMANT_DAYS 无新条目的主题为 dormant

    双源扫描: SQLite (topic_blocks, 兼容旧卡) + PG (topics, 主力).
    任一路径发现的过期主题都会同步写到两边的表.
    """
    store = None
    _pg_conn = None
    _pg_lease = None
    try:
        store = _get_store(config)
        now = datetime.now(timezone.utc)
        dormant_ids: list[str] = []
        # ── PG 路径: 主力数据源 ──
        # Runtime-backed callers pass the Registry-owned pool.  The legacy branch
        # remains for explicit CLI/offline maintenance only.
        if pool is not None:
            try:
                _pg_lease = pool.lease(timeout=DEFAULT_LEASE_TIMEOUT)
                _pg_conn = _pg_lease.connection
                _pg_cur = _pg_conn.cursor()
                _pg_cur.execute("""
                    SELECT topic_id, status, updated_at
                    FROM topics WHERE status = 'active'
                """)
                pg_topics = _pg_cur.fetchall()
            except Exception as e:
                logger.warning("mark_dormant PG pool 读取失败, 降级 SQLite-only: %s", _safe_err(e)[:100])
                pg_topics = []
        else:
            _pg_password = os.environ.get("V3CORE_PG_PASSWORD", "")
            if _pg_password:
                try:
                    import psycopg2 as _ps
                    _pg_conn = _ps.connect(
                        host=os.environ.get("V3CORE_PG_HOST", "localhost"),
                        port=int(os.environ.get("V3CORE_PG_PORT", "5433")),
                        dbname=os.environ.get("V3CORE_PG_DB", "v3embeddings"),
                        user=os.environ.get("V3CORE_PG_USER", "v3user"),
                        password=_pg_password,
                    )
                    _pg_cur = _pg_conn.cursor()
                    _pg_cur.execute("""
                        SELECT topic_id, status, updated_at
                        FROM topics WHERE status = 'active'
                    """)
                    pg_topics = _pg_cur.fetchall()
                except Exception as e:
                    logger.warning("mark_dormant legacy PG 读取失败, 降级 SQLite-only: %s", _safe_err(e)[:100])
                    pg_topics = []
            else:
                pg_topics = []
        # ── SQLite 路径: 历史兼容 ──
        sqlite_topics = store.conn.execute("""
            SELECT t.id, t.title, t.status,
                   (SELECT MAX(e.timestamp) FROM topic_entries e WHERE e.topic_id = t.id) as last_ts
            FROM topic_blocks t
        """).fetchall()

        # ── 检查单个 topic 是否超期 ──
        def _check_and_mark(tid: str, last_dt) -> bool:
            nonlocal dormant_ids
            if last_dt and (now.replace(tzinfo=None) - last_dt).days >= DORMANT_DAYS:
                dormant_ids.append(tid)
                return True
            return False

        marked = 0
        skipped = 0

        # 处理 SQLite topic (用 topic_entries 时间戳判断)
        for tid, title, status, last_ts in sqlite_topics:
            if status == "dormant":
                skipped += 1
                continue
            if not last_ts:
                continue
            try:
                last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00").split("+")[0])
                if _check_and_mark(tid, last_dt):
                    if not dry_run:
                        store.conn.execute(
                            "UPDATE topic_blocks SET status='dormant', updated_at=? WHERE id=?",
                            (now.isoformat(), tid),
                        )
                    marked += 1
            except Exception as e:
                logger.warning("mark_dormant SQLite 日期解析失败 (tid=%s): %s", tid, _safe_err(e)[:100])

        # 处理 PG topic (用 updated_at 判断)
        pg_marked_ids: list[str] = []
        for tid, status, updated_at in pg_topics:
            if status == "dormant":
                continue
            if updated_at is None:
                continue
            try:
                if (now.replace(tzinfo=None) - updated_at.replace(tzinfo=None)).days >= DORMANT_DAYS:
                    if _check_and_mark(tid, updated_at):
                        pg_marked_ids.append(tid)
                        if not dry_run:
                            marked += 1
            except Exception as e:
                logger.warning("mark_dormant PG 日期检查失败 (tid=%s): %s", tid, _safe_err(e)[:100])

        # ── 写入 ──
        if not dry_run:
            store.conn.commit()
            if _pg_conn and dormant_ids:
                try:
                    _pg_cur = _pg_conn.cursor()
                    for _tid in dormant_ids:
                        _pg_cur.execute(
                            "UPDATE topics SET status='dormant', updated_at=NOW() WHERE topic_id=%s AND status='active'",
                            (_tid,)
                        )
                    _pg_conn.commit()
                    logger.info("mark_dormant PG: %d topic(s) → dormant", len(dormant_ids))
                except Exception as e:
                    logger.warning("mark_dormant PG 写入失败: %s", _safe_err(e)[:200])


        return {
            "action": "preview" if dry_run else "mark",
            "dormant_count": marked,
            "already_dormant": skipped,
            "dormant_ids": dormant_ids[:10],
            "pg_scanned": len(pg_topics),
            "sqlite_scanned": len(sqlite_topics),
        }
    finally:
        try:
            if _pg_lease is not None:
                _pg_lease.close()
            elif _pg_conn is not None:
                _pg_conn.close()
        finally:
            if store is not None:
                store.close()


def run_maintenance(dry_run: bool = False, pool=None, config=None) -> dict:
    """入口：跑全部维护任务"""
    result = {}
    result["orphan"] = cluster_orphans(dry_run=dry_run, config=config)
    result["dormant"] = mark_dormant(dry_run=dry_run, pool=pool, config=config)
    result["dry_run"] = dry_run
    return result


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    result = run_maintenance(dry_run=dry)
    print(json.dumps(result, ensure_ascii=False, indent=2))
