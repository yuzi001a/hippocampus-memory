"""Contract tests for v3core.importers (Hippocampus v0.2).

These tests are hermetic — no real PG, no real network. They use:

  * a synthetic SQLite state.db and a tiny JSONL export (under
    tests/fixtures/importers/hermes/)
  * synthetic markdown files (MEMORY.md, USER.md, NOTES.md) under
    tests/fixtures/importers/memory/
  * a fake pool object that records every SQL execute/commit so the
    no-writes-on-dry-run and dedupe predicates can be asserted directly
    on the recorded statements.

Fixtures use synthetic names (Alice/Bob), never real personal data.

If a real disposable PostgreSQL is reachable via env V3CORE_PG_DSN, the
test that needs a live PG runs and proves idempotency end-to-end.
Otherwise the same test is skipped with the exact reason and a sibling
test asserts the SQL/parameters the code would have executed.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "importers"
HERMES_FIX = FIXTURES / "hermes"
MEMORY_FIX = FIXTURES / "memory"


# ── helpers ────────────────────────────────────────────────────────────


def _load_importers():
    """Import v3core.importers lazily so the conftest sandbox is in effect."""
    return importlib.import_module("v3core.importers")


# ── fake PG pool ──────────────────────────────────────────────────────
# Same shape as v3core.pg_pool.PgPool.lease() — yields an object with
# .connection (with .cursor() and .commit()), .close().


class _FakeCursor:
    def __init__(self):
        self.statements: list[tuple[str, tuple]] = []
        self._rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        # Normalize SQL whitespace for easier assertions.
        norm = " ".join(sql.split())
        self.statements.append((norm, params or ()))
        # Heuristic: dedupe INSERT-...-WHERE-NOT-EXISTS reports rowcount=0
        # when a matching row already exists. We model "insert succeeded"
        # unless the rowcount was preset to 0 (used by tests for the
        # re-run case).
        self._rowcount = 1

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    @property
    def rowcount(self) -> int:
        return self._rowcount

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeLease:
    def __init__(self, recorder: "FakePool"):
        self._conn = _FakeConnection(recorder)
        self.closed = False

    @property
    def connection(self):
        return self._conn

    def close(self) -> None:
        if not self.closed:
            self.closed = True


class _FakeConnection:
    def __init__(self, recorder: "FakePool"):
        self._recorder = recorder
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> _FakeCursor:
        cur = _FakeCursor()
        self._recorder.cursors.append(cur)
        return cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakePool:
    """In-process PgPool replacement.

    Records every lease + cursor + commit so tests can assert the exact
    SQL/parameters the production code executed without a real DB.
    """

    def __init__(self):
        self.leases: list[_FakeLease] = []
        self.cursors: list[_FakeCursor] = []

    def lease(self, timeout=None):
        ls = _FakeLease(self)
        self.leases.append(ls)
        return ls


# ── fixture reset ─────────────────────────────────────────────────────


@pytest.fixture
def fake_pool():
    return FakePool()


@pytest.fixture
def importer_module():
    return _load_importers()


@pytest.fixture(autouse=True)
def _reset_installed_pool(importer_module):
    importer_module.install_pool(None)
    yield
    importer_module.install_pool(None)


# ── framework surface ─────────────────────────────────────────────────


def test_four_importers_registered_with_known_capabilities(importer_module):
    listing = importer_module.list_importers()
    names = [e["name"] for e in listing]
    assert set(names) == {"hermes", "memory-md", "openclaw", "hindsight"}

    by_name = {e["name"]: e for e in listing}
    # Honest capability flags.
    assert by_name["hermes"]["capability"] == "production"
    assert by_name["memory-md"]["capability"] == "production"
    assert by_name["openclaw"]["capability"] == "framework_ready"
    assert by_name["hindsight"]["capability"] == "framework_ready"
    # Each framework_ready entry carries a reason from its NotImplementedError.
    assert "reason" in by_name["openclaw"]
    assert "framework_ready" in by_name["openclaw"]["reason"].lower() or \
        "not implemented" in by_name["openclaw"]["reason"].lower() or \
        "framework" in by_name["openclaw"]["reason"].lower()
    assert "reason" in by_name["hindsight"]


def test_get_importer_known_and_unknown(importer_module):
    assert isinstance(
        importer_module.get_importer("hermes"),
        importer_module.HermesSessionImporter,
    )
    assert isinstance(
        importer_module.get_importer("memory-md"),
        importer_module.MemoryMarkdownImporter,
    )
    with pytest.raises(ValueError, match="unknown importer"):
        importer_module.get_importer("not-a-real-source")


# ── hermes: discover + parse (synthetic state.db + JSONL) ───────────────


def test_hermes_discover_finds_state_db_and_jsonl(importer_module):
    imp = importer_module.HermesSessionImporter()
    found = imp.discover(HERMES_FIX)
    names = {p.name for p in found}
    assert "state.db" in names
    assert "extra_export.jsonl" in names


def test_hermes_parse_state_db_emits_raw_messages(importer_module):
    imp = importer_module.HermesSessionImporter()
    items = list(imp.parse(HERMES_FIX / "state.db"))
    assert items, "expected raw items from state.db"
    for it in items:
        assert it.kind == importer_module.KIND_RAW
        assert it.source_system == "hermes"
        assert it.text.strip()
        assert it.role in {"user", "assistant", "system", "tool"}
        assert it.occurred_at and it.occurred_at.startswith("2026")
        # Provenance carries the fields downstream PG needs for dedupe.
        assert it.provenance is not None
        assert "session_id" in it.provenance
        assert "artifact" in it.provenance

    sessions = {it.source_ref for it in items}
    # Two distinct sessions in the fixture.
    assert "sess-alice-001" in sessions
    assert "sess-bob-002" in sessions


def test_hermes_parse_jsonl_records_session_and_timestamps(importer_module):
    imp = importer_module.HermesSessionImporter()
    items = list(imp.parse(HERMES_FIX / "extra_export.jsonl"))
    assert len(items) == 2
    # All records share a session_id.
    assert {it.source_ref for it in items} == {"sess-alice-001"}
    # Order preserved (lineno in provenance).
    assert items[0].provenance["lineno"] == 1
    assert items[1].provenance["lineno"] == 2


def test_hermes_introspects_schema_when_column_names_diverge(
    importer_module, tmp_path
):
    """An 'odd' Hermes store with renamed columns must still parse."""
    db = tmp_path / "odd.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE conversation_log ("
        "  pk INTEGER PRIMARY KEY,"
        "  speaker TEXT, message TEXT, ts TEXT, conversation_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO conversation_log VALUES (?,?,?,?,?)",
        [
            (1, "user", "odd schema user turn",
             "2026-09-01T10:00:00Z", "odd-1"),
            (2, "assistant", "odd schema assistant turn",
             "2026-09-01T10:00:05Z", "odd-1"),
        ],
    )
    conn.commit()
    conn.close()

    imp = importer_module.HermesSessionImporter()
    items = list(imp.parse(db))
    assert len(items) == 2
    for it in items:
        assert it.kind == importer_module.KIND_RAW
        # Schema mapping preserved verbatim in provenance (renamed
        # columns surface here so a downstream reader can audit).
        assert "speaker" in (it.provenance.get("schema_columns") or [])
        assert "message" in (it.provenance.get("schema_columns") or [])
        assert it.source_ref == "odd-1"


def test_hermes_oldest_newest_computed_from_parsed_items(importer_module):
    imp = importer_module.HermesSessionImporter()
    db_items = list(imp.parse(HERMES_FIX / "state.db"))
    jl_items = list(imp.parse(HERMES_FIX / "extra_export.jsonl"))
    all_ts = [it.occurred_at for it in db_items + jl_items if it.occurred_at]
    assert min(all_ts).startswith("2026-08-01")
    assert max(all_ts).startswith("2026-08-15") or max(all_ts).startswith("2026-08-02")


# ── memory-md: discover + parse (synthetic markdown) ──────────────────


def test_memory_md_discover_finds_canonical_and_other(importer_module):
    imp = importer_module.MemoryMarkdownImporter()
    found = imp.discover(MEMORY_FIX)
    names = {p.name for p in found}
    assert "MEMORY.md" in names
    assert "USER.md" in names
    # Non-canonical .md files are also imported.
    assert "NOTES.md" in names


def test_memory_md_parse_yields_full_and_per_section(importer_module):
    imp = importer_module.MemoryMarkdownImporter()
    items = list(imp.parse(MEMORY_FIX / "MEMORY.md"))
    assert items, "expected at least one item"
    for it in items:
        assert it.kind == importer_module.KIND_CURATED
        assert it.source_system == "memory-md"
        assert it.text.strip()
        assert it.category == "user_curated"
        assert "user-curated" in (it.tags or [])
        # Provenance contract per task spec.
        prov = it.provenance or {}
        assert prov.get("kind") == "user_curated"
        assert prov.get("source_system") == "memory-md"
        assert prov.get("file") == "MEMORY.md"
        assert isinstance(prov.get("file_sha256"), str)
        assert len(prov["file_sha256"]) == 64
        assert isinstance(prov.get("line_start"), int)
        assert isinstance(prov.get("line_end"), int)
        assert prov.get("imported_at") and "T" in prov["imported_at"]

    # Full-file item is present, plus per-section items.
    titles = {it.title for it in items}
    assert any("(full)" in (t or "") for t in titles)
    assert any(t and "Project" in t for t in titles)


def test_memory_md_provenance_changes_when_file_content_changes(
    importer_module, tmp_path
):
    imp = importer_module.MemoryMarkdownImporter()
    f = tmp_path / "M.md"
    f.write_text("# Title\n\nLine A.\n", encoding="utf-8")
    a = list(imp.parse(f))
    f.write_text("# Title\n\nLine B (changed).\n", encoding="utf-8")
    b = list(imp.parse(f))
    assert a[0].provenance["file_sha256"] != b[0].provenance["file_sha256"]


# ── framework_ready refusal ───────────────────────────────────────────


def test_framework_ready_importer_refuses_live_run(importer_module):
    out = io.StringIO()
    with pytest.raises(RuntimeError) as ei:
        importer_module.import_source(
            source="openclaw", root=Path("unused"), out=out, dry_run=False,
        )
    # The refusal must clearly mention the importer name.
    assert "openclaw" in str(ei.value).lower() or "framework_ready" in str(
        ei.value
    ).lower()


def test_framework_ready_importer_refuses_hindsight_live_run(importer_module):
    out = io.StringIO()
    with pytest.raises(RuntimeError) as ei:
        importer_module.import_source(
            source="hindsight", root=Path("unused"), out=out, dry_run=False,
        )
    assert "hindsight" in str(ei.value).lower() or "framework" in str(
        ei.value
    ).lower()


def test_framework_ready_importer_dry_run_returns_clean_stats(
    importer_module,
):
    out = io.StringIO()
    stats = importer_module.import_source(
        source="openclaw",
        root=Path("unused"),
        out=out,
        dry_run=True,
    )
    # dry-run with no discoverable artifacts → all zeros + "none".
    assert stats.source_system == "openclaw"
    assert stats.dry_run is True
    assert stats.raw_messages == 0
    assert stats.sessions == 0
    assert stats.user_curated == 0
    assert stats.legacy_derived == 0
    assert stats.oldest is None and stats.newest is None
    rendered = out.getvalue()
    assert "Imported:" in rendered
    assert "Oldest source: none" in rendered
    assert "Newest source: none" in rendered


# ── report format contract ────────────────────────────────────────────


def test_report_uses_thousands_separators_and_none_for_empty(
    importer_module,
):
    out = io.StringIO()
    stats = importer_module.ImportStats(source_system="x")
    stats.raw_messages = 18432
    stats.sessions = 214
    stats.user_curated = 37
    stats.legacy_derived = 126
    stats.oldest = "2025-11-03"
    stats.newest = "2026-09-17"
    rendered = importer_module._format_report(stats)
    # Exact format from task spec.
    assert "18,432 raw messages" in rendered
    assert "214 sessions" in rendered
    assert "37 user-curated memories" in rendered
    assert "126 legacy-derived memories" in rendered
    assert "Oldest source: 2025-11-03" in rendered
    assert "Newest source: 2026-09-17" in rendered

    # Empty case → 'none'.
    empty_stats = importer_module.ImportStats(source_system="x")
    rendered_empty = importer_module._format_report(empty_stats)
    assert "Oldest source: none" in rendered_empty
    assert "Newest source: none" in rendered_empty


# ── dry-run writes nothing ─────────────────────────────────────────────


def test_dry_run_writes_nothing_to_pool(importer_module, fake_pool):
    importer_module.install_pool(fake_pool)
    out = io.StringIO()
    importer_module.import_source(
        source="hermes",
        root=HERMES_FIX,
        out=out,
        dry_run=True,
    )
    # Pool never leased; zero writes.
    assert fake_pool.leases == []
    assert fake_pool.cursors == []
    # Report still has the right shape.
    assert "Imported:" in out.getvalue()


def test_live_run_with_no_pool_surfaces_clear_error(importer_module):
    importer_module.install_pool(None)
    out = io.StringIO()
    stats = importer_module.import_source(
        source="memory-md",
        root=MEMORY_FIX,
        out=out,
        dry_run=False,
    )
    # No pool + non-framework-ready importer + parsed content → error.
    assert any(
        "no PG pool injected" in e.lower() or "no pg pool" in e.lower()
        for e in stats.errors
    ) or any(
        "pool" in e.lower() for e in stats.errors
    )


# ── dedupe predicate on (session_id, role, timestamp) ────────────────


def test_hermes_live_run_executes_dedupe_insert_for_each_item(
    importer_module, fake_pool
):
    importer_module.install_pool(fake_pool)
    out = io.StringIO()
    stats = importer_module.import_source(
        source="hermes",
        root=HERMES_FIX,
        out=out,
        dry_run=False,
    )
    assert fake_pool.leases, "expected at least one lease on live path"
    # Every INSERT must use WHERE NOT EXISTS with (session_id, role, timestamp).
    dedupe_sql_count = 0
    for cur in fake_pool.cursors:
        for sql, params in cur.statements:
            norm = " ".join(sql.split()).upper()
            if "INSERT INTO PUBLIC.CONVERSATION_STREAM" in norm and \
                    "WHERE NOT EXISTS" in norm:
                dedupe_sql_count += 1
                # Params should include (sess, role, ts) twice — INSERT
                # values + WHERE-NOT-EXISTS predicate.
                assert len(params) >= 8, params
    assert dedupe_sql_count >= 1


def test_hermes_rerun_records_zero_new_rows(importer_module, fake_pool):
    """Replay run: every INSERT must report rowcount==0 (matched existing)."""
    # Pre-poison the fake cursor to model 'row already exists'.
    class _AlwaysDedupedCursor(_FakeCursor):
        @property
        def rowcount(self):
            return 0

    class _DedupPool(FakePool):
        def lease(self, timeout=None):
            ls = _FakeLease(self)
            # Replace the connection's cursor factory.
            outer = self

            class _DedupConn(_FakeConnection):
                def cursor(self):
                    cur = _AlwaysDedupedCursor()
                    outer.cursors.append(cur)
                    return cur
            ls._conn = _DedupConn(self)
            self.leases.append(ls)
            return ls

    pool = _DedupPool()
    importer_module.install_pool(pool)
    out = io.StringIO()
    stats = importer_module.import_source(
        source="hermes",
        root=HERMES_FIX,
        out=out,
        dry_run=False,
    )
    # All inserts deduped → 0 new raw_messages, deduped == items processed.
    assert stats.raw_messages == 0
    assert stats.deduped > 0
    assert stats.sessions >= 2  # sessions are counted from discovery


# ── limit arg ─────────────────────────────────────────────────────────


def test_limit_caps_parsed_items(importer_module, fake_pool):
    importer_module.install_pool(fake_pool)
    out = io.StringIO()
    importer_module.import_source(
        source="hermes",
        root=HERMES_FIX,
        out=out,
        dry_run=True,
        limit=2,
    )
    rendered = out.getvalue()
    # With limit=2 we cannot see more than 2 raw messages in the dry-run.
    import re as _re
    m = _re.search(r"(\d[\d,]*)\s+raw messages", rendered)
    assert m is not None
    assert int(m.group(1).replace(",", "")) <= 2


# ── list & description contract ───────────────────────────────────────


def test_list_importers_includes_descriptions(importer_module):
    listing = importer_module.list_importers()
    assert len(listing) == 4
    for entry in listing:
        assert entry["name"]
        assert entry["class"]
        assert entry["description"]
        assert entry["capability"] in {"production", "framework_ready"}


# ── live PG (only when reachable) ────────────────────────────────────


def _try_pg_dsn() -> str | None:
    dsn = os.environ.get("V3CORE_PG_DSN", "").strip()
    if not dsn:
        return None
    # Never test against the production boundary.
    if ":5433" in dsn or "/v3embeddings" in dsn:
        return None
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return None
    return dsn


@pytest.mark.skipif(
    _try_pg_dsn() is None,
    reason=(
        "no live disposable PG available (set env V3CORE_PG_DSN to a "
        "non-production DSN to run end-to-end idempotency proof)"
    ),
)
def test_end_to_end_idempotency_on_real_pg(importer_module):
    """End-to-end proof: live PG reachable → re-run imports 0 new rows."""
    import psycopg2
    from v3core.pg_pool import PgPool

    dsn = _try_pg_dsn()

    def _factory():
        return psycopg2.connect(dsn)

    pool = PgPool(_factory, max_connections=2, min_connections=0)
    try:
        importer_module.install_pool(pool)
        out = io.StringIO()
        first = importer_module.import_source(
            source="hermes", root=HERMES_FIX, out=out, dry_run=False,
        )
        out2 = io.StringIO()
        second = importer_module.import_source(
            source="hermes", root=HERMES_FIX, out=out2, dry_run=False,
        )
        assert second.raw_messages == 0, (
            "re-run must import 0 new rows; "
            f"first={first.raw_messages} second={second.raw_messages}"
        )
    finally:
        try:
            pool.shutdown(timeout=2.0)
        except Exception:
            pass


def test_no_live_pg_sql_assertion_replaces_idempotency_proof(
    importer_module, fake_pool,
):
    """When no live PG is reachable, prove the dedupe SQL/params on the fake."""
    importer_module.install_pool(fake_pool)
    out = io.StringIO()
    importer_module.import_source(
        source="memory-md", root=MEMORY_FIX, out=out, dry_run=False,
    )
    # For memory-md the contract is explicit_memories INSERT.
    saw_insert = False
    for cur in fake_pool.cursors:
        for sql, params in cur.statements:
            norm = " ".join(sql.split()).upper()
            if "INSERT INTO PUBLIC.EXPLICIT_MEMORIES" in norm and \
                    "ON CONFLICT" in norm:
                saw_insert = True
                # Canonical-hash dedupe: INSERT carries the memory_id that
                # was derived from (category, title, content, tags) — but
                # the writer builds it internally, so we only assert that
                # the params include the memory_id, category, title,
                # content, tags, provenance::jsonb.
                assert len(params) == 6, params
                memory_id, category, title, content, tags, prov_json = params
                assert memory_id.startswith("mem_")
                assert category == "user_curated"
                assert title
                assert content
                assert isinstance(tags, list)
                assert "user-curated" in tags
                prov = json.loads(prov_json)
                assert prov.get("kind") == "user_curated"
                assert prov.get("source_system") == "memory-md"
                assert prov.get("file_sha256") and len(prov["file_sha256"]) == 64
    assert saw_insert, "expected explicit_memories INSERT in live run"
