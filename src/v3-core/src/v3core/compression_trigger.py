"""压缩事件触发器 — 每次 QA 配对后调用，检查是否需要压缩

设计原则：
- 事件驱动，不依赖 cron/定时器
- 每次调用查 state.json，有增量才处理
- 失败不抛，不影响 sync_turn
"""

import sys
import os
import threading
import logging
from typing import Any

# v3core.__init__._safe_err 是模块级函数, 同一包内直接相对导入即可。
# 避免压缩 daemon 线程异常时 NameError → 静默死 → 整条压缩链路断。
from . import _safe_err

logger = logging.getLogger("v3core.compression_trigger")

# 压缩引擎路径
_ENGINE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "compression_engine",
)

# state.json 路径（跟压缩引擎的 delta_run_state.json 同一位置）
_STATE_PATH = os.path.join(_ENGINE_DIR, "delta_run_state.json")

# last_compressed_id 缓存（避免每次调用读文件）
_last_id = None

# 后台压缩线程引用 — alive-check 守卫 (P10b 修复方案 A)
# 防止 maybe_compress() 并发调用 spawn 多个压缩线程 →
# 多个线程同时跑同一游标范围 → 重复压缩 + 撞 LLM 限流 (429 风暴)。
# 旧线程退出后引用保留但 is_alive() == False, 后续调用会正常 spawn 新线程。
_worker_thread: "threading.Thread | None" = None

# 压缩后触发 run_full_pipeline 的 debounce（秒）
# 30s 内只起一个后台线程跑全链路，不阻塞 sync_turn。
_FULL_PIPELINE_DEBOUNCE_SECONDS = 30


def _load_last_id() -> int:
    import json

    global _last_id
    try:
        if os.path.exists(_STATE_PATH):
            with open(_STATE_PATH, encoding="utf-8") as f:
                data = json.load(f)
                _last_id = data.get("last_compressed_id", 0)
        if _last_id is None:
            _last_id = 0
    except Exception:
        _last_id = 0
    return _last_id


def _get_latest_qa_id(pg_dsn: str, pool: Any = None) -> int:
    """查 qa_pairs 最新 id，不调 LLM"""
    if pool is not None:
        try:
            with pool.lease(timeout=3) as lease:
                cur = lease.connection.cursor()
                try:
                    cur.execute("SELECT max(id) FROM qa_pairs")
                    row = cur.fetchone()
                    return row[0] if row and row[0] else 0
                finally:
                    cur.close()
        except Exception:
            return 0

    import psycopg2

    try:
        conn = psycopg2.connect(pg_dsn, connect_timeout=3)
        cur = conn.cursor()
        cur.execute("SELECT max(id) FROM qa_pairs")
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row and row[0] else 0
    except Exception:
        return 0


def _schedule_full_pipeline(pg_dsn: str, delay: int) -> None:
    """30s 后在后台线程跑 run_full_pipeline()，不阻塞 sync_turn。

    通过 _next_full_pipeline_at 全局变量保证 30s 窗口内只启动一个 worker：
    进入函数立即预约一个时间戳，后续相同窗口内的调用直接 return。
    即使 maybe_compress 被连续多次调用进入此函数，也只有一个线程会被触发。

    注意：v6 fix — 不再接受 config 参数。
    v3core 传入的是 V3Config 实例 (dataclass)，不是 mapping；compression_engine
    内部的 run_full_pipeline() 做 ``{**DEFAULT_CONFIG, **(config or {})}``，
    传 V3Config 会抛 ``'V3Config' object is not a mapping``。
    修复：边界在 v3core 这边解出 pg_dsn 就够了，压缩引擎用 DEFAULT_CONFIG 自给自足。
    """
    import time as _time

    _now = _time.time()
    # 预约窗口：如果当前时间 < 已预约时间，说明已有 worker 在排期，跳过。
    reserved_until = globals().get("_next_full_pipeline_at", 0)
    if _now < reserved_until:
        return
    globals()["_next_full_pipeline_at"] = _now + delay

    def _worker():
        try:
            _time.sleep(delay)
            import importlib.util
            _init_path = os.path.join(_ENGINE_DIR, "__init__.py")
            if not os.path.exists(_init_path):
                logger.warning("_schedule_full_pipeline: __init__.py 不存在: %s", _init_path)
                return
            _spec = importlib.util.spec_from_file_location("compression_engine", _init_path)
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            run_full_pipeline = _mod.run_full_pipeline

            logger.info("全链路触发: 30s debounce 后调 run_full_pipeline()")
            # v6 fix: 不传 config — V3Config 不是 mapping，传给 compression_engine
            # 会抛 'V3Config' object is not a mapping。run_full_pipeline 用
            # DEFAULT_CONFIG 已经覆盖所有生产配置。
            result = run_full_pipeline(pg_dsn=pg_dsn)
            topics_written = (
                result.get("writing", {}).get("topics_written", 0)
                if result.get("writing")
                else 0
            )
            logger.info("全链路完成: %d topic 写入 PG", topics_written)
        except Exception as e:
            logger.warning("全链路触发失败(非致命): %s", _safe_err(e))

    t = threading.Thread(target=_worker, name="v3core-full-pipeline", daemon=True)
    t.start()


def maybe_compress(config: dict | None = None, pool: Any = None) -> None:
    """检查是否需要压缩，需要则调 run_delta()

    2026-08-06: 观察者 v2 后聚簇校准无人工核对环节 — 自动触发停用。
    开关: config compression.enabled (默认 false)。手动 run_delta/run_full_pipeline 仍可用。
    """
    # 开关检查: compression.enabled — 聚簇/增量压缩自动触发已停
    try:
        if not bool((config or {}).get("compression", {}).get("enabled", False)):
            return
    except Exception:
        return
    # v4 fix: debounce — sync_turn 每条 QA 都调一次, 无 debounce 会导致 PG max(id) 高频查询
    # 且如果 LLM 限速同步路径会卡。距离上次 < 10s 直接 return, 降低调用频率。
    import time as _time
    _now = _time.time()
    _last = globals().get("_last_compress_attempt", 0)
    if _now - _last < 10:
        return
    globals()["_last_compress_attempt"] = _now

    # 方案 B 开关: 导入锁存在 → 暂停压缩（重导/批量导入期间, 新消息照常写但不进压缩）
    # 导入脚本开始置 import_lock.flag, 完成删 flag 后压缩冷启动全量
    import os as _os
    from .config import _resolve_data_dir
    _lock_path = str(_resolve_data_dir(config) / "import_lock.flag")
    if _os.path.exists(_lock_path):
        logger.info("导入锁存在 (import_lock.flag), 跳过压缩 — 批量导入进行中")
        return

    if config is None:
        config = {}
    storage = config.get("storage", {})

    # 1. 取 PG DSN
    pg_cfg = storage.get("pg", {})
    pg_dsn = (
        f"host={pg_cfg.get('host', 'localhost')} "
        f"port={pg_cfg.get('port', 5433)} "
        f"dbname={pg_cfg.get('database', 'v3embeddings')} "
        f"user={pg_cfg.get('user', 'v3user')} "
        f"password={pg_cfg.get('password', '')}"
    )

    # 2. 快速检查：qa_pairs 有没有新数据
    last_id = _load_last_id()
    if pool is not None:
        latest_id = _get_latest_qa_id(pg_dsn, pool=pool)
    else:
        latest_id = _get_latest_qa_id(pg_dsn)
    if latest_id <= last_id:
        return  # 无新数据，跳过

    # 3. 有新数据 → 后台线程跑压缩，不阻塞 sync_turn
    #    首次（冷启动）可能跑十几分钟，但后续增量只有几条，很快。
    # v6 fix: 不再把 config 透传给后台 worker — V3Config 不是 mapping，
    # 透传给 compression_engine.run_delta 会抛 TypeError。后台 worker 只需要 pg_dsn。
    # P10b fix: alive-check 守卫 — 旧线程还在跑就直接 return，避免重复 spawn。
    # 注意：global 声明必须在任何对 _worker_thread 的赋值之前。
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        # 旧 worker 仍在跑，跳过本次 spawn；冷启动期间 run_delta 可能持续几分钟，
        # 期间每条新 QA 都触发 maybe_compress, 但都靠这里拦截，止住重复压缩。
        return
    worker_kwargs = {"pool": pool} if pool is not None else {}
    t = threading.Thread(
        target=_run_compression_worker,
        args=(pg_dsn,),
        kwargs=worker_kwargs,
        name="v3core-compression-worker",
        daemon=True,
    )
    _worker_thread = t
    t.start()
    logger.info("压缩已启动: %d → %d (后台)", last_id, latest_id)


def _run_compression_worker(pg_dsn: str, pool: Any = None) -> None:
    """后台线程跑压缩引擎，失败不抛，新数据再下次自动触发

    v6 fix: 不再接受 config 形参。
    v3core 传入的是 V3Config 实例 (dataclass)，不是 mapping；
    compression_engine.run_delta() 做 ``{**DEFAULT_CONFIG, **(config or {})}``，
    透传 V3Config 会抛 ``'V3Config' object is not a mapping``。
    修复：pg_dsn 已在 maybe_compress() 顶部解出；压缩引擎用 DEFAULT_CONFIG 自给自足。
    """
    if pool is not None:
        # compression_engine owns a legacy direct-DSN pipeline.  It is safe for
        # explicit offline/CLI calls only; never let a Runtime-backed provider
        # bypass its Registry-owned PgPool.
        logger.warning("Runtime-backed compression worker skipped: offline direct pipeline is not an online PG owner")
        return
    try:
        # 用 importlib 直接加载 compression_engine/__init__.py
        # 不依赖 sys.path（daemon thread 中 sys.path 状态不确定）
        import importlib.util
        _init_path = os.path.join(_ENGINE_DIR, "__init__.py")
        if not os.path.exists(_init_path):
            logger.warning("压缩引擎 __init__.py 不存在: %s", _init_path)
            return
        _spec = importlib.util.spec_from_file_location("compression_engine", _init_path)
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        run_delta = _mod.run_delta

        # v6 fix: 不传 config — 用 DEFAULT_CONFIG（见函数 docstring）
        result = run_delta(pg_dsn=pg_dsn)
        windows = result.get("windows", 0)
        if windows > 0:
            logger.info("压缩完成: %d 窗口已压缩", windows)
            # 压缩完成后 30s debounce 后台跑聚类+写 PG
            _schedule_full_pipeline(
                pg_dsn=pg_dsn,
                delay=_FULL_PIPELINE_DEBOUNCE_SECONDS,
            )
    except Exception as e:
        logger.warning("压缩线程失败(非致命), 下次自动重试: %s", _safe_err(e))