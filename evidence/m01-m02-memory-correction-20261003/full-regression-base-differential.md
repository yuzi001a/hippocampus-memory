# Full-regression base differential (M01/M02)

Date: 2026-10-03
Branch: `feature/m01-m02-memory-correction` (HEAD `89e50a5bbecca8b7ed54308e40fbcc33abf932e8` + working tree)
Author host: WinNAS (read-only w.r.t. production)
Execution host: Y400 (`DESKTOP-EQP3OBU`), disposable isolated venv, no production contact

## Why this exists

A full regression of the branch produced 12 failures in `src/v3-core`. A failure count
alone does not say whether the branch broke something or whether the baseline was
already red. Every failing node id was therefore re-run against a **pristine base
tree** (`git archive 89e50a5` -> `C:/hp-testbed/m01-m02-integration-20261003/base-src`)
on the same host with the same interpreter, and the two runs were compared node by node.

Base provenance: `BASE_STORE_SHA 6798976bacbfd1c36b115ea0509a2345a3c823c8c2e67eebe0ba2bae908a699b`
(`active_memory_store.py`, 30358 bytes at base).

## Result

`BASE_FAILING_IDENTICALLY_COUNT=10 OF 12`

| # | test node | base | branch | classification |
|---|-----------|------|--------|----------------|
| 1 | `test_b02_active_memory_source_read.py::test_hm_get_reads_explicit_memory_source_id` | PASS | FAIL | **real regression (branch)** |
| 2 | `test_b02_tool_scope_completion.py::test_handbook_lookup_uses_scoped_manager` | PASS | FAIL | **real regression (branch)** |
| 3 | `test_backfill_cli_entrypoint.py::test_missing_model_fails_closed_and_names_the_key` | FAIL | FAIL | base-known |
| 4 | `test_backfill_cli_entrypoint.py::test_missing_endpoint_fails_closed_and_names_the_key` | FAIL | FAIL | base-known |
| 5 | `test_backfill_cli_entrypoint.py::test_config_errors_are_not_disguised_as_disabled` | FAIL | FAIL | base-known |
| 6 | `test_first_run_install_contract.py::test_reused_container_password_is_adopted` | FAIL | FAIL | base-known |
| 7 | `test_importers_contract.py::test_hermes_parse_state_db_emits_raw_messages` | FAIL | FAIL | base-known |
| 8 | `test_importers_contract.py::test_hermes_oldest_newest_computed_from_parsed_items` | FAIL | FAIL | base-known |
| 9 | `test_importers_contract.py::test_hermes_rerun_records_zero_new_rows` | FAIL | FAIL | base-known |
| 10 | `test_reliability_cli.py::test_health_help_smoke` | FAIL | FAIL | base-known |
| 11 | `test_reliability_cli.py::test_diagnose_help_smoke` | FAIL | FAIL | base-known |
| 12 | `test_reliability_cli.py::test_repair_help_smoke` | FAIL | FAIL | base-known |

The 10 base-known failures are environment/asset artifacts of the disposable Y400 tree
(missing Hermes `state.db` fixture for the importer contracts; missing repo-local
`.venv` for the reliability-CLI `--help` smoke tests, which hardcode
`REPO_ROOT/.venv/Scripts/python.exe`; container/docker-dependent first-run and
backfill-CLI cases). They are **not** repaired here — out of scope, and out of scope by
the task's own rule not to fix unrelated baseline failures.

The 2 branch-introduced regressions are not base-known and are therefore in scope.
Both share one root cause: the M02 fail-closed runtime-scope pre-check in
`handle_hm_get` (`get_tool.py`) fired whenever a caller supplied an already-constructed
`core`, and `resolve_scope()` demands a pool *and* an effective config. Two pre-existing
B02 contracts read through paths that need neither:

* the ACTIVE explicit-memory row read uses only `core.pg` and never resolves a profile;
* the handbook fallback uses `handbook_manager(current_scope())` and never needs PG.

The default-profile hazard the guard exists for is the card-file fallback, which calls
`resolve_config()`. The guard has been moved to that point.

## Reproduce

```
# branch
cd C:/hp-testbed/src/v3-core
PYTHONPATH=src V3CORE_CONFIG= F2_PG_DSN= TEST_POSTGRES_DSN= \
  C:/hp-testbed/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  tests/test_b02_active_memory_source_read.py tests/test_b02_tool_scope_completion.py

# base (same host, pristine tree)
cd C:/hp-testbed/m01-m02-integration-20261003/base-src/src/v3-core
PYTHONPATH=src ... -m pytest -q -p no:cacheprovider tests/test_b02_active_memory_source_read.py::test_hm_get_reads_explicit_memory_source_id
```

## Provenance / limits

* Runner: `C:/Users/servi/AppData/Local/hermes/cache/scratch/m01m02-base-differential.ps1`
  (per-node `BASE_RC` loop, fail-closed on a non-empty run root).
* Failing-node list: `C:/Users/servi/AppData/Local/hermes/cache/scratch/m01m02-failing-ids.json`.
* Branch full-regression logs: `C:/Users/servi/AppData/Local/hermes/cache/scratch/m01m02-regression-v3-core-full.log`
  (core: 12 failed / 2147 passed / 12 skipped), `...-plugin-full.log` (plugin: 12 passed).
* The base differential re-ran **only the 12 failing node ids**, not the whole base
  suite. It therefore proves "these 12 behave identically/differently", not "base is
  otherwise green".
* `NOT RECORDED`: base run wall-clock and per-node durations were not captured.
