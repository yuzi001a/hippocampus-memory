# Current status

_Last updated: 2026-10-03_

This page is the maintained public snapshot of what Hippocampus currently supports. It intentionally separates **current product capability** from older milestone/evidence documents retained in the repository.

## Project stage

Hippocampus is a **public alpha**. It is not presented as production-ready infrastructure.

The project has completed its first major host-adapter phase:

- core install/bootstrap and source durability;
- a thin shared bridge contract;
- source-level idempotency;
- automatic memory for pi;
- automatic memory for DeepSeek Harness (DSH);
- source-trace closure for recalled QA references;
- one-click import of existing history (Hermes / DSH / pi / memory-md).

The one-click history import milestone has landed — see [`docs/IMPORT.md`](IMPORT.md) and `evidence/i01-history-import-20261003/` (isolated integration, 42/42 checks PASS). The main line now returns to memory inspection, correction, changed facts and temporal reasoning.

## Supported host surface

| Host | Support | Tested version / evidence | Notes |
| --- | --- | --- | --- |
| DeepSeek Harness (DSH) | **AUTO** | `@deepseek-ai/dsh@0.2.0-rc.2`, upstream `639ed015397290b3745d163aafe02ffee4aa3f84`; 39 adapter unit tests plus real-host isolated integration with **18/18 checks PASS** | Automatic user/assistant capture, current-turn recall through `agent/pre-step`, once-per-turn latch, self-recapture exclusion, fail-open. |
| pi | **AUTO** | pi `0.99.2` (adapter), `1.0.0` (session format, import); Node `24.21.0`; real host load + isolated PostgreSQL ingest/recall/source-trace PASS | Automatic persisted-message capture and one additive recall per user input; historical sessions importable — see [`docs/IMPORT.md`](IMPORT.md). |
| Hermes Agent | Provider / tools | Existing `v3-hermes-plugin` contract and 13-tool public surface | Existing integration; its public evidence boundary is older and narrower than the current DSH/pi automatic-memory acceptance. |

### DSH

Current adapter: [packages/dsh-adapter](../packages/dsh-adapter/README.md)

Evidence-backed behavior:

- reads the current upstream lifecycle rather than an old local install;
- captures native DSH message/session identities;
- recalls on the current turn and injects into the same request;
- does not recapture its own injected memory;
- preserves downstream host decisions;
- fails open if the memory backend is unavailable;
- durable dedupe remains the core B01/F2 `(host, session_id, event_id)` identity.

Real-host isolated integration finished with **18 checks / 0 failures** on the final archived driver.

The model endpoint in that integration was a local credential-free stub. This validates the host/adapter/bridge/recall contract; it is not a claim about any particular external model provider.

### pi

Current adapter: [packages/pi-adapter](../packages/pi-adapter/README.md)

Evidence-backed behavior:

- package builds and loads in a real pi 0.99.2 host;
- persisted user/assistant messages are captured with native identities;
- recall is automatic and limited to once per user input;
- injection is additive and does not replace host context;
- recalled QA references resolve through the core source-read path;
- isolated PostgreSQL persistence, recall and source trace pass.

A live-model A→B conversation is an optional smoke and has **not** been run on the dedicated test laptop because no model credential is configured there. It is not treated as a code-acceptance blocker.

## Core status

Current evidence-backed core behavior includes:

- PostgreSQL/pgvector-backed source storage;
- fresh bootstrap;
- source event ingest;
- source-level idempotent replay;
- explicit-memory canonical writes;
- keyword recall and optional embedding/vector paths;
- source trace from recall reference back to stored evidence;
- backup/restore;
- fail-closed test isolation fixes;
- host-facing fail-open behavior.

The repository contains additional observer/topic/yin/E1/journal and research paths. Their presence does **not** automatically make them part of the supported product surface.

## Reliability rules that are now part of the product contract

1. **Raw/source memory is authoritative.** Derived indexes may be rebuilt; the source should not be truncated to satisfy an embedding limit.
2. **Durability and derivation are separate outcomes.** A source write can succeed even when an optional embedding/provider step fails.
3. **Canonical event identity is host + session + event.** Retry/reload must not create a second durable source for the same event.
4. **Host integration fails open.** Memory failure must not make the host agent unusable.
5. **Destructive tests fail closed on isolation.** Missing test configuration must never fall back to production.
6. **Recall references must resolve.** The system should not emit a source reference that its own read path cannot open.

## Current limitations

- Public alpha; no production-readiness claim.
- First-party acceptance is still Windows-heavy.
- Current DSH/pi adapters are text-first.
- pi live-model A→B smoke has not been executed on the dedicated test machine.
- DSH live integration used a credential-free model stub.
- Minimum compatible DSH version is not claimed; the current tested target is `0.2.0-rc.2`.
- The pi package is still a local package workflow rather than a broad registry-distribution promise.
- Not every internal memory derivation path has the same acceptance depth as source ingest and host adapters.
- The history-import acceptance run used synthetic Hermes content (on the real schema) plus real DSH/pi session files; a full real-history import on an end-user machine has not been executed yet.
- Multi-host portability beyond the current adapters remains a later expansion area.

See [KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md) for the longer engineering inventory.

## Current development direction

The host-adapter milestone is complete enough to stop making integrations the default priority.

**One-click history import has landed**: `hippocampus import auto` automatically discovers supported local agent history, previews it safely (`--dry-run`), imports it idempotently, and makes imported history usable by recall — see [`docs/IMPORT.md`](IMPORT.md) and `evidence/i01-history-import-20261003/`.

The next product work focuses on:

- **memory inspection** — users should be able to see what the system believes it remembers;
- **memory correction** — a later correction should not leave an obsolete fact silently dominant;
- **temporal state** — distinguish “was true then” from “is true now”;
- **management UX** — make memory status, provenance and updates understandable without reading database rows;
- **real usage feedback** — let product use decide which deeper recall/reasoning work matters next.

The one-click import milestone was intentionally placed before M01/M02 because continuity for existing users is a product prerequisite, not a later convenience.

## Research signal

Historical LoCoMo evaluation:

- Hippocampus: **1077.5 / 1540 = 69.97%**
- no-long-term-memory baseline: **6.85%**
- gold-context reference: **75.84%**

See [evaluation/locomo-recall-v2.md](evaluation/locomo-recall-v2.md) for protocol details and caveats.

## Historical documents

Older files such as [PUBLIC_ALPHA_SUPPORTED_SURFACE.md](PUBLIC_ALPHA_SUPPORTED_SURFACE.md), B01/F2/F3 milestone documents and integration evidence are intentionally kept for traceability.

They answer **“how did we establish this?”**

This page answers **“what do we support now?”**
