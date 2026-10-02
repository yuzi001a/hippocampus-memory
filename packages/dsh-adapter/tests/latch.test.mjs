// UNIT tests only (§3). The bridge is an in-process stub: no socket, no
// child process, no config file, no database. What is under test is the
// once-per-user-turn latch and the additive message it produces.
import { test } from "node:test";
import assert from "node:assert/strict";

import { RecallLatch, RECALL_NOTE } from "../src/recall.js";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const BLOCK = "topic: retention policy\n- 30 days (source: sess-0)";
const REFERENCES = [{ sourceId: "qa_sync/s0/t1/m1", kind: "qa", label: "retention policy" }];

// `references` needs a sentinel rather than a `undefined` default: a JS
// default parameter fires on an explicit `undefined`, which would silently
// stand in the very "response carried no references" case under test.
const ABSENT = Symbol("absent");
function fakeClient(block = BLOCK, { ok = true, references = REFERENCES } = {}) {
  const calls = [];
  return {
    calls,
    async prefetch(query, sessionId) {
      calls.push({ query, sessionId });
      return ok
        ? { ok: true, block, references: references === ABSENT ? undefined : references }
        : { ok: false, block: "" };
    },
  };
}

function claimed(...texts) {
  return texts.map((text, i) => ({
    id: `claimed-${i}`, role: "user", content: text, source: { kind: "user" },
  }));
}

const toolContinuation = [
  { id: "c-0", role: "user", content: "tool output", source: { kind: "tool-result" } },
];

// ── 1. one injection per user turn ────────────────────────────────────────
test("at most one injection per user turn: a second pre-step in the same turn injects nothing", async () => {
  const warnings = [];
  const latch = new RecallLatch({ warn: (m) => warnings.push(m) });
  const client = fakeClient();

  const first = await latch.recallFor({
    sessionId: "sess-A", turn: 1, messages: claimed("what is the retention policy?"),
    client, memoryBudgetChars: 8000,
  });
  const second = await latch.recallFor({
    sessionId: "sess-A", turn: 1, messages: claimed("what is the retention policy?"),
    client, memoryBudgetChars: 8000,
  });

  assert.ok(first, "the first pre-step of the turn injects");
  assert.equal(second, undefined, "the same turn never injects twice");
  assert.equal(client.calls.length, 1, "no second prefetch is spent");
  assert.deepEqual(warnings, []);
});

// ── 2. a new turn injects again ───────────────────────────────────────────
test("a new turn in the same session injects again", async () => {
  const latch = new RecallLatch();
  const client = fakeClient();

  const first = await latch.recallFor({
    sessionId: "sess-A", turn: 1, messages: claimed("first question"), client, memoryBudgetChars: 8000,
  });
  const second = await latch.recallFor({
    sessionId: "sess-A", turn: 2, messages: claimed("second question"), client, memoryBudgetChars: 8000,
  });

  assert.ok(first && second);
  assert.notEqual(first.id, second.id, "each injection is its own message");
  assert.deepEqual(client.calls.map((c) => c.query), ["first question", "second question"]);
});

// ── 3. a tool-continuation step never triggers recall ─────────────────────
test("a step claiming no user-sourced message never injects, even on a fresh turn", async () => {
  const latch = new RecallLatch();
  const client = fakeClient();

  assert.equal(await latch.recallFor({
    sessionId: "sess-A", turn: 1, messages: toolContinuation, client, memoryBudgetChars: 8000,
  }), undefined);
  assert.equal(await latch.recallFor({
    sessionId: "sess-A", turn: 2, messages: [], client, memoryBudgetChars: 8000,
  }), undefined);
  assert.equal(await latch.recallFor({
    sessionId: "sess-A", turn: 3, messages: undefined, client, memoryBudgetChars: 8000,
  }), undefined);
  assert.equal(client.calls.length, 0, "no query is ever built from a non-user step");

  // A blank user message is not a query either.
  assert.equal(await latch.recallFor({
    sessionId: "sess-A", turn: 4, messages: claimed("   \n "), client, memoryBudgetChars: 8000,
  }), undefined);
  assert.equal(client.calls.length, 0);
});

// ── 4. two sessions keep independent latches ──────────────────────────────
test("two sessions keep independent latches", async () => {
  const latch = new RecallLatch();
  const client = fakeClient();

  const a1 = await latch.recallFor({
    sessionId: "sess-A", turn: 7, messages: claimed("a question"), client, memoryBudgetChars: 8000,
  });
  const b1 = await latch.recallFor({
    sessionId: "sess-B", turn: 7, messages: claimed("b question"), client, memoryBudgetChars: 8000,
  });
  const a2 = await latch.recallFor({
    sessionId: "sess-A", turn: 7, messages: claimed("a question"), client, memoryBudgetChars: 8000,
  });

  assert.ok(a1 && b1, "the same turn number in another session is a separate turn");
  assert.equal(a2, undefined, "sess-A turn 7 is still latched");
  assert.deepEqual(client.calls.map((c) => c.sessionId), ["sess-A", "sess-B"]);
});

// ── message shape: additive, producer-owned source, honest references ─────
test("the injected message is an additive user message with a hippocampus recall source", async () => {
  const latch = new RecallLatch();
  const message = await latch.recallFor({
    sessionId: "sess-A", turn: 1, messages: claimed("what is the retention policy?"),
    client: fakeClient(), memoryBudgetChars: 8000,
  });

  assert.match(message.id, UUID_RE, "id is a plain UUID, assigned fresh per injection");
  assert.equal(message.role, "user");
  assert.equal(message.source.kind, "hippocampus");
  assert.equal(message.source.form, "recall");
  assert.equal(message.source.version, 1);
  assert.deepEqual(message.source.references, REFERENCES, "the core's own references, copied verbatim");
  assert.deepEqual(message.content, [{ type: "text", text: `${RECALL_NOTE}\n\n${BLOCK}` }]);
  assert.equal(
    RECALL_NOTE,
    "[hippocampus memory — recalled context, not a new user fact]",
  );
});

test("a prefetch response without references injects an empty list, never an invented one", async () => {
  const latch = new RecallLatch();
  const message = await latch.recallFor({
    sessionId: "sess-A", turn: 1, messages: claimed("q"),
    client: fakeClient(BLOCK, { ok: true, references: ABSENT }), memoryBudgetChars: 8000,
  });
  assert.deepEqual(message.source.references, []);
});

// ── failure and budget: fail-open, warn once, never a substring ───────────
test("an oversized block skips the whole injection with one warning and no substring", async () => {
  const warnings = [];
  const latch = new RecallLatch({ warn: (m) => warnings.push(m) });
  const block = "x".repeat(5001);
  const client = fakeClient(block);

  const first = await latch.recallFor({
    sessionId: "sess-A", turn: 1, messages: claimed("q"), client, memoryBudgetChars: 5000,
  });
  const second = await latch.recallFor({
    sessionId: "sess-A", turn: 2, messages: claimed("q"), client, memoryBudgetChars: 5000,
  });

  assert.equal(first, undefined);
  assert.equal(second, undefined);
  assert.equal(warnings.length, 1, "one warning for the budget failure class, not one per turn");
  assert.ok(!/x{100,}/.test(warnings[0]), "the warning carries no payload of the block");
});

test("every failure class fails open with at most one warning per class", async () => {
  const warnings = [];
  const latch = new RecallLatch({ warn: (m) => warnings.push(m) });

  // bridge down / no answer
  for (const turn of [1, 2, 3]) {
    assert.equal(await latch.recallFor({
      sessionId: "sess-A", turn, messages: claimed("q"),
      client: fakeClient(BLOCK, { ok: false }), memoryBudgetChars: 8000,
    }), undefined);
  }
  // empty block
  assert.equal(await latch.recallFor({
    sessionId: "sess-A", turn: 4, messages: claimed("q"),
    client: fakeClient("   \n  "), memoryBudgetChars: 8000,
  }), undefined);
  // no client at all (unconfigured / bridge never became ready)
  assert.equal(await latch.recallFor({
    sessionId: "sess-A", turn: 5, messages: claimed("q"), client: null, memoryBudgetChars: 8000,
  }), undefined);
  // a throwing client must not escape into the host
  assert.equal(await latch.recallFor({
    sessionId: "sess-A", turn: 6, messages: claimed("q"),
    client: { prefetch: async () => { throw new Error("connection reset"); } },
    memoryBudgetChars: 8000,
  }), undefined);

  assert.equal(warnings.length, 2, "one warning for 'recall failed', one for 'no block'; never per turn");
  for (const message of warnings) {
    assert.ok(!/connection reset/.test(message), "no raw server or transport error is ever printed");
  }
});

test("a latch that failed still consumes its turn: no retry storm inside one turn", async () => {
  const warnings = [];
  const latch = new RecallLatch({ warn: (m) => warnings.push(m) });
  const client = fakeClient(BLOCK, { ok: false });

  assert.equal(await latch.recallFor({
    sessionId: "sess-A", turn: 1, messages: claimed("q"), client, memoryBudgetChars: 8000,
  }), undefined);
  assert.equal(await latch.recallFor({
    sessionId: "sess-A", turn: 1, messages: claimed("q"), client, memoryBudgetChars: 8000,
  }), undefined);
  assert.equal(client.calls.length, 1);
  assert.equal(warnings.length, 1);
});
