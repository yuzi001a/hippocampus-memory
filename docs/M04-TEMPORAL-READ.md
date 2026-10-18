# M04 — 时间点读取（temporal read）实现契约

状态：**CONFIRMED-BY-SCOPE / 最小实现契约**。由 `docs/M04-TEMPORAL-READ-SCOPE.md`（草稿）按用户 8 条修订意见定稿。
本文件是实现与验收的唯一契约来源；不覆盖 `M01`/`M02`/`M03` 的既有语义。

架构前提：**Hippocampus 负责保存历史、组织记忆、准确检索、来源追溯；Agent 负责理解、推理、判断。**
本里程碑只加"按时间点取正确版本"的检索能力，不新增任何事实推理。

---

## 0. 定位

M04 是**读取侧**能力，唯一新增能力是：**给定一个时间点，返回该时间点上版本链的真实状态**。

它不判断历史陈述在现实世界是否仍成立，不裁定原始 QA 失效，不消解跨项目冲突，不建知识图谱，
不做意图识别。查询模式（current / 何时 / 哪一版）由**宿主 Agent** 决定，插件只执行明确参数并校验。

---

## 1. 语义定义

### 1.1 `as_of` = "链在该时间点的当前版本"

`as_of` 是**链级**语义，不是节点级：`read_version(X, as_of=T)` 回答的是"X 所在版本链在 T 时刻的当前版本是什么"，
与从链上哪个节点进入无关（与今天 `mode='current'` 的行为一致）。

**重建只依据版本关系（边）的时间线，不依据版本今天的 `status` 列（active / archived）。**
今天的 `status` 是当下状态，不能反推过去。实现上：只使用 `read_chain` 返回的**链结构与边**，
**丢弃**其 `current_memory_id` / `current_status`（那两个值是当下推理的结果）。

### 1.2 两种时间基准

| `basis` | 判定字段 | 含义 | 默认 |
| --- | --- | --- | --- |
| `recorded` | `memory_relations.recorded_at` | 系统在该时刻**已经知道**的状态 | **是** |
| `effective` | `memory_relations.effective_at` | 按**用户声明**在该时刻生效的状态 | 否 |

`recorded_at` 是服务端 `NOW()`，**不可能为 NULL**，因此 `basis='recorded'` 永远可判定。

`effective_at` 是调用方声明的有效起始时间，**可以为 NULL**（NULL = 从未声明）。

### 1.3 `effective_at` 不是延迟激活（重要，不得混淆）

`effective_at` **只是用户声明的时间元数据**。写入发生在提交那一刻，行在提交后立即是活动行；
**数据库没有实施任何延迟激活、没有定时任务、没有未来生效调度**。

因此：

- `basis='effective'` 回答的是"**按声明**在 T 时刻应该生效的是哪一版"，是一个**声明视图**；
- 它**不是**、也不得被表述为"系统在 T 时刻的实际运行状态"——实际运行状态只能由 `basis='recorded'` 回答；
- 接口与文档都不得把 `effective_at` 描述为"生效时间到了才生效"。

### 1.4 边的时间不等于版本自身的时间

版本（`explicit_memories` 行）**没有"自身生效时间"这一列**。时间只存在于**转换边**上：

- 一条 `supersedes` 边的 `effective_at` 是"**这次替换被声明为从何时起生效**"，
  不是"新版本自身的生效时间"，二者不可互相冒充；
- 一条 `withdraws` 边的 `effective_at` 是"**这次撤回被声明为从何时起生效**"。

所以 M04-3 暴露边时间时，必须**带边身份**（`relation_id` / `relation_type` / 端点），
并明确标注它属于转换，不得填进任何看起来像"版本生效时间"的字段。

---

## 2. 参数与校验

```python
read_version(
    memory_id,
    mode="current",          # 已有
    include_history=False,   # 已有
    as_of=None,              # 新增：ISO-8601，必须带时区偏移
    basis=None,              # 新增：'recorded' | 'effective'；仅当 as_of 非 None 时有意义
)
```

首版**只支持 `mode='current'` + `include_history=False`**（修订 7）。

| 组合 | 结果 |
| --- | --- |
| `as_of=None`、`basis=None` | **与今天逐字段相同**（零回归） |
| `as_of=<ts>`、`basis=None` | `basis` 默认 `recorded`，并在响应中回显 |
| `as_of=<ts>`、`basis='recorded'` / `'effective'` | 正常执行，回显 `basis` |
| `as_of=<ts>`、`mode='history'` | `INVALID_REQUEST`（首版不支持，显式拒绝） |
| `as_of=<ts>`、`include_history=True` | `INVALID_REQUEST`（`history` 语义不变，组合未定义） |
| `as_of=None`、`basis` 非 None | `INVALID_REQUEST`（没有时间点就没有基准，拒绝静默无效参数） |
| `as_of` 无时区 / 无法解析 / 空串 | `INVALID_REQUEST`（沿用既有 `_normalize_effective_at` 的"谁的 now？"规则） |
| `basis` 不在枚举内 | `INVALID_REQUEST` |

`as_of` 允许是**未来时间**（见 §4.4）。

---

## 3. 重建算法（唯一合法过程）

```
1. 取链结构：rows + edges（复用 read_chain；只用结构，丢弃其 status 推理）
2. 链首 = 最老的祖先版本
3. 存在性下界：若 as_of < 链首的 created_at → NO_VERSION_AT_AS_OF（链当时还不存在）
4. 从链首沿出边向前走，对每条被遍历到的边 e：
     basis='recorded'  → t = e.recorded_at            （永不为 NULL）
     basis='effective' → t = e.effective_at
                         若 t IS NULL → EFFECTIVE_TIME_INDETERMINATE（停止，不猜）
     若 t <= as_of → 该转换已生效：current = e.to_memory_id，继续
     否则          → 该转换尚未发生：停止，current 保持
5. 若停下的那条边是 'withdraws' 且已生效 → 状态 withdrawn（无当前版本）
```

### 3.1 相关性界定（修订 2：不机械地对整条链的任意 NULL 判失败）

判定只消费**遍历到的那条边**：

- 一旦遇到"尚未生效"的边就停止，**其后的边一律不参与判定**——后面边上的 `effective_at` 是否为 NULL
  与本问题无关；
- **单版本链（无边）**不需要任何时间证据即可判定；
- 只有**必须消费却读不到** `effective_at` 时才判不可确定。

因此失败条件是"**本次判定所必需的证据缺失**"，不是"链上存在 NULL"。

### 3.2 不可确定的返回形态

`EFFECTIVE_TIME_INDETERMINATE` 是**明确的不可确定状态**，不是错误推断：

- `success=False`、`error_code='EFFECTIVE_TIME_INDETERMINATE'`
- `memory=None`、`resolved_memory_id=None`、`current_status='indeterminate'`
- 但仍返回**已确认的部分证据**：`timeline`（走过的版本与边）+ `indeterminate_edge`（缺证据的那条边）
- **不得**用 `recorded_at`、`created_at`、正文里的日期文字去替代缺失的 `effective_at`

---

## 4. 响应契约（仅 `as_of` 非 None 时新增字段）

在原 `mode='current'` 响应体上**新增**，原有键不改名、不删除：

| 字段 | 值 |
| --- | --- |
| `as_of` | 归一化后的 UTC ISO 字符串（回显） |
| `basis` | `'recorded'` / `'effective'`（回显，默认值也回显） |
| `as_of_future` | `as_of > now` 时为 `true` |
| `query_status` | `'OK'` / `'EFFECTIVE_TIME_INDETERMINATE'` / `'NO_VERSION_AT_AS_OF'` |
| `current_status` | `'active'`（该时间点上有当前版本）/ `'withdrawn'` / `'absent'` / `'indeterminate'` |
| `timeline` | `{versions: [...], edges: [...]}`，每条边带 `decision` |
| `indeterminate_edge` | 不可确定时给出缺证据的边，否则 `null` |

`timeline.versions[]` 每项：`{memory_id, label, is_current_at_as_of, created_at}`，`label` ∈
`current` / `superseded` / `future` / `withdrawn` / `indeterminate`（`future` = 在 T 时刻尚未生效；
`indeterminate` = 缺少本次判定必需的证据，无法断言该版本在 T 时刻的地位——不猜，也不降级成 `future`）。
`timeline.edges[]` 每项：`{relation_id, relation_type, from_memory_id, to_memory_id, effective_at, recorded_at, decision}`，
`decision` ∈ `applied` / `not_yet` / `indeterminate`。**只包含本次判定实际消费的边**（按遍历顺序）：
走到第一条「尚未发生」或「不确定」的边走完即停，其后的边不出现在这里——不把没查过的边写成结论。

### 4.1 `current_status` 的来源声明

`as_of` 路径下的 `current_status` **只由边的时间线推导**，不读 `explicit_memories.status`。
因此 `'active'` 的含义是"**在重建出的时间线里，该时间点上存在当前版本**"，**不是**"该行现在是 active"。

### 4.2 撤回

`as_of` 落在撤回边之前 → 返回被撤回前的版本；落在撤回边之后 → `memory=None`、
`current_status='withdrawn'`、`resolved_memory_id=None`（与今天 `mode='current'` 遇撤回同形）。

### 4.3 链不存在

链首创建时间晚于 `as_of` → `success=True`、`query_status='NO_VERSION_AT_AS_OF'`、
`current_status='absent'`、`memory=None`。这是**答案**（当时确实没有），不是错误。

### 4.4 未来时间

允许。`basis='recorded'` 下未来 `as_of` 等于当前状态（`as_of_future=true` 明示）；
`basis='effective'` 下未来 `as_of` 会包含已声明但尚未到期的转换——这正是声明视图的用途。

### 4.5 完整性检查不被绕过

时间过滤**不得**绕过既有 fail-closed：成环、链端点缺失仍然返回 `DURABLE_FAILED` 并拒绝给出答案。
同一响应仍来自**同一个** READ ONLY REPEATABLE READ 快照。

---

## 5. M04-3 时间字段补全（范围收窄，修订 6）

只做**语义明确、来源可追溯、零额外查询**的补充：

- `read_chain` 已经一次性加载了 `edges`。在 `versions[]` 的每一项上附加
  `transition = {relation_id, relation_type, effective_at, recorded_at}`，取自该版本**出边**；
  无出边则 `transition=None`。
- 该字段名与结构都表明它属于**转换边**，不是版本自身的时间。

**明确不做**：

- 不在召回路径（`search_keyword` / `search_vector`）上为补时间字段而增加 JOIN——
  没有真实需求，且会改到召回路径。召回结果维持现状。
- 不新增 `effective_at` 索引（无性能证据，修订 8）。
- 不给版本行伪造"自身生效时间"。

---

## 6. 明确不做

1. 不用 LLM 判断历史陈述在现实世界是否仍成立。
2. 不据 Observer / 主题卡摘要自动裁定原始 QA 失效。
3. 不自动消解跨项目事实冲突。
4. 不决定用户提问深层语义，不产出最终答案。
5. 不建通用时序知识图谱、事实裁判引擎、新认知层。
6. 不做插件内意图识别；查询模式由宿主 Agent 选择。
7. 缺 `effective_at` 时不推断（不回落 `recorded_at`、`created_at`、正文日期、相邻记录）。
8. 不做双时间数据库（bi-temporal）通用化，不加 `valid_time` / `transaction_time` 体系。
9. 不改 QA 召回、RRF、主题卡、Observer、M03 边界（修订 8）。
10. **不做 M04-2 区间检索**（修订 3，延后）。
11. 不重跑已完成的全量历史测评。

---

## 7. 验收场景

| # | 场景 | 期望 |
| --- | --- | --- |
| 1 | `as_of=None` 的 `current` / `history` / `include_history=True` | 与改动前逐字段相同（零回归） |
| 2 | A→B→C，`as_of` 落在三段内（`basis='recorded'`） | 依次解到 A / B / C |
| 3 | A→B→C，`as_of` 落在某条边之前 | 解到该边之前的版本 |
| 4 | 撤回前 / 撤回后 | 撤回前得旧版本；撤回后 `memory=None` + `withdrawn` |
| 5 | `basis='effective'`，相关边 `effective_at` 为 NULL | `EFFECTIVE_TIME_INDETERMINATE`，**不**回落 recorded |
| 6 | 同上但 NULL 位于判定停止点**之后** | 正常判定（不因无关 NULL 失败） |
| 7 | 单版本链（无边）+ `basis='effective'` | 正常判定，不要求时间证据 |
| 8 | `as_of` 无时区 / 非法字符串 / 空串 | `INVALID_REQUEST` |
| 9 | `as_of` + `mode='history'`；`as_of` + `include_history=True`；无 `as_of` 但有 `basis` | 各自 `INVALID_REQUEST` |
| 10 | 未来 `as_of` | 正常返回，`as_of_future=true` |
| 11 | `as_of` 早于链首创建 | `NO_VERSION_AT_AS_OF` + `absent` |
| 12 | 成环 / 端点缺失 + `as_of` | `DURABLE_FAILED`，fail-closed，不给答案 |
| 13 | 并发写者在读取中提交新版本 | 单快照一致：结果不混两个状态 |
| 14 | 未知 id + `as_of` | `NOT_FOUND` |
| 15 | `versions[].transition` | 有出边时带边身份与时间；无边时 `None`；命名体现其属于转换 |

---

## 8. 涉及模块

| 文件 | 改动 |
| --- | --- |
| `src/v3-core/src/v3core/active_memory_store.py` | `read_version` 增加 `as_of`/`basis` 校验与分派；新增 `_read_version_as_of`；`read_chain` 的 `versions[]` 附加 `transition`；新增错误码与解析助手 |
| `src/v3-core/src/v3core/tools/memory_correction.py` | 工具层透传并校验 `as_of` / `basis`（与既有 `_read_mode` / `_include_history` 同规格） |
| `src/v3-core/tests/test_m04_temporal_read.py` | 新增：上表 15 个场景 |
| `src/v3-core/tests/test_m02_memory_correction_tools.py` | 参数面拒绝用例 |
| `docs/M03-CORRECTION-PROPAGATION.md` | 仅更新"Not in scope"中 M04 已交付的部分 |

**不改**：`recall_pool.py`、`assistant_delivery.py`、`observer.py`、主题卡相关、`derived_*.py`、任何 SQL 迁移。

---

## 9. 附带修复的既有缺陷（不在 M04 原范围）

M04 的隔离真 PG 验收在做 A→B→C 时撞到一个**先于 M04 存在**的缺陷，不修就无法证明
`as_of` 的输入侧（写入的 `effective_at`）是可信的：

**症状**：`correct(..., effective_at="2026-03-16T00:00:00+00:00")` 事务提交成功、行与边都写进去了，
但收据返回 `DURABLE_FAILED`。

**根因**：提交后的 readback 校验把 `effective_at` 当**字符串**比较。`timestamptz` 回读时按服务端
会话时区渲染，同一个瞬间可能读成 `2026-03-16T08:00:00+08:00`，与调用方传入的 `+00:00` 字面不等。

**修法**：`_same_instant()`——两侧都能解析为时间戳时按**瞬间**比较，否则维持严格字符串相等。
真正不同的声明时间、无法解析的值仍然报错，不放过真实不一致。

**边界声明**：这是 M01 纠正校验路径的缺陷，不是 M04 引入的，也不扩大 M04 语义；改动的等价条件是
"同一瞬间"而非"任意宽松"。回归见 `test_M04_effective_at_readback_compares_instants_not_strings`。
