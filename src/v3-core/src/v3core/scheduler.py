"""E1Scheduler — 内部轻量调度器，替代外部 cron

v3 系统的核心产出 (印) 不再依赖外部 crontab。Runtime-owned E1Service
为每个 Runtime 持有一套 scheduler；Runtime-backed V3Core 只负责确保该 service
启动，legacy standalone V3Core 仍可直接持有一套兼容 scheduler。

P0-C (2026-08-25): 跨进程单飞锁。gateway/desktop 多进程 + 每 session 一套
V3Core 导致多个 E1Scheduler 并存，曾产生 17 秒内重复 E1 印。现在到期 tick
必须先通过注入式 ``lock_provider``（生产 = PG advisory lock, 见 daemon.py）
拿到跨进程互斥锁才允许触发 e1：

- 未注入 lock_provider → fail-closed，tick 不执行 callback；
- 拿不到锁（别人持有）→ 本实例跳过本轮，记录 owner/age；
- 锁故障（PG 不可达等）→ 显式 lock_error 状态，绝不静默放行。

注意：这只挡 E1↔E1 竞态；observer 入口的锁接入是后续任务。

Stage 7 (2026-08-27): 可选 ``after_release_callback`` — 当 ``callback()`` 内部
mutate 了缓存相关数据时，真正的 cache invalidation 必须推迟到
``lock_provider.release()`` 成功之后执行（释放锁并归还 lease → 连接回到
pool → 此时再调用 cache.invalidate 才不会与 cache reload 形成 lease 互转）。
正常路径严格执行 ``callback() → release() → after_release_callback()``。
未持锁 / already_ran_elsewhere / busy / lock_error / release 失败分支均
不会触发 after-release 回调。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


try:
    from . import _safe_err
except (ImportError, AttributeError):
    try:
        from .. import _safe_err
    except (ImportError, AttributeError):
        def _safe_err(e, max_len=80):
            try:
                from . import _safe_err as _impl
            except ImportError:
                from .. import _safe_err as _impl
            globals()["_safe_err"] = _impl
            return _impl(e, max_len)

logger = logging.getLogger("v3core.scheduler")


class E1Scheduler:
    """印合成内部调度器

    策略：
    - 启动时计算距离上次运行的等待时间
    - 每 CHECK_INTERVAL 秒检查一次是否到期
    - 到期先取跨进程单飞锁，成功才调用 registered callback
    - shutdown() 时优雅停止
    """

    CHECK_INTERVAL = 60  # 秒 — 检查频率
    DEFAULT_PERIOD = 86400  # 秒 — 默认 24h

    def __init__(
        self,
        state_dir: str | Path,
        callback: Callable[[], str],
        lock_provider=None,
        *,
        after_release_callback: Callable[[], object] | None = None,
    ):
        self._state_dir = Path(state_dir)
        self._callback = callback
        self._event = threading.Event()
        self._thread: threading.Thread | None = None
        self._period = self.DEFAULT_PERIOD  # 可从配置读取
        self._lock_provider = lock_provider  # P0-C: 注入式单飞锁 (None = fail-closed)
        # Stage 7: 推迟执行的副作用 (e.g. cache invalidation)。仅在 callback()
        # 真正执行过且 lock_provider.release() 成功时才被调用。
        self._after_release_callback = after_release_callback
        self.last_tick_outcome: dict | None = None  # 最近一次 tick 结果（health 可读）

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.info("E1Scheduler 已在运行")
            return
        self._event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="e1-scheduler")
        self._thread.start()
        logger.info("E1Scheduler 启动 (period=%ds)", self._period)

    def _loop(self) -> None:
        while not self._event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.warning("E1Scheduler tick 异常: %s", _safe_err(e)[:100])
            self._event.wait(timeout=self.CHECK_INTERVAL)

    def tick(self) -> dict:
        """单次调度判定（可测试入口）。

        返回 outcome dict：
        - ran / reason / owner / age_seconds / error 等字段见各分支。
        - reason: not_due | no_lock_provider | lock_busy | lock_error |
                  already_ran_elsewhere | ok
        """
        last_run_outer = self._read_last_run()
        now = datetime.now(timezone.utc)  # aware — 与 lastE1Run (UTC aware) 一致
        if last_run_outer is None:
            elapsed = None
        else:
            elapsed = (now - last_run_outer).total_seconds()
        due = last_run_outer is None or elapsed >= self._period
        if not due:
            outcome = {"ran": False, "reason": "not_due"}
            self.last_tick_outcome = outcome
            return outcome
        return self._run_e1_locked(due_snapshot=last_run_outer)

    # 兼容旧内部名
    _tick = tick

    def _read_last_run(self) -> datetime | None:
        state_file = self._state_dir / "state.json"
        if not state_file.exists():
            return None
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            last_str = state.get("processed", {}).get("lastE1Run")
            if last_str:
                return datetime.fromisoformat(last_str)
        except Exception:
            pass
        return None

    def _run_e1_locked(self, due_snapshot: datetime | None = None) -> dict:
        """持单飞锁执行 e1 — P0-C 核心。

        双重校验（double-checked）：外层 tick 判定到期后，持锁成功还要再读一次
        lastE1Run —— 若别的实例刚在窗口内跑过（快照变化），本实例跳过。
        这正是 17 秒重复印的 TOCTOU 防线：锁只保证互斥，锁内复查保证同窗唯一。

        Stage 7 (2026-08-27): 严格执行 ``callback() → release() → after_release_callback()``
        顺序；``after_release_callback`` 仅在 callback 真正执行过、且 release 成功
        时才触发。already_ran_elsewhere / busy / lock_error / release 失败分支
        均不会触发 after-release（避免凭空失效或被失效异常炸掉 release）。
        """
        outcome: dict = {"ran": False}
        if self._lock_provider is None:
            outcome["reason"] = "no_lock_provider"
            outcome["error"] = "lock provider missing — refusing to run e1 unguarded"
            logger.error("E1Scheduler: %s", outcome["error"])
            self.last_tick_outcome = outcome
            return outcome
        acquired: dict | None = None
        try:
            acquired = self._lock_provider.acquire()
        except Exception as e:
            outcome["reason"] = "lock_error"
            outcome["error"] = f"lock acquire raised: {_safe_err(e)[:150]}"
            logger.error("E1Scheduler: %s", outcome["error"])
            self.last_tick_outcome = outcome
            return outcome
        if not isinstance(acquired, dict) or "granted" not in acquired:
            outcome["reason"] = "lock_error"
            outcome["error"] = f"lock provider returned invalid result: {acquired!r}"[:200]
            logger.error("E1Scheduler: %s", outcome["error"])
            self.last_tick_outcome = outcome
            return outcome
        if not acquired.get("granted"):
            if acquired.get("error"):
                outcome["reason"] = "lock_error"
                outcome["error"] = str(acquired["error"])[:200]
            else:
                outcome["reason"] = "lock_busy"
                outcome["skipped"] = True
            outcome["owner"] = acquired.get("owner", "unknown")
            if acquired.get("age_seconds") is not None:
                outcome["age_seconds"] = acquired["age_seconds"]
            logger.info(
                "E1Scheduler: 跳过本轮 e1 (%s, owner=%s)", outcome["reason"], outcome["owner"]
            )
            self.last_tick_outcome = outcome
            return outcome

        # ── 锁内二次校验：别的实例可能刚跑完并推进了 lastE1Run ──
        try:
            last_inside = self._read_last_run()
        except Exception as e:
            # 读失败按"未变化"处理，避免瞬时 IO 故障造成误跳过（e1 本身会再失败兜底）
            last_inside = due_snapshot
            logger.warning("E1Scheduler: 锁内复查 lastE1Run 失败(按到期继续): %s",
                           _safe_err(e)[:120])
        if last_inside != due_snapshot:
            outcome.update({
                "ran": False,
                "reason": "already_ran_elsewhere",
                "skipped": True,
                "owner": acquired.get("owner"),
            })
            logger.info(
                "E1Scheduler: 持锁后发现 lastE1Run 已被其他实例推进 (%s → %s), 跳过",
                due_snapshot, last_inside,
            )
            # callback() 未执行 → 不触发 after_release_callback。
            try:
                self._lock_provider.release()
            except Exception:
                pass
            self.last_tick_outcome = outcome
            return outcome

        # ── 持锁执行 ──
        outcome.update({"ran": True, "reason": "ok", "owner": acquired.get("owner")})
        # Stage 7 (T5): 必须先标记 callback_entered，才能保证 callback 抛异常
        # 但 marker 已触发时仍走 release → after-release 路径。T2 的
        # "marker 未触发则 no-op" 由 _after_release_handler 读 dirty flag
        # 自然保证（dirty=False → 直接 return）。
        callback_entered = True
        try:
            result = self._callback()
            logger.info("E1Scheduler: e1 完成 — %s", result[:200] if result else "(空)")
            outcome["result"] = str(result)[:200] if result else ""
        except Exception as e:
            # Stage 7 (T5): 即便 callback 抛异常，marker 已触发时 after-release
            # 仍要跑。callback_error 必须带异常类型名方便诊断（"RuntimeError: …"），
            # 同时保留原 _safe_err 脱敏文本，不把异常吞成 success。
            etype = type(e).__name__ or "Exception"
            outcome["callback_error"] = f"{etype}: {_safe_err(e)[:200]}"
            logger.warning("E1Scheduler: e1 执行失败: %s", _safe_err(e)[:200])
        finally:
            # ── Stage 7 严格顺序：先 release，再 after-release。
            # callback_entered=True 覆盖正常 return + 异常（marker 已触发）两条
            # 路径；marker 未触发的异常 → dirty=False → handler 自然 no-op。
            release_ok = True
            try:
                self._lock_provider.release()
            except Exception as e:
                release_ok = False
                # release 失败不能吞掉运行结果，但要留痕；会话级锁随连接回收兜底
                outcome["release_error"] = _safe_err(e)[:120]
                logger.warning("E1Scheduler: lock release 失败: %s", _safe_err(e)[:120])
            if (
                callback_entered
                and release_ok
                and self._after_release_callback is not None
            ):
                try:
                    self._after_release_callback()
                except Exception as _e_after:
                    # after-release 异常绝不能破坏本 tick 已记录的 release 成功语义
                    outcome["after_release_error"] = _safe_err(_e_after)[:200]
                    logger.warning(
                        "E1Scheduler: after_release_callback 失败 (非阻塞): %s",
                        _safe_err(_e_after)[:200],
                    )
        self.last_tick_outcome = outcome
        return outcome

    @property
    def thread(self) -> threading.Thread | None:
        """The scheduler thread handle for Runtime lifecycle observation."""
        return self._thread

    def shutdown(self, timeout: float | None = 5.0) -> None:
        """优雅停止调度器，并服从调用方剩余 shutdown deadline。"""
        self._event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            remaining = None if timeout is None else max(0.0, float(timeout))
            thread.join(timeout=remaining)
        if thread and thread.is_alive():
            raise TimeoutError("E1Scheduler thread did not stop before deadline")
        logger.info("E1Scheduler 已停止")

    @property
    def status(self) -> str:
        """状态文本，用于 format_status 显示"""
        if self._thread and self._thread.is_alive():
            last = self._read_last_run()
            if last:
                elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                return f"内部调度: ✅ 运行中 (距上次 {elapsed/3600:.1f}h)"
            return "内部调度: ✅ 运行中 (尚未触发)"
        return "内部调度: ⏸ 未启动"
