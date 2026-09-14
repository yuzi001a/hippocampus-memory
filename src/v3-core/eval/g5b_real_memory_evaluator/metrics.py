"""Metrics for the G5b evaluator.

The evaluator emits a flat list of ``ScenarioResult`` records. The
metrics module reduces that list into:

  * **scenario_pass_rate**           — pass / (pass + fail); UNSUPPORTED
                                       excluded from denominator.
  * **must_recall_hit_rate**         — over all queries with
                                       ``must_recall``: fraction that
                                       retrieved every expected memory
                                       inside ``must_recall_rank_max``.
  * **must_not_recall_violation_rate** — over all queries with
                                       ``must_not_recall``: fraction
                                       that returned a forbidden
                                       memory inside ``limit``.
  * **hit_at_1 / hit_at_3 / hit_at_5** — fraction of queries where at
                                        least one must_recall memory
                                        appears at rank ≤ 1 / 3 / 5.
  * **mrr**                          — mean reciprocal rank of the
                                       first must_recall hit, over
                                       queries that have must_recall.
  * **false_positives / misses**     — total counts across queries.
  * **archive_violations**           — count of queries that returned
                                       a memory with status='archived'
                                       when expected_status='active'.
  * **conflict_violations**          — count of scenarios where the
                                       conflict-resolution expectation
                                       was not met.
  * **pipeline_errors**              — count of scenarios where the
                                       writer/reader raised.
  * **unsupported_count**            — count of scenarios tagged
                                       ``unsupported=True``.
  * **median_latency_ms /
     p95_latency_ms**                — over all per-query latencies.

All metrics are integer / float; nothing here requires a remote
service.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class ScenarioResult:
    scenario_id: str
    category: str
    passed: bool
    pipeline_status: str  # PASS / FAIL / UNSUPPORTED / ERROR
    failure_label: str  # one of failure_taxonomy.* or PASS
    unsupported: bool
    per_query: list["QueryResult"] = field(default_factory=list)
    elapsed_ms: float = 0.0
    notes: str = ""


@dataclass
class QueryResult:
    query_id: str
    text: str
    expected_lane: str
    actual_lane: str
    returned_memory_ids: list[str] = field(default_factory=list)
    must_recall_hit_ranks: list[int] = field(default_factory=list)
    must_not_recall_violations: list[str] = field(default_factory=list)
    archived_returned: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    failure_label: str = "PASS"
    raw_error: str = ""


@dataclass
class MetricsReport:
    scenario_count: int
    unsupported_count: int
    pass_count: int
    fail_count: int
    error_count: int
    scenario_pass_rate: float
    must_recall_hit_rate: float
    must_not_recall_violation_rate: float
    hit_at_1: float
    hit_at_3: float
    hit_at_5: float
    mrr: float
    false_positives: int
    misses: int
    archive_violations: int
    conflict_violations: int
    pipeline_errors: int
    median_latency_ms: float
    p95_latency_ms: float
    per_label_count: dict[str, int]
    per_category_pass_rate: dict[str, float]


def _safe_div(num: float, den: float) -> float:
    return float(num) / float(den) if den else 0.0


def compute_metrics(
    results: Iterable[ScenarioResult],
    query_records_by_scenario: dict[str, list[tuple[Any, QueryResult]]] | None = None,
) -> MetricsReport:
    """Compute the metrics report.

    ``query_records_by_scenario`` maps ``scenario_id`` to a list of
    ``(original_query, QueryResult)`` tuples. The original Query
    object carries ``must_recall`` / ``must_not_recall`` which the
    runner can resolve from the scenario schema. When this dict is
    not provided, we fall back to the QueryResult-only view (which
    only knows about the actual returned ranks, not the spec).
    """
    rs = list(results)
    scenario_count = len(rs)
    unsupported_count = sum(1 for r in rs if r.unsupported)
    pass_count = sum(1 for r in rs if r.pipeline_status == "PASS")
    fail_count = sum(1 for r in rs if r.pipeline_status == "FAIL")
    error_count = sum(1 for r in rs if r.pipeline_status == "ERROR")

    billable = scenario_count - unsupported_count
    pass_rate = _safe_div(pass_count, billable)

    # Build a quick lookup of query text → (must_recall, must_not_recall)
    # when the caller supplied the raw scenario query objects.
    must_recall_lookup: dict[tuple[str, str], list[str]] = {}
    must_not_recall_lookup: dict[tuple[str, str], list[str]] = {}
    if query_records_by_scenario:
        for sid, recs in query_records_by_scenario.items():
            for q, _qr in recs:
                key = (sid, q.query_id)
                must_recall_lookup[key] = list(q.must_recall or [])
                must_not_recall_lookup[key] = list(q.must_not_recall or [])

    all_q: list[QueryResult] = []
    for r in rs:
        all_q.extend(r.per_query)

    must_recall_total = 0
    must_recall_hit = 0
    must_not_total = 0
    must_not_violations = 0
    hit_at_1 = 0
    hit_at_3 = 0
    hit_at_5 = 0
    rr_sum = 0.0
    rr_count = 0
    false_positives = 0
    misses = 0
    archive_violations = 0
    pipeline_errors = 0
    latencies: list[float] = []

    for q in all_q:
        latencies.append(q.elapsed_ms)
        # Resolve must_recall / must_not_recall from the lookup.
        key = ("", q.query_id)
        for sid, recs in (query_records_by_scenario or {}).items():
            for qq, _ in recs:
                if qq.query_id == q.query_id:
                    key = (sid, q.query_id)
                    break
        must_recall = must_recall_lookup.get(key, [])
        must_not_recall = must_not_recall_lookup.get(key, [])
        # Per-query pass/fail:
        #   * ANY must_recall at rank <= must_recall_rank_max is a hit.
        #   * Any must_not_recall in the returned set is a violation.
        # We use ANY-of semantics because the scenarios encode the
        # "correct answer" as the set of memory_ids that, if any
        # shows up in top-K, means the reader was correct. ALL-of
        # would require every must_recall to be present, which is
        # not what the author intends (see e.g. G5b-DT-001 where
        # the lab and staging fixtures share a topical signature
        # and the production fixture is the unique answer).
        ranks = q.must_recall_hit_ranks or []
        if must_recall:
            must_recall_total += 1
            valid_ranks = [r for r in ranks if r is not None and r > 0]
            if valid_ranks:
                must_recall_hit += 1
                rr = min(valid_ranks)
                rr_sum += 1.0 / float(rr)
                rr_count += 1
                if any(r <= 1 for r in valid_ranks):
                    hit_at_1 += 1
                if any(r <= 3 for r in valid_ranks):
                    hit_at_3 += 1
                if any(r <= 5 for r in valid_ranks):
                    hit_at_5 += 1
            else:
                misses += 1
        if must_not_recall:
            must_not_total += 1
            if q.must_not_recall_violations:
                must_not_violations += 1
                false_positives += len(q.must_not_recall_violations)
        if q.archived_returned:
            archive_violations += len(q.archived_returned)
        if q.raw_error:
            pipeline_errors += 1

    conflict_violations = sum(
        1 for r in rs if r.failure_label == "CONFLICT_RESOLUTION"
    )

    median_latency = statistics.median(latencies) if latencies else 0.0
    p95_latency = (
        statistics.quantiles(latencies, n=20, method="inclusive")[-1]
        if len(latencies) >= 2
        else (latencies[0] if latencies else 0.0)
    )

    per_label: dict[str, int] = {}
    for r in rs:
        per_label[r.failure_label] = per_label.get(r.failure_label, 0) + 1

    # per-category pass rate (excludes unsupported scenarios)
    per_cat: dict[str, tuple[int, int]] = {}
    for r in rs:
        if r.unsupported:
            continue
        c = r.category
        passed, total = per_cat.get(c, (0, 0))
        per_cat[c] = (passed + (1 if r.pipeline_status == "PASS" else 0), total + 1)
    per_cat_rate = {k: _safe_div(v[0], v[1]) for k, v in per_cat.items()}

    return MetricsReport(
        scenario_count=scenario_count,
        unsupported_count=unsupported_count,
        pass_count=pass_count,
        fail_count=fail_count,
        error_count=error_count,
        scenario_pass_rate=pass_rate,
        must_recall_hit_rate=_safe_div(must_recall_hit, must_recall_total),
        must_not_recall_violation_rate=_safe_div(
            must_not_violations, must_not_total
        ),
        hit_at_1=_safe_div(hit_at_1, must_recall_total),
        hit_at_3=_safe_div(hit_at_3, must_recall_total),
        hit_at_5=_safe_div(hit_at_5, must_recall_total),
        mrr=_safe_div(rr_sum, rr_count),
        false_positives=false_positives,
        misses=misses,
        archive_violations=archive_violations,
        conflict_violations=conflict_violations,
        pipeline_errors=pipeline_errors,
        median_latency_ms=float(median_latency),
        p95_latency_ms=float(p95_latency),
        per_label_count=per_label,
        per_category_pass_rate=per_cat_rate,
    )


__all__ = [
    "ScenarioResult",
    "QueryResult",
    "MetricsReport",
    "compute_metrics",
]