# Hippocampus — long observation index

Status: `DECISION_REQUIRED`

## Candidate (deployed)

- Branch: `feature/long-observation-index-v1`
- `product_code_head`: `2f23e34574880e3c61ed0e9a2d73ded4e685edac`
- `artifact_build_head` / final documentation head at build: `75e7d1297927fa932bf766fddbf06407660f23d2`
- Candidate wheel: `C:\hp-testbed\artifacts\LONG_OBSERVATION_PRODUCTION_CANDIDATE\v3_core-4.0.0-py3-none-any.whl`
- Candidate wheel SHA256: `25a3bb46296b4aae261e8eed686b9d0fcd96e6bda1761343af75979d4d67fc5c`
- Previous wheel `9bb82c79…`; old vs new product blobs byte-identical (`source_wheel_product_drift = 0`). The earlier blocker was stale provenance metadata, not a bad artifact.
- Deployed to the live Hermes venv; installed member byte-equality `108/108`.

## Production rollout performed

- Additive migration only: `observation_embedding_chunks` created (FK, unique constraint, parent index, ivfflat index); `observation_notes` unchanged; 0 rows.
- Gateway `3924/15408 -> 3772/5252`; serve `6548/18172 -> 2864/19988`; old PIDs absent; heartbeat fresh; Feishu inbound working.
- Forward smoke: P0 validator active; embed policies unchanged; a post-restart short observation got a 1024-dim `bf32771ecbd1` vector; 0 new unexplained NULL.

## Historical repair result

```text
LONG_OBSERVATION_PRODUCTION_CANARY     = PASS          (id 759)
LONG_OBSERVATION_HISTORICAL_REPAIR     = COMPLETE
repaired_ids                           = 759 (canary), 634, 726, 732, 743, 746
observation_notes NULL parents         = 6 -> 0
sidecar rows                           = 12 (6 parents x 2 children, indexes 0..1)
source hashes                          = unchanged for all 6 historical long rows
embed model fingerprint                = bf32771ecbd1 uniform; 0 wrong-model parents
embedding_failures                     = 0 / 0 unresolved
new unexplained NULL (12h)             = 0
```

Structural acceptance (children count/dense indexes/contiguous offsets/100% coverage/
per-child token window/source+chunk sha/representation version/parent vector/ledger)
passed for all six rows.

Recall acceptance was split into two layers:

- Layer A (indexing retrieval, must pass): passed for every row — the tail-only query
  surfaced the row's child candidate and the candidate mapped back to the full parent
  observation.
- Layer B (final recall policy): `746` returned the full 21699-char parent at rank 1
  (`FINAL_RECALL_PASS`). `743` was suppressed by the pre-existing one-note-per-day cap:
  its tail child scored 0.6776 (above 743's own parent 0.6753) but same-day observation
  754 (`v752-compressed`) scored 0.6805 and won the day
  (`FINAL_RECALL_POLICY_SUPPRESSED`, not a repair failure).
- Registered as design debt `OBSERVATION_SAME_DAY_DEDUPE_CAN_SUPPRESS_RELEVANT_PARENT_HIT`
  (`P2 / RECALL_POLICY_DESIGN`) — `evidence/design-debt/`. No ranking/dedupe code changed.

## Remaining production actions

1. Decide whether the same-day dedupe design debt gets a follow-up comparison
   (one-per-day / top-N-per-day / strong-relevance exemption / relevance-first then diversity).
2. Decide whether the static audit's 18 potentially-unbounded non-observation call sites need follow-up.
3. Other NULL backfills (`conversation_stream` 1072, `qa_pairs` 11, `yin_paragraphs` 7) remain untouched and unauthorized.


Evidence root: `C:\hp-testbed\evidence\`
Production canary + repair evidence: `C:\Users\servi\workspace\backups\long-observation-canary-20260922\`
