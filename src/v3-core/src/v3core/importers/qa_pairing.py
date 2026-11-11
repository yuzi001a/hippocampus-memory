"""v3core.importers.qa_pairing — deterministic raw→recall-ready derivation.

Imported raw history alone is NOT recallable (recall lanes read qa_pairs).
This module derives QA pairs from the raw import stream with **no LLM, no
summarisation, no rewriting** (§9): a small deterministic state machine per
(host, session).

Pairing rules
-------------
* ``user`` rows open a question; consecutive user rows merge into ONE
  question (multi-injection questions — v3 pairing decision).
* ``assistant`` rows join the open question; consecutive assistant rows
  merge into ONE answer (multi-step replies).
* Rows with no counterpart never become QA pairs — orphan raw stays raw
  ("宁可只保存 raw，不要错误配 QA").
* Ordering is deterministic: sort by ``occurred_at`` (stable tie-break =
  parse order), so turn_id / pair boundaries are reproducible run to run.

Identity: ``source_id = qa_import/<host>/<session_id>/<q_event_id>`` where
``q_event_id`` is the **first user message's** event id — the same value
written to ``conversation_stream.event_id``, so the chain
qa_pairs → conversation_stream → native event is greppable.
"""
from __future__ import annotations

import re

import logging
from dataclasses import dataclass, replace
from typing import Iterable

from . import KIND_RAW, ImportItem
from .legacy_sources import is_machine_preamble, strip_host_markers, unwrap_injected

logger = logging.getLogger("v3core.importers.qa_pairing")

QA_SOURCE_PREFIX = "qa_import"
_SEP = "\n\n"

# Session provenance that must never enter the conversational QA layer.
#
# Hermes records *how* a session was created in ``sessions.source``:
#   desktop / tui / feishu / weixin  → a human talking
#   tool                             → one scripted CLI call (max 2 messages)
#   cron                             → a scheduler run
#   oneshot / subagent               → programmatic dispatch
#
# The batch judge was 1,060 QA pairs whose "question" was a 12k–124k char
# reviewer prompt: every one of them came from a ``tool`` session. No text
# prefix or length heuristic catches that reliably — the native field does,
# with zero false positives. Unknown/absent sources stay in (fail-open) so a
# store without a ``sessions`` table degrades to the old behaviour instead of
# silently dropping history.
MACHINE_SESSION_SOURCES = frozenset({
    "tool", "cron", "oneshot", "subagent", "api_server", "kanban",
    "p0-phase23", "p0-phase23-scoped",
})

# 文件转储冒充提问：JSON 状态文件、会话语料导出、记忆文件全文、附件引用。
# 这些是"把文件内容粘进对话框"，不是提问；长度可达 250 万字符，永远嵌不进
# 向量库（API 直接 400），留着只会挤掉真实召回槽位。raw 原文不动。
_DUMP_PREFIXES = (
    '{"version":',
    '{"last_check":',
    '# Session Corpus:',
    '# USER.md',
    '@file:',
)
_DUMP_MARKERS = ('"processed_files":',)
# 显式上限：超过这个长度的题面不可能是人类一问一答（实测最长 250 万字符）
MAX_QUESTION_CHARS = 100_000



@dataclass
class QAPair:
    host: str
    session_id: str
    question: str
    answer: str
    q_event_id: str
    turn_id: int
    occurred_at: str | None
    source_id: str


def pair_items(
    items: Iterable[ImportItem],
    *,
    excluded_session_sources: Iterable[str] | None = None,
) -> list[QAPair]:
    """Derive deterministic QA pairs from a raw import stream."""
    excluded = (
        MACHINE_SESSION_SOURCES
        if excluded_session_sources is None
        else frozenset(str(s).strip().lower() for s in excluded_session_sources)
    )
    skipped_sessions: dict[str, int] = {}
    dump_skipped = 0
    groups: dict[tuple[str, str], list[ImportItem]] = {}
    for it in items:
        if it.kind != KIND_RAW:
            continue
        if not (it.text or "").strip():
            continue
        role = (it.role or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        # Native session provenance beats every text heuristic: a scripted
        # judge run is not a conversation no matter how clean its prompt reads.
        src = (getattr(it, "session_source", None) or "").strip().lower()
        if src and src in excluded:
            key = (it.host or it.source_system or "", it.source_ref or "")
            if key not in skipped_sessions:
                skipped_sessions[key] = 0
            skipped_sessions[key] += 1
            continue
        # 投递层贴在正文前的标记（[Wed 2026-04-22 01:59 GMT+8] / [message_id: …] /
        # [[reply_to_current]]）不是内容：剥掉留正文，两条角色都适用。
        marked = strip_host_markers(it.text or "")
        if marked != (it.text or ""):
            it = replace(it, text=marked)
        # 机器外壳里裹着真人原话：OOB 插话（84 条真话）/ standing goal 的 Goal /
        # skill 调用尾部的真人指令。整条丢等于删真意图 —— 剥壳保留正文，纯外壳才丢。
        if role == "user":
            raw_text = it.text or ""
            # 裸的机器前言必须在这里拦掉。此前这段判断只写在"剥壳成功"分支里，
            # 导致没有外壳的 cron/后台通知（实测 361 条 "You are running as a
            # scheduled cron job"、217 条 "Your active task list…"）整条混成提问。
            if is_machine_preamble(raw_text):
                continue
            # 文件转储（JSON 状态 / 会话语料导出 / 记忆文件全文 / 附件引用）不是提问
            if is_machine_dump(raw_text):
                dump_skipped += 1
                continue
            unwrapped = unwrap_injected(raw_text)
            if unwrapped is not None:
                body, _kind = unwrapped
                # 剥壳后还得再判一次：skill 外壳里裹的常常正是 cron 指令本身
                # （实测 892 条里 890 条剥出来是机器正文），剥完当提问等于白剥。
                if not body or is_machine_preamble(body):
                    continue
                it = replace(it, text=body)
        # 机器状态落在 assistant 侧同样不能进问答：[SILENT] 1,628 条、
        # Operation interrupted 170 条、[ASYNC DELEGATION …] 770 条。
        # 但**不能整条删**——删了上下两个 user 轮会粘成一个问题（实测 1,638 对
        # QA 因此消失）。保留这一轮当分界，由 _final_answer 跳过机器文本、
        # flush 时答案为空自然不出对。
        host_key = (it.host or it.source_system or "").strip() or "unknown"
        sess_key = (it.source_ref or "").strip() or "imported"
        # 同一 session 内相邻的完全相同消息 = 宿主重复落库（实测占 23%），
        # 不是用户又说了一遍。折叠掉，否则答案被复制几十遍。
        bucket = groups.setdefault((host_key, sess_key), [])
        if bucket and bucket[-1].role == role and (bucket[-1].text or "").strip() == (it.text or "").strip():
            continue
        # 上下文压缩摘要是宿主塞进来的元文本，不是任何一方说的话。
        # 实测 71 条以 assistant 落库（最长 6.7 万字符），有 6 条混成了答案。
        if _COMPACTION_RE.match((it.text or "").lstrip()):
            continue
        host = (it.host or it.source_system or "").strip() or "unknown"
        sid = (it.source_ref or "").strip() or "imported"
        groups.setdefault((host, sid), []).append(it)

    if dump_skipped:
        logger.info("qa_pairing: skipped %d machine dump question(s)", dump_skipped)
    if skipped_sessions:
        logger.info(
            "qa_pairing: skipped %d machine-session message(s) across %d session(s) "
            "(excluded sources: %s)",
            sum(skipped_sessions.values()),
            len(skipped_sessions),
            ",".join(sorted(excluded)) or "<none>",
        )

    pairs: list[QAPair] = []
    for (host, sid), group in groups.items():
        ordered = sorted(group, key=lambda x: (x.occurred_at or ""))
        pairs.extend(_pair_session(ordered, host=host, session_id=sid))
    return pairs


# 宿主压缩产物：胶水标记，来源固定，非对话内容
_COMPACTION_RE = re.compile(
    r"^\[(?:PRIOR CONTEXT|CONTEXT COMPACTION|END OF PRIOR CONTEXT|STILL IN PROGRESS)"
)


def is_machine_dump(text: str) -> bool:
    """文件转储/导出体冒充提问的确定性判据（只作用于 QA 派生层，raw 不动）"""
    head = (text or "").lstrip()[:2000]
    if not head:
        return False
    if len(text or "") > MAX_QUESTION_CHARS:
        return True
    if head.startswith(_DUMP_PREFIXES):
        return True
    return any(m in head for m in _DUMP_MARKERS)


def _final_answer(assts: list[ImportItem]) -> str:
    """长链任务里 assistant 行是「过程」，只有最后一轮是「结论」。

    判据是结构性的：中间行带 tool_calls（工具调用前的旁白），最后一轮通常
    是纯文本总结。除非没有任何纯文本收尾，否则只保留最后那条纯文本行。
    没有 tool_calls 的多行 = 同一条回复被宿主拆段，照旧合并。
    """
    texts = [a.text.strip() for a in assts if (a.text or "").strip()]
    if not texts:
        return ""
    # 机器状态（[SILENT] / Operation interrupted / [ASYNC DELEGATION …]）不是结论。
    # 必须先剔掉再判断"只有一条"，否则单条 [SILENT] 会被当成答案直接返回。
    substantive = [a for a in assts if not is_machine_preamble(a.text or "")]
    if not substantive:
        return ""
    if len(substantive) == 1:
        return (substantive[0].text or "").strip()
    def _has_tools(item: ImportItem) -> bool:
        prov = item.provenance or {}
        tc = prov.get("tool_calls")
        return bool(tc) and tc not in ("[]", "null")
    if not any(_has_tools(a) for a in substantive):
        return _SEP.join(t.text.strip() for t in substantive if (t.text or "").strip())
    for a in reversed(substantive):
        if (a.text or "").strip() and not _has_tools(a):
            return a.text.strip()
    return (substantive[-1].text or "").strip()


def _pair_session(
    items: list[ImportItem], *, host: str, session_id: str
) -> list[QAPair]:
    out: list[QAPair] = []
    users: list[ImportItem] = []
    assts: list[ImportItem] = []

    def flush() -> None:
        nonlocal users, assts
        if users and assts:
            answer = _final_answer(assts)
            if not answer.strip():
                # 这一轮只有机器状态（[SILENT] / 中断标记），不是问答。
                users, assts = [], []
                return
            question = _SEP.join(u.text.strip() for u in users)
            # 合并相邻 user 行后可能拼出超长转储体（单条各自都不超限），
            # 所以判据必须在**合并后**再跑一次：>100k 或转储签名都不是提问。
            if is_machine_dump(question):
                users, assts = [], []
                return
            q0 = users[0]
            qid = (q0.event_id or "").strip()
            if not qid:
                # Defensive only — production importers always carry an
                # event id. Never masquerade as native.
                qid = f"derived:{host}:{session_id}:turn-{len(out) + 1}"
            out.append(
                QAPair(
                    host=host,
                    session_id=session_id,
                    question=question,
                    answer=_final_answer(assts),
                    q_event_id=qid,
                    turn_id=len(out) + 1,
                    occurred_at=q0.occurred_at,
                    source_id=f"{QA_SOURCE_PREFIX}/{host}/{session_id}/{qid}",
                )
            )
        users, assts = [], []

    for it in items:
        role = (it.role or "").strip().lower()
        if role == "user":
            if users and not assts:
                # consecutive user rows → same question
                users.append(it)
            else:
                flush()
                users, assts = [it], []
        elif role == "assistant":
            if users:
                assts.append(it)
            # leading assistant without a question → raw only
    flush()
    return out


def _clean_nul(text: str) -> tuple[str, int]:
    """Strip NUL (0x00) — PostgreSQL text cannot store it (see v3core.importers.sanitize_nul)."""
    if not text or "\x00" not in text:
        return text, 0
    return text.replace("\x00", ""), 1


def emit_qa_pairs(*, pool, pairs: list[QAPair], stats, source_tag: str = "import") -> None:
    """Write QA pairs with ``qa_pairs.source_id`` idempotency.

    ``WHERE NOT EXISTS (source_id)`` — a re-run inserts 0 new rows. Rows
    carry question/answer verbatim; embedding stays NULL (base import never
    calls embedding — §10 durability before derivation).
    """
    import json as _json

    sql = """
        INSERT INTO public.qa_pairs
            (source_id, session_id, turn_id, question, answer, chars, timestamp,
             source, tool_calls, tool_results)
        SELECT %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, NOW()), %s,
               '[]'::jsonb, '[]'::jsonb
        WHERE NOT EXISTS (
            SELECT 1 FROM public.qa_pairs WHERE source_id = %s
        )
    """
    for pair in pairs:
        lease = pool.lease(timeout=5.0)
        try:
            cur = lease.connection.cursor()
            q, qn = _clean_nul(pair.question)
            a, an = _clean_nul(pair.answer)
            if qn or an:
                stats.nul_sanitized += 1
            cur.execute(
                sql,
                (
                    pair.source_id,
                    pair.session_id,
                    pair.turn_id,
                    q,
                    a,
                    len(q) + len(a),
                    _iso_or_none(pair.occurred_at),
                    source_tag,
                    pair.source_id,
                ),
            )
            inserted = bool(getattr(cur, "rowcount", 0))
            lease.connection.commit()
            if inserted:
                stats.qa_pairs_derived += 1
            else:
                stats.deduped += 1
        except Exception as exc:
            stats.skipped += 1
            stats.errors.append(f"qa_pairs write failed ({pair.source_id}): {exc!r}")
            try:
                lease.connection.rollback()
            except Exception:
                pass
        finally:
            try:
                lease.close()
            except Exception:
                pass


def _iso_or_none(value: str | None):
    if not value:
        return None
    return value


_QA_INSERT_SQL = """
        INSERT INTO public.qa_pairs
            (source_id, session_id, turn_id, question, answer, chars, timestamp,
             source, tool_calls, tool_results)
        SELECT %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, NOW()), %s,
               '[]'::jsonb, '[]'::jsonb
        WHERE NOT EXISTS (
            SELECT 1 FROM public.qa_pairs WHERE source_id = %s
        )
    """


def emit_qa_pairs_bulk(*, pool, pairs: list[QAPair], stats, source_tag: str = "import",
                       batch_size: int = 500) -> None:
    """Batched QA writer — one lease and one commit per batch.

    Same SQL and same ``source_id`` idempotency as ``emit_qa_pairs``; only
    the transaction granularity changes. A failing batch is rolled back as
    a unit and reported, never dropped silently.
    """
    for start in range(0, len(pairs), batch_size):
        chunk = pairs[start:start + batch_size]
        if not chunk:
            continue
        lease = pool.lease(timeout=10.0)
        inserted = 0
        try:
            cur = lease.connection.cursor()
            for pair in chunk:
                q, qn = _clean_nul(pair.question)
                a, an = _clean_nul(pair.answer)
                if qn or an:
                    stats.nul_sanitized += 1
                cur.execute(
                    _QA_INSERT_SQL,
                    (
                        pair.source_id,
                        pair.session_id,
                        pair.turn_id,
                        q,
                        a,
                        len(q) + len(a),
                        _iso_or_none(pair.occurred_at),
                        source_tag,
                        pair.source_id,
                    ),
                )
                inserted += 1 if getattr(cur, "rowcount", 0) else 0
            lease.connection.commit()
            stats.qa_pairs_derived += inserted
            stats.deduped += len(chunk) - inserted
        except Exception as exc:
            try:
                lease.connection.rollback()
            except Exception:
                pass
            stats.skipped += len(chunk)
            stats.errors.append(
                f"qa batch failed ({len(chunk)} pairs, first {chunk[0].source_id}): {exc!r}"
            )
        finally:
            try:
                lease.close()
            except Exception:
                pass


__all__ = ["QAPair", "pair_items", "emit_qa_pairs", "emit_qa_pairs_bulk", "QA_SOURCE_PREFIX"]
