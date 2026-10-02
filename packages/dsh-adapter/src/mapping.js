// DSH session/event -> B01 canonical event, plus the two text extractors.
//
// Identity discipline: the canonical `event_id` is the DSH message's own id
// and `session_id` is the session's own id. Nothing here hashes, derives,
// shortens or otherwise invents an identity — a missing id yields no event.
//
// Every function is total: an unknown, future or malformed event type falls
// through to `null` instead of throwing, because a capture listener that
// throws would take the host's session/append down with it.
import { HOST_ID } from "./bridge.js";

// The producer kind this adapter owns. It is also the self-recapture
// exclusion: our own injected recall message is persisted as a `user/message`
// whose `source.kind` is exactly this, and must never be ingested again.
export const RECALL_SOURCE_KIND = "hippocampus";

// Text blocks only, in order, joined with "". Image / thinking / tool blocks
// are a documented first-version limitation: they are dropped, never converted
// into invented prose.
export function textOf(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((block) => block && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("");
}

// `time` is unix epoch milliseconds (session/types.ts:493-516). Rendered as
// ISO-8601; anything that is not a finite number is omitted, never guessed.
function timestampOf(time) {
  if (typeof time !== "number" || !Number.isFinite(time)) return undefined;
  try {
    return new Date(time).toISOString();
  } catch {
    return undefined;
  }
}

function sessionIdOf(session) {
  const id = session?.id;
  return typeof id === "string" && id ? id : "";
}

// Only these two event types commit a durable surface message
// (session/types.ts:281-427). Everything else — attempts, tool wrappers, the
// system prompt, developer tool add/remove, turn/step coordinates, streaming
// frames, and any future type — is structurally not something we record.
export function mapSessionEvent(session, event, { turn } = {}) {
  const sessionId = sessionIdOf(session);
  if (!sessionId) return null;
  const type = event?.type;
  if (type !== "user/message" && type !== "assistant/message") return null;
  const data = event?.data;
  if (!data || typeof data !== "object") return null;

  let id;
  let role;
  let content;
  let turnId;
  if (type === "user/message") {
    // `user/message` data IS the UserMessage. Only a message the human
    // actually sent is a recordable user fact; every other producer kind
    // (our recall, runtime context, a host tool wrapper) is not.
    if (data.source?.kind !== "user") return null;
    id = data.id;
    role = "user";
    content = textOf(data.content);
    turnId = turn;
  } else {
    // An interrupted turn is not an answer.
    if (data.interrupted === true) return null;
    id = data.message?.id;
    role = "assistant";
    content = textOf(data.message?.content);
    // The message's own turn beats the caller's attribution.
    turnId = data.turn === undefined || data.turn === null ? turn : data.turn;
  }

  if (typeof id !== "string" || !id) return null;
  if (!content) return null;

  const event_ = {
    host: HOST_ID,
    session_id: sessionId,
    event_id: id,
    role,
    content,
  };
  const timestamp = timestampOf(event?.time);
  if (timestamp !== undefined) event_.timestamp = timestamp;
  if (turnId !== undefined && turnId !== null && turnId !== "") event_.turn_id = turnId;
  return event_;
}

// The query for one recall: the step's user-sourced text only. A
// tool-continuation step carries tool or context messages, so it produces ""
// and can never trigger a second recall inside the same turn.
export function userQueryOf(messages) {
  if (!Array.isArray(messages)) return "";
  const parts = [];
  for (const message of messages) {
    if (message?.source?.kind !== "user") continue;
    const text = textOf(message.content).trim();
    if (text) parts.push(text);
  }
  return parts.join("\n\n").trim();
}

// Turn attribution: `turn/start` and `step/start` carry the coordinates the
// message envelopes do not. Only what the host actually reported is kept.
export class TurnTracker {
  constructor() { this.turns = new Map(); }

  observe(session, event) {
    const sessionId = sessionIdOf(session);
    if (!sessionId) return;
    const type = event?.type;
    if (type === "turn/start") {
      const turn = event?.data?.turn;
      if (typeof turn === "number") this.turns.set(sessionId, turn);
    } else if (type === "turn/end") {
      const turn = event?.data?.turn;
      if (this.turns.get(sessionId) === turn) this.turns.delete(sessionId);
    }
  }

  turnOf(session) { return this.turns.get(sessionIdOf(session)); }

  forget(session) { this.turns.delete(sessionIdOf(session)); }
}
