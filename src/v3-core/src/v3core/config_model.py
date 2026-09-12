"""v3-core typed config model (P3).

Dataclass hierarchy with per-component None-able optionality. No external
dependency — stdlib ``dataclasses`` only.

Migration notes:
  - Old: ``cfg.get("storage", {}).get("embed", {}).get("endpoint")``
  - New: ``cfg.embed.endpoint if cfg.embed else ""``

``resolve_config()`` (in config.py) returns ``V3Config`` instances. Sub-dicts
(passed to ``embedding.py`` / ``rerank.py`` helpers) still expect dicts — use
``cfg.embed.to_legacy_dict()`` or ``cfg._embed_legacy()`` to bridge when
needed.

The ``half_life`` field controls the time-decay weighting in ``recall_pool``.
"""
from __future__ import annotations
import copy
from dataclasses import dataclass, field, fields, asdict
from typing import Any


# ── Component configs ────────────────────────────────────────────────


@dataclass
class PGConfig:
    """Postgres + pgvector storage backend. None = 文件模式."""
    host: str = "localhost"
    port: int = 5433
    database: str = "v3embeddings"
    user: str = "v3user"
    password: str = ""
    # Pool limits are part of the effective PG runtime configuration.  Keep
    # them in the profile so the RuntimeRegistry fingerprint covers them and
    # the provider never has to invent connection limits at runtime.
    pool_max_connections: int = 4
    pool_min_connections: int = 0

    def to_legacy_dict(self) -> dict:
        d = asdict(self)
        return d

    def __repr__(self):
        return f"PGConfig(host={self.host!r}, port={self.port}, database={self.database!r}, user={self.user!r}, password='***')"


@dataclass
class EmbedConfig:
    """Embedding API 客户端配置. ``endpoint == ''`` 表示未配置向量召回."""
    endpoint: str = ""
    dim: int = 1024
    api_key: str = ""  # canonical; YAML 既支持 api_key 也支持 apiKey
    proxy: str = ""  # 例如 http://127.0.0.1:10808
    model: str = ""  # 默认空: 必须显式配置, 禁止隐式 fallback 模型

    @property
    def configured(self) -> bool:
        return bool(self.endpoint)

    def to_legacy_dict(self) -> dict:
        """输出历史上 ``storage.embed`` 字典的形状, 包含 apiKey 驼峰别名,
        让 embedding.py / pg_store.py 这些沿用 dict 形态的下游无需修改.
        返回的是新 dict, 不修改原 dataclass 状态."""
        d = asdict(self)
        api_key = d.pop("api_key", "")
        d["apiKey"] = api_key
        d["api_key"] = api_key  # 双保留 — 历史代码两种拼写都用
        return d

    def __repr__(self):
        return f"EmbedConfig(endpoint={self.endpoint!r}, dim={self.dim}, api_key='***')"


@dataclass
class RerankConfig:
    """Rerank API 配置. ``endpoint == ''`` 表示不启用 rerank."""
    endpoint: str = ""
    proxy: str = ""
    timeout: int = 30
    api_key: str = ""
    model: str = ""
    # 2026-09-02 P1.3.1 resilience: 当 prefetch deadline 剩余预算 < 该值时,
    # ``recall_pool._rerank`` 走受控的 RRF 回退 (skip + log), 不发起 remote HTTP.
    # 缺省 1.5s — 与 P1.2-A1 internal 6.5s budget 与外部 8.0s join timeout 的
    # 间隙一致; 不存在 / 非正数 / 类型异常均回落 1.5.
    min_remaining_s: float = 1.5

    @property
    def configured(self) -> bool:
        return bool(self.endpoint)

    def to_legacy_dict(self) -> dict:
        d = asdict(self)
        api_key = d.pop("api_key", "")
        d["apiKey"] = api_key
        d["api_key"] = api_key
        return d

    def __repr__(self):
        return f"RerankConfig(endpoint={self.endpoint!r}, api_key='***')"


@dataclass
class LLMConfig:
    """LLM provider config — 空默认，支持任意 OpenAI 兼容端点."""
    provider: str = ""  # 空 = 不显式配置就不发数据（运行时由 config.yaml 覆盖）
    model: str = ""  # 空 = 不显式配置就不发数据（运行时由 config.yaml 覆盖）
    api_key: str = ""
    base_url: str = ""  # 自定义端点；空 = 按 provider 用官方默认

    def __repr__(self):
        return f"LLMConfig(provider={self.provider!r}, model={self.model!r}, api_key='***')"


@dataclass
class E1Config:
    """E1 每日印合成 (印 = yin, identity-layer synthesis)."""
    enabled: bool = True
    # Phase 2 confidence gating: cards with confidence < this threshold are
    # filtered out of yin synthesis. Default 0.3 — single-extraction cards
    # land here and need dreamer verification to be promoted.
    confidence_threshold: float = 0.3


@dataclass
class TKGConfig:
    """TKG 时序知识图谱配置."""
    enabled: bool = False
    conflict_threshold: float = 0.85


@dataclass
class PrefetchConfig:
    """prefetch 召回策略配置."""
    enabled: bool = True
    default_limit: int = 5
    dual_path: bool = True
    rrf_k: int = 60
    include_message_vector: bool = True
    # 2026-09-03 P1.3.1: 长 term 时启用 QA snapshot (in-Python 子串匹配) 的阈值.
    # 5-term boundary / term_frequency tail: 默认 5 — 实证默认从 7 下调到 5, 以更早
    # 触发 snapshot 路径 (相同 5435 snapshot 上 integrated 200 eval 唯一 5-term 冷跑
    # 仍有 ~6502ms 拖尾; 进程内 threshold=5 前 12 eval=24 calls、max 4072ms、0 deadline,
    # counts unchanged, 见设计文档 2026-09-03 迭代证据段). 中/短 term 仍走
    # combined/parallel PG ILIKE 路径, 行为不变; 设 0 / 负值 = 禁用 snapshot (回落到
    # combined/parallel + A2 worker path, 不静默改 rerank/topic/A2 行为); 显式 1–4
    # 仍走 snapshot 但等于「低于默认」, 显式 32 仅作对照.
    qa_snapshot_min_terms: int = 5


@dataclass
class IngestConfig:
    """ingest 引擎配置 — LiveBuffer + cron."""
    cron: str = "0 2 * * *"
    live_buffer_batch: int = 8
    live_buffer_flush_sec: float = 30.0


# ── 兼容性映射 ──
_LEGACY_KEY_MAP = {
    "basePath": "base_path",
    "base_path": "base_path",
}


def _key_to_attr(key: str) -> str:
    return _LEGACY_KEY_MAP.get(key, key)


# ── Top-level V3Config ────────────────────────────────────────────────


@dataclass
class V3Config:
    """v3-core 顶层配置 (Typed).

    每个 sub-component 都可以为 None (用户禁用 / 未配置), 也有自己的状态报告
    (``format_status``).

    新增: ``half_life`` 字段控制 RRF 时间衰减半衰期 (P3).
    """
    def get(self, key: str, default=None):
        """向后兼容: 旧脚本用 cfg.get('key') 调 V3Config"""
        if key == "basePath":
            return self.base_path or default
        if key == "storage":
            return self._legacy_storage()
        if key == "llm":
            if self.llm:
                return {"api_key": self.llm.api_key}
            return {}
        if key == "tkg":
            if self.tkg:
                return {"conflict_threshold": self.tkg.conflict_threshold}
            return {}
        # 普通属性
        attr = _key_to_attr(key)
        return getattr(self, attr, default) if hasattr(self, attr) else default

    def __repr__(self):
        return (f"V3Config(mode={self.mode!r}, "
                f"llm={repr(self.llm) if self.llm else None}, "
                f"embed={repr(self.embed) if self.embed else None}, "
                f"rerank={repr(self.rerank) if self.rerank else None})")

    def _legacy_storage(self) -> dict:
        """返回旧格式的 storage dict — 给旧 cron 脚本用"""
        d: dict = {}
        if self.pg:
            d["pg"] = {
                "host": self.pg.host,
                "port": self.pg.port,
                "database": self.pg.database,
                "user": self.pg.user,
                "password": self.pg.password,
            }
        if self.embed and self.embed.endpoint:
            d["embed"] = {
                "endpoint": self.embed.endpoint,
                "dim": self.embed.dim,
                "proxy": self.embed.proxy or "",
            }
        return d

    mode: str = "cloud"  # "cloud" | "local"
    base_path: str = ""
    llm: LLMConfig = field(default_factory=LLMConfig)
    pg: PGConfig | None = field(default_factory=PGConfig)  # None = 文件模式
    embed: EmbedConfig | None = None  # None = 不使用向量
    rerank: RerankConfig | None = None  # None = 不 rerank
    e1: E1Config = field(default_factory=E1Config)
    tkg: TKGConfig = field(default_factory=TKGConfig)
    prefetch: PrefetchConfig = field(default_factory=PrefetchConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    prompts: dict[str, str] = field(default_factory=dict)
    # 兼容老字段 cfg.get("prompts", {}).get("extract") — 仍可单值配置到 prompts_extract
    prompts_extract: str = ""
    half_life: int = 30  # P3 — 时间衰减半衰期 (recall_pool)

    # -- Backward-compat: legacy dict access -------------------------------
    def to_legacy_dict(self) -> dict:
        """给仍然期望 raw dict 的下游一个 escape hatch. 不推荐新代码使用."""
        out: dict[str, Any] = {
            "mode": self.mode,
            "basePath": self.base_path,
            "llm": asdict(self.llm),
            "e1": asdict(self.e1),
            "tkg": asdict(self.tkg),
            "prefetch": asdict(self.prefetch),
            "ingest": {
                "cron": self.ingest.cron,
                "live_buffer": {
                    "batch": self.ingest.live_buffer_batch,
                    "flush_sec": self.ingest.live_buffer_flush_sec,
                },
            },
            # 优先 dumps 整个 prompts dict; 若为空但有 legacy 字段, 透传
            "prompts": {**(self.prompts or {}), **(
                {"extract": self.prompts_extract} if self.prompts_extract and "extract" not in (self.prompts or {})
                else {}
            )},
            "half_life": self.half_life,
            "storage": {
                "pg": self.pg.to_legacy_dict() if self.pg else {},
                "embed": self.embed.to_legacy_dict() if self.embed else {},
                "rerank": self.rerank.to_legacy_dict() if self.rerank else {},
            },
        }
        return out

    def _embed_legacy(self) -> dict:
        return self.embed.to_legacy_dict() if self.embed else {}

    def _rerank_legacy(self) -> dict:
        return self.rerank.to_legacy_dict() if self.rerank else {}

    def _ingest_legacy(self) -> dict:
        return {
            "cron": self.ingest.cron,
            "live_buffer": {
                "batch": self.ingest.live_buffer_batch,
                "flush_sec": self.ingest.live_buffer_flush_sec,
            },
        }


# ── Migration: legacy dict -> V3Config ───────────────────────────────


def _coerce_int(v: Any, default: int) -> int:
    """yaml 加载到的可能是字符串, 转 int 时保护."""
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _coerce_float(v: Any, default: float) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _coerce_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1", "on"):
            return True
        if s in ("false", "no", "0", "off"):
            return False
    return default


def from_legacy_dict(src: dict | None) -> V3Config:
    """从历史 raw dict (config.yaml + defaults merged) 构造 V3Config.

    容错设计: dict 缺字段 / 类型异常都走默认值, 不抛错. 已知 bug 模式下
    ``validate_config()`` 会给出警告.
    """
    src = src or {}

    storage = src.get("storage") or {}
    pg_raw = storage.get("pg") or {}
    embed_raw = storage.get("embed") or {}
    rerank_raw = storage.get("rerank") or {}

    llm_raw = src.get("llm") or {}
    e1_raw = src.get("e1") or {}
    tkg_raw = src.get("tkg") or {}
    prefetch_raw = src.get("prefetch") or {}
    ingest_raw = src.get("ingest") or {}
    prompts = src.get("prompts") or {}

    # -- LLM --
    llm = LLMConfig(
        provider=str(llm_raw.get("provider", "")),
        model=str(llm_raw.get("model", "")),
        api_key=str(llm_raw.get("api_key", "") or llm_raw.get("apiKey", "")),
        base_url=str(llm_raw.get("base_url", "") or llm_raw.get("baseUrl", "") or ""),
    )

    # -- PG --
    if pg_raw:
        pool_raw = pg_raw.get("pool") or {}
        pool_max = pg_raw.get(
            "pool_max_connections",
            pg_raw.get("max_connections", pool_raw.get("max_connections")),
        )
        pool_min = pg_raw.get(
            "pool_min_connections",
            pg_raw.get("min_connections", pool_raw.get("min_connections")),
        )
        pg = PGConfig(
            host=str(pg_raw.get("host", "localhost")),
            port=_coerce_int(pg_raw.get("port"), 5433),
            database=str(pg_raw.get("database", "v3core")),
            user=str(pg_raw.get("user", "v3user")),
            password=str(pg_raw.get("password", "")),
            pool_max_connections=_coerce_int(pool_max, 4),
            pool_min_connections=_coerce_int(pool_min, 0),
        )
    else:
        pg = None

    # -- Embed --
    if embed_raw:
        embed = EmbedConfig(
            endpoint=str(embed_raw.get("endpoint", "") or ""),
            dim=_coerce_int(embed_raw.get("dim"), 1024),
            api_key=str(embed_raw.get("api_key", "") or embed_raw.get("apiKey", "")),
            proxy=str(embed_raw.get("proxy", "") or ""),
            model=str(embed_raw.get("model", "") or ""),
        )
    else:
        embed = None

    # -- Rerank --
    if rerank_raw:
        rerank = RerankConfig(
            endpoint=str(rerank_raw.get("endpoint", "") or ""),
            proxy=str(rerank_raw.get("proxy", "") or ""),
            timeout=_coerce_int(rerank_raw.get("timeout"), 30),
            api_key=str(rerank_raw.get("api_key", "") or rerank_raw.get("apiKey", "")),
            model=str(rerank_raw.get("model", "") or ""),
            # 2026-09-02 P1.3.1: 缺默 1.5; 字段缺失 / 非正数 / 类型异常回落 1.5.
            min_remaining_s=_coerce_float(rerank_raw.get("min_remaining_s"), 1.5),
        )
    else:
        rerank = None
    # -- Component bool/int flags --
    e1 = E1Config(
        enabled=_coerce_bool(e1_raw.get("enabled"), True),
        confidence_threshold=_coerce_float(e1_raw.get("confidence_threshold"), 0.3),
    )
    tkg = TKGConfig(
        enabled=_coerce_bool(tkg_raw.get("enabled"), False),
        conflict_threshold=_coerce_float(tkg_raw.get("conflict_threshold"), 0.85),
    )
    prefetch = PrefetchConfig(
        enabled=_coerce_bool(prefetch_raw.get("enabled"), True),
        default_limit=_coerce_int(prefetch_raw.get("default_limit"), 5),
        dual_path=_coerce_bool(prefetch_raw.get("dual_path"), True),
        rrf_k=_coerce_int(prefetch_raw.get("rrf_k"), 60),
        include_message_vector=_coerce_bool(prefetch_raw.get("include_message_vector"), True),
        # 2026-09-03 P1.3.1: 字段缺失 / 类型异常回落 5 (term_frequency tail; 5-term boundary).
        qa_snapshot_min_terms=_coerce_int(prefetch_raw.get("qa_snapshot_min_terms"), 5),
    )

    # -- Ingest --
    live_buf = ingest_raw.get("live_buffer") or {}
    ingest = IngestConfig(
        cron=str(ingest_raw.get("cron", "0 2 * * *") or "0 2 * * *"),
        live_buffer_batch=_coerce_int(live_buf.get("batch"), 8),
        live_buffer_flush_sec=_coerce_float(live_buf.get("flush_sec"), 30.0),
    )

    # -- Half life (P3) --
    # 既支持 cfg.get("half_life") 也支持 cfg.get("decay", {}).get("half_life_days")
    half_life = _coerce_int(src.get("half_life"), 30)
    decay_block = src.get("decay") or {}
    if half_life == 30 and decay_block:
        # 如果显式给了 decay 块, 用它
        half_life = _coerce_int(decay_block.get("half_life_days"), half_life)

    return V3Config(
        mode=str(src.get("mode", "cloud") or "cloud"),
        base_path=str(src.get("basePath", "") or src.get("base_path", "") or ""),
        llm=llm,
        pg=pg,
        embed=embed,
        rerank=rerank,
        e1=e1,
        tkg=tkg,
        prefetch=prefetch,
        ingest=ingest,
        # 整个 prompts dict 入新版字段
        prompts={k: str(v) for k, v in prompts.items() if isinstance(k, str) and v is not None}
                if isinstance(prompts, dict) else {},
        # 兼容老字段: extract 单值同时保留在 prompts_extract
        prompts_extract=str(prompts.get("extract", "") or ""),
        half_life=half_life if half_life > 0 else 30,
    )


# ── Validation & status reporting ─────────────────────────────────────


def validate_config(cfg: V3Config) -> list[str]:
    """对 V3Config 做组件级校验, 返回人类可读的 warnings (不抛异常).

    这些警告适合注入到 status prompt, 让运维一眼看到系统缺口.
    """
    warnings: list[str] = []

    if cfg.mode not in ("cloud", "local"):
        warnings.append(f"未知 mode '{cfg.mode}'，默认 cloud")

    if cfg.pg and not cfg.pg.password:
        warnings.append("PG 密码为空，生产环境建议配置")

    if cfg.embed and cfg.embed.endpoint:
        if cfg.embed.proxy:
            warnings.append(f"Embed 走代理 {cfg.embed.proxy}")
        if not cfg.embed.api_key:
            warnings.append("Embed endpoint 已配置但 api_key 缺失，可能 403")
    elif cfg.embed is not None:
        # 显式给 embed 但 endpoint 为空 — 不会被用到, 提示
        warnings.append("Embed 已实例化但 endpoint 为空，无向量召回")

    if cfg.rerank and cfg.rerank.endpoint:
        if cfg.rerank.proxy:
            warnings.append(f"Rerank 走代理 {cfg.rerank.proxy}")

    if not cfg.embed or not cfg.embed.endpoint:
        warnings.append("Embed 未配置，向量搜索/召回依赖文件模式")

    if cfg.half_life <= 0:
        warnings.append("half_life 应为正数，使用默认 30")

    # 2026-09-03 P1.3.1: qa_snapshot_min_terms 应为非负整数; 负值会被回落到默认 5
    # (5-term boundary / term_frequency tail).
    if getattr(cfg.prefetch, "qa_snapshot_min_terms", 5) < 0:
        warnings.append("prefetch.qa_snapshot_min_terms 应为非负整数，使用默认 5")

    if cfg.pg and cfg.pg.port and not (1 <= cfg.pg.port <= 65535):
        warnings.append(f"PG 端口异常: {cfg.pg.port}")

    return warnings


def format_status(cfg: V3Config, scheduler_status: str = "", compression_status: str = "") -> str:
    """组件状态报告 — 人类可读, 每项标出 ✅ / ⏸ / ⚠️.

    输出 ~20 行以内, 可注入 system prompt.
    """
    lines: list[str] = []
    lines.append("v3 记忆系统 — 组件状态")
    lines.append("")

    # LLM
    api = "API key 已配置" if cfg.llm.api_key else "⚠️ 无 API key"
    lines.append(f"  LLM:        ✅ {cfg.llm.provider}/{cfg.llm.model} ({api})")

    # PG
    if cfg.pg:
        lines.append(
            f"  PG 存储:    ✅ {cfg.pg.host}:{cfg.pg.port}/{cfg.pg.database} (user={cfg.pg.user})"
        )
    else:
        lines.append(f"  PG 存储:    ⏸ 未配置，使用文件模式")

    # Embed
    if cfg.embed and cfg.embed.endpoint:
        proxy_note = f" (代理 {cfg.embed.proxy})" if cfg.embed.proxy else ""
        dim_note = f" dim={cfg.embed.dim}"
        lines.append(f"  Embed:      ✅ {cfg.embed.endpoint}{dim_note}{proxy_note}")
    else:
        lines.append(f"  Embed:      ⏸ 未配置，无向量召回")

    # Rerank
    if cfg.rerank and cfg.rerank.endpoint:
        proxy_note = f" (代理 {cfg.rerank.proxy})" if cfg.rerank.proxy else ""
        lines.append(f"  Rerank:     ✅ {cfg.rerank.endpoint}{proxy_note}")
    else:
        lines.append(f"  Rerank:     ⏸ 未配置，跳过语义排序")

    # E1
    e1_line = scheduler_status if scheduler_status else (
        f"  E1 印合成:  ✅ 内部调度 (period={cfg.e1.period_h or 24}h)"
        if cfg.e1.enabled else "  E1 印合成:  ⏸ 已禁用"
    )
    lines.append(e1_line)

    # Compression daemon (可选 — 只有 V3Core 拉起 daemon 时才有 status)
    if compression_status:
        lines.append(compression_status)

    # TKG
    if cfg.tkg.enabled:
        lines.append(
            f"  TKG 事实图: ✅ 已启用 (conflict_threshold={cfg.tkg.conflict_threshold})"
        )
    else:
        lines.append(f"  TKG 事实图: ⏸ 已禁用")

    # Prefetch
    if cfg.prefetch.enabled:
        paths = ["A链/B链双路径"] if cfg.prefetch.dual_path else ["单路径"]
        if cfg.prefetch.include_message_vector:
            paths.append("含消息向量")
        lines.append(f"  Prefetch:   ✅ {' / '.join(paths)} (limit={cfg.prefetch.default_limit})")
    else:
        lines.append(f"  Prefetch:   ⏸ 已禁用")

    # 时间衰减
    lines.append(f"  时间衰减:    ✅ exp(-age_days/{cfg.half_life})")

    # 模式
    lines.append(f"  模式:        {cfg.mode}")

    # 降级警告
    warns = validate_config(cfg)
    if warns:
        lines.append("")
        lines.append("  ⚠️ 注意：")
        for w in warns:
            lines.append(f"    • {w}")

    return "\n".join(lines)


__all__ = [
    "PGConfig",
    "EmbedConfig",
    "RerankConfig",
    "LLMConfig",
    "E1Config",
    "TKGConfig",
    "PrefetchConfig",
    "IngestConfig",
    "V3Config",
    "from_legacy_dict",
    "validate_config",
    "format_status",
]
