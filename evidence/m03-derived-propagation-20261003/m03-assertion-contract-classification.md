# M03 E2E — assertion contract classification (14 pre-fix assertions)

Scope: the 14 assertions that FAIL against the *fixed* product code
(`043038220425696ba267da28fc2b1fcbf0749f9e`). Every one of them was written while
the three real-PG defects (A/B/C) were still live, so each encoded "the defect is
present" as its expectation.

Rule applied (from the task book): an expectation may only be rewritten when it is
**stale because a locked M03 contract now requires the opposite behaviour**. No
assertion is deleted, skipped, xfailed, weakened to a non-empty check, or turned
from failure into warning. Check count stays 136.

Product code is frozen at `0430382` for this step; only the driver's expectations
change. If the focused suite is still red after this, the implementation is what
gets fixed — not the remaining expectations.

| # | assertion | old expectation | new expectation | why the old expectation is stale | locked M03 contract that supports the new expectation |
|---|---|---|---|---|---|
| 1 | `real_propagation_reports_the_discovery_defect` | `status == PROPAGATION_FAILED` and `"int8range @> integer"` in error | `status in (APPLIED, DEDUPLICATED)` and `error` empty, **and** the D1 sidecar row physically exists | The old expectation *is* defect A (`int8range @> %s` without `::bigint`). A is fixed, so the real propagation over the public tool surface must succeed and leave a durable row. | `M03 §The invalidation sidecar` (rows are written, deterministic id) + `§Current recall must recognise staleness` (the row is the product gate) |
| 2 | `real_propagation_never_claims_success` | `not success` and `derived_propagation == "pending"` | `success is True` and `derived_propagation in (applied, deduplicated)` | Same as #1: "never claims success" was a truthful-failure assertion for a propagation that could not run at all. With A fixed, a truthful receipt reports success. | `M03 §Honest reporting requirements` (receipt must reflect what actually happened) |
| 3 | `real_propagation_maps_zero_derived_artifacts` | `mapped == 0` and `invalidated == 0` | `mapped >= 1` **and** (`invalidated >= 1` or the D1 sidecar row already exists) | `mapped == 0` was the A defect: discovery could not execute, so nothing could be mapped. A fixed ⇒ discovery must find D1. `invalidated == 0` is a legitimate idempotent replay outcome when the correction already propagated (the row exists). | `M03 §What becomes stale — and at what granularity` (topic is mappable when the resolved `qa_pairs.id` is in `topic_entries.source_qa_id`) + `§Re-running propagation` |
| 4 | `inprocess_propagation_confirms_the_same_defect` | `status == PROPAGATION_FAILED` and `"int8range @> integer"` | `status in (APPLIED, DEDUPLICATED)` and no `int8range` error | Same as #1, on the in-process path (transport-independent capture). | same as #1 |
| 5 | `discovery_defect_reproduces_on_real_postgres` | `real_discover.ok is False` and `"int8range @> integer"` | `real_discover.ok is True` and `d1_id in affected.topic` | The old expectation pins the operator-mismatch defect itself. A fixed ⇒ the real `_discover` must execute and return D1 as affected. | `M03 §What becomes stale` (topic granularity is whole-topic, keyed by `source_qa_id`) |
| 6 | `fixture_propagation_wrote_the_sidecar_through_the_real_writer` | `status == PROPAGATION_APPLIED` and `inserted >= 1` | **DRIVER_EXPECTATION_BUG** — new expectation: `writer == "shipped _write_invalidations"` and `real_writer is None` **and** the sidecar row for `(correction_id_1, topic, d1_id)` physically exists | The old expectation assumed `prop_real` (#1) *fails*, making this fixture the first writer of the row. With A fixed, `prop_real` succeeds at line 1943, so by line 2011 the same `correction_id_1` has already been propagated and the fixture call is a **replay** (`DEDUPLICATED`, `inserted == 0`). The driver contradicts itself: line 2042 (`fixture_propagation_replay_is_idempotent`) asserts `DEDUPLICATED` + `inserted == 0` for the *same* operation sequence. The new expectation is strictly stronger — it checks the writer identity and the physical row instead of a return code. | `M03 §The invalidation sidecar` (`UNIQUE (correction_id, derived_kind, derived_id)` + deterministic id ⇒ replay writes zero rows) + `§Re-running propagation` |
| 7 | `sidecar_writer_defect_reproduces_on_real_postgres` | `real_writer.ok is False` and `"more expressions than target columns"` | `real_writer is None` (no writer failure recorded) and `writer == "shipped _write_invalidations"` | The old expectation pins defect C (12 columns vs 15 VALUES expressions). C is fixed ⇒ the shipped writer must not fail. `real_writer is None` is the observable shape of "no failure was recorded". | `M03 §The invalidation sidecar` (the shipped writer executes; no workaround path) |
| 8 | `sidecar_writer_defect_is_the_column_values_arity_mismatch` | `error_class == "SyntaxError"` | `error_class is None` | Same as #7 — this assertion *is* the arity defect. | same as #7 |
| 9 | `sidecar_rows_written_only_via_the_documented_workaround` | `writer.startswith("harness INSERT")` | `writer == "shipped _write_invalidations"` | The old expectation records that only the harness workaround could insert. C fixed ⇒ the shipped writer is the one that writes. | same as #7 |
| 10 | `suppression_fails_open_on_the_production_plumbing` | `degraded is True` and no suppression ids | `degraded is False` **and** `d1_id in ids` | The old expectation pins defect B (PgPool routed into the `pg` slot ⇒ fail-OPEN). B is fixed ⇒ suppression must read the real sidecar and return the invalidated id. This tightens the fail-closed contract, it does not relax it. | `M03 §Current recall must recognise staleness` (suppression happens at the injection layer; existing recall behaviour for non-invalidated candidates is unchanged) |
| 11 | `real_pgpool_recall_lane_fails_open_on_the_lease_defect` | `topic_{d1_id}` in `topic_hit_ids` and `suppression_on_real_pool.degraded` | `topic_{d1_id}` **not** in `topic_hit_ids` and `suppression_on_real_pool.degraded is False` and `d1_id` in its `ids` | Same as #10, observed at the recall lane with a real PgPool injected. The old expectation asserts the stale artifact is still injected — exactly what the M03 gate forbids. | same as #10 + `§Rebuild` (`Suppression is permanent; state is lifecycle only`) |
| 12 | `propagate_rerun_still_reports_the_defect_truthfully` | `status == PROPAGATION_FAILED` and `derived_propagation == "pending"` | `status == PROPAGATION_APPLIED` and `derived_propagation == "applied"` | The rerun replays propagation for the same `correction_id` *after* chain Y was freshly seeded, so there are genuinely new rows to write. The old expectation recorded the A defect blocking the rerun; with A fixed the rerun must write, and the very next assertion (`chain_y_head_and_prev_were_mapped_by_the_real_writer`) independently requires those rows. | `M03 §Re-running propagation` (explicit maintenance replay for a single `correction_id`) |
| 13 | `s30_real_propagation_also_blocked_by_the_same_defect` | `status == PROPAGATION_FAILED` and `derived_propagation == "pending"` | `status in (APPLIED, DEDUPLICATED)` and `derived_propagation in (applied, deduplicated)` | Same as #1, on the §30 provider-unavailable scenario. | same as #1 |
| 14 | `s30_fixture_propagation_needs_no_provider_at_all` | `status == PROPAGATION_APPLIED` and `inserted >= 1` | **DRIVER_EXPECTATION_BUG** — new expectation: `writer == "shipped _write_invalidations"` and `real_writer is None` **and** the sidecar row for `(cid30, topic, d30_id)` physically exists | Same contradiction as #6 on the §30 path: line 2336 already propagates `cid30` over the real surface, so the fixture at 2347 is a replay. The point of this assertion — that no provider is needed to write the sidecar — is preserved and strengthened by checking the writer identity and the physical row. | `M03 §Rebuild` (a rebuild that cannot run because no provider is available keeps the artifact suppressed; the invalidation write itself never needs a provider) + `§The invalidation sidecar` |

## Assertions explicitly NOT touched

The following remain byte-identical, because they lock acceptance contracts rather
than pre-fix behaviour (task book §1 forbidden list):

- durability / history preservation: `a_content_bytes_unchanged`, `d1_body_never_rewritten`, `d1_physically_present_after_correction`, `raw_layers_never_rewritten`, `s31_t1_artifact_still_present_and_unmodified`
- idempotency: `fixture_propagation_replay_is_idempotent`, `s31_rebuild_replay_is_deduplicated_on_the_same_t2`, `s31_replay_created_no_extra_artifact`
- fail-closed isolation: `suppression_not_degraded`, `suppression_reads_the_real_sidecar_on_the_store_plumbing`, `d1_in_suppression_set_after_correction`
- provider failure safety: `s30_http_rebuild_never_claims_success`, `s30_unusable_provider_is_truthful_provider_unavailable`, `s30_raising_provider_is_a_truthful_failure`, `s30_sidecar_byte_identical_after_failed_rebuilds`, `s30_no_artifact_invented_by_failed_rebuilds`, `s30_d30_still_physically_present`, `s30_d30_still_suppressed_after_failed_rebuilds`
- source trace: `d1_sidecar_correction_id_matches`, `d1_sidecar_source_is_a_and_replacement_is_b`, `d1_sidecar_canonical_qa_id`, `real_propagation_source_resolution_still_succeeded`
- unrelated artifact unaffected: `control_topic_still_recalled_after_correction`, `real_suppression_fn_drops_only_d1`
- A→B→C terminal resolution: `s31_t1_absent_from_current_recall_after_rebuild_pgpool_injected`, `s31_t2_artifact_created`, `s31_replacement_derived_id_is_t2`, `s31_t1_still_in_suppression_set_after_rebuild`, `s31_t2_not_in_suppression_set`
- negative controls: `negative_control_*`, `production_mutation_none`, `no_secret_in_report`, `no_residual_server_process`

## DRIVER_EXPECTATION_BUG summary

Two assertions (#6, #14) are not stale contract expectations but a genuine
self-contradiction in the driver, provable without any product code:

- `#6` requires `APPLIED + inserted >= 1` for a fixture call at line 2011, while
  `fixture_propagation_replay_is_idempotent` (line 2042) requires
  `DEDUPLICATED + inserted == 0` for the *identical* probe call with the identical
  `correction_id_1`, after line 1943 already propagated the same correction.
  Two assertions over the same operation sequence cannot both hold once the
  propagation actually writes on the first attempt.
- `#14` repeats the same shape on the §30 path (line 2336 vs 2347).

Before/after evidence for both is preserved in the new run report
(`assertion_updates` block) and in the retained pre-fix report
`m03-report-20261004_001402.json`.
