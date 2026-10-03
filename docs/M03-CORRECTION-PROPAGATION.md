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

**Suppression is permanent; `state` is lifecycle only.** Every row in this table
marks a permanently stale artifact, so recall suppression is driven by the mere
existence of the row and never by its `state`. A successful rebuild produces a
NEW artifact D2 and records it as `replacement_derived_id`; D1 stays suppressed
forever, because D1's content is still the superseded derivation:

```
old D1 stale
new D2 active
replacement_derived_id = D2

current recall:
D2 allowed
D1 suppressed
```

That is the task book's §31 verbatim, and §39 lists both
`provider rebuild failure 重新激活 stale content` and
`correction durable，但明确 mapped stale derived 仍能进入 current injection` as
M03 blockers. Filtering suppression by `state <> 'rebuilt'` would therefore
re-admit D1 the moment its rebuild completed — an earlier revision of this
implementation did exactly that and was corrected. `replacement_derived_id` is
the only field that records which artifact took over.

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

## Stage 2 delivered (2026-10-03)

**Recall suppression** — `src/v3core/derived_suppression.py` (new, 314 lines) is
the gate. It only READS the sidecar, never writes, never touches a derived body,
and only ever DROPS candidates at the injection layer.

| lane | suppression point | id used |
|---|---|---|
| topic, query recall | `recall_pool.py:3597` (single convergence before RRF/rerank) | `topic_<topic_id>` source_id |
| topic, first round | `injector.py` (`_m03_filter_first_round_topics`, before units are built) | `topic_id` from the recall dict |
| observer note | `observer.py:5311` / `:5411` via `select_note_head` | `observation_notes.id`, walking `prev_id` |
| yin paragraph | `__init__.py:3240` (`_recall_yin_segments`) | `yin_paragraphs.id` |

`select_note_head` never falls back to an invalidated note: it walks the existing
`prev_id` chain to the nearest admissible note, a fully-invalidated chain yields
no candidate, and a cycle is guarded. `pg_store.search_effective`'s yin branch
gained the trailing `id` column only — row set, order and cosine unchanged.

Degraded reads **fail open but are visible**: `invalidated_ids` returns an empty
set on failure, so an empty result is disambiguated by one bounded readability
probe on the same table; a truly unreadable sidecar yields `degraded=True`, a
WARNING carrying the literal `suppression_degraded=True`, and a structured
`suppression_degraded` field where the lane has one (injector, observer). A
sidecar read failure can therefore never turn a live correction into a recall
outage, and `cannot determine != invalidated` is enforced in code.

**Write side** — the two frozen keys are accepted and validated through the ONE
canonical resolver (`derived_invalidation.resolve_source_qa_ids`), never a second
parser. A declared-but-unresolvable, ambiguous or conflicting reference is
REFUSED with the frozen code and nothing is written; a memory that declares no
source key behaves byte-for-byte as before. On success the row's provenance gains
`canonical_qa_id` + `input_source_kind` + `input_source_id`.

**Trigger and rerun** — after a correction is committed and verified,
`_maybe_propagate_after_correction` runs the bounded, no-LLM propagation once.
It is strictly past the point of no return: a failure can never undo A→B and is
reported as `derived_propagation='pending'` alongside the propagation receipt.
`propagate_correction` reads the source memory's provenance from the database
through the canonical read path, so the DB is the single source of truth and the
trigger's precondition check is only a saved round-trip. The explicit rerun is
`v3_update(action='propagate', correction_id=...)`, idempotent by construction
(a second run inserts zero rows and reports `PROPAGATION_DEDUPLICATED`), with the
tool-layer name `propagate_dry_run` deliberately kept distinct from
`topic_maintain`'s `dry_run` so a default `True` cannot silently turn a rerun into
a preview. `distribution_cli._doctor` now knows the artifact in both the packaged
inventory and the live `required_tables` probe.

Parent-run verification, no guard downgrade anywhere:

```
pytest (M03 four files)                                     -> 107 passed
pytest (M03 + M01/M02 + recall-v2 + bootstrap/distribution)  -> see below
```

Nits found by parent review and fixed here: the injector's
`last_suppression_degraded` was sticky (`or self.last_suppression_degraded`),
which made a field named *last* report "ever" — now per-round, with the durable
record left to the WARNING that is emitted on every degraded read.

Open items, stated not hidden:

1. `distribution_cli._UPGRADE_REQUIRED_TABLES` was deliberately NOT extended:
   adding the table flips the frozen `plan_sha` assertions in
   `test_production_upgrade_contract`. An upgrade dry-run therefore does not yet
   list `derived_memory_invalidations` as required. Propagation itself does report
   a truthful `MIGRATION_REQUIRED` when the table is absent.
2. The `recall_pool` topic lane has no structured result field to carry the
   degradation marker (its `(hits, pg_fail)` arity is fixed by existing callers),
   so it surfaces via WARNING + an injection-trace `warn` probe only.
3. The yin lane reads the sidecar through the unbound store rather than the
   deadline-bound copy — one bounded extra query, not deadline-clamped.
4. The topic query lane reads the sidecar on every `recall_pool` call (one query,
   plus one `LIMIT 1` probe when the result is empty). Per-call cost not
   benchmarked.
5. Every lane is proven against in-memory fakes; no live-PostgreSQL E2E has run
   yet. That is the Y400 stage.

## Stage 3 delivered (2026-10-03)

Topic local rebuild — `src/v3core/derived_rebuild.py` (new, 1038 lines) plus the
`v3_update(action='rebuild')` entry (`tools/derived_rebuild_tool.py`). Only
`topic` is rebuildable this round (`REBUILDABLE_KINDS = (DERIVED_KIND_TOPIC,)`);
`observer_note` and `yin_paragraph` return a truthful `REBUILD_NOT_SUPPORTED`
rather than having their architecture rewritten, which is exactly what the task
book allows.

The generation context is assembled through the canonical correction state and
labels the two memories verbatim as `historical/superseded` and `current`
(`_A_STATE` / `_B_STATE`), with the correction relation and the instruction that
the superseded memory must not be presented as the current answer. A provider can
therefore never be asked to re-derive `current = A` from the old raw history.

Success produces a NEW artifact `T2` and advances the sidecar row in ONE own
transaction (autocommit ownership taken before the explicit `BEGIN`, as in
M01/M02):

```
INSERT INTO topics (...) VALUES (...) ON CONFLICT (topic_id) DO NOTHING   -- T2, idempotent
UPDATE derived_memory_invalidations
   SET state='rebuilt', resolved_at=NOW(), replacement_derived_id=T2
```

`T2`'s id is deterministic from `(T, correction_id)` —
`t_` + md5(`{topic_id}|rebuild|{correction_id}`)[:8] — so it can never collide
with `T` and a replay always lands on the same `T2`. `T` keeps its body verbatim,
is never deleted, and **stays suppressed** because suppression is driven by the
sidecar row's existence, never by `state` (§31). Provider unavailable or failing
leaves the sidecar row byte-identical, writes nothing and invents nothing.

Lineage is persisted on `T2` where the table can hold it: `note_ref =
'rebuild://<correction_id>'` plus a trailing `<!-- m03-rebuild-lineage: ... -->`
marker carrying the chain `T2 → rebuild_from_correction → correction_id → A → B →
qa_pairs:<id>`; the full dict is also returned in the receipt.

Parent-run verification, no guard downgrade:

```
pytest (all five M03 files)  -> 135 passed, 3 errors
```

The 3 errors are the conftest P0-A production-outbox teardown guard firing while
the real gateway on this shared host writes its own `j/journal_*` and
`accepted_live_buffer/*` during the test window — teardown-only, non-deterministic
(22 errors on one run of a superset, 2 on a re-run of the same files), and the
guard's own message documents that it cannot tell the two apart. The task book
§38 says not to drag M03 back into a reliability audit over environment fixture
reds, so this is recorded, not "fixed".

Open items added by this stage:

1. `derived_rebuild` resolves the default LLM provider itself
   (`config.resolve_config()` + `LLMClient`, no hardcoded endpoint or
   credential) rather than receiving it from the tool layer, because the frozen
   `rebuild_topic` signature had no config parameter. Database scope is still
   fail-closed at the tool layer via `memory_correction.resolve_scope`.
2. No live-PostgreSQL E2E for the rebuild either; it is proven against a
   deterministic in-memory fake plus an injectable stub provider (the task book
   explicitly permits a stub and says a paid model is not a merge blocker).
3. `observer_note` / `yin_paragraph` remain invalidate-and-suppress only, with
   rebuild recorded as pending/manual.
