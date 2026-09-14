"""Topic-card candidate radar — pure vector computation, zero LLM, zero DB writes.

Responsibilities
----------------
Scan active topics in the PG `topics` table, pairwise compare their
embeddings, and return a candidate report:

  * ``duplicates``: pairs with cosine > dup_threshold — handed to E1
    LLM to decide whether to merge.
  * ``related``:    pairs with rel_threshold < cosine <= dup_threshold
    — handed to E1 to decide whether to link.

Strictly read-only / compute-only:
  * No LLM call. The radar finds candidates; E1 (and the LLM it owns)
    decides what to do with them.
  * No database writes. No commit / rollback is ever issued; the only
    statement is a single SELECT.

Connection shape
----------------
``pg`` may be:
  * ``None`` or falsy → return the empty report immediately.
  * An object exposing ``_connect()`` (e.g. ``e1._PgLeaseConnection``)
    — the radar calls ``_connect()`` to obtain the underlying DB
    connection, then uses that connection for the SELECT and closes it
    implicitly via the cursor context manager.
  * A raw psycopg2 connection (no ``_connect``) — used directly.

Usage
-----
    from v3core.topic_radar import radar_scan
    report = radar_scan(pg)   # -> {"duplicates": [...], "related": [...], ...}
"""
from __future__ import annotations

import logging

logger = logging.getLogger("v3core.topic_radar")

# Cosine thresholds — dup_threshold aligns with topic_cluster._simple_cluster
# (0.75 in the public tree). Below dup_threshold but above rel_threshold is
# the "related" band. Pairs below rel_threshold are silently dropped.
DUP_THRESHOLD = 0.75
REL_THRESHOLD = 0.60

# Output caps. dup_total / rel_total on the report reflect the pre-truncation
# counts; only the returned candidate lists are truncated.
MAX_DUPS = 50
MAX_RELS = 100

# Title field cap in candidate dicts — keep the report payload small.
TITLE_MAX_CHARS = 60


def _load_topic_embeddings(pg) -> list[dict]:
    """Load all active topics + their embeddings from PG with one SELECT.

    Returns a list of {"topic_id", "title", "embedding"} dicts. Returns an
    empty list when:
      * ``pg`` is None / falsy, or
      * the resolved connection is falsy, or
      * the SELECT raises (logged as a warning, never propagated), or
      * a row has a missing / unparsable / empty embedding text
        (such rows are skipped individually).

    The function never calls commit / rollback / any write.
    """
    rows: list[dict] = []
    if not pg:
        return rows
    try:
        conn = pg._connect() if hasattr(pg, "_connect") else pg
        if not conn:
            return rows
        with conn.cursor() as cur:
            cur.execute(
                "SELECT topic_id, title, embedding::text FROM topics "
                "WHERE status='active' AND embedding IS NOT NULL"
            )
            for tid, title, emb_text in cur.fetchall():
                if not emb_text:
                    continue
                try:
                    emb = [float(x) for x in emb_text.strip("[]").split(",") if x.strip()]
                except Exception:
                    continue
                if not emb:
                    continue
                rows.append({"topic_id": tid, "title": title or "", "embedding": emb})
    except Exception as e:
        logger.warning("radar: 读取 topics embedding 失败: %s", str(e)[:150])
    return rows


def radar_scan(pg=None, dup_threshold: float = DUP_THRESHOLD,
               rel_threshold: float = REL_THRESHOLD,
               max_dups: int = MAX_DUPS, max_rels: int = MAX_RELS) -> dict:
    """Scan topic embeddings and return duplicate / related candidate reports.

    Behaviour parity contract (see module docstring):
      * pure vector computation, no LLM, no DB writes
      * one SELECT only, no commit / rollback
      * empty report returned when fewer than 2 rows survive the loader,
        or when numpy is unavailable

    Intentional safety deviation from the historical behaviour:
        a row whose vector length differs from the first row's length is
        dropped before the matrix is built, with a single warning. The
        historical module let numpy raise ``ValueError`` on shape mismatch,
        which would crash the entire radar scan; dropping is strictly safer
        for malformed rows and is the only intentional behavioural change.
    """
    topics = _load_topic_embeddings(pg)
    n = len(topics)
    empty = {"duplicates": [], "related": [], "scanned": n, "dup_total": 0, "rel_total": 0}
    if n < 2:
        return empty

    try:
        import numpy as np
    except ImportError:
        logger.warning("radar: numpy 不可用, 返回空候选")
        return {"duplicates": [], "related": [], "scanned": n, "dup_total": 0, "rel_total": 0}

    # Drop rows whose vector length differs from the first row's length.
    # Historical behaviour let numpy raise ValueError on shape mismatch;
    # dropping is a strictly safer handling of malformed data and is the
    # only intentional behavioural deviation in this public restoration.
    first_len = len(topics[0]["embedding"])
    kept = [t for t in topics if len(t["embedding"]) == first_len]
    dropped = n - len(kept)
    if dropped:
        logger.warning("radar: drop %d rows whose vector length != %d", dropped, first_len)
    if len(kept) < 2:
        return {"duplicates": [], "related": [], "scanned": n, "dup_total": 0, "rel_total": 0}

    X = np.array([t["embedding"] for t in kept], dtype=np.float32)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    sim = X @ X.T / (norms @ norms.T + 1e-10)

    m = len(kept)
    dups: list[dict] = []
    rels: list[dict] = []
    for i in range(m):
        for j in range(i + 1, m):
            s = float(sim[i][j])
            if s > dup_threshold:
                dups.append({
                    "source_id": kept[i]["topic_id"],
                    "target_id": kept[j]["topic_id"],
                    "source_title": kept[i]["title"][:TITLE_MAX_CHARS],
                    "target_title": kept[j]["title"][:TITLE_MAX_CHARS],
                    "cosine": round(s, 4),
                })
            elif s > rel_threshold:
                # A pair above dup_threshold must never also appear here.
                rels.append({
                    "source_id": kept[i]["topic_id"],
                    "target_id": kept[j]["topic_id"],
                    "source_title": kept[i]["title"][:TITLE_MAX_CHARS],
                    "target_title": kept[j]["title"][:TITLE_MAX_CHARS],
                    "cosine": round(s, 4),
                })

    # Stable sort by cosine descending — duplicates first, then related.
    dups.sort(key=lambda x: -x["cosine"])
    rels.sort(key=lambda x: -x["cosine"])
    return {
        "duplicates": dups[:max_dups],
        "related": rels[:max_rels],
        "scanned": n,
        "dup_total": len(dups),
        "rel_total": len(rels),
    }
