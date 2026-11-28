# -*- coding: utf-8 -*-
"""Isolated real PostgreSQL + pgvector acceptance tests for Observer/E1 decoupling
and unified llm_gateway routing.

Opt-in gate:
    Runs ONLY when ``HIPPOCAMPUS_TOPIC_V2_TEST_DSN`` is set and points to a
    loopback, non-production (port != 5433, dbname != 'v3embeddings') database.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
import requests

_TEST_DSN = os.environ.get("HIPPOCAMPUS_TOPIC_V2_TEST_DSN", "").strip()
if not _TEST_DSN:
    pytest.skip(
        "HIPPOCAMPUS_TOPIC_V2_TEST_DSN not set — opt-in isolated-PG Observer/E1 acceptance skipped",
        allow_module_level=True,
    )


def _parse_dsn(dsn: str) -> dict[str, str]:
    try:
        from psycopg2.extensions import parse_dsn as _pg_parse_dsn

        parsed = _pg_parse_dsn(dsn)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if v is not None}
    except Exception:
        pass
    out: dict[str, str] = {}
    for tok in dsn.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            out[k.strip()] = v.strip()
    return out


_DSN_PARTS = _parse_dsn(_TEST_DSN)
_DSN_PORT = _DSN_PARTS.get("port", "")
_DSN_HOST = _DSN_PARTS.get("host", "")
_DSN_DB = _DSN_PARTS.get("dbname", _DSN_PARTS.get("database", ""))
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "localhost.localdomain"}

if _DSN_PORT == "5433" or _DSN_DB == "v3embeddings":
    pytest.skip(
        "HIPPOCAMPUS_TOPIC_V2_TEST_DSN targets production (port 5433 / v3embeddings); refusing to run.",
        allow_module_level=True,
    )
if _DSN_HOST and _DSN_HOST.lower() not in _LOOPBACK_HOSTS:
    pytest.skip(
        f"HIPPOCAMPUS_TOPIC_V2_TEST_DSN host={_DSN_HOST!r} is not loopback; refusing to run.",
        allow_module_level=True,
    )

REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))


def _resolve_real_connect() -> Callable[..., Any]:
    try:
        from psycopg2._psycopg import connect as _real_connect  # type: ignore

        return _real_connect
    except Exception:
        pass
    try:
        from psycopg2._psycopg import _connect as _real_connect  # type: ignore

        return _real_connect
    except Exception:
        pass
    import psycopg2 as _psycopg2

    return _psycopg2.connect


_REAL_CONNECT = _resolve_real_connect()

import v3core  # noqa: E402
import v3core.config as v3_config  # noqa: E402
import v3core.e1 as e1_mod  # noqa: E402
import v3core.embedding as emb_mod  # noqa: E402
import v3core.llm as llm_mod  # noqa: E402
import v3core.observation_chunks as obs_chunks_mod  # noqa: E402
import v3core.observer as observer_mod  # noqa: E402
from v3core.config_model import from_legacy_dict  # noqa: E402
from v3core.generated_context_contract import (  # noqa: E402
    validate_identity_block,
    validate_situation_overview,
)
from v3core.pg_pool import PgPool  # noqa: E402
from v3core.pg_store import PgEmbedStore  # noqa: E402
from v3core.prefetch import prefetch  # noqa: E402
from v3core.topic_v2 import consolidate_pg_incremental  # noqa: E402

_QA_PREFIX = "oepg_qa_"
_TOPIC_PREFIX = "tv2_"
_EXTRA_TOPIC_PREFIX = "oepg_topic_"
_MARKER = "oepg_marker"

_THEME_KEYWORDS: list[tuple[int, tuple[str, ...]]] = [
    (0, ("定时", "任务", "调度", "cron", "scheduler", "worker", "event-scheduler")),
    (1, ("日志", "采集", "fluentd", "vector", "logging", "loki")),
    (2, ("数据库", "连接池", "pgbouncer", "postgres", "pg", "sql")),
    (3, ("鉴权", "认证", "jwt", "oauth", "auth", "token")),
]


def _make_1024d_embed_fn() -> Callable[[str], list[float]]:
    def _embed(text: str) -> list[float]:
        low = str(text or "").lower()
        vec = [0.0] * 1024
        matched = False
        for bucket, kws in _THEME_KEYWORDS:
            if any(k in low for k in kws):
                base = bucket * 32
                for j in range(16):
                    vec[base + j] += 2.0
                matched = True
        if not matched:
            vec[512] = 1.0
        padded = f"^{low}$"
        for i in range(max(len(padded) - 2, 1)):
            tri = padded[i : i + 3].encode("utf-8", errors="ignore")
            h = int.from_bytes(hashlib.blake2b(tri, digest_size=4).digest(), "little")
            vec[600 + (h % 424)] += 0.25
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [round(v / norm, 6) for v in vec]

    return _embed


_EMBED_FN = _make_1024d_embed_fn()


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(str(x) for x in vec) + "]"


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


@pytest.fixture(autouse=True)
def _restore_pg_connect():
    import psycopg2 as _psycopg2

    saved = getattr(_psycopg2, "connect", _REAL_CONNECT)
    try:
        _psycopg2.connect = _REAL_CONNECT  # type: ignore[assignment]
    except Exception:
        pass
    try:
        yield
    finally:
        try:
            _psycopg2.connect = saved  # type: ignore[assignment]
        except Exception:
            pass


def _pg_kwargs_from_dsn(parts: dict[str, str]) -> dict[str, Any]:
    return {
        "host": parts.get("host", "127.0.0.1"),
        "port": int(parts.get("port", 5432)),
        "database": parts.get("dbname", parts.get("database", "v3test_isolated")),
        "user": parts.get("user", "v3test"),
        "password": parts.get("password", ""),
    }


def _test_pg_dict_config(base_path: Path, *, provider: str = "deepseek", model: str = "deepseek-chat") -> dict[str, Any]:
    return {
        "basePath": str(base_path),
        "llm": {
            "provider": provider,
            "model": model,
            "api_key": "sk-oepg-test",
            "thinking": False,
        },
        "e1": {
            "enabled": True,
            "confidence_threshold": 0.3,
        },
        "observer": {
            "enabled": True,
            "debounce_minutes": 0,
            "event_time_now": True,
            "count_threshold": 1,
            "token_threshold": 1,
            "batch_size": 10,
            "max_qa_per_run": 20,
            "observer_cursor_file": str(base_path / "observer_cursor.json"),
        },
        "storage": {
            "pg": _pg_kwargs_from_dsn(_DSN_PARTS),
            "embed": {
                "endpoint": "http://127.0.0.1:0/v1/embeddings",
                "model": "bge-m3",
                "_fingerprint": "oepg-test",
            },
            "rerank": {},
        },
        "prefetch": {
            "dual_path": True,
            "rrf_k": 60,
            "include_message_vector": False,
        },
        "recall": {
            "vector_top_mult": 4,
            "qa_per_term_min": 10,
            "qa_freq_limit": 40,
        },
        "half_life": 30,
    }


@pytest.fixture(scope="module")
def pg_pool() -> PgPool:
    pool = PgPool(lambda: _REAL_CONNECT(_TEST_DSN), max_connections=4, min_connections=0)
    yield pool
    try:
        pool.shutdown()
    except Exception:
        pass


@pytest.fixture(scope="module")
def pg_store(pg_pool: PgPool) -> PgEmbedStore:
    return PgEmbedStore(config=None, pool=pg_pool)


def _cleanup_pg_rows(pool: PgPool) -> None:
    with pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("DELETE FROM public.qa_pairs WHERE source_id LIKE %s", (_QA_PREFIX + "%",))
        cur.execute(
            "DELETE FROM public.topics WHERE topic_id LIKE %s OR topic_id LIKE %s",
            (_TOPIC_PREFIX + "%", _EXTRA_TOPIC_PREFIX + "%"),
        )
        cur.execute(
            "DELETE FROM public.topic_entries WHERE topic_id LIKE %s OR topic_id LIKE %s",
            (_TOPIC_PREFIX + "%", _EXTRA_TOPIC_PREFIX + "%"),
        )
        cur.execute(
            "DELETE FROM public.observation_notes WHERE prev_id IS NOT NULL AND (content LIKE %s OR version LIKE %s OR version IN ('v1', 'v1-compressed', 'v2', 'v2-compressed'))",
            ("%" + _MARKER + "%", "oepg_%"),
        )
        cur.execute(
            "DELETE FROM public.observation_notes WHERE content LIKE %s OR version LIKE %s OR version IN ('v1', 'v1-compressed', 'v2', 'v2-compressed')",
            ("%" + _MARKER + "%", "oepg_%"),
        )
        cur.execute("DELETE FROM public.yin WHERE content LIKE %s", ("%" + _MARKER + "%",))
        cur.execute("DELETE FROM public.yin_history WHERE content LIKE %s", ("%" + _MARKER + "%",))
        cur.execute("DELETE FROM public.yin_paragraphs WHERE content LIKE %s", ("%" + _MARKER + "%",))
        lease.connection.commit()


@pytest.fixture(autouse=True)
def _clean_each_test(pg_pool: PgPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v3_config, "_resolve_data_dir", lambda cfg=None: tmp_path)
    _orig_plan = obs_chunks_mod.plan_observation_chunks

    def _offline_plan(content: str, embed_cfg: Any, **kwargs: Any):
        kwargs.setdefault("tokenizer_override", "char_estimate")
        return _orig_plan(content, embed_cfg, **kwargs)

    monkeypatch.setattr(obs_chunks_mod, "plan_observation_chunks", _offline_plan)
    _cleanup_pg_rows(pg_pool)
    yield
    _cleanup_pg_rows(pg_pool)


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    cfg_dict: dict[str, Any],
    *,
    llm_responder: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    captured_llm: list[dict[str, Any]] = []
    cfg_obj = from_legacy_dict(cfg_dict)

    def _fake_resolve_config(*args: Any, return_legacy: bool = False, **kwargs: Any):
        return cfg_dict if return_legacy else cfg_obj

    monkeypatch.setattr(v3_config, "resolve_config", _fake_resolve_config)
    monkeypatch.setattr(emb_mod, "call_embedding", lambda text, *a, **kw: _EMBED_FN(str(text)))

    def _fake_post(url: str, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None, **kwargs: Any):
        body = json or {}
        if "/embeddings" in url:
            raw_in = body.get("input", "")
            inputs = [raw_in] if isinstance(raw_in, str) else list(raw_in)
            return _FakeHttpResponse(
                200,
                {
                    "data": [
                        {"index": idx, "embedding": _EMBED_FN(str(item))}
                        for idx, item in enumerate(inputs)
                    ]
                },
            )
        captured_llm.append({"url": url, "json": body, "headers": headers or {}})
        payload = llm_responder(url, body) if llm_responder else {"choices": [{"message": {"content": "ok"}}]}
        return _FakeHttpResponse(200, payload)

    monkeypatch.setattr(llm_mod.requests, "post", _fake_post)
    monkeypatch.setattr(emb_mod.requests, "post", _fake_post)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)
    return captured_llm


def _insert_qa(
    pool: PgPool,
    *,
    source_id: str,
    session_id: str,
    turn_id: int,
    question: str,
    answer: str,
    ts: datetime,
) -> int:
    emb = _vec_literal(_EMBED_FN(f"{question}\n{answer}"))
    with pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            """
            INSERT INTO public.qa_pairs
                (source_id, session_id, turn_id, question, answer, timestamp, source, embedding, embed_model)
            VALUES (%s, %s, %s, %s, %s, %s::timestamptz, 'oepg_test', %s::vector, 'bge-m3')
            RETURNING id
            """,
            (source_id, session_id, turn_id, question, answer, ts.isoformat(), emb),
        )
        qa_id = int(cur.fetchone()[0])
        lease.connection.commit()
    return qa_id


def test_pg_observer_and_topic_v2_no_double_write_conflict(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dict = _test_pg_dict_config(tmp_path)
    _install_stubs(monkeypatch, cfg_dict)

    t0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    qa1 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}no_dw_1",
        session_id="sess_oepg_dw",
        turn_id=1,
        question="定时任务调度方案现在选型是什么？",
        answer="决定采用 Event-Scheduler-V2，废止轮询 Cron-Worker-V1。",
        ts=t0,
    )

    state_dir = tmp_path / "tv2_state"
    tv2_res = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
        backfill_history=True,
    )
    assert tv2_res["processed_qas"] == 1

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            "SELECT topic_id, title, body, status, updated_at FROM public.topics WHERE topic_id LIKE %s ORDER BY topic_id",
            (_TOPIC_PREFIX + "%",),
        )
        topics_before = cur.fetchall()
    assert len(topics_before) == 1
    existing_tid = topics_before[0][0]

    # Legacy M3 output attempts to overwrite the existing Topic V2 row and create a rogue topic.
    fake_note = (
        "## 关键决策（拍板的决策 + 理由）\n"
        f"- [{_MARKER}] 生产定时调度方案确定采用 Event-Scheduler-V2，彻底废止 Cron-Worker-V1 轮询机制以消除延迟抖动 [用户]〔已验证〕[#1]\n\n"
        "## 进行中状态（未完事项）\n"
        f"- [{_MARKER}] 各服务配置已完成切换并与 Topic V2 保持一致性校验〔已实施〕[#1]\n"
    )
    monkeypatch.setattr(
        observer_mod,
        "call_m3",
        lambda *a, **kw: {
            "text": json.dumps(
                {
                    "note": {"content": fake_note},
                    "topic_updates": [
                        {
                            "action": "update",
                            "topic_id": existing_tid,
                            "title": "ROGUE_OVERWRITE",
                            "facts": ["Observer attempted corrupt overwrite"],
                        },
                        {
                            "action": "create",
                            "title": "ROGUE_NEW_TOPIC",
                            "facts": ["Observer attempted topic creation"],
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            "usage": {"total_tokens": 120},
        },
    )

    obs_cfg = dict(cfg_dict["observer"])
    obs_cfg["basePath"] = str(tmp_path)
    observer_mod._save_cursor(qa1 - 1, obs_cfg)
    observer_mod._observe_worker(obs_cfg, "", dry_run=False, pool=pg_pool)

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            "SELECT topic_id, title, body, status, updated_at FROM public.topics WHERE topic_id LIKE %s ORDER BY topic_id",
            (_TOPIC_PREFIX + "%",),
        )
        topics_after = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM public.topic_entries WHERE topic_id = %s", (existing_tid,))
        entry_count = int(cur.fetchone()[0])
        cur.execute(
            "SELECT id, content, embedding IS NOT NULL, upper(source_qa_range) FROM public.observation_notes WHERE content LIKE %s",
            ("%" + _MARKER + "%",),
        )
        note_rows = cur.fetchall()

    assert topics_after == topics_before
    assert entry_count == 0
    assert len(note_rows) == 1
    assert note_rows[0][2] is True
    assert int(note_rows[0][3]) == qa1 + 1


def test_pg_e1_synthesize_yin_via_gateway_reads_clean_topics_and_never_mutates(
    pg_pool: PgPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dict = _test_pg_dict_config(tmp_path, provider="deepseek", model="deepseek-chat")
    valid_identity = (
        f"我是一个稳健的工程记忆协同助手（{_MARKER}），遵循只说已验证事实的原则，"
        "确保主题文档与连续身份快照互不越权修改。"
    )
    valid_situation = (
        "## 活跃主题\n"
        f"系统当前处于 Topic V2 单写者模式，调度器升级方案已完成收敛（{_MARKER}）。\n\n"
        "## 当前关注与待办\n"
        "验证多模型网关下 E1 态势生成与观察者快照压缩闭环。\n\n"
        "## 状态速记\n"
        "PostgreSQL 隔离验收与契约检查均处于活跃执行阶段。\n"
    )
    valid_yin = f"# 连续人格印（{_MARKER}）\n\n当前确认调度方案为 Event-Scheduler-V2，所有主题写权限归 Topic V2。"

    assert validate_identity_block(valid_identity) == valid_identity.strip()
    validate_situation_overview(valid_situation)

    seen_prompts: list[str] = []

    def _responder(_url: str, body: dict[str, Any]) -> dict[str, Any]:
        msgs = body.get("messages") or []
        sys_txt = " ".join(str(m.get("content", "")) for m in msgs if m.get("role") == "system")
        usr_txt = " ".join(str(m.get("content", "")) for m in msgs if m.get("role") != "system")
        seen_prompts.append(usr_txt)
        assert "<!-- topic_v2:" not in usr_txt
        if "自我简述" in sys_txt or "identity" in sys_txt.lower():
            return {"choices": [{"message": {"content": valid_identity}}]}
        if "系统态势总览" in sys_txt or "活跃主题" in sys_txt:
            return {"choices": [{"message": {"content": valid_situation}}]}
        return {"choices": [{"message": {"content": valid_yin}}]}

    captured_llm = _install_stubs(monkeypatch, cfg_dict, llm_responder=_responder)

    t1 = f"{_EXTRA_TOPIC_PREFIX}dup_1"
    t2 = f"{_EXTRA_TOPIC_PREFIX}dup_2"
    emb_dup = _vec_literal(_EMBED_FN("定时任务 Event-Scheduler-V2 调度器配置升级"))
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        for tid, suffix in ((t1, "A"), (t2, "B")):
            body = (
                f'<!-- topic_v2:{{"version":2,"kind":"dossier","tag":"{suffix}"}} -->\n'
                f"## 当前结论\n定时调度系统已完成向 Event-Scheduler-V2 的切换并淘汰旧版轮询（{suffix}）。"
            )
            cur.execute(
                """
                INSERT INTO public.topics (topic_id, title, body, embedding, embed_model, status, created_at)
                VALUES (%s, %s, %s, %s::vector, 'bge-m3', 'active', NOW())
                """,
                (tid, f"调度器升级-{suffix}", body, emb_dup),
            )
        cur.execute(
            """
            INSERT INTO public.observation_notes (version, content, embedding, embed_model, created_at)
            VALUES ('v1', %s, %s::vector, 'bge-m3', NOW())
            """,
            (
                f"[{_MARKER}] 观察快照：Event-Scheduler-V2 调度稳定性验证通过，旧方案不再使用。",
                _vec_literal(_EMBED_FN("观察快照：Event-Scheduler-V2")),
            ),
        )
        cur.execute(
            "SELECT topic_id, title, body, status, updated_at FROM public.topics WHERE topic_id IN (%s, %s) ORDER BY topic_id",
            (t1, t2),
        )
        topics_before = cur.fetchall()
        lease.connection.commit()

    res = e1_mod.synthesize_yin(from_legacy_dict(cfg_dict), dry_run=False, pool=pg_pool)
    assert "E1 完成" in res or "y_" in res
    assert len(captured_llm) == 3
    assert all(req["url"] == "https://api.deepseek.com/v1/chat/completions" for req in captured_llm)

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            "SELECT topic_id, title, body, status, updated_at FROM public.topics WHERE topic_id IN (%s, %s) ORDER BY topic_id",
            (t1, t2),
        )
        topics_after = cur.fetchall()
        cur.execute(
            "SELECT is_current, content FROM public.yin WHERE content LIKE %s",
            ("%" + _MARKER + "%",),
        )
        yin_rows = cur.fetchall()
        cur.execute(
            "SELECT COUNT(*) FROM public.yin_history WHERE content LIKE %s",
            ("%" + _MARKER + "%",),
        )
        yin_hist_count = int(cur.fetchone()[0])

    assert topics_after == topics_before
    assert len(yin_rows) == 1 and yin_rows[0][0] is True
    assert yin_hist_count >= 1
    assert (tmp_path / "identity_block.md").exists()
    assert (tmp_path / "situation_overview.md").exists()


def test_pg_e1_compress_observer_note_via_gateway_atomic(
    pg_pool: PgPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dict = _test_pg_dict_config(tmp_path, provider="deepseek", model="deepseek-chat")
    compressed_text = (
        f"[{_MARKER}] 压缩后观察者印：系统围绕定时调度与日志采集完成了架构收敛。"
        + ("核心迁移结论保持一致，旧轮询方案已废止。" * 35)
    )
    assert 500 <= len(compressed_text) <= 8000

    captured_llm = _install_stubs(
        monkeypatch,
        cfg_dict,
        llm_responder=lambda _u, _b: {"choices": [{"message": {"content": compressed_text}}]},
    )

    long_note = f"[{_MARKER}] 原始观察长快照：\n" + ("定时调度架构与参数审计记录明细。" * 320)
    assert len(long_note) >= 4500

    topic_id = f"{_EXTRA_TOPIC_PREFIX}compress_guard"
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            """
            INSERT INTO public.topics (topic_id, title, body, status, created_at)
            VALUES (%s, '压缩守护主题', '话题内容不应被压缩路径修改。', 'active', NOW())
            """,
            (topic_id,),
        )
        cur.execute(
            """
            INSERT INTO public.observation_notes (version, content, embedding, embed_model, created_at)
            VALUES ('v1', %s, %s::vector, 'bge-m3', NOW())
            RETURNING id
            """,
            (long_note, _vec_literal(_EMBED_FN(long_note))),
        )
        orig_id = int(cur.fetchone()[0])
        lease.connection.commit()

    out = e1_mod.compress_observer_note_strict(
        from_legacy_dict(cfg_dict),
        notes_table="observation_notes",
        target_note_id=orig_id,
        pool=pg_pool,
    )
    assert out["accepted"] is True
    assert out["source_id"] == orig_id
    new_id = int(out["new_id"])
    assert len(captured_llm) == 1
    assert captured_llm[0]["url"] == "https://api.deepseek.com/v1/chat/completions"

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            "SELECT version, content, embedding IS NOT NULL FROM public.observation_notes WHERE id IN (%s, %s) ORDER BY id",
            (orig_id, new_id),
        )
        rows = cur.fetchall()
        cur.execute("SELECT title, body, status FROM public.topics WHERE topic_id = %s", (topic_id,))
        topic_row = cur.fetchone()

    assert len(rows) == 2
    assert rows[0][0] == "v1" and rows[0][1] == long_note
    assert rows[1][0] == "v1-compressed" and rows[1][2] is True
    assert topic_row == ("压缩守护主题", "话题内容不应被压缩路径修改。", "active")


def test_pg_full_pipeline_topic_v2_observer_e1_freshness_preserved(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dict = _test_pg_dict_config(tmp_path, provider="deepseek", model="deepseek-chat")
    valid_identity = f"我是工程研发助手（{_MARKER}），始终区分活跃结论与历史已废弃方案。"
    valid_situation = (
        "## 活跃主题\n"
        f"定时任务架构已演进至 Event-Scheduler-V2，旧方案 Cron-Worker-V1 已淘汰（{_MARKER}）。\n\n"
        "## 当前关注与待办\n"
        "确认 Topic V2、Observer、E1 全链路后召回排序保持最新结论领先。\n\n"
        "## 状态速记\n"
        "默认召回保鲜机制生效。\n"
    )
    valid_yin = f"# 连续人格印（{_MARKER}）\n\n已知定时任务方案经历从 Cron-Worker-V1 到 Event-Scheduler-V2 的演进。"

    def _responder(_url: str, body: dict[str, Any]) -> dict[str, Any]:
        msgs = body.get("messages") or []
        sys_txt = " ".join(str(m.get("content", "")) for m in msgs if m.get("role") == "system")
        if "自我简述" in sys_txt:
            return {"choices": [{"message": {"content": valid_identity}}]}
        if "系统态势总览" in sys_txt:
            return {"choices": [{"message": {"content": valid_situation}}]}
        return {"choices": [{"message": {"content": valid_yin}}]}

    _install_stubs(monkeypatch, cfg_dict, llm_responder=_responder)

    t1 = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(hours=3)
    qa1 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}pipe_1",
        session_id="sess_pipe_1",
        turn_id=1,
        question="定时任务架构最初采用什么？",
        answer="早期暂定使用 Cron-Worker-V1 进行轮询调度。",
        ts=t1,
    )
    qa2 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}pipe_2",
        session_id="sess_pipe_2",
        turn_id=1,
        question="定时任务架构后来怎么调整的？",
        answer="正式废弃 Cron-Worker-V1（轮询延迟高），全面升级为 Event-Scheduler-V2。",
        ts=t2,
    )

    state_dir = tmp_path / "tv2_state"
    tv2_out = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
        backfill_history=True,
    )
    assert tv2_out["processed_qas"] == 2

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("SELECT topic_id, title, body, status, updated_at FROM public.topics WHERE topic_id LIKE %s", (_TOPIC_PREFIX + "%",))
        topics_before = cur.fetchall()
    assert len(topics_before) == 1
    fresh_topic_id = topics_before[0][0]

    obs_note = (
        "## 关键决策（拍板的决策 + 理由）\n"
        f"- [{_MARKER}] 早期曾暂定使用 Cron-Worker-V1 进行轮询调度〔已推翻〕[#1]\n"
        f"- [{_MARKER}] 正式废弃 Cron-Worker-V1，全面升级为 Event-Scheduler-V2 并停用旧方案 [用户]〔已验证〕[#2]\n\n"
        "## 进行中状态（未完事项）\n"
        f"- [{_MARKER}] 定时任务架构改造与默认召回保鲜链路验证〔已验证〕[#2]\n"
    )
    monkeypatch.setattr(
        observer_mod,
        "call_m3",
        lambda *a, **kw: {
            "text": json.dumps(
                {
                    "note": {"content": obs_note},
                    "topic_updates": [{"action": "update", "topic_id": fresh_topic_id, "title": "OVERWRITE_TEST"}],
                },
                ensure_ascii=False,
            ),
            "usage": {"total_tokens": 90},
        },
    )
    obs_cfg = dict(cfg_dict["observer"])
    obs_cfg["basePath"] = str(tmp_path)
    observer_mod._save_cursor(qa1 - 1, obs_cfg)
    observer_mod._observe_worker(obs_cfg, "", dry_run=False, pool=pg_pool)

    e1_mod.synthesize_yin(from_legacy_dict(cfg_dict), dry_run=False, pool=pg_pool)

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("SELECT topic_id, title, body, status, updated_at FROM public.topics WHERE topic_id LIKE %s", (_TOPIC_PREFIX + "%",))
        topics_after = cur.fetchall()
        cur.execute("SELECT id FROM public.observation_notes WHERE content LIKE %s", ("%" + _MARKER + "%",))
        note_id = int(cur.fetchone()[0])

    assert topics_after == topics_before

    query = "现在定时任务是用什么方案？"
    hits = prefetch(
        query,
        limit=8,
        config=cfg_dict,
        pg=pg_store,
        q_emb=_EMBED_FN(query),
        max_chars=4000,
    )
    ranked_ids = [h["source_id"] for h in hits]
    assert all("<!-- topic_v2:" not in str(h.get("text", "")) for h in hits)

    active_qa_sid = f"qa_{qa2}"
    fresh_topic_sid = f"topic_{fresh_topic_id}"
    stale_qa_sid = f"qa_{qa1}"
    obs_note_sid = "note:v1"
    assert active_qa_sid in ranked_ids and fresh_topic_sid in ranked_ids
    # Active QA, Topic V2 dossier, and the post-evolution observation note (created AFTER qa2
    # and carrying Event-Scheduler-V2) must all outrank the superseded raw QA.
    for active in (active_qa_sid, fresh_topic_sid, obs_note_sid):
        if active in ranked_ids and stale_qa_sid in ranked_ids:
            assert ranked_ids.index(active) < ranked_ids.index(stale_qa_sid), f"{active} vs {stale_qa_sid}: {ranked_ids}"
