# ADR: Generated memory is untrusted until it passes the contract of the surface that consumes it

**Status:** accepted
**Date:** 2026-09-21
**Incident class:** `MODEL_OUTPUT_BOUNDARY_ESCAPE` / `HIGH_TRUST_CONTEXT_CONTAMINATION`
**Scope:** every LLM-generated artifact that is persisted and then injected into future context

---

## 核心原则

> **模型生成的记忆，在通过其注入面的契约校验之前，一律是不可信派生数据。**
>
> *Generated memory is untrusted until it passes the contract of the surface that will consume it.*

这句话是本次事故的产物，也是本 ADR 唯一要立住的东西。

---

## 两类东西，必须分开对待

### 1. Source truth（源真值）

原始对话、印、用户的输入、`y_*.md`。**不可变。**

本次事故里这些**一个字都没动**——污染的是派生文件。这不是运气，是"源不可变"这条线守住了。任何时候都不允许为了"清理污染"去改写、清洗或全局替换源数据。

### 2. Generated high-trust context artifact（生成式高信任上下文产物）

模型生成 → 落盘 → **自动、固定地注入未来每一次对话的上下文**。

这类东西有两个致命属性叠加：

- **它是模型输出。** 模型会越界、会续写、会跑出 chat 模板、会自称另一个身份。
- **它被当成系统级事实注入。** 注入面默认它可信，没有任何质疑环节。

两者相加 = 一次生成失误，污染**此后每一个新会话**，且无法从已污染会话的历史中撤回。

`identity_block.md` 和 `situation_overview.md` 是当前仅有的两个此类产物。

---

## 事故链（已由源码与运行时证据确认）

```
LLM generation overrun
   ↳ 输出预算失控：_chat_minimax 写死 max_completion_tokens = 131072，而契约只要 300–500 字
→ raw model output accepted
   ↳ 写入闸门只有 `if not content: raise` —— 即"非空即合格"
→ persisted into situation_overview.md
   ↳ os.replace 原子替换 —— 坏候选直接销毁上一个好文件
→ fixed high-trust injection
   ↳ _read_situation_overview() 零校验，读到什么注入什么
→ every new session receives polluted context
```

契约**一直写在 prompt 里**（三段固定顺序、300–500 字、第三人称）。它从未被执行。**契约在散文里，不在代码里。**

### 同一缺陷链在 `identity_block.md` 上完整存在

`identity_block.md` 与 `situation_overview.md` 是同一写路径（`e1.synthesize_yin`）、同一 provider、同一 chat 模板产出的两个注入面。事故当时它具备**每一步**相同的缺口：

```
identity call 无 per-call 预算（走 provider 默认 131072）
→ 写入闸门同样只有 `if not identity_content: raise`
→ os.replace 原子替换 —— 坏候选销毁上一份好身份块
→ _compress_yin_to_identity() 读到什么返回什么（逐字注入）
→ 文件缺失时的 legacy 印截断兜底同样零校验
```

它**没有**在本次事故里被触发（被污染的是态势总览），但形状相同 —— 所以按"同一缺陷类一并修"处理，不等它真被污染。

---

## 决定

任何生成式高信任上下文产物，必须有**三层闭环**，缺一不可：

| 层 | 做什么 | 为什么不能省 |
|---|---|---|
| **1. bounded generation** | per-call 输出预算 | 不给模型"写完之后继续生成"的预算空间 |
| **2. write-side validation** | 落盘前校验结构/长度/标记/角色边界 | 坏候选**绝不落盘**；失败即整体拒绝，不截断、不猜结尾、不覆盖 last-known-good |
| **3. read/injection-side validation** | 注入前再校验一次 | 注入边界是最后一道闸：writer 出 bug、手工文件污染、旧版本坏文件、外部恢复旧产物，都挡在这里 |

**只做其中一层都不算数。** 只做写入校验 → 历史遗留的坏文件仍会注入；只做读取校验 → 坏文件会覆盖好文件，好状态被销毁。

### 附带决定

- **不依赖 stop sequence 作为安全边界。** 停用词是"这次泄漏出来的那个标记"，下次可能是别的模板，或者根本没有标记。标记检测是**辅助**，结构+长度+角色边界才是主防线。
- **标记检测必须是通用 pattern，不是字面量。** 只为 `]<]minimax[>[` 写死一条规则，等于为上一次事故打补丁。
- **校验器必须是纯函数、模型无关、无 I/O。** 写入端与读取端共用**同一个**校验器，否则两边会漂移。
- **拒绝必须 fail-closed。** 校验失败 → 整份丢弃。有已验证的旧文件就保持旧文件不动，没有就保持缺失。
- **缓存只能缓存"通过过校验"的值。** 不允许"磁盘上是新的但非法 → 回退到更旧的、从未校验过的缓存块"。

---

## 这条原则的推广

判定一个 LLM 产物是否属于本 ADR 管辖，问一个问题：

> **它会不会在没有人再看一眼的情况下，自动进入未来对话的上下文？**

- 会 → 高信任注入面 → 必须三层闭环。
- 不会（召回候选、用户主动查询结果）→ 不在本 ADR 的强制范围内，但仍应视为不可信数据。

---

## 反面模式（本次犯过的）

- ✗ "非空就写" —— 把"模型返回了东西"当成"模型返回了合格的东西"。
- ✗ 契约写在 prompt 里就以为契约存在了 —— prompt 是请求，不是保证。
- ✗ 用 `os.replace` 做原子写，却没有在写之前校验 —— 原子性保证的是"要么旧要么新"，不是"新的合格"。
- ✗ 读取端零校验 —— 把注入面当成可信消费者。
- ✗ 异常时无条件回退缓存 —— 把"读到坏东西"降级成"继续用可能也是坏的东西"。
- ✗ 为事故里的具体字符串写检测规则 —— 修的是这一次，不是这一类。

---

## 与本次修复的对应

| 决定 | 落地 |
|---|---|
| bounded generation | `LLMClient.chat(..., max_output_tokens=)` 加法式 per-call 预算；situation overview 用实测标定的小上限 |
| write-side validation | `generated_context_contract.validate_situation_overview()`；失败整体拒绝并记录 failure class / 长度 / 指纹 / provider |
| read/injection-side validation | `_read_situation_overview()` 注入前走**同一个**校验器；失败返回空串 |
| 同一缺陷类一并修 | `identity_block.md` 同结构、同缺口，同批修复（不等它真被污染） |
| 源不可变 | `y_*.md`、历史对话、PG 历史：本次 0 修改 |

### `identity_block.md` 的落地（同批补齐）

| 层 | 落地 |
|---|---|
| bounded generation | identity 调用带 `max_output_tokens=IDENTITY_BLOCK_MAX_OUTPUT_TOKENS`（1024：契约 500 字 ≈559 token 的 1.83 倍余量，生产实测合法 616 字 ≈700 token 的 1.46 倍余量；provider 默认 131072 不变） |
| write-side validation | `validate_identity_block()`；失败整体拒绝、不写文件、上一份已验证文件逐字节保留 |
| read/injection-side validation | `_compress_yin_to_identity()` 返回前校验；缓存只存通过校验的值；"更新但非法" → `""`，不回退旧缓存；读异常时缓存须在**本次调用**同样通过校验 |
| 兜底不得绕过边界 | 文件缺失时的 legacy 印截断兜底**同样**过 `validate_identity_block()`，不过则返回 `""` |

**契约差异（刻意的，不是漏做）**：身份块是**自由第一人称散文**，prompt 从未规定标题，生产实测合法产物是五段无标题中文（约 616 字 / 1695 B）。因此：

- **不套用三段标题契约** —— 拿态势总览的标题要求去卡身份块会把合法产物全部拒掉；
- **不设 300–500 字下限** —— 合法产物本身已超出该窗口（616 字），任何按窗口卡的下限都会把**当前生产文件**判为非法，读侧会把合法身份块从注入里摘掉；
- **裸"我是 <拉丁名>"不算越界** —— 身份块的主题就是"我是谁"，裸自述是合法语义；只拦**模型身份声明**（问候 + 自报，或自报 + 模型类别词）。

---

## 一句话总结

**模型输出是输入，不是结论。** 在它变成"系统事实"之前，必须有人（代码）按消费它的那个面的契约验一遍。
