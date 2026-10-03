# -*- coding: utf-8 -*-
"""M03 Topic rebuild — RED→GREEN contract tests.

Locked contract (docs/M03-CORRECTION-PROPAGATION.md §"Rebuild", §19-§21,
§28, §31, §32; docs/M03-DERIVED-LINEAGE-MAP.md):

  * ``v3core.derived_rebuild.rebuild_topic`` is LOCAL and ON DEMAND: one
    committed correction, one bounded rebuild. No full historical rebuild, no
    full re-embedding, no Observer / E1 rerun.
  * Only ``derived_kind='topic'`` is rebuilt. ``observer_note`` /
    ``yin_paragraph`` come back ``REBUILD_NOT_SUPPORTED`` — reported, not
    architected around.
  * The generation context is assembled through the CANONICAL correction state
    (the M02 ``A -> B`` edge + the canonical chain walk): A is explicitly
    ``historical/superseded``, B is explicitly ``current``, and the correction
    relation is explicit. The provider is never handed the old raw A alone.
  * Success produces a NEW artifact ``T2`` (a new ``topics`` row) whose id is
    deterministic::

        T2 = 't_' + md5(f"{T}|rebuild|{correction_id}")[:8]

    and advances the old sidecar row to ``state='rebuilt'`` /
    ``resolved_at`` / ``replacement_derived_id=T2``.
  * §31: ``old T stale / new T2 active / replacement_derived_id = T2`` →
    current recall keeps ``T2 allowed`` and ``T suppressed``. Suppression is
    driven by the row's EXISTENCE, never by ``state``.
  * Provider unavailable / failing → ``REBUILD_PROVIDER_UNAVAILABLE`` /
    ``REBUILD_FAILED``: the sidecar is untouched, NO artifact is produced, NO
    text is invented, T stays suppressed and the correction is never rolled
    back.
  * Idempotent: a second successful rebuild of the same correction returns the
    SAME T2 with ZERO new rows and reports ``REBUILD_DEDUPLICATED``.
  * ``dry_run=True`` writes nothing. A missing sidecar table is a truthful
    ``MIGRATION_REQUIRED``.
  * Raw rows (``conversation_stream`` / ``qa_pairs`` / ``explicit_memories``)
    are never written, and nothing is ever DELETEd.

No real PostgreSQL: the P0-A conftest hard-blocks ``psycopg2.connect``, so this
contract is proven against a deterministic in-memory fake that models leases,
transaction snapshots, ON CONFLICT DO NOTHING, the canonical chain walk and the
sidecar read/update surface.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

MODULE = "v3core.derived_rebuild"
TOOL_MODULE = "v3core.tools.derived_rebuild_tool"

REPO_ROOT = Path(__file__).resolve().parents[3]
V3CORE_SRC = REPO_ROOT / "src" / "v3-core" / "src" / "v3core"

_T0 = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
_T1 = datetime(2026, 10, 1, 0, 1, 0, tzinfo=timezone.utc)

MEMORY_COLS = (
    "memory_id", "category", "title", "content", "tags", "provenance",
    "status", "created_at", "updated_at", "embedding", "embed_model",
)
RELATION_COLS = (
    "relation_id", "relation_type", "from_memory_id", "to_memory_id",
    "correction_id", "authority", "correction_reason", "correction_source_id",
    "provenance", "effective_at", "recorded_at", "request",
)
SIDECAR_COLS = (
    "invalidation_id", "correction_id", "relation_id", "derived_kind",
    "derived_id", "source_memory_id", "replacement_memory_id", "state",
    "canonical_qa_id", "input_source_kind", "input_source_id",
    "created_at", "resolved_at", "replacement_derived_id",
)
TOPIC_COLS = (
    "topic_id", "title", "summary", "body", "keywords", "note_ref", "status",
)

CORRECTION_ID = "cor_ab"
RELATION_ID = "rel_ab"
SOURCE_MEMORY = "mem_a"
REPLACEMENT_MEMORY = "mem_b"
OLD_TOPIC = "t_d1"
CANONICAL_QA_ID = 42

A_CONTENT = "A content: the deploy window is Friday."
B_CONTENT = "B content: the deploy window is Monday."
OLD_TOPIC_BODY = "Topic body derived from A: deploy Friday."


# ── deterministic in-memory fake PG ───────────────────────────────────────

def _norm(sql: str) -> str:
    return " ".join(sql.split()).upper()


_MISSING = object()


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self.sql = ""
        self.params: tuple = ()
        self._one: Optional[tuple] = None
        self._many: list = []
        self.rowcount = -1

    @staticmethod
    def _op(sql: str) -> str:
        s = _norm(sql)
        if s in ("COMMIT", "ROLLBACK"):
            return s
        if s.startswith("BEGIN"):
            return "BEGIN"
        if "INSERT INTO PUBLIC.TOPICS" in s:
            return "insert_topic"
        if "UPDATE PUBLIC.DERIVED_MEMORY_INVALIDATIONS" in s:
            return "update_invalidation"
        if "FROM PUBLIC.DERIVED_MEMORY_INVALIDATIONS" in s:
            return "read_invalidations"
        if "FROM PUBLIC.TOPICS" in s:
            return "read_topics"
        if "FROM PUBLIC.MEMORY_RELATIONS" in s:
            if "AUTHORITY" in s:
                return "read_relation_full"
            return "read_relation_edge"
        if "FROM PUBLIC.EXPLICIT_MEMORIES" in s:
            return "read_memory"
        raise AssertionError(f"fake pg: unmapped SQL — {s[:200]}")

    def _guard(self, op: str) -> None:
        conn = self._conn
        conn.statements.append((op, _norm(self.sql)))
        if conn.missing_invalidations and op in {
            "read_invalidations", "insert_topic", "update_invalidation",
        }:
            raise RuntimeError(
                'relation "public.derived_memory_invalidations" does not exist'
            )
        if op in conn.fail_on:
            raise RuntimeError(f"synthetic failure at {op}")

    def execute(self, sql: str, params: Any = ()) -> None:
        self.sql = sql
        self.params = tuple(params or ())
        self._one, self._many, self.rowcount = None, [], -1
        op = self._op(sql)
        self._guard(op)
        if op in ("BEGIN", "COMMIT", "ROLLBACK"):
            self._conn._txn(op)
            return
        if self._conn._txn_open:
            self._conn._txn_has_query = True
        self._dispatch(op)

    def _dispatch(self, op: str) -> None:
        conn, p = self._conn, self.params
        s = _norm(self.sql)
        if op == "read_memory":
            row = conn.memories.get(p[0])
            if row is not None:
                self._one = tuple(row.get(c) for c in MEMORY_COLS)
        elif op == "read_relation_edge":
            if "CORRECTION_ID = %S" in s:
                row = None
                for cand in conn.edges.values():
                    if cand["correction_id"] == p[0]:
                        row = cand
                        break
            elif "FROM_MEMORY_ID = %S" in s:
                row = conn.edges.get(("from", p[0]))
            elif "TO_MEMORY_ID = %S" in s:
                row = conn.edges.get(("to", p[0]))
            else:
                row = None
            if row is not None:
                self._one = (
                    row["relation_id"], row["relation_type"],
                    row["from_memory_id"], row["to_memory_id"],
                )
        elif op == "read_relation_full":
            row = None
            if "FROM_MEMORY_ID = %S" in s:
                row = conn.edges.get(("from", p[0]))
            elif "TO_MEMORY_ID = %S" in s:
                row = conn.edges.get(("to", p[0]))
            if row is not None:
                self._one = tuple(row.get(c) for c in RELATION_COLS)
        elif op == "read_invalidations":
            rows = list(conn.invalidations.values())
            if "INVALIDATION_ID = %S" in s and "CORRECTION_ID = %S" in s:
                rows = [r for r in rows
                        if r["invalidation_id"] == p[0] and r["correction_id"] == p[1]]
            elif "INVALIDATION_ID = %S" in s:
                rows = [r for r in rows if r["invalidation_id"] == p[0]]
            elif "CORRECTION_ID = %S" in s:
                rows = [r for r in rows if r["correction_id"] == p[0]]
            rows.sort(key=lambda r: (r["derived_kind"], r["derived_id"]))
            if "SELECT DERIVED_ID FROM" in s:
                # invalidated_ids projects only the derived id
                self._many = [(r["derived_id"],) for r in rows]
                return
            self._many = [tuple(r.get(c) for c in SIDECAR_COLS) for r in rows]
        elif op == "read_topics":
            row = conn.topics.get(p[0])
            if row is not None:
                self._one = tuple(row.get(c) for c in TOPIC_COLS)
        elif op == "insert_topic":
            tid = p[0]
            if tid in conn.topics:
                self._one = None
                self.rowcount = 0
                return
            conn._record_undo("topics", tid)
            conn.topics[tid] = {
                "topic_id": tid, "title": p[1], "summary": p[2], "body": p[3],
                "keywords": list(p[4] or []), "note_ref": p[5],
                "status": "active", "created_at": _T0, "updated_at": _T0,
            }
            self._one = (tid,)
            self.rowcount = 1
        elif op == "update_invalidation":
            iid = p[2]
            row = conn.invalidations.get(iid)
            if row is None:
                self.rowcount = 0
                return
            conn._record_undo("invalidations", iid)
            row["state"] = p[0]
            row["resolved_at"] = _T1
            row["replacement_derived_id"] = p[1]
            self.rowcount = 1
        else:  # pragma: no cover
            raise AssertionError(op)

    def fetchone(self) -> Optional[tuple]:
        return self._one

    def fetchall(self) -> list:
        return list(self._many)

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_a: Any) -> bool:
        return False


class _FakeConn:
    def __init__(self, db: "_FakeDb") -> None:
        self._db = db
        self._cursor = _FakeCursor(self)
        self._snapshot: Optional[dict] = None
        self._txn_open = False
        self._txn_has_query = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    @property
    def autocommit(self) -> bool:
        return self._db.autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        self._db.autocommit = bool(value)

    def get_transaction_status(self) -> int:
        return 0 if not self._txn_open else 2

    @property
    def statements(self) -> list:
        return self._db.statements

    def _table(self, kind: str) -> dict:
        if kind == "topics":
            return self._db.topics
        if kind == "invalidations":
            return self._db.invalidations
        raise AssertionError(kind)  # pragma: no cover

    def _record_undo(self, kind: str, key: Any) -> None:
        if self._snapshot is None:
            return
        store = self._snapshot.setdefault(kind, {})
        if key in store:
            return
        table = self._table(kind)
        cur = table.get(key, _MISSING)
        store[key] = dict(cur) if isinstance(cur, dict) else cur

    def _txn(self, op: str) -> None:
        if op == "BEGIN":
            if (
                "ISOLATION LEVEL" in _norm(self._cursor.sql)
                and self._txn_open
                and self._txn_has_query
            ):
                raise RuntimeError(
                    "SET TRANSACTION ISOLATION LEVEL must be called "
                    "before any query"
                )
            self._txn_open = True
            self._txn_has_query = False
            self._snapshot = {"topics": {}, "invalidations": {}}
        elif op == "COMMIT":
            self._db.commits += 1
            self._snapshot = None
            self._txn_open = False
            self._txn_has_query = False
        elif op == "ROLLBACK":
            self._db.rollbacks += 1
            if self._snapshot is not None:
                for kind, store in self._snapshot.items():
                    table = self._table(kind)
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
            return
        self._db.statements.append(("COMMIT", "COMMIT"))
        self._txn("COMMIT")

    def rollback(self) -> None:
        if self.autocommit:
            return
        self._db.statements.append(("ROLLBACK", "ROLLBACK"))
        self._txn("ROLLBACK")

    @property
    def memories(self) -> dict:
        return self._db.memories

    @property
    def edges(self) -> dict:
        return self._db.edges

    @property
    def topics(self) -> dict:
        return self._db.topics

    @property
    def invalidations(self) -> dict:
        return self._db.invalidations

    @property
    def missing_invalidations(self) -> bool:
        return self._db.missing_invalidations

    @property
    def fail_on(self) -> set:
        return self._db.fail_on


class _FakeLease:
    def __init__(self, conn: _FakeConn) -> None:
        self.connection = conn

    def close(self) -> None:
        pass

    def __enter__(self) -> _FakeConn:
        return self.connection

    def __exit__(self, *_a: Any) -> bool:
        self.close()
        return False


class _FakeDb:
    def __init__(self, *, missing_invalidations: bool = False,
                 autocommit: bool = True) -> None:
        self.memories: dict = {}
        self.edges: dict = {}
        self.streams: dict = {}
        self.qa: dict = {}
        self.topics: dict = {}
        self.invalidations: dict = {}
        self.missing_invalidations = missing_invalidations
        self.autocommit = autocommit
        self.fail_on: set = set()
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0
        self.conn = _FakeConn(self)

    def lease(self, timeout: Optional[float] = None) -> _FakeLease:
        return _FakeLease(self.conn)

    def ops(self) -> list:
        return [op for op, _ in self.statements]


class _Seed:
    """Seed raw + derived tables without going through a writer."""

    def __init__(self, db: _FakeDb) -> None:
        self.db = db

    def memory(self, mid: str, content: str, *, status: str = "active",
               provenance: Optional[dict] = None, title: str = "m",
               category: str = "ops") -> str:
        self.db.memories[mid] = {
            "memory_id": mid, "category": category, "title": title,
            "content": content, "tags": ["x"], "provenance": dict(provenance or {}),
            "status": status, "created_at": _T0, "updated_at": _T0,
            "embedding": None, "embed_model": None,
        }
        return mid

    def edge(self, from_id: str, to_id: Optional[str], *, correction_id: str,
             relation_id: str, relation_type: str) -> None:
        row = {
            "relation_id": relation_id, "relation_type": relation_type,
            "from_memory_id": from_id, "to_memory_id": to_id,
            "correction_id": correction_id, "authority": "user_explicit",
            "correction_reason": None, "correction_source_id": None,
            "provenance": "explicit_tool_request", "effective_at": None,
            "recorded_at": _T1, "request": {},
        }
        self.db.edges[("from", from_id)] = row
        if to_id is not None:
            self.db.edges[("to", to_id)] = row

    def stream(self, sid: int, role: str, host: str, session_id: str,
               event_id: str) -> None:
        self.db.streams[sid] = {
            "id": sid, "role": role, "host": host, "session_id": session_id,
            "event_id": event_id, "content": f"raw stream {sid}",
        }

    def qa(self, qid: int, source_id: str) -> None:
        self.db.qa[qid] = {"id": qid, "source_id": source_id,
                           "answer": f"raw qa {qid}"}

    def topic(self, topic_id: str, title: str, body: str) -> None:
        self.db.topics[topic_id] = {
            "topic_id": topic_id, "title": title, "summary": "",
            "body": body, "keywords": [], "note_ref": None,
            "status": "active", "created_at": _T0, "updated_at": _T0,
        }

    def invalidation(self, *, invalidation_id: str, correction_id: str,
                     derived_kind: str, derived_id: str,
                     source_memory_id: str,
                     replacement_memory_id: Optional[str],
                     state: str = "pending_rebuild",
                     relation_id: Optional[str] = RELATION_ID,
                     canonical_qa_id: Optional[int] = CANONICAL_QA_ID,
                     input_source_kind: Optional[str] = "qa_pairs",
                     input_source_id: Optional[str] = str(CANONICAL_QA_ID),
                     replacement_derived_id: Optional[str] = None) -> None:
        self.db.invalidations[invalidation_id] = {
            "invalidation_id": invalidation_id, "correction_id": correction_id,
            "relation_id": relation_id, "derived_kind": derived_kind,
            "derived_id": derived_id, "source_memory_id": source_memory_id,
            "replacement_memory_id": replacement_memory_id, "state": state,
            "canonical_qa_id": canonical_qa_id,
            "input_source_kind": input_source_kind,
            "input_source_id": input_source_id, "created_at": _T0,
            "resolved_at": None, "replacement_derived_id": replacement_derived_id,
        }


class _StubProvider:
    """Deterministic provider stub: captures the context it was handed."""

    def __init__(self, out: Any = None, exc: Optional[BaseException] = None) -> None:
        self.calls: list = []
        self.out = out if out is not None else {
            "title": "Rebuilt deploy topic",
            "summary": "Deploy window is Monday.",
            "body": "Rebuilt body: the deploy window is Monday (per the current memory).",
            "keywords": ["deploy", "monday"],
        }
        self.exc = exc

    def __call__(self, context: dict) -> Any:
        self.calls.append(context)
        if self.exc is not None:
            raise self.exc
        return self.out


# ── helpers ────────────────────────────────────────────────────────────────

def _mod():
    import importlib

    try:
        return importlib.import_module(MODULE)
    except Exception as exc:  # pragma: no cover - RED surface
        pytest.fail(f"cannot import {MODULE!r}: {exc!r}")


def _sidecar_id(correction_id: str, kind: str, derived_id: str) -> str:
    from v3core.derived_invalidation import invalidation_id

    return invalidation_id(correction_id, kind, derived_id)


def _ab_db(*, with_topic: bool = True, raw_rows: bool = True) -> tuple:
    """A committed A -> B correction with one stale Topic T."""
    db = _FakeDb()
    seed = _Seed(db)
    seed.memory(SOURCE_MEMORY, A_CONTENT, status="archived",
                provenance={"source_qa_id": str(CANONICAL_QA_ID)}, title="A title")
    seed.memory(REPLACEMENT_MEMORY, B_CONTENT, status="active", title="B title")
    seed.edge(SOURCE_MEMORY, REPLACEMENT_MEMORY, correction_id=CORRECTION_ID,
              relation_id=RELATION_ID, relation_type="supersedes")
    if with_topic:
        seed.topic(OLD_TOPIC, "Old deploy topic", OLD_TOPIC_BODY)
    if raw_rows:
        seed.stream(11, "user", "h1", "s1", "msg-1")
        seed.qa(CANONICAL_QA_ID, "qa_sync/h1/s1/msg-1")
    seed.invalidation(
        invalidation_id=_sidecar_id(CORRECTION_ID, "topic", OLD_TOPIC),
        correction_id=CORRECTION_ID, derived_kind="topic", derived_id=OLD_TOPIC,
        source_memory_id=SOURCE_MEMORY,
        replacement_memory_id=REPLACEMENT_MEMORY,
    )
    return db, seed


def _rebuild(db, **kw):
    return _mod().rebuild_topic(db, **kw)


def _raw_snapshot(db: _FakeDb) -> dict:
    return {
        "memories": deepcopy(db.memories),
        "streams": deepcopy(db.streams),
        "qa": deepcopy(db.qa),
    }


# ─────────────────────────── public API surface ───────────────────────────

def test_M03_rebuild_public_api_is_locked():
    mod = _mod()
    for name in (
        "REBUILD_APPLIED", "REBUILD_DEDUPLICATED", "REBUILD_NOT_SUPPORTED",
        "REBUILD_PROVIDER_UNAVAILABLE", "REBUILD_FAILED",
        "REBUILD_NO_REPLACEMENT", "REBUILD_NO_INVALIDATION",
        "REBUILD_MIGRATION_REQUIRED", "REBUILD_INVALID_REQUEST",
        "REBUILD_STATE_APPLIED", "REBUILD_STATE_DEDUPLICATED",
        "REBUILD_STATE_PENDING", "REBUILD_STATE_NOT_SUPPORTED",
        "REBUILDABLE_KINDS",
    ):
        assert hasattr(mod, name), f"missing locked constant {name}"
    assert mod.REBUILD_MIGRATION_REQUIRED == "MIGRATION_REQUIRED"
    assert mod.REBUILDABLE_KINDS == ("topic",)

    sig = inspect.signature(mod.rebuild_topic)
    assert list(sig.parameters) == [
        "pg", "correction_id", "invalidation_id", "dry_run", "provider",
    ]
    for name in ("correction_id", "invalidation_id", "dry_run", "provider"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["correction_id"].default is None
    assert sig.parameters["invalidation_id"].default is None
    assert sig.parameters["dry_run"].default is False
    assert sig.parameters["provider"].default is None


def test_M03_rebuild_id_rule_is_deterministic_and_recomputable():
    mod = _mod()
    expected = "t_" + hashlib.md5(
        f"{OLD_TOPIC}|rebuild|{CORRECTION_ID}".encode("utf-8")
    ).hexdigest()[:8]
    assert mod.rebuilt_topic_id(OLD_TOPIC, CORRECTION_ID) == expected
    assert mod.rebuilt_topic_id(OLD_TOPIC, CORRECTION_ID) == \
        mod.rebuilt_topic_id(OLD_TOPIC, CORRECTION_ID)
    # never collides with T, and a different correction is a different artifact
    assert mod.rebuilt_topic_id(OLD_TOPIC, CORRECTION_ID) != OLD_TOPIC
    assert mod.rebuilt_topic_id(OLD_TOPIC, "cor_other") != expected
    assert mod.rebuilt_topic_id("t_other", CORRECTION_ID) != expected


# ─────────────────────────── the happy path ───────────────────────────────

def test_M03_rebuild_generates_T2_and_advances_the_sidecar():
    db, _seed = _ab_db()
    provider = _StubProvider()
    before_topic = deepcopy(db.topics[OLD_TOPIC])

    receipt = _rebuild(db, correction_id=CORRECTION_ID, provider=provider)

    t2 = _mod().rebuilt_topic_id(OLD_TOPIC, CORRECTION_ID)
    assert receipt["status"] == "REBUILD_APPLIED", receipt
    assert receipt["success"] is True
    assert receipt["rebuild_state"] == "rebuilt"
    assert receipt["derived_kind"] == "topic"
    assert receipt["derived_id"] == OLD_TOPIC
    assert receipt["source_memory_id"] == SOURCE_MEMORY
    assert receipt["replacement_memory_id"] == REPLACEMENT_MEMORY
    assert receipt["new_derived_id"] == t2
    assert receipt["counts"]["rebuilt"] == 1
    assert receipt["counts"]["requested"] == 1
    assert receipt["dry_run"] is False

    # T2 is a NEW topics row, distinct from T
    assert t2 in db.topics, "a successful rebuild must produce the new artifact"
    assert t2 != OLD_TOPIC
    assert len(db.topics) == 2
    new_row = db.topics[t2]
    assert new_row["status"] == "active"
    assert "Monday" in new_row["body"]
    assert new_row["note_ref"] == f"rebuild://{CORRECTION_ID}"

    # T is NOT rewritten: its body stays byte-for-byte the superseded one
    assert db.topics[OLD_TOPIC] == before_topic, (
        "the old artifact keeps its body verbatim — rebuild is additive"
    )

    # the sidecar row advanced truthfully
    sidecar = db.invalidations[_sidecar_id(CORRECTION_ID, "topic", OLD_TOPIC)]
    assert sidecar["state"] == "rebuilt"
    assert sidecar["replacement_derived_id"] == t2
    assert sidecar["resolved_at"] is not None
    assert sidecar["replacement_memory_id"] == REPLACEMENT_MEMORY
    assert sidecar["source_memory_id"] == SOURCE_MEMORY

    # §31: T stays suppressed (row existence), T2 was never suppressed
    from v3core.derived_invalidation import invalidated_ids

    suppressed = invalidated_ids(db, "topic")
    assert OLD_TOPIC in suppressed
    assert t2 not in suppressed


def test_M03_rebuild_context_exposes_A_historical_and_B_current():
    db, _seed = _ab_db()
    provider = _StubProvider()

    receipt = _rebuild(db, correction_id=CORRECTION_ID, provider=provider)

    assert len(provider.calls) == 1, "the provider must have been called once"
    ctx = provider.calls[0]

    # A is historical/superseded, with A's real payload
    assert ctx["source_memory"]["memory_id"] == SOURCE_MEMORY
    assert ctx["source_memory"]["state"] == "historical/superseded"
    assert ctx["source_memory"]["content"] == A_CONTENT
    assert ctx["source_memory"]["label"] == "superseded"

    # B is current, with B's real payload
    assert ctx["replacement_memory"]["memory_id"] == REPLACEMENT_MEMORY
    assert ctx["replacement_memory"]["state"] == "current"
    assert ctx["replacement_memory"]["content"] == B_CONTENT
    assert ctx["replacement_memory"]["label"] == "current"

    # the correction relation is explicit
    rel = ctx["correction"]
    assert rel["correction_id"] == CORRECTION_ID
    assert rel["relation_id"] == RELATION_ID
    assert rel["relation_type"] == "supersedes"
    assert rel["from_memory_id"] == SOURCE_MEMORY
    assert rel["to_memory_id"] == REPLACEMENT_MEMORY
    assert ctx["correction_relation"] == rel

    # the old topic is labelled stale in the context too
    assert ctx["old_topic"]["state"] == "historical/superseded"
    assert ctx["old_topic"]["body"] == OLD_TOPIC_BODY

    # original source(s) survive
    assert ctx["original_sources"]["canonical_qa_id"] == CANONICAL_QA_ID

    # the context is echoed verbatim in the receipt
    assert receipt["generation_context"] == ctx
    # and the current payload is present, never only the old raw A
    rendered = json.dumps(ctx, ensure_ascii=False)
    assert B_CONTENT in rendered
    assert A_CONTENT in rendered


def test_M03_rebuild_lineage_is_persisted_on_T2_and_in_the_receipt():
    db, _seed = _ab_db()
    receipt = _rebuild(db, correction_id=CORRECTION_ID, provider=_StubProvider())

    t2 = receipt["new_derived_id"]
    lineage = receipt["lineage"]
    assert lineage["rebuilt_derived_id"] == t2
    assert lineage["derived_id"] == OLD_TOPIC
    assert lineage["rebuild_from_correction"] == CORRECTION_ID
    assert lineage["source_memory_id"] == SOURCE_MEMORY
    assert lineage["replacement_memory_id"] == REPLACEMENT_MEMORY
    assert lineage["canonical_qa_id"] == CANONICAL_QA_ID
    chain = lineage["chain"]
    assert chain[0] == t2
    assert "rebuild_from_correction" in chain
    assert CORRECTION_ID in chain
    assert SOURCE_MEMORY in chain
    assert REPLACEMENT_MEMORY in chain
    assert any(str(CANONICAL_QA_ID) in str(x) for x in chain)

    # persisted on T2 itself: machine-readable marker + cross-ref note_ref
    body = db.topics[t2]["body"]
    assert "<!-- m03-rebuild-lineage: " in body
    payload = body.split("<!-- m03-rebuild-lineage: ", 1)[1].rsplit(" -->", 1)[0]
    stored = json.loads(payload)
    assert stored["rebuild_from_correction"] == CORRECTION_ID
    assert stored["rebuilt_derived_id"] == t2
    assert stored["source_memory_id"] == SOURCE_MEMORY
    assert stored["replacement_memory_id"] == REPLACEMENT_MEMORY
    assert db.topics[t2]["note_ref"] == f"rebuild://{CORRECTION_ID}"


def test_M03_rebuild_by_invalidation_id_targets_one_row():
    db, _seed = _ab_db()
    iid = _sidecar_id(CORRECTION_ID, "topic", OLD_TOPIC)
    receipt = _rebuild(db, invalidation_id=iid, provider=_StubProvider())
    assert receipt["status"] == "REBUILD_APPLIED", receipt
    assert receipt["invalidation_id"] == iid
    assert receipt["correction_id"] == CORRECTION_ID


# ─────────────────────────── provider failures ────────────────────────────

def test_M03_rebuild_provider_error_keeps_sidecar_and_invents_nothing():
    db, _seed = _ab_db()
    provider = _StubProvider(exc=RuntimeError("provider exploded"))
    raw_before = _raw_snapshot(db)
    sidecar_before = deepcopy(
        db.invalidations[_sidecar_id(CORRECTION_ID, "topic", OLD_TOPIC)]
    )
    topics_before = deepcopy(db.topics)

    receipt = _rebuild(db, correction_id=CORRECTION_ID, provider=provider)

    assert receipt["status"] == "REBUILD_FAILED", receipt
    assert receipt["success"] is False
    assert receipt["new_derived_id"] is None
    assert receipt["counts"]["pending"] == 1
    assert receipt["counts"]["rebuilt"] == 0
    # no artifact, no invented text, no write at all
    assert db.topics == topics_before
    assert "insert_topic" not in db.ops()
    assert "update_invalidation" not in db.ops()
    # the sidecar row is EXACTLY as it was
    assert db.invalidations[_sidecar_id(CORRECTION_ID, "topic", OLD_TOPIC)] == \
        sidecar_before
    # T stays suppressed
    from v3core.derived_invalidation import invalidated_ids

    assert OLD_TOPIC in invalidated_ids(db, "topic")
    # the correction itself is untouched and never rolled back
    assert db.edges[("from", SOURCE_MEMORY)]["to_memory_id"] == REPLACEMENT_MEMORY
    assert db.memories[SOURCE_MEMORY]["status"] == "archived"
    assert db.memories[REPLACEMENT_MEMORY]["status"] == "active"
    assert _raw_snapshot(db) == raw_before


def test_M03_rebuild_empty_provider_output_is_a_truthful_failure():
    db, _seed = _ab_db()
    provider = _StubProvider(out="   ")
    receipt = _rebuild(db, correction_id=CORRECTION_ID, provider=provider)
    assert receipt["status"] == "REBUILD_FAILED", receipt
    assert receipt["success"] is False
    assert "insert_topic" not in db.ops()
    assert len(db.topics) == 1


def test_M03_rebuild_default_provider_unavailable_when_unconfigured(monkeypatch):
    db, _seed = _ab_db()

    import v3core.config as _config

    def _boom():
        raise RuntimeError("TEST mode refuses to resolve a profile")

    monkeypatch.setattr(_config, "resolve_config", _boom)

    sidecar_before = deepcopy(
        db.invalidations[_sidecar_id(CORRECTION_ID, "topic", OLD_TOPIC)]
    )
    receipt = _rebuild(db, correction_id=CORRECTION_ID)  # provider=None

    assert receipt["status"] == "REBUILD_PROVIDER_UNAVAILABLE", receipt
    assert receipt["success"] is False
    assert receipt["rebuild_state"] == "pending"
    assert receipt["new_derived_id"] is None
    assert db.invalidations[_sidecar_id(CORRECTION_ID, "topic", OLD_TOPIC)] == \
        sidecar_before
    assert len(db.topics) == 1
    assert "insert_topic" not in db.ops()


# ─────────────────────────── idempotency / dry-run ────────────────────────

def test_M03_rebuild_is_idempotent_and_reports_deduplicated():
    db, _seed = _ab_db()
    first = _rebuild(db, correction_id=CORRECTION_ID, provider=_StubProvider())
    assert first["status"] == "REBUILD_APPLIED"
    t2 = first["new_derived_id"]
    topics_after_first = len(db.topics)
    inserts_after_first = db.ops().count("insert_topic")

    second = _rebuild(db, correction_id=CORRECTION_ID, provider=_StubProvider())

    assert second["status"] == "REBUILD_DEDUPLICATED", second
    assert second["success"] is True
    assert second["new_derived_id"] == t2, "the SAME T2 must come back"
    assert second["counts"]["deduplicated"] == 1
    assert second["counts"]["rebuilt"] == 0
    assert len(db.topics) == topics_after_first, "no new artifact on replay"
    assert db.ops().count("insert_topic") == inserts_after_first, (
        "a replay must not attempt another artifact write"
    )


def test_M03_rebuild_dry_run_writes_nothing():
    db, _seed = _ab_db()
    provider = _StubProvider()
    raw_before = _raw_snapshot(db)
    sidecar_before = deepcopy(
        db.invalidations[_sidecar_id(CORRECTION_ID, "topic", OLD_TOPIC)]
    )
    topics_before = deepcopy(db.topics)

    receipt = _rebuild(db, correction_id=CORRECTION_ID, dry_run=True,
                       provider=provider)

    assert receipt["dry_run"] is True
    assert receipt["would_rebuild"] is True
    assert receipt["rebuild_state"] == "preview"
    assert receipt["new_derived_id"] == _mod().rebuilt_topic_id(
        OLD_TOPIC, CORRECTION_ID
    )
    assert db.topics == topics_before
    assert db.invalidations[_sidecar_id(CORRECTION_ID, "topic", OLD_TOPIC)] == \
        sidecar_before
    assert db.ops().count("insert_topic") == 0
    assert db.ops().count("update_invalidation") == 0
    assert provider.calls == [], "dry-run must not call the provider"
    assert _raw_snapshot(db) == raw_before


# ─────────────────────────── unsupported kinds ────────────────────────────

@pytest.mark.parametrize("kind", ["observer_note", "yin_paragraph"])
def test_M03_rebuild_observer_and_yin_are_truthfully_not_supported(kind):
    db = _FakeDb()
    seed = _Seed(db)
    seed.memory(SOURCE_MEMORY, A_CONTENT, status="archived")
    seed.memory(REPLACEMENT_MEMORY, B_CONTENT, status="active")
    seed.edge(SOURCE_MEMORY, REPLACEMENT_MEMORY, correction_id=CORRECTION_ID,
              relation_id=RELATION_ID, relation_type="supersedes")
    seed.invalidation(
        invalidation_id=_sidecar_id(CORRECTION_ID, kind, "7"),
        correction_id=CORRECTION_ID, derived_kind=kind, derived_id="7",
        source_memory_id=SOURCE_MEMORY,
        replacement_memory_id=REPLACEMENT_MEMORY,
    )
    provider = _StubProvider()

    receipt = _rebuild(db, correction_id=CORRECTION_ID, provider=provider)

    assert receipt["status"] == "REBUILD_NOT_SUPPORTED", receipt
    assert receipt["success"] is False
    assert receipt["rebuild_state"] == "not_supported"
    assert receipt["counts"]["unsupported"] == 1
    assert receipt["new_derived_id"] is None
    assert db.topics == {}
    assert db.ops().count("insert_topic") == 0
    assert db.ops().count("update_invalidation") == 0
    assert provider.calls == [], (
        "an unsupported derived kind must not reach the provider at all"
    )
    # the sidecar row is untouched — the artifact stays invalidated
    row = db.invalidations[_sidecar_id(CORRECTION_ID, kind, "7")]
    assert row["state"] == "pending_rebuild"
    assert row["replacement_derived_id"] is None


def test_M03_rebuild_mixed_correction_rebuilds_topic_and_reports_the_rest():
    """A correction with a topic AND an observer note: the topic is rebuilt,
    the note is truthfully reported — never silently dropped, never fabricated.
    """
    db, _seed = _ab_db()
    seed = _Seed(db)
    seed.invalidation(
        invalidation_id=_sidecar_id(CORRECTION_ID, "observer_note", "7"),
        correction_id=CORRECTION_ID, derived_kind="observer_note", derived_id="7",
        source_memory_id=SOURCE_MEMORY,
        replacement_memory_id=REPLACEMENT_MEMORY,
    )
    provider = _StubProvider()

    receipt = _rebuild(db, correction_id=CORRECTION_ID, provider=provider)

    assert receipt["counts"]["requested"] == 2
    assert receipt["counts"]["rebuilt"] == 1
    assert receipt["counts"]["unsupported"] == 1
    # headline fields describe the rebuilt topic
    assert receipt["derived_kind"] == "topic"
    assert receipt["derived_id"] == OLD_TOPIC
    assert receipt["status"] == "REBUILD_APPLIED"
    assert receipt["new_derived_id"] == _mod().rebuilt_topic_id(
        OLD_TOPIC, CORRECTION_ID
    )
    kinds = {r["derived_kind"]: r["status"] for r in receipt["results"]}
    assert kinds["topic"] == "REBUILD_APPLIED"
    assert kinds["observer_note"] == "REBUILD_NOT_SUPPORTED"
    # the observer sidecar row is untouched (still suppressed)
    note_row = db.invalidations[_sidecar_id(CORRECTION_ID, "observer_note", "7")]
    assert note_row["state"] == "pending_rebuild"
    assert note_row["replacement_derived_id"] is None
    # the provider was called exactly once — for the topic only
    assert len(provider.calls) == 1


# ─────────────────────────── raw rows / boundaries ────────────────────────

def test_M03_rebuild_never_modifies_raw_rows():
    db, _seed = _ab_db()
    raw_before = _raw_snapshot(db)

    _rebuild(db, correction_id=CORRECTION_ID, provider=_StubProvider())

    assert _raw_snapshot(db) == raw_before, (
        "conversation_stream / qa_pairs / explicit_memories must be untouched"
    )


def test_M03_rebuild_missing_sidecar_table_is_migration_required():
    db, _seed = _ab_db()
    db.missing_invalidations = True
    receipt = _rebuild(db, correction_id=CORRECTION_ID, provider=_StubProvider())
    assert receipt["status"] == "MIGRATION_REQUIRED", receipt
    assert receipt["success"] is False
    assert "derived_memory_invalidations" in (receipt["error"] or "")
    # nothing was written: only the pre-existing T remains, no T2
    assert list(db.topics) == [OLD_TOPIC]
    assert _mod().rebuilt_topic_id(OLD_TOPIC, CORRECTION_ID) not in db.topics


def test_M03_rebuild_unknown_correction_reports_no_invalidation():
    db, _seed = _ab_db()
    receipt = _rebuild(db, correction_id="cor_missing", provider=_StubProvider())
    assert receipt["status"] == "REBUILD_NO_INVALIDATION", receipt
    assert receipt["success"] is True
    assert receipt["new_derived_id"] is None
    assert "insert_topic" not in db.ops()


def test_M03_rebuild_requires_an_identifier():
    db, _seed = _ab_db()
    receipt = _rebuild(db, provider=_StubProvider())
    assert receipt["status"] == "INVALID_REQUEST", receipt
    assert receipt["success"] is False
    assert db.ops() == []


def test_M03_rebuild_withdrawal_has_no_replacement_and_invents_nothing():
    db = _FakeDb()
    seed = _Seed(db)
    seed.memory(SOURCE_MEMORY, A_CONTENT, status="archived")
    seed.edge(SOURCE_MEMORY, None, correction_id="cor_wd",
              relation_id="rel_wd", relation_type="withdraws")
    seed.topic(OLD_TOPIC, "Old deploy topic", OLD_TOPIC_BODY)
    seed.invalidation(
        invalidation_id=_sidecar_id("cor_wd", "topic", OLD_TOPIC),
        correction_id="cor_wd", derived_kind="topic", derived_id=OLD_TOPIC,
        source_memory_id=SOURCE_MEMORY, replacement_memory_id=None, state="stale",
    )
    provider = _StubProvider()

    receipt = _rebuild(db, correction_id="cor_wd", provider=provider)

    assert receipt["status"] == "REBUILD_NO_REPLACEMENT", receipt
    assert receipt["success"] is False
    assert receipt["new_derived_id"] is None
    assert list(db.topics) == [OLD_TOPIC], "no artifact may be invented"
    assert db.topics[OLD_TOPIC]["body"] == OLD_TOPIC_BODY
    assert provider.calls == [], "no replacement means no generation input"


# ─────────────────────────── source-level guarantees ──────────────────────

def _executable_source_text(path: Path) -> str:
    """Code-only text: docstrings are excluded, real string literals kept."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    chunks: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                chunks.append(node.value)
        elif isinstance(node, ast.Name):
            chunks.append(node.id)
        elif isinstance(node, ast.Attribute):
            chunks.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            chunks.append(node.name)
        elif isinstance(node, ast.arg):
            chunks.append(node.arg)
    return "\n".join(chunks)


def test_M03_rebuild_module_never_deletes_or_writes_raw_tables():
    code = _executable_source_text(V3CORE_SRC / "derived_rebuild.py").upper()

    assert "DELETE" not in code, "the rebuild stage must stay additive"
    assert "TRUNCATE" not in code
    assert "DROP " not in code

    for raw_table in (
        "PUBLIC.CONVERSATION_STREAM", "PUBLIC.QA_PAIRS",
        "PUBLIC.EXPLICIT_MEMORIES", "PUBLIC.OBSERVATION_NOTES",
        "PUBLIC.YIN_PARAGRAPHS", "PUBLIC.MEMORY_RELATIONS",
    ):
        assert f"INSERT INTO {raw_table}" not in code, raw_table
        assert f"UPDATE {raw_table}" not in code, raw_table

    # exactly ONE INSERT (the new artifact) and ONE UPDATE (the sidecar).
    # The SQL lives in f-strings, so the AST extractor yields the literal
    # fragments and the interpolated names separately.
    assert code.count("INSERT INTO") == 1
    assert "INSERT INTO " in code
    assert "_TOPICS_TABLE" in code
    assert code.count("UPDATE ") == 1
    assert "UPDATE " in code
    assert "_INVALIDATION_TABLE" in code
    # the only INSERT/UPDATE targets are those two tables
    assert "PUBLIC.TOPICS" in code or "public.topics" in code


def test_M03_rebuild_module_does_not_hardcode_provider_endpoint_or_credential():
    text = (V3CORE_SRC / "derived_rebuild.py").read_text(encoding="utf-8")
    for forbidden in ("http://", "https://", "api.minimaxi", "Bearer",
                      "api_key=", "MINIMAX_API_KEY", "psycopg2.connect"):
        assert forbidden not in text, (
            f"no hardcoded endpoint/credential may live in the rebuild module "
            f"({forbidden!r})"
        )
    # the default provider goes through the project's configured LLM path
    assert "resolve_config" in text
    assert "LLMClient" in text


# ─────────────────────────── tool layer ───────────────────────────────────

class _FakeCore:
    """A booted-core stub for ``resolve_scope`` (pool + config, no pg)."""

    def __init__(self, pool: Any, config: Any = None) -> None:
        self.pg_pool = pool
        self.config = {} if config is None else config
        self._pg = None


def _tool():
    import importlib

    return importlib.import_module(TOOL_MODULE)


def test_M03_rebuild_tool_requires_an_identifier():
    payload = json.loads(_tool().handle_v3_rebuild({"correction_id": ""}))
    assert payload["success"] is False
    assert payload["error_code"] == "INVALID_REQUEST"
    assert payload["action"] == "rebuild"


def test_M03_rebuild_tool_fails_closed_on_a_declared_runtime_without_scope():
    payload = json.loads(
        _tool().handle_v3_rebuild(
            {"correction_id": CORRECTION_ID}, runtime_context=True
        )
    )
    assert payload["success"] is False
    assert payload["error_code"] == "SCOPE_UNAVAILABLE", payload


def test_M03_rebuild_tool_passes_the_receipt_through_verbatim():
    db, _seed = _ab_db()
    core = _FakeCore(db)
    provider = _StubProvider()

    payload = json.loads(
        _tool().handle_v3_rebuild(
            {"correction_id": CORRECTION_ID}, core=core,
            rebuild_provider=provider,
        )
    )

    assert payload["action"] == "rebuild"
    assert payload["status"] == "REBUILD_APPLIED", payload
    assert payload["new_derived_id"] == _mod().rebuilt_topic_id(
        OLD_TOPIC, CORRECTION_ID
    )
    assert payload["lineage"]["rebuild_from_correction"] == CORRECTION_ID
    assert db.topics[_mod().rebuilt_topic_id(OLD_TOPIC, CORRECTION_ID)]


def test_M03_v3_update_dispatches_the_rebuild_action():
    from v3core.tools.api_update import handle_v3_update

    db, _seed = _ab_db()
    core = _FakeCore(db)
    payload = json.loads(
        handle_v3_update(
            {"action": "rebuild", "correction_id": CORRECTION_ID},
            core=core, rebuild_provider=_StubProvider(),
        )
    )
    assert payload["success"] is True, payload
    assert payload["status"] == "REBUILD_APPLIED"
    assert payload["action"] == "rebuild"


def test_M03_v3_update_schema_exposes_the_rebuild_action_and_params():
    from v3core.tools.api_update import V3_UPDATE_SCHEMA

    props = V3_UPDATE_SCHEMA["parameters"]["properties"]
    assert "rebuild" in props["action"]["enum"]
    assert "invalidation_id" in props
    assert "rebuild_dry_run" in props
    assert props["rebuild_dry_run"]["default"] is False
