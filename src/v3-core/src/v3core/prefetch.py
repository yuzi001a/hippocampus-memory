"""prefetch 引擎 — 召回 + context block 注入"""
from __future__ import annotations
import json
import logging
from typing import Any
from .types import DEFAULT_CATEGORIES
from ._deadline import PrefetchDeadlineExceeded, bind_store_deadline, coerce_deadline
from .recall_v2.contracts import DropReasonCode, CandidateEventType


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

logger = logging.getLogger("v3core.prefetch")


def _fetch_tail_qas(pg=None, state_path=None, limit: int = 5, deadline=None) -> list[tuple[str, str, str]]:
    """读取压缩游标之后尚未压缩的 QA 原文；任何失败都降级为空列表。"""
    try:
        deadline = coerce_deadline(deadline)
        if deadline is not None:
            deadline.check(context="tail QA")
            pg = bind_store_deadline(pg, deadline)
        from pathlib import Path
        from .pg_pool import DEFAULT_LEASE_TIMEOUT

        if state_path is None:
            # 2026-08-08 P0 隔离修复: 压缩状态文件改走数据目录, 源码树禁止写运行时状态
            try:
                from .config import _resolve_data_dir
                state_path = str(_resolve_data_dir() / "delta_run_state.json")
            except Exception:
                state_path = Path(__file__).resolve().parents[3] / "compression_engine" / "delta_run_state.json"
        state_file = Path(state_path)
        if not state_file.is_file():
            return []

        state = json.loads(state_file.read_text(encoding="utf-8"))
        last_compressed_id = int(state.get("last_compressed_id", 0))
        if pg is None:
            return []

        pool = getattr(pg, "pool", None)
        if pool is not None and hasattr(pg, "lease"):
            def _read_tail(conn):
                if conn is None:
                    return []
                with conn.cursor() as cur:
                    if deadline is not None:
                        deadline.check(context="tail QA SQL")
                    cur.execute(
                        "SELECT question, answer, session_id FROM qa_pairs "
                        "WHERE id > %s ORDER BY id LIMIT %s",
                        (last_compressed_id, limit),
                    )
                    return [
                        (str(q or ""), str(a or ""), str(sid or ""))
                        for q, a, sid in cur.fetchall()
                    ]

            lease_obj = pg.lease(timeout=DEFAULT_LEASE_TIMEOUT)
            # PgPool.lease returns PgLease; PgEmbedStore.lease returns a
            # contextmanager. Support both without changing the query.
            if hasattr(lease_obj, "connection"):
                try:
                    return _read_tail(lease_obj.connection)
                finally:
                    lease_obj.close()
            with lease_obj as conn:
                return _read_tail(getattr(conn, "connection", conn))

        # Legacy/offline store: retain its explicit raw-connection seam.
        conn = pg._connect() if hasattr(pg, "_connect") else None
        if conn is None:
            return []
        with conn.cursor() as cur:
            if deadline is not None:
                deadline.check(context="legacy tail QA SQL")
            cur.execute(
                "SELECT question, answer, session_id FROM qa_pairs "
                "WHERE id > %s ORDER BY id LIMIT %s",
                (last_compressed_id, limit),
            )
            return [
                (str(q or ""), str(a or ""), str(sid or ""))
                for q, a, sid in cur.fetchall()
            ]
    except PrefetchDeadlineExceeded:
        raise
    except Exception as exc:
        logger.warning("读取未压缩 QA 尾巴失败: %s", _safe_err(exc)[:200])
        return []


def _append_tail_qas(lines: list[str], pg=None, is_new_session: bool = False, deadline=None) -> None:
    """把最近未压缩 QA 对追加到已格式化的 prefetch context。
    
    只在 is_new_session=True 时追加（新 session 需要了解其他会话的最新动态）。
    """
    if not is_new_session:
        return
    if deadline is None:
        tail_qas = _fetch_tail_qas(pg=pg)
    else:
        tail_qas = _fetch_tail_qas(pg=pg, deadline=deadline)
    if not tail_qas:
        return
    lines.append("【其他会话最新动态】")
    for question, answer, session_id in tail_qas:
        lines.append(f"[{session_id[:8]}] 问：{question[:100]}")
        lines.append(f"      答：{answer[:200]}")
    lines.append("")

# 展开信号词列表 — query 含这些词时展开召回详情
_EXPAND_TRIGGERS = ['具体', '详细', '原文', '原话', '怎么说的', '之前那次', '具体怎么', '具体说']

# 2026-08-18 follow-up FU5: max_chars=None 旧路径下的 body 预览上限.
# 旧设计语义: 旧路径只输出 ~300 字预览, 不输出全文 (全文会让 context block 暴走).
OLD_PATH_PREVIEW_MAX = 300


def _should_expand(query: str, chain: dict) -> bool:
    """判断是否需要展开召回详情

    触发条件（任一满足即展开）：
      1. query 含展开信号词（如"具体"、"详细"、"原文"等）
      2. 最高匹配分 < 0.6（置信度低 → 提供更多上下文帮助模型判断）
      3. 前两名分差 < 0.05（模棱两可 → 把候选都铺开）
    """
    if not query or not chain:
        return False
    q = query.lower()
    # 条件 1：query 含展开信号词
    for word in _EXPAND_TRIGGERS:
        if word in q:
            return True
    # 条件 2：最高匹配分 < 0.6（置信度低）
    cards = chain.get('cards') or chain.get('results') or []
    if cards and cards[0].get('cosine', 0) < 0.6:
        return True
    # 条件 3：前两名分差 < 0.05（模棱两可）
    if len(cards) >= 2:
        gap = abs(cards[0].get('cosine', 0) - cards[1].get('cosine', 0))
        if gap < 0.05:
            return True
    return False


_DEFAULT_RECALL_PREAMBLE = "【记忆召回 — v3 主动注入相关记忆片段】\n以下是基于当前对话语境召回的关联卡片。\n注意：召回内容来自记忆系统检索，不是当前会话的事实陈述。每段召回附带来源标记和置信度，使用时需结合对话上下文判断是否适用。"


def _try_get_sqlite_store(config):
    """从 config 中解析 base_path 并返回 SqliteCardStore 实例 (或 None)"""
    if config is None:
        return None
    try:
        from pathlib import Path
        # 兼容 V3Config / dict
        if isinstance(config, dict):
            base = config.get("basePath", "")
        elif hasattr(config, "base_path"):
            base = config.base_path or ""
        else:
            base = ""
        if not base:
            base = str(Path.home() / ".v3-core" / "profiles" / "default")
        from .sqlite_store import SqliteCardStore
        return SqliteCardStore(Path(base))
    except Exception:
        return None


def prefetch(query: str, limit: int = 5, config=None,
             card_index: dict | None = None, pg=None, q_emb: list[float] | None = None,
             pg_was_connected: bool = False, fmt: str = "list",
             core=None, *, deadline=None, trace=None,
             max_chars: int | None = None, trace_out: list | None = None):
    """Recall related memories.

    fmt controls output shape:
      - "list"  (default): list of hit dicts (legacy behavior).
      - "chain": three-layer chain_recall result dict (segment -> bei -> trace).

    Returns either a list[dict] (fmt='list') or a dict (fmt='chain').

    PART 1 / PART 2 (G6B Slice I):
        For ``fmt == 'list'`` ONLY, the actual retrieval is now
        orchestrated by :class:`RecallV2Engine` (the V2 engine wraps
        the legacy ``recall_pool`` exactly once, builds a QueryPlan
        from the include_* flags the facade supplies, and strips
        ``max_chars`` from the forwarded payload when the legacy
        callable does not declare it).  ``fmt == 'chain'`` still goes
        through ``chain_recall`` byte-for-byte.

    PART 2 / PART 4:
        ``max_chars`` (optional keyword-only) is the caller's real
        injection budget — forwarded to the engine so
        ``QueryContext.max_chars`` and ``QueryPlan.max_chars`` reflect
        the caller's intent, but the engine strips it before invoking
        ``recall_pool`` (which does not declare ``max_chars``).
        ``trace_out`` (optional keyword-only list) is the internal
        channel the engine uses to publish the typed trace so
        ``prefetch_to_context_block`` can attach injection probes
        (``inject`` / ``drop CHAR_BUDGET``) to the SAME trace
        instance — one request, one trace.
    """
    deadline = coerce_deadline(deadline)
    if deadline is not None:
        deadline.check(context="prefetch")
    if not query or not query.strip():
        if fmt == "chain":
            return {"query": query or "", "type": "fallback", "error": "empty query", "cards": []}
        return []

    if fmt == "chain":
        # Path A three-layer chain -- falls back to B / file keyword internally
        from .recall_pool import chain_recall
        return chain_recall(query, pg=pg, q_emb=q_emb, config=config, core=core, deadline=deadline)

    # legacy list-shape
    cfg = config
    # 兼容 V3Config / dict / None
    if cfg is not None and not isinstance(cfg, dict) and hasattr(cfg, "prefetch"):
        pf_cfg_obj = cfg.prefetch
        dual = pf_cfg_obj.dual_path
        rrf_k = pf_cfg_obj.rrf_k
        include_msg_vec = pf_cfg_obj.include_message_vector
        rerank_cfg = cfg.rerank.to_legacy_dict() if cfg.rerank else {}
        cfg_for_pool = cfg  # 传入 V3Config 即可, recall_pool 内部读 half_life
    elif isinstance(cfg, dict):
        pf_cfg = cfg.get("prefetch", {}) or {}
        dual = pf_cfg.get("dual_path", True)
        rrf_k = pf_cfg.get("rrf_k", 60)
        include_msg_vec = pf_cfg.get("include_message_vector", True)
        rerank_cfg = cfg.get("storage", {}).get("rerank", {}) or {}
        cfg_for_pool = cfg
    else:
        dual = True
        rrf_k = 60
        include_msg_vec = True
        rerank_cfg = {}
        cfg_for_pool = None

    # PART 1 (G6B Slice I) — route the fmt='list' path through the
    # V2 engine.  The include_* flags below are what the LEGACY
    # production path has always passed to ``recall_pool``; the
    # engine sees them verbatim (no algorithm / constant change), and
    # uses them to build an effective QueryPlan so the typed plan
    # cannot disagree with the trace.
    include_keyword = bool(dual)
    include_card_vector = bool(dual)
    include_message_vector = bool(include_msg_vec)
    rerank_top_n = 30 if rerank_cfg.get("endpoint") else None
    sqlite_store = _try_get_sqlite_store(config)

    # Lazy import — keep recall_v2 out of the module-load graph so the
    # facade module stays cheap and there is no import-order surprise.
    from .recall_v2.engine import RecallV2Engine

    engine_kwargs: dict = {
        "card_index": card_index or {},
        "pg": pg,
        "q_emb": q_emb,
        "pg_was_connected": pg_was_connected,
        "core": core,
        "sqlite_store": sqlite_store,
        "limit": limit,
        "config": cfg_for_pool,
        "rerank_top_n": rerank_top_n,
        "rerank_cfg": rerank_cfg,
        "deadline": deadline,
        "include_keyword": include_keyword,
        "include_card_vector": include_card_vector,
        "include_message_vector": include_message_vector,
    }
    if max_chars is not None:
        engine_kwargs["max_chars"] = max_chars
    # PART 4 (G6B Slice I) — forward the trace sink + the trace_out
    # holder.  The engine uses the sink (``trace``) when supplied so
    # the existing trace contract still works; when the caller
    # instead supplies ``trace_out``, the engine appends its built
    # trace to that list so the facade can emit injection probes
    # against the SAME instance.
    if trace is not None:
        engine_kwargs["trace"] = trace
    if trace_out is not None:
        engine_kwargs["trace_out"] = trace_out

    engine = RecallV2Engine()
    result = engine.recall(query, **engine_kwargs)
    return [h.to_dict() for h in result.hits]


def _read_session_summary_block(source_id: str, config=None) -> str:
    """读 session_summary 卡片，提取 Memory Block 元数据行"""
    try:
        from pathlib import Path
        from .config import resolve_config
        cfg = config or resolve_config()
        # 兼容 V3Config / dict
        if isinstance(cfg, dict) or not hasattr(cfg, "base_path"):
            base = (cfg.get("basePath", "") if cfg else "") or str(Path.home() / ".v3-core" / "profiles" / "default")
        else:
            base = cfg.base_path or str(Path.home() / ".v3-core" / "profiles" / "default")
        fp = Path(base) / "cards" / source_id
        if not fp.is_file():
            return ""
        text = fp.read_text(encoding="utf-8", errors="replace")
        # 提取 frontmatter 字段
        import re as _re
        meta_lines = []
        for field in ("hermes_source", "hermes_model", "hermes_tokens", "hermes_cost"):
            m = _re.search(rf"^{field}: (.+)", text, _re.MULTILINE)
            if m:
                v = m.group(1).strip()
                if v:
                    meta_lines.append(f"{field.replace('hermes_', '')}: {v}")
        # 提取摘要预览（"## 摘要"之后的第一段非空行）
        preview = ""
        m = _re.search(r"## 摘要\n+((?:[^\n]+\n?)+)", text)
        if m:
            preview = m.group(1).strip()[:200]
        if not preview:
            # 兜底：取"# "开头之后的第一段
            m2 = _re.search(r"^# (.+)", text, _re.MULTILINE)
            if m2:
                preview = m2.group(1).strip()[:200]
        block = ""
        if meta_lines:
            block += "  " + " | ".join(meta_lines) + "\n"
        if preview:
            block += f"  preview: {preview}\n"
        return block
    except Exception:
        return ""


def prefetch_to_context_block(query: str, limit: int = 5, config: dict | None = None,
                              card_index: dict | None = None, pg=None,
                              q_emb: list[float] | None = None,
                              pg_was_connected: bool = False,
                              is_new_session: bool = False,
                              core=None,
                              max_chars: int | None = None,
                              *, deadline=None, trace=None) -> str:
    """召回 + 格式化为可注入 context block

    2026-08-05 整合: 单路化 — topics RRF 融合召回 (向量+关键词) 为主路径。
    A 路 (chain_recall 旧表链式, v3_effective/v3_cards/v3_messages) 退役 —
    观察者 v2 后 anchor 源不更新, 实际生产召回早已全走 B 路。
    链式溯源保留为按需工具 (v3_prefetch format=chain / v3_get_message_context)。

    2026-08-18 统一注入预算 (设计：docs/provider-injection-budget-design-20260818.md)：
      - 新增 max_chars 参数；非 None 时，函数返回的字符串长度 ≤ max_chars；
      - 选择策略：保留 preamble 与图谱提示等公共头，按"完整结果单元"贪心
        加入 — 一个结果单元放不下就跳过，不切字符串。

    Args:
        core: 可选 V3Core 实例 — 透传给 prefetch → recall_pool,
              复用 core._topic_recall 守护线程预热的共享缓存.
        max_chars: 可选 — 调用方注入预算上限 (默认 None 表示不限制)。
    """
    deadline = coerce_deadline(deadline)
    if deadline is not None:
        deadline.check(context="prefetch context")
    # B1 (G6B Slice B): local copy of ``recall_pool._probe`` so this
    # module does not import a private helper from recall_pool.  When
    # ``trace`` is None the helper is a pure no-op — byte-identical to
    # the pre-trace behaviour.
    #
    # SLICE J (G6B): the helper now resolves BOTH the sink-style
    # protocol (LegacySink.drop / LegacySink.inject) AND the trace-style
    # protocol (RecallTrace.record_drop / RecallTrace.inject) so the
    # injection-stage evidence ALWAYS lands on the typed
    # ``RecallTrace`` regardless of which facade the engine built.  The
    # sink-style call stays primary (zero behavior change for callers
    # that already attach a LegacySink); the trace-style call is a
    # silent fallback when the sink-style method is absent.  The
    # string drop code ``'CHAR_BUDGET'`` is converted to the contracts
    # enum (``DropReasonCode``); if conversion fails we return silently.
    # ``trace=None`` short-circuits BEFORE any method lookup — the
    # no-op path costs only one identity check.
    def _probe(trace, method, *args, **kwargs):
        if trace is None:
            return
        # ---- Sink-style primary: ``drop(sid, code, stage=stage)`` ----
        try:
            fn = getattr(trace, method, None)
            if fn is not None and callable(fn):
                fn(*args, **kwargs)
                return
        except Exception:
            return
        # ---- Trace-style fallback for drop / inject ----
        if method == "drop":
            try:
                record_drop = getattr(trace, "record_drop", None)
                if record_drop is None or not callable(record_drop):
                    return
                sid = args[0] if args else kwargs.get("candidate_id", "")
                code_str = args[1] if len(args) >= 2 else kwargs.get("code", "")
                stage = kwargs.get("stage", "")
                try:
                    code_enum = DropReasonCode(code_str)
                except (ValueError, KeyError):
                    return
                record_drop(sid, code_enum, stage=stage)
            except Exception:
                return
        elif method == "inject":
            try:
                inject_fn = getattr(trace, "inject", None)
                if inject_fn is not None and callable(inject_fn):
                    sid = args[0] if args else kwargs.get("candidate_id", "")
                    char_count = kwargs.get("char_count", 0)
                    if len(args) >= 2:
                        char_count = args[1]
                    inject_fn(sid, char_count=char_count)
                    return
            except Exception:
                pass
            try:
                record_event = getattr(trace, "record_candidate_event", None)
                if record_event is None or not callable(record_event):
                    return
                sid = args[0] if args else kwargs.get("candidate_id", "")
                record_event(sid, CandidateEventType.INJECTED)
            except Exception:
                return

    # B 路: 扁平 RRF 融合 (topics 向量 + 关键词) — 主路径
    prefetch_kwargs = {
        "pg_was_connected": pg_was_connected,
        "core": core,
    }
    if deadline is not None:
        prefetch_kwargs["deadline"] = deadline
    if trace is not None:
        prefetch_kwargs["trace"] = trace
    # PART 2 (G6B Slice I) — forward the caller's real injection
    # budget so ``QueryContext.max_chars`` and ``QueryPlan.max_chars``
    # reflect the caller's intent.  The engine strips it before
    # invoking ``recall_pool``.
    if max_chars is not None:
        prefetch_kwargs["max_chars"] = max_chars
    # PART 4 (G6B Slice I) — the facade needs the SAME trace the
    # engine built so injection probes land on the one trace for
    # this request.  Create a holder list, pass it down as
    # ``trace_out``, and after the call inspect the holder to find
    # the trace instance.  Falls back to the legacy ``trace=``
    # behaviour when no trace is available.
    trace_holder: list = []
    prefetch_kwargs["trace_out"] = trace_holder
    results = prefetch(query, limit, config, card_index, pg, q_emb,
                       **prefetch_kwargs)
    # Resolve the trace instance that the engine published (if any).
    # ``trace_holder`` is populated by the engine on the success
    # path; if the engine used the pre-supplied ``trace=`` instead,
    # ``trace_holder`` stays empty and we fall back to ``trace``.
    engine_trace = trace_holder[0] if trace_holder else None
    effective_trace = engine_trace if engine_trace is not None else trace
    if not results:
        lines: list[str] = []
        _append_tail_qas(lines, pg=pg, is_new_session=is_new_session, deadline=deadline)
        joined = "\n".join(lines)
        if max_chars is not None and len(joined) > max_chars:
            # 没有召回结果且追加尾巴本身就超预算 → 整体截断到 max_chars
            # 这是「连尾巴都放不下」的退化情况；保留第一段（preamble 之类）
            return ""
        return joined
    # 先取到 results，再判断是否展开 — _should_expand 需要 cards 才能算 cosine
    expand = _should_expand(query, {"cards": results})

    # ── 2026-08-18 预算选择：完整结果单元，贪心放入 ──
    # 公共头（preamble + 空行）始终保留；每个 r 作为一个完整单元（header + body），
    # 放不下就跳过该单元，不切字符串。
    _preamble_lines: list[str] = [_DEFAULT_RECALL_PREAMBLE, ""]
    # 2026-08-26: 图谱 related hint 已砍 (三轮消融不显著, 语义图与主召回结构性冗余)
    # — 见 docs/graph-ablation-20260825.md。graph_recall.py 保留离线分析用。

    # 每个 r 的"完整单元"行
    unit_lines_for = lambda r: _format_unit_lines(r, expand)

    # ── 2026-08-18 follow-up FU4 / FU5 修正 ──
    # - FU5: 旧路径 (max_chars=None) 不展开仍要保持 ~300 字预览语义;
    #   不应输出完整 body. 用 OLD_PATH_PREVIEW_MAX (=300) 截 body, 加省略号.
    # - FU4: 预算路径下, _append_tail_qas 必须受 budget 约束 — 完整尾巴
    #   > 剩余预算就整体跳过, 不能无条件 append 后造成超额.

    def _render_result_block(r: dict) -> list[str]:
        """单条结果格式化行为, 旧路径加 FU5 预览截断."""
        if max_chars is None:
            # 旧路径: 即使 max_chars=None, body 也按预览语义截断 (~300 字)
            # 避免普通 context block 暴走; 完整召回是 recall_path 自己的事.
            sid = r.get("source_id", "")
            title = r.get("title", "")
            cat = r.get("category", "")
            score = r.get("rrf_score", 0)
            full = r.get("content", "") or r.get("content_preview", "")
            block = [f"- {sid} | {title} [{cat}] (score={score:.3f})"]
            if full:
                if len(full) > OLD_PATH_PREVIEW_MAX:
                    preview = full[:OLD_PATH_PREVIEW_MAX].rstrip() + "…"
                    block.append(f"  {preview}")
                else:
                    block.append(f"  {full}")
            return block
        return unit_lines_for(r)

    if max_chars is None:
        # 旧路径：保留完全兼容的输出 (含 FU5 预览语义)
        lines = list(_preamble_lines)
        for r in results:
            _unit_lines = _render_result_block(r)
            lines.extend(_unit_lines)
            lines.append("")
            # B2 (G6B Slice B): every rendered result in the legacy
            # max_chars=None path is unconditionally injected.  Emit an
            # inject probe with the joined unit-text length.  The
            # source_id MUST be the result dict's own source_id value.
            try:
                _joined_unit = "\n".join(_unit_lines)
                _probe(effective_trace, 'inject', r.get("source_id", ""),
                       char_count=len(_joined_unit))
            except Exception:
                pass
        _append_tail_qas(lines, pg=pg, is_new_session=is_new_session, deadline=deadline)
        return "\n".join(lines)

    # ── 2026-08-18 统一预算：以最终 join 后的字符串为唯一计费真值 ──
    # 不再用“裸 unit 长度 + overhead”推算；公共头、空行、tail
    # 都必须和最终返回值使用同一套换行规则计算。
    def _compose_units(unit_lines_list: list[list[str]]) -> list[str]:
        """按最终输出顺序组装行；不修改任何完整 unit。"""
        if not unit_lines_list:
            return list(_preamble_lines)

        composed = list(_preamble_lines)
        for unit_lines in unit_lines_list:
            composed.extend(unit_lines)
            composed.append("")
        return composed

    selected_units: list[list[str]] = []
    base_lines = _compose_units([])
    if len("\n".join(base_lines)) > max_chars:
        # 公共头本身就超过预算；不切 preamble/hint，也不返回孤儿 unit。
        return ""

    for r in results:
        unit_lines = _render_result_block(r)
        candidate_units = selected_units + [unit_lines]
        candidate_lines = _compose_units(candidate_units)
        if len("\n".join(candidate_lines)) > max_chars:
            # 完整单元放不下 → 跳过并继续尝试后续候选。
            logger.debug(
                "prefetch_to_context_block: 跳过超额结果单元 (unit_len=%d, remain=%d)",
                len("\n".join(unit_lines)),
                max_chars - len("\n".join(_compose_units(selected_units))),
            )
            # B2 (G6B Slice B): record the budget-driven skip on the
            # trace.  source_id MUST be the result dict's own value.
            try:
                _probe(effective_trace, 'drop', r.get("source_id", ""),
                       'CHAR_BUDGET', stage='injection')
            except Exception:
                pass
            continue
        selected_units.append(unit_lines)
        # B2 (G6B Slice B): record the successful injection with the
        # newline-joined unit-text length (the lines actually appended).
        try:
            _joined_unit = "\n".join(unit_lines)
            _probe(effective_trace, 'inject', r.get("source_id", ""),
                   char_count=len(_joined_unit))
        except Exception:
            pass

    lines = _compose_units(selected_units)

    # tail 是完整块；能放下就追加，放不下整体跳过。
    tail_lines: list[str] = []
    try:
        _append_tail_qas(tail_lines, pg=pg, is_new_session=is_new_session, deadline=deadline)
    except PrefetchDeadlineExceeded:
        raise
    except Exception:
        tail_lines = []
    if tail_lines and len("\n".join(lines + tail_lines)) <= max_chars:
        lines.extend(tail_lines)
    elif tail_lines:
        logger.debug(
            "prefetch_to_context_block: 跳过完整尾巴块 (tail_len=%d, remain=%d)",
            len("\n".join(tail_lines)),
            max_chars - len("\n".join(lines)),
        )

    return "\n".join(lines)


def _format_unit_lines(r: dict, expand: bool) -> list[str]:
    """格式化单条召回结果为完整单元行（header + body）。

    2026-08-18 统一预算：body 不再做字符串切片 — 完整单元放不下由调用方跳过。
    """
    sid = r.get("source_id", "")
    title = r.get("title", "")
    cat = r.get("category", "")
    score = r.get("rrf_score", 0)
    # 2026-08-08: 全文通道 — content 优先(召回层已查全文), 回退 content_preview
    full = r.get("content", "") or r.get("content_preview", "")
    lines = [f"- {sid} | {title} [{cat}] (score={score:.3f})"]
    if full:
        # 2026-08-18: 不再做 [:preview_max] 切字符串；
        # 预算选择由 build_context / prefetch_to_context_block 的 max_chars 控制，
        # 完整单元放不下 → 跳过该单元。
        if expand:
            lines.append(f"  {full}")
        else:
            # 展开判断由 _should_expand 决定；不展开仍给全文（设计: 主题卡/印
            # 3500-16000 字，召回价值所在）— 预算由调用方控。
            lines.append(f"  {full}")
    return lines

def format_prefetch(results, fmt: str = "json") -> str:
    """Format output: json / context / ids / chain

    For fmt='chain' results is the chain_recall dict (not a list).
    """
    if fmt == "chain":
        return json.dumps(results, ensure_ascii=False, indent=2)
    if fmt == "ids":
        return json.dumps([r.get("source_id", "") for r in results], ensure_ascii=False)
    if fmt == "context":
        lines = []
        for r in results:
            lines.append(f"- {r.get('source_id', '')} | {r.get('title', '')}")
        return chr(10).join(lines)
    return json.dumps(results, ensure_ascii=False, indent=2)