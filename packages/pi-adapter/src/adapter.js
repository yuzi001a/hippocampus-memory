// Session-scoped memory thin client for pi.
// Responsibilities: map persisted native entries to B01 events, drain them in
// order, and inject one additive custom memory message per user input.
// It never invents identity (no hashed ids, no synthetic turn/branch ids, no
// timers), never replaces host context, and never reports a failed send as
// "remembered".
import { BridgeClient, OwnedBackend, HOST_ID } from "./bridge.js";

export const MEMORY_CUSTOM_TYPE = "hippocampus-memory";
const TEXT_ONLY_NOTE = "[hippocampus memory — recalled context, not a new user fact]";

// Only these two roles are recordable; custom/system/toolResult entries and
// compaction/branch summaries are structurally not messages we own.
function roleOf(entry) {
  const message = entry?.message;
  return message && typeof message === "object" ? message.role : undefined;
}

// Verbatim text blocks, in order. Non-text parts (image / thinking / toolCall)
// are a documented first-version limitation: they are dropped, never converted
// into invented prose.
export function textOf(message) {
  const content = message?.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((block) => block && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("");
}

export function mapEntry(entry, { sessionId, projectId } = {}) {
  if (!entry || entry.type !== "message") return null;
  if (typeof entry.id !== "string" || !entry.id) return null;
  const role = roleOf(entry);
  if (role !== "user" && role !== "assistant") return null;
  // An aborted or errored assistant result is not a completed answer.
  if (role === "assistant" && (entry.message.stopReason === "aborted" || entry.message.stopReason === "error")) {
    return null;
  }
  const content = textOf(entry.message);
  if (!content) return null;
  return {
    host: HOST_ID,
    session_id: sessionId,
    event_id: entry.id,
    role,
    content,
    timestamp: typeof entry.timestamp === "string" ? entry.timestamp : undefined,
    parent_event_id: typeof entry.parentId === "string" && entry.parentId ? entry.parentId : undefined,
    project_id: projectId || undefined,
  };
}

export class MemoryAdapter {
  constructor({ config, configState = "ok", fetchImpl, spawnImpl, readyTimeoutMs, warn = () => {} } = {}) {
    this.config = config || null;
    this.configState = config ? "ok" : configState;
    this.fetchImpl = fetchImpl;
    this.spawnImpl = spawnImpl;
    // Internal runtime bound for owned readiness. Deliberately NOT a config
    // file field: config owns the HTTP timeout only.
    this.readyTimeoutMs = readyTimeoutMs;
    this.warn = warn;
    this.sessionEnabled = Boolean(config);
    this.client = null;
    this.owned = null;
    this.handshake = null;
    this.seen = new Set();          // `${sessionId}::${entryId}` — native identity only
    this.recallArmed = false;        // per-input latch; local control, not an event
    this.chain = Promise.resolve();  // serial drain
    this.closed = false;
  }

  get state() {
    if (this.configState === "ok" && !this.sessionEnabled) return "disabled";
    return this.config ? "configured" : this.configState;
  }

  // Lazy on purpose: no process, socket or timer at import or factory time.
  async #ensureClient() {
    if (this.client) return this.client;
    if (!this.config || !this.sessionEnabled || this.closed) return null;
    // External mode always talks to the configured URL. Owned mode binds an
    // ephemeral port the child reports at readiness, so THAT url is the only
    // correct base: user config is never rewritten to hide the difference.
    let baseUrl = this.config.bridgeUrl;
    if (this.config.mode === "owned") {
      this.owned = new OwnedBackend({ ...this.config, spawnImpl: this.spawnImpl,
        readyTimeoutMs: this.readyTimeoutMs });
      baseUrl = await this.owned.start();
      if (!baseUrl) { this.owned = null; this.warn("hippocampus: backend did not become ready"); return null; }
    }
    this.client = new BridgeClient({ baseUrl, timeoutMs: this.config.timeoutMs,
      fetchImpl: this.fetchImpl });
    this.handshake = await this.client.handshake();
    if (!this.handshake.ok) {
      this.warn("hippocampus: memory backend unavailable (handshake failed)");
      this.client = null;
      if (this.owned) { await this.owned.close(); this.owned = null; }
      return null;
    }
    return this.client;
  }

  // Serialized: one drain at a time, so ordering is never raced.
  #serial(task) {
    const run = this.chain.then(task, task);
    this.chain = run.then(() => undefined, () => undefined);
    return run;
  }

  // Synchronous snapshot, taken BEFORE any await. The SDK sessionManager is a
  // LIVE object: a session switch that lands while this drain waits for
  // readiness would otherwise attribute the old entries to the new session.
  #capture(ctx) {
    const sessionId = this.#sessionId(ctx);
    if (!sessionId) return null;
    const projectId = typeof ctx?.cwd === "string" ? ctx.cwd : "";
    let entries;
    try { entries = ctx?.sessionManager?.getEntries?.(); } catch { return null; }
    if (!Array.isArray(entries)) return null;
    const events = [];
    for (const entry of entries) {
      const event = mapEntry(entry, { sessionId, projectId });
      if (event) events.push(event);
    }
    return { sessionId, events };
  }

  // A loaded session replays its persisted eligible entries once; every later
  // drain sends only unacknowledged deltas. Drain stops at the first failed
  // event so a later assistant answer can never pass a missing user turn.
  drain(ctx) {
    if (!this.config || !this.sessionEnabled || this.closed) return this.#settled();
    const snapshot = this.#capture(ctx);
    if (!snapshot) return this.#settled();
    return this.#serial(async () => {
      if (this.closed) return;
      const client = await this.#ensureClient();
      if (!client) return;
      const { sessionId, events } = snapshot;
      for (const event of events) {
        const key = `${sessionId}::${event.event_id}`;
        if (this.seen.has(key)) continue;
        const ack = await client.sendEvent(event);
        if (!ack.ok) { this.warn("hippocampus: event not acknowledged; will retry"); return; }
        this.seen.add(key);
      }
    });
  }

  #settled() { return Promise.resolve(); }

  #sessionId(ctx) {
    try { return ctx?.sessionManager?.getSessionId?.() || ""; } catch { return ""; }
  }

  // One recall per user input: the latch is armed by `input` and consumed by
  // the first before-agent call, so a repeated call performs no second
  // prefetch. The injected message is additive: it never replaces the prompt,
  // the system prompt, or any host context.
  async onBeforeAgentStart(event, ctx) {
    const armed = this.recallArmed;
    this.recallArmed = false;              // consumed exactly once
    if (!armed) return undefined;
    if (!this.config || !this.sessionEnabled || this.closed) return undefined;
    const query = typeof event?.prompt === "string" ? event.prompt : "";
    if (!query.trim()) return undefined;
    // Session id is read NOW: the manager is live, and the recall must be
    // attributed to the session that was selected when the hook fired.
    const sessionId = this.#sessionId(ctx);
    if (!sessionId) return undefined;
    // Unacknowledged entries from a previous turn are flushed BEFORE the
    // recall, so the block can never be older than what pi already knows.
    // drain() takes its own serial slot; it is not re-entered from inside one.
    await this.drain(ctx);
    return this.#serial(async () => {
      const client = await this.#ensureClient();
      if (!client) return undefined;
      const result = await client.prefetch(query, sessionId);
      if (!result.ok) {
        // One generic warning: no raw error, endpoint or path ever escapes.
        this.warn("hippocampus: recall unavailable; continuing without memory context");
        return undefined;
      }
      if (!result.block.trim()) return undefined;
      const budget = this.config.memoryBudgetChars;
      if (result.block.length > budget) {
        // Refuse the whole injection; a partial block would misrepresent the
        // memory content. Never substring it.
        this.warn(`hippocampus: recalled block exceeds budget (${result.block.length} > ${budget} chars); skipped`);
        return undefined;
      }
      return { message: { customType: MEMORY_CUSTOM_TYPE, display: false,
        content: `${TEXT_ONLY_NOTE}\n\n${result.block}`,
        details: { host: HOST_ID, sessionId, chars: result.block.length, budgetChars: budget, origin: "prefetch" } } };
    });
  }

  async readSource(sourceId) {
    const client = await this.#ensureClient();
    if (!client) return { ok: false, reason: "unavailable", found: false, text: "" };
    const response = await client.tool("v3_get", { target: "message", source_id: sourceId });
    if (!response.ok) return { ok: false, reason: "failed", found: false, text: "" };
    let found = true;
    if (typeof response.result === "string") {
      try {
        const parsed = JSON.parse(response.result);
        if (parsed && typeof parsed === "object" && parsed.success === false) found = false;
      } catch { /* non-JSON result is returned verbatim, treated as found */ }
    }
    return { ok: true, found, text: response.result ?? "" };
  }

  status() {
    if (this.closed) return "closed";
    if (!this.config) return `unavailable (config ${this.configState})`;
    if (!this.sessionEnabled) return "disabled (session off)";
    if (!this.handshake) return "configured (not connected yet)";
    return this.handshake.ok
      ? `active (backend ready, capabilities: ${Object.keys(this.handshake.capabilities || {}).join(", ") || "none"})`
      : `unavailable (${this.handshake.reason})`;
  }

  // Idempotent: only a child this adapter spawned is ever released. The final
  // persisted entries are drained FIRST, while the bridge is still reachable.
  async shutdown(ctx) {
    if (this.closed) return;
    await this.drain(ctx);
    this.closed = true;
    this.client = null;
    if (this.owned) { await this.owned.close(); this.owned = null; }
  }
}
