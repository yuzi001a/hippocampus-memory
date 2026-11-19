"""v3core.session_policy — 零 A 会话的四档处置（确定性，不写死比例）。

背景：10,806 个会话里 8,799 个没有 A 类消息，但"零 A"并不等于"零价值"
（实测 1,339 个零 A 会话里有真实任务和审查结论）。所以不整批过滤，分四档：

========  ==================================================================
keep      有真人提问且有 QA —— 正常参与 QA 与长期记忆提炼
downgrade 没有真人提问但有 QA（指令来自包装/自动化）—— 保留，检索降权
raw_only  没有 QA 但有实质助手产出 —— 只保存原文，不参与提炼
exclude   通篇机器状态 —— 不生成 QA，不参与提炼（原文照旧保留）
========  ==================================================================

判据只用结构信号（角色、是否命中机器标记、是否配出 QA），不用长度阈值、
中文关键词或内容语义。比例由数据算出来，不同语料自然得到不同数字。

另有"来源本身缺用户侧"的标记（例如 chat_md 的导出），这类会话**不能**
因为零 A 被判 exclude：不是没有用户，是来源没带。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

TIERS = ("keep", "downgrade", "raw_only", "exclude")

# 已知导出缺用户侧的来源（导入时按来源判定，不按内容猜）
KNOWN_INCOMPLETE_SOURCES = {"chat_md"}


@dataclass
class SessionSignals:
    session_id: str
    host: str | None = None
    raw_n: int = 0
    human_user_n: int = 0
    machine_n: int = 0
    assistant_n: int = 0
    qa_n: int = 0

    @property
    def source_incomplete(self) -> bool:
        return (self.host or "") in KNOWN_INCOMPLETE_SOURCES


def classify_session(sig: SessionSignals) -> tuple[str, str]:
    """Return ``(tier, reason)``. Pure function, no side effects."""
    if sig.source_incomplete and sig.qa_n == 0 and sig.human_user_n == 0:
        # 来源本身缺用户侧：留原文 + 标记不完整，不判负价值。
        return "raw_only", "source_missing_user_side"
    if sig.human_user_n > 0 and sig.qa_n > 0:
        return "keep", "human_question_with_answer"
    if sig.qa_n > 0:
        return "downgrade", "no_human_question_but_qa_exists"
    if sig.assistant_n > 0 and sig.raw_n > sig.machine_n:
        return "raw_only", "no_qa_but_substantive_assistant_output"
    if sig.raw_n > 0:
        return "exclude", "machine_only"
    return "exclude", "empty"


def tier_counts(rows: Iterable[tuple[str, str]]) -> dict:
    """``rows`` = iterable of (tier, reason) → counts by tier and by reason."""
    by_tier = {t: 0 for t in TIERS}
    by_reason: dict[str, int] = {}
    for tier, reason in rows:
        by_tier[tier] = by_tier.get(tier, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {"by_tier": by_tier, "by_reason": by_reason, "total": sum(by_tier.values())}


# ── 采集与落库 ──────────────────────────────────────────────────────────

_SIGNAL_SQL = """
    WITH raw AS (
        SELECT host, session_id,
               count(*) FILTER (WHERE role = 'user')      AS user_n,
               count(*) FILTER (WHERE role = 'assistant') AS assistant_n,
               count(*)                                   AS raw_n,
               count(*) FILTER (WHERE role = 'tool')      AS tool_n
          FROM public.conversation_stream
         GROUP BY host, session_id
    ), qa AS (
        SELECT split_part(source_id, '/', 2) AS host,
               session_id, count(*) AS qa_n
          FROM public.qa_pairs
         GROUP BY 1, 2
    )
    SELECT r.host, r.session_id, r.raw_n, r.user_n, r.assistant_n, r.tool_n,
           COALESCE(q.qa_n, 0)
      FROM raw r
      LEFT JOIN qa q ON q.host = r.host AND q.session_id = r.session_id
"""


def collect_signals(pool, *, machine_marker=None) -> list[SessionSignals]:
    """Read per-session signals from PG.

    ``machine_marker`` is a predicate ``(text) -> bool`` used to count machine
    rows; the caller passes ``is_machine_preamble`` from the importers so the
    classification and the capture rules can never drift apart.
    """
    lease = pool.lease(timeout=10.0)
    try:
        cur = lease.connection.cursor()
        cur.execute(_SIGNAL_SQL)
        rows = cur.fetchall() or []
    finally:
        try:
            lease.close()
        except Exception:
            pass
    out = [SessionSignals(session_id=r[1], host=r[0], raw_n=r[2] or 0,
                          human_user_n=r[3] or 0, assistant_n=r[4] or 0,
                          qa_n=r[6] or 0) for r in rows]
    if machine_marker is not None:
        # 机器行数用同一套捕获规则补算（结构前缀，不猜内容）。
        # 逐批取，避免把全库正文一次装进内存（生产 20 万行 ≈ 260MB）。
        by_key = {(s.host, s.session_id): s for s in out}
        lease = pool.lease(timeout=10.0)
        try:
            cur = lease.connection.cursor()
            last_id = 0
            while True:
                # keyset 分页（走主键），不把全库正文一次装进内存：
                # 生产 20 万行正文约 260MB，fetchall 会直接爆掉。
                cur.execute(
                    "SELECT id, host, session_id, content FROM public.conversation_stream "
                    "WHERE role IN ('user','assistant') AND id > %s ORDER BY id LIMIT 2000",
                    (last_id,),
                )
                chunk = cur.fetchall() or []
                if not chunk:
                    break
                for row_id, host, sid, content in chunk:
                    last_id = row_id
                    sig = by_key.get((host, sid))
                    if sig is not None and machine_marker(content or ""):
                        sig.machine_n += 1
        finally:
            try:
                lease.close()
            except Exception:
                pass
    return out


_UPSERT_SQL = """
    INSERT INTO public.session_policies
        (session_id, host, tier, reason, signals, updated_at)
    VALUES (%s, %s, %s, %s, %s::jsonb, now())
    ON CONFLICT (session_id) DO UPDATE
       SET host = EXCLUDED.host,
           tier = EXCLUDED.tier,
           reason = EXCLUDED.reason,
           signals = EXCLUDED.signals,
           updated_at = now()
"""


def store_policies(pool, signals: Iterable[SessionSignals]) -> dict:
    counts: dict[str, int] = {}
    lease = pool.lease(timeout=10.0)
    try:
        cur = lease.connection.cursor()
        for sig in signals:
            tier, reason = classify_session(sig)
            cur.execute(
                _UPSERT_SQL,
                (
                    sig.session_id,
                    sig.host,
                    tier,
                    reason,
                    json.dumps(
                        {
                            "raw": sig.raw_n,
                            "human_user": sig.human_user_n,
                            "machine": sig.machine_n,
                            "assistant": sig.assistant_n,
                            "qa": sig.qa_n,
                            "source_incomplete": sig.source_incomplete,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            counts[tier] = counts.get(tier, 0) + 1
        lease.connection.commit()
    except Exception:
        try:
            lease.connection.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            lease.close()
        except Exception:
            pass
    return counts


__all__ = [
    "TIERS",
    "KNOWN_INCOMPLETE_SOURCES",
    "SessionSignals",
    "classify_session",
    "collect_signals",
    "store_policies",
    "tier_counts",
]
