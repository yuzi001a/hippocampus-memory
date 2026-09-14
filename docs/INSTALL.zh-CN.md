# 安装 — v3 Memory Plugin（公开 Alpha 候选，简体中文）

> **读者**：在 Windows 上首次试用、希望对照一次性、隔离的
> PostgreSQL/pgvector 评估公开 Alpha 支持面的用户。
>
> **范围**：从本仓库源码为 `v3-core` 与 `v3-hermes-plugin` 构建
> 不可编辑的 wheel + sdist 产物 → 把构建出的 wheel 装入全新 venv →
> 把 v3 指向一次性 pgvector → 通过打包命令 `hippocampus` 显式完成
> 数据库 bootstrap → 确认安装成功。
>
> **不在范围**：连接任何已存在的生产 PostgreSQL、导入历史数据、
> 替换已有部署、运行任何历史迁移脚本。本文档假设一个全新环境。
>
> **验证状态**：本安装流程是文档化的 bring-up 路径。空 tmpfs pg17
> 上的 clean-history export E2E 验收结果见
> [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
> 第 2.1 节（PASS 行覆盖 clean-install、`uv pip check`、bootstrap
> 幂等）。公开 Alpha 没有自行关闭的前置发布阻塞见
> [`docs/RELEASE-CHECKLIST.md`](RELEASE-CHECKLIST.md) 第 D 节。

---

## 1. 你需要什么

| 项目 | 用途 | 备注 |
|---|---|---|
| **Windows 10/11** | 本次 Alpha 的主支持目标。 | 包本身是跨平台 Python。 |
| **Python 3.10 或 3.11** | `pyproject.toml` 要求 `>=3.10`。 | 3.12 在引擎上同样可用。 |
| **Docker Desktop** | 跑一次性 `pgvector/pgvector:pg17` 容器。 | 原生 Windows PG 安装在 SQL 层等价；文档化流程使用 Docker 容器。 |
| **Git** | 克隆本仓库。 | — |
| **可选：网络可达的模型 provider** | 召回用的 embedding；observer/E1 用的 LLM（可选但推荐）。 | 端点全部由用户在 `config.yaml` 配置；见 [`docs/CONFIGURATION.md`](CONFIGURATION.md)。 |
| **~5 GB 空闲磁盘** | 仓库 + venv + 容器镜像 + 一次性 PG 数据。 | — |
| **`uv`** | 创建/管理 venv，并执行 wheel 构建与安装。 | 一次性安装：`pip install uv`、`winget install astral-sh.uv`，或 `irm https://astral.sh/uv/install.ps1 | iex`。 |

> ⚠️ **硬性规则**：**不要**把这次安装指向任何生产 PostgreSQL。
> 支持面只覆盖全新一次性环境。生产凭证、生产端点、生产端口不在
> 本文档范围。

---

## 2. 克隆

```powershell
git clone https://github.com/yuzi001a/hippocampus-memory.git
cd hippocampus-memory
```

本公开仓库是 `v3-core` 与 `v3-hermes-plugin` 的规范开发与发布源。
本文档**不**假定任何具体的 tag / branch / commit SHA；
`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md` 第 2 节的每一行支持面证据
都对应到验收当时实际检出的 HEAD，而不是某个固定的内部标识。

验证工作树是公开 Alpha 候选：

```powershell
git log --oneline -1
# 记录验收 § 2 PASS 行时实际使用的 HEAD。
```

---

## 3. 全新 venv（Windows）

每次评估都用**全新**虚拟环境。混用不同 v3-core 版本或不同
profile 名是「昨天还能跑今天就坏」最常见的原因。

```powershell
uv venv --python 3.11 .venv
.\.venv\Scripts\Activate.ps1
```

确认你确实在 venv 里：

```powershell
where python
# 应解析为 ...\.venv\Scripts\python.exe
```

---

## 4. 构建不可编辑 wheel + sdist 产物

文档化的分发路径是先**构建产物**，再把构建出的 wheel 装入全新
venv——而不是 `pip install` 直接装源树。从仓库根目录执行：

```powershell
uv build --wheel --sdist --out-dir .\dist\v3-core .\src\v3-core
uv build --wheel --sdist --out-dir .\dist\v3-hermes-plugin .\src\v3-hermes-plugin
```

会产生（路径相对于仓库根）：

- `dist\v3-core\v3_core-4.0.0-py3-none-any.whl`
- `dist\v3-core\v3_core-4.0.0.tar.gz`
- `dist\v3-hermes-plugin\v3_hermes_plugin-4.0.0-py3-none-any.whl`
- `dist\v3-hermes-plugin\v3_hermes_plugin-4.0.0.tar.gz`

第 6 步会装这些 wheel。**本次 Alpha 不会**把这些推到 PyPI——没有
PyPI 项目可声明，本文档也不会引导你从任何 registry 安装。

---

## 5. 一次性 pgvector 容器

使用 Docker。镜像为官方 `pgvector/pgvector:pg17`（与 clean-history
export E2E 验收一致；见
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
第 1 节）。端口**不要**用任何生产端口——`55432` 是文档化的一次性
占位端口：

```powershell
$pgPort = 55432
$pgPassword = "<local-only-password>"   # ← 替换，不要复用真实口令

docker run --name v3-pgvector-alpha --rm -d `
  -e POSTGRES_PASSWORD=$pgPassword `
  -e POSTGRES_DB=v3embeddings_alpha `
  -p "${pgPort}:5432" `
  pgvector/pgvector:pg17
```

确认容器起来且 `vector` 已装：

```powershell
docker exec v3-pgvector-alpha psql -U postgres -d v3embeddings_alpha `
  -c "CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion FROM pg_extension WHERE extname='vector';"
```

应看到形如 `0.7.x` 的版本字符串。

> 如果没有 Docker Desktop，原生 Windows PostgreSQL 17 + `vector`
> 在 SQL 层等价；文档化流程以 Docker 容器为基准，原生安装记为兼容
> 但不是参考路径。

---

## 6. 装实际构建出来的 wheel（不可编辑，全新 venv）

```powershell
uv pip install .\dist\v3-core\v3_core-4.0.0-py3-none-any.whl
uv pip install .\dist\v3-hermes-plugin\v3_hermes_plugin-4.0.0-py3-none-any.whl
```

然后跑 `uv pip check`（pip 兼容），确认两个 wheel 在每一个传递依赖
上互相一致。**不要**跳过这一步；一次干净的 `uv pip check` 是支持面
证据的一部分。

```powershell
uv pip check
```

这一步会：

- 从 wheel 安装 `v3-core`（声明 `psycopg2-binary`、`pgvector`、
  `pyyaml`、`numpy`、`requests`、`openai`、`jieba`、
  `pyahocorasick>=2.3.0`）。
- 从 wheel 安装 `v3-hermes-plugin`（声明
  `v3-core>=4.0.0,<5.0.0`、`requests`、`pyyaml`）。
- 安装两个 `v3-core` 控制台脚本：
  - `v3-core`——原样保留（例如 `v3-core info`）。
  - `hippocampus`——分发面向控制台（Gate 2）。提供
    `hippocampus doctor`（只读安装检查）与
    `hippocampus bootstrap`（对显式目标应用打包 SQL；无条件拒绝
    生产边界 DSN）。
- 注册 Hermes memory provider 入口点
  `hermes_agent.memory_providers / deep_memory_v3 →
  v3hermes:register`（与 manifest `name: deep_memory_v3` 一致）。

> 💡 `pyahocorasick` 是 C 扩展。在 Windows 上需要与 Python 匹配的
> 工作 C 编译器（例如匹配 Python 版本的 MSVC build tools）。如果
> `uv pip install` 在这一步失败，先看
> [`docs/KNOWN-LIMITATIONS.md`](KNOWN-LIMITATIONS.md) 再重试。
>
> 旧版 `src/v3-core/scripts/bootstrap_alpha_db.py` 仍保留在仓库里，
> 只用于源树开发，**不**是本次 Alpha 的主分发路径——主分发路径是
> `hippocampus bootstrap`（第 8 步）。

---

## 7.5 `hippocampus doctor --static` — 只读安装检查

完成 `uv pip install` 构建出的 wheel（第 6 步）之后、在动任何数据库
之前，先跑一次分发面向的只读健全性检查：

```powershell
hippocampus doctor --static
```

预期：stdout 上有一个 JSON 对象，含 `command: doctor`、`static:
true`、`checks.packaged_sql` 列出 `alpha_bootstrap.sql` 与
`explicit_memories.sql`（都带 sha256）。`--static` 标志跳过配置解析，
所以该命令在打包/CI 上下文里是安全的。不带 `--static` 时，doctor
还会解析当前 profile 的 config（只读）并打印凭证脱敏摘要。

`doctor --static` 是文档化的「bootstrap 前置闸」：`hippocampus
bootstrap --target <DSN>` 之前必须先看到 `status: ok`。

---

## 8. 数据库 bootstrap（显式，**不**自动）——打包命令

> ⚠️ **这一步是显式的。**`v3core.active_memory_store` 写路径**不**
> 会应用 schema。首次写入前**必须**对一次性 PG 跑 `hippocampus
> bootstrap`，否则会因缺表而失败。

支持面验收在空 tmpfs pg17 上产生了 `7` 张表，第二次运行幂等无报错。
见 [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
第 2.1 节，「DB schema applied via `hippocampus bootstrap`」一行。

```powershell
# 通过环境变量传口令，永远不要让它出现在 CLI 参数里。
# plugin manifest 同时为 `requires_env` 读 V3CORE_PG_PASSWORD，
# bootstrap 与运行时使用同一个环境变量即可。
$env:V3CORE_PG_PASSWORD = $pgPassword
$env:PGPASSWORD = $pgPassword

hippocampus bootstrap --target "postgres://postgres@127.0.0.1:${pgPort}/v3embeddings_alpha"
```

这一步会：

- 按字面应用打包进 wheel 的
  `src/v3-core/src/v3core/schema/alpha_bootstrap.sql`，其中内联了
  规范版的 `src/v3-core/src/v3core/schema/explicit_memories.sql`
  产物（仓库根目录 `src/v3-core/schema/*.sql` 是权威源，包内
  副本与根目录副本字节相同）。
- 幂等：每条 DDL 都用 `IF NOT EXISTS` / `ADD COLUMN IF NOT
  EXISTS`。跑两次是 no-op。
- 默认拒绝生产边界 DSN：端口 `5433` 无条件拒绝；环回主机 +
  `v3embeddings` 库的组合也拒绝。**没有**覆盖标志；密钥不出现在
  argv 上。

> 🛑 **永远不要把这条命令指向生产 PG。** 打包命令 `hippocampus
> bootstrap` 无条件拒绝默认生产边界 DSN；如果你传了一个长得像生产
> 的 DSN，风险自担。Alpha 契约假设是一次性 PG。

---

## 9. 引导 v3 引擎配置

`v3-hermes-plugin` 从你选的 profile 目录的绝对路径读引擎配置。
支持的规范键是 `config.yaml` 里的**顶层 `basePath`**（引擎在
`src/v3-core/src/v3core/config.py::_resolve_data_dir` 和
`src/v3-core/src/v3core/config_model.py::V3Config` 中解析它；这是
Alpha 契约推荐的**唯一**路径样式键）。最简单的引导：

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.v3-core\profiles\default"
Copy-Item .\examples\config.example.yaml "$env:USERPROFILE\.v3-core\profiles\default\config.yaml"
Copy-Item .\examples\.env.example "$env:USERPROFILE\.v3-core\profiles\default\.env"
notepad "$env:USERPROFILE\.v3-core\profiles\default\config.yaml"
```

填写：

- 顶层 `basePath` 为 profile 目录的绝对路径（例如
  `C:\Users\<you>\.v3-core\profiles\default`）。**路径必须为绝对
  路径；引擎不展开 `~`，也不会默认到某个用户路径。** 公开 Alpha
  契约明确要求：把顶层 `basePath` 设为该绝对 profile 目录。
- `storage.pg.host` / `port` / `database` / `user` 对齐你的一次性
  容器（`127.0.0.1`、你选的一次性端口、`v3embeddings_alpha`、
  `postgres`）。
- `storage.pg.password`：在 `config.yaml` 里留空，并把
  `V3CORE_PG_PASSWORD` 写到 `.env`（引擎读这个环境变量；plugin
  manifest 在 `requires_env` 里也声明了它）。`examples/config.example.yaml`
  因此以 `password: ""` 形式发布。
- 可选 provider 块（`storage.embed`、顶层 `llm`、`storage.rerank`）：
  在 `config.yaml` 中**省略**以禁用向量召回 / LLM 合成 / rerank。
  发布的示例文件把这些块注释掉了——见
  [`examples/config.example.yaml`](../examples/config.example.yaml)。
  如果你取消注释，请把 `endpoint` / `model` 填成真实值，不要留
  占位 URL。

> 永远不要把真实的 `config.yaml` 或 `.env` 提交到版本控制。
> `examples/` 下的示例文件只有占位值。

---

## 10. 烟雾测试引擎

```powershell
v3-core info
```

预期：简短的状态横幅，打印引擎版本并报告 PG 连接状态。如果 PG
不可达，横幅会如实显示——这在 bring-up 阶段是预期行为，不是构建
失败。

要在没有完整 Hermes host 的情况下跑耐久路径，可以对一次性 PG 跑
[`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md) 中的 lab 配方。

---

## 11. （可选）接入 Hermes

> **Hermes 是外部前置依赖，不是本仓库的 host package 依赖。** 本
> 仓库**不**声明存在 Hermes wheel/sdist。先按官方文档装好 Hermes，
> 再把 `v3-hermes-plugin` 产物装到**同一个** Hermes host 环境。

如果你已经有可用的 Hermes Agent host，并希望跑端到端 hook 契约：

1. 从官方源装好上游 Hermes host，用 `hermes --version` 验证。本次
   Alpha 验收使用的可复现 host 路径是浅克隆当前上游仓库后
   `uv sync`。
2. 在你的 Hermes `config.yaml` 中加：
   ```yaml
   memory:
     provider: deep_memory_v3
   ```
3. 把**同一个** `v3_hermes_plugin-4.0.0-py3-none-any.whl`（第 6 步
   构建）装到 Hermes host 环境。入口点是
   `hermes_agent.memory_providers / deep_memory_v3 →
   v3hermes:register`。
4. 启动 Hermes。（没有自动化的安装向导；plugin 加载前必须满足
   `requires_env: [V3CORE_PG_PASSWORD]`。）
5. 实际跑一段对话：观察 `sync_turn` 写入落到 `conversation_stream`，
   以及你通过 `v3_add` 写入的活动记忆能被后续 `v3_get` / `v3_search`
   读到。

plugin manifest 在 `src/v3-hermes-plugin/plugin.yaml`（`name:
deep_memory_v3`，要求 `V3CORE_PG_PASSWORD`）。安装后的 provider 在
初始化前会读取 `HERMES_HOME` 和 profile 选择；**不要**指向生产
profile。

> ⚠️ **Hermes 是 host 依赖。** 没有可用的 Hermes host，plugin 调停
> 的契约不会被执行。`v3-core` 本身仍然可用。

---

## 12. 清理

```powershell
# 丢掉一次性 PG（破坏性）
docker rm -f v3-pgvector-alpha

# 如果不想保留 venv
deactivate
Remove-Item -Recurse -Force .\.venv
```

---

## 13. 本次安装不会做的事

- **不会**自动应用 `public.explicit_memories` DDL。请显式跑
  `hippocampus bootstrap`（第 8 步）。`v3core.active_memory_store`
  写路径不会应用 DDL。
- **不会**导入历史数据、回放旧对话流，或运行任何历史迁移脚本。
- **不会**触碰生产环境、迁移历史数据或替换已有部署。

---

## 14. 下一步

- [`docs/CONFIGURATION.md`](CONFIGURATION.md) — 全部配置键、
  环境变量、provider 未配置时的默认行为。
- [`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md) — `pg_dump` +
  `pg_restore` 配方。
- [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
  — 公开 Alpha 契约与每条证据指向。
- [`docs/RELEASE-CHECKLIST.md`](RELEASE-CHECKLIST.md) — Alpha 发布闸
  与后续限制。
