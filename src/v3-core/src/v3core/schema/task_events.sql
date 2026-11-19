-- task_events — 最小任务事件存储（2026-11-10）
--
-- 只记录"有来源的任务事件"，任务状态由事件计算得出，不存"当前状态"可变字段。
-- 一个任务的当前状态 = 该 task_id 下 occurred_at 最新的事件（同刻按 recorded_at）。
--
-- 设计约束（来自用户决定）：
--   * 不拿 topics 当任务状态库；不碰 qa_pairs / observer / E1 / yin。
--   * task_id 必须稳定：优先原生任务身份（委派 id / cron job id / 调用方显式
--     task_id），没有原生身份就不产生任务事件——不做语义相似度合并。
--   * 同一任务的多次自动注入落在同一个 task_id 上，产生多条事件而不是多个任务。
--   * "Agent 声称完成" 与 "测试/日志证明完成" 必须是不同状态：
--     claimed_done vs verified_done。更正以新事件表达，旧事件保留。
--
-- 幂等：event_id 是主键，重复导入 ON CONFLICT DO NOTHING。

CREATE TABLE IF NOT EXISTS public.task_events (
    event_id      TEXT PRIMARY KEY,
    task_id       TEXT        NOT NULL,
    project       TEXT,
    event_type    TEXT        NOT NULL,
    status        TEXT        NOT NULL,
    summary       TEXT        NOT NULL,
    source_id     TEXT        NOT NULL,
    occurred_at   TIMESTAMPTZ NOT NULL,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    provenance    JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS task_events_task_idx
    ON public.task_events (task_id, occurred_at DESC, recorded_at DESC);

CREATE INDEX IF NOT EXISTS task_events_project_idx
    ON public.task_events (project, occurred_at DESC);

CREATE INDEX IF NOT EXISTS task_events_source_idx
    ON public.task_events (source_id);
