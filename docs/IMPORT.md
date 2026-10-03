# Import — bring your existing history into memory

> **Audience:** anyone who has been using an agent host (Hermes, DeepSeek
> Harness, pi) for a while and wants a fresh Hippocampus install to
> remember that history — instead of starting memory from zero.
>
> **Scope:** one-command import of on-disk session history into
> `conversation_stream` (raw, verbatim) plus deterministic QA pairs that
> make the imported history recallable. Local only: no LLM, no embedding,
> no network calls — just your files and your PostgreSQL.

---

## What can be imported

| Host | What is read | Default location |
|---|---|---|
| **Hermes** | `state.db` (SQLite), plus any top-level `.jsonl` / `.json` exports | `%LOCALAPPDATA%\hermes\` (Windows) · `~/.hermes/` (Linux/macOS) |
| **DSH** | `session.v*.jsonl.zstd` session files (zstd-compressed JSONL) | `~/.dsh/sessions/` (or `$DSH_HOME/sessions/`) |
| **pi** | session `.jsonl` files (v1–v3 entry format) | `~/.pi/agent/sessions/` |
| **memory-md** | Your hand-maintained markdown: `MEMORY.md`, `USER.md`, `SOUL.md`, `AGENTS.md` | the current working directory |

Anything else on disk is ignored. Discovery never scans outside the
locations above, and it never walks your whole disk.

## One-command auto flow

```bash
hippocampus import auto
```

That single command:

1. **discovers** every supported host on this machine,
2. **parses** the session files it finds,
3. writes each original message to `conversation_stream`, and
4. derives deterministic **QA pairs** so the history is immediately
   recallable in new sessions.

Useful flags:

```bash
hippocampus import auto --dry-run                  # report only; write nothing
hippocampus import auto --hosts dsh,pi             # limit to specific hosts
hippocampus import auto --override dsh=D:\sessions # point a host at a path
hippocampus import auto --json                     # machine-readable report
```

After a live run the full report is also written to
`<profile>/import_report.json`.

## Dry-run

```bash
hippocampus import auto --dry-run
```

Dry-run performs the complete discovery and parse — you get real counts
for every host — but writes nothing: no database rows, no report file, no
changes to any source. Use it to sanity-check what a live import would do.

```text
Found:
  hermes     2047 sessions   79997 messages
  dsh           2 sessions       6 messages
  pi            1 sessions       6 messages
  memory-md — not found (no MEMORY.md in the working directory)

No source will be modified.
No LLM will be called.
```

## What is preserved

- **Original text, verbatim.** Messages are stored exactly as they were
  written — no summarising, no rewriting.
- **Native identity.** Every imported message keeps its original event id
  (the host's own message id), its host and session ids, and its original
  timestamp. Re-importing is keyed on `(host, session_id, event_id)`.
- **Host separation.** Hermes / DSH / pi rows stay distinguishable in the
  database (`host` column) and in every QA pair's `source_id`.
- **Deterministic pairing.** A user message followed by assistant
  replies becomes one QA pair; consecutive user messages merge into one
  question, consecutive assistant messages into one answer. No model is
  involved, so the same input always produces the same pairs.
- **Source traceability.** Every QA pair can be traced back:
  `qa_pairs.source_id` → `conversation_stream` row → the original event
  id in the host's own files.

## What is not imported

The import is deliberately conservative — it skips anything that is not
a durable human/assistant message:

- **Runtime notifications** — background-process notices, model-switch
  notes, async-delegation banners, failed-turn placeholders.
- **Compaction summaries and system notes** — `[CONTEXT COMPACTION …]`
  and `[System note: …]` pseudo-messages.
- **Hidden / internal rows** — messages the host itself marks as hidden
  or non-conversational.
- **Hippocampus's own injections** — recalled-memory blocks injected into
  pi (`customType: hippocampus-memory`) and DSH (`kind: hippocampus`)
  sessions are excluded, so memory never re-imports itself.
- **Tool traffic and empty rows** — tool calls/results, and empty
  assistant rows that only carry tool metadata.
- **Orphan messages** — a user message with no reply is kept in the raw
  stream but does not become a QA pair (better raw-only than a wrong pair).

Everything skipped is counted in the report, per reason.

## Idempotency

Run the import as many times as you like:

- raw rows are keyed on `(host, session_id, event_id)` — a re-run inserts
  zero new rows for events already present;
- QA pairs are keyed on `source_id` (`qa_import/<host>/<session>/<event>`)
  — a re-run inserts zero new pairs.

The second run's report shows the duplicates it skipped, so you can see
the idempotency working. Re-running after your hosts accumulate new
sessions imports only the new part.

## Privacy

- **Everything is local.** The import reads local files and writes to
  your PostgreSQL. It does not call an LLM, does not call an embedding
  endpoint, and does not send anything over the network.
- **Read-only sources.** Host session files are opened read-only and are
  never modified, moved, or deleted. (A Hermes `state.db` is opened with
  SQLite's read-only URI mode.)
- **Embedding is separate.** Imported QA pairs have no embeddings yet;
  that is intentional — base import must work without any model
  credential. The normal recall rebuild/indexing flow fills embeddings
  later, when you have an embedding endpoint configured.

## Troubleshooting

**`dsh` not found.** DSH stores sessions under `~/.dsh/sessions/`. If
your DSH home is elsewhere, point at it explicitly:

```bash
hippocampus import auto --override dsh=D:\path\to\dsh-home
```

(or set `DSH_HOME`).

**`pi` not found.** pi sessions live under `~/.pi/agent/sessions/`.
If you set `PI_CODING_AGENT_DIR` or `PI_CODING_AGENT_SESSION_DIR` for
pi itself, the importer honours the same variables.

**`memory-md` not found.** The markdown importer reads the *current
working directory* — run the command from the directory that holds your
`MEMORY.md`.

**`zstandard` missing.** DSH session files are zstd-compressed. Install
the dependency: `pip install zstandard`.

**Status says `PARTIAL`.** One host failed while others succeeded — look
at the `errors` section of the report (`--json` for details). A common
cause is an unreadable file; the other hosts' imports are unaffected.

**Status says `FAILED`.** Nothing was imported. The report's `errors`
list the cause — most often the database is unreachable (check your
`config.yaml` and `V3CORE_PG_PASSWORD`) or the profile config is
missing.
