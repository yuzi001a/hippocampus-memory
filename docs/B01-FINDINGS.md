# B01 findings — 本轮实测到的两个**既有**缺陷（未修，不属 B01 范围）

发现方式：隔离真机 E2E（`src/v3-core/eval/b01_host_contract_e2e.py`）与全新库 bootstrap。
两条都不是 B01 引入的，也都**没有**在本轮顺手改（避免把传输层的改动和别的修复混在一起）。

---

## F1 — 打包 bootstrap 在全新库上必然失败（splice 顺序）

```text
复现    docker run pgvector/pgvector:pg17（全新空库）
        hippocampus bootstrap --dsn postgresql://.../<newdb>
结果    rc=1
        {"result": {"applied": false,
                    "error": "UndefinedTable('relation \"public.observation_notes\" does not exist')"}}
```

根因（读 schema 原文得出，非推断）：

```text
src/v3-core/schema/alpha_bootstrap.sql:139
  -- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/observation_embedding_chunks.sql <<<
src/v3-core/schema/alpha_bootstrap.sql:269
  CREATE TABLE IF NOT EXISTS public.observation_notes (...)

observation_embedding_chunks.sql:9
  REFERENCES public.observation_notes(id) ON DELETE CASCADE
```

marker 被**原地展开**，而它排在 `observation_notes` 建表之前 → 外键引用不存在的表 → 整批回滚。

影响：任何**全新安装**走打包 bootstrap 都拿不到 schema。存量库不受影响（早已建好）。
B01 的 E2E 因此改为按依赖顺序手工应用同一批 artifact
（`alpha_bootstrap.sql → explicit_memories.sql → qa_embedding_chunks.sql → observation_embedding_chunks.sql → embedding_failures.sql`），
并在证据里显式标注 `bootstrap_fallback.packaged_bootstrap_failed = true`。

建议修法（未实施）：把 `alpha_bootstrap.sql` 里的
`observation_embedding_chunks.sql` marker 移到 `observation_notes` 建表之后（与 `qa_embedding_chunks.sql`
相对 `qa_pairs` 的次序一致）。

---

## F2 — outbox 重放是 at-least-once：崩溃窗口内 source 可能二次写入

```text
复现    run1 收到事件 → 进程在 PG ack 之前被终止 → run2 启动
结果    run2 的 LiveBuffer._recover_live_pending 重放未 ack 项
        → conversation_stream 出现同一身份的第二次写入（stream 行数 +1）
        同批次的派生层不重复（qa_pairs 行数不变）
```

为什么既有：`conversation_stream` 没有身份唯一键（无 `msg_id`/`event_id` 列），
重放只能靠 outbox 文件，无法判断"这行到底写没写进去"。这是 at-least-once 的固有代价。

B01 做了什么：**没有**改变这条语义，但让"重复"第一次变得可识别 ——
`event_status()` 探针 + `duplicate` ACK 让调用方能看到身份已存在；
派生层靠"重复事件跳过派生 + `source_id` 幂等"保证 QA 不增长（E2E 已断言）。

建议修法（未实施，属后续）：给 source 层补一个按身份的 durable 接收索引
（B01 §4 已授权的 additive receipt seam），让重放能判"已写则不再写"。

---

## 附：本轮 E2E 的实测数字（`evidence/b01-e2e-report.json`）

```text
serve --port 0 --ready-json   → 实际端口 62864（不是 0）✓
single event                  → accepted=true duplicate=false
same-process duplicate        → accepted=false duplicate=true，行数不变
restart duplicate             → accepted=false duplicate=true，身份行数不变
same ids / other host         → accepted=true（不碰撞）
missing assistant             → turn2 记为 incomplete（answer 空），从未配到 turn3
out-of-order assistant        → 源层收下，不猜配对（未派生 QA）
writer rejection              → HTTP 503 + ok=false + status=retryable
legacy payload（无 host）      → 200 + host=legacy（旧契约不破）
prefetch                      → hit=true（合成历史可召回）
```
