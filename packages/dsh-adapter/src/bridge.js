// B01 bridge client for the DSH adapter.
// Owns exactly one thing: the transport and lifecycle of the frozen v3core
// HTTP surface (GET /health, POST /events, POST /prefetch, POST /tool).
// No endpoint, credential, profile or port is hardcoded, and no method here
// throws or logs: a bridge problem is a value, never an exception that could
// escape into the DSH host.
export const HOST_ID = "dsh";
export const REQUIRED_PROTOCOL = "b01.1";
export const DEFAULT_MEMORY_BUDGET_CHARS = 8000;
export const DEFAULT_TIMEOUT_MS = 10000;

export class BridgeClient {
  constructor({ baseUrl, timeoutMs = DEFAULT_TIMEOUT_MS, fetchImpl = globalThis.fetch } = {}) {
    this.baseUrl = baseUrl;
    this.timeoutMs = timeoutMs;
    this.fetchImpl = fetchImpl;
  }

  // Every call carries an abort deadline: a bridge that accepts a connection
  // and then stalls must never hold a DSH turn open.
  async #request(path, body) {
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    const payload = await response.json().catch(() => null);
    if (!payload || typeof payload !== "object") throw new Error("malformed-response");
    return { status: response.status, payload };
  }

  // The protocol handshake: only b01.1 is accepted. Anything else is reported
  // as unavailable rather than silently tolerated.
  async handshake() {
    try {
      const { payload } = await this.#request("/health");
      if (payload.bridge_protocol_version !== REQUIRED_PROTOCOL) return { ok: false, reason: "protocol-mismatch" };
      return { ok: true, capabilities: payload.capabilities || {}, core: payload.core || "unknown" };
    } catch {
      return { ok: false, reason: "unreachable" };
    }
  }

  // Only an explicit accepted/duplicate ACK counts as transport acceptance;
  // the raw server body never leaves this method.
  async sendEvent(event) {
    try {
      const { payload } = await this.#request("/events", event);
      const ok = payload.ok === true && (payload.status === "accepted" || payload.status === "duplicate");
      return {
        ok,
        status: typeof payload.status === "string" ? payload.status : "failed",
        sourceId: typeof payload.source_id === "string" ? payload.source_id : undefined,
      };
    } catch {
      return { ok: false, status: "failed" };
    }
  }

  // A block is only ever the core's own text: on any failure it is "" and
  // never a fabricated one.
  async prefetch(query, sessionId) {
    try {
      const { payload } = await this.#request("/prefetch", { query, session_id: sessionId });
      if (payload.ok !== true || typeof payload.block !== "string") return { ok: false, block: "" };
      const references = Array.isArray(payload.references) ? payload.references : [];
      return { ok: true, block: payload.block, references };
    } catch {
      return { ok: false, block: "" };
    }
  }

  async tool(name, args) {
    try {
      const { payload } = await this.#request("/tool", { name, args });
      if (payload.ok !== true) return { ok: false, result: null, found: false };
      return { ok: true, result: typeof payload.result === "string" ? payload.result : null, found: true };
    } catch {
      return { ok: false, result: null, found: false };
    }
  }
}
