# M03 — correction propagation into derived memory

Status: design contract for `feature/m03-derived-correction-propagation`, based on
`main` = `23865f74366e9c39e6860f9d1658ece77f224e33` (the M01/M02 merge).
Companion reconnaissance: [`M03-DERIVED-LINEAGE-MAP.md`](M03-DERIVED-LINEAGE-MAP.md).

## What this solves

M01/M02 make an explicit user correction `A -> B` durable, authoritative and
inspectable. They do **not** touch anything the system derived from A. So after a
correction, a Topic / Observer note / E1 paragraph that still says "A is the
current answer" can keep being injected into a new session as if the user had
never spoken.

M03 closes exactly that gap, and nothing else:

```
M02 correction COMMIT (durable, authoritative)
        |
        v
M03 deterministic invalidation  (bounded, no LLM, idempotent)
        |
        v
optional local rebuild          (may need a provider)
```

The order is not negotiable. `CORRECTION FIRST`, `INVALIDATION SECOND`,
`REBUILD THIRD`. A provider, embedding or rebuild failure can never undo `A -> B`,
restore A as current, or roll the correction back. If propagation itself fails,
the correction stays durable and the result reports
`derived_propagation = pending`.

## The anchor problem, and the decision

Reconnaissance (`M03-DERIVED-LINEAGE-MAP.md` §3) established that **no
deterministic edge from `explicit_memories.memory_id` to any derived artifact
exists today**. `v3_store(source_id=...)` is an alias of `memory_id`, not a source
link (`active_memory_store.py:879-897`). Text matching, vector similarity and LLM
judgement are forbidden as mapping evidence.

So M03 declares one canonical, **validated** source reference on an explicit
memory, following the discipline M01/M02 already established for
`correction_source_id` (`schema/memory_relations.sql:51-57`,
`active_memory_store.py:96-101`): the value is stored literally, must resolve to a
real row, and is refused — never fabricated, stripped or normalised — when it does
not.

Accepted forms, in `explicit_memories.provenance`:

| key | value | validation |
|---|---|---|
| `source_conversation_stream_id` | plain ASCII decimal id of a real `public.conversation_stream.id` with `role='user'` | refused if malformed or unresolvable |
| `source_qa_id` | plain ASCII decimal id of a real `public.qa_pairs.id` | refused if malformed or unresolvable |

Supplying both with disagreeing values is refused, mirroring
`active_memory_store.py:883-890`. The resolver normalises both forms to one
internal identity — **`qa_pairs.id`** — before matching anything, so the matching
logic is a single path.

### Frozen source-key contract

Owner-frozen 2026-10-03. **Two accepted input forms, one derived identity.**

```
Accepted input:            conversation_stream.id | qa_pairs.id
Canonical internal identity: qa_pairs.id
```

Resolution rules — all of them fail closed:

```
conversation_stream.id
  -> read the real conversation_stream row
  -> role MUST be 'user'                      else SOURCE_NOT_MAPPED
  -> use that row's real canonical identity (host, session_id, event_id)
  -> build the exact canonical candidate strings and match qa_pairs.source_id
       'qa_sync/<host>/<session_id>/<event_id>'     live sync
       'qa_import/<host>/<session_id>/<event_id>'   I01 import
  -> EXACTLY ONE row must match
       0 rows -> SOURCE_NOT_MAPPED
       >1     -> SOURCE_AMBIGUOUS
  -> normalize = qa_pairs.id

qa_pairs.id
  -> must resolve to a real public.qa_pairs row  else SOURCE_NOT_MAPPED

both supplied
  -> resolve each independently; the final canonical qa_pairs.id must be EQUAL
  -> otherwise SOURCE_IDENTITY_CONFLICT (refuse; never prefer one)
```

Forbidden as resolution evidence, without exception:

```
text matching | nearest-in-time | nearest QA in the session
turn-based inference | embedding / similarity guessing
```

**Why this matters for imported history:** I01's QA identity is
`qa_import/<host>/<session_id>/<first_user_event_id>`, where consecutive user
messages merge into ONE question and only the **first** user event becomes the
QA's identity (`importers/qa_pairing.py:10-22, 83, 97`). A `conversation_stream`
row with `role='user'` therefore does **not** imply that a QA row exists — a
second consecutive user message has none, and the correct answer there is
`SOURCE_NOT_MAPPED`, never the nearest QA. Legacy live rows
(`qa_sync/<session_id>/<turn>/<msg_id[:16]>`, `__init__.py:4054`) need `q_turn`,
which has no equivalent truth in `conversation_stream`; those resolve to
`SOURCE_NOT_MAPPED` rather than being inferred.

The join is structural, not guessed: the same `it.source_ref` supplies the
session component of both sides (`importers/__init__.py:370`,
`importers/qa_pairing.py:62`), and the same `msg_id` supplies `event_id` and the
live QA identity (`__init__.py:3700-3706, 4039-4055`).

Lineage is persisted with `canonical_qa_id = qa_pairs.id`, plus
`input_source_kind` (`conversation_stream` | `qa_pairs`) and `input_source_id`
(the caller's original value) for audit. The deterministic `invalidation_id`
never includes the input form, so the two entries can never produce two derived
identities for the same fact.

Why both: `conversation_stream.id` is the convention M01/M02 already validate and
is what a host naturally holds; `qa_pairs.id` is the derived side's native key
(`topic_entries.source_qa_id`, `observation_notes.source_qa_range`) and removes the
template hop. `conversation_stream -> qa_pairs` is structural, not guessed: the
same `msg_id` builds the live item's `event_id` (`__init__.py:3700-3706`) and
`qa_pairs.source_id = 'qa_sync/<host>/<session_id>/<msg_id>'`
(`__init__.py:4039-4055`), and `conversation_stream` carries the partial unique
index `(host, session_id, event_id)` (`schema/alpha_bootstrap.sql:200-202`).

This is the "minimal reinforcement so future writes can be located" the task
allows, and it is deliberately **not** a backfill: historical memories keep
whatever provenance they already have, and everything that cannot be mapped is
reported as unmapped.

## What becomes stale — and at what granularity

| derived kind | mappable when | granularity | notes |
|---|---|---|---|
| `topic` | the resolved `qa_pairs.id` appears in `topic_entries.source_qa_id` for that `topic_id` | whole topic | only the observer's single-QA direct attach populates that column; other entry paths leave it NULL |
| `observer_note` | the resolved `qa_pairs.id` is contained in `source_qa_range` | whole note (`id`) | the range is a nominal half-open span built from a timestamp-ordered batch (`observer.py:4665-4669`), so it can be a superset |
| `yin_paragraph` | never, from current structure | — | `yin_paragraphs` has no source column and the `<!-- premise: -->` annotation is written only for one section kind and in a mismatched id space |

Anything that can only be judged by body text, vector similarity or an LLM is
**not** auto-adjudicated. It stays `UNKNOWN_DEPENDENCY` and is counted in the
unmapped report. M03 does not attempt a whole-corpus semantic rewrite.

## Historical content is never rewritten

Invalidation is a **sidecar fact**, never an edit:

```
old derived artifact:  content = still A, state = stale
new derived artifact:  content = rebuilt from B, state = active
```

No `UPDATE topics SET body = replace(A, B)`. No `DELETE topic` / `DELETE
observation` / `DELETE yin`. Stale artifacts stay physically present and stay
readable by id / history / source trace — they simply stop being eligible for
current automatic injection.

## The invalidation sidecar

One small table, one uniform mechanism for every derived kind — not a different
correction column on four tables:

```
public.derived_memory_invalidations
  invalidation_id       TEXT PRIMARY KEY   -- 'inv_' + sha256(correction_id|derived_kind|derived_id)
  correction_id         TEXT NOT NULL      -- M02 'cor_' identity
  relation_id           TEXT               -- memory_relations.relation_id
  derived_kind          TEXT NOT NULL      -- CHECK IN ('topic','observer_note','yin_paragraph')
  derived_id            TEXT NOT NULL
  source_memory_id      TEXT NOT NULL      -- A
  replacement_memory_id TEXT               -- B, NULL for a withdrawal
  canonical_qa_id       BIGINT             -- normalized identity (public.qa_pairs.id)
  input_source_kind     TEXT               -- CHECK IN ('conversation_stream','qa_pairs') or NULL
  input_source_id       TEXT               -- caller's original value, kept verbatim for audit
  state                 TEXT NOT NULL      -- CHECK IN ('stale','pending_rebuild','rebuilt','unresolved')
  reason                TEXT
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
  resolved_at           TIMESTAMPTZ
  replacement_derived_id TEXT
  UNIQUE (correction_id, derived_kind, derived_id)
```

The `UNIQUE` constraint plus the deterministic `invalidation_id` make propagation
idempotent: replaying the same propagation produces **zero** new rows. Additive,
idempotent, no backfill; a missing table is a truthful `MIGRATION_REQUIRED`, never
a silent fallback to a default profile.

## Current recall must recognise staleness

Writing rows is not M03. The product gate is that the **real injection paths**
stop injecting invalidated derived content:

| lane | suppression point | how the id is known |
|---|---|---|
| Topic, query recall | `recall_pool.py:3594` (single convergence before RRF/rerank) | `source_id = 'topic_<topic_id>'` |
| Topic, first round | `injector.py:524` | dict carries `topic_id` |
| Observer note | `observer.py:5316` / `5405` (chain-head selection) | `note_id` |
| Yin paragraph | `__init__.py:3240` | `yin_version` / `section` (paragraph `id` must be added to the SELECT at `pg_store.py:1117` for id-level filtering) |

Suppression happens at the injection layer only. Existing recall behaviour for
non-invalidated candidates is unchanged, and Recall V2 is not rewritten.

Two bounded limitations are stated, not hidden: the `TopicRecall` snapshot can
serve an already-invalidated topic for up to ~60s
(`topic_recall_cache.py:48/209/232`), and a stale observer note is handled by
walking the existing `prev_id` chain to the nearest non-stale note rather than
falling back to the stale one.

## Rebuild

Rebuild is local and on demand — never a full historical rebuild, never a full
re-embedding, never a full Observer / E1 rerun. The rebuild input is assembled
through the canonical correction state, so the generation context sees
`A = historical/superseded, B = current` and cannot re-derive A as current from
the old raw history. Raw source rows are never modified.

Scope for this round, stated up front:

```
rebuild support:
  topic          = complete
  observer_note  = pending / manual
  yin_paragraph  = pending / manual
```

Only `topic` needs a working rebuild to satisfy the round. Observer and Yin must
still be invalidated and suppressed, and their missing rebuild is recorded as a
limitation rather than hidden. A rebuild that cannot run because no provider is
available keeps the artifact suppressed — `replacement_memory_id = null` for a
withdrawal, and no invented "not A" text.

State semantics (decided 2026-10-03, pinned by test): the propagation layer never
rebuilds, so it writes exactly two states — `pending_rebuild` when a live
replacement exists and a rebuild is owed, `stale` when the chain ends in nothing
current (a withdrawal) and there is nothing to rebuild from. `rebuilt` is written
only by the rebuild stage; `unresolved` is reserved for a rebuild that ran and
could not determine the replacement. Both written states suppress recall.

## Re-running propagation

Propagation is triggered after a committed correction, but it is also exposed as
an explicit maintenance action so a failed or newer-versioned propagation can be
replayed for a single `correction_id` — extending the existing maintenance entry
point rather than introducing a daemon, a service or a queue.

## Not in scope

M04 temporal reasoning (`as-of` queries, current-vs-historical intent routing,
cross-project temporal conflict resolution), a generic knowledge graph
(entities / claims / beliefs / temporal edges), any GUI, and any backfill of
provenance for historical content. M03 only guarantees that a **known** obsolete
A stops being propagated as the current conclusion by currently supported derived
paths.

## Honest reporting requirements

- the mapped / invalidated / unmapped / rebuild-pending / rebuilt counts are
  runtime values, never estimates;
- `UNMAPPED_HISTORICAL_DERIVED` is reported as a first-class outcome, not a
  failure;
- the Y400 E2E reports `production mutation = NONE`;
- nothing is claimed about a derived kind whose rebuild was not exercised.

## Stage 1 delivered (2026-10-03)

Landed on `feature/m03-derived-correction-propagation` off `main` `23865f74`:

- `schema/derived_memory_invalidations.sql` (+ byte-identical packaged mirror,
  LF-only, CR=0) with the include wired into `alpha_bootstrap.sql` and
  `upgrade_v0_2.sql` (both copies each) and `pyproject.toml` package-data. The
  artifact is never auto-applied; an absent table is `PROPAGATION_MIGRATION_REQUIRED`,
  never a silent fallback to a default profile.
- `src/v3core/derived_invalidation.py` — resolver, structural discovery,
  idempotent sidecar write, propagation orchestration, reads. 1101 lines,
  **zero** `DELETE`/`UPDATE`/`DROP`/`TRUNCATE` anywhere: it can never rewrite a
  derived artifact's content, which is pinned by source-level tests.
- Tests: `test_m03_derived_invalidation_schema.py` (23) and
  `test_m03_derived_invalidation.py` (46). Parent re-run without any guard
  downgrade: **23 passed** and **46 passed**.

Reuse rather than a second mechanism: leases go through
`active_memory_store._acquire_lease` (so the open-side-connection fail-closed rule
is identical), the ASCII-decimal literal rule is
`active_memory_store._is_source_id_literal`, and the A→B→C current successor is
resolved with `ActiveMemoryReader.read_version(mode='current')` → `read_chain`.
The sidecar write takes driver ownership the same way M01/M02 learned to
(autocommit off **before** the explicit `BEGIN`, baseline restored before lease
release) and is never nested inside an M02 transaction.

Known limitations, stated rather than hidden:

1. Not every importer follows the `(host, session, event)` identity. The
   `tools/import_.py` paths use `qa_import/state/<message_id>` (`:420`) and
   `qa_import/<source_tag>/<turn.turn_id>` (`:644`); those cannot be resolved
   from `conversation_stream` and correctly return `SOURCE_NOT_MAPPED`. No
   matching is invented for them.
2. Legacy live rows `qa_sync/<session_id>/<turn>/<msg_id[:16]>` need `q_turn`,
   which `conversation_stream` does not carry — also `SOURCE_NOT_MAPPED`, never a
   turn-based inference.
3. `distribution_cli.py` hardcodes its packaged-SQL inventory and live
   `required_tables` probe, so `hippocampus doctor` emits a false include-presence
   warning for the new artifact and does not yet flag a live DB missing the table.
   Wiring the doctor and the live `MIGRATION_REQUIRED` path belongs to the M03
   runtime stage (Stage 3), not to the schema artifact.
4. Two pre-existing failures in `test_alpha_bootstrap_contract.py`
   (`explicit_memories` repo copy CRLF vs packaged mirror LF) exist at the base
   commit and are byte-for-byte identical before and after this change; they are
   a worktree-checkout artifact of this author host (they pass where the tree is
   checked out LF-only, as on Y400). Left untouched.
