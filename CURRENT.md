# P0 + Embedding Reliability Integration — CURRENT

**Status:** `DECISION_REQUIRED`
**Branch:** `integration/p0-embedding-reliability`
**Validated product source HEAD:** `4a7f496170c23fc0ee57be8286e65c31ade51546`
**Artifact build source HEAD:** `8c1a50f62e086c7139924696050650a376e2fc4a`
**Code cherry-pick HEAD:** `5c4fba03cddbbfda8b8dcd8c80821d665d29e3a4`
**P0 source parent:** `7808b89239787ba09eee7915c69392981a8ca911`
**Embedding source commit:** `b344b801bac06e3dad0f68f47d243930d57ab07f`
**Merge base / common baseline:** `0bd9e1ee7f42f0184f83aff367000a6656a06dcd` (`v0.2.1`)
**Common modified file:** `src/v3-core/src/v3core/__init__.py` — semantically reviewed, not resolved by ours/theirs.

## Integration validation

- P0 protected Git-blob drift relative to the P0 parent: `0`.
- Embedding-related product/eval/schema/test paths compared: `20`; drift excluding common `__init__.py`: `0`.
- y400 integration full suite: `763 passed / 4 failed / 5 skipped`.
- `INTEGRATION_NEW_REGRESSION = 0`; the four failures are the inherited baseline set.
- P0 targeted integration-source scope: `123 passed / 1 skipped`.
- Final wheel P0 targeted scope: `123 passed / 1 skipped`.
- Embedding exact policy/reliability two-file scope: `33 passed / 0 skipped` from source and final wheel.
- Extended embedding policy/reliability/ledger/yin/backfill scope: `58 passed / 0 skipped`.
- Critical path: source `16/16`, final wheel `16/16`.
- Final wheel disposable integration smoke: P0 reader/boundary `PASS`; embedding failure-ledger forward E2E `38/38`; backfill safety E2E `32/32`.

The previously reported embedding-branch `46 passed / 2 skipped` line is not used as the integration result because its exact command artifact was not preserved. The exact current scopes and logs are recorded above and under `evidence/`.

## Artifact

The old embedding-only wheel is `SUPERSEDED_FOR_PRODUCTION_INTEGRATION` and must not be installed in production.

The final integration wheel is the only candidate artifact:

- built from branch HEAD: `8c1a50f62e086c7139924696050650a376e2fc4a`
- filename: `v3_core-4.0.0-py3-none-any.whl`
- SHA256: `8f5fecdf5c9af049011f925c3bd14c5175e727ec33996a61e22981daee6dbb88`
- artifact record: `evidence/integration-wheel-build.final.json`
- production file comparison: `evidence/production-current-vs-integration-artifact.final.json`

## Production boundary

Production remains P0-only. No embedding migration, wheel install, restart, or database backfill has occurred:

```text
migration = 0
deploy    = 0
restart   = 0
db_backfill = 0
```

The deployment/rollback plan remains plan-only. Any production action requires a separate decision; this branch is now stopped at `DECISION_REQUIRED`.
