# B03 pi adapter — 60–90 second end-to-end demo

> ## `SCRIPT READY / NOT YET EXECUTED`
>
> This filmed scenario has never been run — no frame of it exists. What *has* been verified
> separately is narrower: a real pi 0.99.2 host on the dedicated machine `DESKTOP-EQP3OBU`
> (Node 24.21.0) installed the local package tarball with `pi install .\package --local --approve`
> and a real Pi RPC process loaded it (`get_commands` listed `hippocampus`, `/hippocampus status`
> returned success, exit 0), and isolated non-filmed runs drove the adapter → bridge → disposable
> PostgreSQL path all the way through event persistence, recall, the once-per-turn latch and the
> exact-source trace (the second run passes; the first failed at source trace and is retained as the
> pre-fix baseline — see P10). Those runs are still **not** a substitute for this demo. The
> model-backed A→B story this script describes is an **optional release smoke that has not been run**
> (no model credentials on the dedicated machine). Every observation point below is what a future filmed run must *check*,
> not something that was checked.

The purpose of this run is not "it looks nice on camera". It is the one thing a unit test and a
simulated host cannot produce: a **real pi process**, loading **this package**, talking to the
**unchanged B01 bridge**, storing a **harmless synthetic fact**, and recalling it in a **different
session** through a **natural question** that shares no wording with the stored sentence except
one entity name — with the original source then traced back exactly.

Everything here is synthetic. The fact is made up, and the entity in it (`Luma`) is invented. The
question is generic. No real project name, credential, customer name, path or personal detail
appears anywhere in this script or in the recording it describes.

---

## Before you press record — prerequisites

Every one of these is a precondition. If any is missing, the run is **BLOCKED** and you record
nothing; you do not degrade the scenario into something weaker to make it work.

| # | Prerequisite | Why it is required | How to confirm |
| --- | --- | --- | --- |
| P1 | Dedicated test machine (`DESKTOP-EQP3OBU` in the acceptance harness), **not** a production or personal machine | This run writes real rows to a real database | hostname check before starting |
| P2 | **Disposable** PostgreSQL/pgvector instance on a non-production port, freshly created and named for this run only | Rows are written and left behind; nothing is cleaned up destructively | you created the database *for this run* and you would not mind deleting it |
| P3 | Disposable backend schema bootstrapped on that database | The bridge refuses to write into an unbootstrapped database | the packaged bootstrap command returned success |
| P4 | Backend credentials configured **locally on this machine**, through the normal Hippocampus credential channel | The adapter config file has no credential field and must not grow one | credential present in the environment/profile; **never** typed into the adapter config, never on screen |
| P5 | A bridge reachable at the `bridgeUrl` you will put in the config — started by you, in its own terminal window, before the recording | So the recording shows only the two pi sessions, not a database boot | `GET /health` on that URL answers before you start |
| P6 | `packages/pi-adapter/dist/` freshly rebuilt | pi loads `dist/`, not `src/` | `npm run build` in the package directory; rebuild changes nothing (that is the point) |
| P7 | `.pi/hippocampus.json` written with the values above (config block below) | The adapter reads nothing else | `Get-Content` shows no password field |
| P8 | **pi authentication deliberately configured on this machine**, with a credential that belongs to this machine | Without it pi cannot answer, and the "natural question" half of the demo cannot happen | a throwaway/non-production credential is configured **before** the run; do not reuse a production credential to unblock this |
| P9 | Screen recorder started; pi terminal is legible; the bridge window is on a second monitor or hidden | One on-camera window keeps the 60–90 s honest | recorder running, other window out of frame |
| P10 | A **real, verified** way to read the source id out of the `display:false` recalled block, or step 7 is BLOCKED | The trace is worthless with a guessed or substituted id, and no such surface has been verified for this package | **Still BLOCKED.** You have opened the actual pi inspection view yourself and read an id out of a recalled block; if not, the run is BLOCKED before recording |

**Credentials never appear in this document, in the config file, in the recording, or in the
commit.** The only credential-related step is P8, which happens before the recorder starts.

### Config to use (no secrets in it)

Place at `<project>/.pi/hippocampus.json`, where `<project>` is a throwaway directory, and replace
the two placeholders:

```json
{
  "enabled": true,
  "mode": "external",
  "bridgeUrl": "http://127.0.0.1:<port-reported-by-your-bridge>",
  "timeoutMs": 10000,
  "memoryBudgetChars": 8000
}
```

Use the disposable database from P2/P3. If you use `owned` mode instead, the adapter launches
the backend itself and you skip P5 — but then the recording shows a child process appearing,
which is a different and longer story. **For the 60–90 s cut, use `external`.**

---

## The synthetic content

Use exactly this. It is deliberately mundane and has no counterpart in any real project, so a
pass can never be confused with leakage from a real session.

**Stored in session A (spoken by you, as a side note inside an ordinary question):**

> "One thing worth remembering for later: Luma ships on Thursdays."

The load-bearing part is **`Luma`** — a made-up entity name, and the *only* exact lexical
anchor in the pair below.

**Asked in session B (a fresh session, reworded, no other shared wording):**

> "When does Luma deploy?"

The one exact lexical overlap between the two is the entity name *Luma*. Nothing else matches
verbatim: the stored text says *ships*, the question says *deploy*; the stored day
(*Thursdays*) is deliberately **not** in the question, so the answer cannot be read off the
prompt. There is no exact sentence reuse, no copied phrase, and no id pasted in.

**This is a keyword-anchored pairing, and the script claims nothing more.** The shared entity is
the *keyword anchor*, not evidence of semantic retrieval — matching `Luma` says the lookup
happened, not that the core understood "ships" and "deploy" as the same act. If the answer comes
back correct **because** the model simply guessed "Thursdays", that is a FAIL — which is why you
must also complete the source trace (step 7), where guessing is impossible.

---

## Timing plan

The **60–90 s** budget covers the operator's actions on camera. Model response latency is the one
variable that can blow it; if a single answer takes longer than that, record it as an honest
**longer run** rather than cutting the source trace off the end. A truncated demo that omits the
trace is worse than a 3-minute one that completes.

---

## The run

### 0 — Install the local package (before the timed part, or on camera as the cold open)

```bash
cd <repo root>
pi install ./packages/pi-adapter
```

- **PASS:** pi reports the extension as installed, and `/hippocampus status` is accepted as a
  command rather than "unknown command".
- **FAIL:** "unknown command", an extension load error, or pi silently starting with no
  `/hippocampus`. Any of these stops the run: there is no honest way to continue.

### 1 — Session A: confirm it is live (≈5 s)

Start pi in `<project>` and type:

```
/hippocampus status
```

- **PASS:** the status line reports **configured/active**, not `unavailable`. Read the exact
  wording on camera; if it says `unavailable (config …)` or `unavailable (unreachable)`, stop —
  the rest of the demo would be a no-op that merely looks like a pass.
- **FAIL:** any `unavailable` / `disabled` status.

### 2 — Session A: store the fact in natural conversation (≈15 s)

Ask a normal, throwaway question in the project directory, and slip the memory in as a side note.
For example: *"What's a sensible first thing to check in a repo like this? Also, one thing worth
remembering for later: Luma ships on Thursdays."*

- **PASS:** the assistant answers the question normally. **No Hippocampus output is expected or
  wanted here** — recording is silent, and that is correct behaviour.
- **FAIL:** any Hippocampus warning on screen, any raw error text, any endpoint or file path from
  the backend leaking into the conversation. Raw server errors are never supposed to be shown.

### 3 — Close session A completely (≈5 s)

Exit pi in the way you normally would (quit the process; if your host reloads instead, say so on
camera and record which action you used).

- **PASS:** pi exits cleanly; the bridge window is still alive and was never restarted.
- **FAIL:** the borrowed bridge process died or restarted. A borrowed backend is never signalled
  by this adapter — if it went away, something else is wrong and the run is void.

### 4 — Session B: a genuinely new session (≈5 s)

Start pi again in the same directory. Do **not** resume session A — the whole point is that B
cannot see A's transcript. (Record which action you actually used to start a new session, so the
transcript is reproducible.)

- **PASS:** pi comes up with no transcript from session A. The `/hippocampus` command is still
  available, which also shows the extension survives a restart.
- **FAIL:** the previous session's messages are on screen, or the command is missing.

### 5 — Session B: ask naturally, one shared keyword anchor (≈15 s)

Type: **"When does Luma deploy?"**

- **PASS (recall):** the answer says **Thursday/Thursdays**, and ideally attributes it to memory
  rather than to reasoning. The recalled block is injected as a custom message with
  `display: false`, so on screen you see a clean answer — a visible "hippocampus memory" bubble
  in the transcript is **not** expected for automatic recall and would itself be a finding.
- **FAIL:** the answer does not mention Thursday; or a Hippocampus warning appears; or the answer
  is right but hedged as "you mentioned earlier in this session" (it was not in this session);
  or a truncated/mangled block is injected.

**Ambiguity check, do not skip:** if the answer is "Thursdays" but you cannot tell whether it
came from memory or from a lucky guess, treat the run as **INCONCLUSIVE** and go to step 7. The
trace is what makes it evidence.

### 6 — What this step can and cannot show (≈5 s, optional on camera)

**Do not script this step as a suppression test.** The adapter arms one fetch per user input, so
typing a second, unrelated question *should* prefetch again — that is correct behaviour, not a
duplicate. A step that told you to expect no second recall would be describing the wrong system.

Ask a second, unrelated short question in the same session.

- **PASS (on-camera honesty):** an ordinary answer, nothing Hippocampus-branded on screen, no
  duplicated or mangled block spliced into the answer text. A second invisible prefetch behind
  that answer is expected and is **not** a failure.
- **FAIL:** a duplicated block, or a memory block injected into the answer text.

**The latch itself is not observable in a manual run.** What the code guarantees is narrower: a
*repeated* `before_agent_start` for the **same** input performs no second fetch, because the
`input` hook arms the latch and the first before-agent call consumes it. A human run cannot
provoke a second before-agent call for one input, so this property is **unit-tested, not
demonstrated here** — treat the manual run as silent on the latch rather than as a pass for it.

### 7 — Trace the exact source (≈15 s, **required**, and conditional on P10)

The recalled block carries the backend's own source reference. That id has to come from the
recalled block itself. How you read it is a **gate you must clear first** (P10 below) — do not
assume a way to see it, because a wrong id makes this step meaningless.

```
/hippocampus source <the-exact-id-from-the-recalled-block>
```

- **PASS:** one visible message arrives containing the original stored text, and it contains the
  string **`Luma ships on Thursdays`** — the synthetic fact from step 2. The id echoed back is the
  same id you asked for.
- **FAIL:** the read returns no result; returns a *different* id; returns text that does not
  contain `Luma ships on Thursdays` (that is a different row, not a match); or shows an internal
  JSON error envelope instead of the source text.

> **Gate P10 — read the id from a surface you have actually verified, or the run is BLOCKED.**
> The recalled block is injected with `display: false`, so by design it is not shown in the
> transcript, and **no host inspection surface for a hidden custom message has been verified for
> this package** — neither in this repo nor against a running pi host. That gap is still open even
> though the package itself now loads: a real pi 0.99.2 host has installed and loaded it
> (`get_commands` listed `hippocampus`, `/hippocampus status` returned success, exit 0), and no one
> has yet read a recalled block's id out of a pi view. Two separate non-filmed runs exercised this
> step against a **simulated** host instead: the first traced the exact emitted id `qa_1` and
> `/tool v3_get` with target `message` returned `source_id not found: qa_1` (an id that was **not**
> substituted); after the v3-core read path was fixed to resolve the references the engine itself
> prints, the second run traced that same `qa_1` to its own row and passed every host check. Neither
> run read the id off a pi surface — both took it from the recalled block the adapter returned, which
> is not the same thing.
> If you cannot open a real, documented pi inspection view that shows the injected block's text and
> id, then this step is **BLOCKED**, not skippable: the run stops, you record the blockage, and the
> video is marked incomplete. Do **not** invent a surface, and do **not** substitute an id from the
> database, a log, or memory — a substituted id proves nothing about the recall and is exactly the
> failure this step exists to catch. Resolve the id question before recording, and say on camera
> which surface you used.

### 8 — Close out (≈5 s)

```
/hippocampus status
```

- **PASS:** a clean final status. Then stop the recorder.
- **FAIL:** a status change or warning that contradicts anything shown earlier.

---

## What a PASS does and does not prove

A clean run proves: **this package loaded into a real pi host, recorded a real user turn,
recalled it in a different session, and traced the claim back to its exact source**, against the
unchanged B01 bridge and a disposable database.

It does **not** prove: that the fact is semantically retrieved rather than keyword-matched — `Luma`
is a **keyword anchor** and nothing more, so this run can at best show the keyword lane; that
images or tool calls are handled (they are dropped by design); that the one-recall-per-input latch
behaves as specified (a manual run cannot provoke a repeated before-agent call for a single input,
so that property stays unit-tested); that the run is safe on a production database (it was never
run on one); or that the bridge has flushed to durable storage at the instant the transport
acknowledged an event — an accepted event is not a durability claim.

Record, alongside the video: the pi version, the Node version, the package version, the database
name, which surface you used to read the source id in step 7, whether the fact was recalled by
keyword or semantically, and every FAIL above. A video with no written result is an anecdote, not
acceptance evidence. If step 7 was blocked for want of a verified id surface, say so in the write-
up — an incomplete video labelled incomplete is worth more than a complete-looking one.

---

## If you cannot run it yet

Two honest alternatives, in this order:

1. **The scripted, model-free acceptance path.** `eval/b03_acceptance.py` drives a simulated host
   through the real unchanged bridge into a disposable PostgreSQL and reads the database back
   independently; `eval/isolated_host.mjs` is the host driver. It proves record / exclude / recall
   / source-trace without a model and without launching pi. It has now driven two real
   database-backed runs. The first (disposable DB `b03pi_20261002_c`) persisted events and recalled a
   fixture, but the source trace **failed** on the exact emitted id `qa_1` (`source_id not found:
   qa_1`). That failure was in the v3-core read path, which resolved only `conversation_stream` ids
   and never the `qa_<qa_pairs.id>` references the recall engine prints; after the minimal fix the
   second run (DB `b03pi_20261002_d`, fresh root) passes every host check and resolves `qa_1` to its
   own row. Both records are kept. Even so it is explicitly *not* a real pi A→B run: a passing result
   there does not close the gate this demo closes.
2. **Wait for deliberate pi authentication** on the dedicated machine. Until then the model step
   cannot run and the smoke record stays *not executed*, which is a correct state, not a failure to
   hide. (Loading the package is
   not the blocker — a real pi host has done that. Answering a question is.)

Never fabricate an assistant response, never call a mock model real, never paste a production
credential into this machine to make the demo work, and never present this script's text as
though it were a run.
