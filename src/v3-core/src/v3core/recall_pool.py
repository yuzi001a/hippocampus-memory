"""recall_pool v2 - keyword + vector RRF + rerank + noise filter + facts fetch"""
from __future__ import annotations
import contextlib
import contextvars
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import logging
import math
import re
import threading
from datetime import datetime
from typing import Any, Optional
from .types import RecallHit
from ._deadline import (
    PrefetchDeadline,
    PrefetchDeadlineExceeded,
    DeadlinePoolView,
    bind_store_deadline,
    coerce_deadline,
)
from ._expand_query import _maybe_expand_query
from .config import _resolve_data_dir
from .pg_pool import PgPool


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


# 2026-09-04 P1.3.1 deadline-closure: Aho-Corasick multi-pattern string
# scan for the QA snapshot path.  pyahocorasick is declared as a runtime
# dependency (pyproject.toml) so the C extension is always available in
# production.  For legacy/test environments that lack the extension, the
# snapshot lookup keeps the nested-substring scan as a fail-safe
# fallback — note the fallback does NOT meet the long-query SLO
# (6.5/8s); the snapshot is only entered for the long-query path that
# must complete inside the budget.
_AHOCORASICK_AUTOMATON = None
_AHOCORASICK_IMPORT_ERROR = None


def _import_ahocorasick():
    """Lazy import of the Aho-Corasick Automaton class.

    Returns the ``Automaton`` class on success, or ``None`` on
    ``ImportError`` (so the caller can switch to the nested-substring
    fallback).  Other exceptions propagate so we never silently mask a
    real bug behind a "fallback".
    """
    global _AHOCORASICK_AUTOMATON, _AHOCORASICK_IMPORT_ERROR
    if _AHOCORASICK_AUTOMATON is not None:
        return _AHOCORASICK_AUTOMATON
    if _AHOCORASICK_IMPORT_ERROR is not None:
        return None
    try:
        # The PyPI distribution is ``pyahocorasick`` but the import name
        # is ``ahocorasick`` (the C extension module).
        import ahocorasick as _mod  # type: ignore
    except ImportError as e:
        _AHOCORASICK_IMPORT_ERROR = e
        return None
    cls = getattr(_mod, "Automaton", None)
    if cls is None:
        _AHOCORASICK_IMPORT_ERROR = ImportError("ahocorasick.Automaton missing")
        return None
    _AHOCORASICK_AUTOMATON = cls
    return cls

logger = logging.getLogger("v3core.recall_pool")


def _probe(trace, method, *args, **kwargs):
    """Additive, guarded probe that emits one legacy ``getattr`` hook call.

    When ``trace`` is ``None`` (the default), this is a no-op and the
    recall function's behaviour is byte-identical to before.  When
    ``trace`` is supplied, ``trace`` is duck-typed — recall_v2 is never
    imported here, so the dependency direction stays one-way.  Any
    exception is swallowed so a faulty sink can never break retrieval.
    """
    if trace is None:
        return
    try:
        fn = getattr(trace, method, None)
        if fn is not None:
            fn(*args, **kwargs)
    except Exception:
        return


def _pg_is_connected(pg, deadline=None) -> bool:
    """Health check that does not hide an active prefetch deadline.

    ``PgEmbedStore.is_connected`` is intentionally best-effort and catches
    all exceptions.  In a deadline-bound call that would turn a deadline
    signal into ``False`` before the actual recall SQL gets a chance to
    propagate it.  The guarded lease is the authoritative check in that
    mode; the legacy probe remains unchanged when no deadline is supplied.
    """
    bound = coerce_deadline(deadline)
    if bound is not None:
        bound.check(context="PG health gate")
        return pg is not None
    return bool(pg is not None and pg.is_connected())


def _is_real_pg_cursor(cursor: Any) -> bool:
    """Detect a DB-API PostgreSQL cursor without trusting a fake cursor."""
    return callable(getattr(cursor, "mogrify", None))

# RRF k 常数 (Reciprocal Rank Fusion, 行业标准 = 60)
K = 60

# Premise 注释解析 — E1 印层写入的 HTML 注释形如
#   <!-- premise: decisions/b_d1.md, lessons/b_l1.md -->
# 链式召回时先用 premise IDs 做精确匹配 (>=1.0 置信度), 再用向量相似度填补空位.
# Matches <!-- premise: id1.md, id2.md, ... --> or <!-- contradiction: text -->
_PREMISE_RE = re.compile(r"<!-- (?:premise|contradiction):\s*([^>]+)\s*-->")


@contextlib.contextmanager
def _lease_pg_connection(pg, timeout: float | None = 5, deadline: Optional[PrefetchDeadline] = None):
    """Borrow a PostgreSQL connection lease or legacy direct connection.

    - If pg is a pool-backed store-like object (pool is not None and lease() is callable),
      borrows via with pg.lease(timeout=timeout) as conn and releases on exit.
      When ``deadline`` is set, the underlying pool applies a
      ``statement_timeout`` derived from the deadline, and any
      ``QueryCanceled`` / statement timeout raised by SQL inside the
      block is translated into the canonical
      ``PrefetchDeadlineExceeded`` so callers can uniformly short-circuit.
    - If pg is a legacy store/direct connection (no pool), preserves existing
      _connect() / raw connection behavior without requiring lease().
    - If pg is an already pinned connection/handle, yields it without closing.
    - If pg is None, yields None.

    P1.2-A1: ``deadline`` is optional. ``deadline=None`` keeps the
    pre-A1 behavior exactly: no statement_timeout, no error
    translation, no extra checks.
    """
    # Defensive: a set deadline should never let us issue a SQL when
    # the budget is already exhausted. Caller layers above also check,
    # but this is the last guard before any SQL runs.
    deadline = coerce_deadline(deadline)
    if deadline is not None:
        deadline.check(context="recall lease")
        # Do not mutate the shared PgEmbedStore.  Its shallow deadline view
        # makes every existing store method acquire from a deadline-bound
        # pool while preserving the no-deadline path byte-for-byte.
        pg = bind_store_deadline(pg, deadline)

    if pg is None:
        yield None
        return

    pool = getattr(pg, "pool", None)
    lease_fn = getattr(pg, "lease", None)
    if (isinstance(pool, PgPool) or isinstance(pool, DeadlinePoolView)) and callable(lease_fn):
        # Pool path — the bound store view routes through PgLease.connection,
        # whose cursor facade refreshes statement_timeout before every SQL.
        with pg.lease(timeout=timeout) as conn:
            yield getattr(conn, "connection", conn)
        return

    if hasattr(pg, "_connect") and callable(getattr(pg, "_connect", None)):
        conn = None
        try:
            conn = pg._connect()
        except PrefetchDeadlineExceeded:
            raise
        except Exception:
            conn = None
        yield conn
        return

    if hasattr(pg, "cursor"):
        yield pg
        return

    yield None


def _extract_embed(cfg):
    """Build the validated embedding config, preserving disabled semantics.

    This compatibility wrapper is retained only for recall_pool's lazy import;
    it must never return a hand-built/raw dict.
    """
    from .embedding import safe_embed_cfg
    return safe_embed_cfg(cfg)


def _parse_premise_ids(content: str) -> list[str]:
    """Extract premise card IDs from HTML comments in yin / yin_segment text.

    Returns a list of source_id strings (e.g. ``["decisions/b_d1.md", ...]``).
    Empty list if no premise comment found. Only the FIRST premise comment is
    consumed — we assume one section maps to one premise batch (per e1.py layout).
    """
    if not content:
        return []
    m = _PREMISE_RE.search(content)
    if not m:
        return []
    raw = m.group(1).strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


def _chain_premise_lookup(pg, premise_ids: list[str], limit: int, *, deadline=None) -> list[dict]:
    """Exact-match lookup: pull topics rows by topic_id (ANY semantics).

    2026-08-06: 从 v3_cards 改查 topics (premise ID = 观察者 topic_id)
    Returns list of dicts compatible with ``_chain_search_by_embedding`` output
    (source_id / title / content_preview / cosine=1.0 / kind="topic" /
    matched_by="premise"). On any PG/lookup failure returns [].
    """
    if not pg or not premise_ids:
        return []
    if hasattr(pg, "is_connected") and not _pg_is_connected(pg, deadline):
        return []
    try:
        with _lease_pg_connection(pg) as conn:
            if not conn:
                return []
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT topic_id, COALESCE(title, ''), "
                    " COALESCE(summary, '') || COALESCE(body, '')"
                    " FROM topics WHERE topic_id = ANY(%s) AND status='active'"
                    " LIMIT %s",
                    (list(premise_ids), max(int(limit), 1)),
                )
                rows = cur.fetchall()
    except PrefetchDeadlineExceeded:
        raise
    except Exception as e:
        logger.debug("_chain_premise_lookup failed: %s", _safe_err(e)[:200])
        return []
    out: list[dict] = []
    for row in rows:
        out.append({
            "source_id": row[0],
            "title": row[1] or "",
            "content_preview": _trim(row[2] or "", 300),
            "content": row[2] or "",  # 2026-08-08: 全文通道, 不再截 500
            "cosine": 1.0,  # exact match = maximum confidence
            "kind": "topic",
            "matched_by": "premise",
        })
    return out


# Time decay half-life in days — 默认 30, 可被 recall_pool(config=...) 中的
# config.half_life 覆盖 (P3). 这个模块常量保留为兼容入口.
TIME_DECAY_HALF_LIFE = 30  # content older than 30 days gets ~63% weight

# CJK U+3400-U+4DBF, U+4E00-U+9FFF, U+A000-U+A97F
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\ua000-\ua97f]+")
_CJK_CHAR_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\ua000-\ua97f]")

# Noise filter (j/ message specific)
def _cjk_bigrams(text):
    if not text:
        return []
    out = []
    seen = set()
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            if run not in seen:
                seen.add(run)
                out.append(run)
            continue
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg in seen:
                continue
            seen.add(bg)
            out.append(bg)
    return out

def _keyword_score(card_id, meta, query_terms):
    """Keyword scoring using tokenizer (jieba or tri-gram fallback)"""
    from .tokenizer import score_keyword_on_text
    title = meta.get("title") or ""
    tags = meta.get("tags") or []
    content_preview = meta.get("content_preview") or ""
    score = score_keyword_on_text(title, tags, content_preview, query_terms)
    # bonus for source_id match
    if query_terms:
        for qt in query_terms:
            if qt in card_id.lower():
                score += 0.2
                break
    return min(score, 1.0)

def _cjk_bigram_score(meta, bigrams):
    if not bigrams:
        return 0.0
    title = meta.get("title") or ""
    raw_tags = meta.get("tags") or []
    tags_lower = []
    for t in raw_tags:
        if isinstance(t, str):
            tags_lower.append(t.lower())
    if not title and not tags_lower:
        return 0.0
    matched = 0
    for bg in bigrams:
        hit = False
        if bg and bg in title:
            hit = True
        else:
            for tl in tags_lower:
                if bg in tl:
                    hit = True
                    break
        if hit:
            matched += 1
    if matched == 0:
        return 0.0
    return min(0.15 * matched, 0.9)

def _rerank(query, hits, top_n=20, rerank_cfg=None, *, deadline=None):
    """Re-rank top-N hits via cross-encoder. Failures degrade silently."""
    if not hits or not rerank_cfg or not rerank_cfg.get("endpoint"):
        return hits
    deadline = coerce_deadline(deadline)
    if deadline is not None:
        deadline.check(context="rerank")
    # 2026-09-02 P1.3.1 resilience: 受控 RRF 回退 (controlled budget skip).
    # 仅当 deadline 已 set, 剩余预算 > 0, 但小于 rerank_cfg['min_remaining_s'] 时,
    # 跳过本次 remote rerank 并直接返回入参 hits — 不改分数 / 顺序, 不抛 PDE, 不发起 HTTP.
    # deadline=None / 已耗尽 / 剩余充足 的路径语义不变 (前者照常调 remote,
    # 后者由上面的 deadline.check 抛 PDE). 缺省 1.5s 与外部 8.0s join timeout
    # 之间的间隙一致. 这是 rerank skip, 不是 PASS.
    if deadline is not None:
        try:
            _min_remaining = float(rerank_cfg.get("min_remaining_s", 1.5))
        except (TypeError, ValueError):
            _min_remaining = 1.5
        if _min_remaining <= 0:
            _min_remaining = 1.5
        _remaining = deadline.remaining()
        if 0 < _remaining < _min_remaining:
            logger.info(
                "rerank skip (budget): remaining=%.3fs < min_remaining_s=%.3fs, returning RRF order",
                _remaining, _min_remaining,
            )
            return hits
    cfg = dict(rerank_cfg or {})
    if deadline is not None:
        try:
            configured_timeout = float(cfg.get("timeout", 30))
        except (TypeError, ValueError):
            configured_timeout = 30.0
        cfg["timeout"] = max(0.001, min(configured_timeout, deadline.remaining()))
    candidates = hits[:top_n] if top_n else hits
    if not candidates:
        return hits
    try:
        from .rerank import rerank as _rerank_api
        # 2026-08-08: rerank 输入用全文 (content 优先), 不再截 200 字 —
        # rerank 只看 200 字会导致长文档相关性误判 (主体被忽略)
        documents = [(h.content or h.content_preview or h.title) for h in candidates]
        # rerank API 单文档长度限制保护 (一般 512-2048 token), 超长截到 2000 字符
        documents = [d[:2000] for d in documents]
        scores = _rerank_api(query, documents, cfg)
        if deadline is not None:
            deadline.check(context="rerank result")
        if scores is None:
            return hits
        if len(scores) != len(candidates):
            logger.warning("rerank returned %d scores for %d candidates, fallback to RRF", len(scores), len(candidates))
            return hits
        for h, s in zip(candidates, scores):
            try:
                h.cosine = float(s)
            except (TypeError, ValueError):
                pass
        reranked = sorted(candidates, key=lambda h: -h.cosine)
        if top_n and len(hits) > top_n:
            reranked.extend(hits[top_n:])
        return reranked
    except PrefetchDeadlineExceeded:
        raise
    except Exception as e:
        if deadline is not None and deadline.is_exceeded():
            raise PrefetchDeadlineExceeded(
                "prefetch internal deadline exceeded during rerank",
                deadline=deadline,
                context="rerank",
            ) from e
        logger.warning("rerank failed, fallback to RRF order: %s", _safe_err(e)[:200])
        return hits

def _fetch_facts(source_ids, pg, include_superseded=False):
    """Batch-fetch topic entries for card hits from topic_entries."""
    if not source_ids or pg is None:
        return {}
    result = {}
    try:
        with _lease_pg_connection(pg) as conn:
            if not conn:
                return {}
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT topic_id, id, question, answer, timestamp "
                    "FROM topic_entries WHERE topic_id = ANY(%s) "
                    "ORDER BY topic_id, timestamp DESC NULLS LAST",
                    (source_ids,),
                )
                for row in cur.fetchall():
                    sid = row[0]
                    question = row[2] or ""
                    answer = row[3] or ""
                    fact_text = f"Q: {question}\nA: {answer}" if answer else question
                    result.setdefault(sid, []).append({
                        "id": row[1],
                        "fact_text": fact_text,
                        "confidence": 1.0,
                        "created_at": row[4].isoformat() if hasattr(row[4], "isoformat") else row[4],
                    })
    except PrefetchDeadlineExceeded:
        raise
    except Exception as e:
        logger.warning("_fetch_facts: query failed: %s", _safe_err(e)[:200])
        return {}
    return result


def _add_active_memory_to_hits(rows, *, hits: dict, target_ids: set, with_cosine: bool):
    """Convert ActiveMemoryReader rows into RecallHit(kind='active_memory') and
    append them to the existing ``hits`` dict + ``target_ids`` set.

    Reuses the existing RecallHit shape and respects the existing RRF
    machinery: kw_ids → kw_rank (KW_RRF_WEIGHT=0.5); vec_ids → vec_rank
    (VEC_RRF_WEIGHT=1.0).  No new lane, no new constant.

    ``rows`` may be empty.  Any row missing a usable ``source_id`` is
    silently dropped (preserves the existing recall semantics — IDs come
    from the canonical memory_id, which is always set on reader output).
    """
    if not rows:
        return
    for row in rows:
        try:
            mid = row.get("memory_id") or row.get("source_id")
        except (AttributeError, TypeError):
            mid = None
        if not isinstance(mid, str) or not mid:
            continue
        sid = mid  # canonical source_id == memory_id for explicit_memories
        if sid in hits:
            # Already collected by another path — keep the higher cosine if any,
            # otherwise leave the existing hit untouched (preserves precedence).
            try:
                rc = float(row.get("cosine") or 0.0)
            except (TypeError, ValueError):
                rc = 0.0
            if rc > hits[sid].cosine:
                hits[sid].cosine = rc
            target_ids.add(sid)
            continue
        try:
            title = row.get("title") or ""
        except (AttributeError, TypeError):
            title = ""
        try:
            content = row.get("content") or ""
        except (AttributeError, TypeError):
            content = ""
        try:
            preview = row.get("content_preview") or (content[:500] if content else "")
        except (AttributeError, TypeError):
            preview = content[:500] if isinstance(content, str) else ""
        try:
            created_at = row.get("created_at") or ""
        except (AttributeError, TypeError):
            created_at = ""
        try:
            tags = list(row.get("tags") or [])
        except (AttributeError, TypeError):
            tags = []
        try:
            category = row.get("category") or "active_memory"
        except (AttributeError, TypeError):
            category = "active_memory"
        try:
            cosine = float(row.get("cosine") or 0.0)
        except (TypeError, ValueError):
            cosine = 0.0
        hits[sid] = RecallHit(
            source_id=sid,
            title=title,
            content_preview=preview,
            content=content,
            category=category,
            tags=tags,
            cosine=cosine if with_cosine else 0.0,
            rrf_score=0.0,
            kind="active_memory",
            created_at=created_at,
        )
        target_ids.add(sid)


def _active_memory_reader_for(pg, deadline) -> Any:
    """Build an ActiveMemoryReader bound to the caller's existing pg + deadline.

    No independent pool/connection: the reader reuses the injected ``pg`` (and
    the same deadline the caller already bound).  Any ImportError or surface
    failure to construct the reader surfaces as ``None`` so callers degrade
    silently — preserving existing recall behavior when the active-memory
    module is absent or the table is unavailable.
    """
    if pg is None:
        return None
    try:
        from .active_memory_store import ActiveMemoryReader
    except Exception as exc:  # pragma: no cover - module presence test
        logger.debug("active_memory_store unavailable: %s", _safe_err(exc)[:200])
        return None
    try:
        return ActiveMemoryReader(pg=pg, deadline=deadline)
    except Exception as exc:
        logger.debug("ActiveMemoryReader init failed: %s", _safe_err(exc)[:200])
        return None




def _bind_topic_recall_deadline(topic_recall, deadline):
    """Bind a temporary pool view without mutating the Runtime cache."""
    bound = coerce_deadline(deadline)
    if bound is None or topic_recall is None:
        return topic_recall
    pool = getattr(topic_recall, "_pool", None)
    if pool is None:
        return topic_recall
    if isinstance(pool, DeadlinePoolView) and pool.deadline is bound:
        return topic_recall
    if isinstance(pool, DeadlinePoolView):
        pool = pool._pool
    view = copy.copy(topic_recall)
    view._pool = DeadlinePoolView(pool, bound)
    return view


def _resolve_topic_recall(core, embed_cfg, pg, deadline=None):
    """Reuse Runtime cache, then legacy core._topic_recall, else a temporary instance.

    Fake/legacy cores without a real TopicRecallCache keep the F4 `_topic_recall`
    path. core=None still constructs a local TopicRecall.
    """
    if core is not None:
        cache = getattr(core, "topic_recall_cache", None)
        try:
            from .topic_recall_cache import TopicRecallCache as _CacheCls
        except Exception:
            _CacheCls = tuple()
        if isinstance(cache, _CacheCls):
            getter = getattr(cache, "get_topic_recall", None)
            if callable(getter):
                tr = getter()
                if tr is not None:
                    return _bind_topic_recall_deadline(tr, deadline)
        tr = getattr(core, "_topic_recall", None)
        if tr is not None:
            tr = _bind_topic_recall_deadline(tr, deadline)
            if not getattr(tr, "_topics", None):
                ensure = getattr(tr, "_ensure_loaded", None)
                if callable(ensure):
                    ensure()
            return tr
    from .topic_recall import TopicRecall
    pool = getattr(pg, "pool", None) if pg is not None else None
    return TopicRecall(embed_cfg, pool=pool) if pool is not None else TopicRecall(embed_cfg)

# === 2026-09-02 P1.3.1: long-term QA snapshot helper ==============
# Designed for the measured long-query shape (47-term full-provider cold path).
# The snapshot is a *performance hint*, not a correctness boundary: it never
# expands recall beyond what the SQL path would return, and it is invalidated
# by any change in (COUNT(*), MAX(id)) on qa_pairs.

def _resolve_qa_snapshot_min_terms(config) -> int:
    """Return the configured qa_snapshot_min_terms threshold; 0 = disabled.

    Tolerant to V3Config / dict / None / missing field. Non-positive values
    disable the snapshot path so callers fall back to the combined/parallel
    SQL path unchanged (preserves A2 worker slot / topic / rerank wiring).
    Default 5 — 5-term boundary / term_frequency tail: 实证默认从 7 下调至 5,
    以在 5435 snapshot 上更早触发 snapshot 路径. integrated 200-case eval
    唯一 5-term locomo:0011 冷跑仍有 ~6502ms 拖尾; 进程内 threshold=5 前 12
    eval=24 calls、max 4072ms、0 deadline、counts unchanged. 显式 1–4 仍走
    snapshot (语义等同于「低于默认」), 0 = 禁用, 显式 32/47 等仅作对照.
    """
    default = 5
    if config is None:
        return default
    try:
        if isinstance(config, dict):
            pf = config.get("prefetch") or {}
            raw_value = pf.get("qa_snapshot_min_terms", default)
        else:
            pf = getattr(config, "prefetch", None)
            raw_value = getattr(pf, "qa_snapshot_min_terms", default) if pf is not None else default
    except (TypeError, AttributeError):
        return default
    # Absent / None / empty-string -> safe default (preserves prior contract).
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return default
    # Explicit numeric/string "0" disables the snapshot.
    try:
        if isinstance(raw_value, bool):
            # bool is a subclass of int — treat True/False as malformed, not as 0/1.
            return default
        if isinstance(raw_value, str):
            value = int(raw_value.strip())
        else:
            value = int(raw_value)
    except (TypeError, ValueError):
        return default
    # Negative handling: existing safe default/warning contract (silent fallback).
    if value < 0:
        return default
    return value


def _qa_snapshot_load(
    pg,
    pool,
    cursor,
    version,
    config,
    *,
    deadline=None,
) -> dict | None:
    """Build a pool-owner-scoped, versioned QA snapshot.

    Returns a cache dict with rows pre-lowercased once per row. Returns
    None on any failure (DB error, empty pool, custom store without
    writable attrs) so the caller can transparently fall back to the
    combined/parallel SQL path. Version mismatch invalidates the cache
    and triggers a fresh load under the same lock.

    ``deadline`` (2026-09-04 P1.3.1 deadline-closure): optional
    ``PrefetchDeadline`` (or absolute monotonic float) that bounds the
    per-row prep loop.  The function checks the bound every
    ``_QA_SNAPSHOT_PREP_CHECK_EVERY`` rows and raises
    ``PrefetchDeadlineExceeded`` if the budget is exhausted, so a
    long prep does not silently overrun the 6.5/8s SLO.  ``PrefetchDeadlineExceeded``
    is propagated unchanged — never swallowed into ``None``.
    """
    cache = _qa_snapshot_cache_for_pool(pool, version)
    if cache is None:
        return None
    bound = coerce_deadline(deadline)
    lock = cache.get("lock") or threading.RLock()
    cache["lock"] = lock
    with lock:
        if cache.get("version") == version and cache.get("rows") is not None:
            return cache
        rows = []
        try:
            cur = cursor
            # Use the existing real-cursor path so deadline / statement_timeout
            # / connection ownership semantics are preserved exactly.
            if bound is not None:
                bound.check(context="qa_snapshot_load SELECT")
            cur.execute(
                "SELECT id, timestamp, question, answer, session_id FROM qa_pairs"
            )
            raw_rows = list(cur.fetchall())
        except PrefetchDeadlineExceeded:
            raise
        except Exception as e:
            logger.debug("qa_snapshot: load failed (%s); falling back", _safe_err(e)[:120])
            cache["version"] = version  # mark attempt; caller may invalidate via version bump
            cache["rows"] = None
            return None
        # Precompute lowercase once per row.
        # 2026-09-02 P1.3.1 fix: preserve SQL ``answer IS NOT NULL`` semantics —
        # store the original (possibly None) answer separately from the lowercase
        # text used for matching, so non-NULL empty-string answers are not
        # accidentally treated as NULL by the eligibility check below.
        # 2026-09-04 P1.3.1 deadline-closure: low-frequency deadline check
        # inside the prep loop so a long CPU-bound lower() pass cannot run
        # past the budget.  PrefetchDeadlineExceeded propagates; we never
        # mask the signal into a partial cache.
        prepped = []
        _check_every = _QA_SNAPSHOT_PREP_CHECK_EVERY
        for _i, row in enumerate(raw_rows):
            if bound is not None and _check_every and (_i % _check_every == 0):
                bound.check(context="qa_snapshot_load prep")
            rid = row[0]
            q = row[2] if row[2] is not None else ""
            a_raw = row[3]  # preserve None vs "" distinction for answer IS NOT NULL
            a_text = a_raw if a_raw is not None else ""
            s = row[4] if row[4] is not None else ""
            prepped.append((rid, row[1], q, a_raw, s, q.lower(), a_text.lower()))
        # Atomic visibility: assign rows first, then version last.
        cache["rows"] = prepped
        cache["version"] = version
        return cache


# 2026-09-04 P1.3.1 deadline-closure: low-frequency deadline probe inside
# the per-row prep loop.  128 is a balance between overhead and SLO
# granularity; the loop itself is O(N) string ops so checking every
# 128 rows is far cheaper than a single Python-level syscall.
_QA_SNAPSHOT_PREP_CHECK_EVERY = 128
# Same probe granularity for the per-row scan inside _qa_snapshot_lookup.
_QA_SNAPSHOT_SCAN_CHECK_EVERY = 64

# 2026-09-04 P1.3.1 — algorithm-dispatch threshold for the QA snapshot
# scan inside ``_qa_snapshot_lookup``.
#
# Below this term count the Aho-Corasick path is skipped and the
# preserved nested-substring path runs instead; above (or equal to) this
# count the Aho path is the runtime path of record.
#
# Evidence (100-query canonical comparison, P1.3.1 deadline-closure):
#   * 5–21-term "snapshot" queries were ~179ms/case slower with Aho —
#     the per-call Automaton construction + make_automaton cost
#     dominates at small term sets, and the nested scan is already
#     cache-friendly for those shapes.
#   * Real production shapes with 154 / 275 / 517 terms (event-shape
#     expansions) benefit measurably from the single-pass Aho scan.
# 32 is a conservative dispatch boundary placed between the two observed
# regimes; it does not claim an exact benchmarked crossover point and it
# never touches the qa_snapshot_min_terms=5 eligibility floor, the term set,
# candidate limits, lanes, or semantics.
#
# This is an algorithm dispatch threshold ONLY.  It does NOT change
# ``qa_snapshot_min_terms`` (still 5 = below which the snapshot is
# disabled entirely) or any other public surface.  The nested path
# remains a correctness-preserving fallback for short queries and for
# environments where pyahocorasick cannot be imported.
_QA_SNAPSHOT_AUTOMATON_MIN_TERMS = 32


def _qa_snapshot_cache_for_pool(pool, version):
    """Return a pool-owner-scoped, versioned snapshot cache slot.

    Mirrors _qa_keyword_cache_for_pool: stores on the underlying pool object
    (transparently unwrapping DeadlinePoolView) so the cache is shared across
    long-query recall calls but isolated between different pool owners.
    Returns None if the pool is missing, unwrappable, or lacks writable
    instance attributes (legacy / fake stores).
    """
    if pool is None or version is None:
        return None
    owner = pool
    while isinstance(owner, DeadlinePoolView):
        owner = getattr(owner, "_pool", None)
    if owner is None:
        return None
    try:
        cache = getattr(owner, "_prefetch_qa_snapshot_cache", None)
        if not isinstance(cache, dict):
            cache = {"version": None, "rows": None, "lock": threading.RLock()}
            setattr(owner, "_prefetch_qa_snapshot_cache", cache)
        if cache.get("lock") is None:
            cache["lock"] = threading.RLock()
        if cache.get("version") != version:
            # Invalidate. Do not pre-write rows; the load function fills atomically.
            cache["version"] = version
            cache["rows"] = None
        return cache
    except Exception:
        return None


def _qa_snapshot_lookup_nested(
    snapshot,
    terms,
    *,
    rare_limit: int,
    per_term_limit: int,
    freq_limit: int,
    bound=None,
) -> tuple[dict, list, dict]:
    """Preserved nested-substring scan.

    Returns ``(frequencies, eligible_per_term, per_row_hits)``:

      * ``frequencies`` — {lower_term: count} for every lower term.
      * ``eligible_per_term`` — list[list[rid]] parallel to ``terms``.
      * ``per_row_hits`` — {rid: [term_idx, ...]} for every row that
        substring-hit any term (eligibility-agnostic).  Order is
        ``terms`` order.  This is the metadata the recall_pool scoring
        block reuses to skip the second per-candidate scan.

    ``bound`` (P1.3.1 deadline-closure) is an optional
    ``PrefetchDeadline``; when supplied, the function checks it every
    ``_QA_SNAPSHOT_SCAN_CHECK_EVERY`` rows and raises
    ``PrefetchDeadlineExceeded`` if the budget is exhausted.
    """
    rows = snapshot.get("rows") or []
    term_lower = [str(term).lower() for term in terms]
    frequencies = {tl: 0 for tl in term_lower}
    eligible_per_term = [[] for _ in terms]
    per_row_hits: dict = {}
    for _i, (rid, _ts, _q, _a, sess, q_low, a_low) in enumerate(rows):
        if bound is not None and _QA_SNAPSHOT_SCAN_CHECK_EVERY and (
            _i % _QA_SNAPSHOT_SCAN_CHECK_EVERY == 0
        ):
            bound.check(context="qa_snapshot_lookup scan")
        row_hits: list[int] = []
        for idx, needle in enumerate(term_lower):
            if not needle:
                continue
            if (needle in q_low) or (needle in a_low):
                frequencies[needle] += 1
                row_hits.append(idx)
                # 2026-09-02 P1.3.1 fix: SQL ``answer IS NOT NULL`` semantics —
                # eligibility requires the original answer value to be non-NULL,
                # not merely truthy (an empty-string answer is still eligible).
                if (
                    _a is not None
                    and sess is not None
                    and (".trajectory" not in sess)
                ):
                    eligible_per_term[idx].append(rid)
        if row_hits:
            per_row_hits[rid] = row_hits
    return frequencies, eligible_per_term, per_row_hits


def _qa_snapshot_lookup_automaton(
    snapshot,
    terms,
    *,
    rare_limit: int,
    per_term_limit: int,
    freq_limit: int,
    bound=None,
    matched_terms_out=None,
) -> tuple[dict, list, dict] | None:
    """Aho-Corasick based scan; returns ``None`` on any Automaton
    failure so the caller can fall back to the nested scan.

    The Automaton stores one entry per *unique lower term* because
    pyahocorasick overwrites duplicate keys.  Each entry keeps a
    representative term index plus an expansion map to every duplicate
    index, so a match preserves the original per-index semantics without
    changing the searchable key.

    The per-row scan iterates the snapshot rows once and scans q_low and
    a_low separately, avoiding false matches across the question/answer
    boundary.  It records both frequency and per-row term indices.

    ``bound`` (P1.3.1 deadline-closure) is checked every
    ``_QA_SNAPSHOT_SCAN_CHECK_EVERY`` rows; on exhaustion the function
    raises ``PrefetchDeadlineExceeded`` (it does NOT silently return).
    """
    Automaton = _import_ahocorasick()
    if Automaton is None:
        return None
    rows = snapshot.get("rows") or []
    term_lower = [str(term).lower() for term in terms]
    # Build the Automaton with one entry per *unique* lower term.  We
    # cannot add one Automaton entry per (lower, idx) because the
    # duplicate-key behaviour of pyahocorasick.add_word is "last wins"
    # (the earlier test confirmed this); encoding the index into the
    # key bytes (e.g. "{lower}\x00{idx}") would make the needle never
    # match real text.  Instead, we add each unique lower term once
    # with a representative index, then expand any hit to *all* term
    # indices that share the same lower form.
    auto = Automaton()
    try:
        # unique_lower -> representative index in terms
        unique_to_indices: dict[str, list[int]] = {}
        for idx, tl in enumerate(term_lower):
            if not tl:
                continue
            unique_to_indices.setdefault(tl, []).append(idx)
        for tl, indices in unique_to_indices.items():
            # Use the first index as the Automaton value; the per-row
            # expansion below turns one match into one entry per
            # shared-index term, preserving duplicate semantics.
            auto.add_word(tl, indices[0])
        try:
            auto.make_automaton()
        except Exception:
            return None
        # Expansion map: representative index -> full list of indices
        # sharing the same lower form (in terms order).
        expand: dict[int, list[int]] = {
            indices[0]: list(indices) for indices in unique_to_indices.values()
        }
        frequencies = {tl: 0 for tl in term_lower}
        eligible_per_term = [[] for _ in terms]
        per_row_hits: dict = {}
        for _i, (rid, _ts, _q, _a, sess, q_low, a_low) in enumerate(rows):
            if bound is not None and _QA_SNAPSHOT_SCAN_CHECK_EVERY and (
                _i % _QA_SNAPSHOT_SCAN_CHECK_EVERY == 0
            ):
                bound.check(context="qa_snapshot_lookup automaton scan")
            # Concat would risk cross-boundary hits (e.g. a needle whose
            # first half appears at the end of q_low and second half at
            # the start of a_low), which the SQL/nested reference would
            # not match.  Scan q_low and a_low separately and union.
            seen: set[int] = set()
            try:
                for _end, idx in auto.iter(q_low):
                    if idx in seen:
                        continue
                    seen.add(idx)
                for _end, idx in auto.iter(a_low):
                    if idx in seen:
                        continue
                    seen.add(idx)
            except Exception:
                # Per-row Automaton failure: fall back to nested scan for
                # this row only.  Safer than aborting the whole lookup
                # when a single weird input breaks the C extension.
                _needle_hits = []
                for _idx, _tl in enumerate(term_lower):
                    if _tl and (_tl in q_low or _tl in a_low):
                        _needle_hits.append(_idx)
                if not _needle_hits:
                    continue
                seen = set(_needle_hits)
            if not seen:
                continue
            # Expand each representative index to all term indices that
            # share the same lower form.  This preserves the
            # duplicate-index semantics that a naive add_word would
            # silently overwrite (see the unit test for evidence).
            expanded_indices: set[int] = set()
            for _idx in seen:
                expanded_indices.update(expand.get(_idx, [_idx]))
            for idx in expanded_indices:
                frequencies[term_lower[idx]] += 1
            if (
                _a is not None
                and sess is not None
                and (".trajectory" not in sess)
            ):
                for idx in expanded_indices:
                    eligible_per_term[idx].append(rid)
            # Record the per-row hit list in original terms order
            # (matches the nested reference exactly so QA scoring can
            # consume it without an extra reorder).
            per_row_hits[rid] = sorted(expanded_indices)
        if matched_terms_out is not None:
            # Caller wants a mutable dict view: populate it in place so
            # the same object the recall_pool code created is mutated.
            matched_terms_out.clear()
            matched_terms_out.update(per_row_hits)
        return frequencies, eligible_per_term, per_row_hits
    except PrefetchDeadlineExceeded:
        raise
    except Exception:
        return None


def _qa_snapshot_lookup(
    snapshot,
    terms,
    *,
    rare_limit: int,
    per_term_limit: int,
    freq_limit: int,
    matched_terms_out=None,
    deadline=None,
) -> tuple[dict, list]:
    """Match ``terms`` against the QA snapshot with the same semantics as the SQL helper.

    Frequency counts every row matching question OR answer (substring,
    case-insensitive). Selected rows for ranking require answer IS NOT NULL
    and session_id NOT LIKE '%.trajectory%', ordered by id ASC, and capped
    by the existing rare / common dynamic limits.

    2026-09-04 P1.3.1 deadline-closure — two new optional kwargs:

      * ``matched_terms_out``: mutable dict that, when supplied, is
        populated in place with ``{rid: [term_idx, ...]}`` for every
        row that substring-hit any term (eligibility-agnostic), in
        ``terms`` order.  The recall_pool code reuses this to skip the
        second per-candidate ``term in q+a`` scan in the QA scoring
        block.  Default ``None`` keeps the legacy signature.

      * ``deadline``: optional ``PrefetchDeadline`` (or absolute
        monotonic float).  Probed at low frequency inside the scan;
        exhaustion raises ``PrefetchDeadlineExceeded`` unchanged.

    2026-09-04 — runtime path is now Aho-Corasick (pyahocorasick is a
    declared runtime dependency).  The nested substring scan is kept
    as a fail-safe fallback for environments where the C extension
    cannot be imported; the fallback does NOT meet the long-query SLO
    so it is only used in tests/legacy hosts.
    """
    if not terms:
        if matched_terms_out is not None:
            matched_terms_out.clear()
        return {}, []
    rows = snapshot.get("rows") or []
    if not rows:
        if matched_terms_out is not None:
            matched_terms_out.clear()
        return (
            {str(term).lower(): 0 for term in terms},
            [[] for _ in terms],
        )
    term_lower = [str(term).lower() for term in terms]
    bound = coerce_deadline(deadline)
    # Pre-check the budget so callers that pass a clearly-expired
    # deadline get the signal immediately rather than after a long scan.
    if bound is not None:
        bound.check(context="qa_snapshot_lookup entry")

    # Try the Aho-Corasick path first.  Any ImportError / Automaton
    # construction failure falls back to the nested scan.  We never
    # swallow PrefetchDeadlineExceeded — that signal is the whole point
    # of the deadline.
    #
    # 2026-09-04 P1.3.1 — adaptive dispatch by term count (see
    # ``_QA_SNAPSHOT_AUTOMATON_MIN_TERMS``).  Short queries (below the
    # threshold) take the nested path directly; long queries
    # (>= threshold) go through Aho.  When the Aho call returns None
    # for any other reason (import failure, build failure, per-row
    # exception) the nested fallback still runs.
    auto_result: tuple | None = None
    if len(term_lower) >= _QA_SNAPSHOT_AUTOMATON_MIN_TERMS:
        auto_result = _qa_snapshot_lookup_automaton(
            snapshot, terms,
            rare_limit=rare_limit,
            per_term_limit=per_term_limit,
            freq_limit=freq_limit,
            bound=bound,
            matched_terms_out=matched_terms_out,
        )
    if auto_result is not None:
        frequencies, eligible_per_term, _ = auto_result
    else:
        # Fail-safe fallback.  Also the default path for short queries
        # (below ``_QA_SNAPSHOT_AUTOMATON_MIN_TERMS``) where Aho's
        # per-call construction cost dominates.  Does not meet
        # long-query SLO; the dependency is required at runtime
        # precisely to avoid this on the long-query path.
        frequencies, eligible_per_term, per_row_hits = _qa_snapshot_lookup_nested(
            snapshot, terms,
            rare_limit=rare_limit,
            per_term_limit=per_term_limit,
            freq_limit=freq_limit,
            bound=bound,
        )
        if matched_terms_out is not None:
            matched_terms_out.clear()
            matched_terms_out.update(per_row_hits)

    by_id = {row[0]: row for row in rows}
    rows_by_term = []
    for idx, ids in enumerate(eligible_per_term):
        ids_sorted = sorted(ids)  # id ASC, mirror ORDER BY id ASC in SQL
        freq = frequencies[term_lower[idx]]
        if freq <= 3:
            cap = max(int(rare_limit), 1)
        else:
            if freq:
                cap = max(int(per_term_limit), min(freq, int(freq_limit)))
            else:
                cap = max(int(per_term_limit), 1)
        rows_by_term.append(
            [
                (
                    by_id[rid][0],
                    by_id[rid][1],
                    by_id[rid][2],
                    by_id[rid][3],
                )
                for rid in ids_sorted[:cap]
            ]
        )
    return frequencies, rows_by_term


# === 2026-09-02 P1.3.1: long-term topic-keyword snapshot helper ===
# TopicRecall vector cache cannot serve the keyword path because it lacks
# the ``keywords`` text column and the active-status filter.  This helper
# builds a pool-owner-scoped snapshot of active ``topics`` rows sufficient
# to reproduce the existing ``OR-ILIKE + keywords`` semantics in Python.
# It mirrors the QA snapshot contract: a *performance hint*, not a
# correctness boundary, versioned by ``(COUNT(*), MAX(id), MAX(updated_at))``
# on active topics, guarded by a per-pool lock, and falling back to None on
# any failure so the existing combined/parallel SQL path runs unchanged.

def _topic_snapshot_cache_for_pool(pool, version):
    """Return a pool-owner-scoped, versioned topic-snapshot cache slot.

    Mirrors :func:`_qa_snapshot_cache_for_pool`: stores on the underlying
    pool object (transparently unwrapping ``DeadlinePoolView``) so the
    cache is shared across long-query recall calls but isolated between
    different pool owners.  Returns None if the pool is missing,
    unwrappable, or lacks writable instance attributes (legacy / fake
    stores).
    """
    if pool is None or version is None:
        return None
    owner = pool
    while isinstance(owner, DeadlinePoolView):
        owner = getattr(owner, "_pool", None)
    if owner is None:
        return None
    try:
        cache = getattr(owner, "_prefetch_topic_snapshot_cache", None)
        if not isinstance(cache, dict):
            cache = {"version": None, "rows": None, "lock": threading.RLock()}
            setattr(owner, "_prefetch_topic_snapshot_cache", cache)
        if cache.get("lock") is None:
            cache["lock"] = threading.RLock()
        if cache.get("version") != version:
            # Invalidate. Do not pre-write rows; the load function fills atomically.
            cache["version"] = version
            cache["rows"] = None
        return cache
    except Exception:
        return None


def _topic_snapshot_load(pg, pool, cursor, version, config=None):
    """Build a pool-owner-scoped, versioned active-topics snapshot.

    The snapshot reads ``topic_id, title, summary, body, keywords`` for
    every row with ``status='active'`` via the existing real-cursor lease
    so ``statement_timeout`` / connection ownership semantics are
    preserved exactly.  Lowercase forms of ``title``, ``summary`` and
    ``body`` are precomputed once per row; lowercase keyword strings are
    precomputed once per keyword.  No partial cache visibility: the
    caller observes either a fully-populated cache or ``None`` (which
    triggers the existing SQL fallback).
    """
    cache = _topic_snapshot_cache_for_pool(pool, version)
    if cache is None:
        return None
    lock = cache.get("lock") or threading.RLock()
    cache["lock"] = lock
    with lock:
        if cache.get("version") == version and cache.get("rows") is not None:
            return cache
        try:
            cur = cursor
            # SELECT only the columns needed for exact keyword semantics;
            # matches the existing ILIKE OR with EXISTS/unnest(keywords) path
            # field-for-field so Python matching is byte-equivalent.
            cur.execute(
                "SELECT topic_id, title, summary, body, keywords FROM topics "
                "WHERE status='active'"
            )
            raw_rows = list(cur.fetchall())
        except PrefetchDeadlineExceeded:
            raise
        except Exception as e:
            logger.debug(
                "topic_snapshot: load failed (%s); falling back",
                _safe_err(e)[:120],
            )
            cache["version"] = version
            cache["rows"] = None
            return None
        prepped = []
        for row in raw_rows:
            tid = row[0]
            title = row[1] if row[1] is not None else ""
            summary = row[2] if row[2] is not None else ""
            body = row[3] if row[3] is not None else ""
            # keywords may be None (older rows) or a Python list/array
            # depending on the driver adapter; normalize to list[str].
            raw_kw = row[4]
            if raw_kw is None:
                kw_list: list[str] = []
            elif isinstance(raw_kw, (list, tuple)):
                kw_list = [str(k) for k in raw_kw if k is not None]
            else:
                # psycopg2 typically returns a list; fall back to single string.
                kw_list = [str(raw_kw)]
            prepped.append((
                tid,
                title,
                summary,
                body,
                title.lower(),
                summary.lower(),
                body.lower(),
                [k.lower() for k in kw_list],
            ))
        # Atomic visibility: assign rows first, then version last.
        cache["rows"] = prepped
        cache["version"] = version
        return cache


def _topic_snapshot_lookup(snapshot, terms, *, limit):
    """Match ``terms`` against the topic snapshot with the SQL-path semantics.

    Each term is matched case-insensitively as a substring in
    ``title``, ``summary``, ``body`` or any ``keywords`` entry.  Two
    outputs:

    * ``main`` — first ``limit`` rows that match ANY term, in stable
      source (snapshot) order — replicates the existing
      ``OR (title ILIKE %s OR body ILIKE %s OR summary ILIKE %s OR
      EXISTS(unnest(keywords) kw WHERE kw ILIKE %s)) LIMIT %s`` shape.
    * ``rare`` — per-term list of the first ``limit`` rows for terms
      with ``len(term) > 3``, de-duplicated in original term order —
      replicates the existing rare-batch helper.

    Returns ``(main, rare)`` where ``main`` is a flat list of tuples
    ``(topic_id, title, summary, body)`` and ``rare`` is a list of
    such tuples per qualifying term (in original ``terms`` order).
    """
    cap = max(int(limit), 1)
    if not terms:
        return [], []
    rows = snapshot.get("rows") if snapshot is not None else None
    if not rows:
        return [], [[] for _ in terms]
    term_lower = [str(t).lower() for t in terms]
    # rare_term_set is the set of indices where len(original term) > 3.
    # The original Python ``len(t) > 3`` uses the raw term length, not the
    # lowered form, mirroring the existing rare-batch helper exactly.
    rare_indices = [i for i, t in enumerate(terms) if len(t) > 3]

    def _row_matches(row, needle: str) -> bool:
        # row tuple layout: (tid, title, summary, body,
        #                   title_low, summary_low, body_low, kw_low_list)
        return (
            needle in row[4]
            or needle in row[5]
            or needle in row[6]
            or any(needle in k for k in row[7])
        )

    main_rows: list[tuple] = []
    main_seen: set = set()
    for row in rows:
        for needle in term_lower:
            if not needle:
                continue
            if _row_matches(row, needle):
                tid = row[0]
                if tid in main_seen:
                    break
                main_seen.add(tid)
                main_rows.append((tid, row[1], row[2], row[3]))
                break  # one term is enough for the OR — match next row
        if len(main_rows) >= cap:
            break

    rare_rows: list[list[tuple]] = [[] for _ in terms]
    for idx in rare_indices:
        needle = term_lower[idx]
        if not needle:
            continue
        seen_for_term: set = set()
        for row in rows:
            if not _row_matches(row, needle):
                continue
            tid = row[0]
            if tid in seen_for_term:
                continue
            seen_for_term.add(tid)
            rare_rows[idx].append((tid, row[1], row[2], row[3]))
            if len(rare_rows[idx]) >= cap:
                break
    return main_rows, rare_rows


def _combined_qa_keyword_lookup(
    cursor,
    terms: list[str],
    *,
    rare_limit: int,
    per_term_limit: int,
    freq_limit: int,
) -> tuple[dict[str, int], list[list[tuple]]]:
    """Return exact term frequencies and per-term QA rows in one SQL round-trip.

    The query deliberately keeps two different semantics separate:

    * ``stats`` counts every ``qa_pairs`` row matching ``question OR answer``;
      this is the old rare/dynamic-limit input and does not apply the candidate
      filters.
    * ``ranked`` applies the old ``answer IS NOT NULL`` and trajectory filters,
      then keeps ``id ASC`` rows for the dynamic per-term limit.

    ``term_list`` is built only from tokenizer output.  Its values are still
    bound parameters; the generated SQL contains no user text or bind values.
    A LEFT JOIN emits one row even when a term has no eligible candidate, so a
    zero-frequency/zero-candidate term cannot disappear from the map.
    """
    if not terms:
        return {}, []

    values_sql = ",".join("(%s, %s)" for _ in terms)
    sql = f"""
        WITH term_list(ord, term) AS (VALUES {values_sql}),
        matches AS MATERIALIZED (
            SELECT t.ord, q.id, q.timestamp, q.session_id,
                   (q.answer IS NOT NULL) AS answer_present
            FROM term_list AS t
            CROSS JOIN LATERAL (
                SELECT id, timestamp, answer, session_id
                FROM qa_pairs
                WHERE question ILIKE ('%%' || t.term || '%%')
                   OR answer ILIKE ('%%' || t.term || '%%')
            ) AS q
        ),
        stats AS (
            SELECT ord, COUNT(*) AS freq
            FROM matches
            GROUP BY ord
        ),
        ranked AS (
            SELECT m.ord, m.id, m.timestamp,
                   s.freq,
                   row_number() OVER (PARTITION BY m.ord ORDER BY m.id ASC) AS rn
            FROM matches AS m
            JOIN stats AS s ON s.ord = m.ord
            WHERE m.answer_present
              AND m.session_id NOT LIKE '%%.trajectory%%'
        ),
        limited AS (
            SELECT ord, id, timestamp
            FROM ranked
            WHERE rn <= CASE
                    WHEN freq <= 3 THEN %s
                    ELSE GREATEST(%s, LEAST(freq, %s))
                 END
        )
        SELECT t.ord, COALESCE(s.freq, 0) AS freq,
               r.id, r.timestamp, q.question, q.answer
        FROM term_list AS t
        LEFT JOIN stats AS s ON s.ord = t.ord
        LEFT JOIN limited AS r ON r.ord = t.ord
        LEFT JOIN qa_pairs AS q ON q.id = r.id
        ORDER BY t.ord, r.id
    """
    params: list[Any] = []
    for index, term in enumerate(terms):
        params.extend((index, term))
    params.extend((int(rare_limit), int(per_term_limit), int(freq_limit)))
    cursor.execute(sql, params)

    frequencies = {str(term).lower(): 0 for term in terms}
    rows_by_term: list[list[tuple]] = [[] for _ in terms]
    for row in cursor.fetchall():
        index = int(row[0])
        if index < 0 or index >= len(terms):
            raise RuntimeError("combined QA lookup returned an invalid term ordinal")
        frequencies[str(terms[index]).lower()] = int(row[1] or 0)
        if row[2] is not None:
            rows_by_term[index].append((row[2], row[3], row[4], row[5]))
    return frequencies, rows_by_term


def _combined_topic_rare_keyword_lookup(cursor, terms: list[str], limit: int) -> list[tuple]:
    """Return the old per-term topic rare rows in one ordered SQL call."""
    if not terms:
        return []
    values_sql = ",".join("(%s, %s)" for _ in terms)
    sql = f"""
        WITH term_list(ord, term) AS (VALUES {values_sql})
        SELECT t.ord, r.topic_id, r.title, r.summary, r.body
        FROM term_list AS t
        CROSS JOIN LATERAL (
            SELECT topic_id, title, summary, body
            FROM topics
            WHERE status='active'
              AND (
                  title ILIKE ('%%' || t.term || '%%')
                  OR body ILIKE ('%%' || t.term || '%%')
                  OR summary ILIKE ('%%' || t.term || '%%')
                  OR EXISTS (
                      SELECT 1 FROM unnest(keywords) kw
                      WHERE kw ILIKE ('%%' || t.term || '%%')
                  )
              )
            LIMIT %s
        ) AS r
        ORDER BY t.ord
    """
    params: list[Any] = []
    for index, term in enumerate(terms):
        params.extend((index, term))
    params.append(max(int(limit), 1))
    cursor.execute(sql, params)
    return [tuple(row[1:]) for row in cursor.fetchall()]


def _combined_qa_keyword_lookup_parallel(
    pg,
    terms: list[str],
    *,
    workers: int = 2,
    rare_limit: int,
    per_term_limit: int,
    freq_limit: int,
) -> tuple[dict[str, int], list[list[tuple]]]:
    """Run independent term chunks on independent pool leases.

    This is deliberately reserved for the measured long-query shape.  Each
    chunk is independent, and the caller must prove that the pool has one
    spare connection beyond the worker set; callers invoke it before their
    main keyword lease is acquired.
    """
    worker_count = max(2, min(int(workers), len(terms)))
    chunk_count = worker_count + 1 if worker_count >= 3 and len(terms) >= 4 else worker_count
    chunks = tuple(
        terms[index * len(terms) // chunk_count : (index + 1) * len(terms) // chunk_count]
        for index in range(chunk_count)
    )

    def run_chunk(chunk: list[str]):
        with _lease_pg_connection(pg) as conn:
            if conn is None:
                raise RuntimeError("parallel QA lookup could not acquire a connection")
            with conn.cursor() as cur:
                return _combined_qa_keyword_lookup(
                    cur,
                    chunk,
                    rare_limit=rare_limit,
                    per_term_limit=per_term_limit,
                    freq_limit=freq_limit,
                )

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="v3-qa") as executor:
        futures = [
            executor.submit(contextvars.copy_context().run, run_chunk, chunk)
            for chunk in chunks
        ]
        parts = [future.result() for future in futures]

    frequencies: dict[str, int] = {}
    rows_by_term: list[list[tuple]] = []
    for chunk_result in parts:
        chunk_frequencies, chunk_rows = chunk_result
        frequencies.update(chunk_frequencies)
        rows_by_term.extend(chunk_rows)
    return frequencies, rows_by_term


def _qa_parallel_allowed(pool: Any, terms: list[str]) -> bool:
    """Allow three QA workers only when one pool slot remains for a writer."""
    if pool is None or len(terms) < 8:
        return False
    owner = pool
    while isinstance(owner, DeadlinePoolView):
        owner = getattr(owner, "_pool", None)
    return int(getattr(owner, "_max_connections", 0) or 0) >= 4


def _qa_prefill_parallel_cache(
    pg,
    pool: Any,
    terms: list[str],
    *,
    rerank_top_n: int,
    limit: int,
    config: Any,
    current_outer: bool = False,
) -> bool:
    """Prefill long-query QA terms before the main keyword lease is held.

    P1.3 + A2 admission (contract on return value):
        * True  — prefill completed OR cache hit / legacy no-op path did
                  not consume any unreserved workers.  Caller may safely
                  ask the main keyword lookup to run with ``parallel=…``
                  only if its other gates still permit it.
        * False — version / pool reservation / prefill failure path.
                  Caller MUST force the main lookup to ``parallel=False``
                  so the unguarded parallel worker race cannot start
                  without an explicit reservation.
        * PrefetchDeadlineExceeded still raises (never swallowed).

    P1.3 + A2 follow-up — ``current_outer`` (keyword-only, default False):
        When True, the caller (V3Core recall seam) has already admitted
        an outer-reader token; passing it through prevents the worker
        reservation from double-counting that outer slot.  Legacy/fake
        pools without ``current_outer`` continue to work via signature
    inspection in the dispatch site.
    """
    if not _qa_parallel_allowed(pool, terms):
        return False
    per_term_limit = max(rerank_top_n or 0, limit * 2) // max(len(terms), 1)
    per_term_min = int((config.get("recall") or {}).get("qa_per_term_min", 10))
    per_term_limit = max(per_term_limit, per_term_min)
    rare_limit = max(rerank_top_n or 0, limit * 3)
    freq_limit = max(rerank_top_n or 0, limit * 8)
    if isinstance(config, dict):
        freq_limit = int((config.get("recall") or {}).get("qa_freq_limit", freq_limit) or freq_limit)

    version = None
    try:
        with _lease_pg_connection(pg) as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute("SELECT count(*), COALESCE(max(id), 0) FROM qa_pairs")
                row = cur.fetchone() or ()
                if len(row) > 1:
                    version = (int(row[0] or 0), int(row[1] or 0))
    except PrefetchDeadlineExceeded:
        raise
    except Exception:
        return False
    if version is None:
        return False

    cache = _qa_keyword_cache_for_pool(
        pool,
        version,
        (int(rare_limit), int(per_term_limit), int(freq_limit)),
    )
    # ── A2 admission gate ──────────────────────────────────────────
    # Only reserve worker slots when there is real work to do (i.e. cache
    # miss or no cache).  A fully-cached query MUST NOT trigger a worker
    # reservation; doing so would falsely reject otherwise valid queries.
    # We probe the cache under its own lock to detect missing terms; if
    # nothing is missing we skip reservation entirely.
    will_start_workers = True
    if cache is not None:
        cache_lock = cache.get("lock")
        if cache_lock is None:
            cache_lock = threading.RLock()
            cache["lock"] = cache_lock
        with cache_lock:
            cached_terms = cache.get("terms") or {}
            seen_keys: set[str] = set()
            missing = False
            for term in terms:
                key = str(term).lower()
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                if key not in cached_terms:
                    missing = True
                    break
            will_start_workers = missing

    if will_start_workers:
        # Resolve the real PgPool (DeadlinePoolView.__getattr__ forwards to
        # underlying pool).  We must reserve against the SAME pool that
        # physical leases are drawn from so the writer reserve invariant
        # holds across outer reader + internal worker + Observer.
        owner = pool
        while isinstance(owner, DeadlinePoolView):
            owner = getattr(owner, "_pool", None)
        reservation = None
        if owner is not None and hasattr(owner, "try_reserve_workers"):
            # Real owner callsite: the V3Core recall seam has already
            # admitted an outer-reader token before invoking this helper,
            # so the worker reservation must NOT double-count that outer
            # token.  ``current_outer=True`` tells PgPool to treat
            # ``_outer_readers - 1`` as the "other outer readers" count.
            # Use signature inspection so legacy/fake owners without this
            # kwarg (incl. unit tests using mocks / stub pools) keep
            # working — we only forward when supported.
            import inspect as _inspect
            try:
                _sig = _inspect.signature(owner.try_reserve_workers)
                _supports_current_outer = (
                    "current_outer" in _sig.parameters
                    or any(
                        p.kind is _inspect.Parameter.VAR_KEYWORD
                        for p in _sig.parameters.values()
                    )
                )
            except (TypeError, ValueError):
                _supports_current_outer = False
            if _supports_current_outer:
                reservation = owner.try_reserve_workers(
                    desired=3, minimum=2, current_outer=current_outer
                )
            else:
                reservation = owner.try_reserve_workers(desired=3, minimum=2)
            if reservation.granted < 2:
                # Hard reject: cannot run the parallel helper without the
                # minimum worker set, and the A2 contract forbids falling
                # back to an unguarded reader race.  Tell the caller to
                # force the main lookup onto its serial path so it never
                # starts an unreserved parallel race.
                return False
        try:
            workers = reservation.granted if reservation is not None else 3
            if cache is not None:
                _qa_keyword_cache_lookup(
                    None,
                    terms,
                    cache,
                    pg=pg,
                    parallel=True,
                    parallel_workers=workers,
                    rare_limit=rare_limit,
                    per_term_limit=per_term_limit,
                    freq_limit=freq_limit,
                )
            else:
                _combined_qa_keyword_lookup_parallel(
                    pg,
                    terms,
                    workers=workers,
                    rare_limit=rare_limit,
                    per_term_limit=per_term_limit,
                    freq_limit=freq_limit,
                )
        except PrefetchDeadlineExceeded:
            raise
        except Exception:
            return False
        finally:
            if reservation is not None:
                reservation.token.close()
        return True
    else:
        # Pure cache hit — still invoke the cache lookup to register the
        # terms, but in serial mode (no parallel workers).
        try:
            _qa_keyword_cache_lookup(
                None,
                terms,
                cache,
                pg=pg,
                parallel=False,
                parallel_workers=3,
                rare_limit=rare_limit,
                per_term_limit=per_term_limit,
                freq_limit=freq_limit,
            )
        except PrefetchDeadlineExceeded:
            raise
        except Exception:
            return False
        return True


def _qa_keyword_cache_for_pool(
    pool: Any,
    version: tuple[int, int] | None,
    params: tuple[int, int, int],
) -> dict[str, Any] | None:
    """Return a pool-scoped read cache when a stable QA version is available.

    The current write path appends QA rows and only repairs embeddings in
    place.  ``(COUNT(*), MAX(id))`` therefore changes for every supported
    content insert/delete while embedding-only repairs keep the text stable.
    A cache is refused when the version cannot be read, so legacy/fake stores
    retain their exact old behavior rather than guessing freshness.
    """
    if pool is None or version is None:
        return None
    owner = pool
    while isinstance(owner, DeadlinePoolView):
        owner = getattr(owner, "_pool", None)
    if owner is None:
        return None
    try:
        cache = getattr(owner, "_prefetch_qa_keyword_cache", None)
        if not isinstance(cache, dict):
            cache = {"version": None, "params": None, "terms": {}, "lock": threading.RLock()}
            setattr(owner, "_prefetch_qa_keyword_cache", cache)
        if cache.get("version") != version or cache.get("params") != params:
            cache["version"] = version
            cache["params"] = params
            cache["terms"] = {}
        terms = cache.get("terms")
        if not isinstance(terms, dict):
            cache["terms"] = {}
        if cache.get("lock") is None:
            cache["lock"] = threading.RLock()
        return cache
    except Exception:
        # A custom pool without writable instance attributes is not cacheable;
        # this is a performance hint, never a reason to change recall output.
        return None


def _qa_keyword_cache_lookup(
    cursor,
    terms: list[str],
    cache: dict[str, Any],
    *,
    pg=None,
    parallel: bool = False,
    parallel_workers: int = 2,
    rare_limit: int,
    per_term_limit: int,
    freq_limit: int,
) -> tuple[dict[str, int], list[list[tuple]]]:
    """Read/fill a pool-scoped QA cache without exposing partial entries."""
    lock = cache.get("lock")
    if lock is None:
        lock = threading.RLock()
        cache["lock"] = lock
    with lock:
        cached_terms = cache.get("terms")
        if not isinstance(cached_terms, dict):
            cached_terms = {}
            cache["terms"] = cached_terms

        missing_terms: list[str] = []
        missing_keys: set[str] = set()
        for term in terms:
            key = str(term).lower()
            if key not in cached_terms and key not in missing_keys:
                missing_keys.add(key)
                missing_terms.append(term)

        if missing_terms:
            if parallel and pg is not None:
                missing_freq, missing_rows = _combined_qa_keyword_lookup_parallel(
                    pg,
                    missing_terms,
                    workers=parallel_workers,
                    rare_limit=rare_limit,
                    per_term_limit=per_term_limit,
                    freq_limit=freq_limit,
                )
            else:
                missing_freq, missing_rows = _combined_qa_keyword_lookup(
                    cursor,
                    missing_terms,
                    rare_limit=rare_limit,
                    per_term_limit=per_term_limit,
                    freq_limit=freq_limit,
                )
            new_entries = {}
            for index, term in enumerate(missing_terms):
                key = str(term).lower()
                new_entries[key] = (
                    int(missing_freq.get(key, 0)),
                    missing_rows[index],
                )
            cached_terms.update(new_entries)

        frequencies = {
            str(term).lower(): int(cached_terms[str(term).lower()][0])
            for term in terms
        }
        rows = [cached_terms[str(term).lower()][1] for term in terms]
        return frequencies, rows


def recall_pool(
    query,
    card_index=None,
    pg=None,
    q_emb=None,
    include_keyword=True,
    # 2026-08-06: 观察者 v2 后旧表(v3_cards 7395 张历史卡 / v3_messages / v3_effective 155 条死数据)
    # 不再参与召回 — topics(689) 是唯一检索索引。旧表开关默认关, 防污染/稀释。
    include_card_vector=False,
    include_message_vector=False,
    include_effective=False,
    include_topic=True,
    include_yin=True,
    include_notes=True,
    rerank_top_n=10,
    rerank_cfg=None,
    limit=10,
    pg_was_connected=False,
    config=None,
    sqlite_store=None,
    core=None,
    *,
    deadline: Optional[PrefetchDeadline] = None,
    outer_admitted: bool = False,
    trace=None,
):
    """Multi-path recall + RRF + optional rerank + facts enrichment.

    Args:
        ...
        config: 可选 V3Config / dict, 用于读取 ``half_life``.
        sqlite_store: 可选 SqliteCardStore 实例, PG 离线时作为 keyword 搜索兜底.
        core: 可选 V3Core 实例 — 提供时复用其守护线程预热的 ``_topic_recall``
              缓存 (60s 一次原子替换), 避免每次召回都重新加载全部 topics.
              为 None 时行为与之前完全一致 (回退到本函数内临时构造 TopicRecall).
        deadline (P1.2-A1): 可选 — 整个 recall_pool 的内部 deadline.
              设置后, glossary / keyword / QA vector / yin / notes 路径在每次
              SQL 前调用 deadline.check()，lease 也会获得一个
              statement_timeout, PG 端 QueryCanceled 会被翻译成
              PrefetchDeadlineExceeded。
              deadline=None 旧行为完全不变 (新参数全部 optional, 不影响老调用).
        outer_admitted (P1.3 + A2 follow-up, keyword-only, default False):
              可选 — 调用方 (V3Core.prefetch / V3Core.prefetch_to_context_block)
              是否已经在自身 try/finally 中成功取得 outer-reader token.
              True 时, _qa_prefill_parallel_cache 的 worker 预留会以
              ``current_outer=True`` 计算, 不会把本次 outer 重复扣掉.
              False (默认) 时维持旧 direct / legacy 语义 — 不凭空多算一个
              outer reader. 不传时, 旧调用方 (含测试 fake) 行为零变化.
        trace (G6B Slice B, keyword-only, default None):
              可选 — 一个鸭子类型 ``LegacySink`` 兼容对象.  为 ``None`` 时
              recall_pool 行为与之前字节级一致 (no-op probes, 零额外开销).
              非 ``None`` 时 recall_pool 在以下阶段通过 ``_probe`` 发出
              ``getattr`` 探针:

              * 5 个 lane 的 ``lane_start`` / ``lane_finish`` (或
                ``lane_finish(timed_out=True, reason='deadline')`` 当
                ``PrefetchDeadlineExceeded`` 在该 lane 内重抛) —
                ``keyword`` / ``qa`` / ``explicit`` / ``vector`` /
                ``topic``;
              * ``lane_candidates(lane, source_ids)`` 在每个 lane 收集结束
                时给出该 lane 的 source_id 列表;
              * fusion (RRF) 阶段: 每条 ``scored`` hit 发出一次
                ``score(source_id, 'fusion', rrf_score, operation='rrf')``;
              * temporal 阶段: 当某 hit 真实应用了 half-life 衰减时, 发出
                ``score(source_id, 'temporal', rrf_score,
                operation='half_life_decay', decay=, age_days=)``;
              * rare-bonus 阶段: 当某 hit 加上 0.08 封顶的稀有词加分时,
                发出 ``event(source_id, 'SCORED', 'rare_bonus')``;
              * exact-QA-boost 阶段: 当某 hit 加上 0.5 保送分时, 发出
                ``event(source_id, 'SCORED', 'exact_qa_boost')``;
              * rerank 阶段: 实际跑 ``_rerank`` 时, 对 ``ranked`` 中每条 hit
                发出 ``event(source_id, 'RERANKED')``; 不跑 rerank 时发出
                ``warn('rerank skipped: <reason>')``, reason 严格从
                ``rerank_cfg`` / ``rerank_top_n`` / 长度比较三个真实条件
                之一派生 ('no_rerank_cfg' / 'rerank_top_n_disabled' /
                'len_le_rerank_top_n');
              * selection 阶段: ``final`` 里的 hit 发出
                ``select(source_id)``; ``ranked[limit:]`` 里的 hit 发出
                ``drop(source_id, 'OUTSIDE_LIMIT', stage='selection')``;
              * lane degradation: 每个 lane 的 ``except Exception as e``
                日志分支前, 发出 ``error('lane=NAME: ' + str(e)[:200])``;
              * lane timed-out: 每个 lane 的 ``except PrefetchDeadlineExceeded``
                重抛之前, 发出
                ``lane_finish(NAME, timed_out=True, reason='deadline')``;
              * pg_fail 警告: 当 recall 后 PG 失效时, 发出
                ``warn('pg_fail=True')``.

              recall_v2 模块**永不**在此模块导入 — 依赖方向保持单向
              (recall_v2 -> recall_pool).

    Returns:
        (hits, pg_fail) where pg_fail=True means PG was configured but disconnected.
    """
    # P3: 从 config 中读 half_life (覆盖模块常量)
    half_life = _resolve_half_life(config, default=TIME_DECAY_HALF_LIFE)
    if not query or not query.strip():
        return [], False

    # 短查询扩展（glossary / M3）
    deadline = coerce_deadline(deadline)
    if deadline is not None:
        deadline.check(context="recall_pool")
        # Bind a shallow view, never mutate the Runtime-owned store.  All
        # existing PgEmbedStore read methods then borrow guarded leases.
        pg = bind_store_deadline(pg, deadline)
    _pg_pool = getattr(pg, "pool", None) if pg is not None else None
    if not isinstance(_pg_pool, (PgPool, DeadlinePoolView)):
        _pg_pool = None
    _use_combined_qa = _pg_pool is not None
    if deadline is None:
        expanded = (
            _maybe_expand_query(query, pool=_pg_pool)
            if _pg_pool is not None
            else _maybe_expand_query(query)
        )
    else:
        expanded = (
            _maybe_expand_query(query, pool=_pg_pool, deadline=deadline)
            if _pg_pool is not None
            else _maybe_expand_query(query, deadline=deadline)
        )
    if expanded != query:
        logger.info("recall_pool: query expanded: '%s' -> '%s'",
                     query[:30], expanded[:60])
        query = expanded

    from .tokenizer import build_query_tokens
    query_terms = build_query_tokens(query)
    if not query_terms:
        query_terms = [t.lower() for t in query.strip().split() if len(t) > 1]
    if not query_terms and query.strip():
        query_terms = [query.strip().lower()]

    cjk_bigrams = _cjk_bigrams(query)

    hits = {}
    kw_ids = set()
    vec_ids = set()
    msg_ids = set()
    eff_ids = set()
    topic_ids = set()
    yin_ids = set()
    notes_ids = set()
    _total_qa = 0  # 2026-08-12 A-1: keyword 分支填充, 保送阈值归一化用 (未执行时保送走绝对下限 3)
    # 2026-08-17: include_keyword=False 时下面 keyword 分支不会执行, 但 line 664 `if _rare_bonus:`
    # 和 line 678 `if _qa_boost_sig:` 仍引用这两个名字 — 在函数级先初始化为 {} 避免 UnboundLocalError,
    # 不改任何排序语义 (空字典触发 false 分支, 等价于 keyword 路径未命中)。
    _rare_bonus = {}
    _qa_boost_sig = {}

    # Keyword path — PG topics 表（优先）或 SQLite topic_blocks 兜底
    # Keyword + QA lanes (single combined block — both seams share the
    # `kw_ids` set + the kind discriminant on each hit).
    _probe(trace, 'lane_start', 'keyword')
    _probe(trace, 'lane_start', 'qa')
    # Explicit lane (active-memory): the legacy code reuses kw_ids/vec_ids
    # so these rows still flow through the existing RRF machinery; the probe
    # only records that the explicit reader actually returned rows.
    _probe(trace, 'lane_start', 'explicit')
    # G6B Slice D (D2): always initialize the explicit-lane id-capture lists
    # so the lane_finish probe below can read them regardless of which path
    # ran (include_keyword and/or include_card_vector).
    _am_kw_ids_for_probe: list[str] = []
    _am_vec_ids_for_probe: list[str] = []
    _am_kw_reader_seen = False  # reader resolver ran for kw seam
    _am_vec_reader_seen = False  # reader resolver ran for vec seam
    _am_kw_reader_present = False  # kw reader resolved to non-None
    _am_vec_reader_present = False  # vec reader resolved to non-None
    if include_keyword:
        if deadline is not None:
            deadline.check(context="keyword path")
        try:
            # 2026-09-09 P2a: active-memory keyword retrieval at the candidate-
            # collection seam. Reuses the same `pg` + bound deadline, adds hits
            # to the existing kw_ids set so the existing KW_RRF_WEIGHT=0.5 path
            # picks them up unchanged. No new RRF lane, no new connection.
            try:
                if deadline is not None:
                    deadline.check(context="active memory keyword path")
                _am_reader = _active_memory_reader_for(pg, deadline)
                _am_kw_reader_seen = True
                _am_kw_reader_present = _am_reader is not None
                if _am_reader is not None:
                    try:
                        _am_kw_rows = _am_reader.search_keyword(
                            query, limit=max(rerank_top_n or 0, limit * 3)
                        )
                    except PrefetchDeadlineExceeded:
                        # A8 (G6B Slice B): record the lane timeout
                        # BEFORE the raise.
                        try:
                            _probe(trace, 'lane_finish', 'explicit', timed_out=True, reason='deadline')
                        except Exception:
                            pass
                        raise
                    except Exception as _am_kw_exc:
                        # A7 (G6B Slice B): record the degradation in the
                        # trace BEFORE the existing logger call.
                        try:
                            _probe(trace, 'error', 'lane=explicit: ' + str(_am_kw_exc)[:200])
                        except Exception:
                            pass
                        logger.debug(
                            "active-memory keyword search failed: %s",
                            _safe_err(_am_kw_exc)[:200],
                        )
                        _am_kw_rows = []
                    # Snapshot the explicit ids BEFORE adding to hits so the
                    # probe can record the *real* active-memory provenance
                    # even after _add_active_memory_to_hits mutates the dict.
                    for _row in (_am_kw_rows or []):
                        try:
                            _mid = _row.get("memory_id") or _row.get("source_id")
                        except Exception:
                            _mid = None
                        if isinstance(_mid, str) and _mid:
                            _am_kw_ids_for_probe.append(_mid)
                    _add_active_memory_to_hits(
                        _am_kw_rows, hits=hits, target_ids=kw_ids, with_cosine=False
                    )
            except PrefetchDeadlineExceeded:
                # A8 (G6B Slice B): record the lane timeout BEFORE the raise.
                try:
                    _probe(trace, 'lane_finish', 'explicit', timed_out=True, reason='deadline')
                except Exception:
                    pass
                raise
            except Exception as _am_kw_outer:
                # A7 (G6B Slice B): record the degradation in the trace
                # BEFORE the existing logger call.
                try:
                    _probe(trace, 'error', 'lane=explicit: ' + str(_am_kw_outer)[:200])
                except Exception:
                    pass
                logger.debug(
                    "active-memory keyword seam failed: %s",
                    _safe_err(_am_kw_outer)[:200],
                )
                _am_kw_ids_for_probe = []
            _kw_topics = []
            _qa_hits_raw = []  # 2026-08-09: QA 原文命中 (PG 分支填充, 兜底空)
            _qa_boost_sig: dict[str, tuple] = {}  # 2026-08-12 A-1: (min_freq, 低频词数) 保送信号
            _term_freq = {}  # 2026-08-09: query 词频 (稀有词加权)
            _rare_bonus = {}  # 2026-08-09: 稀有词命中 QA 的排序提升分 (source_id -> bonus, 0.08 封顶)
            _terms = [t for t in query_terms if len(t) > 1] if query_terms else []
            if not _terms and query.strip():
                _terms = [query.strip().lower()]

            _pg_connected = bool(_terms and _pg_is_connected(pg, deadline))
            # 2026-09-02 P1.3.1: snapshot serves the long-query path itself,
            # so do not reserve three QA workers for _qa_prefill_parallel_cache
            # when snapshot eligibility is met (preserves the A2 writer slot).
            _qa_snapshot_min_terms = _resolve_qa_snapshot_min_terms(config)
            _snapshot_eligible = (
                _pg_connected
                and _use_combined_qa
                and _qa_snapshot_min_terms > 0
                and len(_terms) >= _qa_snapshot_min_terms
            )
            # P1.3 + A2: track whether the prefill succeeded so the main
            # keyword cache lookup NEVER spins up an unreserved parallel
            # race when the prefill was rejected (granted<2) or failed.
            # Snapshot-served long queries must not reserve workers — the
            # _snapshot_eligible short-circuit above skips this whole branch.
            qa_prefill_parallel_ok = False
            if _pg_connected and not _snapshot_eligible and _qa_parallel_allowed(_pg_pool, _terms):
                qa_prefill_parallel_ok = _qa_prefill_parallel_cache(
                    pg,
                    _pg_pool,
                    _terms,
                    rerank_top_n=rerank_top_n,
                    limit=limit,
                    config=config,
                    current_outer=outer_admitted,
                )

            if _pg_connected:
                # PG ILIKE 搜索
                try:
                    with _lease_pg_connection(pg) as _pg_conn:
                        if _pg_conn:
                            with _pg_conn.cursor() as _pg_cur:
                                # 2026-08-09: 计算 query 词在 qa_pairs 的词频 (稀有词加权用)
                                # 2026-08-17: 先为所有 term 建零频默认值；聚合 SQL 对零命中 term 不返回行，
                                # 但旧逐词 count(*) 会明确得到 0，缺失项也必须继续按 rare 处理。
                                _term_freq = {_t.lower(): 0 for _t in _terms}
                                _term_freq_ok = True  # 2026-08-09 (W-4 修复): 词频统计失败标记 — 失败时不启用稀有加权, 防"全 0 → 全稀有 → 全部 +2.0"假实现
                                # 2026-09-04 P1.3.1 deadline-closure: per-call cache mapping
                                # ``qa_id`` → ``[term_idx, ...]`` for the QA scoring block to
                                # reuse.  Initialised to an empty dict here so the
                                # scoring block can safely test ``if _qa_matched_terms_by_id``
                                # regardless of which path produced ``_qa_hits_raw``.
                                # Only the snapshot path populates it; SQL / non-snapshot
                                # paths leave it empty and the scoring block falls back
                                # to the original substring scan (fail-safe).
                                _qa_matched_terms_by_id: dict = {}
                                _total_qa = 0
                                _total_qa_version = None
                                _total_qa_ok = False
                                try:
                                    _pg_cur.execute("SELECT count(*), COALESCE(max(id), 0) FROM qa_pairs")
                                    _total_row = _pg_cur.fetchone() or (0,)
                                    _total_qa = int(_total_row[0] or 0)
                                    if len(_total_row) > 1:
                                        _total_qa_version = (_total_qa, int(_total_row[1] or 0))
                                    _total_qa_ok = True
                                except PrefetchDeadlineExceeded:
                                    raise
                                except Exception:
                                    _term_freq_ok = False
                                    logger.debug("词频统计失败, 稀有加权禁用: %s", _safe_err(Exception)[:80])
                                # Real pool-backed reads use one SQL statement for the
                                # exact frequencies and the per-term QA rows.  The
                                # legacy/fake path below remains unchanged as a
                                # fail-safe fallback.
                                _qa_per_term_limit = max(rerank_top_n or 0, limit * 2) // max(len(_terms), 1)
                                _qa_per_term_min = int((config.get("recall") or {}).get("qa_per_term_min", 10))
                                _qa_per_term_limit = max(_qa_per_term_limit, _qa_per_term_min)
                                _qa_rare_limit = max(rerank_top_n or 0, limit * 3)
                                _qa_freq_limit = max(rerank_top_n or 0, limit * 8)
                                if isinstance(config, dict):
                                    _qa_freq_limit = int((config.get("recall") or {}).get("qa_freq_limit", _qa_freq_limit) or _qa_freq_limit)
                                _combined_qa_rows = None
                                _qa_cache_params = (
                                    int(_qa_rare_limit),
                                    int(_qa_per_term_limit),
                                    int(_qa_freq_limit),
                                )
                                _qa_cache = (
                                    _qa_keyword_cache_for_pool(
                                        _pg_pool,
                                        _total_qa_version,
                                        _qa_cache_params,
                                    )
                                    if _use_combined_qa and _total_qa_ok
                                    else None
                                )
                                # 2026-09-02 P1.3.1: long-query QA snapshot path.
                                # When len(_terms) >= qa_snapshot_min_terms and the snapshot
                                # version matches, load all qa_pairs once via the existing
                                # real-cursor lease, precompute lowercase, and serve the
                                # combined lookup in Python. The result feeds the SAME
                                # _combined_qa_rows / _term_freq variables as the SQL path,
                                # preserving phase-1/phase-2/RRF semantics.
                                # Failures silently fall through to the existing path.
                                # 2026-09-04 P1.3.1 deadline-closure: pass the current
                                # ``deadline`` into the snapshot load/lookup so they
                                # propagate the bound to the per-row prep/scan loops and
                                # raise ``PrefetchDeadlineExceeded`` instead of silently
                                # overrunning the budget.  Also build a per-call
                                # ``_qa_matched_terms_by_id`` dict and hand it to the
                                # lookup as ``matched_terms_out``; the QA scoring block
                                # (line ~2065) reuses the populated dict to skip the
                                # second per-candidate ``term in q+a`` substring scan.
                                if _snapshot_eligible and _total_qa_ok:
                                    try:
                                        _snapshot = _qa_snapshot_load(
                                            pg, _pg_pool, _pg_cur,
                                            _total_qa_version, config,
                                            deadline=deadline,
                                        )
                                        if _snapshot is not None:
                                            # 2026-09-04 P1.3.1: the per-call dict was
                                            # hoisted to the outer ``with _pg_cur:``
                                            # scope above so the QA scoring block can
                                            # reuse it.  The snapshot lookup populates
                                            # it in place; only the snapshot path
                                            # populates it; SQL / non-snapshot paths
                                            # leave it empty and the scoring block
                                            # falls back to its original substring
                                            # check (fail-safe).
                                            _term_freq, _combined_qa_rows = _qa_snapshot_lookup(
                                                _snapshot, _terms,
                                                rare_limit=_qa_rare_limit,
                                                per_term_limit=_qa_per_term_limit,
                                                freq_limit=_qa_freq_limit,
                                                matched_terms_out=_qa_matched_terms_by_id,
                                                deadline=deadline,
                                            )
                                            _term_freq_ok = True
                                    except PrefetchDeadlineExceeded:
                                        raise
                                    except Exception:
                                        try:
                                            _pg_conn.rollback()
                                        except Exception:
                                            pass
                                if _use_combined_qa and _total_qa_ok and _combined_qa_rows is None:
                                    try:
                                        if _qa_cache is not None:
                                            _term_freq, _combined_qa_rows = _qa_keyword_cache_lookup(
                                                _pg_cur,
                                                _terms,
                                                _qa_cache,
                                                pg=pg,
                                                # P1.3 + A2 follow-up: main lookup
                                                # only spins up the parallel race
                                                # when the prefill gate granted it.
                                                # ``_qa_parallel_allowed`` is the
                                                # capability gate (pool size, term
                                                # count); ``qa_prefill_parallel_ok``
                                                # is the runtime admission gate
                                                # (reservation actually granted ≥ 2).
                                                parallel=(
                                                    qa_prefill_parallel_ok
                                                    and _qa_parallel_allowed(_pg_pool, _terms)
                                                ),
                                                rare_limit=_qa_rare_limit,
                                                per_term_limit=_qa_per_term_limit,
                                                freq_limit=_qa_freq_limit,
                                            )
                                    except PrefetchDeadlineExceeded:
                                        raise
                                    except Exception:
                                        try:
                                            _pg_conn.rollback()
                                        except Exception:
                                            pass
                                if _use_combined_qa and _total_qa_ok and _combined_qa_rows is None:
                                    try:
                                        _term_freq, _combined_qa_rows = _combined_qa_keyword_lookup(
                                            _pg_cur,
                                            _terms,
                                            rare_limit=_qa_rare_limit,
                                            per_term_limit=_qa_per_term_limit,
                                            freq_limit=_qa_freq_limit,
                                        )
                                    except PrefetchDeadlineExceeded:
                                        raise
                                    except Exception:
                                        # psycopg2 marks the transaction aborted
                                        # after a failed statement; rollback before
                                        # the old fallback issues another SQL.
                                        try:
                                            _pg_conn.rollback()
                                        except Exception:
                                            pass
                                if _combined_qa_rows is None and _total_qa_ok:
                                    try:
                                        _patterns = [f'%{_t}%' for _t in _terms]
                                        _pg_cur.execute(
                                            "SELECT p, COUNT(*) FROM qa_pairs, unnest(%s::text[]) AS p "
                                            "WHERE question ILIKE p OR answer ILIKE p GROUP BY p",
                                            (_patterns,),
                                        )
                                        for _pat, _cnt in _pg_cur.fetchall():
                                            _term_freq[_pat.strip("%")] = int(_cnt or 0)
                                    except PrefetchDeadlineExceeded:
                                        raise
                                    except Exception:
                                        _term_freq_ok = False
                                        logger.debug("词频统计失败, 稀有加权禁用: %s", _safe_err(Exception)[:80])
                                # 2026-09-02 P1.3.1: topic-keyword snapshot path.
                                # Active topics change slower than qa_pairs, so the
                                # version (COUNT, MAX(topic_id), MAX(updated_at))
                                # is read once here (same real cursor / lease) and
                                # a single SELECT of (topic_id, title, summary,
                                # body, keywords) for status='active' rows
                                # replaces both the OR-with-keywords ILIKE main
                                # query and the rare-batch helper.  On load
                                # failure the existing SQL paths run unchanged.
                                _topic_snapshot_cache = None
                                _topic_snapshot_main: list = []
                                _topic_snapshot_rare: list[list] = [[] for _ in _terms]
                                _topic_version = None
                                if _snapshot_eligible and _use_combined_qa and _is_real_pg_cursor(_pg_cur):
                                    try:
                                        _pg_cur.execute(
                                            "SELECT COUNT(*), "
                                            "       COALESCE(MAX(id), 0), "
                                            "       COALESCE(MAX(updated_at), 'epoch') "
                                            "FROM topics WHERE status='active'"
                                        )
                                        _tv_row = _pg_cur.fetchone() or (0, 0, "epoch")
                                        _topic_version = (
                                            int(_tv_row[0] or 0),
                                            int(_tv_row[1] or 0),
                                            str(_tv_row[2] or "epoch"),
                                        )
                                    except PrefetchDeadlineExceeded:
                                        raise
                                    except Exception:
                                        _topic_version = None
                                        try:
                                            _pg_conn.rollback()
                                        except Exception:
                                            pass
                                    if _topic_version is not None:
                                        try:
                                            _topic_snapshot_cache = _topic_snapshot_load(
                                                pg, _pg_pool, _pg_cur,
                                                _topic_version, config,
                                            )
                                        except PrefetchDeadlineExceeded:
                                            raise
                                        except Exception:
                                            _topic_snapshot_cache = None
                                            try:
                                                _pg_conn.rollback()
                                            except Exception:
                                                pass
                                        if _topic_snapshot_cache is not None:
                                            try:
                                                (
                                                    _topic_snapshot_main,
                                                    _topic_snapshot_rare,
                                                ) = _topic_snapshot_lookup(
                                                    _topic_snapshot_cache,
                                                    _terms,
                                                    limit=max(rerank_top_n or 0, limit * 3),
                                                )
                                            except Exception:
                                                _topic_snapshot_cache = None

                                if _topic_snapshot_cache is not None:
                                    # Snapshot served the keyword path. Append in
                                    # the same order the SQL path would have used:
                                    # main OR rows first, then rare-batch rows in
                                    # original term order, de-duplicated by topic_id.
                                    for _r in _topic_snapshot_main:
                                        _kw_topics.append(_r)
                                    for _et_list in _topic_snapshot_rare:
                                        _existing_t = {r[0] for r in _kw_topics}
                                        for _et in _et_list:
                                            if _et[0] not in _existing_t:
                                                _existing_t.add(_et[0])
                                                _kw_topics.append(_et)
                                else:
                                    _wheres = []
                                    _params = []
                                    for _t in _terms:
                                        # 2026-08-09: keywords 列加入匹配 — 提炼时生成的原词/同义词/实体
                                        # (t_eaca3584 keywords 含'瑞典', query 'sweden' 可命中; 原文改写丢词由 keywords 补回)
                                        _wheres.append("(title ILIKE %s OR body ILIKE %s OR summary ILIKE %s OR EXISTS (SELECT 1 FROM unnest(keywords) kw WHERE kw ILIKE %s))")
                                        _params.extend([f'%{_t}%', f'%{_t}%', f'%{_t}%', f'%{_t}%'])
                                    _params.append(max(rerank_top_n or 0, limit * 3))
                                    _pg_cur.execute(
                                        f"SELECT topic_id, title, summary, body FROM topics "
                                        f"WHERE status='active' AND ({' OR '.join(_wheres)}) LIMIT %s",
                                        _params
                                    )
                                    for _r in _pg_cur.fetchall():
                                        _kw_topics.append(_r)

                                    # 2026-08-09: 主题卡稀有词补充查询 — OR+LIKE+LIMIT 让泛词(research)
                                    # 命中一堆卡, 精确实体(adoption)卡排 30 名外被截断 (idx 3 案例)
                                    # 主题卡库 ≤3 张含该词 = 稀有 → 单独查保证进候选
                                    try:
                                        _topic_rare_terms = [t for t in _terms if len(t) > 3]
                                        _topic_rare_batch = None
                                        if _use_combined_qa and _is_real_pg_cursor(_pg_cur):
                                            try:
                                                _topic_rare_batch = _combined_topic_rare_keyword_lookup(
                                                    _pg_cur,
                                                    _topic_rare_terms,
                                                    max(rerank_top_n or 0, limit * 3),
                                                )
                                            except PrefetchDeadlineExceeded:
                                                raise
                                            except Exception:
                                                try:
                                                    _pg_conn.rollback()
                                                except Exception:
                                                    pass
                                        if _topic_rare_batch is not None:
                                            _existing_t = {r[0] for r in _kw_topics}
                                            for _et in _topic_rare_batch:
                                                if _et[0] not in _existing_t:
                                                    _existing_t.add(_et[0])
                                                    _kw_topics.append(_et)
                                        for _rt in ([] if _topic_rare_batch is not None else _topic_rare_terms):
                                            _pg_cur.execute(
                                                "SELECT topic_id, title, summary, body FROM topics "
                                                "WHERE status='active' AND (title ILIKE %s OR body ILIKE %s OR summary ILIKE %s OR EXISTS (SELECT 1 FROM unnest(keywords) kw WHERE kw ILIKE %s)) LIMIT %s",
                                                [f'%{_rt}%', f'%{_rt}%', f'%{_rt}%', f'%{_rt}%', max(rerank_top_n or 0, limit * 3)]
                                            )
                                            _extra_t = _pg_cur.fetchall()
                                            _existing_t = {r[0] for r in _kw_topics}
                                            for _et in _extra_t:
                                                if _et[0] not in _existing_t:
                                                    _kw_topics.append(_et)
                                    except PrefetchDeadlineExceeded:
                                        raise
                                    except Exception:
                                        pass

                                # 2026-08-09: QA 原文维 (用户: 原文精确匹配就该兜住) —
                                # qa_pairs 问题+答案 ILIKE, 命中返回原文 (含时间戳)
                                # 2026-08-09: 不按 id DESC 截断 — 那会只取最新 QA, 早期精确命中
                                # (如 5 月的 charity race) 被 10 月高频 QA 挤掉; 全量命中交给 RRF 评分排序
                                # 2026-08-11: 结构性修复 — OR 全词 + id ASC + LIMIT 会让泛词 (melanie 56 条
                                # /when/class 72 条) 占满候选位, id 靠后的精确实体命中 (pottery 14 条全在
                                # id 474+) 被截断永远进不来 (实测 0/14)。改为每词独立查询配额:
                                # 每个 query 词单独 ILIKE (各 LIMIT 小量), 合并去重 — 保证每个词都有
                                # 候选配额, 实体词不被泛词挤出。评分排序保持稀有度分级不变 (不稀释 ≤3 提权)。
                                _qa_hits_raw = []
                                _seen_qa = set()
                                _qa_per_term_limit = max(rerank_top_n or 0, limit * 2) // max(len(_terms), 1)
                                _qa_per_term_min = int((config.get("recall") or {}).get("qa_per_term_min", 10))
                                _qa_per_term_limit = max(_qa_per_term_limit, _qa_per_term_min)
                                # 2026-08-17 P95 优化: 每词仍只 1 次 SQL; rare term 第一次就用大 LIMIT 拿到足够 rows,
                                # 把 rows 缓存到 _qa_rows_per_term[i], 给 Phase 1 (rows[:_qa_per_term_limit]) 和
                                # Phase 2 (rare term 的剩余 rows) 共享 — 删除旧的双轮 repeated-query, 同时
                                # 复现旧代码 Phase 1 (per-term order) + Phase 2 (rare-term trailing extras)
                                # 的两阶段插入顺序 — 防止 rare extras 错位插入 _terms 中间、撑乱 _qa_boost_sig
                                # 的 dict 顺序、把生产 top-30 集合/排名搞乱。
                                _qa_rare_limit = max(rerank_top_n or 0, limit * 3)
                                # 2026-08-20: 中度词配额按词频缩放 — 固定 per-term LIMIT(=10) 按 id ASC
                                # 截断会让 id 靠后的精确命中永远进不了候选 (running 54 条命中里
                                # 目标 QA id=1563 排第 11+ 位, 全库 2871 重排后 Melanie 会话 id 集中
                                # 后段; 旧库 id=65 就能进)。词频越高配额越大, 封顶 _qa_freq_limit。
                                # 稀有词 (≤3) 语义不变 (仍走 _qa_rare_limit 大配额 + 0.08 bonus)。
                                _qa_freq_limit = max(rerank_top_n or 0, limit * 8)  # 默认 limit=5 -> 40
                                if isinstance(config, dict):
                                    _qa_freq_limit = int((config.get("recall") or {}).get("qa_freq_limit", _qa_freq_limit) or _qa_freq_limit)
                                _qa_rows_per_term: list[list] = []
                                _qa_is_rare_per_term: list[bool] = []
                                _qa_terms_in_order: list[str] = []
                                _qa_term_limits: list[int] = []
                                def _read_qa_rows_for_term(_term, _limit):
                                    if _combined_qa_rows is not None:
                                        return _combined_qa_rows[len(_qa_rows_per_term)]
                                    try:
                                        _pg_cur.execute(
                                            "SELECT id, timestamp, question, answer FROM qa_pairs "
                                            "WHERE (question ILIKE %s OR answer ILIKE %s) "
                                            "AND answer IS NOT NULL "
                                            "AND session_id NOT LIKE '%%.trajectory%%' "
                                            "ORDER BY id ASC LIMIT %s",
                                            [f'%{_term}%', f'%{_term}%', _limit]
                                        )
                                        return list(_pg_cur.fetchall())
                                    except PrefetchDeadlineExceeded:
                                        raise
                                    except Exception:
                                        return []

                                for _t in _terms:
                                    # 2026-09-04 P1.3.1 deadline-closure: probe the
                                    # bound once per term inside the per-term read
                                    # loop so a long combined row set cannot overrun
                                    # the SLO.  PrefetchDeadlineExceeded is propagated
                                    # by the surrounding try/except.
                                    if deadline is not None and _total_qa_ok:
                                        deadline.check(context="qa candidate collection")
                                    _tl = _t.lower()
                                    _is_rare = _term_freq_ok and _term_freq.get(_tl, 99) <= 3
                                    if _is_rare:
                                        _lim = _qa_rare_limit
                                    else:
                                        # 按词频缩放: 词频 4~封顶之间给足配额; 词频>封顶仍取封顶 (防泛词爆炸)
                                        _freq = _term_freq.get(_tl, 0) if _term_freq_ok else 0
                                        _lim = max(_qa_per_term_limit, min(_freq, _qa_freq_limit) if _freq else _qa_per_term_limit)
                                    _qa_terms_in_order.append(_t)
                                    _qa_term_limits.append(_lim)
                                    _qa_rows_per_term.append(_read_qa_rows_for_term(_t, _lim))
                                    _qa_is_rare_per_term.append(_is_rare)
                                # Phase 1: 按 _terms 顺序, 每词只取 rows[:_lim] (lim=该词动态配额,
                                # 非稀有词按词频缩放后能覆盖 id 靠后的精确命中), dedup append。
                                # 2026-09-04 P1.3.1 deadline-closure: probe every 64
                                # candidate appends so a long raw-hit list cannot
                                # overrun the SLO.  PrefetchDeadlineExceeded propagates
                                # through the surrounding try/except.
                                _phase1_seen = 0
                                for _t, _rows, _lim in zip(_qa_terms_in_order, _qa_rows_per_term, _qa_term_limits):
                                    for _r in _rows[:_lim]:
                                        if deadline is not None and (_phase1_seen & 0x3F) == 0:
                                            deadline.check(context="qa phase1 dedup")
                                        _phase1_seen += 1
                                        if _r[0] not in _seen_qa:
                                            _seen_qa.add(_r[0])
                                            _qa_hits_raw.append(_r)
                                # Phase 2: 严格复现旧语义 — 仅在 _term_freq_ok=True 时启用,
                                # 按 _terms 原顺序的 _rare_terms 子集遍历, 每词遍历 rows[_qa_per_term_limit:]
                                # (extra) dedup append 到 _qa_hits_raw; 同时给该 rare term 的全部 rows
                                # 一次性设置 _rare_bonus=0.08 (旧代码 rare query 给所有 row 设 bonus,
                                # 含 phase-1 已收录的 rows[:_qa_per_term_limit] 部分)。
                                # 2026-09-04 P1.3.1 deadline-closure: same low-frequency
                                # probe inside the rare-extras dedup pass.
                                if _term_freq_ok:
                                    _rare_terms_ordered = [
                                        _t for _t, _is_rare in zip(_qa_terms_in_order, _qa_is_rare_per_term)
                                        if _is_rare
                                    ]
                                    _phase2_seen = 0
                                    for _t in _rare_terms_ordered:
                                        _idx = _qa_terms_in_order.index(_t)
                                        _rows = _qa_rows_per_term[_idx]
                                        # bonus 给该 rare term 全部 rows (含 phase-1 已写入的 rows[:_qa_per_term_limit])
                                        for _r in _rows:
                                            _rare_bonus[f"qa_{_r[0]}"] = 0.08
                                        # extra rows (rows[_qa_per_term_limit:]) dedup append
                                        for _r in _rows[_qa_per_term_limit:]:
                                            if deadline is not None and (_phase2_seen & 0x3F) == 0:
                                                deadline.check(context="qa phase2 dedup")
                                            _phase2_seen += 1
                                            if _r[0] not in _seen_qa:
                                                _seen_qa.add(_r[0])
                                                _qa_hits_raw.append(_r)
                except PrefetchDeadlineExceeded:
                    # A8 (G6B Slice B): record the lane timeout BEFORE the raise.
                    try:
                        _probe(trace, 'lane_finish', 'keyword', timed_out=True, reason='deadline')
                    except Exception:
                        pass
                    raise
                except Exception as _e:
                    # A7 (G6B Slice B): record the degradation in the
                    # trace BEFORE the existing logger call.
                    try:
                        _probe(trace, 'error', 'lane=keyword: ' + str(_e)[:200])
                    except Exception:
                        pass
                    logger.debug("PG keyword search failed: %s", str(_e)[:100])
            elif _terms:
                # SQLite 兜底
                import os as _os
                _kw_db = str(_resolve_data_dir(config) / 'v3_topic_full.db')
                if _os.path.exists(_kw_db):
                    import sqlite3 as _sqlite3
                    _c = _sqlite3.connect(_kw_db)
                    _c.execute("PRAGMA case_sensitive_like=0")
                    _wheres = ' OR '.join(
                        "(title LIKE ? OR body LIKE ? OR summary LIKE ?)"
                        for _ in _terms
                    )
                    _params = []
                    for _t in _terms:
                        _params.extend([f'%{_t}%', f'%{_t}%', f'%{_t}%'])
                    _params.append(max(rerank_top_n or 0, limit * 3))
                    _rows = _c.execute(
                        f"SELECT id, title, summary, body FROM topic_blocks WHERE {_wheres} LIMIT ?",
                        _params
                    ).fetchall()
                    for _r in _rows:
                        _kw_topics.append((_r[0], _r[1], _r[2], _r[3]))
                    _c.close()

            for _r in _kw_topics:
                _sid = f"topic_{_r[0]}"
                kw_ids.add(_sid)
                # 2026-08-09: body 优先, summary 兜底 — summary 常丢细节 (Sweden 案例:
                # summary 没提瑞典, body 有"她来自瑞典" — 注入 summary 导致模型答'记忆中没有')
                _full = (_r[3] or '') or (_r[2] or '')
                # 2026-08-09: 主题卡关键词评分 — 匹配词数 + 稀有词加成 (与 QA 逻辑对称)
                # 原来写死 0.5 — 泛词(research)命中一堆卡同分, 精确实体(adoption)卡无法凸显
                _t_score = 0.5
                try:
                    _t_lower = ((_r[1] or '') + ' ' + (_r[2] or '') + ' ' + (_r[3] or '')).lower()
                    _t_match = [t for t in _terms if len(t) > 2 and t.lower() in _t_lower]
                    _t_score = 0.5 + 0.15 * len(_t_match)
                except PrefetchDeadlineExceeded:
                    # A8 (G6B Slice B): record the lane timeout BEFORE
                    # the raise.  Inside the topic-keyword scoring block.
                    try:
                        _probe(trace, 'lane_finish', 'keyword', timed_out=True, reason='deadline')
                    except Exception:
                        pass
                    raise
                except Exception:
                    pass
                hits[_sid] = RecallHit(
                    source_id=_sid,
                    title=_r[1] or '',
                    content_preview=_full[:500],
                    content=_full,  # 2026-08-08: 全文通道
                    category='topic',
                    tags=[],
                    rrf_score=min(_t_score, 1.5),  # 2026-08-09: 上限 1.5 — 多词命中允许更高
                    kind='topic',
                    created_at='',
                )

            # 2026-08-09: QA 原文命中加入结果 (用户: 原文精确匹配就该兜住)
            # 2026-08-09: 评分按匹配词数 — 高频泛词(Melanie)命中多但每个词信息量低,
            # 稀有词(charity)命中少但信息量高; 按 query 词在原文的出现数加权,
            # 避免"Melanie"命中 100 条把"charity race"原文挤出 top20
            try:
                _qa_terms = [t for t in _terms if len(t) > 2]  # 去掉 1-2 字符泛词
                # 2026-09-04 P1.3.1 deadline-closure: when the snapshot path
                # populated ``_qa_matched_terms_by_id`` we reuse it to skip
                # the second per-candidate ``term in q+a`` substring scan.
                # The prebuilt dict is keyed by row id and stores term
                # indices in ``_terms`` order; we map those indices back to
                # the original-case terms to keep rare / min-frequency /
                # boost semantics identical.  When the dict is empty (SQL
                # / non-snapshot / no-Automaton-fallback paths) the original
                # substring check is preserved as a fail-safe.
                _have_snapshot_meta = bool(_qa_matched_terms_by_id)
                _score_seen = 0
                for _r in _qa_hits_raw:
                    # 2026-09-04 P1.3.1 deadline-closure: probe the bound every
                    # 64 candidates inside the QA scoring loop so a long hit
                    # list cannot overrun the SLO.  PrefetchDeadlineExceeded
                    # propagates through the surrounding try/except.
                    if deadline is not None and (_score_seen & 0x3F) == 0:
                        deadline.check(context="qa scoring")
                    _score_seen += 1
                    _sid = f"qa_{_r[0]}"
                    kw_ids.add(_sid)
                    _q = (_r[2] or '')
                    _a = (_r[3] or '')
                    _ts = str(_r[1] or '')
                    _full = f"[{_ts}] Q: {_q}\nA: {_a}"
                    # 匹配质量: 该 QA 命中了几个 query 词 (区分稀有词: 词在全部 QA 出现越少权重越高)
                    if _have_snapshot_meta and _r[0] in _qa_matched_terms_by_id:
                        # Fast path: re-use the snapshot's per-row term index list.
                        # Filter to _qa_terms (len>2) so rare/min-frequency/boost
                        # semantics stay identical to the substring check.
                        _match_terms = [
                            _terms[i] for i in _qa_matched_terms_by_id[_r[0]]
                            if i < len(_terms) and len(_terms[i]) > 2
                        ]
                    else:
                        _qa_lower = (_q + ' ' + _a).lower()
                        _match_terms = [t for t in _qa_terms if t.lower() in _qa_lower]
                    _qa_score = 0.6 + 0.2 * len(_match_terms)
                    # 稀有词加成: charity(1 条) > melanie(100 条) — 用词频倒数加权
                    # 2026-08-09 修正: 分级加权 — 全库仅 1 条命中 = 用户精确指认, 必须第一
                    # (museum 案例: 全库 1 条含 museum, 但只 +0.3 排第 29 — 精确命中没赢)
                    # 2026-08-09 v2: rrf_score 直接设超高值 (2.0) — 精确命中=用户指认, 不跟 RRF 排名缠斗
                    # (RRF 各路径最高 ~0.05, 2.0 保证精确命中排第一)
                    try:
                        # 2026-08-12 A-1 修正: 稀有度取匹配词中【最低词频】(最稀有的词), 不是 _match_terms[0]
                        # — 泛词在前 (the/when 匹配 museum 证据的 the+museum) 时原逻辑加成被泛词词频吞掉,
                        # 真稀有词 (museum) 拿不到分 → 精确证据不保送
                        if _term_freq_ok and _match_terms:
                            _freqs = [_term_freq.get(t.lower(), 99) for t in _match_terms]
                            _freq = min(_freqs)
                        else:
                            _freq = 10
                        if _freq <= 1:
                            _qa_score += 2.0  # 全库唯一命中 = 超强信号, 直接第一
                        elif _freq <= 3:
                            _qa_score += 0.8  # 极稀有
                        elif _freq <= 10:
                            _qa_score += 0.4  # 稀有
                    except PrefetchDeadlineExceeded:
                        raise
                    except Exception:
                        pass
                    # 2026-08-12 A-1: 记录保送信号 (min_freq, 低频词数) — 条件见保送块。
                    # 泛词共现 (when+melanie+the) 无低频词 → 不保送; children 单词(freq25) 1低频 → 不保送;
                    # charity(16)+race(14) 2低频共现 / museum(≤3) 极稀有 → 保送
                    if _term_freq_ok and _match_terms:
                        _freqs_l = [_term_freq.get(t.lower(), 99) for t in _match_terms]
                        _qa_boost_sig[_sid] = (min(_freqs_l), sum(1 for f in _freqs_l if f <= max(3, int(_total_qa * 0.01))))
                    else:
                        _qa_boost_sig[_sid] = (99, 0)
                    hits[_sid] = RecallHit(
                        source_id=_sid,
                        title=_q[:50] or '',
                        content_preview=_full[:500],
                        content=_full,  # 全文 (含时间戳)
                        category='qa',
                        tags=[],
                        rrf_score=min(_qa_score, 2.0),  # 2026-08-09: 上限 1.0→2.0 — 精确稀有命中允许超高 (museum 唯一命中必须第一)
                        kind='qa',
                        created_at=_ts,
                    )
            except PrefetchDeadlineExceeded:
                # A8 (G6B Slice B): record the lane timeout BEFORE the
                # raise.  Inside the QA scoring block.
                try:
                    _probe(trace, 'lane_finish', 'qa', timed_out=True, reason='deadline')
                except Exception:
                    pass
                raise
            except Exception:
                pass
        except PrefetchDeadlineExceeded:
            # A8 (G6B Slice B): record the lane timeout BEFORE the
            # raise.  Top-level keyword block handler.
            try:
                _probe(trace, 'lane_finish', 'keyword', timed_out=True, reason='deadline')
            except Exception:
                pass
            raise
        except Exception as e:
            # A7 (G6B Slice B): record the degradation in the trace
            # BEFORE the existing logger call.  This is the keyword
            # lane's top-level except — covers the PG / SQLite /
            # scoring seams of the entire keyword block.
            try:
                _probe(trace, 'error', 'lane=keyword: ' + str(e)[:200])
            except Exception:
                pass
            logger.debug("topic keyword search failed: %s", _safe_err(e)[:100])

    # ── Keyword / QA lane summary (split kw_ids by hit kind) ──
    # Yields the lists of ids that flowed through the keyword path.
    try:
        _kw_kw_ids: list[str] = []
        _qa_kw_ids: list[str] = []
        for _sid in kw_ids:
            try:
                _h = hits.get(_sid)
                if _h is None:
                    continue
                _k = str(getattr(_h, "kind", "") or "")
            except Exception:
                _k = ""
            if _k == "qa":
                _qa_kw_ids.append(_sid)
            else:
                _kw_kw_ids.append(_sid)
    except Exception:
        _kw_kw_ids, _qa_kw_ids = [], []
    _probe(trace, 'lane_candidates', 'keyword', _kw_kw_ids)
    _probe(trace, 'lane_candidates', 'qa', _qa_kw_ids)
    if include_keyword:
        _probe(trace, 'lane_finish', 'keyword', candidate_count=len(_kw_kw_ids))
        _probe(trace, 'lane_finish', 'qa', candidate_count=len(_qa_kw_ids))
    else:
        _probe(trace, 'lane_finish', 'keyword', skipped=True, reason='include_keyword=False')
        _probe(trace, 'lane_finish', 'qa', skipped=True, reason='include_keyword=False')

    # Vector topic path — TopicRecall 向量搜索（替代旧 card_vector）
    _probe(trace, 'lane_start', 'vector')
    if include_card_vector:
        if deadline is not None:
            deadline.check(context="topic vector path")
        try:
            # 2026-09-09 P2a: active-memory vector retrieval at the candidate-
            # collection seam. Reuses the same `pg` + bound deadline + the
            # caller's already-computed q_emb (no embedding provider call).
            # Adds hits to the existing vec_ids set so the existing
            # VEC_RRF_WEIGHT=1.0 path picks them up unchanged.
            try:
                if deadline is not None:
                    deadline.check(context="active memory vector path")
                _am_vec_reader_seen = True
                _am_vec_reader_present = bool(q_emb)
                if q_emb:
                    _am_reader_v = _active_memory_reader_for(pg, deadline)
                    if _am_reader_v is not None:
                        _am_vec_reader_present = True
                        try:
                            _am_vec_rows = _am_reader_v.search_vector(
                                q_emb, limit=max(rerank_top_n or 0, limit * 3)
                            )
                        except PrefetchDeadlineExceeded:
                            # A8 (G6B Slice B): record the lane timeout
                            # BEFORE the raise.
                            try:
                                _probe(trace, 'lane_finish', 'explicit', timed_out=True, reason='deadline')
                            except Exception:
                                pass
                            raise
                        except Exception as _am_vec_exc:
                            # A7 (G6B Slice B): record the degradation
                            # in the trace BEFORE the existing logger.
                            try:
                                _probe(trace, 'error', 'lane=explicit: ' + str(_am_vec_exc)[:200])
                            except Exception:
                                pass
                            logger.debug(
                                "active-memory vector search failed: %s",
                                _safe_err(_am_vec_exc)[:200],
                            )
                            _am_vec_rows = []
                        for _row in (_am_vec_rows or []):
                            try:
                                _mid = _row.get("memory_id") or _row.get("source_id")
                            except Exception:
                                _mid = None
                            if isinstance(_mid, str) and _mid:
                                _am_vec_ids_for_probe.append(_mid)
                        _add_active_memory_to_hits(
                            _am_vec_rows,
                            hits=hits,
                            target_ids=vec_ids,
                            with_cosine=True,
                        )
            except PrefetchDeadlineExceeded:
                # A8 (G6B Slice B): record the lane timeout BEFORE the raise.
                try:
                    _probe(trace, 'lane_finish', 'explicit', timed_out=True, reason='deadline')
                except Exception:
                    pass
                raise
            except Exception as _am_vec_outer:
                # A7 (G6B Slice B): record the degradation in the trace
                # BEFORE the existing logger call.
                try:
                    _probe(trace, 'error', 'lane=explicit: ' + str(_am_vec_outer)[:200])
                except Exception:
                    pass
                logger.debug(
                    "active-memory vector seam failed: %s",
                    _safe_err(_am_vec_outer)[:200],
                )
                _am_vec_ids_for_probe = []
            # G6B Slice D (D2): the explicit lane now always finishes below,
            # outside the ``if include_card_vector:`` branch.
            _emb_cfg = _extract_embed(config) if config is not None else None
            # Runtime cache when a real runtime-backed Core is passed; legacy
            # core._topic_recall and core=None temporary fallback unchanged.
            if deadline is None:
                _tr = _resolve_topic_recall(core, _emb_cfg, pg)
            else:
                _tr = _resolve_topic_recall(core, _emb_cfg, pg, deadline=deadline)
            _vec_mult = int((config.get("recall") or {}).get("vector_top_mult", 4))
            # 2026-08-17: 把上游已算好的 q_emb 透传给 TopicRecall.match,
            # 避免长 query (>1000 字符) 因 cache key 与上游 call_embedding 不同
            # 而再次调 provider embedding (实测多耗 0.5-1.9s). q_emb 缺失时
            # match 仍走旧路径 (内部 call_embedding(query[:1000], ...)).
            _tm = _tr.match(
                query,
                threshold=0.4,
                top_k=max(rerank_top_n or 0, limit * _vec_mult),
                query_embedding=q_emb,
                deadline=deadline,
            )
            for _sim, _t in _tm:
                _tid = _t.get('id')
                if _tid is None:
                    continue
                _sid = f"topic_{_tid}"
                vec_ids.add(_sid)
                if _sid in hits:
                    if _sim > hits[_sid].cosine:
                        hits[_sid].cosine = float(_sim)
                else:
                    _full = (_t.get('body', '') or _t.get('summary', ''))  # 2026-08-09: body 优先 — summary 丢细节 (Sweden 案例)
                    hits[_sid] = RecallHit(
                        source_id=_sid,
                        title=_t.get('title', ''),
                        content_preview=_full[:500],
                        content=_full,  # 2026-08-08: 全文通道
                        category='topic',
                        tags=[],
                        cosine=float(_sim),
                        rrf_score=0.0,
                        kind='topic',
                        created_at='',
                    )

            # 2026-08-09: QA 原文向量检索 (措辞不同时兜底: speech vs event)
            # 关键词匹配对措辞敏感 (school event ≠ speech), 向量语义匹配兜住
            # qa_pairs 有 embedding 时, 用 query 向量查 hnsw
            if q_emb and pg is not None and _pg_is_connected(pg, deadline):
                try:
                    with _lease_pg_connection(pg) as _qa_conn:
                        if _qa_conn:
                            try:
                                _emb_str = "[" + ",".join(str(x) for x in q_emb) + "]"
                                with _qa_conn.cursor() as _qa_cur:
                                    _qa_cur.execute(
                                        "SELECT id, timestamp, question, answer, "
                                        " 1 - (embedding <=> %s::vector) AS cosine "
                                        "FROM qa_pairs WHERE embedding IS NOT NULL "
                                        "AND answer IS NOT NULL "
                                        "AND session_id NOT LIKE '%%.trajectory%%' "
                                        "ORDER BY embedding <=> %s::vector LIMIT %s",
                                        (_emb_str, _emb_str, max(rerank_top_n or 0, limit * _vec_mult)))
                                    for _qid, _qts, _qq, _qa, _cos in _qa_cur.fetchall():
                                        _sim = float(_cos or 0)
                                        if _sim < 0.35:  # 低阈值 — 语义兜底, 宁可多召回
                                            continue
                                        _qsid = f"qa_{_qid}"
                                        vec_ids.add(_qsid)
                                        _qfull = f"[{str(_qts or '')}] Q: {_qq}\nA: {_qa}"
                                        if _qsid in hits:
                                            if _sim > hits[_qsid].cosine:
                                                hits[_qsid].cosine = _sim
                                        else:
                                            hits[_qsid] = RecallHit(
                                                source_id=_qsid,
                                                title=(_qq or '')[:50],
                                                content_preview=_qfull[:500],
                                                content=_qfull,
                                                category='qa',
                                                tags=[],
                                                cosine=_sim,
                                                rrf_score=0.0,
                                                kind='qa',
                                                created_at=str(_qts or ''),
                                            )
                            except PrefetchDeadlineExceeded:
                                # A8 (G6B Slice B): record the lane
                                # timeout BEFORE the raise.  Inside the
                                # QA vector cursor block.
                                try:
                                    _probe(trace, 'lane_finish', 'qa', timed_out=True, reason='deadline')
                                except Exception:
                                    pass
                                raise
                            except Exception:
                                pass
                except PrefetchDeadlineExceeded:
                    # A8 (G6B Slice B): record the lane timeout BEFORE
                    # the raise.  QA collection outer handler.
                    try:
                        _probe(trace, 'lane_finish', 'qa', timed_out=True, reason='deadline')
                    except Exception:
                        pass
                    raise
                except Exception as _qe:
                    # A7 (G6B Slice B): record the degradation in the
                    # trace BEFORE the existing logger call.  This is
                    # the QA-collection block embedded in the vector
                    # topic path.
                    try:
                        _probe(trace, 'error', 'lane=qa: ' + str(_qe)[:200])
                    except Exception:
                        pass
                    logger.debug("QA vector recall failed: %s", str(_qe)[:100])
        except ValueError:
            raise
        except PrefetchDeadlineExceeded:
            # A8 (G6B Slice B): record the lane timeout BEFORE the raise.
            # Vector topic path top-level handler.
            try:
                _probe(trace, 'lane_finish', 'vector', timed_out=True, reason='deadline')
            except Exception:
                pass
            raise
        except Exception as e:
            # A7 (G6B Slice B): record the degradation in the trace
            # BEFORE the existing logger call.  This is the vector
            # topic path's top-level except.
            try:
                _probe(trace, 'error', 'lane=vector: ' + str(e)[:200])
            except Exception:
                pass
            logger.debug("vector topic recall failed: %s", _safe_err(e)[:200])

    # G6B Slice D (D2): always finish the explicit lane, regardless of
    # ``include_card_vector``.  The lane_finish ALWAYS lands: with the real
    # active-memory candidate count when at least one seam ran, or with
    # skipped=True + a precise reason otherwise.
    try:
        _explicit_ids = list(
            dict.fromkeys(_am_kw_ids_for_probe + _am_vec_ids_for_probe)
        )
    except Exception:
        _explicit_ids = []
    _probe(trace, 'lane_candidates', 'explicit', _explicit_ids)
    if not (_am_kw_reader_seen or _am_vec_reader_seen):
        # Neither kw nor vec seam ran at all (e.g. include_keyword=False AND
        # include_card_vector=False).
        _probe(trace, 'lane_finish', 'explicit',
               skipped=True, reason='active_memory_paths_disabled')
    elif (not _am_kw_reader_present) and (not _am_vec_reader_present):
        # At least one seam's reader resolver ran but resolved to None.
        _probe(trace, 'lane_finish', 'explicit',
               skipped=True, reason='no_active_memory_reader')
    else:
        _probe(trace, 'lane_finish', 'explicit',
               candidate_count=len(_explicit_ids))

    # Vector message path (j/) + noise filter
    if include_message_vector and pg and q_emb:
        if deadline is not None:
            deadline.check(context="message vector path")
        try:
            msg_hits = pg.search(q_emb, kind="message", limit=max(rerank_top_n or 0, limit * 3))
            for vh in msg_hits:
                sid = vh.get("source_id", "")
                cos = vh.get("cosine", 0)
                if cos < 0.3:  # P1: cosine 下限 — 同 card 路径, 低于 0.3 的 message 向量不进入 RRF
                    continue
                preview = vh.get("content_preview", "") or vh.get("title", "")
                msg_ids.add(sid)
                if sid in hits:
                    hits[sid].cosine = cos
                else:
                    hits[sid] = RecallHit(
                        source_id=sid,
                        title=vh.get("title", ""),
                        content_preview=preview,
                        content=vh.get("content", "") or preview,  # 2026-08-08: 全文通道
                        cosine=cos,
                        rrf_score=0.0,
                        kind="message",
                        created_at=vh.get("created_at", ""),
                    )
        except PrefetchDeadlineExceeded:
            # A8 (G6B Slice B): record the lane timeout BEFORE the raise.
            # Vector message path handler.
            try:
                _probe(trace, 'lane_finish', 'vector', timed_out=True, reason='deadline')
            except Exception:
                pass
            raise
        except Exception as e:
            # A7 (G6B Slice B): record the degradation in the trace
            # BEFORE the existing logger call.  This is the vector
            # message path's except — part of the vector lane family.
            try:
                _probe(trace, 'error', 'lane=vector: ' + str(e)[:200])
            except Exception:
                pass
            logger.warning("message vector recall failed: %s", _safe_err(e)[:200])

    # Effective pool path
    if include_effective and pg and q_emb:
        if deadline is not None:
            deadline.check(context="effective path")
        try:
            eff_hits = pg.search_effective(q_emb, limit=max(rerank_top_n or 0, limit * 3))
            for eh in eff_hits:
                sid = eh.get("source_id", "")
                cos = eh.get("cosine", 0)
                if cos < 0.3:  # P1: cosine 下限 — 有效池项目也需过 cosine 闸门
                    continue
                eff_ids.add(sid)
                if sid in hits:
                    hits[sid].cosine = max(hits[sid].cosine, cos)
                else:
                    _eff_full = eh.get("content", "") or ""
                    hits[sid] = RecallHit(
                        source_id=sid,
                        title=eh.get("title", ""),
                        content_preview=_eff_full[:500],
                        content=_eff_full,  # 2026-08-08: 全文通道
                        cosine=cos,
                        rrf_score=0.0,
                        kind="effective",
                        created_at=eh.get("created_at", ""),
                    )
        except ValueError:
            raise
        except PrefetchDeadlineExceeded:
            # A8 (G6B Slice B): record the lane timeout BEFORE the raise.
            # Effective pool path handler.
            try:
                _probe(trace, 'lane_finish', 'vector', timed_out=True, reason='deadline')
            except Exception:
                pass
            raise
        except Exception as e:
            # A7 (G6B Slice B): record the degradation in the trace
            # BEFORE the existing logger call.  This is the effective
            # pool path's except — part of the vector lane family.
            try:
                _probe(trace, 'error', 'lane=vector: ' + str(e)[:200])
            except Exception:
                pass
            logger.debug("effective recall failed: %s", _safe_err(e)[:100])

    # Topic recall path (5th RRF path) — 层次化主题召回, 权重最高
    _probe(trace, 'lane_start', 'topic')
    if include_topic:
        if deadline is not None:
            deadline.check(context="topic path")
        try:
            embed_cfg = _extract_embed(config) if config is not None else None
            # Runtime cache when a real runtime-backed Core is passed; legacy
            # core._topic_recall and core=None temporary fallback unchanged.
            if deadline is None:
                topic_recaller = _resolve_topic_recall(core, embed_cfg, pg)
            else:
                topic_recaller = _resolve_topic_recall(core, embed_cfg, pg, deadline=deadline)
            # 2026-08-17: 把上游已算好的 q_emb 透传给 TopicRecall.match,
            # 避免长 query (>1000 字符) 因 cache key 与上游 call_embedding 不同
            # 而再次调 provider embedding (实测多耗 0.5-1.9s). q_emb 缺失时
            # match 仍走旧路径 (内部 call_embedding(query[:1000], ...)).
            topic_matches = topic_recaller.match(
                query,
                top_k=max(rerank_top_n or 0, limit * 2),
                query_embedding=q_emb,
                deadline=deadline,
            )
            for sim, t in topic_matches:
                tid = t.get("id")
                if tid is None:
                    continue
                sid = f"topic_{tid}"
                topic_ids.add(sid)
                if sid in hits:
                    # higher cosine wins (topic cosine > existing cosine)
                    if sim > hits[sid].cosine:
                        hits[sid].cosine = float(sim)
                else:
                    _full = (t.get("body", "") or t.get("summary", ""))  # 2026-08-09: body 优先 — summary 丢细节 (Sweden 案例)
                    hits[sid] = RecallHit(
                        source_id=sid,
                        title=t.get("title", ""),
                        content_preview=_full[:500],
                        content=_full,  # 2026-08-08: 全文通道
                        category="topic",
                        tags=[],
                        cosine=float(sim),
                        rrf_score=0.0,
                        kind="topic",
                        created_at="",
                    )
        except ValueError:
            raise
        except PrefetchDeadlineExceeded:
            # A8 (G6B Slice B): record the lane timeout BEFORE the raise.
            # Topic lane top-level handler.
            try:
                _probe(trace, 'lane_finish', 'topic', timed_out=True, reason='deadline')
            except Exception:
                pass
            raise
        except Exception as e:
            # A7 (G6B Slice B): record the degradation in the trace
            # BEFORE the existing logger call.  This is the topic
            # lane's top-level except.
            try:
                _probe(trace, 'error', 'lane=topic: ' + str(e)[:200])
            except Exception:
                pass
            logger.debug("topic recall failed: %s", _safe_err(e)[:200])

    # ── Topic lane summary (sorted, mirrors topic_ids set semantics) ──
    try:
        _topic_sorted = sorted(topic_ids)
    except Exception:
        _topic_sorted = list(topic_ids)
    _probe(trace, 'lane_candidates', 'topic', _topic_sorted)
    if include_topic:
        _probe(trace, 'lane_finish', 'topic', candidate_count=len(_topic_sorted))
    else:
        _probe(trace, 'lane_finish', 'topic', skipped=True, reason='include_topic=False')

    # ── 2026-08-06 Step 3c: 印段落召回 (yin_paragraphs — E1 印切段入库) ──
    if include_yin and pg is not None:
        if deadline is not None:
            deadline.check(context="yin path")
        try:
            from .yin_pool import search_yin
            embed_cfg = _extract_embed(config) if config is not None else None
            yin_qemb = None
            if q_emb:
                yin_qemb = q_emb
            elif embed_cfg is not None:
                from .embedding import call_embedding
                # 2026-09-04 P1.3.1 deadline-closure: the foreground
                # yin fallback was a known blocking call (fixed
                # timeout=2.5 + retries=0).  When a prefetch deadline is
                # set we honour it: check the budget *before* the call,
                # clamp the per-call timeout to the remaining budget
                # (capped at the legacy 2.5s ceiling, floored at 1ms),
                # force retries=0, and re-check after the call.  When no
                # deadline is supplied the legacy fixed-timeout contract
                # is preserved byte-for-byte.  PrefetchDeadlineExceeded is
                # propagated by the outer ``try/except`` block; we never
                # add a broad catch that could mask the signal.
                _yin_bound = coerce_deadline(deadline)
                if _yin_bound is not None:
                    _yin_bound.check(context="yin fallback embedding")
                if _yin_bound is None:
                    yin_qemb = call_embedding(query[:1000], embed_cfg, timeout=2.5, retries=0)
                else:
                    _yin_timeout = max(0.001, min(2.5, _yin_bound.remaining()))
                    yin_qemb = call_embedding(query[:1000], embed_cfg,
                                              timeout=_yin_timeout, retries=0)
                    _yin_bound.check(context="yin fallback embedding post-call")
            with _lease_pg_connection(pg) as conn:
                if conn:
                    yin_hits = search_yin(yin_qemb, keyword=query[:60], pg=conn, limit=max(limit, 3))
                    for yh in yin_hits:
                        sid = f"yin:{yh['yin_version']}#{yh['section']}"
                        yin_ids.add(sid)
                        if sid not in hits:
                            hits[sid] = RecallHit(
                                source_id=sid,
                                title=f"印·{yh['section']}",
                                content_preview=(yh.get("content") or "")[:500],
                                content=yh.get("content") or "",  # 2026-08-08: 全文通道
                                category="yin",
                                tags=[],
                                cosine=float(yh.get("score") or 0.0),
                                rrf_score=0.0,
                                kind="yin",
                                created_at="",
                            )
        except ValueError:
            raise
        except PrefetchDeadlineExceeded:
            # A8 (G6B Slice B): record the lane timeout BEFORE the raise.
            # Yin recall handler — part of the vector lane family.
            try:
                _probe(trace, 'lane_finish', 'vector', timed_out=True, reason='deadline')
            except Exception:
                pass
            raise
        except Exception as e:
            # A7 (G6B Slice B): record the degradation in the trace
            # BEFORE the existing logger call.  This is the yin
            # recall block's except — part of the vector lane family.
            try:
                _probe(trace, 'error', 'lane=vector: ' + str(e)[:200])
            except Exception:
                pass
            logger.debug("yin recall failed: %s", _safe_err(e)[:200])

    # ── 2026-08-06: 观察者印召回 (observation_notes — 滚动叙事, 比主题卡更细) ──
    # 观察者印是滚动承接(每版≈上版+增量) — 同一天多版高度重复, 只取每天最新一版
    # (DISTINCT ON date): 历史每天都有代表(不丢), 同天不冗余(不污染)。
    if include_notes and pg is not None and q_emb:
        if deadline is not None:
            deadline.check(context="observation notes path")
        try:
            emb_str = "[" + ",".join(str(x) for x in q_emb) + "]"
            with _lease_pg_connection(pg) as conn:
                if conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT DISTINCT ON (created_at::date) id, version, content, "
                            " 1 - (embedding <=> %s::vector) AS cosine "
                            " FROM observation_notes WHERE embedding IS NOT NULL "
                            " ORDER BY created_at::date DESC, embedding <=> %s::vector LIMIT %s",
                            (emb_str, emb_str, max(limit, 3)),
                        )
                        for row in cur.fetchall():
                            note_id, version, full_content, cosine = row
                            sid = f"note:{version}"
                            sim = float(cosine or 0)
                            if sim < 0.30:  # 低相关不注入（观察者印是叙事, 阈值放宽到 0.3）
                                continue
                            notes_ids.add(sid)
                            if sid not in hits:
                                hits[sid] = RecallHit(
                                    source_id=sid,
                                    title=f"记忆印·{version}",
                                    content_preview=(full_content or "")[:500],
                                    content=full_content or "",  # 2026-08-08: 全文通道
                                    category="note",
                                    tags=[],
                                    cosine=sim,
                                    rrf_score=0.0,
                                    kind="note",
                                    created_at="",
                                )
        except PrefetchDeadlineExceeded:
            # A8 (G6B Slice B): record the lane timeout BEFORE the raise.
            # observation_notes handler — part of the vector lane family.
            try:
                _probe(trace, 'lane_finish', 'vector', timed_out=True, reason='deadline')
            except Exception:
                pass
            raise
        except Exception as e:
            # A7 (G6B Slice B): record the degradation in the trace
            # BEFORE the existing logger call.  This is the
            # observation_notes block's except — part of the vector
            # lane family.
            try:
                _probe(trace, 'error', 'lane=vector: ' + str(e)[:200])
            except Exception:
                pass
            logger.debug("observation_notes recall failed: %s", _safe_err(e)[:200])

    # ── Vector lane summary (union of vec_ids + msg_ids + eff_ids + yin_ids + notes_ids) ──
    # Yin and notes are vector-family retrieval but land in the 'vector' lane
    # by documented mapping (recall_v2 has a fixed 5-lane API; we map real
    # retrieval onto those five lanes rather than fabricating new ones).
    try:
        _vec_union_ids: list[str] = []
        for _sid in (vec_ids | msg_ids | eff_ids | yin_ids | notes_ids):
            try:
                _h = hits.get(_sid)
                if _h is None:
                    continue
                _k = str(getattr(_h, "kind", "") or "")
            except Exception:
                _k = ""
            if _k == "qa":
                continue
            _vec_union_ids.append(_sid)
    except Exception:
        _vec_union_ids = []
    _probe(trace, 'lane_candidates', 'vector', _vec_union_ids)
    if (
        include_card_vector
        or include_message_vector
        or include_effective
        or include_yin
        or include_notes
    ):
        _probe(trace, 'lane_finish', 'vector', candidate_count=len(_vec_union_ids))
    else:
        _probe(
            trace,
            'lane_finish',
            'vector',
            skipped=True,
            reason='all_vector_paths_disabled',
        )

    # PG fail detection
    if deadline is not None:
        deadline.check(context="before RRF")
    pg_fail = False
    if pg_was_connected and not _pg_is_connected(pg, deadline):
        pg_fail = True
        _probe(trace, 'warn', 'pg_fail=True')

    # RRF — keyword 权重衰减至 0.5, 其他路径 1.0, topic 权重最高 2.0
    # P2: keyword 路径权重降半, 防 ILIKE 海啸淹没向量+有效池的语义信号
    # 2026-08-09: QA 原文精确命中 (kind='qa') 是强信号 — keyword 权重 1.0 (不被降半),
    # 用户明确要找的原文 (charity race) 必须排前面, 不能淹没在语义路径里
    paths_count = sum(bool(x) for x in (kw_ids, vec_ids, msg_ids, eff_ids, topic_ids, yin_ids, notes_ids))
    if paths_count == 0:
        return [], False

    KW_RRF_WEIGHT = 0.5
    VEC_RRF_WEIGHT = 1.0
    TOPIC_RRF_WEIGHT = 2.0
    QA_RRF_WEIGHT = 1.0  # 原文精确命中权重 (高于降半的 keyword, 低于 topic)

    kw_rank = {sid: i + 1 for i, sid in enumerate(sorted(kw_ids, key=lambda s: hits[s].rrf_score, reverse=True))} if kw_ids else {}
    qa_kw_rank = {sid: i + 1 for i, sid in enumerate(sorted([s for s in kw_ids if hits[s].kind == 'qa'],
                                                           key=lambda s: hits[s].rrf_score, reverse=True))} if kw_ids else {}
    vec_rank = {sid: i + 1 for i, sid in enumerate(sorted([s for s in vec_ids if hits[s].kind != 'qa'],
                                                           key=lambda s: hits[s].cosine, reverse=True))} if vec_ids else {}
    # 2026-08-12 A-2: vec_rank 排除 QA — QA 向量命中只走 qa_vec_rank 一条路径, 消除双计分
    # (之前 QA 同时参与 vec_rank(1.0) + qa_vec_rank(1.0) = 无关语义 QA 双倍虚高 0.022-0.025,
    # 把精确命中压出 top30; 归因 A 类 55.5% 的次要根因)
    # 2026-08-09: QA 向量命中单独路径 — QA 原文向量命中 (cos>=0.35) 是精确事实信号,
    # 之前 QA 只在 qa_kw_rank(1.0)+vec_rank(1.0) 混合, topic 有 topic_rank(2.0) 单独路径
    # → topic 天生多一条 2.0 路径, QA 原文永远被压出 limit (LGBTQ 查询 qa_231 cos=0.696 排 14 名)
    qa_vec_rank = {sid: i + 1 for i, sid in enumerate(sorted([s for s in vec_ids if hits[s].kind == 'qa'],
                                                            key=lambda s: hits[s].cosine, reverse=True))} if vec_ids else {}
    msg_rank = {sid: i + 1 for i, sid in enumerate(sorted(msg_ids, key=lambda s: hits[s].cosine, reverse=True))} if msg_ids else {}
    eff_rank = {sid: i + 1 for i, sid in enumerate(sorted(eff_ids, key=lambda s: hits[s].cosine, reverse=True))} if eff_ids else {}
    topic_rank = {sid: i + 1 for i, sid in enumerate(sorted(topic_ids, key=lambda s: hits[s].cosine, reverse=True))}
    yin_rank = {sid: i + 1 for i, sid in enumerate(sorted(yin_ids, key=lambda s: hits[s].cosine, reverse=True))}
    notes_rank = {sid: i + 1 for i, sid in enumerate(sorted(notes_ids, key=lambda s: hits[s].cosine, reverse=True))}

    paths = []
    if kw_rank:
        paths.append((kw_rank, KW_RRF_WEIGHT))
    if qa_kw_rank:
        paths.append((qa_kw_rank, QA_RRF_WEIGHT))
    if qa_vec_rank:
        paths.append((qa_vec_rank, QA_RRF_WEIGHT))
    if vec_rank:
        paths.append((vec_rank, VEC_RRF_WEIGHT))
    if msg_rank:
        paths.append((msg_rank, VEC_RRF_WEIGHT))
    if eff_rank:
        paths.append((eff_rank, VEC_RRF_WEIGHT))
    if topic_rank:
        paths.append((topic_rank, TOPIC_RRF_WEIGHT))
    if yin_rank:
        paths.append((yin_rank, VEC_RRF_WEIGHT))
    if notes_rank:
        paths.append((notes_rank, VEC_RRF_WEIGHT))

    scored = []
    for sid, hit in hits.items():
        rrf = 0.0
        for rank_map, w in paths:
            if sid in rank_map:
                rrf += w / (K + rank_map[sid])
        hit.rrf_score = round(rrf, 6)
        # Apply exponential time decay — half_life from config (P3) or default
        # 2026-08-09: 原文精确命中 (kind='qa') 跳过衰减 —
        # 关键词精确匹配 = 用户明确要找的信号, 时间衰减是语义召回(近的优先)的工具,
        # 不该惩罚"用户明确要的原文" (否则 3 年前的关键信息永远召不回)
        if hit.created_at and hit.kind != 'qa':
            try:
                cat_str = hit.created_at.replace("Z", "+00:00")
                card_time = datetime.fromisoformat(cat_str)
                age_days = max(0, (datetime.now() - card_time).days)
                decay = math.exp(-age_days / half_life)
                hit.rrf_score = round(hit.rrf_score * max(decay, 0.05), 6)
                # A2 (G6B Slice B): record the temporal half-life decay
                # in the trace when a real decay was applied.  Only emits
                # when the ``try`` succeeded (no ValueError / TypeError).
                try:
                    _probe(
                        trace,
                        'score',
                        hit.source_id,
                        'temporal',
                        hit.rrf_score,
                        operation='half_life_decay',
                        decay=round(decay, 6),
                        age_days=age_days,
                    )
                except Exception:
                    pass
            except (ValueError, TypeError):
                pass
        scored.append(hit)

    # A1 (G6B Slice B): emit a fusion (rrf) score record for every scored
    # hit BEFORE the rare-bonus block.  Guarded so a faulty sink can never
    # break retrieval — the existing `_probe` helper already swallows
    # exceptions.
    try:
        for _hit in scored:
            _probe(trace, 'score', _hit.source_id, 'fusion', _hit.rrf_score, operation='rrf')
    except Exception:
        pass

    # 2026-08-09: 稀有词精确命中后置加分 — _qa_score +2.0 只在关键词路径内排序生效,
    # RRF 排名倒数加权后贡献仅 ~0.016, 精确命中仍会被其他路径压出 top (museum 案例实测 17 名)
    # 后置加分确保"全库唯一命中"直接进结果; 幅度 0.08 (远超正常 0.01-0.05 分差, 但非霸榜级)
    # 2026-08-09 (C-3 修正): 原 0.15/0.08 与 _qa_score +2.0 叠加到 2.15 永久霸榜 — 统一为 0.08 封顶
    if _rare_bonus:
        for hit in scored:
            if hit.kind == 'qa' and hit.source_id in _rare_bonus:
                hit.rrf_score = round(hit.rrf_score + min(_rare_bonus[hit.source_id], 0.08), 6)
                # A3 (G6B Slice B): record that the rare-bonus adjustment
                # was applied to this hit's rrf_score.  Guarded so a faulty
                # sink cannot change the score path.
                try:
                    _probe(trace, 'event', hit.source_id, 'SCORED', 'rare_bonus')
                except Exception:
                    pass

    # 2026-08-12 A-1: 精确命中保送 — 极稀有(min_freq≤3) 或 ≥2 低频词共现 (词频≤库量×1%) = 强精确信号
    # 根因: RRF 倒数加权稀释精确命中 (charity race 证据 rrf=0.0169 排 30 名, limit=30 出局;
    # 无关语义 QA 双计分 0.022-0.025 反而靠前)。保送加分 0.5 (远超 0.01-0.03 普通分差),
    # 上限 2 条防霸榜+防挤占 (v2 实测: 主题词案例 children 3 条保送占 top5, 语义命中只剩 2 位);
    # 不替代 rerank, 只保证进 rerank 输入。
    # v2 收紧 (全量评测 36.4% vs 37.9% 回归, cat1 -15pt/cat3 -12pt):
    # v1 条件"min_freq≤1%"太宽 — when 类查询中等频率动词 (give/go) 单词命中也保送,
    # 3-5 条保送占满注入 top5, 语义命中全被挤 (prefetch limit=5)。v2 需极稀有或多低频共现。
    try:
        if _qa_boost_sig:
            _boost_thr = max(3, int(_total_qa * 0.01))
            _boost_sids = [sid for sid, (mf, lc) in _qa_boost_sig.items() if mf <= 3 or lc >= 2]
            _boost_sids.sort(key=lambda s: hits[s].rrf_score, reverse=True)
            for _sid in _boost_sids[:2]:
                for _hit in scored:
                    if _hit.kind == 'qa' and _hit.source_id == _sid:
                        _hit.rrf_score = round(_hit.rrf_score + 0.5, 6)
                        # A4 (G6B Slice B): record that the exact-QA
                        # boost (+0.5) was applied to this hit.
                        try:
                            _probe(trace, 'event', _hit.source_id, 'SCORED', 'exact_qa_boost')
                        except Exception:
                            pass
                        break
    except Exception:
        pass

    scored.sort(key=lambda x: x.rrf_score, reverse=True)

    # Optional rerank
    ranked = scored
    if rerank_top_n and len(ranked) > rerank_top_n and rerank_cfg:
        if deadline is None:
            ranked = _rerank(query, ranked, top_n=rerank_top_n, rerank_cfg=rerank_cfg)
        else:
            ranked = _rerank(
                query,
                ranked,
                top_n=rerank_top_n,
                rerank_cfg=rerank_cfg,
                deadline=deadline,
            )
        # A5 (G6B Slice B): when ``_rerank`` actually runs, emit a
        # RERANKED event for every hit in the reranked list.  Guarded
        # so a faulty sink cannot change the reranked order path.
        try:
            for _h in ranked:
                _probe(trace, 'event', _h.source_id, 'RERANKED')
        except Exception:
            pass
    else:
        # A5 (G6B Slice B): rerank was skipped.  Emit a warn probe with
        # the SINGLE real reason — derived only from the real condition.
        # Order of the original conjunct matters: try the most specific
        # first so the recorded reason matches the boolean the caller saw.
        try:
            if not rerank_cfg:
                _skip_reason = 'no_rerank_cfg'
            elif not rerank_top_n:
                _skip_reason = 'rerank_top_n_disabled'
            else:
                # len(ranked) <= rerank_top_n
                _skip_reason = 'len_le_rerank_top_n'
            _probe(trace, 'warn', 'rerank skipped: ' + _skip_reason)
        except Exception:
            pass

    final = ranked[:limit]
    # A6 (G6B Slice B): selection stage — record each hit inside the
    # final limit as ``select``, and each hit outside the limit as
    # ``drop / OUTSIDE_LIMIT / selection``.  Guarded so a faulty sink
    # cannot change the returned hits list.
    try:
        for _h in final:
            _probe(trace, 'select', _h.source_id)
    except Exception:
        pass
    try:
        for _h in ranked[limit:]:
            _probe(trace, 'drop', _h.source_id, 'OUTSIDE_LIMIT', stage='selection')
    except Exception:
        pass

    # TKG: batch-fetch associated facts (card only)
    if pg:
        card_ids = [h.source_id for h in final if h.kind == "card"]
        if card_ids:
            try:
                facts_map = _fetch_facts(card_ids, pg)
                for h in final:
                    h.facts = facts_map.get(h.source_id, [])
            except PrefetchDeadlineExceeded:
                raise
            except Exception as e:
                logger.warning("_fetch_facts failed (skipped): %s", _safe_err(e)[:200])

    return final, pg_fail

# -------------------------------------------------------------------------
# chain_recall -- A-path three-layer diffusion recall (v3 memory v4 plan)
#
# Design intent:
#   Path A -- effective-pool-segment (anchor) -> bei (diffusion) -> trace
#   (raw dialogue). Three-layer chained recall. Outperforms flat RRF ranking
#   by one extra semantic depth. Verified in 6/30 experiment: recall@5
#   improved over flat RRF.
#     1. query -> v3_effective ANN -> top-1 anchor
#     2. If anchor cosine >= 0.5: use query embedding to search v3_cards top-5
#        (experiment: query embedding beats anchor embedding on cards by ~8%).
#     3. For each card, use its own embedding to search v3_messages top-2.
#     4. If anchor cosine < 0.5: fallback to Path B (multi-path recall + RRF).
#     5. If PG unavailable: fallback to file keyword search.
#
# Output shape: {query, type: chain|fallback, anchor, cards, traces}
#   - type=chain    -> Path A succeeded
#   - type=fallback -> Path B (fallback), cards is B-path result
#   - type=fallback + warning=pg_unavailable -> file keyword fallback
# -------------------------------------------------------------------------

# Anchor cosine threshold -- below this, anchor is untrusted; go to Path B
_CHAIN_ANCHOR_THRESHOLD = 0.5
# Anchor count -- chain mode takes only top-1
_CHAIN_ANCHOR_TOP_N = 1
# Card count -- how many cards per anchor
_CHAIN_CARDS_TOP_N = 5
# Trace count -- how many raw dialogues per card
_CHAIN_TRACES_PER_CARD = 2


def _resolve_half_life(config, default: int = 30) -> int:
    """从 V3Config / dict 中解析半衰期; 缺省回落到模块默认.

    接受:
      - V3Config: ``cfg.half_life``
      - dict 半衰期: ``half_life`` (P3 命名) 或 ``time_decay.half_life_days``
      - ``None``: 用 ``default``
    """
    if config is None:
        return int(default)
    # V3Config dataclass
    if hasattr(config, "half_life") and not isinstance(config, dict):
        try:
            v = int(config.half_life)
            return v if v > 0 else int(default)
        except (TypeError, ValueError):
            return int(default)
    if isinstance(config, dict):
        v = config.get("half_life")
        if v is None:
            td = config.get("time_decay") or {}
            v = td.get("half_life_days")
        try:
            iv = int(v) if v is not None else 0
            return iv if iv > 0 else int(default)
        except (TypeError, ValueError):
            return int(default)
    return int(default)


def _trim(s, n=200):
    """Trim preview string, strip newlines"""
    if not s:
        return ""
    s = s.replace(chr(13) + chr(10), chr(10)).replace(chr(10), " ")
    return s[:n].strip()


def _chain_search_by_embedding(pg, emb, table, kind, limit):
    """Search the current chain tables and return compact vector hits.

    ``topics`` supplies the anchor/card layer and ``topic_entries`` supplies
    the QA trace layer. Retired PG tables are intentionally not dispatched.
    """
    if not pg or not emb:
        return []
    if table == "topics":
        # topics 是观察者链的唯一锚点/主题卡索引
        sql = (
            "SELECT topic_id, COALESCE(title, %s), "
            " LEFT(COALESCE(summary, '') || COALESCE(body, ''), 500),"
            " 1 - (embedding <=> %s::vector) AS cosine"
            " FROM topics WHERE status='active' AND embedding IS NOT NULL"
            " ORDER BY embedding <=> %s::vector LIMIT %s"
        )
    elif table == "topic_entries":
        # 2026-08-06: chain traces 用 topic_entries (QA 原文 + embedding)
        sql = (
            "SELECT topic_id, LEFT(COALESCE(question, %s), 200),"
            " LEFT(COALESCE(question, '') || ' ' || COALESCE(answer, ''), 500),"
            " 1 - (embedding <=> %s::vector) AS cosine"
            " FROM topic_entries WHERE embedding IS NOT NULL"
            " ORDER BY embedding <=> %s::vector LIMIT %s"
        )
    else:
        return []
    emb_str = "[" + ",".join(str(x) for x in emb) + "]"
    try:
        with _lease_pg_connection(pg) as conn:
            if not conn:
                return []
            with conn.cursor() as cur:
                cur.execute(sql, (chr(34), emb_str, emb_str, limit))
                rows = cur.fetchall()
    except PrefetchDeadlineExceeded:
        raise
    except Exception as e:
        logger.warning("chain_recall: %s search failed: %s", table, _safe_err(e)[:200])
        return []
    out = []
    for row in rows:
        # topics / topic_entries use the same compact result shape.
        out.append({
            "source_id": row[0],
            "title": row[1],
            "content_preview": _trim(row[2], 300),
            "cosine": float(row[3]) if row[3] is not None else 0.0,
            "kind": kind,
        })
    return out


def _get_topic_embedding(pg, topic_id):
    """Fetch a topics row embedding -- used by the chain traces stage (2026-08-06)."""
    if pg is None:
        return None
    try:
        with _lease_pg_connection(pg) as conn:
            if not conn:
                return None
            with conn.cursor() as cur:
                cur.execute("SELECT embedding::text FROM topics WHERE topic_id = %s", (topic_id,))
                row = cur.fetchone()
                if not row or not row[0]:
                    return None
                emb_str = row[0].strip("[]")
                return [float(x) for x in emb_str.split(",") if x.strip()]
    except PrefetchDeadlineExceeded:
        raise
    except Exception as e:
        logger.debug("_get_topic_embedding(%s) failed: %s", topic_id, _safe_err(e)[:100])
        return None


def chain_recall(query, pg=None, q_emb=None,
                 anchor_threshold=_CHAIN_ANCHOR_THRESHOLD,
                 cards_top_n=_CHAIN_CARDS_TOP_N,
                 traces_per_card=_CHAIN_TRACES_PER_CARD,
                 config=None, core=None, *, deadline=None):
    """Path A three-layer diffusion recall -- segment -> bei -> trace.

    Args:
        query: user query text
        pg: PgEmbedStore instance (None triggers file fallback)
        q_emb: query embedding (None triggers file fallback)
        anchor_threshold: anchor cosine threshold; below this fallback to B
        cards_top_n: cards per anchor
        traces_per_card: raw dialogues per card
        config: 可选 V3Config / dict
        core: 可选 V3Core 实例 — 透传给 _chain_fallback_b → recall_pool,
              用于复用 core._topic_recall 守护线程预热的共享缓存,
              避免每次召回都重新加载全部 topics.

    Returns:
        dict {query, type, anchor, cards, traces, [warning], [error]}
    """
    if not query or not query.strip():
        return {"query": query or "", "type": "fallback", "error": "empty query", "cards": []}

    deadline = coerce_deadline(deadline)
    if deadline is not None:
        deadline.check(context="chain_recall")
        pg = bind_store_deadline(pg, deadline)

    # Path 1: PG + embedding available -> try Path A
    if pg and _pg_is_connected(pg, deadline) and q_emb:
        try:
            # Step 1: topics 锚点 top-1 (2026-08-06: 观察者 v2 后 topics 是唯一索引, 旧 v3_effective 退役)
            eff_hits = _chain_search_by_embedding(
                pg, q_emb, "topics", "topic", _CHAIN_ANCHOR_TOP_N
            )
            if not eff_hits:
                return _chain_fallback_b(query, pg, q_emb, reason="no anchor in v3_effective", config=config, core=core, deadline=deadline)
            anchor = eff_hits[0]
            if anchor.get("cosine", 0.0) < anchor_threshold:
                return _chain_fallback_b(
                    query, pg, q_emb,
                    reason="anchor cosine {:.3f} < {:.2f}".format(
                        anchor.get("cosine", 0.0), anchor_threshold
                    ),
                    config=config,
                    core=core,
                    deadline=deadline,
                )

            # Step 1.5: 从 anchor 解析 premise 标注的卡片 ID (e1.py 注入的 HTML 注释)
            # premise 精确匹配 = 锚定"印里明引用的卡一定召回", cosine=1.0
            anchor_content = anchor.get("content_preview", "") or ""
            premise_ids = _parse_premise_ids(anchor_content)
            premise_cards = _chain_premise_lookup(pg, premise_ids, cards_top_n, deadline=deadline) if premise_ids else []

            # Step 2: query embedding -> topics top-N (premise + vector 双路径)
            # 双路径: premise 命中的卡必在前; 向量召回填补剩余槽位.
            # 如果 premise 命中 2 张, 这 2 张排在最前, 最多 3 张由向量搜索补齐.
            # 若 premise 未命中, 行为与原来完全一致 (纯向量搜索).
            card_hits = _chain_search_by_embedding(
                pg, q_emb, "topics", "topic", cards_top_n
            )
            if premise_cards:
                existing_ids = {c["source_id"] for c in premise_cards}
                for vc in card_hits:
                    if vc["source_id"] not in existing_ids:
                        premise_cards.append(vc)
                card_hits = premise_cards[:cards_top_n]
            if not card_hits:
                return _chain_fallback_b(query, pg, q_emb, reason="no cards for anchor", config=config, core=core, deadline=deadline)

            # Step 3: per-card embedding -> topic_entries top-K (QA 原文)
            traces = {}
            for card in card_hits:
                card_emb = _get_topic_embedding(pg, card["source_id"])
                if not card_emb:
                    continue
                msg_hits = _chain_search_by_embedding(
                    pg, card_emb, "topic_entries", "entry", traces_per_card
                )
                if msg_hits:
                    traces[card["source_id"]] = msg_hits

            return {
                "query": query,
                "type": "chain",
                "anchor": {
                    "source_id": anchor["source_id"],
                    "title": anchor.get("title", ""),
                    "cosine": round(anchor.get("cosine", 0.0), 4),
                    "content_preview": anchor.get("content_preview", ""),
                },
                "cards": [
                    {
                        "source_id": c["source_id"],
                        "title": c.get("title", ""),
                        "cosine": round(c.get("cosine", 0.0), 4),
                        "content_preview": c.get("content_preview", ""),
                        "matched_by": c.get("matched_by", "vector"),
                    }
                    for c in card_hits
                ],
                "traces": traces,
            }
        except PrefetchDeadlineExceeded:
            raise
        except Exception as e:
            logger.warning("chain_recall: A path failed, fallback to B: %s", _safe_err(e)[:200])
            return _chain_fallback_b(query, pg, q_emb, reason="A path error: " + _safe_err(e)[:100], config=config, core=core, deadline=deadline)

    # Path 2: PG unavailable / no embedding -> file keyword fallback
    return _chain_fallback_file(query, pg, deadline=deadline)


def _chain_fallback_b(query, pg, q_emb, reason, config=None, core=None, *, deadline=None):
    """Path B fallback -- multi-path recall + RRF, wrapped as fallback shape

    core: 可选 V3Core 实例 — 透传给 recall_pool, 复用 _topic_recall 守护缓存.
    """
    try:
        hits, pg_fail = recall_pool(
            query=query,
            card_index=None,
            pg=pg,
            q_emb=q_emb,
            limit=10,
            pg_was_connected=_pg_is_connected(pg, deadline),
            config=config,
            core=core,
            deadline=deadline,
        )
    except PrefetchDeadlineExceeded:
        raise
    except Exception as e:
        return {
            "query": query,
            "type": "fallback",
            "warning": "b_path_failed",
            "error": _safe_err(e)[:200],
            "cards": [],
        }
    return {
        "query": query,
        "type": "fallback",
        "reason": reason,
        "pg_fail": pg_fail,
        "cards": [h.to_dict() for h in hits],
    }


# 文件扫描上限：兜底函数，PG 不可用时扫 file index。文件数可能几千，全扫耗时 5-6s。
# 优化：按 source_id 排序取前 FALLBACK_FILE_LIMIT 个（2000），覆盖绝大多数场景。
FALLBACK_FILE_LIMIT = 2000


def _chain_fallback_file(query, pg, *, deadline=None):
    """File keyword fallback -- when PG fully unavailable, scan file index by keyword.

    这是 prefetch 链路的最后兜底（PG 完全失败 → 降级到文件索引扫描）。
    实现：按 source_id 字典序取前 FALLBACK_FILE_LIMIT=2000 个文件，避免几千文件全扫拖死 prefetch。
    """
    try:
        from .card_store import DeepStore
        from .config import resolve_config
        from .tokenizer import build_query_tokens

        cfg = resolve_config()
        store = DeepStore(cfg)
        index = store.get_index()
        files = index.get("files", {}) if isinstance(index, dict) else {}

        query_terms = build_query_tokens(query)
        if not query_terms:
            query_terms = [t.lower() for t in query.strip().split() if len(t) > 1]
        bigrams = _cjk_bigrams(query)

        # 取前 FALLBACK_FILE_LIMIT 个文件（按 source_id 排序，稳定可复现）
        if len(files) > FALLBACK_FILE_LIMIT:
            items = sorted(files.items(), key=lambda kv: kv[0])[:FALLBACK_FILE_LIMIT]
            file_iter = dict(items).items()
        else:
            file_iter = files.items()

        scored = []
        for rel, meta in file_iter:
            if deadline is not None:
                deadline.check(context="chain file fallback")
            score = _keyword_score(rel, meta, query_terms)
            if bigrams:
                score += _cjk_bigram_score(meta, bigrams)
            if score > 0:
                scored.append({
                    "source_id": rel,
                    "title": meta.get("title", ""),
                    "category": meta.get("category", ""),
                    "content_preview": (meta.get("content_preview", "") or "")[:300],
                    "rrf_score": round(score, 4),
                    "kind": "card",
                })
        scored.sort(key=lambda x: -x["rrf_score"])
        scored = scored[:10]
    except PrefetchDeadlineExceeded:
        raise
    except Exception as e:
        return {
            "query": query,
            "type": "fallback",
            "warning": "file_keyword_failed",
            "error": _safe_err(e)[:200],
            "cards": [],
        }
    return {
        "query": query,
        "type": "fallback",
        "reason": "pg_unavailable",
        "warning": "pg_unavailable",
        "cards": scored,
    }

