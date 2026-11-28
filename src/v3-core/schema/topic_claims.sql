-- =============================================================================
-- topic_claims + topic_claim_evidence — Topic V2 relational claim & lineage store.
--
-- Three-layer Topic V2 memory architecture:
--   Layer 1: public.topics                (stable topic identity + active view)
--   Layer 2: public.topic_claims          (active & superseded claims + chain)
--   Layer 3: public.topic_claim_evidence  (many-to-many claim <-> raw QA links)
--
-- Source of truth for raw utterances remains public.qa_pairs (append-only;
-- never mutated or deleted). Every claim carries a stable claim_id so LLM
-- Phase 1 updates can supersede exact claims without lexical guessing, and
-- Recall can query exact superseded QA IDs without any header-length cap.
--
-- Additive and idempotent by construction (CREATE TABLE / INDEX IF NOT EXISTS
-- only). No DROP / TRUNCATE / DELETE.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.topic_claims (
    claim_id              TEXT         PRIMARY KEY,
    topic_id              TEXT         NOT NULL,
    claim_text            TEXT         NOT NULL,
    kind                  TEXT         NOT NULL DEFAULT 'fact',
    status                TEXT         NOT NULL DEFAULT 'active',
    replaced_by_claim_id  TEXT         NOT NULL DEFAULT '',
    supersede_reason      TEXT         NOT NULL DEFAULT '',
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    superseded_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS topic_claims_topic_status_idx
    ON public.topic_claims (topic_id, status);

CREATE INDEX IF NOT EXISTS topic_claims_status_idx
    ON public.topic_claims (status);

CREATE TABLE IF NOT EXISTS public.topic_claim_evidence (
    claim_id    TEXT         NOT NULL,
    topic_id    TEXT         NOT NULL,
    qa_id       BIGINT       NOT NULL,
    relation    TEXT         NOT NULL DEFAULT 'supports',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (claim_id, qa_id, relation)
);

CREATE INDEX IF NOT EXISTS topic_claim_evidence_qa_idx
    ON public.topic_claim_evidence (qa_id);

CREATE INDEX IF NOT EXISTS topic_claim_evidence_topic_idx
    ON public.topic_claim_evidence (topic_id, relation);
