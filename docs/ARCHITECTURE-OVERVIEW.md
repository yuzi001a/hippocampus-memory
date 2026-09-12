# Architecture Overview — Hippocampus v0.1-alpha

> A reading map for someone who wants to understand the supported
> surface without reading the whole engine. This concise document is
> the public alpha-facing summary; the source tree is the authority for
> implementation details outside the supported contract.
>
> Code references below are to the actual modules on this release's
> HEAD. Internal development identifiers are not part of
> the public contract — see
> [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
> § 1 for the evidence window the alpha claims depend on. File paths
> are relative to the repo root unless stated otherwise.

---

## 1. Two packages, one engine

```
hippocampus-memory/
├── src/v3-core/                       ← engine (no Hermes coupling)
│   ├── src/v3core/
│   │   ├── __init__.py                ← V3Core façade
│   │   ├── active_memory_store.py     ← ActiveMemoryWriter / ActiveMemoryReader
│   │   ├── card_store.py              ← write_card_strict (legacy path, still in code)
│   │   ├── sqlite_store.py            ← SQLite cache + RLock + check_same_thread=False
│   │   ├── ingest.py                  ← LiveBuffer + outbox
│   │   ├── pg_pool.py                 ← PgPool + PgLease
│   │   ├── pg_store.py                ← PgEmbedStore
│   │   ├── observer.py                ← observer note roll-forward
│   │   ├── e1.py                      ← E1 yin synthesis
│   │   ├── topic_store.py             ← topic cards (PG + SQLite bridge)
│   │   ├── recall_pool.py             ← keyword + vector + RRF
│   │   ├── rerank.py                  ← optional rerank lane
│   │   ├── prefetch.py                ← PrefetchDeadline + build_memory_context
│   │   ├── injector.py                ← block formatting for the context
│   │   ├── extract.py                 ← v3_extract logic
│   │   ├── embedding.py               ← OpenAI-compatible embed client
│   │   ├── llm.py                     ← OpenAI-compatible chat client
│   │   ├── llmstatus.py               ← observability state for llm
│   │   ├── config.py / config_model.py
│   │   └── ...
│   ├── schema/
│   │   ├── explicit_memories.sql      ← canonical active-memory DDL (artifact)
│   │   └── alpha_bootstrap.sql        ← additive alpha bring-up wrapper (artifact)
│   ├── scripts/
│   │   ├── bootstrap_alpha_db.py      ← explicit alpha DB bootstrap (CLI)
│   │   └── backup_alpha.py            ← alpha backup helper (pg_dump / pg_restore)
│   ├── tests/                         ← focused tests (unit + disposable-PG)
│
└── src/v3-hermes-plugin/              ← Hermes adapter
    └── src/v3hermes/
        ├── __init__.py                ← provider entry + 13 tool handlers + 6 hooks
```

The split is deliberate: `v3-core` has **no Hermes dependency** and can be
embedded in any LLM project; `v3-hermes-plugin` is a thin adapter that wires
the engine into a Hermes host.

---

## 2. Source-of-truth boundary: where active memory lives

The supported surface has exactly **one** canonical write path for active
memory:

```
caller (tool, hook, CLI)
   ↓
V3Core.store_card(...)
   ↓
ActiveMemoryWriter.create(...)        ← src/v3-core/src/v3core/active_memory_store.py
   ↓
public.explicit_memories (PG)          ← src/v3-core/schema/explicit_memories.sql
```

What this boundary guarantees:

- `v3_add` / `v3_store` / `v3_extract(write=True)` all reach PG through the
  same `ActiveMemoryWriter.create` code path.
- No `b_*` / `shou_*` / SQLite mirror table holds active-memory content as
  the canonical store. The SQLite file under the absolute profile
  directory (top-level `basePath` in `config.yaml`) is cache / journal
  only; it can be regenerated from PG.
- Observer / E1 / topic_store / dedup (passive paths) **do not** perform
  DML against `public.explicit_memories`. This is enforced by the
  `test_C5_passive_paths_have_no_explicit_memories_dml` test.

What this boundary does **not** guarantee:

- It does not auto-apply the schema. The DDL is a file shipped with
  the package; ops owns migration via
  `src/v3-core/scripts/bootstrap_alpha_db.py` (see
  [`docs/INSTALL.md`](INSTALL.md) § 6).
- It does not magically reconcile historical data. Active memories from
  the legacy SQLite-mirror era are **not** imported.
- It does not bypass the embedding provider. If `storage.embed` is
  unconfigured, `embedding` is NULL on the canonical row and the vector
  lane does not hit that row.

---

## 3. Source vs derived

A useful mental model that the engine itself uses:

| Tier | Examples | Source-of-truth | Engine guarantees |
|---|---|---|---|
| **Source / raw** | `conversation_stream` rows, `qa_pairs` rows, the original observer note (`version LIKE 'v%'` chain head) | PG | Idempotent writes; stable identity across retries; LiveBuffer outbox |
| **Canonical active memory** | `public.explicit_memories` rows | PG | `INSERT ... ON CONFLICT DO NOTHING` + fresh readback; `DURABLE_COMMITTED` / `DEDUPLICATED` / `DURABLE_FAILED` / `DERIVED_WARNING` |
| **Derived / computed** | Observer note roll-forward, E1 yin paragraphs, topic cards, `[主动记忆]` injection blocks, RRF recall ranking | Recomputable from source + active memory | Recomputable; allowed to be wrong / stale; **must not** mutate source |

The contract:

- Derived paths **read** source + canonical active memory.
- Derived paths **never** `INSERT` / `UPDATE` / `DELETE` / `MERGE` against
  `public.explicit_memories`.
- Source / canonical paths **never** call the derived path's write APIs to
  "self-correct" — that creates loops.

This split is what "clean boundary" means in the alpha contract. See
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md) §
2.3 for the corresponding supported-surface row.

---

## 4. Hook → flow mapping (Hermes-mediated)

| Hook | Owner module | Direction | Network |
|---|---|---|---|
| `on_session_switch` | `v3hermes.__init__` | Host → plugin | none |
| `sync_turn` | `V3Core.sync_turn` | Plugin → engine (write) | PG only by default |
| `prefetch` | `v3core.prefetch` + `V3Core.build_memory_context` | Engine → host (read) | PG only; embed if configured |
| `on_pre_compress` | `v3hermes.__init__` | Host → plugin | none |
| `on_delegation` | `v3hermes.__init__` | Host → plugin | none |
| `system_prompt_block` | `v3hermes.__init__` | Engine → host (read) | PG only |

The 8-second prefetch budget is hard-capped at the provider layer (via
`PrefetchDeadline`) and the engine deliberately performs **zero LLM
calls** inside that budget. The LLM-dependent paths (observer roll-forward,
E1, session summary, topic extraction) run on background timers, not in the
prefetch hot path.

---

## 5. Recall: keyword + vector + rerank → RRF

The `recall_pool` module owns the orchestration:

1. **Keyword lane** — tokenized query, hit against `qa_pairs` and topic
   tables via SQL. Local PG only.
2. **Vector lane** — embed the query (if `storage.embed` configured), hit
   the IVFFLAT index on `public.explicit_memories.embedding`. Local PG.
3. **Optional rerank lane** — if `storage.rerank` configured, send the
   top-N candidates to the rerank endpoint.
4. **RRF fusion** — `KW_RRF_WEIGHT=0.5`, `VEC_RRF_WEIGHT=1.0`. These are
   constants on `recall_pool`; the supported surface does **not** introduce
   new weights.

The supported surface reuses this seam for the new `[主动记忆] <title>...`
block — same lanes, same RRF weights, just an extra `kind=active_memory`
hit type. No new lane, no new constant, no new RRF weight.

---

## 6. Tools (plugin-side, 13 total)

Four groups in `src/v3-hermes-plugin/plugin.yaml`:

| Group | Tools | Notes |
|---|---|---|
| Unified entry | `v3_add`, `v3_get`, `v3_update`, `v3_manage` | Single-purpose routing into the underlying APIs. |
| High-frequency | `v3_store`, `v3_search`, `v3_status`, `v3_extract`, `v3_prefetch` | What most callers actually use. |
| Topic / handbook | `v3_topic_correct`, `v3_moc_overview`, `v3_moc_get` | Read paths; topic-correct is **experimental** for this alpha. |
| Health | `v3_health` | The per-provider observability surface. |

The historical 31-tool surface was narrowed to 13 in 2026-08-07. The
`v3core.TOOL_REGISTRY` still exposes the full 31; the plugin handler
narrows the surface to what `plugin.yaml` declares. This is by design.

---

## 7. Lifecycle: Runtime identity

`RuntimeRegistry` / `V3Runtime` give every active plugin instance its own
generation + ACTIVE→DRAINING→CLOSED lifecycle. Each runtime owns:

- one `ObserverService` + one `E1Service` (per-identity, no sharing),
- one `TopicRecallCache` (per-runtime, invalidate-after-commit),
- one `PgPool` reference (admission via `PgLease`).

Cross-runtime fences use PostgreSQL advisory locks; the physical PG
connection is held for the duration of the work, then released. This
matters for the prefetch deadline contract (above) and for restart
recovery.

For the alpha, you don't need to know any of this to use the supported
surface — but if you read `runtime.py` and see `DRAINING` markers, that's
why.

---

## 8. Backup / restore boundary

See [`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md) for the recipe. From an
architecture standpoint, the key points are:

- The **canonical truth** is `pg_dump` of the configured PG. The local
  profile directory (top-level `basePath` in `config.yaml`) is **not**
  authoritative.
- The schema artifact at `src/v3-core/schema/explicit_memories.sql` is
  bundled into the dump by `pg_dump` (because the table is in PG, and the
  dump captures the DDL plus the extension entry).
- Restore onto an empty isolated PG must produce a table presence + row
  count parity with the source before the dump is considered trustworthy.

---

## 9. Where to read next

- For the *what is supported* question →
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md).
- For the *how do I install it* question →
  [`docs/INSTALL.md`](INSTALL.md).
- For the *what data goes where* question →
  [`docs/PRIVACY-DATA-FLOW.md`](PRIVACY-DATA-FLOW.md).
- For the *what's still broken / deferred* question →
  [`docs/KNOWN-LIMITATIONS.md`](KNOWN-LIMITATIONS.md).
- For implementation details outside the supported surface, inspect
  the corresponding source modules only after reading the supported-
  surface and limitations documents.
