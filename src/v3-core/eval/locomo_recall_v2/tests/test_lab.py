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


# ---------------------------------------------------------------------------
# Vector coercion — G6C-A2 evaluator-only lab seam (vector import safety)
# ---------------------------------------------------------------------------


class TestSerializePgvectorLiteral:
    def _vec(self, n: int = 1024, seed: float = 0.1) -> list[float]:
        # Deterministic 1024-dim float list (no randomness — pure
        # stdlib, predictable output for the literal-form check).
        return [seed] * n

    def test_list_of_floats_serializes_to_pgvector_literal(self):
        v = self._vec()
        out = lab._serialize_pgvector_literal(v)
        assert out.startswith("[")
        assert out.endswith("]")
        # Round-trip parse — every element must be a finite float.
        body = out[1:-1]
        parts = body.split(",")
        assert len(parts) == 1024
        for p in parts:
            float(p)  # raises on malformed

    def test_tuple_accepted(self):
        v = tuple(self._vec())
        out = lab._serialize_pgvector_literal(v)
        assert out.startswith("[") and out.endswith("]")

    def test_pre_serialised_string_literal_passes_through(self):
        v = self._vec()
        literal = "[" + ",".join(repr(float(x)) for x in v) + "]"
        # _coerce_embedding accepts pre-serialised literals.
        assert lab._coerce_embedding(literal) == literal

    def test_preserves_none_for_structural_mode(self):
        # The whole point of the structural-mode import is that
        # ``None`` survives — the importer never invents a vector.
        assert lab._coerce_embedding(None) is None

    def test_wrong_dim_refused(self):
        with pytest.raises(lab.LabVectorError) as exc:
            lab._serialize_pgvector_literal([0.1] * 512)
        assert "length 512" in str(exc.value)
        assert "vector(1024)" in str(exc.value)

    def test_nan_refused(self):
        v = self._vec()
        v[7] = float("nan")
        with pytest.raises(lab.LabVectorError) as exc:
            lab._serialize_pgvector_literal(v)
        assert "not finite" in str(exc.value)

    def test_inf_refused(self):
        v = self._vec()
        v[3] = float("inf")
        with pytest.raises(lab.LabVectorError):
            lab._serialize_pgvector_literal(v)
        v[3] = float("-inf")
        with pytest.raises(lab.LabVectorError):
            lab._serialize_pgvector_literal(v)

    def test_non_numeric_refused(self):
        v = self._vec()
        v[5] = "0.1"  # type: ignore[assignment]
        with pytest.raises(lab.LabVectorError) as exc:
            lab._serialize_pgvector_literal(v)
        assert "not numeric" in str(exc.value)

    def test_bool_refused(self):
        v = self._vec()
        v[0] = True  # type: ignore[assignment]
        with pytest.raises(lab.LabVectorError):
            lab._serialize_pgvector_literal(v)

    def test_non_list_refused(self):
        with pytest.raises(lab.LabVectorError) as exc:
            lab._serialize_pgvector_literal("not a list")  # type: ignore[arg-type]
        assert "must be a list/tuple" in str(exc.value)

    def test_garbage_string_refused(self):
        # Pre-serialised string must look like a pgvector literal.
        with pytest.raises(lab.LabVectorError):
            lab._coerce_embedding("hello")


class TestVectorImportIntegration:
    """End-to-end: row → coerced payload → driver-bound SQL.

    Uses the same ``_ShapeConn`` fake as the SQL-shape tests so
    we can assert the importer actually serialises the vector
    and adds the ``::vector`` cast to the embedding placeholder.
    """

    def test_list_embedding_serialised_and_cast_applied(self):
        v = [0.125] * 1024
        rows = {
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
                    "embedding": v,         # list, not None
                    "embed_model": "test",
                    "created_at": None,
                },
            ],
        }
        conn = _ShapeConn()
        lab.import_rows(conn, rows)
        # Collect every SQL we issued; find the qa_pairs INSERT.
        all_sqls: list[str] = []
        bound_params: list[Any] = []
        for c in conn.cursors:
            for sql, params in c.calls:
                all_sqls.append(sql)
                bound_params.append(params)
        qa_lines = [s for s in all_sqls
                    if s.startswith("INSERT INTO public.qa_pairs")]
        assert len(qa_lines) == 1
        # The ::vector cast is present on the embedding placeholder.
        assert "%s::vector" in qa_lines[0]
        # Other columns keep the bare %s placeholder; the total
        # placeholder count equals the column count (one bare %s
        # for every non-vector column plus one ``%s::vector`` for
        # the embedding column).
        bare = qa_lines[0].count("%s") - qa_lines[0].count("%s::vector")
        casted = qa_lines[0].count("%s::vector")
        assert bare + casted == len(lab._QA_PAIRS_COLUMNS)
        assert casted == 1  # exactly one ::vector cast (the embedding)
        # The bound embedding cell is the pgvector literal string.
        emb_literal = "[" + ",".join(repr(float(x)) for x in v) + "]"
        # The first cursor's first executemany holds the row payload.
        for c in conn.cursors:
            for _sql, params in c.calls:
                if isinstance(params, list) and params and isinstance(params[0], tuple):
                    cells = params[0]
                    # Find the embedding column (column order is the
                    # canonical list).
                    emb_idx = lab._QA_PAIRS_COLUMNS.index("embedding")
                    assert cells[emb_idx] == emb_literal
                    return
        raise AssertionError("did not find executemany payload")

    def test_none_embedding_preserved_and_no_cast_overhead(self):
        # Structural-mode import: ``None`` survives intact, the
        # bound cell is Python ``None`` (psycopg2 maps NULL).
        conn = _ShapeConn()
        lab.import_rows(conn, self._basic_rows_for_none_embedding())

        all_params: list[Any] = []
        for c in conn.cursors:
            for _sql, params in c.calls:
                if isinstance(params, list) and params and isinstance(params[0], tuple):
                    all_params.append(params[0])
        assert all_params, "no executemany payload recorded"
        emb_idx = lab._QA_PAIRS_COLUMNS.index("embedding")
        for cells in all_params:
            assert cells[emb_idx] is None

    def _basic_rows_for_none_embedding(self):
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
        }

    def test_malformed_embedding_refused_before_driver(self):
        # A bad embedding must NEVER reach the driver — the
        # coercion layer must reject it before ``executemany``.
        rows = {
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
                    "embedding": [0.1] * 512,  # wrong dim
                    "embed_model": None,
                    "created_at": None,
                },
            ],
        }
        conn = _ShapeConn()
        with pytest.raises(lab.LabVectorError):
            lab.import_rows(conn, rows)
        # No commit / rollback — coercion failed before any
        # cursor work.
        assert conn.commits == 0

    def test_credentials_never_serialised_in_payload(self):
        # Defensive: even when the row carries a secret-looking
        # field, the bound payload must contain only coerced
        # column values. We assert the secret never leaks via
        # the executemany params.
        secret = "supersecret-pw-XYZ"
        rows = {
            "qa_pairs": [
                {
                    "source_id": "locomo|eval_v2|s|session_1|a>b",
                    "session_id": secret,    # not a credential column,
                    "turn_id": 0,            # but we still want to make
                    "question": secret,      # sure the secret round-trips
                    "answer": secret,        # through the coercion layer
                    "tool_calls": secret,    # only as TEXT, never as
                    "tool_results": [],      # a serialised parameter
                    "timestamp": None,       # outside the column list.
                    "source": "live_buffer",
                    "embedding": [0.5] * 1024,
                    "embed_model": None,
                    "created_at": None,
                    "password": secret,      # extra key — must be ignored
                    "dsn": secret,           # extra key — must be ignored
                },
            ],
        }
        conn = _ShapeConn()
        lab.import_rows(conn, rows)
        # Walk every captured payload cell. The secret string
        # SHOULD appear (because we put it in legitimate text
        # fields), but it MUST never appear OUTSIDE the column
        # list — the executemany ``params`` length is exactly the
        # column count, so a leaked credential would show up as
        # an extra trailing element in the bound tuple.
        for c in conn.cursors:
            for _sql, params in c.calls:
                if isinstance(params, list) and params and isinstance(params[0], tuple):
                    cells = params[0]
                    assert len(cells) == len(lab._QA_PAIRS_COLUMNS), (
                        "credentials / DSN leaked into the bound "
                        "parameter tuple (extra columns past the "
                        "canonical column list)"
                    )


# ---------------------------------------------------------------------------
# Provenance mapping — G6C-A2 evaluator-only helper
# ---------------------------------------------------------------------------


class TestBuildProvenanceMap:
    def _qa_rows(self):
        # Two qa_pairs rows mirroring the dataset.build_import_rows
        # contract: ``tool_calls[0] = {q_provenance, a_provenance, ...}``
        # and ``source_id`` in the canonical locomo|eval_v2 form.
        return [
            {
                "source_id": "locomo|eval_v2|s1|session_a|d1>d2",
                "tool_calls": [
                    {
                        "q_provenance": {"dia_id": "d1"},
                        "a_provenance": {"dia_id": "d2"},
                    },
                ],
            },
            {
                "source_id": "locomo|eval_v2|s1|session_a|d3>d4",
                "tool_calls": [
                    {
                        "q_provenance": {"dia_id": "d3"},
                        "a_provenance": {"dia_id": "d4"},
                    },
                ],
            },
        ]

    def _conv_rows(self):
        # Conversation rows carry dia_id provenance in
        # ``tool_calls[0].dia_id``; numeric id preserved as
        # ``"id"`` (BIGSERIAL assignment).
        return [
            {"id": 101, "tool_calls": [{"dia_id": "d2"}]},
            {"id": 102, "tool_calls": [{"dia_id": "d4"}]},
            {"id": 103, "tool_calls": [{"dia_id": "d_unmapped"}]},
        ]

    def test_conv_id_resolves_via_supplied_dia_map(self):
        dia_map = {"d2": "locomo|eval_v2|s1|session_a|d1>d2",
                   "d4": "locomo|eval_v2|s1|session_a|d3>d4"}
        m = lab.build_provenance_map(
            qa_pairs_rows=self._qa_rows(),
            conversation_stream_rows=self._conv_rows(),
            dia_id_to_source_id=dia_map,
        )
        assert m["conv_id_to_source_id"]["101"] == \
            "locomo|eval_v2|s1|session_a|d1>d2"
        assert m["conv_id_to_source_id"]["102"] == \
            "locomo|eval_v2|s1|session_a|d3>d4"
        # ``103`` is unmapped — counts must reflect that.
        assert "103" not in m["conv_id_to_source_id"]
        assert m["counts"]["conv_id_resolved"] == 2
        assert m["counts"]["conv_id_unresolved"] == 1

    def test_conv_id_resolves_via_inferred_dia_map(self):
        # No caller-supplied map → helper infers from qa_pairs.
        m = lab.build_provenance_map(
            qa_pairs_rows=self._qa_rows(),
            conversation_stream_rows=self._conv_rows(),
        )
        assert m["conv_id_to_source_id"]["101"] == \
            "locomo|eval_v2|s1|session_a|d1>d2"
        assert m["conv_id_to_source_id"]["102"] == \
            "locomo|eval_v2|s1|session_a|d3>d4"

    def test_qa_id_resolves_via_supplied_index(self):
        # The BIGSERIAL → source_id index is what the lab uses
        # to resolve ``qa_<n>`` candidates. Without it, the map
        # stays empty (and the count surfaces the gap).
        m = lab.build_provenance_map(
            qa_pairs_rows=self._qa_rows(),
            qa_id_index={1: "locomo|eval_v2|s1|session_a|d1>d2",
                         2: "locomo|eval_v2|s1|session_a|d3>d4"},
        )
        assert m["qa_id_to_source_id"]["qa_1"] == \
            "locomo|eval_v2|s1|session_a|d1>d2"
        assert m["qa_id_to_source_id"]["qa_2"] == \
            "locomo|eval_v2|s1|session_a|d3>d4"
        assert m["counts"]["qa_id_resolved"] == 2

    def test_qa_id_map_empty_without_index(self):
        # No ``qa_id_index`` → no resolution, but the counts
        # block still reports input sizes so the auditor can
        # detect the missing index.
        m = lab.build_provenance_map(qa_pairs_rows=self._qa_rows())
        assert m["qa_id_to_source_id"] == {}
        assert m["counts"]["qa_id_resolved"] == 0
        assert m["counts"]["qa_pairs_input"] == 2

    def test_topic_id_map_is_always_empty(self):
        # Topics are not the focus of this seam — the map is
        # returned empty so the caller can detect the gap
        # instead of silently inventing fake ids. The helper
        # does not accept a topic_id_to_source_id parameter
        # because the canonical contract is "topics are out of
        # scope" — the auditor can spot the gap via the empty
        # ``topic_id_to_source_id`` slot.
        m = lab.build_provenance_map(
            qa_pairs_rows=self._qa_rows(),
            conversation_stream_rows=self._conv_rows(),
        )
        assert m["topic_id_to_source_id"] == {}
        assert m["counts"]["topic_id_resolved"] == 0

    def test_counts_block_audits_every_dim(self):
        m = lab.build_provenance_map(
            qa_pairs_rows=self._qa_rows(),
            conversation_stream_rows=self._conv_rows(),
            qa_id_index={1: "locomo|eval_v2|s1|session_a|d1>d2"},
        )
        c = m["counts"]
        assert c["qa_pairs_input"] == 2
        assert c["conversation_stream_input"] == 3
        assert c["qa_id_resolved"] == 1
        assert c["conv_id_resolved"] == 2
        assert c["conv_id_unresolved"] == 1

    def test_credential_never_serialised(self):
        # The mapping is in-memory Python only — it must not
        # accidentally echo a DSN or password back into a value.
        secret = "supersecret-pw-XYZ"
        # We do NOT pass secrets; we pass plain dicts. The
        # helper's output must not include the secret string in
        # any value field. (The point is that the helper never
        # sees credentials in the first place.)
        m = lab.build_provenance_map(
            qa_pairs_rows=[
                {"source_id": "src|1", "tool_calls": [{"q_provenance": {"dia_id": "x"}}]}
            ],
            conversation_stream_rows=[],
            dia_id_to_source_id={"x": "src|1"},
        )
        for v in m["qa_id_to_source_id"].values():
            assert secret not in v
        for v in m["conv_id_to_source_id"].values():
            assert secret not in v

    def test_handles_missing_provenance_gracefully(self):
        # Some legacy rows may have empty tool_calls — the
        # helper must NOT crash, it just skips them.
        rows = [
            {"source_id": "src|a", "tool_calls": []},
            {"source_id": "src|b"},  # missing tool_calls key
            {"tool_calls": [{"q_provenance": {"dia_id": "d"}}]},  # missing source_id
        ]
        m = lab.build_provenance_map(qa_pairs_rows=rows)
        c = m["counts"]
        assert c["qa_pairs_input"] == 3
        assert c["qa_pairs_without_dia_id"] >= 2
        assert c["qa_pairs_without_source_id"] >= 1


class TestMapCandidateIds:
    def test_resolves_qa_id(self):
        m = lab.map_candidate_ids(
            ["qa_1", "qa_2"],
            qa_id_to_source_id={"qa_1": "src|1", "qa_2": "src|2"},
        )
        assert m["resolved"] == {"qa_1": "src|1", "qa_2": "src|2"}
        assert m["unresolved"] == []
        assert m["counts"]["resolved_qa"] == 2
        assert m["counts"]["input"] == 2

    def test_resolves_numeric_conv_id(self):
        m = lab.map_candidate_ids(
            ["101", "102"],
            qa_id_to_source_id={},
            conv_id_to_source_id={"101": "src|1", "102": "src|2"},
        )
        assert m["resolved"] == {"101": "src|1", "102": "src|2"}
        assert m["counts"]["resolved_conv"] == 2

    def test_resolves_topic_id(self):
        m = lab.map_candidate_ids(
            ["topic_7"],
            qa_id_to_source_id={},
            topic_id_to_source_id={"topic_7": "src|t"},
        )
        assert m["resolved"] == {"topic_7": "src|t"}
        assert m["counts"]["resolved_topic"] == 1

    def test_unresolved_candidate_kept_in_audit(self):
        m = lab.map_candidate_ids(
            ["qa_999", "9999", "topic_99", "garbage_id"],
            qa_id_to_source_id={"qa_1": "src|1"},
            conv_id_to_source_id={"100": "src|100"},
            topic_id_to_source_id={"topic_1": "src|t1"},
        )
        # All four inputs are unresolved — they surface in the
        # audit list so the caller can spot gaps. ``topic_99``
        # is a recognised shape (topic_<n>) but no entry exists
        # in topic_id_to_source_id, so it counts as ``unresolved``
        # rather than ``unknown_shape``. ``garbage_id`` is the
        # only truly unknown shape (no qa_/numeric/topic_ prefix).
        assert set(m["unresolved"]) == {"qa_999", "9999", "topic_99", "garbage_id"}
        assert m["counts"]["unresolved"] == 3   # qa_999, 9999, topic_99
        assert m["counts"]["unknown_shape"] == 1   # garbage_id

    def test_no_fake_source_id_invented(self):
        # The seam must NEVER invent a fake source_id for an
        # unresolved candidate — it returns the id unchanged
        # so the caller can detect the gap, not a silent match.
        m = lab.map_candidate_ids(
            ["qa_404"],
            qa_id_to_source_id={"qa_1": "src|1"},
        )
        assert m["resolved"] == {}
        assert "qa_404" in m["unresolved"]

    def test_input_validation(self):
        # None / empty / non-string inputs are surfaced as
        # unresolved without crashing. ``None`` is recorded as
        # the literal string ``"None"`` so the audit list stays
        # JSON-safe (we never surface Python ``None`` in a list
        # that may be serialised by the caller).
        m = lab.map_candidate_ids(
            [None, "", 42, "qa_1"],
            qa_id_to_source_id={"qa_1": "src|1"},
        )
        assert m["counts"]["input"] == 4
        assert m["resolved"] == {"qa_1": "src|1"}
        # ``None`` is normalised to the literal "None" so the
        # audit list stays string-only.
        assert "None" in m["unresolved"]
        assert "" in m["unresolved"]
        assert "42" in m["unresolved"]
        assert m["counts"]["unresolved"] == 3

    def test_credential_never_serialised_in_audit(self):
        secret = "supersecret-pw-XYZ"
        m = lab.map_candidate_ids(
            [secret],
            qa_id_to_source_id={"qa_1": secret},
        )
        # The secret never appears in ``resolved`` because
        # ``secret`` is not a valid qa_/numeric/topic_ shape —
        # it is surfaced as unresolved so the auditor sees the
        # raw input without leaking the secret as a "source".
        assert secret not in m["resolved"]
        assert secret in m["unresolved"]
        # And it never appears as a value produced by resolution.
        for v in m["resolved"].values():
            assert v != secret or v in ("qa_1",)