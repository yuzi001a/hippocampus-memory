# LoCoMo → Recall V2 evaluation runbook

This is the public, disposable-lab workflow for the G6C-A evaluator. It is not a production operation and it does not run G6C-B ablations.

## Frozen dataset

Use the historical `locomo10.json` source externally, not a new dataset and not a checked-in raw blob.

- source SHA-256: `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`
- bytes: `2,805,274`
- samples: `10`
- sessions: `272` message-bearing sessions (`288` sibling `session_N_date_time` metadata keys)
- messages: `5,882`
- questions: `1,986`
- categories: `1:282`, `2:321`, `3:96`, `4:841`, `5:446`
- historical transform: `eval_v2`, builder `1.1.0`

Always verify the file hash before parsing. Source variants with another hash are a different benchmark and must not be compared silently.

## Environment and safety

Run from the repository root with the current checkout explicitly first:

```bash
git rev-parse HEAD
git status --short --untracked-files=all
export PYTHONPATH="$(pwd)/src/v3-core;$(pwd)/src/v3-core/src"
```

The experiment must use:

- a disposable PostgreSQL/pgvector container on loopback;
- a non-production port such as `55465`;
- an experiment manifest recording commit, source hash, provider/model/dimension and Python/dependency identity;
- embedding identity `BAAI/bge-m3`, dimension `1024`, when objective vector evaluation is actually enabled.

The lab guard refuses missing/non-loopback hosts, reserved production database names/hosts, and port `5433`. Never pass a production DSN, production profile, or production memory corpus.

## Build and verify the dataset

The loader is external-source-only and preserves the real historical source shape: list-valued `session_N` keys, sibling `session_N_date_time` keys, full text, speaker, dia ID and image fields. It creates stable QA-pair source IDs and a deterministic evidence map; it never fuzzy-matches text.

Use the loader and manifest builder from `eval.locomo_recall_v2.dataset` and `eval.locomo_recall_v2.manifest`:

```python
from eval.locomo_recall_v2.dataset import load_locomo, build_evidence_map, build_import_rows
from eval.locomo_recall_v2.manifest import build_manifest

data = load_locomo(SOURCE_PATH, EXPECTED_SHA256)
evidence_map = build_evidence_map(data)
rows = build_import_rows(data)
manifest = build_manifest(
    data,
    commit_sha=COMMIT_SHA,
    provider_id="siliconflow",
    model_id="BAAI/bge-m3",
    embedding_dim=1024,
)
```

Do not write the raw conversation corpus into the repository. External run artifacts may contain bounded per-case results but must not contain production memory payloads or credentials.

## Create and bootstrap disposable PostgreSQL

Create a fresh loopback-only container on a non-5433 port. The exact container lifecycle belongs to the external experiment, not the public source tree. After connecting, run:

```python
from eval.locomo_recall_v2.lab import validate_disposable_dsn, bootstrap_schema, import_rows
import psycopg2
from pgvector.psycopg2 import register_vector

DSN = "host=127.0.0.1 port=55465 dbname=lab_eval user=lab password=lab"
validate_disposable_dsn(DSN)
conn = psycopg2.connect(DSN)
register_vector(conn)
bootstrap_schema(conn, REPO_ROOT)
import_counts = import_rows(conn, rows)
```

The bootstrap applies the public alpha schema plus `explicit_memories.sql` at its include marker and creates the evaluator's additive `eval_queries` table. It must not contain destructive SQL. Read back table counts after import; a successful `INSERT` call without readback is not an accepted import.

## Run the current production-shaped facade

The only retrieval seam is:

```text
prefetch_to_context_block
  → RecallV2Engine
  → recall_pool
  → fusion/rerank
  → injection / RecallTrace published through the facade's existing `trace_out` channel

The adapter temporarily observes the module-level `prefetch` call only to capture that real internal trace; it does not pass a competing external trace and does not modify production source.
```

Use `eval.locomo_recall_v2.adapter.run_case` or `runner.run_cases`. Pass the disposable PG connection and, for an objective vector run, a real BAAI/bge-m3 query embedding. Do not make a keyword-only or zero-vector run look like a full objective run. A zero-vector/no-provider run is allowed only as a structural seam sanity check and must be labeled that way.

Each result preserves:

- stable `case_id = sample_id|query_idx`;
- question/category/gold answer reference;
- original gold evidence dia IDs;
- deterministically mapped gold source IDs and unresolved evidence;
- all candidate source IDs, final selected IDs and injected IDs;
- `RecallTrace` ID, lane summaries and injection/drop summary;
- elapsed time.

The JSONL writer does not emit the returned memory context body.

## Prove the real facade was exercised

The adapter guard test replaces the engine's legacy recall callable with a counting fake and runs the real `prefetch_to_context_block` facade. It must observe exactly one legacy recall invocation per request. A shim that bypasses the facade leaves the count at zero and is a failing guard. The adapter source must not import `active_memory_store`, `lane_a`, issue direct SQL, or implement a second retrieval path.

For a real run, keep an independent counter around the legacy recall callable if exact engine/recall_pool invocation counts are required in `coverage-audit.json`. Do not infer coverage from a successful process exit or a non-empty context string.

## Metrics and coverage

`eval.locomo_recall_v2.metrics.compute_metrics` reports retrieval separately from answer quality:

- evidence mapping coverage;
- Hit@1, Hit@5, Hit@k, MRR and relevant rank over mapped gold source IDs only;
- per-category breakdown;
- candidate/selected/injection counts;
- trace coverage and bypass count.

Answer scoring is optional. If no explicitly approved judge provider is available, write `UNAVAILABLE_IN_SAFE_ENV`; do not substitute a different judge and call it comparable.

Write bounded artifacts under the external results directory:

```text
benchmark-manifest.json
canary-results.jsonl
metrics.json
coverage-audit.json
latency.csv
failure-analysis.jsonl
run-log.md
```

Use `compare.py` to compare BASE/CANDIDATE JSONL by `case_id`, never by row order. A changed dataset hash, changed source row identity, changed evaluator contract or missing trace coverage makes the score `NOT COMPARABLE`.

## Canary and full run order

1. Run unit/mapping/bypass tests and compile the evaluator.
2. Load a known existing slice of the frozen dataset (conv-26 is the historical canary slice) into disposable PG.
3. Run the seam canary and read back counts, traces, candidate IDs, selected IDs and injection outcomes.
4. Only after the canary passes, run the mature full dataset with frozen algorithm constants. Do not tune RRF K, lane weights, half-life, rerank top-n, thresholds or candidate limits in this phase.
5. If the same-model embedding provider is unavailable, stop the full objective run and report `UNAVAILABLE_IN_SAFE_ENV`; retain the structural canary as non-quality evidence.

## Cleanup

After the run, remove only the exact disposable container and exact external result directory created for this run. Verify:

- the disposable container is gone;
- production PG `5433` was never targeted;
- production config mtime/hash is unchanged;
- the public worktree contains no raw benchmark output, secrets or temporary PG data.

Historical evaluators that use `active_memory_store`/G5B Lane A, direct raw-message cosine retrieval, or the old observation-note-only v6/v7 runner are retired for current Recall V2 measurement. The historical answer report remains a separate memory-continuity reference, not a retrieval baseline.
