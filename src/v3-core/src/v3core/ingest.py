"""6 项 ingest 引擎 — 处理迹->碑->pg 全链路"""
from __future__ import annotations
import json
import logging
import os
import time
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


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

logger = logging.getLogger("v3core.ingest")
import hashlib
from .embedding import call_embedding, STREAM_PRIMARY_EMBED_POLICY, BATCH_EMBED_POLICY
from .embed_failures import embed_for_write, EmbedOutcomeStatus
from .config import resolve_config, _resolve_data_dir

# ── LiveBuffer (实时 ingest) ──

LIVE_BATCH = 8
LIVE_FLUSH_SEC = 30.0
# 2026-08-27 pool-owner 回归修复: 断线重连参数显式化
LIVE_RECONNECT_THROTTLE_SEC = 5.0        # 重连节流窗口(秒), 语义与原硬编码 5.0 一致
LIVE_RECONNECT_LEASE_TIMEOUT_SEC = 5.0   # pool-backed 探测的有界 lease 上限(秒)

class LiveBuffer:
    """sync_turn -> queue -> writer 线程 -> pg"""

    def __init__(self, pg=None, config=None):
        """兼容 V3Config | dict | None."""
        self._q: queue.Queue[tuple[str, str, str, str, str]] = queue.Queue()  # session_id, msg_id, content, role, turn_id
        self._writer: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_flush_ts: float = 0
        self._stop = threading.Event()
        self._pg = pg
        self._pg_config = config  # 存 config 用于断线重连
        # 2026-08-27 pool-owner fix: 若 pg 是 pool-backed store (携带 .pool owner),
        # 记录该共享 PgPool — 断线重连时以 ``PgEmbedStore(config, pool=shared_pool)``
        # 重建并保留同一 owner, 绝不退回 direct (无池) 构造。
        # 只从传入的 pool-backed store 推导 pool, 不触碰 v3core/__init__.py 的 wiring。
        self._shared_pool = self._derive_shared_pool(pg)
        self._last_reconnect_ts: float = 0.0  # 距上次重连时间(秒),用于节流
        # --- durability/fence generation (RC1/RC5/RC6) ---
        self._accepting = True
        self._fenced = False
        self._closed = False
        self._inflight: list = []
        self._inflight_lock = threading.Lock()
        self._generation = 0
        self._durability_lock = threading.Lock()
        # 2026-09-07 compression-aware ingest durability (C):
        # in-memory pending job set — dedupes same-job re-enqueues within
        # this LiveBuffer instance, and lets ``ack_live_item`` (post-PG
        # write) clear the slot so the next legitimate enqueue of the
        # same key is treated as fresh.  Persisted durable markers on
        # disk remain the authoritative cross-process record; this set
        # is the in-process idempotency cache.
        self._pending_jobs: set[str] = set()
        self._pending_jobs_lock = threading.Lock()

        # 从 config 提取 (V3Config / dict / None)
        embed_cfg, ib = self._extract_cfg(config)
        self._embed_cfg = embed_cfg
        if ib:
            self._batch = ib.get("batch", LIVE_BATCH)
            self._flush_sec = ib.get("flush_sec", LIVE_FLUSH_SEC)
        else:
            self._batch = LIVE_BATCH
            self._flush_sec = LIVE_FLUSH_SEC
        # deterministic recovery of prior generation markers
        try:
            self._recover_live_pending()
        except Exception:
            pass

    @staticmethod
    def _extract_cfg(config):
        """兼容 V3Config / dict / None. 返回 (embed_cfg_dict, ingest_block_dict).

        阶段1 (2026-08-20): embed_cfg 改走 ``safe_embed_cfg`` 工厂.
        * cfg 为 None / 无 embed 子配置 → embed_cfg = None (disabled)
        * cfg 有 embed 但缺 model/endpoint → ValueError (fail-closed, 不吞配置错误)
        * 正常配置 → 返回 ``build_embed_cfg(cfg)`` 产出的 dict (含 _fingerprint)
        """
        if config is None:
            return None, None
        # V3Config
        if not isinstance(config, dict) and hasattr(config, "embed") and hasattr(config, "ingest"):
            from .embedding import safe_embed_cfg as _safe_embed_cfg
            embed_cfg = _safe_embed_cfg(config)
            ib = {
                "batch": config.ingest.live_buffer_batch,
                "flush_sec": config.ingest.live_buffer_flush_sec,
            }
            return embed_cfg, ib
        # dict
        if isinstance(config, dict):
            from .embedding import safe_embed_cfg as _safe_embed_cfg
            embed_cfg = _safe_embed_cfg(config)
            ib = config.get("ingest", {}).get("live_buffer", {}) or {}
            return embed_cfg, ib
        return None, None

    def enqueue(self, session_id: str, msg_id: str, content: str = '', role: str = '',
                turn_id: str = '', timestamp=None, tool_calls=None, tool_results=None) -> bool | None:
        """Enqueue a live message.

        Return contract (2026-09-07 compression-aware durability, B + C):
          * ``True``  — accepted: durable marker written and (when writer
            is running) the item has been queued for PG insert.
          * ``False`` — explicitly rejected: fence closed / no durable
            marker / no fallback.  Caller MUST treat this as "not yet
            accepted" and retry the next sync.
          * ``None``  — legacy accepted (backward-compatible).  Pre-C
            callers and ``MagicMock`` instances that don't implement the
            bool return contract still get this value, which Python
            ``if _ok is False`` checks treat as "accepted".

        Idempotency (C): if a pending job for the same content-hash
        already exists in this LiveBuffer instance, the enqueue is a
        no-op and returns ``True`` (already accepted previously).  The
        disk-side durable marker is the cross-process guard; the
        in-memory ``_pending_jobs`` is the same-process dedupe.
        """
        if not session_id or not msg_id:
            return False
        # fence: after shutdown, reject new accepts
        if getattr(self, '_closed', False) or getattr(self, '_fenced', False) or not getattr(self, '_accepting', True):
            return False
        try:
            from . import _parse_msg_timestamp
            timestamp = _parse_msg_timestamp(timestamp)
        except (ImportError, AttributeError):
            pass
        item = (session_id, msg_id, content, role, turn_id, timestamp, tool_calls, tool_results)
        # Cross-process accepted/pending identity gate: do this before
        # computing or persisting a new content-sensitive job id.
        if self.has_durable_marker(session_id, msg_id, content, role, turn_id,
                                   timestamp, tool_calls, tool_results):
            return True
        # Compute the same job_id we will persist on disk; cheap sha256
        # over the deterministic payload.
        try:
            job_id = self._live_job_id(item)
        except Exception:
            job_id = None
        # In-process dedupe: if same job is already pending in this
        # LiveBuffer instance, treat as already accepted and return True.
        if job_id is not None:
            with self._pending_jobs_lock:
                if job_id in self._pending_jobs:
                    return True
        # durable outbox first: atomic temp+flush+fsync+replace
        persisted = False
        try:
            persisted = self._persist_live_item(item)
        except Exception:
            persisted = False
        if not persisted:
            # no silent loss: if durable marker cannot be created, use legacy _lost fallback
            # and do NOT pretend to have accepted without verifiable evidence for THIS item
            fallback_ok = False
            try:
                self._write_fallback([item])
                # verify fallback for this specific session/item was created
                from .config import _resolve_data_dir
                base = _resolve_data_dir(self._pg_config)
                safe_sid = str(session_id).replace("..", "_").replace("/", "_").replace("\\", "_")
                fallback_path = base / "j" / "_lost" / safe_sid / "turns.md"
                if fallback_path.exists():
                    # ensure fallback contains this msg_id (verifiable)
                    try:
                        txt = fallback_path.read_text(encoding="utf-8", errors="ignore")
                        if msg_id in txt:
                            fallback_ok = True
                        else:
                            # fallback file exists but not for this item -> still consider not verifiable
                            fallback_ok = False
                    except Exception:
                        fallback_ok = fallback_path.stat().st_size > 0
            except Exception:
                fallback_ok = False
            if not fallback_ok:
                logger.warning("LiveBuffer enqueue rejected: durable marker failed and verifiable fallback not available for %s/%s", session_id, msg_id)
                return False
        # Reserve the in-process pending slot BEFORE the queue put, so
        # even a re-entrant enqueue of the same job (within the same
        # tick) cannot race past us into a double-write.
        if job_id is not None:
            with self._pending_jobs_lock:
                self._pending_jobs.add(job_id)
        with self._lock:
            if self._writer is None or not self._writer.is_alive():
                # do not restart if fenced/closed
                if getattr(self, '_closed', False) or getattr(self, '_fenced', False) or not getattr(self, '_accepting', True):
                    pass
                else:
                    self._start_writer()
        try:
            self._q.put(item)
        except Exception:
            # enqueue failure: drop the reserved slot so the next call
            # can retry.  Do NOT swallow silently — surface as False.
            if job_id is not None:
                with self._pending_jobs_lock:
                    self._pending_jobs.discard(job_id)
            return False
        return True
    
    def _start_writer(self):
        if getattr(self, '_closed', False) or getattr(self, '_fenced', False) or not getattr(self, '_accepting', True):
            return
        self._stop.clear()
        self._writer = threading.Thread(target=self._run, name="v3-live-writer", daemon=True)
        self._writer.start()

    def _live_pending_dir(self):
        try:
            base = _resolve_data_dir(self._pg_config)
            if not base:
                return None
            return Path(base) / "j" / "pending_live_buffer"
        except Exception:
            return None

    def _live_accepted_dir(self):
        try:
            base = _resolve_data_dir(self._pg_config)
            if not base:
                return None
            return Path(base) / "j" / "accepted_live_buffer"
        except Exception:
            return None

    @staticmethod
    def _live_identity_id(session_id: str, msg_id: str) -> str:
        """Hash only the canonical durable identity, never message content."""
        identity = f"{session_id}\x00{msg_id}".encode("utf-8", errors="ignore")
        return hashlib.sha256(identity).hexdigest()

    def _live_accepted_path(self, session_id: str, msg_id: str):
        dirp = self._live_accepted_dir()
        if dirp is None:
            return None
        return dirp / f"{self._live_identity_id(session_id, msg_id)}.json"

    def _persist_live_accepted(self, item) -> bool:
        """Persist a post-PG-ack identity tombstone before deleting outbox."""
        try:
            session_id = str(item[0]) if len(item) > 0 else ""
            msg_id = str(item[1]) if len(item) > 1 else ""
            path = self._live_accepted_path(session_id, msg_id)
            dirp = self._live_accepted_dir()
            if path is None or dirp is None:
                return False
            if path.exists():
                return True
            payload = {"version": 1, "session_id": session_id,
                       "msg_id": msg_id, "accepted": True}
            with self._durability_lock:
                dirp.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
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
            return True
        except Exception as e:
            logger.warning("LiveBuffer accepted identity persist failed: %s", _safe_err(e)[:120])
            return False

    def has_durable_marker(self, session_id: str, msg_id: str, content: str = "",
                           role: str = "", turn_id: str = "", timestamp=None,
                           tool_calls=None, tool_results=None) -> bool:
        """Return whether this canonical identity is pending or already accepted."""
        try:
            accepted = self._live_accepted_path(str(session_id), str(msg_id))
            if accepted is not None and accepted.exists():
                return True
            try:
                from . import _parse_msg_timestamp
                timestamp = _parse_msg_timestamp(timestamp)
            except Exception:
                pass
            item = (session_id, msg_id, content, role, turn_id, timestamp,
                    tool_calls, tool_results)
            job_id = self._live_job_id(item)
            with self._pending_jobs_lock:
                if job_id in self._pending_jobs:
                    return True
            pending = self._live_pending_dir()
            return pending is not None and (pending / f"{job_id}.json").exists()
        except Exception:
            return False

    def _live_job_id(self, item) -> str:
        try:
            import hashlib, json
            # stable hash over all tuple fields
            session_id = item[0] if len(item) > 0 else ""
            msg_id = item[1] if len(item) > 1 else ""
            content = item[2] if len(item) > 2 else ""
            role = item[3] if len(item) > 3 else ""
            turn_id = item[4] if len(item) > 4 else ""
            ts = item[5] if len(item) > 5 else None
            tc = item[6] if len(item) > 6 else None
            tr = item[7] if len(item) > 7 else None
            payload = json.dumps({"session_id": session_id, "msg_id": msg_id, "content": content, "role": role, "turn_id": turn_id, "ts": str(ts) if ts is not None else "", "tc": tc, "tr": tr}, sort_keys=True, ensure_ascii=False, default=str)
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()
        except Exception:
            import hashlib
            return hashlib.sha256(f"{item[0]}:{item[1]}:{id(item)}".encode()).hexdigest()

    def _persist_live_item(self, item) -> bool:
        try:
            dirp = self._live_pending_dir()
            if dirp is None:
                return False
            job_id = self._live_job_id(item)
            path = dirp / f"{job_id}.json"
            if path.exists():
                return True
            payload = {
                "version": 1,
                "job_id": job_id,
                "session_id": item[0] if len(item)>0 else "",
                "msg_id": item[1] if len(item)>1 else "",
                "content": item[2] if len(item)>2 else "",
                "role": item[3] if len(item)>3 else "",
                "turn_id": item[4] if len(item)>4 else "",
                "timestamp": str(item[5]) if len(item)>5 and item[5] is not None else None,
                "tool_calls": item[6] if len(item)>6 else None,
                "tool_results": item[7] if len(item)>7 else None,
            }
            with self._durability_lock:
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
                # also fsync dir for durability (best effort)
                try:
                    fd = os.open(str(dirp), os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except Exception:
                    pass
            return True
        except Exception as e:
            logger.warning("LiveBuffer _persist_live_item failed: %s", _safe_err(e)[:120])
            return False

    def _ack_live_item(self, item):
        try:
            dirp = self._live_pending_dir()
            if dirp is None:
                return
            job_id = self._live_job_id(item)
            # Never delete the pending marker until the accepted identity
            # tombstone is durable; otherwise a fresh Core could replay it.
            if not self._persist_live_accepted(item):
                return
            path = dirp / f"{job_id}.json"
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            # 2026-09-07 (C): drop the in-process pending slot so a fresh
            # enqueue of the same job (e.g. from a re-sync after the
            # writer drained) is treated as a real delta, not a duplicate.
            try:
                with self._pending_jobs_lock:
                    self._pending_jobs.discard(job_id)
            except Exception:
                pass
        except Exception:
            pass

    def _recover_live_pending(self):
        try:
            dirp = self._live_pending_dir()
            if dirp is None or not dirp.exists():
                return
            files = sorted(dirp.glob("*.json"))
            recovered = 0
            for p in files:
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    session_id = data.get("session_id", "")
                    msg_id = data.get("msg_id", "")
                    content = data.get("content", "")
                    role = data.get("role", "")
                    turn_id = data.get("turn_id", "")
                    ts_raw = data.get("timestamp")
                    ts = None
                    if ts_raw:
                        try:
                            from . import _parse_msg_timestamp
                            ts = _parse_msg_timestamp(ts_raw)
                        except Exception:
                            ts = None
                    tc = data.get("tool_calls")
                    tr = data.get("tool_results")
                    item = (session_id, msg_id, content, role, turn_id, ts, tc, tr)
                    # 2026-09-07 (C): register the recovered job into the
                    # in-process pending set so the same LiveBuffer
                    # instance does not double-enqueue it.  The durable
                    # marker on disk is the cross-process guard; the
                    # in-memory set is the same-process dedupe.
                    try:
                        rid = data.get("job_id") or self._live_job_id(item)
                        with self._pending_jobs_lock:
                            self._pending_jobs.add(rid)
                    except Exception:
                        pass
                    # direct put without re-persist
                    self._q.put(item)
                    recovered += 1
                except Exception as e:
                    logger.warning("LiveBuffer recover skip %s: %s", p.name, _safe_err(e)[:80])
            # ensure writer is running to drain recovered items (RC6: new generation must auto flush after unblock)
            if recovered > 0:
                with self._lock:
                    if not getattr(self, '_closed', False) and not getattr(self, '_fenced', False) and getattr(self, '_accepting', True):
                        if self._writer is None or not self._writer.is_alive():
                            self._start_writer()
        except Exception:
            pass

    def _run(self):
        # durability + fence aware loop with stop signal and timeout get
        deadline = time.time() + self._flush_sec
        while not self._stop.is_set() and not getattr(self, '_fenced', False) and not getattr(self, '_closed', False):
            try:
                try:
                    item = self._q.get(timeout=0.2)
                except queue.Empty:
                    if time.time() >= deadline:
                        # periodic flush (no items) -- only if not fenced
                        if not getattr(self, '_fenced', False) and not getattr(self, '_closed', False):
                            self._flush()
                        deadline = time.time() + self._flush_sec
                    continue
                batch = [item]
                # hold inflight for drain re-queue visibility
                with self._inflight_lock:
                    self._inflight = list(batch)
                while len(batch) < self._batch:
                    try:
                        nxt = self._q.get_nowait()
                        batch.append(nxt)
                        with self._inflight_lock:
                            self._inflight = list(batch)
                    except queue.Empty:
                        break
                # fence check before flush: if fenced after dequeue, put back and exit
                if getattr(self, '_fenced', False) or getattr(self, '_closed', False):
                    # put batch back to queue (preserve) and exit loop
                    for it in batch:
                        try:
                            self._q.put(it)
                        except Exception:
                            pass
                    with self._inflight_lock:
                        self._inflight = []
                    break
                self._flush(batch_size=len(batch), items=batch)
                deadline = time.time() + self._flush_sec
                with self._inflight_lock:
                    self._inflight = []
                # mark queue task done for each consumed item
                for _ in batch:
                    try:
                        self._q.task_done()
                    except Exception:
                        pass
            except Exception as e:
                # 不让 writer 线程崩;sleep 1s (reduced) 防止无限 crash loop
                logger.error("LiveBuffer writer 异常: %s", _safe_err(e)[:200])
                time.sleep(1)
                deadline = time.time() + self._flush_sec
                with self._inflight_lock:
                    self._inflight = []
        # drain on stop -- bounded, respects fence: do not PG-write if fenced, just keep markers
        try:
            remaining = []
            while not self._q.empty():
                try:
                    remaining.append(self._q.get_nowait())
                except queue.Empty:
                    break
            if remaining and not getattr(self, '_fenced', False) and not getattr(self, '_closed', False):
                self._flush(items=remaining)
                for _ in remaining:
                    try:
                        self._q.task_done()
                    except Exception:
                        pass
            elif remaining:
                # fenced: put back for recovery and mark done to avoid join hang
                for it in remaining:
                    try:
                        self._q.put(it)
                    except Exception:
                        pass
                    try:
                        self._q.task_done()
                    except Exception:
                        pass
                # also re-queue inflight if any
                with self._inflight_lock:
                    infl = list(self._inflight) if isinstance(self._inflight, list) else []
                    self._inflight = []
                for it in infl:
                    try:
                        self._q.put(it)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("LiveBuffer drain on stop 失败: %s", _safe_err(e)[:200])
    
    @staticmethod
    def _derive_shared_pool(pg) -> Any:
        """从传入的 store 推导共享 PgPool owner (pool-backed 模式才返回非 None).

        2026-08-27 pool-owner fix: 只认 ``store.pool`` 属性 (PgEmbedStore 的
        public owner accessor)。legacy / mock store 无 pool → 返回 None,
        重连走 legacy pg/config 模式。
        """
        if pg is None:
            return None
        pool = getattr(pg, "pool", None)
        # duck-typing: 共享池必须可 lease (排掉 MagicMock/任意真值误伤)
        if pool is not None and callable(getattr(pool, "lease", None)):
            return pool
        return None

    def _try_reconnect(self) -> bool:
        """尝试重连 PG — 用于 _flush 时 pg=None 的断线重连

        P1 加强: 5s 内不重复重试(避免每次 flush 都尝试建连);失败时 warn 级别日志辅助排查

        2026-08-27 pool-owner fix:
        * pool-backed 初始化过的实例 (_shared_pool 非 None): 重连一律
          ``PgEmbedStore(self._pg_config, pool=self._shared_pool)`` — 保留同一
          PgPool owner, 探测用 **有界 public lease/is_connected**, 不触碰 raw
          _connect()。探测失败时新 store 即刻 close, 不泄漏 store/连接。
        * legacy 模式 (config-only, 从未持有 pool): 维持原 direct 构造路径不变。
        """
        # 节流: LIVE_RECONNECT_THROTTLE_SEC 内不重复尝试
        if time.time() - self._last_reconnect_ts < LIVE_RECONNECT_THROTTLE_SEC:
            return False
        self._last_reconnect_ts = time.time()
        if not self._pg_config:
            logger.warning("LiveBuffer _try_reconnect 失败: 无 _pg_config, 无法重连")
            return False
        try:
            from .pg_store import PgEmbedStore
            shared_pool = self._shared_pool
            if shared_pool is not None:
                # pool-backed 路径: 同一 PgPool owner 重建 (direct 构造被禁止)
                pg = PgEmbedStore(self._pg_config, pool=shared_pool)
                ok = self._probe_store(pg)
                if ok:
                    old_pg = self._pg
                    self._pg = pg
                    # 旧 store 若是同代 pool-backed 实例则只清引用; pool 本体
                    # 由 V3Core owner 持有, 此处绝不 shutdown/close 物理池。
                    if old_pg is not None and old_pg is not pg:
                        try:
                            old_pg.close()
                        except Exception as e_close:
                            logger.debug(
                                "LiveBuffer 重连后旧 store close 失败(忽略): %s",
                                _safe_err(e_close)[:120],
                            )
                    logger.info("LiveBuffer 重连 PG 成功 (shared pool owner 保留)")
                    return True
                # 探测失败: 立即释放空壳 store, 不留悬挂引用/资源
                try:
                    pg.close()
                except Exception:
                    pass
                logger.warning(
                    "LiveBuffer 重连 PG 失败: pool-backed PgEmbedStore.is_connected()=False"
                )
                return False
            # legacy 模式 (无 pool owner): 维持原 direct 构造 + is_connected 探测
            pg = PgEmbedStore(self._pg_config)
            if pg and pg.is_connected():
                self._pg = pg
                logger.info("LiveBuffer 重连 PG 成功")
                return True
            logger.warning("LiveBuffer 重连 PG 失败: PgEmbedStore.is_connected()=False")
        except Exception as e:
            logger.warning("LiveBuffer 重连 PG 异常: %s", _safe_err(e)[:200])
        return False

    @staticmethod
    def _probe_store(pg) -> bool:
        """有界连通性探测 — 只经 public lease(timeout)/is_connected.

        * pool-backed store: 用 ``with lease(LIVE_RECONNECT_LEASE_TIMEOUT_SEC)``
          执行 ``SELECT 1``。绝不调用 raw ``_connect()`` (pool-backed 下它被
          设计为 raise)。归还由 lease context 保证 — 成功/失败都不泄漏借出槽位。
        * legacy store (无 pool): 退回 ``is_connected()``, 内部同样走 lease()
          路径 (legacy 直连), 语义与历史行为一致。
        """
        pool = getattr(pg, "pool", None)
        if pool is None:
            # legacy store
            try:
                return bool(pg.is_connected())
            except Exception:
                return False
        timeout = LIVE_RECONNECT_LEASE_TIMEOUT_SEC
        try:
            with pg.lease(timeout) as conn:
                if not conn:
                    return False
                cur = conn.cursor()
                cur.execute("SELECT 1")
                return True
        except Exception as e:
            logger.warning(
                "LiveBuffer _probe_store via pool lease 失败: %s", _safe_err(e)[:160]
            )
            return False

    def _write_fallback(self, items: list):
        """PG 不通时写原始消息到 j/_lost/<session_id>/turns.md 兜底

        P1 改写: 不再依赖 j_writer.write_turn (journal 格式), 改为直接写
        与旧 turns.md 一致的 raw markdown 格式, 保留 role/content/msg_id 信息
        """
        if not items:
            return
        try:
            # group by session_id
            sessions: dict[str, list] = {}
            for item in items:
                if not isinstance(item, (tuple, list)) or len(item) < 2:
                    continue
                sid = item[0]
                if not sid:
                    continue
                sessions.setdefault(sid, []).append(item)

            from .config import _resolve_data_dir
            base = _resolve_data_dir(self._pg_config) / "j" / "_lost"
            base.mkdir(parents=True, exist_ok=True)

            for sid, session_items in sessions.items():
                # 把 sid 中的不安全字符替换成下划线,避免路径遍历
                safe_sid = str(sid).replace("..", "_").replace("/", "_").replace("\\", "_")
                session_dir = base / safe_sid
                session_dir.mkdir(parents=True, exist_ok=True)
                path = session_dir / "turns.md"
                ts = datetime.now().isoformat(timespec="seconds")
                with open(path, "a", encoding="utf-8") as f:
                    for it in session_items:
                        msg_id = it[1] if len(it) > 1 else "?"
                        content = it[2] if len(it) > 2 else ""
                        role = it[3] if len(it) > 3 else ""
                        if not content:
                            continue
                        # 与旧 turns.md 一致的 raw 格式: role + content, msg_id 写在 header 上
                        f.write(f"\n## {role or 'unknown'} ({msg_id}) {ts}\n")
                        f.write(f"{content}\n")
                logger.warning(
                    "LiveBuffer fallback 写入 %d 条原始消息到 %s",
                    len(session_items), path,
                )
        except Exception as e:
            logger.warning("LiveBuffer fallback 写原始消息失败: %s", _safe_err(e)[:200])

    def _flush(self, batch_size: int = 0, items: list | None = None):
        """flush live buffer — 写 pg v3_messages，PG 不通时尝试重连; fence-aware"""
        logger.debug("LiveBuffer flush (batch=%d)", batch_size)
        # fence check before any remote work
        if getattr(self, '_fenced', False) or getattr(self, '_closed', False):
            # keep durable markers, do not attempt PG
            return
        if not hasattr(self, '_pg') or not self._pg:
            # P0 fix: PG 断连时尝试重连，而非静默丢弃
            if not self._try_reconnect():
                self._last_flush_ts = time.time()
                # 仍写不了 PG → 写 j/ 文件兜底（fallback）
                self._write_fallback(items or [])
                return
        if not items:
            items = []
        flushed = 0
        acked_items = []
        for item in items:
            if len(item) < 2:
                continue
            # fence re-check per item after potential long block from previous item
            if getattr(self, '_fenced', False) or getattr(self, '_closed', False):
                break
            session_id, msg_id = item[0], item[1]
            content = item[2] if len(item) > 2 else ''
            role = item[3] if len(item) > 3 else ''
            turn_id = item[4] if len(item) > 4 else ''
            # 2026-08-08 融合: 8 元组扩展字段 (timestamp/tool_calls/tool_results)
            ts_value = item[5] if len(item) > 5 else None
            tool_calls = item[6] if len(item) > 6 else None
            tool_results = item[7] if len(item) > 7 else None
            if not content:
                # content empty -> ack to avoid leak (nothing to insert)
                try:
                    self._ack_live_item(item)
                except Exception:
                    pass
                continue
            source_id = f'live/{session_id}/{msg_id}'
            # check fence before embedding
            if getattr(self, '_fenced', False) or getattr(self, '_closed', False):
                break
            # Embedding is a DERIVED index; the source row is the asset. A failure
            # here must not destroy the row, must not be swallowed, and must leave a
            # durable marker — an *unexplained* NULL is the production memory hole
            # this replaces.
            #
            # `_flush` runs on the v3-live-writer DAEMON THREAD (sync_turn → queue →
            # writer), NOT inside the user's 8s realtime budget, so it uses the
            # bounded stream-primary policy (5s/0 single shot) instead of
            # silently inheriting the 3s/0 realtime default — which is exactly
            # how 3.5–5s provider responses used to become permanent holes.
            # A 10s/2 chain inline would head-of-line-block the single-threaded
            # serial writer (worst case ~33s/item vs ~13 rows/min peak arrival),
            # so the patient 10s/2 retry lives on the deferred repair/backfill
            # path: on primary failure the source row is kept, a retryable
            # marker is recorded, and the writer moves to the next item.
            outcome = embed_for_write(
                (content[:2000] if content else ""),
                self._embed_cfg or {},
                entity_table="conversation_stream",
                entity_id=source_id,
                phase="live_ingest",
                conn_factory=getattr(self._pg, "open_side_connection", None),
                policy=STREAM_PRIMARY_EMBED_POLICY,
            )
            ev = outcome.vector
            if not outcome.ok:
                _log = (logger.error if outcome.status is EmbedOutcomeStatus.FAILED
                        else logger.warning)
                _log(
                    "LiveBuffer embedding %s: source_id=%s class=%s retryable=%s "
                    "attempts=%s policy=%s marker_recorded=%s — 源记录保留, embedding 待修复",
                    outcome.status.value, source_id, outcome.error_class,
                    outcome.retryable, outcome.attempts, outcome.policy,
                    outcome.marker_recorded,
                )
            # fence re-check after embedding (late return must not use PG)
            if getattr(self, '_fenced', False) or getattr(self, '_closed', False):
                break
            metadata = {"role": role}
            if session_id:
                metadata["session_id"] = session_id
            if turn_id:
                metadata["turn_id"] = turn_id
            if ts_value is not None:
                metadata["timestamp"] = ts_value
            if tool_calls:
                metadata["tool_calls"] = tool_calls
            if tool_results:
                metadata["tool_results"] = tool_results
            # check fence before PG lease
            if getattr(self, '_fenced', False) or getattr(self, '_closed', False):
                break
            # 尝试插入 + 3次重试 (应对PG短暂断连) -- but fence must prevent retry after fenced
            _inserted = False
            for _attempt in range(4):
                if getattr(self, '_fenced', False) or getattr(self, '_closed', False):
                    break
                try:
                    self._pg.insert_message(source_id, content, embedding=ev, metadata=metadata)
                    flushed += 1
                    _inserted = True
                    acked_items.append(item)
                    break
                except Exception as _e:
                    # if fenced during retry, stop
                    if getattr(self, '_fenced', False) or getattr(self, '_closed', False):
                        break
                    if _attempt < 3:
                        time.sleep(0.2)
                    else:
                        logger.warning("LiveBuffer flush insert 失败 (重试3次后放弃): %s", str(_e)[:200])
        # ack only successful PG inserts; failed/blocked retain marker for recovery
        for it in acked_items:
            try:
                self._ack_live_item(it)
            except Exception:
                pass
        if flushed:
            logger.info("LiveBuffer flush: 写入 %d 条消息到 pg", flushed)
            # Phase 6.2: PG 写成功 → 触发轻量主题匹配 (only if not fenced)
            if not getattr(self, '_fenced', False) and not getattr(self, '_closed', False):
                self._trigger_post_flush(items)
        self._last_flush_ts = time.time()
    
    def _post_flush(self, items: list):
        """post_flush 钩子 — 子类/调用方可覆写, 用于触发后续处理"""
        pass
    
    def _trigger_post_flush(self, items: list):
        """触发 post_flush 回调"""
        try:
            self._post_flush(items)
        except Exception as e:
            logger.debug("post_flush 回调失败: %s", _safe_err(e)[:100])
    
    def shutdown(self, timeout: float | None = 1.0):
        # bounded shutdown: stop accepting, bounded join, then fence late workers
        # compatible with legacy callers with no args -> default 1.0s
        if timeout is None:
            timeout = 1.0
        try:
            timeout = float(timeout)
        except Exception:
            timeout = 1.0
        if timeout < 0:
            timeout = 0.0
        # idempotent stop accepting (do not fence immediately so normal drain can ack)
        with self._lock:
            if getattr(self, '_closed', False):
                return
            self._accepting = False
            # generation bump for fencing late workers after deadline
            try:
                self._generation += 1
            except Exception:
                self._generation = 1
        # signal writer to exit after current batch
        self._stop.set()
        # bounded join -- allow daemon detach if permanently blocked
        writer = self._writer
        if writer and writer.is_alive():
            writer.join(timeout=timeout)
            if writer.is_alive():
                # still blocked on embedding -> now fence to prevent PG lease on return
                with self._lock:
                    self._fenced = True
                    self._closed = True
                try:
                    with self._inflight_lock:
                        infl = list(self._inflight) if isinstance(self._inflight, list) and self._inflight else []
                    for it in infl:
                        try:
                            self._q.put(it)
                        except Exception:
                            pass
                except Exception:
                    pass
                logger.warning("LiveBuffer shutdown bounded: writer still alive after %.2fs, fenced", timeout)
            else:
                # graceful exit within deadline: fence now for late returns, but allow acked items to be cleaned
                with self._lock:
                    self._fenced = True
                    self._closed = True
        else:
            # no writer or already exited
            with self._lock:
                self._fenced = True
                self._closed = True
        # ensure queue consumers don't hang on task_done: drain any remaining unfinished count without losing durable
        # For normal case, queue should be empty and markers acked; for fenced blocked case, queue now contains re-queued inflight + queued
        # and durable files remain for RC6 recovery. No additional PG work here.
        try:
            pass
        except Exception:
            pass


# ── Ingest 引擎 ──

def _parse_md_messages(text: str, source_prefix=None) -> list[dict]:  # noqa: ANN001
    """Parse j_writer markdown format into message dicts.

    Bug 3.2 fix: source_prefix (typically the j/ file stem or a session_id)
    is used as the source_id stem so multiple j/*.md files do NOT collide on
    "msg_0" / "msg_1" when their messages land in v3_messages.
    """
    import re
    msgs = []
    lines = text.split(chr(10))
    current_role = None
    current_content = []
    for line in lines:
        if line.startswith("## ") and not current_role:
            continue  # skip title header
        if line.strip().startswith("user:") or line.strip() == "user:":
            if current_role and current_content:
                msgs.append({"role": current_role, "content": chr(10).join(current_content).strip()})
            current_role = "user"
            rest = line.split("user:", 1)[1].strip() if "user:" in line else ""
            current_content = [rest] if rest else []
        elif line.strip().startswith("assistant:") or line.strip() == "assistant:":
            if current_role and current_content:
                msgs.append({"role": current_role, "content": chr(10).join(current_content).strip()})
            current_role = "assistant"
            rest = line.split("assistant:", 1)[1].strip() if "assistant:" in line else ""
            current_content = [rest] if rest else []
        elif line.strip().startswith("tool_calls:") or current_role == "tool_calls":
            if current_role == "tool_calls" or line.strip().startswith("tool_calls:"):
                pass  # skip tool call blocks
            else:
                current_content.append(line)
        else:
            if current_role:
                current_content.append(line)
    if current_role and current_content:
        msgs.append({"role": current_role, "content": chr(10).join(current_content).strip()})
    # Convert to expected format
    result = []
    for i, m in enumerate(msgs):
        # Bug 3.2 fix: prefix source_id with the file stem (or caller-supplied
        # source_prefix) so msg_0 from j/a.md does NOT collide with msg_0 from
        # j/b.md when both end up in v3_messages.
        prefix = source_prefix or "msg"
        result.append({"id": f"{prefix}_{i}", "role": m["role"], "content": m["content"]})
    return result

def ingest_session(j_dir: Path, pg, embed_cfg: dict, max_count: int = 100):
    """Ingest 1: 会话原始 -> pg messages"""
    count = 0
    if not j_dir.exists():
        return count
    for f in sorted(list(j_dir.glob("*.json")) + list(j_dir.glob("*.md")))[:max_count]:
        try:
            if f.suffix == ".json":
                data = json.loads(f.read_text(encoding="utf-8"))
                msgs = data.get("messages", [])
            elif f.suffix == ".md":
                # Bug 3.2 fix: pass f.stem so source_id becomes "{stem}_{i}"
                # instead of the global msg_{i} that would collide across j/ files.
                msgs = _parse_md_messages(f.read_text(encoding="utf-8"), source_prefix=f.stem)
            else:
                continue
            for msg in msgs:
                source_id = msg.get("id", "")
                content = msg.get("content", "")
                if source_id and content:
                    # j/ file import is a BATCH path, not the 8s realtime budget, so
                    # it takes the batch policy and records failures durably rather
                    # than writing an unexplained NULL.
                    outcome = embed_for_write(
                        content[:2000],
                        embed_cfg or {},
                        entity_table="conversation_stream",
                        entity_id=source_id,
                        phase="j_import",
                        conn_factory=getattr(pg, "open_side_connection", None),
                        policy=BATCH_EMBED_POLICY,
                    )
                    if not outcome.ok:
                        logger.warning(
                            "j import embedding %s: source_id=%s class=%s "
                            "retryable=%s attempts=%s marker_recorded=%s",
                            outcome.status.value, source_id, outcome.error_class,
                            outcome.retryable, outcome.attempts,
                            outcome.marker_recorded,
                        )
                    pg.insert_message(source_id, content, embedding=outcome.vector)
                    count += 1
        except ValueError:
            raise
        except Exception as e:
            logger.warning("ingest session %s 失败: %s", f.name, _safe_err(e)[:200])
    return count

def ingest_session_summary(b_dir: Path, pg, embed_cfg: dict, max_cards: int = 10):
    """Ingest 2: session 摘要 -> pg.

    Bug 3.1 fix: this function previously did pg.insert_message(...) which
    double-stored every summary: once as the canonical file at
    b/session_summaries/{stem}.md and once as a raw row in v3_messages.
    Recall could then surface the same summary twice (once via the file scan,
    once via the raw messages query). The new
    v3core.effective.ingest_session_summary path already routes summaries into
    v3_messages.effective_pool with pool_role="session_summary" via UPSERT on
    source_id, so the canonical store is the file on disk plus the
    effective_pool row.

    Resolution:
      * If pg.insert_effective is available, write the summary there with
        pool_role="session_summary". This is idempotent and is the
        non-duplicating path.
      * Otherwise (no insert_effective -- older pg shim), fall back to a raw
        pg.insert_message so the summary is still retrievable. Callers are
        advised to prefer running v3core.effective.run_all_ingests which uses
        the pool_role path and does not double-write.
    """
    summaries_dir = b_dir / "session_summaries"
    count = 0
    if summaries_dir.exists():
        for f in sorted(summaries_dir.glob("*.md"))[:max_cards]:
            try:
                content = f.read_text(encoding="utf-8")
                source_id = f"summ_{f.stem}"
                # Bug 3.1 fix: prefer the effective_pool path; only fall back
                # to the raw message table when insert_effective is unavailable.
                inserted = False
                insert_effective = getattr(pg, "insert_effective", None)
                if callable(insert_effective):
                    try:
                        insert_effective(
                            source_id,
                            content,
                            pool_role="session_summary",
                            extra={"path": str(f), "kind": "session_summary"},
                        )
                        inserted = True
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "ingest_session_summary insert_effective failed, "
                            "fallback to insert_message: %s",
                            _safe_err(e)[:200],
                        )
                if not inserted:
                    pg.insert_message(source_id, content)
                count += 1
            except Exception:
                pass
    return count

def ingest_key_decisions(decisions_dir: Path, pg, embed_cfg: dict, max_cards: int = 10):
    """Ingest 3: 关键决策 -> pg"""
    count = 0
    if decisions_dir.exists():
        for f in sorted(decisions_dir.glob("*.md"))[:max_cards]:
            try:
                content = f.read_text(encoding="utf-8")
                pg.insert_card(f"dec_{f.stem}", f.stem, content, "decisions")
                count += 1
            except Exception:
                pass
    return count