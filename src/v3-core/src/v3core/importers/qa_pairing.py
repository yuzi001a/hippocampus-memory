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
        host = (it.host or it.source_system or "").strip() or "unknown"
        sid = (it.source_ref or "").strip() or "imported"
        groups.setdefault((host, sid), []).append(it)

    pairs: list[QAPair] = []
    for (host, sid), group in groups.items():
        ordered = sorted(group, key=lambda x: (x.occurred_at or ""))
        pairs.extend(_pair_session(ordered, host=host, session_id=sid))
    return pairs


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
                    answer=_SEP.join(a.text.strip() for a in assts),
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
            cur.execute(
                sql,
                (
                    pair.source_id,
                    pair.session_id,
                    pair.turn_id,
                    pair.question,
                    pair.answer,
                    len(pair.question) + len(pair.answer),
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


__all__ = ["QAPair", "pair_items", "emit_qa_pairs", "QA_SOURCE_PREFIX"]
