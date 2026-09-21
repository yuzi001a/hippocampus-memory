# Embedding backfill operator

离线修复工具，用来把历史遗留的 `NULL` embedding 补齐。它**不属于 live runtime**：
不在 gateway / serve / ingest 的调用链上，只在 operator 手动执行时运行。

入口只有一个：

```bash
python -m v3core.tools.embedding_backfill --table <table> [--apply] [--limit N]
```

不带 `--apply` 就是 dry-run（只统计与打印，不写库）。

---

## 为什么不复用 live 路径

修复 pass 必须复现「活写入者当时真正喂给 embedding 模型的文本」。如果它自己另造一份
文本，写回去的向量就落在**第二个语义空间**里：召回行为与活写入的行不一致，而且事后
不可察——向量列非空、指纹列统一，任何健康检查都看不出来。

所以每张表的 canonical 输入都在 `_row_text()` 里逐字复刻活写入者的表达式，
并且有回归测试钉住它。**新增表或改动活写入者的 embedding 文本时，必须同步改这里。**

---

## 曾经静默失效的五个缺陷

这套 CLI 从写出第一天就跑不起来。此前的 E2E 全部只调用内部函数
`backfill_table()`，**从来没有启动过真实 CLI**，所以缺陷一路活到了生产。

| 编号 | 缺陷 | 后果 |
| --- | --- | --- |
| D1 | `from v3core.pg_store import PGStore` —— 该符号不存在（真实是 `PgEmbedStore`）；且用 `pg.conn` / `pg._conn` 取连接，两者都不是合法契约 | 任何调用都 `ImportError` 退出，rc=1 |
| D2 | `cfg.get("embedding") or cfg.get("embed")` —— `resolve_config()` 返回 typed `V3Config`，`"embedding"` 是 `None`、`"embed"` 是 `EmbedConfig` 对象（没有 `.get()`）；配置真实位置是 `storage.embed` | `AttributeError`；即使绕过也永远解析不到 model |
| D3 | D2 的连带后果：长 QA 的 tokenizer 拿不到 model 名 | `TokenizerUnavailableError`，`qa_pairs` 每次修复都被记成假失败 |
| D4 | `topics` 只截 `body[:800]`，漏掉活路径最后的 `[:1000]` | 生产 345 张卡里 173 张 >1000 字，会 embed 一个活路径从未产生过的字符串 |
| D5 | 修复成功后固定 `resolve(phase="backfill")` | live 失败记录的 phase 是 `live_ingest` / `j_import`，向量修好了 marker 还挂着 → **假 backlog** |

---

## 现在的不变量

### 配置与连接：fail-closed

- `embed_cfg` 只能来自 `build_embed_cfg(cfg)` / `safe_embed_cfg(cfg)`。禁止手拼 dict。
- 配置**存在但非法**（缺 model / endpoint）与**根本未配置**是两回事，走不同分支、
  给不同消息——否则运维看到的原因是错的。
- 连接只能经 `PgEmbedStore.lease()` 获取。遗留模式下 `_connect()` 失败会**返回 None
  而不抛异常**，所以 `conn is None` 必须显式 fail-closed。

退出码：

| rc | 含义 |
| --- | --- |
| 0 | 成功（dry-run 或 apply） |
| 1 | 完成但有永久失败 |
| 2 | 配置 fail-closed（未配置 / 非法 / 缺 model / 缺 endpoint） |
| 3 | 拿不到 PostgreSQL 连接 |

### 修复成功后的 marker 关闭

一个实体的 canonical embedding 只有一个派生状态。恢复即该实体名下**全部**
unresolved phase 同时失效：

```sql
UPDATE public.embedding_failures
   SET resolved_at = now(), resolution = 'repaired', updated_at = now()
 WHERE entity_table = ? AND entity_id = ? AND resolved_at IS NULL
```

没有 `phase` 谓词——这是与 `resolve_embedding_failure()`（按 phase 关闭）的唯一区别。
作用域严格限定在一个实体：不误伤其它实体、其它表、已 resolved 的历史记录。只
UPDATE 不 DELETE，历史保留可审计。

---

## 测试面

CLI 是**一等测试面**，不允许只测内部函数：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_backfill_cli_entrypoint.py` | 真实子进程跑 `main()`：`--help`、非法表、配置 fail-closed 三种形态、PG 不可达 rc=3、五张表都能走到连接阶段 |
| `tests/test_backfill_canonical_input.py` | `_row_text()` 的 canonical 输入，含 topics 的 <1000 / =1000 / >1000 / body>800 但整体<1000 四个边界 |
| `tests/test_embedding_failure_resolution.py` | 按实体关闭的 SQL 语义与五个作用域场景（三个 phase 一起关、其它实体/表不误伤、已 resolved 不动） |

CLI 测试**不得** monkeypatch `resolve_config` / `safe_embed_cfg` / `PgEmbedStore` ——
那三处正是缺陷所在，绕过等于把缺陷重新藏起来。

需要真实数据库的路径（dry-run 全表、`--apply --limit N`、长 QA 分块、
marker 关闭）放在 `eval/` 下的一次性库脚本里，因为 `tests/conftest.py` 在测试域
硬封禁 `psycopg2.connect`。

---

## 生产上怎么跑

在**独立 operator venv** 里跑，不要动 live Hermes venv：

```bash
python -m v3core.tools.embedding_backfill --table conversation_stream          # 先 dry-run
python -m v3core.tools.embedding_backfill --table conversation_stream --apply --limit 10
```

- 先 `--limit` 小批量验证，再考虑全量。
- `topics` 在 canonical 表示被证明与活写入者 byte-identical 之前不要 `--apply`。

### 不要指望用 `PGOPTIONS` 强制只读

试过用 `PGOPTIONS='-c default_transaction_read_only=on'` 把每条连接压进只读事务，
**结果是 5 张表全部 rc=3**：`PgEmbedStore` 在建立连接时会执行 `CREATE EXTENSION`
（pgvector 类型注册），而 PostgreSQL 拒绝在只读事务里执行 DDL —— 被挡掉的是**连接
本身**，不是写入。

推论有两条，都是真实约束：

1. 这个 CLI **无法**在 `default_transaction_read_only` 硬化过的库或角色上运行。
   部署时若做了这层硬化，backfill 会整个不可用。
2. dry-run 的安全性不来自"强制只读"，而来自 `--apply` 是显式 opt-in，
   加上 dry-run 在写循环之前就返回。验证零写要靠 `pg_stat_user_tables` 的
   ins/upd/del 增量 + 行数/NULL 数对账，不要靠 `PGOPTIONS`。

