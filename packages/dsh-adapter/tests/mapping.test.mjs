// UNIT tests only (§3 of the B04 spec).
// Nothing here opens a socket, spawns a process, reads a real config file or
// touches a database: every session, event and message is an in-process
// fixture shaped exactly like the upstream 0.2.0-rc.2 session envelope.
import { test } from "node:test";
import assert from "node:assert/strict";

import { textOf, mapSessionEvent, userQueryOf } from "../src/mapping.js";
import { HOST_ID } from "../src/bridge.js";

const T_USER = 1_700_000_000_000;
const T_ASSISTANT = 1_700_000_001_000;

// Upstream `user/message` data IS the UserMessage (session/types.ts:281-427).
function userEvent({ id, content, source = { kind: "user" }, time = T_USER } = {}) {
  return { type: "user/message", seq: 1, time, data: { id, role: "user", content, source } };
}

// Upstream `assistant/message` data is { turn, step, message, stream, usage?, interrupted? }.
function assistantEvent({ id, content = "the policy is 30 days", turn = 3, interrupted, time = T_ASSISTANT } = {}) {
  const data = { turn, step: 1, stream: "chat", message: { id, role: "assistant", content } };
  if (interrupted !== undefined) data.interrupted = interrupted;
  return { type: "assistant/message", seq: 2, time, data };
}

const session = { id: "sess-1" };

// ── 1. user/message maps when the source is the user ──────────────────────
test("mapSessionEvent maps a user-sourced user/message to a canonical B01 event", () => {
  const mapped = mapSessionEvent(
    session,
    userEvent({ id: "m-user-1", content: "what is the retention policy?" }),
    { turn: 4 },
  );
  assert.equal(mapped.host, HOST_ID);
  assert.equal(HOST_ID, "dsh");
  assert.equal(mapped.session_id, "sess-1");
  assert.equal(mapped.event_id, "m-user-1");
  assert.equal(mapped.role, "user");
  assert.equal(mapped.content, "what is the retention policy?");
  assert.equal(mapped.timestamp, new Date(T_USER).toISOString());
  assert.equal(mapped.turn_id, 4);
});

// ── 2. self-recapture exclusion ───────────────────────────────────────────
test("mapSessionEvent excludes our own injected memory message (source.kind hippocampus)", () => {
  const injected = userEvent({
    id: "m-mem-1",
    content: [{ type: "text", text: "[hippocampus memory — recalled context, not a new user fact]\n\nblock" }],
    source: { kind: "hippocampus", form: "recall", version: 1, references: [] },
  });
  assert.equal(mapSessionEvent(session, injected, { turn: 4 }), null);
  // A non-user, non-hippocampus producer source is not ours to record either.
  assert.equal(
    mapSessionEvent(session, userEvent({ id: "m-rt", content: "ctx", source: { kind: "runtime-context" } })),
    null,
  );
});

// ── 3. assistant/message maps; interrupted is not an answer ───────────────
test("mapSessionEvent maps assistant/message and drops an interrupted one", () => {
  const mapped = mapSessionEvent(session, assistantEvent({ id: "m-asst-1" }), { turn: 9 });
  assert.equal(mapped.event_id, "m-asst-1");
  assert.equal(mapped.role, "assistant");
  assert.equal(mapped.content, "the policy is 30 days");
  assert.equal(mapped.timestamp, new Date(T_ASSISTANT).toISOString());
  // event.data.turn wins over the caller's turn attribution.
  assert.equal(mapped.turn_id, 3);

  assert.equal(mapSessionEvent(session, assistantEvent({ id: "m-asst-2", interrupted: true })), null);
});

// ── 4. every non-committed event type is skipped ──────────────────────────
test("mapSessionEvent returns null for every event that commits no message", () => {
  const skipped = [
    { type: "assistant/attempt", data: { turn: 1, step: 1, stream: "chat" } },
    { type: "tool/call", data: { id: "t1", name: "read", args: {} } },
    { type: "tool/result", data: { id: "t1", result: "ok" } },
    { type: "system/message", data: { id: "m-sys", role: "system", content: "you are..." } },
    { type: "developer/message", data: { id: "m-dev", content: "tool removed" } },
    { type: "turn/start", data: { turn: 1 } },
    { type: "turn/end", data: { turn: 1 } },
    { type: "step/start", data: { turn: 1, step: 1 } },
    { type: "step/end", data: { turn: 1, step: 1 } },
    { type: "agent/assistant-stream", data: { delta: "tok" } },
  ];
  for (const event of skipped) {
    assert.equal(mapSessionEvent(session, event, { turn: 1 }), null, event.type);
  }
});

// ── 5. unknown future types fall through, never throw ─────────────────────
test("mapSessionEvent falls through on unknown and malformed events without throwing", () => {
  const unknown = [
    { type: "some/future-event", data: { id: "x", content: "x" } },
    { type: "", data: {} },
    {},
    { type: "user/message" },
    { type: "assistant/message", data: {} },
    null,
    undefined,
  ];
  for (const event of unknown) {
    assert.equal(mapSessionEvent(session, event, { turn: 1 }), null, String(event && event.type));
  }
  assert.equal(mapSessionEvent(null, userEvent({ id: "m1", content: "x" })), null);
});

// ── 6. text extraction: verbatim order, non-text dropped, empty → null ────
test("textOf keeps text blocks verbatim in order and drops every non-text block", () => {
  assert.equal(textOf("plain text"), "plain text");
  assert.equal(textOf([
    { type: "text", text: "alpha " },
    { type: "image", data: "base64..." },
    { type: "thinking", thinking: "hidden" },
    { type: "toolUse", id: "t1", name: "read", input: { path: "x" } },
    { type: "text", text: "omega" },
  ]), "alpha omega");
  assert.equal(textOf([{ type: "text", text: 42 }]), "");
  assert.equal(textOf([{ type: "image", data: "x" }]), "");
  assert.equal(textOf([]), "");
  assert.equal(textOf(undefined), "");
  assert.equal(textOf({ type: "text" }), "");
});

test("a message whose extracted text is empty maps to null", () => {
  assert.equal(mapSessionEvent(session, userEvent({ id: "m-empty", content: [] })), null);
  assert.equal(mapSessionEvent(session, userEvent({ id: "m-empty2", content: [{ type: "image", data: "x" }] })), null);
  assert.equal(mapSessionEvent(session, userEvent({ id: "m-empty3", content: "" })), null);
  assert.equal(mapSessionEvent(session, assistantEvent({ id: "m-empty4", content: [] })), null);
});

// ── 7. identity is never invented ────────────────────────────────────────
test("a missing session id or message id maps to null instead of an invented identity", () => {
  const user = userEvent({ id: "m-user-1", content: "hello" });
  const asst = assistantEvent({ id: "m-asst-1" });
  assert.equal(mapSessionEvent({ id: "" }, user), null);
  assert.equal(mapSessionEvent({}, user), null);
  assert.equal(mapSessionEvent(null, user), null);
  assert.equal(mapSessionEvent({ id: 42 }, user), null);
  assert.equal(mapSessionEvent(session, userEvent({ id: "", content: "hello" })), null);
  assert.equal(mapSessionEvent(session, userEvent({ content: "hello" })), null);
  assert.equal(mapSessionEvent(session, assistantEvent({ id: "" })), null);
  assert.equal(mapSessionEvent(session, assistantEvent({ id: null })), null);
});

// ── 8. query extraction from the claimed messages ─────────────────────────
test("userQueryOf joins user-sourced claimed messages and ignores every other source", () => {
  assert.equal(
    userQueryOf([
      { id: "a", content: "first question", source: { kind: "user" } },
      { id: "b", content: [{ type: "text", text: "second question" }], source: { kind: "user" } },
      { id: "c", content: "tool result noise", source: { kind: "hippocampus", form: "recall" } },
      { id: "d", content: "ctx", source: { kind: "runtime-context" } },
    ]),
    "first question\n\nsecond question",
  );
  // Nothing user-sourced in this step (a tool continuation) → no query at all.
  assert.equal(userQueryOf([{ id: "c", content: "noise", source: { kind: "runtime-context" } }]), "");
  assert.equal(userQueryOf([]), "");
  assert.equal(userQueryOf(undefined), "");
  // Whitespace-only user text is not a query.
  assert.equal(userQueryOf([{ id: "a", content: "   \n  ", source: { kind: "user" } }]), "");
  // Surrounding whitespace is trimmed after the join.
  assert.equal(userQueryOf([{ id: "a", content: "  hi  ", source: { kind: "user" } }]), "hi");
});
