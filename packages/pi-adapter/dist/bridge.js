// B01 bridge client for the pi adapter.
// Owns exactly two things: config resolution, and the transport/lifecycle of
// the frozen v3core HTTP surface (GET /health, POST /events, POST /prefetch,
// POST /tool). Nothing here writes memory semantics, and no endpoint,
// credential or profile is ever hardcoded.
import { spawn as nodeSpawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { isAbsolute, join } from "node:path";

export const HOST_ID = "pi";
export const REQUIRED_PROTOCOL = "b01.1";
export const DEFAULT_MEMORY_BUDGET_CHARS = 8000;
const DEFAULT_TIMEOUT_MS = 10000;
const READY_TIMEOUT_MS = 20000;
const CLOSE_TIMEOUT_MS = 5000;

// ── config ────────────────────────────────────────────────────────────────
export function configPathFor({ env = process.env, cwd = process.cwd() } = {}) {
  const explicit = (env.HIPPOCAMPUS_PI_CONFIG || "").trim();
  return explicit || join(cwd, ".pi", "hippocampus.json");
}

export function loadConfig({ env = process.env, cwd = process.cwd(), readFile = readFileSync } = {}) {
  const path = configPathFor({ env, cwd });
  let raw;
  try {
    raw = readFile(path, "utf8");
  } catch {
    return { ok: false, path, reason: "unreadable" };
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { ok: false, path, reason: "invalid-json" };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, path, reason: "not-an-object" };
  }
  if (parsed.enabled !== true) return { ok: false, path, reason: "disabled", config: parsed };
  const mode = parsed.mode === "owned" ? "owned" : "external";
  const base = { mode, timeoutMs: positiveInt(parsed.timeoutMs, DEFAULT_TIMEOUT_MS),
    memoryBudgetChars: positiveInt(parsed.memoryBudgetChars, DEFAULT_MEMORY_BUDGET_CHARS) };
  if (mode === "external") {
    if (!isHttpUrl(parsed.bridgeUrl)) return { ok: false, path, reason: "invalid-config", config: base };
    return { ok: true, path, config: { ...base, bridgeUrl: stripSlash(parsed.bridgeUrl) } };
  }
  const owned = { pythonExecutable: parsed.pythonExecutable, coreConfig: parsed.coreConfig,
    profile: typeof parsed.profile === "string" ? parsed.profile.trim() : "" };
  if (!isAbsolute(owned.pythonExecutable || "") || !isAbsolute(owned.coreConfig || "") || !owned.profile) {
    return { ok: false, path, reason: "invalid-config", config: base };
  }
  return { ok: true, path, config: { ...base, ...owned } };
}

function positiveInt(value, fallback) {
  const n = Number(value);
  return Number.isInteger(n) && n > 0 ? n : fallback;
}
function isHttpUrl(value) {
  if (typeof value !== "string" || !value.trim()) return false;
  try {
    const u = new URL(value.trim());
    return (u.protocol === "http:" || u.protocol === "https:") && Boolean(u.host);
  } catch { return false; }
}
function stripSlash(url) { return url.trim().replace(/\/+$/, ""); }

// ── transport ─────────────────────────────────────────────────────────────
export class BridgeClient {
  constructor({ baseUrl, timeoutMs = DEFAULT_TIMEOUT_MS, fetchImpl = globalThis.fetch } = {}) {
    this.baseUrl = baseUrl;
    this.timeoutMs = timeoutMs;
    this.fetchImpl = fetchImpl;
  }

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

  // Actual protocol handshake: only b01.1 is accepted, everything else is
  // reported as unavailable instead of being silently tolerated.
  async handshake() {
    try {
      const { payload } = await this.#request("/health");
      if (payload.bridge_protocol_version !== REQUIRED_PROTOCOL) return { ok: false, reason: "protocol-mismatch" };
      return { ok: true, capabilities: payload.capabilities || {}, core: payload.core || "unknown" };
    } catch {
      return { ok: false, reason: "unreachable" };
    }
  }

  // Only an explicit accepted/duplicate ACK counts as transport acceptance.
  async sendEvent(event) {
    try {
      const { payload } = await this.#request("/events", event);
      const ok = payload.ok === true && (payload.status === "accepted" || payload.status === "duplicate");
      return { ok, status: typeof payload.status === "string" ? payload.status : "failed",
        sourceId: typeof payload.source_id === "string" ? payload.source_id : undefined };
    } catch {
      return { ok: false, status: "failed" };
    }
  }

  async prefetch(query, sessionId) {
    try {
      const { payload } = await this.#request("/prefetch", { query, session_id: sessionId });
      if (payload.ok !== true || typeof payload.block !== "string") return { ok: false, block: "" };
      return { ok: true, block: payload.block };
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

// ── owned child lifecycle ─────────────────────────────────────────────────
// Lazily launched, bounded readiness, released only when this object owns it.
export class OwnedBackend {
  constructor({ pythonExecutable, coreConfig, profile, spawnImpl = nodeSpawn,
    env = process.env, readyTimeoutMs = READY_TIMEOUT_MS, closeTimeoutMs = CLOSE_TIMEOUT_MS } = {}) {
    this.spec = { pythonExecutable, coreConfig, profile };
    this.spawnImpl = spawnImpl;
    this.env = env;
    this.readyTimeoutMs = readyTimeoutMs;
    this.closeTimeoutMs = closeTimeoutMs;
    this.child = null;
    this.closePromise = null;
  }

  async start() {
    if (this.child) return this.baseUrl;
    this.closePromise = null;     // a restart begins a fresh lifecycle
    const args = ["-u", "-m", "v3core", "serve", "--host", "127.0.0.1", "--port", "0",
      "--profile", this.spec.profile, "--ready-json"];
    try {
      this.child = this.spawnImpl(this.spec.pythonExecutable, args, {
        env: { ...this.env, V3CORE_CONFIG: this.spec.coreConfig },
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch {
      await this.close();
      return null;
    }
    this.child.on("error", () => { this.exited = true; });
    this.child.on("exit", () => { this.exited = true; });
    try { this.child.stdin?.end?.(); } catch { /* the serve surface never reads stdin */ }
    try { this.child.stderr?.resume?.(); } catch { /* consumed, never echoed */ }
    const ready = await this.#awaitReady();
    if (!ready) { await this.close(); return null; }
    this.baseUrl = `http://${ready.host || "127.0.0.1"}:${ready.port}`;
    return this.baseUrl;
  }

  #awaitReady() {
    const child = this.child;
    if (!child?.stdout) return Promise.resolve(null);
    return new Promise((resolve) => {
      let buffer = "";
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        child.stdout.off("data", onData);
        child.stdout.off("end", onEnd);
        child.off("exit", onExit);
        child.off("error", onError);
        resolve(value);
      };
      const onData = (chunk) => {
        buffer += String(chunk);
        for (const line of buffer.split("\n")) {
          if (!line.trim().startsWith("{")) continue;
          try {
            const msg = JSON.parse(line);
            if (msg && msg.event === "ready" && Number.isInteger(msg.port)) { finish(msg); return; }
          } catch { /* keep scanning: ready json is one line among others */ }
        }
      };
      const onEnd = () => finish(null);
      const onExit = () => finish(null);
      const onError = () => finish(null);
      // Deliberately NOT unref'd: this is a bounded, short-lived wait and the
      // process must not be able to exit out from under a pending readiness.
      const timer = setTimeout(() => finish(null), this.readyTimeoutMs);
      child.stdout.on("data", onData);
      child.stdout.on("end", onEnd);
      child.once("exit", onExit);
      child.once("error", onError);
    });
  }

  // Only the child this object spawned is ever signalled, through its own
  // handle. An external backend is never borrowed, killed or tree-killed, and
  // no bare pid/taskkill is ever used. The exit wait is installed BEFORE the
  // signal so a child that exits instantly is still observed, and it is
  // bounded: a stubborn child yields `{ ok: false }` instead of an endless
  // wait or a false claim of release. Idempotent.
  close() {
    if (this.closePromise) return this.closePromise;
    const child = this.child;
    this.child = null;
    this.baseUrl = null;
    if (!child) return (this.closePromise = Promise.resolve({ ok: true, reason: "no-child" }));
    this.closePromise = this.#awaitExit(child).then((exited) => {
      if (exited) return { ok: true, reason: "exited", code: child.exitCode };
      return { ok: false, reason: "still-alive" };
    });
    return this.closePromise;
  }

  #awaitExit(child) {
    if (child.exitCode !== null || child.exited) return Promise.resolve(true);
    return new Promise((resolve) => {
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        child.off("exit", onExit);
        child.off("error", onError);
        resolve(value);
      };
      const onExit = () => finish(true);
      const onError = () => finish(true);     // spawn/handle errors mean it is gone
      const timer = setTimeout(() => finish(false), this.closeTimeoutMs);
      child.on("exit", onExit);
      child.on("error", onError);
      try { child.stdin?.end?.(); } catch { /* already gone */ }
      try { if (child.exitCode === null && !child.killed) child.kill(); } catch { /* already gone */ }
    });
  }
}
