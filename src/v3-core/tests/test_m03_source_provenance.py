# -*- coding: utf-8 -*-
"""M03 write-side source provenance + auto-propagation + rerun + doctor.

Contract under test (docs/M03-CORRECTION-PROPAGATION.md, frozen source-key
contract):

  * An explicit-memory write may declare its conversation source through the
    two frozen keys ``source_conversation_stream_id`` / ``source_qa_id``.
    Validation reuses the ONE resolver (``derived_invalidation``); a present
    but unresolvable / ambiguous / conflicting reference is REFUSED with the
    frozen code — never fabricated, stripped or downgraded to "no source".
  * Omitting both keys keeps today's behaviour exactly (backward compatible).
  * After a committed M02 correction, propagation runs once. A propagation
    failure NEVER rolls the correction back: the receipt carries
    ``derived_propagation='pending'`` plus the propagation receipt.
  * ``v3_update(action='propagate')`` re-runs propagation for one
    ``correction_id`` and is idempotent (a second run inserts zero rows).
  * ``hippocampus doctor`` knows the M03 artifact and probes the live table, so
    a DB missing it is discovered; a missing sidecar is a real
    ``MIGRATION_REQUIRED``, never a silent fallback.

No real PostgreSQL: the P0-A conftest hard-blocks psycopg2.connect, so this
contract is proven against a deterministic in-memory fake.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

import pytest

AMS_MODULE = "v3core.active_memory_store"
DI_MODULE = "v3core.derived_invalidation"

REPO_ROOT = Path(__file__).resolve().parents[3]
V3CORE = REPO_ROOT / "src" / "v3-core"
CLI_PATH = V3CORE / "src" / "v3core" / "distribution_cli.py"

AUTHORITY = "user_explicit"

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
    return f"qa_sync/{host}/{session}/{event}"


# ── deterministic in-memory fake PG (write + resolver + propagation) ──────

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
        if "INSERT INTO PUBLIC.EXPLICIT_MEMORIES" in s:
            return "insert_memory"
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
        if op == "insert_memory":
            mid = p[0]
            if mid in conn.memories:
                self._one = None
                self.rowcount = 0
                return
            prov = p[5]
            if isinstance(prov, str):
                try:
                    prov = json.loads(prov)
                except ValueError:
                    prov = {}
            conn._record_undo("memories", mid)
            conn.memories[mid] = {
                "memory_id": mid, "category": p[1], "title": p[2],
                "content": p[3], "tags": list(p[4] or []), "provenance": prov,
                "status": "active", "created_at": _T0, "updated_at": _T0,
                "embedding": None, "embed_model": None,
            }
            self._one = (mid,)
            self.rowcount = 1
        elif op == "read_memory":
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
            if "INVALIDATION_ID = ANY(%S)" in s:
                wanted = {str(x) for x in (p[0] if p else [])}
                self._many = [(r["invalidation_id"],) for r in rows
                              if r["invalidation_id"] in wanted]
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


# ── helpers ───────────────────────────────────────────────────────────────

def _ams():
    import importlib

    return importlib.import_module(AMS_MODULE)


def _di():
    import importlib

    return importlib.import_module(DI_MODULE)


def _writer(db: _FakeDb):
    return _ams().ActiveMemoryWriter(pool=None, pg=db, config=None)


def _resolve_db(*, host: str = "h1", session: str = "s1",
                event: str = "msg-1", qa_id: int = 42) -> tuple:
    """A live-sync source: stream 11 (role=user) → qa_pairs.id 42."""
    db = _FakeDb()
    seed = _Seed(db)
    seed.stream(11, "user", host, session, event)
    seed.qa(qa_id, _live_source_id(host, session, event))
    return db, seed


def _ab_db() -> tuple:
    """A -> B correction whose source memory declares stream 11 → qa 42."""
    db, seed = _resolve_db()
    seed.memory("mem_a", "A content", status="archived",
                provenance={"source_conversation_stream_id": "11"})
    seed.memory("mem_b", "B content", status="active",
                provenance={"corrected_from": "mem_a"})
    seed.edge("mem_a", "mem_b", correction_id="cor_ab",
              relation_id="rel_ab", relation_type="supersedes")
    return db, seed


def _stored_provenance(db: _FakeDb, mid: str) -> dict:
    prov = db.memories[mid]["provenance"]
    if isinstance(prov, str):
        prov = json.loads(prov)
    return prov


# ══════════════════════════ A. write-side provenance ═════════════════════

def test_valid_stream_reference_is_written_with_audit_fields():
    db, _seed = _resolve_db()
    result = _writer(db).create(
        "ops", "t", "body", provenance={"source_conversation_stream_id": "11"},
    )
    assert result.success is True, result
    prov = _stored_provenance(db, result.memory_id)
    # caller's literal input is kept verbatim
    assert prov["source_conversation_stream_id"] == "11"
    # the single canonical identity + audit fields
    assert prov["canonical_qa_id"] == 42
    assert prov["input_source_kind"] == "conversation_stream"
    assert prov["input_source_id"] == "11"


def test_valid_qa_pairs_reference_is_written_with_audit_fields():
    db, _seed = _resolve_db()
    result = _writer(db).create(
        "ops", "t", "body", provenance={"source_qa_id": "42"},
    )
    assert result.success is True, result
    prov = _stored_provenance(db, result.memory_id)
    assert prov["source_qa_id"] == "42"
    assert prov["canonical_qa_id"] == 42
    assert prov["input_source_kind"] == "qa_pairs"
    assert prov["input_source_id"] == "42"


def test_unresolvable_reference_is_refused_with_the_frozen_code():
    db, _seed = _resolve_db()
    result = _writer(db).create(
        "ops", "t", "body", provenance={"source_qa_id": "999"},
    )
    assert result.success is False
    assert result.status == "SOURCE_NOT_MAPPED", result.status
    assert result.durable is False
    # nothing was written — no fabricated row, no downgrade to "no source"
    assert db.memories == {}
    assert "insert_memory" not in db.ops()


def test_malformed_reference_is_refused_not_normalised():
    db, _seed = _resolve_db()
    result = _writer(db).create(
        "ops", "t", "body", provenance={"source_conversation_stream_id": "+11"},
    )
    assert result.success is False
    assert result.status == "SOURCE_NOT_MAPPED"
    assert db.memories == {}


def test_disagreeing_keys_are_an_identity_conflict_and_refused():
    db, seed = _resolve_db()
    seed.qa(77, _live_source_id("h9", "s9", "other"))
    result = _writer(db).create(
        "ops", "t", "body",
        provenance={"source_conversation_stream_id": "11", "source_qa_id": "77"},
    )
    assert result.success is False
    assert result.status == "SOURCE_IDENTITY_CONFLICT", result.status
    assert db.memories == {}


def test_omitted_source_keys_keep_the_existing_behaviour():
    db = _FakeDb()
    result = _writer(db).create(
        "ops", "t", "body", provenance={"source": "user", "when": "today"},
    )
    assert result.success is True, result
    prov = _stored_provenance(db, result.memory_id)
    assert prov == {"source": "user", "when": "today"}
    # no audit identity is invented when no source was declared
    assert "canonical_qa_id" not in prov
    assert "input_source_kind" not in prov
    assert "input_source_id" not in prov


def test_tool_layer_whitelists_and_forwards_the_two_keys():
    from v3core.tools import store as store_tool

    assert "source_conversation_stream_id" in store_tool._PROVENANCE_KEYS
    assert "source_qa_id" in store_tool._PROVENANCE_KEYS

    seen: dict = {}

    class _Core:
        def store_card(self, category, title, content, tags=None, **kwargs):
            seen.update(kwargs)
            return type("R", (), {
                "success": True, "path": "explicit_memories/m1",
                "source_id": "m1", "durable": True,
                "durable_store": "explicit_memories",
                "status": "DURABLE_COMMITTED", "warnings": [],
                "card": None, "error": "",
            })()

    out = json.loads(store_tool.handle_v3_store(
        {"category": "ops", "title": "t", "content": "body",
         "source_conversation_stream_id": "11", "source_qa_id": "42"},
        core=_Core(),
    ))
    assert out["success"] is True
    assert seen.get("provenance") == {
        "source_conversation_stream_id": "11", "source_qa_id": "42",
    }


# ══════════════════ B. auto-propagation after a commit ════════════════════

def test_propagation_failure_never_rolls_back_the_committed_correction(
    monkeypatch,
):
    import test_m01_memory_correction as m01

    db = m01._FakeDb()
    m01._Seed(db).add(
        "mem_a", "A content",
        provenance={"source_conversation_stream_id": "11"},
    )
    di = _di()

    def _boom(pg, *, correction_id, dry_run=False):
        raise RuntimeError("derived_memory_invalidations is missing")

    monkeypatch.setattr(di, "propagate_correction", _boom)

    receipt = m01._writer(db).correct(
        "mem_a", "B content", authority=AUTHORITY,
    )

    # the correction is durable and NOT rolled back
    assert receipt["success"] is True, receipt
    assert receipt["durable"] is True
    assert db.memories["mem_a"]["status"] == "archived"
    assert db.memories[receipt["new_memory_id"]]["status"] == "active"
    assert ("from", "mem_a") in db.edges
    # ... and the caller sees the derived layer is not caught up
    assert receipt["derived_propagation"] == "pending", receipt
    assert receipt["derived_receipt"]["status"] == di.PROPAGATION_FAILED


def test_propagation_failure_is_surfaced_by_the_correct_tool(monkeypatch):
    """The M02 tool receipt carries the pending marker through verbatim."""
    import test_m01_memory_correction as m01
    from v3core.tools import memory_correction as mc

    db = m01._FakeDb()
    m01._Seed(db).add(
        "mem_a", "A content",
        provenance={"source_conversation_stream_id": "11"},
    )
    di = _di()
    monkeypatch.setattr(
        di, "propagate_correction",
        lambda pg, *, correction_id, dry_run=False: (_ for _ in ()).throw(
            RuntimeError("sidecar missing")
        ),
    )
    # real writer, isolated scope
    monkeypatch.setattr(
        mc, "_writer_cls",
        lambda **kw: m01._writer(db),
    )
    out = json.loads(mc.handle_correct_action(
        {"memory_id": "mem_a", "replacement_content": "B content"},
        pool=db, effective_config={},
    ))
    assert out["success"] is True, out
    assert out["derived_propagation"] == "pending", out


# ══════════════════ C. explicit rerun entry (idempotent) ══════════════════

def test_rerun_entry_replays_and_is_idempotent():
    from v3core.tools.api_update import handle_v3_update

    db, seed = _ab_db()
    seed.topic_entry("t_1", 42)
    seed.note(7, 40, 45, "note body")

    first = json.loads(handle_v3_update(
        {"action": "propagate", "correction_id": "cor_ab"},
        pool=db, effective_config={},
    ))
    assert first["success"] is True, first
    assert first["status"] == "PROPAGATION_APPLIED", first["status"]
    assert first["derived_propagation"] == "applied"
    assert first["counts"]["invalidated"] == 2, first["counts"]
    assert len(db.invalidations) == 2

    second = json.loads(handle_v3_update(
        {"action": "propagate", "correction_id": "cor_ab"},
        pool=db, effective_config={},
    ))
    assert second["success"] is True, second
    assert second["status"] == "PROPAGATION_DEDUPLICATED", second["status"]
    assert second["counts"]["invalidated"] == 0, second["counts"]
    assert len(db.invalidations) == 2, "a replay must insert zero new rows"


def test_rerun_entry_requires_a_correction_id():
    from v3core.tools.api_update import handle_v3_update

    out = json.loads(handle_v3_update(
        {"action": "propagate"}, pool=_FakeDb(), effective_config={},
    ))
    assert out["success"] is False
    assert out["error_code"] == "INVALID_REQUEST"


def test_rerun_entry_dry_run_writes_nothing():
    from v3core.tools.api_update import handle_v3_update

    db, seed = _ab_db()
    seed.topic_entry("t_1", 42)
    out = json.loads(handle_v3_update(
        {"action": "propagate", "correction_id": "cor_ab",
         "propagate_dry_run": True},
        pool=db, effective_config={},
    ))
    assert out["success"] is True, out
    assert out["dry_run"] is True
    assert out["counts"]["would_invalidate"] == 1, out["counts"]
    assert db.invalidations == {}


def test_rerun_entry_reports_migration_required_for_a_missing_sidecar():
    from v3core.tools.api_update import handle_v3_update

    db, seed = _ab_db()
    seed.topic_entry("t_1", 42)
    db.missing_invalidations = True

    out = json.loads(handle_v3_update(
        {"action": "propagate", "correction_id": "cor_ab"},
        pool=db, effective_config={},
    ))
    assert out["success"] is False
    assert out["status"] == "MIGRATION_REQUIRED", out["status"]
    assert out["derived_propagation"] == "pending"


# ═══════════════════════════ D. doctor wiring ═════════════════════════════

def test_doctor_inventory_and_live_probe_know_the_m03_artifact():
    body = CLI_PATH.read_text(encoding="utf-8")
    doctor_start = body.index('for name in ("alpha_bootstrap.sql"')
    doctor_end = body.index("# 3. Packaged v3hermes plugin.yaml", doctor_start)
    assert '"derived_memory_invalidations.sql"' in body[doctor_start:doctor_end], (
        "doctor 的 packaged SQL 检查未包含 derived_memory_invalidations.sql"
    )
    req_start = body.index('for table in ("explicit_memories"')
    req_end = body.index("information_schema.tables", req_start)
    assert '"derived_memory_invalidations"' in body[req_start:req_end], (
        "doctor 的 live required_tables 未包含 derived_memory_invalidations"
    )

    import importlib

    dc = importlib.import_module("v3core.distribution_cli")
    packaged = dc._package_sql("derived_memory_invalidations.sql")
    assert "CREATE TABLE IF NOT EXISTS public.derived_memory_invalidations" in packaged
    canonical = (V3CORE / "schema" / "derived_memory_invalidations.sql").read_text(
        encoding="utf-8"
    )
    assert packaged == canonical


def _install_fake_psycopg2(monkeypatch, tables: set) -> None:
    """A psycopg2 stand-in: no socket, deterministic table presence."""

    class _Cur:
        def __init__(self) -> None:
            self._one: Optional[tuple] = None

        def __enter__(self) -> "_Cur":
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

        def execute(self, sql: str, params: Any = ()) -> None:
            up = _norm(sql)
            self._one = None
            if "INFORMATION_SCHEMA.TABLES" in up:
                name = params[0] if params else None
                self._one = (1,) if name in tables else None

        def fetchone(self) -> Optional[tuple]:
            return self._one

    class _Conn:
        def cursor(self) -> _Cur:
            return _Cur()

        def close(self) -> None:
            return None

    fake = ModuleType("psycopg2")
    fake.connect = lambda *a, **kw: _Conn()  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "psycopg2", fake)


def test_doctor_live_probe_discovers_a_missing_m03_table(monkeypatch):
    import importlib

    dc = importlib.import_module("v3core.distribution_cli")
    dsn = "postgresql://u:p@127.0.0.1:5432/probe_db"

    _install_fake_psycopg2(monkeypatch, tables={"explicit_memories", "qa_pairs"})
    info = dc._doctor_probe_database(dsn)
    assert info["reachable"] is True, info
    assert info["required_tables"]["derived_memory_invalidations"] is False, (
        "a live DB missing the M03 sidecar must be discovered, not silently ok"
    )

    _install_fake_psycopg2(
        monkeypatch,
        tables={"explicit_memories", "qa_pairs", "derived_memory_invalidations"},
    )
    info = dc._doctor_probe_database(dsn)
    assert info["required_tables"]["derived_memory_invalidations"] is True
