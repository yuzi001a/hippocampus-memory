# Hippocampus（海马）

> **Give an agent a past.**

**Hippocampus v0.1-alpha** · 技术预览版（Technical Preview）/ 公开 Alpha

Hippocampus（海马）是一个面向 AI Agent 的开源、local-first 长期记忆运行时。它负责可靠保存对话来源和显式记忆，并在后续会话中检索与当前问题相关的内容。

这个项目最初并不是从“做一个更好的向量数据库”开始的，而是从一个很朴素的问题开始：一个 Agent 可以和你一起工作很久，理解一个项目、形成共同背景，甚至逐渐表现出某种稳定的人格；但新的 Session 开始以后，大量连续性又会消失。最开始，我只是想让 Agent 记住过去。后来随着对大模型的理解不断变化，这个问题逐渐变成了另一个问题：**如果生成即是存在，那么过去怎样参与下一次生成？**

这也是 Hippocampus 今天仍然在研究的事情。完整的项目思想演变见 [`docs/WHY_HIPPOCAMPUS.md`](docs/WHY_HIPPOCAMPUS.md)。

Hippocampus 是 V3 memory runtime 的公开产品名。仓库中的 Python 包、CLI、配置键和工具名仍使用现有的 `v3-core`、`v3-hermes-plugin`、`v3-core info` 等名称；本版本不因品牌展示而重命名这些内部接口。

[English](README.md) | 简体中文

> **版本定位：** v0.1-alpha 是技术预览版，不是稳定生产版本。本文把“已经在公开 Alpha 验收中验证过的路径”和“代码中存在、但仍处于实验或未完成验收的路径”分开写。

### 快速入口

- **想知道为什么做这个项目：** [`docs/WHY_HIPPOCAMPUS.md`](docs/WHY_HIPPOCAMPUS.md)
- **正在比较不同 Agent Memory 方案：** [`docs/COMPARISON.zh-CN.md`](docs/COMPARISON.zh-CN.md)
- **愿意拿真实项目跑 3～7 天：** [`docs/ALPHA-TESTING.zh-CN.md`](docs/ALPHA-TESTING.zh-CN.md) + [Public Alpha Issue #1](https://github.com/yuzi001a/hippocampus-memory/issues/1)
- **只想确认当前到底支持什么：** [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)

## 这是什么

Hippocampus 把聊天式 Agent 的对话来源和显式记忆保存到可恢复的持久化存储中，再按查询寻找相关内容。当前公开版本由两个包组成：

| 包 | 作用 |
|---|---|
| `v3-core` | 核心记忆引擎，负责 PostgreSQL/pgvector 存储、对话来源写入、显式记忆读写、关键词召回，以及可选的向量召回和 rerank。 |
| `v3-hermes-plugin` | Hermes Agent 的适配器，把核心引擎接入 Hermes host，并注册公开工具面。当前公开 Alpha 的适配器契约包含 13 个工具。 |

核心能力位于 `v3-core`；Hermes 是当前公开 Alpha 提供的一种宿主集成，不意味着 Hippocampus 永远只支持 Hermes，也不意味着所有宿主集成已经完成。

## 为什么记忆不只是“把历史存下来”

最开始做这个项目时，我以为长期记忆主要是存储和检索问题：把历史留下来，用关键词或向量搜索，在需要时找回来。

真实使用很快暴露了更麻烦的问题。记忆一旦重新进入上下文，就会参与下一次生成。错误的记忆、已经过时的判断、重复总结出来的“事实”，都可能让一个 Agent 比没有记忆时表现得更差。长期记忆因此不只是一个存储系统，它还必须面对来源、更新、淘汰、重建和召回时机这些问题。

这也是 Hippocampus 当前设计里一个很重要的区分：原始经历和后来对经历形成的理解不是同一层东西。`conversation_stream` 等来源数据应该尽可能忠实地保存真正发生过的事情，而 Topic、Observer、Embedding、E1 等派生结构可以随着模型和算法变化被重新生成、修正甚至推翻。

换句话说：**过去应该保存，但过去的解释必须允许改变。**

## “生成即是存在”

Hippocampus 背后还有一个比存储更长期的问题。

早期我曾经试图通过 Soul / System Prompt 定义一个 Agent 的人格：它是谁、怎样表达、我们是什么关系。后来在长期使用不同 LLM 的过程中，我逐渐形成了一个新的理解：**生成即是存在。**

这一刻的 Agent 并不只存在于某一份 Soul、某一个 LLM 或某一个记忆数据库中。模型、System Prompt、长期记忆、共同历史、当前上下文、工具结果和此刻的输入一起参与生成，形成这一刻具体的“它”。实际更换不同 LLM 时，能力和气质会明显变化，但只要相当一部分共同历史和生成条件仍然保留，又往往能观察到某种连续性。

因此，Hippocampus 并不把记忆理解成给一个始终存在的 Agent 接上一块硬盘。它更关心的是：已经结束的过去，怎样继续影响下一次生成。

这不是对机器意识的声明。Hippocampus 不声称长期记忆能够创造或证明意识。它只是把“连续性”中的一部分问题变成可以真正实现、运行和验证的工程问题。

## 当前已经验证的能力

下面的结果来自 v0.1-alpha 的公开、隔离验收路径。它们证明了对应路径在新环境中的行为，不等于对所有生产环境、所有 provider 或长时间运行情况作出保证。

- 全新环境安装与 `pip` 安装检查；
- 显式数据库 bootstrap；
- 对话来源持久化写入（`conversation_stream`）；
- canonical（权威）显式记忆写入与搜索；
- 关键词召回；
- 新进程启动后的数据恢复；
- PostgreSQL `pg_dump` / `pg_restore` 恢复；
- 恢复后的继续写入和召回；
- Hermes adapter contract 与公开工具面检查，当前公开工具面为 13 个工具。

代码和配置支持配置外部 embedding / rerank provider，用于 provider-backed vector recall 和重排序。但本次 v0.1-alpha 的 frozen public acceptance profile 没有注入外部 provider credential，因此公开发行验收没有执行 provider-backed vector/rerank E2E。不要把这部分写成“本版本已经完整验证向量检索和 rerank”。

逐项状态、证据边界和未测试项目见 [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)。

## 仍处于实验阶段的能力

以下能力不属于本版本的稳定承诺，使用时应按实验功能看待：

- Observer/E1 自动记忆闭环；
- Recall Engine V2；
- 多 Agent / 多 writer 运行方式；
- 历史 active-memory 迁移；
- 更广泛的 provider 集成；
- 更完善的配置体验；
- 依赖真实 Hermes host、外部 LLM 和 embedding provider 的更完整端到端组合。

当前 Alpha 明确不支持把历史 SQLite mirror 或旧版主动记忆自动迁移到新的 canonical 存储。更多限制见 [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md)。

## 它为什么不只是一个向量数据库

向量检索只是可选的一条召回路径。Hippocampus 还关心：

- 原始对话来源能否可靠保存；
- 显式记忆是否有一个 canonical（权威）的持久化位置；
- 没有 embedding provider 时，关键词路径是否仍能工作；
- 配置了 provider 后，是否可以使用向量召回和可选 rerank；
- Agent 或进程重启后，数据能否继续读取；
- PostgreSQL 能否 dump、restore，并在恢复后继续写入和召回；
- embedding、索引和摘要等派生状态损坏或过期时，是否有机会从来源重新构建。

这也是 Hippocampus 的基本判断：能重建的派生状态不应成为唯一真相。

## 核心设计原则

### 原始数据是资产，派生状态应当可以重建

原始对话来源和显式记忆是需要认真保护的持久化数据。关键词索引、embedding、排序结果和自动摘要属于派生状态，应尽可能能够重新生成，不能反过来替代原始来源。

### 显式记忆有单独的权威存储

当前支持的显式记忆写入路径以 PostgreSQL 中的 `public.explicit_memories` 为 canonical 存储。写入成功和后续的 embedding、摘要等派生工作分开处理，派生步骤失败不应悄悄变成“核心记忆没有保存”的假象。

### 未配置的 provider 默认不接收数据

embedding、LLM 和 rerank 都由用户在本地配置。没有配置 provider 时，系统不会把数据发送给该 provider；配置了 endpoint 后，发送范围取决于用户自己的配置，使用前应核对 endpoint 和数据流。

### Alpha 只声明有证据的范围

本文和公开文档不会把“代码里存在”直接写成“已经稳定可用”。标为 `UNKNOWN`、`NOT TESTED` 或 `EXPERIMENTAL` 的路径，不应当被当作本版本的生产保证。

## Quick Start

当前公开 Alpha 的推荐试用环境是 Windows + 全新 Python `venv` + 本地源码安装 + disposable PostgreSQL/pgvector。下面的命令沿用英文 README 和 [`docs/INSTALL.md`](docs/INSTALL.md) 的安装契约，没有另造一套 Hippocampus CLI 或简化掉数据库 bootstrap。

### 1. 克隆仓库并创建虚拟环境

```powershell
git clone https://github.com/yuzi001a/hippocampus-memory
cd hippocampus-memory

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip wheel
```

每次试用建议使用全新的虚拟环境，不要混用不同版本的 `v3-core`、不同 profile 或旧依赖。

### 2. 启动 disposable pgvector

不要把试用环境指向生产 PostgreSQL。选择一个本地、非生产端口；下面的 `55432` 只是 disposable 环境示例端口。

```powershell
$pgPort = 55432
$pgPassword = "<local-only-password>"   # ← replace, never reuse a real one

docker run --name v3-pgvector-alpha --rm -d `
  -e POSTGRES_PASSWORD=$pgPassword `
  -e POSTGRES_DB=v3embeddings_alpha `
  -p "${pgPort}:5432" `
  pgvector/pgvector:pg17
```

可以按安装文档的命令确认容器已启动并安装了 `vector` 扩展：

```powershell
docker exec v3-pgvector-alpha psql -U postgres -d v3embeddings_alpha `
  -c "CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion FROM pg_extension WHERE extname='vector';"
```

### 3. 从本地源码安装两个包

```powershell
pip install .\src\v3-core
pip install .\src\v3-hermes-plugin
```

这会安装核心包、Hermes 适配器及其依赖，并提供 `v3-core info` 命令。Windows 上如果 `pyahocorasick` 编译依赖导致安装失败，先看 [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md)，不要把失败的半安装环境当作有效验收结果。

### 4. 准备 profile 配置

公开 Alpha 不会自动创建 profile 配置。复制示例文件到你自己选择的 profile 目录：

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.v3-core\profiles\default"
Copy-Item .\examples\config.example.yaml "$env:USERPROFILE\.v3-core\profiles\default\config.yaml"
Copy-Item .\examples\.env.example "$env:USERPROFILE\.v3-core\profiles\default\.env"
notepad "$env:USERPROFILE\.v3-core\profiles\default\config.yaml"
```

至少检查以下内容：

- 顶层 `basePath` 填写你实际使用的绝对路径；Windows 不会在这些路径配置中展开 `~`；
- `storage.pg.host`、`port`、`database`、`user` 与 disposable 容器一致；
- `storage.pg.password` 保持为空，把密码放入环境变量；
- 没有 provider 时可以继续保留 embedding、LLM、rerank 配置块为注释状态；
- 不要把包含真实 credential 的 `config.yaml` 或 `.env` 提交到 Git。

配置键和环境变量的完整说明见 [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)。

### 5. 显式执行数据库 bootstrap

数据库 schema 不是第一次写入时自动应用的。先设置密码，再运行 bootstrap：

```powershell
$env:V3CORE_PG_PASSWORD = $pgPassword
$env:PGPASSWORD = $pgPassword

python .\src\v3-core\scripts\bootstrap_alpha_db.py `
  --host 127.0.0.1 `
  --port $pgPort `
  --database v3embeddings_alpha `
  --user postgres
```

密码通过环境变量传递，不要放在命令行参数中。该脚本针对 disposable 数据库设计，并且在空数据库上重复运行应保持幂等。

### 6. 做最小 smoke check

```powershell
v3-core info
```

这个命令只输出最小引擎状态摘要，不等于 provider health 报告，也不是稳定的固定输出契约。要验证持久化写入、备份和恢复，请继续看 [`docs/BACKUP-RESTORE.md`](docs/BACKUP-RESTORE.md) 以及 supported-surface 文档。

### 清理 disposable 环境

完成试用后，可以按需要删除 disposable 容器和虚拟环境：

```powershell
docker rm -f v3-pgvector-alpha
deactivate
Remove-Item -Recurse -Force .\.venv
```

## Hermes 集成

当前 Public Alpha 包含 Hermes adapter。核心记忆能力位于 `v3-core`，适配器位于 `v3-hermes-plugin`；要测试 plugin-mediated hook contract，需要一个正常运行的 Hermes Agent host。

在 Hermes 的 `config.yaml` 中选择公开的 provider 名称：

```yaml
memory:
  provider: deep_memory_v3
```

同时确保 Hermes 加载插件前已经设置 `V3CORE_PG_PASSWORD`。然后使用刚才创建的 Python 环境启动 Hermes。没有 Hermes host 时，仍可以直接使用 `v3-core` 和其 Python API，但不能把核心包直连测试等同于完整的 Hermes plugin contract 验收。

更详细的安装步骤和约束见 [`docs/INSTALL.md`](docs/INSTALL.md)。

## 外部模型与 provider 配置

Hippocampus 可以在本地 PostgreSQL/pgvector 上运行持久化写入、读取、关键词召回和 soft archive。以下 provider 都是可选配置，端点和模型由使用者提供：

| Provider | 用途 | 未配置时 |
|---|---|---|
| Embedding | 为文本生成向量，支持 active-memory 的向量召回和部分派生流程。 | 核心写入和关键词路径仍可用；向量路径跳过。 |
| LLM | Observer、E1、会话摘要和 topic-card 等自动形成流程。 | 写入仍可落库；对应自动生成流程跳过。 |
| Rerank | 对召回候选重新排序。 | 不执行 rerank，使用已有召回顺序。 |

支持 OpenAI-compatible 的接口格式，但“支持配置”不等于本次发行已经对每一种 provider 做过端到端验收。特别是本版本的 frozen public acceptance profile 没有注入外部 provider credential，因此 provider-backed vector/rerank E2E 不在本次发行验收范围内。

数据流和隐私边界见 [`docs/PRIVACY-DATA-FLOW.md`](docs/PRIVACY-DATA-FLOW.md)。配置 provider 之前，请先确认 endpoint、模型和发送的数据范围。

## 数据、备份与恢复

Hippocampus 的数据边界可以这样理解：

- `conversation_stream` 等原始对话来源是需要保留的来源数据；
- `public.explicit_memories` 是显式记忆的 canonical（权威）存储；
- embedding、索引、自动摘要和排序结果是派生状态，应该可以从来源重新构建；
- PostgreSQL 是当前 Alpha 运行时的中心持久化组件，备份应围绕实际 PostgreSQL 数据库进行；
- 恢复成功不能只看命令退出码，还应检查表和数据，并确认恢复后可以继续写入与召回。

具体的 `pg_dump` / `pg_restore` 操作见 [`docs/BACKUP-RESTORE.md`](docs/BACKUP-RESTORE.md)。

## 当前限制

请把下面这些限制当作 v0.1-alpha 的一部分，而不是安装后再猜：

- v0.1-alpha 是 Technical Preview，不是稳定生产版本；
- 当前运行时以 PostgreSQL / pgvector 为中心；
- 配置体验仍偏开发者，profile 和环境变量需要手动准备；
- 自动记忆形成仍处于实验阶段，不能假设普通对话都会自动成为可靠记忆；
- 多 Agent / 多 writer 还未成熟；
- 历史 active-memory 迁移不属于当前公开 Alpha 支持面；
- provider-backed vector/rerank 的公开发行验收未使用外部 credential，因此相关 E2E 证据不属于本次 release acceptance；
- Alpha 期间 API、工具面和 internal naming 可能继续演进；
- 长时间 soak、所有 provider 组合以及完整生产部署，不是本版本的默认保证。

逐项限制和未测试路径见 [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md) 与 [`docs/RELEASE-CHECKLIST.md`](docs/RELEASE-CHECKLIST.md)。

## FAQ / 排障入口

### 为什么第一次写入时报表不存在？

数据库 bootstrap 是显式步骤，不是自动首次运行步骤。确认你已经对 disposable PostgreSQL 执行了 `src/v3-core/scripts/bootstrap_alpha_db.py`，并且连接参数与容器一致。详见 [`docs/INSTALL.md`](docs/INSTALL.md) 第 6 节。

### 为什么没有向量召回或自动摘要？

先检查对应 provider 是否在 `config.yaml` 中配置。未配置 embedding 时走不了向量 lane；未配置 LLM 时 Observer/E1、摘要和 topic-card 等自动形成路径会跳过，但持久化写入和关键词路径不应因此被静默丢弃。

### 为什么 `~` 写在 `basePath` 里不生效？

Windows 下公开 Alpha 要求 `basePath` 使用绝对路径，不能依赖引擎展开 `~`。请按 [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) 的规则填写。

### `v3-core info` 是不是完整健康检查？

不是。它只输出最小状态摘要。provider health 是独立路径，不能用某一行固定输出推断所有 provider 都已经可用。

### 发现了 bug，应该在哪里反馈？

一般问题和功能建议请使用 [GitHub Issues](https://github.com/yuzi001a/hippocampus-memory/issues)。记忆质量相关问题建议使用仓库里的 **Alpha memory feedback** 模板。不要在公开 issue 中粘贴 credential、DSN、私有数据或完整配置；安全问题先阅读 [`SECURITY.md`](SECURITY.md)。

## Documentation map

详细的架构、安装、恢复和安全文档目前以英文为主。中文 README 只提供核心入口，不复制整套 `docs/`，以免 Alpha 阶段形成两套长期维护的完整文档。

| 文档 | 用途 |
|---|---|
| [`docs/WHY_HIPPOCAMPUS.md`](docs/WHY_HIPPOCAMPUS.md) | 项目的思想演变：从“让她记住我”、Soul，到“生成即是存在”、记忆治理和连续性问题。 |
| [`docs/COMPARISON.zh-CN.md`](docs/COMPARISON.zh-CN.md) | 中文方案对比：Hermes 内置记忆、历史搜索、Mem0、Hindsight 与 Hippocampus 分别适合什么。 |
| [`docs/ALPHA-TESTING.zh-CN.md`](docs/ALPHA-TESTING.zh-CN.md) | 中文 Public Alpha 实测指南：如何跑 3～7 天真实项目并提交有价值的失败案例。 |
| [`docs/INSTALL.md`](docs/INSTALL.md) | Windows 安装、disposable pgvector、本地源码安装和 bootstrap。 |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | `config.yaml`、环境变量、provider 默认行为和 fail-closed 规则。 |
| [`docs/BACKUP-RESTORE.md`](docs/BACKUP-RESTORE.md) | PostgreSQL dump / restore 以及恢复后检查。 |
| [`docs/PRIVACY-DATA-FLOW.md`](docs/PRIVACY-DATA-FLOW.md) | 哪些数据保存在本地，哪些数据会发给已配置的 provider。 |
| [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md) | 当前 Alpha 的逐项支持面、证据和 `UNKNOWN / NOT TESTED` 边界。 |
| [`docs/ARCHITECTURE-OVERVIEW.md`](docs/ARCHITECTURE-OVERVIEW.md) | 模块地图、写入/读取边界和 canonical active-memory 位置。 |
| [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md) | 未完成、延期和本版本明确不关闭的问题。 |
| [`docs/RELEASE-CHECKLIST.md`](docs/RELEASE-CHECKLIST.md) | Alpha 发布门槛及后续限制。 |
| [`src/v3-core/README.md`](src/v3-core/README.md) | `v3-core` 包说明。 |
| [`src/v3-hermes-plugin/README.md`](src/v3-hermes-plugin/README.md) | Hermes adapter 包说明。 |

## 反馈

- Public Alpha 实测：[`docs/ALPHA-TESTING.zh-CN.md`](docs/ALPHA-TESTING.zh-CN.md) + [Issue #1](https://github.com/yuzi001a/hippocampus-memory/issues/1)
- Bug 和功能建议：[GitHub Issues](https://github.com/yuzi001a/hippocampus-memory/issues)
- 安全问题：先阅读 [`SECURITY.md`](SECURITY.md)，不要公开提交 credential、DSN 或私有数据

## License

两个包都使用 `AGPL-3.0-or-later`，具体许可证文件见 [`src/v3-core/LICENSE`](src/v3-core/LICENSE) 和 [`src/v3-hermes-plugin/LICENSE`](src/v3-hermes-plugin/LICENSE)。本仓库不会因为增加中文 README 而改变任一包的许可证。