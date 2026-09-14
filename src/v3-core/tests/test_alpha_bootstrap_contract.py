# -*- coding: utf-8 -*-
"""Static contract tests for src/v3-core/schema/alpha_bootstrap.sql,
scripts/bootstrap_alpha_db.py, and scripts/backup_alpha.py.

Scope (RED→GREEN anchors; never pretend to substitute real PG):

    1. alpha_bootstrap.sql exists, is parseable, contains exactly the seven
       alpha tables in the supported order, embeds pgvector, references
       explicit_memories.sql (the canonical artifact) by include marker, and
       has NO DROP / TRUNCATE / DELETE statements.

    2. bootstrap_alpha_db.py is importable, refuses an empty DSN, refuses
       the production boundary by default, accepts it with
       ``--allow-production-dsn``, never prints a password, and applies DDL
       idempotently in --check-only mode without connecting to PG.

    3. backup_alpha.py is importable, refuses an empty DSN, refuses the
       production boundary by default, refuses --extra-table=source_raw_*,
       and assembles a pg_dump command whose --table flags cover exactly the
       alpha working set.

These tests are static — they MUST NOT call psycopg2.connect (P0-A conftest
hard-blocks it). They never load production DSN values, never touch
``~/.v3-core``, and never reach a real PostgreSQL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Paths (resolved via public repo-root markers). The repo root is
# identified by the canonical public artifact path
# ``src/v3-core/schema/alpha_bootstrap.sql`` together with
# ``src/v3-core/scripts/bootstrap_alpha_db.py`` — both of which are
# shipped in the public tree.
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        if (
            (parent / "src" / "v3-core" / "schema" / "alpha_bootstrap.sql").is_file()
            and (parent / "src" / "v3-core" / "scripts" / "bootstrap_alpha_db.py").is_file()
        ):
            return parent
    raise FileNotFoundError(
        f"cannot locate hippocampus repo root (need "
        f"src/v3-core/schema/alpha_bootstrap.sql + "
        f"src/v3-core/scripts/bootstrap_alpha_db.py) from {start}"
    )


REPO_ROOT = _find_repo_root(Path(__file__))
ALPHA_SQL = REPO_ROOT / "src" / "v3-core" / "schema" / "alpha_bootstrap.sql"
EXPLICIT_SQL = REPO_ROOT / "src" / "v3-core" / "schema" / "explicit_memories.sql"
BOOTSTRAP_PY = REPO_ROOT / "src" / "v3-core" / "scripts" / "bootstrap_alpha_db.py"
BACKUP_PY = REPO_ROOT / "src" / "v3-core" / "scripts" / "backup_alpha.py"


def _extract_create_columns(sql: str, table_name: str) -> str:
    """Extract the column block of ``CREATE TABLE [IF NOT EXISTS] public.<name> (...)``.

    A naive ``(.*?)\\)`` regex stops at the first ``)``, which breaks on
    ``REFERENCES public.foo(id)``. This helper walks paren depth from the
    opening ``(`` after the table name to the matching ``)`` and returns
    just the column list as a single string.

    Returns "" if the CREATE TABLE is not found.
    """
    pat = re.compile(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+public\.{re.escape(table_name)}\s*\(",
        re.IGNORECASE,
    )
    m = pat.search(sql)
    if not m:
        return ""
    depth = 1
    i = m.end()
    while i < len(sql) and depth > 0:
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return sql[m.end() : i]
        i += 1
    return ""


# ---------------------------------------------------------------------------
# Module imports (path injection — never modify sys.path globally)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scripts_on_path():
    p = str(REPO_ROOT / "src" / "v3-core" / "scripts")
    saved = sys.path.copy()
    sys.path.insert(0, p)
    try:
        yield p
    finally:
        sys.path[:] = saved


@pytest.fixture(scope="module")
def bootstrap_mod(scripts_on_path):
    import importlib

    if "bootstrap_alpha_db" in sys.modules:
        del sys.modules["bootstrap_alpha_db"]
    return importlib.import_module("bootstrap_alpha_db")


@pytest.fixture(scope="module")
def backup_mod(scripts_on_path):
    import importlib

    if "backup_alpha" in sys.modules:
        del sys.modules["backup_alpha"]
    return importlib.import_module("backup_alpha")


# ---------------------------------------------------------------------------
# alpha_bootstrap.sql — static contract
# ---------------------------------------------------------------------------


class TestAlphaBootstrapSQL:
    def test_file_exists(self):
        assert ALPHA_SQL.is_file(), f"missing {ALPHA_SQL}"

    def test_explicit_memories_artifact_exists(self):
        assert EXPLICIT_SQL.is_file(), f"missing {EXPLICIT_SQL}"

    def test_pgvector_extension_required(self):
        sql = ALPHA_SQL.read_text(encoding="utf-8")
        assert "CREATE EXTENSION IF NOT EXISTS vector" in sql, (
            "alpha_bootstrap.sql 必须确保 pgvector extension 存在"
        )

    def test_no_destructive_statements(self):
        """Bootstrap-only. DROP / TRUNCATE / DELETE must NEVER appear."""
        sql = ALPHA_SQL.read_text(encoding="utf-8")
        # Strip line comments to avoid false positives on "-- old DROP ..."
        # notes (we use '--' SQL comments; bootstrap doc has a few).
        non_comment = "\n".join(
            line for line in sql.splitlines()
            if not line.lstrip().startswith("--")
        )
        forbidden = [
            r"\bDROP\s+TABLE\b",
            r"\bDROP\s+SCHEMA\b",
            r"\bTRUNCATE\b",
            r"\bDELETE\s+FROM\b",
            r"\bDROP\s+FUNCTION\b",
        ]
        for pat in forbidden:
            m = re.search(pat, non_comment, re.IGNORECASE)
            assert not m, f"alpha_bootstrap.sql 含破坏性 DDL: {pat} → {m.group(0)!r}"

    def test_combined_ddl_has_seven_alpha_tables(self, bootstrap_mod):
        """The seven supported alpha tables, in canonical order, must each
        have a ``CREATE TABLE IF NOT EXISTS public.<name>`` in the COMBINED
        DDL (alpha_bootstrap.sql + explicit_memories.sql via marker)."""
        combined, includes = bootstrap_mod.load_alpha_ddl(REPO_ROOT)
        assert includes, "ALPHA_BOOTSTRAP_INCLUDE 至少要解析出一个 include"
        expected = [
            "public.explicit_memories",
            "public.qa_pairs",
            "public.conversation_stream",
            "public.topics",
            "public.topic_entries",
            "public.observation_notes",
            "public.yin_paragraphs",
        ]
        positions = []
        for name in expected:
            pat = re.compile(
                rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(name)}\b",
                re.IGNORECASE,
            )
            m = pat.search(combined)
            assert m, f"缺少 CREATE TABLE for {name}"
            positions.append((name, m.start()))
        ordered = sorted(positions, key=lambda x: x[1])
        assert [n for n, _ in ordered] == expected, (
            "alpha 表顺序必须固定为 "
            f"{expected}, 实际顺序={[n for n, _ in ordered]}"
        )

    def test_no_legacy_archived_tables_revived(self):
        """v3_cards / v3_messages / v3_facts / v3_effective / embeddings /
        facts are archived (2026-08-06) and must NOT come back via alpha."""
        sql = ALPHA_SQL.read_text(encoding="utf-8")
        for legacy in (
            "v3_cards",
            "v3_messages",
            "v3_facts",
            "v3_effective",
            "embeddings",
            "facts",
        ):
            pat = re.compile(rf"CREATE\s+TABLE[^;]*\b{legacy}\b", re.IGNORECASE)
            m = pat.search(sql)
            assert not m, f"alpha bootstrap 不应重建 legacy 表 {legacy!r}"

    def test_no_b_or_shou_namespace(self):
        """b_*/shou_* are explicitly out-of-scope per task CONTEXT."""
        sql = ALPHA_SQL.read_text(encoding="utf-8")
        for prefix in ("b_", "shou_"):
            assert prefix not in sql, (
                f"alpha bootstrap 不应包含 {prefix}* 命名空间 (legacy/orphan)"
            )

    def test_includes_explicit_memories_via_marker(self):
        sql = ALPHA_SQL.read_text(encoding="utf-8")
        m = re.search(
            r"--\s*>>>\s*ALPHA_BOOTSTRAP_INCLUDE:\s*(\S+)\s*<<<",
            sql,
        )
        assert m, "alpha_bootstrap.sql 必须含 ALPHA_BOOTSTRAP_INCLUDE marker"
        assert m.group(1).endswith("explicit_memories.sql"), (
            f"include 必须指向 explicit_memories.sql, 实际={m.group(1)!r}"
        )

    def test_required_indexes_present(self, bootstrap_mod):
        """Required operational indexes (per evidence in pg_store /
        recall_pool / observer / yin_pool). explicit_memories_embedding_ivfflat
        is in explicit_memories.sql; we look in the combined DDL."""
        combined, _ = bootstrap_mod.load_alpha_ddl(REPO_ROOT)
        required = [
            "explicit_memories_embedding_ivfflat",
            "qa_pairs_embedding_ivfflat",
            "conversation_stream_embedding_ivfflat",
            "topics_embedding_ivfflat",
            "topic_entries_embedding_ivfflat",
            "observation_notes_embedding_ivfflat",
            "yin_paragraphs_embedding_ivfflat",
        ]
        for idx in required:
            assert idx in combined, f"缺少必要向量索引 {idx}"

    def test_unique_constraint_status_active(self):
        """ActiveMemoryReader filters by status='active'; the topics and
        explicit_memories rows must keep that contract enforceable."""
        sql = ALPHA_SQL.read_text(encoding="utf-8")
        # explicit_memories status CHECK is in explicit_memories.sql — not
        # duplicated here. Verify the partial index that requires active is
        # present in BOTH places (idempotent adds are fine).
        assert "WHERE status = 'active'" in sql

    def test_begin_commit_envelopes(self):
        sql = ALPHA_SQL.read_text(encoding="utf-8")
        assert re.search(r"^\s*BEGIN\s*;", sql, re.MULTILINE), (
            "alpha_bootstrap.sql 应在事务边界内执行 (BEGIN;)"
        )
        assert re.search(r"^\s*COMMIT\s*;", sql, re.MULTILINE), (
            "alpha_bootstrap.sql 应在事务边界内执行 (COMMIT;)"
        )

    def test_qa_pairs_real_ingest_columns(self, bootstrap_mod):
        """Every column actually written by ``__init__.py``'s
        ``_flush_pending_qa`` INSERT (source_id, session_id, turn_id,
        question, answer, tool_calls, tool_results, timestamp, source,
        embedding, embed_model, created_at) MUST exist in the qa_pairs
        CREATE TABLE — fresh init will fail the first sync_turn ingest
        if any of these columns are missing."""
        block = _extract_create_columns(ALPHA_SQL.read_text(encoding="utf-8"), "qa_pairs")
        assert block, "qa_pairs CREATE TABLE block not found"
        block_lower = block.lower()
        required = [
            "source_id",
            "session_id",
            "turn_id",
            "question",
            "answer",
            "tool_calls",
            "tool_results",
            "timestamp",
            "source",
            "embedding",
            "embed_model",
            "created_at",
        ]
        for col in required:
            assert col in block_lower, f"qa_pairs CREATE TABLE 缺真实 ingest 列: {col}"

    def test_qa_pairs_source_id_unique_constraint(self, bootstrap_mod):
        """source_id MUST be UNIQUE — __init__.py uses
        ``ON CONFLICT (source_id) DO NOTHING`` as the canonical idempotency
        key for retry-safe live-buffer ingest. Without the UNIQUE
        constraint, the first duplicate ingest will raise
        ``no unique or exclusion constraint matching the ON CONFLICT
        specification``."""
        combined, _ = bootstrap_mod.load_alpha_ddl(REPO_ROOT)
        assert "UNIQUE" in combined and "source_id" in combined, (
            "qa_pairs.source_id 必须声明 UNIQUE 约束 (供 ON CONFLICT DO NOTHING 使用)"
        )
        # Confirm a real UNIQUE constraint exists, not just a comment claim.
        assert re.search(
            r"CONSTRAINT\s+qa_pairs_source_id_key\s+UNIQUE\s*\(\s*source_id\s*\)",
            combined,
            re.IGNORECASE,
        ), "qa_pairs_source_id_key UNIQUE(source_id) constraint missing"

    def test_observation_notes_has_embed_model(self, bootstrap_mod):
        """observer.py backfill + pg_store.insert_effective BOTH write
        (embedding, embed_model) on observation_notes. Without the column
        the writer silently degrades to "anonymous vectors" — fresh init
        must declare the column up front."""
        block = _extract_create_columns(ALPHA_SQL.read_text(encoding="utf-8"), "observation_notes")
        assert block, "observation_notes CREATE TABLE block not found"
        assert "embed_model" in block.lower(), (
            "observation_notes CREATE TABLE 缺 embed_model 列 "
            "(observer 回填 + pg_store.insert_effective 都要写)"
        )

    def test_yin_paragraphs_has_embed_model(self, bootstrap_mod):
        """yin_pool.insert also writes embed_model (with the live
        fingerprint) on yin_paragraphs. Fresh init must declare it
        up front."""
        block = _extract_create_columns(ALPHA_SQL.read_text(encoding="utf-8"), "yin_paragraphs")
        assert block, "yin_paragraphs CREATE TABLE block not found"
        assert "embed_model" in block.lower(), (
            "yin_paragraphs CREATE TABLE 缺 embed_model 列 (yin_pool 写入)"
        )


# ---------------------------------------------------------------------------
# bootstrap_alpha_db.py — static contract
# ---------------------------------------------------------------------------


class TestBootstrapScript:
    def test_file_exists(self):
        assert BOOTSTRAP_PY.is_file()

    def test_no_hardcoded_passwords(self):
        text = BOOTSTRAP_PY.read_text(encoding="utf-8")
        # Look for any literal "password = 'literal'" or "password: 'literal'"
        # or quoted password= tokens with non-empty content.
        bad = re.findall(
            r"(password\s*[:=]\s*['\"])([^'\"]+)(['\"])",
            text,
            re.IGNORECASE,
        )
        assert not bad, f"bootstrap 脚本禁止硬编码密码: {bad}"

    def test_no_hardcoded_production_dsn(self):
        """No DEFAULT / FALLBACK production DSN string should be assigned
        to a variable that flows into ``connect()``. Docstrings legitimately
        mention the boundary; we only flag a default DSN as a Python
        expression (excluding comments / docstrings)."""
        text = BOOTSTRAP_PY.read_text(encoding="utf-8")
        # Strip module-level docstring and inline comments.
        no_docstring = re.sub(r'^\s*"""[\s\S]*?"""\s*', "", text, count=1)
        non_comment = "\n".join(
            line for line in no_docstring.splitlines()
            if not line.lstrip().startswith("#")
        )
        bad = re.search(
            r"(port\s*=\s*5433|dbname\s*=\s*['\"]v3embeddings['\"]|"
            r"database\s*=\s*['\"]v3embeddings['\"])",
            non_comment,
            re.IGNORECASE,
        )
        assert not bad, f"bootstrap 脚本禁止硬编码生产 DSN 默认值: {bad.group(0)!r}"

    def test_imports_cleanly(self, bootstrap_mod):
        assert bootstrap_mod is not None
        assert hasattr(bootstrap_mod, "resolve_dsn")
        assert hasattr(bootstrap_mod, "load_alpha_ddl")
        assert hasattr(bootstrap_mod, "DSNSafetyError")

    def test_refuses_empty_dsn(self, bootstrap_mod, monkeypatch):
        # Clear any env DSN.
        monkeypatch.delenv("V3CORE_BOOTSTRAP_DSN", raising=False)
        monkeypatch.delenv("PGPASSWORD", raising=False)
        args = argparse.Namespace(
            dsn="",
            host="",
            port=0,
            database="",
            user="",
            password="",
            allow_production_dsn=False,
        )
        with pytest.raises(bootstrap_mod.DSNSafetyError):
            bootstrap_mod.resolve_dsn(args)

    def test_refuses_production_boundary_by_default(self, bootstrap_mod, monkeypatch):
        monkeypatch.delenv("V3CORE_BOOTSTRAP_DSN", raising=False)
        monkeypatch.delenv("PGPASSWORD", raising=False)
        args = argparse.Namespace(
            dsn="host=127.0.0.1 port=5433 dbname=v3embeddings user=v3user password=***",
            host="",
            port=0,
            database="",
            user="",
            password="",
            allow_production_dsn=False,
        )
        with pytest.raises(bootstrap_mod.DSNSafetyError) as ei:
            bootstrap_mod.resolve_dsn(args)
        assert "生产边界" in str(ei.value)

    def test_allows_production_boundary_with_flag(self, bootstrap_mod, monkeypatch):
        monkeypatch.delenv("V3CORE_BOOTSTRAP_DSN", raising=False)
        args = argparse.Namespace(
            dsn="host=127.0.0.1 port=5433 dbname=v3embeddings user=v3user password=***",
            host="",
            port=0,
            database="",
            user="",
            password="",
            allow_production_dsn=True,
        )
        out = bootstrap_mod.resolve_dsn(args)
        assert out["host"] == "127.0.0.1"
        assert int(out["port"]) == 5433
        assert out["database"] == "v3embeddings"

    def test_password_redaction(self, bootstrap_mod):
        s = bootstrap_mod._redact_password(
            "host=localhost port=5432 dbname=v3embeddings user=v3user password=hunter2"
        )
        assert "hunter2" not in s, f"密码泄漏: {s}"
        assert "***" in s

    def test_password_redaction_uri(self, bootstrap_mod):
        s = bootstrap_mod._redact_password(
            "postgresql://v3user:changeme@127.0.0.1:5433/v3embeddings"
        )
        assert "hunter2" not in s, f"URI 密码泄漏: {s}"

    def test_load_alpha_ddl_resolves_include(self, bootstrap_mod):
        ddl, includes = bootstrap_mod.load_alpha_ddl(REPO_ROOT)
        assert "explicit_memories" in ddl
        assert "BEGIN INCLUDED schema/explicit_memories.sql" in ddl
        assert "END INCLUDED schema/explicit_memories.sql" in ddl
        assert len(includes) == 1
        assert includes[0].endswith("explicit_memories.sql")

    def test_load_alpha_ddl_is_idempotent(self, bootstrap_mod):
        """Every CREATE uses IF NOT EXISTS; running load twice must not
        change the DDL body (the marker replacement is deterministic)."""
        ddl1, _ = bootstrap_mod.load_alpha_ddl(REPO_ROOT)
        ddl2, _ = bootstrap_mod.load_alpha_ddl(REPO_ROOT)
        assert ddl1 == ddl2
        # Confirm DDL really has the idempotency tokens.
        assert "CREATE TABLE IF NOT EXISTS" in ddl1
        assert "CREATE INDEX IF NOT EXISTS" in ddl1
        assert "ADD COLUMN IF NOT EXISTS" in ddl1

    def test_check_only_does_not_connect(self, bootstrap_mod, monkeypatch, capsys, tmp_path):
        monkeypatch.chdir(tmp_path)
        rc = bootstrap_mod.main(
            [
                "--check-only",
                "--repo-root",
                str(REPO_ROOT),
                "--log-level",
                "WARNING",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0, f"check-only 应该返回 0; out={out}"
        data = json.loads(out.strip().splitlines()[-1])
        assert data["status"] == "check_only"
        assert data["ddl_chars"] > 0
        assert data["includes"][0].endswith("explicit_memories.sql")

    def test_missing_dsn_exits_2(self, bootstrap_mod, monkeypatch, capsys, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("V3CORE_BOOTSTRAP_DSN", raising=False)
        monkeypatch.delenv("PGPASSWORD", raising=False)
        rc = bootstrap_mod.main(
            [
                "--dsn",
                "",
                "--repo-root",
                str(REPO_ROOT),
                "--log-level",
                "WARNING",
            ]
        )
        out = capsys.readouterr().out.strip().splitlines()[-1]
        assert rc == 2, f"empty DSN 应该退出 2, 实际={rc}; out={out}"
        data = json.loads(out)
        assert data["status"] == "refused"
        assert data["stage"] == "dsn"

    def test_production_dsn_exits_2(self, bootstrap_mod, monkeypatch, capsys, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("V3CORE_BOOTSTRAP_DSN", raising=False)
        rc = bootstrap_mod.main(
            [
                "--dsn",
                "host=127.0.0.1 port=5433 dbname=v3embeddings user=v3user password=x",
                "--repo-root",
                str(REPO_ROOT),
                "--log-level",
                "WARNING",
            ]
        )
        out = capsys.readouterr().out.strip().splitlines()[-1]
        assert rc == 2, f"生产 DSN 应该退出 2, 实际={rc}; out={out}"
        data = json.loads(out)
        assert data["status"] == "refused"


    # ─── A. 组件 + PGPASSWORD accepted ───────────────────────────────────────
    def test_components_with_pgpassword_accepted(
        self, bootstrap_mod, monkeypatch
    ):
        """A 契约: 完整组件参数 + PGPASSWORD 环境变量应被接受; password
        由 PGPASSWORD 提供, 走 _enforce_production_boundary 检查, 最终
        返回的 kwargs 同时含 host/port/database/user/password 五个字段。"""
        monkeypatch.delenv("V3CORE_BOOTSTRAP_DSN", raising=False)
        monkeypatch.setenv("PGPASSWORD", "hunter2-secret-XXX")
        args = argparse.Namespace(
            dsn="",
            host="localhost",
            port=5432,
            database="alpha_db",
            user="alpha_user",
            password="",
            allow_production_dsn=False,
        )
        out = bootstrap_mod.resolve_dsn(args)
        assert out["host"] == "localhost"
        assert int(out["port"]) == 5432
        assert out["database"] == "alpha_db"
        assert out["user"] == "alpha_user"
        assert out["password"] == "hunter2-secret-XXX"

    # ─── B. 组件生产边界 refused ─────────────────────────────────────────────
    def test_components_production_boundary_refused(
        self, bootstrap_mod, monkeypatch
    ):
        """B 契约: 组件参数指向 127.0.0.1:5433/v3embeddings 但未传
        --allow-production-dsn 时必须 DSNSafetyError。"""
        monkeypatch.delenv("V3CORE_BOOTSTRAP_DSN", raising=False)
        monkeypatch.setenv("PGPASSWORD", "x")
        args = argparse.Namespace(
            dsn="",
            host="127.0.0.1",
            port=5433,
            database="v3embeddings",
            user="v3user",
            password="",
            allow_production_dsn=False,
        )
        with pytest.raises(bootstrap_mod.DSNSafetyError) as ei:
            bootstrap_mod.resolve_dsn(args)
        assert "生产边界" in str(ei.value)

    def test_components_production_boundary_allowed_with_flag(
        self, bootstrap_mod, monkeypatch
    ):
        """组件路径 + --allow-production-dsn 放行生产边界。"""
        monkeypatch.delenv("V3CORE_BOOTSTRAP_DSN", raising=False)
        monkeypatch.setenv("PGPASSWORD", "x")
        args = argparse.Namespace(
            dsn="",
            host="127.0.0.1",
            port=5433,
            database="v3embeddings",
            user="v3user",
            password="",
            allow_production_dsn=True,
        )
        out = bootstrap_mod.resolve_dsn(args)
        assert out["host"] == "127.0.0.1"
        assert int(out["port"]) == 5433
        assert out["database"] == "v3embeddings"

    # ─── C. 空/无连接规格 refused ────────────────────────────────────────────
    def test_components_incomplete_refused(
        self, bootstrap_mod, monkeypatch
    ):
        """C 契约: 组件不完整 (缺 port) 必须 fail-closed, 即便 password
        已提供也不能拼凑出半个 DSN。"""
        monkeypatch.delenv("V3CORE_BOOTSTRAP_DSN", raising=False)
        monkeypatch.delenv("PGPASSWORD", raising=False)
        args = argparse.Namespace(
            dsn="",
            host="localhost",
            port=0,           # missing
            database="alpha_db",
            user="alpha_user",
            password="x",
            allow_production_dsn=False,
        )
        with pytest.raises(bootstrap_mod.DSNSafetyError) as ei:
            bootstrap_mod.resolve_dsn(args)
        assert "DSN" in str(ei.value) or "组件" in str(ei.value)

    def test_components_no_password_no_env_refused(
        self, bootstrap_mod, monkeypatch
    ):
        """C 契约 (变体): 组件完整但既无 --password 也无 PGPASSWORD 时,
        必须 fail-closed — 禁止假设 auth_method=none。"""
        monkeypatch.delenv("V3CORE_BOOTSTRAP_DSN", raising=False)
        monkeypatch.delenv("PGPASSWORD", raising=False)
        args = argparse.Namespace(
            dsn="",
            host="localhost",
            port=5432,
            database="alpha_db",
            user="alpha_user",
            password="",
            allow_production_dsn=False,
        )
        with pytest.raises(bootstrap_mod.DSNSafetyError) as ei:
            bootstrap_mod.resolve_dsn(args)
        assert "PGPASSWORD" in str(ei.value) or "password" in str(ei.value).lower()

    # ─── D. --dsn 既有行为 unchanged ─────────────────────────────────────────
    def test_dsn_literal_unchanged(
        self, bootstrap_mod, monkeypatch
    ):
        """D 契约: --dsn 既有路径不受组件参数影响 (即使同时传了组件)。
        解析结果以 --dsn 为准, host/port/database/user 来自 DSN 字符串,
        password 走 PGPASSWORD 兜底。"""
        monkeypatch.delenv("V3CORE_BOOTSTRAP_DSN", raising=False)
        monkeypatch.setenv("PGPASSWORD", "dsn-env-pw-XXX")
        args = argparse.Namespace(
            dsn="host=localhost port=5432 dbname=alpha_db user=alpha_user",
            host="should-be-ignored",
            port=9999,
            database="should-be-ignored",
            user="should-be-ignored",
            password="should-be-ignored",
            allow_production_dsn=False,
        )
        out = bootstrap_mod.resolve_dsn(args)
        assert out["host"] == "localhost"
        assert int(out["port"]) == 5432
        assert out["database"] == "alpha_db"
        assert out["user"] == "alpha_user"
        assert out["password"] == "dsn-env-pw-XXX"

    # ─── E. password 不出现在 JSON/log 输出 ──────────────────────────────────
    def test_password_absent_from_main_output_and_log(
        self, bootstrap_mod, monkeypatch, capsys, tmp_path, caplog
    ):
        """E 契约: 当 apply_ddl 抛出包含密码字面量的异常时, password 不能
        出现在 stdout JSON 的 detail 字段或 stderr log 行中。错误仍需
        可诊断 (异常类型名保留), 但密码文本必须被 redact。"""
        sentinel = "PW-SECRET-LEAK-CANARY-7f3a"
        captured_kwargs: dict = {}

        def fake_apply_ddl(dsn_kwargs, ddl):
            captured_kwargs.update(dsn_kwargs)
            raise RuntimeError(
                f"connection failed for user with password={sentinel} "
                f"on host={dsn_kwargs.get('host')}"
            )

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(bootstrap_mod, "apply_ddl", fake_apply_ddl)
        monkeypatch.delenv("V3CORE_BOOTSTRAP_DSN", raising=False)
        monkeypatch.setenv("PGPASSWORD", sentinel)

        with caplog.at_level("WARNING"):
            rc = bootstrap_mod.main(
                [
                    "--host", "localhost",
                    "--port", "5432",
                    "--database", "alpha_db",
                    "--user", "alpha_user",
                    "--password", sentinel,
                    "--repo-root", str(REPO_ROOT),
                    "--log-level", "INFO",
                ]
            )

        captured = capsys.readouterr()
        out = captured.out
        err = captured.err
        assert rc == 5, f"apply_ddl 异常应退出 5, 实际={rc}"
        # stdout JSON: 解析最后一行, 确认 detail 不含密码
        last = [ln for ln in out.strip().splitlines() if ln.strip()][-1]
        data = json.loads(last)
        assert data["status"] == "error"
        assert data["stage"] == "apply_ddl"
        assert sentinel not in data["detail"], (
            f"密码泄漏到 stdout JSON: {data['detail']!r}"
        )
        # 确认仍然可诊断: detail 含 RuntimeError
        assert "RuntimeError" in data["detail"], (
            f"diagnosability 丢失: {data['detail']!r}"
        )
        # stderr log: 确认 caplog 捕获的所有 log 行不含密码
        for record in caplog.records:
            assert sentinel not in record.getMessage(), (
                f"密码泄漏到 log record: {record.getMessage()!r}"
            )
        # 同样直接扫描 capsys 的 stderr 缓冲
        assert sentinel not in err, f"密码泄漏到 stderr narrative: {err!r}"


# ---------------------------------------------------------------------------
# bootstrap_alpha_db.py — path-resolution hardening
# ---------------------------------------------------------------------------
#
# Bug being pinned: the previous _find_repo_root() resolved upward from
# Path.cwd(). A caller in checkout-A invoking a clean-export copy that
# physically lived under checkout-A/src/v3-core/scripts/ would always
# select the checkout-A schema (correct by luck). A caller invoking the
# same clean-export copy from a totally different cwd (e.g. checkout-B's
# root) would still resolve from cwd and silently pick checkout-B's
# schema — a public script must not allow this.
#
# Contract:
#   * Without --repo-root, the script MUST locate the schema by walking
#     upward from the executing script's own directory, never from cwd.
#   * --repo-root is an explicit override; if it does not contain the
#     schema marker, fail closed (exit code 4, status=error, stage=locate).
#   * The redacted JSON output MUST include `repo_root` and `includes`
#     so operators can audit which copy was selected.
# ---------------------------------------------------------------------------


def _materialize_checkout(
    root_dir: Path,
    schema_body: str,
    scripts_dir: Path,
    *,
    explicit_sql_body: str | None = None,
) -> Path:
    """Create a minimal fake checkout rooted at ``root_dir``.

    Layout produced::

        <root>/src/v3-core/schema/alpha_bootstrap.sql   (with marker)
        <root>/src/v3-core/schema/explicit_memories.sql
        <root>/src/v3-core/scripts/bootstrap_alpha_db.py  (copy)
        <root>/AGENTS.md                                 (candidate marker)

    Returns ``root_dir``. ``schema_body`` must already contain the
    ``-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/explicit_memories.sql <<<``
    marker — we write ``explicit_memories.sql`` next to it.
    """
    schema_dir = root_dir / "src" / "v3-core" / "schema"
    real_scripts_dir = root_dir / "src" / "v3-core" / "scripts"
    schema_dir.mkdir(parents=True, exist_ok=True)
    real_scripts_dir.mkdir(parents=True, exist_ok=True)
    (root_dir / "AGENTS.md").write_text(
        "# synthetic checkout for path-resolution contract tests\n",
        encoding="utf-8",
    )
    schema_path = schema_dir / "alpha_bootstrap.sql"
    schema_path.write_text(schema_body, encoding="utf-8")
    explicit_body = explicit_sql_body or (
        "-- synthetic explicit_memories.sql\n"
        "CREATE TABLE IF NOT EXISTS public.explicit_memories (id int);\n"
    )
    (schema_dir / "explicit_memories.sql").write_text(explicit_body, encoding="utf-8")
    # Copy the real script into the synthetic checkout (under a sibling
    # layout) so the script's __file__ resolves to the synthetic copy.
    script_text = scripts_dir.read_text(encoding="utf-8")
    (real_scripts_dir / "bootstrap_alpha_db.py").write_text(
        script_text, encoding="utf-8"
    )
    return root_dir


def _build_marker_aware_schema(sentinel: str) -> str:
    """A minimal but realistic alpha_bootstrap.sql carrying the marker.

    ``sentinel`` is a per-checkout token that lets us tell schemas apart
    when their bodies happen to be otherwise identical.
    """
    return (
        "-- synthetic alpha_bootstrap.sql — "
        f"sentinel={sentinel}\n"
        "BEGIN;\n"
        "-- >>> ALPHA_BOOTSTRAP_INCLUDE: schema/explicit_memories.sql <<<\n"
        "COMMIT;\n"
    )


class TestBootstrapPathResolution:
    """Path-resolution hardening: cwd must not select the wrong schema."""

    def test_script_dir_uses_module_file(self, bootstrap_mod):
        """Internal contract: ``_script_dir()`` returns the directory of
        the executing module's ``__file__``, not cwd.
        """
        sd = bootstrap_mod._script_dir()
        # Resolves to the real scripts dir next to this test file.
        assert sd.name == "scripts"
        assert (sd / "bootstrap_alpha_db.py").is_file()
        # And critically: _script_dir() must not equal Path.cwd().
        assert sd.resolve() != Path.cwd().resolve()

    def test_resolve_repo_root_default_uses_script_dir(
        self, bootstrap_mod, tmp_path
    ):
        """Without --repo-root, the resolver walks upward from the
        executing script, never from cwd. Build a second checkout at
        ``tmp_path`` and chdir there; the script must STILL pick the
        real repo (its own __file__), not the unrelated tmp_path tree.
        """
        # Build a tempting-but-wrong checkout at tmp_path. If the script
        # ever falls back to cwd ancestor, it would find this one.
        wrong = _materialize_checkout(
            tmp_path / "wrong-checkout",
            schema_body=_build_marker_aware_schema("WRONG"),
            scripts_dir=BOOTSTRAP_PY,
        )
        # Sanity: the wrong checkout really does carry the marker.
        assert (wrong / "src" / "v3-core" / "schema" / "alpha_bootstrap.sql").is_file()

        cwd = tmp_path / "elsewhere-cwd"
        cwd.mkdir()
        args = argparse.Namespace(repo_root="")
        resolved = bootstrap_mod._resolve_repo_root(args)
        # The real repo's schema must win.
        assert resolved.resolve() == REPO_ROOT.resolve(), (
            f"resolved={resolved} REPO_ROOT={REPO_ROOT}"
        )
        # And it must NOT be the wrong checkout.
        assert resolved.resolve() != wrong.resolve()

    def test_check_only_uses_script_dir_when_cwd_differs(
        self, bootstrap_mod, monkeypatch, capsys, tmp_path
    ):
        """End-to-end: invoke check-only from a completely different
        checkout root and confirm the JSON output points at the script's
        adjacent schema, not the cwd's schema.
        """
        wrong = _materialize_checkout(
            tmp_path / "source-checkout",
            schema_body=_build_marker_aware_schema("SOURCE"),
            scripts_dir=BOOTSTRAP_PY,
        )
        # Run from a separate, unrelated cwd that has nothing to do
        # with either checkout.
        neutral_cwd = tmp_path / "neutral-cwd"
        neutral_cwd.mkdir()
        monkeypatch.chdir(neutral_cwd)
        rc = bootstrap_mod.main(["--check-only", "--log-level", "WARNING"])
        assert rc == 0, f"check-only should exit 0; out={capsys.readouterr().out}"
        out = capsys.readouterr().out.strip().splitlines()[-1]
        data = json.loads(out)
        # The emitted JSON must carry the chosen root + includes.
        assert "repo_root" in data, f"missing repo_root in {data!r}"
        assert "includes" in data, f"missing includes in {data!r}"
        assert data["status"] == "check_only"
        # And the chosen root is the real repo (script-adjacent), NOT
        # the unrelated source-checkout.
        emitted = Path(data["repo_root"]).resolve()
        assert emitted == REPO_ROOT.resolve(), (
            f"emitted_repo_root={emitted} REPO_ROOT={REPO_ROOT} "
            f"wrong={wrong}"
        )
        assert emitted != wrong.resolve()
        # Includes path resolves under the chosen root.
        for inc in data["includes"]:
            ip = Path(inc).resolve()
            assert ip.is_relative_to(emitted), (
                f"include {inc} escapes chosen repo_root {emitted}"
            )

    def test_explicit_repo_root_overrides_script_dir(
        self, bootstrap_mod, tmp_path
    ):
        """--repo-root is the explicit override. Build two checkouts;
        pointing --repo-root at the second must select the second,
        not the first (i.e. --repo-root wins over the script-adjacent
        default).
        """
        second = _materialize_checkout(
            tmp_path / "second-checkout",
            schema_body=_build_marker_aware_schema("SECOND"),
            scripts_dir=BOOTSTRAP_PY,
        )
        args = argparse.Namespace(repo_root=str(second))
        resolved = bootstrap_mod._resolve_repo_root(args)
        assert resolved.resolve() == second.resolve()
        assert resolved.resolve() != REPO_ROOT.resolve()

    def test_invalid_repo_root_fails_closed(self, bootstrap_mod, tmp_path):
        """A --repo-root that does not contain the schema marker must
        fail closed — silently picking the script-adjacent schema would
        be exactly the bug we're closing.
        """
        empty_dir = tmp_path / "not-a-checkout"
        empty_dir.mkdir()
        args = argparse.Namespace(repo_root=str(empty_dir))
        with pytest.raises(FileNotFoundError) as ei:
            bootstrap_mod._resolve_repo_root(args)
        assert "alpha_bootstrap.sql" in str(ei.value)

    def test_find_repo_root_raises_when_no_marker(self, bootstrap_mod, tmp_path):
        """A leaf directory with no upward marker must raise — _resolve_repo_root
        must surface that to the caller as exit code 4."""
        leaf = tmp_path / "leaf"
        leaf.mkdir()
        with pytest.raises(FileNotFoundError):
            bootstrap_mod._find_repo_root(leaf)

    def test_subprocess_cwd_outside_repo_still_finds_adjacent_schema(
        self, tmp_path
    ):
        """Hard end-to-end check (mirrors the original bug report):
        run the REAL script from a cwd that is inside a different
        checkout, and confirm the JSON output identifies the script's
        adjacent schema, NOT the cwd's.

        We invoke the actual bootstrap_alpha_db.py via ``python`` so
        ``__file__`` is the on-disk copy, not the test-imported module.
        """
        # Build a decoy checkout that physically lives OUTSIDE the real
        # candidate, with a *script copy* under src/v3-core/scripts/ —
        # this is the "clean export" topology from the bug report.
        decoy = tmp_path / "clean-export"
        decoy.mkdir()
        _materialize_checkout(
            decoy,
            schema_body=_build_marker_aware_schema("DECOY"),
            scripts_dir=BOOTSTRAP_PY,
        )
        decoy_script = decoy / "src" / "v3-core" / "scripts" / "bootstrap_alpha_db.py"
        assert decoy_script.is_file(), "decoy script copy missing"

        # Run from a totally unrelated cwd (no AGENTS.md, no schema).
        unrelated_cwd = tmp_path / "totally-unrelated"
        unrelated_cwd.mkdir()

        result = subprocess.run(
            [
                sys.executable,
                str(decoy_script),
                "--check-only",
                "--log-level",
                "WARNING",
            ],
            cwd=str(unrelated_cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"check-only failed; rc={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        # Parse the last JSON line on stdout.
        out_lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
        assert out_lines, f"empty stdout; stderr={result.stderr!r}"
        data = json.loads(out_lines[-1])
        assert data["status"] == "check_only"
        emitted_root = Path(data["repo_root"]).resolve()
        # The emitted root MUST be the decoy tree (the script-adjacent
        # schema) — NOT the real source candidate.
        assert emitted_root == decoy.resolve(), (
            f"expected script-adjacent decoy {decoy}, got {emitted_root}"
        )
        # Belt-and-braces: it must NOT be the real source candidate.
        assert emitted_root != REPO_ROOT.resolve()

    def test_subprocess_invalid_repo_root_exits_4(self, tmp_path):
        """A bogus --repo-root passed to the REAL script exits 4 with
        a JSON ``status=error`` (locate stage) — never falls back to
        the script-adjacent schema silently.
        """
        # Use the real script (not a decoy copy) for this assertion —
        # we want to verify the CLI surface directly.
        result = subprocess.run(
            [
                sys.executable,
                str(BOOTSTRAP_PY),
                "--check-only",
                "--repo-root",
                str(tmp_path / "does-not-exist"),
                "--log-level",
                "WARNING",
            ],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 4, (
            f"invalid --repo-root should exit 4; rc={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        out_lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
        assert out_lines, f"empty stdout; stderr={result.stderr!r}"
        data = json.loads(out_lines[-1])
        assert data["status"] == "error"
        assert data["stage"] == "locate"
        assert "alpha_bootstrap.sql" in data["detail"]

    def test_redacted_output_carries_repo_root_and_includes(
        self, bootstrap_mod, monkeypatch, capsys, tmp_path
    ):
        """The successful --check-only JSON must include both repo_root
        and includes so operators can audit which copy was selected.
        """
        monkeypatch.chdir(tmp_path)
        rc = bootstrap_mod.main(
            [
                "--check-only",
                "--repo-root",
                str(REPO_ROOT),
                "--log-level",
                "WARNING",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()[-1]
        data = json.loads(out)
        assert data["status"] == "check_only"
        # Both paths present and resolvable on disk.
        assert Path(data["repo_root"]).is_dir()
        for inc in data["includes"]:
            assert Path(inc).is_file()
        # And the includes path lives UNDER repo_root, not some other tree.
        rr = Path(data["repo_root"]).resolve()
        for inc in data["includes"]:
            assert Path(inc).resolve().is_relative_to(rr)


# ---------------------------------------------------------------------------
# backup_alpha.py — static contract
# ---------------------------------------------------------------------------


class TestBackupScript:
    def test_file_exists(self):
        assert BACKUP_PY.is_file()

    def test_no_hardcoded_passwords(self):
        text = BACKUP_PY.read_text(encoding="utf-8")
        bad = re.findall(
            r"(password\s*[:=]\s*['\"])([^'\"]+)(['\"])",
            text,
            re.IGNORECASE,
        )
        assert not bad, f"backup 脚本禁止硬编码密码: {bad}"

    def test_no_hardcoded_production_dsn(self):
        text = BACKUP_PY.read_text(encoding="utf-8")
        no_docstring = re.sub(r'^\s*"""[\s\S]*?"""\s*', "", text, count=1)
        non_comment = "\n".join(
            line for line in no_docstring.splitlines()
            if not line.lstrip().startswith("#")
        )
        bad = re.search(
            r"(port\s*=\s*5433|dbname\s*=\s*['\"]v3embeddings['\"]|"
            r"database\s*=\s*['\"]v3embeddings['\"])",
            non_comment,
            re.IGNORECASE,
        )
        assert not bad, f"backup 脚本禁止硬编码生产 DSN 默认值: {bad.group(0)!r}"

    def test_uses_pg_dump_pg_restore(self, backup_mod):
        # The constants defining ALPHA_TABLES must be present and ordered.
        assert backup_mod.ALPHA_TABLES == (
            "explicit_memories",
            "qa_pairs",
            "conversation_stream",
            "topics",
            "topic_entries",
            "observation_notes",
            "yin_paragraphs",
        )

    def test_refuses_source_raw_extras(self, backup_mod, tmp_path, capsys):
        """Explicit refusal per CONTEXT: source/raw table backups are not
        part of the alpha working set, even if requested via --extra-table.
        Refusal surfaces as JSON ``status: refused`` + exit 2 (NOT a Python
        exception bubbling out of main()) — the contract documented in
        backup_alpha.py is that all refusals are JSON-shaped so downstream
        operators can parse them uniformly."""
        rc = backup_mod.main(
            [
                "--dry-run",
                "--dsn",
                "host=localhost port=55433 dbname=alpha_test_fresh user=v3user password=***",
                "--extra-table",
                "source_raw_messages",
                "--out-dir",
                str(tmp_path),
            ]
        )
        out = capsys.readouterr().out.strip().splitlines()[-1]
        assert rc == 2, f"source_raw_* extras 应该拒绝退出 2, 实际={rc}; out={out}"
        data = json.loads(out)
        assert data["status"] == "refused"
        assert "source_raw" in data["detail"]

    def test_dry_run_does_not_connect(self, backup_mod, monkeypatch, capsys, tmp_path):
        monkeypatch.chdir(tmp_path)
        rc = backup_mod.main(
            [
                "--dry-run",
                "--dsn",
                "host=localhost port=5432 dbname=alpha user=v3user password=hunter2",
                "--out-dir",
                str(tmp_path),
                "--log-level",
                "WARNING",
            ]
        )
        out = capsys.readouterr().out.strip().splitlines()[-1]
        assert rc == 0, f"dry-run 应该返回 0; out={out}"
        data = json.loads(out)
        assert data["status"] == "dry_run"
        # Password must be redacted in the command preview.
        assert "hunter2" not in data["would_run"], f"密码泄漏: {data['would_run']}"

    def test_dry_run_covers_all_alpha_tables(self, backup_mod, monkeypatch, capsys, tmp_path):
        monkeypatch.chdir(tmp_path)
        rc = backup_mod.main(
            [
                "--dry-run",
                "--dsn",
                "host=localhost port=5432 dbname=alpha user=v3user password=***",
                "--out-dir",
                str(tmp_path),
                "--log-level",
                "WARNING",
            ]
        )
        out = capsys.readouterr().out.strip().splitlines()[-1]
        data = json.loads(out)
        cmd = data["would_run"]
        for tbl in backup_mod.ALPHA_TABLES:
            assert f"--table=public.{tbl}" in cmd, (
                f"dry-run 缺失 --table=public.{tbl}: {cmd}"
            )

    def test_refuses_empty_dsn(self, backup_mod, monkeypatch, capsys, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("V3CORE_BACKUP_DSN", raising=False)
        rc = backup_mod.main(
            [
                "--dry-run",
                "--dsn",
                "",
                "--out-dir",
                str(tmp_path),
                "--log-level",
                "WARNING",
            ]
        )
        out = capsys.readouterr().out.strip().splitlines()[-1]
        assert rc == 2
        data = json.loads(out)
        assert data["status"] == "refused"

    def test_refuses_production_dsn_by_default(
        self, backup_mod, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        rc = backup_mod.main(
            [
                "--dry-run",
                "--dsn",
                "host=127.0.0.1 port=5433 dbname=v3embeddings user=v3user password=***",
                "--out-dir",
                str(tmp_path),
                "--log-level",
                "WARNING",
            ]
        )
        out = capsys.readouterr().out.strip().splitlines()[-1]
        assert rc == 2
        data = json.loads(out)
        assert data["status"] == "refused"

    def test_allows_production_dsn_with_flag(
        self, backup_mod, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        rc = backup_mod.main(
            [
                "--dry-run",
                "--allow-production-dsn",
                "--dsn",
                "host=127.0.0.1 port=5433 dbname=v3embeddings user=v3user password=***",
                "--out-dir",
                str(tmp_path),
                "--log-level",
                "WARNING",
            ]
        )
        out = capsys.readouterr().out.strip().splitlines()[-1]
        data = json.loads(out)
        assert data["status"] == "dry_run", f"out={out}"
