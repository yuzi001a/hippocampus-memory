# Long Observation Derived Index v1

Status: **implemented integration candidate; evidence gates below are required before any production decision**

> 向量模型的窗口限制，只能约束派生索引，不能反过来决定哪些历史值得被保存。
>
> Embedding window limits constrain derived indexes, not the amount of source history we preserve.

## 1. Problem and root cause

`public.observation_notes.content` is the source of record. The current observer writes the source row first, then calls `_backfill_note_embedding()` with the complete `content`. The embedding client sends that complete string in one provider request. The production BAAI/bge-m3 configuration has `max_input_tokens=8192` and the safe target is `8192-512=7680`. The six current production NULL rows contain 8,514–11,631 provider-input tokenizer tokens (the census counter includes the tokenizer's two special boundary tokens); the reusable planner reports the corresponding content-token counts as 8,512–11,629. Both measurements are above the safe target and provider hard window, so the long path is deterministically outside the provider window; their source is present but the single derived vector cannot be produced.

This is a **PRODUCT_DEFECT / P1-HIGH / OBSERVATION_LONG_INPUT_UNEMBEDDABLE**. It is not fixed by dropping, truncating, or rewriting source content.

## 2. Invariants

1. `observation_notes.content` is immutable source truth for this index. No writer, repair operator, or migration may replace it with a prefix.
2. Chunk rows are derived state. They may be deleted and rebuilt from the source row.
3. A long parent vector exists only after every planned child has a valid vector.
4. A failed child leaves the source durable, the parent vector absent for a new build, and a durable failure marker.
5. A sidecar rebuild is scoped to one `(observation_id, observation_version)` and commits atomically.
6. Every child records the complete source SHA-256, exact character offsets, exact span hash, token count, representation version, model fingerprint, and verbatim span content.
7. Recall returns the complete parent `observation_notes.content`, never a chunk fragment.
8. A parent appears once even if multiple child vectors and/or the parent vector are candidates.
9. Short observations preserve their existing one-request/full-content representation and do not create sidecar children.
10. The production database is not migrated or written by this candidate task.

## 3. Representation contract

Representation version: **`v1-long-observation`**.

### Short path

If the exact existing canonical observation text (`content`) is within the safe token target:

```text
content -> one existing parent vector -> no observation child rows
```

The request bytes, model fingerprint, vector dimension, parent table, and recall return object remain unchanged. A scoped cleanup removes any stale children if a row previously had a long representation but is now short.

### Long path

If `content` exceeds the safe target:

```text
full immutable content
  -> tokenizer-aware, non-overlapping, coverage-complete chunks
  -> one provider call per chunk
  -> L2-normalized mean of all child vectors
  -> parent observation_notes.embedding + observation_embedding_chunks rows
```

No `content[:N]` substitution is used. The only slices are derived child spans, each of which is persisted with offsets and hashes.

## 4. Sidecar schema

The additive table is `public.observation_embedding_chunks`:

- natural parent identity: `(observation_id, observation_version)`;
- parent FK: `observation_id -> observation_notes(id) ON DELETE CASCADE`;
- uniqueness: `(observation_id, observation_version, chunk_index)`;
- `source_sha256`: hash of the complete parent source content, repeated on each child;
- `chunk_sha256`: hash of the exact child `content` span;
- offsets are Python character offsets into the UTF-8 decoded source string, with half-open `[source_start, source_end)` semantics;
- `content` is the exact source slice, not a summary or prefix;
- `updated_at` supports idempotent replacement auditing;
- vector index is additive and scoped to the sidecar.

The scoped rebuild algorithm is:

```text
plan and embed every current child outside the DB transaction
BEGIN
  DELETE FROM observation_embedding_chunks
    WHERE observation_id = ? AND observation_version = ?
  INSERT the complete current child set
  UPDATE observation_notes parent vector + fingerprint
  resolve all unresolved failure phases for this entity
COMMIT
```

Any exception rolls back the derived mutation. The already-committed source row is not rolled back.

## 5. Planner

The candidate extracts the existing tokenizer/offset walk in `embed_chunks.py` into a generic text-span seam while retaining the long-QA API. The generic planner:

- tokenizes each complete source once;
- uses configured model tokenizer/offsets, never a guessed character split in production;
- emits contiguous non-overlapping spans;
- preserves leading/trailing/inter-token Unicode whitespace;
- verifies first offset `0`, last offset `len(source)`, contiguous offsets, concatenated text equality, and source hash;
- keeps every child at or below `safe_token_target()`;
- treats empty source as no embedding request, not as an invented vector;
- is linear in token/source size and deterministic for a fixed tokenizer/config/version.

The production observation planner uses `source_field='content'`. The QA planner retains its existing `question` then `answer` ordering and `v0.2.0-long-qa` representation version.

## 6. Aggregation and failure semantics

`aggregate_parent_embedding()` remains the single aggregation implementation: component-wise mean followed by L2 normalization. It rejects empty vectors, zero vectors, mixed dimensions, and zero-norm means.

For a long observation with `N` children:

- `N/N` valid vectors: parent may be written in the derived transaction;
- fewer than `N`: parent is not written; current derived replacement is not committed; failure accounting identifies the child phase;
- tokenizer failure: `observation_long_tokenizer` marker;
- provider timeout/connection/429/retryable 5xx: child phase marker with provider classification;
- deterministic bad request/config: non-retryable marker;
- sidecar transaction failure: `observation_long_sidecar` marker;
- unexpected failure: classified as unknown/retryable and retained as a marker.

Failure markers store classification, retryability, provider status, model/fingerprint and a safe error fingerprint only. They never store source text, API keys, or full provider responses. Successful rebuild uses the existing entity-level D5 resolution semantics so stale `live_ingest`, `j_import`, and `backfill` phases are all resolved for that observation entity.

## 7. Writer integration

The existing source-first order is retained:

1. insert and commit the observation source row;
2. decide short vs long from the complete content;
3. perform provider work outside the derived mutation transaction;
4. atomically replace sidecar children and parent vector for the long path, or update the existing parent vector for the short path;
5. leave the source untouched on any derived failure.

The observer remains a background derived-index operation. It must not make source durability depend on provider availability.

## 8. Recall

The observation vector lane becomes a parent/child union:

- parent candidates: `observation_notes.embedding`;
- child candidates: `observation_embedding_chunks.embedding` joined to the parent;
- child rows are eligible only when their `source_sha256` matches the current parent content, their representation version is current, and the parent has a non-NULL vector;
- both lanes use the same query vector and current note threshold;
- candidates are grouped by parent `(observation_id, version)` before creating a `RecallHit`;
- final cosine is `max(parent_cosine, max(child_cosine))`;
- within same-day groups, the legacy parent-only path keeps newest-note semantics; when a child contributes, max-score selection protects a distinctive tail hit from being discarded by a newer weak note;
- `RecallHit.content` is always the full parent content;
- one parent is returned once even when several child vectors and the parent vector are candidates.

If the sidecar table is absent, the new query path treats it as optional and falls back to the parent-only path. A newly migrated table is used automatically. This keeps the old runtime startable before migration and makes migration-first deployment safe.

## 9. Historical repair

The backfill operator will use the same representation contract as the writer:

- short row: existing full-content parent embedding;
- long row: generic observation planner, one child request per planned span, all-child gate, scoped sidecar replace, parent aggregation;
- dry-run reports id/version, source hash, token count, long flag, child count/tokens, offsets, estimated provider calls, representation version, current ledger and existing child count;
- `--apply` remains explicit and is not run in this task against production.

A source hash is checked before and after repair. A row with changed source hash is a stop condition.

## 10. Migration, compatibility, rollback

The schema artifact is additive-only and idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`). It does not alter `observation_notes.content` or rewrite existing rows. Migration-first is the recommended release ordering:

1. apply additive schema in a controlled environment;
2. deploy code that can use the table but falls back if it is absent;
3. verify new writes and recall;
4. run historical dry-run;
5. separately authorize production backfill.

Rollback is code rollback first; retain the sidecar table and derived rows for inspection. A legacy runtime ignores the unused table and continues parent-only recall. A candidate runtime with the table absent uses the parent-only/legacy path and records no source mutation.

## 11. Monitoring

Track separately:

- source rows written;
- parent vectors present/NULL;
- child count and current representation version;
- unresolved failure markers by phase/error class;
- rows whose parent source hash has no matching complete child coverage;
- model fingerprint split;
- child provider calls and elapsed time;
- short-path request/recall parity.

A NULL parent with an unresolved marker is explained and repairable. A NULL parent with no marker is still a defect.
