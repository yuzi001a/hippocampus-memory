# @hippocampus-memory/dsh

Hippocampus memory thin client for [DSH (deepseek-harness)](https://github.com/deepseek-ai/deepseek-harness).
It does two things and nothing else:

1. **Records** the session's own committed user/assistant text into the v3core B01 bridge.
2. **Recalls** memory at most once per user turn and injects it as one additive `user/message`,
   through the `agent/pre-step` waterfall, into the *same* model request.

It changes no DSH behaviour, replaces no host context, and never reports a failed send as
"remembered". Every failure path returns the host's own decision unchanged.

## Versions — target vs tested

| Thing | Value | What that word means here |
| --- | --- | --- |
| This package | `@hippocampus-memory/dsh` **0.1.0** | declared in `package.json`; not published to any registry |
| DSH host | **0.2.0-rc.2** (`639ed015397290b3745d163aafe02ffee4aa3f84`) | **TARGET / contract source.** Every event name, payload shape, decision type and source-kind rule used here was read from that commit. **Not** exercised by a real DSH process by this package's unit tests — see below. |
| Minimum known compatible | **0.2.0-rc.2** | the oldest version whose `agent/pre-step` payload/decision and `session/event` envelope were verified against these sources. Older versions are **unverified and not claimed**. |
| Backend | v3core B01 bridge, protocol `b01.1` | **TARGET.** The adapter calls the frozen HTTP surface and never imports the core. |
| Node | `>=22` | enforced by `package.json` `engines`; the unit tests use only `node --test` built-ins |

**Tested here:** the unit suite (`node --test`, 39 tests) — mapping, latch, plugin wiring, config
resolution, fail-open. **Not tested here:** a real DSH process, a live bridge, a real model. The
isolated integration run is described in [`eval/README.md`](eval/README.md) and is executed by the
release owner on a dedicated test machine, not by this package.

## Install

Zero runtime npm dependencies — Node built-ins only (`node:test`, `node:crypto`, global `fetch`,
`node:fs`, `node:path`). No build step, no bundler, no TypeScript. Plain ESM.

Register the package in a DSH overlay (for example `<profile>/cordis.patch.yml`). The overlay is a
list of patch operations, and a plugin row is an `insert`:

```yaml
- insert:
    - id: hippocampus
      name: '@hippocampus-memory/dsh'
```

`name` accepts an installed package name or a path relative to the profile directory (a vendored
copy, e.g. `./hippocampus-adapter/src/index.js`).

## Configuration

Two sources, in this precedence order. Both are normalized to the identical shape
`{ mode, bridgeUrl, timeoutMs, memoryBudgetChars, capture, recall }`.

### 1. The DSH overlay config object (DSH-native, preferred)

The `config` block of the plugin's overlay row is passed to `apply(ctx, config)`:

```yaml
- insert:
    - id: hippocampus
      name: '@hippocampus-memory/dsh'
      config:
        enabled: true
        mode: external
        bridgeUrl: http://127.0.0.1:8765
        timeoutMs: 10000
        memoryBudgetChars: 8000
        capture: true
        recall: true
```

A present overlay **wins**: the file is not read at all. An overlay that is present but *invalid*
(`mode` other than `external`, a non-http(s) `bridgeUrl`, `enabled: false`) leaves the plugin inert
rather than silently falling back — an explicit operator row is never overridden by a file.

### 2. The config file (used when the overlay object is absent, `null`, or `{}`)

Path: `$HIPPOCAMPUS_DSH_CONFIG` (trimmed, non-empty) else `<cwd>/.dsh/hippocampus.json`.

```json
{
  "enabled": true,
  "mode": "external",
  "bridgeUrl": "http://127.0.0.1:8765",
  "timeoutMs": 10000,
  "memoryBudgetChars": 8000,
  "capture": true,
  "recall": true
}
```

| Field | Rule |
| --- | --- |
| `enabled` | must be exactly `true`; anything else is `disabled` (silent — no warning spam) |
| `mode` | `"external"` only in v1; anything else → `invalid-config` |
| `bridgeUrl` | required, `http:`/`https:`, trailing slash stripped |
| `timeoutMs` | positive integer, default `10000` |
| `memoryBudgetChars` | positive integer, default `8000` |
| `capture` | `false` disables capture only; default `true` |
| `recall` | `false` disables recall only; default `true` |

If **neither** source is usable the plugin is inert, warns **once** with a class name only
(`unreadable` / `invalid-json` / `not-an-object` / `invalid-config`), and never throws. No URL, path,
credential or raw file text is ever printed.

## Behaviour

### Capture — `session/event`

Records only the two event types that commit a durable surface message:

| DSH event | Recorded? |
| --- | --- |
| `user/message` with `source.kind === "user"` | yes, `role: "user"` |
| `user/message` with any other source (including **`hippocampus`**) | no |
| `assistant/message` | yes, `role: "assistant"`; skipped when `interrupted === true` |
| `assistant/attempt`, `tool/call`, `tool/result`, `system/message`, `developer/message`, `turn/*`, `step/*`, streaming frames, any unknown future type | no |

- **Identity is native**: `event_id` is the DSH message's own `MessageId`, `session_id` the
  session's own id. Nothing is hashed, derived or invented; a missing id yields no event.
- **Self-recapture exclusion** is structural: our own injected message is persisted as a
  `user/message` whose `source.kind` is `hippocampus`, and is therefore never ingested again.
- **Ordering** is preserved by a serial chain, so a slow send can never be overtaken.
- **Dedupe** is in-memory `${sessionId}::${eventId}`; the durable identity is B01's own
  `(host, session_id, event_id)`. A failed send does **not** advance the dedupe set, so the same
  event is retried if the log offers it again.
- Capture is fire-and-forget: `session/event` is a post-commit feed and the host never awaits it.
- Turn attribution comes from `turn/start` / `step/start`, and `assistant/message` uses its own
  `data.turn`. Subagent hierarchy is stored only if the host reports it; it is never inferred.

### Recall — `agent/pre-step`

```js
async (payload, next) => {
  const downstream = await next();
  if (downstream.kind === "reject") return downstream;          // identity preserved
  const message = await recall(payload);
  return message ? { ...downstream, messages: [...downstream.messages, message] } : downstream;
}
```

- **At most one injection per user turn**, latched on `sessionId + turn`. A tool-continuation step
  claims no user-sourced message, so it can never trigger a second recall inside one turn. Two
  sessions keep independent latches.
- **Additive only**: the claimed messages are never replaced, reordered or mutated, and the system
  prompt is never touched. `startsRequestSeries` is preserved, and never invented when absent.
- The injected message:

  ```js
  { id: randomUUID(), role: "user",
    content: [{ type: "text", text: "[hippocampus memory — recalled context, not a new user fact]\n\n<block>" }],
    source: { kind: "hippocampus", form: "recall", version: 1, references: [...verbatim from the bridge] } }
  ```

  `kind: "hippocampus"` is a **producer-owned** source kind (upstream has no shared catch-all
  `plugin` kind); `form: "recall"` is the upstream `ContextForm` member for material lifted out of
  another session. `references` are the core's own source references, copied verbatim — never
  invented; absent references become `[]`.
- **Budget**: a block longer than `memoryBudgetChars` skips the **whole** injection. It is never
  substringmed — a trimmed block would misrepresent what was recalled.
- **Fail-open**: no config, bridge down, protocol mismatch, timeout, empty block → the downstream
  decision is returned unchanged. One generic warning per failure class for the life of the
  session, never one per turn, and never a raw server or transport message.

## Text handling

Array content keeps **text blocks only, in order, joined with `""`**. Image, thinking and tool
blocks are dropped — never converted into invented prose. An empty result is not recorded.

## Status

`status()` reports the real state and never claims a failed send was stored:

`active (backend ready, capabilities: events, prefetch)` · `configured (not connected yet)` ·
`disabled` · `unavailable (config <class>)` · `unavailable (<handshake reason>)`

## Real-host integration

Run against a real DSH CLI (2026-10-02): DSH `0.2.0-rc.2` (upstream `639ed015397290b3745d163aafe02ffee4aa3f84`)
+ this adapter + a real B01 bridge + a disposable PostgreSQL, with the model endpoint replaced by a
local credential-free stub and embeddings disabled (keyword-only recall). **18/18 checks PASS, 0
failures**: the composed profile tree really mounts the row, one recalled block reached the model
request of the same turn (411 chars, carrying the seeded fact), the durable user and assistant
messages were captured with their native DSH event ids, our own injected message was not recaptured,
reloading the session added no duplicate source, and — with the bridge process killed mid-run — a
fresh turn still completed with no memory injected (fail-open). Evidence: `evidence/b04-dsh-integration-20261002/`;
harness notes: `eval/README.md`.

## Tested versions

| DSH version | Status |
| --- | --- |
| `@deepseek-ai/dsh@0.2.0-rc.2` (upstream `639ed015397290b3745d163aafe02ffee4aa3f84`) | **tested** — 39 unit tests + the isolated integration run above (2026-10-02) |

Minimum known compatible version: **not claimed.** The host contract is read from `0.2.0-rc.2`;
earlier generations (including the `0.1.0-rc.*` line) have not been exercised and are not asserted to
work. If an older host turns out to need only a field-name/event-shape shim, that shim is welcome;
anything needing a second lifecycle or heavy version branching is out of scope for v1.

## Limits (honest list)

- The integration above is an isolated harness, not a production deployment: the model endpoint was
  a stub (no model credential exists on that machine) and embeddings were disabled. A run against a
  live model provider and a live embedding endpoint has **not** been performed.
- Of the failure classes, only **bridge-down** is integration-covered; protocol mismatch, an oversized
  block and invalid config are unit-covered (`tests/plugin.test.mjs`).
- The bridge must answer `bridge_protocol_version: "b01.1"`; anything else is treated as
  unavailable by design.
- Non-text content is dropped, not summarised.
- Subagent sessions are stored as their own sessions; the hierarchy is never inferred or flattened.
- Dedupe is in-memory per process. Durable idempotency is B01's own `(host, session_id, event_id)`.
- `mode: "owned"` (spawning a local core) is **not** implemented in v1.

## Tests

```bash
cd packages/dsh-adapter
node --test "tests/*.test.mjs"
```

Every test injects a fake `fetchImpl` and a fake `ctx`. No socket, no child process, no real config
file, no database, no model provider.

## License

MIT
