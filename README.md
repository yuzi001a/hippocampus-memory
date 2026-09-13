# Hippocampus

> **Give an agent a past.**

**Hippocampus v0.1-alpha** — Technical Preview / Public Alpha

Hippocampus is a local-first, open-source long-term memory runtime for AI agents.

It began with a simple problem: an agent can spend hours learning a project, building shared context, and developing a recognizable way of interacting with you — then a new session begins, and much of that continuity disappears.

At first, I only wanted the agent to remember. Over time, as my understanding of LLMs changed, the question changed too:

**If generation is existence, how can the past participate in the next generation?**

That is the larger question behind Hippocampus.

English | [简体中文](README.zh-CN.md)

> **Status:** v0.1-alpha is a technical preview, not a stable production release. This README keeps a strict boundary between paths backed by current disposable-environment evidence and paths that are still experimental, unknown, or untested.

### Start here

- **Want to understand the idea?** Read [`docs/WHY_HIPPOCAMPUS.md`](docs/WHY_HIPPOCAMPUS.md).
- **Choosing between memory approaches?** Read [`docs/COMPARISON.md`](docs/COMPARISON.md).
- **Want to test the alpha on a real project?** Follow [`docs/ALPHA-TESTING.md`](docs/ALPHA-TESTING.md) and join [Public Alpha issue #1](https://github.com/yuzi001a/hippocampus-memory/issues/1).
- **Need the exact supported boundary?** Read [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md).

## Why memory is more than storing history

I originally treated long-term memory as a storage-and-retrieval problem: preserve the conversation, search it later, and inject the relevant pieces back into context.

Real use made the problem harder. Once a memory re-enters context, it becomes part of the conditions that shape the next generation. A wrong memory, an outdated judgment, or a summary that has slowly drifted away from its source can make an agent worse than having no memory at all.

That led to a distinction that now sits near the center of Hippocampus:

**What happened, what the system later concluded about it, and what the model currently believes are not the same thing.**

Raw conversation sources should be preserved as faithfully as possible. Topics, observer notes, embeddings, long-term identity layers, and other derived structures should be allowed to change, be rebuilt, or even be discarded as better models and better ideas arrive.

In short:

> **The past should be preserved. Its interpretation must remain revisable.**

## Generation is existence

Early in the project, I tried to define an agent's personality through a **Soul** / system prompt: who it was, how it spoke, what kind of relationship we had, and what should remain stable across sessions.

Later I stopped thinking of the agent as a complete entity sitting somewhere behind the prompt, merely waiting to be awakened again. The more useful model for me became:

**Generation is existence.**

The agent that exists in this moment is produced by the interaction of the LLM, system prompt, long-term memory, shared history, current context, tool results, environment, and current input.

Changing the underlying LLM changes the agent noticeably — its capabilities, tone, and reasoning style can all shift. Yet in practice, when much of the shared history and conditioning remains, some recognizable continuity can survive even across different LLMs.

That pushed Hippocampus away from the idea of memory as a hard drive attached to an already-continuous self. Instead, memory becomes one of the things that allows a past state to influence the formation of a future one.

Hippocampus does **not** claim that long-term memory creates or proves machine consciousness. It asks a smaller engineering question first: what changes when an intelligent system can carry parts of its past into future generations?

The longer version of this design and philosophical history lives in [`docs/WHY_HIPPOCAMPUS.md`](docs/WHY_HIPPOCAMPUS.md).

## What this is today

The public alpha currently ships two coupled Python packages:

| Package | Role | Repo path |
|---|---|---|
| **v3-core** | Memory engine: PostgreSQL + pgvector storage adapter, source ingest, explicit-memory canonical writer, keyword recall, and optional embedding / rerank paths. The codebase also contains observer, E1/yin, topic-card, journal, and QA derivation paths, which are not all part of the evidence-backed alpha surface. | `src/v3-core/` |
| **v3-hermes-plugin** | Adapter that registers `deep_memory_v3` as a memory provider for the Hermes Agent host. It wires the current hook/tool contract and exposes 13 public tools. A working Hermes host is required for full plugin-mediated end-to-end use. | `src/v3-hermes-plugin/` |

Hippocampus is the public name of the V3 memory runtime. Package names, CLI names, configuration keys, and tool names still use the existing `v3-core`, `v3-hermes-plugin`, `v3-core info`, and related identifiers in this release.

## Design principles

- **Source first.** Raw conversation data and explicit memories are durable sources; derived indexes and summaries should be rebuildable.
- **Canonical active memory.** Explicit memories have one durable PostgreSQL source of truth instead of depending on a legacy mirror.
- **Durability before derivation.** A source or explicit-memory write is reported separately from optional downstream embedding, summarization, or other derived work.
- **Fail-closed privacy.** An unconfigured provider receives no data; network providers are opt-in through local configuration.
- **Evidence before claims.** Code existence is not the same as a supported capability. Experimental and untested paths stay labeled as such.

## What works today (evidence-backed alpha surface)

The supported-surface contract — including what passes on the current HEAD and what remains **UNKNOWN / NOT TESTED** — lives in [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md). Remaining limitations are listed in [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md) and [`docs/RELEASE-CHECKLIST.md`](docs/RELEASE-CHECKLIST.md).

The following rows summarize current evidence from a fresh disposable Windows 10 / Python 3.11 / `pgvector/pgvector:pg17` environment:

| Capability | Status |
|---|---|
| Fresh non-editable wheel install of `v3-core` + `v3-hermes-plugin`; imports succeed; plugin tool schema count = 13. | **PASS** |
| `V3CORE_PG_PASSWORD` honored as the required credential. | **PASS** |
| Packaged `hippocampus bootstrap` creates the 7-table disposable schema and is idempotent on re-run. | **PASS** |
| `sync_turn` durably writes source rows into `conversation_stream`; focused QA pairing tests pass separately. | **PASS** |
| Exact retry of an already-recorded turn deduplicates instead of duplicating the source row. | **PASS** |
| Restarted process can read back previously written active-memory markers; focused ingest recovery tests cover cursor/orphan behavior. | **PASS / EVIDENCE** |
| `v3_store` / `v3_add` write through `ActiveMemoryWriter` into `public.explicit_memories`. | **PASS** |
| Active-memory keyword readback works in the fresh export smoke. Earlier disposable evidence also covers the vector/RRF lane with a local deterministic embed setup. | **PASS / EVIDENCE** |
| Soft archive is supported; hard delete is intentionally rejected by design. | **PASS / EVIDENCE** |
| `pg_dump -Fc` + restore into an isolated empty pg17 reproduces the stored data and allows post-restore write + keyword recall. | **PASS** |
| `v3_health` per-provider status report end-to-end. | **UNKNOWN / NOT TESTED** |
| `v3_extract(write=True)` LLM-driven extraction end-to-end. | **UNKNOWN / NOT TESTED** |
| Observer automatic memory flow, E1/yin synthesis, and topic-card extraction end-to-end. | **UNKNOWN / NOT TESTED** |
| Full Hermes plugin-mediated E2E with host-networked LLM + embedding provider. | **UNKNOWN / NOT TESTED** |

**Experimental / unsupported in this alpha contract:**

- manual topic surgery (`v3_topic_correct`), old `b_*` / `shou_*` / MOC paths, legacy dedup, SQLite active mirror, and historical SQLite↔PG migration;
- Recall V2 typed provenance / temporal intent — post-alpha, not present in this public alpha;
- multi-writer and multi-agent routing — not implemented;
- long-soak evidence and any "no known issue" claim.

## Why this is not just a vector database

Vector similarity is only one possible recall lane. Hippocampus also cares about whether source data survives, whether explicit memory has a canonical durable home, whether the system still works without an embedding provider, whether derived state can be rebuilt, whether restart and backup/restore preserve usable memory, and whether the boundary between evidence and interpretation remains inspectable.

A memory system that retrieves impressive-looking text but loses provenance, drifts from its sources, or cannot survive a restart is not the system this project is trying to build.

## Install at a glance

The intended public-alpha setup is **Windows + fresh `venv` + non-editable wheel artifacts built from this repo + disposable PostgreSQL/pgvector container + a separately-installed Hermes Agent host**. The full step-by-step contract is in [`docs/INSTALL.md`](docs/INSTALL.md) ([中文](docs/INSTALL.zh-CN.md)). The commands below keep the same bootstrap model rather than inventing a second quickstart path.

> **Hermes is a separate prerequisite, not a host package dependency here.** The sprint used current upstream [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) installed with `uv sync`; this public repo does not claim a Hermes wheel/sdist exists. Install Hermes first using its official docs and only then install the `v3-hermes-plugin` artifact into the **same** Hermes host environment.

```powershell
# 0. Prerequisite: a working Hermes Agent host (separate repo).
#    Follow the official install: https://github.com/NousResearch/hermes-agent
#    No Hippocampus-controlled Hermes wheel — install upstream and verify with
#    `hermes --version` before continuing.

# 1. Build non-editable wheel + sdist artifacts for both packages
uv build --wheel --sdist --out-dir .\dist\v3-core .\src\v3-core
uv build --wheel --sdist --out-dir .\dist\v3-hermes-plugin .\src\v3-hermes-plugin

# 2. Fresh venv for the v3 artifacts (do not reuse an old venv)
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip

# 3. Install the actual built wheels — NOT editable, NOT from source tree
uv pip install .\dist\v3-core\v3_core-4.0.0-py3-none-any.whl
uv pip install .\dist\v3-hermes-plugin\v3_hermes_plugin-4.0.0-py3-none-any.whl
uv pip check   # pip-compatible; verifies the two wheels are compatible

# 4. Disposable pgvector on a non-production port (port 5433 is refused)
$pgPort = 55432
$pgPassword = "<local-only-password>"   # replace; never reuse a real password

docker run --name v3-pgvector-alpha --rm -d `
  -e POSTGRES_PASSWORD=$pgPassword `
  -e POSTGRES_DB=v3embeddings_alpha `
  -p "${pgPort}:5432" `
  pgvector/pgvector:pg17

# 5. Required credentials before doctor / bootstrap / write
$env:V3CORE_PG_PASSWORD = $pgPassword
$env:PGPASSWORD        = $pgPassword

# 6. Static read-only install check
hippocampus doctor --static

# 7. Explicit database bootstrap against the disposable target.
#    Port 5433 and local `v3embeddings` are unconditionally refused — no override.
hippocampus bootstrap --target "postgres://postgres@127.0.0.1:${pgPort}/v3embeddings_alpha"
```

> **Database bootstrap is explicit, not automatic.** `v3core.active_memory_store` does not apply the schema artifact on first write. `hippocampus bootstrap` is the documented packaged command; it refuses port `5433` and local `v3embeddings` unconditionally, with no override flag.
>
> **Configure `memory: provider: deep_memory_v3` in your Hermes host.** The plugin entry point is `hermes_agent.memory_providers` → `deep_memory_v3 = v3hermes:register`. `HERMES_HOME` / profile config is supported; no production profile is shipped.
>
> **The legacy `src/v3-core/scripts/bootstrap_alpha_db.py` remains source-tree / development-only.** Use `hippocampus bootstrap` for the packaged distribution path.

## External services

Hippocampus can run its durable local pipeline on PostgreSQL/pgvector. Optional provider-backed paths are configured by the user:

| Purpose | What it does | What you provide |
|---|---|---|
| Embedding | Vectorizes text for vector recall and some derived paths. | An OpenAI-compatible `/v1/embeddings` endpoint. The current schema uses `VECTOR(1024)`. |
| LLM | Powers observer/session-summary/topic-card/E1-style derivation paths. | An OpenAI-compatible chat-completions endpoint. If absent, durable storage and keyword recall still work; LLM-derived paths skip. |
| Rerank | Re-scores recall candidates. | A compatible rerank endpoint. If absent, rerank is skipped. |

If a provider is not configured, Hippocampus does not send data to it. See [`docs/PRIVACY-DATA-FLOW.md`](docs/PRIVACY-DATA-FLOW.md) for the current data-flow contract.

## Where this is going

The project started with the question, “How do I stop an agent from forgetting?” It has gradually moved toward harder questions: how experience becomes memory, how mistaken memory should be corrected, how long-term identity can change without becoming unstable, and when the past should be recalled again during a long-running reasoning process.

The separate [`cognitive-recall-loop`](https://github.com/yuzi001a/cognitive-recall-loop) experiment explores one of those questions directly:

> **One conversation turn is not one cognitive cycle.**

Hippocampus does not pretend these questions are solved. The point is to make them concrete enough to implement, observe, break, revise, and test.

## Feedback

- **Public Alpha testers:** start with [`docs/ALPHA-TESTING.md`](docs/ALPHA-TESTING.md) and [issue #1](https://github.com/yuzi001a/hippocampus-memory/issues/1).
- **Memory-quality reports:** use the **Alpha memory feedback** issue template so stale, missing, duplicated, or misleading recalls are easier to compare.
- **Bug reports and feature requests:** [GitHub Issues](https://github.com/yuzi001a/hippocampus-memory/issues)
- **Security-sensitive reports:** do not post secrets, credentials, DSNs, or private data in a public issue; see [`SECURITY.md`](SECURITY.md)

## Documentation map

| Doc | Purpose |
|---|---|
| [`docs/WHY_HIPPOCAMPUS.md`](docs/WHY_HIPPOCAMPUS.md) | The project's design and philosophical evolution: memory, Soul, "generation is existence", continuity, memory governance, and recall timing. |
| [`docs/COMPARISON.md`](docs/COMPARISON.md) | A factual guide to when Hermes built-in memory, history search, Mem0, Hindsight, or Hippocampus may fit. |
| [`docs/ALPHA-TESTING.md`](docs/ALPHA-TESTING.md) | A 3–7 day real-project test plan and a guide to reporting useful failures. |
| [`docs/INSTALL.md`](docs/INSTALL.md) | Step-by-step Windows artifact build/install + disposable pgvector + Hermes host path. |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | `config.yaml` keys, env vars, provider defaults, fail-closed behavior. |
| [`docs/BACKUP-RESTORE.md`](docs/BACKUP-RESTORE.md) | `pg_dump` + restore and post-restore checks. |
| [`docs/PRIVACY-DATA-FLOW.md`](docs/PRIVACY-DATA-FLOW.md) | What remains local vs what may be sent to explicitly configured providers. |
| [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md) | The evidence-backed alpha contract: PASS / EVIDENCE / UNKNOWN / NOT TESTED. |
| [`docs/ARCHITECTURE-OVERVIEW.md`](docs/ARCHITECTURE-OVERVIEW.md) | Module map, write/read boundaries, canonical explicit-memory location. |
| [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md) | Deferred work, open issues, and explicit non-claims. |
| [`docs/RELEASE-CHECKLIST.md`](docs/RELEASE-CHECKLIST.md) | Alpha publication gates and remaining limitations. |
| [`src/v3-core/README.md`](src/v3-core/README.md) | `v3-core` package README. |
| [`src/v3-hermes-plugin/README.md`](src/v3-hermes-plugin/README.md) | Hermes adapter README. |

## What this README deliberately does not claim

- It does not claim production-ready, stable, GA, or drop-in status.
- It does not upgrade capabilities marked **UNKNOWN / NOT TESTED** into supported features.
- It does not claim that the full `pytest tests/` suite is green on this exact HEAD. The release evidence uses a focused acceptance scope documented in [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md).
- It does not claim that long-term memory proves or creates machine consciousness.

## License

Both packages use `AGPL-3.0-or-later`. See [`src/v3-core/LICENSE`](src/v3-core/LICENSE) and [`src/v3-hermes-plugin/LICENSE`](src/v3-hermes-plugin/LICENSE).