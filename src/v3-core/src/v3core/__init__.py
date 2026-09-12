"""v3-core — v3 记忆系统业务核心

零依赖 Hermes。4 档 mode: cloud / cloud-embed / cloud-llm / fully-local
"""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
import queue
import re
import logging
import threading
import time
from collections.abc import MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import psycopg2

from .config import resolve_config, _resolve_prompt
from .card_store import DeepStore
from .pg_store import PgEmbedStore
from .embedding import (
    _EMBED_CACHE, call_embedding,
    build_embed_cfg, safe_embed_cfg,
    get_embed_fingerprint, get_embed_profile, EmbedProfile,
)
from .prefetch import prefetch_to_context_block
from ._deadline import PrefetchDeadlineExceeded, bind_store_deadline, coerce_deadline
from .ingest import LiveBuffer
from .types import CardResult
from .llm import LLMClient
from .scheduler import E1Scheduler
from .injector import MemoryInjector
from .session_context import V3SessionContext
from .offload import ToolLogOffloader


@contextlib.contextmanager
def _lease_pg_store(pg: Any, timeout: float | None = 5, *, deadline=None):
    """Context manager for leasing a connection from PgEmbedStore or legacy store/mock.

    - If `pg` is None: yields None.
    - If `pg` has an active pool and callable `lease`:
      uses `with pg.lease(timeout=timeout) as conn: yield conn`.
    - Otherwise: preserves legacy `pg._connect()` raw connection behavior without
      requiring legacy fakes or mocks to implement lease.
    """
    deadline = coerce_deadline(deadline)
    if deadline is not None:
        deadline.check(context="core lease")
        pg = bind_store_deadline(pg, deadline)

    if pg is None:
        yield None
        return

    pool = getattr(pg, "pool", None)
    if pool is None:
        pool = getattr(pg, "_pool", None)

    is_mock_pool = hasattr(pool, "_mock_return_value") or hasattr(pool, "_mock_methods")
    is_real_pool = pool is not None and not is_mock_pool

    lease_fn = getattr(pg, "lease", None)
    is_mock_lease = hasattr(lease_fn, "_mock_return_value") or hasattr(lease_fn, "_mock_methods")
    is_real_lease = callable(lease_fn) and not is_mock_lease

    if is_real_pool and is_real_lease:
        with pg.lease(timeout=timeout) as conn:
            yield conn
    else:
        conn = None
        connect_fn = getattr(pg, "_connect", None)
        if callable(connect_fn):
            conn = connect_fn()
        yield conn


def _resolve_base_path_for_lock(core: "V3Core") -> str:
    """P0-C (2026-08-26): 解析 core 的 basePath 供 advisory-lock 工厂用。

    独立小函数便于测试 monkeypatch（test_p0c_singleton_lock_contract）。
    """
    try:
        return core._get_base_path()
    except Exception:
        return ""

logger = logging.getLogger("v3core")


# -- 日志脱敏 (v3-hermes-plugin 依赖 _safe_err) --
_PWD_SIG = re.compile(r'(password|passwd|pwd)\s*[=:]\s*\S+', re.IGNORECASE)
_DSN_SIG = re.compile(r'://[^:]+:[^@]+@')
_BEARER_SIG = re.compile(r'Bearer\s+\S+', re.IGNORECASE)
_APIKEY_SIG = re.compile(r'(api[_-]?key)\s*[=:]\s*\S+', re.IGNORECASE)


def _extract_base_path(cfg) -> str:
    """兼容 V3Config / dict / None 解析 basePath."""
    if cfg is None:
        return ""
    if isinstance(cfg, dict):
        return cfg.get("basePath", "") or ""
    # V3Config dataclass
    return getattr(cfg, "base_path", "") or ""


def _extract_embed_cfg(cfg):
    """Compatibility alias for the strict embedding configuration factory.

    Callers must receive either ``None`` (embedding section completely absent)
    or a factory-built config carrying ``_profile``/``_fingerprint``.  Incomplete
    explicit configuration is fail-closed by ``safe_embed_cfg``.
    """
    return safe_embed_cfg(cfg)


def _extract_rerank_cfg(cfg) -> dict:
    """兼容 V3Config / dict / None 解析 rerank 子配置, 总返回 raw dict."""
    if cfg is None:
        return {}
    if isinstance(cfg, dict):
        return cfg.get("storage", {}).get("rerank", {}) or {}
    if hasattr(cfg, "rerank") and cfg.rerank:
        return cfg.rerank.to_legacy_dict()
    return {}


def _safe_err(e: Exception, max_len: int = 80) -> str:
    """脱敏异常信息 -- 屏蔽 password/DSN/Bearer/apiKey"""
    s = str(e)[:200]
    s = _DSN_SIG.sub('://***:***@', s)
    s = _PWD_SIG.sub(r'\1=***', s)
    s = _BEARER_SIG.sub('Bearer ***', s)
    s = _APIKEY_SIG.sub(r'\1=***', s)
    return s[:max_len]


def _parse_msg_timestamp(ts: Any) -> Optional["datetime"]:
    """安全地把 msg["timestamp"] 解析成 tz-aware datetime（UTC）。

    支持输入类型（agent runtime / 各类适配器都会塞进来）：
    - None → None（让调用方走 NOW() 兜底）
    - datetime 对象 → 直接用 / 强制 tz-aware
    - float / int（Unix 秒或毫秒） → 自动判断（>1e12 视为毫秒）
    - str（ISO 8601 / 带时区后缀 / 不带时区 → 默认 UTC） → 解析
    - 解析失败 → None（不抛异常，让调用方走 NOW() 兜底）

    设计：宁可返回 None 走 NOW()，绝不抛——sync_turn 是热路径。
    """
    import datetime as _dt
    if ts is None:
        return None
    # datetime
    if isinstance(ts, _dt.datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=_dt.timezone.utc)
        return ts.astimezone(_dt.timezone.utc)
    # float / int
    if isinstance(ts, (int, float)):
        try:
            v = float(ts)
            # 毫秒 vs 秒 自动判断（毫秒通常 > 1e12）
            if v > 1e12:
                v = v / 1000.0
            return _dt.datetime.fromtimestamp(v, tz=_dt.timezone.utc)
        except Exception:
            return None
    # str
    if isinstance(ts, str) and ts.strip():
        try:
            s = ts.strip().replace("Z", "+00:00")
            dt = _dt.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            return dt.astimezone(_dt.timezone.utc)
        except Exception:
            return None
    return None


def _stable_message_key(msg: dict) -> tuple[str, bool]:
    """Return ``(stable_key, reliable)`` for a sync_turn message.

    Compression-aware source-ingest durability (2026-09-07):
    ``sync_turn`` no longer relies on list-index / positional cursors.
    Every message must carry a stable idempotency key so re-syncing a
    compressed snapshot never silently drops or duplicates a turn.

    Preference order (first non-empty wins):
      1. Provider-assigned ids (canonical row identity):
         ``_row_id`` → ``id`` → ``message_id`` → ``platform_message_id``
         → ``event_id``.
      2. Identity correction (2026-09-07 mid-turn): when the provider
         preserves the durable row ``timestamp`` while rewriting content
         (Hermes ``drop_stale_api_content`` / image shrink / summary-tail
         rewrite), the canonical fallback is ``timestamp + role`` —
         NEVER content hash, because the same durable row with rewritten
         content must collapse to the same key (otherwise re-sync would
         enqueue a duplicate durable row).
      3. Truly-no-timestamp legacy fallback: ``role + sha256(content)``,
         marked ``reliable=False`` for diagnostics only.  New callers
         MUST emit a stable id or a normalized timestamp.

    Never use list index or fuzzy text for the key.
    """
    if not isinstance(msg, dict):
        return ("", False)
    for field_name in ("_row_id", "id", "message_id", "platform_message_id", "event_id"):
        v = msg.get(field_name)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return (s, True)
    # canonical timestamp + role fallback — survives content rewrites of
    # the same durable row (Hermes drop_stale_api_content / image shrink /
    # summary-tail rewrite).  ``reliable=True`` because normalized
    # timestamp is a durable, stable row identity.
    try:
        role = str(msg.get("role", "") or "")
        dt = _parse_msg_timestamp(msg.get("timestamp"))
        if dt is not None:
            return (f"ts:{dt.isoformat()}|{role}", True)
    except Exception:
        pass
    # legacy: no provider id AND no timestamp → content hash, unreliable
    try:
        import hashlib as _hl
        role = str(msg.get("role", "") or "")
        content = str(msg.get("content", "") or "")
        digest = _hl.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
        return (f"fb:{role}|{digest}", False)
    except Exception:
        return ("", False)



# ─ 多专家 prompt 模板 (v3_extract multi-expert mode) ─
EXTRACTOR_PROMPTS: dict[str, str] = {
    "decisions": "从以下对话中提取关键决策。每项：什么决策 + 为什么做这个选择 + 影响。输出格式：- <决策>：<原因> → <影响>",
    "lessons": "从以下对话中提取踩坑教训。每项：踩到什么坑 + 根因 + 怎么避免。输出格式：- <坑>：<根因> → <对策>",
    "projects": "从以下对话中提取项目进展。每项：项目名 + 当前状态 + 阻塞/下一步。输出格式：- <项目>：<状态> -> <下一步>",
    "system": "从以下对话中提取系统/配置变更。每项：什么变了 + 原因 + 影响范围。输出格式：- <变更>：<原因> → <影响>",
}


class _PendingQAView(MutableMapping[str, dict]):
    """Backward-compatible mapping view over per-session Context.pending_qa.

    The view is deliberately not a second store.  Existing tests and legacy
    callers can still inspect ``core._pending_qa`` while the Context registry
    remains the only mutable source of truth.
    """

    def __init__(self, core: "V3Core") -> None:
        self._core = core

    def _contexts(self) -> dict[str, V3SessionContext]:
        contexts = getattr(self._core, "_session_contexts", None)
        return contexts if isinstance(contexts, dict) else {}

    def __getitem__(self, session_id: str) -> dict:
        context = self._contexts().get(session_id)
        if context is None or context.pending_qa is None:
            raise KeyError(session_id)
        return context.pending_qa

    def __setitem__(self, session_id: str, pending: dict) -> None:
        context = self._core.get_session_context(session_id)
        context.pending_qa = pending

    def __delitem__(self, session_id: str) -> None:
        context = self._contexts().get(session_id)
        if context is None or context.pending_qa is None:
            raise KeyError(session_id)
        context.pending_qa = None

    def __iter__(self):
        for session_id, context in list(self._contexts().items()):
            if context.pending_qa is not None:
                yield session_id

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, MutableMapping):
            other = dict(other.items())
        return dict(self.items()) == other


class V3Core:
    """v3-core 统一入口"""

    def __init__(self, profile: str = "default", config_path: str | None = None,
                 hermes_home: str | None = None, pg_pool: Any | None = None,
                 effective_config: Any | None = None, runtime: Any | None = None):
        from .config_model import V3Config
        if runtime is not None:
            if pg_pool is None:
                pg_pool = getattr(runtime, "pg_pool", None)
            if effective_config is None:
                effective_config = getattr(runtime, "effective_config", None)
            identity = getattr(runtime, "identity", None)
            if not hermes_home and identity is not None:
                hermes_home = identity.canonical_hermes_home
        self._runtime = runtime
        self._profile = profile
        self._config_path = config_path
        self._pg_pool = pg_pool
        # hermes_home (官方 MemoryProvider 协议): 全新安装场景下, 数据根放 hermes_home 下.
        # 老路径 ~/.v3-core/profiles/<profile>/ 存在时, _find_config 会优先用它,
        # 所以本机生产路径零变化 (见 config._find_config / resolve_config 的 hermes_home 契约).
        self._hermes_home: str = hermes_home or ""
        # Runtime-backed facades receive the already-resolved generation snapshot.
        # Legacy facades keep lazy config resolution for compatibility.
        self._config: V3Config | dict | None = effective_config
        self._store: DeepStore | None = None
        self._pg: PgEmbedStore | None = None
        self._live_buffer: LiveBuffer | None = None
        self._live_buffer_pg: PgEmbedStore | None = None
        self._pg_was_connected: bool = False  # track PG state for fail detection
        self._moc: Any | None = None
        self._initialized = False
        self._identity_block: str | None = None  # cached identity block (session scope)
        self._identity_block_mtime: float = 0.0  # cache timestamp
        # 手帐机制 C: 系统态势总览缓存（毫秒级纯读 + mtime）
        self._situation_overview: str | None = None
        self._situation_overview_mtime: float = 0.0
        self._e1_scheduler: E1Scheduler | None = None  # 内部 e1 调度线程

        self._injector: MemoryInjector | None = None  # 统一注入器 (lazy init)
        # topic_recall 缓存（后台静默刷新）
        self._topic_recall: Any | None = None
        self._topic_recall_count: int = 0
        self._topic_refresher_stop = threading.Event()
        self._topic_refresher_thread: threading.Thread | None = None
        # Runtime-backed providers pay jieba's one-time dictionary load during
        # startup, not inside the first deadline-bound prefetch.  If the
        # optional tokenizer cannot warm here, the original lazy path remains.
        if runtime is not None:
            try:
                from .tokenizer import build_query_tokens as _warm_query_tokenizer
                _warm_query_tokenizer("__v3core_tokenizer_warmup__")
            except Exception:
                pass
        # Optional v1.4 anchor table capability.  ``None`` means unknown;
        # False is learned only from an explicit UndefinedTable/42P01 error.
        self._note_segments_available: bool | None = None
        # Session-local state belongs to one lightweight context per session.
        self._session_contexts: dict[str, V3SessionContext] = {}
        self._active_session_id: str = ""
        self._session_context_lock = threading.RLock()
        # Compatibility view only; Context.pending_qa is the source of truth.
        self._pending_qa = _PendingQAView(self)
        self._qa_lock = threading.Lock()
        self._qa_flush_timer: threading.Timer | None = None
        # --- core-local quiesce/durability/fence (RC3/RC4) ---
        self._core_accepting = True
        self._core_fenced = False
        self._core_generation = 0
        self._core_closed = False
        self._qa_stop = threading.Event()
        self._qa_stop_sentinel = object()
        self._core_shutdown_handoff = False
        self._flush_queue: queue.Queue = queue.Queue()
        self._flush_worker: threading.Thread | None = None
        self._qa_durability_lock = threading.RLock()
        self._core_lock = threading.RLock()

    # ── 配置 ──

    @property
    def pg_pool(self) -> Any:
        return self._pg_pool

    @property
    def e1_service(self) -> Any:
        """Runtime-owned E1 service exposed through the Core façade."""
        runtime = getattr(self, "_runtime", None)
        if runtime is None:
            return None
        return getattr(runtime, "e1_service", None)

    @property
    def observer_service(self) -> Any:
        """Runtime-owned Observer service exposed through the Core façade."""
        runtime = getattr(self, "_runtime", None)
        if runtime is None:
            return None
        return getattr(runtime, "observer_service", None)

    @property
    def topic_recall_cache(self) -> Any:
        runtime = getattr(self, "_runtime", None)
        if runtime is None:
            return None
        return getattr(runtime, "topic_recall_cache", None)

    @property
    def config(self):
        """Return V3Config (or dict for legacy callers).

        Tries to return ``V3Config`` first; falls back to raw dict if the
        loader somehow returns one (e.g. user passes return_legacy=True).

        hermes_home 透传给 resolve_config — 老路径存在时仍用老路径 (本机生产
        零变化), 只有全新安装才会被 hermes_home 接管.
        """
        if self._config is None:
            self._config = resolve_config(self._profile, hermes_home=self._hermes_home)
        return self._config

    @config.setter
    def config(self, value):
        self._config = value

    def get_session_context(
        self,
        session_id: str | None = None,
        *,
        create: bool = True,
    ) -> V3SessionContext | None:
        """Return the lightweight context for one session.

        The registry is owned by this Core facade, while each value contains
        only disposable session state.  ``create=False`` is used by flush and
        lifecycle paths that must not manufacture a new session.
        """
        contexts = getattr(self, "_session_contexts", None)
        if not isinstance(contexts, dict):
            if not create:
                return None
            contexts = {}
            self._session_contexts = contexts
        lock = getattr(self, "_session_context_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._session_context_lock = lock
        sid = session_id or getattr(self, "_active_session_id", "") or ""
        with lock:
            context = contexts.get(sid)
            if context is None and create:
                context = V3SessionContext(session_id=sid)
                contexts[sid] = context
            return context

    @property
    def session_contexts(self) -> dict[str, V3SessionContext]:
        """Read-only-by-convention view used by the provider compatibility layer."""
        contexts = getattr(self, "_session_contexts", None)
        return contexts if isinstance(contexts, dict) else {}

    @property
    def store(self) -> DeepStore:
        if self._store is None:
            self._store = DeepStore(self.config)
        return self._store

    @property
    def pg(self) -> PgEmbedStore:
        if self._pg is None:
            self._pg = PgEmbedStore(self.config, pool=self._pg_pool)
            invalidate = getattr(self.topic_recall_cache, "invalidate", None)
            if callable(invalidate):
                self._pg._on_topics_commit = invalidate
        return self._pg

    @property
    def live_buffer(self) -> LiveBuffer:
        if self._live_buffer is None:
            pg_connected = self.pg and self.pg.is_connected()
            if pg_connected:
                self._pg_was_connected = True
            pg = self.pg if pg_connected or getattr(self, "_pg_pool", None) is not None else None
            self._live_buffer = LiveBuffer(pg=pg, config=self.config)
            # Phase 6.2: PG 写成功后触发轻量主题匹配.
            # Bind the V3Core method (not LiveBuffer self) so flush invalidation
            # can reach runtime.topic_recall_cache after the lease is released.
            self._live_buffer._post_flush = self._on_live_flush
        return self._live_buffer

    @property
    def moc(self) -> Any:
        """手帐索引 (lazy-load MOCManager)"""
        if self._moc is None:
            from .moc import MOCManager
            self._moc = MOCManager(self._get_base_path())
        return self._moc

    def _get_base_path(self) -> Path:
        """解析 profile 根目录 — 落盘方法依赖它"""
        cfg = self.config
        base = _extract_base_path(cfg)
        if not base:
            base = str(Path.home() / ".v3-core" / "profiles" / self._profile)
        return Path(base)

    # ── 生命周期 ──

    def initialize(self) -> None:
        """初始化 — 验证配置 + 启动 E1 内部调度"""
        # Runtime-backed Core only starts the Runtime-owned services.  A
        # legacy standalone Core keeps the old per-Core scheduler for
        # compatibility.
        try:
            runtime = getattr(self, "_runtime", None)
            if runtime is not None:
                start_services = getattr(runtime, "start_services", None)
                if callable(start_services):
                    start_services()
            else:
                base_path = self._get_base_path()
                # P0-C (2026-08-26): legacy standalone Core keeps the
                # injected PG advisory singleflight behavior.
                from .daemon import build_default_lock_provider

                sched = E1Scheduler(
                    state_dir=base_path,
                    callback=self._run_e1,
                    lock_provider=build_default_lock_provider(
                        config=self._config,
                        name="e1_singleton",
                        pool=getattr(self, "_pg_pool", None),
                    ),
                )
                sched.start()
                self._e1_scheduler = sched
                logger.info("E1 内部调度器已启动 (base=%s)", base_path)
        except Exception as e:
            logger.warning("E1 内部调度器启动失败 (不阻塞): %s", _safe_err(e)[:100])


        self._initialized = True
        logger.info("V3Core 初始化 (profile=%s)", self._profile)
        # recover durable QA markers deterministically (sorted, dedup)
        try:
            self._recover_pending_qa()
        except Exception as e:
            logger.warning("recover pending QA failed: %s", _safe_err(e))
        # also LiveBuffer will recover its own pending on its init; ensure it exists
        # touch live_buffer to trigger its recovery via its __init__ already

        # Runtime-backed Core uses the Runtime-owned cache/refresher.
        if getattr(self, "_runtime", None) is None:
            self._start_topic_recall_refresher()

    def _run_e1(self) -> str:
        """调用 e1 合成印 + dreamer 置信度验证 — 外部 cron 也通过此路径"""
        from .e1 import synthesize_yin
        on_topics_commit = self._notify_topic_recall_invalidation
        if getattr(self, "_pg_pool", None) is not None:
            result = synthesize_yin(
                config=self._config,
                pool=self._pg_pool,
                on_topics_commit=on_topics_commit,
            )
        else:
            result = synthesize_yin(
                config=self._config,
                on_topics_commit=on_topics_commit,
            )
        # e1 成功后顺带跑 dreamer 验证
        try:
            base_path = self._get_base_path()
            cards_dir = base_path / "cards"
            if cards_dir.exists():
                from .dreamer import run_dreamer_pass
                dreamer_result = run_dreamer_pass(base_path)
                if dreamer_result.get("boosted", 0) > 0 or dreamer_result.get("verified", 0) > 0:
                    logger.info("dreamer: boosted=%d verified=%d",
                                dreamer_result["boosted"], dreamer_result["verified"])
        except Exception as e:
            logger.warning("dreamer pass 失败 (不阻塞): %s", _safe_err(e)[:200])
        return result

    def is_available(self) -> bool:
        """轻量可用性检查 — PG 通或文件卡库可用即认为 v3-core 在线"""
        try:
            if self.pg and self.pg.is_connected():
                return True
            # 没有 PG 也可用（纯文件模式）
            return self.store is not None
        except Exception:
            return False

    def _start_topic_recall_refresher(self) -> None:
        """启动 topic_recall 后台刷新线程

        每 60s 检查 topic 数量是否变化, 有变化则静默重建缓存.
        新建 TopicRecall 实例后原子替换 self._topic_recall,
        旧实例在被替换后由 GC 回收, 不影响正在使用旧实例的 prefetch 调用.
        """
        if self._topic_refresher_stop.is_set():
            self._topic_refresher_stop.clear()

        def _refresh_loop():
            # 冷启动优化: 首轮先 wait(60) 让 prefetch 主线程先完成加载,
            # 避免与 prefetch_to_context_block / recall_pool 并发抢 PG 全量加载.
            # 第二次循环起 wait(60) 仍然在末尾 (相当于"先睡再检查, 间隔仍是 60s").
            self._topic_refresher_stop.wait(60)
            while not self._topic_refresher_stop.is_set():
                try:
                    pg = self._pg or self.pg
                    with _lease_pg_store(pg) as conn:
                        if conn:
                            cur = conn.cursor()
                            cur.execute(
                                "SELECT COUNT(*) FROM topics "
                                "WHERE status='active' AND embedding IS NOT NULL"
                            )
                            new_count = cur.fetchone()[0]
                            cur.close()
                            if new_count != self._topic_recall_count:
                                from .topic_recall import TopicRecall
                                embed_cfg = safe_embed_cfg(self.config)
                                new_recall = TopicRecall(embed_cfg, pool=self._pg_pool)
                                new_recall._ensure_loaded()
                                self._topic_recall = new_recall
                                self._topic_recall_count = new_count
                                logger.info(
                                    "topic_recall 缓存已刷新: %d topics",
                                    new_count,
                                )
                except Exception:
                    pass  # 刷新失败不影响现有缓存
                self._topic_refresher_stop.wait(60)

        t = threading.Thread(
            target=_refresh_loop,
            name="v3core-topic-refresher",
            daemon=True,
        )
        self._topic_refresher_thread = t
        t.start()

    # --- QA durable owner (RC3) ---
    def _qa_pending_dir(self) -> Path | None:
        try:
            base = self._get_base_path()
            if not base:
                return None
            # ensure Path
            if isinstance(base, str):
                base = Path(base)
            # guard against mock objects in tests that return MagicMock for base path
            if type(base).__module__.startswith("unittest.mock"):
                return None
            if not isinstance(base, Path):
                return None
            return base / "j" / "pending_qa"
        except Exception:
            return None

    def _qa_job_id(self, session_id: str, pending: dict) -> str:
        try:
            payload = {
                "session_id": session_id,
                "q": pending.get("q", ""),
                "a": pending.get("a", ""),
                "q_msg_id": pending.get("q_msg_id", ""),
                "q_turn": pending.get("q_turn", ""),
                "q_ts": str(pending.get("q_ts", "")) if pending.get("q_ts") is not None else "",
                "tool_calls": pending.get("tool_calls", []),
                "tool_results": pending.get("tool_results", []),
            }
            data = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            return hashlib.sha256(data).hexdigest()
        except Exception:
            return hashlib.sha256(f"{session_id}:{pending.get('q_msg_id','')}:{id(pending)}".encode()).hexdigest()

    def _persist_qa_pending(self, session_id: str, pending: dict) -> Path | None:
        try:
            dirp = self._qa_pending_dir()
            if dirp is None:
                return None
            job_id = self._qa_job_id(session_id, pending)
            path = dirp / f"{job_id}.json"
            if path.exists():
                return path
            payload = {
                "version": 1,
                "job_id": job_id,
                "session_id": session_id,
                "pending": pending,
            }
            # need to handle datetime in pending: default=str handles
            with self._qa_durability_lock:
                dirp.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
                data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(data + "\n")
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                os.replace(str(tmp), str(path))
                try:
                    fd = os.open(str(dirp), os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except Exception:
                    pass
            return path
        except Exception as e:
            logger.warning("QA _persist_qa_pending failed: %s", _safe_err(e)[:120])
            return None

    def _ack_qa_pending(self, session_id: str, pending: dict):
        try:
            dirp = self._qa_pending_dir()
            if dirp is None:
                return
            job_id = self._qa_job_id(session_id, pending)
            path = dirp / f"{job_id}.json"
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        except Exception:
            pass

    def _mark_qa_embedding_failure(self, session_id: str, pending: dict, error: BaseException) -> None:
        """Keep a durable QA marker and record bounded embedding retry state."""
        path = self._persist_qa_pending(session_id, pending)
        if path is None:
            logger.warning("QA embedding failure has no durable marker (session=%s)", session_id)
            return
        try:
            with self._qa_durability_lock:
                data = json.loads(path.read_text(encoding="utf-8"))
                attempts = int(data.get("embedding_attempts", 0) or 0) + 1
                max_attempts = 3
                delay_s = min(3600.0, 60.0 * (2 ** min(attempts - 1, 5)))
                error_sig = hashlib.sha256(
                    f"{type(error).__name__}:{error}".encode("utf-8", errors="replace")
                ).hexdigest()
                data.update(
                    {
                        "embedding_status": "poisoned" if attempts >= max_attempts else "failed",
                        "embedding_attempts": attempts,
                        "embedding_next_retry_at": time.time() + delay_s,
                        "embedding_error_fingerprint": error_sig,
                    }
                )
                tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str) + "\n")
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                os.replace(str(tmp), str(path))
        except Exception as e:
            logger.warning("QA embedding failure state update failed: %s", _safe_err(e)[:120])

    def _recover_pending_qa(self):
        try:
            dirp = self._qa_pending_dir()
            if dirp is None or not dirp.exists():
                return
            files = sorted(dirp.glob("*.json"))
            seen: set[str] = set()
            for p in files:
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    session_id = str(data.get("session_id", ""))
                    pending = data.get("pending")
                    if not isinstance(pending, dict) or not pending.get("q"):
                        continue
                    job_id = str(data.get("job_id") or self._qa_job_id(session_id, pending))
                    attempts = int(data.get("embedding_attempts", 0) or 0)
                    if attempts >= 3:
                        logger.warning("QA embedding marker quarantined after bounded retries: %s", p.name)
                        continue
                    retry_at = data.get("embedding_next_retry_at")
                    if retry_at is not None and float(retry_at) > time.time():
                        continue
                    if job_id in seen:
                        continue
                    seen.add(job_id)
                    # use internal submit without re-persist duplication check
                    # directly put into queue if accepting
                    if getattr(self, '_core_fenced', False) or not getattr(self, '_core_accepting', True):
                        continue
                    # ensure worker exists
                    q = getattr(self, '_flush_queue', None)
                    if q is None:
                        q = queue.Queue()
                        self._flush_queue = q
                    # persist already exists, just enqueue
                    try:
                        q.put((session_id, pending))
                    except Exception:
                        pass
                    # also ensure worker running
                    with self._core_lock:
                        if not getattr(self, '_core_fenced', False) and getattr(self, '_core_accepting', True):
                            wk = getattr(self, '_flush_worker', None)
                            if wk is None or not wk.is_alive():
                                self._flush_worker = threading.Thread(target=self._flush_worker_loop, name="v3-qa-flush", daemon=True)
                                self._flush_worker.start()
                except Exception as e:
                    logger.warning("QA recover skip %s: %s", p.name, _safe_err(e)[:80])
        except Exception:
            pass

    def _spill_all_pending_qa_to_durable(self):
        items: list = []
        try:
            contexts = getattr(self, "_session_contexts", None)
            if isinstance(contexts, dict):
                with self._qa_lock:
                    items = [(sid, ctx, ctx.pending_qa) for sid, ctx in list(contexts.items()) if ctx.pending_qa and ctx.pending_qa.get("q")]
            else:
                pending_dict = getattr(self, "_pending_qa", {})
                items = [(sid, None, pending) for sid, pending in list(pending_dict.items()) if pending and pending.get("q")]
        except Exception:
            return items
        try:
            for sid, ctx, pending in items:
                try:
                    self._persist_qa_pending(sid, pending)
                except Exception:
                    pass
            # also spill any already-queued items that may not have durable yet (ensure each queued pending has file)
            q = getattr(self, "_flush_queue", None)
            if q is not None:
                try:
                    # queue.queue is deque under lock; iterate snapshot
                    with q.mutex:
                        queued = list(q.queue)
                    for sess, pend in queued:
                        try:
                            self._persist_qa_pending(sess, pend)
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass
        return items

    def _is_core_fenced(self) -> bool:
        return bool(getattr(self, '_core_fenced', False) or getattr(self, '_core_closed', False))

    def shutdown(self, timeout: float | None = 1.0) -> None:
        """关闭 — bounded quiesce + durable handoff + fence (monotonic deadline)"""
        # compatible: timeout default 1.0, legacy callers with no args still work
        if timeout is None:
            timeout = 1.0
        try:
            timeout = float(timeout)
        except Exception:
            timeout = 1.0
        if timeout < 0:
            timeout = 0.0
        deadline = time.monotonic() + timeout
        # 1. stop accepting
        with self._core_lock:
            if getattr(self, '_core_closed', False):
                return
            self._core_accepting = False
            self._core_generation += 1
        # 1b. stop topic refresher if legacy — bounded join with remaining deadline
        if getattr(self, "_runtime", None) is None:
            try:
                self._topic_refresher_stop.set()
            except Exception:
                pass
            try:
                t = getattr(self, "_topic_refresher_thread", None)
                if t is not None and t.is_alive() and t is not threading.current_thread():
                    remaining = max(0.0, deadline - time.monotonic())
                    # daemon refresher: bounded join, no unbounded wait
                    t.join(timeout=remaining if remaining > 0 else 0)
                    if t.is_alive():
                        logger.warning("V3Core shutdown bounded: topic refresher still alive after %.2fs, fenced (daemon)", timeout)
            except Exception:
                pass
        # 2. cancel timer
        try:
            if self._qa_flush_timer is not None:
                self._qa_flush_timer.cancel()
        except Exception:
            pass
        self._qa_flush_timer = None
        # 3. durably persist every open Context.pending_qa and already-submitted QA job
        try:
            self._spill_all_pending_qa_to_durable()
        except Exception as e:
            logger.warning("shutdown spill pending failed: %s", _safe_err(e))
        # 3b. shutdown handoff: dispatch normal q+a pending to existing worker bounded drain
        try:
            with self._core_lock:
                self._core_shutdown_handoff = True
            remaining = max(0.0, deadline - time.monotonic())
            self._flush_all_pending_qa(timeout=remaining)
        finally:
            with self._core_lock:
                self._core_shutdown_handoff = False
        # 4. stop / bounded join QA worker (prohibit q.join without deadline) — remaining deadline
        try:
            self._qa_stop.set()
        except Exception:
            pass
        worker = getattr(self, '_flush_worker', None)
        if worker is not None and worker.is_alive():
            try:
                remaining = max(0.0, deadline - time.monotonic())
                worker.join(timeout=remaining if remaining > 0 else 0)
            except Exception:
                pass
            if worker.is_alive():
                logger.warning("V3Core shutdown bounded: QA worker still alive after %.2fs, fenced", timeout)
            # bounded drain: empty queue without blocking, keep durable files for later recovery
            q = getattr(self, '_flush_queue', None)
            if q is not None:
                try:
                    while True:
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            break
                        try:
                            q.task_done()
                        except Exception:
                            pass
                except Exception:
                    pass
        # set fenced after bounded join so late embedding returns cannot lease PG
        with self._core_lock:
            self._core_closed = True
            self._core_fenced = True
        # 5. bounded LiveBuffer shutdown — remaining deadline
        if self._live_buffer:
            try:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    self._live_buffer.shutdown(timeout=remaining if remaining > 0 else 0)
                except TypeError:
                    logger.warning("LiveBuffer shutdown skip: remaining=%.2fs no timeout support, bounded detach fenced", remaining)
            except Exception as e:
                logger.warning("LiveBuffer shutdown failed: %s", _safe_err(e))
        # 6. bounded legacy E1 scheduler shutdown — remaining deadline
        if getattr(self, "_runtime", None) is None and self._e1_scheduler:
            try:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    self._e1_scheduler.shutdown(timeout=remaining if remaining > 0 else 0)
                except TypeError:
                    logger.warning("E1Scheduler shutdown skip: remaining=%.2fs no timeout support, bounded detach fenced", remaining)
            except TimeoutError as te:
                logger.warning("E1Scheduler shutdown timed out after remaining %.2fs: %s", remaining, _safe_err(te))
            except Exception:
                pass
            self._e1_scheduler = None
        if self._pg:
            try:
                self._pg.close()
            except Exception:
                pass
        logger.info("V3Core shutdown (bounded)")

    def _notify_topic_recall_invalidation(self) -> None:
        cache = getattr(self, "topic_recall_cache", None)
        invalidate = getattr(cache, "invalidate", None)
        if callable(invalidate):
            invalidate()

    def get_status_report(self) -> str:
        """全量状态报告（含调度器状态）"""
        from .config_model import format_status
        sched_status = ""
        if self._e1_scheduler:
            sched_status = self._e1_scheduler.status
        else:
            service = self.e1_service
            if service is not None:
                sched_status = getattr(service, "status", "")
        return format_status(self._config, scheduler_status=sched_status, compression_status="")

    def _flush_completed_pending_qa(self, session_id: str) -> None:
        """Flush one session's completed QA without discarding an open question -- durable submit."""
        if not session_id:
            return
        if self._is_core_fenced():
            return
        if not getattr(self, '_core_accepting', True) and not getattr(self, '_core_shutdown_handoff', False):
            return
        context = self.get_session_context(session_id, create=False)
        if context is None or not context.pending_qa:
            return
        pending = context.pending_qa
        if not pending.get("q") or not pending.get("a"):
            return
        success = False
        try:
            # durable submit, not synchronous remote LLM -- bool-gated, failure not clear pending
            success = bool(self._submit_flush(session_id, pending))
            if not success:
                # truthful handoff failed without exception -- try exact per-job fallback
                try:
                    fb = self._persist_qa_pending(session_id, pending)
                    if fb is not None and Path(fb).exists():
                        success = True
                except Exception:
                    success = False
        except Exception as e:
            logger.warning("_flush_completed_pending_qa submit failed: %s", _safe_err(e))
            # try exact per-job fallback before deciding to preserve
            try:
                fb = self._persist_qa_pending(session_id, pending)
                if fb is not None and Path(fb).exists():
                    success = True
                else:
                    # keep marker visible via spill fallback — do not clear context
                    success = False
            except Exception:
                success = False
        finally:
            if success and context is not None and context.pending_qa is pending:
                context.pending_qa = None
            # if not success, preserve context.pending_qa for next spill/retry (no silent loss)

    def switch_session(self, session_id: str, *,
                       parent_session_id: str = "",
                       reset: bool = False,
                       rewound: bool = False) -> None:
        """session 切换时刷新内部状态

        on_session_switch 驱动: 重建 live_buffer (flush 旧的), reset=True 时也清身份缓存.
        已完成 q+a 在切换时 flush；未完成 open-q 留在各自 SessionContext，
        由 shutdown/watchdog 的全量 flush 兜底。
        """
        # Keep unfinished q in its own Context so an interleaved session can resume it.
        old_session_id = getattr(self, "_active_session_id", "") or ""
        if old_session_id and old_session_id != session_id:
            old_context = self.get_session_context(old_session_id, create=False)
            if old_context is not None:
                old_context.summary_lifecycle["state"] = "suspended"
            try:
                self._flush_completed_pending_qa(old_session_id)
            except Exception as e:
                logger.warning("switch_session completed pending flush 失败: %s", _safe_err(e))
        # flush 旧 live buffer (对应 old session)
        if self._live_buffer:
            self._live_buffer.shutdown()
            self._live_buffer = None
        # 如果 reset=True, 清理身份缓存让新 session 重新压缩印
        if reset:
            self.reset_injector(session_id=session_id)
            self._identity_block = None
            self._identity_block_mtime = 0.0
            # 手帐机制 C: 同时清态势总览缓存
            self._situation_overview = None
            self._situation_overview_mtime = 0.0
        # 更新 profile (如果 session_id 变了)
        # ⚠️ 注意：不改 self._profile！profile 是部署配置名（如 "default"），
        #   session_id 是对话标识，两者不同。Runtime-backed Core 保留其
        #   effective_config snapshot；只有 legacy (无 pg_pool) facade 才在
        #   session switch 时按历史语义懒加载默认配置。
        if session_id and session_id != self._profile:
            # A Runtime-backed Core belongs to one frozen generation.  Session
            # changes reset facade caches, never reload the generation config.
            # 不清 _config（保留 generation snapshot，不因 session 切换重载）。
            self._store = None
            # bounded drain old QA queue before closing old _pg；未排空保留旧 _pg
            old_pg = self._pg
            if old_pg is not None:
                try:
                    self._drain_flush_queue(timeout=0.5)
                except Exception:
                    pass
                drained = True
                try:
                    q = getattr(self, "_flush_queue", None)
                    if q is not None:
                        try:
                            unfinished = int(getattr(q, "unfinished_tasks", 0))
                        except Exception:
                            unfinished = 0
                        try:
                            is_empty = q.empty()
                        except Exception:
                            is_empty = (unfinished == 0)
                        if unfinished != 0 or not is_empty:
                            drained = False
                        if drained:
                            try:
                                if q.qsize() != 0:
                                    drained = False
                            except Exception:
                                pass
                except Exception:
                    drained = True
                if drained:
                    try:
                        old_pg.close()
                    except Exception as _pg_close_err:
                        logger.warning("switch_session 关闭旧 PG 失败 (继续切换): %s",
                                       _safe_err(_pg_close_err))
                    self._pg = None
                else:
                    logger.warning("switch_session queue not drained (unfinished), retain old _pg")
                    # retain old_pg: do not clear reference
                    pass
            else:
                self._pg = None
            self._moc = None
        if session_id:
            self._active_session_id = session_id
            new_context = self.get_session_context(session_id)
            if new_context is not None:
                new_context.summary_lifecycle["state"] = "active"
        logger.info("V3Core.switch_session → %s (parent=%s, reset=%s, rewound=%s)",
                     session_id, parent_session_id, reset, rewound)

    def get_high_signal_snippets(self, messages) -> str:
        """从即将压缩的消息中提取高信号片段

        on_pre_compress 驱动: 扫最后 20 条 user/assistant 消息中的关键内容.
        返回空字符串 = 无贡献 (MemoryManager 会忽略).
        """
        if not messages:
            return ""
        snippets = []
        # 从尾往前取最近 20 条消息
        for msg in (messages or [])[-20:]:
            role = msg.get("role", "")
            content = str(msg.get("content", "") or "")
            if not content:
                continue
            # 只取有价值的内容: 长 user 消息, 或含关键字的 assistant 响应
            if role == "user" and len(content) > 50:
                snippets.append(f"[User]: {content[:200]}")
            elif role == "assistant" and any(k in content for k in ("决策", "结论", "修复", "方案", "根因", "原因")):
                snippets.append(f"[Assistant]: {content[:200]}")
        return "\n\n".join(snippets[-5:]) if snippets else ""

    def offload_messages(self, session_id: str, messages: list) -> str:
        """on_pre_compress 驱动: 把大工具输出外置到 refs/

        遍历 messages 列表, 把 role=="tool" 且 content > 5KB 的消息摘要化,
        完整原文写到 {v3_data_root}/refs/{session_id}/{turn_index}.txt.
        原地修改 messages 中的 content.

        返回
        ----
        str: JSON 格式压缩报告 — `json.dumps({"offloaded": N, "saved_chars": M})`.
        Hermes 的 on_pre_compress hook 拿到这个字符串后会作为高信号片段注入.
        """
        try:
            refs_root = Path(self.get_v3_data_root())
            offloaded, saved = ToolLogOffloader.offload_tool_logs(
                messages, session_id=session_id, refs_root=refs_root,
            )
        except Exception as e:
            logger.warning("offload_messages 失败: %s", _safe_err(e))
            return json.dumps({"offloaded": 0, "saved_chars": 0})

        return json.dumps({"offloaded": offloaded, "saved_chars": saved})

    def on_session_end(self, session_id: str, messages: list | None = None,
                       state_db_path: str | None = None) -> None:
        """从 PG 消息生成 session 摘要；PG 不可用时回退到 j/ 路径。"""
        from . import session_summary

        def fallback() -> None:
            try:
                if self._pg_pool is not None:
                    session_summary.on_session_end_handler(
                        session_id, messages=messages, state_db_path=state_db_path,
                        pool=self._pg_pool,
                    )
                else:
                    session_summary.on_session_end_handler(
                        session_id, messages=messages, state_db_path=state_db_path,
                    )
            except Exception as e:
                logger.warning("session_summary fallback 失败: %s", _safe_err(e))

        if not session_id:
            logger.warning("on_session_end 跳过空 session_id")
            return

        try:
            pg = self._pg or self.pg
            with _lease_pg_store(pg) as conn:
                if not conn:
                    logger.warning("on_session_end PG 不可用，回退 j/ 摘要路径")
                    fallback()
                    return

                cur = conn.cursor()
                session_pattern = f"%{session_id}%"

                # 当前 topics 映射：insert_card(category) 写入 note_ref，source_id 写入 topic_id。
                cur.execute(
                    "SELECT 1 FROM topics "
                    "WHERE note_ref = %s AND topic_id LIKE %s "
                    "AND COALESCE(status, 'active') NOT IN ('archived', 'deleted') LIMIT 1",
                    ("session_summary", session_pattern),
                )
                if cur.fetchone():
                    logger.debug("session %s 已有摘要，跳过", session_id)
                    return

                # PG 是摘要主数据源；2026-08-08 融合：v3_messages 已归档，改查 conversation_stream（按 session_id）
                try:
                    cur.execute(
                        "SELECT content, role, timestamp FROM conversation_stream "
                        "WHERE session_id = %s AND role IN ('user','assistant') "
                        "ORDER BY timestamp",
                        (session_id,),
                    )
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT content, role, timestamp FROM conversation_stream "
                        "WHERE session_id = %s ORDER BY timestamp",
                        (session_id,),
                    )
                pg_rows = cur.fetchall()
        except Exception as e:
            logger.warning("on_session_end PG 查询失败，回退 j/ 摘要路径: %s", _safe_err(e))
            fallback()
            return

        # 容错: 部分驱动/mock 下 fetchall() 可能返回 None, 视作空集走 fallback。
        if not pg_rows and not messages:
            logger.info("session %s PG 无完整轮次且无内存消息，回退 j/ 摘要路径", session_id)
            fallback()
            return
        source_messages = [
            {"content": row[0], "role": row[1]}
            for row in (pg_rows or [])
        ]
        # 兼容 hook 尚未把消息 flush 到 PG 的情况，但 PG 查询本身始终优先。
        if not source_messages and messages:
            source_messages = messages

        dialogue: list[str] = []
        user_count = assistant_count = 0
        for message in source_messages:
            role = str(message.get("role", "") or "").lower()
            if role not in ("user", "assistant"):
                continue
            content = message.get("content", "")
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            content = str(content or "").strip()
            if not content:
                continue
            if role == "user":
                user_count += 1
                label = "用户"
            else:
                assistant_count += 1
                label = "助手"
            dialogue.append(f"[{label}]: {content}")

        turn_count = min(user_count, assistant_count)
        if turn_count < 3:
            logger.info("session %s 完整轮次不足 3，回退 j/ 摘要路径", session_id)
            fallback()
            return

        try:
            llm = LLMClient(self.config)
            # prompts.session_summary 可在 config.yaml 覆盖；缺失则走 SESSION_PROMPT 默认
            system_prompt = _resolve_prompt(self.config, "session_summary", session_summary.SESSION_PROMPT)
            result = llm.chat(
                system=system_prompt,
                messages=[{
                    "role": "user",
                    "content": "【对话原文】\n" + "\n\n".join(dialogue),
                }],
                temperature=0.3,
            )
            result = str(result or "").strip()
            if len(result) < 50:
                logger.warning("on_session_end LLM 输出过短: %d chars", len(result))
                return

            def section(name: str) -> str:
                match = re.search(
                    rf"##\s*{re.escape(name)}\s*\n([\s\S]*?)(?=\n##\s|\Z)", result,
                )
                return match.group(1).strip() if match else ""

            title_section = section("标题")
            title = title_section.splitlines()[0].strip()[:40] if title_section else ""
            summary_text = section("叙事摘要") or result
            decisions = [
                line.lstrip("-• ").strip()
                for line in section("关键决策").splitlines()
                if line.lstrip().startswith(("-", "•"))
            ]
            topics = [
                line.lstrip("-• ").strip()
                for line in section("待办").splitlines()
                if line.lstrip().startswith(("-", "•"))
            ]
            body = json.dumps({
                "summary": summary_text,
                "key_decisions": decisions,
                "key_topics": topics,
                "turn_count": turn_count,
            }, ensure_ascii=False)
            pg.insert_card(
                f"session_summary/{session_id}",
                title or f"Session {session_id}",
                body,
                "session_summary",
                tags=["auto", "session_summary"],
                on_topics_commit=self._notify_topic_recall_invalidation,
            )
            logger.info("已写入 PG session_summary: %s", session_id)
        except Exception as e:
            logger.warning("on_session_end 摘要生成或写入失败: %s", _safe_err(e))

    @staticmethod
    def get_v3_data_root() -> str:
        """返回 v3-core 数据根目录 (HERMES_HOME 外路径)

        backup_paths 驱动: 让 hermes backup 能包含 v3 数据.
        """
        try:
            from .config import resolve_config, _resolve_prompt
            cfg = resolve_config()
            base = _extract_base_path(cfg)
            if base:
                return str(base)
        except Exception:
            pass
        return str(Path.home() / ".v3-core" / "profiles" / "default")

    def append_journal(self, text: str) -> None:
        """向迹层追加一条记录

        on_delegation 驱动: 子代理协作记录.
        """
        try:
            from datetime import datetime as dt
            from pathlib import Path
            now = dt.now()
            base = self._get_base_path()
            j_dir = base / "j" / f"journal_{now.strftime('%Y%m%d')}"
            j_dir.mkdir(parents=True, exist_ok=True)
            entry = f"---\ncreated_at: {now.isoformat()}\nsource: v3core.append_journal\n---\n\n{text}\n"
            (j_dir / f"{now.strftime('%H%M%S')}_{int(now.timestamp())}.md").write_text(entry, encoding="utf-8")
        except Exception as e:
            logger.warning("append_journal 失败: %s", _safe_err(e))

    # ── 身份核心 ──
    # 身份块由 E1 每天写印时一并生成；实时注入路径只读现成文件。
    # 文件缺失时才退化为最新印原文截断，保证旧数据/首次升级仍可用。
    _IDENTITY_BLOCK_MAX_LEN = 500

    def _compress_yin_to_identity(self) -> str:
        """DEPRECATED: 旧身份注入路径，已被 MemoryInjector 替代。
        保留作向后兼容，新代码请调用 build_memory_context('')。

        身份块由 E1 每天写时生成（identity_block.md），本方法纯读；
        文件缺失时截断最新印兜底。
        """
        base_path = self._get_base_path()
        identity_path = base_path / "identity_block.md"

        # 正常路径只读 E1 生成的身份块；mtime 变化时刷新进程内缓存。
        if identity_path.exists():
            try:
                identity_mtime = identity_path.stat().st_mtime
                if (
                    self._identity_block is not None
                    and identity_mtime <= self._identity_block_mtime
                ):
                    return self._identity_block

                block = identity_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
                self._identity_block = block
                self._identity_block_mtime = identity_mtime
                logger.info("身份核心已读取 (来源=%s)", identity_path.name)
                return block
            except Exception as e:
                logger.warning("身份块读取失败: %s", _safe_err(e))
                return self._identity_block or ""

        if self._identity_block is not None:
            return self._identity_block

        logger.warning("E1 未生成身份块，用截断兜底")
        y_dir = base_path / "y"
        if not y_dir.exists():
            return ""

        try:
            yin_files = sorted(
                y_dir.glob("y_*.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not yin_files:
                return ""

            latest = yin_files[0]
            text = latest.read_text(encoding="utf-8", errors="replace")
            # 优先从"我是谁"段落开始截取。
            match = re.search(r'(?:^|\n)\s*#+\s*我是谁[\s\S]*', text)
            block = match.group(0) if match else text
            if len(block) > self._IDENTITY_BLOCK_MAX_LEN:
                cut = block[:self._IDENTITY_BLOCK_MAX_LEN]
                boundary = max(cut.rfind('\n'), cut.rfind('。'))
                if boundary >= self._IDENTITY_BLOCK_MAX_LEN * 0.6:
                    cut = cut[:boundary + 1]
                block = cut.rstrip() + "\n（截断自印原文，500 字以内）"

            self._identity_block = block
            self._identity_block_mtime = 0.0
            logger.info("身份核心使用截断兜底 (%d 字, 来源=%s)", len(block), latest.name)
            return block
        except Exception as e:
            logger.warning("身份核心截断兜底失败: %s", _safe_err(e))
            return self._identity_block or ""

    # ── 系统态势总览（手帐机制 C） ──
    # 态势总览由 E1 写印后顺带生成（situation_overview.md），本方法纯读。
    # 与身份块同模式：mtime 变化时刷新缓存；文件缺失则返回空串（不注入，静默）。
    # 注入格式: "## 系统态势\n{content}" — 与身份块的"## 我是谁"段平级。
    def _read_situation_overview(self) -> str:
        """读取 E1 生成的系统态势总览（手帐机制 C：prefetch 固定注入）

        Returns:
            "## 系统态势\n{content}" — 文件存在且 mtime 变化时。
            "" — 文件不存在或读取失败时（静默，不阻塞注入链路）。
        """
        base_path = self._get_base_path()
        situation_path = base_path / "situation_overview.md"

        if not situation_path.exists():
            # 文件缺失 → 静默返回空串（首次升级/未跑过 E1 的正常情况）
            return ""

        try:
            situation_mtime = situation_path.stat().st_mtime
            # mtime 未变 → 直接返回进程内缓存（毫秒级）
            if (
                self._situation_overview is not None
                and situation_mtime <= self._situation_overview_mtime
            ):
                return self._situation_overview

            block = situation_path.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
            if not block:
                return ""

            # 注入格式: 与身份块的"## 我是谁"平级
            formatted = "## 系统态势\n" + block
            self._situation_overview = formatted
            self._situation_overview_mtime = situation_mtime
            logger.info("系统态势总览已读取 (来源=%s, %d 字)", situation_path.name, len(formatted))
            return formatted
        except Exception as e:
            logger.warning("系统态势总览读取失败: %s", _safe_err(e))
            return self._situation_overview or ""

    # ── 卡库操作 ──

    def store_card(self, category: str, title: str, content: str,
        tags: list[str] | None = None, **kwargs) -> CardResult:
        """写一张主动记忆卡 — P2a (2026-09-09) clean-boundary 路由.

        新契约 (branch p2a/active-memory-clean-boundary-20260909):

        * **唯一真值写入路径**: ``ActiveMemoryWriter`` (来自
          :mod:`v3core.active_memory_store`). 不再调
          ``DeepStore.write_card_strict`` / ``PgEmbedStore.insert_card``
          / ``SqliteCardStore.write_card`` / ``_sync_card_to_topics`` —
          这些 legacy 主动写接口在新的 supported 面上完全停用.
        * **caller 提供的 ``source_id``/``memory_id`` 原值透传**:
          memory_id 与 source_id 同语义, 显式非空 → 原值优先; 都不
          传 → 由 ``ActiveMemoryWriter`` 走内容感知默认 id (sha256 of
          canonical JSON). ``source`` / ``source_j_ids`` 等内部
          provenance 字段原值透传, **不**伪造 QA id.
        * **不初始化 ``self.store`` (legacy DeepStore)**: 旧版入口先
          触发 ``self.store`` lazy 创建. 新版只走 ActiveMemoryWriter,
          不去碰 DeepStore; legacy 路径仍在, 但本方法不再依赖.
        * **派生侧 warning 处理**: ActiveMemoryWriter 已自带
          DERIVED_WARNING / DEDUPLICATED / DURABLE_COMMITTED /
          DURABLE_FAILED 状态机. 本方法只做 ``MemoryWriteResult`` →
          ``CardResult`` 的字段翻译, 不引入第二份状态计算.
        * **CardResult 外部 shape 保持不变**: success / path / error /
          card / source_id / durable / durable_store / status / warnings
          全部填好, 老 caller 无感. ``durable_store`` 对齐
          ``"explicit_memories"`` (writer 的 canonical 真值位置).
        """
        # Forward caller identities separately; the writer owns conflict semantics.
        source_id = kwargs.pop("source_id", "") or ""
        memory_id = kwargs.pop("memory_id", "") or ""
        receiver_id = memory_id or source_id or ""

        # P2a clean boundary: 唯一写入入口.
        from .active_memory_store import ActiveMemoryWriter

        # 复用既有的 PgPool (有就传 pool, 没有就退到 pg). 不开第二份 pool.
        pool = getattr(self, "_pg_pool", None)
        pg = getattr(self, "_pg", None)
        if pg is None:
            try:
                pg = self.pg
            except Exception:
                pg = None

        try:
            writer = ActiveMemoryWriter(pool=pool, pg=pg, config=self.config)
        except Exception as _w_e:
            # 无法构造 writer → 立刻 fail-closed, 不写任何东西,
            # 不去碰 legacy DeepStore.
            return CardResult(
                success=False,
                durable=False,
                status="DURABLE_FAILED",
                durable_store="explicit_memories",
                source_id=receiver_id,
                error=f"active memory writer unavailable: {_safe_err(_w_e)}",
                warnings=[],
            )

        # ActiveMemoryWriter.create 内部已做完 canonical id 派生 /
        # ON CONFLICT DO NOTHING / 回读比对 / 嵌入 (post-commit).
        # ``memory_id`` 与 ``source_id`` 等价, 显式非空 → 原值优先.
        write_kwargs: dict[str, Any] = {}
        if memory_id:
            write_kwargs["memory_id"] = memory_id
        if source_id:
            write_kwargs["source_id"] = source_id
        if tags:
            write_kwargs["tags"] = list(tags)
        # provenance: source / source_j_ids / when / where / who / why /
        # confidence / observation_count — 原值透传给 writer, 作为
        # ``provenance`` jsonb 落表. 不参与 canonical id.
        prov: dict[str, Any] = {}
        supplied_prov = kwargs.get("provenance")
        if isinstance(supplied_prov, dict):
            prov.update(supplied_prov)
        for k in ("source", "source_j_ids", "when", "where",
                  "who", "why", "confidence", "observation_count"):
            v = kwargs.get(k)
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            prov[k] = v
        if prov:
            write_kwargs["provenance"] = prov

        try:
            mw_result = writer.create(
                category=category,
                title=title,
                content=content,
                **write_kwargs,
            )
        except Exception as _w_run_e:
            # writer 抛了未捕获异常 — 显式 fail-closed, 不写 SQLite /
            # 不走 legacy. MemoryWriteResult.status 已经处理了
            # DURABLE_FAILED 的常规失败路径, 这里是兜底.
            logger.warning("store_card -> ActiveMemoryWriter 未捕获异常: %s", _safe_err(_w_run_e))
            return CardResult(
                success=False,
                durable=False,
                status="DURABLE_FAILED",
                durable_store="explicit_memories",
                source_id=receiver_id,
                error=f"active memory writer raised: {_safe_err(_w_run_e)}",
                warnings=[],
            )

        # MemoryWriteResult → CardResult 翻译. CardResult 外部 shape
        # 完全保持; ``durable_store`` 显式标注 ``explicit_memories``
        # (P2a clean boundary 的真值落点), 让工具层/api_delete/审计
        # 都能一眼识别 "走的是新 writer, 不是 legacy DeepStore".
        status = str(mw_result.status or "DURABLE_FAILED")
        success = bool(mw_result.success)
        durable = bool(mw_result.durable)
        warnings: list[str] = list(mw_result.warnings or [])

        # CardResult.path: 旧 caller 期望的 "文件路径" 字段. P2a
        # 新真值路径不再落文件系统; 用 memory_id / source_id 作为
        # 逻辑路径占位 (与 recall_pool / inject 链路一致). 旧 caller
        # 只把它当 receipt 字段, 不再 unlink, 不再当文件读.
        logical_path = (
            f"explicit_memories/{mw_result.memory_id}"
            if mw_result.memory_id else ""
        )

        # 把 MemoryRecord (writer 落表后回读的行) 翻译为 card dict,
        # 字段名与 DeepCard.to_dict() 对齐, 旧 caller 无感.
        card_dict: dict | None = None
        rec = mw_result.record
        if isinstance(rec, dict):
            card_dict = {
                "filename": rec.get("memory_id") or "",
                "category": rec.get("category") or category,
                "date": (rec.get("created_at") or "")[:10] if rec.get("created_at") else "",
                "title": rec.get("title") or title,
                "content": rec.get("content") or content,
                "tags": list(rec.get("tags") or []),
                "source": (rec.get("provenance") or {}).get("source", "active_memory"),
                "path": logical_path,
                "embedding": rec.get("embedding"),
                "when": (rec.get("provenance") or {}).get("when", ""),
                "where": (rec.get("provenance") or {}).get("where", ""),
                "who": (rec.get("provenance") or {}).get("who", ""),
                "why": (rec.get("provenance") or {}).get("why", ""),
                "source_j_ids": (rec.get("provenance") or {}).get("source_j_ids", []),
                "confidence": (rec.get("provenance") or {}).get("confidence", 0.3),
                "observation_count": (rec.get("provenance") or {}).get("observation_count", 1),
            }

        return CardResult(
            success=success,
            path=logical_path,
            error=str(mw_result.error or "") if not success else "",
            card=card_dict,
            source_id=str(mw_result.source_id or receiver_id or ""),
            durable=durable,
            durable_store="explicit_memories",
            status=status,
            warnings=warnings,
        )

    def _sync_card_to_topics(self, category: str, title: str, content: str,
                             tags: list[str] | None = None) -> list[str]:
        """2026-08-08 r7: 主动记忆卡同步进 topics 表 (召回池)。

        手账/主动写卡 (v3_store) 是用户显式想记住的东西 — 观察者被动提炼
        可能漏掉。同步进 topics 后, 主动记忆与被动提炼统一召回。

        P2a (2026-09-09): 这是 PG 真值写入后的派生副作用. 边界规则:
          * **可选的 embedding 派生失败** → 收集为 warnings 返回,
            caller (``store_card``) 合并到 receipt 并升级 status 为
            ``DERIVED_WARNING``. PG 真值不被抹掉.
          * **硬失败 (no-PG / lease 拿不到 / commit 抛错 / notification
            失败)** → 仍抛出 RuntimeError, 由 caller 捕获并升级
            ``DERIVED_WARNING``. 这是 P0 修复: 旧版把所有派生失败都 swallow,
            让 caller 误以为"PG + topics 都成功了", 实际召回池可能半空.

        返回值: ``list[str]`` 派生侧 warnings (可空). 即使空 list 也走
        合并逻辑, caller 不依赖返回值做 hard-error 判断.
        """
        warnings: list[str] = []
        import hashlib as _hl
        # no-PG 必须抛 — caller 必须知道召回池没拿到这条卡
        if not self.pg or not self.pg.is_connected():
            raise RuntimeError(
                "topic derived sync: postgres unavailable or not connected"
            )
        # topic_id: sha256(category+title) 前缀 shou_ 区分主动记忆
        _seed = f"{category}:{title}".encode("utf-8")
        _tid = "shou_" + _hl.sha256(_seed).hexdigest()[:12]
        _emb = None
        _embed_cfg = safe_embed_cfg(self.config)
        if _embed_cfg is not None:
            try:
                from .embedding import call_embedding
                _emb = call_embedding(f"{title}\n{content}"[:2000], _embed_cfg)
            except ValueError:
                # 配置/调用契约错误 — 这是 contract violation, 必须抛
                raise
            except Exception as _e:
                # 嵌入失败是**可选派生侧**问题, 不致命 — 仍允许无 emb
                # 写入 topics (旧契约保留, 不视为硬失败). 但 P2a 边界:
                # 不能 swallow, 必须让 caller 看见这条 warning, 升级
                # status 为 DERIVED_WARNING, 不抹掉 PG 真值.
                logger.debug("_sync_card_to_topics embedding 失败: %s", _safe_err(_e))
                warnings.append(
                    f"topic derived sync embedding failed: {_safe_err(_e)[:200]}"
                )
        _emb_str = "[" + ",".join(str(x) for x in _emb) + "]" if _emb else None
        # lease 拿不到 → 抛 (派生侧问题, caller 升级为 DERIVED_WARNING).
        # commit / cursor 抛错 → 同样冒泡. 不再 catch-and-log-and-return.
        with _lease_pg_store(self.pg) as conn:
            if not conn:
                raise RuntimeError(
                    "topic derived sync: postgres lease unavailable"
                )
            with conn.cursor() as cur:
                if _emb_str:
                    cur.execute(
                        "INSERT INTO topics (topic_id, title, summary, body, keywords, status, created_at, updated_at, embedding) "
                        "VALUES (%s, %s, %s, %s, %s, 'active', NOW(), NOW(), %s::vector) "
                        "ON CONFLICT (topic_id) DO UPDATE SET title=EXCLUDED.title, body=EXCLUDED.body, "
                        "updated_at=NOW(), embedding=EXCLUDED.embedding",
                        (_tid, title, content[:200], content, tags or [], _emb_str))
                else:
                    cur.execute(
                        "INSERT INTO topics (topic_id, title, summary, body, keywords, status, created_at, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, 'active', NOW(), NOW()) "
                        "ON CONFLICT (topic_id) DO UPDATE SET title=EXCLUDED.title, body=EXCLUDED.body, updated_at=NOW()",
                        (_tid, title, content[:200], content, tags or []))
            conn.commit()
            logger.info("store_card 同步 topics: %s (%s)", _tid, category)
        # 召回缓存失效: notification 失败也是派生侧问题, 必须抛给 caller
        # 升档为 DERIVED_WARNING, 不让召回池持有陈旧索引.
        self._notify_topic_recall_invalidation()
        return warnings

    def search_cards(self, query: str, category: str | None = None, limit: int = 10) -> list[dict]:
        """搜索碑卡"""
        from .recall_pool import recall_pool
        cfg = self.config
        embed_cfg = safe_embed_cfg(cfg)
        q_emb = None
        if embed_cfg is not None and query in _EMBED_CACHE:
            q_emb = call_embedding(query, embed_cfg, cache=True)
        index = self.store.get_index()
        files_meta = index.get("files", {})
        card_index = {}
        for rel, meta in files_meta.items():
            if category and meta.get("category") != category:
                continue
            card_index[rel] = meta
        rerank_cfg = _extract_rerank_cfg(cfg)
        # PG fail detection
        pg_connected = self.pg and self.pg.is_connected()
        if pg_connected:
            self._pg_was_connected = True
        pg_fail = self._pg_was_connected and not pg_connected
        
        hits, _ = recall_pool(query, card_index=card_index, pg=self.pg, q_emb=q_emb,
                           rerank_top_n=10 if rerank_cfg.get("endpoint") else None,
                           rerank_cfg=rerank_cfg, limit=limit,
                           pg_was_connected=self._pg_was_connected,
                           config=cfg,
                           sqlite_store=self.store.sqlite)

        result = [{
            "source_id": h.source_id,
            "title": h.title,
            "category": h.category,
            "content_preview": h.content_preview[:500],
            "tags": h.tags,
            "cosine": h.cosine,
            "rrf_score": h.rrf_score,
        } for h in hits]
        
        if pg_fail:
            result.append({"warning": "PG disconnected - search degraded to keyword only"})
        
        return result

    def get_status(self, category: str | None = None) -> dict:
        return self.store.status(category)

    def extract_from_session(self, raw_text: str, write: bool = False,
        experts: list[str] | None = None) -> dict:
        # 多专家模式: 同一段对话 → 多个专家 prompt 各产一张卡
        if experts:
            return self._extract_multi_expert(raw_text, experts, write=write)

        # 原通用逻辑 — P2a clean-boundary: extractor 必须以 ``store=None``
        # 调用, 这样不会经由 ``DeepStore`` 走任何 active 写路径.
        # P2a 之前, 这里传 ``store=self.store`` 让 extractor 内部直接
        # 走 ``DeepStore.write_card_strict`` / ``PgEmbedStore.insert_card``
        # 等 legacy 主动写接口 — 现在统一改由 :func:`V3Core.store_card`
        # 路由到 ``ActiveMemoryWriter``. ``store=None`` 是契约; 任何
        # extractor 调用都必须能看到 ``store`` 参数并遵守 ``None``
        # 语义 (不写, 只产 card dict).
        from .extract import extract_from_session as _extract_fn
        cards = _extract_fn(raw_text, self.config, store=None)
        if not write:
            return {"success": True, "count": len(cards), "cards": cards, "write": False}
        # P2a (2026-09-09): 按 strict 真值逐张写, 把每张的 CardResult 透传上去.
        # summary 字段 (durable_written / written / failed / partial_failure /
        # success) 让 caller 能识别"PG 部分失败"而不是混在一起装作"全部成功".
        files: list[dict] = []
        durable_written = 0
        failed = 0
        for card in cards:
            # P2a clean-boundary: **绝不**把 LLM card 自带的 ``source_id``
            # 透传给 ``store_card`` 作为 canonical source_id. canonical id
            # 必须在 ActiveMemoryWriter 内部从 (category, title, 完整 content,
            # tags) 重新派生, 保证 retry → 同 id → idempotent.
            #
            # 仍然透传 ``source`` / ``source_j_ids`` 等 provenance 字段
            # (若 caller 显式给了的话), 它们是 metadata, 不参与 canonical
            # id 计算. ActiveMemoryWriter 会把它们收进 ``provenance`` jsonb.
            card_kwargs: dict = {}
            _csrc = card.get("source")
            if _csrc:
                card_kwargs["source"] = _csrc
            _cjs = card.get("source_j_ids")
            if _cjs:
                card_kwargs["source_j_ids"] = _cjs
            result = self.store_card(
                card.get("category", ""),
                card.get("title", ""),
                card.get("content", ""),
                card.get("tags", []),
                **card_kwargs,
            )
            file_summary = result.to_dict()
            # 兼容老 caller: file_summary 仍带 success/path/card;
            # 新增 durable/source_id/durable_store/status/warnings.
            if result.durable:
                durable_written += 1
            else:
                failed += 1
            files.append(file_summary)
        # P2a (2026-09-09) partial_failure 口径: 只在 0<failed<count 时为
        # True. 全失败 (failed==count) 是 failure 但不是 partial —
        # caller 需要区分"部分失败"与"全军覆没"以便决策 (是否重试).
        partial_failure = 0 < failed < len(cards)
        # ``written`` 口径与 ``durable_written`` 对齐 — 只计真正落到 PG
        # 的卡. P2a 旧版的 ``written=len(files)`` 把所有尝试都算成 "已写"
        # 是 misleading (PG 失败也被记成 written=1). 现在 ``written`` 是
        # durable_written 的同义词, caller 一眼看出"有几张真值落了".
        written = durable_written
        # success 口径:
        #   * 所有尝试都成功 (failed == 0, durable_written > 0) → True
        #   * 至少一张失败 → False (不论其他几张是否 durable=True)
        #   * 没有任何尝试 (cards 空) → True (没失败就当成功)
        overall_success = failed == 0 and (
            durable_written > 0 or written == 0
        )
        return {
            "success": overall_success,
            "count": len(cards),
            "written": written,
            "durable_written": durable_written,
            "failed": failed,
            "partial_failure": partial_failure,
            "files": files,
        }

    def _extract_multi_expert(self, raw_text: str, experts: list[str],
                                write: bool = False) -> dict:
        """多专家模式: 对同一段 raw_text, 广播类别的 prompt 各调一次 LLM, 各产一张碑卡.
        专家名称在 EXTRACTOR_PROMPTS 中有定义才会被处理, 空列表或未知名的别名静静跳过.
        write=False 时仅返回 LLM 输出 (预览); write=True 时写盘.

        P2a (2026-09-09): 聚合字段 (count / cards_count / durable_written /
        written / failed / partial_failure / success) 与
        ``extract_from_session`` 对齐. ``llm_failed`` 不再混入 ``failed`` —
        LLM 调用失败是"输入没拿到", 与 PG 真值写入失败是两件事, caller 需
        要分别审计. ``written`` 现在只计 durable 成功 (旧版
        ``written=len(results)`` 把所有结果都算"已写", 是 misleading).
        """
        llm = LLMClient(self.config)

        results: list[dict] = []   # 预览结果 / 写卡结果
        cards_count = 0            # 写入成功的碑卡数 (result.success)
        durable_written = 0        # 真值落入 PG 的卡数 (result.durable)
        written = 0                # durable_written 的同义别名, 与
                                   # extract_from_session 对齐
        failed = 0                 # PG 写失败的卡数 (result.durable=False)
        llm_failed = 0             # LLM 调用失败的专家数 (与 failed 分开)
        experts_used: list[str] = []

        for expert_name in experts:
            prompt = EXTRACTOR_PROMPTS.get(expert_name)
            if not prompt:
                # 别名不在专家表 → 静静跳过
                continue
            experts_used.append(expert_name)

            user_msg = "从以下对话提取内容:\n\n" + raw_text
            try:
                # llm.chat(system, messages, temperature) — 全 positional 参
                content = llm.chat(
                    prompt,
                    [{"role": "user", "content": user_msg}],
                    temperature=0.3,
                )
            except Exception as e:
                from . import _safe_err
                logger.warning("专家[%s] LLM 调用失败: %s", expert_name, _safe_err(e))
                llm_failed += 1
                results.append({
                    "expert": expert_name,
                    "category": expert_name,
                    "success": False,
                    "error": _safe_err(e)[:200],
                })
                continue

            content = (content or "").strip()
            if not content:
                results.append({
                    "expert": expert_name,
                    "category": expert_name,
                    "success": True,
                    "content": "",
                })
                continue

            if not write:
                # 只预览 — 不写盘
                results.append({
                    "expert": expert_name,
                    "category": expert_name,
                    "success": True,
                    "content": content,
                })
                continue

            # 写盘 — 利用 store_card
            # store_card 内部已生成 b_{category}_{date}_{ms}-{safe}.md,
            # category = expert_name, 文件名自然带专家前缀
            result = self.store_card(
                expert_name,                                   # category → 文件名前缀
                f"专家提取: {expert_name}",   # title
                content,                                       # content
                tags=[expert_name, "expert-extract", "multi-expert"],
            )
            # P2a: 用 strict 真值计数 + 透传 receipt, 让 caller 能
            # 区分"durable 成功"和"PG 部分失败".
            if result.success:
                cards_count += 1
            if result.durable:
                durable_written += 1
                written += 1
            else:
                failed += 1
            results.append({
                "expert": expert_name,
                "category": expert_name,
                "success": result.success,
                "durable": result.durable,
                "durable_store": result.durable_store,
                "source_id": result.source_id,
                "status": result.status,
                "warnings": list(result.warnings),
                "path": result.path,
                "error": result.error,
                "content": content,
            })

        # P2a (2026-09-09) 聚合口径 — 与 ``extract_from_session`` 对齐:
        #   * partial_failure: PG 真值写入失败数 > 0 即 True (与 LLM 失败分开算)
        #   * success: failed == 0 且 llm_failed == 0 且至少一张 durable=True
        #     (若 experts_used 为空 → 没失败也算 True, 但 cards_count=0)
        # P2a (2026-09-09) partial_failure 口径: 只在 0<failed<count 时为
        # True. 全失败 (failed==count) 是 failure 但不是 partial —
        # caller 需要区分"部分失败"与"全军覆没". count 是 experts_used
        # 数 (不是 experts requested 数 — 别名未匹配的不算尝试).
        partial_failure = 0 < failed < len(experts_used)
        overall_success = (
            failed == 0
            and llm_failed == 0
            and (durable_written > 0 or len(experts_used) == 0)
        )
        return {
            "success": overall_success,
            "mode": "multi-expert",
            "experts_requested": list(experts),
            "experts_used": experts_used,
            "count": len(experts_used),
            "cards_count": cards_count,
            "written": written,
            "durable_written": durable_written,
            "failed": failed,
            "llm_failed": llm_failed,
            "partial_failure": partial_failure,
            "results": results,
            "message": (
                f"提取完成: {len(experts_used)} 位专家 → {cards_count} 张卡"
            ),
        }

    # ── 召回 ──

    def prefetch(self, query: str, limit: int = 5, fmt: str = "list", *, deadline=None):
        """Recall — topic 卡优先，不足时走印→topic→原始对话链式召回

        fmt:
          - "list"  (default): flat list of topic card hits
          - "chain": chain_recall dict with yin_context + entries

        Returns list[dict] when fmt='list', or dict when fmt='chain'.

        P1.3 + A2 admission: pool-owned outer-reader admission around the
        full recall path.  When the pool rejects, return a controlled
        empty result — do NOT fall through to the legacy unguarded
        fallback (the ``except Exception -> old path`` swallow would
        silently bypass the writer reserve and recreate the RED shape).
        Legacy/fake stores without ``try_acquire_outer_reader`` retain
        their old behavior through a fail-safe compatibility branch.
        """
        deadline = coerce_deadline(deadline)
        if deadline is not None:
            deadline.check(context="core prefetch")
        # P1.3 + A2 outer admission — minimal local seam: acquire just
        # before the recall_pool call below and release in a finally
        # scoped to that single call.  Rejection returns a controlled
        # empty/marker outcome that does NOT fall through to the legacy
        # unguarded fallback further down.  We do not restructure the
        # surrounding try/except to keep existing semantics intact.
        outer_token = None
        pool = getattr(self, "_pg_pool", None)
        if pool is not None and hasattr(pool, "try_acquire_outer_reader"):
            outer_token = pool.try_acquire_outer_reader()
            if outer_token is None:
                logger.debug(
                    "prefetch: outer admission rejected, returning empty recall"
                )
                return [] if fmt != "chain" else {
                    "type": "recall_pool",
                    "cards": [],
                    "count": 0,
                }
        try:
            from .recall_pool import recall_pool
            embed_cfg = safe_embed_cfg(self.config)
            q_emb = None
            # 2026-08-09: 首轮也算向量 — 原设计"query 在缓存才算"导致首轮查询
            # (用户问一次的问题) 永远只有关键词, QA 向量语义匹配(speech vs event)失效。
            # embedding 一次 ~100ms, 相对召回收益可接受。缓存仍生效(同 query 复用)。
            if embed_cfg is not None:
                try:
                    if deadline is None:
                        q_emb = call_embedding(query, embed_cfg, cache=True)
                    else:
                        deadline.check(context="query embedding")
                        q_emb = call_embedding(
                            query,
                            embed_cfg,
                            cache=True,
                            timeout=max(0.001, min(3.0, deadline.remaining())),
                            retries=0,
                        )
                except ValueError:
                    raise
                except PrefetchDeadlineExceeded:
                    raise
                except Exception as _qe:
                    logger.debug("prefetch: query embedding 失败, 走纯关键词: %s", str(_qe)[:80])
                    q_emb = None

            # 2026-09-02 P1.3.1: forward rerank cfg/top_n so this public path
            # also exercises the rerank caller (same contract as
            # prefetch_to_context_block).
            _rerank_cfg = _extract_rerank_cfg(self.config)
            _rerank_endpoint = _rerank_cfg.get("endpoint") if isinstance(_rerank_cfg, dict) else ""
            # P1.3 + A2 admission: own outer-reader token already admitted
            # above → tell recall_pool so the prefill worker reservation
            # does not double-count it; release the token in a LOCAL
            # try/finally around ONLY this recall_pool call (the rest of
            # the try-block below — _hit_dict / cards / fmt branching —
            # is left untouched so dedupe/PDE/legacy semantics remain
            # byte-for-byte identical).
            try:
                ret, _ = recall_pool(
                    query,
                    config=self.config,
                    pg=self.pg,  # 2026-08-09: 传 pg — 否则 keyword 段走 SQLite, QA 原文维不执行
                    q_emb=q_emb,
                    include_keyword=True,
                    # 2026-08-09: 补 include_card_vector=True — 原调用没传, recall_pool 默认 False
                    # → 向量分支 (TopicRecall + QA 原文向量检索) 整个没跑, 召回只有关键词
                    # → QA 原文 cos=0 只靠 ILIKE, RRF 被 topic 状态摘要压 (LGBTQ 查询 QA 排 17+ 名)
                    include_card_vector=True,
                    # 2026-08-06: 旧表(v3_cards/v3_messages/v3_effective)不再参与召回 —
                    # 用 recall_pool 默认 False (topics 是唯一检索索引)
                    limit=limit,
                    pg_was_connected=self._pg_was_connected,
                    core=self,
                    deadline=deadline,
                    rerank_top_n=30 if _rerank_endpoint else None,
                    rerank_cfg=_rerank_cfg,
                    # P1.3 + A2 follow-up: own outer-reader token already
                    # admitted above → tell recall_pool so the prefill
                    # worker reservation does not double-count it.
                    outer_admitted=outer_token is not None,
                )
            finally:
                # P1.3 + A2: outer token released at the seam.  Idempotent.
                if outer_token is not None:
                    outer_token.close()

            def _hit_dict(h):
                return {
                    "source_id": h.source_id,
                    "title": h.title,
                    "score": h.rrf_score,
                    "kind": h.kind,
                    "content_preview": (h.content_preview or "")[:200],
                }

            cards = [_hit_dict(h) for h in ret]
            if fmt == "chain":
                # 2026-08-06: _chain_yin_segments 退役 — v3_effective yin_segment 是
                # 观察者 v2 前死数据 (155 条不更新)。链式溯源走:
                #   v3_get_message_context (按 source_id 取原文) /
                #   prefetch.chain_recall (已接 topics+topic_entries 新架构)
                return {"type": "recall_pool", "cards": cards, "count": len(ret)}
            return cards
        except PrefetchDeadlineExceeded:
            raise
        except ValueError:
            raise
        except Exception as e:
            logger.warning("prefetch topic 召回失败，回退旧路: %s", _safe_err(e)[:100])
            # 旧路兜底
            from .prefetch import prefetch as _prefetch
            embed_cfg = safe_embed_cfg(self.config)
            q_emb = None
            if embed_cfg is not None:
                # 2026-09-04 P1.3.1 deadline-closure: the legacy fallback's
                # ``call_embedding`` was an unguarded blocking call that
                # could outlive the prefetch budget.  When a deadline is
                # supplied we honour it: pre-check the budget, clamp the
                # per-call timeout to ``max(0.001, min(3.0,
                # deadline.remaining()))``, force ``retries=0``, and re-check
                # after the call so a tight budget surfaces immediately.
                # ``deadline=None`` keeps the legacy ``cache=True`` call
                # byte-for-byte (existing behaviour).  ``PrefetchDeadlineExceeded``
                # propagates through the outer ``except PrefetchDeadlineExceeded:
                # raise`` chain — no broad catch swallows the signal.
                if deadline is not None:
                    deadline.check(context="core prefetch legacy fallback")
                    _legacy_timeout = max(0.001, min(3.0, deadline.remaining()))
                    q_emb = call_embedding(query, embed_cfg, cache=True,
                                           timeout=_legacy_timeout, retries=0)
                    deadline.check(context="core prefetch legacy fallback post-call")
                else:
                    q_emb = call_embedding(query, embed_cfg, cache=True)
            index = self.store.get_index()
            return _prefetch(
                query, limit, self.config, index.get("files", {}), self.pg, q_emb,
                pg_was_connected=self._pg_was_connected, fmt=fmt, core=self,
                deadline=deadline,
            )

    def prefetch_to_context_block(self, query: str, session_id: str = "",
                                    max_chars: int | None = None, *, deadline=None) -> str:
        """格式化召回为上下文块 — topic 卡优先

        session_id 传进来时自动判断是否新 session，新 session 追加尾巴原文补漏。

        2026-08-18 follow-up 统一注入预算 (设计：docs/provider-injection-budget-design-20260818.md):
          - caller 传入的 max_chars (非 None) 必须 override config 的 10000
            budget；作用于正常 embed/recall_pool 主路径，不只在 fallback 路径
            生效；
          - 选择策略保留：完整单元 (完整 topic 卡头+正文 / 完整 QA) 贪心放入,
            放不下整体跳过, 不切字符串 (无 `body[:N]` / `yin[:N]` 切片);
          - yin 不再走 `[:min(...)]` 平切; 完整 yin 放不下就跳过;
          - 返回值 (含 prefix / content / yin / graph hint) 计入同一 budget,
            最终 ≤ max_chars;
          - 兼容旧调用: max_chars=None / 缺参 → 走 config budget (旧行为).
        """
        deadline = coerce_deadline(deadline)
        if deadline is not None:
            deadline.check(context="core context")
        dedupe_context = None
        dedupe_snapshot = None
        legacy_given = None
        legacy_given_snapshot = None

        def _rollback_dedupe_state() -> None:
            """Undo only this invocation's session dedupe mutations on PDE."""
            if dedupe_context is not None and dedupe_snapshot is not None:
                dedupe = dedupe_context.injection_dedupe
                dedupe.clear()
                dedupe.update(dedupe_snapshot)
            if legacy_given is not None and legacy_given_snapshot is not None:
                legacy_given.clear()
                legacy_given.update(legacy_given_snapshot)

        # v4 fix: _tail_given_to 用 OrderedDict (LRU) 替换 set, maxlen=1000,
        # 防止长跑进程 (子代理/回放/test session) 累积几千个 session_id 占几十 MB。
        is_new = False
        effective_session_id = session_id or getattr(self, "_active_session_id", "")
        contexts = getattr(self, "_session_contexts", None)
        if effective_session_id and isinstance(contexts, dict):
            context = self.get_session_context(effective_session_id)
            if context is not None:
                dedupe_context = context
                dedupe_snapshot = dict(context.injection_dedupe)
                dedupe = context.injection_dedupe
                is_new = not bool(dedupe.get("tail_given"))
                # Provisional commit: PDE handlers restore the exact
                # snapshot; a successful call keeps this legacy-equivalent
                # dedupe state.
                dedupe["tail_given"] = True
        elif session_id:
            # Legacy V3Core.__new__ / fake-core compatibility only.
            from collections import OrderedDict
            given = getattr(self, "_tail_given_to", None)
            if given is None:
                given = OrderedDict()
                self._tail_given_to = given
            legacy_given = given
            legacy_given_snapshot = given.copy()
            if session_id not in given:
                is_new = True
                given[session_id] = True
                given.move_to_end(session_id)
                if len(given) > 1000:
                    given.popitem(last=False)
        try:
            embed_cfg = safe_embed_cfg(self.config)
            if deadline is None and getattr(self, "_runtime", None) is None and self._topic_recall is None:
                from .topic_recall import TopicRecall
                self._topic_recall = TopicRecall(embed_cfg, pool=getattr(self, "_pg_pool", None))
                self._topic_recall._ensure_loaded()
                # 2026-08-09 (W-5): 去掉冗余 count 查询 — _topic_recall_count 只被守护线程 (319行) 使用,
                # 此处初始化后从不更新, 且新主路径不走 TopicRecall, count 无消费方
            # 优化: 去掉 query in _EMBED_CACHE 前置条件 — 首轮也走 TopicRecall 快路径。
            # 原因: call_embedding 自带 10 分钟缓存，同 query 二次调用命中极快；
            #       若首轮因 query 不在缓存跳过 TopicRecall，会落到旧路 prefetch → chain_recall → _chain_fallback_file
            #       全文件扫描，几千文件 Python 循环 = 5-6s，贴 8s 预算线，波动即超时。
            # TopicRecall.match 内部本身有 try/except，embedding 失败会降级 (走 format_context 返回空)。
            # 2026-08-09: 主路径改走 recall_pool (与评测/prefetch 同链路) —
            # 原实现只走 TopicRecall 5 卡, QA 原文维/稀有词加权在生产注入完全不可用
            # (评测 63.6% 是 prefetch 链路的分数, 生产注入走老路 = 假实现风险)
            # ── P1.3 + A2 admission: outer reader seam (local try/finally
            # wrapping ONLY the recall_pool call below; the surrounding
            # outer try/except structure is left untouched). ──
            if embed_cfg is not None:
                # P1.3 + A2 outer admission — controlled reject returns
                # empty context and skips the recall_pool call entirely.
                outer_token = None
                pool = getattr(self, "_pg_pool", None)
                if pool is not None and hasattr(pool, "try_acquire_outer_reader"):
                    outer_token = pool.try_acquire_outer_reader()
                    if outer_token is None:
                        logger.debug(
                            "prefetch_to_context_block: outer admission rejected, returning empty context"
                        )
                        return ""
                try:
                    from .recall_pool import recall_pool
                    if deadline is None:
                        q_emb = call_embedding(query, embed_cfg, cache=True, timeout=3, retries=0)
                    else:
                        deadline.check(context="context query embedding")
                        q_emb = call_embedding(
                            query,
                            embed_cfg,
                            cache=True,
                            timeout=max(0.001, min(3.0, deadline.remaining())),
                            retries=0,
                        )
                    # 2026-09-02 P1.3.1: forward rerank cfg/top_n so the real PG
                    # keyword path actually exercises the rerank caller. When the
                    # endpoint is unset we pass rerank_top_n=None so recall_pool
                    # skips rerank — never silently disabling an explicit endpoint.
                    _rerank_cfg = _extract_rerank_cfg(self.config)
                    _rerank_endpoint = _rerank_cfg.get("endpoint") if isinstance(_rerank_cfg, dict) else ""
                    # P1.3 + A2: outer token released in finally around
                    # ONLY this recall_pool call.  The wider block is left
                    # untouched so existing dedupe/PDE/legacy semantics
                    # remain byte-for-byte identical.
                    try:
                        hits, _ = recall_pool(
                            query, config=self.config, pg=self.pg, q_emb=q_emb,
                            include_keyword=True, include_card_vector=True,
                            limit=8,  # 2026-08-12 B-1: 5→8 — 归因 B 类 15.5% (证据在候选 5-29 位被 limit=5 截断); 预算 MAX_INJECT_CHARS 兜底
                            core=self,
                            deadline=deadline,
                            rerank_top_n=30 if _rerank_endpoint else None,
                            rerank_cfg=_rerank_cfg,
                            # P1.3 + A2 follow-up: own outer-reader token
                            # already admitted above → tell recall_pool so
                            # the prefill worker reservation does not
                            # double-count it.
                            outer_admitted=outer_token is not None,
                        )
                    finally:
                        if outer_token is not None:
                            outer_token.close()
                    if hits:
                        parts: list[str] = []
                        # 2026-08-09 (t6 围栏): 注入引导段 — 只做中性描述 + 诚实性约束
                        # 2026-08-11 修正: 原"可能包含答案线索，请从中寻找"是评测引导词(评测集答案必然在历史里=泄露测试结构),
                        # 生产=幻觉放大器(记忆里没有答案时逼模型硬找=编造)。改为通用原则: 信息不足如实说明。
                        # 2026-08-09 (C-2 修复): 引导段只在有实际内容时输出 — 全空时避免"只有引导没内容"脏输出
                        # 2026-08-16 config 化: inject.max_chars (默认 10000 = v5 实测; 甲骨文硬件可调小)
                        config_max = int((self.config.get("inject") or {}).get("max_chars", 10000))
                        # 2026-08-18 follow-up FU1: caller max_chars override
                        # 必须作用于此 embed/recall_pool 主路径 (不止 fallback)。
                        # 设计: 完整单元走"放过就放过, 放不下整体跳过"。
                        MAX_INJECT_CHARS = max_chars if max_chars is not None else config_max
                        # v1.5 维度保底预算 (2026-08-15 用户设计): 每路独立保底配额,
                        # 互不挤占 — QA 精确匹配永远有空间 (museum 案例: 证据 rank1
                        # 被 anchor 预算吃掉的根因 = 全局顺序填充, 先到先得)。
                        # 保底: qa 1500 / anchor 2000 / topic 1000 / yin 500 (合计 5000 ≤ 6000)
                        # 弹性: 剩余按优先级 qa > anchor > topic > yin
                        #
                        # 2026-08-18 follow-up FU1 修正: 当 caller 用了 max_chars override
                        # (例如 500), 跳过数量/floor 软预算 — 直接走硬上限，避免吐超预算
                        # 仍以"floor=2000"为先制造伪合规。floor 仍适用旧行为 (无 override)。
                        if max_chars is not None:
                            QA_QUOTA = ANCHOR_QUOTA = TOPIC_QUOTA = YIN_QUOTA = MAX_INJECT_CHARS
                            QA_FLOOR = ANCHOR_FLOOR = TOPIC_FLOOR = 0
                        else:
                            QA_QUOTA, ANCHOR_QUOTA, TOPIC_QUOTA, YIN_QUOTA = 2000, 2000, 2000, 500
                            QA_FLOOR = ANCHOR_FLOOR = TOPIC_FLOOR = 2000
                        anchor_parts: list[str] = []
                        qa_parts: list[str] = []
                        topic_parts: list[str] = []
                        try:
                            anchor_parts = self._anchor_expand(query, embed_cfg, q_emb, deadline=deadline)
                        except ValueError:
                            raise
                        except PrefetchDeadlineExceeded:
                            raise
                        except Exception:
                            pass
                        for h in hits[:8]:
                            content = (h.content or h.content_preview or "").strip()
                            if not content:
                                continue
                            if h.kind == 'topic':
                                # v2: QA 引用回填时间戳 — 主题卡 body 的 [5904] → [5904 @2023-04-20]
                                topic_parts.append(
                                    f"━━━ {h.title} ━━━\n{self._attach_qa_timestamps(content, deadline=deadline)}")
                            elif h.kind == 'yin':
                                # yin 走独立完整段落通道；不混入 QA 配额，避免重复并抢占 QA 预算。
                                # 最终是否放入由下方完整 unit 预算统一决定，绝不切片。
                                continue
                            elif h.kind == 'active_memory':
                                # P2a (2026-09-09) clean-boundary 注入格式:
                                # 主动记忆召回以 ``[主动记忆] <title>`` + 完整
                                # content 进入预算, **不**走 topic / QA 通道,
                                # **不**额外申请预算. 仍由下方完整 unit /
                                # MAX_INJECT_CHARS 统一约束 — 完整 unit 贪心
                                # 放入, 放不下整段跳过 (不切片). caller 期望
                                # 完整正文, 所以这里不复用 ``_attach_qa_timestamps``
                                # 或 topic 头/尾格式.
                                qa_parts.append(
                                    f"[主动记忆] {h.title}\n{content}")
                            else:
                                qa_parts.append(f"[{h.kind}] {content}")
                        # 2026-08-16 数量控制 v3 (混合): 数量 N + 每维保底字符 FLOOR 双约束 (先到先停)
                        # 微集实测: 纯数量 6/6/3=61.0%, 4/4/4=62.0%, 均低于字符配额 63.0%
                        # — 纯数量下大卡仍吃总预算(4张大卡=8000)挤掉 QA 兜底。
                        # 混合: 小单元数量主导(4张卡), 大单元保底兜底(每维≥2000字符)。
                        _inj = self.config.get("inject") or {}
                        N_QA = int(_inj.get("qa", 4))
                        N_ANCHOR = int(_inj.get("anchor", 4))
                        N_TOPIC = int(_inj.get("topic", 4))
                        prefix = "[以下为与当前话题相关的过往对话/笔记片段；若信息不足，请如实说明，不要编造]"
                        sep = "\n\n"
                        # yin 是独立记忆维度：先取完整段落，再从总预算预留完整 unit。
                        # 两段整体放不下时退到一段；单段仍放不下则整块跳过，绝不切片。
                        yin_block = self._recall_yin_segments(query, embed_cfg, limit=2, q_emb=q_emb, deadline=deadline)
                        if yin_block and len(prefix) + len(sep) + len(yin_block) > MAX_INJECT_CHARS:
                            yin_block = self._recall_yin_segments(query, embed_cfg, limit=1, q_emb=q_emb, deadline=deadline)
                        if yin_block and len(prefix) + len(sep) + len(yin_block) > MAX_INJECT_CHARS:
                            logger.debug(
                                "prefetch_to_context_block: 单个完整 yin 块也超上限 (yin_len=%d, max=%d)",
                                len(yin_block), MAX_INJECT_CHARS,
                            )
                            yin_block = ""
                        yin_reserve = len(yin_block) + len(sep) if yin_block else 0
                        # 选择阶段就预留最终输出中的 prefix、完整 yin unit 和每个内容
                        # unit 前的分隔符；不能等选完后才二次跳过大卡。
                        budget = MAX_INJECT_CHARS - len(prefix) - yin_reserve
                        unit_sep_cost = len(sep)
                        seen_content_parts: set[str] = set()
                        c_qa: list[str] = []
                        c_anchor: list[str] = []
                        c_topic: list[str] = []
                        # 2026-08-18 follow-up FU3: 主循环用 selected 索引集
                        # 记录实际已选候选 (continue 跳过的候选不被记录),
                        # 弹性补充按 selected 排除已选项 — 避免主循环
                        # 选了小卡后, 弹性 fallback 又从 pool[len(cur):]
                        # 把同一卡加一次的重复 bug.
                        sel_qa: set[int] = set()
                        sel_anchor: set[int] = set()
                        sel_topic: set[int] = set()
                        # 贪心放入 — 整条 topic/anchor/qa 块作为一个 unit, 放不下就跳,
                        # 不切 body. (旧 floor 软预算只在 max_chars 未 override 时保留。)
                        #
                        # 2026-08-18 follow-up FU2: 超预算/超过 floor 的当前候选必须
                        # `continue` 跳过 (而不是 `break` 整个循环), 后续候选继续尝试.
                        # 仅"数量上限"才允许 `break`. 主循环对实际入选候选
                        # 记录其 pool 索引到 sel_* 集合, 弹性补充按 sel_*
                        # 显式排除已选项, 不会重复添加同一字符串.
                        for i, p in enumerate(qa_parts):
                            if len(c_qa) >= N_QA:
                                break
                            if p in seen_content_parts:
                                continue
                            if budget - (len(p) + unit_sep_cost) < 0:
                                continue  # 超预算 → 跳过本候选, 继续试下一个
                            if QA_FLOOR and sum(len(x) for x in c_qa) + len(p) > QA_FLOOR:
                                continue  # 超 floor → 跳过本候选, 继续试下一个
                            c_qa.append(p)
                            seen_content_parts.add(p)
                            sel_qa.add(i)
                            budget -= len(p) + unit_sep_cost
                        for i, p in enumerate(anchor_parts):
                            if len(c_anchor) >= N_ANCHOR:
                                break
                            if p in seen_content_parts:
                                continue
                            if budget - (len(p) + unit_sep_cost) < 0:
                                continue  # 超预算 → 跳过本候选, 继续试下一个
                            if ANCHOR_FLOOR and sum(len(x) for x in c_anchor) + len(p) > ANCHOR_FLOOR:
                                continue  # 超 floor → 跳过本候选, 继续试下一个
                            c_anchor.append(p)
                            seen_content_parts.add(p)
                            sel_anchor.add(i)
                            budget -= len(p) + unit_sep_cost
                        for i, p in enumerate(topic_parts):
                            if len(c_topic) >= N_TOPIC:
                                break
                            if p in seen_content_parts:
                                continue
                            if budget - (len(p) + unit_sep_cost) < 0:
                                continue  # 超预算 → 跳过本候选, 继续试下一个 (e.g. 大卡 → 小卡仍可入)
                            if TOPIC_FLOOR and sum(len(x) for x in c_topic) + len(p) > TOPIC_FLOOR:
                                continue  # 超 floor → 跳过本候选, 继续试下一个
                            c_topic.append(p)
                            seen_content_parts.add(p)
                            sel_topic.add(i)
                            budget -= len(p) + unit_sep_cost
                        # 弹性: 剩余预算按优先级补 (qa → anchor → topic).
                        # 2026-08-18 follow-up FU3: 不能用 pool[len(cur):] —
                        # pool/cur 是不同列表, len(cur) 不等于 pool 中实际
                        # 已选位置, 大卡 skip 后会把同一个已选小卡重复加入.
                        # 正确做法: 用 selected 索引集 (sel_*) 显式排除已选项,
                        # 并对超预算候选 continue (不阻断后续更小的候选).
                        if budget > 200:
                            for pool, cur, sel in [
                                (qa_parts, c_qa, sel_qa),
                                (anchor_parts, c_anchor, sel_anchor),
                                (topic_parts, c_topic, sel_topic),
                            ]:
                                for i, p in enumerate(pool):
                                    if i in sel:
                                        continue  # 排除主循环已选
                                    if p in seen_content_parts:
                                        continue  # 跨维度精确去重
                                    if budget - (len(p) + unit_sep_cost) < 0:
                                        continue  # 超预算 → 跳过本候选, 继续试下一个
                                    cur.append(p)
                                    sel.add(i)
                                    seen_content_parts.add(p)
                                    budget -= len(p) + unit_sep_cost
                        content_parts = c_qa + c_anchor + c_topic
                        if not content_parts and not yin_block:
                            return ""
                        # 选择阶段已经把 prefix/分隔符/完整 yin unit 计入 budget；这里保留
                        # MAX_INJECT_CHARS 作为最终拼接的硬上限。
                        planned_parts = [prefix] + content_parts
                        chosen: list[str] = []
                        running_len = 0
                        for unit in planned_parts:
                            unit_len = len(unit)
                            sep_cost = len(sep) if chosen else 0
                            if running_len + sep_cost + unit_len > MAX_INJECT_CHARS:
                                # 完整单元超过剩余预算 → 跳过, 不切字符串
                                logger.debug(
                                    "prefetch_to_context_block: 跳超额 unit (unit_len=%d, remain=%d)",
                                    unit_len, MAX_INJECT_CHARS - running_len,
                                )
                                continue
                            chosen.append(unit)
                            running_len += sep_cost + unit_len
                        if not chosen or (all(u == prefix for u in chosen) and not yin_block):
                            return ""
                        block = sep.join(chosen)
                        # yin: 已在选择阶段预留，作为一个完整 unit 放入；不再二次计算/切片。
                        if yin_block:
                            sep_cost = len(sep) if block else 0
                            if running_len + sep_cost + len(yin_block) <= MAX_INJECT_CHARS:
                                block = block + sep + yin_block
                                running_len += sep_cost + len(yin_block)
                            else:
                                logger.debug(
                                    "prefetch_to_context_block: 预留后的 yin 块仍无法放入 (yin_len=%d, remain=%d)",
                                    len(yin_block), MAX_INJECT_CHARS - running_len,
                                )
                        # 2026-08-26: 图谱关联提示已砍 (三轮消融 A−B=+0.010 p≈0.79 不显著,
                        # 语义图与主召回结构性冗余) — 见 docs/graph-ablation-20260825.md。
                        # 表 v3_graph_edges/nodes 保留离线分析用, 召回链路不再注入。
                        return block
                    logger.debug("prefetch_to_context_block: recall_pool 空, 直接返回空")
                    return ""
                except PrefetchDeadlineExceeded:
                    raise
                except ValueError:
                    raise
                except Exception as _me:
                    logger.debug("prefetch_to_context_block recall_pool 失败, 落旧路: %s", str(_me)[:100])
        except PrefetchDeadlineExceeded:
            _rollback_dedupe_state()
            raise
        except ValueError:
            raise
        except Exception:
            pass
        # 旧路兜底
        try:
            cfg = self.config
            embed_cfg = safe_embed_cfg(cfg)
            q_emb = None
            if embed_cfg is not None:
                if deadline is None:
                    q_emb = call_embedding(query, embed_cfg, cache=True)
                else:
                    deadline.check(context="legacy context embedding")
                    q_emb = call_embedding(
                        query, embed_cfg,
                        cache=True,
                        timeout=max(0.001, min(3.0, deadline.remaining())),
                        retries=0,
                    )
            index = self.store.get_index()
            return prefetch_to_context_block(
                query, 5, cfg, index.get("files", {}), self.pg, q_emb,
                pg_was_connected=self._pg_was_connected, is_new_session=is_new,
                core=self, max_chars=max_chars, deadline=deadline,
            )
        except PrefetchDeadlineExceeded:
            _rollback_dedupe_state()
            raise

    def _attach_qa_timestamps(self, body: str, conn: Any = None, *, deadline=None) -> str:
        """v2 时间戳回填 (2026-08-14) — 主题卡 body 的 QA id 引用附加日期

        conv-50:0 案例: body 引用 [5904] 但无日期 → 模型看到消息文本无法做时间推断。
        代码级修复 (不赌 LLM 提炼): [5904] → [5904 @2023-04-20]。
        失败/无引用原样返回。
        """
        deadline = coerce_deadline(deadline)
        if deadline is not None:
            deadline.check(context="QA timestamp")
        pg = bind_store_deadline(self.pg, deadline)
        try:
            if not body or "[" not in body or not pg:
                return body
            if deadline is None and not pg.is_connected():
                return body
            ids = [int(m) for m in re.findall(r"\[(\d{3,7})\]", body)]
            if not ids:
                return body

            if conn is not None:
                with conn.cursor() as cur:
                    placeholders = ",".join(["%s"] * len(ids))
                    cur.execute(
                        f"SELECT id, timestamp FROM qa_pairs WHERE id IN ({placeholders})",
                        ids,
                    )
                    ts_map = {r[0]: r[1] for r in cur.fetchall()}
                if not ts_map:
                    return body

                def _rep(m):
                    i = int(m.group(1))
                    ts = ts_map.get(i)
                    if ts:
                        return f"[{i} @{str(ts)[:10]}]"
                    return m.group(0)

                return re.sub(r"\[(\d{3,7})\]", _rep, body)

            with _lease_pg_store(pg, deadline=deadline) as leased_conn:
                if not leased_conn:
                    return body
                with leased_conn.cursor() as cur:
                    placeholders = ",".join(["%s"] * len(ids))
                    cur.execute(
                        f"SELECT id, timestamp FROM qa_pairs WHERE id IN ({placeholders})",
                        ids,
                    )
                    ts_map = {r[0]: r[1] for r in cur.fetchall()}
                if not ts_map:
                    return body

                def _rep(m):
                    i = int(m.group(1))
                    ts = ts_map.get(i)
                    if ts:
                        return f"[{i} @{str(ts)[:10]}]"
                    return m.group(0)

                return re.sub(r"\[(\d{3,7})\]", _rep, body)
        except PrefetchDeadlineExceeded:
            raise
        except Exception:
            return body

    def _anchor_expand(self, query: str, embed_cfg: dict, q_emb: list, *, deadline=None) -> list[str]:
        """v1.3 锚点展开（印→卡判断层→卡关联QA）+ v1.4 句子级优先（note_segments）

        2026-08-15 v1.4: 命中粒度从整版印细化到印的一句话 — query 向量命中
        note_segments（[#N] 切分的句/事件段），段带 qa_id（生成时绑定的来源）→
        topic_entries 反向查卡 → 精准展开。段命中不足/无表回退整版路径（v1.3）。
        """
        deadline = coerce_deadline(deadline)
        if deadline is not None:
            deadline.check(context="anchor expand")
        pg = bind_store_deadline(self.pg, deadline)
        try:
            if not pg or (deadline is None and not pg.is_connected()) or not q_emb:
                return []
            with _lease_pg_store(pg, deadline=deadline) as conn:
                if not conn:
                    return []
                cur = conn.cursor()
                # ── v1.4: 句子级优先 ──
                # The production snapshot may legitimately predate this
                # optional table.  Probe it once per Core, then keep the same
                # v1.3 fallback without paying a failed round-trip each query.
                segs = []
                if getattr(self, "_note_segments_available", None) is not False:
                    try:
                        cur.execute(
                            "SELECT id, note_id, version, ref_no, qa_id, text, "
                            "embedding <=> %s::vector AS d FROM note_segments "
                            "WHERE embedding IS NOT NULL AND text IS NOT NULL "
                            "ORDER BY embedding <=> %s::vector LIMIT 4",
                            (q_emb, q_emb),
                        )
                        segs = cur.fetchall()
                        self._note_segments_available = True
                    except PrefetchDeadlineExceeded:
                        raise
                    except Exception as _note_segments_error:
                        if (
                            type(_note_segments_error).__name__ == "UndefinedTable"
                            or getattr(_note_segments_error, "pgcode", None) == "42P01"
                        ):
                            self._note_segments_available = False
                if segs:
                    parts: list[str] = []
                    used_notes: set[int] = set()
                    used_cards: set[str] = set()
                    for seg_id, note_id, version, ref_no, qa_id, text, dist in segs:
                        if dist is None or dist > 0.55:  # 句级向量距离更近 (短文本)
                            continue
                        if note_id in used_notes and qa_id is None:
                            continue
                        used_notes.add(note_id)
                        # 1. 段原文 (浓缩叙事锚点)
                        if qa_id:
                            cur.execute(
                                "SELECT timestamp FROM qa_pairs WHERE id = %s", (qa_id,))
                            row = cur.fetchone()
                            ts_str = str(row[0])[:19] if row and row[0] else ""
                            parts.append(f"[印句] [{ts_str}] {text}")
                        else:
                            parts.append(f"[印句] {text}")
                        # 2. 段来源 QA → 关联主题卡 (推理前置层: 卡 body = 跨轮浓缩关联知识)
                        if qa_id:
                            cur.execute(
                                "SELECT DISTINCT topic_id FROM topic_entries "
                                "WHERE topic_id LIKE 't_%%' AND question = ("
                                "SELECT question FROM qa_pairs WHERE id = %s) LIMIT 2",
                                (qa_id,),
                            )
                            tids = [r[0] for r in cur.fetchall()]
                            for tid in tids:
                                if tid in used_cards:
                                    continue
                                used_cards.add(tid)
                                cur.execute(
                                    "SELECT title, body FROM topics WHERE topic_id = %s", (tid,))
                                row = cur.fetchone()
                                if not row or not row[1]:
                                    continue
                                # 卡 body 全文 (跨轮关联的推理产物 — 核心注入)
                                parts.append(
                                    f"━━━ {row[0]} ━━━\n{self._attach_qa_timestamps(row[1], conn=conn, deadline=deadline)}")
                                # 3. 卡关联 QA (entries): 按与 query 相关度取, 不是 seq 前4
                                #    (卡跨轮累积, seq 前4=最早轮次旧事实 — 时间错位)
                                from .tokenizer import build_query_tokens as _bqt
                                _terms = [t for t in _bqt(query) if len(t) >= 2][:6]
                                cur.execute(
                                    "SELECT question, answer, timestamp FROM topic_entries "
                                    "WHERE topic_id = %s AND question <> ''", (tid,))
                                ent_rows = cur.fetchall()
                                if _terms and ent_rows:
                                    scored = []
                                    for q, a, ts in ent_rows:
                                        s = sum(1 for t in _terms if t in f"{q or ''} {a or ''}".lower())
                                        if s > 0:
                                            scored.append((s, q, a, ts))
                                    scored.sort(key=lambda x: -x[0])
                                    for s, q, a, ts in scored[:3]:
                                        ts2 = str(ts)[:19] if ts else ""
                                        parts.append(f"[qa] [{ts2}] Q: {q}\nA: {a}")
                                elif ent_rows:
                                    # 无词命中 → 取时间跨度代表 (首/尾各1)
                                    for q, a, ts in [ent_rows[0], ent_rows[-1]]:
                                        if not (q or "").strip():
                                            continue
                                        ts2 = str(ts)[:19] if ts else ""
                                        parts.append(f"[qa] [{ts2}] Q: {q}\nA: {a}")
                        # 4. 段来源 QA 原文 (本轮细节 + 时间戳)
                        if qa_id:
                            cur.execute(
                                "SELECT question, answer, timestamp FROM qa_pairs WHERE id = %s",
                                (qa_id,))
                            row = cur.fetchone()
                            if row and (row[0] or "").strip():
                                ts2 = str(row[2])[:19] if row[2] else ""
                                parts.append(f"[qa] [{ts2}] Q: {row[0]}\nA: {row[1]}")
                    # 5. 范围 QA 精准补漏 (v1.4.2 2026-08-15): 卡管跨轮主题, 一次性事件
                    #    (museum 案例) 不在卡 entries — 范围原文是唯一来源。但不全倒:
                    #    按 query 关键词筛选命中 QA 优先 (精准补漏, 预算内 top 8)。
                    note_ids = list(used_notes)[:2]
                    for nid in note_ids:
                        cur.execute(
                            "SELECT links::text FROM observation_notes WHERE id = %s", (nid,))
                        _row = cur.fetchone()
                        if not _row:
                            continue
                        _links = json.loads(_row[0] or "[]")
                        _qa_link = next((l for l in _links if l.get("kind") == "qa"), None)
                        if not _qa_link or not _qa_link.get("first") or not _qa_link.get("last"):
                            continue
                        cur.execute(
                            "SELECT id, question, answer, timestamp FROM qa_pairs "
                            "WHERE id BETWEEN %s AND %s ORDER BY id",
                            (_qa_link["first"], _qa_link["last"]))
                        _rows = cur.fetchall()
                        if not _rows:
                            continue
                        from .tokenizer import build_query_tokens as _bqt2
                        _t2 = [t for t in _bqt2(query) if len(t) >= 2][:8]
                        _scored = []
                        for qid, q, a, ts in _rows:
                            if not (q or "").strip():
                                continue
                            if _t2:
                                s = sum(1 for t in _t2 if t in f"{q or ''} {a or ''}".lower())
                            else:
                                s = 1
                            _scored.append((s, qid, q, a, ts))
                        _scored.sort(key=lambda x: -x[0])
                        for s, qid, q, a, ts in _scored[:8]:
                            if s <= 0:
                                continue
                            ts3 = str(ts)[:19] if ts else ""
                            parts.append(f"[qa] [{ts3}] Q: {q}\nA: {a}")
                    if parts:
                        return parts
                # ── v1.3 整版路径（句子级无命中/无表时）──
                from .tokenizer import build_query_tokens
                # 1. 向量召回印 top2 (observation_notes 全文向量, cosine distance)
                cur.execute(
                    "SELECT id, version, content, links::text, embedding <=> %s::vector AS d "
                    "FROM observation_notes WHERE embedding IS NOT NULL "
                    "ORDER BY embedding <=> %s::vector LIMIT 2",
                    (q_emb, q_emb),
                )
                notes = cur.fetchall()
                # v1.1 (2026-08-14): 停用词过滤 — what/does/her/as 等制造假命中,
                # 证据被同分挤出 top3 (conv-44:49 实锤: pets 证据排第4被丢)
                _STOP = {"what","does","her","as","when","who","where","how","was","were",
                         "is","are","did","have","has","do","the","a","an","and","or","of",
                         "to","in","on","at","for","with","your","you","their","they","she",
                         "he","it","its","that","this","these","those","not","his","him",
                         "our","we","can","could","would","will","been","being","about",
                         "there","why","which","than","then","from","into","out","over",
                         "again","once","here","all","any","both","each","few","more",
                         "most","other","some","such","only","own","same","so","too","very"}
                terms = [t for t in build_query_tokens(query) if len(t) >= 2 and t not in _STOP][:8]
                parts: list[str] = []
                for nid, version, content, links_txt, dist in notes:
                    # 阈值: 印是 3500-4300 字全文向量, 短 query 距离天然大
                    # (生产中文实测最相关 0.62-0.64, 实验英文 0.33) — 放宽到 0.70, 质量靠 QA 关键词裁剪
                    if dist is None or dist > 0.70:
                        continue
                    try:
                        links = json.loads(links_txt or "[]")
                    except Exception:
                        links = []
                    qa_link = next((l for l in links if l.get("kind") == "qa"), None)
                    tp_link = next((l for l in links if l.get("kind") == "topics"), None)
                    got = 0
                    # v1.2 (2026-08-14 用户纠正): 三段链式传播 — 印命中 → 主题卡判断层
                    # → 主题卡 body 引用的 QA 展开 (不是范围全量倒灌 = 跳过卡判断 = 偷懒)
                    # 卡 body 的 [QA id] 引用是 LLM 提炼时判断过的"相关 QA" — 精准收敛,
                    # 展开量 4-8 条/卡, 预算可控; 卡无引用时回退范围兜底 (少数场景)。
                    if tp_link:
                        tids = [t for t in (tp_link.get("topic_ids") or []) if t][:2]
                        ref_ids: list[int] = []
                        # [#N] 是观察者卡 body 的段内序号引用 → 映射到范围第 N 条 QA
                        # (2026-08-14: 不能用 first+N-1 — 范围 id 可能有空洞(其他会话穿插),
                        #  必须取范围内实际有序 id 列表再按序号取)
                        seq_ids: list[int] = []
                        if qa_link and qa_link.get("first") and qa_link.get("last"):
                            cur.execute(
                                "SELECT id FROM qa_pairs WHERE id BETWEEN %s AND %s ORDER BY id",
                                (qa_link["first"], qa_link["last"]),
                            )
                            seq_ids = [r[0] for r in cur.fetchall()]
                        for tid in tids:
                            cur.execute(
                                "SELECT title, body FROM topics WHERE topic_id = %s", (tid,),
                            )
                            row = cur.fetchone()
                            if not row or not row[1]:
                                continue
                            # 卡 body 时间戳回填 (v2) + 收集 QA 引用
                            body_ts = self._attach_qa_timestamps(row[1], conn=conn, deadline=deadline)
                            parts.append(f"━━━ {row[0]} ━━━\n{body_ts}")
                            got += 1
                            # v1.3 (2026-08-14): 卡→QA 关联优先用 topic_entries (代码向量匹配,
                            # 8/7 方案A — obs:xxx:vector:0.6x 可靠); [#N] 解析降为兜底。
                            # 评测库 8/14 建库漏拷 entries 导致评测环境断链 — 已补拷 4240 条。
                            cur.execute(
                                "SELECT question, answer, timestamp FROM topic_entries "
                                "WHERE topic_id = %s AND question <> '' ORDER BY seq LIMIT 6",
                                (tid,),
                            )
                            ent_rows = cur.fetchall()
                            if ent_rows:
                                for q, a, ts in ent_rows:
                                    if not (q or "").strip():
                                        continue
                                    ts_str = str(ts)[:19] if ts else ""
                                    parts.append(f"[qa] [{ts_str}] Q: {q}\nA: {a}")
                                    got += 1
                                continue  # entries 已覆盖, 不解析 [#N]
                            # 兜底: 无 entries → 解析卡 body [#N] 段内序号
                            if seq_ids:
                                for m in re.finditer(r"\[#(\d+)\]", row[1]):
                                    n = int(m.group(1))
                                    if 1 <= n <= len(seq_ids):
                                        ref_ids.append(seq_ids[n - 1])
                            # 格式2: [数字] 直接 QA id (聚簇卡格式)
                            ref_ids += [int(m) for m in re.findall(r"\[(\d{3,7})\]", row[1])]
                        if ref_ids:
                            ref_ids = list(dict.fromkeys(ref_ids))[:12]
                            ph = ",".join(["%s"] * len(ref_ids))
                            cur.execute(
                                f"SELECT id, question, answer, timestamp FROM qa_pairs "
                                f"WHERE id IN ({ph}) ORDER BY id", ref_ids,
                            )
                            for qid, q, a, ts in cur.fetchall():
                                if not (q or "").strip():
                                    continue
                                ts_str = str(ts)[:19] if ts else ""
                                parts.append(f"[qa] [{ts_str}] Q: {q}\nA: {a}")
                                got += 1
                            # v1.2.1 (2026-08-14): 卡引用展开后, 范围 QA 补充 (证据保底)
                            # Calvin Tokyo 案例: 证据 5921 不在命中印范围(6022-6051) — 印命中
                            # 是话题相关而非证据所在段。卡引用(LLM判断) + 范围(印提炼来源)
                            # 合并注入, 预算内由主路径 total_len 截断, 卡引用优先。
                            if qa_link and qa_link.get("first") and qa_link.get("last"):
                                cur.execute(
                                    "SELECT id, question, answer, timestamp FROM qa_pairs "
                                    "WHERE id BETWEEN %s AND %s AND id NOT IN ("
                                    + ",".join(["%s"] * len(ref_ids)) + ") ORDER BY id",
                                    [qa_link["first"], qa_link["last"]] + ref_ids,
                                )
                                for qid, q, a, ts in cur.fetchall():
                                    if not (q or "").strip():
                                        continue
                                    ts_str = str(ts)[:19] if ts else ""
                                    parts.append(f"[qa] [{ts_str}] Q: {q}\nA: {a}")
                                    got += 1
                        else:
                            # 兜底: 卡无 QA 引用 → 范围全量 (卡没提炼出关联时, 宁全勿漏)
                            if qa_link and qa_link.get("first") and qa_link.get("last"):
                                cur.execute(
                                    "SELECT id, question, answer, timestamp FROM qa_pairs "
                                    "WHERE id BETWEEN %s AND %s ORDER BY id",
                                    (qa_link["first"], qa_link["last"]),
                                )
                                for qid, q, a, ts in cur.fetchall():
                                    if not (q or "").strip():
                                        continue
                                    ts_str = str(ts)[:19] if ts else ""
                                    parts.append(f"[qa] [{ts_str}] Q: {q}\nA: {a}")
                                    got += 1
                    elif qa_link and qa_link.get("first") and qa_link.get("last"):
                        # 无 topics 链接的印 → 范围兜底
                        cur.execute(
                            "SELECT id, question, answer, timestamp FROM qa_pairs "
                            "WHERE id BETWEEN %s AND %s ORDER BY id",
                            (qa_link["first"], qa_link["last"]),
                        )
                        for qid, q, a, ts in cur.fetchall():
                            if not (q or "").strip():
                                continue
                            ts_str = str(ts)[:19] if ts else ""
                            parts.append(f"[qa] [{ts_str}] Q: {q}\nA: {a}")
                            got += 1
                    # 4. 印段本身 (前 600 字) — 只有该印有展开内容时才附带, 防噪声
                    if content and got > 0:
                        parts.append(f"━━━ 印({version}) ━━━\n{content[:600]}")
                return parts
        except PrefetchDeadlineExceeded:
            raise
        except Exception:
            return []

    def _graph_related_hint(self, hits: list) -> str:
        """[RETIRED 2026-08-26] 图谱关联提示已砍 (三轮消融不显著, 语义图与主召回冗余)。

        保留空壳方法: 测试桩 patch.object(core, "_graph_related_hint") 仍引用此名,
        删方法会让旧测试 AttributeError。返回空串 = 召回链路不再有图谱注入。
        复活路径 (观察者提炼时顺带输出关联卡) 见 docs/graph-ablation-20260825.md。
        """
        return ""

    def _recall_yin_segments(self, query: str, embed_cfg: dict, limit: int = 2,
                             *, q_emb=None, deadline=None) -> str:
        """印段落召回 — effective 池 (pool_role='yin_segment') 段落级向量匹配

        复用上游 query embedding (cache 命中即零成本)，再跑 1 次 PG 向量查询 (秒级)。
        传入 q_emb 时直接使用上游算好的向量 — 不再调 call_embedding,
        避免 prefetch 与本路径算两次 embedding (第二次 cache key 不同会真的发请求)。
        q_emb=None 表示上游 embedding 已失败 — 不为 yin 再发第二个请求,
        避免实时 P95 被重复失败/重试放大。命中段落附 metadata.topic_ids (主题卡链接)
        — 链式召回可展开。失败/超预算静默返回空 — 不阻塞主路径。

        注: embed_cfg 参数保留兼容性 — 当前实现不依赖它做二次 embedding.
        """
        deadline = coerce_deadline(deadline)
        if deadline is not None:
            deadline.check(context="yin segments")
        pg = bind_store_deadline(self.pg, deadline)
        try:
            from .embedding import call_embedding  # noqa: F401  # 保留 import: 兼容外部 monkey-patch 时仍能找到符号
            if not pg:
                return ""
            if deadline is None and not pg.is_connected():
                return ""
            if q_emb is None:
                # 上游 embedding 已失败 — 不再尝试, 避免 P95 被放大
                logger.debug("_recall_yin_segments: 上游 q_emb=None, 跳过")
                return ""
            hits = pg.search_effective(q_emb, pool_role="yin_segment", limit=limit)
            if not hits:
                return ""
            lines = ["[印段落] "]
            for hit in hits:
                title = hit.get("title", "")
                cos = hit.get("cosine", 0.0)
                lines.append(f"- {title} (cos={cos:.3f})")
                body = (hit.get("content") or "").strip()
                if body:
                    lines.append(f"  {body}")  # 2026-08-09: 去 [:200] 截断 — 用户红线"不能截断", 印段落全文注入
            return "\n".join(lines)
        except PrefetchDeadlineExceeded:
            raise
        except Exception as e:
            logger.debug("_recall_yin_segments 失败: %s", _safe_err(e)[:100])
            return ""

    # ── 统一注入 ──

    def get_injector(self, strategy: str | None = None) -> MemoryInjector:
        """获取统一注入器（lazy init）

        Args:
            strategy: 可选, 覆盖默认 strategy (首次创建时生效)
        """
        if self._injector is None:
            self._injector = MemoryInjector(self, strategy=strategy or "identity_first")
        return self._injector

    def build_memory_context(self, query: str = "", strategy: str | None = None,
                             session_id: str = "", *, deadline=None) -> str:
        """统一注入入口——替换手动调 prefetch + system_prompt_block

        Args:
            query: 当前场景的查询 (空串=仅身份锚点)
            strategy: 可选, 临时指定注入策略
            session_id: 可选, 当前 session id — 用于首轮召回幂等。
                        同一 session_id 在 reset 之前只做一次首轮召回注入。
                        旧调用不传 session_id 时行为保持兼容。

        Returns:
            合并后的注入字符串 (identity + recall + 首轮召回)
        """
        deadline = coerce_deadline(deadline)
        if deadline is not None:
            deadline.check(context="build_memory_context")
        session_id = session_id or getattr(self, "_active_session_id", "")
        if strategy is not None:
            # 临时策略, 不影响 injector 内部缓存
            inj = MemoryInjector(self, strategy=strategy)
            if deadline is None:
                return inj.build_context(query, session_id=session_id)
            return inj.build_context(query, session_id=session_id, deadline=deadline)
        injector = self.get_injector()
        if deadline is None:
            return injector.build_context(query, session_id=session_id)
        return injector.build_context(query, session_id=session_id, deadline=deadline)

    def reset_injector(self, session_id: str | None = None) -> None:
        """Reset injection dedupe for one session (or the active legacy session)."""
        if self._injector is not None:
            self._injector.reset(session_id=session_id)

    def get_message_context(self, source_id: str) -> str:
        """按 source_id 取原文 — PG 优先, 文件兜底"""
        import json as _json
        if self.pg:
            try:
                result = self.pg.get_message_context(source_id)
                if result:
                    return _json.dumps({"success": True, "source_id": source_id, "content": result["content"], "metadata": result["metadata"]}, ensure_ascii=False)
            except Exception:
                pass
            try:
                row = self.pg.get_card_by_source_id(source_id)
                if row:
                    return _json.dumps({"success": True, "source_id": source_id, "content": row.get("content", ""), "metadata": {}}, ensure_ascii=False)
            except Exception:
                pass
        try:
            from .j_writer import get_message_context as j_get
            cfg = self._config or {}
            result = j_get(source_id, cfg)
            if result:
                return _json.dumps({"success": True, "source_id": source_id, "session_id": result["session_id"], "content": result["content"]}, ensure_ascii=False)
        except Exception:
            pass
        return _json.dumps({"success": False, "error": f"source_id 未找到: {source_id}"}, ensure_ascii=False)

    # ── 手帐 ──










    def _on_live_flush(self, items: list):
        """LiveBuffer 刷入 PG 后的回调 — 触发轻量主题匹配.

        Bound to V3Core (not LiveBuffer). TopicRecallCache.invalidate runs only
        after the pool lease / direct connection is fully released.
        """
        if not items:
            return
        mutated = False
        try:
            from .topic_store import TopicStore
            from .event_adapter import SessionEvents, QAEvent
            from .topic_refine import SessionTracker, TopicMatcher, refine_turn
            from .config import _resolve_data_dir
            import os as _os
            import time as _time

            cfg = self.config
            db_path = str(_resolve_data_dir(cfg) / "v3_topic_full.db")
            if not _os.path.exists(db_path):
                return

            _embed_cfg = safe_embed_cfg(cfg)
            lb_pg = getattr(self, "_pg", None)

            def _do_refine(pg_conn):
                nonlocal mutated
                store = TopicStore(db_path, pg_conn=pg_conn)
                try:
                    tracker = SessionTracker(store)
                    matcher = TopicMatcher(store, embed_cfg=_embed_cfg, pg_conn=pg_conn)

                    # 持久化 match_history 跨多次 _on_live_flush 调用
                    if not hasattr(self, "_refine_mh"):
                        self._refine_mh = {}

                    # 从 items 中按 session_id 分组构建 SessionEvents
                    # items 是 (session_id, msg_id, content, role) 元组，刚刷入 PG
                    from collections import defaultdict
                    sessions_data = defaultdict(list)
                    for item in items:
                        if len(item) >= 4:
                            sessions_data[item[0]].append(item)

                    for sid, sess_items in sessions_data.items():
                        try:
                            qa_pairs = []
                            for it in sess_items:
                                qa_pairs.append(QAEvent(
                                    role=it[3] or "", content=(it[2] or "")[:1000],
                                    timestamp=_time.time(),
                                ))
                            if len(qa_pairs) < 3:
                                continue
                            session = SessionEvents(
                                session_id=sid, source="live_buffer",
                                qa_pairs=qa_pairs, turn_count=len(qa_pairs),
                            )
                            result = refine_turn(session, tracker, matcher, self._refine_mh)
                            if result == "match":
                                mutated = True
                        except ValueError:
                            raise
                        except Exception as e:
                            logger.warning("_on_live_flush refine_turn error: %s", _safe_err(e)[:100])
                finally:
                    try:
                        store.close()
                    except Exception as e:
                        logger.warning("_on_live_flush store.close error: %s", _safe_err(e)[:100])

            if lb_pg is not None:
                try:
                    with _lease_pg_store(lb_pg) as pg_conn:
                        _do_refine(pg_conn)
                except Exception as e:
                    logger.warning("_on_live_flush PG lease failed (proceed fallback/SQLite): %s", _safe_err(e)[:100])
            else:
                # 兜底: 仅当没有 lb_pg 时新建独立直连 (并负责物理关闭)
                pg_conn = None
                try:
                    if cfg and hasattr(cfg, "pg") and cfg.pg:
                        import psycopg2
                        pg_conn = psycopg2.connect(
                            host=cfg.pg.host, port=cfg.pg.port,
                            dbname=cfg.pg.database, user=cfg.pg.user,
                            password=cfg.pg.password,
                        )
                except Exception as e:
                    logger.warning("_on_live_flush PG connect failed (proceed SQLite-only): %s", _safe_err(e)[:100])
                    pg_conn = None

                try:
                    _do_refine(pg_conn)
                finally:
                    if pg_conn is not None:
                        try:
                            pg_conn.close()
                        except Exception:
                            pass

            if mutated:
                try:
                    self._notify_topic_recall_invalidation()
                except Exception as e:
                    logger.warning("_on_live_flush on_topics_commit 失败 (非阻塞): %s", _safe_err(e)[:100])
        except ValueError:
            raise
        except Exception as e:
            logger.warning("_on_live_flush 整体失败: %s", _safe_err(e)[:100])

    def _enqueue_live_message(self, session_id: str, msg_id: str,
                              content: str, role: str, turn_id: str,
                              timestamp=None, tool_calls=None,
                              tool_results=None):
        """Accept a live message only when its canonical identity is new."""
        live = self.live_buffer
        checker = getattr(live, "has_durable_marker", None)
        if callable(checker):
            try:
                if checker(session_id, msg_id, content, role, turn_id,
                           timestamp, tool_calls or [], tool_results or []) is True:
                    return True
            except Exception:
                # A failed probe must not silently drop a real delta; enqueue
                # remains the conservative fallback.
                pass
        return live.enqueue(
            session_id, msg_id, content, role, turn_id,
            timestamp=timestamp,
            tool_calls=tool_calls or [],
            tool_results=tool_results or [],
        )

    def sync_turn(self, session_id: str, messages: list[dict] | None = None) -> None:
        """每轮 turn 调用 — 入队 live buffer + 就地 QA 配对 + 写 qa_pairs

        Phase v4 改造:
          - 沿用原逻辑: enqueue 到 live_buffer (写 v3_messages)
          - 新增: 就地追踪 user→assistant 配对, 当新 user 消息到达时
                  flush 上一个 QA 对 (算 embedding + 写 qa_pairs)
          - 不依赖 mapper cron (mapper 仍可做历史积压回填, 走 ON CONFLICT DO NOTHING)

        状态:
          - _pending_qa[session_id] = {
                "q": str, "a": str,
                "q_msg_id": str, "q_turn": str, "q_ts": datetime,
                "tool_calls": list, "tool_results": list
            }

        配对规则:
          - role=user: 触发 flush (上回合配对) → reset pending → 存新 q
          - role=assistant: 追加到 pending.a
          - tool/function 消息: 不进 pending, 但记到 tool_calls/tool_results (给下个 q 配)
        """
        if not session_id or not messages:
            if not session_id:
                logger.warning("sync_turn 收到空 session_id, 跳过")
            if not messages:
                logger.warning("sync_turn 收到空 messages (session=%s), 跳过", session_id or "?")
            return
        if getattr(self, '_core_fenced', False) or not getattr(self, '_core_accepting', True) or getattr(self, '_core_closed', False):
            return
        import hashlib



        # The active id is only a routing pointer; all mutable session data
        # lives in the selected Context.
        self._active_session_id = session_id
        context = self.get_session_context(session_id)
        if context is None:
            return
        with self._qa_lock:
            context.turn += 1
            session_turn = str(context.turn)
            pending = context.pending_qa
        # 2026-09-07 compression-aware source-ingest durability:
        # walk the FULL snapshot in order; the per-session identity set
        # ``context.synced_message_ids`` is the AUTHORITATIVE delta cursor.
        # The previous positional cursor (``synced_count = len(messages)``)
        # was unsafe: compression shrinks the re-assembled history, so
        # ``len(messages) <= prev_count`` early-returned and dropped the
        # new turn.  ``synced_count`` is retained as an observable signal
        # of the most recent snapshot length.
        #
        # Algorithm (compression-aware):
        #   1. Freeze ``prior_seen`` from the persistent context set BEFORE
        #      iteration — this snapshot's delta is computed against what
        #      was already accepted in prior calls, NOT what this snapshot
        #      contains.
        #   2. Walk ``messages`` in order.  Only ``key in prior_seen`` skips.
        #   3. ``processed_this_call`` accumulates keys that either got a
        #      durable enqueue, or hit an explicit NON-LIVE-SYNC-BY-DESIGN
        #      branch (tool/injection/empty/oversize/todo-snapshot-synthetic)
        #      so re-sync of the same snapshot is a clean no-op.
        #   4. At end of snapshot: ``synced_message_ids |= processed_this_call``.
        prior_seen = set(context.synced_message_ids)
        processed_this_call: set[str] = set()
        source_ingest_failed = False
        for msg in messages:
            role = str(msg.get("role", ""))
            key, _reliable = _stable_message_key(msg)
            if not key:
                # No stable id and no fallback — skip silently to avoid
                # silent acceptance of unknown deltas (I4).
                continue
            if key in prior_seen or key in processed_this_call:
                # Already accepted in a prior sync or earlier in this snapshot — historical replay
                # within the same session must NOT re-enqueue (I3, I6).
                # Also record in processed_this_call so a replay of this
                # exact snapshot in the same call (rare) stays idempotent.
                processed_this_call.add(key)
                continue
            # NON-LIVE-SYNC-BY-DESIGN: Hermes compression may inject a
            # synthetic user turn tagged ``_todo_snapshot_synthetic=True``
            # as part of the rolling compactor's todo snapshot.  This is
            # not a direct durable turn; the durable contract is the
            # [CONTEXT COMPACTION] prefix branch below.  Acknowledge it
            # as processed so it does not re-enter on every replay.
            if msg.get("_todo_snapshot_synthetic") is True:
                processed_this_call.add(key)
                continue
            # 跳过 tool 消息，只写 user/assistant
            if role in ("tool", "tool_call", "tool_result", "function"):
                # 收集 tool 上下文到 pending (下个 user 配对时一起入 qa_pairs)
                if pending is not None:
                    tc = msg.get("tool_calls") or []
                    tr = msg.get("tool_result") or msg.get("tool_results") or []
                    if isinstance(tc, list) and tc:
                        pending["tool_calls"].extend(tc)
                    if isinstance(tr, list) and tr:
                        pending["tool_results"].extend(tr)
                # tool branches are NON-LIVE-SYNC-BY-DESIGN for the live
                # buffer (tool context only feeds pending); still mark
                # processed so re-sync is idempotent.
                processed_this_call.add(key)
                continue
            content = str(msg.get("content", ""))
            if not content:
                # empty content — accept as processed (NON-LIVE-SYNC-BY-DESIGN)
                processed_this_call.add(key)
                continue
            # v4 fix: 真实注入前缀带前导时间戳+空格 (例如 "Sat 2026-04-11 01:15 GMT+8] [Subagent Context]"),
            # startswith 元组匹配会失败, 改用正则兼容多种前导格式 (ISO 日期 / GMT+8 / 时分秒 / 中文方括号)。
            # 2026-08-01 扩展: 覆盖系统消息格式 — [cron:...]/[System]/[AGENT_RULES]/[Retry after...]/
            # "You just executed tool calls..." (Hermes tool-use 强制提示, 正常用户不会以这些格式说话)
            # 2026-08-05 扩展: Hermes v0.20 滚动压缩 summary 消息前缀 [CONTEXT COMPACTION — REFERENCE ONLY]
            # (压缩产物 role 可能是 user/assistant, 不能当真实对话入消息河/配对)
            _injection_pattern = re.compile(
                r'^\s*\[?(?:'
                r'(?:\d{1,2}\s+\w{3}\s+\d{4}(?:\s+\d{1,2}:\d{2})?(?:\s+GMT[+-]\d+)?'
                r'|\w{3}\s+\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2})?(?:\s+GMT[+-]\d+)?'
                r'|\w{3}\s+\d{1,2}(?:\s+\d{1,2}:\d{2})?'
                r'|\d{1,2}:\d{2}'
                r'|\d{4}-\d{2}-\d{2})?'
                r'\s*\]?\s*\[(?:IMPORTANT|ASYNC DELEGATION|Subagent Context|OUT-OF-BAND|【IMPORTANT|【ASYNC|'
                r'cron:|System|AGENT_RULES|Retry after|CONTEXT COMPACTION)'
                r'|^\s*You just executed tool calls'
                r'|^\s*\[System\]'
                r')'
            )
            if _injection_pattern.match(content):
                processed_this_call.add(key)
                continue
            # 兼容: 元组直接前缀也能匹配 (纯前缀无时间戳)
            _injection_prefixes_fallback = ("[IMPORTANT:", "[ASYNC DELEGATION", "[Subagent Context]", "[OUT-OF-BAND", "[CONTEXT COMPACTION")
            if content.startswith(_injection_prefixes_fallback):
                processed_this_call.add(key)
                continue
            # 超长消息跳过（>24K chars不进消息河）
            if len(content) > 24000:
                processed_this_call.add(key)
                continue
            # LiveBuffer identity must follow the canonical stable key,
            # not a content-derived surrogate that changes on compression.
            msg_id = key


            # ── Phase v4: 就地 QA 配对 ──
            if role == "user":
                _ok = self._enqueue_live_message(
                    session_id, msg_id, content, role, session_turn,
                    timestamp=_parse_msg_timestamp(msg.get("timestamp")),
                    tool_calls=msg.get("tool_calls") or [],
                    tool_results=msg.get("tool_results") or msg.get("tool_result") or [],
                )
                if _ok is False:
                    source_ingest_failed = True
                    # Do not flush or replace pending QA until source ingest accepts.
                    continue
                # 新 user 来了 → 上一回合结束, flush pending (若有)
                if pending is not None and pending.get("q"):
                    try:
                        # 2026-08-02: 异步化 — 入队后台 worker (embedding 0.5-2s 不阻塞 turn 结束)
                        self._submit_flush(session_id, pending)
                    except Exception as e:
                        # v4 fix: 立即捕获异常消息到本地变量, 避免 Python 嵌套作用域下 e 未绑定
                        _err_msg = _safe_err(e)
                        logger.warning("sync_turn flush pending 失败 (不阻塞): %s", _err_msg)
                        try:
                            import json as _json
                            import os as _os
                            from datetime import datetime as _dt
                            from .config import _resolve_data_dir
                            lost_dir = str(_resolve_data_dir(self._config) / "j" / "_pending_lost")
                            _os.makedirs(lost_dir, exist_ok=True)
                            _ts = _dt.now().strftime("%Y%m%d_%H%M%S")
                            _fname = f"{session_id[:12]}_{_ts}.json"
                            _dump = {
                                "session_id": session_id,
                                "failed_at": _ts,
                                "error": _err_msg,
                                "pending": pending,
                            }
                            _json.dump(_dump, open(_os.path.join(lost_dir, _fname), "w"),
                                       ensure_ascii=False, indent=2)
                        except Exception:
                            pass  # 兜底也失败就算了, 不阻塞
                # 启动新 pending
                pending = {
                    "q": content,
                    "a": "",
                    "q_msg_id": msg_id,
                    "q_turn": session_turn,
                    "q_ts": datetime.now(timezone.utc),
                    "tool_calls": [],
                    "tool_results": [],
                }
                with self._qa_lock:
                    context.pending_qa = pending
                processed_this_call.add(key)
                continue
            elif role == "assistant":
                _ok = self._enqueue_live_message(
                    session_id, msg_id, content, role, session_turn,
                    timestamp=_parse_msg_timestamp(msg.get("timestamp")),
                    tool_calls=msg.get("tool_calls") or [],
                    tool_results=msg.get("tool_results") or msg.get("tool_result") or [],
                )
                if _ok is False:
                    source_ingest_failed = True
                    # Do not mutate pending QA until source ingest accepts.
                    continue
                if pending is None:
                    # 没有 pending q → 跳过 (避免孤儿 answer)
                    processed_this_call.add(key)
                    continue
                # 累加 a (可能有多个 assistant 段落, 用换行合并)
                if pending["a"]:
                    pending["a"] += "\n" + content
                else:
                    pending["a"] = content
                # 收集 assistant 自己的 tool_calls
                tc = msg.get("tool_calls") or []
                if isinstance(tc, list) and tc:
                    pending["tool_calls"].extend(tc)
                processed_this_call.add(key)
                continue
            # 写 live_buffer (融合后: LiveBuffer 是唯一消息河写入路径 — conversation_stream)
            _ok = self._enqueue_live_message(
                session_id, msg_id, content, role, session_turn,
                timestamp=_parse_msg_timestamp(msg.get("timestamp")),
                tool_calls=msg.get("tool_calls") or [],
                tool_results=msg.get("tool_results") or msg.get("tool_result") or [],
            )
            # truthful bool contract (B): explicit False => NOT accepted, retry
            # next sync, do NOT mark processed.  None / Mock / legacy => accepted.
            if _ok is False:
                source_ingest_failed = True
                continue
            processed_this_call.add(key)
        # End-of-snapshot cursor commit (A): ONLY now do we advance the
        # authoritative delta cursor.  Until this point, ``prior_seen``
        # is what the algorithm consults for "already accepted".
        context.synced_message_ids.update(processed_this_call)
        # unconditional sync_count update — even on no-op replays (I3, I6).
        # We retain it for backward compatibility and as an observable
        # signal of the most recent snapshot length.  The per-session
        # identity set ``synced_message_ids`` remains the AUTHORITATIVE
        # delta cursor.
        if not source_ingest_failed:
            context.synced_count = len(messages)
        # 写 j/ trace — 已禁用（PG 断连时用 state.db 恢复，不写碎片 .md 文件）
        # if not self.pg or not self.pg.is_connected():
        #     try:
        #         from .j_writer import write_turn
        #         cfg = self._config or {}
        #         write_turn(session_id, messages, cfg)
        #     except Exception:
        #         pass
        # v4 fix: checkpoint 机制已禁用 — sync_turn 已实时写 QA 对到 qa_pairs,
        # 旧的每 10 轮写一张空内容 projects 卡会污染 projects 类目, 且文件名
        # checkpoint_{sid}_{YYYY-MM-DD}.md 同日多次会撞名覆盖, 现在直接注释掉。
        # 计数仍保留 (_checkpoint_counter) 以便后续如果需要再启用时无缝接入。
        # Checkpoint creation remains disabled; keep its counter per session.
        context.checkpoint_count += 1
        ck_count = context.checkpoint_count
        # if ck_count % 10 == 0:
        #     try:
        #         from .card_store import DeepStore
        #         store = DeepStore(self._config or {})
        #         now = datetime.now().strftime("%Y-%m-%d %H:%M")
        #         content = f"Session {session_id[:12]} checkpoint at {now} (turn {ck_count})"
        #         store.write_card(
        #             category="projects",
        #             filename=f"checkpoint_{session_id[:12]}_{now[:16].replace(':', '').replace(' ', '_')}.md",
        #             title=f"checkpoint-{session_id[:8]}",
        #             content=content,
        #             tags=["checkpoint"],
        #         )
        #     except Exception:
        #         pass

        # Phase 6.2: 由 post_flush hook 触发（替代旧 Phase 1 每 5 轮匹配）
        # 旧代码已禁用，逻辑移至 _on_live_flush

        # ── watchdog timer: 60s 自动排空 pending QA ──
        try:
            if self._qa_flush_timer:
                self._qa_flush_timer.cancel()
            self._qa_flush_timer = threading.Timer(60.0, self._flush_all_pending_qa)
            self._qa_flush_timer.daemon = True
            self._qa_flush_timer.start()
        except Exception:
            pass  # timer 启动失败不阻塞 sync_turn

        # ── 观察者触发链 (P1: sync_turn 末尾旁路检查 — 四信号触发, 失败不阻塞热路径) ──
        try:
            on_topics_commit = self._notify_topic_recall_invalidation
            runtime = getattr(self, "_runtime", None)
            observer_service = getattr(runtime, "observer_service", None)
            if observer_service is not None:
                observer_service.trigger(
                    config=self._config,
                    on_topics_commit=on_topics_commit,
                )
            else:
                from .observer import maybe_observe
                if self._pg_pool is not None:
                    maybe_observe(
                        self._config,
                        pool=self._pg_pool,
                        on_topics_commit=on_topics_commit,
                    )
                else:
                    maybe_observe(self._config, on_topics_commit=on_topics_commit)
        except Exception:
            pass  # 观察者触发失败不阻塞 sync_turn

    def _submit_flush(self, session_id: str, pending: dict) -> bool:
        """异步提交 QA flush — durable first, then queue (fence-aware, truthful handoff).

        Never enqueue without a verifiable durable marker/fallback for THIS item.
        Returns/bounces via bool if durable cannot be verified, so callers
        can preserve context or write exact per-job fallback.
        """
        if not pending or not pending.get("q"):
            return False
        if not getattr(self, '_core_accepting', True) and not getattr(self, '_core_shutdown_handoff', False):
            return False
        if self._is_core_fenced():
            # after fence, only persist, do not enqueue
            try:
                self._persist_qa_pending(session_id, pending)
            except Exception:
                pass
            return False
        # durable outbox first: atomic temp+flush+fsync+replace — must be verifiable
        try:
            persisted = self._persist_qa_pending(session_id, pending)
            # truthful verification: file must exist for THIS job_id
            verified = False
            if persisted is not None:
                try:
                    if isinstance(persisted, Path) and persisted.exists():
                        verified = True
                    elif isinstance(persisted, (str, Path)) and Path(persisted).exists():
                        verified = True
                except Exception:
                    verified = False
            if not verified:
                # durable failure must not silently accept — return False
                return False
        except Exception as e:
            logger.warning("_submit_flush persist failed: %s", _safe_err(e))
            # truthful handoff: do not enqueue without verifiable durable; propagate as False
            return False
        # now enqueue if still accepting — verified durable exists
        if self._is_core_fenced():
            return False
        if not getattr(self, '_core_accepting', True) and not getattr(self, '_core_shutdown_handoff', False):
            return False
        try:
            q = getattr(self, '_flush_queue', None)
            if q is None:
                q = queue.Queue()
                self._flush_queue = q
            # ensure worker running
            wk = getattr(self, '_flush_worker', None)
            if wk is None or not wk.is_alive():
                with self._core_lock:
                    if not self._is_core_fenced():
                        if getattr(self, '_core_accepting', True) or getattr(self, '_core_shutdown_handoff', False):
                            self._flush_worker = threading.Thread(target=self._flush_worker_loop, name="v3-qa-flush", daemon=True)
                            self._flush_worker.start()
            # check fence again after acquiring lock
            if self._is_core_fenced():
                return False
            if not getattr(self, '_core_accepting', True) and not getattr(self, '_core_shutdown_handoff', False):
                return False
            q.put((session_id, pending))
            return True
        except Exception as e:
            logger.warning("_submit_flush 入队失败: %s", _safe_err(e))
            # do not synchronous fallback to _flush_pending_qa here to avoid blocking hot path
            # keep pending durable for retry
            return False

    def _flush_worker_loop(self) -> None:
        """后台 worker — 顺序处理 flush 队列 (FIFO) with stop signal and fence"""
        import queue as _queue
        while not self._qa_stop.is_set() and not self._is_core_fenced():
            try:
                item = self._flush_queue.get(timeout=0.2)
            except _queue.Empty:
                continue
            except Exception:
                return
            if item is getattr(self, '_qa_stop_sentinel', None):
                try:
                    self._flush_queue.task_done()
                except Exception:
                    pass
                break
            try:
                session_id, pending = item
            except Exception:
                try:
                    self._flush_queue.task_done()
                except Exception:
                    pass
                continue
            # fence check before processing: if fenced after dequeue, keep durable marker and skip PG
            if self._is_core_fenced():
                try:
                    self._flush_queue.task_done()
                except Exception:
                    pass
                continue
            try:
                self._flush_pending_qa(session_id, pending)
            except Exception as e:
                logger.warning("_flush_worker 处理失败: %s", _safe_err(e))
            finally:
                try:
                    self._flush_queue.task_done()
                except Exception:
                    pass

    def _flush_pending_qa(self, session_id: str, pending: dict) -> None:
        """把 pending 的 (q, a) 配对算 embedding + 写 qa_pairs -- fence-aware, durable ack"""
        # fence check before any remote work
        if self._is_core_fenced():
            return
        from .embedding import embed_batch
        q = (pending.get("q") or "").strip()
        a = (pending.get("a") or "").strip()
        q_msg_id = pending.get("q_msg_id", "")
        q_turn = pending.get("q_turn", "")
        if not q:
            return  # 空 q 直接跳过 (理论上不会到这里, sync_turn 已防)
        # open q without answer is also durable but not flushed to PG yet? Spec says open q must be retained as marker.
        # For flush, we allow empty a as valid (existing write contract writes q with empty a)
        # So do not return if a empty; instead insert with a="" (worker will handle)
        source_id = f"qa_sync/{session_id}/{q_turn}/{q_msg_id[:16]}" if q_msg_id else f"qa_sync/{session_id}/{q_turn}"

        parsed_q_ts = _parse_msg_timestamp(pending.get("q_ts"))
        if not isinstance(parsed_q_ts, datetime):
            parsed_q_ts = datetime.now(timezone.utc)
        timestamp = parsed_q_ts

        # 1. 算 embedding (use q + \"\n\" + a)
        embed_cfg = safe_embed_cfg(self._config) if self._config else None
        emb = None
        embedding_failed = False
        embedding_error: BaseException | None = None
        if embed_cfg is not None:
            # fence before embedding
            if self._is_core_fenced():
                return
            try:
                text_for_emb = f"{q}\n{a}" if a else q
                embeds = embed_batch([text_for_emb], embed_cfg)
                if embeds and embeds[0] and any(x != 0.0 for x in embeds[0]):
                    emb = embeds[0]
                else:
                    embedding_failed = True
                    embedding_error = RuntimeError("embedding API returned an empty vector")
            except ValueError:
                raise
            except Exception as e:
                logger.warning("_flush_pending_qa embedding 失败: %s", _safe_err(e))
                embedding_failed = True
                embedding_error = e
                # keep marker for retry, do not ack
                # but if embedding fails, we still try to write without embedding? original does; keep marker only on PG exception?
                # For durability, embedding failure should keep marker (not ack) so next gen can retry with embedding
                # However we will still attempt PG without embedding; if that succeeds, ack.
                emb = None
            if embedding_failed and embedding_error is not None:
                self._mark_qa_embedding_failure(session_id, pending, embedding_error)
            # fence after embedding
            if self._is_core_fenced():
                return

        # 2. 写 qa_pairs (PG) -- fence before lease
        if self._is_core_fenced():
            return
        should_ack = False
        existing_already_durable = False
        existing_requires_repair = False
        repair_committed = False
        new_insert_committed = False
        new_fully_durable_insert = False

        try:
            pg = self._pg or self.pg  # property — lazy connect
            with _lease_pg_store(pg) as conn:
                if not conn:
                    logger.debug("_flush_pending_qa PG 不可用, 跳过写入 (q_turn=%s)", q_turn)
                    return
                # fence re-check after lease acquired? if fenced, skip PG use
                if self._is_core_fenced():
                    return
                with conn.cursor() as cur:
                    emb_str = None
                    fp = ""
                    if emb:
                        emb_str = "[" + ",".join(str(x) for x in emb) + "]"
                        if embed_cfg:
                            try:
                                from .pg_store import _resolve_embed_cfg
                                fp, _ = _resolve_embed_cfg(embed_cfg)
                            except Exception as _e_fp:
                                logger.warning("_flush_pending_qa 解析 fingerprint 失败: %s", _safe_err(_e_fp))
                                raise
                    cur.execute(
                        "SELECT id, (embedding IS NOT NULL) FROM qa_pairs "
                        "WHERE source_id=%s LIMIT 1",
                        (source_id,),
                    )
                    existing = cur.fetchone()
                    if existing is None:
                        cur.execute(
                            "SELECT id, (embedding IS NOT NULL) FROM qa_pairs "
                            "WHERE session_id=%s AND question=%s AND answer=%s LIMIT 1",
                            (session_id, q, a),
                        )
                        existing = cur.fetchone()
                    if existing:
                        existing_id = existing[0] if isinstance(existing, (tuple, list)) else None
                        # A one-column legacy SELECT can only prove existence;
                        # preserve its old already-durable/no-op meaning.
                        has_embedding = (
                            True if not isinstance(existing, (tuple, list)) or len(existing) < 2
                            else bool(existing[1])
                        )
                        existing_requires_repair = not has_embedding and embed_cfg is not None
                        existing_already_durable = has_embedding or embed_cfg is None
                        if existing_already_durable:
                            should_ack = existing_already_durable
                            return
                        if existing_requires_repair and emb_str is None:
                            logger.warning(
                                "_flush_pending_qa existing row has NULL embedding; marker retained"
                            )
                            should_ack = False
                            return
                        cur.execute(
                            "UPDATE qa_pairs SET embedding=%s::vector, embed_model=%s "
                            "WHERE id=%s AND embedding IS NULL",
                            (emb_str, fp, existing_id),
                        )
                        rowcount = getattr(cur, "rowcount", None)
                        if rowcount is not None and int(rowcount) == 0:
                            conn.rollback()
                            should_ack = False
                            return
                        conn.commit()
                        repair_committed = True
                        should_ack = repair_committed
                        return
                    if emb_str:
                        cur.execute(
                            """
                            INSERT INTO qa_pairs
                                (source_id, session_id, turn_id, question, answer,
                                 tool_calls, tool_results, timestamp, source, embedding, embed_model, created_at)
                            VALUES (%s, %s, %s, %s, %s,
                                    %s::jsonb, %s::jsonb, %s, %s, %s::vector, %s, NOW())
                            ON CONFLICT (source_id) DO NOTHING
                            """,
                            (
                                source_id, session_id, q_turn, q, a,
                                json.dumps(pending.get("tool_calls") or [], ensure_ascii=False),
                                json.dumps(pending.get("tool_results") or [], ensure_ascii=False),
                                timestamp, "live_sync",
                                emb_str, fp,
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO qa_pairs
                                (source_id, session_id, turn_id, question, answer,
                                 tool_calls, tool_results, timestamp, source, created_at)
                            VALUES (%s, %s, %s, %s, %s,
                                    %s::jsonb, %s::jsonb, %s, %s, NOW())
                            ON CONFLICT (source_id) DO NOTHING
                            """,
                            (
                                source_id, session_id, q_turn, q, a,
                                json.dumps(pending.get("tool_calls") or [], ensure_ascii=False),
                                json.dumps(pending.get("tool_results") or [], ensure_ascii=False),
                                timestamp, "live_sync",
                            ),
                        )
                    conn.commit()
                    new_insert_committed = True
                    # A configured embedding is a durability requirement.  A
                    # raw row is still valuable, but it cannot acknowledge the
                    # durable outbox until its vector exists.
                    new_fully_durable_insert = new_insert_committed and not (
                        embed_cfg is not None and embedding_failed
                    )
                    should_ack = new_fully_durable_insert
                logger.debug(
                    "_flush_pending_qa 已写入 (session=%s turn=%s source_id=%s emb=%s)",
                    session_id, q_turn, source_id, "yes" if emb else "no",
                )
        except Exception as e:
            # Any exception means the configured durability contract is not
            # proven.  Keep the marker for a later retry or quarantine.
            logger.warning("_flush_pending_qa 写 qa_pairs 失败: %s", _safe_err(e))
            should_ack = False
            return
        finally:
            if should_ack:
                try:
                    self._ack_qa_pending(session_id, pending)
                except Exception:
                    pass
                # also handle duplicate source_id conflict already
                pass
            # compression trigger only if not fenced and acked
            if should_ack and not self._is_core_fenced():
                try:
                    from .compression_trigger import maybe_compress
                    if getattr(self, "_pg_pool", None) is not None:
                        maybe_compress(self._config, pool=self._pg_pool)
                    else:
                        maybe_compress(self._config)
                except Exception as e:
                    logger.warning("maybe_compress 触发失败(非阻塞): %s", _safe_err(e))

        # keep original duplicate handling outside PG block (already acked)

    def _flush_all_pending_qa(self, timeout: float | None = 0.5) -> None:
        """Flush every Context.pending_qa -- watchdog: durable spill + bounded drain (no sync _flush_pending_qa)."""
        # spill all pending_qa to durable first (including open q)
        try:
            self._spill_all_pending_qa_to_durable()
        except Exception:
            pass
        contexts = getattr(self, "_session_contexts", None)
        if isinstance(contexts, dict):
            with self._qa_lock:
                items = [
                    (sid, context, context.pending_qa)
                    for sid, context in list(contexts.items())
                    if context.pending_qa and context.pending_qa.get("q") and context.pending_qa.get("a")
                ]
        else:
            pending_dict = getattr(self, "_pending_qa", {})
            items = [(sid, None, pending) for sid, pending in list(pending_dict.items()) if pending and pending.get("q") and pending.get("a")]
        # async submit completed qa via durable outbox, not sync _flush_pending_qa — only clear on verifiable success
        for sid, context, pending in items:
            success = False
            try:
                success = bool(self._submit_flush(sid, pending))
                if not success:
                    try:
                        fb = self._persist_qa_pending(sid, pending)
                        if fb is not None and Path(fb).exists():
                            success = True
                    except Exception:
                        success = False
            except Exception as e:
                logger.warning("_flush_all_pending_qa submit session=%s 失败: %s", sid, _safe_err(e))
                # attempt exact per-job durable before preserving
                try:
                    fb = self._persist_qa_pending(sid, pending)
                    if fb is not None and Path(fb).exists():
                        success = True
                except Exception:
                    success = False
            if success:
                if context is not None and context.pending_qa is pending:
                    context.pending_qa = None
                elif context is None:
                    # legacy: per-job delete, retain failures
                    try:
                        pm = getattr(self, "_pending_qa", None)
                        if isinstance(pm, dict):
                            pm.pop(sid, None)
                        elif pm is not None:
                            try:
                                del pm[sid]
                            except KeyError:
                                pass
                            except Exception:
                                pass
                    except Exception:
                        pass
            # else preserve: not verifiable, keep for next retry/shutdown spill
        self._drain_flush_queue(timeout=timeout)

    def _drain_flush_queue(self, timeout: float | None = 0.5) -> None:
        """bounded drain -- never unbounded q.join()"""
        try:
            q = getattr(self, "_flush_queue", None)
            if q is None:
                return
            # bounded wait for unfinished_tasks to drain
            if timeout is None:
                timeout = 0.5
            try:
                timeout = float(timeout)
            except Exception:
                timeout = 0.5
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    # q.unfinished_tasks is protected by mutex but we can read via qsize check
                    if q.unfinished_tasks == 0:  # type: ignore
                        break
                except Exception:
                    # fallback: if we can't read, check empty
                    if q.empty():
                        break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        except Exception:
            pass

    def ingest_signals(self, signals) -> int:
        """Phase 3 入口 — 接收标准化 Signal 列表, 路由到对应处理路径.

        当前实现：仅记录日志 + 累加计数 (placeholder)。Phase 4 会把
        turn / session_end / feedback 信号真正接到现有 pipeline
        (live_buffer / j_writer / extract_from_session)。

        返回实际处理的 signal 数 (供调用方核对)。
        """
        from .signals import Signal
        if not signals:
            return 0
        n = 0
        for sig in signals:
            if not isinstance(sig, Signal):
                continue
            n += 1
            try:
                logger.debug(
                    "ingest_signal source=%s type=%s subject=%s",
                    sig.source, sig.type, sig.subject,
                )
            except Exception:
                pass
        return n

    # ── Port-friendly wrappers (Phase 4 — 六边形端口) ──
    # 这些方法不替换现有 sync_turn/on_session_end 等;
    # 只是给 HermesInputAdapter 一个稳定的入口, 让 plugin 改造时
    # 不必关心 core 内部细节.

    def ingest_turn(self, signal: Signal) -> None:
        """Port-friendly wrapper for sync_turn.

        Signal.payload 形状 (来源: hermes_turn_to_signal):
          {
            "role": "user" | "assistant",
            "content": str,
            "turn_id": int,
          }
        Signal.subject = session_id
        """
        from .signals import Signal
        if not isinstance(signal, Signal):
            return
        payload = signal.payload or {}
        role = str(payload.get("role", ""))
        content = str(payload.get("content", "") or "")
        session_id = str(signal.subject or "")
        if not session_id:
            return
        # 构造 messages 形状, 复用现有 sync_turn (写 live_buffer + j trace)
        messages = [{"id": payload.get("turn_id", 0), "role": role, "content": content}]
        try:
            self.sync_turn(session_id=session_id, messages=messages)
        except Exception as e:
            logger.warning("ingest_turn 失败: %s", _safe_err(e))

    def ingest_session_end(self, signal: Signal) -> None:
        """Port-friendly wrapper — session_end 信号.

        当前实现: 占位. session_end 真正的提炼逻辑在 plugin 的
        on_session_end 里 (extract_from_session); 未来可迁到这里.
        """
        try:
            logger.debug(
                "ingest_session_end subject=%s source=%s",
                signal.subject if signal else "", signal.source if signal else "",
            )
        except Exception:
            pass

    def ingest_session_start(self, signal: Signal) -> None:
        """Port-friendly wrapper — session_start 信号.

        当前实现: 占位. session_start 在 plugin 里通常调用
        switch_session; 未来可迁到这里.
        """
        try:
            logger.debug(
                "ingest_session_start subject=%s payload=%s",
                signal.subject if signal else "",
                (signal.payload or {}) if signal else {},
            )
        except Exception:
            pass

    def ingest_feedback(self, signal: Signal) -> None:
        """Port-friendly wrapper — feedback 信号.

        当前实现: 占位. 未来可在此处落一张 feedback 卡到碑库.
        """
        try:
            logger.debug(
                "ingest_feedback subject=%s payload_keys=%s",
                signal.subject if signal else "",
                list((signal.payload or {}).keys()) if signal else [],
            )
        except Exception:
            pass