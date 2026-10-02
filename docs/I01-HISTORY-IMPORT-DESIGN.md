# I01 — One-click historical memory import (DESIGN)

- Status: **DESIGN — locked before implementation**
- Base: `main` @ `f9c5fb22c511d73587d91d13716f032feffe7c13`
- Branch: `feature/i01-one-click-history-import`
- Product statement: **A new user who has used an agent for months must not start
  from zero.** `hippocampus import auto` discovers what is on the machine, previews
  safely, imports in one command, is idempotent, source-traceable, and the imported
  history is recallable from a fresh session without LLM or embedding credentials.

---

## 0. Scope

```text
IN:   auto discovery (hermes / dsh / pi / memory-md)
      one-command import (`import auto --dry-run` / `--yes`)
      deterministic QA pairing → recall-ready (qa_pairs)
      F2 identity preservation (host, session_id, event_id)
      self-memory exclusion (hippocampus-injected messages never re-imported)
      machine + human readable report (import_report.json)
      install-time hint when history is detected
      docs/IMPORT.md + INSTALL/STATUS sync

OUT:  recall rewrite / B01 bridge changes / schema rewrite / F2/F3 rework /
      LoCoMo re-run / observer changes / M01-M02 / GUI / cloud sync /
      production migration / new importer frameworks
```

Rejected by name: **"import = parse files = done"**. The gate is
`old fact → import → new session auto-recall → source resolvable`.

---

## 1. Recon findings (facts, verified during recon)

### 1.1 Two existing import systems (do NOT create a third)

| System | Path | Public entry | Capability |
|---|---|---|---|
| v3core.importers (framework) | `src/v3-core/src/v3core/importers/` | `hippocampus import <source> --root` | hermes + memory-md = production; openclaw + hindsight = framework_ready |
| tools/import_.py (older) | `src/v3-core/src/v3core/tools/import_.py` | `v3_import_seed` / `v3_import_full` tools | full QA-pairing pipeline for state.db/chat_md/md/trajectory; **not** exposed via the new CLI |

**Canonical public path chosen: `v3core.importers` + `hippocampus import`.**
`tools/import_.py` is reused as a *source of proven helper logic only* (QA pairing
rules, content-dedupe semantics), never as a second framework.

### 1.2 Write surfaces (verified against schema + callers)

- `conversation_stream` — raw messages. Columns include
  `host`, `event_id` (F2, additive 2026-09-28) + partial unique index
  `(host, session_id, event_id) WHERE all NOT NULL`
  (`schema/alpha_bootstrap.sql:172-183`).
- `qa_pairs` — **the recall hot source**. `source_id` is UNIQUE (canonical
  idempotency key; `ON CONFLICT DO NOTHING`). `recall_pool.py` and `prefetch.py`
  SELECT question/answer from `qa_pairs` only; `conversation_stream` does **not**
  participate in keyword/vector recall.
  → *Implication: raw-only import is NOT recallable. I01 must derive `qa_pairs`.*
- `explicit_memories` — user-curated + legacy-derived (ActiveMemoryWriter,
  canonical hash idempotency). Already used by memory-md importer.
- `topics` / `topic_entries` — observer-derived; **out of scope this round** (the
  observer and E1 run on their own schedules; I01 must not trigger them).

### 1.3 Existing dedupe (to be upgraded)

Current `_emit_raw_messages` uses `WHERE NOT EXISTS (session_id, role, timestamp)`.
I01 upgrades the write predicate to the **F2 identity** `(host, session_id, event_id)`;
rows without a native message id get a stable **import-derived** event id
(never masquerading as native; prefix convention documented in §3.3).

### 1.4 Latest host formats (LATEST FORMAT FIRST — no guessing from old installs)

#### DSH — deepseek-harness `0.2.0-rc.2`

```text
DSH_VERSION          = 0.2.0-rc.2
DSH_UPSTREAM_SHA     = 639ed015397290b3745d163aafe02ffee4aa3f84
                       (= tag dsh-v0.2.0-rc.2 = origin/master HEAD @ recon time)
```

- Session root: `$DSH_HOME/sessions` (config `dshHomePath('sessions')`,
  `packages/bundle/base/cordis.patch.yml:130`; `DSH_HOME` default `~/.dsh`).
- Layout: `<root>/--<normalized-cwd>--/<encoded-session-id>/session.vN.jsonl.zstd`
  (JSONL backend, checksummed zstd frames; `session-persistence-jsonl/src/format.ts`).
- Current format version: **v4** (`SESSION_FORMAT_VERSION=4`,
  `packages/core/session/src/types.ts:89`), header `version: 4`.
- Physical rows: line 0 = header
  `{type:"session", version, id, createdAt(ms), cwd, isSeeded, delegationDepth,
  parentSession?, origin?}`; every later line = one JSON event
  `{type, seq, time(ms), data, surfaceOp?, sourceEventSeqs?}`.
- Message events (only these two carry durable surface messages):
  - `user/message` — `data` IS the UserMessage:
    `{id: <MessageId>, role:"user", content:[ContentBlock], source:{kind}}`.
    **Record only `source.kind === "user"`** (human input). Our own injected
    recall carries `kind:"hippocampus"` (B04 contract) and is excluded structurally.
  - `assistant/message` — `data: {turn, step, message:{id, content, ...},
    stream, usage?, interrupted?}`. **Skip `interrupted === true`** (not an answer).
- Verified against a real file produced by the installed CLI (see §6.1 sample dump).

#### pi — `@earendil-works/pi` `v1.0.0`

```text
PI_VERSION           = 1.0.0
PI_UPSTREAM_SHA      = a13d35a742c6ef8462812a28fbe1d8c8b7431c32   (tag v1.0.0)
PI_MAIN_SHA          = 9fba660cf1caca0ade5bea72269352416e595a19   (main @ recon time)
```

- Session root: `getAgentDir()/sessions` → default `~/.pi/agent/sessions`; env
  overrides `PI_CODING_AGENT_DIR` (agent dir) and `PI_CODING_AGENT_SESSION_DIR`
  (session storage dir; `packages/coding-agent/src/config.ts:542-544`, `main.ts:688`).
- Layout: `<sessions>/--<escaped-cwd>--/<timestamp>_<session-id>.jsonl`
  (`docs/session-format.md`).
- Format: plain JSONL (no compression). Line 0 = header
  `{type:"session", version:3, id, timestamp, cwd, parentSession?}`.
  Entries `{type, id, parentId, timestamp, ...}` form a tree.
- Message entries: `type:"message"`, `message:{role, content, timestamp(ms), ...}`.
  Record only `role ∈ {user, assistant}`.
- Injection to exclude: `custom_message` entries with
  `customType:"hippocampus-memory"` (B03 contract, `MEMORY_CUSTOM_TYPE`).
- Branch/fork metadata: header `parentSession` + entry `parentId` — preserve in
  provenance when present, never invent.

#### Hermes — live store on this machine (and the general case)

- Home: `HERMES_HOME` env → platform default (`%LOCALAPPDATA%/hermes` on Windows,
  `~/.hermes` elsewhere; `hermes_constants.get_hermes_home`).
- Store: `<home>/state.db` SQLite, `messages` table with
  `id (rowid), session_id, role, content, tool_calls, timestamp (epoch float),
  display_kind, active, compacted, ...`.
- Role histogram on the real store (662k rows): assistant 280k (most tool-only,
  empty text), tool 369k, user 13.5k, system/session_meta small.
- Rows that are **system-generated pseudo-messages** and must be excluded:
  - `[CONTEXT COMPACTION — REFERENCE ONLY] …` (compaction summary markers)
  - `[System note: …` (interruption notes)
  - `display_kind = 'hidden'`
  - empty content (assistant tool-call-only rows, the empty user row)
- `steer` rows (`[OUT-OF-BAND USER MESSAGE …`) carry a real user message inside a
  system wrapper → **kept** (原文保真), because the inner text is user-authored.
- `active`/`compacted` states: both belong to compression bookkeeping. I01 imports
  the full conversation history (active=1, compacted=1 and active=0/compacted=0
  rows alike) minus the exclusions above. Compression summaries are never imported
  as user facts.

#### memory-md
Unchanged production importer (MEMORY.md / USER.md / SOUL.md / AGENTS.md / *.md →
explicit_memories, canonical hash idempotency).

### 1.5 Dedicated test machine (Y400) — current state

```text
DESKTOP-EQP3OBU — ssh y400 (user ranger)
pyenv     C:\hp-testbed\f2-overnight-20261002\pyenv  (v3core editable → candidate\src)
node      C:\hp-testbed\f2-overnight-20261002\node\node-v24.21.0-win-x64\node.exe
PG 17.10  @ 127.0.0.1:55432 (user f2e2e; password only via process env / secrets file)
DSH       0.2.0-rc.2 installed under C:\hp-testbed\b04-integration-20261002\
           with two real v4 session files under dsh-home\sessions\ (verified, 10-02)
```

---

## 2. Architecture

```text
                 ┌─────────────────────────── import auto ───────────────────────────┐
                 │                                                                   │
 [discovery]     │  hermes        dsh          pi          memory-md                  │
 resolve roots   │  HERMES_HOME   DSH_HOME     PI_* env    explicit --root            │
 (env > config    │  └ state.db     └ sessions/  └ sessions/  └ *.md (project)       │
  > platform      │                                                                   │
  default)       │        └──────────────┬─────────────┘                             │
                 │                       ▼                                           │
 [parse]         │   Importer.parse() → ImportItem(kind=raw, host, session_id,       │
                 │                      event_id, role, text, occurred_at, prov)      │
                 │                       ▼                                           │
 [derive]        │   deterministic QA pairing (no LLM, no embedding)                  │
                 │   user→assistant merge within session → qa_pairs candidates        │
                 │                       ▼                                           │
 [write]         │   conversation_stream   (F2 identity dedupe)                       │
                 │   qa_pairs              (source_id = qa_import/<host>/<sid>/<qid>)  │
                 │   explicit_memories     (curated; existing writer path)            │
                 │                       ▼                                           │
 [report]        │   <profile>/import_report.json + stdout summary                    │
                 └───────────────────────────────────────────────────────────────────┘
```

### 2.1 Modules

```text
src/v3core/importers/
  __init__.py         extensions: host/event_id plumbing, import_auto(), QA phase hook
  discovery.py        NEW — per-host resolvers + ImportSource dataclass (bounded,
                      deterministic, read-only, host-specific; prints no secrets)
  hermes_sessions.py  UPGRADE — host="hermes", event_id=native row id, exclusions
  dsh_sessions.py     NEW — zstd JSONL reader + v4/v3 event mapping
  pi_sessions.py      NEW — JSONL reader + message entry mapping
  memory_md.py        unchanged
  openclaw.py         unchanged (framework_ready)
  hindsight.py        unchanged (framework_ready)
  qa_pairing.py       NEW — deterministic pairing state machine + qa_pairs writer
```

CLI (`distribution_cli.py`):
`import auto` subcommand family; keeps `import list` and `import <source> --root`
exactly as they are.

### 2.2 ImportItem extensions (additive, backwards compatible)

New optional fields carried per raw item:
`host: str | None`, `event_id: str | None`, `identity_kind: str | None`
(`"native"` | `"import-derived"`). Existing constructors keep working.

---

## 3. Identity & idempotency (BLOCKER-class requirements)

### 3.1 conversation_stream identity

Write predicate becomes F2-canonical:

```sql
INSERT INTO public.conversation_stream
    (session_id, role, content, trigger, turn_id, timestamp, source, host, event_id,
     tool_calls, tool_results)
SELECT ...
WHERE NOT EXISTS (
    SELECT 1 FROM public.conversation_stream
     WHERE host = %s AND session_id = %s AND event_id = %s)
```

- `host` ∈ {`hermes`, `dsh`, `pi`} — matches B01/B04 host vocabulary; the B01
  bridge also writes this table with host names (`bridge_contract.LEGACY_HOST`
  stays for old rows).
- Re-run of the same import inserts 0 new rows **even if timestamps differ in
  sub-second precision** (identity no longer depends on timestamp).
- `source` column = `import:<host>` (existing convention `import:hermes` etc.).
- `trigger` = `import` (existing).

### 3.2 Native ids (must be preserved, never invented)

| Host | session_id | event_id | source |
|---|---|---|---|
| hermes | `messages.session_id` | `messages.id` (row id, stringified) | native |
| dsh | header `id` | `user/message.data.id`, `assistant/message.data.message.id` | native (MessageId) |
| pi | header `id` | `entry.id` | native |

### 3.3 Import-derived identity (only when a native id truly does not exist)

Format: `derived:<artifact-token>:<ordinal>` where `artifact-token` is a short
stable token of the source artifact (file stem + sha256 prefix for file-backed
sources; table+session for SQLite). `identity_kind="import-derived"` recorded in
ImportItem provenance and counted in the report. The prefix `derived:` makes the
distinction inspectable from `event_id` alone (B04 precedent: discriminate by
event_id prefix + host — no schema change).

### 3.4 qa_pairs identity

```text
source_id = qa_import/<host>/<session_id>/<q_event_id>
```

- `q_event_id` = the **first user message's** event_id in the pair (native or
  derived, same guarantees as above). Stable across re-runs; namespaced by host
  so two hosts reusing a session id cannot collide; greppable back to
  conversation_stream via `(host, session_id, event_id)`.
- `source` column = `import`.
- `timestamp` = first user message's occurred_at.
- `turn_id` = 1-based pair ordinal within (host, session_id) — deterministic
  because the pair stream is deterministically sorted (§4.1).
- `embedding` = NULL (base import never calls embedding; optional derived
  indexing is a separate, later step).
- ON CONFLICT (source_id) DO NOTHING → re-run = 0 new rows.

---

## 4. Deterministic QA pairing (raw → recall-ready, no LLM)

### 4.1 Algorithm (state machine per (host, session_id))

Input: raw items sorted by `(occurred_at, native order)`; stable ordering is a
hard requirement (deterministic turn_id / pair boundaries).

```text
pending_users = [], pending_assts = []
for msg in stream:
    if msg.role == "user":
        if pending_users and not pending_assts:
            pending_users.append(msg)          # consecutive user → one question
        else:
            flush(pending_users, pending_assts)  # closes previous pair
            pending_users, pending_assts = [msg], []
    elif msg.role == "assistant":
        if pending_users:
            pending_assts.append(msg)          # consecutive assistant → one answer
        # else: leading assistant with no user → not paired (raw only)
flush(pending_users, pending_assts)

flush(users, assts):
    if not users or not assts: raw-only, no qa_pair   # orphan → never a fake QA
    else: emit qa_pair(question=join(users), answer=join(assts), q_id=users[0].event_id)
```

- Consecutive user merging and consecutive assistant merging follow the
  established v3 pairing decision (multi-injection questions, multi-step answers).
- Orphans (no answer / no question) stay **raw-only** — "宁可只保存 raw，不要错误配 QA".
- Multi-step turns (user→asst→asst) merge the assistant texts with `\n\n`.
- DSH/pi/Hermes all flow through this one rule set; host-specific semantics were
  already applied during parse (interrupted skips, role filters, exclusions).

### 4.2 What pairing delivers

- `qa_pairs` rows → keyword recall (per-term snapshot) and vector recall (when
  embedding is later enabled) immediately serve the imported history; a fresh
  session's `prefetch` can recall it. **This is the product gate.**

---

## 5. CLI / reporting / install touch points

### 5.1 Commands

```text
hippocampus import list                       (existing, unchanged output shape)
hippocampus import auto [--dry-run] [--yes]
                          [--hosts hermes,dsh,pi,memory-md]
                          [--root <path>]      (single-host override only)
                          [--json] [--profile-dir <dir>]
hippocampus import <source> --root <path>     (existing per-source path, unchanged)
```

- `--dry-run`: default-safe; **zero writes, zero LLM, zero embedding**; prints
  detected sources with sessions/messages/oldest/newest/skipped reasons.
- `--yes`: required for non-interactive import. TTY without `--yes` → one
  `Import now? [Y/n]` prompt. Non-TTY without `--yes` → print detection summary
  and exit 2 (never hang CI).
- `--json`: machine-readable stdout (same payload written to the report).
- One source failing never aborts the others → overall status `PARTIAL` with
  per-source error class/count; parse errors are counted and **shown**, never
  silently skipped (§18 of the mission).

### 5.2 Report (`<profile>/import_report.json`)

Fields: source hosts, artifacts, sessions discovered, messages discovered, raw
imported, qa_pairs derived, user-curated imported, duplicates skipped,
unsupported skipped, non-text skipped, errors, oldest, newest, recall_ready,
duration, per-source detail. **Rewritten (not appended)** on each run; no
unbounded report files. No secrets ever (no tokens, no connection strings).

### 5.3 Install hint (`hippocampus install`)

After a successful install, if any host history is detected (same discovery
code), print:

```text
Existing agent history detected.
Run:  hippocampus import auto --dry-run
```

Detection failures must be swallowed (install must not fail because discovery
failed). `--import-history` flag is a stretch item — not a gate.

---

## 6. Evidence & test plan

### 6.1 Fixtures — from REAL formats (not "looks like DSH")

```text
fixtures/dsh/     real session.v4.jsonl.zstd bytes (from the Y400 B04 run,
                  re-generated in E2E; committed as bytes + a small redacted
                  text twin for readability)
fixtures/pi/      real pi v1.0.0 session .jsonl — generate on Y400 by running
                  the real pi CLI (no network model needed for session file
                  creation? see §6.3; fallback: a session emitted by pi's own
                  SessionManager in SDK mode)
fixtures/hermes/  existing fixtures + extended coverage (exclusions)
```

Format provenance (versions + SHAs from §1.4) recorded in the fixtures README.

### 6.2 Test layers

1. Unit (hermetic, no PG): discovery resolvers (env precedence, bounded paths,
   missing dirs), hermes/dsh/pi parsers (native id preservation, exclusions,
   non-text skip counting), QA pairing table-driven cases, idempotency SQL shape,
   dry-run zero-write, report shape, partial-failure reporting, CLI arg gate.
2. Fixture parse tests: golden counts + provenance for each host fixture.
3. Real isolated E2E on Y400 (disposable PG, fresh profile, real CLI paths):
   - synthetic old history: a fact ("我们的迁移项目内部代号叫银杏") written into a
     real DSH session file (via real CLI) and a real pi session file;
   - `import auto --dry-run` → discovers sources, zero writes;
   - `import auto --yes` → imports; re-run → `duplicates skipped, new rows = 0`;
   - fresh DSH session (real adapter + real bridge + credential-free stub) asks
     about the fact → automatic recall → block contains 银杏 → source resolvable
     to the exact conversation_stream row;
   - cross-host: hermes → import → DSH recall (stretch, low-cost path);
   - source files unchanged (hash before/after); production untouched.

### 6.3 Non-goals / honest limits

- Model-endpoint scope is the same as B04: credential-free local stub; real
  model answer remains an optional smoke.
- embedding stays disabled in base import (keyword-only proof).
- openclaw/hindsight stay framework_ready.
- Format compat claims are bounded: DSH v4/v3, pi v3(.1/.2 tolerated), Hermes
  current state.db — no "supports all versions" claims.

---

## 7. Decisions log (Why)

| # | Decision | Rationale |
|---|---|---|
| D1 | Canonical = `v3core.importers`; reuse `tools/import_.py` logic only | Mission §2: no third framework; the CLI already calls this package |
| D2 | QA pairing is deterministic, in-memory, no LLM | Mission §9/§11; qa_pairs is the recall hot path; import must work with zero credentials |
| D3 | F2 identity `(host, session_id, event_id)` replaces timestamp dedupe | Mission §7; B04/F2 contracts; timestamp-tie fragility |
| D4 | Import-derived ids use `derived:` prefix; native ids stay untouched | Inspectable, no schema change, "never masquerade as native" |
| D5 | qa_pairs.source_id = `qa_import/<host>/<sid>/<qid>` | Stable + host-namespaced + traceable; distinct from live `qa_sync/...` |
| D6 | steer kept / system-note + compaction + hidden excluded (hermes) | Preserve user-authored text; never import system pseudo-messages as facts |
| D7 | zstandard as a dependency; graceful `unsupported` reason if absent | DSH default storage is zstd; import must not crash without it |
| D8 | discovery is bounded to known per-host roots (env > config > platform default) | Mission §5 prohibitions (no C:\ scans, no fuzzy sqlite hunting) |

## 8. Risks

- **R1**: DSH multi-frame zstd appends — reader must read across frames (tested
  on real files; use stream reader with cross-frame support).
- **R2**: Hermes state.db is live (WAL churn) while importing — open read-only
  (`mode=ro` URI), never mutate; worst case fall back to a snapshot copy without
  asking the user to stop the agent. Large store → streaming parse, bounded
  memory per row.
- **R3**: pi has no installed CLI on Y400 at recon time — E2E for pi may have to
  use the pi SDK to emit a session file, or install pi on Y400; if pi cannot be
  exercised end-to-end this round, I01 is declared PARTIAL with the reason, not
  fake-passed.
- **R4**: `--root` semantics for multi-host imports — restricted to single-host
  override to stay predictable.
