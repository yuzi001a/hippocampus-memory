#!/usr/bin/env python3
"""Generate the I01 Hermes state.db fixture (synthetic, real schema).

The schema mirrors the live Hermes state.db (introspected 2026-10-03):
messages/sessions tables with their real column names. Rows are synthetic
(no personal data) and cover every exclusion class I01 must handle:

  * normal user/assistant rows (kept)
  * [CONTEXT COMPACTION — REFERENCE ONLY] summary rows (excluded: content)
  * [System note: …] rows (excluded: content)
  * tool rows (excluded: role)
  * empty assistant rows w/ tool_calls (excluded: empty)
  * display_kind in {hidden, auto_continue, async_delegation_complete,
    process_complete, model_switch, failed_turn} (excluded: display_kind)
  * steer rows (KEPT — they carry a real user message)
  * archived history rows active=0/compacted=1 and folded rows 0/0 (KEPT)

Run:  python make_fixture.py           (writes ./state.db next to this file)
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "state.db"

MESSAGES_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    effect_disposition TEXT,
    api_content TEXT,
    display_kind TEXT,
    display_metadata TEXT,
    _compressed_summary INTEGER NOT NULL DEFAULT 0,
    display_identity BLOB,
    display_order INTEGER
)
"""

SESSIONS_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    compression_failure_cooldown_until REAL,
    compression_failure_error TEXT,
    display_name TEXT,
    origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    system_prompt_hash TEXT,
    last_activity_at REAL,
    last_activity_description TEXT,
    last_activity_provenance TEXT,
    compression_ineffective_count INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    git_metadata_generation INTEGER NOT NULL DEFAULT 0,
    title_source TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    last_read_at REAL,
    compression_recovery_deadline REAL,
    tool_names TEXT,
    transport_profile TEXT
)
"""

# Base epoch (2026-06-01T00:00:00Z) + N minutes for determinism.
BASE = 1780272000.0


def ts(minutes: float) -> float:
    return BASE + minutes * 60.0


def main() -> None:
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    cur.execute(MESSAGES_DDL)
    cur.execute(SESSIONS_DDL)

    for sid, title in (("sess-ginkgo-a", "银杏迁移项目"), ("sess-ginkgo-b", "部署操作")):
        cur.execute(
            "INSERT INTO sessions (id, source, started_at, title, message_count)"
            " VALUES (?, 'cli', ?, ?, 0)",
            (sid, ts(0), title),
        )

    rows = [
        # ── s1: normal exchange + every exclusion class ───────────────
        # (id auto, session, role, content, ts, display_kind, active, compacted, tool_calls)
        ("sess-ginkgo-a", "user", "我们内部把这次存储迁移叫作「银杏」，你记住一下。", ts(1), None, 1, 0, None),
        ("sess-ginkgo-a", "assistant", "记住了，代号是「银杏」。", ts(2), None, 1, 0, None),
        (
            "sess-ginkgo-a",
            "user",
            "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. "
            "This is a handoff from a previous context window — treat it as background reference.",
            ts(3),
            None,
            1,
            0,
            None,
        ),
        ("sess-ginkgo-a", "assistant", "上下文继续。还有什么要做的？", ts(4), None, 1, 0, None),
        ("sess-ginkgo-a", "tool", "tool result payload — must not be imported", ts(5), None, 1, 0, None),
        ("sess-ginkgo-a", "assistant", "", ts(6), None, 1, 0, json.dumps([{"id": "tc1", "type": "function"}])),
        ("sess-ginkgo-a", "assistant", "另外我把盘位表更新到第 3 版了。", ts(7), None, 0, 1, None),
        ("sess-ginkgo-a", "user", "好，继续。", ts(8), None, 0, 0, None),
        ("sess-ginkgo-a", "user", "（内部隐藏提示 — 不得导入）", ts(9), "hidden", 1, 0, None),
        ("sess-ginkgo-a", "assistant", "那我们下一步部署。", ts(10), None, 1, 0, None),
        (
            "sess-ginkgo-a",
            "user",
            "[ASYNC DELEGATION BATCH COMPLETE — deleg_fixture] A background fan-out of 2 subagent(s) finished.",
            ts(11),
            "async_delegation_complete",
            1,
            0,
            None,
        ),
        (
            "sess-ginkgo-a",
            "user",
            "[System: The active model for this chat has changed to fixture-model via provider fixture.]",
            ts(12),
            "model_switch",
            1,
            0,
            None,
        ),
        # ── s2: system note + steer (kept) + failed_turn (excluded) ──
        (
            "sess-ginkgo-b",
            "user",
            "[System note: Your previous turn was interrupted mid-run — the app or its backend process stopped.]",
            ts(20),
            "auto_continue",
            1,
            0,
            None,
        ),
        (
            "sess-ginkgo-b",
            "user",
            "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position]\n"
            "先停一下，别动配置。",
            ts(21),
            "steer",
            1,
            0,
            None,
        ),
        ("sess-ginkgo-b", "assistant", "好，停了。", ts(22), None, 1, 0, None),
        (
            "sess-ginkgo-b",
            "assistant",
            "Your request was not processed. Send it again if you still want me to carry it out.",
            ts(23),
            "failed_turn",
            1,
            0,
            None,
        ),
        # ── s1 continued: a process_complete notification (excluded) ──
        (
            "sess-ginkgo-a",
            "user",
            "[IMPORTANT: Background process proc_fixture completed normally (exit code 0).]",
            ts(13),
            "process_complete",
            1,
            0,
            None,
        ),
        ("sess-ginkgo-a", "assistant", "把验收记录归档一下。", ts(14), None, 1, 0, None),
    ]

    for sid, role, content, t, dk, active, compacted, tool_calls in rows:
        cur.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, display_kind,"
            " active, compacted, tool_calls) VALUES (?,?,?,?,?,?,?,?)",
            (sid, role, content, t, dk, active, compacted, tool_calls),
        )

    for sid in ("sess-ginkgo-a", "sess-ginkgo-b"):
        n = cur.execute(
            "SELECT count(*) FROM messages WHERE session_id=?", (sid,)
        ).fetchone()[0]
        cur.execute("UPDATE sessions SET message_count=? WHERE id=?", (n, sid))

    con.commit()
    con.close()
    print(f"wrote {DB} ({DB.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
