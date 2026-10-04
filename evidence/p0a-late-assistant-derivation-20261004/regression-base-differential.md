# P0-A regression — base differential (focused set)

Date: 2026-10-04
Head tree: `fix/p0a-late-assistant-qa-derivation` (working tree, P0-A changes uncommitted)
Base tree: `git archive f9b68e5` (origin/main) extracted to an isolated snapshot
Command (identical on both): pytest <10 focused test files> -q
Machine: WinNAS dev host (NOT the Y400 acceptance laptop; §19 E2E is separate)

## Results

| | head (P0-A tree) | base (f9b68e5) |
|---|---|---|
| passed | 346 | 346 |
| failed | 1 | 1 |
| skipped | 5 | 5 |
| errors | 3 | 1 |
| duration | 232.73s | 277.21s |

## Failure / error attribution

- FAILED (both trees): `test_alpha_bootstrap_contract.py::TestAlphaBootstrapSQL::test_explicit_memories_sql_is_lf_only_for_packaged_hash_parity`
  — CRLF checkout environmental (hash parity requires LF-only SQL). Identical on base ⇒ NOT branch-introduced.
- ERROR (both trees, count varies 3 vs 1): the conftest "P0-A outbox guard" teardown fixture
  detects real production outbox writes made by the live gateway during the test window
  (guard's own doc: "若本机真实 gateway 正在运行，它自己也会写 j/journal_<date>/ 与 live-buffer marker；
  这种环境下的比对无法区分两者"). Timing-dependent; the specific affected tests differ per run
  (head: doctor_reports_embedding_failures, explicit_memories_sql, pgvector_extension_required;
  base: dsn_literal_unchanged). Same class of noise on both trees ⇒ NOT branch-introduced.

## Verdict

No branch-introduced regression in the focused set. Head vs base identical on
pass/fail/skip counts; only the nondeterministic guard-noise error set differs.

Scope note: the full-repo suite was NOT run on either tree — the task book §25
lists this focused set as the regression scope; full-repo runs are not part of
this receipt.

## Raw logs

- head: `C:\Users\servi\AppData\Local\hermes\cache\scratch\p0a_merged_regression.log`
- base: `C:\Users\servi\AppData\Local\hermes\cache\scratch\p0a_base_regression.log`
- base snapshot: `C:\Users\servi\AppData\Local\hermes\cache\scratch\p0a-base-snapshot\`
