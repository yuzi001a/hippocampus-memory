# -*- coding: utf-8 -*-
"""回归测试：测试/探针域的文件系统隔离守卫（fail-closed）。

背景（真实事故）：一个 harness 用 ``LiveBuffer(pg=FakePG(), config=<plain dict>)``
跑关键路径测量。假 pg 隔离了**数据库**，但没有隔离**文件系统**：
``LiveBuffer._persist_live_item`` 的持久化 outbox 落在
``_resolve_data_dir(self._pg_config)``，而 plain dict 无 basePath → 兜底到真实生产
profile ``~/.v3-core/profiles/default``，把 9 个 marker 文件写进了线上 outbox；
下一次 gateway 重启 ``_recover_live_pending()`` 会把它们当真实行补插进生产库。

    *** 假数据库不等于假文件系统。 ***

本文件把这条教训钉成可执行的回归：默认路径危险（case 3）、只 patch
``v3core.config`` 不足以覆盖模块级别名（case 5）、假 pg 的 LiveBuffer 确实会把
outbox 指到生产（case 6），而 ``isolate()`` 能把这条路径关掉且零生产写入（case 7）。
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

# _harness_guard 与 tests/ 同目录：pytest 的 prepend import 模式通常已把该目录放进
# sys.path，这里显式补一次，保证任何 import 模式下都能 import。
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _harness_guard import (  # noqa: E402
    ALLOW_REAL_ENV_VAR,
    REPRESENTATIVE_CONFIG,
    HarnessIsolationError,
    assert_isolated,
    guarded_outbox_paths,
    is_inside_production,
    isolate,
    production_outbox_dir,
    production_profile_dir,
    real_home,
    tree_fingerprint,
    tree_names,
)

#: 触发事故的 config 形状：plain dict、无 basePath。与 eval/embedfix_critical_path.py 一致。
PLAIN_CFG = {"storage": {"embed": {"endpoint": "http://embed.test/v1",
                                  "model": "BAAI/bge-m3"}}}
VEC = [0.125] * 1024


class _Resp:
    """canned 成功响应，形状与 eval/embedfix_critical_path.py 的 stub 一致。"""

    status_code = 200
    text = ""
    headers = {"content-type": "application/json"}

    def json(self):
        return {"data": [{"index": 0, "embedding": VEC}]}

    def raise_for_status(self):
        pass


class FakePG:
    """只记录插入、永不建连的假 store（事故现场就是它 + plain dict）。"""

    def __init__(self):
        self.inserted: list = []
        self.lock = threading.Lock()

    def insert_message(self, source_id, content, embedding=None, metadata=None):
        with self.lock:
            self.inserted.append({"source_id": source_id, "content": content,
                                  "has_vector": embedding is not None})

    def open_side_connection(self):
        return None

    def __getattr__(self, name):
        return lambda *a, **kw: None


def _real_home_redirect(monkeypatch):
    """把 Path.home 指回真实 HOME（conftest 的 fake HOME 是 session 级 autouse 的）。

    这是复现事故的必要条件：默认路径只有在 Path.home() 是真实 HOME 时才会落到生产。
    monkeypatch 在测试结束后恢复 conftest 的 fake HOME。
    """
    monkeypatch.setattr(Path, "home", staticmethod(real_home))


# ────────────────────────────────────────────────────────────────────────────
# 1-2：守卫本身的方向性（拒绝生产、放行临时目录）
# ────────────────────────────────────────────────────────────────────────────
def test_guard_rejects_production_root():
    """钉住：生产 profile 根永远被 assert_isolated 拒绝（fail closed）。"""
    with pytest.raises(HarnessIsolationError) as ei:
        assert_isolated(real_home() / ".v3-core" / "profiles" / "default",
                        what="prod profile root")
    msg = str(ei.value)
    assert "production" in msg.lower()
    assert "isolate()" in msg  # 报错信息必须给出补救手段
    assert "prod profile root" in msg  # 必须点名 what


def test_guard_allows_temp_root(tmp_path):
    """钉住：临时沙箱目录被放行，且返回 resolve() 后的 Path。"""
    out = assert_isolated(tmp_path, what="tmp sandbox")
    assert out == tmp_path.resolve()
    assert not is_inside_production(tmp_path)
    # 生产根下的任意后代也必须被拒（含尚不存在的路径）
    with pytest.raises(HarnessIsolationError):
        assert_isolated(production_outbox_dir() / "does" / "not" / "exist.json")


# ────────────────────────────────────────────────────────────────────────────
# 3：省略 data path 时默认值就是生产目录（这是事故的根因）
# ────────────────────────────────────────────────────────────────────────────
def test_omitted_data_path_resolves_to_production(monkeypatch):
    """钉住：无 basePath 时 _resolve_data_dir(None/{}) 兜底到生产 profile —— 默认危险。"""
    import v3core.config as config_mod

    _real_home_redirect(monkeypatch)
    expected = (real_home() / ".v3-core" / "profiles" / "default").resolve()

    for cfg in (None, {}):
        resolved = Path(config_mod._resolve_data_dir(cfg)).resolve()
        assert resolved == expected, f"cfg={cfg!r} resolved to {resolved}"
        with pytest.raises(HarnessIsolationError):
            assert_isolated(resolved, what=f"_resolve_data_dir({cfg!r})")


# ────────────────────────────────────────────────────────────────────────────
# 4：环境变量把进程指向生产位置时必须被抓住
# ────────────────────────────────────────────────────────────────────────────
def test_inherited_environment_profile_is_caught(monkeypatch):
    """钉住：从环境继承来的生产位置（HOME/USERPROFILE/V3CORE_HOME）也落在守卫范围内。"""
    import v3core.config as config_mod

    home = real_home()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # V3CORE_HOME 是 v3core.config._find_env 真正读取的环境变量
    monkeypatch.setenv("V3CORE_HOME", str(home))
    _real_home_redirect(monkeypatch)

    resolved = Path(config_mod._resolve_data_dir(None)).resolve()
    assert is_inside_production(resolved), f"env-implied prod path not detected: {resolved}"
    with pytest.raises(HarnessIsolationError):
        assert_isolated(resolved, what="env-implied data dir")
    # env 派生的根也在生产根集合里（断言集合而非单点）
    from _harness_guard import production_roots
    assert any(str(home) == str(r) for r in production_roots()), production_roots()


# ────────────────────────────────────────────────────────────────────────────
# 5：CRITICAL —— 模块级别名（kind (a)）必须被覆盖
# ────────────────────────────────────────────────────────────────────────────
def test_module_level_alias_is_patched(tmp_path, monkeypatch):
    """钉住：只 patch v3core.config 覆盖不到 ingest 的模块级别名；isolate() 两者都覆盖。

    实测事实（本测试内断言）：模块级 ``from .config import _resolve_data_dir``
    绑定的是**同一个函数对象**（未 patch 前 id 相同）；但绑定发生在 import 时，
    因此一旦只替换 ``v3core.config`` 的属性，``v3core.ingest`` 命名空间里的名字
    仍指向旧对象 —— 逃逸窗口就此出现，marker 直达生产。
    """
    import v3core.config as config_mod
    import v3core.ingest as ingest_mod

    _real_home_redirect(monkeypatch)  # 让未 patch 的解析器真的落到生产
    cfg = dict(REPRESENTATIVE_CONFIG)
    original_ingest = ingest_mod._resolve_data_dir
    original_config = config_mod._resolve_data_dir

    # kind (a) 的绑定事实：同一个函数对象，但是两个独立的绑定位置
    assert ingest_mod._resolve_data_dir is config_mod._resolve_data_dir, (
        "expected the module-level `from .config import` alias to bind the same "
        "function object at import time"
    )
    assert "ingest" in ingest_mod.__name__

    # —— 只 patch v3core.config（kind (b) 能覆盖，kind (a) 覆盖不到）——
    sandbox_only = tmp_path / "config-only-patch"
    sandbox_only.mkdir()
    config_mod._resolve_data_dir = lambda _cfg=None: sandbox_only
    try:
        assert ingest_mod._resolve_data_dir is not config_mod._resolve_data_dir
        leaked = Path(ingest_mod._resolve_data_dir(cfg)).resolve()
        assert not str(leaked).startswith(str(sandbox_only)), (
            "sanity: the config-only patch must be invisible to the ingest alias"
        )
        with pytest.raises(HarnessIsolationError):
            assert_isolated(leaked, what="ingest module-level alias (config-only patch)")
    finally:
        config_mod._resolve_data_dir = original_config

    # —— isolate() 必须同时覆盖两种 import 形态 ——
    with isolate("alias-regression") as sandbox:
        sandbox_resolved = Path(sandbox).resolve()
        # kind (a): 模块级别名（LiveBuffer._live_pending_dir 走这条）
        via_alias = Path(ingest_mod._resolve_data_dir(cfg)).resolve()
        # kind (b): 函数内局部 import（enqueue 的 _lost 兜底、observer/prefetch 走这条）
        via_local = Path(config_mod._resolve_data_dir(cfg)).resolve()
        # 无参默认形态（最危险的那条）
        via_default = Path(config_mod._resolve_data_dir()).resolve()

        for label, got in (("ingest alias", via_alias),
                           ("config (function-local import)", via_local),
                           ("config no-arg default", via_default)):
            assert sandbox_resolved == got or sandbox_resolved in got.parents, (
                f"{label} -> {got} is not inside sandbox {sandbox_resolved}"
            )
            assert not is_inside_production(got), f"{label} leaked to production: {got}"
            assert_isolated(got, what=label)
        assert via_alias == via_local == via_default

    # 退出后两个绑定都必须还原成原对象
    assert ingest_mod._resolve_data_dir is original_ingest
    assert config_mod._resolve_data_dir is original_config


# ────────────────────────────────────────────────────────────────────────────
# 6：HEADLINE —— 假 DB 不等于假文件系统（事故可复现）
# ────────────────────────────────────────────────────────────────────────────
def test_fake_db_does_not_imply_fake_filesystem(monkeypatch):
    """钉住：fake pg + plain dict 的 LiveBuffer 确实指向生产 outbox；isolate() 关掉它。

    ``_recover_live_pending`` 被替换成 no-op：本测试只验证**路径解析**的危险性，
    绝不允许 ``__init__`` 的恢复流程去读/删真实生产的 pending marker。
    """
    from v3core.ingest import LiveBuffer
    from v3core.config import _resolve_data_dir

    pending_prod = production_outbox_dir() / "pending_live_buffer"
    before = tree_fingerprint(pending_prod)
    assert before[0] >= 0, f"cannot fingerprint production outbox: {before}"

    _real_home_redirect(monkeypatch)
    monkeypatch.setattr(LiveBuffer, "_recover_live_pending", lambda self: None)

    # —— 事故现场（无 isolate）：outbox 根就在生产里 ——
    lb_unsafe = LiveBuffer(pg=FakePG(), config=PLAIN_CFG)
    try:
        outbox = Path(lb_unsafe._live_pending_dir()).resolve()
        # 与生产代码同样的计算方式：_resolve_data_dir(self._pg_config) / "j" / ...
        expected = (Path(_resolve_data_dir(lb_unsafe._pg_config))
                    / "j" / "pending_live_buffer").resolve()
        assert outbox == expected
        assert is_inside_production(outbox), f"hazard not reproducible: {outbox}"
        assert production_profile_dir().resolve() in outbox.parents
        with pytest.raises(HarnessIsolationError):
            assert_isolated(outbox, what="LiveBuffer durable outbox (fake pg + plain dict)")
    finally:
        lb_unsafe._stop.set()

    # —— 同一构造，套上 isolate()：outbox 根落到沙箱 ——
    with isolate("fake-db-fake-fs") as sandbox:
        lb_safe = LiveBuffer(pg=FakePG(), config=PLAIN_CFG)
        try:
            safe_outbox = Path(lb_safe._live_pending_dir()).resolve()
            assert not is_inside_production(safe_outbox), safe_outbox
            assert Path(sandbox).resolve() in safe_outbox.parents
            assert_isolated(safe_outbox, what="LiveBuffer durable outbox (isolated)")
        finally:
            lb_safe._stop.set()

    # 两次构造都没留下任何东西
    assert tree_fingerprint(pending_prod) == before


# ────────────────────────────────────────────────────────────────────────────
# 7：END-TO-END —— 隔离后的真实 LiveBuffer 写不进生产 outbox
# ────────────────────────────────────────────────────────────────────────────
def test_isolated_livebuffer_writes_no_production_marker(monkeypatch):
    """钉住：真实 LiveBuffer 走完 enqueue→writer→ack 全链路后，生产 outbox 指纹不变。"""
    import requests

    from v3core.ingest import LiveBuffer

    pending_prod = production_outbox_dir() / "pending_live_buffer"
    accepted_prod = production_outbox_dir() / "accepted_live_buffer"
    before_pending = tree_fingerprint(pending_prod)
    before_accepted = tree_fingerprint(accepted_prod)
    assert before_pending[0] >= 0 and before_accepted[0] >= 0

    _real_home_redirect(monkeypatch)
    original_post = requests.post
    requests.post = lambda *a, **kw: _Resp()
    try:
        with isolate("livebuffer-e2e") as sandbox:
            pg = FakePG()
            lb = LiveBuffer(pg=pg, config=PLAIN_CFG)
            lb._batch = 1
            lb._flush_sec = 30.0
            accepted_sandbox = Path(sandbox) / "data" / "j" / "accepted_live_buffer"
            try:
                assert lb.enqueue("sess-guard", "msg-guard", "hello guard",
                                  "assistant", "t-guard") is True
                deadline = time.time() + 10
                while not pg.inserted and time.time() < deadline:
                    time.sleep(0.05)
                assert pg.inserted, "writer thread never flushed the enqueued item"
                # ack tombstone 必须落在沙箱里 —— 证明写入路径真的跑过，
                # 否则“生产没变”这个结论是空的（vacuous）。
                deadline = time.time() + 5
                while time.time() < deadline and not any(accepted_sandbox.glob("*.json")):
                    time.sleep(0.05)
                assert any(accepted_sandbox.glob("*.json")), (
                    "the ack/tombstone write path never ran in the sandbox — the "
                    "production-unchanged assertion below would be vacuous"
                )
            finally:
                lb._stop.set()
    finally:
        requests.post = original_post

    assert tree_fingerprint(pending_prod) == before_pending
    assert tree_fingerprint(accepted_prod) == before_accepted


# ────────────────────────────────────────────────────────────────────────────
# 8：tree_fingerprint 本身可检测增删（只在 tmp_path 上验证，绝不碰真实树）
# ────────────────────────────────────────────────────────────────────────────
def test_tree_fingerprint_detects_add_and_remove(tmp_path):
    """钉住：tree_fingerprint 对新增/删除敏感、对不变稳定，且对缺失目录稳定不抛。"""
    root = tmp_path / "tree"
    assert tree_fingerprint(root)[0] == 0  # 目录尚不存在也不抛
    root.mkdir()
    base = tree_fingerprint(root)

    (root / "a.json").write_text("{}", encoding="utf-8")
    added = tree_fingerprint(root)
    assert added != base
    assert added[0] == base[0] + 1
    assert "a.json" in tree_names(root)  # 指纹不匹配时用来定位具体路径

    sub = root / "nested"
    sub.mkdir()
    (sub / "b.json").write_text("{}", encoding="utf-8")
    nested = tree_fingerprint(root)
    assert nested != added
    assert nested[0] == added[0] + 2

    assert tree_fingerprint(root) == nested  # 无变化 → 稳定

    (root / "a.json").unlink()
    removed = tree_fingerprint(root)
    assert removed != nested
    assert removed[0] == nested[0] - 1

    # 缺失目录的指纹稳定且不抛
    missing = tmp_path / "nope"
    assert tree_fingerprint(missing) == tree_fingerprint(missing)
    assert tree_fingerprint(missing)[0] == 0
    # conftest 守卫用的 helper：四个被守护的生产路径必须都在生产里
    guarded = guarded_outbox_paths()
    assert len(guarded) == 4 and all(is_inside_production(p) for p in guarded)


# ────────────────────────────────────────────────────────────────────────────
# 9：escape hatch 默认关闭（危险开关不能被误开）
# ────────────────────────────────────────────────────────────────────────────
def test_escape_hatch_is_off_by_default(monkeypatch):
    """钉住：V3_HARNESS_ALLOW_REAL_DATA_DIR 默认未设置 → isolate() 给沙箱而非生产。"""
    import os

    monkeypatch.delenv(ALLOW_REAL_ENV_VAR, raising=False)
    assert os.environ.get(ALLOW_REAL_ENV_VAR) != "1"
    with isolate("hatch-default") as sandbox:
        assert not is_inside_production(sandbox)
    # 显式打开时才交出真实生产路径（危险，仅授权维护脚本）
    monkeypatch.setenv(ALLOW_REAL_ENV_VAR, "1")
    with isolate("hatch-on") as real:
        assert is_inside_production(real)


# ────────────────────────────────────────────────────────────────────────────
# 10：性能回归 —— tree_fingerprint 必须便宜到能每测试调用
#
# 为什么这条必须存在：conftest 的 ``_guard_prod_outbox_tree`` 对 4 条生产路径
# 每测试前后各取一次 ``tree_fingerprint``（before 阶段还各取一次 ``tree_names``），
# 而线上 outbox 现在有 ~9.9k 文件 —— 即每个测试要走 ~8 遍整棵树。旧实现
# （每个 entry 构造 ``Path`` + ``relative_to().as_posix()``，再把 10k 个名字
# ``"\n".join`` 成一个大字符串一次性 sha256）在本机实测：
#
#     10,000 文件的 tmp 树                  : 864 ms（同机另一次测量 1528 ms）
#     生产 accepted_live_buffer（9,156 文件）: 857 ms（另一次 1079 ms）
#     生产 <prod>/j 的 tree_names（9,890）   : 1140-1197 ms
#
# 折算下来 ~6.6 s/测试 → 619 个测试约 68 分钟，守卫事实上不可用。新实现
# （一次 scandir 遍历 + DirEntry 缓存 stat + 增量滚动 sha256）在同样的树上只要
# ~60 ms；本机裸 ``os.scandir`` 遍历 9.2k 个条目的 OS 下限就已经是 24 ms，
# 所以 60 ms 已接近物理下限（"几毫秒"对 10k 条目的目录在 Windows 上做不到）。
#
# 只算 conftest 守卫本身（8 次 fingerprint + 4 次 tree_names，同一棵生产树、
# 新旧实现交替测量）：旧 4916 ms/测试 → 新 267 ms/测试（18.4x）。
# accepted_live_buffer（9223 文件）单次 fingerprint：旧 771.6 ms → 新 41.4 ms。
#
# 上界取 0.5 s：新实现有 ~15x 余量，旧实现（min-of-3 实测 826-853 ms）必然失败。
# ────────────────────────────────────────────────────────────────────────────
_FP_PERF_BOUND_SEC = 0.5
_FP_PERF_FILES = 10_000
_FP_PERF_DIRS = 10


@pytest.fixture(scope="session")
def _large_tree(tmp_path_factory):
    """10,000 个空文件的临时树（session 级：建树在本机约 7 s，只付一次）。

    用 ``os.open(..., O_CREAT)`` 而不是 ``write_text``：payload 内容对本测试无意义，
    空的 dirent 就足以让走树付出真实代价。目录也一并建出来（10 个子目录），
    否则子目录递归这条路径不会被覆盖。
    """
    root = tmp_path_factory.mktemp("fp-perf") / "tree"
    root.mkdir()
    per_dir = _FP_PERF_FILES // _FP_PERF_DIRS
    for d in range(_FP_PERF_DIRS):
        sub = root / f"d{d:03d}"
        sub.mkdir()
        sub_str = str(sub)
        for i in range(per_dir):
            fd = os.open(os.path.join(sub_str, f"f{i:04d}.json"),
                         os.O_CREAT | os.O_WRONLY)
            os.close(fd)
    return root


def test_tree_fingerprint_is_fast_on_a_large_tree(_large_tree):
    """钉住：10k 文件的树取一次指纹必须在 0.5 s 内（旧实现 min-of-3 826-853 ms 会失败）。"""
    best = None
    fp = None
    for _ in range(3):  # 取最小值，避免一次磁盘抖动把结论判反
        t0 = time.perf_counter()
        fp = tree_fingerprint(_large_tree)
        elapsed = time.perf_counter() - t0
        best = elapsed if best is None else min(best, elapsed)

    # 先证明这棵树真的是 10k 文件规模 —— 否则"很快"这个结论是空的（vacuous）
    assert fp[0] == _FP_PERF_FILES + _FP_PERF_DIRS, (
        f"perf tree is not the expected size: count={fp[0]} "
        f"(expected {_FP_PERF_FILES} files + {_FP_PERF_DIRS} dirs)"
    )
    assert len(fp) == 3 and len(fp[1]) == 64 and fp[2] > 0
    assert best < _FP_PERF_BOUND_SEC, (
        f"tree_fingerprint over {_FP_PERF_FILES} files took {best * 1000:.1f} ms "
        f"(bound {_FP_PERF_BOUND_SEC * 1000:.0f} ms). The conftest outbox guard calls "
        f"this ~8x per test over the ~10k-file live outbox — anything near a second "
        f"per call makes the whole suite unusable."
    )


def test_tree_fingerprint_semantics_preserved(tmp_path):
    """钉住：性能重写没有削弱任何检测语义（增/删/触碰文件、增/删子目录）。

    每一步都与**前一步**的指纹比较（不是与最初的比较），所以每一步的"检测到了"
    都是真的检测，而不是状态绕了一圈又回到原点。
    """
    missing = tmp_path / "missing"
    # 缺失目录：稳定、不抛，且 count == 0
    assert tree_fingerprint(missing) == tree_fingerprint(missing)
    assert tree_fingerprint(missing)[0] == 0

    root = tmp_path / "sem"
    root.mkdir()
    fp = tree_fingerprint(root)
    assert fp[0] == 0
    # 存在但为空的目录 == 缺失目录（语义保持：digest 都是 sha256(b"")）
    assert fp[1] == tree_fingerprint(missing)[1]

    # 什么都不发生 → 指纹完全不变
    assert tree_fingerprint(root) == fp

    # 新增文件
    a = root / "a.json"
    a.write_text("{}", encoding="utf-8")
    fp_add_file = tree_fingerprint(root)
    assert fp_add_file != fp
    assert fp_add_file[0] == fp[0] + 1
    # 对一个**文件**取指纹（不是目录）仍返回稳定的空指纹，不抛
    assert tree_fingerprint(a)[0] == 0

    # 触碰（名字集合不变，只有 mtime_ns 变）→ 必须变
    st = a.stat()
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    fp_touch = tree_fingerprint(root)
    assert fp_touch != fp_add_file, "touching a file must change the fingerprint"
    assert fp_touch[0] == fp_add_file[0]
    assert fp_touch[2] > fp_add_file[2]  # max_mtime_ns 是触碰检测的落点（与旧实现一致）

    # 新增子目录
    sub = root / "nested"
    sub.mkdir()
    fp_add_dir = tree_fingerprint(root)
    assert fp_add_dir != fp_touch, "adding a subdirectory must change the fingerprint"
    assert fp_add_dir[0] == fp_touch[0] + 1

    # 子目录里的文件（递归确实走到底）
    (sub / "b.json").write_text("{}", encoding="utf-8")
    fp_sub_file = tree_fingerprint(root)
    assert fp_sub_file != fp_add_dir
    assert fp_sub_file[0] == fp_add_dir[0] + 1
    assert "nested/b.json" in tree_names(root)
    # 无事件 → 连续两次必须完全相等。这一条专门钉住 Windows 的"目录 mtime 延迟
    # 落盘"：目录时间戳在最后一个句柄关闭时才更新，若把 mtime 折进 digest，
    # 同一次写入之后两次背靠背的调用会不一致（性能重写时就踩过这个坑）。
    assert tree_fingerprint(root) == fp_sub_file == tree_fingerprint(root)

    # 删除文件
    a.unlink()
    fp_rm_file = tree_fingerprint(root)
    assert fp_rm_file != fp_sub_file, "removing a file must change the fingerprint"
    assert fp_rm_file[0] == fp_sub_file[0] - 1

    # 删除子目录（连同里面的文件）
    shutil.rmtree(sub)
    fp_rm_dir = tree_fingerprint(root)
    assert fp_rm_dir != fp_rm_file, "removing a subdirectory must change the fingerprint"
    assert fp_rm_dir[0] == fp_rm_file[0] - 2

    # 无变化 → 连续两次调用完全相等
    assert tree_fingerprint(root) == fp_rm_dir == tree_fingerprint(root)

    # 新增一个空子目录再删掉：两步各自都要改变指纹（顺序无关的滚动哈希不得吞掉它）
    empty_dir = root / "empty-dir"
    empty_dir.mkdir()
    fp_empty = tree_fingerprint(root)
    assert fp_empty != fp_rm_dir
    empty_dir.rmdir()
    assert tree_fingerprint(root) != fp_empty

    # 返回形状契约：3 元组，conftest 与既有测试按 fp[0]/fp[2] 取值
    assert len(fp_rm_dir) == 3
    assert isinstance(fp_rm_dir[0], int) and isinstance(fp_rm_dir[2], int)
    assert isinstance(fp_rm_dir[1], str) and len(fp_rm_dir[1]) == 64

    # 内部错误 → count == -1（永不等于任何健康指纹 → 前后比对必然炸，fail closed）
    bad = tree_fingerprint(object())  # Path(object()) raises TypeError
    assert bad[0] == -1
    assert bad != tree_fingerprint(root) and bad != tree_fingerprint(missing)
