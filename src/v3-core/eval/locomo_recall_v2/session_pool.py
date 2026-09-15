"""G6C-B0 evaluator-only session pool — minimum reusable wrapper.

Wraps :class:`v3core.pg_pool.PgPool` so every physical
connection is configured and verified by
:func:`eval.locomo_recall_v2.search_protocol.apply_search_protocol`
before being handed to the recall engine.

The wrapper exists so:

  * the ``semantic`` stage can inject a single
    ``PgEmbedStore(pool=...)`` instance whose every leased
    connection has the strict exact/ANN policy applied;
  * the planner sees the verified GUC values at every lease,
    not just the first one.

Public surface (the minimum):

  * :class:`EvaluatorSessionPool` — the wrapper.
  * :class:`EvaluatorPoolClosed` / :class:`EvaluatorPoolProtocolError`
    — error types surfaced to callers.
  * :class:`EvaluatorPoolState` — sanitised state snapshot.

Production-only — no test fakes here. Tests under
``tests/test_session_pool.py`` carry their own minimal stub.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .search_protocol import (
    SearchProtocol,
    SearchProtocolError,
    apply_search_protocol,
)


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------


class EvaluatorPoolClosed(RuntimeError):
    """Raised when the evaluator pool is asked to lease after ``close()``."""


class EvaluatorPoolProtocolError(RuntimeError):
    """Raised when the search-protocol round-trip failed."""


# ---------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluatorPoolState:
    """Sanitised snapshot of the pool's internal state.

    Intentionally exposes no DSN parts, no passwords, no
    hostnames. Safe to write verbatim to the sanitised manifest.
    """

    max_connections: int
    min_connections: int
    leases_served: int
    leases_failed: int
    closed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_connections": int(self.max_connections),
            "min_connections": int(self.min_connections),
            "leases_served": int(self.leases_served),
            "leases_failed": int(self.leases_failed),
            "closed": bool(self.closed),
        }


# ---------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------


class EvaluatorSessionPool:
    """Evaluator-only session pool with strict exact/ANN policy.

    Parameters
    ----------
    factory:
        Zero-argument callable returning a new psycopg2-style
        connection. The factory MUST be idempotent — the
        wrapper calls it once per physical connection.
    policy:
        The :class:`SearchProtocol` every leased connection
        must round-trip. Construction fails closed: a
        protocol failure tears the pool down before the first
        ``lease()`` returns.
    max_connections, min_connections:
        Forwarded to :class:`PgPool`.
    """

    def __init__(
        self,
        *,
        factory: Callable[[], Any],
        policy: SearchProtocol,
        max_connections: int = 1,
        min_connections: int = 0,
    ) -> None:
        if not callable(factory):
            raise TypeError(
                "EvaluatorSessionPool: factory must be a zero-argument callable"
            )
        if not isinstance(policy, SearchProtocol):
            raise TypeError(
                f"EvaluatorSessionPool: policy must be SearchProtocol; "
                f"got {type(policy).__name__}"
            )
        if int(max_connections) < 1:
            raise ValueError(
                f"EvaluatorSessionPool: max_connections must be >= 1; "
                f"got {max_connections!r}"
            )
        if int(min_connections) < 0 or int(min_connections) > int(max_connections):
            raise ValueError(
                f"EvaluatorSessionPool: min_connections must be in "
                f"[0, max_connections]; got {min_connections!r}"
            )

        self._factory = factory
        self._policy = policy
        self._max_connections = int(max_connections)
        self._min_connections = int(min_connections)
        self._closed = False
        self._leases_served = 0
        self._leases_failed = 0
        self._pool: Any = None

    # ------------------------------------------------------------------
    # Lease
    # ------------------------------------------------------------------

    def lease(self, *, timeout: float | None = None) -> Any:
        """Lease a verified connection from the pool.

        The search protocol is applied and verified on every
        lease — a recycled connection that the pool returned
        cannot leak a degraded policy to the next caller.
        """

        if self._closed:
            raise EvaluatorPoolClosed(
                "evaluator session pool: lease requested on closed pool"
            )
        if self._pool is None:
            self._pool = self._build_pool()
        try:
            inner = (
                self._pool.lease(timeout=timeout)
                if timeout is not None
                else self._pool.lease()
            )
        except Exception as exc:
            self._leases_failed += 1
            raise EvaluatorPoolClosed(
                f"evaluator session pool: lease failed: "
                f"{type(exc).__name__} (message suppressed)"
            ) from exc

        conn = getattr(inner, "connection", None)
        if conn is None:
            conn = inner
        try:
            apply_search_protocol(conn, self._policy)
        except SearchProtocolError as exc:
            self._leases_failed += 1
            try:
                inner.close()
            except Exception:
                pass
            self._closed = True
            raise EvaluatorPoolProtocolError(
                f"evaluator session pool: search-protocol verification "
                f"failed: {exc}"
            ) from exc
        self._leases_served += 1
        return _VerifiedLease(inner)

    # ------------------------------------------------------------------
    # State + lifecycle
    # ------------------------------------------------------------------

    def state(self) -> EvaluatorPoolState:
        return EvaluatorPoolState(
            max_connections=self._max_connections,
            min_connections=self._min_connections,
            leases_served=self._leases_served,
            leases_failed=self._leases_failed,
            closed=self._closed,
        )

    @property
    def policy(self) -> SearchProtocol:
        return self._policy

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pool = self._pool
        self._pool = None
        if pool is None:
            return
        try:
            close = getattr(pool, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_pool(self) -> Any:
        try:
            from v3core.pg_pool import PgPool
        except ImportError as exc:
            raise EvaluatorPoolClosed(
                "evaluator session pool: cannot import v3core.pg_pool.PgPool"
            ) from exc
        try:
            return PgPool(
                connect=self._factory,
                max_connections=self._max_connections,
                min_connections=self._min_connections,
            )
        except Exception as exc:
            raise EvaluatorPoolClosed(
                f"evaluator session pool: PgPool construction failed: "
                f"{type(exc).__name__} (message suppressed)"
            ) from exc


class _VerifiedLease:
    """Lease handle that exposes ``.connection`` + ``.close()``."""

    __slots__ = ("_inner", "_closed")

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._closed = False

    @property
    def connection(self) -> Any:
        if self._closed:
            raise EvaluatorPoolClosed(
                "evaluator session pool: connection accessed on closed lease"
            )
        conn = getattr(self._inner, "connection", None)
        if conn is None:
            return self._inner
        return conn

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._inner, "close", None)
            if callable(close):
                close()
        except Exception:
            pass


__all__ = [
    "EvaluatorPoolClosed",
    "EvaluatorPoolProtocolError",
    "EvaluatorPoolState",
    "EvaluatorSessionPool",
]
