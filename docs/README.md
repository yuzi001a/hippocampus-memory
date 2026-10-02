# Hippocampus documentation

This directory contains both current product documentation and retained engineering history.

If you are new to the project, do **not** start with milestone files such as `B01-*`, `F2-*`, or the reliability evidence documents. Start with the user-facing path below.

## Start here

| Goal | Read |
| --- | --- |
| Understand what Hippocampus is | [WHY_HIPPOCAMPUS.md](WHY_HIPPOCAMPUS.md) |
| See what is supported today | [STATUS.md](STATUS.md) |
| Install the core | [INSTALL.md](INSTALL.md) / [INSTALL.zh-CN.md](INSTALL.zh-CN.md) |
| Configure storage and providers | [CONFIGURATION.md](CONFIGURATION.md) |
| Connect DeepSeek Harness | [../packages/dsh-adapter/README.md](../packages/dsh-adapter/README.md) |
| Connect pi | [../packages/pi-adapter/README.md](../packages/pi-adapter/README.md) |
| Connect Hermes Agent | [../src/v3-hermes-plugin/README.md](../src/v3-hermes-plugin/README.md) |
| Understand architecture | [ARCHITECTURE-OVERVIEW.md](ARCHITECTURE-OVERVIEW.md) |
| Review privacy / data flow | [PRIVACY-DATA-FLOW.md](PRIVACY-DATA-FLOW.md) |
| Back up or restore | [BACKUP-RESTORE.md](BACKUP-RESTORE.md) |
| Upgrade | [UPGRADE.md](UPGRADE.md) |
| Check current limitations | [KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md) |

## Product and design

- [WHY_HIPPOCAMPUS.md](WHY_HIPPOCAMPUS.md) — project motivation and design philosophy.
- [ARCHITECTURE-OVERVIEW.md](ARCHITECTURE-OVERVIEW.md) — core architecture.
- [COMPARISON.md](COMPARISON.md) / [COMPARISON.zh-CN.md](COMPARISON.zh-CN.md) — how Hippocampus differs from other memory approaches.
- [COST.md](COST.md) — provider/cost considerations.
- [PRIVACY-DATA-FLOW.md](PRIVACY-DATA-FLOW.md) — what can leave the machine and under which configuration.
- [KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md) — limitations and evidence boundaries.
- [STATUS.md](STATUS.md) — maintained snapshot of supported hosts, tested versions and current direction.

## Installation and operation

- [INSTALL.md](INSTALL.md) / [INSTALL.zh-CN.md](INSTALL.zh-CN.md)
- [CONFIGURATION.md](CONFIGURATION.md)
- [UPGRADE.md](UPGRADE.md)
- [BACKUP-RESTORE.md](BACKUP-RESTORE.md)
- [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md)
- [ALPHA-TESTING.md](ALPHA-TESTING.md) / [ALPHA-TESTING.zh-CN.md](ALPHA-TESTING.zh-CN.md)

## Host integrations

The current first-class automatic-memory adapters are:

- [DeepSeek Harness adapter](../packages/dsh-adapter/README.md)
- [pi adapter](../packages/pi-adapter/README.md)

Existing Hermes integration:

- [v3-hermes-plugin](../src/v3-hermes-plugin/README.md)

Design/evidence documents:

- [B02-DSH-TOOL-SUPPORT.md](B02-DSH-TOOL-SUPPORT.md) — earlier DSH tool-only stage; retained for history.
- [B03-PI-ADAPTER-DESIGN.md](B03-PI-ADAPTER-DESIGN.md)
- [B04-DSH-ADAPTER-DESIGN.md](B04-DSH-ADAPTER-DESIGN.md)

## Evaluation and recall research

- [evaluation/locomo-recall-v2.md](evaluation/locomo-recall-v2.md)
- [recall-v2-contract.md](recall-v2-contract.md)
- [recall-v2-orchestration.md](recall-v2-orchestration.md)
- [QA-EMBEDDING-INDEX-DESIGN.md](QA-EMBEDDING-INDEX-DESIGN.md)
- [Q01-QUERY-CANARY.md](Q01-QUERY-CANARY.md)
- [G5B_EVALUATOR_RUNBOOK.md](G5B_EVALUATOR_RUNBOOK.md)

These are engineering/research documents. They should not be read as the current product support matrix unless [STATUS.md](STATUS.md) links to them as current evidence.

## Reliability and engineering history

Current reliability design:

- [reliability/](reliability/)
- [RUNTIME-INTEGRITY.md](RUNTIME-INTEGRITY.md)
- [WRITE-FLOW-CONTRACT.md](WRITE-FLOW-CONTRACT.md)

Milestone / incident / closure documents retained for traceability:

- [B01-CORE-INTERFACE-INVENTORY.md](B01-CORE-INTERFACE-INVENTORY.md)
- [B01-HOST-CONTRACT-SPEC.md](B01-HOST-CONTRACT-SPEC.md)
- [B01-FINDINGS.md](B01-FINDINGS.md)
- [F2-SOURCE-IDEMPOTENCY.md](F2-SOURCE-IDEMPOTENCY.md)
- [F3-FRESH-BOOTSTRAP.md](F3-FRESH-BOOTSTRAP.md)
- [GLOBAL-BASELINE.md](GLOBAL-BASELINE.md)
- [decisions/](decisions/)

## Historical public-alpha documents

[PUBLIC_ALPHA_SUPPORTED_SURFACE.md](PUBLIC_ALPHA_SUPPORTED_SURFACE.md) is retained as the evidence contract for the earlier public-alpha baseline. The repository has moved beyond that snapshot, especially in host adapters.

For the current product surface, use [STATUS.md](STATUS.md).

---

Documentation rule: **current user-facing claims live in README + STATUS + adapter READMEs; milestone/evidence files preserve how those claims were established.**
