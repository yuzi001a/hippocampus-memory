"""B02 scope completion — every tool helper must see the booted profile.

The dispatcher knows the booted ``V3Core``; nested helpers that receive no
``**kw`` used to resolve the DEFAULT profile on their own, so a non-default
profile silently read/wrote default-profile data. These tests pin the contract:
with a scope bound, the leaf resolution points must use it; without one, the
legacy default fallback must keep working.
"""
from __future__ import annotations

import json
import pathlib
import types
from unittest import mock

import pytest

from v3core.tools import handle_tool_call
from v3core.tools import organize as organize_mod


CAN = {"basePath": "C:/__scope_canary__"}          # non-default scope marker


class _Recorder:
    """Callable recorder that stands in for a dependency constructor."""

    def __init__(self, return_value=None):
        self.calls = []
        self.kwargs = []
        self._return = return_value

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        self.kwargs.append(kwargs)
        return self._return


# ── 1. topic_correct (delete path → 真实 sqlite 路径) ────────────────────────

def test_topic_correct_writes_to_scoped_data_root(tmp_path, monkeypatch):
    """机制层: 绑定 scope 后 _resolve_data_dir() 必须落在 scope 根。
    集成层: 经 handle_tool_call 的 v3_topic_correct 必须用同一个根。"""
    import sqlite3

    from v3core.config import _resolve_data_dir
    from v3core.tools import _scope

    base = tmp_path / "scope-prof"
    scoped_cfg = {"basePath": str(base)}

    with _scope.scoped(scoped_cfg):
        assert _resolve_data_dir() == base, "scope 未作用于 _resolve_data_dir()"

    seen = []

    def _connect(path, *a, **k):
        seen.append(path)
        raise RuntimeError("stop-after-path")     # 只验路径, 不建库

    monkeypatch.setattr(sqlite3, "connect", _connect)

    handle_tool_call(
        "v3_topic_correct",
        {"action": "delete", "topic_id": "t_nonexistent"},
        effective_config=scoped_cfg,
    )

    assert seen, "v3_topic_correct 未触达 sqlite（测试前提失效）"
    got = str(seen[0]).replace("\\", "/")
    assert got.startswith(str(base).replace("\\", "/")), f"未使用 booted scope 的数据根: {got}"


# ── 2. affinity (hardcoded resolve_config("default")) ───────────────────────

def test_affinity_uses_bound_scope_instead_of_default(monkeypatch):
    store = mock.MagicMock()
    deep_store = _Recorder(return_value=store)
    monkeypatch.setattr("v3core.card_store.DeepStore", deep_store)

    def _boom(*a, **k):  # 触发即证明仍在解析 default
        raise AssertionError("affinity 仍在 resolve_config() 解析 default profile")

    monkeypatch.setattr("v3core.config.resolve_config", _boom)

    handle_tool_call("v3_update", {"action": "affinity"}, effective_config=CAN)

    assert deep_store.calls, "affinity 没有构造 DeepStore（测试前提失效）"
    cfg = deep_store.calls[0][0]
    assert cfg is CAN, f"DeepStore 拿到的是 default 配置而不是 booted scope: {cfg!r}"


# ── 3. handbook fallback (HandbookManager() 无参构造) ───────────────────────

def test_handbook_lookup_uses_scoped_manager(monkeypatch):
    hb = _Recorder(return_value=mock.MagicMock(get=lambda k: None))
    monkeypatch.setattr("v3core.handbook.HandbookManager", hb)

    fake_core = types.SimpleNamespace(
        config=CAN,
        get_message_context=lambda sid: json.dumps({"success": False, "error": "nope"}),
    )
    handle_tool_call(
        "v3_get",
        {"target": "hm", "source_id": "no-such-key"},
        core=fake_core,
        effective_config=CAN,
    )

    assert hb.kwargs, "handbook 回退没有构造 HandbookManager（测试前提失效）"
    assert hb.kwargs[-1].get("config") is CAN, (
        f"HandbookManager 未收到 booted scope: {hb.kwargs[-1]!r}"
    )


# ── 4. organize (_resolve_paths helper, no kw) ──────────────────────────────

def test_organize_paths_follow_bound_scope(monkeypatch):
    from v3core.tools import _scope

    def _boom(*a, **k):
        raise AssertionError("organize 仍在 resolve_config() 解析 default profile")

    monkeypatch.setattr("v3core.config.resolve_config", _boom)

    with _scope.scoped(CAN):
        paths = organize_mod._resolve_paths()

    assert str(paths["y_dir"]).replace("\\", "/").startswith("C:/__scope_canary__"), paths
    assert str(paths["shou_zhang"]).replace("\\", "/").startswith("C:/__scope_canary__"), paths


# ── 5/6. safety: no leakage between calls; legacy fallback intact ───────────

def test_scope_does_not_leak_after_the_call():
    from v3core.tools import _scope

    assert _scope.current_scope() is None
    with _scope.scoped(CAN):
        assert _scope.current_scope() is CAN
    assert _scope.current_scope() is None, "scope 在调用结束后泄漏到后续调用"


def test_legacy_call_without_scope_still_resolves_default(monkeypatch):
    """直接调用（无 kwargs）时必须保留旧的 default 解析路径。"""
    calls = []

    def _default_cfg(*a, **k):
        calls.append(a)
        return {"basePath": str(pathlib.Path.home() / ".v3-core" / "profiles" / "default")}

    monkeypatch.setattr("v3core.config.resolve_config", _default_cfg)

    paths = organize_mod._resolve_paths()

    assert calls, "无 scope 时应回退 resolve_config()"
    assert "v3-core" in str(paths["y_dir"]).replace("\\", "/")
