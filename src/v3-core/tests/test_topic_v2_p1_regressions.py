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
    stale_legacy_topic = rp_mod.RecallHit(
        source_id="topic_legacy_cron",
        kind="topic",
        title="定时任务调度",
        content="旧主题结论：当前仍建议继续使用 Cron-Worker-V1 进行轮询调度。",
        content_preview="旧主题结论：当前仍建议继续使用 Cron-Worker-V1 进行轮询调度。",
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

    scored = [superseded_qa, stale_legacy_topic, fresh_topic, active_qa]
    guard = apply_topic_v2_ranking_guard(scored, query="现在定时任务采用什么方案？")
    assert set(guard["demoted_sids"]) == {"qa_10", "topic_legacy_cron"}

    # Simulate Rerank returning higher cosine for stale legacy & superseded QA than active hits!
    for h in scored:
        if h.source_id in ("qa_10", "topic_legacy_cron"):
            h.cosine = 0.99
        else:
            h.cosine = 0.72
    reranked_raw = sorted(scored, key=lambda h: -h.cosine)
    assert reranked_raw[0].source_id in ("qa_10", "topic_legacy_cron")

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
    assert enforced_ids.index("topic_tv2_sched_20") < enforced_ids.index("topic_legacy_cron")


# ---------------------------------------------------------------------------
# P1-2: Cross-topic max QA ID must NOT demote unrelated Topic A
# P1-3: Unrelated long-term Yin / Note must NOT be demoted indiscriminately
# ---------------------------------------------------------------------------

def test_p1_cross_topic_isolation_and_scoped_yin_note_demotion():
    consolidator = TopicConsolidatorV2(embed_fn=_embed)
    dossiers, _ = consolidator.consolidate_qa_stream(
        [
            {
                "id": 100,
                "timestamp": _utc(72),
                "question": "数据库连接池采用什么？",
                "answer": "PostgreSQL 使用 PgBouncer 连接池。",
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
    topic_b = next(d for d in dossiers if d.last_evidence_qa_id == 200)

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
        title="数据库连接池采用什么？",
        content="PostgreSQL 使用 PgBouncer 连接池。",
        content_preview="PostgreSQL 使用 PgBouncer 连接池。",
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
        content="早期观察：PostgreSQL 暂未部署 PgBouncer 连接池。",
        content_preview="早期观察：PostgreSQL 暂未部署 PgBouncer 连接池。",
        rrf_score=0.047,
        created_at=_utc(120),
    )

    scored = [hit_topic_a, hit_qa_100, hit_qa_200, hit_unrelated_yin, hit_related_stale_note]
    guard = apply_topic_v2_ranking_guard(scored, query="PostgreSQL PgBouncer 连接池现在配置如何？")

    assert guard["demoted_topics"] == 0, "Topic A must NOT be demoted by unrelated QA 200"
    assert hit_topic_a.rrf_score == 0.050
    assert "yin:v1" not in guard["demoted_sids"], "Unrelated long-term yin must NOT be demoted"
    assert hit_unrelated_yin.rrf_score == 0.049
    assert "note:v_db_old" in guard["demoted_sids"], "Related stale note mentioning PgBouncer MUST be demoted"
    assert hit_related_stale_note.rrf_score < hit_topic_a.rrf_score


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
