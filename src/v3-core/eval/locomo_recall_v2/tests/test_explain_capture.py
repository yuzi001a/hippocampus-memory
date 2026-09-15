"""Focused tests for the planner-evidence capture helper.

The fake connection returns canned EXPLAIN JSON so we can
exercise the tree-walking classifier:

  * A plan whose top node is ``Limit`` but contains an
    ``Index Scan`` with an ``ivfflat`` index name is classified
    as ``CLASS_IVFFLAT_INDEX_SCAN`` — the contract is "IVFFlat
    anywhere wins".
  * A plan with a top ``Seq Scan`` and no IVFFlat anywhere is
    classified as ``CLASS_SEQ_SCAN``.
  * A plan with neither Seq Scan nor an IVFFlat index scan
    falls through to ``CLASS_OTHER``.
"""
from __future__ import annotations

import json
import os
import sys

import pytest


_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval.locomo_recall_v2.explain_capture import (  # noqa: E402
    CLASS_IVFFLAT_INDEX_SCAN,
    CLASS_OTHER,
    CLASS_SEQ_SCAN,
    ExplainCaptureError,
    capture_explain,
)


# ---------------------------------------------------------------------
# Fake cursor / connection
# ---------------------------------------------------------------------


class FakeCursor:
    def __init__(self, plan_payload):
        self._plan_payload = plan_payload
        self.executed_sql: list[str] = []
        self.executed_params: list[tuple] = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed_sql.append(str(sql))
        self.executed_params.append(tuple(params) if params else ())

    def fetchone(self):
        # JSON-encode the payload so capture_explain exercises
        # the string→dict parse path that real psycopg2 produces.
        return (json.dumps(self._plan_payload),)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, plan_payload):
        self._payload = plan_payload
        self.cursors: list[FakeCursor] = []

    def cursor(self):
        cur = FakeCursor(self._payload)
        self.cursors.append(cur)
        return cur


def _ann_top5_plan():
    """A canonical PG plan: Limit -> Sort -> Index Scan (ivfflat)."""
    return [{
        "Plan": {
            "Node Type": "Limit",
            "Plan Rows": 5,
            "Plans": [
                {
                    "Node Type": "Sort",
                    "Plans": [
                        {
                            "Node Type": "Index Scan",
                            "Index Name": "qa_pairs_embedding_ivfflat",
                            "Relation Name": "qa_pairs",
                            "Plan Rows": 5,
                        }
                    ],
                },
            ],
        },
    }]


def _seq_scan_plan():
    """A plan where the IVFFlat plan was bypassed."""
    return [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "qa_pairs",
            "Plan Rows": 1,
        },
    }]


def _other_plan():
    """A plan with no Seq Scan and no IVFFlat index scan.

    A nested-loop join with two Index Scans (none of them
    ivfflat) is the canonical ``other`` shape.
    """
    return [{
        "Plan": {
            "Node Type": "Nested Loop",
            "Plans": [
                {
                    "Node Type": "Index Scan",
                    "Index Name": "qa_pairs_session_ts_idx",
                    "Relation Name": "qa_pairs",
                },
                {
                    "Node Type": "Index Only Scan",
                    "Index Name": "qa_pairs_pkey",
                    "Relation Name": "qa_pairs",
                },
            ],
        },
    }]


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


class TestCaptureExplain:
    def test_tree_walk_ivfflat_anywhere_wins(self):
        conn = FakeConnection(_ann_top5_plan())
        report = capture_explain(
            conn, sql="SELECT id FROM public.qa_pairs LIMIT 5",
            query_label="unit/ivfflat_top5",
            query_vector=[0.0] * 4,
        )
        assert report.top_node.classification == CLASS_IVFFLAT_INDEX_SCAN
        assert report.top_node.any_ivfflat_index_scan is True
        assert report.top_node.ivfflat_index_name == "qa_pairs_embedding_ivfflat"
        # The vector hash is 16 hex chars.
        assert isinstance(report.top_node.query_vector_sha256_16, str)
        assert len(report.top_node.query_vector_sha256_16) == 16
        # EXPLAIN options captured.
        assert "ANALYZE" in report.explain_options
        assert "BUFFERS" in report.explain_options

    def test_seq_scan_classification(self):
        conn = FakeConnection(_seq_scan_plan())
        report = capture_explain(
            conn, sql="SELECT id FROM public.qa_pairs",
            query_label="unit/seq_scan",
        )
        assert report.top_node.classification == CLASS_SEQ_SCAN
        assert report.top_node.any_ivfflat_index_scan is False

    def test_other_classification(self):
        conn = FakeConnection(_other_plan())
        report = capture_explain(
            conn, sql="SELECT * FROM a JOIN b ON a.id=b.id",
            query_label="unit/other",
        )
        assert report.top_node.classification == CLASS_OTHER

    def test_vector_hash_never_echoes_vector(self):
        """The vector must be hashed, never echoed in the record."""
        conn = FakeConnection(_ann_top5_plan())
        vec = [1.0, 2.0, 3.0, 4.0]
        report = capture_explain(
            conn, sql="SELECT id FROM public.qa_pairs LIMIT 5",
            query_label="unit/hash_only",
            query_vector=vec,
        )
        # Inspect every serialisable field.
        record = report.to_dict()
        blob = json.dumps(record)
        assert "1.0" not in blob
        assert "2.0" not in blob
        assert "3.0" not in blob
        assert "4.0" not in blob
        # The hash is still present.
        assert record["top_node"]["query_vector_sha256_16"] is not None

    def test_no_vector_means_no_hash(self):
        conn = FakeConnection(_ann_top5_plan())
        report = capture_explain(
            conn, sql="SELECT id FROM public.qa_pairs LIMIT 5",
            query_label="unit/no_vec",
            query_vector=None,
        )
        assert report.top_node.query_vector_sha256_16 is None

    def test_empty_sql_rejected(self):
        conn = FakeConnection(_ann_top5_plan())
        with pytest.raises(ExplainCaptureError):
            capture_explain(
                conn, sql="  ", query_label="unit/empty",
            )

    def test_query_vector_is_bound_as_parameter(self):
        """``capture_explain`` MUST forward ``query_vector``
        as a DB-API bound parameter so the raw vector never
        touches the SQL string and is never persisted.

        Real contract (PG ``EXPLAIN`` does NOT accept
        ``USING`` — that is ``PREPARE`` / ``EXECUTE``
        syntax):

          * caller supplies the complete ``SELECT`` with
            exactly one ``%s::vector`` placeholder;
          * helper composes ``EXPLAIN (options) <sql>``
            verbatim (no extra ``USING`` clause);
          * vector is bound via the DB-API ``params`` tuple.
        """

        conn = FakeConnection(_ann_top5_plan())
        vec = [0.1, 0.2, 0.3, 0.4]
        capture_explain(
            conn,
            sql="SELECT id FROM public.qa_pairs "
                "WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT 5",
            query_label="unit/ann_top5_bound",
            query_vector=vec,
        )
        cur = conn.cursors[0]
        assert len(cur.executed_sql) == 1
        issued_sql = cur.executed_sql[0]
        # EXPLAIN body is exactly the caller's SQL — NO
        # ``USING`` clause was injected.
        assert "USING" not in issued_sql.upper(), (
            "capture_explain must NOT append a USING clause; "
            "EXPLAIN does not accept that syntax. Got: "
            f"{issued_sql!r}"
        )
        # Exactly one ``%s::vector`` placeholder is in the
        # issued SQL — the caller's own.
        assert issued_sql.count("%s::vector") == 1, (
            "expected exactly one %s::vector placeholder (the "
            "caller's); got "
            f"{issued_sql!r}"
        )
        # The vector is bound as the first positional param
        # (the only param of the issued statement).
        assert cur.executed_params[0] == (vec,)
        # The vector literal never appears in the issued SQL.
        assert "0.1" not in issued_sql
        assert "0.2" not in issued_sql
        assert "0.3" not in issued_sql
        assert "0.4" not in issued_sql

    def test_no_query_vector_omits_placeholder(self):
        """When ``query_vector`` is ``None`` the EXPLAIN body
        carries the caller's SQL verbatim — no ``USING`` clause
        is appended.
        """

        conn = FakeConnection(_ann_top5_plan())
        capture_explain(
            conn,
            sql="SELECT count(*) FROM public.qa_pairs",
            query_label="unit/no_vec_bound",
            query_vector=None,
        )
        cur = conn.cursors[0]
        issued_sql = cur.executed_sql[0]
        assert "USING" not in issued_sql.upper()
        assert cur.executed_params[0] == ()

    def test_vector_hash_matches_bound_vector(self):
        """The vector hash is computed from the same bytes
        the cursor received as a bound parameter — an auditor
        can therefore confirm the planner saw exactly the
        vector whose hash is in the manifest.
        """

        conn = FakeConnection(_ann_top5_plan())
        vec = [0.5, -0.5, 0.25, -0.25]
        report = capture_explain(
            conn,
            sql="SELECT id FROM public.qa_pairs LIMIT 1",
            query_label="unit/ann_top1_bound",
            query_vector=vec,
        )
        # Recompute the expected hash the same way the
        # production helper does.
        import hashlib
        expected = hashlib.sha256(
            json.dumps(
                [float(x) for x in vec], separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()[:16]
        assert report.top_node.query_vector_sha256_16 == expected
