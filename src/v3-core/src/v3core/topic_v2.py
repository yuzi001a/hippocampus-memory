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
       - Demotes legacy/stale topics whose ``last_qa`` trails recalled raw QA IDs.
       - On current-state queries, demotes ``superseded_qas``, stale ``note``/``yin``,
         and breaks stale-vs-current QA ties via recency without hiding unique old facts.
       - On retrospective ``why did we drop X`` queries, preserves raw historical QA
         evidence and suppresses recency decay.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

_PHASE1_SYSTEM_PROMPT = (
    "你是长期记忆系统中的两阶段主题整理器（Phase 1）。\n"
    "给定一条新对话 QA 与向量检索得到的 Top-K 候选主题摘要，请输出严格 JSON（不要 Markdown 代码块）：\n"
    "{\n"
    '  "action": "attach" | "create" | "skip",\n'
    '  "target_topic_id": "若 attach 填候选 topic_id，否则留空",\n'
    '  "topic_title": "简短主题名",\n'
    '  "update_type": "add" | "problem" | "evolve" | "correct" | "historical_note",\n'
    '  "claim_text": "当前生效的简洁结论，仅写新状态，不要把已废弃旧方案写进来",\n'
    '  "replaces_text": "若 update_type 为 evolve/correct/historical_note，填写被替代或已下线的旧方案标识",\n'
    '  "reason": "变更/替代/下线原因",\n'
    '  "keywords": ["关键词1", "关键词2"]\n'
    "}\n"
    "规则：1) 发生 A->B 演进或纠正时必须标为 evolve/correct 并填 replaces_text；\n"
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "replaced_by_claim_id": self.replaced_by_claim_id,
            "reason": self.reason,
            "superseded_at": self.superseded_at,
            "evidence_qa_ids": list(self.evidence_qa_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SupersededClaim":
        return cls(
            text=str(data.get("text") or ""),
            replaced_by_claim_id=str(data.get("replaced_by_claim_id") or ""),
            reason=str(data.get("reason") or ""),
            superseded_at=str(data.get("superseded_at") or ""),
            evidence_qa_ids=[int(x) for x in (data.get("evidence_qa_ids") or [])],
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


@dataclass(frozen=True)
class ConsolidationReport:
    processed_qas: int
    created_topics: int
    updated_topics: int
    superseded_claims: int
    skipped_qas: int
    max_qa_id: int


def encode_topic_v2_header(dossier: TopicDossier) -> str:
    """Encode compact freshness + lineage metadata under ``TopicRecall``'s 500-char slice."""
    payload = {
        "v": 2,
        "last_qa": int(dossier.last_evidence_qa_id),
        "last_at": str(dossier.last_evidence_at or "")[:32],
        "active_qas": dossier.active_qa_ids()[-8:],
        "superseded_qas": dossier.superseded_qa_ids()[-12:],
    }
    return f"<!-- topic_v2:{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)} -->"


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
    ) -> None:
        self.embed_fn = embed_fn
        self.llm_fn = llm_fn
        self.top_k_candidates = max(int(top_k_candidates), 1)

    def seed_from_legacy_topics(self, legacy_topics: Sequence[Mapping[str, Any]]) -> list[TopicDossier]:
        """Convert legacy ``topics`` rows into initial ``TopicDossier`` instances.

        Preserves legacy vocabulary bridges (``summary`` / ``body`` / ``keywords``) at
        watermark ``last_evidence_qa_id=0`` so subsequent ``evolve`` / ``correct`` notes cleanly
        supersede obsolete legacy claims while uncontradicted synthesis topics remain intact.
        """
        dossiers: list[TopicDossier] = []
        for t in legacy_topics:
            tid = str(t.get("topic_id") or "")
            if not tid:
                continue
            title = str(t.get("title") or "")
            summary = str(t.get("summary") or "").strip()
            body = strip_topic_v2_header(str(t.get("body") or "")).strip()
            seed_text = summary or body
            claims: list[TopicClaim] = []
            if seed_text:
                claims.append(
                    TopicClaim(
                        claim_id=f"c_seed_{tid}",
                        text=seed_text,
                        kind="legacy_seed",
                        evidence_qa_ids=[],
                    )
                )
            raw_emb = t.get("embedding")
            emb = [float(x) for x in raw_emb] if raw_emb is not None else None
            dossier = TopicDossier(
                topic_id=tid,
                title=title,
                summary=seed_text,
                current_claims=claims,
                superseded_claims=[],
                keywords=[str(k) for k in (t.get("keywords") or [])],
                last_evidence_qa_id=0,
                last_evidence_at="",
                status=str(t.get("status") or "active"),
                embedding=emb,
            )
            if dossier.embedding is None and self.embed_fn is not None:
                dossier.embedding = self.embed_fn(dossier.embedding_text())
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
        qa_id = int(qa.get("id") or 0)
        question = str(qa.get("question") or "").strip()
        answer = str(qa.get("answer") or "").strip()
        ts = _format_iso_ts(qa.get("timestamp"))
        if not question and not answer:
            return None

        qa_text = f"{question}\n{answer}".strip()
        qa_emb = qa.get("embedding")
        if qa_emb is None:
            qa_emb = self.embed_fn(qa_text)
        candidates = route_candidate_topics(
            qa_emb, dossiers, top_k=self.top_k_candidates, query_text=qa_text
        )

        if self.llm_fn is not None:
            return self._distill_with_llm(qa_id, question, answer, ts, candidates)
        return self._distill_deterministic(qa_id, question, answer, ts, qa_text, candidates)

    def _distill_with_llm(
        self,
        qa_id: int,
        question: str,
        answer: str,
        ts: str,
        candidates: list[tuple[float, TopicDossier]],
    ) -> Phase1Note | None:
        cand_desc = [
            {
                "topic_id": d.topic_id,
                "title": d.title,
                "summary": d.summary,
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

        action = str(data.get("action") or "skip").strip().lower()
        if action not in ("attach", "create", "skip"):
            return None
        update_type = str(data.get("update_type") or "add").strip().lower()
        if update_type not in ("add", "problem", "evolve", "correct", "historical_note", "skip"):
            update_type = "add"

        valid_ids = {d.topic_id for _, d in candidates}
        target_id = str(data.get("target_topic_id") or "").strip()
        if action == "attach" and target_id not in valid_ids:
            if candidates:
                target_id = candidates[0][1].topic_id
            else:
                action = "create"
                target_id = ""

        kws = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]
        return Phase1Note(
            action=action,
            target_topic_id=target_id,
            topic_title=str(data.get("topic_title") or _derive_topic_title(question, answer)).strip(),
            update_type=update_type,
            claim_text=_clean_clause(str(data.get("claim_text") or "")),
            replaces_text=_clean_clause(str(data.get("replaces_text") or "")),
            reason=_clean_clause(str(data.get("reason") or "")),
            keywords=kws,
            evidence_qa_id=qa_id,
            timestamp=ts,
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

        # 4. Problem / limitation report on an existing approach.
        target = self._pick_target_candidate(qa_text, candidates)
        clean_ans = _clean_clause(answer)
        codes = _ASCII_CODE_TOKEN_RE.findall(qa_text)
        update_type = "problem" if _PROBLEM_HINT_RE.search(qa_text) else "add"
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

        if note.evidence_qa_id > target.last_evidence_qa_id:
            target.last_evidence_qa_id = note.evidence_qa_id
        if note.timestamp:
            target.last_evidence_at = note.timestamp

        if note.update_type in ("evolve", "correct"):
            new_cid = f"c_{note.evidence_qa_id}_{len(target.current_claims) + len(target.superseded_claims) + 1}"
            old_low = note.replaces_text.strip().lower()
            kept_claims: list[TopicClaim] = []
            for claim in target.current_claims:
                c_low = claim.text.lower()
                is_superseded = (
                    (bool(old_low) and old_low in c_low)
                    or claim.kind in ("legacy_seed", "problem")
                )
                if is_superseded:
                    target.superseded_claims.append(
                        SupersededClaim(
                            text=claim.text,
                            replaced_by_claim_id=new_cid,
                            reason=note.reason or f"被 {note.claim_text} 替代",
                            superseded_at=note.timestamp,
                            evidence_qa_ids=list(claim.evidence_qa_ids),
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
            if old_low:
                target.keywords = [kw for kw in target.keywords if kw.lower() != old_low]
            for kw in note.keywords:
                if kw and (not old_low or kw.lower() != old_low) and kw not in target.keywords:
                    target.keywords.append(kw)

        elif note.update_type == "historical_note":
            target.superseded_claims.append(
                SupersededClaim(
                    text=note.replaces_text or note.topic_title,
                    replaced_by_claim_id="",
                    reason=note.reason or note.claim_text,
                    superseded_at=note.timestamp,
                    evidence_qa_ids=[note.evidence_qa_id] if note.evidence_qa_id > 0 else [],
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
        target.embedding = self.embed_fn(target.embedding_text())
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
        max_qa_id = max((d.last_evidence_qa_id for d in dossiers), default=0)

        for qa in ordered:
            qid = int(qa.get("id") or 0)
            if qid > max_qa_id:
                max_qa_id = qid
            note = self.distill_qa(qa, dossiers)
            if note is None or note.action == "skip" or note.update_type == "skip":
                skipped += 1
                continue
            touched = self.apply_note(note, dossiers)
            if touched is None:
                skipped += 1
                continue
            if touched.topic_id in initial_ids:
                updated_ids.add(touched.topic_id)

        created = len([d for d in dossiers if d.topic_id not in initial_ids])
        superseded_after = sum(len(d.superseded_claims) for d in dossiers)
        report = ConsolidationReport(
            processed_qas=len(ordered),
            created_topics=created,
            updated_topics=len(updated_ids),
            superseded_claims=max(0, superseded_after - superseded_before),
            skipped_qas=skipped,
            max_qa_id=max_qa_id,
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


def apply_topic_v2_ranking_guard(
    scored: list[Any],
    *,
    query: str,
    trace: Any = None,
    probe_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Enforce read-side freshness, watermark check, and A->B supersession in ``scored``.

    Mutates ``hit.content``, ``hit.content_preview`` (stripping internal ``topic_v2`` headers),
    and ``hit.rrf_score`` in place prior to final score sorting.
    """
    is_hist = is_historical_trace_query(query)
    topic_meta: dict[str, dict[str, Any] | None] = {}
    qa_ids: list[int] = []
    qa_timestamps: dict[str, datetime] = {}

    for hit in scored:
        kind = getattr(hit, "kind", "")
        sid = str(getattr(hit, "source_id", "") or "")
        if kind == "topic":
            raw_body = str(getattr(hit, "content", "") or getattr(hit, "content_preview", "") or "")
            meta = parse_topic_v2_header(raw_body)
            topic_meta[sid] = meta
            if meta is not None:
                clean_full = strip_topic_v2_header(raw_body)
                hit.content = clean_full
                hit.content_preview = clean_full[:500]
        elif kind == "qa":
            qid = _parse_qa_id(sid)
            if qid is not None:
                qa_ids.append(qid)
            dt = _parse_iso_ts(getattr(hit, "created_at", ""))
            if dt is not None:
                qa_timestamps[sid] = dt

    max_qa_id = max(qa_ids, default=0)
    latest_qa_ts = max(qa_timestamps.values(), default=None)
    earliest_qa_ts = min(qa_timestamps.values(), default=None)
    qa_span_days = (
        (latest_qa_ts - earliest_qa_ts).total_seconds() / 86400.0
        if (latest_qa_ts is not None and earliest_qa_ts is not None)
        else 0.0
    )

    superseded_qa_ids: set[int] = set()
    fresh_topic_sids: set[str] = set()
    stale_topic_sids: set[str] = set()
    for sid, meta in topic_meta.items():
        if meta is not None and int(meta.get("v") or 0) == 2 and (max_qa_id == 0 or int(meta.get("last_qa") or 0) >= max_qa_id):
            fresh_topic_sids.add(sid)
            for qid in (meta.get("superseded_qas") or []):
                try:
                    superseded_qa_ids.add(int(qid))
                except (TypeError, ValueError):
                    pass
        elif max_qa_id > 0:
            # Either legacy v1 topic without header, or v2 topic lagging behind recalled QA.
            stale_topic_sids.add(sid)

    has_fresh_v2_topic = bool(fresh_topic_sids)

    # First, adjust non-demoted QA recency tie-breaks on current-state queries (lag-window protection).
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

    # Compute the lowest score among fresh active candidates so every demoted hit is pushed below it.
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

    for hit in scored:
        kind = getattr(hit, "kind", "")
        sid = str(getattr(hit, "source_id", "") or "")
        if kind == "topic" and sid in stale_topic_sids:
            hit.rrf_score = _cap_demoted_score(float(hit.rrf_score), 0.25, min_fresh_score)
            demoted_topics += 1
            if probe_fn is not None and trace is not None:
                probe_fn(trace, "score", sid, "temporal", hit.rrf_score, operation="topic_v2_freshness_guard")
        elif kind == "qa" and not is_hist:
            qid = _parse_qa_id(sid)
            if qid is not None and qid in superseded_qa_ids:
                hit.rrf_score = _cap_demoted_score(float(hit.rrf_score), 0.20, min_fresh_score)
                demoted_superseded_qas += 1
                if probe_fn is not None and trace is not None:
                    probe_fn(trace, "score", sid, "temporal", hit.rrf_score, operation="topic_v2_freshness_guard")
        elif kind in ("note", "yin") and not is_hist:
            dt = _parse_iso_ts(getattr(hit, "created_at", ""))
            is_older_than_qa = (
                latest_qa_ts is not None and (dt is None or dt < latest_qa_ts)
            )
            if is_older_than_qa or (has_fresh_v2_topic and max_qa_id > 0):
                hit.rrf_score = _cap_demoted_score(float(hit.rrf_score), 0.25, min_fresh_score)
                demoted_derived += 1
                if probe_fn is not None and trace is not None:
                    probe_fn(trace, "score", sid, "temporal", hit.rrf_score, operation="topic_v2_freshness_guard")

    return {
        "is_historical": is_hist,
        "max_qa_id": max_qa_id,
        "fresh_topics": len(fresh_topic_sids),
        "demoted_topics": demoted_topics,
        "demoted_superseded_qas": demoted_superseded_qas,
        "demoted_derived": demoted_derived,
        "qa_recency_adjusted": qa_recency_adjusted,
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


def consolidate_pg_incremental(
    pg: Any,
    *,
    embed_fn: Callable[[str], list[float]],
    llm_fn: Callable[[list[dict[str, str]]], str] | None = None,
    state_dir: str | Path,
    dry_run: bool = False,
    batch_limit: int = 100,
) -> dict[str, Any]:
    """Incrementally consolidate new PG ``qa_pairs`` into ``TopicDossier``s and PG ``topics``.

    Maintains a crash-safe watermark in ``<state_dir>/topic_v2_state.json`` and full lineage
    dossiers in ``<state_dir>/topic_v2_dossiers.json``. Never mutates or deletes ``qa_pairs``.
    """
    if pg is None:
        raise ValueError("consolidate_pg_incremental requires a non-None PG store")

    root = Path(state_dir)
    state_path = root / "topic_v2_state.json"
    dossiers_path = root / "topic_v2_dossiers.json"

    last_qa_id = 0
    if state_path.is_file():
        try:
            raw_state = json.loads(state_path.read_text(encoding="utf-8"))
            last_qa_id = int(raw_state.get("last_qa_id") or 0)
        except Exception:
            last_qa_id = 0

    consolidator = TopicConsolidatorV2(embed_fn=embed_fn, llm_fn=llm_fn)
    dossiers = load_dossiers(dossiers_path)

    new_qas: list[dict[str, Any]] = []
    with _pg_conn_ctx(pg) as conn:
        if conn is None:
            raise RuntimeError("consolidate_pg_incremental: failed to obtain PG connection")
        with conn.cursor() as cur:
            if not dossiers:
                try:
                    cur.execute(
                        "SELECT topic_id, title, COALESCE(summary, ''), COALESCE(body, ''), "
                        "keywords, status, embedding FROM topics WHERE status='active'"
                    )
                    legacy_rows = []
                    for row in cur.fetchall() or []:
                        legacy_rows.append(
                            {
                                "topic_id": row[0],
                                "title": row[1] or "",
                                "summary": row[2] or "",
                                "body": row[3] or "",
                                "keywords": list(row[4] or []) if isinstance(row[4], (list, tuple)) else [],
                                "status": row[5] or "active",
                                "embedding": _parse_pg_emb(row[6]),
                            }
                        )
                    if legacy_rows:
                        dossiers = consolidator.seed_from_legacy_topics(legacy_rows)
                except Exception:
                    dossiers = []

            cur.execute(
                "SELECT id, timestamp, question, answer FROM qa_pairs "
                "WHERE id > %s AND answer IS NOT NULL AND session_id NOT LIKE '%%.trajectory%%' "
                "ORDER BY id ASC LIMIT %s",
                (last_qa_id, max(int(batch_limit), 1)),
            )
            for row in cur.fetchall() or []:
                new_qas.append(
                    {
                        "id": int(row[0]),
                        "timestamp": row[1],
                        "question": str(row[2] or ""),
                        "answer": str(row[3] or ""),
                    }
                )

    if not new_qas:
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
        }

    before_map = {d.topic_id: d.to_dict() for d in dossiers}
    updated_dossiers, report = consolidator.consolidate_qa_stream(
        new_qas,
        existing_dossiers=dossiers,
    )
    changed_dossiers = [
        d for d in updated_dossiers if before_map.get(d.topic_id) != d.to_dict()
    ]
    new_max_qa = max(last_qa_id, report.max_qa_id)

    if not dry_run:
        with _pg_conn_ctx(pg) as conn:
            if conn is None:
                raise RuntimeError("consolidate_pg_incremental: PG connection lost before upsert")
            with conn.cursor() as cur:
                for dossier in changed_dossiers:
                    row = dossier.to_topic_row(embed_fn)
                    emb = row.get("embedding")
                    emb_str = "[" + ",".join(str(x) for x in emb) + "]" if emb is not None else None
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
            if callable(getattr(conn, "commit", None)):
                conn.commit()
        save_dossiers(dossiers_path, updated_dossiers)
        _atomic_write_json(
            state_path,
            {
                "last_qa_id": int(new_max_qa),
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "topic_count": len(updated_dossiers),
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
