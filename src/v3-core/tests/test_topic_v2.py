# -*- coding: utf-8 -*-
"""Unit & integration tests for G2 Codex-style two-phase TopicConsolidatorV2."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
V3CORE_PKG = REPO_ROOT / "src" / "v3-core"
V3CORE_SRC = V3CORE_PKG / "src"
for p in (str(V3CORE_SRC), str(V3CORE_PKG)):
    if p not in sys.path:
        sys.path.insert(0, p)

from eval.topic_ablation_bench import _AblationFakePg, embed_text, prefetch_with_trace  # noqa: E402
from v3core.topic_v2 import (  # noqa: E402
    TopicConsolidatorV2,
    TopicDossier,
    encode_topic_v2_header,
    is_historical_trace_query,
    load_dossiers,
    parse_topic_v2_header,
    route_candidate_topics,
    save_dossiers,
    strip_topic_v2_header,
)


def _utc(days_ago: int) -> datetime:
    return datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc) - timedelta(days=days_ago)


def test_scale_free_vector_routing_ignores_hardcoded_threshold_and_handles_shifted_scores():
    dossiers = [
        TopicDossier(
            topic_id="t_db",
            title="Storage Engine",
            summary="Uses PostgreSQL",
            keywords=["database"],
            embedding=[0.30, 0.95, 0.0, 0.0],
        ),
        TopicDossier(
            topic_id="t_ui",
            title="Frontend Theme",
            summary="Uses dark mode",
            keywords=["ui"],
            embedding=[0.05, 0.05, 0.99, 0.0],
        ),
    ]
    # Even when raw cosine is well below legacy SEED_COSINE=0.65, Top-K still returns ranked candidates.
    q_low = [0.95, 0.25, 0.0, 0.0]
    routed = route_candidate_topics(q_low, dossiers, top_k=2)
    assert len(routed) == 2
    assert routed[0][1].topic_id == "t_db"
    assert 0.0 < routed[0][0] < 0.65


def test_llm_mode_consolidation_parses_json_fenced_and_fails_safe_on_garbage():
    responses = [
        json.dumps(
            {
                "action": "create",
                "target_topic_id": "",
                "topic_title": "Cache Layer Selection",
                "update_type": "add",
                "claim_text": "Adopted Memcached-Legacy for initial caching",
                "replaces_text": "",
                "reason": "",
                "keywords": ["Memcached-Legacy", "cache"],
            }
        ),
        # Fenced markdown JSON evolving A -> B
        "```json\n"
        + json.dumps(
            {
                "action": "attach",
                "target_topic_id": "CAND_0",
                "topic_title": "Cache Layer Selection",
                "update_type": "evolve",
                "claim_text": "Current cache layer uses Dragonfly-Cluster",
                "replaces_text": "Memcached-Legacy",
                "reason": "Needed persistent snapshots",
                "keywords": ["Dragonfly-Cluster", "cache"],
            }
        )
        + "\n```",
        "not valid json at all",
    ]
    idx = {"i": 0}

    def fake_llm(messages):
        payload = json.loads(messages[-1]["content"])
        raw = responses[idx["i"]]
        idx["i"] += 1
        if "CAND_0" in raw and payload["candidate_topics"]:
            raw = raw.replace("CAND_0", payload["candidate_topics"][0]["topic_id"])
        return raw

    consolidator = TopicConsolidatorV2(embed_fn=embed_text, llm_fn=fake_llm)
    qas = [
        {"id": 10, "timestamp": _utc(15), "question": "Cache choice?", "answer": "Start with Memcached-Legacy."},
        {"id": 11, "timestamp": _utc(2), "question": "New cache?", "answer": "Switched from Memcached-Legacy to Dragonfly-Cluster."},
        {"id": 12, "timestamp": _utc(1), "question": "Noise?", "answer": "Random malformed output test."},
    ]
    dossiers, report = consolidator.consolidate_qa_stream(qas)
    assert report.processed_qas == 3
    assert report.skipped_qas == 1
    assert report.superseded_claims == 1
    assert len(dossiers) == 1

    d = dossiers[0]
    assert d.last_evidence_qa_id == 11
    assert d.active_qa_ids() == [11]
    assert d.superseded_qa_ids() == [10]
    assert "Dragonfly-Cluster" in d.render_current_body(include_header=False)
    assert "Memcached-Legacy" not in d.render_current_body(include_header=False)
    assert "Memcached-Legacy" not in d.embedding_text()


def test_deterministic_unseen_bilingual_evolution_and_correction(tmp_path):
    consolidator = TopicConsolidatorV2(embed_fn=embed_text)
    unseen_qas = [
        {
            "id": 101,
            "timestamp": _utc(20),
            "question": "关于日志采集管线最初选用什么组件？",
            "answer": "一开始采用 Fluentd-Ruby-Agent 采集日志。",
        },
        {
            "id": 102,
            "timestamp": _utc(12),
            "question": "Fluentd-Ruby-Agent 遇到什么性能问题？",
            "answer": "Fluentd-Ruby-Agent 在高并发下出现内存泄漏与写入阻塞问题。",
        },
        {
            "id": 103,
            "timestamp": _utc(1),
            "question": "日志采集管线现在确认改用什么组件？",
            "answer": "正式废弃 Fluentd-Ruby-Agent，改为 Vector-Rust-Sink 作为唯一当前方案。",
        },
        {
            "id": 201,
            "timestamp": _utc(10),
            "question": "Code formatting preference?",
            "answer": "Initially used Tabs-Width-4.",
        },
        {
            "id": 202,
            "timestamp": _utc(1),
            "question": "Updated code formatting rule?",
            "answer": "Stop using Tabs-Width-4; must use Spaces-Width-2.",
        },
    ]
    dossiers, report = consolidator.consolidate_qa_stream(unseen_qas)
    assert report.created_topics == 2
    assert report.superseded_claims >= 2

    save_path = tmp_path / "dossiers.json"
    save_dossiers(save_path, dossiers)
    reloaded = load_dossiers(save_path)
    assert len(reloaded) == 2

    log_topic = next(d for d in reloaded if "Vector-Rust-Sink" in d.summary)
    assert log_topic.last_evidence_qa_id == 103
    assert log_topic.active_qa_ids() == [103]
    assert log_topic.superseded_qa_ids() == [101, 102]
    assert "Fluentd-Ruby-Agent" not in log_topic.render_current_body(include_header=False)
    assert "Fluentd-Ruby-Agent" in log_topic.render_history_block()

    header = encode_topic_v2_header(log_topic)
    assert len(header) < 260
    parsed = parse_topic_v2_header(log_topic.render_current_body(include_header=True))
    assert parsed is not None and parsed["v"] == 2 and parsed["last_qa"] == 103
    assert strip_topic_v2_header(log_topic.render_current_body(include_header=True)).startswith("- 当前结论：")

    fmt_topic = next(d for d in reloaded if "Spaces-Width-2" in d.summary)
    assert fmt_topic.active_qa_ids() == [202]
    assert fmt_topic.superseded_qa_ids() == [201]
    assert "Tabs-Width-4" not in fmt_topic.render_current_body(include_header=False)


def test_is_historical_trace_query_bilingual():
    assert is_historical_trace_query("以前为什么放弃了 Legacy-FlatFile-V0 存储格式？") is True
    assert is_historical_trace_query("Why did we replace Redis-PubSub-v1 earlier?") is True
    assert is_historical_trace_query("日志采集管线目前决定采用什么方案？") is False
    assert is_historical_trace_query("What formatting rule must we follow right now?") is False


def test_prefetch_topic_v2_mode_opt_in_strips_header_and_respects_query_intent(tmp_path, monkeypatch):
    import v3core.config as v3_config
    import v3core.topic_recall as v3_topic_recall

    monkeypatch.setattr(v3_config, "_resolve_data_dir", lambda cfg=None: tmp_path)
    monkeypatch.setattr(v3_topic_recall, "_resolve_data_dir", lambda cfg=None: tmp_path)

    consolidator = TopicConsolidatorV2(embed_fn=embed_text)
    qas = [
        {
            "id": 1,
            "timestamp": _utc(25),
            "session_id": "s1",
            "question": "关于检索缓存方案最初用什么？",
            "answer": "早期使用 OldCache-V1，后来因为命中率低且不支持原子淘汰，因此在第 2 版下线。",
            "embedding": embed_text("关于检索缓存方案最初用什么？ 早期使用 OldCache-V1，后来因为命中率低且不支持原子淘汰"),
        },
        {
            "id": 2,
            "timestamp": _utc(1),
            "session_id": "s2",
            "question": "关于检索缓存方案现在确认改用什么？",
            "answer": "正式废弃 OldCache-V1，改为 TieredCache-V2 作为唯一当前方案。",
            "embedding": embed_text("关于检索缓存方案现在确认改用什么？ 正式废弃 OldCache-V1，改为 TieredCache-V2"),
        },
    ]
    dossiers, _ = consolidator.consolidate_qa_stream(qas)
    pg = _AblationFakePg(
        qa_pairs=qas,
        topics=[d.to_topic_row(embed_text) for d in dossiers],
        observation_notes=[
            {
                "id": 1,
                "version": "v_old_note",
                "content": "近期观察笔记：检索缓存继续沿用 OldCache-V1。",
                "created_at": _utc(2),
                "embedding": embed_text("近期观察笔记：检索缓存继续沿用 OldCache-V1。"),
            }
        ],
    )

    cur_query = "检索缓存方案现在采用什么？"
    ctx_v2, tr_v2 = prefetch_with_trace(
        cur_query,
        limit=5,
        config={
            "basePath": str(tmp_path),
            "prefetch": {
                "dual_path": True,
                "include_message_vector": False,
                "include_topic": True,
                "include_notes": True,
                "include_yin": False,
                "topic_v2_mode": True,
            },
        },
        pg=pg,
        q_emb=embed_text(cur_query),
        max_chars=4000,
    )
    assert "<!-- topic_v2:" not in ctx_v2
    assert "TieredCache-V2" in ctx_v2
    assert any(sid.startswith("topic_") for sid in tr_v2["injected_ids"])
    # Rank #1 in prompt must be current state (either consolidated topic or qa_2), never stale note or qa_1.
    assert tr_v2["injected_ids"][0] not in ("qa_1", "note:v_old_note")
    assert tr_v2["injected_ids"].index("qa_2") < tr_v2["injected_ids"].index("qa_1")
    topic_rank = next(i for i, sid in enumerate(tr_v2["injected_ids"]) if sid.startswith("topic_"))
    assert topic_rank < tr_v2["injected_ids"].index("note:v_old_note")

    hist_query = "以前为什么放弃了 OldCache-V1 方案？"
    ctx_hist, tr_hist = prefetch_with_trace(
        hist_query,
        limit=5,
        config={
            "basePath": str(tmp_path),
            "prefetch": {
                "dual_path": True,
                "include_message_vector": False,
                "include_topic": True,
                "include_notes": True,
                "include_yin": False,
                "topic_v2_mode": True,
            },
        },
        pg=pg,
        q_emb=embed_text(hist_query),
        max_chars=4000,
    )
    assert "OldCache-V1" in ctx_hist
    assert "qa_1" in tr_hist["injected_ids"]
