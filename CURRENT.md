# Embedding Reliability Hotfix — CURRENT

**Status:** `PRODUCTION_INTEGRATION_VALIDATION`
**Branch:** `integration/p0-embedding-reliability`
**Validation base:** P0 HEAD `7808b89239787ba09eee7915c69392981a8ca911`
**P0 source parent:** `7808b89239787ba09eee7915c69392981a8ca911` (retained as integration parent)
**Embedding source commit:** `b344b801bac06e3dad0f68f47d243930d57ab07f`
**Cherry-pick integration HEAD:** `5c4fba03cddbbfda8b8dcd8c80821d665d29e3a4` (cherry-pick of embedding source onto P0 parent)
**Merge base / common baseline:** `0bd9e1ee7f42f0184f83aff367000a6656a06dcd` (`v0.2.1`)
**P0 isolation:** P0 branch `hotfix/e1-generated-context-boundary` (HEAD `7808b892...`) is retained as the integration parent; P0 generated-context boundary logic is preserved in this tree.

## Verified in this round

- Dedicated y400 targeted suite: `46 passed, 2 skipped`.
- Dedicated y400 baseline: `585 passed, 4 failed, 4 skipped`.
- Dedicated y400 corrected candidate: `640 passed, 4 failed, 4 skipped`.
- Differential: 55 new candidate tests passed; 4 failures are exact inherited baseline failures; `NEW_REGRESSION=0`.
- Critical-path harness: `16/16 passed` with UTF-8 console encoding; caller path remains off-thread.
- Outage simulation: 12/12 cells passed isolation/ledger invariants; evidence-selected stream primary is 5s/0.
- Disposable E2E: `38/38` passed; backfill E2E: `32/32` passed.
- Disposable read-only vector check: repaired long-QA parent and all 3 sidecar chunks are in the top-100 vector candidate set.
- Production read-only census completed; production migration/deploy/restart/backfill remain `0`.

> Note: the verification numbers above were recorded against the pre-integration
> embedding branch state. Integration HEAD `5c4fba0...` has NOT yet completed
> its own validation round; no wheel/tests claim is made for it here.

## Current policies

- `conversation_stream`: `STREAM_PRIMARY_EMBED_POLICY = 5s / 0 retries`.
- Deferred/batch repair: `10s / 2 retries` remains separate from the stream primary path.
- Realtime recall remains on its own `3s / 0` policy.

## Production boundary

`embedding_failures` is absent in production, so the tool-level backfill selector cannot yet run its ledger-aware dry-run. A ledger-independent, read-only upper-bound census is recorded in `evidence/production-backfill-dry-run-20260921.json`. No migration, deploy, restart, or DB backfill has occurred.

## Release gate

Integration validation on HEAD `5c4fba0...` is still pending. No production migration/deploy/backfill is authorized by this file.
