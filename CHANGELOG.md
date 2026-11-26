# Changelog — Hippocampus

> **Pre-release history note:** the historical tool-surface framing and
> the SQLite-mirror-based card-store path are **not** the supported
> alpha contract. They are referenced in this changelog only as
> historical background, not as current behavior.

---

## [4.3.0] - 2026-11-26

### 修：模型「写一半就停」，以及三处让不合格产出溜过去的校验漏子

**退化兜底**

`deepseek-v4.1-flash` 走 opencode-go 时，遇到特定输入会先烧一段思考、再自己吐结束符，
留下半截 JSON——服务端报 `completed`、`finish_reason=stop`，不是超时也不是预算打满。
实测同一提示词同一批问答：原样重发 3 次全断；把输入里的 `<` 垫开一格后 3 次全通。

现在判定退化后不再原样重试，直接换消毒过的输入重发；消毒痕迹按映射在写库前还原，
印记不进笔记。判定只看形状（半截 JSON / 提前收尾 / 逐字循环），内容质量仍归校验层。

**四条先前溜过去的**

- **省略不等于关思考**：请求里不带 `reasoning` 字段时，模型仍然产 163–278 token 思考；
  显式发 `"none"` 才是 0。现在通道支持 effort 且配置要求关思考时补 `"none"`。
  实测中位单轮 80s → 28s。
- **空壳输出**：模型偶尔把提示词里的格式样板原样吐回来（43 字）。旧校验只查「锚点越界」，
  不查「压根没有锚点」。现在无正文 / 无引用编号按失败批处理，不写库。
- **整篇抄上一版**：修复轮救不回来的「原样重发」以前照写。实测有两条与上一版分别只差
  3 字 / 2 字，而输入问答差异达 94% / 82%。现在按篇幅区分——只写本批变化的短版本
  （篇幅 < 上一版 80%）照写，整篇重发不写库、推进游标并记进 `repeat_skipped`。
- **锚点正则**：`[#25](https://.../pull/25)` 是正文里的超链接，不是问答锚点。旧正则
  把它算进去，实测 2 条只覆盖 10 条问答的正常笔记被判「锚点越界」。

### 验证

- 单元测试 25 条（本版新增 6 条）。
- Y400 全量回放 19,271 条问答 / 1,686 条笔记：覆盖 19,270 条（余 1 条为同一句问了两遍）、
  整篇重复 0、锚点越界 0、空壳 0、「字数 >8,000 且压缩比 >0.6」0 条。
- 生产库 637 条历史笔记在新校验下零误判。
- 关思考实测：单轮中位 80s → 28s；生产 11-26 03:15 真实调用 reasoning=0、33.5s、
  写出 5,777 字。

---

## [4.2.1] - 2026-11-23

### 修：切到 opencode-go 后 E1 报「不支持的 LLM provider」

`llm.py` 原本只有 minimax / openai 两条硬编码分支，配置换成 opencode-go 后
E1 合成、抽取、摘要这些不走观察者画像的调用直接失败。

现在 OpenCode 家族（含自定义同族名字）统一走通道画像：URL、会话头、UA、
输出字段名按画像给，responses 模式把多轮历史折进 `input`、system 走 `instructions`，
chat 模式保留原生 messages。其余 provider 行为不变。

验证：`tests/test_llm_opencode.py` 3 条 + 相关 364 passed / 5 skipped。

---

## [4.2.0] - 2026-11-23

### OpenCode Go / Zen 适配层

把这家中转自己的规矩集中到一处（`providers_opencode.py`），调用点不再判断模型名：

- **按模型选传输**：`gpt-*`/`grok-*`/`muse-spark` 走 `/responses`，
  `claude-*`/`minimax-*`/`qwen*`/`union-alpha` 走 Anthropic 线，其余走 `/chat/completions`；
- **端点与传输成对**：官方主机上路径段按 family 纠正（`/zen` vs `/zen/go`），anthropic 线去掉 `/v1`，
  其余补 `/v1`；自建代理端点一个字不改；
- **会话头自动补**：`x-opencode-session` 缺失即 400，现在画像里自动带；
- **模型 id 归一**：`opencode-go/xxx` 前缀在发请求前剥掉；
- **错误翻译**：400 MissingSessionID / 401 模型不支持 / 403 免费档 都翻成人话再报。

配置上 `providers.<name>` 可以留空（family 名或 opencode.ai 端点即可识别），
显式写的 `api_mode` / `base_url` / `session_header` 一律优先。

DEFAULT_SUPPORTS 按传输给出（responses 支持 json_schema 等），显式 `supports:` 覆盖。

---

## [4.1.0] - 2026-11-22

### 代理身份（记忆插件作为 agent 向 provider 自报身份）

OpenCode Go 的文档要求客户端「每个会话发一个稳定的 session id」并「用自己的
User-Agent，而不是通用 SDK 或 HTTP 库的名字」。历史实现两条都不合规，且都不报错：

- `_session_affinity_id` 把 pid 写进会话 id → 进程重启即「新会话」，后端亲和与提示词缓存全丢；
- 请求从未设置 User-Agent，出去是 `python-requests/x.y`。

改动：

- 新增 `agent_identity`：安装首次运行生成安装 id 落盘（`agent_session.json`），
  会话 id = 安装 id + 当天日期 —— 同日恒定（重启/升级不变），跨天换新；
- `providers`：`ProviderProfile` 增加 `user_agent` / `client_headers`，`build_headers` 必发 UA，
  默认 `hippocampus-memory/<version>`，可由 `providers.<name>.user_agent` 覆盖；
- `observer`：会话 id 改走 `agent_identity`，不再含进程号；`observer.session_id` 可显式钉死。

未改提示词、模型、数据结构。

---

## [4.0.9] - 2026-11-22

### 有界重试（长请求被掐断不再整批作废）

- `call_m3` 对**瞬态失败**重试：连接中断 / 超时 / 408·425·429·5xx，默认 3 次、指数退避（2s→4s，上限 30s），
  服务端给 `Retry-After` 时听它的。
- 配置/鉴权/结构类 4xx（400/401/403/404/422）一次都不重试——重试它们只是把错误拖长。
- 可配：`observer.retry_attempts` / `observer.retry_backoff_s`。
- 实测依据：500 条问答样本跑批时 23 发请求有 6 发被掐断（`Max retries exceeded`），
  手工补跑三发内全部救回；接上重试后同类失败不再丢批。

## [4.0.8] - 2026-11-22

### 多模型适配（换模型/换通道不再改代码）

- 新增 `providers.py`：通道画像（`chat_completions` / `responses` / `anthropic_messages`），
  按 `config.yaml` 的 `providers:` 段解析端点、鉴权头、输出上限字段名、支持的能力，
  并把三家响应统一折成 `{text, reasoning, usage, finish_reason}`。
- `models:` 段新增 `provider` 绑定：换模型只改配置两行；未声明的端点保持历史形状并给一次 WARNING。
- `observer.call_m3` 改走画像；通道能强制 `json_schema` 时自动带 `OBSERVER_RESPONSE_SCHEMA`，
  不支持的通道（如 MiniMax）行为完全不变。
- 真实 canary（OpenCode Go `/responses` + strict schema，9 条真实 QA）：70.2s，
  印 3,173 字，归属/状态零缺失，3 个主题动作。

### 调研结论（写入文档，避免再踩）

- DeepSeek 官方 `chat/completions` 的 `response_format: json_schema` 返回 400；
  `json_object` 可解析但字段不强制。
- DeepSeek 官方与 OpenCode Go 的 `/responses` + `text.format.json_schema` 均**真正强制**；
  OpenCode Go 的 `chat/completions` 也强制。

## [4.0.7] - 2026-11-21

### 修复：回放模式取批排序与游标同维度

- 4.0.6 修好区间界之后暴露出上一层问题：取批按 `timestamp`、游标按 `id`，回放库里两者不同步，30 条的批次会横跨几千个 id，`max(id)` 把游标一口气推到底，中间大量 QA 从未被观察，却表现为"跑完了"。
- 新增 `_qa_batch_order_clause(cfg)`：`debounce_minutes` **显式** <= 0（回放标识）时 `ORDER BY id ASC`；缺键或生产默认值仍按 `timestamp ASC NULLS LAST, id ASC`，不改变生产的时间序语义。
- 回归测试覆盖回放/生产两种排序，以及"缺键按生产走"的边界。

## [4.0.6] - 2026-11-21

### 修复：批次区间界取 min/max（回放/重导场景下的静默停摆）

- `_observe_worker` 用 `qa_ids[0], qa_ids[-1]` 当区间界。取批按 `timestamp` 排、游标与 `source_qa_range` 按 `id`，增量库里同步，**回放或按时间序重导时两者不同步**，首尾相减得到反向区间 → 每轮写 `observation_notes` 都抛 `range lower bound must be less than or equal to range upper bound`，游标停在原地，日志持续刷同一条错误。
- 现场表现最容易误判为成功：全量重放跑到 80%（15,537/19,330 条 QA）后每轮都"正常返回"，游标不动，没有任何数据错误。
- 改为 `_qa_range_bounds(qa_ids)` 取 min/max，并为它加回归测试（乱序、单条、空批、以及"不许改回列表首尾"的 AST 断言）。

## [4.0.5] - 2026-11-20

### Observer 证据归属与状态的遵从性修复

- `[建议]` 只描述「还没发生的事」：已落地的执行结果不再被降级成提案。已实施但看不出发起者的条目改为不写归属标记并括注「归属未见记录」；归属可从上版印沿用，不因本轮只见执行结果而重新判成 `[建议]`。
- 「关键决策」与「进行中状态」两节每条都必须带状态词。原规则留了「写不准就省」的口子，实测出现过整节 0/10 条带状态词；现在该要求在节定义与格式示例里各写一遍，只保留纯背景描述可省。
- 真实数据对照（两组各 30 条生产 QA，同模型、同上一版印）：决策节归属 0/4、0/4 → 7/7、7/7；决策节状态词 0/4、0/4 → 7/7、7/7；进行中状态词 0/4、0/8 → 10/10、12/12；「已完成却标建议」1 → 0。
- 提示词改动不涉及 schema，历史 `observation_notes` 保持原样。

## [4.0.4] — Observer says who decided what, and models become config

> **Scope:** one round of "read the real source, fix the real defect". Prompt-shaped
> changes only where a real corpus showed the failure; no new tables, no new engine.

### Observer: evidence attribution and work status

- Every section carries a one-line definition again (the v4 prompt diet had removed
  them, leaving section names only). Measured on 617 production notes: of 2,905
  「关键决策」 bullets only 15.4% were recognisably a user decision, 217 carried
  「尚未/计划」, and 533 of 2,585 「进行中状态」 bullets were finished work.
- Decision/progress bullets carry `[用户]` / `[建议]` / `[共同]` (uncertainty may only
  be `[建议]`), and `〔已实施〕〔已验证〕〔计划中〕〔未完成〕〔已推翻〕` sits next to the
  conclusion. Status is decided by the current QA only — it is never inherited from
  the previous note.
- Corrections inside one batch are written `原为 X → 改为 Y`, keeping the superseded
  value in the same line instead of appending a second "fact".
- One-off task parameters are not promoted to long-term preferences; an explicit
  「我不喜欢这种写法」/「别再默认开思考」 is a preference without needing a keyword.
- `new_facts` (the layer that becomes topics.body / topic_entries) carries the same
  attribution, and E1 compression is told to preserve the markers.

### Models: request shape is configuration

- New `v3core/model_params.py`: built-in table + user `models:` section, longest
  prefix match, vendor-namespace tolerant (`MiniMax/MiniMax-M3.1` and bare `M3.1`
  still resolve), one WARNING per undeclared model, `v3-core config validate`
  lists them.
- `reasoning_effort` is configurable per model and overridable per consumer
  (`llm.reasoning_effort` / `observer.reasoning_effort`); `null`/`omit` means "do not
  send the field". `llm.py` only applies an effort that was explicitly configured —
  E1/extract run at a 131k budget and have no quality evidence for a global downgrade.
- A missing model is a configuration error that stops the round without touching
  adaptive batch state, failed-segment counters or the cursor.

### Packaging

- The packaged DDL now carries `conversation_stream.session_source` in both
  `alpha_bootstrap.sql` and `upgrade_v0_2.sql`. `importers._RAW_DEDUPE_SQL_F2`
  INSERTs that column, so a wheel install previously failed on first import while a
  hand-migrated production database hid it.
- `task_events.sql` / `session_policies.sql` ship with the package (the runtime tells
  users to execute them).
- `tests/test_schema_packaged_parity.py` pins "every column the importer writes must be
  creatable from the packaged bootstrap".
- Packaging tests now derive the expected version from `pyproject.toml` instead of
  hardcoding it, and `plugin.yaml` / docs pins are back in sync.

## [4.0.2] — one runtime schema contract, and an upgrade gate that cannot be bypassed

> **Scope:** the 2026-10-19 existing-install compatibility incident. The engine
> wrote a column the database never had; the diagnostics said everything was
> fine. No recall/QA/observer behaviour changes.

### Fixed
- **One schema contract.** `v3core/schema_contract.py` derives tables, columns,
  indexes and ledger versions from the packaged DDL, and the upgrader, `doctor`,
  `health`, `v3_status` and the runtime write gate all read it. Before, each of
  the four kept its own hand-written list; the writer's list had
  `qa_pairs.merged_event_ids`, the doctor's and health's did not.
- **The upgrade path can deliver what the writer needs.** `upgrade_v0_2.sql` now
  adds `qa_pairs.chars`, includes `embedding_failures.sql`, and records `v0.4`
  in the ledger only after the whole script commits.
- **`doctor` fails on drift it used to call healthy.** A missing column or a
  ledger behind the installed contract is `fail`, not `ok`.
- **`health` checks columns, not just table names.** New `ST06_schema_columns`;
  `ST03` compares against every required ledger version.
- **The runtime refuses a write it cannot store — cheaply.** `_flush_pending_qa`
  probes before embedding (no wasted provider call, no attempt-counter bump),
  leaves the pending marker in place, and logs one actionable line instead of
  126 identical `42703` errors.
- **A structural quarantine is no longer terminal.** Markers poisoned by
  `UndefinedColumn`/`UndefinedTable` are revived once the contract probes clean;
  a missing column is our fault, not the user's data.

### Not changed
- Recall, prefetch, topic cards, observer notes, M03/M04 semantics and the tool
  surface. The gate only blocks writes the database provably cannot accept.

---

## [4.0.1] — MCP SDK demoted to an optional extra

> **Scope:** one compatibility defect found while preparing Hippocampus for
> Hermes v0.22.2. No memory behaviour changes.

### Fixed
- **`v3-core` no longer declares the MCP SDK as a core requirement.** Only the
  standalone stdio server (`v3-core mcp`) imports it, and it uses the FastMCP
  1.x API; every other install of the engine carried a `mcp<2` pin for nothing.
  Hermes v0.22.2 pins `mcp==2.0.0`, so the plugin's dependency member was
  unsatisfiable inside the plugin manager's workspace and the plugin was
  disabled at admission. The SDK now lives in the `mcp` extra
  (`pip install 'v3-core[mcp]'`) for the dedicated stdio environment.
- The server's own import error now names that extra instead of asking the
  operator to install a bare pin.
- `v3-hermes-plugin` requires `v3-core>=4.0.1` so a fixed plugin cannot be
  paired with a core that still carries the old core-level pin.

### Not changed
- Decode/recall/QA/observer/RRF paths, M03 and M04 semantics, and the tool
  surface. The stdio server still exposes the same 13 tools when the extra is
  installed.

---

## [0.2.5] — tool-level profile scoping completed

> **Scope:** finish what 0.2.4 started. 0.2.4 fixed the *dispatch* layer; this
> patch fixes the *leaf* helpers, so a non-default profile no longer reads or
> writes default-profile data through any tool.

### Fixed
- **Tool helpers that ignored the booted profile.** Sites that resolved the
  default profile on their own: `HandbookManager()` ×7 (handbook paths in
  `v3_get`/`hm_status`/`hm_handbook_*`), `resolve_config("default")` in
  affinity, `_resolve_paths()` in organize, and `_resolve_data_dir()` /
  `resolve_config()` in the topic create/edit/correct paths (sqlite under the
  default base). `v3core/_tool_scope.py` binds the booted config for one tool call
  (`handle_tool_call` is the choke point for HTTP `/tool`, stdio MCP, and the
  in-process provider); `config._resolve_data_dir()` honours the bound scope
  when the caller passes none, and every other site prefers the bound scope
  with `resolve_config()` kept as the legacy fallback for direct calls.

### Tests
- `tests/test_b02_tool_scope_completion.py` (RED first, then GREEN): scope
  reaches the sqlite root / `DeepStore` / `HandbookManager` / organize paths;
  scope never leaks past the call; unscoped legacy calls still resolve default.

## [0.2.4] — B02 MCP prerequisite fixes

> **Scope:** make the existing v3-core stdio MCP path usable from a clean install.
> This is not a DSH adapter, not automatic recording/injection, and not a production
> deployment.

### Fixed
- Declare the official MCP SDK as `mcp>=1.0,<2`; the server uses the FastMCP 1.x
  API and a clean wheel install must not require a manual dependency repair.
- Preserve the selected profile/config through HTTP `/tool` and stdio MCP dispatch;
  non-default profiles no longer silently fall back to the default database.
- Make `v3_get(target=hm, source_id=...)` read active-memory ids returned by
  `v3_search` from `explicit_memories`, completing the search → source-read path.

### Added
- B02 TOOL-support contract and scope: `docs/B02-DSH-TOOL-SUPPORT.md`.
- Regression coverage for packaged MCP dependencies, profile scoping, and active-memory
  source reads.

### Not changed
- No database schema, production data, gateway process, automatic recording, or
  automatic recall injection.

---

## [0.2.3] — B01 minimal host contract + fresh-database bootstrap fix

> **Scope:** transport/identity/host-contract work. Not deployed to production;
> no schema change; no new model calls.
>
> **Fix fresh-database bootstrap ordering for `observation_embedding_chunks`.**
> A brand-new database could not be initialised at all from the installed wheel
> (`UndefinedTable: relation "public.observation_notes" does not exist`) because
> the long-observation sidecar was spliced before its FK target existed. This is
> a first-install hotfix, not a feature release.

### Fixed
- **Fresh-database bootstrap (`FRESH_DB_BOOTSTRAP_DEPENDENCY_ORDER`, P1 install blocker).**
  `hippocampus bootstrap` on an empty PostgreSQL+pgvector failed with
  `UndefinedTable: relation "public.observation_notes" does not exist` and rolled the whole
  transaction back, so a brand-new install ended up with no schema. The
  `observation_embedding_chunks.sql` include marker now sits after the `observation_notes`
  block in both artifact copies (repo-root canonical + packaged). Ordering only — no schema,
  FK or migration change. Guard: `tests/test_bootstrap_dependency_order.py` validates the FK
  creation order of the *expanded* packaged SQL, and
  `eval/fresh_bootstrap_e2e.py` proves the whole packaged path (clean wheel → fresh venv →
  fresh DB → bootstrap #1 → bootstrap #2) with `fallback_used=false`.
- **Post-restart duplicate QA (`EVENT_INGEST_CORRECTNESS_DEPENDS_ON_EPHEMERAL_PROCESS_STATE`).**
  Derived QA identity embedded `q_turn = context.turn`, an in-process counter that resets on a
  fresh Core, so a re-sent event after a restart produced a second `qa_pairs` row. Event
  ingestion now reports a per-event status and an already-known identity (durable **or** pending)
  is a `duplicate` for which QA derivation is skipped.
- **`/events` could not report a failed write.** The endpoint always answered `{"ok": true}`
  because `sync_turn` returned `None`. It now answers `accepted` / `duplicate` /
  `retryable` / `failed` with `event_id`, `host` and (when known) `source_id`; a refused durable
  write returns HTTP 503 with `ok=false` instead of pretending the event was remembered.
- **Cross-host identity collision.** Identical native session/event ids coming from two hosts were
  treated as one identity (in-process cursor and QA identity were host-blind). Identity is now
  `(host, session_id, event_id)`; legacy callers keep the historical template byte-for-byte.
- **Turn-blind pairing.** An answer arriving with a turn id that disagrees with the open question
  used to be appended to it. Pairing now holds instead of guessing (the raw event is still kept).

### Added
- `v3core.bridge_contract` — single truth for event identity, ingest status semantics, ACK mapping
  and `get_bridge_capabilities()` (bridge protocol `b01.1`), shared by HTTP `/health`,
  MCP `v3_health`, the stdio startup handshake and the serve ready event.
- `v3-core serve --port 0` now reports the **actually bound** port, and `--ready-json` prints one
  deterministic ready event (host, real port, pid, protocol/core version, capabilities).
  `SIGTERM` goes through the same graceful path as Ctrl-C (stop accepting → `core.shutdown()`).
- Event payloads carry optional `host` / `project_id` / `agent_id` / `parent_event_id` /
  `branch_id` provenance (G4 keeps identity available; X03 semantics are explicitly NOT claimed).

### Changed
- `msg_buffer` is bounded (32 sessions / 500 events per session / 6h idle TTL) and is documented as
  a performance optimisation only — durable identity on disk is what makes ingestion correct.
- `LiveBuffer` gained `event_status()` and a host-scoped accepted-tombstone path; `enqueue`'s
  existing True/False/None contract is unchanged.

### Known limitations (explicit)
- The crash window where the source is durable but QA was never derived is skipped rather than
  guessed (recovery path: the existing mapper backfill).
- Outbox replay stays at-least-once for `conversation_stream` (see `docs/B01-FINDINGS.md` F2).
- Packaged `hippocampus bootstrap` fails on a brand-new database (F1, not fixed here).
- No cross-host concurrent shared memory, permission isolation or conflict resolution — that is X03.

## [0.2.2] — unified baseline (A01 + A02), production canary closed

> **Scope of this entry:** the unified baseline (`integration/global-baseline-v1`) that
> merges the embedding-reliability recovery line with the runtime-integrity line and fixes
> the query path (A02). It **is deployed** to the production Hermes runtime (canary closed
> 2026-09-27, gateway 3772/5252 → 5420/17068) and is published as tag `v0.2.2`. The
> packaged version string stays `4.0.0`; the last published entry before this one is
> `0.1-alpha` below, with `v0.2`/`v0.2.1` as the intermediate tags.

### Added

- Runtime integrity / deployment identity surface: `hippocampus doctor --runtime
  --wheel <whl>` (content fingerprints of the live process environment),
  `install --plan`, `uninstall --plan`, plus the read-only `reliability`
  diagnose/repair command group. See `docs/RUNTIME-INTEGRITY.md` and
  `docs/reliability/`.

### Changed

- `README.md` install walkthrough now documents the runtime-integrity step that
  the packaged code implements.
- `README.md` bootstrap row and `docs/INSTALL.md` `doctor --static` contract now
  state the actual packaged SQL set: five artifacts, three bootstrap include
  markers, and a nine-table bootstrap schema (six core tables plus
  `explicit_memories` and the two derived-index sidecars).

### Query path (A02) — the fix this release exists for

- **Cold queries now reach the semantic lane.** `search_cards` no longer pre-checks its own
  `_EMBED_CACHE` membership before embedding; `cache=True` owns the reuse. Failure keeps the public
  path's degradation (`ValueError` / `PrefetchDeadlineExceeded` re-raise, otherwise keyword-only).
- **One token-safe query embedding seam.** `embed_chunks.prepare_query_embedding_text()` +
  `embedding.call_query_embedding()`: a query that already fits is returned byte-for-byte; a query past
  the provider window is reduced head 60% / tail 40% by tokenizer offsets with a single `\n` join and a
  deterministic shrink, so no query can hand an over-window payload to the embedding provider. Durable
  source text is never capped by this path.
- Production canary evidence: 16 frozen Q01 cases executed against the installed candidate — cold
  `+1` provider call, hot `+0`, a 17 013-token query reduced to a 7 678-token provider payload, short
  query byte-identical, failure degradation intact. See `CURRENT.md` §A03.

### Documentation

- `docs/GLOBAL-BASELINE.md` — the single capability matrix for this baseline
  (what is included, what is deliberately not, with evidence and known limits).

## [0.1-alpha] — First Public Technical Preview

> **Scope of this entry:** Hippocampus v0.1-alpha is the first public
> technical preview. It carries the additive schema artifacts, the
> explicit bootstrap CLI, and the documented supported surface below.
> The release is intentionally not a stable production release; see
> [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
> and [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md) for the
> evidence boundary and non-claims.

### Added

- New user-facing docs at the repo root and under `docs/`:
  `README.md` (front door rewritten for the alpha), `docs/INSTALL.md`,
  `docs/CONFIGURATION.md`, `docs/BACKUP-RESTORE.md`,
  `docs/PRIVACY-DATA-FLOW.md`,
  `docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`,
  `docs/ARCHITECTURE-OVERVIEW.md`, `docs/KNOWN-LIMITATIONS.md`,
  `docs/RELEASE-CHECKLIST.md`, plus root `CONTRIBUTING.md`,
  `SECURITY.md`, and `CHANGELOG.md`.
- Starter config templates under `examples/`:
  `examples/config.example.yaml` and `examples/.env.example`. **All
  values are placeholders.**
- Explicit "first-screen" framing of what the alpha is, who it's for,
  what's supported, and what's experimental — replacing the previous
  product framing in `src/v3-core/README.md` and the wizard-centric
  framing in `src/v3-hermes-plugin/README.md`.
- Explicit documentation that **DB bootstrap is a command**, not an
  automatic first-run step: trial users run
  `src/v3-core/scripts/bootstrap_alpha_db.py` against their disposable
  PG before the first write. See
  [`docs/INSTALL.md`](docs/INSTALL.md) § 6.
- Explicit documentation that **the engine config `basePath` /
  data root is required**, not derived: trial users must set the
  **top-level `basePath`** key in `config.yaml` to the absolute
  profile directory; the engine does NOT auto-create the directory
  and does NOT expand `~` on Windows. The legacy/internal
  `Path.home() / ".v3-core" / ...` last-resort branch in the
  resolver is documented as an implementation detail, not a
  recommended contract.
- Explicit documentation that **Hermes is a host dependency** for
  `v3-hermes-plugin`: the plugin is a thin adapter and the
  end-to-end hook contract requires a working Hermes Agent host.

### Additive schema, scripts, and tooling (not in the original base)

The release adds the following **additive** artifacts, separate from
the engine source itself. None of them are auto-invoked by
`v3core.active_memory_store`; each is operator-callable.

- `src/v3-core/schema/alpha_bootstrap.sql` — additive wrapper
  DDL that bundles the canonical
  `src/v3-core/schema/explicit_memories.sql` artifact with the alpha
  working set (`qa_pairs`, `conversation_stream`, `topics`,
  `topic_entries`, `observation_notes`, `yin_paragraphs`) plus the
  `vector` extension. Idempotent (`IF NOT EXISTS` /
  `ADD COLUMN IF NOT EXISTS`).
- `src/v3-core/scripts/bootstrap_alpha_db.py` — single-entry CLI
  that applies the bootstrap SQL above. Refuses empty / production
  boundary DSNs by default; never hardcodes a password; emits
  machine-readable JSON status.
- `src/v3-core/scripts/backup_alpha.py` — alpha PostgreSQL
  backup helper. Stock `pg_dump` / `pg_restore` / `gzip` only;
  no auto-restore; refusal-list DSN policy mirrors
  `bootstrap_alpha_db.py`.
- `tools/build_public_export.py` — reproducible clean-history
  public-export builder. Reads a curated allowlist of relative
  paths from the source tree, copies them into a fresh output
  directory, writes two machine-verifiable manifests (tree +
  sha256), then initializes a brand-new Git repository with a
  single initial commit. Allowlist is mandatory; nothing outside
  the allowlist is exported.
- `tools/scan_release_tree.py` — local secret/privacy scanner for
  a release tree. Walks a directory, applies a curated set of
  pattern groups, and emits a machine-readable JSON report
  (including a `real_secret_count`). Values are redacted; no
  network calls.
- `tools/release.gitignore` — gitignore template for the
  clean-history export output directory.

### Release changes

- Reworked the first-screen README and the public-alpha documents so the
  supported surface, configuration, privacy, backup/restore, and known limits
  match the current tree.
- The candidate adds an additive, version-controlled PostgreSQL/pgvector bootstrap:
  `src/v3-core/schema/alpha_bootstrap.sql` plus the explicit
  `src/v3-core/scripts/bootstrap_alpha_db.py` entry point. It is not applied
  implicitly by runtime startup and does not recreate legacy tables.
- Added the standard `pg_dump` backup helper and focused bootstrap contract
  tests. The clean-history export builder and local secret scanner are private
  release-preparation tooling and are not part of the public export.
- The adapter exposes the existing 13 host tools; no new plugin tool was
  introduced.
- No production deployment, historical data migration, or production
  schema change is part of this release. Public publication is represented
  by the clean-history repository and the `v0.1-alpha` prerelease.

### Notes on env-var mapping

- The `V3CORE_PG_PASSWORD` env var is read by the engine runtime via
  `v3core.config._apply_pg_password_env` and is declared under
  `requires_env` in `src/v3-hermes-plugin/plugin.yaml`. This mapping
  is an engine-runtime feature predating this release; the change in
  this release is the docs + manifest declaration, not the engine
  resolution itself. Do not characterize this release as having
  *added* the env-var resolution.

### Evidence boundary

- Claims in the public documents are limited to the disposable Windows
  / Python 3.11 / pgvector evidence recorded for this release.
- The focused acceptance scope for this release is **191 passed**,
  run file-by-file to avoid known order contamination. Unsupported
  observer/LLM synthesis, host-networked providers, and historical
  evaluation paths remain labeled experimental, internal, legacy, or
  unknown.

### Not in this release

- No Recall V2, QueryPlan, temporal recall, multi-agent, multi-writer, or
  historical active-memory migration.
- No deletion of legacy subsystems or historical data is part of this
  release; the public tree is a separate allowlisted clean-history export.
- No package-index publication or production Hermes deployment. The GitHub
  prerelease is the public release channel for this technical preview.

### Known caveats

- The six source-ingest preservation files were re-validated
  independently on this HEAD to avoid known order contamination:
  `test_sync_turn_qa.py` (9), `test_sync_turn_timestamp_parse.py` (4),
  `test_live_buffer_pool_owner.py` (13),
  `test_compression_ingest_durability.py` (19),
  `test_pg_store_timestamp_normalization.py` (15), and
  `test_llm_observability.py` (12). The focused acceptance total also
  includes `test_import_hermes_state.py` (3).
- The fresh export stranger smoke produced `conversation_stream=2` and
  `qa_pairs=0` after one direct `sync_turn`; it intentionally did not
  trigger a separate QA flush or observer/E1 cycle. QA pairing and
  ingest preservation are covered by the current focused files listed
  above.
- Items still outside the public-alpha supported surface are recorded in
  [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md) § "What
  the public-alpha does not close" and in
  [`docs/RELEASE-CHECKLIST.md`](docs/RELEASE-CHECKLIST.md)
  § P–S (POST-ALPHA / STABLE).

---

## Historical context (preserved, not the current contract)

The following pre-alpha items are **not** part of the supported
public-alpha surface and are listed only for readers who have
historical context documents:

- A pre-alpha product matrix with tiered offline / BYOK / local
  stack labels. That product framing has been retired; the
  supported public-alpha surface is described in
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md).
- A 31-tool plugin surface. Narrowed to 13 in 2026-08-07; the
  supported alpha surface is the 13-tool surface.
- A `hermes memory setup` wizard with auto-config scaffolding. The
  supported alpha flow is the manual `config.yaml` + local-source
  `pip install` flow described in
  [`docs/INSTALL.md`](docs/INSTALL.md).
- A `b_*` / `shou_*` / SQLite-mirror active-memory path. The
  supported alpha surface writes only to `public.explicit_memories`
  via `v3core.active_memory_store.ActiveMemoryWriter`.
- Production infrastructure and credentials are outside this release; the
  supported surface assumes a fresh disposable container with locally
  supplied placeholder credentials.
