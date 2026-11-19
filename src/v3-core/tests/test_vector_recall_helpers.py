from __future__ import annotations

import logging

import pytest

from v3core import vector_recall


class _CatalogCursor:
    def __init__(self, records):
        self.records = records
        self.statements = []
        self.last_statement = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        self.last_statement = str(statement)
        self.statements.append((self.last_statement, params))

    def fetchall(self):
        if self.last_statement.startswith("SELECT tbl.oid"):
            return self.records
        return []


class _CatalogConnection:
    def __init__(self, records, *, autocommit=False, transaction_status=0):
        self.records = records
        self.autocommit = autocommit
        self.transaction_status = transaction_status
        self.cursors = []

    def get_transaction_status(self):
        return self.transaction_status

    def cursor(self):
        cursor = _CatalogCursor(self.records)
        self.cursors.append(cursor)
        return cursor


def test_configure_small_dataset_sets_max_index_lists_and_restores_autocommit():
    conn = _CatalogConnection(
        [
            (10, 6, "CREATE INDEX a USING ivfflat (embedding vector_cosine_ops) WITH (lists='100')"),
            # The same table can have several IVFFlat indexes; its estimate is
            # counted once, while the maximum list count is selected.
            (10, 6, "CREATE INDEX b USING ivfflat (embedding vector_cosine_ops) WITH (lists=64)"),
            (11, 9, "CREATE INDEX c USING ivfflat (embedding vector_cosine_ops) WITH (lists=20)"),
        ]
    )

    vector_recall.configure_connection_recall(conn)

    statements = [statement for cursor in conn.cursors for statement, _ in cursor.statements]
    assert "SET ivfflat.probes = 100" in statements
    assert conn.autocommit is False


def test_configure_does_not_override_probes_for_large_dataset(monkeypatch):
    conn = _CatalogConnection([], autocommit=True)
    monkeypatch.setattr(vector_recall, "_ivfflat_stats", lambda _conn: (20_000, 100))

    vector_recall.configure_connection_recall(conn, small_rows=20_000)

    statements = [statement for cursor in conn.cursors for statement, _ in cursor.statements]
    assert not any(statement.startswith("SET ivfflat.probes") for statement in statements)


def test_configure_skips_connection_already_in_transaction():
    conn = _CatalogConnection([], autocommit=False, transaction_status=2)

    vector_recall.configure_connection_recall(conn)

    assert conn.cursors == []
    assert conn.autocommit is False


class _FallbackCursor:
    def __init__(self, connection):
        self.connection = connection
        self.statements = []
        self.query_count = 0
        self._rows = []
        self._row = None

    def execute(self, statement, _params=None):
        self.statements.append(str(statement))
        if str(statement).startswith("SELECT yin_version"):
            self.query_count += 1
            self._rows = [] if self.query_count == 1 else [("y1", "s1", "body", 0.9)]
        elif str(statement).startswith("SELECT EXISTS"):
            self._row = (True,)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class _FallbackConnection:
    autocommit = True

    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.transaction_status = 0

    def get_transaction_status(self):
        return self.transaction_status

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_empty_ann_retries_in_owned_transaction_and_warns_with_context(caplog):
    conn = _FallbackConnection()
    cur = _FallbackCursor(conn)
    sql = "SELECT yin_version FROM yin_paragraphs ORDER BY embedding <=> %s::vector LIMIT 3"

    with caplog.at_level(logging.WARNING, logger="v3core.vector_recall"):
        rows, used_fallback = vector_recall.query_with_exact_fallback(
            cur, sql, ("[]",), table="yin_paragraphs", context="unit-yin"
        )

    assert rows == [("y1", "s1", "body", 0.9)]
    assert used_fallback is True
    assert cur.statements == [
        "BEGIN",
        sql,
        "SET LOCAL enable_indexscan = off",
        sql,
        "SELECT EXISTS (SELECT 1 FROM yin_paragraphs)",
    ]
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert any("table=yin_paragraphs context=unit-yin" in record.getMessage() for record in caplog.records)


def test_fallback_does_not_commit_an_existing_transaction():
    conn = _FallbackConnection()
    conn.transaction_status = 2
    cur = _FallbackCursor(conn)
    sql = "SELECT yin_version FROM yin_paragraphs ORDER BY embedding <=> %s::vector LIMIT 3"

    rows, used_fallback = vector_recall.query_with_exact_fallback(
        cur, sql, ("[]",), table="yin_paragraphs", context="existing-tx"
    )

    assert rows
    assert used_fallback is True
    assert cur.statements[0] == sql
    assert conn.commits == 0


def test_fallback_rejects_untrusted_table_identifier_before_query():
    conn = _FallbackConnection()
    cur = _FallbackCursor(conn)

    with pytest.raises(ValueError, match="unsupported vector fallback table"):
        vector_recall.query_with_exact_fallback(
            cur,
            "SELECT 1",
            (),
            table="yin_paragraphs; DROP SCHEMA public CASCADE",
        )

    assert cur.statements == []


def test_connection_factory_wrapper_configures_new_connection(monkeypatch):
    created = object()
    configured = []
    monkeypatch.setattr(
        vector_recall,
        "configure_connection_recall",
        lambda conn: configured.append(conn),
    )

    wrapped = vector_recall.with_recall_configuration(lambda: created)

    assert wrapped() is created
    assert configured == [created]
