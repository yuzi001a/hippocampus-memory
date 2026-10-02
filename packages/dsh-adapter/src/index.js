// DSH plugin entry for Hippocampus memory.
//
// It does two things and nothing else:
//   1. `session/event`  — records committed user/assistant text to the B01 bridge.
//   2. `agent/pre-step` — injects at most ONE additive recalled-memory message
//      per user turn, into the SAME model request.
//
// No top-level side effect: importing this module opens no socket, reads no
// config, starts no process and creates no timer. Everything is lazy, and
// every failure path leaves the host exactly as it found it.
import { BridgeClient, HOST_ID } from "./bridge.js";
import { loadConfig, normalizeConfig } from "./config.js";
import { RecallLatch, RECALL_NOTE } from "./recall.js";
import { TurnTracker, mapSessionEvent, textOf, userQueryOf } from "./mapping.js";

export const name = "hippocampus";

// Optional DSH service injection (see upstream `export const inject` in
// packages/acp/acp/src/index.ts:62). `sessions` backs `session/event`; the
// agent scope backs `agent/pre-step`.
export const inject = ["sessions"];

export { configPathFor, loadConfig, normalizeConfig } from "./config.js";
export { BridgeClient, HOST_ID, REQUIRED_PROTOCOL } from "./bridge.js";
export { RecallLatch, RECALL_NOTE } from "./recall.js";
export { TurnTracker, mapSessionEvent, textOf, userQueryOf } from "./mapping.js";

function sessionIdOf(session) {
  const id = typeof session === "string" ? session : session?.id;
  return typeof id === "string" ? id : "";
}

// The runtime: everything with state. `apply()` is a thin binding of it to a
// DSH `ctx`; tests construct it directly with an injected fetchImpl, a fake
// ctx and an explicit config, so nothing on disk or on the network is read.
export function createMemoryRuntime({
  ctx = null,
  config = null,
  configState = "ok",
  fetchImpl = globalThis.fetch,
  warn = () => {},
} = {}) {
  const client = config ? new BridgeClient({ baseUrl: config.bridgeUrl, timeoutMs: config.timeoutMs, fetchImpl }) : null;
  const latch = new RecallLatch({ warn });
  const tracker = new TurnTracker();
  const seen = new Set();          // `${sessionId}::${eventId}` — native identity only
  let chain = Promise.resolve();   // serial capture chain: emission order preserved
  let handshake = null;
  let handshakeAttempted = false;

  const active = () => Boolean(config) && !latch.disabled;

  // Lazy: no socket, no timer, no work until the host actually emits an event.
  async function ensureReady() {
    if (!client) return null;
    if (handshakeAttempted) return handshake?.ok ? client : null;
    handshakeAttempted = true;
    handshake = await client.handshake();
    if (!handshake.ok) {
      warn("hippocampus: memory backend unavailable (handshake failed); memory features are off");
      return null;
    }
    return client;
  }

  // One drain at a time, so a later message can never overtake an earlier one.
  function enqueue(task) {
    const run = chain.then(task, task);
    chain = run.then(() => undefined, () => undefined);
    return run;
  }

  async function onSessionEvent(session, event) {
    if (!config || !active() || !config.capture) return;
    const sessionId = sessionIdOf(session);
    if (!sessionId) return;
    tracker.observe(session, event);

    const mapped = mapSessionEvent(session, event, { turn: tracker.turnOf(session) });
    if (!mapped) return;
    const key = `${sessionId}::${mapped.event_id}`;
    if (seen.has(key)) return;
    // Fire-and-forget by design: `session/event` is a post-commit feed, and
    // the host must never await a memory write.
    void enqueue(async () => {
      const ready = await ensureReady();
      if (!ready) return;
      const ack = await ready.sendEvent(mapped);
      // A failed send does NOT advance the dedupe set, so the same event is
      // retried if the log offers it again. It is never reported as stored.
      if (!ack.ok) {
        warn("hippocampus: an event was not acknowledged by the memory backend; it will be retried");
        return;
      }
      seen.add(key);
    });
  }

  async function onPreStep(payload, next) {
    // `next()` first: the decision is the host's, and a rejection must be
    // returned untouched and unrebuilt.
    const downstream = await next();
    if (!config || !active() || !config.recall) return downstream;
    if (downstream?.kind !== "enter") return downstream;

    const session = payload?.agent?.session ?? null;
    const message = await latch.recallFor({
      session,
      sessionId: sessionIdOf(session),
      turn: payload?.turn,
      messages: payload?.messages,
      client: await ensureReady(),
      memoryBudgetChars: config.memoryBudgetChars,
    });
    // Purely additive: the claimed messages are never replaced, reordered,
    // mutated or spliced, and the system prompt is never touched.
    return message ? { ...downstream, messages: [...downstream.messages, message] } : downstream;
  }

  function status() {
    if (!config) return configState === "disabled" ? "disabled" : `unavailable (config ${configState})`;
    if (!handshake) return "configured (not connected yet)";
    return handshake.ok
      ? `active (backend ready, capabilities: ${Object.keys(handshake.capabilities || {}).join(", ") || "none"})`
      : `unavailable (${handshake.reason})`;
  }

  // Observable idle point for tests; also what a host shutdown would await.
  function whenIdle() { return chain; }

  return { onSessionEvent, onPreStep, status, whenIdle, seen, tracker, latch };
}

function hostWarn(ctx, message) {
  try { ctx?.logger?.warn?.(message); } catch { /* a host without a logger is still fine */ }
}

// The DSH plugin shape: `export const name` / `export function apply(ctx, config)`.
// Registration goes through `ctx.on`, so every listener is disposed with the
// plugin's scope on unload.
//
// Configuration comes from two sources, in this precedence order:
//   1. the DSH overlay object DSH passes as `config` (the DSH-native path —
//      the profile's cordis.patch.yml row carries a `config` block);
//   2. otherwise the file convention of §2.1 (env `HIPPOCAMPUS_DSH_CONFIG`,
//      else `<cwd>/.dsh/hippocampus.json`).
// Both go through the same normalizer, so both yield the identical shape.
// A missing or invalid configuration in BOTH places leaves the plugin inert
// with at most one warning, and never throws.
//
// The third argument is a test seam only: it lets a unit test exercise this
// exact wiring with an injected loader and factory, so that no config file and
// no socket are ever touched.
export function apply(ctx, config, { loadConfigImpl = loadConfig, createRuntime = createMemoryRuntime } = {}) {
  const warn = (message) => hostWarn(ctx, message);

  const loaded = resolveConfig(config, loadConfigImpl);
  const resolved = loaded.ok ? loaded.config : null;

  if (!loaded.ok && loaded.reason !== "disabled") {
    warn(`hippocampus: configuration not usable (${loaded.reason}); memory features are off`);
  }

  const runtime = createRuntime({ ctx, config: resolved, configState: loaded.ok ? "ok" : loaded.reason, warn });

  ctx.on("session/event", (session, event) => runtime.onSessionEvent(session, event));
  ctx.on("agent/pre-step", (payload, next) => runtime.onPreStep(payload, next));

  return runtime;
}

// An overlay object counts as "supplied" only when it is a non-array object
// that actually carries a key: an absent, null or `{}` config falls through to
// the file. An overlay that is present but invalid is NOT silently replaced by
// the file — the operator's explicit row wins, and an invalid one is inert.
function overlayIsSupplied(config) {
  return Boolean(config) && typeof config === "object" && !Array.isArray(config)
    && Object.keys(config).length > 0;
}

function resolveConfig(config, loadConfigImpl) {
  if (overlayIsSupplied(config)) return { ...normalizeConfig(config), source: "overlay" };
  const loaded = loadConfigImpl();
  return loaded.ok ? { ...loaded, source: "file" } : { ...loaded, source: "file" };
}
