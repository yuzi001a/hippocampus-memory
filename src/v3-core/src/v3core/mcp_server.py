"""v3-core MCP server — stdio transport.

让任何 MCP 客户端 (dsh mcp-client / Claude Desktop / Cursor 等) 通过 MCP 协议
调用 v3 记忆能力 (与 serve.py 的 HTTP 模式并列的另一条外部调用通路).

实现方式: 官方 mcp SDK 1.28.1 + FastMCP (from mcp.server.fastmcp import FastMCP).
FastMCP 提供装饰器/注册工具 API; 我们在启动时把 13 个工具注册上去.

复用:
- 工具 dispatch: serve.py._dispatch_tool(core, name, args)
- V3Core 初始化: 与 serve.serve() 同款 (V3Core(profile).initialize()),
  启动时初始化一次, 进程生命周期内复用同一实例.

schema 处理:
- FastMCP 1.28.1 的 add_tool() 仅从 type hints 推断参数 schema, 不接受 raw JSON Schema.
- 我们用 13 工具的 union 参数集动态构造 handler 签名 — 每个参数 Optional (default=None),
  FastMCP 转 pydantic 后 schema.required 为空, 客户端可传任意子集; pydantic 会自动
  coerce int / list / bool 等 (None 默认值让字段变 Optional).
- 真实参数语义/校验在 _dispatch_tool 里完成 — 它走 v3core.tools.handle_tool_call 的原有逻辑.

入口: run_mcp(profile='default') — 阻塞, 跑 stdio 直到 MCP 客户端断开或 Ctrl+C.
CLI: python -m v3core mcp [--profile Z]
"""
from __future__ import annotations
import logging
import sys
import traceback
from typing import Any

from . import _safe_err

logger = logging.getLogger("v3core.mcp_server")


# ── 暴露的 13 个工具名 (与 v3hermes.__init__.get_tool_schemas() 的 exposed 集合一致) ──
_EXPOSED_TOOLS: set[str] = {
    # 统一入口 (4)
    "v3_add", "v3_get", "v3_update", "v3_manage",
    # 高频核心 (5)
    "v3_store", "v3_search", "v3_status", "v3_extract", "v3_prefetch",
    # 主题/手帐 (3)
    "v3_topic_correct", "v3_moc_overview", "v3_moc_get",
    # 健康 (1)
    "v3_health",
}


def _filter_schemas() -> list[dict]:
    """从 v3core.tools.get_tool_schemas() 里挑出 _EXPOSED_TOOLS 13 个工具的 schema."""
    from .tools import get_tool_schemas
    all_schemas = get_tool_schemas()
    return [s for s in all_schemas if s.get("name") in _EXPOSED_TOOLS]


def _collect_union_params(schemas: list[dict]) -> list[str]:
    """收集 13 工具 parameters.properties 的并集 (用于动态构造 handler 签名)."""
    union: set[str] = set()
    for s in schemas:
        params = (s.get("parameters") or {}).get("properties") or {}
        union.update(params.keys())
    # 排除任何以下划线开头的名字 (func_metadata 视为非法)
    return sorted(p for p in union if not p.startswith("_"))


def _make_dispatcher_handler(core, tool_name: str, params: list[str], dispatch_fn):
    """动态构造一个 FastMCP 可注册的 wrapper — 签名 = 所有 13 工具 params 的并集 (全部 Optional).

    客户端可传任意子集; pydantic 自动 coerce int/list/bool; 未传的字段 = None.
    handler 把收到的 dict 透传给 dispatch_fn (它返回字符串).
    """
    # 动态拼 Python 源代码 — 所有参数 Optional (default=None).
    # FastMCP 拒绝下划线开头的参数名, 所以 closure 变量用 ddfn/dcore/dname (安全字符).
    sig_args = ", ".join(f"{p}=None" for p in params)
    body_lines = ["    _kwargs = {}"] + [
        f"    if {p} is not None:\n        _kwargs['{p}'] = {p}"
        for p in params
    ] + [
        "    return ddfn(dcore, dname, _kwargs)",
    ]
    src = (
        "def _handler(" + sig_args + ", ddfn=ddfn, dcore=dcore, dname=dname):\n"
        + "\n".join(body_lines)
    )
    ns: dict[str, Any] = {
        "ddfn": dispatch_fn,    # 实际函数引用 — 通过默认值绑定进函数闭包
        "dcore": core,
        "dname": tool_name,
    }
    exec(src, ns)
    fn = ns["_handler"]
    fn.__name__ = tool_name
    fn.__qualname__ = tool_name
    return fn


def _tool_docstring(name: str, *, schemas: list[dict] | None) -> str:
    """构造工具 docstring (透传 v3 schema.description + 参数提示)."""
    from .tools import get_tool_schemas
    if schemas is None:
        schemas = get_tool_schemas()
    s = next((x for x in schemas if x.get("name") == name), None)
    desc = (s or {}).get("description", "")
    params = (s or {}).get("parameters", {})
    props = params.get("properties", {})
    req = params.get("required", [])
    # 简短 param 列表
    lines = [f"[MCP tool] {name}", ""]
    if desc:
        lines.extend([desc, ""])
    if props:
        lines.append("参数 (所有参数均 Optional, 未传走 None):")
        for k, v in props.items():
            t = v.get("type", "?")
            d = v.get("description", "")
            star = "*" if k in req else ""
            lines.append(f"  - {star}{k} ({t}): {d}")
    return "\n".join(lines)


def _build_mcp_server(core):
    """构造 FastMCP 实例 + 注册 13 个工具.

    每个工具的 MCP description 透传自 v3 schema.description (作为 docstring);
    每个工具的 MCP parameters = 13 工具 properties 的并集 (全部 Optional),
    客户端可传任意子集 — 实际参数语义在 _dispatch_tool 里走原有逻辑校验.

    返回 (mcp_server, tool_names) 二元组.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "未安装官方 mcp SDK (>=1.0). 请在 venv 里: pip install 'mcp>=1.0'"
        ) from e

    mcp = FastMCP("v3-memory")

    from .serve import _dispatch_tool

    schemas = _filter_schemas()
    union_params = _collect_union_params(schemas)
    logger.info("MCP 工具 union 参数集 (%d 个): %s", len(union_params), union_params)

    for schema in schemas:
        name = schema["name"]
        description = schema.get("description", "")
        params_schema = schema.get("parameters", {"type": "object", "properties": {}})

        # 构造 wrapper — 签名 = union_params (全部 Optional).
        handler = _make_dispatcher_handler(core, name, union_params, _dispatch_tool)
        # 透传 v3 description 作为 MCP description; 完整 docstring 用作附加文档.
        handler.__doc__ = _tool_docstring(name, schemas=schemas)
        mcp.add_tool(handler, name=name, description=description)

    return mcp, [s["name"] for s in schemas]


def run_mcp(profile: str = "default") -> None:
    """入口 — 初始化 V3Core + 起 stdio MCP server, 阻塞直到客户端断开."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    logger.info("v3core.mcp_server 启动: profile=%s", profile)

    # 1. 初始化 V3Core (与 serve.serve() 同款)
    try:
        from . import V3Core
        core = V3Core(profile=profile)
        core.initialize()
    except Exception as e:
        logger.error("V3Core 初始化失败: %s\n%s", _safe_err(e), traceback.format_exc())
        sys.exit(1)

    # 2. 注册工具
    try:
        mcp, tool_names = _build_mcp_server(core)
    except Exception as e:
        logger.error("构建 MCP server 失败: %s\n%s", _safe_err(e), traceback.format_exc())
        try:
            core.shutdown()
        except Exception:
            pass
        sys.exit(1)

    logger.info("MCP server 已注册 %d 个工具: %s", len(tool_names), ", ".join(tool_names))
    # 注意: 不要 print 到 stdout — 会污染 JSON-RPC 流 (stdio transport).
    # 启动信息走 logger (stderr).

    # 3. 阻塞运行 stdio
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt, 准备关闭…")
    except Exception as e:
        logger.error("MCP server 运行异常: %s\n%s", _safe_err(e), traceback.format_exc())
    finally:
        try:
            core.shutdown()
        except Exception as e:
            logger.warning("core.shutdown() 异常: %s", _safe_err(e))
        logger.info("v3core.mcp_server 已退出")


if __name__ == "__main__":
    run_mcp()