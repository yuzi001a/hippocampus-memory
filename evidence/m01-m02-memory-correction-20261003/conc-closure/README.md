# M01 bounded closure A — 真实 PostgreSQL 并发相同纠正：历史复现证据

**PRE-PARENT / SUPERSEDED PATCH EVIDENCE。** 本文 GREEN 与 SHA 对应父级 fail-closed 补丁前的中间版本，不是最终 canonical 验收。最终结果见上级 `README.md` / `parent-concurrency-final.json` / `run7-report.json`。`staged/` 的三份插桩副本（可由 `make_instrumented.py` / `make_prefix.py` 重新生成）已在交付前从工作树删除，避免与 canonical 模块混淆；`scoped-repair.diff` 与插桩/运行脚本仅留本地 authoring scratch，不纳入本任务 commit；本目录提交的报告只作为历史复现证据。本文 §3.2 sequential 使用独立 fixture，不能替代父级对原并发 A→B 的顺序重试。

范围：仅修复真实 PG 上「并发相同纠正」这一个 blocker。未提交 / 未 push / 未 merge。
主机：只在 Y400（DESKTOP-EQP3OBU）操作，唯一一次性 PG 127.0.0.1:55432，一次性库
`m01m02e2e_concclosure_*`，一次性 root `C:/hp-testbed/m01-m02-integration-20261003/conc-closure-*`。
WinNAS 未连生产库 / 未动 outbox / 未重启服务。密钥仅运行时环境变量，未打印。

## 1. 根因（已由真实证据确认，非推断）

真实 `v3core serve` 进程里 `V3Core(profile=...)` **没有 pg_pool**，因此
`ActiveMemoryWriter` 以 `pool=None, pg=<legacy PgEmbedStore>` 构造。
`_acquire_lease` 走 `_PgStoreLeaseAdapter(pg.lease())`，而 **无池的
`PgEmbedStore.lease()` 永远把同一个缓存 `self._conn` 交给每个 lease**。

结果：三个并发纠正请求共用 **同一条物理连接 + 同一个事务**。三条线程的
`BEGIN / SELECT FOR UPDATE / INSERT / UPDATE / COMMIT / ROLLBACK` 在同一连接上交错：

- 失败者的 `conn.rollback()` 会**回滚赢家刚写入的行**（ON CONFLICT DO NOTHING 返回空行
  只是因为它看到了同一事务里兄弟线程尚未提交的 B 行）；
- 提交后的「fresh lease」回读其实**还是同一条共享连接**，读到的是被回滚/中间态，
  于是 `_verify_correction` 报 `durable=true` 的 readback 失败；
- 最终 `explicit_memories`/`memory_relations` 增量为 0。

`_acquire_lease` 的「fresh lease」语义在 legacy 无池路径下是假的 —— 这是唯一 choke point。

## 2. 真实证据（Y400，public HTTP `POST /tool` → `v3_update action=correct`）

### 2.1 修复前（canonical，未改动源码）RED
`conc-closure-canon/report.json`（+ `canonical-report.json`）：
3 个请求同一 barrier 同时发出（start_offset 均 0.016，end 0.047）。

- `successes=0/3`，`memory_delta=0`，`relation_delta=0`
- A 仍 `active`，B 行 0，A→B 边 0
- t0: `DURABLE_FAILED durable=true old_status=archived` 错误
  `纠正已提交但 readback 显示 ... status='active'（期望 archived）`
- t1: `DURABLE_FAILED durable=false` 错误
  `replacement memory_id 已被相同 payload 占用；未覆盖任何行`
- t2: `DURABLE_FAILED` 错误 `纠正事务失败: ForeignKeyViolation(...)`
  （类=psycopg2 ForeignKeyViolation，stage=insert_edge；**SQLSTATE 本 run 未记录**）
- 三个 receipt 的 `new_memory_id` 相同（确定性 id），`deduplicated` 全 false

### 2.2 修复前（staged 插桩副本，获取事务证据）
插桩只在 **staged 副本**上做（`staged/active_memory_store_prefix_instr.py`），
canonical 源码保持 RED 不变。JSONL 记录 thread id / `get_backend_pid()` /
autocommit / `get_transaction_status()` / 异常类 / SQLSTATE / stage。

`conc-closure-instr2/report.json`（`instr-report.json`）关键行：

```
correct_lease   tid=1456 backend=320 autocommit=true  txn=0
correct_lease   tid=9056 backend=320 autocommit=true  txn=0
correct_lease   tid=8704 backend=320 autocommit=true  txn=0      <- 同一 backend
after_begin     tid=1456/9056/8704 backend=320 autocommit=false txn=2
race_lost_insert_memory tid=9056 backend=320 txn=2               <- ON CONFLICT 空行
race_lost_rolled_back   tid=9056 backend=320 txn=0               <- 回滚共享事务
commit_attempt  tid=1456 backend=320 txn=2
committed       tid=1456 backend=320 txn=0
race_resolve_lease/edge tid=1456/8704 backend=320 found=false     <- "fresh" 回读仍共享
verify_result   status=DURABLE_FAILED
```

**三条并发纠正的 `correct_lease` 全部是 backend=320** —— 共享连接被直接证实。
多次 pre-fix 复现（同一 3×identical 场景）：

| run | successes | mem/rel delta | A | correct_lease backend |
|---|---|---|---|---|
| canonical | 0/3 | 0/0 | active | (未插桩) |
| instr2 | 0/3 | 0/0 | active | 320 (同一) |
| instr3 | 1/3 | 0/0 | active | 1844 (同一) |
| instr4 | 0/3 | 0/0 | active | 7000 (同一) |
| instr5 | 3/3 | +1/+1 | archived | 2784 (同一) |

即：**pre-fix 是时序相关的真实竞态**，3/4 次 RED，偶尔侥幸通过（instr5）。这解释了
为什么基于 fake 的并发单测一直是绿的（fake 只模拟共享连接，不会真的交错）。

> run6 历史：只有 returned payload 与 A/B/relation 状态（memory_delta=0 relation_delta=0，
> t0 durable=true readback 失败、t1/t2 ON CONFLICT 空行）在
> `cache/scratch/m01m02-two-blockers-run6.json` 有记录；
> **run6 的 exception class / SQLSTATE / statement stage / transaction state = NOT RECORDED**
> （当时未插桩，不编造）。本次 Y400 复现补齐了这些维度。

## 3. 修复（最小、限定在 scope 文件内）

文件：`src/v3-core/src/v3core/active_memory_store.py`（仅此产品文件；scoped diff 见
`scoped-repair.diff`，88 行 / +65 行）。

在唯一 choke point `_acquire_lease`：当 `pool is None` 且注入的 store 暴露其公共
`open_side_connection()` 独立连接 seam 时，优先取**真正独立的连接**（新增
`_OwnedConnectionLease`，lease 释放时清理事务并关闭该连接）。没有该 seam 的 store
（注入的 fake、调用方 stub）保持原 `pg.lease()` 路径不变。

- 未引入 global mutex / table lock / advisory lock / 通用事务重写。
- 未改 `pg_store.py` 等外部文件（根因来自 `PgEmbedStore.lease` 共享 `_conn`，但修复
  完全可收敛在 `active_memory_store.py` 的 lease 选择上）。
- 该修复让 `correct()` 的事务、提交后 `_verify_correction` 的「fresh」快照、
  `_resolve_from_committed_edge` 的 race 回读各自拿到独立连接。

### 3.1 单测 RED → GREEN（Y400 venv，pytest 9.1.1，fake-only）
新增 2 个测试（`test_m01_memory_correction.py` 末尾）：

- `test_M01_pool_less_store_lease_is_an_independent_connection`
- `test_M01_pool_less_store_without_independent_seam_keeps_legacy_lease`

```
RED  (pre-fix product + new tests): 1 failed, 1 passed   (unit-red.log)
GREEN(fixed product + new tests):   2 passed             (unit-green.log)
```

### 3.2 真实 PG 并发回归 GREEN
`conc-closure-green/report.json`（`green-report.json`）：

- 3 identical concurrent A→B：`successes=3`，`memory_delta=+1`，`relation_delta=+1`，
  A=`archived`，B 行=1，A→B 边=1；三个 caller 结果可解释：
  t2 `CORRECTION_COMMITTED`，t0/t1 `CORRECTION_DEDUPLICATED`（`deduplicated=true`，
  `durable=true`），三者 `new_memory_id`/`current_memory_id` 一致。
- 随后 sequential 同一 A→B：first `CORRECTION_COMMITTED`，second `CORRECTION_DEDUPLICATED`
  （`deduplicated=true`），delta 仍 +1/+1，无多余行 —— 原幂等未被破坏。
- connect log 显示每个 lease 独立连接（多次 psycopg2.connect，backend pid 各异）。

### 3.3 现有单测回归
- `-k "concurrent or idempotent or retry"`：12 passed（`unit-concurrency.log`）
- 整个 M01 单测文件：**184 passed**（`unit-m01-full.log`）

## 4. 变更文件与指纹

| 文件 | pre-fix sha256 | 修复后 sha256 |
|---|---|---|
| `src/v3-core/src/v3core/active_memory_store.py` | `de1fa546…96edffd` | `19d70c4349449c168f41660551a2c4aaacc149658f08a50adb18ba1b9b383856` |
| `src/v3-core/tests/test_m01_memory_correction.py` | `eae6c680…46bde6dc` | `c6a9d59c57f82b65aeebd86c808f77e09e74cd6aca35dd692c7458c8720bb7f7` |

git status：本任务只写了以上两个 scope 文件（其余 M/?? 均为既有状态）。未触碰 driver /
driver-contract test（另一位作者负责 history label）。

## 5. 复现（可复用 scratch probe）

- 探针：`m01m02_conc_probe.py`（复用 driver 的 fail-closed gates / isolated env /
  `Server` / HTTP helpers；只跑 3×identical concurrent + sequential，不跑完整 driver）
- 插桩生成器：`make_instrumented.py`（对 staged 副本；canonical 保持 RED）
- pre-fix 复原：`make_prefix.py`
- 运行脚本：`run-conc-closure.ps1`（canonical RED）、`run-instr*.ps1`、
  `run-unit-and-green.ps1`（单测 RED/GREEN + 真库 GREEN）
- Y400 run roots：`conc-closure-canon` / `-instr2/3/4/5` / `-green`（report.json + console.log）

## 6. 未测试 / 未覆盖（诚实边界）

- **run6 历史** 的 exception class / SQLSTATE / stage / txn state = NOT RECORDED（无插桩）。
- 本次三个并发请求在插桩 run 中**未抛出 DB 异常**（失败来自 ON CONFLICT 空行 / readback），
  故 SQLSTATE 为 N/A；仅在 canonical run 观察到一次 `ForeignKeyViolation`（类已记录，
  **该 run 的 SQLSTATE 未记录**）。
- 未做 full driver / 完整 Y400 验收 / 全量测试套件 / CI（按任务边界）。
- 未测 divergent（A→B 与 A→C 并发）——修复未天然触碰 conflicting-successor 逻辑，按任务
  要求不扩展（既有单测已覆盖 conflict 语义）。
- `create()` / `read_version()` 也走同一 `_acquire_lease`，现在 legacy 无池路径同样改为
  独立连接；未单独做这两条的真实 PG 并发回归（超出本 blocker 边界）。
