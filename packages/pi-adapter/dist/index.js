// pi extension entry point (package metadata points at dist/index.js).
// The default export receives the host ExtensionAPI; this package never
// imports the pi SDK at runtime and starts nothing at import time.
import { loadConfig } from "./bridge.js";
import { MemoryAdapter, MEMORY_CUSTOM_TYPE } from "./adapter.js";

export { MemoryAdapter, mapEntry, textOf } from "./adapter.js";
export { BridgeClient, OwnedBackend, loadConfig, configPathFor, HOST_ID } from "./bridge.js";

const USAGE = "/hippocampus status | on | off | source <exact-id>";

function notify(ctx, message, type = "info") {
  try { ctx?.ui?.notify?.(message, type); } catch { /* no UI in print/json modes */ }
}

export default function hippocampusExtension(pi) {
  const loaded = loadConfig();
  const adapter = new MemoryAdapter({
    config: loaded.ok ? loaded.config : null,
    configState: loaded.ok ? "ok" : loaded.reason,
    warn: (message) => console.warn(message),
  });
  if (!loaded.ok && loaded.reason !== "disabled") {
    console.warn(`hippocampus: configuration not usable (${loaded.reason}); memory features are off`);
  }

  // One recall per user input. The latch is local runtime control only.
  pi.on("input", () => { adapter.recallArmed = true; });

  // message_end carries no native entry id and fires before persistence, so
  // nothing is recorded here — identities come from the persisted entries.
  pi.on("message_end", () => {});

  pi.on("before_agent_start", (event, ctx) => adapter.onBeforeAgentStart(event, ctx));

  // Drain points: after persistence (agent_end), before the next recall, and
  // on every session replacement / shutdown path. Every hook returns or awaits
  // the drain promise, so a completed drain is observable, never fire-and-forget.
  pi.on("agent_end", (_event, ctx) => adapter.drain(ctx));
  pi.on("session_start", (_event, ctx) => adapter.drain(ctx));
  pi.on("session_before_switch", async (_event, ctx) => { await adapter.drain(ctx); });
  pi.on("session_before_fork", async (_event, ctx) => { await adapter.drain(ctx); });
  // The installed SDK (pi 0.99.2, dist/core/extensions/types.d.ts:1143-1184)
  // types no `session_switch` and no `session_fork`: the old session is
  // drained by the two before-hooks above, and the replacing/forked session
  // drains its own persisted entries from its own session_start.
  // The SDK ctx is forwarded so the final entries of the closing session are
  // drained while the bridge is still reachable.
  pi.on("session_shutdown", async (_event, ctx) => { await adapter.shutdown(ctx); });

  pi.registerCommand("hippocampus", {
    description: `Hippocampus memory status and controls (${USAGE})`,
    handler: async (args, ctx) => {
      const [verb = "status", ...rest] = String(args || "").trim().split(/\s+/);
      const value = rest.join(" ").trim();
      if (verb === "status") {
        notify(ctx, `hippocampus: ${adapter.status()}`);
        return;
      }
      if (verb === "on" || verb === "off") {
        if (!adapter.config) { notify(ctx, "hippocampus: unavailable (no usable configuration)", "warning"); return; }
        adapter.sessionEnabled = verb === "on";
        notify(ctx, `hippocampus: ${verb === "on" ? "enabled" : "disabled"} for this session only`);
        return;
      }
      if (verb === "source") {
        if (!value) { notify(ctx, `hippocampus: source needs an exact id — ${USAGE}`, "warning"); return; }
        // A source read is a custom message too: it must never be recorded as
        // if the user or the assistant had said it. It IS displayed, because
        // the user explicitly asked to see this traced original.
        const result = await adapter.readSource(value);
        if (!result.ok) { notify(ctx, "hippocampus: source read unavailable (backend did not answer)", "warning"); return; }
        if (!result.found) { notify(ctx, `hippocampus: no stored source for id ${value}`, "warning"); return; }
        pi.sendMessage({ customType: MEMORY_CUSTOM_TYPE, display: true, content: result.text,
          details: { origin: "source", sourceId: value } });
        return;
      }
      notify(ctx, `hippocampus: unknown argument — ${USAGE}`, "warning");
    },
  });
}
