-- =============================================================================
-- upgrade_v0_2.sql — additive existing-install upgrade for v0.2 (first-user
-- release). Owned by the existing-install closure task; child A owns the
-- qa_embedding_chunks.sql artifact and that file is NOT duplicated here.
--
-- Scope (additive only — no destructive reset, no data rewrite):
--   * Create public.explicit_memories (verbatim reuse of explicit_memories.sql
--     via the canonical ALPHA_BOOTSTRAP_INCLUDE marker, so the table stays
--     single-sourced across fresh bootstrap and existing-install upgrade).
--   * Create public.schema_versions (a tiny migration ledger used to record
--     that the upgrade ran; it is the single artifact that the doctor and
--     downstream tools key off for "has this install been upgraded to v0.2?").
--   * Idempotent ADD COLUMN IF NOT EXISTS guards for any later columns that
--     an older install may lack (qa_pairs.embed_model / created_at /
--     tool_calls / tool_results / turn_id / source / source_id, topics
--     .embed_model / note_ref / last_observer_ts, topic_entries.embed_model /
--     source_qa_id, observation_notes.embed_model, yin_paragraphs.embed_model).
--     These are the same ADD COLUMN IF NOT EXISTS seams that ship in
--     alpha_bootstrap.sql — the upgrade file is intentionally a strict subset
--     of that file, so re-running bootstrap after upgrade is safe.
--   * IF NOT EXISTS on every CREATE TABLE / CREATE INDEX / CREATE EXTENSION.
--   * schema_versions row is INSERTed via INSERT ... ON CONFLICT DO NOTHING
--     so the upgrade is safe to re-run.
--
-- Out of scope (child A owns):
--   * qa_embedding_chunks sidecar DDL — the upgrade tool will *consume* that
--     artifact if and only if the file is present in the installed package
--     (v3core.schema.qa_embedding_chunks.sql). The upgrade MUST NOT silently
--     succeed when qa_embedding_chunks.sql is missing on a v0.2 install: the
--     tool reports the missing artifact and exits with status 2 (operator
--     must decide: rebuild child-A artifact, or accept the partial upgrade).
--
-- Boundary (hard guarantee — enforced by test):
--   * No DROP, no TRUNCATE, no DELETE, no ALTER COLUMN ... DROP, no UPDATE.
--   * No data rewrite — every statement is structural only.
--   * Single BEGIN / COMMIT transaction. If any statement raises, the
--     entire upgrade rolls back; the schema_versions row is only written
--     on a successful COMMIT.
--
-- This file is wrapped in BEGIN / COMMIT by the apply path. The dry-run path
-- parses it without executing it.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- pgvector extension — required by VECTOR(1024) columns on the explicit
-- memory table. Idempotent: no-op when the extension is already installed.
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;

-- -----------------------------------------------------------------------------
-- explicit_memories — verbatim reuse of the canonical artifact. The marker
-- line below is the same single-source-of-truth anchor the bootstrap uses;
-- the apply path replaces it with the body of explicit_memories.sql at apply
-- time, and the dry-run path reports "explicit_memories.sql present" without
-- writing anything.
-- -----------------------------------------------------------------------------
-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/explicit_memories.sql <<<

-- -----------------------------------------------------------------------------
-- schema_versions — minimal migration ledger. One row per applied upgrade.
-- A separate upgrade tool MAY add additional rows; this file's role is to
-- create the table + record the v0.2 upgrade.
--
-- The doctor `_check_schema_version` and `_check_migration_state` checks key
-- off this table when it exists; on an install that pre-dates the table they
-- fall back to counting canonical alpha tables (existing behavior, preserved).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.schema_versions (
    version        TEXT        PRIMARY KEY,
    description    TEXT        NOT NULL DEFAULT '',
    applied_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by     TEXT        NOT NULL DEFAULT 'hippocampus upgrade'
);

-- The v0.2 upgrade row. Idempotent: re-running the upgrade is a no-op.
INSERT INTO public.schema_versions (version, description)
VALUES ('v0.2', 'additive existing-install upgrade: explicit_memories + schema_versions ledger + ADD COLUMN IF NOT EXISTS guards')
ON CONFLICT (version) DO NOTHING;

-- -----------------------------------------------------------------------------
-- Idempotent ADD COLUMN IF NOT EXISTS guards — match the same set of "later
-- columns" alpha_bootstrap.sql ships. Re-applying the upgrade on a database
-- that already has them is a no-op; running it on an older install adds
-- exactly what alpha_bootstrap.sql would add on a fresh bootstrap.
-- -----------------------------------------------------------------------------
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS source_id    TEXT;
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS turn_id      INTEGER;
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS source       TEXT NOT NULL DEFAULT 'live_sync';
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS tool_calls   JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS tool_results JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS embed_model  TEXT NOT NULL DEFAULT '';
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE public.topics
    ADD COLUMN IF NOT EXISTS note_ref         TEXT;
ALTER TABLE public.topics
    ADD COLUMN IF NOT EXISTS last_observer_ts TIMESTAMPTZ;
ALTER TABLE public.topics
    ADD COLUMN IF NOT EXISTS embed_model      TEXT;

ALTER TABLE public.topic_entries
    ADD COLUMN IF NOT EXISTS source_qa_id BIGINT;
ALTER TABLE public.topic_entries
    ADD COLUMN IF NOT EXISTS embed_model  TEXT;

ALTER TABLE public.observation_notes
    ADD COLUMN IF NOT EXISTS embed_model TEXT NOT NULL DEFAULT '';

ALTER TABLE public.yin_paragraphs
    ADD COLUMN IF NOT EXISTS embed_model TEXT NOT NULL DEFAULT '';

-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/observation_embedding_chunks.sql <<<

COMMIT;

-- =============================================================================
-- End of artifact. The apply path appends the qa_embedding_chunks.sql artifact
-- (when present) BEFORE this COMMIT, in a separate transaction that records
-- its own schema_versions row. See distribution_cli._upgrade_apply for the
-- orchestration.
-- =============================================================================
