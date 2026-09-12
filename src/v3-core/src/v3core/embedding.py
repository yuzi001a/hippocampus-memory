"""bge 远程 embedding 客户端 — OpenAI 兼容格式 + 代理 + API key

阶段1（embedding/vector 一致性守护）核心契约（2026-08-19/20）：

* 唯一入口: ``build_embed_cfg(cfg)`` — 所有构造点必须走它, **不得手拼 dict**。
* fail-closed: model / endpoint 缺失立即 raise ValueError, 禁止隐式 fallback。
  embedding **无 fallback 概念**（fallback 是 LLM 的事, 两者不要混）。
* 校验发生在 cache 命中之前 — 历史已经发生过"cache 命中遮蔽 400"的盲点。
* embed_batch 禁用 zero-vector 兜底: 重试结束必须抛真实错误, 调用方按需处理。
* 每次成功调用后生成 profile fingerprint (sha256(provider|base_url|model|dim|...)),
  写入 PG ``<vector_table>.embed_model`` 列, GROUP BY 混库检查。

不连真实 HTTP — 任何 HTTP 行为由本文件唯一发起, 测试通过 monkeypatch
``requests.post`` 验证。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

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


logger = logging.getLogger("v3core.embedding")

_EMBED_CACHE: dict[str, tuple[float, list[float]]] = {}
_EMBED_TTL = 600.0  # 10分钟，覆盖对话流中重复 query
_FINGERPRINT_LEN = 12  # sha256 前缀长度（足以唯一标识 model 字符串）


# ─── Embed profile (阶段1) ─────────────────────────────────────────────


@dataclass
class EmbedProfile:
    """嵌入模型 profile — 不含任何 secret, 仅描述 identity。

    用于:
    * 失败排查日志 (provider/base_url/model)
    * PG ``embed_model`` 列 fingerprint 校验
    * 跨表 / 跨库混库检测 (GROUP BY embed_model)
    """

    provider: str = ""
    base_url: str = ""
    endpoint: str = ""
    model: str = ""
    dim: int = 0
    pooling: str = ""  # cls / mean / max — 如可推导
    normalization: bool = False
    request_format: str = "openai"  # openai / 其他

    def fingerprint(self) -> str:
        """稳定 sha256 前缀指纹 — 同样配置永远产生同样指纹 (字段顺序固定, 无 secret).

        空 model 返回空串 (历史/未配置 标识, 调用方据此决定是否写库)。
        """
        if not self.model:
            return ""
        canonical = "|".join([
            self.provider or "",
            self.base_url or "",
            self.endpoint or "",
            self.model,
            str(self.dim or 0),
            self.pooling or "",
            "1" if self.normalization else "0",
            self.request_format or "",
        ])
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]

    def to_log_dict(self) -> dict:
        """仅返回非 secret 字段, 供日志/profile 上报."""
        d = asdict(self)
        # 不打印任何凭据 — provider 端点可能含 query, 这里截到 ? 前
        if d.get("endpoint") and "?" in d["endpoint"]:
            d["endpoint"] = d["endpoint"].split("?", 1)[0]
        return d


def _derive_pooling_and_normalization(model: str) -> tuple[str, bool]:
    """按模型名启发式推导 pooling + normalization — 仅日志用途, 不参与逻辑判断.

    已知对照:
    * BAAI/bge-m3: cls, True
    * BAAI/bge-large-zh-v1.5: cls, True
    * text-embedding-3-* / text-embedding-ada-002: mean, True
    """
    m = (model or "").lower()
    if "bge-m3" in m or "bge-large" in m or "bge-small" in m:
        return "cls", True
    if m.startswith("text-embedding"):
        return "mean", True
    return "", False


# ─── build_embed_cfg (阶段1 唯一工厂) ────────────────────────────────


# 用于 _extract_raw_embed_dict 的内部兼容逻辑: cfg 可能是 V3Config / dict / None
def _embed_section_present(cfg) -> bool:
    """区分"embed 不存在 / 默认空块" vs "用户实际配置了 embed (待校验)".

    历史 (2026-09-10 之前): 任何非 None embed 块都视为"已配置", 导致
    _LEGACY_DEFAULT_CONFIG.storage.embed 的默认空块 (endpoint="", model="",
    dim=1024) 总是被当成"已配置但缺 endpoint" → safe_embed_cfg 误抛
    ValueError: embed endpoint 未配置. 公共文档承诺 embedding 可选 / 关键字
    only 模式, 期望"未配置 embed"应该返回 disabled, **不抛**.

    新契约:
      * cfg 为 None → False (disabled)
      * embed 属性/键不存在 → False (disabled)
      * embed 属性存在但 endpoint 与 model **皆**空 (即默认空块 / 用户显式
        置空) → False (disabled, 兼容"未配 embed 也能跑文件模式")
      * embed 块存在且至少 endpoint 或 model 非空 → True, 委托 build_embed_cfg
        做后续 fail-closed 校验 (endpoint 缺失或 model 缺失 → 抛 ValueError)

    注意: 这一行为变化仅影响 build_embed_cfg / safe_embed_cfg 的"是否抛错"
    决策; 已有 endpoint + model 的完整配置与 dict 形态契约保持完全一致.
    """
    if cfg is None:
        return False
    if isinstance(cfg, dict):
        storage = cfg.get("storage")
        if isinstance(storage, dict) and "embed" in storage:
            inner = storage.get("embed")
        elif "embed" in cfg:
            inner = cfg.get("embed")
        else:
            return False
        if not isinstance(inner, dict):
            return False
        # 默认空块判定: endpoint 与 model 皆空视为"未配置" (兼容默认空 dict).
        return bool((inner.get("endpoint") or "").strip() or (inner.get("model") or "").strip())
    # dataclass / V3Config-like: 仅当 cfg.embed 存在且至少 endpoint 或 model 非空时,
    # 视为已配置.
    if not hasattr(cfg, "embed"):
        return False
    inner = getattr(cfg, "embed")
    if inner is None:
        return False
    # 兼容历史 caller 把 dict 直接塞进 cfg.embed (e.g. observer SimpleNamespace 测试).
    if isinstance(inner, dict):
        return bool((inner.get("endpoint") or "").strip() or (inner.get("model") or "").strip())
    endpoint = getattr(inner, "endpoint", "") or ""
    model = getattr(inner, "model", "") or ""
    return bool(endpoint.strip() or model.strip())




def _extract_raw_embed_dict(cfg) -> dict:
    """从 V3Config / dict / None 中提取 raw embed 子配置 dict.

    与 ``__init__._extract_embed_cfg`` 同等语义, 但本文件独立可导入
    (避免循环引用, 调用方愿意直接用 build_embed_cfg 时不必 import __init__).
    """
    if cfg is None:
        return {}
    if isinstance(cfg, dict):
        storage = cfg.get("storage")
        if isinstance(storage, dict) and storage.get("embed"):
            return storage["embed"]
        embed = cfg.get("embed")
        return embed if isinstance(embed, dict) else {}
    if hasattr(cfg, "embed") and cfg.embed is not None:
        # EmbedConfig dataclass
        return cfg.embed.to_legacy_dict() if hasattr(cfg.embed, "to_legacy_dict") else dict(cfg.embed)
    return {}


def build_embed_cfg(cfg) -> dict:
    """**唯一** embed_cfg 工厂 — 阶段1 强制入口.

    行为契约:
    * cfg 不存在 / 完全没有 embed 子配置 → 返回 ``{"endpoint": "", "model": ""}``,
      调用方据此走 disabled 路径 (无 embedding 语义), **不抛** (兼容历史).
    * cfg 有 embed 子配置但 ``endpoint`` 缺失 → ``ValueError("embed endpoint 未配置")``.
    * cfg 有 embed 子配置但 ``model`` 缺失 → ``ValueError("embed model 未配置 ...")``.
    * dim 缺失 → 1024 (保留历史默认值, 日志里打 warning).
    * proxy / api_key 原样透传, 不做任何屏蔽 (调用方负责不在日志裸打).

    返回 dict 同时携带:
      * 原始 5 字段: endpoint / dim / api_key / apiKey / model / proxy
      * ``_profile``: EmbedProfile dataclass (provider / base_url / pooling / ...)
      * ``_fingerprint``: sha256[:12] 指纹串 (空串 = model 缺失, 不应调用)
      * ``_raw``: 原始 cfg 透传, 给需要反查的极端场景

    测试约定: monkeypatch ``v3core.embedding.build_embed_cfg`` 即可拦截所有
    cfg 构造; 测试**不连真实 HTTP**, requests.post 也走 monkeypatch.
    """
    # 2026-09-10 G4 hardening: _embed_section_present 是"用户是否实际配置
    # 了 embed"的唯一判断; _extract_raw_embed_dict 在 V3Config 默认空块场景
    # 会返回非空 dict (因 dim=1024), 因此**不能**再用 ``not raw`` 作为 disabled
    # gate. 新逻辑: 仅当 _embed_section_present 为 False (即 None / 完全无
    # embed 键 / endpoint+model 皆空) 时返回 disabled. 已配置但 endpoint 或
    # model 缺失 → fail-closed raise (下方校验分支).
    raw = _extract_raw_embed_dict(cfg)
    if not _embed_section_present(cfg):
        # 显式 disabled: 无配置 / 默认空块 — 调用方走 disabled 分支.
        # 故意不抛, 兼容历史"未配 embed 也能跑文件模式".
        return {
            "endpoint": "",
            "dim": 0,
            "model": "",
            "api_key": "",
            "apiKey": "",
            "proxy": "",
            "_profile": EmbedProfile(),
            "_fingerprint": "",
            "_raw": cfg,
        }

    endpoint = (raw.get("endpoint") or "").rstrip("/")
    model = raw.get("model") or ""

    if not endpoint:
        raise ValueError("embed endpoint 未配置 (config.yaml storage.embed.endpoint)")
    if not model:
        # **fail-closed**: 缺 model 立即抛, 绝不隐式 fallback.
        raise ValueError(
            "embed model 未配置 (config.yaml storage.embed.model) — "
            "embedding 无 fallback, 必须显式声明 model"
        )

    dim = raw.get("dim")
    if not isinstance(dim, int) or dim <= 0:
        dim = raw.get("dimension", 1024) or 1024
        try:
            dim = int(dim)
        except (TypeError, ValueError):
            dim = 1024
        logger.warning("embed dim 缺失/异常, 退回默认 1024")

    api_key = raw.get("api_key", "") or raw.get("apiKey", "")
    proxy = raw.get("proxy", "") or ""

    # 解析 provider/base_url: OpenAI 兼容 endpoint 通常含 /v1/embeddings
    # 这里只用于 profile 日志, **不影响** HTTP 行为.
    provider = ""
    base_url = endpoint
    m = re.match(r"^(https?://[^/]+)(/.*)?$", endpoint)
    if m:
        base_url = m.group(1)
        # 启发式: 端点 hostname 关键字 → provider 标识 (仅日志)
        host = base_url.lower()
        if "siliconflow" in host:
            provider = "siliconflow"
        elif "openai.com" in host:
            provider = "openai"
        elif "dashscope" in host:
            provider = "dashscope"
        elif "zhipu" in host or "bigmodel" in host:
            provider = "zhipu"
        else:
            provider = "custom"

    pooling, normalization = _derive_pooling_and_normalization(model)
    profile = EmbedProfile(
        provider=provider,
        base_url=base_url,
        endpoint=endpoint,
        model=model,
        dim=dim,
        pooling=pooling,
        normalization=normalization,
        request_format="openai",
    )
    fp = profile.fingerprint()
    return {
        "endpoint": endpoint,
        "dim": dim,
        "model": model,
        "api_key": api_key,
        "apiKey": api_key,  # 驼峰别名, 下游兼容
        "proxy": proxy,
        "_profile": profile,
        "_fingerprint": fp,
        "_raw": cfg,
    }


def get_embed_profile(cfg) -> EmbedProfile:
    """便捷: 从 cfg 拿 EmbedProfile — 失败/disabled 返回空 profile."""
    try:
        d = build_embed_cfg(cfg)
        prof = d.get("_profile")
        return prof if isinstance(prof, EmbedProfile) else EmbedProfile()
    except ValueError:
        # endpoint/model 缺失 — 仍尝试推导 profile (让上层能上报缺什么)
        raw = _extract_raw_embed_dict(cfg)
        if not raw:
            return EmbedProfile()
        endpoint = (raw.get("endpoint") or "").rstrip("/")
        model = raw.get("model") or ""
        prof = EmbedProfile(endpoint=endpoint, model=model)
        return prof
    except Exception:
        return EmbedProfile()


def get_embed_fingerprint(cfg) -> str:
    """便捷: 拿 fingerprint 字符串. 空 = 未配置/失败."""
    try:
        d = build_embed_cfg(cfg)
        return d.get("_fingerprint", "") or ""
    except ValueError:
        return ""
    except Exception:
        return ""


def safe_embed_cfg(cfg) -> dict | None:
    """``build_embed_cfg`` 的 disabled-aware 包装.

    * cfg 为 None / 完全没有 embed 子配置 → 返回 None (disabled)
    * 存在 embed 子配置 → 委托唯一工厂；缺 endpoint/model 或其他非法配置
      的 ``ValueError`` 原样冒泡，禁止把配置错误伪装成 disabled
    * 正常配置 → 返回带 ``_profile`` / ``_fingerprint`` 的工厂结果

    这里不补 model，也不提供 embedding fallback。调用方只有在明确未配置
    embedding 时才得到 None；一旦用户配置了 embedding，就必须配置完整。
    """
    if cfg is None:
        return None
    if not _embed_section_present(cfg):
        return None
    return build_embed_cfg(cfg)


# ─── call_embedding / embed_batch (主调用路径) ───────────────────────


def _validate_for_call(embed_cfg: dict) -> tuple[str, str, str, str, str]:
    """集中校验 — 在 cache 命中前调用, 避免历史 cache 命中遮蔽 400 错误.

    返回 (endpoint, model, api_key, proxy, fingerprint).
    任何缺项 → raise ValueError. fingerprint 为空 → raise (不应走到调用路径).
    """
    endpoint = (embed_cfg.get("endpoint") or "").rstrip("/")
    model = embed_cfg.get("model") or ""
    if not endpoint:
        raise ValueError("embed endpoint 未配置 (config.yaml storage.embed.endpoint)")
    if not model:
        raise ValueError(
            "embed model 未配置 (config.yaml storage.embed.model) — "
            "embedding 无 fallback, 必须显式声明 model"
        )
    api_key = embed_cfg.get("apiKey", "") or embed_cfg.get("api_key", "")
    proxy = (
        embed_cfg.get("proxy", "")
        or os.environ.get("HTTP_PROXY", "")
        or os.environ.get("http_proxy", "")
    )
    fp = embed_cfg.get("_fingerprint") or ""
    if not fp:
        # 缺 fingerprint = 走的是 build_embed_cfg 之外的手拼路径, 立即 fail-closed.
        # 这是**唯一一道防线**, 防止又有人写裸 dict.
        raise ValueError(
            "embed_cfg 缺 _fingerprint — 必须经 build_embed_cfg(cfg) 工厂构造, "
            "禁止手拼 dict"
        )
    return endpoint, model, api_key, proxy, fp


def call_embedding(
    text: str,
    embed_cfg: dict,
    cache: bool = True,
    timeout: float = 3,
    retries: int = 0,
) -> list[float]:
    """调 bge 远程 embedding API (OpenAI 兼容格式).

    timeout: 单次请求超时。prefetch 实时链路保持 3s（8s 预算内）；
             批量导入场景传 10s（代理在持续高频连接下偶发慢，3s 太紧）。
    retries: 失败重试次数（指数退避 1s/2s/4s...）。导入等后台批量传 2；
             实时链路保持 0。

    **阶段1 契约**:
    * **校验发生在 cache 命中前** — 历史已经踩过"cache 命中遮蔽 400"的坑.
    * 失败重试耗尽 → raise last_err, **绝不**返回空向量 / partial 结果.
    """
    import time as _t

    # 1. 校验必须发生在 cache 命中前 — 这是阶段1 的核心修复
    endpoint, model, api_key, proxy, fp = _validate_for_call(embed_cfg)

    if cache:
        now = _t.time()
        cached = _EMBED_CACHE.get(text)
        if cached and (now - cached[0]) < _EMBED_TTL:
            return cached[1]

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {"model": model, "input": text}
    proxies = {"http": proxy, "https": proxy} if proxy else None

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                endpoint, json=body, headers=headers, proxies=proxies, timeout=timeout
            )
            resp.raise_for_status()
            data = resp.json()
            emb = (data.get("data") or [{}])[0].get("embedding", [])
            if not emb:
                raise RuntimeError(f"API 返回空: {json.dumps(data)[:200]}")
            if cache:
                _EMBED_CACHE[text] = (_t.time(), emb)
                if len(_EMBED_CACHE) > 256:
                    oldest = min(_EMBED_CACHE.items(), key=lambda x: x[1][0])
                    _EMBED_CACHE.pop(oldest[0], None)
            return emb
        except requests.HTTPError as e:
            # 403 可能是缺 apiKey
            if e.response is not None and e.response.status_code == 403 and not api_key:
                logger.warning("embedding 403: 需配置 apiKey (config.yaml storage.embed.apiKey)")
            # 400/4xx: 打印服务端响应体（服务端会说明具体原因：长度/格式/参数）+ 请求体长度
            _resp_body = ""
            if e.response is not None:
                try:
                    _resp_body = (e.response.text or "")[:300].replace("\n", " ")
                except Exception:
                    pass
            logger.warning(
                "embedding HTTP %s: url=%s model=%s fp=%s input_len=%d body=%s",
                e.response.status_code if e.response is not None else "?", endpoint, model, fp,
                len(text), _resp_body,
            )
            last_err = e
        except Exception as e:
            last_err = e
        if attempt < retries:
            _t.sleep(2 ** attempt)  # 指数退避 1s/2s/4s
    # 阶段1 契约: **不再返回空向量兜底**, raise 真实错误
    logger.error(
        "embedding 调用失败 (重试%d次后 raise, 不再静默): fp=%s model=%s err=%s",
        retries, fp, model, str(last_err)[:200],
    )
    if last_err is not None:
        raise last_err
    raise RuntimeError("embedding 调用失败 (无 last_err 上下文)")


def embed_batch(texts: list[str], embed_cfg: dict, retries: int = 3) -> list[list[float]]:
    """Batch embedding with retries and exponential backoff.

    **阶段1 契约**:
    * **禁止** zero-vector 兜底: 重试耗尽 raise 真实错误, 调用方按需 catch.
    * **校验发生在去重/cache 命中前**.
    * 返回值仅包含真实算出的向量; 任何条目失败导致整个 batch 失败 → raise.
    """
    import time as _t

    if not texts:
        return []

    # 1. 校验必须在做任何工作前 (含去重 + cache)
    endpoint, model, api_key, proxy, fp = _validate_for_call(embed_cfg)
    dim = embed_cfg.get("dim") or embed_cfg.get("dimension") or 1024
    try:
        dim = int(dim)
    except (TypeError, ValueError):
        dim = 1024
    # No placeholder vector is ever allocated; incomplete responses raise below.

    # Deduplicate (按前 500 字符 key)
    seen: dict[str, int] = {}
    unique: list[str] = []
    idx_map: list[int] = []
    for t in texts:
        key = t[:500]
        if key not in seen:
            seen[key] = len(unique)
            unique.append(t)
        idx_map.append(seen[key])

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {"model": model, "input": unique}
    proxies = {"http": proxy, "https": proxy} if proxy else None

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                endpoint, json=body, headers=headers, proxies=proxies, timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data") or []
            # Build result dict by index
            emb_by_idx: dict[int, list[float]] = {}
            for item in items:
                idx = item.get("index", 0)
                emb = item.get("embedding", [])
                if emb:
                    emb_by_idx[idx] = emb
            # 阶段1: items 数 != unique 数 = 不完整响应, 不接受 partial zero
            if len(emb_by_idx) != len(unique):
                missing = [
                    i for i in range(len(unique)) if i not in emb_by_idx
                ]
                logger.warning(
                    "embed_batch 返回不完整: got=%d want=%d missing_idx=%s model=%s fp=%s",
                    len(emb_by_idx), len(unique), missing[:10], model, fp,
                )
                # 不返回 partial, 直接 raise 让上层决策
                raise RuntimeError(
                    f"embed_batch 返回不完整: got {len(emb_by_idx)}/{len(unique)} "
                    f"model={model} fp={fp}"
                )
            # Map back to original order
            return [emb_by_idx[idx] for idx in idx_map]
        except requests.HTTPError as e:
            _resp_body = ""
            if e.response is not None:
                try:
                    _resp_body = (e.response.text or "")[:300].replace("\n", " ")
                except Exception:
                    pass
            logger.warning(
                "embed_batch HTTP %s: url=%s model=%s fp=%s texts=%d body=%s",
                e.response.status_code if e.response is not None else "?",
                endpoint, model, fp, len(unique), _resp_body,
            )
            last_err = e
            if attempt < retries - 1:
                wait = 0.5 * (2 ** attempt)
                logger.debug(
                    "embed_batch attempt %d failed, retry in %.1fs: %s",
                    attempt + 1, wait, _safe_err(e)[:100],
                )
                _t.sleep(wait)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait = 0.5 * (2 ** attempt)
                logger.debug(
                    "embed_batch attempt %d failed, retry in %.1fs: %s",
                    attempt + 1, wait, _safe_err(e)[:100],
                )
                _t.sleep(wait)
    # 阶段1 契约: **禁止** zero-vector 兜底. raise 真实错误.
    logger.error(
        "embed_batch 重试%d次后失败 raise (不再返回 zero): fp=%s model=%s",
        retries, fp, model,
    )
    if last_err is not None:
        raise last_err
    raise RuntimeError(
        f"embed_batch 重试{retries}次后失败 fp={fp} model={model}"
    )