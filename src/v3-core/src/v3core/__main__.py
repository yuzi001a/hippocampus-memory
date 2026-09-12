"""v3-core CLI — init / migrate / status"""
from __future__ import annotations
import argparse, json, os, shutil, sys
from pathlib import Path



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

def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    """交互式问答，支持默认值和密码模式"""
    if default:
        label = f"{label} [{default}]"
    try:
        val = input(f"  {label}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not val:
        return default
    return val


def _run_init_wizard():
    """交互式配置向导：生成 ~/.v3-core/profiles/<profile>/config.yaml"""
    print("v3-core 初始化配置向导")
    print("（留空=跳过/使用默认值，API key 建议稍后手动编辑文件填入）")
    print()

    profile = _prompt("Profile 名称", "default")
    config_dir = Path.home() / ".v3-core" / "profiles" / profile
    config_path = config_dir / "config.yaml"

    if config_path.exists():
        ok = _prompt(f"  {config_path} 已存在，覆盖？[y/N]", "N")
        if ok.lower() != "y":
            print("已取消。")
            return

    llm_provider = _prompt("LLM provider (留空跳过；可选 minimax/openai/ollama 或其他 OpenAI 兼容名)", "")
    llm_model = _prompt("LLM model", "" if not llm_provider else ("minimax-m3-4" if llm_provider == "minimax" else ""))
    llm_key = _prompt("LLM API key（留空跳过，稍后手动编辑）", "")
    embed_ep = _prompt("Embed endpoint（如 http://localhost:9999/v1/embeddings，留空跳过）", "")
    embed_key = _prompt("Embed API key", "")
    embed_proxy = _prompt("Embed proxy（如 http://127.0.0.1:10808）", "")
    rerank_ep = _prompt("Rerank endpoint（如 http://localhost:9998/v1/rerank）", "")
    rerank_key = _prompt("Rerank API key", "")
    rerank_proxy = _prompt("Rerank proxy", "")
    pg_host = _prompt("PG host", "localhost")
    pg_port = _prompt("PG port", "5433")
    pg_db = _prompt("PG database", "v3core")
    pg_user = _prompt("PG user", "v3user")
    pg_pass = _prompt("PG password", "")

    cfg = {"mode": "cloud", "basePath": str(config_dir)}

    llm = {"provider": llm_provider, "model": llm_model}
    if llm_key:
        llm["apiKey"] = "***  # 请手动编辑填入实际 key"
    cfg["llm"] = llm

    storage = {}
    if embed_ep:
        embed = {"endpoint": embed_ep, "dim": 1024}
        if embed_key:
            embed["apiKey"] = "***  # 请手动编辑填入实际 key"
        if embed_proxy:
            embed["proxy"] = embed_proxy
        storage["embed"] = embed
    if rerank_ep:
        rerank = {"endpoint": rerank_ep}
        if rerank_key:
            rerank["apiKey"] = "***  # 请手动编辑填入实际 key"
        if rerank_proxy:
            rerank["proxy"] = rerank_proxy
        storage["rerank"] = rerank
    storage["pg"] = {
        "host": pg_host,
        "port": int(pg_port) if pg_port.isdigit() else 5433,
        "database": pg_db,
        "user": pg_user,
    }
    if pg_pass:
        storage["pg"]["password"] = pg_pass
    cfg["storage"] = storage

    config_dir.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
    except ImportError:
        print("错误：缺少 yaml 库（pip install pyyaml）")
        return 1

    print()
    print(f"配置文件已写入：{config_path}")
    print()
    print("下一步：")
    if llm_key or embed_key or rerank_key:
        print("  1. 编辑配置文件，把 *** 替换为实际的 API key")
    print("  2. 运行 v3-core status 验证配置")
    print()
    return 0


def _do_migrate(source: str, target: str | None):
    # ... unchanged from original ...
    src = Path(source).expanduser()
    if not src.exists():
        print(f"错误: 源目录不存在: {src}")
        return 1
    if target:
        dst = Path(target).expanduser()
    else:
        dst = Path.home() / ".v3-core" / "profiles" / "default"
    print(f"迁移: {src} -> {dst}")

    src_b = src / "b"
    dst_cards = dst / "cards"
    if src_b.exists():
        for cat_dir in src_b.iterdir():
            if cat_dir.is_dir():
                dst_cat = dst_cards / cat_dir.name
                dst_cat.mkdir(parents=True, exist_ok=True)
                count = 0
                for md_file in cat_dir.glob("*.md"):
                    shutil.copy2(md_file, dst_cat / md_file.name)
                    count += 1
                print(f"  b/{cat_dir.name}/ -> cards/{cat_dir.name}/: {count} 张卡")

    src_j = src / "j"
    dst_j = dst / "journal"
    if src_j.exists():
        dst_j.mkdir(parents=True, exist_ok=True)
        count = 0
        for f in src_j.iterdir():
            if f.is_file():
                shutil.copy2(f, dst_j / f.name)
                count += 1
        print(f"  j/ -> journal/: {count} 个文件")

    src_s = src / "src"
    dst_s = dst / "sources"
    if src_s.exists():
        dst_s.mkdir(parents=True, exist_ok=True)
        count = 0
        for f in src_s.iterdir():
            if f.is_file():
                shutil.copy2(f, dst_s / f.name)
                count += 1
        print(f"  src/ -> sources/: {count} 个文件")

    src_p = src / "prompts"
    dst_p = dst / "prompts"
    if src_p.exists():
        dst_p.mkdir(parents=True, exist_ok=True)
        count = 0
        for f in src_p.iterdir():
            if f.is_file():
                shutil.copy2(f, dst_p / f.name)
                count += 1
        print(f"  prompts/ -> prompts/: {count} 个文件")

    src_cfg = src / "config.json"
    if src_cfg.exists():
        try:
            with open(src_cfg, encoding="utf-8") as fh:
                old_cfg = json.load(fh)
            yaml_cfg = {
                "mode": "cloud",
                "storage": {
                    "pg": {
                        "host": os.environ.get("V3CORE_PG_HOST", "localhost"),
                        "port": int(os.environ.get("V3CORE_PG_PORT", "5433")),
                        "database": os.environ.get("V3CORE_PG_DB", "v3core"),
                        "user": os.environ.get("V3CORE_PG_USER", "v3user"),
                        "password": os.environ.get("V3CORE_PG_PASSWORD", ""),
                    },
                    "embed": {
                        "endpoint": old_cfg.get("vectors", {}).get("endpoint", os.environ.get("V3CORE_BGE_ENDPOINT", "")),
                        "dim": old_cfg.get("vectors", {}).get("dim", 1024),
                    },
                },
                "llm": {
                    "provider": "",
                    "model": old_cfg.get("llm", {}).get("e1", {}).get("model", ""),
                },
            }
            try:
                import yaml
                with open(dst / "config.yaml", "w", encoding="utf-8") as fh:
                    yaml.dump(yaml_cfg, fh, default_flow_style=False, allow_unicode=True)
                print("  config.json -> config.yaml (已合并 pg/bge/llm 配置)")
            except ImportError:
                print("  config.json 转换跳过 (缺少 yaml 库)")
        except Exception as e:
            print(f"  配置迁移失败: {_safe_err(e)}")

    dst.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    state = {"migrated_at": datetime.now().isoformat(), "source": str(src)}
    with open(dst / "state.json", "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)

    print(f"\n迁移完成。数据在: {dst}")
    print("运行 'v3-core init' 完成配置向导。")
    return 0


def main():
    parser = argparse.ArgumentParser(description="v3-core 记忆系统")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="初始化配置向导")
    sub.add_parser("status", help="查看状态", aliases=["info"])
    migrate_p = sub.add_parser("migrate", help="从 yjby 迁移数据")
    migrate_p.add_argument("source", help="源目录 (如 ~/柚Deep/yjby)")
    migrate_p.add_argument("target", nargs="?", default=None, help="目标目录 (默认 ~/.v3-core/profiles/<profile>); 实际默认由 config.yaml basePath 决定")
    mcp_p = sub.add_parser("mcp", help="MCP server 模式 (stdio) — 让外部进程通过 MCP 协议调用 v3 记忆能力")
    mcp_p.add_argument("--profile", default="default", help="V3Core profile (默认 default)")
    serve_p = sub.add_parser("serve", help="HTTP serve 模式 — 让外部进程通过 HTTP 调用 v3 记忆能力")
    serve_p.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    serve_p.add_argument("--port", type=int, default=39090, help="监听端口 (默认 39090)")
    serve_p.add_argument("--profile", default="default", help="V3Core profile (默认 default)")

    args = parser.parse_args()
    if args.command == "info":
        args.command = "status"
    if args.command == "init":
        sys.exit(_run_init_wizard())
    elif args.command == "status":
        from . import V3Core
        core = V3Core()
        s = core.get_status()
        print(f"Status: {s}")
    elif args.command == "migrate":
        sys.exit(_do_migrate(args.source, args.target))
    elif args.command == "mcp":
        from .mcp_server import run_mcp
        run_mcp(profile=args.profile)
    elif args.command == "serve":
        from .serve import serve
        serve(host=args.host, port=args.port, profile=args.profile)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
