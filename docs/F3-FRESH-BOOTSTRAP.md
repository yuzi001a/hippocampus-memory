# F3 — fresh bootstrap creates the embedding-failure ledger

Status: **DONE — PR #26 merged** at
`9d8101a4f48e0f2969ee216a11b8458b7ecb1f94`.
Final review head: `ee7954bcc7f6be95e43a94ebf1b1599d8825ab1a`; product-ci and
Windows distribution-packaging smoke both SUCCESS on that exact head. Installed
packaging tests: **15 passed, zero skipped**; static doctor rc0. Remote main and
merge ancestry independently read back. No production deployment.

The local evidence below was captured before CI; its pending labels are historical.
Base: `3f6adc6c2e77be983bb7ccfd1851d26ff23bf475` (accepted F2 merge).
Branch: `fix/f3-fresh-bootstrap-embedding-failures`.
This is isolated installed-wheel evidence, not a production deployment.

## Defect and minimal product change

The canonical `schema/embedding_failures.sql` existed in the repository,
but was neither a packaged resource nor included in normal bootstrap.
The old installed wheel reported successful bootstrap while leaving the table absent.

- Ship a byte-identical copy of that existing DDL in `v3core/schema/`.
- Add the include marker before bootstrap's `COMMIT`, in both SQL copies.
- Declare the resource in package-data and the doctor's packaged SQL check.
- Add `embedding_failures` to the doctor's required database tables.
- Update the old evaluator's single-table classification and add two scoped regressions.

No migration-system redesign, installer refactor, or adjacent-table audit.
F2's runtime, upgrade SQL and identity-index definition remain unchanged.

## Actual verification scope

The parent inspected the actual product diff, compared the baseline wheel's 127
package members with canonical base source and the candidate wheel's 128 members
with current source (line-ending-normalized), then used fresh wheel installations
on the dedicated Windows laptop with disposable loopback PostgreSQL.
The database names are allowlisted; existing databases and existing report files
are refusals, never dropped or reused. Passwords stay in process environment.

The pre-product-change RED is retained as chronological evidence. The parent
independently repeated RED/GREEN using the final frozen tracked probe:
`src/v3-core/eval/f3_fresh_bootstrap_e2e.py`.

| Gate | Parent-observed result |
|---|---|
| Baseline fresh RED | Two normal bootstrap calls succeeded; ledger absent before/after both |
| Candidate fresh GREEN | Ledger present after first bootstrap, before any writer |
| Indexes | Three required indexes plus primary key present |
| Second bootstrap | Succeeded; table/index facts unchanged |
| Actual failure writer | Returned true; exact synthetic timeout row read back |
| Doctor | Required table true; zero errors |
| Product-shaped local gates | 430 core passed, 5 real-PG skips; 102 evaluator and 12 plugin passed |
| Final scoped F3 regressions | 2 passed, 55 deselected |
| Final probe guards | Unknown database, wrong expectation and non-dedicated host refused |

Doctor returns **WARN / exit 1**, not clean PASS: these fresh environments install
only the engine, so the Hermes adapter/entry point and configured profile are
missing. Those three warnings are recorded; the F3 table check passes and errors
are empty. The writer receives a synthetic `TimeoutError`; no provider call is made.

The five local PG skips were not newly executed for F3; the accepted F2 laptop
run covered them. The F2 W matrix was not repeated. No full repository-wide suite
or unrelated bootstrap-table scan was performed.

## Deviations and invalid attempts — preserved, not hidden

- The first child's probe bug consumed an empty RED database. It independently
  switched to `red2`; that naming deviation is recorded, not retroactively authorized.
- The initial probe lacked hard admission/verdict gates. The parent retained the
  minimal product change but required probe-only repair before acceptance.
- The parent's first RED completed all DB/CLI observations and wrote its report,
  then failed while printing through GBK. It is excluded as a completed driver
  gate. Five probe lines were added for UTF-8 and a separately authorized new RED
  database. The final RED and GREEN drivers both exited 0; reports contain no
  replacement characters.
- Probe grew beyond its advisory line-count target; its checks were not discarded
  merely to meet that target. It is an isolated verifier, not runtime machinery.
- A duplicate local broad gate was started unnecessarily, then explicitly stopped.
  Its partial output contributes **no** acceptance evidence.
- Local setup also had corrected command quoting/hash-input mistakes and a
  blocked oversized inline command. They were not product results. The blocked
  payload was reviewed and run through the tool's documented script recovery.

## CI-only regression follow-up

The first PR head's product-ci passed, but installed-package smoke failed in two
existing tests: they rejected any occurrence of `ALPHA_BOOTSTRAP_INCLUDE`, including
a harmless explanatory SQL comment. Parent reproduction: 2 failed / 13 deselected.
The assertions now reject actual marker lines, with a raw-marker precondition;
normal comments are allowed. Product/wheel bytes are unchanged. Local packaging
file: 13 passed, 2 host-plugin skips; those two must execute in repository CI.
The failed old-head CI is preserved, not counted as current acceptance.

Sanitized evidence: `evidence/f3-fresh-bootstrap/parent-acceptance.json`.
Raw reports, complete console output and invalid attempts remain in the
owner-controlled incident archive. No production mutation or restart occurred.
