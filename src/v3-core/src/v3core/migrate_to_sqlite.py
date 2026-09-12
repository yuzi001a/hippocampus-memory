"""v3_cards.db 迁移脚本

将 cards/<cat>/*.md + .meta.json 一次性导入 SQLite v3_cards.db。

用法:
    python -m v3core.migrate_to_sqlite                              # 默认 profile
    python -m v3core.migrate_to_sqlite --profile myprofile           # 指定 profile
    python -m v3core.migrate_to_sqlite --keep-files                  # 保留源文件
    python -m v3core.migrate_to_sqlite --dry-run                     # 预览不动库

安全:
  - 迁移前自动备份现有 v3_cards.db → v3_cards.db.bak.<ts>
  - 源文件默认保留 (--keep-files), 可加 --remove-files 删除
  - 幂等: 重复跑会更新已存在的 source_id, 不产生重复
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
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

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("v3core.migrate_to_sqlite")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 markdown frontmatter (YAML/JSON/manual) + 正文"""
    parts = text.split("---", 2)
    body = parts[2].strip() if len(parts) >= 3 else text
    meta: dict = {}
    if len(parts) >= 2:
        fm = parts[1].strip()
        # JSON
        try:
            meta = json.loads(fm)
        except json.JSONDecodeError:
            # YAML
            try:
                import yaml as _y
                _m = _y.safe_load(fm)
                if isinstance(_m, dict):
                    for k, v in _m.items():
                        if hasattr(v, "isoformat"):
                            _m[k] = v.isoformat()
                        elif isinstance(v, list):
                            _m[k] = [str(x) if not isinstance(x, (str, int, float, bool)) else x for x in v]
                    meta = _m
            except Exception:
                # Manual: key: value lines
                for line in fm.split("\n"):
                    line = line.strip()
                    if ":" in line:
                        k, v = line.split(":", 1)
                        k = k.strip()
                        v = v.strip()
                        if k == "tags":
                            v = [t.strip().strip("\"'") for t in v.strip("[]").split(",") if t.strip()]
                        meta[k] = v
    return meta, body


def migrate(
    profile: str = "default",
    keep_files: bool = True,
    dry_run: bool = False,
    config_path: str | None = None,
) -> dict:
    """执行迁移

    Returns: 报告 dict
    """
    from .config import resolve_config
    from .sqlite_store import SqliteCardStore

    cfg = resolve_config(profile=profile)
    base = Path(cfg.get("basePath", str(Path.home() / ".v3-core" / "profiles" / profile))) if isinstance(cfg, dict) else Path(cfg.base_path)
    cards_dir = base / "cards"

    report = {
        "profile": profile,
        "base": str(base),
        "cards_dir": str(cards_dir),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "keep_files": keep_files,
        "skipped_dirs": [],
        "found_md": 0,
        "found_meta": 0,
        "migrated": 0,
        "updated": 0,
        "errors": [],
    }

    if not cards_dir.exists():
        report["errors"].append(f"cards dir not found: {cards_dir}")
        return report

    if not dry_run:
        # 备份已有 db
        db_path = base / SqliteCardStore.DB_NAME
        if db_path.exists():
            bak = db_path.with_suffix(f".db.bak.{int(time.time())}")
            shutil.copy2(db_path, bak)
            logger.info("已备份现有 %s → %s", db_path.name, bak.name)

        store = SqliteCardStore(base)

    # 扫描所有 .md
    all_cat_dirs = sorted(cards_dir.iterdir())
    for cat_dir in all_cat_dirs:
        if not cat_dir.is_dir():
            continue
        cat_name = cat_dir.name
        # 跳过 archive/ (去重归档目录)
        if cat_name == "archive":
            report["skipped_dirs"].append(cat_name)
            continue
        # 跳过 session_summaries/ (它们不是碑卡，走 session_summary 管线)
        if cat_name == "session_summaries":
            report["skipped_dirs"].append(cat_name)
            continue

        md_files = sorted(cat_dir.glob("*.md"))
        if not md_files:
            continue

        logger.info("处理类别 %s: %d 张卡", cat_name, len(md_files))

        for md_path in md_files:
            report["found_md"] += 1
            source_id = md_path.stem
            try:
                text = md_path.read_text(encoding="utf-8")
            except Exception as e:
                report["errors"].append(f"{cat_name}/{md_path.name}: 读文件失败: {_safe_err(e)}")
                continue

            # 解析 frontmatter
            meta, body = _parse_frontmatter(text)
            title = meta.get("title", source_id)
            tags = meta.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.strip("[]").split(",") if t.strip()] if tags else []
            confidence = float(meta.get("confidence", 0.3))
            observation_count = int(meta.get("observation_count", 1))
            source = meta.get("source", "migration")
            source_j_ids = meta.get("source_j_ids", [])
            when_ = str(meta.get("when", ""))
            where_ = str(meta.get("where", ""))
            who = str(meta.get("who", ""))
            why = str(meta.get("why", ""))

            # 读 .meta.json sidecar (如果有)
            meta_path = md_path.with_suffix(".meta.json")
            if meta_path.exists():
                report["found_meta"] += 1
                try:
                    mdata = json.loads(meta_path.read_text(encoding="utf-8"))
                    confidence = float(mdata.get("confidence", confidence))
                    observation_count = int(mdata.get("observation_count", observation_count))
                except Exception:
                    pass

            # 跳过 embedding — 迁移只转卡片数据, 嵌入由后续 batch 脚本补充
            embedding = None

            if dry_run:
                continue

            # 写入 SQLite
            try:
                store.write_card(
                    source_id=source_id,
                    category=cat_name,
                    title=title,
                    content=body,
                    tags=tags,
                    embedding=embedding,
                    confidence=confidence,
                    observation_count=observation_count,
                    source=source,
                    source_j_ids=source_j_ids if isinstance(source_j_ids, list) else [],
                    when_=when_,
                    where_=where_,
                    who=who,
                    why=why,
                )
                report["migrated"] += 1
            except Exception as e:
                report["errors"].append(f"{cat_name}/{source_id}: 写入失败: {_safe_err(e)}")

        # 每类完成后清理源文件 (除非 --keep-files)
        if not dry_run and not keep_files and md_files:
            logger.info("删除 %s 的源文件...", cat_name)
            for f in md_files:
                f.unlink(missing_ok=True)
                meta_path = f.with_suffix(".meta.json")
                if meta_path.exists():
                    meta_path.unlink(missing_ok=True)

    if not dry_run:
        logger.info("迁移完成: migrated=%d, updated=%d, errors=%d",
                     report["migrated"], report["updated"], len(report["errors"]))
        if keep_files:
            logger.info("源文件已保留 (可用 --remove-files 清理)")

    return report


def main() -> int:
    p = argparse.ArgumentParser(description="Migrate cards/*.md → v3_cards.db")
    p.add_argument("--profile", default="default", help="v3-core profile (default)")
    p.add_argument("--keep-files", action="store_true", default=True,
                   help="保留源文件 (默认)")
    p.add_argument("--remove-files", action="store_true",
                   help="迁移后删除源 .md/.meta.json 文件")
    p.add_argument("--dry-run", action="store_true", help="只扫描, 不写库")
    args = p.parse_args()

    keep = not args.remove_files  # 默认 keep; --remove-files 覆盖

    report = migrate(
        profile=args.profile,
        keep_files=keep,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if report.get("errors"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
