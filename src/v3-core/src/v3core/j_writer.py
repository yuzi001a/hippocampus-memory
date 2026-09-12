"""j_writer — L0 trace write: session turns -> j/<session_id>/"""

from __future__ import annotations
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("v3core.j_writer")


def _j_dir(config) -> Path:
    """Get j/ directory path from config (V3Config | dict | None)."""
    base = _extract_base_path(config)
    if base:
        return Path(base) / "j"
    return Path.home() / ".v3-core" / "profiles" / "default" / "j"


def _extract_base_path(config) -> str:
    """兼容 V3Config / dict / None — 提取 basePath/base_path 字符串."""
    if config is None:
        return ""
    if isinstance(config, dict):
        return config.get("basePath", "") or ""
    return getattr(config, "base_path", "") or ""


def write_turn(session_id: str, messages: list[dict], config: dict,
               platform: str = "", model: str = "",
               signal_dict: dict | None = None) -> int:
    """Write a session turn to j/<session_id>/turns.md.

    signal_dict: optional standardized dict (e.g. from Signal.payload).
      When provided, takes precedence over messages for the content.
      Format: {"role": str, "content": str, "turn_id": int, "timestamp": str}

    Returns number of messages written.
    """
    if not session_id:
        return 0

    # Use signal_dict if provided (standardized path)
    if signal_dict:
        role = signal_dict.get("role", "unknown")
        content = (signal_dict.get("content", "") or "")[:5000]
        turn_id = signal_dict.get("turn_id", 0)
        timestamp = signal_dict.get("timestamp", "") or datetime.now().isoformat()
        user_msg = ""
    else:
        # Legacy path: extract from messages list
        if not messages:
            return 0
        last = messages[-1]
        role = last.get("role", "unknown")
        content = (last.get("content", "") or "")[:5000]
        timestamp = datetime.now().isoformat()
        turn_id = 0
        user_msg = ""
        for m in reversed(messages[:-1]):
            if m.get("role") == "user":
                user_msg = (m.get("content", "") or "")[:2000]
                break

    j_dir = _j_dir(config)
    sess_dir = j_dir / session_id
    sess_dir.mkdir(parents=True, exist_ok=True)
    filepath = sess_dir / "turns.md"

    # Determine turn number from existing file
    turn_num = 1
    if filepath.exists():
        try:
            for line in open(filepath, encoding="utf-8"):
                if line.startswith("## Turn "):
                    turn_num += 1
        except Exception:
            pass

    # Signal path: all data already extracted above
    if not signal_dict:
        # Legacy path: extract from messages list
        if not messages:
            return 0
        last = messages[-1]
        role = last.get("role", "unknown")
        content = (last.get("content", "") or "")[:5000]
        timestamp = datetime.now().isoformat()
        turn_id = 0
        user_msg = ""
        for m in reversed(messages[:-1]):
            if m.get("role") == "user":
                user_msg = (m.get("content", "") or "")[:2000]
                break

    # Build markdown entry
    entry_parts = [
        "## Turn " + str(turn_num),
        "> timestamp: " + timestamp + " | role: " + role,
    ]
    if platform:
        entry_parts.append("> source: " + platform + " | model: " + model)
    if user_msg:
        entry_parts.append("")
        entry_parts.append("**user:**")
        entry_parts.append(user_msg)
    entry_parts.append("")
    entry_parts.append("**" + role + ":**")
    entry_parts.append(content)
    entry_parts.append("")
    entry_parts.append("---")
    entry_parts.append("")

    mode = "a" if turn_num > 1 else "w"
    with open(filepath, mode, encoding="utf-8") as f:
        f.write(chr(10).join(entry_parts) + chr(10))

    return 1


def read_turns(session_id: str, config: dict) -> str:
    """Read full j/ file for a session"""
    fp = _j_dir(config) / session_id / "turns.md"
    if not fp.exists():
        return ""
    return fp.read_text(encoding="utf-8", errors="replace")


def get_message_context(source_id: str, config: dict,
                        window: int = 5) -> dict | None:
    """Find a message by source_id in j/ files and return +-window lines
    of context around the match. Returns
    {source_id, session_id, content, line_range} or None."""
    j_dir = _j_dir(config)
    if not j_dir.exists():
        return None

    # Search all session dirs for the source_id
    for sess_dir in sorted(j_dir.iterdir()):
        if not sess_dir.is_dir():
            continue
        turns_file = sess_dir / "turns.md"
        if not turns_file.exists():
            continue

        text = turns_file.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        # Locate the source_id on a specific line and return +-window context
        for i, line in enumerate(lines):
            if source_id in line:
                start = max(0, i - window)
                end = min(len(lines), i + window + 1)
                context = chr(10).join(lines[start:end])
                _line_range = str(start + 1) + "-" + str(end)
                return {
                    "source_id": source_id,
                    "session_id": sess_dir.name,
                    "content": context,
                    "line_range": _line_range,
                }

    return None
