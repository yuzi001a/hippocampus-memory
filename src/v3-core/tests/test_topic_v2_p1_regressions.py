# -*- coding: utf-8 -*-
"""Regression suite for P1 correctness/async issues & P2 hardening in Topic V2."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

from v3core import recall_pool as rp_mod  # noqa: E402
from v3core.topic_v2 import (  # noqa: E402
    TopicClaim,
    TopicConsolidatorV2,
    TopicDossier,
    apply_topic_v2_ranking_guard,
    consolidate_pg_incremental,
    encode_topic_v2_header,
    enforce_topic_v2_post_rerank_order,
    parse_topic_v2_header,
    strip_topic_v2_header,
)


def _utc(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _embed(text: str) -> list[float]:
    vec = [0.0] * 128
    low = str(text or "").lower()
    buckets = [
        (0, ("定时", "任务", "调度", "cron", "scheduler", "event-scheduler")),
        (1, ("数据库", "连接池", "pgbouncer", "postgres", "pool")),
        (2, ("主题", "界面", "darkmode", "theme", "ui")),
        (3, ("鉴权", "认证", "oauth", "token", "auth")),
        (4, ("部署", "容器", "k8s", "helm")),
    ]
    for idx, kws in buckets:
        if any(k in low for k in kws):
            for j in range(12):
                vec[idx * 16 + j] += 2.0
    for i in range(max(len(low) - 1, 1)):
        h = int.from_bytes(hashlib.blake2b(low[i : i + 2].encode("utf-8"), digest_size=4).digest(), "little")
        vec[80 + (h % 48)] += 0.3
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# P1-1: Rerank must NOT overturn Topic V2 freshness demotion
# ---------------------------------------------------------------------------

def test_p1_rerank_cannot_overturn_topic_v2_freshness_guard(monkeypatch):
    class _StubPg:
        def lease(self, timeout=5.0):
            raise AssertionError("PG should not be hit in unit stub")

    fresh_header = encode_topic_v2_header(
        TopicConsolidatorV2(embed_fn=_embed).consolidate_qa_stream(
            [
                {
                    "id": 10,
                    "timestamp": _utc(48),
                    "question": "定时任务最初用什么？",
                    "answer": "早期使用 Cron-Worker-V1。",
                },
                {
                    "id": 20,
                    "timestamp": _utc(1),
                    "question": "定时任务现在用什么？",
                    "answer": "正式废弃 Cron-Worker-V1，改为 Event-Scheduler-V2。",
                },
            ]
        )[0][0]
    )

    superseded_qa = rp_mod.RecallHit(
        source_id="qa_10",
        kind="qa",
        title="定时任务最初用什么？",
        content="早期使用 Cron-Worker-V1。",
        content_preview="早期使用 Cron-Worker-V1。",
        rrf_score=0.05,
        created_at=_utc(48),
    )
    stale_topic_header = (
        '<!-- topic_v2:{"v":2,"last_qa":10,"last_at":"2026-11-26T00:00:00Z",'
        '"active_qas":[10],"superseded_qas":[]} -->'
    )
    stale_topic = rp_mod.RecallHit(
        source_id="topic_tv2_old_cron_10",
        kind="topic",
        title="定时任务调度",
        content=f"{stale_topic_header}\n- 当前结论：使用 Cron-Worker-V1 进行轮询调度。",
        content_preview=f"{stale_topic_header}\n- 当前结论：使用 Cron-Worker-V1 进行轮询调度。",
        rrf_score=0.06,
        created_at=_utc(48),
    )
    fresh_topic = rp_mod.RecallHit(
        source_id="topic_tv2_sched_20",
        kind="topic",
        title="定时任务调度",
        content=f"{fresh_header}\n- 当前结论：正式采用 Event-Scheduler-V2。",
        content_preview=f"{fresh_header}\n- 当前结论：正式采用 Event-Scheduler-V2。",
        rrf_score=0.04,
        created_at=_utc(1),
    )
    active_qa = rp_mod.RecallHit(
        source_id="qa_20",
        kind="qa",
        title="定时任务现在用什么？",
        content="正式废弃 Cron-Worker-V1，改为 Event-Scheduler-V2。",
        content_preview="正式废弃 Cron-Worker-V1，改为 Event-Scheduler-V2。",
        rrf_score=0.045,
        created_at=_utc(1),
    )

    scored = [superseded_qa, stale_topic, fresh_topic, active_qa]
    guard = apply_topic_v2_ranking_guard(scored, query="现在定时任务采用什么方案？")
    assert set(guard["demoted_sids"]) == {"qa_10", "topic_tv2_old_cron_10"}

    # Simulate Rerank returning higher cosine for stale topic & superseded QA than active hits!
    for h in scored:
        if h.source_id in ("qa_10", "topic_tv2_old_cron_10"):
            h.cosine = 0.99
        else:
            h.cosine = 0.72
    reranked_raw = sorted(scored, key=lambda h: -h.cosine)
    assert reranked_raw[0].source_id in ("qa_10", "topic_tv2_old_cron_10")

    enforced = enforce_topic_v2_post_rerank_order(
        reranked_raw,
        demoted_sids=guard["demoted_sids"],
        is_historical=guard["is_historical"],
    )
    enforced_ids = [h.source_id for h in enforced]
    assert enforced_ids[:2] == ["topic_tv2_sched_20", "qa_20"] or set(enforced_ids[:2]) == {
        "topic_tv2_sched_20",
        "qa_20",
    }
    assert enforced_ids.index("qa_20") < enforced_ids.index("qa_10")
    assert enforced_ids.index("topic_tv2_sched_20") < enforced_ids.index("topic_tv2_old_cron_10")


# ---------------------------------------------------------------------------
# P1-2: Cross-topic max QA ID must NOT demote unrelated Topic A
# P1-3: Unrelated long-term Yin / Note must NOT be demoted indiscriminately
# ---------------------------------------------------------------------------

def test_p1_cross_topic_isolation_and_scoped_yin_note_demotion():
    consolidator = TopicConsolidatorV2(embed_fn=_embed)
    dossiers, _ = consolidator.consolidate_qa_stream(
        [
            {
                "id": 90,
                "timestamp": _utc(96),
                "question": "数据库连接池最初用什么？",
                "answer": "早期采用 DirectConn-V0 直连模式。",
            },
            {
                "id": 100,
                "timestamp": _utc(72),
                "question": "数据库连接池现在采用什么？",
                "answer": "正式废弃 DirectConn-V0，改为 PgBouncer 连接池。",
            },
            {
                "id": 200,
                "timestamp": _utc(1),
                "question": "前端界面默认主题配色选什么？",
                "answer": "UI 启用 DarkMode 深色主题，废止 LightTheme-Legacy，改为 DarkMode-V2。",
            },
        ]
    )
    topic_a = next(d for d in dossiers if d.last_evidence_qa_id == 100)
    assert 90 in topic_a.superseded_qa_ids()

    hit_topic_a = rp_mod.RecallHit(
        source_id=f"topic_{topic_a.topic_id}",
        kind="topic",
        title=topic_a.title,
        content=topic_a.render_current_body(include_header=True),
        content_preview=topic_a.render_current_body(include_header=True)[:500],
        rrf_score=0.050,
        created_at=_utc(72),
    )
    hit_qa_100 = rp_mod.RecallHit(
        source_id="qa_100",
        kind="qa",
        title="数据库连接池现在采用什么？",
        content="正式废弃 DirectConn-V0，改为 PgBouncer 连接池。",
        content_preview="正式废弃 DirectConn-V0，改为 PgBouncer 连接池。",
        rrf_score=0.048,
        created_at=_utc(72),
    )
    hit_qa_200 = rp_mod.RecallHit(
        source_id="qa_200",
        kind="qa",
        title="前端界面默认主题配色选什么？",
        content="UI 启用 DarkMode 深色主题。",
        content_preview="UI 启用 DarkMode 深色主题。",
        rrf_score=0.040,
        created_at=_utc(1),
    )
    hit_unrelated_yin = rp_mod.RecallHit(
        source_id="yin:v1",
        kind="yin",
        title="长期沟通风格偏好",
        content="用户喜欢直白简练的技术结论，避免冗余表格和空话。",
        content_preview="用户喜欢直白简练的技术结论，避免冗余表格和空话。",
        rrf_score=0.049,
        created_at=_utc(120),
    )
    hit_related_stale_note = rp_mod.RecallHit(
        source_id="note:v_db_old",
        kind="note",
        title="数据库连接池旧观察笔记",
        content="早期观察：PostgreSQL 暂未部署 PgBouncer 连接池，沿用 DirectConn-V0。",
        content_preview="早期观察：PostgreSQL 暂未部署 PgBouncer 连接池，沿用 DirectConn-V0。",
        rrf_score=0.047,
        created_at=_utc(90),
        source_qa_range="[90,95)",
    )
    hit_unlinked_note = rp_mod.RecallHit(
        source_id="note:v_unlinked",
        kind="note",
        title="数据库监控观察笔记",
        content="PostgreSQL PgBouncer 指标采集正常。",
        content_preview="PostgreSQL PgBouncer 指标采集正常。",
        rrf_score=0.046,
        created_at=_utc(90),
        source_qa_range=None,
    )

    scored = [
        hit_topic_a,
        hit_qa_100,
        hit_qa_200,
        hit_unrelated_yin,
        hit_related_stale_note,
        hit_unlinked_note,
    ]
    guard = apply_topic_v2_ranking_guard(scored, query="PostgreSQL PgBouncer 连接池现在配置如何？")

    assert guard["demoted_topics"] == 0, "Topic A must NOT be demoted by unrelated QA 200"
    assert hit_topic_a.rrf_score == 0.050
    assert "yin:v1" not in guard["demoted_sids"], "Unrelated long-term yin must NOT be demoted"
    assert hit_unrelated_yin.rrf_score == 0.049
    # Multi-topic observation notes must NOT be whole-demoted merely from coarse source_qa_range
    assert "note:v_db_old" not in guard["demoted_sids"]
    assert "note:v_unlinked" not in guard["demoted_sids"]


# ---------------------------------------------------------------------------
# P1-4: _on_live_flush must be non-blocking & async worker shuts down cleanly
# ---------------------------------------------------------------------------

def test_p1_on_live_flush_non_blocking_async_and_shutdown(monkeypatch):
    import v3core
    from v3core.config_model import from_legacy_dict

    core = v3core.V3Core.__new__(v3core.V3Core)
    core._config = from_legacy_dict({})
    caller_tid = threading.get_ident()
    worker_tids: list[int] = []
    started = threading.Event()
    release = threading.Event()

    def slow_consolidate(**kwargs):
        worker_tids.append(threading.get_ident())
        started.set()
        release.wait(timeout=2.0)
        return {"upserted_topics": 1}

    monkeypatch.setattr(core, "consolidate_topics_v2", slow_consolidate)

    t0 = time.monotonic()
    core._on_live_flush([("sess1", "m1", "msg", "user")])
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1, f"_on_live_flush blocked caller thread for {elapsed:.3f}s"

    assert started.wait(timeout=2.0)
    assert worker_tids and worker_tids[0] != caller_tid
    release.set()
    assert core.wait_topic_v2_idle(timeout=2.0)
    core._stop_topic_v2_worker(timeout=1.0)


# ---------------------------------------------------------------------------
# P1-5: LLM failure must NOT advance watermark; explicit skip DOES advance
# ---------------------------------------------------------------------------

def test_p1_llm_failure_does_not_skip_qa_permanently():
    calls = {"11": 0}

    def flaky_llm(messages):
        payload = json.loads(messages[-1]["content"])
        qid = payload["qa_id"]
        if qid == 10:
            return json.dumps(
                {
                    "action": "create",
                    "target_topic_id": "",
                    "topic_title": "Cache",
                    "update_type": "add",
                    "claim_text": "Initial Cache-V1",
                    "keywords": ["Cache-V1"],
                }
            )
        if qid == 11:
            calls["11"] += 1
            if calls["11"] == 1:
                return "{invalid json from transient outage"
            return json.dumps(
                {
                    "action": "attach",
                    "target_topic_id": payload["candidate_topics"][0]["topic_id"],
                    "topic_title": "Cache",
                    "update_type": "evolve",
                    "claim_text": "Migrated to Cache-V2",
                    "replaces_text": "Cache-V1",
                    "keywords": ["Cache-V2"],
                }
            )
        return json.dumps({"action": "skip", "update_type": "skip"})

    consolidator = TopicConsolidatorV2(embed_fn=_embed, llm_fn=flaky_llm)
    qas = [
        {"id": 10, "timestamp": _utc(5), "question": "Cache?", "answer": "Use Cache-V1."},
        {"id": 11, "timestamp": _utc(2), "question": "Cache update?", "answer": "Switch Cache-V1 to Cache-V2."},
        {"id": 12, "timestamp": _utc(1), "question": "Bye?", "answer": "Thanks!"},
    ]

    dossiers1, rep1 = consolidator.consolidate_qa_stream(qas)
    assert rep1.max_qa_id == 10
    assert rep1.failed_qa_ids == (11,)

    # Retry from watermark=10: must recover QA 11 and then explicitly advance through skip QA 12.
    pending = [q for q in qas if q["id"] > rep1.max_qa_id]
    dossiers2, rep2 = consolidator.consolidate_qa_stream(pending, existing_dossiers=dossiers1)
    assert rep2.failed_qa_ids == ()
    assert rep2.max_qa_id == 12
    assert len(dossiers2) == 1
    assert "Cache-V2" in dossiers2[0].render_current_body(include_header=False)
    assert "Cache-V1" not in dossiers2[0].render_current_body(include_header=False)


# ---------------------------------------------------------------------------
# P2-1, P2-2, P2-3, P2-4, P2-5, P2-6 hardening checks
# ---------------------------------------------------------------------------

def test_p2_legacy_body_preserved_and_selective_supersession():
    consolidator = TopicConsolidatorV2(embed_fn=_embed)
    dossiers = consolidator.seed_from_legacy_topics(
        [
            {
                "topic_id": "t_multi",
                "title": "平台基础设施",
                "summary": "平台核心配置总览",
                "body": (
                    "- 当前结论：数据库连接池存在 PgBouncer 锁等待问题\n"
                    "- 当前结论：鉴权采用 Static-Token-V1\n"
                    "- 当前结论：部署架构采用 K8s Helm Chart\n"
                ),
                "keywords": ["PgBouncer", "Static-Token-V1", "Helm"],
            }
        ]
    )
    assert len(dossiers) == 1
    assert len(dossiers[0].current_claims) >= 3

    updated, _ = consolidator.consolidate_qa_stream(
        [
            {
                "id": 50,
                "timestamp": _utc(1),
                "question": "鉴权怎么改了？",
                "answer": "正式废弃 Static-Token-V1，改为 OAuth2-PKCE-V2 作为唯一当前方案。",
            }
        ],
        existing_dossiers=dossiers,
    )
    body = updated[0].render_current_body(include_header=False)
    assert "OAuth2-PKCE-V2" in body
    assert "PgBouncer" in body
    assert "Helm" in body
    assert "Static-Token-V1" not in body


def test_p2_null_session_id_embed_model_updated_at_and_replay_idempotency(tmp_path):
    store_qas = [
        {"id": 1, "session_id": None, "timestamp": _utc(4), "question": "调度?", "answer": "用 Cron-Worker-V1"},
        {"id": 2, "session_id": "s.trajectory.1", "timestamp": _utc(3), "question": "skip?", "answer": "skip"},
    ]
    executed_sqls: list[tuple[str, Any]] = []

    class _FakeCur:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            executed_sqls.append((" ".join(str(sql).split()), params))
            q = " ".join(str(sql).split()).lower()
            if q.startswith("select topic_id, title"):
                self._rows = []
            elif "from qa_pairs" in q:
                wm = int(params[0])
                self._rows = [
                    (r["id"], r["timestamp"], r["question"], r["answer"])
                    for r in store_qas
                    if r["id"] > wm and (r["session_id"] or "") != "s.trajectory.1"
                ]
            else:
                self._rows = []

        def fetchall(self):
            return list(self._rows)

    class _FakeConn:
        autocommit = True

        def cursor(self):
            return _FakeCur()

    class _FakePg:
        def _connect(self):
            return _FakeConn()

        @contextlib.contextmanager
        def lease(self, timeout=10.0):
            yield _FakeConn()

    pg = _FakePg()
    res = consolidate_pg_incremental(
        pg,
        embed_fn=_embed,
        llm_fn=None,
        state_dir=tmp_path,
        dry_run=False,
        embed_model="fp-v1",
        backfill_history=True,
    )
    assert res["new_last_qa_id"] == 1
    assert res["upserted_topics"] == 1
    assert any("coalesce(session_id, '')" in s.lower() for s, _ in executed_sqls)
    assert any("embed_model" in s.lower() and "updated_at = now()" in s.lower() for s, _ in executed_sqls)

    # Simulate crash recovery where state_dir/topic_v2_state.json stayed at 0 while dossiers saved qa_id=1.
    (tmp_path / "topic_v2_state.json").write_text(json.dumps({"last_qa_id": 0}), encoding="utf-8")
    res_replay = consolidate_pg_incremental(
        pg,
        embed_fn=_embed,
        llm_fn=None,
        state_dir=tmp_path,
        dry_run=False,
        embed_model="fp-v1",
        backfill_history=True,
    )
    assert res_replay["new_last_qa_id"] == 1
    assert res_replay["upserted_topics"] == 0, "Replay must be idempotent when dossier already has qa_id=1"

    # Changing embed_model fingerprint must trigger dossier re-embedding + upsert.
    res_reembed = consolidate_pg_incremental(
        pg,
        embed_fn=_embed,
        llm_fn=None,
        state_dir=tmp_path,
        dry_run=False,
        embed_model="fp-v2",
    )
    assert res_reembed["upserted_topics"] == 1


def test_p2_chain_recall_strips_header_and_filters_superseded_entries(monkeypatch):
    header = (
        '<!-- topic_v2:{"v":2,"last_qa":20,"last_at":"2026-11-28T01:00:00Z",'
        '"active_qas":[20],"superseded_qas":[10]} -->'
    )

    class _FakeCur:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            q = " ".join(str(sql).split()).lower()
            if "from topics" in q and "select embedding" in q:
                self._rows = [(json.dumps(_embed("调度")),)]
            elif "from topics" in q:
                self._rows = [
                    ("tv2_sched", "调度器", f"{header}\n- 当前结论：Event-Scheduler-V2", 0.92, f"{header}\n- 当前结论：Event-Scheduler-V2"),
                ]
            elif "from topic_entries" in q:
                self._rows = [
                    ("tv2_sched", "早期方案?", "使用 Cron-Worker-V1", 0.88, 10),
                    ("tv2_sched", "当前方案?", "改为 Event-Scheduler-V2", 0.91, 20),
                ]
            else:
                self._rows = []

        def fetchall(self):
            return list(self._rows)

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class _FakeConn:
        def cursor(self):
            return _FakeCur()

        def close(self):
            pass

    class _FakePg:
        def _connect(self):
            return _FakeConn()

        @contextlib.contextmanager
        def lease(self, timeout=10.0):
            yield _FakeConn()

    monkeypatch.setattr(rp_mod, "_pg_is_connected", lambda pg, deadline=None: True)
    out = rp_mod.chain_recall(
        "现在定时任务用什么？",
        pg=_FakePg(),
        q_emb=_embed("定时任务"),
        config={"prefetch": {"topic_v2_mode": True}},
    )
    assert out["type"] == "chain"
    assert "<!-- topic_v2:" not in out["anchor"]["content_preview"]
    assert all("<!-- topic_v2:" not in c["content_preview"] for c in out["cards"])
    traces = out["traces"]["tv2_sched"]
    assert [t["source_qa_id"] for t in traces] == [20]


def test_p2_deterministic_skips_trivial_chitchat_noise_and_keeps_header_compact():
    consolidator = TopicConsolidatorV2(embed_fn=_embed)
    dossiers, report = consolidator.consolidate_qa_stream(
        [
            {"id": 1, "timestamp": _utc(3), "question": "你好", "answer": "你好"},
            {"id": 2, "timestamp": _utc(2), "question": "收到吗？", "answer": "好的，明白了！"},
            {
                "id": 3,
                "timestamp": _utc(1),
                "question": "数据库连接池采用什么？",
                "answer": "生产环境确认使用 PgBouncer 连接池管理 PostgreSQL 连接。",
            },
        ]
    )
    assert len(dossiers) == 1
    assert report.created_topics == 1
    assert report.skipped_qas == 2
    hdr = encode_topic_v2_header(dossiers[0])
    assert len(hdr) < 260
    assert parse_topic_v2_header(hdr)["v"] == 2
    assert "<!-- topic_v2:" not in strip_topic_v2_header(f"{hdr}\nhello")


# ---------------------------------------------------------------------------
# Round-2 Review Fixes: P1-1, P1-2, P1-3 and P2-1..P2-4 regressions
# ---------------------------------------------------------------------------

def test_p1_1_llm_missing_action_or_invalid_update_type_fails_for_retry():
    calls = {"11": 0, "12": 0}

    def bad_then_good_llm(messages):
        payload = json.loads(messages[-1]["content"])
        qid = payload["qa_id"]
        if qid == 10:
            return json.dumps(
                {
                    "action": "create",
                    "topic_title": "数据库连接池",
                    "update_type": "add",
                    "claim_text": "使用 PgBouncer",
                    "keywords": ["PgBouncer"],
                }
            )
        if qid == 11:
            calls["11"] += 1
            if calls["11"] == 1:
                # Valid JSON but missing explicit "action" -> must NOT silently skip!
                return json.dumps({"topic_title": "数据库连接池", "claim_text": "采用 PgBouncer"})
            return json.dumps(
                {
                    "action": "attach",
                    "target_topic_id": payload["candidate_topics"][0]["topic_id"],
                    "topic_title": "数据库连接池",
                    "update_type": "evolve",
                    "claim_text": "改用 PgBouncer-V2",
                    "replaces_text": "PgBouncer",
                }
            )
        calls["12"] += 1
        if calls["12"] == 1:
            # Invalid update_type on create/attach -> must NOT silently downgrade to "add"!
            return json.dumps(
                {
                    "action": "create",
                    "topic_title": "日志",
                    "update_type": "bogus_mode",
                    "claim_text": "Loki",
                }
            )
        return json.dumps({"action": "skip", "update_type": "skip"})

    consolidator = TopicConsolidatorV2(embed_fn=_embed, llm_fn=bad_then_good_llm)
    qas = [
        {"id": 10, "timestamp": _utc(5), "question": "连接池?", "answer": "PgBouncer"},
        {"id": 11, "timestamp": _utc(3), "question": "升级?", "answer": "PgBouncer-V2"},
        {"id": 12, "timestamp": _utc(1), "question": "日志?", "answer": "Loki"},
    ]
    dossiers1, rep1 = consolidator.consolidate_qa_stream(qas)
    assert rep1.max_qa_id == 10
    assert rep1.failed_qa_ids == (11,)

    # Retry QA 11 succeeds, then QA 12 fails on invalid update_type (watermark stops at 11).
    dossiers2, rep2 = consolidator.consolidate_qa_stream(
        [q for q in qas if q["id"] > rep1.max_qa_id], existing_dossiers=dossiers1
    )
    assert rep2.max_qa_id == 11
    assert rep2.failed_qa_ids == (12,)

    # Retry QA 12 returns explicit skip -> watermark advances cleanly to 12.
    dossiers3, rep3 = consolidator.consolidate_qa_stream(
        [q for q in qas if q["id"] > rep2.max_qa_id], existing_dossiers=dossiers2
    )
    assert rep3.failed_qa_ids == ()
    assert rep3.max_qa_id == 12
    assert "PgBouncer-V2" in dossiers3[0].render_current_body(include_header=False)


def test_p1_2_same_platform_distinct_subtopics_not_demoted():
    consolidator = TopicConsolidatorV2(embed_fn=_embed)
    dossiers, _ = consolidator.consolidate_qa_stream(
        [
            {
                "id": 100,
                "timestamp": _utc(48),
                "question": "PostgreSQL 连接池怎么配置？",
                "answer": "PostgreSQL 连接池采用 PgBouncer 复用连接。",
            },
            {
                "id": 105,
                "timestamp": _utc(36),
                "question": "Docker 容器如何编排部署？",
                "answer": "Docker 生产部署使用 Compose 进行多服务编排。",
            },
            {
                "id": 108,
                "timestamp": _utc(24),
                "question": "Hermes 消息路由规则是什么？",
                "answer": "Hermes 消息路由按频道分发会话流量。",
            },
        ]
    )
    t_pg_pool = next(d for d in dossiers if d.last_evidence_qa_id == 100)
    t_docker = next(d for d in dossiers if d.last_evidence_qa_id == 105)
    t_hermes = next(d for d in dossiers if d.last_evidence_qa_id == 108)

    scored = [
        rp_mod.RecallHit(
            source_id=f"topic_{t_pg_pool.topic_id}",
            kind="topic",
            title=t_pg_pool.title,
            content=t_pg_pool.render_current_body(include_header=True),
            content_preview=t_pg_pool.render_current_body(include_header=True)[:500],
            rrf_score=0.050,
            created_at=_utc(48),
        ),
        rp_mod.RecallHit(
            source_id="qa_200",
            kind="qa",
            title="PostgreSQL 数据库备份用什么？",
            content="PostgreSQL 全量增量备份与归档使用 WAL-G。",
            content_preview="PostgreSQL 全量增量备份与归档使用 WAL-G。",
            rrf_score=0.048,
            created_at=_utc(2),
        ),
        rp_mod.RecallHit(
            source_id=f"topic_{t_docker.topic_id}",
            kind="topic",
            title=t_docker.title,
            content=t_docker.render_current_body(include_header=True),
            content_preview=t_docker.render_current_body(include_header=True)[:500],
            rrf_score=0.049,
            created_at=_utc(36),
        ),
        rp_mod.RecallHit(
            source_id="qa_201",
            kind="qa",
            title="Docker 网络隔离模式怎么选？",
            content="Docker 宿主机直连网络改用 macvlan 网卡驱动。",
            content_preview="Docker 宿主机直连网络改用 macvlan 网卡驱动。",
            rrf_score=0.047,
            created_at=_utc(2),
        ),
        rp_mod.RecallHit(
            source_id=f"topic_{t_hermes.topic_id}",
            kind="topic",
            title=t_hermes.title,
            content=t_hermes.render_current_body(include_header=True),
            content_preview=t_hermes.render_current_body(include_header=True)[:500],
            rrf_score=0.046,
            created_at=_utc(24),
        ),
        rp_mod.RecallHit(
            source_id="qa_202",
            kind="qa",
            title="Hermes 记忆插件开关在哪里配？",
            content="Hermes 记忆存储插件通过 explicit_opt_in 启停。",
            content_preview="Hermes 记忆存储插件通过 explicit_opt_in 启停。",
            rrf_score=0.045,
            created_at=_utc(1),
        ),
    ]
    guard = apply_topic_v2_ranking_guard(scored, query="PostgreSQL Docker Hermes 当前配置")
    assert guard["demoted_topics"] == 0, f"Shared platform name must not cross-demote distinct subtopics: {guard}"

    # Verify recall NEVER guesses topic membership from unconsolidated QAs via keywords:
    # both Topic V2 and the unconsolidated newer QA stay un-demoted until LLM establishes supersession.
    scored_unconsolidated = [
        scored[0],
        rp_mod.RecallHit(
            source_id="qa_250",
            kind="qa",
            title="PostgreSQL 连接池改用什么？",
            content="PostgreSQL 连接池改为内置连接池架构。",
            content_preview="PostgreSQL 连接池改为内置连接池架构。",
            rrf_score=0.040,
            created_at=_utc(1),
        ),
    ]
    guard_unconsolidated = apply_topic_v2_ranking_guard(
        scored_unconsolidated, query="PostgreSQL 连接池现在用什么？"
    )
    assert guard_unconsolidated["demoted_topics"] == 0
    assert "qa_250" not in guard_unconsolidated["demoted_sids"]


def test_p1_3_first_time_legacy_migration_reembeds_before_stamping_fingerprint(tmp_path):
    legacy_old_vec = [1.0] + [0.0] * 127
    pg_rows: dict[str, tuple[str, list[float] | None]] = {}
    embed_calls: list[str] = []

    def tracking_embed(text: str) -> list[float]:
        embed_calls.append(text)
        return _embed(text)

    class _FakeCur:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            q = " ".join(str(sql).split()).lower()
            if q.startswith("select topic_id, title"):
                self._rows = [
                    (
                        "legacy_t1",
                        "调度系统",
                        "使用 Cron-Worker-V1",
                        "使用 Cron-Worker-V1 进行周期调度。",
                        ["Cron-Worker-V1"],
                        "active",
                        legacy_old_vec,
                        "",
                    )
                ]
            elif "from qa_pairs" in q:
                self._rows = []
            elif q.startswith("insert into topics"):
                tid, _t, _s, _b, _k, _st, fp, emb_str = params
                pg_rows[tid] = (fp, json.loads(emb_str))

        def fetchall(self):
            return list(self._rows)

    class _FakeConn:
        autocommit = True

        def cursor(self):
            return _FakeCur()

    class _FakePg:
        def _connect(self):
            return _FakeConn()

        @contextlib.contextmanager
        def lease(self, timeout=10.0):
            yield _FakeConn()

    res = consolidate_pg_incremental(
        _FakePg(),
        embed_fn=tracking_embed,
        llm_fn=None,
        state_dir=tmp_path,
        dry_run=False,
        embed_model="new-model-fp-v2",
        backfill_history=True,
    )
    assert res["upserted_topics"] == 1
    assert len(embed_calls) >= 1, "Legacy migration must recompute vector when embed_model differs"
    assert pg_rows["legacy_t1"][0] == "new-model-fp-v2"
    assert pg_rows["legacy_t1"][1] != legacy_old_vec

    # Failure guard: if embed_fn fails (returns []) during migration, fingerprint must stay unset.
    sub_dir = tmp_path / "fail_embed"
    pg_rows.clear()
    res_fail = consolidate_pg_incremental(
        _FakePg(),
        embed_fn=lambda _txt: [],
        llm_fn=None,
        state_dir=sub_dir,
        dry_run=False,
        embed_model="new-model-fp-v2",
        backfill_history=True,
    )
    assert res_fail["upserted_topics"] == 0
    assert pg_rows == {}
    assert not (sub_dir / "topic_v2_dossiers.json").exists()


def test_p2_1_newer_note_not_demoted_by_older_topic_v2():
    consolidator = TopicConsolidatorV2(embed_fn=_embed)
    dossiers, _ = consolidator.consolidate_qa_stream(
        [
            {
                "id": 40,
                "timestamp": "2026-11-10T08:00:00Z",
                "question": "日志采集最初用什么？",
                "answer": "早期使用 Syslog-V0 采集日志。",
            },
            {
                "id": 50,
                "timestamp": "2026-11-20T08:00:00Z",
                "question": "日志采集方案？",
                "answer": "正式废弃 Syslog-V0，改为 Fluentd-Agent 采集容器日志。",
            },
        ]
    )
    topic = dossiers[0]
    assert 40 in topic.superseded_qa_ids()
    scored = [
        rp_mod.RecallHit(
            source_id=f"topic_{topic.topic_id}",
            kind="topic",
            title=topic.title,
            content=topic.render_current_body(include_header=True),
            content_preview=topic.render_current_body(include_header=True)[:500],
            rrf_score=0.050,
            created_at="2026-11-20T08:00:00Z",
        ),
        rp_mod.RecallHit(
            source_id="note:v_newer",
            kind="note",
            title="最新日志采集观察",
            content="观察结论：Fluentd-Agent 已在边缘集群灰度替换为 Vector。",
            content_preview="观察结论：Fluentd-Agent 已在边缘集群灰度替换为 Vector。",
            rrf_score=0.049,
            created_at="2026-11-27T09:00:00Z",
            source_qa_range="[40,60)",
        ),
        rp_mod.RecallHit(
            source_id="note:v_older",
            kind="note",
            title="旧日志采集观察",
            content="早期观察：Syslog-V0 正在测试中。",
            content_preview="早期观察：Syslog-V0 正在测试中。",
            rrf_score=0.048,
            created_at="2026-11-15T09:00:00Z",
            source_qa_range="[40,45)",
        ),
    ]
    guard = apply_topic_v2_ranking_guard(scored, query="日志采集方案现在怎样？")
    assert "note:v_newer" not in guard["demoted_sids"]
    assert scored[1].rrf_score == 0.049
    # Coarse source_qa_range must not whole-demote multi-topic observation notes either
    assert "note:v_older" not in guard["demoted_sids"]


def test_p2_2_chain_recall_backfills_after_filtering_superseded_entries(monkeypatch):
    header = (
        '<!-- topic_v2:{"v":2,"last_qa":20,"last_at":"2026-11-28T01:00:00Z",'
        '"active_qas":[20],"superseded_qas":[10,11]} -->'
    )
    seen_entry_queries: list[tuple[str, Any]] = []

    class _FakeCur:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            q = " ".join(str(sql).split()).lower()
            if "from topics" in q and "select embedding" in q:
                self._rows = [(json.dumps(_embed("调度")),)]
            elif "from topics" in q:
                self._rows = [
                    ("tv2_sched", "调度器", f"{header}\n- 当前结论：Event-Scheduler-V2", 0.92, f"{header}\n- 当前结论：Event-Scheduler-V2"),
                ]
            elif "from topic_entries" in q:
                seen_entry_queries.append((q, params))
                all_rows = [
                    ("tv2_sched", "旧方案1", "Cron-Worker-V1", 0.95, 10),
                    ("tv2_sched", "旧方案2", "Cron-Worker-V1.5", 0.93, 11),
                    ("tv2_sched", "当前方案", "Event-Scheduler-V2", 0.89, 20),
                ]
                if "source_qa_id = any" in q:
                    excl = set(params[2] or [])
                    lim = int(params[-1])
                    self._rows = [r for r in all_rows if r[4] not in excl][:lim]
                else:
                    lim = int(params[-1])
                    self._rows = all_rows[:lim]

        def fetchall(self):
            return list(self._rows)

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class _FakeConn:
        def cursor(self):
            return _FakeCur()

        def close(self):
            pass

    class _FakePg:
        def _connect(self):
            return _FakeConn()

    monkeypatch.setattr(rp_mod, "_pg_is_connected", lambda pg, deadline=None: True)
    out = rp_mod.chain_recall(
        "现在定时任务用什么？",
        pg=_FakePg(),
        q_emb=_embed("定时任务"),
        traces_per_card=2,
        config={"prefetch": {"topic_v2_mode": True}},
    )
    assert seen_entry_queries and "source_qa_id = any" in seen_entry_queries[0][0]
    traces = out["traces"]["tv2_sched"]
    assert [t["source_qa_id"] for t in traces] == [20]


def test_p2_3_async_worker_drains_backlog_retries_and_shutdown_flushes(monkeypatch):
    import v3core
    from v3core.config_model import from_legacy_dict

    core = v3core.V3Core.__new__(v3core.V3Core)
    core._config = from_legacy_dict({})
    core._topic_v2_retry_base_s = 0.01

    state = {"remaining": 45, "failed_once": False, "calls": 0}

    def fake_consolidate(*, dry_run=False, batch_limit=20, **kw):
        state["calls"] += 1
        if state["calls"] == 1 and not state["failed_once"]:
            state["failed_once"] = True
            return {"processed_qas": 1, "failed_qa_ids": [99]}
        take = min(batch_limit, state["remaining"])
        state["remaining"] -= take
        return {"processed_qas": take, "failed_qa_ids": []}

    monkeypatch.setattr(core, "consolidate_topics_v2", fake_consolidate)

    core._on_live_flush([("sess", "m1", "msg", "user")])
    assert core.wait_topic_v2_idle(timeout=3.0)
    assert state["remaining"] == 0, "Worker must keep draining full batches & retry transient failures"

    # Simulate LiveBuffer final flush during shutdown path before _stop_topic_v2_worker runs.
    state["remaining"] = 5
    core._core_closed = True
    core._on_live_flush([("sess", "m2", "msg", "user")])
    assert core.wait_topic_v2_idle(timeout=2.0)
    assert state["remaining"] == 0
    core._stop_topic_v2_worker(timeout=1.0)


def test_p2_4_deterministic_preserves_short_user_directives():
    consolidator = TopicConsolidatorV2(embed_fn=_embed)
    dossiers, report = consolidator.consolidate_qa_stream(
        [
            {"id": 1, "timestamp": _utc(4), "question": "偏好", "answer": "不要用表格。"},
            {"id": 2, "timestamp": _utc(3), "question": "语言", "answer": "以后用中文回答。"},
            {"id": 3, "timestamp": _utc(2), "question": "权限", "answer": "不要自动发邮件。"},
            {"id": 4, "timestamp": _utc(1), "question": "收到？", "answer": "好的！"},
        ]
    )
    assert report.created_topics == 3
    assert report.skipped_qas == 1
    combined = "\n".join(d.render_current_body(include_header=False) for d in dossiers)
    assert "不要用表格" in combined
    assert "以后用中文回答" in combined
    assert "不要自动发邮件" in combined


# ---------------------------------------------------------------------------
# Round-3 Review Fixes: P1-1, P1-2, P2-1, P2-3 end-to-end regressions
# ---------------------------------------------------------------------------

def test_r3_p1_1_plain_english_and_cjk_subtopics_do_not_cross_demote():
    consolidator = TopicConsolidatorV2(embed_fn=_embed)
    dossiers, _ = consolidator.consolidate_qa_stream(
        [
            {
                "id": 300,
                "timestamp": _utc(48),
                "question": "PostgreSQL database connection pool config?",
                "answer": "PostgreSQL database connection pool uses connection pooling limits.",
            },
            {
                "id": 310,
                "timestamp": _utc(36),
                "question": "Python 用户认证采用什么方案？",
                "answer": "Python 用户认证采用独立令牌验证机制。",
            },
            {
                "id": 320,
                "timestamp": _utc(30),
                "question": "人工智能 OPC 社区建设的目标是什么？",
                "answer": "人工智能 OPC 社区建设聚焦开源生态与开发者协作。",
            },
        ]
    )
    t_pg = next(d for d in dossiers if d.last_evidence_qa_id == 300)
    t_py = next(d for d in dossiers if d.last_evidence_qa_id == 310)
    t_ai = next(d for d in dossiers if d.last_evidence_qa_id == 320)

    scored_cross = [
        rp_mod.RecallHit(
            source_id=f"topic_{t_pg.topic_id}",
            kind="topic",
            title=t_pg.title,
            content=t_pg.render_current_body(include_header=True),
            content_preview=t_pg.render_current_body(include_header=True)[:500],
            rrf_score=0.050,
            created_at=_utc(48),
        ),
        rp_mod.RecallHit(
            source_id="qa_400",
            kind="qa",
            title="PostgreSQL database backup plan?",
            content="PostgreSQL database backup runs nightly snapshot archiving.",
            content_preview="PostgreSQL database backup runs nightly snapshot archiving.",
            rrf_score=0.048,
            created_at=_utc(2),
        ),
        rp_mod.RecallHit(
            source_id=f"topic_{t_py.topic_id}",
            kind="topic",
            title=t_py.title,
            content=t_py.render_current_body(include_header=True),
            content_preview=t_py.render_current_body(include_header=True)[:500],
            rrf_score=0.049,
            created_at=_utc(36),
        ),
        rp_mod.RecallHit(
            source_id="qa_410",
            kind="qa",
            title="Python 用户权限怎么控制？",
            content="Python 用户权限按部门隔离管理。",
            content_preview="Python 用户权限按部门隔离管理。",
            rrf_score=0.047,
            created_at=_utc(1),
        ),
        rp_mod.RecallHit(
            source_id=f"topic_{t_ai.topic_id}",
            kind="topic",
            title=t_ai.title,
            content=t_ai.render_current_body(include_header=True),
            content_preview=t_ai.render_current_body(include_header=True)[:500],
            rrf_score=0.051,
            created_at=_utc(30),
        ),
        rp_mod.RecallHit(
            source_id="qa_420",
            kind="qa",
            title="人工智能模型部署架构怎么选？",
            content="人工智能模型部署采用多卡推理集群与动态批处理。",
            content_preview="人工智能模型部署采用多卡推理集群与动态批处理。",
            rrf_score=0.046,
            created_at=_utc(1),
        ),
    ]
    guard_cross = apply_topic_v2_ranking_guard(scored_cross, query="PostgreSQL Python 人工智能 当前配置")
    assert guard_cross["demoted_topics"] == 0, f"Unrelated topics must NEVER cross-demote: {guard_cross}"

    # Verify LLM-driven evolution (Python 认证 Session -> JWT) uses explicit claim_id + superseded_qas
    # rather than read-side lexical overlap guessing.
    llm_calls: list[dict] = []

    def _auth_llm(messages):
        payload = json.loads(messages[-1]["content"])
        llm_calls.append(payload)
        if payload["qa_id"] == 501:
            return json.dumps(
                {
                    "action": "create",
                    "target_topic_id": "",
                    "topic_title": "Python 认证方案",
                    "update_type": "add",
                    "claim_text": "Python 认证使用 Session",
                    "replaces_text": "",
                    "reason": "",
                    "keywords": ["Python", "认证", "Session"],
                },
                ensure_ascii=False,
            )
        cand = payload["candidate_topics"][0]
        old_cid = cand["active_claims"][0]["claim_id"]
        return json.dumps(
            {
                "action": "attach",
                "target_topic_id": cand["topic_id"],
                "topic_title": "Python 认证方案",
                "update_type": "evolve",
                "claim_text": "Python 认证改用 JWT",
                "replaces_claim_ids": [old_cid],
                "replaces_text": "Session",
                "reason": "无状态扩展需求",
                "keywords": ["Python", "认证", "JWT"],
            },
            ensure_ascii=False,
        )

    llm_consolidator = TopicConsolidatorV2(embed_fn=_embed, llm_fn=_auth_llm)
    auth_dossiers, auth_rep = llm_consolidator.consolidate_qa_stream(
        [
            {
                "id": 501,
                "timestamp": _utc(48),
                "question": "Python 认证用什么？",
                "answer": "Python 认证使用 Session。",
            },
            {
                "id": 502,
                "timestamp": _utc(1),
                "question": "Python 认证方案有调整吗？",
                "answer": "我们决定改成 JWT 认证。",
            },
        ]
    )
    assert len(auth_dossiers) == 1
    assert auth_rep.superseded_claims == 1
    auth_topic = auth_dossiers[0]
    assert auth_topic.active_qa_ids() == [502]
    assert auth_topic.superseded_qa_ids() == [501]
    assert any("JWT" in c.text for c in auth_topic.current_claims)
    assert any("Session" in s.text for s in auth_topic.superseded_claims)

    scored_auth = [
        rp_mod.RecallHit(
            source_id=f"topic_{auth_topic.topic_id}",
            kind="topic",
            title=auth_topic.title,
            content=auth_topic.render_current_body(include_header=True),
            content_preview=auth_topic.render_current_body(include_header=True)[:500],
            rrf_score=0.050,
            created_at=_utc(1),
        ),
        rp_mod.RecallHit(
            source_id="qa_501",
            kind="qa",
            title="Python 认证用什么？",
            content="Python 认证使用 Session。",
            content_preview="Python 认证使用 Session。",
            rrf_score=0.055,
            created_at=_utc(48),
        ),
        rp_mod.RecallHit(
            source_id="qa_502",
            kind="qa",
            title="Python 认证方案有调整吗？",
            content="我们决定改成 JWT 认证。",
            content_preview="我们决定改成 JWT 认证。",
            rrf_score=0.052,
            created_at=_utc(1),
        ),
    ]
    guard_auth = apply_topic_v2_ranking_guard(scored_auth, query="Python 认证现在用什么？")
    assert guard_auth["demoted_sids"] == ["qa_501"]
    assert scored_auth[1].rrf_score < scored_auth[0].rrf_score
    assert scored_auth[1].rrf_score < scored_auth[2].rrf_score


def test_r3_p1_2_real_recall_note_and_yin_propagate_created_at_and_protect_newer(monkeypatch):
    from v3core import topic_recall as tr_mod
    from v3core import yin_pool as yp_mod

    ts_old_note = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
    ts_topic = "2026-11-20T08:00:00Z"
    ts_newer = datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc)

    # 1. Verify _merge_observation_note_hits propagates created_at AND source_qa_range onto RecallHit
    merged_hits = rp_mod._merge_observation_note_hits(
        [
            (101, "v_new", "日志采集边缘集群已灰度切换到 Vector", 0.92, ts_newer, "[40,60)"),
            (100, "v_old", "日志采集早期仍在评估方案", 0.85, ts_old_note, "[40,45)"),
        ],
        [],
        limit=5,
    )
    by_sid = {h.source_id: h for h in merged_hits}
    assert by_sid["note:v_new"].created_at == ts_newer.isoformat()
    assert by_sid["note:v_new"].source_qa_range == "[40,60)"
    assert by_sid["note:v_old"].created_at == ts_old_note.isoformat()
    assert by_sid["note:v_old"].source_qa_range == "[40,45)"

    # 2. Verify yin_pool.search_yin propagates created_at
    class _YinCur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return [("y_20261127", "偏好", "日志采集保持结构化输出", 0.94, ts_newer)]

    class _YinConn:
        def cursor(self):
            return _YinCur()

    monkeypatch.setattr(
        yp_mod,
        "query_with_exact_fallback",
        lambda cur, sql, params, **kw: ([("y_20261127", "偏好", "日志采集保持结构化输出", 0.94, ts_newer)], False),
    )
    yin_res = yp_mod.search_yin(_embed("日志采集"), keyword="", pg=_YinConn(), limit=3)
    assert yin_res[0]["created_at"] == ts_newer.isoformat()

    # 3. End-to-end recall_pool -> real apply_topic_v2_ranking_guard (NO hand-built RecallHits)
    # Also validates P2-1 fix: tz-aware TIMESTAMPTZ created_at applies real UTC half-life decay cleanly.
    topic_header = (
        '<!-- topic_v2:{"v":2,"last_qa":50,"last_at":"2026-11-20T08:00:00Z",'
        '"active_qas":[50],"superseded_qas":[40]} -->'
    )

    class _FakeTopicCur:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            q = " ".join(str(sql).split()).lower()
            if "from observation_notes" in q:
                self._rows = [
                    (101, "v_new", "日志采集边缘集群已切换 Fluentd-Agent 灰度替代方案", 0.88, ts_newer, "[40,60)"),
                    (100, "v_old", "早期观察日志采集使用 Syslog-V0", 0.86, ts_old_note, "[40,45)"),
                ]
            elif "from yin_paragraphs" in q:
                self._rows = [("y_20261127", "logging", "Fluentd-Agent 日志采集近期实践记录", 0.89, ts_newer)]
            else:
                self._rows = []

        def fetchall(self):
            return list(self._rows)

    class _FakePoolConn:
        def cursor(self):
            return _FakeTopicCur()

        def close(self):
            pass

    class _FakePg:
        def _connect(self):
            return _FakePoolConn()

    class _FakeTopicRecall:
        def __init__(self, *a, **kw):
            pass

        def match(self, query, top_k=5, query_embedding=None, deadline=None):
            return [
                (
                    0.91,
                    {
                        "id": "tv2_log_50",
                        "title": "日志采集架构",
                        "body": f"{topic_header}\n- 当前结论：使用 Fluentd-Agent 采集容器日志。",
                        "summary": "使用 Fluentd-Agent",
                    },
                )
            ]

    monkeypatch.setattr(tr_mod, "TopicRecall", _FakeTopicRecall)
    monkeypatch.setattr(
        yp_mod,
        "search_yin",
        lambda qemb, keyword="", pg=None, limit=3: [
            {
                "yin_version": "y_20261127",
                "section": "logging",
                "content": "Fluentd-Agent 日志采集近期实践记录",
                "score": 0.89,
                "matched_by": "vector",
                "created_at": ts_newer.isoformat(),
            }
        ],
    )
    monkeypatch.setattr(rp_mod, "_fetch_observation_child_candidates", lambda cur, emb_str, lim: [])

    results, _rp_meta = rp_mod.recall_pool(
        "日志采集 Fluentd-Agent 现在情况？",
        pg=_FakePg(),
        q_emb=_embed("日志采集 Fluentd-Agent"),
        include_keyword=False,
        include_topic=True,
        include_yin=True,
        include_notes=True,
        limit=10,
        config={"prefetch": {"topic_v2_mode": True}},
    )
    by_res = {h.source_id: h for h in results}
    assert by_res["note:v_new"].created_at == ts_newer.isoformat()
    assert by_res["yin:y_20261127#logging"].created_at == ts_newer.isoformat()
    assert by_res["note:v_old"].created_at == ts_old_note.isoformat()
    assert "<!-- topic_v2:" not in by_res["topic_tv2_log_50"].content
    # Newer note & yin must stay un-demoted while older note IS demoted below topic & newer hits
    idx_ids = [h.source_id for h in results]
    assert idx_ids.index("note:v_new") < idx_ids.index("note:v_old")
    assert idx_ids.index("yin:y_20261127#logging") < idx_ids.index("note:v_old")
    assert idx_ids.index("topic_tv2_log_50") < idx_ids.index("note:v_old")

    # 4. Undated note/yin (created_at="") without superseded identifiers must fail safe and NOT be demoted
    undated_yin = rp_mod.RecallHit(
        source_id="yin:undated",
        kind="yin",
        title="日志",
        content="Fluentd-Agent 补充原则说明",
        content_preview="Fluentd-Agent 补充原则说明",
        rrf_score=0.055,
        created_at="",
    )
    topic_only = rp_mod.RecallHit(
        source_id="topic_tv2_log_50",
        kind="topic",
        title="日志采集架构",
        content=f"{topic_header}\n- 当前结论：Fluentd-Agent",
        content_preview=f"{topic_header}\n- 当前结论：Fluentd-Agent",
        rrf_score=0.050,
        created_at=ts_topic,
    )
    guard_undated = apply_topic_v2_ranking_guard([undated_yin, topic_only], query="日志采集 Fluentd-Agent")
    assert "yin:undated" not in guard_undated["demoted_sids"]


def test_r3_p2_1_real_v3core_shutdown_drains_final_live_buffer_flush(monkeypatch):
    import queue as _queue
    import v3core
    from v3core.config_model import from_legacy_dict

    core = v3core.V3Core.__new__(v3core.V3Core)
    core._config = from_legacy_dict({})
    core._core_lock = threading.Lock()
    core._core_closed = False
    core._core_fenced = False
    core._core_accepting = True
    core._core_generation = 0
    core._runtime = None
    core._topic_refresher_stop = threading.Event()
    core._topic_refresher_thread = None
    core._qa_flush_timer = None
    core._flush_worker = None
    core._flush_queue = _queue.Queue()
    core._qa_stop = threading.Event()
    core._e1_scheduler = None
    core._spill_all_pending_qa_to_durable = lambda: None
    core._flush_all_pending_qa = lambda timeout=0: None

    events_order: list[str] = []

    class _FakePg:
        def close(self):
            events_order.append("pg_close")

    class _FakeLiveBuffer:
        def shutdown(self, timeout=1.0):
            events_order.append("live_buffer_flush")
            # Real V3Core.shutdown has ALREADY set _core_closed=True & _core_fenced=True here!
            assert core._core_closed is True and core._core_fenced is True
            core._on_live_flush([("sess", "m_final", "final msg", "user")])

    core._pg = _FakePg()
    core._live_buffer = _FakeLiveBuffer()

    consolidate_calls: list[int] = []

    def fake_consolidate(**kw):
        events_order.append("consolidate")
        consolidate_calls.append(1)
        return {"processed_qas": 1, "failed_qa_ids": []}

    monkeypatch.setattr(core, "consolidate_topics_v2", fake_consolidate)

    core.shutdown(timeout=2.0)
    assert len(consolidate_calls) == 1, "Final LiveBuffer flush during real shutdown MUST execute consolidate_topics_v2"
    assert events_order.index("consolidate") < events_order.index("pg_close")

    # After shutdown finishes, _topic_v2_stop_requested prevents restarting worker on late flush.
    core._on_live_flush([("sess", "m_late", "late msg", "user")])
    assert len(consolidate_calls) == 1


def test_r3_p2_3_state_dir_file_lock_fails_closed_on_contention(tmp_path: Path):
    import pytest
    from v3core.topic_v2 import _state_dir_file_lock

    lock_path = tmp_path / ".topic_v2.lock"
    lock_path.write_bytes(b"0")
    holder = open(lock_path, "r+b")
    try:
        try:
            import fcntl  # type: ignore

            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            import msvcrt  # type: ignore

            holder.seek(0)
            msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)

        with pytest.raises(TimeoutError, match="failed to acquire file lock"):
            with _state_dir_file_lock(tmp_path, timeout=0.08):
                pytest.fail("Must NOT enter locked body when OS file lock is held")
    finally:
        try:
            import fcntl  # type: ignore

            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        except ImportError:
            try:
                import msvcrt  # type: ignore

                holder.seek(0)
                msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        holder.close()


def test_r4_p2_1_tz_aware_note_applies_utc_half_life_decay(monkeypatch):
    """P2-1: TIMESTAMPTZ created_at on observation notes must not raise TypeError in time decay."""
    now_utc = datetime.now(timezone.utc)
    ts_fresh = now_utc - timedelta(days=1)
    ts_old = now_utc - timedelta(days=90)

    class _Cur:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            q = " ".join(str(sql).split()).lower()
            if "from observation_notes" in q:
                # Give the older note a slightly higher raw cosine so ONLY working half-life decay
                # can push the fresher tz-aware note above the 90-day-old tz-aware note.
                self._rows = [
                    (201, "v_old_tz", "部署观察笔记：早期策略", 0.90, ts_old, None),
                    (202, "v_fresh_tz", "部署观察笔记：最新策略", 0.88, ts_fresh, None),
                ]
            else:
                self._rows = []

        def fetchall(self):
            return list(self._rows)

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    class _Pg:
        def _connect(self):
            return _Conn()

    monkeypatch.setattr(rp_mod, "_fetch_observation_child_candidates", lambda cur, emb_str, lim: [])
    results, _meta = rp_mod.recall_pool(
        "部署策略",
        pg=_Pg(),
        q_emb=_embed("部署策略"),
        include_keyword=False,
        include_topic=False,
        include_yin=False,
        include_notes=True,
        limit=5,
        config={"prefetch": {"topic_v2_mode": True, "half_life_days": 30}},
    )
    by_sid = {h.source_id: h for h in results}
    assert by_sid["note:v_fresh_tz"].rrf_score > by_sid["note:v_old_tz"].rrf_score
    assert results[0].source_id == "note:v_fresh_tz"


def test_r5_p1_1a_replaces_claim_ids_is_strict_and_ambiguous_text_does_not_wipe_sibling_claims():
    """P1-1A: Explicit replaces_claim_ids must NOT fall through to text matching on sibling claims."""
    dossier = TopicDossier(
        topic_id="tv2_auth_1",
        title="Python 认证方案",
        current_claims=[
            TopicClaim(claim_id="c1", text="认证方式使用 Session", kind="decision", evidence_qa_ids=[10]),
            TopicClaim(
                claim_id="c2",
                text="Session Cookie 设置 HttpOnly 和 Secure",
                kind="rule",
                evidence_qa_ids=[11],
            ),
        ],
        keywords=["Python", "认证", "Session"],
        last_evidence_qa_id=11,
        last_evidence_at="2026-11-20T08:00:00Z",
    )

    def _strict_id_llm(messages):
        return json.dumps(
            {
                "action": "attach",
                "target_topic_id": "tv2_auth_1",
                "topic_title": "Python 认证方案",
                "update_type": "evolve",
                "claim_text": "认证方式改用 JWT",
                "replaces_claim_ids": ["c1"],
                "replaces_text": "Session",
                "reason": "无状态扩展需求",
                "keywords": ["Python", "认证", "JWT"],
            },
            ensure_ascii=False,
        )

    consolidator = TopicConsolidatorV2(embed_fn=_embed, llm_fn=_strict_id_llm)
    out, rep = consolidator.consolidate_qa_stream(
        [
            {
                "id": 12,
                "timestamp": "2026-11-25T08:00:00Z",
                "question": "认证方案有变吗？",
                "answer": "我们把认证方式从 Session 改为 JWT。",
            }
        ],
        existing_dossiers=[dossier],
    )
    assert rep.superseded_claims == 1
    active_ids = {c.claim_id for c in out[0].current_claims}
    assert "c1" not in active_ids
    assert "c2" in active_ids, "Sibling Session Cookie rule must NOT be wiped when LLM only replaced c1"
    assert any("JWT" in c.text for c in out[0].current_claims)
    assert out[0].superseded_qa_ids() == [10]

    # When replaces_claim_ids is omitted and replaces_text ambiguously matches >1 primary claims,
    # refuse to blindly wipe both sibling claims.
    dossier_ambig = TopicDossier(
        topic_id="tv2_auth_2",
        title="Python 认证方案",
        current_claims=[
            TopicClaim(claim_id="c1", text="认证方式使用 Session", kind="decision", evidence_qa_ids=[20]),
            TopicClaim(
                claim_id="c2",
                text="Session Cookie 设置 HttpOnly 和 Secure",
                kind="rule",
                evidence_qa_ids=[21],
            ),
        ],
        keywords=["Python", "认证", "Session"],
        last_evidence_qa_id=21,
        last_evidence_at="2026-11-20T08:00:00Z",
    )

    def _no_id_ambig_llm(messages):
        return json.dumps(
            {
                "action": "attach",
                "target_topic_id": "tv2_auth_2",
                "topic_title": "Python 认证方案",
                "update_type": "evolve",
                "claim_text": "认证方式改用 JWT",
                "replaces_text": "Session",
                "reason": "无状态扩展需求",
                "keywords": ["Python", "认证", "JWT"],
            },
            ensure_ascii=False,
        )

    consolidator2 = TopicConsolidatorV2(embed_fn=_embed, llm_fn=_no_id_ambig_llm)
    out2, rep2 = consolidator2.consolidate_qa_stream(
        [
            {
                "id": 22,
                "timestamp": "2026-11-25T08:00:00Z",
                "question": "认证方案有变吗？",
                "answer": "我们把认证方式从 Session 改为 JWT。",
            }
        ],
        existing_dossiers=[dossier_ambig],
    )
    assert rep2.superseded_claims == 0
    assert {"c1", "c2"}.issubset({c.claim_id for c in out2[0].current_claims})


def test_r5_p1_1b_empty_claim_text_missing_target_or_invalid_target_topic_id_fails_for_retry():
    """P1-1B & invalid target_topic_id: reject incomplete LLM JSON so watermark does NOT advance."""
    existing = TopicDossier(
        topic_id="tv2_sched_1",
        title="定时任务调度",
        current_claims=[
            TopicClaim(claim_id="c1", text="使用 Cron-Worker-V1", kind="decision", evidence_qa_ids=[1]),
        ],
        keywords=["定时任务", "Cron-Worker-V1"],
        last_evidence_qa_id=1,
        last_evidence_at="2026-11-20T08:00:00Z",
    )

    bad_payloads = [
        # 1. Empty claim_text on evolve -> must NOT drop old claim without adding replacement
        {
            "action": "attach",
            "target_topic_id": "tv2_sched_1",
            "topic_title": "定时任务调度",
            "update_type": "evolve",
            "claim_text": "",
            "replaces_claim_ids": ["c1"],
            "replaces_text": "Cron-Worker-V1",
            "reason": "淘汰旧调度器",
            "keywords": ["定时任务"],
        },
        # 2. Evolve missing both replaces_claim_ids and replaces_text
        {
            "action": "attach",
            "target_topic_id": "tv2_sched_1",
            "topic_title": "定时任务调度",
            "update_type": "evolve",
            "claim_text": "改用 Event-Scheduler-V2",
            "replaces_text": "",
            "reason": "升级",
            "keywords": ["Event-Scheduler-V2"],
        },
        # 3. Invalid hallucinated target_topic_id on attach -> must NOT attach to candidates[0]
        {
            "action": "attach",
            "target_topic_id": "tv2_nonexistent_999",
            "topic_title": "定时任务调度",
            "update_type": "add",
            "claim_text": "补充配置参数",
            "replaces_text": "",
            "reason": "",
            "keywords": ["定时任务"],
        },
    ]

    for idx, payload in enumerate(bad_payloads, start=10):
        fresh_copy = TopicDossier.from_dict(existing.to_dict())
        consolidator = TopicConsolidatorV2(
            embed_fn=_embed,
            llm_fn=lambda _msgs, p=payload: json.dumps(p, ensure_ascii=False),
        )
        out, rep = consolidator.consolidate_qa_stream(
            [
                {
                    "id": idx,
                    "timestamp": "2026-11-25T08:00:00Z",
                    "question": "定时任务调度更新？",
                    "answer": "定时任务调度改为 Event-Scheduler-V2。",
                }
            ],
            existing_dossiers=[fresh_copy],
        )
        assert rep.failed_qa_ids == (idx,), f"Expected failure for payload #{idx}: {rep}"
        assert rep.updated_topics == 0 and rep.created_topics == 0 and rep.superseded_claims == 0
        assert rep.max_qa_id == 1, "Watermark must NOT advance past existing last_evidence_qa_id=1"
        assert [c.claim_id for c in out[0].current_claims] == ["c1"], "Old claim must stay intact on validation failure"


def test_r5_p1_2_observation_note_not_whole_demoted_by_sparse_source_qa_range():
    """P1-2: Multi-topic Observation Note whose sparse [min,max) range covers a superseded QA stays un-demoted."""
    header = (
        '<!-- topic_v2:{"v":2,"last_qa":8,"last_at":"2026-11-25T08:00:00Z",'
        '"active_qas":[8],"superseded_qas":[5]} -->'
    )
    topic_hit = rp_mod.RecallHit(
        source_id="topic_tv2_sched_8",
        kind="topic",
        title="定时任务调度",
        content=f"{header}\n- 当前结论：Event-Scheduler-V2",
        content_preview=f"{header}\n- 当前结论：Event-Scheduler-V2",
        rrf_score=0.055,
        created_at="2026-11-25T08:00:00Z",
    )
    superseded_qa = rp_mod.RecallHit(
        source_id="qa_5",
        kind="qa",
        title="定时任务旧方案",
        content="早期采用 Cron-Worker-V1。",
        content_preview="早期采用 Cron-Worker-V1。",
        rrf_score=0.054,
        created_at="2026-11-10T08:00:00Z",
    )
    multi_topic_note = rp_mod.RecallHit(
        source_id="note:v_multi_topic",
        kind="note",
        title="综合观察笔记",
        content="记录 QA 1、7、9：包含安全规范与部署清单（未实际包含 QA 5）。",
        content_preview="记录 QA 1、7、9：包含安全规范与部署清单（未实际包含 QA 5）。",
        rrf_score=0.052,
        created_at="2026-11-18T08:00:00Z",
        source_qa_range="[1,10)",
    )
    guard = apply_topic_v2_ranking_guard(
        [topic_hit, superseded_qa, multi_topic_note],
        query="定时任务与部署规范现状",
    )
    assert guard["demoted_sids"] == ["qa_5"]
    assert multi_topic_note.rrf_score == 0.052


# ---------------------------------------------------------------------------
# PR #70: Three-layer PG claims persistence, lossless JSON migration,
# unbounded Recall hydration, relevance-ranked active_claims, and Worker recovery
# ---------------------------------------------------------------------------

def test_pr70_pg_claims_transactional_sync_and_lossless_json_migration(tmp_path: Path):
    """Lossless JSON->PG backfill writes topic_claims & topic_claim_evidence transactionally."""
    from v3core.topic_v2 import SupersededClaim, save_dossiers

    state_dir = tmp_path / "pr70_mig"
    state_dir.mkdir(parents=True, exist_ok=True)
    # Pre-PR#70 dossier JSON: SupersededClaim has no claim_id yet.
    pre_dossier = TopicDossier(
        topic_id="tv2_mig_1",
        title="部署编排方案",
        current_claims=[
            TopicClaim(claim_id="c_2_1", text="使用 Kubernetes-V2", kind="decision", evidence_qa_ids=[2]),
        ],
        superseded_claims=[
            SupersededClaim(
                text="早期使用 Manual-Script-V0",
                replaced_by_claim_id="c_2_1",
                reason="自动化升级",
                superseded_at="2026-11-20T08:00:00Z",
                evidence_qa_ids=[1],
                claim_id="",
            )
        ],
        keywords=["部署", "Kubernetes-V2"],
        last_evidence_qa_id=2,
        last_evidence_at="2026-11-20T08:00:00Z",
        embed_model="fp-1",
        embedding=_embed("部署编排方案 Kubernetes-V2"),
    )
    save_dossiers(state_dir / "topic_v2_dossiers.json", [pre_dossier])
    (state_dir / "topic_v2_state.json").write_text(
        json.dumps({"last_qa_id": 2}), encoding="utf-8"
    )

    claims_tbl: dict[str, dict] = {}
    evidence_tbl: set[tuple[str, str, int, str]] = set()
    tx_events: list[str] = []

    class _FakeClaimsCur:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            q = " ".join(str(sql).split()).lower()
            if "to_regclass('public.topic_claims')" in q:
                self._rows = [("public.topic_claims", "public.topic_claim_evidence")]
            elif "select distinct topic_id from public.topic_claims" in q:
                self._rows = [(c["topic_id"],) for c in claims_tbl.values()]
            elif "from qa_pairs" in q:
                self._rows = []
            elif q == "begin":
                tx_events.append("BEGIN")
            elif q == "commit":
                tx_events.append("COMMIT")
            elif "insert into public.topic_claims" in q:
                if "'active'" in q:
                    cid, tid, ctext, ckind, _upd = params
                    claims_tbl[cid] = {
                        "topic_id": tid,
                        "text": ctext,
                        "kind": ckind,
                        "status": "active",
                        "replaced_by": "",
                    }
                else:
                    scid, tid, ctext, rep_by, sreason, _upd, _sup = params
                    claims_tbl[scid] = {
                        "topic_id": tid,
                        "text": ctext,
                        "status": "superseded",
                        "replaced_by": rep_by,
                        "reason": sreason,
                    }
            elif "insert into public.topic_claim_evidence" in q:
                cid, tid, qid = params
                rel = "supersedes" if "'supersedes'" in q else "supports"
                evidence_tbl.add((cid, tid, int(qid), rel))
            else:
                self._rows = []

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

    class _FakeConn:
        autocommit = True

        def cursor(self):
            return _FakeClaimsCur()

        def close(self):
            pass

    class _FakePg:
        def _connect(self):
            return _FakeConn()

    res = consolidate_pg_incremental(
        _FakePg(),
        embed_fn=_embed,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
        embed_model="fp-1",
    )
    assert res["migrated_claim_topics"] == 1
    assert res["upserted_claims"] == 2
    assert tx_events == ["BEGIN", "COMMIT"]
    assert "c_2_1" in claims_tbl and claims_tbl["c_2_1"]["status"] == "active"
    sup_entries = [v for k, v in claims_tbl.items() if v["status"] == "superseded"]
    assert len(sup_entries) == 1 and sup_entries[0]["replaced_by"] == "c_2_1"
    sup_cid = next(k for k, v in claims_tbl.items() if v["status"] == "superseded")
    assert ("c_2_1", "tv2_mig_1", 2, "supports") in evidence_tbl
    assert (sup_cid, "tv2_mig_1", 1, "supports") in evidence_tbl
    assert (sup_cid, "tv2_mig_1", 2, "supersedes") in evidence_tbl

    # Second run is a clean idempotent no-op (migrated_claim_topics == 0).
    res2 = consolidate_pg_incremental(
        _FakePg(),
        embed_fn=_embed,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
        embed_model="fp-1",
    )
    assert res2["migrated_claim_topics"] == 0
    assert res2["upserted_claims"] == 0


def test_pr70_pg_superseded_qa_hydration_lifts_header_budget_cap(monkeypatch):
    """Recall hydrates superseded QA IDs from PG even when truncated from the 32-item header."""
    from v3core import topic_v2 as tv2_mod

    monkeypatch.setattr(
        tv2_mod,
        "fetch_pg_superseded_qa_ids",
        lambda pg, qids: {5} & set(qids),
    )

    # Header has empty superseded_qas (simulating truncation or missing parent topic in top-K).
    header = (
        '<!-- topic_v2:{"v":2,"last_qa":99,"last_at":"2026-11-28T08:00:00Z",'
        '"active_qas":[99],"superseded_qas":[]} -->'
    )
    qa_old = rp_mod.RecallHit(
        source_id="qa_5",
        kind="qa",
        title="早期方案",
        content="使用 Legacy-Engine-V0",
        content_preview="使用 Legacy-Engine-V0",
        rrf_score=0.060,
        created_at="2026-10-01T08:00:00Z",
    )
    qa_cur = rp_mod.RecallHit(
        source_id="qa_99",
        kind="qa",
        title="当前方案",
        content="使用 Modern-Engine-V5",
        content_preview="使用 Modern-Engine-V5",
        rrf_score=0.055,
        created_at="2026-11-28T08:00:00Z",
    )
    topic_hit = rp_mod.RecallHit(
        source_id="topic_tv2_eng_99",
        kind="topic",
        title="引擎选型",
        content=f"{header}\n- 当前结论：使用 Modern-Engine-V5",
        content_preview=f"{header}\n- 当前结论：使用 Modern-Engine-V5",
        rrf_score=0.052,
        created_at="2026-11-28T08:00:00Z",
    )

    guard = apply_topic_v2_ranking_guard(
        [qa_old, qa_cur, topic_hit],
        query="当前引擎选型是什么？",
        extra_superseded_qa_ids=tv2_mod.fetch_pg_superseded_qa_ids(object(), [5, 99]),
    )
    assert guard["demoted_sids"] == ["qa_5"]
    assert qa_old.rrf_score < topic_hit.rrf_score
    assert qa_old.rrf_score < qa_cur.rrf_score


def test_pr70_phase1_selects_relevant_active_claims_beyond_8():
    """Topics with >16 active claims still surface relevant older claims to LLM Phase 1."""
    claims = [
        TopicClaim(
            claim_id=f"c_{i}",
            text=f"常规配置项 Alpha-{i} 保持默认参数",
            kind="fact",
            evidence_qa_ids=[i],
        )
        for i in range(1, 20)
    ]
    claims.insert(
        0,
        TopicClaim(
            claim_id="c_target_early",
            text="分布式缓存早期使用 Memcached-Pool-X 节点",
            kind="decision",
            evidence_qa_ids=[100],
        ),
    )
    dossier = TopicDossier(
        topic_id="tv2_big_1",
        title="分布式基础设施配置",
        current_claims=claims,
        keywords=["分布式", "缓存", "Memcached-Pool-X"],
        last_evidence_qa_id=100,
    )
    seen_payloads: list[dict] = []

    def _capture_llm(messages):
        payload = json.loads(messages[-1]["content"])
        seen_payloads.append(payload)
        return json.dumps(
            {
                "action": "attach",
                "target_topic_id": "tv2_big_1",
                "topic_title": "分布式基础设施配置",
                "update_type": "evolve",
                "claim_text": "分布式缓存改用 Redis-Cluster-V7",
                "replaces_claim_ids": ["c_target_early"],
                "replaces_text": "Memcached-Pool-X",
                "reason": "持久化与集群分片",
                "keywords": ["分布式", "缓存", "Redis-Cluster-V7"],
            },
            ensure_ascii=False,
        )

    consolidator = TopicConsolidatorV2(embed_fn=_embed, llm_fn=_capture_llm)
    out, rep = consolidator.consolidate_qa_stream(
        [
            {
                "id": 200,
                "timestamp": "2026-11-28T09:00:00Z",
                "question": "Memcached-Pool-X 缓存还用吗？",
                "answer": "Memcached-Pool-X 已切换为 Redis-Cluster-V7。",
            }
        ],
        existing_dossiers=[dossier],
    )
    assert len(seen_payloads) == 1
    sent_claim_ids = [c["claim_id"] for c in seen_payloads[0]["candidate_topics"][0]["active_claims"]]
    assert len(sent_claim_ids) == 16
    assert "c_target_early" in sent_claim_ids
    assert rep.superseded_claims == 1
    assert "c_target_early" not in {c.claim_id for c in out[0].current_claims}


def test_pr70_worker_auto_recovers_after_immediate_retry_exhaustion():
    """Worker schedules automatic background recovery after 3 failed attempts with no new flush."""
    from v3core import V3Core

    core = V3Core.__new__(V3Core)
    core._config = {"prefetch": {"topic_v2_mode": True}}
    core._topic_v2_retry_base_s = 0.01
    core._topic_v2_recovery_cooldown_s = 0.06
    core._topic_v2_recovery_max_s = 0.2
    core._topic_v2_stop_requested = False

    attempts = {"n": 0}
    recovered_ev = threading.Event()

    def _flaky_consolidate(**kwargs):
        attempts["n"] += 1
        # Fail the first 3 attempts (exhausting immediate retries), then succeed on auto-recovery!
        if attempts["n"] <= 3:
            return {"processed_qas": 1, "failed_qa_ids": [999]}
        recovered_ev.set()
        return {"processed_qas": 1, "failed_qa_ids": []}

    core.consolidate_topics_v2 = _flaky_consolidate  # type: ignore[method-assign]
    try:
        core._on_live_flush([{"id": 999}])
        # Immediate burst finishes and wait_topic_v2_idle returns promptly (does not block on cooldown).
        assert core.wait_topic_v2_idle(timeout=1.0)
        assert attempts["n"] >= 3
        # WITHOUT any second _on_live_flush, automatic recovery wakes up and clears the failure!
        assert recovered_ev.wait(timeout=2.0), f"Worker failed to auto-recover; attempts={attempts['n']}"
        assert core.wait_topic_v2_idle(timeout=1.0)
        assert getattr(core, "_topic_v2_pending_recovery", False) is False
    finally:
        core._stop_topic_v2_worker(timeout=1.0)


def test_pr71_first_boot_defaults_to_max_qa_without_backfill_and_degrades_observably(
    tmp_path: Path, caplog, monkeypatch
):
    """B2 + B4: First boot without state initializes watermark to MAX(qa_pairs.id); missing PG tables warn + degrade."""
    import logging
    import psycopg2
    from v3core import distribution_cli as dcli
    from v3core import topic_v2 as tv2_mod

    # 1. Missing PG claims/state tables + pre-existing historical QAs [10, 25]
    llm_calls: list[int] = []
    hist_qas = [
        (10, "2026-10-01T08:00:00Z", "历史问题 1", "历史答案 1"),
        (25, "2026-11-01T08:00:00Z", "历史问题 2", "历史答案 2"),
    ]

    class _DegradedCur:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            q = " ".join(str(sql).split()).lower()
            if "to_regclass('public.topic_claims')" in q or "to_regclass('public.topic_v2_state')" in q:
                self._rows = [(None,)]
            elif "coalesce(max(id), 0) from qa_pairs" in q:
                self._rows = [(max(r[0] for r in hist_qas),)]
            elif "from qa_pairs" in q and "where id > %s" in q:
                min_id = int(params[0])
                self._rows = [r for r in hist_qas if r[0] > min_id]
            else:
                self._rows = []

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

    class _DegradedConn:
        def cursor(self):
            return _DegradedCur()

        def close(self):
            pass

    class _DegradedPg:
        def _connect(self):
            return _DegradedConn()

    def _counting_llm(messages):
        payload = json.loads(messages[-1]["content"])
        llm_calls.append(int(payload["qa_id"]))
        return json.dumps(
            {
                "action": "create",
                "topic_title": "新主题",
                "update_type": "add",
                "claim_text": "新结论",
                "keywords": ["新结论"],
            },
            ensure_ascii=False,
        )

    tv2_mod._WARNED_MISSING_PG_CLAIMS = False
    state_dir = tmp_path / "first_boot_degraded"
    with caplog.at_level(logging.WARNING, logger="v3core.topic_v2"):
        res_init = consolidate_pg_incremental(
            _DegradedPg(),
            embed_fn=_embed,
            llm_fn=_counting_llm,
            state_dir=state_dir,
            dry_run=False,
            embed_model="fp-1",
        )
    assert res_init["previous_last_qa_id"] == 25
    assert res_init["new_last_qa_id"] == 25
    assert res_init["initialized_baseline"] is True
    assert res_init["claims_store"] == "degraded"
    assert llm_calls == [], "First boot must NOT backfill pre-existing historical QAs without backfill_history=True"
    assert any("degraded" in rec.message.lower() for rec in caplog.records)

    # New QA arriving AFTER baseline initialization IS consolidated normally.
    hist_qas.append((26, "2026-11-28T08:00:00Z", "新问题 26", "采用 Redis-Cache-V3。"))
    res_next = consolidate_pg_incremental(
        _DegradedPg(),
        embed_fn=_embed,
        llm_fn=_counting_llm,
        state_dir=state_dir,
        dry_run=False,
        embed_model="fp-1",
    )
    assert res_next["previous_last_qa_id"] == 25
    assert res_next["new_last_qa_id"] == 26
    assert res_next["claims_store"] == "degraded"
    assert llm_calls == [26]

    # 2. doctor --dsn checks topic_claims, topic_claim_evidence, topic_v2_state
    checked_tables: list[str] = []

    class _DocCur:
        def __init__(self):
            self._row = (1,)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            q = " ".join(str(sql).split()).lower()
            if "information_schema.tables" in q and params:
                checked_tables.append(str(params[0]))
                self._row = None if str(params[0]).startswith("topic_") else (1,)
            else:
                self._row = (1,)

        def fetchone(self):
            return self._row

    class _DocConn:
        def cursor(self):
            return _DocCur()

        def close(self):
            pass

    monkeypatch.setattr(psycopg2, "connect", lambda **_kw: _DocConn())
    probe = dcli._doctor_probe_database("postgresql://user:pass@127.0.0.1:5432/testdb")
    for tbl in ("topic_claims", "topic_claim_evidence", "topic_v2_state"):
        assert tbl in checked_tables
        assert probe["required_tables"][tbl] is False


def test_pr71_config_backfill_wiring_doctor_warning_and_incomplete_qa_guard(
    tmp_path: Path, monkeypatch, capsys
):
    """Verify PrefetchConfig.topic_v2_backfill_history, doctor missing-table warning, and pending empty-answer QA guard."""
    from types import SimpleNamespace
    from v3core import distribution_cli as dcli
    from v3core.config_model import PrefetchConfig, from_legacy_dict

    # 1. PrefetchConfig & from_legacy_dict wiring
    assert PrefetchConfig().topic_v2_backfill_history is False
    assert from_legacy_dict({}).prefetch.topic_v2_backfill_history is False
    assert (
        from_legacy_dict({"prefetch": {"topic_v2_backfill_history": True}}).prefetch.topic_v2_backfill_history
        is True
    )
    assert (
        from_legacy_dict({"topic_v2": {"backfill_history": True}}).prefetch.topic_v2_backfill_history
        is True
    )

    # 2. doctor --dsn promotes missing required_tables into warnings + warn exit code (rc == 1)
    monkeypatch.setattr(dcli, "_doctor_resolve_config", lambda _p: ("cfg.yaml", {}, True, None))
    monkeypatch.setattr(
        dcli,
        "_doctor_probe_database",
        lambda _dsn: {
            "skipped": None,
            "target": "postgresql://127.0.0.1:5432/test",
            "reachable": True,
            "pgvector": {"installed": True, "version": "0.7.0"},
            "required_tables": {
                "qa_pairs": True,
                "topic_claims": False,
                "topic_claim_evidence": False,
                "topic_v2_state": False,
            },
        },
    )
    rc = dcli._doctor(SimpleNamespace(profile="default", static=False, dsn="postgresql://127.0.0.1:5432/test"))
    assert rc == 1
    doc_report = json.loads(capsys.readouterr().out)
    assert doc_report["status"] == "warn"
    assert any("topic_claims" in w for w in doc_report["warnings"])

    # 3. Pending recent empty-answer QA (id=10) halts watermark before 10 even when id=11 has an answer;
    # once id=10 answer arrives, both 10 and 11 consolidate cleanly; stale abandoned empty row (>grace) skips.
    now = datetime.now(timezone.utc)
    rows = [
        (10, now - timedelta(seconds=10), "问题 10", ""),
        (11, now - timedelta(seconds=5), "问题 11", "使用 Kafka-V4 方案。"),
    ]

    class _PendingCur:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            q = " ".join(str(sql).split()).lower()
            if "to_regclass" in q:
                self._rows = [(None,)]
            elif "from qa_pairs" in q and "where id > %s" in q:
                min_id = int(params[0])
                self._rows = [r for r in rows if r[0] > min_id]
            else:
                self._rows = []

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

    class _PendingConn:
        def cursor(self):
            return _PendingCur()

        def close(self):
            pass

    class _PendingPg:
        def _connect(self):
            return _PendingConn()

    state_dir = tmp_path / "pending_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "topic_v2_state.json").write_text(json.dumps({"last_qa_id": 9}), encoding="utf-8")

    r_hold = consolidate_pg_incremental(
        _PendingPg(),
        embed_fn=_embed,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
        pending_answer_grace_s=900.0,
    )
    assert r_hold["pending_incomplete_qa_ids"] == [10]
    assert r_hold["new_last_qa_id"] == 9
    assert r_hold["processed_qas"] == 0

    # Late assistant derivation completes qa_10's answer:
    rows[0] = (10, now - timedelta(seconds=10), "问题 10", "使用 Pulsar-V3 方案。")
    r_done = consolidate_pg_incremental(
        _PendingPg(),
        embed_fn=_embed,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
        pending_answer_grace_s=900.0,
    )
    assert r_done["pending_incomplete_qa_ids"] == []
    assert r_done["processed_qas"] == 2
    assert r_done["new_last_qa_id"] == 11

    # Abandoned empty row older than grace window does NOT deadlock watermark:
    rows.append((12, now - timedelta(hours=2), "废弃空问 12", ""))
    rows.append((13, now - timedelta(seconds=2), "问题 13", "使用 Redis-V8 方案。"))
    r_skip_old = consolidate_pg_incremental(
        _PendingPg(),
        embed_fn=_embed,
        llm_fn=None,
        state_dir=state_dir,
        dry_run=False,
        pending_answer_grace_s=900.0,
    )
    assert r_skip_old["pending_incomplete_qa_ids"] == []
    assert r_skip_old["new_last_qa_id"] == 13
    assert r_skip_old["processed_qas"] == 1


def test_pr71_r2_baseline_retry_after_pg_outage_and_zero_state_file(tmp_path: Path):
    """P1-1 & P2:
    1) _ensure_topic_v2_baseline must NOT lock _topic_v2_baseline_checked=True when PG is down;
       once PG recovers, subsequent live_buffer / _ensure_topic_v2_baseline retries and pins baseline.
    2) A legacy topic_v2_state.json containing {"last_qa_id": 0} without baseline_initialized=True
       must NOT trigger full historical backfill when backfill_history=False.
    """
    from types import SimpleNamespace
    from v3core import V3Core

    # 1. V3Core baseline retry across transient PG outage
    class _OutageCur:
        def __init__(self, max_id: int):
            self._max_id = max_id
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            q = " ".join(str(sql).split()).lower()
            if "to_regclass" in q:
                self._rows = [(None,)]
            elif "coalesce(max(id), 0)" in q:
                self._rows = [(self._max_id,)]
            elif "coalesce(min(id), 0)" in q:
                self._rows = [(0,)]
            else:
                self._rows = []

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class _OutageConn:
        def __init__(self, max_id: int):
            self._max_id = max_id

        def cursor(self):
            return _OutageCur(self._max_id)

        def commit(self):
            pass

        def close(self):
            pass

    class _FlakyPg:
        def __init__(self):
            self.connected = False

        def is_connected(self):
            return self.connected

        def _connect(self):
            if not self.connected:
                raise RuntimeError("pg down")
            return _OutageConn(max_id=42)

    data_dir = tmp_path / "core_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    core = V3Core.__new__(V3Core)
    core._config = SimpleNamespace(
        base_path=str(data_dir),
        data_dir=str(data_dir),
        prefetch=SimpleNamespace(
            topic_v2_enabled=True,
            topic_v2_backfill_history=False,
        ),
    )
    core._pg = _FlakyPg()
    core._pg_pool = None
    core._live_buffer = None
    core._pg_was_connected = False
    core._topic_v2_baseline_checked = False

    core._ensure_topic_v2_baseline()
    assert core._topic_v2_baseline_checked is False, "PG outage must not lock baseline flag"
    assert not (data_dir / "topic_v2_state.json").exists()

    core._pg.connected = True
    core._ensure_topic_v2_baseline()
    assert core._topic_v2_baseline_checked is True
    pinned = json.loads((data_dir / "topic_v2_state.json").read_text(encoding="utf-8"))
    assert pinned["last_qa_id"] == 42
    assert pinned["baseline_initialized"] is True

    # 2. Legacy zero-watermark {"last_qa_id": 0} without baseline_initialized
    legacy_dir = tmp_path / "legacy_zero"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "topic_v2_state.json").write_text(json.dumps({"last_qa_id": 0}), encoding="utf-8")

    now = datetime.now(timezone.utc)
    hist_rows = [
        (1, now - timedelta(minutes=10), "历史 1", "历史答案 1"),
        (2, now - timedelta(minutes=8), "历史 2", "历史答案 2"),
        (3, now - timedelta(minutes=5), "历史 3", "历史答案 3"),
    ]

    class _ZeroCur:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            q = " ".join(str(sql).split()).lower()
            if "to_regclass" in q:
                self._rows = [(None,)]
            elif "coalesce(max(id), 0)" in q:
                self._rows = [(3,)]
            elif "coalesce(min(id), 0)" in q:
                self._rows = [(0,)]
            elif "from qa_pairs" in q and "where id > %s" in q:
                min_id = int(params[0])
                self._rows = [r for r in hist_rows if r[0] > min_id]
            else:
                self._rows = []

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

    class _ZeroConn:
        def cursor(self):
            return _ZeroCur()

        def close(self):
            pass

    class _ZeroPg:
        def _connect(self):
            return _ZeroConn()

    res_zero = consolidate_pg_incremental(
        _ZeroPg(),
        embed_fn=_embed,
        llm_fn=None,
        state_dir=legacy_dir,
        dry_run=False,
        backfill_history=False,
    )
    assert res_zero["initialized_baseline"] is True
    assert res_zero["processed_qas"] == 0
    assert res_zero["new_last_qa_id"] == 3
    saved_zero = json.loads((legacy_dir / "topic_v2_state.json").read_text(encoding="utf-8"))
    assert saved_zero["baseline_initialized"] is True
    assert saved_zero["last_qa_id"] == 3
