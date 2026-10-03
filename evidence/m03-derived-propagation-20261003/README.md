# M03 derived correction propagation — evidence index

## Files

| File | Status | Meaning |
|---|---|---|
| `m03_e2e.py` | driver, 141,576 B, sha256 `d77db5dc8f2a94e902f1e6f6dde1b192a955bc635de948ec97ad94b42105f1bf` | The 136-check Y400 real-PostgreSQL E2E driver. Only the 14 stale pre-fix expectations were rewritten (see `m03-assertion-contract-classification.md`); check count stays 136 and no assertion was removed, skipped or weakened. |
| `m03-report-20261004_001402.json` | **PRE-FIX FAILED RUN** (preserved unchanged) | First real-PG run of shipped M03 code (commit `63b8596`). **Do not read this as a pass.** |
| `m03-report-20261004_031923.json` | **POST-FIX PASS RUN** | The revalidation of the fixed code (`0430382`) on the same host, same disposable-PG setup, same 136-check driver. |
| `m03-assertion-contract-classification.md` | assertion classification | Per-assertion `old expectation / new expectation / why stale / supporting M03 contract` for the 14 assertions that encoded the pre-fix defects. |

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

## POST-FIX PASS RUN — headline

```text
verdict             = PASS
checks              = 136
pass                = 136
fail                = 0
not_evaluated       = 1 coverage gap (/prefetch public surface, offline host)
exit                = 0
production_mutation = NONE
host                = DESKTOP-EQP3OBU (Y400)
runner_sha256       = d77db5dc8f2a94e902f1e6f6dde1b192a955bc635de948ec97ad94b42105f1bf
db                  = m03e2e_20261004_031923 (disposable, 127.0.0.1:55432)
source              = src-root (isolated); v3core resolved from src-root, not site-packages
report_sha256       = 2ca6ed7ec0977ca12d058d7c06b82a5fe1162a6a74c73ae71ef9e7bfd0c45e1e
```

### A / B / C closure evidence in that run

| Defect | Assertion now green | Observed on real PostgreSQL |
|---|---|---|
| A — `int8range @> %s` without `::bigint` | `discovery_defect_reproduces_on_real_postgres` | `real_discover={"ok": true, "affected": {"topic": ["t_m03e2ed1"], "observer_note": ["2"]}}` |
| A — propagation never wrote a row | `real_propagation_maps_zero_derived_artifacts` | `counts={'mapped': 2, 'invalidated': 0, ...}` with the sidecar row physically present |
| B — real `PgPool` fail-OPEN suppression | `suppression_fails_open_on_the_production_plumbing`, `real_pgpool_recall_lane_fails_open_on_the_lease_defect` | `real_pool_suppression={'ids': ['t_m03e2ed1'], 'degraded': False}`; lane hits `['topic_t_m03e2edok']` only |
| C — invalidation `INSERT` arity | `sidecar_writer_defect_reproduces_on_real_postgres`, `sidecar_rows_written_only_via_the_documented_workaround` | `real_writer=None`, `writer='shipped _write_invalidations'` |

Product gate (the thing M03 actually promises): the stale derived artifact is
**physically preserved** (`d1_body_never_rewritten`, `d1_physically_present_after_correction`)
and **excluded from current injection** (`d1_absent_from_current_recall_after_correction_pgpool_injected`,
`real_suppression_fn_drops_only_d1` → `removed=['topic_t_m03e2ed1']`, `kept=['topic_t_m03e2edok']`),
while the unrelated control topic keeps being recalled.

Idempotency and durability were not touched: `fixture_propagation_replay_is_idempotent`
still pins `DEDUPLICATED` + `inserted == 0`, and the negative control
(`negative_control_write_probe`) still classifies a missing sidecar table as
`MIGRATION_REQUIRED` rather than falling back.

### The one NOT VERIFIED item

`POST /prefetch` (the real public surface) could not be exercised on this offline
host: the prefetch/rerank path resolves the stub embedding model name as a
HuggingFace repo id and blocks on 5 network retries for `tokenizer.json`
(`WinError 10060`). This is a coverage gap, **not** an A/B/C or stale-suppression
item — those are all evaluated and green. The `/prefetch` limitation is identical
in the pre-fix run.

## The three P0 defects the pre-fix run found (all invisible to in-memory fakes)

| # | Defect | Mechanism | Consequence |
|---|---|---|---|
| A | `derived_invalidation._discover`: `int8range @> %s` | psycopg2 adapts a Python int as `int4`; `int8range @> integer` does not exist | real PG `UndefinedFunction` → propagation never wrote a single invalidation row |
| B | `_acquire_lease(None, pg)` at `derived_invalidation` ×2 and `derived_rebuild` ×1 | `_acquire_lease(pool, pg)` is pool-slot-first; a real `PgPool` in the `pg` slot takes `_PgStoreLeaseAdapter`, whose `.connection` is a `PgLease`, not a DBAPI connection | sidecar read/write fails → suppression degrades and **fails OPEN** |
| C | invalidation `INSERT` listed 12 target columns but 15 VALUES expressions | `NOW(), NULL, NULL` were appended to 12 `%s` | real PG `INSERT has more expressions than target columns` — the shipped writer had never succeeded once |

## Status of this directory

- The failed run above is **preserved as-is**; it is the evidence that M03 as shipped at `63b8596` was broken on real PostgreSQL.
- Fixes for A/B/C live in commit `0430382` on `feature/m03-derived-correction-propagation` (`fix(m03): repair real-pg invalidation writes and lease routing`), with the CI-visible static assertions in `src/v3-core/tests/test_m03_sql_arity_and_lease_routing.py`.
- The post-fix revalidation is a **separate file** (`m03-report-20261004_031923.json`); the pre-fix report is never written over.
