"""v3-core HTTP serve 模式 — 让外部进程 (Node/dsh 等) 通过 HTTP 调用 v3 记忆能力

零新依赖: 仅用标准库 http.server + json + threading.
线程模型: ThreadingHTTPServer, 每个请求一个 daemon 线程.
"""
from __future__ import annotations
import json
import logging
import os
import signal
import sys
import threading
import traceback
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from . import _safe_err

logger = logging.getLogger("v3core.serve")

# B01 bounded message buffer.  The buffer is a performance/short-term
# convenience ONLY: durable event identity lives on disk (accepted
# tombstones), so correctness never depends on restoring this state.
MAX_BUFFER_SESSIONS = 32
MAX_BUFFER_EVENTS_PER_SESSION = 500
BUFFER_IDLE_TTL_S = 6 * 3600


# ── 工具 dispatch: v3hermes handle_tool_call 的等价物 ──
# v3_prefetch / v3_get_message_context 走 core 方法; 其余走 v3core.tools._handle_tool
def _dispatch_tool(core, name: str, args: dict) -> str:
    """把 /tool 请求按 v3hermes.V3HermesProvider.handle_tool_call 的方式转发.

    返回字符串 (与 v3-hermes handle_tool_call 一致).
    """
    if name == "v3_prefetch":
        try:
            results = core.prefetch(args.get("query", ""), args.get("limit", 5))
            from .prefetch import format_prefetch
            fmt = args.get("format", "json")
            return format_prefetch(results, fmt)
        except Exception as e:
            return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)
    if name == "v3_get_message_context":
        try:
            result = core.get_message_context(args.get("source_id", ""))
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)
    # 其余走 v3core.tools._handle_tool (与 v3hermes 一致)
    from .tools import handle_tool_call as _core_handle_tool
    return _core_handle_tool(name, args)


# ── 健康状态检测 ──
def _check_pg(core) -> bool:
    try:
        return bool(core.pg and core.pg.is_connected())
    except Exception:
        return False


def _check_embed(core) -> bool:
    """Embed 可用性检查：只接受工厂产出的完整配置。"""
    try:
        from .embedding import safe_embed_cfg
        cfg = safe_embed_cfg(core.config)
        if cfg is None:
            return False
        # endpoint 配置存在且 profile 完整才视为 embed 可用; pg 未连则不可用
        if not _check_pg(core):
            return False
        return bool(cfg.get("endpoint"))
    except ValueError as e:
        logger.error("embed 配置不完整，健康检查判定不可用: %s", _safe_err(e)[:160])
        return False
    except Exception:
        return False


# ── Handler ──
class _Handler(BaseHTTPRequestHandler):
    # 由 build_handler 注入
    core: Any = None
    msg_buffer: dict[str, list[dict]] = {}
    buffer_last_seen: dict[str, float] = {}
    server_version: str = "v3core-serve/1.0"

    # 静音 BaseHTTPServer 默认 access log — 我们有自己的 logger
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        try:
            logger.info("%s - %s", self.address_string(), format % args)
        except Exception:
            pass

    # ── 工具方法 ──
    def _send_json(self, status: int, payload: dict) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except Exception as e:
            body = json.dumps({"ok": False, "error": f"encode failed: {_safe_err(e)}"}).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError):
            return None
        if length <= 0:
            return None
        if length > 4 * 1024 * 1024:  # 4MB 上限 — 防御恶意大 body
            raise ValueError(f"body too large: {length}")
        try:
            raw = self.rfile.read(length)
        except Exception as e:
            raise ValueError(f"read body failed: {_safe_err(e)}")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"json parse failed: {_safe_err(e)}")
        if not isinstance(data, dict):
            raise ValueError("body must be JSON object")
        return data

    # ── 路由 ──
    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/health" or self.path.startswith("/health?"):
                pg_ok = _check_pg(self.core)
                embed_ok = _check_embed(self.core)
                payload = {
                    "ok": True,
                    "pg": pg_ok,
                    "embed": embed_ok,
                    "core": "initialized" if self.core is not None else "missing",
                }
                try:
                    from .bridge_contract import get_bridge_capabilities
                    payload.update(get_bridge_capabilities())
                except Exception as e:
                    logger.warning("/health capability 注入失败: %s", _safe_err(e))
                self._send_json(200, payload)
                return
            if self.path == "/" or self.path.startswith("/?"):
                self._send_json(200, {
                    "ok": True,
                    "service": "v3core-serve",
                    "endpoints": ["GET /health", "POST /events", "POST /prefetch", "POST /tool"],
                })
                return
            self._send_json(404, {"ok": False, "error": f"not found: {self.path}"})
        except Exception as e:
            logger.exception("GET %s 异常", self.path)
            self._send_json(500, {"ok": False, "error": _safe_err(e)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_body()
            if body is None:
                # body 为空但路径已知 — POST 端点都要求 body
                self._send_json(400, {"ok": False, "error": "empty body"})
                return

            if self.path == "/events" or self.path.startswith("/events?"):
                self._handle_events(body)
                return
            if self.path == "/prefetch" or self.path.startswith("/prefetch?"):
                self._handle_prefetch(body)
                return
            if self.path == "/tool" or self.path.startswith("/tool?"):
                self._handle_tool(body)
                return

            self._send_json(404, {"ok": False, "error": f"not found: {self.path}"})
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": _safe_err(e)})
        except Exception as e:
            logger.exception("POST %s 异常", self.path)
            self._send_json(500, {"ok": False, "error": _safe_err(e)})

    # ── 各端点实现 ──
    def _trim_buffer(self) -> None:
        """Bounded growth: idle TTL + per-session cap + session-count cap.

        Never a correctness authority — evicted events are still durable on
        disk, so a re-send is detected as a duplicate rather than re-ingested.
        """
        now = time.time()
        buf = self.msg_buffer
        last = self.buffer_last_seen
        for sid in [s for s, seen in last.items() if now - seen > BUFFER_IDLE_TTL_S]:
            buf.pop(sid, None)
            last.pop(sid, None)
        for sid, msgs in list(buf.items()):
            if len(msgs) > MAX_BUFFER_EVENTS_PER_SESSION:
                buf[sid] = msgs[-MAX_BUFFER_EVENTS_PER_SESSION:]
        if len(buf) > MAX_BUFFER_SESSIONS:
            ordered = sorted(last.items(), key=lambda kv: kv[1])
            for sid, _ in ordered[: len(buf) - MAX_BUFFER_SESSIONS]:
                buf.pop(sid, None)
                last.pop(sid, None)

    @staticmethod
    def _event_status_from_receipt(receipt, event_id: str) -> str:
        """Per-event status from a sync_turn receipt (legacy cores → accepted)."""
        if not isinstance(receipt, dict):
            return "accepted"
        events = receipt.get("events")
        if isinstance(events, dict) and event_id in events:
            value = events[event_id]
            if isinstance(value, str):
                # sync_turn 用 rejected 表示"持久化没接受" → ACK 里说 retryable
                return "retryable" if value == "rejected" else value
        if receipt.get("ok") is False:
            return "failed"
        return "accepted"

    @staticmethod
    def _source_id_from_receipt(receipt, event_id: str):
        if not isinstance(receipt, dict):
            return None
        source_ids = receipt.get("source_ids")
        if isinstance(source_ids, dict):
            value = source_ids.get(event_id)
            if isinstance(value, str) and value:
                return value
        return None

    def _handle_events(self, body: dict) -> None:
        # B01: canonical event identity = (host, session_id, event_id).
        # ``msg_id`` stays a legacy alias for ``event_id``; ``host`` missing
        # means the historical namespace, so old clients keep working.
        from .bridge_contract import ack_from_receipt, normalize_event

        try:
            event = normalize_event(body)
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": _safe_err(e)})
            return

        session_id = event["session_id"]
        msg_id = event["event_id"]
        host = event["host"]
        content = event["content"]
        role = event["role"]
        turn_id = body.get("turn_id", "")
        # PG conversation_stream.turn_id 是 integer — 容错转换, 非数字置 None
        if turn_id not in (None, ""):
            try:
                turn_id = int(turn_id)
            except (TypeError, ValueError):
                turn_id = None
        timestamp = body.get("timestamp")
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            try:
                from datetime import datetime, timezone
                timestamp = datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                timestamp = None
        tool_calls = body.get("tool_calls")
        tool_results = body.get("tool_results")

        try:
            # sync_turn 内部负责写 conversation_stream（这里不单独 enqueue，
            # 否则会双写）。事件身份随 msg 一起带给 kin，供 host 作用域判重。
            msg = {
                "id": msg_id,
                "event_id": msg_id,
                "host": host,
                "content": content,
                "role": role,
                "tool_calls": tool_calls or [],
                "tool_results": tool_results or [],
            }
            if turn_id not in (None, ""):
                msg["turn_id"] = str(turn_id)
            if timestamp is not None:
                msg["timestamp"] = timestamp
            for field_name in ("parent_event_id", "project_id", "agent_id", "branch_id"):
                value = body.get(field_name)
                if value not in (None, ""):
                    msg[field_name] = value

            buf = self.msg_buffer
            session_msgs = buf.get(session_id)
            if session_msgs is None:
                session_msgs = []
                buf[session_id] = session_msgs
            session_msgs.append(msg)
            self.buffer_last_seen[session_id] = time.time()
            self._trim_buffer()

            receipt = self.core.sync_turn(session_id, session_msgs, host=host)
            status = self._event_status_from_receipt(receipt, msg_id)
            ack = ack_from_receipt({
                "status": status,
                "event_id": msg_id,
                "host": host,
                "source_id": self._source_id_from_receipt(receipt, msg_id),
            })
            # 写失败绝不显示成"已经记住": retryable/failed → 503 + ok=false
            self._send_json(200 if ack["ok"] else 503, ack)
        except Exception as e:
            logger.warning("events sync_turn 失败 (session=%s msg=%s): %s",
                           str(session_id)[:30], str(msg_id)[:30], _safe_err(e))
            self._send_json(500, {"ok": False, "error": _safe_err(e)})

    def _handle_prefetch(self, body: dict) -> None:
        query = body.get("query", "")
        if not query:
            self._send_json(400, {"ok": False, "error": "query is required"})
            return
        session_id = body.get("session_id", "")
        try:
            block = self.core.prefetch_to_context_block(query, session_id)
            self._send_json(200, {"ok": True, "block": block})
        except Exception as e:
            logger.warning("prefetch 失败 (query=%r): %s", query[:50], _safe_err(e))
            self._send_json(500, {"ok": False, "error": _safe_err(e)})

    def _handle_tool(self, body: dict) -> None:
        name = body.get("name")
        if not name:
            self._send_json(400, {"ok": False, "error": "name is required"})
            return
        args = body.get("args") or {}
        if not isinstance(args, dict):
            self._send_json(400, {"ok": False, "error": "args must be object"})
            return
        try:
            result = _dispatch_tool(self.core, name, args)
            # 兼容: 有些 handler 返回 str, 有些 dict — 一律包成字符串
            if not isinstance(result, str):
                try:
                    result = json.dumps(result, ensure_ascii=False)
                except Exception:
                    result = str(result)
            self._send_json(200, {"ok": True, "result": result})
        except Exception as e:
            logger.warning("tool 失败 (name=%s): %s", name, _safe_err(e))
            self._send_json(500, {"ok": False, "error": _safe_err(e)})


def build_handler(core, msg_buffer=None):
    """返回一个绑定到指定 core 实例的 Handler 类.

    msg_buffer: per-session 全量消息缓冲 (dict[session_id, list[dict]]);
    不传则内部创建一个空 dict — 仅用于兼容测试场景, 生产 serve() 必传.
    """
    class BoundHandler(_Handler):
        pass
    BoundHandler.core = core
    BoundHandler.msg_buffer = msg_buffer if msg_buffer is not None else {}
    # B01: last-seen map is fresh per bound handler; the buffer itself is
    # injected so callers/tests can observe it.
    BoundHandler.buffer_last_seen = {}
    return BoundHandler


# ── 入口函数 ──
def serve(host: str = "127.0.0.1", port: int = 39090, profile: str = "default",
          ready_json: bool = False) -> None:
    """起 HTTP serve 模式 — 阻塞运行直到 KeyboardInterrupt.

    host/port 不从环境变量读 (spec 要求只走参数, 不污染全局配置).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    logger.info("v3core.serve 启动: host=%s port=%d profile=%s", host, port, profile)

    # 1. 初始化 V3Core
    try:
        from . import V3Core
        core = V3Core(profile=profile)
        core.initialize()
    except Exception as e:
        logger.error("V3Core 初始化失败: %s\n%s", _safe_err(e), traceback.format_exc())
        sys.exit(1)

    # 2. 起 ThreadingHTTPServer
    #    msg_buffer: per-session 全量消息缓冲 dict[session_id, list[dict]]
    #    ── serve 是常驻进程 ── 缓冲不设上限, dsh 会话消息量有限 (几十到几百);
    #    若未来服务化到多用户/多 session 长期运行, 需要加 LRU/TTL 兜底
    #    (避免内存泄漏).
    msg_buffer: dict[str, list[dict]] = {}
    handler_cls = build_handler(core, msg_buffer)
    try:
        server = ThreadingHTTPServer((host, port), handler_cls)
    except OSError as e:
        logger.error("绑定 %s:%d 失败: %s", host, port, _safe_err(e))
        try:
            core.shutdown()
        except Exception:
            pass
        sys.exit(1)

    # --port 0 时必须回报实际绑定端口，不能打印 0
    try:
        bound_port = int(server.server_address[1])
    except Exception:
        bound_port = port
    if ready_json:
        try:
            from .bridge_contract import get_bridge_capabilities
            _handshake = get_bridge_capabilities()
        except Exception:
            _handshake = {}
        print(json.dumps({
            "event": "ready",
            "service": "v3core-serve",
            "host": host,
            "port": bound_port,
            "pid": os.getpid(),
            "profile": profile,
            "endpoints": ["GET /health", "POST /events", "POST /prefetch", "POST /tool"],
            **_handshake,
        }, ensure_ascii=False), flush=True)
    logger.info("HTTP 服务已启动: http://%s:%d  (Ctrl+C 退出)", host, bound_port)
    print(f"v3core-serve listening on http://{host}:{bound_port}", flush=True)
    print(f"  GET  /health  → 健康检查", flush=True)
    print(f"  POST /events  → 写消息 + 触发 QA 配对 (sync_turn)", flush=True)
    print(f"  POST /prefetch → 召回上下文块", flush=True)
    print(f"  POST /tool    → 转发到 v3 工具 dispatch", flush=True)

    # 3. 阻塞运行 + 优雅关闭 (SIGTERM 也走同一条 graceful 路径:
    #    停止接受 → core.shutdown() → 退出; owned child 由父进程终止)
    def _on_sigterm(_signum, _frame):
        logger.info("收到 SIGTERM, 准备关闭…")
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except Exception as e:
        logger.warning("SIGTERM handler 注册失败: %s", _safe_err(e))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt, 准备关闭…")
    finally:
        try:
            server.shutdown()
        except Exception as e:
            logger.warning("server.shutdown() 异常: %s", _safe_err(e))
        try:
            server.server_close()
        except Exception as e:
            logger.warning("server_close() 异常: %s", _safe_err(e))
        try:
            core.shutdown()
        except Exception as e:
            logger.warning("core.shutdown() 异常: %s", _safe_err(e))
        logger.info("v3core.serve 已退出")


if __name__ == "__main__":
    serve()
