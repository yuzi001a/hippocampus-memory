# eval/ — the real isolated integration run (owner-executed)

`node --test` in the parent directory is the *unit* layer. This directory holds the harness that
lets a real DSH host be exercised without a model provider:

- `stub-model-server.mjs` — an Anthropic-shaped streaming stub. It answers `/v1/messages` with a
  fixed reply and records every request (including the exact text of any recalled-memory block it
  received) to a JSONL file. Credential-free by construction; it never talks to a provider.
- The driver that composes a full run (disposable PG → bootstrap → bridge → stub → real DSH CLI with
  this adapter mounted → assertions) is the release owner's, and lives with the run's evidence:
  `evidence/b04-dsh-integration-20261002/`.

**Result on 2026-10-02 — 16/16 checks PASS, 0 failures.** Real DSH CLI `0.2.0-rc.2` + this adapter +
real B01 bridge + disposable PostgreSQL; model endpoint = the stub; embeddings disabled
(keyword-only recall). Filed evidence: `b04-integration.json` (the machine-readable report),
`stub-requests.jsonl` (what the model actually received), `bridge.log`, `run1.stdout.jsonl`,
`profile-config.yaml`.

What that run does **not** cover, and must not be read as covered: a live model provider, a live
embedding endpoint, and the failure-injection cases below (those are unit-covered — see
`tests/plugin.test.mjs`, "every failure class fails open with at most one warning per class").

Do not record a result here that was not actually observed. `exit 0` from a unit run is not
evidence for any claim in this file.

## Preconditions

1. A real, installable DSH: `@deepseek-ai/dsh@0.2.0-rc.2` (or a checkout of
   `639ed015397290b3745d163aafe02ffee4aa3f84`). Record the exact resolved version.
2. This package installed into that DSH as a plugin, with the DSH-native overlay `config` block
   (see the parent `README.md`) pointing at a bridge URL.
3. A **disposable** v3core: its own PG database, its own profile directory. Never production PG,
   never the production profile.
4. A model credential. If none is available, this run stays `OPTIONAL RELEASE SMOKE` and does not
   block the merge — label it honestly rather than claiming a pass.

## Start the disposable bridge

Use the repo's existing core serve surface (B01). It binds an ephemeral port and prints exactly
one ready JSON line to stdout:

```
python -m v3core serve --host 127.0.0.1 --port 0 --profile <DISPOSABLE_PROFILE> --ready-json
```

Take the **actual** port from the ready line (never assume `0`), then confirm the protocol before
starting DSH:

```
curl -s http://127.0.0.1:<PORT>/health
# require bridge_protocol_version == "b01.1"; anything else is a FAIL, not a warning
```

## The six observations that must be captured

Each row is a claim; each needs a real transcript, not a claim.

| # | Claim | How to observe it | Expected |
| --- | --- | --- | --- |
| 1 | Current-turn recall reaches the **same** model request | Turn 2 asks a question whose fact was stated in session A. Capture the outgoing provider request payload. | The recalled block appears in that request, after the system prompt, in the same call — not a later turn. |
| 2 | The injection is additive | Same capture. | The claimed user messages are present and unmodified; the system prompt is byte-identical to a run without the plugin. |
| 3 | The assistant completion is auto-captured | Let the turn finish; query the bridge for `host=dsh`. | A canonical event with `event_id` = the DSH `MessageId`, `role=assistant`, and the same `session_id`. |
| 4 | The user turn is auto-captured | Same query. | A canonical event with `role=user`. |
| 5 | The injected memory is **never recaptured** | Count ingested events whose `content` contains the recall note. | Exactly the injected text appears **0 times** as a newly ingested source, and stays 0 across a session reload. |
| 6 | Reload/retry produces no duplicate durable source | Replay the session log (or restart DSH against the same session) and re-query. | No new durable row; B01 reports `duplicate`, not a second `accepted`. |

## Failure cases to exercise as well

Each must leave DSH fully usable, and must log **at most one** warning per class:

- Bridge stopped mid-session → the turn still completes; no injected message.
- `bridge_protocol_version` other than `b01.1` → unavailable, host unaffected.
- Prefetch slower than `timeoutMs` → the turn still completes.
- Block longer than `memoryBudgetChars` → no injection, one warning, and **no truncated text**.
- Config missing / `enabled: false` / `mode: "owned"` / non-http URL → inert, at most one warning,
  no throw, DSH unaffected.

## Evidence to file

For each observation: the exact command, the real stdout/stderr (trimmed only of secrets), the
resolved DSH version, the resolved bridge protocol, and the recorded pass/fail. Store it under the
repo's existing `evidence/` convention; do not summarise a failure away.
