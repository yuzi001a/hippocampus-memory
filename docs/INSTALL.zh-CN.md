# 安装 Hippocampus v0.1-alpha

> 本页同步 [`INSTALL.md`](INSTALL.md) 的 Gate 2 改动。只追加不重写。

## 5. 本地源码 `pip install`（不可编辑）

```powershell
pip install .\src\v3-core
pip install .\src\v3-hermes-plugin
```

安装完成后会得到两个控制台脚本：

- `v3-core` —— 保持原样（`v3-core info` 等调用方式不变）。
- `hippocampus` —— 分发面向的控制台（Gate 2 提供）。子命令：
  - `hippocampus doctor [--static]`：只读安装体检。`--static` 跳过
    配置解析、可在打包/CI 环境安全运行；输出 JSON、密钥自动脱敏。
  - `hippocampus bootstrap --target <DSN>`：对显式目标执行打包的
    `alpha_bootstrap.sql`。生产边界 DSN
    `127.0.0.1:5433 / v3embeddings` 永久拒绝，不提供绕过开关。

`v3-hermes-plugin` 同时声明 Hermes memory provider 入口点：

```toml
[project.entry-points."hermes_agent.memory_providers"]
deep_memory_v3 = "v3hermes:register"
```

入口点名 `deep_memory_v3` 与 `plugin.yaml` 中的 `name: deep_memory_v3`
一致；`v3-core` 依赖范围固定为 `>=4.0.0,<5.0.0`。

## 7.5 验证安装（只读）

```powershell
hippocampus doctor --static
```

预期：stdout 输出一段 JSON，`status` 为 `ok`，`checks.packaged_sql`
列出 `alpha_bootstrap.sql` 与 `explicit_memories.sql`（带 sha256）。
