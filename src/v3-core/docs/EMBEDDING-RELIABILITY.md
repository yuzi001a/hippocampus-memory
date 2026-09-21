# Embedding Reliability

**Root cause, policy matrix, caller matrix, and evidence.**

Companion document: `embedding-reliability-deploy-plan.md` (migration / deploy / rollback /
stop conditions / monitoring). That one is a PLAN and is not executed.

---

## 1. The defect, in one sentence

A durable write that forgets to name its timeout budget silently inherits the **realtime**
`3s / 0` default, so one transient provider timeout becomes a **permanent** NULL with no
record that a request was ever attempted.

`conversation_stream` did exactly that: `ingest.py` called `call_embedding(content[:2000],
self._embed_cfg)` with no budget, and `call_embedding`'s historical defaults were
`timeout=3, retries=0`.

Captured live, 2026-09-20 22:07:

```
ERROR v3core.embedding: embedding 调用失败 (重试0次后 raise, 不再静默):
fp=bf32771ecbd1 model=BAAI/bge-m3
err=HTTPSConnectionPool(host='api.siliconflow.cn', port=443): Read timed out. (read timeout=3)
```

Nine consecutive failures across 21:42:38 → 21:43:20 — one provider hiccup, zero retries,
nine permanent holes.

## 2. Measured production impact

`conversation_stream`, fixed windows taken before any change:

| window | new rows | new NULLs | NULL rate | marker coverage |
|---|---|---|---|---|
| 1h | 100 | 8 | 8.00% | 0% |
| 6h | 730 | 85 | 11.64% | 0% |
| 24h | 2289 | 196 | 8.56% | 0% |

Other tables, last 24h: `yin_paragraphs` 7/36 = **19.4%**, `observation_notes` 2/25 = 8.0%,
`qa_pairs` 0/161 = 0%.

Totals: `conversation_stream` 1002/11247, `qa_pairs` 12/13908, `yin_paragraphs` 8/1209,
`observation_notes` 3/733, `topics` 0/342.

**Two causes, separated by evidence:**

| regime | window | NULLs | avg len | >8k rows |
|---|---|---|---|---|
| API-key outage | 09-13 08 → 09-14 16 | 725 | 686 | 15 |
| intermittent + active leak | 09-15 → 09-20 | 276 | 289 | **0** |
| before | — | 1 | 98 | 0 |

The leak is **accelerating**, not steady: 1.2% (09-18) → 6–12% (09-19) → 12–14% (09-20).

Two facts that rule out the obvious hypotheses:

* **`source_empty` = 0.** Every one of the 1002 NULLs has non-empty source text. None is a
  legitimately-empty row; all are genuine losses.
* **The active leak is not long content.** 0 of the 262 active-leak rows exceed 8k chars
  (average 289). Meanwhile the API-key regime contains 15 rows over 8k — a different cause.

`embed_model` is uniformly `bf32771ecbd1`, so there is no cross-model vector mixing.

**Marker coverage for all historical NULLs is 0%** — `public.embedding_failures` does not
exist in production yet. Every historical classification above is **attribution by
evidence, not recorded state**, and must be quoted that way.

---

## 3. The contract this hotfix introduces

```
Embedding failure is allowed. Silent permanent memory loss is not.
```

| | before | after |
|---|---|---|
| provider timeout | permanent NULL | source saved + retryable marker |
| no budget named | silently realtime 3s/0 | must name a policy (guard test goes red) |
| "never attempted" vs "lost" vs "misconfigured" | indistinguishable | distinct `error_class` |
| repair target discovery | impossible | `LEFT JOIN ... WHERE f.id IS NULL` |
| retry behaviour | none | deferred `10s/2`, off the live writer |

### Success definition

```
Primary invariant :  new UNEXPLAINED NULL = 0
NOT required      :  new embedding failure = 0
```

A temporary, explained NULL is fine. A silent permanent NULL is not. A provider outage in
which every failure is correctly recorded is a **passing** state.

---

## 4. Policy matrix

| class | timeout / retries | blocks user turn | on exhaustion |
|---|---|---|---|
| realtime recall | 3s / 0 | **yes** | graceful recall degradation |
| **conversation_stream source write** | **primary 5s / 0**, deferred 10s/2 | **no — proven** | source saved + retryable marker |
| other background durable write | 10s / 2 | no | marker + backlog |
| batch / import | 10s / 2 | no | checkpoint + marker |
| health | 5s / 1 | diagnostic | explicit degraded |

Defined in `embedding.py`: `REALTIME_EMBED_POLICY` (3.0/0),
`STREAM_PRIMARY_EMBED_POLICY` (5.0/0), `DURABLE_WRITE_EMBED_POLICY` (10.0/2),
`BATCH_EMBED_POLICY` (10.0/2), `HEALTH_EMBED_POLICY` (5.0/1).
`STREAM_PRIMARY_EMBED_POLICY` is the validation-branch primary for the
`conversation_stream` live writer (`LiveBuffer._flush`): bounded 5s/0 single shot,
deferred 10s/2 repair off the writer. Production is undeployed — see §10.

Resolution rule (`call_embedding`): an explicit `timeout`/`retries` beats the policy;
with neither, **realtime** is used — so an un-migrated caller keeps today's behaviour
instead of silently gaining a longer timeout inside the 8s path.

### `blocks_user_turn` is measured, not inferred from a name

`conversation_stream` embedding is off the user-turn path. Three independent lines:

1. **Static call graph** — all three `self._flush(` call sites sit inside `_run()`;
   `_run` is the target of `threading.Thread(target=self._run, name="v3-live-writer",
   daemon=True)`. No `_q.join()` exists anywhere in the module, so nothing on the
   caller's path waits.
2. **Injected latency** — provider stubbed to 4000 ms: `enqueue` returned in **37 ms**;
   the embedding ran on `v3-live-writer` (caller = `MainThread`); the row was persisted
   **4.04 s after** `enqueue` returned; 8 enqueues took 37 ms total.
3. Harness `eval/embedfix_critical_path.py`, **16/16 passed**, verdict
   `CONVERSATION_STREAM_EMBEDDING_IS_OFF_THREAD`.

Consequence: the 8s user budget is **not** a constraint on this embedding timeout, and
`10s/2` cannot invade it. It does **not** follow that a background item may take
arbitrarily long — see §6.

---

## 5. Caller matrix (all 33 real call sites, classified)

`blocks_user_turn` decided by enclosing function and calling context, not by policy name.

| file:line | enclosing function | budget | blocks user turn |
|---|---|---|---|
| `__init__.py:1862` | `V3Core.search_cards` | realtime 3s/0 | **yes** |
| `__init__.py:2169` | `V3Core.prefetch` | realtime 3s/0 | **yes** |
| `__init__.py:2270` | `V3Core._hit_dict` | realtime 3s/0 | **yes** |
| `__init__.py:2385` | `V3Core._rollback_dedupe_state` | realtime 3s/0 | **yes** (see note) |
| `__init__.py:2651` | `V3Core._rollback_dedupe_state` | realtime 3s/0 | **yes** (see note) |
| `topic_recall.py:467` | `TopicRecall.match` | realtime 3s/0 | **yes** |
| `topic_recall.py:664` | `TopicRecall.chain_recall` | realtime 3s/0 | **yes** |
| `observer.py:1463` | `_PoolPgConnection._recall_candidates` | inline 2.5s/0 | **yes** |
| `recall_pool.py:3146` | `_read_qa_rows_for_term` | inline 2.5s/0 | **yes** |
| `ingest.py:767` | `LiveBuffer._flush` | stream_primary 5s/0, deferred 10s/2 | **no — proven** |
| `ingest.py:981` | `LiveBuffer.ingest_session` | batch 10s/2 | no |
| `__init__.py:1806` | `V3Core._sync_card_to_topics` | durable_write 10s/2 | no |
| `card_store.py:209` | `DeepStore.write_card` | durable_write 10s/2 | no |
| `card_store.py:441` | `DeepStore.write_card_strict` | durable_write 10s/2 | no |
| `session_summary.py:281` | `summarize_j_file` | durable_write 10s/2 | no |
| `session_summary.py:359` | `summarize_j_file` | durable_write 10s/2 | no |
| `session_summary.py:513` | `_write_summary_to_stores` | durable_write 10s/2 | no |
| `topic_store.py:233` | `TopicStore.upsert_topic` | durable_write 10s/2 | no |
| `yin_pool.py:123` | `ingest_yin` | durable_write 10s/2 | no |
| `tools/topic_create.py:89` | `handle_v2_topic_create` | durable_write 10s/2 | no |
| `tools/topic_edit.py:123` | `handle_v2_topic_edit` | durable_write 10s/2 | no |
| `embed_topics.py:62` | `main` | batch 10s/2 | no |
| `topic_cluster.py:209` | `compute_embedding` | batch 10s/2 | no |
| `topic_maintain.py:112` | `_try_seed_from_buffer` | batch 10s/2 | no |
| `topic_maintain.py:260` | `cluster_orphans` | batch 10s/2 | no |
| `topic_refine.py:736` | `TopicMatcher._sync_card_to_db` | batch 10s/2 | no |
| `tools/import_.py:517` | `_write_candidates` | batch 10s/2 | no |
| `tools/import_.py:994` | `handle_v3_import_seed` | batch 10s/2 | no |
| `tools/health.py:412` | `_PrefetchPgView._stage_conn` | health 5s/1 | diagnostic |

**The only user-blocking sites are realtime 3s/0 or tighter.** No `10s/2` policy appears
on any path that a user turn waits for. That is the answer to "does the durable policy
enter the user-blocking path" — **no**, and it is answered by the table above, not by the
policy's name.

Note on `_rollback_dedupe_state`: it embeds a query and sits on the prefetch-side state
machine. Classified `yes` because it can run while a prefetch is in flight; its budget is
the safe realtime default either way.

### ⚠️ A third, un-governed budget family exists

Not every embedding goes through `call_embedding`'s policy system:

| path | function | budget | governed by a named policy? |
|---|---|---|---|
| QA pair embedding | `V3Core._flush_pending_qa` → `embed_batch` | **hardcoded 60s**, `retries=3` | **no** |
| 印 note embedding | `observer._backfill_note_embedding` → `embed_batch` | **hardcoded 60s**, `retries=2` | **no** |

`embed_batch` hardcodes `timeout=60` at its `requests.post` and takes `retries` as a plain
argument. So the two highest-value derived artifacts (QA pairs, observation notes) are
**outside** the policy matrix entirely.

This is the **safe direction** — 60s/3 is far more generous than 10s/2, which is why
`qa_pairs` shows 0 NULLs in the last 24h — but it means the matrix above is not the whole
story. Recorded as a finding: `EMBED_BATCH_BUDGET_UNPOLICED`. Not changed by this hotfix
(changing it would alter the budget of the healthiest path for no measured benefit).

---

## 6. Why the primary attempt is not `10s/2` inline

The live writer is **single-threaded and serial**, and its queue is `queue.Queue()` —
**unbounded, no backpressure**.

| policy | worst case / item | drain ceiling | vs 13/min peak arrival |
|---|---|---|---|
| 3s / 0 (current) | 3 s | ~20/min | headroom, but loses the vector |
| 5s / 0 (validation primary) | 5 s | ~12/min | marginal at peak |
| 10s / 0 | 10 s | ~6/min | backlog grows at peak |
| **10s / 2 inline** | **10×3 + 1 + 2 = 33 s** | **~1.8/min** | backlog grows ~11/min |

Arrival: peak **13 rows/min**, daily mean 1.07–1.42 rows/min. Healthy provider
(p50 ≈ 0.58 s, observed tail 3.488 s): ~103/min — ample.

`10s/2` inline would convert a provider outage into head-of-line blocking → backlog →
unbounded memory growth. Hence the split:

* **A. primary attempt (live writer): bounded single shot, `5s / 0`**
  (`STREAM_PRIMARY_EMBED_POLICY`, validation branch). No inline retry chain.
  Covers the 3.488 s tail without serious HOL blocking. Selected by the outage
  simulation (`evidence/outage-simulation-20260920.txt` verdict: 5s/0 pick);
  realtime recall stays 3s/0. Validation only — production undeployed (§10).
* **B. deferred durable retry (repair path): `10s / 2`.** Source row retained, embedding
  NULL, ledger row written, retryable, **writer moves on immediately**. The patient retry
  never occupies the live writer. Live ingestion throughput is thereby decoupled from
  eventual embedding completion.

### Registered, deliberately not fixed

`UNBOUNDED_LIVE_EMBED_QUEUE` — `queue.Queue()` has no ceiling. A real reliability risk,
but durable marker/recovery/ordering semantics are sensitive, so this hotfix does not
change queue bounds, drop enqueues, add workers, or alter ordering. Mitigated by the
bounded primary attempt + no inline long retries + the failure ledger + deferred repair +
backlog monitoring. Escalate only if simulation shows unacceptable backlog anyway.

---

## 7. Failure ledger

`public.embedding_failures`, additive-only DDL in `schema/embedding_failures.sql`.
Full DDL, lock levels, compatibility in both deploy orders, and downgrade behaviour are in
the deploy plan (§2 there).

Identity and lifecycle:

| property | mechanism |
|---|---|
| identity key | `UNIQUE (entity_table, entity_id, phase)` |
| repeat failure | `ON CONFLICT ... attempts = existing + EXCLUDED.attempts`, `last_failed_at = now()` — bumps, never piles up |
| success after failure | `resolved_at` + `resolution` set; row **kept**, not deleted |
| unresolved backlog | `resolved_at IS NULL` — a resolved failure stops counting |
| state transition | `FAILED_RETRYABLE` → `SUCCESS/RESOLVED` |
| `first_failed_at` | preserved across bumps |

`record_embedding_failure` **never raises** — it catches everything, rolls back, logs an
ERROR loudly and returns `False`, because failure accounting must not be able to take down
the write path it protects, and an unrecorded failure is exactly the silent hole this
module exists to close.

No secrets stored: `error_fingerprint` is a short hash of the error text, never the text.

---

## 8. Canonical embedding input per table (repair semantics)

The repair must reproduce **the representation the live path actually uses**, or
historical and new embeddings end up in two incompatible semantic spaces.

| table | canonical input | verified at |
|---|---|---|
| `conversation_stream` | `content[:2000]` | `ingest.py:768`, `:982` (both sites agree) |
| `observation_notes` | **full** `content`, no slice | `observer.py:1696` |
| `yin_paragraphs` (yin_pool writer) | `f"{section}. {content[:1500]}"` | `yin_pool.py:124` |
| `yin_paragraphs` (E1 writer) | `content[:2000]` | `e1.py:1027` |
| `qa_pairs` | chunked long-QA pipeline | `embed_chunks.build_qa_embedding_representation` |
| `topics` | 0 NULLs → not a repair target | — |

### ⚠️ `yin_paragraphs` has two writers with two representations

| discriminator | writer | rows | NULLs |
|---|---|---|---|
| `yin_version LIKE 'e1_seg_%'` / `section LIKE 'E1/%'` | `e1.py` | 620 | 0 |
| everything else | `yin_pool.py` | 589 | **8** |

All 8 NULL rows are the yin_pool kind → `f"{section}. {content[:1500]}"`.

**Defect found by this dry-run:** the backfill tool's `_row_text` returned bare `content`
for `yin_paragraphs` — neither representation. Repairing with it as-is would have written
vectors no live path ever produces. Fixed, with a regression test.

### Long-QA plan (`qa_pairs`)

Computed offline by `eval/embedfix_longqa_plan.py` from the canonical builder — real
numbers, not "12 API calls":

| metric | value |
|---|---|
| parent QA count | **12** |
| estimated child chunks | **28** |
| representation version | `v0.2.0-long-qa` |
| chunks per parent | 2,2,2,2,2,3,3,2,2,2,3,3 |
| stale sidecar chunks for these parents | **0** |

Parents: `247449, 247710, 247728, 247733, 247945, 247947, 247948, 247951, 247952,
247953, 247954, 247955`. Splits at the 7680-token boundary, e.g. `247947` →
`question[0:17607]`, `answer[0:18449]`, `answer[18449:23234]`.

A simple parent `UPDATE` is **forbidden**: the repair writes the parent vector *and*
replaces the `qa_embedding_chunks` sidecar for that parent (DELETE + INSERT scoped to the
parent, so a shrinking chunk count cannot leave stale higher-index children). Recall must
be verified end-to-end: child match → parent QA.

---

## 9. Evidence index

| artifact | path |
|---|---|
| critical-path instrumentation (16/16, `..._IS_OFF_THREAD`) | `eval/embedfix_critical_path.py` |
| long-QA canonical plan | `eval/embedfix_longqa_plan.py` → `evidence/long-qa-plan.json` |
| outage simulation | `eval/embedfix_outage_simulation.py` |
| disposable E2E (schema derived from production DDL) | `eval/embedfix_e2e.py` |
| backfill E2E | `eval/embedfix_backfill_e2e.py` |
| baseline full-suite differential | `evidence/baseline-full-suite.txt` |
| contamination incident + quarantine | `evidence/contamination-20260920/` |
| deploy / rollback / stop conditions / monitoring | `docs/embedding-reliability-deploy-plan.md` |

## 10. Round status — accurate statement

This round is **not** "production 0 writes". The accurate statement:

> During the investigation phase there was **one disclosed test-harness filesystem
> contamination of the production outbox (9 marker files, 0 DB rows)**, remediated by
> exact-file cleanup with quarantine and hash verification. Apart from that, this round
> performed **no code deployment, no configuration change, no DB write, and no restart**
> of production.
