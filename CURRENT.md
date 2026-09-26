# Hippocampus — global development baseline (A01)

Status: `A01 = DONE` / `A02 = DOING` (see the A02 section at the end)

## A01 — one integration baseline for all follow-on work

```text
branch                 integration/global-baseline-v1
integration HEAD       bb2446d (docs commit; product-code HEAD = 86dd126, the merge)
product blobs unchanged after 86dd126 — only CURRENT.md / README / docs / evidence follow
documentation HEAD     bb2446d
base                   origin/feature/embedding-reliability-recovery (8d7a32f)
merged                 origin/feature/runtime-integrity (a007d72)
conflicts              none (git auto-merged distribution_cli.py; both sides verified present)
```

Inclusion matrix (proven by `merge-base` / `merge-base --is-ancestor` / blob comparison, not by
commit messages):

| Capability | Verdict | Evidence |
| --- | --- | --- |
| A. v0.2.1 public baseline | `ALREADY_INCLUDED` | `origin/main` (`0bd9e1e`) is an ancestor of the base |
| B. P0 generated-context boundary | `ALREADY_INCLUDED` | `hotfix/e1-generated-context-boundary` (`7808b89`) is an ancestor of the base |
| C. embedding reliability (policies, ledger, D1–D5, canonical representations) | `ALREADY_INCLUDED` | `integration/p0-embedding-reliability` (`22606e4`) and `fix/embedding-backfill-operator` (`9c4d191`) are ancestors |
| D. long QA | `ALREADY_INCLUDED` | `qa_embedding_chunks.sql` + canonical planner + child→parent recall present on the base |
| E. Long Observation | `ALREADY_INCLUDED` | `feature/long-observation-index-v1` (`425c42f`) is an ancestor of the base |
| F. runtime integrity / deployment identity | `NEEDS_INTEGRATION` → merged | was a separate line off `main`; only shared file `distribution_cli.py`, disjoint hunks |
| G. installer / upgrade / doctor / migration required by C–E | `ALREADY_INCLUDED` | `distribution_cli` splices `qa_embedding_chunks.sql` + `observation_embedding_chunks.sql`; `upgrade_v0_2.sql` carries the additive DDL |
| installer-execution v2 (defects A1–A9) | `NOT_RELEASE_READY` | separate line, defects unfixed; not carried in |
| Recall V2 (g6a/g6b/g6c) | `NOT_RELEVANT_TO_THIS_RELEASE` | separate contract/evaluator line |

`feature/long-observation-index-v1` relative to `feature/embedding-reliability-recovery`: it is an
**ancestor** (the recovery branch was cut from the long-observation tip, and its only later commits
are evidence + `CURRENT.md`). Proven by `merge-base --is-ancestor`; the two tips are byte-identical
on all 14 architecture-critical product files.

Merge footprint, measured: 45 files, 14839 insertions, 1 deletion. The only **modified** (non-new)
files are `src/v3-core/src/v3core/distribution_cli.py` (product; disjoint hunks — the sidecar
splice/migration markers and the new `install --plan` / `uninstall --plan` / `reliability` /
runtime-integrity subcommands are all present after the merge) and `README.md` (docs; adds the
`hippocampus doctor --runtime --wheel …` step that the merged code implements). Merged code was
not judged by commit message.

Validation (isolated venvs, Python 3.11.15, editable installs, no production contact):

```text
candidate full suite    1126 passed   3 failed   7 skipped   (577.0s, JUnit run)
baseline  full suite     853 passed   3 failed   5 skipped   (431.1s, JUnit run)
candidate vs baseline   collected 1136 vs 861; 275 tests only in candidate, 0 only in baseline
inherited failures      3  test_importers_contract hermes state.db fixture
                           (per-test differential: same 3 ids fail on both trees;
                            in isolation each tree reports 3 failed / 18 passed / 1 skipped)
environment-dependent   3  test_reliability_cli --help smoke, hardcoded
                           REPO_ROOT/.venv/Scripts/python.exe; with that assumption
                           satisfied locally the file is 29 passed
NEW_REGRESSION          0
```

`REAL_Y400_FULL_DIFFERENTIAL = NOT_AVAILABLE` (not applicable to A01; A03 owns the real canary).

Artifact:

```text
artifacts/HIPPOCAMPUS_GLOBAL_BASELINE_CANDIDATE/v3_core-4.0.0-py3-none-any.whl
wheel SHA256           e543f4dd1ce846407b45c7d5add3d36f3896624839c422233f47b4ef55b3bcc4
members                130  (21/21 key product members byte-identical to the source tree)
SOURCE_WHEEL_PRODUCT_DRIFT = 0
wheel smoke (fresh isolated venv): import identity / P0 validator / forward embedding policy
constants / failure ledger / backfill CLI --help / long-QA planner / Long Observation planner /
child→parent merge / doctor schema-artifact discovery
```

Documentation drift fixed this round (facts only, no history rewrite):

- `README.md` — bootstrap schema table count corrected `7` → `9` (six core tables plus
  `explicit_memories` and the two derived-index sidecars). Code truth: `alpha_bootstrap.sql`
  (6 tables) + three resolved includes.
- `docs/INSTALL.md` — `doctor --static` `packaged_sql` set corrected: **five** artifacts (adds
  `observation_embedding_chunks.sql`) and **three** `include_markers` (adds
  `schema/observation_embedding_chunks.sql`). Code truth: `distribution_cli.py` artifact tuple.
- `docs/GLOBAL-BASELINE.md` — new: the single capability matrix (included / not included, with
  evidence and known limits).
- `CURRENT.md` — this section; and the drain accounting above restated precisely
  (1072 = 20 + 1052) instead of quoting only the ledgered subtotal.

Production was not touched: no wheel install, no gateway/serve restart, no schema mutation, no
backfill. Read-only identity check only: the live `v3core` matches the baseline for all 103
installed `.py` files once CRLF/LF is normalised (103/103), and lacks only the two packages this
merge adds (`runtime_integrity`, `reliability`).

## A02 — DOING (see the A02 section appended by the A02 round)

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
Drain accounting, stated precisely: 1072 rows total = 20 repaired by the first CLI run
(which had no per-row ledger entry; covered by the final census and the global recall
smoke) + 1052 repaired across the four checkpointed batches
(A=20 / B=100 / C=300 / D=632). Source integrity was verified per row for those 1052
ledgered rows: 1052/1052 hashes unchanged (not a sample). `conversation_stream` has no
`embed_model` column, so the fingerprint was proven by re-embedding the canonical slice
(`content[:2000]`) and comparing: cosine >= 0.9999 on every sampled row in every batch.

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

## A02 — query path (DONE)

Two P2 defects fixed on the A01 baseline. Production was not touched (A03 owns the canary).

### P2-1 `SEARCH_CARDS_EMBED_CACHE_INVERTED_CONDITION`

`V3Core.search_cards` no longer pre-checks `query in _EMBED_CACHE`. It attempts the query embedding
on the first call and lets the cache (`cache=True`) do the reuse, so a cold query reaches the
semantic lane. Failure keeps this public path's existing degradation — `ValueError` and
`PrefetchDeadlineExceeded` re-raise, anything else degrades to `q_emb=None` + debug log — the same
policy the prefetch path already followed.

### P2-2 `QUERY_NEEDS_CAP` — one query-only seam

```text
v3core/embed_chunks.py   prepare_query_embedding_text()   representation only
v3core/embedding.py      call_query_embedding()           representation + the ordinary call
```

* `tokens <= safe_token_target(embed_cfg)` → the text is returned **byte-for-byte** (no strip, no
  normalisation, no marker). Hard contract.
* `tokens > target` → head ≈ 60% / tail ≈ 40% of the token budget, cut by tokenizer offsets, joined
  with a single non-semantic separator (`"\n"`), original order preserved, the middle omitted, the
  join re-counted; a deterministic 60/40 shrink repeats until it fits (never a provider 400 probe).
* Token accounting reuses the shared planning helpers (`_get_token_counter`,
  `_encode_field_offsets`, `safe_token_target`) — no second tokenizer, and `7680` is not hardcoded:
  it comes from `max_input_tokens` (8192 default) minus the configured safety margin.
* Budgets are untouched: each caller keeps its own `cache` / `timeout` / `retries` / `deadline`.
* Observability is aggregate only: original tokens, prepared tokens, truncated flag, strategy,
  provider/error class, elapsed — never the query text, the head or the tail.
* Query-only: no source path (messages / conversation_stream / observation / QA / yin / topic /
  explicit memory) can reach it; a test asserts those modules never reference it.

### Coverage: old call site → seam

```text
1980 search_cards                    -> 1985 search_cards (guard removed + seam + degradation)
2287 prefetch                        -> 2299 prefetch
2290 prefetch                        -> 2302 prefetch
2388 prefetch                        -> 2400 prefetch
2392 prefetch                        -> 2404 prefetch
2500 prefetch_to_context_block        -> 2512 prefetch_to_context_block
2503 prefetch_to_context_block        -> 2515 prefetch_to_context_block
2769 prefetch_to_context_block        -> 2781 prefetch_to_context_block
2772 prefetch_to_context_block        -> 2784 prefetch_to_context_block

9/9 covered; `call_embedding(` occurrences left in __init__.py = 0.
Outside the core query path, every query-derived call is capped at 1000 chars by construction
(recall_pool.py 3355/3358, topic_recall.py 467/475/664) — no query path can hand an over-window
payload to the provider.
```

Evidence: `evidence/a02-query-seam-coverage.json`.

### Verification

```text
RED -> GREEN (same tests, assertions unchanged)
  tests/test_a02_query_path_red.py                2 passed   (were 2 failed)
targeted behaviour                                 12 passed
  cold query embeds / hot query 0 extra provider calls / target-1, target unchanged /
  target+1 and 9001-token capped with both ends kept / original never mutated /
  source paths never on the seam / failure degrades / ValueError re-raised
affected query + embedding suites                 121 passed
  embed_chunks_contract, observation_chunks_contract, embedding_write_reliability,
  embedding_policy_guard, embedding_failure_resolution, prefetch_isolation_contract,
  long_observation_writer_recall, qa_failure_accounting, backfill_cli_entrypoint
required CI (local reproduction of product-ci.yml)
  focused-tests-v3-core 191 passed / g5b 102 passed / v3-hermes-plugin 12 passed
  import-smoke PASS, compileall PASS
full suite (regression safety)                    1137 passed / 6 failed / 7 skipped (584.5s)
  the 6 failures are the A01-known set: 3x test_importers_contract (hermes state.db fixture,
  inherited) + 3x test_reliability_cli --help smoke (needs a repo-local .venv; 29 passed with it)
  -> no newly introduced regression
production                                          NOT TOUCHED
```

Q01 status: `READY_FOR_CANARY` — 16 frozen cases (`docs/Q01-QUERY-CANARY.md`,
`src/v3-core/eval/q01_query_canary.py`); wording and expected behaviour frozen before execution, two
off-topic cases carry a frozen response-level rubric for the A03 canary.

B01 status: `READY` — `docs/B01-CORE-INTERFACE-INVENTORY.md` (inventory only; no adapter).
