// UNIT tests only (§3). The DSH host is a fake `ctx` that merely captures the
// registered handlers, and the bridge is a fake `fetchImpl` running
// in-process. No real DSH process, no socket, no config file from the user's
// home, no database and no model provider is involved anywhere in this file.
import { test } from "node:test";
import assert from "node:assert/strict";

import * as plugin from "../src/index.js";
import { createMemoryRuntime } from "../src/index.js";
import { loadConfig, configPathFor, normalizeConfig } from "../src/config.js";
import { REQUIRED_PROTOCOL, HOST_ID } from "../src/bridge.js";
import { join as joinPath } from "node:path";

const { name, apply } = plugin;

const BLOCK = "topic: retention policy\n- 30 days (source: sess-0)";
const REFERENCES = [{ sourceId: "qa_sync/s0/t1/m1", kind: "qa", label: "retention policy" }];

const T0 = 1_700_000_000_000;

const HEALTH = { status: 200, payload: { ok: true, bridge_protocol_version: REQUIRED_PROTOCOL,
  capabilities: { events: "v1", prefetch: "v1" } } };
const ACCEPTED = { status: 200, payload: { ok: true, status: "accepted", host: HOST_ID } };

// ── fixtures ──────────────────────────────────────────────────────────────
function fakeFetch(routes) {
  const calls = [];
  const impl = async (url, init = {}) => {
    const full = String(url);
    const path = full.replace(/^https?:\/\/[^/]+/, "");
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ url: full, path, method: init.method || "GET", body });
    const route = routes[path];
    if (!route) return { status: 404, json: async () => ({ ok: false, error: "not found" }) };
    const reply = typeof route === "function" ? route(body, calls.length) : route;
    if (reply instanceof Error) throw reply;
    return { status: reply.status || 200, json: async () => reply.payload };
  };
  impl.calls = calls;
  return impl;
}

// One sink for every warning, whoever raises it: `apply()` warns through the
// host logger, a directly constructed runtime warns through the injected
// `warn`. Both land in this array, so a test can assert on the total.
function fakeCtx(sink = []) {
  const handlers = new Map();
  const logger = {
    info() {}, debug() {},
    warn(m) { sink.push(String(m)); },
    error(m) { sink.push(String(m)); },
    warnings: sink,
  };
  return {
    handlers, logger,
    on(event, handler) { handlers.set(event, handler); return () => handlers.delete(event); },
  };
}

// The DSH overlay row as it appears in a profile's cordis.patch.yml — the
// first-priority configuration source, in its RAW (un-normalized) form.
const OVERLAY = { enabled: true, mode: "external", bridgeUrl: "http://127.0.0.1:9",
  timeoutMs: 500, memoryBudgetChars: 8000, capture: true, recall: true };
// The same thing after normalization: what the runtime actually consumes. Both
// sources must produce exactly this shape.
const CONFIG = { mode: "external", bridgeUrl: "http://127.0.0.1:9",
  timeoutMs: 500, memoryBudgetChars: 8000, capture: true, recall: true };

// The runtime is built with an explicit config and an injected fetch, so no
// test ever reads a file from disk or opens a socket. `bind()` calls the real
// `apply(ctx, overlay)` with the file loader injected, so the two-source
// precedence wiring under test is the production one while neither the config
// file nor the real `fetch` is reached.
function runtime(routes, { config = CONFIG, configState = "ok", overlay = OVERLAY, fileConfig = null } = {}) {
  const fetchImpl = fakeFetch(routes);
  const warnings = [];
  const ctx = fakeCtx(warnings);
  const adapter = createMemoryRuntime({ ctx, config: config ?? null, configState, fetchImpl,
    warn: (m) => ctx.logger.warn(m) });
  // `fileConfig === null` means "the loader reports a failure class";
  // otherwise the loader reports this (already normalized) file config.
  const loadConfigImpl = () => (fileConfig
    ? { ok: true, path: "<injected-file>", config: fileConfig }
    : { ok: false, path: "<injected-file>", reason: configState });
  let bound = adapter;
  const bind = (target = ctx) => {
    bound = apply(target, overlay, {
      loadConfigImpl,
      createRuntime: (options) => createMemoryRuntime({ ...options, fetchImpl }),
    });
    return bound;
  };
  return {
    // `adapter` is the standalone runtime; `bound` is the one `apply()` wires
    // to the fake ctx. Both are the same construction, and `bind()` first.
    adapter, bind, fetchImpl, ctx, warnings,
    get runtime() { return bound; },
    onEvent: (session, event) => ctx.handlers.get("session/event")(session, event),
    onPreStep: (payload, next) => ctx.handlers.get("agent/pre-step")(payload, next),
    idle: () => bound.whenIdle(),
  };
}

function userEvent(id, text, { source = { kind: "user" }, time = T0 } = {}) {
  return { type: "user/message", seq: 1, time, data: { id, role: "user", content: text, source } };
}
function assistantEvent(id, text, { turn = 1, time = T0 + 1000 } = {}) {
  return { type: "assistant/message", seq: 2, time,
    data: { turn, step: 1, stream: "chat", message: { id, role: "assistant", content: [{ type: "text", text }] } } };
}
function claimed(text, { kind = "user" } = {}) {
  return [{ id: "claimed-1", role: "user", content: text, source: { kind } }];
}
function enterDecision(messages, extra = {}) {
  return { kind: "enter", messages, startsRequestSeries: true, ...extra };
}
function preStep(sessionId, messages, { turn = 1, step = 1 } = {}) {
  return { sessionId, turn, step, messages, agent: { session: { id: sessionId, header: {} } } };
}

// ── 0. plugin shape (§2.5) ────────────────────────────────────────────────
test("the module exports the DSH plugin shape and registers no import-time side effect", () => {
  assert.equal(name, "hippocampus");
  assert.equal(typeof apply, "function");
  assert.equal(typeof plugin.createMemoryRuntime, "function");
  // `inject` is optional upstream (github-ready-review-rule.mjs declares one);
  // when declared it must be the DSH services these listeners bind to.
  if (plugin.inject !== undefined) assert.ok(Array.isArray(plugin.inject));

  // Importing the module must not have opened a socket or read a config.
  const { ctx, fetchImpl, warnings, bind } = runtime({ "/health": HEALTH, "/events": ACCEPTED });
  bind();
  assert.equal(fetchImpl.calls.length, 0, "no HTTP happens before a host event arrives");
  assert.equal(warnings.length, 0);
  assert.ok(ctx.handlers.get("session/event"));
  assert.ok(ctx.handlers.get("agent/pre-step"));
});

// ── 1. additive decision preserves downstream messages and the series flag ─
test("pre-step recall is additive: downstream messages, order and startsRequestSeries survive", async () => {
  const messages = [{ id: "u1", role: "user", content: "what is the retention policy?", source: { kind: "user" } }];
  const { bind, fetchImpl, onPreStep } = runtime({ "/health": HEALTH,
    "/prefetch": { payload: { ok: true, block: BLOCK, references: REFERENCES } } });
  bind();

  const decision = await onPreStep(preStep("sess-A", messages), async () => enterDecision(messages));

  assert.equal(decision.kind, "enter");
  assert.equal(decision.startsRequestSeries, true, "the downstream series flag is preserved");
  assert.equal(decision.messages.length, 2);
  assert.equal(decision.messages[0], messages[0], "claimed messages are not replaced, reordered or mutated");
  assert.equal(decision.messages[1].role, "user");
  assert.equal(decision.messages[1].source.kind, "hippocampus");
  assert.match(decision.messages[1].content[0].text, /hippocampus memory/);
  assert.ok(!JSON.stringify(messages).includes("hippocampus memory"), "the claimed messages are never mutated");
  assert.equal(fetchImpl.calls.filter((c) => c.path === "/prefetch").length, 1);
});

test("a decision without startsRequestSeries stays without it", async () => {
  const messages = claimed("what is the retention policy?");
  const { bind, onPreStep } = runtime({ "/health": HEALTH,
    "/prefetch": { payload: { ok: true, block: BLOCK, references: REFERENCES } } });
  bind();
  const decision = await onPreStep(preStep("sess-A", messages), async () => ({ kind: "enter", messages }));
  assert.equal("startsRequestSeries" in decision, false, "the flag is not invented");
  assert.equal(decision.messages.length, 2);
});

// ── 2. a rejection is returned unchanged, by identity ────────────────────
test("a downstream rejection is returned unchanged (same object, never rebuilt)", async () => {
  const { bind, onPreStep } = runtime({ "/health": HEALTH,
    "/prefetch": { payload: { ok: true, block: BLOCK, references: REFERENCES } } });
  bind();
  const rejection = { kind: "reject" };
  const decision = await onPreStep(preStep("sess-A", claimed("hello")), async () => rejection);
  assert.equal(decision, rejection, "identity preserved");
});

// ── 3. bridge failure / timeout leaves the host untouched ─────────────────
test("a bridge failure or a rejected prefetch returns the downstream decision unchanged", async () => {
  const cases = [
    { label: "refused", routes: { "/health": HEALTH, "/prefetch": new Error("connection refused") } },
    { label: "http-500", routes: { "/health": HEALTH, "/prefetch": { status: 500, payload: { ok: false, error: "PG down user=secret" } } } },
    { label: "ok-false", routes: { "/health": HEALTH, "/prefetch": { status: 200, payload: { ok: false } } } },
    { label: "no-health", routes: { "/health": { status: 503, payload: { ok: false } }, "/prefetch": { payload: { ok: true, block: BLOCK, references: REFERENCES } } } },
    { label: "protocol-mismatch", routes: { "/health": { payload: { ok: true, bridge_protocol_version: "b09.9" } }, "/prefetch": { payload: { ok: true, block: BLOCK } } } },
  ];
  for (const { label, routes } of cases) {
    const { bind, onPreStep, warnings, ctx } = runtime(routes);
    bind();
    const messages = claimed("what is the retention policy?");
    const decision = await onPreStep(preStep("sess-A", messages), async () => enterDecision(messages));
    assert.equal(decision.messages, messages, `${label}: the exact downstream array comes back`);
    assert.equal(decision.messages.length, 1, `${label}: nothing is injected when the bridge cannot answer`);
    for (const message of [...warnings, ...ctx.logger.warnings]) {
      assert.ok(!/secret|connection refused|PG down|127\.0\.0\.1/.test(message),
        `${label}: no raw server detail, transport error or URL is ever printed: ${message}`);
    }
  }
});

test("a prefetch that never resolves is cut off by the abort timeout", async () => {
  const fetchImpl = async () => new Promise(() => {}); // never settles
  const ctx = fakeCtx();
  const config = { ...CONFIG, timeoutMs: 200 };
  const adapter = createMemoryRuntime({ ctx, config, configState: "ok", fetchImpl,
    warn: (m) => ctx.logger.warn(m) });
  apply(ctx, config, { loadConfigImpl: () => ({ ok: true, config }),
    createRuntime: (options) => createMemoryRuntime({ ...options, fetchImpl }) });
  const messages = claimed("what is the retention policy?");
  const decision = await ctx.handlers.get("agent/pre-step")(preStep("sess-A", messages),
    async () => enterDecision(messages));
  assert.equal(decision.messages, messages, "a hung bridge never blocks the host turn");
  assert.ok(ctx.logger.warnings.length <= 1);
  await adapter.whenIdle();
});

// ── 4. oversized block: no injection, no substring, one warning ───────────
test("an oversized recall block injects nothing, warns once, and is never substring", async () => {
  const { bind, onPreStep, warnings } = runtime({ "/health": HEALTH,
    "/prefetch": { payload: { ok: true, block: "S".repeat(9000), references: REFERENCES } } });
  bind();
  for (const turn of [1, 2]) {
    const messages = claimed("what is the retention policy?");
    const decision = await onPreStep(preStep("sess-A", messages, { turn }), async () => enterDecision(messages));
    assert.equal(decision.messages, messages);
    assert.ok(!JSON.stringify(decision).includes("SSSS"), "no truncated block is ever injected");
  }
  assert.equal(warnings.length, 1, "one warning for the whole class, not one per turn");
});

// ── 5. capture: what is recorded, what is not, retry and ordering ─────────
test("session/event capture records committed user and assistant text only, in order", async () => {
  const { bind, onEvent, fetchImpl, idle } = runtime({ "/health": HEALTH, "/events": ACCEPTED });
  bind();
  const session = { id: "sess-A", header: {} };

  onEvent(session, { type: "turn/start", seq: 0, time: T0, data: { turn: 4 } });
  onEvent(session, userEvent("u1", "what is the retention policy?"));
  onEvent(session, { type: "tool/call", seq: 1, time: T0, data: { id: "c1", name: "read", args: {} } });
  onEvent(session, { type: "tool/result", seq: 2, time: T0, data: { id: "c1", result: "file" } });
  onEvent(session, { type: "assistant/attempt", seq: 3, time: T0, data: { turn: 4, step: 1 } });
  onEvent(session, assistantEvent("a1", "30 days", { turn: 4 }));
  onEvent(session, userEvent("m1", "[hippocampus memory — recalled context, not a new user fact]\n\nblock",
    { source: { kind: "hippocampus", form: "recall", version: 1, references: [] } }));
  onEvent(session, { type: "system/message", seq: 4, time: T0, data: { id: "s1", role: "system", content: "you are" } });
  await idle();

  const sent = fetchImpl.calls.filter((c) => c.path === "/events").map((c) => c.body);
  assert.deepEqual(sent.map((e) => e.event_id), ["u1", "a1"],
    "only the two committed surface messages are recorded");
  assert.deepEqual(sent.map((e) => e.role), ["user", "assistant"]);
  assert.deepEqual(sent.map((e) => e.session_id), ["sess-A", "sess-A"]);
  assert.equal(sent[0].host, "dsh");
  assert.equal(sent[0].content, "what is the retention policy?");
  assert.equal(sent[1].content, "30 days");
  assert.equal(sent[0].turn_id, 4, "turn attribution comes from turn/start");
  assert.equal(sent[1].turn_id, 4);
  assert.ok(!JSON.stringify(sent).includes("hippocampus memory"), "our own injection is never recaptured");
});

test("a failed send does not advance the dedupe set and the next offer retries it", async () => {
  let attempt = 0;
  const { bind, onEvent, fetchImpl, idle, warnings, ctx } = runtime({ "/health": HEALTH, "/events": () => {
    attempt += 1;
    return attempt === 1
      ? { status: 500, payload: { ok: false, status: "retryable", error: "writer rejected host=db" } }
      : ACCEPTED;
  } });
  bind();

  onEvent({ id: "sess-A", header: {} }, userEvent("u1", "first"));
  await idle();
  assert.equal(fetchImpl.calls.filter((c) => c.path === "/events").length, 1, "the rejected event was attempted");
  assert.ok(!/writer rejected|host=db/.test([...warnings, ...ctx.logger.warnings].join("\n")),
    "no raw server error is ever printed");
  assert.ok(warnings.some((w) => /not acknowledged|retry/i.test(w)), "the failure is reported honestly");

  onEvent({ id: "sess-A", header: {} }, userEvent("u1", "first"));
  await idle();
  const afterRetry = fetchImpl.calls.filter((c) => c.path === "/events");
  assert.equal(afterRetry.length, 2, "a failed send is retried, not swallowed");
  assert.equal(afterRetry[1].body.event_id, "u1");

  onEvent({ id: "sess-A", header: {} }, userEvent("u1", "first"));
  await idle();
  assert.equal(fetchImpl.calls.filter((c) => c.path === "/events").length, 2, "acknowledged ids are deduped");
});

test("capture preserves ordering even when one send is far slower than the next", async () => {
  const delays = new Map([["slow", 40]]);
  const { bind, onEvent, fetchImpl, idle } = runtime({ "/health": HEALTH, "/events": (body) => ({
    status: 200,
    json: async () => { await new Promise((r) => setTimeout(r, delays.get(body.event_id) ?? 0)); return ACCEPTED.payload; },
  }) });
  bind();
  const session = { id: "sess-A", header: {} };
  onEvent(session, userEvent("slow", "slow user message"));
  onEvent(session, userEvent("fast", "fast user message"));
  onEvent(session, userEvent("fast2", "second fast message"));
  await idle();

  const sent = fetchImpl.calls.filter((c) => c.path === "/events").map((c) => c.body.event_id);
  assert.deepEqual(sent, ["slow", "fast", "fast2"], "the serial chain preserves emission order");
});

test("capture and recall switch off independently by config", async () => {
  const { bind, onEvent, onPreStep, fetchImpl, idle } = runtime(
    { "/health": HEALTH, "/events": ACCEPTED, "/prefetch": { payload: { ok: true, block: BLOCK, references: REFERENCES } } },
    { overlay: { ...OVERLAY, capture: false } });
  bind();

  onEvent({ id: "sess-A", header: {} }, userEvent("u1", "hello"));
  await idle();
  assert.equal(fetchImpl.calls.filter((c) => c.path === "/events").length, 0, "capture off sends nothing");

  const messages = claimed("what is the retention policy?");
  const decision = await onPreStep(preStep("sess-A", messages), async () => enterDecision(messages));
  assert.equal(decision.messages.length, 2, "recall is still on");
});

test("recall can be switched off while capture keeps working", async () => {
  const { bind, onEvent, onPreStep, fetchImpl, idle } = runtime(
    { "/health": HEALTH, "/events": ACCEPTED, "/prefetch": { payload: { ok: true, block: BLOCK, references: REFERENCES } } },
    { overlay: { ...OVERLAY, recall: false } });
  bind();

  onEvent({ id: "sess-A", header: {} }, userEvent("u1", "hello"));
  await idle();
  assert.equal(fetchImpl.calls.filter((c) => c.path === "/events").length, 1, "capture still works");

  const messages = claimed("what is the retention policy?");
  const decision = await onPreStep(preStep("sess-A", messages), async () => enterDecision(messages));
  assert.equal(decision.messages, messages, "no injection when recall is off");
});

// ── 6. config: disabled / unreadable / invalid stays inert ────────────────
test("a disabled, unreadable or invalid config leaves the host untouched with at most one warning", async () => {
  const cases = [
    { state: "disabled", silent: true },
    { state: "unreadable" },
    { state: "invalid-json" },
    { state: "not-an-object" },
    { state: "invalid-config" },
  ];
  for (const { state, silent } of cases) {
    const { bind, onEvent, onPreStep, fetchImpl, warnings, idle } = runtime(
      { "/health": HEALTH, "/events": ACCEPTED }, { config: null, configState: state, overlay: null });
    assert.doesNotThrow(() => bind());

    const messages = claimed("what is the retention policy?");
    const decision = await onPreStep(preStep("sess-A", messages), async () => enterDecision(messages));
    assert.equal(decision.messages, messages, `${state}: downstream untouched`);
    onEvent({ id: "sess-A", header: {} }, userEvent("u1", "hello"));
    await idle();
    assert.equal(fetchImpl.calls.length, 0, `${state}: no HTTP at all`);
    assert.ok(warnings.length <= 1, `${state}: at most one warning, got ${warnings.length}`);
    assert.equal(warnings.filter((w) => w.startsWith("hippocampus: configuration")).length,
      silent ? 0 : 1, `${state}: disabled is silent, a broken config warns exactly once`);
  }
});

test("loadConfig reads a given file and reports why it is unusable, without ever throwing", () => {
  const readFile = (contents) => () => {
    if (contents instanceof Error) throw contents;
    return contents;
  };
  const base = { env: {}, cwd: "/proj" };
  assert.equal(configPathFor(base), joinPath("/proj", ".dsh", "hippocampus.json"));
  assert.equal(configPathFor({ env: { HIPPOCAMPUS_DSH_CONFIG: "  " }, cwd: "/proj" }),
    joinPath("/proj", ".dsh", "hippocampus.json"), "a blank override falls back to the default path");
  assert.equal(configPathFor({ env: { HIPPOCAMPUS_DSH_CONFIG: " /x/y.json " }, cwd: "/proj" }),
    "/x/y.json", "the override is trimmed");

  assert.equal(loadConfig({ ...base, readFile: readFile(new Error("ENOENT")) }).reason, "unreadable");
  assert.equal(loadConfig({ ...base, readFile: readFile("{oops") }).reason, "invalid-json");
  assert.equal(loadConfig({ ...base, readFile: readFile("[]") }).reason, "not-an-object");
  assert.equal(loadConfig({ ...base, readFile: readFile('{"enabled": false}') }).reason, "disabled");
  assert.equal(loadConfig({ ...base, readFile: readFile('{"enabled": true, "mode": "owned"}') }).reason,
    "invalid-config", "v1 supports external mode only");
  assert.equal(loadConfig({ ...base, readFile: readFile('{"enabled": true, "mode": "external"}') }).reason,
    "invalid-config", "external mode requires a bridgeUrl");
  assert.equal(loadConfig({ ...base, readFile: readFile('{"enabled": true, "mode": "external", "bridgeUrl": "ftp://x"}') }).reason,
    "invalid-config", "only http/https is accepted");

  const good = loadConfig({ ...base, readFile: readFile(
    '{"enabled": true, "mode": "external", "bridgeUrl": "http://127.0.0.1:8765/"}') });
  assert.equal(good.ok, true);
  assert.equal(good.config.bridgeUrl, "http://127.0.0.1:8765", "the trailing slash is stripped");
  assert.equal(good.config.timeoutMs, 10000, "the default timeout");
  assert.equal(good.config.memoryBudgetChars, 8000, "the default budget");
  assert.equal(good.config.capture, true);
  assert.equal(good.config.recall, true);

  const tuned = loadConfig({ ...base, readFile: readFile(
    '{"enabled": true, "mode": "external", "bridgeUrl": "http://h:1", "timeoutMs": 0, "memoryBudgetChars": -5, "capture": false}') });
  assert.equal(tuned.config.timeoutMs, 10000, "a non-positive timeout falls back to the default");
  assert.equal(tuned.config.memoryBudgetChars, 8000, "a non-positive budget falls back to the default");
  assert.equal(tuned.config.capture, false);
  assert.equal(tuned.config.recall, true);
});

// ── 6b. two configuration sources, and their precedence ──────────────────
test("source 1: the DSH overlay object passed to apply(ctx, config) is the config", async () => {
  const overlay = { enabled: true, mode: "external", bridgeUrl: "http://127.0.0.1:9/",
    timeoutMs: 500, memoryBudgetChars: 8000, capture: true, recall: true };
  const { bind, fetchImpl, onEvent, idle, warnings } = runtime(
    { "/health": HEALTH, "/events": ACCEPTED }, { overlay, fileConfig: null, configState: "unreadable" });
  bind();

  assert.deepEqual(warnings, [], "the file is never consulted, so it is never reported");
  onEvent({ id: "sess-A", header: {} }, userEvent("u1", "hello"));
  await idle();
  assert.equal(fetchImpl.calls.filter((c) => c.path === "/events").length, 1, "the overlay config took effect");
  assert.equal(fetchImpl.calls[0].url.startsWith("http://127.0.0.1:9/"), true, "the overlay bridgeUrl was used");
});

test("source 2: with no overlay object the §2.1 config file convention is used", async () => {
  for (const overlay of [undefined, null, {}]) {
    const { bind, fetchImpl, onEvent, idle, warnings } = runtime(
      { "/health": HEALTH, "/events": ACCEPTED },
      { overlay, fileConfig: CONFIG, configState: "ok" });
    bind();
    assert.deepEqual(warnings, [], `overlay ${JSON.stringify(overlay)}: the file config is usable, no warning`);
    onEvent({ id: "sess-A", header: {} }, userEvent("u1", "hello"));
    await idle();
    assert.equal(fetchImpl.calls.filter((c) => c.path === "/events").length, 1,
      `overlay ${JSON.stringify(overlay)}: the file config took effect`);
  }
});

test("precedence: the overlay wins over a perfectly good config file", async () => {
  const overlay = { enabled: true, mode: "external", bridgeUrl: "http://overlay.invalid:1111" };
  const { bind, fetchImpl, onEvent, idle, warnings } = runtime(
    { "/health": HEALTH, "/events": ACCEPTED },
    { overlay, fileConfig: { ...CONFIG, bridgeUrl: "http://from-file.invalid:2222" } });
  bind();

  assert.deepEqual(warnings, [], "the file is not even read when the overlay is present");
  onEvent({ id: "sess-A", header: {} }, userEvent("u1", "hello"));
  await idle();
  const events = fetchImpl.calls.filter((c) => c.path === "/events");
  assert.equal(events.length, 1);
  assert.equal(events[0].url.startsWith("http://overlay.invalid:1111"), true,
    "the overlay bridgeUrl won, not the file's");
  assert.equal(fetchImpl.calls.some((c) => c.url.includes("from-file.invalid")), false);
});

test("precedence: an empty overlay falls through to the file, an INVALID overlay does not", async () => {
  // Empty → the file is used.
  const empty = runtime({ "/health": HEALTH, "/events": ACCEPTED },
    { overlay: {}, fileConfig: CONFIG });
  empty.bind();
  empty.onEvent({ id: "sess-A", header: {} }, userEvent("u1", "hello"));
  await empty.idle();
  assert.equal(empty.fetchImpl.calls.filter((c) => c.path === "/events").length, 1,
    "an empty overlay defers to the file");

  // Present but invalid → inert, and the file is NOT silently substituted.
  for (const bad of [{ enabled: true, mode: "owned", bridgeUrl: "http://x.invalid:1" },
    { enabled: true, mode: "external", bridgeUrl: "ftp://x.invalid" },
    { enabled: false, mode: "external", bridgeUrl: "http://x.invalid:1" }]) {
    const inert = runtime({ "/health": HEALTH, "/events": ACCEPTED },
      { overlay: bad, fileConfig: CONFIG, config: null, configState: "invalid-config" });
    inert.bind();
    inert.onEvent({ id: "sess-A", header: {} }, userEvent("u1", "hello"));
    await inert.idle();
    assert.equal(inert.fetchImpl.calls.length, 0,
      `${JSON.stringify(bad)}: an explicit invalid overlay is inert, never back-filled from the file`);
    assert.ok(inert.warnings.length <= 1);
  }
});

test("both sources produce the identical normalized shape", () => {
  const fileSource = loadConfig({ env: {}, cwd: "/proj", readFile: () => JSON.stringify(OVERLAY) });
  const overlaySource = normalizeConfig(OVERLAY);
  assert.equal(fileSource.ok, true);
  assert.equal(overlaySource.ok, true);
  assert.deepEqual(overlaySource.config, fileSource.config,
    "the overlay and the file normalize to exactly the same object");
  assert.deepEqual(Object.keys(fileSource.config).sort(),
    ["bridgeUrl", "capture", "memoryBudgetChars", "mode", "recall", "timeoutMs"]);
});

test("neither source usable leaves the plugin inert with at most one warning and no throw", async () => {
  const cases = [
    { label: "no overlay, unreadable file", overlay: null, configState: "unreadable", warn: 1 },
    { label: "no overlay, broken file", overlay: null, configState: "invalid-json", warn: 1 },
    { label: "no overlay, disabled file", overlay: null, configState: "disabled", warn: 0 },
    { label: "no overlay, invalid file", overlay: null, configState: "invalid-config", warn: 1 },
    { label: "invalid overlay, unreadable file", overlay: { enabled: true, mode: "owned" },
      configState: "unreadable", warn: 1 },
  ];
  for (const { label, overlay, configState, warn: expected } of cases) {
    const { bind, onEvent, onPreStep, fetchImpl, warnings, idle } = runtime(
      { "/health": HEALTH, "/events": ACCEPTED }, { overlay, configState, config: null });
    assert.doesNotThrow(() => bind(), label);
    const messages = claimed("what is the retention policy?");
    const decision = await onPreStep(preStep("sess-A", messages), async () => enterDecision(messages));
    assert.equal(decision.messages, messages, `${label}: downstream untouched`);
    onEvent({ id: "sess-A", header: {} }, userEvent("u1", "hello"));
    await idle();
    assert.equal(fetchImpl.calls.length, 0, `${label}: no HTTP at all`);
    assert.equal(warnings.length, expected, `${label}: expected ${expected} warning(s), got ${warnings.length}`);
  }
});

// ── status: real state, and never a false "remembered" ────────────────────
test("status reports the real state and never claims a failed send was remembered", async () => {
  const ready = runtime({ "/health": HEALTH, "/events": ACCEPTED });
  ready.bind();
  ready.onEvent({ id: "sess-A", header: {} }, userEvent("u1", "hello"));
  await ready.idle();
  const status = ready.runtime.status();
  assert.match(status, /^active \(backend ready, capabilities: /);
  assert.ok(status.includes("events") && status.includes("prefetch"));

  const failing = runtime({ "/health": HEALTH,
    "/events": { status: 500, payload: { ok: false, status: "retryable" } } });
  failing.bind();
  failing.onEvent({ id: "sess-B", header: {} }, userEvent("u1", "hello"));
  await failing.idle();
  assert.ok(failing.warnings.length >= 1);
  assert.ok(!/remembered/i.test(failing.warnings.join("\n")),
    "a failed send is never reported as remembered");

  const down = runtime({ "/health": { status: 503, payload: { ok: false } } });
  down.bind();
  await down.onPreStep(preStep("sess-C", claimed("q")), async () => enterDecision(claimed("q")));
  assert.ok(/^unavailable \(/.test(down.runtime.status()), `got: ${down.runtime.status()}`);

  const inert = runtime({}, { config: null, configState: "disabled", overlay: null });
  inert.bind();
  assert.equal(inert.runtime.status(), "disabled");
  const broken = runtime({}, { config: null, configState: "unreadable", overlay: null });
  broken.bind();
  assert.ok(broken.runtime.status().startsWith("unavailable (config unreadable)"),
    `got: ${broken.runtime.status()}`);
});
