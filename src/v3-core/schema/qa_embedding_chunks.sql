-- qa_embedding_chunks.sql
-- Additive derived retrieval sidecar for long QA representations.
-- The complete source remains in conversation_stream/qa_pairs; this table
-- stores only token-safe derived spans and their vectors.
CREATE TABLE IF NOT EXISTS public.qa_embedding_chunks (
    id                     BIGSERIAL    PRIMARY KEY,
    qa_id                  BIGINT       NOT NULL
        REFERENCES public.qa_pairs(id) ON DELETE CASCADE,
    chunk_index            INTEGER      NOT NULL,
    source_field           TEXT         NOT NULL
        CHECK (source_field IN ('question', 'answer')),
    source_start           INTEGER      NOT NULL,
    source_end             INTEGER      NOT NULL,
    source_sha256          TEXT         NOT NULL,
    token_count            INTEGER      NOT NULL,
    representation_version TEXT         NOT NULL,
    embedding              VECTOR(1024),
    embed_model            TEXT         NOT NULL DEFAULT '',
    content                TEXT         NOT NULL,
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT qa_embedding_chunks_qa_id_chunk_index_key
        UNIQUE (qa_id, chunk_index),
    CONSTRAINT qa_embedding_chunks_source_range_valid
        CHECK (source_end >= source_start AND source_start >= 0),
    CONSTRAINT qa_embedding_chunks_token_count_nonneg
        CHECK (token_count >= 0)
);

CREATE INDEX IF NOT EXISTS qa_embedding_chunks_qa_id_idx
    ON public.qa_embedding_chunks (qa_id);

CREATE INDEX IF NOT EXISTS qa_embedding_chunks_embedding_ivfflat
    ON public.qa_embedding_chunks
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
