# B01 — Core interface inventory

> **Scope.** Inventory only. This document records what the current `v3core` + packaged CLIs can do
> today, so a later bridge (B02 DSH / B03 pi) wraps *existing* operations instead of rewriting the
> core. Nothing here is implemented by this document; no adapter, no HTTP server, no SDK, no schema
> change.
>
> Baseline: `integration/global-baseline-v1` (product-code HEAD `86dd126`).

## Entry points that already exist

| Surface | Command | Transport |
| --- | --- | --- |
| `hippocampus` | `doctor` · `bootstrap` · `upgrade` · `install` · `uninstall` · `import` · `rebuild` · `health` · `diagnose` · `repair` | local CLI (`v3core.distribution_cli:main`) |
| `v3-core` | `init` · `status` · `migrate` · `mcp` · `serve` | local CLI (`v3core.__main__:main`) |
| `v3-core mcp` | MCP server over **stdio**, exposes the same 13 tools as the Hermes provider | stdio JSON-RPC |
| `v3-core serve` | HTTP server, default `127.0.0.1:39090` | HTTP |
| Hermes provider | `deep_memory_v3` → 13 tool schemas (`v3_add` `v3_get` `v3_update` `v3_manage` `v3_store` `v3_search` `v3_status` `v3_extract` `v3_prefetch` `v3_topic_correct` `v3_moc_overview` `v3_moc_get` `v3_health`) | in-process tool call |

The `mcp` subcommand is the **existing stdio bridge** candidate: same tool surface, no new code path.

## 1. Memory write

| Field | Value |
| --- | --- |
| Python/core entry | `V3Core.store_card(category, title, content, tags=None, source_id=None)` — `v3core/__init__.py:1721`; explicit-memory boundary: `v3core/active_memory_store.py` |
| CLI/tool entry | tool `v3_add` / `v3_store`; MCP `v3_add`/`v3_store`; no dedicated `hippocampus` write command |
| Input contract | `category: str`, `title: str`, `content: str` (body), optional `tags: list[str]`, optional `source_id` |
| Output contract | `list[str]` of warnings (empty = clean); writes the card and notifies recall-cache invalidation. Recall-cache invalidation failure is raised to the caller (derived-side warning, never silent) |
| Sync/async | synchronous; the durable-store write is the caller's blocking path |
| Timeout / failure | no internal budget; a missing/invalid embed config raises `ValueError` (fail-closed, never a silent keyword-only card) |
| Stable identity | `source_id` when supplied; otherwise derived per card. Message-level identity is `msg_id` (see §5) |
| stdio bridge fit | **direct** — the tool already returns JSON; no transformation needed |
| Gaps | no idempotency key for "same content written twice"; caller must dedupe (`v3_update dedup` exists but is a maintenance action) |

## 2. Memory search / recall

| Field | Value |
| --- | --- |
| Python/core entry | `V3Core.search_cards(query, category=None, limit=10)` — `__init__.py:1973`; `V3Core.prefetch(query, limit=5, fmt="list"\|"chain", *, deadline=None)` — `__init__.py:2238`; `V3Core.prefetch_to_context_block(query, session_id="", max_chars=None, *, deadline=None)` |
| CLI/tool entry | tools `v3_search` / `v3_get(target=search)` / `v3_prefetch`; MCP equivalents |
| Input contract | free-text query (+ optional category / limit / format / char budget) |
| Output contract | `search_cards` → `list[dict{source_id,title,category,content_preview,tags,cosine,rrf_score}]` (+`{"warning": ...}` entry when PG degraded); `prefetch(fmt="list")` → `list[dict]`; `prefetch(fmt="chain")` → chain dict; `prefetch_to_context_block` → formatted `str` |
| Sync/async | synchronous, with an optional `deadline` (`PrefetchDeadlineExceeded` propagates and is never swallowed by the broad handlers) |
| Timeout / failure | query embedding: 3 s cap / 0 retries inside the deadline path, realtime policy otherwise; provider failure degrades to keyword-only (`q_emb=None`) and is debug-logged. `ValueError` (bad config) and `PrefetchDeadlineExceeded` always re-raise |
| Stable identity | hit identity = `source_id`; layer/provenance is carried in the hit kind (`qa` / `topic` / `yin` / `active_memory`) and the trace |
| stdio bridge fit | **direct** for `v3_search`; `prefetch_to_context_block` returns prompt text rather than structured hits — wrap only if the consumer needs injection, not retrieval |
| Gaps | ranking/threshold policy is internal and not part of any documented contract; `recall limit` and context budget are configured, not per-call (except `max_chars`) |

## 3. Source / read provenance

| Field | Value |
| --- | --- |
| Python/core entry | `V3Core.get_message_context(source_id)` (raw message text); provenance chain: hit → `topics` / `topic_entries` → `qa_pairs` rows |
| CLI/tool entry | `v3_get(target=message, source_id=...)`, `v3_get(target=hm, source_id=...)`, `hm_get` (any source_id: message, card, handbook, file) |
| Input contract | one stable `source_id` |
| Output contract | full original content (message text / card body / handbook entry); `db_error` when PG is unreachable |
| Sync/async | synchronous |
| Timeout / failure | read-only; PG errors surface as an explicit error payload, never a fabricated empty result |
| Stable identity | `msg_id` for messages, `topic_id` (`t_*`) for topic cards, handbook key for handbook entries |
| stdio bridge fit | **direct** |
| Gaps | no single call that returns *hit + its provenance path* together; a consumer that wants "why did this surface" must walk `topics → topic_entries → qa_pairs` itself |

## 4. Status / doctor

| Field | Value |
| --- | --- |
| Python/core entry | `V3Core.get_status(category=None)`; `v3core/tools/health.py`; `v3core/reliability` (health / diagnose / repair plan); `v3core/runtime_integrity` (live-env identity, shadow detection, install/uninstall plan) |
| CLI/tool entry | tool `v3_health`; `v3-core status`; `hippocampus doctor [--static\|--full\|--runtime --wheel <whl>]`, `hippocampus health`, `hippocampus diagnose`, `hippocampus repair` |
| Input contract | optional category / flags; `doctor --static` skips config resolution |
| Output contract | structured JSON on stdout (`command`, `checks`, `status`); exit 0 healthy / non-zero on failure |
| Sync/async | synchronous, read-only by default |
| Timeout / failure | `doctor --runtime` compares content fingerprints of the live process environment against a wheel; a stale process reports `warn`, a shadowed copy `error` |
| Stable identity | package content fingerprint (not the version string — both read `4.0.0`) |
| stdio bridge fit | **direct** (JSON stdout) |
| Gaps | `repair` produces a plan; it does not apply changes (`ADR-003 repair-planner-no-apply-v1`) |

## 5. Raw-event narrow ingest

| Field | Value |
| --- | --- |
| Python/core entry | `LiveBuffer.enqueue(session_id, msg_id, content, role, turn_id, timestamp, tool_calls, tool_results)` — `v3core/ingest.py:121`; `V3Core.sync_turn(session_id, messages=None)` — `__init__.py:3434`; `V3Core.ingest_turn(signal)` / `ingest_session_start` / `ingest_session_end` / `ingest_feedback` / `ingest_signals` |
| CLI/tool entry | none for narrow events; the Hermes provider calls `sync_turn` on the outbound hook. `hippocampus import` / `rebuild` are bulk paths, not event paths |
| Input contract | one message event: `session_id` + `msg_id` + `content` + `role` (+ optional turn/timestamp/tool payloads) |
| Output contract | `True` = accepted (durable marker written, queued when the writer runs); `False` = explicitly rejected (fence closed / no durable marker) → **caller must retry**; `None` = accepted-without-queue |
| Sync/async | enqueue is non-blocking; PG insert happens on the writer thread (single worker, FIFO) |
| Timeout / failure | rejection is explicit (`False`), never a silent drop; the durable marker is written before queueing so a crash does not lose the event |
| Stable identity | `msg_id` (message), `session_id` (conversation), `turn_id` (turn grouping) — all caller-supplied and preserved end to end |
| stdio bridge fit | **direct but must honour the retry contract** — a bridge that ignores `False` will silently lose events |
| Gaps | no CLI/stdio surface for a single event today (`mcp` exposes memory tools, not the event ingest); a bridge must construct `messages` in the provider's shape or call `enqueue` directly |

## Summary for the bridge layer (B02/B03)

* Reusable as-is over stdio: `mcp` tool surface (write, search, read, status) — no core changes.
* Needs a thin wrapper only: single-event narrow ingest (retry on `False`) and "hit + provenance in
  one call".
* Must **not** be re-implemented: embedding policy, cache, canonical slices, chunked derived index,
  failure ledger, deadline handling. The versions are the only supported entry points.
* Not available through any surface today: cross-host identity, second-host coordination (those are
  A03+/future work).
