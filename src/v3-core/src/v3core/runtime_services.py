"""Runtime-owned Observer/E1 lifecycle services (P1 Stage 6).

This module owns only service lifecycle, process-local singleflight, and the
PostgreSQL advisory-lock/lease boundary. Observer/E1 business functions remain
in their existing modules and keep their prompts and data semantics.

Stage 7 (2026-08-27): deferred cache invalidation. E1Service._callback
hands the business function a *marker* (mark_topics_commit) instead of
the real cache callback; synthesize_yin fires the marker when it commits
a topic mutation, and the real cache.invalidate() runs strictly AFTER
E1Scheduler._run_e1_locked releases the scheduler lock and closes the
outer lease — via E1Scheduler(after_release_callback=...).
ObserverService._execute_observer_worker_locked already implements the
same shape (callback_seen marker + post-release callback); the Observer
worker just needs to forward that marker into synthesize_yin on the
nested E1 path so topic mutations during the nested run actually arm the
deferred invalidation.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from .daemon import build_default_lock_provider
from .scheduler import E1Scheduler


def _pool_in_use(pool: Any) -> int | None:
    stats = getattr(pool, "stats", None)
    if callable(stats):
        try:
            value = stats().get("in_use")
            if value is not None:
                return int(value)
        except Exception:
            pass
    try:
        value = getattr(pool, "in_use")
        return int(value)
    except Exception:
        return None


def _backend_pid(conn: Any) -> int | None:
    """Read the backend pid without changing business state."""
    if conn is None:
        return None
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_backend_pid()")
        row = cur.fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        return None
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
    return None


def _rejected(reason: str = "draining") -> dict[str, Any]:
    return {
        "ran": False,
        "status": reason,
        "reason": reason,
        "skipped": True,
    }


class _BorrowedPinnedLease:
    """Inner handle over a scheduler-held outer lease.

    ``close()`` / context-exit must not return or close the outer lease.
    ``scheduler_lock_provider.release()`` remains responsible for
    unlock-before-close.
    """

    def __init__(self, outer_lease: Any) -> None:
        self._outer = outer_lease
        self._closed = False

    @property
    def connection(self) -> Any:
        return getattr(self._outer, "connection", self._outer)

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "_BorrowedPinnedLease":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        del exc_type, exc_val, exc_tb
        self.close()
        return False


class _PinnedSchedulerPoolView:
    """Pool view whose every ``lease()`` borrows the same outer scheduler lease."""

    def __init__(self, outer_lease: Any) -> None:
        self._outer = outer_lease

    def lease(self, timeout: float | None = None) -> _BorrowedPinnedLease:
        del timeout
        return _BorrowedPinnedLease(self._outer)


def _run_locked_callback(
    provider: Any,
    pool: Any,
    work_callback: Callable[[Any], Any],
) -> dict[str, Any]:
    """Run a test/maintenance callback on one lock-held physical connection.

    The production Observer/E1 callbacks use their existing business functions;
    this explicit seam is also used by the isolated two-process contract so no
    LLM or business-table write is needed to prove the ownership boundary.
    """
    try:
        acquired = provider.acquire()
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        return {
            "ran": False,
            "status": "lock_error",
            "reason": "lock_error",
            "error": f"lock acquire raised: {type(exc).__name__}",
        }

    if not isinstance(acquired, dict) or not acquired.get("granted"):
        if isinstance(acquired, dict) and acquired.get("error"):
            return {
                "ran": False,
                "status": "lock_error",
                "reason": "lock_error",
                "error": str(acquired.get("error"))[:200],
                "owner": acquired.get("owner", "unknown"),
            }
        return {
            "ran": False,
            "status": "busy",
            "reason": "lock_busy",
            "skipped": True,
            "owner": acquired.get("owner", "unknown")
            if isinstance(acquired, dict)
            else "unknown",
        }

    conn = getattr(provider, "connection", None)
    if conn is None:
        # Compatibility with an older provider implementation while keeping
        # the public service seam strict for the current daemon adapter.
        conn = getattr(provider, "_conn", None)
    result: dict[str, Any] = {
        "ran": False,
        "status": "executed",
        "reason": "ok",
        "owner": acquired.get("owner"),
        "lease_in_use_at_acquire": _pool_in_use(pool),
    }
    result["acquire_backend_pid"] = _backend_pid(conn)

    try:
        callback_result = work_callback(conn)
        result["ran"] = True
        if isinstance(callback_result, int) and not isinstance(callback_result, bool):
            result["work_backend_pid"] = int(callback_result)
        else:
            result["work_backend_pid"] = _backend_pid(conn) or result.get(
                "acquire_backend_pid"
            )
        result["lease_in_use_at_work"] = _pool_in_use(pool)
    except Exception as exc:
        result.update(
            {
                "status": "error",
                "reason": "callback_error",
                "callback_error": f"{type(exc).__name__}: {str(exc)[:180]}",
            }
        )
    finally:
        result["unlock_backend_pid"] = _backend_pid(conn) or result.get(
            "work_backend_pid"
        )
        result["lease_in_use_at_unlock"] = _pool_in_use(pool)
        try:
            # PgAdvisoryLock.release() performs unlock on this same connection
            # before closing/returning its lease.
            provider.release()
            result["unlock_before_lease_close"] = True
        except Exception as exc:  # pragma: no cover - adapter is fail-soft
            result["unlock_before_lease_close"] = False
            result["release_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        result["lease_in_use_after_close"] = _pool_in_use(pool)

    return result


class E1Service:
    """One Runtime-owned E1 scheduler and its lock provider."""

    def __init__(
        self,
        *,
        config: Any,
        pool: Any,
        state_dir: str | Path,
        on_topics_commit: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self.pool = pool
        self.state_dir = Path(state_dir)
        self._on_topics_commit = on_topics_commit
        self._accepting = True
        self._closed = False
        self._execution_lock = threading.RLock()
        # Stage 7: pending topic-mutation marker. It is armed by
        # mark_topics_commit() and consumed only by _after_release_handler()
        # after a successful scheduler release. It must survive across callback
        # invocations and release failures; a later successful lease exit may
        # consume an older pending marker.
        self._pending_topics_dirty = False
        # PgAdvisoryLock keeps mutable pinned-lease state on the provider
        # object. Scheduler ticks and explicit/nested execution therefore
        # need separate handles even though they contend on the same PG key.
        self._scheduler_lock_provider = build_default_lock_provider(
            config=config,
            name="e1_singleton",
            pool=pool,
        )
        self._execution_lock_provider = build_default_lock_provider(
            config=config,
            name="e1_singleton",
            pool=pool,
        )
        # Backward-compatible internal seam: execution owns this alias;
        # E1Scheduler is wired only to _scheduler_lock_provider above.
        self._lock_provider = self._execution_lock_provider
        self._scheduler = E1Scheduler(
            state_dir=self.state_dir,
            callback=self._callback,
            lock_provider=self._scheduler_lock_provider,
            after_release_callback=self._after_release_handler,
        )

    @property
    def scheduler_thread(self) -> threading.Thread | None:
        return getattr(self._scheduler, "thread", None) or getattr(
            self._scheduler, "_thread", None
        )

    @property
    def thread(self) -> threading.Thread | None:
        return self.scheduler_thread

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def accepting_work(self) -> bool:
        return self._accepting and not self._closed

    @property
    def scheduler(self) -> E1Scheduler:
        return self._scheduler

    @property
    def scheduler_lock_provider(self) -> Any:
        return self._scheduler_lock_provider

    @property
    def execution_lock_provider(self) -> Any:
        return self._execution_lock_provider

    @property
    def status(self) -> str:
        return self._scheduler.status

    def start(self) -> None:
        if not self.accepting_work:
            return
        self._scheduler.start()

    def begin_draining(self) -> None:
        self._accepting = False

    def _callback(self) -> str:
        with self._execution_lock:
            from .e1 import synthesize_yin

            outer_lease = self._scheduler_lock_provider.lease
            if outer_lease is None:
                raise RuntimeError(
                    "E1 callback has no pinned scheduler lease; refusing raw pool fallback"
                )
            # Stage 7: hand synthesize_yin the marker, NOT the real cache callback.
            # The actual cache.invalidate() runs in _after_release_handler, strictly
            # AFTER scheduler unlock + outer lease close. T2 / T4 contract.
            result = synthesize_yin(
                config=self.config,
                pool=_PinnedSchedulerPoolView(outer_lease),
                on_topics_commit=self.mark_topics_commit,
            )
            # Preserve the old Core callback's optional dreamer pass without
            # making Dreamer an owner of the Runtime service.
            try:
                cards_dir = self.state_dir / "cards"
                if cards_dir.exists():
                    from .dreamer import run_dreamer_pass

                    dreamer_result = run_dreamer_pass(self.state_dir)
                    if dreamer_result.get("boosted", 0) > 0 or dreamer_result.get(
                        "verified", 0
                    ) > 0:
                        result = (
                            f"{result} (dreamer boosted="
                            f"{dreamer_result.get('boosted', 0)} verified="
                            f"{dreamer_result.get('verified', 0)})"
                        )
            except Exception:
                pass
            return str(result or "")

    def mark_topics_commit(self) -> None:
        """Stage 7 marker: synthesize_yin (or nested E1) reports a topic merge.

        Must not be the real cache.invalidate(). The actual invalidate is
        invoked by _after_release_handler after the scheduler releases the
        pinned lease — see E1Scheduler(after_release_callback=...).

        用 ``self._execution_lock`` 同步：与 ``_after_release_handler`` 的
        读+清零配对，避免在 callback 抛异常、handler 正在锁外执行 cb 时
        又被另一条路径 flip dirty flag。
        """
        with self._execution_lock:
            self._pending_topics_dirty = True

    def _after_release_handler(self) -> None:
        """Stage 7 deferred invalidation hook (runs in E1Scheduler finally).

        Strictly invoked AFTER lock_provider.release() succeeded on the
        callback-entered path. Reads + clears the dirty flag; if and only if
        a topic merge actually happened during the callback, invokes the
        original on_topics_commit (the real cache invalidation).

        lock 协议：读 + 清零 + 复制 callback 都在 ``self._execution_lock``
        内；actual ``self._on_topics_commit()`` 在锁外执行，避免 cache
        callback 反向去拿 service lock 形成自死锁。dirty=False / cb 不可调
        用时直接 return，绝不空跑。
        """
        import logging as _logging
        with self._execution_lock:
            dirty = bool(self._pending_topics_dirty)
            self._pending_topics_dirty = False
            if not dirty:
                return
            cb = self._on_topics_commit
            if not callable(cb):
                return
        # ── 锁外执行 actual cache invalidate，绝不在持锁状态跑。 ──
        try:
            cb()
        except Exception as _e_invalidate:
            _logging.getLogger("v3core.runtime_services").warning(
                "E1Service: cache invalidation failed (post-release, non-fatal): %s",
                _safe_err(_e_invalidate)[:200],
            )

    def run_once(
        self,
        *,
        work_callback: Callable[[Any], Any] | None = None,
    ) -> dict[str, Any]:
        if not self.accepting_work:
            return _rejected()
        with self._execution_lock:
            if work_callback is not None:
                return _run_locked_callback(
                    self._execution_lock_provider,
                    self.pool,
                    work_callback,
                )
            outcome = self._scheduler.tick()
            return outcome if isinstance(outcome, dict) else {"ran": bool(outcome)}

    run_locked = run_once

    def run_business_locked(
        self,
        callback: Callable[[], Any],
        *,
        lease: Any = None,
    ) -> dict[str, Any]:
        """Run an existing E1 business callback under ``e1_singleton``.

        When an Observer worker already owns a pinned lease, the E1 lock is
        layered on that same physical connection and releases only the inner
        advisory lock. The Observer owner remains responsible for closing the
        outer lease.
        """
        if not self.accepting_work:
            return _rejected()
        with self._execution_lock:
            try:
                if lease is None:
                    acquired = self._execution_lock_provider.acquire()
                else:
                    acquired = self._execution_lock_provider.acquire_on_lease(
                        lease,
                        release_lease=False,
                    )
            except Exception as exc:
                return {
                    "ran": False,
                    "status": "lock_error",
                    "reason": "lock_error",
                    "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                }
            if not isinstance(acquired, dict) or not acquired.get("granted"):
                if isinstance(acquired, dict) and acquired.get("error"):
                    return {
                        "ran": False,
                        "status": "lock_error",
                        "reason": "lock_error",
                        "error": str(acquired.get("error"))[:200],
                    }
                return {
                    "ran": False,
                    "status": "busy",
                    "reason": "lock_busy",
                    "skipped": True,
                }
            try:
                return {
                    "ran": True,
                    "status": "executed",
                    "reason": "ok",
                    "result": callback(),
                }
            finally:
                self._execution_lock_provider.release()

    def close(self, timeout: float | None = 5.0) -> None:
        self.begin_draining()
        if self._closed:
            return
        self._scheduler.shutdown(timeout=timeout)
        self._closed = True


class ObserverService:
    """One Runtime-owned Observer trigger worker and advisory-lock boundary."""

    def __init__(
        self,
        *,
        config: Any,
        pool: Any,
        e1_service: E1Service | None = None,
        on_topics_commit: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self.pool = pool
        self._e1_service = e1_service
        self._default_on_topics_commit = on_topics_commit
        self._lock_provider = build_default_lock_provider(
            config=config,
            name="observer_singleton",
            pool=pool,
        )
        self._state_lock = threading.RLock()
        # Worker and run_once/maintenance both touch the stateful provider;
        # serialize those paths without changing the PG lock key.
        self._execution_lock = threading.RLock()
        self._last_attempt_monotonic: float | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._pending: dict[str, Any] | None = None
        self._worker_thread: threading.Thread | None = None
        self._accepting = True
        self._closed = False
        self.last_result: Any = None

    @property
    def worker_thread(self) -> threading.Thread | None:
        return self._worker_thread

    @property
    def thread(self) -> threading.Thread | None:
        return self._worker_thread

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def accepting_work(self) -> bool:
        return self._accepting and not self._closed

    def claim_debounce(
        self,
        debounce_seconds: float = 10.0,
        *,
        now: float | None = None,
        window: float | None = None,
    ) -> bool:
        """Atomically claim this Runtime's inline debounce window.

        ``now`` is injectable for deterministic tests; production uses the
        monotonic clock. Runtime-backed calls must not touch the legacy module
        global in ``observer.py`` because Runtime instances are independent.
        """
        if window is not None:
            debounce_seconds = window
        try:
            debounce = max(0.0, float(debounce_seconds))
        except (TypeError, ValueError):
            debounce = 0.0
        current = time.monotonic() if now is None else float(now)
        with self._state_lock:
            previous = self._last_attempt_monotonic
            if previous is not None and current - previous < debounce:
                return False
            self._last_attempt_monotonic = current
            return True

    def start(self) -> None:
        with self._state_lock:
            if not self.accepting_work:
                return
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._worker_thread = threading.Thread(
                target=self._loop,
                name="v3-runtime-observer-worker",
                daemon=True,
            )
            self._worker_thread.start()

    def begin_draining(self) -> None:
        with self._state_lock:
            self._accepting = False
            pending = self._pending
            self._pending = None
            self._wake.set()
        if pending is not None:
            done = pending.get("_done")
            result_box = pending.get("_result_box")
            if done is not None and result_box is not None:
                result_box["result"] = _rejected()
                done.set()

    def trigger(
        self,
        *,
        config: Any = None,
        dry_run: bool = False,
        on_topics_commit: Callable[[], Any] | None = None,
        work_callback: Callable[[Any], Any] | None = None,
        wait_timeout: float = 60.0,
        **request: Any,
    ) -> dict[str, Any]:
        with self._state_lock:
            if not self.accepting_work:
                return _rejected()
            if self._worker_thread is None or not self._worker_thread.is_alive():
                self.start()
            done = threading.Event() if work_callback is not None else None
            result_box: dict[str, Any] | None = {} if done is not None else None
            self._pending = {
                "config": self.config if config is None else config,
                "dry_run": bool(dry_run),
                "on_topics_commit": on_topics_commit
                or self._default_on_topics_commit,
                "_work_callback": work_callback,
                "_done": done,
                "_result_box": result_box,
                **request,
            }
            self._wake.set()
        if done is not None and result_box is not None:
            if not done.wait(timeout=max(0.0, float(wait_timeout))):
                return {
                    "ran": False,
                    "status": "timeout",
                    "reason": "observer_worker_timeout",
                }
            result = result_box.get("result")
            return result if isinstance(result, dict) else {"result": result}
        return {"accepted": True, "status": "queued", "reason": "queued"}

    submit_trigger = trigger
    submit = trigger

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=0.5)
            if self._stop.is_set():
                break
            self._wake.clear()
            with self._state_lock:
                request = self._pending
                self._pending = None
            if request is None or not self.accepting_work:
                continue
            try:
                work_callback = request.get("_work_callback")
                if callable(work_callback):
                    self.last_result = self.run_once(work_callback=work_callback)
                else:
                    from .observer import maybe_observe

                    self.last_result = maybe_observe(
                        request.get("config", self.config),
                        dry_run=bool(request.get("dry_run", False)),
                        pool=self.pool,
                        on_topics_commit=request.get("on_topics_commit"),
                        _inline=True,
                        _runtime_service=self,
                    )
            except Exception as exc:
                self.last_result = {
                    "ran": False,
                    "status": "error",
                    "reason": "observer_error",
                    "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                }
            finally:
                done = request.get("_done")
                result_box = request.get("_result_box")
                if done is not None and result_box is not None:
                    result_box["result"] = self.last_result
                    done.set()

    def run_once(
        self,
        *,
        work_callback: Callable[[Any], Any] | None = None,
    ) -> dict[str, Any]:
        if not self.accepting_work:
            return _rejected()
        if work_callback is None:
            return {
                "ran": False,
                "status": "unsupported",
                "reason": "work_callback_required",
            }
        with self._execution_lock:
            return _run_locked_callback(
                self._lock_provider,
                self.pool,
                work_callback,
            )

    run_locked = run_once

    def _execute_observer_worker(
        self,
        worker_cfg: Any,
        pg_dsn: str,
        dry_run: bool,
        on_topics_commit: Callable[[], Any] | None,
    ) -> dict[str, Any]:
        """Run the existing Observer worker under one pinned lock lease."""
        if not self.accepting_work:
            return _rejected()
        with self._execution_lock:
            return self._execute_observer_worker_locked(
                worker_cfg,
                pg_dsn,
                dry_run,
                on_topics_commit,
            )

    def _execute_observer_worker_locked(
        self,
        worker_cfg: Any,
        pg_dsn: str,
        dry_run: bool,
        on_topics_commit: Callable[[], Any] | None,
    ) -> dict[str, Any]:
        lease = None
        acquired: dict[str, Any] | None = None
        callback_seen = False
        callback = on_topics_commit

        def mark_topics_commit() -> None:
            nonlocal callback_seen
            callback_seen = True

        try:
            lease = self.pool.lease(timeout=5)
            acquire_on_lease = getattr(self._lock_provider, "acquire_on_lease", None)
            if not callable(acquire_on_lease):
                lease.close()
                return {
                    "ran": False,
                    "status": "lock_error",
                    "reason": "lock_adapter_missing_acquire_on_lease",
                }
            acquired = acquire_on_lease(lease)
            if not isinstance(acquired, dict) or not acquired.get("granted"):
                if isinstance(acquired, dict) and acquired.get("error"):
                    return {
                        "ran": False,
                        "status": "lock_error",
                        "reason": "lock_error",
                        "error": str(acquired.get("error"))[:200],
                    }
                return {
                    "ran": False,
                    "status": "busy",
                    "reason": "lock_busy",
                    "skipped": True,
                    "owner": acquired.get("owner", "unknown")
                    if isinstance(acquired, dict)
                    else "unknown",
                }

            from .observer import _observe_worker

            _observe_worker(
                worker_cfg,
                pg_dsn,
                dry_run=dry_run,
                pool=self.pool,
                on_topics_commit=mark_topics_commit,
                _pinned_lease=lease,
                _manage_lease=False,
                _e1_service=self._e1_service,
            )
            return {
                "ran": True,
                "status": "executed",
                "reason": "ok",
                "owner": acquired.get("owner"),
            }
        finally:
            if acquired and acquired.get("granted"):
                try:
                    self._lock_provider.release()
                finally:
                    if callback_seen and callable(callback):
                        try:
                            callback()
                        except Exception:
                            pass
            elif lease is not None:
                try:
                    lease.close()
                except Exception:
                    pass

    def close(self, timeout: float | None = 5.0) -> None:
        self.begin_draining()
        if self._closed:
            return
        self._stop.set()
        self._wake.set()
        thread = self._worker_thread
        if thread is not None and thread is not threading.current_thread():
            remaining = None
            if timeout is not None:
                remaining = max(0.0, float(timeout))
            thread.join(timeout=remaining)
        if thread is not None and thread.is_alive():
            raise TimeoutError("ObserverService worker did not stop before deadline")
        self._closed = True


__all__ = ["E1Service", "ObserverService"]
