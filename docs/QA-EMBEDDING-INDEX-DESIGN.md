# QA Embedding / Recall Index Design

Status: implementation contract for v0.2 closing round

## 1. Boundary

- **Source truth**: `public.conversation_stream` preserves the complete raw experience: user/assistant/tool messages, tool calls/results, timestamps, session and turn identity. `qa_pairs` also preserves the complete derived user/assistant pairing and tool provenance for that pair.
- **Recall index**: `qa_pairs.embedding`, `topics.embedding`, `topic_entries.embedding`, and the derived `qa_embedding_chunks` sidecar described below. A recall hit always resolves back to the complete parent QA; a chunk is never returned as an independent memory.
- `explicit_memories` remains a separate user-curated store. It is not silently folded into the observer/QA index.
- The current branch does not define or read a `memory_formation_log` table. This round does not invent a new memory layer; if an observer log is added later it must remain provenance/operational metadata, not a replacement for source truth.

## 2. Short QA compatibility

For a QA whose exact tokenizer count for `question + "\\n" + answer` is within the configured provider limit, the existing request is unchanged:

```text
question + "\\n" + answer
```

The parent vector remains in `qa_pairs.embedding`, and the existing QA vector recall path continues to read it. No extra child row is required for a short QA.

## 3. Long QA representation

A long answer is not truncated, rewritten, or silently dropped.

1. Keep the complete source in `conversation_stream` and `qa_pairs`.
2. Use the model tokenizer (configured tokenizer name; for the production BAAI/bge-m3 profile this is `BAAI/bge-m3`) to split the question and answer into non-overlapping token-safe derived spans.
3. Store each derived span in `qa_embedding_chunks`, with `qa_id`, source field (`question`/`answer`), source character offsets, token count, representation version, source hash, text, and its embedding.
4. Compute the stable parent vector as the L2-normalized mean of all successful child vectors. This gives the existing `qa_pairs.embedding` contract a whole-QA representation without another over-limit provider request.
5. Recall searches both the parent vector and child vectors, takes the best score per `qa_id`, and returns the complete parent QA. A middle/tail child hit therefore never loses the surrounding answer.
6. Tool calls/results remain in source/provenance fields. They are not blindly added to the semantic chunks. Natural-language assistant content is indexed; evidence-dump indexing is deferred and does not enter this v0.2 change.

The child target is deliberately below the provider limit (`max_input_tokens - safety_margin`, default safety margin 512). Every chunk is locally counted before the provider call; if the tokenizer cannot be loaded, the item fails closed with a durable marker instead of sending an unsafe request or silently truncating.

## 4. Sidecar schema

`qa_embedding_chunks` is additive and idempotent:

- `qa_id` references the parent `qa_pairs.id` with cascade cleanup;
- `(qa_id, chunk_index)` is unique;
- `source_field`, `source_start`, `source_end`, `source_sha256` make the derived span traceable;
- `embedding` uses the configured vector dimension;
- `embed_model`, `token_count`, and `representation_version` make rebuild/dedupe decisions observable.

The parent row remains the compatibility anchor. The sidecar is not a new memory layer.

## 5. Existing-install upgrade

Fresh bootstrap includes `explicit_memories` and `qa_embedding_chunks`. Existing installations use a separate additive upgrade path:

- `hippocampus upgrade --target ... --dry-run` is read-only and reports missing canonical objects;
- applying the upgrade is explicit, transactional, idempotent, and has no `DROP`, `TRUNCATE`, `DELETE`, or rewrite of existing rows;
- the migration records a schema version after commit;
- `doctor --full` reports the exact missing object and the dry-run/apply command, rather than telling an existing user to recreate the database.

This round runs only the dry-run/audit path against production; it does not apply the migration or repair data.

## 6. Repair contract

Repair is per source, never a blanket reset:

- short NULL rows: retry the current representation;
- long rows: build the sidecar and parent aggregate;
- rows with a vector but stale marker: clear only the matching marker after verifying the vector;
- orphan markers: retain in the dry-run report until their source identity is verified;
- source rows without markers: report as legacy repair candidates, not silently reset.

The repair planner emits counts, IDs/source IDs, hashes, token counts, model/config fingerprint, and the exact intended state transition. Production writes remain a separately confirmed operation.

## 7. Verification gates

The implementation must pass:

- short QA regression with unchanged parent behavior;
- real BAAI/bge-m3 provider boundaries for Chinese, English/code, mixed text, near-limit and over-limit input;
- chunk recall where the query matches only the middle and only the tail of a long answer;
- restart-process recall resolving a child hit back to the same complete QA;
- fresh bootstrap and existing-install dry-run/apply tests;
- full test suite, import, rebuild smoke, fresh install, doctor, Gate 1, and a new CI-built Gate 2 from the final SHA.

No production repair, schema migration, formal tag, or formal release is part of this round.
