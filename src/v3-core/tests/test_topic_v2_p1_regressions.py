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
    TopicConsolidatorV2,
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
    assert "note:v_db_old" in guard["demoted_sids"], "Stale note with explicit superseded source_qa_range MUST be demoted"
    assert hit_related_stale_note.rrf_score < hit_topic_a.rrf_score
    assert "note:v_unlinked" not in guard["demoted_sids"], "Note without explicit supersession provenance must NOT be demoted by keywords"


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
    assert "note:v_older" in guard["demoted_sids"]


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
