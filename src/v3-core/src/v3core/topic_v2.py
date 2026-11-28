# -*- coding: utf-8 -*-
"""v3core.topic_v2 — Codex-style two-phase structured topic consolidation & freshness guard.

Design goals (see docs/DESIGN-PHILOSOPHY.md):
  1. Zero HDBSCAN / zero brittle cosine clustering cutoffs (e.g. ``SEED_COSINE=0.65``).
     Routing uses scale-free vector Top-K candidate matching + optional lexical tie-break.
  2. Source-preserving: raw ``qa_pairs`` are never rewritten or deleted. Every structured
     claim carries explicit ``evidence_qa_ids``; superseded conclusions remain auditable in
     ``superseded_claims``.
  3. Two-phase write side (``TopicConsolidatorV2``):
       - Phase 1 (``distill_qa``): distills one raw QA into a structured delta note
         (``add`` / ``problem`` / ``evolve`` / ``correct`` / ``historical_note`` / ``skip``)
         against Top-K existing topic dossiers. Supports both real ``llm_gateway`` JSON
         mode and deterministic bilingual offline extraction (tests / zero-API CI).
       - Phase 2 (``apply_note``): mutates the target ``TopicDossier`` so
         ``current_claims`` ONLY holds active truths, obsolete claims move into
         ``superseded_claims``, ``last_evidence_qa_id`` watermark advances, and topic
         embeddings are recomputed from active state only.
  4. Read-side freshness & supersession guard (``apply_topic_v2_ranking_guard``):
       - Parses and strips ``<!-- topic_v2:... -->`` metadata before prompt injection.
       - Enforces LLM-established explicit relations ONLY (zero read-side keyword or
         bigram topic-overlap guessing):
           * On current-state queries, demotes ``qa`` hits in persisted ``superseded_qas``
             and Topic V2 dossiers whose ``active_qas`` were superseded by another dossier.
           * Preserves derived ``note``/``yin`` hits (fail-safe: whole multi-topic notes
             are never demoted merely from coarse ``source_qa_range`` intervals; they rely
             on UTC half-life decay until paragraph-level provenance lands).
           * Breaks non-superseded QA ties via recency without hiding unique old facts.
       - On retrospective ``why did we drop X`` queries, preserves raw historical QA
         evidence and suppresses recency decay.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger("v3core.topic_v2")


_TOPIC_V2_HEADER_RE = re.compile(r"^\s*<!--\s*topic_v2:(\{[^\n]*?\})\s*-->\s*\n?")

_HISTORICAL_QUERY_PATTERNS = (
    re.compile(r"(以前|早期|之前|当初|曾经|原来|早先).{0,16}(为什么|为何|怎么).{0,16}(放弃|淘汰|移除|下线|废除|替换|弃用)"),
    re.compile(r"(为什么|为何).{0,12}(下线|放弃|淘汰|移除|废除|弃用)"),
    re.compile(r"\bwhy\b.{0,28}\b(drop|dropped|remove|removed|replace|replaced|deprecate|deprecated|sunset|abandon|abandoned)\b", re.I),
    re.compile(r"\b(previously|originally|earlier|used to)\b.{0,24}\b(why|drop|dropped|remove|removed|replaced)\b", re.I),
)

_CURRENT_STATE_OVERRIDE_RE = re.compile(r"(现在采用|目前决定|当前确定|当前采用|现在用什么|currently use|right now)", re.I)

# General bilingual A -> B replacement / evolution patterns (used only when llm_fn is None).
_EVOLVE_ZH_RE = re.compile(
    r"(?:正式)?(?:废弃|放弃|淘汰|替换掉|移除|下线|停用)\s*(?P<old>[^，。,;\n]+?)\s*[，,]\s*"
    r"(?:全面)?(?:改为|改用|切换为|切换到|升级为|迁移至|迁移到|采用)\s*(?P<new>[^，。,;\n]+)"
)
_EVOLVE_EN_RE = re.compile(
    r"\b(?:replaced|switched\s+from|migrated\s+from|deprecated)\s+(?P<old>[^,.;\n]+?)\s+"
    r"(?:with|to|for|in\s+favor\s+of)\s+(?P<new>[^,.;\n]+)",
    re.I,
)

# General bilingual user-correction / prohibition patterns (used only when llm_fn is None).
_CORRECT_ZH_RE = re.compile(
    r"(?:明确纠正|纠正一下|更正|以后一律|从现在起)?[：:，,]?\s*(?:禁止|不要再用|取消|废止)\s*(?P<old>[^，。,;\n]+?)\s*[，,]\s*"
    r"(?P<new>(?:必须|一律|改用|改为|统一|严格遵守)[^。；;\n]+)"
)
_CORRECT_EN_RE = re.compile(
    r"\b(?:stop\s+using|do\s+not\s+use|don't\s+use|forbid|ban)\s+(?P<old>[^,.;\n]+?)[,;.]\s*"
    r"(?P<new>(?:must|always|use|switch\s+to|follow)[^.;\n]+)",
    re.I,
)

# General bilingual historical postmortem patterns.
_HIST_ZH_RE = re.compile(
    r"因为\s*(?P<old>[A-Za-z0-9_.-]+|\S+?)\s+(?P<reason>[^，。,;\n]+?)\s*[，,]\s*(?:因此|所以).{0,16}(?:下线|放弃|淘汰|移除|停用)"
)
_HIST_EN_RE = re.compile(
    r"\b(?:because|since)\s+(?P<old>[A-Za-z0-9_.-]+)\s+(?P<reason>[^,.;\n]+?)[,;]\s*(?:it\s+was\s+|we\s+)?(?:dropped|removed|retired|deprecated)",
    re.I,
)

_PROBLEM_HINT_RE = re.compile(
    r"(锁库|不持久化|误拦|空轮询|阻塞|漏拷|卡顿|超时|失败|问题|故障|缺陷|无法满足|bug|crash|deadlock|stall|fail)",
    re.I,
)

_ASCII_CODE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+|[A-Za-z0-9_]{3,}")
_ZH_WORD_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

_TRIVIAL_NOISE_WORD = (
    r"(?:好的|收到|谢谢|感谢|明白(?:了)?|了解(?:了)?|嗯嗯?|哈哈+|哦哦?|行|没问题|ok|okay|thanks|thank you|thx|got it|cool|nice|hi|hello|你好)"
)
_TRIVIAL_NOISE_RE = re.compile(
    rf"^{_TRIVIAL_NOISE_WORD}(?:[!！。.,，~～\s]+{_TRIVIAL_NOISE_WORD})*[!！。.,，~～\s]*$",
    re.I,
)

_PHASE1_SYSTEM_PROMPT = (
    "你是长期记忆系统中的两阶段主题整理器（Phase 1）。\n"
    "给定一条新对话 QA 与向量检索得到的 Top-K 候选主题摘要（含 active_claims），请输出严格 JSON（不要 Markdown 代码块）：\n"
    "{\n"
    '  "action": "attach" | "create" | "skip",\n'
    '  "target_topic_id": "若 attach 填候选 topic_id，否则留空",\n'
    '  "topic_title": "简短主题名",\n'
    '  "update_type": "add" | "problem" | "evolve" | "correct" | "historical_note",\n'
    '  "claim_text": "当前生效的简洁结论，仅写新状态，不要把已废弃旧方案写进来",\n'
    '  "replaces_claim_ids": ["若 evolve/correct，可选填被替代的旧 claim_id 列表"],\n'
    '  "replaces_text": "若 update_type 为 evolve/correct/historical_note，填写被替代或已下线的旧方案标识",\n'
    '  "reason": "变更/替代/下线原因",\n'
    '  "keywords": ["关键词1", "关键词2"]\n'
    "}\n"
    "规则：1) 发生 A->B 演进或纠正时必须标为 evolve/correct，优先填写 replaces_claim_ids 并填 replaces_text；\n"
    "2) 回溯历史下线原因时标为 historical_note，不得污染当前结论；\n"
    "3) claim_text 只陈述真实明确信息，不杜撰；\n"
    "4) 若当前 QA 只是零碎上下文或子模块代号、缺乏独立主题价值，优先 attach 到最相关的候选主题或 skip，避免拆出碎小主题。"
)


def cosine_similarity(a: Sequence[float] | None, b: Sequence[float] | None) -> float:
    """Return cosine similarity in ``[-1, 1]`` (``0.0`` for empty/mismatched vectors)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        na += fx * fx
        nb += fy * fy
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float(dot / (math.sqrt(na) * math.sqrt(nb)))


def _slugify_topic_id(title: str, qa_id: int) -> str:
    ascii_parts = re.findall(r"[a-z0-9]+", str(title or "").lower())
    stem = "_".join(ascii_parts[:4])[:28] or f"q{qa_id}"
    return f"tv2_{stem}_{qa_id}"


def _clean_clause(text: str) -> str:
    s = str(text or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = s.rstrip("。.!！;；,，")
    s = re.sub(r"\s*(?:作为唯一当前方案|作为当前方案|作为最终方案)$", "", s).strip()
    return s


def _extract_tokens(text: str) -> set[str]:
    low = str(text or "").lower()
    tokens = {m.lower() for m in _ASCII_CODE_TOKEN_RE.findall(str(text or ""))}
    for run in _ZH_WORD_RUN_RE.findall(low):
        if len(run) <= 6:
            tokens.add(run)
        else:
            for i in range(len(run) - 1):
                tokens.add(run[i : i + 2])
    return tokens


@dataclass
class TopicClaim:
    claim_id: str
    text: str
    kind: str = "fact"  # fact | decision | rule | problem | legacy_seed
    evidence_qa_ids: list[int] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "kind": self.kind,
            "evidence_qa_ids": list(self.evidence_qa_ids),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TopicClaim":
        return cls(
            claim_id=str(data.get("claim_id") or ""),
            text=str(data.get("text") or ""),
            kind=str(data.get("kind") or "fact"),
            evidence_qa_ids=[int(x) for x in (data.get("evidence_qa_ids") or [])],
            updated_at=str(data.get("updated_at") or ""),
        )


@dataclass
class SupersededClaim:
    text: str
    replaced_by_claim_id: str = ""
    reason: str = ""
    superseded_at: str = ""
    evidence_qa_ids: list[int] = field(default_factory=list)
    claim_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "text": self.text,
            "replaced_by_claim_id": self.replaced_by_claim_id,
            "reason": self.reason,
            "superseded_at": self.superseded_at,
            "evidence_qa_ids": list(self.evidence_qa_ids),
        }
        if self.claim_id:
            out["claim_id"] = self.claim_id
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SupersededClaim":
        return cls(
            text=str(data.get("text") or ""),
            replaced_by_claim_id=str(data.get("replaced_by_claim_id") or ""),
            reason=str(data.get("reason") or ""),
            superseded_at=str(data.get("superseded_at") or ""),
            evidence_qa_ids=[int(x) for x in (data.get("evidence_qa_ids") or [])],
            claim_id=str(data.get("claim_id") or ""),
        )


@dataclass
class TopicDossier:
    topic_id: str
    title: str
    summary: str = ""
    current_claims: list[TopicClaim] = field(default_factory=list)
    superseded_claims: list[SupersededClaim] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    last_evidence_qa_id: int = 0
    last_evidence_at: str = ""
    status: str = "active"
    embed_model: str = ""
    embedding: list[float] | None = None

    def active_qa_ids(self) -> list[int]:
        seen: set[int] = set()
        out: list[int] = []
        for c in self.current_claims:
            for qid in c.evidence_qa_ids:
                iq = int(qid)
                if iq not in seen:
                    seen.add(iq)
                    out.append(iq)
        return sorted(out)

    def superseded_qa_ids(self) -> list[int]:
        active = set(self.active_qa_ids())
        out: set[int] = set()
        for s in self.superseded_claims:
            for qid in s.evidence_qa_ids:
                iq = int(qid)
                if iq not in active:
                    out.add(iq)
        return sorted(out)

    def embedding_text(self) -> str:
        """Text indexed for vector retrieval: ONLY active claims + title/summary/keywords."""
        parts = [self.title, self.summary]
        parts.extend(c.text for c in self.current_claims if c.text)
        if self.keywords:
            parts.append(" ".join(self.keywords))
        return "\n".join(p for p in parts if p)

    def render_current_body(self, include_header: bool = True) -> str:
        lines: list[str] = []
        for c in self.current_claims:
            if not c.text:
                continue
            ev = ", ".join(f"qa_{q}" for q in c.evidence_qa_ids)
            if ev:
                lines.append(f"- 当前结论：{c.text} [证据: {ev}]")
            else:
                lines.append(f"- 当前结论：{c.text}")
        if not lines and self.summary:
            lines.append(f"- 当前结论：{self.summary}")
        body = "\n".join(lines)
        if include_header:
            return f"{encode_topic_v2_header(self)}\n{body}" if body else encode_topic_v2_header(self)
        return body

    def render_history_block(self) -> str:
        lines: list[str] = []
        for s in self.superseded_claims:
            if not s.text and not s.reason:
                continue
            ev = ", ".join(f"qa_{q}" for q in s.evidence_qa_ids)
            suffix = f" [证据: {ev}]" if ev else ""
            why = f"（原因：{s.reason}）" if s.reason else ""
            lines.append(f"- 已废止：{s.text}{why}{suffix}")
        return "\n".join(lines)

    def to_topic_row(self, embed_fn: Callable[[str], list[float]] | None = None) -> dict[str, Any]:
        emb = self.embedding
        if emb is None and embed_fn is not None:
            emb = embed_fn(self.embedding_text())
            self.embedding = emb
        return {
            "topic_id": self.topic_id,
            "title": self.title,
            "summary": self.summary,
            "body": self.render_current_body(include_header=True),
            "keywords": list(self.keywords),
            "status": self.status,
            "embed_model": self.embed_model,
            "embedding": emb,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "title": self.title,
            "summary": self.summary,
            "current_claims": [c.to_dict() for c in self.current_claims],
            "superseded_claims": [s.to_dict() for s in self.superseded_claims],
            "keywords": list(self.keywords),
            "last_evidence_qa_id": int(self.last_evidence_qa_id),
            "last_evidence_at": self.last_evidence_at,
            "status": self.status,
            "embed_model": self.embed_model,
            "embedding": list(self.embedding) if self.embedding is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TopicDossier":
        raw_emb = data.get("embedding")
        return cls(
            topic_id=str(data.get("topic_id") or ""),
            title=str(data.get("title") or ""),
            summary=str(data.get("summary") or ""),
            current_claims=[TopicClaim.from_dict(c) for c in (data.get("current_claims") or [])],
            superseded_claims=[SupersededClaim.from_dict(s) for s in (data.get("superseded_claims") or [])],
            keywords=[str(k) for k in (data.get("keywords") or [])],
            last_evidence_qa_id=int(data.get("last_evidence_qa_id") or 0),
            last_evidence_at=str(data.get("last_evidence_at") or ""),
            status=str(data.get("status") or "active"),
            embed_model=str(data.get("embed_model") or ""),
            embedding=[float(x) for x in raw_emb] if raw_emb is not None else None,
        )


@dataclass(frozen=True)
class Phase1Note:
    action: str  # attach | create | skip
    target_topic_id: str
    topic_title: str
    update_type: str  # add | problem | evolve | correct | historical_note | skip
    claim_text: str
    replaces_text: str
    reason: str
    keywords: list[str]
    evidence_qa_id: int
    timestamp: str
    replaces_claim_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConsolidationReport:
    processed_qas: int
    created_topics: int
    updated_topics: int
    superseded_claims: int
    skipped_qas: int
    max_qa_id: int
    failed_qa_ids: tuple[int, ...] = ()


def encode_topic_v2_header(dossier: TopicDossier) -> str:
    """Encode compact freshness + explicit lineage metadata under ``TopicRecall``'s 500-char slice."""
    sup_all = dossier.superseded_qa_ids()[-32:]
    payload: dict[str, Any] = {
        "v": 2,
        "last_qa": int(dossier.last_evidence_qa_id),
        "last_at": str(dossier.last_evidence_at or "")[:32],
        "active_qas": dossier.active_qa_ids()[-8:],
        "superseded_qas": sup_all,
    }
    encoded = f"<!-- topic_v2:{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)} -->"
    while len(encoded) > 460 and len(payload["superseded_qas"]) > 12:
        payload["superseded_qas"] = payload["superseded_qas"][-max(12, len(payload["superseded_qas"]) // 2):]
        encoded = f"<!-- topic_v2:{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)} -->"
    return encoded


def parse_topic_v2_header(text: str) -> dict[str, Any] | None:
    """Parse ``<!-- topic_v2:{...} -->`` metadata prefix, or ``None`` for legacy topics."""
    if not text:
        return None
    m = _TOPIC_V2_HEADER_RE.match(str(text))
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def strip_topic_v2_header(text: str) -> str:
    """Remove leading ``<!-- topic_v2:... -->`` comment before prompt rendering."""
    if not text:
        return ""
    return _TOPIC_V2_HEADER_RE.sub("", str(text), count=1).lstrip("\r\n")


def is_historical_trace_query(query: str) -> bool:
    """Detect retrospective 'why did we drop/replace X previously' queries."""
    q = str(query or "").strip()
    if not q:
        return False
    if _CURRENT_STATE_OVERRIDE_RE.search(q) and not any(
        w in q.lower() for w in ("以前为什么", "早期为什么", "之前为什么", "当初为什么", "why did")
    ):
        return False
    return any(pat.search(q) for pat in _HISTORICAL_QUERY_PATTERNS)


def route_candidate_topics(
    query_emb: Sequence[float] | None,
    dossiers: Sequence[TopicDossier],
    top_k: int = 5,
    *,
    query_text: str = "",
) -> list[tuple[float, TopicDossier]]:
    """Scale-free Top-K candidate routing (zero hardcoded cosine threshold)."""
    if not dossiers or top_k <= 0:
        return []
    q_toks = _extract_tokens(query_text) if query_text else set()
    q_codes = {m.lower() for m in _ASCII_CODE_TOKEN_RE.findall(str(query_text or ""))} if query_text else set()
    scored: list[tuple[float, TopicDossier]] = []
    for dossier in dossiers:
        if dossier.status != "active":
            continue
        sim = cosine_similarity(query_emb, dossier.embedding)
        d_text = dossier.embedding_text()
        d_hist = dossier.render_history_block()
        d_toks = _extract_tokens(d_text)
        # Small lexical & distinctive entity-code tie-break (useful for short bilingual CJK/code strings).
        lex_bonus = 0.0
        if q_toks and d_toks:
            overlap = len(q_toks & d_toks)
            lex_bonus += 0.08 * min(overlap, 4)
        if q_codes:
            d_codes = {
                m.lower()
                for m in _ASCII_CODE_TOKEN_RE.findall(f"{dossier.title}\n{d_text}\n{d_hist}")
            }
            code_overlap = len(q_codes & d_codes)
            if code_overlap:
                lex_bonus += 0.22 * min(code_overlap, 3)
        score = sim + lex_bonus
        scored.append((round(score, 6), dossier))
    scored.sort(key=lambda pair: (-pair[0], pair[1].topic_id))
    return scored[:top_k]


def save_dossiers(path: str | Path, dossiers: Sequence[TopicDossier]) -> None:
    """Persist structured topic dossiers atomically as UTF-8 JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "topics": [d.to_dict() for d in dossiers],
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def load_dossiers(path: str | Path) -> list[TopicDossier]:
    """Load persisted topic dossiers from JSON (returns ``[]`` if missing)."""
    target = Path(path)
    if not target.is_file():
        return []
    raw = json.loads(target.read_text(encoding="utf-8"))
    items = raw.get("topics") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [TopicDossier.from_dict(item) for item in items if isinstance(item, dict)]


@dataclass
class GatewayLLMStats:
    """Secret-free invocation and token telemetry for gateway-backed Topic V2."""

    calls: int = 0
    succeeded: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_latency_s: float = 0.0
    adaptive_fallbacks: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": int(self.calls),
            "succeeded": int(self.succeeded),
            "failed": int(self.failed),
            "prompt_tokens": int(self.prompt_tokens),
            "completion_tokens": int(self.completion_tokens),
            "total_tokens": int(self.total_tokens),
            "total_latency_s": round(float(self.total_latency_s), 3),
            "adaptive_fallbacks": int(self.adaptive_fallbacks),
            "errors": list(self.errors[-10:]),
        }


class GatewayLLMAdapter:
    """Callable wrapper around ``v3core.llm_gateway.invoke_simple`` with telemetry."""

    def __init__(
        self,
        llm_cfg: Mapping[str, Any],
        *,
        cfg: Any = None,
        max_tokens: int = 800,
        temperature: float = 0.1,
        timeout: float = 60.0,
    ) -> None:
        self.llm_cfg = dict(llm_cfg)
        self.cfg = cfg if cfg is not None else self.llm_cfg.get("config")
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)
        self.timeout = float(timeout)
        self.stats = GatewayLLMStats()

    def __call__(self, messages: list[dict[str, str]]) -> str:
        from .llm_gateway import invoke_simple

        self.stats.calls += 1
        res = invoke_simple(
            self.llm_cfg,
            messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            timeout=self.timeout,
            cfg=self.cfg,
        )
        self.stats.total_latency_s += float(getattr(res, "latency", 0.0) or 0.0)
        if getattr(res, "adaptive_fallback_used", False):
            self.stats.adaptive_fallbacks += 1
        usage = getattr(res, "usage", None) or {}
        if isinstance(usage, Mapping):
            pt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            ct = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            tt = int(usage.get("total_tokens") or (pt + ct))
            self.stats.prompt_tokens += pt
            self.stats.completion_tokens += ct
            self.stats.total_tokens += tt
        if not res.ok:
            self.stats.failed += 1
            err_msg = str(res.error or "topic_v2 llm call failed")[:180]
            self.stats.errors.append(err_msg)
            raise RuntimeError(err_msg)
        self.stats.succeeded += 1
        return str(res.text or "")


def make_gateway_llm_fn(
    cfg: Any = None,
    *,
    llm_cfg: Mapping[str, Any] | None = None,
    max_tokens: int = 800,
    temperature: float = 0.1,
    timeout: float = 60.0,
) -> GatewayLLMAdapter:
    """Build a telemetry-equipped ``llm_fn(messages) -> str`` backed by ``llm_gateway``."""
    from .llm_gateway import resolve_simple_llm_cfg

    resolved = dict(llm_cfg) if llm_cfg is not None else resolve_simple_llm_cfg(cfg)
    if not resolved:
        raise RuntimeError("topic_v2: LLM configuration or API key is missing")
    return GatewayLLMAdapter(
        resolved,
        cfg=cfg,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )


def _format_iso_ts(raw: Any) -> str:
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(raw or "")


def _extract_json_object(raw_text: str) -> dict[str, Any] | None:
    s = str(raw_text or "").strip()
    if not s:
        return None
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
        s = s.strip()
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _derive_topic_title(question: str, answer: str) -> str:
    q = str(question or "").strip()
    m = re.match(r"^(?:关于|对于|针对)\s*([^，。,？?\n]+)", q)
    if m and len(m.group(1).strip()) >= 2:
        return _clean_clause(m.group(1))[:40]
    m2 = re.search(r"[，,]\s*([^，。,？?\n]+?)(?:最新确认|现在|目前|当前|以后)", q)
    if m2 and len(m2.group(1).strip()) >= 2:
        return _clean_clause(m2.group(1))[:40]
    cleaned = re.sub(r"[？?！!。]+$", "", q)
    return (cleaned or str(answer or ""))[:40]


class TopicConsolidatorV2:
    """Codex-style two-phase topic consolidator (Phase 1 distill + Phase 2 merge/supersede)."""

    def __init__(
        self,
        *,
        embed_fn: Callable[[str], list[float]],
        llm_fn: Callable[[list[dict[str, str]]], str] | None = None,
        top_k_candidates: int = 5,
        embed_model: str = "",
    ) -> None:
        self.embed_fn = embed_fn
        self.llm_fn = llm_fn
        self.top_k_candidates = max(int(top_k_candidates), 1)
        self.embed_model = str(embed_model or "").strip()

    def seed_from_legacy_topics(self, legacy_topics: Sequence[Mapping[str, Any]]) -> list[TopicDossier]:
        """Convert legacy ``topics`` rows into initial ``TopicDossier`` instances.

        Preserves legacy vocabulary bridges (``summary`` / ``body`` / ``keywords``) at
        watermark ``last_evidence_qa_id=0`` so subsequent ``evolve`` / ``correct`` notes
        selectively supersede matching claims while preserving unrelated legacy facts.
        """
        dossiers: list[TopicDossier] = []
        for t in legacy_topics:
            tid = str(t.get("topic_id") or "")
            if not tid:
                continue
            title = str(t.get("title") or "")
            summary = str(t.get("summary") or "").strip()
            body = strip_topic_v2_header(str(t.get("body") or "")).strip()
            # Keep detailed body clauses alongside summary without dropping facts.
            combined_parts: list[str] = []
            if summary and summary not in body:
                combined_parts.append(summary)
            if body:
                combined_parts.append(body)
            raw_seed_text = "\n".join(combined_parts) or summary
            clauses: list[str] = []
            seen_clauses: set[str] = set()
            for part in re.split(r"[\n。；;]+", raw_seed_text):
                cleaned = re.sub(r"^\s*[-*•]\s*", "", part).strip()
                cleaned = re.sub(r"^当前结论[：:]\s*", "", cleaned).strip()
                cleaned = re.sub(r"\s*\[证据:[^\]]+\]\s*$", "", cleaned).strip()
                if len(cleaned) < 3:
                    continue
                low = cleaned.lower()
                if low not in seen_clauses:
                    seen_clauses.add(low)
                    clauses.append(cleaned)
            claims: list[TopicClaim] = []
            if clauses:
                for idx, clause in enumerate(clauses[:12], start=1):
                    cid = f"c_seed_{tid}" if idx == 1 and len(clauses) == 1 else f"c_seed_{tid}_{idx}"
                    claims.append(
                        TopicClaim(
                            claim_id=cid,
                            text=clause,
                            kind="legacy_seed",
                            evidence_qa_ids=[],
                        )
                    )
            elif raw_seed_text:
                claims.append(
                    TopicClaim(
                        claim_id=f"c_seed_{tid}",
                        text=raw_seed_text,
                        kind="legacy_seed",
                        evidence_qa_ids=[],
                    )
                )
            seed_summary = summary or (clauses[0][:240] if clauses else raw_seed_text[:240])
            raw_emb = t.get("embedding")
            emb = [float(x) for x in raw_emb] if raw_emb is not None else None
            dossier = TopicDossier(
                topic_id=tid,
                title=title,
                summary=seed_summary,
                current_claims=claims,
                superseded_claims=[],
                keywords=[str(k) for k in (t.get("keywords") or [])],
                last_evidence_qa_id=0,
                last_evidence_at="",
                status=str(t.get("status") or "active"),
                embed_model=str(t.get("embed_model") or ""),
                embedding=emb,
            )
            if dossier.embedding is None and self.embed_fn is not None:
                new_emb = self.embed_fn(dossier.embedding_text())
                if new_emb:
                    dossier.embedding = new_emb
                    if self.embed_model:
                        dossier.embed_model = self.embed_model
            dossiers.append(dossier)
        return dossiers

    def _pick_target_candidate(
        self,
        qa_text: str,
        candidates: list[tuple[float, TopicDossier]],
        *,
        replaces_text: str = "",
    ) -> TopicDossier | None:
        if not candidates:
            return None
        qa_codes = {m.lower() for m in _ASCII_CODE_TOKEN_RE.findall(qa_text)}
        rep_low = replaces_text.strip().lower()
        qa_low = qa_text.lower()

        best_score = -1.0
        best_dossier: TopicDossier | None = None
        for sim_score, dossier in candidates:
            d_all = f"{dossier.title}\n{dossier.embedding_text()}\n{dossier.render_history_block()}"
            d_low = d_all.lower()
            d_codes = {m.lower() for m in _ASCII_CODE_TOKEN_RE.findall(d_all)}

            strong_match = False
            if rep_low and rep_low in d_low:
                strong_match = True
            elif dossier.title and len(dossier.title) >= 4 and dossier.title.lower() in qa_low:
                strong_match = True
            elif qa_codes & d_codes:
                strong_match = True

            # Require either explicit entity/title grounding OR solid semantic alignment.
            if strong_match or sim_score >= 0.42:
                if sim_score > best_score:
                    best_score = sim_score
                    best_dossier = dossier
        return best_dossier

    def distill_qa(
        self,
        qa: Mapping[str, Any],
        dossiers: Sequence[TopicDossier],
    ) -> Phase1Note | None:
        """Phase 1: Distill one QA pair against Top-K candidate topic dossiers."""
        note, _failed = self._distill_qa_with_status(qa, dossiers)
        return note

    def _distill_qa_with_status(
        self,
        qa: Mapping[str, Any],
        dossiers: Sequence[TopicDossier],
    ) -> tuple[Phase1Note | None, bool]:
        """Distill one QA and return ``(note, failed)`` where ``failed=True`` blocks watermark advance."""
        qa_id = int(qa.get("id") or 0)
        question = str(qa.get("question") or "").strip()
        answer = str(qa.get("answer") or "").strip()
        ts = _format_iso_ts(qa.get("timestamp"))
        if not question and not answer:
            return None, False

        qa_text = f"{question}\n{answer}".strip()
        qa_emb = qa.get("embedding")
        if qa_emb is None:
            qa_emb = self.embed_fn(qa_text)
        candidates = route_candidate_topics(
            qa_emb, dossiers, top_k=self.top_k_candidates, query_text=qa_text
        )

        if self.llm_fn is not None:
            note = self._distill_with_llm(qa_id, question, answer, ts, candidates)
            return (note, note is None)
        return self._distill_deterministic(qa_id, question, answer, ts, qa_text, candidates), False

    def _select_active_claims_for_prompt(
        self,
        dossier: TopicDossier,
        query_text: str,
        limit: int = 16,
    ) -> list[dict[str, str]]:
        claims = [c for c in dossier.current_claims if c.text]
        if len(claims) <= limit:
            return [{"claim_id": c.claim_id, "text": c.text} for c in claims]
        q_toks = _extract_tokens(query_text)
        scored: list[tuple[int, int, TopicClaim]] = []
        for idx, c in enumerate(claims):
            overlap = len(q_toks & _extract_tokens(c.text)) if q_toks else 0
            scored.append((overlap, idx, c))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        picked = sorted(scored[:limit], key=lambda item: item[1])
        return [{"claim_id": c.claim_id, "text": c.text} for _, _, c in picked]

    def _distill_with_llm(
        self,
        qa_id: int,
        question: str,
        answer: str,
        ts: str,
        candidates: list[tuple[float, TopicDossier]],
    ) -> Phase1Note | None:
        qa_text = f"{question}\n{answer}"
        cand_desc = [
            {
                "topic_id": d.topic_id,
                "title": d.title,
                "summary": d.summary,
                "active_claims": self._select_active_claims_for_prompt(d, qa_text, limit=16),
                "keywords": d.keywords[:8],
                "similarity": round(score, 4),
            }
            for score, d in candidates
        ]
        user_prompt = json.dumps(
            {
                "qa_id": qa_id,
                "timestamp": ts,
                "question": question,
                "answer": answer,
                "candidate_topics": cand_desc,
            },
            ensure_ascii=False,
        )
        try:
            raw = self.llm_fn(
                [
                    {"role": "system", "content": _PHASE1_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ]
            )
        except Exception as exc:
            logger.warning("topic_v2: Phase 1 LLM call failed for qa_id=%s: %s", qa_id, str(exc)[:140])
            return None
        data = _extract_json_object(raw)
        if not data:
            return None

        raw_action = data.get("action")
        if not isinstance(raw_action, str) or not raw_action.strip():
            logger.warning("topic_v2: Phase 1 JSON missing explicit action for qa_id=%s", qa_id)
            return None
        action = raw_action.strip().lower()
        if action not in ("attach", "create", "skip"):
            logger.warning("topic_v2: Phase 1 JSON invalid action=%r for qa_id=%s", raw_action, qa_id)
            return None

        raw_utype = data.get("update_type")
        if action == "skip":
            update_type = str(raw_utype or "skip").strip().lower() or "skip"
            if update_type != "skip":
                logger.warning("topic_v2: Phase 1 skip JSON has non-skip update_type=%r for qa_id=%s", raw_utype, qa_id)
                return None
        else:
            if not isinstance(raw_utype, str) or not raw_utype.strip():
                logger.warning("topic_v2: Phase 1 JSON missing explicit update_type for qa_id=%s", qa_id)
                return None
            update_type = raw_utype.strip().lower()
            if update_type not in ("add", "problem", "evolve", "correct", "historical_note"):
                logger.warning("topic_v2: Phase 1 JSON invalid update_type=%r for qa_id=%s", raw_utype, qa_id)
                return None

        valid_ids = {d.topic_id for _, d in candidates}
        target_id = str(data.get("target_topic_id") or "").strip()
        if action == "attach" and target_id not in valid_ids:
            logger.warning(
                "topic_v2: Phase 1 attach JSON has invalid target_topic_id=%r (valid=%s) for qa_id=%s",
                target_id,
                sorted(valid_ids),
                qa_id,
            )
            return None

        kws = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]
        raw_rep_ids = data.get("replaces_claim_ids")
        rep_ids = (
            tuple(str(cid).strip() for cid in raw_rep_ids if str(cid).strip())
            if isinstance(raw_rep_ids, (list, tuple))
            else ()
        )
        claim_text = _clean_clause(str(data.get("claim_text") or ""))
        replaces_text = _clean_clause(str(data.get("replaces_text") or ""))
        reason = _clean_clause(str(data.get("reason") or ""))

        if action != "skip":
            if update_type == "historical_note":
                if not (reason or claim_text):
                    logger.warning(
                        "topic_v2: Phase 1 historical_note missing reason/claim_text for qa_id=%s",
                        qa_id,
                    )
                    return None
            else:
                if not claim_text:
                    logger.warning(
                        "topic_v2: Phase 1 JSON missing non-empty claim_text for qa_id=%s (action=%s update_type=%s)",
                        qa_id,
                        action,
                        update_type,
                    )
                    return None
                if update_type in ("evolve", "correct") and not (rep_ids or replaces_text):
                    logger.warning(
                        "topic_v2: Phase 1 %s JSON missing replaces_claim_ids/replaces_text for qa_id=%s",
                        update_type,
                        qa_id,
                    )
                    return None

        return Phase1Note(
            action=action,
            target_topic_id=target_id,
            topic_title=str(data.get("topic_title") or _derive_topic_title(question, answer)).strip(),
            update_type=update_type,
            claim_text=claim_text,
            replaces_text=replaces_text,
            reason=reason,
            keywords=kws,
            evidence_qa_id=qa_id,
            timestamp=ts,
            replaces_claim_ids=rep_ids,
        )

    def _distill_deterministic(
        self,
        qa_id: int,
        question: str,
        answer: str,
        ts: str,
        qa_text: str,
        candidates: list[tuple[float, TopicDossier]],
    ) -> Phase1Note:
        title_hint = _derive_topic_title(question, answer)

        # 1. Explicit bilingual user correction / rule override (checked before evolve).
        m_corr = _CORRECT_ZH_RE.search(answer) or _CORRECT_EN_RE.search(answer)
        if m_corr:
            old_tok = _clean_clause(m_corr.group("old"))
            new_rule = _clean_clause(m_corr.group("new"))
            target = self._pick_target_candidate(qa_text, candidates, replaces_text=old_tok)
            new_codes = _ASCII_CODE_TOKEN_RE.findall(new_rule)
            prefix = target.title if target else title_hint
            return Phase1Note(
                action="attach" if target else "create",
                target_topic_id=target.topic_id if target else "",
                topic_title=prefix,
                update_type="correct",
                claim_text=f"{prefix}：{new_rule}",
                replaces_text=old_tok,
                reason=f"用户纠正：{new_rule}",
                keywords=new_codes,
                evidence_qa_id=qa_id,
                timestamp=ts,
            )

        # 2. Explicit A -> B architecture/decision evolution.
        m_evo = _EVOLVE_ZH_RE.search(answer) or _EVOLVE_EN_RE.search(answer)
        if m_evo:
            old_tok = _clean_clause(m_evo.group("old"))
            new_tok = _clean_clause(m_evo.group("new"))
            target = self._pick_target_candidate(qa_text, candidates, replaces_text=old_tok)
            new_codes = _ASCII_CODE_TOKEN_RE.findall(new_tok)
            prefix = target.title if target else title_hint
            return Phase1Note(
                action="attach" if target else "create",
                target_topic_id=target.topic_id if target else "",
                topic_title=prefix,
                update_type="evolve",
                claim_text=f"{prefix} 当前采用 {new_tok}",
                replaces_text=old_tok,
                reason=f"方案演进为 {new_tok}",
                keywords=new_codes,
                evidence_qa_id=qa_id,
                timestamp=ts,
            )

        # 3. Historical postmortem explaining why an obsolete module was retired.
        m_hist = _HIST_ZH_RE.search(answer) or _HIST_EN_RE.search(answer)
        if m_hist or ("当初为什么要" in question and _ASCII_CODE_TOKEN_RE.search(qa_text)):
            if m_hist:
                old_tok = _clean_clause(m_hist.group("old"))
                reason = _clean_clause(m_hist.group("reason"))
            else:
                codes = _ASCII_CODE_TOKEN_RE.findall(qa_text)
                old_tok = codes[0] if codes else title_hint
                reason = _clean_clause(answer)
            target = self._pick_target_candidate(qa_text, candidates, replaces_text=old_tok)
            return Phase1Note(
                action="attach" if target else "create",
                target_topic_id=target.topic_id if target else "",
                topic_title=target.title if target else title_hint,
                update_type="historical_note",
                claim_text="",
                replaces_text=old_tok,
                reason=reason,
                keywords=[],
                evidence_qa_id=qa_id,
                timestamp=ts,
            )

        # 4. Problem / limitation report on an existing approach (skip trivial chitchat/acks).
        target = self._pick_target_candidate(qa_text, candidates)
        clean_ans = _clean_clause(answer)
        codes = _ASCII_CODE_TOKEN_RE.findall(qa_text)
        update_type = "problem" if _PROBLEM_HINT_RE.search(qa_text) else "add"
        if (
            target is None
            and update_type == "add"
            and not codes
            and (
                _TRIVIAL_NOISE_RE.match(clean_ans)
                or (_TRIVIAL_NOISE_RE.match(_clean_clause(question)) and len(clean_ans) <= 24)
            )
        ):
            return Phase1Note(
                action="skip",
                target_topic_id="",
                topic_title="",
                update_type="skip",
                claim_text="",
                replaces_text="",
                reason="trivial_chitchat",
                keywords=[],
                evidence_qa_id=qa_id,
                timestamp=ts,
            )
        return Phase1Note(
            action="attach" if target else "create",
            target_topic_id=target.topic_id if target else "",
            topic_title=target.title if target else title_hint,
            update_type=update_type,
            claim_text=clean_ans,
            replaces_text="",
            reason="",
            keywords=codes[:4],
            evidence_qa_id=qa_id,
            timestamp=ts,
        )

    def apply_note(self, note: Phase1Note, dossiers: list[TopicDossier]) -> TopicDossier | None:
        """Phase 2: Merge delta note into dossiers, separating current vs superseded state."""
        if note.action == "skip" or note.update_type == "skip":
            return None

        target: TopicDossier | None = None
        if note.action == "attach" and note.target_topic_id:
            for d in dossiers:
                if d.topic_id == note.target_topic_id:
                    target = d
                    break

        if target is None:
            tid = note.target_topic_id or _slugify_topic_id(note.topic_title, note.evidence_qa_id)
            while any(d.topic_id == tid for d in dossiers):
                tid = f"{tid}_n"
            target = TopicDossier(
                topic_id=tid,
                title=note.topic_title or f"主题 {note.evidence_qa_id}",
                keywords=[k for k in note.keywords if k],
            )
            dossiers.append(target)

        # Replay idempotency: if this QA was already materialized before a crash, do not duplicate claims.
        if note.evidence_qa_id > 0 and (
            note.evidence_qa_id in target.active_qa_ids()
            or note.evidence_qa_id in target.superseded_qa_ids()
            or any(note.evidence_qa_id in s.evidence_qa_ids for s in target.superseded_claims)
        ):
            return target

        if note.evidence_qa_id > target.last_evidence_qa_id:
            target.last_evidence_qa_id = note.evidence_qa_id
        if note.timestamp:
            target.last_evidence_at = note.timestamp

        if note.update_type in ("evolve", "correct"):
            new_cid = f"c_{note.evidence_qa_id}_{len(target.current_claims) + len(target.superseded_claims) + 1}"
            replaces_id_set = {cid.strip() for cid in note.replaces_claim_ids if cid.strip()}
            old_low = note.replaces_text.strip().lower()
            old_clean = re.sub(r"[（(][^）)]*[）)]", "", old_low).strip()
            old_codes = [c.lower() for c in _ASCII_CODE_TOKEN_RE.findall(note.replaces_text)]
            old_terms = [t for t in dict.fromkeys([old_low, old_clean, *old_codes]) if len(t) >= 2]
            matched_indices: set[int] = set()
            if replaces_id_set:
                # Strict ID-only replacement when LLM provided explicit replaces_claim_ids:
                # never fall through to text matching that could wipe sibling claims.
                for idx, claim in enumerate(target.current_claims):
                    if claim.claim_id and claim.claim_id in replaces_id_set:
                        matched_indices.add(idx)
            else:
                # Compatibility fallback (deterministic extractor / legacy LLM without IDs):
                # require unambiguous match on primary claims (fact/decision/rule) so a shared
                # term never wipes multiple distinct sibling claims.
                primary_matches: list[int] = []
                aux_matches: list[int] = []
                for idx, claim in enumerate(target.current_claims):
                    c_low = claim.text.lower()
                    if old_terms and any(term in c_low for term in old_terms):
                        if claim.kind in ("problem", "legacy_seed"):
                            aux_matches.append(idx)
                        else:
                            primary_matches.append(idx)
                if len(primary_matches) == 1:
                    matched_indices.add(primary_matches[0])
                    matched_indices.update(aux_matches)
                elif len(primary_matches) == 0:
                    if aux_matches:
                        matched_indices.update(aux_matches)
                    else:
                        # Unparsed pre-V2 legacy_seed blob inside this exact dossier.
                        for idx, claim in enumerate(target.current_claims):
                            if claim.kind == "legacy_seed":
                                matched_indices.add(idx)
                # When len(primary_matches) > 1, text matching is ambiguous: preserve sibling
                # claims rather than wiping them indiscriminately.
            kept_claims: list[TopicClaim] = []
            for idx, claim in enumerate(target.current_claims):
                if idx in matched_indices:
                    target.superseded_claims.append(
                        SupersededClaim(
                            text=claim.text,
                            replaced_by_claim_id=new_cid,
                            reason=note.reason or f"被 {note.claim_text} 替代",
                            superseded_at=note.timestamp,
                            evidence_qa_ids=list(claim.evidence_qa_ids),
                            claim_id=claim.claim_id,
                        )
                    )
                else:
                    kept_claims.append(claim)
            target.current_claims = kept_claims
            if note.claim_text:
                target.current_claims.append(
                    TopicClaim(
                        claim_id=new_cid,
                        text=note.claim_text,
                        kind="rule" if note.update_type == "correct" else "decision",
                        evidence_qa_ids=[note.evidence_qa_id],
                        updated_at=note.timestamp,
                    )
                )
            # Prune obsolete identifier from keywords and add new clean keywords.
            if old_terms:
                target.keywords = [
                    kw for kw in target.keywords if not any(term == kw.lower() for term in old_terms)
                ]
            for kw in note.keywords:
                if kw and not any(term == kw.lower() for term in old_terms) and kw not in target.keywords:
                    target.keywords.append(kw)

        elif note.update_type == "historical_note":
            target.superseded_claims.append(
                SupersededClaim(
                    text=note.replaces_text or note.topic_title,
                    replaced_by_claim_id="",
                    reason=note.reason or note.claim_text,
                    superseded_at=note.timestamp,
                    evidence_qa_ids=[note.evidence_qa_id] if note.evidence_qa_id > 0 else [],
                    claim_id=f"hist_{note.evidence_qa_id}_{len(target.superseded_claims) + 1}",
                )
            )
            if not target.current_claims and note.topic_title:
                target.summary = target.summary or note.topic_title

        else:  # add | problem
            if note.claim_text:
                already_present = any(note.claim_text in c.text for c in target.current_claims)
                if already_present:
                    # Attach supporting QA watermark to matching existing claim.
                    for c in target.current_claims:
                        if note.claim_text in c.text and note.evidence_qa_id not in c.evidence_qa_ids:
                            c.evidence_qa_ids.append(note.evidence_qa_id)
                            c.updated_at = note.timestamp or c.updated_at
                            break
                else:
                    new_cid = f"c_{note.evidence_qa_id}_{len(target.current_claims) + len(target.superseded_claims) + 1}"
                    target.current_claims.append(
                        TopicClaim(
                            claim_id=new_cid,
                            text=note.claim_text,
                            kind="problem" if note.update_type == "problem" else "fact",
                            evidence_qa_ids=[note.evidence_qa_id] if note.evidence_qa_id > 0 else [],
                            updated_at=note.timestamp,
                        )
                    )
            for kw in note.keywords:
                if kw and kw not in target.keywords:
                    target.keywords.append(kw)

        active_texts = [c.text for c in target.current_claims if c.text]
        if active_texts:
            target.summary = "；".join(active_texts[-2:])[:240]
        new_emb = self.embed_fn(target.embedding_text())
        if new_emb:
            target.embedding = new_emb
            if self.embed_model:
                target.embed_model = self.embed_model
        return target

    def consolidate_qa_stream(
        self,
        qa_pairs: Sequence[Mapping[str, Any]],
        existing_dossiers: Sequence[TopicDossier] | None = None,
    ) -> tuple[list[TopicDossier], ConsolidationReport]:
        """Incrementally consolidate chronological QA pairs into structured dossiers."""
        dossiers: list[TopicDossier] = [
            TopicDossier.from_dict(d.to_dict()) for d in (existing_dossiers or [])
        ]
        ordered = sorted(
            qa_pairs,
            key=lambda q: (_format_iso_ts(q.get("timestamp")), int(q.get("id") or 0)),
        )
        initial_ids = {d.topic_id for d in dossiers}
        updated_ids: set[str] = set()
        superseded_before = sum(len(d.superseded_claims) for d in dossiers)
        skipped = 0
        processed = 0
        failed_qa_ids: list[int] = []
        max_qa_id = max((d.last_evidence_qa_id for d in dossiers), default=0)

        for qa in ordered:
            qid = int(qa.get("id") or 0)
            processed += 1
            note, failed = self._distill_qa_with_status(qa, dossiers)
            if failed:
                skipped += 1
                if qid > 0:
                    failed_qa_ids.append(qid)
                # Preserve exact chronological watermark: halt before advancing past failed QA.
                break
            if note is None or note.action == "skip" or note.update_type == "skip":
                skipped += 1
                if qid > max_qa_id:
                    max_qa_id = qid
                continue
            touched = self.apply_note(note, dossiers)
            if touched is None:
                skipped += 1
                if qid > max_qa_id:
                    max_qa_id = qid
                continue
            if qid > max_qa_id:
                max_qa_id = qid
            if touched.topic_id in initial_ids:
                updated_ids.add(touched.topic_id)

        created = len([d for d in dossiers if d.topic_id not in initial_ids])
        superseded_after = sum(len(d.superseded_claims) for d in dossiers)
        report = ConsolidationReport(
            processed_qas=processed,
            created_topics=created,
            updated_topics=len(updated_ids),
            superseded_claims=max(0, superseded_after - superseded_before),
            skipped_qas=skipped,
            max_qa_id=max_qa_id,
            failed_qa_ids=tuple(failed_qa_ids),
        )
        return dossiers, report


def _parse_qa_id(source_id: str) -> int | None:
    sid = str(source_id or "")
    if not sid.startswith("qa_"):
        return None
    tail = sid[3:]
    if not tail.isdigit():
        return None
    return int(tail)


def _parse_iso_ts(raw: str) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    norm = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(norm)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _cap_demoted_score(raw_score: float, factor: float, min_fresh_score: float) -> float:
    scaled = float(raw_score) * float(factor)
    if min_fresh_score > 0.0:
        # Preserve monotonic order among demoted items while guaranteeing every demoted hit
        # sorts strictly below the lowest active/fresh evidence score (e.g. overriding +0.5 boosts).
        ceiling = min_fresh_score * 0.4 * (float(raw_score) / (float(raw_score) + 0.1)) if raw_score > 0 else 0.0
        return round(min(scaled, ceiling), 6)
    return round(scaled, 6)


def enforce_topic_v2_post_rerank_order(
    ranked: list[Any],
    *,
    demoted_sids: Sequence[str] | set[str],
    is_historical: bool = False,
) -> list[Any]:
    """Enforce Topic V2 demotion invariants after optional cross-encoder rerank.

    Preserves Rerank's relative ordering inside the active group and inside the
    demoted group, while ensuring demoted stale items never rank above active hits.
    """
    if is_historical or not demoted_sids or not ranked:
        return ranked
    demoted_set = set(demoted_sids)
    active_group = [h for h in ranked if str(getattr(h, "source_id", "") or "") not in demoted_set]
    demoted_group = [h for h in ranked if str(getattr(h, "source_id", "") or "") in demoted_set]
    if not demoted_group or not active_group:
        return ranked
    return active_group + demoted_group


def apply_topic_v2_ranking_guard(
    scored: list[Any],
    *,
    query: str,
    trace: Any = None,
    probe_fn: Callable[..., Any] | None = None,
    extra_superseded_qa_ids: Sequence[int] | set[int] | None = None,
) -> dict[str, Any]:
    """Enforce read-side freshness and explicit LLM-established supersession in ``scored``.

    Design invariant:
      - Topic membership and supersession are established by the LLM during consolidation
        and persisted as explicit QA IDs (``active_qas``, ``superseded_qas``).
      - Recall NEVER guesses topic membership via keyword/bigram overlap and NEVER demotes
        whole multi-topic Observation Notes or Yin paragraphs from coarse ``source_qa_range``
        intervals (fail-safe preservation; notes rely on UTC half-life decay until
        paragraph-level provenance lands).

    Mutates ``hit.content``, ``hit.content_preview`` (stripping internal ``topic_v2`` headers),
    and ``hit.rrf_score`` in place prior to final score sorting. Returns guard metadata
    including ``demoted_sids`` for downstream post-rerank enforcement.
    """
    is_hist = is_historical_trace_query(query)
    topic_meta: dict[str, dict[str, Any] | None] = {}
    qa_ids_seen: list[int] = []
    qa_timestamps: dict[str, datetime] = {}

    for hit in scored:
        kind = getattr(hit, "kind", "")
        sid = str(getattr(hit, "source_id", "") or "")
        raw_body = str(getattr(hit, "content", "") or getattr(hit, "content_preview", "") or "")
        if kind == "topic":
            meta = parse_topic_v2_header(raw_body)
            topic_meta[sid] = meta
            if meta is not None:
                clean_full = strip_topic_v2_header(raw_body)
                hit.content = clean_full
                hit.content_preview = clean_full[:500]
        elif kind == "qa":
            qid = _parse_qa_id(sid)
            dt = _parse_iso_ts(getattr(hit, "created_at", ""))
            if dt is not None:
                qa_timestamps[sid] = dt
            if qid is not None:
                qa_ids_seen.append(qid)

    max_qa_id = max(qa_ids_seen, default=0)
    latest_qa_ts = max(qa_timestamps.values(), default=None)
    earliest_qa_ts = min(qa_timestamps.values(), default=None)
    qa_span_days = (
        (latest_qa_ts - earliest_qa_ts).total_seconds() / 86400.0
        if (latest_qa_ts is not None and earliest_qa_ts is not None)
        else 0.0
    )

    hit_dt_by_sid = {
        str(getattr(hit, "source_id", "") or ""): _parse_iso_ts(getattr(hit, "created_at", ""))
        for hit in scored
        if getattr(hit, "kind", "") == "topic"
    }

    # Collect explicit LLM-confirmed supersession relations from Topic V2 headers + PG claims store.
    superseded_qa_ids: set[int] = set()
    if extra_superseded_qa_ids:
        for qid in extra_superseded_qa_ids:
            try:
                superseded_qa_ids.add(int(qid))
            except (TypeError, ValueError):
                pass
    superseded_qa_by_topic: dict[str, set[int]] = {}
    superseded_qa_ts: dict[int, datetime | None] = {}
    topic_active_qas: dict[str, set[int]] = {}

    for sid, meta in topic_meta.items():
        if meta is None or int(meta.get("v") or 0) != 2:
            continue
        t_dt = _parse_iso_ts(meta.get("last_at", "")) or hit_dt_by_sid.get(sid)
        act_set: set[int] = set()
        for qid in (meta.get("active_qas") or []):
            try:
                act_set.add(int(qid))
            except (TypeError, ValueError):
                pass
        topic_active_qas[sid] = act_set

        sup_set: set[int] = set()
        for qid in (meta.get("superseded_qas") or []):
            try:
                iq = int(qid)
            except (TypeError, ValueError):
                continue
            sup_set.add(iq)
            superseded_qa_ids.add(iq)
            prev_dt = superseded_qa_ts.get(iq)
            if t_dt is not None and (prev_dt is None or t_dt > prev_dt):
                superseded_qa_ts[iq] = t_dt
            elif iq not in superseded_qa_ts:
                superseded_qa_ts[iq] = None
        superseded_qa_by_topic[sid] = sup_set

    # A Topic V2 dossier is stale ONLY when another Topic V2 dossier explicitly superseded
    # one of its active evidence QAs. Never demote any Topic V2 dossier via keyword guessing
    # or global max_qa_id.
    fresh_topic_sids: set[str] = set()
    stale_topic_sids: set[str] = set()
    for sid, meta in topic_meta.items():
        if meta is None or int(meta.get("v") or 0) != 2:
            continue
        act_set = topic_active_qas.get(sid, set())
        other_sup = set().union(
            *(s for other_sid, s in superseded_qa_by_topic.items() if other_sid != sid)
        ) if superseded_qa_by_topic else set()
        if act_set and (act_set & other_sup):
            stale_topic_sids.add(sid)
        else:
            fresh_topic_sids.add(sid)

    # Schema-version precedence (zero keyword guessing): when structured Topic V2 dossiers
    # are present, unmigrated pre-V2 legacy topic rows (meta is None) yield rank to them.
    if fresh_topic_sids:
        for sid, meta in topic_meta.items():
            if meta is None:
                stale_topic_sids.add(sid)

    # First, adjust non-demoted QA recency tie-breaks on current-state queries (pure timestamp recency).
    qa_recency_adjusted = 0
    if not is_hist and len(qa_timestamps) >= 2 and qa_span_days >= 3.0 and latest_qa_ts is not None:
        for hit in scored:
            if getattr(hit, "kind", "") != "qa":
                continue
            sid = str(getattr(hit, "source_id", "") or "")
            if sid not in qa_timestamps:
                continue
            qid = _parse_qa_id(sid)
            if qid is not None and qid in superseded_qa_ids:
                continue
            age_delta_days = max(0.0, (latest_qa_ts - qa_timestamps[sid]).total_seconds() / 86400.0)
            factor = 0.70 + 0.30 * math.exp(-age_delta_days / 14.0)
            if factor < 0.9999:
                hit.rrf_score = round(float(hit.rrf_score) * factor, 6)
                qa_recency_adjusted += 1
                if probe_fn is not None and trace is not None:
                    probe_fn(trace, "score", sid, "temporal", hit.rrf_score, operation="topic_v2_qa_recency")

    # Compute the lowest score among fresh active candidates so every explicitly demoted hit sorts below it.
    fresh_active_scores: list[float] = []
    for hit in scored:
        kind = getattr(hit, "kind", "")
        sid = str(getattr(hit, "source_id", "") or "")
        if kind == "topic" and sid in fresh_topic_sids and not is_hist:
            fresh_active_scores.append(float(hit.rrf_score))
        elif kind == "qa":
            qid = _parse_qa_id(sid)
            if qid is not None and qid in superseded_qa_ids:
                continue
            if not is_hist and len(qa_timestamps) >= 2 and qa_span_days >= 3.0 and latest_qa_ts is not None:
                dt = qa_timestamps.get(sid)
                if dt is not None and (latest_qa_ts - dt).total_seconds() <= 86400.0:
                    fresh_active_scores.append(float(hit.rrf_score))
            elif is_hist or len(qa_timestamps) < 2 or qa_span_days < 3.0:
                fresh_active_scores.append(float(hit.rrf_score))
    min_fresh_score = min(fresh_active_scores, default=0.0)

    demoted_topics = 0
    demoted_superseded_qas = 0
    demoted_derived = 0
    demoted_sids: set[str] = set()

    for hit in scored:
        kind = getattr(hit, "kind", "")
        sid = str(getattr(hit, "source_id", "") or "")
        if kind == "topic" and sid in stale_topic_sids and not is_hist:
            hit.rrf_score = _cap_demoted_score(float(hit.rrf_score), 0.25, min_fresh_score)
            demoted_topics += 1
            demoted_sids.add(sid)
            if probe_fn is not None and trace is not None:
                probe_fn(trace, "score", sid, "temporal", hit.rrf_score, operation="topic_v2_freshness_guard")
        elif kind == "qa" and not is_hist:
            qid = _parse_qa_id(sid)
            if qid is not None and qid in superseded_qa_ids:
                hit.rrf_score = _cap_demoted_score(float(hit.rrf_score), 0.20, min_fresh_score)
                demoted_superseded_qas += 1
                demoted_sids.add(sid)
                if probe_fn is not None and trace is not None:
                    probe_fn(trace, "score", sid, "temporal", hit.rrf_score, operation="topic_v2_freshness_guard")
        # Derived multi-topic Observation Notes & Yin paragraphs are fail-safe preserved here:
        # coarse source_qa_range intervals cannot prove an entire multi-topic note is stale.

    return {
        "is_historical": is_hist,
        "max_qa_id": max_qa_id,
        "fresh_topics": len(fresh_topic_sids),
        "demoted_topics": demoted_topics,
        "demoted_superseded_qas": demoted_superseded_qas,
        "demoted_derived": demoted_derived,
        "qa_recency_adjusted": qa_recency_adjusted,
        "demoted_sids": sorted(demoted_sids),
    }


@contextlib.contextmanager
def _pg_conn_ctx(pg: Any):
    """Yield an active DB connection from either ``pg.lease()`` or ``pg._connect()``."""
    if pg is None:
        yield None
        return
    lease_fn = getattr(pg, "lease", None)
    if callable(lease_fn):
        with pg.lease(timeout=10.0) as conn:
            yield getattr(conn, "connection", conn)
        return
    connect_fn = getattr(pg, "_connect", None)
    if callable(connect_fn):
        yield connect_fn()
        return
    if hasattr(pg, "cursor"):
        yield pg
        return
    yield None


def _parse_pg_emb(raw: Any) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    s = str(raw).strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            return [float(x) for x in json.loads(s)]
        except Exception:
            return None
    return None


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


_STATE_DIR_LOCKS_GUARD = threading.Lock()
_STATE_DIR_LOCKS: dict[str, threading.Lock] = {}


def _get_state_dir_lock(state_dir: str | Path) -> threading.Lock:
    key = str(Path(state_dir).resolve())
    with _STATE_DIR_LOCKS_GUARD:
        lock = _STATE_DIR_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _STATE_DIR_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _state_dir_file_lock(state_dir: Path, timeout: float = 15.0):
    """Fail-closed advisory file lock serializing multi-process writers on the same state_dir."""
    lock_path = state_dir / ".topic_v2.lock"
    state_dir.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    locked = False
    try:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
        deadline = time.monotonic() + max(0.05, float(timeout))
        while True:
            try:
                try:
                    import fcntl  # type: ignore

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except ImportError:
                    import msvcrt  # type: ignore

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
            except (BlockingIOError, PermissionError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"topic_v2: failed to acquire file lock {lock_path} within {timeout:.1f}s"
                    ) from exc
                time.sleep(0.02)
        yield
    finally:
        if locked:
            try:
                import fcntl  # type: ignore

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except ImportError:
                try:
                    import msvcrt  # type: ignore

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            except Exception:
                pass
        try:
            fh.close()
        except Exception:
            pass


def _stable_claim_id(
    topic_id: str,
    existing_id: str,
    text: str,
    status: str,
    qa_ids: Sequence[int],
) -> str:
    cid = str(existing_id or "").strip()
    if cid:
        return cid
    digest = hashlib.sha1(
        f"{topic_id}|{status}|{text}|{sorted(int(q) for q in qa_ids)}".encode("utf-8")
    ).hexdigest()[:12]
    prefix = "sc" if status == "superseded" else "c"
    return f"{prefix}_{topic_id}_{digest}"


_WARNED_MISSING_PG_CLAIMS = False


def _warn_missing_pg_claims_once(context: str) -> None:
    """Emit one clear warning when Topic V2 relational PG tables are absent."""
    global _WARNED_MISSING_PG_CLAIMS
    if not _WARNED_MISSING_PG_CLAIMS:
        _WARNED_MISSING_PG_CLAIMS = True
        logger.warning(
            "topic_v2: PG relational tables (topic_claims / topic_claim_evidence / topic_v2_state) "
            "unavailable during %s; operating in degraded JSON/header fallback mode. "
            "Run schema bootstrap/upgrade (schema/topic_claims.sql) and verify health ST04.",
            context,
        )


def _has_pg_claims_tables(cur: Any) -> bool:
    """Return True when both ``public.topic_claims`` and ``public.topic_claim_evidence`` exist."""
    try:
        cur.execute(
            "SELECT to_regclass('public.topic_claims'), to_regclass('public.topic_claim_evidence')"
        )
        row = cur.fetchone()
        return bool(row and len(row) >= 2 and row[0] and row[1])
    except Exception:
        return False


def _has_pg_state_table(cur: Any) -> bool:
    """Return True when ``public.topic_v2_state`` exists."""
    try:
        cur.execute("SELECT to_regclass('public.topic_v2_state')")
        row = cur.fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def _sync_dossiers_to_pg_claims(cur: Any, dossiers: Sequence[TopicDossier]) -> int:
    """Transactionally upsert ``topic_claims`` and ``topic_claim_evidence`` for ``dossiers``."""
    upserted_claims = 0
    for dossier in dossiers:
        tid = dossier.topic_id
        # Map active claim_id -> evidence_qa_ids for recording explicit 'supersedes' audit links.
        active_ev_by_cid: dict[str, list[int]] = {}
        for claim in dossier.current_claims:
            cid = _stable_claim_id(tid, claim.claim_id, claim.text, "active", claim.evidence_qa_ids)
            if not claim.claim_id:
                claim.claim_id = cid
            active_ev_by_cid[cid] = [int(q) for q in claim.evidence_qa_ids if int(q) > 0]
            updated_dt = _parse_iso_ts(claim.updated_at) or _parse_iso_ts(dossier.last_evidence_at)
            cur.execute(
                "INSERT INTO public.topic_claims "
                "(claim_id, topic_id, claim_text, kind, status, replaced_by_claim_id, "
                " supersede_reason, updated_at, superseded_at) "
                "VALUES (%s, %s, %s, %s, 'active', '', '', COALESCE(%s, NOW()), NULL) "
                "ON CONFLICT (claim_id) DO UPDATE SET "
                "topic_id = EXCLUDED.topic_id, claim_text = EXCLUDED.claim_text, "
                "kind = EXCLUDED.kind, status = 'active', replaced_by_claim_id = '', "
                "supersede_reason = '', updated_at = EXCLUDED.updated_at, superseded_at = NULL",
                (cid, tid, claim.text, claim.kind or "fact", updated_dt),
            )
            upserted_claims += 1
            for qid in active_ev_by_cid[cid]:
                cur.execute(
                    "INSERT INTO public.topic_claim_evidence (claim_id, topic_id, qa_id, relation) "
                    "VALUES (%s, %s, %s, 'supports') "
                    "ON CONFLICT (claim_id, qa_id, relation) DO NOTHING",
                    (cid, tid, qid),
                )

        for sup in dossier.superseded_claims:
            scid = _stable_claim_id(tid, sup.claim_id, sup.text, "superseded", sup.evidence_qa_ids)
            if not sup.claim_id:
                sup.claim_id = scid
            sup_dt = _parse_iso_ts(sup.superseded_at) or _parse_iso_ts(dossier.last_evidence_at)
            cur.execute(
                "INSERT INTO public.topic_claims "
                "(claim_id, topic_id, claim_text, kind, status, replaced_by_claim_id, "
                " supersede_reason, updated_at, superseded_at) "
                "VALUES (%s, %s, %s, 'decision', 'superseded', %s, %s, COALESCE(%s, NOW()), COALESCE(%s, NOW())) "
                "ON CONFLICT (claim_id) DO UPDATE SET "
                "topic_id = EXCLUDED.topic_id, claim_text = EXCLUDED.claim_text, "
                "status = 'superseded', replaced_by_claim_id = EXCLUDED.replaced_by_claim_id, "
                "supersede_reason = EXCLUDED.supersede_reason, "
                "updated_at = EXCLUDED.updated_at, superseded_at = EXCLUDED.superseded_at",
                (scid, tid, sup.text, sup.replaced_by_claim_id or "", sup.reason or "", sup_dt, sup_dt),
            )
            upserted_claims += 1
            for qid in sup.evidence_qa_ids:
                iq = int(qid)
                if iq > 0:
                    cur.execute(
                        "INSERT INTO public.topic_claim_evidence (claim_id, topic_id, qa_id, relation) "
                        "VALUES (%s, %s, %s, 'supports') "
                        "ON CONFLICT (claim_id, qa_id, relation) DO NOTHING",
                        (scid, tid, iq),
                    )
            for rep_qid in active_ev_by_cid.get(sup.replaced_by_claim_id or "", []):
                cur.execute(
                    "INSERT INTO public.topic_claim_evidence (claim_id, topic_id, qa_id, relation) "
                    "VALUES (%s, %s, %s, 'supersedes') "
                    "ON CONFLICT (claim_id, qa_id, relation) DO NOTHING",
                    (scid, tid, rep_qid),
                )
    return upserted_claims


def _load_dossiers_from_pg_claims(
    cur: Any,
    topic_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[TopicDossier], list[Mapping[str, Any]]]:
    """Reconstruct TopicDossiers directly from PG ``topic_claims`` + ``topic_claim_evidence``.

    Returns ``(reconstructed_dossiers, remaining_legacy_rows)`` for topics that do not yet
    have relational claims in PG.
    """
    if not topic_rows:
        return [], []
    tids = [str(r.get("topic_id") or "") for r in topic_rows if r.get("topic_id")]
    if not tids:
        return [], list(topic_rows)
    cur.execute(
        "SELECT claim_id, topic_id, claim_text, kind, status, replaced_by_claim_id, "
        " supersede_reason, updated_at, superseded_at "
        "FROM public.topic_claims "
        "WHERE topic_id = ANY(%s) "
        "ORDER BY created_at ASC, claim_id ASC",
        (tids,),
    )
    claim_rows = list(cur.fetchall() or [])
    if not claim_rows:
        return [], list(topic_rows)

    cur.execute(
        "SELECT claim_id, qa_id, relation FROM public.topic_claim_evidence "
        "WHERE topic_id = ANY(%s) AND relation = 'supports' "
        "ORDER BY qa_id ASC",
        (tids,),
    )
    ev_by_claim: dict[str, list[int]] = {}
    for cid, qid, _rel in (cur.fetchall() or []):
        ev_by_claim.setdefault(str(cid), []).append(int(qid))

    claims_by_topic: dict[str, list[tuple[Any, ...]]] = {}
    for row in claim_rows:
        claims_by_topic.setdefault(str(row[1]), []).append(row)

    reconstructed: list[TopicDossier] = []
    remaining_legacy: list[Mapping[str, Any]] = []
    for trow in topic_rows:
        tid = str(trow.get("topic_id") or "")
        t_claims = claims_by_topic.get(tid)
        if not t_claims:
            remaining_legacy.append(trow)
            continue
        cur_claims: list[TopicClaim] = []
        sup_claims: list[SupersededClaim] = []
        max_qid = 0
        latest_ts = ""
        for cid, _tid, ctext, ckind, cstatus, rep_by, sreason, upd_at, sup_at in t_claims:
            qids = sorted(set(ev_by_claim.get(str(cid), [])))
            if qids:
                max_qid = max(max_qid, max(qids))
            upd_str = _format_iso_ts(upd_at)
            sup_str = _format_iso_ts(sup_at)
            if upd_str and upd_str > latest_ts:
                latest_ts = upd_str
            if sup_str and sup_str > latest_ts:
                latest_ts = sup_str
            if str(cstatus or "active") == "superseded":
                sup_claims.append(
                    SupersededClaim(
                        text=str(ctext or ""),
                        replaced_by_claim_id=str(rep_by or ""),
                        reason=str(sreason or ""),
                        superseded_at=sup_str,
                        evidence_qa_ids=qids,
                        claim_id=str(cid),
                    )
                )
            else:
                cur_claims.append(
                    TopicClaim(
                        claim_id=str(cid),
                        text=str(ctext or ""),
                        kind=str(ckind or "fact"),
                        evidence_qa_ids=qids,
                        updated_at=upd_str,
                    )
                )
        header = parse_topic_v2_header(str(trow.get("body") or "")) or {}
        if int(header.get("last_qa") or 0) > max_qid:
            max_qid = int(header.get("last_qa") or 0)
        if str(header.get("last_at") or "") > latest_ts:
            latest_ts = str(header.get("last_at") or "")
        reconstructed.append(
            TopicDossier(
                topic_id=tid,
                title=str(trow.get("title") or tid),
                summary=str(trow.get("summary") or ""),
                current_claims=cur_claims,
                superseded_claims=sup_claims,
                keywords=list(trow.get("keywords") or []),
                last_evidence_qa_id=max_qid,
                last_evidence_at=latest_ts,
                status=str(trow.get("status") or "active"),
                embed_model=str(trow.get("embed_model") or ""),
                embedding=trow.get("embedding"),
            )
        )
    return reconstructed, remaining_legacy


def fetch_pg_superseded_qa_ids(pg: Any, qa_ids: Sequence[int]) -> set[int]:
    """Query PG relational claim tables for recalled QA IDs that ONLY support superseded claims.

    Lifts the 32/12-item Topic V2 header budget cap so arbitrarily old superseded evidence
    stays accurately demoted even when its parent topic is not in the top-K topic hits.
    Fail-open (returns empty set) when PG or relational tables are unavailable.
    """
    clean_ids = sorted({int(q) for q in (qa_ids or []) if int(q) > 0})
    if pg is None or not clean_ids:
        return set()
    try:
        with _pg_conn_ctx(pg) as conn:
            if conn is None:
                return set()
            with conn.cursor() as cur:
                if not _has_pg_claims_tables(cur):
                    _warn_missing_pg_claims_once("recall_hydration")
                    return set()
                cur.execute(
                    "SELECT DISTINCT e.qa_id "
                    "FROM public.topic_claim_evidence e "
                    "JOIN public.topic_claims c ON c.claim_id = e.claim_id "
                    "WHERE e.qa_id = ANY(%s) "
                    "  AND e.relation = 'supports' "
                    "  AND c.status = 'superseded' "
                    "  AND NOT EXISTS ("
                    "      SELECT 1 "
                    "      FROM public.topic_claim_evidence e2 "
                    "      JOIN public.topic_claims c2 ON c2.claim_id = e2.claim_id "
                    "      WHERE e2.qa_id = e.qa_id "
                    "        AND e2.relation = 'supports' "
                    "        AND c2.status = 'active'"
                    "  )",
                    (clean_ids,),
                )
                return {int(row[0]) for row in (cur.fetchall() or []) if row and row[0] is not None}
    except Exception:
        return set()


def _compute_safe_baseline_qa_id(
    cur: Any,
    *,
    pending_answer_grace_s: float = 900.0,
) -> int:
    """Compute a first-boot baseline watermark that skips historical completed QAs
    without swallowing any recent in-flight empty-answer QA awaiting late derivation.
    """
    max_completed = 0
    try:
        cur.execute(
            "SELECT COALESCE(MAX(id), 0) FROM qa_pairs "
            "WHERE COALESCE(BTRIM(answer), '') <> '' "
            "AND COALESCE(session_id, '') NOT LIKE '%%.trajectory%%'"
        )
        mrow = cur.fetchone()
        max_completed = int(mrow[0] or 0) if mrow and mrow[0] is not None else 0
    except Exception:
        try:
            cur.execute(
                "SELECT COALESCE(MAX(id), 0) FROM qa_pairs "
                "WHERE answer IS NOT NULL "
                "AND COALESCE(session_id, '') NOT LIKE '%%.trajectory%%'"
            )
            mrow = cur.fetchone()
            max_completed = int(mrow[0] or 0) if mrow and mrow[0] is not None else 0
        except Exception:
            max_completed = 0

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(0.0, float(pending_answer_grace_s)))
        cur.execute(
            "SELECT MIN(id) FROM qa_pairs "
            "WHERE COALESCE(BTRIM(answer), '') = '' "
            "AND COALESCE(session_id, '') NOT LIKE '%%.trajectory%%' "
            "AND (timestamp IS NULL OR timestamp >= %s)",
            (cutoff,),
        )
        prow = cur.fetchone()
        if prow is not None and prow[0] is not None:
            min_pending = int(prow[0])
            if min_pending > 0:
                return max(0, min(max_completed, min_pending - 1))
    except Exception:
        pass
    return max(0, max_completed)


def _parse_local_topic_v2_state(state_path: Path) -> tuple[int, bool]:
    """Read local ``topic_v2_state.json`` and distinguish legacy zero-watermark files.

    A legacy file containing only ``{"last_qa_id": 0}`` without
    ``"baseline_initialized": true`` is treated as uninitialized so first-boot
    baseline protection still pins ``MAX(completed)`` when ``backfill_history=False``.
    """
    if not state_path.is_file():
        return 0, False
    try:
        raw_state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(raw_state, dict):
            return 0, False
        last_qa_id = max(0, int(raw_state.get("last_qa_id") or 0))
        has_valid = last_qa_id > 0 or bool(raw_state.get("baseline_initialized"))
        return last_qa_id, has_valid
    except Exception:
        return 0, False


def _parse_reprocess_qa_ids(raw_val: Any) -> list[int]:
    """Normalize a JSONB/list ``reprocess_qa_ids`` payload into sorted positive ints."""
    if raw_val is None:
        return []
    if isinstance(raw_val, str):
        try:
            raw_val = json.loads(raw_val)
        except Exception:
            return []
    if not isinstance(raw_val, (list, tuple)):
        return []
    out: set[int] = set()
    for item in raw_val:
        try:
            qid = int(item)
            if qid > 0:
                out.add(qid)
        except Exception:
            continue
    return sorted(out)


def ensure_topic_v2_baseline_watermark(
    pg: Any,
    *,
    state_dir: str | Path,
    backfill_history: bool = False,
    pending_answer_grace_s: float = 900.0,
) -> dict[str, Any]:
    """Initialize Topic V2 global watermark at application startup BEFORE live ingestion starts."""
    if pg is None:
        return {"initialized": False, "last_qa_id": 0, "reason": "no_pg"}
    if backfill_history:
        return {"initialized": False, "last_qa_id": 0, "reason": "backfill_enabled"}

    root = Path(state_dir)
    state_path = root / "topic_v2_state.json"
    dir_lock = _get_state_dir_lock(root)

    with dir_lock, _state_dir_file_lock(root):
        local_last_qa, has_valid_local = _parse_local_topic_v2_state(state_path)

        with _pg_conn_ctx(pg) as conn:
            if conn is None:
                return {"initialized": False, "last_qa_id": local_last_qa, "reason": "no_conn"}
            has_pg_state = False
            with conn.cursor() as cur:
                has_pg_state = _has_pg_state_table(cur)
                if has_pg_state:
                    try:
                        cur.execute(
                            "SELECT last_qa_id FROM public.topic_v2_state WHERE state_key = %s",
                            ("default",),
                        )
                        srow = cur.fetchone()
                        if srow is not None and srow[0] is not None:
                            return {"initialized": False, "last_qa_id": int(srow[0]), "reason": "pg_state_exists"}
                    except Exception:
                        pass
                if has_valid_local:
                    return {"initialized": False, "last_qa_id": local_last_qa, "reason": "local_state_exists"}

                baseline = _compute_safe_baseline_qa_id(
                    cur, pending_answer_grace_s=pending_answer_grace_s
                )
                if has_pg_state:
                    cur.execute(
                        "INSERT INTO public.topic_v2_state "
                        "(state_key, last_qa_id, topic_count, failed_qa_ids, updated_at) "
                        "VALUES (%s, %s, 0, '[]'::jsonb, NOW()) "
                        "ON CONFLICT (state_key) DO NOTHING",
                        ("default", int(baseline)),
                    )
            if has_pg_state and getattr(conn, "autocommit", None) is not True and callable(getattr(conn, "commit", None)):
                conn.commit()
        _atomic_write_json(
            state_path,
            {
                "last_qa_id": int(baseline),
                "baseline_initialized": True,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "topic_count": 0,
                "failed_qa_ids": [],
                "reprocess_qa_ids": [],
            },
        )
        return {"initialized": True, "last_qa_id": int(baseline), "reason": "baseline_pinned"}


def consolidate_pg_incremental(
    pg: Any,
    *,
    embed_fn: Callable[[str], list[float]],
    llm_fn: Callable[[list[dict[str, str]]], str] | None = None,
    state_dir: str | Path,
    dry_run: bool = False,
    batch_limit: int = 100,
    embed_model: str = "",
    backfill_history: bool = False,
    pending_answer_grace_s: float = 900.0,
) -> dict[str, Any]:
    """Incrementally consolidate new PG ``qa_pairs`` into ``TopicDossier``s and PG tables.

    Persists global watermark in ``public.topic_v2_state`` (mirrored to
    ``<state_dir>/topic_v2_state.json``) and full relational claims/evidence in
    ``public.topic_claims`` / ``public.topic_claim_evidence`` in the same PG
    transaction. When no watermark exists yet, ``backfill_history=False`` (default)
    initializes ``last_qa_id`` to ``MAX(qa_pairs.id)`` so existing deployments do not
    trigger an unexpected full-history LLM backfill on first boot.
    """
    if pg is None:
        raise ValueError("consolidate_pg_incremental requires a non-None PG store")

    root = Path(state_dir)
    state_path = root / "topic_v2_state.json"
    dossiers_path = root / "topic_v2_dossiers.json"
    dir_lock = _get_state_dir_lock(root)

    with dir_lock, _state_dir_file_lock(root):
        last_qa_id, has_local_state = _parse_local_topic_v2_state(state_path)

        eff_embed_model = str(embed_model or "").strip()
        consolidator = TopicConsolidatorV2(
            embed_fn=embed_fn,
            llm_fn=llm_fn,
            embed_model=eff_embed_model,
        )
        dossiers = load_dossiers(dossiers_path)
        reembedded_dossiers: set[str] = set()
        claims_backfill_ids: set[str] = set()
        has_pg_claims = False
        has_pg_state = False
        has_pg_reprocess_col = False
        pg_state_present = False
        initialized_baseline = False
        reprocess_qa_ids: list[int] = []
        reprocess_qas: list[dict[str, Any]] = []
        pre_consumed_reprocess_ids: list[int] = []
        now_utc = datetime.now(timezone.utc)

        new_qas: list[dict[str, Any]] = []
        with _pg_conn_ctx(pg) as conn:
            if conn is None:
                raise RuntimeError("consolidate_pg_incremental: failed to obtain PG connection")
            with conn.cursor() as cur:
                has_pg_claims = _has_pg_claims_tables(cur)
                has_pg_state = _has_pg_state_table(cur)
                if not has_pg_claims or not has_pg_state:
                    _warn_missing_pg_claims_once("consolidate_pg_incremental")

                if has_pg_state:
                    try:
                        cur.execute(
                            "SELECT last_qa_id, COALESCE(reprocess_qa_ids, '[]'::jsonb) "
                            "FROM public.topic_v2_state WHERE state_key = %s",
                            ("default",),
                        )
                        srow = cur.fetchone()
                        has_pg_reprocess_col = True
                        if srow is not None and srow[0] is not None:
                            last_qa_id = int(srow[0])
                            reprocess_qa_ids = _parse_reprocess_qa_ids(srow[1] if len(srow) > 1 else [])
                            pg_state_present = True
                    except Exception:
                        has_pg_reprocess_col = False
                        if getattr(conn, "autocommit", None) is not True and callable(getattr(conn, "rollback", None)):
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                        try:
                            cur.execute(
                                "SELECT last_qa_id FROM public.topic_v2_state WHERE state_key = %s",
                                ("default",),
                            )
                            srow = cur.fetchone()
                            if srow is not None and srow[0] is not None:
                                last_qa_id = int(srow[0])
                                pg_state_present = True
                        except Exception:
                            pg_state_present = False

                has_persisted_state = pg_state_present or has_local_state
                if not has_persisted_state and not backfill_history:
                    last_qa_id = _compute_safe_baseline_qa_id(
                        cur, pending_answer_grace_s=pending_answer_grace_s
                    )
                    initialized_baseline = True

                if not dossiers:
                    try:
                        try:
                            cur.execute(
                                "SELECT topic_id, title, COALESCE(summary, ''), COALESCE(body, ''), "
                                "keywords, status, embedding, COALESCE(embed_model, '') "
                                "FROM topics WHERE status='active'"
                            )
                        except Exception:
                            cur.execute(
                                "SELECT topic_id, title, COALESCE(summary, ''), COALESCE(body, ''), "
                                "keywords, status, embedding FROM topics WHERE status='active'"
                            )
                        topic_rows = []
                        for row in cur.fetchall() or []:
                            topic_rows.append(
                                {
                                    "topic_id": row[0],
                                    "title": row[1] or "",
                                    "summary": row[2] or "",
                                    "body": row[3] or "",
                                    "keywords": list(row[4] or []) if isinstance(row[4], (list, tuple)) else [],
                                    "status": row[5] or "active",
                                    "embedding": _parse_pg_emb(row[6]),
                                    "embed_model": str(row[7] or "") if len(row) > 7 else "",
                                }
                            )
                        if topic_rows:
                            if has_pg_claims:
                                pg_dossiers, rem_legacy = _load_dossiers_from_pg_claims(cur, topic_rows)
                                dossiers = list(pg_dossiers)
                                if rem_legacy:
                                    seeded = consolidator.seed_from_legacy_topics(rem_legacy)
                                    dossiers.extend(seeded)
                                    claims_backfill_ids.update(d.topic_id for d in seeded)
                            else:
                                dossiers = consolidator.seed_from_legacy_topics(topic_rows)
                    except Exception:
                        dossiers = []
                elif has_pg_claims:
                    # Lossless JSON -> PG migration: backfill any dossier whose claims are not yet in PG.
                    try:
                        tids = [d.topic_id for d in dossiers if d.current_claims or d.superseded_claims]
                        if tids:
                            cur.execute(
                                "SELECT DISTINCT topic_id FROM public.topic_claims WHERE topic_id = ANY(%s)",
                                (tids,),
                            )
                            present = {str(r[0]) for r in (cur.fetchall() or []) if r and r[0]}
                            claims_backfill_ids.update(tid for tid in tids if tid not in present)
                    except Exception:
                        pass

                due_reprocess_ids = [rid for rid in reprocess_qa_ids if rid <= last_qa_id][: max(int(batch_limit), 1)]
                if due_reprocess_ids:
                    cur.execute(
                        "SELECT id, timestamp, question, answer FROM qa_pairs "
                        "WHERE id = ANY(%s) "
                        "AND COALESCE(session_id, '') NOT LIKE '%%.trajectory%%' "
                        "ORDER BY id ASC",
                        (due_reprocess_ids,),
                    )
                    found_rep: dict[int, dict[str, Any]] = {}
                    for row in cur.fetchall() or []:
                        qid = int(row[0])
                        found_rep[qid] = {
                            "id": qid,
                            "timestamp": row[1],
                            "question": str(row[2] or ""),
                            "answer": str(row[3] or ""),
                        }
                    for rid in due_reprocess_ids:
                        qa_item = found_rep.get(rid)
                        if qa_item is None:
                            pre_consumed_reprocess_ids.append(rid)
                        elif str(qa_item.get("answer") or "").strip():
                            reprocess_qas.append(qa_item)
                        else:
                            qa_dt = _parse_iso_ts(_format_iso_ts(qa_item.get("timestamp")))
                            if qa_dt is not None and (now_utc - qa_dt).total_seconds() >= max(0.0, float(pending_answer_grace_s)):
                                pre_consumed_reprocess_ids.append(rid)

                cur.execute(
                    "SELECT id, timestamp, question, answer FROM qa_pairs "
                    "WHERE id > %s "
                    "AND COALESCE(session_id, '') NOT LIKE '%%.trajectory%%' "
                    "ORDER BY id ASC LIMIT %s",
                    (last_qa_id, max(int(batch_limit), 1)),
                )
                raw_fetched_qas: list[dict[str, Any]] = []
                for row in cur.fetchall() or []:
                    raw_fetched_qas.append(
                        {
                            "id": int(row[0]),
                            "timestamp": row[1],
                            "question": str(row[2] or ""),
                            "answer": str(row[3] or ""),
                        }
                    )

        # Preserve contiguous watermark across in-flight empty-answer QAs awaiting late
        # assistant derivation: halt the batch BEFORE any recent empty-answer QA so a
        # subsequent completed QA cannot advance last_qa_id past it. Abandoned empty-answer
        # QAs older than pending_answer_grace_s are skipped cleanly so they never deadlock.
        pending_incomplete_qa_ids: list[int] = []
        skipped_empty_max_id = last_qa_id
        for qa in raw_fetched_qas:
            if str(qa.get("answer") or "").strip():
                new_qas.append(qa)
                continue
            qa_dt = _parse_iso_ts(_format_iso_ts(qa.get("timestamp")))
            is_recent_pending = (
                qa_dt is None
                or (now_utc - qa_dt).total_seconds() < max(0.0, float(pending_answer_grace_s))
            )
            if is_recent_pending:
                pending_incomplete_qa_ids.append(int(qa["id"]))
                break
            skipped_empty_max_id = max(skipped_empty_max_id, int(qa["id"]))

        claims_store = "pg" if (has_pg_claims and has_pg_state) else "degraded"
        before_map = {d.topic_id: d.to_dict() for d in dossiers}

        # Re-embed ALL loaded or freshly migrated legacy dossiers whose model fingerprint does
        # not match the active model BEFORE consolidating new QAs or stamping PG metadata.
        if eff_embed_model:
            for d in dossiers:
                if d.embed_model != eff_embed_model:
                    new_emb = embed_fn(d.embedding_text())
                    if new_emb:
                        d.embedding = new_emb
                        d.embed_model = eff_embed_model
                        reembedded_dossiers.add(d.topic_id)

        if (
            not new_qas
            and not reprocess_qas
            and not pre_consumed_reprocess_ids
            and not reembedded_dossiers
            and not claims_backfill_ids
            and skipped_empty_max_id <= last_qa_id
        ):
            if not dry_run and (initialized_baseline or (has_pg_state and not pg_state_present)):
                with _pg_conn_ctx(pg) as conn:
                    if conn is not None and has_pg_state:
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO public.topic_v2_state "
                                "(state_key, last_qa_id, topic_count, failed_qa_ids, updated_at) "
                                "VALUES (%s, %s, %s, %s::jsonb, NOW()) "
                                "ON CONFLICT (state_key) DO UPDATE SET "
                                "last_qa_id = EXCLUDED.last_qa_id, "
                                "topic_count = EXCLUDED.topic_count, "
                                "failed_qa_ids = EXCLUDED.failed_qa_ids, "
                                "updated_at = NOW()",
                                ("default", int(last_qa_id), len(dossiers), "[]"),
                            )
                        if getattr(conn, "autocommit", None) is not True and callable(getattr(conn, "commit", None)):
                            conn.commit()
                _atomic_write_json(
                    state_path,
                    {
                        "last_qa_id": int(last_qa_id),
                        "baseline_initialized": True,
                        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "topic_count": len(dossiers),
                        "failed_qa_ids": [],
                        "reprocess_qa_ids": reprocess_qa_ids,
                    },
                )
            return {
                "dry_run": bool(dry_run),
                "previous_last_qa_id": last_qa_id,
                "new_last_qa_id": last_qa_id,
                "processed_qas": 0,
                "created_topics": 0,
                "updated_topics": 0,
                "superseded_claims": 0,
                "skipped_qas": 0,
                "upserted_topics": 0,
                "upserted_claims": 0,
                "migrated_claim_topics": 0,
                "claims_store": claims_store,
                "initialized_baseline": initialized_baseline,
                "pending_incomplete_qa_ids": pending_incomplete_qa_ids,
                "failed_qa_ids": [],
                "reprocessed_qa_ids": [],
                "remaining_reprocess_qa_ids": reprocess_qa_ids,
            }

        updated_dossiers = dossiers
        rep_report: ConsolidationReport | None = None
        new_report: ConsolidationReport | None = None
        succeeded_reprocess_ids: list[int] = []

        if reprocess_qas:
            updated_dossiers, rep_report = consolidator.consolidate_qa_stream(
                reprocess_qas,
                existing_dossiers=updated_dossiers,
            )
            if not rep_report.failed_qa_ids:
                succeeded_reprocess_ids = [int(q["id"]) for q in reprocess_qas]
            else:
                rep_failed = set(rep_report.failed_qa_ids)
                rep_ordered = sorted(
                    reprocess_qas,
                    key=lambda q: (_format_iso_ts(q.get("timestamp")), int(q.get("id") or 0)),
                )
                for q in rep_ordered:
                    qid = int(q.get("id") or 0)
                    if qid in rep_failed:
                        break
                    succeeded_reprocess_ids.append(qid)

        if new_qas and not (rep_report and rep_report.failed_qa_ids):
            updated_dossiers, new_report = consolidator.consolidate_qa_stream(
                new_qas,
                existing_dossiers=updated_dossiers,
            )
            inc_failed = set(new_report.failed_qa_ids)
            for rid in reprocess_qa_ids:
                if rid > last_qa_id and rid <= new_report.max_qa_id and rid not in inc_failed:
                    succeeded_reprocess_ids.append(rid)

        combined_failed = list(rep_report.failed_qa_ids if rep_report else ()) + list(
            new_report.failed_qa_ids if new_report else ()
        )
        report = ConsolidationReport(
            processed_qas=(rep_report.processed_qas if rep_report else 0) + (new_report.processed_qas if new_report else 0),
            created_topics=(rep_report.created_topics if rep_report else 0) + (new_report.created_topics if new_report else 0),
            updated_topics=(
                (rep_report.updated_topics if rep_report else 0)
                + (new_report.updated_topics if new_report else 0)
                + (len(reembedded_dossiers) if (rep_report is None and new_report is None) else 0)
            ),
            superseded_claims=(rep_report.superseded_claims if rep_report else 0) + (new_report.superseded_claims if new_report else 0),
            skipped_qas=(rep_report.skipped_qas if rep_report else 0) + (new_report.skipped_qas if new_report else 0),
            max_qa_id=new_report.max_qa_id if new_report is not None else max(last_qa_id, skipped_empty_max_id),
            failed_qa_ids=tuple(combined_failed),
        )
        changed_dossiers = [
            d
            for d in updated_dossiers
            if d.topic_id in reembedded_dossiers or before_map.get(d.topic_id) != d.to_dict()
        ]
        changed_ids = {d.topic_id for d in changed_dossiers}
        claims_sync_dossiers = [
            d for d in updated_dossiers if d.topic_id in changed_ids or d.topic_id in claims_backfill_ids
        ]
        # Never advance watermark beyond last succeeded/explicitly-skipped QA (nor past any pending incomplete QA).
        candidate_max_qa = max(last_qa_id, report.max_qa_id)
        if not report.failed_qa_ids:
            candidate_max_qa = max(candidate_max_qa, skipped_empty_max_id)
        if pending_incomplete_qa_ids:
            candidate_max_qa = min(candidate_max_qa, min(pending_incomplete_qa_ids) - 1)
        new_max_qa = max(last_qa_id, candidate_max_qa)
        consumed_reprocess_qa_ids = sorted(set(pre_consumed_reprocess_ids) | set(succeeded_reprocess_ids))
        remaining_reprocess_qa_ids = [rid for rid in reprocess_qa_ids if rid not in set(consumed_reprocess_qa_ids)]
        upserted_claims = 0

        if not dry_run:
            need_pg_tx = bool(
                changed_dossiers
                or (has_pg_claims and claims_sync_dossiers)
                or (
                    has_pg_state
                    and (
                        not pg_state_present
                        or new_max_qa > last_qa_id
                        or report.failed_qa_ids
                        or consumed_reprocess_qa_ids
                    )
                )
            )
            if need_pg_tx:
                with _pg_conn_ctx(pg) as conn:
                    if conn is None:
                        raise RuntimeError("consolidate_pg_incremental: PG connection lost before upsert")
                    manual_tx = getattr(conn, "autocommit", None) is True
                    with conn.cursor() as cur:
                        if manual_tx:
                            cur.execute("BEGIN")
                        try:
                            for dossier in changed_dossiers:
                                row = dossier.to_topic_row(embed_fn)
                                emb = row.get("embedding")
                                emb_str = "[" + ",".join(str(x) for x in emb) + "]" if emb is not None else None
                                row_fp = str(row.get("embed_model") or "")
                                try:
                                    cur.execute(
                                        "INSERT INTO topics (topic_id, title, summary, body, keywords, status, embed_model, embedding, updated_at) "
                                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, NOW()) "
                                        "ON CONFLICT (topic_id) DO UPDATE SET "
                                        "title = EXCLUDED.title, summary = EXCLUDED.summary, body = EXCLUDED.body, "
                                        "keywords = EXCLUDED.keywords, status = EXCLUDED.status, "
                                        "embed_model = COALESCE(NULLIF(EXCLUDED.embed_model, ''), topics.embed_model), "
                                        "embedding = EXCLUDED.embedding, "
                                        "updated_at = NOW()",
                                        (
                                            row["topic_id"],
                                            row["title"],
                                            row["summary"],
                                            row["body"],
                                            row["keywords"],
                                            row["status"],
                                            row_fp,
                                            emb_str,
                                        ),
                                    )
                                except Exception:
                                    if manual_tx:
                                        raise
                                    # Compat fallback for minimal fake test cursors lacking embed_model/updated_at.
                                    cur.execute(
                                        "INSERT INTO topics (topic_id, title, summary, body, keywords, status, embedding) "
                                        "VALUES (%s, %s, %s, %s, %s, %s, %s::vector) "
                                        "ON CONFLICT (topic_id) DO UPDATE SET "
                                        "title = EXCLUDED.title, summary = EXCLUDED.summary, body = EXCLUDED.body, "
                                        "keywords = EXCLUDED.keywords, status = EXCLUDED.status, embedding = EXCLUDED.embedding",
                                        (
                                            row["topic_id"],
                                            row["title"],
                                            row["summary"],
                                            row["body"],
                                            row["keywords"],
                                            row["status"],
                                            emb_str,
                                        ),
                                    )
                            if has_pg_claims and claims_sync_dossiers:
                                upserted_claims = _sync_dossiers_to_pg_claims(cur, claims_sync_dossiers)
                            if has_pg_state:
                                if has_pg_reprocess_col:
                                    cur.execute(
                                        "INSERT INTO public.topic_v2_state "
                                        "(state_key, last_qa_id, topic_count, failed_qa_ids, reprocess_qa_ids, updated_at) "
                                        "VALUES (%s, %s, %s, %s::jsonb, '[]'::jsonb, NOW()) "
                                        "ON CONFLICT (state_key) DO UPDATE SET "
                                        "last_qa_id = GREATEST(public.topic_v2_state.last_qa_id, EXCLUDED.last_qa_id), "
                                        "topic_count = EXCLUDED.topic_count, "
                                        "failed_qa_ids = EXCLUDED.failed_qa_ids, "
                                        "reprocess_qa_ids = COALESCE(("
                                        "    SELECT jsonb_agg(elem) "
                                        "    FROM jsonb_array_elements("
                                        "        COALESCE(public.topic_v2_state.reprocess_qa_ids, '[]'::jsonb)"
                                        "    ) AS elem "
                                        "    WHERE NOT (elem::text::bigint = ANY(%s::bigint[]))"
                                        "), '[]'::jsonb), "
                                        "updated_at = NOW()",
                                        (
                                            "default",
                                            int(new_max_qa),
                                            len(updated_dossiers),
                                            json.dumps(list(report.failed_qa_ids)),
                                            list(consumed_reprocess_qa_ids),
                                        ),
                                    )
                                else:
                                    cur.execute(
                                        "INSERT INTO public.topic_v2_state "
                                        "(state_key, last_qa_id, topic_count, failed_qa_ids, updated_at) "
                                        "VALUES (%s, %s, %s, %s::jsonb, NOW()) "
                                        "ON CONFLICT (state_key) DO UPDATE SET "
                                        "last_qa_id = EXCLUDED.last_qa_id, "
                                        "topic_count = EXCLUDED.topic_count, "
                                        "failed_qa_ids = EXCLUDED.failed_qa_ids, "
                                        "updated_at = NOW()",
                                        (
                                            "default",
                                            int(new_max_qa),
                                            len(updated_dossiers),
                                            json.dumps(list(report.failed_qa_ids)),
                                        ),
                                    )
                            if manual_tx:
                                cur.execute("COMMIT")
                        except Exception:
                            if manual_tx:
                                try:
                                    cur.execute("ROLLBACK")
                                except Exception:
                                    pass
                            raise
                    if not manual_tx and callable(getattr(conn, "commit", None)):
                        conn.commit()
            save_dossiers(dossiers_path, updated_dossiers)
            _atomic_write_json(
                state_path,
                {
                    "last_qa_id": int(new_max_qa),
                    "baseline_initialized": True,
                    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "topic_count": len(updated_dossiers),
                    "failed_qa_ids": list(report.failed_qa_ids),
                    "reprocess_qa_ids": remaining_reprocess_qa_ids,
                },
            )

        return {
            "dry_run": bool(dry_run),
            "previous_last_qa_id": last_qa_id,
            "new_last_qa_id": int(new_max_qa),
            "processed_qas": report.processed_qas,
            "created_topics": report.created_topics,
            "updated_topics": report.updated_topics,
            "superseded_claims": report.superseded_claims,
            "skipped_qas": report.skipped_qas,
            "upserted_topics": 0 if dry_run else len(changed_dossiers),
            "upserted_claims": 0 if dry_run else upserted_claims,
            "migrated_claim_topics": 0 if dry_run else len(claims_backfill_ids),
            "claims_store": claims_store,
            "initialized_baseline": initialized_baseline,
            "pending_incomplete_qa_ids": pending_incomplete_qa_ids,
            "failed_qa_ids": list(report.failed_qa_ids),
            "reprocessed_qa_ids": sorted(set(succeeded_reprocess_ids)),
            "remaining_reprocess_qa_ids": remaining_reprocess_qa_ids,
        }


def inspect_dossiers_summary(state_dir: str | Path) -> dict[str, Any]:
    """Return a concise JSON-serializable snapshot of persisted Topic V2 state."""
    root = Path(state_dir)
    state_path = root / "topic_v2_state.json"
    dossiers_path = root / "topic_v2_dossiers.json"

    state: dict[str, Any] = {}
    if state_path.is_file():
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = raw
        except Exception:
            state = {}

    dossiers = load_dossiers(dossiers_path)
    return {
        "state_dir": str(root),
        "last_qa_id": int(state.get("last_qa_id") or 0),
        "updated_at": str(state.get("updated_at") or ""),
        "topic_count": len(dossiers),
        "topics": [
            {
                "topic_id": d.topic_id,
                "title": d.title,
                "last_evidence_qa_id": int(d.last_evidence_qa_id),
                "current_claims_count": len(d.current_claims),
                "superseded_claims_count": len(d.superseded_claims),
                "summary": d.summary,
            }
            for d in dossiers
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for inspecting or incrementally running Topic V2 consolidation."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="v3core.topic_v2",
        description="Inspect or incrementally consolidate Topic V2 structured dossiers.",
    )
    parser.add_argument("--inspect", action="store_true", help="Inspect current Topic V2 watermark and dossiers.")
    parser.add_argument("--dry-run", action="store_true", help="Preview incremental consolidation without writing PG or state files.")
    parser.add_argument("--limit", type=int, default=100, help="Max new QA rows to process per run (default: 100).")
    parser.add_argument("--deterministic", action="store_true", help="Force offline deterministic consolidation without calling LLM.")
    parser.add_argument("--state-dir", default="", help="Override state/dossier directory (defaults to configured profile data dir).")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from .config import _resolve_data_dir, resolve_config

    cfg = None
    if args.state_dir:
        state_dir = Path(args.state_dir)
    else:
        cfg = resolve_config()
        state_dir = _resolve_data_dir(cfg)

    if args.inspect:
        payload = inspect_dossiers_summary(state_dir)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"topic_v2 inspect: state_dir={payload['state_dir']} "
                f"last_qa_id={payload['last_qa_id']} topics={payload['topic_count']}"
            )
            for item in payload["topics"]:
                print(
                    f"  - {item['topic_id']} | {item['title']} "
                    f"(last_qa={item['last_evidence_qa_id']}, "
                    f"current={item['current_claims_count']}, "
                    f"superseded={item['superseded_claims_count']}): {item['summary'][:80]}"
                )
        return 0

    if cfg is None:
        cfg = resolve_config()

    from .embedding import DURABLE_WRITE_EMBED_POLICY, call_embedding, safe_embed_cfg
    from .pg_store import PgEmbedStore

    embed_cfg = safe_embed_cfg(cfg)
    if embed_cfg is None:
        print("topic_v2 error: embedding endpoint/config is not configured.", file=sys.stderr)
        return 1

    pg_cfg = getattr(cfg, "pg", None)
    if pg_cfg is None:
        print("topic_v2 error: PostgreSQL storage is not configured.", file=sys.stderr)
        return 1

    pg = PgEmbedStore(cfg)
    llm_fn = None if args.deterministic else make_gateway_llm_fn(cfg)
    embed_fn = (
        lambda text: call_embedding(
            str(text or ""), embed_cfg, policy=DURABLE_WRITE_EMBED_POLICY
        )
        or []
    )

    result = consolidate_pg_incremental(
        pg,
        embed_fn=embed_fn,
        llm_fn=llm_fn,
        state_dir=state_dir,
        dry_run=bool(args.dry_run),
        batch_limit=max(int(args.limit or 100), 1),
        embed_model=str(embed_cfg.get("_fingerprint") or ""),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"topic_v2 sync: dry_run={result['dry_run']} "
            f"processed={result['processed_qas']} created={result['created_topics']} "
            f"updated={result['updated_topics']} superseded={result['superseded_claims']} "
            f"upserted={result['upserted_topics']} last_qa_id={result['new_last_qa_id']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
