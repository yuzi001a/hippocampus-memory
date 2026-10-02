// UNIT tests only. Every HTTP call and every child process here is an
// in-process UNIT fixture — nothing in this file talks to a real bridge, a
// real v3core, PostgreSQL, a model provider or pi itself.
import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { MemoryAdapter, mapEntry, textOf, MEMORY_CUSTOM_TYPE } from "../src/adapter.js";
import { BridgeClient, OwnedBackend, loadConfig, configPathFor, HOST_ID, REQUIRED_PROTOCOL } from "../src/bridge.js";
import hippocampusExtension from "../src/index.js";

// ── UNIT fixtures ─────────────────────────────────────────────────────────
function fakeFetch(routes) {
  const calls = [];
  const impl = async (url, init = {}) => {
    const full = String(url);
    const path = full.replace(/^https?:\/\/[^/]+/, "");
    const host = full.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ url: full, host, path, method: init.method || "GET", body });
    const route = routes[path];
    if (!route) return { status: 404, json: async () => ({ ok: false, error: "not found" }) };
    const reply = typeof route === "function" ? route(body, calls.length) : route;
    if (reply instanceof Error) throw reply;
    return { status: reply.status || 200, json: async () => reply.payload };
  };
  impl.calls = calls;
  return impl;
}

function ctxFor(entries, { sessionId = "sess-1", cwd = "/proj" } = {}) {
  return { cwd, ui: { notify() {} }, sessionManager: {
    getSessionId: () => sessionId,
    getEntries: () => entries,
  } };
}

function userEntry(id, parentId, text, timestamp = "2026-10-02T01:00:00.000Z") {
  return { type: "message", id, parentId, timestamp, message: { role: "user", content: text, timestamp: 1 } };
}
function assistantEntry(id, parentId, text, extra = {}) {
  return { type: "message", id, parentId, timestamp: "2026-10-02T01:00:01.000Z",
    message: { role: "assistant", content: [{ type: "text", text }], stopReason: "stop", ...extra } };
}

const OK = { status: 200, payload: { ok: true, status: "accepted", host: "pi" } };
const config = { mode: "external", bridgeUrl: "http://127.0.0.1:9", timeoutMs: 500, memoryBudgetChars: 8000 };

function makeAdapter(routes, overrides = {}) {
  const fetchImpl = fakeFetch(routes);
  const warnings = [];
  const adapter = new MemoryAdapter({ config, fetchImpl, warn: (m) => warnings.push(m), ...overrides });
  return { adapter, fetchImpl, warnings };
}

// ── 1. mapping: native identity, parent, timestamp, text ──────────────────
test("mapEntry preserves native entry id, parent, timestamp and text", () => {
  const event = mapEntry(userEntry("a0b1c2d3", "root0", "what is the retention policy?"), {
    sessionId: "sess-1", projectId: "/proj",
  });
  assert.equal(event.host, HOST_ID);
  assert.equal(event.session_id, "sess-1");
  assert.equal(event.event_id, "a0b1c2d3");
  assert.equal(event.parent_event_id, "root0");
  assert.equal(event.timestamp, "2026-10-02T01:00:00.000Z");
  assert.equal(event.content, "what is the retention policy?");
  assert.equal(event.role, "user");
  assert.equal(event.project_id, "/proj");
});

test("mapEntry emits no synthetic turn or branch identity", () => {
  const event = mapEntry(assistantEntry("b1", "a0b1c2d3", "the policy is 30 days"), { sessionId: "s" });
  assert.equal("turn_id" in event, false);
  assert.equal("branch_id" in event, false);
  assert.equal(event.parent_event_id, "b1".slice(0, 0) + "a0b1c2d3");
});

test("textOf keeps text blocks verbatim and ordered, dropping other parts", () => {
  const text = textOf({ content: [{ type: "text", text: "one " }, { type: "thinking", thinking: "hidden" },
    { type: "text", text: "two" }, { type: "image", data: "..." }] });
  assert.equal(text, "one two");
});

// ── 2. exclusion: custom / system / toolResult / partial / error ──────────
test("custom, system, toolResult, thinking-only and error/aborted entries are excluded", () => {
  const base = { sessionId: "s" };
  const excluded = [
    { type: "message", id: "c1", parentId: null, timestamp: "t", message: { role: "custom", content: "adapter output" } },
    { type: "message", id: "c2", parentId: null, timestamp: "t", message: { role: "system", content: "" } },
    { type: "message", id: "c3", parentId: null, timestamp: "t", message: { role: "toolResult", content: [{ type: "text", text: "out" }] } },
    { type: "message", id: "c4", parentId: null, timestamp: "t", message: { role: "assistant", content: [{ type: "thinking", thinking: "hmm" }] } },
    { type: "message", id: "c5", parentId: null, timestamp: "t", message: { role: "assistant", content: [{ type: "text", text: "partial" }], stopReason: "aborted" } },
    { type: "message", id: "c6", parentId: null, timestamp: "t", message: { role: "assistant", content: [{ type: "text", text: "boom" }], stopReason: "error" } },
    { type: "compaction", id: "c7", parentId: null, timestamp: "t", summary: "summary" },
  ];
  for (const entry of excluded) assert.equal(mapEntry(entry, base), null, entry.id);
  assert.equal(mapEntry({ type: "thinking_level_change", id: "c8", parentId: null, timestamp: "t" }, base), null);
});

test("only text-only completed user/assistant entries are sent", async () => {
  const entries = [
    userEntry("u1", null, "question"),
    { type: "message", id: "t1", parentId: "u1", timestamp: "t", message: { role: "toolResult", content: [{ type: "text", text: "tool output" }] } },
    assistantEntry("a1", "t1", "answer"),
  ];
  const { adapter, fetchImpl } = makeAdapter({ "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL, capabilities: { events: "v1" } } }, "/events": OK });
  await adapter.drain(ctxFor(entries));
  const sent = fetchImpl.calls.filter((c) => c.path === "/events").map((c) => c.body);
  assert.deepEqual(sent.map((e) => e.event_id), ["u1", "a1"]);
  assert.deepEqual(sent.map((e) => e.content), ["question", "answer"]);
});

// ── 3. dedupe: same event twice, and a new SDK session ───────────────────
test("repeated drains send deltas only; the same entry id in a new session is distinct", async () => {
  const routes = { "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } }, "/events": OK };
  const { adapter, fetchImpl } = makeAdapter(routes);
  const entries = [userEntry("u1", null, "one"), assistantEntry("a1", "u1", "two")];
  await adapter.drain(ctxFor(entries, { sessionId: "sess-A" }));
  await adapter.drain(ctxFor(entries, { sessionId: "sess-A" }));
  const afterReplay = fetchImpl.calls.filter((c) => c.path === "/events").length;
  assert.equal(afterReplay, 2);

  // Same native entry id, different SDK session -> different key, sent again.
  await adapter.drain(ctxFor(entries, { sessionId: "sess-B" }));
  const bodies = fetchImpl.calls.filter((c) => c.path === "/events").map((c) => c.body);
  assert.equal(bodies.length, 4);
  assert.deepEqual(bodies.map((e) => e.session_id), ["sess-A", "sess-A", "sess-B", "sess-B"]);
  assert.deepEqual(bodies.map((e) => e.event_id), ["u1", "a1", "u1", "a1"]);
});

test("a replayed entry that the backend answers as duplicate counts as seen", async () => {
  let n = 0;
  const { adapter, fetchImpl } = makeAdapter({
    "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } },
    "/events": () => { n += 1; return { payload: { ok: true, status: n === 1 ? "accepted" : "duplicate" } }; },
  });
  const entries = [userEntry("u1", null, "one")];
  await adapter.drain(ctxFor(entries));
  assert.equal(adapter.seen.has("sess-1::u1"), true);
  await adapter.drain(ctxFor(entries));
  assert.equal(fetchImpl.calls.filter((c) => c.path === "/events").length, 1);
});

// ── 4. failure: retry + order ───────────────────────────────────────────
test("a failed event stays retryable and stops the drain so the answer cannot pass it", async () => {
  let userAttempts = 0;
  const { adapter, fetchImpl } = makeAdapter({
    "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } },
    "/events": (body) => {
      if (body.event_id !== "u1") return { payload: { ok: true, status: "accepted" } };
      userAttempts += 1;
      return userAttempts === 1
        ? { status: 503, payload: { ok: false, status: "retryable" } }
        : { payload: { ok: true, status: "accepted" } };
    },
  });
  const entries = [userEntry("u1", null, "q"), assistantEntry("a1", "u1", "a")];
  await adapter.drain(ctxFor(entries));
  const ids = fetchImpl.calls.filter((c) => c.path === "/events").map((c) => c.body.event_id);
  assert.deepEqual(ids, ["u1"], "assistant must not be sent past a failed user event");
  assert.equal(adapter.seen.has("sess-1::u1"), false);

  await adapter.drain(ctxFor(entries));   // retry now succeeds
  const retried = fetchImpl.calls.filter((c) => c.path === "/events").map((c) => c.body.event_id);
  assert.deepEqual(retried, ["u1", "u1", "a1"]);
  assert.equal(adapter.seen.has("sess-1::a1"), true);
});

// ── 5. recall latch + additive memory message ────────────────────────────
test("one input latches exactly one prefetch even if before_agent_start runs twice", async () => {
  const { adapter, fetchImpl } = makeAdapter({
    "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } },
    "/prefetch": { payload: { ok: true, block: "MEMORY-BLOCK" } },
  });
  adapter.recallArmed = true;
  const event = { prompt: "what did we decide about retention?" };
  const first = await adapter.onBeforeAgentStart(event, ctxFor([]));
  const second = await adapter.onBeforeAgentStart(event, ctxFor([]));
  assert.equal(fetchImpl.calls.filter((c) => c.path === "/prefetch").length, 1);
  assert.ok(first.message.content.includes("MEMORY-BLOCK"));
  assert.equal(first.message.display, false);
  assert.equal(first.message.customType, "hippocampus-memory");
  assert.equal(second, undefined);
  assert.equal("systemPrompt" in first, false, "host system prompt is never replaced");
});

test("the injected message carries the core source references and no invented ids", async () => {
  const block = "## 召回\n[source v3_m1] retention policy\n";
  const { adapter, fetchImpl } = makeAdapter({
    "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } },
    "/prefetch": { payload: { ok: true, block } },
  });
  adapter.recallArmed = true;
  const result = await adapter.onBeforeAgentStart({ prompt: "retention?" }, ctxFor([], { sessionId: "sess-9" }));
  assert.ok(result.message.content.includes("v3_m1"), "source ids are preserved");
  assert.equal(fetchImpl.calls[1].body.session_id, "sess-9");
  assert.equal(fetchImpl.calls[1].body.query, "retention?");
});

test("an oversize block is refused whole and never truncated", async () => {
  const { adapter, warnings } = makeAdapter({
    "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } },
    "/prefetch": { payload: { ok: true, block: "X".repeat(50) } },
  }, { config: { ...config, memoryBudgetChars: 20 } });
  adapter.recallArmed = true;
  const result = await adapter.onBeforeAgentStart({ prompt: "hi" }, ctxFor([]));
  assert.equal(result, undefined);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /exceeds budget/);
});

test("an empty block produces no custom message", async () => {
  const { adapter } = makeAdapter({
    "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } },
    "/prefetch": { payload: { ok: true, block: "   " } },
  });
  adapter.recallArmed = true;
  assert.equal(await adapter.onBeforeAgentStart({ prompt: "hi" }, ctxFor([])), undefined);
});

// ── 6. fail-open and lifecycle ───────────────────────────────────────────
test("an unreachable backend never breaks pi and never claims to remember", async () => {
  const { adapter, warnings } = makeAdapter(new Error("ECONNREFUSED"));
  const entries = [userEntry("u1", null, "q")];
  await adapter.drain(ctxFor(entries));
  adapter.recallArmed = true;
  assert.equal(await adapter.onBeforeAgentStart({ prompt: "q" }, ctxFor(entries)), undefined);
  assert.ok(adapter.seen.size === 0);
  assert.ok(warnings.length > 0);
  assert.equal(adapter.status().startsWith("unavailable"), true);
});

test("a protocol mismatch is treated as unavailable, not tolerated", async () => {
  const { adapter } = makeAdapter({ "/health": { payload: { ok: true, bridge_protocol_version: "b02.0" } } });
  await adapter.drain(ctxFor([userEntry("u1", null, "q")]));
  assert.equal(adapter.seen.size, 0);
  assert.equal(adapter.status().includes("protocol-mismatch"), true);
});

test("missing or invalid configuration is unavailable, never a crash", () => {
  const dir = mkdtempSync(join(tmpdir(), "hippocampus-cfg-"));
  const missing = loadConfig({ env: { HIPPOCAMPUS_PI_CONFIG: join(dir, "nope.json") }, cwd: dir });
  assert.equal(missing.ok, false);
  assert.equal(missing.reason, "unreadable");
  assert.equal(configPathFor({ env: {}, cwd: "/w" }), join("/w", ".pi", "hippocampus.json"));

  const bad = join(dir, "bad.json");
  writeFileSync(bad, "{not json");
  assert.equal(loadConfig({ env: { HIPPOCAMPUS_PI_CONFIG: bad }, cwd: dir }).reason, "invalid-json");

  const off = join(dir, "off.json");
  writeFileSync(off, JSON.stringify({ enabled: false }));
  assert.equal(loadConfig({ env: { HIPPOCAMPUS_PI_CONFIG: off }, cwd: dir }).reason, "disabled");

  const relative = join(dir, "rel.json");
  writeFileSync(relative, JSON.stringify({ enabled: true, mode: "owned", pythonExecutable: "python",
    coreConfig: "cfg.yaml", profile: "default" }));
  assert.equal(loadConfig({ env: { HIPPOCAMPUS_PI_CONFIG: relative }, cwd: dir }).reason, "invalid-config");
});

test("session off stops sends and on resumes them without a config change", async () => {
  const { adapter, fetchImpl } = makeAdapter({
    "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } }, "/events": OK,
  });
  const entries = [userEntry("u1", null, "q")];
  await adapter.drain(ctxFor(entries));
  adapter.sessionEnabled = false;
  await adapter.drain(ctxFor([...entries, assistantEntry("a1", "u1", "a")]));
  assert.equal(adapter.status(), "disabled (session off)");
  assert.equal(fetchImpl.calls.filter((c) => c.path === "/events").length, 1);
  adapter.sessionEnabled = true;
  await adapter.drain(ctxFor([...entries, assistantEntry("a1", "u1", "a")]));
  assert.deepEqual(fetchImpl.calls.filter((c) => c.path === "/events").map((c) => c.body.event_id), ["u1", "a1"]);
});

test("external mode shutdown drains and never kills a borrowed backend", async () => {
  const { adapter } = makeAdapter({
    "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } }, "/events": OK,
  });
  await adapter.drain(ctxFor([userEntry("u1", null, "q")]));
  await adapter.shutdown();
  assert.equal(adapter.owned, null, "no child process is ever created for an external backend");
  assert.equal(adapter.status(), "closed");
  await adapter.drain(ctxFor([assistantEntry("a1", "u1", "a")]));
});

test("source read preserves the result and reports an explicit miss", async () => {
  const { adapter } = makeAdapter({
    "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } },
    "/tool": (body) => (body.args.source_id === "v3_missing"
      ? { payload: { ok: true, result: JSON.stringify({ success: false, error: "not found" }) } }
      : { payload: { ok: true, result: "original text of v3_1" } }),
  });
  const hit = await adapter.readSource("v3_1");
  assert.deepEqual(hit, { ok: true, found: true, text: "original text of v3_1" });
  const miss = await adapter.readSource("v3_missing");
  assert.equal(miss.ok, true);
  assert.equal(miss.found, false);
});

// ── 7. owned lifecycle: readiness, cleanup, early exit, timeout ───────────
// Mirrors a real node ChildProcess: kill() delivers the signal and the child
// then exits on its own (exitCode + "exit" event), optionally late. `stubborn`
// is the pathological child that accepts the signal and never exits.
function fakeChild({ ready, exitEarly = false, exitDelayMs = 0, stubborn = false } = {}) {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.killed = false;
  child.exitCode = null;
  const emitExit = (code) => {
    if (child.exitCode !== null) return;
    child.exitCode = code;
    child.emit("exit", code, null);
  };
  child.kill = () => {
    if (child.exitCode !== null) return false;
    child.killed = true;
    if (!stubborn) setTimeout(() => emitExit(0), exitDelayMs);
    return true;
  };
  if (exitEarly) setImmediate(() => emitExit(1));
  else if (ready) setImmediate(() => child.stdout.emit("data", `${JSON.stringify(ready)}\n`));
  return child;
}

test("owned backend starts lazily, uses the real ready port and closes only its own child", async () => {
  const spawned = [];
  const child = fakeChild({ ready: { event: "ready", service: "v3core-serve", host: "127.0.0.1",
    port: 51789, pid: 4242, profile: "test", bridge_protocol_version: REQUIRED_PROTOCOL } });
  const owned = new OwnedBackend({ pythonExecutable: "/abs/python", coreConfig: "/abs/cfg.yaml", profile: "test",
    spawnImpl: (exe, args, opts) => { spawned.push({ exe, args, opts }); return child; } });
  assert.equal(spawned.length, 0, "no process is started before start() is called");
  const baseUrl = await owned.start();
  assert.equal(baseUrl, "http://127.0.0.1:51789");
  assert.deepEqual(spawned[0].args, ["-u", "-m", "v3core", "serve", "--host", "127.0.0.1", "--port", "0",
    "--profile", "test", "--ready-json"]);
  assert.equal(spawned[0].opts.env.V3CORE_CONFIG, "/abs/cfg.yaml");
  assert.equal(spawned[0].opts.stdio[0], "ignore");
  await owned.close();
  assert.equal(child.killed, true);
  assert.equal(owned.child, null);
});

test("an owned backend that exits early or times out is cleaned up and reported unavailable", async () => {
  for (const kind of ["exit", "timeout"]) {
    const child = fakeChild(kind === "exit" ? { exitEarly: true } : {});
    const owned = new OwnedBackend({ pythonExecutable: "/abs/python", coreConfig: "/abs/cfg.yaml",
      profile: "test", readyTimeoutMs: 30, spawnImpl: () => child });
    assert.equal(await owned.start(), null);
    // A child that already exited needs no signal; one that is still alive at
    // the readiness deadline is released. Both must end with no child held.
    assert.equal(child.exitCode !== null || child.killed === true, true, `${kind}: child released`);
    assert.equal(owned.child, null);
  }
  const owned = new OwnedBackend({ pythonExecutable: "/abs/python", coreConfig: "/abs/cfg.yaml",
    profile: "test", readyTimeoutMs: 30, spawnImpl: () => { throw new Error("spawn ENOENT"); } });
  assert.equal(await owned.start(), null);
  assert.equal(owned.child, null);
});

test("owned mode adapter reports a generic warning when the backend never becomes ready", async () => {
  const warnings = [];
  const adapter = new MemoryAdapter({ config: { mode: "owned", pythonExecutable: "/abs/python",
    coreConfig: "/abs/cfg.yaml", profile: "test", timeoutMs: 5000, memoryBudgetChars: 8000 },
    readyTimeoutMs: 30, spawnImpl: () => fakeChild({}), warn: (m) => warnings.push(m) });
  await adapter.drain(ctxFor([userEntry("u1", null, "q")]));
  assert.equal(adapter.seen.size, 0);
  assert.equal(warnings.length, 1);
  assert.doesNotMatch(warnings[0], /\/abs\/|Traceback|http:\/\//, "no paths or endpoints in warnings");
});

test("BridgeClient sends flat events and a query+session prefetch body", async () => {
  const fetchImpl = fakeFetch({ "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } },
    "/events": { payload: { ok: true, status: "accepted" } },
    "/prefetch": { payload: { ok: true, block: "b" } } });
  const client = new BridgeClient({ baseUrl: "http://127.0.0.1:1234", fetchImpl });
  const event = { host: "pi", session_id: "s", event_id: "e1", role: "user", content: "c",
    timestamp: "2026-10-02T01:00:00.000Z", parent_event_id: "p1", project_id: "/proj" };
  assert.deepEqual(await client.sendEvent(event), { ok: true, status: "accepted", sourceId: undefined });
  assert.deepEqual(fetchImpl.calls[0].body, event);
  assert.equal("events" in fetchImpl.calls[0].body, false, "no nested envelope");
  await client.prefetch("why?", "sess-2");
  assert.deepEqual(fetchImpl.calls[1].body, { query: "why?", session_id: "sess-2" });
});

// ── 8. the extension factory itself ──────────────────────────────────────
// Guards the module graph of the built entry point: a missing export here is
// exactly the failure that breaks a real pi load.
test("the extension factory registers its hooks, command and stays inert at load", () => {
  const dir = mkdtempSync(join(tmpdir(), "hippocampus-ext-"));
  const events = {};
  const commands = {};
  const sent = [];
  const pi = {
    on: (name, handler) => { events[name] = handler; return () => { delete events[name]; }; },
    registerCommand: (name, options) => { commands[name] = options; },
    sendMessage: (message) => sent.push(message),
  };
  const previous = process.env.HIPPOCAMPUS_PI_CONFIG;
  process.env.HIPPOCAMPUS_PI_CONFIG = join(dir, "absent.json");
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (m) => warnings.push(m);
  try {
    hippocampusExtension(pi);      // no throw, no process, no fetch at load time
  } finally {
    console.warn = originalWarn;
    if (previous === undefined) delete process.env.HIPPOCAMPUS_PI_CONFIG;
    else process.env.HIPPOCAMPUS_PI_CONFIG = previous;
  }
  for (const hook of ["input", "message_end", "before_agent_start", "agent_end", "session_start",
    "session_before_switch", "session_before_fork", "session_shutdown"]) {
    assert.equal(typeof events[hook], "function", `hook ${hook} registered`);
  }
  assert.equal(typeof commands.hippocampus.handler, "function");
  assert.equal(MEMORY_CUSTOM_TYPE, "hippocampus-memory");
  assert.equal(sent.length, 0);
  assert.match(warnings.join(" "), /configuration not usable \(unreadable\)/);
});

// The exact event-name subset the installed pi 0.99.2 SDK types
// (dist/core/extensions/types.d.ts:1143-1184) accepts. Anything outside it —
// notably `session_switch` / `session_fork`, which this SDK never typed — is a
// contract violation, so the fake host refuses to register it at all.
const SUPPORTED_EVENTS = new Set(["input", "message_end", "before_agent_start", "agent_end",
  "session_start", "session_before_switch", "session_before_fork", "session_shutdown"]);

function strictHost() {
  const events = {};
  const commands = {};
  const sent = [];
  const rejected = [];
  const pi = {
    on: (name, handler) => {
      if (!SUPPORTED_EVENTS.has(name)) {
        rejected.push(name);
        throw new Error(`unsupported pi event ${JSON.stringify(name)}`);
      }
      events[name] = handler;
      return () => { delete events[name]; };
    },
    registerCommand: (name, options) => { commands[name] = options; },
    sendMessage: (message) => sent.push(message),
  };
  return { pi, events, commands, sent, rejected };
}

test("the factory registers only event names the installed SDK types support", () => {
  const dir = mkdtempSync(join(tmpdir(), "hippocampus-events-"));
  const { pi, events, rejected } = strictHost();
  const previous = process.env.HIPPOCAMPUS_PI_CONFIG;
  process.env.HIPPOCAMPUS_PI_CONFIG = join(dir, "absent.json");
  const originalWarn = console.warn;
  console.warn = () => {};
  try {
    hippocampusExtension(pi);
  } finally {
    console.warn = originalWarn;
    if (previous === undefined) delete process.env.HIPPOCAMPUS_PI_CONFIG;
    else process.env.HIPPOCAMPUS_PI_CONFIG = previous;
  }
  assert.deepEqual(rejected, [], "no registration was refused by the strict host");
  assert.deepEqual([...Object.keys(events)].sort(), [...SUPPORTED_EVENTS].sort());
  assert.equal("session_switch" in events, false);
  assert.equal("session_fork" in events, false);
});

test("session_start on a replacement session drains that session's own persisted entries", async () => {
  const dir = mkdtempSync(join(tmpdir(), "hippocampus-start-"));
  const configPath = join(dir, "hippocampus.json");
  writeFileSync(configPath, JSON.stringify({ enabled: true, mode: "external",
    bridgeUrl: "http://127.0.0.1:9", timeoutMs: 50 }));
  const { pi, events, rejected } = strictHost();
  const previous = process.env.HIPPOCAMPUS_PI_CONFIG;
  process.env.HIPPOCAMPUS_PI_CONFIG = configPath;
  const originalFetch = globalThis.fetch;
  const sentIds = [];
  try {
    globalThis.fetch = async (url, init = {}) => {
      const path = String(url).replace(/^https?:\/\/[^/]+/, "");
      if (path === "/events") sentIds.push([JSON.parse(init.body).session_id, JSON.parse(init.body).event_id]);
      return { status: 200, json: async () => path === "/health"
        ? { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL }
        : { ok: true, status: "accepted" } };
    };
    hippocampusExtension(pi);
    assert.deepEqual(rejected, [], "session_start is a supported name");
    // reason "new"/"resume"/"fork": the ctx is the REPLACING session, and its
    // already-persisted entries must be drained under the NEW session id.
    const ctx = ctxFor([userEntry("n-u1", null, "asked in the new session")], { sessionId: "sess-NEW" });
    await events.session_start({ type: "session_start", reason: "new", previousSessionFile: "/s/old.json" }, ctx);
    assert.deepEqual(sentIds, [["sess-NEW", "n-u1"]],
      "the replacement session drains its own entries under its own session id");
  } finally {
    globalThis.fetch = originalFetch;
    if (previous === undefined) delete process.env.HIPPOCAMPUS_PI_CONFIG;
    else process.env.HIPPOCAMPUS_PI_CONFIG = previous;
  }
});

test("the command reports status honestly and refuses a bare source id", async () => {
  const dir = mkdtempSync(join(tmpdir(), "hippocampus-cmd-"));
  const configPath = join(dir, "hippocampus.json");
  writeFileSync(configPath, JSON.stringify({ enabled: true, mode: "external",
    bridgeUrl: "http://127.0.0.1:9", timeoutMs: 50 }));
  const events = {};
  const commands = {};
  const notes = [];
  const pi = { on: (n, h) => { events[n] = h; }, registerCommand: (n, o) => { commands[n] = o; },
    sendMessage: () => {} };
  const previous = process.env.HIPPOCAMPUS_PI_CONFIG;
  process.env.HIPPOCAMPUS_PI_CONFIG = configPath;
  try {
    hippocampusExtension(pi);
    const ctx = { ui: { notify: (m, t) => notes.push([m, t]) } };
    await commands.hippocampus.handler("status", ctx);
    assert.match(notes[0][0], /^hippocampus: configured \(not connected yet\)$/);
    await commands.hippocampus.handler("source", ctx);
    assert.match(notes[1][0], /source needs an exact id/);
    await commands.hippocampus.handler("nonsense", ctx);
    assert.match(notes[2][0], /unknown argument/);
  } finally {
    if (previous === undefined) delete process.env.HIPPOCAMPUS_PI_CONFIG;
    else process.env.HIPPOCAMPUS_PI_CONFIG = previous;
  }
});

// ═══════════════════════════════════════════════════════════════════════════
// 9. PARENT-PRESCRIBED BLOCKER REGRESSIONS (B03 acceptance corrections).
// Each test pins exactly one numbered blocker from the fix spec.
// ═══════════════════════════════════════════════════════════════════════════

// ── BLOCKER 1: the owned child binds an ephemeral port; that ACTUAL URL must
//    reach BridgeClient. User config is never rewritten to conceal the gap.
test("BLOCKER1 owned adapter talks to the bound-port URL the child actually reports", async () => {
  const fetchImpl = fakeFetch({
    "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } },
    "/events": OK,
  });
  const warnings = [];
  const userConfig = { mode: "owned", pythonExecutable: "/abs/python", coreConfig: "/abs/cfg.yaml",
    profile: "test", timeoutMs: 5000, memoryBudgetChars: 8000, bridgeUrl: "http://127.0.0.1:9" };
  const child = fakeChild({ ready: { event: "ready", service: "v3core-serve", host: "127.0.0.1",
    port: 51234, pid: 4242, profile: "test", bridge_protocol_version: REQUIRED_PROTOCOL } });
  const adapter = new MemoryAdapter({ config: userConfig, fetchImpl, warn: (m) => warnings.push(m),
    spawnImpl: () => child });

  await adapter.drain(ctxFor([userEntry("u1", null, "q")]));

  assert.ok(fetchImpl.calls.length >= 2, "the owned adapter performed a handshake and a send");
  assert.deepEqual([...new Set(fetchImpl.calls.map((c) => c.host))], ["127.0.0.1:51234"],
    "every owned call goes to the port the child actually bound");
  const paths = fetchImpl.calls.map((c) => c.path);
  assert.ok(paths.includes("/health"));
  assert.ok(paths.includes("/events"));
  assert.equal(adapter.config.bridgeUrl, "http://127.0.0.1:9",
    "user config is never mutated to hide the owned endpoint");
  assert.equal(warnings.length, 0);
});

// ── BLOCKER 2: shutdown(ctx) must drain entries that exist ONLY at shutdown,
//    and the SDK ctx must actually reach the adapter through the hook.
test("BLOCKER2 session_shutdown awaits a drain of the final persisted entry before cleanup", async () => {
  const dir = mkdtempSync(join(tmpdir(), "hippocampus-shutdown-"));
  const configPath = join(dir, "hippocampus.json");
  writeFileSync(configPath, JSON.stringify({ enabled: true, mode: "external",
    bridgeUrl: "http://127.0.0.1:9", timeoutMs: 50 }));
  const events = {};
  const commands = {};
  const sentIds = [];
  const pi = { on: (n, h) => { events[n] = h; }, registerCommand: (n, o) => { commands[n] = o; },
    sendMessage: () => {} };
  const previous = process.env.HIPPOCAMPUS_PI_CONFIG;
  process.env.HIPPOCAMPUS_PI_CONFIG = configPath;
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async (url, init = {}) => {
      const path = String(url).replace(/^https?:\/\/[^/]+/, "");
      if (path === "/events") sentIds.push(JSON.parse(init.body).event_id);
      return { status: 200, json: async () => path === "/health"
        ? { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL }
        : { ok: true, status: "accepted" } };
    };
    hippocampusExtension(pi);
    for (const hook of ["agent_end", "session_start", "session_before_switch", "session_before_fork"]) {
      assert.equal(typeof events[hook], "function", `hook ${hook} registered`);
    }
    // No prior drain anywhere: this entry exists only when shutdown runs.
    const ctx = ctxFor([userEntry("final-1", null, "last words")]);
    const ret = events.session_shutdown({}, ctx);
    assert.ok(ret && typeof ret.then === "function", "session_shutdown returns the drain promise");
    await ret;
    assert.deepEqual(sentIds, ["final-1"], "the final persisted entry is sent before cleanup");
  } finally {
    globalThis.fetch = originalFetch;
    if (previous === undefined) delete process.env.HIPPOCAMPUS_PI_CONFIG;
    else process.env.HIPPOCAMPUS_PI_CONFIG = previous;
  }
});

// ── BLOCKER 3: the SDK sessionManager is a LIVE object. A drain must snapshot
//    session id + entries synchronously, before any await, or a session switch
//    that lands mid-drain attributes the old entries to the new session.
test("BLOCKER3 a drain snapshots the live session before any asynchronous wait", async () => {
  let current = { id: "sess-A", entries: [userEntry("a-u1", null, "asked in A")] };
  const liveCtx = {
    cwd: "/proj",
    ui: { notify() {} },
    sessionManager: { getSessionId: () => current.id, getEntries: () => current.entries },
  };
  let healthCalls = 0;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const sent = [];
  const fetchImpl = async (url, init = {}) => {
    const path = String(url).replace(/^https?:\/\/[^/]+/, "");
    if (path === "/health") {
      healthCalls += 1;
      if (healthCalls === 1) await gate;      // readiness is slow: A is switched away here
      return { status: 200, json: async () => ({ ok: true, bridge_protocol_version: REQUIRED_PROTOCOL }) };
    }
    if (path === "/events") sent.push(JSON.parse(init.body));
    return { status: 200, json: async () => ({ ok: true, status: "accepted" }) };
  };
  const adapter = new MemoryAdapter({ config, fetchImpl, warn: () => {} });

  const draining = adapter.drain(liveCtx);
  current = { id: "sess-B", entries: [userEntry("b-u1", null, "asked in B")] };  // switch mid-drain
  release();
  await draining;
  await adapter.drain(liveCtx);

  assert.deepEqual(sent.map((e) => e.event_id), ["a-u1", "b-u1"]);
  assert.equal(sent[0].session_id, "sess-A", "A's entries keep A's session, not the switched-to B");
  assert.equal(sent[1].session_id, "sess-B");
});

test("BLOCKER3b the before-agent hook flushes events before it prefetches", async () => {
  const order = [];
  const fetchImpl = async (url, init = {}) => {
    const path = String(url).replace(/^https?:\/\/[^/]+/, "");
    if (path === "/events" || path === "/prefetch") order.push(path);
    return { status: 200, json: async () => path === "/health"
      ? { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL }
      : path === "/prefetch"
        ? { ok: true, block: "MEMORY-BLOCK" }
        : { ok: true, status: "accepted" } };
  };
  const adapter = new MemoryAdapter({ config, fetchImpl, warn: () => {} });
  // A persisted previous message that was never acknowledged (crash between turns).
  const ctx = ctxFor([userEntry("prev-1", null, "asked earlier")]);
  adapter.recallArmed = true;

  const first = await adapter.onBeforeAgentStart({ prompt: "and now?" }, ctx);
  const second = await adapter.onBeforeAgentStart({ prompt: "and now?" }, ctx);   // duplicate hook
  assert.deepEqual(order, ["/events", "/prefetch"], "events are flushed before the recall is issued");
  assert.equal(order.filter((p) => p === "/prefetch").length, 1, "duplicate hook still one prefetch");
  assert.ok(first.message.content.includes("MEMORY-BLOCK"));
  assert.equal(second, undefined);
});

// ── BLOCKER 4: a source command must be visible to the user; a failed prefetch
//    must warn generically, keep pi running, and leak no internals.
test("BLOCKER4 the source command message is visible and automatic recall stays hidden", async () => {
  const dir = mkdtempSync(join(tmpdir(), "hippocampus-src-"));
  const configPath = join(dir, "hippocampus.json");
  writeFileSync(configPath, JSON.stringify({ enabled: true, mode: "external",
    bridgeUrl: "http://127.0.0.1:9", timeoutMs: 50 }));
  const commands = {};
  const sent = [];
  const pi = { on: () => {}, registerCommand: (n, o) => { commands[n] = o; },
    sendMessage: (m) => sent.push(m) };
  const previous = process.env.HIPPOCAMPUS_PI_CONFIG;
  process.env.HIPPOCAMPUS_PI_CONFIG = configPath;
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({ status: 200, json: async () => ({ ok: true,
      bridge_protocol_version: REQUIRED_PROTOCOL, result: "original text of v3_1" }) });
    hippocampusExtension(pi);
    await commands.hippocampus.handler("source v3_1", { ui: { notify() {} } });
    assert.equal(sent.length, 1);
    assert.equal(sent[0].display, true, "the traced original must be visible to the user");
    assert.equal(sent[0].customType, MEMORY_CUSTOM_TYPE, "it stays a custom, never-recorded message");
    assert.equal(sent[0].details.origin, "source");
  } finally {
    globalThis.fetch = originalFetch;
    if (previous === undefined) delete process.env.HIPPOCAMPUS_PI_CONFIG;
    else process.env.HIPPOCAMPUS_PI_CONFIG = previous;
  }

  // Automatic recall is unchanged: injected silently.
  const { adapter } = makeAdapter({
    "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } },
    "/prefetch": { payload: { ok: true, block: "B" } },
  });
  adapter.recallArmed = true;
  const recall = await adapter.onBeforeAgentStart({ prompt: "hi" }, ctxFor([]));
  assert.equal(recall.message.display, false);
});

test("BLOCKER4b a prefetch that fails after a good handshake warns once and yields no context", async () => {
  const { adapter, warnings } = makeAdapter({
    "/health": { payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL } },
    "/prefetch": new Error("ECONNRESET at http://127.0.0.1:51234/prefetch /home/u/secret/path"),
  });
  adapter.recallArmed = true;
  const result = await adapter.onBeforeAgentStart({ prompt: "hi" }, ctxFor([]));
  assert.equal(result, undefined, "no context is injected when the recall fails");
  assert.equal(adapter.status().startsWith("active"), true, "pi keeps running on a healthy session");
  const recallWarnings = warnings.filter((w) => /prefetch|recall/i.test(w));
  assert.equal(recallWarnings.length, 1, "exactly one generic warning for the failed recall");
  for (const w of warnings) {
    assert.doesNotMatch(w, /ECONNRESET|127\.0\.0\.1|\/home\/|secret|Traceback/,
      "no raw error, endpoint or path is surfaced");
  }
});

// ── BLOCKER 5: close() must not claim release before the child actually exits.
test("BLOCKER5 close waits for the child real exit, not merely for the signal", async () => {
  const child = fakeChild({ ready: { event: "ready", host: "127.0.0.1", port: 51789 }, exitDelayMs: 40 });
  const owned = new OwnedBackend({ pythonExecutable: "/abs/python", coreConfig: "/abs/cfg.yaml",
    profile: "test", spawnImpl: () => child });
  assert.equal(await owned.start(), "http://127.0.0.1:51789");
  let finished = false;
  const closing = owned.close().then((result) => { finished = true; return result; });
  await new Promise((r) => setTimeout(r, 15));
  assert.equal(child.killed, true, "the signal was sent");
  assert.equal(finished, false, "close must not settle before the child exits");
  const result = await closing;
  assert.equal(finished, true);
  assert.equal(result.ok, true, "a child that exited is released");
  assert.equal(child.exitCode, 0);
});

test("BLOCKER5b a child that ignores the signal times out instead of hanging forever", async () => {
  const child = fakeChild({ ready: { event: "ready", host: "127.0.0.1", port: 51790 }, stubborn: true });
  const owned = new OwnedBackend({ pythonExecutable: "/abs/python", coreConfig: "/abs/cfg.yaml",
    profile: "test", closeTimeoutMs: 60, spawnImpl: () => child });
  assert.equal(await owned.start(), "http://127.0.0.1:51790");
  const started = Date.now();
  const result = await owned.close();
  const elapsed = Date.now() - started;
  assert.ok(elapsed < 3000, `close is bounded (took ${elapsed}ms)`);
  assert.equal(result.ok, false, "a still-alive child is reported as NOT released");
  assert.equal(child.exitCode, null);
});
