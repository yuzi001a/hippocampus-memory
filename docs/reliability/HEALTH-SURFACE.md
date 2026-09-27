# HEALTH-SURFACE.md — `hippocampus health`

> Stability: schema_version 1 (see ADR-004). Exit codes are part of the contract.
> Design basis: `docs/reliability/DESIGN.md`; debt semantics: `docs/decisions/ADR-001`.

## Usage

```bash
hippocampus health                 # human summary, local checks only
hippocampus health --json          # full deterministic JSON report
hippocampus health --json --deep   # + bounded provider auth probes (10s each)
hippocampus health --json --allow-production-read   # SELECT-only against production
hippocampus health --window-hours 72                # widen the "current" window
hippocampus health --debug-paths                    # include raw paths in labels
```

Exit codes: `0` healthy · `1` degraded · `2` unhealthy / collection failure.

## What it answers

"Am I healthy *right now*?" — not "was I ever sick". Historical debt (legacy NULL
embeddings, old poisoned markers, historical empty answers) is reported but never
lowers the verdict by itself. A failure counts as *current* only when it happened
at/after the last successful embedding (fallback: the rolling window). See ADR-001.

## JSON shape (schema_version = "1")

```jsonc
{
  "schema_version": "1",
  "overall": "healthy | degraded | unhealthy",
  "generated_at": "<ISO8601>",
  "runtime":          { "import_source": {...}, "versions": {...}, ... },
  "storage":          { "pg": {...}, "pgvector": {...}, "schema_versions": [...],
                        "missing_tables": [], "missing_indexes": [] },
  "memory_write":     { "recent_qa": n, "recent_embedding_null": n,
                        "embedding_null_total": n, "empty_answer_total": n, ... },
  "failure_accounting": { "by_status": {...}, "current_poisoned": n,
                          "effective_since": "<ISO8601>", ... },
  "derived_memory":   { "topics_total": n, "observer_backlog": n, ... },
  "providers":        { "embed_configured": true, ... },
  "metrics":          { ... },
  "checks": [ { "check_id": "ST01_pg_reachable", "section": "storage",
                "status": "ok|warn|fail|skip|unknown",
                "summary": "...", "evidence": {...}, "duration_ms": 3 } ]
}
```

Field policy (ADR-004): timestamps ISO8601, durations seconds (`duration_ms` the sole
ms exception), counts int, unknown = `null` or explicit enum — never mixed types.

## Check catalog (27)

| Section | Checks |
|---|---|
| runtime | RT01 import_source (site_packages/editable/unknown) · RT02 python · RT03 profile · RT04 versions |
| storage | ST01 pg_reachable · ST02 pgvector · ST03 schema ledger · ST04 canonical tables · ST05 canonical indexes |
| memory_write | MW01 current pipeline (recent window) · MW02 embedding-NULL debt · MW03 empty answers · MW04 last writes · MW05 explicit memory · MW06 long-QA child consistency |
| failure_accounting | FA01 ledger · FA02 current failures · FA03 poisoned (current vs isolated) · FA04 malformed · FA05 stale |
| derived_memory | DM01 topics · DM02 observer cursor · DM03 observations · DM04 derived rows (freshness; warn-only, no hard contract yet) |
| providers | PR01 configuration · PR02 recent provider failure classes (from ledger) · PR03 deep auth (skip unless `--deep`) |

## Aggregation

`fail` → unhealthy; else `warn` → degraded; else `unknown` → degraded; else healthy.
`skip` never affects the verdict — it means "not applicable" (no traffic yet, fresh
install, deep not requested). `unknown` is reserved for "wanted to read it but could
not" (e.g. corrupt observer state).

## Performance budget

Default `health` issues ≤ 15 SELECTs against one reused connection, never scans source
rows row-by-row, and completes in well under a second on a warm runtime. Marker scan is
a bounded directory read (~hundreds of files). Remote probes happen only under `--deep`.
