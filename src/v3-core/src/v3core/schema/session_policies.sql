-- session_policies — 零 A 会话四档处置的落库位置（2026-11-10）
--
-- 只存"每个会话属于哪一档 + 判据信号"，不删任何原始记录。
-- 判据由 v3core.session_policy.classify_session 用结构信号算出：
--   keep / downgrade / raw_only / exclude
-- 具体数量随语料变化，不写死比例，也不作为删除依据。

CREATE TABLE IF NOT EXISTS public.session_policies (
    session_id  TEXT PRIMARY KEY,
    host        TEXT,
    tier        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    signals     JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS session_policies_tier_idx
    ON public.session_policies (tier, host);
