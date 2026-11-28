# -*- coding: utf-8 -*-
"""Offline unit & AST contract tests for Observer/E1 decoupling from public.topics
and unified multi-model routing through v3core.llm_gateway.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import requests

REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

from v3core import e1 as e1_mod  # noqa: E402
from v3core import llm as llm_mod  # noqa: E402
from v3core import observer as observer_mod  # noqa: E402
from v3core.generated_context_contract import (  # noqa: E402
    validate_identity_block,
    validate_situation_overview,
)


_V3CORE_DIR = REPO_SRC / "v3core"
_CREDENTIAL_ENV_VARS = (
    "V3_LLM_API_KEY",
    "MINIMAX_CN_API_KEY",
    "MINIMAX_API_KEY",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "SILICONFLOW_API_KEY",
    "OPENROUTER_API_KEY",
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPU_API_KEY",
    "GLM_API_KEY",
    "OPENCODE_API_KEY",
    "OPENAI_API_KEY",
)


class _FakeHttpResponse:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload, ensure_ascii=False)
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


@pytest.fixture
def clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# 1. Static AST single-writer guards
# ---------------------------------------------------------------------------

def _defined_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _imported_and_referenced_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name)
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _string_constants(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


_TOPIC_DML_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:public\.)?(?:topics|topic_entries)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    ("filename", "forbidden_symbols"),
    [
        (
            "observer.py",
            {
                "TopicStore",
                "topic_store",
                "_mark_topic_updates_pending",
                "_link_entries_by_vector",
                "_apply_topic_updates",
                "SEED_COMPRESS_PROMPT",
                "_seed_compress",
            },
        ),
        (
            "e1.py",
            {
                "TopicStore",
                "topic_store",
                "merge_topic_pg",
                "_merge_duplicate_topics",
            },
        ),
    ],
)
def test_single_writer_ast_guards_forbid_topic_mutations(
    filename: str,
    forbidden_symbols: set[str],
) -> None:
    source = (_V3CORE_DIR / filename).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=filename)

    all_symbols = _defined_names(tree) | _imported_and_referenced_names(tree)
    leaked = forbidden_symbols & all_symbols
    assert not leaked, f"{filename} still references retired topic writer symbols: {sorted(leaked)}"

    dml_hits = [s for s in _string_constants(tree) if _TOPIC_DML_RE.search(s)]
    assert not dml_hits, f"{filename} contains SQL DML targeting topics/topic_entries: {dml_hits[:3]}"


def test_e1_retains_readonly_radar_scan_import() -> None:
    source = (_V3CORE_DIR / "e1.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="e1.py")
    radar_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").endswith("topic_radar")
        and any(a.name == "radar_scan" for a in node.names)
    ]
    assert radar_imports, "e1.py must retain read-only `from .topic_radar import radar_scan`"


# ---------------------------------------------------------------------------
# 2. Observer backward compatibility & Topic V2 header stripping
# ---------------------------------------------------------------------------

def test_observer_parse_m3_output_accepts_legacy_topic_updates() -> None:
    payload = json.dumps(
        {
            "note": {"content": "本轮观察结论：调度器已完成配置迁移并保持稳定运行。"},
            "topic_updates": [
                {"action": "update", "topic_id": "t_1", "facts": ["事实A"]},
                {"action": "create", "title": "新主题", "facts": ["事实B"]},
            ],
        },
        ensure_ascii=False,
    )
    parsed = observer_mod._parse_observer_json(payload)
    assert parsed is not None
    assert "调度器已完成配置迁移" in parsed["note"]["content"]
    assert isinstance(parsed["topic_updates"], list) and len(parsed["topic_updates"]) == 2


def test_observer_recall_candidates_strips_topic_v2_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from v3core import embedding as emb_mod
    from v3core import config as cfg_mod

    monkeypatch.setattr(
        cfg_mod,
        "resolve_config",
        lambda *a, **kw: {
            "storage": {
                "embed": {
                    "endpoint": "http://127.0.0.1:0/v1/embeddings",
                    "model": "bge-m3",
                    "_fingerprint": "test-fp",
                }
            }
        },
    )
    monkeypatch.setattr(emb_mod, "safe_embed_cfg", lambda c: c["storage"]["embed"])
    monkeypatch.setattr(emb_mod, "call_embedding", lambda *a, **kw: [0.1] * 4)

    class _FakeCur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            return [
                (
                    "tv2_scheduler",
                    "调度器架构",
                    '<!-- topic_v2:{"version":2,"kind":"dossier"} -->\n## 当前结论\nEvent-Scheduler-V2 已上线',
                    0.91,
                )
            ]

    class _FakeConn:
        def cursor(self):
            return _FakeCur()

    class _FakeLease:
        def __init__(self):
            self.connection = _FakeConn()

        def close(self):
            pass

    class _FakePool:
        def lease(self, timeout=5):
            return _FakeLease()

    rows = observer_mod._recall_candidates("调度器", cfg={}, pool=_FakePool())
    assert len(rows) == 1
    assert "<!-- topic_v2:" not in rows[0]["body"]
    assert "Event-Scheduler-V2 已上线" in rows[0]["body"]


# ---------------------------------------------------------------------------
# 3. Unified multi-model routing via llm_gateway + E1 wiring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("provider", "model", "config_extra", "expected_url_part", "expected_auth_header", "expected_max_field", "expected_response"),
    [
        (
            "deepseek",
            "deepseek-chat",
            {},
            "https://api.deepseek.com/v1/chat/completions",
            ("Authorization", "Bearer sk-test-deepseek"),
            "max_tokens",
            {"choices": [{"message": {"content": "ds-ok"}}]},
        ),
        (
            "siliconflow",
            "Qwen/Qwen2.5-72B-Instruct",
            {},
            "https://api.siliconflow.cn/v1/chat/completions",
            ("Authorization", "Bearer sk-test-silicon"),
            "max_tokens",
            {"choices": [{"message": {"content": "sf-ok"}}]},
        ),
        (
            "anthropic",
            "claude-sonnet-4-20250514",
            {},
            "https://api.anthropic.com/v1/messages",
            ("x-api-key", "sk-ant-test"),
            "max_tokens",
            {"content": [{"type": "text", "text": "ant-ok"}]},
        ),
        (
            "opencode-go",
            "gpt-5.4-mini",
            {"prompts": {}},
            "https://opencode.ai/zen/go/v1/responses",
            ("Authorization", "Bearer sk-oc-test"),
            "max_output_tokens",
            {"output": [{"type": "message", "content": [{"type": "output_text", "text": "oc-ok"}]}]},
        ),
        (
            "minimax-cn",
            "MiniMax-M3.1-Flash",
            {},
            "https://api.minimaxi.com/v1/chat/completions",
            ("Authorization", "Bearer sk-mmcn-test"),
            "max_completion_tokens",
            {"choices": [{"message": {"content": "mmcn-ok"}}]},
        ),
        (
            "ollama",
            "qwen2.5:14b",
            {},
            "http://127.0.0.1:11434/v1/chat/completions",
            ("Authorization", "Bearer ollama"),
            "max_tokens",
            {"choices": [{"message": {"content": "ollama-ok"}}]},
        ),
        (
            "custom-gw",
            "custom-model-v1",
            {
                "providers": {
                    "custom-gw": {
                        "api_mode": "chat_completions",
                        "base_url": "https://gw.example.internal/v1",
                        "max_tokens_field": "max_tokens",
                        "supports": {"temperature": True},
                    }
                }
            },
            "https://gw.example.internal/v1/chat/completions",
            ("Authorization", "Bearer sk-custom-gw"),
            "max_tokens",
            {"choices": [{"message": {"content": "custom-ok"}}]},
        ),
    ],
)
def test_llm_client_routes_all_providers_via_gateway(
    clean_llm_env: None,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model: str,
    config_extra: dict[str, Any],
    expected_url_part: str,
    expected_auth_header: tuple[str, str],
    expected_max_field: str,
    expected_response: dict[str, Any],
) -> None:
    captured: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None, **kwargs: Any):
        captured.append({"url": url, "json": json or {}, "headers": headers or {}})
        return _FakeHttpResponse(200, expected_response)

    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)

    api_key = "" if provider == "ollama" else expected_auth_header[1].replace("Bearer ", "")
    cfg: dict[str, Any] = {
        **config_extra,
        "llm": {
            "provider": provider,
            "model": model,
            "api_key": api_key,
            "thinking": False,
        },
    }
    client = llm_mod.LLMClient(cfg)
    assert client._profile_routed() is True

    text = client.chat("system prompt", [{"role": "user", "content": "user hello"}], temperature=0.2, max_output_tokens=768)
    assert text
    assert len(captured) == 1
    req = captured[0]
    assert req["url"] == expected_url_part
    hdr_name, hdr_val = expected_auth_header
    assert req["headers"].get(hdr_name) == hdr_val
    assert req["json"].get(expected_max_field) == 768
    if provider == "anthropic":
        assert req["headers"].get("anthropic-version") == "2023-06-01"
    if provider == "opencode-go":
        assert "x-opencode-session" in req["headers"]


def test_e1_resolve_api_key_provider_agnostic(
    clean_llm_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("V3_LLM_API_KEY", "sk-unified-gateway")
    assert (
        e1_mod._resolve_e1_api_key({"llm": {"provider": "deepseek", "model": "deepseek-chat"}})
        == "sk-unified-gateway"
    )

    monkeypatch.delenv("V3_LLM_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-env")
    assert (
        e1_mod._resolve_e1_api_key({"llm": {"provider": "deepseek", "model": "deepseek-chat"}})
        == "sk-ds-env"
    )

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert e1_mod._resolve_e1_api_key({"llm": {"provider": "ollama", "model": "qwen2.5:7b"}}) == "ollama"
    assert (
        e1_mod._resolve_e1_api_key({"llm": {"provider": "anthropic", "apiKey": "sk-ant-camel"}})
        == "sk-ant-camel"
    )


def test_e1_synthesize_yin_routes_non_minimax_and_strips_topic_v2_header(
    clean_llm_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_requests: list[dict[str, Any]] = []
    valid_identity = (
        "我是一位长期协同工程架构师，专注记忆系统稳定性与测试闭环。"
        "我坚持只陈述已验证的事实，区分当前默认架构与历史兼容路径。"
    )
    valid_situation = (
        "## 活跃主题\n"
        "系统当前以 Topic V2 作为唯一结构化主题写入入口。\n\n"
        "## 当前关注与待办\n"
        "持续推进多模型网关与隔离 PostgreSQL 验收测试。\n\n"
        "## 状态速记\n"
        "各主链路运行平稳，测试闭环覆盖核心契约。\n"
    )
    valid_yin = "# 连续人格印\n\n系统已切换到单写者 Topic V2 架构并保持稳定。"

    assert validate_identity_block(valid_identity)[0]
    assert validate_situation_overview(valid_situation)[0]

    def fake_post(url: str, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None, **kwargs: Any):
        body = json or {}
        captured_requests.append({"url": url, "json": body, "headers": headers or {}})
        msgs = body.get("messages") or []
        sys_txt = str(body.get("system") or "") + " " + " ".join(
            str(m.get("content", "")) for m in msgs if m.get("role") == "system"
        )
        user_txt = " ".join(str(m.get("content", "")) for m in msgs if m.get("role") != "system")
        assert "<!-- topic_v2:" not in user_txt
        if "自我简述" in sys_txt or "我是谁" in user_txt or "identity" in sys_txt.lower():
            text = valid_identity
        elif "系统态势总览" in sys_txt or "活跃主题" in sys_txt:
            text = valid_situation
        else:
            text = valid_yin
        return _FakeHttpResponse(200, {"choices": [{"message": {"content": text}}]})

    from v3core import topic_radar as topic_radar_mod

    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(topic_radar_mod, "radar_scan", lambda *a, **kw: [])
    monkeypatch.setattr(e1_mod, "_ingest_yin_segments", lambda *a, **kw: None)

    class _FakeCur:
        def __init__(self, state: dict[str, Any]):
            self._state = state
            self._last_sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql: str, params: Any = None):
            self._last_sql = sql
            self._state["sqls"].append(sql)

        def fetchall(self):
            low = self._last_sql.lower()
            if "from topics" in low:
                body = '<!-- topic_v2:{"version":2,"kind":"dossier"} -->\n' + (
                    "调度器升级为 Event-Scheduler-V2，替代旧版。" * 3
                )
                if "created_at" in low:
                    return [("调度器升级", body, "2026-11-28")]
                return [("调度器升级", body)]
            return []

        def fetchone(self):
            low = self._last_sql.lower()
            if "from observation_notes" in low:
                return ("观察者快照：确认调度器已切换完成。",)
            return (1,)

    class _FakePg:
        def __init__(self):
            self.sqls: list[str] = []

        def cursor(self):
            return _FakeCur({"sqls": self.sqls})

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    fake_pg = _FakePg()
    monkeypatch.setattr(e1_mod, "_get_cfg_pg_conn_compat", lambda cfg, pool=None: fake_pg)

    cfg = {
        "basePath": str(tmp_path),
        "llm": {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "api_key": "sk-ds-unit",
            "thinking": False,
        },
        "e1": {"enabled": True, "confidence_threshold": 0.3},
        "storage": {"pg": {"host": "127.0.0.1", "port": 55432, "database": "unused"}},
    }
    res = e1_mod.synthesize_yin(cfg, dry_run=False)
    assert "E1 完成" in res or "y_" in res
    assert len(captured_requests) == 3
    assert all(r["url"] == "https://api.deepseek.com/v1/chat/completions" for r in captured_requests)
    assert captured_requests[-1]["json"].get("max_tokens") == e1_mod.SITUATION_OVERVIEW_MAX_OUTPUT_TOKENS
    assert not any(_TOPIC_DML_RE.search(sql) for sql in fake_pg.sqls)
    assert (tmp_path / "identity_block.md").exists()
    assert (tmp_path / "situation_overview.md").exists()
