"""Contract tests for the OpenClaw-era legacy source importers.

Every fixture is a *tiny* artifact that keeps the real shape of the era it
represents, so the parser contracts (identity, noise, provenance) are exercised
without shipping the originals.

Covered shapes:
  normalized JSONL · lcm.db · trajectory · openclaw v3 jsonl ·
  honcho CSV · honcho pg_dump COPY · chat_md · renamed state.db backup
"""
from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from v3core.importers import KIND_RAW, IMPORTERS, get_importer
from v3core.importers.hermes_sessions import HermesSessionImporter
from v3core.importers.legacy_sources import (
    LegacySourcesImporter,
    classify_noise,
    detect_shape,
    parse_ts,
)


# ── helpers ────────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _items(path: Path) -> list:
    return list(LegacySourcesImporter().parse(path))


# ── registry / dispatch ────────────────────────────────────────────────────


def test_legacy_sources_registered():
    assert "legacy_sources" in IMPORTERS
    imp = get_importer("legacy_sources")
    assert imp.capability == "production"


def test_detect_shape_by_content_not_name(tmp_path: Path):
    db = tmp_path / "anything_at_all.bin"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE conversations (conversation_id TEXT);
        CREATE TABLE messages (message_id TEXT, conversation_id TEXT, seq INT,
                               role TEXT, content TEXT, created_at TEXT);
        """
    )
    conn.commit()
    conn.close()
    assert detect_shape(db) == "lcm_db"

    jl = _write(tmp_path / "x.jsonl", json.dumps({"session_id": "s", "role": "user", "content": "hi"}) + "\n")
    assert detect_shape(jl) == "normalized_jsonl"


# ── normalized JSONL (pipeline export contract) ───────────────────────────


def test_normalized_jsonl_identity_and_noise(tmp_path: Path):
    p = tmp_path / "raw_all.jsonl.gz"
    rows = [
        {"source": "honcho", "host": "honcho", "session_id": "honcho:s1", "event_id": "11",
         "ts": "2026-04-22T01:20:00Z", "role": "user", "content": "你好"},
        {"source": "honcho", "host": "honcho", "session_id": "honcho:s1", "event_id": "12",
         "ts": "2026-04-22T01:21:00Z", "role": "assistant", "content": "在"},
        {"source": "honcho", "host": "honcho", "session_id": "honcho:s1", "event_id": "13",
         "ts": "2026-04-22T01:22:00Z", "role": "user", "content": "[cron:nightly] 跑一下"},
    ]
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    items = _items(p)
    assert [i.role for i in items] == ["user", "assistant"]
    assert all(i.kind == KIND_RAW for i in items)
    assert {i.host for i in items} == {"honcho"}
    assert [i.source_ref for i in items] == ["honcho:s1", "honcho:s1"]
    assert [i.event_id for i in items] == ["11", "12"]
    assert items[0].occurred_at == "2026-04-22T01:20:00.000Z"
    assert items[0].identity_kind == "native"


# ── lcm.db ────────────────────────────────────────────────────────────────


def test_lcm_db(tmp_path: Path):
    db = tmp_path / "lcm.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE conversations (conversation_id TEXT, session_id TEXT, title TEXT);
        CREATE TABLE messages (message_id TEXT, conversation_id TEXT, seq INT,
                               role TEXT, content TEXT, created_at TEXT);
        INSERT INTO conversations VALUES ('c1', 'sess-1', 't');
        INSERT INTO messages VALUES ('m1','c1',1,'user','早','2026-06-04 10:00:00');
        INSERT INTO messages VALUES ('m2','c1',2,'assistant','早啊','2026-06-04 10:00:30');
        INSERT INTO messages VALUES ('m3','c1',3,'tool','{}','2026-06-04 10:00:40');
        """
    )
    conn.commit()
    conn.close()

    items = _items(db)
    assert [i.role for i in items] == ["user", "assistant"]
    assert items[0].event_id == "m1"
    assert items[0].source_ref == "lcm:c1"
    assert items[0].provenance["seq"] == 1


# ── trajectory event stream ───────────────────────────────────────────────


def test_trajectory_jsonl(tmp_path: Path):
    p = tmp_path / "sess.trajectory.jsonl"
    events = [
        {"type": "session.started", "data": {"sessionKey": "agent:main:dm:owner"}, "ts": "2026-05-24T02:00:00Z"},
        {"type": "prompt.submitted",
         "data": {"prompt": "[Sun 2026-05-24 10:00 GMT+8] 今天干嘛"},
         "ts": "2026-05-24T02:00:01Z"},
        {"type": "model.completed", "data": {"assistantTexts": ["跑重建"]}, "ts": "2026-05-24T02:00:05Z"},
    ]
    p.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")

    items = _items(p)
    assert [i.role for i in items] == ["user", "assistant"]
    assert items[0].text == "今天干嘛"
    assert items[0].source_ref == "trajectory:agent:main:dm:owner"
    assert all(i.host == "trajectory" for i in items)


def test_trajectory_ids_stable_across_daily_snapshots(tmp_path: Path):
    """daily_backups 是同一批 session 的每日快照 — 同一事件必须拿到同一个 id，
    否则 (host, session, event_id) 去重会把它当成新消息重复入库。"""
    events = [
        {"type": "session.started", "data": {"sessionKey": "agent:main:dm:owner"}, "ts": "2026-05-24T02:00:00Z"},
        {"type": "prompt.submitted", "data": {"prompt": "[Sun 2026-05-24 10:00 GMT+8] 今天干嘛"},
         "ts": "2026-05-24T02:00:01Z"},
        {"type": "model.completed", "data": {"assistantTexts": ["跑重建"]}, "ts": "2026-05-24T02:00:05Z"},
    ]
    body = "\n".join(json.dumps(e, ensure_ascii=False) for e in events)
    a = _write(tmp_path / "snap_a.trajectory.jsonl", body)
    b = _write(tmp_path / "snap_b.trajectory.jsonl", body)   # 同一内容，不同文件名

    ids_a = [(i.role, i.event_id) for i in _items(a)]
    ids_b = [(i.role, i.event_id) for i in _items(b)]
    assert ids_a == ids_b
    assert len({eid for _, eid in ids_a}) == len(ids_a)  # 文件内也不撞车


def test_trajectory_cron_session_is_skipped(tmp_path: Path):
    p = tmp_path / "cron.trajectory.jsonl"
    events = [
        {"type": "session.started", "data": {"sessionKey": "agent:main:cron:daily"}, "ts": "2026-05-24T02:00:00Z"},
        {"type": "model.completed", "data": {"assistantTexts": ["定时任务输出"]}, "ts": "2026-05-24T02:00:05Z"},
    ]
    p.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    assert _items(p) == []


# ── openclaw v3 session stream ────────────────────────────────────────────


def test_openclaw_v3_jsonl(tmp_path: Path):
    p = tmp_path / "session_20260508.jsonl.gz"
    events = [
        {"type": "session", "id": "s1"},
        {"type": "message", "timestamp": "2026-05-08T01:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "在吗"}]}},
        {"type": "message", "timestamp": "2026-05-08T01:00:05Z",
         "message": {"role": "assistant", "content": [{"type": "thinking", "text": "想"},
                                                     {"type": "text", "text": "在"}]}},
    ]
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    items = _items(p)
    assert [i.text for i in items] == ["在吗", "在"]
    assert items[1].host == "openclaw_jsonl"


def test_openclaw_checkpoint_snapshots_share_identity(tmp_path: Path):
    """每日备份把同一 session 反复快照，文件名带 checkpoint uuid。

    不去掉后缀 = 每个快照都是新 session（实测 3,897 文件 → 2,856 个"新" session）；
    去掉后同一句话在两个快照里必须落到同一个 (session, event_id)，否则去重失效。
    """
    events = [
        {"type": "message", "timestamp": "2026-05-08T01:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "在吗"}]}},
        {"type": "message", "timestamp": "2026-05-08T01:00:05Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "在"}]}},
    ]
    names = [
        "abc123.checkpoint.11111111-2222-3333-4444-555555555555.jsonl",
        "abc123.checkpoint.99999999-8888-7777-6666-555555555555.jsonl",
    ]
    got = []
    for n in names:
        p = tmp_path / n
        with open(p, "w", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        got.append(_items(p))

    assert {i.source_ref for i in got[0]} == {i.source_ref for i in got[1]} == {"openclaw_jsonl:abc123"}
    assert [i.event_id for i in got[0]] == [i.event_id for i in got[1]]
    assert len(set(i.event_id for i in got[0])) == 2


# ── honcho CSV ────────────────────────────────────────────────────────────


def test_honcho_host_aliases_unify_for_dedupe(tmp_path: Path):
    """同一个 Honcho 消息可能来自 CSV / pg_dump / PG 导出 — 三条路径必须落在同一
    个 host，否则 (host, session, event_id) 去重失效，同一句会被导入三遍。"""
    csv_p = _write(
        tmp_path / "honcho.csv",
        "id,public_id,content,created_at,peer_name,session_name,seq_in_session\n"
        "9,p9,同一句,2026-04-22T01:20:00Z,owner,s1,1\n",
    )
    norm_p = tmp_path / "honcho_dump.jsonl"
    norm_p.write_text(json.dumps({"host": "honcho_dump", "session_id": "honcho:s1", "event_id": "9",
                                  "ts": "2026-04-22T01:20:00Z", "role": "user", "content": "同一句"},
                                 ensure_ascii=False) + "\n", encoding="utf-8")
    a, b = _items(csv_p), _items(norm_p)
    assert a[0].host == b[0].host == "honcho"
    assert a[0].source_ref == b[0].source_ref == "honcho:s1"
    assert a[0].event_id == b[0].event_id == "9"


def test_honcho_csv(tmp_path: Path):
    p = _write(
        tmp_path / "honcho_messages.csv",
        "id,public_id,content,created_at,peer_name,session_name,seq_in_session\n"
        "1,p1,你好呀,2026-04-22T01:20:00Z,owner,agent-main-main-webchat,1\n"
        "2,p2,你好,2026-04-22T01:20:05Z,agent-main,agent-main-main-webchat,2\n",
    )
    items = _items(p)
    assert [i.role for i in items] == ["user", "assistant"]
    assert items[0].source_ref == "honcho:agent-main-main-webchat"
    assert items[0].event_id == "1"


# ── honcho pg_dump COPY ───────────────────────────────────────────────────


def test_honcho_sql_copy(tmp_path: Path):
    body = (
        "COPY public.messages (id, public_id, content, metadata, created_at, peer_name,"
        " workspace_name, session_name, token_count, internal_metadata, seq_in_session) FROM stdin;\n"
        "7\tp7\t第一行\\n第二行\t{}\t2026-05-11 10:00:00+00\towner\ty\tagent-main\t1\t{}\t1\n"
        "8\tp8\t收到\t{}\t2026-05-11 10:00:05+00\tagent-main\ty\tagent-main\t1\t{}\t2\n"
        "\\.\n"
    )
    p = _write(tmp_path / "honcho.sql", body)
    items = _items(p)
    assert len(items) == 2
    assert items[0].text == "第一行\n第二行"
    assert items[0].role == "user" and items[1].role == "assistant"


# ── markdown transcript ───────────────────────────────────────────────────


def test_chat_md(tmp_path: Path):
    p = _write(
        tmp_path / "chat_2026-04-10.md",
        "---\ndate: 2026-04-10\n---\n# 对话记录\n\n"
        "- 👤 **09:30**: 早上好\n"
        "- 🤖 **09:30**: 早\n"
        "- 👤 **09:31**: [cron:deep] 定时任务\n",
    )
    items = _items(p)
    assert [i.role for i in items] == ["user", "assistant"]
    assert items[0].text == "早上好"
    assert items[0].occurred_at.startswith("2026-04-10T09:30")


# ── hermes: renamed backup must be accepted ───────────────────────────────


def test_chat_md_section_heading_sets_time(tmp_path: Path):
    """Real chat_md files carry the clock on ``### HH:MM``, not on the line."""
    p = _write(
        tmp_path / "chat_2026-04-10.md",
        "---\ndate: 2026-04-10\n---\n# 对话记录\n\n### 08:00\n\n"
        "- 👤 早上好\n- 🤖 早\n",
    )
    items = _items(p)
    assert items[0].occurred_at.startswith("2026-04-10T08:00")
    assert items[1].occurred_at.startswith("2026-04-10T08:00")


def test_honcho_open_id_peer_is_the_user(tmp_path: Path):
    """``ou_*`` is a Feishu open_id (the human), not a bot peer."""
    p = _write(
        tmp_path / "honcho_messages.csv",
        "id,public_id,content,created_at,peer_name,session_name,seq_in_session\n"
        "1,p1,在吗,2026-04-22T01:20:00Z,ou_df4b156a62367c2061051339b1af58ca,s,1\n"
        "2,p2,在,2026-04-22T01:20:05Z,agent-main,s,2\n",
    )
    items = _items(p)
    assert [i.role for i in items] == ["user", "assistant"]


def test_hermes_importer_accepts_renamed_backup(tmp_path: Path):
    db = tmp_path / "state.db.bak-20260920-092212"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT, title TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                               content TEXT, timestamp REAL);
        INSERT INTO messages VALUES (1,'20260608_203917_eba4ec','user','你好',1780000000.0);
        INSERT INTO messages VALUES (2,'20260608_203917_eba4ec','assistant','在',1780000060.0);
        """
    )
    conn.commit()
    conn.close()

    items = list(HermesSessionImporter().parse(db))
    assert len(items) == 2
    assert items[0].source_ref == "20260608_203917_eba4ec"
    assert items[0].role == "user"


# ── helpers ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,reason",
    [
        ("[cron:nightly] 跑", "cron"),
        ("HEARTBEAT_OK", "heartbeat"),
        ("[CONTEXT COMPACTION — REFERENCE ONLY] …", "subagent"),
        ("", "empty"),
    ],
)
def test_classify_noise(text: str, reason: str):
    is_noise, got = classify_noise("user", text)
    assert is_noise and got == reason


def test_chinese_keywords_alone_do_not_drop_real_turns():
    """子代理 / 心跳 are ordinary vocabulary here — keyword-only matches must not
    delete a real turn (measured: 24 false positives in lcm.db, 11 in Honcho)."""
    for text in (
        "自然吸收即可。ignoreSessionPatterns [] 忽略自动存档的会话（定时任务 / 子代理）",
        "收到。VERIFY-CHECK-1357 已记。……你这串数字，是定时心跳还是真在测什么？",
    ):
        assert classify_noise("user", text) == (False, None)


def test_machine_cron_preambles_are_noise_but_mentions_are_not():
    """Hermes 把定时任务/后台通知包成 role=user 注入；这些是机器生成的，
    不是对话。但用户自己提到「cron job」的正常句子必须保留。"""
    from v3core.importers.legacy_sources import classify_noise

    for prefix in (
        "[IMPORTANT: You are running as a scheduled cron job. DELIVERY: ...",
        "[Your active task list was preserved across context compaction]",
        "[IMPORTANT: Background process proc_abc completed (exit code 0).",
        "You've reached the maximum number of tool-call iterations.",
        "Review transcript for durable skill updates.",
    ):
        assert classify_noise("user", prefix)[0] is True, prefix

    for real in (
        "这个 scheduled cron job 到底该不该停掉？",
        "你上次跑的那个后台进程完成了吗",
    ):
        assert classify_noise("user", real)[0] is False, real


def test_bare_tool_json_is_noise_but_real_json_is_not(tmp_path: Path):
    assert classify_noise("assistant", '{"action": "none"}') == (True, "tool_json")
    assert classify_noise("assistant", '{"name": "Alice", "age": 3}') == (False, None)


def test_parse_ts_variants():
    assert parse_ts(1780000000) is not None
    assert parse_ts("1780000000") is not None
    assert parse_ts("2026-05-11 10:00:00+00").isoformat().startswith("2026-05-11T10:00:00")
    assert parse_ts("2026-05-11T10:00:00Z").isoformat().startswith("2026-05-11T10:00:00")
    assert parse_ts("not a date") is None
