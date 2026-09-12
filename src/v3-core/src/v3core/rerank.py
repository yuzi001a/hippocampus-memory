"""cross-encoder rerank API client — OpenAI 兼容格式 + 代理 + API key"""
from __future__ import annotations
import json
import logging
from typing import Any

import requests


try:
    from . import _safe_err
except (ImportError, AttributeError):
    try:
        from .. import _safe_err
    except (ImportError, AttributeError):
        def _safe_err(e, max_len=80):
            try:
                from . import _safe_err as _impl
            except ImportError:
                from .. import _safe_err as _impl
            globals()["_safe_err"] = _impl
            return _impl(e, max_len)

logger = logging.getLogger("v3core.rerank")


def rerank(
    query: str,
    documents: list[str],
    rerank_cfg: dict | None = None,
) -> list[float] | None:
    """调 cross-encoder rerank API，返回 relevance_score 列表。

    Args:
        query: 用户查询
        documents: 候选文档列表（简短文本，如 title / content_preview）
        rerank_cfg: 配置字典 {endpoint, apiKey/api_key, proxy, timeout}

    Returns:
        list[float] | None: 与 documents 一一对应的分数; 失败/未配置返 None
    """
    cfg = rerank_cfg or {}
    endpoint = (cfg.get("endpoint") or "").rstrip("/")
    if not endpoint or not documents:
        return None

    api_key = cfg.get("apiKey", "") or cfg.get("api_key", "")
    proxy = cfg.get("proxy", "") or ""
    timeout = float(cfg.get("timeout", 30))

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "query": query,
        "documents": documents,
        "top_n": len(documents),
    }
    model = cfg.get("model", "") or ""
    if model:
        body["model"] = model
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        resp = requests.post(
            endpoint, json=body, headers=headers,
            proxies=proxies, timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if len(results) != len(documents):
            logger.warning(
                "rerank 返 %d 分, 期待 %d — 降级",
                len(results), len(documents),
            )
            return None
        return [r.get("relevance_score", 0.0) for r in results]
    except requests.HTTPError as e:
        if e.response.status_code == 403 and not api_key:
            logger.info("rerank 403: 需配置 apiKey (config.yaml storage.rerank.apiKey)")
        else:
            logger.warning("rerank HTTP %d: %s", e.response.status_code, _safe_err(e)[:200])
        return None
    except Exception as e:
        logger.warning("rerank 失败 (降级): %s", _safe_err(e)[:200])
        return None
