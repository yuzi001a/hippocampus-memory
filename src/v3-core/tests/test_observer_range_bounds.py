"""批次区间界必须取 min/max，不取列表首尾 (2026-11-21 回放实测)。

取批的排序维度是 ``timestamp``，游标与 ``source_qa_range`` 的维度是 ``id``。
增量库里两者同步；按历史回放 / 按时间序重导时 id 分配顺序 ≠ 事件顺序，
``qa_ids[0] > qa_ids[-1]``，写 int8range 时抛
``range lower bound must be less than or equal to range upper bound``。

现场表现：跑到某一批之后每轮都失败，游标停在原地不动，日志里持续刷同一条错误，
而 ``observation_notes`` 只覆盖前 80% —— 看起来像"跑完了"，实际后半段一条没写。
"""

from __future__ import annotations

import pytest

from v3core import observer as obs


def test_sorted_ids_return_first_and_last():
    assert obs._qa_range_bounds([3, 4, 5]) == (3, 5)


def test_unsorted_ids_still_return_min_and_max():
    # 时间序取批时 id 可能乱序：首尾相减会得到反向区间
    assert obs._qa_range_bounds([9, 2, 7, 1]) == (1, 9)


def test_single_row_batch_is_its_own_range():
    assert obs._qa_range_bounds([42]) == (42, 42)


def test_empty_batch_is_refused_loudly():
    with pytest.raises(ValueError):
        obs._qa_range_bounds([])


def test_worker_no_longer_uses_list_edges():
    """防止有人把 min/max 改回 qa_ids[0] / qa_ids[-1]（注释里提到不算）。"""
    import ast
    import inspect

    src = inspect.getsource(obs._observe_worker)
    assert "_qa_range_bounds(qa_ids)" in src

    tree = ast.parse(src.lstrip() if src.startswith(" ") else src)
    edges = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "qa_ids"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value in (0, -1)
    ]
    assert not edges, "区间界不能再用 qa_ids[0] / qa_ids[-1]"
