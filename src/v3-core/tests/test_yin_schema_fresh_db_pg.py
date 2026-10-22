# -*- coding: utf-8 -*-
"""D2 regression tests for fresh-database yin schema and E1 dual writes."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

import pytest

import v3core.e1 as e1_mod


HP_FIX_PG_DSN = os.environ.get("HP_FIX_PG_DSN", "").strip()
_FAKE_KEY = "test-key-not-a-real-credential"
_TEST_YIN = (
    "## 关键决策\n这是一段足够长的合成印内容，用于验证新版本写入与检索。\n"
    "\n## 关键教训\n这是一段合成教训，用于验证印段落仍能正常写入。\n"
)
_TEST_IDENTITY = "我是合成占位的身份核心。我把长期脉络连起来，支持新对话延续。"
_TEST_SITUATION = (
    "## 活跃主题\n- 暂无新数据。\n\n"
    "## 当前关注与待办\n- 暂无新数据。\n\n"
    "## 状态速记\n- 暂无新数据。\n"
)


def _real_pg_connect():
    """Use the psycopg2 C-extension factory; conftest blocks the public alias."""
    try:
        from psycopg2._psycopg import connect as real_connect
    except ImportError:
        from psycopg2._psycopg import _connect as real_connect
    return real_connect(HP_FIX_PG_DSN)


def _require_safe_test_dsn() -> None:
    if not HP_FIX_PG_DSN:
        pytest.skip("HP_FIX_PG_DSN not set — real-PG test is opt-in")
    from psycopg2.extensions import parse_dsn

    parsed = parse_dsn(HP_FIX_PG_DSN)
    host = (parsed.get("host") or "localhost").lower()
    port = parsed.get("port", "")
    if host not in {"127.0.0.1", "::1", "localhost", "localhost.localdomain"}:
        pytest.skip(f"HP_FIX_PG_DSN host {host!r} is not loopback; refusing external DB")
    if port == "5433":
        pytest.skip("HP_FIX_PG_DSN targets production port 5433; refusing to run")


def _bootstrap_sql() -> str:
    from v3core.distribution_cli import _expand_alpha_include, _package_sql

    return _expand_alpha_include(_package_sql("alpha_bootstrap.sql"))


def test_yin_schema_bootstraps_on_real_fresh_pg_and_topic_recall_reads_it():
    """Bootstrap creates both tables; E1's current row is readable by recall."""
    _require_safe_test_dsn()
    conn = _real_pg_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_bootstrap_sql())
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.yin'), to_regclass('public.yin_history')"
            )
            yin_regclass, history_regclass = cur.fetchone()
        assert yin_regclass is not None
        assert history_regclass is not None

        old_version = "d2d5_test_old"
        new_version = "d2d5_test_new"
        old_content = "旧版本的测试印。"
        new_content = "新版本当前印，用于 topic_recall 的真实 PostgreSQL 读取。"
        with conn.cursor() as cur:
            # The fixture is disposable but may have been reused: move any prior
            # current row aside within this transaction, then prove E1's update
            # followed by insert semantics with two distinct versions.
            cur.execute("UPDATE public.yin SET is_current=false WHERE is_current=true")
            cur.execute(
                "INSERT INTO public.yin (version, title, content, created_at, is_current) "
                "VALUES (%s, %s, %s, NOW(), true)",
                (old_version, "old", old_content),
            )
            cur.execute(
                "INSERT INTO public.yin_history "
                "(version, title, content, created_at, is_current) "
                "VALUES (%s, %s, %s, NOW(), true)",
                (old_version, "old", old_content),
            )
            cur.execute("UPDATE public.yin SET is_current=false WHERE is_current=true")
            cur.execute(
                "INSERT INTO public.yin (version, title, content, created_at, is_current) "
                "VALUES (%s, %s, %s, NOW(), true)",
                (new_version, "new", new_content),
            )
            cur.execute(
                "INSERT INTO public.yin_history "
                "(version, title, content, created_at, is_current) "
                "VALUES (%s, %s, %s, NOW(), true)",
                (new_version, "new", new_content),
            )
            cur.execute(
                "SELECT version, is_current FROM public.yin "
                "WHERE version IN (%s, %s)",
                (old_version, new_version),
            )
            current_by_version = dict(cur.fetchall())
            cur.execute(
                "SELECT count(*) FROM public.yin_history WHERE version=%s",
                (new_version,),
            )
            assert cur.fetchone()[0] == 1

        assert current_by_version == {old_version: False, new_version: True}

        from v3core.topic_recall import TopicRecall

        recall = TopicRecall.__new__(TopicRecall)
        recall.match = lambda query: []

        @contextmanager
        def _lease():
            yield conn

        recall.lease = _lease
        recalled = recall.chain_recall("read the current yin")
        assert new_content in recalled["yin_context"]
        assert recalled["source"] == "yin_topic"
    finally:
        # DDL intentionally persists, while this test's version rows and the
        # temporary is_current flip are rolled back.
        try:
            conn.rollback()
        finally:
            conn.close()


class _RecordingCursor:
    def __init__(self, statements: list[str], failure: str | None = None):
        self.statements = statements
        self.failure = failure

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.statements.append(" ".join(str(sql).split()))
        if self.failure and self.failure in str(sql).lower():
            raise RuntimeError("permission denied creating public.yin")

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _RecordingConnection:
    def __init__(self, statements: list[str], failure: str | None = None):
        self.statements = statements
        self.failure = failure
        self.committed = False
        self.closed = False

    def cursor(self):
        return _RecordingCursor(self.statements, self.failure)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _fake_e1_client(monkeypatch):
    class FakeClient:
        model = "M3-test"
        provider = "test-provider"

        def chat(self, system, messages, temperature=0.7, max_output_tokens=None):
            if system.startswith(e1_mod.IDENTITY_SHORT_PROMPT):
                return _TEST_IDENTITY
            if system == e1_mod.SITUATION_OVERVIEW_PROMPT:
                return _TEST_SITUATION
            return _TEST_YIN

    client = FakeClient()
    monkeypatch.setattr(e1_mod, "LLMClient", lambda config: client)
    return client


def _fake_e1_config(base: Path) -> dict:
    return {
        "basePath": str(base),
        "e1": {"enabled": True, "confidence_threshold": 0.3},
        "llm": {
            "provider": "minimax",
            "model": "M3-test",
            "api_key": _FAKE_KEY,
            "base_url": "https://llm.invalid/v1",
        },
    }


def _patch_dual_write_connection(monkeypatch, conn):
    calls = 0

    def _get_conn(config=None, pool=None):
        nonlocal calls
        calls += 1
        # E1's first calls are optional radar/topic/observer reads; the fourth
        # is paragraph ingestion (disabled without embedding config); the fifth
        # is the main yin + yin_history dual write. The final call is overview IO.
        return conn if calls == 5 else None

    monkeypatch.setattr(e1_mod, "_get_cfg_pg_conn_compat", _get_conn)


def test_e1_dual_write_ensures_missing_tables_before_insert(tmp_path, monkeypatch):
    base = tmp_path / "profile"
    statements: list[str] = []
    conn = _RecordingConnection(statements)
    _fake_e1_client(monkeypatch)
    _patch_dual_write_connection(monkeypatch, conn)

    result = e1_mod.synthesize_yin(_fake_e1_config(base), pool=None)

    assert "E1 完成" in result
    normalized = [sql.lower() for sql in statements]
    create_yin = next(i for i, sql in enumerate(normalized) if "create table if not exists public.yin (" in sql)
    create_history = next(i for i, sql in enumerate(normalized) if "create table if not exists public.yin_history (" in sql)
    update_yin = next(i for i, sql in enumerate(normalized) if "update public.yin set is_current=false" in sql)
    insert_yin = next(i for i, sql in enumerate(normalized) if "insert into public.yin (" in sql)
    insert_history = next(i for i, sql in enumerate(normalized) if "insert into public.yin_history (" in sql)
    assert create_yin < update_yin < insert_yin
    assert create_history < insert_history
    assert conn.committed
    assert conn.closed


def test_e1_dual_write_failure_is_raised_and_keeps_the_written_yin_file(
    tmp_path, monkeypatch, caplog
):
    base = tmp_path / "profile"
    statements: list[str] = []
    conn = _RecordingConnection(statements, failure="create table if not exists public.yin (")
    _fake_e1_client(monkeypatch)
    _patch_dual_write_connection(monkeypatch, conn)

    with pytest.raises(RuntimeError, match="public.yin"):
        e1_mod.synthesize_yin(_fake_e1_config(base), pool=None)

    yin_files = list((base / "y").glob("y_*.md"))
    assert len(yin_files) == 1, "the local yin file must survive a PG dual-write failure"
    assert "关键决策" in yin_files[0].read_text(encoding="utf-8")
    assert conn.closed
    errors = [record.getMessage() for record in caplog.records if record.levelname == "ERROR"]
    assert any(
        yin_files[0].name in message
        and "public.yin" in message
        and "permission denied creating public.yin" in message
        for message in errors
    ), "the ERROR must identify the persisted file, table, and database error"
