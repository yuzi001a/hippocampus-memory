"""B03 source-trace 引用解析契约：引擎自己打印的引用，读路径必须能解析。

缺陷（run C 隔离 E2E 复现）：recall 引擎在注入块里打印
``- qa_1 | ... [qa]``，但 ``PgEmbedStore.get_message_context()`` 只认
``conversation_stream.id`` 与 ``session_id``，``qa_1`` 落空 →
``source_id 未找到: qa_1``。同一行数据的规范 id 是
``qa_pairs.source_id = qa_sync/pi/b03-session-a/b03A-0001``。

本文件只锁契约：无 PG（conftest 硬封禁 psycopg2.connect，桩池是必须的），
且**不得改写注入块文本格式**，也不得用别的行 id 替换 ``qa_1``。
"""
from __future__ import annotations

from datetime import datetime

from v3core.pg_store import PgEmbedStore


# ── 桩：满足 PgEmbedStore.lease() 的最小契约 ────────────────────────────
# lease() 的实现是 pool.lease(timeout=...) → yield lease.connection → lease.close()
class _Cursor:
    def __init__(self, row):
        self.row, self.sql, self.params = row, None, None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


class _Lease:
    def __init__(self, conn):
        self.connection = conn

    def close(self):
        pass


class _Pool:
    def __init__(self, cur):
        self._cur = cur

    def lease(self, timeout=None):
        return _Lease(_Conn(self._cur))


# SELECT id, source_id, session_id, turn_id, question, answer, timestamp
_QA_ROW = (
    7,                                             # id
    "qa_sync/pi/b03-session-a/b03A-0001",          # source_id
    "b03-session-a",                               # session_id
    3,                                             # turn_id
    "synthetic B03 fact: the deploy window is on Thursdays",
    "Thursdays.",
    datetime(2026, 10, 2, 9, 15, 0),               # timestamp
)


def _store(row):
    """真 store + 桩池；桩只回一行，且只捕获**最后一次** execute。"""
    cur = _Cursor(row)
    return PgEmbedStore(config=None, pool=_Pool(cur)), cur


# ── qa_pairs 命名空间：引擎打印的两种形式都必须原样解析 ──────────────────
def test_qa_numeric_reference_resolves_qa_pairs_by_id():
    # qa_<row id>：recall_pool.py 的 f"qa_{row[0]}" 打印的形式
    store, cur = _store(_QA_ROW)
    out = store.get_message_context("qa_7")

    assert "FROM qa_pairs" in cur.sql
    assert "WHERE id = %s" in cur.sql
    assert cur.params == (7,)
    assert out is not None
    assert "synthetic B03 fact" in out["content"]
    assert "Thursdays." in out["content"]
    assert out["metadata"]["qa_id"] == 7
    # session_id 必须是该行自己的值，绝不从传入字符串编造
    assert out["metadata"]["session_id"] == "b03-session-a"


def test_qa_canonical_reference_resolves_qa_pairs_by_source_id():
    # 规范形式：qa_pairs.source_id 原样回显
    sid = "qa_sync/pi/b03-session-a/b03A-0001"
    store, cur = _store(_QA_ROW)
    out = store.get_message_context(sid)

    assert "FROM qa_pairs" in cur.sql
    assert "WHERE source_id = %s" in cur.sql
    assert cur.params == (sid,)
    assert out is not None
    assert out["metadata"]["qa_source_id"] == sid
    assert out["metadata"]["qa_id"] == 7


def test_missing_qa_row_falls_through_to_legacy_and_returns_none():
    # 缺失的 qa 行必须落回旧分支并以 None 结束，绝不能被当成 qa 成功
    store, cur = _store(None)
    out = store.get_message_context("qa_7")

    assert out is None
    assert "FROM qa_pairs" not in (cur.sql or "")
    assert "FROM conversation_stream" in (cur.sql or "")


# ── 旧命名空间逐字不变（回归锁） ────────────────────────────────────────
def test_legacy_numeric_input_still_resolves_conversation_stream_by_id():
    store, cur = _store((42, "assistant", "b03-session-a", 3, None, None))
    store.get_message_context("42")

    assert "FROM conversation_stream" in cur.sql
    assert "WHERE id = %s" in cur.sql
    assert cur.params == (42,)
    assert "FROM qa_pairs" not in cur.sql


def test_legacy_non_numeric_input_still_resolves_conversation_stream_by_session():
    store, cur = _store((42, "assistant", "b03-session-a", 3, None, None))
    store.get_message_context("some-session")

    assert "FROM conversation_stream" in cur.sql
    assert "WHERE session_id = %s" in cur.sql
    assert cur.params == ("some-session",)


# ── topic 命名空间：topic lane 打印的 f"topic_{tid}" 必须能解析 ──────────
def test_topic_namespaced_reference_strips_prefix_for_topics_lookup():
    store, cur = _store(("topic body",))
    out = store.get_card_by_source_id("topic_t_abc123")

    assert "FROM topics" in cur.sql
    assert "WHERE topic_id = %s" in cur.sql
    assert cur.params == ("t_abc123",)
    assert out == {"content": "topic body"}


def test_card_lookup_without_namespace_prefix_is_unchanged():
    store, cur = _store(("card body",))
    out = store.get_card_by_source_id("t_plain")

    assert "WHERE topic_id = %s" in cur.sql
    assert cur.params == ("t_plain",)
    assert out == {"content": "card body"}
