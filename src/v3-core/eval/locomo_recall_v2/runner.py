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
    MODE_OBJECTIVE,
    MODE_STRUCTURAL,
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
    "MODE_OBJECTIVE",
    "MODE_STRUCTURAL",
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
      * ``adapter_mode`` — forwarded to the adapter so the per-case
        ``q_emb`` resolution policy is consistent end-to-end.
        ``"structural"`` keeps the legacy single-``q_emb`` path;
        ``"objective"`` enables the per-case
        ``q_emb_by_case`` mapping with strict dimension and
        non-zero validation.
      * ``expected_dim`` — required when ``adapter_mode ==
        "objective"``; the per-case ``q_emb`` vector MUST have
        exactly this many elements.  Ignored in structural mode.
      * ``limit`` / ``max_chars`` — forwarded to the adapter.
      * ``hit_at_k_limit`` — used by :func:`run_cases_with_metrics`
        when the caller wants the metrics computed inline.

    Default ``limit`` matches the production facade default
    (``5``); ``max_chars`` stays ``None`` so the adapter's own
    ``DEFAULT_INJECTION_MAX_CHARS`` is used.
    """

    mode: str = "full"
    adapter_mode: str = MODE_STRUCTURAL
    expected_dim: Optional[int] = None
    limit: int = 5
    max_chars: Optional[int] = None
    # Hit@K is reported at the observable ranking depth
    # (``ranking_limit``).  The default 5 mirrors the production
    # facade default so the default ``run_cases_with_metrics``
    # call never reports an N/A ``hit_at_k`` slot.
    hit_at_k_limit: int = 5

    def __post_init__(self) -> None:
        # The mode flag is FROZEN today.  Future offline paths MUST
        # add an explicit implementation — silent fallback is
        # forbidden by the spec.
        if self.mode != "full":
            raise NotImplementedError(
                f"runner mode {self.mode!r} is not implemented; "
                "only 'full' is supported today."
            )
        if self.adapter_mode not in (MODE_STRUCTURAL, MODE_OBJECTIVE):
            raise ValueError(
                f"adapter_mode must be {MODE_STRUCTURAL!r} or "
                f"{MODE_OBJECTIVE!r}; got {self.adapter_mode!r}"
            )
        if self.adapter_mode == MODE_OBJECTIVE:
            if self.expected_dim is None or int(self.expected_dim) <= 0:
                raise ValueError(
                    "RunnerConfig with adapter_mode='objective' requires a "
                    f"positive expected_dim (got {self.expected_dim!r})"
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
    gold_evidence_dia_ids: Sequence[str] = (),
    gold_source_ids: Sequence[str] = (),
    unmapped_dia_ids: Sequence[str] = (),
    unresolved_evidence: Sequence[str] = (),
    gold_evidence: Any = None,
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
    adapter_mode: str = MODE_STRUCTURAL,
    q_emb_by_case: Mapping[str, Sequence[float]] | None = None,
    expected_dim: Optional[int] = None,
) -> CaseRecord:
    """Run one benchmark case via the adapter.

    This is a thin passthrough to :func:`adapter.run_case`.  The
    runner keeps the function so external callers can use a
    single import path (``runner.run_case``) without depending on
    the adapter module.

    The ``adapter_mode`` / ``q_emb_by_case`` / ``expected_dim``
    trio is forwarded verbatim so the per-case ``q_emb`` policy
    is consistent across the runner and adapter surfaces.  When
    ``adapter_mode='objective'`` the caller MUST supply a
    ``q_emb_by_case`` mapping containing an entry for every case
    and a positive ``expected_dim``; missing keys or wrong
    dimensions are surfaced by the adapter as
    ``status='invalid_q_emb'`` records so a single degenerate
    case cannot abort the whole batch.
    """
    return _adapter_run_case(
        sample_id=sample_id,
        query_idx=query_idx,
        question=question,
        gold_answer=gold_answer,
        gold_evidence_dia_ids=gold_evidence_dia_ids,
        gold_source_ids=gold_source_ids,
        unmapped_dia_ids=unmapped_dia_ids,
        unresolved_evidence=unresolved_evidence,
        gold_evidence=gold_evidence,
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
        mode=adapter_mode,
        q_emb_by_case=q_emb_by_case,
        expected_dim=expected_dim,
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
    adapter_mode: str = MODE_STRUCTURAL,
    q_emb_by_case: Mapping[str, Sequence[float]] | None = None,
    expected_dim: Optional[int] = None,
) -> list[CaseRecord]:
    """Run a batch of cases via the adapter.

    Each ``case`` dict must carry:

      * ``sample_id`` — the LoCoMo sample id
      * ``query_idx`` — the within-sample query index (int)
      * ``question`` — the eval question text
      * ``answer`` — the gold answer text
      * ``gold_evidence_dia_ids`` — sequence of dia IDs
      * ``gold_source_ids`` — sequence of source IDs (mapped)
      * ``unmapped_dia_ids`` — optional sequence of unmapped dia IDs
      * ``unresolved_evidence`` — optional sequence of unresolved dia IDs
      * ``gold_evidence`` — optional GoldEvidence-shaped object used
        to fill gaps when explicit fields are absent
      * ``category`` — optional LoCoMo category label
      * ``session_id`` / ``conversation_id`` — optional identifiers

    ``case_id`` is derived as ``f"{sample_id}|{query_idx}"`` — the
    runner NEVER uses row order to construct an id, so the
    sibling ``compare.py`` stays stable across re-runs.

    ``mode`` is forwarded to :class:`RunnerConfig` and validated.
    Today only ``"full"`` is accepted; any other value raises
    :class:`NotImplementedError` so a future caller cannot
    silently degrade the run.

    The per-case ``q_emb`` policy is determined by ``adapter_mode``:

      * ``"structural"`` (default) — the ``q_emb`` vector is
        forwarded to every case unchanged.
      * ``"objective"`` — ``q_emb_by_case`` MUST contain an entry
        for every case ``case_id`` and ``expected_dim`` MUST be
        positive.  The runner stitches the per-case vector
        deterministically (in the iteration order of ``cases``)
        and the adapter validates dimension + non-zero per case.
        A degenerate case is reported via
        ``status='invalid_q_emb'`` instead of aborting the batch.
    """
    rc = RunnerConfig(
        mode=mode,
        adapter_mode=adapter_mode,
        expected_dim=expected_dim,
        limit=limit,
        max_chars=max_chars,
    )
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
        case_sample_id = str(case["sample_id"])
        case_query_idx = int(case["query_idx"])
        case_id = f"{case_sample_id}|{case_query_idx}"
        # Per-case q_emb: in objective mode the mapping is the
        # canonical source of truth — the adapter reads it and
        # fails closed on missing keys / wrong dimension.  In
        # structural mode the runner still computes
        # ``q_emb_by_case`` for the single-``q_emb`` case so the
        # adapter can pass it through (a missing entry falls
        # back to ``None`` which the legacy facade treats as
        # "no vector").
        per_case_emb: Sequence[float] | None
        if rc.adapter_mode == MODE_OBJECTIVE:
            # Per-case mapping is the canonical source in
            # objective mode.  A missing key is a hard
            # configuration error — raise ``ValueError`` here so
            # the caller sees the exact case_id that broke the
            # contract.  The adapter additionally validates
            # dimension + non-zero (degenerate vectors) and
            # returns an ``invalid_q_emb`` record; the post-append
            # check below translates that into ``RuntimeError``.
            if q_emb_by_case is None or case_id not in q_emb_by_case:
                raise ValueError(
                    f"runner.run_cases objective mode missing q_emb for "
                    f"case_id={case_id!r} (q_emb_by_case must cover every case)"
                )
            per_case_emb = q_emb_by_case[case_id]
        else:
            per_case_emb = q_emb
        out.append(run_case(
            sample_id=case_sample_id,
            query_idx=case_query_idx,
            question=str(case.get("question", "") or ""),
            gold_answer=str(case.get("answer", "") or ""),
            gold_evidence_dia_ids=tuple(case.get("gold_evidence_dia_ids", ()) or ()),
            gold_source_ids=tuple(case.get("gold_source_ids", ()) or ()),
            unmapped_dia_ids=tuple(case.get("unmapped_dia_ids", ()) or ()),
            unresolved_evidence=tuple(case.get("unresolved_evidence", ()) or ()),
            gold_evidence=case.get("gold_evidence"),
            category=str(case.get("category", "") or ""),
            limit=rc.limit,
            max_chars=rc.max_chars,
            session_id=case.get("session_id"),
            conversation_id=case.get("conversation_id"),
            config=config,
            pg=pg,
            q_emb=per_case_emb,
            core=core,
            deadline=deadline,
            adapter_mode=rc.adapter_mode,
            q_emb_by_case=q_emb_by_case,
            expected_dim=rc.expected_dim,
        ))
        # Objective mode is fail-closed: a degenerate per-case
        # vector (missing key, wrong dimension, all-zero) MUST
        # abort the batch rather than silently producing a record
        # that downstream consumers might score as a normal hit.
        if (
            rc.adapter_mode == MODE_OBJECTIVE
            and out[-1].status == "invalid_q_emb"
        ):
            raise RuntimeError(
                f"runner.run_cases aborted in objective mode: case_id="
                f"{out[-1].case_id!r} produced status='invalid_q_emb' "
                f"({out[-1].error!r})"
            )
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
    adapter_mode: str = MODE_STRUCTURAL,
    q_emb_by_case: Mapping[str, Sequence[float]] | None = None,
    expected_dim: Optional[int] = None,
    engine_invocations: int = -1,
) -> tuple[list[CaseRecord], MetricSnapshot]:
    """Run a batch of cases and return both records and metrics.

    Convenience helper for the CLI: the records are the per-case
    detail, the metrics are the aggregate headline.  Both are
    returned in the same call so the caller does not have to
    re-run the engine to compute metrics.

    The ``adapter_mode`` / ``q_emb_by_case`` / ``expected_dim``
    trio is forwarded to :func:`run_cases` so the per-case
    ``q_emb`` policy is identical to the non-metrics batch path.
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
        adapter_mode=adapter_mode,
        q_emb_by_case=q_emb_by_case,
        expected_dim=expected_dim,
    )
    # Resolve the runner config once so we honor the SAME
    # ``hit_at_k_limit`` validation ``run_cases`` already applied
    # (mode/limit/objectivity gates).  ``ranking_limit`` is the
    # actual facade ``limit`` the adapter just used — this keeps
    # the metric-K truth contract honest: Hit@K is never reported
    # beyond the depth the adapter actually surfaced.
    rc_for_metrics = RunnerConfig(
        mode=mode,
        adapter_mode=adapter_mode,
        expected_dim=expected_dim,
        limit=limit,
        max_chars=max_chars,
    )
    snap = compute_metrics(
        records,
        hit_at_k_limit=rc_for_metrics.hit_at_k_limit,
        ranking_limit=int(rc_for_metrics.limit),
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