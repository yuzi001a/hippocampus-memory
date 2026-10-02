# B04 — DSH automatic memory: recon evidence and locked adapter design

Baseline: `main = 8c576b39023efe09ee3312b75b76f4efb2b96eb7` (A01–A03 / B01–B03 / F2 / F3 / G01 DONE).
Scope: MISSION §§0–18 only. **No Python core change, no second bridge, no DSH fork.**

## 0. Upstream truth (§0 LATEST UPSTREAM FIRST)

Read from a fresh clone of `deepseek-ai/deepseek-harness`, not from the machine's old install.

| Fact | Value |
|---|---|
| `DSH_UPSTREAM_SHA` | `639ed015397290b3745d163aafe02ffee4aa3f84` |
| upstream commit date / subject | 2026-09-29 17:21:31 +0800 — *Merge pull request #5479 from deepseek-harness/worktree/release-dsh-0.2.0-rc.2* |
| `DSH_UPSTREAM_VERSION` | `0.2.0-rc.2` (`apps/cli/package.json`, root `package.json`, tag `dsh-v0.2.0-rc.2`) |
| latest published/installable | `@deepseek-ai/dsh@0.2.0-rc.2` (npm `latest`) → upstream HEAD **is** the released version |
| `DSH_RUNTIME_TEST_VERSION` | to be filled by the integration run (§13) |
| local machine's old install | `0.1.0-rc.6` — **reference only**, never the design basis |

Clone used for this recon: `C:/Users/servi/workspace/dsh-upstream` (shallow, `--depth 1`).

## 1. The four questions (§2) — all answered from source, all YES

**A. Does `agent/pre-step` exist?** YES.

`packages/core/agent/src/runtime-types.ts:320`:
```ts
'agent/pre-step'(this: Scoped<Agent>, payload: { agent: Agent; messages: UserMessage[]; turn: number; step: number; signal: AbortSignal }, next: () => Promise<PreStepDecision>): Promise<PreStepDecision>
```

**B. Does `payload.messages` carry the current step's claimed user messages?** YES.

`packages/core/agent-loop/src/agent.ts:267-282` — the driver claims, assembles, then emits pre-step with the claimed batch:
```ts
const claimed = this.inbox.claim(target, position.turn)
const assembly = await this.loopCtx.systemPrompt.assemble(assembleContextFor(this, signal))
...
const decision = await this.dispatch.waterfall('agent/pre-step', { messages: claimed, ...position, signal }, ...)
```
The official lifecycle graph agrees (`docs/agent-lifecycle.md:25-29`: claim → `system-prompt/assemble` → `agent/pre-step`).

**C. May the handler `await`?** YES. The waterfall returns `Promise<PreStepDecision>` and awaits listeners; first-party `dsh-compaction-basic` registers `ctx.on('agent/pre-step', async (...) => ...)` (`packages/compaction/compaction-basic/src/index.ts:158`).

**D. Do returned messages enter the same model request?** YES.

`packages/core/agent-loop/src/agent.ts:419-425` — the decision's messages are admitted, then the request is built:
```ts
if (firstAttempt) {
  for (const message of decision.messages) {
    this.session.append('user/message', message, { surfaceOp: 'append' })
  }
}
...
const request = this.buildRequest(config, preparedCall, assembly.tools, { turn, step }, startsRequestSeries, signal)
```
The core's own contract test proves an added message reaches the model in the same request (`packages/core/agent-loop/tests/interception.spec.ts:196-221`: `adapter.requests[0].messages` contains the injected text; the injected event is separately sourced).

**Consequence (§2): adopt `agent/pre-step` current-turn recall. No previous-turn cache, no next-turn delay, no `systemPrompt.context` workaround.**

## 2. Decision shape and waterfall correctness (§6)

`packages/core/agent/src/runtime-types.ts:112-119`:
```ts
export type PreStepDecision =
  | { kind: 'reject' }
  | { kind: 'enter'; messages: UserMessage[]; startsRequestSeries?: true }
```

`docs/agent-lifecycle.md:87`: *"The returned `agent/pre-step` decision is authoritative; listeners wrapping `next()` preserve downstream messages and `startsRequestSeries` unless replacement is intentional."*

So the adapter is a **wrapping, additive** listener:
```ts
const downstream = await next()
if (downstream.kind === 'reject') return downstream          // never rebuild a rejection
return { ...downstream, messages: [...downstream.messages, memoryMessage] }
```

## 3. Memory message form (§7) — producer-owned kind + `recall` form

`packages/llm/llm/src/message.ts:103-115` — the source map is **merge-extensible** and there is deliberately **no shared catch-all `plugin` kind**; each producer declares its own kind in its own module. `packages/core/agent-loop/src/runtime-context.ts:15` is the canonical pattern:
```ts
'runtime-context': { kind: 'runtime-context' } & ContextFormed
```
`MessageSourceMap` also carries `ContextForm` (`message.ts:55-67`), whose **`'recall'`** member is documented as *"Material lifted out of another session's log, possibly reduced on the way in."*

Legacy `{ kind: 'plugin', plugin: 'x' }` is a **v3-format** shape: the v3→v4 migration rewrites it to a producer-owned kind (`packages/session/session-format-v3-to-v4/src/sources.ts:38-65`, `74-105`; unknown producers become `plugin:<name>`). The first-party recall producer to copy is `packages/context/session-reference/src/types.ts:12-38`:
```ts
export interface SessionReferenceSource { kind: 'session-reference'; form: 'recall'; version: 1; references: [...] }
declare module '@deepseek-ai/dsh-llm' { interface MessageSourceMap { 'session-reference': SessionReferenceSource } }
```

**B04 adopts the same shape under its own identity:**
```ts
export interface HippocampusRecallSource {
  kind: 'hippocampus'
  form: 'recall'
  version: 1
  references: { sourceId: string; kind: string; label?: string }[]   // the core's own source references, verbatim
}
declare module '@deepseek-ai/dsh-llm' { interface MessageSourceMap { hippocampus: HippocampusRecallSource } }
```

Forbidden by §7 and by the core's invariants: mutating the claimed user message (it is deep-frozen — `interception.spec.ts:123-141` proves caller mutation throws), splicing memory into user text, claiming `kind: 'user'`, or touching the system prompt.

## 4. Capture surface (§4) — `session/event`, durable, message-producing only

`packages/core/session/src/types.ts:281-427` (`SessionEventMap`) and `:493-516` (envelope `{ type, seq, time, data, ignorable?, surfaceOp? }`):

| Event | Data | B04 |
|---|---|---|
| `user/message` | **the `UserMessage` itself** (`id`, `content`, `source`) | **capture** when `source.kind === 'user'` |
| `assistant/message` | `{ turn, step, message, stream, usage?, interrupted? }` | **capture** `message` |
| `assistant/attempt` | `{ turn, step, stream }` | skip (an attempt that committed no surface message) |
| `tool/call` / `tool/result` | call/result payloads | skip (tool wrapper) |
| `system/message` | system prompt rendering | skip |
| `developer/message` | tool add/remove | skip |
| `turn/start` `turn/end` `step/start` `step/end` | coordinates | use for turn/step attribution, not as content |
| `agent/assistant-stream` | transient chunk frames | skip (streaming tokens) |

`docs/agent-lifecycle.md:89`: *"SDK users that need replayable transcript data should consume `session/event`; `agent/*` is the live coordination API."*

Canonical event handed to the bridge (§4): `host='dsh'`, `session_id` = native session id, `event_id` = the message's own `MessageId` (documented as *"Stable identity preserved across every representation boundary"*, `message.ts:140-141`), `role`, `content` (text blocks, verbatim order), `timestamp` = event `time` (unix ms), `turn`/`step` from the surrounding boundary events, plus `parent_session` / `origin` / `delegation_depth` when the host provides them.

**Dedupe = F2 canonical `(host, session_id, event_id)` through the existing B01 ingest. No new DSH-side dedupe system.**

## 5. Self-recapture exclusion (§8) — the blocker

The injected memory message is admitted as a real `user/message` session event (`agent.ts:420`), so it is persisted and would be captured by the naive listener. The exclusion is **structural, not heuristic**: `event.data.source.kind === 'hippocampus'` → skip.

Required proof (integration): `memory → injected → model sees it → session persists it → Hippocampus does NOT ingest it again` (count of ingested events for the injected text stays zero across reload).

## 6. Subagent scope (§9)

`SessionHeader` (`packages/core/session/src/types.ts:94-131`) exposes `parentSession`, `origin: 'subagent'`, `delegationDepth`, `agentPreset`. Store what the host provides; **do not infer hierarchy**, do not merge child sessions into the parent.

## 7. Backend contract — reuse B01 (§11)

Frozen `docs/B01-HOST-CONTRACT-SPEC.md`: `GET /health` (capability handshake, `bridge_protocol_version = "b01.1"`), `POST /events` (flat event, `host` mandatory for new hosts, four-state ACK), `POST /prefetch` (`{query, session_id}` → `{ok, block}` with the core's own source references), `POST /tool` (source read). The DSH adapter reuses the B03 pi adapter's client patterns (`packages/pi-adapter/src/bridge.js`) rather than inventing a second wire.

## 8. Failure policy (§10)

- recall failure / bridge down / PG unavailable / timeout → **return `downstream` unchanged**; DSH keeps working.
- capture failure → truthful warning/status; never claim "remembered".
- config missing/invalid → warn and stay inert; never print raw server errors or credentials.

## 9. Version policy (§3)

`LATEST_FIRST + CHEAP_BACKWARD_COMPAT`: primary = latest upstream API + `0.2.0-rc.2`; secondary = at most one previous generation, only if a field-name/event-shape shim is trivial. No second lifecycle, no large cache, no heavy version branching. README states **tested** versions and the **minimum known compatible** version; unverified versions are never claimed.

## 10. Test layers (§13)

1. **Unit** — DSH event → canonical event mapping; current-user query extraction; plugin-memory exclusion; once-per-turn latch; downstream `reject` preservation; fail-open; any compat shim.
2. **Real isolated integration** — latest installable DSH + the actual adapter plugin + B01 bridge + disposable PG, on the dedicated test laptop: current user message → same-turn pre-step recall → injected into the same request boundary; assistant completion → session event → auto capture; reload/retry → no duplicate durable source; injected memory → not recaptured.
3. **Product story (§14)** — session A states a fact, session B asks for it; the current turn recalls it. If the laptop still has no model credential this stays `OPTIONAL RELEASE SMOKE` (the same honest label B03 uses) and does not block the merge.

## 11. Non-goals

Python core rewrite; a second bridge; a new recall design; a new long-term cache subsystem; forking or patching DSH; a new large acceptance framework; a second full-repo audit; historical replay; F2/F3 re-verification; production canary; P10.

## 12. Integration instrument (as executed)

The harness is the release owner's driver `b04_integration.py`, run on the dedicated test laptop
through `b04-run-on-y400.ps1` (hostname gate). It composes, in one process tree: a disposable
PostgreSQL database → schema bootstrap through the installed `hippocampus` CLI → the B01 bridge
(`python -m v3core serve`) → the credential-free stub model endpoint → the real DSH CLI with the
adapter mounted as a profile overlay row → the assertions. Every artifact lands under one run root.

Two instrument details that cost a cycle each and are worth remembering:

- The profile's shipped `cordis.patch.yml` materializes as an **empty list** (`[]`); appending a row
  after it is invalid YAML. The driver replaces only an empty/comment-only template and refuses to
  touch anything else.
- v3core's keyword-only path is selected by `storage.embed.endpoint` **and** `.model` being empty.
  Setting `model` while leaving `endpoint` empty declares "embed configured but incomplete", which
  is fail-closed (every `sync_turn` 500s). Keyword-only means both empty.

Result: `evidence/b04-dsh-integration-20261002/` — 16/16 checks PASS, 0 failures, with the model
endpoint stubbed and embeddings disabled (labelled as such in every artifact).
