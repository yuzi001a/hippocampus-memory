"""Focused tests for the G5b evaluator infrastructure.

These tests verify the evaluator's contract — the lane implementations,
the failure taxonomy, the scenario schema, and the runner wiring — without
touching production code or production data. They run in isolation
against the lab fake pool (Lane A) and the in-package scenario set.

Tests are intentionally small and orthogonal; a single failure should
identify the broken layer.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# Make ``src/v3-core/src`` importable so ``v3core`` resolves.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
_SRC_ROOT = os.path.join(_PKG_ROOT, "src")
_EVAL_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
for _p in (_SRC_ROOT, _EVAL_ROOT, _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


from eval.g5b_real_memory_evaluator.failure_taxonomy import (  # noqa: E402
    ALL_LABELS,
    ARCHIVED_MEMORY_RETURNED,
    CONFLICT_RESOLUTION,
    NOT_RETRIEVED,
    NOT_STORED,
    PIPELINE_ERROR,
    RANKED_TOO_LOW,
    STALE_MEMORY,
    UNKNOWN,
    UNSUPPORTED,
    WRONG_MEMORY,
    normalize,
)
from eval.g5b_real_memory_evaluator.lane_a import (  # noqa: E402
    build_lane_a_writer_and_reader,
    deterministic_embedder,
)
from eval.g5b_real_memory_evaluator.lane_b import (  # noqa: E402
    LaneBDisabled,
    _parse_dsn,
    _safe_exc_message,
    _validate_disposable_dsn,
    is_lane_b_enabled,
    resolve_lane_b_dsn,
)
from eval.g5b_real_memory_evaluator.metrics import (  # noqa: E402
    QueryResult,
    ScenarioResult,
    compute_metrics,
)
from eval.g5b_real_memory_evaluator.runner import (  # noqa: E402
    DEFAULT_RECALL_LIMIT,
    run_scenarios,
)
from eval.g5b_real_memory_evaluator.scenario_schema import (  # noqa: E402
    ALLOWED_CATEGORIES,
    ScenarioLoadError,
    scenario_from_dict,
)
from eval.g5b_real_memory_evaluator.scenarios import scenarios_v1  # noqa: E402


# ── failure taxonomy ──────────────────────────────────────────────────────


def test_failure_taxonomy_has_all_required_labels():
    expected = {
        "NOT_STORED", "NOT_INDEXED", "NOT_RETRIEVED", "RANKED_TOO_LOW",
        "WRONG_MEMORY", "STALE_MEMORY", "ARCHIVED_MEMORY_RETURNED",
        "CONFLICT_RESOLUTION", "PIPELINE_ERROR", "TIMEOUT",
        "UNSUPPORTED", "UNKNOWN",
    }
    assert set(ALL_LABELS) == expected


def test_normalize_known_label_returns_same():
    assert normalize(NOT_RETRIEVED) == NOT_RETRIEVED
    assert normalize(PIPELINE_ERROR) == PIPELINE_ERROR


def test_normalize_unknown_label_returns_unknown():
    assert normalize("BOGUS") == UNKNOWN
    assert normalize("") == UNKNOWN
    assert normalize(None) == UNKNOWN


# ── lane A: deterministic fake pool ───────────────────────────────────────


def test_lane_a_writer_create_returns_durable_committed():
    writer, reader, pool = build_lane_a_writer_and_reader()
    res = writer.create(
        category="test",
        title="hello world",
        content="simple test",
        tags=["test"],
    )
    assert res.durable is True
    assert res.status == "DURABLE_COMMITTED"
    assert res.memory_id.startswith("mem_")
    assert len(res.memory_id) == 4 + 64  # "mem_" + 64-hex


def test_lane_a_writer_retry_returns_deduplicated():
    writer, reader, pool = build_lane_a_writer_and_reader()
    a = writer.create(category="test", title="x", content="y", tags=["t"])
    b = writer.create(category="test", title="x", content="y", tags=["t"])
    assert a.memory_id == b.memory_id
    assert b.status == "DEDUPLICATED"


def test_lane_a_writer_conflict_returns_durable_failed():
    writer, reader, pool = build_lane_a_writer_and_reader()
    a = writer.create(category="test", title="x", content="y", tags=["t"])
    b = writer.create(
        category="test",
        title="x-mutated",
        content="DIFFERENT",
        tags=["t"],
        memory_id=a.memory_id,
    )
    assert b.durable is False
    assert b.status == "DURABLE_FAILED"
    # Row preserved.
    rec = reader.get_by_memory_id(a.memory_id)
    assert rec["content"] == "y"


def test_lane_a_archive_isolates_recall():
    writer, reader, pool = build_lane_a_writer_and_reader()
    a = writer.create(category="test", title="x", content="y", tags=["t"])
    ar = writer.archive(a.memory_id)
    assert ar.archived is True
    kw = reader.search_keyword("y", limit=5)
    assert all(r["memory_id"] != a.memory_id for r in kw)


def test_lane_a_hard_archive_rejected():
    writer, reader, pool = build_lane_a_writer_and_reader()
    a = writer.create(category="test", title="x", content="y", tags=["t"])
    ar = writer.archive(a.memory_id, hard=True)
    assert ar.hard_rejected is True
    rec = reader.get_by_memory_id(a.memory_id)
    assert rec["status"] == "active"


def test_lane_a_deterministic_embedder_is_deterministic():
    a = deterministic_embedder("hello world", {"model": "stub"})
    b = deterministic_embedder("hello world", {"model": "stub"})
    assert a == b
    assert len(a) == 1024


# ── lane B: safety guard ──────────────────────────────────────────────────


def test_lane_b_reserved_port_rejected():
    # 5433 is the reserved production-port safety sentinel; the
    # guard rejects it before any connection is opened.
    dsn = "host=127.0.0.1 port=5433 dbname=lab_eval user=lab password=lab"
    with pytest.raises(LaneBDisabled) as exc_info:
        _validate_disposable_dsn(dsn)
    assert "reserved" in str(exc_info.value).lower()


def test_lane_b_reserved_dbname_rejected():
    dsn = "host=127.0.0.1 port=55462 dbname=v3embeddings user=lab password=lab"
    with pytest.raises(LaneBDisabled) as exc_info:
        _validate_disposable_dsn(dsn)
    assert "reserved" in str(exc_info.value).lower()


def test_lane_b_disposable_dsn_accepted():
    dsn = "host=127.0.0.1 port=55462 dbname=lab_eval user=lab password=lab"
    _validate_disposable_dsn(dsn)  # should not raise


def test_lane_b_disabled_when_env_missing(monkeypatch):
    monkeypatch.delenv("G5B_EVAL_LIVE_PG", raising=False)
    monkeypatch.delenv("G5B_EVAL_LAB_DSN", raising=False)
    assert is_lane_b_enabled() is False


def test_lane_b_disabled_when_only_dsn_set(monkeypatch):
    monkeypatch.delenv("G5B_EVAL_LIVE_PG", raising=False)
    monkeypatch.setenv("G5B_EVAL_LAB_DSN", "host=127.0.0.1 port=55462 dbname=lab_eval")
    assert is_lane_b_enabled() is False


def test_lane_b_enabled_when_both_set(monkeypatch):
    monkeypatch.setenv("G5B_EVAL_LIVE_PG", "1")
    monkeypatch.setenv(
        "G5B_EVAL_LAB_DSN",
        "host=127.0.0.1 port=55462 dbname=lab_eval user=lab password=lab",
    )
    assert is_lane_b_enabled() is True


def test_lane_b_resolve_dsn_prefers_explicit_over_env(monkeypatch):
    monkeypatch.setenv("G5B_EVAL_LAB_DSN", "from-env")
    explicit = "from-explicit"
    assert resolve_lane_b_dsn(explicit) == explicit


def test_lane_b_dsn_parser_handles_quoted_values():
    parts = _parse_dsn("host='127.0.0.1' port=55462 dbname=lab_eval")
    assert parts["host"] == "127.0.0.1"
    assert parts["port"] == "55462"


def test_lane_b_safe_exc_message_does_not_leak_payload():
    """_safe_exc_message must not echo the underlying message, which
    for psycopg2 / pg drivers can include the DSN / password.
    """

    class FakeDSNLeak(Exception):
        def __init__(self) -> None:
            super().__init__(
                "connection to server failed: "
                "host=127.0.0.1 port=55462 dbname=lab_eval "
                "user=lab password=SECRET-LEAK"
            )

    msg = _safe_exc_message(FakeDSNLeak())
    assert "SECRET-LEAK" not in msg
    assert "password" not in msg
    assert "127.0.0.1" not in msg
    assert "lab_eval" not in msg
    assert "FakeDSNLeak" in msg


# ── lane B: safe setup error boundary (no live PG required) ──────────────
# These tests monkeypatch the private psycopg2 / pgvector modules so we
# can exercise the Lane B setup failure boundary without actually opening
# any network connection. They are the regression guard for the contract
# that no DSN / password / raw exception message ever escapes
# ``build_lane_b_writer_and_reader`` or the Lane B generic runner path.


class _FakeOperationalError(Exception):
    """Mimics psycopg2.OperationalError — its __str__ includes the DSN."""


def _install_fake_psycopg2(monkeypatch, *, mode: str) -> None:
    """Install a stub psycopg2 / pgvector in ``sys.modules`` for lane_b.

    ``mode`` controls where the stubbed chain fails:

      * ``"connect"``   — ``psycopg2.connect(dsn)`` raises.
      * ``"register"``  — connect returns a stub conn, ``register_vector`` raises.
      * ``"cursor"``    — connect + register_vector OK, ``conn.cursor()`` raises.
      * ``"execute"``   — connect + register + cursor OK, ``cur.execute(ddl)`` raises.
      * ``"commit"``    — connect + register + cursor + execute OK, ``conn.commit()`` raises.
      * ``"ok"``        — entire chain succeeds; the stub returns a sentinel conn.
    """
    class _StubCursor:
        def __init__(self, fail_execute: bool = False) -> None:
            self._fail_execute = fail_execute

        def execute(self, _ddl: str) -> None:
            if self._fail_execute:
                raise _FakeOperationalError(
                    "schema execute failed: "
                    "host=127.0.0.1 port=55462 dbname=lab_eval "
                    "user=lab password=SECRET-LEAK"
                )

    class _StubConn:
        def __init__(self, fail_cursor: bool = False,
                     fail_commit: bool = False) -> None:
            self._fail_cursor = fail_cursor
            self._fail_commit = fail_commit
            self.closed = False

        def cursor(self) -> _StubCursor:
            if self._fail_cursor:
                raise _FakeOperationalError(
                    "cursor() failed: "
                    "host=127.0.0.1 port=55462 dbname=lab_eval "
                    "user=lab password=SECRET-LEAK"
                )
            return _StubCursor(fail_execute=(mode == "execute"))

        def commit(self) -> None:
            if self._fail_commit:
                raise _FakeOperationalError(
                    "commit() failed: "
                    "host=127.0.0.1 port=55462 dbname=lab_eval "
                    "user=lab password=SECRET-LEAK"
                )

        def close(self) -> None:
            self.closed = True

    def _fake_connect(_dsn: str) -> _StubConn:
        if mode == "connect":
            raise _FakeOperationalError(
                "connection to server failed: "
                "host=127.0.0.1 port=55462 dbname=lab_eval "
                "user=lab password=SECRET-LEAK"
            )
        conn = _StubConn(
            fail_cursor=(mode == "cursor"),
            fail_commit=(mode == "commit"),
        )
        # Expose the most-recently-opened stub connection on the
        # stub psycopg2 module so boundary tests can assert that
        # the Lane B setup boundary closed it best-effort.
        sys.modules["psycopg2"]._last_conn = conn  # type: ignore[attr-defined]
        return conn

    def _fake_register_vector(_conn: Any) -> None:
        if mode == "register":
            raise _FakeOperationalError(
                "register_vector failed: "
                "host=127.0.0.1 port=55462 dbname=lab_eval "
                "user=lab password=SECRET-LEAK"
            )

    # Stub the psycopg2 / pgvector module surface that lane_b imports.
    import types
    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = _fake_connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    fake_pgvector = types.ModuleType("pgvector")
    fake_pgvector_psycopg2 = types.ModuleType("pgvector.psycopg2")
    fake_pgvector_psycopg2.register_vector = _fake_register_vector  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pgvector", fake_pgvector)
    monkeypatch.setitem(sys.modules, "pgvector.psycopg2", fake_pgvector_psycopg2)


# Disposable DSN used by the boundary tests. It passes the existing
# guard (port != 5433, dbname != reserved, host != reserved, no
# PGUSER collision under monkeypatch).
_SAFE_DSN = "host=127.0.0.1 port=55462 dbname=lab_eval user=lab password=lab"


def test_lane_b_connect_failure_raises_lane_b_disabled_without_secret(monkeypatch):
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("G5B_EVAL_LAB_DSN_OPT_IN", raising=False)
    _install_fake_psycopg2(monkeypatch, mode="connect")

    from eval.g5b_real_memory_evaluator.lane_b import build_lane_b_writer_and_reader

    with pytest.raises(LaneBDisabled) as exc_info:
        build_lane_b_writer_and_reader(_SAFE_DSN)
    msg = str(exc_info.value)
    # Must NOT echo any part of the simulated DSN / password.
    assert "SECRET-LEAK" not in msg
    assert "password" not in msg.lower()
    assert "127.0.0.1" not in msg
    assert "lab_eval" not in msg
    assert "55462" not in msg
    # Must still identify the failure as a setup failure.
    assert "psycopg2.connect failed" in msg
    assert "_FakeOperationalError" in msg


def test_lane_b_register_vector_failure_raises_lane_b_disabled(monkeypatch):
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("G5B_EVAL_LAB_DSN_OPT_IN", raising=False)
    _install_fake_psycopg2(monkeypatch, mode="register")

    from eval.g5b_real_memory_evaluator.lane_b import build_lane_b_writer_and_reader

    with pytest.raises(LaneBDisabled) as exc_info:
        build_lane_b_writer_and_reader(_SAFE_DSN)
    msg = str(exc_info.value)
    assert "SECRET-LEAK" not in msg
    assert "register_vector failed" in msg
    assert "_FakeOperationalError" in msg


def test_lane_b_cursor_failure_closes_connection(monkeypatch):
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("G5B_EVAL_LAB_DSN_OPT_IN", raising=False)
    _install_fake_psycopg2(monkeypatch, mode="cursor")

    from eval.g5b_real_memory_evaluator.lane_b import build_lane_b_writer_and_reader

    with pytest.raises(LaneBDisabled):
        build_lane_b_writer_and_reader(_SAFE_DSN)
    # The stub connection must have been closed best-effort.
    conn = sys.modules["psycopg2"]._last_conn  # type: ignore[attr-defined]
    assert conn.closed is True


def test_lane_b_ddl_execute_failure_closes_connection(monkeypatch):
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("G5B_EVAL_LAB_DSN_OPT_IN", raising=False)
    _install_fake_psycopg2(monkeypatch, mode="execute")

    from eval.g5b_real_memory_evaluator.lane_b import build_lane_b_writer_and_reader

    with pytest.raises(LaneBDisabled) as exc_info:
        build_lane_b_writer_and_reader(_SAFE_DSN)
    msg = str(exc_info.value)
    assert "SECRET-LEAK" not in msg
    assert "DDL execute failed" in msg
    conn = sys.modules["psycopg2"]._last_conn  # type: ignore[attr-defined]
    assert conn.closed is True


def test_lane_b_commit_failure_closes_connection(monkeypatch):
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("G5B_EVAL_LAB_DSN_OPT_IN", raising=False)
    _install_fake_psycopg2(monkeypatch, mode="commit")

    from eval.g5b_real_memory_evaluator.lane_b import build_lane_b_writer_and_reader

    with pytest.raises(LaneBDisabled) as exc_info:
        build_lane_b_writer_and_reader(_SAFE_DSN)
    msg = str(exc_info.value)
    assert "SECRET-LEAK" not in msg
    assert "conn.commit() failed" in msg
    conn = sys.modules["psycopg2"]._last_conn  # type: ignore[attr-defined]
    assert conn.closed is True


def test_lane_b_successful_setup_does_not_raise(monkeypatch):
    """When the entire psycopg2 / register_vector / cursor / DDL chain
    succeeds, ``build_lane_b_writer_and_reader`` returns writer, reader,
    conn — and no exception escapes the boundary.
    """
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("G5B_EVAL_LAB_DSN_OPT_IN", raising=False)
    _install_fake_psycopg2(monkeypatch, mode="ok")

    from eval.g5b_real_memory_evaluator.lane_b import build_lane_b_writer_and_reader

    # Build the writer/reader; we don't exercise the underlying v3core
    # machinery here — we only verify the setup boundary itself does
    # not raise and returns the expected tuple shape.
    writer, reader, conn = build_lane_b_writer_and_reader(_SAFE_DSN)
    assert writer is not None
    assert reader is not None
    assert conn is not None


def test_lane_b_run_py_lane_b_generic_exception_uses_type_only(monkeypatch, capsys):
    """The Lane B generic runner exception path in ``run.py`` must print
    only a type-only diagnostic and NO repr/traceback — those would
    echo any DSN / password carried by psycopg2.OperationalError.
    """
    monkeypatch.setenv("G5B_EVAL_LIVE_PG", "1")
    monkeypatch.setenv(
        "G5B_EVAL_LAB_DSN",
        "host=127.0.0.1 port=55462 dbname=lab_eval user=lab password=lab",
    )

    # Force run_scenarios to raise a fake exception whose message
    # looks exactly like a leaked DSN / password.
    class _Leaky(Exception):
        def __init__(self) -> None:
            super().__init__(
                "host=127.0.0.1 port=55462 "
                "user=lab password=SECRET-LEAK"
            )

    import eval.g5b_real_memory_evaluator.run as run_mod
    monkeypatch.setattr(
        run_mod,
        "run_scenarios",
        lambda *_a, **_kw: (_ for _ in ()).throw(_Leaky()),
    )
    # Make the lane-b branch believe it is fully enabled.
    monkeypatch.setattr(run_mod, "is_lane_b_enabled", lambda: True)

    rc = run_mod.main([
        "--lane", "b", "--live-pg",
        "--out-dir", "outputs/_test_b_safe",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    # The diagnostic must be type-only — no repr, no traceback, no
    # DSN / password / raw message.
    err = captured.err
    assert "SECRET-LEAK" not in err
    assert "password" not in err.lower()
    assert "127.0.0.1" not in err
    assert "55462" not in err
    assert "lab_eval" not in err
    assert "_Leaky" in err
    # And it must NOT include a full Python traceback frame.
    assert "Traceback (most recent call last)" not in err


def test_lane_a_run_py_generic_exception_still_uses_repr_traceback(monkeypatch, capsys):
    """Lane A diagnostic path is unchanged: repr(exc) + traceback are
    still emitted because Lane A carries no secrets.
    """
    class _LaneAErr(Exception):
        def __init__(self) -> None:
            super().__init__("lane-a-only-no-secret")

    import eval.g5b_real_memory_evaluator.run as run_mod
    monkeypatch.setattr(
        run_mod,
        "run_scenarios",
        lambda *_a, **_kw: (_ for _ in ()).throw(_LaneAErr()),
    )

    rc = run_mod.main(["--lane", "a", "--out-dir", "outputs/_test_a_unchanged"])
    captured = capsys.readouterr()
    assert rc == 1
    err = captured.err
    # Lane A still uses repr + traceback.
    assert "Traceback (most recent call last)" in err
    assert "_LaneAErr" in err
    # And the Lane B safety suffix must NOT appear in the Lane A path.
    assert "suppressed for safety" not in err


def test_lane_b_schema_path_resolves_to_existing_file():
    """The schema DDL path used by Lane B must point at the
    in-tree ``explicit_memories.sql`` file (not a wrong location).
    Importing lane_b exercises the exact same path math the
    production code runs.
    """
    import importlib
    import os

    pkg = importlib.import_module("eval.g5b_real_memory_evaluator.lane_b")
    lb_file = os.path.abspath(pkg.__file__)
    schema_path = os.path.normpath(
        os.path.join(
            os.path.dirname(lb_file),
            os.pardir, os.pardir,
            "schema", "explicit_memories.sql",
        )
    )
    assert os.path.exists(schema_path), (
        f"expected schema DDL at {schema_path}; update lane_b path math"
    )


# ── scenario schema ───────────────────────────────────────────────────────


def test_scenario_count_meets_minimum():
    scenarios = scenarios_v1.all_scenarios()
    assert len(scenarios) >= 40


def test_scenario_count_matches_documented_total():
    """The canonical total must agree with the README (currently 40).
    This pins the contract: a future edit that changes the count
    must update README + this assertion together; do not invent a
    new total in docs.
    """
    scenarios = scenarios_v1.all_scenarios()
    assert len(scenarios) == 40


def test_scenario_categories_cover_all_required():
    cats = set(s["category"] for s in scenarios_v1.all_scenarios())
    expected = set(ALLOWED_CATEGORIES)
    assert cats == expected


def test_scenario_schema_rejects_unknown_category():
    bad = {
        "scenario_id": "BAD-1",
        "category": "WRONG",
        "queries": [{"text": "x"}],
    }
    with pytest.raises(ScenarioLoadError):
        scenario_from_dict(bad)


def test_scenario_schema_rejects_empty_queries():
    bad = {
        "scenario_id": "BAD-2",
        "category": "STABLE_FACT",
        "fixtures": [],
        "queries": [],
    }
    with pytest.raises(ScenarioLoadError):
        scenario_from_dict(bad)


def test_scenario_schema_accepts_minimal_valid():
    sc = scenario_from_dict(
        {
            "scenario_id": "MIN-1",
            "category": "STABLE_FACT",
            "queries": [{"text": "what?"}],
        }
    )
    assert sc.scenario_id == "MIN-1"
    assert sc.category == "STABLE_FACT"


def test_scenario_schema_loads_explicit_memories_alias():
    """``explicit_memories`` is the documented compatibility alias of
    ``fixtures`` and must parse with the same shape."""
    raw = {
        "scenario_id": "G5b-EXM-001",
        "category": "STABLE_FACT",
        "explicit_memories": [
            {
                "category": "fact",
                "title": "timezone",
                "content": "Project uses Asia/Shanghai.",
                "tags": ["timezone"],
            }
        ],
        "queries": [{"text": "timezone"}],
    }
    sc = scenario_from_dict(raw)
    assert len(sc.fixtures) == 1
    assert sc.fixtures[0].title == "timezone"
    assert sc.fixtures[0].category == "fact"


def test_scenario_schema_rejects_duplicate_fixtures_and_alias():
    """Specifying both ``fixtures`` and ``explicit_memories`` is
    rejected to prevent silent double-seeding."""
    raw = {
        "scenario_id": "G5b-EXM-002",
        "category": "STABLE_FACT",
        "fixtures": [
            {"category": "fact", "title": "a", "content": "A", "tags": []}
        ],
        "explicit_memories": [
            {"category": "fact", "title": "b", "content": "B", "tags": []}
        ],
        "queries": [{"text": "x"}],
    }
    with pytest.raises(ScenarioLoadError) as exc_info:
        scenario_from_dict(raw)
    assert "fixtures" in str(exc_info.value)
    assert "explicit_memories" in str(exc_info.value)


def test_scenario_schema_loads_sessions_with_conversation_turns():
    """``sessions`` is optional and loads conversation turns."""
    raw = {
        "scenario_id": "G5b-SES-001",
        "category": "STABLE_FACT",
        "queries": [{"text": "x"}],
        "sessions": [
            {
                "session_id": "S1",
                "conversation_turns": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi there"},
                ],
            }
        ],
    }
    sc = scenario_from_dict(raw)
    assert len(sc.sessions) == 1
    s = sc.sessions[0]
    assert s.session_id == "S1"
    assert len(s.conversation_turns) == 2
    assert s.conversation_turns[0].role == "user"
    assert s.conversation_turns[1].content == "hi there"


def test_scenario_schema_loads_sessions_with_turns_alias():
    """``turns`` is the documented alias of ``conversation_turns``."""
    raw = {
        "scenario_id": "G5b-SES-002",
        "category": "STABLE_FACT",
        "queries": [{"text": "x"}],
        "sessions": [
            {
                "session_id": "S1",
                "turns": [
                    {"role": "user", "content": "alias-hello"},
                ],
            }
        ],
    }
    sc = scenario_from_dict(raw)
    assert len(sc.sessions) == 1
    assert sc.sessions[0].conversation_turns[0].content == "alias-hello"


def test_scenario_schema_rejects_duplicate_turn_keys():
    """Both ``conversation_turns`` and ``turns`` in the same session
    must be rejected to avoid silent ambiguity."""
    raw = {
        "scenario_id": "G5b-SES-003",
        "category": "STABLE_FACT",
        "queries": [{"text": "x"}],
        "sessions": [
            {
                "session_id": "S1",
                "conversation_turns": [
                    {"role": "user", "content": "primary"}
                ],
                "turns": [
                    {"role": "user", "content": "alias"}
                ],
            }
        ],
    }
    with pytest.raises(ScenarioLoadError):
        scenario_from_dict(raw)


def test_scenario_schema_rejects_turn_missing_role():
    """A conversation turn without a non-empty role is rejected."""
    raw = {
        "scenario_id": "G5b-SES-004",
        "category": "STABLE_FACT",
        "queries": [{"text": "x"}],
        "sessions": [
            {
                "session_id": "S1",
                "turns": [{"content": "no role"}],
            }
        ],
    }
    with pytest.raises(ScenarioLoadError):
        scenario_from_dict(raw)


# ── runner / metrics ──────────────────────────────────────────────────────


def test_run_scenarios_lane_a_executes_all():
    raw = scenarios_v1.all_scenarios()
    scenarios = [scenario_from_dict(d) for d in raw]
    results, _ = run_scenarios(scenarios, lane="a", limit=5)
    assert len(results) == len(scenarios)


def test_run_scenarios_metrics_no_pipeline_errors():
    """Lane A baseline must NOT raise PIPELINE_ERROR — that's a harness bug."""
    raw = scenarios_v1.all_scenarios()
    scenarios = [scenario_from_dict(d) for d in raw]
    results, query_records = run_scenarios(scenarios, lane="a", limit=5)
    metrics = compute_metrics(results, query_records)
    assert metrics.pipeline_errors == 0


def test_run_scenarios_all_scenarios_have_trace():
    raw = scenarios_v1.all_scenarios()
    scenarios = [scenario_from_dict(d) for d in raw]
    results, _ = run_scenarios(scenarios, lane="a", limit=5)
    for r in results:
        # Every scenario records a per-query trace.
        assert r.per_query is not None
        for q in r.per_query:
            assert isinstance(q.elapsed_ms, float)
            assert q.elapsed_ms >= 0.0


def test_metrics_pass_rate_is_bounded():
    raw = scenarios_v1.all_scenarios()
    scenarios = [scenario_from_dict(d) for d in raw]
    results, query_records = run_scenarios(scenarios, lane="a", limit=5)
    metrics = compute_metrics(results, query_records)
    assert 0.0 <= metrics.scenario_pass_rate <= 1.0
    assert 0.0 <= metrics.must_recall_hit_rate <= 1.0
    assert 0.0 <= metrics.mrr <= 1.0


def test_metrics_per_label_count_sums_to_scenario_count():
    raw = scenarios_v1.all_scenarios()
    scenarios = [scenario_from_dict(d) for d in raw]
    results, query_records = run_scenarios(scenarios, lane="a", limit=5)
    metrics = compute_metrics(results, query_records)
    total = sum(metrics.per_label_count.values())
    # Each scenario contributes exactly one label.
    assert total == metrics.scenario_count


def test_default_recall_limit_is_5():
    """Pin the default recall limit; changing it is a contract change."""
    assert DEFAULT_RECALL_LIMIT == 5


def test_run_scenarios_lane_b_without_live_pg_raises(monkeypatch):
    """Lane B without --live-pg / DSN must raise LaneBDisabled."""
    monkeypatch.delenv("G5B_EVAL_LIVE_PG", raising=False)
    monkeypatch.delenv("G5B_EVAL_LAB_DSN", raising=False)
    with pytest.raises(LaneBDisabled):
        run_scenarios(
            [scenario_from_dict(s) for s in scenarios_v1.all_scenarios()],
            lane="b",
            limit=5,
        )