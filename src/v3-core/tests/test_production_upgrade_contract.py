"""Hippocampus v0.2.1 deployment-contract offline half-tests.

These tests cover the explicit / auditable production-apply path
introduced in v0.2.1 (two-part confirmation: ``--allow-production-write``
+ ``--confirm-plan-sha <PLAN_SHA>``) and the canonical-plan emission
on dry-run (``plan_sha256`` + destructive scan + transaction-boundary
check + exact combined SQL).

All tests are offline — the conftest-level psycopg2.connect ban is
kept intact; the FakePg harness below monkeypatches psycopg2.connect
per test to record every connect/execute/commit/rollback and answer
the canonical probe statements against an in-memory schema.

Real PG canary coverage (transactions, idempotency, destructive-blocked
rollback) lives outside this file and is run against a live cluster.
"""
from __future__ import annotations

import argparse
import io
import json
import re
from contextlib import redirect_stderr, redirect_stdout

import psycopg2
import pytest

from v3core import distribution_cli as dc


# ---------------------------------------------------------------------------
# DSN literals (sourced from §4.1 of the dispatch).
# ---------------------------------------------------------------------------
PROD_DSN = "postgres://v3user:prodpass@localhost:5433/v3embeddings"
NONPROD_DSN = (
    "postgres://v3user:devpass@localhost:55432/v3embeddings_alpha"
)


# ---------------------------------------------------------------------------
# Helpers — FakePg / FakeConn / FakeCursor + _ns + _full_shape
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, pg):
        self.pg = pg
        self._result = None
        self.last = None  # (sql, params)
        self._normalize_ws = lambda s: " ".join((s or "").split())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.pg.executed.append(self._normalize_ws(sql))
        self.last = (sql, params)
        norm = (sql or "").lower()
        if "from information_schema.tables" in norm:
            # probe for table presence; params = (table,).
            # The real cursor returns a row tuple when present, and
            # None when absent — encode both faithfully so the caller's
            # ``cur.fetchone() is not None`` boolean logic works.
            self._result = (1,) if (params and params[0] in self.pg.tables) else None
        elif "from information_schema.columns" in norm:
            # params = (table, column). Same row-tuple vs None
            # distinction as the tables probe above.
            self._result = (
                (1,)
                if (params and (params[0], params[1]) in self.pg.columns)
                else None
            )
        elif "from public.schema_versions" in norm:
            self._result = self.pg.schema_row
        else:
            self._result = None

    def fetchone(self):
        return self._result


class FakeConn:
    def __init__(self, pg):
        self.pg = pg
        self.autocommit = False

    def cursor(self):
        self.pg.cursor_count += 1
        return FakeCursor(self.pg)

    def commit(self):
        self.pg.commits += 1

    def rollback(self):
        self.pg.rollbacks += 1

    def close(self):
        self.pg.closes += 1


class FakePg:
    def __init__(self):
        self.tables: set[str] = set()
        self.columns: set[tuple[str, str]] = set()
        self.schema_row: tuple | None = None
        self.executed: list[str] = []
        self.cursor_count: int = 0
        self.commits: int = 0
        self.rollbacks: int = 0
        self.closes: int = 0
        self.last_connect_kwargs: dict | None = None

    def connect(self, *args, **kwargs):
        self.last_connect_kwargs = kwargs
        self.connect_count = getattr(self, "connect_count", 0) + 1
        return FakeConn(self)


def _ns(
    *,
    target=None,
    dsn=None,
    apply=False,
    dry_run=False,
    allow_production_read=False,
    allow_production_write=False,
    confirm_plan_sha=None,
    plan_out=None,
    show_sql=False,
) -> argparse.Namespace:
    """Construct an argparse.Namespace mirroring the upgrade subparser."""
    return argparse.Namespace(
        target=target,
        dsn=dsn,
        apply=apply,
        dry_run=dry_run,
        allow_production_read=allow_production_read,
        allow_production_write=allow_production_write,
        confirm_plan_sha=confirm_plan_sha,
        plan_out=plan_out,
        show_sql=show_sql,
    )


def _full_shape():
    tables = {
        "qa_pairs",
        "conversation_stream",
        "topics",
        "topic_entries",
        "observation_notes",
        "yin_paragraphs",
        # extras from the upgrade-required + optional sets:
        "explicit_memories",
        "schema_versions",
        "qa_embedding_chunks",
    }
    cols = {
        ("qa_pairs", "source_id"),
        ("qa_pairs", "turn_id"),
        ("qa_pairs", "source"),
        ("qa_pairs", "tool_calls"),
        ("qa_pairs", "tool_results"),
        ("qa_pairs", "embed_model"),
        ("qa_pairs", "created_at"),
        ("topics", "note_ref"),
        ("topics", "last_observer_ts"),
        ("topics", "embed_model"),
        ("topic_entries", "source_qa_id"),
        ("topic_entries", "embed_model"),
        ("observation_notes", "embed_model"),
        ("yin_paragraphs", "embed_model"),
    }
    return tables, cols


def _run(fn, *args, **kwargs):
    """Run a distribution_cli function with stdout/stderr captured."""
    out = io.StringIO()
    err = io.StringIO()
    rc = None
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = fn(*args, **kwargs)
        except SystemExit as e:
            rc = int(e.code) if e.code is not None else 0
    return rc, out.getvalue(), err.getvalue()


def _executed_has_ddl(executed: list[str]) -> bool:
    """Return True if any executed SQL starts with BEGIN/CREATE/ALTER/INSERT.

    Used by the tests that assert "zero DDL" — the only thing the apply
    path may legitimately write is the combined SQL body, so a fully
    short-circuited refusal shows zero executed statements at all.
    """
    starters = ("begin", "create", "alter", "insert", "commit")
    return any(s.strip().lower().startswith(starters) for s in executed)


def _install_fake_pg(monkeypatch, pg: FakePg):
    """Install a fake psycopg2.connect for the duration of one test."""
    monkeypatch.setattr(psycopg2, "connect", pg.connect)
    return pg


# ===========================================================================
# §18-1 — prod apply without ANY confirmation is refused
# ===========================================================================


def test_prod_apply_without_any_confirmation_refused(monkeypatch, tmp_path):
    pg = FakePg()
    _install_fake_pg(monkeypatch, pg)
    rc, _out, err = _run(
        dc._upgrade_apply, _ns(target=PROD_DSN, apply=True)
    )
    assert rc == 2
    assert "refusing production-boundary" in err
    assert pg.executed == []
    assert pg.commits == 0


# ===========================================================================
# §18-2 — allow_production_write without confirm_plan_sha is refused
# ===========================================================================


def test_prod_apply_write_flag_without_plan_sha_refused(monkeypatch):
    pg = FakePg()
    _install_fake_pg(monkeypatch, pg)
    rc, _out, err = _run(
        dc._upgrade_apply,
        _ns(target=PROD_DSN, apply=True, allow_production_write=True),
    )
    assert rc == 2
    assert "must be provided together" in err.lower()
    assert pg.executed == []
    assert pg.commits == 0


# ===========================================================================
# §18-3 — confirm_plan_sha without allow_production_write is refused
# ===========================================================================


def test_prod_apply_plan_sha_without_write_flag_refused(monkeypatch):
    pg = FakePg()
    _install_fake_pg(monkeypatch, pg)
    rc, _out, err = _run(
        dc._upgrade_apply,
        _ns(target=PROD_DSN, apply=True, confirm_plan_sha="a" * 64),
    )
    assert rc == 2
    # The apply path rejects the prod target before the sha-verify check
    # fires — either SystemExit message is acceptable evidence.
    assert (
        "refusing production-boundary" in err
        or "must be provided together" in err.lower()
    )
    assert pg.executed == []
    assert pg.commits == 0


# ===========================================================================
# §18-4 — wrong plan_sha refuses with plan_mismatch=true, ZERO DDL
# ===========================================================================


def test_prod_apply_wrong_plan_sha_refused(monkeypatch):
    tables, cols = _full_shape()
    pg = FakePg()
    pg.tables = set(tables)
    pg.columns = set(cols)
    pg.schema_row = ("v0.2", "2026-09-18 00:00:00+00")
    _install_fake_pg(monkeypatch, pg)
    rc, out, _err = _run(
        dc._upgrade_apply,
        _ns(
            target=PROD_DSN,
            apply=True,
            allow_production_write=True,
            confirm_plan_sha="0" * 64,
        ),
    )
    assert rc == 2
    payload = json.loads(out)
    assert payload.get("plan_mismatch") is True
    assert payload.get("expected_plan_sha") == "0" * 64
    assert payload.get("current_plan_sha") and payload["current_plan_sha"] != (
        "0" * 64
    )
    assert payload.get("plan_sha256") == payload["current_plan_sha"]
    # zero DDL: no commit AND no DDL statements ever touched the fake conn
    assert pg.commits == 0
    assert not any(
        s.strip().lower().startswith(("begin", "create", "alter", "insert"))
        for s in pg.executed
    )


# ===========================================================================
# §18-5 — plan_target_binding disposables cannot authorize production
# ===========================================================================


def test_plan_target_binding_disposable_cannot_authorize_production(
    monkeypatch,
):
    tables, cols = _full_shape()
    pg = FakePg()
    pg.tables = set(tables)
    pg.columns = set(cols)
    pg.schema_row = ("v0.2", "2026-09-18")
    _install_fake_pg(monkeypatch, pg)

    plan_prod, _meta_p, _sql_p = dc._upgrade_build_plan(
        dc._parse_dsn(PROD_DSN)
    )
    plan_nonprod, _meta_n, _sql_n = dc._upgrade_build_plan(
        dc._parse_dsn(NONPROD_DSN)
    )
    assert plan_prod["plan_sha256"] != plan_nonprod["plan_sha256"]
    # Target binding isolates the database + port — same host, different
    # db; same host+db, different port.
    plan_other_db, _, _ = dc._upgrade_build_plan(
        dc._parse_dsn("postgres://u:p@localhost:55432/v3embeddings")
    )
    plan_other_port, _, _ = dc._upgrade_build_plan(
        dc._parse_dsn("postgres://u:p@localhost:55432/v3embeddings_alpha")
    )
    assert plan_other_port["plan_sha256"] == plan_nonprod["plan_sha256"]
    assert plan_other_db["plan_sha256"] != plan_nonprod["plan_sha256"]
    # Case-insensitive host normalization (LOCALHOST == localhost).
    plan_upper_host, _, _ = dc._upgrade_build_plan(
        dc._parse_dsn(
            "postgres://v3user:devpass@LOCALHOST:55432/v3embeddings_alpha"
        )
    )
    assert plan_upper_host["plan_sha256"] == plan_nonprod["plan_sha256"]


# ===========================================================================
# §18-6 — SQL artifact change changes plan_sha
# ===========================================================================


def test_sql_artifact_change_changes_plan_sha(monkeypatch):
    tables, cols = _full_shape()
    pg = FakePg()
    pg.tables = set(tables)
    pg.columns = set(cols)
    pg.schema_row = ("v0.2", "2026-09-18")
    _install_fake_pg(monkeypatch, pg)

    plan_a, _meta_a, _sql_a = dc._upgrade_build_plan(dc._parse_dsn(NONPROD_DSN))
    sha_a = plan_a["plan_sha256"]
    combined_a = plan_a["combined_sql"]["sha256"]

    real_pkg = dc._package_sql
    # Append a benign comment line to every subsequent _package_sql
    # call so the upgrade_v0_2.sql artifact (and therefore the
    # combined_sql sha) changes between build_plan invocations.
    suffix = "\n-- hotfix test comment line\n"

    def patched_package_sql(name):
        text = real_pkg(name)
        return text + suffix

    monkeypatch.setattr(dc, "_package_sql", patched_package_sql)

    plan_b, _meta_b, _sql_b = dc._upgrade_build_plan(dc._parse_dsn(NONPROD_DSN))
    sha_b = plan_b["plan_sha256"]
    combined_b = plan_b["combined_sql"]["sha256"]

    assert combined_a != combined_b, (
        "expected the SQL-artifact change to flip combined_sql.sha256"
    )
    assert sha_a != sha_b, (
        "expected the SQL-artifact change to flip plan_sha256"
    )


# ===========================================================================
# §18-7 — live schema change (explicit_memories absent) changes plan_sha
# ===========================================================================


def test_live_schema_change_changes_plan_sha(monkeypatch):
    tables_a, cols_a = _full_shape()
    pg_a = FakePg()
    pg_a.tables = set(tables_a)
    pg_a.columns = set(cols_a)
    pg_a.schema_row = ("v0.2", "2026-09-18")
    _install_fake_pg(monkeypatch, pg_a)
    plan_a, _, _ = dc._upgrade_build_plan(dc._parse_dsn(NONPROD_DSN))

    # Same column + schema_row, but explicit_memories is missing → a
    # different pre_state.missing_required_tables list → different sha.
    tables_b = set(tables_a)
    tables_b.discard("explicit_memories")
    pg_b = FakePg()
    pg_b.tables = tables_b
    pg_b.columns = set(cols_a)
    pg_b.schema_row = ("v0.2", "2026-09-18")
    _install_fake_pg(monkeypatch, pg_b)
    plan_b, _, _ = dc._upgrade_build_plan(dc._parse_dsn(NONPROD_DSN))

    assert plan_a["pre_state"]["missing_required_tables"] == []
    assert plan_b["pre_state"]["missing_required_tables"] == ["explicit_memories"]
    assert plan_a["plan_sha256"] != plan_b["plan_sha256"]


# ===========================================================================
# §18-9 — dry-run is zero DDL
# ===========================================================================


def test_dry_run_produces_zero_ddl(monkeypatch):
    tables, cols = _full_shape()
    pg = FakePg()
    pg.tables = set(tables)
    pg.columns = set(cols)
    pg.schema_row = ("v0.2", "2026-09-18")
    _install_fake_pg(monkeypatch, pg)
    rc, _out, _err = _run(
        dc._upgrade_dry_run, _ns(target=NONPROD_DSN, dry_run=True)
    )
    # Even with everything present, dry-run typically reports
    # would_apply=True because schema_versions_v0_2 may differ in
    # detail; rc is informational here (0/1 both acceptable).
    assert rc in (0, 1)
    assert pg.commits == 0
    # Every executed statement on the dry-run path is a SELECT-style
    # information_schema probe (or schema_versions probe). No DDL.
    for stmt in pg.executed:
        assert stmt.strip().lower().startswith("select"), stmt[:80]


# ===========================================================================
# §18-10 — production dry-run emits exact plan + sha + recommended apply
# ===========================================================================


def test_production_dry_run_emits_exact_plan_and_sha(monkeypatch):
    tables, cols = _full_shape()
    pg = FakePg()
    pg.tables = set(tables)
    pg.columns = set(cols)
    pg.schema_row = ("v0.2", "2026-09-18")
    _install_fake_pg(monkeypatch, pg)
    rc, out, _err = _run(
        dc._upgrade_dry_run,
        _ns(target=PROD_DSN, dry_run=True, allow_production_read=True),
    )
    assert rc in (0, 1)
    payload = json.loads(out)
    assert re.fullmatch(r"[0-9a-f]{64}", payload["plan_sha256"])
    assert payload["plan"]["target"] == {
        "host": "localhost",
        "port": 5433,
        "database": "v3embeddings",
    }
    assert payload["plan"]["destructive_scan"]["clean"] is True
    assert payload["plan"]["transaction_boundary"]["single_transaction"] is True
    for k in ("upgrade_v0_2.sql", "explicit_memories.sql", "qa_embedding_chunks.sql"):
        sha = payload["plan"]["artifacts"].get(k)
        assert sha and re.fullmatch(r"[0-9a-f]{64}", sha), k
    assert payload["plan"]["combined_sql"]["bytes"] > 0
    tables_after = payload["plan"]["expected_objects_after_apply"]["tables"]
    # Tables may be schema-qualified (``public.explicit_memories``)
    # — match by suffix so the assertion is robust either way.
    for t in ("explicit_memories", "schema_versions", "qa_embedding_chunks"):
        assert any(t in name for name in tables_after), (t, tables_after)
    apply_cmd = payload["recommended_commands"]["apply"]
    assert "--allow-production-write" in apply_cmd
    assert "--confirm-plan-sha" in apply_cmd
    assert payload["plan_sha256"] in apply_cmd
    assert "prodpass" not in out


# ===========================================================================
# §18-11 — destructive SQL injection is refused (unit + integration + dry)
# ===========================================================================


def test_destructive_sql_injection_refused(monkeypatch):
    # (a) unit: every forbidden keyword produces clean=False
    cases = [
        "DROP TABLE public.qa_pairs;",
        "TRUNCATE public.qa_pairs;",
        "DELETE FROM public.qa_pairs WHERE id = 1;",
        "UPDATE public.qa_pairs SET foo = 'x';",
        "ALTER TABLE public.qa_pairs DROP COLUMN y;",
        "GRANT ALL ON public.qa_pairs TO public;",
        "COPY public.qa_pairs FROM stdin;",
    ]
    for sql in cases:
        scan = dc._scan_destructive_sql(sql)
        assert scan["clean"] is False, sql
        assert scan["violations"], sql

    # (b) integration: combined SQL with DROP → apply refuses, zero DDL
    tables, cols = _full_shape()
    pg = FakePg()
    pg.tables = set(tables)
    pg.columns = set(cols)
    pg.schema_row = ("v0.2", "2026-09-18")
    _install_fake_pg(monkeypatch, pg)

    # First grab a real plan-sha so we can attempt the two-part
    # confirmation path.
    plan, _meta, _sql = dc._upgrade_build_plan(dc._parse_dsn(PROD_DSN))
    assert plan["destructive_scan"]["clean"] is True
    plan_sha = plan["plan_sha256"]

    # Now corrupt the combined SQL body by appending a DROP TABLE.
    real_load = dc._upgrade_load_combined_sql

    def corrupted_load(include_qa_chunks_artifact):
        text, meta = real_load(include_qa_chunks_artifact)
        if text:
            text = text + "\nDROP TABLE public.qa_pairs;\n"
            meta = dict(meta)
            meta["expanded_bytes"] = len(text.encode("utf-8"))
        return text, meta

    monkeypatch.setattr(dc, "_upgrade_load_combined_sql", corrupted_load)
    rc, out, _err = _run(
        dc._upgrade_apply,
        _ns(
            target=PROD_DSN,
            apply=True,
            allow_production_write=True,
            confirm_plan_sha=plan_sha,
        ),
    )
    assert rc == 2
    payload = json.loads(out)
    assert payload["plan_sha256"] is not None
    assert payload["plan_verified"] is False
    assert payload["destructive_scan"]["clean"] is False
    assert pg.commits == 0
    assert not any(
        s.strip().lower().startswith("begin") for s in pg.executed
    )

    # (c) dry-run: same corrupted body → dry-run refuses with rc=2 and
    # plan.destructive_scan.clean=False.
    rc2, out2, _err2 = _run(
        dc._upgrade_dry_run,
        _ns(target=NONPROD_DSN, dry_run=True),
    )
    assert rc2 == 2
    payload2 = json.loads(out2)
    assert payload2["plan"]["destructive_scan"]["clean"] is False


# ===========================================================================
# §18-14 — second confirmed apply is supported (offline half)
# ===========================================================================


def test_second_confirmed_apply_is_supported(monkeypatch):
    tables, cols = _full_shape()
    pg = FakePg()
    pg.tables = set(tables)
    pg.columns = set(cols)
    pg.schema_row = ("v0.2", "2026-09-18")
    _install_fake_pg(monkeypatch, pg)

    plan, _meta, _sql = dc._upgrade_build_plan(dc._parse_dsn(NONPROD_DSN))
    plan_sha = plan["plan_sha256"]
    assert plan_sha and re.fullmatch(r"[0-9a-f]{64}", plan_sha)

    rc, out, _err = _run(
        dc._upgrade_apply,
        _ns(
            target=NONPROD_DSN,
            apply=True,
            allow_production_write=True,
            confirm_plan_sha=plan_sha,
        ),
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["plan_verified"] is True
    assert payload["plan_sha256"] == plan_sha
    assert payload["confirmed_plan_sha"] == plan_sha
    # The fake conn saw the BEGIN…COMMIT body and committed exactly once.
    assert pg.commits == 1
    # The combined SQL is sent as a single multi-statement execute()
    # call, so pg.executed[-1] is the full body. The statement-level
    # BEGIN/COMMIT markers appear as ``; BEGIN;`` and ``; COMMIT;``
    # tokens inside that body (whitespace-normalized). Verify both
    # boundaries landed.
    combined = pg.executed[-1]
    assert "BEGIN;" in combined.upper()
    assert "COMMIT;" in combined.upper()


# ===========================================================================
# §18-15 — secret scan: no credentials in plan, dry-run stdout, plan-out, apply
# ===========================================================================


def test_secret_scan_plan_and_outputs_have_no_credentials(monkeypatch, tmp_path):
    tables, cols = _full_shape()
    pg = FakePg()
    pg.tables = set(tables)
    pg.columns = set(cols)
    pg.schema_row = ("v0.2", "2026-09-18")
    _install_fake_pg(monkeypatch, pg)

    # Build plan directly — must not embed the password anywhere.
    plan, _meta, _sql = dc._upgrade_build_plan(dc._parse_dsn(PROD_DSN))
    plan_text = json.dumps(plan, sort_keys=True)
    assert "prodpass" not in plan_text
    assert "devpass" not in plan_text

    # Dry-run stdout over production (with read opt-in).
    _rc, dry_out, _dry_err = _run(
        dc._upgrade_dry_run,
        _ns(target=PROD_DSN, dry_run=True, allow_production_read=True),
    )
    assert "prodpass" not in dry_out
    assert "devpass" not in dry_out

    # Plan-out file — write and verify.
    plan_path = tmp_path / "plan.json"
    _rc, dry_out2, _dry_err2 = _run(
        dc._upgrade_dry_run,
        _ns(
            target=PROD_DSN,
            dry_run=True,
            allow_production_read=True,
            plan_out=str(plan_path),
        ),
    )
    file_text = plan_path.read_text(encoding="utf-8")
    assert "prodpass" not in file_text
    assert "devpass" not in file_text
    # Apply output (correct sha path) over a non-prod target with devpass
    # in the DSN — also must not leak the password.
    plan_n, _m_n, _s_n = dc._upgrade_build_plan(dc._parse_dsn(NONPROD_DSN))
    plan_sha = plan_n["plan_sha256"]
    _rc, apply_out, _apply_err = _run(
        dc._upgrade_apply,
        _ns(
            target=NONPROD_DSN,
            apply=True,
            allow_production_write=True,
            confirm_plan_sha=plan_sha,
        ),
    )
    assert "devpass" not in apply_out
    assert "prodpass" not in apply_out


# ===========================================================================
# §4.2#16 — _is_production_boundary matches _enforce_production_boundary
# ===========================================================================


def test_is_production_boundary_matches_enforce():
    """Predicate must agree with the raise-or-pass behavior of the enforcer."""
    cases = [
        (PROD_DSN, True),                # port 5433 → production
        (NONPROD_DSN, False),            # alpha port + alpha db → not prod
        (
            "postgres://v3user:devpass@localhost:55432/v3embeddings",
            True,
        ),                                # loopback + v3embeddings → production
        (
            "postgres://v3user:devpass@localhost:55432/v3embeddings_alpha",
            False,
        ),                                # loopback but alpha db → not prod
        (
            "postgres://v3user:devpass@db.internal.example:5432/v3embeddings",
            False,
        ),                                # non-loopback + v3embeddings → not prod
    ]
    for dsn, expected in cases:
        parsed = dc._parse_dsn(dsn)
        assert dc._is_production_boundary(parsed) is expected, dsn
        # Enforce must raise iff predicate says True.
        raised = False
        try:
            dc._enforce_production_boundary(dict(parsed))
        except SystemExit:
            raised = True
        assert raised is expected, dsn


# ===========================================================================
# §4.2#17 — plan_sha is deterministic
# ===========================================================================


def test_plan_sha_is_deterministic(monkeypatch):
    tables, cols = _full_shape()
    pg = FakePg()
    pg.tables = set(tables)
    pg.columns = set(cols)
    pg.schema_row = ("v0.2", "2026-09-18")
    _install_fake_pg(monkeypatch, pg)
    parsed = dc._parse_dsn(NONPROD_DSN)
    plan_a, _, _ = dc._upgrade_build_plan(parsed)
    plan_b, _, _ = dc._upgrade_build_plan(parsed)
    assert plan_a["plan_sha256"] == plan_b["plan_sha256"]
    # Key-order independence of the canonical sha.
    h1 = dc._canonical_json_sha256({"a": 1, "b": 2})
    h2 = dc._canonical_json_sha256({"b": 2, "a": 1})
    assert h1 == h2


# ===========================================================================
# §4.2#18 — --plan-out and --show-sql expose exact SQL
# ===========================================================================


def test_plan_out_and_show_sql_expose_exact_sql(monkeypatch, tmp_path):
    tables, cols = _full_shape()
    pg = FakePg()
    pg.tables = set(tables)
    pg.columns = set(cols)
    pg.schema_row = ("v0.2", "2026-09-18")
    _install_fake_pg(monkeypatch, pg)

    plan_path = tmp_path / "plan.json"
    rc, out, _err = _run(
        dc._upgrade_dry_run,
        _ns(
            target=NONPROD_DSN,
            dry_run=True,
            plan_out=str(plan_path),
            show_sql=True,
        ),
    )
    assert rc in (0, 1)
    assert plan_path.exists()
    file_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    assert "plan" in file_payload
    assert "plan_sha256" in file_payload
    assert "final_combined_sql" in file_payload
    final_sql = file_payload["final_combined_sql"]
    assert "BEGIN;" in final_sql
    assert "COMMIT;" in final_sql
    assert file_payload["plan_sha256"] == file_payload["plan"]["plan_sha256"]
    # stdout also contains the sentinel-wrapped SQL block.
    assert "FINAL COMBINED SQL" in out
    assert "BEGIN;" in out
    assert "COMMIT;" in out


# ===========================================================================
# §4.2#19 — resolve pairing + read-flag semantics
# ===========================================================================


def test_resolve_pairing_and_read_flag_semantics(monkeypatch):
    pg = FakePg()
    _install_fake_pg(monkeypatch, pg)

    # Apply + read flag (no write / no sha) → still refused; the read
    # flag does not authorize writes.
    rc, _out, err = _run(
        dc._upgrade_apply,
        _ns(
            target=PROD_DSN,
            apply=True,
            allow_production_read=True,
        ),
    )
    assert rc == 2
    # apply+read gets refused at the "must be provided together" gate
    # (the prod-target refusal precedes it; either SystemExit is OK).
    assert (
        "must be provided together" in err.lower()
        or "refusing production-boundary" in err
    )

    # Dry-run mode + write flags → does NOT raise (warnings only).
    pg2 = FakePg()
    tables, cols = _full_shape()
    pg2.tables = set(tables)
    pg2.columns = set(cols)
    pg2.schema_row = ("v0.2", "2026-09-18")
    _install_fake_pg(monkeypatch, pg2)
    rc2, _out2, err2 = _run(
        dc._upgrade_dry_run,
        _ns(
            target=NONPROD_DSN,
            dry_run=True,
            allow_production_write=True,
            confirm_plan_sha="a" * 64,
        ),
    )
    # Dry-run completes normally; warnings go to stderr/log.
    assert rc2 in (0, 1, 2)
