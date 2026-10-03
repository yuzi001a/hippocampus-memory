# M03 — full-repository regression and pristine-base comparison

Date: 2026-10-04
Branch under test: `feature/m03-derived-correction-propagation` (head `7726f35`, tree also carries the M03 source changes from `0430382`)
Pristine base: `23865f74366e9c39e6860f9d1658ece77f224e33` (`Merge pull request #37`, i.e. `main` before M03)

## Method

- **Branch run**: the whole suite (`src/v3-core/tests`, 93 files / 2210 tests collected) was run in
  four batches, because a single foreground run exceeds the 420 s tool ceiling and a single
  background run gets truncated. Each batch wrote its raw output to a file; nothing was piped
  through `tail` (an earlier attempt lost its output that way).
- **Base run**: a detached worktree of the pristine base was created at `C:/hp-testbed-base`, and
  `PYTHONPATH=C:/hp-testbed-base/src/v3-core/src` was used so `v3core` resolved from the **base**
  tree, not from the branch tree. Verified by printing `v3core.__file__`
  (`C:\hp-testbed-base\src\v3-core\src\v3core\__init__.py`).
- The base run re-executed exactly the files that failed on the branch, plus the files whose
  branch-side failures were errors, so the comparison is per-assertion and not per-total.

## Branch result (all 93 files)

| Batch | Files | Result |
|---|---|---|
| 1 | 1–16 | `2 failed, 158 passed, 5 errors` |
| 2 | 17–32 | `488 passed, 3 skipped, 3 errors` |
| 3 | 33–48 | `11 failed, 241 passed, 6 skipped` |
| rest | 49–93 | `1300 passed, 4 skipped, 21 errors` |
| **total** | **93** | **2187 passed / 13 failed / 29 errors / 13 skipped** |

## Base result (same failing files)

| Base run | Files | Result |
|---|---|---|
| `base_compare` | alpha_bootstrap, i01_dsh_parser, i01_import_auto, importers_contract | `13 failed, 83 passed, 1 skipped` |
| `base_compare2` | b01_event_contract_red, b02_mcp_packaging_contract, b02_mcp_profile_resolution | `26 passed, 1 error` |

## Per-assertion classification

Every branch-side failure was reproduced on the pristine base, i.e. **`BRANCH_REGRESSION = 0`**.

| Failure | Count | Root cause | base | Verdict |
|---|---|---|---|---|
| `test_explicit_memories_sql_is_lf_only_for_packaged_hash_parity` | 1 | CRLF checkout (`core.autocrlf=true`, no `.gitattributes`) | FAIL | `BASE_KNOWN` (environment) |
| `test_i01_dsh_parser` (4) + `test_i01_import_auto` (4) | 8 | `RuntimeError: dsh importer requires the 'zstandard' package` — not installed in this venv | FAIL | `BASE_KNOWN` (environment) |
| `test_importers_contract` (3) | 3 | `ValueError: hermes importer: no messages-like table in 'state.db' (tables=[])` — fixture DB is empty | FAIL | `BASE_KNOWN` (environment) |
| `test_explicit_memories_packaged_copy_is_byte_identical_to_repo_root` | 1 | **worktree-local byte pollution** (see below) | PASS | `NOT_M03` — fixed, re-verified PASS |
| `conftest.py` P0-A prod-outbox guard (teardown) | 26 | the real gateway on this host writes `j/journal_<date>/` during the run, so the guard cannot tell tester from gateway | ERROR | `BASE_KNOWN` (environment; task book §5 forbids opening `V3_HARNESS_ALLOW_LIVE_PROD_OUTBOX=1`) |
| `test_mcp_child_finds_the_same_profile_through_hermes_home` and 2 sibling errors | 3 | same guard family / environment | ERROR | `BASE_KNOWN` |

## Worktree-local byte pollution (found during this comparison, not an M03 defect)

`test_explicit_memories_packaged_copy_is_byte_identical_to_repo_root` failed on the branch with:

```text
At index 80 diff: b'\n' != b'\r'
```

Measured on disk:

| tree | `src/v3-core/schema/explicit_memories.sql` | `src/v3-core/src/v3core/schema/explicit_memories.sql` |
|---|---|---|
| branch worktree | CRLF, 6188 B | **LF, 6072 B** |
| pristine base worktree (fresh checkout) | CRLF, 6188 B | CRLF, 6188 B |

The 116-byte delta is exactly the 117 missing CR bytes. `git status` showed the file as clean,
because `core.autocrlf=true` normalises CRLF→LF when hashing, so git cannot see this class of
drift. The M03 change set does **not** contain `explicit_memories.sql`.

After restoring the file to its HEAD bytes, the assertion passes:

```text
test_alpha_bootstrap_contract.py:  1 failed, 58 passed, 1 error   (only the LF-only assertion + one guard error remain)
```

A sweep of every tracked `*.sql` file in both worktrees found **no other** LF-only file, so the
pollution was isolated to this one path.

## Conclusion

```text
BRANCH_REGRESSION            = 0
BASE_KNOWN_FAILURES          = 12   (1 LF contract + 8 missing zstandard + 3 empty fixture DB)
ENVIRONMENT_GUARD_ERRORS     = 29   (real gateway writing the production outbox during the run)
WORKTREE_LOCAL_POLLUTION     = 1    (fixed, re-verified)
M03_OWNED_TESTS              = all PASS (see the focused run: 144 passed, and the 136/136 real-PG E2E)
```

Nothing in the M03 change set is implicated by any failure in this run. The `zstandard` and
fixture-DB failures are dependency gaps of this developer machine; on CI (Linux, LF checkout,
dependencies installed) they do not occur.

Raw outputs: `batch1.txt`, `batch2.txt`, `batch3.txt`, `batch_rest.txt`, `base_compare.txt`,
`base_compare2.txt` under the scratch directory used for this run.
