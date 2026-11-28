"""回归: v3_health 的 prefetch 自检不能把"吞掉异常"当成 working。

背景（生产实测 2026-11-28）：health.py 用 _PrefetchPgView 包住一条借来的连接传进
prefetch；active_memory_store._acquire_lease 需要 pg.lease()，而 _PrefetchPgView
没有这个方法 → AttributeError 被 search_keyword 吞掉 → 返回空列表 →
health 仍然报 {"working": True, "hit_count": 0}。也就是说这条自检永远不会失败。
"""
from __future__ import annotations

import contextlib

import pytest

from v3core.tools import health as health_mod
from v3core.tools.health import _PrefetchPgView, _prefetch_self_check


class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.closed = False

    def execute(self, sql: str, params=None) -> None:
        self.statements.append(sql)

    def fetchone(self):
        return (1,)

    def close(self) -> None:
        self.closed = True


class FakeConn:
    def __init__(self) -> None:
        self.closed = False
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        cur = FakeCursor()
        self.cursors.append(cur)
        return cur

    def close(self) -> None:  # pragma: no cover - must never be called here
        self.closed = True


class FakeStore:
    """Stands in for PgEmbedStore / PgPool: lease() yields the shared connection."""

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn
        self.leases = 0

    @contextlib.contextmanager
    def lease(self, timeout=None, *, deadline=None):
        self.leases += 1
        yield self._conn


def test_view_lease_is_pglease_shaped_and_never_closes_borrowed_conn():
    conn = FakeConn()
    view = _PrefetchPgView(conn)

    with view.lease(timeout=1.0) as lease:
        assert lease.connection is conn
        # _PgStoreLeaseAdapter reads .connection then opens a cursor
        cur = lease.connection.cursor()
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)

    assert view.lease_acquisitions == 1
    assert conn.closed is False, "借来的连接由调用方关闭，lease() 不能替它关"


def test_self_check_working_when_prefetch_really_took_a_lease(monkeypatch):
    conn = FakeConn()
    store = FakeStore(conn)

    def fake_prefetch(query, limit=1, pg=None):
        # 模拟真实 keyword 路径：经 lease 取连接后查询，结果为空
        with pg.lease() as lease:
            cur = lease.connection.cursor()
            cur.execute("SELECT 1 WHERE false")
        return []

    monkeypatch.setattr("v3core.prefetch.prefetch", fake_prefetch)

    out = _prefetch_self_check(store, object(), pg_connected=True)

    assert out["working"] is True
    assert out["hit_count"] == 0
    assert out["leases_during_prefetch"] == 1
    assert "lease_probe" not in out, "真实路径取到过 lease 就不需要兜底探针"


def test_self_check_probes_lease_when_prefetch_took_none(monkeypatch):
    conn = FakeConn()
    store = FakeStore(conn)
    monkeypatch.setattr("v3core.prefetch.prefetch", lambda *a, **kw: [])

    out = _prefetch_self_check(store, object(), pg_connected=True)

    assert out["working"] is True
    assert out["leases_during_prefetch"] == 0
    assert out["lease_probe"]["ok"] is True


def test_self_check_fails_when_lease_seam_is_broken(monkeypatch):
    """旧版 _PrefetchPgView（没有 lease）必须被判成 working=False。"""

    class BrokenView:
        def __init__(self, conn) -> None:
            self._connection = conn

        def is_connected(self) -> bool:
            return True

        def _connect(self):
            return self._connection

    conn = FakeConn()
    store = FakeStore(conn)
    monkeypatch.setattr(health_mod, "_PrefetchPgView", BrokenView)
    monkeypatch.setattr("v3core.prefetch.prefetch", lambda *a, **kw: [])

    out = _prefetch_self_check(store, object(), pg_connected=True)

    assert out["working"] is False
    assert out["hit_count"] == 0
    assert out["lease_probe"]["ok"] is False
    assert "lease probe failed" in out["error"]
    assert "lease" in out["lease_probe"]["error"]


def test_self_check_reports_prefetch_exception(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("prefetch exploded")

    monkeypatch.setattr("v3core.prefetch.prefetch", boom)

    with pytest.raises(RuntimeError):
        # 内层异常不在这里吞；handle_v3_health 的外层 try 负责兜成 working=False
        _prefetch_self_check(FakeStore(FakeConn()), object(), pg_connected=True)
