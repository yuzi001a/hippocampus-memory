# -*- coding: utf-8 -*-
"""G6C-A LoCoMo Recall V2 retrieval metrics.

Trace-aware retrieval metrics computed over the typed
``CaseRecord`` produced by :mod:`adapter`.  All headline
metrics are computed over **mapped** gold ``source_id``s only —
unmapped gold dia IDs (cases where the dataset could not resolve
evidence to a ``source_id``) and unresolved evidence dia IDs
(compound / malformed strings that cannot be mapped without
heuristic splitting) are kept explicit but do not contribute to
hit/rank scoring.

Metric K truth contract
=======================

The reported ``Hit@K`` is never greater than the **observable
ranking depth**.  The contract enforces:

  * ``ranking_limit`` is the production-shaped limit (default
    ``5``) — the maximum number of source IDs the adapter ever
    surfaces as ranked candidates.  A Hit@K above this value is
    fabricated and the snapshot must say so.
  * ``hit_at_k_k`` is the K the caller asked for.  When
    ``hit_at_k_k <= ranking_limit`` the ``hit_at_k`` slot reports
    a real fraction and ``hit_at_k_status = "ok"``.  When
    ``hit_at_k_k > ranking_limit`` the snapshot reports
    ``hit_at_k = NaN`` (Python ``float('nan')``) and
    ``hit_at_k_status = "n_a"`` so a downstream consumer can
    distinguish "real metric" from "not applicable" without
    silently swallowing the gap.
  * ``mrr`` is always computed over the **observed** ranking
    depth (``min(hit_at_k_k, ranking_limit)``) — never above
    what the adapter actually produced.
  * ``mean_relevant_rank`` reflects observed ranks only.

The default production shape reports ``Hit@1``, ``Hit@5`` (the
canonical ranked surface) and a ``hit_at_k_k=ranking_limit``
slot — ``ranking_limit=5`` is the canonical deployment, so
``hit_at_k == hit_at_5`` for the default snapshot.  A caller
that asks for ``hit_at_k_k=30`` against a ``ranking_limit=5``
deployment will see ``hit_at_k = NaN`` and
``hit_at_k_status = "n_a"``.

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

Three GoldEvidence states
=========================

The dataset's :class:`dataset.GoldEvidence` carries three
distinct lists: ``source_ids`` (mapped), ``unmapped_dia_ids``
(dia IDs whose evidence could not be resolved to a source_id),
and ``unresolved`` (compound / malformed strings).  The metrics
module preserves all three independently:

  * ``mapped_gold_count`` — sum of ``case.gold_source_ids``
    lengths (the headline denominator).
  * ``unmapped_gold_count`` — sum of ``case.unmapped_dia_ids``
    lengths.
  * ``unresolved_gold_count`` — sum of ``case.unresolved_evidence``
    lengths.

The headline metrics (``Hit@K``, ``MRR``, ``mean_relevant_rank``)
are computed over ``mapped_gold_count`` only; the two auxiliary
counters are surfaced so callers can audit evidence-mapping
coverage without rerunning the dataset loader.

Comparator
==========

A changed-case comparator IS NOT implemented here — the sibling
``compare.py`` already owns JSONL file-vs-file diffing.  Importing
or re-exporting it from this module would invite overlap.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any, Iterable, Optional, Sequence


__all__ = [
    "CoverageAudit",
    "MetricSnapshot",
    "StatusFlag",
    "compute_metrics",
    "compute_per_category",
    "compute_coverage_audit",
    "summarise_gold_evidence",
    "summarise_gold_evidence_extended",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


class StatusFlag(str):
    """Sentinel status codes for the metrics module."""

    OK = "ok"
    NO_MAPPED_EVIDENCE = "no_mapped_evidence"
    ANSWER_SCORING_UNAVAILABLE = "answer_scoring_unavailable"


#: Sentinel status for the hit_at_k slot.  "ok" means the
#: configured K is within the observable ranking depth and the
#: reported fraction is real.  "n_a" means the configured K is
#: greater than the ranking_limit so the slot is honestly NaN.
HIT_AT_K_STATUS_OK = "ok"
HIT_AT_K_STATUS_NA = "n_a"


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

    All numeric headline fields are computed strictly over MAPPED
    gold source IDs.  ``status`` reports the explicit run-level
    state (e.g. ``"no_mapped_evidence"`` when the dataset had no
    mappable evidence at all).

    The metric-K truth fields are:

      * ``ranking_limit`` — the observable ranking depth
        (production-shaped default ``5``).
      * ``hit_at_k_k`` — the K the caller asked for.
      * ``hit_at_k_status`` — ``"ok"`` when K <= ranking_limit;
        ``"n_a"`` when K > ranking_limit so the
        ``hit_at_k`` slot reports NaN instead of a fabricated
        fraction.
      * ``hit_at_k`` — the Hit@K fraction, or NaN when
        ``hit_at_k_status == "n_a"``.

    The three GoldEvidence counts are surfaced independently:

      * ``mapped_gold_count`` — the headline denominator.
      * ``unmapped_gold_count`` — kept explicit for audit.
      * ``unresolved_gold_count`` — kept explicit for audit.
    """

    status: str
    questions_total: int
    questions_with_mapped_evidence: int
    questions_with_unmapped_evidence: int
    questions_with_unresolved_evidence: int
    mapped_gold_count: int
    unmapped_gold_count: int
    unresolved_gold_count: int
    hit_at_1: float
    hit_at_1_status: str
    hit_at_5: float
    hit_at_5_status: str
    hit_at_k: float
    hit_at_k_k: int
    hit_at_k_status: str
    ranking_limit: int
    mrr: float
    mrr_k: int
    mean_relevant_rank: float
    per_category: tuple[tuple[str, dict[str, float]], ...]
    coverage: CoverageAudit

    def to_dict(self) -> dict[str, Any]:
        def _hit_or_none(value: float, status: str) -> float | None:
            """Return the value as-is, or ``None`` when N/A.

            JSON has no standard NaN literal; ``None`` plus the
            ``hit_at_k_status`` field is the explicit "N/A"
            contract.  Callers that want a sentinel can pattern-
            match on ``status == "n_a"`` (or the per-slot
            ``hit_at_5_status`` below).
            """
            if status == HIT_AT_K_STATUS_NA:
                return None
            return float(value)

        def _per_category_dict(
            cat: str,
            vals: dict[str, float],
        ) -> dict[str, Any]:
            """Convert one per-category dict to its JSON-safe form.

            Per-category Hit@K slots also fail closed (None)
            when the per-K status is "n_a" so a downstream
            consumer never sees a non-standard NaN literal.
            """
            out: dict[str, Any] = {"category": cat}
            for k, v in vals.items():
                if k in {"hit_at_1", "hit_at_5", "hit_at_k"}:
                    status_key = f"{k}_status"
                    status = str(vals.get(status_key) or HIT_AT_K_STATUS_OK)
                    out[k] = _hit_or_none(float(v), status)
                    out[status_key] = status
                else:
                    out[k] = float(v) if isinstance(v, (int, float)) else v
            return out

        return {
            "status": self.status,
            "questions_total": int(self.questions_total),
            "questions_with_mapped_evidence": int(self.questions_with_mapped_evidence),
            "questions_with_unmapped_evidence": int(self.questions_with_unmapped_evidence),
            "questions_with_unresolved_evidence": int(self.questions_with_unresolved_evidence),
            "mapped_gold_count": int(self.mapped_gold_count),
            "unmapped_gold_count": int(self.unmapped_gold_count),
            "unresolved_gold_count": int(self.unresolved_gold_count),
            "hit_at_1": _hit_or_none(self.hit_at_1, self.hit_at_1_status),
            "hit_at_1_status": str(self.hit_at_1_status),
            "hit_at_5": _hit_or_none(self.hit_at_5, self.hit_at_5_status),
            "hit_at_5_status": str(self.hit_at_5_status),
            "hit_at_k": _hit_or_none(self.hit_at_k, self.hit_at_k_status),
            "hit_at_k_k": int(self.hit_at_k_k),
            "hit_at_k_status": str(self.hit_at_k_status),
            "ranking_limit": int(self.ranking_limit),
            "mrr": float(self.mrr),
            "mrr_k": int(self.mrr_k),
            "mean_relevant_rank": float(self.mean_relevant_rank),
            "per_category": [
                _per_category_dict(cat, dict(vals))
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

    ``k`` must be a positive integer.  Non-positive values return
    0.0 — callers should validate ``K`` against
    ``ranking_limit`` before passing it in.
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

    Backwards-compatible two-tuple shape — kept so older
    consumers (and pre-existing tests) keep working.  New code
    should prefer :func:`summarise_gold_evidence_extended` which
    surfaces all three GoldEvidence states.

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


def summarise_gold_evidence_extended(
    cases: Sequence[Any],
) -> tuple[int, int, int]:
    """Return ``(mapped, unmapped, unresolved)`` summed across cases.

    Each component reflects one of the three distinct GoldEvidence
    states the dataset loader produces:

      * ``mapped`` — ``case.gold_source_ids`` length (the
        headline denominator for Hit/MRR scoring).
      * ``unmapped`` — ``case.unmapped_dia_ids`` length (dia
        IDs whose evidence could not be resolved to a source_id;
        contributes zero to hit scoring).
      * ``unresolved`` — ``case.unresolved_evidence`` length
        (compound / malformed evidence strings; contributes zero
        to hit scoring).

    The function never raises: every count is guarded by an
    attribute-existence check so a partial ``CaseRecord`` (e.g.
    a stub built in a test) cannot poison the totals.
    """
    mapped = 0
    unmapped = 0
    unresolved = 0
    for c in cases or ():
        try:
            mapped += len(getattr(c, "gold_source_ids", ()) or ())
        except Exception:
            pass
        try:
            unmapped += len(getattr(c, "unmapped_dia_ids", ()) or ())
        except Exception:
            pass
        try:
            unresolved += len(getattr(c, "unresolved_evidence", ()) or ())
        except Exception:
            pass
    return mapped, unmapped, unresolved


# ---------------------------------------------------------------------------
# Per-case scoring
# ---------------------------------------------------------------------------


def _score_case(
    case: Any,
    *,
    hit_at_k_limit: int,
    ranking_limit: int,
) -> dict[str, Any]:
    """Compute per-case retrieval stats over MAPPED gold source IDs.

    The returned ``hit_at_k`` slot is the OBSERVED Hit@K where
    ``K = min(hit_at_k_limit, ranking_limit)`` — never above the
    observable ranking depth.  Unmapped gold dia IDs are
    counted in the ``unmapped_count`` field but contribute
    nothing to hit/rank.  Unresolved evidence is counted in
    ``unresolved_count``.  When the case has no mapped gold,
    ``hit`` / ``mrr`` / ``relevant_rank`` stay at ``0.0`` /
    ``None`` respectively — the caller can detect the
    "no mapped evidence" condition via ``gold_count == 0``.
    """
    gold: list[str] = list(getattr(case, "gold_source_ids", ()) or ())
    ranked: list[str] = list(getattr(case, "ranked_source_ids", ()) or ())
    selected: list[str] = list(getattr(case, "selected_source_ids", ()) or ())
    injected: list[str] = list(getattr(case, "injected_source_ids", ()) or ())
    gold_set = set(gold)

    # Per-case observed K — never above ranking_limit so the
    # per-case slot cannot fabricate hits beyond observable depth.
    observed_k = max(0, min(int(hit_at_k_limit), int(ranking_limit)))

    hit_1 = _hit_at_k(ranked, gold_set, 1) if gold else 0.0
    hit_5 = _hit_at_k(ranked, gold_set, 5) if gold else 0.0
    hit_k = _hit_at_k(ranked, gold_set, observed_k) if gold else 0.0
    relevant_rank = _first_relevant_rank(ranked, gold_set) if gold else None
    # Per-case MRR — reciprocal of the observed relevant rank,
    # capped at observed_k so a relevant doc that surfaces beyond
    # the observable ranking depth never inflates MRR.
    if relevant_rank is None:
        per_case_mrr = 0.0
    elif observed_k <= 0:
        per_case_mrr = 0.0
    elif relevant_rank > observed_k:
        # The relevant doc exists but is beyond the observed
        # depth — for the per-case MRR we still report 0.0 so
        # the headline / per-category means reflect what the
        # deployment could actually surface.
        per_case_mrr = 0.0
    else:
        per_case_mrr = 1.0 / relevant_rank
    # Hit at K for SELECTED list — separate signal: did the adapter
    # actually promote any relevant item to selection?
    hit_k_selected = _hit_at_k(selected, gold_set, observed_k) if gold else 0.0

    # gold evidence retrieved: did ANY retrieved surface (ranked OR
    # selected OR injected) include a mapped gold source id?
    retrieved_surfaces = set(ranked) | set(selected) | set(injected)
    gold_retrieved = sum(1 for g in gold if g in retrieved_surfaces)

    return {
        "category": str(getattr(case, "category", "") or ""),
        "hit_at_1": hit_1,
        "hit_at_5": hit_5,
        "hit_at_k": hit_k,
        "mrr": per_case_mrr,
        "relevant_rank": relevant_rank,
        "hit_at_k_selected": hit_k_selected,
        "gold_count": len(gold),
        "unmapped_count": len(getattr(case, "unmapped_dia_ids", ()) or ()),
        "unresolved_count": len(getattr(case, "unresolved_evidence", ()) or ()),
        "gold_retrieved": int(gold_retrieved),
        "ranked_count": len(ranked),
        "selected_count": len(selected),
        "injected_count": len(injected),
        "observed_k": int(observed_k),
    }


def compute_per_category(
    cases: Sequence[Any],
    *,
    hit_at_k_limit: int,
    ranking_limit: int,
) -> tuple[tuple[str, dict[str, float]], ...]:
    """Aggregate :func:`_score_case` per category.

    The category label is the ``case.category`` field; missing or
    empty labels fold into the bucket ``"__unlabelled__"`` so the
    output is exhaustive.

    Per-case ``hit_at_k`` is computed at the **observed K**
    (``min(hit_at_k_limit, ranking_limit)``) — never above
    ranking depth — so the per-category fraction cannot fabricate
    a hit beyond observable ranking.
    """
    safe_hit_k_limit = max(0, int(hit_at_k_limit))
    safe_ranking_limit = max(0, int(ranking_limit))
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for c in cases or ():
        per = _score_case(
            c,
            hit_at_k_limit=hit_at_k_limit,
            ranking_limit=ranking_limit,
        )
        cat = per["category"] or "__unlabelled__"
        by_cat.setdefault(cat, []).append(per)

    out: list[tuple[str, dict[str, float]]] = []
    for cat in sorted(by_cat.keys()):
        rows = by_cat[cat]
        n = len(rows)
        observed_k = int(rows[0]["observed_k"]) if rows else 0
        # When K is N/A for the whole category the hit_at_k slot
        # surfaces as None (JSON) so callers can distinguish
        # "real metric" from "not applicable" without non-
        # standard NaN literals.
        hit_k_vals = list(rows[i]["hit_at_k"] for i in range(n))
        hit_at_k_mean: float | None
        if observed_k <= 0:
            hit_at_k_mean = None
        else:
            hit_at_k_mean = _safe_mean(hit_k_vals)
        # Per-category hit_at_5 / hit_at_1 also fail closed
        # when the observable depth is shallower than the slot.
        hit_at_1_vals = list(rows[i]["hit_at_1"] for i in range(n))
        hit_at_5_vals = list(rows[i]["hit_at_5"] for i in range(n))
        hit_at_1_mean = (
            _safe_mean(hit_at_1_vals)
            if 1 <= safe_ranking_limit else None
        )
        hit_at_5_mean = (
            _safe_mean(hit_at_5_vals)
            if 5 <= safe_ranking_limit else None
        )
        out.append((
            cat,
            {
                "questions": float(n),
                "questions_with_evidence": float(
                    sum(1 for r in rows if r["gold_count"] > 0)
                ),
                "hit_at_1": float(hit_at_1_mean) if hit_at_1_mean is not None else float("nan"),
                "hit_at_1_status": (
                    HIT_AT_K_STATUS_OK if 1 <= safe_ranking_limit else HIT_AT_K_STATUS_NA
                ),
                "hit_at_5": float(hit_at_5_mean) if hit_at_5_mean is not None else float("nan"),
                "hit_at_5_status": (
                    HIT_AT_K_STATUS_OK if 5 <= safe_ranking_limit else HIT_AT_K_STATUS_NA
                ),
                "hit_at_k": (
                    float(hit_at_k_mean)
                    if hit_at_k_mean is not None
                    else float("nan")
                ),
                "hit_at_k_k": float(observed_k),
                "hit_at_k_status": (
                    HIT_AT_K_STATUS_OK
                    if observed_k > 0 and safe_hit_k_limit <= safe_ranking_limit
                    else HIT_AT_K_STATUS_NA
                ),
                "mrr": _safe_mean(r["mrr"] for r in rows),
                # ``mean_relevant_rank`` only averages ranks that
                # fall WITHIN the observable ranking depth — a
                # relevant doc that surfaces beyond observed_k
                # never enters the mean so the metric cannot
                # silently claim a measurement from a rank the
                # adapter never surfaced.
                "mean_relevant_rank": (
                    _safe_mean(
                        float(r["relevant_rank"])
                        for r in rows
                        if r["relevant_rank"] is not None
                        and int(r["relevant_rank"]) <= observed_k
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
    hit_at_k_limit: int = 5,
    ranking_limit: int = 5,
    engine_invocations: int = -1,
) -> MetricSnapshot:
    """Compute retrieval metrics over ``cases``.

    Parameters
    ----------
    cases:
        Iterable of :class:`adapter.CaseRecord` (or any duck-typed
        object carrying the same fields).
    hit_at_k_limit:
        The ``K`` the caller asks for in the Hit@K slot.  The
        reported ``hit_at_k`` is the Hit@K fraction computed at
        ``min(hit_at_k_limit, ranking_limit)`` — never above the
        observable ranking depth.  When ``hit_at_k_limit >
        ranking_limit`` the snapshot's ``hit_at_k`` is ``NaN``
        and ``hit_at_k_status == "n_a"``.  Defaults to ``5`` (the
        production-shaped ranking depth) so the default snapshot
        reports Hit@1, Hit@5, Hit@5 (slot) and MRR@5.
    ranking_limit:
        The observable ranking depth — the maximum number of
        ranked source IDs the adapter surfaces.  Defaults to
        ``5`` (the production default limit).  When the caller
        asks for ``hit_at_k_limit > ranking_limit`` the
        ``hit_at_k`` slot fails closed (NaN) instead of
        fabricating hits above the observable depth.
    engine_invocations:
        Optional hint about engine seam invocations.  Pass ``-1``
        when unknown so the coverage audit reports ``-1`` instead
        of fabricating a count.

    Behaviour:

      * Headline metrics are computed over MAPPED gold source IDs
        only.  Unmapped dia IDs and unresolved evidence
        contribute to the ``unmapped_gold_count`` /
        ``unresolved_gold_count`` fields but never to Hit/MRR.
      * When the entire run has zero mapped evidence, status is
        set to ``"no_mapped_evidence"``; the headline metrics
        stay at ``0.0`` and the coverage audit reports the
        underlying counts so callers can see why.
      * When ``engine_invocations`` is left at ``-1`` the audit
        reflects "not measured" rather than fabricate a count.
      * Per-category breakdown is always emitted, including for
        the ``"__unlabelled__"`` bucket when categories are
        missing.
      * ``MRR`` is computed over the observed ranking depth
        (``min(hit_at_k_limit, ranking_limit)``); ``mrr_k`` is
        the K used for that computation.
    """
    questions_total = len(cases or ())
    per_scores = [
        _score_case(
            c,
            hit_at_k_limit=hit_at_k_limit,
            ranking_limit=ranking_limit,
        )
        for c in cases or ()
    ]
    mapped_total = sum(p["gold_count"] for p in per_scores)
    unmapped_total = sum(p["unmapped_count"] for p in per_scores)
    unresolved_total = sum(p["unresolved_count"] for p in per_scores)
    q_with_evidence = sum(1 for p in per_scores if p["gold_count"] > 0)
    q_with_unmapped = sum(1 for p in per_scores if p["unmapped_count"] > 0)
    q_with_unresolved = sum(1 for p in per_scores if p["unresolved_count"] > 0)

    if mapped_total == 0:
        status = StatusFlag.NO_MAPPED_EVIDENCE
    else:
        status = StatusFlag.OK

    # The metric-K truth contract — K is honest about depth.
    safe_hit_k_limit = max(0, int(hit_at_k_limit))
    safe_ranking_limit = max(0, int(ranking_limit))
    # Per-K status: every reported K slot must explicitly report
    # "n_a" when K > ranking_limit.  This includes the fixed
    # Hit@1 / Hit@5 slots — Hit@5 is N/A when the observable
    # ranking depth is below 5, so the slot cannot fabricate a
    # fraction over a shorter surface.
    hit_at_1_status = (
        HIT_AT_K_STATUS_OK if 1 <= safe_ranking_limit else HIT_AT_K_STATUS_NA
    )
    hit_at_5_status = (
        HIT_AT_K_STATUS_OK if 5 <= safe_ranking_limit else HIT_AT_K_STATUS_NA
    )
    if safe_hit_k_limit <= safe_ranking_limit:
        hit_at_k_status = HIT_AT_K_STATUS_OK
        observed_k = safe_hit_k_limit
    else:
        hit_at_k_status = HIT_AT_K_STATUS_NA
        observed_k = safe_ranking_limit

    # Headline aggregates — only over cases with mapped evidence.
    scorable = [p for p in per_scores if p["gold_count"] > 0]
    hit_at_1 = (
        _safe_mean(p["hit_at_1"] for p in scorable)
        if hit_at_1_status == HIT_AT_K_STATUS_OK
        else float("nan")
    )
    hit_at_5 = (
        _safe_mean(p["hit_at_5"] for p in scorable)
        if hit_at_5_status == HIT_AT_K_STATUS_OK
        else float("nan")
    )
    if hit_at_k_status == HIT_AT_K_STATUS_OK:
        hit_at_k_value: float = _safe_mean(p["hit_at_k"] for p in scorable)
    else:
        hit_at_k_value = float("nan")
    # ``mean_relevant_rank`` only averages ranks that fall
    # WITHIN the observable ranking depth — a relevant doc
    # that surfaces beyond ``observed_k`` never enters the mean
    # so the headline metric cannot silently claim a
    # measurement from a rank the adapter never surfaced.
    # Pair contract with the per-case ``mrr`` clamp (which is
    # already capped at observed_k in ``_score_case``).
    relevant_ranks = [
        float(p["relevant_rank"])
        for p in scorable
        if p["relevant_rank"] is not None
        and int(p["relevant_rank"]) <= int(observed_k)
    ]
    mean_relevant_rank = _safe_mean(relevant_ranks)
    # MRR — always at observed depth; ``mrr_k`` records the K
    # used.  MRR remains 0.0 even when hit slots are N/A because
    # it is the reciprocal of the observed relevant rank and the
    # deployment did surface (or fail to surface) the doc at the
    # observed depth.
    mrr = _safe_mean(p["mrr"] for p in scorable)
    mrr_k = observed_k

    per_category = compute_per_category(
        cases,
        hit_at_k_limit=hit_at_k_limit,
        ranking_limit=ranking_limit,
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
        questions_with_unresolved_evidence=int(q_with_unresolved),
        mapped_gold_count=int(mapped_total),
        unmapped_gold_count=int(unmapped_total),
        unresolved_gold_count=int(unresolved_total),
        hit_at_1=float(hit_at_1),
        hit_at_1_status=str(hit_at_1_status),
        hit_at_5=float(hit_at_5),
        hit_at_5_status=str(hit_at_5_status),
        hit_at_k=float(hit_at_k_value),
        hit_at_k_k=int(safe_hit_k_limit),
        hit_at_k_status=str(hit_at_k_status),
        ranking_limit=int(safe_ranking_limit),
        mrr=float(mrr),
        mrr_k=int(mrr_k),
        mean_relevant_rank=float(mean_relevant_rank),
        per_category=per_category,
        coverage=audit,
    )
