# -*- coding: utf-8 -*-
"""CLI 一等测试面：``python -m v3core.tools.embedding_backfill`` 的真实入口门禁。

不变量（本文件存在的唯一理由）：
    此前所有 E2E 都只调内部函数 ``backfill_table()``，**从来没有启动过真实 CLI**。
    于是 D1（``from v3core.pg_store import PGStore`` —— 该符号根本不存在）和
    D2（``cfg.get("embedding") or cfg.get("embed")`` —— resolve_config() 返回
    typed V3Config，两个键都不是 dict）在"全绿"的验证下活到了生产：
    CLI 任何调用都会 ImportError 退出，而没人测过它。

    所以这里的每一例都必须**真实启动子进程**跑 main path，不得 monkeypatch 绕过
    ``resolve_config`` / ``safe_embed_cfg`` / ``PgEmbedStore`` 连接建立 ——
    那三处正是缺陷所在。绕过它们等于把缺陷重新藏起来。

    测试域 conftest 硬封禁 psycopg2.connect，但本文件的连接尝试发生在**子进程**里，
    且配置被指向一个死端口，所以不会有任何真实数据库连接。

不联网、除 tmp_path 外不写文件。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

V3_CORE_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = V3_CORE_DIR / "src"

# 一个几乎必然关闭的本地端口：连接会失败，但不会打到任何真实服务。
DEAD_PORT = 59987


def _base_config() -> dict:
    return {
        "storage": {
            "embed": {
                "endpoint": "https://embed.invalid/v1",
                "model": "test-embed-model",
                "apiKey": "test-key",
                "dim": 8,
            },
            "pg": {
                "host": "127.0.0.1",
                "port": DEAD_PORT,
                "database": "no_such_db",
                "user": "v3user",
                "password": "x",
            },
        }
    }


def _write_config(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


def _run_cli(config_path: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """真实子进程跑 CLI。绝不绕过 resolve_config / safe_embed_cfg / PgEmbedStore。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    env["V3CORE_CONFIG"] = str(config_path)
    env["V3CORE_PG_PASSWORD"] = "x"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "v3core.tools.embedding_backfill", *args],
        capture_output=True, text=True, env=env, cwd=str(V3_CORE_DIR), timeout=timeout,
    )


# ── 1. argparse 面 ────────────────────────────────────────────────────────────

def test_help_exits_zero_and_lists_apply(tmp_path):
    """``--help`` 必须 rc=0 —— 修复前连这一条都是 ImportError。"""
    r = _run_cli(_write_config(tmp_path, _base_config()), "--help")
    assert r.returncode == 0
    assert "--apply" in r.stdout
    assert "ImportError" not in r.stderr


def test_help_never_reports_the_missing_pgstore_symbol(tmp_path):
    """D1 的回归钉子：任何情况下都不得再出现找不到 PGStore 的 ImportError。"""
    r = _run_cli(_write_config(tmp_path, _base_config()), "--help")
    assert "PGStore" not in r.stderr
    assert "cannot import name" not in r.stderr


def test_invalid_table_is_rejected_by_argparse(tmp_path):
    r = _run_cli(_write_config(tmp_path, _base_config()), "--table", "nosuchtable")
    assert r.returncode == 2
    assert "invalid choice" in r.stderr


def test_missing_table_argument_is_rejected(tmp_path):
    r = _run_cli(_write_config(tmp_path, _base_config()))
    assert r.returncode == 2
    assert "required" in r.stderr.lower()


# ── 2. 配置 fail-closed 面（D2） ──────────────────────────────────────────────

def test_no_embed_section_fails_closed(tmp_path):
    """完全没有 embed 子配置 → rc=2，明确 fail-closed，不伪装成 disabled 继续跑。"""
    cfg = _base_config()
    del cfg["storage"]["embed"]
    r = _run_cli(_write_config(tmp_path, cfg), "--table", "topics")
    assert r.returncode == 2
    assert "no embedding configured" in r.stderr
    assert "fail-closed" in r.stderr


def test_missing_model_fails_closed_and_names_the_key(tmp_path):
    cfg = _base_config()
    del cfg["storage"]["embed"]["model"]
    r = _run_cli(_write_config(tmp_path, cfg), "--table", "topics")
    assert r.returncode == 2
    assert "fail-closed" in r.stderr
    assert "model" in r.stderr


def test_missing_endpoint_fails_closed_and_names_the_key(tmp_path):
    cfg = _base_config()
    del cfg["storage"]["embed"]["endpoint"]
    r = _run_cli(_write_config(tmp_path, cfg), "--table", "topics")
    assert r.returncode == 2
    assert "fail-closed" in r.stderr
    assert "endpoint" in r.stderr


def test_config_errors_are_not_disguised_as_disabled(tmp_path):
    """D2 的语义边界：配置存在但非法 ≠ 未配置。

    两者必须走**不同**的分支（非法 → "configuration invalid"，
    未配置 → "no embedding configured"），否则运维看到的信息是错的。
    """
    cfg = _base_config()
    del cfg["storage"]["embed"]["model"]
    invalid = _run_cli(_write_config(tmp_path, cfg), "--table", "topics")

    cfg2 = _base_config()
    del cfg2["storage"]["embed"]
    disabled = _run_cli(_write_config(tmp_path, cfg2), "--table", "topics")

    assert invalid.returncode == disabled.returncode == 2
    assert "configuration invalid" in invalid.stderr
    assert "no embedding configured" in disabled.stderr


# ── 3. 连接 fail-closed 面（D1） ─────────────────────────────────────────────

def test_unreachable_postgres_fails_closed_with_rc3(tmp_path):
    """PG 不可达 → rc=3。

    这一条**只有**在 D1 修好后才可达：修复前 lease/连接契约根本建立不起来，
    进程在 import 阶段就死了，永远走不到这里。rc=3 本身就是 D1 生效的证据。
    """
    r = _run_cli(_write_config(tmp_path, _base_config()), "--table", "topics")
    assert r.returncode == 3, f"stderr={r.stderr[-500:]}"
    assert "could not obtain a PostgreSQL connection" in r.stderr
    assert "fail-closed" in r.stderr


def test_unreachable_postgres_does_not_leak_a_traceback(tmp_path):
    """连接失败必须是**有意的 fail-closed 消息**，而不是未捕获异常的回溯。"""
    r = _run_cli(_write_config(tmp_path, _base_config()), "--table", "topics")
    assert "Traceback (most recent call last)" not in r.stderr


@pytest.mark.parametrize("table", [
    "conversation_stream", "observation_notes", "yin_paragraphs", "qa_pairs", "topics",
])
def test_every_canonical_table_reaches_the_connection_stage(tmp_path, table):
    """五张表都要能走到连接阶段（rc=3），证明表分派与配置解析对每张表都通。"""
    r = _run_cli(_write_config(tmp_path, _base_config()), "--table", table)
    assert r.returncode == 3, f"{table}: stderr={r.stderr[-400:]}"
