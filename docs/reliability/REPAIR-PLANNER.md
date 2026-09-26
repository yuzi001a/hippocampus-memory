# REPAIR-PLANNER.md — `hippocampus repair --dry-run`

> Stability: schema_version 1 (ADR-004, ADR-003). v1 is **dry-run only** —
> `--apply` is hard-disabled. See also: DIAGNOSE.md, ADR-003.

## Usage

```bash
hippocampus repair --dry-run --json                     # plan only, zero writes
hippocampus repair --dry-run --json --allow-production-read
hippocampus repair --apply                              # exit 2, refused
```

Exit codes: `0` nothing to repair · `1` repair candidates exist · `2` planner failed /
unsafe. `--apply` always returns `2` with
`REPAIR_APPLY_NOT_IMPLEMENTED_IN_V1` **before any service is constructed**.

## What it answers

"If we were to repair, *what* would be repaired?" — a deterministic mapping from
diagnoses to actions, each with a full risk envelope:

```jsonc
{
  "action_id": "REBUILD_EMBEDDING_FOR_QA_IDS",
  "issue_code": "RECENT_EMBEDDING_FAILURE,HISTORICAL_EMBEDDING_DEBT",
  "target_count": 105,
  "risk": "medium",
  "reversible": false,
  "requires_provider": true,
  "estimated_remote_calls": 105,
  "estimated_cost": null,
  "writes_database": true,
  "automatic_safe": false,
  "reason": "..."
}
```

## Action catalog

| Action | Issue codes | Scope of work |
|---|---|---|
| `REBUILD_EMBEDDING_FOR_QA_IDS` | RECENT_/HISTORICAL_EMBEDDING_* | deterministic re-embed of source-complete QA rows |
| `RETRY_PENDING_FAILURES` | RECENT_FAILURE_MARKER, POISONED_FAILURE_MARKER, RETRY_EXHAUSTED, STALE_PENDING_MARKER, EMBED_PROVIDER_RATE_LIMIT, EMBED_PROVIDER_TIMEOUT | re-dispatch retryable-class tickets once the provider recovers |
| `REPAIR_EXPLICIT_MEMORY_EMBEDDING` | EXPLICIT_MEMORY_EMBEDDING_NULL | re-embed explicit_memories rows |
| `REBUILD_LONG_QA_CHILDREN` | LONG_QA_CHUNK_INCONSISTENT (non-orphan cases) | repair/remove inconsistent child rows |
| `FIX_PROVIDER_CREDENTIALS` | EMBED_PROVIDER_AUTH | rotate/fix credentials (no DB writes) |
| `MANUAL_REVIEW_EMPTY_ANSWER` | EMPTY_ANSWER_RECENT / _HISTORICAL_DEBT | source-of-truth check before any mutation |
| `MANUAL_REVIEW_MALFORMED_MARKER` | MALFORMED_FAILURE_MARKER | inspect unreadable marker files |
| `NO_AUTOMATIC_REPAIR_SOURCE_MISSING` | orphan parents / unknown codes | source unreconstructable — **never guess** |
| `RUN_SCHEMA_UPGRADE` / `RECREATE_MISSING_INDEX` | SCHEMA_TABLE_MISSING / SCHEMA_INDEX_MISSING / SCHEMA_VERSION_MISMATCH | additive schema repair via the upgrade path |

Manual-only codes (no automatic action emitted): `RUNTIME_EDITABLE_ACTIVE`,
`RUNTIME_UNKNOWN_IMPORT`, `PG_UNREACHABLE`, `PGVECTOR_MISSING`, `OBSERVER_STALE`,
`DERIVED_LAYER_STALE`, `PROVIDER_UNCONFIGURED`.

## Source-preservation rules (hard)

- Source complete → deterministic re-embed is a legitimate future action.
- Source missing → `NO_AUTOMATIC_REPAIR_SOURCE_MISSING`. Never reconstruct from
  summaries, never guess from similar sessions, never invert from derived memory.
- Every action declares `automatic_safe: false` in v1 — nothing executes without a
  future apply contract (plan-hash + confirmation, mirroring the v0.2.1 upgrade path).
- Duplicate actions merge: `issue_code` joins contributors, `target_count` takes the max.

## Zero-write guarantee

Locked by tests: the planner never opens a database connection, consumes only diagnosis
objects, and `repair --apply` refuses before building any service. `repair --dry-run`
against production is safe by construction.
