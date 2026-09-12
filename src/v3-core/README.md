# v3-core

> **Hippocampus — v3-core engine.**
> Public Alpha release of this repo. See [`../../README.md`](../../README.md) for the front door and
> [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](../../docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
> for the supported-surface contract.

`v3-core` is a Python library + CLI that gives a chat-based AI agent a
durable memory layer. It has **no Hermes dependency** — you can use it
from any LLM project.

---

## What it does (alpha scope)

- Stores conversation messages, QA pairs, observer notes, and topic
  cards in PostgreSQL (with `pgvector`).
- Maintains a canonical **active-memory** table
  (`public.explicit_memories`) for "remember this" style cards. Writes
  are idempotent (`INSERT ... ON CONFLICT DO NOTHING` + fresh readback);
  receipts are `DURABLE_COMMITTED` / `DEDUPLICATED` /
  `DURABLE_FAILED` / `DERIVED_WARNING`.
- Recalls via a keyword lane + (optional) vector lane + (optional)
  rerank lane, fused by RRF.
- Reports provider health via `v3_health` when that tool is used; `v3-core info`
  is a separate minimal status-summary command.

## What it deliberately does NOT do (alpha scope)

- It does **not** host a network service. It is a library + CLI; wrap it
  in your own process if you need that.
- It does **not** auto-apply the schema. The DDL is shipped as
  `src/v3-core/schema/explicit_memories.sql` (and the alpha bootstrap
  wrapper at `src/v3-core/schema/alpha_bootstrap.sql`). The
  `v3core.active_memory_store` writer does **not** apply DDL — ops
  (or the trial user) runs
  `src/v3-core/scripts/bootstrap_alpha_db.py` explicitly. See
  [`docs/INSTALL.md`](../../docs/INSTALL.md) for the bring-up flow.
- It does **not** implement Recall V2 (typed provenance, temporal
  intent), multi-writer, or multi-agent routing. See
  [`docs/KNOWN-LIMITATIONS.md`](../../docs/KNOWN-LIMITATIONS.md).
- It does **not** ship a SaaS or hosted offering.

---

## Install

```bash
pip install ./src/v3-core
```

This builds `v3-core` from `src/v3-core/pyproject.toml` and installs the
`v3-core` console script (`v3-core info`).

Requirements:

- Python ≥ 3.10
- A `pgvector` PostgreSQL instance (a Docker container using
  `pgvector/pgvector:pg17` is the documented disposable path; matches
  the clean-history export E2E run recorded in
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](../../docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
  § 1).
- The C extension build chain for `pyahocorasick` on Windows (see
  [`docs/KNOWN-LIMITATIONS.md`](../../docs/KNOWN-LIMITATIONS.md) § 1.1).

> The supported-surface bring-up also requires an explicit DB bootstrap
> step before the first write — see
> [`docs/INSTALL.md`](../../docs/INSTALL.md) § "DB bootstrap". The
> `v3core.active_memory_store` writer does not auto-apply DDL.

## CLI

```bash
v3-core info
```

Prints the engine status summary for the resolved profile. Exit code is
non-zero if status initialization fails; provider health is reported by
`v3_health`, not by this command.

## Python API

The supported surface is exercised through the `V3Core` façade:

```python
from v3core import V3Core

# You must pass an explicit config path (the engine does NOT expand
# `~` on Windows and does NOT default to a particular user path):
v3 = V3Core(config_path="C:\\Users\\<you>\\.v3-core\\profiles\\default\\config.yaml")
result = v3.store_card(
    category="lessons",
    title="Read the README",
    content="The README is not decorative.",
    tags=["meta"],
)
# result.status ∈ {"DURABLE_COMMITTED", "DEDUPLICATED",
#                  "DURABLE_FAILED", "DERIVED_WARNING"}
```

> Placeholder example only — do not embed real credentials or production
> endpoints in your own code. See
> [`docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md) for how to wire up
> providers.

The active-memory path goes through
`v3core.active_memory_store.ActiveMemoryWriter`. It is **the only**
canonical write path for `public.explicit_memories`; passive paths
(observer / E1 / topic_store / dedup) do not perform DML against it.

## Configuration

Engine config lives at the absolute profile path you set under the
top-level `basePath` key in `config.yaml` (see
[`docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md)). The public-alpha
contract has no alternate data-root override; write the absolute path
explicitly. A starter template with **placeholder values only** is at
[`examples/config.example.yaml`](../../examples/config.example.yaml).

## Backup / restore

See [`docs/BACKUP-RESTORE.md`](../../docs/BACKUP-RESTORE.md). The recipe is
`pg_dump -Fc` followed by `pg_restore` into an empty isolated
`pgvector` container, with a post-restore active-memory write + recall
proof. **Status (this HEAD):** the clean-export E2E run on empty tmpfs
pg17 reproduced the row counts (`raw` = 2, `QA` = 0, `explicit` = 1) —
see [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](../../docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
§ 2.5 for the evidence row. Remaining alpha limitations the alpha does not
close by itself are listed in
[`docs/RELEASE-CHECKLIST.md`](../../docs/RELEASE-CHECKLIST.md)
§ E.

## Project layout (alpha-facing subset)

```
src/v3-core/
├── src/v3core/
│   ├── __init__.py                # V3Core façade
│   ├── __main__.py                # CLI entrypoint (`v3-core info`)
│   ├── active_memory_store.py     # canonical writer/reader
│   ├── card_store.py              # legacy explicit-write path (still in code)
│   ├── sqlite_store.py            # SQLite cache + cross-thread RLock
│   ├── pg_pool.py                 # PgPool + PgLease (admission)
│   ├── pg_store.py                # PgEmbedStore
│   ├── ingest.py                  # LiveBuffer + outbox
│   ├── observer.py                # observer note roll-forward
│   ├── e1.py                      # E1 yin synthesis
│   ├── recall_pool.py             # keyword + vector + RRF
│   ├── prefetch.py                # PrefetchDeadline + build_memory_context
│   ├── extractor / extract.py     # v3_extract logic
│   ├── embedding.py               # OpenAI-compatible embed client
│   ├── llm.py                     # OpenAI-compatible chat client
│   ├── llmstatus.py               # llm observability state
│   ├── config.py / config_model.py
│   └── ...
├── schema/
│   ├── explicit_memories.sql      # canonical active-memory DDL (artifact)
│   └── alpha_bootstrap.sql        # alpha bring-up wrapper (artifact)
├── scripts/
│   ├── bootstrap_alpha_db.py      # explicit alpha DB bootstrap (CLI)
│   └── backup_alpha.py            # alpha backup helper (pg_dump / pg_restore)
├── tests/                         # focused tests
```

## Design philosophy (alpha framing)

- **Memory is a time stream** — QA pairs are paired in time, not sliced
  by session.
- **Source vs derived** — `conversation_stream` / `qa_pairs` /
  `public.explicit_memories` are source/canonical; observer roll-
  forward, E1 yin, topic cards, and `[主动记忆]` blocks are derived
  and recomputable. Derived paths must never mutate source.
- **Privacy default** — if a provider is unconfigured, the engine does
  not transmit data to it. See
  [`docs/PRIVACY-DATA-FLOW.md`](../../docs/PRIVACY-DATA-FLOW.md).
- **Configuration externalized** — endpoints and credentials live in
  `config.yaml` / `.env`, not in code.
- **No automatic schema bootstrap** — schema artifacts are shipped as
  files; ops (or the trial user) applies them via
  `bootstrap_alpha_db.py`.

## License

AGPL-3.0-or-later — see [`LICENSE`](LICENSE). This package does not
relicense either subpackage.

---

> **Note:** this README replaces a previous version that used a tiered
> pre-alpha product framing (e.g. offline / BYOK / local stack). That
> framing has been retired; the supported public-alpha surface is
> described in
> [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](../../docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md).
> The pre-alpha tier labels are preserved as historical context only
> and should not be cited as the current contract.
