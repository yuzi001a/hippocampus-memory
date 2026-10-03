# M01/M02 final isolated acceptance

All facts below refer to a disposable PostgreSQL on Y400 (`DESKTOP-EQP3OBU`), isolated profiles and the real public HTTP tool route. The seeded deployment facts are synthetic regression fixtures, not user/production data.

## Final delta closure

- Proven concurrency cause: the no-pool legacy `PgEmbedStore.lease()` shares its cached physical connection, allowing concurrent requests to share a transaction. Correction now borrows an independent side connection. A callable independent seam that returns `None` or raises fails closed (`durable=false`); it must not fall back to the shared lease. Seam-absent legacy behavior and the PgPool path remain unchanged.
- Parent targeted real-PG/public-HTTP test: three identical A→B callers return one `CORRECTION_COMMITTED` and two `CORRECTION_DEDUPLICATED`, all `durable=true`; B row count=1, edge A→B count=1, A archived, B active. A subsequent identical sequential request returns deduplicated and creates zero rows/edges. Exact receipts/readback: `parent-concurrency-final.json`.
- History failure was a driver expectation bug, not product corruption. A→B→C is superseded/superseded/current before withdrawing C and superseded/superseded/withdrawn afterward. Phase-specific assertions preserve exact ordered edge/row/content checks. Parent Y400 targeted result: 47 passed, RC=0.
- Parent lease-focused suite: 13 passed / 175 deselected, RC=0. Child RED: 4 failed / 184 deselected, RC=1; GREEN: 6 passed / 182 deselected, RC=0; child full M01 file: 188 passed, RC=0. These child totals are supporting evidence, not a replacement for parent real-PG acceptance.

## Complete Y400 acceptance — run twice, and why

| run | tree | result |
|-----|------|--------|
| `run7-report.json` | the tree as it stood when the two targeted blockers closed | 182 checks / 181 PASS / 0 FAIL / 1 NOT_EVALUATED / exit 0 / VERDICT=PASS |
| `run8-report.json` | the **delivered** tree, after the two read-path regressions below were fixed | 182 checks / 181 PASS / 0 FAIL / 1 NOT_EVALUATED / exit 0 / VERDICT=PASS |

`run7` accepted the pre-fix bytes. The full-repository regression then found two regressions this branch had introduced (see `full-regression-base-differential.md` / `regression-closure.md`). Fixing them changed the product bytes, so `run7` no longer covered the deliverable and the complete acceptance was run **once more** on the fixed tree. That is re-validation of changed bytes, not a repeat run to seek green: the check count is unchanged at 182, and no acceptance check was added, relaxed or skipped. Run8's parent-read `verdict` is `PASS`; the parent matched the installed module/schema bytes (`artifact_provenance.actual`, including `module:tools/get_tool.py`) and confirmed `env_sanitized` carried no ambient credential, endpoint or production-bypass, `no_secret_in_report=true`, and `no_leftover_server_process` (all three lanes `alive=false`).

The single unevaluable check is `fresh_session_recall_contains_current_version`: no embedding endpoint is configured and canonical rows carry `embedding=null` (0/18 embedded), so the vector recall block is empty (`block_chars=0`). Recorded as `ok=None` / `NOT EVALUABLE BY ENVIRONMENT` with `declared_coverage_gap=true` — **not** a pass. The adjacent `fresh_session_recall_classified_embeddings_unavailable` and `fresh_session_recall_does_not_return_old_as_current` checks do pass, so the recall path is exercised but its semantic quality is not evaluable here. Real model answer quality is not evaluated and is not required here. M03 propagation, M04 as-of reasoning, LoCoMo, I01, F2/F3 and production deployment are outside this task.

## Repository regression

| suite | before the regression fix | after the fix |
|-------|---------------------------|---------------|
| `src/v3-core` `tests/` **+** `eval/g5b_real_memory_evaluator/tests` | 12 failed / 2147 passed / 12 skipped (RC 1) | — |
| `src/v3-core` `tests/` only | — | 10 failed / 2047 passed / 12 skipped (RC 1) |
| `src/v3-core` `eval/g5b_real_memory_evaluator/tests` only | — | 102 passed (RC 0) |
| `src/v3-hermes-plugin` | 12 passed (RC 0) | 12 passed (RC 0) |
| `packages/dsh-adapter`, `packages/pi-adapter` (`node --test`) | exit 0 | exit 0 |

**Count reconciliation (declared, because the two core runs do not have identical scope).** The pre-fix core run collected `tests/` *and* `eval/g5b_real_memory_evaluator/tests`; the post-fix core run was scoped to `tests/` and the evaluator package was run separately. `2147 − 102 (evaluator) + 2 (the two fixed regressions now pass) = 2047`, and `12 − 2 = 10`. The two runs are therefore consistent, and the failure set after the fix is exactly the ten base-known nodes.

Base differential: `BASE_FAILING_IDENTICALLY_COUNT=10 OF 12` — ten of the twelve pre-fix failing nodes fail identically on a pristine `89e50a5` tree and are base-known environment artifacts of the disposable test tree. The remaining two were branch-introduced, are fixed in `handle_hm_get` with no test-file change, and are documented in `regression-closure.md`.

The CI slice from `product-ci.yml` was also run locally on this tree: v3-core focused 432 passed / 5 skipped; G5b evaluator 102 passed; v3-hermes-plugin 12 passed; import-smoke and `compileall` clean. Exact-head GitHub CI is the authoritative gate; its outcome is reported separately and is not claimed here.

## Preserved failures and evidence limitations

- Prior complete run6 remains FAIL: 182 checks, 6 failures, one not evaluable, exit=1. Its five identical-concurrency failures and one history-phase failure were not erased or relabeled.
- Historical run6 exception class/SQLSTATE/statement stage/transaction state not present in original diagnostics remain **NOT RECORDED**; later reproductions are not retroactively attributed to run6.
- The first parent targeted attempt ran 13 unit tests successfully but the real-PG harness refused its nonempty run root (`unit.log`); RC=90 and no real concurrency test ran. A fresh empty root and sibling log path corrected the parent runner, not a product guard downgrade.
- One earlier production-outbox bypass attempt is **INVALID / historical impact not provable without snapshot**. It is not used as acceptance evidence, not reopened or claimed harmless. No guard-bypass environment was inherited by the valid final isolated runs.
- The follow-up child initially edited only the Y400 staged copy, not the canonical checkout. Parent verified the exact preimage, applied only the two-file delta locally, compared canonical and remote contents, then restaged canonical source before full acceptance.
- `conc-closure/staged/` held three instrumentation copies of `active_memory_store.py` from the pre-fix investigation. They were deleted from the working tree before delivery because a stale full copy of a product module in the repo is a hazard; they are regenerable from the retained `make_instrumented.py` / `make_prefix.py`. `scoped-repair.diff` and the instrumentation runners remain uncommitted authoring scratch, as the `conc-closure/README.md` declares.
- The base differential re-ran **only the twelve failing node ids** against the base tree, not the whole base suite. It proves how those twelve nodes behave, not that base is otherwise green. The ten base-known failures were not re-verified further and are not claimed fixed.
- Production database/outbox/gateway: no connection, write, install or restart in the valid final isolated acceptance. This does not rewrite the historical invalid attempt into proven no-contact.

No merge/release is authorized.
