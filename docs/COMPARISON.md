# Choosing a memory approach

Hippocampus is not the only way to give an agent continuity across sessions. This page is a practical comparison of several approaches a Hermes or coding-agent user is likely to consider.

The goal is not to declare a winner. These systems make different trade-offs, and Hippocampus is still a public alpha.

## Short version

| Approach | Good fit | Main trade-off |
|---|---|---|
| Hermes built-in `MEMORY.md` / `USER.md` | A small set of critical facts that should always be available | Intentionally bounded; not a general long-horizon memory engine |
| Raw session/history search | Finding the exact conversation where something happened | Search does not itself decide what should become durable memory |
| Mem0 | A general-purpose memory layer with broad SDK, server, cloud, and framework integration options | A larger product surface and a different source/derivation model from Hippocampus |
| Hindsight | Structured retain/recall/reflect workflows with semantic, keyword, graph, and temporal retrieval | More machinery and a different ingestion model; raw input is transformed into its memory model |
| Hippocampus | Local-first source durability, explicit canonical memory, and experimentation around memory formation and continuity | v0.1-alpha is still developer-oriented; several automatic-memory paths are experimental |

## Hermes built-in memory

Hermes already has useful persistent memory. `MEMORY.md` stores the agent's notes and `USER.md` stores a compact user profile. Hermes keeps both bounded and injects them into the system prompt at session start. It also has `session_search` for looking back through past sessions on demand.

This is a very good default when the amount of durable information is small and curated. It is simple, transparent, and has almost no infrastructure burden.

Hippocampus becomes interesting when you want to separate source history from derived memory, keep a larger durable source stream, rebuild derived state, or experiment with memory formation beyond a small always-in-context file.

Hermes documentation: <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md>

## Mem0

Mem0 positions itself as a general memory layer for AI agents and applications. It can be used as a library, as a self-hosted server, or through its managed platform. Its current public documentation describes multi-signal retrieval, user/session/agent scoping, broad framework integrations, and a production-oriented API/CLI surface.

If you want a mature integration ecosystem, many framework examples, and a product that already spans OSS and managed deployment, Mem0 is a natural system to evaluate.

Hippocampus is pursuing a narrower public-alpha contract today. Its emphasis is on durable source data, an explicit canonical memory path, rebuildable derivations, and keeping the historical evidence separate from later interpretation. That is a design preference, not a claim that the approach is universally better.

Mem0: <https://github.com/mem0ai/mem0>

## Hindsight

Hindsight exposes three central operations: retain, recall, and reflect. Its public docs describe memory banks, structured facts/experiences/observations, and recall that combines semantic search, BM25 keyword matching, graph traversal, and temporal reasoning. It also has integrations across many agent and coding environments.

Hindsight is a strong system to evaluate if you want a more complete memory reasoning stack now, including an explicit reflection layer and temporal/graph retrieval.

A notable architectural difference is source handling. Hindsight's documentation says its retain path extracts facts, entities, and relationships and does not keep raw retained content verbatim as the primary memory representation. Hippocampus deliberately preserves raw conversation/source data and treats topics, observer notes, embeddings, summaries, and identity layers as derived state that should remain revisable.

Hindsight: <https://github.com/vectorize-io/hindsight>

## Hippocampus

Hippocampus v0.1-alpha currently has evidence-backed paths for:

- durable conversation-source ingest into `conversation_stream`;
- explicit canonical memory writes into PostgreSQL;
- keyword recall;
- restart readback;
- PostgreSQL backup/restore followed by continued write and recall;
- the current Hermes adapter contract and its public tool surface.

The repository also contains work around Observer, E1/identity synthesis, topic cards, vector/rerank recall, and broader memory formation. These should not be confused with the evidence-backed alpha surface: several of those paths are still experimental, unknown, or not yet exercised end to end in the frozen public acceptance profile.

See [`PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md) for the exact boundary.

## A useful way to choose

Use Hermes built-in memory first if a small, curated set of facts is enough. Use session/history search if your main need is to recover exact past conversations. Evaluate Mem0 if you want a broad general-purpose memory product with many integrations. Evaluate Hindsight if you want an already-developed retain/recall/reflect stack with graph and temporal retrieval.

Evaluate Hippocampus if the following questions are central to what you are building:

- Do you want the original experience to remain available even if the memory algorithm changes later?
- Do you want derived memory to be rebuildable instead of becoming the only source of truth?
- Do you care about distinguishing what happened from what the system later concluded about it?
- Do you want to experiment with how long-term memory influences an agent's continuity rather than only adding a retrieval API?

## What Hippocampus does not claim

Hippocampus is not currently a drop-in replacement for every memory system above. It is not production-ready, does not yet have comparable integration breadth, and does not claim that its experimental automatic-memory layers are stable.

The project would rather expose those gaps than turn roadmap ideas into marketing claims.

For the reasoning behind that design, see [`WHY_HIPPOCAMPUS.md`](WHY_HIPPOCAMPUS.md). For a hands-on trial, see [`ALPHA-TESTING.md`](ALPHA-TESTING.md).
