"""Real-PostgreSQL regression coverage for small-table ivfflat recall.

Opt in with HP_FIX_PG_DSN. The test creates an isolated schema in that database,
expands and applies the canonical alpha bootstrap, then drops only that schema.
"""
from __future__ import annotations

import logging
import math
import os
import random
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

_TEST_DSN = os.environ.get("HP_FIX_PG_DSN")
if not _TEST_DSN:
    pytest.skip("HP_FIX_PG_DSN not set", allow_module_level=True)

_parsed_dsn = urlparse(_TEST_DSN)
if (_parsed_dsn.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
    pytest.skip("HP_FIX_PG_DSN must target loopback PostgreSQL", allow_module_level=True)
if _parsed_dsn.port == 5433:
    pytest.skip("refusing to run this regression test against production port 5433", allow_module_level=True)

REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"
_INCLUDE_RE = re.compile(
    r"(?m)^--\s*>>>?\s*ALPHA_BOOTSTRAP_INCLUDE:\s*schema/([A-Za-z0-9_.-]+\.sql)\s*<<<\s*$"
)

_VECTOR_QUERY = (
    "SELECT yin_version, section, content, "
    "1 - (embedding <=> %s::vector) AS cosine "
    "FROM yin_paragraphs WHERE embedding IS NOT NULL "
    "ORDER BY embedding <=> %s::vector LIMIT %s"
)


def _connect_real():
    """Bypass the suite-wide psycopg2.connect guard for this opt-in local PG test."""
    import psycopg2._psycopg as _psycopg

    return _psycopg._connect(_TEST_DSN)


def _unit_vector(rng: random.Random) -> list[float]:
    values = [rng.gauss(0.0, 1.0) for _ in range(1024)]
    norm = math.sqrt(math.fsum(value * value for value in values))
    return [value / norm for value in values]


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def _expanded_alpha_sql(schema: str) -> str:
    sql_text = (_SCHEMA_DIR / "alpha_bootstrap.sql").read_text(encoding="utf-8")

    def _include(match: re.Match[str]) -> str:
        return (_SCHEMA_DIR / match.group(1)).read_text(encoding="utf-8")

    sql_text = _INCLUDE_RE.sub(_include, sql_text)
    # alpha_bootstrap intentionally targets public; a generated schema isolates
    # this test's complete bootstrap without mutating the shared public schema.
    return sql_text.replace("public.", f'"{schema}".')


@pytest.fixture(scope="module")
def small_yin_table():
    schema = f"d1_vector_recall_{uuid.uuid4().hex}"
    quoted_schema = f'"{schema}"'
    setup_conn = _connect_real()
    setup_conn.autocommit = True
    try:
        with setup_conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {quoted_schema}")
            cur.execute(f"SET search_path TO {quoted_schema}, public")
            cur.execute(_expanded_alpha_sql(schema))

            rng = random.Random(20261022)
            vectors = [_unit_vector(rng) for _ in range(6)]
            query_rng = random.Random(20261023)
            query_vectors = [_vector_literal(_unit_vector(query_rng)) for _ in range(64)]
            for idx, vector in enumerate(vectors):
                cur.execute(
                    "INSERT INTO yin_paragraphs "
                    "(yin_version, section, content, embedding) "
                    "VALUES (%s, %s, %s, %s::vector)",
                    (
                        f"d1-test-yin-{idx}",
                        f"section-{idx}",
                        f"test paragraph content {idx}",
                        _vector_literal(vector),
                    ),
                )
            cur.execute("ANALYZE yin_paragraphs")
        yield schema, query_vectors
    finally:
        setup_conn.close()
        cleanup_conn = _connect_real()
        cleanup_conn.autocommit = True
        try:
            with cleanup_conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE")
        finally:
            cleanup_conn.close()


def _prepare_ann_connection(conn, schema: str, *, probes: int | None = 1) -> None:
    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{schema}", public')
        if probes is not None:
            cur.execute(f"SET ivfflat.probes = {int(probes)}")
        # Make the test exercise the ivfflat path even on tiny tables where the
        # planner would otherwise prefer a sequential scan on cost alone.
        cur.execute("SET enable_seqscan = off")


def _ann_rows(cur, vector_literal: str):
    cur.execute(_VECTOR_QUERY, (vector_literal, vector_literal, 3))
    return cur.fetchall()


def _find_empty_ann_query(cur, candidates: list[str]) -> tuple[str, list]:
    for candidate in candidates:
        rows = _ann_rows(cur, candidate)
        if not rows:
            return candidate, rows
    raise AssertionError("no empty result found among deterministic small-table ANN probes")


def test_small_table_ann_reproduces_empty_rows_without_configuration(small_yin_table):
    schema, query_vectors = small_yin_table
    baseline = _connect_real()
    baseline.autocommit = True
    try:
        _prepare_ann_connection(baseline, schema, probes=1)
        with baseline.cursor() as cur:
            cur.execute("EXPLAIN " + _VECTOR_QUERY, (query_vectors[0], query_vectors[0], 3))
            plan = " ".join(str(row[0]) for row in cur.fetchall())
            assert "ivfflat" in plan.lower(), f"query did not use the expected ANN index: {plan}"
            _, baseline_rows = _find_empty_ann_query(cur, query_vectors)
        assert baseline_rows == [], "the unconfigured small-table ANN query should reproduce zero rows"
    finally:
        baseline.close()


def test_small_table_configuration_returns_three_ann_rows(small_yin_table):
    from v3core.vector_recall import configure_connection_recall

    schema, query_vectors = small_yin_table
    configured = _connect_real()
    try:
        configure_connection_recall(configured)
        _prepare_ann_connection(configured, schema, probes=None)
        with configured.cursor() as cur:
            cur.execute("SHOW ivfflat.probes")
            assert cur.fetchone()[0] == "100"
            configured_rows = _ann_rows(cur, query_vectors[0])
        assert len(configured_rows) == 3
    finally:
        configured.close()


def test_exact_fallback_recovers_rows_and_logs_nonempty_table(small_yin_table, caplog):
    from v3core.vector_recall import configure_connection_recall, query_with_exact_fallback

    schema, query_vectors = small_yin_table
    conn = _connect_real()
    conn.autocommit = True
    try:
        configure_connection_recall(conn)
        _prepare_ann_connection(conn, schema, probes=1)
        with conn.cursor() as cur, caplog.at_level(logging.WARNING, logger="v3core.vector_recall"):
            query_vector, baseline_rows = _find_empty_ann_query(cur, query_vectors)
            assert baseline_rows == []
            rows, used_fallback = query_with_exact_fallback(
                cur,
                _VECTOR_QUERY,
                (query_vector, query_vector, 3),
                table="yin_paragraphs",
                context="test-yin-vector",
            )
        assert len(rows) == 3
        assert used_fallback is True
        assert any(
            "yin_paragraphs" in record.getMessage()
            and "test-yin-vector" in record.getMessage()
            for record in caplog.records
        )
    finally:
        conn.close()


def test_large_table_estimate_leaves_ivfflat_probes_unchanged(small_yin_table, monkeypatch):
    from v3core import vector_recall

    conn = _connect_real()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET ivfflat.probes = 1")
        monkeypatch.setattr(vector_recall, "_ivfflat_stats", lambda _conn: (20_000, 100))
        vector_recall.configure_connection_recall(conn, small_rows=20_000)
        with conn.cursor() as cur:
            cur.execute("SHOW ivfflat.probes")
            assert cur.fetchone()[0] == "1"
    finally:
        conn.close()
