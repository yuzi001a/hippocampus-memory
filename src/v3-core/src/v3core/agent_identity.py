# -*- coding: utf-8 -*-
"""Agent identity — 记忆插件是常驻代理，向 provider 如实自报身份。

为什么需要
----------
OpenCode Go 的要求写得很直白（docs/console/go）：客户端要「用自己的 User-Agent，
而不是通用 SDK 或 HTTP 库的名字」，并且「每个会话发一个稳定的 session id，
方便我们做路由和提示词缓存」。缺 session 头直接 400 MissingSessionID。

我们原来的做法两处都不合规：

* ``v3core-{pid}-{hash}`` 把进程号写进会话 id —— 进程一重启就是「全新会话」，
  后端亲和与缓存全丢，路由每次重赌。
* 请求根本没设 User-Agent，出去就是 ``python-requests/2.x``。

现在的规则
----------
1. 会话 id 与**安装**绑定：首次调用在 profile 目录写 ``agent_session.json``，
   之后每次都读它。进程重启、升级、清缓存都不变，除非显式删掉这个文件。
2. ``config.yaml`` 可显式覆盖：``observer.session_id``（固定值）或
   ``agent.session_id``（全局），优先级最高。
3. User-Agent 默认 ``hippocampus-memory/<version>``，可由
   ``providers.<name>.user_agent`` 覆盖。
4. 可选 ``x-opencode-client`` / ``x-opencode-project``：``agent.client`` /
   ``agent.project``。两个都不是必须，官方只要求 session 头 + 自家 UA。

这一层不做别的事：不改提示词、不改模型、不碰数据。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from pathlib import Path

logger = logging.getLogger("v3core.agent_identity")

DEFAULT_CLIENT = "hippocampus-memory"
SESSION_FILE = "agent_session.json"

_LOCK = threading.Lock()
_CACHE: dict[str, str] = {}


def agent_version() -> str:
    """发行版本号；读不到就退回 '0'，绝不因此中断请求。"""
    try:
        from importlib.metadata import version as _pkg_version

        return str(_pkg_version("v3-core"))
    except Exception:
        return "0"


def default_user_agent() -> str:
    return f"{DEFAULT_CLIENT}/{agent_version()}"


def _profile_dir(profile_dir: str | os.PathLike[str] | None) -> Path:
    if profile_dir:
        return Path(profile_dir)
    try:
        from .config import _resolve_data_dir

        return _resolve_data_dir()
    except Exception:
        return Path.home() / ".v3-core" / "profiles" / "default"


def stable_session_id(profile_dir: str | os.PathLike[str] | None = None) -> str:
    """返回与安装绑定的会话 id；首次调用生成并落盘。

    落盘失败也要返回一个可用值（本次进程内稳定），因为「发不出 session 头」
    比「会话 id 每次变」严重得多 —— 前者是 400，后者只是亲和丢失。
    """
    root = _profile_dir(profile_dir)
    key = str(root)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached:
            return cached
        path = root / SESSION_FILE
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                sid = str(data.get("session_id") or "").strip()
                if sid:
                    _CACHE[key] = sid
                    return sid
            sid = uuid.uuid4().hex
            root.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"session_id": sid, "created_by": default_user_agent()},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("agent_identity: 生成安装级会话 id (%s)", path)
            _CACHE[key] = sid
            return sid
        except Exception as exc:
            sid = uuid.uuid4().hex
            logger.warning("agent_identity: 会话 id 落盘失败 (%s), 本次进程内临时使用", exc)
            _CACHE[key] = sid
            return sid


def resolve_session_id(cfg=None, profile_dir: str | os.PathLike[str] | None = None) -> str:
    """优先级：observer.session_id → agent.session_id → 安装级稳定 id。"""
    for section in (cfg, getattr(cfg, "agent", None)):
        if not section:
            continue
        try:
            explicit = section.get("session_id") if hasattr(section, "get") else None
        except Exception:
            explicit = None
        if explicit:
            return str(explicit).strip()
    return stable_session_id(profile_dir)


def resolve_user_agent(cfg=None) -> str:
    """``agent.client`` / ``agent.version`` 可拼出一个更具体的 UA。"""
    client = version = ""
    try:
        agent_cfg = cfg.get("agent") if hasattr(cfg, "get") else None
        if isinstance(agent_cfg, dict):
            client = str(agent_cfg.get("client") or "").strip()
            version = str(agent_cfg.get("version") or "").strip()
    except Exception:
        pass
    client = client or DEFAULT_CLIENT
    return f"{client}/{version or agent_version()}"
