# B04 — DSH automatic-memory adapter: isolated integration run

Run: **2026-10-02**, machine `DESKTOP-EQP3OBU` (the dedicated test laptop; the driver refuses any
other hostname), Node `v24.21.0`.

## Result

**VERDICT = PASS — 18/18 checks, 0 failures** (`b04-integration.json`).

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
| 9 | `run1_session_id` | `session-c59e6750-f896-4e94-aab3-18a922caaeb4` |
| 10 | `capture_user_and_assistant` | 2 durable rows, roles `user` + `assistant` |
| 11 | `capture_used_native_event_id` | ids are the DSH `MessageId`s, not derived |
| 12 | `injected_memory_not_recaptured` | 0 rows contain the injected memory text |
| 13 | `reload_exit_zero` | same session resumed, rc=0 |
| 14 | `reload_no_duplicate_source` | rows 2→4, duplicate `(role,event_id)` rows = 0 |
| 15 | `fail_open_dsh_still_works` | rc=0 for a full turn **with the bridge stopped** |
| 16 | `fail_open_no_memory_injected` | 0 memory blocks among the requests made after the stop |
| 17 | `memory_in_same_request` | 2 of 10 recorded model requests carried a memory block |
| 18 | `memory_carries_seeded_fact` | both blocks carry the seeded fact |

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

The two `fail_open` rows are a real end-to-end failure injection: the bridge process is terminated,
then a fresh DSH turn runs to completion (`rc=0`, `run3.stdout.jsonl`) and the stub records the
requests it received — none carrying memory. DSH stays usable with the memory backend gone.

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
- Failure injection is covered for **bridge-down** only; protocol mismatch, oversize block and
  invalid config remain unit-covered (`tests/plugin.test.mjs`).

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
| `b04-integration.json` | `8ddda78636595559f06aa95891ce2ccdf24d24ffe6d2946d4059e85066df96e3` | the machine-readable report |
| `b04_integration.py` | `f3d91387f247741a263d32ff023ebb824c24c2494160b1e830772e1d3536f227` | the driver (hash printed by the run itself as `HASH_HELPER`) |
| `b04-run-on-y400.ps1` | `da3825ffdd7aea4cf1bc4d7117df99085fcf954e7addd7a97878d804b536c894` | the wrapper (hostname gate) |
| `stub-requests.jsonl` | `642f8ffb8c66aada691ebf50c47f54c4c4316e11c8727cfefdfd45dbcb3e3ce7` | what the model endpoint received |
| `bridge.log` | `facaaf1e8a2cb9cf6f3967957601e47185f7993d17dcc0d6bbb393056f2da9f8` | bridge-side log |
| `run1.stdout.jsonl` | `789be48a23a4973bf253904da01012b86337f1bc8c673b1a2226d920bc580ab2` | first DSH turn (`--json` stream) |
| `run2.stdout.jsonl` | `def7a11f56fa0387ef28d68fc3022b4be7438566b5b1c3404fc8b10e12a2204f` | reloaded-session turn |
| `run3.stdout.jsonl` | `e6ed2d43dd59aac2e21e9e92454f703afbd8bbf33a23f386ae85516e6d13dd22` | the fail-open turn (bridge stopped) |
| `profile-config.yaml` | `fc341286c68d11e14112ae8eb09b1e1f0adc6a986cce09b67033968959ee0ede` | the disposable core config (no credentials) |

`stub-requests.jsonl` is append-only; the report's request counts are read after teardown, so the
counts and this file describe the same completed run.
