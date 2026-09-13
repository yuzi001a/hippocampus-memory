# 安装 Hippocampus v0.1-alpha

> 本页与英文 [`INSTALL.md`](INSTALL.md) 描述同一条分发验收路径：构建 wheel/sdist → 新环境安装实际 wheel → disposable PostgreSQL/pgvector → doctor → bootstrap → Hermes host 激活。
>
> 这是 Technical Preview，不是生产发布。不要使用生产端口 `5433`、本机数据库 `v3embeddings`、生产 profile 或真实 provider credential。

## 1. 前置条件

- Windows 10/11、Python 3.10+、Git、Docker Desktop、uv。
- 当前 Hermes 是单独的宿主前置条件，不是本项目的 PyPI 依赖。当前 upstream Hermes 不以 wheel/sdist 方式分发；本 sprint 的可复现实验路径是 clone 当前 upstream 后执行 `uv sync`。正式使用请遵循 [Hermes 官方安装文档](https://hermes-agent.nousresearch.com/docs/getting-started/installation)。
- 一个 disposable `pgvector/pgvector` PostgreSQL 容器。

## 2. 构建两个分发包

在仓库根目录执行：

```powershell
uv build --wheel --sdist --out-dir .\dist\v3-core .\src\v3-core
uv build --wheel --sdist --out-dir .\dist\v3-hermes-plugin .\src\v3-hermes-plugin
```

应得到：

```text
dist\v3-core\v3_core-4.0.0-py3-none-any.whl
dist\v3-core\v3_core-4.0.0.tar.gz
dist\v3-hermes-plugin\v3_hermes_plugin-4.0.0-py3-none-any.whl
dist\v3-hermes-plugin\v3_hermes_plugin-4.0.0.tar.gz
```

本版本没有创建或占用 PyPI 项目；不要把 `pip install v3-core` 写成已验证的 registry 安装路径。

## 3. 新建 venv，安装实际 wheel

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
uv pip install .\dist\v3-core\v3_core-4.0.0-py3-none-any.whl
uv pip install .\dist\v3-hermes-plugin\v3_hermes_plugin-4.0.0-py3-none-any.whl
uv pip check
```

安装是非 editable 的。`v3core.__file__` 和 `v3hermes.__file__` 应指向 `.venv\Lib\site-packages`，不能指向仓库源码。入口点是：

```toml
[project.entry-points."hermes_agent.memory_providers"]
deep_memory_v3 = "v3hermes:register"
```

## 4. disposable PostgreSQL/pgvector

选择非生产端口，例如 `55432`；`5433` 永久拒绝：

```powershell
$pgPort = 55432
$pgPassword = "<local-only-password>"

docker run --name v3-pgvector-alpha --rm -d `
  -e POSTGRES_PASSWORD=$pgPassword `
  -e POSTGRES_DB=v3embeddings_alpha `
  -p "${pgPort}:5432" `
  pgvector/pgvector:pg17
```

## 5. doctor 与显式 bootstrap

先做只读体检：

```powershell
hippocampus doctor --static
```

预期 stdout 是 JSON，`status` 为 `ok`，并列出打包的两个 SQL 资源、`deep_memory_v3` 入口点和 provider 配置状态。`doctor` 默认只读；非 static 模式的数据库探针只执行 `SELECT 1`、`pg_extension` 和 `information_schema` 查询。

准备密码环境变量，不把密码放到命令行：

```powershell
$env:V3CORE_PG_PASSWORD = $pgPassword
$env:PGPASSWORD = $pgPassword
```

对 disposable 目标执行打包后的 schema bootstrap：

```powershell
hippocampus bootstrap --target "postgres://postgres@127.0.0.1:${pgPort}/v3embeddings_alpha"
```

`hippocampus bootstrap` 会展开并执行打包的 `alpha_bootstrap.sql` 与 canonical `explicit_memories.sql`，可重复执行。端口 `5433` 和 loopback 上的 `v3embeddings` 永久拒绝，没有 override 开关。

## 6. profile 配置与 Hermes

复制示例配置到你自己的 profile，填写绝对 `basePath`、disposable PG 连接信息，并保持 provider 块未配置即可运行关键词路径：

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.v3-core\profiles\default"
Copy-Item .\examples\config.example.yaml "$env:USERPROFILE\.v3-core\profiles\default\config.yaml"
Copy-Item .\examples\.env.example "$env:USERPROFILE\.v3-core\profiles\default\.env"
notepad "$env:USERPROFILE\.v3-core\profiles\default\config.yaml"
```

Hermes host 配置选择：

```yaml
memory:
  provider: deep_memory_v3
```

把 `v3-hermes-plugin` wheel 安装到同一个 current Hermes Python 环境；Hermes 通过 `hermes_agent.memory_providers` 发现 `deep_memory_v3`。安装后的 provider 会尊重 `HERMES_HOME` 和 profile 选择；不要指向生产 profile。

## 7. 清理

```powershell
docker rm -f v3-pgvector-alpha
deactivate
Remove-Item -Recurse -Force .\.venv
```

源码目录中的 `src/v3-core/scripts/bootstrap_alpha_db.py` 仅保留给源码开发，不是本分发路径的主命令。