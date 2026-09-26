"""Q01 — frozen query canary for the A02 query path.

WHY THIS FILE EXISTS
--------------------
A02 gave the query path two behaviours: a cold query is embedded on its first call, and a query past
the provider window is reduced token-safely (head 60% / tail 40%). Q01 asks the frozen question set
below against a real profile and records what actually happened.

RULES (frozen before execution)
-------------------------------
* The wording of every case in ``CASES`` is frozen. A failing case is reported as failing; the set
  may not be reworded afterwards to make results look better.
* Observation reuses the existing RecallTrace: the Core path calls ``recall_pool`` directly, so this
  runner hands the engine a real trace built with the existing factories
  (``build_query_context`` + ``build_default_query_plan`` + ``recall_trace_from_plan``). Nothing new
  is invented for observability.
* Privacy: traces are serialized with the privacy-safe defaults (no content, no query text), and
  provider payloads are never printed. Only counts, ids, classes and timings are reported.
* Nothing is written to the target profile.

Two levels of case (both frozen here):
  * ``mode="retrieval"`` — mechanically checkable: cold/hot provider behaviour, cap, head/tail
    retention, failure degradation, semantic-lane vector hand-off.
  * ``mode="response"`` — the *answer* is what matters (off-topic honesty). Retrieval cannot prove
    it, so the runner records the retrieval-side facts and the case is judged during the A03 real
    canary against the frozen rubric in ``docs/Q01-QUERY-CANARY.md``. Never reported as PASS here.

Usage
-----
    python q01_query_canary.py --list
    python q01_query_canary.py --only Q01-01,Q01-15 --out q01-report.json
    python q01_query_canary.py --profile default --out q01-report.json
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ── frozen case set ──────────────────────────────────────────────────────────
# category: cold_semantic | hot_cache | historical_fact | project_continuation |
#           long_query | embedding_failure | off_topic

CASES: list[dict[str, Any]] = [
    {"id": "Q01-01", "mode": "retrieval", "category": "cold_semantic",
     "query": "我们之前定过一条规则：任何写入路径都不许悄悄丢掉向量，那条规则的英文名字是什么？",
     "expect": {"embedding_attempted": True, "min_hits": 1}},
    {"id": "Q01-02", "mode": "retrieval", "category": "hot_cache",
     "query": "我们之前定过一条规则：任何写入路径都不许悄悄丢掉向量，那条规则的英文名字是什么？",
     "expect": {"repeat_of": "Q01-01", "additional_provider_calls": 0, "same_hit_ids": True}},
    {"id": "Q01-03", "mode": "retrieval", "category": "historical_fact",
     "query": "long-observation 那批历史数据最后修了几条？",
     "expect": {"min_hits": 1}},
    {"id": "Q01-04", "mode": "retrieval", "category": "historical_fact",
     "query": "长 QA 的向量索引表叫什么名字？",
     "expect": {"min_hits": 1}},
    {"id": "Q01-05", "mode": "retrieval", "category": "project_continuation",
     "query": "我们现在这条统一的开发基线叫什么？",
     "expect": {"min_hits": 1}},
    {"id": "Q01-06", "mode": "retrieval", "category": "project_continuation",
     "query": "下一步该做哪个阶段的工作？",
     "expect": {"min_hits": 1}},
    {"id": "Q01-07", "mode": "retrieval", "category": "long_query",
     "query_ref": "synth_long",
     "expect": {"truncated": True, "prepared_tokens_max": 7680, "head_retained": True,
                "tail_retained": True, "no_provider_error": True}},
    {"id": "Q01-08", "mode": "retrieval", "category": "long_query",
     "query_ref": "synth_long_with_tail_question",
     "expect": {"truncated": True, "prepared_tokens_max": 7680, "tail_retained": True,
                "min_hits": 1, "no_provider_error": True}},
    {"id": "Q01-09", "mode": "retrieval", "category": "embedding_failure",
     "query": "long-observation 那批历史数据最后修了几条？",
     "expect": {"inject_failure": "EmbeddingCallError", "no_crash": True,
                "embedding_attempted": True, "min_hits": 1}},
    {"id": "Q01-10", "mode": "retrieval", "category": "embedding_failure",
     "query": "长 QA 的向量索引表叫什么名字？",
     "expect": {"inject_failure": "timeout", "no_crash": True, "embedding_attempted": True,
                "min_hits": 1}},
    {"id": "Q01-11", "mode": "response", "category": "off_topic",
     "query": "请从第一性原理解释量子色动力学里的渐近自由是什么。",
     "expect": {"rubric": "不把任何历史片段当作本问题的答案；无命中或明确说明信息不足"}},
    {"id": "Q01-12", "mode": "response", "category": "off_topic",
     "query": "帮我写一个把 CSV 转成 Parquet 的 Python 函数。",
     "expect": {"rubric": "不把任何历史片段当作本问题的答案；无命中或明确说明信息不足"}},
    {"id": "Q01-13", "mode": "retrieval", "category": "cold_semantic",
     "query": "embedding 模型不一致会造成什么后果？",
     "expect": {"embedding_attempted": True, "min_hits": 1}},
    {"id": "Q01-14", "mode": "retrieval", "category": "historical_fact",
     "query": "印是什么？它和主题卡分别负责什么？",
     "expect": {"min_hits": 1}},
    {"id": "Q01-15", "mode": "retrieval", "category": "long_query",
     "query_ref": "synth_short_control",
     "expect": {"truncated": False, "prepared_equals_original": True}},
    {"id": "Q01-16", "mode": "retrieval", "category": "hot_cache",
     "query": "下一步该做哪个阶段的工作？",
     "expect": {"repeat_of": "Q01-06", "additional_provider_calls": 0, "same_hit_ids": True}},
]

HONESTY_FENCE = "[以下为与当前话题相关的过往对话/笔记片段；若信息不足，请如实说明，不要编造]"

# Deterministic synthetic long queries — frozen; no private content, no per-run variation.
_SYNTH_WORDS = ["基线", "向量", "召回", "索引", "写入", "失败", "账本", "窗口", "切片", "观察",
                "主题", "段落", "压缩", "回填", "校验", "契约", "采样", "预算", "语义", "关键词"]


def _synth_paragraph(i: int) -> str:
    w = _SYNTH_WORDS
    return (f"记录 {i}：本段用于合成超长查询，第 {i} 步处理 {w[i % len(w)]} 与 "
            f"{w[(i + 3) % len(w)]}，结论是 {w[(i + 7) % len(w)]} 需要复核。"
            f"step {i} reviewed {w[i % len(w)]}.")


def synth_long(tail: str = "") -> str:
    """>9001 tokens of deterministic filler, optionally ending in a real question."""
    return "".join(_synth_paragraph(i) for i in range(400)) + tail


SYNTHETIC_INPUTS: dict[str, Callable[[], str]] = {
    "synth_long": lambda: synth_long(),
    "synth_long_with_tail_question":
        lambda: synth_long("（以上是背景材料）现在请回答：长 QA 的向量索引表叫什么名字？"),
    "synth_short_control": lambda: "一句普通的短查询，用来确认短查询完全不被改动。",
}


@dataclass
class CaseResult:
    case_id: str
    mode: str
    category: str
    query_chars: int
    checks: dict[str, Any] = field(default_factory=dict)
    observed: dict[str, Any] = field(default_factory=dict)
    verdict: str = "UNKNOWN"


def _build_trace(query: str):
    """A real RecallTrace for one request, built with the existing factory helpers."""
    from v3core.recall_v2.adapters import build_query_context
    from v3core.recall_v2.contracts import build_default_query_plan
    from v3core.recall_v2.trace import recall_trace_from_plan

    ctx = build_query_context(query)
    return recall_trace_from_plan(ctx, build_default_query_plan(ctx))


def _toolchain(v3core_mod) -> tuple[dict, Callable[[], None]]:
    """Count provider-bound query embeddings, capture a real RecallTrace, allow failure injection."""
    state: dict[str, Any] = {"sent": [], "attempts": [], "inject": None, "built_traces": []}
    original_ce = v3core_mod.call_query_embedding

    def _ce_observed(text, *args, **kwargs):
        state["attempts"].append(len(text))
        if state["inject"] is not None:
            from v3core.embedding import EmbeddingCallError
            raise EmbeddingCallError(f"Q01 injected {state['inject']}")
        state["sent"].append(text)
        return original_ce(text, *args, **kwargs)

    v3core_mod.call_query_embedding = _ce_observed

    import v3core.recall_pool as recall_pool_module
    original_rp = recall_pool_module.recall_pool

    def _rp_observed(query, *args, **kwargs):
        if kwargs.get("trace") is None and kwargs.get("trace_out") is None:
            try:
                tr = _build_trace(query)
                state["built_traces"].append(tr)
                kwargs["trace"] = tr
            except Exception as exc:  # noqa: BLE001 — recorded, never faked
                state["trace_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return original_rp(query, *args, **kwargs)

    recall_pool_module.recall_pool = _rp_observed

    def _restore() -> None:
        v3core_mod.call_query_embedding = original_ce
        recall_pool_module.recall_pool = original_rp

    return state, _restore


def _token_count(text: str, embed_cfg) -> int:
    from v3core.embed_chunks import _get_token_counter
    return int(_get_token_counter(embed_cfg, tokenizer_override=None)(text))


def run_cases(profile: str, config_path: str | None,
              only: list[str] | None = None) -> dict[str, Any]:
    import v3core
    from v3core import V3Core

    core = V3Core(profile=profile, config_path=config_path)
    from v3core.embedding import safe_embed_cfg
    embed_cfg = safe_embed_cfg(core.config)

    results: list[CaseResult] = []
    by_id: dict[str, CaseResult] = {}
    selected = [c for c in CASES if not only or c["id"] in only]

    for case in selected:
        query = (SYNTHETIC_INPUTS[case["query_ref"]]() if case.get("query_ref") else case["query"])
        res = CaseResult(case_id=case["id"], mode=case["mode"], category=case["category"],
                         query_chars=len(query))
        expect = case["expect"]
        state, restore = _toolchain(v3core)
        try:
            state["inject"] = expect.get("inject_failure")
            started = time.time()
            try:
                hits = core.prefetch(query, limit=5, fmt="list")
                res.observed["raised"] = None
            except Exception as exc:  # noqa: BLE001 — the canary records, it never hides
                hits = None
                res.observed["raised"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            res.observed["elapsed_ms"] = round((time.time() - started) * 1000, 1)
            state["inject"] = None
            sent = list(state["sent"])
            attempts = list(state["attempts"])
            traces = list(state["built_traces"])
            trace_error = state.get("trace_error")
        finally:
            restore()

        hit_ids = [h.get("source_id") for h in (hits or []) if isinstance(h, dict)]
        res.observed.update({
            "hit_count": len(hit_ids),
            "hit_ids": hit_ids[:5],
            "provider_query_calls": len(sent),
            "query_attempts": len(attempts),
            "sent_chars": [len(c) for c in sent],
            "sent_tokens": ([_token_count(sent[0], embed_cfg) if sent and embed_cfg else None]),
            "trace_built": bool(traces),
            "trace_error": trace_error,
        })
        if traces:
            res.observed["trace"] = traces[-1].to_dict()   # privacy-safe defaults

        # ── mechanical checks (retrieval cases) ──
        if expect.get("embedding_attempted"):
            res.checks["embedding_attempted"] = len(attempts) >= 1
        if expect.get("no_crash"):
            res.checks["no_crash"] = res.observed["raised"] is None
        if expect.get("min_hits"):
            res.checks["min_hits"] = len(hit_ids) >= expect["min_hits"]
        if "truncated" in expect:
            if expect["truncated"]:
                res.checks["truncated_true"] = bool(sent) and len(sent[0]) < len(query)
            else:
                res.checks["truncated_false"] = bool(sent) and len(sent[0]) == len(query)
        if expect.get("prepared_equals_original"):
            res.checks["prepared_equals_original"] = len(sent) == 1 and sent[0] == query
        if expect.get("head_retained"):
            res.checks["head_retained"] = bool(sent) and sent[0].startswith(query[:24])
        if expect.get("tail_retained"):
            res.checks["tail_retained"] = bool(sent) and sent[0].endswith(query[-24:])
        if expect.get("prepared_tokens_max") and sent and embed_cfg:
            tok = _token_count(sent[0], embed_cfg)
            res.observed["sent_tokens"] = tok
            res.checks["prepared_tokens_max"] = tok <= expect["prepared_tokens_max"]
        if expect.get("no_provider_error"):
            res.checks["no_provider_error"] = res.observed["raised"] is None
        if expect.get("repeat_of"):
            prev = by_id.get(expect["repeat_of"])
            if prev is None:
                res.checks["repeat_of_available"] = False
            else:
                res.checks["additional_provider_calls"] = (
                    res.observed["provider_query_calls"]
                    - prev.observed["provider_query_calls"]
                    == expect.get("additional_provider_calls", 0))
                if expect.get("same_hit_ids"):
                    res.checks["same_hit_ids"] = res.observed["hit_ids"] == prev.observed["hit_ids"]

        if case["mode"] == "response":
            res.verdict = "READY_FOR_CANARY_JUDGEMENT"
        elif res.checks:
            res.verdict = "PASS" if all(res.checks.values()) else "FAIL"
        else:
            res.verdict = "NO_CHECKS"
        results.append(res)
        by_id[res.case_id] = res

    summary = {
        "cases": len(results),
        "selected": [c["id"] for c in selected],
        "pass": sum(1 for r in results if r.verdict == "PASS"),
        "fail": sum(1 for r in results if r.verdict == "FAIL"),
        "ready_for_canary_judgement": sum(1 for r in results
                                          if r.verdict == "READY_FOR_CANARY_JUDGEMENT"),
        "no_checks": sum(1 for r in results if r.verdict == "NO_CHECKS"),
        "profile": profile,
        "config_path": config_path,
    }
    return {"summary": summary,
            "results": [{"case_id": r.case_id, "mode": r.mode, "category": r.category,
                         "query_chars": r.query_chars, "verdict": r.verdict,
                         "checks": r.checks, "observed": r.observed} for r in results]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Q01 frozen query canary")
    parser.add_argument("--list", action="store_true", help="print the frozen case set and exit")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--config", default=None, help="explicit config.yaml path")
    parser.add_argument("--only", default=None,
                        help="comma-separated case ids (e.g. Q01-01,Q01-15)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(
            [{"id": c["id"], "mode": c["mode"], "category": c["category"],
              "query": c.get("query") or f"<{c['query_ref']}>"} for c in CASES],
            ensure_ascii=False, indent=2))
        return 0

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    report = run_cases(args.profile, args.config, only)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"Q01 report -> {args.out}")
    s = report["summary"]
    print(f"Q01: {s['pass']}/{s['cases']} PASS, {s['fail']} FAIL, "
          f"{s['ready_for_canary_judgement']} awaiting canary judgement")
    return 0 if s["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
