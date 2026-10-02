# B04 — DSH automatic-memory adapter: isolated integration run

Run: **2026-10-02**, machine `DESKTOP-EQP3OBU` (the dedicated test laptop; the driver refuses any
other hostname), Node `v24.21.0`.

## Result

**VERDICT = PASS — 16/16 checks, 0 failures** (`b04-integration.json`).

| # | Check | Observed |
| --- | --- | --- |
| 1 | `schema_bootstrapped` | CLI bootstrap rc=0, applied=true, guarded target |
| 2 | `bridge_health_ok` | `health=true` |
| 3 | `bridge_embed_disabled` | `embed=false` (keyword-only scope) |
| 4 | `seed_ingested` | 3 seed events, all `accepted` (HTTP 200) |
| 5 | `seed_pair_committed` | 1 `qa_pairs` row carries the seeded marker |
| 6 | `seed_recallable` | keyword prefetch returned 349 chars, marker present |
| 7 | `adapter_row_mounted` | `--dump-config` mentions the adapter row |
| 8 | `run1_exit_zero` | real DSH CLI run rc=0 |
| 9 | `run1_session_id` | `session-7500d871-76ec-42c1-b3ac-fb454b302a46` |
| 10 | `capture_user_and_assistant` | 2 durable rows, roles `user` + `assistant` |
| 11 | `capture_used_native_event_id` | ids are the DSH `MessageId`s, not derived |
| 12 | `injected_memory_not_recaptured` | 0 rows contain the injected memory text |
| 13 | `reload_exit_zero` | same session resumed, rc=0 |
| 14 | `reload_no_duplicate_source` | rows 2→4, duplicate `(role,event_id)` rows = 0 |
| 15 | `memory_in_same_request` | 2 of 6 recorded model requests carried a memory block |
| 16 | `memory_carries_seeded_fact` | both blocks carry the seeded fact |

The core claim — *a recalled block reaches the model request of the same turn* — is visible in
`stub-requests.jsonl`: each matched record carries, verbatim,

```
[hippocampus memory — recalled context, not a new user fact]

【记忆召回 — v3 主动注入相关记忆片段】
...
- qa_1 | What is the b04-isolated-marker-7f3a deploy window [qa] (score=0.605)
  [2026-10-02 13:56:54.119727+00:00] Q: What is the b04-isolated-marker-7f3a deploy window?
A: The b04-isolated-marker-7f3a deploy window is on Thursdays at 02:00.
```

## What is real here, and what is not

Real: the DSH CLI (`@deepseek-ai/dsh@0.2.0-rc.2`), this adapter package, the B01 bridge
(`python -m v3core serve`), PostgreSQL 17 on `127.0.0.1:55432`, and the CLI's own profile
materialization + `--dump-config` composition.

Deliberately substituted, and **not** covered by this run:

- **The model endpoint is a local stub** (`packages/dsh-adapter/eval/stub-model-server.mjs`). There
  is no model credential on this machine (`models-store.json` is 2 bytes; every provider env var is
  absent), so the run proves *what was sent to the model*, not *what a model would answer*.
- **Embeddings are disabled** (`storage.embed.endpoint` and `.model` both empty → v3core's documented
  keyword-only path), so recall here is keyword-based. A live embedding endpoint was not exercised.
- **Failure injection** (bridge down, protocol mismatch, oversize block, bad config) is covered by
  the unit layer, not by this run.

## Reproduction

1. On the test laptop: install `@deepseek-ai/dsh@0.2.0-rc.2` into a scratch root, and put the
   disposable v3core (installed CLI + `hippocampus.exe`) next to it.
2. Copy `packages/dsh-adapter/` and this directory's `b04_integration.py`,
   `b04-run-on-y400.ps1`, plus the package's `eval/stub-model-server.mjs` into one staging root.
3. Create an empty run root and invoke:

   ```
   powershell -NoProfile -ExecutionPolicy Bypass -File b04-run-on-y400.ps1
   ```

   The driver refuses a non-empty run root, a hostname other than `DESKTOP-EQP3OBU`, any PG target
   other than `127.0.0.1:55432`, and any database name without the `b04dsh_` prefix. The PG password
   is read from a secrets file into the process environment only — never argv, never the report.

## Files

| File | sha256 | What it is |
| --- | --- | --- |
| `b04-integration.json` | `52591a3b769849b40b1969b00f5e9a9dc014f13d9b0d91cd5d70057db8b5e0ea` | the machine-readable report |
| `b04_integration.py` | `dc70777e6644df9fc0ea491330cff24364adf2309856284c6d29eaf4053e1c71` | the driver (hash printed by the run itself as `HASH_HELPER`) |
| `b04-run-on-y400.ps1` | `da3825ffdd7aea4cf1bc4d7117df99085fcf954e7addd7a97878d804b536c894` | the wrapper (hostname gate) |
| `stub-requests.jsonl` | `bd12faf3eb328967ba86900aa83bbfa584403b5cbd40240b4acde4e7022f66b1` | what the model endpoint received |
| `bridge.log` | `e648b50efe3f40c92c6681e4ae76d6efa700b3dd7ca1121214f5897f7a0de8a7` | bridge-side log |
| `run1.stdout.jsonl` | `a0ffa747d79fbee0ddee562503c7868fc81b2a06f2d2b372932d0fa445966368` | first DSH turn (`--json` stream) |
| `run2.stdout.jsonl` | `751f08d216302e59c7230a213d2c4a6e0383f452f53b87cf5da4b9ecbb9acdc1` | reloaded-session turn |
| `profile-config.yaml` | `fc341286c68d11e14112ae8eb09b1e1f0adc6a986cce09b67033968959ee0ede` | the disposable core config (no credentials) |

`stub-requests.jsonl` is append-only; the report's request counts are read after teardown, so the
counts and this file describe the same completed run.
