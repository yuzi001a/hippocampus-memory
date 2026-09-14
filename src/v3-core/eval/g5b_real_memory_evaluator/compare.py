"""G5b differential evaluator — compare two offline result sets.

Development-only. Compares a BASE run against a CANDIDATE run and
exposes explicit improvement/regression deltas without modifying the
baseline evaluator.

Inputs:
    Two pairs of JSON files produced by the G5b evaluator:

      base_results.json + base_summary.json   (baseline run)
      cand_results.json + cand_summary.json   (candidate run)

    The pairs may also be supplied as pre-loaded dicts to the
    callable API (``compare_results_dicts``).

Output:
    A ``DeltaReport`` dataclass (also JSON-serializable) summarising:

      * scenario_count, pass_rate, unsupported_count
      * Hit@1 / Hit@3 / Hit@5, MRR
      * must_recall_hit_rate, must_not_recall_violation_rate
      * archive / conflict / pipeline counts
      * latency (median, p95)
      * new/fixed misses, new/fixed false positives
      * scenario IDs whose pass/fail LABEL changed
      * per-category pass-rate deltas

Fail-closed semantics:
    When the scenario sets between BASE and CANDIDATE differ in a way
    that makes the comparison unsafe (mismatched scenario IDs,
    mismatched lane label, missing fields), the module raises
    ``IncompatibleScenarioSets`` whose message lists the specific
    mismatches. The caller decides whether to ignore or surface.

Scope discipline:
    This module is read-only with respect to the runner, the writer,
    the reader, the lane A/B implementations, and the failure taxonomy.
    It loads the JSON outputs the runner already wrote; it never
    re-runs scenarios, never opens a PG/lab connection, never touches
    production config or services. It is a pure offline diff.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IncompatibleScenarioSets(ValueError):
    """Raised when BASE and CANDIDATE result sets cannot be diffed.

    The error message intentionally does NOT echo any payload field
    that may carry credentials or secret-shaped data. It only lists
    structural mismatches that are observable from the public schema
    (count, scenario_id set, lane label, summary keys).
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MetricSnapshot:
    """One side's headline metrics (BASE or CANDIDATE)."""

    label: str
    scenario_count: int
    pass_count: int
    fail_count: int
    error_count: int
    unsupported_count: int
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
    per_label_count: dict[str, int] = field(default_factory=dict)
    per_category_pass_rate: dict[str, float] = field(default_factory=dict)


@dataclass
class PerCategoryDelta:
    """Per-category pass-rate change. ``abs_delta`` is rounded to 6 dp."""

    category: str
    base_pass_rate: float
    cand_pass_rate: float
    abs_delta: float


@dataclass
class LabelChange:
    """A scenario whose PASS/FAIL label flipped between BASE and CANDIDATE.

    ``direction`` is "improved" (FAIL/UNSUPPORTED/ERROR → PASS, or
    FAIL → UNSUPPORTED, or ERROR → FAIL/UNSUPPORTED) or "regressed"
    (the inverse).
    """

    scenario_id: str
    category: str
    base_label: str
    base_pipeline_status: str
    cand_label: str
    cand_pipeline_status: str
    direction: str


@dataclass
class DeltaReport:
    """Full differential report.

    All numeric deltas are CANDIDATE minus BASE. Positive deltas mean
    the candidate improved over the base for that metric (more passes,
    higher hit-rate, lower latency, fewer violations). Negative deltas
    mean a regression.
    """

    base: MetricSnapshot
    cand: MetricSnapshot

    # Headline deltas (cand - base). Floats rounded to 6 dp.
    pass_rate_delta: float
    must_recall_hit_rate_delta: float
    must_not_recall_violation_rate_delta: float
    hit_at_1_delta: float
    hit_at_3_delta: float
    hit_at_5_delta: float
    mrr_delta: float

    # Integer / count deltas (cand - base). Positive = candidate has more.
    pass_count_delta: int
    fail_count_delta: int
    error_count_delta: int
    unsupported_count_delta: int
    misses_delta: int
    false_positives_delta: int
    archive_violations_delta: int
    conflict_violations_delta: int
    pipeline_errors_delta: int

    # Latency deltas (cand - base, ms). Negative = faster.
    median_latency_ms_delta: float
    p95_latency_ms_delta: float

    # Scenario-level flips.
    new_misses: list[str]                  # miss in cand, hit in base
    fixed_misses: list[str]                # hit in cand, miss in base
    new_false_positives: list[str]         # query id "scenario::query"
    fixed_false_positives: list[str]
    label_changes: list[LabelChange]

    # Per-category deltas (sorted by abs delta descending).
    per_category: list[PerCategoryDelta]

    # Diagnostic.
    verdict: str                           # "improved" / "regressed" / "mixed" / "unchanged"
    base_label: str
    cand_label: str


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------


def load_run(results_path: str, summary_path: str) -> tuple[dict, dict]:
    """Load a (results.json, summary.json) pair from disk.

    Returns ``(results_dict, summary_dict)``. Both files must exist
    and parse as JSON objects; otherwise raises ``IncompatibleScenarioSets``
    with a non-secret, structural message.
    """

    def _read(path: str) -> dict:
        if not os.path.isfile(path):
            raise IncompatibleScenarioSets(
                f"required file not found: {os.path.basename(path)!r}"
            )
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except json.JSONDecodeError as exc:
            raise IncompatibleScenarioSets(
                f"invalid JSON in {os.path.basename(path)!r}: "
                f"line {exc.lineno} col {exc.colno}"
            ) from None
        if not isinstance(obj, dict):
            raise IncompatibleScenarioSets(
                f"{os.path.basename(path)!r} must be a JSON object"
            )
        return obj

    return _read(results_path), _read(summary_path)


# The exact metric keys the differential evaluator consumes from
# ``summary.json['metrics']``. The presence of every one of these is
# a precondition for a sound diff — silently defaulting a missing
# key to zero is a correctness bug, because ``0`` is a perfectly
# legal metric value. We fail-closed instead. Order is preserved
# in error messages via the sorted list of missing keys.
_REQUIRED_METRIC_KEYS: frozenset[str] = frozenset({
    "scenario_count",
    "pass_count",
    "fail_count",
    "error_count",
    "unsupported_count",
    "scenario_pass_rate",
    "must_recall_hit_rate",
    "must_not_recall_violation_rate",
    "hit_at_1",
    "hit_at_3",
    "hit_at_5",
    "mrr",
    "false_positives",
    "misses",
    "archive_violations",
    "conflict_violations",
    "pipeline_errors",
    "median_latency_ms",
    "p95_latency_ms",
    "per_label_count",
    "per_category_pass_rate",
})

# Expected container type per metric key. Container-typed keys must
# be the exact mapping type listed here (we do NOT accept arbitrary
# ``collections.abc.Mapping`` because the writer emits plain
# ``dict`` literals and we want the type contract to be tight).
_METRIC_KEY_TYPES: dict[str, type] = {
    "scenario_count": int,
    "pass_count": int,
    "fail_count": int,
    "error_count": int,
    "unsupported_count": int,
    "scenario_pass_rate": float,
    "must_recall_hit_rate": float,
    "must_not_recall_violation_rate": float,
    "hit_at_1": float,
    "hit_at_3": float,
    "hit_at_5": float,
    "mrr": float,
    "false_positives": int,
    "misses": int,
    "archive_violations": int,
    "conflict_violations": int,
    "pipeline_errors": int,
    "median_latency_ms": float,
    "p95_latency_ms": float,
    "per_label_count": dict,
    "per_category_pass_rate": dict,
}


def _summary_metric_block(summary: dict) -> dict:
    """Return the validated ``metrics`` sub-dict or raise an explicit error.

    Fail-closed contract: every required metric key MUST be present
    AND match its declared container type (``int``, ``float``, ``dict``).
    The error message lists only structural key names — it never
    echoes any metric value (which could carry operational data) and
    never echoes any field that the caller may not want surfaced.
    """

    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        raise IncompatibleScenarioSets(
            "summary.json missing 'metrics' object — incompatible output "
            "schema (was this produced by a different evaluator version?)"
        )

    missing = sorted(k for k in _REQUIRED_METRIC_KEYS if k not in metrics)
    if missing:
        # Use repr on the list so the message is unambiguous even if
        # a key happens to be named like a Python literal.
        raise IncompatibleScenarioSets(
            "summary.json 'metrics' is missing required keys: "
            f"{missing!r} — incompatible output schema (was this "
            "produced by a different evaluator version?)"
        )

    bad_type = sorted(
        k for k in _REQUIRED_METRIC_KEYS
        if not isinstance(metrics[k], _METRIC_KEY_TYPES[k])
    )
    if bad_type:
        expected = {k: _METRIC_KEY_TYPES[k].__name__ for k in bad_type}
        raise IncompatibleScenarioSets(
            "summary.json 'metrics' keys have wrong container type: "
            f"{bad_type!r} (expected {expected!r}) — incompatible output "
            "schema"
        )

    return metrics


def _build_snapshots(
    base_results: dict,
    base_summary: dict,
    cand_results: dict,
    cand_summary: dict,
) -> tuple[MetricSnapshot, MetricSnapshot]:
    """Build two MetricSnapshots from raw JSON dicts.

    Performs structural validation BEFORE any numeric work so that
    a malformed file fails fast with a non-secret error. Failures:

    * missing/wrong-type metric keys (see :data:`_REQUIRED_METRIC_KEYS`)
    * ``summary.json['metrics']['scenario_count']`` ≠
      ``len(results.json['scenarios'])``
    * ``results.json['lane']`` (when present) ≠ ``summary.json['lane']``
    * ``base_lane`` ≠ ``cand_lane``
    """

    base_metrics = _summary_metric_block(base_summary)
    cand_metrics = _summary_metric_block(cand_summary)

    # Lane mismatch is a hard fail.
    base_lane = base_summary.get("lane")
    cand_lane = cand_summary.get("lane")
    if base_lane not in ("a", "b") or cand_lane not in ("a", "b"):
        raise IncompatibleScenarioSets(
            "summary.json 'lane' must be 'a' or 'b' in both runs "
            f"(got base={base_lane!r}, cand={cand_lane!r})"
        )
    if base_lane != cand_lane:
        raise IncompatibleScenarioSets(
            f"lane mismatch between runs (base={base_lane}, cand={cand_lane}) "
            "— cross-lane comparisons are not supported by the differential "
            "evaluator"
        )

    def _check_results_consistency(
        label: str,
        results: dict,
        summary: dict,
        metrics: dict,
    ) -> None:
        """Fail-closed when results and summary disagree.

        Two invariants the writer must uphold:

        1. ``results['scenarios']`` is present and its length matches
           ``summary['metrics']['scenario_count']``. A mismatch means
           either a writer bug or a hand-edited file — silently trusting
           one side would corrupt the diff.
        2. When ``results['lane']`` is present, it must equal
           ``summary['lane']``. A mismatch means a writer bug or
           cross-lane contamination — fail-closed.
        """

        scenarios = results.get("scenarios")
        if not isinstance(scenarios, list):
            # Already covered exhaustively by :func:`_scenario_records`,
            # but we look it up here too so the scenario_count check
            # doesn't IndexError on a malformed input.
            raise IncompatibleScenarioSets(
                f"{label} results.json missing 'scenarios' array — "
                "incompatible output schema"
            )
        n_scenarios = len(scenarios)
        n_summary = metrics["scenario_count"]
        if n_scenarios != n_summary:
            raise IncompatibleScenarioSets(
                f"{label} results/summary scenario count mismatch "
                f"(results_scenarios_len={n_scenarios}, "
                f"summary_metrics_scenario_count={n_summary}) — "
                "incompatible output schema"
            )

        if "lane" in results:
            r_lane = results["lane"]
            s_lane = summary["lane"]
            if r_lane != s_lane:
                raise IncompatibleScenarioSets(
                    f"{label} results/summary lane mismatch "
                    f"(results_lane={r_lane!r}, summary_lane={s_lane!r}) "
                    "— incompatible output schema"
                )

    _check_results_consistency("base", base_results, base_summary, base_metrics)
    _check_results_consistency("cand", cand_results, cand_summary, cand_metrics)

    def _snap(label: str, results: dict, summary: dict, metrics: dict) -> MetricSnapshot:
        # All keys are guaranteed present and typed by
        # :func:`_summary_metric_block`. No silent default to 0/0.0:
        # a missing key already raised upstream.
        return MetricSnapshot(
            label=label,
            scenario_count=int(metrics["scenario_count"]),
            pass_count=int(metrics["pass_count"]),
            fail_count=int(metrics["fail_count"]),
            error_count=int(metrics["error_count"]),
            unsupported_count=int(metrics["unsupported_count"]),
            scenario_pass_rate=float(metrics["scenario_pass_rate"]),
            must_recall_hit_rate=float(metrics["must_recall_hit_rate"]),
            must_not_recall_violation_rate=float(
                metrics["must_not_recall_violation_rate"]
            ),
            hit_at_1=float(metrics["hit_at_1"]),
            hit_at_3=float(metrics["hit_at_3"]),
            hit_at_5=float(metrics["hit_at_5"]),
            mrr=float(metrics["mrr"]),
            false_positives=int(metrics["false_positives"]),
            misses=int(metrics["misses"]),
            archive_violations=int(metrics["archive_violations"]),
            conflict_violations=int(metrics["conflict_violations"]),
            pipeline_errors=int(metrics["pipeline_errors"]),
            median_latency_ms=float(metrics["median_latency_ms"]),
            p95_latency_ms=float(metrics["p95_latency_ms"]),
            per_label_count=dict(metrics["per_label_count"]),
            per_category_pass_rate=dict(metrics["per_category_pass_rate"]),
        )

    return (
        _snap("base", base_results, base_summary, base_metrics),
        _snap("cand", cand_results, cand_summary, cand_metrics),
    )


def _scenario_records(results: dict) -> dict[str, dict]:
    """Index ``results.json['scenarios']`` by scenario_id.

    Raises ``IncompatibleScenarioSets`` if the scenarios list is
    missing or contains a duplicate scenario_id.
    """

    scenarios = results.get("scenarios")
    if not isinstance(scenarios, list):
        raise IncompatibleScenarioSets(
            "results.json missing 'scenarios' array — incompatible output "
            "schema (was this produced by a different evaluator version?)"
        )

    out: dict[str, dict] = {}
    for s in scenarios:
        if not isinstance(s, dict):
            raise IncompatibleScenarioSets(
                "results.json 'scenarios' contains a non-object entry"
            )
        sid = s.get("scenario_id")
        if not isinstance(sid, str) or not sid:
            raise IncompatibleScenarioSets(
                "results.json scenario entry missing 'scenario_id' string"
            )
        if sid in out:
            raise IncompatibleScenarioSets(
                f"results.json has duplicate scenario_id {sid!r}"
            )
        out[sid] = s
    return out


def _validate_scenario_sets(
    base_records: dict[str, dict],
    cand_records: dict[str, dict],
) -> None:
    """Fail-closed when the two runs cover different scenarios.

    The comparison is only safe when the scenario sets are identical
    AND the per-query counts are identical (so we are comparing the
    same queries in the same order).
    """

    base_ids = set(base_records)
    cand_ids = set(cand_records)
    if base_ids != cand_ids:
        only_base = sorted(base_ids - cand_ids)
        only_cand = sorted(cand_ids - base_ids)
        # Cap the list in the error message so a 1000-scenario diff
        # doesn't flood logs. The full diff is recoverable from the
        # outputs.
        def _cap(xs: list[str]) -> list[str]:
            return xs[:10] + (["...(+{} more)".format(len(xs) - 10)] if len(xs) > 10 else [])

        raise IncompatibleScenarioSets(
            "scenario set mismatch between BASE and CANDIDATE "
            f"(base_size={len(base_ids)}, cand_size={len(cand_ids)}, "
            f"only_in_base={_cap(only_base)}, only_in_cand={_cap(only_cand)})"
        )

    # Per-query identity check. Same scenario_id must mean the same
    # set of query_ids in the same order — otherwise the new/fixed
    # miss accounting would silently corrupt.
    for sid, base_rec in base_records.items():
        cand_rec = cand_records[sid]
        base_qs = [q.get("query_id") for q in (base_rec.get("per_query") or [])]
        cand_qs = [q.get("query_id") for q in (cand_rec.get("per_query") or [])]
        if base_qs != cand_qs:
            raise IncompatibleScenarioSets(
                f"scenario {sid!r} has differing query_id sequence between "
                "BASE and CANDIDATE — comparison would not be apples-to-apples"
            )


# ---------------------------------------------------------------------------
# Scenario-level diff
# ---------------------------------------------------------------------------


# Order matters for direction classification. Higher number = stronger.
_STATUS_RANK = {
    "PASS": 3,
    "UNSUPPORTED": 2,
    "FAIL": 1,
    "ERROR": 0,
}


def _classify_direction(base_status: str, cand_status: str) -> str:
    """Return 'improved', 'regressed', or 'unchanged' for a status flip."""

    base_r = _STATUS_RANK.get(base_status, 0)
    cand_r = _STATUS_RANK.get(cand_status, 0)
    if cand_r > base_r:
        return "improved"
    if cand_r < base_r:
        return "regressed"
    return "unchanged"


def _diff_scenarios(
    base_records: dict[str, dict],
    cand_records: dict[str, dict],
) -> tuple[list[str], list[str], list[str], list[str], list[LabelChange]]:
    """Compute scenario-level flip sets.

    Returns ``(new_misses, fixed_misses, new_false_positives,
    fixed_false_positives, label_changes)``.
    """

    new_misses: list[str] = []
    fixed_misses: list[str] = []
    new_fps: list[str] = []
    fixed_fps: list[str] = []
    flips: list[LabelChange] = []

    for sid in sorted(base_records):
        b = base_records[sid]
        c = cand_records[sid]
        b_status = str(b.get("pipeline_status", ""))
        c_status = str(c.get("pipeline_status", ""))
        b_label = str(b.get("failure_label", ""))
        c_label = str(c.get("failure_label", ""))
        category = str(b.get("category", ""))

        if b_label != c_label or b_status != c_status:
            direction = _classify_direction(b_status, c_status)
            if direction != "unchanged":
                flips.append(LabelChange(
                    scenario_id=sid,
                    category=category,
                    base_label=b_label,
                    base_pipeline_status=b_status,
                    cand_label=c_label,
                    cand_pipeline_status=c_status,
                    direction=direction,
                ))

        # Scenario-level miss flip: a miss is a non-pass, non-unsupported
        # scenario with a recall-related failure label. The summary's
        # ``misses`` counter is authoritative for the absolute count;
        # this loop only classifies *which* scenarios flipped.
        b_is_miss = b_status == "FAIL" and _is_recall_failure_label(b_label)
        c_is_miss = c_status == "FAIL" and _is_recall_failure_label(c_label)
        if b_is_miss and not c_is_miss:
            fixed_misses.append(sid)
        elif c_is_miss and not b_is_miss:
            new_misses.append(sid)

        # Per-query false-positive flip. The per-query record carries
        # ``must_not_recall_violations``; a non-empty list means at
        # least one forbidden memory was returned. Tagged by
        # ``<scenario>::<query>`` for traceability.
        b_qs = b.get("per_query") or []
        c_qs = c.get("per_query") or []
        for bq, cq in zip(b_qs, c_qs):
            qid = str(bq.get("query_id", ""))
            b_fp = bool(bq.get("must_not_recall_violations"))
            c_fp = bool(cq.get("must_not_recall_violations"))
            tag = f"{sid}::{qid}"
            if b_fp and not c_fp:
                fixed_fps.append(tag)
            elif c_fp and not b_fp:
                new_fps.append(tag)

    return new_misses, fixed_misses, new_fps, fixed_fps, flips


# Failure labels that mean the recall path failed at the scenario
# level. These are the labels the metrics layer aggregates into
# ``misses``. Other FAIL labels (PIPELINE_ERROR, CONFLICT_RESOLUTION,
# TIMEOUT, STALE_MEMORY, ARCHIVED_MEMORY_RETURNED) are tracked via
# the per-label counter, not the misses counter.
_RECALL_FAILURE_LABELS = frozenset({
    "NOT_RETRIEVED",
    "NOT_INDEXED",
    "NOT_STORED",
    "RANKED_TOO_LOW",
    "WRONG_MEMORY",
    "UNKNOWN",
})


def _is_recall_failure_label(label: str) -> bool:
    """Return True if a failure_label indicates a recall miss."""

    return label in _RECALL_FAILURE_LABELS


# ---------------------------------------------------------------------------
# Per-category delta
# ---------------------------------------------------------------------------


def _per_category(
    base: MetricSnapshot,
    cand: MetricSnapshot,
) -> list[PerCategoryDelta]:
    """Return per-category pass-rate deltas, sorted by |delta| desc."""

    cats = sorted(set(base.per_category_pass_rate) | set(cand.per_category_pass_rate))
    out: list[PerCategoryDelta] = []
    for cat in cats:
        b = base.per_category_pass_rate.get(cat, 0.0)
        c = cand.per_category_pass_rate.get(cat, 0.0)
        out.append(PerCategoryDelta(
            category=cat,
            base_pass_rate=b,
            cand_pass_rate=c,
            abs_delta=round(c - b, 6),
        ))
    out.sort(key=lambda p: (-abs(p.abs_delta), p.category))
    return out


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def _verdict(
    base: MetricSnapshot,
    cand: MetricSnapshot,
    flips: list[LabelChange],
) -> str:
    """Return one of 'improved', 'regressed', 'mixed', 'unchanged'."""

    pass_rate_delta = cand.scenario_pass_rate - base.scenario_pass_rate
    # Use a 1% absolute threshold for "headline" change to keep the
    # verdict stable on noisy single-scenario deltas.
    headline_improved = pass_rate_delta > 0.01
    headline_regressed = pass_rate_delta < -0.01

    improved_flips = sum(1 for f in flips if f.direction == "improved")
    regressed_flips = sum(1 for f in flips if f.direction == "regressed")

    if headline_improved and improved_flips >= regressed_flips:
        return "improved"
    if headline_regressed and regressed_flips > improved_flips:
        return "regressed"
    if improved_flips > regressed_flips:
        return "mixed"
    if regressed_flips > improved_flips:
        return "mixed"
    return "unchanged"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compare_results_dicts(
    base_results: dict,
    base_summary: dict,
    cand_results: dict,
    cand_summary: dict,
    *,
    base_label: str = "base",
    cand_label: str = "cand",
) -> DeltaReport:
    """Diff two in-memory run dicts. Returns a ``DeltaReport``.

    All structural checks are performed before any numeric work. On
    incompatible inputs the function raises ``IncompatibleScenarioSets``
    with a non-secret, list-shaped error.
    """

    base_snap, cand_snap = _build_snapshots(
        base_results, base_summary, cand_results, cand_summary,
    )
    base_records = _scenario_records(base_results)
    cand_records = _scenario_records(cand_results)
    _validate_scenario_sets(base_records, cand_records)

    new_misses, fixed_misses, new_fps, fixed_fps, flips = _diff_scenarios(
        base_records, cand_records,
    )

    report = DeltaReport(
        base=base_snap,
        cand=cand_snap,
        pass_rate_delta=round(cand_snap.scenario_pass_rate - base_snap.scenario_pass_rate, 6),
        must_recall_hit_rate_delta=round(
            cand_snap.must_recall_hit_rate - base_snap.must_recall_hit_rate, 6
        ),
        must_not_recall_violation_rate_delta=round(
            cand_snap.must_not_recall_violation_rate
            - base_snap.must_not_recall_violation_rate,
            6,
        ),
        hit_at_1_delta=round(cand_snap.hit_at_1 - base_snap.hit_at_1, 6),
        hit_at_3_delta=round(cand_snap.hit_at_3 - base_snap.hit_at_3, 6),
        hit_at_5_delta=round(cand_snap.hit_at_5 - base_snap.hit_at_5, 6),
        mrr_delta=round(cand_snap.mrr - base_snap.mrr, 6),
        pass_count_delta=cand_snap.pass_count - base_snap.pass_count,
        fail_count_delta=cand_snap.fail_count - base_snap.fail_count,
        error_count_delta=cand_snap.error_count - base_snap.error_count,
        unsupported_count_delta=cand_snap.unsupported_count - base_snap.unsupported_count,
        misses_delta=cand_snap.misses - base_snap.misses,
        false_positives_delta=cand_snap.false_positives - base_snap.false_positives,
        archive_violations_delta=(
            cand_snap.archive_violations - base_snap.archive_violations
        ),
        conflict_violations_delta=(
            cand_snap.conflict_violations - base_snap.conflict_violations
        ),
        pipeline_errors_delta=(
            cand_snap.pipeline_errors - base_snap.pipeline_errors
        ),
        median_latency_ms_delta=round(
            cand_snap.median_latency_ms - base_snap.median_latency_ms, 6
        ),
        p95_latency_ms_delta=round(
            cand_snap.p95_latency_ms - base_snap.p95_latency_ms, 6
        ),
        new_misses=new_misses,
        fixed_misses=fixed_misses,
        new_false_positives=new_fps,
        fixed_false_positives=fixed_fps,
        label_changes=flips,
        per_category=_per_category(base_snap, cand_snap),
        verdict="",  # filled in below
        base_label=base_label,
        cand_label=cand_label,
    )
    report.verdict = _verdict(base_snap, cand_snap, flips)
    return report


def compare_runs(
    base_results_path: str,
    base_summary_path: str,
    cand_results_path: str,
    cand_summary_path: str,
    *,
    base_label: str = "base",
    cand_label: str = "cand",
) -> DeltaReport:
    """Diff two on-disk runs by file path."""

    base_results, base_summary = load_run(base_results_path, base_summary_path)
    cand_results, cand_summary = load_run(cand_results_path, cand_summary_path)
    return compare_results_dicts(
        base_results, base_summary,
        cand_results, cand_summary,
        base_label=base_label,
        cand_label=cand_label,
    )


def report_to_dict(report: DeltaReport) -> dict:
    """Return a JSON-serializable dict representation of a ``DeltaReport``."""

    return json.loads(json.dumps(asdict(report), ensure_ascii=False))


def report_to_json(report: DeltaReport) -> str:
    """Return a JSON string for a ``DeltaReport``."""

    return json.dumps(report_to_dict(report), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="g5b-differential-evaluator",
        description=(
            "Compare two offline G5b evaluator runs and emit an explicit "
            "improvement/regression delta. Development-only; never opens "
            "PG, never touches production config."
        ),
    )
    p.add_argument("--base-results", required=True,
                   help="Path to BASE results.json")
    p.add_argument("--base-summary", required=True,
                   help="Path to BASE summary.json")
    p.add_argument("--cand-results", required=True,
                   help="Path to CANDIDATE results.json")
    p.add_argument("--cand-summary", required=True,
                   help="Path to CANDIDATE summary.json")
    p.add_argument("--base-label", default="base",
                   help="Display label for the BASE run (default 'base').")
    p.add_argument("--cand-label", default="cand",
                   help="Display label for the CANDIDATE run (default 'cand').")
    p.add_argument("--out-json", default=None,
                   help="Optional path to write the JSON delta report.")
    p.add_argument("--fail-on-regression", action="store_true",
                   help="Exit 3 when verdict in {regressed, mixed}.")
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = _build_argparser().parse_args(argv)
    try:
        report = compare_runs(
            args.base_results, args.base_summary,
            args.cand_results, args.cand_summary,
            base_label=args.base_label, cand_label=args.cand_label,
        )
    except IncompatibleScenarioSets as exc:
        print(
            f"[g5b-diff] incompatible inputs: {exc}",
            file=sys.stderr,
        )
        return 2

    payload = report_to_json(report)
    if args.out_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_json)) or ".",
                    exist_ok=True)
        with open(args.out_json, "w", encoding="utf-8") as f:
            f.write(payload)
    else:
        print(payload)

    # Human-friendly one-line summary on stderr (so it doesn't pollute
    # the JSON stream when --out-json is not set and stdout is piped).
    print(
        f"[g5b-diff] verdict={report.verdict} "
        f"pass_rate_delta={report.pass_rate_delta:+.3f} "
        f"hit@3_delta={report.hit_at_3_delta:+.3f} "
        f"mrr_delta={report.mrr_delta:+.3f} "
        f"new_misses={len(report.new_misses)} "
        f"fixed_misses={len(report.fixed_misses)} "
        f"new_fp={len(report.new_false_positives)} "
        f"fixed_fp={len(report.fixed_false_positives)}",
        file=sys.stderr,
    )

    if args.fail_on_regression and report.verdict in ("regressed", "mixed"):
        return 3
    return 0


__all__ = [
    "DeltaReport",
    "IncompatibleScenarioSets",
    "LabelChange",
    "MetricSnapshot",
    "PerCategoryDelta",
    "compare_results_dicts",
    "compare_runs",
    "load_run",
    "report_to_dict",
    "report_to_json",
]


if __name__ == "__main__":
    sys.exit(main())
