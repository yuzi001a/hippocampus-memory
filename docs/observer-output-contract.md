# Observer 输出约束分层（2026-11-21）

把 Codex memories/write 与 Hermes context compressor 的约束手法拆开看，落到我们自己的
observer 写入链路上。结论先行：**我们现在的 observer 只有第 1 层（提示词），第 2、3、4 层
基本空缺，所以产出质量随模型当次状态漂移。**

## 0. 四层模型

| 层 | 执行者 | 能保证 | 不能保证 | 我们的现状 |
|---|---|---|---|---|
| 1 契约 | 提示词 | 模型理解要写什么 | 每次遵守、写多少 | ✅ 很重（4k 字符） |
| 2 校验 | 代码 | 缺项/越界/格式错会被发现 | 内容正确 | ⚠️ 只有 JSON 可解析 + 审计告警（不处置） |
| 3 兜底 | 代码 | 失败时仍有结构合格的产物 | 内容质量 | ❌ 无（空输出直接 return，游标不动） |
| 4 重建 | 代码 | 可从原始 QQ 重放、可复现 | 单次完美 | ⚠️ 有回放脚本，无自动比对与校验门 |

## 1. 参考实现的机制清单（已验证 file:line）

### Codex memories/write

- **provider 级 strict JSON Schema**：`phase1.rs:319-320`（`output_schema_strict = true`），
  客户端只 `serde_json::from_str` 一次，结构体 `deny_unknown_fields`
  （`phase1_output.rs:18/27`）。无 markdown 抠 JSON、无 in-turn 修复。
- **解析失败 = job 失败**：1 小时后重试，初值 3 次（`memories.rs:29/1030-1066`），
  耗尽 `SkippedRetryExhausted`。
- **空输出有明确语义**：`succeeded_no_output`，并删除该 thread 旧 stage1 行
  （`phase1.rs:228-239`、`memories.rs:947-955`）。
- **确定性后处理**：三字段 `redact_secrets`；V2 summary 硬截 9,000 字节
  （`phase1_output.rs:49-52`，掐中间保头尾）。
- **文件名/ID 由代码生成**：`rollout_summary_file_stem`（`storage.rs:153`），
  uuid + 4 字符哈希 + 消毒 slug，slug ≤ 60 字节（`:166`）→ 指针精确性成为结构不变量。
- **产物验收**：`is_valid_v2_summary`（`workspace.rs:118-126`）——首行 `v1`、
  `< 10_000` 字节、`## User Profile / ## User preferences / ## General Tips /
  ## What's in Memory` 四个标题齐；验收失败 `failed_invalid_artifacts`，
  不 reset baseline。
- **输入分层预算**：Human > Final > OtherAgent > Commentary > Context > Tool，
  每行 10,000 字节、工具输出 2,000 tokens，从新到旧填，插 `[... omitted ...]`
  （`rollout_input.rs:94-102/267-308`）。
- **幂等重建**：每次 phase2 从 DB 全量重建 `rollout_summaries/` 并 prune
  （`phase2.rs:210-225`、`storage.rs:23-42`）。
- **只在提示词里**：指针必须精确、`MEMORY.md` 章节结构、内容质量 —— 代码不验。

### Hermes context compressor

- **阈值与保留由代码算**：`_compute_threshold_tokens:2765`、`tail_token_budget:2219`、
  `_find_tail_cut_by_tokens:4973`、`_protect_head_size:4571`。
- **出口四道校验**：空内容、拒答、`finish_reason=length` 都抛错
  （`context_compressor.py:3848-3869`）。
- **反增长拒绝提交**：`_salvage_or_refuse_grown_transcript`
  （`conversation_compression.py:3112-3166`）——压缩后比原来大 → 机械 salvage 一次 →
  仍大就拒绝，原 transcript 不动。
- **代码重写模型的一节**：`## Historical Task Snapshot` 由代码从真实用户轮生成
  （`:4380`）；无真实用户轮时必须逐字等于哨兵串，否则抛错（`:4260-4271`）——
  「不许编造用户说过什么」的代码级保证。
- **三级降级**：摘要模型失败 → 主模型重试一次（`:3760-3783`）→ fallback 链
  （`:929-970`）→ 确定性摘要 `_build_static_fallback_summary:3501`（不调模型，
  本地抽锚点，硬切 8,000 字符）。
- **明确不设 max_tokens**（`:3798-3799`）：成功路径的长度只靠提示词 + 反增长护栏。

## 2. 落到 observer：缺口与处置

| 缺口 | 现状 | 学谁 | 处置 |
|---|---|---|---|
| 空输出 | warn + return，游标不动 → 回放原地卡死 | Codex `succeeded_no_output` | 先重试一次（带纠正提示）；仍空且批 ≤5 视为无变化，推进游标并记审计；批 >5 走拆批 |
| 语义不合格（缺状态词/缺节） | 只审计告警 | Hermes 出口校验 | 校验判定 `repairable` → 带审计清单重试一次；仍不合格则写入 + 计数打标（不替模型补写） |
| 结构不合格（无节/锚点越界/重复块） | 不检查 | Codex `is_valid_v2_summary` | `invalid` → 重试 → 仍错走自适应拆批 |
| 兜底 | 无 | Hermes 静态摘要 | 重试两次仍失败且批 >5：生成**机械摘要**（只引用真实用户原话与错误行，带 `[#N]`），不编造断言 |
| 反增长 | 无 | Hermes 反增长 | ✅ 已实现：`_note_overlap_with_prev()` 字符 6-gram 包含度；>0.5 判 `repetitive` → 修复轮；阈值依据 615 对相邻印实测（中位 0.08 / p99 0.19 / 最大 0.27） |
| 幂等重建 | 有回放脚本，无比对 | Codex 全量重建 + prune | 回放后逐条比对 QA 覆盖与区间连续性，缺失段落表 |
| 产物验收 | 无 | Codex 产物校验 | 写库前过 `validate_note_payload`，不合格不静默写入 |

## 3. 实施顺序

1. **`validate_note_payload` + 处置状态机**（本轮）：`ok / no_output / repairable / invalid`，
   每种有明确动作；空输出不再阻塞游标。
2. **确定性兜底摘要**（本轮）：机械摘要，只引用原文，不生成断言。
3. ~~**反增长检查**~~（已完成 2026-11-21）：与 prev 的 6-gram 包含度，>0.5 判重复并修复一轮。
4. **重建比对工具**（未做）：回放后自动比对区间覆盖、锚点范围、缺失段。
5. **确定性兜底摘要**（未做）：模型连续失败时用原文机械拼一份，不生成任何断言。

## 4. 不变式（写进代码，不写进提示词）

- `source_qa_range` 永远由代码从真实 QA id 算，不信模型输出（已如此）。
- 锚点 `[#N]` 的 N 必须落在 `1..len(batch)`，越界视为结构错。
- 空输出必须推进游标或显式拆批，**不允许原地 return**。
- 校验失败必须留下可数的记录（日志 + 计数），不允许静默通过。
- 任何「替模型补写内容」的行为都禁止：缺状态词就缺着，计数上报。
