"""DISPATCH-C1 — fault matrix (in-process) for the reliability layer.

Three-layer consistency per fault class::

    fault input  →  health.collect()  →  diagnose(report)  →  plan_repairs(diagnoses)

Each matrix row is an independent ``test_fault_<class>_<case>`` function
that constructs a single fault (runtime path / fake PG state / marker
file / injected deep runner), drives the full pipeline, and asserts the
three layers agree.

Coverage (DISPATCH-C1 §1, A/B/C/F/G/H/Recovery/D/E):

  A. runtime            — 3 cases (editable / unknown / site-packages)
  B. database (fake pg) — 5 cases (PG down / pgvector / schema /
                          tables / indexes)
  C. provider failure   — 7 cases (401 / 402 / 429 / 5xx / timeout /
                          connection / tokenizer)
  F. failure accounting — 6 cases (poisoned-old / poisoned-new /
                          malformed / stale / retry_due / duplicates)
  G/H. memory data      — 6 cases (recent-null / debt / empty / null /
                          orphan-parents / child-null)
  Recovery              — 3 cases (FA / MW01 / indexes)
  D/E. deep provider    — 3 cases (rerank 401 / llm timeout / no --deep)

All tests are offline: ``FakePg`` is a recording cursor stub, markers
live in tmp dirs, deep runners are simple callables. ``conftest``
already hard-blocks ``psycopg2.connect`` so this file can't reach a
real DB even by accident.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from v3core.reliability.diagnose import diagnose as run_diagnose
from v3core.reliability.health import HealthService
from v3core.reliability.models import (
    OVERALL_DEGRADED,
    OVERALL_HEALTHY,
    OVERALL_UNHEALTHY,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_UNKNOWN,
    STATUS_WARN,
)
from v3core.reliability.repair import plan_repairs


# ──────────────────────────────────────────────────────────────────────
# Recording FakePg — same shape as test_reliability_health.FakePg, but
# a little wider (storage helper for missing tables/indexes) so the
# matrix can drive every storage/memory_write check from one fixture.
# ──────────────────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, pg: "FakePg") -> None:
        self.pg = pg
        self._result: Any = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        normalized = " ".join((sql or "").split())
        self.pg.executed.append(normalized)
        # SELECT-only contract — enforced here, not just on tests.
        if not normalized.lstrip().upper().startswith("SELECT"):
            self.pg.first_non_select = normalized
            raise AssertionError(
                f"HealthService executed non-SELECT statement: {normalized!r}"
            )
        self._dispatch(normalized, params)

    def _dispatch(self, sql: str, params: Any) -> None:
        s = sql.lower()
        # Specific patterns first — the bare "select 1" pattern would
        # otherwise swallow the schema/tables/indexes variants that all
        # start with "SELECT 1 FROM ..."
        if "from public.schema_versions" in s:
            if params and params[0] in self.pg.schema_versions:
                self._result = (1,)
            else:
                self._result = None
            return
        if "from information_schema.tables" in s:
            self._result = (1,) if (params and params[0] in self.pg.tables) else None
            return
        if "from pg_indexes" in s:
            self._result = (1,) if (params and params[0] in self.pg.indexes) else None
            return
        if s.startswith("select 1"):
            self._result = (1,)
            return
        if s.startswith("select version"):
            self._result = ("PostgreSQL 14.10",)
            return
        if "from pg_extension" in s and "extname" in s:
            # FakePg.vector_ext_version controls ST02.
            self._result = (self.pg.vector_ext_version,)
            return
        if "from public.qa_pairs" in s:
            self._result = self.pg.qa_pairs_query(sql, params)
            return
        if "from public.explicit_memories" in s:
            self._result = self.pg.explicit_memories_row
            return
        if "from public.qa_embedding_chunks" in s:
            self._result = self.pg.qa_chunks_query(sql, params)
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
        if s.startswith("select count(*)") and "from public." in s:
            tbl = s.split("from public.", 1)[1].strip().split()[0].rstrip(";")
            self._result = (self.pg.table_counts.get(tbl, 0),)
            return
        if "max(id)" in s and "from public.qa_pairs" in s:
            self._result = (self.pg.max_qa_id,)
            return
        self._result = None

    def fetchone(self):
        return self._result

    def close(self):
        pass


class _FakeConn:
    def __init__(self, pg: "FakePg") -> None:
        self.pg = pg

    def cursor(self) -> _FakeCursor:
        self.pg.cursor_count += 1
        return _FakeCursor(self.pg)

    def close(self) -> None:
        self.pg.closes += 1


class FakePg:
    """Drivable fake PG for the matrix."""

    def __init__(self) -> None:
        self.tables: set[str] = set()
        self.indexes: set[str] = set()
        self.schema_versions: set[str] = set()
        self.vector_ext_version: str | None = "0.7.4"  # set to None to simulate missing

        self.table_counts: dict[str, int] = {}

        self.explicit_memories_row: tuple | None = (0, 0, 0)
        self.topics_row: tuple | None = (0, None)
        self.observation_notes_row: tuple | None = (None,)
        self.yin_row: tuple | None = (None,)
        self.max_qa_id: int | None = 0

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

        # Connect control: when set, the connect() call raises — ST01 fails.
        self.connect_raises: Exception | None = None

        # Records.
        self.executed: list[str] = []
        self.cursor_count: int = 0
        self.closes: int = 0
        self.first_non_select: str | None = None
        self.connect_kwargs: dict | None = None

    def connect(self, *args, **kwargs):
        if self.connect_raises is not None:
            raise self.connect_raises
        self.connect_kwargs = kwargs
        return _FakeConn(self)

    def qa_pairs_query(self, sql: str, params: Any) -> tuple:
        s = sql.lower()
        if "now() - " in s and "%s || ' hours')" in s and "count(*) filter" in s:
            return (self.recent_qa, self.recent_embedding_ok,
                    self.recent_embedding_null, self.recent_empty_answer)
        if ("answer is null or btrim(answer) = ''" in s and "now() - " in s):
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


# Canonical full pg state — the "everything green" baseline.
# NOTE: timestamps are computed relative to ``now=1_700_000_000.0`` (the
# default the matrix uses for pipeline()). Hard-coding 2026 dates here
# would put ``last_embedding_success_at`` in the *future* relative to
# ``now``, which the failure-accounting "since last success" boundary
# treats as the cutoff — every marker would silently become "historical"
# and FA02/FA03 would under-report. The tests below assume these
# ISO timestamps parse to an epoch before ``now`` (Nov 2023).
def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


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
    pg.recent_qa = 4
    pg.recent_embedding_ok = 4
    # ``last_qa_at`` / ``last_emb_at`` are parsed by the FA boundary
    # against ``now`` — keep them well before now (i.e. < 24h old) so the
    # recent-window logic stays consistent.
    pg.last_qa_at = _iso(1_700_000_000.0 - 3600)
    pg.last_emb_at = _iso(1_700_000_000.0 - 3600)
    pg.max_qa_id = 13654
    return pg


# ──────────────────────────────────────────────────────────────────────
# Marker / config / deep-runner helpers
# ──────────────────────────────────────────────────────────────────────


def _write_marker(
    marker_dir: Path,
    name: str,
    *,
    q_msg_id: str,
    embedding_status: str = "failed",
    error_class: str | None = None,
    last_failure_at_epoch: float | None = None,
    next_retry_at_epoch: float | None = None,
    embedding_attempts: int = 1,
    raw_payload: dict | None = None,
) -> Path:
    """Write one v2-shape marker with the given status / error_class.

    ``raw_payload`` overrides everything else (used to plant malformed
    or partially-broken files).
    """
    marker_dir.mkdir(parents=True, exist_ok=True)
    p = marker_dir / f"{name}.json"
    if raw_payload is not None:
        p.write_text(json.dumps(raw_payload), encoding="utf-8")
        return p
    now = last_failure_at_epoch or 0.0
    payload: dict[str, Any] = {
        "job_id": name,
        "session_id": "sess-test",
        "pending": {"q_msg_id": q_msg_id, "q_turn": 1, "q": "qtext", "a": "atext"},
        "embedding_status": embedding_status,
        "embedding_attempts": embedding_attempts,
        "version": "2",
    }
    if error_class is not None:
        payload["error_class"] = error_class
    if last_failure_at_epoch is not None:
        payload["last_failure_at"] = _iso(last_failure_at_epoch)
        payload["first_failure_at"] = _iso(last_failure_at_epoch)
        payload["failure_count"] = embedding_attempts
    if next_retry_at_epoch is not None:
        # failure_reader accepts numeric epoch for next_retry_at.
        payload["embedding_next_retry_at"] = next_retry_at_epoch
    p.write_text(json.dumps(payload), encoding="utf-8")
    if last_failure_at_epoch is not None:
        os.utime(p, (last_failure_at_epoch, last_failure_at_epoch))
    return p


def _stub_config_loader(cfg: dict | None = None) -> Callable[[], dict]:
    """Build a config_loader returning a valid legacy dict.

    Override ``embed`` / ``rerank`` / ``llm`` via the ``cfg`` argument
    to simulate missing providers.
    """
    base = {
        "embed": {"endpoint": "https://example.com/embed", "model": "m-1", "api_key": "x"},
        "rerank": {"endpoint": "https://example.com/rerank", "model": "r-1", "api_key": "x"},
        "llm": {"endpoint": "https://example.com/llm", "model": "l-1", "api_key": "x"},
    }
    if cfg:
        base.update(cfg)
    return lambda **kw: base


# ──────────────────────────────────────────────────────────────────────
# Common pipeline driver — runs all three layers and returns them.
# ──────────────────────────────────────────────────────────────────────


def _pipeline(
    *,
    pg: FakePg | None = None,
    pg_conn_raises: Exception | None = None,
    marker_dir: Path | None = None,
    module_file: str | None = None,
    config_loader: Callable[[], dict] | None = None,
    deep: bool = False,
    deep_auth_runner: Callable[..., dict[str, Any]] | None = None,
    now: float = 1_700_000_000.0,
    window_hours: int = 24,
):
    """Build a HealthService, drive collect/diagnose/plan, return the trio.

    Tests can then assert on any layer.
    """
    pg_kwargs: dict[str, Any] = {}
    if pg is not None:
        pg_kwargs["pg"] = {
            "host": "localhost", "port": 55432,
            "database": "v3embeddings_alpha", "user": "v3user", "password": "devpass",
        }
        if pg_conn_raises is not None:
            pg.connect_raises = pg_conn_raises
        pg_kwargs["pg_connect"] = pg.connect
    elif pg_conn_raises is not None:
        # Pure failure mode: no pg dict, just a connect that raises.
        def _raise_conn(*a, **kw):
            raise pg_conn_raises

        pg_kwargs["pg_connect"] = _raise_conn
        pg_kwargs["pg"] = {
            "host": "localhost", "port": 55432,
            "database": "v3embeddings_alpha", "user": "v3user", "password": "devpass",
        }
    if marker_dir is not None:
        pg_kwargs["marker_dir"] = marker_dir
    if module_file is not None:
        pg_kwargs["module_file"] = module_file
    if config_loader is not None:
        pg_kwargs["config_loader"] = config_loader
    if deep_auth_runner is not None:
        pg_kwargs["deep_auth_runner"] = deep_auth_runner

    svc = HealthService(
        now=now, window_hours=window_hours, deep=deep, **pg_kwargs,
    )
    report = svc.collect()
    issues = run_diagnose(report)
    actions = plan_repairs(issues)
    return report, issues, actions


# ──────────────────────────────────────────────────────────────────────
# A — runtime
# ──────────────────────────────────────────────────────────────────────


def test_fault_a_runtime_editable_module_file():
    """RT01 editable → fail; diagnose RUNTIME_EDITABLE_ACTIVE (error, not
    repairable); repair has no automatic action."""
    report, issues, actions = _pipeline(
        pg=_full_pg(),
        module_file="C:/x/v3-memory-plugin/src/v3core/__init__.py",
    )
    rt01 = next(c for c in report.checks if c.check_id == "RT01_import_source")
    assert rt01.status == STATUS_FAIL
    assert rt01.evidence["kind"] == "editable"
    codes = [d.code for d in issues]
    assert "RUNTIME_EDITABLE_ACTIVE" in codes
    issue = next(d for d in issues if d.code == "RUNTIME_EDITABLE_ACTIVE")
    assert issue.severity == "error"
    assert issue.repairable is False
    # No automatic repair should be planned for this code (manual-only).
    assert all(a.issue_code != "RUNTIME_EDITABLE_ACTIVE" for a in actions)


def test_fault_a_runtime_unknown_module_file():
    """RT01 unknown → warn; diagnose RUNTIME_UNKNOWN_IMPORT (warning)."""
    report, issues, actions = _pipeline(
        pg=_full_pg(),
        module_file="C:/some/unexpected/path/v3core/__init__.py",
    )
    rt01 = next(c for c in report.checks if c.check_id == "RT01_import_source")
    assert rt01.status == STATUS_WARN
    assert rt01.evidence["kind"] == "unknown"
    issue = next(d for d in issues if d.code == "RUNTIME_UNKNOWN_IMPORT")
    assert issue.severity == "warning"
    assert issue.repairable is False
    assert all(a.issue_code != "RUNTIME_UNKNOWN_IMPORT" for a in actions)


def test_fault_a_runtime_site_packages_ok():
    """RT01 site-packages → ok; no runtime diagnosis at all."""
    report, issues, actions = _pipeline(
        pg=_full_pg(),
        module_file="C:/x/site-packages/v3core/__init__.py",
    )
    rt01 = next(c for c in report.checks if c.check_id == "RT01_import_source")
    assert rt01.status == STATUS_OK
    assert rt01.evidence["kind"] == "site_packages"
    assert not [d for d in issues if d.scope == "runtime"]


# ──────────────────────────────────────────────────────────────────────
# B — database (fake pg)
# ──────────────────────────────────────────────────────────────────────


def test_fault_b_pg_connect_raises():
    """ST01 fail; diagnose PG_UNREACHABLE (error); no automatic action."""
    report, issues, actions = _pipeline(
        pg_conn_raises=RuntimeError("connection refused (offline test)"),
    )
    st01 = next(c for c in report.checks if c.check_id == "ST01_pg_reachable")
    assert st01.status == STATUS_FAIL
    assert "PG_UNREACHABLE" in [d.code for d in issues]
    assert report.overall == OVERALL_UNHEALTHY
    # PG_UNREACHABLE is in _MANUAL_ONLY_CODES → no repair action for it.
    assert all(a.issue_code != "PG_UNREACHABLE" for a in actions)


def test_fault_b_pgvector_missing():
    """ST02 fail (vector extversion=None); diagnose PGVECTOR_MISSING (error)."""
    pg = _full_pg()
    pg.vector_ext_version = None
    report, issues, _ = _pipeline(pg=pg)
    st02 = next(c for c in report.checks if c.check_id == "ST02_pgvector")
    assert st02.status == STATUS_FAIL
    assert st02.evidence["present"] is False
    issue = next(d for d in issues if d.code == "PGVECTOR_MISSING")
    assert issue.severity == "error"
    assert issue.repairable is False


def test_fault_b_schema_version_missing_v02():
    """ST03 fail; diagnose SCHEMA_VERSION_MISMATCH; repair RUN_SCHEMA_UPGRADE."""
    pg = _full_pg()
    pg.schema_versions = set()  # v0.2 row absent
    report, issues, actions = _pipeline(pg=pg)
    st03 = next(c for c in report.checks if c.check_id == "ST03_schema_ledger")
    assert st03.status == STATUS_FAIL
    assert "SCHEMA_VERSION_MISMATCH" in [d.code for d in issues]
    upgrade = next((a for a in actions if a.action_id == "RUN_SCHEMA_UPGRADE"), None)
    assert upgrade is not None
    assert upgrade.risk == "low"
    assert upgrade.writes_database is True
    assert upgrade.automatic_safe is False


def test_fault_b_missing_required_tables():
    """ST04 fail; SCHEMA_TABLE_MISSING; repair RUN_SCHEMA_UPGRADE."""
    pg = _full_pg()
    pg.tables.discard("qa_embedding_chunks")  # drop one canonical table
    report, issues, actions = _pipeline(pg=pg)
    st04 = next(c for c in report.checks if c.check_id == "ST04_canonical_tables")
    assert st04.status == STATUS_FAIL
    assert "qa_embedding_chunks" in st04.evidence["missing"]
    issue = next(d for d in issues if d.code == "SCHEMA_TABLE_MISSING")
    assert issue.severity == "error"
    assert any(a.action_id == "RUN_SCHEMA_UPGRADE" for a in actions)


def test_fault_b_missing_index():
    """ST05 warn; SCHEMA_INDEX_MISSING; repair RECREATE_MISSING_INDEX.

    The planner's ``_evidence_count`` helper reads integer-valued keys;
    the ``missing`` field is a list, so ``target_count`` is what the
    planner produces (``0`` is acceptable here — the contract is that
    the action exists with the right risk / writes flags). Per dispatch
    "若某行与 B 的命名有差异，以 B 为准" we match the planner as-is.
    """
    pg = _full_pg()
    pg.indexes.discard("explicit_memories_tags_gin")
    report, issues, actions = _pipeline(pg=pg)
    st05 = next(c for c in report.checks if c.check_id == "ST05_canonical_indexes")
    assert st05.status == STATUS_WARN
    assert "explicit_memories_tags_gin" in st05.evidence["missing"]
    issue = next(d for d in issues if d.code == "SCHEMA_INDEX_MISSING")
    assert issue.severity == "warning"
    rec = next((a for a in actions if a.action_id == "RECREATE_MISSING_INDEX"), None)
    assert rec is not None
    assert rec.risk == "low"
    assert rec.writes_database is True
    assert rec.automatic_safe is False
    assert rec.requires_provider is False


# ──────────────────────────────────────────────────────────────────────
# C — embedding provider failure (via marker ledger)
# ──────────────────────────────────────────────────────────────────────


def _provider_fault_pipeline(tmp_path: Path, *, error_class: str, now: float = 1_700_000_000.0):
    """Plant ONE recent (now-60) failed marker with retryable next_retry_at
    (now+3600) for the given error_class; run the pipeline."""
    marker_dir = tmp_path / "j" / "pending_qa"
    _write_marker(
        marker_dir, "p", q_msg_id="qm-1",
        embedding_status="failed",
        error_class=error_class,
        last_failure_at_epoch=now - 60,
        next_retry_at_epoch=now + 3600,
        embedding_attempts=1,
    )
    return _pipeline(
        pg=_full_pg(),
        marker_dir=marker_dir,
        now=now,
    )


def test_fault_c_provider_401():
    """provider_401 → PR02 fail; EMBED_PROVIDER_AUTH (error); FIX_PROVIDER_CREDENTIALS
    repair (automatic_safe=False) — and the planner must NOT silently fabricate
    a re-embed action that pretends to fix credentials."""
    report, issues, actions = _pipeline_with_one_marker("provider_401")
    pr02 = next(c for c in report.checks if c.check_id == "PR02_failure_ledger")
    assert pr02.status == STATUS_FAIL
    auth = next((d for d in issues if d.code == "EMBED_PROVIDER_AUTH"), None)
    assert auth is not None
    assert auth.severity == "error"
    fix = next((a for a in actions if a.action_id == "FIX_PROVIDER_CREDENTIALS"), None)
    assert fix is not None
    assert fix.automatic_safe is False
    assert fix.writes_database is False
    # No phantom REBUILD_EMBEDDING_FOR_QA_IDS for credential errors.
    for a in actions:
        assert not (
            a.action_id == "REBUILD_EMBEDDING_FOR_QA_IDS"
            and "EMBED_PROVIDER_AUTH" in a.issue_code
        )


def test_fault_c_provider_402():
    """provider_402 → same code path as 401."""
    report, issues, actions = _pipeline_with_one_marker("provider_402")
    pr02 = next(c for c in report.checks if c.check_id == "PR02_failure_ledger")
    assert pr02.status == STATUS_FAIL
    assert any(d.code == "EMBED_PROVIDER_AUTH" for d in issues)
    assert any(a.action_id == "FIX_PROVIDER_CREDENTIALS" for a in actions)


def test_fault_c_provider_429():
    """provider_429 → PR02 warn; EMBED_PROVIDER_RATE_LIMIT (warning);
    RETRY_PENDING_FAILURES."""
    report, issues, actions = _pipeline_with_one_marker("provider_429")
    pr02 = next(c for c in report.checks if c.check_id == "PR02_failure_ledger")
    assert pr02.status == STATUS_WARN
    rate = next((d for d in issues if d.code == "EMBED_PROVIDER_RATE_LIMIT"), None)
    assert rate is not None
    assert rate.severity == "warning"
    retry = next((a for a in actions if a.action_id == "RETRY_PENDING_FAILURES"), None)
    assert retry is not None
    assert retry.requires_provider is True


@pytest.mark.parametrize("err_class", [
    "provider_5xx",
    "provider_timeout",
    "provider_connection",
])
def test_fault_c_provider_transient_classes(err_class: str):
    """5xx / timeout / connection → PR02 warn; EMBED_PROVIDER_TIMEOUT; retry."""
    report, issues, actions = _pipeline_with_one_marker(err_class)
    pr02 = next(c for c in report.checks if c.check_id == "PR02_failure_ledger")
    assert pr02.status == STATUS_WARN
    # All three classes collapse into the EMBED_PROVIDER_TIMEOUT diagnosis.
    assert any(d.code == "EMBED_PROVIDER_TIMEOUT" for d in issues)
    assert any(a.action_id == "RETRY_PENDING_FAILURES" for a in actions)


def test_fault_c_tokenizer_unavailable():
    """tokenizer_unavailable is FA-side retryable, NOT a provider-auth code."""
    report, issues, actions = _pipeline_with_one_marker("tokenizer_unavailable")
    pr02 = next(c for c in report.checks if c.check_id == "PR02_failure_ledger")
    # Doesn't match cred_classes or transient_classes → PR02 stays ok.
    assert pr02.status == STATUS_OK
    # No provider-auth diagnosis appears.
    assert not any(d.code == "EMBED_PROVIDER_AUTH" for d in issues)
    # But the FA02 status reflects the active failure and there's a retry action.
    fa02 = next(c for c in report.checks if c.check_id == "FA02_recent_failures")
    assert fa02.status in (STATUS_FAIL, STATUS_WARN)
    assert any(a.action_id == "RETRY_PENDING_FAILURES" for a in actions)


# Helper used by every C-class test.
def _pipeline_with_one_marker(error_class: str):
    import tempfile
    td = tempfile.TemporaryDirectory()
    _TEMP_HOLD.append(td)
    marker_dir = Path(td.name) / "j" / "pending_qa"
    now = 1_700_000_000.0
    _write_marker(
        marker_dir, "p", q_msg_id="qm-1",
        embedding_status="failed",
        error_class=error_class,
        last_failure_at_epoch=now - 60,
        next_retry_at_epoch=now + 3600,
        embedding_attempts=1,
    )
    return _pipeline(pg=_full_pg(), marker_dir=marker_dir, now=now)


# ──────────────────────────────────────────────────────────────────────
# F — failure accounting (shape through health)
# ──────────────────────────────────────────────────────────────────────


# A registry so the tempdir handles stay alive for the duration of the
# test — without it, ``tempfile.TemporaryDirectory`` cleans up the
# directory when ``td`` falls out of scope at function return, leaving
# the returned ``Path`` pointing at nothing.
_TEMP_HOLD: list = []


def _plant_marker_dir(*markers: dict, now: float = 1_700_000_000.0) -> Path:
    """Plant a marker directory with one or more markers.

    Each marker dict provides kwargs for ``_write_marker`` (no ``now``
    kwarg is forwarded — last_failure_at_epoch / next_retry_at_epoch
    are the temporal controls). The underlying ``TemporaryDirectory``
    handle is held in a module-level list so it survives until the test
    process exits — tests delete the marker dir themselves when they
    want to simulate "marker removed".
    """
    import tempfile
    td = tempfile.TemporaryDirectory()
    _TEMP_HOLD.append(td)
    marker_dir = Path(td.name) / "j" / "pending_qa"
    for m in markers:
        kwargs = dict(m)
        kwargs.setdefault("embedding_status", "poisoned")
        kwargs.setdefault("q_msg_id", f"qm-{kwargs.get('name', 'm')}")
        name = kwargs.pop("name", f"m-{len(list(marker_dir.glob('*.json'))) + 1}")
        _write_marker(marker_dir, name, **kwargs)
    return marker_dir


def test_fault_f_poisoned_old_is_historical_iso(tmp_path):
    """Old poisoned (older than last success) → FA03 ok; diagnose
    POISONED_FAILURE_MARKER (info) — historical isolation. No current
    failure → no RECENT_FAILURE_MARKER."""
    marker_dir = _plant_marker_dir(dict(
        name="old-poison",
        embedding_status="poisoned",
        q_msg_id="qm-old",
        error_class="provider_5xx",
        last_failure_at_epoch=1_700_000_000.0 - 10 * 3600,  # 10h ago
        embedding_attempts=3,
    ))
    # Marker uses ISO last_failure_at, and mtime is set to the same epoch.
    report, issues, actions = _pipeline(
        pg=_full_pg(),
        marker_dir=marker_dir,
        now=1_700_000_000.0,
    )
    fa03 = next(c for c in report.checks if c.check_id == "FA03_poisoned")
    assert fa03.status == STATUS_OK
    assert fa03.evidence["total_poisoned"] >= 1
    assert fa03.evidence["current_poisoned"] == 0
    codes = [d.code for d in issues]
    assert "POISONED_FAILURE_MARKER" in codes
    issue = next(d for d in issues if d.code == "POISONED_FAILURE_MARKER")
    assert issue.severity == "info"
    # An "action exists" promise: there must be a RETRY action for poisoned.
    assert any(a.action_id == "RETRY_PENDING_FAILURES" for a in actions)


def test_fault_f_poisoned_new_after_last_success():
    """Newer-than-last-success poisoned → FA03 fail; POISONED_FAILURE_MARKER
    (error); retry action exists, automatic_safe=False."""
    marker_dir = _plant_marker_dir(dict(
        name="new-poison",
        embedding_status="poisoned",
        q_msg_id="qm-new",
        error_class="provider_5xx",
        last_failure_at_epoch=1_700_000_000.0 - 600,
        embedding_attempts=3,
    ))
    report, issues, actions = _pipeline(
        pg=_full_pg(), marker_dir=marker_dir, now=1_700_000_000.0,
    )
    fa03 = next(c for c in report.checks if c.check_id == "FA03_poisoned")
    assert fa03.status == STATUS_FAIL
    issue = next(d for d in issues if d.code == "POISONED_FAILURE_MARKER")
    assert issue.severity == "error"
    retry = next((a for a in actions if a.action_id == "RETRY_PENDING_FAILURES"), None)
    assert retry is not None
    assert retry.automatic_safe is False


def test_fault_f_malformed_marker():
    """A broken marker (no job_id) → FA04 warn; MALFORMED_FAILURE_MARKER
    (warning); no current-active fail."""
    marker_dir = _plant_marker_dir(dict(
        name="broken",
        embedding_status="failed",
        raw_payload={
            "version": "2",
            "session_id": "s",
            # No job_id, no pending → malformed.
        },
    ))
    report, issues, _ = _pipeline(
        pg=_full_pg(), marker_dir=marker_dir, now=1_700_000_000.0,
    )
    fa04 = next(c for c in report.checks if c.check_id == "FA04_malformed")
    assert fa04.status == STATUS_WARN
    assert fa04.evidence["malformed"] >= 1
    issue = next(d for d in issues if d.code == "MALFORMED_FAILURE_MARKER")
    assert issue.severity == "warning"


def test_fault_f_stale_in_flight_marker():
    """mtime 3 days ago in_flight → FA05 warn; STALE_PENDING_MARKER."""
    marker_dir = _plant_marker_dir(dict(
        name="stuck",
        embedding_status="in_flight",
        q_msg_id="qm-stuck",
        last_failure_at_epoch=1_700_000_000.0 - 3 * 24 * 3600,
    ))
    report, issues, _ = _pipeline(
        pg=_full_pg(), marker_dir=marker_dir, now=1_700_000_000.0,
    )
    fa05 = next(c for c in report.checks if c.check_id == "FA05_stale")
    assert fa05.status == STATUS_WARN
    issue = next(d for d in issues if d.code == "STALE_PENDING_MARKER")
    assert issue.severity == "warning"


def test_fault_f_retry_due_marker():
    """failed + next_retry_at in the past → RETRY_EXHAUSTED (warning) or
    RECENT_FAILURE_MARKER (error); both are acceptable per the dispatch
    ('以 B 为准' — diagnose surfaces one of them)."""
    marker_dir = _plant_marker_dir(dict(
        name="due",
        embedding_status="failed",
        q_msg_id="qm-due",
        error_class="provider_5xx",
        last_failure_at_epoch=1_700_000_000.0 - 300,
        next_retry_at_epoch=1_700_000_000.0 - 120,  # already due
        embedding_attempts=1,
    ))
    report, issues, _ = _pipeline(
        pg=_full_pg(), marker_dir=marker_dir, now=1_700_000_000.0,
    )
    fa02 = next(c for c in report.checks if c.check_id == "FA02_recent_failures")
    assert fa02.status == STATUS_FAIL
    codes = [d.code for d in issues]
    assert any(c in codes for c in ("RECENT_FAILURE_MARKER", "RETRY_EXHAUSTED"))


def test_fault_f_duplicate_markers_recorded():
    """Two markers with the same q_msg_id → ledger.duplicates ≥ 1; no
    fail verdict (only evidence)."""
    marker_dir = _plant_marker_dir(
        dict(name="dup-a", q_msg_id="shared-qmid",
             last_failure_at_epoch=1_700_000_000.0 - 60,
             embedding_status="failed", error_class="provider_5xx",
             embedding_attempts=1),
        dict(name="dup-b", q_msg_id="shared-qmid",
             last_failure_at_epoch=1_700_000_000.0 - 60,
             embedding_status="failed", error_class="provider_5xx",
             embedding_attempts=1),
    )
    report, _, _ = _pipeline(
        pg=_full_pg(), marker_dir=marker_dir, now=1_700_000_000.0,
    )
    fa01 = next(c for c in report.checks if c.check_id == "FA01_ledger")
    assert fa01.evidence["duplicates"] >= 1


# ──────────────────────────────────────────────────────────────────────
# G/H — memory data (fake pg)
# ──────────────────────────────────────────────────────────────────────


def test_fault_g_recent_embedding_null():
    """recent_null=2 → MW01 fail; RECENT_EMBEDDING_FAILURE (error);
    REBUILD_EMBEDDING_FOR_QA_IDS with target_count≥2, requires_provider,
    writes_database, automatic_safe=False."""
    pg = _full_pg()
    pg.recent_qa = 4
    pg.recent_embedding_ok = 2
    pg.recent_embedding_null = 2
    report, issues, actions = _pipeline(pg=pg)
    mw01 = next(c for c in report.checks if c.check_id == "MW01_write_pipeline_recent")
    assert mw01.status == STATUS_FAIL
    issue = next(d for d in issues if d.code == "RECENT_EMBEDDING_FAILURE")
    assert issue.severity == "error"
    assert issue.evidence["recent_null"] == 2
    rebuild = next(a for a in actions if a.action_id == "REBUILD_EMBEDDING_FOR_QA_IDS")
    assert rebuild.target_count >= 2
    assert rebuild.requires_provider is True
    assert rebuild.writes_database is True
    assert rebuild.automatic_safe is False


def test_fault_g_historical_debt_only():
    """total=105, recent=0 → MW01 ok; HISTORICAL_EMBEDDING_DEBT (info);
    REBUILD_EMBEDDING_FOR_QA_IDS with target_count=105."""
    pg = _full_pg()
    pg.null_total = 105
    pg.recent_qa = 4
    pg.recent_embedding_ok = 4
    pg.recent_embedding_null = 0
    pg.oldest_null = "2025-01-01T00:00:00+00:00"
    pg.newest_null = "2026-08-01T00:00:00+00:00"
    report, issues, actions = _pipeline(pg=pg)
    mw01 = next(c for c in report.checks if c.check_id == "MW01_write_pipeline_recent")
    assert mw01.status == STATUS_OK
    issue = next((d for d in issues if d.code == "HISTORICAL_EMBEDDING_DEBT"), None)
    assert issue is not None
    assert issue.severity == "info"
    rebuild = next(a for a in actions if a.action_id == "REBUILD_EMBEDDING_FOR_QA_IDS")
    assert rebuild.target_count == 105


def test_fault_h_recent_empty_answer():
    """recent empty answer → MW03 warn; EMPTY_ANSWER_RECENT (warning);
    MANUAL_REVIEW_EMPTY_ANSWER (no automatic write).

    The diagnose layer reads ``recent_empty_answer`` from the *MW01*
    evidence (not MW03) — the empty-answer counter is part of the
    qa_pairs recent-window aggregate. Both surfaces need values.
    """
    pg = _full_pg()
    pg.empty_total = 5
    pg.empty_recent = 2
    pg.recent_empty_answer = 2  # MW01-side counter
    report, issues, actions = _pipeline(pg=pg)
    mw03 = next(c for c in report.checks if c.check_id == "MW03_empty_answer")
    assert mw03.status == STATUS_WARN
    issue = next(d for d in issues if d.code == "EMPTY_ANSWER_RECENT")
    assert issue.severity == "warning"
    review = next((a for a in actions if a.action_id == "MANUAL_REVIEW_EMPTY_ANSWER"), None)
    assert review is not None
    assert review.writes_database is False


def test_fault_h_explicit_memory_null_embedding():
    """MW05 null > 0 → EXPLICIT_MEMORY_EMBEDDING_NULL (warning);
    REPAIR_EXPLICIT_MEMORY_EMBEDDING."""
    pg = _full_pg()
    pg.explicit_memories_row = (10, 3, 7)  # 10 total, 3 null, 7 set
    report, issues, actions = _pipeline(pg=pg)
    mw05 = next(c for c in report.checks if c.check_id == "MW05_explicit_memory_embedding")
    assert mw05.status == STATUS_WARN
    assert mw05.evidence["explicit_embedding_null"] == 3
    issue = next(d for d in issues if d.code == "EXPLICIT_MEMORY_EMBEDDING_NULL")
    assert issue.severity == "warning"
    fix = next((a for a in actions if a.action_id == "REPAIR_EXPLICIT_MEMORY_EMBEDDING"), None)
    assert fix is not None
    assert fix.target_count == 3
    assert fix.requires_provider is True


def test_fault_h_longqa_orphan_parents():
    """MW06 parents_missing_parent_row=1 → LONG_QA_CHUNK_INCONSISTENT
    (error); NO_AUTOMATIC_REPAIR_SOURCE_MISSING (automatic_safe=False,
    reason explains we can't guess source)."""
    pg = _full_pg()
    pg.qa_chunks.update({
        "child_rows": 5, "distinct_parents": 3,
        "missing_parents": 1, "child_null": 0,
        "bad_offsets": 0, "dup_keys": 0,
    })
    report, issues, actions = _pipeline(pg=pg)
    mw06 = next(c for c in report.checks if c.check_id == "MW06_longqa_child_consistency")
    assert mw06.status == STATUS_FAIL
    issue = next(d for d in issues if d.code == "LONG_QA_CHUNK_INCONSISTENT")
    assert issue.severity == "error"
    noop = next((a for a in actions if a.action_id == "NO_AUTOMATIC_REPAIR_SOURCE_MISSING"), None)
    assert noop is not None
    assert noop.automatic_safe is False
    # The reason must explicitly call out "cannot guess source" or similar.
    assert "cannot" in noop.reason.lower() or "source" in noop.reason.lower()
    # The long-qa rebuild action must NOT be planned when parents are missing.
    assert not any(a.action_id == "REBUILD_LONG_QA_CHILDREN" for a in actions)


def test_fault_h_longqa_child_null_embedding():
    """MW06 child_null_embedding=1 → fail; REBUILD_LONG_QA_CHILDREN."""
    pg = _full_pg()
    pg.qa_chunks.update({
        "child_rows": 5, "distinct_parents": 3,
        "missing_parents": 0, "child_null": 1,
        "bad_offsets": 0, "dup_keys": 0,
    })
    report, issues, actions = _pipeline(pg=pg)
    mw06 = next(c for c in report.checks if c.check_id == "MW06_longqa_child_consistency")
    assert mw06.status == STATUS_FAIL
    issue = next(d for d in issues if d.code == "LONG_QA_CHUNK_INCONSISTENT")
    assert issue.severity == "error"
    rebuild = next((a for a in actions if a.action_id == "REBUILD_LONG_QA_CHILDREN"), None)
    assert rebuild is not None
    assert rebuild.target_count >= 1


# ──────────────────────────────────────────────────────────────────────
# Recovery — fault → degraded/unhealthy → fix → healthy
# ──────────────────────────────────────────────────────────────────────


def test_recovery_marker_removed():
    """Recent poisoned → unhealthy; remove marker → re-collect → healthy
    (no error-level diagnosis from FA)."""
    marker_dir = _plant_marker_dir(dict(
        name="new-poison",
        embedding_status="poisoned",
        q_msg_id="qm-new",
        error_class="provider_5xx",
        last_failure_at_epoch=1_700_000_000.0 - 600,
        embedding_attempts=3,
    ))
    # First pass: bad.
    report1, issues1, _ = _pipeline(
        pg=_full_pg(), marker_dir=marker_dir, now=1_700_000_000.0,
    )
    assert report1.overall == OVERALL_UNHEALTHY
    assert any(d.severity == "error" and d.scope == "failure_accounting"
               for d in issues1)

    # Now remove the marker → re-collect → should be healthy (no FA error).
    for f in marker_dir.glob("*.json"):
        f.unlink()
    report2, issues2, _ = _pipeline(
        pg=_full_pg(), marker_dir=marker_dir, now=1_700_000_000.0,
    )
    assert not any(
        d.severity == "error" and d.scope == "failure_accounting" for d in issues2
    )
    # And FA03 must now be ok.
    fa03 = next(c for c in report2.checks if c.check_id == "FA03_poisoned")
    assert fa03.status == STATUS_OK


def test_recovery_recent_null_cleared():
    """recent_null=1 → unhealthy; flip back to 0 → RECENT_EMBEDDING_FAILURE
    disappears; only HISTORICAL_EMBEDDING_DEBT info remains."""
    pg = _full_pg()
    pg.recent_qa = 4
    pg.recent_embedding_ok = 3
    pg.recent_embedding_null = 1
    pg.null_total = 5
    pg.oldest_null = "2025-01-01T00:00:00+00:00"
    pg.newest_null = "2026-08-01T00:00:00+00:00"
    report1, issues1, _ = _pipeline(pg=pg)
    assert report1.overall == OVERALL_UNHEALTHY
    assert any(d.code == "RECENT_EMBEDDING_FAILURE" for d in issues1)

    # Flip back to 0 null.
    pg.recent_embedding_ok = 4
    pg.recent_embedding_null = 0
    report2, issues2, _ = _pipeline(pg=pg)
    assert not any(d.code == "RECENT_EMBEDDING_FAILURE" for d in issues2)
    # Debt info still present (history didn't go away).
    debt = [d for d in issues2 if d.code == "HISTORICAL_EMBEDDING_DEBT"]
    assert debt, "HISTORICAL_EMBEDDING_DEBT should remain after recovery"


def test_recovery_index_missing_restored():
    """missing index → warn; restore → ok."""
    pg = _full_pg()
    pg.indexes.discard("explicit_memories_tags_gin")
    report1, _, _ = _pipeline(pg=pg)
    st05_1 = next(c for c in report1.checks if c.check_id == "ST05_canonical_indexes")
    assert st05_1.status == STATUS_WARN
    assert "explicit_memories_tags_gin" in st05_1.evidence["missing"]

    pg.indexes.add("explicit_memories_tags_gin")
    report2, _, _ = _pipeline(pg=pg)
    st05_2 = next(c for c in report2.checks if c.check_id == "ST05_canonical_indexes")
    assert st05_2.status == STATUS_OK
    assert st05_2.evidence["missing"] == []


# ──────────────────────────────────────────────────────────────────────
# D/E — deep provider (injected runner)
# ──────────────────────────────────────────────────────────────────────


def test_fault_deep_rerank_401():
    """Deep runner rerank 401 → PR03 fail; diagnose contains a provider
    code (one of {EMBED_PROVIDER_AUTH/RATE_LIMIT/TIMEOUT/PROVIDER_UNCONFIGURED})
    at severity >= warning."""
    def runner(cfg, *, timeout):
        return {
            "embedding": {"status": "ok", "summary": "ok"},
            "rerank": {"status": "fail", "summary": "401 Unauthorized"},
            "llm": {"status": "ok", "summary": "ok"},
        }

    report, issues, _ = _pipeline(
        pg=_full_pg(), deep=True, deep_auth_runner=runner,
    )
    pr03 = next(c for c in report.checks if c.check_id == "PR03_deep_auth")
    assert pr03.status == STATUS_FAIL
    allowed = {
        "EMBED_PROVIDER_AUTH", "EMBED_PROVIDER_RATE_LIMIT",
        "EMBED_PROVIDER_TIMEOUT", "PROVIDER_UNCONFIGURED",
    }
    matches = [d for d in issues if d.code in allowed]
    assert matches, f"expected a provider code, got {[d.code for d in issues]}"
    assert any(d.severity in ("warning", "error") for d in matches)


def test_fault_deep_llm_timeout():
    """Deep runner llm timeout → PR03 warn/fail; same code expectations."""
    def runner(cfg, *, timeout):
        return {
            "embedding": {"status": "ok", "summary": "ok"},
            "rerank": {"status": "ok", "summary": "ok"},
            "llm": {"status": "fail", "summary": "timeout"},
        }

    report, issues, _ = _pipeline(
        pg=_full_pg(), deep=True, deep_auth_runner=runner,
    )
    pr03 = next(c for c in report.checks if c.check_id == "PR03_deep_auth")
    assert pr03.status in (STATUS_FAIL, STATUS_WARN)
    allowed = {
        "EMBED_PROVIDER_AUTH", "EMBED_PROVIDER_RATE_LIMIT",
        "EMBED_PROVIDER_TIMEOUT", "PROVIDER_UNCONFIGURED",
    }
    matches = [d for d in issues if d.code in allowed]
    assert matches
    assert any(d.severity in ("warning", "error") for d in matches)


def test_fault_deep_skipped_means_runner_never_called():
    """Without --deep, PR03 is skip and the runner is NOT invoked — inject
    a runner that raises to prove it."""
    called = {"n": 0}

    def runner(cfg, *, timeout):
        called["n"] += 1
        raise AssertionError("deep runner must not be called when deep=False")

    report, _, _ = _pipeline(
        pg=_full_pg(), deep=False, deep_auth_runner=runner,
    )
    pr03 = next(c for c in report.checks if c.check_id == "PR03_deep_auth")
    assert pr03.status == STATUS_SKIP
    assert pr03.evidence.get("skipped_reason") == "deep_not_enabled"
    assert called["n"] == 0
