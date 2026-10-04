-- =============================================================================
-- alpha_bootstrap.sql — minimal alpha PostgreSQL bootstrap for v3-core
-- (public-alpha candidate; see CHANGELOG.md for the candidate entry)
--
-- Scope (P0, additive only — no destructive reset):
--   * pgvector extension + canonical active-memory table (verbatim reuse of
--     explicit_memories.sql — DO NOT duplicate the body inline).
--   * Supported alpha working set, derived strictly from PG usage evidence in:
--       - src/v3core/pg_store.py        (conversation_stream, topics,
--                                       topic_entries, observation_notes via
--                                       observer)
--       - src/v3core/recall_pool.py     (qa_pairs, topics, topic_entries)
--       - src/v3core/observer.py        (observation_notes, topic_entries
--                                       with embedding/embed_model/source_qa_id,
--                                       topics note_ref + last_observer_ts)
--       - src/v3core/active_memory_store.py (public.explicit_memories only —
--                                            see explicit_memories.sql)
--       - src/v3core/yin_pool.py        (yin_paragraphs)
--       - src/v3core/__init__.py        (conversation_stream by session_id,
--                                       qa_pairs by id)
--   * This file does NOT recreate the legacy archived tables
--     (v3_cards / v3_messages / v3_facts / v3_effective / embeddings / facts).
--     They were archived 2026-08-06 and must not be revived.
--
-- Boundary (2026-09-09, p2a/active-memory-clean-boundary):
--   * public.explicit_memories writes go through v3core.active_memory_store
--     only (ActiveMemoryWriter). Bootstrap does NOT enable legacy writers
--     against the alpha working set.
--
-- Idempotency:
--   * Every CREATE uses IF NOT EXISTS.
--   * ALTER TABLE ... ADD COLUMN IF NOT EXISTS keeps the file idempotent
--     even when reused across schema bumps (observer note_ref /
--     last_observer_ts arrived after the topics table shipped).
--   * No DROP / TRUNCATE / DELETE statements. This file is bootstrap-only.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- pgvector extension — required by explicit_memories.embedding / topics.embedding /
-- qa_pairs.embedding / observation_notes.embedding / yin_paragraphs.embedding /
-- topic_entries.embedding. Must precede every table that uses VECTOR(1024).
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;

-- -----------------------------------------------------------------------------
-- explicit_memories — verbatim reuse of the canonical artifact.
--
-- Body is NOT inlined: scripts/bootstrap_alpha_db.py reads both this file
-- and schema/explicit_memories.sql and applies them in one transaction so
-- the artifact stays single-sourced. Do NOT duplicate the body here.
--
-- The marker line below is a static anchor used by bootstrap_alpha_db.py
-- (and by tests/test_alpha_bootstrap_contract.py) to locate the inclusion
-- point programmatically. It must remain the only line of its kind.
-- -----------------------------------------------------------------------------
-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/explicit_memories.sql <<<

-- -----------------------------------------------------------------------------
-- memory_relations — M01 memory-correction relation sidecar.
--
-- Body is NOT inlined: schema/memory_relations.sql is the canonical
-- artifact and is spliced here through the same include mechanism as
-- explicit_memories, so the repo DDL stays single-sourced and the
-- packaged copy ships inside the installed wheel.
--
-- Placement: immediately AFTER explicit_memories because every edge
-- references public.explicit_memories(memory_id). Do not move this
-- marker above the explicit_memories include — the fresh-install
-- dependency-order test enforces the order.
--
-- The table is REQUIRED, not optional. A bootstrap that cannot splice it
-- fails loudly (the expansion raises FileNotFoundError) rather than
-- producing an install that silently lacks the correction schema.
-- -----------------------------------------------------------------------------
-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/memory_relations.sql <<<

-- -----------------------------------------------------------------------------
-- derived_memory_invalidations — M03 derived-memory invalidation sidecar.
--
-- Body is NOT inlined: schema/derived_memory_invalidations.sql is the canonical
-- artifact and is spliced here through the same ALPHA_BOOTSTRAP_INCLUDE
-- mechanism as explicit_memories / memory_relations, so the repo DDL stays
-- single-sourced and the packaged copy ships inside the installed wheel.
--
-- Placement: immediately AFTER memory_relations (and therefore after
-- explicit_memories), because the sidecar references
-- public.explicit_memories(memory_id) and records the
-- public.memory_relations(relation_id) edge. Do not move this marker above
-- either of them — the fresh-install dependency-order test enforces the order.
--
-- The table is REQUIRED, not optional. An expansion that cannot splice it
-- fails loudly (FileNotFoundError) rather than producing an install that
-- silently lacks the invalidation schema. Additive and idempotent by
-- construction (CREATE TABLE / CREATE INDEX IF NOT EXISTS only): historic
-- derived artifacts pre-date the sidecar and are left untouched — no backfill,
-- no data rewrite.
-- -----------------------------------------------------------------------------
-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/derived_memory_invalidations.sql <<<

-- -----------------------------------------------------------------------------
-- qa_pairs — QA raw trace; source-of-truth for observer rows + keyword/vector
-- recall. Evidence: __init__.py ~L3750 inserts (source_id, session_id, turn_id,
-- question, answer, tool_calls, tool_results, timestamp, source, embedding,
-- embed_model, created_at) ON CONFLICT (source_id) DO NOTHING;
-- recall_pool.py SELECTs id/timestamp/question/answer/session_id + embedding.
--
-- source_id is the canonical idempotency key (UNIQUE) — retrying the same
-- sync_turn ingest is a no-op via ON CONFLICT (source_id) DO NOTHING, which
-- is the live durability contract. chars is retained as a nullable legacy
-- column (no fresh-ingest path writes it; recall_pool doesn't SELECT it) so
-- a downstream reader expecting the old observer row shape still finds it.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.qa_pairs (
    id            BIGSERIAL    PRIMARY KEY,
    source_id     TEXT         NOT NULL,
    session_id    TEXT,
    turn_id       INTEGER,
    question      TEXT         NOT NULL,
    answer        TEXT,
    -- chars matches observer row shape (id, session_id, timestamp,
    -- question, answer, chars); nullable for legacy rows.
    chars         INTEGER,
    timestamp     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    source        TEXT         NOT NULL DEFAULT 'live_sync',
    tool_calls    JSONB        NOT NULL DEFAULT '[]'::jsonb,
    tool_results  JSONB        NOT NULL DEFAULT '[]'::jsonb,
    merged_event_ids JSONB     NOT NULL DEFAULT '[]'::jsonb,
    embedding     VECTOR(1024),
    embed_model   TEXT         NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Idempotent guards for already-bootstrapped databases that pre-date the
-- fresh-init column set. Fresh init: every column above already exists so
-- every ADD COLUMN is a no-op.
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS source_id   TEXT;
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS turn_id     INTEGER;
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS source      TEXT NOT NULL DEFAULT 'live_sync';
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS tool_calls  JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS tool_results JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS embed_model TEXT NOT NULL DEFAULT '';
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW();
-- P0-A: per-QA membership ledger of the assistant events already merged into
-- the answer row. Idempotent late-derivation guard (data model §4); the
-- DEFAULT keeps every historic row a valid empty ledger.
ALTER TABLE public.qa_pairs
    ADD COLUMN IF NOT EXISTS merged_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Canonical idempotency: source_id is the retry-safe identifier.
-- DO NOTHING (per __init__.py ON CONFLICT (source_id) DO NOTHING) makes a
-- re-attempted live-buffer PG insert for the same durable row a no-op
-- rather than a duplicate row.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.qa_pairs'::regclass
           AND contype  = 'u'
           AND conname  = 'qa_pairs_source_id_key'
    ) THEN
        ALTER TABLE public.qa_pairs
            ADD CONSTRAINT qa_pairs_source_id_key UNIQUE (source_id);
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS qa_pairs_session_ts_idx
    ON public.qa_pairs (session_id, timestamp DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS qa_pairs_timestamp_idx
    ON public.qa_pairs (timestamp DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS qa_pairs_embedding_ivfflat
    ON public.qa_pairs
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/qa_embedding_chunks.sql <<<

-- -----------------------------------------------------------------------------
-- conversation_stream — per-turn message log (replaces the archived
-- v3_messages). Evidence: pg_store.insert_message INSERT (session_id, role,
-- content, trigger, turn_id, timestamp, source, embedding, tool_calls,
-- tool_results); get_message_context SELECT by id / session_id+content prefix.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.conversation_stream (
    id            BIGSERIAL    PRIMARY KEY,
    session_id    TEXT,
    role          TEXT,
    content       TEXT         NOT NULL,
    trigger       TEXT         DEFAULT 'live_buffer',
    turn_id       INTEGER,
    timestamp     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    source        TEXT         DEFAULT 'live_buffer',
    embedding     VECTOR(1024),
    tool_calls    JSONB        NOT NULL DEFAULT '[]'::jsonb,
    tool_results  JSONB        NOT NULL DEFAULT '[]'::jsonb,
    host_turn_id  TEXT
);

-- WHERE-NOT-EXISTS dedupe predicate relies on (session_id, role, timestamp).
CREATE INDEX IF NOT EXISTS conversation_stream_session_role_ts_idx
    ON public.conversation_stream (session_id, role, timestamp);

CREATE INDEX IF NOT EXISTS conversation_stream_session_ts_idx
    ON public.conversation_stream (session_id, timestamp DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS conversation_stream_embedding_ivfflat
    ON public.conversation_stream
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- F2 canonical source identity (additive only, 2026-09-28):
-- nullable host/event_id plus a partial unique index over complete
-- identity tuples. Historic rows (NULL) are untouched and unconstrained;
-- no backfill, no rewrite. Fresh bootstrap and the v0.2 upgrade path
-- carry the same DDL so both install shapes converge.
ALTER TABLE public.conversation_stream
    ADD COLUMN IF NOT EXISTS host TEXT;
ALTER TABLE public.conversation_stream
    ADD COLUMN IF NOT EXISTS event_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS conversation_stream_host_session_event_uniq
    ON public.conversation_stream (host, session_id, event_id)
    WHERE host IS NOT NULL AND session_id IS NOT NULL AND event_id IS NOT NULL;

-- P0-A source provenance (additive only, 2026-10-04): host_turn_id records
-- which *host* turn (the caller's turn_id, not the sync batch turn_id) a
-- source row belongs to, so a late assistant event can deterministically
-- resolve its QA identity after flush/restart. Nullable with a partial index;
-- historic rows (NULL) are untouched and unconstrained; no backfill.
ALTER TABLE public.conversation_stream
    ADD COLUMN IF NOT EXISTS host_turn_id TEXT;
CREATE INDEX IF NOT EXISTS conversation_stream_host_turn_idx
    ON public.conversation_stream (session_id, host_turn_id)
    WHERE host_turn_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- topics — observer topic card (replaces the archived v3_cards). Evidence:
-- pg_store.insert_card INSERT (topic_id, title, summary, body, keywords,
-- note_ref, embedding, embed_model, status, created_at, updated_at) +
-- observer note_ref / last_observer_ts ALTER statements.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.topics (
    topic_id          TEXT         PRIMARY KEY,
    title             TEXT         NOT NULL DEFAULT '',
    summary           TEXT         NOT NULL DEFAULT '',
    body              TEXT         NOT NULL DEFAULT '',
    keywords          TEXT[]       NOT NULL DEFAULT '{}'::text[],
    embedding         VECTOR(1024),
    embed_model       TEXT,
    -- status is the recall hot filter. Hard guard against future
    -- 'archived'/'deleted' literals diverging from the writer's CHECK.
    status            TEXT         NOT NULL DEFAULT 'active',
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- observer v2 columns (added after topics shipped — idempotent guards
    -- cover already-bootstrapped databases):
    note_ref          TEXT,
    last_observer_ts  TIMESTAMPTZ
);

ALTER TABLE public.topics
    ADD COLUMN IF NOT EXISTS note_ref TEXT;
ALTER TABLE public.topics
    ADD COLUMN IF NOT EXISTS last_observer_ts TIMESTAMPTZ;
ALTER TABLE public.topics
    ADD COLUMN IF NOT EXISTS embed_model TEXT;

CREATE INDEX IF NOT EXISTS topics_status_active_idx
    ON public.topics (status)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS topics_updated_at_idx
    ON public.topics (updated_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS topics_embedding_ivfflat
    ON public.topics
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE INDEX IF NOT EXISTS topics_keywords_gin
    ON public.topics
    USING GIN (keywords);

-- -----------------------------------------------------------------------------
-- topic_entries — per-QA back-link to topics (observer writes here). Evidence:
-- observer._link_entries_by_vector / new_facts loop INSERT (topic_id,
-- question, answer, source, seq, timestamp, embedding, embed_model,
-- source_qa_id).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.topic_entries (
    id            BIGSERIAL    PRIMARY KEY,
    topic_id      TEXT         NOT NULL,
    seq           INTEGER      NOT NULL DEFAULT 0,
    question      TEXT         NOT NULL DEFAULT '',
    answer        TEXT         NOT NULL DEFAULT '',
    source        TEXT         DEFAULT 'user',
    timestamp     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    embedding     VECTOR(1024),
    embed_model   TEXT,
    -- 2026-08-22 G1B: explicit back-link to qa_pairs.id; legacy rows had NULL.
    source_qa_id  BIGINT,
    message_id    TEXT,
    confidence    REAL         DEFAULT 0.5
);

ALTER TABLE public.topic_entries
    ADD COLUMN IF NOT EXISTS source_qa_id BIGINT;
ALTER TABLE public.topic_entries
    ADD COLUMN IF NOT EXISTS embed_model TEXT;

CREATE INDEX IF NOT EXISTS topic_entries_topic_idx
    ON public.topic_entries (topic_id);

CREATE INDEX IF NOT EXISTS topic_entries_topic_seq_idx
    ON public.topic_entries (topic_id, seq);

CREATE INDEX IF NOT EXISTS topic_entries_source_qa_id_idx
    ON public.topic_entries (source_qa_id);

CREATE INDEX IF NOT EXISTS topic_entries_embedding_ivfflat
    ON public.topic_entries
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- -----------------------------------------------------------------------------
-- observation_notes — observer rolling印 (v2 single-head). Evidence:
-- observer.py docstring + _observe_worker INSERT (version, content,
-- source_qa_range, prev_id, links) RETURNING id; backfill then writes
-- embedding + embed_model via UPDATE; pg_store.insert_effective also writes
-- embed_model on the row it updates / inserts (line ~L777).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.observation_notes (
    id               BIGSERIAL    PRIMARY KEY,
    version          TEXT         NOT NULL,
    content          TEXT         NOT NULL,
    source_qa_range  INT8RANGE,
    prev_id          BIGINT       REFERENCES public.observation_notes(id),
    links            JSONB        NOT NULL DEFAULT '[]'::jsonb,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    embedding        VECTOR(1024),
    -- embed_model: observer backfill + pg_store.insert_effective BOTH write
    -- (embedding, embed_model) on this table. Without the column the writer
    -- falls back to a no-fingerprint path that leaks "anonymous vectors".
    -- Fresh-init requires the column up front (idempotent ADD COLUMN covers
    -- older bootstrapped DBs that pre-date it).
    embed_model      TEXT         NOT NULL DEFAULT ''
);

ALTER TABLE public.observation_notes
    ADD COLUMN IF NOT EXISTS embed_model TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS observation_notes_version_idx
    ON public.observation_notes (version);

CREATE INDEX IF NOT EXISTS observation_notes_created_at_idx
    ON public.observation_notes (created_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS observation_notes_embedding_ivfflat
    ON public.observation_notes
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/observation_embedding_chunks.sql <<<

-- -----------------------------------------------------------------------------
-- yin_paragraphs — E1 印段落池 (yin_pool.ensure_table). Schema shape comes
-- verbatim from v3core/yin_pool.py SCHEMA_SQL, with embed_model added for
-- fingerprint parity with the rest of the active-memory working set (yin
-- writer also INSERTs embed_model with the live fingerprint).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.yin_paragraphs (
    id           SERIAL       PRIMARY KEY,
    yin_version  TEXT,
    section      TEXT,
    content      TEXT,
    embedding    VECTOR(1024),
    embed_model  TEXT         NOT NULL DEFAULT '',
    created_at   TIMESTAMP    NOT NULL DEFAULT NOW()
);

ALTER TABLE public.yin_paragraphs
    ADD COLUMN IF NOT EXISTS embed_model TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS yin_paragraphs_yin_version_idx
    ON public.yin_paragraphs (yin_version);

CREATE INDEX IF NOT EXISTS yin_paragraphs_embedding_ivfflat
    ON public.yin_paragraphs
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- -----------------------------------------------------------------------------
-- embedding_failures — durable accounting for embedding failures.
--
-- Body is NOT inlined: schema/embedding_failures.sql is the canonical artifact
-- and is spliced here through the same ALPHA_BOOTSTRAP_INCLUDE mechanism as
-- explicit_memories / the derived sidecars, so the repo root DDL stays
-- single-sourced and the packaged copy ships inside the installed wheel.
--
-- Placed last: the ledger has no foreign keys and nothing references it, so
-- appending it keeps every pre-existing table's relative order (and the F2
-- DDL) byte-for-byte where it was.
-- -----------------------------------------------------------------------------
-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/embedding_failures.sql <<<

COMMIT;

-- =============================================================================
-- End of artifact. No DDL below this line. Apply via scripts/bootstrap_alpha_db.py
-- only. Do NOT add DROP / TRUNCATE / DELETE — this file is bootstrap-only.
-- =============================================================================
