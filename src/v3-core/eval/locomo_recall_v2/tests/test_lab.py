"""Tests for the LoCoMo disposable-lab guard.

We do NOT touch PG. ``psycopg2`` is mocked out where the
SQL-shape path is exercised; the DSN guard and the schema
safety are pure stdlib.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# The worktree root (one level above _PKG_ROOT's ``src`` parent).
# _PKG_ROOT itself is the ``src/`` directory in the canonical worktree.
_WORKTREE_ROOT = os.path.normpath(os.path.join(_PKG_ROOT, ".."))

from eval.locomo_recall_v2 import lab  # noqa: E402


# ---------------------------------------------------------------------------
# DSN guard
# ---------------------------------------------------------------------------


class TestValidateDisposableDSN:
    def test_loopback_localhost_accepted(self):
        parts = lab.validate_disposable_dsn(
            "host=localhost dbname=lab port=5444 user=lab_user password=lab_pw"
        )
        assert parts["host"] == "localhost"
        assert parts["dbname"] == "lab"
        assert parts["port"] == "5444"

    def test_loopback_ipv4_accepted(self):
        parts = lab.validate_disposable_dsn(
            "host=127.0.0.1 dbname=lab user=lab"
        )
        assert parts["host"] == "127.0.0.1"

    def test_loopback_ipv6_accepted(self):
        parts = lab.validate_disposable_dsn(
            "host=::1 dbname=lab user=lab"
        )
        assert parts["host"] == "::1"

    def test_empty_dsn_refused(self):
        with pytest.raises(lab.LabDSNRefused):
            lab.validate_disposable_dsn("")

    def test_unparseable_dsn_refused(self):
        with pytest.raises(lab.LabDSNRefused):
            lab.validate_disposable_dsn("not a real dsn")

    def test_reserved_port_5433_refused(self):
        with pytest.raises(lab.LabDSNRefused) as exc:
            lab.validate_disposable_dsn(
                "host=localhost dbname=lab port=5433 user=lab"
            )
        assert "5433" in str(exc.value)

    def test_reserved_dbname_refused(self):
        with pytest.raises(lab.LabDSNRefused) as exc:
            lab.validate_disposable_dsn(
                "host=localhost dbname=v3embeddings user=lab"
            )
        assert "v3embeddings" in str(exc.value)

    def test_reserved_hostname_refused(self):
        with pytest.raises(lab.LabDSNRefused) as exc:
            lab.validate_disposable_dsn(
                "host=v3-pgvector dbname=lab user=lab"
            )
        assert "v3-pgvector" in str(exc.value)

    def test_non_loopback_host_refused(self):
        with pytest.raises(lab.LabDSNRefused) as exc:
            lab.validate_disposable_dsn(
                "host=db.internal dbname=lab user=lab"
            )
        assert "not loopback" in str(exc.value)

    def test_missing_host_refused(self):
        with pytest.raises(lab.LabDSNRefused) as exc:
            lab.validate_disposable_dsn(
                "dbname=lab user=lab"
            )
        assert "host is required" in str(exc.value)

    def test_password_not_echoed_in_error(self):
        secret = "supersecret-pw-12345"
        with pytest.raises(lab.LabDSNRefused) as exc:
            lab.validate_disposable_dsn(
                f"host=db.internal dbname=lab user=lab password={secret}"
            )
        assert secret not in str(exc.value)
        assert "***" in str(exc.value)


# ---------------------------------------------------------------------------
# Bootstrap schema safety (static)
# ---------------------------------------------------------------------------


class TestBootstrapStaticSafety:
    def test_actual_alpha_bootstrap_passes_static_check(self):
        # Locate the canonical schema dir.
        repo_root = _WORKTREE_ROOT
        sql = lab._compose_bootstrap_sql(repo_root)  # noqa: SLF001
        assert isinstance(sql, str)
        assert "CREATE TABLE" in sql

    def test_composed_sql_has_additive_eval_queries_ddl(self):
        # The composed bootstrap SQL MUST additively create the
        # ``eval_queries`` table (CREATE TABLE IF NOT EXISTS) with
        # the columns used by import_rows, plus a unique index on
        # ``source_id``. It must NOT carry destructive tokens
        # (verified by the static checker; defensive coverage).
        repo_root = _WORKTREE_ROOT
        sql = lab._compose_bootstrap_sql(repo_root)  # noqa: SLF001
        # The CREATE TABLE for eval_queries appears additively.
        assert "CREATE TABLE IF NOT EXISTS public.eval_queries" in sql
        # Every column used by import_rows is in the DDL.
        for col in (
            "source_id", "sample_id", "qa_id", "query_idx",
            "category", "question", "answer", "evidence", "source_hash",
        ):
            assert col in sql, f"eval_queries DDL missing column {col!r}"
        # The unique-source_id constraint is present.
        assert "eval_queries_source_id_key" in sql

    def test_composed_sql_has_no_destructive_tokens(self):
        repo_root = _WORKTREE_ROOT
        sql = lab._compose_bootstrap_sql(repo_root)  # noqa: SLF001
        # The composed SQL must not contain DROP/TRUNCATE/DELETE
        # outside of comments. The static checker strips comments
        # before scanning, so a plain substring check on the
        # full text is allowed to find comment-only mentions.
        # We use the same regex the loader uses.
        import re
        no_line = re.sub(r"--[^\n]*", " ", sql)
        no_block = re.sub(r"/\*.*?\*/", " ", no_line, flags=re.DOTALL)
        assert not lab._DESTRUCTIVE_RE.search(no_block)  # noqa: SLF001

    def test_destructive_sql_refused(self, tmp_path):
        # Build a tmp schema dir with a destructive explicit_memories
        # and confirm bootstrap refuses it. We pass ``tmp_path`` as
        # the repo_root; bootstrap resolves the schema under
        # ``repo_root/src/v3-core/schema/`` — so we lay out the
        # tmp tree accordingly.
        schema_dir = tmp_path / "src" / "v3-core" / "schema"
        schema_dir.mkdir(parents=True)
        (schema_dir / "alpha_bootstrap.sql").write_text(
            "-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/explicit_memories.sql <<<\n"
            "BEGIN;\n"
            "COMMIT;\n",
            encoding="utf-8",
        )
        (schema_dir / "explicit_memories.sql").write_text(
            "DROP TABLE IF EXISTS public.foo;\n",
            encoding="utf-8",
        )
        with pytest.raises(lab.LabSchemaError):
            lab._compose_bootstrap_sql(str(tmp_path))  # noqa: SLF001

    def test_missing_alpha_bootstrap_refused(self, tmp_path):
        # No schema dir at all → bootstrap fails closed.
        with pytest.raises(lab.LabSchemaError):
            lab._compose_bootstrap_sql(str(tmp_path))  # noqa: SLF001

    def test_include_marker_must_be_unique(self, tmp_path):
        schema_dir = tmp_path / "src" / "v3-core" / "schema"
        schema_dir.mkdir(parents=True)
        (schema_dir / "alpha_bootstrap.sql").write_text(
            "-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/explicit_memories.sql <<<\n"
            "-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/explicit_memories.sql <<<\n"
            "BEGIN;\nCOMMIT;\n",
            encoding="utf-8",
        )
        (schema_dir / "explicit_memories.sql").write_text(
            "-- empty\n",
            encoding="utf-8",
        )
        with pytest.raises(lab.LabSchemaError) as exc:
            lab._compose_bootstrap_sql(str(tmp_path))  # noqa: SLF001
        assert "include marker must appear exactly once" in str(exc.value)


# ---------------------------------------------------------------------------
# Bootstrap execution with a fake connection
# ---------------------------------------------------------------------------


class _RecordedCursor:
    """Records the SQL passed to ``execute``."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def close(self) -> None:
        self.closed = True


class _FakeConn:
    """Records commit/rollback calls and exposes one fake cursor."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.cursor_obj = _RecordedCursor()

    def cursor(self) -> _RecordedCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class TestBootstrapExecution:
    def test_executes_composed_sql_and_commits_once(self):
        repo_root = _WORKTREE_ROOT
        conn = _FakeConn()
        lab.bootstrap_schema(conn, repo_root)
        assert len(conn.cursor_obj.executed) == 1
        # The executed SQL must contain the explicit_memories
        # CREATE TABLE statement (proves the marker was replaced).
        assert "public.explicit_memories" in conn.cursor_obj.executed[0]
        assert conn.commits == 1
        assert conn.rollbacks == 0
        assert conn.cursor_obj.closed is True

    def test_cursor_failure_rolls_back(self):
        class _BrokenCursor(_RecordedCursor):
            def execute(self, sql: str) -> None:
                raise RuntimeError("boom")

        class _BrokenConn(_FakeConn):
            def __init__(self) -> None:
                super().__init__()
                self.cursor_obj = _BrokenCursor()

        repo_root = _WORKTREE_ROOT
        conn = _BrokenConn()
        with pytest.raises(lab.LabSchemaError):
            lab.bootstrap_schema(conn, repo_root)
        assert conn.rollbacks == 1


# ---------------------------------------------------------------------------
# import_rows SQL shape with a fake connection
# ---------------------------------------------------------------------------


class _InfoCursor:
    """Cursor that records every (sql, params) pair.

    Used by the import_rows table-existence + INSERT paths.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._result: list[tuple] | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))
        # First call from import_rows is the existence check;
        # we let ``fetchone`` return ``None`` so import_rows skips
        # the table (or returns True if the caller wants that).
        if "information_schema.tables" in sql:
            self._result = [(1,)]   # say the table exists
        else:
            self._result = None

    def executemany(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))

    def fetchone(self) -> tuple | None:
        return self._result

    def close(self) -> None:
        pass


class _ShapeConn:
    """Conn whose cursor() returns the same _InfoCursor each call.

    import_rows opens multiple cursors; we capture all of them
    in ``self.cursors`` so the test can assert on every (sql,
    params) pair.
    """

    def __init__(self) -> None:
        self.cursors: list[_InfoCursor] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _InfoCursor:
        c = _InfoCursor()
        self.cursors.append(c)
        return c

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class TestImportRowsSQLShape:
    def _basic_rows(self):
        return {
            "qa_pairs": [
                {
                    "source_id": "locomo|eval_v2|s|session_1|a>b",
                    "session_id": "s",
                    "turn_id": 0,
                    "question": "?",
                    "answer": "!",
                    "tool_calls": [],
                    "tool_results": [],
                    "timestamp": None,
                    "source": "live_buffer",
                    "embedding": None,
                    "embed_model": None,
                    "created_at": None,
                },
            ],
            "conversation_stream": [
                {
                    "session_id": "s",
                    "role": "user",
                    "content": "x",
                    "trigger": "live_buffer",
                    "turn_id": 0,
                    "timestamp": None,
                    "source": "live_buffer",
                    "embedding": None,
                    "tool_calls": [],
                    "tool_results": [],
                },
            ],
            "eval_queries": [
                {
                    "source_id": "locomo|eval_v2|s|session_1|a>b",
                    "sample_id": "s",
                    "qa_id": "q-1",
                    "query_idx": 0,
                    "category": "cat",
                    "question": "?",
                    "answer": "!",
                    "evidence": [],
                    "source_hash": "h",
                },
            ],
        }

    def test_insert_columns_are_explicit(self):
        conn = _ShapeConn()
        lab.import_rows(conn, self._basic_rows())
        # Collect every INSERT statement we issued.
        all_sqls = []
        for c in conn.cursors:
            for sql, _params in c.calls:
                all_sqls.append(sql)
        insert_sqls = [s for s in all_sqls if s.startswith("INSERT INTO public.")]
        assert len(insert_sqls) == 3
        for sql in insert_sqls:
            assert "*" not in sql
            assert "VALUES" in sql

    def test_qa_pairs_on_conflict(self):
        conn = _ShapeConn()
        lab.import_rows(conn, self._basic_rows())
        joined = "\n".join(
            sql for c in conn.cursors for (sql, _p) in c.calls
        )
        assert "INSERT INTO public.qa_pairs" in joined
        assert "ON CONFLICT (source_id) DO NOTHING" in joined

    def test_eval_queries_on_conflict(self):
        conn = _ShapeConn()
        lab.import_rows(conn, self._basic_rows())
        joined = "\n".join(
            sql for c in conn.cursors for (sql, _p) in c.calls
        )
        assert "INSERT INTO public.eval_queries" in joined
        assert "ON CONFLICT (source_id) DO NOTHING" in joined

    def test_eval_queries_insert_columns_include_query_idx(self):
        # After bootstrap, import_rows must accept eval_queries
        # rows carrying ``query_idx`` (no pre-existing table
        # demand). Verify the INSERT column list is explicit and
        # includes ``query_idx`` so we never silently drop the
        # positional identity of an eval question.
        conn = _ShapeConn()
        lab.import_rows(conn, self._basic_rows())
        joined = "\n".join(
            sql for c in conn.cursors for (sql, _p) in c.calls
        )
        eq_line = next(
            s for s in joined.split("\n")
            if s.startswith("INSERT INTO public.eval_queries")
        )
        # All required columns present in the explicit INSERT.
        for col in (
            "source_id", "sample_id", "qa_id", "query_idx",
            "category", "question", "answer", "evidence", "source_hash",
        ):
            assert col in eq_line, (
                f"eval_queries INSERT missing column {col!r}"
            )
        # No SELECT * expansion.
        assert "*" not in eq_line

    def test_eval_queries_table_existence_not_demanded_after_bootstrap(self):
        # After bootstrap, the ``eval_queries`` table exists; the
        # importer must accept eval_queries rows without
        # demanding a pre-existing-table shape. The fake conn
        # simulates that by returning (1,) for every
        # information_schema.tables lookup.
        conn = _ShapeConn()
        # ``import_rows`` proceeds with no error and writes the
        # eval_queries rows.
        lab.import_rows(conn, self._basic_rows())
        joined = "\n".join(
            sql for c in conn.cursors for (sql, _p) in c.calls
        )
        assert "INSERT INTO public.eval_queries" in joined
        # Commits once; does not roll back.
        assert conn.commits == 1
        assert conn.rollbacks == 0

    def test_conversation_stream_has_no_on_conflict(self):
        conn = _ShapeConn()
        lab.import_rows(conn, self._basic_rows())
        joined = "\n".join(
            sql for c in conn.cursors for (sql, _p) in c.calls
        )
        cs_line = next(
            s for s in joined.split("\n")
            if s.startswith("INSERT INTO public.conversation_stream")
        )
        assert "ON CONFLICT" not in cs_line

    def test_commits_once_and_zero_on_empty(self):
        conn = _ShapeConn()
        lab.import_rows(conn, {})
        assert conn.commits == 1
        assert conn.rollbacks == 0

    def test_missing_target_table_refused(self):
        class _EmptyCursor(_InfoCursor):
            def execute(self, sql: str, params: Any = None) -> None:
                super().execute(sql, params)
                if "information_schema.tables" in sql:
                    self._result = None   # pretend table does NOT exist

        class _EmptyConn(_ShapeConn):
            def cursor(self) -> _InfoCursor:
                c = _EmptyCursor()
                self.cursors.append(c)
                return c

        conn = _EmptyConn()
        with pytest.raises(lab.LabImportError):
            lab.import_rows(conn, self._basic_rows())
        assert conn.rollbacks == 0
        assert conn.commits == 0

    def test_row_dict_required(self):
        conn = _ShapeConn()
        with pytest.raises(lab.LabImportError):
            lab.import_rows(conn, {"qa_pairs": ["not a dict"]})