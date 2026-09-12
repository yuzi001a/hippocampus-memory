# Privacy & Data Flow — Hippocampus v0.1-alpha

> This document describes where user data lives and where it goes **for the
> supported public-alpha surface**. It does not enumerate every code path
> in the engine — only the flows that carry user content (messages, memory
> cards, embeddings, observer notes) and the boundaries at which they leave
> the local machine.
>
> **Privacy default:** if a provider is not configured, the engine does
> not transmit data to that provider. The "no LLM key configured" mode is a
> real, supported mode of operation — writes still land in PostgreSQL,
> keyword recall still works, but observer/E1/summarization paths skip.

---

## 1. Where data lives (local, on your machine)

| What | Where | Who can read it |
|---|---|---|
| Conversation messages, QA pairs, observer notes, topic cards, the canonical active-memory table `public.explicit_memories` | Your PostgreSQL (the one configured by `storage.pg.*` in the `config.yaml` whose top-level `basePath` resolves to your chosen profile directory). | You, plus anyone who has PG access (per your PG ACL). |
| Embedding vectors for messages / cards / topics | Same PostgreSQL (e.g. `embedding VECTOR(1024)` columns). | Same as above. |
| A small SQLite cache/journal file + a couple of JSON state files | The absolute `basePath` you set in `config.yaml` (e.g. `C:\Users\<you>\.v3-core\profiles\default\` on Windows). | You, plus any process running as your user. |
| Plugin / engine config + `.env` | The same `basePath` directory: `config.yaml` and `.env`. | You. |
| Engine logs | Local stderr / journal, depending on how you run Hermes. | You. Logs are sanitized via the engine's `_safe_err` helpers; do **not** rely on a specific coverage number for secret redaction — redact secrets yourself before pasting logs. |

> The engine never writes user content into the source tree of this repo.
> All runtime state resolves through `_resolve_data_dir(config)`.

---

## 2. Where data can leave (only if you configured it)

| Provider (config key) | What it sees | Triggered by | Skipped if unconfigured |
|---|---|---|---|
| **Embedding endpoint** (`storage.embed`) | Text snippets needed to compute a vector. For the active-memory vector lane: card `content` (truncated to the model's context window). For other recall paths: query strings + candidate text. | Every write (post-commit embedding lease on `public.explicit_memories`), every vector-lane recall, every topic-card embedding. | Yes — vector recall skips; writes still durable; keyword recall still works. |
| **LLM endpoint** (`llm`) | Conversation fragments used as LLM prompt input. Concretely: observer note synthesis gets the previous observer note + recent QA pairs; E1 yin synthesis gets the E1 prompt + recent context; topic-card extraction gets the candidate text + a system prompt; session summary gets a session digest. | Observer note roll-forward, E1 yin synthesis, topic-card extraction, session summary, `v3_extract`. | Yes — those paths are skipped entirely. The write/recall path is unaffected. |
| **Rerank endpoint** (`storage.rerank`) | The top-N recall candidates (text + score) for re-scoring. | Recall path's rerank step only. | Yes — engine uses embedding-only ordering fallback. |
| **None of the above** | — | — | Default if you set only `storage.pg`. The engine works fully local for write + keyword recall. |

The model names and endpoint URLs in `examples/config.example.yaml` are
**placeholders**. The engine does not bake in any specific provider; you
decide where requests go.

---

## 3. What the engine explicitly does NOT do

- It does **not** auto-apply the `public.explicit_memories` schema to a PG
  you did not choose. The DDL is shipped as a file
  (`src/v3-core/schema/explicit_memories.sql`) and ops owns migration.
- It does **not** upload your PG data to a third-party service.
- It does **not** send your config or secrets to anyone — they live on disk.
- It does **not** print secrets in logs. The `_safe_err`
  sanitization covers DSNs, prompts, and keys.
- It does **not** open listening sockets on the network for the alpha
  surface. `v3-core` is a library + CLI; the only network egress it does is
  to the providers you configured, plus outbound PG connections.

---

## 4. End-to-end flow: writing an active memory

```
caller (tool / hook / CLI)
   │
   │ (a) tool input: category, title, content, tags, optional provenance
   ▼
v3-hermes-plugin tool handler (v3_add / v3_store / v3_extract(write=True))
   │
   │ (b) normalizes to V3Core.store_card(...)
   ▼
v3core.V3Core.store_card
   │
   │ (c) delegates to ActiveMemoryWriter.create(...)
   ▼
v3core.active_memory_store.ActiveMemoryWriter
   │
   │ (d) INSERT ... ON CONFLICT DO NOTHING + commit (PG only, no network)
   │ (e) fresh readback SELECT against the same row
   │ (f) post-commit embedding lease:
   │       - if storage.embed configured: HTTP POST to your endpoint,
   │         then UPDATE embedding / embed_model only (no timestamp change)
   │       - if unconfigured: skipped, NULL embedding, no network call
   ▼
PG: row durable in public.explicit_memories (canonical truth)

→ No step (a)–(e) touches the network except (f), and only if you wired
  up storage.embed.
```

If you did **not** configure `storage.embed`, the row is still durable and
recallable via the keyword lane. The vector lane returns 0 hits for that row
because `embedding IS NULL`.

---

## 5. End-to-end flow: recall (next turn)

```
Hermes host → v3-hermes-plugin.on_session_switch / prefetch hook
   │
   │ (a) builds a PrefetchDeadline (8s budget; zero LLM by design)
   ▼
v3core.prefetch.build_memory_context
   │
   │ (b) query → recall_pool
   ▼
v3core.recall_pool
   │
   │ (c) keyword lane: SELECT against qa_pairs / topic tables (local PG)
   │ (d) vector lane:  SELECT against vector index (local PG)
   │     → if storage.embed configured: query text → HTTP POST → vector
   │     → if unconfigured: vector lane skipped
   │ (e) optional rerank lane:
   │       - if storage.rerank configured: top-N → HTTP POST → scores
   │       - else: embedding-only ordering
   │ (f) RRF fusion (KW_RRF_WEIGHT=0.5, VEC_RRF_WEIGHT=1.0)
   ▼
context block returned to Hermes host (local in-process)
```

No step (a)–(f) sends your full memory history to anyone — only the
specific snippets needed to embed the query, optionally rerank the top-N,
and (if LLM is configured) the observer/E1 prompts which are local to your
machine.

---

## 6. End-to-end flow: backup / restore

```
pg_dump -Fc against your PG  →  .dump file on your disk
                                  │
                                  │ (optional) move to offline storage
                                  ▼
pg_restore into an empty isolated PG (your disk + your PG)
```

The dump is **local-only**. The supported recipe in
[`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md) does not transmit the dump
file off your machine.

---

## 7. Observability of data flow

The plugin ships a `v3_health` tool that reports connection state per
provider:

- `pg.ok` / `pg.fail`
- `embed.ok` / `embed.skip` (unconfigured) / `embed.fail`
- `llm.ok` / `llm.degraded` (last call failed) / `llm.skip` (unconfigured)
  / `llm.fail` (credential_missing / auth_failed / rate_limited / timeout /
  endpoint_or_model_error)
- `rerank.ok` / `rerank.skip` / `rerank.fail`

This is the user-visible "did my data leave the machine?" knob. If
`llm.skip`, no LLM traffic is happening. If `embed.skip`, no embedding
traffic is happening. The classification enum and the skip-vs-fail
distinction are documented in `src/v3-core/src/v3core/llmstatus.py`.

---

## 8. Handling secrets

The public tree is intended to contain no real credentials. If you
discover a credential in a checkout, log, issue, or configuration, do
not publish it or paste it into a public issue. Rotate or invalidate it
at the source provider, then remove it from local logs and temporary
files. This repository does not promise a private security channel;
withhold sensitive details until a verified private channel is
available.

## 9. Threat-model boundaries (alpha scope)

| Threat | In scope for alpha? | Where it's addressed |
|---|---|---|
| Credentials in plaintext on disk | Yes | This doc + `docs/CONFIGURATION.md` § "Environment variables". |
| Provider endpoint typo leaking data to the wrong host | Partial | User-config endpoint; no automatic allowlist. Double-check URLs. |
| Log leakage of DSN / prompt / API key | Yes | `_safe_err` historical coverage; `v3_health` redacts provider errors. |
| Memory contents leaking between users / profiles | Partial | Per-profile `data_dir` separation; per-profile PG database is the user's responsibility. |
| Network exfiltration by the engine itself | Yes | This doc + the `requires_env` plugin manifest. |
| Long-term identity (E1 yin) carrying private data to LLM | Yes | The E1 prompt is configurable; if your LLM is a third party, treat E1 prompts as data you send. |

Anything outside the table above is **out of scope** for this document and
not a public-alpha claim.
