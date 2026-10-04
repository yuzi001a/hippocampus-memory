# P0-C1 offline replay — BEFORE vs AFTER (real historical export)

Generated: 2026-10-04 23:39:15 · interpreter `C:/hp-testbed/.venv/Scripts/python.exe`
Source (read-only): `C:/Users/servi/AppData/Local/hermes/cache/scratch/p0c_tax_rows.pkl`
Corpus: 688,934 state.db rows → dedup 358,483 messages / 3,568 sessions.
No PG, no DB write, no worktree edit, no network. AFTER uses the real `src/v3-core/src/v3core/host_events.py` classifier (imported, not re-implemented).
Same dedup + same turn-split + same 60 s watchdog skeleton for both models, so the only variable is the pairing policy. Driver: `p0c1_offline_replay.py` (this dir).

## Headline (§14)

| metric | BEFORE (HEAD) | AFTER (P0-C1) | verdict |
|---|---:|---:|---|
| CTX orphan | 2,412 | 933 | −1,479 (residual = boundary-shadowed, see 口径) |
| CTX misattr | 1,276 | 0 | ≈0 ✅ |
| OOB skipped (real-user rows dropped) | 92 | 0 (all 92 opened) | ≈0 ✅ |
| OOB misattr | 751 | 0 | ≈0 ✅ |
| CONTROL-induced pairing drift (rows opening a QA) | 473 | 0 | 0 ✅ |
| ASYNC contamination (folded into pre-boundary QA) | 1,290 | 0 | 0 ✅ |
| **normal-dialogue pairing regression** | — | **0** | **= 0 ✅** |
| **total orphan + misattr** | **10,747** | **7,459** | −3,288 |

Normal-dialogue assistants checked: 37,653 (turn-start row = plain `REAL_USER` under the new classifier **and** non-injection under the old). Regression = 0. Assistants in normal turns that the new classifier re-labels (display-only / compaction products) and therefore no longer appends: `{"TERMINAL": 760, "CONTINUATION": 35}` — an intended exclusion, not a dialogue regression. Turns excluded as non-dialogue (empty user row, no question): 2.

## orphan / misattr by turn-start family

| turn-start family | BEFORE orphan | AFTER orphan | BEFORE misattr | AFTER misattr |
|---|---:|---:|---:|---:|
| ASYNC_BATCH_COMPLETE | 2,243 | 2,803 | 576 | 0 |
| ASYNC_OTHER | 7 | 15 | 8 | 0 |
| BG_PROCS_COMPLETE | 60 | 148 | 91 | 0 |
| BG_PROC_COMPLETE | 978 | 1,584 | 614 | 0 |
| BG_SUBAGENTS_COMPLETE | 1 | 2 | 1 | 0 |
| CRON_JOB | 200 | 198 | 0 | 0 |
| CTRL_EMPTY_RESPONSE | 0 | 0 | 12 | 0 |
| CTRL_MAX_TOOLS | 0 | 10 | 0 | 0 |
| CTX_COMPACTION | 2,370 | 919 | 1,276 | 0 |
| CTX_STILL_IN_PROGRESS | 42 | 2 | 0 | 0 |
| CTX_TASKLIST | 0 | 12 | 0 | 0 |
| OUT_OF_BAND | 5 | 0 | 751 | 0 |
| REAL_USER | 0 | 32 | 0 | 0 |
| SESSION_START | 5 | 5 | 0 | 0 |
| SKILL_INVOKED | 1,507 | 1,504 | 0 | 0 |
| SYS_CUT_OFF | 0 | 72 | 0 | 0 |
| SYS_MODEL_SWITCH | 0 | 103 | 0 | 0 |
| SYS_REASONING_ONLY | 0 | 1 | 0 | 0 |
| SYS_TOOL_ISSUE | 0 | 37 | 0 | 0 |
| TITLE_GEN | 0 | 12 | 0 | 0 |
| **total** | **7,418** | **7,459** | **3,329** | **0** |

## structural buckets (orphan + misattr reclassified)

| bucket | BEFORE | AFTER | Δ |
|---|---:|---:|---:|
| continuation | 3,688 | 933 | -2,755 |
| async_completion | 4,579 | 4,552 | -27 |
| control | 768 | 235 | -533 |
| new_root | 1,712 | 1,707 | -5 |
| unknown | 0 | 32 | +32 |

## 口径 (methodology) notes — read before quoting numbers

- **BEFORE** = the prior-round faithful replication (`p0c_tax_e.py`) of the HEAD binary injection skip; its skip-surface is `prior_is_inj` (prefix tuple incl. `[STILL IN PROGRESS`, `[Hermes`, `[Reminder`, `[Note`). This is the calibration that reproduces the anchor 10,747 = orphan 7,418 / misattr 3,329. Two known 口径 gaps vs the literal HEAD `_injection_pattern`: (a) HEAD's regex also skips `[System:…` (model-switch) whereas this replication treats it as a real-user turn → counted here under *drift*, not *skipped*; (b) HEAD applies the pattern to **every** row incl. assistant-role compaction products, while this replication tests only turn starts. Neither gap changes any AFTER number.
- **AFTER** models the flattened session as a re-sent full snapshot (Hermes re-sends the full history each sync and `sync_turn` re-classifies every row *before* the delta filter), so the snapshot anchor = most recent `REAL_USER` up to that point; the 60 s watchdog is modelled identically and only removes `pending`, which §4 structural continuation recovers. This was verified against the **real** `sync_turn` (in-memory FakePg harness — appendix).
- **'orphan' vs 'misattr' (AFTER):** a QA-less autonomous output is an *orphan* (held; source already durable in `conversation_stream`), never a *misattr* — the boundary is cut first. AFTER misattr is 0 by construction and measured 0 here.
- AFTER `terminal_excluded` (failed_turn / hidden / interrupted display-only) = 847 assistant rows never enter an answer; BEFORE counted them as ordinary answers.
- Total orphan does **not** reach 0 by design: autonomous (async / cron / skill) outputs after a boundary are held orphans — durable delivery is P0-C2, not this round (§3/§6). The measured win: **all 3,329 misattributes, all contamination and all control drift are gone**, and 1,479 CTX continuations are now correctly paired.

### Why 933 CTX orphans remain (measured reasons)

`{"anchor_none": 15, "boundary_shadowed": 918}`

- `boundary_shadowed` — an `ASYNC_BOUNDARY` / `NEW_ROOT` row occurred after the last `REAL_USER` and before the continuation row; §4's `struct.boundary` short-circuit then forbids folding the post-compaction assistant into that QA (correct: it must not be guessed into a possibly-unrelated human QA). Empirically confirmed against the real `sync_turn`.
- `anchor_none` — the continuation turn has no preceding `REAL_USER` in the session.

**Sensitivity variant** (NOT the implemented behaviour): if a `CONTINUATION` row were also to clear `_struct_boundary`, CTX orphan would fall to 15 (total orphan 6,522). The shipped implementation does **not** do this (§3: CONTINUATION is fully transparent), so the primary AFTER column keeps the boundary-shadowed orphans. This is the one gap between the design's “CTX orphan ≈ 0” wording and measured behaviour — flagged for P0-C2 / contract clarification.

## Appendix — real-`sync_turn` probe (boundary semantics)

Driven through the in-memory `FakePg` harness of `tests/test_p0c1_host_event_semantics.py` (`C:/Users/servi/AppData/Local/hermes/cache/scratch/p0c1_probe_boundary.py`):

| snapshot sequence | real result |
|---|---|
| `U1, A1, ASYNC, A2, CTX, A3` | QA(U1) = `A1-answer` only — **A2 and A3 held orphans**; the boundary survives the CONTINUATION row |
| `U1, A1, CTX, A2` (no async) | QA(U1) = `A1-pre\nA2-post` — continuation folded ✅ |
| `U1, A1, ASYNC, A2, U2, A3, CTX, A4` | QA(U1)=`A1`; QA(U2)=`A3-answer2\nA4-post-ctx` — the intervening REAL_USER clears the boundary ✅ |
