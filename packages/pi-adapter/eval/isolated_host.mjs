#!/usr/bin/env node
// B03 acceptance helper 1/2 — SIMULATED HOST / REAL BACKEND.
//
// NOT a real pi E2E: pi is never started and no model is called. A fake host
// drives the installed package's PUBLIC surface only — the default factory
// factory(pi), pi.on(event, fn(payload, ctx)),
// registerCommand('hippocampus', {handler(text, ctx)}), sendMessage, and ctx
// (ui.notify(message, level) / cwd / frozen sessionManager). The SDK binds ctx
// to hooks and to the command handler, never to the factory. The backend is the
// REAL unchanged B01 bridge.
//
// Honesty rules: no assistant reply is generated and called real; every entry
// is a stable synthetic NATIVE-SHAPED entry (id / parentId / ISO timestamp);
// the fixture fact and decision are openly synthetic and CLI-configurable;
// nothing is seeded into the backend beyond what the adapter's own hooks send.
// A command returns Promise<void> — its real output is what it sent through
// pi.sendMessage, so that is what is verified, against the exact source id
// parsed from the recalled block. The AUTOMATIC RECALL is a different channel
// entirely: the adapter returns it from before_agent_start as
// {message:{customType,display,content}} and never calls sendMessage for it, so
// it is validated from the hook result itself. Node's exit code reflects
// host-side observations only; b03_acceptance.py reads PostgreSQL back
// independently.
import { pathToFileURL } from "node:url";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

const argv = process.argv.slice(2);
const arg = (name, dflt = "") => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && i + 1 < argv.length ? String(argv[i + 1]) : dflt;
};
const ADAPTER = arg("adapter"), CONFIG = arg("config"), OUT = arg("out");
const KEY = arg("fixture-key", "synthetic B03 fact: the deploy window is on Thursdays");
const DECISION = arg("fixture-decision", "deploy window");
const SETTLE_MS = Number(arg("settle-ms", "500")) || 500;

const out = {
  helper: "b03-isolated-host",
  scope: "SIMULATED HOST / REAL BACKEND — not a real pi E2E, not a model-backed run",
  real_pi_started: false,
  adapter: ADAPTER, config: CONFIG, out: OUT,
  synthetic: { fixture_key: KEY, fixture_decision: DECISION, generated_assistant_reply: false },
  checks: {}, failures: [], order: [], sessions: {},
};
const setCheck = (name, value) => { out.checks[name] = value; return value; };
const fail = (name, detail) => { setCheck(name, false); out.failures.push({ check: name, detail }); };
const settle = (ms) => new Promise((r) => setTimeout(r, ms));
const short = (v) => String(v ?? "").slice(0, 300);
const write = (code) => {
  if (OUT) {
    try { mkdirSync(dirname(OUT), { recursive: true }); writeFileSync(OUT, JSON.stringify(out, null, 2), "utf8"); }
    catch (e) { process.stderr.write(`cannot write ${OUT}: ${e}\n`); }
  }
  process.stdout.write(JSON.stringify({
    helper: out.helper, checks: out.checks, failures: out.failures,
    source_reference: out.source_reference || null, exit: code,
  }) + "\n");
};

if (!ADAPTER || !CONFIG || !OUT) {
  fail("arguments", `--adapter, --config and --out are all required (got adapter=${ADAPTER ? "set" : "MISSING"}, config=${CONFIG ? "set" : "MISSING"}, out=${OUT ? "set" : "MISSING"})`);
  write(1);
  process.exit(1);
}
process.env.HIPPOCAMPUS_PI_CONFIG = CONFIG;   // explicit config file, never <cwd>/.pi
let mod;
try {
  mod = await import(pathToFileURL(ADAPTER).href);
} catch (e) {
  // An unresolvable adapter must be a STRUCTURED failure, never an unhandled
  // ESM rejection: the parent reads the report, not a stack trace.
  fail("adapter_import", `cannot import ${ADAPTER}: ${e?.code || e?.name || "Error"}: ${short(e?.message || e)}`);
  write(1);
  process.exit(1);
}
const factory = mod?.default;
if (typeof factory !== "function") {
  fail("adapter_export", `${ADAPTER} has no default export function (got ${typeof mod?.default})`);
  write(1);
  process.exit(1);
}
setCheck("adapter_export", true);

// ── synthetic native-shaped entries ────────────────────────────────────────
const T0 = Date.parse("2026-10-02T00:00:00.000Z");
const iso = (n) => new Date(T0 + n * 1000).toISOString();
const mid = (p, n) => `b03${p}-${String(n).padStart(4, "0")}`;
const msg = (id, parent, n, role, text) => ({
  type: "message", id, parentId: parent, timestamp: iso(n),
  message: { role, content: [{ type: "text", text }] },
});
const buildA = () => [
  msg(mid("A", 1), null, 1, "user", KEY),                                  // the fact
  msg(mid("A", 2), mid("A", 1), 2, "assistant", "synthetic assistant answer one"),
  msg(mid("A", 3), mid("A", 2), 3, "user", "synthetic unrelated second question"),
  msg(mid("A", 4), mid("A", 3), 4, "assistant", "synthetic assistant answer two"),
  { type: "custom", id: mid("A", 5), parentId: mid("A", 4), timestamp: iso(5),
    customType: "hippocampus-memory", content: "synthetic custom memory entry", display: false },
  { type: "message", id: mid("A", 6), parentId: mid("A", 5), timestamp: iso(6),
    message: { role: "system", content: [{ type: "text", text: "synthetic system wrapper" }] } },
  { type: "message", id: mid("A", 7), parentId: mid("A", 6), timestamp: iso(7),
    message: { role: "user", content: [{ type: "toolResult", toolUseId: "synthetic-1", content: "synthetic tool output" }] } },
  { type: "compaction", id: mid("A", 8), parentId: mid("A", 7), timestamp: iso(8),
    summary: "synthetic compaction summary" },
];
// Fixed expectations, mirrored by the driver's own fixed sets: only the four
// non-system message entries are recordable; the display:false custom entry, the
// system wrapper, the tool result and the compaction are excluded by contract.
const RECORDABLE = [mid("A", 1), mid("A", 2), mid("A", 3), mid("A", 4)];
const EXCLUDED = [mid("A", 5), mid("A", 6), mid("A", 7), mid("A", 8)];

// ── fake host activation (one per simulated session) ──────────────────────
// Every message the adapter sends is recorded, but NOTHING sent is treated as
// the automatic recall: that recall is a hook RETURN value (see the header),
// so its contract is checked from the session-B before_agent_start result
// below. A sendMessage-based guess would capture the source command's own
// visible output and fail a correct adapter.

function activate(sessionId, entries) {
  const handlers = new Map(), commands = new Map(), notices = [], sent = [];
  const session = { session_id: sessionId, notices, sent_messages: sent, commands: [], injections: [] };
  out.sessions[sessionId] = session;
  const sm = Object.freeze({
    getSessionId: () => sessionId,
    getEntries: () => entries.slice(),
    getBranch: () => entries.slice(),
    getHeader: () => ({ sessionId, createdAt: iso(0) }),
    getLeafId: () => (entries.at(-1) || {}).id || null,
    getEntry: (id) => entries.find((e) => e && e.id === id) || null,
    getLeafEntry: () => entries.at(-1) || null,
    getSessionFile: () => `${sessionId}.jsonl`,
  });
  const ctx = {
    ui: { notify: (message, level) => notices.push({ message: String(message), level: level ?? null }) },
    cwd: process.cwd(), sessionManager: sm,
  };
  const pi = {
    on: (event, fn) => { if (!handlers.has(event)) handlers.set(event, []); handlers.get(event).push(fn); },
    registerCommand: (name, def) => { commands.set(name, def); session.commands.push(name); },
    sendMessage: (m) => { sent.push(m); },   // recorded, never interpreted as a recall
  };
  // factory(pi) only — ctx is supplied to hooks and to the command handler.
  try { factory(pi); } catch (e) { fail("activate_" + sessionId, `factory threw: ${e?.name || "Error"}: ${short(e?.message || e)}`); }
  if (!handlers.size) fail("activate_" + sessionId, "no hooks were registered through pi.on()");
  return {
    session, ctx, entries,
    command(name) { return commands.get(name); },
    async fire(event, payload) {
      out.order.push(`${sessionId}:${event}`);
      const results = [];
      for (const fn of handlers.get(event) || []) results.push(await fn(payload, ctx));
      return results;
    },
  };
}

const flat = (v) => JSON.stringify(v ?? null);
const firstSourceId = (block) => {
  const m = /(^|\n)\s*-\s*([A-Za-z0-9_.:\-]{1,120})\s*\|/.exec(String(block ?? ""));
  return m ? m[2] : "";
};

// ── session A: record, flush, drain ───────────────────────────────────────
const A = activate("b03-session-a", []);
await A.fire("session_start", { sessionId: "b03-session-a", cwd: process.cwd() });
const inputA = { prompt: KEY };
const inputSnapshot = flat(inputA);
const rA = (await A.fire("input", inputA))[0];
out.sessions["b03-session-a"].input_result = rA ?? null;
if (flat(inputA) !== inputSnapshot) fail("no_input_mutation", `input was mutated in place: ${flat(inputA)}`);
else setCheck("no_input_mutation", true);
const startA = (await A.fire("before_agent_start", { prompt: KEY }))[0];
A.session.injections.push({ phase: "before_agent_start", result: startA ?? null });
const jsonA = flat(startA);
if (jsonA !== null && /systemPrompt/.test(jsonA)) fail("no_system_prompt_replacement", `before_agent_start returned a systemPrompt: ${jsonA.slice(0, 400)}`);
else setCheck("no_system_prompt_replacement", true);
// persistence happens here: entries reach the sessionManager BEFORE agent_end
for (const e of buildA()) { A.entries.push(e); await settle(5); }
// the auto-recalled custom entry carries display:false and is never recordable
for (const e of A.entries.filter((x) => x.type === "message" && x.message?.role !== "system")) {
  await A.fire("message_end", { message: e.message, entry: undefined });
}
await A.fire("agent_end", { sessionId: "b03-session-a" });
await A.fire("session_shutdown", { sessionId: "b03-session-a" });
await settle(SETTLE_MS);
out.sessions["b03-session-a"].persisted_ids = A.entries.map((e) => e.id);
out.sessions["b03-session-a"].persisted_before_agent_end = true;

// ── session B: new session, recall, latch ─────────────────────────────────
const B = activate("b03-session-b", []);
await B.fire("session_start", { sessionId: "b03-session-b", cwd: process.cwd() });
await B.fire("input", { prompt: `synthetic B03 recall probe: ${DECISION}` });
const b1 = (await B.fire("before_agent_start", { prompt: `synthetic B03 recall probe: ${DECISION}` }))[0];
const b2 = (await B.fire("before_agent_start", { prompt: `synthetic B03 recall probe: ${DECISION}` }))[0];
B.session.injections.push({ phase: "before_agent_start#1", result: b1 ?? null },
  { phase: "before_agent_start#2", result: b2 ?? null });
const jsonB1 = flat(b1);
if (jsonB1 !== null && /systemPrompt/.test(jsonB1)) fail("no_system_prompt_replacement_b", jsonB1.slice(0, 400));
const text1 = String(b1?.message?.content ?? ""), text2 = String(b2?.message?.content ?? "");
const sid = firstSourceId(text1);
out.source_reference = sid;
out.recall_scope = text1.includes(KEY)
  ? "fixture fact present in the returned block; keyword/structured path only — NO model, NO external endpoint, NOT semantic E2E"
  : "fixture fact ABSENT from the returned block";
if (!text1.includes(KEY)) fail("b_contains_fixture_fact", `session-B block does not contain the fixture fact ${JSON.stringify(KEY)}; block=${text1.slice(0, 400)}`);
if (!sid) fail("b_contains_source_reference", "no '- <source_id> | ...' reference found in the session-B block; cannot trace the exact core reference");
else if (!text1.includes(sid)) fail("b_contains_source_reference", `parsed source id ${sid} is not present in its own block`);
// The latch is proven by ASYMMETRY, not equality: a correct latch returns a
// block on the first call and NOTHING on a repeated before_agent_start without
// a new `input`. Comparing raw results would flag that asymmetry, and checking
// only the first call would reward an adapter that prefetches again.
if (text1 && !text2) setCheck("recall_latch_second_call", "first before_agent_start returned a block; the repeated call returned none (no second prefetch at the host boundary)");
else if (text1 && text2) fail("recall_latch_second_call", "the repeated before_agent_start returned ANOTHER block: a second prefetch happened without a new input event");
else fail("recall_latch_second_call", "the first session-B before_agent_start returned no block, so the latch cannot be assessed");
if (b1 && b1.message && !text1) fail("additive_custom_message", "before_agent_start returned an empty additive message");
else setCheck("additive_custom_message", true);

// The AUTOMATIC RECALL, read off the session-B before_agent_start RETURN value
// (b1.message) — not off anything the adapter sent. That is where the product
// puts it: before_agent_start returns {message:{customType,display,content}} and
// never calls pi.sendMessage for the recall; sendMessage belongs to the source
// command alone. Checking the sent stream here would capture the command's own
// visible output and fail a correct adapter.
//
// All three parts of the contract are exact, because each one is a distinct
// defect: a wrong customType is a mislabelled block the host will not recognise
// as memory, display:true is a user-facing DUPLICATE of the block already
// injected into the turn, and empty content is a silently dropped recall.
const recall = (b1 && typeof b1 === "object") ? b1.message : null;
const recallType = recall?.customType ?? null;
const recallDisplay = recall?.display ?? null;
const recallContent = String(recall?.content ?? "");
out.automatic_recall = {
  channel: "before_agent_start return value (pi.sendMessage is the source command's channel only)",
  customType: recallType, display: recallDisplay,
  content_chars: recallContent.length, contains_fixture_fact: recallContent.includes(KEY),
  content_preview: recallContent.slice(0, 200),
};
if (!recall || typeof recall !== "object") {
  fail("automatic_recall", "the session-B before_agent_start returned no {message:{...}} object; "
       + "the automatic recall must arrive as a hook RETURN value, not as a sent message");
} else if (recallType !== "hippocampus-memory") {
  fail("automatic_recall", `the returned automatic recall has customType `
       + `${JSON.stringify(recallType)} instead of the exact "hippocampus-memory"; `
       + `a mislabelled block is not recognised as memory by the host`);
} else if (recallDisplay !== false) {
  fail("automatic_recall", `the returned automatic recall has display `
       + `${JSON.stringify(recallDisplay)} instead of the exact false; it must stay hidden `
       + `because it is already injected into the turn — display:true duplicates it`);
} else if (!recallContent.trim()) {
  fail("automatic_recall", "the returned automatic recall carries empty content; "
       + "a correctly shaped message with no text is a dropped recall, not a pass");
} else if (!recallContent.includes(KEY)) {
  fail("automatic_recall", `the returned automatic recall does not contain this run's fixture `
       + `fact ${JSON.stringify(KEY)}; block=${recallContent.slice(0, 400)}`);
} else {
  setCheck("automatic_recall", `the before_agent_start return value was the exact `
    + `"hippocampus-memory" recall with display:false and ${recallContent.length} chars `
    + `carrying this run's fixture fact`);
}

// ── trace: the source command's own visible custom output ─────────────────
const cmd = B.command("hippocampus");
if (!cmd || typeof cmd.handler !== "function") {
  fail("command_registered", "registerCommand('hippocampus', {handler}) did not yield a callable handler");
} else {
  const before = B.session.sent_messages.length;
  let thrown = null;
  try { await cmd.handler(`source ${sid}`, B.ctx); }   // handler(string, ctx)
  catch (e) { thrown = `${e?.name || "Error"}: ${short(e?.message || e)}`; }
  // Only messages emitted by THIS invocation count: anything the adapter sent
  // earlier (e.g. an auto-recall notice, or a visible note from a prior command)
  // is not the command's output and is deliberately excluded by the slice.
  const fresh = B.session.sent_messages.slice(before);
  // The EXACT rule: the invocation must emit exactly ONE custom message, it must
  // be visible, and it must be the source result. A second custom message is a
  // failure even when exactly one of them parses as a source read — "one parses"
  // is not a contract, and a silent id echo beside a real read is still a
  // duplicated trace. The automatic_recall check above is independent of this
  // slice: the recall never travels this channel at all.
  const custom = fresh.filter((m) => m && (m.type === "custom" || typeof m.customType === "string"));
  const visible = custom.filter((m) => m.display === true);
  const record = {
    attempted_identifier: sid, handler_threw: thrown,
    sent_during_invocation: fresh.length, custom_messages: custom.length,
    visible_custom_messages: visible.length, source_reads: visible.length,
    prior_custom_messages_ignored: B.session.sent_messages.slice(0, before)
      .filter((m) => m && (m.type === "custom" || typeof m.customType === "string")).length,
    raw: null,
    note: "the identifier is exactly the one parsed from the returned block; no other row was substituted",
  };
  B.session.source_trace = record;
  if (thrown) {
    fail("source_trace", `the source command handler threw for ${JSON.stringify(`source ${sid}`)}: ${thrown}`);
  } else if (!sid) {
    fail("source_trace", "no source id was parsed from the recalled block, so there is nothing to trace");
  } else if (custom.length !== 1) {
    // Exactly one, not "at least one that parses": an unsupported command, a
    // swallowed lookup, a duplicated trace or a silent id echo all land here.
    fail("source_trace", `the source command must emit exactly one custom message total for `
      + `${JSON.stringify(sid)}; got ${custom.length} (${visible.length} visible of `
      + `${fresh.length} total sent) — extra custom output is a duplicate trace even if `
      + `only one of them parses as a source result`);
  } else if (visible.length !== 1) {
    fail("source_trace", `the single custom message from the source command for `
      + `${JSON.stringify(sid)} has display ${JSON.stringify(custom[0].display ?? null)} instead of true; `
      + `the trace the user asked for must be visible`);
  } else {
    const m = visible[0];
    const raw = typeof m.content === "string" ? m.content : JSON.stringify(m.content ?? m.message ?? "");
    record.raw = raw.slice(0, 600);
    let body = null;
    try { body = JSON.parse(raw); } catch { body = null; }
    if (!body || typeof body !== "object") {
      fail("source_trace", `the visible custom output is not JSON: ${record.raw}`);
    } else if (body.success !== true) {
      fail("source_trace", `the core did not report success:true for ${JSON.stringify(sid)} (got ${JSON.stringify(body.success)}, error=${short(body.error)}); reported as a failure rather than replaced by an unrelated id`);
    } else if (body.source_id !== sid) {
      fail("source_trace", `the output carries source_id ${JSON.stringify(body.source_id)} but the exact one attempted was ${JSON.stringify(sid)}; another row's result is not a success`);
    } else if (!String(body.content ?? "").trim()) {
      fail("source_trace", `the output for ${JSON.stringify(sid)} has empty content; an id echo with no text is not a pass`);
    } else if (!String(body.content).includes(KEY)) {
      fail("source_trace", `the content for ${JSON.stringify(sid)} does not contain this source's fixture fact, so it came from a different row: ${short(body.content)}`);
    } else {
      setCheck("source_trace", `the exact source reference ${sid} resolved to its own non-empty content carrying this source's fixture fact`);
    }
  }
}
out.expected = {
  host: "pi",
  session_a: { id: "b03-session-a", recordable_event_ids: RECORDABLE, must_be_absent_event_ids: EXCLUDED },
  session_b: { id: "b03-session-b", source_reference: sid },
  fixture_key: KEY, fixture_decision: DECISION,
};
write(out.failures.length ? 1 : 0);
process.exit(out.failures.length ? 1 : 0);
