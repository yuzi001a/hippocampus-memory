# 怎么选择 Agent Memory 方案

Hippocampus 不是给 Agent 做跨 Session 连续性的唯一方法。下面这份对比尽量只说各自适合什么，不把“不同设计”写成“谁一定更好”。

## 一句话版本

| 方案 | 更适合 | 主要取舍 |
|---|---|---|
| Hermes 自带 `MEMORY.md` / `USER.md` | 少量关键事实始终进入上下文 | 容量刻意受限，不是通用长期记忆引擎 |
| Session / 历史搜索 | 精确找回“以前哪次对话说过什么” | 搜得到历史，不等于系统会形成、维护长期记忆 |
| Mem0 | 希望快速接入一个成熟、通用、集成面广的 Memory Layer | 产品面很大，source / derived 的设计取舍与 Hippocampus 不同 |
| Hindsight | 需要 retain / recall / reflect、图关系和时间检索等成熟能力 | 系统更完整也更重；写入后会进入自己的事实/观察模型 |
| Hippocampus | 看重 local-first、原始来源持久化、派生记忆可重建，以及“经历/解释”分层 | 目前还是 v0.1-alpha，自动记忆和集成广度都没有成熟项目完整 |

## Hermes 自带 Memory

Hermes 自己已经有一套很实用的持久记忆：`MEMORY.md` 保存 Agent 的关键笔记，`USER.md` 保存用户画像，两者都被严格限制大小，并在 Session 开始时进入 System Prompt。Hermes 还提供 `session_search`，用于按需搜索全部历史 Session。

如果你只是希望 Agent 永久记住少量稳定事实，这通常是最简单的方案：透明、便宜、几乎没有额外基础设施。

Hippocampus 更适合另一类需求：希望长期保存更完整的来源历史，把来源和后来的总结分开，派生状态可以重新生成，并继续实验更复杂的记忆形成机制。

Hermes 官方文档：<https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md>

## Mem0

Mem0 把自己定位为面向 AI Agent 和应用的通用 Memory Layer。目前既可以作为 Python / Node Library 使用，也提供自托管 Server 和托管平台，并且已经有很广的框架与 Coding Agent 集成。

如果你最看重成熟度、接入方式和已有生态，Mem0 是非常自然的候选。

Hippocampus 目前走的是更窄的 Public Alpha 路线。它更强调原始 source 的持久化、显式记忆的 canonical 存储、派生状态可重建，以及“真正发生过的事情”和“模型后来对它的解释”不要混成唯一事实源。

这是架构取舍，不代表 Hippocampus 当前比 Mem0 更成熟。

Mem0：<https://github.com/mem0ai/mem0>

## Hindsight

Hindsight 的核心接口是 retain、recall 和 reflect。公开文档里，它会把写入内容处理成 facts / experiences / observations，并使用 semantic、BM25、graph、temporal 等多种方式组合召回；同时已经有很多 Agent、Coding Agent 和 MCP 方向的集成。

如果你现在就希望得到一套比较完整的“写入—召回—反思”记忆栈，尤其重视时间和图关系检索，Hindsight 很值得直接评估。

它与 Hippocampus 一个比较重要的区别在 source 处理。Hindsight 文档明确说明 retain 会抽取 facts、entities、relationships，原始输入不会作为主要 memory representation 原样保存。Hippocampus 则有意把原始 conversation/source 作为长期来源，把 Topic、Observer、Embedding、摘要、人格层等视为可以重建和修改的派生理解。

Hindsight：<https://github.com/vectorize-io/hindsight>

## Hippocampus 当前真实做到哪一步

v0.1-alpha 已经有当前证据支持的主要路径包括：

- `conversation_stream` 原始对话来源持久化；
- PostgreSQL 中的显式 canonical memory 写入；
- 关键词召回；
- 进程重启后的读回；
- PostgreSQL backup / restore 后继续写入和召回；
- 当前 Hermes adapter 的工具和生命周期契约。

仓库中也已经存在 Observer、E1 / identity synthesis、Topic Card、vector / rerank 等代码和设计，但其中若干自动记忆路径并不属于当前 frozen Public Alpha 已经完整 E2E 验证的范围。

准确边界请看 [`PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)。

## 怎么选最简单

如果少量关键事实就够，先用 Hermes 自带 Memory。

如果你主要是“我要找回上个月那次聊天”，先用 Session / History Search。

如果你需要一个成熟、通用、集成非常广的 Memory Layer，先评估 Mem0。

如果你需要已经发展得比较完整的 retain / recall / reflect，以及 temporal / graph retrieval，先评估 Hindsight。

如果下面这些问题本身就是你关心的东西，再试 Hippocampus：

- 即使未来记忆算法完全换掉，我还想保留真正发生过的原始经历吗？
- 派生记忆应该允许重新计算，而不是变成唯一真相吗？
- 我是否需要明确区分“发生过什么”和“系统后来认为这意味着什么”？
- 我关心的不只是搜索，而是长期记忆究竟如何参与 Agent 的连续性吗？

## Hippocampus 当前不声称什么

Hippocampus 现在不是上述成熟系统的全功能 drop-in replacement，也不是 production-ready 产品。它的集成数量、安装体验和自动记忆稳定性都还有明显差距。

这个项目宁愿把这些差距公开写出来，也不把 roadmap 当成已经完成的能力。

想理解为什么会形成这样的设计，可以继续看 [`WHY_HIPPOCAMPUS.md`](WHY_HIPPOCAMPUS.md)。想实际跑几天，可以看 [`ALPHA-TESTING.zh-CN.md`](ALPHA-TESTING.zh-CN.md)。
