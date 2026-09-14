"""CLI entry point for the G5b evaluator.

Usage::

  # Deterministic Lane A baseline (always available)
  python -m eval.g5b_real_memory_evaluator.run --out-dir outputs/baseline

  # Lane B (live PG) — requires --live-pg AND --dsn
  G5B_EVAL_LIVE_PG=1 python -m eval.g5b_real_memory_evaluator.run \\
      --live-pg --dsn "host=127.0.0.1 port=55462 dbname=lab_eval user=lab pass=lab" \\
      --out-dir outputs/live-pg

Outputs (under ``--out-dir``):

  - ``results.json``     — every ScenarioResult with per-query trace.
  - ``summary.json``     — MetricsReport, lane label, timestamps.
  - ``run.log``          — human-readable log.

Exit code:

  - 0 when the run completes (regardless of pass/fail counts).
  - 1 when the runner itself errors out (e.g. missing scenario file,
    Lane B guard refused).

The run NEVER mutates production config / PG. It only opens
connections to a disposable lab PG when Lane B is explicitly opted in.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from typing import Any

# Make ``src/v3-core/src`` importable when this module is run from
# anywhere in the repository.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_SRC_ROOT = os.path.join(_PKG_ROOT, "src")
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
_EVAL_ROOT = _PKG_ROOT
if _EVAL_ROOT not in sys.path:
    sys.path.insert(0, _EVAL_ROOT)


from eval.g5b_real_memory_evaluator.failure_taxonomy import (  # noqa: E402
    ALL_LABELS,
)
from eval.g5b_real_memory_evaluator.lane_b import (  # noqa: E402
    LaneBDisabled,
    is_lane_b_enabled,
)
from eval.g5b_real_memory_evaluator.metrics import (  # noqa: E402
    compute_metrics,
)
from eval.g5b_real_memory_evaluator.runner import (  # noqa: E402
    DEFAULT_RECALL_LIMIT,
    run_scenarios,
)
from eval.g5b_real_memory_evaluator.scenario_schema import (  # noqa: E402
    ALLOWED_CATEGORIES,
    count_by_category,
    load_scenarios,
)
from eval.g5b_real_memory_evaluator.gold_scenarios import (  # noqa: E402
    GOLD_IDS,
    gold_count,
)
from eval.g5b_real_memory_evaluator.scenarios import (  # noqa: E402
    scenarios_v1,
    scenarios_v2,
)


def _scenarios_from_argv(argv: list[str]) -> tuple[list, str]:
    """Load scenarios from one or more --scenarios JSON files.

    Falls back to the in-package corpus selected by ``--corpus``
    default ``v1`` to preserve the original baseline contract; use
    ``v2`` for the 86-scenario expanded corpus or ``gold`` for the
    25-scenario regression subset). When ``--scenarios`` is passed,
    it takes precedence and ``--corpus`` is ignored. Returns
    ``(scenarios, source_label)``.
    """
    parser = argparse.ArgumentParser(
        prog="g5b_real_memory_evaluator",
        description="Run the G5b REAL MEMORY EVALUATOR v1 against the "
        "ActiveMemoryWriter/Reader boundary.",
    )
    parser.add_argument(
        "--scenarios",
        action="append",
        default=[],
        help="Path to a JSON scenario file. May be passed multiple times.",
    )
    parser.add_argument(
        "--corpus",
        choices=("v1", "v2", "gold"),
        default="v1",
        help="In-package corpus to run. 'v1' is the original 40-scenario "
        "baseline (default; preserves the contract pinned by the "
        "existing regression tests). 'v2' is the 86-scenario expanded "
        "corpus. 'gold' is the 25-scenario regression subset. "
        "Ignored when --scenarios is passed.",
    )
    parser.add_argument(
        "--lane",
        choices=("a", "b"),
        default="a",
        help="Lane A (deterministic in-memory) or Lane B (live PG). "
        "Default: a. Lane B requires --live-pg and a disposable DSN.",
    )
    parser.add_argument(
        "--live-pg",
        action="store_true",
        help="Opt in to Lane B. Refuses to run without a disposable DSN.",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="Disposable PG DSN for Lane B. If unset, falls back to "
        "$G5B_EVAL_LAB_DSN.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_RECALL_LIMIT,
        help="Per-query recall limit (default 5).",
    )
    parser.add_argument(
        "--out-dir",
        default="outputs/baseline",
        help="Directory to write summary/results/log files.",
    )
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        choices=ALLOWED_CATEGORIES,
        help="Filter scenarios to one or more categories. May be repeated.",
    )

    args = parser.parse_args(argv)

    if args.scenarios:
        scenarios = []
        for path in args.scenarios:
            scenarios.extend(load_scenarios(path))
        source_label = "argv:" + ",".join(args.scenarios)
    else:
        # Pick the in-package corpus.
        from eval.g5b_real_memory_evaluator.scenario_schema import scenario_from_dict
        if args.corpus == "v2":
            raw = scenarios_v2.all_scenarios()
            source_label = "scenarios_v2"
        elif args.corpus == "gold":
            # Gold mode: resolve IDs against the v2 corpus so the
            # runner gets the full Scenario objects (not just IDs).
            corpus_by_id = {
                s["scenario_id"]: s for s in scenarios_v2.all_scenarios()
            }
            raw = [corpus_by_id[sid] for sid in GOLD_IDS]
            source_label = f"gold_scenarios[{gold_count()}]"
        else:  # default: v1 baseline
            raw = scenarios_v1.all_scenarios()
            source_label = "scenarios_v1"
        scenarios = [scenario_from_dict(d) for d in raw]

    if args.category:
        scenarios = [s for s in scenarios if s.category in args.category]

    # Stash parsed args on a global for downstream use.
    global _PARSED_ARGS
    _PARSED_ARGS = args
    return scenarios, source_label


_PARSED_ARGS: argparse.Namespace | None = None


def _write_outputs(
    out_dir: str,
    source_label: str,
    lane: str,
    scenarios: list,
    results: list,
    metrics: Any,
) -> dict:
    """Write results.json / summary.json / run.log. Return summary dict."""
    os.makedirs(out_dir, exist_ok=True)

    # results.json
    results_payload = {
        "version": "g5b-v1",
        "source": source_label,
        "lane": lane,
        "scenarios": [
            {
                "scenario_id": r.scenario_id,
                "category": r.category,
                "passed": r.passed,
                "pipeline_status": r.pipeline_status,
                "failure_label": r.failure_label,
                "unsupported": r.unsupported,
                "elapsed_ms": r.elapsed_ms,
                "notes": r.notes,
                "per_query": [
                    {
                        "query_id": q.query_id,
                        "text": q.text,
                        "expected_lane": q.expected_lane,
                        "actual_lane": q.actual_lane,
                        "returned_memory_ids": q.returned_memory_ids,
                        "must_recall_hit_ranks": q.must_recall_hit_ranks,
                        "must_not_recall_violations": q.must_not_recall_violations,
                        "archived_returned": q.archived_returned,
                        "elapsed_ms": q.elapsed_ms,
                        "failure_label": q.failure_label,
                    }
                    for q in r.per_query
                ],
            }
            for r in results
        ],
    }
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(results_payload, f, ensure_ascii=False, indent=2)

    summary = {
        "version": "g5b-v1",
        "source": source_label,
        "lane": lane,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "metrics": {
            "scenario_count": metrics.scenario_count,
            "unsupported_count": metrics.unsupported_count,
            "pass_count": metrics.pass_count,
            "fail_count": metrics.fail_count,
            "error_count": metrics.error_count,
            "scenario_pass_rate": metrics.scenario_pass_rate,
            "must_recall_hit_rate": metrics.must_recall_hit_rate,
            "must_not_recall_violation_rate": metrics.must_not_recall_violation_rate,
            "hit_at_1": metrics.hit_at_1,
            "hit_at_3": metrics.hit_at_3,
            "hit_at_5": metrics.hit_at_5,
            "mrr": metrics.mrr,
            "false_positives": metrics.false_positives,
            "misses": metrics.misses,
            "archive_violations": metrics.archive_violations,
            "conflict_violations": metrics.conflict_violations,
            "pipeline_errors": metrics.pipeline_errors,
            "median_latency_ms": metrics.median_latency_ms,
            "p95_latency_ms": metrics.p95_latency_ms,
            "per_label_count": metrics.per_label_count,
            "per_category_pass_rate": metrics.per_category_pass_rate,
        },
        "scenarios_by_category": count_by_category(scenarios),
        "labels_taxonomy": list(ALL_LABELS),
        "is_deterministic_lab": lane == "a",
        "is_live_provider_backed": lane == "b",
        "honest_baseline_note": (
            "Lane A is a deterministic lab measurement against an in-memory "
            "SQLite-backed fake pool; not a production-runtime measurement. "
            "Lane B (only when --live-pg is set with a disposable lab DSN) "
            "is the closest to a real-runtime measurement; still bounded to "
            "a disposable lab database."
        ),
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    log_lines = [
        f"timestamp_utc={summary['timestamp_utc']}",
        f"lane={lane}",
        f"source={source_label}",
        f"scenarios={metrics.scenario_count}",
        f"pass_rate={metrics.scenario_pass_rate:.3f}",
        f"must_recall_hit_rate={metrics.must_recall_hit_rate:.3f}",
        f"must_not_recall_violation_rate={metrics.must_not_recall_violation_rate:.3f}",
        f"hit_at_3={metrics.hit_at_3:.3f}",
        f"mrr={metrics.mrr:.3f}",
        f"pipeline_errors={metrics.pipeline_errors}",
        f"unsupported={metrics.unsupported_count}",
    ]
    with open(os.path.join(out_dir, "run.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")

    return summary


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        scenarios, source_label = _scenarios_from_argv(argv)
    except SystemExit:
        return 2  # argparse already printed
    except Exception as exc:
        print(f"[g5b-eval] failed to load scenarios: {exc!r}", file=sys.stderr)
        traceback.print_exc()
        return 1

    args = _PARSED_ARGS
    lane = args.lane

    if lane == "b":
        if not args.live_pg:
            print(
                "[g5b-eval] Lane B requires --live-pg; refusing to open "
                "any production-grade connection.",
                file=sys.stderr,
            )
            return 1
        # Export the DSN so lane_b picks it up.
        if args.dsn:
            os.environ["G5B_EVAL_LAB_DSN"] = args.dsn
        os.environ["G5B_EVAL_LIVE_PG"] = "1"
        if not is_lane_b_enabled():
            print(
                "[g5b-eval] Lane B guard refused: G5B_EVAL_LAB_DSN missing or "
                "unsafe.",
                file=sys.stderr,
            )
            return 1

    try:
        results, query_records_by_scenario = run_scenarios(
            scenarios, lane=lane, limit=args.limit
        )
    except LaneBDisabled as exc:
        print(f"[g5b-eval] Lane B disabled: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Lane B path: a raw runner exception may carry DSN / password
        # data in its args tuple (notably psycopg2.OperationalError).
        # Print a type-only diagnostic and DO NOT echo repr(exc) or
        # the traceback — those would leak the lab credential. Lane A
        # diagnostics above (the scenario-load path) remain unchanged
        # because Lane A is fully in-memory and carries no secrets.
        if lane == "b":
            print(
                f"[g5b-eval] Lane B runner error: {type(exc).__name__} "
                "(message suppressed for safety)",
                file=sys.stderr,
            )
            return 1
        print(f"[g5b-eval] runner exception: {exc!r}", file=sys.stderr)
        traceback.print_exc()
        return 1

    metrics = compute_metrics(results, query_records_by_scenario)
    summary = _write_outputs(args.out_dir, source_label, lane, scenarios, results, metrics)

    print(
        f"[g5b-eval] lane={lane} scenarios={metrics.scenario_count} "
        f"pass={metrics.pass_count} fail={metrics.fail_count} "
        f"unsupported={metrics.unsupported_count} "
        f"pass_rate={metrics.scenario_pass_rate:.3f} "
        f"output_dir={args.out_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())