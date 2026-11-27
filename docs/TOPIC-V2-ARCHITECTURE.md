# Topic V2: Codex-Style Two-Phase Consolidation & Freshness Guard

**Status**: Implemented as an **opt-in** feature (`prefetch.topic_v2_mode: false` by default).  
**Related Philosophy**: [DESIGN-PHILOSOPHY.md](DESIGN-PHILOSOPHY.md)

## 1. Why Replace Unsupervised Vector Clustering?

The legacy topic pipeline (`topic_cluster.py`, `topic_maintain.py`, `topic_fusion.py`) relied on HDBSCAN density clustering, an orphan buffer, and hardcoded cosine thresholds (`SEED_COSINE = 0.65`) followed by prose rewriting:

1. **Embedding-model brittleness**: Cosine distributions vary across OpenAI, BGE, Qwen, and Ollama models, so hardcoded thresholds misclassify or stall when developers swap providers.
2. **Stale prose accumulation**: Rewriting discussions into narrative paragraphs blurred old choices (`A`) and new decisions (`B`), causing outdated `Topic` cards (weighted `2.0` vs QA `1.0` in RRF) to outrank newer raw QA pairs.
3. **High maintenance complexity**: Nine intertwined modules and auxiliary state machines hindered debugging and open-source adoption.

Topic V2 replaces clustering with **scale-free Vector Top-K routing** + **Two-Phase Structured Consolidation** + a **Read-Side Freshness & Supersession Guard**.

## 2. Two-Phase Architecture (`v3core/topic_v2.py`)

### Write Side (`TopicConsolidatorV2` + `consolidate_pg_incremental`)

1. **Phase 1 — Incremental Distillation (`distill_qa`)**:
   - Computes or reuses the QA embedding and retrieves the **Top-K** active candidate dossiers (`route_candidate_topics`, zero threshold cutoff).
   - Distills each QA into a structured `Phase1Note` (`add`, `problem`, `evolve`, `correct`, `historical_note`, or `skip`) either through `v3core.llm_gateway` or via bilingual deterministic fallback rules.
2. **Phase 2 — Structured Dossier Merge (`apply_note`)**:
   - Separates `current_claims` (active truth) from `superseded_claims` (auditable history).
   - Never mutates or deletes `qa_pairs`; every claim records `evidence_qa_ids`.
   - Advances `last_evidence_qa_id` / `last_evidence_at`, recomputes topic embeddings solely from active state, and writes a compact metadata header `<!-- topic_v2:{"v":2,"last_qa":...,"active_qas":[...],"superseded_qas":[...]} -->` (kept well under `TopicRecall`'s 500-char body slice).
   - Maintains durable crash-safe checkpoints (`topic_v2_state.json`, `topic_v2_dossiers.json`) in the profile data directory alongside PG `topics` upserts.

### Read Side (`apply_topic_v2_ranking_guard` in `recall_pool.py`)

When `prefetch.topic_v2_mode` is enabled:

1. Parses and strips internal `<!-- topic_v2:... -->` headers so metadata never leaks into prompts.
2. Demotes unstructured legacy topics or `v2` topics whose `last_qa` trails recalled raw `qa_pairs`.
3. On current-state queries: demotes `superseded_qas` and older derived `note`/`yin` layers strictly below active evidence, breaking remaining QA ties by recency.
4. On retrospective queries (`is_historical_trace_query`, e.g. *"以前为什么放弃..."* / *"Why did we drop..."*): preserves raw historical QA evidence and suppresses recency decay.

## 3. Verified Benchmark Results

### Offline Ablation (`eval.topic_ablation_bench`, 18 scenarios / 5 categories)

| Profile | Overall Pass | A->B Evolution | Cross-Session Synth | Correction Residual | Historical Trace | Stale Outranks Current | False Recall | Avg Context Chars |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `G0_qa_only` | 6/18 (33.3%) | 0.0% | 0.0% | 0.0% | 100.0% | 100.0% | 0.0% | 315.1 |
| `G1_obs` | 10/18 (55.6%) | 0.0% | 100.0% | 0.0% | 100.0% | 100.0% | 0.0% | 384.0 |
| `G1_full` | 10/18 (55.6%) | 0.0% | 100.0% | 0.0% | 100.0% | 100.0% | 0.0% | 541.4 |
| `G2_freshness_only` | 18/18 (100.0%) | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 541.4 |
| `G2_structured` | 18/18 (100.0%) | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% | 0.0% | 550.0 |

### Live LLM Benchmark (`eval.topic_v2_live_bench` with `MiniMax-M3.1-Flash`)

- **42 / 42** Phase 1 calls succeeded (`failed = 0`, `adaptive_fallbacks = 0`, **`valid_json_rate = 100.0%`**)
- Total tokens: `33,341` (`prompt = 25,366`, `completion = 7,975`), total latency `119.057s`
- `G1_full`: `10/18 (55.6%)` -> `G2_live_llm`: **`18/18 (100.0%)`** (`A->B Evolution = 100.0%`, `Correction Residual = 100.0%`, `Cross-Session Synth = 100.0%`, `Historical Trace = 100.0%`, `Stale Outranks Current = 0.0%`)

## 4. Usage & Operations

```bash
# Enable / disable opt-in Topic V2 mode
python -m v3core config set-toggle prefetch.topic_v2_mode true

# Inspect persisted watermark and dossiers
python -m v3core topics-v2 --inspect

# Preview incremental PG consolidation without writing
python -m v3core topics-v2 --dry-run

# Run live-PG / offline evaluators with Topic V2
python -m eval.topic_ablation_bench --out-dir ./out/ablation
python -m eval.locomo_recall_v2.cli run --topic-v2-mode ...
```

When `prefetch.topic_v2_mode` is `true`, `V3Core._on_live_flush` routes incremental flush consolidation to `V3Core.consolidate_topics_v2()` (and invalidates `TopicRecallCache` after DB commit) instead of the legacy SQLite `topic_refine.py` path.

## 5. Deprecation Roadmap for Legacy Topic Modules

1. **Current stage (Opt-in Beta)**: `prefetch.topic_v2_mode` defaults to `false`; legacy modules remain intact for zero-risk rollback.
2. **Next validation gate**: Complete full LoCoMo long-session `--topic-v2-mode` A/B run against isolated PG and verify no regressions on multi-hundred-turn histories.
3. **Default flip & cleanup**: Once LoCoMo A/B confirms parity or improvement, switch `PrefetchConfig.topic_v2_mode` default to `true`, mark `topic_cluster.py`, `topic_maintain.py`, and `topic_fusion.py` deprecated, and remove them in the subsequent minor release.
