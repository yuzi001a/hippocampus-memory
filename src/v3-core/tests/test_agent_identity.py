# -*- coding: utf-8 -*-
"""代理身份契约 — 会话 id 稳定、UA 自报、可配置覆盖。

为什么单独立这组测试
--------------------
OpenCode Go 的要求是「每个会话一个稳定 session id」+「用自己的 User-Agent」。
历史实现把进程号写进会话 id，重启就是新会话；请求也没设 UA，出去是 python-requests。
这两条都是静默缺陷：不会报错，只会让后端亲和、缓存、以及中转对客户端的判断全部失真。
"""
from __future__ import annotations

import json

import pytest

from v3core import agent_identity, providers


# ── 1. 安装级会话 id ────────────────────────────────────────────────────────
def test_session_id_is_stable_across_calls_and_processes(tmp_path):
    first = agent_identity.stable_session_id(tmp_path)
    agent_identity._CACHE.clear()          # 模拟进程重启
    second = agent_identity.stable_session_id(tmp_path)
    assert first == second
    stored = json.loads((tmp_path / agent_identity.SESSION_FILE).read_text(encoding="utf-8"))
    assert stored["session_id"] == first


def test_session_id_differs_per_install(tmp_path):
    a = agent_identity.stable_session_id(tmp_path / "install-a")
    b = agent_identity.stable_session_id(tmp_path / "install-b")
    assert a != b


def test_session_id_ignores_process_id(tmp_path):
    """回归：历史 id 形如 v3core-<pid>-<hash>，重启就变。"""
    import os

    agent_identity._CACHE.clear()
    sid = agent_identity.stable_session_id(tmp_path)
    assert str(os.getpid()) not in sid
    assert not sid.startswith("v3core-")


def test_session_file_survives_corruption(tmp_path):
    (tmp_path / agent_identity.SESSION_FILE).write_text("{not json", encoding="utf-8")
    agent_identity._CACHE.clear()
    sid = agent_identity.stable_session_id(tmp_path)
    assert sid


# ── 2. 显式配置优先 ────────────────────────────────────────────────────────
def test_explicit_session_id_wins(tmp_path):
    assert agent_identity.resolve_session_id({"session_id": "pinned-1"}, tmp_path) == "pinned-1"


def test_falls_back_to_install_id(tmp_path):
    agent_identity._CACHE.clear()
    resolved = agent_identity.resolve_session_id({}, tmp_path)
    assert resolved == agent_identity.stable_session_id(tmp_path)


# ── 3. UA / 身份头 ────────────────────────────────────────────────────────
CFG = {
    "providers": {
        "opencode-go": {
            "api_mode": "responses",
            "base_url": "https://opencode.ai/zen/go/v1",
            "session_header": "x-opencode-session",
            "supports": {"reasoning_effort": True},
        },
        "custom-ua": {
            "api_mode": "chat_completions",
            "base_url": "https://example.test/v1",
            "session_header": "x-session",
            "user_agent": "my-agent/9.9",
            "client_headers": {"x-client": "my-agent", "x-project": "proj-1"},
        },
    },
    "models": {
        "deepseek-v4.1-flash": {"provider": "opencode-go"},
        "custom-model": {"provider": "custom-ua"},
    },
}


def test_default_user_agent_is_not_generic():
    profile = providers.resolve_profile("deepseek-v4.1-flash", CFG, api_key="k")
    headers = providers.build_headers(profile, "k", session_id="sid-1")
    ua = headers.get("User-Agent", "")
    assert ua.startswith("hippocampus-memory/")
    assert "python-requests" not in ua.lower() and "python" not in ua.lower()
    assert headers["x-opencode-session"] == "sid-1"


def test_provider_can_override_user_agent_and_client_headers():
    profile = providers.resolve_profile("custom-model", CFG, api_key="k")
    headers = providers.build_headers(profile, "k", session_id="sid-2")
    assert headers["User-Agent"] == "my-agent/9.9"
    assert headers["x-client"] == "my-agent"
    assert headers["x-project"] == "proj-1"
    assert headers["x-session"] == "sid-2"


def test_user_agent_sent_even_without_session_header():
    """没声明 session_header 的通道也要有身份，不能退回 python-requests。"""
    profile = providers.build_profile("plain", {"api_mode": "chat_completions", "base_url": "https://x.test/v1"})
    headers = providers.build_headers(profile, "k")
    assert headers["User-Agent"].startswith("hippocampus-memory/")


# ── 4. observer 调用点 ────────────────────────────────────────────────────
def test_observer_uses_agent_session_id(monkeypatch):
    from v3core import observer

    monkeypatch.setattr(agent_identity, "resolve_session_id", lambda cfg, profile_dir=None: "install-scoped-1")
    assert observer._agent_session_id({"session_id": None}) == "install-scoped-1"
