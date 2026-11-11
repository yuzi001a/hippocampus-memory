"""Session provenance — `sessions.source` must reach pairing.

为什么存在：批量评审器每个 case 单开一个会话，会话里只有 1 条 user（整段
12k–124k 字符的评审 prompt）+ 1 条 assistant（JSON 结论）。它没有任何机器
文本前缀，形态就是标准一问一答，所以所有基于文本的过滤器都拦不住——实测
1,060 对被当成普通问答。源库里其实有原生标识：`sessions.source`。

契约：
  * 导入器必须把 `sessions.source` 带到 ImportItem.session_source；
  * pairing 必须整会话跳过机器来源（tool/cron/oneshot/subagent）；
  * **没有** sessions 表的旧库保持旧行为（fail-open），不静默丢历史；
  * 显式传入的 excluded_session_sources 覆盖默认集合。
"""
from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest


def _make_state_db(path: Path, sessions: list[tuple[str, str]], rows: list[tuple]) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, timestamp REAL,
            message_uid TEXT
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, end_reason TEXT,
            message_count INTEGER, parent_session_id TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO sessions (id, source, end_reason, message_count) VALUES (?,?,?,?)",
        [(sid, src, "cli_close", 2) for sid, src in sessions],
    )
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp, message_uid) "
        "VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def _items(db: Path):
    mod = importlib.import_module("v3core.importers.hermes_sessions")
    imp = mod.HermesSessionImporter()
    imp.reset_excluded_counts()
    return list(imp.parse(db))


def _pairing():
    return importlib.import_module("v3core.importers.qa_pairing")


def test_importer_carries_session_source(tmp_path):
    db = _make_state_db(
        tmp_path / "state.db",
        sessions=[("sess-tool", "tool"), ("sess-human", "desktop")],
        rows=[
            ("sess-tool", "user", "# 评审员\n\n你是语义评审员。", 1.0, "u-tool-1"),
            ("sess-tool", "assistant", '{"verdict": "clean"}', 2.0, "u-tool-2"),
            ("sess-human", "user", "帮我看看这个排版", 3.0, "u-h-1"),
            ("sess-human", "assistant", "问题在页边距", 4.0, "u-h-2"),
        ],
    )
    items = _items(db)
    by_sess = {}
    for it in items:
        by_sess.setdefault(it.source_ref, set()).add(it.session_source)
    assert by_sess["sess-tool"] == {"tool"}
    assert by_sess["sess-human"] == {"desktop"}
    # provenance 也带上会话元数据，便于回查而不必再开一次 sqlite
    tool_item = next(i for i in items if i.source_ref == "sess-tool")
    assert tool_item.provenance["session_source"] == "tool"
    assert tool_item.provenance["session_end_reason"] == "cli_close"


def test_pairing_skips_machine_sessions(tmp_path):
    db = _make_state_db(
        tmp_path / "state.db",
        sessions=[("sess-tool", "tool"), ("sess-human", "desktop")],
        rows=[
            ("sess-tool", "user", "# 评审员\n\n你是语义评审员。", 1.0, "u-tool-1"),
            ("sess-tool", "assistant", '{"verdict": "clean"}', 2.0, "u-tool-2"),
            ("sess-human", "user", "帮我看看这个排版", 3.0, "u-h-1"),
            ("sess-human", "assistant", "问题在页边距", 4.0, "u-h-2"),
        ],
    )
    qa = _pairing()
    pairs = qa.pair_items(_items(db))
    assert [p.session_id for p in pairs] == ["sess-human"]
    assert pairs[0].question == "帮我看看这个排版"


def test_pairing_all_machine_sources_excluded(tmp_path):
    sources = ["tool", "cron", "oneshot", "subagent"]
    sessions = [(f"s-{s}", s) for s in sources]
    rows = []
    ts = 1.0
    for s in sources:
        rows.append((f"s-{s}", "user", f"{s} 派发的任务", ts, f"u-{s}-1"))
        rows.append((f"s-{s}", "assistant", "完成", ts + 0.5, f"u-{s}-2"))
        ts += 10
    db = _make_state_db(tmp_path / "state.db", sessions=sessions, rows=rows)
    assert _pairing().pair_items(_items(db)) == []


def test_fail_open_without_sessions_table(tmp_path):
    """旧库没有 sessions 表：不能因为缺元数据就把历史全丢掉。"""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, timestamp REAL
        );
        """
    )
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
        [("s-old", "user", "老库的问题", 1.0), ("s-old", "assistant", "老库的回答", 2.0)],
    )
    conn.commit()
    conn.close()
    items = _items(db)
    assert all(it.session_source is None for it in items)
    pairs = _pairing().pair_items(items)
    assert len(pairs) == 1 and pairs[0].question == "老库的问题"


def test_explicit_exclusion_overrides_default(tmp_path):
    db = _make_state_db(
        tmp_path / "state.db",
        sessions=[("s-desktop", "desktop")],
        rows=[
            ("s-desktop", "user", "正常提问", 1.0, "u-1"),
            ("s-desktop", "assistant", "正常回答", 2.0, "u-2"),
        ],
    )
    items = _items(db)
    qa = _pairing()
    assert len(qa.pair_items(items)) == 1
    assert qa.pair_items(items, excluded_session_sources=["desktop"]) == []
