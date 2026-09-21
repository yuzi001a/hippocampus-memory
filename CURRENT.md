# Embedding Reliability Hotfix — CURRENT

**Status:** `VALIDATION_IN_PROGRESS`
**Branch:** `fix/embedding-write-reliability`
**Validation base:** `0bd9e1ee7f42f0184f83aff367000a6656a06dcd` (`v0.2.1`)
**P0 isolation:** P0 branch `hotfix/e1-generated-context-boundary` is not merged or ported.

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

## Current policies

- `conversation_stream`: `STREAM_PRIMARY_EMBED_POLICY = 5s / 0 retries`.
- Deferred/batch repair: `10s / 2 retries` remains separate from the stream primary path.
- Realtime recall remains on its own `3s / 0` policy.

## Production boundary

`embedding_failures` is absent in production, so the tool-level backfill selector cannot yet run its ledger-aware dry-run. A ledger-independent, read-only upper-bound census is recorded in `evidence/production-backfill-dry-run-20260921.json`. No migration, deploy, restart, or DB backfill has occurred.

## Release gate

Validation is complete on the isolated branch. After this tree is committed and pushed, parent must verify the remote ref and stop at `DECISION_REQUIRED` before any production migration/deploy/backfill.
