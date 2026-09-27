# -*- coding: utf-8 -*-
"""``resolve_embedding_failures_for_entity`` 的作用域回归测试。

不变量（本文件存在的唯一理由）：
    repair 成功意味着该实体的 canonical embedding 已恢复，那么挂在它名下的
    **每一个** unresolved phase 都不再成立。修复前 backfill 只关闭
    ``phase="backfill"``，而 live 写入失败记录的 phase 是 ``live_ingest`` /
    ``j_import`` 等 —— 向量修好了，历史 marker 还挂着，operator 账面上表现为
    **假 backlog**：看起来还有一堆没修的失败，实际数据已经修完。

    作用域必须严格限定在 ``(entity_table, entity_id)``：不误伤其它实体、其它表，
    也不重写已 resolved 的历史行。只 UPDATE 不 DELETE。

本文件是纯单元测试：用一个记录型 stub 连接捕获真实执行的 SQL 与参数，
不连 PostgreSQL（测试域 conftest 硬封禁 psycopg2.connect）、不联网、不写文件。
SQL 文本本身取自模块里真实发布的常量，因此断言钉住的是**实际会跑的那条 SQL**，
不是测试里另写的一条。
"""
from __future__ import annotations

from v3core.embed_failures import (
    _RESOLVE_ENTITY_SQL,
    _RESOLVE_SQL,
    resolve_embedding_failures_for_entity,
)


# ── 记录型 stub：捕获 SQL 与参数，可模拟失败 ───────────────────────────────────

class _RecordingCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        if self._conn.raise_exc is not None:
            raise self._conn.raise_exc
        self.rowcount = self._conn.rowcount

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingConn:
    def __init__(self, rowcount: int = 0, raise_exc: BaseException | None = None):
        self.executed: list[tuple[str, dict | None]] = []
        self.rowcount = rowcount
        self.raise_exc = raise_exc
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return _RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


# ── 1. SQL 语义：这是本修复的契约本体 ─────────────────────────────────────────

def test_sql_has_no_phase_predicate():
    """核心契约：谓词里**没有** phase —— 这正是"按实体关闭"的全部含义。

    只保留 entity_table + entity_id + resolved_at IS NULL。任何 phase 谓词的
    重新引入都会让 live_ingest / j_import 的 marker 再次变成假 backlog。
    """
    sql = _RESOLVE_ENTITY_SQL.lower()
    assert "phase" not in sql, "按实体关闭的 SQL 不允许出现 phase 谓词"
    assert "entity_table = %(entity_table)s" in sql
    assert "entity_id    = %(entity_id)s" in sql
    assert "resolved_at is null" in sql


def test_sql_updates_lifecycle_columns_and_never_deletes():
    """成功后统一写 resolved_at / resolution / updated_at；只 UPDATE 不 DELETE。"""
    sql = _RESOLVE_ENTITY_SQL
    assert sql.strip().upper().startswith("UPDATE PUBLIC.EMBEDDING_FAILURES")
    assert "resolved_at = now()" in sql
    assert "resolution  = %(resolution)s" in sql
    assert "updated_at  = now()" in sql
    upper = sql.upper()
    assert "DELETE" not in upper
    assert "INSERT" not in upper


def test_entity_scoped_sql_differs_from_phase_scoped_sql_only_by_phase():
    """与旧的按 phase 关闭相比，差异必须**只有** phase 这一条谓词。

    这条断言防止有人在"修 D5"时顺手把作用域改宽（比如去掉 entity_id）。
    """
    entity_lines = [l.strip() for l in _RESOLVE_ENTITY_SQL.strip().splitlines()]
    phase_lines = [l.strip() for l in _RESOLVE_SQL.strip().splitlines()]
    assert len(entity_lines) == len(phase_lines) - 1
    assert [l for l in phase_lines if "phase" not in l] == entity_lines


# ── 2. 返回值与连接语义 ───────────────────────────────────────────────────────

def test_returns_rowcount_and_commits():
    conn = _RecordingConn(rowcount=3)

    n = resolve_embedding_failures_for_entity(
        conn, entity_table="conversation_stream", entity_id=1)

    assert n == 3
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_zero_rows_resolved_returns_zero():
    conn = _RecordingConn(rowcount=0)

    assert resolve_embedding_failures_for_entity(
        conn, entity_table="topics", entity_id=9) == 0
    assert conn.commits == 1


def test_entity_id_is_stringified_in_params():
    """entity_id 一律转字符串：marker 写入端就是这么存的，比较必须同型。"""
    conn = _RecordingConn(rowcount=1)

    resolve_embedding_failures_for_entity(
        conn, entity_table="conversation_stream", entity_id=4242)

    _, params = conn.executed[0]
    assert params["entity_id"] == "4242"
    assert isinstance(params["entity_id"], str)


def test_default_resolution_is_repaired():
    conn = _RecordingConn(rowcount=1)

    resolve_embedding_failures_for_entity(conn, entity_table="topics", entity_id="t1")

    _, params = conn.executed[0]
    assert params["resolution"] == "repaired"


def test_cursor_failure_rolls_back_returns_zero_and_never_raises():
    """记账不得拖垮 repair：异常一律吞掉，rollback 后返回 0。"""
    conn = _RecordingConn(raise_exc=RuntimeError("connection reset"))

    n = resolve_embedding_failures_for_entity(
        conn, entity_table="conversation_stream", entity_id=1)

    assert n == 0
    assert conn.rollbacks == 1
    assert conn.commits == 0


def test_no_connection_returns_zero_without_raising():
    """无可用连接时安全返回 0（不抛），由调用方的其它信号暴露问题。"""
    assert resolve_embedding_failures_for_entity(
        None, entity_table="topics", entity_id=1) == 0


# ── 3. 五个必需的场景：对 SQL 谓词建模，证明作用域行为 ─────────────────────────
#
# 下面的模拟严格使用 SQL 里那条谓词（entity_table 相等 + entity_id 相等 +
# resolved_at IS NULL），因此它验证的是**已发布的谓词**所蕴含的行为。

def _apply_predicate(markers, *, entity_table, entity_id):
    """按 SQL 谓词挑选会被本次 UPDATE 命中的 marker。"""
    return [m for m in markers
            if m["entity_table"] == entity_table
            and m["entity_id"] == str(entity_id)
            and m["resolved_at"] is None]


def _markers():
    return [
        # 目标实体：三个不同 phase 全部 unresolved
        {"entity_table": "conversation_stream", "entity_id": "1", "phase": "live_ingest",
         "resolved_at": None},
        {"entity_table": "conversation_stream", "entity_id": "1", "phase": "j_import",
         "resolved_at": None},
        {"entity_table": "conversation_stream", "entity_id": "1", "phase": "backfill",
         "resolved_at": None},
        # 其它实体：绝不能被误关
        {"entity_table": "conversation_stream", "entity_id": "2", "phase": "live_ingest",
         "resolved_at": None},
        # 其它表：绝不能被误关
        {"entity_table": "topics", "entity_id": "1", "phase": "live_ingest",
         "resolved_at": None},
        # 已 resolved 的历史：保持原状，不重复
        {"entity_table": "conversation_stream", "entity_id": "1", "phase": "tokenizer",
         "resolved_at": "2020-01-01T00:00:00Z"},
    ]


def test_scenario_live_ingest_marker_is_closed_by_repair():
    hit = _apply_predicate(_markers(), entity_table="conversation_stream", entity_id=1)
    assert any(m["phase"] == "live_ingest" for m in hit)


def test_scenario_j_import_marker_is_closed_by_repair():
    hit = _apply_predicate(_markers(), entity_table="conversation_stream", entity_id=1)
    assert any(m["phase"] == "j_import" for m in hit)


def test_scenario_backfill_marker_is_closed_by_repair():
    hit = _apply_predicate(_markers(), entity_table="conversation_stream", entity_id=1)
    assert any(m["phase"] == "backfill" for m in hit)


def test_scenario_all_three_phases_close_together():
    """三个 phase 必须**一起**关闭 —— 这是假 backlog 消失的判据。"""
    hit = _apply_predicate(_markers(), entity_table="conversation_stream", entity_id=1)
    assert {m["phase"] for m in hit} == {"live_ingest", "j_import", "backfill"}
    assert len(hit) == 3


def test_scenario_other_entity_and_other_table_are_never_touched():
    hit = _apply_predicate(_markers(), entity_table="conversation_stream", entity_id=1)
    assert not any(m["entity_id"] == "2" for m in hit)
    assert not any(m["entity_table"] == "topics" for m in hit)


def test_scenario_already_resolved_row_is_untouched():
    hit = _apply_predicate(_markers(), entity_table="conversation_stream", entity_id=1)
    assert not any(m["phase"] == "tokenizer" for m in hit)
    # 历史行仍在集合里（只 UPDATE 不 DELETE）
    assert any(m["phase"] == "tokenizer" for m in _markers())
