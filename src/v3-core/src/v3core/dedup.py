"""去重引擎 — cosine 快速去重 + numpy 聚类合并 (SQLite 版)

V2: 不再使用文件 rename / cards/archive/ 目录, 改为 SQLite archived_at 字段.
"""

from __future__ import annotations
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .card_store import DeepStore


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

logger = logging.getLogger("v3core.dedup")

DEDUP_COSINE_THRESHOLD = 0.95
MERGE_THRESHOLD = 0.95


def check_duplicate(store: DeepStore, title: str, content: str,
                     category: str, embedding: list[float] | None = None,
                     threshold: float = DEDUP_COSINE_THRESHOLD) -> str | None:
    """检查重复 — 标题精确匹配即判重"""
    index = store.get_index()
    for rel, meta in index.get("files", {}).items():
        if meta.get("category") != category:
            continue
        if meta.get("title") == title:
            return rel
    return None


def dedup_daily(store: DeepStore, config: dict | None = None) -> dict:
    """每日快速去重 — 标题精确匹配, O(n) — SQLite 版"""
    index = store.get_index()
    files = index.get("files", {})
    total = len(files)
    removed = 0
    seen: dict[str, str] = {}
    for rel in sorted(files.keys()):
        meta = files[rel]
        title = meta.get("title", "")
        if not title:
            continue
        if title in seen:
            try:
                store.delete_card(rel)
                removed += 1
            except Exception:
                pass
        else:
            seen[title] = rel
    return {"total": total, "removed": removed, "remaining": total - removed}


# ── LLM 聚类合并 (numpy + union-find) ──

def _find_clusters(
    source_ids: list[str],
    embeddings: np.ndarray,
    threshold: float = MERGE_THRESHOLD,
) -> list[list[str]]:
    """并查集找 >= threshold 的连通分量"""
    if len(embeddings) == 0:
        return []
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    A = embeddings / norms
    sim = A @ A.T
    np.fill_diagonal(sim, 0.0)

    n = len(source_ids)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(n):
        for j in range(i + 1, n):
            if sim[i][j] >= threshold:
                union(i, j)

    clusters_dict: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters_dict[find(i)].append(i)

    clusters = [
        [source_ids[i] for i in idxs]
        for idxs in clusters_dict.values()
        if len(idxs) >= 2
    ]
    clusters.sort(key=len, reverse=True)
    return clusters


def _pick_strongest(
    cluster: list[str],
    embeddings: np.ndarray,
    source_ids: list[str],
) -> str:
    """挑簇内平均 cosine 最高的卡当最强"""
    sid_idx = {sid: i for i, sid in enumerate(source_ids)}
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    A = embeddings / norms

    avg_scores: dict[str, float] = {}
    for sid in cluster:
        i = sid_idx[sid]
        scores = []
        for other_sid in cluster:
            if other_sid == sid:
                continue
            j = sid_idx[other_sid]
            scores.append(float(A[i] @ A[j]))
        avg_scores[sid] = sum(scores) / len(scores) if scores else 0.0

    return max(cluster, key=lambda s: (avg_scores[s], s or ""))


def build_merge_plan(
    store: DeepStore,
    pg: Any,
    threshold: float = MERGE_THRESHOLD,
) -> dict:
    """生成合并计划 (不写盘)"""
    try:
        sids, titles, embs_list = pg.get_all_card_embeddings()
    except Exception as e:
        return {"success": False, "error": _safe_err(e)}

    if not sids:
        return {"success": False, "error": "no card embeddings found"}

    embs = np.array(embs_list, dtype=np.float32)
    clusters = _find_clusters(sids, embs, threshold=threshold)

    plan_clusters = []
    total_to_merge = 0
    for cluster in clusters:
        keep_sid = _pick_strongest(cluster, embs, sids)
        merge_sids = [s for s in cluster if s != keep_sid]
        plan_clusters.append({
            "keep": keep_sid,
            "merge": merge_sids,
            "merge_count": len(merge_sids),
        })
        total_to_merge += len(merge_sids)

    return {
        "success": True,
        "total_cards": len(sids),
        "threshold": threshold,
        "clusters_found": len(clusters),
        "total_to_merge": total_to_merge,
        "estimated_remaining": len(sids) - total_to_merge,
        "clusters": plan_clusters,
    }


def execute_merge_plan(
    plan: dict,
    store: DeepStore,
    pg: Any,
    dry_run: bool = True,
) -> dict:
    """落合并动作 — 保留最强卡, 其他卡 archived_at 软删除 (SQLite 版)"""
    if not plan.get("success"):
        return {"success": False, "error": "invalid plan"}

    merged = []
    failed = []

    for cluster in plan.get("clusters", []):
        keep_sid = cluster["keep"]
        merge_sids = cluster["merge"]

        if not dry_run:
            for sid in merge_sids:
                try:
                    # SQLite 软删除 (设置 archived_at)
                    store.sqlite.archive_card(sid)
                    # 也从 PG 删除
                    pg.delete_card(sid)
                except Exception as e:
                    logger.warning("合并归档 %s 失败: %s", sid, _safe_err(e)[:200])
                    failed.append({"sid": sid, "error": _safe_err(e)[:200]})

        merged.append({
            "keep": keep_sid,
            "merge_count": len(merge_sids),
        })

    return {
        "success": True,
        "dry_run": dry_run,
        "merged_clusters": len(merged),
        "failed_clusters": len(failed),
        "total_cards_moved": sum(f["merge_count"] for f in merged),
        "archive_mode": "sqlite_archived_at",  # 标识: 不再使用目录归档
        "details": merged[:20],
    }


def dedup_with_llm(
    store: DeepStore,
    pg: Any,
    threshold: float = MERGE_THRESHOLD,
    dry_run: bool = True,
) -> dict:
    """一站式: 扫描 -> 聚类 -> 归档"""
    plan = build_merge_plan(store, pg, threshold)
    if not plan.get("success"):
        return plan
    result = execute_merge_plan(plan, store, pg, dry_run=dry_run)
    return {**result, "plan": plan}


def dedup_all(
    store: DeepStore,
    config: dict | None = None,
    dry_run: bool = False,
    pg: Any = None,
) -> dict:
    """两阶段去重: 标题精确匹配 + cosine 聚类 (PG 可用时)."""
    result = dedup_daily(store, config)

    try:
        if pg is None:
            from .pg_store import PgEmbedStore
            pg = PgEmbedStore()
        if pg.is_connected():
            cosine_result = dedup_with_llm(store, pg, dry_run=dry_run)
            result["cosine_removed"] = cosine_result.get("total_cards_moved", 0)
            result["cosine_clusters"] = cosine_result.get("merged_clusters", 0)
            result["scope"] = "title_exact + cosine_clustering"
        else:
            result["scope"] = "title_exact_only (PG unavailable, cosine skipped)"
    except Exception as e:
        logger.warning("cosine dedup skipped: %s", _safe_err(e)[:200])
        result["scope"] = "title_exact_only (cosine error: " + _safe_err(e)[:80] + ")"

    return result
