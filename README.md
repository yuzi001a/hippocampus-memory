<div align="center">

# Hippocampus

### Give an agent a past.

**Long-term memory for AI agents — local-first, source-traceable, and built to survive new sessions.**

[![Status](https://img.shields.io/badge/status-public%20alpha-orange)](docs/STATUS.md)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![DSH](https://img.shields.io/badge/DeepSeek%20Harness-auto%20memory-success)](packages/dsh-adapter/README.md)
[![pi](https://img.shields.io/badge/pi-auto%20memory-success)](packages/pi-adapter/README.md)

[Get started](docs/INSTALL.md) · [中文](README.zh-CN.md) · [How it works](docs/ARCHITECTURE-OVERVIEW.md) · [Current status](docs/STATUS.md)

</div>

---

## Your agent should not wake up as a stranger

You can spend days working with an agent. It learns the project, the decisions you made, the way you like things done.

Then a new session starts.

The context is gone.

Hippocampus gives agents a durable past. It records what actually happened, brings relevant memories into later sessions automatically, and keeps those memories traceable back to their source.

```text
Session A
You: The internal codename is "North Star".

        ↓  Hippocampus remembers

New session
You: What was the codename again?

Agent: North Star.
       ↳ recalled from the earlier conversation
```

For supported automatic adapters, you do **not** need to manually call `remember` or `search`.

---

## What makes Hippocampus different

### 🧠 Memory that follows the agent across sessions

Hippocampus can automatically capture real user/assistant messages and recall relevant history when a later conversation needs it.

### 🔎 A memory can point back to what really happened

A remembered claim should not become an orphaned summary. Hippocampus keeps source provenance so recalled material can be traced back toward the original conversation.

### ♻️ The past is durable; its interpretation can change

Raw history is treated differently from embeddings, topics, notes, and summaries.

**Preserve the source. Rebuild the interpretation.**

That matters because forgetting is inconvenient — but confidently remembering the wrong thing can be worse.

### 🏠 Local-first by design

Your durable memory store lives under your control. External embedding, rerank, or LLM providers are optional and explicitly configured.

### 🧩 Built for more than one agent

The memory engine is separate from the host integration. Thin adapters connect agent runtimes to the same memory core.

---

## Works with

| Agent host | Experience today |
| --- | --- |
| **DeepSeek Harness (DSH)** | **Automatic memory.** Captures user/assistant messages and recalls relevant memory into the current turn. |
| **pi** | **Automatic memory.** Captures persisted messages and recalls once per user input. |
| **Hermes Agent** | Existing provider/tool integration through `v3-hermes-plugin`. |

The current tested versions and exact evidence boundaries live in [STATUS.md](docs/STATUS.md).

---

## Bring your existing history

A memory system is much less useful if it only starts remembering the day you install it.

Hippocampus already contains import support for:

- **Hermes history** — `state.db`, JSONL and JSON exports;
- **curated memory files** — `MEMORY.md`, `USER.md`, `SOUL.md`, `AGENTS.md` and other Markdown notes.

A broader **one-click history migration** flow — automatic discovery plus DSH/pi history import — is the next installation milestone.

---

## The idea in one picture

```text
              your agent
          DSH · pi · Hermes
                 │
        real conversation events
                 │
                 ▼
          ┌───────────────┐
          │  Hippocampus  │
          │               │
          │  remember     │
          │  retrieve     │
          │  trace source │
          └───────┬───────┘
                  │
          ┌───────┴────────┐
          │                │
          ▼                ▼
    durable sources   revisable memory
    what happened     what it may mean
          │                │
          └───────┬────────┘
                  ▼
           future sessions
```

The important split is simple:

> **What happened, what the system later concluded, and what the model believes right now are not the same thing.**

That distinction is the foundation of the project.

---

## Try it

Hippocampus is currently a **public alpha**. The install path is explicit rather than polished into a one-line installer yet.

**1. Install the core**

Follow [the installation guide](docs/INSTALL.md).

**2. Pick your agent**

- [DeepSeek Harness adapter](packages/dsh-adapter/README.md)
- [pi adapter](packages/pi-adapter/README.md)
- [Hermes plugin](src/v3-hermes-plugin/README.md)

**3. Start a new session later and ask about something from the past**

If the relevant memory is recalled, its source remains inspectable.

For configuration, backup and upgrades:

[Configuration](docs/CONFIGURATION.md) · [Backup & restore](docs/BACKUP-RESTORE.md) · [Upgrade](docs/UPGRADE.md)

---

## Why not just use a vector database?

Because long-term memory has more failure modes than “similarity search returned the wrong chunk.”

A useful memory layer has to care about:

- whether the original history survived;
- whether retries created duplicates;
- whether old facts were superseded;
- whether a summary drifted away from its source;
- whether memory can be rebuilt after models change;
- whether the agent still works when the memory backend is unavailable.

Hippocampus treats embeddings as one retrieval tool, not as the definition of memory.

---

## Built from real long-running use

This project grew out of using agents on ongoing work where continuity mattered.

That experience changed the original goal from:

> “make the agent remember more”

to:

> **“make the agent remember without losing the difference between memory and evidence.”**

That is also why the next phase is focused less on adding another retrieval trick and more on **memory inspection, correction, changed facts, and time**.

Read the longer story in [Why Hippocampus](docs/WHY_HIPPOCAMPUS.md).

---

## Where the project is now

**Done:** durable core · install/bootstrap · shared bridge · pi automatic memory · DSH automatic memory · source trace · reliability hardening.

**Now:** one-click import of existing history.

**Next:** memory inspection and correction, then temporal memory — understanding that “true once” does not always mean “true now.”

See [Current status](docs/STATUS.md) for the maintained technical snapshot.

---

## Evaluation

Hippocampus has also been evaluated on LoCoMo as a research signal:

| Setup | Score |
| --- | ---: |
| Hippocampus | **69.97%** |
| No long-term memory | **6.85%** |
| Gold-context reference | **75.84%** |

Benchmark score is not treated as a substitute for real product behavior. Protocol and caveats: [LoCoMo evaluation](docs/evaluation/locomo-recall-v2.md).

---

## Documentation

**For users:** [Install](docs/INSTALL.md) · [Configuration](docs/CONFIGURATION.md) · [Status](docs/STATUS.md) · [Known limitations](docs/KNOWN-LIMITATIONS.md)

**For builders:** [Architecture](docs/ARCHITECTURE-OVERVIEW.md) · [Privacy/data flow](docs/PRIVACY-DATA-FLOW.md) · [Documentation index](docs/README.md)

**For the curious:** [Why Hippocampus](docs/WHY_HIPPOCAMPUS.md) · [Comparison](docs/COMPARISON.md)

---

## Public alpha

Hippocampus is under active development. Current first-party integration evidence is still Windows-heavy, package/distribution UX is being simplified, and not every internal experimental memory path is part of the supported surface.

Claims on this page are intentionally narrower than “everything the repository can theoretically do.”

---

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports should follow [SECURITY.md](SECURITY.md).

MIT licensed — see [LICENSE](LICENSE).

<div align="center">

**Give an agent a past — without asking it to forget where that past came from.**

</div>
