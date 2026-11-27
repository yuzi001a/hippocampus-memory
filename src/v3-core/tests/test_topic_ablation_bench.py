# -*- coding: utf-8 -*-
"""Regression tests for topic/notes/yin ablation switches and offline A->B benchmark."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
V3CORE_PKG = REPO_ROOT / "src" / "v3-core"
V3CORE_SRC = V3CORE_PKG / "src"
for p in (str(V3CORE_SRC), str(V3CORE_PKG)):
    if p not in sys.path:
        sys.path.insert(0, p)

from eval.locomo_recall_v2 import cli as locomo_cli  # noqa: E402
from eval.topic_ablation_bench import (  # noqa: E402
    _AblationFakePg,
    _make_qa,
    _make_topic,
    embed_text,
    prefetch_with_trace,
    run_ablation,
)


def test_prefetch_include_topic_false_suppresses_both_vector_and_keyword_topics(tmp_path, monkeypatch):
    import v3core.config as v3_config
    import v3core.topic_recall as v3_topic_recall

    monkeypatch.setattr(v3_config, "_resolve_data_dir", lambda cfg=None: tmp_path)
    monkeypatch.setattr(v3_topic_recall, "_resolve_data_dir", lambda cfg=None: tmp_path)

    query = "NebulaDB 存储后端目前采用什么方案？"
    pg = _AblationFakePg(
        qa_pairs=[
            _make_qa(1, 1, query, "NebulaDB 存储后端已切换为 PostgreSQL-Vector-Store。", "s1"),
        ],
        topics=[
            _make_topic(
                "t_nebula",
                "NebulaDB 存储后端选型",
                "NebulaDB 存储后端长期沿用 Legacy-Flat-Mode。",
                "NebulaDB 存储后端早期采用 Legacy-Flat-Mode。",
                ["NebulaDB", "存储后端"],
            )
        ],
        observation_notes=[],
    )
    q_emb = embed_text(query)

    block_off, d_off = prefetch_with_trace(
        query,
        limit=5,
        config={
            "basePath": str(tmp_path),
            "prefetch": {
                "dual_path": True,
                "include_message_vector": False,
                "include_topic": False,
                "include_notes": False,
                "include_yin": False,
            },
        },
        pg=pg,
        q_emb=q_emb,
        max_chars=4000,
    )
    assert d_off["trace_id"]
    assert not any(sid.startswith("topic_") for sid in d_off["injected_ids"])
    assert not any(sid.startswith("topic_") for sid in d_off["selected_ids"])
    assert "PostgreSQL-Vector-Store" in block_off

    _, d_on = prefetch_with_trace(
        query,
        limit=5,
        config={
            "basePath": str(tmp_path),
            "prefetch": {
                "dual_path": True,
                "include_message_vector": False,
                "include_topic": True,
                "include_notes": True,
                "include_yin": True,
            },
        },
        pg=pg,
        q_emb=q_emb,
        max_chars=4000,
    )
    assert any(sid == "topic_t_nebula" for sid in d_on["selected_ids"])


def test_locomo_cli_parses_ablation_flags_without_breaking_defaults(tmp_path):
    parser = locomo_cli.make_argument_parser()
    base_argv = [
        "run",
        "--dataset", "locomo10.json",
        "--expected-sha256", "a" * 64,
        "--dsn", "host=127.0.0.1 port=55465 dbname=lab user=lab password=lab",
        "--output-dir", str(tmp_path / "out"),
        "--cache-dir", str(tmp_path / "cache"),
        "--source-config", str(tmp_path / "src.yaml"),
    ]
    ns_default = parser.parse_args(base_argv)
    assert ns_default.disable_topic is False
    assert ns_default.disable_notes is False
    assert ns_default.disable_yin is False
    assert ns_default.topic_v2_mode is True

    ns_disabled = parser.parse_args(
        base_argv + ["--disable-topic", "--disable-notes", "--disable-yin", "--no-topic-v2-mode"]
    )
    assert ns_disabled.disable_topic is True
    assert ns_disabled.disable_notes is True
    assert ns_disabled.disable_yin is True
    assert ns_disabled.topic_v2_mode is False

    base_dir = tmp_path / "lab_base"
    base_dir.mkdir()
    default_cfg = locomo_cli.build_isolated_lab_config(
        base_path=str(base_dir),
        dsn="host=127.0.0.1 port=55465 dbname=lab user=lab password=lab",
        output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
        embed_cfg={"provider": "siliconflow", "model": "BAAI/bge-m3"},
    )
    assert "prefetch" not in default_cfg

    ablation_cfg = locomo_cli.build_isolated_lab_config(
        base_path=str(base_dir),
        dsn="host=127.0.0.1 port=55465 dbname=lab user=lab password=lab",
        output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
        embed_cfg={"provider": "siliconflow", "model": "BAAI/bge-m3"},
        include_topic=False,
        include_notes=False,
        include_yin=False,
    )
    assert ablation_cfg["prefetch"] == {
        "include_topic": False,
        "include_notes": False,
        "include_yin": False,
    }

    v2_off_cfg = locomo_cli.build_isolated_lab_config(
        base_path=str(base_dir),
        dsn="host=127.0.0.1 port=55465 dbname=lab user=lab password=lab",
        output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
        embed_cfg={"provider": "siliconflow", "model": "BAAI/bge-m3"},
        topic_v2_mode=False,
    )
    assert v2_off_cfg["prefetch"]["topic_v2_mode"] is False


def test_topic_ablation_bench_runs_all_profiles_and_g2_beats_g1(tmp_path):
    summary = run_ablation(out_dir=tmp_path)
    assert summary["scenario_count"] == 18
    assert (tmp_path / "ablation_summary.json").is_file()
    assert (tmp_path / "ablation_report.md").is_file()

    for pname in ("G0_qa_only", "G1_obs", "G1_full", "G2_freshness_only", "G2_structured"):
        assert len(summary["cases"][pname]) == 18
        assert summary["profiles"][pname]["trace_coverage"] == 1.0

    for case in summary["cases"]["G0_qa_only"]:
        assert not any(
            sid.startswith(("topic_", "obs_", "note:", "yin:")) for sid in case["injected_ids"]
        )

    g1_obs = summary["profiles"]["G1_obs"]
    g2 = summary["profiles"]["G2_structured"]
    assert g2["pass_rate"] > g1_obs["pass_rate"]
    assert g2["evolution_pass_rate"] == 1.0
    assert g2["correction_pass_rate"] == 1.0
    assert g2["synthesis_pass_rate"] == 1.0
    assert g2["history_pass_rate"] == 1.0
    assert g2["false_recall_rate"] == 0.0
    assert g2["stale_outranks_current_rate"] == 0.0
