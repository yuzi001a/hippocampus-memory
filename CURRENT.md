# Hippocampus — long observation index

Status: `DECISION_REQUIRED`

## Candidate (deployed)

- Branch: `feature/long-observation-index-v1`
- `product_code_head`: `2f23e34574880e3c61ed0e9a2d73ded4e685edac`
- `artifact_build_head` / final documentation head at build: `75e7d1297927fa932bf766fddbf06407660f23d2`
- Candidate wheel: `C:\hp-testbed\artifacts\LONG_OBSERVATION_PRODUCTION_CANDIDATE\v3_core-4.0.0-py3-none-any.whl`
- Candidate wheel SHA256: `25a3bb46296b4aae261e8eed686b9d0fcd96e6bda1761343af75979d4d67fc5c`
- Previous wheel SHA256 `9bb82c792a0d1ccbd62ed1511b3b5502afc8f751a8273397c7beb60629544d4a`; old vs new product blobs are byte-identical (`source_wheel_product_drift = 0`). The old blocker was stale provenance metadata, not a bad artifact.
- Deployed to the live Hermes venv; installed member byte-equality `108/108`.

## Production rollout performed

- Additive migration only: `observation_embedding_chunks` created (FK, unique constraint, parent index, ivfflat index); `observation_notes` unchanged; 0 rows.
- Gateway `3924/15408 -> 3772/5252`; serve `6548/18172 -> 2864/19988`; old PIDs absent; heartbeat fresh; Feishu inbound working.
- Forward smoke: P0 validator active; embed policies unchanged; a post-restart short observation got a 1024-dim `bf32771ecbd1` vector; 0 new unexplained NULL.
- Six-row dry-run re-planned against current production source: no source drift, all long, 2 children each.

## Canary result

- Repaired exactly one row: `observation_notes.id = 759` (smallest long row, 8512 tokens).
- Source SHA unchanged before/after: `9adee801b4d5a9860b0ea78cd1fd5c7d432c03902e6c3c1e81f319ec969d7838`.
- Children: `[7680, 833]` tokens, offsets `[0,14844]` + `[14844,16282]`, coverage complete, representation `v1-long-observation`, fingerprint `bf32771ecbd1`.
- Parent: 1024-dim vector, fingerprint correct, aggregated from all children. Ledger unresolved = 0.
- Recall proof (tail-only query): parent-only cosine `0.6631`, best child `0.7035`, merged rank 1, returned the FULL 16282-char parent.
- Head-region query returned no hit for 759 — pre-existing newest-per-day semantics (a same-day observation scored higher). Not a long-observation regression.
- `LONG_OBSERVATION_PRODUCTION_CANARY = PASS`

## Remaining production actions

1. `observation_notes` NULL parents: `5` (`634, 726, 732, 743, 746`) — awaiting a separate authorization to finish the batch.
2. Decide whether the newest-per-day interaction with long-child hits needs a follow-up design item.
3. Decide whether the static audit's 18 potentially-unbounded non-observation call sites need follow-up.

Evidence root: `C:\hp-testbed\evidence\`
Production canary evidence: `C:\Users\servi\workspace\backups\long-observation-canary-20260922\`
