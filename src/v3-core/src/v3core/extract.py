"""对话 -> 碑卡提取 (调 LLM)"""
from __future__ import annotations
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any
from .config import resolve_config, _resolve_prompt as _cfg_prompt
from .llm import LLMClient
from .card_store import DeepStore

logger = logging.getLogger("v3core.extract")


class LLMNoResult(Exception):
    """Raised when at least one LLM call was attempted but no parseable cards
    were produced (LLM returned empty content or non-JSON garbage).

    This is distinct from network/HTTP errors (which raise from llm.chat
    directly) and from legitimately empty input (raw_text < 200 chars,
    handled by callers upstream — returns [] without raising).

    Propagates to handle_v3_extract which converts it to
    ``{"success": False, "error": "..."}`` so upstream cron / tool callers
    no longer treat silent empty results as success.
    """


DEFAULT_EXTRACT_PROMPT = """你是一个记忆提取助手。从对话中提取有价值的记忆，输出为 JSON 列表。

每条记忆包含:
- category: 分类ID (memory/lessons/decisions/projects/creative/evolution/system 或自由类别)
- title: 标题 (20字内)
- content: 正文 (100-300字，写清楚什么、为什么、怎么做)
- tags: 标签列表
- when: 时间 (可选)
- where: 地点 (可选)
- who: 涉及人物 (可选)
- why: 理由 (可选)

返回 JSON 数组。只输出 JSON，不要 markdown 代码块。

【结构化输出契约】
- 严格 JSON 数组（[...]），顶层不包字典。
- 每条对象必含字段：category / title / content / tags。
- 可选字段仅在对话中明确出现时填写，缺失就省略（不要填空字符串）。
- title ≤20 字，content 100-300 字，tags 为字符串数组（每项 ≤30 字）。
- 输出语言与对话原文保持一致。"""

def chunk_text(text: str, max_chars: int = 12000, overlap: int = 1000) -> list[str]:
    """长会话分段，每段 max_chars 字符，相邻段重叠 overlap 字符"""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end < len(text):
            # 在 max_chars 附近找换行符切
            cut = text.rfind("\n", start + max_chars - overlap, end)
            if cut > start:
                end = cut
        chunks.append(text[start:end])
        start = end - overlap
    return chunks

def _clean_tags(tags: list[str]) -> list[str]:
    """清洗标签：过滤控制字符、替换无意义标点"""
    cleaned = []
    for t in tags:
        t = re.sub(r"[\x00-\x1f\x7f]", "", t)
        t = t.strip(" ,.;:!?-")
        if t and len(t) <= 30:
            cleaned.append(t)
    return cleaned

def _parse_json_with_fix(text: str) -> Any:
    """容忍 M3 think block、markdown 包裹等输出格式问题"""
    text = text.strip()
    # 去除 think block
    if "\n" in text:
        text = re.sub(r"(?s)^.*?<｜end▁of▁thinking｜>", "", text, count=1)
    # 去除 markdown 代码块
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # 尝试修复截断
    text = text.strip()
    # 尝试解析
    result = json.loads(text)
    return result

def build_dedup_context(store: DeepStore | None, days: int = 14, max_cards: int = 20, max_chars_per_card: int = 60) -> str:
    """建去重上下文：近期已有卡片列表，供 LLM 避免重复提取"""
    if store is None:
        return ""
    from datetime import datetime as dt
    cutoff = dt.now() - timedelta(days=days)
    NL = chr(10)
    try:
        index = store.get_index()
        recent = []
        for rel, meta in (index.get("files") or {}).items():
            cat = meta.get("category", "")
            title_part = (meta.get("title") or "")[:max_chars_per_card]
            mtime = meta.get("mtime", 0)
            if mtime and dt.fromtimestamp(mtime) >= cutoff:
                recent.append(f"- [{cat}] {title_part}")
        if recent:
            return "近期已有记忆:" + NL + NL.join(recent[:max_cards])
        return ""
    except Exception:
        return ""

def extract_from_session(raw_text: str, config: dict | None = None,
                         store: DeepStore | None = None) -> list[dict]:
    """从对话文本提取记忆卡，支持长会话分段 + 去重上下文"""
    if not raw_text or len(raw_text) < 200:
        return []
    cfg = config or resolve_config()
    llm = LLMClient(cfg)
    # 统一读取: prompts.extract 可以是内联文本，也可以是文件路径 (.md/.txt/.prompt/.yaml/.yml)
    # _cfg_prompt 自动识别文件路径并读文件 (文件路径必须 ≤500 字符且无换行 — 兼容历史用法)
    system_prompt = _cfg_prompt(cfg, "extract", DEFAULT_EXTRACT_PROMPT)

    # 去重上下文
    dedup_ctx = build_dedup_context(store)
    if dedup_ctx:
        system_prompt += "\n\n注意: 以下是已有记忆，避免重复提取相同的卡片。\n" + dedup_ctx

    # 长会话分段
    chunks = chunk_text(raw_text)
    all_cards = []
    chunks_attempted = 0   # LLM 真的调了几次
    chunks_empty = 0       # 调了但返回空
    chunks_bad_json = 0    # 调了但返回非JSON

    for chunk in chunks:
        user_msg = f"从以下对话提取记忆:\n\n{chunk}"
        result = llm.chat(system_prompt, [
            {"role": "user", "content": user_msg}
        ], temperature=0.3)

        chunks_attempted += 1

        if not result or not result.strip():
            logger.warning("LLM 返回空, 跳过该分段")
            chunks_empty += 1
            continue

        try:
            cards = _parse_json_with_fix(result)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("LLM 返回非JSON (分段): %s ... err=%s", result[:200], e)
            chunks_bad_json += 1
            continue

        if isinstance(cards, dict):
            cards = [cards]
        if isinstance(cards, list):
            # 清洗标签
            for c in cards:
                if "tags" in c and isinstance(c["tags"], list):
                    c["tags"] = _clean_tags(c["tags"])
            all_cards.extend(cards)

    # 失败信号: 至少调过一次 LLM, 但所有调用都没产出任何卡.
    # 这种情况之前静默返空列表 (success=True), 现在抛 LLMNoResult 让上层 (V3Core /
    # handle_v3_extract) 知道这是 LLM 失败而非"对话无内容可提取".
    if chunks_attempted > 0 and not all_cards:
        msg = (
            f"LLM 调用 {chunks_attempted} 次均未产出有效卡 "
            f"(empty={chunks_empty}, bad_json={chunks_bad_json})"
        )
        logger.error("[extract] %s", msg)
        raise LLMNoResult(msg)

    return all_cards
