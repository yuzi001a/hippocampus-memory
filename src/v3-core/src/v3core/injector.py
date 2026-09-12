"""MemoryInjector — 统一记忆注入器

合并 prefetch（事实回忆）+ identity（人格锚点）两条路径，
统一回答"当前场景 agent 最需要知道什么"。
"""

from __future__ import annotations
import inspect
import logging
from typing import Any
from ._deadline import DeadlinePoolView, PrefetchDeadlineExceeded, coerce_deadline

logger = logging.getLogger("v3core.injector")

# 注入策略常量
STRATEGY_IDENTITY_FIRST = "identity_first"
STRATEGY_RECALL_FIRST = "recall_first"
STRATEGY_BALANCED = "balanced"
STRATEGY_BUDGET = "budget"
VALID_STRATEGIES = (
    STRATEGY_IDENTITY_FIRST,
    STRATEGY_RECALL_FIRST,
    STRATEGY_BALANCED,
    STRATEGY_BUDGET,
)

# 默认注入预算（与 inject.max_chars 配置同步；统一入口最终返回值的总预算）
DEFAULT_MAX_CHARS = 10000


def _read_max_chars(core) -> int:
    """读取 core 注入预算 inject.max_chars — 兼容 dict / V3Config / 测试 fake。

    设计：配置缺失 / 异常时回退到 DEFAULT_MAX_CHARS（设计默认值 10000）。
    """
    try:
        cfg = getattr(core, "_config", None) or getattr(core, "config", None)
        if cfg is None:
            return DEFAULT_MAX_CHARS
        inject_cfg = None
        if isinstance(cfg, dict):
            inject_cfg = cfg.get("inject")
        elif hasattr(cfg, "inject"):
            inject_cfg = cfg.inject
        elif hasattr(cfg, "get"):
            try:
                inject_cfg = cfg.get("inject")
            except Exception:
                inject_cfg = None
        if not inject_cfg:
            return DEFAULT_MAX_CHARS
        if isinstance(inject_cfg, dict):
            v = inject_cfg.get("max_chars")
        elif hasattr(inject_cfg, "max_chars"):
            v = inject_cfg.max_chars
        else:
            v = None
        if v is None:
            return DEFAULT_MAX_CHARS
        return int(v)
    except Exception:
        return DEFAULT_MAX_CHARS


class MemoryInjector:
    """统一注入器

    strategy 控制注入行为：
      - "identity_first": 优先注入身份锚点（印），事实回忆靠后
      - "recall_first": 优先注入事实回忆
      - "balanced": 平衡两条路径
      - "budget": 按 token 预算分配（未来）

    去重：同一 session 内, 身份锚点内容若未变化则跳过（避免每次重复推送）。
    session 切换时调用 reset() 重置去重缓存。

    2026-08-18 统一注入与总预算（设计：docs/provider-injection-budget-design-20260818.md）：
      - build_context(query, session_id="") 统一组装最终文本；
      - 总预算由 inject.max_chars 控制，作用于最终返回值；
      - 预算选择只接受完整单元：完整 identity / 完整 situation / 完整
        note / 完整 tail QA / 完整 topic / 完整 recall block；
      - 一个完整单元超过剩余预算 → 整体跳过，不切字符串；
      - 首轮召回（v3core.observer.recall_for_new_session）按 session 幂等：
        同一 session 只调一次，reset 后可再调；
      - 每个 query recall 仍每次都走，不做 session 级缓存（不破坏现有
        query 刷新语义）。

    2026-08-18 follow-up FU2 修复:
      - 首轮每个完整 unit 在最终组装阶段独立参与预算竞争, 不再先拼成一个
        block 后作为一个 part (避免大 topic 挤掉 note);
      - 首轮 header 必须搭配首个 unit 原子评估 (避免 orphan header);
      - 至少一个首轮内容 unit 已进最终 chosen 才标记 _first_round_done,
        否则下次重试 (避免错误 done 标记).

    2026-08-18 follow-up FU3 修复:
      - 首轮 header 必须搭配至少一个 unit 原子评估 — 但如果首个 unit
        超预算, 跳过该 unit 继续尝试后续 unit (而不是放弃整个首轮);
      - 找到 anchor_unit 后, header+anchor_unit 原子加入, 后续 unit 再
        独立按真实 "\\n\\n" 分隔符预算选择;
      - 没有任何 unit 实际加入时不标记 _first_round_done, 下次重试.
    """

    def __init__(self, core, strategy: str = STRATEGY_IDENTITY_FIRST):
        if strategy not in VALID_STRATEGIES:
            logger.warning("未知 strategy=%s, 回退到 identity_first", strategy)
            strategy = STRATEGY_IDENTITY_FIRST
        self._core = core
        self._strategy = strategy
        self._last_identity_injected: str = ""  # 去重
        self._last_situation_injected: str = ""  # 去重（手帐机制 C）
        # 首轮召回 session 幂等：已成功组装首轮的 session 集合
        self._first_round_done: set[str] = set()

    def _get_session_context(self, session_id: str, *, create: bool = True):
        """Return a real Core-owned Context, or None for legacy fake cores."""
        if not session_id:
            return None
        contexts = getattr(self._core, "_session_contexts", None)
        if not isinstance(contexts, dict):
            return None
        getter = getattr(self._core, "get_session_context", None)
        if not callable(getter):
            return None
        try:
            context = getter(session_id, create=create)
        except TypeError:
            # Legacy fake cores expose only getter(session_id); preserve their
            # old behaviour when no deadline is in play.
            context = getter(session_id)
        return context if hasattr(context, "injection_dedupe") else None

    def build_context(self, query: str = "", session_id: str = "", *, deadline=None) -> str:
        """为当前 query 构建统一注入上下文（含总预算 + 首轮注入）。

        流程（设计 3.2）：
          1. 读 inject.max_chars 总预算；
          2. 取 identity / situation（含去重）；
          3. 取首轮召回（仅当 session_id 给出且未做过该 session），每个
             recall 单元 (note / tail_qa_block / 每张 topic) 作为独立候选
             进入预算竞争 — 放不下整体跳过, 不切 body;
          4. 取 query recall（每个 query 重新走，剩余预算）；
          5. 按 strategy 排序；
          6. 按完整单元挑选 + 总预算截断（不放就跳过）。separator `\n\n`
             必须计入总预算；任何 `\\n\\n`.join 之后的 len 必须 ≤ max_chars。

        Returns:
            合并后的注入字符串（多 part 用 \\n\\n 分隔），长度 ≤ max_chars。
        """
        deadline = coerce_deadline(deadline)
        if deadline is not None:
            deadline.check(context="injector")
        max_chars = _read_max_chars(self._core)
        sep_len = len("\n\n")
        # Create the Core-owned context for both paths so a successful
        # deadline-bound first call can persist first_round_done/tail_given.
        # The snapshot is restored on every PDE edge below, so a failed
        # deadline call does not consume session dedupe state.
        context = self._get_session_context(session_id, create=True)
        dedupe_snapshot = None
        if deadline is not None and context is not None:
            dedupe_snapshot = dict(context.injection_dedupe)

        def _restore_deadline_state() -> None:
            if dedupe_snapshot is None or context is None:
                return
            context.injection_dedupe.clear()
            context.injection_dedupe.update(dedupe_snapshot)

        try:
            identity = self._get_identity(context)
            situation = self._get_situation(context)  # 手帐机制 C
            if deadline is not None:
                deadline.check(context="after identity/situation")
        except PrefetchDeadlineExceeded:
            _restore_deadline_state()
            raise

        # ── 首轮召回（仅新 session 一次）
        # 2026-08-18 follow-up FU2 修复:
        #   - _format_first_round_block 返回 (header, units) — header 与每个 unit
        #     都是独立候选; caller (build_context) 在最终组装阶段直接挑选,
        #     每个首轮内容单元 (note / tail_block / 每张 topic) 独立参与最终
        #     预算竞争, 不再先 formatter 拼成一个 block 后作为一个 part.
        #   - 首轮 header 必须搭配至少一个内容 unit 才输出 (避免 orphan header).
        #   - 第一个 unit 与 header 原子评估 (header + sep + unit); 能放下才进.
        #   - 只有至少一个首轮内容 unit 已进入最终 chosen 后, 才 _first_round_done.
        #     如果 identity/query 把所有首轮 unit 挤掉, 仍不标记 done — 下次重试.
        first_round_header = ""
        first_round_units: list[str] = []
        if context is not None:
            first_round_pending = bool(
                session_id and not context.injection_dedupe.get("first_round_done")
            )
        else:
            first_round_pending = bool(
                session_id and session_id not in self._first_round_done
            )
        if first_round_pending:
            try:
                if deadline is None:
                    first_round_header, first_round_units = self._format_first_round_block(
                        session_id,
                    )
                else:
                    first_round_header, first_round_units = self._format_first_round_block(
                        session_id, deadline=deadline,
                    )
                if deadline is not None:
                    deadline.check(context="after new-session recall")
            except PrefetchDeadlineExceeded:
                _restore_deadline_state()
                raise
            if not first_round_units:
                # 没有任何首轮内容 unit → 不标记 session_id, 允许下一轮重试
                first_round_header = ""

        # query recall 位于 identity/situation 之后时，先把已经确定会占位的
        # 完整单元和与 recall 的连接符计入内部预算；recall_first 则不预扣。
        recall_max_chars = max_chars
        if self._strategy != STRATEGY_RECALL_FIRST:
            pre_recall_parts: list[str] = []
            seen_pre_recall: set[str] = set()
            for part in (identity, situation):
                if not part or part in seen_pre_recall:
                    continue
                sep_cost = sep_len if pre_recall_parts else 0
                if sep_cost + len(part) > recall_max_chars:
                    continue
                pre_recall_parts.append(part)
                seen_pre_recall.add(part)
                recall_max_chars -= sep_cost + len(part)
            if pre_recall_parts:
                recall_max_chars -= sep_len

        # ── Query recall（每个 query 都跑；传入前置块后的剩余预算）
        recall = ""
        if query and query.strip():
            recall_kwargs = {
                "session_id": session_id,
                "max_chars": max(0, recall_max_chars),
            }
            if deadline is not None:
                recall_kwargs["deadline"] = deadline
            try:
                recall = self._get_recall(query, **recall_kwargs)
            except PrefetchDeadlineExceeded:
                _restore_deadline_state()
                raise
            if deadline is not None:
                try:
                    deadline.check(context="after query recall")
                except PrefetchDeadlineExceeded:
                    _restore_deadline_state()
                    raise

        # ── 按 strategy 排序 + 按完整单元挑选 + separator-aware 预算
        # 首轮召回永远排在 query recall 之后 (与设计契约一致 — 头部与首轮单元
        # 在最终组装阶段原子加入).
        parts_ordered = self._order_parts(identity, situation, recall, "")
        chosen: list[str] = []
        seen_parts: set[str] = set()
        budget = max_chars
        first_round_used = False  # 至少一个首轮内容 unit 已选入最终 chosen 才 True
        for p in parts_ordered:
            if not p or p in seen_parts:
                continue
            # 完整单元 + separator 开销 同时算入预算
            sep_cost = sep_len if chosen else 0
            if sep_cost + len(p) > budget:
                # 完整单元超过剩余预算 → 整体跳过，不切字符串
                logger.debug(
                    "build_context: 跳过超额完整单元 (sep_cost=%d, unit_len=%d, remain_budget=%d)",
                    sep_cost, len(p), budget,
                )
                continue
            chosen.append(p)
            seen_parts.add(p)
            budget -= sep_cost + len(p)

        # ── 首轮单元最终组装: header + 每个 unit 独立按完整预算竞争
        # 1) header 必须搭配至少一个 unit 原子评估 (避免 orphan header 输出);
        # 2) `for unit in first_round_units` 找到第一个能与 header 原子放下的
        #    unit — 第一个超预算时 `continue` 跳过 (不放弃整个首轮), 后续
        #    unit 继续尝试; 找到后 header+该 unit 原子加入;
        # 3) 后续 unit (找到的 unit 之后) 再独立按 separator + len(unit) 评估;
        # 4) 没有任何 unit 实际加入时, 不标记 _first_round_done (下次重试).
        if first_round_pending and first_round_units:
            # 寻找第一个能与 header 原子放下的 unit — header 必须搭配
            # 该 unit 同时进 (避免 orphan header).
            anchor_unit: str | None = None
            anchor_idx: int | None = None
            for idx, unit in enumerate(first_round_units):
                if not unit or unit in seen_parts:
                    continue
                if chosen:
                    header_total_cost = (
                        sep_len + len(first_round_header) + sep_len + len(unit)
                    )
                else:
                    header_total_cost = (
                        len(first_round_header) + sep_len + len(unit)
                    )
                if header_total_cost > budget:
                    # 这个 unit 放不下, continue 跳过 — 不放弃整个首轮,
                    # 下一个 unit 继续尝试.
                    logger.debug(
                        "build_context: 首轮 header + 当前 unit 整体放不下 "
                        "(budget=%d, header=%d, unit=%d) — 跳过当前 unit, "
                        "继续尝试下一个",
                        budget, len(first_round_header), len(unit),
                    )
                    continue
                anchor_unit = unit
                anchor_idx = idx
                break
            if anchor_unit is None:
                # 所有 unit 与 header 一起都放不下 → 整个首轮跳过
                # (不允许 orphan header). 不标记 session_id done — 下次重试.
                logger.debug(
                    "build_context: 首轮所有 unit 与 header 都放不下, "
                    "跳过整个首轮 — 下次重试",
                )
            else:
                # 1) header 进入 chosen
                sep_cost_h = sep_len if chosen else 0
                chosen.append(first_round_header)
                seen_parts.add(first_round_header)
                budget -= sep_cost_h + len(first_round_header)
                # 2) anchor_unit 进入 chosen
                sep_cost_u = sep_len if len(chosen) > 1 else 0
                if sep_cost_u + len(anchor_unit) <= budget:
                    chosen.append(anchor_unit)
                    seen_parts.add(anchor_unit)
                    budget -= sep_cost_u + len(anchor_unit)
                    first_round_used = True
                    # 3) 后续 units (anchor_unit 之后) 独立按 separator + len(unit) 竞争
                    if anchor_idx is not None:
                        for unit in first_round_units[anchor_idx + 1:]:
                            if not unit or unit in seen_parts:
                                continue
                            sep_cost = sep_len if len(chosen) > 1 else 0
                            if sep_cost + len(unit) > budget:
                                logger.debug(
                                    "build_context: 跳过超额首轮 unit (unit_len=%d, remain=%d)",
                                    len(unit), budget,
                                )
                                continue
                            chosen.append(unit)
                            seen_parts.add(unit)
                            budget -= sep_cost + len(unit)
                            first_round_used = True
                else:
                    # header 进了但 anchor_unit 放不下 (理论上不会触发, 因为
                    # 我们刚验过 header_total_cost ≤ budget) → 回滚 header,
                    # 整个首轮跳过.
                    chosen.pop()
                    seen_parts.discard(first_round_header)
                    budget += sep_cost_h + len(first_round_header)
                    logger.debug(
                        "build_context: 首轮 header 进了但 anchor_unit 放不下 "
                        "(budget=%d, unit=%d), 回滚 header, 整个首轮跳过 — 下次重试",
                        budget, len(anchor_unit),
                    )
        if first_round_pending and first_round_used:
            # 只有至少一个首轮内容 unit 已进最终 chosen, 才 _first_round_done.
            # 否则 (identity / query 把所有首轮 unit 挤掉) 保留 session_id,
            # 下一次 build_context 必须重试 recall_for_new_session.
            if context is not None:
                context.injection_dedupe["first_round_done"] = True
            else:
                self._first_round_done.add(session_id)
        return "\n\n".join(chosen)

    def _order_parts(
        self, identity: str, situation: str, recall: str, first_round_block: str,
    ) -> list[str]:
        """按 strategy 排序各段。

        手帐机制 C：态势总览固定在身份块之后、事实回忆之前。
        首轮召回（first_round_block）紧随 query recall 之后，呈现给模型
        的顺序：identity → situation → query recall → 首轮（note/tail/topics）。

        - identity_first / balanced / budget: identity → situation → recall → first_round
        - recall_first: recall → situation → identity → first_round

        2026-08-18 follow-up FU2: 首轮召回在 build_context 末尾原子加入,
        本方法返回时 first_round_block 参数固定为空串 (caller 不再预拼);
        这里保留签名以兼容现有测试。
        """
        first = first_round_block or ""
        if self._strategy == STRATEGY_RECALL_FIRST:
            return [recall, situation, identity, first]
        return [identity, situation, recall, first]

    # ── 首轮召回 ────────────────────────────────────────────────────────────

    def _format_first_round_block(
        self, session_id: str, *, deadline=None,
    ) -> tuple[str, list[str]]:
        """首轮召回格式化 — 从 v3core.observer.recall_for_new_session 取数据。

        返回 (header, units):
          - header: 顶部 [新会话记忆召回] 头串 (长度固定 8)
          - units: 内容 unit 列表 — note_unit / tail_qa_block / 每张 topic_unit.
            每个 unit 是一个完整字符串, 由 caller (build_context) 在最终组装
            阶段独立参与预算竞争; 不在这里预算选择, 也不拼成块.
          - 若 recall 失败 / 无内容, 返回 ("", []).

        重要约束 (2026-08-18 follow-up FU2):
          - 运行时合法 import observer (模块级引入会循环依赖与冷启动开销);
          - 返回独立的 header + units — caller 在 build_context 末尾按完整预算
            逐个挑选, 放不下整体跳过 (避免大 topic 挤掉 note / header orphan);
          - 调用 observer 时把 core 配置/session_id 传入 (runtime import;
            `recall_for_new_session(config=..., session_id=...)` 接口合法,
            observer.py 不动);
          - `qa_hits` 保持兼容返回但不计入文本（连接债务归 observer 后续修）.

        重要约束 (Stage 4 FU4 — Runtime-backed first-round recall PgPool 旁路):
          - Runtime-backed Core (拥有有效 core.pg_pool) 时, 必须把
            core.pg_pool 透传给 observer.recall_for_new_session(pool=...);
            effective config 必须来自 Core owned snapshot (core.config 属性),
            不得硬编码或回退到旧 resolver 路径;
          - Runtime-backed 路径下, observer 拒绝 pool kwarg / 抛 TypeError
            时必须 fail closed (logger.warning + 返回 ("", [])) — 绝不能
            catch 后再次以无 pool 参数调用 observer (避免静默退到 direct
            psycopg2.connect);
          - legacy / offline Core (core.pg_pool 为 None) 时, 保留旧 direct
            fallback 链: (config=, session_id=) → (session_id=) → (),
            与既有旧签名兼容;
          - 不改 observer.py、不把 pool 或 DB 句柄塞入 SessionContext。
        """
        # Stage 4 FU4: 取 Core owned snapshot (effective config) 与 pg_pool.
        # `core.config` 是 property, 读 self._config 并按需懒解析, 正是
        # 当前 Core owned 的 effective snapshot; 任何硬编码 config 都是违约。
        try:
            effective_config = getattr(self._core, "config", None)
        except Exception:
            effective_config = None
        # 防御：旧 fake Core 既无 pg_pool property 也无 _pg_pool backing field
        # 时, 视为 legacy / offline, 保留旧签名兼容链。
        pg_pool = getattr(self._core, "pg_pool", None)
        if pg_pool is None:
            pg_pool = getattr(self._core, "_pg_pool", None)
        deadline = coerce_deadline(deadline)
        if deadline is not None:
            deadline.check(context="new-session recall")
            if pg_pool is not None:
                pg_pool = DeadlinePoolView(pg_pool, deadline)

        try:
            # 2026-08-18 统一入口: injector 内部运行时合法 import observer
            from v3core import observer as _observer
            if pg_pool is not None:
                # Runtime-backed Core: 必须走 pool 旁路, 不允许无 pool 调用。
                # 若 observer 不接受 pool (旧签名) → fail closed, 绝不能再试。
                try:
                    recall = _observer.recall_for_new_session(
                        config=effective_config,
                        session_id=session_id,
                        pool=pg_pool,
                    )
                except TypeError as e:
                    # Runtime-backed 契约: 池已注入, 旧签名就应当 fail closed,
                    # 禁止 catch 后退到无 pool direct 调用。
                    logger.warning(
                        "首轮召回 fail-closed (Runtime-backed Core pg_pool 注入"
                        ", observer 拒绝 pool kwarg): %s", e,
                    )
                    return ("", [])
            else:
                # legacy / offline Core: 保留旧签名兼容链 (config + session_id
                # → session_id → 无参)。即便 observer 抛 TypeError, 也允许
                # 渐进退避到无参旧签名 — 这是被 Runtime-backed 禁用的旁路。
                try:
                    recall = _observer.recall_for_new_session(
                        config=effective_config,
                        session_id=session_id,
                    )
                except TypeError:
                    # 旧签名不接 config — 回退 session_id 关键字
                    try:
                        recall = _observer.recall_for_new_session(
                            session_id=session_id,
                        )
                    except TypeError:
                        recall = _observer.recall_for_new_session()
        except PrefetchDeadlineExceeded:
            raise
        except Exception as e:
            logger.warning("首轮召回失败: %s", e)
            return ("", [])

        if not recall or not isinstance(recall, dict):
            return ("", [])

        # ── 按完整单元拆分 (不做预算选择, 全部返回给 caller) ──
        # 每个 part (note / tail_qa / 每张 topic) 作为独立 unit;
        # caller 在 build_context 末尾按完整预算挑选.
        header = "[新会话记忆召回]"
        units: list[str] = []

        note = recall.get("note") or {}
        if isinstance(note, dict) and note.get("content"):
            note_unit = f"[记忆印 {note.get('version', '')}] {note['content']}"
            units.append(note_unit)

        tail = recall.get("tail") or []
        if isinstance(tail, list) and tail:
            qa_lines = [
                f"Q: {it.get('question', '')}\nA: {it.get('answer', '')}"
                for it in tail
                if isinstance(it, dict)
            ]
            if qa_lines:
                fence = recall.get("fence", "") or ""
                if not isinstance(fence, str):
                    fence = ""
                body = "\n".join(qa_lines)
                tail_unit = (fence + "\n" + body) if fence else body
                units.append(tail_unit)

        topics = recall.get("topics") or []
        if isinstance(topics, list) and topics:
            for t in topics:
                if not isinstance(t, dict):
                    continue
                t_title = t.get("title", "")
                t_body = (t.get("body") or "").strip()
                if t_body:
                    # 完整 topic 卡作为一个 unit (完整 body), 由 caller 决定是否选入
                    units.append(
                        f"[相关主题] {t_title}({t.get('sim', '')}): {t_body}"
                    )
                elif t_title:
                    units.append(f"[相关主题] {t_title}({t.get('sim', '')})")

        return (header, units)

    # ── 现有 identity / situation / query recall ────────────────────────────

    def _get_situation(self, context=None) -> str:
        """获取系统态势总览（手帐机制 C），含去重

        内容由 E1 写印后生成（situation_overview.md），纯读 + mtime 缓存。
        若与上次注入相同则跳过（避免重复推送）。
        """
        try:
            block = self._core._read_situation_overview()
        except Exception as e:
            logger.warning("态势总览获取失败: %s", e)
            return ""

        if not block:
            return ""
        if context is not None:
            if block == context.injection_dedupe.get("situation", ""):
                return ""
            context.injection_dedupe["situation"] = block
            return block
        if block == self._last_situation_injected:
            return ""
        self._last_situation_injected = block
        return block

    def _get_identity(self, context=None) -> str:
        """获取身份锚点，含去重

        若当前身份锚点内容与上次注入相同, 返回空字符串（避免重复注入）。
        """
        try:
            block = self._core._compress_yin_to_identity()
        except Exception as e:
            logger.warning("身份锚点获取失败: %s", e)
            return ""

        if not block:
            return ""
        if context is not None:
            if block == context.injection_dedupe.get("identity", ""):
                return ""
            context.injection_dedupe["identity"] = block
            return block
        if block == self._last_identity_injected:
            return ""
        self._last_identity_injected = block
        return block

    def _get_recall(self, query: str, session_id: str = "", max_chars: int | None = None, *, deadline=None) -> str:
        """获取事实回忆，并尽量把调用方上下文预算/会话传给旧实现。"""
        deadline = coerce_deadline(deadline)
        if deadline is not None:
            deadline.check(context="injector recall")
        try:
            method = self._core.prefetch_to_context_block
            kwargs: dict[str, Any] = {}
            if session_id:
                kwargs["session_id"] = session_id
            if max_chars is not None:
                kwargs["max_chars"] = int(max_chars)
            if deadline is not None:
                kwargs["deadline"] = deadline

            # 旧 V3Core 可能只支持其中一个 kwarg；先按真实签名过滤，
            # 避免 TypeError 后把两个上下文参数一起丢掉。
            try:
                signature = inspect.signature(method)
            except (TypeError, ValueError):
                signature = None
            if signature is not None and not any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in signature.parameters.values()
            ):
                kwargs = {
                    key: value
                    for key, value in kwargs.items()
                    if key in signature.parameters
                }

            try:
                return method(query, **kwargs)
            except PrefetchDeadlineExceeded:
                raise
            except TypeError:
                # 签名不可读或运行时实现拒绝了某个 kwarg：逐个退避，
                # 保留仍被旧实现接受的参数。
                for key in ("max_chars", "session_id"):
                    if key not in kwargs:
                        continue
                    reduced = dict(kwargs)
                    reduced.pop(key)
                    try:
                        return method(query, **reduced)
                    except PrefetchDeadlineExceeded:
                        raise
                    except TypeError:
                        continue
                return method(query)
        except PrefetchDeadlineExceeded:
            raise
        except Exception as e:
            logger.warning("事实回忆失败: %s", e)
            return ""

    def reset(self, session_id: str | None = None) -> None:
        """Reset one session's injection state, or the legacy fallback state."""
        target_session = session_id or getattr(self._core, "_active_session_id", "")
        context = self._get_session_context(target_session)
        if context is not None:
            context.injection_dedupe.update(
                identity="",
                situation="",
                first_round_done=False,
                tail_given=False,
            )
            return
        self._last_identity_injected = ""
        self._last_situation_injected = ""  # 手帐机制 C
        self._first_round_done = set()

    @property
    def strategy(self) -> str:
        return self._strategy

    def set_strategy(self, strategy: str) -> None:
        if strategy not in VALID_STRATEGIES:
            logger.warning("未知 strategy=%s, 保持 %s", strategy, self._strategy)
            return
        self._strategy = strategy