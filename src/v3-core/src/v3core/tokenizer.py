"""分词器 — jieba 优先, CJK tri-gram 兜底

使用策略:
1. 尝试 import jieba, 成功则走精准模式分词（cut_all=False）
2. 失败则走 CJK tri-gram 滑动窗口 (比之前的 bigram 更准)
3. 英文/数字保持原样, 按空格/标点拆分
"""
from __future__ import annotations
import re
import logging

logger = logging.getLogger("v3core.tokenizer")

_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\ufa00-\ufaff]+")
_jieba_available: bool | None = None


def _check_jieba() -> bool:
    global _jieba_available
    if _jieba_available is not None:
        return _jieba_available
    try:
        import jieba
        _jieba_available = True
    except ImportError:
        _jieba_available = False
    return _jieba_available


def tokenize(text: str) -> list[str]:
    """RETURN: 去重的分词 token 列表"""
    if not text:
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    text_lower = text.lower()

    if _check_jieba():
        import jieba
        for word in jieba.cut(text_lower, cut_all=False):
            word = word.strip()
            if word and word not in seen:
                seen.add(word)
                tokens.append(word)
        return tokens

    segments = re.split(r"([\u3400-\u4dbf\u4e00-\u9fff\ufa00-\ufaff]+)", text_lower)
    for seg in segments:
        if not seg:
            continue
        if _CJK_RUN_RE.match(seg):
            if len(seg) <= 3:
                if seg not in seen:
                    seen.add(seg)
                    tokens.append(seg)
            else:
                for i in range(len(seg) - 2):
                    tk = seg[i : i + 3]
                    if tk not in seen:
                        seen.add(tk)
                        tokens.append(tk)
                if seg not in seen:
                    seen.add(seg)
                    tokens.append(seg)
        else:
            for word in re.split(r"[\s,;:.!?()\[\]{}<>/@#$%^&*+=~\"'|\-]+", seg):
                w = word.strip()
                if w and w not in seen:
                    seen.add(w)
                    tokens.append(w)
    return tokens


def build_query_tokens(query: str) -> list[str]:
    """RETURN: 查询的分词 token 列表"""
    return tokenize(query)


def score_keyword_on_text(title: str, tags: list[str], content: str, query_tokens: list[str]) -> float:
    """RETURN: 0.0-1.0 的关键词匹配分数"""
    if not query_tokens:
        return 0.0
    score = 0.0
    title_lower = title.lower()
    content_lower = content.lower() if content else ""
    tag_lower_list = [t.lower() for t in tags if isinstance(t, str)]

    for qt in query_tokens:
        if not qt:
            continue
        if qt in title_lower:
            score += 0.4
            continue
        tag_hit = False
        for tl in tag_lower_list:
            if qt in tl:
                score += 0.3
                tag_hit = True
                break
        if tag_hit:
            continue
        if content_lower and qt in content_lower:
            score += 0.25
    return min(score, 1.0)
