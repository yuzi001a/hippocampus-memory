# V3 Write Flow Contract

> Public-alpha contract for write paths that produce canonical explicit
> memory. It pins the *opt-in ownership* rule for durable writes; it
> does not describe the implementation history or any specific branch.

## A0. Explicit-memory opt-in ownership

`public.explicit_memories` is the canonical durable store for explicit
memory. The supported active write entry points
(`v3_store` / `v3_add` / `hm_write` / `v3_extract(write=True)` /
`v3_manage(action="extract", write=True)`) all ultimately commit a row
to this table.

### A0.1 Opt-in rule

Every call into one of the supported active write entry points is an
**opt-in durable write** that requires **explicit authorization**.

Allowed authorization:

- The user explicitly asks to remember / store / save / retain a
  *specific* durable item (title, content, category are user-supplied
  or explicitly accepted by the user).
- An explicitly authorized host workflow (handbook sync, seed import,
  user-initiated archive, host configuration that enables the path)
  requests the write.

Forbidden (do **not** authorize / 禁止):

- Development experience, reviewer findings, debugging notes /
  开发经验、评审发现、调试笔记.
- Task status / task summary / "summarize tonight" /
  任务状态 / 任务摘要 / "今晚总结一下".
- Implementation decisions, inferred preferences or facts /
  实现决策、推断出的偏好或事实.
- Generic lessons, generic best practices / 通用 lesson、通用 best practice.
- Any passive observation derived from session traffic / 被动观察到的会话流量.

### A0.2 Passive paths are separate

The passive paths (`sync_turn` / observer / E1 / topic store /
deduplication / etc.) own their own derived state. They must **not**
silently promote into explicit memory. The supported canonical
explicit-memory table is changed only through the supported active
write entry points listed above.

### A0.3 Runtime scope

This contract pins the description / default layer of the schema
dicts and the wording in the v3-workflow skill. It does **not** add a
policy engine, an LLM intent classifier, or a runtime interceptor; it
does not alter the call graph of any handler.

## Non-claims

- No LLM intent classifier is added.
- No policy engine / runtime interceptor is added.
- The runtime handlers and the canonical store schema are unchanged
  by this contract.
