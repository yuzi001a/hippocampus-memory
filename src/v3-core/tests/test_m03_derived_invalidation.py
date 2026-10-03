# -*- coding: utf-8 -*-
"""M03 deterministic derived-memory invalidation — RED→GREEN contract tests.

Locked contract (docs/M03-CORRECTION-PROPAGATION.md,
docs/M03-DERIVED-LINEAGE-MAP.md, base main 23865f74366e9c39e6860f9d1658ece77f224e33):

  * ``v3core.derived_invalidation`` is a pure deterministic layer: no LLM, no
    embedding, no body-text / vector guessing, bounded work.
  * A correction ``A -> B`` invalidates only derived artifacts that are
    structurally linked to A's source ``qa_pairs.id``:
      - ``topic``         via ``topic_entries.source_qa_id``
      - ``observer_note`` via ``observation_notes.source_qa_range @> qa_id``
      - ``yin_paragraph`` never (no structural edge) — always empty, never guessed
  * Invalidation is a SIDECAR fact: no derived body is ever rewritten and
    nothing is ever DELETEd.
  * The sidecar write is idempotent: ``UNIQUE (correction_id, derived_kind,
    derived_id)`` plus the deterministic ``invalidation_id`` mean replaying one
    propagation inserts ZERO new rows.
  * A withdrawal (``to_memory_id IS NULL``) leaves ``replacement_memory_id``
    None and marks dependents ``stale`` — no replacement fact is invented.
  * ``A -> B -> C`` resolves the CURRENT successor through the canonical
    reader's chain walk (``ActiveMemoryReader.read_version`` / ``read_chain``),
    never a second traversal.

Frozen source-key contract (two entry points, ONE identity):
  * ``source_conversation_stream_id`` / ``source_qa_id`` are both accepted, but
    the canonical internal identity is always ``qa_pairs.id``.
  * exact candidates ``qa_sync/<host>/<session_id>/<event_id>`` and
    ``qa_import/<host>/<session_id>/<event_id>``; exactly one hit required.
  * codes: ``OK`` / ``NO_SOURCE`` / ``SOURCE_NOT_MAPPED`` / ``SOURCE_AMBIGUOUS``
    / ``SOURCE_IDENTITY_CONFLICT``.
  * the legacy live form needs ``q_turn`` which conversation_stream does not
    carry as a canonical truth → NOT MAPPED; a turn is NEVER inferred and the
    nearest QA is NEVER selected.

No real PostgreSQL: the P0-A conftest hard-blocks psycopg2.connect, so this
contract is proven against a deterministic in-memory fake that models leases,
transaction snapshots, RETURNING row counts, unique-key DO NOTHING, int8range
containment and the canonical correction-chain reads.
"""
from __future__ import annotations

import hashlib
import inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

MODULE = "v3core.derived_invalidation"

REPO_ROOT = Path(__file__).resolve().parents[3]
V3CORE_SRC = REPO_ROOT / "src" / "v3-core" / "src" / "v3core"

AUTHORITY = "user_explicit"
LEGACY_HOST = "legacy"

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
INVALIDATION_COLS = (
    "invalidation_id", "correction_id", "relation_id", "derived_kind",
    "derived_id", "source_memory_id", "replacement_memory_id", "state",
    "reason", "canonical_qa_id", "input_source_kind", "input_source_id",
    "created_at", "resolved_at", "replacement_derived_id",
)
INVALIDATION_INSERT_COLS = INVALIDATION_COLS[:12]


def _live_source_id(host: str, session: str, event: str) -> str:
    """Mirror of ``V3Core._qa_source_id`` host form (__init__.py:4053)."""
    return f"qa_sync/{host}/{session}/{event}"


def _import_source_id(host: str, session: str, event: str) -> str:
    """Mirror of the I01 import identity (qa_pairing.py:97)."""
    return f"qa_import/{host}/{session}/{event}"


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

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_a: Any) -> bool:
        return False

    @staticmethod
    def _op(sql: str) -> str:
        s = _norm(sql)
        if s in ("COMMIT", "ROLLBACK"):
            return s
        if s.startswith("BEGIN"):
            return "BEGIN"
        if "INSERT INTO PUBLIC.DERIVED_MEMORY_INVALIDATIONS" in s:
            return "insert_invalidation"
        if "FROM PUBLIC.DERIVED_MEMORY_INVALIDATIONS" in s:
            return "read_invalidations"
        if "FROM PUBLIC.CONVERSATION_STREAM" in s:
            return "read_stream"
        if "FROM PUBLIC.QA_PAIRS" in s:
            return "read_qa"
        if "FROM PUBLIC.TOPIC_ENTRIES" in s:
            return "read_topic_entries"
        if "FROM PUBLIC.OBSERVATION_NOTES" in s:
            return "read_observation"
        if "FROM PUBLIC.YIN_PARAGRAPHS" in s:
            return "read_yin"
        if "FROM PUBLIC.TOPICS" in s:
            return "read_topics"
        if "FROM PUBLIC.MEMORY_RELATIONS" in s:
            return "read_edge"
        if "FROM PUBLIC.EXPLICIT_MEMORIES" in s:
            return "read_memory"
        raise AssertionError(f"fake pg: unmapped SQL — {s[:160]}")

    def _guard(self, op: str) -> None:
        conn = self._conn
        conn.statements.append((op, _norm(self.sql)))
        if conn.missing_invalidations and op in {
            "insert_invalidation", "read_invalidations",
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
            if (
                op == "BEGIN"
                and "ISOLATION LEVEL" in _norm(sql)
                and self._conn._txn_open
                and self._conn._txn_has_query
            ):
                raise RuntimeError(
                    "SET TRANSACTION ISOLATION LEVEL must be called "
                    "before any query"
                )
            self._conn._txn(op)
            return
        if self._conn._txn_open:
            self._conn._txn_has_query = True
        elif not self._conn.autocommit:
            self._conn._txn_open = True
            self._conn._txn_has_query = True
        self._dispatch(op)

    def _dispatch(self, op: str) -> None:
        conn, p = self._conn, self.params
        s = _norm(self.sql)
        if op == "read_memory":
            row = conn.memories.get(p[0])
            if row is not None:
                self._one = tuple(row.get(c) for c in MEMORY_COLS)
        elif op == "read_edge":
            row = None
            if "WHERE CORRECTION_ID = %S" in s:
                for cand in conn.edges.values():
                    if cand["correction_id"] == p[0]:
                        row = cand
                        break
            elif "FROM_MEMORY_ID = %S" in s and "TO_MEMORY_ID = %S" not in s:
                row = conn.edges.get(("from", p[0]))
            elif "TO_MEMORY_ID = %S" in s:
                row = conn.edges.get(("to", p[0]))
            if row is not None:
                self._one = tuple(row.get(c) for c in RELATION_COLS)
        elif op == "read_stream":
            row = conn.streams.get(int(p[0]))
            if row is not None:
                # the module selects the real canonical identity only
                self._one = (
                    row["role"], row["host"], row["session_id"], row["event_id"],
                )
        elif op == "read_qa":
            if "SOURCE_ID = ANY(%S)" in s:
                cands = list(p[0]) if p and isinstance(p[0], (list, tuple, set)) else []
                hits = sorted({conn.qa_by_source[c] for c in cands
                               if c in conn.qa_by_source})
                self._many = [(h,) for h in hits]
            elif "SOURCE_ID = %S" in s:
                qid = conn.qa_by_source.get(p[0])
                if qid is not None:
                    self._one = (qid,)
            else:
                qid = int(p[0])
                if qid in conn.qa:
                    self._one = (1,)
        elif op == "read_topic_entries":
            qa_ids = set(p[0]) if p and isinstance(p[0], (list, tuple, set)) else set()
            if "DISTINCT TOPIC_ID" in s:
                found = sorted({e["topic_id"] for e in conn.topic_entries
                                if e["source_qa_id"] in qa_ids})
                self._many = [(t,) for t in found]
            else:
                found = sorted({e["source_qa_id"] for e in conn.topic_entries
                                if e["source_qa_id"] in qa_ids})
                self._many = [(q,) for q in found]
        elif op == "read_observation":
            if "SOURCE_QA_RANGE @>" in s:
                qa = int(p[0])
                self._many = [
                    (nid,) for nid, n in sorted(conn.observation_notes.items())
                    if n["lo"] <= qa < n["hi"]
                ]
            else:
                ids = [int(x) for x in (p[0] if p else [])]
                self._many = [
                    (i, conn.observation_notes[i]["content"])
                    for i in sorted(ids) if i in conn.observation_notes
                ]
        elif op == "read_yin":
            ids = [int(x) for x in (p[0] if p else [])]
            self._many = [(i, conn.yin[i]["content"]) for i in sorted(ids)
                          if i in conn.yin]
        elif op == "read_topics":
            ids = [str(x) for x in (p[0] if p else [])]
            self._many = [(t, conn.topics[t]["title"]) for t in sorted(ids)
                          if t in conn.topics]
        elif op == "insert_invalidation":
            vals = dict(zip(INVALIDATION_INSERT_COLS, p))
            iid = vals["invalidation_id"]
            if iid in conn.invalidations:
                # ON CONFLICT (invalidation_id) DO NOTHING → no row returned
                self._one = None
                self.rowcount = 0
                return
            conn._record_undo("invalidations", iid)
            row = dict(vals)
            row["created_at"] = _T0
            row["resolved_at"] = None
            row["replacement_derived_id"] = None
            conn.invalidations[iid] = row
            self._one = (iid,)
            self.rowcount = 1
        elif op == "read_invalidations":
            rows = list(conn.invalidations.values())
            if "CORRECTION_ID = %S" in s:
                rows = [r for r in rows if r["correction_id"] == p[0]]
            if "DERIVED_KIND = %S AND STATE <> %S" in s:
                rows = [r for r in rows
                        if r["derived_kind"] == p[0] and r["state"] != p[1]]
            elif "DERIVED_KIND = %S" in s:
                rows = [r for r in rows if r["derived_kind"] == p[0]]
            if "INVALIDATION_ID = ANY(%S)" in s:
                wanted = {str(x) for x in (p[0] if p else [])}
                self._many = [(r["invalidation_id"],) for r in rows
                              if r["invalidation_id"] in wanted]
                return
            if "SELECT DERIVED_ID FROM" in s:
                # invalidated_ids projects only the derived id
                self._many = [(r["derived_id"],) for r in rows]
                return
            rows.sort(key=lambda r: (r["derived_kind"], r["derived_id"]))
            self._many = [tuple(r.get(c) for c in INVALIDATION_COLS) for r in rows]
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

    def _table(self, kind: str) -> dict:
        if kind == "memories":
            return self._db.memories
        if kind == "edges":
            return self._db.edges
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
            self._txn_open = True
            self._txn_has_query = False
            self._snapshot = {"memories": {}, "edges": {}, "invalidations": {}}
        elif op == "COMMIT":
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
        self._db.commits += 1
        self._db.statements.append(("COMMIT", "COMMIT"))
        self._snapshot = None
        self._txn_open = False
        self._txn_has_query = False

    def rollback(self) -> None:
        if self.autocommit:
            return
        self._db.statements.append(("ROLLBACK", "ROLLBACK"))
        self._txn("ROLLBACK")

    @property
    def statements(self) -> list:
        return self._db.statements

    @property
    def memories(self) -> dict:
        return self._db.memories

    @property
    def edges(self) -> dict:
        return self._db.edges

    @property
    def streams(self) -> dict:
        return self._db.streams

    @property
    def qa(self) -> dict:
        return self._db.qa

    @property
    def qa_by_source(self) -> dict:
        return self._db.qa_by_source

    @property
    def topic_entries(self) -> list:
        return self._db.topic_entries

    @property
    def observation_notes(self) -> dict:
        return self._db.observation_notes

    @property
    def topics(self) -> dict:
        return self._db.topics

    @property
    def yin(self) -> dict:
        return self._db.yin

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
        self._closed = False

    def close(self) -> None:
        self._closed = True

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
        self.qa_by_source: dict = {}
        self.topic_entries: list = []
        self.observation_notes: dict = {}
        self.topics: dict = {}
        self.yin: dict = {}
        self.invalidations: dict = {}
        self.missing_invalidations = missing_invalidations
        self.autocommit = autocommit
        self.fail_on: set = set()
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0
        self.leases = 0
        self.conn = _FakeConn(self)

    def lease(self, timeout: Optional[float] = None) -> _FakeLease:
        self.leases += 1
        return _FakeLease(self.conn)

    def ops(self) -> list:
        return [op for op, _ in self.statements]


class _Seed:
    """Seed the raw + derived tables without going through a writer."""

    def __init__(self, db: _FakeDb) -> None:
        self.db = db

    def memory(self, mid: str, content: str, *, status: str = "active",
               provenance: Optional[dict] = None, category: str = "ops",
               title: str = "t", tags: tuple = ("x",)) -> str:
        self.db.memories[mid] = {
            "memory_id": mid, "category": category, "title": title,
            "content": content, "tags": list(tags),
            "provenance": dict(provenance or {}), "status": status,
            "created_at": _T0, "updated_at": _T0,
            "embedding": None, "embed_model": None,
        }
        return mid

    def edge(self, from_id: str, to_id: Optional[str], *, correction_id: str,
             relation_id: str, relation_type: str) -> None:
        row = {
            "relation_id": relation_id, "relation_type": relation_type,
            "from_memory_id": from_id, "to_memory_id": to_id,
            "correction_id": correction_id, "authority": AUTHORITY,
            "correction_reason": None, "correction_source_id": None,
            "provenance": "explicit_tool_request", "effective_at": None,
            "recorded_at": _T1, "request": {},
        }
        self.db.edges[("from", from_id)] = row
        if to_id is not None:
            self.db.edges[("to", to_id)] = row

    def stream(self, sid: int, role: str, host: str, session_id: str,
               event_id: str, turn_id: Optional[int] = 3) -> None:
        self.db.streams[sid] = {
            "id": sid, "role": role, "host": host, "session_id": session_id,
            "event_id": event_id, "turn_id": turn_id,
        }

    def qa(self, qid: int, source_id: str) -> None:
        self.db.qa[qid] = {"id": qid, "source_id": source_id}
        self.db.qa_by_source[source_id] = qid

    def topic_entry(self, topic_id: str, source_qa_id: int) -> None:
        self.db.topic_entries.append(
            {"topic_id": topic_id, "source_qa_id": source_qa_id}
        )

    def note(self, nid: int, lo: int, hi: int, content: str) -> None:
        self.db.observation_notes[nid] = {
            "id": nid, "lo": lo, "hi": hi, "content": content,
        }

    def topic(self, topic_id: str, title: str) -> None:
        self.db.topics[topic_id] = {"topic_id": topic_id, "title": title}

    def yin(self, pid: int, content: str) -> None:
        self.db.yin[pid] = {"id": pid, "content": content}


# ── helpers ────────────────────────────────────────────────────────────────

def _mod():
    import importlib

    try:
        return importlib.import_module(MODULE)
    except Exception as exc:  # pragma: no cover - RED surface
        pytest.fail(f"cannot import {MODULE!r}: {exc!r}")


def _ab_db(*, host: str = "h1", session: str = "s1", event: str = "msg-1",
           qa_id: int = 42) -> tuple:
    """A -> B correction with a resolvable live-sync provenance source."""
    db = _FakeDb()
    seed = _Seed(db)
    seed.memory("mem_a", "A content", status="archived",
                provenance={"source_conversation_stream_id": "11"})
    seed.memory("mem_b", "B content", status="active",
                provenance={"corrected_from": "mem_a"})
    seed.edge("mem_a", "mem_b", correction_id="cor_ab",
              relation_id="rel_ab", relation_type="supersedes")
    seed.stream(11, "user", host, session, event)
    seed.qa(qa_id, _live_source_id(host, session, event))
    return db, seed


def _propagate(db, **kw):
    kw.setdefault("correction_id", "cor_ab")
    return _mod().propagate_correction(db, **kw)


def _rows(db, **kw):
    return _mod().list_invalidations(db, **kw)


# ─────────────────────────── public API surface ───────────────────────────

def test_M03_public_api_is_locked():
    mod = _mod()
    for name in (
        "DERIVED_KIND_TOPIC", "DERIVED_KIND_OBSERVER_NOTE",
        "DERIVED_KIND_YIN_PARAGRAPH", "DERIVED_KINDS",
        "STATE_STALE", "STATE_PENDING_REBUILD", "STATE_REBUILT",
        "STATE_UNRESOLVED",
        "PROPAGATION_APPLIED", "PROPAGATION_DEDUPLICATED",
        "PROPAGATION_NO_LINEAGE", "PROPAGATION_MIGRATION_REQUIRED",
        "PROPAGATION_FAILED",
        "RESOLVE_OK", "NO_SOURCE", "SOURCE_NOT_MAPPED", "SOURCE_AMBIGUOUS",
        "SOURCE_IDENTITY_CONFLICT",
    ):
        assert hasattr(mod, name), f"missing locked constant {name}"
    assert mod.DERIVED_KIND_TOPIC == "topic"
    assert mod.DERIVED_KIND_OBSERVER_NOTE == "observer_note"
    assert mod.DERIVED_KIND_YIN_PARAGRAPH == "yin_paragraph"
    assert mod.DERIVED_KINDS == ("topic", "observer_note", "yin_paragraph")
    assert mod.STATE_STALE == "stale"
    assert mod.STATE_PENDING_REBUILD == "pending_rebuild"
    assert mod.STATE_REBUILT == "rebuilt"
    assert mod.STATE_UNRESOLVED == "unresolved"
    assert mod.PROPAGATION_APPLIED == "PROPAGATION_APPLIED"
    assert mod.PROPAGATION_DEDUPLICATED == "PROPAGATION_DEDUPLICATED"
    assert mod.PROPAGATION_NO_LINEAGE == "PROPAGATION_NO_LINEAGE"
    assert mod.PROPAGATION_MIGRATION_REQUIRED == "MIGRATION_REQUIRED"
    assert mod.PROPAGATION_FAILED == "PROPAGATION_FAILED"
    assert mod.RESOLVE_OK == "OK"
    assert mod.NO_SOURCE == "NO_SOURCE"
    assert mod.SOURCE_NOT_MAPPED == "SOURCE_NOT_MAPPED"
    assert mod.SOURCE_AMBIGUOUS == "SOURCE_AMBIGUOUS"
    assert mod.SOURCE_IDENTITY_CONFLICT == "SOURCE_IDENTITY_CONFLICT"

    psig = inspect.signature(mod.propagate_correction)
    assert list(psig.parameters)[:2] == ["pg", "correction_id"]
    assert psig.parameters["correction_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert psig.parameters["dry_run"].kind is inspect.Parameter.KEYWORD_ONLY
    assert psig.parameters["dry_run"].default is False

    rsig = inspect.signature(mod.resolve_source_qa_ids)
    assert list(rsig.parameters) == ["pg", "provenance"]

    dsig = inspect.signature(mod.discover_affected)
    assert list(dsig.parameters) == ["pg", "qa_ids"]

    lsig = inspect.signature(mod.list_invalidations)
    assert list(lsig.parameters)[0] == "pg"
    assert lsig.parameters["correction_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert lsig.parameters["derived_kind"].kind is inspect.Parameter.KEYWORD_ONLY

    isig = inspect.signature(mod.invalidated_ids)
    assert list(isig.parameters) == ["pg", "derived_kind"]


def test_M03_invalidation_id_is_deterministic_and_recomputable():
    mod = _mod()
    raw = "cor_ab|topic|t_d1"
    expected = "inv_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert mod.invalidation_id("cor_ab", "topic", "t_d1") == expected
    assert mod.invalidation_id("cor_ab", "topic", "t_d1") == mod.invalidation_id(
        "cor_ab", "topic", "t_d1"
    )
    # the kind is part of the identity
    assert mod.invalidation_id("cor_ab", "topic", "t_d1") != mod.invalidation_id(
        "cor_ab", "observer_note", "t_d1"
    )
    # source-identity normalisation never enters the derived identity
    assert mod.invalidation_id("cor_ab", "topic", "t_d1") == "inv_" + hashlib.sha256(
        b"cor_ab|topic|t_d1"
    ).hexdigest()


# ─────────────────────────── source resolution ────────────────────────────

def test_M03_resolve_no_source_is_reported_not_guessed():
    db = _FakeDb()
    ids, code = _mod().resolve_source_qa_ids(db, {})
    assert ids == []
    assert code == "NO_SOURCE"
    # a key present with an explicit None is still absence
    assert _mod().resolve_source_qa_ids(
        db, {"source_qa_id": None, "source_conversation_stream_id": None}
    ) == ([], "NO_SOURCE")


def test_M03_resolve_stream_id_through_the_live_sync_candidate():
    db, _seed = _ab_db()
    ids, code = _mod().resolve_source_qa_ids(
        db, {"source_conversation_stream_id": "11"}
    )
    assert ids == [42]
    assert code == "OK"


def test_M03_resolve_stream_id_through_the_i01_import_candidate():
    db = _FakeDb()
    seed = _Seed(db)
    seed.stream(11, "user", "h1", "s1", "msg-1")
    seed.qa(42, _import_source_id("h1", "s1", "msg-1"))
    ids, code = _mod().resolve_source_qa_ids(
        db, {"source_conversation_stream_id": "11"}
    )
    assert ids == [42]
    assert code == "OK"


def test_M03_resolve_qa_id_directly():
    db, _seed = _ab_db()
    ids, code = _mod().resolve_source_qa_ids(db, {"source_qa_id": "42"})
    assert ids == [42]
    assert code == "OK"


def test_M03_both_keys_agreeing_resolve_to_one_identity():
    db, _seed = _ab_db()
    ids, code = _mod().resolve_source_qa_ids(
        db, {"source_conversation_stream_id": "11", "source_qa_id": "42"}
    )
    assert ids == [42]
    assert code == "OK"


def test_M03_both_keys_disagreeing_are_an_identity_conflict():
    db, seed = _ab_db()
    seed.qa(77, _live_source_id("h9", "s9", "other"))
    ids, code = _mod().resolve_source_qa_ids(
        db, {"source_conversation_stream_id": "11", "source_qa_id": "77"}
    )
    assert ids == []
    assert code == "SOURCE_IDENTITY_CONFLICT", (
        "two entry points must not become two identities — one side is never "
        "silently preferred"
    )


def test_M03_two_candidate_forms_hitting_different_qa_rows_is_ambiguous():
    db = _FakeDb()
    seed = _Seed(db)
    seed.stream(11, "user", "h1", "s1", "msg-1")
    seed.qa(42, _live_source_id("h1", "s1", "msg-1"))
    seed.qa(43, _import_source_id("h1", "s1", "msg-1"))
    ids, code = _mod().resolve_source_qa_ids(
        db, {"source_conversation_stream_id": "11"}
    )
    assert ids == []
    assert code == "SOURCE_AMBIGUOUS"


def test_M03_legacy_live_form_is_not_mapped_and_never_infers_a_turn():
    """``conversation_stream`` carries no canonical q_turn, so the legacy live
    ``qa_sync/<session>/<turn>/<msg[:16]>`` form must NOT be resolved — even
    when a matching row physically exists via the turn_id column."""
    db = _FakeDb()
    seed = _Seed(db)
    # a real row that the legacy template WOULD match if turn_id were used
    seed.stream(11, "user", LEGACY_HOST, "s1", "abcdefghijklmnopTAIL", turn_id=3)
    seed.qa(42, "qa_sync/s1/3/abcdefghijklmnop")
    ids, code = _mod().resolve_source_qa_ids(
        db, {"source_conversation_stream_id": "11"}
    )
    assert ids == []
    assert code == "SOURCE_NOT_MAPPED"


def test_M03_user_row_without_a_matching_qa_is_not_mapped():
    """I01 merges consecutive user rows: a role='user' row does NOT guarantee a
    QA row, and the nearest QA must never be selected."""
    db = _FakeDb()
    seed = _Seed(db)
    seed.stream(11, "user", "h1", "s1", "second-user-msg")
    # a QA exists for a DIFFERENT (earlier) user event of the same session
    seed.qa(41, _live_source_id("h1", "s1", "first-user-msg"))
    ids, code = _mod().resolve_source_qa_ids(
        db, {"source_conversation_stream_id": "11"}
    )
    assert ids == []
    assert code == "SOURCE_NOT_MAPPED"


@pytest.mark.parametrize("prov", [
    {"source_conversation_stream_id": "99"},        # no such stream row
    {"source_conversation_stream_id": "+11"},       # not a plain ASCII literal
    {"source_conversation_stream_id": " 11"},
    {"source_conversation_stream_id": ""},
    {"source_conversation_stream_id": "11.5"},
    {"source_qa_id": "9999"},                       # no such qa row
    {"source_qa_id": "abc"},
    {"source_qa_id": ""},
])
def test_M03_unresolvable_or_malformed_reference_is_not_mapped(prov):
    db, _seed = _ab_db()
    ids, code = _mod().resolve_source_qa_ids(db, prov)
    assert ids == []
    assert code == "SOURCE_NOT_MAPPED"


def test_M03_stream_must_be_a_user_message():
    db = _FakeDb()
    _Seed(db).stream(11, "assistant", "h1", "s1", "msg-1")
    _Seed(db).qa(42, _live_source_id("h1", "s1", "msg-1"))
    ids, code = _mod().resolve_source_qa_ids(
        db, {"source_conversation_stream_id": "11"}
    )
    assert ids == []
    assert code == "SOURCE_NOT_MAPPED"


def test_M03_stream_row_without_an_identity_is_not_mapped():
    db = _FakeDb()
    _Seed(db).stream(11, "user", "", "s1", "msg-1")   # empty host
    _Seed(db).qa(42, _live_source_id("h1", "s1", "msg-1"))
    ids, code = _mod().resolve_source_qa_ids(
        db, {"source_conversation_stream_id": "11"}
    )
    assert ids == []
    assert code == "SOURCE_NOT_MAPPED"


def test_M03_source_input_identity_records_the_caller_literal_verbatim():
    mod = _mod()
    assert mod.source_input_identity({"source_qa_id": "42"}) == ("qa_pairs", "42")
    assert mod.source_input_identity(
        {"source_conversation_stream_id": "11"}
    ) == ("conversation_stream", "11")
    assert mod.source_input_identity({}) == (None, None)


# ─────────────────────────── discovery ────────────────────────────────────

def test_M03_topic_discovery_uses_source_qa_id_only():
    db, seed = _ab_db()
    seed.topic_entry("t_d1", 42)
    seed.topic_entry("t_d2", 999)
    affected = _mod().discover_affected(db, [42])
    assert affected == {"topic": ["t_d1"], "observer_note": [], "yin_paragraph": []}


def test_M03_observer_note_discovery_is_range_containment():
    db, seed = _ab_db()
    seed.note(7, 40, 45, "covers qa 42")
    seed.note(8, 100, 200, "does not cover qa 42")
    affected = _mod().discover_affected(db, [42])
    assert affected["observer_note"] == ["7"]
    # yin has no structural edge: never guessed from body text / vectors
    assert affected["yin_paragraph"] == []
    assert affected["topic"] == []


def test_M03_empty_qa_set_discovers_nothing():
    db, seed = _ab_db()
    seed.topic_entry("t_d1", 42)
    assert _mod().discover_affected(db, []) == {
        "topic": [], "observer_note": [], "yin_paragraph": []
    }


# ─────────────────────────── the core propagation ─────────────────────────

def test_M03_topic_directly_attached_to_the_corrected_source_is_invalidated():
    db, seed = _ab_db()
    seed.topic_entry("t_d1", 42)      # D1 directly attached to A's source
    seed.topic_entry("t_d2", 999)     # unrelated

    receipt = _propagate(db)

    assert receipt["status"] == "PROPAGATION_APPLIED", receipt
    assert receipt["success"] is True
    assert receipt["derived_propagation"] == "applied"
    assert receipt["correction_id"] == "cor_ab"
    assert receipt["relation_id"] == "rel_ab"
    assert receipt["source_memory_id"] == "mem_a"
    assert receipt["replacement_memory_id"] == "mem_b"
    assert receipt["resolved_qa_ids"] == [42]
    assert receipt["canonical_qa_id"] == 42
    assert receipt["input_source_kind"] == "conversation_stream"
    assert receipt["input_source_id"] == "11"
    assert receipt["resolution_status"] == "OK"
    assert receipt["counts"]["mapped"] == 1
    assert receipt["counts"]["invalidated"] == 1
    assert receipt["counts"]["unmapped"] == 0
    assert receipt["counts"]["pending_rebuild"] == 1
    assert receipt["counts"]["rebuilt"] == 0
    assert receipt["derived"] == {
        "topic": ["t_d1"], "observer_note": [], "yin_paragraph": [],
    }

    rows = _rows(db, correction_id="cor_ab")
    assert len(rows) == 1
    row = rows[0]
    assert row["derived_kind"] == "topic"
    assert row["derived_id"] == "t_d1"
    assert row["state"] == "pending_rebuild"
    assert row["source_memory"] == "mem_a"
    assert row["replacement"] == "mem_b"
    assert row["invalidated_by"] == "cor_ab"
    assert row["canonical_qa_id"] == 42
    assert row["input_source_kind"] == "conversation_stream"
    assert row["input_source_id"] == "11"
    assert row["invalidation_id"] == _mod().invalidation_id(
        "cor_ab", "topic", "t_d1"
    )
    # D2 was never touched
    assert set(db.invalidations) == {_mod().invalidation_id("cor_ab", "topic", "t_d1")}
    assert _mod().invalidated_ids(db, "topic") == {"t_d1"}


def test_M03_qa_pairs_input_is_recorded_in_the_audit_columns():
    db, seed = _ab_db()
    db.memories["mem_a"]["provenance"] = {"source_qa_id": "42"}
    seed.topic_entry("t_d1", 42)

    receipt = _propagate(db)

    assert receipt["status"] == "PROPAGATION_APPLIED", receipt
    assert receipt["canonical_qa_id"] == 42
    assert receipt["input_source_kind"] == "qa_pairs"
    assert receipt["input_source_id"] == "42"
    row = _rows(db)[0]
    assert row["canonical_qa_id"] == 42
    assert row["input_source_kind"] == "qa_pairs"
    assert row["input_source_id"] == "42"


def test_M03_observer_note_covering_the_source_qa_is_invalidated():
    db, seed = _ab_db()
    seed.note(7, 40, 45, "note covering qa 42")
    seed.note(8, 100, 200, "unrelated note")

    receipt = _propagate(db)

    assert receipt["status"] == "PROPAGATION_APPLIED"
    assert receipt["derived"]["observer_note"] == ["7"]
    assert receipt["counts"]["mapped"] == 1
    assert _mod().invalidated_ids(db, "observer_note") == {"7"}
    assert _mod().invalidated_ids(db, "topic") == set()


def test_M03_rerun_inserts_zero_new_rows_and_reports_deduplicated():
    db, seed = _ab_db()
    seed.topic_entry("t_d1", 42)

    first = _propagate(db)
    size_after_first = len(db.invalidations)
    assert size_after_first == 1
    writes_after_first = db.ops().count("insert_invalidation")

    second = _propagate(db)

    assert len(db.invalidations) == size_after_first == 1
    assert db.ops().count("insert_invalidation") - writes_after_first == 1, (
        "the replay must ATTEMPT the insert (idempotent ON CONFLICT), "
        "but it must not add a row"
    )
    assert second["status"] == "PROPAGATION_DEDUPLICATED", second
    assert second["derived_propagation"] == "deduplicated"
    assert second["counts"]["invalidated"] == 0
    assert second["counts"]["pending_rebuild"] == 0
    assert second["counts"]["mapped"] == 1
    assert first["status"] == "PROPAGATION_APPLIED"


def test_M03_unrelated_derived_is_left_alone():
    db, seed = _ab_db()
    seed.topic_entry("t_d2", 999)
    seed.note(8, 500, 600, "unrelated note")
    seed.yin(3, "unrelated paragraph")

    receipt = _propagate(db)

    assert receipt["status"] == "PROPAGATION_NO_LINEAGE", receipt
    assert receipt["derived_propagation"] == "none"
    assert receipt["counts"]["mapped"] == 0
    assert receipt["counts"]["invalidated"] == 0
    assert receipt["counts"]["unmapped"] == 1
    assert db.invalidations == {}
    assert _mod().invalidated_ids(db, "topic") == set()
    assert _mod().invalidated_ids(db, "observer_note") == set()


def test_M03_withdraw_invalidates_dependents_as_stale_without_a_replacement():
    db = _FakeDb()
    seed = _Seed(db)
    seed.memory("mem_a", "A content", status="archived",
                provenance={"source_conversation_stream_id": "11"})
    seed.edge("mem_a", None, correction_id="cor_wd",
              relation_id="rel_wd", relation_type="withdraws")
    seed.stream(11, "user", "h1", "s1", "msg-1")
    seed.qa(42, _live_source_id("h1", "s1", "msg-1"))
    seed.topic_entry("t_d1", 42)

    receipt = _propagate(db, correction_id="cor_wd")

    assert receipt["status"] == "PROPAGATION_APPLIED", receipt
    assert receipt["replacement_memory_id"] is None, (
        "a withdrawal must not fabricate a replacement fact"
    )
    assert receipt["counts"]["pending_rebuild"] == 0
    row = _rows(db, correction_id="cor_wd")[0]
    assert row["state"] == "stale"
    assert row["replacement"] is None
    assert row["source_memory"] == "mem_a"


def test_M03_chain_current_successor_resolves_to_the_terminal_version():
    db, seed = _ab_db()
    seed.memory("mem_c", "C content", status="active")
    seed.edge("mem_b", "mem_c", correction_id="cor_bc",
              relation_id="rel_bc", relation_type="supersedes")
    seed.topic_entry("t_d1", 42)

    receipt = _propagate(db)

    assert receipt["replacement_memory_id"] == "mem_c", (
        "A->B->C must resolve the CURRENT successor C through read_chain"
    )
    row = _rows(db, correction_id="cor_ab")[0]
    assert row["replacement"] == "mem_c"


def test_M03_unknown_correction_has_no_lineage_and_writes_nothing():
    db, seed = _ab_db()
    seed.topic_entry("t_d1", 42)
    receipt = _propagate(db, correction_id="cor_missing")
    assert receipt["status"] == "PROPAGATION_NO_LINEAGE"
    assert receipt["derived_propagation"] == "none"
    assert db.invalidations == {}


def test_M03_source_without_a_reference_reports_no_source_verbatim():
    db, seed = _ab_db()
    db.memories["mem_a"]["provenance"] = {"origin": "seed"}
    seed.topic_entry("t_d1", 42)
    receipt = _propagate(db)
    assert receipt["status"] == "PROPAGATION_NO_LINEAGE"
    assert receipt["resolution_status"] == "NO_SOURCE"
    assert receipt["resolved_qa_ids"] == []
    assert receipt["derived_propagation"] == "none"
    assert db.invalidations == {}


def test_M03_source_not_mapped_surfaces_verbatim_not_as_a_generic_failure():
    db, seed = _ab_db()
    db.memories["mem_a"]["provenance"] = {"source_conversation_stream_id": "99"}
    seed.topic_entry("t_d1", 42)
    receipt = _propagate(db)
    assert receipt["status"] == "SOURCE_NOT_MAPPED", receipt
    assert receipt["resolution_status"] == "SOURCE_NOT_MAPPED"
    assert receipt["success"] is False
    assert receipt["derived_propagation"] == "pending"
    assert db.invalidations == {}


def test_M03_identity_conflict_surfaces_verbatim():
    db, seed = _ab_db()
    seed.qa(77, _live_source_id("h9", "s9", "other"))
    db.memories["mem_a"]["provenance"] = {
        "source_conversation_stream_id": "11", "source_qa_id": "77",
    }
    seed.topic_entry("t_d1", 42)
    receipt = _propagate(db)
    assert receipt["status"] == "SOURCE_IDENTITY_CONFLICT", receipt
    assert receipt["resolution_status"] == "SOURCE_IDENTITY_CONFLICT"
    assert receipt["success"] is False
    assert db.invalidations == {}


def test_M03_ambiguous_reference_surfaces_verbatim():
    db = _FakeDb()
    seed = _Seed(db)
    seed.memory("mem_a", "A content", status="archived",
                provenance={"source_conversation_stream_id": "11"})
    seed.memory("mem_b", "B content", status="active")
    seed.edge("mem_a", "mem_b", correction_id="cor_ab",
              relation_id="rel_ab", relation_type="supersedes")
    seed.stream(11, "user", "h1", "s1", "msg-1")
    seed.qa(42, _live_source_id("h1", "s1", "msg-1"))
    seed.qa(43, _import_source_id("h1", "s1", "msg-1"))
    seed.topic_entry("t_d1", 42)
    receipt = _propagate(db)
    assert receipt["status"] == "SOURCE_AMBIGUOUS", receipt
    assert receipt["success"] is False
    assert db.invalidations == {}


# ─────────────────────────── idempotency / reads ──────────────────────────

def test_M03_list_invalidations_exposes_the_debug_fields_and_content():
    db, seed = _ab_db()
    seed.topic_entry("t_d1", 42)
    seed.topic("t_d1", "Deploy topic")
    _propagate(db)

    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    for key in ("derived_id", "content", "state", "invalidated_by",
                "source_memory", "replacement"):
        assert key in row, f"list_invalidations row must expose {key!r}"
    assert row["derived_id"] == "t_d1"
    assert row["content"] == "Deploy topic"
    assert row["invalidated_by"] == "cor_ab"
    assert row["source_memory"] == "mem_a"
    assert row["replacement"] == "mem_b"
    assert _rows(db, derived_kind="observer_note") == []
    assert len(_rows(db, derived_kind="topic")) == 1


def test_M03_invalidated_ids_excludes_rebuilt_rows():
    db, seed = _ab_db()
    seed.topic_entry("t_d1", 42)
    _propagate(db)
    iid = next(iter(db.invalidations))
    assert _mod().invalidated_ids(db, "topic") == {"t_d1"}

    db.invalidations[iid]["state"] = "rebuilt"
    assert _mod().invalidated_ids(db, "topic") == set(), (
        "a rebuilt artifact is replaced by its new version and must not "
        "keep suppressing the old id"
    )


def test_M03_dry_run_writes_nothing_but_reports_the_work():
    db, seed = _ab_db()
    seed.topic_entry("t_d1", 42)
    receipt = _propagate(db, dry_run=True)
    assert receipt["dry_run"] is True
    assert receipt["counts"]["invalidated"] == 0
    assert receipt["counts"]["would_invalidate"] == 1
    assert receipt["derived"]["topic"] == ["t_d1"]
    assert db.invalidations == {}


def test_M03_replay_after_dry_run_still_applies():
    db, seed = _ab_db()
    seed.topic_entry("t_d1", 42)
    _propagate(db, dry_run=True)
    receipt = _propagate(db)
    assert receipt["status"] == "PROPAGATION_APPLIED"
    assert len(db.invalidations) == 1


# ─────────────────────────── migration / boundaries ───────────────────────

def test_M03_missing_sidecar_table_is_a_truthful_migration_required():
    db, seed = _ab_db()
    seed.topic_entry("t_d1", 42)
    db.missing_invalidations = True

    receipt = _propagate(db)

    assert receipt["status"] == "MIGRATION_REQUIRED", receipt
    assert receipt["success"] is False
    assert receipt["derived_propagation"] == "pending"
    assert "derived_memory_invalidations" in (receipt["error"] or "")
    assert db.invalidations == {}


def test_M03_open_side_connection_returning_none_fails_closed():
    db, seed = _ab_db()
    seed.topic_entry("t_d1", 42)

    class _PgLike:
        def __init__(self, inner: _FakeDb) -> None:
            self._inner = inner

        def lease(self, timeout: Optional[float] = None) -> _FakeLease:
            return self._inner.lease(timeout)

        def open_side_connection(self):
            return None

    receipt = _propagate(_PgLike(db))

    assert receipt["success"] is False
    assert receipt["status"] == "PROPAGATION_FAILED", receipt
    assert db.invalidations == {}
    assert "insert_invalidation" not in db.ops()


def test_M03_never_rewrites_or_deletes_derived_content():
    text = (V3CORE_SRC / "derived_invalidation.py").read_text(encoding="utf-8")
    upper = text.upper()
    for forbidden in ("DELETE FROM", "TRUNCATE", "DROP "):
        assert forbidden not in upper, (
            f"the invalidation layer must stay additive ({forbidden!r})"
        )
    for table in ("PUBLIC.TOPICS", "PUBLIC.OBSERVATION_NOTES",
                  "PUBLIC.YIN_PARAGRAPHS", "PUBLIC.TOPIC_ENTRIES",
                  "PUBLIC.QA_PAIRS", "PUBLIC.EXPLICIT_MEMORIES"):
        assert f"UPDATE {table}" not in upper, (
            f"derived bodies are never rewritten ({table})"
        )
    # exactly one INSERT target: the sidecar itself
    assert upper.count("INSERT INTO") == 1, (
        "the invalidation layer writes only the sidecar table"
    )
    assert "INSERT INTO {_INVALIDATION_TABLE}" in text
    assert '"public.derived_memory_invalidations"' in text
    assert '_INVALIDATION_TABLE = "public.derived_memory_invalidations"' in text


def _executable_source_text(path: Path) -> str:
    """Code-only text: docstrings and comments are excluded.

    A forbidden-method scan must judge the CODE, not the prose that documents
    which methods are forbidden.
    """
    import ast

    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    chunks: list[str] = []
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


def test_M03_forbidden_resolution_methods_are_absent():
    """No text match / nearest / similarity / turn inference in the resolver."""
    path = V3CORE_SRC / "derived_invalidation.py"
    code = _executable_source_text(path).lower()
    for forbidden in ("ilike", "similarity", "cosine", "embedding",
                      "order by timestamp", "order by id desc",
                      "qa_sync/", "qa_import/", "[:16]", "turn_id",
                      "nearest", "last_value", "max("):
        assert forbidden not in code, (
            f"forbidden resolution method present in executable code: "
            f"{forbidden!r}"
        )
    # and the forbidden methods are documented as forbidden
    doc = path.read_text(encoding="utf-8")
    assert "FORBIDDEN RESOLUTION METHODS" in doc
    assert "never" in doc.lower()


def test_M03_module_never_opens_its_own_db_connection():
    text = (V3CORE_SRC / "derived_invalidation.py").read_text(encoding="utf-8")
    for forbidden in ("psycopg2.connect", "CREATE TABLE", "subprocess", "open("):
        assert forbidden not in text, (
            f"derived_invalidation must never own connections/schema ({forbidden!r})"
        )
