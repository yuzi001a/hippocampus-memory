# @hippocampus-memory/pi

Hippocampus memory thin client for [pi](https://github.com/earendil-works/pi). It does two
things and nothing else:

1. **Records** the session's own persisted user/assistant text into the v3core B01 bridge.
2. **Recalls** memory once per user input and injects it as one additive custom context message.

It changes no pi behaviour, replaces no host context, and never reports a failed send as
"remembered".

## Versions — target vs tested

| Thing | Value | What that word means here |
| --- | --- | --- |
| This package | `@hippocampus-memory/pi` **0.1.0** | declared in `package.json`; not published to any registry |
| pi host | **0.99.2** | **TARGET / contract source.** The hooks, identity timing and wire fields used here were read from that version's installed SDK sources. A real pi 0.99.2 host (Node 24.21.0) has now **loaded a locally installed build of this package** — load/status only, see the capability matrix |
| Backend | `v3core` **4.0.0** | **TARGET.** Matches this repo's `src/v3-core` version; the adapter calls the unchanged B01 bridge over HTTP and never imports it |
| Node | `>=22` | enforced by `package.json` `engines` |

This version table is context only, not an end-to-end test result. The package has not been
published to npm or exercised against a live database. The one real pi host
observation in this package is the local install + load/status pass described in the capability
matrix; it is a load check, not a conversation.

## Install

From the repository root, with pi already installed on your machine:

```bash
pi install ./packages/pi-adapter        # install the local package
pi -e ./packages/pi-adapter             # try it for one invocation, no permanent install
```

Then create the config (below), and confirm the extension is loaded:

```
/hippocampus status
```

Requires Node >= 22. There are no runtime npm dependencies: the host `ExtensionAPI` is passed
to the default export, and the package uses only Node built-ins (`fetch`, `AbortSignal`,
`child_process`). It never imports the pi SDK and starts no process at import time.

> **Partially executed — install/load only.** A real pi host has now run these commands against
> a local build of this package: the package tarball was extracted into an isolated project and
> installed with `pi install .\package --local --approve`, and a real Pi RPC process loaded it —
> `get_commands` listed `hippocampus`, `/hippocampus status` returned success, and the process
> exited 0. That is **package load and status only**. It is **not** a model-backed A→B
> conversation, and it is **not** the P10 hidden-message inspection surface. The wider install in
> a *project* with a configured backend is still part of the acceptance you are about to perform.

## Configure

Config is read from `HIPPOCAMPUS_PI_CONFIG`, else `<cwd>/.pi/hippocampus.json`.

```json
{
  "enabled": true,
  "mode": "external",
  "bridgeUrl": "http://127.0.0.1:39090",
  "timeoutMs": 10000,
  "memoryBudgetChars": 8000
}
```

| Field | Meaning |
| --- | --- |
| `enabled` | `true` only when the adapter should run. Anything else is "disabled". |
| `mode` | `external` (borrow a running bridge) or `owned` (launch `v3core serve` lazily). |
| `bridgeUrl` | Required for `external`. `http`/`https` only. |
| `pythonExecutable`, `coreConfig` | Required absolute paths for `owned`; `coreConfig` is passed as `V3CORE_CONFIG`. |
| `profile` | Required v3core profile name for `owned`. |
| `timeoutMs` | Optional HTTP timeout, default 10000. |
| `memoryBudgetChars` | Optional positive integer, default 8000 (the frozen core injection ceiling). |

No endpoint, credential, model or profile is hardcoded. A missing or invalid config makes
memory unavailable with a warning; it never stops pi.

The file lives at `<cwd>/.pi/hippocampus.json` — `<cwd>` is wherever you launch pi, which is
also what becomes `project_id`. Point `HIPPOCAMPUS_PI_CONFIG` at an absolute path if you want
one config to cover several projects.

Copyable starting point for `external` mode (borrow a bridge you already started yourself):

```json
{
  "enabled": true,
  "mode": "external",
  "bridgeUrl": "http://127.0.0.1:39090",
  "timeoutMs": 10000,
  "memoryBudgetChars": 8000
}
```

For `owned` mode, add the three absolute paths the adapter needs to launch the backend itself:

```json
{
  "enabled": true,
  "mode": "owned",
  "pythonExecutable": "C:/absolute/path/to/venv/Scripts/python.exe",
  "coreConfig": "C:/absolute/path/to/profile/config.yaml",
  "profile": "your-v3core-profile",
  "timeoutMs": 10000,
  "memoryBudgetChars": 8000
}
```

**Never put a password in this file.** The adapter has no credential field by design; the
backend process reads its own credentials the way the rest of Hippocampus does
(`V3CORE_PG_PASSWORD` / the profile's own environment, see the root
[`docs/INSTALL.md`](../../docs/INSTALL.md)). A config that needs a secret is a misconfiguration.

If `enabled` is anything other than `true`, no config is loaded and `/hippocampus status` says
`unavailable (config disabled)`. If the file is missing, unparseable, or the mode-specific fields
are incomplete, the status names that specific reason — and pi keeps running either way.

## Use

Four commands, all slash commands in the pi host:

```
/hippocampus status            # disabled / unavailable / configured / active — and why
/hippocampus on | off          # session-local; the persistent choice stays in config
/hippocampus source <exact-id> # read one stored source, delivered as a custom message
```

**Enable / disable.** There are two switches and they are not the same thing:

| Switch | Where | Scope |
| --- | --- | --- |
| `"enabled": true` in the config file | `.pi/hippocampus.json` | persistent; survives restarts |
| `/hippocampus off` | current pi session | session-local; `on` restores it |

Both must be true for anything to be recorded or recalled. `/hippocampus off` never rewrites the
config file.

**Status.** `/hippocampus status` reports exactly one of:

| Status | Means |
| --- | --- |
| `disabled (session off)` | config loaded, but switched off for this session |
| `unavailable (config disabled)` | `enabled` was not `true` — the config is not loaded at all |
| `unavailable (config unreadable)` | the config file could not be read |
| `unavailable (config invalid-json)` | the config file is not valid JSON |
| `unavailable (config not-an-object)` | the config file parsed to something that is not an object |
| `unavailable (config invalid-config)` | the mode-specific fields are incomplete |
| `configured (not connected yet)` | usable config, no handshake attempted yet (lazy) |
| `active (backend ready, capabilities: …)` | `GET /health` returned `bridge_protocol_version: b01.1` |
| `unavailable (unreachable)` / `unavailable (protocol-mismatch)` | handshake failed; recording and recall are off |
| `closed` | the session shut down; start a new pi session |

Status distinguishes "transport accepted" from "a memory was actually retrieved": an accepted
event is not a claim that PostgreSQL has already flushed, and a `prefetch` result is the only
thing that means recall succeeded.

**Source.** `/hippocampus source <exact-id>` reads back one stored source through
`POST /tool` (`v3_get`) and delivers it as a visible custom message, so a recalled claim can be
traced to the original turn. The id must be the exact reference that appeared in the recalled
block; nothing is substituted for it, and an unknown id produces an explicit "no stored source"
notice rather than a different row's text.

## What is recorded, and what is not

Recorded: persisted `type=message` entries whose role is `user` or `assistant`, text blocks
verbatim and in order, carrying the **native** `id`, `parentId`, `timestamp`, and the SDK's own
session id.

Not recorded: `custom` messages, `system` prompts, `toolResult` wrappers, compaction and branch
summaries, streaming deltas, and assistants that ended `aborted` or `error`.

First-version limitation: image, thinking and tool-call parts are **dropped**, never rewritten
into invented prose.

Drain points are `agent_end` (after persistence), `session_start`, `session_before_switch`,
`session_before_fork` and `session_shutdown`. `message_end` is deliberately empty: pi emits it
*before* the entry is persisted and it carries no native entry id, so no id is invented there and
no timer is used to manufacture one.

- Deltas, never the whole session, on every request.
- Draining is serial, and stops at the first failed event, so an assistant answer can never
  overtake a user turn that was not stored.
- Only an explicit `accepted`/`duplicate` acknowledgement marks an entry as seen; anything else
  stays retryable, and a later drain resends it. A loaded session replays once, then sends only
  deltas.
- The same entry id in a different SDK session is a different event.

## Recall

One `POST /prefetch` per user input, keyed by an `input`-armed latch; a repeated
`before_agent_start` in the same input performs no second fetch. The returned block is opaque and
carries the core's own source references, which are preserved for `/hippocampus source`.

If a block exceeds `memoryBudgetChars`, the **whole** injection is refused with a warning. It is
never truncated, because a partial block would misrepresent the memory. An empty block adds no
message at all. The injected message is always `custom`, so it can never be mistaken for a new
user fact, and it is never added to the record.

## Failure behaviour

B03 failure is always fail-open and always returns `continue`: pi keeps running and the user sees
at most one generic warning. Raw server errors, stack traces, endpoints and private paths are
never displayed. In `owned` mode only the child process this adapter spawned is released; a
borrowed external backend is never signalled, and no unrelated PID tree is touched.

## Known limitations

Design limits, stated up front:

- **Text only.** Image, thinking and tool-call parts are dropped, never paraphrased.
- **One recall per user input.** No mid-turn or repeated recall; that is a later milestone.
- **No invented identity.** No hashed message ids, no synthetic turn/branch ids, no timers. If
  the host did not give a native id, nothing is recorded.
- **No background daemon.** Nothing runs until pi hands over a hook; in `owned` mode the backend
  is launched lazily and released on shutdown.
- **Bridge-only.** The adapter speaks B01 `b01.1` and nothing else; a backend with a different
  `bridge_protocol_version` is reported unavailable rather than tolerated.
- **Fail-open.** When memory breaks, pi keeps running and you may see one generic warning.

Limitations of the current evidence:

- **Package load is verified; the conversation is not.** A real pi 0.99.2 host on the dedicated
  machine `DESKTOP-EQP3OBU` (Node 24.21.0) installed the local tarball into an isolated project
  with `pi install .\package --local --approve` and a real Pi RPC process loaded it: `get_commands`
  listed `hippocampus`, `/hippocampus status` returned success/handled, exit 0. Scope is
  **install + load + status only** — no model-backed A→B conversation, and no evidence about the
  hidden-message inspection surface.
- **The disposable-PostgreSQL run passes on the second attempt; the first one failed and is kept.**
  The first fresh-root run (disposable DB `b03pi_20261002_c`) got all the way to persistence and
  recall and then **failed at source trace**: it traced the exact emitted id `qa_1`, and
  `/tool v3_get` with target `message` returned `source_id not found: qa_1`, while the QA row read
  back `source_id` `qa_sync/pi/b03-session-a/b03A-0001`. The emitted reference was **not**
  substituted. That failure was real, and it was in **v3-core, not in this package**:
  `recall_pool.py` prints QA references as `qa_{qa_pairs.id}` (and topic references as
  `topic_{topic_id}`), while `PgEmbedStore.get_message_context` resolved only
  `conversation_stream.id` / `session_id` and `get_card_by_source_id` only a bare `topic_id` — so
  the references the engine itself printed resolved nowhere.
- **The minimal fix makes the read path resolve the engine's own references.** In
  `src/v3-core/src/v3core/pg_store.py` the read path now resolves `qa_<qa_pairs.id>` and the
  canonical `qa_sync/<host>/<session_id>/<event id>` against `qa_pairs`, and `topic_<topic_id>`
  against `topics`, falling through to the untouched legacy branches when nothing matches. The
  injected block text is unchanged and no identifier was renamed or substituted.
- **The re-run passes end to end.** A second fresh-root run on disposable DB `b03pi_20261002_d`
  (new run root, same support root) returned `verdict = PASS` with every host check green: the
  source command emitted exactly one visible custom message for `/hippocampus source qa_1`, the
  read returned **its own** non-empty content carrying this run's fixture fact, and the independent
  PostgreSQL trace resolved that same reference to the row whose canonical `source_id` embeds the
  native identity `(host=pi, session_id=b03-session-a, event_id=b03A-0001)` — an id inside the
  driver's fixed recordable set. The trace is a `qa_pairs` lookup by the exact emitted id, and every
  fail-closed branch still fails the run: missing row, wrong session, wrong fixture text, foreign
  host, event id outside the recordable set, and a canonical id that embeds no identity triple.
  The earlier failing run and its evidence are retained, not overwritten.
- **Real model-backed A→B remains AUTH BLOCKED.** No model credentials are configured on the
  dedicated machine. This is a **BLOCKED gate**, not a skipped convenience: no external or model
  call was made, no assistant answer was fabricated, no mock model was called real, and no
  production credential was reused to unblock it.
- **Not published.** `0.1.0` exists only in this repo. There is no npm release.
- **B03 is not DONE.** Package implementation, a real-host load check and a passing real
  disposable-PG run (including exact-source resolution) now exist. The model-backed A→B story
  remains a **named, blocked gate**, and the hidden-message inspection surface remains unverified.
  No PR or merge is claimed.

## Capability matrix

What is actually verified today, and what is not:

| Capability | State | How it was checked |
| --- | --- | --- |
| Mapping, exclusion, native ids, drain ordering, ack/dedupe, retry-after-failure, recall latch, budget refusal, fail-open | **PASS** — 34/34 | `npm test` (`node --test`, 34 tests, 0 fail, 0 skip), twice against the uncommitted package working-tree snapshot (not in HEAD) |
| Owned/external lifecycle, bounded readiness, owned-child-only release | **PASS** | unit-covered with a fake child process in the same suite |
| Reproducible build | **PASS** | `npm run build` twice; `dist/*.js` SHA-256 identical across runs and byte-identical to `src/*.js` |
| `dist/` is importable as the real entry | **PASS** | `import('./dist/index.js')` yields `default` (factory) plus 8 named exports |
| Adapter → unchanged bridge → **disposable PostgreSQL** | **PASS** | fresh-root run on disposable DB `b03pi_20261002_d`: DB/bootstrap/PG healthy, bridge `b01.1`, owned bridge released, four expected native event ids persisted, fixture recalled, latch behaviour observed, and the source read traced to its own row. The earlier `b03pi_20261002_c` run (same path, failing at source trace) is retained as the pre-fix baseline |
| Fresh `pi install` of the local package; pi actually loading `dist/index.js` | **PASS (load/status only)** | real pi 0.99.2 / Node 24.21.0 on `DESKTOP-EQP3OBU`: `pi install .\package --local --approve` from an extracted tarball, then a real Pi RPC process loaded it — `get_commands` listed `hippocampus`, `/hippocampus status` success/handled, exit 0. No model-backed conversation, no P10 inspection surface |
| Real model-backed pi A→B story | **AUTH BLOCKED** | no model credentials on the dedicated machine; no external/model call was made and no credential was reused to force it |
| Recalled-reference → exact source resolution | **PASS** | the isolated run traced the exact emitted id `qa_1`; `/tool v3_get` with target `message` returned `success: true` with that row's own text, and the independent PG trace resolved the reference to the `qa_pairs` row whose canonical `source_id` embeds `(host=pi, session_id=b03-session-a, event_id=b03A-0001)` — **not** substituted. The minimal read-path fix lives in `src/v3-core/src/v3core/pg_store.py`; the injected block text and every identifier are unchanged |

The test suite is unit-only. Every HTTP call and child process in it is an in-process fixture.
The two acceptance helpers under `eval/` are **scripts, not results**, and they are easy to
conflate with the unit suite:

- `eval/b03_acceptance.py` and `eval/isolated_host.mjs` have moved past the cheap ways: they have
  driven two **database-backed runs** (disposable DBs `b03pi_20261002_c` and `..._d`) through event
  persistence, recall, latch and exact-source resolution. The refusal ladder, the hostname check and
  the stub paths are still part of their normal shape — a live run on a non-dedicated machine stops
  at the hostname check before any credential, file write, process or connection — but they are no
  longer the *only* thing that has exercised them. The parse/help paths remain cheap.
- **A database-backed run now passes, and the failing one is kept.** The first fresh-root run
  persisted the events, recalled the fixture and then failed the source trace on `qa_1`; the second,
  on a fresh root and a fresh disposable database, passes every host check and resolves `qa_1` to its
  own row. Both run records exist — the failure is the pre-fix baseline, not something to hide.
  Neither is a real pi A→B run.

## Try it end-to-end

A 60–90 second, entirely synthetic, recording-ready scenario — enable, store a harmless fact in
session A, close it, start session B, ask naturally, observe recall, trace the exact source — is
in [`demo/60-90s-demo.md`](demo/60-90s-demo.md). It is labelled **`SCRIPT READY / NOT YET
EXECUTED`**, because this filmed scenario has never been run. Non-filmed runs have since covered the
package load and the whole adapter→bridge→disposable-PG path, including the exact-source trace (see
the capability matrix); they are still not this demo, and not a real pi A→B run.

## Develop

```bash
npm test          # node --test, no network, no services
npm run build     # copies src/*.js to dist/ reproducibly
npm pack --dry-run
```

`dist/` is generated and is the file the package manifest points pi at. It is committed, so a
clone can be loaded without a build step — but rebuild it before trusting any evidence.
