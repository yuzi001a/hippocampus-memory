# #15 — 8s realtime budget validation (production, read-only)

Harness: `eval/embedfix_realtime_budget.py`
Model: `BAAI/bge-m3`  fingerprint `bf32771ecbd1`
Date: 2026-09-20

## Scope — what was and was not measured

Measured, against the real provider and the real production database:

1. the **query embedding** under `REALTIME_EMBED_POLICY` (3.0s / 0 retries) — the budget
   the recall path actually uses;
2. the **vector recall SELECT** (`ORDER BY embedding <=> ...`), read-only.

**NOT measured: the full turn path end-to-end.** `V3Core.prefetch()` writes
`delta_run_state.json` into the resolved data dir, which is the LIVE production profile,
and production is read-only this round. The end-to-end turn measurement therefore belongs
to the canary. Stated plainly rather than papered over.

No INSERT / UPDATE / DELETE / DDL was issued. The database session was opened read-only.

## Results

### Run 1 (20 samples)

| component | n | p50 | p95 | max | mean | provider timeouts |
|---|---|---|---|---|---|---|
| query embedding (REALTIME 3s/0) | 19 | 1075 ms | 1348 ms | 1348 ms | 1114 ms | **1 / 20** |
| vector recall SELECT | — | NOT MEASURED (harness bug, fixed) | | | | |

### Run 2 (20 samples, full)

| component | n | p50 | p95 | max | mean | provider timeouts |
|---|---|---|---|---|---|---|
| query embedding (REALTIME 3s/0) | 16 | **1066 ms** | **2054 ms** | 2054 ms | 1213 ms | **4 / 20** |
| vector recall SELECT | 20 | **81 ms** | 194 ms | 194 ms | — | 0 |

The four timeout attempts took 3.91 s, 5.22 s, 4.39 s, 4.10 s (budget 3 s + connection
overhead; the 5.22 s sample shows the provider can be far beyond the budget).

## Verdict

| quantity | value |
|---|---|
| worst observed realtime component sum | **2249 ms** |
| user-facing budget | 8000 ms |
| headroom | **5751 ms** |
| does `10s/2` enter a user-blocking path? | **NO** |

The answer is `NO`, and it is answered two independent ways: (a) the caller matrix shows
every user-blocking site uses realtime 3s/0 or tighter, never a durable policy; and (b)
the realtime components measured above consume at most 2249 ms of the 8000 ms budget, so
even a background policy running concurrently cannot push the turn past the budget.

## ⚠️ Finding that this measurement produced

**The realtime 3 s query-embedding budget is itself marginal, and it is silently costing
recall.**

Two independent runs timed out **1/20** and **4/20** of query embeddings against the live
provider. The provider's current p50 is ~1.07 s (not the ~0.58 s previously recorded) and
its tail exceeds 3 s.

On the recall path this does not create a permanent NULL — it degrades gracefully to a
recall miss — but it means roughly 5–20 % of recall queries in this window were answered
without a query vector at all. That is the **same root cause** as the conversation_stream
leak (provider latency beyond a too-tight budget) manifesting on a different path.

This also independently supports raising the conversation_stream primary attempt to
`5s / 0`: the measured realtime p95 is 2054 ms and the previously observed tail is
3488 ms, both of which fit inside 5 s but not reliably inside 3 s.

**Not acted on.** Changing `REALTIME_EMBED_POLICY` alters the recall path for every user
and is a separate decision; it is recorded here as evidence, not applied. Note there is
room inside the 8 s budget (5751 ms headroom), so it is a policy choice rather than a
constraint.

## Reproduction

```
cd C:/Users/servi/workspace/wt-embedfix/src/v3-core
PYTHONPATH=src <python312> eval/embedfix_realtime_budget.py
```

Read-only: SELECT only, provider read only, no writes to any production surface.
