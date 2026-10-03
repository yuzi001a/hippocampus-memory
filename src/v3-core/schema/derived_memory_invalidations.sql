-- =============================================================================
-- derived_memory_invalidations — derived-memory invalidation sidecar (M03).
--
-- Scope:
--   * This file is a schema ARTIFACT ONLY. It is intentionally never auto-applied
--     by v3core; production migrations are owned by ops. Absence of this table is
--     a truthful migration-required failure at runtime
--     (error_code='MIGRATION_REQUIRED') — never a hidden fallback, and never a
--     silent downgrade to a default profile.
--   * The contract is the table below. Code MUST match these column types,
--     defaults and constraints verbatim — keep the SQL and the M03 writer /
--     reader in sync when one of them changes.
--
-- WHAT THIS TABLE EXPRESSES (and nothing more)
-- -------------------------------------------
--     A = was once true     B = is currently true      B supersedes A
--     A = was once true     A is not true any more     A withdraws itself
--
-- is the M01/M02 memory_relations edge. This table records the SIDECAR fact
-- that a DERIVED artifact built from A is no longer eligible to be injected as
-- the current conclusion:
--
--     old derived artifact:  content = still A, state = stale
--     new derived artifact:  content = rebuilt from B, state = active
--
-- Invalidation is a sidecar fact, NEVER an edit. This table exists so that no
-- rewrite of a derived body and no removal of a derived row is ever needed:
-- stale artifacts stay physically present and stay readable by id / history /
-- source trace — they simply stop being eligible for current automatic
-- injection.
--
-- TWO ACCEPTED INPUT FORMS, ONE DERIVED IDENTITY
-- ----------------------------------------------
-- A correction's source may be declared in explicit_memories.provenance in
-- either of two forms:
--
--   source_conversation_stream_id  plain ASCII decimal id of a real
--                                  public.conversation_stream.id (role='user')
--   source_qa_id                   plain ASCII decimal id of a real
--                                  public.qa_pairs.id
--
-- 接受两种入口，不等于接受两套身份。
-- Two accepted input forms, one derived identity.
--
-- The resolver normalises BOTH forms to ONE internal identity —
-- public.qa_pairs.id — and stores it in canonical_qa_id. That is the single
-- identity every match is performed against. The caller's own value is kept
-- verbatim in input_source_kind / input_source_id for audit only, so the two
-- entry forms can never become two identities, two rows, or two matching paths.
-- Supplying both forms with disagreeing values is refused rather than guessed.
--
-- Refusals are truthful and named: the resolver reports SOURCE_NOT_MAPPED
-- (present but not resolvable — including a role='user' stream row that owns no
-- QA, the normal case for the 2nd and later of merged consecutive user
-- messages), SOURCE_AMBIGUOUS (one stream identity matched more than one QA
-- row), SOURCE_IDENTITY_CONFLICT (both input forms were supplied and resolved
-- to different canonical qa_pairs.id — neither side is preferred) or
-- NO_SOURCE (no source supplied at all). A refused source is never fabricated,
-- stripped or normalised into a plausible-looking id.
--
-- ONE UNIFORM MECHANISM FOR EVERY DERIVED KIND
-- -------------------------------------------
-- derived_kind pins the three derived layers M03 lineage reconnaissance proved
-- mappable — Topic / Observer note / E1 paragraph — instead of a different
-- correction column on four tables. Anything judgeable only by body text,
-- vector similarity or an LLM is NOT auto-adjudicated: it is reported as
-- UNMAPPED_HISTORICAL_DERIVED / UNKNOWN_DEPENDENCY and counted, never claimed
-- fixed here.
--
-- STATE MACHINE
-- -------------
--   stale            invalidated, not yet rebuilt; suppressed from injection
--   pending_rebuild  rebuild requested but could not run (e.g. no provider);
--                    the artifact stays suppressed
--   rebuilt          a replacement derived artifact exists
--   unresolved       not adjudicated deterministically — reported, not claimed
--
-- The correction is NEVER rolled back by this table. CORRECTION FIRST,
-- INVALIDATION SECOND, REBUILD THIRD: a provider / embedding / rebuild failure
-- leaves A -> B durable and the row in pending_rebuild.
--
-- REPLAY IDENTITY
-- ---------------
-- invalidation_id = 'inv_' + sha256(correction_id|derived_kind|derived_id) and
-- the UNIQUE (correction_id, derived_kind, derived_id) key together make
-- propagation IDEMPOTENT: replaying the same propagation produces ZERO new
-- rows. Identity normalisation deliberately does NOT enter that formula — the
-- id is derived from the correction and the derived artifact only, so the same
-- correction maps to the same invalidation no matter which of the two input
-- forms was used to declare its source.
--
-- PROVENANCE IS LITERAL, NEVER INVENTED
-- -------------------------------------
-- source_memory_id is the corrected-from memory A, replacement_memory_id the
-- replacement B (NULL for a withdrawal), relation_id the M01 edge when one was
-- recorded. No value here is produced by text matching, vector similarity or an
-- LLM guess; a derived artifact that cannot be mapped deterministically gets no
-- row at all.
--
-- Idempotency / migration model:
--   * CREATE TABLE IF NOT EXISTS + CREATE INDEX / CREATE UNIQUE INDEX
--     IF NOT EXISTS only.
--   * No DROP / TRUNCATE / DELETE. Applying this is additive and idempotent.
--   * Existing derived artifacts need no backfill: an artifact with no row here
--     simply has no recorded invalidation, which is the truthful state for
--     historic data.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.derived_memory_invalidations (

    -- Stable, deterministic identity: 'inv_' + sha256(correction_id|derived_kind|derived_id).
    invalidation_id        TEXT        PRIMARY KEY,

    -- The M02 correction identity ('cor_' + sha256(canonical_json(request))).
    correction_id          TEXT        NOT NULL,

    -- The M01 memory_relations edge when the correction recorded one. NULL when
    -- the invalidation was propagated without a persisted relation row.
    relation_id            TEXT,

    -- The ONE internal identity: the normalised public.qa_pairs.id. Both
    -- accepted input forms resolve to this single value before anything is
    -- matched, so two entry forms can never become two identities. NULL when the
    -- source could not be resolved (reported, never guessed).
    canonical_qa_id        BIGINT,

    -- Which accepted input form the caller actually supplied. Pinned, and NULL
    -- when no source was supplied at all (NO_SOURCE) — there is no third form
    -- and no 'inferred' value to fall back to.
    input_source_kind      TEXT
                                     CHECK (input_source_kind IN ('conversation_stream', 'qa_pairs')),

    -- The caller's original value, stored verbatim for audit. It is NEVER used
    -- for matching and is never rewritten into the canonical identity above.
    input_source_id        TEXT,

    -- Which derived layer this row invalidates. Pinned, not free text: only the
    -- three kinds the M03 lineage reconnaissance proved mappable are admissible.
    derived_kind           TEXT        NOT NULL
                                     CHECK (derived_kind IN ('topic', 'observer_note', 'yin_paragraph')),

    -- The derived artifact identity: topics.topic_id / observation_notes.id /
    -- yin_paragraphs.id, stored literally. Never a title, a body match or a vector.
    derived_id             TEXT        NOT NULL,

    -- A: the memory the derived artifact was built from.
    source_memory_id       TEXT        NOT NULL,

    -- B: the replacement memory that supersedes A. NULL for a withdrawal — and
    -- no invented "not A" text is ever recorded in its place.
    replacement_memory_id  TEXT,

    -- Lifecycle state. Pinned to the four truthful outcomes; there is no
    -- 'deleted' / 'fixed' value to overstate the result with.
    state                  TEXT        NOT NULL
                                     CHECK (state IN ('stale', 'pending_rebuild', 'rebuilt', 'unresolved')),

    -- Caller-declared "why", stored verbatim when supplied; NULL when omitted.
    reason                 TEXT,

    -- When the invalidation was durably recorded. Server-side NOW() — never caller input.
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- When the row reached a terminal state (rebuilt / unresolved). NULL while
    -- still stale / pending_rebuild, so absence stays absence.
    resolved_at            TIMESTAMPTZ,

    -- The rebuilt derived artifact that replaces the stale one, when a rebuild
    -- actually ran. NULL when no rebuild ran: the stale artifact is suppressed,
    -- never replaced by an invention.
    replacement_derived_id TEXT
);

-- -----------------------------------------------------------------------------
-- The idempotency key: one invalidation per (correction, derived kind, derived
-- id). This is the constraint that makes replaying propagation a no-op instead
-- of a duplicate row, and that makes last-write-wins impossible.
-- -----------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS derived_memory_invalidations_correction_kind_derived_key
    ON public.derived_memory_invalidations (correction_id, derived_kind, derived_id);

-- Re-running propagation for a single correction_id looks its rows up directly.
CREATE INDEX IF NOT EXISTS derived_memory_invalidations_correction_id_idx
    ON public.derived_memory_invalidations (correction_id);

-- The injection-time suppression probe — "is this derived artifact stale?" —
-- asks by (derived_kind, derived_id) and must not scan the table.
CREATE INDEX IF NOT EXISTS derived_memory_invalidations_kind_derived_idx
    ON public.derived_memory_invalidations (derived_kind, derived_id);

-- created_at DESC is the newest-first inspection / unmapped-report order.
CREATE INDEX IF NOT EXISTS derived_memory_invalidations_created_at_idx
    ON public.derived_memory_invalidations (created_at DESC);

-- canonical_qa_id is the single normalised internal identity; the mapping step
-- asks "which derived artifacts came from this qa id?" by this column alone.
CREATE INDEX IF NOT EXISTS derived_memory_invalidations_canonical_qa_id_idx
    ON public.derived_memory_invalidations (canonical_qa_id);

-- =============================================================================
-- End of artifact. Do NOT add ad-hoc DDL below — extend this file in version
-- control and ship via ops migration.
--
-- Table order matters: this file references public.explicit_memories and
-- public.memory_relations, so it must be applied after
-- schema/explicit_memories.sql and schema/memory_relations.sql.
-- =============================================================================
