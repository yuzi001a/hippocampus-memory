# RELIABILITY-MATRIX.md — fault → detection → diagnosis → repair

> One row per fault class, so future bugs can be triaged straight into this
> matrix. Health states & codes are stable identifiers (ADR-004). "Prod tested"
> = observed or replayed against the real production profile (read-only).

## Matrix

| Fault | Detection | Health | Diagnose code | Auto-repairable | Repair action | Needs provider | Data risk | Prod tested |
|---|---|---|---|---|---|---|---|---|
| editable / dev-tree runtime | RT01 import source | fail | `RUNTIME_EDITABLE_ACTIVE` | No (manual fix) | — | — | none | ✔ (canary env attr) |
| unrecognizable import | RT01 | warn | `RUNTIME_UNKNOWN_IMPORT` | No | — | — | none | ✔ |
| PG unreachable | ST01 `SELECT 1` | fail | `PG_UNREACHABLE` | No | — | — | none | ✔ lab |
| pgvector missing | ST02 extension probe | fail | `PGVECTOR_MISSING` | No | — | — | none | ✔ lab |
| schema ledger missing | ST03 schema_versions | fail | `SCHEMA_VERSION_MISMATCH` | Planned | `RUN_SCHEMA_UPGRADE` | No | additive only | ✔ lab (legacy shape) |
| canonical table missing | ST04 | fail | `SCHEMA_TABLE_MISSING` | Planned | `RUN_SCHEMA_UPGRADE` | No | additive only | ✔ lab |
| canonical index missing | ST05 | warn | `SCHEMA_INDEX_MISSING` | Yes (future) | `RECREATE_MISSING_INDEX` | No | additive | ✔ lab (drop & restore) |
| new NULL embedding | MW01 (since last success) | fail | `RECENT_EMBEDDING_FAILURE` | Yes (future) | `REBUILD_EMBEDDING_FOR_QA_IDS` (target=recent) | Yes | source intact → deterministic re-embed | ✔ prod |
| historical NULL debt | MW02 total | ok (reported) | `HISTORICAL_EMBEDDING_DEBT` (info) | Yes (future) | `REBUILD_EMBEDDING_FOR_QA_IDS` (target=debt) | Yes | same | ✔ prod |
| new empty answer | MW03 recent | warn | `EMPTY_ANSWER_RECENT` | Manual | `MANUAL_REVIEW_EMPTY_ANSWER` | No | needs source-of-truth check | ✔ prod |
| historical empty answers | MW03 total | ok (reported) | `EMPTY_ANSWER_HISTORICAL_DEBT` (info) | Manual | same | No | same | ✔ prod |
| explicit memory NULL | MW05 | warn | `EXPLICIT_MEMORY_EMBEDDING_NULL` | Yes (future) | `REPAIR_EXPLICIT_MEMORY_EMBEDDING` | Yes | re-embed only | ✔ lab |
| long-QA chunk inconsistency | MW06 | fail | `LONG_QA_CHUNK_INCONSISTENT` | Partial | `REBUILD_LONG_QA_CHILDREN`; orphans → `NO_AUTOMATIC_REPAIR_SOURCE_MISSING` | Mix | never guess source | ✔ lab + prod (column-name bug fixed) |
| active failure markers | FA02 (since last success) | fail | `RECENT_FAILURE_MARKER` | Partial | `RETRY_PENDING_FAILURES` | Yes | retry only | ✔ prod |
| poisoned markers (new) | FA03 | fail | `POISONED_FAILURE_MARKER` (error) | Partial | `RETRY_PENDING_FAILURES` | Yes | retry only | ✔ prod |
| poisoned markers (old) | FA03 | ok (reported) | `POISONED_FAILURE_MARKER` (info) | Future | same | Yes | none | ✔ prod (112 historical) |
| retry exhaustion | FA02 | warn | `RETRY_EXHAUSTED` | Future | `RETRY_PENDING_FAILURES` | Yes | none | ✔ lab |
| stale pending tickets | FA05 (48h) | warn | `STALE_PENDING_MARKER` | Future | `RETRY_PENDING_FAILURES` | Yes | none | ✔ lab |
| malformed markers | FA04 | warn | `MALFORMED_FAILURE_MARKER` | Manual | `MANUAL_REVIEW_MALFORMED_MARKER` | No | inspect only | ✔ lab |
| provider auth 401/402 | PR02 / deep | fail | `EMBED_PROVIDER_AUTH` | Manual | `FIX_PROVIDER_CREDENTIALS` | — | none | ✔ lab |
| provider 429 | PR02 / deep | warn | `EMBED_PROVIDER_RATE_LIMIT` | Partial | `RETRY_PENDING_FAILURES` | — | none | ✔ lab |
| provider timeout/5xx | PR02 / deep | warn | `EMBED_PROVIDER_TIMEOUT` | Partial | `RETRY_PENDING_FAILURES` | — | none | ✔ lab |
| provider unconfigured | PR01 | fail | `PROVIDER_UNCONFIGURED` | Manual | — | — | none | ✔ prod (bug fixed round 1) |
| observer stalled | DM02 | warn | `OBSERVER_STALE` | No | — | — | none | ✔ prod |
| derived layer stale | DM01/03/04 | warn | `DERIVED_LAYER_STALE` | No | — | — | none | ✔ prod |

## Status vocabulary

- **Auto-repairable** levels: `yes (future)` = a deterministic plan exists but v1
  ships dry-run only; `partial` = some cases; `manual` = operator judgement;
  `no` = infra/config fix outside the tool.
- **Failure accounting statuses** (marker ledger): `pending / in_flight /
  pending_db / retryable / retry_due / poisoned / recovered / stale_* /
  malformed / unresolved`. Current-incident boundary = `max(window, last
  success)`.
- Cross-cutting rule (ADR-001): debt is *reported*, current incidents *fail*;
  the same rule applies to markers, embeddings, and empty answers.

## Known open items (from the soak round, 2026-09-19)

1. **Long-text provider 400s are intermittent** (HTTP 400 code 20015 on
   19–24 k-char texts, identical fingerprint across 8/26 & 9/18 incidents).
   Manual replay of the exact text succeeds. System handling is correct
   (retryable markers); watch for retry outcomes.
2. **Legacy-format marker writer** observed at 01:14/01:18 during soak
   (fields match the private-source implementation). Channel not located;
   `.pth`/sys.path/config all clean. Next router restart re-verifies runtime
   identity end-to-end. Tracked in the vault soak-alert-01 investigation.
