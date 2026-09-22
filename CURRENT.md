# Hippocampus — embedding reliability recovery

Status: `DECISION_REQUIRED`

## This round (2026-09-23) — audit A + historical NULL debt B + census C

Branch: `feature/embedding-reliability-recovery` (cut from `425c42f`, the closed
long-observation branch tip; no long-observation product code / schema / tests / docs /
operator CLI / wheel was touched this round).

### A — 18 POTENTIALLY_UNBOUNDED embedding call sites, reclassified

Evidence: `evidence/embedding-input-window-audit-v2.final.json`

```text
total_call_sites            = 18
classified                  = 18
UNKNOWN                     = 0
SAFE_UPSTREAM_BOUNDED       = 4    (shared seam + planner-gated backfill paths)
SAFE_DOMAIN_BOUNDED         = 5    (legacy topic CLI, seed import, health constant, [:1000] edits)
QUERY_NEEDS_CAP             = 5    (tool-path queries: v3_search / v3_prefetch)
PROVEN_PRODUCTION_DEFECT    = 4    (Core.prefetch_to_context_block — production injection path)
NEW_P1                      = none
code changed this round     = none (task rule: no P1 -> no code change for its own sake)
```

Method: code evidence (call site + enclosing function + upstream producer) + read-only
production census of every real input family (bge-m3 tokenizer) + a controlled synthetic
probe through the REAL production embed config.

Measured provider window: 8001 tokens OK, 9001 tokens -> HTTP 400 `code=20015`.

Query family (the only unbounded production input): all 1249 `role='user'` rows in
`conversation_stream` -> p50 40 tok, p90 2778, p99 5167, max 9134; exactly 1 row (0.08%)
exceeds the 7680-token safe window. The failure is swallowed by
`except Exception: logger.debug` -> the vector lane silently degrades to keyword-only for
that turn. Ranked **P2** (deterministic, but 0.08% frequency and graceful degradation), not
P1 — so no code was changed; the fix (token-safe query cap, head+tail keep, new
`call_embedding_for_query` seam) is designed and recorded in the audit JSON as backlog.

Second finding, recorded only: `SEARCH_CARDS_EMBED_CACHE_INVERTED_CONDITION` (P2) —
`Core.search_cards` embeds the query only when it is already in `_EMBED_CACHE`
(`__init__.py:1979`), so cold `v3_search` calls run keyword-only. `__init__.py:2474` shows the
same guard was already removed on the prefetch path.

### B — historical NULL embedding debt cleared

```text
B1 qa_pairs            11 -> 0    QA_HISTORICAL_EMBEDDING_REPAIR   = COMPLETE
B2 yin_paragraphs       7 -> 0    YIN_HISTORICAL_EMBEDDING_REPAIR  = COMPLETE
B3 conversation_stream 1072 -> 0  (batches A=20, B=100, C=300, D=632)
C  whole-DB NULL census           UNEXPLAINED_NONEMPTY_EMBEDDING_NULL = 0
```

Evidence: `evidence/qa-historical-repair-20260923.json`,
`evidence/yin-historical-repair-20260923.json`,
`evidence/conversation-stream-drain-20260923.json`,
`evidence/final-census-embedding-recovery.json`

B1 — all 11 rows were long (mode `qa_chunked`), 2–3 children each, 26 children total,
representation `v0.2.0-long-qa`; per-field coverage 100%, dense indexes, contiguous offsets,
`source_sha256` = hash of the chunk span (canonical contract, verified against stored
content and the live source slice), parent 1024-dim `bf32771ecbd1`, ledger 0.
Recall: three-slice protocol (first/mid/last of the tail child) — every row retrieved with
its FULL question+answer through the production merge shape; ranks reported as measured.

B2 — writer classification re-verified and stable: all 7 NULL rows are `yin_pool`
(`yin_version` = 印 filename, Chinese section names); none matches the `e1` predicate
(`yin_version LIKE 'e1_seg_%' AND section LIKE 'E1/%'`). Canonical inputs applied per writer:
`content[:2000]` for e1, `f"{section}. {content[:1500]}"` for yin_pool. Canonical proof:
re-embedding the canonical text reproduces the stored vector at cosine >= 0.9999 for all 7.

B3 — serial provider usage only (shipped CLI loops serially); no concurrency, no 429, no
timeout, 0 retryable / 0 permanent failures. Throughput 52–54 rows/min (≈1.1 s per call).
Source integrity was checked on EVERY row in each batch (not a sample): 1052/1052 hashes
unchanged. `conversation_stream` has no `embed_model` column, so the fingerprint was proven
by re-embedding the canonical slice (`content[:2000]`) and comparing: cosine >= 0.9999 on
every sampled row in every batch.

### Final whole-DB state

```text
conversation_stream    14027 rows   0 NULL   1024-dim
qa_pairs               14035 rows   0 NULL   1024-dim   embed_model = bf32771ecbd1 (100%)
topics                   347 rows   0 NULL   1024-dim   embed_model = bf32771ecbd1 (100%)
yin_paragraphs          1303 rows   0 NULL   1024-dim   embed_model = bf32771ecbd1 (100%)
observation_notes        782 rows   0 NULL   1024-dim   embed_model = bf32771ecbd1 (100%)
qa_embedding_chunks      263 rows / 93 parents   0 NULL   0 non-1024
observation_embedding_chunks 12 rows / 6 parents 0 NULL   0 non-1024
embedding_failures         0 rows   0 unresolved
forward writer (12h)      cs 192/0, qa 21/0, obs 5/0, yin 6/0 NULL
runtime identity          gateway 3772/5252, serve 2864/19988 — unchanged this round
UNEXPLAINED_NONEMPTY_EMBEDDING_NULL = 0
```

### Recall policy debt (unchanged, still independent)

```text
OBSERVATION_SAME_DAY_DEDUPE_CAN_SUPPRESS_RELEVANT_PARENT_HIT
P2 / RECALL_POLICY_DESIGN / RECORDED_ONLY
not touched this round — to be designed after embedding reliability closes
```

## Previous round — long observation index (closed)

Status of that work: `LONG_OBSERVATION_PRODUCTION_CANARY = PASS`,
`LONG_OBSERVATION_HISTORICAL_REPAIR = COMPLETE`. Frozen: do not modify the long-observation
planner, sidecar schema, aggregation, recall merge, or observation historical rows.

- Branch: `feature/long-observation-index-v1` @ `425c42f`
- `product_code_head`: `2f23e34574880e3c61ed0e9a2d73ded4e685edac`
- Candidate wheel SHA256: `25a3bb46296b4aae261e8eed686b9d0fcd96e6bda1761343af75979d4d67fc5c`
  (deployed to the live venv; installed member byte-equality 108/108)
- Repaired long observations: 759 (canary), 634, 726, 732, 743, 746 — 6 parents, 12 children,
  source hashes unchanged, fingerprint `bf32771ecbd1` uniform, ledger 0.
- 743 recall = `FINAL_RECALL_POLICY_SUPPRESSED` (same-day 754, registered as the P2 debt above);
  759 head likewise suppressed by same-day 751.
