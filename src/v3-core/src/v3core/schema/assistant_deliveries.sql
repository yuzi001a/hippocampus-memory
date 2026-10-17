-- =============================================================================
-- assistant_deliveries — P0-C2 autonomous/async assistant delivery (derived).
--
-- Source of truth is public.conversation_stream.  A delivery is a DERIVED,
-- REBUILDABLE grouping of the contiguous non-empty NORMAL_ASSISTANT rows that
-- follow one host boundary (ASYNC_BOUNDARY / NEW_ROOT) until the next real
-- boundary or REAL_USER row.  It is NOT a synthetic QA pair: no question is
-- fabricated, and no prior human QA row is touched.
--
-- Identity is deterministic and content-independent:
--     source_id = 'delivery/<percent-encoded-host>/<percent-encoded-session_id>/'
--                 '<percent-encoded-trigger_event_id>'
-- where trigger_event_id is the durable conversation_stream event id of the
-- boundary row (role 'host_event:<KIND>').  Replaying the same source
-- snapshot therefore collapses to exactly one row.  Replays may extend a
-- delivery as more assistant events arrive; stale snapshots cannot shrink it.
--
-- event_ids is the ordered list of assistant source event ids that make up the
-- delivery; each resolves to its conversation_stream row via the F2 composite
-- identity (host, session_id, event_id).  embedding is derived/optional — the
-- source rows and the delivery row are durable with or without a provider.
--
-- Additive and idempotent by construction (CREATE TABLE / INDEX IF NOT EXISTS
-- only).  No DROP / TRUNCATE / DELETE.  No backfill, no data rewrite: historic
-- source rows pre-date the delivery container and are left untouched.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.assistant_deliveries (
    id               BIGSERIAL    PRIMARY KEY,
    source_id        TEXT         NOT NULL,
    host             TEXT         NOT NULL,
    session_id       TEXT         NOT NULL,
    trigger_event_id TEXT         NOT NULL,
    trigger_kind     TEXT         NOT NULL,
    content          TEXT         NOT NULL,
    event_ids        JSONB        NOT NULL DEFAULT '[]'::jsonb,
    started_at       TIMESTAMPTZ,
    embedding        VECTOR(1024),
    embed_model      TEXT         NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Canonical idempotency: source_id is the retry-safe identifier. Mirrors the
-- qa_pairs.source_id contract. The writer inserts with ON CONFLICT DO NOTHING,
-- then locks an existing row and appends only new source-event segments after
-- the persisted event_ids prefix. Same-ID text rewrites cannot overwrite raw
-- source content; an authoritative rebuild from conversation_stream may repair
-- the entire derived row. A plain unique index (not a DO-block constraint)
-- keeps the artifact idempotent and upgrade destructive-scan clean.
CREATE UNIQUE INDEX IF NOT EXISTS assistant_deliveries_source_id_key
    ON public.assistant_deliveries (source_id);

-- One delivery per complete F2 source identity.
CREATE UNIQUE INDEX IF NOT EXISTS assistant_deliveries_trigger_uniq
    ON public.assistant_deliveries (host, session_id, trigger_event_id);

CREATE INDEX IF NOT EXISTS assistant_deliveries_session_ts_idx
    ON public.assistant_deliveries (session_id, created_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS assistant_deliveries_embedding_ivfflat
    ON public.assistant_deliveries
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
