# -*- coding: utf-8 -*-
"""G6C-A LoCoMo Recall V2 retrieval metrics.

Trace-aware retrieval metrics computed over the typed
``CaseRecord`` produced by :mod:`adapter`.  All metrics are
computed over **mapped** gold ``source_id``s only — unmapped gold
dia IDs (cases where the dataset could not resolve evidence to a
``source_id``) are kept explicit but do not contribute to
hit/rank scoring.

Coverage audit
==============

The metrics module emits a deterministic ``CoverageAudit`` block
alongside the headline metrics.  This audit is what an evaluator
uses to detect silent data loss:

  * ``questions_total`` — every case counted once
  * ``engine_invocations`` — sum of fake-engine invocations
  * ``traces`` — distinct ``trace_id`` values seen
  * ``candidates`` — sum of candidate source IDs (every snapshot
    the engine recorded, dedup-preserving-order) across all
    traces — distinct from ``selected`` so coverage can detect
    "engine surfaced X but only ranked Y"
  * ``selected`` — sum of selected candidate counts
  * ``injection_outcomes`` — sum of injected candidate counts
  * ``gold_evidence`` — total mapped gold ``source_id`` count
  * ``gold_evidence_retrieved`` — mapped gold source IDs that
    were retrieved (in ranked or selected lists)
  * ``bypasses`` — cases where the adapter reported a status that
    suggests it did not exercise the production facade

The audit is honest: it counts what the typed trace surface
reports, never what the adapter claims to have done.

Honest status
=============

When the eval set has no mapped gold evidence we report an
explicit ``status="no_mapped_evidence"`` rather than fabricate a
zero score.  When answer-scoring is not available we report
``status="answer_scoring_unavailable"`` — a separate field is kept
so callers can distinguish.

Comparator
==========

A changed-case comparator IS NOT implemented here — the sibling
``compare.py`` already owns JSONL file-vs-file diffing.  Importing
or re-exporting it from this module would invite overlap.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Optional, Sequence


__all__ = [
    "CoverageAudit",
    "MetricSnapshot",
    "StatusFlag",
    "compute_metrics",
    "compute_per_category",
    "compute_coverage_audit",
    "summarise_gold_evidence",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


class StatusFlag(str):
    """Sentinel status codes for the metrics module."""

    OK = "ok"
    NO_MAPPED_EVIDENCE = "no_mapped_evidence"
    ANSWER_SCORING_UNAVAILABLE = "answer_scoring_unavailable"


@dataclasses.dataclass(frozen=True)
class CoverageAudit:
    """Honest coverage audit over a set of cases.

    Every field is a count, never a fraction.  When a count is
    unknown (e.g. ``traces`` cannot be derived from
    ``CaseRecord`` alone) the value is ``-1`` so callers can tell
    the difference between "zero" and "missing".
    """

    questions_total: int
    engine_invocations: int
    traces: int
    candidates: int
    selected: int
    injection_outcomes: int
    gold_evidence: int
    gold_evidence_retrieved: int
    bypasses: int

    def to_dict(self) -> dict[str, int]:
        return {
            "questions_total": int(self.questions_total),
            "engine_invocations": int(self.engine_invocations),
            "traces": int(self.traces),
            "candidates": int(self.candidates),
            "selected": int(self.selected),
            "injection_outcomes": int(self.injection_outcomes),
            "gold_evidence": int(self.gold_evidence),
            "gold_evidence_retrieved": int(self.gold_evidence_retrieved),
            "bypasses": int(self.bypasses),
        }


@dataclasses.dataclass(frozen=True)
class MetricSnapshot:
    """Per-run aggregate retrieval metrics.

    All numeric fields are computed strictly over MAPPED gold
    source IDs.  ``status`` reports the explicit run-level state
    (e.g. ``"no_mapped_evidence"`` when the dataset had no
    mappable evidence at all).
    """

    status: str
    questions_total: int
    questions_with_mapped_evidence: int
    questions_with_unmapped_evidence: int
    mapped_gold_count: int
    unmapped_gold_count: int
    hit_at_1: float
    hit_at_5: float
    hit_at_k: float
    mrr: float
    mean_relevant_rank: float
    per_category: tuple[tuple[str, dict[str, float]], ...]
    coverage: CoverageAudit

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "questions_total": int(self.questions_total),
            "questions_with_mapped_evidence": int(self.questions_with_mapped_evidence),
            "questions_with_unmapped_evidence": int(self.questions_with_unmapped_evidence),
            "mapped_gold_count": int(self.mapped_gold_count),
            "unmapped_gold_count": int(self.unmapped_gold_count),
            "hit_at_1": float(self.hit_at_1),
            "hit_at_5": float(self.hit_at_5),
            "hit_at_k": float(self.hit_at_k),
            "mrr": float(self.mrr),
            "mean_relevant_rank": float(self.mean_relevant_rank),
            "per_category": [
                {"category": cat, **vals}
                for cat, vals in self.per_category
            ],
            "coverage": self.coverage.to_dict(),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hit_at_k(ranked_ids: Sequence[str], gold: set[str], k: int) -> float:
    """Return 1.0 iff any id in ``gold`` appears in the first ``k``
    ranked ids, else 0.0.
    """
    if k <= 0:
        return 0.0
    head = ranked_ids[:k]
    return 1.0 if any(g in head for g in gold) else 0.0


def _first_relevant_rank(ranked_ids: Sequence[str], gold: set[str]) -> Optional[int]:
    """Return the 1-based rank of the first relevant id, or ``None``."""
    for i, sid in enumerate(ranked_ids):
        if sid in gold:
            return i + 1
    return None


def _safe_mean(values: Iterable[float]) -> float:
    """Mean of ``values``; ``0.0`` when empty."""
    seq = list(values)
    if not seq:
        return 0.0
    return float(sum(seq) / len(seq))


def summarise_gold_evidence(cases: Sequence[Any]) -> tuple[int, int]:
    """Return ``(mapped_count, unmapped_count)`` summed across cases.

    ``mapped_count`` is the total number of mapped gold source IDs
    (every ``case.gold_source_ids`` contributes its length).
    ``unmapped_count`` is the total number of unresolved dia IDs.
    """
    mapped = 0
    unmapped = 0
    for c in cases or ():
        try:
            mapped += len(getattr(c, "gold_source_ids", ()) or ())
        except Exception:
            pass
        try:
            unmapped += len(getattr(c, "unresolved_evidence", ()) or ())
        except Exception:
            pass
    return mapped, unmapped


# ---------------------------------------------------------------------------
# Per-case scoring
# ---------------------------------------------------------------------------


def _score_case(
    case: Any,
    *,
    hit_at_k_limit: int,
) -> dict[str, Any]:
    """Compute per-case retrieval stats over MAPPED gold source IDs.

    Unmapped gold dia IDs are counted in the ``unmapped_count``
    field but contribute nothing to hit/rank.  When the case has
    no mapped gold, ``hit`` / ``mrr`` / ``relevant_rank`` stay at
    ``0.0`` / ``None`` respectively — the caller can detect the
    "no mapped evidence" condition via ``gold_count == 0``.
    """
    gold: list[str] = list(getattr(case, "gold_source_ids", ()) or ())
    ranked: list[str] = list(getattr(case, "ranked_source_ids", ()) or ())
    selected: list[str] = list(getattr(case, "selected_source_ids", ()) or ())
    injected: list[str] = list(getattr(case, "injected_source_ids", ()) or ())
    gold_set = set(gold)

    hit_1 = _hit_at_k(ranked, gold_set, 1) if gold else 0.0
    hit_5 = _hit_at_k(ranked, gold_set, 5) if gold else 0.0
    hit_k = _hit_at_k(ranked, gold_set, hit_at_k_limit) if gold else 0.0
    relevant_rank = _first_relevant_rank(ranked, gold_set) if gold else None
    mrr = (1.0 / relevant_rank) if relevant_rank else 0.0
    # Hit at K for SELECTED list — separate signal: did the adapter
    # actually promote any relevant item to selection?
    hit_k_selected = _hit_at_k(selected, gold_set, hit_at_k_limit) if gold else 0.0

    # gold evidence retrieved: did ANY retrieved surface (ranked OR
    # selected OR injected) include a mapped gold source id?
    retrieved_surfaces = set(ranked) | set(selected) | set(injected)
    gold_retrieved = sum(1 for g in gold if g in retrieved_surfaces)

    return {
        "category": str(getattr(case, "category", "") or ""),
        "hit_at_1": hit_1,
        "hit_at_5": hit_5,
        "hit_at_k": hit_k,
        "mrr": mrr,
        "relevant_rank": relevant_rank,
        "hit_at_k_selected": hit_k_selected,
        "gold_count": len(gold),
        "unmapped_count": len(getattr(case, "unresolved_evidence", ()) or ()),
        "gold_retrieved": int(gold_retrieved),
        "ranked_count": len(ranked),
        "selected_count": len(selected),
        "injected_count": len(injected),
    }


def compute_per_category(
    cases: Sequence[Any],
    *,
    hit_at_k_limit: int,
) -> tuple[tuple[str, dict[str, float]], ...]:
    """Aggregate :func:`_score_case` per category.

    The category label is the ``case.category`` field; missing or
    empty labels fold into the bucket ``"__unlabelled__"`` so the
    output is exhaustive.
    """
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for c in cases or ():
        per = _score_case(c, hit_at_k_limit=hit_at_k_limit)
        cat = per["category"] or "__unlabelled__"
        by_cat.setdefault(cat, []).append(per)

    out: list[tuple[str, dict[str, float]]] = []
    for cat in sorted(by_cat.keys()):
        rows = by_cat[cat]
        n = len(rows)
        out.append((
            cat,
            {
                "questions": float(n),
                "questions_with_evidence": float(
                    sum(1 for r in rows if r["gold_count"] > 0)
                ),
                "hit_at_1": _safe_mean(r["hit_at_1"] for r in rows),
                "hit_at_5": _safe_mean(r["hit_at_5"] for r in rows),
                "hit_at_k": _safe_mean(r["hit_at_k"] for r in rows),
                "mrr": _safe_mean(r["mrr"] for r in rows),
                "mean_relevant_rank": (
                    _safe_mean(
                        float(r["relevant_rank"])
                        for r in rows
                        if r["relevant_rank"] is not None
                    )
                ),
            },
        ))
    return tuple(out)


# ---------------------------------------------------------------------------
# Coverage audit
# ---------------------------------------------------------------------------


def compute_coverage_audit(
    cases: Sequence[Any],
    *,
    engine_invocations: int = 0,
) -> CoverageAudit:
    """Build a :class:`CoverageAudit` over the supplied cases.

    ``engine_invocations`` is an OPTIONAL hint — when the caller
    knows the exact count of engine seam invocations during the
    run (e.g. ``CountingFakeRecallFn._counter``), pass it for an
    accurate audit.  When ``-1`` we report ``-1`` to signal "not
    measured".
    """
    distinct_traces: set[str] = set()
    total_candidates = 0
    total_selected = 0
    total_injection = 0
    gold_total = 0
    gold_retrieved_total = 0
    bypasses = 0
    questions_total = 0
    for c in cases or ():
        questions_total += 1
        # trace_id: every record carries one (production contract).
        tid = str(getattr(c, "trace_id", "") or "")
        is_bypass = False
        if tid:
            distinct_traces.add(tid)
        else:
            # An empty trace id is a strong signal the facade
            # never ran.  Count as a bypass candidate.
            is_bypass = True
        # ranked / selected / candidate / injected counts come from
        # the typed record, not the trace, because the adapter
        # surfaces them as tuples.  ``candidate_source_ids`` is the
        # canonical coverage input — counting ranked/selected here
        # would double-count and misrepresent "what the engine
        # surfaced" vs "what the engine chose to rank".
        try:
            ranked = list(getattr(c, "ranked_source_ids", ()) or ())
            selected = list(getattr(c, "selected_source_ids", ()) or ())
            candidates = list(getattr(c, "candidate_source_ids", ()) or ())
            injected = list(getattr(c, "injected_source_ids", ()) or ())
            total_candidates += len(candidates)
            total_selected += len(selected)
            total_injection += len(injected)
        except Exception:
            pass
        # gold retrieval — over MAPPED source IDs.
        gold = list(getattr(c, "gold_source_ids", ()) or ())
        gold_total += len(gold)
        surfaces = set(ranked) | set(selected) | set(injected)
        for g in gold:
            if g in surfaces:
                gold_retrieved_total += 1
        # Status string reflects whether the adapter raised.
        status = str(getattr(c, "status", "") or "")
        if status and status != "ok":
            # Engine_error / non-ok status suggests a bypass or a
            # failed call.  We do NOT count empty status as a
            # bypass — only "ok" or empty is fine.
            is_bypass = True
        if is_bypass:
            bypasses += 1

    return CoverageAudit(
        questions_total=int(questions_total),
        engine_invocations=int(engine_invocations),
        traces=len(distinct_traces),
        candidates=int(total_candidates),
        selected=int(total_selected),
        injection_outcomes=int(total_injection),
        gold_evidence=int(gold_total),
        gold_evidence_retrieved=int(gold_retrieved_total),
        bypasses=int(bypasses),
    )


# ---------------------------------------------------------------------------
# Top-level metrics
# ---------------------------------------------------------------------------


def compute_metrics(
    cases: Sequence[Any],
    *,
    hit_at_k_limit: int = 30,
    engine_invocations: int = -1,
) -> MetricSnapshot:
    """Compute retrieval metrics over ``cases``.

    Behaviour:

      * Headline metrics are computed over MAPPED gold source IDs
        only.  Unmapped dia IDs contribute to the
        ``unmapped_gold_count`` field but never to Hit/MRR.
      * When the entire run has zero mapped evidence, status is
        set to ``"no_mapped_evidence"``; the headline metrics
        stay at ``0.0`` and the coverage audit reports the
        underlying counts so callers can see why.
      * When ``engine_invocations`` is left at ``-1`` the audit
        reflects "not measured" rather than fabricate a count.
      * Per-category breakdown is always emitted, including for
        the ``"__unlabelled__"`` bucket when categories are
        missing.
    """
    questions_total = len(cases or ())
    per_scores = [
        _score_case(c, hit_at_k_limit=hit_at_k_limit)
        for c in cases or ()
    ]
    mapped_total = sum(p["gold_count"] for p in per_scores)
    unmapped_total = sum(p["unmapped_count"] for p in per_scores)
    q_with_evidence = sum(1 for p in per_scores if p["gold_count"] > 0)
    q_with_unmapped = sum(1 for p in per_scores if p["unmapped_count"] > 0)

    if mapped_total == 0:
        status = StatusFlag.NO_MAPPED_EVIDENCE
    else:
        status = StatusFlag.OK

    # Headline aggregates — only over cases with mapped evidence.
    scorable = [p for p in per_scores if p["gold_count"] > 0]
    hit_at_1 = _safe_mean(p["hit_at_1"] for p in scorable)
    hit_at_5 = _safe_mean(p["hit_at_5"] for p in scorable)
    hit_at_k = _safe_mean(p["hit_at_k"] for p in scorable)
    mrr = _safe_mean(p["mrr"] for p in scorable)
    relevant_ranks = [float(p["relevant_rank"]) for p in scorable if p["relevant_rank"] is not None]
    mean_relevant_rank = _safe_mean(relevant_ranks)

    per_category = compute_per_category(
        cases, hit_at_k_limit=hit_at_k_limit,
    )

    audit = compute_coverage_audit(
        cases,
        engine_invocations=engine_invocations,
    )

    return MetricSnapshot(
        status=status,
        questions_total=int(questions_total),
        questions_with_mapped_evidence=int(q_with_evidence),
        questions_with_unmapped_evidence=int(q_with_unmapped),
        mapped_gold_count=int(mapped_total),
        unmapped_gold_count=int(unmapped_total),
        hit_at_1=float(hit_at_1),
        hit_at_5=float(hit_at_5),
        hit_at_k=float(hit_at_k),
        mrr=float(mrr),
        mean_relevant_rank=float(mean_relevant_rank),
        per_category=per_category,
        coverage=audit,
    )