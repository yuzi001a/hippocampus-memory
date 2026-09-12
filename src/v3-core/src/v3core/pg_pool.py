# -*- coding: utf-8 -*-
"""Stage 1 — PgPool 最小生产原语 (P1 runtime decoupling §4.1)。

显式 lease 语义的线程安全 PostgreSQL 连接池 (零第三方依赖):

  - ``PgPool.lease(timeout=...)`` 返回显式 :class:`PgLease`,
    ``PgLease.connection`` 是借出的物理连接且同一 lease 内身份稳定 (pinned);
  - 归还路径唯一且固定 (context manager 与显式 ``close()`` 走同一实现):
    未完成事务先 rollback → 恢复 autocommit=True → 执行 ``DISCARD ALL``
    session reset → 成功才回 idle 队列;
  - reset 失败立即 discard 该物理连接 (close + 计数), 绝不带病回池,
    下次 lease 由 factory 创建 replacement;
  - 交付 baseline: 成功交付给 borrower 的每个 lease, 其物理连接
    autocommit 都已 normalize 为 True —— 覆盖 idle 复用与 factory 新建
    (含 min_connections 预热后首次借出); normalize 在池锁外执行, 失败时
    fail-closed: discard 该物理连接, 绝不外借、绝不带病回池;
  - 池耗尽在显式 timeout 到期后抛 :class:`PoolTimeout`; 归还唤醒等待者;
    factory 失败原样快速传播, 不伪装成超时;
  - ``shutdown()`` 拒绝新 lease (:class:`PoolClosed`)、立即关闭 idle 连接、
    不跨线程强杀 active lease: 有 active lease 超过 deadline 时抛
    :class:`PoolShutdownTimeout` 并保留该 lease, 其后续显式 close 时关闭。

设计红线: :class:`PgLease` 绝不定义 ``__del__`` —— 归还只依赖显式
close / context manager, 无任何 GC 参与。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from ._deadline import (
    PrefetchDeadline,
    PrefetchDeadlineExceeded,
    coerce_deadline,
    is_statement_timeout_error,
)

logger = logging.getLogger(__name__)

# Default used by every production-facing pool lease.  Callers that are
# intentionally non-blocking must opt into ``timeout=None`` explicitly.
DEFAULT_LEASE_TIMEOUT = 5.0

__all__ = [
    "DEFAULT_LEASE_TIMEOUT",
    "PgPool",
    "PgLease",
    "PoolTimeout",
    "PoolClosed",
    "PoolShutdownTimeout",
    "OuterReaderToken",
    "WorkerReservation",
    "WorkerReservationToken",
]


class _NoopToken:
    """Idempotent no-op token used by rejected admission primitives.

    Closing it is a no-op so callers can use ``finally: tok.close()``
    unconditionally.
    """

    __slots__ = ("_closed",)

    def __init__(self) -> None:
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def __repr__(self) -> str:
        return "<AdmissionToken rejected=True closed=%r>" % (self._closed,)


class OuterReaderToken:
    """Idempotent outer-reader admission token. Closing releases the slot.

    Holds no physical connection — it is a logical reservation only.
    """

    __slots__ = ("_pool", "_closed")

    def __init__(self, pool: "PgPool") -> None:
        self._pool = pool
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._pool._cond:
            self._pool._outer_readers -= 1
            # Wake up any outer reader waiting (the only sleep is in
            # try_acquire_outer_reader when caller asks for blocking, but
            # we expose non-blocking only — still notify_all for symmetry).
            self._pool._cond.notify_all()

    def __enter__(self) -> "OuterReaderToken":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:
        return "<OuterReaderToken closed=%r>" % (self._closed,)


class WorkerReservationToken:
    """Idempotent token for an internal worker slot reservation.

    Holds no physical connection — it is a logical reservation only.
    Real SQL still uses the existing ``PgPool.lease`` path.
    """

    __slots__ = ("_pool", "_granted", "_closed")

    def __init__(self, pool: "PgPool", granted: int) -> None:
        self._pool = pool
        self._granted = int(granted)
        self._closed = False

    @property
    def granted(self) -> int:
        return self._granted

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._granted > 0:
            with self._pool._cond:
                self._pool._worker_reserved -= self._granted
                self._pool._cond.notify_all()

    def __enter__(self) -> "WorkerReservationToken":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:
        return "<WorkerReservationToken granted=%d closed=%r>" % (self._granted, self._closed)


class WorkerReservation:
    """Result of ``try_reserve_workers``.

    ``granted`` is the number of worker slots reserved (``0..desired``).
    ``token`` releases them when closed (no-op when ``granted==0``).
    """

    __slots__ = ("granted", "token")

    def __init__(self, granted: int, token: Any) -> None:
        self.granted = int(granted)
        self.token = token

    def __repr__(self) -> str:
        return "<WorkerReservation granted=%d>" % (self.granted,)

# 等价 PostgreSQL session reset 的最低要求 (§4.1: 至少 DISCARD ALL 级别)。
_RESET_SQL = "DISCARD ALL"

# psycopg2 get_transaction_status(): 非 0 即存在未完成事务 (IDLE == 0)。
_TXN_STATUS_IDLE = 0


class PgPoolError(Exception):
    """pg_pool 异常基类。"""


class PoolTimeout(PgPoolError):
    """池耗尽: 显式等待 timeout 到期仍未拿到连接。"""


class PoolClosed(PgPoolError):
    """池已 shutdown, 拒绝新 lease / 新建连接。"""


class PoolShutdownTimeout(PgPoolError):
    """shutdown deadline 内仍有 active lease 未释放 (明确失败, 保留现场)。"""


def _apply_statement_timeout(conn: Any, timeout_ms: int) -> None:
    """Set session-level ``statement_timeout`` on a PostgreSQL connection.

    Best-effort: psycopg2 connection exposes ``cursor()`` for arbitrary
    SQL, so we run ``SET statement_timeout`` directly. Any failure
    here is raised — the caller (PgPool delivery path) will fail-closed
    by discarding the connection rather than silently handing it out
    without the deadline-aware safety net.

    The bound is intentionally per-connection (session-level ``SET``),
    not transaction-scoped — the prefetch inner worker may issue
    multiple SELECTs on the same lease, all of which need to be bounded
    by the same deadline-derived upper limit.

    ``PgPool._reset_session`` issues ``DISCARD ALL`` at release, which
    resets session-level settings — so each new lease re-applies its
    own deadline-driven timeout and no cross-lease contamination is
    possible.
    """
    if timeout_ms is None or int(timeout_ms) <= 0:
        return
    # ``SET`` does not accept a server-side bind parameter on all supported
    # PostgreSQL versions.  The value is validated as an integer, so only
    # that integer is interpolated into the session-level command.
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {int(timeout_ms)}")


def _compute_statement_timeout_ms(deadline: "Optional[PrefetchDeadline]") -> Optional[int]:
    """Translate a PrefetchDeadline into a ``statement_timeout`` integer
    (milliseconds) for ``SET statement_timeout``. ``None`` when no
    deadline is bound — caller then leaves session default alone.

    Floor at 1ms so that a sub-millisecond remaining budget still
    produces a non-zero session limit (a zero timeout in PostgreSQL
    means *unlimited*, the opposite of what we want).
    """
    if deadline is None or not deadline.is_set():
        return None
    remaining_s = deadline.remaining()
    if remaining_s <= 0:
        # Already expired — leave session default; the SQL guard
        # upstream will refuse to run anyway. We do not apply a
        # zero-ms timeout because PostgreSQL interprets 0 as unlimited.
        return None
    ms = int(remaining_s * 1000)
    if ms < 1:
        return 1
    return ms


def _rollback_quietly(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:
        logger.debug("pg_pool: rollback failed after statement timeout", exc_info=True)


class _DeadlineCursor:
    """Cursor facade that refreshes the absolute deadline before each SQL."""

    __slots__ = ("_lease", "_cursor")

    def __init__(self, lease: "PgLease", cursor: Any) -> None:
        self._lease = lease
        self._cursor = cursor

    def _before_sql(self) -> None:
        deadline = self._lease._deadline
        if deadline is None:
            return
        deadline.check(context="SQL")
        self._lease.refresh_statement_timeout()

    def _after_error(self, exc: BaseException) -> None:
        deadline = self._lease._deadline
        if deadline is None:
            return
        if is_statement_timeout_error(exc) or deadline.is_exceeded():
            _rollback_quietly(self._lease._connection)
            raise PrefetchDeadlineExceeded(
                "prefetch internal deadline exceeded during PostgreSQL query",
                deadline=deadline,
                context="SQL",
            ) from exc

    def execute(self, operation: Any, params: Any = None) -> Any:
        self._before_sql()
        try:
            if params is None:
                return self._cursor.execute(operation)
            return self._cursor.execute(operation, params)
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            self._after_error(exc)
            raise

    def executemany(self, operation: Any, seq_of_params: Any) -> Any:
        self._before_sql()
        try:
            return self._cursor.executemany(operation, seq_of_params)
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            self._after_error(exc)
            raise

    def callproc(self, procname: Any, params: Any = None) -> Any:
        self._before_sql()
        try:
            if params is None:
                return self._cursor.callproc(procname)
            return self._cursor.callproc(procname, params)
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            self._after_error(exc)
            raise

    def _check_fetch(self) -> None:
        deadline = self._lease._deadline
        if deadline is not None:
            deadline.check(context="SQL fetch")

    def fetchone(self) -> Any:
        self._check_fetch()
        try:
            return self._cursor.fetchone()
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            self._after_error(exc)
            raise

    def fetchmany(self, *args: Any, **kwargs: Any) -> Any:
        self._check_fetch()
        try:
            return self._cursor.fetchmany(*args, **kwargs)
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            self._after_error(exc)
            raise

    def fetchall(self) -> Any:
        self._check_fetch()
        try:
            return self._cursor.fetchall()
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            self._after_error(exc)
            raise

    def __enter__(self) -> "_DeadlineCursor":
        enter = getattr(self._cursor, "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        exit_fn = getattr(self._cursor, "__exit__", None)
        if callable(exit_fn):
            return exit_fn(exc_type, exc, tb)
        self.close()
        return False

    def __iter__(self):
        self._check_fetch()
        return iter(self._cursor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def close(self) -> Any:
        return self._cursor.close()


class _DeadlineConnection:
    """Connection facade preserving pool ownership while guarding cursors."""

    __slots__ = ("_lease", "_connection")

    def __init__(self, lease: "PgLease") -> None:
        self._lease = lease
        self._connection = lease._connection

    def cursor(self, *args: Any, **kwargs: Any) -> _DeadlineCursor:
        return _DeadlineCursor(self._lease, self._connection.cursor(*args, **kwargs))

    def __enter__(self) -> "_DeadlineConnection":
        enter = getattr(self._connection, "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        exit_fn = getattr(self._connection, "__exit__", None)
        if callable(exit_fn):
            return exit_fn(exc_type, exc, tb)
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _connection_in_transaction(conn: Any) -> bool:
    """探测连接是否存在未完成事务 (duck-typing, 不依赖具体驱动)。"""
    status = getattr(conn, "status", None)
    flag = getattr(status, "in_transaction", None)
    if flag is not None:
        return bool(flag)
    get_ts = getattr(conn, "get_transaction_status", None)
    if callable(get_ts):
        try:
            return int(get_ts()) != _TXN_STATUS_IDLE
        except Exception:
            return True  # 状态未知 → 按"可能有脏事务"处理, 宁可多一次 rollback
    return False


def _close_quietly(conn: Any) -> None:
    """尽力关闭物理连接, 关闭失败也不抛出 (discard 路径不允许二次异常)。"""
    try:
        conn.close()
    except Exception:
        logger.debug("pg_pool: physical close failed during discard", exc_info=True)


class PgLease:
    """一次显式借出。归还只经 ``close()`` / context manager, 绝不定义 ``__del__``。

    同一 lease 期间 ``connection`` 恒为同一个物理连接对象 (pinned lease)。

    P1.2-A1 deadline support (optional, fully backward-compatible):

      * ``deadline`` is bound at lease acquisition time. When set, the
        lease carries an internal ``statement_timeout`` already applied
        to the connection so any long-running SQL inside the prefetch
        worker is bounded by the deadline (PG ``QueryCanceled`` on
        timeout — the caller layer translates this into the canonical
        ``PrefetchDeadlineExceeded`` signal so all SQL paths surface
        the same outcome).
      * ``refresh_statement_timeout()`` re-applies the bound based on
        the current remaining deadline; callers should call this
        BEFORE each new SQL step so a step that took a previous SQL
        close to the deadline does not leave subsequent SQL unbounded.
      * ``deadline=None`` at acquisition preserves the old behavior
        exactly (no statement_timeout applied, no helper calls needed).
    """

    __slots__ = (
        "_pool",
        "_connection",
        "_closed",
        "_deadline",
        "_statement_timeout_ms",
        "_deadline_connection",
    )

    def __init__(
        self,
        pool: "PgPool",
        connection: Any,
        *,
        deadline: "Optional[PrefetchDeadline]" = None,
    ) -> None:
        self._pool = pool
        self._connection = connection
        self._closed = False
        self._deadline = deadline
        # Initial application is the pool's job (so failure can
        # fail-closed before delivery). Lease stores the value for
        # refresh on demand.
        self._statement_timeout_ms: Optional[int] = _compute_statement_timeout_ms(deadline)
        self._deadline_connection = _DeadlineConnection(self) if deadline is not None else None

    @property
    def connection(self) -> Any:
        """借出的连接; deadline lease 返回受保护 facade, 否则返回原对象."""
        return self._deadline_connection or self._connection

    @property
    def deadline(self) -> "Optional[PrefetchDeadline]":
        """The deadline bound to this lease, or None when unset."""
        return self._deadline

    @property
    def statement_timeout_ms(self) -> Optional[int]:
        """The current session-level statement_timeout (ms) applied to the
        underlying connection, or None when no deadline was bound.
        """
        return self._statement_timeout_ms

    def set_statement_timeout(self, milliseconds: int) -> None:
        """Apply one validated session-level statement timeout to this lease."""
        if self._deadline is not None:
            self._deadline.check(context="set statement_timeout")
        timeout_ms = int(milliseconds)
        if timeout_ms <= 0:
            raise ValueError("statement_timeout must be a positive integer")
        _apply_statement_timeout(self._connection, timeout_ms)
        self._statement_timeout_ms = timeout_ms

    def refresh_statement_timeout(self) -> Optional[int]:
        """Recompute and re-apply ``SET statement_timeout`` based on the
        lease's remaining deadline budget.

        Returns the new ms value (or None when no deadline is bound).
        Cheap to call: a single short ``SET statement_timeout`` round
        trip on the pinned connection.

        Call this BEFORE each new SQL step that may take significant
        wall-clock time (especially anything between blocking reads,
        not just the first SQL on the lease).
        """
        if self._deadline is None:
            return None
        # This is an absolute deadline: never retain the timeout calculated
        # when the lease was acquired.  Recompute and apply before every SQL.
        self._deadline.check(context="statement_timeout refresh")
        ms = _compute_statement_timeout_ms(self._deadline)
        if ms is None:
            self._deadline.check(context="statement_timeout refresh")
            return None
        try:
            self.set_statement_timeout(ms)
        except PrefetchDeadlineExceeded:
            raise
        except Exception as exc:
            if is_statement_timeout_error(exc) or self._deadline.is_exceeded():
                _rollback_quietly(self._connection)
                raise PrefetchDeadlineExceeded(
                    "prefetch internal deadline exceeded while refreshing statement_timeout",
                    deadline=self._deadline,
                    context="statement_timeout refresh",
                ) from exc
            raise
        return ms

    def close(self) -> None:
        """显式归还; 幂等 —— 第二次起不做任何事 (不重复 rollback/reset/归还)。"""
        self._pool._release_lease(self)

    def __enter__(self) -> "PgLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()  # 与显式 close 完全同一路径; 不吞异常
        return False

    def __repr__(self) -> str:
        return "<PgLease closed=%r connection=%r deadline=%r>" % (
            self._closed,
            self._connection,
            self._deadline,
        )


class PgPool:
    """线程安全的显式 lease 连接池。

    参数:
        connect: 无参工厂, 返回一个新的物理连接 (如 psycopg2.connect(...));
            工厂抛错时从 ``lease()`` 原样快速传播。
        max_connections: 物理连接总数上限 (>0), 含 idle + in_use + 创建中。
        min_connections: 可选预热下标 (best-effort, 构造时尝试建满;
            个别失败降级为按需创建, 不视为致命错误)。
        connect_timeout: 工厂已声明并实际施加的最大同步建连时长 (秒)。
            deadline-aware lease 在进入工厂前用它做 fail-fast admission；
            Runtime 必须把同一个值传给 psycopg2.connect(connect_timeout=...)
            和本构造器。None 保留通用/legacy 工厂语义，但不提供生产建连
            的 deadline containment 保证；factory 返回后的 deadline 检查仍会
            discard late connection。
    """

    def __init__(
        self,
        connect: Callable[[], Any],
        max_connections: int,
        min_connections: int = 0,
        *,
        connect_timeout: Optional[float] = None,
    ) -> None:
        if not callable(connect):
            raise TypeError("connect 必须是可调用工厂 connect() -> connection")
        max_connections = int(max_connections)
        min_connections = int(min_connections)
        if max_connections < 1:
            raise ValueError("max_connections 必须 >= 1, got %r" % (max_connections,))
        if min_connections < 0 or min_connections > max_connections:
            raise ValueError(
                "min_connections 必须在 [0, max_connections] 内, got %r" % (min_connections,)
            )
        if connect_timeout is not None:
            connect_timeout = float(connect_timeout)
            if connect_timeout <= 0:
                raise ValueError(
                    "connect_timeout 必须为正数秒, got %r" % (connect_timeout,)
                )

        self._connect = connect
        self._max_connections = max_connections
        self._min_connections = min_connections
        # This is a constructor contract, not an inferred timeout.  The
        # caller is responsible for configuring the underlying factory/driver
        # with the same maximum.
        self._connect_timeout = connect_timeout

        self._cond = threading.Condition()
        self._idle: List[Any] = []          # 可复用空闲物理连接
        self._in_use: Set[Any] = set()      # 已借出的物理连接
        self._creating = 0                  # 已预留槽位、factory 执行中
        self._closed_count = 0              # 累计 close/discard 的物理连接数
        self._shutdown = False
        # P1.3 + A2 — pool-owned admission counters.  These are logical
        # reservations only; no physical connection is held by them.  All
        # mutations are made under ``self._cond`` so the writer reserve is
        # always computed against a consistent view of physical leases,
        # creating slots, outer-reader admissions and internal worker
        # reservations.
        self._outer_readers = 0   # current admitted outer prefetch readers
        self._worker_reserved = 0  # current reserved internal QA worker slots

        # best-effort 预热到 min_connections (构造期单线程, 直接入队即可)。
        for _ in range(self._min_connections):
            try:
                conn = self._connect()
            except Exception as exc:
                logger.warning("pg_pool: min_connections 预热失败, 降级为按需创建: %s", exc)
                break
            try:
                self._normalize_delivery(conn)
            except Exception as exc:
                logger.warning(
                    "pg_pool: min_connections 预热连接 normalize 失败, discard: %s", exc
                )
                _close_quietly(conn)
                self._closed_count += 1
                break
            self._idle.append(conn)

    # ── 借出 ──────────────────────────────────────────────────────

    def lease(
        self,
        timeout: Optional[float] = DEFAULT_LEASE_TIMEOUT,
        *,
        deadline: "Optional[PrefetchDeadline]" = None,
    ) -> PgLease:
        """借出一个连接; 池耗尽时最多等待 ``timeout`` 秒, 到期抛 PoolTimeout。

        ``deadline`` (P1.2-A1, optional): when provided, a deadline-aware
        ``statement_timeout`` is applied to the borrowed connection via
        ``SET statement_timeout``. The deadline is also attached to the
        lease so callers can refresh the bound between SQL steps and
        so the canonical ``PrefetchDeadlineExceeded`` signal is raised
        in the outer caller layer (rather than a silent
        ``QueryCanceled`` / ``OperationalError``).

        ``deadline=None`` is a pure no-op: no SET is issued, no helper
        is required, behavior is identical to the pre-A1 contract.

        When a deadline is bound:

          * ``SET statement_timeout`` is applied as part of the
            delivery normalize step (lock-free). Failure here is
            fail-closed (the connection is discarded, not returned to
            idle, not handed to the borrower).
          * ``timeout`` (the lease-wait budget) is automatically
            clamped to ``deadline.remaining()`` when the deadline is
            tighter — no caller arithmetic needed.
          * The pool lease timeout (waiting for a free connection) is
            bounded by min(explicit ``timeout``, deadline.remaining()).

        factory 抛错时原样传播 (不是 PoolTimeout).
        交付 baseline: 成功返回的 lease 其物理连接 autocommit 必为 True;
        normalize 在锁外执行, 失败时 fail-closed (discard, 不回 idle)。
        """
        # ── Effective wait budget ────────────────────────────────────
        # When a deadline is bound, the lease-wait budget cannot exceed
        # the deadline itself — there is no point waiting longer than
        # the caller is willing to stay alive. Clamp here so callers
        # don't need to do the arithmetic themselves.
        deadline = coerce_deadline(deadline)
        if deadline is not None:
            deadline.check(context="pool lease")
        deadline_remaining: Optional[float] = None
        # Keep this distinction separate from the effective numeric timeout:
        # an explicit lease timeout remains PoolTimeout; only a timeout caused
        # by clamping to the absolute prefetch deadline becomes PDE.
        deadline_limited_wait = False
        if deadline is not None and deadline.is_set():
            deadline_remaining = deadline.remaining()
            if deadline_remaining <= 0:
                # Deadline already exhausted at the call site — refuse
                # to block at all and let the caller raise the canonical
                # signal via deadline.check() before issuing SQL.
                timeout = 0.0
                deadline_limited_wait = True
            elif timeout is None or float(timeout) > deadline_remaining:
                timeout = deadline_remaining
                deadline_limited_wait = True

        cond = self._cond
        deadline_ts = None if timeout is None else time.monotonic() + float(timeout)
        conn: Any = None

        with cond:
            while True:
                if self._shutdown:
                    raise PoolClosed("pg_pool: pool is shut down; new leases rejected")

                if self._idle:
                    # 先从 idle 摘出并记入 in_use (锁内), normalize 在锁外做;
                    # 期间该物理连接既不在 idle 也不会被 shutdown 关闭,
                    # total == idle + in_use 计数保持一致。
                    conn = self._idle.pop()
                    self._in_use.add(conn)
                    break

                # 预留创建槽位后必须在锁外调 factory, 否则会阻塞整个池。
                if (
                    len(self._idle) + len(self._in_use) + self._creating
                    < self._max_connections
                ):
                    self._creating += 1
                    break

                if deadline_ts is None:
                    cond.wait()
                    continue
                remaining = deadline_ts - time.monotonic()
                if remaining <= 0:
                    if deadline_limited_wait and deadline is not None:
                        deadline.check(context="pool lease acquisition")
                    raise PoolTimeout(
                        "pg_pool: exhausted (%d/%d connections in use), "
                        "gave up after explicit timeout=%gs"
                        % (len(self._in_use), self._max_connections, timeout)
                    )
                cond.wait(remaining)  # 被 release/shutdown 唤醒后重查状态

        if conn is not None:
            # ---- 锁外: 交付前 normalize (idle 复用路径) ----
            try:
                self._normalize_delivery(conn)
            except BaseException:
                # fail-closed: 坏连接绝不外借、绝不回 idle; 回收计数并唤醒等待者。
                with cond:
                    self._in_use.discard(conn)
                    _close_quietly(conn)
                    self._closed_count += 1
                    cond.notify_all()
                raise
            # normalize 期间池可能已 shutdown: 不允许交付新 lease (race 语义)。
            with cond:
                if self._shutdown:
                    self._in_use.discard(conn)
                    _close_quietly(conn)
                    self._closed_count += 1
                    cond.notify_all()  # 唤醒 shutdown 等待者
                    raise PoolClosed(
                        "pg_pool: pool shut down while preparing a lease"
                    )
                lease = PgLease(self, conn, deadline=deadline)
            # Apply deadline-driven statement_timeout outside the pool lock;
            # this is a database round trip and must not block other leases.
            if deadline is not None and deadline.is_set():
                ms = lease.statement_timeout_ms
                if ms is not None:
                    try:
                        deadline.check(context="lease statement_timeout")
                        _apply_statement_timeout(conn, ms)
                    except BaseException as exc:
                        self._discard_unready_lease(lease)
                        if deadline is not None and (
                            is_statement_timeout_error(exc) or deadline.is_exceeded()
                        ):
                            raise PrefetchDeadlineExceeded(
                                "prefetch internal deadline exceeded during lease setup",
                                deadline=deadline,
                                context="lease statement_timeout",
                            ) from exc
                        raise
            return lease

        # ---- 锁外创建物理连接 ----
        # A synchronous factory cannot be safely interrupted by a caller-side
        # thread timeout.  Admission is therefore fail-fast whenever the
        # explicitly declared factory bound cannot fit in the remaining
        # absolute deadline.  The driver factory itself must enforce the same
        # bound (Runtime does this with psycopg2.connect(connect_timeout=...)).
        try:
            if deadline is not None and deadline.is_set():
                deadline.check(context="physical connection admission")
                if (
                    self._connect_timeout is not None
                    and deadline.remaining() <= self._connect_timeout
                ):
                    raise PrefetchDeadlineExceeded(
                        "prefetch deadline cannot fit declared physical connect bound",
                        deadline=deadline,
                        context="physical connection admission",
                    )
            conn = self._connect()
        except PrefetchDeadlineExceeded:
            with cond:
                self._creating -= 1
                cond.notify_all()  # 槽位释放, 唤醒其它等待者重新竞争
            raise
        except BaseException as exc:
            with cond:
                self._creating -= 1
                cond.notify_all()  # 槽位释放, 唤醒其它等待者重新竞争
            if deadline is not None and deadline.is_exceeded():
                raise PrefetchDeadlineExceeded(
                    "prefetch deadline exceeded while creating physical connection",
                    deadline=deadline,
                    context="physical connection",
                ) from exc
            raise

        # A factory that violates its declared bound may still return a
        # connection after the absolute deadline.  It is never deliverable;
        # discard it synchronously and release the reserved slot.
        if deadline is not None and deadline.is_exceeded():
            with cond:
                _close_quietly(conn)
                self._creating -= 1
                self._closed_count += 1
                cond.notify_all()
            deadline.check(context="physical connection")

        # ---- 锁外: 交付前 normalize (factory 新建路径) ----
        # normalize 先于入池/交付; 失败时同样 fail-closed 销毁并回收槽位。
        try:
            self._normalize_delivery(conn)
        except BaseException:
            with cond:
                self._creating -= 1
                _close_quietly(conn)
                self._closed_count += 1
                cond.notify_all()
            raise

        # normalize is currently an attribute-only operation, but keep the
        # post-normalize guard at the delivery boundary so a future normalize
        # implementation cannot hand out a late connection accidentally.
        if deadline is not None and deadline.is_exceeded():
            with cond:
                _close_quietly(conn)
                self._creating -= 1
                self._closed_count += 1
                cond.notify_all()
            deadline.check(context="physical connection delivery")

        with cond:
            self._creating -= 1
            if self._shutdown:
                # 创建/normalize 期间池被关闭: 立即销毁, 不入池、不外借。
                _close_quietly(conn)
                self._closed_count += 1
                cond.notify_all()
                raise PoolClosed("pg_pool: pool shut down while creating a connection")
            self._in_use.add(conn)
            lease = PgLease(self, conn, deadline=deadline)
            cond.notify_all()
        if deadline is not None and deadline.is_set():
            ms = lease.statement_timeout_ms
            if ms is not None:
                try:
                    deadline.check(context="lease statement_timeout")
                    _apply_statement_timeout(conn, ms)
                except BaseException as exc:
                    self._discard_unready_lease(lease)
                    if deadline is not None and (
                        is_statement_timeout_error(exc) or deadline.is_exceeded()
                    ):
                        raise PrefetchDeadlineExceeded(
                            "prefetch internal deadline exceeded during lease setup",
                            deadline=deadline,
                            context="lease statement_timeout",
                        ) from exc
                    raise
        return lease

    # ── 归还 ──────────────────────────────────────────────────────

    def _discard_unready_lease(self, lease: PgLease) -> None:
        """Discard a lease whose deadline setup failed before delivery."""
        conn = lease._connection
        with self._cond:
            if lease._closed:
                return
            lease._closed = True
            self._in_use.discard(conn)
            _close_quietly(conn)
            self._closed_count += 1
            self._cond.notify_all()

    def _release_lease(self, lease: PgLease) -> None:
        """lease 归还的唯一入口 (close() 与 __exit__ 共用); 幂等。

        正常路径: rollback(如有脏事务) → autocommit=True → DISCARD ALL → 回 idle。
        reset 失败或池已关闭: discard 物理连接, 不回 idle。
        """
        conn = lease._connection
        cond = self._cond
        with cond:
            if lease._closed:
                return
            lease._closed = True
            if self._shutdown:
                # 池已关闭: 不再 reset/回池, 直接销毁并唤醒 shutdown 等待者。
                self._in_use.discard(conn)
                _close_quietly(conn)
                self._closed_count += 1
                cond.notify_all()
                return

        # 锁外执行 reset (真实 PG 上是网络往返, 不能持锁)。
        reset_ok = self._reset_session(conn)

        with cond:
            self._in_use.discard(conn)
            if not reset_ok or self._shutdown:
                _close_quietly(conn)
                self._closed_count += 1
            else:
                self._idle.append(conn)
            cond.notify_all()  # 唤醒等待 lease 的线程与 shutdown 等待者

    @staticmethod
    def _reset_session(conn: Any) -> bool:
        """§4.1 归还前 reset 顺序; 成功返回 True, 任何失败返回 False。"""
        # 物理连接已被借用方关闭 → 无从 reset, 直接按失败 discard。
        if getattr(conn, "closed", False):
            logger.debug("pg_pool: physically closed connection; discarding")
            return False
        try:
            # 1) 存在未完成事务 → 先 rollback
            if _connection_in_transaction(conn):
                conn.rollback()
            # 2) 恢复 autocommit=True (必须在 rollback 之后)
            if getattr(conn, "autocommit", True) is not True:
                conn.autocommit = True
            # 3) session reset: 至少 DISCARD ALL 级别
            with conn.cursor() as cur:
                cur.execute(_RESET_SQL)
            return True
        except Exception as exc:
            logger.debug("pg_pool: session reset failed; discarding connection: %s", exc)
            return False

    @staticmethod
    def _normalize_delivery(conn: Any) -> None:
        """交付 baseline (§4.1 对称前提): 交付给 borrower 前强制 autocommit=True。

        只做属性 normalize, 不执行网络往返; 失败时抛原始异常, 由调用方
        fail-closed (discard 物理连接, 绝不外借、绝不回 idle)。
        """
        if getattr(conn, "autocommit", True) is not True:
            conn.autocommit = True

    # ── 关闭 ──────────────────────────────────────────────────────

    def shutdown(self, timeout: Optional[float] = 5.0) -> None:
        """关闭池: 拒绝新 lease, 立即关 idle, 等 active lease 显式释放。

        deadline 内仍有 active lease 时抛 PoolShutdownTimeout (明确失败);
        这些 lease 保持可用, 后续显式 close 时其物理连接会被关闭。
        可重复调用 (前次超时后可再次等待剩余 lease)。
        """
        cond = self._cond
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        with cond:
            self._shutdown = True
            # 立即关闭所有 idle 连接。
            while self._idle:
                _close_quietly(self._idle.pop())
                self._closed_count += 1
            # 唤醒仍在等 lease 的线程, 让它们看到 PoolClosed 而非干等到自身超时。
            cond.notify_all()
            # 等待 active lease 自然释放 + 正在进行的 factory 调用收尾
            # (绝不跨线程强杀正在使用的连接 / 正在建连的调用)。
            while self._in_use or self._creating:
                if deadline is None:
                    cond.wait(0.1)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolShutdownTimeout(
                        "pg_pool: %d connection(s) still outstanding "
                        "(%d leased, %d creating) after %gs shutdown deadline; "
                        "left intact, each will be closed on its explicit close()"
                        % (
                            len(self._in_use) + self._creating,
                            len(self._in_use),
                            self._creating,
                            float(timeout),
                        )
                    )
                cond.wait(remaining)

    # ── 观测 ──────────────────────────────────────────────────────

    def stats(self) -> Dict[str, int]:
        """返回 total/idle/in_use/closed。

        total 是当前存活物理连接数, 恒等于 idle + in_use
        (创建中的槽位未成形不计入); closed 是累计 close/discard 计数。
        """
        with self._cond:
            return {
                "total": len(self._idle) + len(self._in_use),
                "idle": len(self._idle),
                "in_use": len(self._in_use),
                "closed": self._closed_count,
            }

    # ── 池所有权 admission (P1.3 + A2) ────────────────────────────
    #
    # 这些原语在 ``self._cond`` 内完成所有状态读改写, 与 lease 创建/归还
    # 共享同一把锁, 保证 ``in_use + creating + outer_readers +
    # worker_reserved + 1 (writer reserve) <= max_connections`` 的不变量。
    # 设计意图: pool max=4 时, 物理 lease + 当前 admission 始终为 3,
    # 写线程始终有 1 个槽位可用。
    #
    # 它们仅做逻辑计数预留, 不持有物理连接 — 真正的 SQL 仍走现有的
    # ``PgPool.lease`` / ``PgLease`` 路径, 不影响 lease/reset/shutdown
    # 语义, 也不需要修改现有 caller 的连接管理代码。
    #
    # 显式拒绝 (None / granted=0) 时返回的 token 是 no-op, 调用方可以
    # 无脑 ``finally: token.close()`` 而不必判断是否拿到。

    def try_acquire_outer_reader(self) -> Any:
        """非阻塞地申请一个 outer-reader admission slot。

        接受条件 (在 ``self._cond`` 内一次性检查):

            ``in_use + creating + outer_readers + worker_reserved
                <= max_connections - 1``

        其中 ``-1`` 是 durability writer reserve。

        返回 ``OuterReaderToken`` 或 ``None`` (拒绝)。绝不阻塞。
        """
        with self._cond:
            if self._shutdown:
                return None
            used = (
                len(self._in_use)
                + self._creating
                + self._outer_readers
                + self._worker_reserved
            )
            if used >= self._max_connections - 1:
                return None
            self._outer_readers += 1
            return OuterReaderToken(self)

    def try_reserve_workers(
        self,
        desired: int,
        minimum: int = 1,
        *,
        current_outer: bool = False,
    ) -> "WorkerReservation":
        """非阻塞地预留 ``min(desired, available)`` 个内部 worker slot。

        可用容量 = ``max_connections - 1 (writer reserve)
                              - in_use - creating
                              - other_outer_readers - worker_reserved``

        ``current_outer`` (P1.3 + A2 follow-up, optional kwarg, 默认
        ``False`` 保持直接调用 / 旧 fake 测试语义不变):
            当 ``True`` 时, 表示 caller 自己已经持有一个 outer-reader
            token (例如 ``_qa_prefill_parallel_cache`` 的真实 owner 在调
            入 V3Core 端两个 recall seam 时已经先 acquire 了 outer token,
            再调本方法再扣一次就会重复扣同一个 outer slot)。此时我们
            只把 ``_outer_readers - 1`` 当作"其他 outer readers", 不能
            把本次调用自己的 outer token 重复扣掉。
            当 ``False`` (默认) 时, 行为与 P1.3-A2 合同完全一致:
            ``_outer_readers`` 全部当作其他 outer readers, 仍按旧语义
            empty=3 / one observer=2 / observer+outer=1 reject。

        分配 = ``min(desired, max(available, 0))``。若 ``分配 < minimum``
        则 caller 必须视为 reject (返回 ``granted=分配``, 但 token 是
        no-op — 仅用来 ``finally`` 释放零计数, 不会扣任何容量)。

        永不阻塞, 永不抛 ``PoolTimeout``。计算在 ``self._cond`` 内完成,
        与现有 lease 创建/归还互斥, 保证 writer reserve 不变量。
        """
        if desired < 0:
            raise ValueError("desired must be >= 0")
        if minimum < 0:
            raise ValueError("minimum must be >= 0")
        desired = int(desired)
        minimum = int(minimum)
        with self._cond:
            if self._shutdown:
                return WorkerReservation(0, _NoopToken())
            # ``current_outer=True`` 时 caller 已先持有一个 outer token,
            # ``_outer_readers`` 已包含本次调用自己的那个, 不能重复扣。
            # 视作"其他 outer readers"为 ``_outer_readers - 1``, 下限 0
            # (防御: 若 caller 谎报 current_outer=True 但实际未 acquire,
            # 仍按"至少 0"不破坏现有不变量, 但会让 capacity 多算 1 —
            # 这是 caller 的契约责任, 本方法不做更激进的反证)。
            if current_outer:
                other_outer = max(0, self._outer_readers - 1)
            else:
                other_outer = self._outer_readers
            used = (
                len(self._in_use)
                + self._creating
                + other_outer
                + self._worker_reserved
            )
            capacity = self._max_connections - 1 - used
            if capacity < 0:
                capacity = 0
            grant = min(desired, capacity)
            if grant < minimum:
                # Caller treats this as a hard reject; we still return the
                # granted count so it can distinguish "got 1 below minimum"
                # from "got 0".  Token is a no-op so caller may close it
                # unconditionally.
                return WorkerReservation(grant, _NoopToken())
            if grant > 0:
                self._worker_reserved += grant
            return WorkerReservation(grant, WorkerReservationToken(self, grant))

    def __repr__(self) -> str:
        with self._cond:
            return (
                "<PgPool max=%d idle=%d in_use=%d creating=%d closed=%d "
                "outer_readers=%d worker_reserved=%d shutdown=%r>"
                % (
                    self._max_connections,
                    len(self._idle),
                    len(self._in_use),
                    self._creating,
                    self._closed_count,
                    self._outer_readers,
                    self._worker_reserved,
                    self._shutdown,
                )
            )
