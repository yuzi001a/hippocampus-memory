"""B01 RED — event identity / idempotent ingest / capability handshake contract.

这些测试在实现落地前必须 FAIL（缺 seam），落地后必须全绿。
覆盖：duplicate same-process / duplicate after restart / missing turn / out-of-order /
writer rejection / host identity 不碰撞 / capability 单真值源。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── 1. 事件身份契约 ────────────────────────────────────────────────

def test_normalize_event_legacy_host_default():
    from v3core.bridge_contract import LEGACY_HOST, normalize_event

    ev = normalize_event({"session_id": "s1", "msg_id": "m1",
                          "role": "user", "content": "hi"})
    assert ev["host"] == LEGACY_HOST
    assert ev["legacy_host"] is True
    assert ev["event_id"] == "m1"  # msg_id 兼容为 event_id，不建两套身份


def test_normalize_event_explicit_host_is_not_legacy():
    from v3core.bridge_contract import normalize_event

    ev = normalize_event({"host": "synthetic-dsh", "session_id": "s1",
                          "event_id": "e1", "role": "user", "content": "hi",
                          "project_id": "p1", "agent_id": "a1"})
    assert ev["host"] == "synthetic-dsh"
    assert ev["legacy_host"] is False
    assert ev["project_id"] == "p1"
    assert ev["agent_id"] == "a1"


@pytest.mark.parametrize("payload", [
    {"msg_id": "m1", "role": "user"},                      # 缺 session_id
    {"session_id": "s1", "role": "user"},                  # 缺 event_id/msg_id
])
def test_normalize_event_rejects_missing_identity(payload):
    from v3core.bridge_contract import normalize_event

    with pytest.raises(ValueError):
        normalize_event(payload)


def test_event_identity_does_not_collide_across_hosts():
    from v3core.bridge_contract import event_identity, normalize_event

    a = normalize_event({"host": "host-a", "session_id": "s", "event_id": "e",
                         "role": "user", "content": "x"})
    b = normalize_event({"host": "host-b", "session_id": "s", "event_id": "e",
                         "role": "user", "content": "x"})
    assert event_identity(a) != event_identity(b)
    assert event_identity(a) == ("host-a", "s", "e")


# ── 2. 配对决策：缺答案/乱序不许猜 ─────────────────────────────────

def test_pairing_decision_table():
    from v3core.bridge_contract import qa_pairing_decision

    assert qa_pairing_decision(role="user", event_turn_id="1",
                               pending_turn_id=None, pending_has_answer=False) == "open"
    assert qa_pairing_decision(role="user", event_turn_id="2",
                               pending_turn_id="1", pending_has_answer=True) == "flush_open"
    assert qa_pairing_decision(role="assistant", event_turn_id="1",
                               pending_turn_id="1", pending_has_answer=False) == "append"
    # legacy：没有 turn 信息 → 保留既有顺序行为
    assert qa_pairing_decision(role="assistant", event_turn_id=None,
                               pending_turn_id="1", pending_has_answer=False) == "append"


def test_pairing_decision_missing_answer_never_mispairs():
    """turn1 user 的答案丢了；turn2 assistant 到达 → 不许配到 turn1。"""
    from v3core.bridge_contract import qa_pairing_decision

    decision = qa_pairing_decision(role="assistant", event_turn_id="2",
                                   pending_turn_id="1", pending_has_answer=False)
    assert decision == "hold_orphan"


def test_pairing_decision_out_of_order_assistant_holds():
    """assistant 先到（无 pending）→ 暂存，不猜。"""
    from v3core.bridge_contract import qa_pairing_decision

    assert qa_pairing_decision(role="assistant", event_turn_id="3",
                               pending_turn_id=None, pending_has_answer=False) == "hold_orphan"


# ── 3. ACK 必须说真话 ──────────────────────────────────────────────

def test_ack_new_event_is_accepted():
    from v3core.bridge_contract import ack_from_receipt

    ack = ack_from_receipt({"status": "accepted", "event_id": "e1",
                            "source_id": "qa_sync/legacy/s/e1", "host": "legacy"})
    assert ack["ok"] is True and ack["accepted"] is True and ack["duplicate"] is False
    assert ack["event_id"] == "e1" and ack["source_id"] == "qa_sync/legacy/s/e1"


def test_ack_existing_event_is_duplicate():
    from v3core.bridge_contract import ack_from_receipt

    ack = ack_from_receipt({"status": "duplicate", "event_id": "e1", "host": "legacy"})
    assert ack["ok"] is True and ack["accepted"] is False and ack["duplicate"] is True


@pytest.mark.parametrize("status", ["retryable", "failed"])
def test_ack_writer_rejection_never_reports_ok(status):
    from v3core.bridge_contract import ack_from_receipt

    ack = ack_from_receipt({"status": status, "event_id": "e1", "host": "legacy"})
    assert ack["ok"] is False
    assert ack["accepted"] is False
    assert ack["status"] == status


# ── 4. durable identity：同进程重复 / 重启后重复 ───────────────────

def _live_buffer(tmp_path: Path):
    from v3core.ingest import LiveBuffer

    return LiveBuffer(pg=None, config={"basePath": str(tmp_path)})


def _item(session_id="s1", msg_id="m1", content="hello", role="user", turn_id="1"):
    return (session_id, msg_id, content, role, turn_id, None, None, None)


def test_event_status_absent_then_pending_then_durable(tmp_path):
    from v3core.bridge_contract import LEGACY_HOST

    lb = _live_buffer(tmp_path)
    item = _item()
    assert lb.event_status("s1", "m1", host=LEGACY_HOST) == "absent"
    assert lb._persist_live_item(item) is True
    # pending 身份是 content-sensitive 的，所以探测要带完整 item
    assert lb.event_status("s1", "m1", host=LEGACY_HOST, item=item) == "pending"
    assert lb._persist_live_accepted(item) is True
    assert lb.event_status("s1", "m1", host=LEGACY_HOST) == "durable"


def test_event_status_survives_restart(tmp_path):
    """同 event 在 fresh LiveBuffer（进程重启）上仍判 durable —— 这是当前方案真正缺的能力。"""
    from v3core.bridge_contract import LEGACY_HOST

    lb1 = _live_buffer(tmp_path)
    lb1._persist_live_item(_item())
    lb1._persist_live_accepted(_item())

    lb2 = _live_buffer(tmp_path)  # 模拟重启后的新进程
    assert lb2.event_status("s1", "m1", host=LEGACY_HOST) == "durable"


def test_event_status_is_host_scoped(tmp_path):
    """同 native session/event、不同 host → 不碰撞（不互相判重）。"""
    from v3core.bridge_contract import LEGACY_HOST

    lb = _live_buffer(tmp_path)
    lb._persist_live_accepted(_item())  # legacy 身份
    assert lb.event_status("s1", "m1", host=LEGACY_HOST) == "durable"
    assert lb.event_status("s1", "m1", host="synthetic-dsh") == "absent"


def test_legacy_accepted_path_template_unchanged(tmp_path):
    """存量墓碑路径模板必须逐字节不变（保护已落盘的 accepted 身份）。"""
    from v3core.bridge_contract import LEGACY_HOST

    lb = _live_buffer(tmp_path)
    legacy = lb._live_accepted_path("s1", "m1", host=LEGACY_HOST)
    no_host = lb._live_accepted_path("s1", "m1")
    assert legacy == no_host
    assert lb._live_accepted_path("s1", "m1", host="synthetic-dsh") != legacy


# ── 5. capability handshake：单真值源 ──────────────────────────────

def test_capabilities_shape_and_no_release_tag_leak():
    from v3core.bridge_contract import get_bridge_capabilities

    caps = get_bridge_capabilities()
    blob = json.dumps(caps, ensure_ascii=False)
    for key in ("bridge_protocol_version", "core_package_version",
                "server_version", "capabilities"):
        assert key in caps, key
    assert caps["capabilities"] == {
        "events": "v1", "prefetch": "v1", "tools": "v1",
        "source_read": "v1", "status": "v1",
    }
    # 不许把 release tag 硬编码进 core，也不许提前声明未实现能力
    assert "0.2.2" not in blob
    for forbidden in ("automatic_recall", "correction", "multi_agent_share"):
        assert forbidden not in caps["capabilities"]


def test_capabilities_have_single_source_of_truth():
    """HTTP /health 与 MCP v3_health 必须复用同一 helper，不许各写一份。"""
    root = Path(__file__).resolve().parents[1] / "src" / "v3core"
    serve_src = (root / "serve.py").read_text(encoding="utf-8")
    mcp_src = (root / "mcp_server.py").read_text(encoding="utf-8")
    assert "get_bridge_capabilities" in serve_src
    assert "get_bridge_capabilities" in mcp_src


def test_health_payload_carries_capabilities():
    root = Path(__file__).resolve().parents[1] / "src" / "v3core"
    serve_src = (root / "serve.py").read_text(encoding="utf-8")
    health_section = serve_src[: serve_src.index("def do_POST")]
    assert "get_bridge_capabilities" in health_section, "/health 未接入共享 capability"
    assert "payload.update" in health_section


# ── 6. msg_buffer 有界（不再承担 correctness 权威）────────────────

def test_msg_buffer_is_bounded():
    root = Path(__file__).resolve().parents[1] / "src" / "v3core"
    serve_src = (root / "serve.py").read_text(encoding="utf-8")
    for token in ("MAX_BUFFER_SESSIONS", "MAX_BUFFER_EVENTS_PER_SESSION",
                  "BUFFER_IDLE_TTL_S"):
        assert token in serve_src, token


# ── 7. lifecycle：owned child readiness 说真话 ────────────────────

def test_serve_ready_json_reports_real_port_source():
    root = Path(__file__).resolve().parents[1] / "src" / "v3core"
    serve_src = (root / "serve.py").read_text(encoding="utf-8")
    main_src = (root / "__main__.py").read_text(encoding="utf-8")
    # CLI 暴露 --ready-json；serve 侧必须回报实际绑定端口（port 0 时不能打印 0）
    assert "--ready-json" in main_src
    assert "ready_json" in serve_src
    assert "server_address" in serve_src or "getsockname" in serve_src
    # owned child 的生命周期契约：SIGTERM 走同一条 graceful 路径
    assert "SIGTERM" in serve_src
