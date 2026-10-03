# I01 — one-click history import: isolated integration run

Run: **2026-10-03**, machine `DESKTOP-EQP3OBU` (the dedicated test laptop; the driver refuses any
other hostname), Python `3.11.16` (`i01-venv`), v3core imported from
`i01-src/src/v3-core/src/v3core/__init__.py` (git archive of
`af3c971a06765f7ff7ecae0ce3129deb0368c9e8`, tar SHA-256
`df919d8f46acf4dbec19a3fd27a77fb68317ec2e57a5b09c9cf353d1a6d06b45`).

## Result

**VERDICT = PASS — 42/42 checks, 0 failures** (`i01-integration.json`).

### Staging & discovery

| # | Check | Observed |
| --- | --- | --- |
| 1 | `hermes_fixture_generated` | real-schema synthetic `state.db` written (20 480 bytes) |
| 2 | `pi_fixture_staged` | 1 pi session file (produced by the official pi SDK `1.0.0`) |
| 3 | `dsh_fixture_staged` | 2 DSH session files |
| 4 | `dsh_fixture_byte_identical_to_b04_originals` | 2/2 files byte-identical to the DSH CLI's own session store |
| 5 | `v3core_resolves_to_i01_source` | import path = `i01-src/...` (not an installed leftover) |
| 6 | `schema_bootstrapped` | CLI bootstrap rc=0, `applied=True` |
| 7 | `dry_run_rc0` | dry-run exit 0 |
| 8–14 | `dry_run_found_*` / `dry_run_*_messages` | all 4 hosts found; hermes 9, dsh 6, pi 6 messages — the exact fixture counts |
| 15 | `dry_run_zero_raw_written` | `raw_imported=0` |
| 16 | `dry_run_wrote_nothing` | `conversation_stream` rows after dry-run = 0 |

### Live import

| # | Check | Observed |
| --- | --- | --- |
| 17 | `live_rc0` | live import exit 0 |
| 18 | `live_raw_imported` | **21 raw rows** = 9 (hermes) + 6 (dsh) + 6 (pi) |
| 19 | `live_qa_pairs_derived` | **9 QA pairs** = 3 + 3 + 3 |
| 20–22 | `stream_rows_hermes/dsh/pi` | 9 / 6 / 6 — matches the source counts exactly |
| 23 | `native_identity_present` | rows missing `host`/`event_id` = 0 |
| 24 | `pi_native_event_id` | `event_id='9921d832'` — pi's own entry id, not derived |
| 25 | `dsh_native_event_ids` | 6 rows carry DSH's native UUID `MessageId`s |
| 26–27 | `qa_pairs_total` / `qa_source_id_format` | 9 rows, all `qa_import/…` prefixed |
| 28–30 | `qa_content_hermes/dsh/pi` | markers `银杏` / `b04-isolated-marker-7f3a` / `盘位核对表` found in the imported QA text |
| 31–32 | `memory_md_*` | 2 curated notes written; both visible in `explicit_memories` |
| 33 | `self_memory_excluded` | 0 rows contain Hippocampus's own injected-memory text |

### Idempotency & source integrity

| # | Check | Observed |
| --- | --- | --- |
| 34 | `rerun_stream_unchanged` | `conversation_stream` 21 → 21 |
| 35 | `rerun_qa_unchanged` | `qa_pairs` 9 → 9 |
| 36 | `rerun_deduped_reported` | second run reported `duplicates_skipped=32` (≥ 21 raw + 9 QA + curated) |
| 42 | `source_files_unchanged` | every staged source file byte-identical after two live runs (`changed=[] added=[]`) |

### Recall (new-session path)

| # | Check | Observed |
| --- | --- | --- |
| 37 | `bridge_health_ok` | `health=True` (keyword-only scope) |
| 38 | `recall_hermes` | `prefetch('银杏')` → 981-char block, marker present |
| 39 | `recall_dsh` | `prefetch('b04-isolated-marker-7f3a')` → 654-char block, marker present |
| 40 | `recall_pi` | `prefetch('盘位核对表')` → 981-char block, marker present |
| 41 | `source_trace_complete` | `qa_import/pi/01a0fdab-…/9921d832` → matching `conversation_stream` row, **verbatim** |

The core claim — *imported history becomes recallable in a fresh session* — is check 38–40: the
bridge's `/prefetch` (the exact path a new session uses to pull memory) returns blocks containing
the imported facts for all three agent hosts, and check 41 shows the QA pair traces back to the
original event id in the source file.

## What is real here, and what is not

Real: the DSH `0.2.0-rc.2` session files (produced by the real CLI on this machine, byte-compared
to the shipped fixture), the pi `1.0.0` session file (produced by the official
`@earendil-works/pi-coding-agent` SDK), PostgreSQL 17 on `127.0.0.1:55432` (disposable database
`i01e2e_20261003c`), the full `hippocampus import auto` CLI path (discovery → parse → raw write →
QA derivation → report), the B01 bridge (`python -m v3core serve`), and the idempotent second run.

Deliberately substituted / not exercised:

- **The Hermes source is synthetic** (generated on the live `state.db` schema by
  `make_fixture.py`), because this machine's real Hermes history lives on the user's production
  machine and must not be touched. The *parser* runs against the real schema; the *content* is
  synthetic.
- **No LLM and no embedding endpoint were called** — by design. The import is deterministic and
  model-free, and recall here is v3core's documented keyword-only path (`embed.endpoint` empty).
- The imported QA pairs deliberately have **no embeddings**; embedding backfill is the normal
  indexing flow's job, not the import's.
- **Topic-card recall is not exercised**: the imported dataset contains no topic cards, and the
  bridge's `topic_recall` probe logs `no data source` in this isolated profile (it opens its own
  PG connection independently of the import path). The recall checks above run on the QA path —
  the path this milestone delivers.

## Reproduction

1. On the test laptop, stage `i01-src` (git archive of the branch HEAD) and create `i01-venv`
   (`pip install -e i01-src/src/v3-core`).
2. Create an empty run root and invoke:

   ```
   i01-venv\Scripts\python.exe i01_integration.py ^
       --root <empty-run-root> --source-root <i01-src> --env-python <i01-venv python> ^
       --secrets <pg-password-file> --db-name i01e2e_<stamp> --out <run-root>\i01-integration.json
   ```

   The driver refuses a non-empty run root, a hostname other than `DESKTOP-EQP3OBU`, any PG target
   other than `127.0.0.1:55432`, and any database name outside the `i01e2e_` prefix.
3. Expected: `VERDICT=PASS`, 42 checks, 0 failures.

## Files

- `i01_integration.py` — the driver (SHA-256 `56cf8332729ab3d40089ca19893e6c0af5be13a332083597e9ac7591c7aa7590`).
- `i01-integration.json` — full report: checks, observed values, per-source reports.
- `import-dry-run.stdout.json`, `import-live-1.stdout.json`, `import-live-2.stdout.json` — the
  three CLI reports (dry-run / live / idempotent re-run), verbatim.
- `import_report.json` — the report the live run persisted to the profile directory.
- `bridge.log` — bridge startup + the three prefetch requests.
- `profile-config.yaml` — the isolated config used (no credentials; empty endpoints).
