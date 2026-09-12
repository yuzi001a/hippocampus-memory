"""Explicit deadline primitives for P1.2-A1 prefetch containment.

A deadline is an absolute ``time.monotonic()`` value.  It is passed through
call arguments; this module deliberately has no global or thread-local state.
"""
from __future__ import annotations

import copy
import time
from typing import Any, Optional


# Hermes MemoryManager's external provider join timeout is 8.0s.  Keep a
# single internal budget below it so v3 can unwind its SQL/lease path before
# the outer worker returns an empty result.  This is a control-plane constant,
# not an algorithm or candidate-size limit.
PREFETCH_EXTERNAL_TIMEOUT_SECONDS = 8.0
INTERNAL_PREFETCH_BUDGET_SECONDS = 6.5

# A synchronous connection factory cannot be interrupted safely from the
# outside.  Runtime therefore declares the driver's physical connect bound
# explicitly and PgPool uses the same value as a pre-entry admission check.
# 5.0s leaves 1.5s before the 6.5s internal deadline, which itself leaves
# another 1.5s before Hermes' external 8.0s join timeout.
PREFETCH_CONNECT_TIMEOUT_SECONDS = 5


class PrefetchDeadlineExceeded(Exception):
    """The v3 prefetch budget is exhausted.

    This is deliberately a normal ``Exception`` so callers can handle it
    explicitly.  Every fallback that catches ``Exception`` in the prefetch
    chain must re-raise this type; only the Hermes provider boundary converts
    it to the provider's empty-result response.
    """

    def __init__(
        self,
        message: str = "prefetch internal deadline exceeded",
        *,
        deadline: "PrefetchDeadline | None" = None,
        context: str = "",
    ) -> None:
        self.deadline = deadline
        self.context = context
        super().__init__(message)


class PrefetchDeadline:
    """One absolute monotonic deadline shared by one prefetch invocation."""

    __slots__ = ("_deadline", "_started_at", "_budget_s")

    def __init__(
        self,
        deadline: Optional[float] = None,
        *,
        budget_s: Optional[float] = None,
    ) -> None:
        if deadline is not None and budget_s is not None:
            raise ValueError("pass either deadline or budget_s, not both")
        self._started_at = time.monotonic()
        if deadline is None and budget_s is None:
            self._deadline = None
            self._budget_s = None
        elif deadline is not None:
            self._deadline = float(deadline)
            self._budget_s = max(0.0, self._deadline - self._started_at)
        else:
            self._budget_s = float(budget_s)
            self._deadline = self._started_at + self._budget_s

    @property
    def deadline(self) -> Optional[float]:
        """Absolute monotonic timestamp, or None for the legacy no-deadline path."""
        return self._deadline

    @property
    def budget_s(self) -> Optional[float]:
        return self._budget_s

    def is_set(self) -> bool:
        return self._deadline is not None

    def remaining(self) -> float:
        if self._deadline is None:
            return float("inf")
        return self._deadline - time.monotonic()

    def is_exceeded(self) -> bool:
        return self._deadline is not None and self.remaining() <= 0.0

    def check(self, *, context: str = "") -> None:
        if self.is_exceeded():
            remaining = self.remaining()
            suffix = f", context={context}" if context else ""
            raise PrefetchDeadlineExceeded(
                f"prefetch internal deadline exceeded "
                f"(remaining={remaining:.3f}s, budget={self._budget_s!r}{suffix})",
                deadline=self,
                context=context,
            )

    # Alias kept for call sites that read more naturally as a guard.
    raise_if_exceeded = check


def coerce_deadline(value: Any) -> Optional[PrefetchDeadline]:
    """Accept the explicit value object or an absolute monotonic float.

    ``None`` remains the exact legacy/no-deadline mode.  Supporting an
    absolute float at public seams keeps the primitive compatible with callers
    that already compute ``time.monotonic() + budget`` themselves.
    """
    if value is None:
        return None
    if isinstance(value, PrefetchDeadline):
        return value if value.is_set() else None
    if isinstance(value, (int, float)):
        return PrefetchDeadline(deadline=float(value))
    raise TypeError(f"deadline must be PrefetchDeadline, float, or None; got {type(value).__name__}")


def remaining_seconds(deadline: Any) -> Optional[float]:
    """Return remaining seconds for an explicit deadline, else ``None``."""
    bound = coerce_deadline(deadline)
    return None if bound is None else bound.remaining()


def raise_if_deadline_exceeded(deadline: Any, *, context: str = "") -> None:
    """Fail fast at a boundary; a missing deadline is a no-op."""
    bound = coerce_deadline(deadline)
    if bound is not None:
        bound.check(context=context)


def is_statement_timeout_error(exc: BaseException) -> bool:
    """Recognise PostgreSQL's statement-timeout cancellation without importing psycopg2."""
    if getattr(exc, "pgcode", None) == "57014":
        return True
    text = str(exc).lower()
    return (
        "statement timeout" in text
        or "canceling statement due to statement timeout" in text
        or "query canceled" in text
        or "querycancel" in type(exc).__name__.lower()
    )


class DeadlinePoolView:
    """A thin explicit view that clamps every lease to one deadline.

    It does not change the underlying pool or its ownership rules.  A lease
    returned by the underlying ``PgPool`` carries the same deadline, and its
    connection wrapper refreshes statement_timeout before each SQL call.
    """

    def __init__(self, pool: Any, deadline: PrefetchDeadline) -> None:
        self._pool = pool
        self._deadline = deadline

    @property
    def deadline(self) -> PrefetchDeadline:
        return self._deadline

    def lease(self, timeout: Optional[float] = 5.0, *, deadline=None):
        bound = coerce_deadline(deadline) if deadline is not None else self._deadline
        if bound is None:
            return self._pool.lease(timeout=timeout)
        bound.check(context="pool lease")
        # Preserve the caller's explicit timeout.  PgPool must see the
        # original value so it can distinguish a deadline clamp (PDE) from
        # a legacy explicit timeout (PoolTimeout).
        return self._pool.lease(timeout=timeout, deadline=bound)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


def bind_store_deadline(store: Any, deadline) -> Any:
    """Return a shallow store view whose pool leases use ``deadline``.

    ``PgEmbedStore`` keeps its pool in ``_pool`` and all read methods call its
    existing ``lease()`` context manager.  Copying the facade avoids mutating
    the shared Runtime store or introducing implicit state.
    """
    bound = coerce_deadline(deadline)
    if bound is None or not bound.is_set() or store is None:
        return store
    pool = getattr(store, "pool", None)
    if pool is None:
        return store
    if isinstance(pool, DeadlinePoolView) and pool.deadline is bound:
        return store
    view = copy.copy(store)
    wrapped_pool = DeadlinePoolView(pool, bound)
    # Production PgEmbedStore stores the pool in ``_pool``; small legacy/test
    # store-likes often keep a public ``pool`` attribute.  Bind whichever
    # instance field the facade actually owns, without mutating the original.
    if "_pool" in getattr(store, "__dict__", {}):
        setattr(view, "_pool", wrapped_pool)
    if "pool" in getattr(store, "__dict__", {}):
        setattr(view, "pool", wrapped_pool)
    if "_pool" not in getattr(store, "__dict__", {}) and "pool" not in getattr(store, "__dict__", {}):
        setattr(view, "_pool", wrapped_pool)
    return view
