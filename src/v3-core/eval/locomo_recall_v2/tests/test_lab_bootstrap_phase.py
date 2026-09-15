"""Focused tests for the deferred IVFFlat bootstrap helper and
the ANALYZE helper added by G6C-B0.

We do NOT touch PG. The fake connection records every cursor
``execute`` call so we can assert:

  * ``create_vector_indexes`` issues exactly the seven canonical
    ``CREATE INDEX IF NOT EXISTS`` statements in the locked
    order;
  * ``analyze_tables`` issues one ``ANALYZE`` per canonical
    table;
  * ``bootstrap_schema(index_build_phase='after_import')`` strips
    every IVFFlat ``CREATE INDEX`` statement from the
    bootstrap body;
  * ``bootstrap_schema(index_build_phase='bootstrap_before_import')``
    keeps every IVFFlat statement (default, byte-stable).
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest


_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval.locomo_recall_v2 import lab  # noqa: E402


# ---------------------------------------------------------------------
# Fake connection
# ---------------------------------------------------------------------


class _FakeCursor:
    def __init__(self):
        self.log: list[str] = []
        self.closed = False

    def execute(self, sql, params=None):
        self.log.append(str(sql))

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.cursors: list[_FakeCursor] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        cur = _FakeCursor()
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


class TestCreateVectorIndexes:
    def test_emits_all_seven_canonical_indexes(self):
        conn = FakeConnection()
        applied = lab.create_vector_indexes(conn)
        # ``create_vector_indexes`` opens exactly one cursor and
        # issues every CREATE INDEX on it; the cursor records
        # each statement in ``log``. Flatten + join to assert.
        flat_log = [stmt for cur in conn.cursors for stmt in cur.log]
        joined = "\n".join(flat_log)
        count = joined.count("CREATE INDEX IF NOT EXISTS")
        assert count == 7, f"expected 7 IVFFlat CREATE INDEX, got {count}"
        expected_tables = (
            "qa_pairs",
            "conversation_stream",
            "topics",
            "explicit_memories",
            "topic_entries",
            "observation_notes",
            "yin_paragraphs",
        )
        for t in expected_tables:
            assert t in joined, f"missing index DDL for {t!r}"
        # Applied table → index_name mapping covers all 7.
        assert set(applied.keys()) == set(expected_tables)
        assert conn.commits == 1


class TestAnalyzeTables:
    def test_default_tables_emit_analyze(self):
        conn = FakeConnection()
        applied = lab.analyze_tables(conn)
        log = conn.cursors[0].log
        # ANALYZE per canonical table (7 by default).
        assert len(log) == 7
        for table in (
            "qa_pairs", "conversation_stream", "topics",
            "explicit_memories", "topic_entries",
            "observation_notes", "yin_paragraphs",
        ):
            assert f"ANALYZE public.{table}" in log
        assert applied["qa_pairs"] == "ok"
        assert conn.commits == 1

    def test_custom_tables(self):
        conn = FakeConnection()
        applied = lab.analyze_tables(conn, tables=("qa_pairs",))
        log = conn.cursors[0].log
        assert log == ["ANALYZE public.qa_pairs"]
        assert applied == {"qa_pairs": "ok"}


class TestBootstrapPhase:
    """``bootstrap_schema`` builds the SQL via
    ``_compose_bootstrap_sql_with_phase``; that helper is
    testable without a real schema file because we read the
    alpha_bootstrap.sql + explicit_memories.sql from the
    repo root. We assert only the IVFFlat-strip behaviour."""

    def test_strip_ivfflat_removes_index_ddl(self):
        sql_with = (
            "-- header\n"
            "CREATE INDEX IF NOT EXISTS qa_pairs_embedding_ivfflat\n"
            "    ON public.qa_pairs\n"
            "    USING ivfflat (embedding vector_cosine_ops)\n"
            "    WITH (lists = 100);\n"
            "CREATE INDEX IF NOT EXISTS qa_pairs_session_ts_idx\n"
            "    ON public.qa_pairs (session_id, timestamp);\n"
            "CREATE INDEX IF NOT EXISTS topics_embedding_ivfflat\n"
            "    ON public.topics\n"
            "    USING ivfflat (embedding vector_cosine_ops)\n"
            "    WITH (lists = 100);\n"
        )
        stripped = lab._strip_ivfflat_from_sql(sql_with)
        # Both IVFFlat blocks gone.
        assert "qa_pairs_embedding_ivfflat" not in stripped
        assert "topics_embedding_ivfflat" not in stripped
        # Non-IVFFlat index kept.
        assert "qa_pairs_session_ts_idx" in stripped

    def test_strip_ivfflat_multiline_schema_shaped_block(self):
        """Real-world schema-shaped block where ``USING ivfflat``
        sits on a CONTINUATION line (not the ``CREATE INDEX``
        line).

        This is the exact shape the production schema files
        emit and the one the legacy "first-line contains
        IVFFLAT" logic missed: ``CREATE INDEX`` on line 1,
        ``USING ivfflat`` on line 3, ``WITH (lists = 100)``
        on line 4.  The block-level walker must drop the
        entire statement without breaking the
        adjacent non-IVFFlat ``CREATE INDEX`` block.
        """

        sql_with = (
            "-- =============== qa_pairs ===============\n"
            "CREATE INDEX IF NOT EXISTS qa_pairs_embedding_ivfflat\n"
            "    ON public.qa_pairs\n"
            "    USING ivfflat (embedding vector_cosine_ops)\n"
            "    WITH (lists = 100);\n"
            "\n"
            "-- =============== conversation_stream ===============\n"
            "CREATE INDEX IF NOT EXISTS conversation_stream_embedding_ivfflat\n"
            "    ON public.conversation_stream\n"
            "    USING ivfflat (embedding vector_cosine_ops)\n"
            "    WITH (lists = 100);\n"
            "\n"
            "-- =============== topics ===============\n"
            "CREATE INDEX IF NOT EXISTS topics_embedding_ivfflat\n"
            "    ON public.topics\n"
            "    USING ivfflat (embedding vector_cosine_ops)\n"
            "    WITH (lists = 100);\n"
            "\n"
            "-- =============== explicit_memories ===============\n"
            "CREATE INDEX IF NOT EXISTS explicit_memories_embedding_ivfflat\n"
            "    ON public.explicit_memories\n"
            "    USING ivfflat (embedding vector_cosine_ops)\n"
            "    WITH (lists = 100);\n"
            "\n"
            "-- =============== topic_entries ===============\n"
            "CREATE INDEX IF NOT EXISTS topic_entries_embedding_ivfflat\n"
            "    ON public.topic_entries\n"
            "    USING ivfflat (embedding vector_cosine_ops)\n"
            "    WITH (lists = 100);\n"
            "\n"
            "-- =============== observation_notes ===============\n"
            "CREATE INDEX IF NOT EXISTS observation_notes_embedding_ivfflat\n"
            "    ON public.observation_notes\n"
            "    USING ivfflat (embedding vector_cosine_ops)\n"
            "    WITH (lists = 100);\n"
            "\n"
            "-- =============== yin_paragraphs ===============\n"
            "CREATE INDEX IF NOT EXISTS yin_paragraphs_embedding_ivfflat\n"
            "    ON public.yin_paragraphs\n"
            "    USING ivfflat (embedding vector_cosine_ops)\n"
            "    WITH (lists = 100);\n"
            "\n"
            "-- =============== non-IVFFlat B-tree (KEEP) ===============\n"
            "CREATE INDEX IF NOT EXISTS qa_pairs_session_ts_idx\n"
            "    ON public.qa_pairs (session_id, timestamp);\n"
            "\n"
            "CREATE INDEX IF NOT EXISTS eval_queries_sample_idx\n"
            "    ON public.eval_queries (sample_id);\n"
        )
        stripped = lab._strip_ivfflat_from_sql(sql_with)
        # All seven canonical IVFFlat indexes are gone — by
        # name AND by their ``USING ivfflat`` clause.
        canonical_indexes = (
            "qa_pairs_embedding_ivfflat",
            "conversation_stream_embedding_ivfflat",
            "topics_embedding_ivfflat",
            "explicit_memories_embedding_ivfflat",
            "topic_entries_embedding_ivfflat",
            "observation_notes_embedding_ivfflat",
            "yin_paragraphs_embedding_ivfflat",
        )
        for name in canonical_indexes:
            assert name not in stripped, (
                f"multiline-schema stripper leaked {name!r}; "
                "the bug is back."
            )
        assert "USING ivfflat" not in stripped, (
            "stripper left a 'USING ivfflat' clause behind — block "
            "boundary detection is wrong."
        )
        # Non-IVFFlat indexes survive — neither their CREATE
        # INDEX nor their column list is touched.
        assert "qa_pairs_session_ts_idx" in stripped
        assert "eval_queries_sample_idx" in stripped
        assert "(session_id, timestamp)" in stripped
        assert "(sample_id)" in stripped
        # Surrounding SQL comments and blank lines are
        # preserved verbatim.
        assert "-- =============== qa_pairs ===============" in stripped
        assert "-- =============== yin_paragraphs ===============" in stripped

    def test_strip_ivfflat_preserves_block_without_ivfflat_keyword(self):
        """A multi-line ``CREATE INDEX`` block whose body
        does NOT contain ``USING ivfflat`` must be kept
        byte-stable.  This guards against over-eager
        block-level matching.

        Example shape::

            CREATE INDEX IF NOT EXISTS qa_pairs_session_idx
                ON public.qa_pairs
                USING btree (session_id);
        """

        sql_with = (
            "CREATE INDEX IF NOT EXISTS qa_pairs_session_idx\n"
            "    ON public.qa_pairs\n"
            "    USING btree (session_id);\n"
        )
        stripped = lab._strip_ivfflat_from_sql(sql_with)
        assert "qa_pairs_session_idx" in stripped
        assert "USING btree" in stripped
        # The non-IVFFlat block is preserved — content-wise
        # the stripper may drop a trailing newline (the
        # ``splitlines``/``join`` contract) so we compare on
        # lines rather than raw bytes.
        assert stripped.splitlines() == sql_with.splitlines()

    def test_after_import_strips_ivfflat_from_real_sql(self):
        # Use the real alpha_bootstrap.sql in the worktree.
        worktree = os.path.normpath(os.path.join(_PKG_ROOT, ".."))
        alpha = os.path.join(
            worktree, "src", "v3-core", "schema", "alpha_bootstrap.sql"
        )
        explicit = os.path.join(
            worktree, "src", "v3-core", "schema", "explicit_memories.sql"
        )
        if not (os.path.isfile(alpha) and os.path.isfile(explicit)):
            pytest.skip("alpha_bootstrap.sql / explicit_memories.sql missing")
        before = lab._compose_bootstrap_sql_with_phase(
            worktree, index_build_phase=lab.INDEX_BUILD_PHASE_BOOTSTRAP
        )
        after = lab._compose_bootstrap_sql_with_phase(
            worktree, index_build_phase=lab.INDEX_BUILD_PHASE_AFTER_IMPORT
        )
        # bootstrap_before_import retains all seven IVFFlat index DDL.
        canonical_indexes = (
            "qa_pairs_embedding_ivfflat",
            "conversation_stream_embedding_ivfflat",
            "topics_embedding_ivfflat",
            "explicit_memories_embedding_ivfflat",
            "topic_entries_embedding_ivfflat",
            "observation_notes_embedding_ivfflat",
            "yin_paragraphs_embedding_ivfflat",
        )
        for name in canonical_indexes:
            assert name in before, f"missing {name} in bootstrap_before_import"
        # after_import drops every IVFFlat statement.
        for name in canonical_indexes:
            assert name not in after, (
                f"unexpected {name} still present in after_import body"
            )
        # The non-IVFFlat index list keeps at least one entry.
        assert "qa_pairs_session_ts_idx" in after
