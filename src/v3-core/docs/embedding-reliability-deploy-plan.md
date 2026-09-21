# Embedding Reliability — Migration / Deploy / Rollback Plan

**Status: VALIDATED CANDIDATE — DECISION_REQUIRED; PLAN ONLY — NOT EXECUTED.**
Nothing in this document has been applied to production. Production is read-only
pending explicit approval.

Production baseline: P0 HEAD `7808b89239787ba09eee7915c69392981a8ca911`
Embedding source: `b344b801bac06e3dad0f68f47d243930d57ab07f`
Integration branch: `integration/p0-embedding-reliability` (validated source HEAD `4a7f496170c23fc0ee57be8286e65c31ade51546`)
Common baseline (merge base): tag `v0.2.1` = `0bd9e1ee7f42f0184f83aff367000a6656a06dcd`

Historical note: this plan was first written against tag `v0.2.1` on branch
`fix/embedding-write-reliability`. Those identifiers below are historical
context, not the current integration candidate.

---

## 0. What is actually broken

`conversation_stream` (and `yin_paragraphs`, `observation_notes`) embedding used the
**realtime** default `timeout=3, retries=0`. A transient provider timeout therefore
became a **permanent** NULL with no record that a request had ever been attempted.
From the outside, "never attempted", "lost to a 3s timeout", and "misconfigured key"
were indistinguishable.

Measured production leak (`conversation_stream`), fixed windows taken before any change:

| window | new rows | new NULLs | NULL rate | marker coverage |
|---|---|---|---|---|
| 1h | 100 | 8 | 8.00% | 0% |
| 6h | 730 | 85 | 11.64% | 0% |
| 24h | 2289 | 196 | 8.56% | 0% |

Other tables, last 24h: `yin_paragraphs` 7/36 = **19.4%**, `observation_notes` 2/25 = 8.0%,
`qa_pairs` 0/161 = 0%.

Two distinct causes, separated by evidence:

| regime | window | NULLs | avg len | >8k rows | attribution |
|---|---|---|---|---|---|
| API-key outage | 09-13 08 → 09-14 16 | 725 | 686 | 15 | `API_KEY_EMPTY_CONFIGURATION` (100% failure every hour in window) |
| intermittent + active leak | 09-15 → 09-20 | 276 | 289 | **0** | `EMBEDDING_TIMEOUT_EXHAUSTED` |
| before | — | 1 | 98 | 0 | `UNKNOWN` |

**Long content is excluded as the cause of the active leak**: 0 of the 262 active-leak
rows exceed 8k chars, average length 289 chars. `source_empty` = **0** — every NULL is a
genuine loss, none is a legitimately-empty source.

Marker coverage for all historical NULLs is **0%** because `public.embedding_failures`
does not exist in production. Historical classification is therefore **attribution by
evidence, not recorded state** — say so whenever these numbers are quoted.

---

## 1. Success definition (do not get this wrong)

```
Primary invariant :  new UNEXPLAINED NULL = 0
NOT required      :  new embedding failure = 0
```

A temporary, *explained* NULL is acceptable: the source row is saved, the failure is
recorded with entity identity, failure class, retryable status, attempts, provider/model
fingerprint and timestamp, and a deferred pass fills the vector later.

A silent permanent NULL is not.

Deploy is successful when the unexplained-NULL rate goes to 0 — **not** when the raw
failure rate goes to 0. A provider outage during which every failure is correctly
recorded is a *passing* state.

---

## 2. Failure ledger — exact DDL

File: `src/v3-core/schema/embedding_failures.sql`. **Additive only.** No `ALTER`, no
`DROP`, no touch of any existing table or column.

```sql
CREATE TABLE IF NOT EXISTS public.embedding_failures (
    id                 BIGSERIAL PRIMARY KEY,
    entity_table       TEXT        NOT NULL,
    entity_id          TEXT        NOT NULL,
    phase              TEXT        NOT NULL,
    error_class        TEXT        NOT NULL,
    retryable          BOOLEAN     NOT NULL,
    provider_status    INTEGER,
    attempts           INTEGER     NOT NULL DEFAULT 0,
    timeout_policy     TEXT        NOT NULL,
    timeout_seconds    DOUBLE PRECISION,
    max_retries        INTEGER,
    elapsed_ms         DOUBLE PRECISION,
    model              TEXT,
    model_fingerprint  TEXT,
    error_fingerprint  TEXT,
    resolved_at        TIMESTAMPTZ,
    resolution         TEXT,
    first_failed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_failed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS embedding_failures_entity_phase_key
    ON public.embedding_failures (entity_table, entity_id, phase);

CREATE INDEX IF NOT EXISTS embedding_failures_unresolved_idx
    ON public.embedding_failures (entity_table, retryable)
    WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS embedding_failures_class_idx
    ON public.embedding_failures (error_class, last_failed_at DESC);
```

**No secrets are stored.** `error_fingerprint` is a short hash of the error text, never
the text. `model_fingerprint` is the same non-secret profile hash already used by the
vector tables.

### Idempotency contract

| property | mechanism |
|---|---|
| identity key | `UNIQUE (entity_table, entity_id, phase)` |
| repeat failure | `ON CONFLICT ... DO UPDATE SET attempts = embedding_failures.attempts + EXCLUDED.attempts, last_failed_at = now()` — bumps, never piles up rows |
| success after failure | `resolve_embedding_failure` sets `resolved_at` + `resolution`; the row is **kept**, not deleted, so history survives |
| unresolved backlog | `resolved_at IS NULL` — a resolved failure stops counting against backlog |
| state transition | `FAILED_RETRYABLE` → `SUCCESS/RESOLVED` |
| `first_failed_at` | preserved across bumps (only `last_failed_at`/`updated_at` move) |

### Lock level and execution time

| statement | lock | scope |
|---|---|---|
| `CREATE TABLE IF NOT EXISTS` | none on existing objects | new relation only |
| `CREATE UNIQUE INDEX IF NOT EXISTS` | on the new (empty) table only | ~instant |
| `CREATE INDEX IF NOT EXISTS` ×2 | on the new (empty) table only | ~instant |

No existing table is locked or rewritten. Estimated execution: **< 100 ms** on an empty
database; unchanged at any data volume, because nothing existing is touched.

### Compatibility — both directions, no fragile window

| order | behaviour | verdict |
|---|---|---|
| **migration first, then code** | old runtime never references `embedding_failures` → completely unaffected | **SAFE — recommended** |
| **code first, then migration** | `record_embedding_failure` **never raises**: it catches every exception, rolls back, logs an ERROR loudly and returns `False`. The source row is still written; only the marker is lost for that window. No crash, no data loss. | **SAFE — degraded marker coverage only** |

Verified in source: `embed_failures.record_embedding_failure` wraps the UPSERT in
`try/except Exception` with `conn.rollback()` and returns `False`.
`embed_for_write` returns an `EmbedOutcome` and does not raise on provider failure.

**Conclusion: there is no millisecond-level simultaneous-deploy requirement.**
Recommended order is migration-first purely because it gives 100% marker coverage from the
first new failure.

### Downgrade

`DROP TABLE public.embedding_failures;` — additive-only means the old runtime has no
reference to it and keeps working after the drop. Losing the ledger loses *accounting*,
not data; the embeddings themselves live in their own tables and are untouched.
**Do not drop while a repair pass is mid-run.**

---

## 3. Policy matrix (frozen)

| class | timeout / retries | blocks user turn | on exhaustion |
|---|---|---|---|
| realtime recall | 3s / 0 | **yes** | graceful recall degradation |
| **conversation_stream source write embed** | **primary 5s / 0** (`STREAM_PRIMARY_EMBED_POLICY`, validation branch), then deferred | **no — proven** | source saved + retryable marker; deferred repair uses 10s/2 |
| other background durable embed | 10s / 2 | no | marker + backlog |
| batch / import | 10s / 2 | no | checkpoint + marker |
| health | 5s / 1 | diagnostic | explicit degraded |

### `blocks_user_turn = false` — the evidence

`conversation_stream` embedding is **not** on the synchronous user-turn path. This was
established by instrumentation, not by reading a name:

1. **Static call graph** — all three `self._flush(` call sites are inside `_run()`;
   `_run` is the target of `threading.Thread(target=self._run, name="v3-live-writer",
   daemon=True)`. There is no `_q.join()` anywhere in the module, so nothing on the
   caller's path waits.
2. **Injected latency** — with the provider stubbed to take **4000 ms**:
   `enqueue` returned in **37 ms**; the embedding ran on thread `v3-live-writer`
   (caller = `MainThread`); the row was persisted **4.04 s after** `enqueue` returned;
   8 enqueues took 37 ms total.
3. Harness: `eval/embedfix_critical_path.py`, 16/16 passed,
   verdict `CONVERSATION_STREAM_EMBEDDING_IS_OFF_THREAD`.

Therefore the user-facing 8s budget is **not** a direct constraint on this embedding
timeout. It does **not** follow that a background item may take arbitrarily long — see §4.

---

## 4. Why the primary attempt is NOT `10s / 2` inline

The writer is **single-threaded and serial**, and its queue is `queue.Queue()` —
**unbounded, no backpressure**.

Worst-case cost of one item, and the resulting drain ceiling:

| policy | worst case / item | drain ceiling | vs 13/min peak arrival |
|---|---|---|---|
| 3s / 0 (current) | 3 s | ~20/min | headroom, but **loses the vector** |
| 5s / 0 (validation primary) | 5 s | ~12/min | marginal at peak |
| 10s / 0 | 10 s | ~6/min | backlog grows at peak |
| **10s / 2 inline** | **10×3 + 1 + 2 = 33 s** | **~1.8/min** | backlog grows ~11/min |

Production arrival: peak **13 rows/min**, daily mean 1.07–1.42 rows/min.
Healthy provider (p50 ≈ 0.58 s): ~103/min — ample.

Putting `10s/2` inline would turn a provider outage into head-of-line blocking →
backlog → unbounded memory growth. So the policy is split:

**A. Primary attempt (live writer): bounded single-shot `5s / 0`**
(`STREAM_PRIMARY_EMBED_POLICY`, validation branch).
No inline long retry chain. Covers the observed tail (3.488 s) while minimising HOL
blocking. Selected by the outage simulation
(`evidence/outage-simulation-20260920.txt` verdict: 5s/0 pick, comparing 3/0, 5/0,
10/0, 10/2). Realtime recall stays 3s/0. Validation only — this plan is NOT
executed, production undeployed.

**B. Deferred durable retry (repair/backfill path): `10s / 2`.**
On primary failure: source row retained, embedding NULL, failure ledger row written,
retryable state recorded, **writer moves to the next item immediately**. The patient
retry does not occupy the live writer. This decouples live ingestion throughput from
eventual embedding completion.

### Registered, deliberately not fixed here

`UNBOUNDED_LIVE_EMBED_QUEUE` — `queue.Queue()` has no ceiling. Real reliability risk,
but durable marker/recovery/ordering semantics are sensitive, so this hotfix does **not**
change queue bounds, drop enqueues, add workers, or alter message order. Mitigated for
now by the bounded primary attempt + no inline long retries + the failure ledger +
deferred repair + backlog monitoring. Escalate to a blocker only if the simulation shows
unacceptable backlog even with those measures. Queue capacity / concurrency model is a
separate follow-up reliability task.

---

## 5. Deployment stages

**Stage 1 — DB additive migration.**
Apply `schema/embedding_failures.sql`. Verify: table exists, 3 indexes exist, 0 rows.
No existing object touched. Old runtime keeps running unaffected.

**Stage 2 — code deployment.**
Named policies + `embed_for_write` + explicit failure accounting + bounded primary
attempt + caller migration (17 files). Deploy artifact must be identified by wheel
SHA256, not by "the new code".

**Stage 3 — restart.**
Restart only the components that load the changed modules. Verify the live loaded
content fingerprint matches the deployed wheel.

**Stage 4 — forward smoke (before any historical repair).**
1. Produce/wait for a normal write: source saved, embedding present, `embed_model`
   fingerprint correct.
2. Controlled timeout test: source saved, failure ledger row written, **no unexplained
   NULL**.
3. Re-take the §0 fixed-window snapshot and compare: the invariant is
   `unexplained NULL rate → 0`, not `failure rate → 0`.

**Stage 5 — historical repair, smallest batch first.**
Start with **5–10 rows**, verify embedding / marker resolution / recall / source hash
unchanged, then widen. **Do not scan all 973+ on the first run.**

Order is deliberate: forward fix → ledger/observability → deploy+smoke → confirm no new
unexplained NULL → small historical repair → recall verification → widen.
**Not** "973 → 0 first, then worry about the leak."

---

## 6. Stop conditions (automatic halt)

Halt the run immediately on any of:

* provider error rate above threshold
* repeated 429
* model fingerprint mismatch against the expected `bf32771ecbd1`
* unexpected schema (a column or table that is not what the plan expects)
* source hash mismatch (the canonical input changed under us)
* latency runaway (drain ceiling below arrival rate for a sustained period)
* failure-ledger write failure
* canonical pipeline invariant failure (e.g. child vector count ≠ chunk count)

"900 rows remain, so keep going" is not an acceptable reason to continue past a stop
condition.

---

## 7. Repair semantics — canonical input per table (do not invent)

The repair must reproduce **the representation the live path actually uses**, otherwise
historical and new embeddings end up in two incompatible semantic spaces.

| table | canonical embedding input | verified at |
|---|---|---|
| `conversation_stream` | `content[:2000]` | `ingest.py:768`, `ingest.py:982` (both sites agree) |
| `observation_notes` | **full** `content`, no slice, via `embed_batch(..., retries=2)` | `observer.py:1696` |
| `yin_paragraphs` (yin_pool writer) | `f"{section}. {content[:1500]}"` | `yin_pool.py:124` |
| `yin_paragraphs` (E1 writer) | `("## " + title + "\n\n" + body)[:2000]` | `e1.py:1027` |
| `qa_pairs` | chunked long-QA pipeline (`build_qa_embedding_representation`) | `embed_chunks.py` |
| `topics` | 0 NULLs → **not a repair target** | — |

### ⚠️ `yin_paragraphs` has TWO incompatible representations

The same table is written by two different code paths with different embedding text.
Rows are distinguishable by writer:

| discriminator | writer | rows | NULLs |
|---|---|---|---|
| `yin_version LIKE 'e1_seg_%'` (and `section LIKE 'E1/%'`) | `e1.py` | 620 | **0** |
| everything else | `yin_pool.py` | 589 | **8** |

All 8 NULL rows are the **yin_pool** kind, so they need
`f"{section}. {content[:1500]}"`.

**Historical finding, now repaired in source (not yet applied to production):**
during the original dry-run, the backfill tool's `_row_text` returned bare
`content` for `yin_paragraphs`, which was **neither** representation — it dropped
the `"{section}. "` prefix. Repairing with the tool as-it-was would have written
embeddings with different semantics than the live path. That defect was fixed by
embedding source commit `b344b801...` (yin dual-writer canonical repair) and is
retained in validated integration source HEAD `4a7f496...`; it is covered by
`tests/test_backfill_canonical_input.py` and the integration evidence. Production still
runs the old code, so no production repair may run until the fixed code is deployed.
(The `observation_notes` and `conversation_stream` entries were correct as written.)

---

## 8. Long-QA repair plan (`qa_pairs`)

12 NULL parents, all long. The plan is computed offline from the canonical builder
(`eval/embedfix_longqa_plan.py`), not guessed:

| metric | value |
|---|---|
| parent QA count | **12** |
| estimated child chunks | **28** |
| representation version | `v0.2.0-long-qa` |
| chunks per parent | 2,2,2,2,2,3,3,2,2,2,3,3 |
| existing sidecar chunks for these parents | **0** (clean, no stale children) |

Parent IDs: `247449, 247710, 247728, 247733, 247945, 247947, 247948, 247951, 247952,
247953, 247954, 247955`.

Chunking splits at the 7680-token boundary, e.g. `247947` → `question[0:17607]`,
`answer[0:18449]`, `answer[18449:23234]`.

Each child carries `source_field`, `source_start`, `source_end`, `token_count`,
`source_sha256`, `representation_version`; the parent vector is
`aggregate_parent_embedding(child_vecs)` and is only computed once **every** child
succeeded (a parent built from a partial child set would be a meaningless vector).

**A simple parent `UPDATE` is forbidden.** The repair writes the parent vector *and*
replaces the `qa_embedding_chunks` sidecar for that parent (DELETE + INSERT, scoped to
the parent, so a shrinking chunk count cannot leave stale higher-index children).

Recall verification must be end-to-end: **child match → parent QA**, actually queried.

---

## 9. Recall-after-repair verification

Do not accept `embedding IS NOT NULL` as proof. After repair, run real queries and
confirm the repaired rows are actually reachable. Pre-selected samples (one per cause
class):

| sample class | row | why |
|---|---|---|
| recent timeout row | a `conversation_stream` row from the 09-19/09-20 active leak | the live, escalating failure |
| historical config-failure row | a `conversation_stream` row from 09-13/09-14 | proves the API-key class is repairable too |
| long-QA row | one of the 12 `qa_pairs` parents | exercises child→parent provenance |
| yin paragraph | one of the 8 yin_pool rows | exercises the two-representation hazard |
| observation note | one of the 3 note rows | full-content representation |

For each: query by its own distinctive content, confirm the repaired row is returned, and
confirm the parent/child link resolves. Record the exact query and the observed hit.

---

## 10. Monitoring

`conversation_stream` has **no `created_at` column** (verified: 0 matching columns), so
ingest lag cannot be derived from it. **Do not invent a lag number.** Use signals that
actually exist:

| signal | source |
|---|---|
| in-memory queue depth | `LiveBuffer._q.qsize()` |
| pending marker count | file count of `j/pending_live_buffer` |
| oldest pending marker age | min mtime in `j/pending_live_buffer` |
| unresolved failure count | `embedding_failures WHERE resolved_at IS NULL` |
| writer throughput | rows/min persisted vs arrival rate |
| retry recovery rate | resolved / (resolved + unresolved) |

Unexplained-NULL query (the primary metric):

```sql
SELECT count(*) AS unexplained_nulls
  FROM public.<table> t
  LEFT JOIN public.embedding_failures f
         ON f.entity_table = '<table>' AND f.entity_id = t.id::text
 WHERE t.embedding IS NULL AND f.id IS NULL;
```

Health/diagnostic output must report at least: current pending count, oldest pending age,
recent embedding failure rate.

---

## 11. Rollback

| stage | rollback |
|---|---|
| Stage 1 (migration) | `DROP TABLE public.embedding_failures;` — additive, old runtime unaffected |
| Stage 2/3 (code) | redeploy the previous wheel by SHA256 and restart; no data migration to undo |
| Stage 5 (repair) | repairs are per-row and idempotent; a repaired vector is a valid vector. To revert a specific row, set its `embedding = NULL` — the row itself is never mutated (source hash verified unchanged) |

Source text is never modified by any stage: repairs write only `embedding`,
`embed_model`, and the sidecar. Every repair verifies the source hash before and after.

---

## 12. Evidence

| artifact | path |
|---|---|
| critical-path instrumentation (16/16) | `eval/embedfix_critical_path.py` |
| long-QA canonical plan | `eval/embedfix_longqa_plan.py` → `evidence/long-qa-plan.json` |
| outage simulation | `eval/embedfix_outage_simulation.py` |
| disposable E2E (schema from production DDL) | `eval/embedfix_e2e.py` |
| backfill E2E | `eval/embedfix_backfill_e2e.py` |
| baseline full-suite differential | `evidence/baseline-full-suite.txt` |
| contamination incident | `evidence/contamination-20260920/INCIDENT.md` |
