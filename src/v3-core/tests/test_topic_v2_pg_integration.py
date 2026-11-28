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
    import psycopg2 as _psycopg2

    fn = getattr(_psycopg2, "connect", None)
    if callable(fn) and getattr(fn, "__module__", "").startswith("psycopg2"):
        return fn
    try:
        from psycopg2._psycopg import _connect as _raw_connect  # type: ignore

        def _compat_connect(dsn: str = "", **kwargs: Any):
            if dsn:
                return _raw_connect(dsn)
            parts = []
            for k, v in kwargs.items():
                if v is not None and v != "":
                    key = "dbname" if k == "database" else k
                    parts.append(f"{key}={v}")
            return _raw_connect(" ".join(parts))

        return _compat_connect
    except Exception:
        pass
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
    cfg = from_legacy_dict({"storage": {"pg": _pg_kwargs_from_dsn(_DSN_PARTS)}})
    return PgEmbedStore(config=cfg, pool=pg_pool)


def _cleanup_pg_rows(pool: PgPool) -> None:
    with pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("DELETE FROM public.qa_pairs WHERE source_id LIKE %s", (_QA_PREFIX + "%",))
        cur.execute(
            "DELETE FROM public.topics WHERE topic_id LIKE %s OR topic_id = %s",
            (_TOPIC_PREFIX + "%", _LEGACY_TOPIC_ID),
        )
        cur.execute("DELETE FROM public.topic_entries WHERE topic_id LIKE %s", (_TOPIC_PREFIX + "%",))
        cur.execute(
            "DELETE FROM public.topic_claim_evidence WHERE topic_id LIKE %s OR topic_id = %s",
            (_TOPIC_PREFIX + "%", _LEGACY_TOPIC_ID),
        )
        cur.execute(
            "DELETE FROM public.topic_claims WHERE topic_id LIKE %s OR topic_id = %s",
            (_TOPIC_PREFIX + "%", _LEGACY_TOPIC_ID),
        )
        cur.execute("DELETE FROM public.topic_v2_state")
        cur.execute("DELETE FROM public.observation_notes WHERE version LIKE %s", (_NOTE_PREFIX + "%",))
        cur.execute("DELETE FROM public.yin WHERE version LIKE %s", (_NOTE_PREFIX + "%",))
        cur.execute("DELETE FROM public.yin_paragraphs WHERE yin_version LIKE %s", (_NOTE_PREFIX + "%",))
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
    session_id: str | None,
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
        backfill_history=True,
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
        backfill_history=True,
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
        backfill_history=True,
    )
    assert first["created_topics"] == 1

    # Seed an older observation note + a stale unheadered legacy topic card referencing A.
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            """
            INSERT INTO public.observation_notes (version, content, source_qa_range, embedding, embed_model, created_at)
            VALUES (%s, %s, int8range(%s, %s), %s::vector, 'bge-m3', %s::timestamptz)
            """,
            (
                f"{_NOTE_PREFIX}1",
                "历史观察：定时任务目前采用 Cron-Worker-V1 方案。",
                qa1,
                qa1 + 1,
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
    core._config = from_legacy_dict({"basePath": str(tmp_path), "prefetch": {"topic_v2_backfill_history": True}})
    core._pg = pg_store
    invalidations = []
    monkeypatch.setattr(core, "_notify_topic_recall_invalidation", lambda: invalidations.append(1))
    monkeypatch.setattr(v3core, "safe_embed_cfg", lambda cfg: {"endpoint": "http://fake", "_fingerprint": "fp"})
    monkeypatch.setattr(v3core, "call_embedding", lambda text, cfg, **kw: _EMBED_FN(text))

    core._on_live_flush([("sess_flush", "m1", "msg", "user")])
    assert core.wait_topic_v2_idle(timeout=5.0)
    core._stop_topic_v2_worker(timeout=1.0)
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
            backfill_history=True,
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


def test_pg_p2_null_session_id_and_embed_model_updated_at(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path
):
    t0 = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
    qa_null = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}null_sess_1",
        session_id=None,
        turn_id=1,
        question="数据库连接池采用什么？",
        answer="PostgreSQL 确定采用 PgBouncer 连接池方案。",
        ts=t0,
    )
    state_dir = tmp_path / "null_sess_state"
    res1 = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
        embed_model="fp-alpha-v1",
        backfill_history=True,
    )
    assert res1["processed_qas"] == 1
    assert res1["new_last_qa_id"] == qa_null
    assert res1["upserted_topics"] == 1

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            "SELECT embed_model, updated_at FROM public.topics WHERE topic_id LIKE %s",
            (_TOPIC_PREFIX + "%",),
        )
        row1 = cur.fetchone()
    assert row1 is not None and row1[0] == "fp-alpha-v1" and row1[1] is not None

    res2 = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
        embed_model="fp-beta-v2",
    )
    assert res2["upserted_topics"] == 1
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            "SELECT embed_model, updated_at FROM public.topics WHERE topic_id LIKE %s",
            (_TOPIC_PREFIX + "%",),
        )
        row2 = cur.fetchone()
    assert row2 is not None and row2[0] == "fp-beta-v2" and row2[1] >= row1[1]


def test_pg_p1_cross_topic_unrelated_yin_and_failed_qa_recovery(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path
):
    t_db = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
    t_log = datetime(2026, 11, 26, 8, 0, tzinfo=timezone.utc)
    qa_db = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}cross_db",
        session_id="sess_cross_1",
        turn_id=1,
        question="数据库连接池采用什么方案？",
        answer="PostgreSQL 连接池统一采用 PgBouncer-Pool-V2。",
        ts=t_db,
    )
    qa_log = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}cross_log",
        session_id="sess_cross_2",
        turn_id=1,
        question="日志采集管线目前决定采用什么？",
        answer="正式废弃 Fluentd-Legacy，改为 Vector-Agent-V2。",
        ts=t_log,
    )

    state_dir = tmp_path / "cross_state"
    attempts = {"log": 0}

    def flaky_llm(messages):
        payload = json.loads(messages[-1]["content"])
        qid = payload["qa_id"]
        if qid == qa_db:
            return json.dumps(
                {
                    "action": "create",
                    "topic_title": "数据库连接池",
                    "update_type": "add",
                    "claim_text": "PostgreSQL 连接池采用 PgBouncer-Pool-V2",
                    "keywords": ["PgBouncer-Pool-V2", "postgres"],
                }
            )
        attempts["log"] += 1
        if attempts["log"] == 1:
            return "NOT VALID JSON"
        return json.dumps(
            {
                "action": "create",
                "topic_title": "日志采集管线",
                "update_type": "add",
                "claim_text": "日志采集采用 Vector-Agent-V2",
                "keywords": ["Vector-Agent-V2", "logging"],
            }
        )

    out1 = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=flaky_llm,
        state_dir=state_dir,
        dry_run=False,
        backfill_history=True,
    )
    assert out1["new_last_qa_id"] == qa_db
    assert out1["failed_qa_ids"] == [qa_log]

    out2 = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=flaky_llm,
        state_dir=state_dir,
        dry_run=False,
    )
    assert out2["new_last_qa_id"] == qa_log
    assert out2["failed_qa_ids"] == []

    # Seed unrelated long-term yin & a stale DB observation note
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            "INSERT INTO public.yin (version, title, content, is_current) VALUES (%s, %s, %s, TRUE)",
            (f"{_NOTE_PREFIX}yin_style", "沟通风格", "回答保持简明扼要，避免空话与冗余表格。"),
        )
        cur.execute(
            """
            INSERT INTO public.observation_notes (version, content, embedding, embed_model, created_at)
            VALUES (%s, %s, %s::vector, 'bge-m3', %s::timestamptz)
            """,
            (
                f"{_NOTE_PREFIX}stale_db",
                "早期观察：PostgreSQL 暂未使用 PgBouncer-Pool-V2。",
                _vec_literal(_EMBED_FN("PostgreSQL PgBouncer-Pool-V2 数据库连接池")),
                (t_db - timedelta(days=5)).isoformat(),
            ),
        )
        lease.connection.commit()

    cfg = _default_pg_dict_config(tmp_path)
    q_db = "PostgreSQL PgBouncer 连接池方案是什么？"
    hits = prefetch(
        q_db,
        limit=8,
        config=cfg,
        pg=pg_store,
        q_emb=_EMBED_FN(q_db),
        max_chars=4000,
    )
    hit_map = {h["source_id"]: h for h in hits}
    db_topics = [
        sid
        for sid in hit_map
        if sid.startswith("topic_")
        and "PgBouncer" in (hit_map[sid].get("content") or hit_map[sid].get("content_preview") or "")
    ]
    assert db_topics, f"expected DB Topic V2 hit in {list(hit_map)}"
    # Topic A (last_qa == qa_db < qa_log) must NOT be demoted below stale DB note even if qa_log exists!
    stale_note_sid = f"note:{_NOTE_PREFIX}stale_db"
    if stale_note_sid in hit_map:
        assert list(hit_map).index(db_topics[0]) < list(hit_map).index(stale_note_sid)


def test_pg_r2_legacy_migration_reembeds_and_missing_action_retries(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path
):
    legacy_old_vec = _vec_literal([1.0] + [0.0] * 1023)
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            """
            INSERT INTO public.topics (topic_id, title, summary, body, keywords, status, embedding, embed_model)
            VALUES (%s, '缓存历史主题', '使用 Memcached-Legacy', '采用 Memcached-Legacy 作为缓存', ARRAY['Memcached-Legacy'], 'active', %s::vector, NULL)
            """,
            (_LEGACY_TOPIC_ID, legacy_old_vec),
        )
        lease.connection.commit()

    t0 = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
    qa1 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}r2_missing_action",
        session_id="sess_r2_1",
        turn_id=1,
        question="缓存方案怎么升级了？",
        answer="正式废弃 Memcached-Legacy，改为 Dragonfly-Cluster-V2。",
        ts=t0,
    )

    state_dir = tmp_path / "r2_state"
    attempts = {"n": 0}

    def missing_action_then_valid(messages):
        attempts["n"] += 1
        payload = json.loads(messages[-1]["content"])
        if attempts["n"] == 1:
            # Valid JSON without explicit action -> MUST fail cleanly and trigger retry on next run.
            return json.dumps({"topic_title": "缓存方案", "claim_text": "Dragonfly-Cluster-V2"})
        return json.dumps(
            {
                "action": "attach",
                "target_topic_id": payload["candidate_topics"][0]["topic_id"],
                "topic_title": "缓存历史主题",
                "update_type": "evolve",
                "claim_text": "改用 Dragonfly-Cluster-V2",
                "replaces_text": "Memcached-Legacy",
                "keywords": ["Dragonfly-Cluster-V2"],
            }
        )

    out1 = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=missing_action_then_valid,
        state_dir=state_dir,
        dry_run=False,
        embed_model="r2-fp-v2",
        backfill_history=True,
    )
    assert out1["failed_qa_ids"] == [qa1]
    assert out1["new_last_qa_id"] == 0
    # Legacy migrated topic must ALREADY have its vector recomputed before stamping r2-fp-v2!
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            "SELECT embed_model, embedding::text FROM public.topics WHERE topic_id = %s",
            (_LEGACY_TOPIC_ID,),
        )
        mig_row = cur.fetchone()
    assert mig_row is not None and mig_row[0] == "r2-fp-v2"
    assert mig_row[1] != legacy_old_vec

    # Retry succeeds and consumes qa1 cleanly
    out2 = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=missing_action_then_valid,
        state_dir=state_dir,
        dry_run=False,
        embed_model="r2-fp-v2",
    )
    assert out2["failed_qa_ids"] == []
    assert out2["new_last_qa_id"] == qa1
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("SELECT body, embed_model FROM public.topics WHERE topic_id = %s", (_LEGACY_TOPIC_ID,))
        final_row = cur.fetchone()
    assert final_row is not None and final_row[1] == "r2-fp-v2"
    assert "Dragonfly-Cluster-V2" in strip_topic_v2_header(final_row[0])


def test_pg_r3_newer_note_and_yin_preserved_via_real_prefetch(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path
):
    from v3core.recall_pool import recall_pool as _rp_call

    t_old = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
    t_topic = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
    t_new = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)

    qid_old = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}r3_log_0",
        session_id="sess_r3_log",
        turn_id=1,
        question="日志采集最初用什么方案？",
        answer="日志采集系统早期采用 Syslog-Legacy 方案。",
        ts=t_old,
    )
    qid_cur = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}r3_log_1",
        session_id="sess_r3_log",
        turn_id=2,
        question="日志采集方案现在是什么？",
        answer="正式废弃 Syslog-Legacy，改为 Fluentd-Current 方案。",
        ts=t_topic,
    )
    consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=tmp_path / "r3_state",
        dry_run=False,
        embed_model="r3-fp",
        backfill_history=True,
    )

    emb_log = _vec_literal(_EMBED_FN("日志采集 Fluentd-Current 灰度验证"))
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            """
            INSERT INTO public.observation_notes (version, content, source_qa_range, embedding, embed_model, created_at)
            VALUES (%s, %s, int8range(%s, %s), %s::vector, 'bge-m3', %s::timestamptz)
            """,
            (
                f"{_NOTE_PREFIX}r3_new",
                "最新观察：日志采集 Fluentd-Current 已安排近期切换 Vector 节点。",
                qid_old,
                qid_cur + 1,
                emb_log,
                t_new.isoformat(),
            ),
        )
        cur.execute(
            """
            INSERT INTO public.observation_notes (version, content, source_qa_range, embedding, embed_model, created_at)
            VALUES (%s, %s, int8range(%s, %s), %s::vector, 'bge-m3', %s::timestamptz)
            """,
            (
                f"{_NOTE_PREFIX}r3_old",
                "早期观察：日志采集 Syslog-Legacy 初版调研中。",
                qid_old,
                qid_old + 1,
                emb_log,
                t_old.isoformat(),
            ),
        )
        cur.execute(
            """
            INSERT INTO public.yin_paragraphs (yin_version, section, content, embedding, embed_model, created_at)
            VALUES (%s, 'logging', %s, %s::vector, 'bge-m3', %s::timestamptz)
            """,
            (
                f"{_NOTE_PREFIX}y_new",
                "长期经验：日志采集 Fluentd-Current 需要结构化字段统一。",
                emb_log,
                t_new.isoformat(),
            ),
        )
        lease.connection.commit()

    cfg = _default_pg_dict_config(tmp_path)
    hits, _rp_meta = _rp_call(
        "日志采集 Fluentd-Current 最新情况？",
        pg=pg_store,
        q_emb=_EMBED_FN("日志采集 Fluentd-Current"),
        include_keyword=True,
        include_topic=True,
        include_yin=True,
        include_notes=True,
        limit=10,
        config=cfg,
    )
    by_sid = {h.source_id: h for h in hits}
    new_note_sid = f"note:{_NOTE_PREFIX}r3_new"
    old_note_sid = f"note:{_NOTE_PREFIX}r3_old"
    new_yin_sid = f"yin:{_NOTE_PREFIX}y_new#logging"

    assert new_note_sid in by_sid and by_sid[new_note_sid].created_at != ""
    assert new_yin_sid in by_sid and by_sid[new_yin_sid].created_at != ""
    assert old_note_sid in by_sid and by_sid[old_note_sid].created_at != ""

    ranked_sids = [h.source_id for h in hits]
    assert ranked_sids.index(new_note_sid) < ranked_sids.index(old_note_sid)
    assert ranked_sids.index(new_yin_sid) < ranked_sids.index(old_note_sid)
    assert by_sid[new_yin_sid].rrf_score > by_sid[old_note_sid].rrf_score
    assert by_sid[new_note_sid].rrf_score > by_sid[old_note_sid].rrf_score


def test_pg_pr70_three_layer_claims_persistence_stateless_reload_and_unbounded_recall(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path
):
    from v3core.recall_pool import recall_pool as _rp_call
    from v3core.topic_v2 import fetch_pg_superseded_qa_ids

    t0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=5)
    t2 = t0 + timedelta(days=10)

    qa1 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}pr70_1",
        session_id="sess_pr70",
        turn_id=1,
        question="队列系统最初选型是什么？",
        answer="队列系统最初采用 RabbitMQ-Classic 方案。",
        ts=t0,
    )
    qa2 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}pr70_2",
        session_id="sess_pr70",
        turn_id=2,
        question="队列系统现在用什么？",
        answer="正式废弃 RabbitMQ-Classic，改为 NATS-JetStream 方案。",
        ts=t1,
    )

    state_dir = tmp_path / "pr70_state"
    res1 = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
        embed_model="pr70-fp",
        backfill_history=True,
    )
    assert res1["processed_qas"] == 2
    assert res1["upserted_claims"] >= 2

    # 1. Verify all three layers in PostgreSQL (topics, topic_claims, topic_claim_evidence) + raw qa_pairs intact
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            "SELECT claim_id, topic_id, claim_text, status, replaced_by_claim_id "
            "FROM public.topic_claims WHERE topic_id LIKE %s ORDER BY status ASC",
            (_TOPIC_PREFIX + "%",),
        )
        claim_rows = cur.fetchall()
        cur.execute(
            "SELECT claim_id, qa_id, relation FROM public.topic_claim_evidence WHERE topic_id LIKE %s",
            (_TOPIC_PREFIX + "%",),
        )
        ev_rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM public.qa_pairs WHERE id IN (%s, %s)", (qa1, qa2))
        raw_count = int(cur.fetchone()[0])

    assert raw_count == 2
    active_rows = [r for r in claim_rows if r[3] == "active"]
    sup_rows = [r for r in claim_rows if r[3] == "superseded"]
    assert len(active_rows) == 1 and "NATS-JetStream" in active_rows[0][2]
    assert len(sup_rows) == 1 and "RabbitMQ-Classic" in sup_rows[0][2]
    assert sup_rows[0][4] == active_rows[0][0]
    assert any(r[1] == qa1 and r[2] == "supports" for r in ev_rows)
    assert any(r[1] == qa2 and r[2] == "supports" for r in ev_rows)
    assert any(r[0] == sup_rows[0][0] and r[1] == qa2 and r[2] == "supersedes" for r in ev_rows)

    # 2. Stateless reload: delete local topic_v2_dossiers.json and consolidate a 3rd QA -> PG restores full claims!
    (state_dir / "topic_v2_dossiers.json").unlink()
    qa3 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}pr70_3",
        session_id="sess_pr70",
        turn_id=3,
        question="队列系统又升级了吗？",
        answer="正式废弃 NATS-JetStream，改为 Kafka-KRaft-V4 方案。",
        ts=t2,
    )
    res2 = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
        embed_model="pr70-fp",
    )
    assert res2["processed_qas"] == 1
    assert res2["superseded_claims"] >= 1

    # Both qa1 and qa2 must now be superseded in PostgreSQL relational tables.
    pg_sup = fetch_pg_superseded_qa_ids(pg_store, [qa1, qa2, qa3])
    assert pg_sup == {qa1, qa2}

    # 3. Real recall_pool with include_topic=False (NO topic header at all!) STILL demotes qa1 & qa2 via PG!
    cfg = _default_pg_dict_config(tmp_path)
    hits, _meta = _rp_call(
        "队列系统现在用什么方案？",
        pg=pg_store,
        q_emb=_EMBED_FN("队列系统现在用什么方案"),
        include_keyword=True,
        include_topic=False,
        include_yin=False,
        include_notes=False,
        limit=10,
        config=cfg,
    )
    ranked = [h.source_id for h in hits]
    assert f"qa_{qa3}" in ranked
    for old_q in (qa1, qa2):
        if f"qa_{old_q}" in ranked:
            assert ranked.index(f"qa_{qa3}") < ranked.index(f"qa_{old_q}"), (
                f"PG relational hydration must demote qa_{old_q} even without topic header: {ranked}"
            )


def test_pg_pr71_stateless_recovery_deletes_both_json_files_and_skips_reprocessing(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path
):
    """B2 PG16 regression: deleting BOTH local JSON files restores global watermark from PG topic_v2_state."""
    t0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)

    qa1 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}b2_1",
        session_id="sess_b2",
        turn_id=1,
        question="历史旧问答 1",
        answer="历史结论：采用 Legacy-Cache-V1。",
        ts=t0,
    )
    qa2 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}b2_2",
        session_id="sess_b2",
        turn_id=2,
        question="收到",
        answer="好的",
        ts=t0 + timedelta(minutes=1),
    )

    # 1. First boot WITHOUT backfill_history must initialize watermark to MAX(qa_pairs.id) with 0 LLM calls.
    calls_first: list[int] = []

    def _counting_llm(messages):
        payload = json.loads(messages[-1]["content"])
        qid = int(payload["qa_id"])
        calls_first.append(qid)
        if qid == qa2 or payload["question"] == "收到":
            return json.dumps({"action": "skip", "update_type": "skip"}, ensure_ascii=False)
        return json.dumps(
            {
                "action": "create",
                "topic_title": "缓存架构",
                "update_type": "add",
                "claim_text": "缓存采用 Redis-V7",
                "keywords": ["Redis-V7"],
            },
            ensure_ascii=False,
        )

    state_dir = tmp_path / "b2_state"
    boot_res = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=_counting_llm,
        state_dir=state_dir,
        dry_run=False,
        embed_model="b2-fp",
    )
    assert boot_res["claims_store"] == "pg"
    assert boot_res["initialized_baseline"] is True
    assert boot_res["new_last_qa_id"] == qa2
    assert calls_first == [], "Default first boot must NOT backfill pre-existing QAs"

    # 2. Insert QA3 (topic claim) + QA4 (explicitly skipped chitchat so watermark > max(claim evidence QA IDs)).
    qa3 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}b2_3",
        session_id="sess_b2",
        turn_id=3,
        question="缓存架构用什么？",
        answer="缓存采用 Redis-V7。",
        ts=t0 + timedelta(minutes=2),
    )
    qa4 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}b2_4",
        session_id="sess_b2",
        turn_id=4,
        question="收到",
        answer="谢谢",
        ts=t0 + timedelta(minutes=3),
    )
    res_inc = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=_counting_llm,
        state_dir=state_dir,
        dry_run=False,
        embed_model="b2-fp",
    )
    assert res_inc["new_last_qa_id"] == qa4
    assert calls_first == [qa3, qa4]

    # 3. Delete BOTH local JSON files (topic_v2_dossiers.json AND topic_v2_state.json).
    (state_dir / "topic_v2_dossiers.json").unlink()
    (state_dir / "topic_v2_state.json").unlink()

    calls_after_wipe: list[int] = []

    def _after_wipe_llm(messages):
        payload = json.loads(messages[-1]["content"])
        calls_after_wipe.append(int(payload["qa_id"]))
        return json.dumps({"action": "skip", "update_type": "skip"}, ensure_ascii=False)

    res_reload = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=_after_wipe_llm,
        state_dir=state_dir,
        dry_run=False,
        embed_model="b2-fp",
    )
    assert res_reload["previous_last_qa_id"] == qa4
    assert res_reload["new_last_qa_id"] == qa4
    assert res_reload["claims_store"] == "pg"
    assert calls_after_wipe == [], "Deleting BOTH JSON files must restore watermark from PG without re-calling LLM"
    # Also verify reconstructed claims/topics from PG retained Redis-V7 cleanly after JSON wipe.
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            "SELECT claim_text FROM public.topic_claims WHERE status = 'active' AND claim_text LIKE %s",
            ("%Redis-V7%",),
        )
        assert cur.fetchone() is not None

    # 4. Insert QA5 -> only QA5 is processed.
    qa5 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}b2_5",
        session_id="sess_b2",
        turn_id=5,
        question="收到",
        answer="明白",
        ts=t0 + timedelta(minutes=4),
    )
    res_next = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=_after_wipe_llm,
        state_dir=state_dir,
        dry_run=False,
        embed_model="b2-fp",
    )
    assert res_next["previous_last_qa_id"] == qa4
    assert res_next["new_last_qa_id"] == qa5
    assert calls_after_wipe == [qa5]


def test_pg_pr71_fresh_install_first_conversation_and_late_answer_never_lost(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path
):
    """P1 PG16 regression:
    1) Fresh install (0 rows): startup baseline=0 so turn #1 is NEVER swallowed.
    2) Existing install: startup baseline=MAX(completed) skips history while live turns after boot process.
    3) In-flight empty-answer QA + later completed QA: watermark halts before empty QA until late answer fills.
    """
    from v3core.topic_v2 import ensure_topic_v2_baseline_watermark

    t0 = datetime.now(timezone.utc) - timedelta(minutes=2)

    # Ensure clean qa_pairs table state for the fresh-install baseline check
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("DELETE FROM public.qa_pairs WHERE source_id LIKE %s", (_QA_PREFIX + "%",))
        cur.execute("DELETE FROM public.topic_v2_state")
        lease.connection.commit()

    # 1. Fresh install: startup pins baseline BEFORE turn #1 arrives
    state_fresh = tmp_path / "fresh_state"
    boot_fresh = ensure_topic_v2_baseline_watermark(pg_store, state_dir=state_fresh)
    assert boot_fresh["initialized"] is True

    qa1 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}first_conv_1",
        session_id="sess_first",
        turn_id=1,
        question="新库首次真实对话：队列选什么？",
        answer="新库首次真实对话决定采用 NATS-JetStream-V1。",
        ts=t0,
    )
    res_fresh = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_fresh,
        dry_run=False,
        embed_model="first-fp",
    )
    assert res_fresh["processed_qas"] == 1
    assert res_fresh["new_last_qa_id"] == qa1

    # 2. Existing install with pre-existing qa1: new state_dir2 pins baseline at qa1 BEFORE qa2 arrives
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("DELETE FROM public.topic_v2_state")
        lease.connection.commit()
    state_existing = tmp_path / "existing_state"
    boot_existing = ensure_topic_v2_baseline_watermark(pg_store, state_dir=state_existing)
    assert boot_existing["initialized"] is True
    assert boot_existing["last_qa_id"] == qa1

    qa2 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}existing_live_2",
        session_id="sess_live",
        turn_id=1,
        question="上线后首条对话：缓存用什么？",
        answer="上线后首条对话决定采用 Valkey-V8。",
        ts=t0 + timedelta(seconds=30),
    )
    res_live = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_existing,
        dry_run=False,
        embed_model="first-fp",
    )
    assert res_live["previous_last_qa_id"] == qa1
    assert res_live["processed_qas"] == 1
    assert res_live["new_last_qa_id"] == qa2

    # 3. In-flight empty-answer qa3 + later completed qa4 -> watermark holds at qa2 until qa3 gets late answer
    qa3 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}late_3",
        session_id="sess_late",
        turn_id=1,
        question="对象存储用什么方案？",
        answer="",
        ts=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    qa4 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}late_4",
        session_id="sess_late",
        turn_id=2,
        question="监控告警用什么？",
        answer="监控告警采用 Prometheus-Alertmanager。",
        ts=datetime.now(timezone.utc) - timedelta(seconds=2),
    )
    res_hold = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_existing,
        dry_run=False,
        embed_model="first-fp",
    )
    assert res_hold["pending_incomplete_qa_ids"] == [qa3]
    assert res_hold["new_last_qa_id"] == qa2

    # Simulate late assistant derivation completing qa3's answer
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            "UPDATE public.qa_pairs SET answer = %s WHERE id = %s",
            ("对象存储最终采用 MinIO-Cluster-V2。", qa3),
        )
        lease.connection.commit()

    res_late_done = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=state_existing,
        dry_run=False,
        embed_model="first-fp",
    )
    assert res_late_done["pending_incomplete_qa_ids"] == []
    assert res_late_done["processed_qas"] == 2
    assert res_late_done["new_last_qa_id"] == qa4
    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute(
            "SELECT claim_text FROM public.topic_claims WHERE status = 'active' AND claim_text LIKE %s",
            ("%MinIO-Cluster-V2%",),
        )
        assert cur.fetchone() is not None


def test_pg_pr71_r2_v3core_pg_recovery_and_late_merge_past_grace_window(
    pg_pool: PgPool, pg_store: PgEmbedStore, tmp_path: Path
):
    """PG16 integration regression for PR #71 round-2 review fixes:
    1) V3Core._ensure_topic_v2_baseline() recovers cleanly after a transient PG outage
       so baseline is pinned before turn #1 and turn #1 is consolidated.
    2) Late assistant derivation arriving PAST the 900s grace window atomically queues
       reprocess_qa_ids via V3Core._late_merge_into_qa() and consolidates ONLY that QA
       without regressing last_qa_id or duplicating LLM calls on intermediate QAs.
    """
    from types import SimpleNamespace
    from v3core import V3Core

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("DELETE FROM public.qa_pairs WHERE source_id LIKE %s", (_QA_PREFIX + "%",))
        cur.execute("DELETE FROM public.topic_claims")
        cur.execute("DELETE FROM public.topic_claim_evidence")
        cur.execute("DELETE FROM public.topics")
        cur.execute("DELETE FROM public.topic_v2_state")
        lease.connection.commit()

    # 1. V3Core transient PG outage during init -> recovers before turn #1
    data_dir = tmp_path / "r2_core"
    data_dir.mkdir(parents=True, exist_ok=True)
    core = V3Core.__new__(V3Core)
    core._config = SimpleNamespace(
        base_path=str(data_dir),
        data_dir=str(data_dir),
        embed=None,
        embedding=None,
        prefetch=SimpleNamespace(
            topic_v2_enabled=True,
            topic_v2_backfill_history=False,
        ),
    )

    class _TogglePg:
        def __init__(self, inner):
            self._inner = inner
            self.online = False

        def is_connected(self):
            return self.online

        def lease(self, timeout: float = 10.0):
            if not self.online:
                raise RuntimeError("pg temporarily down")
            return self._inner.lease(timeout=timeout)

        def _connect(self):
            if not self.online:
                raise RuntimeError("pg temporarily down")
            return self._inner._connect()

    toggle_pg = _TogglePg(pg_store)
    core._pg = toggle_pg
    core._pg_pool = None
    core._live_buffer = None
    core._pg_was_connected = False
    core._topic_v2_baseline_checked = False
    core._topic_v2_worker = None
    core._topic_v2_stop_requested = True

    core._ensure_topic_v2_baseline()
    assert core._topic_v2_baseline_checked is False

    toggle_pg.online = True
    core._ensure_topic_v2_baseline()
    assert core._topic_v2_baseline_checked is True

    t0 = datetime.now(timezone.utc) - timedelta(minutes=1)
    qa_turn1 = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}r2_turn1",
        session_id="sess_r2",
        turn_id=1,
        question="PG 恢复后的首轮对话：搜索引擎选型？",
        answer="PG 恢复后决定采用 Meilisearch-V1。",
        ts=t0,
    )
    res_turn1 = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=data_dir,
        dry_run=False,
        embed_model="r2-fp",
    )
    assert res_turn1["processed_qas"] == 1
    assert res_turn1["new_last_qa_id"] == qa_turn1

    # 2. Abandoned empty QA past 15-minute grace window + later completed QA
    qa_old_empty = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}r2_old_empty",
        session_id="sess_r2",
        turn_id=2,
        question="分析引擎选型是什么？",
        answer="",
        ts=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    qa_after = _insert_qa(
        pg_pool,
        source_id=f"{_QA_PREFIX}r2_after",
        session_id="sess_r2",
        turn_id=3,
        question="日志采集用什么？",
        answer="日志采集采用 Vector-V0。",
        ts=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    res_skip = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=None,
        state_dir=data_dir,
        dry_run=False,
        embed_model="r2-fp",
        pending_answer_grace_s=900.0,
    )
    assert res_skip["pending_incomplete_qa_ids"] == []
    assert res_skip["new_last_qa_id"] == qa_after
    assert res_skip["processed_qas"] == 1

    # Late assistant derivation arrives AFTER watermark passed qa_old_empty
    core._pg = pg_store
    merge_res = core._late_merge_into_qa(
        pg=pg_store,
        qa_row=(qa_old_empty, "", [], "分析引擎选型是什么？"),
        msg_id="late_r2_msg_1",
        content="分析引擎最终采用 ClickHouse-V24。",
        remember=lambda: None,
    )
    assert merge_res == "completed_late"

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("SELECT reprocess_qa_ids FROM public.topic_v2_state WHERE state_key = 'default'")
        queued = cur.fetchone()[0]
        assert qa_old_empty in list(queued)

    llm_calls: list[str] = []

    def _counting_llm(messages: list[dict[str, str]]) -> str:
        user_msg = messages[-1]["content"] if messages else ""
        llm_calls.append(user_msg)
        return json.dumps(
            {
                "action": "create",
                "topic_id": "",
                "title": "分析引擎选型",
                "summary": "分析引擎最终采用 ClickHouse-V24。",
                "keywords": ["ClickHouse-V24", "分析引擎"],
                "update_type": "add",
                "claims": ["分析引擎最终采用 ClickHouse-V24。"],
                "replaces_claim_ids": [],
                "supersede_reason": "",
                "confidence": 0.92,
                "reason": "explicit decision",
            },
            ensure_ascii=False,
        )

    res_reprocess = consolidate_pg_incremental(
        pg_store,
        embed_fn=_EMBED_FN,
        llm_fn=_counting_llm,
        state_dir=data_dir,
        dry_run=False,
        embed_model="r2-fp",
        pending_answer_grace_s=900.0,
    )
    assert len(llm_calls) == 1
    assert "ClickHouse-V24" in llm_calls[0]
    assert res_reprocess["processed_qas"] == 1
    assert res_reprocess["new_last_qa_id"] == qa_after
    assert res_reprocess["reprocessed_qa_ids"] == [qa_old_empty]
    assert res_reprocess["remaining_reprocess_qa_ids"] == []

    with pg_pool.lease() as lease:
        cur = lease.connection.cursor()
        cur.execute("SELECT reprocess_qa_ids FROM public.topic_v2_state WHERE state_key = 'default'")
        assert list(cur.fetchone()[0]) == []
        cur.execute(
            "SELECT claim_text FROM public.topic_claims WHERE status = 'active' AND claim_text LIKE %s",
            ("%ClickHouse-V24%",),
        )
        assert cur.fetchone() is not None
