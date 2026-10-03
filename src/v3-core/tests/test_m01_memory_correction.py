# -*- coding: utf-8 -*-
"""M01 canonical explicit-memory correction — RED→GREEN contract tests.

Locked contract (docs/M01-M02-MEMORY-CORRECTION-DESIGN.md, base main 89e50a5):

  * ``public.explicit_memories`` payload is IMMUTABLE. A correction never
    UPDATEs content in place; it inserts a NEW active row plus one
    ``public.memory_relations`` edge and archives the old row — all inside a
    single transaction.
  * ``ActiveMemoryWriter.correct(memory_id, replacement_content, *,
    replacement_title=None, replacement_tags=None, correction_reason=None,
    correction_source_id=None, effective_at=None, authority=None) -> dict``
  * ``ActiveMemoryReader.read_version(memory_id, *, mode='current',
    include_history=False) -> dict``
  * authority is EXPLICITLY ``user_explicit``; inference / passive / ordinary
    writer paths never obtain it automatically.
  * Relation / replacement IDs are deterministic and target-bound, so an exact
    retry is idempotent (DEDUPLICATED) and A→B→A-content is a NEW version,
    never a cycle back to the original row.
  * A different request against a target that already has an outgoing edge is
    refused as CORRECTION_CONFLICT and reports the current successor; an
    unrelated archived target with no edge is TARGET_NOT_CURRENT; a missing
    relation table is MIGRATION_REQUIRED. Never last-write-wins, never a
    hidden fallback, never a second current successor.
  * Both schema copies stay byte-identical.

No real PostgreSQL: the P0-A conftest hard-blocks psycopg2.connect, so the
runtime contract is proven against a deterministic in-memory fake that models
leases, transaction snapshots, RETURNING row counts and statement ordering.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

MODULE = "v3core.active_memory_store"

REPO_ROOT = Path(__file__).resolve().parents[3]
V3CORE_SRC = REPO_ROOT / "src" / "v3-core" / "src" / "v3core"
REPO_SQL = REPO_ROOT / "src" / "v3-core" / "schema" / "memory_relations.sql"
PACKAGED_SQL = V3CORE_SRC / "schema" / "memory_relations.sql"

AUTHORITY = "user_explicit"
PROVENANCE = "explicit_tool_request"

_T0 = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(minutes=1)

MEMORY_COLS = (
    "memory_id", "category", "title", "content", "tags", "provenance",
    "status", "created_at", "updated_at", "embedding", "embed_model",
)
RELATION_COLS = (
    "relation_id", "relation_type", "from_memory_id", "to_memory_id",
    "correction_id", "authority", "correction_reason", "correction_source_id",
    "provenance", "effective_at", "recorded_at", "request",
)
# The INSERT omits recorded_at (it keeps its SQL DEFAULT NOW()), so the bind
# parameters map to every column except that one, in the writer's own order.
RELATION_INSERT_COLS = RELATION_COLS[:9] + ("effective_at", "request")


# ── deterministic in-memory fake PG ───────────────────────────────────────
#
# Models exactly what the writer / reader may rely on from the driver:
#   * lease() → lease object exposing .connection, with context-manager support
#   * BEGIN / COMMIT / ROLLBACK against a real pre-transaction snapshot
#   * INSERT/UPDATE ... RETURNING → rowcount + fetchone
#   * SELECT ... → fetchone / fetchall
#
# Failure injection (``missing_relations`` / ``fail_on``) is how the
# migration-required and rollback contracts are proven without a database.


def _norm(sql: str) -> str:
    return " ".join(sql.split()).upper()


class _FakeUniqueViolation(RuntimeError):
    """psycopg2-shaped unique-violation (SQLSTATE 23505).

    The real ``memory_relations`` table carries unique indexes on
    ``from_memory_id`` and ``to_memory_id``, so a second edge leaving the same
    memory raises exactly this. The writer must never surface it raw.
    """

    pgcode = "23505"

    def __init__(self, constraint: str, detail: str) -> None:
        super().__init__(
            f'duplicate key value violates unique constraint "{constraint}" '
            f"DETAIL: Key {detail} already exists"
        )


_MISSING = object()


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self.sql = ""
        self.params: tuple = ()
        self._one: Optional[tuple] = None
        self._many: list = []
        self.rowcount = -1

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_a: Any) -> bool:
        return False

    # -- statement classification ---------------------------------------
    @staticmethod
    def _op(sql: str) -> str:
        s = _norm(sql)
        if s in ("COMMIT", "ROLLBACK"):
            return s
        if s.startswith("BEGIN"):
            # including the reader's
            # "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
            return "BEGIN"
        if "INSERT INTO PUBLIC.MEMORY_RELATIONS" in s:
            return "insert_edge"
        if "INSERT INTO PUBLIC.EXPLICIT_MEMORIES" in s:
            return "insert_memory"
        if "FROM PUBLIC.CONVERSATION_STREAM" in s:
            return "read_source"
        if "UPDATE PUBLIC.EXPLICIT_MEMORIES" in s:
            return "archive" if "'ARCHIVED'" in s else "embed"
        if "FROM PUBLIC.MEMORY_RELATIONS" in s:
            return "read_edge"
        if "FROM PUBLIC.EXPLICIT_MEMORIES" in s:
            return "search_memory" if "ILIKE" in s else "read_memory"
        raise AssertionError(f"fake pg: unmapped SQL — {_norm(sql)[:140]}")

    def _guard(self, op: str) -> None:
        conn = self._conn
        conn.statements.append((op, _norm(self.sql)))
        if conn.missing_relations and op in {
            "read_edge", "insert_edge", "insert_memory", "archive", "read_memory",
        }:
            raise RuntimeError(
                'relation "public.memory_relations" does not exist'
            )
        if op in conn.fail_on:
            raise RuntimeError(f"synthetic failure at {op}")
    # -- execution -------------------------------------------------------
    def execute(self, sql: str, params: Any = ()) -> None:
        self.sql = sql
        self.params = tuple(params or ())
        self._one, self._many, self.rowcount = None, [], -1
        op = self._op(sql)
        self._guard(op)
        if op in ("BEGIN", "COMMIT", "ROLLBACK"):
            if (
                op == "BEGIN"
                and "ISOLATION LEVEL" in _norm(sql)
                and self._conn._txn_open
                and self._conn._txn_has_query
            ):
                # psycopg2 compiles "BEGIN ... ISOLATION LEVEL ..." into BEGIN
                # plus SET TRANSACTION ISOLATION LEVEL; the server refuses the
                # latter once any query has already run in the open transaction.
                # This is independent of autocommit: an explicit BEGIN opens a
                # transaction even in autocommit mode (where rollback() is a
                # silent no-op) — the exact defect being modelled.
                raise RuntimeError(
                    "SET TRANSACTION ISOLATION LEVEL must be called "
                    "before any query"
                )
            self._conn._txn(op)
            return
        if self._conn._txn_open:
            # any query inside an already-open transaction (including one an
            # explicit BEGIN opened in autocommit mode) makes it non-empty
            self._conn._txn_has_query = True
        elif not self._conn.autocommit:
            # a non-autocommit driver opens an implicit transaction on the
            # first query and keeps it open until COMMIT / ROLLBACK
            self._conn._txn_open = True
            self._conn._txn_has_query = True
        self._dispatch(op)

    def _dispatch(self, op: str) -> None:
        conn, p = self._conn, self.params
        up = _norm(self.sql)
        if op == "read_memory":
            row = conn.memories.get(p[0])
            if row is not None:
                self._one = tuple(row[c] for c in MEMORY_COLS)
        elif op == "search_memory":
            needle = str(p[0]).strip("%")
            rows = [
                r for r in conn.memories.values()
                if r["status"] == "active"
                and (
                    needle in r["title"] or needle in r["content"]
                    or any(needle in t for t in r["tags"])
                )
            ]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            limit = p[-1] if isinstance(p[-1], int) else 20
            self._many = [tuple(r[c] for c in MEMORY_COLS) for r in rows[:limit]]
        elif op == "insert_memory":
            if p[0] in conn.memories:
                # ON CONFLICT (memory_id) DO NOTHING: an existing row — for
                # instance one a concurrent winner just committed under the
                # same deterministic id — is left untouched and NO row is
                # returned, exactly as the server behaves.
                self._one = None
                self.rowcount = 0
                return
            conn._record_undo("memories", p[0])
            conn.memories[p[0]] = {
                "memory_id": p[0], "category": p[1], "title": p[2],
                "content": p[3], "tags": list(p[4] or []), "provenance": p[5],
                "status": "active", "created_at": _T0, "updated_at": _T0,
                "embedding": None, "embed_model": None,
            }
            self._one = (p[0],)
            self.rowcount = 1
        elif op == "archive":
            row = conn.memories.get(p[0])
            if row is not None and row["status"] == "active":
                conn._record_undo("memories", p[0])
                row["status"] = "archived"
                row["updated_at"] = _T1
                self._one = (p[0],)
                self.rowcount = 1
        elif op == "embed":
            row = conn.memories.get(p[2])
            if row is not None:
                conn._record_undo("memories", p[2])
                row["embedding"] = p[0]
                row["embed_model"] = p[1]
                self._one = (p[2],)
                self.rowcount = 1
        elif op == "read_edge":
            up = _norm(self.sql)
            if "WHERE RELATION_ID = %S" in up:
                # verification lookup: relation_id (+ from, + to when the
                # writer pins both endpoints)
                row = None
                for cand in conn.edges.values():
                    if cand["relation_id"] == p[0]:
                        row = cand
                        break
                if row is not None and len(p) >= 2 and row["from_memory_id"] != p[1]:
                    row = None
                if row is not None and len(p) >= 3 and row["to_memory_id"] != p[2]:
                    row = None
            elif "FROM_MEMORY_ID = %S" in up and "TO_MEMORY_ID = %S" not in up:
                row = conn.edges.get(("from", p[0]))
            else:
                row = conn.edges.get(("to", p[0]))
            if row is not None:
                self._one = tuple(row[c] for c in RELATION_COLS)
        elif op == "insert_edge":
            row = dict(zip(RELATION_INSERT_COLS, p))
            row["recorded_at"] = _T1
            for col in RELATION_COLS:
                row.setdefault(col, None)
            # The real table's unique indexes: a target has exactly ONE
            # outgoing edge, and a memory exactly one incoming one.
            if ("from", row["from_memory_id"]) in conn.edges:
                raise _FakeUniqueViolation(
                    "memory_relations_from_memory_id_key",
                    f"(from_memory_id)=({row['from_memory_id']})",
                )
            if row["to_memory_id"] is not None and (
                ("to", row["to_memory_id"]) in conn.edges
            ):
                raise _FakeUniqueViolation(
                    "memory_relations_to_memory_id_key",
                    f"(to_memory_id)=({row['to_memory_id']})",
                )
            conn._record_undo("edges", ("from", row["from_memory_id"]))
            conn.edges[("from", row["from_memory_id"])] = row
            # a withdrawal edge has to_memory_id NULL: the unique incoming
            # index admits many NULLs, so only a real target is indexed.
            if row["to_memory_id"] is not None:
                conn._record_undo("edges", ("to", row["to_memory_id"]))
                conn.edges[("to", row["to_memory_id"])] = row
            self._one = (row["relation_id"],)
            self.rowcount = 1
        elif op == "read_source":
            row = conn.messages.get(int(p[0]))
            if row is not None:
                self._one = (row["id"], row["role"])
        else:  # pragma: no cover
            raise AssertionError(op)

    def fetchone(self) -> Optional[tuple]:
        return self._one

    def fetchall(self) -> list:
        return list(self._many)


class _FakeConn:
    def __init__(self, db: "_FakeDb") -> None:
        self._db = db
        self._cursor = _FakeCursor(self)
        self._snapshot: Optional[dict] = None
        # driver-level implicit transaction state (non-autocommit mode)
        self._txn_open = False
        self._txn_has_query = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    @property
    def autocommit(self) -> bool:
        """Report the connection's autocommit mode (psycopg2-compatible)."""
        return self._db.autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        """psycopg2 lets a caller take/return ownership of the transaction."""
        self._db.autocommit = bool(value)

    @property
    def in_transaction(self) -> bool:
        """True while an explicit/implicit transaction is open."""
        return self._txn_open

    def get_transaction_status(self) -> int:
        """psycopg2-compatible real status: IDLE (0) or INTRANS (2).

        This is what the reader must trust instead of the autocommit flag: a
        transaction opened by an explicit BEGIN is open even while
        ``autocommit`` is True.
        """
        return 0 if not self._txn_open else 2

    @property
    def missing_relations(self) -> bool:
        return self._db.missing_relations

    @property
    def fail_on(self) -> set:
        return self._db.fail_on

    @property
    def memories(self) -> dict:
        return self._db.memories

    @property
    def edges(self) -> dict:
        return self._db.edges

    @property
    def messages(self) -> dict:
        return self._db.messages

    def _record_undo(self, kind: str, key: Any) -> None:
        """Remember the pre-transaction value of ``kind[key]`` for rollback.

        Only THIS transaction's own writes are undone: a concurrent session's
        committed write must never be rolled back by our ROLLBACK.
        """
        if self._snapshot is None:
            return
        store = self._snapshot.setdefault(kind, {})
        if key in store:
            return
        table = self.memories if kind == "memories" else self.edges
        current = table.get(key, _MISSING)
        store[key] = dict(current) if isinstance(current, dict) else current

    def _forget_undo(self, kind: str, key: Any) -> None:
        """A concurrent session now owns this row: it is not OURS to undo."""
        if self._snapshot is not None:
            self._snapshot.get(kind, {}).pop(key, None)

    def _txn(self, op: str) -> None:
        if op == "BEGIN":
            self._txn_open = True
            self._txn_has_query = False
            # An undo log of THIS transaction's own writes. A full-database
            # snapshot would wrongly roll back another session's committed
            # writes when two corrections race for one target.
            self._snapshot = {"memories": {}, "edges": {}}
        elif op == "COMMIT":
            self._snapshot = None
            self._txn_open = False
            self._txn_has_query = False
        elif op == "ROLLBACK":
            self._db.rollbacks += 1
            if self._snapshot is not None:
                for kind, store in self._snapshot.items():
                    table = (self._db.memories if kind == "memories"
                             else self._db.edges)
                    for key, prior in store.items():
                        if prior is _MISSING:
                            table.pop(key, None)
                        else:
                            table[key] = prior
            self._snapshot = None
            self._txn_open = False
            self._txn_has_query = False

    def commit(self) -> None:
        if self.autocommit:
            # psycopg2: commit() is a SILENT NO-OP in autocommit mode — it
            # sends nothing to the server and ends no transaction.
            return
        self._db.commits += 1
        self._db.statements.append(("COMMIT", "COMMIT"))
        self._snapshot = None
        self._txn_open = False
        self._txn_has_query = False

    def rollback(self) -> None:
        if self.autocommit:
            # psycopg2: rollback() is a SILENT NO-OP in autocommit mode — an
            # explicit ROLLBACK statement through a cursor is required there.
            return
        self._db.statements.append(("ROLLBACK", "ROLLBACK"))
        self._txn("ROLLBACK")

    @property
    def statements(self) -> list:
        return self._db.statements


class _FakeLease:
    def __init__(self, conn: "_FakeConn") -> None:
        self.connection = conn
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "_FakeConn":
        return self.connection

    def __exit__(self, *_a: Any) -> bool:
        self.close()
        return False


class _FakeDb:
    def __init__(
        self,
        *,
        memories: Optional[dict] = None,
        messages: Optional[dict] = None,
        missing_relations: bool = False,
        autocommit: bool = True,
    ) -> None:
        self.memories: dict = {k: dict(v) for k, v in (memories or {}).items()}
        self.edges: dict = {}
        self.messages: dict = dict(messages or {})
        self.missing_relations = missing_relations
        # psycopg2-style autocommit flag. True (the default) is the clean,
        # existing path; False models a driver that holds an implicit
        # transaction open across queries.
        self.autocommit = autocommit
        self.fail_on: set = set()
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0
        self.leases = 0
        self.conn = _FakeConn(self)

    def lease(self, timeout: Optional[float] = None):
        self.leases += 1
        return _FakeLease(self.conn)

    def ops(self) -> list:
        return [op for op, _ in self.statements]


class _Seed:
    """Seed a canonical row without going through the writer."""

    def __init__(self, db: _FakeDb) -> None:
        self.db = db

    def add(self, memory_id: str, content: str, *, category="ops", title="t",
            tags=("x",), status="active", provenance=None) -> str:
        self.db.memories[memory_id] = {
            "memory_id": memory_id, "category": category, "title": title,
            "content": content, "tags": list(tags),
            "provenance": dict(provenance or {"origin": "seed"}),
            "status": status, "created_at": _T0, "updated_at": _T0,
            "embedding": None, "embed_model": None,
        }
        return memory_id


def _mod():
    import importlib

    try:
        return importlib.import_module(MODULE)
    except Exception as exc:  # pragma: no cover - RED surface
        pytest.fail(f"cannot import {MODULE!r}: {exc!r}")


def _writer(db: _FakeDb, **kw: Any):
    return _mod().ActiveMemoryWriter(pool=None, pg=db, **kw)


def _reader(db: _FakeDb):
    return _mod().ActiveMemoryReader(pool=None, pg=db)


def _correct_a_to_b(db: _FakeDb, **kw: Any) -> dict:
    kw.pop("memory_id", None)
    content = kw.pop("replacement_content", "B content")
    return _writer(db).correct("mem_a", content, authority=AUTHORITY, **kw)


# ─────────────────────────── schema artifact ──────────────────────────────


def test_M01_schema_artifact_exists_in_both_copies():
    assert REPO_SQL.is_file(), f"missing canonical schema artifact {REPO_SQL}"
    assert PACKAGED_SQL.is_file(), f"missing packaged schema artifact {PACKAGED_SQL}"


def test_M01_schema_copies_are_byte_identical():
    assert REPO_SQL.read_bytes() == PACKAGED_SQL.read_bytes(), (
        "schema/memory_relations.sql drifted between the repo copy and the "
        "packaged v3core.schema copy — they must stay single-sourced"
    )


def _strip_sql_comments(sql: str) -> str:
    """Drop ``--`` comment lines so prose about DROP/TRUNCATE isn't mistaken
    for an actual destructive statement."""
    return "\n".join(
        line for line in sql.splitlines()
        if not line.lstrip().startswith("--")
    )


def test_M01_schema_contract_shape():
    raw = REPO_SQL.read_text(encoding="utf-8")
    up = raw.upper()
    assert "CREATE TABLE IF NOT EXISTS PUBLIC.MEMORY_RELATIONS" in up
    for col in (
        "RELATION_ID", "RELATION_TYPE", "FROM_MEMORY_ID", "TO_MEMORY_ID",
        "CORRECTION_ID", "AUTHORITY", "CORRECTION_SOURCE_ID", "PROVENANCE",
        "EFFECTIVE_AT", "RECORDED_AT", "REQUEST",
    ):
        assert col in up, f"memory_relations.sql missing column {col}"
    # relation_type / authority / provenance are pinned, not free text
    assert "RELATION_TYPE IN ('SUPERSEDES','WITHDRAWS')" in up or (
        "RELATION_TYPE IN ('SUPERSEDES', 'WITHDRAWS')" in up
    ), up[up.find("relation_type"):][:400]
    assert "'CORRECTS'" not in up, (
        "'corrects' is no longer a persisted relation type"
    )
    assert "'USER_EXPLICIT'" in up
    assert "'EXPLICIT_TOOL_REQUEST'" in up
    # the sidecar target is NULL-able: supersedes needs a target, withdraws
    # must NOT have one
    assert "TO_MEMORY_ID        TEXT" in up
    assert "REFERENCES PUBLIC.EXPLICIT_MEMORIES (MEMORY_ID)" in up
    assert "MEMORY_RELATIONS_TARGET_MATCHES_TYPE" in up
    # unique outgoing AND incoming edge + non-self CHECK
    assert "CHECK (FROM_MEMORY_ID <> TO_MEMORY_ID)" in up
    assert up.count("CREATE UNIQUE INDEX IF NOT EXISTS") >= 2
    assert "ON PUBLIC.MEMORY_RELATIONS (FROM_MEMORY_ID);" in up
    assert "ON PUBLIC.MEMORY_RELATIONS (TO_MEMORY_ID);" in up
    # the edge points at the canonical memories table
    # additive / idempotent only — checked on real statements, not comments
    body = _strip_sql_comments(raw).upper()
    for forbidden in ("DROP ", "TRUNCATE", "DELETE FROM"):
        assert forbidden not in body, (
            f"memory_relations.sql must stay additive ({forbidden})"
        )
    assert body.count("CREATE TABLE IF NOT EXISTS") == 1


def test_M01_schema_declares_no_history_backfill_requirement():
    body = _strip_sql_comments(REPO_SQL.read_text(encoding="utf-8")).upper()
    for forbidden in ("UPDATE ", "ALTER TABLE", "INSERT INTO"):
        assert forbidden not in body, (
            f"memory_relations.sql must not backfill existing memories ({forbidden})"
        )


# ─────────────────────────── public API surface ───────────────────────────


def test_M01_correct_and_read_version_signatures_are_locked():
    mod = _mod()
    sig = inspect.signature(mod.ActiveMemoryWriter.correct)
    params = list(sig.parameters)
    assert params[:3] == ["self", "memory_id", "replacement_content"], params
    for name in (
        "replacement_title", "replacement_tags", "correction_reason",
        "correction_source_id", "effective_at", "authority",
    ):
        param = sig.parameters.get(name)
        assert param is not None, f"correct() missing locked parameter {name}"
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{name} must be keyword-only"
        )
        assert param.default is None, f"{name} must default to None"
    # mode is keyword-only and defaults to 'replace', so every existing
    # caller that omits it keeps the replace behaviour.
    mode = sig.parameters.get("mode")
    assert mode is not None, "correct() must accept the frozen `mode` keyword"
    assert mode.kind is inspect.Parameter.KEYWORD_ONLY
    assert mode.default == "replace", mode.default

    rsig = inspect.signature(mod.ActiveMemoryReader.read_version)
    rparams = list(rsig.parameters)
    assert rparams[:2] == ["self", "memory_id"], rparams
    assert rsig.parameters["mode"].kind is inspect.Parameter.KEYWORD_ONLY
    assert rsig.parameters["include_history"].kind is inspect.Parameter.KEYWORD_ONLY
    assert rsig.parameters["mode"].default == "current"
    assert rsig.parameters["include_history"].default is False


def test_M01_withdraw_is_part_of_the_same_canonical_signature():
    """Restored withdraw lives in `correct` — not a separate method."""
    mod = _mod()
    assert not hasattr(mod.ActiveMemoryWriter, "withdraw"), (
        "withdraw must be a mode of correct(), not a second entry point"
    )
    for name in ("withdraws", "supersedes"):
        assert hasattr(mod, f"RELATION_TYPE_{name.upper()}"), (
            f"canonical relation constant RELATION_TYPE_{name.upper()} missing"
        )
    assert mod.RELATION_TYPE_SUPERSEDES == "supersedes"
    assert mod.RELATION_TYPE_WITHDRAWS == "withdraws"
    assert mod.RELATION_TYPE_CORRECTS == "supersedes", (
        "RELATION_TYPE_CORRECTS survives only as a compatibility SYMBOL alias"
    )
    assert mod.CORRECTION_MODES == ("replace", "withdraw")


def test_M01_get_by_memory_id_keeps_exact_historical_semantics():
    db = _FakeDb()
    _Seed(db).add("mem_a", "old payload", status="archived")
    got = _reader(db).get_by_memory_id("mem_a")
    assert got is not None and got["status"] == "archived"
    assert got["content"] == "old payload"
    assert _reader(db).get_by_memory_id("mem_missing") is None


# ─────────────────────────── authority is explicit ─────────────────────────


@pytest.mark.parametrize("bad", [None, "", "model_inference", "inferred",
                                 "user", "USER_EXPLICIT", "system"])
def test_M01_correction_requires_explicit_user_authority(bad):
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _writer(db).correct("mem_a", "B content", authority=bad)
    assert receipt["success"] is False, receipt
    assert receipt["durable"] is False
    assert receipt["error_code"] == "UNAUTHORIZED_AUTHORITY"
    # No write may happen without explicit user authority.
    assert db.memories["mem_a"]["status"] == "active"
    assert db.memories["mem_a"]["content"] == "A content"
    assert set(db.memories) == {"mem_a"}
    assert db.edges == {}


def test_M01_authority_constant_is_exactly_user_explicit():
    mod = _mod()
    assert mod.REQUIRED_CORRECTION_AUTHORITY == AUTHORITY
    assert mod.RELATION_TYPE_CORRECTS == "supersedes"
    assert mod.EXPLICIT_TOOL_REQUEST_PROVENANCE == PROVENANCE


# ─────────────────────────── A → B is the core state change ───────────────


def test_M01_a_to_b_archives_old_inserts_new_and_records_one_edge():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content", category="ops", title="deploy",
                  tags=("proj",), provenance={"origin": "seed"})

    receipt = _correct_a_to_b(db)

    assert receipt["success"] is True, receipt
    assert receipt["status"] == "CORRECTION_COMMITTED", receipt
    assert receipt["durable"] is True
    assert receipt["deduplicated"] is False
    assert receipt["old_memory_id"] == "mem_a"
    assert receipt["new_memory_id"] != "mem_a"
    assert receipt["old_status"] == "archived"
    assert receipt["new_status"] == "active"

    old = db.memories["mem_a"]
    assert old["status"] == "archived"
    # original payload + provenance are untouched — correction is NOT an UPDATE
    assert old["content"] == "A content"
    assert old["category"] == "ops" and old["title"] == "deploy"
    assert old["tags"] == ["proj"]
    assert old["provenance"] == {"origin": "seed"}

    new = db.memories[receipt["new_memory_id"]]
    assert new["status"] == "active"
    assert new["content"] == "B content"
    # category is preserved; omitted title/tags inherit the old payload
    assert new["category"] == "ops"
    assert new["title"] == "deploy"
    assert new["tags"] == ["proj"]

    edge = db.edges[("from", "mem_a")]
    assert edge["relation_type"] == "supersedes"
    assert edge["to_memory_id"] == receipt["new_memory_id"]
    assert edge["authority"] == AUTHORITY
    assert edge["correction_source_id"] is None
    assert edge["provenance"] == PROVENANCE
    # exactly one outgoing and one incoming index entry for this pair
    assert db.edges[("to", receipt["new_memory_id"])] is edge


def test_M01_correction_receipt_exposes_the_mandated_fields():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db, correction_reason="user said A is cancelled")

    for key in (
        "success", "old_memory_id", "new_memory_id", "relation",
        "old_status", "new_status", "correction_source", "deduplicated",
    ):
        assert key in receipt, f"M01 receipt must expose {key!r} (got {sorted(receipt)})"
    rel = receipt["relation"]
    assert rel["from_memory_id"] == "mem_a"
    assert rel["to_memory_id"] == receipt["new_memory_id"]
    assert rel["relation_type"] == "supersedes"
    assert rel["authority"] == AUTHORITY
    assert rel["correction_reason"] == "user said A is cancelled"
    assert rel["correction_id"] and rel["relation_id"]
    assert receipt["correction_source"] == {
        "provenance": PROVENANCE,
        "correction_source_id": None,
        "host": None,
        "session_id": None,
        "event_id": None,
    }


def test_M01_correction_uses_explicit_title_and_tags_when_supplied():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content", title="old title", tags=("old",))
    receipt = _correct_a_to_b(
        db, replacement_title="new title", replacement_tags=["new", "keep"],
    )
    new = db.memories[receipt["new_memory_id"]]
    assert new["title"] == "new title"
    assert new["tags"] == ["new", "keep"]


def test_M01_new_row_provenance_records_the_correction_not_the_old_origin():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content", provenance={"origin": "seed"})
    receipt = _correct_a_to_b(db)
    new = db.memories[receipt["new_memory_id"]]
    prov = json.loads(new["provenance"]) if isinstance(new["provenance"], str) else new["provenance"]
    assert prov["corrected_from"] == "mem_a"
    assert prov["authority"] == AUTHORITY
    assert prov["correction_id"] == receipt["relation"]["correction_id"]
    # the old row keeps its own origin untouched
    assert db.memories["mem_a"]["provenance"] == {"origin": "seed"}


# ─────────────────────────── input validation ─────────────────────────────


@pytest.mark.parametrize("kw,needle", [
    ({"replacement_content": ""}, "replacement_content"),
    ({"replacement_content": "   "}, "replacement_content"),
    ({"replacement_content": None}, "replacement_content"),
    ({"replacement_content": 5}, "replacement_content"),
    ({"replacement_title": ""}, "replacement_title"),
    ({"replacement_title": "  "}, "replacement_title"),
    ({"replacement_tags": []}, "replacement_tags"),
    ({"replacement_tags": ""}, "replacement_tags"),
    ({"replacement_tags": ["ok", ""]}, "replacement_tags"),
    ({"replacement_tags": ["ok", 3]}, "replacement_tags"),
    ({"replacement_tags": "notalist"}, "replacement_tags"),
    ({"correction_reason": ""}, "correction_reason"),
    ({"correction_source_id": ""}, "correction_source_id"),
    ({"correction_source_id": "   "}, "correction_source_id"),
])
def test_M01_explicit_empty_or_invalid_inputs_are_rejected_not_defaulted(kw, needle):
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    payload = {"replacement_content": "B content"}
    mid = "mem_a"
    for k, v in kw.items():
        if k == "memory_id":
            mid = v
        else:
            payload[k] = v
    receipt = _writer(db).correct(mid, **payload, authority=AUTHORITY)
    assert receipt["success"] is False, receipt
    assert receipt["error_code"] == "INVALID_REQUEST"
    assert needle in receipt["error"], receipt["error"]
    assert db.memories["mem_a"]["status"] == "active"
    assert set(db.memories) == {"mem_a"}


@pytest.mark.parametrize("bad_time", [
    "2026-10-01T08:00:00",          # naive ISO — no timezone
    "2026-10-01 08:00:00",          # naive ISO
    "not-a-time",
    "",
    "   ",
    12345,
    datetime(2026, 10, 1, 8, 0, 0),  # naive datetime
])
def test_M01_effective_at_requires_a_timezone(bad_time):
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db, effective_at=bad_time)
    assert receipt["success"] is False, receipt
    assert receipt["error_code"] == "INVALID_REQUEST"
    assert "effective_at" in receipt["error"]
    assert set(db.memories) == {"mem_a"}


def test_M01_effective_at_with_timezone_is_recorded():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db, effective_at="2026-10-01T08:00:00+08:00")
    assert receipt["success"] is True, receipt
    edge = db.edges[("from", "mem_a")]
    assert edge["effective_at"] == "2026-10-01T00:00:00+00:00"
    assert receipt["relation"]["effective_at"] == "2026-10-01T00:00:00+00:00"
    # absence stays absence — and stays absent in the canonical request
    db2 = _FakeDb()
    _Seed(db2).add("mem_a", "A content")
    _correct_a_to_b(db2)
    assert db2.edges[("from", "mem_a")]["effective_at"] is None
    assert json.loads(db2.edges[("from", "mem_a")]["request"])["effective_at"] is None


def test_M01_timezone_equivalent_effective_at_retries_deduplicate():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    first = _correct_a_to_b(db, effective_at="2026-10-01T08:00:00+08:00")
    second = _correct_a_to_b(db, effective_at="2026-10-01T00:00:00+00:00")
    assert first["new_memory_id"] == second["new_memory_id"]
    assert second["deduplicated"] is True
    assert len(db.memories) == 2


# ─────────────────────────── source provenance ────────────────────────────


def test_M01_supplied_correction_source_id_must_be_a_real_user_message():
    db = _FakeDb(messages={
        11: {"id": 11, "role": "user"},
        12: {"id": 12, "role": "assistant"},
    })
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db, correction_source_id="11")
    assert receipt["success"] is True, receipt
    edge = db.edges[("from", "mem_a")]
    assert edge["correction_source_id"] == "11", "must be stored literally"
    assert receipt["correction_source"]["correction_source_id"] == "11"
    assert receipt["correction_source"]["provenance"] == PROVENANCE
    # never fabricate a host/event/QA reference from the caller
    assert receipt["correction_source"]["host"] is None
    assert receipt["correction_source"]["session_id"] is None
    assert receipt["correction_source"]["event_id"] is None


@pytest.mark.parametrize("bad", ["99", "12", "not-an-int", "-1", "11.5", ""])
def test_M01_invalid_or_non_user_correction_source_is_rejected(bad):
    db = _FakeDb(messages={
        11: {"id": 11, "role": "user"},
        12: {"id": 12, "role": "assistant"},
    })
    _Seed(db).add("mem_a", "A content")
    receipt = _writer(db).correct("mem_a", "B content", authority=AUTHORITY,
                                 correction_source_id=bad)
    assert receipt["success"] is False, receipt
    # an empty string is an explicitly invalid value, not "absent"
    assert receipt["error_code"] in {"INVALID_REQUEST", "INVALID_CORRECTION_SOURCE"}
    assert set(db.memories) == {"mem_a"}


def test_M01_no_source_id_means_explicit_tool_request_and_null_source():
    db = _FakeDb(messages={11: {"id": 11, "role": "user"}})
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db)
    edge = db.edges[("from", "mem_a")]
    assert receipt["correction_source"]["provenance"] == PROVENANCE
    assert receipt["correction_source"]["correction_source_id"] is None
    assert edge["correction_source_id"] is None
    # no source was invented even though a real user message was available
    assert "11" not in json.dumps(receipt["correction_source"])
    # trace is limited to the durable explicit-memory chain
    assert json.loads(edge["request"])["correction_source_id"] is None


# ─────────────────────────── idempotency / conflict ───────────────────────


def test_M01_exact_retry_is_deduplicated_with_no_new_rows():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    first = _correct_a_to_b(db, correction_reason="user asked")
    writes_before = db.ops().count("insert_memory") + db.ops().count("insert_edge")

    second = _correct_a_to_b(db, correction_reason="user asked")

    assert second["success"] is True
    assert second["status"] == "CORRECTION_DEDUPLICATED"
    assert second["deduplicated"] is True
    assert second["durable"] is True
    assert second["new_memory_id"] == first["new_memory_id"]
    assert second["relation"]["relation_id"] == first["relation"]["relation_id"]
    assert second["old_status"] == "archived"
    # B is still the terminal active version, so the actual status is active
    assert second["new_status"] == "active"
    assert second["current_memory_id"] == first["new_memory_id"]
    assert len(db.memories) == 2
    assert db.ops().count("insert_memory") + db.ops().count("insert_edge") == writes_before
    assert db.ops().count("insert_edge") == 1


def test_M01_retry_is_stable_even_after_the_replacement_was_corrected():
    """A→B, then B→C; retrying the original A→B request must still report A→B."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    ab = _correct_a_to_b(db, correction_reason="first")
    b_id = ab["new_memory_id"]
    bc = _writer(db).correct(b_id, "C content", authority=AUTHORITY,
                            correction_reason="second")
    c_id = bc["new_memory_id"]

    retry = _correct_a_to_b(db, correction_reason="first")

    assert retry["success"] is True
    assert retry["deduplicated"] is True
    assert retry["new_memory_id"] == b_id
    assert len(db.memories) == 3
    # exactly two edges, indexed once per endpoint
    assert len(db.edges) == 4
    assert set(db.edges) == {("from", "mem_a"), ("to", b_id),
                             ("from", b_id), ("to", c_id)}
    assert db.edges[("from", "mem_a")]["to_memory_id"] == b_id
    assert db.edges[("from", b_id)]["to_memory_id"] == c_id


def test_M01_conflicting_second_successor_is_refused():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    ab = _correct_a_to_b(db, correction_reason="first")
    # the target gains a second successor via a follow-up on B, so the chain
    # has moved on and B is no longer the terminal
    _writer(db).correct(ab["new_memory_id"], "C content", authority=AUTHORITY,
                        correction_reason="second")

    conflict = _correct_a_to_b(db, replacement_content="D content",
                              correction_reason="different")

    assert conflict["success"] is False
    assert conflict["status"] == "CORRECTION_CONFLICT"
    assert conflict["error_code"] == "CORRECTION_CONFLICT"
    assert conflict["durable"] is False
    # the reported current is the chain's REAL terminal successor, which
    # here is C (B was itself corrected), not the stale first successor B
    c_id = [m["memory_id"] for m in db.memories.values()
            if m["content"] == "C content"][0]
    assert conflict["current_memory_id"] == c_id
    assert len(db.memories) == 3, "no second successor may be created"
    assert db.memories["mem_a"]["status"] == "archived"


def test_M01_superseded_target_surfaces_the_successor_and_refuses_to_rechain():
    """Pointing at the deprecated A must surface B and never re-chain A."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    ab = _correct_a_to_b(db)
    b_id = ab["new_memory_id"]

    stale = _writer(db).correct("mem_a", "Z content", authority=AUTHORITY)

    assert stale["success"] is False
    assert stale["error_code"] == "CORRECTION_CONFLICT"
    assert stale["current_memory_id"] == b_id, (
        "the caller must be told which memory is current so it can correct that"
    )
    assert db.edges[("from", "mem_a")]["to_memory_id"] == b_id
    assert len(db.memories) == 2

    forward = _writer(db).correct(b_id, "Z content", authority=AUTHORITY)
    assert forward["success"] is True
    assert db.edges[("from", "mem_a")]["to_memory_id"] == b_id
    assert db.edges[("from", b_id)]["to_memory_id"] == forward["new_memory_id"]


def test_M01_unrelated_archived_target_is_rejected():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content", status="archived")
    receipt = _correct_a_to_b(db)
    assert receipt["success"] is False
    assert receipt["error_code"] == "TARGET_NOT_CURRENT"
    assert receipt["current_memory_id"] is None
    assert set(db.memories) == {"mem_a"}


def test_M01_unknown_target_is_a_truthful_not_found():
    db = _FakeDb()
    receipt = _writer(db).correct("mem_missing", "B", authority=AUTHORITY)
    assert receipt["success"] is False
    assert receipt["error_code"] == "NOT_FOUND"
    assert receipt["old_memory_id"] == "mem_missing"
    assert receipt["new_memory_id"] is None
    assert db.memories == {}
    assert db.edges == {}


# ─────────────────────────── A → B → C chain ──────────────────────────────


def test_M01_chain_a_b_c_keeps_every_archived_payload_and_one_current():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    ab = _correct_a_to_b(db)
    b_id = ab["new_memory_id"]
    bc = _writer(db).correct(b_id, "C content", authority=AUTHORITY)

    assert bc["success"] is True
    c_id = bc["new_memory_id"]
    active = [m for m in db.memories.values() if m["status"] == "active"]
    assert [m["memory_id"] for m in active] == [c_id], "exactly one current"
    assert db.memories["mem_a"]["content"] == "A content"
    assert db.memories[b_id]["content"] == "B content"
    assert db.edges[("from", "mem_a")]["to_memory_id"] == b_id
    assert db.edges[("from", b_id)]["to_memory_id"] == c_id
    assert len(db.edges) == 4  # 2 edges × (from, to)


def test_M01_reusing_the_old_content_is_a_new_version_not_a_cycle():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    ab = _correct_a_to_b(db, replacement_content="B content")
    b_id = ab["new_memory_id"]
    back = _writer(db).correct(b_id, "A content", authority=AUTHORITY)

    assert back["success"] is True
    assert back["new_memory_id"] not in {"mem_a", b_id}
    # the ORIGINAL row is untouched — A→B→A-content is a new version
    assert db.memories["mem_a"]["status"] == "archived"
    assert db.memories["mem_a"]["content"] == "A content"
    assert db.memories[back["new_memory_id"]]["status"] == "active"
    assert db.edges[("from", "mem_a")]["to_memory_id"] == b_id
    assert db.edges[("from", b_id)]["to_memory_id"] == back["new_memory_id"]


def test_M01_ids_are_deterministic_and_target_bound():
    mod = _mod()
    db1, db2 = _FakeDb(), _FakeDb()
    for db in (db1, db2):
        _Seed(db).add("mem_a", "A content")
    r1 = _correct_a_to_b(db1)
    r2 = _correct_a_to_b(db2)
    assert r1["new_memory_id"] == r2["new_memory_id"]
    assert r1["relation"]["relation_id"] == r2["relation"]["relation_id"]
    assert r1["relation"]["correction_id"] == r2["relation"]["correction_id"]

    # target-bound: the same payload corrected from a different target differs
    db3 = _FakeDb()
    _Seed(db3).add("mem_z", "A content")
    r3 = _writer(db3).correct("mem_z", "B content", authority=AUTHORITY)
    assert r3["new_memory_id"] != r1["new_memory_id"]
    assert r3["relation"]["relation_id"] != r1["relation"]["relation_id"]


def test_M01_correction_id_is_a_pure_function_of_the_canonical_request():
    mod = _mod()
    derive = getattr(mod, "derive_correction_id", None)
    assert callable(derive), "derive_correction_id must be exposed for replay identity"
    request = mod.normalize_correction_request(
        "mem_a", "B content", replacement_title="t", replacement_tags=["x"],
        correction_reason=None, correction_source_id=None, effective_at=None,
    )
    expected = "cor_" + hashlib.sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert derive("mem_a", request) == expected
    # the target is bound in even if the request omitted it
    bound = dict(request)
    bound.pop("memory_id", None)
    assert derive("mem_z", bound) != expected


def test_M01_newer_unrelated_memory_does_not_supersede_anything():
    """A plain create() is not a correction — it must not touch any edge."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    ab = _correct_a_to_b(db)
    b_id = ab["new_memory_id"]
    edges_before = dict(db.edges)

    later = _writer(db).create("ops", "t", "newer unrelated fact", tags=["x"])

    assert later.durable is True
    assert db.edges == edges_before, "an ordinary create must not write an edge"
    assert db.memories["mem_a"]["status"] == "archived"
    assert db.memories[b_id]["status"] == "active"
    assert db.memories[later.memory_id]["status"] == "active"


# ─────────────────────────── transaction / rollback ───────────────────────


def test_M01_every_precommit_failure_rolls_the_whole_correction_back():
    """A failure after the new row is written must leave NOTHING behind."""
    for fail_op in ("insert_memory", "insert_edge", "archive"):
        db = _FakeDb()
        _Seed(db).add("mem_a", "A content")
        db.fail_on = {fail_op}

        receipt = _correct_a_to_b(db)

        assert receipt["success"] is False, f"{fail_op} must fail the correction"
        assert receipt["durable"] is False
        assert receipt["error_code"] == "DURABLE_FAILED", receipt
        assert set(db.memories) == {"mem_a"}, f"{fail_op}: partial write leaked"
        assert db.memories["mem_a"]["status"] == "active"
        assert db.memories["mem_a"]["content"] == "A content"
        assert db.edges == {}, f"{fail_op}: partial edge leaked"
        assert db.rollbacks >= 1, f"{fail_op}: no rollback issued"
        assert "COMMIT" not in db.ops(), f"{fail_op}: committed despite failure"


def test_M01_commits_once_and_only_after_a_full_verified_readback():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db)
    assert receipt["success"] is True
    ops = db.ops()
    assert ops.count("COMMIT") == 1
    commit_at = ops.index("COMMIT")
    # The WRITE transaction commits without a rollback. The required fresh,
    # read-only verification snapshot DOES roll back (cleanup) before its lease
    # is released — that is not a write rollback.
    assert "ROLLBACK" not in ops[:commit_at], (
        "the write transaction must not roll back before its COMMIT"
    )
    assert ops[commit_at + 1:].count("ROLLBACK") == 1, (
        "the read-only verification snapshot must be cleaned up exactly once"
    )
    core_ops = {"read_memory", "read_edge", "read_source", "insert_memory",
                "insert_edge", "archive"}
    # inside the transaction: lock → existing-edge check → new → edge → archive
    core = [op for op in ops[:commit_at] if op in core_ops]
    assert core == ["read_memory", "read_edge", "insert_memory",
                    "insert_edge", "archive"], core
    # verified readback of target + replacement + edge on a FRESH lease
    after = ops[commit_at + 1:]
    assert after.count("read_memory") == 2, after
    assert after.count("read_edge") == 1, after
    assert db.leases >= 2, "readback must use a fresh lease"


def test_M01_lock_for_update_is_used_on_the_target():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    _correct_a_to_b(db)
    lock_sql = [sql for op, sql in db.statements
                if op == "read_memory" and "FOR UPDATE" in sql]
    assert lock_sql, "target row must be locked FOR UPDATE before mutating"
    assert lock_sql[0].index("FOR UPDATE") > lock_sql[0].index("SELECT")


def test_M01_insert_uses_returning_row_count_checks():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    _correct_a_to_b(db)
    returning = [sql for op, sql in db.statements
                 if op in {"insert_memory", "insert_edge", "archive"}
                 and "RETURNING" in sql]
    assert len(returning) == 3, returning


def test_M01_readback_mismatch_is_a_truthful_durable_failure():
    """If the committed chain is not exactly what we wrote, do not claim success."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    original = _FakeCursor._dispatch

    def sabotage(self, op: str) -> None:
        if op == "read_memory" and "FOR UPDATE" not in self.sql.upper():
            self._conn.memories.pop(self.params[0], None)  # row vanished
            return
        original(self, op)

    _FakeCursor._dispatch = sabotage
    try:
        receipt = _correct_a_to_b(db)
    finally:
        _FakeCursor._dispatch = original
    assert receipt["success"] is False
    assert receipt["error_code"] == "DURABLE_FAILED"
    assert "readback" in receipt["error"]


def test_M01_readback_rolls_back_when_it_runs_inside_the_transaction():
    """The verified readback must not be the same snapshot we just wrote."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db)
    ops = db.ops()
    commit_at = ops.index("COMMIT")
    # exactly one readback lease, opening three reads, all AFTER the commit
    reads_after_commit = [op for op in ops[commit_at + 1:]
                          if op in {"read_memory", "read_edge"}]
    assert reads_after_commit == ["read_memory", "read_memory", "read_edge"]
    # and it ran on a different lease than the write
    assert db.leases == 2
    assert receipt["success"] is True


# ─────────────────────────── migration / schema failure ───────────────────


def test_M01_missing_relation_table_is_a_truthful_migration_failure():
    db = _FakeDb(missing_relations=True)
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db)
    assert receipt["success"] is False
    assert receipt["error_code"] == "MIGRATION_REQUIRED"
    assert "memory_relations" in receipt["error"]
    # never a hidden fallback: no orphan replacement, no partial write
    assert set(db.memories) == {"mem_a"}


def test_M01_missing_relation_table_fails_the_reader_truthfully():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    db.missing_relations = True
    out = _reader(db).read_version("mem_a")
    assert out["success"] is False
    assert out["error_code"] == "MIGRATION_REQUIRED"
    assert "memory_relations" in out["error"]
    assert out["memory"] is None


# ─────────────────────────── post-commit embedding ────────────────────────


def _boom_embedder(*_a, **_kw):
    raise RuntimeError("synthetic embedder outage")


def test_M01_embedding_failure_keeps_the_correction_durable():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    writer = _writer(db, embed_cfg={"model": "m", "_fingerprint": "fp"},
                     embedder=_boom_embedder)
    receipt = writer.correct("mem_a", "B content", authority=AUTHORITY)
    assert receipt["success"] is True
    assert receipt["durable"] is True
    assert receipt["status"] == "CORRECTION_DERIVED_WARNING"
    assert receipt["warnings"], "an embed failure must surface as a warning"
    assert db.memories["mem_a"]["status"] == "archived"
    assert db.memories[receipt["new_memory_id"]]["status"] == "active"
    assert db.edges[("from", "mem_a")]["to_memory_id"] == receipt["new_memory_id"]


def test_M01_correction_needs_no_embedding_at_all():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db)
    assert receipt["success"] is True
    assert receipt["warnings"] == []
    assert db.memories[receipt["new_memory_id"]]["embedding"] is None


def test_M01_embedding_only_runs_after_the_commit():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    writer = _writer(db, embed_cfg={"model": "m", "_fingerprint": "fp"},
                     embedder=lambda *_: [0.0] * 1024)
    writer.correct("mem_a", "B content", authority=AUTHORITY)
    ops = db.ops()
    assert "embed" in ops
    assert ops.index("COMMIT") < ops.index("embed")


# ─────────────────────────── canonical reader ────────────────────────────


def _chain_db() -> tuple:
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    ab = _correct_a_to_b(db)
    bc = _writer(db).correct(ab["new_memory_id"], "C content", authority=AUTHORITY)
    return db, ab["new_memory_id"], bc["new_memory_id"]


def test_M01_read_version_current_resolves_to_the_terminal_active_version():
    db, b_id, c_id = _chain_db()
    out = _reader(db).read_version("mem_a", mode="current")
    assert out["success"] is True, out
    assert out["memory_id"] == "mem_a"
    assert out["resolved_memory_id"] == c_id
    assert out["is_current"] is False
    assert out["status"] == "OK"
    assert out["memory"]["content"] == "C content"
    assert out["memory"]["status"] == "active"
    assert out["memory"]["memory_id"] == c_id
    # the requested archived payload is still reachable, untruncated
    assert out["requested"]["content"] == "A content"
    assert out["requested"]["status"] == "archived"
    assert "history" not in out, "history is not returned unless asked for"


def test_M01_read_version_current_on_the_terminal_is_current():
    db, b_id, c_id = _chain_db()
    out = _reader(db).read_version(c_id, mode="current")
    assert out["success"] is True
    assert out["resolved_memory_id"] == c_id
    assert out["is_current"] is True
    assert out["memory"]["content"] == "C content"


def test_M01_read_version_history_returns_the_full_ordered_chain():
    db, b_id, c_id = _chain_db()
    out = _reader(db).read_version("mem_a", mode="history")
    assert out["success"] is True, out
    assert out["memory_id"] == "mem_a"
    assert out["mode"] == "history"
    assert out["requested"]["content"] == "A content"
    assert out["requested"]["status"] == "archived"
    hist = out["history"]
    assert [v["memory_id"] for v in hist["versions"]] == ["mem_a", b_id, c_id]
    assert [e["from_memory_id"] for e in hist["edges"]] == ["mem_a", b_id]
    assert [e["to_memory_id"] for e in hist["edges"]] == [b_id, c_id]
    labels = {v["memory_id"]: v["label"] for v in hist["versions"]}
    assert labels == {"mem_a": "superseded", b_id: "superseded", c_id: "current"}
    assert [v["status"] for v in hist["versions"]] == ["archived", "archived", "active"]
    assert all(v["content"] for v in hist["versions"]), "history must not truncate"
    assert hist["versions"][0]["content"] == "A content"
    assert out["resolved_memory_id"] == c_id
    # the edge chain carries authority / reason / provenance / timestamps
    edge = hist["edges"][0]
    for key in ("relation_id", "relation_type", "authority", "provenance",
                "correction_reason", "correction_source_id", "effective_at",
                "recorded_at", "correction_id"):
        assert key in edge, f"history edge must expose {key!r}"


def test_M01_read_version_include_history_attaches_the_chain_to_current():
    db, b_id, c_id = _chain_db()
    out = _reader(db).read_version("mem_a", include_history=True)
    assert out["success"] is True
    assert out["memory"]["content"] == "C content"
    assert out["mode"] == "current"
    assert [v["memory_id"] for v in out["history"]["versions"]] == ["mem_a", b_id, c_id]


def test_M01_read_version_of_a_never_corrected_memory_is_itself():
    db = _FakeDb()
    _Seed(db).add("mem_plain", "plain content")
    out = _reader(db).read_version("mem_plain")
    assert out["success"] is True
    assert out["resolved_memory_id"] == "mem_plain"
    assert out["is_current"] is True
    assert out["memory"]["content"] == "plain content"
    hist = _reader(db).read_version("mem_plain", mode="history")
    assert [v["memory_id"] for v in hist["history"]["versions"]] == ["mem_plain"]
    assert hist["history"]["edges"] == []


def test_M01_read_version_unknown_id_fails_truthfully():
    db = _FakeDb()
    out = _reader(db).read_version("mem_nope")
    assert out["success"] is False
    assert out["error_code"] == "NOT_FOUND"
    assert out["memory"] is None
    assert out["memory_id"] == "mem_nope"


@pytest.mark.parametrize("kw", [
    {"memory_id": ""},
    {"memory_id": "   "},
    {"memory_id": 42},
    {"mode": "weird"},
    {"mode": None},
])
def test_M01_read_version_rejects_bad_arguments(kw):
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    out = _reader(db).read_version(kw.get("memory_id", "mem_a"),
                                   mode=kw.get("mode", "current"))
    assert out["success"] is False
    assert out["error_code"] == "INVALID_REQUEST"


def test_M01_ordinary_keyword_recall_only_returns_the_current_version():
    db, b_id, c_id = _chain_db()
    hits = _reader(db).search_keyword("content")
    got = {h["memory_id"] for h in hits}
    assert "mem_a" not in got and b_id not in got
    assert c_id in got


# ─────────────────────────── boundary hygiene ─────────────────────────────


def test_M01_correction_dml_lives_only_in_the_canonical_writer_module():
    text = (V3CORE_SRC / "active_memory_store.py").read_text(encoding="utf-8")
    assert '"public.memory_relations"' in text or "'public.memory_relations'" in text, (
        "the correction DML must name the canonical relation table"
    )
    assert "INSERT INTO {_RELATION_TABLE}" in text
    classes = {n.name for n in ast.walk(ast.parse(text))
               if isinstance(n, ast.ClassDef)}
    assert {"ActiveMemoryWriter", "ActiveMemoryReader"} <= classes


@pytest.mark.parametrize("name", ["observer.py", "e1.py", "topic_store.py",
                                  "dedup.py", "recall_pool.py"])
def test_M01_no_passive_path_writes_the_relation_table(name):
    peer = V3CORE_SRC / name
    if not peer.exists():
        pytest.skip(f"{name} not in this tree")
    low = peer.read_text(encoding="utf-8").lower()
    assert "memory_relations" not in low, (
        f"{name} must not reach around the canonical writer boundary"
    )


def test_M01_schema_artifact_is_never_auto_applied_by_the_store():
    text = (V3CORE_SRC / "active_memory_store.py").read_text(encoding="utf-8")
    for forbidden in ("CREATE TABLE", "CREATE INDEX", "open(", "read_text(",
                      "subprocess", "psycopg2.connect"):
        assert forbidden not in text, (
            f"active_memory_store must never create/apply schema ({forbidden!r})"
        )


# ═══════════════════ restored M01 regression surface ═══════════════════
#
# Everything below pins behaviour the previous round got wrong or omitted.
# RED was observed on HEAD 89e50a5 before the writer/schema/reader changes.


def _withdraw(db: _FakeDb, mid: str, **kw: Any) -> dict:
    kw.pop("memory_id", None)
    kw.pop("replacement_content", None)
    return _writer(db).correct(mid, None, mode="withdraw", authority=AUTHORITY, **kw)


# ── withdraw: no replacement is ever created ──────────────────────────


def test_M01_withdraw_archives_the_target_and_creates_no_replacement():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content", category="ops", title="deploy",
                  tags=("proj",), provenance={"origin": "seed"})

    receipt = _withdraw(db, "mem_a", correction_reason="user retracted it")

    assert receipt["success"] is True, receipt
    assert receipt["status"] == "CORRECTION_COMMITTED", receipt
    assert receipt["durable"] is True
    assert receipt["mode"] == "withdraw"
    assert receipt["old_memory_id"] == "mem_a"
    assert receipt["new_memory_id"] is None, (
        "a withdrawal MUST NOT create a replacement row"
    )
    assert receipt["old_status"] == "archived"
    assert receipt["new_status"] is None
    # exactly one memory row exists afterwards — no B was invented
    assert set(db.memories) == {"mem_a"}
    assert db.memories["mem_a"]["status"] == "archived"
    # the payload of the withdrawn memory is preserved verbatim
    assert db.memories["mem_a"]["content"] == "A content"
    assert db.memories["mem_a"]["provenance"] == {"origin": "seed"}


def test_M01_withdraw_writes_one_null_target_edge_and_archives_atomically():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")

    receipt = _withdraw(db, "mem_a", correction_reason="retracted")

    edge = db.edges[("from", "mem_a")]
    assert edge["relation_type"] == "withdraws"
    assert edge["to_memory_id"] is None, "a withdrawal has no replacement target"
    assert edge["from_memory_id"] == "mem_a"
    assert edge["authority"] == AUTHORITY
    assert edge["correction_reason"] == "retracted"
    assert edge["provenance"] == PROVENANCE
    # no incoming index entry is fabricated for a NULL target
    assert [k for k in db.edges if k[0] == "to"] == []
    # edge + archive land in the same transaction, exactly one commit
    assert db.ops().count("COMMIT") == 1
    commit_at = db.ops().index("COMMIT")
    # the WRITE transaction never rolls back; only the fresh read-only
    # verification snapshot is cleaned up (rollback) before its lease release
    assert "ROLLBACK" not in db.ops()[:commit_at], (
        "the write transaction must not roll back before its COMMIT"
    )
    assert db.ops()[commit_at + 1:].count("ROLLBACK") == 1, (
        "the read-only verification snapshot must be cleaned up exactly once"
    )
    core = [op for op in db.ops()[:commit_at] if op in
            {"read_memory", "read_edge", "insert_memory", "insert_edge", "archive"}]
    assert core == ["read_memory", "read_edge", "insert_edge", "archive"], core
    assert "insert_memory" not in db.ops(), "withdraw must insert no new memory"
    assert receipt["relation"]["relation_type"] == "withdraws"
    assert receipt["relation"]["to_memory_id"] is None


def test_M01_withdraw_never_calls_the_embedder():
    calls: list = []

    def counting(text, cfg):
        calls.append(text)
        return [0.0] * 8

    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    writer = _writer(db, embed_cfg={"model": "m", "_fingerprint": "fp"},
                     embedder=counting)
    receipt = writer.correct("mem_a", None, mode="withdraw", authority=AUTHORITY)

    assert receipt["success"] is True, receipt
    assert calls == [], "no embedding call for a withdrawal"
    assert "embed" not in db.ops()


@pytest.mark.parametrize("field,value", [
    ("replacement_content", ""),
    ("replacement_content", "   "),
    ("replacement_content", "some content"),
    ("replacement_title", ""),
    ("replacement_title", "a title"),
    ("replacement_tags", []),
    ("replacement_tags", ["x"]),
    ("replacement_tags", ""),
])
def test_M01_withdraw_rejects_any_replacement_payload_even_when_empty(field, value):
    """An empty value is an explicit payload, not "absent" — both are refused."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    if field == "replacement_content":
        # a caller that supplies a content payload passes it as the real
        # positional/keyword argument it is — either way the writer must
        # refuse it rather than treat it as "absent"
        receipt = _writer(db).correct("mem_a", value, mode="withdraw",
                                      authority=AUTHORITY)
    else:
        receipt = _writer(db).correct("mem_a", None, mode="withdraw",
                                      authority=AUTHORITY, **{field: value})
    assert receipt["success"] is False, receipt
    assert receipt["error_code"] == "INVALID_REQUEST", receipt
    assert field in receipt["error"], receipt["error"]
    # nothing was written: the target is untouched, no edge, no new row
    assert db.memories["mem_a"]["status"] == "active"
    assert db.memories["mem_a"]["content"] == "A content"
    assert set(db.memories) == {"mem_a"}
    assert db.edges == {}


def test_M01_withdraw_still_requires_explicit_user_authority():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _writer(db).correct("mem_a", None, mode="withdraw",
                                  authority="model_inference")
    assert receipt["success"] is False, receipt
    assert receipt["error_code"] == "UNAUTHORIZED_AUTHORITY"
    assert db.memories["mem_a"]["status"] == "active"
    assert db.edges == {}


def test_M01_withdraw_still_validates_a_real_source_message():
    db = _FakeDb(messages={12: {"id": 12, "role": "assistant"}})
    _Seed(db).add("mem_a", "A content")
    bad = _withdraw(db, "mem_a", correction_source_id="12")
    assert bad["success"] is False, bad
    assert bad["error_code"] == "INVALID_CORRECTION_SOURCE"
    assert db.edges == {}
    good = _withdraw(db, "mem_a", correction_source_id="12")  # still not a user row
    assert good["success"] is False


@pytest.mark.parametrize("bad", ["+11", " 11", "11 ", "١١", "11\n", "0x1b",
                                 "1_1", "１１"])
def test_M01_malformed_source_literal_is_rejected_never_repaired(bad):
    """Only ASCII decimal digits — a malformed id is refused, not stripped."""
    db = _FakeDb(messages={11: {"id": 11, "role": "user"}})
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db, correction_source_id=bad)
    assert receipt["success"] is False, receipt
    assert receipt["error_code"] == "INVALID_REQUEST", receipt
    assert db.edges == {}
    assert set(db.memories) == {"mem_a"}


def test_M01_source_id_is_stored_exactly_as_supplied():
    """A real source must not be silently normalised/repaired in the edge."""
    db = _FakeDb(messages={11: {"id": 11, "role": "user"}})
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db, correction_source_id="11")
    assert receipt["success"] is True, receipt
    assert receipt["relation"]["correction_source_id"] == "11"
    assert db.edges[("from", "mem_a")]["correction_source_id"] == "11"


def test_M01_unknown_mode_is_rejected():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    for bad in ("delete", "replace ", "WITHDRAW", None, "", 3):
        receipt = _writer(db).correct("mem_a", "B", mode=bad, authority=AUTHORITY)
        assert receipt["success"] is False, (bad, receipt)
        assert receipt["error_code"] == "INVALID_REQUEST", (bad, receipt)
    assert set(db.memories) == {"mem_a"}
    assert db.edges == {}


def test_M01_omitted_mode_is_replace_for_existing_callers():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _writer(db).correct("mem_a", "B content", authority=AUTHORITY)
    assert receipt["success"] is True, receipt
    assert receipt["mode"] == "replace"
    assert db.edges[("from", "mem_a")]["relation_type"] == "supersedes"
    assert db.edges[("from", "mem_a")]["to_memory_id"] == receipt["new_memory_id"]


# ── withdraw idempotency / conflict / truthfulness ──────────────────────


def test_M01_withdraw_retry_is_deduplicated_with_zero_writes():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    first = _withdraw(db, "mem_a", correction_reason="retracted")
    writes_before = db.ops().count("insert_edge") + db.ops().count("archive")

    second = _withdraw(db, "mem_a", correction_reason="retracted")

    assert second["success"] is True, second
    assert second["deduplicated"] is True
    assert second["status"] == "CORRECTION_DEDUPLICATED"
    assert second["durable"] is True
    assert second["new_memory_id"] is None
    assert second["current_memory_id"] is None, "a withdrawal has no current"
    assert second["relation"]["relation_id"] == first["relation"]["relation_id"]
    assert db.ops().count("insert_edge") + db.ops().count("archive") == writes_before
    assert db.ops().count("insert_edge") == 1


def test_M01_conflicting_second_request_against_a_withdrawn_target():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    _withdraw(db, "mem_a", correction_reason="retracted")

    other = _correct_a_to_b(db, replacement_content="B content",
                            correction_reason="changed my mind")

    assert other["success"] is False, other
    assert other["error_code"] == "CORRECTION_CONFLICT"
    assert other["current_memory_id"] is None, (
        "nothing is current after a withdrawal — do not invent a successor"
    )
    assert len(db.memories) == 1, "no replacement may be created afterwards"
    assert db.memories["mem_a"]["status"] == "archived"


def test_M01_withdraw_of_an_unknown_or_archived_target_is_truthful():
    unknown = _writer(_FakeDb()).correct("mem_missing", None, mode="withdraw",
                                         authority=AUTHORITY)
    assert unknown["success"] is False
    assert unknown["error_code"] == "NOT_FOUND"
    assert unknown["new_memory_id"] is None

    db = _FakeDb()
    _Seed(db).add("mem_a", "A content", status="archived")
    archived = _withdraw(db, "mem_a")
    assert archived["success"] is False
    assert archived["error_code"] == "TARGET_NOT_CURRENT"
    assert db.edges == {}


# ── A → B → C → withdraw chain ────────────────────────────────────────


def _chain_with_withdraw() -> tuple:
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    ab = _correct_a_to_b(db)
    b_id = ab["new_memory_id"]
    bc = _writer(db).correct(b_id, "C content", authority=AUTHORITY)
    c_id = bc["new_memory_id"]
    wd = _withdraw(db, c_id, correction_reason="C retracted")
    return db, b_id, c_id, wd


def test_M01_a_b_c_then_withdraw_leaves_no_current_version():
    db, b_id, c_id, wd = _chain_with_withdraw()
    assert wd["success"] is True, wd
    assert db.memories[c_id]["status"] == "archived"
    active = [m for m in db.memories.values() if m["status"] == "active"]
    assert active == [], "a withdrawal leaves no current memory at all"
    assert set(db.edges) == {("from", "mem_a"), ("to", b_id),
                             ("from", b_id), ("to", c_id), ("from", c_id)}


def test_M01_history_of_a_terminal_withdrawn_row_keeps_the_exact_payload():
    db, b_id, c_id, _ = _chain_with_withdraw()
    out = _reader(db).read_version(c_id, mode="history")
    assert out["success"] is True, out
    # history mode answers with the EXACT requested payload
    assert out["memory"]["content"] == "C content"
    assert out["requested"]["content"] == "C content"
    assert out["requested"]["status"] == "archived"
    assert out["resolved_memory_id"] is None, (
        "nothing is current after the terminal version was withdrawn"
    )
    assert out["is_current"] is False
    hist = out["history"]
    assert [v["memory_id"] for v in hist["versions"]] == ["mem_a", b_id, c_id]
    labels = {v["memory_id"]: v["label"] for v in hist["versions"]}
    assert labels["mem_a"] == "superseded"
    assert labels[b_id] == "superseded"
    assert labels[c_id] == "withdrawn"
    assert hist["current_memory_id"] is None
    assert [e["relation_type"] for e in hist["edges"]] == [
        "supersedes", "supersedes", "withdraws",
    ]
    assert hist["edges"][-1]["to_memory_id"] is None


def test_M01_history_called_on_a_predecessor_includes_its_ancestors():
    db, b_id, c_id, _ = _chain_with_withdraw()
    out = _reader(db).read_version(b_id, mode="history")
    assert out["success"] is True, out
    assert [v["memory_id"] for v in out["history"]["versions"]] == [
        "mem_a", b_id, c_id,
    ], "ancestors must be included when reading a mid-chain version"
    assert [e["from_memory_id"] for e in out["history"]["edges"]] == [
        "mem_a", b_id, c_id,
    ]


def test_M01_current_read_of_a_withdrawn_row_or_its_predecessor_has_no_memory():
    db, b_id, c_id, _ = _chain_with_withdraw()
    for target in (c_id, b_id, "mem_a"):
        out = _reader(db).read_version(target, mode="current")
        assert out["success"] is True, (target, out)
        assert out["memory"] is None, (target, out)
        assert out["resolved_memory_id"] is None, (target, out)
        assert out["is_current"] is False, (target, out)
        assert out["current_status"] == "withdrawn", (target, out)
        # the historical payload is still available
        assert out["requested"]["content"], (target, out)
        assert out["requested"]["status"] == "archived", (target, out)


def test_M01_current_read_of_an_archived_row_without_an_edge_is_not_current():
    db = _FakeDb()
    _Seed(db).add("mem_z", "archived by hand", status="archived")
    out = _reader(db).read_version("mem_z", mode="current")
    assert out["success"] is True, out
    assert out["memory"] is None, (
        "an archived row with no edge is not the current version"
    )
    assert out["resolved_memory_id"] is None
    assert out["is_current"] is False
    assert out["current_status"] == "archived"
    assert out["requested"]["content"] == "archived by hand"
    assert out["relation"] is None


# ── retry / conflict against a chain that moved on ─────────────────────


def test_M01_retry_after_a_second_correction_reports_actual_status_and_current():
    """A→B, B→C; the original A→B retry is still B, but C is what is current."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    ab = _correct_a_to_b(db, correction_reason="first")
    b_id = ab["new_memory_id"]
    bc = _writer(db).correct(b_id, "C content", authority=AUTHORITY,
                            correction_reason="second")
    c_id = bc["new_memory_id"]

    retry = _correct_a_to_b(db, correction_reason="first")

    assert retry["success"] is True, retry
    assert retry["deduplicated"] is True
    # the retry still refers to ITS OWN new row, with its ACTUAL status
    assert retry["new_memory_id"] == b_id
    assert retry["new_status"] == "archived", "B was itself corrected"
    # but the chain's current version is C, not the stale B
    assert retry["current_memory_id"] == c_id
    assert len(db.memories) == 3


def test_M01_conflict_against_a_moved_chain_returns_the_real_current():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    ab = _correct_a_to_b(db, correction_reason="first")
    b_id = ab["new_memory_id"]
    c_id = _writer(db).correct(b_id, "C content", authority=AUTHORITY,
                               correction_reason="second")["new_memory_id"]

    conflict = _correct_a_to_b(db, replacement_content="Z content",
                               correction_reason="different")

    assert conflict["success"] is False
    assert conflict["error_code"] == "CORRECTION_CONFLICT"
    assert conflict["current_memory_id"] == c_id, (
        "the conflict must surface the CURRENT successor C, not the stale B"
    )
    assert len(db.memories) == 3


def test_M01_retry_of_a_withdraw_survives_a_later_conflict():
    """Withdrawal retry keeps referring to the same edge and no current."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    first = _withdraw(db, "mem_a", correction_reason="r")
    again = _withdraw(db, "mem_a", correction_reason="r")
    assert again["deduplicated"] is True
    assert again["relation"]["relation_id"] == first["relation"]["relation_id"]
    assert again["current_memory_id"] is None
    assert db.ops().count("insert_edge") == 1


def test_M01_existing_edge_comparison_uses_the_request_not_only_the_hash():
    """A different request with the same target is a conflict, never a dedupe."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    _correct_a_to_b(db, correction_reason="first")
    other = _correct_a_to_b(db, replacement_content="totally other",
                            correction_reason="first")
    assert other["success"] is False
    assert other["error_code"] == "CORRECTION_CONFLICT"
    assert len(db.memories) == 2


# ── _verify_correction: payload, actual recorded_at, durability ────────


def test_M01_recorded_at_is_the_actual_server_value_not_none():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db)
    recorded = receipt["relation"]["recorded_at"]
    assert recorded is not None, "recorded_at must be the real server value"
    assert recorded == _T1.isoformat()
    assert db.edges[("from", "mem_a")]["recorded_at"] == _T1


def test_M01_withdraw_recorded_at_is_the_actual_server_value():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _withdraw(db, "mem_a")
    assert receipt["relation"]["recorded_at"] == _T1.isoformat()


def test_M01_verification_checks_the_old_immutable_payload_and_the_new_row():
    """A payload that disagrees with what we intended is a truthful failure."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content", title="deploy", tags=("proj",))
    original = _FakeCursor._dispatch

    def tamper(self, op: str) -> None:
        if op == "read_memory" and "FOR UPDATE" not in self.sql.upper():
            row = self._conn.memories.get(self.params[0])
            if row is not None and row["status"] == "archived":
                row["content"] = "TAMPERED"  # payload no longer matches
                return
        original(self, op)

    _FakeCursor._dispatch = tamper
    try:
        receipt = _correct_a_to_b(db)
    finally:
        _FakeCursor._dispatch = original
    assert receipt["success"] is False
    assert receipt["error_code"] == "DURABLE_FAILED"
    assert "readback" in receipt["error"]


def test_M01_committed_but_unverifiable_write_is_durable_true():
    """Post-commit verification failure is NOT a rolled-back write."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    original = _FakeCursor._dispatch

    def sabotage(self, op: str) -> None:
        if op == "read_edge" and "RELATION_ID = %S" in _norm(self.sql):
            self._one = None  # edge "vanishes" from the readback
            return
        original(self, op)

    _FakeCursor._dispatch = sabotage
    try:
        receipt = _correct_a_to_b(db)
    finally:
        _FakeCursor._dispatch = original
    assert receipt["success"] is False
    assert receipt["durable"] is True, (
        "the transaction already committed; durability is not rolled back"
    )
    assert receipt["error_code"] == "DURABLE_FAILED"
    assert "readback" in receipt["error"]
    assert receipt["new_memory_id"], "the write did land"
    assert db.memories["mem_a"]["status"] == "archived"


def test_M01_no_embedding_runs_when_durable_verification_fails():
    calls: list = []

    def counting(text, cfg):
        calls.append(text)
        return [0.0] * 8

    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    original = _FakeCursor._dispatch

    def sabotage(self, op: str) -> None:
        if op == "read_edge" and "RELATION_ID = %S" in _norm(self.sql):
            self._one = None
            return
        original(self, op)

    writer = _writer(db, embed_cfg={"model": "m", "_fingerprint": "fp"},
                     embedder=counting)
    _FakeCursor._dispatch = sabotage
    try:
        receipt = writer.correct("mem_a", "B content", authority=AUTHORITY)
    finally:
        _FakeCursor._dispatch = original
    assert receipt["durable"] is True
    assert calls == [], "no embedding before a durable verification"


def test_M01_verification_permits_a_legitimate_concurrent_followup():
    """B superseded by C before the readback: report the real status."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db)
    b_id = receipt["new_memory_id"]

    # a concurrent writer corrects B -> C before the first receipt's readback
    follow = _writer(db).correct(b_id, "C content", authority=AUTHORITY)
    assert follow["success"] is True
    c_id = follow["new_memory_id"]

    # re-verifying the SAME request must not falsely claim B is still active
    again = _correct_a_to_b(db)
    assert again["success"] is True
    assert again["current_memory_id"] == c_id
    assert again["new_status"] == "archived"


# ── cycle / corrupt chain fails closed ─────────────────────────────────


def _make_cycle(db: _FakeDb, a: str, b: str) -> None:
    """Force a C -> A back edge: the unique indexes do NOT prevent a cycle."""
    db.edges[("from", a)] = {
        "relation_id": "rel_cycle_a", "relation_type": "supersedes",
        "from_memory_id": a, "to_memory_id": b, "correction_id": "cor_cycle_a",
        "authority": AUTHORITY, "correction_reason": None,
        "correction_source_id": None, "provenance": PROVENANCE,
        "effective_at": None, "recorded_at": _T1, "request": {},
    }
    db.edges[("to", b)] = db.edges[("from", a)]
    db.edges[("from", b)] = {
        "relation_id": "rel_cycle_b", "relation_type": "supersedes",
        "from_memory_id": b, "to_memory_id": a, "correction_id": "cor_cycle_b",
        "authority": AUTHORITY, "correction_reason": None,
        "correction_source_id": None, "provenance": PROVENANCE,
        "effective_at": None, "recorded_at": _T1, "request": {},
    }
    db.edges[("to", a)] = db.edges[("from", b)]


def test_M01_reader_refuses_a_cycle_in_current_mode():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    _Seed(db).add("mem_b", "B content")
    _make_cycle(db, "mem_a", "mem_b")
    out = _reader(db).read_version("mem_a", mode="current")
    assert out["success"] is False, (
        "a cycle must fail closed, never break-and-succeed"
    )
    assert out["error_code"] == "DURABLE_FAILED"
    assert "环" in out["error"] or "cycle" in out["error"].lower(), out["error"]
    assert out["memory"] is None


def test_M01_reader_refuses_a_cycle_in_history_mode():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    _Seed(db).add("mem_b", "B content")
    _make_cycle(db, "mem_a", "mem_b")
    out = _reader(db).read_version("mem_a", mode="history")
    assert out["success"] is False, out
    assert out["error_code"] == "DURABLE_FAILED"
    assert out.get("history") is None


def test_M01_reader_refuses_a_chain_with_a_missing_edge_endpoint():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    db.edges[("from", "mem_a")] = {
        "relation_id": "rel_x", "relation_type": "supersedes",
        "from_memory_id": "mem_a", "to_memory_id": "mem_missing",
        "correction_id": "cor_x", "authority": AUTHORITY,
        "correction_reason": None, "correction_source_id": None,
        "provenance": PROVENANCE, "effective_at": None, "recorded_at": _T1,
        "request": {},
    }
    out = _reader(db).read_version("mem_a", mode="current")
    assert out["success"] is False, out
    assert out["error_code"] == "DURABLE_FAILED"
    assert out["memory"] is None
    assert "mem_missing" in out["error"]


def test_M01_writer_refuses_to_build_on_a_cyclic_target():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    _Seed(db).add("mem_b", "B content")
    _make_cycle(db, "mem_a", "mem_b")
    receipt = _correct_a_to_b(db, replacement_content="Z")
    assert receipt["success"] is False, receipt
    assert receipt["error_code"] == "CORRECTION_CONFLICT"
    assert len(db.memories) == 2, "no new row may be written onto a cycle"


# ── coherent snapshot for multi-query chain reads ──────────────────────


def test_M01_reader_chain_reads_share_one_leased_repeatable_read_snapshot():
    db, b_id, c_id = _chain_db()
    out = _reader(db).read_version("mem_a", mode="history")
    assert out["success"] is True, out
    ops = db.ops()
    # a single leased, read-only repeatable-read transaction covers the walk
    assert ops.count("BEGIN") >= 1
    assert any(
        op == "BEGIN" and "REPEATABLE READ" in sql
        for op, sql in db.statements if op == "BEGIN"
    ), [s for op, s in db.statements if op == "BEGIN"]
    assert any("READ ONLY" in sql for op, sql in db.statements if op == "BEGIN")


def test_M01_current_read_uses_one_snapshot_too():
    db, b_id, c_id = _chain_db()
    out = _reader(db).read_version("mem_a", mode="current")
    assert out["success"] is True, out
    assert any(
        op == "BEGIN" and "REPEATABLE READ" in sql
        for op, sql in db.statements if op == "BEGIN"
    ), [s for op, s in db.statements if op == "BEGIN"]


# ── concurrency: no nested lease while one is held ─────────────────────


def test_M01_no_nested_fresh_lease_inside_a_held_lease():
    """A pool whose max size is 1 must not deadlock: each lease is closed
    before the next one is taken."""
    class _Max1Pool:
        def __init__(self, inner: _FakeDb) -> None:
            self._inner = inner
            self.outstanding = 0
            self.max_seen = 0
            self.leases = 0

        def lease(self, timeout: Optional[float] = None, deadline: Any = None):
            assert self.outstanding == 0, (
                "a fresh lease was taken while one was still held"
            )
            self.outstanding += 1
            self.leases += 1
            self.max_seen = max(self.max_seen, self.outstanding)
            inner = self._inner
            lease = _FakeLease(inner.conn)

            original_close = lease.close

            def close() -> None:
                self.outstanding -= 1
                original_close()

            lease.close = close  # type: ignore[method-assign]
            return lease

    inner = _FakeDb()
    _Seed(inner).add("mem_a", "A content")
    pool = _Max1Pool(inner)
    writer = _mod().ActiveMemoryWriter(pool=pool, pg=None)

    receipt = writer.correct("mem_a", "B content", authority=AUTHORITY)

    assert receipt["success"] is True, receipt
    assert pool.max_seen == 1, "leases must never overlap"
    assert pool.leases >= 2, "verification still needs its own lease"


def test_M01_reader_holds_one_lease_for_the_whole_chain_walk():
    class _CountingPool:
        def __init__(self, inner: _FakeDb) -> None:
            self._inner = inner
            self.leases = 0

        def lease(self, timeout: Optional[float] = None, deadline: Any = None):
            self.leases += 1
            return _FakeLease(self._inner.conn)

    inner, b_id, c_id = _chain_db()
    pool = _CountingPool(inner)
    reader = _mod().ActiveMemoryReader(pool=pool, pg=None)
    out = reader.read_version("mem_a", mode="history")
    assert out["success"] is True, out
    assert pool.leases == 1, (
        f"the whole chain must be read on ONE lease, took {pool.leases}"
    )


# ── provenance truthfulness ────────────────────────────────────────────


def test_M01_source_provenance_distinguishes_real_source_from_tool_request():
    db = _FakeDb(messages={11: {"id": 11, "role": "user"}})
    _Seed(db).add("mem_a", "A content")
    with_src = _correct_a_to_b(db, correction_source_id="11")
    assert with_src["correction_source"]["correction_source_id"] == "11"
    assert with_src["correction_source"]["provenance"] == PROVENANCE
    # never fabricate host/session/event that we do not actually have
    for key in ("host", "session_id", "event_id"):
        assert with_src["correction_source"][key] is None

    db2 = _FakeDb()
    _Seed(db2).add("mem_a", "A content")
    no_src = _correct_a_to_b(db2)
    assert no_src["correction_source"] == {
        "provenance": PROVENANCE,
        "correction_source_id": None,
        "host": None, "session_id": None, "event_id": None,
    }


def test_M01_withdraw_provenance_block_is_explicit_tool_request():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    receipt = _withdraw(db, "mem_a")
    assert receipt["correction_source"] == {
        "provenance": PROVENANCE,
        "correction_source_id": None,
        "host": None, "session_id": None, "event_id": None,
    }


def test_M01_title_and_reason_are_preserved_literally_after_validation():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    title = "  spaced title  "
    reason = "  because the user said so  "
    receipt = _correct_a_to_b(db, replacement_title=title,
                            correction_reason=reason)
    assert receipt["success"] is True, receipt
    edge = db.edges[("from", "mem_a")]
    # non-empty validation passes; the value is stored as supplied
    assert edge["correction_reason"] == reason
    assert receipt["relation"]["correction_reason"] == reason


def test_M01_request_identity_binds_mode_and_full_payload():
    """A withdraw and a replace of the same target are different requests."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    replaced = _correct_a_to_b(db, correction_reason="same reason")
    assert replaced["success"] is True
    withdrawn = _withdraw(db, "mem_a", correction_reason="same reason")
    assert withdrawn["success"] is False, (
        "a different mode against the same target is a conflict"
    )
    assert withdrawn["error_code"] == "CORRECTION_CONFLICT"


# ═══════════════ parent-verified gap fixes (M01 final round) ═══════════════
#
# RED on the previous writer's HEAD before this round:
#   A) _read_snapshot never entered _ReadSnapshot's context, so the read-only
#      transaction was never closed before the lease was released.
#   B) correct() hardcoded new_status='active' / current_memory_id=new_id and
#      could not explain a legitimate B->C or B->withdraw follow-up.
#   C) expected_old was only captured on replace and omitted provenance.
#   D) an indeterminate COMMIT was reported durable=False, and a lease-close
#      error after a known commit was reported as not durable.
#   E) the correction validator and read_version stripped the memory_id,
#      violating the literal-preservation contract.


@contextmanager
def _followup_after_first_commit(db: _FakeDb, fn: Any):
    """Run ``fn(db)`` immediately after the correction's first COMMIT lands."""
    original = _FakeConn.commit
    fired = {"done": False}

    def commit(self: _FakeConn) -> None:
        original(self)
        if not fired["done"] and self._db.commits == 1:
            fired["done"] = True
            fn(self._db)

    _FakeConn.commit = commit  # type: ignore[method-assign]
    try:
        yield
    finally:
        _FakeConn.commit = original  # type: ignore[method-assign]


def _correction_row_of(db: _FakeDb, target: str) -> Optional[str]:
    """The replacement row created from ``target`` (identified by provenance)."""
    for mid, row in db.memories.items():
        prov = row.get("provenance")
        if isinstance(prov, str):
            try:
                prov = json.loads(prov)
            except ValueError:
                prov = {}
        if isinstance(prov, dict) and prov.get("corrected_from") == target:
            return mid
    return None


def _inject_committed_replace(db: _FakeDb, src: str, dst: str, content: str) -> None:
    """Another committed session superseding ``src`` -> ``dst``."""
    db.memories[src]["status"] = "archived"
    db.memories[dst] = {
        "memory_id": dst, "category": "ops", "title": "t", "content": content,
        "tags": ["x"], "provenance": {"corrected_from": src},
        "status": "active", "created_at": _T0, "updated_at": _T1,
        "embedding": None, "embed_model": None,
    }
    edge = {
        "relation_id": "rel_follow_" + dst, "relation_type": "supersedes",
        "from_memory_id": src, "to_memory_id": dst,
        "correction_id": "cor_follow_" + dst, "authority": AUTHORITY,
        "correction_reason": "concurrent", "correction_source_id": None,
        "provenance": PROVENANCE, "effective_at": None, "recorded_at": _T1,
        "request": {},
    }
    db.edges[("from", src)] = edge
    db.edges[("to", dst)] = edge


def _inject_committed_withdraw(db: _FakeDb, src: str) -> None:
    """Another committed session withdrawing ``src``."""
    db.memories[src]["status"] = "archived"
    db.edges[("from", src)] = {
        "relation_id": "rel_follow_wd_" + src, "relation_type": "withdraws",
        "from_memory_id": src, "to_memory_id": None,
        "correction_id": "cor_follow_wd_" + src, "authority": AUTHORITY,
        "correction_reason": "concurrent withdraw", "correction_source_id": None,
        "provenance": PROVENANCE, "effective_at": None, "recorded_at": _T1,
        "request": {},
    }


# ── A) the read-only snapshot transaction is cleaned up before release ──


def test_M01_reader_snapshot_is_rolled_back_before_the_lease_is_released():
    log: list = []
    # In autocommit mode conn.rollback() is a silent no-op, so the read-only
    # snapshot is closed with a real ROLLBACK statement through the cursor.
    original_exec = _FakeCursor.execute

    def execute(self: _FakeCursor, sql: str, params: Any = ()) -> None:
        if _norm(sql) == "ROLLBACK":
            log.append("snapshot_rollback")
        original_exec(self, sql, params)

    class _ObservingPool:
        def __init__(self, inner: _FakeDb) -> None:
            self._inner = inner
            self.outstanding = 0

        def lease(self, timeout: Optional[float] = None, deadline: Any = None):
            assert self.outstanding == 0, "a fresh lease was taken while held"
            self.outstanding += 1
            lease = _FakeLease(self._inner.conn)
            original_close = lease.close

            def close() -> None:
                self.outstanding -= 1
                log.append("lease_release")
                original_close()

            lease.close = close  # type: ignore[method-assign]
            return lease

    inner, b_id, c_id = _chain_db()
    pool = _ObservingPool(inner)
    reader = _mod().ActiveMemoryReader(pool=pool, pg=None)
    _FakeCursor.execute = execute  # type: ignore[method-assign]
    try:
        out = reader.read_version("mem_a", mode="history")
    finally:
        _FakeCursor.execute = original_exec  # type: ignore[method-assign]

    assert out["success"] is True, out
    assert "ROLLBACK" in inner.ops(), (
        "the read-only snapshot transaction was never closed"
    )
    assert log == ["snapshot_rollback", "lease_release"], (
        "the snapshot must be cleaned up BEFORE the lease is released"
    )


# ── B) interleaved follow-ups immediately after the first COMMIT ────────


def test_M01_verify_reports_real_status_when_B_is_superseded_after_commit():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    seen: dict = {}

    def inject(d: _FakeDb) -> None:
        b = _correction_row_of(d, "mem_a")
        seen["b"] = b
        _inject_committed_replace(d, b, "mem_follow_c", "C content")

    with _followup_after_first_commit(db, inject):
        receipt = _correct_a_to_b(db)

    b_id = seen["b"]
    assert receipt["success"] is True, receipt
    assert receipt["new_memory_id"] == b_id, (
        "the receipt must preserve the exact original new_memory_id B"
    )
    assert receipt["new_status"] == "archived", (
        "B was superseded by a concurrent follow-up; never relabel it active"
    )
    assert receipt["current_memory_id"] == "mem_follow_c"
    assert db.memories[b_id]["status"] == "archived"


def test_M01_verify_reports_no_current_when_B_is_withdrawn_after_commit():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    seen: dict = {}

    def inject(d: _FakeDb) -> None:
        b = _correction_row_of(d, "mem_a")
        seen["b"] = b
        _inject_committed_withdraw(d, b)

    with _followup_after_first_commit(db, inject):
        receipt = _correct_a_to_b(db)

    assert receipt["success"] is True, receipt
    assert receipt["new_memory_id"] == seen["b"]
    assert receipt["new_status"] == "archived"
    assert receipt["current_memory_id"] is None, (
        "a legitimate withdrawal to NULL leaves no current — it is not unexplained"
    )


def test_M01_verify_follows_a_longer_concurrent_chain_to_the_real_terminal():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    seen: dict = {}

    def inject(d: _FakeDb) -> None:
        b = _correction_row_of(d, "mem_a")
        seen["b"] = b
        _inject_committed_replace(d, b, "mem_follow_c", "C content")
        _inject_committed_replace(d, "mem_follow_c", "mem_follow_d", "D content")

    with _followup_after_first_commit(db, inject):
        receipt = _correct_a_to_b(db)

    assert receipt["success"] is True, receipt
    assert receipt["new_memory_id"] == seen["b"]
    assert receipt["new_status"] == "archived"
    assert receipt["current_memory_id"] == "mem_follow_d"


# ── C) the old immutable payload + provenance are captured for BOTH modes ──


def test_M01_verify_fails_durable_true_when_old_provenance_changed():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content", provenance={"origin": "seed"})
    original = _FakeCursor._dispatch

    def tamper(self: _FakeCursor, op: str) -> None:
        if op == "read_memory" and "FOR UPDATE" not in self.sql.upper():
            row = self._conn.memories.get(self.params[0])
            if row is not None and row["status"] == "archived":
                row["provenance"] = {"origin": "TAMPERED"}
        original(self, op)

    _FakeCursor._dispatch = tamper  # type: ignore[method-assign]
    try:
        receipt = _correct_a_to_b(db)
    finally:
        _FakeCursor._dispatch = original  # type: ignore[method-assign]

    assert receipt["success"] is False, receipt
    assert receipt["durable"] is True
    assert receipt["error_code"] == "DURABLE_FAILED"
    assert "provenance" in receipt["error"], receipt["error"]


def test_M01_withdraw_verify_fails_durable_true_when_old_payload_corrupted():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content", provenance={"origin": "seed"})
    original = _FakeCursor._dispatch

    def tamper(self: _FakeCursor, op: str) -> None:
        if op == "read_memory" and "FOR UPDATE" not in self.sql.upper():
            row = self._conn.memories.get(self.params[0])
            if row is not None and row["status"] == "archived":
                row["content"] = "TAMPERED"
        original(self, op)

    _FakeCursor._dispatch = tamper  # type: ignore[method-assign]
    try:
        receipt = _withdraw(db, "mem_a")
    finally:
        _FakeCursor._dispatch = original  # type: ignore[method-assign]

    assert receipt["success"] is False, receipt
    assert receipt["durable"] is True
    assert receipt["error_code"] == "DURABLE_FAILED"
    assert "content" in receipt["error"], receipt["error"]


# ── D) commit transport indeterminacy vs a known commit ────────────────


def test_M01_indeterminate_commit_before_persistence_is_not_rolled_back_claim():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    original = _FakeConn.commit

    def commit_raises(self: _FakeConn) -> None:
        raise RuntimeError("commit transport failure (no ack)")

    _FakeConn.commit = commit_raises  # type: ignore[method-assign]
    try:
        receipt = _correct_a_to_b(db)
    finally:
        _FakeConn.commit = original  # type: ignore[method-assign]

    assert receipt["success"] is False, receipt
    assert receipt["durable"] is None, (
        "an indeterminate COMMIT must not be reported as a rolled-back write"
    )
    assert "commit" in receipt["error"].lower() or "不确定" in receipt["error"]
    # nothing durable: the pre-transaction state is what remains
    assert set(db.memories) == {"mem_a"}
    assert db.memories["mem_a"]["status"] == "active"
    assert db.edges == {}


def test_M01_indeterminate_commit_after_persistence_is_durable_none():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    original = _FakeConn.commit

    def commit_raises(self: _FakeConn) -> None:
        # the server DID commit; only the acknowledgement was lost
        self._snapshot = None
        self._db.commits += 1
        self._db.statements.append(("COMMIT", "COMMIT"))
        raise RuntimeError("commit ack lost after persistence")

    _FakeConn.commit = commit_raises  # type: ignore[method-assign]
    try:
        receipt = _correct_a_to_b(db)
    finally:
        _FakeConn.commit = original  # type: ignore[method-assign]

    assert receipt["success"] is False, receipt
    assert receipt["durable"] is None
    assert "commit" in receipt["error"].lower()
    # the commit did land: a rollback must not fabricate an undo
    assert db.memories["mem_a"]["status"] == "archived"
    active = [m for m in db.memories.values() if m["status"] == "active"]
    assert len(active) == 1


def test_M01_known_commit_with_lease_close_error_is_durable_true():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    original = _FakeLease.close
    calls = {"n": 0}

    def close(self: _FakeLease) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            self._closed = True
            raise RuntimeError("lease release failed after a known commit")
        original(self)

    _FakeLease.close = close  # type: ignore[method-assign]
    try:
        # a raw pool lease (not the pg adapter, whose close swallows errors)
        writer = _mod().ActiveMemoryWriter(pool=db, pg=None)
        receipt = writer.correct("mem_a", "B content", authority=AUTHORITY)
    finally:
        _FakeLease.close = original  # type: ignore[method-assign]

    assert receipt["success"] is False, receipt
    assert receipt["durable"] is True, (
        "the COMMIT already returned; a lease-close failure cannot undo it"
    )
    assert receipt["error_code"] == "DURABLE_FAILED"
    assert db.memories["mem_a"]["status"] == "archived"


# ── E) literal id preservation (writer + reader) ───────────────────────


def test_M01_padded_target_is_looked_up_literally_and_writes_nothing():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    padded = "  mem_a  "
    receipt = _writer(db).correct(padded, "B content", authority=AUTHORITY)

    assert receipt["success"] is False, receipt
    assert receipt["error_code"] == "NOT_FOUND", receipt
    assert receipt["old_memory_id"] == padded, (
        "the supplied literal must be preserved in the receipt"
    )
    # the unpadded row is untouched: no write may target a different id
    assert db.memories["mem_a"]["status"] == "active"
    assert db.memories["mem_a"]["content"] == "A content"
    assert set(db.memories) == {"mem_a"}
    assert db.edges == {}


def test_M01_reader_padded_id_is_literal_and_never_resolves_the_unpadded_row():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    padded = "  mem_a  "
    out = _reader(db).read_version(padded)

    assert out["success"] is False, out
    assert out["error_code"] == "NOT_FOUND", out
    assert out["memory_id"] == padded, (
        "lookup/output must preserve the literal id, never the stripped one"
    )
    assert out["memory"] is None


def test_M01_stored_request_preserves_the_literal_target_id():
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    _correct_a_to_b(db)
    req = json.loads(db.edges[("from", "mem_a")]["request"])
    assert req["memory_id"] == "mem_a"


# ═══════════ final transaction-lifetime closure (M01 minimal round) ═══════════
#
# Remaining verified defects on the landed HEAD:
#   A) ActiveMemoryWriter._verify_correction built _ReadSnapshot directly and
#      never closed it, depending on a pool reset that the legacy no-pool
#      PgEmbedStore.lease path does NOT perform (shared _conn, no reset).
#   B) _read_snapshot did not attempt transaction cleanup when the
#      _ReadSnapshot BEGIN failed, and _ReadSnapshot.close swallowed a failed
#      rollback — so a read-only transaction could remain open while the
#      operation reported success.
#   C) correct() checked a missing-table textual shape BEFORE the
#      commit-attempt classification, so an indeterminate COMMIT whose error
#      merely resembled memory_relations was mislabeled MIGRATION_REQUIRED
#      instead of durable=None.


class _ObservingLeasePool:
    """A pool that tracks outstanding leases so release is observable."""

    def __init__(self, inner: _FakeDb) -> None:
        self._inner = inner
        self.outstanding = 0

    def lease(self, timeout: Optional[float] = None, deadline: Any = None):
        self.outstanding += 1
        lease = _FakeLease(self._inner.conn)
        original_close = lease.close

        def close() -> None:
            self.outstanding -= 1
            original_close()

        lease.close = close  # type: ignore[method-assign]
        return lease


def test_M01_verify_readback_closes_readonly_txn_before_release_and_conn_reusable():
    """On the NON-resetting pg-style lease, the verifier's read-only snapshot
    must roll back BEFORE its lease is released, and the shared physical
    connection must serve the next correction and read."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")

    log: list = []
    original_exec = _FakeCursor.execute
    original_close = _FakeLease.close

    def execute(self: _FakeCursor, sql: str, params: Any = ()) -> None:
        if _norm(sql) == "ROLLBACK":
            log.append("snapshot_rollback")
        original_exec(self, sql, params)

    def close(self: _FakeLease) -> None:
        log.append("lease_release")
        original_close(self)

    _FakeCursor.execute = execute  # type: ignore[method-assign]
    _FakeLease.close = close  # type: ignore[method-assign]
    try:
        first = _correct_a_to_b(db)
        assert first["success"] is True, first
        assert db.leases == 2, "one write lease + one fresh readback lease"
        assert log == ["lease_release", "snapshot_rollback", "lease_release"], (
            "the read-only readback snapshot must roll back BEFORE its lease "
            "is released, exactly once"
        )
        b_id = first["new_memory_id"]
        second = _writer(db).correct(b_id, "C content", authority=AUTHORITY)
        assert second["success"] is True, second
        out = _reader(db).read_version("mem_a", mode="history")
        assert out["success"] is True, out
    finally:
        _FakeCursor.execute = original_exec  # type: ignore[method-assign]
        _FakeLease.close = original_close  # type: ignore[method-assign]

    assert second["new_memory_id"] != b_id
    assert db.memories[second["new_memory_id"]]["status"] == "active"


def test_M01_verify_read_failure_still_closes_snapshot_and_stays_durable_true():
    """An induced verify-read failure must still close the read-only snapshot
    before releasing its lease, and the committed write stays durable=True."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")

    log: list = []
    original_close = _FakeLease.close
    original_exec = _FakeCursor.execute

    def close(self: _FakeLease) -> None:
        log.append("lease_release")
        original_close(self)

    def execute(self: _FakeCursor, sql: str, params: Any = ()) -> None:
        # the write path only reads the target FOR UPDATE; any plain
        # read_memory happens inside the read-only readback
        if _norm(sql) == "ROLLBACK":
            log.append("snapshot_rollback")
            original_exec(self, sql, params)
            return
        if self._op(sql) == "read_memory" and "FOR UPDATE" not in _norm(sql):
            raise RuntimeError("synthetic verify read failure")
        original_exec(self, sql, params)

    _FakeLease.close = close  # type: ignore[method-assign]
    _FakeCursor.execute = execute  # type: ignore[method-assign]
    try:
        receipt = _correct_a_to_b(db)
    finally:
        _FakeLease.close = original_close  # type: ignore[method-assign]
        _FakeCursor.execute = original_exec  # type: ignore[method-assign]

    assert receipt["success"] is False, receipt
    assert receipt["durable"] is True, (
        "the COMMIT already returned; a readback failure cannot undo it"
    )
    assert receipt["error_code"] == "DURABLE_FAILED", receipt
    assert "readback" in receipt["error"], receipt["error"]
    assert log == ["lease_release", "snapshot_rollback", "lease_release"], (
        "the read-only snapshot must still be cleaned up before release"
    )
    out = _reader(db).read_version("mem_a")
    assert out["success"] is True, out
    assert db.memories["mem_a"]["status"] == "archived"


def test_M01_read_snapshot_begin_failure_cleans_up_and_releases_the_lease():
    """If the read-only BEGIN fails, transaction cleanup must still be
    attempted and the lease must always be released — explicitly failing."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    db.fail_on = {"BEGIN"}
    pool = _ObservingLeasePool(db)
    reader = _mod().ActiveMemoryReader(pool=pool, pg=None)

    out = reader.read_version("mem_a", mode="history")

    assert out["success"] is False, out
    assert out["error_code"] == "DURABLE_FAILED", out
    assert pool.outstanding == 0, "the lease must be released even if BEGIN failed"
    assert "ROLLBACK" in db.ops(), (
        "a failed BEGIN must still attempt transaction cleanup before release"
    )


def test_M01_read_snapshot_rollback_failure_is_explicit_not_success():
    """A failed read-only cleanup must never be reported as a success."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    pool = _ObservingLeasePool(db)
    reader = _mod().ActiveMemoryReader(pool=pool, pg=None)
    original_exec = _FakeCursor.execute

    def execute(self: _FakeCursor, sql: str, params: Any = ()) -> None:
        if _norm(sql) == "ROLLBACK":
            raise RuntimeError("synthetic snapshot rollback failure")
        original_exec(self, sql, params)

    _FakeCursor.execute = execute  # type: ignore[method-assign]
    try:
        out = reader.read_version("mem_a", mode="history")
    finally:
        _FakeCursor.execute = original_exec  # type: ignore[method-assign]

    assert out["success"] is False, (
        "a failed read-only cleanup must never be reported as success"
    )
    assert out["error_code"] == "DURABLE_FAILED", out
    assert pool.outstanding == 0, "the lease must still be released"


def test_M01_read_snapshot_closes_before_release_on_the_error_path():
    """On a read error, the snapshot rollback still precedes lease release."""
    log: list = []
    original_exec = _FakeCursor.execute

    def execute(self: _FakeCursor, sql: str, params: Any = ()) -> None:
        if _norm(sql) == "ROLLBACK":
            log.append("snapshot_rollback")
        original_exec(self, sql, params)

    class _LoggingPool(_ObservingLeasePool):
        def lease(self, timeout: Optional[float] = None, deadline: Any = None):
            lease = super().lease(timeout=timeout, deadline=deadline)
            original_close = lease.close

            def close() -> None:
                log.append("lease_release")
                original_close()

            lease.close = close  # type: ignore[method-assign]
            return lease

    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    pool = _LoggingPool(db)
    reader = _mod().ActiveMemoryReader(pool=pool, pg=None)

    _FakeCursor.execute = execute  # type: ignore[method-assign]
    try:
        out = reader.read_version("mem_nope", mode="current")
    finally:
        _FakeCursor.execute = original_exec  # type: ignore[method-assign]

    assert out["success"] is False, out
    assert out["error_code"] == "NOT_FOUND", out
    assert log == ["snapshot_rollback", "lease_release"], log
    assert pool.outstanding == 0


def test_M01_indeterminate_commit_resembling_missing_table_is_durable_none():
    """An indeterminate COMMIT must dominate a textual missing-table shape:
    durable=None, never MIGRATION_REQUIRED, and no persistence fabricated."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    original = _FakeConn.commit

    def commit_raises(self: _FakeConn) -> None:
        raise RuntimeError('relation "public.memory_relations" does not exist')

    _FakeConn.commit = commit_raises  # type: ignore[method-assign]
    try:
        receipt = _correct_a_to_b(db)
    finally:
        _FakeConn.commit = original  # type: ignore[method-assign]

    assert receipt["success"] is False, receipt
    assert receipt["durable"] is None, (
        "an indeterminate COMMIT must dominate a missing-table-looking error"
    )
    assert receipt.get("commit_indeterminate") is True, receipt
    assert receipt["error_code"] == "DURABLE_FAILED", receipt
    assert receipt["error_code"] != "MIGRATION_REQUIRED", receipt
    # do not fabricate persistence: the pre-transaction state remains
    assert set(db.memories) == {"mem_a"}
    assert db.memories["mem_a"]["status"] == "active"
    assert db.edges == {}
    assert "COMMIT" not in db.ops()


# ═══════ real-connection snapshot opening (non-autocommit transaction) ═══════
#
# On a real psycopg2 connection that is NOT in autocommit mode, the driver
# keeps an implicit transaction open once any query has run. Executing
# "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY" there makes the server run
# BEGIN + SET TRANSACTION ISOLATION LEVEL, and PostgreSQL refuses the SET with
# "SET TRANSACTION ISOLATION LEVEL must be called before any query"
# (ActiveSqlTransaction). _ReadSnapshot must end that open transaction BEFORE
# opening the snapshot, without downgrading REPEATABLE READ / READ ONLY and
# without adding a statement to the clean autocommit path.

_PLAIN_MEMORY_READ = (
    "SELECT memory_id, category, title, content, tags, provenance, status, "
    "created_at, updated_at, embedding, embed_model "
    "FROM public.explicit_memories WHERE memory_id = %s"
)


def _open_nonautocommit_txn_with_query(db: _FakeDb) -> _FakeConn:
    """Put ``db.conn`` in the state the real database was in: non-autocommit
    with an already-executed query inside the open transaction."""
    assert db.autocommit is False
    conn = db.conn
    conn.cursor().execute(_PLAIN_MEMORY_READ, ("mem_a",))
    assert conn.in_transaction is True, "fake must report an open transaction"
    return conn


def test_M01_read_snapshot_opens_on_nonautocommit_conn_with_open_query():
    db = _FakeDb(autocommit=False)
    _Seed(db).add("mem_a", "A content")
    conn = _open_nonautocommit_txn_with_query(db)
    before = len(db.statements)

    snap = _mod()._ReadSnapshot(conn)
    try:
        assert snap.read_one("mem_a") is not None
    finally:
        snap.close()

    ops = [op for op, _ in db.statements[before:]]
    assert ops[:2] == ["ROLLBACK", "BEGIN"], (
        "the open transaction must be ended BEFORE the BEGIN-with-isolation"
    )
    begin_sql = [sql for op, sql in db.statements[before:] if op == "BEGIN"]
    assert len(begin_sql) == 1
    assert "REPEATABLE READ" in begin_sql[0]
    assert "READ ONLY" in begin_sql[0]


def test_M01_read_snapshot_leaves_nonautocommit_conn_out_of_transaction():
    db = _FakeDb(autocommit=False)
    _Seed(db).add("mem_a", "A content")
    conn = _open_nonautocommit_txn_with_query(db)

    snap = _mod()._ReadSnapshot(conn)
    assert snap.read_one("mem_a") is not None
    snap.close()

    assert conn.in_transaction is False, (
        "close() must leave the borrowed connection out of any transaction"
    )


def test_M01_read_snapshot_opens_on_nonautocommit_conn_without_prior_query():
    db = _FakeDb(autocommit=False)
    _Seed(db).add("mem_a", "A content")
    conn = db.conn
    assert conn.in_transaction is False

    snap = _mod()._ReadSnapshot(conn)
    try:
        assert snap.read_one("mem_a") is not None
    finally:
        snap.close()

    assert conn.in_transaction is False


def test_M01_read_snapshot_autocommit_path_keeps_exact_statement_sequence():
    db = _FakeDb()  # autocommit=True: the clean, pre-existing path
    _Seed(db).add("mem_a", "A content")
    conn = db.conn
    assert conn.autocommit is True

    snap = _mod()._ReadSnapshot(conn)
    assert snap.read_one("mem_a") is not None
    snap.close()

    assert db.ops() == ["BEGIN", "read_memory", "ROLLBACK"], db.ops()


def test_M01_read_snapshot_begin_keeps_repeatable_read_and_read_only():
    """Static guard: the snapshot BEGIN is never downgraded."""
    text = (V3CORE_SRC / "active_memory_store.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    snapshot_cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "_ReadSnapshot"
    )
    begins = [
        n.value for n in ast.walk(snapshot_cls)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value.strip().upper().startswith("BEGIN")
    ]
    assert begins == ["BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"], begins
    literals = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert not any(
        "SET SESSION CHARACTERISTICS" in s.upper() for s in literals
    ), "a session-level characteristics change is not an acceptable substitute"


def test_M01_negative_control_old_begin_first_hits_activetx_on_open_txn():
    """Negative control: the OLD first statement raises the exact
    ActiveSqlTransaction error the real database returned, and the fixed
    _ReadSnapshot path no longer hits it. No database is launched."""
    db = _FakeDb(autocommit=False)
    _Seed(db).add("mem_a", "A content")
    conn = _open_nonautocommit_txn_with_query(db)

    with pytest.raises(RuntimeError) as excinfo:
        conn.cursor().execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    assert "must be called before any query" in str(excinfo.value)

    snap = _mod()._ReadSnapshot(conn)
    try:
        assert snap.read_one("mem_a") is not None
    finally:
        snap.close()


# ═══ real-connection transaction ownership (autocommit no-op closure) ═══
#
# psycopg2's commit()/rollback() are SILENT NO-OPS while autocommit is True,
# and PgPool delivers every connection in autocommit mode. The correction's
# explicit BEGIN ... COMMIT transaction and the read snapshot's cleanup must
# therefore not rely on those calls. The fake now models the real driver: an
# explicit BEGIN opens a transaction even in autocommit mode, an explicit
# ROLLBACK statement ends it, while the commit()/rollback() METHODS do
# nothing at all there.

_TX_IDLE = 0
_TX_INTRANS = 2


def test_M01_correction_really_commits_on_autocommit_delivered_connection():
    db = _FakeDb()  # autocommit=True — the pool's delivered baseline
    _Seed(db).add("mem_a", "A content")
    receipt = _correct_a_to_b(db)
    assert receipt["success"] is True, receipt
    assert db.conn.autocommit is True, "delivered baseline must be restored"
    ops = db.ops()
    assert ops.count("COMMIT") == 1, ops
    # a real COMMIT reached the connection (not a silent autocommit no-op)
    assert ("COMMIT", "COMMIT") in db.statements, db.statements
    assert db.memories["mem_a"]["status"] == "archived"
    assert db.conn.get_transaction_status() == _TX_IDLE, (
        "the correction must not leave its transaction open"
    )
    assert db.conn.in_transaction is False


def test_M01_negative_control_autocommit_commit_is_a_silent_noop():
    """The PRE-FIX shape: commit()/rollback() under autocommit do nothing, so
    an explicit BEGIN leaves the connection INTRANS and never committed."""
    db = _FakeDb()  # autocommit=True
    _Seed(db).add("mem_a", "A content")
    conn = db.conn
    conn.cursor().execute("BEGIN")
    conn.cursor().execute(_PLAIN_MEMORY_READ, ("mem_a",))
    assert conn.get_transaction_status() == _TX_INTRANS

    conn.commit()  # the pre-fix correction relied on this
    assert conn.get_transaction_status() == _TX_INTRANS, (
        "commit() is a silent no-op in autocommit mode"
    )
    conn.rollback()  # ...and so is this
    assert conn.get_transaction_status() == _TX_INTRANS, (
        "rollback() is a silent no-op in autocommit mode"
    )
    assert "COMMIT" not in db.ops() and "ROLLBACK" not in db.ops()
    # only an explicit statement actually ends it
    conn.cursor().execute("ROLLBACK")
    assert conn.get_transaction_status() == _TX_IDLE


def test_M01_correction_on_autocommit_conn_leaves_no_open_txn_and_read_opens():
    db = _FakeDb()  # autocommit=True
    _Seed(db).add("mem_a", "A content")
    first = _correct_a_to_b(db)
    assert first["success"] is True, first
    assert db.conn.get_transaction_status() == _TX_IDLE, (
        "the correction left a transaction open for the next lease"
    )
    assert db.conn.in_transaction is False
    # the following read snapshot must open (BEGIN ISOLATION LEVEL ...) rather
    # than fail with ActiveSqlTransaction on the leaked open transaction
    out = _reader(db).read_version("mem_a", mode="history")
    assert out["success"] is True, out


def test_M01_read_snapshot_ends_really_open_txn_when_autocommit_flag_is_true():
    db = _FakeDb()  # autocommit=True, yet a transaction is REALLY open
    _Seed(db).add("mem_a", "A content")
    conn = db.conn
    conn.cursor().execute("BEGIN")
    conn.cursor().execute(_PLAIN_MEMORY_READ, ("mem_a",))
    assert conn.get_transaction_status() == _TX_INTRANS
    assert conn.autocommit is True
    before = len(db.statements)

    snap = _mod()._ReadSnapshot(conn)
    try:
        assert snap.read_one("mem_a") is not None
        begin_sql = [sql for op, sql in db.statements[before:] if op == "BEGIN"]
        assert len(begin_sql) == 1
        assert "REPEATABLE READ" in begin_sql[0] and "READ ONLY" in begin_sql[0]
    finally:
        snap.close()

    ops = [op for op, _ in db.statements[before:]]
    assert ops[0] == "ROLLBACK", (
        "the really-open transaction must be ended before BEGIN ISOLATION"
    )
    assert conn.get_transaction_status() == _TX_IDLE


def test_M01_snapshot_begin_not_downgraded_static_guard():
    text = (V3CORE_SRC / "active_memory_store.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    snapshot_cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "_ReadSnapshot"
    )
    begins = [
        n.value for n in ast.walk(snapshot_cls)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value.strip().upper().startswith("BEGIN")
    ]
    assert begins == ["BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"], begins
    literals = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert not any("SET SESSION CHARACTERISTICS" in s.upper() for s in literals), (
        "a session-characteristics downgrade is not an acceptable substitute"
    )


def test_M01_correction_takes_and_restores_txn_ownership_before_release():
    db = _FakeDb()  # delivered baseline autocommit=True
    _Seed(db).add("mem_a", "A content")
    seen: dict = {}
    order: list = []
    original_exec = _FakeCursor.execute
    original_close = _FakeLease.close

    def execute(self: _FakeCursor, sql: str, params: Any = ()) -> None:
        if _norm(sql) == "BEGIN":
            seen["autocommit_at_begin"] = self._conn.autocommit
        original_exec(self, sql, params)

    def close(self: _FakeLease) -> None:
        order.append(self.connection.autocommit)
        original_close(self)

    _FakeCursor.execute = execute  # type: ignore[method-assign]
    _FakeLease.close = close  # type: ignore[method-assign]
    try:
        receipt = _correct_a_to_b(db)
    finally:
        _FakeCursor.execute = original_exec  # type: ignore[method-assign]
        _FakeLease.close = original_close  # type: ignore[method-assign]

    assert receipt["success"] is True, receipt
    assert seen.get("autocommit_at_begin") is False, (
        "the driver must own the transaction before the explicit BEGIN"
    )
    assert order and all(ac is True for ac in order), (
        "the delivered autocommit baseline must be restored before release"
    )
    assert db.conn.autocommit is True
    assert db.conn.get_transaction_status() == _TX_IDLE

    # a connection delivered NON-autocommit keeps its own baseline untouched
    db2 = _FakeDb(autocommit=False)
    _Seed(db2).add("mem_a", "A content")
    receipt2 = _correct_a_to_b(db2)
    assert receipt2["success"] is True, receipt2
    assert db2.conn.autocommit is False
    assert db2.conn.get_transaction_status() == _TX_IDLE


# ═══════════ concurrent-correction races (M01 idempotency closure) ═══════════
#
# A target has exactly ONE outgoing edge (unique index on from_memory_id), so
# when N corrections of the SAME target run concurrently one wins that single
# slot and the others lose it. The loser's write is refused by the driver:
#
#   * IDENTICAL request — the winner already committed the deterministic
#     replacement id, so INSERT ... ON CONFLICT (memory_id) DO NOTHING
#     RETURNING returns no row (and the outgoing-edge unique index refuses the
#     duplicate edge too);
#   * DIVERGENT request — the loser's own replacement row is new, but the
#     outgoing-edge unique index raises UniqueViolation on from_memory_id.
#
# Neither is an internal failure. The loser must re-read the COMMITTED edge
# and answer deterministically: an idempotent replay for an identical request,
# CORRECTION_CONFLICT pointing at the winner for a divergent one — never a
# hard DURABLE_FAILED and never a raw UniqueViolation.
#
# The race is driven deterministically by committing the winner's state
# mid-transaction, at the exact statement where the loser would otherwise
# collide (an interleaved fake connection/edge state, no threads, no timing).


def _winner_receipt(**kw: Any) -> tuple:
    """A committed winner correction, run on its own twin database."""
    twin = _FakeDb()
    _Seed(twin).add("mem_a", "A content")
    return twin, _correct_a_to_b(twin, **kw)


def _install_committed_winner(
    db: _FakeDb, winner_db: _FakeDb, receipt: dict,
) -> None:
    """Install ``receipt``'s committed correction as ANOTHER session's write.

    It must survive any rollback of the in-flight loser transaction, so the
    loser's undo entries for the rows the winner owns are dropped.
    """
    src, dst = receipt["old_memory_id"], receipt["new_memory_id"]
    db.memories[src]["status"] = "archived"
    db.memories[dst] = dict(winner_db.memories[dst])
    edge = dict(winner_db.edges[("from", src)])
    db.edges[("from", src)] = edge
    db.edges[("to", dst)] = edge
    db.conn._forget_undo("memories", dst)
    db.conn._forget_undo("edges", ("from", src))
    db.conn._forget_undo("edges", ("to", dst))


@contextmanager
def _winner_commits_before(
    db: _FakeDb, *, before_op: str, winner_db: _FakeDb, receipt: dict,
):
    """Commit the winner's state just before the loser's first ``before_op``.

    That is the real interleaving: the loser has already taken its lock-free
    decisions (target active, no outgoing edge) and its next write collides
    with the winner's now-committed rows.
    """
    original = _FakeCursor.execute
    fired = {"done": False}

    def execute(self: _FakeCursor, sql: str, params: Any = ()) -> None:
        if not fired["done"] and _FakeCursor._op(sql) == before_op:
            fired["done"] = True
            _install_committed_winner(db, winner_db, receipt)
        original(self, sql, params)

    _FakeCursor.execute = execute  # type: ignore[method-assign]
    try:
        yield fired
    finally:
        _FakeCursor.execute = original  # type: ignore[method-assign]


def _assert_idempotent_replay(
    receipt: dict, *, target: str, winner_id: str,
) -> None:
    """The loser's truthful idempotent-replay verdict, backed by real reads."""
    assert receipt["success"] is True, receipt
    assert receipt["status"] == "CORRECTION_DEDUPLICATED", receipt
    assert receipt["deduplicated"] is True, receipt
    assert receipt["durable"] is True, receipt
    assert receipt.get("error_code") is None, receipt
    assert receipt["old_memory_id"] == target, receipt
    assert receipt["new_memory_id"] == winner_id, receipt
    assert receipt["current_memory_id"] == winner_id, receipt
    assert receipt["relation"] is not None, receipt
    assert receipt["relation"]["to_memory_id"] == winner_id, receipt
    assert "DURABLE_FAILED" not in json.dumps(receipt), receipt


def _assert_deterministic_conflict(
    receipt: dict, *, target: str, winner_id: str,
) -> None:
    """The divergent loser's deterministic CORRECTION_CONFLICT verdict."""
    assert receipt["success"] is False, receipt
    assert receipt["status"] == "CORRECTION_CONFLICT", receipt
    assert receipt["error_code"] == "CORRECTION_CONFLICT", receipt
    assert receipt["current_memory_id"] == winner_id, receipt
    assert receipt["new_memory_id"] is None, receipt
    assert "UniqueViolation" not in json.dumps(receipt), receipt
    assert "DURABLE_FAILED" not in json.dumps(receipt), receipt


@pytest.mark.parametrize("before_op", ["insert_memory", "insert_edge"])
def test_M01_concurrent_identical_corrections_converge_on_one_version(before_op):
    """N identical concurrent corrections: one commit, losers replay it."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    winner_db, winner = _winner_receipt(correction_reason="same")

    with _winner_commits_before(db, before_op=before_op, winner_db=winner_db,
                                receipt=winner) as fired:
        loser = _correct_a_to_b(db, correction_reason="same")

    assert fired["done"], "the race interleaving never fired"
    _assert_idempotent_replay(loser, target="mem_a",
                              winner_id=winner["new_memory_id"])
    # exactly ONE committed version and ONE relation survive the race
    assert set(db.memories) == {"mem_a", winner["new_memory_id"]}, db.memories
    assert db.memories["mem_a"]["status"] == "archived"
    assert db.memories["mem_a"]["content"] == "A content"
    assert set(db.edges) == {("from", "mem_a"), ("to", winner["new_memory_id"])}
    # the replay verdict is BACKED BY the committed edge
    assert (loser["relation"]["relation_id"]
            == db.edges[("from", "mem_a")]["relation_id"])
    assert (loser["relation"]["correction_id"]
            == db.edges[("from", "mem_a")]["correction_id"])


@pytest.mark.parametrize("before_op", ["insert_memory", "insert_edge"])
def test_M01_concurrent_divergent_corrections_are_deterministic_conflicts(before_op):
    """N divergent concurrent corrections: one winner, losers get CONFLICT."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    winner_db, winner = _winner_receipt(correction_reason="winner")

    with _winner_commits_before(db, before_op=before_op, winner_db=winner_db,
                                receipt=winner) as fired:
        loser = _writer(db).correct("mem_a", "Z content", authority=AUTHORITY,
                                    correction_reason="loser")

    assert fired["done"], "the race interleaving never fired"
    _assert_deterministic_conflict(loser, target="mem_a",
                                   winner_id=winner["new_memory_id"])
    # no second current version and no second relation were created
    assert set(db.memories) == {"mem_a", winner["new_memory_id"]}, db.memories
    assert set(db.edges) == {("from", "mem_a"), ("to", winner["new_memory_id"])}
    assert db.memories["mem_a"]["status"] == "archived"


def test_M01_concurrent_loser_observing_intermediate_readback_answers_deterministically():
    """A readback that observes the target still active is a RACE observation,
    not a durability failure: re-read the committed edge and decide."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    original = _FakeCursor._dispatch
    fired = {"done": False}

    def dispatch(self: _FakeCursor, op: str) -> None:
        original(self, op)
        if (op == "read_memory" and "FOR UPDATE" not in _norm(self.sql)
                and not fired["done"] and tuple(self.params) == ("mem_a",)):
            fired["done"] = True
            row = self._conn.memories.get("mem_a")
            if row is not None:
                stale = dict(row)
                stale["status"] = "active"   # the intermediate observation
                self._one = tuple(stale[c] for c in MEMORY_COLS)

    _FakeCursor._dispatch = dispatch  # type: ignore[method-assign]
    try:
        receipt = _correct_a_to_b(db)
    finally:
        _FakeCursor._dispatch = original  # type: ignore[method-assign]

    assert fired["done"], "the intermediate readback never happened"
    assert receipt["status"] == "CORRECTION_DEDUPLICATED", receipt
    assert receipt["deduplicated"] is True, receipt
    assert receipt["durable"] is True, receipt
    assert receipt.get("error_code") is None, receipt
    assert "readback" not in (receipt.get("error") or ""), receipt
    # the verdict is backed by the committed state, not by the stale read
    active = [m for m in db.memories.values() if m["status"] == "active"]
    assert len(active) == 1, active
    assert receipt["new_memory_id"] == active[0]["memory_id"]
    assert receipt["current_memory_id"] == receipt["new_memory_id"]
    assert (receipt["relation"]["relation_id"]
            == db.edges[("from", "mem_a")]["relation_id"])
    assert db.memories["mem_a"]["status"] == "archived"


def test_M01_negative_control_pre_fix_race_receipts_fail_the_new_assertions():
    """The PRE-FIX shapes — a race loser reported as a hard DURABLE_FAILED —
    must FAIL the assertions the fixed paths now make."""
    pre_fix_identical = {
        "success": False,
        "status": "DURABLE_FAILED",
        "durable": False,
        "deduplicated": False,
        "error_code": "DURABLE_FAILED",
        "old_memory_id": "mem_a",
        "new_memory_id": "mem_x",
        "current_memory_id": None,
        "relation": None,
        "error": "replacement memory_id 已被相同 payload 占用；未覆盖任何行",
    }
    with pytest.raises(AssertionError):
        _assert_idempotent_replay(pre_fix_identical, target="mem_a",
                                  winner_id="mem_x")

    pre_fix_divergent = {
        "success": False,
        "status": "DURABLE_FAILED",
        "durable": False,
        "deduplicated": False,
        "error_code": "DURABLE_FAILED",
        "old_memory_id": "mem_a",
        "new_memory_id": None,
        "current_memory_id": None,
        "relation": None,
        "error": (
            "纠正事务失败: UniqueViolation('duplicate key value violates "
            "unique constraint \"memory_relations_from_memory_id_key\" "
            "DETAIL: Key (from_memory_id)=(mem_a) already exists')"
        ),
    }
    with pytest.raises(AssertionError):
        _assert_deterministic_conflict(pre_fix_divergent, target="mem_a",
                                       winner_id="mem_x")


def test_M01_indeterminate_commit_with_a_committed_edge_stays_durable_none():
    """A genuine indeterminate COMMIT is never folded into the dedupe path."""
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")
    winner_db, winner = _winner_receipt(correction_reason="same")
    original = _FakeConn.commit

    def commit_raises(self: _FakeConn) -> None:
        _install_committed_winner(db, winner_db, winner)
        raise RuntimeError("commit ack lost after persistence")

    _FakeConn.commit = commit_raises  # type: ignore[method-assign]
    try:
        receipt = _correct_a_to_b(db, correction_reason="same")
    finally:
        _FakeConn.commit = original  # type: ignore[method-assign]

    assert receipt["success"] is False, receipt
    assert receipt["durable"] is None, (
        "an indeterminate COMMIT must stay unknown-durability, never deduped"
    )
    assert receipt.get("commit_indeterminate") is True, receipt
    assert receipt.get("deduplicated") is not True, receipt
    assert receipt["error_code"] == "DURABLE_FAILED", receipt
    assert receipt["status"] != "CORRECTION_DEDUPLICATED", receipt


# ═══════════ pool-less store: a lease must be an INDEPENDENT connection ══════
#
# The real serve runtime builds ``ActiveMemoryWriter(pool=None, pg=PgEmbedStore)``
# (V3Core has no pg_pool; serve forwards ``core.pg_pool or core._pg_pool``). A
# pool-less ``PgEmbedStore.lease()`` yields its ONE cached ``_conn`` to every
# caller, so three concurrent corrections share one physical connection AND one
# transaction: the first loser's ``rollback()`` undoes the winner's writes and
# the "fresh" readback reads inside that shared, half-rolled-back transaction —
# exactly the 0/3 DURABLE_FAILED seen on real PostgreSQL. ``PgEmbedStore`` also
# exposes ``open_side_connection()``, its public independent-connection seam; a
# lease MUST prefer it, or no correction can ever be serialized per-connection.
#
# These tests pin the lease seam itself (the deterministic fake can only model a
# shared connection, which is why the real race has to be reproduced against a
# real PostgreSQL in the integration probe).


class _SentinelConn:
    """A connection-shaped object that is never asked to run SQL."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.autocommit = True
        self.closed = False

    def get_transaction_status(self) -> int:
        return 0  # TRANSACTION_STATUS_IDLE: cleanup is a no-op

    def close(self) -> None:
        self.closed = True


class _SentinelLease:
    def __init__(self, conn: _SentinelConn) -> None:
        self.connection = conn

    def close(self) -> None:
        pass

    def __enter__(self) -> _SentinelConn:
        # a real lease context yields the CONNECTION, not the lease wrapper
        return self.connection

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


class _SharedLegacyStore:
    """Pool-less store: ONE shared connection + the independent side seam."""

    def __init__(self) -> None:
        self.shared = _SentinelConn("shared")
        self.lease_calls = 0
        self.side_calls = 0

    def lease(self, timeout: Optional[float] = None):
        self.lease_calls += 1
        return _SentinelLease(self.shared)

    def open_side_connection(self):
        self.side_calls += 1
        return _SentinelConn(f"side-{self.side_calls}")


class _LeaseOnlyStore:
    """Pool-less store WITHOUT an independent seam (the injected fakes)."""

    def __init__(self) -> None:
        self.shared = _SentinelConn("shared")
        self.lease_calls = 0

    def lease(self, timeout: Optional[float] = None):
        self.lease_calls += 1
        return _SentinelLease(self.shared)


def test_M01_pool_less_store_lease_is_an_independent_connection():
    store = _SharedLegacyStore()
    lease_a = _mod()._acquire_lease(None, store)
    lease_b = _mod()._acquire_lease(None, store)
    try:
        assert lease_a.connection is not lease_b.connection, (
            "a correction must not share one physical connection with a "
            "concurrent request"
        )
        assert lease_a.connection is not store.shared, (
            "the shared PgEmbedStore._conn must never be handed to a "
            "correction lease while an independent seam exists"
        )
        assert lease_a.connection.closed is False
    finally:
        lease_a.close()
        lease_b.close()
    assert store.side_calls == 2, store.side_calls
    assert store.lease_calls == 0, (
        "the shared lease must not be used while an independent seam exists"
    )
    # the owned lease closes the connection it took
    assert lease_a.connection.closed is True


def test_M01_pool_less_store_without_independent_seam_keeps_legacy_lease():
    store = _LeaseOnlyStore()
    lease = _mod()._acquire_lease(None, store)
    try:
        assert lease.connection is store.shared, (
            "a store with no independent seam must keep the legacy lease path"
        )
    finally:
        lease.close()
    assert store.lease_calls == 1, store.lease_calls


class _BrokenSeamStore:
    """Pool-less store whose independent seam EXISTS but cannot produce a conn.

    ``open_side_connection`` is present (so the legacy shared-connection path
    must NOT be taken) yet returns ``None`` or raises. Fail-closed is the only
    correct behaviour: borrowing ``lease()``'s shared connection would put the
    correction back on the single physical connection the seam exists to avoid.
    """

    def __init__(self, *, raises: bool) -> None:
        self.shared = _SentinelConn("shared")
        self.lease_calls = 0
        self.side_calls = 0
        self._raises = raises

    def lease(self, timeout: Optional[float] = None):
        self.lease_calls += 1
        return _SentinelLease(self.shared)

    def open_side_connection(self):
        self.side_calls += 1
        if self._raises:
            raise RuntimeError("side connection refused")
        return None


@pytest.mark.parametrize("raises", [False, True], ids=["returns-none", "raises"])
def test_M01_pool_less_store_broken_independent_seam_fails_closed(raises):
    """A callable seam that yields no connection must FAIL CLOSED.

    The store's ``open_side_connection`` is the only source of an independent
    connection; if it returns ``None`` or raises, borrowing ``lease()``'s
    shared ``_conn`` would silently reintroduce the cross-request transaction
    corruption the seam exists to prevent. The lease acquisition must raise
    ``_PoolUnavailable`` and never touch the shared lease.
    """
    store = _BrokenSeamStore(raises=raises)
    with pytest.raises(_mod()._PoolUnavailable):
        _mod()._acquire_lease(None, store)
    assert store.side_calls == 1, store.side_calls
    assert store.lease_calls == 0, (
        "an unusable independent seam must never fall back to the store's "
        "shared lease connection"
    )


@pytest.mark.parametrize("raises", [False, True], ids=["returns-none", "raises"])
def test_M01_broken_independent_seam_is_a_durable_false_failure(raises):
    """The fail-closed lease error surfaces as the canonical durable=False.

    The writer already maps a lease-acquisition failure to
    ``_correction_failure(ERR_DURABLE_FAILED, ...)``; a broken independent seam
    must reach that same path (nothing written, nothing borrowed) rather than
    succeeding on the shared connection.
    """
    db = _FakeDb()
    _Seed(db).add("mem_a", "A content")

    def _broken_side():
        if raises:
            raise RuntimeError("side connection refused")
        return None

    db.open_side_connection = _broken_side  # type: ignore[attr-defined]

    receipt = _correct_a_to_b(db)

    assert receipt["success"] is False, receipt
    assert receipt["durable"] is False, (
        "a correction that never obtained a real connection must not claim "
        "durability"
    )
    assert receipt["error_code"] == "DURABLE_FAILED", receipt
    assert db.leases == 0, (
        "the writer must not borrow the shared connection when the "
        "independent seam is unusable"
    )


