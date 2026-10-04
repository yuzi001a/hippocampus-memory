# P0-A — late assistant QA derivation

Status: **implementation complete** on `fix/p0a-late-assistant-qa-derivation`
(base `f9b68e5`). Isolated probe 6/6 GREEN; formal pytest regression 7/7;
Y400 real-PostgreSQL E2E 36/36 PASS; focused regression base-differential clean.
Exact-head CI status: see the PR.

The single judgement this change is measured against:

> As long as an assistant delivery has been accepted by Hippocampus at the
> source layer, it must never be lost from the derived QA record merely
> because it arrived late.

## The problem: source accepted != QA derivation complete

The source layer (`conversation_stream`) and the QA derivation layer
(`qa_pairs` + pending) are two different lifecycles.  The source layer is
idempotent (F2: one row per `(host, session_id, event_id)`).  The QA layer
derives `(question, answer)` pairs by pairing a user turn with the assistant
deliveries that belong to it.

Before this change, that pairing could only happen against the *current
pending* pair.  Once the pending pair was flushed (watchdog), replaced by a
newer user turn, or lost to a process restart, a late assistant delivery had
nowhere to land:

- the source layer accepted it (its row exists, durable), but
- the QA layer silently dropped it: the user's question stayed in `qa_pairs`
  with an answer that would never receive the assistant's final report.

This is exactly the failure the judgement above forbids.

## Why the late assistant used to disappear

In the pre-P0-A assistant branch:

- a `duplicate` source event exited early (`continue`) — a replay never
  re-attempted derivation;
- with no matching pending, the event was skipped to avoid manufacturing
  orphan answers;
- out-of-order / cross-turn events were held (`hold_orphan`) with no durable
  retry path.

There was no code path that said: "this assistant event is already durable at
the source layer; find the user turn it belongs to and derive the QA state
from durable truth."

## New lifecycle

Two independent facts are now tracked separately, and neither is redefined as
the other:

1. **Source acceptance** (unchanged, F2): the event has a canonical
   `conversation_stream` row.  Receipts keep reporting this truthfully.
2. **Derivation completion** (new, P0-A): the event is folded into the QA row
   of its owning user turn, exactly once.

When an assistant event reaches the pairing logic and the normal pending-based
pairing cannot apply (pending flushed, replaced, or absent), and the event
carries a real (non-legacy) host and a host turn id, the core runs a
deterministic **late resolution** against durable truth:

- **Find the owning user turn U1** in `conversation_stream` by
  `(session_id, host, host_turn_id)` (role=user).  No U1 -> resolve nothing
  (keep legacy semantics; never guess a pairing).
- **Compute the QA identity** the same way the flush path does:
  `_qa_source_id(session, U1.turn_id, U1.event_id, host)`.
- **Row exists** -> atomically append the late content to `answer` and record
  the event id in `merged_event_ids`:
  `UPDATE ... WHERE id=%s AND NOT (merged_event_ids @> [msg_id])`.
  - appended -> `completed_late`; guard shows it was already there ->
    `already_merged`.
- **Row does not exist** (Q not flushed yet) -> rebuild the pair from the
  durable U1 row and submit it through the normal flush chain
  (`recovered_late`).  Idempotent via the content-sensitive flush job id and
  the `qa_pairs.source_id` unique constraint.

The receipt gains an optional `derivation` map (`{event_id: status}`) that
appears only when there is new information — the source-acceptance keys keep
their existing semantics.  Legacy hosts (empty/`legacy`) keep the historical
behaviour byte-for-byte.

## Identity and matching rule

- `conversation_stream.host_turn_id` (new column + partial index) records the
  host turn an event belongs to; it is written by the identity insert path
  only, never by the dedupe/legacy paths.
- `qa_pairs.merged_event_ids` (new jsonb ledger, default `[]`) records which
  assistant events have been folded into each QA row.
- Matching is purely durable: session + host + host turn id -> U1 -> QA
  identity.  No content heuristics, no LLM, no embedding.

## Replay and idempotency

- Correctness rests entirely on the durable queries plus the SQL guard.  The
  per-session in-memory memo only accelerates replays (a lost memo just means
  another durable query).
- Replaying a settled late event three times yields exactly one QA row, one
  merged membership and one occurrence of the content in the answer.
- An in-process replay of an already-processed snapshot is short-circuited by
  the per-session delta cursor (B01 I3/I6) — the receipt then reports
  `duplicate` and no new derivation; that is the designed no-op, not a loss.
- After a restart (cursor gone), a replay re-enters the late path and settles
  via the same durable resolution (`already_merged`).

## Restart behaviour

Durable markers and PG rows are the only state that matters.  After a process
restart with pending QA state lost, a late assistant event is still resolved:
`conversation_stream` holds U1, and either the QA row exists (merge) or is
rebuilt from U1 and flushed.  Verified by the probe S6 scenario and by the
Y400 E2E restart-free flow (the E2E drives the same durable resolution).

## Known boundary

- A standalone assistant delivery with no owning user turn (no U1) is **not**
  converted into a QA row — it stays held/raw-only by design.  That case
  belongs to P0-C, not this change.
- Legacy hosts keep their historical behaviour exactly (no late recovery).
- This change does not touch F2 canonical identity, the durable outbox /
  fence / crash-recovery contracts, or M03 correction suppression.

## Verification (what was actually run)

| Layer | What | Result |
|---|---|---|
| Isolated probe (fake PG + real core) | 6 scenarios incl. flush-boundary, next-user, multi-session, replay x3, restart | 6/6 GREEN (RED first) |
| Formal pytest regression | `tests/test_p0a_late_assistant_derivation.py` (7 cases) | 7/7 pass |
| Focused repo regression (10 files) | head vs base `f9b68e5` differential | 346/346 same; 1 known CRLF fail both; guard-noise errors both |
| Real PostgreSQL E2E (Y400) | real bootstrap CLI + disposable PG + isolated profile; scenarios A (flush + resend) and B (new pending) | 36/36 PASS, `production_mutation=NONE` |

Evidence lives in `evidence/p0a-late-assistant-derivation-20261004/`
(RED and GREEN probe runs are both preserved; the E2E reports and the
base-differential write-up are alongside).
