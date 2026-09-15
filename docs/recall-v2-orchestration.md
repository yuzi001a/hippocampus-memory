# Recall V2 Orchestration — G6B

G6B is the orchestration slice that wires the G6A Recall V2 contracts
into the legacy single-executor recall path. It is a wiring change,
not an algorithm change: every retrieval call still goes through one
function and returns the same `RecallHit` shape callers already
expect. G6B adds a typed `RecallTrace`, a thin orchestrator, and an
additive probe surface; it does not rewrite scoring, fusion, rerank,
or SQL.

## 1. Purpose and scope

In scope:

* Build `QueryContext` and `QueryPlan` before any retrieval work.
* Construct a typed `RecallTrace` and a duck-typed sink that adapts
  legacy `getattr(trace, ...)` hooks to that trace.
* Invoke the legacy retrieval function **exactly once** with an
  optional `trace=` keyword argument.
* Convert each returned `RecallHit` to a `RecallCandidate` and bundle
  the outcome into a `RecallV2Result`.
* Add a strictly additive, guarded probe surface so legacy code can
  emit lane/candidate/score/drop/select/inject events without
  importing `recall_v2`.

Explicitly out of scope: any change to scoring weights, RRF `k`,
fusion order, temporal decay, SQL, vector-store layout, embedding,
reranker selection, PG lease behaviour; the G6C evaluator; production
rollout.

## 2. Runtime flow

Target flow:

```
Query -> QueryContext -> QueryPlan -> lanes
  -> candidate pool -> fusion (RRF) -> rerank (optional)
  -> selection -> injection
```

Mapping onto the existing single-executor reality:

| Target stage    | Where it runs today                                  |
|-----------------|------------------------------------------------------|
| `QueryContext`  | `adapters.build_query_context` before any SQL        |
| `QueryPlan`     | `contracts.build_default_query_plan`                 |
| Lane work       | The five lane blocks inside `recall_pool.recall_pool`|
| Candidate pool  | `RecallHit` list returned by `recall_pool`           |
| Fusion          | Existing RRF path inside `recall_pool`               |
| Rerank          | Existing `rerank_top_n` branch inside `recall_pool`  |
| Selection       | Existing `OUTSIDE_LIMIT` pruning + `final_limit` cap |
| Injection       | `prefetch.prefetch_to_context_block` (char budget)   |

There is still one retrieval executor: `recall_pool.recall_pool`. The
lanes are not parallelized in G6B; they are the same five blocks that
already exist, only now they emit structured events through the probe.

## 3. Entry points and compatibility

production facade — the user-facing entry points execute through the V2
orchestration. V2 is no longer an "alternate API":

```
MemoryInjector.build_context
  -> MemoryInjector._get_recall
    -> core.prefetch_to_context_block
      -> prefetch.prefetch                    <- builds QueryContext / QueryPlan / RecallTrace via the engine
        -> RecallV2Engine.recall              <- orchestration; exactly one downstream call
          -> recall_pool.recall_pool          <- the only retrieval executor
```

`prefetch(fmt="list")` is the cutover seam: it forwards the same
effective arguments it always did (so pre-G6B and post-G6B semantics
match) and still returns `[hit.to_dict() for hit in hits]`.
`prefetch(fmt="chain")` is intentionally untouched and still calls
`chain_recall` directly. The engine never calls `prefetch`, so there is
no recursion.

Invariants:

* **Exactly-once; no post-I/O retry.** The legacy function is invoked
  exactly once per request. BEFORE invoking, the engine validates the
  payload with `inspect.signature(...).bind_partial(...)` and drops only
  OPTIONAL unsupported keywords (`trace`, `max_chars`). If the signature
  cannot be inspected, both optional keywords are dropped and
  `trace_attached=False` is reported — correctness beats tracing. Once
  the callable body has started, every exception (including
  `TypeError`) propagates unchanged: the engine never inspects an
  exception message to decide whether to call again.
  `POST_IO_FULL_RETRY = 0`.
* **Forwarded-keyword contract.** Every legacy keyword keeps the
  lenient rule: it is forwarded when the callable declares it or has
  `**kwargs`. `max_chars` is the one exception — it belongs to
  `prefetch_to_context_block`, not to `recall_pool`, so it is forwarded
  only when the callable's signature declares `max_chars` explicitly,
  and it is stripped when the signature cannot be inspected. This keeps
  a thin `def wrapper(*args, **kwargs)` around `recall_pool` working.
* **Additive probe contract.** `recall_pool` exposes a module-level
  `_probe(trace, method, *args, **kwargs)` helper. When `trace is
  None` the helper returns immediately and the legacy code path is
  byte-identical to before. The prefetch-side helper resolves BOTH
  protocol styles: the sink-style call (`drop` / `inject`) stays primary,
  and for a bare `RecallTrace` (which has no `drop` method) it falls back
  to the trace-style call (`record_drop` with a `DropReasonCode`, or
  `record_candidate_event`). Without that fallback the injection-stage
  evidence was silently discarded. `recall_v2` is never imported from
  `recall_pool`.
* **One trace per request.** `prefetch` accepts a keyword-only
  `trace_out` list and publishes the trace it built there;
  `prefetch_to_context_block` passes its own holder and emits the
  injection-stage evidence (`inject`, `drop CHAR_BUDGET`) against that
  SAME instance, so a request has exactly one trace carrying both
  retrieval and injection evidence. Traces are never persisted.
* **Budget accounting.** Every SELECTED candidate omitted solely because
  of `max_chars` is recorded as `CHAR_BUDGET` on that same trace, so
  `selected == injected + char_budget_dropped` holds whenever the greedy
  budget loop runs. The rendered context string is unchanged by tracing.
* **Public API compatibility.** `recall_pool.recall_pool` gained one
  keyword argument (`trace=None`). `prefetch.prefetch` gained optional
  keyword-only `max_chars=None` and `trace_out=None`. Existing
  callers continue to work unchanged.

## 4. Lane model and the legacy-path mapping

Five canonical lanes (G6A `contracts.ALL_LANES`): `keyword`, `vector`,
`topic`, `qa`, `explicit`. Real legacy blocks feed each lane:

| Lane        | Legacy block(s) that feed it                                                      |
|-------------|-----------------------------------------------------------------------------------|
| `keyword`   | The keyword block in `recall_pool`                                                |
| `vector`    | card-vector block, message-vector block, effective block, yin (`yin_paragraphs`), observation_notes |
| `topic`     | The hierarchical topic path                                                        |
| `qa`        | The QA keyword/snapshot/scoring sub-block plus QA-vector hits                     |
| `explicit`  | The two active-memory seams (`ActiveMemoryReader.search_keyword` / `search_vector`) |

Yin and notes are mapped onto the vector family because both are
embedding-indexed paragraphs / observation records; routing them under
a new lane would split the vector family without changing retrieval
semantics. Active memory maps onto `explicit` because the active-memory
store is the canonical explicit-memory seam. Legacy `kind` values
observed against a disposable PostgreSQL include `qa`, `topic`, `yin`,
`active_memory`, and (in other configs) `card`, `message`, `note`.
The default `lane_for_kind` fallback is `vector`.

**Engine default vs production-caller shape.** `RecallV2Engine.recall`
forwards only the kwargs it is given; unknown `include_*` flags
travel through `**passthrough`. Raw engine callers that pass no flags get
`recall_pool`'s own defaults (`include_keyword=True`,
`include_card_vector=False`, `include_message_vector=False`,
`include_effective=False`, `include_topic=True`,
`include_yin=True`, `include_notes=True`).

The production facade never relies on those defaults: `prefetch` passes
`include_keyword` / `include_card_vector` as dual (both `True` by
default) and `include_message_vector` from config, and the effective
`QueryPlan` is derived from exactly those flags via
`contracts.build_effective_query_plan`, so a facade request cannot
diverge from pre-G6B semantics. (A direct `RecallV2Engine` call without
flags can still differ — earlier measurement: 3 hits with engine
defaults vs 4 with the production shape. That caveat applies to raw
engine callers only.)

**Effective plan lane rules.** `build_effective_query_plan` maps the real
legacy execution shape onto the five canonical lanes without renaming or
inventing lanes:

| Lane       | Enabled when                                                        |
|------------|---------------------------------------------------------------------|
| `keyword`  | `include_keyword`                                                    |
| `qa`       | `include_keyword` (QA keyword/snapshot and QA-vector both live inside the keyword block) |
| `topic`    | `include_topic`                                                      |
| `vector`   | any of `include_card_vector`, `include_message_vector`, `include_effective`, `include_yin`, `include_notes` |
| `explicit` | `include_keyword` or `include_card_vector` (the two active-memory seams live in those blocks) |

A disabled lane carries a truthful `reason` (for example
`include_keyword=False`), so the plan cannot contradict the trace.

## 5. Contracts in play

| Contract         | Responsibility                                                              |
|------------------|------------------------------------------------------------------------------|
| `QueryContext`   | Frozen, per-request: `query_id`, query text, deadline (informational monotonic clock value), `budget_ms`, `limit`, `max_chars`, session/conversation/profile/agent ids, optional embedding |
| `QueryPlan`      | Frozen, derived from `QueryContext`: five `LanePlan`s, rerank config, final limit, max chars |
| `RecallCandidate`| Mutable: identity (`source_id`, `source_type`), primary lane, content (off by default), score fields, score history, drop events, provenance, selection/injection flags |
| `RecallTrace`    | Mutable, single-use: lane summaries, candidate snapshots, drop log, warnings/errors, selection, injection; bound to one `(QueryContext, QueryPlan)` |

Sink protocol (duck-typed, never imports `recall_v2`): legacy code
calls these methods on `trace` via `getattr`. `LegacySink` implements
every one and translates it to the typed trace API:

```
lane_start(lane)
lane_finish(lane, candidate_count=, skipped=, timed_out=, error=, reason=)
lane_candidates(lane, source_ids, source_type=, scores=)
score(candidate_id, stage, value, operation=, **params)
event(candidate_id, event_type, note=)
drop(candidate_id, code, detail=, stage=)
select(candidate_id); inject(candidate_id, char_count=)
warn(text); error(text)
```

Unknown lanes, stages, event types, and drop codes are ignored
silently. The sink never raises into the caller.

## 6. Trace semantics

Per-lane lifecycle fields (`LaneSummary`): `lane`, `started`,
`finished`, `duration_ms`, `candidate_count`, `timed_out`,
`skipped`, `error`, `reason`. Measured on a disposable PG:
keyword 47 ms / 1 hit, vector 157 ms / 1, topic 141 ms / 1,
qa 47 ms / 1, explicit 31-47 ms / 1, every lane ending with
`finished` populated.

Provenance: `merge_provenance(lane, source_type, source_id)` is
idempotent on the same triple; `contributing_lanes` returns lanes in
first-seen order. Observed multi-lane snapshots include
`topic_t_fix_1` -> `[keyword, topic, vector]` with three provenance
records and `mem_fix_old` -> `[explicit, vector]`; zero snapshots
with empty provenance.

Score stages (canonical aliases):

| Stage name accepted                              | Field written    |
|--------------------------------------------------|------------------|
| `raw`                                            | `raw_score`      |
| `lane` / `vector` / `keyword` / `topic` / `qa`   | `lane_score`     |
| `fusion` / `rrf`                                 | `fusion_score`   |
| `temporal` / `half_life`                         | `temporal_score` |
| `rerank`                                         | `rerank_score`   |
| `final`                                          | `final_score`    |

Fusion is recorded with `operation="rrf"`. Selection emits
`SELECTED` and `OUTSIDE_LIMIT` drops. A skipped rerank records the
real reason (e.g. `"rerank skipped: no_rerank_cfg"`). Each
`record_score` call also appends to the candidate's immutable
`score_history`; stages are never collapsed into a single float.

The trace **observes execution rather than re-running it.** No retry,
no parallel retrieval, no I/O of its own. The legacy function is the
only executor; the sink records what the legacy function emits.

## 7. Privacy rules

* **Default export excludes query and content.**
  `RecallTrace.to_json()` defaults to `include_query=False` and
  `include_content=False`. The query text and stored candidate
  content require an explicit opt-in at record time
  (`record_candidate(..., capture_content=True)`) and an explicit
  opt-in at serialization.
* **Recursive metadata sanitisation.** Content-like metadata keys
  (`content`, `body`, `text`, `raw`, `full_text`, `snippet`,
  `preview`) are stripped by default from metadata snapshots at
  every nesting depth. Long string values are truncated to 256
  chars with a `<key>_truncated=True` marker.
* **No persistence.** The trace is never persisted automatically:
  no database table, no file, no new memory store. The trace
  describes retrieval; it is not a second copy of memory content.

## 8. Deadline, budget and failure model

The engine never constructs or extends a deadline. It passes the
caller's original deadline object through unchanged, and passes
`None` when the caller passed `None`. A `PrefetchDeadline` whose
`is_set()` returns `False` is treated as no deadline. When no
deadline is supplied, `QueryContext.deadline_monotonic` carries an
informational value derived from
`_deadline.INTERNAL_PREFETCH_BUDGET_SECONDS`.

`deadline_monotonic` is **process-local monotonic-clock state**
(`time.monotonic()`). It is used for in-process budget checks and
is NOT a portable wall-clock value across process restart.

Budgets and fuses (unchanged):

| Boundary                    | Value       |
|-----------------------------|-------------|
| Typical recall round-trip   | 1 - 2 s     |
| p95 round-trip              | 3 - 4 s     |
| Normal hard maximum         | < 6 s       |
| Existing internal fuse      | 6.5 s       |
| Hermes outer budget         | 8 s         |
| Remote-rerank cutoff        | 1.5 s       |

Failure model:

* **Pre-execution fallback only.** If `QueryContext`, `QueryPlan`,
  `RecallTrace`, or `LegacySink` construction raises, the engine
  calls the legacy function exactly once with no `trace=` kwarg and
  reports `fallback_used=True` with a non-empty `fallback_reason`.
  This is the only fallback path.
* **No mid-flight retry.** Once retrieval has started there is no
  retry. A raised `PrefetchDeadlineExceeded` propagates unchanged.
* **Per-lane degradation.** Each lane catches non-deadline
  exceptions, logs, and yields an empty candidate set — exactly as
  legacy.

## 9. Ownership boundary

Recall V2 unifies how memories are **found**. It does not unify who
owns them.

* Explicit memories stay canonical user-owned records. Active-memory
  rows are read by the active-memory seams; the engine never writes
  to them.
* Passive memory stays derived. Yin paragraphs, observation notes,
  topic cards, and QA rows are retrieved views over underlying
  user-owned or system-derived state; the engine never rewrites
  them.
* **No promotion.** Recall results do not become explicit memories.
* **No mirroring.** Recall traces do not become a parallel memory
  store.
* **No shared writes.** `recall_v2` and `recall_pool` do not share
  a write handle to any memory table.

## 10. Known limitations and preserved legacy behaviours

G6B preserves the following legacy behaviours deliberately; they are
not bugs being fixed by this slice.

a. **Temporal decay is effectively inert with current data shapes.**
   `qa` hits are excluded by a kind guard. `topic`, `yin`, and
   `note` hits carry `created_at=''`. Active-memory timestamps are
   offset-aware while the legacy code subtracts them from a naive
   `datetime.now()`, raising a `TypeError` that is swallowed.
   Changing `time_decay.half_life_days` from 30 to 3650 changes no
   score. A temporal stage is therefore recorded only when a decay
   was actually applied.

b. **Recall ordering is deterministic only within a process.**
   Tie-breaking and the max-2 exact-QA boost target depend on set
   iteration order, which follows `PYTHONHASHSEED`. Cross-process
   comparisons must pin `PYTHONHASHSEED`.

c. **`config=None` is not None-safe on the legacy keyword/vector
   paths.** It raises `'NoneType' object has no attribute 'get'`,
   which is swallowed. Production always passes a config dict; G6B
   did not change this.

d. **The topic SQL path is nested inside `if include_keyword:`.**
   This is a real structural fact: the topic lane is only reachable
   when `include_keyword=True`. The mapping is preserved as-is.

## 11. Verification

Verification gates used to ship this slice:

| Gate                            | What it covers                                          |
|---------------------------------|---------------------------------------------------------|
| Unit tests                      | Engine, sink, adapter, trace, contracts                 |
| Contract tests                  | Lane-summary / score-stage / drop-code / event-type enums |
| Differential parity (legacy vs branch) | 13 scenarios, fixed `PYTHONHASHSEED`          |
| Opt-in disposable PG            | Gated by `HIPPOCAMPUS_G6B_TEST_DSN` (loopback disposable DSN); skips on non-loopback hosts and on port 5433 by design |
| Packaging                       | `compileall` clean; wheel contains every `v3core/recall_v2/*` module |

Parity measured: full unit suite 402 passed / 1 skipped with no test
DSN; the opt-in PG test reports 7 passed when the disposable DSN is
configured. The 13-scenario differential is byte-identical against a
fixed `PYTHONHASHSEED`, with 11 of 13 cases non-empty. Identical DB
lease counts (8) were measured for legacy vs V2 on the same query,
confirming no duplicate I/O. Cross-process comparisons must pin
`PYTHONHASHSEED` (see §10b).

## 12. G6A / G6B / G6C split

| Slice | Scope                                                          | Status     |
|-------|----------------------------------------------------------------|------------|
| G6A   | Contracts: `QueryContext`, `QueryPlan`, `RecallCandidate`, `RecallTrace`, drop/event enums, snapshot fields | Landed (additive, unused by legacy) |
| G6B   | Orchestration: `RecallV2Engine`, `LegacySink`, `adapters`, additive probe surface, exactly-once invocation, fallback semantics | This slice |
| G6C   | Evaluator: quality / latency / privacy metrics against the new trace format | Out of scope here |

G6B depends only on the G6A contract layer; it does not import or
re-implement anything from G6C. G6C will consume the trace produced
by G6B but lives in a separate slice.
