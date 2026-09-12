"""v3_manage — 统一维护入口 (v3_extract / v3_import_seed / v3_import_full)"""
from __future__ import annotations
import json
import logging


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

logger = logging.getLogger("v3core.tools.api_manage")

V3_MANAGE_SCHEMA = {
    "name": "v3_manage",
    "description": "[4-管理] 维护操作 — 对话提取/种子导入/全量导入",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["extract", "import_seed", "import_full"],
                "description": "维护动作: extract=从对话提取记忆, import_seed=导入记忆文件到印层, import_full=全量导入(碑层)",
            },
            "raw_text": {"type": "string", "description": "待提取的对话文本 (extract mode, 最少200字符)"},
            "write": {"type": "boolean", "description": "extract: true=写入, false=预览 (默认 false)", "default": False},
            "experts": {
                "type": "array",
                "items": {"type": "string", "enum": ["decisions", "lessons", "projects", "system"]},
                "description": "extract: 多专家模式 — 同一段 raw_text 走多个专家 prompt 各产一张碑卡. 不传/空数组 = 通用模式.",
            },
            "memory_files": {"type": "array", "items": {"type": "string"}, "description": "要导入的文件绝对路径列表 (import_seed mode)"},
            "source": {"type": "string", "enum": ["honcho", "jsonl", "yjby", "md"], "description": "源类型 (import_full mode)"},
            "source_path": {"type": "string", "description": "源文件/目录绝对路径 (import_full mode)"},
            "category": {"type": "string", "description": "目标类别 (import_seed, 默认 memory)"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "附加标签 (import_seed)"},
            "dry_run": {"type": "boolean", "description": "import_full: true=预览 (默认 true)", "default": True},
            "max_cards": {"type": "integer", "description": "import_full: 最多处理卡数 (默认 100)"},
        },
        "required": ["action"],
    },
}

def handle_v3_manage(args: dict, **kw) -> str:
    """Unified management: delegate to v3_extract / v3_import_seed / v3_import_full"""
    action = args.get("action", "extract")
    try:
        if kw.get("runtime_context") and action in {"import_seed", "import_full"}:
            return json.dumps({
                "success": False,
                "error": "import actions are offline/migration-only and cannot run from a Runtime-backed provider",
            }, ensure_ascii=False)
        if action == "extract":
            raw_text = args.get("raw_text", "")
            if len(raw_text) < 200:
                return json.dumps({"success": False, "error": "raw_text needs at least 200 chars"}, ensure_ascii=False)
            from .extract_tool import handle_v3_extract
            return handle_v3_extract({
                "raw_text": raw_text,
                "write": args.get("write", False),
                "experts": args.get("experts"),
            }, **kw)
        elif action == "import_seed":
            memory_files = args.get("memory_files", [])
            if not memory_files:
                return json.dumps({"success": False, "error": "memory_files required for import_seed"}, ensure_ascii=False)
            from .import_ import handle_v3_import_seed
            return handle_v3_import_seed({
                "memory_files": memory_files,
                "category": args.get("category", "memory"),
                "tags": args.get("tags", ["seed", "import"]),
                "with_pg": True,
            }, **kw)
        elif action == "import_full":
            source = args.get("source", "")
            source_path = args.get("source_path", "")
            if not source or not source_path:
                return json.dumps({"success": False, "error": "source and source_path required for import_full"}, ensure_ascii=False)
            from .import_ import handle_v3_import_full
            return handle_v3_import_full({
                "source": source, "source_path": source_path,
                "target": "b",
                "write": not args.get("dry_run", True),
                "max_cards": args.get("max_cards", 100),
                "dry_run": args.get("dry_run", True),
            }, **kw)
        else:
            return json.dumps({"success": False, "error": f"unknown action: {action}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": _safe_err(e)}, ensure_ascii=False)

