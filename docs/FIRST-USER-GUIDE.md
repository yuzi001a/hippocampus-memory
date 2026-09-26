# Hippocampus — First User Guide

**Audience:** someone who has never seen this repository and wants a working long-term memory
attached to their agent on Windows.

This guide is written against the real scripts. Every command below is the command the installer
actually runs — if a step here does not work, that is a bug, not a missing paragraph.

---

## 1. Quick start (one command)

Open **PowerShell** and run:

```powershell
irm https://raw.githubusercontent.com/yuzi001a/hippocampus-memory/main/install/install.ps1 | iex
```

The script checks the machine, then hands over to `hippocampus install`, which performs every step
in §3 and prints a verdict block. Nothing else is required: no `git clone`, no wheel building, no
virtualenv, no manual PostgreSQL setup.

> If you prefer to see what will run before it runs:
> ```powershell
> irm https://raw.githubusercontent.com/yuzi001a/hippocampus-memory/main/install/install.ps1 -OutFile install.ps1
> notepad install.ps1      # read it
> .\install.ps1
> ```

### What you need beforehand

| Requirement | Why | If missing |
|---|---|---|
| Windows 10/11 + PowerShell | primary supported target | — |
| Python 3.10–3.12, or `uv` | the engine and the CLI are Python | the installer tells you the exact command to install `uv` |
| Docker Desktop (running) | hosts the PostgreSQL + pgvector database | the installer stops with a clear message; install Docker Desktop and re-run |
| An embedding/rerank API key (SiliconFlow) | recall needs vectors | the installer marks the embedding step SKIPPED and tells you the re-run command |
| A memory-LLM API key (the selected preset/provider) | observer / E1 / topic synthesis | same: SKIPPED with a re-run command |
| ~5 GB free disk | image + database + venv + your history | — |

---

## 2. One-command install — what it actually does

`install/install.ps1` itself only:

1. finds Python (or `uv`), and fails with a human message if neither exists;
2. checks that Docker Desktop is installed **and running**;
3. installs/upgrades the release packages;
4. calls `hippocampus install`, which does the real work and prints the verdict.

Everything else lives in `v3core/first_run.py`, so it is testable and identical on every machine.

---

## 3. First 10 minutes

The installer runs these steps in order and prints the verdict topics shown below. The end-to-end write/recall smoke is a
separate sub-step; it is not an eighth verdict topic.

| # | Step | What it does | What "done" looks like |
|---|---|---|---|
| 1 | install | Python / uv / Docker / existing install | `PASS` with versions |
| 2 | database | starts or reuses a `pgvector/pgvector:pg17` container on a local port (default `55432`), verifies `CREATE EXTENSION vector` | `PASS pgvector 0.8.x` or the version your image ships |
| 3 | config | writes the profile config with an absolute `basePath`, the database block, and the provider blocks | `PASS` + the config path |
| 4 | bootstrap | applies the packaged SQL (canonical tables plus the two derived-index sidecars: `qa_embedding_chunks`, `observation_embedding_chunks`) — idempotent | `PASS`, and a second run changes nothing |
| 5 | Hermes wiring | installs the provider into the Hermes environment and sets `memory.provider: deep_memory_v3`, with a backup of the old config | `PASS` + backup path |
| 6 | doctor | read-only checks + real probes | `PASS` (see §7 for the full check list) |
| 7 | end-to-end smoke | writes one real memory, reads it back, and recalls it | `PASS recall_hit=true` (keyword fallback is allowed in this install smoke) |

The install verdict block has seven topics:
```
install              : PASS
database             : PASS  (127.0.0.1:55432, pgvector 0.8.0)
embedding            : PASS  (BAAI/bge-m3, dim 1024)
rerank               : PASS  (BAAI/bge-reranker-v2-m3)
memory LLM           : PASS  (the configured preset/model)
hermes provider      : PASS  (deep_memory_v3 discovered)
restart persistence  : advisory — restart the agent yourself, then run the independent restart-recall canary
```

Then start your agent normally and restart it once. `hippocampus doctor --full` can perform a read-back
hint, but it does **not** restart the agent and does not prove cross-process recall. The independent
release Gate 1 canary is the evidence that proves source → derived index → recall after a fresh process.

### Presets

```powershell
hippocampus install --preset siliconflow    # recommended: one embedding key + one LLM key
hippocampus install --preset custom         # you supply endpoint/model/dim for every provider
```

The `siliconflow` preset pre-fills:

| Role | Endpoint | Model | Dim |
|---|---|---|---|
| embedding | `https://api.siliconflow.cn/v1/embeddings` | `BAAI/bge-m3` | 1024 |
| rerank | `https://api.siliconflow.cn/v1/rerank` | `BAAI/bge-reranker-v2-m3` | — |
| memory LLM | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` | — |

The preset uses one SiliconFlow key for embedding, rerank, and the memory LLM. To use MiniMax or another
provider, choose `custom` and supply that provider's endpoint, model, and key explicitly.

Provider pricing is not hard-coded by the installer; verify the selected provider's current pricing before budgeting. The
reference cost table in §5 uses the measured custom MiniMax-M3 run documented in `docs/COST.md`, not the
SiliconFlow Qwen preset automatically selected above.

### Keys

The installer never echoes a key or commits it to the repository. It writes the values to the active
profile's `.env` so later Hermes processes can load them; `config.yaml` contains only `${env:...}`
pointers (and an empty database password field). On Windows the installer applies a best-effort user-only
ACL to `.env`; treat the profile directory as sensitive, include `.env` in your private backup policy,
and never upload or share it.

```powershell
hippocampus install --embed-key "sk-..." --llm-key "sk-..."
# The installer writes the profile .env; do not paste the secret into config.yaml.
```

If you rotate a key, edit the profile `.env` and rerun `hippocampus doctor --full`.

---

## 4. Import existing memory

Hippocampus is useful on day one only if it can inherit a past. The import framework keeps three
layers strictly separate:

| Layer | What it is | Where it lands |
|---|---|---|
| `raw_message` | your original conversations (Hermes / OpenClaw sessions) | source layer — the historical truth |
| `user_curated` | memory you wrote by hand (`MEMORY.md`, `USER.md`, `SOUL.md`) | curated layer, with file + line + hash provenance |
| `legacy_derived` | another system's LLM summaries (Hindsight, Honcho, …) | explicitly tagged `legacy-derived` — **never** mixed into raw facts |

```powershell
hippocampus import list                 # what is supported, and at what capability level
hippocampus import hermes  --root "C:\Users\you\AppData\Local\hermes"   # real sessions
hippocampus import memory-md --root "C:\Users\you\notes"                # hand-written memory files
hippocampus import hermes  --root "..." --dry-run                       # parse and report only
```

A real import ends with a report like:

```
Imported:
- 18,432 raw messages
- 214 sessions
- 37 user-curated memories
- 126 legacy-derived memories
Oldest source: 2025-11-03
Newest source: 2026-09-17
```

Re-running the same import is safe: raw rows dedupe on `(session_id, role, timestamp)` and curated
rows dedupe on a content hash, so the second run reports `deduped` instead of duplicating.

`openclaw` and `hindsight` are registered in the framework and report their capability honestly:
they raise a clear "what artifact format is still needed" message rather than importing nothing.

### After importing: start now, or rebuild

```
Start now                       -> existing data is queryable immediately.
Rebuild Hippocampus memories    -> regenerate QA / topic / observer layers from the raw source.
```

Rebuilding is budgeted and resumable — never a single unbounded burn:

```powershell
hippocampus rebuild --estimate                 # dry run: items, tokens, cost, and the basis
hippocampus rebuild --budget 5                 # stop cleanly at ¥5, checkpoint saved
hippocampus rebuild                            # resume from the checkpoint
```

Interrupting with Ctrl+C is safe: the checkpoint records the exact progress, and the next run
continues from it.

---

## 5. Cost

Measured against real usage, not a guess. The figures below are **reference numbers for the custom MiniMax-M3
memory-LLM configuration used in the cited measurement**, plus the stated embedding/rerank assumptions. They are
not a price promise for the SiliconFlow Qwen preset above; check the selected provider's current pricing.

| Tier | Turns/day | Memory LLM (reference) | Embedding | Rerank | **Total / month (reference)** |
|---|---:|---:|---:|---:|---:|
| Light | 20 | ¥15 | ¥0 (free tier) | ¥0 (free tier) | **≈ ¥15** |
| Regular | 100 | ¥76 | ¥0 | ¥0 | **≈ ¥76** |
| Heavy | 300 | ¥229 | ¥0 | ¥0 | **≈ ¥229** |

The price constants below are historical measurement inputs, not a current-provider guarantee. Verify the provider
pricing pages linked in [`docs/COST.md`](COST.md) before using them for a budget.

For the reference workload, the measured memory-only budget was **about ¥15–¥80/month**. Actual cost depends on
selected providers, model, pricing date, and traffic; verify before deployment.

---

## 6. Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| `docker: command not found` / `Docker Desktop is not running` | the database has nowhere to live | install/start Docker Desktop, re-run the installer |
| `port 55432 already in use` | something else owns the port | `hippocampus install --pg-port 55433` |
| `409 conflict`/`already exists` on the container name | a previous install is still present | re-run: the installer reuses a healthy container instead of duplicating it |
| `embedding: SKIPPED` / `memory LLM: SKIPPED` | no key was supplied | `hippocampus install --embed-key ... --llm-key ...` |
| doctor: `embedding_auth: fail auth_failed (HTTP 401)` | the key is wrong or was rotated | put the new key in the profile `.env` and re-run `hippocampus doctor --full` |
| doctor: `embedding_auth: fail rate_limited (HTTP 429)` | too many requests right now | wait, then re-run; the engine retries |
| `dimensions_consistency: fail` | config dim ≠ the vector column dim | re-create the database with the packaged schema, or align `storage.embed.dim` |
| memory does not come back after a restart | the agent may be using its built-in memory | run the independent restart-recall canary; `doctor --full` only gives an advisory recipe and does not prove cross-process recall |
| recall returns nothing at all | usually an empty corpus, not a bug | import something (§4), or write one memory and search again |

Every one of these has a machine-readable form: `hippocampus doctor --full` prints JSON with
`{id, status, detail, evidence}` per check.

---

## 7. Doctor — what is actually checked

`hippocampus doctor --full` (16 checks):

`db_reachable`, `pgvector_available`, `schema_version`, `migration_state`, `memory_llm_auth`,
`embedding_auth`, `rerank_auth`, `dimensions_consistency`, `hermes_provider_discovery`,
`hermes_home`, `write`, `read`, `vector_insert_search`, `rerank`, `recall`,
`restart_persistence_hint`.

* Auth checks make a **real** minimal request when a key is configured, and map the HTTP status to a
  distinct meaning (`401 auth_failed`, `402 quota_or_plan_limit`, `429 rate_limited`,
  `5xx upstream_error/unavailable`) — a wrong key can never look like a healthy install.
* The write check only runs when you pass `--writes`; without it, it reports `skip` rather than
  pretending.
* `restart_persistence_hint` is advisory: it counts recent `qa_pairs` rows and prints a manual restart-persistence
  recipe. It does not restart Hermes, write a probe row, or prove cross-process recall. The independent release
  Gate 1 canary is the evidence for that capability; Gate 2 separately proves the CI-built distribution path.

`hippocampus doctor --static` (the CI/packaging path from `docs/INSTALL.md`) still works unchanged.

---

## 8. Data location, backup, uninstall

**Data location**

| Item | Path (Windows) |
|---|---|
| engine config | `%USERPROFILE%\.v3-core\profiles\default\config.yaml` |
| secrets | `%USERPROFILE%\.v3-core\profiles\default\.env` (never in `config.yaml`) |
| runtime state (cursors, topic matrix, checkpoint) | same profile directory |
| memory data | inside the PostgreSQL container (`hippocampus-pg`), Docker volume |
| Hermes wiring | `%USERPROFILE%\AppData\Local\hermes\config.yaml` (`memory.provider: deep_memory_v3`) |

**Backup**

```powershell
hippocampus doctor --full > doctor.json                                    # what to expect
docker exec hippocampus-pg pg_dump -U postgres -d v3embeddings_alpha > backup.sql
# verify before trusting it:
docker exec -i hippocampus-pg psql -U postgres -d v3embeddings_alpha_verify < backup.sql
```

**Uninstall / rollback**

```powershell
irm https://raw.githubusercontent.com/yuzi001a/hippocampus-memory/main/install/uninstall.ps1 | iex
```

The uninstaller stops (and optionally removes) the container, restores the Hermes config backup it
recorded during install, and leaves your data dump alone unless you ask for it to be deleted. It
prints exactly what it removed and what it left behind.

---

## 9. Privacy — what leaves the machine

Everything is local by default: the database, the memory files, the runtime state.

| Data | Leaves the machine? | To whom |
|---|---|---|
| Your conversations and memories | **yes, when memory formation is enabled** — the same content the host agent already sends | your configured memory-LLM provider (for the `siliconflow` preset, SiliconFlow; for `custom`, the provider you chose) |
| Text sent for embedding | yes | your configured embedding provider (e.g. SiliconFlow) |
| Recall candidates for reranking | yes | your configured rerank provider |
| Anything at all, if you configure no providers | **no** — keyword recall and durable writes still work | — |

Notes:

* Endpoints, models and keys are **your** configuration; nothing is hard-coded to a vendor.
* A fully local setup (local embedding model, local LLM) is supported by configuration, and is the
  only way to keep every byte on the machine.
* The installer never sends your data anywhere for "telemetry" — there is no telemetry.

---

## 10. Where to go next

* `docs/CONFIGURATION.md` — every configuration key.
* `docs/COST.md` — the full cost calculation and its sources.
* `docs/ARCHITECTURE-OVERVIEW.md` — what the system does with your data.
* `docs/KNOWN-LIMITATIONS.md` — what this alpha does not do yet.
