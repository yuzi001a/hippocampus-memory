# Full-regression closure: two branch-introduced regressions

Date: 2026-10-03
Author host: WinNAS (no production DB/outbox/service contact)
Test host for the Y400 runs: `DESKTOP-EQP3OBU`

## What the full regression found

Split-directory regression (the layout CI uses):

| suite | before the fix | after the fix |
|-------|-----------------|---------------|
| `src/v3-core` `tests/` + `eval/g5b_real_memory_evaluator/tests` | 12 failed / 2147 passed / 12 skipped (RC 1) | — |
| `src/v3-core` `tests/` only | — | 10 failed / 2047 passed / 12 skipped (RC 1) |
| `src/v3-core` `eval/g5b_real_memory_evaluator/tests` only | — | 102 passed (RC 0) |
| `src/v3-hermes-plugin` | 12 passed (RC 0) | 12 passed (RC 0) |
| `packages/dsh-adapter` (`node --test`) | exit 0 | exit 0 |
| `packages/pi-adapter` (`node --test`) | exit 0 | exit 0 |

Count reconciliation: the pre-fix core run collected `tests/` *and* the evaluator package; the post-fix core run was scoped to `tests/` with the evaluator run separately.
`2147 − 102 (evaluator) + 2 (the two fixed regressions now pass) = 2047` and `12 − 2 = 10`, so the two runs are consistent.
After the fix the failing set is exactly the ten base-known nodes; no branch-introduced failure remains.

Base differential (see `full-regression-base-differential.md`):
`BASE_FAILING_IDENTICALLY_COUNT=10 OF 12`. Ten are base-known environment artifacts of
the disposable Y400 tree and are deliberately **not** repaired. Two were introduced by
this branch and are therefore in scope.

## The two regressions

| test | base | branch (before fix) |
|------|------|---------------------|
| `test_b02_active_memory_source_read.py::test_hm_get_reads_explicit_memory_source_id` | PASS | FAIL |
| `test_b02_tool_scope_completion.py::test_handbook_lookup_uses_scoped_manager` | PASS | FAIL |

Both are pre-existing B02 public-read contracts. Neither is a new M01/M02 test.

## Root cause

M02 added an entry-level fail-closed runtime-scope pre-check to `handle_hm_get`
(`src/v3-core/src/v3core/tools/get_tool.py`). Its condition fired whenever a caller
supplied an already-constructed `core`:

```
if ("runtime_context" in kw or kw.get("core") is not None or "effective_config" in kw):
```

`resolve_scope()` (`tools/memory_correction.py`) refuses when the pool **and** pg are both
absent, or when the effective config is absent. So the pre-check refused two reads whose
paths need neither:

* the ACTIVE explicit-memory exact-hit read uses only `core.pg` and never resolves a
  profile — refused with `SCOPE_UNAVAILABLE` instead of returning the row;
* the handbook fallback uses `handbook_manager(current_scope())` and never needs PG —
  refused before `HandbookManager` was ever constructed.

The default-profile hazard the guard exists for is the **card-file** fallback, which
calls `resolve_config()`. The guard was placed at function entry instead of at that
point.

## Fix

Only `src/v3-core/src/v3core/tools/get_tool.py` changed. No test file was modified.

* A supplied `core` is a fully booted runtime and is no longer by itself a refusal
  reason; its own `.pg` / `.config` are used directly.
* The entry guard now fires only when the caller declares an `effective_config` that is
  explicitly empty, or declares a runtime **without** supplying a core.
* The card-file fallback no longer resolves the default profile when a runtime was
  declared: it takes `effective_config`, then the supplied core's own `config`, and if
  still unresolved with a declared runtime it returns the same `SCOPE_UNAVAILABLE`
  payload instead of `resolve_config()`.

Fixed file: `src/v3-core/src/v3core/tools/get_tool.py`
`git hash-object` (LF-normalized index blob): `96167c275f575b7a651b76310a8ec8824e2a5b9f`
raw-content sha1: `232f8a79366d5e8f4a91d9ff86fab494759a8648`
bytes: 26813

## Parent-verified results after the fix

| suite | before | after |
|-------|--------|-------|
| `test_b02_active_memory_source_read.py` + `test_b02_tool_scope_completion.py` | 2 failed / 5 passed / 1 teardown error | — |
| the same two files + `test_m02_memory_correction_tools.py` | 2 failed / 98 passed | **100 passed, 0 failed** |
| `test_m01_memory_correction.py` + `test_m01_m02_integration_driver_contract.py` + `test_m01_correction_distribution.py` | green | **453 passed, 1 skipped** |

Command (author host, repo-local venv):

```
cd C:/hp-testbed/src/v3-core
PYTHONPATH=src V3CORE_CONFIG= F2_PG_DSN= TEST_POSTGRES_DSN= \
  C:/hp-testbed/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  tests/test_b02_active_memory_source_read.py tests/test_b02_tool_scope_completion.py \
  tests/test_m02_memory_correction_tools.py
```

The transient `P0-A production-outbox` teardown ERROR observed in some runs is the
pre-existing harness guard reacting to the live gateway writing
`~/.v3-core/profiles/default/j/`. It is environmental, it is **not** bypassed, and no run
in this closure set `V3_HARNESS_ALLOW_LIVE_PROD_OUTBOX`.

## Acceptance consequence (declared)

`run7` was the one complete Y400 acceptance for the **pre-fix** tree. Fixing a real
regression changes the delivered product bytes, so `run7` no longer covers them. A second
complete Y400 acceptance (`run8`) is run once on the fixed tree — this is re-validation
of changed bytes, not a repeat run to seek green. Its result is recorded in
`run8-report.json` and in `README.md`.

## Provenance / limits

* Child agent `deleg_1987427a` produced the first draft of the change; the parent read the
  full diff, confirmed the file hash, and re-ran every suite above itself. Child-reported
  counts are supporting evidence only.
* `NOT RECORDED`: the exact wall-clock of the pre-fix focused run and of the Y400
  base-differential loop.
* The ten base-known failures were **not** re-verified beyond the base differential, and
  are not claimed fixed.
