-- ─────────────────────────────────────────────────────────────────────────────
-- embedding_failures — durable, explainable accounting for embedding failures
--
-- WHY THIS EXISTS
-- ---------------
-- Before this table, a failed embedding request became a bare NULL in an
-- `embedding`/vector column. Nothing recorded that a request had been attempted,
-- why it failed, or whether retrying could help. From the outside, "never
-- attempted", "attempted and lost to a 3s timeout", and "attempted with a
-- misconfigured API key" were indistinguishable — which is precisely what made
-- the production memory hole silent, and made it impossible to tell afterwards
-- which NULLs were repairable.
--
-- CONTRACT INTRODUCED WITH THIS TABLE
-- -----------------------------------
--   Embedding failure is allowed. Silent permanent memory loss is not.
--   (Embedding 可以失败，但不能无声地变成永久失忆。)
--
-- The source row stays durable; the derived embedding may be missing; and every
-- missing embedding that resulted from a failed *request* has a row here.
--
-- SHAPE
-- -----
-- One row per (entity_table, entity_id, phase). A UNIQUE constraint makes the
-- writer an idempotent UPSERT: repeated failures bump `attempts` instead of
-- piling up rows, and a later success sets `resolved_at` rather than deleting
-- history. That keeps the two questions an operator actually asks cheap:
--   * "which NULLs have no explanation?"        → LEFT JOIN ... IS NULL
--   * "what is still retryable?"                 → resolved_at IS NULL AND retryable
--
-- PHASE 8 EXPLICITLY FORBIDS STORING: API keys, credentials, full sensitive
-- responses, or raw private text. `error_fingerprint` is a short hash of the
-- error text, never the text itself; the model/provider *fingerprint* is already
-- the non-secret profile hash used by the vector tables.
--
-- ADDITIVE ONLY. Nothing here alters an existing table or column. Applying this
-- migration is a production change and therefore requires explicit approval;
-- this file is written and tested against a disposable database first.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.embedding_failures (
    id                 BIGSERIAL PRIMARY KEY,

    -- what failed
    entity_table       TEXT        NOT NULL,
    entity_id          TEXT        NOT NULL,
    phase              TEXT        NOT NULL,

    -- why it failed
    error_class        TEXT        NOT NULL,
    retryable          BOOLEAN     NOT NULL,
    provider_status    INTEGER,

    -- how hard we tried
    attempts           INTEGER     NOT NULL DEFAULT 0,
    timeout_policy     TEXT        NOT NULL,
    timeout_seconds    DOUBLE PRECISION,
    max_retries        INTEGER,
    elapsed_ms         DOUBLE PRECISION,

    -- provider / model identity (non-secret; mirrors the vector tables)
    model              TEXT,
    model_fingerprint  TEXT,

    -- safe error fingerprint: short hash of the error message, never the message
    error_fingerprint  TEXT,

    -- lifecycle
    resolved_at        TIMESTAMPTZ,
    resolution         TEXT,
    first_failed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_failed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent upsert target: one live record per entity+phase.
CREATE UNIQUE INDEX IF NOT EXISTS embedding_failures_entity_phase_key
    ON public.embedding_failures (entity_table, entity_id, phase);

-- "what is still broken / still retryable" — the operator's main query.
CREATE INDEX IF NOT EXISTS embedding_failures_unresolved_idx
    ON public.embedding_failures (entity_table, retryable)
    WHERE resolved_at IS NULL;

-- Failure-class rollups for the reliability report.
CREATE INDEX IF NOT EXISTS embedding_failures_class_idx
    ON public.embedding_failures (error_class, last_failed_at DESC);
