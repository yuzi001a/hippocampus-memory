# G5b Differential Evaluator (development-only)

The differential evaluator compares two **offline** G5b evaluator
result sets (BASE vs CANDIDATE) and exposes explicit improvement /
regression deltas. It is purely a comparison tool — it never runs the
evaluator, never opens PG, never touches production config / data /
services. It is safe to invoke on CI artefacts, lab dumps, or stored
baseline snapshots.

> **Scope.** This file documents the comparison mode only. The
> underlying runner is described in `README.md` next to this file.

## When to use

You have two `summary.json + results.json` pairs (typically a saved
baseline and a fresh candidate run) and you want to know — at a
glance — whether the candidate got better, worse, or moved on
specific scenarios. The differential evaluator answers that with one
JSON document.

## CLI

```bash
PYTHONPATH=src/v3-core python -m eval.g5b_real_memory_evaluator.compare \
    --base-results outputs/baseline/results.json \
    --base-summary outputs/baseline/summary.json \
    --cand-results outputs/candidate/results.json \
    --cand-summary outputs/candidate/summary.json \
    --out-json outputs/candidate/delta.json
```

By default the JSON delta report is written to stdout. With
`--out-json <path>` it is written to the file instead. With
`--fail-on-regression` the process exits with code `3` when the
verdict is `regressed` or `mixed` (handy for CI gating).

Exit codes:

| code | meaning                                                     |
|-----:|-------------------------------------------------------------|
| `0`  | Diff produced; verdict not regressed/mixed (or no gate).    |
| `2`  | Incompatible inputs (fail-closed; see "Safety" below).      |
| `3`  | `--fail-on-regression` triggered (verdict = regressed/mixed).|
| other| argparse usage error.                                       |

## Python API

```python
from eval.g5b_real_memory_evaluator.compare import (
    compare_results_dicts,
    compare_runs,
    report_to_dict,
    report_to_json,
)

# From file paths:
report = compare_runs(
    base_results_path="outputs/baseline/results.json",
    base_summary_path="outputs/baseline/summary.json",
    cand_results_path="outputs/candidate/results.json",
    cand_summary_path="outputs/candidate/summary.json",
)

# From pre-loaded dicts (e.g. inside a notebook or test):
report = compare_results_dicts(base_results, base_summary, cand_results, cand_summary)

# JSON-serialisable:
blob = report_to_json(report)
# or:
as_dict = report_to_dict(report)
```

## What the report contains

A `DeltaReport` exposes:

* **Headline deltas** (CANDIDATE − BASE):
  `pass_rate_delta`, `must_recall_hit_rate_delta`,
  `must_not_recall_violation_rate_delta`, `hit_at_1_delta`,
  `hit_at_3_delta`, `hit_at_5_delta`, `mrr_delta`.
* **Count deltas:** `pass_count_delta`, `fail_count_delta`,
  `error_count_delta`, `unsupported_count_delta`, `misses_delta`,
  `false_positives_delta`, `archive_violations_delta`,
  `conflict_violations_delta`, `pipeline_errors_delta`.
* **Latency deltas:** `median_latency_ms_delta`, `p95_latency_ms_delta`
  (negative = faster).
* **Scenario-level flips:**
  * `new_misses` / `fixed_misses` — scenario IDs whose
    recall-failure label flipped into / out of failure.
  * `new_false_positives` / `fixed_false_positives` — tagged as
    `<scenario>::<query>` so the exact query is identified.
  * `label_changes` — every scenario whose `pipeline_status` /
    `failure_label` flipped between runs, with a `direction` of
    `improved` / `regressed`.
* **Per-category deltas** sorted by `|delta|` descending
  (`PerCategoryDelta` list).
* **Verdict:** one of `improved`, `regressed`, `mixed`, `unchanged`.

For rate-style metrics, **positive** deltas mean the candidate got
better. For violation-count and latency metrics, **negative** deltas
mean the candidate got better. The verdict applies a 1% absolute
pass-rate threshold to keep single-scenario noise from triggering a
"regressed" verdict.

## Safety (fail-closed semantics)

The diff is unsafe when the two runs cover different scenarios. The
module refuses to silently mis-compare by raising
`IncompatibleScenarioSets` (a subclass of `ValueError`). Triggers:

* BASE / CANDIDATE `lane` differ (Lane A vs Lane B cannot be diffed).
* Scenario ID sets differ (lists the symmetric difference, capped to
  10 entries per side).
* Same scenario ID has different `query_id` sequences between runs.
* Either `results.json` is malformed (not a JSON object, missing
  `scenarios`, duplicate IDs, non-object entries).
| Either `summary.json` is malformed (missing `metrics`, unknown
  `lane`, missing required metric keys, or metric keys with wrong
  container type).
* `results.json['scenarios']` length disagrees with
  `summary.json['metrics']['scenario_count']`.
* `results.json['lane']` (when present) disagrees with
  `summary.json['lane']`.

Error messages **never echo payload contents**, DSN-shaped strings,
or any field that might carry credentials. They list structural
mismatches only.

## Tests

```bash
PYTHONPATH=src/v3-core python -m pytest \
    src/v3-core/eval/g5b_real_memory_evaluator/tests/test_compare.py -q
```

36 tests cover structural validation, fail-closed error paths
(missing metric keys, wrong container types, scenario-count and
results/summary lane mismatches), numeric deltas (pass rate, hit@k,
MRR, latency), per-category sorting, verdict classification, JSON
round-trip, and CLI exit codes.

## Files

```
src/v3-core/eval/g5b_real_memory_evaluator/
├── compare.py                      # the differential evaluator module
└── tests/
    └── test_compare.py             # 29 focused tests
```

The baseline evaluator (`run.py`, `runner.py`, `metrics.py`,
`scenario_schema.py`, `lane_a.py`, `lane_b.py`, `failure_taxonomy.py`)
is **untouched** by this worktree.
