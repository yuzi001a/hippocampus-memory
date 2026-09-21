-- observation_embedding_chunks.sql
-- Additive derived retrieval sidecar for long-observation representations.
-- The complete source remains in observation_notes.content; this table
-- stores only token-safe derived spans and their vectors.
-- Additive-only: no ALTER to observation_notes, safe to apply first.
CREATE TABLE IF NOT EXISTS public.observation_embedding_chunks (
    id                     BIGSERIAL    PRIMARY KEY,
    observation_id         BIGINT       NOT NULL
        REFERENCES public.observation_notes(id) ON DELETE CASCADE,
    observation_version    TEXT         NOT NULL,
    chunk_index            INTEGER      NOT NULL,
    source_start           INTEGER      NOT NULL,
    source_end             INTEGER      NOT NULL,
    source_sha256          TEXT         NOT NULL,
    chunk_sha256           TEXT         NOT NULL,
    token_count            INTEGER      NOT NULL,
    representation_version TEXT         NOT NULL,
    embedding              VECTOR(1024),
    embed_model            TEXT         NOT NULL DEFAULT '',
    content                TEXT         NOT NULL,
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT observation_embedding_chunks_parent_chunk_key
        UNIQUE (observation_id, observation_version, chunk_index),
    CONSTRAINT observation_embedding_chunks_source_range_valid
        CHECK (source_end >= source_start AND source_start >= 0),
    CONSTRAINT observation_embedding_chunks_token_count_nonneg
        CHECK (token_count >= 0)
);

CREATE INDEX IF NOT EXISTS observation_embedding_chunks_parent_idx
    ON public.observation_embedding_chunks (observation_id, observation_version);

CREATE INDEX IF NOT EXISTS observation_embedding_chunks_embedding_ivfflat
    ON public.observation_embedding_chunks
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
