# Global development baseline

> Single integration baseline for all follow-on development. Every later work stream (A02, Q01,
> A03, …) branches from here instead of from a historical feature branch.

```text
branch              integration/global-baseline-v1
integration HEAD    bb2446d   (docs)  -> see `evidence/global-baseline-wheel-manifest.json`
product-code HEAD   86dd126   (merge)
base                origin/feature/embedding-reliability-recovery @ 8d7a32f
merged              origin/feature/runtime-integrity @ a007d72
product blobs unchanged after 86dd126 (only CURRENT.md / README / docs / evidence follow)
```

Merge footprint: 45 files, 14839 insertions, 1 deletion; the only modified (non-new) files are
`src/v3-core/src/v3core/distribution_cli.py` (product) and `README.md` (docs).

## Included capabilities

| Capability | Source | Verification evidence | User-visible effect | Known limits |
| --- | --- | --- | --- | --- |
| v0.2.1 public baseline (packaged wheel, bootstrap, doctor, upgrade) | `origin/main` `0bd9e1e` | ancestor of the base; full suite green for these modules | installable package, `hippocampus bootstrap` / `doctor` | public-alpha surface only, see `docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md` |
| P0 generated-context boundary (situation/identity validator, generation output cap, persistence/injection boundary) | `hotfix/e1-generated-context-boundary` `7808b89` | ancestor of the base; `test_e1_generated_context_contract.py`, `test_e1_identity_block_boundary.py`, `test_e1_output_budget.py` | generated context can no longer be persisted/injected past the boundary | — |
| Embedding write reliability (named policies, durable failure ledger, canonical representations, forward accounting) | `integration/p0-embedding-reliability` `22606e4` | ancestor of the base; `test_embedding_write_reliability.py`, `test_embedding_policy_guard.py`, `test_embedding_failure_resolution.py` | durable writes do not lose embeddings to a provider tail; failures are accounted, not silent | provider window 8192 tokens on source-derived text is handled by chunking, query text is not yet capped (A02) |
| Operator repair D1–D5 (`tools/embedding_backfill.py`) | `fix/embedding-backfill-operator` `9c4d191` | ancestor of the base; `test_backfill_cli_entrypoint.py`, `test_backfill_canonical_input.py`; used for the closed historical repairs | `hippocampus`-packaged CLI can dry-run and repair NULL embeddings per table | dry-run by default; `--apply` is explicit |
| Long QA (chunked QA index, parent aggregation, child→parent recall) | this line (pre-long-observation) | `qa_embedding_chunks.sql` + `test_embed_chunks_contract.py`; wheel smoke functional pass | very long QA pairs stay retrievable; recall merges children back to the parent | parent embedding is an aggregate of child vectors |
| Long Observation (planner, writer, sidecar, parent/child recall, historical repair support) | `feature/long-observation-index-v1` `425c42f` | ancestor of the base; `test_long_observation_red.py`, `test_observation_chunks_contract.py`, `test_observation_writer_contract.py`, `test_long_observation_writer_recall.py`; production canary PASS + 6/6 historical rows repaired | long observation notes are indexed without losing source | same-day dedupe can suppress a relevant parent hit (`OBSERVATION_SAME_DAY_DEDUPE_CAN_SUPPRESS_RELEVANT_PARENT_HIT`, P2, design-only) |
| Runtime integrity / deployment identity (doctor `--runtime`, live-env resolver, shadow detection, install/uninstall plan engine, reliability diagnose/repair CLI) | `feature/runtime-integrity` `a007d72` | merged this round; `test_runtime_integrity*.py`, `test_reliability_*.py` (275 new tests) | the loaded package can be identified and a plan produced before touching a host | reviewed as `READY_FOR_REVIEW`; not yet exercised by a production release — A03 owns the real canary; the plan engine does not apply changes |
| Installation / upgrade / doctor / migration for the above | `main` + this line | `distribution_cli` splices `qa_embedding_chunks.sql` and `observation_embedding_chunks.sql`; `upgrade_v0_2.sql` carries additive DDL; `test_alpha_bootstrap_contract.py`, `test_production_upgrade_contract.py` | existing installs upgrade without dropping data | bootstrap schema is the 9-table set |

## Not included (and why)

| Capability | Verdict | Why |
| --- | --- | --- |
| Query cold-cache fix (`SEARCH_CARDS_EMBED_CACHE_INVERTED_CONDITION`) | `OPEN — A02` | RED test pinned; fix is the A02 scope |
| Long-query handling (`QUERY_NEEDS_CAP`) | `OPEN — A02` | cap design is an explicit A02 gate; RED test pinned |
| Q02 daily dedupe change | `RECORDED_ONLY` | design debt; changes recall ranking semantics |
| Correction semantics M01+ | `NOT_IN_THIS_RELEASE` | not started |
| CRL host integration / second host | `NOT_IN_THIS_RELEASE` | separate work stream |
| Recall V2 (g6a/g6b/g6c) | `NOT_RELEVANT_TO_THIS_RELEASE` | separate contract/evaluator line |
| Installer-execution v2 (defects A1–A9) | `NOT_RELEASE_READY` | separate line, defects unfixed, never released |

## Release discipline

- This baseline is **not deployed**. A03 owns the canary, the production wheel install and any
  restart.
- The production data repairs (Long Observation 6/6 historical rows, 1090 embedding backfills) are
  **state and evidence**, not code: do not re-run them from this baseline.
- Two known-failing test groups are inherited, not regressions: 3 `test_importers_contract.py`
  hermes `state.db` fixture failures (identical on the previous baseline), and 3
  `test_reliability_cli.py` `--help` smoke tests that require a repo-local `.venv`
  (`REPO_ROOT/.venv/Scripts/python.exe`); with that assumption satisfied the file is 29 passed.
