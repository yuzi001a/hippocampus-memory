// Per-turn recall: at most one additive memory message per user turn.
//
// The latch is local runtime control, not an event: the key is
// `sessionId + turn`, so a tool-continuation step (which carries no
// user-sourced message) never triggers a second recall inside the same turn,
// and two sessions never share a latch.
//
// Every failure path returns `undefined`, which the plugin entry turns into
// the untouched downstream decision. A memory miss must never be able to stop
// a DSH turn.
import { randomUUID } from "node:crypto";

import { RECALL_SOURCE_KIND, userQueryOf } from "./mapping.js";

export const RECALL_NOTE = "[hippocampus memory — recalled context, not a new user fact]";

function messageIdOf(session, turn) { return `${sessionIdOf(session)}\u0000${String(turn)}`; }
function sessionIdOf(session) {
  const id = typeof session === "string" ? session : session?.id;
  return typeof id === "string" ? id : "";
}

export class RecallLatch {
  constructor({ warn = () => {} } = {}) {
    this.warn = warn;
    this.latched = new Set();
    // One warning per failure class for the life of the host session: a
    // memory backend that is down must not print once per turn.
    this.warned = new Set();
  }

  #warnOnce(class_, message) {
    if (this.warned.has(class_)) return;
    this.warned.add(class_);
    this.warn(message);
  }

  // Returns the additive UserMessage, or undefined for "inject nothing".
  async recallFor({ session, sessionId: explicitSessionId, turn, messages, client, memoryBudgetChars }) {
    const sessionId = explicitSessionId ?? sessionIdOf(session);
    if (!sessionId) return undefined;

    // Consume the latch for this (session, turn) whether or not the injection
    // ends up happening: a step that produced nothing must not be retried by
    // the next step of the same turn.
    const key = messageIdOf(sessionId, turn);
    if (this.latched.has(key)) return undefined;
    this.latched.add(key);

    // Only a step that actually claims user-sourced text is a user turn.
    const query = userQueryOf(messages);
    if (!query) return undefined;
    // No client means the runtime already reported the backend as unavailable
    // (a failed handshake, or no config at all). That class is warned once
    // there, so this path is silent — it must not double-warn.
    if (!client) return undefined;

    let result;
    try {
      result = await client.prefetch(query, sessionId);
    } catch {
      // The client already swallows transport errors; a third-party or
      // unexpected throw is contained here. No error text is ever printed.
      this.#warnOnce("recall-failed", "hippocampus: recall unavailable; continuing without memory context");
      return undefined;
    }
    if (!result?.ok) {
      this.#warnOnce("recall-failed", "hippocampus: recall unavailable; continuing without memory context");
      return undefined;
    }
    const block = typeof result.block === "string" ? result.block : "";
    if (!block.trim()) {
      this.#warnOnce("no-block", "hippocampus: no memory to recall for this turn");
      return undefined;
    }

    const budget = Number.isInteger(memoryBudgetChars) && memoryBudgetChars > 0 ? memoryBudgetChars : Infinity;
    if (block.length > budget) {
      // Refuse the whole injection: a substring would misrepresent what was
      // recalled, and a silently trimmed block is a lie about the memory.
      this.#warnOnce("over-budget",
        `hippocampus: recalled block exceeds the injection budget (${block.length} > ${budget} chars); skipped`);
      return undefined;
    }

    return {
      id: randomUUID(),
      role: "user",
      content: [{ type: "text", text: `${RECALL_NOTE}\n\n${block}` }],
      source: {
        kind: RECALL_SOURCE_KIND,
        form: "recall",
        version: 1,
        // The core's own source references, copied verbatim; never invented.
        references: Array.isArray(result.references) ? result.references : [],
      },
    };
  }
}
