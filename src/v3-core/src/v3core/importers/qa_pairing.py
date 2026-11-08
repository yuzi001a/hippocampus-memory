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
from dataclasses import dataclass
from typing import Iterable

from . import KIND_RAW, ImportItem

logger = logging.getLogger("v3core.importers.qa_pairing")

QA_SOURCE_PREFIX = "qa_import"
_SEP = "\n\n"


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


def pair_items(items: Iterable[ImportItem]) -> list[QAPair]:
    """Derive deterministic QA pairs from a raw import stream."""
    groups: dict[tuple[str, str], list[ImportItem]] = {}
    for it in items:
        if it.kind != KIND_RAW:
            continue
        if not (it.text or "").strip():
            continue
        role = (it.role or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
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

    pairs: list[QAPair] = []
    for (host, sid), group in groups.items():
        ordered = sorted(group, key=lambda x: (x.occurred_at or ""))
        pairs.extend(_pair_session(ordered, host=host, session_id=sid))
    return pairs


# 宿主压缩产物：胶水标记，来源固定，非对话内容
_COMPACTION_RE = re.compile(
    r"^\[(?:PRIOR CONTEXT|CONTEXT COMPACTION|END OF PRIOR CONTEXT)"
)


def _final_answer(assts: list[ImportItem]) -> str:
    """长链任务里 assistant 行是「过程」，只有最后一轮是「结论」。

    判据是结构性的：中间行带 tool_calls（工具调用前的旁白），最后一轮通常
    是纯文本总结。除非没有任何纯文本收尾，否则只保留最后那条纯文本行。
    没有 tool_calls 的多行 = 同一条回复被宿主拆段，照旧合并。
    """
    texts = [a.text.strip() for a in assts if (a.text or "").strip()]
    if not texts:
        return ""
    if len(assts) == 1:
        return texts[0]
    def _has_tools(item: ImportItem) -> bool:
        prov = item.provenance or {}
        tc = prov.get("tool_calls")
        return bool(tc) and tc not in ("[]", "null")
    if not any(_has_tools(a) for a in assts):
        return _SEP.join(texts)
    for a in reversed(assts):
        if (a.text or "").strip() and not _has_tools(a):
            return a.text.strip()
    return texts[-1]


def _pair_session(
    items: list[ImportItem], *, host: str, session_id: str
) -> list[QAPair]:
    out: list[QAPair] = []
    users: list[ImportItem] = []
    assts: list[ImportItem] = []

    def flush() -> None:
        nonlocal users, assts
        if users and assts:
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
                    question=_SEP.join(u.text.strip() for u in users),
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
