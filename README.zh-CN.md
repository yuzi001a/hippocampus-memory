<div align="center">

# Hippocampus（海马）

### Give an agent a past.

**让 AI Agent 拥有跨 Session 的长期记忆。Local-first、可追溯，并且尽量不把“记忆”伪装成“事实”。**

[![Status](https://img.shields.io/badge/status-public%20alpha-orange)](docs/STATUS.md)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![DSH](https://img.shields.io/badge/DeepSeek%20Harness-auto%20memory-success)](packages/dsh-adapter/README.md)
[![pi](https://img.shields.io/badge/pi-auto%20memory-success)](packages/pi-adapter/README.md)

[开始安装](docs/INSTALL.zh-CN.md) · [English](README.md) · [它是怎么工作的](docs/ARCHITECTURE-OVERVIEW.md) · [当前状态](docs/STATUS.md)

</div>

---

## 一个长期合作的 Agent，不该每次醒来都像陌生人

你可能已经和一个 Agent 一起工作了几天、几周，甚至更久。

它知道项目做到哪了，知道之前为什么这么决定，也逐渐知道你喜欢什么样的工作方式。

然后，新 Session 开始了。

很多东西又得从头讲。

Hippocampus 想解决的就是这件事：把真正发生过的对话保存下来，在后续会话需要的时候自动找回来，而且尽量保留“这段记忆到底来自哪里”的线索。

```text
Session A
你：这个项目内部代号叫“北斗七号”。

        ↓  Hippocampus 记住了

新的 Session
你：之前那个项目代号是什么？

Agent：北斗七号。
       ↳ 来自之前那次对话
```

在已经支持自动记忆的宿主里，正常使用不需要手动调用 `remember` 或 `search`。

---

## Hippocampus 想解决的，不只是“存聊天记录”

### 🧠 让记忆跨 Session 留下来

它可以自动记录真实的用户/助手消息，并在后续对话需要时把相关过去召回到当前上下文。

### 🔎 一段记忆，应该还能找到它从哪来

Hippocampus 不希望长期记忆最后变成一堆“看起来像真的总结”。

召回出来的内容应该尽量保留来源关系，必要时能继续追到原始对话。

### ♻️ 过去要保存，但对过去的理解可以改变

原始历史和后来的总结、Embedding、Topic、观察笔记不是一回事。

**来源尽量不动，解释允许重建。**

因为遗忘很麻烦，但“非常自信地记错”可能更糟。

### 🏠 Local-first

长期记忆数据由你自己控制。Embedding、rerank、LLM 等外部 provider 都是显式配置、按需启用。

### 🧩 不绑死一个 Agent

记忆核心和宿主适配分开。不同 Agent 通过薄 adapter 接入同一套记忆能力。

---

## 目前支持哪些 Agent

| Agent 宿主 | 当前体验 |
| --- | --- |
| **DeepSeek Harness（DSH）** | **自动记忆。** 自动记录用户/助手消息，并在当前轮自动召回相关历史。 |
| **pi** | **自动记忆。** 自动记录已持久化消息，每次用户输入最多自动召回一次。 |
| **Hermes Agent** | 已有 Provider / 工具集成，通过 `v3-hermes-plugin` 使用。 |

当前实测版本、证据范围和已知限制统一放在 [STATUS.md](docs/STATUS.md)，首页不再堆验收报告。

---

## 旧聊天也不应该白白丢掉

如果一个长期记忆插件只能“从今天开始记”，那它对很多老用户的价值会打折。

Hippocampus 目前已经具备：

- **Hermes 历史会话导入**：支持 `state.db`、JSONL、JSON；
- **用户整理的记忆文件导入**：支持 `MEMORY.md`、`USER.md`、`SOUL.md`、`AGENTS.md` 等 Markdown。

下一步正在补的是更完整的 **旧消息一键迁移**：自动发现本机已有 Agent 历史，并继续补 DSH / pi 的旧会话导入。

---

## 一张图看懂

```text
             你的 Agent
        DSH · pi · Hermes
               │
        真实对话 / 宿主事件
               │
               ▼
        ┌───────────────┐
        │  Hippocampus  │
        │               │
        │   记录过去    │
        │   找回过去    │
        │   追溯来源    │
        └───────┬───────┘
                │
        ┌───────┴────────┐
        │                │
        ▼                ▼
     原始来源          派生记忆
    发生了什么        可能意味着什么
        │                │
        └───────┬────────┘
                ▼
            未来 Session
```

Hippocampus 最重要的区分其实就一句话：

> **当时发生了什么、系统后来怎么理解、模型此刻相信什么，不是同一件事。**

这也是整个项目后面很多设计的出发点。

---

## 怎么开始

Hippocampus 目前还是 **Public Alpha**，所以安装方式还没有包装成真正意义上的“一条命令全部完成”。

**第一步：安装 Core**

按 [中文安装说明](docs/INSTALL.zh-CN.md) 完成核心环境和存储初始化。

**第二步：选择你的 Agent**

- [DeepSeek Harness](packages/dsh-adapter/README.md)
- [pi](packages/pi-adapter/README.md)
- [Hermes Agent](src/v3-hermes-plugin/README.md)

**第三步：正常使用，然后在新的 Session 里问起以前的事情**

如果召回命中，Hippocampus 会把相关过去带回当前上下文，并保留来源关系。

其他说明：

[配置](docs/CONFIGURATION.md) · [备份与恢复](docs/BACKUP-RESTORE.md) · [升级](docs/UPGRADE.md)

---

## 为什么不直接用向量数据库？

因为长期记忆的问题远不只是“相似度搜错了一个 chunk”。

真正长期使用后，还会遇到：

- 原始聊天到底有没有完整保存；
- 重试以后是不是同一句话存了两遍；
- 一个事实后来变了，旧事实怎么办；
- 摘要会不会越总结越偏；
- 换模型以后，派生记忆能不能重建；
- 记忆后端挂了，Agent 会不会跟着一起挂。

所以 Hippocampus 把向量召回看成一种工具，而不是“长期记忆”的全部定义。

---

## 它不是从 benchmark 开始的

这个项目最开始来自真实的长期 Agent 使用。

最初的问题是：

> “怎么让它别忘？”

后来慢慢变成：

> **“怎么让它记住，同时别把记忆和证据混成一件事？”**

所以接下来的重点，也不会只是继续增加更多检索技巧，而是：

**能不能看见 Agent 记住了什么；能不能纠正；一个曾经正确、后来变化的事实应该怎么处理。**

完整的思路演变见 [为什么做 Hippocampus](docs/WHY_HIPPOCAMPUS.md)。

---

## 项目现在走到哪了

**已经完成：**核心持久化 · 安装/bootstrap · 统一 bridge · pi 自动记忆 · DSH 自动记忆 · source trace · 可靠性加固。

**现在：**旧消息一键导入。

**下一步：**记忆查看和纠错，再进入时间记忆——解决“以前是真的”和“现在还是真的”之间的区别。

维护中的技术状态见 [STATUS.md](docs/STATUS.md)。

---

## 评测

Hippocampus 也跑过 LoCoMo 长期记忆评测，作为研究信号：

| 设置 | 得分 |
| --- | ---: |
| Hippocampus | **69.97%** |
| 无长期记忆 | **6.85%** |
| Gold context 参考 | **75.84%** |

Benchmark 不等于真实长期使用质量。完整协议和限制见 [LoCoMo 评测](docs/evaluation/locomo-recall-v2.md)。

---

## 文档

**想用起来：** [安装](docs/INSTALL.zh-CN.md) · [配置](docs/CONFIGURATION.md) · [当前状态](docs/STATUS.md) · [已知限制](docs/KNOWN-LIMITATIONS.md)

**想研究实现：** [架构](docs/ARCHITECTURE-OVERVIEW.md) · [隐私/数据流](docs/PRIVACY-DATA-FLOW.md) · [完整文档索引](docs/README.md)

**想知道为什么做：** [WHY_HIPPOCAMPUS](docs/WHY_HIPPOCAMPUS.md) · [与其他方案比较](docs/COMPARISON.zh-CN.md)

---

## Public Alpha

Hippocampus 仍在快速开发。当前一手验收明显偏 Windows，安装/分发体验还在简化，也不是仓库里所有实验性路径都已经属于对外支持能力。

这个首页只写我们目前愿意承担的产品承诺，不把“代码里存在”自动等同于“已经支持”。

---

## 参与项目

欢迎 Issue 和 Pull Request。开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题见 [SECURITY.md](SECURITY.md)。

MIT License — 见 [LICENSE](LICENSE)。

<div align="center">

**Give an agent a past — 但别让它忘记，这段过去是从哪里来的。**

</div>
