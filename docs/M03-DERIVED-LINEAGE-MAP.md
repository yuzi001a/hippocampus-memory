# M03 derived lineage map

Read from the repository at `23865f74366e9c39e6860f9d1658ece77f224e33` (the M01/M02
merge commit on `main`). Every row below is backed by a real structure field and a
`file:line`. Nothing here is inferred from "the body mentions the same thing",
vector similarity, or an LLM judgement — those are explicitly out of scope for M03
and are recorded as unmapped instead.

Purpose: decide what M03 can invalidate **deterministically** after an explicit
correction `A -> B`, and to record honestly what it cannot.

---

## 1. Raw layers (not derived, never rewritten by M03)

| layer | canonical identity | key fields that matter for lineage | evidence |
|---|---|---|---|
| `qa_pairs` | `id` BIGSERIAL | `source_id` (UNIQUE, canonical idempotency key), `session_id`, `turn_id`, `timestamp` | `schema/alpha_bootstrap.sql:92-144` |
| `conversation_stream` | `id` BIGSERIAL | `host`, `session_id`, `event_id` + partial UNIQUE `(host, session_id, event_id)` | `schema/alpha_bootstrap.sql:165-202` |
| `explicit_memories` | `memory_id` TEXT | `provenance` JSONB (free-form), `status` CHECK `('active','archived')` | `schema/explicit_memories.sql:30-73` |
| `memory_relations` | `relation_id` TEXT = `cor_`+sha256(request) | `from_memory_id`, `to_memory_id`, `correction_id`, `correction_source_id` | `schema/memory_relations.sql:77-149` |

**The one raw join that is structural and exact** (this is what makes M03 possible):

```
conversation_stream.id
  -> (host, session_id, event_id)
  -> qa_pairs.source_id = 'qa_sync/<host>/<session_id>/<msg_id>'
  -> qa_pairs.id
```

Both sides are built from the **same `msg_id`** in the same loop:
`__init__.py:3700-3706` sets `msg_id = key` and feeds it to the live item
(`conversation_stream`, as `event_id`, with `_msg_host`) *and* to
`pending["q_msg_id"]` (`__init__.py:3765`), which `_qa_source_id()`
(`__init__.py:4039-4055`) turns into `qa_pairs.source_id`. Legacy rows without a
host use `qa_sync/<session_id>/<turn>/<msg_id[:16]>`; both forms are parseable.
Not yet validated against a live database — the Y400 disposable E2E is where that
happens.

---

## 2. Derived layers

| Derived type | canonical identity | current source / provenance field | can deterministically map to corrected source? | current recall entry |
|---|---|---|---|---|
| **Topic** | `topics.topic_id` TEXT (`'t_'+md5(title)[:8]`, `topic_store.py:232-233`) | `topic_entries.source_qa_id` BIGINT -> `qa_pairs.id`, written **only** by the observer's single-QA direct attach (`observer.py:2310-2330`) | **PARTIAL** — see §3.1 | `injector.py:397` first round; `__init__.py:2617-2624` query recall |
| **Observer note** | `observation_notes.id` BIGSERIAL (`version` is a chain label, not identity) | `source_qa_range` INT8RANGE over `qa_pairs.id` (`observer.py:4665-4669`) + `links` JSONB `kind=qa{first,last}` (`observer.py:4740`) | **YES, given a `qa_pairs.id`** | `observer.py:5308-5313` / `5397-5402` (chain head) |
| **Yin paragraph** | `yin_paragraphs.id` SERIAL; natural key `(yin_version, section)` (no UNIQUE) | **NONE** — the table has no source/provenance column at all (`schema/alpha_bootstrap.sql:339-347`) | **NO** | `__init__.py:3240` -> `pg_store.py:1108-1148` |

### 2.1 Topic — why only partial

`topic_entries.source_qa_id` is the only real topic-side edge into the raw layer,
and it is populated for **one** of four writer paths:

| writer path | `source_qa_id` | evidence |
|---|---|---|
| observer single-QA direct attach | set from the observer batch's `qa_pairs.id` | `observer.py:2310-2330` |
| observer `new_facts` | NULL | `observer.py:2662-2683` |
| `topic_store.add_entry` (cluster / refine / maintain / `topic_correct`) | NULL (only appended when truthy) | `topic_store.py:743-818`, `topic_store.py:590-592` |
| card path | n/a | `card_store.py:288-291` |

`topic_entries.message_id` is never written on the PG path at all
(`topic_store.py:791-803`, `observer.py:2300-2312`), so it is NULL and unusable as
an edge. The PG `topics` table has **no** `source_j_ids` / `metadata` column —
`card_store.py:526-538` says so explicitly and refuses to smuggle those values into
`note_ref` / `keywords` / `body`, so provenance that exists on the SQLite side is
dropped on the canonical PG side.

`topics.status` is the recall hot filter everywhere (`topics_status_active_idx`,
`alpha_bootstrap.sql:236-238`). Only two literals are ever written: `'active'`
(`topic_store.py:439/469/494`, `pg_store.py:666/696/713`) and `'dormant'`
(`topic_maintain.py:560`, after `DORMANT_DAYS=14` of inactivity). There is **no**
CHECK constraint on `topics.status` in the repo DDL despite the comment at
`alpha_bootstrap.sql:218-219` claiming one, so the whitelist is implicit.

### 2.2 Observer — the cleanest derived type

`source_qa_range` is a half-open INT8RANGE `[first, last+1)` built from the
batch's first/last `qa_pairs.id` (`observer.py:4665-4669`), so `range @> qa_id`
is a real containment test. Caveat to record: `first_qa_id`/`last_qa_id_now` are
the first/last rows of a batch **ordered by `timestamp ASC, id ASC`**
(`observer.py:4013`, `4189`), so the interval is a nominal span — it can be a
superset of the ids actually summarised, never a guaranteed exact set.

`links` is a JSONB array of `{"kind": ...}` objects. The cross-entity kinds are
`{"kind":"qa","first","last"}` (`observer.py:4740`) and
`{"kind":"topics","topic_ids":[...]}` (`observer.py:4742`, merged at `4874-4893`);
the rest are bookkeeping (`trigger`, `snapshot`, `event_time`, `oversize_atomic`,
`oversize_batch`, `seed`, `topic_updates_pending`) plus E1's own
`{"kind":"e1_compressed",...}` (`e1.py:1423-1435`).

### 2.3 Yin — no structural link exists

`<!-- premise: <id1>, <id2> -->` is real (`e1.py:653-660`) and machine-parseable
(`recall_pool.py:130`, `205-218`), but it is written **only** for sections whose
heading is in `SECTION_CATEGORY_MAP` *and* whose `category_source_ids` entry is
populated — and only the `topic` key is ever filled, with the literal
`f'topic/{title}'` (`e1.py:510`, `547`). The consumer matches
`topics.topic_id = ANY(premise_ids)` (`recall_pool.py:238-243`) against ids that
are `source_id`-derived (`pg_store.py:668`), i.e. a different id space, so the
match is expected to be empty. Separately, `e1.py:1126` computes the **real**
`topic_ids` by embedding match and passes them as `metadata` to
`insert_effective`, but the `yin_segment` branch discards `metadata` and writes no
column (`pg_store.py:1018-1059`). No test or fixture in the repo contains the
string `premise`.

---

## 3. The missing edge — `explicit_memories` has no path to anything

This is the central finding of the reconnaissance, and it is the reason M03 cannot
be a pure "scan the derived tables" job.

`explicit_memories` has no FK, no id column and no typed field referencing
`qa_pairs`, `conversation_stream`, `topics` or `observation_notes`
(`schema/explicit_memories.sql:30-73`). Its only cross-entity field is free-form
`provenance` JSONB, and:

- `v3_store(source_id=...)` is **not** a source link — `source_id` is a documented
  alias of `memory_id` (`active_memory_store.py:879-897`,
  `schema/explicit_memories.sql:32-35`).
- the tool path stores `provenance = {}` unless the caller supplies a whitelisted
  key (`source`, `source_j_ids`, `when`, `where`, `who`, `why`, `confidence`,
  `observation_count`) — `tools/store.py:83-102`.
- the **importer** path is the only in-repo writer that stores a source at all:
  `{kind, source_system, source_ref = "<path>::<session_id>", imported_at, ...}`
  (`importers/__init__.py:492-504`, `importers/hermes_sessions.py:15-27`) — that is
  session-level, not message-level.

Therefore, for a correction `A -> B`, the question *"which derived artifacts depend
on A?"* is **not answerable from current structure**. Per the M03 contract this
must not be resolved by text matching, vector similarity or an LLM guess; it is
`UNKNOWN_DEPENDENCY` and must be reported as unmapped.

The task explicitly allows a **minimal** reinforcement so that *future* writes can
be located (M03 task book §34), and forbids backfilling provenance for all
historical content.

---

## 4. Recall / injection entries and the minimal suppression points

| lane | path | minimal point where the derived id is stably available |
|---|---|---|
| Topic (first round) | `injector.py:397` -> `observer.recall_for_new_session` (`observer.py:5229`) -> `_recall_candidates` (`observer.py:1442`, SQL `1474-1476`) -> units built at `injector.py:522-535` | `injector.py:524` — the dict carries `topic_id` |
| Topic (query recall) | `__init__.py:2441` -> `recall_pool.recall_pool` (`recall_pool.py:1967`); topic hits keyed `source_id='topic_<topic_id>'` (`recall_pool.py:2713/2992/3277`) | `recall_pool.py:3594` — the single convergence point before RRF/rerank |
| Observer note | chain head `WHERE version LIKE 'v%' ORDER BY id DESC LIMIT 1` (`observer.py:5308-5313`, `5397-5402`) -> `injector.py:502-505` | `observer.py:5316` / `5405` |
| Yin paragraph | `__init__.py:2654` -> `__init__.py:3240` -> `search_effective(pool_role='yin_segment')` (`pg_store.py:1108-1148`), rendered `__init__.py:3243-3251` | `__init__.py:3240` (needs `id` added to the SELECT at `pg_store.py:1117` if paragraph-level suppression is wanted) |

Notes:
- The `recall_pool` yin lane (`recall_pool.py:3328-3379`) produces `kind='yin'`
  hits that are **discarded** at injection (`__init__.py:2625-2628`), so it is not
  a live injection path.
- `graph_recall.fetch_related_topics` is offline only (cut 2026-08-26,
  `prefetch.py:501-502`, `__init__.py:2784-2786`).
- `TopicRecall` caches a snapshot and only revalidates every 60s
  (`topic_recall_cache.py:48/209/232`, `topic_recall.py:290-305`), so an
  already-loaded snapshot can serve a just-invalidated topic for up to ~60s.
  This is a bounded staleness window that M03 must state, not hide.

---

## 5. What M03 will therefore treat as mappable

| derived kind | mappable when | granularity |
|---|---|---|
| Topic | the corrected memory resolves to a `qa_pairs.id` that appears in `topic_entries.source_qa_id` | topic (`topic_id`) |
| Observer note | the same `qa_pairs.id` is contained in `source_qa_range` | note (`id`), with the nominal-span caveat in §2.2 |
| Yin paragraph | never, from current structure | `UNKNOWN_DEPENDENCY` |

Everything else — including every historical derived artifact whose generating
path never persisted a source — is reported as `UNMAPPED_HISTORICAL_DERIVED` with
counts, and is explicitly **not** claimed as fixed.

---

## 6. Open items recorded, not resolved

- `eval/production_schema_subset.sql` shows `topics` with a surrogate `id bigint`
  PK plus `UNIQUE(topic_id)`, and `qa_pairs.turn_id` as text, diverging from
  `schema/alpha_bootstrap.sql`. That dump is a partial subset; the authoritative
  production shape was not inspected (production was not touched).
- The real production `topic_entries.source_qa_id` coverage fraction is not
  recorded anywhere in-repo; `tools/health.py:479-485` only computes it live.
  M03 will report the coverage it actually observes in the isolated E2E and will
  not generalise it to production.
- Whether any out-of-repo caller ever puts a real source id into
  `explicit_memories.provenance` is unverifiable from the repository.
