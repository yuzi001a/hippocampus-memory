# Cost — what Hippocampus costs to run

This page answers one question: **"If I want this quality, what does a month cost?"**

It is a measurement, not a marketing estimate. Every number below is either **measured** from real
usage or **priced** from a provider page fetched on 2026-09-18. Nothing is interpolated silently:
where a value is derived, the formula is written out, and the LLM part is reported as a band.

Scope: **memory formation + recall only** — observer (rolling note), E1 (compression), topic
synthesis, embeddings, rerank. Your agent's own conversation tokens are a separate bill; see
§ "What this excludes" at the end.

---

## 1. Answer first

| Tier | Turns/day | Memory LLM | Embedding | Rerank | **Total / month** |
|---|---:|---:|---:|---:|---:|
| **Light** | 20 | ¥15 | ¥0 (free tier) | ¥0 (free tier) | **≈ ¥15** |
| **Regular** | 100 | ¥76 | ¥0 | ¥0 | **≈ ¥76** |
| **Heavy** | 300 | ¥229 | ¥0 | ¥0 | **≈ ¥229** |

LLM band (±, see §4): Light ¥8–24 · Regular ¥38–122 · Heavy ¥133–426.

If you use the paid embedding/rerank mirrors (`Pro/…`, ¥0.07 per M tokens) instead of the free tier,
add ¥0.3 / ¥1.4 / ¥4.2 for embeddings and ¥0.3 / ¥1.6 / ¥4.9 for rerank per month.

---

## 2. Measured usage (read-only production telemetry, 14-day window)

Window: 2026-09-04 → 2026-09-18. Source: read-only SQL over the production database
(`qa_pairs`, `conversation_stream`, `observation_notes`, `topics`, `yin_paragraphs`). No writes.

| Quantity | Value | How it was measured |
|---|---:|---|
| QA pairs (≈ one user turn each) | 321 | `count(*)` in window |
| Avg QA size | 6,867 chars | `avg(length(question)+length(answer))` |
| Conversation messages | 2,392 | `count(*)` in window |
| Avg message size | 789 chars | `avg(length(content))` |
| Rolling notes written (observer calls) | 48 | `count(*)` — one note = one observer LLM call |
| Avg note output size | 6,707 chars | `avg(length(content))` |
| QA consumed per note | 12 | `avg(upper(source_qa_range) - lower(source_qa_range) + 1)` |
| E1 runs | 68 | `count(DISTINCT yin_version)` — one run = one E1 LLM call |
| Topic updates | 72 | `count(*)` on `updated_at` in window |
| Embedded rows | 2,107 | QA 225 + messages 1,660 + notes 48 + yin paragraphs 102 + topics 72 |
| Implied turns/day | 22.9 | 321 QA / 14 days |

The last row is the sanity anchor: the reference deployment runs at roughly **23 turns/day**, i.e.
between the Light and Regular tiers.

---

## 3. Token conversion (measured, three independent ways)

| Method | chars/token | Note |
|---|---:|---|
| Direct provider call: 4,404 mixed CN/EN chars → 2,372 `prompt_tokens` | **1.857** | used for the main model |
| MiniMax documentation rule of thumb ("1600 中文字符 ≈ 1000 tokens") | 1.600 | used as the pessimistic end of the band |
| Host session accounting over 25 real sessions (39.5 M input tokens vs 64.2 M message chars) | 1.627 | message text only; prompts add tokens on top |

Using 1.857 for the headline and 1.600 for the pessimistic band therefore brackets the measurement
rather than flattering it.

---

## 4. Per-turn derivation (before multiplying by turns)

| Component | Calls / turn | Input per call | Output per call | Basis |
|---|---:|---:|---:|---|
| Observer | 0.15 | 89,111 chars | 6,707 chars | notes/QA calls; input = previous note + 12 QA × 6,867 chars |
| E1 | 0.21 | 7,707 chars | 1,440 chars | E1 runs/QA calls; input = the note it compresses + prompt; output = yin paragraphs per run |
| Topic synthesis | 0.22 | 4,476 chars | 1,476 chars | topic updates/QA calls; input = topic body + QA context |
| Embedding | 6.6 calls | — | — | measured embedded rows / QA pairs |
| Rerank | 1.0 call | ~12,500 chars (query + 20 candidates) | — | one recall per turn |

Monthly LLM cost = (input tokens × ¥2.10 + output tokens × ¥8.40) / 1,000,000, with
`turns = turns_per_day × 30`.

---

## 5. Prices (with the evidence)

| Provider | Model | Price | Source | Fetched |
|---|---|---|---|---|
| MiniMax | MiniMax-M3 (≤512k input, standard tier) | ¥2.10 / M input, ¥8.40 / M output, ¥0.42 / M cached read | https://platform.minimaxi.com/docs/guides/pricing-paygo | 2026-09-18 |
| MiniMax | MiniMax-M3 (priority tier) | 1.5× the standard price | same page | 2026-09-18 |
| SiliconFlow | `BAAI/bge-m3` (embedding) | **free tier**; `Pro/` mirror ¥0.07 / M tokens | https://siliconflow.cn/pricing | 2026-09-18 |
| SiliconFlow | `BAAI/bge-reranker-v2-m3` (rerank) | **free tier**; `Pro/` mirror ¥0.07 / M tokens | https://siliconflow.cn/pricing | 2026-09-18 |

Notes that matter:

* The list price of MiniMax-M3 is ¥4.20/¥16.80; the platform shows a permanent 50 % discount, which
  is the price used here (¥2.10/¥8.40). If that discount ends, double the LLM column.
* Prices change. This page is dated for that reason; the model script
  (`reports/v0.2-first-user-release/cost_model.py`) carries the same constants so a reader can
  re-run it with new numbers.
* If a provider's price cannot be fetched, the model sets `cost: null` and prints "price unknown"
  rather than guessing.

---

## 6. Reproducing this

```powershell
# 1) usage (read-only; any disposable connection to the same database)
psql -f telemetry.sql

# 2) token calibration (one real call; prints its own usage)
python calibrate_tokens.py

# 3) the model itself
python cost_model.py        # prints per-turn, per-tier, the band, and writes cost-model-output.json
```

All three scripts live in `reports/v0.2-first-user-release/` in the collaboration vault, together
with their raw output.

---

## 7. What this excludes

* **Your agent's own conversation cost.** The host agent sends its whole context every turn; in the
  same 14-day window one session alone consumed 10.6 M input tokens. That bill belongs to the agent
  and would exist with or without Hippocampus.
* **The first full rebuild of a historical archive.** Importing 18k messages and rebuilding
  everything is a one-off; `hippocampus rebuild --estimate` computes it for your archive before you
  spend anything, and `--budget` caps it.
* **Local/self-hosted inference.** If you configure a local embedding or LLM endpoint, the marginal
  API cost is zero and the cost becomes electricity.
