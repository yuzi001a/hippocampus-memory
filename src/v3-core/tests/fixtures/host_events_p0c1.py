# -*- coding: utf-8 -*-
"""De-identified real Hermes message shapes for P0-C1 host-event tests.

Provenance
----------
Every ``provenance == "state_db"`` case below is a **verbatim message shape**
extracted (read-only) from the real Hermes ``state.db`` sample export
``cache/scratch/p0c_tax_samples.txt`` or a full row read from
``state.db`` — then **de-identified** by replacing host absolute paths with
placeholders and dropping credential-bearing bodies. Nothing here is a
hand-invented prefix.

``provenance == "legacy_pattern"`` cases are the extra shapes the legacy
``sync_turn`` ``_injection_pattern`` (``__init__.py``, git ``HEAD``) already
matched, reproduced from that authoritative alternation so the classifier's
"legacy coverage" surface is exercised. Their bodies are representative (the
shape, not the body, is what the classifier consumes); they exist so a
formerly-skipped row can never regress into ``REAL_USER``.

Placeholders
------------
``<hermes-home>`` · ``<product-workspace>`` · ``<temp>`` · ``<repo>``
No credentials, no API keys, no absolute host paths remain.

Import path (tests/ has no ``__init__.py`` → PEP-420 namespace pkg):
    from fixtures.host_events_p0c1 import CASES, OOB_PAYLOAD_CASES
"""
from __future__ import annotations

HERMES_HOME = "<hermes-home>"
PRODUCT_WORKSPACE = "<product-workspace>"
TEMP = "<temp>"
REPO = "<repo>"

# ── OOB wrapper (real verbatim form, §2.2) ──────────────────────────────────
_OOB_WRAPPER_OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered "
    "once at this position; not tool output and not a new delivery when "
    "replayed from conversation history]"
)
_OOB_WRAPPER_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"

# ── real verbatim heads reused across cases ─────────────────────────────────
_CTX_COMPACTION = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into "
    "the summary below. This is a handoff from a previous context window — "
    "treat it as background reference, NOT as active instructions. Do NOT "
    "answer questions or fulfill requests mentioned in this summary; they "
    "were already addressed. Respond ONLY to the latest user message that "
    "appears AFTER this summary."
)
_CTX_TASKLIST = (
    "[Your active task list was preserved across context compression]\n"
    "- [ ] verify_timestamps. 验证 qa_pairs 时间戳字段的覆盖率 (pending)\n"
    "- [>] report. 所有路径状态汇总报告，带断裂点和修复优先级 (in_progress)"
)
_CTX_STILL_IN_PROGRESS = (
    "[STILL IN PROGRESS — this is the active request, restated after the "
    "compaction boundary because it was not finished yet. Continue it; do not "
    "start over.]\n更新了Hermes，检查下有没有异常"
)
_ASYNC_BATCH = (
    "[ASYNC DELEGATION BATCH COMPLETE — deleg_cd039c28]\n"
    "A background fan-out of 1 subagent(s) you dispatched earlier has "
    "finished. All ran in parallel and waited on each other; their "
    "consolidated results are below. You may have moved on since dispatching "
    "— act on these or re-dispatch if things have changed.\n\n"
    "Dispatched: 2026-06-25 00:41:11 (2m52s ago)\n"
    "[… body redacted: contained environment credentials …]"
)
_BG_PROC = (
    "[IMPORTANT: Background process proc_782cc6eda99c completed normally "
    "(exit code 0).\n"
    "Command: cd " + HERMES_HOME + " && sleep 120 && "
    "grep -E \"v3core.observer|v3core.llm\" logs/agent.log | tail -12\n"
    "Output:\n"
    "last_qa_id: 247956 | updated_at: 2026-09-10T01:51:36.944756+00:00"
)
_BG_PROCS = (
    "[IMPORTANT: 8 background processes completed. Treat these results as one "
    "batch and give one consolidated response; preserve failures and "
    "actionable results.]"
)
_BG_SUBAGENTS = (
    "[IMPORTANT: 6 background subagent delegations completed for this session. "
    "Treat these results as one completion batch and send at most one "
    "consolidated user-facing response. If a result does not change the "
    "current conclusion, absorb it silently.]"
)
_OOB_STEER = (
    _OOB_WRAPPER_OPEN + "\n成人内容不要在电视上显示\n" + _OOB_WRAPPER_CLOSE
)
_OOB_STEER_2 = _OOB_WRAPPER_OPEN + "\n那就不管他了\n" + _OOB_WRAPPER_CLOSE
_SYS_MODEL_SWITCH = (
    "[System: The active model for this chat has changed to "
    "deepseek-v4.1-flash via provider opencode-go. From this point forward, "
    "use this runtime metadata when answering questions about what "
    "model/provider is active.]"
)
_SYS_CUT_OFF = (
    "[System: The previous response was cut off by a network error "
    "mid-stream. Continue exactly where you left off. Do not restart or "
    "repeat prior text. Finish the answer directly.]"
)
_CTRL_EMPTY = (
    "You just executed tool calls but returned an empty response. Please "
    "process the tool results above and continue with the task."
)
_CTRL_MAX_TOOLS = (
    "You've reached the maximum number of tool-calling iterations allowed. "
    "Please provide a final response summarizing what you've found and "
    "accomplished so far, without calling any more tools."
)
_CTRL_REJECTED = (
    "Your previous final response was rejected by the output contract "
    "validator. Validation errors:\n"
    "- Response is not valid JSON: Expecting value: line 1 column 2 (char 0)\n\n"
    "Reply with ONLY the corrected JSON object."
)
_CRON_JOB = (
    "[IMPORTANT: You are running as a scheduled cron job. DELIVERY: Your final "
    "response will be automatically delivered to the user — do NOT use "
    "send_message or try to deliver the output yourself. Just produce your "
    "report/output as your final response and the system handles the rest. "
    "SILENT: If there is genuinely nothing new to report, respond with exactly "
    "\"[SILENT]\" (nothing else) to suppress delivery. Never combine [SILENT] "
    "with content — either report your findings normally, or say [SILENT] and "
    "nothing more.]"
)
_CRON_SKILL = (
    "[IMPORTANT: The user has invoked the \"linuxdo-digest\" skill, indicating "
    "they want you to follow its instructions. The full skill content is "
    "loaded below.]"
)
_TITLE_GEN = (
    "### Task:\nSuggest 3-5 relevant follow-up questions or prompts that the "
    "user might naturally ask next in this conversation as a **user**, based "
    "on the chat history so far."
)
_JUDGE_PROMPT = (
    "# 公文易 文档理解 —— 语义评审员（LLM Judge）\n\n"
    "你是公文易（Word 公文格式排版工具）**研发阶段**的语义评审员。你的职责是"
    "发现当前「文档理解 / 结构识别 / 出件格式」中**用户明显能感知的**错误，"
    "而不是给一个总分。"
)

# ── named module-level constants (consumed by the §11 test matrix ``_fx``) ───
# The sibling acceptance test (``tests/test_p0c1_host_event_semantics.py``)
# resolves these names via ``_fx(...)`` so the real shapes win over its inline
# fallbacks. All values are the verbatim de-identified real shapes above.
CTX_COMPACTION = _CTX_COMPACTION
CTX_TASKLIST = _CTX_TASKLIST
CTX_STILL_IN_PROGRESS = _CTX_STILL_IN_PROGRESS
ASYNC_BATCH_COMPLETE = _ASYNC_BATCH
BG_PROC_COMPLETE = _BG_PROC
SYS_MODEL_SWITCH = _SYS_MODEL_SWITCH
SYS_CUT_OFF = _SYS_CUT_OFF
CTRL_EMPTY_RESPONSE = _CTRL_EMPTY
TITLE_GEN = _TITLE_GEN
CRON_JOB = _CRON_JOB
SKILL_INVOKED = _CRON_SKILL
JUDGE_PROMPT = _JUDGE_PROMPT
FAILED_TURN = FAILED_TURN_NOTICE = (
    "Your request was not processed. Send it again if you still want me to "
    "carry it out."
)
# ``hidden`` rows are real empty-content assistant rows (display_kind=hidden).
HIDDEN = ""
# NOTE: ``OUT_OF_BAND`` / ``OOB_STEER`` are intentionally NOT exposed as bare
# module constants. The acceptance test hard-codes its own OOB payload
# (``OOB_PAYLOAD = "改成 B"``) and pairs it with whatever wrapper ``_fx``
# returns; exposing a real wrapper here would desynchronise that pair. The real
# OOB shapes live in ``CASES`` (``out_of_band_steer``) and
# ``OOB_PAYLOAD_CASES`` (``oob_steer_payload`` / ``oob_steer_payload_2``).

# ── CASES: (name, role, content, display_kind, session_source, expect) ──────
CASES: list[dict] = [
    # ── the 12 required classes ────────────────────────────────────────────
    dict(
        name="ctx_compaction", role="assistant", content=_CTX_COMPACTION,
        display_kind=None, session_source=None, expect="CONTINUATION",
        provenance="state_db",
    ),
    dict(
        name="ctx_tasklist", role="user", content=_CTX_TASKLIST,
        display_kind=None, session_source=None, expect="CONTINUATION",
        provenance="state_db",
    ),
    dict(
        name="ctx_still_in_progress", role="user",
        content=_CTX_STILL_IN_PROGRESS, display_kind=None,
        session_source=None, expect="CONTINUATION", provenance="state_db",
    ),
    dict(
        name="async_batch_complete", role="user", content=_ASYNC_BATCH,
        display_kind=None, session_source=None, expect="ASYNC_BOUNDARY",
        provenance="state_db",
    ),
    dict(
        name="bg_proc_complete", role="user", content=_BG_PROC,
        display_kind="process_complete", session_source=None,
        expect="ASYNC_BOUNDARY", provenance="state_db",
    ),
    dict(
        name="out_of_band_steer", role="user", content=_OOB_STEER,
        display_kind="steer", session_source=None, expect="REAL_USER",
        provenance="state_db",
    ),
    dict(
        name="sys_model_switch", role="user", content=_SYS_MODEL_SWITCH,
        display_kind="model_switch", session_source=None, expect="CONTROL",
        provenance="state_db",
    ),
    dict(
        name="sys_cut_off", role="user", content=_SYS_CUT_OFF,
        display_kind=None, session_source=None, expect="CONTROL",
        provenance="state_db",
    ),
    dict(
        name="ctrl_empty_response", role="user", content=_CTRL_EMPTY,
        display_kind=None, session_source=None, expect="CONTROL",
        provenance="state_db",
    ),
    dict(
        name="cron_job", role="user", content=_CRON_JOB,
        display_kind=None, session_source="cron", expect="NEW_ROOT",
        provenance="state_db",
    ),
    dict(
        name="failed_turn_not_processed", role="assistant",
        content="Your request was not processed. Send it again if you still "
                "want me to carry it out.",
        display_kind="failed_turn", session_source=None, expect="TERMINAL",
        provenance="state_db",
    ),
    dict(
        name="hidden_empty", role="assistant", content="",
        display_kind="hidden", session_source=None, expect="TERMINAL",
        provenance="state_db",
    ),
    # ── required-class variants / extra real shapes ────────────────────────
    dict(
        name="failed_turn_incomplete", role="assistant",
        content="This turn did not complete. Some actions may already have "
                "run; verify their effects before resending.",
        display_kind="failed_turn", session_source=None, expect="TERMINAL",
        provenance="state_db",
    ),
    dict(
        name="bg_procs_complete", role="user", content=_BG_PROCS,
        display_kind="process_complete", session_source=None,
        expect="ASYNC_BOUNDARY", provenance="state_db",
    ),
    dict(
        name="bg_subagents_complete", role="user", content=_BG_SUBAGENTS,
        display_kind=None, session_source=None, expect="ASYNC_BOUNDARY",
        provenance="state_db",
    ),
    dict(
        name="cron_skill_invoked", role="user", content=_CRON_SKILL,
        display_kind=None, session_source="cron", expect="NEW_ROOT",
        provenance="state_db",
    ),
    dict(
        name="ctrl_max_tools", role="user", content=_CTRL_MAX_TOOLS,
        display_kind=None, session_source=None, expect="CONTROL",
        provenance="state_db",
    ),
    dict(
        name="ctrl_rejected", role="user", content=_CTRL_REJECTED,
        display_kind=None, session_source=None, expect="CONTROL",
        provenance="state_db",
    ),
    dict(
        name="title_gen", role="user", content=_TITLE_GEN,
        display_kind=None, session_source=None, expect="CONTROL",
        provenance="state_db",
    ),
    dict(
        name="judge_prompt_stays_real_user", role="user", content=_JUDGE_PROMPT,
        display_kind=None, session_source=None, expect="REAL_USER",
        provenance="state_db",
    ),
    dict(
        name="normal_user_prose", role="user", content="进展",
        display_kind=None, session_source=None, expect="REAL_USER",
        provenance="state_db",
    ),
    dict(
        name="normal_assistant", role="assistant",
        content="已核对这条延迟回执，不重新派工。",
        display_kind=None, session_source=None, expect="NORMAL_ASSISTANT",
        provenance="state_db",
    ),
    dict(
        name="normal_tool", role="tool",
        content='{"output": "3:from novel_engine.search import '
                '_collect_candidates, search_characters_by_keywords"}',
        display_kind=None, session_source=None, expect="NORMAL_TOOL",
        provenance="state_db",
    ),
    dict(
        name="terminal_operation_interrupted", role="assistant",
        content="Operation interrupted.",
        display_kind=None, session_source=None, expect="TERMINAL",
        provenance="state_db",
    ),
    dict(
        name="terminal_operation_interrupted_detail", role="assistant",
        content="Operation interrupted: waiting for model response "
                "(3.8s elapsed).",
        display_kind=None, session_source=None, expect="TERMINAL",
        provenance="state_db",
    ),
    dict(
        name="terminal_interrupted_by_correction", role="assistant",
        content="[This response was interrupted by a user correction.]",
        display_kind=None, session_source=None, expect="TERMINAL",
        provenance="state_db",
    ),
    dict(
        name="continuation_interrupted_midrun", role="user",
        content="[System note: Your previous turn was interrupted mid-run — "
                "the app or its backend process stopped before the turn could "
                "finish. Some of the work may already be complete; check the "
                "current state before redoing anything, then finish the task. "
                "The interrupted request was:]\n\n你是不是忘了我们之前做了很久的"
                "公文国标分析",
        display_kind="auto_continue", session_source=None,
        expect="CONTINUATION", provenance="state_db",
    ),
    # ── legacy coverage: timestamp-wrapped real forms ──────────────────────
    dict(
        name="subagent_context_timestamp_wrapped", role="user",
        content="Sat 2026-04-11 01:15 GMT+8] [Subagent Context] You are "
                "running as a subagent (depth 1/1). Results auto-announce to "
                "the parent when you finish.",
        display_kind=None, session_source=None, expect="CONTROL",
        provenance="state_db",
    ),
    dict(
        name="subagent_context_bracketed_timestamp", role="user",
        content="[Sun 2026-04-19 08:04 GMT+8] [Subagent Context] You are "
                "running as a subagent (depth 1/1). Results auto-announce to "
                "the parent.",
        display_kind=None, session_source=None, expect="CONTROL",
        provenance="state_db",
    ),
    dict(
        name="async_timestamp_wrapped", role="user",
        content="Wed 2026-09-30 09:00 GMT+8] [ASYNC DELEGATION BATCH COMPLETE "
                "— deleg_36bddac6]\nA background fan-out of 1 subagent(s) you "
                "dispatched earlier has finished.",
        display_kind=None, session_source=None, expect="ASYNC_BOUNDARY",
        provenance="legacy_pattern",
    ),
    dict(
        name="important_timestamp_wrapped", role="user",
        content="Mon 2026-10-04 12:00 GMT+8] [IMPORTANT: Background process "
                "proc_0123456789ab completed normally (exit code 0).",
        display_kind=None, session_source=None, expect="ASYNC_BOUNDARY",
        provenance="legacy_pattern",
    ),
    # ── legacy coverage: remaining _injection_pattern alternation ──────────
    dict(
        name="legacy_agent_rules", role="user",
        content="[AGENT_RULES v3] Always verify before claiming completion.",
        display_kind=None, session_source=None, expect="CONTROL",
        provenance="legacy_pattern",
    ),
    dict(
        name="legacy_retry_after", role="user",
        content="[Retry after 60s] Rate limited upstream; retry the request.",
        display_kind=None, session_source=None, expect="CONTROL",
        provenance="legacy_pattern",
    ),
    dict(
        name="legacy_cron_bracket", role="user",
        content="[cron:dee3ece99c15] scheduled run starting",
        display_kind=None, session_source=None, expect="NEW_ROOT",
        provenance="legacy_pattern",
    ),
    dict(
        name="legacy_system_bracket", role="user",
        content="[System] runtime context reset",
        display_kind=None, session_source=None, expect="CONTROL",
        provenance="legacy_pattern",
    ),
    dict(
        name="legacy_cjk_important_cron", role="user",
        content="【IMPORTANT: You are running as a scheduled cron job. "
                "DELIVERY: produce your report as your final response.】",
        display_kind=None, session_source=None, expect="NEW_ROOT",
        provenance="legacy_pattern",
    ),
    dict(
        name="legacy_cjk_async_delegation", role="user",
        content="【ASYNC DELEGATION BATCH COMPLETE — deleg_36bddac6】后台子代理"
                "已完成，结果如下。",
        display_kind=None, session_source=None, expect="ASYNC_BOUNDARY",
        provenance="legacy_pattern",
    ),
    dict(
        name="legacy_cjk_important_background", role="user",
        content="【IMPORTANT: 4 background processes completed. Treat these "
                "results as one batch.】",
        display_kind=None, session_source=None, expect="ASYNC_BOUNDARY",
        provenance="legacy_pattern",
    ),
    dict(
        name="legacy_cjk_important_other", role="user",
        content="【IMPORTANT: 系统提示，本条为宿主注入，不是用户发言。】",
        display_kind=None, session_source=None, expect="CONTROL",
        provenance="legacy_pattern",
    ),
]

# ── OOB payload extraction cases (§2.2) ─────────────────────────────────────
OOB_PAYLOAD_CASES: list[dict] = [
    dict(
        name="oob_steer_payload", content=_OOB_STEER,
        expect_payload="成人内容不要在电视上显示", provenance="state_db",
    ),
    dict(
        name="oob_steer_payload_2", content=_OOB_STEER_2,
        expect_payload="那就不管他了", provenance="state_db",
    ),
    dict(
        name="oob_timestamp_wrapped",
        content="Mon 2026-10-04 12:00 GMT+8] " + _OOB_WRAPPER_OPEN
                + "\n继续\n" + _OOB_WRAPPER_CLOSE,
        expect_payload="继续", provenance="legacy_pattern",
    ),
    dict(
        name="non_oob_passthrough", content="进展",
        expect_payload="进展", provenance="state_db",
    ),
    dict(
        name="non_oob_judge_passthrough", content=_JUDGE_PROMPT,
        expect_payload=_JUDGE_PROMPT, provenance="state_db",
    ),
]

# convenience maps
EXPECTED_BY_NAME = {c["name"]: c["expect"] for c in CASES}
CASES_BY_NAME = {c["name"]: c for c in CASES}
OOB_BY_NAME = {c["name"]: c for c in OOB_PAYLOAD_CASES}

# The 12 classes the task book requires the fixtures to cover, mapped to the
# representative case that exercises each one.
REQUIRED_CLASS_COVERAGE = {
    "CTX_COMPACTION": "ctx_compaction",
    "CTX_TASKLIST": "ctx_tasklist",
    "CTX_STILL_IN_PROGRESS": "ctx_still_in_progress",
    "ASYNC_BATCH_COMPLETE": "async_batch_complete",
    "BG_PROC_COMPLETE": "bg_proc_complete",
    "OUT_OF_BAND": "out_of_band_steer",
    "SYS_MODEL_SWITCH": "sys_model_switch",
    "SYS_CUT_OFF": "sys_cut_off",
    "CTRL_EMPTY_RESPONSE": "ctrl_empty_response",
    "CRON_JOB": "cron_job",
    "failed_turn": "failed_turn_not_processed",
    "hidden": "hidden_empty",
}
