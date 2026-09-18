# ADR-004: Health schema stability

**Status:** accepted (feature/reliability-recovery-v1)

## Context

`health` / `diagnose` / `repair --dry-run` are consumed by operators and will later be
consumed by UIs and scripts. An unstable JSON schema makes every consumer brittle.

## Decision

1. Every JSON payload carries `schema_version: "1"` at the top level.
2. Evolution is **additive-only**: new keys may appear, existing keys may not change
   type, meaning, or be removed within a schema version. Structural changes bump the
   version.
3. Type policy is fixed: timestamps are ISO8601 strings, durations are seconds (the
   `duration_ms` field is the sole integer-milliseconds exception), counts are ints,
   and "unknown" is always `null` or an explicit enum string — never mixed types.
4. Issue codes (`RUNTIME_EDITABLE_ACTIVE`, `HISTORICAL_EMBEDDING_DEBT`, …) are stable
   machine identifiers; human text lives in `summary`/`reason`, never in the code.
5. Exit codes are part of the contract and are pinned by tests + docs:
   - `health`: 0 healthy | 1 degraded | 2 unhealthy / collection failure
   - `diagnose`: 0 no active issue (info-only ok) | 1 issues found | 2 failed
   - `repair --dry-run`: 0 nothing to repair | 1 candidates exist | 2 planner failed

## Consequences

- Consumers can pin on codes and enums now and ignore unknown additions safely.
- Any intentional break must arrive with a `schema_version` bump and a migration note.
