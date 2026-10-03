# M03 derived correction propagation — evidence index

## Files

| File | Status | Meaning |
|---|---|---|
| `m03_e2e.py` | driver (139,556 B) | The 136-check Y400 real-PostgreSQL E2E driver. Unchanged from the run it produced. |
| `m03-report-20261004_001402.json` | **PRE-FIX FAILED RUN** | First real-PG run of shipped M03 code (commit `63b8596`). **Do not read this as a pass.** |

## PRE-FIX FAILED RUN — headline (do not rewrite)

```text
verdict            = FAIL
checks             = 136
pass               = 134
fail               = 2
not_evaluated      = 1 coverage gap (/prefetch public surface, offline host)
production_mutation = NONE
host               = DESKTOP-EQP3OBU (Y400)
```

Failed checks (both on the injected-`PgPool` path):

```text
d1_absent_from_current_recall_after_correction_pgpool_injected
s31_t1_absent_from_current_recall_after_rebuild_pgpool_injected
```

## The three P0 defects this run found (all invisible to in-memory fakes)

| # | Defect | Mechanism | Consequence |
|---|---|---|---|
| A | `derived_invalidation._discover`: `int8range @> %s` | psycopg2 adapts a Python int as `int4`; `int8range @> integer` does not exist | real PG `UndefinedFunction` → propagation never wrote a single invalidation row |
| B | `_acquire_lease(None, pg)` at `derived_invalidation` ×2 and `derived_rebuild` ×1 | `_acquire_lease(pool, pg)` is pool-slot-first; a real `PgPool` in the `pg` slot takes `_PgStoreLeaseAdapter`, whose `.connection` is a `PgLease`, not a DBAPI connection | sidecar read/write fails → suppression degrades and **fails OPEN** |
| C | invalidation `INSERT` listed 12 target columns but 15 VALUES expressions | `NOW(), NULL, NULL` were appended to 12 `%s` | real PG `INSERT has more expressions than target columns` — the shipped writer had never succeeded once |

## Status of this directory

- The failed run above is **preserved as-is**; it is the evidence that M03 as shipped at `63b8596` was broken on real PostgreSQL.
- Fixes for A/B/C live in the follow-up commit on `feature/m03-derived-correction-propagation` (`fix(m03): repair real-pg invalidation writes and lease routing`), with the CI-visible static assertions in `src/v3-core/tests/test_m03_sql_arity_and_lease_routing.py`.
- The post-fix Y400 revalidation produces a **new** run directory and a **new** report file. It is never written over this one.
