# -*- coding: utf-8 -*-
"""Live-LLM G2 Topic V2 benchmark runner (supports MiniMax 3.1 Flash CN subscription).

Reads credentials ONLY from the explicit local key file (default
``C:/Users/liu/Desktop/minimax-cn-key.env``) or process environment variables, never
logs or persists any API key into reports, and compares ``G1_full``, ``G2_deterministic``,
and ``G2_live_llm`` across the offline ablation scenarios.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_HERE = Path(__file__).resolve()
_PKG_ROOT = _HERE.parents[1]
_SRC_ROOT = _PKG_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


DEFAULT_KEY_FILE = Path(r"C:\Users\liu\Desktop\minimax-cn-key.env")
DEFAULT_MINIMAX_CN_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MINIMAX_CN_MODEL = "MiniMax-M3.1-Flash"


def load_key_env_file(path: str | Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` pairs from a UTF-8/UTF-8-BOM env file without setting os.environ."""
    target = Path(path)
    if not target.is_file():
        return {}
    out: dict[str, str] = {}
    for raw_line in target.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _redact_secret(text: str, secret: str) -> str:
    if not text or not secret:
        return text
    return str(text).replace(secret, "[REDACTED]")


def resolve_live_llm_config(
    *,
    key_file: str | Path | None = None,
    base_url: str = "",
    model: str = "",
) -> dict[str, Any]:
    """Resolve MiniMax CN (or override) ``cfg`` ready for ``v3core.llm_gateway``."""
    env_path = Path(key_file) if key_file is not None else DEFAULT_KEY_FILE
    file_vars = load_key_env_file(env_path)

    api_key = (
        file_vars.get("MINIMAX_CN_API_KEY")
        or file_vars.get("MINIMAX_API_KEY")
        or file_vars.get("LLM_API_KEY")
        or os.environ.get("MINIMAX_CN_API_KEY", "").strip()
        or os.environ.get("MINIMAX_API_KEY", "").strip()
        or os.environ.get("LLM_API_KEY", "").strip()
    )
    if not api_key:
        raise RuntimeError(
            f"未找到 API Key：请先在 {env_path} 填写 MINIMAX_CN_API_KEY=<你的Key> 后再运行。"
        )

    eff_base_url = (
        base_url.strip()
        or file_vars.get("MINIMAX_BASE_URL", "").strip()
        or os.environ.get("MINIMAX_BASE_URL", "").strip()
        or os.environ.get("LLM_BASE_URL", "").strip()
        or DEFAULT_MINIMAX_CN_BASE_URL
    )
    eff_model = (
        model.strip()
        or file_vars.get("MINIMAX_MODEL", "").strip()
        or os.environ.get("MINIMAX_MODEL", "").strip()
        or os.environ.get("LLM_MODEL", "").strip()
        or DEFAULT_MINIMAX_CN_MODEL
    )

    cfg = {
        "llm": {
            "provider": "minimax_cn",
            "model": eff_model,
            "base_url": eff_base_url,
            "api_key": api_key,
            "thinking_mode": "auto",
            "reasoning_effort": "low",
        },
        "providers": {
            "minimax_cn": {
                "api_mode": "chat_completions",
                "base_url": eff_base_url,
                "max_tokens_field": "max_completion_tokens",
                "supports": [
                    "temperature",
                    "thinking",
                    "reasoning_split",
                    "reasoning_effort",
                ],
            }
        },
        "models": {
            "MiniMax-M3.1": {
                "provider": "minimax_cn",
                "thinking": "adaptive",
                "reasoning_effort": "low",
            }
        },
    }
    return cfg


def _render_live_markdown(summary: Mapping[str, Any]) -> str:
    profiles = summary["profiles"]
    llm_stats = summary.get("llm_stats") or {}
    lines = [
        f"# Topic V2 Live-LLM Benchmark (`{summary['model_label']}`)",
        "",
        f"- Generated UTC: `{summary['timestamp_utc']}`",
        f"- Scenarios: `{summary['scenario_count']}`",
        f"- LLM Calls: `{llm_stats.get('calls', 0)}` (ok=`{llm_stats.get('succeeded', 0)}`, fail=`{llm_stats.get('failed', 0)}`)",
        f"- Tokens: prompt=`{llm_stats.get('prompt_tokens', 0)}`, completion=`{llm_stats.get('completion_tokens', 0)}`, total=`{llm_stats.get('total_tokens', 0)}`",
        f"- LLM Total Latency (s): `{llm_stats.get('total_latency_s', 0.0)}`",
        "",
        "| Profile | Overall Pass | A->B Evolution | Cross-Session Synth | Correction Residual | Historical Trace | Stale Outranks Current | False Recall | Avg Context Chars |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for pname in ("G1_full", "G2_deterministic", "G2_live_llm"):
        m = profiles[pname]
        lines.append(
            f"| `{pname}` | {m['pass_count']}/{m['scenario_count']} ({m['pass_rate']:.1%}) "
            f"| {m['evolution_pass_rate']:.1%} "
            f"| {m['synthesis_pass_rate']:.1%} "
            f"| {m['correction_pass_rate']:.1%} "
            f"| {m['history_pass_rate']:.1%} "
            f"| {m['stale_outranks_current_rate']:.1%} "
            f"| {m['false_recall_rate']:.1%} "
            f"| {m['avg_context_chars']} |"
        )
    lines.append("")
    return "\n".join(lines)


def run_live_bench(
    *,
    llm_fn: Callable[[list[dict[str, str]]], str],
    model_label: str,
    out_dir: str | Path | None = None,
    scenario_limit: int | None = None,
    secret_to_redact: str = "",
) -> dict[str, Any]:
    """Run ``G1_full`` vs ``G2_deterministic`` vs ``G2_live_llm`` across ablation scenarios."""
    import v3core.config as v3_config
    import v3core.topic_recall as v3_topic_recall
    from eval.topic_ablation_bench import (
        _AblationFakePg,
        _aggregate_profile,
        _evaluate_case,
        build_scenarios,
        embed_text,
        prefetch_with_trace,
    )
    from v3core.topic_v2 import TopicConsolidatorV2

    scenarios = build_scenarios()
    if scenario_limit is not None and scenario_limit > 0:
        scenarios = scenarios[: int(scenario_limit)]

    orig_cfg_resolver = v3_config._resolve_data_dir
    orig_tr_resolver = v3_topic_recall._resolve_data_dir

    profile_order = ("G1_full", "G2_deterministic", "G2_live_llm")
    all_cases: dict[str, list[dict[str, Any]]] = {p: [] for p in profile_order}
    total_qa_inputs = 0
    total_live_skipped = 0

    with tempfile.TemporaryDirectory(
        prefix="v3core_topic_v2_live_", ignore_cleanup_errors=True
    ) as tmp_root:
        tmp_path = Path(tmp_root)
        try:
            for s_idx, scenario in enumerate(scenarios):
                total_qa_inputs += len(scenario.qa_pairs)
                pg_legacy = _AblationFakePg(
                    qa_pairs=scenario.qa_pairs,
                    topics=scenario.topics,
                    observation_notes=scenario.observation_notes,
                    yin_paragraphs=scenario.yin_paragraphs,
                )

                det_cons = TopicConsolidatorV2(embed_fn=embed_text, llm_fn=None)
                det_seeds = det_cons.seed_from_legacy_topics(scenario.topics)
                det_dossiers, _ = det_cons.consolidate_qa_stream(
                    scenario.qa_pairs, existing_dossiers=det_seeds
                )
                det_topics = [d.to_topic_row(embed_text) for d in det_dossiers]
                pg_det = _AblationFakePg(
                    qa_pairs=scenario.qa_pairs,
                    topics=det_topics,
                    observation_notes=scenario.observation_notes,
                    yin_paragraphs=scenario.yin_paragraphs,
                )

                live_cons = TopicConsolidatorV2(embed_fn=embed_text, llm_fn=llm_fn)
                live_seeds = live_cons.seed_from_legacy_topics(scenario.topics)
                live_dossiers, live_rep = live_cons.consolidate_qa_stream(
                    scenario.qa_pairs, existing_dossiers=live_seeds
                )
                total_live_skipped += live_rep.skipped_qas
                live_topics = [d.to_topic_row(embed_text) for d in live_dossiers]
                pg_live = _AblationFakePg(
                    qa_pairs=scenario.qa_pairs,
                    topics=live_topics,
                    observation_notes=scenario.observation_notes,
                    yin_paragraphs=scenario.yin_paragraphs,
                )

                q_emb = embed_text(scenario.query)
                profile_inputs = {
                    "G1_full": (pg_legacy, scenario.topics, False),
                    "G2_deterministic": (pg_det, det_topics, True),
                    "G2_live_llm": (pg_live, live_topics, True),
                }

                for p_idx, pname in enumerate(profile_order):
                    active_pg, active_topics, v2_mode = profile_inputs[pname]
                    case_dir = tmp_path / f"{s_idx}_{p_idx}_{pname}"
                    case_dir.mkdir(parents=True, exist_ok=True)
                    v3_config._resolve_data_dir = lambda cfg=None, _d=case_dir: _d
                    v3_topic_recall._resolve_data_dir = lambda cfg=None, _d=case_dir: _d

                    cfg = {
                        "basePath": str(case_dir),
                        "prefetch": {
                            "dual_path": True,
                            "rrf_k": 60,
                            "include_message_vector": False,
                            "include_topic": True,
                            "include_notes": True,
                            "include_yin": True,
                            "topic_v2_mode": v2_mode,
                        },
                    }
                    t0 = time.perf_counter()
                    ctx, trace_summary = prefetch_with_trace(
                        scenario.query,
                        limit=5,
                        config=cfg,
                        pg=active_pg,
                        q_emb=q_emb,
                        max_chars=4000,
                    )
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    all_cases[pname].append(
                        _evaluate_case(
                            scenario,
                            pname,
                            ctx,
                            trace_summary,
                            elapsed_ms,
                            active_topics=active_topics,
                        )
                    )
        finally:
            v3_config._resolve_data_dir = orig_cfg_resolver
            v3_topic_recall._resolve_data_dir = orig_tr_resolver
            gc.collect()

    profile_summaries = {
        pname: _aggregate_profile(pname, all_cases[pname])
        for pname in profile_order
    }

    stats_obj = getattr(llm_fn, "stats", None)
    stats_dict = stats_obj.to_dict() if hasattr(stats_obj, "to_dict") else {}
    if secret_to_redact and stats_dict.get("errors"):
        stats_dict["errors"] = [_redact_secret(e, secret_to_redact) for e in stats_dict["errors"]]

    valid_notes = max(0, total_qa_inputs - total_live_skipped)
    summary: dict[str, Any] = {
        "version": "topic-v2-live-v1",
        "model_label": str(model_label),
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scenario_count": len(scenarios),
        "total_qa_inputs": total_qa_inputs,
        "valid_json_rate": round(valid_notes / total_qa_inputs, 4) if total_qa_inputs else 0.0,
        "llm_stats": stats_dict,
        "profiles": profile_summaries,
        "cases": all_cases,
    }
    report_md = _render_live_markdown(summary)

    if out_dir is not None:
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        summary_text = _redact_secret(
            json.dumps(summary, ensure_ascii=False, indent=2),
            secret_to_redact,
        )
        report_text = _redact_secret(report_md, secret_to_redact)
        (out_path / "live_ablation_summary.json").write_text(summary_text, encoding="utf-8")
        (out_path / "live_ablation_report.md").write_text(report_text, encoding="utf-8")

    return summary


def main(argv: Sequence[str] | None = None) -> int:
    from v3core.topic_v2 import make_gateway_llm_fn

    parser = argparse.ArgumentParser(
        description="Run live-LLM vs deterministic G2 Topic V2 benchmark."
    )
    parser.add_argument(
        "--key-file",
        default=str(DEFAULT_KEY_FILE),
        help="Path to local key env file (outside repo).",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="External output directory for live_ablation_summary.json and live_ablation_report.md.",
    )
    parser.add_argument("--model", default="", help="Override model ID.")
    parser.add_argument("--base-url", default="", help="Override OpenAI-compatible base_url.")
    parser.add_argument("--limit", type=int, default=0, help="Optional scenario limit for quick smoke test.")
    args = parser.parse_args(argv)

    cfg = resolve_live_llm_config(
        key_file=args.key_file,
        base_url=args.base_url,
        model=args.model,
    )
    secret = str((cfg.get("llm") or {}).get("api_key") or "")
    model_label = str((cfg.get("llm") or {}).get("model") or DEFAULT_MINIMAX_CN_MODEL)
    llm_fn = make_gateway_llm_fn(cfg)

    summary = run_live_bench(
        llm_fn=llm_fn,
        model_label=model_label,
        out_dir=args.out_dir,
        scenario_limit=(args.limit if args.limit > 0 else None),
        secret_to_redact=secret,
    )
    for pname in ("G1_full", "G2_deterministic", "G2_live_llm"):
        m = summary["profiles"][pname]
        print(
            f"[topic-v2-live] model={model_label} profile={pname} "
            f"pass={m['pass_count']}/{m['scenario_count']} ({m['pass_rate']:.1%}) "
            f"evo={m['evolution_pass_rate']:.1%} synth={m['synthesis_pass_rate']:.1%} "
            f"corr={m['correction_pass_rate']:.1%} stale_outranks={m['stale_outranks_current_rate']:.1%}"
        )
    print(f"[topic-v2-live] valid_json_rate={summary['valid_json_rate']:.1%} llm_stats={summary['llm_stats']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
