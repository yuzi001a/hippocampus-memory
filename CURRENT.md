# Hippocampus — global development baseline (A01)

Status: `A01 = DONE` / `A02 = DONE` / `A03 = DONE` (v0.2.2) / `B01 = DONE` (merged, PR #15) / `FRESH_DB_BOOTSTRAP_HOTFIX = DONE` (v0.2.3) / `B02 = DONE` (DSH SUPPORT LEVEL = TOOL, v0.2.8 — see the sections at the end) / `F2 = DONE` (PR #25 merged at `3f6adc6c2e77be983bb7ccfd1851d26ff23bf475`; dedicated matrix, local closeout and repository CI PASS) / `F3 = DONE` (PR #26 merged at `9d8101a4f48e0f2969ee216a11b8458b7ecb1f94`; dedicated installed-wheel RED/GREEN and exact-head repository CI PASS; no production deployment) / `B03 = DONE` (PR #27 merged at `f10847f401c4349bf717120ea90135bbab96904c`; both required exact-head repository CI checks PASS; isolated source-trace fix verified before/after against a real disposable PostgreSQL; `MODEL A→B = OPTIONAL RELEASE SMOKE`, not executed — Y400 has no model credential configured)

Status list: `A01 DONE` / `A02 DONE` / `A03 DONE` / `B01 DONE` / `B02 DONE` / `F2 DONE` / `F3 DONE` / `B03 DONE` / `B04 DONE` (PR #29 merged at `852efa1f0a416609d6be70f141fef16527118d1c`). NEXT: G01 closeout.

Overnight mainline (2026-10-02): F2 DONE. W0–W4 PASS (W4 3/3 cycles); nine guards PASS; zero residuals/sentinel changes; parent readback/hash alignment verified. Local gates and both repository CI checks PASS on fd1da2a; PR #25 merged and remote main read back. F3 DONE: independent installed-wheel fresh RED/GREEN, double-bootstrap idempotency, table/index/doctor and actual failure-writer row readback verified. PR #26 final head ee7954bcc7f6be95e43a94ebf1b1599d8825ab1a passed both required repository CI checks; installed-package tests 15/15 with zero skips and static doctor rc0. Initial packaging failure was reproduced (two overbroad comment-token assertions), minimally corrected without runtime/SQL changes, and retained. Normal merge 9d8101a4f48e0f2969ee216a11b8458b7ecb1f94 and remote main were read back. See docs/F3-FRESH-BOOTSTRAP.md and evidence/f3-fresh-bootstrap/parent-acceptance.json for local evidence scope. F2/F3 runtime frozen; no adjacent-table audit or production deployment. B03 installed-pi API discovery is complete (pi 0.99.2); parent verified actual SDK hooks, native identity timing and existing b01.1 flat HTTP wire fields after the reconnaissance child timed out without an artifact. Locked design: docs/B03-PI-ADAPTER-DESIGN.md. Bounded package implementation and separate acceptance helpers are in progress on feat/b03-pi-auto-memory-adapter. Real model-backed A→B gate is pending deliberate pi authentication on the dedicated laptop (default auth file empty; no API-key environment names at discovery); no production credential reuse and no fake model substitute. Recalled-reference source resolution was a real, reproduced defect and is now fixed and verified: the first isolated run (disposable DB `b03pi_20261002_c`) failed the source trace on the exact emitted id `qa_1` because the v3-core read path resolved only `conversation_stream` ids and never the `qa_<qa_pairs.id>` / `topic_<topic_id>` references the recall engine itself prints; the minimal read-path fix in `src/v3-core/src/v3core/pg_store.py` (plus a `qa_pairs` trace namespace in the acceptance helper, fail-closed) makes the second fresh-root run on disposable DB `b03pi_20261002_d` PASS with every host check green, tracing `qa_1` to its own row without substituting any identifier. B03 is DONE: PR #27 merged at `f10847f401c4349bf717120ea90135bbab96904c` (normal merge; exact head `46e36d1de9f34b89fbd86732e599f72609913225`), with `product-ci (py3.11)` and the Windows distribution-packaging smoke both SUCCESS on that head; remote main was read back and independently confirmed to contain the B03 commits. Real model-driven A→B was **not** executed — Y400's pi auth store is empty and no provider key is present in its environment — so it is recorded as `MODEL A→B = OPTIONAL RELEASE SMOKE`, not a merge gate. Historical failures/recovery retained in the owner-controlled incident archive.







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

---

## A03 — production canary + installable delivery (DONE)

### Artifact (built from a fresh clone, not reused from A01)

| field | value |
|---|---|
| branch | `integration/global-baseline-v1` |
| integration_head | `41a94bc4c9e4838294c4d3745c64d83d0e8297d7` |
| product_code_head | `41a94bc4c9e4838294c4d3745c64d83d0e8297d7` (this commit *is* the query-path fix) |
| artifact_build_head | `41a94bc4c9e4838294c4d3745c64d83d0e8297d7` |
| documentation_head | the evidence commit that carries this section |
| wheel | `v3_core-4.0.0-py3-none-any.whl` sha256 `c13cd4cb39c0c839ee5477b87c1a8d5d5009accb237ec717047052448c02322e` |
| plugin wheel | `v3_hermes_plugin-4.0.0-py3-none-any.whl` sha256 `c705476841deef9ce1944a1f6975adf4b448e95551384acad36fac3f901a3366` |
| members | 130 total / 124 product modules |
| `SOURCE_WHEEL_PRODUCT_DRIFT` | **0** (124 source files vs 124 wheel members, 0 only-in-source, 0 only-in-wheel, 0 differing); 12/12 protected modules byte-identical |

### Live runtime before → after

```text
winning copy           venv/Lib/site-packages (PYTHONPATH precedes .hermes-runtime)
before                 v3_core-4.0.0.dist-info from LONG_OBSERVATION_PRODUCTION_CANDIDATE (108 modules)
after                  A03 candidate wheel (124 modules)
byte identity          124/124 v3core + 2/2 v3hermes identical to the wheel members
installed churn        exactly 4 changed (__init__.py, distribution_cli.py, embed_chunks.py,
                       embedding.py) + 16 new (runtime_integrity/*, reliability/*); 0 only-in-live
editable shadow        none — both editable .pth are .disabled-by-v021-20260918
gateway                3772/5252 (2026-09-22) → 5420/17068 (2026-09-27 08:37:26), old PIDs absent,
                       live-env probe resolves venv site-packages with prepare/call_query/runtime_integrity
readiness fence        2026-09-27 08:38:47 (feishu connected after one transient 30s timeout)
rollback               workspace/backups/a03-canary-20260926/rollback (108+2+10+10 files, readback-verified)
serve                  in-app backend replacement at 09:14:49 (+08) (17280/20244 → 7968/9944) under the
                       same Electron PID; postdates the install, probe resolves the installed bytes
```

### Q01 canary (frozen set, production profile, real provider boundary)

```text
retrieval cases        14/14 PASS   (cold 2, hot 2, historical 4, continuation 2, long 2, short-parity 1,
                                     failure-injection 2 — the failure cases are isolated-contract evidence)
response cases         2/2 PASS by the frozen rubric (off-topic 11/12 answered correctly, no memory
                       fragment presented as the answer)
cold gate              Q01-01 first call: seam input 40 chars -> provider payload 1 call / 27 tokens
hot gate               Q01-02 repeat: provider payloads = 0, identical hit ids
long gate              Q01-07 original 17013 tokens -> provider payload 7678 tokens (<= 7680),
                       strategy head60_tail40, head+tail kept, provider window error = 0
                       Q01-08 original 17037 tokens -> provider payload 7678 tokens, same contract
short parity           Q01-15 provider payload byte-identical to the original query (23 chars)
```

`ISOLATED_FAILURE_CONTRACT`: the two `embedding_failure` cases are proven by the A02 test suite and the
Q01 runner's injection path; **no provider outage was manufactured in production**.

### Write → new session

```text
session 1 (real entry, hermes -z)   wrote the canary fact through v3_store
PG                                  explicit_memories mem_dfef128b…, category memory, embedding not NULL,
                                    status active, created 2026-09-27 00:51:17Z (post-cutover)
session 2 (brand-new session)       asked in natural language; answered
                                    "统一基线 integration/global-baseline-v1 / Canary ID A03-CANARY-20260927-1"
```

### Health gate

```text
qa_pairs                     14046 rows / 0 unexplained NULL embeddings
observation_notes            788 rows / 0 NULL
post-cutoff writes           qa_pairs +2 (0 NULL), explicit_memories +1 (0 NULL)
embedding_failures           0 rows (no ledger growth)
gateway/serve                gateway healthy + fence; Desktop serve backend replaced in-app at
                             09:14:49 (+08) under the same Electron PID — after the install, so the
                             session path resolves the installed bytes (probe rc=0 on all four live
                             processes; replacement trigger NOT PROVEN, no stall precursor)
pre-existing, out of scope   rerank HTTP 400 (siliconflow) first seen 2026-09-23, before this cutover
                             historical tracebacks in gateway.log (2026-08-30) — not this window
```

### Known limitations carried forward

```text
RecallTrace per-lane summaries stay empty on the Core query path (direct recall_pool call) — recorded,
  not fixed here; Q01 relies on provider payload sizes / hit ids / raise status instead
rerank HTTP 400 (siliconflow rerank endpoint) — pre-existing production config issue, out of A02 scope
reliability CLI --help smoke tests still assume a repo-local .venv
```

### Delivery (closed)

```text
PR                       #12 → main, required checks green (product-ci 29s, distribution-packaging
                         smoke 1m9s), normal merge, no force push
merge commit             83fc337a5ecb592a8be90d99b410404ebacb2f7f  (2026-09-27T01:05:31Z)
tag / release            v0.2.2 → release workflow run 36284508666 (success) → "Hippocampus v0.2.2"
                         (Latest) with 4 assets: v3_core wheel+sdist, v3_hermes_plugin wheel+sdist
release wheel vs canary  members 130/130 byte-identical (CRC mismatch 0, header metadata 130) — the
                         published wheel contains exactly the code the canary validated; only zip
                         timestamps differ, i.e. a repackaging, not a code change
install entry (verified) fresh venv + install from the released assets: uv pip check "37 packages,
                         all compatible"; probe shows prepare_query_embedding_text /
                         call_query_embedding / runtime_integrity all present; console entries
                         `hippocampus --help` and `v3-core --help` both rc=0
packaged version string  still 4.0.0 (tag convention v0.2.x, matching v0.2 / v0.2.1)
deployment record        evidence/a03-deployment.json
```

---

## B01 — minimal host contract for DSH (B02) and pi (B03)

```text
branch            feature/b01-host-contract (from main bbcc31b)
baseline          main bbcc31b6e571669dfc1659dc2bcd9a0057aca45c
scope             G1 durable event identity + idempotent ingest   (correctness)
                  G2 bridge version / capability handshake
                  G3 minimal owned-process lifecycle
                  G4 host/session/project/event identity contract
NOT in scope      query algorithm / ranking / daily dedupe / adapter / schema / backfill / M01 / CRL
production        NOT touched (no install, no restart; B02's DSH hookup is the first external canary)
```

### Corrected G1 fact model

```text
EVENT_INGEST_CORRECTNESS_DEPENDS_ON_EPHEMERAL_PROCESS_STATE
```

Read from the code, not assumed:

| layer | mechanism found | truth lives in | defect |
| --- | --- | --- | --- |
| source dedupe | `LiveBuffer.enqueue` → disk durable marker + `j/accepted_live_buffer/<sha>.json` tombstone | disk (restart-proof) | hit returns `True`, indistinguishable from a fresh accept |
| source cursor | `context.synced_message_ids` | **process memory** | empty on a fresh Core → a re-sent snapshot looks new |
| derived identity | `qa_pairs.source_id UNIQUE` + `ON CONFLICT DO NOTHING` | PG | `source_id` embedded `q_turn = context.turn` — an **in-memory counter that resets to 0** |
| pairing state | `context.pending_qa` | **process memory** | lost on restart |
| transport ACK | `/events` always `{"ok": true}`; `sync_turn` returned `None` | — | structurally unable to tell the truth |

Post-restart duplicate chain: fresh Core → cursor empty + turn 0 → re-sent event → tombstone hit
returns True (no block) → pairing runs again → flush builds a **new** `source_id` → `ON CONFLICT`
misses → duplicate QA row.

### What changed (no new table: schema NONE)

```text
new               src/v3core/bridge_contract.py — single source of truth for
                  normalize_event / event_identity / qa_pairing_decision /
                  ack_from_receipt / get_bridge_capabilities (protocol b01.1)
ingest.py         `event_status(session,event,host,item)` → absent|pending|durable; host-scoped
                  accepted-tombstone path (legacy template byte-identical); host in the job
                  identity only for non-legacy hosts
sync_turn         returns a per-event receipt; `duplicate` (pending OR durable) ⇒ SKIP QA
                  derivation — the actual fix for post-restart duplicates; turn-aware pairing
                  via qa_pairing_decision (hold instead of guessing); host-scoped in-process
                  cursor; `_qa_source_id()` single template (host-namespaced for new hosts)
serve.py          /events: canonical identity + honest ACK (accepted/duplicate/retryable/failed
                  + event_id/source_id/host); write failure ⇒ 503 + ok=false, never "remembered";
                  bounded buffer (32 sessions / 500 events / 6h TTL) — no longer a correctness
                  authority; /health carries the shared handshake keys additively
serve lifecycle   --port 0 reports the REAL bound port; --ready-json prints one deterministic
                  ready event; SIGTERM → graceful (stop accepting → core.shutdown())
mcp_server.py     announces the same capability truth at startup (no second hardcoded copy);
                  stdio disconnect → core.shutdown() (existing behaviour, now asserted)
tools/health.py   v3_health carries the same `bridge` block
```

### Isolated real E2E (`src/v3-core/eval/b01_host_contract_e2e.py` → `evidence/b01-e2e-report.json`)

Disposable `pgvector/pgvector:pg17` container (127.0.0.1:55501), isolated profile,
observer disabled, real `serve --port 0 --ready-json`, real HTTP.

```text
ready-json real port       62864 (not 0)
single event               accepted=true  duplicate=false
same-process duplicate     accepted=false duplicate=true   identity rows unchanged
restart duplicate          accepted=false duplicate=true   identity rows + QA rows unchanged
same ids / other host      accepted=true                   (no collision)
missing assistant          turn2 recorded incomplete (empty answer), never paired with turn3
out-of-order assistant     source kept, pairing not guessed
writer rejection           HTTP 503 + ok=false + status=retryable
legacy payload (no host)   200 + host=legacy               (old contract intact)
prefetch                   hit=true (synthesized history retrievable)
```

### Tests

```text
B01 targeted        tests/test_b01_event_contract_red.py + ..._behavior.py → 36 passed
                    (RED first: 21 failed before implementation)
related suites      alpha_bootstrap / harness_filesystem_isolation / config_pg_password_env → 82 passed
full suite          see the delivery block below
NOT re-run          embedding audit / LoCoMo / historical repair / A01 differential
```

### Findings (pre-existing, NOT fixed here — see docs/B01-FINDINGS.md)

```text
F1  packaged `hippocampus bootstrap` fails on a BRAND-NEW database:
    alpha_bootstrap.sql:139 splices observation_embedding_chunks.sql (FK → observation_notes)
    BEFORE alpha_bootstrap.sql:269 creates observation_notes → UndefinedTable → rollback.
    The E2E applied the same artifacts in dependency order and recorded the fallback.
F2  outbox replay is at-least-once: a crash before the PG ack replays the item on the next
    start and conversation_stream can receive a second row for one identity (no unique key on
    that table). B01 does not change it; it makes the duplicate *detectable* and keeps the
    derived layer idempotent (E2E asserts qa_pairs does not grow).
```

## FRESH_DB_BOOTSTRAP_HOTFIX — fresh install unblocked (v0.2.3)

```text
classification   PRODUCT_DEFECT / P1-INSTALL-BLOCKER
code             FRESH_DB_BOOTSTRAP_DEPENDENCY_ORDER
symptom          fresh empty PG + installed wheel + `hippocampus bootstrap`
                 → UndefinedTable('relation "public.observation_notes" does not exist')
                 → transaction rollback → NO schema at all on a brand-new install
root cause       alpha_bootstrap.sql inlined observation_embedding_chunks.sql (FK →
                 observation_notes) at the qa-sidecar marker position, i.e. BEFORE
                 observation_notes was created in the same file; bootstrap splices
                 includes in place, so the FK target did not exist yet
fix              marker moved 139 → 299 (observation_notes @ 269) in BOTH copies
                 (repo-root canonical + packaged src/v3core/schema/); ordering only
RED              evidence/fresh-bootstrap-e2e.red.json  (rc=1, undefinedtable=true)
GREEN            evidence/fresh-bootstrap-e2e.json      (rc=0, PACKAGED_FRESH_BOOTSTRAP=PASS)
idempotency      bootstrap #2 rc=0, tables stable, indexes stable, schema_versions=1
guard            tests/test_bootstrap_dependency_order.py — FK creation order of the
                 EXPANDED packaged SQL (RED 2 failed → GREEN 7 passed), no PG required
B01 E2E          packaged_bootstrap_ok=true, fallback_used=false (fallback now FAILS the run
                 if it is ever needed again; the historical note is kept in the findings doc)
schema           NONE
production       NOT touched — no install, no restart, no schema mutation
release          v0.2.3 patch tag (package version stays 4.0.0: test_distribution_packaging
                 freezes it as the Gate 0/1 contract)
release asset    verified: v0.2.3 wheel downloaded from the public release →
                 clean venv outside the repo → fresh empty PG → bootstrap #1 rc=0,
                 FK present, bootstrap #2 rc=0 (10/11 tables; only F3's
                 embedding_failures absent)
                 evidence/fresh-bootstrap-e2e.release-v0.2.3.json
open findings    F3 embedding_failures.sql packaged but applied by nothing (fresh install has
                    no public.embedding_failures; doctor does not require it)
                 F4 the two schema copies are CRLF/LF-divergent while INSTALL.md §8 claims
                    they are byte-identical
                 F2 outbox replay at-least-once on conversation_stream → B03_PRECONDITION
                    (re-evaluate before pi starts automatic event collection)
next             B02 = DOING — DSH SUPPORT LEVEL = TOOL via `v3-core mcp` stdio

## B02 public clean-install canary (2026-09-27)

- Public release: `v0.2.4`; clean venv installed `v3_core-4.0.0-py3-none-any.whl`
  and automatically resolved `mcp==1.30.0`.
- Clean PostgreSQL+pgvector database: bootstrap #1 and #2 passed from the
  installed wheel; no fallback path; database `b02public` was disposable.
- DSH locked line: `@deepseek-ai/dsh@0.1.0-rc.6`, headless bundle and official
  `@deepseek-ai/dsh-mcp-client` both `0.1.0-rc.6`.
- Real DSH session A: explicit `v3_store` returned `durable=true`,
  `durable_store=explicit_memories`; PG readback found one active memory.
- Real new DSH session B: `v3_search` found that memory through the keyword
  fallback, then `v3_get(target=hm)` read the exact content from
  `explicit_memories`; session C `v3_get(target=status)` returned PG connected
  with no missing tables.
- Known blocker, not hidden: configured SiliconFlow embedding returned HTTP 401
  `Token is invalid`. The write was durable with `DERIVED_WARNING`, and the
  search result had `cosine=0.0`; vector recall is therefore **not** claimed
  healthy. Full evidence: `evidence/b02-dsh-tool-e2e.json`.
- Production: no install, restart, schema mutation, or data write.

next             B03 remains blocked on re-evaluating `OUTBOX_REPLAY_SOURCE_AT_LEAST_ONCE` before pi automatic event collection.

## B02 embedding-401 round → B02 = DONE (2026-09-28)

The single B02 blocker in the section above is closed, and closing it surfaced three
real defects on the same seam. Chain: `v0.2.6` → `v0.2.7` → `v0.2.8`
(main `7a59e5d` → `8ebc431` → `d0b8904`).

**401 classification (not a guess).** The provider rejected a request that carried no
valid token: MCP-child log `embedding HTTP 401 … code 30014 Token is invalid`, same
endpoint/model as a healthy direct call. Root cause: the canary profile had no
*resolvable* embedding credential — the engine reads `api_key` from config or
`${env:V3CORE_EMBED_API_KEY}` (process env or the profile's own `.env`), and the old
harness exported `SILICONFLOW_API_KEY`, a name the package never reads. Direct leg with
the profile's real credential: HTTP 2xx, `dims=1024`. Controlled reproduction (credential
emptied): the identical 401/code 30014 signature.

**Fixed on this path.**
- `v0.2.6` (`7a59e5d`): MCP/CLI entry points never read `HERMES_HOME`, so a fresh
  install's profile was invisible to the MCP child — 13 tools listed, first call failed
  with `V3CORE_PG_PASSWORD not set`.
- `v0.2.7` (`8ebc431`): core-internal leaves resolved the **default** profile — a
  process booted on a non-default profile reported `_get_pg_conn()` against another
  install's database (`topics=349`) and cached that foreign topic matrix locally.
  Verified before/after: `5433/v3embeddings@349` → `55521/v3embeddings_alpha@0`.
  Production untouched (349 topics unchanged, no canary rows).
- `v0.2.8` (`d0b8904`): `v3_search` → `search_cards` skipped the active-memory vector
  lane, so a reworded question returned 0 hits.

**Final acceptance on the public `v0.2.8` asset (fresh venv, fresh pgvector container
55523, `hippocampus install` = INSTALL OK with its own write+readback+recall smoke).**
- Session A `v3_store`: `durable=true`, `DURABLE_COMMITTED`, `warnings=[]`; DB readback
  `embedding IS NOT NULL`, `vector_dims=1024`.
- Session B, new DSH session, reworded query with no substring overlap:
  `v3_search` → 4 hits, top = the stored record `cosine 0.5324` (vector lane);
  `v3_get(target=hm)` read the full source; the answer used it.
- Session C, DSH + MCP child fully restarted, different question: the record was
  retrieved and read again (`v3_get(hm)`), answer "Thursday 09:30 Beijing time".
- Honesty: with the credential removed, a store returns `durable=true` **and**
  `status=DERIVED_WARNING` + `warnings=[… class=EMBEDDING_AUTH_FAILED … 401 …]`.
- Profile leak: none — the non-default profile read its own database throughout.

**G01 delivered with B02:** `docs/B02-DSH-TOOL-SUPPORT.md` (tested versions, exact MCP
config, verified transcript, limitation list), README host-adapter capability matrix,
and `docs/KNOWN-LIMITATIONS.md` §1.7.

**Carried forward (explicit, not hidden):**
- `F2 OUTBOX_REPLAY_SOURCE_AT_LEAST_ONCE` untouched — still `B03_PRECONDITION`.
- `topic_recall: no data source (PG failed, SQLite not found)` on fresh installs is a
  benign warning; the topic lane is degraded, record lanes unaffected. Follow-up.
- A raw stdio JSON-RPC probe of the MCP child (this round's scratch harness, not the
  DSH path) died mid-call with `exit_code=143` and no traceback while the same call
  succeeds in-process and through DSH. Unreproduced through the official client;
  recorded as an open anomaly with evidence paths under
  `workspace/dsh-b02-canary/sp*_child.stderr.txt`.

---

## F2 — canonical source idempotency (DONE — PR #25)

The current tracked harness is `src/v3-core/eval/f2_source_idempotency_e2e.py`.
Latest valid matrix: f2-05, dedicated Windows laptop; W0–W4 PASS, W4 three
cycles, one canonical source row per identity, no duplicate QA, transport
converged. All nine required guards PASS; production sentinel changed=0,
owned residuals=0; provider calls=0 (record-only).

Frozen candidate: 338 files, ZIP SHA256
`0ea0937c80bdd2e104cc9667d59b0ac72062f6ed40c1cb2a3f26b52566abbe6f`.
Parent raw readback verified the real W2 torn-marker state and all three W4
HTTP recovery ACKs. Real-PG focused gate: 242 passed, zero failures/skips. Parent CI-shaped gate:
428 core passed / 5 intentional real-PG skips (covered on the laptop),
102 evaluator passed, 12 plugin passed, compileall and diff-check PASS.
Parent post-exit scan: no residuals, no frozen-byte drift, config absent.
Both required repository checks PASS on fd1da2a0032b93fd7a69ac6cf4395eb1a9c59feb. PR #25 MERGED as 3f6adc6c2e77be983bb7ccfd1851d26ff23bf475; remote main ancestry/readback verified. No production install,
restart or destructive test was performed in this development run. Historical
isolation incidents were preserved and recovered separately; they are not
rewritten into a claim that the earlier investigation had no production contact.

Details: `docs/F2-SOURCE-IDEMPOTENCY.md`.
Current: F3 DONE (PR #26 merged at 9d8101a4f48e0f2969ee216a11b8458b7ecb1f94 after exact-head CI PASS); B03 pi API discovery IN_PROGRESS on feat/b03-pi-auto-memory-adapter. F2/F3 source/runtime remains frozen.

---

## B04 — DSH automatic memory adapter (DONE — PR #29)

Merged: PR #29 → `main` = `852efa1f0a416609d6be70f141fef16527118d1c` (2026-10-02T14:14:11Z), normal
merge, no force push. Both required repository checks PASS on the final head
`25c5d0ac08a87473088b2c0f586a57fe61ef6ae8`. Remote `main` read back: the B04 head is an ancestor,
`packages/dsh-adapter/` is present, and this section is in `main`.

```text
branch            feat/b04-dsh-auto-memory-adapter (from origin/main 8c576b39023efe09ee3312b75b76f4efb2b96eb7)
baseline upstream @deepseek-ai/dsh@0.2.0-rc.2, SHA 639ed015397290b3745d163aafe02ffee4aa3f84
package           packages/dsh-adapter/ — zero npm dependencies, Node built-ins only, no build step
design            docs/B04-DSH-ADAPTER-DESIGN.md
integration       18/18 checks PASS, 0 failures — evidence/b04-dsh-integration-20261002/
production        NOT touched (no install, no restart, no schema mutation, no data write)
```

Locked from the upstream sources, not from the previously installed host (the local install is
`0.1.0-rc.6`; the contract is read from `0.2.0-rc.2`): `agent/pre-step` is an awaitable waterfall
whose `payload.messages` are the messages claimed by the current step, and whose returned messages
enter the *same* model request (`packages/core/agent-loop/src/agent.ts:267-282`, `419-425`; contract
test `packages/core/agent-loop/tests/interception.spec.ts:196-221`). The injected message is a
producer-owned `kind: 'hippocampus'` + `form: 'recall'` user message — `kind: 'plugin'` is the older
v3 form and is already migrated by `session-format-v3-to-v4`. Capture listens on `session/event`,
records only durable `user/message` (with `source.kind === 'user'`) and `assistant/message`, and keeps
native identity (`event_id` = the DSH `MessageId`).

Self-recapture exclusion is structural, not heuristic: the injected message is itself persisted as a
`user/message`, so the listener skips any event whose `source.kind === 'hippocampus'`. Verified in the
run: the injected text appears 0 times among ingested sources, before and after a session reload.

The isolated integration run composed a real DSH CLI + this adapter + a real B01 bridge + a
disposable PostgreSQL, with the model endpoint replaced by a local credential-free stub and
embeddings disabled (keyword-only). It proves: the profile really mounts the row (`--dump-config`),
one recalled block reached the model request of the same turn (411 chars carrying the seeded fact),
the durable user/assistant messages were captured with native event ids, the injected message was not
recaptured, reloading the session produced no duplicate source, and a fresh turn completed with the
bridge process killed and nothing injected (fail-open). **Not covered, and not claimed:** a live model
provider, a live embedding endpoint, and every failure class except bridge-down (unit-covered).

Local unit layer: `cd packages/dsh-adapter && node --test "tests/*.test.mjs"` → 39 tests pass.

## M01 / M02 / M03 — v1.1 explicit-memory correction (2026-10-03)

Status: `M01 = DONE`, `M02 = DONE`, `M03 = DONE`.

- **M01/M02** (explicit-memory correction + unified reads): merged to `main` via PR #37
  (`23865f74`). That pair's acceptance was closed on the branch that was merged; it is not
  re-opened here.
- **M03** (correction propagation into derived memory): branch
  `feature/m03-derived-correction-propagation`, based on `main` `23865f74`.
  - previous real-PG run = **FAIL 134/136** — `evidence/m03-derived-propagation-20261003/`
    holds the PRE-FIX FAILED RUN unchanged (2 failures on the injected-`PgPool` path).
  - the 3 P0 real-PG defects that run found: (A) `int8range @> %s` without a `::bigint`
    cast; (B) a real `PgPool` routed into the `pg` slot of `_acquire_lease`, so suppression
    failed OPEN; (C) the invalidation `INSERT` listed 12 columns but 15 VALUES expressions.
  - 3 P0 fixes implemented and pushed: commit `0430382` on the branch (with
    `tests/test_m03_sql_arity_and_lease_routing.py` pinning A and C in CI).
  - **final real-PG revalidation PASSED** — Y400 (`DESKTOP-EQP3OBU`), same disposable-PG
    setup and the same 136-check driver: `136 checks / 136 pass / 0 fail / VERDICT=PASS /
    exit 0`, `production mutation = NONE`, report
    `evidence/m03-derived-propagation-20261003/m03-report-20261004_031923.json`.
  - the 14 assertions that had encoded the pre-fix defects were rewritten to express the
    locked M03 contract instead of the defect (per-assertion classification in
    `evidence/m03-derived-propagation-20261003/m03-assertion-contract-classification.md`);
    check count stayed 136, and no assertion was removed, skipped or weakened. Two of them
    (`fixture_propagation_wrote_the_sidecar_through_the_real_writer`,
    `s30_fixture_propagation_needs_no_provider_at_all`) were recorded as
    `DRIVER_EXPECTATION_BUG`: they assumed the real propagation would fail, so with A fixed
    the fixture call became a replay.

## P0-C2 → P0-B status overlay (2026-10-11)

Scope: branch `fix/p0b-recall-correctness`. This overlay does not alter the merged B04
baseline above. The B5/B6 edits listed here are still uncommitted; the M03 fix below is committed
and pushed (PR #44) and is listed separately.

### M03 correction propagation — chain-tail fix (2026-10-17)

Status: `COMMITTED + CI GREEN + REAL-MODEL CANARY PASS`. Commits on
`fix/p0b-recall-correctness`: `61842cfc2ec42ab66176cca0466604491d0dd02e` ("fix(m03): invalidate the
live rebuilt artifact on a later correction", 2 files, +128/-3) and
`3f3c25ade552b6b3d66899ca52ea3dff5e5ed4fa` ("fix(p0c): durable metadata-only boundary + M03 lease
routing for deadline-wrapped pools", 6 files, +121/-12), both on top of
`883dc31b028f5993ca00e7f64d8f3f7a647ec442`. PR
https://github.com/yuzi001a/hippocampus-memory/pull/44 — `product-ci (py3.11)` and
`windows / distribution-packaging smoke` pass on `3f3c25a`.

- **Defect.** `_discover` resolved derived topics only through `topic_entries.source_qa_id`. After
  a rebuild recorded `replacement_derived_id = D2` on D1's row, a later correction still resolved to
  the already superseded `D1`: `D1` was re-invalidated while `D2` — the artifact carrying the
  superseded derivation and actually in circulation — kept being injected as the current
  conclusion. Observed in the isolated lifecycle as the B→C correction invalidating `T1` instead of
  the rebuilt `T2`.
- **Fix.** `_discover` walks each discovered id forward to the tail of its replacement chain
  (read-only, bounded at `_REPLACEMENT_CHAIN_MAX_HOPS = 16`, cycle-safe, same kind only). Hops
  recorded by the correction being replayed are not followed, so replay stays idempotent. An id
  with no replacement is its own tail, so a first correction resolves exactly as before.
  `discover_affected` keeps its locked public signature
  (`test_M03_public_api_is_locked`).
- **Evidence.**
  - Isolated full lifecycle on Y400 / disposable PG 55433: `result = PASS`,
    `lifecycle_gaps = []`, 10/10 consumer checks, `propagation_replay.idempotent = true`,
    report `p0b_m03_lifecycle_run_20261017_r12.json`
    (remote SHA-256 `F00F179B2DB5E3E1BDB1532228C80020847A424C339303CCD64ECEF6772013B5`).
  - `tests/test_m03_derived_invalidation.py` on Y400: 50 passed.
  - The same suite on a clean `git archive` of `61842cf` (no uncommitted files):
    50 passed — the commit is self-contained.
  - RED check against the pre-fix module (`671b1b5a…`, CRLF-normalized, matching
    `p0b-current-full-20261011/source-manifest.json`): the new regression fails with
    `assert ['t_d1'] == ['t_d2']`.
  - Full suite on the clean `61842cf` checkout: `20 failed, 2292 passed, 13 skipped in 642.00s`.
    The failing-id set is **identical** to the known baseline (20/20), zero new regressions.
  - **The lifecycle proof is mock-based** (deterministic local embedding + local chat mock). It is
    not a real-model result. Y400 reaches `api.siliconflow.cn:443` and `api.minimaxi.com:443`, so a
    real-model canary is feasible; it was not executed in this round.

#### Two further defects fixed in the same PR

- **Metadata-only async boundary was dropped before the durable write.** `sync_turn()` skipped
  any event with empty content *before* reaching the `host_event:ASYNC_BOUNDARY` /
  `host_event:NEW_ROOT` durable-source branch. The boundary row carries no text by design (P0-C2
  §5) and is the delivery trigger — the only row the F2 identity
  `(host, session_id, trigger_event_id)` can resolve — so an empty-content boundary left the
  autonomous delivery unrecoverable from source. Boundary kinds are now exempt from the
  empty-content skip. Regression
  `tests/test_p0c1_host_event_semantics.py::test_c2_metadata_only_boundary_with_empty_content_is_durable_source`
  (RED on the previous HEAD: `assert 0 == 1`; GREEN here).
- **M03 lease routing crashed on a deadline-wrapped pool.** `_lease_slots()` routed a
  `DeadlinePoolView` into the `pg` slot, so `.cursor()` raised whenever the M03 derived
  invalidation/rebuild plumbing received the observer's wrapped pool (`injector.py` wraps
  `PgPool` in `DeadlinePoolView`). A view over a pool now keeps the pool-slot lease contract.
  Regression
  `tests/test_m03_sql_arity_and_lease_routing.py::test_M03_lease_slots_routes_deadline_pool_view_as_pool_and_yields_dbapi_connection`.

#### Acceptance reclassification (product boundary)

`docs/ARCHITECTURE-OVERVIEW.md` § 3.1 now states the boundary this follows from: Hippocampus is
memory infrastructure (store / organise / make retrievable), the host agent owns understanding and
judgement, and a summarising/compressing model must never gate which raw facts reach a future
model. Applied to the canary:

- **Product gates** (memory infrastructure): raw-record preservation, source identity, version
  relations, current-vs-history read consistency, derived invalidation + suppression, correction
  idempotency, and which version the product exposes as *current*.
- **Answer quality** (model behaviour) is recorded in a separate `answer_quality` section and is
  **never** a pass/fail gate: a model that also mentions the historical value after a correction is
  describing history that is legitimately still recallable.
- No product assertion was removed; the M03 derived-invalidation contract is asserted as before and
  was not loosened.

#### Real-model canary (Y400, disposable PG 55433) — PASS

Runner `p0b_integrated_canary_runner_20261017_r2.py`, SHA-256
`aaf2194979ae5448d66457fd0d623dce54ae58b1cfaef63dbd6a45c39f9176dd`; source root
`C:\hp-testbed\p0b-commit-3f3c25a-20261017` extracted from `git archive` of `3f3c25a`
(`__init__.py` sha256 `2ec3a076b4c543d783172b2b75bfa8f1bb1fef57e33db7be751f89ba43226ee1` = the
committed blob; `core.autocrlf=true` normalises line endings, content identical after CRLF
stripping). Report
`C:\hp-testbed\p0b-integrated-canary-20261017-r2\reports\p0b_integrated_canary_01950b3d4a4b.json`,
SHA-256 `4699091c48feaf35da3de428b4a3873d8c33850fc16b05e69753b0316959a7c8`, run_id `01950b3d4a4b`,
`result = PASS`, `failures = []`, 24 gates, 0 gate failures.

- `P0-C2 = PASS`: metadata-only boundary persisted as durable source, delivery
  `source_id = delivery/.../evt-...-auto-boundary`, `event_ids` complete, recallable in the source
  session and in a **new** session, `synthetic_qa_rows = []`.
- `P0-B = PASS`: real SiliconFlow rerank (`index_mapping_ok = true`, relevant doc at index 1, score
  `0.9963`), real MiniMax-M3 answer used and cited the delivery fact.
- `M03 = PASS` on all product gates: `supersedes`/`user_explicit` relation A→B and B→C; version
  chain `A archived / B archived / C active`; one `topic` invalidation row per correction with
  `topic_invalidated = true`; the suppression set read from `pg` (not degraded) contains the
  invalidated topic; an identical A→B replay is `deduplicated` with `1 → 1` invalidations and
  stable `correction_id` / `relation_id` / `new_memory_id`; `hm_get mode=current` resolves A→B and
  B→C to the replacement while `mode=history` still returns the original record.
- `withdraw`: `current` before the withdrawal returned the secret; after it,
  `{"content": null, "is_current": false, "memory": null}`; `history` still returns the original
  content and its edge; one `topic` invalidation row (`reason = withdraws`).
- **Isolation**: each run owns `ROOT/runs/<run_id>` as its file-state root; no injected block
  contained a token from any earlier run (`a35333d06365`, `76fbc5fd0317`, `a1473fd5906a`).
- **Answer quality (metric only, 5 real MiniMax-M3 calls, all HTTP 200)**: after A→B the model
  stated the current value (production) and also mentioned the historical one; after B→C it stated
  `canary`; in the combined scenario it stated `canary`; after the withdrawal it quoted the
  withdrawn secret from the `[qa]` channel while `[主动记忆]` no longer offered it, and reported the
  two channels as conflicting. That is the raw-history channel behaving as designed under the
  boundary above, not a product correctness failure.
- **Known limitation, not fixed here** (out of this round's scope): the `[qa]` recall channel has
  no derived-invalidation suppression, so a corrected or withdrawn value can still be recalled from
  raw history. Per the boundary, filtering raw history is an agent-side judgement; a QA-level
  invalidation filter is a separate change and must not be smuggled into M03.

#### Previous canary round (kept as evidence, not overwritten)

`canary_r13.json` — report `p0b_integrated_canary_a1473fd5906a.json`, SHA-256
`c0b4912331b83dbbff927541e0ece7c8113b2d037b6fbf0bfb787f0b2e69477b`, `result = FAIL` with
`M03_A_TO_B_SUPPRESSION_NOT_CONSULTED`, `M03_A_TO_B_NOT_IDEMPOTENT`,
`COMBINED_CURRENT_VERSION_NOT_C`, `COMBINED_HISTORY_A_NOT_PRESERVED`. All four were canary-side
defects, not product defects, and were fixed in the harness: the suppression read was pointed at
`core.pg_pool` (None) instead of `core.pg`; a deduplicated correction returns no `derived_receipt`,
so the readback fell through to an invented event id; and the `COMBINED_*` gates were evaluated
before they existed, so "not yet measured" became a permanent failure. The product-side values
behind those four names were correct in that run too.

### B5 / B6 (unchanged, still uncommitted)

Branch edits that remain in the working tree and are **not** part of PR #44: `recall_pool.py`
(B5 global-decay removal) only. The M03 `DeadlinePoolView` lease routing
(`active_memory_store.py` + `tests/test_m03_sql_arity_and_lease_routing.py`) and
`docs/P0C2-AUTONOMOUS-DELIVERY.md` are now committed in `3f3c25a`. The B5/B6 findings below were
measured on `883dc31b…` and still describe that revision.

- **B5 — `B5_NO_CHANGE_TEMPORAL_RANKING`.** The frozen analysis
  `p0b5_no_change_report_20261010.json` records temporal gate `false`, non-temporal
  no-regression gate `true`, and both gates `false`. Its baseline SHA
  `3a6aec0eade6e0ea139c43c294b034a416107d834f6c5171b91370ecf32feba3` matches the frozen B4
  full-seed artifact. Of 20 temporal gold pairs, 6 hit, 7 are fusion misses, and 7 are
  admission misses (not recoverable by reranking). Lifting any reachable fusion miss needs at
  least `0.006684`, versus a median rank-10/11 gap of `0.000597` (11.2×). No temporal-ranking
  stage ships. The non-QA global-decay removal remains a separate correctness fix; measured
  baseline applications were zero.
- **Full exact rerun, seed 0 — baseline reproducibility only, not a B5 candidate test.** Report:
  `C:\Users\servi\AppData\Local\hermes\cache\scratch\p0b5_exact_attribution_20261011_b5fullseed0.json`.
  Scope: 28 queries / 60 target pairs (synthetic 12/28, Astra 12/28, self-source 4/4); two
  isolated DB clones × two repeats per dataset. Results matched across clones/repeats within
  this process; retrieval made zero provider calls and no DB writes. Cross-process repeatability
  was not measured by this run.
- **B6 — automatic-injection path exercised with a controlled host shim.** A Y400 regression
  exposed `DeadlinePoolView` being routed as a direct PG store instead of a pool; observer-note
  suppression then failed open (`PgLease` had no `cursor`). `_lease_slots` now treats
  `DeadlinePoolView` as a pool. The focused M03 lease-routing file passed **10/10** on Y400.
- B6 wire-level report:
  C:/hp-testbed/p0b5-b6-final-20261011/reports/p0b6_real_auto_injection_20261011.json

  (SHA-256 `80b27b8223f06b7a48a74f2a4d72704732d388fa927206e1e65abdf9246b88f6`). Four required
  synthetic scenarios (current status, historical stable fact, current owner, cumulative
  progress) placed their expected markers in the final API wire content. The production
  provider, core, injector, memory manager, turn-context sidecar, and wire-copy stages were
  each observed five times. DB `p0b6_auto_injection_20261011`, port `55433`, read-only;
  deterministic fixture query embeddings, no model/API call. Report status:
  `PASS_WITH_INTEGRATION_GAP`.
- **B6 current evidence / remaining gates:** the previous controlled-host run proved the four required markers entered the API wire (`PASS_WITH_INTEGRATION_GAP`; report SHA-256 `80b27b8223f06b7a48a74f2a4d72704732d388fa927206e1e65abdf9246b88f6`). A second controlled-host/turn-context-shim run proved the assistant-delivery marker enters the final wire for an exact event-key query; `PASS`, 1/1 marker, all provider → V3Core → injector → Hermes turn-context → wire stages observed, DB `p0b6_auto_injection_20261011` on port 55433 with read-only transactions. Report: `C:/hp-testbed/p0b5-b6-final-20261011/reports/p0b6_delivery_exact_key_20261011.json`, SHA-256 `8f0cdfe77f35dbde6b7bd7d3050d1c63eddd29199f1ab3f367c6eb49ca4ce1e1`. The natural-language delivery diagnostic remains a retrieval-quality miss, not an injection-path failure: lexical helper found the delivery row (1 candidate, source ID present, 4 matched terms), but `recall_pool` returned 8 hits; the delivery was raw keyword rank 4, fused position 12 with contribution `0.0078125`, then dropped `OUTSIDE_LIMIT`. Report SHA-256 `0d3aa53297fd6d05e2c767aa38618cc9132636e84007588223a2d782baf98347`. Do not change ranking weights to make this diagnostic pass without a separately approved gate. The project design resolves scope: assistant deliveries are derived/rebuildable and retrievable through the existing lexical path; no synthetic human question is allowed. Actual one-shot Hermes Agent v0.21.3 CLI is now exercised in an isolated profile: five required markers reached the final API wire and each required `api_content` sidecar hash matches that wire message. The report is `C:/hp-testbed/p0b5-b6-final-20261011/reports/p0b6_real_hermes_cli_api_wire_final_20261011.json` (SHA-256 `4a1166788f5f47025bf249cd450e235caddfe4b8fe02b503bd14d81cc4d4e30f`). This used a loopback-only allowlist proxy, local deterministic chat/embedding mocks, and a disclosed test-only character tokenizer for the synthetic embedding model; no external model API was called. All 24 chat and 31 embedding requests were loopback; proxy blocked 0 non-local requests; DB port 55433 stayed read-only with fixture counts unchanged. The exact-key assistant-delivery case passed; the natural-language diagnostic still misses its marker (rank 12, final limit 8), retained as a non-gate retrieval-quality gap. The real invalidation-row mutation/rebuild lifecycle is still unverified.
- **Current-source full suite:** Y400 snapshot had 344 source files hash-verified; `pytest exit=1`, **2289 passed / 20 failed / 14 skipped** in `659.41s`. This is a red full-suite result, not a pass. Log `C:/hp-testbed/p0b-current-full-20261011/results/pytest_full_20261011.log`, SHA-256 `ba1209509b275c0dfaefb2a2267c325f7ae9891ea672d365ae63f8606381f107`.
- **Failure attribution:** replayed the exact 20 failed test IDs on a clean detached checkout of HEAD `883dc31b028f5993ca00e7f64d8f3f7a647ec442`, with 345 source/driver files hash-verified and the same isolated test venv/read-only DB policy. All **20/20** failures reproduced on base; no candidate-only red was observed. This was a 20-test failure-subset replay, **not** a full baseline suite. Baseline log `C:/hp-testbed/p0b-clean-base-20261011/results/pytest_failed_subset_clean_base_20261011.log`, SHA-256 `c4474c31b358f45b8dee475f09293c70e1e6702a0d199b0474cfc2b20dddfe4f`. Existing causes include Windows `core.autocrlf=true` versus LF-only assertions, unavailable Docker, missing declared test dependencies/fixture paths, and a source-snapshot venv-path assumption; remaining same-base failures are not yet individually remediated.

`b5-b6-final` remains **IN_PROGRESS**. B5 stays frozen at `B5_NO_CHANGE_TEMPORAL_RANKING`. The isolated real Hermes Agent v0.21.3 CLI API-wire path is verified with loopback mocks (no external model calls); natural-language assistant-delivery recall remains a diagnostic retrieval gap, and real invalidation-row mutation/rebuild lifecycle is still open. No commit, push, merge, or vault write.
