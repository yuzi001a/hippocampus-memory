# P0-C1 evidence — `test_p0c1_host_event_semantics.py` RED run (2026-10-04)

Deliverable: `src/v3-core/tests/test_p0c1_host_event_semantics.py`
Branch: `fix/p0c1-hermes-event-semantics` · base `21d2bde1`
Interpreter: `C:/hp-testbed/.venv/Scripts/python.exe`

## Files

| file | what |
|---|---|
| `red-run.txt` | **required RED record.** Current working tree, no stubs: `31 failed`. |
| `behavioural-red-vs-HEAD.txt` | T-matrix vs the **pre-fix HEAD** `__init__.py`, contract-correct classifier supplied out-of-tree: `8 failed / 4 passed`. Proves the tests detect the fix. |
| `behavioural-green-current-worktree.txt` | T-matrix vs the **current worktree** `__init__.py` with the same out-of-tree classifier: `12 passed`. |
| `p0c1_stub_classifier.py` | diagnostic pytest plugin (`-p p0c1_stub_classifier`) implementing contract §2.1/§2.2. Writes nothing into `src/`. |

## The required RED (`red-run.txt`) — 31 failed

The adapter rewrite already in the working tree (`src/v3-core/src/v3core/__init__.py`,
uncommitted, +123/−37) imports at `sync_turn` entry:

```
from .host_events import (HOST_EVENT_ASYNC_BOUNDARY, HOST_EVENT_CONTINUATION,
                          HOST_EVENT_CONTROL, classify_host_event,
                          extract_oob_payload, ...)
```

`src/v3-core/src/v3core/host_events.py` (contract §2) does **not exist yet**, so every
T1–T11 test aborts at `__init__.py:3602` with
`ModuleNotFoundError: No module named 'v3core.host_events'` before its assertion, and every
§2 classifier test hits the module-level fallback stub. Same root cause makes the
**pre-existing** `test_p0a_late_assistant_derivation.py` red too (`6 failed / 1 passed`,
all `ModuleNotFoundError`) — the branch is not in a runnable state until `host_events.py`
lands.

## The behavioural RED (`behavioural-red-vs-HEAD.txt`) — 8 failed / 4 passed

Run against the pre-fix `__init__.py` (copied to a scratch dir; the working tree was never
touched) with a contract-correct classifier, so the adapter logic is the only variable.

| case | pre-fix | why (measured) |
|---|---|---|
| T1 normal | PASS | regression guard |
| T2 multi-assistant | PASS | regression guard |
| T3 CTX_COMPACTION | PASS | already matched by legacy `_injection_pattern` → skipped |
| T4 after-flush continuation | **FAIL** | A2 accepted to source but never folded into the durable QA (`answer='A1-pre-flush'`) — the blocker |
| T5 tasklist / still-in-progress | **FAIL** | not in the legacy pattern → treated as a REAL user turn → second QA whose *question* is the marker text |
| T6 SYS_MODEL_SWITCH | PASS | already matched (`[System`) → skipped → transparent |
| T7 OUT_OF_BAND steer | **FAIL** | steer reply folds into the previous human QA (`a='A1-original\nA2-answer-to-steer'`); no new QA, payload lost |
| T8 ASYNC_BATCH_COMPLETE | **FAIL** | autonomous output contaminates the previous human QA |
| T9 BG_PROC_COMPLETE | **FAIL** | same contamination |
| T10 failed_turn | **FAIL** | the display-only notice becomes the QA answer |
| T11 replay | **FAIL** | replayed late event never merged |

## Contract observations / tensions

1. **§1 table is wrong for two of the three CTX_* families.** It lists
   `CTX_COMPACTION / CTX_TASKLIST / CTX_STILL_IN_PROGRESS` as "skip". Measured: only
   `CONTEXT COMPACTION` is in the legacy `_injection_pattern`; tasklist and
   still-in-progress are **not** and were treated as real user turns (second QA). T3 is
   therefore a regression guard while T5 is genuinely RED.
2. **T8/T9 depend on the async assistant's turn identity.** With a `turn_id`, the pre-fix
   `qa_pairing_decision` already returns `hold_orphan` → no pollution; pollution only
   happens when the output carries no turn identity (or shares the pending turn). T8/T9 use
   the no-`turn_id` shape, which is the case §1 describes and the case that measurably
   pollutes.
3. **T4 interpretation (sharpest point).** §4(b) says a continuation resolves against the
   **snapshot anchor** (the REAL_USER row), not the assistant's own `turn_id`. T4's A2
   deliberately carries **no** `turn_id`, so a P0-A-style implementation keyed on
   `event_turn` would still fail. The current worktree implementation agrees with this
   reading (T4 passes).
4. **Fixture interface is not defined by the contract.** §5/§12 name
   `tests/fixtures/host_events_p0c1.py` but not its exported names. The test file imports it
   tolerantly (`_fx(*names, default)`) using the taxonomy report's canonical event-type
   names (`CTX_COMPACTION`, `CTX_TASKLIST`, `CTX_STILL_IN_PROGRESS`,
   `ASYNC_BATCH_COMPLETE`, `BG_PROC_COMPLETE`, `OUT_OF_BAND`/`OOB_STEER`,
   `SYS_MODEL_SWITCH`, `FAILED_TURN`, `HIDDEN`, `TITLE_GEN`, `CRON_JOB`, `SKILL_INVOKED`,
   `JUDGE_PROMPT`) and falls back to the real documented marker strings from the taxonomy
   report. `test_c0_...` asserts the fixture module *imports*, not that it exports those
   names — a name mismatch would silently fall back.

## Reproduce

```bash
cd C:/hp-testbed/src/v3-core
V3_HARNESS_ALLOW_LIVE_PROD_OUTBOX=1 C:/hp-testbed/.venv/Scripts/python.exe \
  -m pytest tests/test_p0c1_host_event_semantics.py -rA -q --tb=line

# behavioural (out-of-tree classifier, current worktree):
V3_HARNESS_ALLOW_LIVE_PROD_OUTBOX=1 \
PYTHONPATH="C:/hp-testbed/evidence/p0c1-hermes-event-semantics-20261004" \
  C:/hp-testbed/.venv/Scripts/python.exe \
  -m pytest tests/test_p0c1_host_event_semantics.py -p p0c1_stub_classifier -k test_t -q
```

`V3_HARNESS_ALLOW_LIVE_PROD_OUTBOX=1` is required only because a live Hermes gateway on this
host concurrently writes `~/.v3-core/profiles/default/j`, which conftest defence 3b cannot
distinguish from the test's writes; it relaxes no behavioural assertion.

## §14 offline replay (real historical export, read-only)

| file | what |
|---|---|
| `p0c1_offline_replay.py` | the replay driver (imports the real `host_events.py`; no PG / no DB / no worktree write) |
| `offline-replay-before.json` | BEFORE = HEAD binary injection skip (reproduces the anchor 10,747 = orphan 7,418 / misattr 3,329) |
| `offline-replay-after.json` | AFTER = current-worktree classifier + §3/§4 semantics |
| `offline-replay-compare.md` | before/after comparison table, 口径 notes, real-`sync_turn` probe appendix |

Corpus: `C:/Users/servi/AppData/Local/hermes/cache/scratch/p0c_tax_rows.pkl`
(688,934 rows → 358,483 dedup / 3,568 sessions). Headline: misattr 3,329 → 0,
CTX misattr 1,276 → 0, OOB skipped 92 → 0, control drift 473 → 0, async
contamination 1,290 → 0, **normal-dialogue pairing regression = 0** (37,653
assistants checked); total orphan+misattr 10,747 → 7,459. Residual CTX orphan
933 is boundary-shadowed (§4 short-circuit, empirically confirmed against the
real `sync_turn`); a CONTINUATION-clears-boundary variant would give 15 — see
the compare md 口径 section.
