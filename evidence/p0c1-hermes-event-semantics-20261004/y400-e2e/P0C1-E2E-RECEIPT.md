# P0-C1 — Hermes Host Event Semantics · Y400 real-PG E2E receipt

Run host: `DESKTOP-EQP3OBU` (SSH alias `y400`)
Date: 2026-10-04
Design contract: `docs/P0C1-HOST-EVENT-SEMANTICS.md`
Source under test: `C:\hp-testbed\p0c1-hermes-event-semantics-20261004\src\v3-core`
(working tree of branch `fix/p0c1-hermes-event-semantics`, incl. new
`src/v3core/host_events.py` and the modified `src/v3core/__init__.py`).

## Verdict

**PASS — 89 / 89 checks, 0 failures** (`p0c1-e2e-report-20261004-233507.json`,
`DRIVER_RC=0`).

Baseline: NO LLM / NO EMBEDDING (`safe_embed_cfg(config) is None`; the
`LiveBuffer embedding degraded … NO_INPUT` lines are the expected
no-embedding-baseline short-circuit, not errors).

## Disposable target / production mutation

| item | value |
|---|---|
| PG host / port | `127.0.0.1:55432` (isolated cluster) |
| database | `p0c1e2e_20261004` |
| user | `f2e2e` |
| password in report | **false** |
| production mutation | **NONE** — the only DB written is `p0c1e2e_20261004`; a fail-closed target gate (port 5433 / `v3embeddings` refused) plus a process-wide `psycopg2.connect` guard block any production connection |
| retention | DB **kept** for inspection (reset via `p0c1-reset-db.ps1`) |

## Scenario results (task book §13)

| # | scenario | result | key ground truth (read back from PG) |
|---|---|---|---|
| S1 | normal QA | PASS | 1 QA `Q1 normal` → `A1-normal-answer`; source u1/a1/u2 = 1 row each |
| S2 | multi assistant | PASS | 1 QA, each of 3 assistant segments once; replay `accepted=[]`, `duplicate` reported; source 5 rows |
| S3 | compaction continuation | PASS | 1 QA `Q1 compaction` folds A1+A2+A3; `[CONTEXT COMPACTION` excluded; CTX **not** written to source |
| S4 | **compaction after flush (BLOCKER)** | PASS | 1 QA (no 2nd row); answer `A1-pre-flush\nA2-post-compaction-late`; `merged_event_ids=[a1,a2]`; A2 durable-appended via snapshot anchor |
| S5 | OOB user | PASS | raw source keeps the full `[OUT-OF-BAND USER MESSAGE …]` wrapper; derived Q = payload `改成 B`; A2 pairs to the steer, **not** folded into the pre-steer QA |
| S6 | control transparency | PASS | 1 QA folds A1+A2; `[System: …]` excluded from Q/A and **not** written to source |
| S7 | async contamination guard | PASS | only 1 (human) QA; `A_ASYNC-autonomous-report` **not** in any QA answer, though its source row is durable (1 row); boundary marker not written to source |
| S8 | failed_turn exclusion | PASS | failed-turn notice never an answer/question; **not** written to source |
| S9 | replay | PASS | snapshot ×3 → 1 QA, A2 once, `merged_event_ids=[a1,a2]` no dup, source u1/a1/a2 = 1 row each; replays #2/#3 `accepted=[]` |
| S10 | restart | PASS | new `V3Core` over same DB re-syncs the full snapshot → still 1 QA, `A1-restart\nA2-restart-late`, `merged_event_ids=[a1,a2]` no dup, source 3 rows |

Aggregate ground truth: `qa_pairs` total = **11**, `conversation_stream` total = **33**.

## Files

| file | role |
|---|---|
| `p0c1_e2e.py` | E2E driver (real V3Core + real PG, 10 scenarios, 89 checks) |
| `p0c1-run-e2e.ps1` | runner (bootstrap + driver, tees console log) |
| `p0c1-reset-db.ps1` / `p0c1_reset_db.py` | drop+create the disposable DB |
| `p0c1-stage-db.ps1` / `p0c1_stage_db.py` | list/create the disposable DB |
| `p0c1-verify.ps1` / `p0c1_verify.py` | independent PG ground-truth readback |
| `p0c1-e2e-report-20261004-233507.json` | **authoritative** structured result (verdict PASS) |
| `p0c1-db-groundtruth.json` | independent DB readback (per-session QA + source counts) |
| `e2e-console.log` | full console transcript of the PASS run |
| `e2e-runs/` | kept isolated run root (HOME/profile/data + durable markers) |
| `p0c1-e2e-report-20261004-233339-driver-assertion-bug-superseded.json` | first run: 88/89, the single FAIL was a **driver assertion bug** (expected the async output in the receipt `held` bucket; the product correctly reported it under `accepted` with no QA derivation and no pollution). Fixed in the driver; product behaviour unchanged. |

## Reproduce

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:/hp-testbed/p0c1-hermes-event-semantics-20261004/p0c1-reset-db.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File C:/hp-testbed/p0c1-hermes-event-semantics-20261004/p0c1-run-e2e.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File C:/hp-testbed/p0c1-hermes-event-semantics-20261004/p0c1-verify.ps1
```
