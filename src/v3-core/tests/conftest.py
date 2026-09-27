# -*- coding: utf-8 -*-
"""v3-core 测试域全局隔离沙箱（P0-A，2026-08-25）。

B0 背景（docs/roadmap-v5-stabilization-20260825.md §4）：cold_start 等测试在
未 patch _save_cursor 时，observer._state_path(None) 会兜底解析到真实
~/.v3-core/profiles/default/observer_state.json —— 全量 pytest 全绿的同时把生产
游标写成假值（实测被写成 last_qa_id=5）。

本 conftest 的四层防线（只动测试域，不碰 src/v3core 生产源码）：

1. fake HOME（autouse, session 级）—— Path.home() 重定向到 pytest tmp 沙箱，
   所有 ``~/.v3-core/...`` 兜底解析天然落到临时目录。
2. fail-closed _state_path —— 无显式 cfg 时禁止解析回真实 HOME，改落
   session 沙箱目录（并打 warning 日志留痕）。显式传了 state_path 的测试
   （replay/backlog 契约）不受影响。
3. 生产 profile 守卫（autouse）—— 每个 test 前后对
   ~/.v3-core/profiles/default/{observer_state.json, config.yaml} 做
   sha256+mtime_ns 快照比对，任何变化立即 fail 该测试（fail closed，
   不静默吞掉污染）。
4. PG 硬封禁 —— psycopg2.connect 在测试进程内一律 raise AssertionError；
   测试域不允许连接任何 PostgreSQL（含 localhost:5433 生产库）。

守卫证据链见 tests/test_p0a_test_isolation_guard.py（RED→GREEN）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import warnings
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

PROD_PROFILE_DIRNAME = ".v3-core"
PROD_PROFILE_LEAF = "profiles/default"

# 真实 HOME 只在本模块加载时捕获一次（此刻 monkeypatch 还未生效）
_REAL_HOME = Path.home()

# ── 文件系统隔离守卫（tests/_harness_guard.py）──────────────────────────────
# 在 conftest 加载期（session 级 fake HOME 生效之前）import，保证 _harness_guard
# 在模块加载时捕获的 real_home() 是真实 HOME。
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness_guard import (  # noqa: E402
    ALLOW_LIVE_OUTBOX_ENV_VAR,
    guarded_outbox_paths,
    is_inside_production,
    tree_fingerprint,
    tree_names,
    tree_recent,
)


def _prod_profile_dir() -> Path:
    return _REAL_HOME / PROD_PROFILE_DIRNAME / PROD_PROFILE_LEAF


def _fingerprint(path: Path):
    """(exists, size, sha256_hex, mtime_ns) — mtime ns 粒度防同秒写入漏检。"""
    if not path.exists():
        return (False, 0, "", 0)
    st = path.stat()
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return (True, -1, "unreadable", st.st_mtime_ns)
    return (True, st.st_size, digest, st.st_mtime_ns)


# ────────────────────────────────────────────────────────────
# 防线 1：fake HOME（session 级，先于一切测试 import 生效）
# ────────────────────────────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def _sandbox_home(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("v3-test-sandbox-home")
    fake_home = str(sandbox)
    real_expanduser = Path.expanduser

    def _fake_expanduser(self):
        # 仅重定向以 ~ 开头的路径；普通相对路径原样返回
        s = str(self)
        if s == "~" or s.startswith("~" + ("/")):
            rest = s[1:].lstrip("/\\")
            return Path(fake_home) / rest if rest else Path(fake_home)
        if s.startswith("~"):
            # ~user 形式：一并落到沙箱（测试域不存在其他用户目录）
            return sandbox / s[1:]
        return real_expanduser(self)

    real_home_fn = Path.home

    def _fake_home():
        return Path(fake_home)

    Path.home = staticmethod(_fake_home)  # type: ignore[method-assign]
    Path.expanduser = _fake_expanduser  # type: ignore[method-assign]
    try:
        yield sandbox
    finally:
        Path.home = real_home_fn  # type: ignore[method-assign]
        Path.expanduser = real_expanduser  # type: ignore[method-assign]


# ────────────────────────────────────────────────────────────
# 防线 2：_state_path fail-closed 兜底（函数级 patch，随测恢复）
# ────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _state_path_fail_closed(monkeypatch, tmp_path, request):
    """无显式 state_path 时，observer._state_path 必须落沙箱而非真实 HOME。

    显式传 cfg.state_path 的测试不受任何影响（契约测试依赖此能力）。
    """
    try:
        obs_mod = pytest.importorskip("v3core.observer")
    except Exception:  # pragma: no cover - observer 缺依赖时跳过该防线
        yield
        return

    real_state_path = obs_mod._state_path
    fallback_root = tmp_path / "state-sandbox"

    def guarded_state_path(cfg=None):
        explicit = None
        if isinstance(cfg, dict):
            explicit = cfg.get("state_path")
        elif cfg is not None:
            try:
                explicit = getattr(cfg, "state_path", None)
            except Exception:
                explicit = None
        path = real_state_path(cfg)
        resolved_under_real_home = False
        try:
            resolved_under_real_home = _REAL_HOME in path.parents or (
                path.parent == _REAL_HOME / PROD_PROFILE_DIRNAME / PROD_PROFILE_LEAF
            )
        except OSError:
            resolved_under_real_home = False
        if resolved_under_real_home and not explicit:
            logger.warning(
                "[P0-A] _state_path 兜底解析到真实生产 profile (%s) → "
                "fail-closed 重定向到 %s", path, fallback_root,
            )
            redirected = fallback_root / path.name
            redirected.parent.mkdir(parents=True, exist_ok=True)
            return redirected
        return path

    monkeypatch.setattr(obs_mod, "_state_path", guarded_state_path)

    # cold_start 在 C6 分支做函数内局部导入 `from .observer import _save_cursor`，
    # 而 _save_cursor 内部再调模块级 _state_path → 上面的 patch 覆盖该路径。
    yield


# ────────────────────────────────────────────────────────────
# 防线 3：生产 profile 文件守卫（每测试前后快照比对）
# ────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _guard_prod_profile():
    targets = [
        _prod_profile_dir() / "observer_state.json",
        _prod_profile_dir() / "config.yaml",
    ]
    before = {t.name: _fingerprint(t) for t in targets}
    yield
    for t in targets:
        after = _fingerprint(t)
        assert after == before[t.name], (
            f"P0-A 守卫：本测试修改了生产 profile 文件 {t} — "
            f"before={before[t.name]} after={after}"
        )


# ────────────────────────────────────────────────────────────
# 防线 3b：生产 durable outbox 树守卫（每测试前后 tree_fingerprint 比对）
#
# 为什么需要这一层：上面的 _guard_prod_profile 只盯两个**文件**
# （observer_state.json / config.yaml），因此**看不到** durable outbox 的写入。
# 真实事故正是从这条缝里漏出去的：harness 用 fake pg + plain dict 构造
# LiveBuffer，_persist_live_item 把 marker 写进真实生产
# ~/.v3-core/profiles/default/j/pending_live_buffer（9 个文件），
# 两个被盯的文件毫无变化 → 全绿。本 fixture 把整棵 outbox 树（含 j/ 一层的
# 条目名集合）纳入快照，任何新增/删除/触碰都会 fail 该测试。
# ────────────────────────────────────────────────────────────
def _j_entry_names() -> tuple:
    """<prod>/j 一层的条目名集合 — 捕获新建 journal/live-buffer 目录。"""
    try:
        j = _prod_profile_dir() / "j"
        if not j.is_dir():
            return ()
        return tuple(sorted(p.name for p in j.iterdir()))
    except OSError:
        return ()


@pytest.fixture(autouse=True)
def _guard_prod_outbox_tree():
    targets = guarded_outbox_paths()          # pending/accepted/_lost + j 本身
    for t in targets:
        # fail closed：若守卫盯错了根（例如 real_home() 被误捕成 pytest 沙箱），
        # 这层防线就是空的 —— 宁可让每个测试都炸，也不能静默放行。
        assert is_inside_production(t), (
            f"P0-A 守卫配置错误：{t} 不在真实生产根内 — 守卫会形同虚设"
        )
    before = {str(t): tree_fingerprint(t) for t in targets}
    before_names = {str(t): tree_names(t) for t in targets}
    before_j_entries = _j_entry_names()
    for path, fp in before.items():
        assert fp[0] >= 0, f"P0-A 守卫：无法对生产 outbox 取指纹 {path} — {fp}"
    yield
    diffs = []
    for path, fp in before.items():
        after = tree_fingerprint(Path(path))
        if after == fp:
            continue
        after_names = tree_names(Path(path))
        added = sorted(set(after_names) - set(before_names[path]))
        removed = sorted(set(before_names[path]) - set(after_names))
        detail = (f"added={added[:8]} removed={removed[:8]}" if (added or removed)
                  else "no name change — content/mtime touched")
        written = tree_recent(Path(path), fp[2])
        diffs.append(
            f"  - {path}: {detail}\n"
            f"      before={fp}\n      after ={after}\n"
            f"      written during this test (mtime newer than before-snapshot): "
            f"{written if written else 'none found'}"
        )
    after_j_entries = _j_entry_names()
    if after_j_entries != before_j_entries:
        diffs.append(
            f"  - {_prod_profile_dir() / 'j'} (one level): "
            f"added={sorted(set(after_j_entries) - set(before_j_entries))[:8]} "
            f"removed={sorted(set(before_j_entries) - set(after_j_entries))[:8]}"
        )
    if not diffs:
        return
    report = (
        "P0-A 守卫：本测试期间真实生产 durable outbox 发生了变化：\n"
        + "\n".join(diffs)
        + "\n  fake DB 不等于 fake filesystem —— 测试要碰 LiveBuffer/outbox 时请用 "
          "tests/_harness_guard.py 的 isolate()。\n"
        + f"  （若本机真实 gateway 正在运行，它自己也会写 j/journal_<date>/ 与 live-buffer "
          f"marker；这种环境下的比对无法区分两者，可显式设 {ALLOW_LIVE_OUTBOX_ENV_VAR}=1 "
          f"把本守卫降级为 warning —— 默认关闭。）"
    )
    if os.environ.get(ALLOW_LIVE_OUTBOX_ENV_VAR) == "1":
        logger.warning("[P0-A] 生产 outbox 变化（已按 %s=1 降级）:\n%s",
                       ALLOW_LIVE_OUTBOX_ENV_VAR, report)
        warnings.warn(report, stacklevel=1)
        return
    assert False, report


# ────────────────────────────────────────────────────────────
# 防线 4：psycopg2.connect 测试域硬封禁
# ────────────────────────────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def _block_pg_connect():
    try:
        import psycopg2
    except ImportError:
        yield
        return

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "P0-A：测试域禁止 psycopg2.connect 连接任何 PostgreSQL "
            f"(含 localhost:5433 生产库)。调用参数={args!r} {kwargs!r}"
        )

    real_connect = psycopg2.connect
    psycopg2.connect = _blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        psycopg2.connect = real_connect  # type: ignore[assignment]


# ────────────────────────────────────────────────────────────
# 附加工具（distribution-packaging tests, 2026-09-14 port; corrected 2026-09-14）
#   P0-A 四层隔离沙箱完整保留不动；仅追加一段：让焦点 packaging 测试
#   能从 source checkout 直接 import 同仓的 ``v3core`` 与 ``v3hermes`` 包
#   （无需 ``pip install``，也无需外部 PYTHONPATH）。这是 public PR #2
#   conftest 的 sibling-path 逻辑；与 P0-A 守卫无任何冲突（守卫走
#   monkeypatch/文件快照，下面只动 ``sys.path``）。
#
#   ``__file__`` 在 ``src/v3-core/tests/``，因此 ``_THIS_DIR`` 是
#   ``src/v3-core/tests``；再上一级才是 `src/`，再拼出 ``v3-core/src`` 与
#   ``v3-hermes-plugin/src``。这与前一次提交里 ``_THIS_DIR.parent`` 的版本
#   不同——后者指向 ``src/v3-core``，会让 ``v3-hermes-plugin/src`` 解析到
#   一个不存在的 ``src/v3-core/v3-hermes-plugin/src`` 路径（同时漏掉
#   ``v3-core/src``，导致 ``v3core`` import 失败）。
# ────────────────────────────────────────────────────────────
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_THIS_DIR = _Path(__file__).resolve().parent          # .../src/v3-core/tests
_SRC_DIR = _THIS_DIR.parent.parent                     # .../src  (one level above v3-core)
_V3HERMES_SRC = _SRC_DIR / "v3-hermes-plugin" / "src"
_V3CORE_SRC = _SRC_DIR / "v3-core" / "src"

for _p in (str(_V3CORE_SRC), str(_V3HERMES_SRC)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
