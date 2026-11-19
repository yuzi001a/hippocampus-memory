"""自动导入管线的四条正确性契约（2026-11-10 下一阶段修复的回归）。

1. ``KIND_CURATED`` 不再被静默跳过 —— memory-md 的用户长期记忆必须进
   explicit_memories（此前 auto_import 只处理 KIND_RAW，导入报告里
   user_curated_imported 恒为 0）。
2. 自动派生的 event_id 必须按 **会话内序号** 生成，而不是按写入批次 ——
   批次 500 条时，会话第 501 条会重用 ``derived:…:1``，raw 被去重丢掉、
   其 QA 的 source_id 指向另一条消息。
3. raw 写入失败时不得继续写 QA —— 否则库里出现指向不存在消息的孤儿 QA。
4. 单个超大会话的缓冲必须有上限 —— 仅仅限制"同时打开几个会话"不构成
   内存有界。
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from v3core.importers import (
    KIND_CURATED,
    KIND_RAW,
    ImportItem,
    auto_import as A,
)
from v3core.importers.discovery import DiscoveredSource


# ── fakes ─────────────────────────────────────────────────────────────


class _Cur:
    def __init__(self, recorder):
        self._recorder = recorder
        self.statements: list[tuple[str, tuple]] = []
        self._rowcount = 1

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params or ()))
        self._recorder["statements"].append((self.statements[-1]))
        if self._recorder.get("fail_raw") and "conversation_stream" in sql:
            raise RuntimeError("simulated raw write failure")
        self._rowcount = 1

    @property
    def rowcount(self):
        return self._rowcount

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class _Conn:
    def __init__(self, rec):
        self._rec = rec

    def cursor(self):
        c = _Cur(self._rec)
        self._rec["cursors"].append(c)
        return c

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _Lease:
    def __init__(self, rec):
        self._conn = _Conn(rec)

    @property
    def connection(self):
        return self._conn

    def close(self):
        pass


class FakePool:
    def __init__(self, fail_raw: bool = False):
        self.rec = {"statements": [], "cursors": [], "fail_raw": fail_raw}

    def lease(self, timeout=None):
        return _Lease(self.rec)


class FakeImporter:
    name = "fake"
    excluded_counts: dict = {}

    def __init__(self, items):
        self._items = items

    def reset_excluded_counts(self):
        self.excluded_counts = {}

    def parse(self, artifact):
        yield from self._items


def _src(host: str = "fake") -> DiscoveredSource:
    return DiscoveredSource(
        host=host,
        root=Path("."),
        found=True,
        artifacts=[Path("fake.txt")],
        format="fake",
        sessions=1,
    )


def _raw(sess: str, role: str, text: str, ts: str, host: str = "fake") -> ImportItem:
    return ImportItem(
        kind=KIND_RAW,
        source_system=host,
        source_ref=sess,
        text=text,
        role=role,
        occurred_at=ts,
        host=host,
    )


@pytest.fixture()
def patched(monkeypatch):
    def _install(items, pool):
        monkeypatch.setattr(A, "get_importer", lambda name: FakeImporter(items))
        monkeypatch.setattr(A, "_current_pool", lambda: pool)
        return pool

    return _install


# ── 1. curated items ──────────────────────────────────────────────────


def test_curated_items_reach_the_memory_writer(monkeypatch):
    """memory-md 的条目不能因为不是 raw 就被丢掉。"""
    import v3core.importers as M

    written: list[dict] = []

    class _Writer:
        def create(self, **kw):
            written.append(kw)

            class R:
                status = "DURABLE_COMMITTED"
                memory_id = "m1"

            return R()

    monkeypatch.setattr(M, "_resolve_writer", lambda pool: _Writer())
    items = [
        _raw("s1", "user", "问一句", "2026-01-01T00:00:00Z"),
        _raw("s1", "assistant", "答一句", "2026-01-01T00:00:01Z"),
        ImportItem(
            kind=KIND_CURATED,
            source_system="memory-md",
            source_ref="MEMORY.md::0",
            text="用户偏好：报告要中文、可落地。",
            title="偏好",
            category="user_curated",
            tags=["memory"],
        ),
    ]
    monkeypatch.setattr(A, "get_importer", lambda name: FakeImporter(items))
    pool = FakePool()
    entry = A._process_source(_src("memory-md"), pool=pool, dry_run=False)
    assert entry["curated_found"] == 1
    assert entry["user_curated_imported"] == 1
    assert written and "报告要中文" in written[0]["content"]
    assert "user-curated" in written[0]["tags"]


def test_curated_items_are_counted_in_dry_run(monkeypatch):
    items = [
        ImportItem(
            kind=KIND_CURATED,
            source_system="memory-md",
            source_ref="USER.md::0",
            text="用户是体制内干部。",
            category="user_curated",
        )
    ]
    monkeypatch.setattr(A, "get_importer", lambda name: FakeImporter(items))
    entry = A._process_source(_src("memory-md"), pool=None, dry_run=True)
    assert entry["curated_found"] == 1


# ── 2. derived identity ordinals ─────────────────────────────────────


def test_derived_event_ids_are_unique_across_batches(monkeypatch):
    """一个会话超过 batch_size 条时，派生 event_id 不能重复。"""
    n = 120
    items = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        items.append(_raw("big", role, f"m{i}", f"2026-01-01T00:00:{i:02d}Z"))
    monkeypatch.setattr(A, "get_importer", lambda name: FakeImporter(items))
    pool = FakePool()
    A._process_source(_src(), pool=pool, dry_run=False, batch_size=10)

    # _RAW_DEDUPE_SQL_F2 参数序：… 8=host, 9=event_id, 12=event_id(去重谓词)
    raw_ids = [
        p[9]
        for sql, p in pool.rec["statements"]
        if "conversation_stream" in sql and len(p) > 9
    ]
    assert len(raw_ids) == n
    assert len(set(raw_ids)) == n, f"派生 event_id 重复：{n - len(set(raw_ids))} 条"

    qa_ids = [
        p[0]
        for sql, p in pool.rec["statements"]
        if "qa_pairs" in sql
    ]
    assert len(set(qa_ids)) == len(qa_ids)
    # 每条 QA 的 q_event_id 都必须真实存在于 conversation_stream 的写入集合里
    for sid in qa_ids:
        qid = sid.split("/")[-1]
        assert qid in set(raw_ids), f"QA 指向不存在的原始消息: {sid}"


# ── 3. raw failure ⇒ no QA ───────────────────────────────────────────


def test_raw_failure_withholds_qa(patched):
    items = [
        _raw("s1", "user", "问", "2026-01-01T00:00:00Z"),
        _raw("s1", "assistant", "答", "2026-01-01T00:00:01Z"),
    ]
    pool = FakePool(fail_raw=True)
    patched(items, pool)
    entry = A._process_source(_src(), pool=pool, dry_run=False)
    qa = [s for s, _ in pool.rec["statements"] if "qa_pairs" in s]
    assert qa == [], "raw 写失败后仍然写了 QA（孤儿）"
    assert entry["qa_pairs_derived"] == 0
    assert any("withheld" in e for e in entry["errors"])


# ── 4. bounded session buffering ─────────────────────────────────────


def test_huge_session_buffer_is_bounded(monkeypatch):
    """单会话 5,000 条也不能整体驻留内存。"""
    items = []
    for i in range(5000):
        role = "user" if i % 2 == 0 else "assistant"
        items.append(_raw("huge", role, "x" * 50, f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z"))

    # 监视 _flush_session 实际收到的批次大小：它们就是峰值驻留量
    sizes: list[int] = []
    import v3core.importers.qa_pairing as qp

    real_pair_items = qp.pair_items

    def _spy_pair(batch):
        sizes.append(len(batch))
        return real_pair_items(batch)

    monkeypatch.setattr(qp, "pair_items", _spy_pair)
    monkeypatch.setattr(A, "get_importer", lambda name: FakeImporter(items))
    pool = FakePool()
    A._process_source(_src(), pool=pool, dry_run=False)
    assert sizes, "没有产生任何 flush"
    assert max(sizes) <= 2001, f"单批缓冲 {max(sizes)} 条，超过上限"
    assert len(sizes) > 1, "大会话没有被分段"


def test_hard_cap_bounds_sessions_with_no_safe_boundary(monkeypatch):
    """整段都是 assistant 行的会话永远等不到安全边界 —— 也必须封顶。"""
    import v3core.importers.qa_pairing as qp

    items = [
        _raw("mono", "assistant", "x" * 40, f"2026-01-01T00:{(i // 60) % 60:02d}:{i % 60:02d}Z")
        for i in range(9000)
    ]
    sizes: list[int] = []
    real = qp.pair_items

    def _spy(batch):
        sizes.append(len(batch))
        return real(batch)

    monkeypatch.setattr(qp, "pair_items", _spy)
    monkeypatch.setattr(A, "get_importer", lambda name: FakeImporter(items))
    entry = A._process_source(_src(), pool=FakePool(), dry_run=False)
    assert max(sizes) <= 8001, f"单批 {max(sizes)} 条，硬上限没生效"
    assert any("hard cap" in e for e in entry["errors"])
