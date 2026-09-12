"""scripts/bootstrap_alpha_db.py — single-entry alpha database bootstrap.

Scope (P0, additive only — no destructive reset):
    * Applies src/v3-core/schema/alpha_bootstrap.sql verbatim, plus the
      canonical explicit_memories.sql artifact referenced by it.
    * Idempotent: every DDL uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.
      Running twice is a no-op (verified by tests).
    * No DROP / TRUNCATE / DELETE statements. This script is bootstrap-only.

DSN policy (refusal list):
    * DSN must come from --dsn (CLI) or V3CORE_BOOTSTRAP_DSN env var.
      Empty string → refuse (exit code 2).
    * Default host:port (127.0.0.1:5433 / v3embeddings) is the **production**
      boundary defined in AGENTS.md. Refused by default; requires
      ``--allow-production-dsn`` to override (still requires explicit
      --dsn/--host/--database/--user; never hardcodes a password).
    * Never hardcodes a password. If --password is omitted, falls back to
      PGPASSWORD env (psycopg2 honors it natively). All reports redact the
      password to ***.

Output:
    * Single-line JSON status on stdout (machine-readable).
    * Operator narrative on stderr.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import psycopg2
except ImportError as e:  # pragma: no cover - import guard for runtime
    print(json.dumps({"status": "error", "stage": "import", "detail": str(e)}))
    sys.exit(3)


LOG = logging.getLogger("bootstrap_alpha_db")

# Production boundary (AGENTS.md: PG `localhost:5433 / v3embeddings` is the
# production data path — bootstrap against it is destructive-by-default).
PROD_HOST_DEFAULT = "127.0.0.1"
PROD_PORT_DEFAULT = 5433
PROD_DB_DEFAULT = "v3embeddings"

ALPHA_BOOTSTRAP_INCLUDE_RE = re.compile(
    r"--\s*>>>\s*ALPHA_BOOTSTRAP_INCLUDE:\s*(\S+)\s*<<<"
)


# ---------------------------------------------------------------------------
# DSN resolution / safety
# ---------------------------------------------------------------------------


class DSNSafetyError(ValueError):
    """Raised when the DSN arguments fail safety policy (refusal-list)."""


def _redact_password(text: str) -> str:
    """Redact ``password=...`` and URI ``:password@`` segments.

    Reports must never leak credentials. We accept the small risk of false
    positives in operator-visible text (any literal "password=xxx" gets
    masked) over the certainty of leaking secrets.
    """
    if not text:
        return text
    text = re.sub(r"(password\s*=\s*)([^\s,;'\"\\]+)", r"\1***", text)
    text = re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", text)
    return text


def resolve_dsn(args: argparse.Namespace) -> dict[str, Any]:
    """Build a psycopg2 kwargs dict from args + env, with safety checks.

    Precedence:
        --dsn (literal DSN) > --host/--port/--database/--user/--password
        > V3CORE_BOOTSTRAP_DSN env.

    Refusal rules (DSNSafetyError):
        * Empty / whitespace-only DSN.
        * Host == 127.0.0.1 (or localhost) AND port == 5433 AND db ==
          v3embeddings AND --allow-production-dsn was not set.
        * password == "" explicit on CLI AND PGPASSWORD env unset (we
          require *some* credential material — never assume auth_method=none).
    """
    explicit_dsn = (args.dsn or "").strip()
    env_dsn = os.environ.get("V3CORE_BOOTSTRAP_DSN", "").strip()

    if explicit_dsn:
        dsn = explicit_dsn
        host = port = database = user = password = None
    else:
        if env_dsn:
            return _kwargs_from_env_dsn(env_dsn, args)
        # Component-arg path: --host/--port/--database/--user are all
        # required when no literal DSN / env DSN is provided. Password
        # falls back to PGPASSWORD (psycopg2 honors it natively).
        cli_host = (args.host or "").strip()
        cli_port = int(args.port or 0)
        cli_database = (args.database or "").strip()
        cli_user = (args.user or "").strip()
        cli_password = args.password or ""
        if cli_host and cli_port and cli_database and cli_user:
            if not cli_password:
                cli_password = os.environ.get("PGPASSWORD", "") or ""
            parsed = {
                "host": cli_host,
                "port": cli_port,
                "database": cli_database,
                "user": cli_user,
                "password": cli_password,
            }
            _enforce_production_boundary(parsed, args)
            if not parsed.get("password"):
                raise DSNSafetyError(
                    "DSN 组件不完整 — 必须提供 --password 或设置 PGPASSWORD"
                )
            return parsed
        # No DSN anywhere → refuse.
        raise DSNSafetyError(
            "DSN 未提供 — 必须通过 --dsn、V3CORE_BOOTSTRAP_DSN 或完整 "
            "--host/--port/--database/--user 组件传入"
        )

    # We have an explicit --dsn. Sanity-check the production boundary inside
    # it even before we let it through.
    parsed = _parse_dsn(dsn)
    _enforce_production_boundary(parsed, args)
    if not parsed.get("password"):
        env_pw = os.environ.get("PGPASSWORD", "")
        if env_pw:
            parsed["password"] = env_pw
    return parsed


def _kwargs_from_env_dsn(env_dsn: str, args: argparse.Namespace) -> dict[str, Any]:
    parsed = _parse_dsn(env_dsn)
    # CLI flags override env DSN components.
    for k in ("host", "port", "database", "user", "password"):
        cli_val = getattr(args, k, None)
        if cli_val not in (None, ""):
            parsed[k] = cli_val
    _enforce_production_boundary(parsed, args)
    if not parsed.get("password"):
        env_pw = os.environ.get("PGPASSWORD", "")
        if env_pw:
            parsed["password"] = env_pw
    return parsed


def _parse_dsn(dsn: str) -> dict[str, Any]:
    """Parse a libpq-style DSN string.

    Accepts either key=value key=value ... pairs (libpq / psycopg2.connect)
    or URI form (postgresql://user@host:port/db?...). URI form is normalized
    to kwargs.

    Accepts both ``dbname=`` (libpq convention) and ``database=`` (psycopg2
    shortcut) for the database parameter; both map to ``database``.
    """
    if not dsn or not dsn.strip():
        raise DSNSafetyError("DSN 为空字符串")
    dsn = dsn.strip()
    if "://" in dsn:
        from urllib.parse import urlparse, parse_qs

        u = urlparse(dsn)
        if u.scheme not in ("postgres", "postgresql"):
            raise DSNSafetyError(
                f"DSN scheme 必须是 postgres:// 或 postgresql://, 实际={u.scheme!r}"
            )
        host = u.hostname or ""
        port = u.port or 5432
        database = (u.path or "/").lstrip("/") or ""
        user = u.username or ""
        password = u.password or ""
        # libpq URI may carry query params; we ignore unknown ones.
        _ = parse_qs(u.query)
        return {
            "host": host,
            "port": int(port),
            "database": database,
            "user": user,
            "password": password,
        }
    out: dict[str, Any] = {}
    for token in dsn.split():
        if "=" not in token:
            continue
        k, v = token.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k == "port":
            try:
                out["port"] = int(v)
            except ValueError as e:
                raise DSNSafetyError(f"DSN port 非整数: {v!r}") from e
        elif k in ("dbname", "database"):
            out["database"] = v
        else:
            out[k] = v
    return out


def _enforce_production_boundary(parsed: dict[str, Any], args: argparse.Namespace) -> None:
    host = (parsed.get("host") or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        port = int(parsed.get("port") or 0)
        database = (parsed.get("database") or "").lower()
        if (
            port == PROD_PORT_DEFAULT
            and database == PROD_DB_DEFAULT
            and not getattr(args, "allow_production_dsn", False)
        ):
            raise DSNSafetyError(
                f"DSN 指向生产边界 ({host}:{port}/{database}) — "
                "bootstrap 默认拒绝空/生产 DSN。显式确认请加 --allow-production-dsn"
            )


# ---------------------------------------------------------------------------
# SQL loading
# ---------------------------------------------------------------------------


def load_alpha_ddl(repo_root: Path) -> tuple[str, list[str]]:
    """Load and concatenate alpha_bootstrap.sql + explicit_memories.sql.

    Returns the combined DDL string and the list of include paths actually
    resolved (for reporting). The marker line in alpha_bootstrap.sql
    (ALPHA_BOOTSTRAP_INCLUDE) is replaced with the body of the referenced
    file. Idempotency is preserved (every DDL uses IF NOT EXISTS).
    """
    bootstrap_path = repo_root / "src" / "v3-core" / "schema" / "alpha_bootstrap.sql"
    if not bootstrap_path.exists():
        raise FileNotFoundError(f"alpha_bootstrap.sql 不存在: {bootstrap_path}")
    bootstrap_sql = bootstrap_path.read_text(encoding="utf-8")

    includes_resolved: list[str] = []
    pieces: list[str] = []

    cursor = 0
    for m in ALPHA_BOOTSTRAP_INCLUDE_RE.finditer(bootstrap_sql):
        pieces.append(bootstrap_sql[cursor : m.start()])
        rel = m.group(1)
        include_path = (bootstrap_path.parent / Path(rel).name).resolve()
        if not include_path.exists():
            raise FileNotFoundError(
                f"alpha_bootstrap.sql 引用了不存在的文件: {rel} → {include_path}"
            )
        includes_resolved.append(str(include_path))
        pieces.append(f"\n-- BEGIN INCLUDED {rel}\n")
        pieces.append(include_path.read_text(encoding="utf-8"))
        pieces.append(f"\n-- END INCLUDED {rel}\n")
        cursor = m.end()
    pieces.append(bootstrap_sql[cursor:])

    combined = "".join(pieces)
    return combined, includes_resolved


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def apply_ddl(dsn_kwargs: dict[str, Any], ddl: str) -> dict[str, Any]:
    """Open a connection, apply DDL in one transaction, report counts."""
    conn = psycopg2.connect(
        host=dsn_kwargs.get("host") or "",
        port=int(dsn_kwargs.get("port") or 5432),
        database=dsn_kwargs.get("database") or "",
        user=dsn_kwargs.get("user") or "",
        password=dsn_kwargs.get("password") or "",
        connect_timeout=int(os.environ.get("V3CORE_BOOTSTRAP_CONNECT_TIMEOUT", "10")),
    )
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name IN ("
                "  'explicit_memories','qa_pairs','conversation_stream',"
                "  'topics','topic_entries','observation_notes',"
                "  'yin_paragraphs'"
                ") ORDER BY table_name"
            )
            tables = [r[0] for r in cur.fetchall()]
        return {"tables_present": tables}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bootstrap_alpha_db",
        description=(
            "单入口 alpha 数据库 bootstrap — 应用 src/v3-core/schema/"
            "alpha_bootstrap.sql (含 explicit_memories) 到目标 PostgreSQL。"
            "幂等、显式、无 destructive reset；默认拒绝空/生产 DSN。"
        ),
    )
    p.add_argument(
        "--dsn",
        default="",
        help=(
            "libpq-style DSN 字符串 (key=value k=v ... 或 URI)。"
            "优先级最高。"
        ),
    )
    p.add_argument("--host", default="")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--database", default="")
    p.add_argument("--user", default="")
    p.add_argument("--password", default="")
    p.add_argument(
        "--allow-production-dsn",
        action="store_true",
        help=(
            "显式允许 DSN 指向生产边界 (127.0.0.1:5433/v3embeddings)。"
            "默认拒绝；该开关不绕过凭据要求。"
        ),
    )
    p.add_argument(
        "--repo-root",
        default="",
        help="v3-core 仓库根目录 (默认自动向上查找 alpha_bootstrap.sql)。",
    )
    p.add_argument(
        "--check-only",
        action="store_true",
        help="只解析 + 装载 DDL, 不连接数据库。常用于 CI / 合同测试。",
    )
    p.add_argument(
        "--log-level",
        default=os.environ.get("V3CORE_BOOTSTRAP_LOG", "INFO"),
    )
    return p.parse_args(argv)


def _script_dir() -> Path:
    """Return the directory containing the executing bootstrap_alpha_db.py.

    ``Path(__file__).resolve()`` is the canonical way to locate the script
    regardless of cwd / symlinks / how the script was invoked
    (direct python, ``-m``, zipapp, etc.). Tests that import the module
    programmatically get the same value (the module's __file__).
    """
    return Path(__file__).resolve().parent


def _find_repo_root(start: Path) -> Path:
    """Walk upward from ``start`` looking for the schema marker.

    Bootstrap layout::

        <repo_root>/AGENTS.md  (or any unique outer marker)
        <repo_root>/src/v3-core/schema/alpha_bootstrap.sql

    The schema file is the operational truth: that file is what
    ``load_alpha_ddl`` reads, and ``bootstrap_alpha_db.py`` ships
    alongside it under ``<repo_root>/src/v3-core/scripts/``. A caller's
    cwd must NOT influence which copy of the schema is selected — a
    public script must load the schema adjacent to the executing
    script copy by default, never silently fall back to a sibling
    checkout's ancestor.
    """
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        candidate = parent / "src" / "v3-core" / "schema" / "alpha_bootstrap.sql"
        if candidate.exists():
            return parent
    raise FileNotFoundError(
        "找不到 alpha_bootstrap.sql — 用 --repo-root 显式指定仓库根"
    )


def _resolve_repo_root(args: argparse.Namespace) -> Path:
    """Resolve the repository root with strict policy.

    Priority:

      1. ``--repo-root`` (explicit override): must contain the schema
         marker; otherwise fail closed (FileNotFoundError).
      2. Default: walk upward from the executing script's own directory
         (``Path(__file__).resolve().parent``). The script ships inside
         ``<repo_root>/src/v3-core/scripts/``, so the schema sits two
         directories up — never the caller's cwd, which may belong to a
         different checkout (e.g. clean export tarball unzipped outside
         the source tree).
    """
    if args.repo_root:
        root = Path(args.repo_root).resolve()
        schema = root / "src" / "v3-core" / "schema" / "alpha_bootstrap.sql"
        if not schema.is_file():
            raise FileNotFoundError(
                f"--repo-root 指定的目录不包含 alpha_bootstrap.sql: {root}"
            )
        return root
    return _find_repo_root(_script_dir())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    try:
        repo_root = _resolve_repo_root(args)
    except FileNotFoundError as e:
        print(json.dumps({"status": "error", "stage": "locate", "detail": str(e)}))
        return 4

    try:
        ddl, includes = load_alpha_ddl(repo_root)
    except FileNotFoundError as e:
        print(json.dumps({"status": "error", "stage": "load_ddl", "detail": str(e)}))
        return 4

    LOG.info("alpha bootstrap DDL loaded; includes=%s", includes)

    if args.check_only:
        print(
            json.dumps(
                {
                    "status": "check_only",
                    "repo_root": str(repo_root),
                    "includes": includes,
                    "ddl_chars": len(ddl),
                },
                ensure_ascii=False,
            )
        )
        return 0

    try:
        dsn_kwargs = resolve_dsn(args)
    except DSNSafetyError as e:
        print(json.dumps({"status": "refused", "stage": "dsn", "detail": str(e)}))
        LOG.error("bootstrap refused: %s", e)
        return 2

    redacted = _redact_password(
        "host={host} port={port} db={database} user={user} password={password}".format(
            **dsn_kwargs
        )
    )
    LOG.info("connecting with %s", redacted)

    try:
        report = apply_ddl(dsn_kwargs, ddl)
    except Exception as e:
        # Sanitize: never let a raw exception traceback (which may echo
        # back DSN kwargs, including password material) leak into the log.
        # The exception TYPE + a redacted one-line excerpt is enough for
        # diagnosability; full traceback is discarded.
        safe_detail = _redact_password(f"{type(e).__name__}: {e}")[:400]
        print(
            json.dumps(
                {
                    "status": "error",
                    "stage": "apply_ddl",
                    "detail": safe_detail,
                },
                ensure_ascii=False,
            )
        )
        LOG.error("apply_ddl failed: %s", safe_detail)
        return 5

    print(
        json.dumps(
            {
                "status": "ok",
                "tables_present": report["tables_present"],
                "includes": includes,
                "dsn": _redact_password(
                    f"{dsn_kwargs.get('host')}:{dsn_kwargs.get('port')}/"
                    f"{dsn_kwargs.get('database')}"
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
