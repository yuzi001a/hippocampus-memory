---
name: v3-workflow
description: deep_memory_v3 插件操作手册 — 31 工具 / PG 实时记忆链路 / 压缩-聚类-印 / 导入管线
---

# v3 插件工作流

> 对齐当前架构 — PG 实时链路（conversation_stream → qa_pairs → 压缩 → topics → E1），
> cron 已停（内部调度 + sync_turn 实时触发），j/ 文件层断更（仅冗余副本）。

## 0. 核心入口（推荐）

| 工具 | 干什么 |
|------|--------|
| `v3_status` | 卡库统计 + PG 服务状态 |
| `v3_search` | 语义搜索卡库（RRF: keyword + vector） |
| `v3_store` / `v3_add` / `hm_write` | **OPT-IN 显式写卡** — 仅在用户明确要求记住/存入/保留某条具体持久项，或显式授权的宿主工作流（handbook 同步、种子导入等）请求时调用。每次调用都向 `public.explicit_memories` 落一行；非授权场景（开发经验、评审发现、调试笔记、任务状态/摘要、实现决策、推断出的偏好/事实、泛泛经验、"今晚总结一下"等）一律不得调用。 |
| `v3_extract` | 对话文本提卡（多专家模式 experts=decisions/lessons/projects/system）。**默认 write=False（仅预览，不落库）；write=True 等同 v3_store 的 opt-in 授权门槛**。 |
| `v3_prefetch` | 手动召回验证（format=json/context/ids/chain） |
| `v3_import_seed` / `v3_import_full` | 记忆文件导入 / 历史数据全量导入（显式授权的宿主工作流路径） |
| `hm_get` / `hm_status` | handbook 读 / 状态 |
| `v2_topic_create` / `v3_topic_correct` | 主题卡手工维护 |

### 0.1 显式记忆 opt-in 契约

`public.explicit_memories` 是**opt-in 用户/宿主拥有的 canonical 记忆**。下面这条契约同时覆盖 `v3_store` / `v3_add` / `hm_write` / `v3_extract(write=True)` / `v3_manage(action="extract", write=True)` 五个写入入口，文档口径与 `docs/WRITE-FLOW-CONTRACT.md § A0` 一致：

- **允许调用**：用户明确说"记住 / 存下 / 保存 / 保留"某条具体持久项；或显式授权的宿主工作流（handbook 同步、种子导入、用户主动存档等）请求写入。
- **禁止调用（不构成授权）**：
  - 开发经验、评审发现、调试笔记
  - 任务状态 / 任务摘要 / "今晚总结一下"
  - 实现决策、推断出的偏好或事实
  - 任何"通用 lesson"、"通用 best practice"
  - 任何被动观察到的会话流量（这些走 sync_turn / observer / E1 / topics 被动路径，绝不静默升级为显式记忆）
- **被动路径对 explicit_memories 无 DML**：sync_turn / observer / E1 / topic_store / dedup 等对 `public.explicit_memories` 不做 INSERT / UPDATE / DELETE / MERGE。active-memory 真值只在受支持的主动写入入口下被改变。

调用 `v3_store` / `v3_add` / `hm_write` / `v3_extract(write=True)` 之前，先问自己：用户是否**明确**说了要保存这条具体内容？没有就停下来，不要写。
