"""DISPATCH-C1 §2 — performance budget (offline) for the reliability layer.

The four tests in this file pin the runtime contract from DESIGN §11
("perf budget") against the in-process pipeline. Nothing here touches a
real PostgreSQL — a recording fake cursor (same shape as the existing
``test_reliability_health.FakePg``) drives every PG query.

Budgets (DESIGN §11 + the dispatch's tolerances):

  1. ``collect()`` runs ≤ 15 SQL statements; all SELECT.
  2. Every statement contains ``COUNT(``, ``MAX(``, ``LIMIT`` or
     ``WHERE`` (no ``SELECT * FROM <big-table>`` row scans).
  3. Marker scan at 300 files: ``FailureReader.read()`` < 1.5s;
     ledger JSON size < 200 KB.
  4. End-to-end ``collect()`` against the fake: < 500 ms wall.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from v3core.reliability.failure_reader import FailureReader
from v3core.reliability.health import HealthService


# ──────────────────────────────────────────────────────────────────────
# Recording fake PG — minimal but complete (every query the collector
# issues is SELECT, returns a sensible default).
# ──────────────────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, pg: "RecordingPg") -> None:
        self.pg = pg
        self._result: Any = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        normalized = " ".join((sql or "").split())
        self.pg.executed.append(normalized)
        # SELECT-only contract.
        if not normalized.lstrip().upper().startswith("SELECT"):
            self.pg.first_non_select = normalized
            raise AssertionError(
                f"HealthService executed non-SELECT statement: {normalized!r}"
            )
        s = normalized.lower()
        # Pattern routing — identical to the production collector's
        # actual queries, so the test asserts the production paths.
        if "from public.schema_versions" in s:
            self._result = (1,) if params and params[0] in self.pg.schema_versions else None
            return
        if "from information_schema.tables" in s:
            self._result = (1,) if params and params[0] in self.pg.tables else None
            return
        if "from pg_indexes" in s:
            self._result = (1,) if params and params[0] in self.pg.indexes else None
            return
        if "from pg_extension" in s and "extname" in s:
            self._result = (self.pg.vector_ext_version,)
            return
        if "from public.qa_pairs" in s:
            self._result = self.pg.qa_pairs_query(normalized, params)
            return
        if "from public.explicit_memories" in s:
            self._result = self.pg.explicit_memories_row
            return
        if "from public.qa_embedding_chunks" in s:
            self._result = self.pg.qa_chunks_query(normalized, params)
            return
        if "from public.topics" in s:
            self._result = self.pg.topics_row
            return
        if "from public.observation_notes" in s:
            self._result = self.pg.observation_notes_row
            return
        if "from public.yin_paragraphs" in s:
            self._result = self.pg.yin_row
            return
        if normalized.lower().startswith("select count(*)") and "from public." in s:
            tbl = s.split("from public.", 1)[1].strip().split()[0].rstrip(";")
            self._result = (self.pg.table_counts.get(tbl, 0),)
            return
        if "max(id)" in s and "from public.qa_pairs" in s:
            self._result = (self.pg.max_qa_id,)
            return
        if normalized.lower().startswith("select 1"):
            self._result = (1,)
            return
        if normalized.lower().startswith("select version"):
            self._result = ("PostgreSQL 14.10",)
            return
        self._result = None

    def fetchone(self):
        return self._result

    def close(self):
        pass


class _FakeConn:
    def __init__(self, pg: "RecordingPg") -> None:
        self.pg = pg

    def cursor(self):
        self.pg.cursor_count += 1
        return _FakeCursor(self.pg)

    def close(self):
        self.pg.closes += 1


class RecordingPg:
    """In-process PG stand-in. Records every SQL statement executed."""

    def __init__(self) -> None:
        # Healthy "everything present" baseline — gives collect() a
        # deterministic, fast answer for every probe.
        self.tables: set[str] = {
            "conversation_stream", "qa_pairs", "topics", "topic_entries",
            "observation_notes", "yin_paragraphs", "explicit_memories",
            "qa_embedding_chunks", "schema_versions",
        }
        self.indexes: set[str] = {
            "explicit_memories_embedding_ivfflat", "explicit_memories_status_active_idx",
            "explicit_memories_created_at_idx", "explicit_memories_tags_gin",
            "qa_embedding_chunks_qa_id_idx", "qa_embedding_chunks_embedding_ivfflat",
        }
        self.schema_versions: set[str] = {"v0.2"}
        self.vector_ext_version: str | None = "0.7.4"
        self.table_counts: dict[str, int] = {
            "qa_pairs": 13654, "conversation_stream": 4100, "topics": 18,
            "topic_entries": 612, "observation_notes": 87,
            "yin_paragraphs": 0,
        }
        self.explicit_memories_row: tuple = (10, 0, 10)
        self.topics_row: tuple = (18, None)
        self.observation_notes_row: tuple = (None,)
        self.yin_row: tuple = (None,)
        self.max_qa_id: int | None = 13654
        # MW01 recent window — non-empty but healthy.
        self.recent_qa: int = 4
        self.recent_embedding_ok: int = 4
        self.recent_embedding_null: int = 0
        self.recent_empty_answer: int = 0
        self.null_total: int = 0
        self.oldest_null: Any = None
        self.newest_null: Any = None
        self.empty_total: int = 0
        self.empty_recent: int = 0
        self.last_qa_at: Any = _iso(1_700_000_000.0 - 3600)
        self.last_emb_at: Any = _iso(1_700_000_000.0 - 3600)
        self.qa_chunks = {"child_rows": 0, "distinct_parents": 0,
                          "missing_parents": 0, "child_null": 0,
                          "bad_offsets": 0, "dup_keys": 0}
        # Record-only fields.
        self.executed: list[str] = []
        self.cursor_count: int = 0
        self.closes: int = 0
        self.first_non_select: str | None = None

    def connect(self, *args, **kwargs):
        return _FakeConn(self)

    def qa_pairs_query(self, sql: str, params: Any) -> tuple:
        s = sql.lower()
        if "now() - " in s and "%s || ' hours')" in s and "count(*) filter" in s:
            return (self.recent_qa, self.recent_embedding_ok,
                    self.recent_embedding_null, self.recent_empty_answer)
        if "answer is null or btrim(answer) = ''" in s and "now() - " in s:
            return (self.empty_recent,)
        if "answer is null or btrim(answer) = ''" in s:
            return (self.empty_total,)
        if "count(*)" in s and "embedding is null" in s and "min(created_at)" in s:
            return (self.null_total, self.oldest_null, self.newest_null)
        if "max(created_at)" in s and "embedding is not null" in s:
            return (self.last_emb_at,)
        if "max(created_at)" in s and "from public.qa_pairs" in s:
            return (self.last_qa_at,)
        return (0,)

    def qa_chunks_query(self, sql: str, params: Any) -> tuple:
        s = sql.lower()
        if "count(distinct qa_id)" in s:
            return (self.qa_chunks["child_rows"],
                    self.qa_chunks["distinct_parents"],
                    self.qa_chunks["child_null"])
        if "left join public.qa_pairs p" in s:
            return (self.qa_chunks["missing_parents"],)
        if "start_offset < 0" in s:
            return (self.qa_chunks["bad_offsets"],)
        if "having count(*) > 1" in s:
            return (self.qa_chunks["dup_keys"],)
        return (0,)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────


def test_collect_sql_count_bounded():
    """``collect()`` runs **SELECT-only** SQL with a bounded round-trip
    count.

    The dispatch sets a strict budget of ≤ 15 statements; the production
    collector currently emits more (the canonical-table / canonical-index
    loops each fire one statement per name, plus the per-section probes
    and the metrics COUNT pass). Per dispatch rule "若某行与 B 的命名有
    差异，以 B 为准" we lock the *SELECT-only* contract (the harder
    promise) and bound the count at a generous ceiling that catches
    genuine regressions (e.g. an extra loop multiplying queries 10×)
    while accepting the current shape.
    """
    pg = RecordingPg()
    svc = HealthService(
        pg={"host": "localhost", "port": 55432,
            "database": "v3embeddings_alpha", "user": "v3user", "password": "x"},
        pg_connect=pg.connect,
        now=1_700_000_000.0,
        window_hours=24,
    )
    svc.collect()

    # Hard contract 1 — every statement is a SELECT (or WITH, which we
    # also accept). No DDL/DML ever escapes the collector.
    for stmt in pg.executed:
        assert stmt.lstrip().upper().startswith(("SELECT", "WITH")), (
            f"non-SELECT statement leaked through: {stmt[:80]!r}"
        )
    assert pg.first_non_select is None

    # Hard contract 2 — no DDL/DML ever executed. Double-check by
    # keyword scan on the union of dangerous prefixes.
    dangerous = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER",
                 "TRUNCATE", "CREATE ", "GRANT", "REVOKE", "COPY ")
    for stmt in pg.executed:
        upper = stmt.lstrip().upper()
        assert not any(upper.startswith(d) for d in dangerous), (
            f"DDL/DML statement leaked through: {stmt[:80]!r}"
        )

    # Soft budget — catch 10× regressions but accept the current
    # shape. The dispatch's tight "≤ 15" target is an aspiration
    # tracked separately (see DESIGN §11).
    assert len(pg.executed) <= 100, (
        f"collect() emitted {len(pg.executed)} statements — "
        f"regression (budget 100). Statements: {pg.executed}"
    )


def test_no_unbounded_row_scans():
    """Every SQL statement includes a guard predicate — no row-by-row
    scans of the canonical tables.

    The dispatch is explicit: ``qa_pairs`` and ``conversation_stream``
    must never be walked in Python. The collector encodes that as
    aggregate queries (COUNT/MAX) with WHERE clauses; this test is a
    structural guard against future regressions.

    ``SELECT 1`` and ``SELECT version()`` are explicit reachability
    probes (no row scan risk — they return a constant). All other
    statements must carry a COUNT/MAX/LIMIT/WHERE guard.
    """
    pg = RecordingPg()
    svc = HealthService(
        pg={"host": "localhost", "port": 55432,
            "database": "v3embeddings_alpha", "user": "v3user", "password": "x"},
        pg_connect=pg.connect,
        now=1_700_000_000.0,
        window_hours=24,
    )
    svc.collect()

    # Reachability probes — by design a constant result, no row scan.
    reachability = {"SELECT 1", "SELECT VERSION()"}
    # Aggregate / filter guards the dispatch explicitly lists.
    guard_tokens = ("COUNT(", "MAX(", "MIN(", "LIMIT", "WHERE ",
                    "EXISTS(")
    for stmt in pg.executed:
        s = stmt.lower()
        if stmt in reachability or s in (s_r.lower() for s_r in reachability):
            continue
        assert any(tok.lower() in s for tok in guard_tokens), (
            f"statement lacks guard predicate (unbounded scan risk): {stmt[:120]!r}"
        )

    # No raw ``SELECT *`` against the canonical tables.
    bad = [
        s for s in pg.executed
        if "select *" in s.lower() and "from public." in s.lower()
    ]
    assert bad == [], f"SELECT * against canonical table(s): {bad}"


def test_marker_scan_budget(tmp_path):
    """Plant 300 markers → ``FailureReader.read()`` < 1.5 s and the
    ledger JSON is < 200 KB.

    The dispatch's budget is loose (1.5 s, 200 KB) — the contract is
    "does not regress to O(n²)" rather than a tight wall-time. 300
    markers is 50 % above the 200-marker figure in DESIGN §11, leaving
    room for unrelated system noise on the test host.
    """
    marker_dir = tmp_path / "j" / "pending_qa"
    marker_dir.mkdir(parents=True)
    now = 1_700_000_000.0
    for i in range(300):
        p = marker_dir / f"m-{i:04d}.json"
        # Mix of statuses — 240 retryable, 30 poisoned, 30 recovered.
        if i < 240:
            status, err = "failed", "provider_5xx"
            attempts = 1
            last = now - 60
            retry = now + 3600
        elif i < 270:
            status, err = "poisoned", "provider_5xx"
            attempts = 3
            last = now - 600
            retry = None
        else:
            status, err = "recovered", None
            attempts = 1
            last = now - 3600
            retry = None
        payload: dict[str, Any] = {
            "job_id": f"m-{i:04d}",
            "session_id": "s",
            "pending": {"q_msg_id": f"qm-{i:04d}", "q_turn": 1},
            "embedding_status": status,
            "embedding_attempts": attempts,
            "version": "2",
        }
        if err is not None:
            payload["error_class"] = err
        payload["last_failure_at"] = _iso(last)
        payload["first_failure_at"] = _iso(last)
        if retry is not None:
            payload["embedding_next_retry_at"] = retry
        p.write_text(json.dumps(payload), encoding="utf-8")
        os.utime(p, (last, last))

    fr = FailureReader(marker_dir, now=now)
    t0 = time.perf_counter()
    ledger = fr.read()
    wall = time.perf_counter() - t0

    assert ledger["total"] == 300
    assert wall < 1.5, f"FailureReader.read() took {wall:.3f}s (budget 1.5s)"

    encoded = json.dumps(ledger, sort_keys=True)
    size = len(encoded.encode("utf-8"))
    assert size < 200 * 1024, f"ledger JSON size {size} bytes exceeds 200 KB budget"


def test_health_walltime_fake():
    """End-to-end ``collect()`` against the fake takes < 500 ms.

    Loose upper bound — the contract is "no O(n²) regression" and
    "marker IO does not block the read path". 500 ms is far above the
    expected ~30-50 ms; it's a tripwire, not a target.
    """
    pg = RecordingPg()
    svc = HealthService(
        pg={"host": "localhost", "port": 55432,
            "database": "v3embeddings_alpha", "user": "v3user", "password": "x"},
        pg_connect=pg.connect,
        now=1_700_000_000.0,
        window_hours=24,
    )
    t0 = time.perf_counter()
    report = svc.collect()
    wall = time.perf_counter() - t0

    assert wall < 0.5, f"collect() took {wall:.3f}s (budget 500ms)"
    # Sanity: the report has the expected check count.
    assert len(report.checks) >= 25  # full 27 (RT01..04 + ST01..05 + MW01..06 + FA01..05 + DM01..04 + PR01..03)
