# -*- coding: utf-8 -*-
"""Runtime-owned TopicRecall cache + COUNT(*) fallback refresher.

Lifecycle seam observed by Stage 5 contracts (not scoring internals):
    version / invalidate() / refresher_thread / closed / close(timeout=)
    get_topic_recall()  — recall_pool / Core hot path

Unit construction does not touch PostgreSQL. The refresher waits 60s before
the first COUNT(*) and swallows pool/query errors. close(timeout) uses one
deadline covering lock acquire, setting closed/stop, and refresher join.

Lazy get/load/publish holds the cache lock (TopicRecall load has no cache
callback). invalidate() drops the snapshot so the next get constructs a
fresh one; after close it is a no-op under the same lock. A closed cache
never loads. Config ValueError from safe_embed_cfg stays fail-closed
(not disguised as disabled).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger("v3core.topic_recall_cache")

__all__ = ["TopicRecallCache", "TopicRecallCacheCloseTimeout"]


class TopicRecallCacheCloseTimeout(TimeoutError):
    """close(timeout) missed its deadline before the refresher fully stopped."""


def _acquire_lock(lock: threading.Lock, timeout: float | None) -> bool:
    """Acquire *lock* honoring threading.Lock zero/positive/None boundaries.

    ``timeout=None`` blocks forever (``acquire()``, never ``timeout=None``).
    ``timeout<=0`` is non-blocking (``acquire(False)``).
    ``timeout>0`` waits up to that many seconds.
    """
    if timeout is None:
        return bool(lock.acquire())
    if timeout <= 0:
        return bool(lock.acquire(False))
    return bool(lock.acquire(timeout=timeout))

_COUNT_SQL = (
    "SELECT COUNT(*) FROM topics "
    "WHERE status='active' AND embedding IS NOT NULL"
)


class TopicRecallCache:
    """One TopicRecall snapshot + one 60s COUNT(*) fallback refresher per Runtime."""

    def __init__(self, pool: Any, config: Any = None) -> None:
        self._pool = pool
        self._config = config
        self._lock = threading.Lock()
        self._version = 0
        self._closed = False
        self._stale = False
        self._topic_recall: Any | None = None
        self._topic_recall_count = 0
        self._stop = threading.Event()
        self.refresher_thread = threading.Thread(
            target=self._refresh_loop,
            name="v3runtime-topic-refresher",
            daemon=True,
        )
        self.refresher_thread.start()

    @property
    def version(self) -> int:
        return self._version

    @property
    def closed(self) -> bool:
        return self._closed

    def invalidate(self) -> None:
        """Write-side notification: version += 1 and drop the in-memory snapshot."""
        with self._lock:
            if self._closed:
                return
            self._version += 1
            self._stale = True
            self._topic_recall = None

    def get_topic_recall(self) -> Any:
        """Return the current TopicRecall, creating/loading lazily.

        First generation uses TopicRecall._ensure_loaded() (NPZ fast path).
        An invalidated generation constructs a new object and loads via
        _load_from_source() so same-count updates bypass a stale on-disk NPZ.

        The lock is held across the single lazy load/publish so concurrent
        first callers share one snapshot and invalidate() cannot interleave
        a stale publish.
        """
        with self._lock:
            if self._closed:
                return self._topic_recall
            if self._topic_recall is not None:
                return self._topic_recall
            recall = self._new_topic_recall()
            self._load_snapshot(recall, stale=self._stale)
            if self._closed:
                return None
            self._topic_recall = recall
            if self._stale:
                self._stale = False
            return self._topic_recall

    def close(self, timeout: float | None = 5.0) -> None:
        """Stop the refresher thread under a single deadline.

        ``timeout=None`` waits forever for the lock and the join.
        ``timeout<=0`` is signal-only (no join) so Registry zero-timeout drain
        probes stay bounded. A positive timeout covers lock acquire + setting
        closed/stop + join; lock acquire failure does not set closed/stop.
        Join timeout after stop/closed is already set stays retryable. A
        positive-timeout close must not succeed while the refresher is alive.
        """
        # timeout<=0 is the Stage 2 zero-timeout probe: acquire lock without
        # blocking, signal stop/closed, do not join. Positive / None timeouts
        # cover lock + join under one deadline and fail if the refresher lives.
        if timeout is not None and float(timeout) <= 0:
            if not _acquire_lock(self._lock, 0.0):
                raise TopicRecallCacheCloseTimeout(
                    "TopicRecallCache.close timed out waiting for lock"
                )
            try:
                self._stop.set()
                self._closed = True
            finally:
                self._lock.release()
            return

        deadline = None if timeout is None else time.monotonic() + float(timeout)

        def remaining() -> float | None:
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        thread = self.refresher_thread
        already_signaled = self._closed and self._stop.is_set()
        if already_signaled and (thread is None or not thread.is_alive()):
            return

        if not already_signaled:
            if not _acquire_lock(self._lock, remaining()):
                raise TopicRecallCacheCloseTimeout(
                    "TopicRecallCache.close timed out waiting for lock"
                )
            try:
                self._stop.set()
                self._closed = True
            finally:
                self._lock.release()

        thread = self.refresher_thread
        if thread is not None and thread.is_alive():
            join_timeout = remaining()
            if join_timeout is None:
                thread.join()
            else:
                thread.join(timeout=join_timeout)
            if thread.is_alive():
                raise TopicRecallCacheCloseTimeout(
                    "TopicRecallCache.close timed out waiting for refresher"
                )

    def _new_topic_recall(self) -> Any:
        from .embedding import safe_embed_cfg
        from .topic_recall import TopicRecall

        embed_cfg = None
        if self._config is not None:
            try:
                embed_cfg = safe_embed_cfg(self._config)
            except ValueError:
                raise
            except Exception:
                embed_cfg = None
        pool = self._pool
        if pool is not None:
            return TopicRecall(embed_cfg, pool=pool)
        return TopicRecall(embed_cfg)

    @staticmethod
    def _load_snapshot(recall: Any, *, stale: bool) -> None:
        if stale:
            load_from_source = getattr(recall, "_load_from_source", None)
            if callable(load_from_source):
                load_from_source()
                if getattr(recall, "_topics", None):
                    save = getattr(recall, "_save_npz_cache", None)
                    if callable(save):
                        save()
                return
        ensure = getattr(recall, "_ensure_loaded", None)
        if callable(ensure):
            ensure()

    def _refresh_loop(self) -> None:
        # Match legacy Core refresher: first wait(60) so a caller can load first.
        self._stop.wait(60)
        while not self._stop.is_set():
            try:
                new_count = self._count_active()
                if new_count is not None:
                    with self._lock:
                        if not self._closed:
                            current = self._topic_recall_count
                            stale = self._stale
                            if stale or new_count != current:
                                recall = self._new_topic_recall()
                                self._load_snapshot(recall, stale=stale)
                                if not self._closed:
                                    self._topic_recall = recall
                                    self._topic_recall_count = new_count
                                    if stale:
                                        self._stale = False
                                    logger.info(
                                        "topic_recall 缓存已刷新: %d topics",
                                        new_count,
                                    )
            except Exception:
                pass
            self._stop.wait(60)

    def _count_active(self) -> int | None:
        pool = self._pool
        lease_fn = getattr(pool, "lease", None)
        if not callable(lease_fn):
            return None
        lease = lease_fn()
        try:
            conn = getattr(lease, "connection", lease)
            if conn is None:
                return None
            cur = conn.cursor()
            try:
                cur.execute(_COUNT_SQL)
                row = cur.fetchone()
                if row is None:
                    return None
                return int(row[0])
            finally:
                close = getattr(cur, "close", None)
                if callable(close):
                    close()
        finally:
            close = getattr(lease, "close", None)
            if callable(close):
                close()
