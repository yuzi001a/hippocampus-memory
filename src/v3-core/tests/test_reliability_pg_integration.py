# -*- coding: utf-8 -*-
"""DISPATCH-C2 §1 — opt-in disposable-PG integration test for reliability.

Mirrors the gate / safety pattern of ``tests/test_recall_v2_pg_integration.py``:

  * **Module-level gate** — only runs when ``HIPPOCAMPUS_RELIABILITY_TEST_DSN``
    is set; otherwise ``pytest.skip(..., allow_module_level=True)``.
  * **Safety refusal** — port==5433 (production) or non-loopback host →
    ``pytest.skip(...)`` so a misconfigured environment cannot reach the
    real DB.
  * **Real connect via C extension** — the conftest's P0-A guard shadows
    the public ``psycopg2.connect`` attribute, so we resolve the real
    factory from ``psycopg2._psycopg.connect`` (or ``_connect`` on Linux
    builds) and use that to construct connections.

The disposable PG MUST be running on ``localhost:55462`` with the lab
profile's three databases seeded:

  - ``v3embeddings_lab``     — v0.2.1 shape + 13650 qa_pairs (105 NULL).
  - ``v3embeddings_legacy``  — same data but missing ``explicit_memories``
    / ``qa_embedding_chunks`` / ``schema_versions``.
  - ``v3embeddings_fresh``   — fully bootstrapped, no production data.

All seeded / mutated rows carry a ``reltest_`` source_id prefix and are
deleted on teardown so the lab DB returns to its baseline (13650 ± 0
qa_pairs rows).

Assertion contract — **delta-based, not absolute**:

The lab DB is a snapshot of production and may drift between rounds
(more or fewer NULLs in the recent window than the documented 0). The
tests therefore assert the *change* that the test body produces, not
absolute counts. The fixtures capture before/after values and the
assertions compare those — so a future production drift does not
invalidate this suite.

Run with lab env::

    cd src/v3-core
    HIPPOCAMPUS_RELIABILITY_TEST_DSN="host=localhost port=55462 dbname=v3embeddings_lab user=v3user password=lab-local-only" \\
    HIPPOCAMPUS_RELIABILITY_TEST_DSN_LEGACY="host=localhost port=55462 dbname=v3embeddings_legacy user=v3user password=lab-local-only" \\
    ../../.venv/Scripts/python.exe -m pytest tests/test_reliability_pg_integration.py -v

Without either variable the module is skipped at collection time so the
full ``pytest tests/`` run stays green.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

# ─────────────────────────────────────────────────────────────────────
# Opt-in module gate — must be set BEFORE any side-effectful v3core import
# so collection-time skips don't crash on partial installs.
# ─────────────────────────────────────────────────────────────────────

_TEST_DSN = os.environ.get("HIPPOCAMPUS_RELIABILITY_TEST_DSN")
if not _TEST_DSN:
    pytest.skip(
        "HIPPOCAMPUS_RELIABILITY_TEST_DSN not set — opt-in disposable-PG "
        "integration test is skipped in CI",
        allow_module_level=True,
    )

_TEST_DSN_LEGACY = os.environ.get("HIPPOCAMPUS_RELIABILITY_TEST_DSN_LEGACY")
_LEGACY_AVAILABLE = bool(_TEST_DSN_LEGACY)


def _parse_dsn_safely(dsn: str) -> dict[str, str]:
    """Parse a libpq-style DSN into a dict, tolerating parse_dsn quirks."""
    try:
        from psycopg2.extensions import parse_dsn as _psycopg_parse_dsn
        parsed = _psycopg_parse_dsn(dsn)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if v is not None}
    except Exception:
        pass
    out: dict[str, str] = {}
    for token in dsn.split():
        if "=" not in token:
            continue
        k, _, v = token.partition("=")
        out[k.strip()] = v.strip()
    return out


_DSN_PARSED = _parse_dsn_safely(_TEST_DSN)
_DSN_PORT = _DSN_PARSED.get("port", "")
_DSN_HOST = _DSN_PARSED.get("host", "")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "localhost.localdomain"}

if _DSN_PORT == "5433":
    pytest.skip(
        "HIPPOCAMPUS_RELIABILITY_TEST_DSN targets port 5433 (production). "
        "Refusing to run — this test is disposable-only and never touches "
        "production. Set the variable to a loopback, non-5433 DSN.",
        allow_module_level=True,
    )
if _DSN_HOST and _DSN_HOST.lower() not in _LOOPBACK_HOSTS:
    pytest.skip(
        f"HIPPOCAMPUS_RELIABILITY_TEST_DSN host={_DSN_HOST!r} is not a "
        f"loopback address. Refusing to run. Allowed hosts: "
        f"{sorted(_LOOPBACK_HOSTS)}.",
        allow_module_level=True,
    )

# If a legacy DSN is provided, run the same safety check on it.
if _TEST_DSN_LEGACY:
    _LEG_PARSED = _parse_dsn_safely(_TEST_DSN_LEGACY)
    if _LEG_PARSED.get("port") == "5433" or (
        _LEG_PARSED.get("host")
        and _LEG_PARSED["host"].lower() not in _LOOPBACK_HOSTS
    ):
        pytest.skip(
            "HIPPOCAMPUS_RELIABILITY_TEST_DSN_LEGACY points at a "
            "non-disposable target. Refusing to run.",
            allow_module_level=True,
        )

# Make the in-tree src importable regardless of how pytest was launched.
REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))


# ─────────────────────────────────────────────────────────────────────
# Real psycopg2 connect factory — bypassing the conftest's P0-A guard.
# Same approach as test_recall_v2_pg_integration.py.
# ─────────────────────────────────────────────────────────────────────

def _resolve_real_connect() -> Callable[..., Any]:
    """Return the real psycopg2 connect factory, bypassing conftest guard."""
    try:
        from psycopg2._psycopg import connect as _real_connect  # type: ignore
        return _real_connect
    except Exception:
        pass
    try:
        from psycopg2._psycopg import _connect as _real_connect  # type: ignore
        return _real_connect
    except Exception:
        pass
    import psycopg2 as _psycopg2
    return _psycopg2.connect


_REAL_CONNECT_FACTORY = _resolve_real_connect()


def _open(dsn: str):
    """Open a real psycopg2 connection via the C-extension factory."""
    return _REAL_CONNECT_FACTORY(dsn)


# ─────────────────────────────────────────────────────────────────────
# Idempotent bypass fixture — reinstall the real psycopg2.connect for
# the duration of this module so anything HealthService pulls through
# also reaches libpq (it uses pg_connect injection in practice, but the
# guard bypass keeps the test self-contained and matches the G6B recipe).
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _restore_pg_connect():
    import psycopg2 as _psycopg2
    saved = getattr(_psycopg2, "connect", _REAL_CONNECT_FACTORY)
    try:
        _psycopg2.connect = _REAL_CONNECT_FACTORY  # type: ignore[assignment]
    except Exception:
        pass
    try:
        yield
    finally:
        try:
            _psycopg2.connect = saved  # type: ignore[assignment]
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────
# v3core imports — kept below the safety checks above.
# ─────────────────────────────────────────────────────────────────────

from v3core.reliability import (  # noqa: E402
    EXPECTED_SCHEMA_VERSION,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    HealthService,
    diagnose,
)


# ─────────────────────────────────────────────────────────────────────
# Idempotent cleanup + service factories.
# ─────────────────────────────────────────────────────────────────────

_RELTEST_PREFIX = "reltest_"
_BASELINE_QA_PAIRS = 13650  # lab DB factory state — must be restored


def _delete_reltest_rows(conn) -> None:
    """Best-effort DELETE for every row the suite may have inserted.

    Idempotent: runs before AND after the test body so a mid-test crash
    cannot leave stranded ``reltest_`` rows behind.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM public.qa_embedding_chunks c "
            "USING public.qa_pairs q "
            "WHERE c.qa_id = q.id AND q.source_id LIKE %s",
            (_RELTEST_PREFIX + "%",),
        )
        cur.execute(
            "DELETE FROM public.qa_pairs WHERE source_id LIKE %s",
            (_RELTEST_PREFIX + "%",),
        )
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _pg_connect_factory_for(dsn: str) -> Callable[..., Any]:
    """Return a pg_connect callable that opens connections to ``dsn``."""
    def _connect(**_kw):
        return _open(dsn)
    return _connect


def _service_for(dsn: str) -> HealthService:
    """Build a HealthService wired to ``dsn``.

    ``pg_connect`` is injected so HealthService ignores the conftest's
    blocked ``psycopg2.connect`` attribute and uses our C-extension
    factory directly.  ``pg`` dict is populated from a parsed view of
    the DSN so the production-boundary predicate sees a non-prod target.
    """
    parsed = _parse_dsn_safely(dsn)
    pg = {
        "host": parsed.get("host", "localhost"),
        "port": int(parsed.get("port", "55462")),
        "database": parsed.get("dbname", "v3embeddings_lab"),
        "user": parsed.get("user", "v3user"),
        # The CLI hard-redacts the password before passing it through,
        # but HealthService only ever reads `pg["host"]` / `pg["port"]`
        # / `pg["database"]` for the production-boundary check.  We
        # populate password for parity with the production-shape config.
        "password": parsed.get("password", "lab-local-only"),
    }
    base = Path(
        os.environ.get(
            "V3CORE_BASEPATH",
            str(Path.home() / ".v3-core-lab" / "profiles" / "default"),
        )
    )
    return HealthService(
        pg=pg,
        pg_connect=_pg_connect_factory_for(dsn),
        base_path=base,
        marker_dir=base / "j" / "pending_qa",
        allow_production_read=True,
    )


# ─────────────────────────────────────────────────────────────────────
# Connection fixtures — one per target DB.  Each cleans reltest_ rows
# before AND after the test, and additionally exposes helpers that
# capture before/after MW01/MW02 readings so the assertions can be
# delta-based.
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def lab_conn():
    """Yield a real psycopg2 connection scoped to a single test on
    ``v3embeddings_lab``."""
    conn = _open(_TEST_DSN)
    try:
        _delete_reltest_rows(conn)
        conn.commit()
        yield conn
    finally:
        try:
            _delete_reltest_rows(conn)
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _check(report, check_id: str):
    """Find a check result by id; raise AssertionError if missing."""
    for chk in report.checks:
        if chk.check_id == check_id:
            return chk
    raise AssertionError(
        f"check {check_id} not found in report (ids={[c.check_id for c in report.checks]})"
    )


def _mw01_evidence(conn):
    """Return the four MW01 counters from the live DB so tests can take
    a baseline reading before mutating and compare after."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT "
            "  COUNT(*) AS total, "
            "  COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS ok, "
            "  COUNT(*) FILTER (WHERE embedding IS NULL) AS null_emb, "
            "  COUNT(*) FILTER (WHERE answer IS NULL OR BTRIM(answer) = '') AS empty_ans "
            "FROM public.qa_pairs "
            "WHERE created_at >= (NOW() - INTERVAL '24 hours')"
        )
        total, ok, null_emb, empty_ans = cur.fetchone()
    finally:
        cur.close()
    return {
        "recent_qa": int(total or 0),
        "recent_embedding_ok": int(ok or 0),
        "recent_embedding_null": int(null_emb or 0),
        "recent_empty_answer": int(empty_ans or 0),
    }


def _mw02_total(conn):
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM public.qa_pairs WHERE embedding IS NULL")
        return int(cur.fetchone()[0])
    finally:
        cur.close()


# ─────────────────────────────────────────────────────────────────────
# 1) lab full shape — ST01–ST05 all ok; MW02 >= 105
# ─────────────────────────────────────────────────────────────────────

def test_lab_full_shape_storage_ok(lab_conn):
    """Lab DB has the v0.2.1 shape — all storage checks green; MW02
    reports >= 105 historical NULLs (DESIGN §6: 105 historical NULL
    embeddings coexist with a healthy overall).  MW01 may be warn/fail
    if the lab DB drifted past the window — we do NOT pin MW01 here;
    the delta-based tests below exercise that path explicitly."""
    service = _service_for(_TEST_DSN)
    report = service.collect()

    # Report is JSON-serializable (DESIGN §3 dataclass contract).
    payload = report.to_dict()
    assert json.dumps(payload)  # raises on non-serializable

    # schema_version at the top
    assert payload["schema_version"] == "1"
    assert payload["overall"] in {"healthy", "degraded", "unhealthy"}

    # ST01–ST05 — lab is full v0.2.1 shape → all ok.
    for cid in (
        "ST01_pg_reachable",
        "ST02_pgvector",
        "ST03_schema_ledger",
        "ST04_canonical_tables",
        "ST05_canonical_indexes",
    ):
        chk = _check(report, cid)
        assert chk.status == STATUS_OK, (
            f"{cid} expected ok, got {chk.status} (evidence={chk.evidence})"
        )

    # ST03 specifically confirms v0.2 row present.
    st03 = _check(report, "ST03_schema_ledger")
    assert st03.evidence["expected_version"] == EXPECTED_SCHEMA_VERSION
    assert st03.evidence["present"] is True

    # MW02 — debt >= 105 (the lab factory ships with 105 historical NULLs).
    mw02 = _check(report, "MW02_embedding_debt")
    assert mw02.evidence["embedding_null_total"] >= 105, (
        f"MW02 expected >= 105 historical NULL rows, got {mw02.evidence}"
    )
    # MW01 status — record only; do not pin (lab may have drifted).

    # qa_pairs baseline preserved — no DB writes from this test.
    cur = lab_conn.cursor()
    try:
        cur.execute("SELECT count(*) FROM public.qa_pairs")
        assert cur.fetchone()[0] == _BASELINE_QA_PAIRS
    finally:
        cur.close()


# ─────────────────────────────────────────────────────────────────────
# 2) legacy shape — ST03 / ST04 fail; diagnose emits SCHEMA_* codes.
# ─────────────────────────────────────────────────────────────────────

def test_legacy_shape_flags_missing_objects():
    """Legacy DB is missing schema_versions / explicit_memories /
    qa_embedding_chunks. ST03 and ST04 must fail; the diagnose step
    must surface at least one of SCHEMA_TABLE_MISSING /
    SCHEMA_VERSION_MISMATCH."""
    if not _TEST_DSN_LEGACY:
        pytest.skip("HIPPOCAMPUS_RELIABILITY_TEST_DSN_LEGACY not set")

    # Sanity: legacy DB doesn't have explicit_memories /
    # qa_embedding_chunks / schema_versions (precondition for this
    # test to be meaningful).
    with _open(_TEST_DSN_LEGACY) as conn:
        cur = conn.cursor()
        try:
            for tbl in ("explicit_memories", "qa_embedding_chunks", "schema_versions"):
                cur.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=%s",
                    (tbl,),
                )
                assert cur.fetchone()[0] == 0, (
                    f"legacy precondition violated: {tbl} unexpectedly exists"
                )
        finally:
            cur.close()

    service = _service_for(_TEST_DSN_LEGACY)
    report = service.collect()

    st03 = _check(report, "ST03_schema_ledger")
    st04 = _check(report, "ST04_canonical_tables")
    assert st03.status == STATUS_FAIL, (
        f"ST03 expected fail on legacy, got {st03.status} (evidence={st03.evidence})"
    )
    assert st04.status == STATUS_FAIL, (
        f"ST04 expected fail on legacy, got {st04.status} (evidence={st04.evidence})"
    )
    assert st04.evidence["missing"], "ST04 must list at least one missing table"

    issues = diagnose(report)
    codes = {d.code for d in issues}
    assert {"SCHEMA_TABLE_MISSING", "SCHEMA_VERSION_MISMATCH"} & codes, (
        f"diagnose did not surface a SCHEMA_* code on legacy DB; "
        f"got codes={codes}"
    )


# ─────────────────────────────────────────────────────────────────────
# 3) drop a canonical index → ST05 warns; diagnose SCHEMA_INDEX_MISSING;
#    finally restore the index and re-verify ST05 ok.
# ─────────────────────────────────────────────────────────────────────

def test_index_missing_then_restored(lab_conn):
    """DROP the explicit_memories_tags_gin index, collect → ST05 warns
    and diagnose emits SCHEMA_INDEX_MISSING; finally CREATE the index
    back and re-collect → ST05 ok again."""
    cur = lab_conn.cursor()
    cur.execute(
        "SELECT 1 FROM pg_indexes "
        "WHERE schemaname='public' AND indexname='explicit_memories_tags_gin'"
    )
    if cur.fetchone() is None:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS explicit_memories_tags_gin "
            "ON public.explicit_memories USING GIN (tags)"
        )
        lab_conn.commit()
    cur.close()

    # Baseline ST05 ok before mutation.
    baseline_report = _service_for(_TEST_DSN).collect()
    st05_before = _check(baseline_report, "ST05_canonical_indexes")
    assert st05_before.status == STATUS_OK, (
        f"baseline ST05 not ok before mutation: {st05_before.evidence}"
    )

    # Drop and re-collect.
    cur = lab_conn.cursor()
    cur.execute("DROP INDEX IF EXISTS public.explicit_memories_tags_gin")
    lab_conn.commit()
    cur.close()

    try:
        broken_report = _service_for(_TEST_DSN).collect()
        st05_broken = _check(broken_report, "ST05_canonical_indexes")
        assert st05_broken.status == STATUS_WARN, (
            f"ST05 expected warn after DROP, got {st05_broken.status}"
        )
        assert "explicit_memories_tags_gin" in st05_broken.evidence["missing"]

        codes = {d.code for d in diagnose(broken_report)}
        assert "SCHEMA_INDEX_MISSING" in codes, (
            f"diagnose missing SCHEMA_INDEX_MISSING on dropped index; got {codes}"
        )
    finally:
        # Always restore the index — even if an assertion above failed.
        cur = lab_conn.cursor()
        try:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS explicit_memories_tags_gin "
                "ON public.explicit_memories USING GIN (tags)"
            )
            lab_conn.commit()
        finally:
            cur.close()

    # Re-collect: ST05 must be ok again.
    restored_report = _service_for(_TEST_DSN).collect()
    st05_after = _check(restored_report, "ST05_canonical_indexes")
    assert st05_after.status == STATUS_OK, (
        f"ST05 not ok after restore: {st05_after.evidence}"
    )

    # qa_pairs count unchanged.
    cur = lab_conn.cursor()
    try:
        cur.execute("SELECT count(*) FROM public.qa_pairs")
        assert cur.fetchone()[0] == _BASELINE_QA_PAIRS
    finally:
        cur.close()


# ─────────────────────────────────────────────────────────────────────
# 4) insert a recent NULL embedding → MW01 fail; DELETE → MW01 ok.
#    Delta-based: lab may already have drifted; we measure the change.
# ─────────────────────────────────────────────────────────────────────

def test_recent_null_insert_fails_then_removed(lab_conn):
    """Insert one qa_pairs row with a NULL embedding + NOW() created_at.
    MW01's recent_null counter must grow by 1; the new row must be
    INSERTed (and deleted afterwards)."""
    cur = lab_conn.cursor()
    try:
        before = _mw01_evidence(lab_conn)

        sid = f"{_RELTEST_PREFIX}null_recent_{int(time.time()*1000)}"
        cur.execute(
            "INSERT INTO public.qa_pairs "
            "  (source_id, session_id, question, answer, embedding, "
            "   source, timestamp, created_at) "
            "VALUES (%s, %s, %s, %s, NULL, 'reltest', NOW(), NOW())",
            (sid, "reltest_session", "reltest q", "reltest a"),
        )
        lab_conn.commit()

        after_insert = _mw01_evidence(lab_conn)
        assert after_insert["recent_embedding_null"] == before["recent_embedding_null"] + 1, (
            f"recent null did not grow by 1: before={before}, after={after_insert}"
        )
        assert after_insert["recent_qa"] == before["recent_qa"] + 1, (
            f"recent qa did not grow by 1: before={before}, after={after_insert}"
        )

        # Re-collect: HealthService's MW01 evidence must reflect the
        # same delta (same SQL, same window).
        failing = _service_for(_TEST_DSN).collect()
        mw01_fail = _check(failing, "MW01_write_pipeline_recent")
        assert mw01_fail.evidence["recent_embedding_null"] == before["recent_embedding_null"] + 1

        # Delete and re-collect: counter returns to baseline.
        cur.execute(
            "DELETE FROM public.qa_pairs WHERE source_id = %s", (sid,)
        )
        lab_conn.commit()

        after_delete = _mw01_evidence(lab_conn)
        assert after_delete["recent_embedding_null"] == before["recent_embedding_null"], (
            f"recent null did not return to baseline: before={before}, "
            f"after_delete={after_delete}"
        )

        recovered = _service_for(_TEST_DSN).collect()
        mw01_recovered = _check(recovered, "MW01_write_pipeline_recent")
        assert mw01_recovered.evidence["recent_embedding_null"] == before["recent_embedding_null"]
    finally:
        cur.close()


# ─────────────────────────────────────────────────────────────────────
# 5) insert historical NULL embedding → MW01 recent_null unchanged;
#    MW02 total grows by 1; diagnose emits HISTORICAL_EMBEDDING_DEBT.
# ─────────────────────────────────────────────────────────────────────

def test_historical_null_insert_keeps_current_clean(lab_conn):
    """A NULL-embedding row whose created_at is older than the window
    must NOT change MW01's recent counter. It must grow MW02's debt
    by 1 and trigger the HISTORICAL_EMBEDDING_DEBT diagnosis (info) —
    never RECENT_EMBEDDING_FAILURE."""
    cur = lab_conn.cursor()
    try:
        # Baseline readings.
        mw01_before = _mw01_evidence(lab_conn)
        mw02_before = _mw02_total(lab_conn)

        sid = f"{_RELTEST_PREFIX}null_hist_{int(time.time()*1000)}"
        cur.execute(
            "INSERT INTO public.qa_pairs "
            "  (source_id, session_id, question, answer, embedding, "
            "   source, timestamp, created_at) "
            "VALUES (%s, %s, %s, %s, NULL, 'reltest', "
            "        NOW() - INTERVAL '90 days', "
            "        NOW() - INTERVAL '90 days')",
            (sid, "reltest_session", "reltest hist q", "reltest hist a"),
        )
        lab_conn.commit()

        # Re-measure: recent counters unchanged, total grew by 1.
        mw01_after = _mw01_evidence(lab_conn)
        mw02_after = _mw02_total(lab_conn)
        assert mw01_after["recent_embedding_null"] == mw01_before["recent_embedding_null"], (
            f"historical NULL must NOT affect MW01 recent counter: "
            f"before={mw01_before}, after={mw01_after}"
        )
        assert mw02_after == mw02_before + 1, (
            f"MW02 total should grow by 1 (before={mw02_before}, "
            f"after={mw02_after})"
        )

        # HealthService + diagnose must reflect the same.
        after = _service_for(_TEST_DSN).collect()
        mw02_check = _check(after, "MW02_embedding_debt")
        assert mw02_check.evidence["embedding_null_total"] == mw02_before + 1

        codes = {d.code for d in diagnose(after)}
        assert "HISTORICAL_EMBEDDING_DEBT" in codes, (
            f"diagnose missing HISTORICAL_EMBEDDING_DEBT; got {codes}"
        )
        # RECENT_EMBEDDING_FAILURE only fires when MW01 fails. If the
        # lab was already drifted to a fail state, this assertion is
        # not meaningful — guard it on MW01 actually being ok.
        mw01_check = _check(after, "MW01_write_pipeline_recent")
        if mw01_check.status == STATUS_OK:
            assert "RECENT_EMBEDDING_FAILURE" not in codes, (
                f"historical NULL must NOT trigger RECENT_EMBEDDING_FAILURE; "
                f"got codes={codes}"
            )
    finally:
        # Cleanup regardless.
        cur.execute(
            "DELETE FROM public.qa_pairs WHERE source_id LIKE %s",
            (f"{_RELTEST_PREFIX}null_hist_%",),
        )
        lab_conn.commit()
        cur.close()


# ─────────────────────────────────────────────────────────────────────
# 6) NULL child chunk → MW06 fail; cleanup both rows.
# ─────────────────────────────────────────────────────────────────────

def test_longqa_null_child_fails(lab_conn):
    """Insert a qa_pairs row + a qa_embedding_chunks row whose embedding
    is NULL. MW06 must fail with child_null_embedding >= 1. Cleanup
    must remove BOTH rows."""
    cur = lab_conn.cursor()
    try:
        # Verify the chunks table is present (it may be absent on a
        # super-legacy DB; in that case the contract is "MW06 cannot
        # observe this fault" and we skip with a clear message).
        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='qa_embedding_chunks'"
        )
        if cur.fetchone()[0] == 0:
            pytest.skip(
                "qa_embedding_chunks table not present on lab DB — "
                "cannot exercise MW06 child-NULL scenario"
            )

        # Capture baseline counters.
        before = _service_for(_TEST_DSN).collect()
        mw06_before = _check(before, "MW06_longqa_child_consistency")
        before_null = mw06_before.evidence["child_null_embedding"]

        # Seed the parent qa_pairs row first (the child has FK to it).
        qa_sid = f"{_RELTEST_PREFIX}longqa_{int(time.time()*1000)}"
        cur.execute(
            "INSERT INTO public.qa_pairs "
            "  (source_id, session_id, question, answer, "
            "   source, timestamp, created_at) "
            "VALUES (%s, %s, %s, %s, 'reltest', NOW(), NOW()) "
            "RETURNING id",
            (qa_sid, "reltest_session", "reltest long q", "reltest long a"),
        )
        qa_id = cur.fetchone()[0]

        # Now the chunk row with NULL embedding. The schema requires
        # qa_id / chunk_index / source_field / source_start / source_end /
        # source_sha256 / token_count / representation_version / content
        # to all be NOT NULL — fill those so only `embedding` is NULL.
        cur.execute(
            "INSERT INTO public.qa_embedding_chunks "
            "  (qa_id, chunk_index, source_field, source_start, source_end, "
            "   source_sha256, token_count, representation_version, "
            "   content, embedding) "
            "VALUES (%s, 0, 'question', 0, 7, 'reltestsha', 3, "
            "        'reltest_v1', 'reltest chunk content', NULL)",
            (qa_id,),
        )
        lab_conn.commit()

        # Re-collect: MW06 must fail, and the null counter must have
        # grown by exactly 1.
        after = _service_for(_TEST_DSN).collect()
        mw06 = _check(after, "MW06_longqa_child_consistency")
        assert mw06.status == STATUS_FAIL, (
            f"MW06 expected fail with NULL child embedding; got {mw06.status} "
            f"(evidence={mw06.evidence})"
        )
        assert mw06.evidence["child_null_embedding"] == before_null + 1, (
            f"child_null_embedding should grow by 1: before={before_null}, "
            f"after={mw06.evidence['child_null_embedding']}"
        )
    finally:
        # Tear down both rows (delete the parent — chunks cascade).
        cur.execute(
            "DELETE FROM public.qa_pairs WHERE source_id LIKE %s",
            (f"{_RELTEST_PREFIX}longqa_%",),
        )
        # Cascade normally handles chunks, but be paranoid.
        cur.execute(
            "DELETE FROM public.qa_embedding_chunks "
            "WHERE qa_id NOT IN (SELECT id FROM public.qa_pairs)"
        )
        lab_conn.commit()
        cur.close()


# ─────────────────────────────────────────────────────────────────────
# 7) collect() wall-clock under 2 seconds on lab.
# ─────────────────────────────────────────────────────────────────────

def test_collect_latency_lab(lab_conn):
    """A single collect() call against the lab DB must finish in under
    2.0s wall — this is the DESIGN §11 performance budget (default
    ``health --json``, warm cache)."""
    service = _service_for(_TEST_DSN)
    t0 = time.perf_counter()
    report = service.collect()
    elapsed = time.perf_counter() - t0

    assert elapsed < 2.0, (
        f"collect() took {elapsed:.3f}s — over the 2.0s budget"
    )
    # Sanity: at least the storage + memory_write sections ran.
    assert any(c.check_id == "ST01_pg_reachable" for c in report.checks)
    assert any(c.check_id == "MW01_write_pipeline_recent" for c in report.checks)


# ─────────────────────────────────────────────────────────────────────
# 8) empty-answer recent row → MW03 warns; delta-based.
# ─────────────────────────────────────────────────────────────────────

def test_empty_answer_recent_warn(lab_conn):
    """An empty-answer row in the recent window must grow MW03's
    ``empty_answer_recent`` counter by 1 and push MW03 to warn (the
    lab may already have other empty answers, so we measure the
    delta)."""
    cur = lab_conn.cursor()
    try:
        before = _service_for(_TEST_DSN).collect()
        mw03_before = _check(before, "MW03_empty_answer")
        before_recent = mw03_before.evidence["empty_answer_recent"]

        sid = f"{_RELTEST_PREFIX}empty_{int(time.time()*1000)}"
        cur.execute(
            "INSERT INTO public.qa_pairs "
            "  (source_id, session_id, question, answer, "
            "   source, timestamp, created_at) "
            "VALUES (%s, %s, %s, '', 'reltest', NOW(), NOW())",
            (sid, "reltest_session", "reltest empty q"),
        )
        lab_conn.commit()

        after = _service_for(_TEST_DSN).collect()
        mw03 = _check(after, "MW03_empty_answer")
        assert mw03.status == STATUS_WARN, (
            f"MW03 expected warn after empty-answer insert; got {mw03.status} "
            f"(evidence={mw03.evidence})"
        )
        assert mw03.evidence["empty_answer_recent"] == before_recent + 1, (
            f"empty_answer_recent should grow by 1: before={before_recent}, "
            f"after={mw03.evidence['empty_answer_recent']}"
        )
    finally:
        cur.execute(
            "DELETE FROM public.qa_pairs WHERE source_id LIKE %s",
            (f"{_RELTEST_PREFIX}empty_%",),
        )
        lab_conn.commit()
        cur.close()


# ─────────────────────────────────────────────────────────────────────
# Final module-level guarantee — qa_pairs back to baseline.
# ─────────────────────────────────────────────────────────────────────

def test_module_final_baseline_restored(lab_conn):
    """Defence in depth: after every other test in this module ran,
    qa_pairs must be back at the lab factory baseline. The per-test
    fixtures already enforce this via finally-block cleanup; this test
    just *asserts* the count and that no reltest_ rows leaked."""
    cur = lab_conn.cursor()
    try:
        cur.execute("SELECT count(*) FROM public.qa_pairs")
        actual = cur.fetchone()[0]
    finally:
        cur.close()
    assert actual == _BASELINE_QA_PAIRS, (
        f"qa_pairs baseline drift: expected {_BASELINE_QA_PAIRS}, got {actual}"
    )
    cur = lab_conn.cursor()
    try:
        cur.execute(
            "SELECT count(*) FROM public.qa_pairs WHERE source_id LIKE %s",
            (_RELTEST_PREFIX + "%",),
        )
        leftover = cur.fetchone()[0]
    finally:
        cur.close()
    assert leftover == 0, (
        f"{leftover} reltest_ row(s) leaked into the lab DB — "
        f"cleanup is mandatory"
    )
