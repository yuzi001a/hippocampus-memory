# Q01 — query canary (frozen)

> **Status: READY_FOR_CANARY.** The case set, the expected behaviour and the observation mechanism
> are frozen here *before* the canary runs. A case that fails is reported as failing; the set may
> not be reworded afterwards to improve the result.
>
> Scope: the A02 query path — cold-query semantic lane, hot-query cache, token-safe cap on long
> queries, failure degradation. Retrieval ranking policy is out of scope.

## Observation mechanism (existing, not new)

* Retrieval-level facts come from the run: hits, provider query calls, the text handed to the
  provider (character and token counts), and the resulting context block.
* The per-request trace reuses the **existing** `RecallTrace` channel:
  `v3core.prefetch.prefetch(..., trace_out=[holder])` — the same technique
  `eval/locomo_recall_v2/adapter.py` already uses. No new observability platform.
* Privacy: the trace is serialized with the privacy-safe defaults (`include_content=False`,
  `include_query=False`); provider payloads are never printed, only counts/ids/classes/timings.
* Known limitation of the observation, recorded honestly: the trace object is really built and
  attached (`RecallTrace` from the existing factories), but its per-lane summaries stay empty on the
  Core query path, because that path calls `recall_pool` directly instead of the engine entry that
  populates lane summaries. Q01's checks therefore rely on the run's own numbers — hit count/ids,
  provider query calls, payload sizes, raised errors — and treat the trace as supporting context,
  not as the primary measurement.
* Runner: `src/v3-core/eval/q01_query_canary.py`
  (`--list` prints the frozen set; `--profile … --out …` runs it). It writes nothing to the target
  profile.

## Case set (frozen wording)

| id | category | question (verbatim) | expected behaviour |
| --- | --- | --- | --- |
| Q01-01 | cold_semantic | 我们之前定过一条规则：任何写入路径都不许悄悄丢掉向量，那条规则的英文名字是什么？ | a *cold* query still reaches the provider once and the semantic lane is handed a vector; ≥1 hit |
| Q01-02 | hot_cache | (Q01-01 原样) | **0** additional provider calls; identical hit ids |
| Q01-03 | historical_fact | long-observation 那批历史数据最后修了几条？ | ≥1 hit, no fabrication |
| Q01-04 | historical_fact | 长 QA 的向量索引表叫什么名字？ | ≥1 hit, no fabrication |
| Q01-05 | project_continuation | 我们现在这条统一的开发基线叫什么？ | ≥1 hit, no fabrication |
| Q01-06 | project_continuation | 下一步该做哪个阶段的工作？ | ≥1 hit, no fabrication |
| Q01-07 | long_query | synthetic 9001+ token 粘贴文本（`synth_long`，固定生成） | `truncated=True`; prepared ≤7680 tokens; head and tail retained; no provider error |
| Q01-08 | long_query | 同上，末尾附真实问题（`synth_long_with_tail_question`） | 同上 + tail 保留后仍 ≥1 hit |
| Q01-09 | embedding_failure | (Q01-03 原样，注入 `EmbeddingCallError`) | no crash; embedding attempted; keyword lane still returns ≥1 hit |
| Q01-10 | embedding_failure | (Q01-04 原样，注入 timeout 类失败) | 同上 |
| Q01-11 | off_topic | 请从第一性原理解释量子色动力学里的渐近自由是什么。 | **response-level** — rubric below |
| Q01-12 | off_topic | 帮我写一个把 CSV 转成 Parquet 的 Python 函数。 | **response-level** — rubric below |
| Q01-13 | cold_semantic | embedding 模型不一致会造成什么后果？ | cold call embeds; ≥1 hit |
| Q01-14 | historical_fact | 印是什么？它和主题卡分别负责什么？ | ≥1 hit, no fabrication |
| Q01-15 | long_query | 一句普通的短查询，用来确认短查询完全不被改动。 | `truncated=False`; the text sent to the provider is byte-identical to the query |
| Q01-16 | hot_cache | (Q01-06 原样) | 0 additional provider calls; identical hit ids |

### Frozen rubric for the `off_topic` cases (judged during the A03 real canary)

Pass requires **all** of:

1. no historical fragment is presented as the answer to the question;
2. either nothing is injected, or the assistant explicitly states that its memory does not cover the
   question (the production fence already asks for this: "若信息不足，请如实说明，不要编造");
3. no invented entity, date, number or file path appears.

An answer that "helpfully" reuses unrelated memories fails, even if it sounds plausible.

## What a failure means

| observation | reading |
| --- | --- |
| Q01-01 `embedding_attempted=False` | the cold-query regression is back (the A02 P2-1 guard) |
| Q01-02/16 `additional_provider_calls>0` | query cache not effective for the repeated query |
| Q01-07/08 `sent_tokens > 7680` | the cap regressed (or the model window shrank) |
| Q01-07/08 head or tail missing | the representation is truncating one end instead of keeping both |
| Q01-09/10 crash or 0 keyword hits | failure degradation changed |
| Q01-15 text ≠ query | the seam is modifying queries that already fit (hard contract violation) |

## Synthetic vs private

* This document, the case wording and the synthetic long queries live in the public repo — they are
  synthetic or desensitised only.
* A second, private variant of Q01 (real personal questions and real history) belongs in private
  evidence / the collaboration vault, never in the public repo. Its wording is frozen the same way
  and must not be "improved" after seeing results.

## Execution

```bash
# frozen set
python src/v3-core/eval/q01_query_canary.py --list
# retrieval-level run against a profile (read-only)
python src/v3-core/eval/q01_query_canary.py --profile default --out q01-report.json
```

The response-level cases (Q01-11/12) are asked to the live agent during the A03 canary and judged
against the rubric above; the script only records the retrieval-side facts for them.
