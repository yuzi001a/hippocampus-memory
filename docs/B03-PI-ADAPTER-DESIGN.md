# B03 pi adapter — locked thin-client contract

Baseline: F2/F3 accepted and frozen; parent branch `feat/b03-pi-auto-memory-adapter`, base `9d8101a4f48e0f2969ee216a11b8458b7ecb1f94`. Scope: MISSION §§9–18 only. No Python core or B01 bridge changes.

## Verified host API

Actual dedicated laptop install: pi **0.99.2**, Node **24.21.0**. Parent fetched the installed package's official `docs/extensions.md`, `docs/packages.md`, `docs/session-format.md`, `dist/core/extensions/types.d.ts`, `dist/core/session-manager.d.ts`, and `dist/core/agent-session.js`. These are installed-SDK evidence, not guesses from old plans.

- `message_end` contains `message`, **no native entry ID** (`types.d.ts:806–810`). It is emitted BEFORE persistence (`agent-session.js:728–747`). Do not invent IDs, hash message text, use current leaf ID as that message's ID, or persist directly inside that hook.
- Completed messages acquire native `SessionMessageEntry.id`, `parentId`, `timestamp`, `message` after `appendMessage`. Read these via public readonly `ctx.sessionManager.getEntries()` after `agent_end`, and drain pending persisted entries before a subsequent recall / session replacement / shutdown. The installed `pi.on` overloads (`types.d.ts:1143–1184`) type exactly `session_start`, `session_before_switch`, `session_before_fork` and `session_shutdown` among the session events; there is NO `session_switch` and NO `session_fork` in this SDK. Actual lifecycle: the OLD session is drained from `session_before_switch` / `session_before_fork`; `session_shutdown` runs before runtime teardown due to quit/reload/session replacement, reasons `new`/`resume`/`fork` included (`types.d.ts:608–614`), and closes the old adapter; the replacing/new/resumed/forked session drains its OWN persisted entries from its own `session_start` (`types.d.ts:546–552`, reasons `startup`/`reload`/`new`/`resume`/`fork`). Long-lived resources start in `session_start` and are closed from `session_shutdown` (`docs/extensions.md:54–63`). Native session identity is `getSessionId()`.
- Only `type=message`, role user/assistant are recordable. Exclude error/aborted assistant results, custom messages, system/toolResult wrappers, compaction/branch summaries and streaming deltas. Text blocks remain verbatim and ordered; images, tool calls and thinking are not silently converted into invented prose. Document the text-only initial scope.
- `before_agent_start` has `prompt` (`types.d.ts:693–701`). Its result permits an additive custom message (`1082–1085`). Return `{message:{customType,content,display:false,details}}`; never return a replacement `systemPrompt`, transform input or replace host context. The returned context is a custom message and must never be recorded as a new user fact.
- `input` starts the per-user-turn recall latch; repeated before-agent calls without a new input perform no second prefetch. This latch is local runtime control, NOT an invented persistent event identity.

## Wire and identity

Use existing B01 only: `GET /health`, `POST /events`, `POST /prefetch`, `POST /tool`. Health handshake requires the actual `bridge_protocol_version="b01.1"`; flat event JSON uses host `pi`, the exact SDK session ID and exact persisted entry ID; preserve native parent ID and timestamp. Do not invent a branch ID or numeric turn ID when the SDK does not provide one. Send deltas, not the entire session on every request. Locally acknowledged entry keys include native session ID; reload retry dedupe ultimately belongs to frozen F2 canonical `(host,session_id,event_id)`.

Per-event POST /events sends flat `{host:'pi',session_id,event_id,role,content,timestamp,parent_event_id,project_id}`; project identity is explicitly configured or the host cwd. There is NO nested event envelope and NO required numeric protocol version field. Only treat explicit successful accepted/duplicate acknowledgement as transport acceptance. Acceptance is NOT a claim that PG is already flushed or a memory was successfully recalled. Failed sends remain eligible for retry. A loaded session may replay persisted eligible entries once for crash/reload recovery, but subsequent drains send only unacknowledged deltas.

`POST /prefetch`: `{query:prompt,session_id}`; response `{ok:true,block}` already contains core source references. Preserve that opaque block and source IDs. Default body budget equals the frozen core 8000-character injection ceiling; configurable positive integer. If a block exceeds the client budget, refuse that entire injection with a warning, never substring it. A short static provenance/trust envelope may be added. Empty block = no custom message.

`POST /tool` for source read: `{name:'v3_get',args:{target:'message',source_id}}`; preserve the returned result and explicit not-found outcome. No new tracing service.

## Configuration and lifecycle

Config file: explicit `HIPPOCAMPUS_PI_CONFIG`, otherwise `<cwd>/.pi/hippocampus.json`. Fields: `enabled`, `mode` (`external` or `owned`), `bridgeUrl` for external; absolute `pythonExecutable`, absolute `coreConfig`, `profile` for owned; `timeoutMs`, `memoryBudgetChars` optional. No machine-specific endpoint, credential, model, or profile hardcoding.

Owned mode launches installed `python -u -m v3core serve --host 127.0.0.1 --port 0 --profile <profile> --ready-json` lazily, with explicit `V3CORE_CONFIG`. Wait for bounded readiness, use the assigned URL, and retain the actual ChildProcess handle. No daemon on module import. Shutdown drains messages then releases only this owned child, never a borrowed external service or unrelated PID tree. Missing/invalid config and backend failures warn without stopping pi. Never display raw server errors or credentials. `remembered=true` is not a permitted failure status.

Expose `/hippocampus status`, `/hippocampus on`, `/hippocampus off`, `/hippocampus source <exact-id>`. On/off is session-local; persistent choice comes from config. Status must distinguish disabled / unavailable / transport accepted from successful retrieval.

## Deliverable and acceptance

New self-contained `packages/pi-adapter/`: real npm package metadata, reproducible generated extension output, Node built-in tests, install page and demo scenario. Add one narrow secret-free Node CI job, not a core CI refactor. README capability matrix remains honest until independent acceptance.

Only three test layers: (1) bounded Node unit tests for mapping/exclusion/IDs/recall latch/reload; (2) adapter → unchanged bridge → dedicated disposable PG, record/new session/recall/source read; (3) one real pi A→B story. Fresh local-package install must demonstrate actual pi load. Tests and destructive state stay on dedicated `DESKTOP-EQP3OBU`; no production reads/writes or production credential reuse.

Discovery limitation: dedicated laptop's default pi auth file is an empty object, and no API-key environment names were present at the parent check. Real model-backed E2E must fail closed or use credentials deliberately configured ON that laptop. Do not fabricate an assistant response, label a mock model real, copy production credentials, or upgrade a partial test into B03 DONE. Package implementation and secret-free acceptance may proceed independently; real E2E remains a named gate.

Non-goals: tree reasoning, repeated mid-turn recall, observer/E1 changes, Recall V2 rewrite, adjacent audit, B04/M01 implementation, publishing to community.
