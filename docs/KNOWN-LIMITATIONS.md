# Known Limitations — Hippocampus v0.1-alpha

> This document lists things that are **known to be incomplete, deferred,
> experimental, or unverified** at the time of writing. It does not list
> features the engine simply doesn't have — for that, see
> [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md).
>
> Every row below is grounded in the same evidence window that
> `PUBLIC_ALPHA_SUPPORTED_SURFACE.md` § 2 records — the clean-export
> E2E run from an empty tmpfs pgvector/pg17 + the pg_dump/pg_restore
> cycle. Items still **UNKNOWN / NOT TESTED** on this HEAD are marked
> as such, not silently carried over from earlier gates. The focused
> acceptance scope for this release is **191 passed**; this is not a
> full-suite claim.

---

## 1. Hard limits a trial user is likely to hit

### 1.1. `pyahocorasick` C extension

`v3-core` declares `pyahocorasick>=2.3.0` as a runtime dependency. On
Windows, building the C extension requires the MSVC toolchain that
matches your Python.

- If you installed Python from python.org, install the matching "Build
  Tools for Visual Studio" workload.
- If `pip install` fails on this step, the engine still loads but
  `_qa_snapshot_lookup` for **long** prefetch queries falls back to the
  nested-substring scan, which does **not** meet the 6.5/8s SLO for long
  queries. Short queries are unaffected.
- There is no pre-built Windows wheel bundled with the package; the
  upstream PyPI wheel works for many Python versions but not all
  combinations. The alpha evidence was produced on Python 3.11.16.
  If 3.12 fails on your machine, try 3.11.

### 1.2. PostgreSQL + pgvector major version

The supported-surface evidence is on **`pgvector/pgvector:pg17`**. The
recipe in [`docs/INSTALL.md`](INSTALL.md) and
[`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md) documents that image
tag. Restoring a `pg17` dump onto a different PostgreSQL major version
is **not** part of the alpha contract and was not exercised in this
evidence window.

### 1.3. Embedding dimensionality

The active-memory table uses `VECTOR(1024)` (the bge-m3 default). If
your embedding provider returns a different dimension, you need a
different DDL — which is a non-trivial ops change. The supported surface
does **not** cover non-1024-dim embeddings.

### 1.4. Hermes host version

`v3-hermes-plugin` is built against the Hermes memory-provider API as of
the 2026-08 review. The clean-export install validated `hermes-agent
0.21.0` with full deps in the fresh venv (`pip check` clean; plugin
`get_tool_schemas` count = 13). If your Hermes host has a different
provider loading contract, the plugin manifest may not load. The
supported surface assumes the user can `pip install` the plugin into
a Hermes-compatible Python environment and that
`V3CORE_PG_PASSWORD` is set before the plugin loads.

### 1.5. Configuration: paths, env vars

- The YAML config loader does **not** expand `~` on Windows. The
  supported path-style config key is the **top-level `basePath` only**
  (see `v3core.config._resolve_data_dir`). The example file
  ([`examples/config.example.yaml`](../examples/config.example.yaml))
  declares the absolute path under top-level `basePath`. Set that
  absolute profile directory explicitly; no alternate data-root override
  is part of the public-alpha contract.
- The plugin manifest hard-requires `V3CORE_PG_PASSWORD`. If your Hermes
  host enforces `requires_env` strictly, you cannot boot the plugin
  without it.
- Optional `storage.embed` / top-level `llm` / `storage.rerank` blocks
  may be omitted; the engine is fail-closed for any unconfigured
  provider (see [`docs/CONFIGURATION.md`](CONFIGURATION.md) § 4). If
  `embed` is omitted, the engine runs in keyword-only recall mode
  (vector lane skipped, writes still durable).

### 1.6. Replay coverage after restart (the real replay limitation)

The alpha E2E confirmed a **narrow** replay/recovery capability and a
**wider** replay gap:

- **fresh export stranger smoke:** one direct `sync_turn` produced
  `conversation_stream` = 2 and `qa_pairs` = 0; a new provider process
  retained active-memory keyword recall. QA pairing, retry, and
  restart-marker behavior are covered by the focused ingest tests.
- **Not rebuilt in the LLM-disabled smoke:**
  observer rolling imprint, E1 yin synthesis, topic cards, and the
  `E1`人格 layer are **derived state** and are not reconstructed
  from raw `conversation_stream` rows on restart. If the alpha ran
  without an `llm` provider (the supported alpha evidence did),
  these derived layers will be empty in your trial until you configure
  an LLM provider and let the engine re-derive them. Do not treat a
  missing observer/E1/topic layer as data loss — it is a derivation
  that has not been run.
- **`public.explicit_memories` is the canonical backup asset.** It is
  not inferred from the raw `conversation_stream` / `qa_pairs`
  history during a restore. A `pg_dump` of the supported surface
  contains `explicit_memories` as its own table; if you restore a
  dump that was taken before a card was written, that card is not
  reconstructible from the raw chat. Backups and active-memory writes
  must be taken from the same supported-surface instance; do not
  assume replay from raw chat will recreate active memories.

---

## 2. Things explicitly out of scope for this alpha

### 2.1. Recall V2

Not implemented. The supported surface uses the existing
`recall_pool` keyword + vector + RRF seam with current weights. Typed
provenance (`RecallTrace`), temporal intent (`normal / latest / time_range
/ historical`), and the new QueryPlan/QueryContext types are designed but
not shipped in this alpha.

### 2.2. Multi-writer / multi-agent

Not implemented. The schema may reserve `agent` / `namespace` columns in
later work, but the runtime does not currently coordinate multiple
writers. If you run two plugin instances against the same PG, they will
race on the canonical active-memory table.

### 2.3. Manual topic surgery

`v3_topic_correct` is in the plugin manifest but is **experimental** for
this alpha. The supported surface does not promise that
`v3_topic_correct` is durable, idempotent, or correct under retry. If you
need it, treat the result as advisory only. The alpha evidence did not
exercise observer/E1/topic synthesis on this HEAD.

### 2.4. Legacy migration

There are historical SQLite-mirror / shou / MOC / dedup code paths in the
repository. They are **not** part of the supported surface and are not
maintained as a public migration story. If you have historical data in
those paths, there is **no supported migration** to the canonical
`public.explicit_memories` table.

### 2.5. Long-soak evidence

`LONG_SOAK` is `PENDING / NON-BLOCKING`. The supported surface does
**not** claim 24h / 7d stability. Trial users running the engine for
a few hours may encounter edge cases not covered by the lab.

### 2.6. Post-compression QA evidence

`POST_COMPRESSION_QA_EVIDENCE` is also `PENDING / NON-BLOCKING`. The
post-compression ingest path is **not** the supported surface; only the
non-compressed normal ingest path is. If your Hermes host triggers
compression while you trial, you may observe behavior outside the alpha
contract.

### 2.7. Full pytest green

The focused acceptance scope for this release is **191 passed** when
run file-by-file to avoid known order contamination. This is not a
claim that the historical full repository suite is green; run the full
suite yourself against a disposable PG if you need that broader
property.

### 2.8. Host-networked provider end-to-end

The local Hermes provider adapter initialization/sync/store smoke was
re-executed after the env + empty-embed fixes and passed with LLM
disabled and local deterministic embeddings.

- A live host-networked LLM + embed contract is **NOT TESTED** on this
  HEAD. The alpha evidence used a local deterministic embed path and did
  not configure an `llm` provider.
- `v3_extract(write=True)`, observer rolling imprint, E1 yin
  synthesis, and topic-card extraction all require an LLM provider;
  they remain skipped in the supported alpha evidence.

---

## 3. What the public-alpha does not close

These are boundaries the alpha does **not** close. They are recorded in
[`docs/RELEASE-CHECKLIST.md`](RELEASE-CHECKLIST.md) (the public
checklist). Read that document for the full follow-up list. Statuses
reflect the evidence window described in § 1.6, not historical
carryover.

| Area | Status for this alpha | What it would take to close |
|---|---|---|
| Data safety (declared supported surface only) | **CLOSED on declared surface** — fresh export v3_store returned `DURABLE_COMMITTED`, restart recall passed, and the focused ingest/active-memory suites passed. | Continued lab work beyond the clean-boundary slice; full-suite green. |
| Recovery (dump/restore into an empty isolated PG) | **CLOSED on declared surface** — fresh export pg_dump/pg_restore reproduced `raw` = 2, `QA` = 0, `explicit` = 1; post-restore write + keyword recall passed. | Same-image-tag restore is the contract; cross-major-version restore is still out of scope. |
| Installability (fresh-install matrix) | **CLOSED on declared surface** — fresh export installed in a fresh venv; `pip check`, `v3-core info`, adapter init, bootstrap, and second bootstrap passed. | Independent-user fresh-install matrix on multiple OS / Python combinations is still pending. |
| Privacy / security | **PASS for this publication** — the clean-history public tree contains no real credentials, private endpoints, or local runtime state; provider credentials are supplied locally and are not bundled. | Re-run the same publication security gate for any future release that changes the tree or introduces new provider credentials. |
| Basic production usability | **PASS on declared alpha smoke slice** — fresh export covered info, sync_turn, v3_store, keyword readback, process restart, and restore write/recall; focused acceptance was **191 passed**. | Long-soak evidence, live host-networked provider run, and full-suite green. |

These limitations do not block this technical preview; they define
what Hippocampus v0.1-alpha does not claim to be. A future stable
release would need its own broader evidence.

---

## 4. Things we don't know (UNKNOWN)

Be honest: the following are claims we have **not** verified on this
exact HEAD and have no current run report for:

- Whether `pytest tests/` is fully green across every historical test
  file on this exact HEAD. The focused acceptance scope used for this
  candidate is **191 passed**; this is not a full-suite claim.
- Whether the host-networked LLM + embed contract passes end-to-end
  via Hermes on this HEAD. The local provider adapter smoke passed with
  LLM disabled and local deterministic embeddings; a live external
  provider run remains **UNKNOWN / NOT TESTED**.
- Whether `v3_extract(write=True)` produces the documented
  per-card `durable/status/source/error/warnings` receipts on this
  HEAD with an LLM provider. The LLM path was not exercised.
- Whether the observer rolling imprint + E1 yin synthesis + topic
  synthesis paths produce stable, restart-survivable derived state on
  this HEAD. They were not exercised (LLM disabled in the alpha
  smoke).
- Whether `v3_health` returns the documented per-provider status
  report across pg / embed / llm / rerank on this HEAD. The tool was
  not run end-to-end against live providers.
- Whether the full 13-tool surface returns caller-side receipts that
  satisfy observability expectations. Only the
  write/recall/archive/restart paths were exercised in the alpha E2E;
  the remaining tools were not enumerated in the same run.
- Whether the engine behaves correctly under sustained multi-session
  load for >24h. (See § 2.5.)
- Whether the active-memory recall RRF weights
  (`KW_RRF_WEIGHT=0.5`, `VEC_RRF_WEIGHT=1.0`) still produce the same
  hit ordering on this HEAD for a non-mock embed provider.

These are flagged as **UNKNOWN** rather than treated as failures. A
reproduction trial that falsifies any of them is valuable — please
file an issue with the failure mode.

---

## 5. What we deliberately do NOT plan to fix in this alpha

These are design decisions, not bugs:

- The plugin manifest is **exclusive** (`kind: exclusive`). You cannot
  load two memory providers at once. This is by design.
- The supported surface deliberately uses `pg_dump -Fc` rather than
  logical replication. Continuous backup is a separate ops decision.
- The active-memory schema is `VECTOR(1024)` only. If you need a
  different dim, you are outside the supported surface.
- `v3-core` does **not** implement a server. It's a library + CLI. If
  you need a network-facing service, wrap it in your own process.
- `explicit_memories` is the canonical active-memory store. Raw
  `conversation_stream` + `qa_pairs` are durable sources for replay
  of the ingest path only; they are not used to derive or re-derive
  `explicit_memories` rows on restore. Treat the canonical PG dump as
  the backup asset; do not assume raw-chat replay will recreate
  active memories.
- Top-level `basePath` is required and absolute. The engine does not
  default to a particular user path and does not expand `~`. The
  legacy/internal last-resort `Path.home() / ".v3-core" / ...` branch
  in `_resolve_data_dir` is an implementation detail, not a
  recommended contract — set `basePath` to the absolute path of the
  profile directory you chose. This is by design (see
  `docs/CONFIGURATION.md` § 1).
