# Hippocampus（海马）

> **Give an agent a past.**

Hippocampus（海马）是一个面向 AI Agent 的开源、local-first 长期记忆运行时。它保存可追溯的对话来源，在后续会话中自动召回相关过去，并尽量让“这段记忆到底来自哪里”始终可查。

**当前状态：**公开 Alpha，持续开发中。当前 `main` 已包含 **DeepSeek Harness（DSH）** 和 **pi** 的自动记忆适配。

[English](README.md) · [安装](docs/INSTALL.zh-CN.md) · [文档导航](docs/README.md) · [当前状态](docs/STATUS.md) · [为什么做 Hippocampus](docs/WHY_HIPPOCAMPUS.md)

---

## 为什么做这个项目

最开始的问题很简单：一个 Agent 可以和你一起工作很久，逐渐理解项目背景、习惯和上下文；但一旦进入新 Session，很多连续性又会消失。

后来真正困难的问题变成了：

> **遗忘很麻烦，但记错可能更糟。**

所以长期记忆不能只是“把聊天历史存起来”，也不能只是“做一个向量数据库”。它还需要处理来源、重复写入、纠错、更新、重建，以及“系统后来形成的判断”和“当时真正发生的事情”之间的区别。

Hippocampus 一直坚持一个原则：

> **过去应该保存，但过去的解释必须允许改变。**

更完整的项目思路见 [WHY_HIPPOCAMPUS.md](docs/WHY_HIPPOCAMPUS.md)。

---

## 现在已经能做什么

### 在真实 Agent 宿主中自动记忆

| 宿主 | 当前支持 | 已验证范围 |
| --- | --- | --- |
| **DeepSeek Harness（DSH）** | **AUTO** — 自动记录用户/助手消息 + 当前轮自动召回 | 已按 `@deepseek-ai/dsh@0.2.0-rc.2` 当前上游契约开发并验证。真实 DSH + B01 bridge + 一次性 PostgreSQL 集成：**18/18 checks PASS**。 |
| **pi** | **AUTO** — 自动记录持久化消息 + 每次用户输入至多一次自动召回 | 已在 pi **0.99.2** / Node **24.21.0** 上验证真实加载、隔离 PostgreSQL 写入、召回、每轮 latch 和 source trace。 |
| **Hermes Agent** | Provider / 工具集成 | 已有 `v3-hermes-plugin` 适配器和公开工具面；验收边界与当前 DSH/pi 自动适配不同，详见插件 README。 |

对应文档：

- [DSH adapter](packages/dsh-adapter/README.md)
- [pi adapter](packages/pi-adapter/README.md)
- [Hermes plugin](src/v3-hermes-plugin/README.md)

### 核心能力

当前 core 已具备支撑这些宿主适配的关键能力：

- 对话来源持久化；
- canonical 事件身份和重放幂等；
- PostgreSQL + pgvector 存储；
- 关键词召回和可选向量召回；
- 显式记忆存储；
- 召回引用反查原始来源；
- fresh bootstrap、备份与恢复；
- 宿主侧 fail-open：记忆挂了，Agent 本身仍应继续工作；
- provider 本地配置、按需启用。

仓库中还存在 Observer、Topic、Yin/E1、Journal 等派生记忆路径，但并不是每一条内部能力都属于当前对外支持面。当前边界见 [STATUS.md](docs/STATUS.md)。

---

## 架构怎么理解

```text
Agent 宿主 / Session
DSH · pi · Hermes · ...
        │
        ▼
薄宿主适配器
capture · recall · fail-open
        │
        ▼
B01 bridge contract
        │
        ▼
v3-core
ingest · recall · provenance
        │
        ├── 原始来源：conversation / explicit memory
        └── 派生记忆：embeddings / topics / notes
        │
        ▼
PostgreSQL / pgvector
```

这里最重要的区别是：

- **来源数据要尽量长期保存。** 对话原文、显式记忆是“发生过什么”的依据。
- **派生理解可以重建。** Embedding、Topic、摘要和高层记忆可以随着算法变化重新生成。
- **召回结果要能追溯。** 记忆不应该只给你一段看起来很像真的文本，而应该能继续找到它背后的来源。

---

## 快速开始

Hippocampus 目前仍是 Alpha，因此暂时不把安装压缩成一个“万能一键命令”。

建议顺序：

1. 按 [INSTALL.zh-CN.md](docs/INSTALL.zh-CN.md) 安装和 bootstrap core；
2. 按 [CONFIGURATION.md](docs/CONFIGURATION.md) 配置存储和 provider；
3. 选择 Agent 宿主：
   - [DeepSeek Harness](packages/dsh-adapter/README.md)
   - [pi](packages/pi-adapter/README.md)
   - [Hermes Agent](src/v3-hermes-plugin/README.md)
4. 先检查 backend/adapter 状态，再用于重要工作；
5. 使用前阅读 [KNOWN-LIMITATIONS.md](docs/KNOWN-LIMITATIONS.md)。

升级与恢复：

- [备份与恢复](docs/BACKUP-RESTORE.md)
- [升级说明](docs/UPGRADE.md)

---

## “自动记忆”是什么意思

对目前的 DSH 和 pi 适配器，目标体验是：

```text
Session A
  你正常讨论一个项目决定、事实或上下文。
  Hippocampus 自动记录宿主真实持久化消息。

新 Session
  你自然地问起之前的事情。
  Adapter 自动触发 recall。
  相关记忆被加入当前请求。
  召回引用仍可继续追到来源。
```

正常路径不要求用户手动调用 `remember` 或 `search`。

Adapter 保持尽量薄：宿主生命周期逻辑留在 adapter，记忆身份、持久化和召回规则留在 core。

---

## 可靠性原则

- **Source first。** 不能为了 embedding/index 限制而截断或替换权威原始来源。
- **Durability before derivation。** 原文写成功和 embedding/摘要成功是两件事。
- **Canonical identity。** host/session/event identity 是重试和重放的幂等边界。
- **Host fail-open。** Hippocampus 出问题时，宿主 Agent 仍应继续工作。
- **Isolation fail-closed。** 测试/恢复环境配置丢失时不能悄悄落回生产路径。
- **Evidence before claims。** “代码里有”不等于“已经支持”。

工程细节集中在 [docs/reliability](docs/reliability/) 和 [文档导航](docs/README.md) 的开发档案区。

---

## 评测

Hippocampus 做过 LoCoMo 长期记忆基准测试，但这里只把它当作研究信号，不把 benchmark 分数等同于真实使用质量。

历史评测：

- Hippocampus：**1077.5 / 1540 = 69.97%**
- 无长期记忆基线：**6.85%**
- Gold context 参考：**75.84%**

完整协议和限制见 [locomo-recall-v2.md](docs/evaluation/locomo-recall-v2.md)。

---

## 当前限制

这是公开 Alpha，而不是稳定生产版。目前尤其要注意：

- 暂不声称 production-ready；
- 当前第一方真实宿主验收仍明显偏 Windows；
- pi 的真实模型 A→B 仍属于可选 smoke，专用测试机上没有配置模型凭据，因此没有执行；
- DSH 的宿主/bridge/召回链路已经真实跑通，但模型端使用本地无凭据 stub 做结构验证；
- 当前宿主适配以文本记忆为主；
- 下一阶段重点已经从“再接更多宿主”转向“记忆管理、纠错、时间变化和用户可控性”；
- 包发布与版本兼容承诺仍比源码能力更窄。

维护中的最新状态见 [STATUS.md](docs/STATUS.md)。旧的 Public Alpha 验收文件会保留作为工程历史，但不再作为当前产品首页。

---

## 文档导航

建议从 [docs/README.md](docs/README.md) 开始。

| 需求 | 文档 |
| --- | --- |
| 安装 | [INSTALL.zh-CN.md](docs/INSTALL.zh-CN.md) |
| 配置 | [CONFIGURATION.md](docs/CONFIGURATION.md) |
| 当前支持 / 测试版本 | [STATUS.md](docs/STATUS.md) |
| 为什么做这个项目 | [WHY_HIPPOCAMPUS.md](docs/WHY_HIPPOCAMPUS.md) |
| 架构 | [ARCHITECTURE-OVERVIEW.md](docs/ARCHITECTURE-OVERVIEW.md) |
| 隐私与数据流 | [PRIVACY-DATA-FLOW.md](docs/PRIVACY-DATA-FLOW.md) |
| 与其他方案比较 | [COMPARISON.zh-CN.md](docs/COMPARISON.zh-CN.md) |
| 已知限制 | [KNOWN-LIMITATIONS.md](docs/KNOWN-LIMITATIONS.md) |
| 备份恢复 | [BACKUP-RESTORE.md](docs/BACKUP-RESTORE.md) |
| 升级 | [UPGRADE.md](docs/UPGRADE.md) |

---

## 接下来做什么

DSH 和 pi 的自动记忆适配阶段已经完成。接下来主线回到“记忆产品本身”：

- 让用户更容易查看和管理已经形成的记忆；
- 把“纠正旧事实”“后来发生变化”做成一等能力；
- 强化时间关系：过去正确的事实，不等于今天仍正确；
- 在派生理解不断变化时继续保留来源和可追溯性；
- 再根据真实使用情况扩展新的 Agent 宿主。

当前维护中的方向见 [STATUS.md](docs/STATUS.md)。

---

## 贡献

欢迎 Issue 和 Pull Request。修改存储、source identity、召回或宿主契约前，请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

安全问题请按 [SECURITY.md](SECURITY.md) 说明提交。

## License

MIT — 见 [LICENSE](LICENSE)。
