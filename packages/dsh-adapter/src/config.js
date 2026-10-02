// Configuration resolution and validation for the DSH adapter.
// Fail-safe by construction: this module never throws, never reads anything
// outside the one configured path, and never returns a value that could carry
// a URL credential or raw file text back to a log.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { DEFAULT_MEMORY_BUDGET_CHARS, DEFAULT_TIMEOUT_MS } from "./bridge.js";

export { DEFAULT_MEMORY_BUDGET_CHARS, DEFAULT_TIMEOUT_MS };

// Same convention as the B03 pi adapter, DSH-specific: an explicit env
// override, else `<cwd>/.dsh/hippocampus.json`.
export function configPathFor({ env = process.env, cwd = process.cwd() } = {}) {
  const explicit = (env.HIPPOCAMPUS_DSH_CONFIG || "").trim();
  return explicit || join(cwd, ".dsh", "hippocampus.json");
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

// The one normalizer. Both configuration sources — the DSH overlay object and
// the JSON file — go through this, so they always produce the identical shape
// `{ mode, bridgeUrl, timeoutMs, memoryBudgetChars, capture, recall }`.
export function normalizeConfig(parsed) {
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, reason: "not-an-object" };
  }
  if (parsed.enabled !== true) return { ok: false, reason: "disabled" };
  // v1 is external-only: this package never starts or owns a core.
  if (parsed.mode !== "external") return { ok: false, reason: "invalid-config" };
  if (!isHttpUrl(parsed.bridgeUrl)) return { ok: false, reason: "invalid-config" };
  return {
    ok: true,
    config: {
      mode: "external",
      bridgeUrl: stripSlash(parsed.bridgeUrl),
      timeoutMs: positiveInt(parsed.timeoutMs, DEFAULT_TIMEOUT_MS),
      memoryBudgetChars: positiveInt(parsed.memoryBudgetChars, DEFAULT_MEMORY_BUDGET_CHARS),
      capture: parsed.capture !== false,
      recall: parsed.recall !== false,
    },
  };
}

// Every unusable shape returns `{ ok: false, reason }` where `reason` is a
// closed vocabulary, not text from the file: a caller can log the class
// without ever printing a URL, a path or a credential.
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
  return { ...normalizeConfig(parsed), path };
}
