-- =============================================================================
-- memory_relations — canonical explicit-memory correction edge (M01).
--
-- Scope:
--   * This file is a schema ARTIFACT ONLY. It is intentionally never auto-applied
--     by v3core.active_memory_store; production migrations are owned by ops.
--     Absence of this table is a truthful migration-required failure at runtime
--     (error_code='MIGRATION_REQUIRED') — never a hidden fallback.
--   * The contract is the table below. Code MUST match these column types,
--     defaults and constraints verbatim — keep the SQL and
--     src/v3core/active_memory_store.py in sync when one of them changes.
--
-- WHAT THIS TABLE EXPRESSES (and nothing more)
-- ------------------------------------------
--     A = was once true     B = is currently true      B supersedes A
--     A = was once true     A is not true any more     A withdraws itself
--
-- It stores ONE edge per correction. The payload of A is never rewritten. So:
--
--   replace   insert a new active explicit_memories row, record a
--             'supersedes' edge A -> B, and archive A — all in ONE transaction.
--   withdraw  record a 'withdraws' edge A -> NULL and archive A, in the same
--             single transaction. NO replacement row is ever created.
--
--     * A survives verbatim and stays readable by exact id / history mode.
--     * B is the single current successor of A (unique outgoing edge).
--     * B itself has at most one predecessor (unique incoming edge), which
--       makes A -> B -> C a chain and never a fan-out.
--     * A -> B -> A-content is a NEW version, not a cycle back to A's row.
--
-- THE TARGET IS SIDECAR, NOT IDENTITY
-- ------------------------------------
-- to_memory_id is NULL-able and its NULL-ness is forced by relation_type:
-- 'supersedes' MUST name a replacement row that is different from its
-- predecessor; 'withdraws' MUST have no target at all. The unique incoming
-- index admits many NULLs, so several unrelated memories can each be
-- withdrawn without ever colliding, while a real target is still claimed by
-- at most one predecessor.
--
-- NOTE: these unique indexes do NOT prevent a cycle. A -> B -> A satisfies
-- both of them. The reader therefore fails closed on a cyclic or dangling
-- chain instead of trusting a partial walk.
--
-- AUTHORITY IS NOT INFERRED
-- -------------------------
-- authority is pinned to 'user_explicit' by CHECK. An inferred, passive or
-- merely newer statement can never establish a correction edge: a correction
-- exists only when an explicitly authorized caller asks for it. There is no
-- 'model_inference' value to fall back to.
--
-- PROVENANCE IS LITERAL, NEVER INVENTED
-- -------------------------------------
-- correction_source_id, when present, is a real public.conversation_stream.id
-- of a role='user' message and is stored literally. It is not rewritten into a
-- guessed host / session / event / QA reference. When no real source id is
-- available the writer records provenance='explicit_tool_request' and leaves
-- correction_source_id NULL.
--
-- REPLAY IDENTITY
-- ---------------
-- request holds the canonical JSON of the normalized correction request
-- (target + mode + full replacement payload + reason + optional source +
-- caller effective_at, absence staying absence). correction_id is
-- 'cor_' + sha256(canonical_json(request)), so an exact retry is recognisable
-- and produces DEDUPLICATED, while any different request against the same
-- target is refused as CORRECTION_CONFLICT instead of creating a second
-- current successor. A withdrawal and a replacement of the same target are
-- therefore always two DIFFERENT requests.
--
-- Idempotency / migration model:
--   * CREATE TABLE IF NOT EXISTS + CREATE UNIQUE INDEX IF NOT EXISTS only.
--   * No DROP / TRUNCATE / DELETE. Applying this is additive and idempotent.
--   * Existing memories need no backfill: a memory with no row here simply has
--     no recorded correction, which is the truthful state for historic data.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.memory_relations (

    -- Stable, deterministic edge identity: 'cor_' + sha256(canonical_json(request)).
    relation_id         TEXT        PRIMARY KEY,

    -- Exactly two relations. 'supersedes' names a replacement row;
    -- 'withdraws' retracts the memory itself and has NO replacement.
    relation_type       TEXT        NOT NULL
                                  CHECK (relation_type IN ('supersedes', 'withdraws')),

    -- The superseded memory. from <> to is structural for a replacement: a
    -- memory can never correct itself, so "reused old content" can only ever
    -- create a new row. For a withdrawal to_memory_id is NULL, and this
    -- comparison is then vacuously true.
    from_memory_id      TEXT        NOT NULL
                                  REFERENCES public.explicit_memories (memory_id),

    -- The replacement memory: a new canonical active row, never a rewrite of
    -- the old one. NULL exactly when relation_type = 'withdraws'.
    to_memory_id        TEXT
                                  REFERENCES public.explicit_memories (memory_id),

    -- 'cor_' + sha256(canonical_json(request)) — the request-level identity,
    -- used for exact-retry comparison and for linking the edge to its request.
    correction_id       TEXT        NOT NULL,

    -- WHO authorized this correction. Pinned, not free text: only an explicit
    -- user correction may replace or withdraw a canonical memory. Inference,
    -- extraction, observer / e1 / topic / dedup paths never obtain this value.
    authority           TEXT        NOT NULL
                                  CHECK (authority IN ('user_explicit')),

    -- Caller-declared "why", stored verbatim when supplied; NULL when omitted.
    correction_reason   TEXT,

    -- Real public.conversation_stream.id of the originating user message, stored
    -- literally. NULL means the correction came from a direct tool request, and
    -- provenance below then reads 'explicit_tool_request'. Never a fabricated
    -- host/event/QA reference.
    correction_source_id TEXT,

    -- How this correction was requested: 'explicit_tool_request' today.
    provenance          TEXT        NOT NULL
                                  CHECK (provenance IN ('explicit_tool_request')),

    -- Declared valid-from time supplied by the caller. NULL when the caller did
    -- not declare one, and absence stays absence so retry identity is stable.
    -- M01 records this value only: it does not schedule future activation and
    -- implements no as-of / temporal reasoning (that is M04).
    effective_at        TIMESTAMPTZ,

    -- When the edge was durably recorded. Server-side NOW() — never caller input.
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Canonical normalized request JSON, for exact replay comparison.
    request             JSONB       NOT NULL,

    CONSTRAINT memory_relations_no_self_edge
        CHECK (from_memory_id <> to_memory_id),

    -- The target must match the relation: a replacement names a DIFFERENT
    -- memory, a withdrawal names none. A withdrawal smuggled in as a
    -- self-target or a 'supersedes' with a NULL target is refused by the
    -- database, not only by the writer.
    CONSTRAINT memory_relations_target_matches_type
        CHECK (
            (relation_type = 'supersedes'
                AND to_memory_id IS NOT NULL
                AND to_memory_id <> from_memory_id)
         OR (relation_type = 'withdraws'
                AND to_memory_id IS NULL)
        )
);

-- -----------------------------------------------------------------------------
-- One outgoing edge per superseded memory: A can have exactly one current
-- successor. This is the constraint that makes a conflicting second successor
-- (A -> C after A -> B) impossible rather than last-write-wins. A withdrawal
-- occupies that one slot too, so a retracted memory can never gain a successor
-- later without a deliberate correction of the withdrawal's own target.
-- -----------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS memory_relations_from_memory_id_key
    ON public.memory_relations (from_memory_id);

-- One incoming edge per replacement memory: A -> B -> C is a chain, and B is
-- never claimed by two different predecessors. Withdrawal rows carry NULL here
-- and PostgreSQL admits any number of NULLs in a unique index, so unrelated
-- withdrawals never collide while a real target stays unique.
-- -----------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS memory_relations_to_memory_id_key
    ON public.memory_relations (to_memory_id);

-- recorded_at DESC is the newest-first inspection order for the reader's
-- history projection; BTree is sufficient (timestamptz, small table).
CREATE INDEX IF NOT EXISTS memory_relations_recorded_at_idx
    ON public.memory_relations (recorded_at DESC);

-- =============================================================================
-- End of artifact. Do NOT add ad-hoc DDL below — extend this file in version
-- control and ship via ops migration.
--
-- Table order matters: this file references public.explicit_memories, so it
-- must be applied after schema/explicit_memories.sql.
-- =============================================================================
