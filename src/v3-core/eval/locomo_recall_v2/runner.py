# -*- coding: utf-8 -*-
"""G6C-A LoCoMo Recall V2 case runner.

Thin orchestration over :mod:`adapter` and :mod:`metrics`.  The
runner owns:

  * :func:`run_case` — direct passthrough to :func:`adapter.run_case`
    for a single eval row.
  * :func:`run_cases` — batch wrapper that delegates to the adapter
    for every eval row, preserving order and never silently
    switching to a keyword-only or synthetic-vector path in
    ``mode="full"``.
  * :func:`write_results_jsonl` — JSONL writer that emits one
    record per case using the adapter's
    :meth:`CaseRecord.to_dict` shape.

Stable ``case_id``
==================

The runner guarantees ``case_id = f"{sample_id}|{query_idx}"``
across all invocations — never row order, never a hash of the
question.  This is what the sibling ``compare.py`` reads to
differential two runs, so any drift here would silently break
the diff tool.

Honest ``mode`` flag
====================

``mode`` is an explicit string the caller passes through:

  * ``"full"`` (default) — exercises the production facade via
    the adapter, exactly once per case.  The runner never
    secretly switches to a degraded path.
  * ``"offline"`` — reserved for a future offline deterministic
    shortcut.  The runner does NOT implement this path today; any
    caller that asks for ``mode != "full"`` gets a clear
    ``NotImplementedError`` so the contract cannot drift.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from typing import Any, Iterable, Optional, Sequence, TextIO


# Re-export the adapter's API so callers can use a single import.
from .adapter import (
    CaseRecord,
    DEFAULT_INCLUDE_FLAGS,
    build_effective_flags,
    get_prefetch_facade,
    resolve_trace_source_ids,
    run_case as _adapter_run_case,
)
from .metrics import (
    CoverageAudit,
    MetricSnapshot,
    StatusFlag,
    compute_coverage_audit,
    compute_metrics,
)


__all__ = [
    "CaseRecord",
    "DEFAULT_INCLUDE_FLAGS",
    "RunnerConfig",
    "build_effective_flags",
    "resolve_trace_source_ids",
    "run_case",
    "run_cases",
    "write_results_jsonl",
]


logger = logging.getLogger("v3core.eval.locomo_recall_v2.runner")


# ---------------------------------------------------------------------------
# Config + data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RunnerConfig:
    """Read-only configuration for :func:`run_cases`.

    The dataclass is the runner's single seam for all knobs:

      * ``mode`` — ``"full"`` only today; anything else raises.
      * ``limit`` / ``max_chars`` — forwarded to the adapter.
      * ``hit_at_k_limit`` — used by :func:`run_cases_with_metrics`
        when the caller wants the metrics computed inline.

    Default ``limit`` matches the production facade default
    (``5``); ``max_chars`` stays ``None`` so the adapter's own
    ``DEFAULT_INJECTION_MAX_CHARS`` is used.
    """

    mode: str = "full"
    limit: int = 5
    max_chars: Optional[int] = None
    hit_at_k_limit: int = 30

    def __post_init__(self) -> None:
        # The mode flag is FROZEN today.  Future offline paths MUST
        # add an explicit implementation — silent fallback is
        # forbidden by the spec.
        if self.mode != "full":
            raise NotImplementedError(
                f"runner mode {self.mode!r} is not implemented; "
                "only 'full' is supported today."
            )
        if int(self.limit) <= 0:
            raise ValueError(f"limit must be > 0 (got {self.limit!r})")
        if int(self.hit_at_k_limit) <= 0:
            raise ValueError(
                f"hit_at_k_limit must be > 0 (got {self.hit_at_k_limit!r})"
            )


# ---------------------------------------------------------------------------
# Single-case entry point
# ---------------------------------------------------------------------------


def run_case(
    *,
    sample_id: str,
    query_idx: int,
    question: str,
    gold_answer: str,
    gold_evidence_dia_ids: Sequence[str],
    gold_source_ids: Sequence[str],
    unresolved_evidence: Sequence[str] = (),
    category: str = "",
    limit: int = 5,
    max_chars: Optional[int] = None,
    session_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    config: Any | None = None,
    card_index: dict | None = None,
    pg: Any = None,
    q_emb: Sequence[float] | None = None,
    core: Any = None,
    is_new_session: bool = False,
    deadline: Any = None,
    pg_was_connected: bool = False,
) -> CaseRecord:
    """Run one benchmark case via the adapter.

    This is a thin passthrough to :func:`adapter.run_case`.  The
    runner keeps the function so external callers can use a
    single import path (``runner.run_case``) without depending on
    the adapter module.
    """
    return _adapter_run_case(
        sample_id=sample_id,
        query_idx=query_idx,
        question=question,
        gold_answer=gold_answer,
        gold_evidence_dia_ids=gold_evidence_dia_ids,
        gold_source_ids=gold_source_ids,
        unresolved_evidence=unresolved_evidence,
        category=category,
        limit=limit,
        max_chars=max_chars,
        session_id=session_id,
        conversation_id=conversation_id,
        config=config,
        card_index=card_index,
        pg=pg,
        q_emb=q_emb,
        core=core,
        is_new_session=is_new_session,
        deadline=deadline,
        pg_was_connected=pg_was_connected,
    )


# ---------------------------------------------------------------------------
# Batch entry point
# ---------------------------------------------------------------------------


def run_cases(
    cases: Iterable[dict[str, Any]],
    *,
    config: Any | None = None,
    pg: Any = None,
    q_emb: Sequence[float] | None = None,
    core: Any = None,
    max_chars: Optional[int] = None,
    limit: int = 5,
    deadline: Any = None,
    mode: str = "full",
) -> list[CaseRecord]:
    """Run a batch of cases via the adapter.

    Each ``case`` dict must carry:

      * ``sample_id`` — the LoCoMo sample id
      * ``query_idx`` — the within-sample query index (int)
      * ``question`` — the eval question text
      * ``answer`` — the gold answer text
      * ``gold_evidence_dia_ids`` — sequence of dia IDs
      * ``gold_source_ids`` — sequence of source IDs (mapped)
      * ``unresolved_evidence`` — optional sequence of unresolved dia IDs
      * ``category`` — optional LoCoMo category label
      * ``session_id`` / ``conversation_id`` — optional identifiers

    ``case_id`` is derived as ``f"{sample_id}|{query_idx}"`` — the
    runner NEVER uses row order to construct an id, so the
    sibling ``compare.py`` stays stable across re-runs.

    ``mode`` is forwarded to :class:`RunnerConfig` and validated.
    Today only ``"full"`` is accepted; any other value raises
    :class:`NotImplementedError` so a future caller cannot
    silently degrade the run.
    """
    rc = RunnerConfig(mode=mode, limit=limit, max_chars=max_chars)
    out: list[CaseRecord] = []
    for case in cases:
        # Stable case_id from sample_id + query_idx — verify the
        # caller supplies both keys; raise ValueError when missing
        # rather than silently defaulting.
        if "sample_id" not in case or "query_idx" not in case:
            raise ValueError(
                "run_cases requires every case to carry "
                "'sample_id' and 'query_idx'"
            )
        out.append(run_case(
            sample_id=str(case["sample_id"]),
            query_idx=int(case["query_idx"]),
            question=str(case.get("question", "") or ""),
            gold_answer=str(case.get("answer", "") or ""),
            gold_evidence_dia_ids=tuple(case.get("gold_evidence_dia_ids", ()) or ()),
            gold_source_ids=tuple(case.get("gold_source_ids", ()) or ()),
            unresolved_evidence=tuple(case.get("unresolved_evidence", ()) or ()),
            category=str(case.get("category", "") or ""),
            limit=rc.limit,
            max_chars=rc.max_chars,
            session_id=case.get("session_id"),
            conversation_id=case.get("conversation_id"),
            config=config,
            pg=pg,
            q_emb=q_emb,
            core=core,
            deadline=deadline,
        ))
    return out


def run_cases_with_metrics(
    cases: Iterable[dict[str, Any]],
    *,
    config: Any | None = None,
    pg: Any = None,
    q_emb: Sequence[float] | None = None,
    core: Any = None,
    max_chars: Optional[int] = None,
    limit: int = 5,
    deadline: Any = None,
    mode: str = "full",
    engine_invocations: int = -1,
) -> tuple[list[CaseRecord], MetricSnapshot]:
    """Run a batch of cases and return both records and metrics.

    Convenience helper for the CLI: the records are the per-case
    detail, the metrics are the aggregate headline.  Both are
    returned in the same call so the caller does not have to
    re-run the engine to compute metrics.
    """
    records = run_cases(
        cases,
        config=config,
        pg=pg,
        q_emb=q_emb,
        core=core,
        max_chars=max_chars,
        limit=limit,
        deadline=deadline,
        mode=mode,
    )
    snap = compute_metrics(
        records,
        hit_at_k_limit=RunnerConfig(mode=mode).hit_at_k_limit,
        engine_invocations=engine_invocations,
    )
    return records, snap


# ---------------------------------------------------------------------------
# JSONL writer
# ---------------------------------------------------------------------------


def write_results_jsonl(
    records: Sequence[CaseRecord],
    *,
    path: Optional[str] = None,
    stream: Optional[TextIO] = None,
) -> int:
    """Write one JSONL record per case.

    Either ``path`` or ``stream`` must be supplied; supplying
    both raises.  The writer appends a single ``\\n`` per line
    so the output is line-delimited JSON compatible with the
    sibling ``compare.load_run`` reader.

    Returns the number of lines written.  No header line is
    emitted — JSONL has no header; ``case_id`` is the key the
    diff tool reads.
    """
    if (path is None) == (stream is None):
        raise ValueError(
            "write_results_jsonl requires exactly one of path / stream"
        )
    own = False
    if stream is None:
        parent = os.path.dirname(os.path.abspath(str(path)))
        if parent:
            os.makedirs(parent, exist_ok=True)
        stream = open(str(path), "w", encoding="utf-8", newline="\n")
        own = True
    try:
        n = 0
        for rec in records or ():
            line = json.dumps(
                rec.to_dict(), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            stream.write(line)
            stream.write("\n")
            n += 1
        return n
    finally:
        if own:
            stream.close()