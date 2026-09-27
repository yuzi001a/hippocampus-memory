"""B01 behavior tests — real HTTP handler + bounded buffer + receipt mapping.

这些测试走真实的 ThreadingHTTPServer（不经 PG）：证明 /events 的 ACK 说真话、
/health 的 capability 是增量且向后兼容、msg_buffer 有界。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


class _FakeCore:
    """最小 core 替身：只提供 /health、/events、/prefetch 需要的面。"""

    def __init__(self, receipt=None, error: bool = False):
        self.pg = None
        self.config = {}
        self.receipt = receipt
        self.error = error
        self.calls: list[tuple] = []

    def sync_turn(self, session_id, messages=None, host=None):
        self.calls.append((session_id, len(messages or []), host))
        if self.error:
            raise RuntimeError("sync_turn boom")
        return self.receipt

    def prefetch_to_context_block(self, query, session_id=""):
        return f"block::{query}"


def _serve(core, buffer=None):
    from v3core.serve import build_handler

    handler_cls = build_handler(core, buffer if buffer is not None else {})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, port


def _post(port, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


# ── ACK 说真话（真实 HTTP）───────────────────────────────────────

def test_events_ack_accepted_over_http():
    core = _FakeCore(receipt={"ok": True, "events": {"e1": "accepted"},
                              "source_ids": {"e1": "qa_sync/h/s/e1"}})
    server, port = _serve(core)
    try:
        status, body = _post(port, "/events", {"host": "h", "session_id": "s",
                                               "event_id": "e1", "role": "user",
                                               "content": "hi"})
        assert status == 200
        assert body["ok"] is True and body["accepted"] is True and body["duplicate"] is False
        assert body["event_id"] == "e1" and body["host"] == "h"
        assert body["source_id"] == "qa_sync/h/s/e1"
    finally:
        server.shutdown()


def test_events_ack_duplicate_over_http():
    core = _FakeCore(receipt={"ok": True, "events": {"e1": "duplicate"}})
    server, port = _serve(core)
    try:
        status, body = _post(port, "/events", {"host": "h", "session_id": "s",
                                               "event_id": "e1", "role": "user",
                                               "content": "hi"})
        assert status == 200
        assert body["ok"] is True and body["duplicate"] is True and body["accepted"] is False
    finally:
        server.shutdown()


def test_events_writer_rejection_never_reports_success():
    """sync_turn 报 rejected ⇒ HTTP 非 2xx + ok=false + retryable。"""
    core = _FakeCore(receipt={"ok": False, "events": {"e1": "rejected"},
                              "rejected": ["e1"]})
    server, port = _serve(core)
    try:
        status, body = _post(port, "/events", {"host": "h", "session_id": "s",
                                               "event_id": "e1", "role": "user",
                                               "content": "hi"})
        assert status >= 400, f"写失败却报成功: {status} {body}"
        assert body["ok"] is False and body["accepted"] is False
        assert body["status"] == "retryable"
    finally:
        server.shutdown()


def test_events_error_path_is_not_success():
    core = _FakeCore(error=True)
    server, port = _serve(core)
    try:
        status, body = _post(port, "/events", {"host": "h", "session_id": "s",
                                               "event_id": "e1", "role": "user",
                                               "content": "hi"})
        assert status >= 400 and body["ok"] is False
    finally:
        server.shutdown()


def test_events_legacy_payload_still_accepted():
    core = _FakeCore(receipt={"ok": True, "events": {"m1": "accepted"}})
    server, port = _serve(core)
    try:
        status, body = _post(port, "/events", {"session_id": "s", "msg_id": "m1",
                                               "role": "user", "content": "hi"})
        assert status == 200 and body["accepted"] is True
        assert body["host"] == "legacy"
        assert body["event_id"] == "m1"
    finally:
        server.shutdown()


def test_events_missing_identity_is_400():
    core = _FakeCore(receipt=None)
    server, port = _serve(core)
    try:
        status, body = _post(port, "/events", {"role": "user", "content": "hi"})
        assert status == 400 and body["ok"] is False
    finally:
        server.shutdown()


def test_events_payload_preserves_identity_fields_for_core():
    """host/turn/parent 等字段要传进 sync_turn（G4：身份先保留）。"""
    core = _FakeCore(receipt={"ok": True, "events": {"e1": "accepted"}})
    buffer: dict = {}
    server, port = _serve(core, buffer)
    try:
        _post(port, "/events", {"host": "synthetic-pi", "session_id": "s",
                                "event_id": "e1", "role": "user", "content": "hi",
                                "turn_id": "7", "project_id": "p1", "agent_id": "a1",
                                "parent_event_id": "e0", "branch_id": "b1"})
        assert core.calls and core.calls[-1][2] == "synthetic-pi"
        msg = buffer["s"][0]
        assert msg["host"] == "synthetic-pi" and msg["turn_id"] == "7"
        assert msg["project_id"] == "p1" and msg["agent_id"] == "a1"
        assert msg["parent_event_id"] == "e0" and msg["branch_id"] == "b1"
    finally:
        server.shutdown()


# ── /health capability 增量 ─────────────────────────────────────

def test_health_is_additive_with_shared_capabilities():
    core = _FakeCore(receipt=None)
    server, port = _serve(core)
    try:
        status, body = _get(port, "/health")
        assert status == 200
        # 旧键必须还在（向后兼容）
        for key in ("ok", "pg", "embed", "core"):
            assert key in body, key
        # 新键来自共享 seam
        from v3core.bridge_contract import get_bridge_capabilities
        caps = get_bridge_capabilities()
        for key in ("bridge_protocol_version", "core_package_version",
                    "server_version", "capabilities"):
            assert body.get(key) == caps[key], key
    finally:
        server.shutdown()


# ── msg_buffer 有界（且不再承担 correctness 权威）────────────────

def _handler_for_buffer():
    from v3core.serve import build_handler

    cls = build_handler(_FakeCore())
    inst = object.__new__(cls)
    inst.msg_buffer = {}
    inst.buffer_last_seen = {}
    return inst


def test_trim_buffer_enforces_per_session_cap():
    from v3core.serve import MAX_BUFFER_EVENTS_PER_SESSION

    inst = _handler_for_buffer()
    sid = "s1"
    inst.msg_buffer[sid] = [{"id": f"e{i}"} for i in range(MAX_BUFFER_EVENTS_PER_SESSION + 25)]
    inst.buffer_last_seen[sid] = time.time()
    inst._trim_buffer()
    assert len(inst.msg_buffer[sid]) == MAX_BUFFER_EVENTS_PER_SESSION
    # 保留最新的一段（尾部）
    assert inst.msg_buffer[sid][-1]["id"] == f"e{MAX_BUFFER_EVENTS_PER_SESSION + 24}"


def test_trim_buffer_evicts_idle_sessions():
    from v3core.serve import BUFFER_IDLE_TTL_S

    inst = _handler_for_buffer()
    inst.msg_buffer["old"] = [{"id": "e1"}]
    inst.buffer_last_seen["old"] = time.time() - BUFFER_IDLE_TTL_S - 5
    inst.msg_buffer["new"] = [{"id": "e2"}]
    inst.buffer_last_seen["new"] = time.time()
    inst._trim_buffer()
    assert "old" not in inst.msg_buffer
    assert "new" in inst.msg_buffer


def test_trim_buffer_enforces_session_cap():
    from v3core.serve import MAX_BUFFER_SESSIONS

    inst = _handler_for_buffer()
    now = time.time()
    for i in range(MAX_BUFFER_SESSIONS + 5):
        sid = f"s{i}"
        inst.msg_buffer[sid] = [{"id": "e"}]
        inst.buffer_last_seen[sid] = now - (MAX_BUFFER_SESSIONS + 5 - i)
    inst._trim_buffer()
    assert len(inst.msg_buffer) == MAX_BUFFER_SESSIONS
    assert "s0" not in inst.msg_buffer          # 最旧的被回收
    assert f"s{MAX_BUFFER_SESSIONS + 4}" in inst.msg_buffer


# ── MCP 侧 capability 同源（静态 + 运行时）────────────────────────

def test_v3_health_source_carries_bridge_capabilities():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "v3core"
    src = (root / "tools" / "health.py").read_text(encoding="utf-8")
    assert "get_bridge_capabilities" in src
    assert 'result["bridge"]' in src
    mcp_src = (root / "mcp_server.py").read_text(encoding="utf-8")
    assert "get_bridge_capabilities" in mcp_src


@pytest.mark.parametrize("status,expect_ok,expect_retryable", [
    ("accepted", True, False),
    ("duplicate", True, False),
    ("rejected", False, True),
])
def test_serve_receipt_status_mapping(status, expect_ok, expect_retryable):
    from v3core.serve import build_handler

    cls = build_handler(_FakeCore())
    mapped = cls._event_status_from_receipt({"ok": status != "rejected",
                                             "events": {"e1": status}}, "e1")
    assert mapped == ("accepted" if status == "accepted" else
                      "duplicate" if status == "duplicate" else "retryable")
    from v3core.bridge_contract import ack_from_receipt

    ack = ack_from_receipt({"status": mapped, "event_id": "e1", "host": "h"})
    assert ack["ok"] is expect_ok
    assert ("retryable" in ack["status"]) is expect_retryable
