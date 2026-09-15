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

## Corpus scope — ONE LOCOMO SAMPLE = ONE INDEPENDENT MEMORY CORPUS

A LoCoMo sample is one conversation. The public loader defines
`LoCoMoSample = one sample / one conversation`, and this runbook therefore
requires:

- **ALL SESSIONS OF THAT SAMPLE REMAIN TOGETHER.** The memory system is supposed
  to recall across the whole long conversation, so narrowing the corpus to the
  question's own session is forbidden.
- **NO ROWS FROM ANOTHER SAMPLE MAY BE VISIBLE.** Cross-sample contamination is
  forbidden.

Run the full benchmark with `--sample-isolated`. The evaluator then gives every
sample its own freshly created disposable database, imports only that sample's
rows, runs only that sample's questions, destroys that database, and finally
aggregates every case by canonical `case_id` (still exactly `1,986` questions),
writing `per-sample-metrics.json` and `corpus-isolation.json` alongside the usual
artifacts.

Importing all 10 samples into one corpus is a **different experiment**. The
mature 2026-08-11 LoCoMo work (`load_locomo_all.py`, "10 个 conv 混合入库") and the
G6C-A / G6C-B0 / G6C-B1 runs — whose manifests record `sample_count: 10` — all
used that shared-corpus shape, so they are classified as `*_MIXED_*` diagnostic
history. They are **not** the canonical LoCoMo quality baseline.

Measured difference (canary, exact search, 12 cases over 3 samples): only `4/12`
ranked lists and `4/12` candidate sets agreed between MIXED and ISOLATED, and the
gold source id appeared in the candidate set for `1/12` MIXED cases versus `4/12`
ISOLATED cases. For `conv-30|0` the MIXED candidate set was 100% `conv-26` rows
with the gold absent, while ISOLATED produced 15 `conv-30` candidates.

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

## One documented full-run entry point

The evaluator is driven by a single small CLI:

```bash
python -m eval.locomo_recall_v2 run \
    --dataset /path/to/locomo10.json \
    --expected-sha256 79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4 \
    --dsn "host=127.0.0.1 port=55465 dbname=lab user=lab password=lab" \
    --output-dir /tmp/locomo-run-001 \
    --cache-dir /tmp/locomo-cache \
    --source-config /path/to/source.config.yaml \
    --mode dry-run
```

The bare invocation `python -m eval.locomo_recall_v2` is equivalent to `python -m eval.locomo_recall_v2 run`.

The CLI accepts an **already-created** disposable loopback DSN — it never brings up the lab container. Use a non-production port (not `5433`) on `localhost`/`127.0.0.1`/`::1`. The DSN is validated by `lab.validate_disposable_dsn` before any state is built; production ports / DB names / hostnames are refused and the error message is sanitised.

The CLI accepts a **read-only** YAML source config via `--source-config`. ONLY the `storage.embed` sub-dict is consumed; every other top-level key is rejected. Forbidden credential-shaped keys (`api_key` / `password` / `secret` / `token` / `private_key` / `production_db_dsn` / `production_endpoint`) at any nesting depth cause `SourceConfigRefused` and exit 1.

### Stages

`--mode` selects one of two explicit stages. The higher stage implies the lower one.

| mode         | What it does                                                                                                                                                                                                                                                                                                                                                                              | What it never does                                |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------------------|
| `dry-run`    | Verify dataset SHA-256, hash the source config, read ONLY `storage.embed` from the YAML, build the deterministic import rows, build the sanitised manifest (with the source-config SHA), validate the disposable DSN, create a real empty disposable experiment directory, build the isolated lab config, optionally write `import-rows.json`. Default; safe dry-run. | No PG. No provider. No HTTP.                       |
| `semantic`   | All of `dry-run`, plus open the disposable lab connection, bootstrap the schema, import the lab rows, prepare per-question query vectors via `embeddings.prepare_query_embeddings` and per-row corpus vectors via `embeddings.prepare_corpus_embeddings`, build the per-case `q_emb_by_case`, run the production-shaped facade sequentially via `runner.run_case`, map candidate IDs through `lab.build_provenance_map` + `lab.map_candidate_ids`, compute metrics + coverage, write `semantic-canary-results.jsonl` + `full-objective-results.jsonl` + `metrics.json` + `coverage-audit.json` + `latency.csv` + `embedding-cache-manifest.json` + `fingerprint.json`. With `--repeatability`, a second cached pass writes `repeatability.json` with metric/latency deltas — no provider calls on the second run. | No second retrieval path of its own. No answer-judge claim. |

This is the explicit, no-answer-judge sequence. Answer scoring is not part of this CLI; if a judge is approved later, it MUST be added as a separate `--judge` flag that is off by default — never substituted silently into `semantic`.

### Exit codes

| code | meaning                                       |
| ---- | --------------------------------------------- |
| 0    | success                                       |
| 1    | user / input error (bad SHA, refused DSN, forbidden source-config key, refused base path, malformed YAML, missing file) |
| 2    | integration gap (a cross-module seam is missing in the worktree — the CLI refuses to fabricate a degraded path) |
| 3    | any other uncaught error                      |

Exit code 2 is the loud-fail signal for the parent driver: the CLI raises `IntegrationTODOError` and refuses to fabricate a "looks-like-semantic" path with a keyword-only or zero-vector run.

### Isolated lab config (plain dict, never `V3Config`)

The CLI builds a single plain dict and passes it to the embedding-preparation / runner seams. It is **not** a `V3Config`:

| key                   | value contract                                                                                                            |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `lab`                 | `True` — disposable marker.                                                                                                |
| `basePath`            | a real disposable experiment directory created by the CLI itself under `--cache-dir`. NEVER an empty string and NEVER a fallback. Only evaluator-owned files (`v3_cards.db` and its `-wal` / `-shm` / `-journal` siblings, written by the facade's sqlite store) may already exist inside; any other entry makes the CLI refuse to reuse the directory. |
| `storage.pg`          | **psycopg2 kwargs** parsed from the loopback lab DSN the caller passed via `--dsn`: `host`, `port` (int), `database`, `user`, `password`. |
| `storage.embed`       | the borrowed ephemeral provider config — the YAML `storage.embed` sub-dict the caller passed via `--source-config`.       |
| `rerank_enabled`      | `False` by default; `True` only when `--rerank` is set.                                                                  |
| `rerank_endpoint_env` | env-var NAME only; never a raw URL or key.                                                                                |
| `output_dir` / `cache_dir` | explicit paths from `--output-dir` / `--cache-dir`.                                                                   |

⚠️ `storage.pg` is deliberately **not** a `{"dsn": ...}` string. `PgEmbedStore._get_pg_config`
reads this block as psycopg2 kwargs, so a `dsn` key is silently ignored and the store falls back to
`localhost:5433` — i.e. the *production* endpoint. A DSN that does not spell out host, database,
user and an explicit numeric port is refused (`LabConfigRefused`, exit 1) instead of defaulting.

Any `api_key` / `password` / `secret` / `token` / `private_key` / `production_db_dsn` / `production_endpoint` key in the YAML source config or in a returned diagnostic is refused; the CLI raises `SourceConfigRefused` / `LabConfigRefused` and exits with code 1. The CLI never constructs or accepts a production `V3Config`, never serialises a raw credential, never resolves an env-var NAME into a value, and never stores the resolved env-var value in the config dict, the manifest, the JSONL output, or the cache. The one permitted exception is the ephemeral `api_key` / `apikey` inside the isolated `storage.embed` block (the provider needs it in memory); every artifact write additionally passes through `redact_secrets_in_payload`, which masks **both** credential-shaped substrings (`password=…`) **and** the value of any credential-shaped dict key — so a bare `{"api_key": "<value>"}` cannot survive a redaction pass.

### Outputs

`--mode dry-run` writes only:

    benchmark-manifest.json
    import-rows.json   (when --write-rows)

`--mode semantic` writes:

    benchmark-manifest.json
    semantic-canary-results.jsonl
    full-objective-results.jsonl
    metrics.json
    coverage-audit.json
    latency.csv
    embedding-cache-manifest.json
    fingerprint.json                 (provider/model/dim/profile fingerprint with endpoint stripped)
    repeatability.json               (when --repeatability)

No `secrets` / `context` blocks are emitted. The CLI never serialises the resolved API key value, the resolved endpoint URL, or the lab DSN password to any output file. The manifest carries the source-config SHA + the dataset SHA + the provider/model / dim identity; the fingerprint.json carries the canonical EmbedProfile fingerprint with the endpoint URL stripped.

### Boundary with the integration TODO seam

The semantic stage calls into `eval.locomo_recall_v2.embeddings` for `prepare_query_embeddings` / `prepare_corpus_embeddings` / `build_query_rows` / `EmbedRow`. When that module is not importable in the worktree, the CLI raises `IntegrationTODOError` and exits 2; it does NOT fall back to a keyword-only or zero-vector path under `mode=semantic`. The CLI also never re-implements retrieval or embedding — the production facade, the embedding-preparation module, the runner, the metrics module, the lab provenance map and the compare module are all reused by direct import.

### Provenance map

The CLI does NOT implement retrieval SQL. It reuses `lab.build_provenance_map` + `lab.map_candidate_ids` to convert production-shaped candidate IDs (`qa_<n>`, `topic_<n>`, numeric `conversation_stream.id`) to canonical LoCoMo `source_id` strings BEFORE metrics are computed.

The index is built from **read-back rows of the evaluator's own lab**, not from import ordinals:
`SELECT id, source_id FROM qa_pairs` supplies `qa_id_index`, `SELECT id, tool_calls FROM
conversation_stream` supplies the `dia_id → source_id` map via the rows' own provenance. The
mapping is deterministic across re-runs, never consults production recall, and reports its tallies
(`id_mapping: input / resolved_qa / resolved_conv / resolved_topic / unresolved / unknown_shape`)
in the run summary.

`CaseRecord` is a **frozen** dataclass, so mapping rebuilds each record with `dataclasses.replace`
and returns `(records, audit)`. Do not reintroduce `setattr`-style mutation: a swallowed
`FrozenInstanceError` silently leaves every ranking in raw `qa_<id>` form and forces hit rates to
zero by construction.

### Metric contract (production-shaped `limit=5`)

The facade ranks at `limit=5`, so `MetricSnapshot` records `ranking_limit` and `hit_at_k_k` and the
headline set is `Hit@1` / `Hit@5` / MRR / mean relevant rank, all computed at the observed depth
`min(K, ranking_limit)`. `Hit@5` is N/A (JSON `null` + `hit_at_5_status="n_a"`) whenever the
observed surface is shallower than 5 — a short surface must never be scored as if it were deeper.
Never change the production recall limit to manufacture a larger K.

Gold evidence keeps three states end-to-end: **MAPPED** (`gold.source_ids`), **UNMAPPED**
(`gold.unmapped_dia_ids` — real dia_id, no benchmark pair maps it) and **UNRESOLVED** (compound /
malformed entry that cannot be parsed as one dia_id). They are never merged; the headline
denominator is the mapped set and all three counts are reported.

### Environment pin (required)

`PYTHONPATH=src/v3-core` alone resolves `import v3core` to the **installed** package, not to the
worktree source, which silently evaluates the wrong engine. Always run with both roots:

```bash
export PYTHONPATH="<worktree>/src/v3-core;<worktree>/src/v3-core/src"
export PYTHONHASHSEED=0
```

### Fail-closed run policy

After the facade pass the CLI aborts (`CLIError`, exit 3) if any case status is not `ok`, printing
the first offending `case_id`, status and error. An objective run with missing/invalid query
vectors, missing traces or engine errors must not be written out as if it had measured retrieval
quality.

### Embedding preparation contract

Corpus vectors mirror the **live** production write path, not the legacy bulk importer:
`qa_pairs` texts are embedded as `f"{q}\n{a}"` (question only when the answer is empty) — the
`_flush_pending_qa` rule — and `conversation_stream` texts as `content[:2000]` — the `ingest` rule.
The historical `tools/import_.py` importer used `question[:1000]`; it is NOT the canonical live
lane and must not be copied. Query vectors are prepared per question (never one shared vector).
Vectors are attached to the import rows before `lab.import_rows`, so the lab stores real
`vector(1024)` values instead of NULLs. `embed_model` is left NULL in the lab: production writes the
`EmbedProfile` fingerprint there, but nothing on the retrieval path reads that column, so the
fingerprint is recorded in the cache manifest / identity path / run summary instead.

### Repeatability

`--repeatability` runs a second cached pass and writes `repeatability.json` with:

* per-case JSONL diff via `compare.compare_files` (joined on `case_id`, NOT row order);
* headline metric deltas (`hit_at_1` / `hit_at_5` / `hit_at_k` / `mrr` / `mean_relevant_rank`);
* per-case latency deltas in milliseconds, with summary `max_abs_delta_ms` and `mean_delta_ms`.

The second pass uses the cached query vectors — no provider calls. A non-zero delta therefore indicates a non-deterministic engine surface, not a flaky embedding pass.

### CLI-side test coverage

Dedicated tests live in `tests/test_cli.py`. They cover, without calling provider / PG:

  * argument-parser shape (all 14 documented flags, including `--mode {dry-run, semantic}`, `--case-limit`, `--sample-filter`, `--batch-size`, `--rerank` / `--rerank-endpoint-env`, `--repeatability`, `--commit-sha`, `--write-rows`);
  * `redact_secrets_in_payload` strips `password=` / `api_key=` / `token=` / `secret=` strings;
  * `load_source_config_embed_section` reads ONLY `storage.embed` and rejects forbidden keys at every nesting depth (no real secrets in any test fixture);
  * `build_isolated_lab_config` rejects non-loopback / reserved-port / reserved-DB / empty DSNs and refuses `basePath` when it is empty / non-existent / non-empty;
  * `run_dry_run` writes a sanitised manifest whose `provider_id` / `model_id` / `embedding_dim` match the contract, hashes the source config, and makes ZERO embeddings / lab.bootstrap_schema / lab.import_rows calls;
  * `run_dry_run` applies `--case-limit` and `--sample-filter` correctly;
  * `run_dry_run` returns exit 1 on a wrong SHA, refused DSN, forbidden source-config key, or missing dataset;
  * `main()` with `--mode dry-run` exits 0 and writes `benchmark-manifest.json`;
  * `main()` with `--mode semantic` exits 2 when the embedding-preparation seam is forced-unavailable;
  * `--rerank` without `--rerank-endpoint-env` is refused with exit 1.

## Cleanup

After the run, remove only the exact disposable container and exact external result directory created for this run. Verify:

- the disposable container is gone;
- production PG `5433` was never targeted;
- production config mtime/hash is unchanged;
- the public worktree contains no raw benchmark output, secrets or temporary PG data.

Historical evaluators that use `active_memory_store`/G5B Lane A, direct raw-message cosine retrieval, or the old observation-note-only v6/v7 runner are retired for current Recall V2 measurement. The historical answer report remains a separate memory-continuity reference, not a retrieval baseline.
