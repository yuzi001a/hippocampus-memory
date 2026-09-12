-- =============================================================================
-- explicit_memories — canonical active-memory schema artifact (P2a).
--
-- Scope:
--   * This file is a schema ARTIFACT ONLY. It is intentionally never auto-applied
--     by v3core.active_memory_store; production migrations are owned by ops.
--   * The contract is the table below. Code MUST match these column types,
--     defaults, and constraints verbatim — keep the SQL and the Python module
--     in sync when one of them changes.
--
-- Boundary (2026-09-09, p2a/active-memory-clean-boundary):
--   * Writes from the v3 active-memory pipeline route ONLY through
--     v3core.active_memory_store (ActiveMemoryWriter).
--   * Passive paths (observer / e1 / topic_store / dedup) MUST NOT perform DML
--     against this table — see RED test test_C5_passive_paths_have_no_explicit_memories_dml.
--
-- Idempotency model:
--   * ON CONFLICT (memory_id) DO NOTHING + fresh readback — see
--     ActiveMemoryWriter.create() canonical algorithm.
--   * memory_id is the canonical SHA256-based identity; explicit caller-supplied
--     memory_id (or source_id alias) wins unchanged.
--
-- Embedding contract:
--   * embedding is VECTOR(1024) NULL — populated only post-commit by an
--     injected embedder (NEVER during the canonical write path).
--   * The CHECK constraint enforces that whenever embedding is set, embed_model
--     must be a non-empty string (no anonymous vectors).
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.explicit_memories (

    -- Canonical identity. PK is the canonical id; explicit nonempty caller
    -- memory_id (or source_id alias) wins unchanged. Otherwise it is derived as
    -- 'mem_' + sha256(canonical_json(category, title, content, tags)).
    memory_id      TEXT        PRIMARY KEY,

    -- Canonical payload — these four columns drive the canonical hash and the
    -- durable readback equality check.
    category       TEXT        NOT NULL,
    title          TEXT        NOT NULL,
    content        TEXT        NOT NULL,
    tags           TEXT[]      NOT NULL DEFAULT '{}'::text[],

    -- Audit / lineage. Caller may pass provenance as a JSON object; the canonical
    -- algorithm never merges provenance across writes — it is overwritten on
    -- insert and never updated on dedup.
    provenance     JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Soft-delete lifecycle. Only 'active' and 'archived' are accepted; see
    -- ActiveMemoryWriter.archive(). Hard delete is explicitly rejected.
    status         TEXT        NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'archived')),

    -- Timestamps. Defaults are server-side NOW(); explicit inserts may override
    -- (e.g. backfill) but the canonical path leaves them unset.
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Embedding is POST-COMMIT ONLY — populated by a separate UPDATE issued by
    -- ActiveMemoryWriter after the canonical readback confirms durable commit.
    -- VECTOR(1024) is the project-wide standard (bge-m3). NULL is allowed when
    -- embedding is disabled or fails; the canonical row remains durable.
    embedding      VECTOR(1024),

    -- Non-empty iff embedding is set. CHECK enforces that anonymous vectors
    -- are never persisted — the active-memory pipeline is fail-closed on
    -- embed_model fingerprint presence, see v3core.embedding._validate_for_call.
    embed_model    TEXT,

    CONSTRAINT explicit_memories_embed_consistency
        CHECK (embedding IS NULL OR (embed_model IS NOT NULL
                                     AND length(btrim(embed_model)) > 0))
);

-- -----------------------------------------------------------------------------
-- Indexes — operational lookups only. Keep them minimal and aligned with the
-- reader's WHERE clauses (search_keyword / search_vector / get_by_memory_id /
-- archive). Avoid partial indexes that depend on caller-supplied provenance.
-- -----------------------------------------------------------------------------

-- Vector search uses IVFFLAT on the standard bge-m3 1024-dim space. Lists=100
-- is the conservative default for tables up to ~1M rows; operators may
-- re-tune via separate ops migration if production traffic warrants.
CREATE INDEX IF NOT EXISTS explicit_memories_embedding_ivfflat
    ON public.explicit_memories
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- status='active' is the reader's hard filter; a partial index keeps vector
-- search small and the partial keyword search cheap.
CREATE INDEX IF NOT EXISTS explicit_memories_status_active_idx
    ON public.explicit_memories (status)
    WHERE status = 'active';

-- created_at DESC is the tie-breaker / freshness order for both reader paths
-- (keyword + vector). BTree is sufficient — created_at is a timestamptz.
CREATE INDEX IF NOT EXISTS explicit_memories_created_at_idx
    ON public.explicit_memories (created_at DESC);

-- GIN on tags for tag-overlap keyword search.
CREATE INDEX IF NOT EXISTS explicit_memories_tags_gin
    ON public.explicit_memories
    USING GIN (tags);

-- =============================================================================
-- End of artifact. Do NOT add ad-hoc DDL below — extend this file in version
-- control and ship via ops migration.
--
-- updated_at policy (intentional, by design):
--   * DEFAULT NOW() on insert.
--   * No trigger overrides updated_at on UPDATE — callers that need to bump it
--     must set it explicitly in the UPDATE statement. The post-commit
--     embedding UPDATE in v3core.active_memory_store MUST leave updated_at
--     unchanged (embedding/embed_model only); the archive UPDATE sets
--     updated_at = NOW() explicitly. This keeps the embedding projection
--     completely free of any side effect on updated_at.
-- =============================================================================