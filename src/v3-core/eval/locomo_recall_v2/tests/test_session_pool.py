"""Focused tests for session_pool (evaluator-only).

The wrapper depends on ``v3core.pg_pool.PgPool``. These tests
monkey-patch the wrapper's ``_import_pg_pool_class``-equivalent
``_build_pool`` so we can drive a deterministic in-memory pool
that hands out our fake connection. We never connect to PG.
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
    SearchProtocol,
    apply_search_protocol,
)
from eval.locomo_recall_v2.session_pool import (  # noqa: E402
    EvaluatorPoolClosed,
    EvaluatorPoolProtocolError,
    EvaluatorSessionPool,
)


# ---------------------------------------------------------------------
# Fake pool — uses our fake connection from test_search_protocol
# ---------------------------------------------------------------------


class FakeCursor:
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
            if len(params_tuple) == 3:
                name, value, _ = params_tuple
            elif len(params_tuple) == 2:
                name, value = params_tuple
            else:
                raise RuntimeError(f"set_config expects 2 or 3 params, got {len(params_tuple)}")
            self._conn.gucs[str(name)] = str(value)
            self._pending = (str(value),)
            return
        if text.startswith("SELECT CURRENT_SETTING("):
            name = params_tuple[0]
            value = self._conn.gucs.get(str(name))
            self._pending = (str(value),) if value is not None else None
            return
        # GUC-registration preflight — the G6C-B0 contract
        # uses ``SELECT 1 FROM pg_settings WHERE name = %s
        # LIMIT 1`` to distinguish a real planner-affecting
        # GUC from a custom-variable echo.  Default: every
        # GUC seen by this fake is "registered" so the
        # preflight returns AVAILABLE.
        if "FROM PG_SETTINGS" in text:
            if not params_tuple:
                raise RuntimeError(
                    f"pg_settings lookup expects a name "
                    f"parameter, got {sql!r}"
                )
            name = str(params_tuple[0])
            registered = name in self._conn.registered_gucs
            self._pending = (1,) if registered else None
            return
        raise RuntimeError(f"FakeCursor unexpected: {sql!r}")

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
        # ``apply_search_protocol`` now ALWAYS reads back
        # ``ivfflat.probes`` via ``current_setting(..., true)``
        # — seed the PG default so the fake can satisfy the
        # read-back during ``lease()``.
        self.gucs.setdefault("ivfflat.probes", "1")
        # Default: every GUC the fake has ever seen is
        # "registered" in pg_settings — i.e. the planner
        # reads it.  Tests that want to exercise the
        # unregistered path can override.
        if registered_gucs is None:
            self.registered_gucs = {
                "enable_indexscan",
                "ivfflat.probes",
            }
        else:
            self.registered_gucs = set(registered_gucs)
        self.cursors = []
        self.closed = False

    def cursor(self):
        cur = FakeCursor(self)
        self.cursors.append(cur)
        return cur

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        self.closed = True


class _FakeInnerLease:
    def __init__(self, conn):
        self.connection = conn
        self.closed = False

    def close(self):
        self.closed = True


class FakePgPool:
    """Stand-in for v3core.pg_pool.PgPool with the same lease shape."""

    def __init__(self, connect, max_connections, min_connections=0):
        self._connect = connect
        self._max = max_connections
        self._min = min_connections
        self._idle: list[FakeConnection] = []
        self.closed = False
        for _ in range(min_connections):
            try:
                self._idle.append(self._connect())
            except Exception:
                pass

    def lease(self, timeout=None):
        if self._idle:
            conn = self._idle.pop()
        else:
            conn = self._connect()
        return _FakeInnerLease(conn)

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------


def _factory():
    return FakeConnection()


def _build_pool_wrapper(pool_cls):
    """Return a function the wrapper can call instead of importing PgPool."""

    def _build(self):
        return pool_cls(
            connect=self._factory,
            max_connections=self._max_connections,
            min_connections=self._min_connections,
        )

    return _build


def _patch_pool(monkeypatch):
    """Patch :class:`EvaluatorSessionPool._build_pool` so it builds our
    in-process fake pool instead of importing ``v3core.pg_pool.PgPool``."""

    def _build(self):
        return FakePgPool(
            connect=self._factory,
            max_connections=self._max_connections,
            min_connections=self._min_connections,
        )

    monkeypatch.setattr(EvaluatorSessionPool, "_build_pool", _build)


# ---------------------------------------------------------------------
# Lease + protocol verification
# ---------------------------------------------------------------------


class TestEvaluatorSessionPool:
    def test_construction_lease_verifies(self, monkeypatch):
        _patch_pool(monkeypatch)
        p = EvaluatorSessionPool(
            factory=_factory,
            policy=SearchProtocol(mode="exact"),
            max_connections=2,
        )
        try:
            state = p.state()
            assert state.closed is False
            assert state.leases_served == 0
            # Now lease and confirm a verified connection comes back.
            lease = p.lease()
            try:
                conn = lease.connection
                # enable_indexscan is now off.
                assert conn.gucs["enable_indexscan"] == "off"
            finally:
                lease.close()
            assert p.state().leases_served == 1
        finally:
            p.close()

    def test_lease_re_applies_protocol_each_time(self, monkeypatch):
        """A recycled connection must see the protocol re-applied.
        The FakeConnection in this test returns its existing
        ``enable_indexscan='on'`` state when first leased; the
        wrapper must issue SETs again on the next lease."""

        _patch_pool(monkeypatch)

        def _factory_with_state():
            conn = FakeConnection(gucs={"enable_indexscan": "on"})
            return conn

        p = EvaluatorSessionPool(
            factory=_factory_with_state,
            policy=SearchProtocol(mode="exact"),
            max_connections=1,
        )
        try:
            lease = p.lease()
            try:
                # After the lease, the connection must show enable_indexscan=off.
                assert lease.connection.gucs["enable_indexscan"] == "off"
            finally:
                lease.close()
            # Second lease must produce a connection that ALSO shows off,
            # even if the underlying state machine reset to on.
            lease2 = p.lease()
            try:
                assert lease2.connection.gucs["enable_indexscan"] == "off"
            finally:
                lease2.close()
        finally:
            p.close()

    def test_lease_failure_fails_closed(self, monkeypatch):
        """A factory that returns a broken cursor → lease raises
        EvaluatorPoolClosed, the pool marks itself closed."""

        def _broken_factory():
            class C:
                def cursor(self_inner):
                    raise RuntimeError("cursor boom")
            return C()

        monkeypatch.setattr(
            EvaluatorSessionPool, "_build_pool", lambda self: FakePgPool(
                connect=_broken_factory,
                max_connections=self._max_connections,
                min_connections=self._min_connections,
            )
        )
        p = EvaluatorSessionPool(
            factory=_broken_factory,
            policy=SearchProtocol(mode="exact"),
            max_connections=1,
        )
        try:
            with pytest.raises((EvaluatorPoolClosed, EvaluatorPoolProtocolError)):
                p.lease()
        finally:
            p.close()

    def test_close_is_idempotent(self, monkeypatch):
        _patch_pool(monkeypatch)
        p = EvaluatorSessionPool(
            factory=_factory,
            policy=SearchProtocol(mode="exact"),
            max_connections=1,
        )
        p.close()
        p.close()  # idempotent
        assert p.state().closed is True
        with pytest.raises(EvaluatorPoolClosed):
            p.lease()

    def test_invalid_policy_type(self, monkeypatch):
        _patch_pool(monkeypatch)
        with pytest.raises(TypeError):
            EvaluatorSessionPool(
                factory=_factory,
                policy="exact",  # type: ignore[arg-type]
                max_connections=1,
            )

    def test_invalid_max_connections(self, monkeypatch):
        _patch_pool(monkeypatch)
        with pytest.raises(ValueError):
            EvaluatorSessionPool(
                factory=_factory,
                policy=SearchProtocol(mode="exact"),
                max_connections=0,
            )

    def test_non_callable_factory(self, monkeypatch):
        with pytest.raises(TypeError):
            EvaluatorSessionPool(
                factory="not callable",  # type: ignore[arg-type]
                policy=SearchProtocol(mode="exact"),
                max_connections=1,
            )
