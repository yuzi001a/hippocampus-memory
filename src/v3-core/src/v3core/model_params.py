# -*- coding: utf-8 -*-
"""模型参数形状表 — 配置管"值", 这里管"形状"。

问题
----
配置层过去只覆盖"值"(模型名 / base_url / key / timeout / 输出上限), 而"形状"——
这个模型收不收 ``thinking``、收不收 ``reasoning_effort``、默认档多少——写死在
调用点里, 用模型名子串判断::

    requires_adaptive = "m3.1" in model.lower()      # llm.py 与 observer.py 各一份

后果: 厂商一改接口契约 (M3 → M3.1 要求 adaptive thinking) 就只能改代码; 两份拷贝
漏改一处就是"某条路径静默用错参数"(Y400 那次 400 故障就是这个形状)。

现在的规则
----------
1. 内置表覆盖已知模型; 用户在 ``config.yaml`` 的 ``models:`` 段按**前缀**覆盖/新增::

       models:
         "MiniMax-M3.1": { thinking: adaptive, reasoning_effort: low }
         "my-local-model": { thinking: classic }

2. 最长前缀匹配, 大小写不敏感。带 vendor 命名空间的 id (``MiniMax/MiniMax-M3.1``、
   ``minimaxai/minimax-m3.1``) 会先去掉命名空间再匹配, 与旧实现 ``"m3.1" in model``
   的子串语义保持一致 —— 收窄成纯前缀会把这类 id 静默翻成 classic, 每次调用先吃
   一个 400 再重试。
3. 表里没有的模型 → 按**历史行为** (classic, 显式发 ``thinking={type:disabled}``)
   调用 + 每个模型一次 WARNING。刻意选"可能 400"而不是"悄悄省略 thinking":
   400 立刻可见、指向配置; 省略 thinking 会退化成 adaptive 思考, 表现为"变慢 /
   思考吃满输出预算", 那正是最难诊断的故障形态。也不静默回落模型名/端点的字面量。
4. ``reasoning_effort`` 三态: 不写 = 用调用方给的默认; 写档位 = 固定该档;
   写 ``null`` 或 ``omit`` = 显式不发该字段 (想用 provider 默认档时用这个)。
5. 这里是**唯一**知道模型参数差异的地方; ``llm.py`` / ``observer.py`` 只查表拼
   payload, 不再各存一份模型名判断。

职责边界: 表只描述"形状 + 该族的默认 effort"。是否真的下发 effort, 由调用方决定 ——
``observer`` 用表 (16k 输出预算, 需要压思考); ``llm.py`` 只认显式配置
(E1 / 抽取走 131k 预算, 没有质量对照就不该被表静默统一降档)。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger("v3core.model_params")

# ── 形状常量 ────────────────────────────────────────────────────────────────
THINKING_ADAPTIVE = "adaptive"  # 服务端默认 adaptive: 不能发 thinking=disabled, 用 effort 收口
THINKING_CLASSIC = "classic"    # 可显式发 thinking={type: disabled|enabled}

#: 显式"不发 effort"的写法 (表里写 null 等价)。
EFFORT_OMIT = "omit"

#: 已知模型的参数形状。键是**前缀**(小写), 最长匹配优先。
DEFAULT_MODEL_TABLE: dict[str, dict[str, Any]] = {
    # M3.1 家族: 拒绝 thinking=disabled (HTTP 400), 默认 adaptive; 不压 effort 会让
    # 长 prompt 的思考吃满输出预算 (实测 30 条 QA 批次 reasoning 56,547 字符, JSON 未闭合)。
    "minimax-m3.1": {"thinking": THINKING_ADAPTIVE, "reasoning_effort": "low"},
    # 裸 id 别名: 有的网关会把 vendor 前缀去掉, 或者模型名就写成 "M3.1"
    "m3.1": {"thinking": THINKING_ADAPTIVE, "reasoning_effort": "low"},
    # M3 / 更早: 只有 thinking 开关
    "minimax-m3": {"thinking": THINKING_CLASSIC},
    "minimax-m2": {"thinking": THINKING_CLASSIC},
}

#: 未声明模型: 保持历史行为 (classic), 由 WARNING 逼你显式声明。
#: 不选"省略 thinking"——那会把未知模型悄悄推到 adaptive, 症状是变慢/思考膨胀。
UNKNOWN_MODEL_PARAMS: dict[str, Any] = {
    "thinking": THINKING_CLASSIC,
    "reasoning_effort": None,
}

_KNOWN_SPEC_KEYS = frozenset({"thinking", "reasoning_effort"})

_WARNED: set[str] = set()
_WARN_LOCK = threading.Lock()


@dataclass(frozen=True)
class ModelParams:
    """一个模型解析后的参数形状。"""

    thinking: str                 # THINKING_ADAPTIVE | THINKING_CLASSIC
    reasoning_effort: str | None  # 表声明的档位; None = 没写或显式关闭
    effort_declared: bool         # 表里是否**显式**写了 reasoning_effort 键
    declared: bool                # 是否命中 models: 表 (内置或用户声明)
    matched: str                  # 命中的前缀; 未命中为 ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
            "effort_declared": self.effort_declared,
            "declared": self.declared,
            "matched": self.matched,
        }


def _as_models_mapping(cfg: Any) -> Mapping[str, Any]:
    """从 V3Config / legacy dict / None 里取出 ``models`` 段 (非 mapping 视为空)。"""
    if cfg is None:
        return {}
    if isinstance(cfg, Mapping):
        raw = cfg.get("models")
    else:
        raw = getattr(cfg, "models", None)
    return raw if isinstance(raw, Mapping) else {}


def _warn_bad_spec(key: str, unknown: set[str]) -> None:
    label = f"spec:{key}"
    with _WARN_LOCK:
        if label in _WARNED:
            return
        _WARNED.add(label)
    logger.warning(
        "model_params: models: 段里 %r 含无法识别的字段 %s (只认 thinking / "
        "reasoning_effort), 这些字段已忽略。",
        key, sorted(unknown),
    )


def model_table(cfg: Any = None) -> dict[str, dict[str, Any]]:
    """内置表 + 用户 ``models:`` 覆盖 (键与字段名统一小写/去空格)。"""
    table: dict[str, dict[str, Any]] = {
        str(k).strip().lower(): {str(sk).strip().lower(): sv for sk, sv in v.items()}
        for k, v in DEFAULT_MODEL_TABLE.items()
    }
    for key, spec in _as_models_mapping(cfg).items():
        if not isinstance(spec, Mapping):
            continue
        normalized_key = str(key).strip().lower()
        entry = table.setdefault(normalized_key, {})
        fields = {str(k).strip().lower(): v for k, v in spec.items()}
        unknown = set(fields) - _KNOWN_SPEC_KEYS
        if unknown:
            _warn_bad_spec(normalized_key, unknown)
        entry.update({k: v for k, v in fields.items() if k in _KNOWN_SPEC_KEYS})
    return table


def _candidates(key: str) -> list[str]:
    """匹配用的候选串: 原样, 以及去掉 vendor 命名空间后的末段。

    旧实现是子串匹配 (``"m3.1" in model``), 所以 ``MiniMax/MiniMax-M3.1-Flash`` 和
    ``minimaxai/minimax-m3.1`` 都算 M3.1。收窄成纯前缀会把这类 id 静默翻成 classic,
    每次调用先吃一个 400 再重试 —— 这里保留旧语义。
    """
    out = [key]
    for sep in ("/", ":"):
        if sep in key:
            tail = key.rsplit(sep, 1)[-1].strip()
            if tail and tail not in out:
                out.append(tail)
    return out


def resolve(model: str, cfg: Any = None, warn: bool = True) -> ModelParams:
    """按最长前缀解析模型参数形状; 未声明 → 历史形状 + 一次性 WARNING。

    ``warn=False`` 是纯查询路径 (validate_config / doctor / 状态展示用): 不写日志,
    也不消耗那一次 WARNING 配额。
    """
    key = str(model or "").strip().lower()
    table = model_table(cfg)
    best_prefix = ""
    for candidate in _candidates(key):
        for prefix in table:
            if prefix and candidate.startswith(prefix) and len(prefix) > len(best_prefix):
                best_prefix = prefix
        if best_prefix:
            break
    if best_prefix:
        spec = table[best_prefix]
        thinking = str(spec.get("thinking") or THINKING_CLASSIC).strip().lower()
        if thinking not in (THINKING_ADAPTIVE, THINKING_CLASSIC):
            thinking = THINKING_CLASSIC
        declared_effort = "reasoning_effort" in spec
        effort = spec.get("reasoning_effort")
        effort_str = str(effort).strip().lower() if effort else None
        if effort_str == EFFORT_OMIT:
            effort_str = None
        return ModelParams(
            thinking=thinking,
            reasoning_effort=effort_str,
            effort_declared=declared_effort,
            declared=True,
            matched=best_prefix,
        )

    if warn:
        _warn_unknown(key)
    return ModelParams(
        thinking=str(UNKNOWN_MODEL_PARAMS["thinking"]),
        reasoning_effort=UNKNOWN_MODEL_PARAMS["reasoning_effort"],
        effort_declared=False,
        declared=False,
        matched="",
    )


def _warn_unknown(key: str) -> None:
    label = key or "(空模型名)"
    with _WARN_LOCK:
        if label in _WARNED:
            return
        _WARNED.add(label)
    logger.warning(
        "model_params: 模型 %r 未在 models: 表声明参数形状 — 按历史 classic 形状调用 "
        "(发 thinking={type:disabled}, 不发 reasoning_effort); 若该模型要求 adaptive "
        "会得到 400 'requires adaptive thinking', 请在此声明 "
        "{thinking: adaptive, reasoning_effort: low}。",
        label,
    )


def thinking_field(params: ModelParams, mode: str) -> dict[str, Any] | None:
    """把形状 + 意图 (auto/enabled/disabled) 折成 payload 里的 ``thinking`` 值。

    返回 ``None`` 表示**省略该字段**(adaptive 族在 disabled/auto 下不能发 disabled)。
    """
    normalized = str(mode or "auto").strip().lower()
    if normalized not in ("auto", "enabled", "disabled"):
        normalized = "auto"
    if params.thinking == THINKING_ADAPTIVE:
        return {"type": "enabled"} if normalized == "enabled" else None
    return {"type": "disabled"} if normalized in ("auto", "disabled") else {"type": "enabled"}


def explicit_reasoning_effort(cfg: Any) -> str | None:
    """只读**调用方显式配置**的 effort (不看表)。

    ``llm.py`` 用这个: E1 / 抽取走 131k 输出预算, 没有质量对照就不该被表静默降档。
    """
    if not isinstance(cfg, Mapping):
        return None
    value = cfg.get("reasoning_effort")
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in (EFFORT_OMIT, "none", "null", "off"):
        return None
    return text or None


def reasoning_effort_for(params: ModelParams, cfg: Any = None, default: str | None = None) -> str | None:
    """adaptive 路径该带的 effort: 显式配置 > 表声明 > ``default``。

    非 adaptive 族一律返回 ``None`` —— 那些模型用 ``thinking`` 开关, 多发一个
    ``reasoning_effort`` 只可能被当成未知参数拒绝。表里显式写 ``null`` / ``omit``
    表示"就是不发", 这时不再落到 ``default``。
    """
    if params.thinking != THINKING_ADAPTIVE:
        return None
    explicit = explicit_reasoning_effort(cfg)
    if explicit:
        return explicit
    if params.effort_declared:
        return params.reasoning_effort  # 可能是 None = 显式关闭
    return default


def warnings_for(cfg: Any, *models: str) -> list[str]:
    """给 ``validate_config`` / doctor 用: 列出未声明参数形状的模型 (纯查询)。"""
    out: list[str] = []
    for model in models:
        if not str(model or "").strip():
            continue
        if not resolve(model, cfg, warn=False).declared:
            out.append(
                f"模型 '{model}' 未在 models: 声明参数形状 (thinking/reasoning_effort)，"
                "将按历史 classic 形状调用"
            )
    return out


__all__ = [
    "DEFAULT_MODEL_TABLE",
    "EFFORT_OMIT",
    "THINKING_ADAPTIVE",
    "THINKING_CLASSIC",
    "UNKNOWN_MODEL_PARAMS",
    "ModelParams",
    "explicit_reasoning_effort",
    "model_table",
    "reasoning_effort_for",
    "resolve",
    "thinking_field",
    "warnings_for",
]
