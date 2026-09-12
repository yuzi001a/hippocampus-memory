"""v3_organize_memory — 读写 5 文件 (印/MEMORY.md/USER.md/SOUL.md/手帐)

action=preview: 只读 5 文件返 JSON (含 SOUL.md)
action=apply: 传完整新内容写回 (带 .bak 备份), 手帐/soul 支持追加
不动印 y/ — 归 E1 cron 04:00 自动管

SOUL.md 路径通过 get_hermes_home() 解析, 不硬写.
"""
from __future__ import annotations
import json
import logging
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

logger = logging.getLogger("v3core.tools.organize")

V3_ORGANIZE_MEMORY_SCHEMA = {
    "name": "v3_organize_memory",
    "description": "[4-管理] 读取印 (y/) / MEMORY.md / USER.md / SOUL.md / 手帐 (s/system_state.md) 5 文件, "
                   "agent 拿到后自行判断整理, 用此工具写回. "
                   "绕过内置 memory add() 一条一条写. "
                   "action=preview 全读; action=apply 必填 memory_md + user_md, "
                   "soul_md / shou_zhang_append 选填. **不动印 y/** (归 E1 cron 04:00 自动管).",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["preview", "apply"],
                "default": "preview",
                "description": "preview=全读返 JSON; apply=写文件 (必填 memory_md + user_md, soul_md / shou_zhang_append 选填)",
            },
            "memory_md": {
                "type": "string",
                "description": "完整新 MEMORY.md 内容 (action=apply 填, action=preview 忽略)",
            },
            "user_md": {
                "type": "string",
                "description": "完整新 USER.md 内容 (action=apply 填, action=preview 忽略)",
            },
            "soul_md": {
                "type": "string",
                "default": "",
                "description": "新 SOUL.md 内容 (action=apply 选填; 空串=不写; 传内容=完整覆盖, 有 .bak)",
            },
            "shou_zhang_append": {
                "type": "string",
                "description": "追加到 s/system_state.md 末尾的内容 (action=apply 选填, 会加时间戳分隔)",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _resolve_soul_path():
    """Resolve SOUL.md via Hermes canonical mechanism, with graceful fallback."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "SOUL.md"
    except ImportError:
        pass
    import os
    hh = os.environ.get("HERMES_HOME")
    if hh:
        return Path(hh) / "SOUL.md"
    home = Path.home()
    candidates = [
        home / "AppData" / "Local" / "hermes" / "SOUL.md",
        home / ".hermes" / "SOUL.md",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _resolve_paths():
    """Resolve paths for the 5-file organize payload.

    y_dir / shou_zhang live under the v3-core data root, resolved via
    config.basePath (with fallback to ~/.v3-core/profiles/default).
    memory_md / user_md live under the Hermes-standard memories/ tree
    and are intentionally NOT moved when the data root changes.
    """
    home = Path.home()
    # Lazy import to avoid circular import at module load time.
    from ..config import resolve_config
    cfg = resolve_config()
    base_str = cfg.get("basePath", "") if isinstance(cfg, dict) else ""
    base = Path(base_str) if base_str else (home / ".v3-core" / "profiles" / "default")
    return {
        "y_dir": base / "y",
        "memory_md": home / "AppData" / "Local" / "hermes" / "memories" / "MEMORY.md",
        "user_md": home / "AppData" / "Local" / "hermes" / "memories" / "USER.md",
        "shou_zhang": base / "s" / "system_state.md",
    }


def _read_latest_yin(y_dir):
    if not y_dir.exists():
        return None
    yin_files = [p for p in y_dir.glob("y_*.md") if p.is_file()]
    if not yin_files:
        return None
    yin_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest = yin_files[0]
    try:
        content = latest.read_text(encoding="utf-8")
    except Exception as e:
        return {"path": str(latest), "error": f"read failed: {_safe_err(e)}"}
    return {
        "path": str(latest),
        "filename": latest.name,
        "mtime": latest.stat().st_mtime,
        "byte_count": len(content.encode("utf-8")),
        "content": content,
    }


def _read_text(path):
    if not path.exists():
        return {"path": str(path), "exists": False, "byte_count": 0, "content": ""}
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"path": str(path), "error": f"read failed: {_safe_err(e)}", "byte_count": 0, "content": ""}
    return {
        "path": str(path),
        "exists": True,
        "byte_count": len(content.encode("utf-8")),
        "content": content,
    }


def _write_text(path, content):
    import time as _t
    ts = int(_t.time())
    backup_path = path.with_suffix(path.suffix + f".bak.{ts}")
    try:
        if path.exists():
            backup_path.write_bytes(path.read_bytes())
    except Exception as e_bak:
        logger.warning("[v3_organize_memory] 备份失败 %s: %s", backup_path, str(e_bak)[:200])
        backup_path = None
    path.write_text(content, encoding="utf-8", newline="\n")
    return {
        "path": str(path),
        "byte_count": len(content.encode("utf-8")),
        "backup": str(backup_path) if backup_path else None,
    }


def _append_shou_zhang(path, content):
    from datetime import datetime as _dt
    now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    sep = f"\n\n---\n\n## 手动追加 ({now_str})\n\n"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8", newline="\n")
        full = sep.lstrip() + content + "\n"
    else:
        existing = path.read_text(encoding="utf-8")
        full = existing + sep + content + "\n"
    return _write_text(path, full)


def handle_v3_organize_memory(args, **kw):
    try:
        action = (args.get("action") or "preview").strip()
        if action not in ("preview", "apply"):
            return json.dumps({
                "success": False,
                "error": f"action 必须是 preview 或 apply, 收到: {action!r}",
            }, ensure_ascii=False)

        paths = _resolve_paths()
        soul_path = _resolve_soul_path()

        if action == "preview":
            yin = _read_latest_yin(paths["y_dir"])
            memory = _read_text(paths["memory_md"])
            user = _read_text(paths["user_md"])
            shou_zhang = _read_text(paths["shou_zhang"])
            soul = _read_text(soul_path)

            return json.dumps({
                "success": True,
                "action": "preview",
                "files": {
                    "latest_yin": yin,
                    "memory_md": memory,
                    "user_md": user,
                    "soul_md": soul,
                    "shou_zhang": shou_zhang,
                },
                "byte_counts": {
                    "yin": yin.get("byte_count", 0) if yin else 0,
                    "memory_md": memory.get("byte_count", 0),
                    "user_md": user.get("byte_count", 0),
                    "soul_md": soul.get("byte_count", 0),
                    "shou_zhang": shou_zhang.get("byte_count", 0),
                },
                "note": "agent 拿到 5 文件内容后自行整理, 用 action=apply + 传 memory_md + user_md + "
                        "可选 soul_md / shou_zhang_append 写回. 不动印 (y/) — 归 E1 cron 04:00.",
            }, ensure_ascii=False)

        memory_md_new = args.get("memory_md")
        user_md_new = args.get("user_md")
        soul_md_new = args.get("soul_md", "")
        shou_zhang_append = args.get("shou_zhang_append")

        if memory_md_new is None or user_md_new is None:
            return json.dumps({
                "success": False,
                "error": "action=apply 必填 memory_md + user_md 两个字段 (soul_md / shou_zhang_append 选填)",
            }, ensure_ascii=False)
        if not isinstance(memory_md_new, str) or not isinstance(user_md_new, str):
            return json.dumps({
                "success": False,
                "error": "memory_md / user_md 必须是字符串",
            }, ensure_ascii=False)
        if soul_md_new is not None and not isinstance(soul_md_new, str):
            return json.dumps({
                "success": False,
                "error": "soul_md 必须是字符串",
            }, ensure_ascii=False)
        if shou_zhang_append is not None and not isinstance(shou_zhang_append, str):
            return json.dumps({
                "success": False,
                "error": "shou_zhang_append 必须是字符串",
            }, ensure_ascii=False)

        wrote = []
        byte_counts = {}

        mem_result = _write_text(paths["memory_md"], memory_md_new)
        wrote.append("memory_md")
        byte_counts["memory_md"] = mem_result["byte_count"]
        if mem_result.get("backup"):
            byte_counts["memory_md_backup"] = mem_result["backup"]

        user_result = _write_text(paths["user_md"], user_md_new)
        wrote.append("user_md")
        byte_counts["user_md"] = user_result["byte_count"]
        if user_result.get("backup"):
            byte_counts["user_md_backup"] = user_result["backup"]

        if soul_md_new:
            soul_result = _write_text(soul_path, soul_md_new)
            wrote.append("soul_md")
            byte_counts["soul_md"] = soul_result["byte_count"]
            if soul_result.get("backup"):
                byte_counts["soul_md_backup"] = soul_result["backup"]

        if shou_zhang_append:
            shou_zhang_result = _append_shou_zhang(paths["shou_zhang"], shou_zhang_append)
            wrote.append("shou_zhang")
            byte_counts["shou_zhang_total"] = shou_zhang_result["byte_count"]
            if shou_zhang_result.get("backup"):
                byte_counts["shou_zhang_backup"] = shou_zhang_result["backup"]

        return json.dumps({
            "success": True,
            "action": "apply",
            "wrote": wrote,
            "byte_counts": byte_counts,
            "note": f"已写 {len(wrote)} 个文件. MEMORY.md / USER.md / SOUL.md 走完整覆盖 (前有 .bak 备份). 手帐 append 加时间戳分隔. 印 (y/) 未动 — 归 E1 cron 04:00.",
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("[v3_organize_memory] 失败")
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)
