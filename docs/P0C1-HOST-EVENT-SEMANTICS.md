# P0-C1 — Hermes Host Event Semantics (design contract)

Status: design locked before implementation. Task book: P0-C1 (Hermes Host Event Semantics).
Fact baseline: `projects/hippocampus/reports/2026-10-04-p0c-{pairing-survey,system-event-taxonomy}.md` (ai-collab-vault).
Base: `21d2bde15cd4bb1ea6858a48f9083290d72ff06a`. Branch: `fix/p0c1-hermes-event-semantics`.

## 1. Current behaviour (measured, not assumed)

`V3Core.sync_turn` (`src/v3-core/src/v3core/__init__.py:3575`) walks the FULL Hermes
snapshot and today applies a **binary** test (`__init__.py:3721-3741`):

```python
if _injection_pattern.match(content):     # or startswith(_injection_prefixes_fallback)
    processed_this_call.add(cursor_key)
    continue                              # ← everything is dropped here
```

Consequences (each one is a P0-C1 defect):

| Event family | Today | Defect |
|---|---|---|
| `CTX_COMPACTION` / `CTX_TASKLIST` / `CTX_STILL_IN_PROGRESS` | skip | structure boundary thrown away with the body; after the 60 s timer flush (`__init__.py:4012-4020`) the continuation assistant finds `pending is None` → orphan (2,370) or misattr (1,276) |
| `OUT-OF-BAND` (real user, `display_kind=steer`) | skip | **real user content never reaches `conversation_stream`**; the reply misattrs (751) or orphans (5) |
| `[ASYNC DELEGATION…]` / `[IMPORTANT: Background process…]` | skip | boundary not cut → the next assistant appends to the previous human QA |
| `[System: …]` (SYS_*) | skip | accidentally transparent — correct by luck |
| `You've reached the maximum number of tool-calling…` / `Your previous final response was rejected by the operator…` | **no match** → treated as a user turn | **flushes the pending QA and opens a new one** → control events cut the task chain |
| `failed_turn` / `hidden` (role=assistant) | no match → appended | display-only transcript boundary enters QA answers |
| `### Task:\nSuggest 3-5 relevant follow-up questions` (TITLE_GEN) | no match → new QA | system housekeeping enters long-term memory |
| Second sync of the same snapshot | `cursor_key in prior_seen` → `continue` **before** classification | the structure of already-seen rows is invisible to this call |

Root cause, in one sentence: **the adapter asks "is this an injection, so should I skip it?"
instead of "what does this Hermes host event mean in the task structure?"**

## 2. New module — `src/v3-core/src/v3core/host_events.py`

Thin, deterministic, adapter-layer. No LLM, no embedding, no similarity, no episode model,
no schema change. Pure functions only.

```python
HOST_EVENT_REAL_USER        = "REAL_USER"
HOST_EVENT_CONTINUATION     = "CONTINUATION"
HOST_EVENT_ASYNC_BOUNDARY   = "ASYNC_BOUNDARY"
HOST_EVENT_NEW_ROOT         = "NEW_ROOT"
HOST_EVENT_CONTROL          = "CONTROL"
HOST_EVENT_TERMINAL         = "TERMINAL"
HOST_EVENT_NORMAL_ASSISTANT = "NORMAL_ASSISTANT"
HOST_EVENT_NORMAL_TOOL      = "NORMAL_TOOL"

def classify_host_event(*, role: str, content: str,
                        display_kind: str | None = None,
                        session_source: str | None = None) -> str: ...

def extract_oob_payload(content: str) -> str: ...
```

### 2.1 Classification order (first match wins — order is part of the contract)

1. `role` in {`tool`, `tool_call`, `tool_result`, `function`} → `NORMAL_TOOL`
2. `display_kind` in {`failed_turn`, `hidden`} → `TERMINAL`
3. `display_kind == "steer"` **or** content starts with `[OUT-OF-BAND USER MESSAGE` → `REAL_USER`
4. content matches CONTINUATION prefixes → `CONTINUATION`
   - `[CONTEXT COMPACTION` · `[Your active task list was preserved across context compression`
   - `[STILL IN PROGRESS` · `[System note: Your previous turn was interrupted mid-run`
5. content matches ASYNC prefixes → `ASYNC_BOUNDARY`
   - `[ASYNC DELEGATION` · `[IMPORTANT: Background process` · `[IMPORTANT: N background processes`
   - `[IMPORTANT: N background subagent delegations`
6. content matches NEW_ROOT prefixes → `NEW_ROOT`
   - `[IMPORTANT: You are running as a scheduled cron job` · `[IMPORTANT: The user has invoked the`
7. `session_source == "cron"` and `role == "user"` and no real-user shape → `NEW_ROOT` (§9)
8. content matches CONTROL prefixes → `CONTROL`
   - `[System:` · `[System note:` · `You just executed tool calls`
   - `You've reached the maximum number of tool-calling` · `Your previous final response was rejected`
   - `### Task:` + follow-up-question generator shape (TITLE_GEN, §10)
9. `role == "user"` → `REAL_USER`
10. `role == "assistant"` → `NORMAL_ASSISTANT`
11. anything else → `CONTROL` (fail-closed: never invent a human turn)

Prefix matching must reuse the existing tolerance for a leading timestamp/bracket wrapper
(the `_injection_pattern` in `__init__.py:3721` already normalises `Sat 2026-04-11 01:15 GMT+8] [X`).
Normalise once, then match on the normalised head.

`JUDGE_PROMPT` (`# 公文易 …`), `@file:` refs and ordinary prose must stay `REAL_USER` —
§10 forbids breaking the currently-correct 1:1 judge pairing.

#### 2.1.1 Legacy coverage (mandatory — regression guard)

The classifier MUST keep the full skip-surface of the legacy `_injection_pattern`
(`__init__.py`, git `HEAD`): every row that the old binary test dropped must still
resolve to a **non-`REAL_USER`** kind — except `[OUT-OF-BAND …]`, which is
intentionally promoted to `REAL_USER` (§2.1 #3). If a formerly-skipped row became
`REAL_USER` it would open a new QA → `NORMAL QA REGRESSION ≠ 0`.

Additional mandatory mappings (beyond the §2.1 list). All are **prefix/shape-exact**
— never a "looks like a system prompt" heuristic:

| shape (after wrapper normalisation) | kind |
|---|---|
| `[Subagent Context]` (commonly timestamp-wrapped) | `CONTROL` |
| `[AGENT_RULES` | `CONTROL` |
| `[Retry after` | `CONTROL` |
| `[cron:` | `NEW_ROOT` |
| `[System]` / `[System:` / `[System note:` | `CONTROL` |
| any other `[IMPORTANT…` not matched by ASYNC/NEW_ROOT | `CONTROL` (fail-closed) |
| `【IMPORTANT…` / `【ASYNC…` (full-width) | content-dispatched (below) |

Full-width `【IMPORTANT` / `【ASYNC` are content-dispatched:
`scheduled cron job` / `has invoked the` → `NEW_ROOT`;
`background process(es)` / `background subagent delegations` / `delegation` →
`ASYNC_BOUNDARY`; anything else → `CONTROL`.

`TERMINAL` additionally covers two display-only assistant content shapes:
`Operation interrupted.` (and its `: …` detail form) and
`[This response was interrupted by a user correction.]`.

`TITLE_GEN` (`### Task:` + `Suggest 3-5 relevant follow-up questions…`) → `CONTROL`
(metadata-only housekeeping; must not enter the memory body or open a QA).

`JUDGE_PROMPT` (`# 公文易 … 语义评审员（LLM Judge）`) stays `REAL_USER` — §10's 1:1
judge pairing is correct today and must not be broken; CONTROL matching is therefore
strictly prefix/shape-exact, not heuristic.

**Leading timestamp / bracket wrapper.** Real injections frequently arrive as
`Sat 2026-04-11 01:15 GMT+8] [Subagent Context] …` /
`Mon 2026-10-04 12:00 GMT+8] [IMPORTANT: …]` /
`Wed 2026-09-30 09:00 GMT+8] [ASYNC DELEGATION …]`. The classifier strips the leading
timestamp/bracket wrapper **once** (same date branches as the legacy pattern) and
matches on the normalised head. The wrapper is only stripped when a real `[…` host
head follows, so ordinary prose is never mangled.

### 2.2 `extract_oob_payload`

Strips the wrapper only:

```
[OUT-OF-BAND USER MESSAGE — a direct message from the user, …]
<the real message>
[/OUT-OF-BAND USER MESSAGE]
```
→ `<the real message>`. Never return the wrapper as the question. Preserve the original
`conversation_stream` source row (the wrapper text stays in the raw source; only the derived
QA question uses the payload).

## 3. Behaviour table (what `sync_turn` does per kind)

| kind | write `conversation_stream`? | pending / pairing effect |
|---|---|---|
| `REAL_USER` | **yes** (OOB: payload as content) | flush previous pending, open a new QA |
| `CONTINUATION` | no | **transparent** — does not flush, does not replace, does not become Q/A |
| `CONTROL` | no | **transparent** — identical to CONTINUATION for pairing state |
| `TERMINAL` | no | transparent for pending; must **never** append to `pending["a"]` |
| `ASYNC_BOUNDARY` | no | **cut the boundary**: flush/clear pending, set the per-snapshot `autonomous_boundary` flag |
| `NEW_ROOT` | no | same as `ASYNC_BOUNDARY` |
| `NORMAL_ASSISTANT` | yes | append to pending (existing path), else structural continuation (§4) |
| `NORMAL_TOOL` | no (collects tool context only) | unchanged from today |

Rule of thumb that the implementation must satisfy: **IGNORE CONTENT ≠ IGNORE STRUCTURE.**

P0-C1 does **not** build an `assistant_deliveries` table. When an autonomous output has no
legal derivation container the correct outcome is *no QA pollution* (held/orphan accounting,
source already durable in `conversation_stream`), not a synthetic question. P0-C2 owns durability.

## 4. Structural continuation (the blocker)

Required scenario:

```
sync #1:            U1, A1              → pending {q:U1, a:A1} → (60 s watchdog) → flushed
sync #2 (full):     U1, A1, CTX_COMPACTION, A2
                    ⇒ still exactly ONE QA (U1); A2 is durably appended to it
```

Two changes make this work.

**(a) Classify before the delta filter.** Today `cursor_key in prior_seen → continue` runs
*before* classification, so an already-seen row is structurally invisible. New order:

```python
for msg in messages:
    key  = _stable_message_key(msg)
    kind = classify_host_event(role=…, content=…, display_kind=…, session_source=…)
    # structure tracking happens for EVERY row, seen or not
    if kind == REAL_USER:                       struct.anchor = {msg_id, turn, host}; struct.boundary = False
    elif kind in (ASYNC_BOUNDARY, NEW_ROOT):    struct.boundary = True
    elif kind in (CONTINUATION, CONTROL, TERMINAL): pass          # transparent
    if cursor_key in prior_seen or in processed_this_call:        # delta only
        receipt duplicate; continue                               # no source write
    …per-kind action…
```

**(b) Recover the anchor when pending is gone.** `NORMAL_ASSISTANT` with `pending is None`
and `not struct.boundary` and a non-legacy host resolves against the snapshot anchor:

1. look up the anchor user row in `conversation_stream` (`session_id`, `host`, `event_id`)
   → its `host_turn_id`;
2. build `qa_source_id = self._qa_source_id(session_id, host_turn_id, event_id, host=host)`;
3. if a `qa_pairs` row exists → reuse the P0-A durable append (`_late_merge_into_qa`,
   guarded by `NOT (merged_event_ids @> [msg_id])`) → receipt `completed_late`;
4. if it does not exist yet → rebuild the pair from the durable U1 and `_submit_flush`
   (P0-A `recovered_late` shape);
5. any failure / no PG / legacy host → keep today's held/orphan semantics. Never guess.

`struct.boundary` short-circuits step 3-4: an autonomous output after a boundary must not be
folded into the last human QA.

## 5. Files touched

- **new** `src/v3-core/src/v3core/host_events.py` — classifier + `extract_oob_payload`.
- **edit** `src/v3-core/src/v3core/__init__.py` — `sync_turn` (classification-first loop,
  per-kind actions, structural continuation); the old `_injection_pattern` / fallback tuple is
  replaced by the classifier (keep the timestamp-normalisation tolerance).
- **new** `src/v3-core/tests/test_p0c1_host_event_semantics.py` — §11 matrix, driven against the
  real `sync_turn` with the in-memory fake PG used by `tests/test_p0a_late_assistant_derivation.py`.
- **new** `src/v3-core/tests/fixtures/host_events_p0c1.py` — §12 real, de-identified message
  shapes (extracted from the P0-C samples; never hand-invented strings).
- **new** `evidence/p0c1-hermes-event-semantics-20261004/` — E2E driver + before/after replay
  numbers + receipt.
- **new** `docs/P0C1-HOST-EVENT-SEMANTICS.md` — this contract, finalised after implementation.

## 6. Non-goals (explicitly forbidden this round)

`assistant_deliveries` schema · synthetic QA · LLM classifier · semantic-similarity pairing ·
`continuation_of` · episode model · Observer rebuild · E1 rebuild · P0-B cumulative writer ·
historical production backfill · LoCoMo · unrelated whole-repo refactors.
Findings that belong to P0-C2 / P0-B go to a backlog section, not into this change.

## 7. Acceptance (all must hold simultaneously)

```
NORMAL QA REGRESSION = 0
CTX continuation: compaction / tasklist / still-in-progress / after-flush recovery = PASS
OUT_OF_BAND: real user payload preserved = PASS; follow-up answer derived = PASS
CONTROL: does not cut task chain = PASS; does not enter memory body = PASS
TERMINAL: failed_turn / hidden excluded = PASS
ASYNC/BG: does not pollute previous QA = PASS
REPLAY = IDEMPOTENT   RESTART = PASS   NO LLM = PASS   NO EMBEDDING = PASS
Y400 disposable real PG = PASS   CI exact head = PASS   production mutation = NONE
```

Offline replay (read-only, `p0c_tax_rows.pkl`) must show CTX orphan/misattr → ≈0,
OOB skipped/misattr → ≈0, control-induced drift → 0, async contamination → 0,
with normal-dialogue pairing regression = 0, and the before/after numbers written to evidence.

## 8. Implementation status (2026-10-04)

### 8.1 `sync_turn` integration — 12 edit sites

| # | site | change |
|---|------|--------|
| 1 | `sync_turn` entry | `from .host_events import ...` |
| 2 | pre-loop | snapshot state `_struct_anchor` / `_struct_boundary` |
| 3 | per-row, **before** the delta filter | `classify_host_event(...)` + structure tracking (S4 STRUCTURE FIRST) |
| 4 | tool branch | `_kind == HOST_EVENT_NORMAL_TOOL or role in (...)` |
| 5 | body branch | per-kind behaviour replaces the `_injection_pattern` skip |
| 6-7 | user branch | `_q_text = extract_oob_payload(content) or content` becomes the QA question; the raw source keeps the full wrapper |
| 8 | assistant branch | structural continuation via `_resolve_late_assistant_derivation(anchor_event_id=...)` when `pending` is gone and the anchor is not shadowed |
| 9-12 | `_resolve_late_assistant_derivation` | new optional `anchor_event_id` locates the owning user turn by **event identity** (compaction rewrites turn identity) |

Two design points worth calling out:

- **Classification runs for every row, including already-seen ones.** The delta cursor
  now gates *source idempotency only*; structure is rebuilt from the full snapshot on
  every sync.  This is what makes the compaction-after-flush case work.
- **`_content.strip()` guards the anchor.** An empty-content user row classifies as
  `REAL_USER` but is dropped by the empty-content delta filter, so it must not become
  the anchor (a later assistant would try to durably attach to a row never written).

### 8.2 Known limitations / deferred

- **Cron sessions (task-book S9)**: `session_source` is read per-message
  (`msg.get("session_source") or msg.get("source")`).  Whether Hermes propagates a
  per-message source is **not verified** this round; when absent, legacy-format cron
  sessions fall back to orphan accounting (no wrong QA either way).
- **Production field availability**: `display_kind` is set by Hermes
  (`conversation_loop.py:311`, `context_compressor.py`); `turn_id` / `host` availability
  on the live adapter path is **not verified** here.  The classifier degrades gracefully
  (content prefixes only).
- **ASYNC / NEW_ROOT durable delivery** is out of scope (task-book S8) — those outputs
  stay source-only until P0-C2.
- **`[Subagent Context]` -> CONTROL** preserves the legacy skip behaviour; it was never
  a durable user turn.

### 8.3 Verification status

| layer | artifact | status |
|---|---|---|
| classifier contract | `tests/test_p0c1_host_event_semantics.py` (19 contract cases) | PASS |
| behavioural T1-T11 (fake PG) | same file (12 cases) | 31 passed |
| P0-A regression | `tests/test_p0a_late_assistant_derivation.py` | PASS |
| RED evidence | `evidence/p0c1-hermes-event-semantics-20261004/` | 31 failed pre-fix; 8 failed / 4 passed behavioural RED vs HEAD |
| Y400 disposable real-PG E2E | see evidence dir | pending |
| offline replay before/after | see evidence dir | pending |

Note on the conftest production-outbox guard: the P0-C1 tests use the isolated
`tmp_path` basePath (same harness as P0-A).  The one observed
"production outbox touched" assertion was the **host gateway's own live-buffer cycle**
(tree content hash unchanged; `j/pending_qa` is a directory that emptied via the normal
pending->accepted flow; journal entry at 23:16:36) — an environment-concurrency false
positive, not a test writing to production.
