"""Existing-install upgrade: an old database, the formal entry, real PostgreSQL.

These are the regressions the 2026-10-19 incident demanded and that no offline
test can honestly provide:

  1. **only ``qa_pairs.merged_event_ids`` missing** — every other table and
     column present. The writer must be blocked, the doctor and the health check
     must say so, and the pending marker must survive;
  2. **stale ledger** — a ``v0.2`` row on a database that lacks everything added
     afterwards. A ledger row must never be read as "current schema";
  3. **the formal upgrade entry migrates an old database** — ``hippocampus
     upgrade --dry-run`` then ``--apply``, the same code path an operator runs,
     must converge the schema, keep existing rows byte-identical, and be a no-op
     on a second run.

Opt-in, and deliberately hard to point at anything real
-------------------------------------------------------
Set ``SCHEMA_CONTRACT_PG_DSN`` at a **disposable** database:

    SCHEMA_CONTRACT_PG_DSN=postgresql://user:pw@127.0.0.1:55434/hpmig

The fixture drops and recreates ``public``, so the safety gate refuses anything
that is not loopback, not a non-production port, or not named ``hpmig*``. These
tests never run in CI (no service) and never touch the Hermes host.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from urllib.parse import unquote, urlparse

import pytest

DSN_ENV = "SCHEMA_CONTRACT_PG_DSN"
DSN = os.environ.get(DSN_ENV, "")

LOOPBACK = {"127.0.0.1", "::1", "localhost", "localhost.localdomain"}
PRODUCTION_PORT = 5433
DB_PREFIX = "hpmig"


def _parse(dsn: str) -> dict:
    u = urlparse(dsn)
    return {
        "host": u.hostname or "",
        "port": u.port or 0,
        "database": (u.path or "").lstrip("/"),
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
    }


def _rejection(dsn: str) -> str:
    if not dsn:
        return "not set"
    parsed = _parse(dsn)
    if parsed["host"].lower() not in LOOPBACK:
        return f"host {parsed['host']!r} is not loopback"
    if parsed["port"] == PRODUCTION_PORT:
        return "port 5433 is the production PG port"
    if not parsed["database"].startswith(DB_PREFIX):
        return (
            f"database {parsed['database']!r} does not start with "
            f"{DB_PREFIX!r} — this fixture DROPS the public schema"
        )
    return ""


_REJECTION = _rejection(DSN)


def _real_connect(**kwargs):
    """The C-level connect, bypassing the tests-wide psycopg2 guard."""
    from psycopg2._psycopg import connect as _c_connect  # type: ignore
    return _c_connect(**kwargs)


@pytest.fixture()
def old_install(monkeypatch):
    """A database shaped like the pre-upgrade production install."""
    if not DSN:
        pytest.skip(f"{DSN_ENV} not set — real-PG upgrade cases are opt-in")
    if _REJECTION:
        pytest.skip(f"{DSN_ENV} rejected by the safety gate: {_REJECTION}")

    parsed = _parse(DSN)
    conn = _real_connect(
        host=parsed["host"], port=parsed["port"], dbname=parsed["database"],
        user=parsed["user"], password=parsed["password"], connect_timeout=5,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        assert cur.fetchone()[0].startswith(DB_PREFIX), "refusing a non-fixture database"
    conn.commit()

    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        for statement in _OLD_INSTALL_SQL:
            cur.execute(statement)
        for artifact in ("explicit_memories.sql", "qa_embedding_chunks.sql"):
            from v3core.distribution_cli import _package_sql
            cur.execute(_package_sql(artifact))
        cur.execute(
            "INSERT INTO public.schema_versions (version, description, applied_at) "
            "VALUES ('v0.2', 'additive existing-install upgrade', "
            "'2026-09-18T15:38:36+00:00')"
        )
        # Rows that must survive the upgrade untouched.
        cur.execute(
            "INSERT INTO public.qa_pairs (session_id, question, answer) VALUES "
            "('sess-old', '项目代号是什么', '北极星'), "
            "('sess-old', '第二问', '第二答')"
        )
        cur.execute(
            "INSERT INTO public.conversation_stream (session_id, role, content, trigger) "
            "VALUES ('sess-old', 'user', '项目代号是什么', 'live_buffer')"
        )
        cur.execute(
            "INSERT INTO public.topics (topic_id, title, summary, body) "
            "VALUES ('t_old', '旧主题', '摘要', '正文')"
        )
    conn.autocommit = False

    # The tests-wide guard blocks psycopg2.connect; every consumer
    # (schema_contract.probe via doctor / upgrade / health) goes through it.
    import psycopg2

    def _factory(*args, **kwargs):
        if args or not kwargs:
            return _real_connect(**parsed)
        kwargs.pop("database", None)
        return _real_connect(**{**kwargs, "dbname": parsed["database"]})

    monkeypatch.setattr(psycopg2, "connect", _factory)

    try:
        yield {"parsed": parsed, "conn": conn, "dsn": DSN}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


_OLD_INSTALL_SQL = (
    """
    CREATE TABLE public.qa_pairs (
        id BIGSERIAL PRIMARY KEY,
        session_id TEXT,
        question TEXT NOT NULL,
        answer TEXT,
        timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        embedding VECTOR(1024)
    )
    """,
    """
    CREATE TABLE public.conversation_stream (
        id BIGSERIAL PRIMARY KEY,
        session_id TEXT,
        role TEXT,
        content TEXT,
        trigger TEXT,
        turn_id INTEGER,
        timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        source TEXT,
        embedding VECTOR(1024),
        tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
        tool_results JSONB NOT NULL DEFAULT '[]'::jsonb
    )
    """,
    """
    CREATE TABLE public.topics (
        topic_id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        keywords TEXT[] NOT NULL DEFAULT '{}'::text[],
        embedding VECTOR(1024),
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE public.topic_entries (
        id BIGSERIAL PRIMARY KEY,
        topic_id TEXT NOT NULL,
        seq INTEGER NOT NULL DEFAULT 0,
        question TEXT NOT NULL DEFAULT '',
        answer TEXT NOT NULL DEFAULT '',
        source TEXT DEFAULT 'user',
        timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        embedding VECTOR(1024),
        message_id TEXT,
        confidence REAL DEFAULT 0.5
    )
    """,
    """
    CREATE TABLE public.observation_notes (
        id BIGSERIAL PRIMARY KEY,
        version TEXT NOT NULL,
        content TEXT NOT NULL,
        source_qa_range INT8RANGE,
        prev_id BIGINT,
        links JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        embedding VECTOR(1024)
    )
    """,
    """
    CREATE TABLE public.yin_paragraphs (
        id SERIAL PRIMARY KEY,
        yin_version TEXT,
        section TEXT,
        content TEXT,
        embedding VECTOR(1024),
        created_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE public.schema_versions (
        version TEXT PRIMARY KEY,
        description TEXT NOT NULL DEFAULT '',
        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        applied_by TEXT NOT NULL DEFAULT 'hippocampus upgrade'
    )
    """,
)


def _probe(conn):
    from v3core import schema_contract as sc
    return sc.probe(conn)


# ---------------------------------------------------------------------------
# 1 + 2: the shape of a stale install, and what every surface says about it
# ---------------------------------------------------------------------------
def test_old_install_is_not_ready_and_blocks_writes(old_install):
    status = _probe(old_install["conn"])

    assert not status.ready
    assert status.write_blocked, "a database that cannot take the QA INSERT must block writes"
    # The exact production gap.
    assert "merged_event_ids" in status.missing_columns["qa_pairs"]
    # The sidecars an old install never had.
    for table in ("memory_relations", "derived_memory_invalidations",
                  "assistant_deliveries", "observation_embedding_chunks",
                  "embedding_failures"):
        assert table in status.missing_tables
    # The ledger is there but does not describe this schema.
    assert status.ledger_table_present
    assert status.missing_ledger_versions == ("v0.3", "v0.4")


def test_only_merged_event_ids_missing_blocks_writes(old_install):
    """Variant: everything present except the one column the writer names."""
    from v3core import schema_contract as sc

    conn = old_install["conn"]
    conn.autocommit = True
    with conn.cursor() as cur:
        # Bring the install to full contract first, then remove exactly one
        # write-critical column — the narrowest possible production shape.
        assert _apply_via_cli(old_install) == 0
        cur.execute("ALTER TABLE public.qa_pairs DROP COLUMN merged_event_ids")
    conn.autocommit = False

    status = _probe(conn)
    assert not status.ready
    assert status.write_blocked
    assert status.missing_columns == {"qa_pairs": ("merged_event_ids",)}
    assert not status.missing_tables
    assert sc.load_contract().version in status.ledger_versions


def test_doctor_and_health_report_the_gap(old_install):
    from v3core import doctor_full
    from v3core import schema_contract as sc
    from v3core.reliability import health

    parsed = old_install["parsed"]
    schema = doctor_full._check_schema_version(parsed, 5.0)
    assert schema["status"] == "fail"
    assert "merged_event_ids" in schema["detail"] or any(
        "merged_event_ids" in cols
        for cols in schema["evidence"].get("missing_columns", {}).values()
    )

    migration = doctor_full._check_migration_state(parsed, 5.0)
    assert migration["status"] == "fail"
    assert {"v0.3", "v0.4"} <= set(migration["evidence"]["missing"])

    missing = health._missing_columns(
        old_install["conn"], sc.load_contract().required_columns()
    )
    assert "merged_event_ids" in missing["qa_pairs"]


# ---------------------------------------------------------------------------
# 3: the formal upgrade entry actually migrates the old database
# ---------------------------------------------------------------------------
def _run_cli(argv):
    from v3core import distribution_cli as dc

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = dc.main(argv)
    text = buf.getvalue()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {"_stdout": text}
    return code, payload


def _apply_via_cli(old_install):
    """dry-run → apply through the real CLI parser and apply path."""
    dsn = old_install["dsn"]
    code, plan = _run_cli(["upgrade", "--target", dsn, "--dry-run"])
    assert code in (0, 1), plan
    sha = plan["plan_sha256"]

    code, applied = _run_cli(
        ["upgrade", "--target", dsn, "--apply", "--confirm-plan-sha", sha]
    )
    assert code == 0, applied
    assert applied["applied"] is True
    return 0


def test_formal_upgrade_migrates_the_old_install(old_install):
    from v3core import schema_contract as sc

    conn = old_install["conn"]
    with conn.cursor() as cur:
        cur.execute("SELECT question, answer FROM public.qa_pairs ORDER BY id")
        before = cur.fetchall()

    _apply_via_cli(old_install)

    status = _probe(conn)
    assert status.ready, status.summary()
    assert not status.write_blocked
    assert status.missing_ledger_versions == ()
    assert sc.CONTRACT_VERSION in status.ledger_versions
    assert "v0.2" in status.ledger_versions  # history is preserved, not replaced

    # No data rewrite: the rows are byte-identical, and the new columns carry
    # their documented defaults rather than being backfilled with guesses.
    with conn.cursor() as cur:
        cur.execute("SELECT question, answer FROM public.qa_pairs ORDER BY id")
        assert cur.fetchall() == before
        cur.execute(
            "SELECT merged_event_ids, chars FROM public.qa_pairs ORDER BY id"
        )
        for merged, chars in cur.fetchall():
            assert merged == []
            assert chars is None
        cur.execute("SELECT count(*) FROM public.topics")
        assert cur.fetchone()[0] == 1


def test_upgrade_is_idempotent(old_install):
    _apply_via_cli(old_install)

    conn = old_install["conn"]
    with conn.cursor() as cur:
        cur.execute("SELECT question, answer FROM public.qa_pairs ORDER BY id")
        once = cur.fetchall()
        cur.execute("SELECT version FROM public.schema_versions ORDER BY version")
        ledger_once = cur.fetchall()

    _apply_via_cli(old_install)

    with conn.cursor() as cur:
        cur.execute("SELECT question, answer FROM public.qa_pairs ORDER BY id")
        assert cur.fetchall() == once
        cur.execute("SELECT count(*) FROM public.qa_pairs")
        assert cur.fetchone()[0] == len(once), "second run must not duplicate rows"
        cur.execute("SELECT version FROM public.schema_versions ORDER BY version")
        assert cur.fetchall() == ledger_once

    assert _probe(conn).ready
