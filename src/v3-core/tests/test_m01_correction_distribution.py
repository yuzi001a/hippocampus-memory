# -*- coding: utf-8 -*-
"""M01/M02 — additive schema distribution wiring (RED→GREEN anchors).

The canonical relation DDL artifact itself is owned by the M01 child
(``schema/memory_relations.sql`` + both packaged copies). This file pins
the *distribution* contract around it:

  1. both schema copies (``src/v3-core/schema/*.sql`` and
     ``src/v3-core/src/v3core/schema/*.sql``) stay byte-identical, and
     the new relation artifact is packaged (``pyproject`` package-data)
     and reachable through the canonical packaged-resource loader.
  2. fresh bootstrap splices the relation artifact through the single
     ALPHA_BOOTSTRAP_INCLUDE mechanism, after explicit_memories.
  3. the existing-install upgrade carries the same relation DDL additively
     and idempotently, with a stable migration version row.
  4. a MISSING packaged relation artifact fails (doctor reports it, the
     include expansion refuses, bootstrap/upgrade refuse) instead of
     silently succeeding.
  5. the doctor's packaged-SQL inventory and live required-table probe
     both know about the relation table.

These are all static / offline checks: the conftest-level
``psycopg2.connect`` ban is untouched.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from v3core import distribution_cli as dc

_HERE = Path(__file__).resolve().parent
_V3CORE = _HERE.parent
_ROOT_SCHEMA = _V3CORE / "schema"
_PKG_SCHEMA = _V3CORE / "src" / "v3core" / "schema"
_PYPROJECT = _V3CORE / "pyproject.toml"

RELATION_ARTIFACT = "memory_relations.sql"
RELATION_TABLE = "memory_relations"
RELATION_VERSION = "v0.3"

_MARKER_RE = re.compile(
    r"^--\s*>>>?\s*ALPHA_BOOTSTRAP_INCLUDE:\s*(schema/[A-Za-z0-9_.-]+\.sql)\s*<<<\s*$",
    re.MULTILINE,
)
# statements that would make the wiring non-additive
_DESTRUCTIVE = (
    r"\bDROP\s+TABLE\b",
    r"\bDROP\s+SCHEMA\b",
    r"\bTRUNCATE\b",
    r"\bDELETE\s+FROM\b",
    r"\bUPDATE\s+(public\.)?explicit_memories\b",
)


def _ddl_body(text: str) -> str:
    """Non-comment, non-string SQL — same shape the existing suites use."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))


def _schema_variants(name: str) -> dict[str, str]:
    return {
        "root": (_ROOT_SCHEMA / name).read_text(encoding="utf-8"),
        "packaged": (_PKG_SCHEMA / name).read_text(encoding="utf-8"),
    }


def _markers(text: str) -> list[str]:
    return _MARKER_RE.findall(text)


# ── 1. artifact presence, mirror equality, packaging ────────────────────────


def test_relation_artifact_exists_in_both_schema_copies():
    for base in (_ROOT_SCHEMA, _PKG_SCHEMA):
        path = base / RELATION_ARTIFACT
        assert path.is_file(), f"missing relation artifact: {path}"


def test_both_schema_copies_are_byte_identical():
    for name in ("alpha_bootstrap.sql", "upgrade_v0_2.sql", RELATION_ARTIFACT):
        root = (_ROOT_SCHEMA / name).read_bytes()
        packaged = (_PKG_SCHEMA / name).read_bytes()
        assert root == packaged, (
            f"{name} 根副本与打包副本必须逐字节一致 "
            f"(root={len(root)}B packaged={len(packaged)}B)"
        )


def test_relation_artifact_is_declared_in_package_data():
    text = _PYPROJECT.read_text(encoding="utf-8")
    assert f"schema/{RELATION_ARTIFACT}" in text, (
        "pyproject package-data 未包含 schema/memory_relations.sql — "
        "已安装 wheel 的 fresh bootstrap 将永远缺这张表"
    )


def test_packaged_loader_reaches_the_relation_artifact():
    text = dc._package_sql(RELATION_ARTIFACT)
    assert "CREATE TABLE IF NOT EXISTS public.memory_relations" in text
    sha = dc._package_sql_sha256(RELATION_ARTIFACT)
    assert re.fullmatch(r"[0-9a-f]{64}", sha)
    assert sha == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_doctor_reports_the_relation_artifact_and_table_as_required():
    body = (_V3CORE / "src" / "v3core" / "distribution_cli.py").read_text(
        encoding="utf-8"
    )
    # packaged-SQL inventory
    doctor_start = body.index('for name in ("alpha_bootstrap.sql"')
    doctor_end = body.index("# 3. Packaged v3hermes plugin.yaml", doctor_start)
    assert f'"{RELATION_ARTIFACT}"' in body[doctor_start:doctor_end], (
        "doctor 的 packaged SQL 检查未包含 memory_relations.sql"
    )
    # live required-table probe
    req_start = body.index('for table in ("explicit_memories"')
    req_end = body.index("information_schema.tables", req_start)
    assert f'"{RELATION_TABLE}"' in body[req_start:req_end], (
        "doctor 的 live required_tables 未包含 memory_relations"
    )


def test_doctor_static_report_includes_the_relation_artifact(capsys):
    import argparse

    rc = dc._doctor(
        argparse.Namespace(static=True, dsn=None)
    )
    out = capsys.readouterr().out
    assert rc in (0, 1, 2)
    import json as _json

    payload = _json.loads(out)
    sql = payload["checks"]["packaged_sql"]
    assert sql[RELATION_ARTIFACT]["present"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", sql[RELATION_ARTIFACT]["sha256"])


# ── 2. fresh bootstrap include wiring ───────────────────────────────────────


@pytest.mark.parametrize("variant", ["root", "packaged"])
def test_alpha_bootstrap_includes_the_relation_artifact_exactly_once(variant):
    text = _schema_variants("alpha_bootstrap.sql")[variant]
    markers = _markers(text)
    assert markers.count(f"schema/{RELATION_ARTIFACT}") == 1, markers
    # explicit_memories must come first: the relation table references it.
    assert markers.index("schema/explicit_memories.sql") < markers.index(
        f"schema/{RELATION_ARTIFACT}"
    ), markers


def test_fresh_bootstrap_expansion_creates_the_relation_table():
    expanded = dc._expand_alpha_include(dc._package_sql("alpha_bootstrap.sql"))
    assert not _MARKER_RE.search(expanded), "include marker survived expansion"
    assert "CREATE TABLE IF NOT EXISTS public.memory_relations" in expanded
    # The relation table must be created after explicit_memories.
    assert expanded.index("CREATE TABLE IF NOT EXISTS public.memory_relations") > (
        expanded.index("CREATE TABLE IF NOT EXISTS public.explicit_memories")
    )


def test_repo_bootstrap_script_expansion_also_reaches_the_relation_table():
    """The repo-root single-entry bootstrap script uses its own include
    resolver — it must pick the new artifact up too (it walks every
    marker)."""
    import importlib
    import sys

    scripts_dir = str(_V3CORE / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    if "bootstrap_alpha_db" in sys.modules:
        del sys.modules["bootstrap_alpha_db"]
    mod = importlib.import_module("bootstrap_alpha_db")
    repo_root = _V3CORE.parent.parent
    ddl, includes = mod.load_alpha_ddl(repo_root)
    assert any(p.endswith(RELATION_ARTIFACT) for p in includes), includes
    assert "CREATE TABLE IF NOT EXISTS public.memory_relations" in ddl


def test_bootstrap_expansion_fails_when_the_relation_artifact_is_missing(monkeypatch):
    """A missing packaged artifact must NOT silently produce a body without
    the relation table."""
    real = dc._package_sql

    def _without_relation(name: str) -> str:
        if name == RELATION_ARTIFACT:
            raise FileNotFoundError(
                "packaged SQL resource v3core.schema/memory_relations.sql not found"
            )
        return real(name)

    monkeypatch.setattr(dc, "_package_sql", _without_relation)
    with pytest.raises(FileNotFoundError) as ei:
        dc._expand_alpha_include(dc._package_sql("alpha_bootstrap.sql"))
    assert "memory_relations" in str(ei.value)


# ── 3. upgrade path ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("variant", ["root", "packaged"])
def test_upgrade_carries_the_relation_artifact_include(variant):
    text = _schema_variants("upgrade_v0_2.sql")[variant]
    markers = _markers(text)
    assert markers.count(f"schema/{RELATION_ARTIFACT}") == 1, markers
    assert markers.index("schema/explicit_memories.sql") < markers.index(
        f"schema/{RELATION_ARTIFACT}"
    ), markers


def test_upgrade_expansion_creates_the_relation_table_before_commit():
    expanded = dc._expand_alpha_include(dc._package_sql("upgrade_v0_2.sql"))
    assert "CREATE TABLE IF NOT EXISTS public.memory_relations" in expanded
    assert (
        expanded.index("CREATE TABLE IF NOT EXISTS public.memory_relations")
        < expanded.rindex("COMMIT;")
    )


def test_upgrade_is_additive_only():
    for variant, text in _schema_variants("upgrade_v0_2.sql").items():
        body = _ddl_body(text)
        for pat in _DESTRUCTIVE:
            assert not re.search(pat, body, re.IGNORECASE), (variant, pat)
    combined = dc._expand_alpha_include(dc._package_sql("upgrade_v0_2.sql"))
    scan = dc._scan_destructive_sql(combined)
    assert scan["clean"] is True, scan["violations"]


def test_upgrade_records_a_stable_migration_version_row():
    combined = dc._expand_alpha_include(dc._package_sql("upgrade_v0_2.sql"))
    expected = dc._parse_expected_objects(combined)
    assert RELATION_VERSION in expected["schema_versions_rows"], (
        f"升级必须写入稳定迁移版本行 {RELATION_VERSION}; 实际="
        f"{expected['schema_versions_rows']}"
    )
    # idempotent re-run
    assert "ON CONFLICT" in combined.upper()
    assert RELATION_TABLE in " ".join(expected["tables"]), expected["tables"]


def test_relation_artifact_itself_is_additive_and_idempotent():
    for variant, text in _schema_variants(RELATION_ARTIFACT).items():
        body = _ddl_body(text)
        for pat in _DESTRUCTIVE:
            assert not re.search(pat, body, re.IGNORECASE), (variant, pat)
        assert "CREATE TABLE IF NOT EXISTS public.memory_relations" in text
        # every index guarded with IF NOT EXISTS
        for line in body.splitlines():
            if re.search(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\b", line, re.I):
                assert "IF NOT EXISTS" in line.upper(), (variant, line)


def test_upgrade_required_tables_and_columns_know_the_relation_table():
    assert RELATION_TABLE in dc._UPGRADE_REQUIRED_TABLES, (
        "memory_relations 必须是升级的必需表 — 否则缺失时 dry-run/apply "
        "不会如实报告"
    )
    expected = dc._parse_expected_objects(
        dc._expand_alpha_include(dc._package_sql("upgrade_v0_2.sql"))
    )
    assert any(RELATION_TABLE in t for t in expected["tables"]), expected["tables"]


def test_upgrade_combined_body_loads_and_includes_the_relation_table():
    sql, meta = dc._upgrade_load_combined_sql(include_qa_chunks_artifact=True)
    assert meta.get("error") is None, meta
    assert "CREATE TABLE IF NOT EXISTS public.memory_relations" in sql
    assert sql.index("CREATE TABLE IF NOT EXISTS public.memory_relations") < sql.rindex(
        "COMMIT;"
    )
    sha = meta.get("memory_relations_sql_sha256")
    assert sha and re.fullmatch(r"[0-9a-f]{64}", sha)


def test_upgrade_apply_refuses_when_the_relation_artifact_is_missing(monkeypatch):
    """Missing artifact ⇒ hard refusal (exit 2), zero DDL, not a
    'successful' partial upgrade."""
    import argparse
    import io
    from contextlib import redirect_stdout

    real = dc._package_optional_sql
    real_pkg = dc._package_sql

    def _missing_optional(name: str):
        if name == RELATION_ARTIFACT:
            return (False, None, None)
        return real(name)

    def _missing_pkg(name: str) -> str:
        if name == RELATION_ARTIFACT:
            raise FileNotFoundError(
                "packaged SQL resource v3core.schema/memory_relations.sql not found"
            )
        return real_pkg(name)

    monkeypatch.setattr(dc, "_package_optional_sql", _missing_optional)
    monkeypatch.setattr(dc, "_package_sql", _missing_pkg)
    buf = io.StringIO()
    args = argparse.Namespace(
        target="postgres://u:p@localhost:55432/v3alpha",
        dsn=None,
        apply=True,
        dry_run=False,
        allow_production_read=False,
        allow_production_write=False,
        confirm_plan_sha=None,
        plan_out=None,
        show_sql=False,
    )
    with redirect_stdout(buf):
        rc = dc._upgrade_apply(args)
    assert rc == 2, buf.getvalue()
    payload = buf.getvalue()
    assert "memory_relations" in payload


# ── 4. missing-artifact contract for bootstrap ──────────────────────────────


def test_bootstrap_refuses_when_the_relation_artifact_is_missing(monkeypatch):
    real = dc._package_sql

    def _missing(name: str) -> str:
        if name == RELATION_ARTIFACT:
            raise FileNotFoundError(
                "packaged SQL resource v3core.schema/memory_relations.sql not found"
            )
        return real(name)

    monkeypatch.setattr(dc, "_package_sql", _missing)
    result = dc._bootstrap_apply_sql(
        {"host": "localhost", "port": 55432, "database": "v3alpha",
         "user": "u", "password": "x"}
    )
    assert result["applied"] is False
    assert "memory_relations" in result["error"]


# ── 5. FK / dependency order still holds with the new artifact ─────────────


def test_relation_table_fk_targets_exist_and_come_first():
    from test_bootstrap_dependency_order import (  # type: ignore
        _create_order,
        _fk_edges,
    )

    expanded = dc._expand_alpha_include(dc._package_sql("alpha_bootstrap.sql"))
    order = _create_order(expanded)
    assert RELATION_TABLE in order
    pos = {name: i for i, name in enumerate(order)}
    for child, parent in _fk_edges(expanded):
        if child == parent:
            continue
        assert parent in pos, (child, parent)
        assert pos[parent] <= pos[child], (child, parent)
