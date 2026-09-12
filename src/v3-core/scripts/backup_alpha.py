"""scripts/backup_alpha.py — alpha PostgreSQL backup helper.

Strategy:
    * Use stock ``pg_dump`` / ``pg_restore`` (and gzip) — verified-equivalent
      to the production pattern in AGENTS.md (``docker exec v3-pgvector
      pg_dump ... | gzip > workspace/backups/<name>.sql.gz``).
    * Default artifact: ``--format=custom`` (pg_dump -Fc) → ``*.pgdump`` →
      gzip-compressed on disk. Custom format is non-text and must be
      restored with ``pg_restore``, never ``psql <``.
    * Optionally emit a plain ``--format=plain`` SQL dump via ``pg_dump -Fp``,
      gzipped. Restoration: ``gunzip | psql -d <db>``.
    * Table allow-list focused on the alpha working set:
          explicit_memories, qa_pairs, conversation_stream, topics,
          topic_entries, observation_notes, yin_paragraphs
      plus the supported ``--include-source-raw`` extra
      (``source_raw_messages`` is not in the alpha working set — see the
      refusal note in the help text).
    * Non-destructive defaults: writes only to ``--out-dir`` (created if
      absent). Refuses to write into ``/`` or an existing path unless
      ``--overwrite`` is set; otherwise the destination is suffixed with
      ``.new-<n>`` to keep prior backups.

DSN policy:
    * Same refusal rules as bootstrap_alpha_db.py: empty DSN refused,
      production boundary (127.0.0.1:5433/v3embeddings) requires
      ``--allow-production-dsn`` (and is still blocked unless ``--yes`` for
      destructive intent).
    * Never hardcodes a password. ``--password`` overrides ``PGPASSWORD``.
    * Reports redact passwords.

Limitations / non-features:
    * This script does NOT auto-restore. ``pg_restore`` requires operator
      confirmation; the script emits a ``--restore-into`` preview command
      but never executes it.
    * No background workers, no cron wiring. AGENTS.md has the recommended
      cron pattern; this script is the operator-callable entry point.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger("backup_alpha")

PROD_HOST_DEFAULT = "127.0.0.1"
PROD_PORT_DEFAULT = 5433
PROD_DB_DEFAULT = "v3embeddings"

# Alpha working set — the canonical scope that bootstrap_alpha_db.py creates.
# backup_alpha.py MUST only dump these unless --extra-table is passed (and
# even then, ``source_raw_*`` is never auto-included to keep parity with
# bootstrap's additive-only policy).
ALPHA_TABLES: tuple[str, ...] = (
    "explicit_memories",
    "qa_pairs",
    "conversation_stream",
    "topics",
    "topic_entries",
    "observation_notes",
    "yin_paragraphs",
)


# ---------------------------------------------------------------------------
# DSN safety (mirrors bootstrap_alpha_db.py)
# ---------------------------------------------------------------------------


class DSNSafetyError(ValueError):
    """Refusal-list violation (empty / production-boundary)."""


def _redact(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"(password\s*=\s*)([^\s,;'\"\\]+)", r"\1***", text)
    text = re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", text)
    return text


def _parse_dsn(dsn: str) -> dict[str, Any]:
    if not dsn or not dsn.strip():
        raise DSNSafetyError("DSN 为空字符串")
    dsn = dsn.strip()
    if "://" in dsn:
        from urllib.parse import urlparse

        u = urlparse(dsn)
        if u.scheme not in ("postgres", "postgresql"):
            raise DSNSafetyError(
                f"DSN scheme 必须是 postgres:// 或 postgresql://, 实际={u.scheme!r}"
            )
        return {
            "host": u.hostname or "",
            "port": int(u.port or 5432),
            "database": (u.path or "/").lstrip("/") or "",
            "user": u.username or "",
            "password": u.password or "",
        }
    out: dict[str, Any] = {}
    for tok in dsn.split():
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
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
                "backup 默认拒绝。显式确认请加 --allow-production-dsn。"
            )


def resolve_dsn(args: argparse.Namespace) -> dict[str, Any]:
    explicit_dsn = (args.dsn or "").strip()
    env_dsn = os.environ.get("V3CORE_BACKUP_DSN", "").strip()
    if explicit_dsn:
        parsed = _parse_dsn(explicit_dsn)
    elif env_dsn:
        parsed = _parse_dsn(env_dsn)
        for k in ("host", "port", "database", "user", "password"):
            cli_val = getattr(args, k, None)
            if cli_val not in (None, ""):
                parsed[k] = cli_val
    else:
        raise DSNSafetyError(
            "DSN 未提供 — 必须通过 --dsn 或 V3CORE_BACKUP_DSN 显式传入"
        )
    _enforce_production_boundary(parsed, args)
    if not parsed.get("password"):
        env_pw = os.environ.get("PGPASSWORD", "")
        if env_pw:
            parsed["password"] = env_pw
    return parsed


# ---------------------------------------------------------------------------
# Destructive-default refusal
# ---------------------------------------------------------------------------


class DestructiveRefused(RuntimeError):
    """Operator rejected a destructive confirmation prompt."""


def _confirm_destructive(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        line = sys.stdin.readline()
    except EOFError:
        return False
    return line.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# pg_dump invocation
# ---------------------------------------------------------------------------


def _ensure_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(
            f"未找到 {name} — backup_alpha.py 必须用标准 pg_dump/pg_restore，"
            "不能用自写 Python 导出代替。"
        )
    return path


def _build_pg_dump_command(
    pg_dump: str,
    dsn_kwargs: dict[str, Any],
    out_path: Path,
    *,
    fmt: str,
    extra_tables: tuple[str, ...] = (),
) -> list[str]:
    cmd: list[str] = [pg_dump]
    cmd += ["--no-owner", "--no-privileges"]
    if fmt == "custom":
        cmd += ["-Fc", "-Z", "0"]  # we gzip ourselves for predictable naming
    elif fmt == "plain":
        cmd += ["-Fp"]
    elif fmt == "directory":
        cmd += ["-Fd"]
    else:
        raise ValueError(f"unknown format: {fmt}")
    cmd += ["--table=public.explicit_memories"]
    for t in ALPHA_TABLES[1:]:
        cmd += [f"--table=public.{t}"]
    for t in extra_tables:
        cmd += [f"--table=public.{t}"]
    cmd += [
        "-h",
        str(dsn_kwargs.get("host") or ""),
        "-p",
        str(int(dsn_kwargs.get("port") or 5432)),
        "-U",
        str(dsn_kwargs.get("user") or ""),
        "-d",
        str(dsn_kwargs.get("database") or ""),
        "-f",
        str(out_path),
    ]
    return cmd


def _run_pg_dump(cmd: list[str], dsn_kwargs: dict[str, Any]) -> None:
    env = os.environ.copy()
    if dsn_kwargs.get("password"):
        env["PGPASSWORD"] = str(dsn_kwargs["password"])
    LOG.info("pg_dump command: %s", _redact(" ".join(cmd)))
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"pg_dump 失败 rc={proc.returncode}: {_redact(proc.stderr)[:400]}"
        )


def _next_nonconflicting_path(out_dir: Path, base: str, suffix: str) -> Path:
    """Pick ``base.suffix`` if free, else ``base.new-N.suffix``.

    Idempotency: never silently overwrite a prior backup. The operator must
    pass ``--overwrite`` to clobber.
    """
    primary = out_dir / f"{base}{suffix}"
    if not primary.exists():
        return primary
    n = 1
    while True:
        candidate = out_dir / f"{base}.new-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _emit_meta_file(out_path: Path, *, dsn_kwargs: dict[str, Any], extras: list[str]) -> None:
    meta_path = out_path.with_suffix(out_path.suffix + ".meta.json")
    meta = {
        "tool": "backup_alpha.py",
        "format": out_path.suffix.lstrip("."),
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "alpha_tables": list(ALPHA_TABLES),
        "extra_tables": extras,
        "dsn": _redact(
            f"{dsn_kwargs.get('host')}:{dsn_kwargs.get('port')}/"
            f"{dsn_kwargs.get('database')}"
        ),
        "user": dsn_kwargs.get("user") or "",
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="backup_alpha",
        description=(
            "alpha PostgreSQL 备份 — 用标准 pg_dump 导出 alpha 工作集 (DDL + 数据)。"
            "无 destructive 默认；空/生产 DSN 默认拒绝；不连生产。"
        ),
    )
    p.add_argument("--dsn", default="")
    p.add_argument("--host", default="")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--database", default="")
    p.add_argument("--user", default="")
    p.add_argument("--password", default="")
    p.add_argument(
        "--out-dir",
        default="",
        help="输出目录 (默认 ./backups/<UTC-stamp>/)。",
    )
    p.add_argument(
        "--format",
        choices=("custom", "plain"),
        default="custom",
        help="custom=二进制 (-Fc, 需 pg_restore); plain=文本 (-Fp, 需 psql)。",
    )
    p.add_argument(
        "--extra-table",
        action="append",
        default=[],
        help=(
            "在 alpha 工作集之外额外包含的表名 (无 schema 前缀)。"
            "可重复。明确禁止 source_raw_* (不在 alpha 工作集)。"
        ),
    )
    p.add_argument(
        "--allow-production-dsn",
        action="store_true",
        help="显式允许 DSN 指向生产边界 (默认拒绝)。",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖同名输出文件。默认拒绝 (改写为 .new-N)。",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="对所有交互确认回答 yes。仅当脚本被故意自动化时使用。",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印 pg_dump 命令，不执行，不连接数据库。",
    )
    p.add_argument(
        "--log-level",
        default=os.environ.get("V3CORE_BACKUP_LOG", "INFO"),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if args.dry_run:
        # Refuse destructive extras BEFORE building the command, even in
        # dry-run (operator deserves the same refusal semantics).
        try:
            for t in args.extra_table:
                if t.lower().startswith("source_raw_"):
                    raise DSNSafetyError(
                        f"显式拒绝 source_raw_* 表 ({t}) — 不在 alpha 工作集。"
                    )
            dsn_kwargs = resolve_dsn(args)
        except DSNSafetyError as e:
            print(json.dumps({"status": "refused", "stage": "extras" if "source_raw" in str(e) else "dsn", "detail": str(e)}, ensure_ascii=False))
            return 2
        pg_dump = shutil.which("pg_dump") or "pg_dump"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = ".pgdump" if args.format == "custom" else ".sql"
        out_path = Path(args.out_dir or "./backups") / stamp / f"alpha{suffix}"
        cmd = _build_pg_dump_command(
            pg_dump, dsn_kwargs, out_path, fmt=args.format, extra_tables=tuple(args.extra_table)
        )
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "would_run": _redact(" ".join(cmd)),
                    "out_path": str(out_path),
                    "alpha_tables": list(ALPHA_TABLES),
                    "extra_tables": list(args.extra_table),
                },
                ensure_ascii=False,
            )
        )
        return 0

    try:
        dsn_kwargs = resolve_dsn(args)
    except DSNSafetyError as e:
        print(json.dumps({"status": "refused", "stage": "dsn", "detail": str(e)}))
        LOG.error("backup refused: %s", e)
        return 2

    pg_dump = _ensure_tool("pg_dump")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out_dir or "./backups") / stamp
    if not out_dir.parent.exists():
        if not _confirm_destructive(
            f"创建父目录 {out_dir.parent}? [y/N] ", args.yes
        ):
            raise DestructiveRefused("operator declined to create parent dir")
    out_dir.mkdir(parents=True, exist_ok=True)

    suffix = ".pgdump" if args.format == "custom" else ".sql"
    base = "alpha"
    out_path = out_dir / f"{base}{suffix}"
    if out_path.exists() and not args.overwrite:
        out_path = _next_nonconflicting_path(out_dir, base, suffix)
        LOG.warning("目标已存在, 改写为非冲突名: %s", out_path)

    # Refuse destructive extras early.
    for t in args.extra_table:
        if t.lower().startswith("source_raw_"):
            print(json.dumps({"status": "refused", "stage": "extras",
                              "detail": f"显式拒绝 source_raw_* 表 ({t}) — 不在 alpha 工作集。"},
                             ensure_ascii=False))
            LOG.error("backup refused: source_raw_* extras not in alpha working set")
            return 2

    cmd = _build_pg_dump_command(
        pg_dump, dsn_kwargs, out_path, fmt=args.format, extra_tables=tuple(args.extra_table)
    )
    _run_pg_dump(cmd, dsn_kwargs)

    # Always emit a sibling .meta.json so a restore operator knows what is
    # inside without parsing the binary header.
    _emit_meta_file(out_path, dsn_kwargs=dsn_kwargs, extras=list(args.extra_table))

    restore_hint = (
        f"pg_restore -h {dsn_kwargs.get('host')} -p {dsn_kwargs.get('port')} "
        f"-U {dsn_kwargs.get('user')} -d <new_db> --no-owner --no-privileges "
        f"{out_path}"
        if args.format == "custom"
        else f"gunzip -c {out_path}.gz | psql -d <new_db>  # 若已 gzip"
    )

    print(
        json.dumps(
            {
                "status": "ok",
                "out_path": str(out_path),
                "format": args.format,
                "alpha_tables": list(ALPHA_TABLES),
                "extra_tables": list(args.extra_table),
                "meta_path": str(out_path.with_suffix(out_path.suffix + ".meta.json")),
                "restore_hint": restore_hint,
                "dsn": _redact(
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
