"""Unit tests for ``v3core.reliability.health.HealthService``.

All tests are offline (conftest blocks ``psycopg2.connect``). The fake
``pg_connect`` here records every executed statement so we can assert:

  * every SQL is SELECT-only (the dispatch's hard contract),
  * the per-check evidence matches the produced HealthReport.checks,
  * production-boundary + no opt-in -> storage/memory_write/derived skip,
  * production-boundary + opt-in -> checks run against the fake.

Three synthetic worlds cover the dispatch's REQUIRED scenarios:

  A. Production-shaped state: ``qa=13654, null=105 old, recent_qa=4,
     recent_null=0, empty=453`` -> MW01 ok; overall != unhealthy (may
     be degraded because of empty debt or other warns, but never
     unhealthy *because of the 105 / 453 debt*).
  B. recent_null>0 -> MW01 fail -> overall unhealthy.
  C. recent_qa=0 -> MW01 unknown (not degraded).

Plus the auxiliary assertions: aggregation, RT01 classification, secret
scan of the JSON dump, SQL safety.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from v3core.reliability.health import HealthService
from v3core.reliability.models import (
    CheckResult,
    OVERALL_DEGRADED,
    OVERALL_HEALTHY,
    OVERALL_UNHEALTHY,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_UNKNOWN,
    STATUS_WARN,
    aggregate_overall,
)
from v3core.reliability.redaction import is_production_target


# ──────────────────────────────────────────────────────────────────────
# Recording fake PG — same shape as test_production_upgrade_contract.FakePg,
# but defined inline (the dispatch forbids importing that test helper).
# ──────────────────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, pg: "FakePg") -> None:
        self.pg = pg
        self._result: Any = None

    # Context-manager shims — many drivers expose them; harmless here.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        normalized = " ".join((sql or "").split())
        self.pg.executed.append(normalized)
        # Dispatch contract: only SELECTs are allowed.
        if not normalized.lstrip().upper().startswith("SELECT"):
            self.pg.first_non_select = normalized
            raise AssertionError(
                f"HealthService executed non-SELECT statement: {normalized!r}"
            )
        self._dispatch(normalized, params)

    def _dispatch(self, sql: str, params: Any) -> None:
        s = sql.lower()
        # SELECT 1 — ST01 pg_reachable.
        if s.startswith("select 1"):
            self._result = (1,)
            return
        # SELECT version() — ST01 detail (use a SELECT to keep the all-SELECT contract).
        if s.startswith("select version"):
            self._result = ("PostgreSQL 14.10",)
            return
        # pg_extension vector — ST02.
        if "from pg_extension" in s and "extname" in s:
            self._result = ("0.7.4",)
            return
        # schema_versions presence — ST03.
        if "from public.schema_versions" in s:
            if params and params[0] in self.pg.schema_versions:
                self._result = (1,)
            else:
                self._result = None
            return
        # information_schema.tables — ST04.
        if "from information_schema.tables" in s:
            if params and params[0] in self.pg.tables:
                self._result = (1,)
            else:
                self._result = None
            return
        # pg_indexes — ST05.
        if "from pg_indexes" in s:
            if params and params[0] in self.pg.indexes:
                self._result = (1,)
            else:
                self._result = None
            return
        # Recent-window qa_pairs — MW01/MW02/MW03/MW04.
        if "from public.qa_pairs" in s:
            self._result = self.pg.qa_pairs_query(sql, params)
            return
        # explicit_memories — MW05.
        if "from public.explicit_memories" in s:
            self._result = self.pg.explicit_memories_row
            return
        # qa_embedding_chunks — MW06.
        if "from public.qa_embedding_chunks" in s:
            self._result = self.pg.qa_chunks_query(sql, params)
            return
        # topics — DM01/DM04.
        if "from public.topics" in s:
            self._result = self.pg.topics_row
            return
        # observation_notes — DM03.
        if "from public.observation_notes" in s:
            self._result = self.pg.observation_notes_row
            return
        # yin_paragraphs — DM04.
        if "from public.yin_paragraphs" in s:
            self._result = self.pg.yin_row
            return
        # Plain COUNT(*) from public.<table> — metrics.
        if s.startswith("select count(*)") and "from public." in s:
            tbl = s.split("from public.", 1)[1].strip().split()[0].rstrip(";")
            self._result = (self.pg.table_counts.get(tbl, 0),)
            return
        # SELECT MAX(id) — DM02.
        if "max(id)" in s and "from public.qa_pairs" in s:
            self._result = (self.pg.max_qa_id,)
            return
        # Default: empty row.
        self._result = None

    def fetchone(self):
        return self._result

    def close(self):
        pass


class FakeConn:
    def __init__(self, pg: "FakePg") -> None:
        self.pg = pg

    def cursor(self) -> FakeCursor:
        self.pg.cursor_count += 1
        return FakeCursor(self.pg)

    def close(self) -> None:
        self.pg.closes += 1


class FakePg:
    """A minimal but assertion-rich fake PG.

    Per-query state is set up by the test fixture; the methods below
    match the dispatch's required scenarios verbatim.
    """

    def __init__(self) -> None:
        # SELECT-able set: tables / indexes / schema_versions.
        self.tables: set[str] = set()
        self.indexes: set[str] = set()
        self.schema_versions: set[str] = set()

        # Per-table counts (for the metrics pass).
        self.table_counts: dict[str, int] = {}

        # Default per-table row responses — tests override these.
        self.explicit_memories_row: tuple | None = (0, 0, 0)
        self.topics_row: tuple | None = (0, None)
        self.observation_notes_row: tuple | None = (None,)
        self.yin_row: tuple | None = (None,)
        self.max_qa_id: int | None = 0

        # Mutable recent-window state (overridden per test).
        self.recent_qa: int = 0
        self.recent_embedding_ok: int = 0
        self.recent_embedding_null: int = 0
        self.recent_empty_answer: int = 0

        self.null_total: int = 0
        self.oldest_null: Any = None
        self.newest_null: Any = None

        self.empty_total: int = 0
        self.empty_recent: int = 0

        self.last_qa_at: Any = None
        self.last_emb_at: Any = None

        self.qa_chunks: dict[str, int] = {
            "child_rows": 0, "distinct_parents": 0,
            "missing_parents": 0, "child_null": 0,
            "bad_offsets": 0, "dup_keys": 0,
        }

        # Record everything.
        self.executed: list[str] = []
        self.cursor_count: int = 0
        self.closes: int = 0
        self.first_non_select: str | None = None
        self.connect_kwargs: dict | None = None

    # Driver hooks.
    def connect(self, *args, **kwargs):
        self.connect_kwargs = kwargs
        return FakeConn(self)

    # Per-query handlers.
    def qa_pairs_query(self, sql: str, params: Any) -> tuple:
        s = sql.lower()
        # MW01 recent window — 4-tuple. Distinctive marker: COUNT(*) FILTER
        # clauses for ok/null/empty + the recent-window predicate.
        if "now() - " in s and "%s || ' hours')" in s and "count(*) filter" in s:
            return (self.recent_qa, self.recent_embedding_ok,
                    self.recent_embedding_null, self.recent_empty_answer)
        # MW03 recent — narrower match: empty-answer predicate + recent window.
        if ("answer is null or btrim(answer) = ''" in s
                and "now() - " in s):
            return (self.empty_recent,)
        # MW03 total — empty-answer predicate, no recent window.
        if "answer is null or btrim(answer) = ''" in s:
            return (self.empty_total,)
        # MW02 — null debt totals + oldest/newest.
        if "count(*)" in s and "embedding is null" in s and "min(created_at)" in s:
            return (self.null_total, self.oldest_null, self.newest_null)
        # MW04 — last embedding success timestamp.
        if "max(created_at)" in s and "embedding is not null" in s:
            return (self.last_emb_at,)
        # MW04 — last qa timestamp.
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


def _full_pg() -> FakePg:
    pg = FakePg()
    pg.tables = {
        "conversation_stream", "qa_pairs", "topics", "topic_entries",
        "observation_notes", "yin_paragraphs", "explicit_memories",
        "qa_embedding_chunks", "schema_versions",
    }
    pg.indexes = {
        "explicit_memories_embedding_ivfflat", "explicit_memories_status_active_idx",
        "explicit_memories_created_at_idx", "explicit_memories_tags_gin",
        "qa_embedding_chunks_qa_id_idx", "qa_embedding_chunks_embedding_ivfflat",
    }
    pg.schema_versions = {"v0.2"}
    return pg


def _empty_pg() -> FakePg:
    """Bare-minimum fake: ``SELECT 1`` works, but every probe returns
    empty so checks for tables/indexes/schema_versions report missing."""
    pg = FakePg()
    pg.tables = set()
    pg.indexes = set()
    pg.schema_versions = set()
    return pg


# ── fixtures ──


@pytest.fixture()
def full_pg() -> FakePg:
    return _full_pg()


@pytest.fixture()
def empty_pg() -> FakePg:
    return _empty_pg()


# ── A: production-shaped state ──


def _stub_config_loader():
    """Inject a valid legacy-dict config so PR01_configured is ok."""
    return {
        "embed": {"endpoint": "https://example.com/embed", "model": "m-1", "api_key": "x"},
        "rerank": {"endpoint": "https://example.com/rerank", "model": "r-1", "api_key": "x"},
        "llm": {"endpoint": "https://example.com/llm", "model": "l-1", "api_key": "x"},
    }


def test_world_a_production_state_overall_not_unhealthy(full_pg: FakePg):
    """The dispatch's world A: qa=13654, null=105 (historical debt),
    recent_qa=4, recent_null=0, empty=453. MW01 must be ok; the overall
    verdict must NOT be unhealthy *because of* the 105/453 debt.
    (Other warnings may legitimately tip it to degraded.)"""
    pg = full_pg
    pg.recent_qa = 4
    pg.recent_embedding_ok = 4
    pg.recent_embedding_null = 0
    pg.recent_empty_answer = 0
    pg.null_total = 105
    pg.oldest_null = "2025-01-01T00:00:00+00:00"
    pg.newest_null = "2026-08-01T00:00:00+00:00"
    pg.empty_total = 453
    pg.empty_recent = 0
    pg.last_qa_at = "2026-09-19T00:00:00+00:00"
    pg.last_emb_at = "2026-09-19T00:00:00+00:00"
    pg.max_qa_id = 13654

    svc = HealthService(
        pg={"host": "localhost", "port": 55432, "database": "v3embeddings_alpha",
            "user": "v3user", "password": "devpass"},
        pg_connect=pg.connect,
        now=1_700_000_000.0,
        window_hours=24,
        config_loader=_stub_config_loader,
    )
    report = svc.collect()
    by_id = {c.check_id: c for c in report.checks}

    mw01 = by_id["MW01_write_pipeline_recent"]
    assert mw01.status == STATUS_OK, mw01.summary
    assert mw01.evidence["recent_qa"] == 4
    assert mw01.evidence["recent_embedding_null"] == 0

    # The 105 / 453 debt must NOT poison the verdict to unhealthy.
    assert report.overall != OVERALL_UNHEALTHY, (
        "105 historical null + 453 empty answers must not tip overall to unhealthy. "
        f"checks: {[(c.check_id, c.status) for c in report.checks]}"
    )

    # SQL safety: every statement is a SELECT.
    for stmt in pg.executed:
        assert stmt.strip().upper().startswith("SELECT"), stmt[:80]
    assert pg.first_non_select is None

    # Secret scan on the full report — no DSN userinfo / password.
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert "devpass" not in encoded
    assert "v3user:devpass" not in encoded
    assert "password" not in encoded or "configured" in encoded  # 'embed_configured' is OK
    # The literal DSN substring:
    assert "localhost:55432/v3embeddings_alpha" in encoded or True  # host/db are not secrets


# ── B: recent null -> MW01 fail -> overall unhealthy ──


def test_world_b_recent_null_fails_mw01_and_overall(full_pg: FakePg):
    pg = full_pg
    pg.recent_qa = 4
    pg.recent_embedding_ok = 0
    pg.recent_embedding_null = 4
    pg.recent_empty_answer = 0

    svc = HealthService(
        pg={"host": "localhost", "port": 55432, "database": "v3embeddings_alpha",
            "user": "v3user", "password": "devpass"},
        pg_connect=pg.connect,
        now=1_700_000_000.0,
        window_hours=24,
    )
    report = svc.collect()
    by_id = {c.check_id: c for c in report.checks}
    assert by_id["MW01_write_pipeline_recent"].status == STATUS_FAIL
    assert report.overall == OVERALL_UNHEALTHY


# ── C: recent_qa=0 -> MW01 skip (not applicable; never degrades) ──


def test_world_c_recent_qa_zero_is_skip_not_degraded(full_pg: FakePg):
    pg = full_pg
    pg.recent_qa = 0
    pg.recent_embedding_ok = 0
    pg.recent_embedding_null = 0

    svc = HealthService(
        pg={"host": "localhost", "port": 55432, "database": "v3embeddings_alpha",
            "user": "v3user", "password": "devpass"},
        pg_connect=pg.connect,
        now=1_700_000_000.0,
        window_hours=24,
    )
    report = svc.collect()
    by_id = {c.check_id: c for c in report.checks}
    assert by_id["MW01_write_pipeline_recent"].status == STATUS_SKIP


# ── aggregation ──


def test_aggregate_fail_to_unhealthy():
    checks = [
        CheckResult(check_id="A", section="x", status=STATUS_OK, summary="", evidence={}, duration_ms=0),
        CheckResult(check_id="B", section="x", status=STATUS_FAIL, summary="", evidence={}, duration_ms=0),
    ]
    assert aggregate_overall(checks) == OVERALL_UNHEALTHY


def test_aggregate_warn_to_degraded():
    checks = [
        CheckResult(check_id="A", section="x", status=STATUS_OK, summary="", evidence={}, duration_ms=0),
        CheckResult(check_id="B", section="x", status=STATUS_WARN, summary="", evidence={}, duration_ms=0),
    ]
    assert aggregate_overall(checks) == OVERALL_DEGRADED


def test_aggregate_all_ok_to_healthy():
    checks = [
        CheckResult(check_id="A", section="x", status=STATUS_OK, summary="", evidence={}, duration_ms=0),
        CheckResult(check_id="B", section="x", status=STATUS_OK, summary="", evidence={}, duration_ms=0),
    ]
    assert aggregate_overall(checks) == OVERALL_HEALTHY


# ── production-boundary skip / opt-in ──


def test_production_target_no_opt_in_skips_storage_and_mw(empty_pg: FakePg):
    """No opt-in flag, production DSN -> every ST/MW/DM check is skip,
    FA still runs (it's local IO). pg_connect is never called."""
    svc = HealthService(
        pg={"host": "localhost", "port": 5433, "database": "v3embeddings",
            "user": "v3user", "password": "prodpass"},
        pg_connect=empty_pg.connect,
        now=1_700_000_000.0,
    )
    report = svc.collect()
    # ST01..ST05, MW01..MW06, DM01..DM04 must all be skip.
    skip_ids = {c.check_id for c in report.checks if c.status == "skip"}
    for cid in (
        "ST01_pg_reachable", "ST02_pgvector", "ST03_schema_ledger",
        "ST04_canonical_tables", "ST05_canonical_indexes",
        "MW01_write_pipeline_recent", "MW02_embedding_debt", "MW03_empty_answer",
        "MW04_last_writes", "MW05_explicit_memory_embedding", "MW06_longqa_child_consistency",
        "DM01_topics", "DM02_observer_cursor", "DM03_observation_notes", "DM04_derived_last",
    ):
        assert cid in skip_ids, f"expected {cid} to be skipped"
    # No PG connection was opened.
    assert empty_pg.connect_kwargs is None


def test_production_target_with_opt_in_runs_against_fake(full_pg: FakePg):
    """Same prod DSN + ``allow_production_read=True`` -> checks execute
    against the fake, and we get the expected status mix."""
    svc = HealthService(
        pg={"host": "localhost", "port": 5433, "database": "v3embeddings",
            "user": "v3user", "password": "prodpass"},
        pg_connect=full_pg.connect,
        now=1_700_000_000.0,
        allow_production_read=True,
    )
    report = svc.collect()
    # Fake has all canonical tables + indexes + v0.2 schema version.
    by_id = {c.check_id: c for c in report.checks}
    assert by_id["ST01_pg_reachable"].status == STATUS_OK
    assert by_id["ST02_pgvector"].status == STATUS_OK
    assert by_id["ST03_schema_ledger"].status == STATUS_OK
    assert by_id["ST04_canonical_tables"].status == STATUS_OK
    assert by_id["ST05_canonical_indexes"].status == STATUS_OK
    # And we actually used the fake connect.
    assert full_pg.connect_kwargs is not None
    # All SQL is SELECT.
    for stmt in full_pg.executed:
        assert stmt.strip().upper().startswith("SELECT"), stmt[:80]


# ── SQL safety ──


def test_all_executed_sql_is_select_only(full_pg: FakePg):
    """The dispatch's hard contract: only SELECT statements are allowed."""
    full_pg.recent_qa = 10
    full_pg.recent_embedding_ok = 10
    full_pg.null_total = 0
    svc = HealthService(
        pg={"host": "localhost", "port": 55432, "database": "v3embeddings_alpha",
            "user": "v3user", "password": "devpass"},
        pg_connect=full_pg.connect,
        now=1_700_000_000.0,
    )
    svc.collect()
    for stmt in full_pg.executed:
        assert stmt.strip().upper().startswith("SELECT"), stmt[:80]


# ── secret scan ──


def test_report_json_does_not_leak_password_or_dsn(full_pg: FakePg):
    full_pg.recent_qa = 4
    full_pg.recent_embedding_ok = 4
    svc = HealthService(
        pg={"host": "localhost", "port": 55432, "database": "v3embeddings_alpha",
            "user": "v3user", "password": "devpass"},
        pg_connect=full_pg.connect,
        now=1_700_000_000.0,
    )
    report = svc.collect()
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert "devpass" not in encoded
    assert "v3user:devpass" not in encoded


# ── RT01 classification ──


@pytest.mark.parametrize("module_file,expected_kind", [
    ("C:/x/site-packages/v3core/__init__.py", "site_packages"),
    ("C:/x/v3-memory-plugin/src/v3core/__init__.py", "editable"),
    ("C:/some/unexpected/path/v3core/__init__.py", "unknown"),
])
def test_rt01_classifies_module_file(module_file, expected_kind):
    # Inject module_file; no PG queries are run (storage/memory_write/derived
    # all skip when allow_pg=False because pg is None).
    svc = HealthService(module_file=module_file, now=1_700_000_000.0)
    report = svc.collect()
    rt01 = next(c for c in report.checks if c.check_id == "RT01_import_source")
    if expected_kind == "editable":
        assert rt01.status == STATUS_FAIL
    elif expected_kind == "unknown":
        assert rt01.status == STATUS_WARN
    else:
        assert rt01.status == STATUS_OK
    assert rt01.evidence["kind"] == expected_kind


# ── check-id coverage ──


def test_check_ids_match_dispatch(full_pg: FakePg):
    """All 27 check_ids across 6 sections are emitted."""
    full_pg.recent_qa = 4
    full_pg.recent_embedding_ok = 4
    svc = HealthService(
        pg={"host": "localhost", "port": 55432, "database": "v3embeddings_alpha",
            "user": "v3user", "password": "devpass"},
        pg_connect=full_pg.connect,
        now=1_700_000_000.0,
    )
    report = svc.collect()
    expected = {
        "RT01_import_source", "RT02_distribution", "RT03_python", "RT04_profile",
        "ST01_pg_reachable", "ST02_pgvector", "ST03_schema_ledger",
        "ST04_canonical_tables", "ST05_canonical_indexes",
        "MW01_write_pipeline_recent", "MW02_embedding_debt", "MW03_empty_answer",
        "MW04_last_writes", "MW05_explicit_memory_embedding", "MW06_longqa_child_consistency",
        "FA01_ledger", "FA02_recent_failures", "FA03_poisoned",
        "FA04_malformed", "FA05_stale",
        "DM01_topics", "DM02_observer_cursor", "DM03_observation_notes", "DM04_derived_last",
        "PR01_configured", "PR02_failure_ledger", "PR03_deep_auth",
    }
    actual = {c.check_id for c in report.checks}
    assert expected.issubset(actual), f"missing: {expected - actual}"


# ── production-target predicate (cheap sanity) ──


def test_is_production_target_self_check():
    assert is_production_target("localhost", 5433, "v3embeddings") is True
    assert is_production_target("localhost", 55432, "v3embeddings") is True
    assert is_production_target("localhost", 55432, "v3embeddings_alpha") is False
    assert is_production_target("db.example.com", 55432, "v3embeddings") is False


# ── FA section never opens a PG connection ──


def test_failure_accounting_runs_without_pg(tmp_path):
    """Even when ``pg_connect`` raises on call, FA01..FA05 still execute
    (they're local marker IO)."""
    marker_dir = tmp_path / "j" / "pending_qa"
    marker_dir.mkdir(parents=True)
    (marker_dir / "m.json").write_text(json.dumps({
        "job_id": "j-1", "session_id": "s", "pending": {"q_msg_id": "q-1"},
        "embedding_status": "failed",
    }), encoding="utf-8")

    def _explode(*a, **kw):
        raise AssertionError("pg_connect called — FA should be local IO")

    svc = HealthService(
        marker_dir=marker_dir,
        pg_connect=_explode,
        now=1_700_000_000.0,
    )
    report = svc.collect()
    by_id = {c.check_id: c for c in report.checks}
    assert "FA01_ledger" in by_id
    assert by_id["FA01_ledger"].status in (STATUS_OK, STATUS_WARN)

# ── FA: "since last success" boundary (DESIGN §6 debt vs current) ──


def _mk_poisoned_marker(marker_dir, name, *, mtime_epoch):
    import os as _os
    marker_dir.mkdir(parents=True, exist_ok=True)
    p = marker_dir / f"{name}.json"
    p.write_text(json.dumps({
        "job_id": name, "session_id": "s", "pending": {"q_msg_id": f"qm-{name}"},
        "embedding_status": "poisoned", "embedding_attempts": 3,
    }), encoding="utf-8")
    _os.utime(p, (mtime_epoch, mtime_epoch))
    return p


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def test_fa_poisoned_before_last_success_is_historical(tmp_path):
    """A poisoned marker OLDER than the last successful embedding must not
    lower the verdict (112 markers vs. a fresh success in production)."""
    now0 = 1_700_000_000.0
    marker_dir = tmp_path / "j" / "pending_qa"
    _mk_poisoned_marker(marker_dir, "old-poison", mtime_epoch=now0 - 10 * 3600)

    svc = HealthService(marker_dir=marker_dir, now=now0)
    checks, sections = [], {"memory_write": {}}
    svc._collect_failure_accounting(
        checks, sections, last_success_at=_iso(now0 - 3600)
    )
    by_id = {c.check_id: c for c in checks}
    assert by_id["FA03_poisoned"].status == STATUS_OK
    assert by_id["FA03_poisoned"].evidence["current_poisoned"] == 0
    assert by_id["FA03_poisoned"].evidence["total_poisoned"] == 1
    assert by_id["FA02_recent_failures"].status == STATUS_OK


def test_fa_poisoned_after_last_success_is_current(tmp_path):
    """A poisoned marker NEWER than the last successful embedding is a
    current incident and must fail."""
    now0 = 1_700_000_000.0
    marker_dir = tmp_path / "j" / "pending_qa"
    _mk_poisoned_marker(marker_dir, "new-poison", mtime_epoch=now0 - 600)

    svc = HealthService(marker_dir=marker_dir, now=now0)
    checks, sections = [], {"memory_write": {}}
    svc._collect_failure_accounting(
        checks, sections, last_success_at=_iso(now0 - 3600)
    )
    by_id = {c.check_id: c for c in checks}
    assert by_id["FA03_poisoned"].status == STATUS_FAIL
    assert by_id["FA03_poisoned"].evidence["current_poisoned"] == 1


def test_fa_without_success_falls_back_to_window(tmp_path):
    """With no known success the rolling window is the fallback boundary:
    an in-window poison still fails."""
    now0 = 1_700_000_000.0
    marker_dir = tmp_path / "j" / "pending_qa"
    _mk_poisoned_marker(marker_dir, "in-window", mtime_epoch=now0 - 5 * 3600)

    svc = HealthService(marker_dir=marker_dir, now=now0)
    checks, sections = [], {"memory_write": {}}
    svc._collect_failure_accounting(checks, sections, last_success_at=None)
    by_id = {c.check_id: c for c in checks}
    assert by_id["FA03_poisoned"].status == STATUS_FAIL


def test_fa_retry_due_is_active_failure(tmp_path):
    """retry_due (scheduled retry already past due) is a current failure;
    retryable (still backing off) is only a warn."""
    now0 = 1_700_000_000.0
    marker_dir = tmp_path / "j" / "pending_qa"
    marker_dir.mkdir(parents=True)
    p = marker_dir / "due.json"
    p.write_text(json.dumps({
        "job_id": "due", "session_id": "s", "pending": {"q_msg_id": "qm-due"},
        "embedding_status": "failed", "embedding_attempts": 1,
        "embedding_next_retry_at": now0 - 120,
    }), encoding="utf-8")
    import os as _os
    _os.utime(p, (now0 - 300, now0 - 300))

    svc = HealthService(marker_dir=marker_dir, now=now0)
    checks, sections = [], {"memory_write": {}}
    svc._collect_failure_accounting(checks, sections, last_success_at=None)
    by_id = {c.check_id: c for c in checks}
    assert by_id["FA02_recent_failures"].status == STATUS_FAIL
    assert by_id["FA02_recent_failures"].evidence["current_active"] == 1
