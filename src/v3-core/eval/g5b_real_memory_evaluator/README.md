# G5b REAL MEMORY EVALUATOR v1

> Development infrastructure (NOT production) for the public
> ``v3core.active_memory_store`` clean boundary. Two explicit lanes,
> an 86-scenario synthetic corpus across 12 categories (Track E
> expansion: v1 baseline 40 ∪ 46 paired/expansion cases), a 25-case
> GOLD REGRESSION subset, and the smallest honest reproducible
> evaluator we could ship.

The canonical scenario count is the runtime value returned by
``scenarios_v2.all_scenarios()`` (currently 86). Documentation must
agree with that count; do not hard-code a different total.

## Honest contract

| Lane | Source | Always available? | What it measures | What it does NOT measure |
|------|--------|-------------------|------------------|---------------------------|
| **A** | Deterministic in-memory SQLite-backed fake pool (Lane A pool) | Yes | The writer/reader contract on a controlled lab fixture set with a stub embedder | Real production recall, real provider semantics, IVFFLAT planner behaviour |
| **B** | Real disposable lab PG (only when explicitly opted in) | No — refuses by default | Closest to real-runtime measurement; still bounded to a disposable lab DB | Production traffic, production weights, rerank semantics |

Lane A is the **default baseline**. The baseline is honest: it is a
deterministic lab measurement, not a production-runtime measurement.
Lane B is opt-in; running ``python -m eval.g5b_real_memory_evaluator.run``
without flags will NEVER open a real PG connection.

## What the evaluator checks

For each scenario, the runner:

1. Seeds ``fixtures`` via ``ActiveMemoryWriter.create``.
2. Applies any pre-recall actions (archive, archive_hard, create_extra).
3. Runs each query through the reader (keyword lane, vector lane, or
   auto union).
4. Tags the earliest-layer failure using the failure taxonomy.

Every scenario produces a ``ScenarioResult`` with per-query trace
fields (scenario_id / query_id / expected IDs / returned IDs / rank /
score / lane / latency / pass / failure category). No payload beyond
what the reader already returns.

## Failure taxonomy (earliest layer first)

| Label | Meaning |
|-------|---------|
| NOT_STORED | The write path never committed a canonical row. |
| NOT_INDEXED | Row durable but embedding missing or embed_model missing. |
| NOT_RETRIEVED | Row durable and indexed; reader returned no candidates. |
| RANKED_TOO_LOW | Expected memory present but rank > must_recall_rank_max. |
| WRONG_MEMORY | Candidate matches a non-expected memory that violates must_not_recall. |
| STALE_MEMORY | Candidate matches a memory whose status / content drifted. |
| ARCHIVED_MEMORY_RETURNED | Reader returned a row whose status is 'archived'. |
| CONFLICT_RESOLUTION | Writer accepted a same-id different-payload attempt. |
| PIPELINE_ERROR | Unexpected exception leaked from writer/reader. |
| TIMEOUT | PrefetchDeadlineExceeded. |
| UNSUPPORTED | Scenario is tagged as not yet implemented by the runtime. |
| UNKNOWN | Reviewer-visible bug — none of the above matched. |

## Metrics

The metrics module reduces ``ScenarioResult`` records into:

* scenario_pass_rate           — pass / (pass + fail); UNSUPPORTED excluded.
* must_recall_hit_rate         — fraction of queries where any expected memory hit top-K.
* must_not_recall_violation_rate — fraction of queries where a forbidden id was returned.
* hit_at_1 / hit_at_3 / hit_at_5
* mrr                          — mean reciprocal rank of first must_recall hit.
* false_positives / misses / archive_violations / conflict_violations
* pipeline_errors / unsupported_count
* median_latency_ms / p95_latency_ms
* per_label_count / per_category_pass_rate

## Running the evaluator

```bash
# Default Lane A baseline (v1 corpus, 40 scenarios, backward-compat default)
PYTHONPATH=src/v3-core python -m eval.g5b_real_memory_evaluator.run \
    --out-dir outputs/baseline

# Track-E expanded corpus (v2 corpus, 86 scenarios)
PYTHONPATH=src/v3-core python -m eval.g5b_real_memory_evaluator.run \
    --corpus v2 --out-dir outputs/v2_corpus

# Gold regression subset (25 cases; fast feedback loop for CI)
PYTHONPATH=src/v3-core python -m eval.g5b_real_memory_evaluator.run \
    --corpus gold --out-dir outputs/gold_subset

# Filter to one or more categories
PYTHONPATH=src/v3-core python -m eval.g5b_real_memory_evaluator.run \
    --category STABLE_FACT --category USER_PREFERENCE \
    --out-dir outputs/baseline

# Lane B — requires --live-pg AND a disposable DSN; refuses by default.
G5B_EVAL_LIVE_PG=1 PYTHONPATH=src/v3-core \
    python -m eval.g5b_real_memory_evaluator.run --lane b --live-pg \
        --dsn "host=127.0.0.1 port=55462 dbname=lab_eval user=lab password=lab" \
        --out-dir outputs/live-pg
```

The ``--corpus`` flag selects which in-package scenario set the
runner uses:

| Value  | Source                | Count | Purpose                                  |
|--------|-----------------------|-------|------------------------------------------|
| ``v1`` (default) | ``scenarios_v1`` | 40    | original baseline; preserves the pinned contract |
| ``v2`` | ``scenarios_v2``     | 86    | Track-E expanded corpus (40 ∪ 46)       |
| ``gold``| ``gold_scenarios``   | 25    | fast regression subset; spans all 12 categories |

## Output artifacts

| File | Contents |
|------|----------|
| ``results.json`` | Every ``ScenarioResult`` with per-query trace |
| ``summary.json`` | ``MetricsReport`` + lane label + honest-baseline note |
| ``run.log``     | One-line human-readable summary |

## Lane B safety guard

``lane_b._validate_disposable_dsn`` rejects:

* reserved ports (e.g. ``5433``),
* reserved dbnames (``v3embeddings``, ``v3embeddings_eval``, ``v3embeddings_eval_v2``),
* reserved hostnames (production v3-pgvector hostnames),
* PGUSER env var matching the DSN user (unless explicitly opted in via
  ``G5B_EVAL_LAB_DSN_OPT_IN=1``).

The guard never silently falls back to Lane A; running Lane B without
a disposable DSN fails closed with ``LaneBDisabled``.

## Tests

```bash
PYTHONPATH=src/v3-core python -m pytest \
    src/v3-core/eval/g5b_real_memory_evaluator/tests/ -v
```

The test surface covers:

* failure taxonomy labels and normalization,
* Lane A writer happy-path / dedup / conflict / archive / hard archive,
* deterministic embedder reproducibility,
* Lane B safety guard (reserved port/dbname/host, env-only, both-set),
  Lane B safe-exception-message (no DSN/password echo), and Lane B
  schema DDL path resolution,
* scenario schema validation, category coverage, exact v1 scenario
  count pin (40), and the ``explicit_memories`` alias / ``sessions``
  with ``conversation_turns`` + ``turns`` alias compatibility fields,
* runner wiring and metrics invariants (no pipeline errors, per-label
  counts sum to scenario count),
* Track-E corpus expansion: v2 size in 80-100 band, v2 ⊇ v1,
  v2 covers all 12 categories, every scenario parses, no duplicate
  scenario IDs, expansion ∩ v1 = ∅, v2 runs end-to-end with zero
  pipeline errors,
* Gold regression subset: 20-30 cases, all 12 categories covered,
  unique IDs, exact subset of v2, all gold IDs parse and run end-to-end,
  gold CLI flag loads through the ``run`` module path,
* CLI corpus selection: ``--corpus {v1,v2,gold}`` plus ``--scenarios``
  file path precedence over ``--corpus``.

Two test modules:

* ``test_g5b_evaluator.py`` — the original 46 tests (unchanged contract).
* ``test_corpus_expansion.py`` — 20 new Track-E tests.

## Scenario sets

### v1 baseline (40)

40 hand-authored JSON scenarios across the 12 required categories
(see ``scenarios/scenarios_v1.py``). Every fixture is synthetic; no
real names, IPs, or private data. Memory IDs are derived from
``(category, title, content, tags)`` via ``derive_memory_id`` so the
JSON stays stable across machines.

### v2 expanded (86)

``scenarios/scenarios_v2.py`` composes the v1 baseline (40) with 46
NEW paired/expansion cases (no fillers, every scenario fills a
specific coverage gap):

* same query / different historical fact (DISTRACTOR pairs)
* same history / different query (intent disambiguation)
* relevant vs tempting distractor (NEGATIVE_RECALL pairs)
* active vs archived (ARCHIVE pairs)
* old vs superseded (TEMPORAL_UPDATE pairs)
* short vs longer history (LONG_DISTANCE expansion)
* bare keyword vs multi-token query (STABLE_FACT expansion)

Category coverage of the 46 added scenarios:

  USER_PREFERENCE    +4 — terminal multiplexer / IDE font / notification / break
  PROJECT_DECISION   +4 — python linter / packaging / API schema / embedding model
  SESSION_CONTINUITY +3 — TODO carryover / env-var carryover / naming rule carryover
  STABLE_FACT        +4 — locale / billing currency / docs language / cloud region
  CONFLICT_UPDATE    +3 — category mutation / status mutation / embedding-model drift
  DISTRACTOR         +5 — port triplet / env var / branch / region / alias pair
  NEGATIVE_RECALL    +4 — host / draft-vs-published / framework / competitor
  ARCHIVE            +3 — soft supersede / hard-idempotent / dual archive
  LONG_DISTANCE      +4 — 4KB content / 30 fixtures / long bg / cross-category
  MULTI_RELEVANT     +4 — postgres-only / subset / 2-yr team / auth tri
  AMBIGUOUS          +4 — fast facets / security homonym / pipeline / deploy-day
  TEMPORAL_UPDATE    +4 — deploy tool / schema version / project name / v3 release

### Gold regression subset (25)

``gold_scenarios.py`` is the machine-readable 25-scenario regression
subset, hand-picked to (a) cover all 12 categories, (b) span the
failure-taxonomy surface, (c) mix v1 stable cases with v2 expansion
cases, and (d) run quickly (<1s on the lab fake pool).

Per-category gold composition (counts must stay fixed; ``GOLD_IDS``
is the canonical list):

  USER_PREFERENCE     x2  (UP-001, UP-101)
  PROJECT_DECISION    x2  (PD-001, PD-101)
  SESSION_CONTINUITY  x2  (SC-001, SC-101)
  STABLE_FACT         x2  (SF-001, SF-101)
  CONFLICT_UPDATE     x2  (CU-001, CU-101)
  DISTRACTOR          x2  (DT-001, DT-101)
  NEGATIVE_RECALL     x2  (NR-001, NR-101)
  ARCHIVE             x3  (AR-001, AR-002, AR-101)  ← extra
  LONG_DISTANCE       x2  (LD-001, LD-102)
  MULTI_RELEVANT      x2  (MR-001, MR-101)
  AMBIGUOUS           x2  (AMB-001, AMB-101)
  TEMPORAL_UPDATE     x2  (TU-001, TU-101)

Total: 25 scenarios. To run only the gold subset:

```bash
PYTHONPATH=src/v3-core python -m eval.g5b_real_memory_evaluator.run \
    --corpus gold --out-dir outputs/gold_subset
```

## Limitations of the baseline

Lane A's keyword lane is plain ``LIKE %q%`` — multi-word natural
language queries may fail with NOT_RETRIEVED even when the answer is
clearly the most relevant row. This is a real semantic gap of the
keyword lane, not a runner bug. The honest baseline reflects that
gap. Lane B (when run against a real pgvector DB) would surface the
same scenarios through a real IVFFLAT + RRF + rerank pipeline.