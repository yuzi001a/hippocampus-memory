"""v3-hermes-plugin — Hermes MemoryProvider 适配层 (v4.0.0)"""
from __future__ import annotations
import copy
import hashlib
import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("v3hermes")

# ── Self-bootstrap: 从 hermes venv 借第三方依赖 ──
_V3_REQUIRED = ("numpy", "requests", "psycopg2", "pgvector", "yaml")
_VENV_CANDIDATES = (
    os.path.join(os.path.expanduser("~"), ".local", "share", "hermes-agent", "venv", "lib", "python3.11", "site-packages"),
)


def _bootstrap():
    missing = [n for n in _V3_REQUIRED if n not in sys.modules]
    if not missing:
        return
    for cand in _VENV_CANDIDATES:
        if cand and os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)
            logger.info("v3hermes bootstrap: 从 hermes venv 借 %s", cand)
            break


_bootstrap()

# ── Hermes MemoryProvider ──
from agent.memory_provider import MemoryProvider
from v3core import V3Core, _safe_err
from v3core._deadline import (
    INTERNAL_PREFETCH_BUDGET_SECONDS,
    PREFETCH_CONNECT_TIMEOUT_SECONDS,
    PREFETCH_EXTERNAL_TIMEOUT_SECONDS,
    PrefetchDeadline,
    PrefetchDeadlineExceeded,
)
from v3core.session_context import V3SessionContext
from v3core.config import resolve_config
from v3core.pg_pool import PgPool
from v3core.runtime import RuntimeIdentity, RuntimeRegistry, RuntimeState, StaleRuntimeError
from v3core.tools import get_tool_schemas as _get_schemas, handle_tool_call as _handle_tool


# RuntimeRegistry owns the process-level Runtime and its PgPool.  The provider
# keeps a small lease count because several Hermes MemoryProvider instances can
# share one identity while each owns a different V3Core facade.
def _build_pg_pool(effective_config) -> PgPool:
    pg = getattr(effective_config, "pg", None)
    if pg is None:
        raise RuntimeError("v3hermes requires a configured Postgres backend")

    import psycopg2

    connect_kwargs = {
        "host": pg.host,
        "port": pg.port,
        "database": pg.database,
        "user": pg.user,
        "password": pg.password,
        # The PgPool constructor receives the same explicit bound below.
        # This is the driver-level guard that makes synchronous factory
        # admission safe for the absolute prefetch deadline.
        "connect_timeout": PREFETCH_CONNECT_TIMEOUT_SECONDS,
    }

    def connect():
        return psycopg2.connect(**connect_kwargs)

    return PgPool(
        connect=connect,
        max_connections=pg.pool_max_connections,
        min_connections=pg.pool_min_connections,
        connect_timeout=PREFETCH_CONNECT_TIMEOUT_SECONDS,
    )


_RUNTIME_REGISTRY = RuntimeRegistry(pg_pool_factory=_build_pg_pool)
_RUNTIME_LEASE_LOCK = threading.RLock()
_RUNTIME_LEASES: dict[RuntimeIdentity, int] = {}

_SUMMARY_DURABLE_VERSION = 1
_SUMMARY_SHUTDOWN_JOIN_TIMEOUT = 0.25
_SUMMARY_DURABLE_LOCK = threading.RLock()


class _SummaryPoolGuard:
    """Expose only a generation-valid shared PgPool lease to a summary job."""

    def __init__(self, provider, generation: int, runtime, pool) -> None:
        self._provider = provider
        self._generation = generation
        self._runtime = runtime
        self._pool = pool

    def lease(self, *args, **kwargs):
        if not self._provider._summary_runtime_is_current(
            self._generation, self._runtime
        ):
            identity = self._provider._runtime_identity
            if not isinstance(identity, RuntimeIdentity):
                identity = RuntimeIdentity.from_values("summary-worker", None)
            raise StaleRuntimeError(
                identity,
                f"generation-{self._generation}",
                "released",
            )
        return self._pool.lease(*args, **kwargs)


def _acquire_runtime(profile: str, hermes_home: str):
    """Resolve one profile and acquire its process-level Runtime."""
    effective_config = resolve_config(profile, hermes_home=hermes_home or "")
    identity = RuntimeIdentity.from_values(profile, hermes_home or None)
    # Keep registry acquire and lease increment atomic with the matching
    # release path.  Otherwise a last-provider shutdown could close a pool
    # between acquire() returning and the new provider recording its lease.
    with _RUNTIME_LEASE_LOCK:
        runtime = _RUNTIME_REGISTRY.acquire(identity, effective_config)
        _RUNTIME_LEASES[identity] = _RUNTIME_LEASES.get(identity, 0) + 1
    return identity, runtime


def _release_runtime(identity: RuntimeIdentity) -> None:
    """Release one provider lease; close the Runtime only at the last lease."""
    with _RUNTIME_LEASE_LOCK:
        count = _RUNTIME_LEASES.get(identity, 0)
        if count <= 1:
            # Keep the lease entry until shutdown succeeds.  A failed drain
            # must remain observable and retryable rather than being hidden.
            _RUNTIME_REGISTRY.shutdown(identity)
            _RUNTIME_LEASES.pop(identity, None)
        else:
            _RUNTIME_LEASES[identity] = count - 1


class V3HermesProvider(MemoryProvider):
    """v3-core Hermes MemoryProvider 适配 (v4.0.0)"""

    def __init__(self):
        self._core: V3Core | None = None
        self._runtime = None
        self._runtime_identity: RuntimeIdentity | None = None
        self._runtime_held = False
        self._name = "deep_memory_v3"
        self._initialized = False
        # Captured from initialize() kwargs
        self._hermes_home: str = ""
        self._profile: str = "default"
        self._platform: str = "cli"
        self._agent_context: str = "primary"
        # Retained lifecycle hooks update the current session binding explicitly.
        self._current_session_id: str = ""
        # Real contexts live in V3Core.  This fallback is used only when a
        # provider is exercised without a real Core (tests/legacy callers).
        self._local_session_contexts: dict[str, V3SessionContext] = {}
        self._SUMMARY_THRESHOLD = 15  # 累积 15 条消息触发一次摘要
        self._summary_queue: "queue.Queue" = queue.Queue()
        self._summary_worker: "threading.Thread | None" = None
        self._summary_lock = threading.Lock()
        self._summary_stop = threading.Event()
        self._summary_accepting = True
        self._summary_generation = 0
        self._summary_enqueued_jobs: set[str] = set()
        self._summary_completed_jobs: set[str] = set()

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        try:
            if self._core:
                return self._core.is_available()
            # 未初始化时轻量检查：profile config 存在即视为可用。
            # 绝不能返回 False —— agent_init 在 initialize() 之前调 is_available()
            # 决定是否 add_provider；False 导致 provider 不激活 → sync_all 无 providers
            # → v3 写入完全断链（2026-07-31 19:25 后 qa_pairs 停更实证）。
            # 也不能建临时 V3Core（旧实现泄漏连接）。
            import os as _os
            _cfg = _os.path.expanduser("~/.v3-core/profiles/default/config.yaml")
            return _os.path.exists(_cfg)
        except Exception:
            return False

    def _release_runtime_binding(self) -> bool:
        """Stop this provider's Core and release its shared Runtime lease.

        The provider records ownership immediately after ``_acquire_runtime``.
        A failed Core shutdown or Runtime drain therefore leaves the complete
        binding intact so a later shutdown/reinitialize can retry safely.
        """
        with self._summary_lock:
            # Invalidate workers that captured the old Runtime before teardown.
            self._summary_generation += 1
        self._initialized = False
        core = self._core
        if core is not None:
            try:
                core.shutdown()
            except Exception as exc:
                logger.warning("V3Core shutdown 失败: %s", _safe_err(exc))
                return False
            self._core = None

        identity = self._runtime_identity
        if not self._runtime_held:
            self._runtime = None
            self._runtime_identity = None
            self._runtime_held = False
            return True
        if identity is None or self._runtime is None:
            logger.error("Runtime ownership invariant broken: held lease has no binding")
            return False

        try:
            _release_runtime(identity)
        except Exception as exc:
            logger.warning("Runtime shutdown 失败: %s", _safe_err(exc))
            return False

        self._runtime = None
        self._runtime_identity = None
        self._runtime_held = False
        return True

    def _context_registry(self) -> dict[str, V3SessionContext]:
        core = getattr(self, "_core", None)
        contexts = getattr(core, "_session_contexts", None)
        if isinstance(contexts, dict):
            return contexts
        contexts = getattr(self, "_local_session_contexts", None)
        if not isinstance(contexts, dict):
            contexts = {}
            self._local_session_contexts = contexts
        return contexts

    def get_session_context(
        self,
        session_id: str | None = None,
        *,
        create: bool = True,
    ) -> V3SessionContext | None:
        """Return the shared Core context or the provider's offline fallback."""
        sid = session_id or getattr(self, "_current_session_id", "") or ""
        core = getattr(self, "_core", None)
        contexts = getattr(core, "_session_contexts", None)
        getter = getattr(core, "get_session_context", None)
        if isinstance(contexts, dict) and callable(getter):
            return getter(sid, create=create)
        registry = self._context_registry()
        if not create:
            return registry.get(sid)
        context = registry.get(sid)
        if context is None:
            context = V3SessionContext(session_id=sid)
            registry[sid] = context
        return context

    @property
    def session_contexts(self) -> dict[str, V3SessionContext]:
        return self._context_registry()

    @property
    def _session_msg_counters(self) -> dict[str, int]:
        """Compatibility snapshot; Context.msg_counter is authoritative."""
        return {sid: context.msg_counter for sid, context in self._context_registry().items()}

    def initialize(self, session_id: str, **kwargs) -> None:
        """Hermes ABC: initialize with session context."""
        try:
            self._hermes_home = str(kwargs.get("hermes_home", "") or "")
            self._profile = str(kwargs.get("profile", "default") or "default")
            self._platform = kwargs.get("platform", "cli")
            self._agent_context = kwargs.get("agent_context", "primary")

            if self._agent_context not in ("primary", "cli"):
                if not self._release_runtime_binding():
                    logger.warning("V3HermesProvider skip init: previous Runtime binding is still held")
                    return
                logger.info(
                    "V3HermesProvider skip full init (context=%s, platform=%s)",
                    self._agent_context,
                    self._platform,
                )
                return

            # Never replace a live Core/Runtime binding.  A failed cleanup is
            # retryable; it must block a second acquire for this provider.
            old_worker = self._summary_worker
            if old_worker is not None and old_worker.is_alive():
                logger.warning("V3HermesProvider initialize aborted: summary worker still running")
                return
            with self._summary_lock:
                self._summary_accepting = True
                self._summary_stop.clear()
            if not self._release_runtime_binding():
                logger.warning("V3HermesProvider initialize aborted: previous binding is not released")
                return
            # Resolve config once inside _acquire_runtime.  RuntimeRegistry
            # freezes that effective snapshot and returns the singleton pool.
            runtime_identity, runtime = _acquire_runtime(self._profile, self._hermes_home)

            # Lease ownership starts here, before any Core constructor or
            # initialize hook can fail.  Cleanup can therefore always retry.
            self._runtime_identity = runtime_identity
            self._runtime = runtime
            self._runtime_held = True

            try:
                core_kwargs = {
                    "profile": self._profile,
                    "hermes_home": self._hermes_home or None,
                    "pg_pool": runtime.pg_pool,
                }
                # Legacy/unit doubles may expose only pg_pool; only a real
                # Runtime-owned cache needs the owner handoff.
                if hasattr(runtime, "topic_recall_cache"):
                    core_kwargs["runtime"] = runtime
                effective_config = getattr(runtime, "effective_config", None)
                if effective_config is not None:
                    core_kwargs["effective_config"] = effective_config
                core = V3Core(**core_kwargs)
                self._core = core
                core.initialize()
            except Exception:
                # _release_runtime_binding retains ownership if either Core
                # shutdown or Runtime drain fails; do not clear fields here.
                self._release_runtime_binding()
                raise

            self._initialized = True
            self._restore_pending_summaries()
            if session_id:
                self._current_session_id = session_id
                context = self.get_session_context(session_id)
                if context is not None:
                    context.summary_lifecycle["state"] = "active"
            logger.info(
                "V3HermesProvider initialize (session=%s, platform=%s)",
                session_id,
                self._platform,
            )
        except StaleRuntimeError:
            # Config generation conflicts must be visible to the caller; a
            # stale request must never look like a successful initialization.
            raise
        except Exception as e:
            logger.warning("V3HermesProvider initialize 失败: %s", _safe_err(e))

    def get_tool_schemas(self) -> list:
        """返回 13 工具 schema (与 plugin.yaml provides_tools 对齐)

        2026-08-07 收窄: 此前全量返回 31 个 (TOOL_REGISTRY 全集), 远超官方
        memory provider 工具面 (honcho 5 / mem0 4 / retaindb 10 最大)。
        收窄后模型可见面 = 统一入口 4 + 高频核心 5 + 主题/手帐 3 + 健康 1,
        其余 18 个内部工具由统一入口 (v3_add/v3_get/v3_update/v3_manage)
        转发覆盖, 功能零丢失。v3-core TOOL_REGISTRY 保持全量不动。
        """
        exposed = {
            # 统一入口 (4)
            "v3_add", "v3_get", "v3_update", "v3_manage",
            # 高频核心 (5)
            "v3_store", "v3_search", "v3_status", "v3_extract", "v3_prefetch",
            # 主题/手帐 (3)
            "v3_topic_correct", "v3_moc_overview", "v3_moc_get",
            # 健康 (1)
            "v3_health",
        }
        return [s for s in _get_schemas() if s.get("name") in exposed]

    def handle_tool_call(self, name: str, args: dict, **kw) -> str:
        """工具调用 dispatch — 优先走 V3Core 实例"""
        if not self._core:
            return '{"success": false, "error": "V3Core 未初始化"}'
        # v3_prefetch / v3_get_message_context 需要 V3Core 实例
        if name == 'v3_prefetch':
            import json
            try:
                results = self._core.prefetch(args.get('query', ''), args.get('limit', 5))
                from v3core.prefetch import format_prefetch
                fmt = args.get('format', 'json')
                return format_prefetch(results, fmt)
            except Exception as e:
                from v3core import _safe_err; return json.dumps({"success": False, "error": _safe_err(e)})
        if name == 'v3_get_message_context':
            import json
            try:
                result = self._core.get_message_context(args.get('source_id', ''))
                return result
            except Exception as e:
                from v3core import _safe_err; return json.dumps({"success": False, "error": _safe_err(e)})
        # 其余工具也必须复用当前 Runtime-backed Core/Pool；不能让
        # handler 内部再无参构造 V3Core 或重新 resolve 生产配置。
        return _handle_tool(
            name,
            args,
            core=self._core,
            effective_config=self._core.config,
            pool=self._core.pg_pool,
            pg_pool=self._core.pg_pool,
            runtime_context=True,
        )

    def prefetch(self, query: str, *, session_id: str = "", timeout: float | None = None) -> str:
        """prefetch — 返回可注入 context block (走 MemoryInjector 统一注入)

        2026-08-18 统一注入入口 (设计：docs/provider-injection-budget-design-20260818.md):
          - Provider.prefetch 只调用 self._core.build_memory_context(query, session_id=...);
          - 不再手工调 v3core.observer.recall_for_new_session 和拼接 [新会话记忆召回] 块;
          - 首轮召回 + 总预算由统一注入器内部按 session 幂等 + inject.max_chars 完成。
        """
        if not query:
            logger.debug("prefetch 跳过: query 为空")
            return ""
        if not self._core:
            logger.warning("prefetch 跳过: V3Core 未初始化")
            return ""
        requested_budget = (
            PREFETCH_EXTERNAL_TIMEOUT_SECONDS
            if timeout is None
            else float(timeout)
        )
        budget = min(requested_budget, INTERNAL_PREFETCH_BUDGET_SECONDS)
        deadline = PrefetchDeadline(budget_s=budget)
        started = time.monotonic()
        try:
            block = self._core.build_memory_context(
                query,
                session_id=session_id,
                deadline=deadline,
            )
            if not block:
                logger.info("prefetch 未召回任何结果 (query=%r, session=%s)", query, session_id)
            return block
        except PrefetchDeadlineExceeded:
            logger.info(
                "prefetch internal deadline exceeded elapsed=%.3fs budget=%.3fs query_len=%d",
                time.monotonic() - started,
                budget,
                len(query),
            )
            return ""
        except Exception as e:
            logger.warning("prefetch 失败: %s", _safe_err(e))
            return ""

    def system_prompt_block(self) -> str:
        """身份核心注入 — 从 MemoryInjector 获取 (含去重)"""
        if not self._core:
            return ""
        try:
            session_id = getattr(self, "_current_session_id", "") or ""
            if session_id:
                try:
                    block = self._core.build_memory_context("", session_id=session_id)
                except TypeError:
                    block = self._core.build_memory_context("")
            else:
                block = self._core.build_memory_context("")
            if block:
                return block
        except Exception as e:
            logger.warning("system_prompt_block 失败: %s", _safe_err(e))
        return ""

    def _summary_base_path(self) -> Path | None:
        core = getattr(self, "_core", None)
        getter = getattr(core, "_get_base_path", None)
        if not callable(getter):
            return None
        try:
            raw_base = getter()
            if type(raw_base).__module__.startswith("unittest.mock"):
                return None
            if not isinstance(raw_base, (str, os.PathLike)):
                return None
            base = Path(raw_base)
            return base if str(base) else None
        except (OSError, TypeError, ValueError, RuntimeError):
            return None

    @staticmethod
    def _summary_job_id(session_id: str, messages: list) -> str:
        payload = json.dumps(
            {"session_id": session_id, "messages": messages},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _summary_pending_path(self, job_id: str, *, base: Path | None = None) -> Path | None:
        base = base or self._summary_base_path()
        if base is None:
            return None
        return base / "j" / "pending_session_summaries" / f"{job_id}.json"

    def _persist_summary_pending(
        self, job_id: str, session_id: str, messages: list, *, base: Path
    ) -> Path | None:
        path = self._summary_pending_path(job_id, base=base)
        if path is None:
            return None
        payload = {
            "version": _SUMMARY_DURABLE_VERSION,
            "job_id": job_id,
            "session_id": session_id,
            "messages": copy.deepcopy(messages),
            "state": "pending",
        }
        tmp = None
        try:
            with _SUMMARY_DURABLE_LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    return path
                tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(str(tmp), str(path))
            return path
        except Exception as exc:
            logger.warning("summary pending durable write failed: %s", _safe_err(exc))
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
            return None

    def _clear_summary_pending(self, job_id: str) -> None:
        path = self._summary_pending_path(job_id)
        if path is None:
            return
        try:
            with _SUMMARY_DURABLE_LOCK:
                path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("summary pending ack failed: %s", _safe_err(exc))

    def _restore_pending_summaries(self) -> None:
        base = self._summary_base_path()
        if base is None:
            return
        pending_dir = base / "j" / "pending_session_summaries"
        try:
            paths = sorted(pending_dir.glob("*.json"))
        except (OSError, RuntimeError):
            return
        for path in paths:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                session_id = str(item.get("session_id") or "")
                messages = item.get("messages")
                if not session_id or not isinstance(messages, list):
                    logger.warning("invalid summary pending marker ignored: %s", path.name)
                    continue
                job_id = str(item.get("job_id") or self._summary_job_id(session_id, messages))
                if job_id in self._summary_completed_jobs:
                    self._clear_summary_pending(job_id)
                    continue
                self._submit_summary(session_id, messages)
            except Exception as exc:
                logger.warning("summary pending recovery failed for %s: %s", path.name, _safe_err(exc))

    def _summary_runtime_is_current(self, generation: int, runtime) -> bool:
        with self._summary_lock:
            if (
                generation != self._summary_generation
                or not self._runtime_held
                or self._runtime is not runtime
            ):
                return False
        state = getattr(runtime, "state", None)
        if state in (RuntimeState.DRAINING, RuntimeState.CLOSED):
            return False
        return True

    def _submit_summary(self, session_id: str, messages: list) -> None:
        """Persist then enqueue one deduplicated summary job."""
        if not session_id or not messages:
            return
        messages_snapshot = copy.deepcopy(messages)
        job_id = self._summary_job_id(session_id, messages_snapshot)
        with self._summary_lock:
            if job_id in self._summary_enqueued_jobs or job_id in self._summary_completed_jobs:
                return
            accepting = self._summary_accepting
        base = self._summary_base_path()
        if base is not None:
            if self._persist_summary_pending(
                job_id, session_id, messages_snapshot, base=base
            ) is None:
                return
        if not accepting:
            return
        context = self.get_session_context(session_id)
        with self._summary_lock:
            if job_id in self._summary_enqueued_jobs or job_id in self._summary_completed_jobs:
                return
            if not self._summary_accepting:
                return
            if context is not None:
                lifecycle = context.summary_lifecycle
                lifecycle["pending"] = int(lifecycle.get("pending", 0)) + 1
            if self._summary_worker is None or not self._summary_worker.is_alive():
                self._summary_worker = threading.Thread(
                    target=self._summary_worker_loop, name="v3-summary", daemon=True
                )
                self._summary_worker.start()
            self._summary_enqueued_jobs.add(job_id)
            self._summary_queue.put((session_id, messages_snapshot))

    def _run_summary_item(self, session_id: str, messages: list) -> bool:
        """Run one summary only while this provider owns its Runtime."""
        job_id = self._summary_job_id(session_id, messages)
        context = self.get_session_context(session_id, create=False)
        with self._summary_lock:
            if not self._summary_accepting:
                return False
            generation = self._summary_generation
            runtime = self._runtime if self._runtime_held else None
            core = self._core
        try:
            if runtime is None or core is None:
                logger.warning("session 摘要跳过: provider 没有 active Runtime binding")
                return False
            if not self._summary_runtime_is_current(generation, runtime):
                return False
            from v3core.session_summary import summarize_from_messages
            config = getattr(runtime, "effective_config", None)
            if config is None:
                config = core.config
            pool = _SummaryPoolGuard(self, generation, runtime, runtime.pg_pool)
            summarize_from_messages(
                session_id,
                messages,
                config=config,
                pool=pool,
            )
            completed = True
        except Exception as exc:
            logger.warning("session 摘要处理失败 (非致命): %s", _safe_err(exc))
            completed = False
        finally:
            if context is not None:
                with self._summary_lock:
                    lifecycle = context.summary_lifecycle
                    lifecycle["pending"] = max(
                        0, int(lifecycle.get("pending", 0)) - 1
                    )
        if completed and self._summary_runtime_is_current(generation, runtime):
            self._clear_summary_pending(job_id)
            with self._summary_lock:
                self._summary_completed_jobs.add(job_id)
            return True
        return False

    def _summary_worker_loop(self) -> None:
        """后台 worker — 顺序处理 session 摘要队列 (FIFO)"""
        while not self._summary_stop.is_set():
            try:
                session_id, messages = self._summary_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            job_id = self._summary_job_id(session_id, messages)
            try:
                with self._summary_lock:
                    accepting = self._summary_accepting
                if accepting:
                    self._run_summary_item(session_id, messages)
            finally:
                self._summary_queue.task_done()
                with self._summary_lock:
                    self._summary_enqueued_jobs.discard(job_id)

    def sync_turn(self, user_content: str, assistant_content: str, *,
                   session_id: str = "", messages: list | None = None) -> None:
        """sync_turn — live buffer enqueue + 计数触发摘要"""
        # V3_SYNC_DISABLED=1 时跳过写入，用于数据重建期间
        if os.environ.get("V3_SYNC_DISABLED", "") == "1":
            return
        logger.info("sync_turn 调用: session=%s, user=%s, msgs=%s",
                    session_id, str(user_content)[:40], len(messages) if messages else 0)
        # F4-FIX-syncturn: messages 缺失或为空时，用 user_content/assistant_content 构造
        # 一对标准消息，避免上游只传两个 content 时静默丢 turn 漏记记忆。
        # sync_turn carries the session_id for the retained lifecycle hooks.
        if session_id:
            self._current_session_id = session_id

        if not messages:
            msgs: list[dict] = []
            if user_content:
                msgs.append({"role": "user", "content": user_content})
            if assistant_content:
                msgs.append({"role": "assistant", "content": assistant_content})
            messages = msgs
        if self._core:
            self._core.sync_turn(session_id, messages)

        # 消息计数 — 每 SUMMARY_THRESHOLD 条调一次摘要
        if not session_id:
            return
        context = self.get_session_context(session_id)
        if context is None:
            return
        context.msg_counter += 1
        context.summary_lifecycle["state"] = "active"
        count = context.msg_counter
        if count % self._SUMMARY_THRESHOLD == 0 and messages:
            self._submit_summary(session_id, messages)

    def shutdown(self) -> None:
        """Quiesce summary dispatch, preserve jobs, then release the owner Runtime."""
        with self._summary_lock:
            self._summary_accepting = False
            self._summary_stop.set()
            self._summary_generation += 1
            worker = self._summary_worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=_SUMMARY_SHUTDOWN_JOIN_TIMEOUT)
        if worker is not None and worker.is_alive():
            logger.warning(
                "V3HermesProvider shutdown deferred: summary worker still running; "
                "pending markers retained"
            )
        while True:
            try:
                self._summary_queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._summary_queue.task_done()
        with self._summary_lock:
            self._summary_enqueued_jobs.clear()
        if not self._release_runtime_binding():
            logger.warning("V3HermesProvider shutdown retained Runtime binding for retry")
            return
        if worker is None or not worker.is_alive():
            self._summary_worker = None
        logger.info("V3HermesProvider shutdown")

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        """session 切换 — 刷新 V3Core session context"""
        old_session_id = getattr(self, "_current_session_id", "") or ""
        if not self._core:
            if new_session_id:
                self._current_session_id = new_session_id
                context = self.get_session_context(new_session_id)
                if context is not None:
                    context.summary_lifecycle["state"] = "active"
            return
        try:
            self._core.switch_session(
                new_session_id,
                parent_session_id=parent_session_id,
                reset=reset,
                rewound=rewound,
            )
            # Update the binding only after the retained switch succeeds.
            # 空新 id 不清空现有绑定（官方 manager 对空 id 直接早退）。
            if new_session_id:
                if old_session_id and old_session_id != new_session_id:
                    old_context = self.get_session_context(old_session_id, create=False)
                    if old_context is not None:
                        old_context.summary_lifecycle["state"] = "suspended"
                self._current_session_id = new_session_id
                new_context = self.get_session_context(new_session_id)
                if new_context is not None:
                    new_context.summary_lifecycle["state"] = "active"
            logger.info(
                "on_session_switch → %s (parent=%s, reset=%s, rewound=%s)",
                new_session_id, parent_session_id, reset, rewound,
            )
        except Exception as e:
            logger.debug("on_session_switch 失败: %s", _safe_err(e))

    def on_pre_compress(self, messages) -> str:
        """压缩前 — 先卸大工具输出，再从清理后的消息捞高信号"""
        if not self._core:
            return ""
        try:
            session_id = getattr(self, "_current_session_id", "") or ""
            context = self.get_session_context(session_id, create=False) if session_id else None
            if context is not None:
                context.summary_lifecycle["state"] = "pre_compress"
            if session_id:
                self._core.offload_messages(session_id, messages)
            snippets = self._core.get_high_signal_snippets(messages)
            if context is not None:
                context.summary_lifecycle["state"] = "active"
            return snippets
        except Exception as e:
            logger.debug("on_pre_compress 失败: %s", _safe_err(e))
            return ""

    # ── backup ──

    def backup_paths(self) -> list[str]:
        """返回 v3-core 数据根目录 (外置 HERMES_HOME 的数据)"""
        try:
            root = V3Core.get_v3_data_root()
            if root:
                return [str(root)]
        except Exception as e:
            logger.debug("backup_paths 失败: %s", _safe_err(e))
        return []

    # ── delegation ──

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        """子代理完成 — 记录到 j/ 迹层"""
        if not self._core:
            return
        try:
            self._core.append_journal(
                f"## 子代理: {task[:120]}\n\n结果: {result[:500]}"
            )
            logger.debug("on_delegation 已记录到迹层 (child=%s)", child_session_id)
        except Exception as e:
            logger.debug("on_delegation 失败: %s", _safe_err(e))

    # ── intentionally no-op hooks (via ABC defaults) ──
    # on_turn_start: 核心自管理 turn 计数
    # on_memory_write: 防双写 (v3 通过 sync_turn+ingest 已完整记录)
    # queue_prefetch: 核心自管理 prefetch

    # ── config ──

    def get_config_schema(self) -> list:
        """暴露 v3 配置给 `hermes memory setup` 命令行向导

        字段说明:
          - secret=True → save_config 跳过, 走 env_var (V3_EMBED_API_KEY / V3CORE_PG_PASSWORD
            / V3CORE_LLM_API_KEY) 注入; 不在磁盘 yaml 留痕.
          - secret=False → save_config 写入 ~/.v3-core/profiles/default/config.yaml
            对应位置 (storage.pg.host/port/database/user, llm.provider/model/base_url).
        """
        return [
            {
                "key": "embed_endpoint",
                "description": "Embed 服务 API 端点 (bge-large-zh-v1.5)",
                "default": "http://localhost:9999/v1/embeddings",
                "required": False,
            },
            {
                "key": "embed_api_key",
                "description": "Embed 服务 API Key",
                "secret": True,
                "env_var": "V3_EMBED_API_KEY",
                "required": False,
            },
            {
                "key": "rerank_endpoint",
                "description": "Rerank 服务 API 端点 (可选)",
                "default": "http://localhost:9998/v1/rerank",
                "required": False,
            },
            {
                "key": "pg_host",
                "description": "Postgres host",
                "default": "localhost",
                "required": False,
            },
            {
                "key": "pg_port",
                "description": "Postgres port",
                "default": 5433,
                "required": False,
            },
            {
                "key": "pg_database",
                "description": "Postgres database name",
                "default": "v3embeddings",
                "required": False,
            },
            {
                "key": "pg_user",
                "description": "Postgres user",
                "default": "v3user",
                "required": False,
            },
            {
                "key": "pg_password",
                "description": "Postgres password (建议通过 V3CORE_PG_PASSWORD 环境变量注入)",
                "secret": True,
                "env_var": "V3CORE_PG_PASSWORD",
                "required": False,
            },
            {
                "key": "llm_provider",
                "description": "LLM provider 标识（如 minimax-cn / opencode-zen），用于观察者/摘要等提炼任务",
                "default": "",
                "required": False,
            },
            {
                "key": "llm_model",
                "description": "LLM model 名 (provider 对应的具体模型)",
                "default": "",
                "required": False,
            },
            {
                "key": "llm_base_url",
                "description": "LLM 自定义 base_url (空 = 按 provider 用官方默认)",
                "default": "",
                "required": False,
            },
            {
                "key": "llm_api_key",
                "description": "LLM API key (建议通过 V3CORE_LLM_API_KEY 环境变量注入)",
                "secret": True,
                "env_var": "V3CORE_LLM_API_KEY",
                "required": False,
            },
        ]

    def save_config(self, values: dict, hermes_home: str) -> None:
        """非敏感配置写入 v3-core config.yaml

        secret 字段 (embed_api_key / pg_password / llm_api_key) 不在此处落盘 —
        它们走 env_var (V3_EMBED_API_KEY / V3CORE_PG_PASSWORD / V3CORE_LLM_API_KEY),
        调用方需要在 yaml 写入前自行 export 到 os.environ 或 ~/.v3-core/.env.

        写入路径: 优先用 hermes_home/.v3-core/profiles/default/config.yaml,
        其次 ~/.v3-core/profiles/default/config.yaml (本机生产), 最后
        ~/.v3-core/config.yaml. 与 _find_config 的查找顺序保持一致.
        """
        if not values:
            return
        try:
            from pathlib import Path
            import yaml as _yaml

            # 确定 v3 配置路径 — 与 config._find_config 查找顺序一致
            cfg_path = None
            candidates = [
                Path.home() / ".v3-core" / "profiles" / "default" / "config.yaml",
                Path.home() / ".v3-core" / "config.yaml",
            ]
            if hermes_home:
                candidates.insert(
                    1,
                    Path(hermes_home) / ".v3-core" / "profiles" / "default" / "config.yaml",
                )
            for p in candidates:
                if p.exists():
                    cfg_path = p
                    break
            if cfg_path is None:
                # 全新安装: 无已存在 config — 在 hermes_home 路径(若有)或老路径创建
                cfg_path = (
                    Path(hermes_home) / ".v3-core" / "profiles" / "default" / "config.yaml"
                    if hermes_home else Path.home() / ".v3-core" / "profiles" / "default" / "config.yaml"
                )
                try:
                    cfg_path.parent.mkdir(parents=True, exist_ok=True)
                    logger.info("save_config 创建新 config: %s", cfg_path)
                except Exception as _mk_err:
                    logger.warning("save_config 创建目录失败: %s", _safe_err(_mk_err))
                    return

            # 文件不存在(全新安装) → 空 dict 开始; 存在 → 读入合并
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = _yaml.safe_load(f) or {}
            else:
                cfg = {}

            # 写 embed 配置
            if values.get("embed_endpoint"):
                cfg.setdefault("storage", {}).setdefault("embed", {})
                cfg["storage"]["embed"]["endpoint"] = values["embed_endpoint"]

            if values.get("rerank_endpoint"):
                cfg.setdefault("storage", {}).setdefault("rerank", {})
                cfg["storage"]["rerank"]["endpoint"] = values["rerank_endpoint"]

            # 写 PG 配置 — host / port / database / user (password 走 env, 不落盘)
            pg_block = cfg.setdefault("storage", {}).setdefault("pg", {})
            if values.get("pg_host") is not None:
                pg_block["host"] = values["pg_host"]
            if values.get("pg_port") is not None:
                pg_block["port"] = int(values["pg_port"])
            if values.get("pg_database") is not None:
                pg_block["database"] = values["pg_database"]
            if values.get("pg_user") is not None:
                pg_block["user"] = values["pg_user"]
            # pg_password 是 secret → 不写 yaml; 调用方需通过 V3CORE_PG_PASSWORD env 注入

            # 写 LLM 配置 — provider / model / base_url (api_key 走 env, 不落盘)
            llm_block = cfg.setdefault("llm", {})
            if values.get("llm_provider") is not None:
                llm_block["provider"] = values["llm_provider"]
            if values.get("llm_model") is not None:
                llm_block["model"] = values["llm_model"]
            if values.get("llm_base_url") is not None:
                llm_block["base_url"] = values["llm_base_url"]
            # llm_api_key 是 secret → 不写 yaml; 调用方需通过 V3CORE_LLM_API_KEY env 注入

            with open(cfg_path, "w", encoding="utf-8") as f:
                _yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

            logger.info("save_config 已写入 %s: %s", cfg_path, list(values.keys()))
        except Exception as e:
            logger.warning("save_config 失败: %s", _safe_err(e))


def register(ctx) -> None:
    """Hermes plugin 注册入口 — 兼容 __init__.py 直接调"""
    try:
        provider = V3HermesProvider()
        ctx.register_memory_provider(provider)
        logger.info("V3HermesProvider registered (via ctx.register_memory_provider)")
    except Exception as e:
        logger.warning("V3HermesProvider 注册失败: %s", _safe_err(e))
    # 注意: memory provider 插件走 plugins/memory 的 _ProviderCollector (fake ctx),
    # 没有 register_skill — skill 注册是普通插件 (PluginContext) 的能力。
    # v3-workflow skill 改走用户 skill 目录 (~/.hermes/skills/v3-workflow/)。


def get_provider() -> V3HermesProvider:
    """工厂函数 — MemoryManager 调用"""
    return V3HermesProvider()
