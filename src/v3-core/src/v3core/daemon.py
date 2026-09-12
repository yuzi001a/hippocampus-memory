"""v3core.daemon — 进程级单例守护（P0-C 单飞边界 / P1 PgPool 解耦）。

roadmap §5.1/§P0-C：gateway 与 desktop 可能是两个 OS 进程，每个 Hermes session
又会各自创建 V3Core → 每个 Core 都拉起一套 E1Scheduler / observer。跨进程单飞
不能靠 Python singleton，必须用数据库级互斥（PostgreSQL advisory lock）或等价的
注入式 lock provider。

本模块提供：

1. ``PgAdvisoryLock`` — PostgreSQL advisory lock 适配层。
   - 会话级 try-lock（``pg_try_advisory_lock``），拿不到立即返回不阻塞；
   - key 用 ``hashtext('v3core:singleton:<name>')`` 派生；
   - owner 标识 ``<hostname>:<pid>``，写进日志与争抢结果便于排障；
   - **连接由调用方工厂提供**：advisory lock 是会话级锁，连接断开自动释放
     （进程崩溃不死锁）；持锁期间锁定连接/lease 句柄，release() 先 unlock 后归还/关闭。
2. ``build_default_lock_provider`` — V3Core.initialize / scheduler 入口的装配点，
   支持直接注入 PgPool 或从 config 的 storage.pg 段构造连接工厂（懒连接，不触碰生产）。
3. ``try_acquire_singleton_lock`` / ``probe_singleton_lock_owner`` —
   health/运维探测用的无副作用辅助函数。

失败语义（fail-closed）：PG 不可达 / 连接工厂失败 / 任何异常 → 返回
``{"granted": False, "error": ...}``，绝不静默放行。调用方（scheduler tick）
据此跳过本轮并记录显式状态。

已知边界（第一轮，如实声明）：
- 会话级 advisory lock 绑定在传入的连接上；上层需保证该连接生命周期 ≥ 调度器
  生命周期，且 release 前不 close（连接关闭 = 锁自动消失，语义仍安全）。
- observer 批量观察入口尚未接锁（observer.py 不在本轮允许修改范围内）；
  ``e1_singleton`` 目前只能挡 E1↔E1 竞态。
"""
from __future__ import annotations

import logging
import os
import socket
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger("v3core.daemon")

# 默认锁名：E1 单实例边界（observer 接入后可加独立锁名）。
DEFAULT_LOCK_NAME = "e1_singleton"

_ADVISORY_KEY_SALT = "v3core:singleton:"


def default_owner_id() -> str:
    """跨进程可读的 owner 标识：<host>:<pid>。"""
    return f"{socket.gethostname()}:{os.getpid()}"


def advisory_lock_key(lock_name: str) -> str:
    """完整锁 key 字符串（SQL 侧再经 hashtext 变整数）。"""
    return f"{_ADVISORY_KEY_SALT}{lock_name}"


@runtime_checkable
class SingletonLockProvider(Protocol):
    """scheduler 依赖的最小锁契约（注入式，测试用 fake 实现）。"""

    def acquire(self) -> dict: ...

    def release(self) -> None: ...


class _PooledLeaseConnection:
    """PgLease 包装层：提供标准连接接口并在 close() 时仅归还 lease（绝不物理关闭池连接）。"""

    def __init__(self, lease: Any) -> None:
        self._lease = lease
        self._closed = False

    @property
    def connection(self) -> Any:
        if self._lease is not None:
            return getattr(self._lease, "connection", self._lease)
        return None

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        return self._lease.connection.cursor(*args, **kwargs)

    def commit(self) -> None:
        if self._lease is not None:
            self._lease.connection.commit()

    def rollback(self) -> None:
        if self._lease is not None:
            self._lease.connection.rollback()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._lease is not None:
                try:
                    self._lease.close()
                except Exception:
                    logger.debug("归还 lease 失败", exc_info=True)

    def _connect(self) -> Any:
        return self.connection

    def __getattr__(self, name: str) -> Any:
        if self._lease is not None:
            return getattr(self._lease.connection, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")


class PgAdvisoryLock:
    """PostgreSQL advisory lock 最小适配层。

    Args:
        conn_factory: 返回 psycopg2 connection（或兼容对象）的可调用对象；
            每次操作调用以获取会话连接。抛异常/返回 None = PG 不可达。
        name: 锁名（默认 ``e1_singleton``）。
        owner_id: 本进程标识；争抢失败时用于日志与结果回填。
        session_scope: True（调度器场景）= acquire 成功后缓存该连接并持有到
            release()；False（health 探测场景）= 仅本次调用持锁，release 时释放。
        pool: 可选注入的 PgPool 实例。
    """

    def __init__(
        self,
        conn_factory: Callable[[], Any],
        name: str = DEFAULT_LOCK_NAME,
        owner_id: str | None = None,
        session_scope: bool = True,
        pool: Any = None,
    ):
        self._conn_factory = conn_factory
        self.name = name
        self.owner_id = owner_id or default_owner_id()
        self._session_scope = session_scope
        self._pool = pool
        self.pool = pool
        self._conn: Any = None
        self._held = False
        self._release_on_close = True

    @property
    def _lease(self) -> Any:
        if self._conn is not None:
            return getattr(self._conn, "_lease", None)
        return None

    @property
    def connection(self) -> Any:
        """The currently pinned connection, for a lock-held service callback."""
        conn = self._conn
        if conn is None:
            return None
        return getattr(conn, "connection", conn)

    @property
    def lease(self) -> Any:
        """The currently pinned PgLease, when the provider uses a pool."""
        return self._lease

    # ── 内部 ──

    def _get_conn(self):
        if self._session_scope and self._conn is not None:
            return self._conn
        return self._conn_factory()

    @staticmethod
    def _run(conn, sql: str, params: tuple | None = None):
        """执行单条 SQL 并取一行；游标即开即关。"""
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchone()
        finally:
            try:
                cur.close()
            except Exception:
                pass

    @staticmethod
    def _close_conn(conn: Any) -> None:
        """关闭或归还连接/lease，静默忽略关闭异常。"""
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            logger.debug("连接关闭/归还失败", exc_info=True)

    # ── 协议实现 ──

    def acquire(self) -> dict:
        """尝试获取锁。总返回 dict、绝不抛出（fail-closed）。

        - granted=True  → 本进程持锁（连接被缓存供 release 用）
        - granted=False → 别人持有（带 owner/pid）或 PG 不可用（带 error）
        """
        conn = None
        try:
            conn = self._get_conn()
        except Exception as e:
            return {"granted": False, "error": f"pg connect failed: {str(e)[:150]}"}
        if conn is None:
            return {"granted": False, "error": "pg not connected (conn factory returned None)"}
        self._release_on_close = True

        key = advisory_lock_key(self.name)
        try:
            row = self._run(
                conn,
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS granted",
                (key,),
            )
            granted = bool(row and row[0])
            if granted:
                self._conn = conn
                self._held = True
                logger.info(
                    "singleton lock acquired: name=%s owner=%s", self.name, self.owner_id
                )
                return {"granted": True, "owner": self.owner_id}
            # 未拿到：尽力查当前持有者信息（只读、失败不影响主结果）
            try:
                owner_info = self._probe_holder(conn, key)
            except Exception:
                owner_info = {"owner": "unknown"}
            result = {"granted": False}
            result.update(owner_info)
            logger.info(
                "singleton lock busy: name=%s holder=%s", self.name, owner_info.get("owner")
            )
            return result
        except Exception as e:
            # SQL 失败（权限/schema/驱动问题）：显式 error，绝不放行
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("singleton lock acquire 失败: %s", e)
            return {"granted": False, "error": str(e)[:200]}
        finally:
            if not self._held:
                self._close_conn(conn)

    def acquire_on_lease(self, lease: Any, *, release_lease: bool = True) -> dict:
        """Acquire on an already borrowed lease and retain it until release().

        Runtime services use this form when the same physical connection must
        carry the advisory lock and the work callback.  A failed try-lock still
        closes/returns the supplied lease; a granted lock transfers that lease
        to this provider, whose ``release()`` unlocks before returning it.
        """
        if lease is None:
            return {"granted": False, "error": "pinned lease is missing"}
        if self._held:
            return {"granted": False, "error": "lock provider already holds a lease"}

        conn = _PooledLeaseConnection(lease)
        self._release_on_close = bool(release_lease)
        key = advisory_lock_key(self.name)
        try:
            row = self._run(
                conn,
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS granted",
                (key,),
            )
            granted = bool(row and row[0])
            if granted:
                self._conn = conn
                self._held = True
                logger.info(
                    "singleton lock acquired on supplied lease: name=%s owner=%s",
                    self.name,
                    self.owner_id,
                )
                return {"granted": True, "owner": self.owner_id}

            try:
                owner_info = self._probe_holder(conn, key)
            except Exception:
                owner_info = {"owner": "unknown"}
            result = {"granted": False}
            result.update(owner_info)
            return result
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            return {"granted": False, "error": str(exc)[:200]}
        finally:
            if not self._held and self._release_on_close:
                self._close_conn(conn)

    def _probe_holder(self, conn, key: str) -> dict:
        """查 advisory lock 当前持有者（pg_locks/pg_stat_activity 只读）。"""
        try:
            row = self._run(
                conn,
                """
                SELECT a.pid,
                       COALESCE(a.application_name, '') AS app_name,
                       EXTRACT(EPOCH FROM (NOW() - a.query_start))::bigint AS age_seconds
                  FROM pg_locks l
                  JOIN pg_stat_activity a ON a.pid = l.pid
                 WHERE l.locktype = 'advisory'
                   AND l.objid = hashtext(%s)
                   AND l.granted
                 LIMIT 1
                """,
                (key,),
            )
            if row and row[0] is not None:
                return {
                    "owner": f"pid={row[0]}",
                    "holder_pid": int(row[0]),
                    "app_name": row[1] or "",
                    "age_seconds": row[2] if row[2] is not None else None,
                }
            # pg_locks 查不到（如其他后端刚释放）：至少给出 key 供人工比对
            return {"owner": f"unknown(holder of {key})"}
        except Exception as e:
            # 只读探查失败不影响"未拿到锁"这一主判定
            try:
                conn.rollback()
            except Exception:
                pass
            return {"owner": "unknown", "probe_error": str(e)[:120]}

    def release(self) -> None:
        """释放锁（会话级）。先执行 pg_advisory_unlock，再关闭/归还连接句柄。幂等。"""
        conn = self._conn
        self._conn = None
        self._held = False
        if conn is None:
            return
        key = advisory_lock_key(self.name)
        try:
            self._run(conn, "SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        except Exception as e:
            logger.warning(
                "singleton lock release 失败（连接回收时会自动释放）: %s", str(e)[:120]
            )
        finally:
            if self._release_on_close:
                self._close_conn(conn)


def build_default_lock_provider(
    config: Any = None,
    name: str = DEFAULT_LOCK_NAME,
    pool: Any = None,
) -> PgAdvisoryLock:
    """从 v3 config 或注入的 PgPool 构造默认 advisory-lock provider（懒连接，不主动触生产）。

    config 兼容 dict / V3Config；解析失败时工厂会在 acquire 时返回显式错误。
    若传入 pool，则使用 pool.lease(timeout=5) 获取连接包装，不触碰 PgEmbedStore/psycopg2.connect。
    """
    if pool is not None:
        def _pool_factory():
            lease = pool.lease(timeout=5)
            return _PooledLeaseConnection(lease)

        return PgAdvisoryLock(conn_factory=_pool_factory, name=name, pool=pool)

    def _factory():
        from .pg_store import PgEmbedStore

        store = PgEmbedStore(config=config)
        return store._connect()

    return PgAdvisoryLock(conn_factory=_factory, name=name)


def try_acquire_singleton_lock(provider) -> dict:
    """acquire + 自动释放的一次性尝试（health/启动诊断用，不长期持锁）。"""
    res = provider.acquire()
    try:
        provider.release()
    except Exception:
        pass
    return res


def probe_singleton_lock_owner(conn_factory, name: str = DEFAULT_LOCK_NAME) -> dict:
    """只读探测当前锁持有者（不改变锁状态）。"""
    lk = PgAdvisoryLock(conn_factory=conn_factory, name=name, session_scope=False)
    try:
        res = lk.acquire()
        if res.get("granted"):
            # 空闲锁：无人持有
            return {"locked": False}
        out = {"locked": True}
        out.update({k: v for k, v in res.items() if k != "granted"})
        return out
    finally:
        try:
            lk.release()
        except Exception:
            pass
