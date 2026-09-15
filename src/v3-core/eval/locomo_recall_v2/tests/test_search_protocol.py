"""Focused tests for search_protocol (evaluator-only).

These tests do NOT touch PG. They build a tiny in-process
fake connection that records every ``set_config`` /
``current_setting`` round-trip so we can assert:

  * exact mode forces ``enable_indexscan='off'`` and the
    connection sees ``off`` after the SET;
  * ann mode forces ``enable_indexscan='on'``;
  * explicit ivfflat.probes round-trips in ann mode;
  * probes without ann mode is refused at parse time;
  * probes without ``--search-mode ann`` at the CLI layer is
    refused at the policy-validation step;
  * a fake connection whose current_setting returns ``'on'``
    after a SET to ``'off'`` fails closed with
    ``SearchProtocolError``.
"""
from __future__ import annotations

import os
import sys

import pytest


_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval.locomo_recall_v2.search_protocol import (  # noqa: E402
    ALLOWED_SEARCH_MODES,
    PROBE_AVAILABILITY_AVAILABLE,
    PROBE_AVAILABILITY_UNAVAILABLE,
    SEARCH_MODE_ANN,
    SEARCH_MODE_EXACT,
    SearchProtocol,
    SearchProtocolError,
    SearchProtocolReport,
    apply_search_protocol,
)


# ---------------------------------------------------------------------
# Local fake — DB-API shaped, understands set_config + current_setting
# ---------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._pending = None
        self.log = []
        self.closed = False

    def execute(self, sql, params=None):
        self.log.append((str(sql), tuple(params) if params else ()))
        text = str(sql).lstrip().upper()
        params_tuple = tuple(params) if params else ()
        if text.startswith("SELECT SET_CONFIG("):
            # Real psycopg2 binding passes ``(name, value, is_local)``
            # for ``set_config(%s, %s, false)``. The wrapper may also
            # pass just ``(name, value)`` in tests; both shapes are
            # accepted here.
            if len(params_tuple) == 3:
                name, value, _ = params_tuple
            elif len(params_tuple) == 2:
                name, value = params_tuple
            else:
                raise SearchProtocolError(
                    f"FakeCursor.execute: set_config expects 2 or 3 params, "
                    f"got {len(params_tuple)} on {sql!r}"
                )
            self._conn.gucs[str(name)] = str(value)
            self._pending = (str(value),)
            return
        if text.startswith("SELECT CURRENT_SETTING("):
            name = params_tuple[0]
            value = self._conn.gucs.get(str(name))
            self._pending = (str(value),) if value is not None else None
            return
        # GUC-registration preflight — the new contract uses
        # ``SELECT 1 FROM pg_settings WHERE name = %s LIMIT 1`` to
        # distinguish a real planner-affecting GUC from a
        # custom-variable echo.
        if "FROM PG_SETTINGS" in text:
            if not params_tuple:
                raise SearchProtocolError(
                    f"FakeCursor.execute: pg_settings lookup expects "
                    f"a name parameter, got {sql!r}"
                )
            name = str(params_tuple[0])
            registered = name in self._conn.registered_gucs
            self._pending = (1,) if registered else None
            return
        raise SearchProtocolError(f"FakeCursor unexpected: {sql!r}")

    def fetchone(self):
        return self._pending

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(
        self,
        gucs=None,
        *,
        registered_gucs: set[str] | frozenset[str] | None = None,
    ):
        self.gucs = dict(gucs or {})
        self.gucs.setdefault("enable_indexscan", "on")
        # ``apply_search_protocol`` now ALWAYS reads back the
        # current ``ivfflat.probes`` GUC via
        # ``current_setting(..., true)`` — seed the PG default
        # (1) so the manifest-truth path can produce an
        # observed value during fake-driven tests.  The seed
        # only affects the read-back; whether the GUC is
        # *registered* is governed by ``registered_gucs``,
        # which defaults to {"enable_indexscan",
        # "ivfflat.probes"} to mirror a real pgvector build
        # that exposes the planner option.
        self.gucs.setdefault("ivfflat.probes", "1")
        if registered_gucs is None:
            self.registered_gucs = {
                "enable_indexscan",
                "ivfflat.probes",
            }
        else:
            # Allow tests to opt out of registration for
            # specific names — e.g. an unregistered
            # ``ivfflat.probes``.
            self.registered_gucs = set(registered_gucs)
        self.cursors = []

    def cursor(self):
        cur = _FakeCursor(self)
        self.cursors.append(cur)
        return cur


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------


class TestSearchProtocolParse:
    def test_default_mode_is_exact_for_legacy_callers(self):
        # ``SearchProtocol.parse`` keeps legacy call-sites
        # working when ``mode=None`` — but the CLI itself
        # never relies on this fallback (it defaults to
        # ``"ann"``).
        p = SearchProtocol.parse(mode=None)
        assert p.mode == SEARCH_MODE_EXACT
        assert p.probes is None

    def test_ann_with_explicit_probes(self):
        p = SearchProtocol.parse(mode="ann", probes=4)
        assert p.mode == SEARCH_MODE_ANN
        assert p.probes == 4

    def test_unknown_mode_raises(self):
        with pytest.raises(SearchProtocolError):
            SearchProtocol.parse(mode="bogus")

    def test_negative_probes_raises(self):
        with pytest.raises(SearchProtocolError):
            SearchProtocol.parse(mode="ann", probes=-1)

    def test_bool_probes_raises(self):
        with pytest.raises(SearchProtocolError):
            SearchProtocol.parse(mode="ann", probes=True)

    def test_allowed_modes_constant(self):
        assert ALLOWED_SEARCH_MODES == frozenset({"exact", "ann"})


# ---------------------------------------------------------------------
# apply_search_protocol — exact guard
# ---------------------------------------------------------------------


class TestApplyExactGuard:
    def test_exact_forces_enable_indexscan_off(self):
        conn = FakeConnection(gucs={"enable_indexscan": "on"})
        report = apply_search_protocol(conn, SearchProtocol(mode="exact"))
        assert report.mode == "exact"
        assert report.enable_indexscan == "off"
        # The cursor recorded a set_config + current_setting round-trip.
        log_upper = [c[0].upper() for c in conn.cursors[0].log]
        assert any("SET_CONFIG" in k for k in log_upper)
        # ``enable_indexscan`` always issues a current_setting
        # read-back; ``ivfflat.probes`` is also read back
        # unconditionally so the manifest records what the
        # planner actually saw.
        cur = conn.cursors[0]
        enable_reads = [
            (sql, params) for (sql, params) in cur.log
            if "current_setting" in str(sql).lower()
            and "enable_indexscan" in tuple(params or ())
        ]
        assert len(enable_reads) == 1, (
            "apply_search_protocol must issue exactly one "
            "current_setting(..., true) read-back for "
            "enable_indexscan; got "
            f"{enable_reads!r}"
        )

    def test_exact_round_trip_failure_raises(self):
        """A fake connection whose current_setting still returns ``on``
        after we issued set_config('enable_indexscan', 'off') fails
        closed."""

        class StubbornCursor(_FakeCursor):
            def execute(self, sql, params=None):
                text = str(sql).lstrip().upper()
                if text.startswith("SELECT CURRENT_SETTING("):
                    # Always report ``on`` regardless of state.
                    self._pending = ("on",)
                    return
                super().execute(sql, params)

        class StubbornConn(FakeConnection):
            def cursor(self):
                cur = StubbornCursor(self)
                self.cursors.append(cur)
                return cur

        conn = StubbornConn()
        with pytest.raises(SearchProtocolError):
            apply_search_protocol(conn, SearchProtocol(mode="exact"))


# ---------------------------------------------------------------------
# apply_search_protocol — ann / probe guard
# ---------------------------------------------------------------------


class TestApplyAnnGuard:
    def test_ann_forces_enable_indexscan_on(self):
        conn = FakeConnection()
        report = apply_search_protocol(conn, SearchProtocol(mode="ann"))
        assert report.mode == "ann"
        assert report.enable_indexscan == "on"
        # ``apply_search_protocol`` ALWAYS reads back the
        # current ``ivfflat.probes`` GUC value so the manifest
        # is truthful even when no explicit override is set.
        assert report.ivfflat_probes == "1"
        assert report.verified_probes is None

    def test_ann_with_probes_round_trips(self):
        conn = FakeConnection()
        report = apply_search_protocol(
            conn, SearchProtocol(mode="ann", probes=7)
        )
        assert report.enable_indexscan == "on"
        assert report.verified_probes == 7
        # 1 SET + 1 read for enable_indexscan, 1 SET + 1 read for probes.
        log_upper = [c[0].upper() for c in conn.cursors[0].log]
        assert sum("SET_CONFIG" in k for k in log_upper) == 2
        assert sum("CURRENT_SETTING" in k for k in log_upper) == 2

    def test_probes_round_trip_failure_raises(self):
        class StubbornCursor(_FakeCursor):
            def execute(self, sql, params=None):
                text = str(sql).lstrip().upper()
                if text.startswith("SELECT CURRENT_SETTING("):
                    # Always report 99 regardless of state.
                    self._pending = ("99",)
                    return
                super().execute(sql, params)

        class StubbornConn(FakeConnection):
            def cursor(self):
                cur = StubbornCursor(self)
                self.cursors.append(cur)
                return cur

        conn = StubbornConn()
        with pytest.raises(SearchProtocolError):
            apply_search_protocol(
                conn, SearchProtocol(mode="ann", probes=3)
            )


# ---------------------------------------------------------------------
# Cursor error → SearchProtocolError, never a raw exception
# ---------------------------------------------------------------------


class TestCursorErrors:
    def test_conn_cursor_failure_raises(self):
        class BrokenConn:
            def cursor(self):
                raise RuntimeError("boom")

        with pytest.raises(SearchProtocolError):
            apply_search_protocol(BrokenConn(), SearchProtocol(mode="exact"))

    def test_set_config_failure_raises(self):
        class FailingCursor(_FakeCursor):
            def execute(self, sql, params=None):
                text = str(sql).lstrip().upper()
                if text.startswith("SELECT SET_CONFIG("):
                    raise RuntimeError("set_config failed")
                super().execute(sql, params)

        class C:
            def cursor(self_inner):
                cur = FailingCursor(self_inner)
                return cur

        with pytest.raises(SearchProtocolError):
            apply_search_protocol(C(), SearchProtocol(mode="exact"))


# ---------------------------------------------------------------------
# Manifest truth — ``ivfflat.probes`` always read back via
# ``current_setting(..., true)``; absent probes are observed,
# explicit probes are verified.
# ---------------------------------------------------------------------


class TestManifestTruth:
    def test_ann_no_explicit_probes_observes_current_guc(self):
        """``apply_search_protocol(SearchProtocol(mode='ann'))``
        ALWAYS reads back the current ``ivfflat.probes`` GUC
        via ``current_setting(..., true)`` and stamps the
        observed value in the report — never silently None.
        """

        conn = FakeConnection(
            gucs={"enable_indexscan": "on", "ivfflat.probes": "1"}
        )
        report = apply_search_protocol(
            conn, SearchProtocol(mode="ann")
        )
        # The observed GUC string is preserved verbatim.
        assert report.ivfflat_probes == "1"
        # ``verified_probes`` stays None — there was no
        # requested value to compare against.
        assert report.verified_probes is None
        # The cursor recorded a ``current_setting`` round-trip
        # for ``ivfflat.probes`` even though no SET was issued
        # (manifest truth contract).
        cur = conn.cursors[0]
        probes_reads = [
            (sql, params) for (sql, params) in cur.log
            if "current_setting" in str(sql).lower()
            and "ivfflat.probes" in tuple(params or ())
        ]
        assert probes_reads, (
            "apply_search_protocol must read back ivfflat.probes via "
            "current_setting(..., true) even when no explicit override "
            "is set (manifest truth contract)."
        )

    def test_ann_with_explicit_probes_round_trips_and_verifies(self):
        conn = FakeConnection()
        report = apply_search_protocol(
            conn, SearchProtocol(mode="ann", probes=11)
        )
        assert report.ivfflat_probes == "11"
        assert report.verified_probes == 11

    def test_exact_mode_rejects_explicit_probes(self):
        """Exact mode + explicit probes must be rejected at
        ``apply_search_protocol`` (the second guard that
        closes the seam — ``__post_init__`` permits the
        shape so callers can build the policy from a
        declarative source; the seam closes here so the
        planner never sees a contradictory contract).
        """

        # Bypass ``__post_init__`` via ``object.__new__`` so
        # we exercise the runtime guard inside
        # ``apply_search_protocol`` without the dataclass
        # ``frozen=True`` validation getting in the way.
        policy = SearchProtocol(mode="exact", probes=4)
        conn = FakeConnection()
        with pytest.raises(SearchProtocolError):
            apply_search_protocol(conn, policy)

    def test_exact_mode_observed_probes_recorded(self):
        """In exact mode with no explicit probes the
        ``ivfflat.probes`` GUC is still observed — manifest
        truth contract is global, not ann-mode-only.
        """

        conn = FakeConnection(
            gucs={"enable_indexscan": "off", "ivfflat.probes": "1"}
        )
        report = apply_search_protocol(
            conn, SearchProtocol(mode="exact")
        )
        # enable_indexscan was SET to off and verified.
        assert report.enable_indexscan == "off"
        # ivfflat.probes was READ BACK but never SET — the
        # observed GUC string is stamped verbatim.
        assert report.ivfflat_probes == "1"
        assert report.verified_probes is None
        cur = conn.cursors[0]
        probes_reads = [
            (sql, params) for (sql, params) in cur.log
            if "current_setting" in str(sql).lower()
            and "ivfflat.probes" in tuple(params or ())
        ]
        assert probes_reads


# ---------------------------------------------------------------------
# GUC-registration contract — pg_settings availability check.
#
# Empirical evidence (disposable
# ``pgvector/pgvector:pg17`` 0.8.6): ``pg_settings`` carries NO
# row for ``ivfflat.probes``; ``current_setting(..., true)``
# returns NULL; yet ``set_config('ivfflat.probes','1',false)``
# followed by ``current_setting(..., true)`` round-trips ``'1'``
# — a custom-GUC echo the planner never reads.
#
# The classes below pin the new contract:
#
#   * Explicit ``probes`` + GUC registered: round-trip is real,
#     fail-closed on mismatch (unchanged from before).
#   * Explicit ``probes`` + GUC unregistered: REFUSED — no
#     custom-GUC echo fallback.
#   * Implicit probes (any mode) + GUC unregistered: no failure;
#     ``probe_availability='UNAVAILABLE'``; never invent a
#     numeric default.
#   * Implicit probes (any mode) + GUC registered: existing
#     read-back semantics; ``probe_availability='AVAILABLE'``.
# ---------------------------------------------------------------------


class TestGucRegistration:
    def test_explicit_probes_registered_round_trips_and_marks_available(self):
        """Happy-path ANN explicit probes against a registered
        GUC: SET + verify round-trip is real, the report stamps
        ``probe_availability='AVAILABLE'``.
        """

        conn = FakeConnection()  # registered by default
        report = apply_search_protocol(
            conn, SearchProtocol(mode="ann", probes=3)
        )
        assert report.verified_probes == 3
        assert report.probe_availability == PROBE_AVAILABILITY_AVAILABLE
        assert report.ivfflat_probes == "3"

    def test_implicit_probes_registered_marks_available_and_observes(self):
        """Implicit probes + registered GUC: report stamps
        ``probe_availability='AVAILABLE'`` and the observed
        GUC string is preserved verbatim.
        """

        conn = FakeConnection(
            gucs={"enable_indexscan": "on", "ivfflat.probes": "1"}
        )
        report = apply_search_protocol(
            conn, SearchProtocol(mode="ann")
        )
        assert report.probe_availability == PROBE_AVAILABILITY_AVAILABLE
        assert report.ivfflat_probes == "1"
        assert report.verified_probes is None

    def test_explicit_probes_unregistered_fails_closed(self):
        """The critical contract: explicit ``--ivfflat-probes``
        against a connection whose ``pg_settings`` carries no
        row for ``ivfflat.probes`` MUST be refused with
        :class:`SearchProtocolError`.  A fake
        ``set_config`` + ``current_setting`` echo of ``'1'``
        (the custom-GUC round-trip) must NOT be treated as a
        successful round-trip.
        """

        # ``enable_indexscan`` is registered, but
        # ``ivfflat.probes`` is NOT — exactly the
        # ``pgvector/pgvector:pg17`` 0.8.6 disposable-lab
        # condition.
        conn = FakeConnection(
            gucs={"enable_indexscan": "on", "ivfflat.probes": "1"},
            registered_gucs={"enable_indexscan"},
        )
        with pytest.raises(SearchProtocolError):
            apply_search_protocol(
                conn, SearchProtocol(mode="ann", probes=1)
            )

    def test_explicit_probes_unregistered_does_not_fall_back_to_echo(self):
        """Make the fake echo ``'1'`` on
        ``current_setting('ivfflat.probes', true)`` to prove
        the new contract refuses the echo rather than
        accepting it as a successful round-trip.

        Without the pg_settings preflight the previous
        implementation would have accepted this echo as a
        real round-trip and stamped
        ``verified_probes=1``.  The new contract fails
        closed.
        """

        class EchoingCursor(_FakeCursor):
            def execute(self, sql, params=None):
                text = str(sql).lstrip().upper()
                if text.startswith("SELECT CURRENT_SETTING("):
                    # Lie: pretend the echo is real even
                    # though pg_settings has no row for
                    # ``ivfflat.probes``.
                    self._pending = ("1",)
                    return
                super().execute(sql, params)

        class EchoingConn(FakeConnection):
            def cursor(self):
                cur = EchoingCursor(self)
                self.cursors.append(cur)
                return cur

        conn = EchoingConn(
            gucs={"enable_indexscan": "on", "ivfflat.probes": "1"},
            registered_gucs={"enable_indexscan"},
        )
        with pytest.raises(SearchProtocolError):
            apply_search_protocol(
                conn, SearchProtocol(mode="ann", probes=1)
            )

    def test_implicit_probes_unregistered_in_ann_does_not_fail(self):
        """Implicit probes (ann, no override) + unregistered
        GUC MUST NOT fail solely on GUC absence.  The
        report stamps ``probe_availability='UNAVAILABLE'``
        and leaves ``ivfflat_probes=None``; the auditor can
        then decide what to do.
        """

        conn = FakeConnection(
            gucs={"enable_indexscan": "on", "ivfflat.probes": "1"},
            registered_gucs={"enable_indexscan"},
        )
        report = apply_search_protocol(
            conn, SearchProtocol(mode="ann")
        )
        assert report.probe_availability == PROBE_AVAILABILITY_UNAVAILABLE
        # We never claim a numeric default for an
        # unregistered GUC.
        assert report.ivfflat_probes is None
        assert report.verified_probes is None
        # enable_indexscan round-trips normally.
        assert report.enable_indexscan == "on"

    def test_implicit_probes_unregistered_in_exact_does_not_fail(self):
        """Exact mode + unregistered GUC MUST NOT fail.
        enable_indexscan='off' is set+verified, and the
        report stamps ``probe_availability='UNAVAILABLE'``.
        """

        conn = FakeConnection(
            gucs={"enable_indexscan": "on", "ivfflat.probes": "1"},
            registered_gucs={"enable_indexscan"},
        )
        report = apply_search_protocol(
            conn, SearchProtocol(mode="exact")
        )
        assert report.probe_availability == PROBE_AVAILABILITY_UNAVAILABLE
        assert report.ivfflat_probes is None
        assert report.verified_probes is None
        assert report.enable_indexscan == "off"

    def test_explicit_probes_unregistered_raises_before_set_config(self):
        """When the preflight sees an unregistered GUC,
        ``apply_search_protocol`` MUST NOT issue a
        ``set_config('ivfflat.probes', ...)`` — that would
        be the very custom-GUC echo we are trying to
        refuse.
        """

        conn = FakeConnection(
            gucs={"enable_indexscan": "on", "ivfflat.probes": "1"},
            registered_gucs={"enable_indexscan"},
        )
        with pytest.raises(SearchProtocolError):
            apply_search_protocol(
                conn, SearchProtocol(mode="ann", probes=1)
            )
        cur = conn.cursors[0]
        probes_sets = [
            (sql, params) for (sql, params) in cur.log
            if "set_config" in str(sql).lower()
            and "ivfflat.probes" in tuple(params or ())
        ]
        assert probes_sets == [], (
            "apply_search_protocol must NOT issue set_config "
            "for an unregistered GUC; got "
            f"{probes_sets!r}"
        )

    def test_report_observed_value_carries_probe_availability(self):
        """``SearchProtocolReport.observed_value`` carries
        ``probe_availability`` so the CLI can stamp it in
        the manifest without re-deriving it.
        """

        conn = FakeConnection()  # registered
        report = apply_search_protocol(
            conn, SearchProtocol(mode="ann", probes=2)
        )
        snap = report.observed_value
        assert snap["probe_availability"] == PROBE_AVAILABILITY_AVAILABLE
        assert snap["verified_probes"] == 2

    def test_constants_exported(self):
        from eval.locomo_recall_v2 import search_protocol as sp
        assert sp.PROBE_AVAILABILITY_AVAILABLE == "AVAILABLE"
        assert sp.PROBE_AVAILABILITY_UNAVAILABLE == "UNAVAILABLE"
        assert "PROBE_AVAILABILITY_AVAILABLE" in sp.__all__
        assert "PROBE_AVAILABILITY_UNAVAILABLE" in sp.__all__

    def test_search_protocol_report_default_availability(self):
        """``SearchProtocolReport`` defaults
        ``probe_availability='AVAILABLE'`` so direct
        construction (without going through
        ``apply_search_protocol``) stays honest for the
        registered-GUC case.
        """

        rep = SearchProtocolReport(
            mode="ann",
            enable_indexscan="on",
            ivfflat_probes="1",
            verified_probes=None,
        )
        assert rep.probe_availability == PROBE_AVAILABILITY_AVAILABLE
