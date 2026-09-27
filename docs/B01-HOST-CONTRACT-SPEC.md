# B01 — 宿主契约收敛规格（冻结版）

> 目标：把现有 MCP/HTTP 能力收敛成 DSH、pi 都能依赖的**最小稳定宿主契约**。
> 不是重写 bridge，不是实现 adapter，不是多 Agent 共享（那是 X03）。
> 优先级：`G1 > G2 > G3 > G4`。真正影响 correctness 的只有 G1。

## 0. G1 事实模型（修正后的正确表述）

```text
EVENT_INGEST_CORRECTNESS_DEPENDS_ON_EPHEMERAL_PROCESS_STATE
```

不是 `CLIENT_RESENDS_WHOLE_SESSION_EVERY_REQUEST`。

由代码逐处读出的事实：

| 层 | 现有机制 | 真值位置 | 问题 |
|---|---|---|---|
| 源层去重 | `LiveBuffer.enqueue()` → 磁盘 durable marker | `j/_pending/<job_id>.json`（content-sensitive hash）+ `j/_accepted/<session>/<msg_id>` 墓碑 | **命中墓碑时 `return True`**，与首次接受不可区分 |
| 源层 delta 游标 | `context.synced_message_ids` | **进程内** | fresh Core 即空 → 重发快照全量视为"新" |
| 派生层唯一键 | `qa_pairs.source_id UNIQUE` + `ON CONFLICT DO NOTHING` | PG（durable）✓ | `source_id = qa_sync/{session}/{q_turn}/{q_msg_id16}`，而 `q_turn = context.turn` **进程内自增，fresh Core 归 0** |
| 配对状态 | `context.pending_qa` | **进程内** | 重启后 pending 丢失 |
| 事件契约 | `session_id/msg_id/content/role/turn_id` | 进程内 `msg_buffer[session]` | 无 host 维度；buffer 无上限 |
| `/events` ACK | `{"ok": true}` 恒定 | — | 结构上无法说真话（`sync_turn` 返回 `None`） |

**重启重复的真实链**：fresh Core → `synced_message_ids` 空 + `turn` 归 0 → 重发同一事件 →
`enqueue` 命中墓碑 return True（`source_ingest_failed=False`，不阻断）→ 走配对 → flush →
用**新的 q_turn** 组出**新的 source_id** → `ON CONFLICT` 不命中 → **QA 行重复**。

## 1. 本轮四件事

```text
G1  durable event identity + idempotent ingest      ← correctness
G2  bridge version / capability handshake
G3  minimal owned-process lifecycle
G4  host/session/project/event identity contract
```

## 2. 设计决定（先复用，不加表）

**schema = NONE。** 理由：源层 durable 身份（`j/_accepted` 墓碑）与派生层 durable 唯一键
（`qa_pairs.source_id`）**都已存在**，缺的是"可区分的状态信号"和"身份里的 host 维度"，
二者都能在不加表的前提下补齐。§4 授权的 additive receipt 表**不需要启用**。

### 2.1 新模块 `src/v3core/bridge_contract.py`（唯一契约真值源）

```python
BRIDGE_PROTOCOL_VERSION = "b01.1"
LEGACY_HOST = "legacy"
STATUS_ACCEPTED = "accepted"; STATUS_DUPLICATE = "duplicate"
STATUS_RETRYABLE = "retryable"; STATUS_FAILED = "failed"

def normalize_event(payload: dict) -> dict
    # 必填: session_id, event_id（或兼容 msg_id）
    # host 缺失/空 → LEGACY_HOST；返回含 legacy_host: bool
    # 保留: project_id / agent_id / turn_id / parent_event_id / branch_id / timestamp
    #       / tool_calls / tool_results / role / content
    # 缺必填 → ValueError

def event_identity(event: dict) -> tuple[str, str, str]   # (host, session_id, event_id)

def qa_pairing_decision(*, role, event_turn_id, pending_turn_id,
                        pending_has_answer) -> str
    # "open"        user 且无 pending
    # "flush_open"  user 且 pending 存在（上一回合结束）
    # "append"      assistant：turn_id 缺省（legacy）或与 pending_turn_id 相同
    # "hold_orphan" assistant：turn_id 与 pending 不同 → 暂存，不猜
    # "skip"        其余（tool/injection 等由调用方先行处理）

def ack_from_receipt(receipt: dict) -> dict
    # 新事件 durable 成功 → {"ok": True,  "accepted": True,  "duplicate": False, "status": "accepted"}
    # 已存在事件        → {"ok": True,  "accepted": False, "duplicate": True,  "status": "duplicate"}
    # writer 拒收       → {"ok": False, "accepted": False, "duplicate": False, "status": "retryable"}
    # 内部失败          → {"ok": False, ..., "status": "failed"}
    # 恒带 event_id；有则带 source_id；恒带 host
    # 硬规则：失败写入绝不能 ok=True

def get_bridge_capabilities() -> dict
    # {"bridge_protocol_version": "b01.1",
    #  "core_package_version": <importlib.metadata.version("v3-core")，缺则 "unknown">,
    #  "server_version": <serve 现有常量>,
    #  "capabilities": {"events": "v1", "prefetch": "v1", "tools": "v1",
    #                   "source_read": "v1", "status": "v1"}}
    # 禁止硬编码 release tag v0.2.2；禁止声明 automatic_recall/correction/multi_agent_share
```

### 2.2 源层（`ingest.py`）

```python
def event_status(self, session_id, msg_id, host=LEGACY_HOST) -> str
    # "durable"（墓碑存在）/ "pending"（outbox 存在）/ "absent"
def _live_accepted_path(self, session_id, msg_id, host=LEGACY_HOST) -> Path | None
    # host ∈ ("", LEGACY_HOST) → 现有路径模板（**逐字节不变**，保护存量墓碑）
    # 其他 host → host 作用域路径（新客户端不碰撞）
```

`enqueue()` 的既有返回契约（True/False/None）**不得改动**。

### 2.3 `sync_turn`（`__init__.py`）

- 新增可选 `host=None` 与 `events=None`（新调用方传结构化事件；旧调用方只传 messages 仍可用）。
- 返回值升级为 receipt（旧调用方忽略返回值不受影响）：
  `{"ok": bool, "accepted": [...], "duplicate": [...], "rejected": [...], "incomplete": [...], "host": ...}`
- **重复即跳过派生**：源层 `durable`（PG 已 ack）**或 `pending`**（durable outbox 标记已存在、
  尚未 ack）⇒ 不再走配对/flush（这是重启重复的根治点）。两者对调用方都是 `duplicate`。
- 进程内 delta 游标（`synced_message_ids` / `processed_this_call`）按 **host 作用域**
  （裸 key 保持不变；来源/QA 身份各自承载 host），否则同一 native session/event 在不同宿主间会互判重复。
- 配对改用 `qa_pairing_decision()`；`hold_orphan` 的 assistant **不配对**（源层照收），
  未配对的 question 记入 `incomplete`（允许 answer 空串，**禁止**配到别的 turn 的 assistant）。
- 既有注入过滤 / tool 累积 / 异步 flush / `_pending_lost` 兜底全部保留。

### 2.4 `/events`（`serve.py`）

- 入参兼容旧 payload；新增 `host/event_id/project_id/agent_id/parent_event_id/branch_id`。
- ACK 由 `ack_from_receipt()` 产出，四态 + `event_id` + `source_id` + `host`。
- `writer 拒收（enqueue=False）` ⇒ `ok=false, status=retryable`，绝不显示"已记住"。
- `msg_buffer` 加有界回收：`max_sessions / max_events_per_session / idle_ttl`，常量或配置；
  **不再承担 correctness 权威**（仅性能优化）。
- `GET /health` 增量加 `bridge_protocol_version/core_package_version/server_version/capabilities`
  （向后兼容，旧键不动）。

### 2.5 MCP（`mcp_server.py`）

- `v3_health` 复用同一 `get_bridge_capabilities()`；禁止在 MCP 侧硬编码第二份 capability。
- stdio 断开 → `core.shutdown()` + 干净退出（保留现有行为并补测试）。

### 2.6 G3 生命周期（`serve.py` / CLI）

- `--port 0` 时 readiness 必须报告**实际绑定端口**（不得打印 0）。
- 新增 `--ready-json`：绑定成功后向 stdout 打印**一条**确定性 JSON ready 事件。
- `SIGTERM`/`Ctrl-C` → graceful：停止接受 → `core.shutdown()` → 退出。
- 不做 Windows service / systemd / port registry / daemon supervisor。

## 3. backward compatibility

- 旧 `/events` payload（无 host）⇒ `host="legacy"`，路径模板与 source_id 语义保持存量一致。
- 新 DSH/pi 客户端**必须显式发 host**（G4 的意义：留下来源身份，避免 B03 之后不知道来自哪个宿主）。
- 旧调用方（不传 host/events）走 legacy 分支，行为不变。

## 4. 安全与 source 原则

禁止：durable receipt 复制 raw content；capability/log 打印私人正文；为 event identity 改写 source；
因重复而删除旧 source；为 bridge 增加新的模型调用。**B01 是传输/身份层，不是记忆生成层。**

## 5. 测试门禁

event contract：single / duplicate same-process / duplicate post-restart / missing turn /
out-of-order / writer rejection / backend unavailable。
identity：同 session+event、不同 host → 不碰撞。
capability：HTTP `/health` 与 MCP `v3_health` 真值同源。
lifecycle：MCP connect→health→disconnect→clean exit；HTTP owned child spawn→ready→health→terminate→clean exit。
compatibility：旧 payload 仍可用。regression：B01 targeted + serve/mcp/event 测试 + required CI。
**不重跑**：全库 embedding audit / LoCoMo / historical repair / A01 differential。

## 6. 已知限制（显式申报，不在本轮修）

- 崩溃窗口内"源已 durable 但 QA 未派生"的记录会被跳过（不猜）。恢复路径 = 既有 mapper 回填。
- `RecallTrace` 在 Core 路径的 per-lane 汇总为空（A03 遗留 backlog）。
- 多宿主并发共享记忆、权限隔离、冲突消解仍属 **X03**，本轮只保留身份字段。
