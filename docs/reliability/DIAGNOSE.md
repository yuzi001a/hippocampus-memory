# DIAGNOSE.md — `hippocampus diagnose`

> Stability: schema_version 1 (ADR-004). Codes are stable machine identifiers;
> human text lives in `summary`. See also: HEALTH-SURFACE.md, ADR-001.

## Usage

```bash
hippocampus diagnose --json                        # classify the current state
hippocampus diagnose --json --allow-production-read
hippocampus diagnose --json --deep                 # + provider auth probes
```

Exit codes: `0` no active issue (info-only or empty) · `1` at least one severity≥warning
issue · `2` hard failure. Info-level historical debt does **not** set exit 1.

## What it answers

"*Why* is it not healthy?" — diagnose consumes the health report's check evidence and
emits stable, machine-readable issues. It never re-queries storage.

Each issue:

```jsonc
{ "code": "RECENT_EMBEDDING_FAILURE", "severity": "error", "scope": "memory_write",
  "summary": "2 new NULL embeddings since last success", "evidence": {...},
  "repairable": true }
```

## Code catalog (24)

**Runtime**
| Code | Severity | Trigger |
|---|---|---|
| `RUNTIME_EDITABLE_ACTIVE` | error | v3core imports from an editable private source instead of site-packages |
| `RUNTIME_UNKNOWN_IMPORT` | warning | import source cannot be classified |

**Storage**
| Code | Severity | Trigger |
|---|---|---|
| `PG_UNREACHABLE` | error | PostgreSQL not reachable / SELECT probe failed |
| `PGVECTOR_MISSING` | error | pgvector extension missing |
| `SCHEMA_VERSION_MISMATCH` | error | schema_versions lacks the expected `v0.2` row |
| `SCHEMA_TABLE_MISSING` | error | one or more canonical tables absent |
| `SCHEMA_INDEX_MISSING` | warning | a canonical index is absent |

**Memory write (current vs debt)**
| Code | Severity | Trigger |
|---|---|---|
| `RECENT_EMBEDDING_FAILURE` | error | `recent_embedding_null > 0` (since last success) |
| `HISTORICAL_EMBEDDING_DEBT` | info | NULL-embedding total > 0; kept even alongside a recent incident |
| `EMPTY_ANSWER_RECENT` | warning | empty answers inside the current window |
| `EMPTY_ANSWER_HISTORICAL_DEBT` | info | empty-answer total (historical) |
| `EXPLICIT_MEMORY_EMBEDDING_NULL` | warning | explicit_memories rows missing embeddings |
| `LONG_QA_CHUNK_INCONSISTENT` | error | chunk rows with NULL embedding / bad offsets / duplicates / orphan parents |

**Failure accounting (marker ledger)**
| Code | Severity | Trigger |
|---|---|---|
| `RECENT_FAILURE_MARKER` | error | active broken ticket (retry_due / stale / unresolved) since last success |
| `POISONED_FAILURE_MARKER` | error if current, info if historical | poisoned markers; historical-only stays info |
| `RETRY_EXHAUSTED` | warning | failed markers at/over the retry budget |
| `STALE_PENDING_MARKER` | warning | in-flight/pending-db tickets older than the stale threshold |
| `MALFORMED_FAILURE_MARKER` | warning | unparseable marker files |

**Derived memory (freshness — warn/unknown only, no hard contract yet)**
| Code | Severity | Trigger |
|---|---|---|
| `OBSERVER_STALE` | warning | observer cursor backlog / observation freshness beyond thresholds |
| `DERIVED_LAYER_STALE` | warning | topic/derived rows older than 7 days |

**Providers**
| Code | Severity | Trigger |
|---|---|---|
| `EMBED_PROVIDER_AUTH` | error | 401/402 class recent failures or deep auth failure |
| `EMBED_PROVIDER_RATE_LIMIT` | warning | 429 class recent failures |
| `EMBED_PROVIDER_TIMEOUT` | warning | timeout / connection / 5xx class recent failures |
| `PROVIDER_UNCONFIGURED` | warning | endpoint/key not configured |

## The debt boundary in practice

`HISTORICAL_EMBEDDING_DEBT` (severity `info`) is present whenever debt exists — it is
**reported, never alarming**. `RECENT_EMBEDDING_FAILURE` appears only when a NULL was
created at/after the last successful embedding. Production today (105 legacy NULLs, last
success minutes ago) therefore shows: debt `info`, no recent failure — and diagnose
exits 0.
