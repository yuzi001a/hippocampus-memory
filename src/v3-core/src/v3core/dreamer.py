"""dreamer — 后台置信度验证器

Phase 2: 扫描低置信度 b 卡, 寻找后续对话中的交叉验证。
被验证 3+ 次的卡提升为 confirmed (observation_count++ → 触发 confidence boost)。

V2 (SQLite 版): 每张卡的 confidence/observation_count/last_verified_at 直接存储在
v3_cards.db 的行字段中, 不再使用 .meta.json sidecar 文件。

设计原则 (per "不演" 教训):
  - 纯 stdlib, 不引入新依赖
  - 失败不阻塞 pipeline: dreamer 异常只会 warning, 不影响 e1/recall
  - boost 阈值保守: 默认 observation_count >= 3 才升 0.7
"""

from __future__ import annotations

import json
import logging
import re
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

logger = logging.getLogger("v3core.dreamer")

# ── Constants ──────────────────────────────────────────────────────────────
CONFIDENCE_NEW = 0.3
CONFIDENCE_VERIFIED = 0.7
OBSERVATION_BOOST_THRESHOLD = 3
MAX_KEYWORDS_PER_CARD = 6

_STOPWORDS = set("""
的 了 是 在 我 你 他 她 它 们 这 那 有 和 与 或 也 都 就 但 而
及 以 其 之 于 乃 矣 焉 者 哉 吗 呢 吧 啊 哦 嗯 哈 唉
as the and or but if then else for to of in on at by with from
""".split())


# ── Helpers ────────────────────────────────────────────────────────────────

def _extract_keywords_from_text(title: str, content: str, max_kw: int = MAX_KEYWORDS_PER_CARD) -> list[str]:
    """从卡片标题+正文抽取关键词 (用于跨卡/跨会话匹配)

    策略: 取标题 + 正文前 500 字, 按非停用词的中文 2-gram + 英文 word 抽取。
    去重取前 max_kw 个。
    """
    sample = title.strip()
    body_preview = (content or "")[:500]
    if sample:
        sample += " " + body_preview
    else:
        sample = body_preview
    if not sample:
        return []

    candidates: list[str] = []
    # 中文 bigram
    chinese_chars = re.findall(r"[\u4e00-\u9fff]+", sample)
    for seg in chinese_chars:
        if len(seg) < 2:
            continue
        for i in range(len(seg) - 1):
            bg = seg[i:i+2]
            if bg not in _STOPWORDS:
                candidates.append(bg)
    # 英文/数字 word
    en_words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", sample)
    for w in en_words:
        if w.lower() not in _STOPWORDS:
            candidates.append(w)

    # 频次 + 顺序去重
    seen: dict[str, int] = {}
    for c in candidates:
        seen[c] = seen.get(c, 0) + 1
    top = sorted(seen.items(), key=lambda x: (-x[1], x[0]))[:max_kw]
    return [k for k, _ in top]


def _count_observations(keywords: list[str], recent_texts: list[str]) -> int:
    """数 recent_texts 中有几段包含至少一个 keyword."""
    if not keywords or not recent_texts:
        return 0
    pattern = re.compile("|".join(re.escape(k) for k in keywords))
    hits = 0
    for text in recent_texts:
        if pattern.search(text):
            hits += 1
    return hits


def _get_sqlite_store(base_path: Path) -> Any | None:
    """懒获取 SqliteCardStore (不引入循环依赖)"""
    try:
        from .sqlite_store import SqliteCardStore as _SCS
        return _SCS(base_path)
    except Exception as e:
        logger.warning("SqliteCardStore 创建失败: %s", _safe_err(e)[:100])
        return None


# ── Core API ───────────────────────────────────────────────────────────────

def verify_card_confidence(
    source_id: str,
    title: str,
    content: str,
    current_confidence: float = CONFIDENCE_NEW,
    current_observation_count: int = 1,
    recent_texts: list[str] | None = None,
    boost_threshold: int = OBSERVATION_BOOST_THRESHOLD,
) -> dict:
    """对单张卡做置信度验证 (不碰 I/O, 纯计算)

    Args:
        source_id: 卡片 ID
        title: 卡片标题
        content: 卡片正文
        current_confidence: 当前置信度
        current_observation_count: 当前观察次数
        recent_texts: 最近的对话/会话文本列表 (None = 跳过验证)
        boost_threshold: observation_count 达到此值才升 confidence

    Returns:
        更新后的 meta dict (含 confidence / observation_count / last_verified_at)
    """
    meta = {
        "confidence": current_confidence,
        "observation_count": current_observation_count,
        "last_verified_at": None,
        "source_id": source_id,
    }

    if not recent_texts:
        return meta

    keywords = _extract_keywords_from_text(title, content)
    if not keywords:
        return meta

    new_observations = _count_observations(keywords, recent_texts)
    if new_observations == 0:
        return meta

    meta["observation_count"] = meta["observation_count"] + new_observations
    meta["last_verified_at"] = datetime.now(timezone.utc).isoformat()

    if meta["observation_count"] >= boost_threshold and meta["confidence"] < CONFIDENCE_VERIFIED:
        meta["confidence"] = CONFIDENCE_VERIFIED
        logger.info("[dreamer] ⬆️  boost %s: count=%d → confidence=%.2f",
                    source_id, meta["observation_count"], CONFIDENCE_VERIFIED)

    return meta


def run_dreamer_pass(
    base_path: Path,
    recent_texts: list[str] | None = None,
    *,
    dry_run: bool = False,
    boost_threshold: int = OBSERVATION_BOOST_THRESHOLD,
    confidence_floor: float = CONFIDENCE_VERIFIED,
    sqlite_store: Any | None = None,
) -> dict:
    """扫描全卡库, 验证低 confidence 卡 — SQLite 版 (零文件扫描)

    Args:
        base_path: <base> 根目录 (含 v3_cards.db)
        recent_texts: 验证素材 (None = 跳过验证)
        dry_run: True = 只统计不改
        boost_threshold: observation_count 阈值
        confidence_floor: 只验证 confidence < 此值的卡
        sqlite_store: 可选已创建的 SqliteCardStore 实例

    Returns:
        报告 dict: {"scanned": N, "boosted": N, "verified": N, "skipped": N, "errors": [...]}
    """
    report = {
        "scanned": 0,
        "boosted": 0,
        "verified": 0,
        "skipped": 0,
        "errors": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "boost_threshold": boost_threshold,
        "confidence_floor": confidence_floor,
    }

    store = sqlite_store or _get_sqlite_store(base_path)
    if store is None:
        report["errors"].append(f"SqliteCardStore 不可用 (base={base_path})")
        return report

    try:
        conn = store.conn
        rows = conn.execute(
            "SELECT source_id, title, content, confidence, observation_count "
            "FROM cards WHERE confidence < ? AND archived_at IS NULL",
            (confidence_floor,)
        ).fetchall()
    except Exception as e:
        report["errors"].append(f"查询低置信度卡失败: {_safe_err(e)[:200]}")
        return report

    for row in rows:
        try:
            sid = row["source_id"]
            current_conf = float(row["confidence"])
            current_obs = int(row["observation_count"])
            report["scanned"] += 1

            if dry_run or not recent_texts:
                continue

            updated = verify_card_confidence(
                source_id=sid,
                title=row["title"] or "",
                content=row["content"] or "",
                current_confidence=current_conf,
                current_observation_count=current_obs,
                recent_texts=recent_texts,
                boost_threshold=boost_threshold,
            )

            if updated.get("observation_count", current_obs) > current_obs:
                report["verified"] += 1

            new_conf = float(updated.get("confidence", current_conf))
            new_obs = int(updated.get("observation_count", current_obs))
            last_verified = updated.get("last_verified_at")

            if new_conf > current_conf or new_obs > current_obs:
                conn.execute(
                    "UPDATE cards SET confidence=?, observation_count=?, "
                    "last_verified_at=?, updated_at=? WHERE source_id=?",
                    (new_conf, new_obs, last_verified,
                     datetime.now(timezone.utc).isoformat(), sid)
                )
                conn.commit()
                if new_conf > current_conf:
                    report["boosted"] += 1
                    logger.info("[dreamer] ⬆️  boost %s: count=%d → confidence=%.2f",
                                sid, new_obs, new_conf)

        except Exception as e:
            report["errors"].append(f"{sid}: {_safe_err(e)[:200]}")
            logger.warning("[dreamer] 处理失败 %s: %s", sid, _safe_err(e)[:200])

    logger.info(
        "[dreamer] pass done: scanned=%d boosted=%d verified=%d skipped=%d errors=%d (SQLite mode)",
        report["scanned"], report["boosted"], report["verified"],
        report["skipped"], len(report["errors"]),
    )
    return report


# ── CLI ────────────────────────────────────────────────────────────────────

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(description="dreamer — 卡置信度验证器 (SQLite 版)")
    p.add_argument("--base", type=Path,
                   default=Path.home() / ".v3-core" / "profiles" / "default",
                   help="v3-core 数据根目录 (含 v3_cards.db)")
    p.add_argument("--recent", type=Path, default=None,
                   help="包含近期对话文本的文件 (一行一段), None=跳过验证")
    p.add_argument("--dry-run", action="store_true", help="只统计不改")
    p.add_argument("--boost-threshold", type=int, default=OBSERVATION_BOOST_THRESHOLD,
                   help="observation_count 阈值")
    args = p.parse_args()

    recent_texts = None
    if args.recent and args.recent.exists():
        try:
            recent_texts = args.recent.read_text(encoding="utf-8").splitlines()
            recent_texts = [t for t in recent_texts if t.strip()]
        except Exception as e:
            print(f"[dreamer] 读 recent 失败: {_safe_err(e)}")
            return 1

    report = run_dreamer_pass(
        base_path=args.base,
        recent_texts=recent_texts,
        dry_run=args.dry_run,
        boost_threshold=args.boost_threshold,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
