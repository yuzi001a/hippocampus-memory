# Hippocampus

> **Give an agent a past.**

Hippocampus is a local-first, open-source long-term memory runtime for AI agents. It records durable conversation sources, recalls relevant past context in later sessions, and keeps the path back to the original source inspectable.

**Status:** Public alpha · active development. The current `main` includes automatic-memory adapters for **DeepSeek Harness (DSH)** and **pi**.

[简体中文](README.zh-CN.md) · [Install](docs/INSTALL.md) · [Documentation](docs/README.md) · [Current status](docs/STATUS.md) · [Why Hippocampus](docs/WHY_HIPPOCAMPUS.md)

---

## Why this exists

The first problem was simple: an agent can spend hours learning a project, then start a new session and lose much of that continuity.

The harder problem appeared later:

> **Forgetting is bad. Remembering the wrong thing can be worse.**

A useful memory system therefore cannot be only a transcript archive or a vector database. It needs durable sources, traceable recall, correction paths, rebuildable derived state, and a clear boundary between **what actually happened** and **what the system later inferred from it**.

Hippocampus follows one rule throughout the design:

> **Preserve the past. Keep its interpretation revisable.**

The longer design story is in [Why Hippocampus](docs/WHY_HIPPOCAMPUS.md).

---

## What works today

### Automatic memory in real agent hosts

| Host | Current support | Tested surface |
| --- | --- | --- |
| **DeepSeek Harness (DSH)** | **AUTO** — automatic user/assistant capture + current-turn automatic recall | Tested with `@deepseek-ai/dsh@0.2.0-rc.2` against the current upstream hook contract. Real host + B01 bridge + disposable PostgreSQL integration: **18/18 checks PASS**. |
| **pi** | **AUTO** — automatic persisted-message capture + one additive recall per user input | Tested with pi **0.99.2** / Node **24.21.0**. Real host load, isolated PostgreSQL ingest, recall, per-turn latch and source-trace resolution PASS. |
| **Hermes Agent** | Provider/tool integration | Existing `v3-hermes-plugin` integration with the public tool surface. See the plugin README for its narrower evidence boundary. |

Adapter details:

- [DSH adapter](packages/dsh-adapter/README.md)
- [pi adapter](packages/pi-adapter/README.md)
- [Hermes plugin](src/v3-hermes-plugin/README.md)

### Core behavior

The current core supports the pieces needed for those host integrations:

- durable conversation-source ingest;
- canonical event identity and idempotent replay;
- PostgreSQL + pgvector storage;
- keyword and optional vector recall;
- explicit-memory storage;
- exact source readback for recalled references;
- fresh bootstrap and backup/restore;
- fail-open host adapters — memory failure should not take the agent down;
- provider configuration that is local and opt-in.

The engine also contains observer, topic, yin/E1, journal and other derived-memory paths. Not every internal path is part of the current supported product surface. See [Current status](docs/STATUS.md) for the boundary.

---

## How it fits together

```text
        ┌─────────────────────────────┐
        │     Agent host / session    │
        │  DSH · pi · Hermes · later │
        └──────────────┬──────────────┘
                       │ host events / recall hook
                       ▼
        ┌─────────────────────────────┐
        │       Thin host adapter     │
        │ capture · recall · fail-open│
        └──────────────┬──────────────┘
                       │ B01 bridge contract
                       ▼
        ┌─────────────────────────────┐
        │          v3-core            │
        │ ingest · recall · provenance│
        └──────────────┬──────────────┘
                       │
              ┌────────┴────────┐
              ▼                 ▼
       durable sources      derived memory
       conversation         embeddings/topics/
       explicit memory      notes/indexes
              │                 │
              └───────┬─────────┘
                      ▼
              PostgreSQL/pgvector
```

The key architectural distinction is deliberate:

- **Sources are durable.** Raw conversation events and canonical explicit memories should survive.
- **Derived memory is revisable.** Embeddings, topics, summaries and higher-level interpretations may be rebuilt as models and algorithms improve.
- **Recall stays traceable.** A retrieved claim should be able to resolve back toward stored evidence instead of becoming an uninspectable summary.

---

## Quick start

Hippocampus is still an alpha, so the supported setup is intentionally explicit rather than a one-line installer.

1. Install and bootstrap the core using [docs/INSTALL.md](docs/INSTALL.md).
2. Configure storage/providers using [docs/CONFIGURATION.md](docs/CONFIGURATION.md).
3. Choose an agent integration:
   - [DeepSeek Harness](packages/dsh-adapter/README.md)
   - [pi](packages/pi-adapter/README.md)
   - [Hermes Agent](src/v3-hermes-plugin/README.md)
4. Verify the backend/adapter status before using it on important work.
5. Read [Known limitations](docs/KNOWN-LIMITATIONS.md) before treating the alpha as production infrastructure.

For backup and upgrades:

- [Backup & restore](docs/BACKUP-RESTORE.md)
- [Upgrade guide](docs/UPGRADE.md)

---

## What “automatic memory” means here

For the current DSH and pi adapters, the intended user experience is:

```text
Session A
  You discuss a decision, fact or project detail.
  Hippocampus records the host's real persisted message identity.

New session
  You ask about that past detail.
  The adapter triggers recall automatically.
  Relevant memory is added to the current request.
  The source reference remains resolvable.
```

No manual `remember` or `search` step is required for the normal path.

The adapters are intentionally thin. Host lifecycle logic stays in the adapter; durable memory rules stay in the core.

---

## Reliability principles

Hippocampus has accumulated a lot of reliability work because memory errors compound over time. The current project rules are:

- **Source first.** Do not truncate or replace the authoritative raw record to satisfy an embedding/index limit.
- **Durability before derivation.** A source write and an embedding/summary result are not the same success condition.
- **Canonical identity.** Host/session/event identity is the durable replay boundary.
- **Fail open at the host boundary.** If memory is unavailable, the agent should keep working.
- **Fail closed on isolation.** Test/recovery environments must not silently fall back to production paths.
- **Evidence before claims.** A code path existing in the repository is not enough to call it supported.

Engineering details live under [docs/reliability](docs/reliability/) and the milestone/evidence documents linked from the [documentation index](docs/README.md).

---

## Evaluation

Hippocampus has been tested on the LoCoMo long-context memory benchmark as a research signal, not as a claim that benchmark score equals real-world memory quality.

The historical evaluation track includes:

- Hippocampus: **1077.5 / 1540 = 69.97%**
- no-long-term-memory baseline: **6.85%**
- gold-context reference: **75.84%**

See [evaluation/locomo-recall-v2.md](docs/evaluation/locomo-recall-v2.md) for the protocol and caveats.

---

## Current limitations

This is still a public alpha. In particular:

- the project is **not** claiming production readiness;
- current first-class adapter evidence is Windows-heavy;
- the pi model-backed A→B conversation remains an optional smoke that has not been run on the dedicated test machine because no model credential is configured there;
- DSH model integration was structurally tested with a local credential-free model stub; the host/bridge/recall path is the evidence-backed part;
- text is the primary supported memory content in the current host adapters;
- memory management/correction and temporal update UX are the next product focus;
- package publication and version guarantees are still narrower than the source tree.

The maintained snapshot is [docs/STATUS.md](docs/STATUS.md). Older public-alpha acceptance documents are retained as engineering history and should not be read as the current product summary.

---

## Documentation

Start with [docs/README.md](docs/README.md). The main paths are:

| Need | Document |
| --- | --- |
| Install | [INSTALL.md](docs/INSTALL.md) |
| Configure | [CONFIGURATION.md](docs/CONFIGURATION.md) |
| Current support / tested versions | [STATUS.md](docs/STATUS.md) |
| Why this project exists | [WHY_HIPPOCAMPUS.md](docs/WHY_HIPPOCAMPUS.md) |
| Architecture | [ARCHITECTURE-OVERVIEW.md](docs/ARCHITECTURE-OVERVIEW.md) |
| Privacy / data flow | [PRIVACY-DATA-FLOW.md](docs/PRIVACY-DATA-FLOW.md) |
| Compare approaches | [COMPARISON.md](docs/COMPARISON.md) |
| Known limitations | [KNOWN-LIMITATIONS.md](docs/KNOWN-LIMITATIONS.md) |
| Backup / restore | [BACKUP-RESTORE.md](docs/BACKUP-RESTORE.md) |
| Upgrade | [UPGRADE.md](docs/UPGRADE.md) |

---

## Project direction

The host-adapter phase is now substantially complete for DSH and pi. The next development focus is the memory product itself:

- make stored memories easier to inspect and manage;
- make corrections and changed facts first-class;
- improve temporal reasoning about “old truth” vs “current truth”;
- keep source provenance visible while derived memory evolves;
- then expand portability and additional host integrations based on real usage.

See [docs/STATUS.md](docs/STATUS.md) for the maintained current snapshot.

---

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before changing storage, source identity, recall or host-adapter contracts.

Security issues should follow [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
