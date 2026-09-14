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
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

PROD_PROFILE_DIRNAME = ".v3-core"
PROD_PROFILE_LEAF = "profiles/default"

# 真实 HOME 只在本模块加载时捕获一次（此刻 monkeypatch 还未生效）
_REAL_HOME = Path.home()


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
