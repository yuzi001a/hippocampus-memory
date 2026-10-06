# P0-C2 — Autonomous / Async Delivery Durability (design contract)

Status: parent-reviewed P0-C2 candidate on `fix/p0c2-autonomous-memory`; focused regressions and
Y400 disposable-PG verification pass. Base: `5b3a71c25ff2e23446d1cf887ed9347d1f69c50f`.
P0-C1 owns host-event *classification*; P0-C2 owns the *durability* of autonomous output.
P0-B is not started.

## 1. Problem

P0-C1 classifies a host boundary (`ASYNC_BOUNDARY` / `NEW_ROOT`) and **cuts the QA chain**,
so an autonomous assistant output can no longer pollute the previous human QA. But the
output then had **no derived container at all**: it was source-only, and the boundary row
itself was consumed as control flow without ever being written to `conversation_stream`.

User-level consequence: an async/cron/skill result the Agent actually produced could not be
recalled, and its trigger boundary was not durable.

## 2. Why an existing container is insufficient

Audited derived containers:

| container | why it cannot hold an autonomous delivery |
|---|---|
| `qa_pairs` | `question` is `NOT NULL` and **user-anchored** (`source_id` derives from the human U1 event); downstream QA lineage (`merged_event_ids`, observer, recall lanes) assumes a human question. Representing a delivery would require a synthetic `Q` (`[system]` / `[cron]` / `[assistant_unpaired]`) — explicitly forbidden by the task book §C2.1. |
| `conversation_stream` | **source truth**, not derived. It is the input, not a recall unit. |
| `topic_entries` / `topics` | observer-derived topic cards; keyed off QA rows, not autonomous output. |
| `observation_notes` | observer rolling印; no per-delivery identity. |

So one **additive sidecar** is justified: `public.assistant_deliveries`.

## 3. Source truth (unchanged invariant)

`conversation_stream` remains authoritative. A delivery is **derived and rebuildable**:

```
boundary row (host_event:<KIND>)   ← durable trigger, written with a distinct role
  → contiguous non-empty NORMAL_ASSISTANT rows
  → next real boundary (boundary | REAL_USER)
```

`event_ids` (JSONB, ordered) are the assistant source event ids; each resolves to its
`conversation_stream` row through the F2 composite identity `(host, session_id, event_id)`.
Raw source is never lost because a derivation failed: `write_delivery` is best-effort and
`rebuild_deliveries` reconstructs the row from source at any time.

## 4. Identity (deterministic, content-independent)

```text
source_id = "delivery/<percent-encoded-host>/<percent-encoded-session_id>/"
            "<percent-encoded-trigger_event_id>"
```

The components are percent-encoded so `/` inside an F2 identity cannot collide with a path
separator. The database has unique indexes on both `source_id` and
`(host, session_id, trigger_event_id)`. Live replays treat an equal event-id list as a no-op
and append only segments beyond the already persisted event-id prefix. A shortened/reordered
snapshot cannot shrink or rewrite prior source text; an authoritative rebuild reads
`conversation_stream` and can replace the derived row with its exact canonical contents.

## 5. Grouping rules (C2.3 / C2.6)

* a boundary opens a group;
* non-empty `NORMAL_ASSISTANT` rows append **in source order**;
* empty assistant, `NORMAL_TOOL`, `CONTROL`, `CONTINUATION`, `TERMINAL` are transparent
  (they neither end the group nor add content);
* a `REAL_USER` row or the next boundary closes the group;
* a group with no assistant members is discarded;
* **not** included: `CONTINUATION` (compaction/tasklist/still-in-progress), `CONTROL`,
  compaction, terminal notices.

Trigger kinds handled: `ASYNC_BOUNDARY` (textual `[ASYNC DELEGATION…]` /
`[IMPORTANT: Background process…]`, **and metadata-only** `display_kind`
`async_delegation_complete` / `process_complete`) and `NEW_ROOT` (cron `[IMPORTANT: You are
running as a scheduled cron job…]`, skill invocation, legacy `session_source=cron`).

## 6. Live integration (`sync_turn`)

1. Classification-first structure tracking now also rebuilds delivery groups for **every**
   row of the snapshot (already-seen included). The delta cursor still gates *source
   idempotency only*, so replay/restart re-derives identical groups.
2. The boundary row is enqueued to `LiveBuffer` with role `host_event:<KIND>` (never
   user/assistant → can never open a QA). This makes the trigger durable and traceable.
3. After the loop, each group is upserted only if all of its source enqueue operations were
   accepted or already durable. A rejected source event suppresses the derivative write; a
   sidecar write failure is logged and non-blocking, and a later full-snapshot sync retries.
   Existing event-ID prefixes are immutable in the live path: only new segment text is
   appended, so a changed/shorter replay cannot overwrite the authoritative source body.

Embedding is derived/optional: `assistant_deliveries.embedding` is left NULL on the live
path and can be backfilled asynchronously. **No provider ⇒ source and delivery are still
durable.**

## 7. Recall integration

`recall_pool.recall_pool` now queries delivery content through the existing lexical QA path
(using bounded `content ILIKE` candidate matching), retains `kind="assistant_delivery"` and
the stable `source_id`, then places it in the existing QA lane for the unchanged five-lane
`QueryPlan`. `recall_v2.adapters.lane_for_kind("assistant_delivery") == "qa"`; there is no
sixth lane and no synthetic question. The current path is keyword-only: the delivery
embedding remains optional/NULL and does not require an embedding provider. The
`assistant_delivery.delivery_recall_hits()` accessor exposes candidates with the complete
source trace (`host` / `session_id` / `trigger_event_id` / `event_ids`).

## 8. Historical repair — READ-ONLY dry-run

`assistant_delivery.plan_delivery_repair(pg, session_id, host)` returns a deterministic,
read-only plan scoped by the F2 `host/session_id` identity. It uses only `qa_pairs.source_id`
when that ID proves the same host/session (e.g. `qa_sync/<host>/<session>/<event>`); rows
from a provably different host are ignored. Rows with unscoped source IDs are never
proposed for repair: if their event ledger or exact answer text overlaps, they are reported
as `unknown`.

Contamination detection is **identity-based** (`merged_event_ids` ∩ autonomous event ids) —
never semantic similarity, never LLM guessing. The plan returns IDs and repair proposals,
not raw question/answer text. Unprovable provenance is `unknown` and never auto-repaired.
Production mutation = NONE.

## 9. Verification and remaining limits

* Y400 disposable-PG verification is complete on `g6b_p0c2e2e_20261006_ad230daa`, created as
  a separate clone of the P0-C1 disposable baseline. The additive upgrade + idempotent
  reapply succeeded, post-upgrade dry-run returned clean, and the two real-PG tests passed:
  source → derived delivery → existing QA recall lane/F2 trace, plus a read-only historical
  repair fixture. Test-created `conversation_stream`, `qa_pairs`, and `assistant_deliveries`
  rows all read back as zero after cleanup. The target was loopback-only, port 55432, and
  `V3_HARNESS_ALLOW_LIVE_PROD_OUTBOX` was not enabled.
* The repair planner has no CLI entry point and no apply mode; it is a read-only module
  function. Historical mutation remains deliberately outside this C2 slice.
* The live path recomputes groups from each full snapshot and reconciles them with a row-locked
  append by event ID; per-session memoization is not implemented or performance-benchmarked.
* Backup behavior was not changed or verified in this candidate. Deliveries are derived from
  `conversation_stream`; backup coverage of that source must be verified independently before
  claiming restart/restore recovery.

## 10. Files

* **new** `src/v3-core/src/v3core/assistant_delivery.py` — grouping, identity, DB helpers,
  rebuild, recall candidates, repair planner.
* **new** `src/v3-core/schema/assistant_deliveries.sql` (+ packaged mirror).
* **edit** `host_events.py` — metadata-only async display kinds → `ASYNC_BOUNDARY`.
* **edit** `__init__.py` — boundary source durability + delivery derivation in `sync_turn`.
* **edit** `recall_v2/adapters.py` — `assistant_delivery` → `qa` lane.
* **edit** `alpha_bootstrap.sql` / `upgrade_v0_2.sql` (+ packaged mirrors) — include marker.
* **edit** `pyproject.toml`, `distribution_cli.py`, and `doctor_full.py` — package-data and
  both packaged/full-doctor upgrade/schema checks.
* **edit** `tests/test_p0c1_host_event_semantics.py` — P0-C2 live-grouping, idempotence,
  source-trace, recall-adapter and read-only repair tests (fake PG).
* **edit** `tests/test_recall_v2_pg_integration.py` — opt-in disposable-PG source → delivery →
  existing recall lane + F2 trace test.
* **edit** `tests/test_alpha_bootstrap_contract.py` and
  `tests/test_production_upgrade_contract.py` — bootstrap/package and upgrade-shape pins.
* **edit** `tests/test_doctor_full_contract.py` — full doctor recognizes the new required table.
