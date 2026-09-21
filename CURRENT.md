# Hippocampus — long observation index

Status: `DECISION_REQUIRED`

## Candidate

- Branch: `feature/long-observation-index-v1`
- Base: `9c4d191017620e9edbd81ca8fd85d232f4faf23c`
- Implementation commits: `12a2bce`, `f20c668`, `2f23e34`
- Candidate wheel: `C:\\hp-testbed\\artifacts\\long-observation-candidate\\v3_core-4.0.0-py3-none-any.whl`
- Candidate wheel SHA256: `9bb82c792a0d1ccbd62ed1511b3b5502afc8f751a8273397c7beb60629544d4a`
- Status: integration candidate only; not production-authorized.

## Production freeze result

Production was used only for SELECT/schema introspection/source hashes/token census and the observation dry-run. No production migration, INSERT, UPDATE, DELETE, real backfill, live venv reinstall, gateway restart, or serve restart was performed by this task.

Final production facts:

- provider/model: SiliconFlow `BAAI/bge-m3`
- fingerprint: `bf32771ecbd1`
- provider hard window: `8192`
- safe planner target: `7680`
- `observation_notes` NULL parent vectors: `6`
- NULL IDs: `634, 726, 732, 743, 746, 759`
- all six are long/unembeddable on the old single-request path
- all six dry-run plans are valid, two children each, no child above `7680`
- total planned historical child calls: `12`
- observation sidecar in production: absent
- observation failure ledger: `total=0, unresolved=0`
- production source hashes are recorded in the private-safe plan evidence.

Live package manifest remained unchanged from the frozen production wheel. Process observation found two gateway and two serve processes; this task issued no lifecycle command. The process observation is retained separately rather than treating PID history as a restart claim.

## Validation

- RED evidence retained before implementation.
- Targeted preservation gates: `229 passed`.
- Full candidate suite: `854 passed, 3 inherited importer fixture failures, 4 skipped`.
- Baseline: `801 passed, 4 inherited importer fixture failures, 4 skipped`.
- Original-only differential: `NEW_REGRESSION=0`.
- Disposable PostgreSQL E2E: short, 10k, 20k, retry, permanent failure, sidecar failure, idempotent rerun, and stale 4→3 replacement all passed.
- Real non-production provider: 10k and 20k sources passed; every child <=7680, dimension 1024, fingerprint correct.
- Final wheel imported from isolated `wheel-env`; P0 identity validator, forward embedding imports, long-QA, observation short/long planner, parent merge, D5 commit boundary, and backfill CLI entrypoint passed.

## Remaining production actions

1. Decide whether to apply the additive migration in production.
2. If migration is approved, run a separately authorized production dry-run/repair gate.
3. Authorize a production observation backfill batch; no batch was applied tonight.
4. Decide separately whether the static audit's 18 potentially-unbounded non-observation call sites need follow-up. No additional product scope was changed tonight.

Evidence root: `C:\\hp-testbed\\evidence\\`
