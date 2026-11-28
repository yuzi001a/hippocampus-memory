"""打包副本里必须能建出"代码真的会写"的列 —— 这条没测试盯着, 已经漏过一次。

发现 (2026-11-20, 发布风险对抗审查): ``src/v3-core/schema/alpha_bootstrap.sql``
给 ``conversation_stream`` 加了 ``session_source`` (P0-A session provenance,
2026-11-11), 但真正被打包、被 ``schema_contract._read_sql`` 通过
``resources.files("v3core.schema")`` 读取的那份 ``src/v3core/schema/`` 里的
副本停在旧版本, ALTER 和索引都没有。而 ``importers._RAW_DEDUPE_SQL_F2`` 显式
INSERT ``session_source`` —— 任何通过 wheel 安装的全新库, 第一次导入就会
UndefinedColumn; 生产库因为早先手工应用过真源 DDL 所以看不见这个问题。

两份副本不整体追平 (真源还带着尚未写 upgrade guard 的表, 全量同步会让
``test_schema_contract`` 的老库可达性门直接红), 所以这里钉的是更窄但更要紧的
不变量: **打包 DDL 必须建得出代码会写入的每一列**。
"""
from __future__ import annotations

import pathlib
import re

CORE = pathlib.Path(__file__).resolve().parents[1]
SOURCE_DIR = CORE / "schema"
PACKAGED_DIR = CORE / "src" / "v3core" / "schema"

_INCLUDE_RE = re.compile(r"^--\s*>>>\s*ALPHA_BOOTSTRAP_INCLUDE:\s*(?:schema/)?(\S+)\s*<<<\s*$", re.M)


def _text(path: pathlib.Path) -> str:
    # 比对内容而不是字节: Windows 检出会把 LF 变 CRLF, 那是检出差异不是漂移
    return path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")


def test_every_packaged_sql_also_exists_in_the_source_tree():
    for path in sorted(PACKAGED_DIR.glob("*.sql")):
        assert (SOURCE_DIR / path.name).exists(), (
            f"{path.name} 只在打包目录存在 — 下次从真源同步时会丢"
        )


def test_every_spliced_sidecar_ships_with_the_package():
    """bootstrap 用 INCLUDE 标记把 sidecar 内联进去 —— 它们必须真的在包里。"""
    missing = [
        name
        for name in _INCLUDE_RE.findall(_text(PACKAGED_DIR / "alpha_bootstrap.sql"))
        if not (PACKAGED_DIR / name).exists()
    ]
    assert not missing, f"bootstrap 引用了包内不存在的 sidecar: {missing}"


def test_packaged_bootstrap_carries_the_columns_importers_insert():
    """导入路径显式 INSERT 的列, 打包 DDL 必须建得出来 (本文件发现的那个 bug)。"""
    bootstrap = _text(PACKAGED_DIR / "alpha_bootstrap.sql")
    importers = _text(CORE / "src" / "v3core" / "importers" / "__init__.py")
    insert = importers.split("_RAW_DEDUPE_SQL_F2", 1)[1].split('"""', 2)[1]
    columns = [c.strip() for c in insert.split("(", 1)[1].split(")", 1)[0].split(",")]
    assert columns, "没能从 _RAW_DEDUPE_SQL_F2 里解析出列名"
    missing = [c for c in columns if c not in bootstrap]
    assert not missing, (
        f"conversation_stream 的这些列被导入路径写入, 但打包 bootstrap 没有: {missing}"
    )


def test_packaged_upgrade_adds_the_same_provenance_column():
    """老库走 upgrade 路径, 同样必须拿到 session_source。"""
    upgrade = _text(PACKAGED_DIR / "upgrade_v0_2.sql")
    assert "ADD COLUMN IF NOT EXISTS session_source" in upgrade
    assert "conversation_stream_session_source_idx" in upgrade


def test_manual_ddl_files_ship_with_the_package():
    """运行时提示「先执行 schema/task_events.sql」——wheel 用户得能拿到这个文件。"""
    for name in ("task_events.sql", "session_policies.sql", "topic_claims.sql"):
        assert (PACKAGED_DIR / name).exists(), f"{name} 没有打进包, 提示里却叫用户执行它"
