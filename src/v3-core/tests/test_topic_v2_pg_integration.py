# -*- coding: utf-8 -*-
"""Isolated real PostgreSQL + pgvector acceptance tests for Topic V2.

Opt-in gate:
    Runs ONLY when ``HIPPOCAMPUS_TOPIC_V2_TEST_DSN`` is set and points to a
    loopback, non-production (port != 5433, dbname != 'v3embeddings') database.
    When unset (e.g. default no-PG unit suite), skips cleanly at collection time.
    In GitHub Actions CI, the ``isolated-pg-topic-v2`` job provisions an
    ephemeral ``pgvector/pgvector:pg16`` container, runs ``bootstrap_alpha_db.py``
    twice for idempotency, and executes this full suite.
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

_TEST_DSN = os.environ.get("HIPPOCAMPUS_TOPIC_V2_TEST_DSN", "").strip()
if not _TEST_DSN:
    pytest.skip(
        "HIPPOCAMPUS_TOPIC_V2_TEST_DSN not set — opt-in isolated-PG Topic V2 acceptance skipped",
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
from v3core.config_model import PrefetchConfig, from_legacy_dict  # noqa: E402
from v3core.pg_pool import PgPool  # noqa: E402
from v3core.pg_store import PgEmbedStore  # noqa: E402
from v3core.prefetch import prefetch, prefetch_to_context_block  # noqa: E402
from v3core.tools.topic_maintain_tool import handle_v3_topic_maintain  # noqa: E402
from v3core.topic_v2 import (  # noqa: E402
    consolidate_pg_incremental,
    parse_topic_v2_header,
    strip_topic_v2_header,
)

_QA_PREFIX = "tv2_pg_qa_"
_TOPIC_PREFIX = "tv2_"
_LEGACY_TOPIC_ID = "tv2_pg_legacy_topic"
_NOTE_PREFIX = "tv2_pg_note_"

_THEME_KEYWORDS: list[tuple[int, tuple[str, ...]]] = [
    (0, ("定时", "任务", "调度", "cron", "scheduler", "worker", "event-scheduler")),
    (1, ("日志", "采集", "fluentd", "vector", "logging", "loki")),
    (2, ("数据库", "连接池", "pgbouncer", "postgres", "pg", "sql")),
    (3, ("鉴权", "认证", "jwt", "oauth", "auth", "token")),
    (4, ("部署", "容器", "k8s", "helm", "deploy", "release")),
]


def _make_1024d_embed_fn() -> Callable[[str], list[float]]:
    """Deterministic 1024-dim unit vector generator for pgvector(1024) cosine search."""

    def _embed(text: str) -> list[float]:
        low = str(text or "").lower()
        vec = [0.0] * 1024
        matched_bucket = False
        for bucket, kws in _THEME_KEYWORDS:
            if any(k in low for k in kws):
                base = bucket * 32
                for j in range(16):
                    vec[base + j] += 2.0
                matched_bucket = True
        if not matched_bucket:
            vec[512] = 1.0
        # Fine-grained char trigram features in dimensions 600..1023
        padded = f"^{low}$"
        for i in range(max(len(padded) - 2, 1)):
            tri = padded[i : i + 3].encode("utf-8", errors="ignore")
            h = int.from_bytes(hashlib.blake2b(tri, digest_size=4).digest(), "little")
            idx = 600 + (h % 424)
            vec[idx] += 0.25
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [round(v / norm, 6) for v in vec]

    return _embed


_EMBED_FN = _make_1024d_embed_fn()


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(str(x) for x in vec) + "]"


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


def _default_pg_dict_config(base_path: Path) -> dict[str, Any]:
    """Production-shaped config dict WITHOUT explicit prefetch.topic_v2_mode."""
    return {
        "basePath": str(base_path),
        "storage": {
            "pg": _pg_kwargs_from_dsn(_DSN_PARTS),
            "embed": {
                "endpoint": "http://127.0.0.1:0/v1/embeddings",
                "model": "bge-m3",
                "_fingerprint": "tv2-pg-test",
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
            "DELETE FROM public.topics WHERE topic_id LIKE %s OR topic_id = %s",
            (_TOPIC_PREFIX + "%", _LEGACY_TOPIC_ID),
        )
        cur.execute("DELETE FROM public.topic_entries WHERE topic_id LIKE %s", (_TOPIC_PREFIX + "%",))
        cur.execute("DELETE FROM public.observation_notes WHERE version LIKE %s", (_NOTE_PREFIX + "%",))
        lease.connection.commit()


@pytest.fixture(autouse=True)
def _clean_each_test(pg_pool: PgPool, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(v3_config, "_resolve_data_dir", lambda cfg=None: tmp_path)
    _cleanup_pg_rows(pg_pool)
    yield
    _cleanup_pg_rows(pg_pool)


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
            VALUES (%s, %s, %s, %s, %s, %s::timestamptz, 'tv2_pg_test', %s::vector, 'bge-m3')
            RETURNING id
            """,
            (source_id, session_id, turn_id, question, answer, ts.isoformat(), emb),
        )
        qa_id = int(cur.fetchone()[0])
        lease.connection.commit()
    return qa_id


def test_pg_default_config_and_dry_run_makes_zero_writes(pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path):
    assert PrefetchConfig().topic_v2_mode is True
    assert from_legacy_dict({}).prefetch.topic_v2_mode is True

    t0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}dry_1",
        session_id="sess_dry",
        turn_id=1,
        question="定时任务调度采用什么方案？",
        answer="当前采用 Cron-Worker-V1。",
        ts=t0,
    )

    state_dir = tmp_path / "state_dry"
    res = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=True,
    )
    assert res["dry_run"] is True
    assert res["processed_qas"] == 1
    assert res["upserted_topics"] == 0
    assert not (state_dir / "topic_v2_state.json").exists()

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("SELECT COUNT(*) FROM public.topics WHERE topic_id LIKE %s", (_TOPIC_PREFIX + "%",))
        assert int(cur.fetchone()[0]) == 0


def test_pg_incremental_consolidation_idempotency_and_source_preservation(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path
):
    t0 = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    qa1 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}idem_1",
        session_id="sess_idem",
        turn_id=1,
        question="数据库连接池选型是什么？",
        answer="使用 PgBouncer-Pool-V1 作为连接池。",
        ts=t0,
    )
    state_dir = tmp_path / "state_idem"

    res1 = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
    )
    assert res1["processed_qas"] == 1
    assert res1["created_topics"] == 1
    assert res1["upserted_topics"] == 1
    assert res1["new_last_qa_id"] == qa1

    # Immediate re-run must be a zero-op idempotent pass.
    res2 = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
    )
    assert res2["processed_qas"] == 0
    assert res2["upserted_topics"] == 0

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("SELECT question, answer FROM public.qa_pairs WHERE id = %s", (qa1,))
        row_qa = cur.fetchone()
        assert row_qa == ("数据库连接池选型是什么？", "使用 PgBouncer-Pool-V1 作为连接池。")

        cur.execute(
            "SELECT topic_id, body, embedding::text FROM public.topics WHERE topic_id LIKE %s",
            (_TOPIC_PREFIX + "%",),
        )
        topic_rows = cur.fetchall()
        assert len(topic_rows) == 1
        meta = parse_topic_v2_header(topic_rows[0][1])
        assert meta is not None and meta["last_qa"] == qa1
        assert topic_rows[0][2] and topic_rows[0][2].startswith("[")


def test_pg_a_to_b_evolution_and_default_prefetch_freshness_guard(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path
):
    t0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=1)
    t2 = t0 + timedelta(days=10)

    qa1 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}ab_1",
        session_id="sess_ab_1",
        turn_id=1,
        question="定时任务模块现在用什么？",
        answer="先使用 Cron-Worker-V1 跑定时任务；因为简单所以暂用它。",
        ts=t0,
    )

    state_dir = tmp_path / "state_ab"
    first = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
    )
    assert first["created_topics"] == 1

    # Seed an older observation note + a stale unheadered legacy topic card referencing A.
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            """
            INSERT INTO public.observation_notes (version, content, embedding, embed_model, created_at)
            VALUES (%s, %s, %s::vector, 'bge-m3', %s::timestamptz)
            """,
            (
                f"{_NOTE_PREFIX}1",
                "历史观察：定时任务目前采用 Cron-Worker-V1 方案。",
                _vec_literal(_EMBED_FN("定时任务 Cron-Worker-V1")),
                t1.isoformat(),
            ),
        )
        cur.execute(
            """
            INSERT INTO public.topics (topic_id, title, summary, body, keywords, status, embedding)
            VALUES (%s, %s, %s, %s, %s, 'active', %s::vector)
            """,
            (
                _LEGACY_TOPIC_ID,
                "定时任务旧卡片",
                "旧方案是 Cron-Worker-V1",
                "定时任务目前使用 Cron-Worker-V1 执行轮询。",
                ["定时任务", "Cron-Worker-V1"],
                _vec_literal(_EMBED_FN("定时任务 Cron-Worker-V1")),
            ),
        )
        lease.connection.commit()

    # Session 2 evolves A -> B
    qa2 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}ab_2",
        session_id="sess_ab_2",
        turn_id=1,
        question="定时任务架构是不是改了？以前为什么放弃 Cron-Worker-V1？",
        answer="正式废弃 Cron-Worker-V1（以前轮询延迟太高），改为 Event-Scheduler-V2。",
        ts=t2,
    )

    second = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
    )
    assert second["processed_qas"] == 1
    assert second["superseded_claims"] >= 1
    assert second["new_last_qa_id"] == qa2

    # Check raw qa_pairs untouched & dossiers updated
    dossiers_payload = json.loads((state_dir / "topic_v2_dossiers.json").read_text(encoding="utf-8"))
    dossiers_list = dossiers_payload.get("topics") if isinstance(dossiers_payload, dict) else dossiers_payload
    assert len(dossiers_list) == 1
    dossier = dossiers_list[0]
    assert len(dossier["superseded_claims"]) >= 1
    assert any("Event-Scheduler-V2" in c["text"] for c in dossier["current_claims"])

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("SELECT COUNT(*) FROM public.qa_pairs WHERE id IN (%s, %s)", (qa1, qa2))
        assert int(cur.fetchone()[0]) == 2

    # Real PG recall via default config (no explicit topic_v2_mode flag!)
    cfg = _default_pg_dict_config(tmp_path)
    cur_query = "现在定时任务是用什么方案？"
    hits_cur = prefetch(
        cur_query,
        limit=6,
        config=cfg,
        pg=pg_store,
        q_emb=_EMBED_FN(cur_query),
        max_chars=4000,
    )
    ranked_ids = [h["source_id"] for h in hits_cur]
    fresh_topic_sid = f"topic_{dossier['topic_id']}"
    legacy_topic_sid = f"topic_{_LEGACY_TOPIC_ID}"
    superseded_qa_sid = f"qa_{qa1}"
    current_qa_sid = f"qa_{qa2}"
    assert current_qa_sid in ranked_ids and fresh_topic_sid in ranked_ids
    for active_sid in (current_qa_sid, fresh_topic_sid):
        for stale_sid in (superseded_qa_sid, legacy_topic_sid, f"note:{_NOTE_PREFIX}1"):
            if stale_sid in ranked_ids:
                assert ranked_ids.index(active_sid) < ranked_ids.index(stale_sid), (
                    f"{active_sid} must outrank {stale_sid}: {ranked_ids}"
                )

    ctx_cur = prefetch_to_context_block(
        cur_query,
        limit=6,
        config=cfg,
        pg=pg_store,
        q_emb=_EMBED_FN(cur_query),
        max_chars=4000,
    )
    assert "<!-- topic_v2:" not in ctx_cur
    assert "Event-Scheduler-V2" in ctx_cur
    assert ctx_cur.index(current_qa_sid) < ctx_cur.index(fresh_topic_sid)
    if legacy_topic_sid in ctx_cur:
        assert ctx_cur.index(fresh_topic_sid) < ctx_cur.index(legacy_topic_sid)

    # Historical query must preserve superseded raw QA evidence.
    hist_query = "以前为什么放弃 Cron-Worker-V1？"
    ctx_hist = prefetch_to_context_block(
        hist_query,
        limit=6,
        config=cfg,
        pg=pg_store,
        q_emb=_EMBED_FN(hist_query),
        max_chars=4000,
    )
    assert "<!-- topic_v2:" not in ctx_hist
    assert "Cron-Worker-V1" in ctx_hist


def test_pg_v3core_live_flush_and_topic_maintain_tool(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path, monkeypatch
):
    t0 = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    qa1 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}flush_1",
        session_id="sess_flush",
        turn_id=1,
        question="日志采集方案现在采用什么？",
        answer="正式废弃 Fluentd-Legacy，改为 Vector-Agent-V2。",
        ts=t0,
    )

    core = v3core.V3Core.__new__(v3core.V3Core)
    core._config = from_legacy_dict({"basePath": str(tmp_path)})
    core._pg = pg_store
    invalidations = []
    monkeypatch.setattr(core, "_notify_topic_recall_invalidation", lambda: invalidations.append(1))
    monkeypatch.setattr(v3core, "safe_embed_cfg", lambda cfg: {"endpoint": "http://fake", "_fingerprint": "fp"})
    monkeypatch.setattr(v3core, "call_embedding", lambda text, cfg, **kw: _EMBED_FN(text))

    core._on_live_flush([("sess_flush", "m1", "msg", "user")])
    assert len(invalidations) == 1

    # Add a second QA and trigger via v3_topic_maintain tool handler
    qa2 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}flush_2",
        session_id="sess_flush_2",
        turn_id=1,
        question="鉴权方案用什么实现？",
        answer="使用 OAuth2-PKCE-V2 替代旧的 Static-Token。",
        ts=t0 + timedelta(hours=2),
    )
    tool_dry = json.loads(
        handle_v3_topic_maintain(
            {"dry_run": True},
            pool=pg_store,
            core=core,
            effective_config={"basePath": str(tmp_path)},
            embed_fn=_EMBED_FN,
            llm_fn=None,
        )
    )
    assert tool_dry["engine"] == "topic_v2"
    assert tool_dry["dry_run"] is True
    assert tool_dry["processed_qas"] == 1

    tool_apply = json.loads(
        handle_v3_topic_maintain(
            {"dry_run": False},
            pool=pg_store,
            core=core,
            effective_config={"basePath": str(tmp_path)},
            embed_fn=_EMBED_FN,
            llm_fn=None,
        )
    )
    assert tool_apply["engine"] == "topic_v2"
    assert tool_apply["processed_qas"] == 1
    assert tool_apply["new_last_qa_id"] == qa2
    assert len(invalidations) == 2


def test_pg_60qa_multi_topic_stress_no_fragmentation(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path
):
    themes = [
        ("定时任务", "Cron-Worker-V1", "Event-Scheduler-V2"),
        ("日志采集", "Fluentd-Legacy", "Vector-Agent-V2"),
        ("数据库连接池", "Raw-Conn-V1", "PgBouncer-Pool-V2"),
        ("鉴权认证", "Static-Key-V1", "OAuth2-PKCE-V2"),
        ("容器部署", "Manual-SSH-V1", "Helm-GitOps-V2"),
    ]
    base_ts = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    max_qa = 0
    idx = 0
    for round_no in range(12):
        for theme_idx, (label, old_tech, new_tech) in enumerate(themes):
            idx += 1
            if round_no < 4:
                ans = f"当前 {label} 使用 {old_tech} 运行第 {round_no + 1} 阶段。"
            elif round_no == 4:
                ans = f"正式废弃 {old_tech}，{label} 改用 {new_tech}。"
            else:
                ans = f"{label} 继续稳定运行 {new_tech}，补充第 {round_no + 1} 次变更确认。"
            max_qa = _insert_qa(
                pg_pool,
                source_id=f"{_QA_PREFIX}stress_{idx}",
                session_id=f"sess_stress_{round_no}_{theme_idx}",
                turn_id=1,
                question=f"{label} 模块第 {round_no + 1} 轮采用什么方案？",
                answer=ans,
                ts=base_ts + timedelta(days=round_no, minutes=theme_idx),
            )

    assert idx == 60
    state_dir = tmp_path / "state_stress"
    for _ in range(3):
        consolidate_pg_incremental(
            pg_store,
            embed_fn=_EMBED_FN,
            llm_fn=None,
            state_dir=state_dir,
            dry_run=False,
            batch_limit=20,
        )

    state = json.loads((state_dir / "topic_v2_state.json").read_text(encoding="utf-8"))
    assert int(state["last_qa_id"]) == max_qa

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("SELECT COUNT(*) FROM public.qa_pairs WHERE source_id LIKE %s", (f"{_QA_PREFIX}stress_%",))
        assert int(cur.fetchone()[0]) == 60

        cur.execute(
            "SELECT topic_id, body FROM public.topics WHERE topic_id LIKE %s AND status='active'",
            (_TOPIC_PREFIX + "%",),
        )
        topic_rows = cur.fetchall()
        # 5 distinct themes across 60 turns must remain compact (no fragmentation explosion).
        assert 1 <= len(topic_rows) <= 10
        assert all(parse_topic_v2_header(row[1]) is not None for row in topic_rows)
        clean_joined = "\n".join(strip_topic_v2_header(row[1]) for row in topic_rows)
        for _, _, new_tech in themes:
            assert new_tech in clean_joined

    # Spot-check default recall on each theme returns latest technology without internal headers
    cfg = _default_pg_dict_config(tmp_path)
    for label, _, new_tech in themes:
        q = f"现在 {label} 用什么方案？"
        ctx = prefetch_to_context_block(
            q,
            limit=5,
            config=cfg,
            pg=pg_store,
            q_emb=_EMBED_FN(q),
            max_chars=4000,
        )
        assert "<!-- topic_v2:" not in ctx
        assert new_tech in ctx
