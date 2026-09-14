# Recall V2 Contract — G6A

**Status:** contracts only (G6A). Execution integration lands in G6B;
the evaluator lives in G6C. Legacy recall is **not** routed through
this module and remains the production hot path until G6B swaps it.

## Scope of G6A

G6A defines the **data contracts** for the next-generation recall
engine. It is a contract layer, not an execution layer: every type is a
plain stdlib dataclass with no I/O, no provider calls, no PG access.

**In scope:**

* `QueryContext`, `LanePlan`, `QueryPlan`, `CanonicalAlgorithmSnapshot`
  — immutable contracts that describe *what* to retrieve.
* `RecallCandidate`, `RecallTrace` — mutable runtime objects that
  describe *what was* retrieved and *how it scored / dropped*.
* `DropReasonCode`, `CandidateEventType` — fixed enumerations for
  structured drop reasons and candidate lifecycle events.

**Out of scope (G6B/G6C):**

* Actual retrieval against PG / vector store / topic radar.
* Rerank execution (calling `BAAI/bge-reranker-v2-m3`).
* MemoryInjector integration (seam belongs to G6B).
* Evaluator metrics and quality gates (G6C).

## Target flow

```
User query
   │
   ▼
QueryContext (frozen: query_id, normalized_query, deadline, budget, limit, max_chars)
   │
   ▼
build_default_query_plan(ctx) ─── QueryPlan (frozen: lanes, rerank, final_limit, max_chars)
   │                                       │
   │                                       └── CanonicalAlgorithmSnapshot (frozen)
   ▼
RecallTrace (mutable; bound to ctx + plan)
   │
   ├── start_lane / finish_lane / skip_lane   ── per-lane timing + status
   ├── record_candidate                       ── RecallCandidate (mutable)
   │       └── merge_provenance / record_score / mark_dropped / mark_selected / mark_injected
   ├── record_drop / record_event             ── structured drop + event log
   ├── select                                 ── final ranking
   └── inject                                 ── counted toward char budget
   │
   ▼
MemoryInjector (G6B)   ← consumes only ``final_selected_ids`` + ``injection_summary``
```

## Ownership boundary

G6A is **read-only**. The contract layer never:

* promotes, mirrors, or rewrites a memory item;
* writes to active-memory stores;
* touches user-owned explicit memory cards;
* shares a write handle with the legacy recall path.

Active explicit memories remain canonical user-owned data. Passive
recalled memories remain derived views. Recall V2 only retrieves and
describes; promotion / mirroring / shared writes are explicitly
out-of-scope.

The legacy `v3core.recall_pool` hot path is **not** routed through this
module. G6A is additive and unused by legacy execution. The two paths
co-exist until G6B cuts over.

## Contract rules

### QueryContext (frozen)

* `query_text` is preserved verbatim; `normalized_query` collapses
  whitespace only (never mutates semantics).
* `query_embedding` may be supplied; converted to an immutable tuple.
  The contract layer **never** computes an embedding.
* `deadline_monotonic` is an absolute `time.monotonic()` value.
* `budget_ms`, `limit`, `max_chars` must be non-negative.
* `remaining_budget_ms()` returns `min(time-to-deadline, budget_ms)`
  clamped at 0.0 — combines the absolute deadline with the configured
  ceiling.
* `has_budget_remaining(min_remaining_ms=1)` defaults to a strict
  positive check so an already-expired budget reads as exhausted.
* `to_dict()` **includes** `deadline_monotonic` (a `float` absolute
  `time.monotonic()` value) — the runtime deadline is part of the
  serialized context so downstream consumers can reproduce the same
  scheduling decisions.

### LanePlan / QueryPlan (frozen)

* Five canonical lanes: `keyword`, `vector`, `topic`, `qa`, `explicit`
  (order preserved by `ALL_LANES`).
* `build_default_query_plan` enables all five by default; final limit
  is read from the context (falls back to `DEFAULT_QUERY_LIMIT = 8`);
  max chars from the context (falls back to
  `DEFAULT_INJECTION_MAX_CHARS = 10000`); rerank limit is 30; QA
  candidate limit is `max(30, final_limit * 8)`.
* `to_dict()` is deterministic (canonical lane order, with
  `deadline_monotonic` exposed on the `QueryPlan` and on every
  nested `LanePlan`).

### CanonicalAlgorithmSnapshot (frozen, metadata only)

Algorithm parameters are stored as immutable data — G6A does not
execute them. Redlines preserved exactly:

| key                              | value                  |
|----------------------------------|------------------------|
| `rrf_k`                          | `60`                   |
| `half_life_days`                 | `30`                   |
| `lane_weights`                   | keyword=0.5, vector=1.0, topic=2.0, qa=1.0 |
| `rare_boost`                     | `0.08`                 |
| `exact_qa_boost`                 | `0.5`                  |
| `max_exact_qa`                   | `2`                    |
| `qa_vector_threshold`            | `0.35`                 |
| `message_effective_threshold`    | `0.3`                  |
| `topic_vector_threshold`         | `0.4`                  |
| `topic_recall_weight`            | `0.5`                  |
| `max_topics`                     | `3`                    |
| `max_subtopics`                  | `3`                    |
| `topic_context_max_chars`        | `3000`                 |
| `qa_frequency_candidate_multiplier` | `8`                 |
| `rerank_top_n`                   | `30`                   |
| `dual_prefetch`                  | `True`                 |
| `default_limit`                  | `8`                    |
| `injection_max_chars`            | `10000`                |
| `reranker_model`                 | `BAAI/bge-reranker-v2-m3` |
| `remote_rerank_min_remaining_ms` | `1500`                 |

## Provenance, score, and drop rules

### Provenance

* Construction creates one provenance record (`lane`, `source_type`,
  `source_id`, `ts`) and one `FOUND` event.
* `merge_provenance(lane, source_type, source_id)` is idempotent on the
  same triple; different triples are appended in insertion order.
* `contributing_lanes` returns lanes in first-seen order — preserves
  the merge history.

### Score stages

* Each `record_score(stage, value)` writes to the matching explicit
  field AND appends an immutable entry to `score_history`. Stages are
  never collapsed into one float.
* Stage → field map (canonical aliases):
  * `raw` → `raw_score`
  * `lane` / `vector` / `keyword` / `topic` / `qa` → `lane_score`
  * `fusion` / `rrf` → `fusion_score`
  * `temporal` / `half_life` → `temporal_score`
  * `rerank` → `rerank_score`
  * `final` → `final_score`

### Drop codes (exactly eight)

`DUPLICATE`, `BELOW_THRESHOLD`, `OUTSIDE_LIMIT`, `DEADLINE`,
`RERANK_PRUNED`, `CHAR_BUDGET`, `INVALID`, `LANE_ERROR`. Each drop
attaches a `DropReason(code, detail, stage)` and emits a `DROPPED`
event. Dropped candidates cannot be selected.

### Candidate event types (exactly eight)

`FOUND`, `DEDUPED`, `SCORED`, `FUSED`, `RERANKED`, `DROPPED`,
`SELECTED`, `INJECTED`. Append-only per candidate.

## Trace rules

### RecallTrace (mutable, single-use)

* One trace per recall request, bound to a `(QueryContext, QueryPlan)`.
* Per-lane execution summary: `lane`, `started`, `finished`,
  `duration_ms`, `candidate_count`, `timed_out`, `skipped`, `error`,
  `reason`.
* Aggregate `timed_out` property — True iff any lane finished
  `timed_out=True`.
* `record_candidate(c, capture_content=False)` is the default — the
  trace **does not** copy full raw memory unless the caller explicitly
  opts in. `content=None` on the stored `CandidateSnapshot`.
* `select(candidate_id)` requires the candidate to be on the trace and
  not dropped. `inject(candidate_id, char_count=...)` requires prior
  `select`.
* Concurrency-safe: an internal `RLock` guards mutations across lane
  executor threads.

### Safe serialization defaults

* `to_dict(include_content=False, include_query=False)` — both flags
  default OFF. `include_content` controls whether stored candidate
  content is exported (only available when `capture_content=True` was
  passed to `record_candidate`); `include_query` controls whether the
  `query_context` and `query_plan` blocks are emitted.
* `to_json(...)` uses `sort_keys=True` and `ensure_ascii=False` —
  byte-stable across calls, sequence order preserved (lanes in
  canonical order; candidates in insertion order).
* Privacy metadata caps: long string values truncated to 256 chars
  with a `<key>_truncated=True` marker; content-like keys
  (`content`, `body`, `text`, `raw`, `full_text`, `snippet`,
  `preview`) dropped by default from metadata snapshots.

The net effect: a default trace export cannot become a second memory
store. The trace describes retrieval; it does not retain raw memory
content.

## Performance expectations

These are targets the contract layer must accommodate; G6B is
responsible for actually meeting them in execution.

| metric                              | target                |
|-------------------------------------|-----------------------|
| Typical recall round-trip           | 1 – 2 s               |
| p95 round-trip                      | 3 – 4 s               |
| Normal hard maximum                 | < 6 s                 |
| Existing internal fuse              | 6.5 s                 |
| Hermes outer budget                 | 8 s                   |

Contracts that help hit these targets:

* `remaining_budget_ms()` lets every lane early-exit when the deadline
  is near.
* `qa_vector_threshold`, `topic_vector_threshold`,
  `message_effective_threshold` let lanes prune without I/O.
* `rerank_top_n = 30` bounds the rerank batch.
* `dual_prefetch = True` lets the contract layer describe a parallel
  prefetch; G6B turns it into actual concurrency.

## Module placement and separability

* Package: `v3core.recall_v2` — three new files: `__init__.py`,
  `contracts.py`, `trace.py`.
* **Not** imported from `v3core/__init__.py` — the module remains
  separable. `from v3core import recall_v2` is not implicit.
* Legacy `v3core.recall_pool` is unchanged. `v3core.recall_pool` and
  `v3core.types.RecallHit` keep their existing public surface.
* No file outside `src/v3-core/src/v3core/recall_v2/`,
  `src/v3-core/tests/test_recall_v2_contracts.py`, and this doc was
  modified by G6A.

## Versioning and roll-out

* G6A: this contract layer (additive, unused).
* G6B: integrates `MemoryInjector` with `RecallTrace.final_selected_ids`
  and `InjectionSummary`; routes user-facing recall through the new
  contracts; falls back to legacy `recall_pool` on any
  contract-layer panic.
* G6C: evaluator — measures quality, latency, and privacy metrics
  against the new trace format.

Until G6B lands, G6A is dormant: nothing imports `v3core.recall_v2`
except tests. Legacy recall continues to serve production traffic
unchanged.
