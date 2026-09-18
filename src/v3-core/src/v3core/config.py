"""v3-core config loader — P3 typed config.

Returns :class:`V3Config` (typed dataclass) rather than raw dict.

Quick API:
  - ``resolve_config(profile="default")`` → ``V3Config`` (legacy: also returns
    raw dict if ``return_legacy=True`` — but the recommendation is to migrate
    call sites to attribute access).
  - ``format_status(cfg)``  → component status report (see config_model.py).
  - ``validate_config(cfg)`` → list[str] warnings (see config_model.py).
"""
from __future__ import annotations
import os
import logging
import re
import sys
from pathlib import Path
from typing import Any

from .config_model import V3Config, format_status, validate_config, from_legacy_dict

__all__ = [
    "V3Config",
    "resolve_config",
    "format_status",
    "validate_config",
    "from_legacy_dict",
    "_resolve_data_dir",
]


def _resolve_data_dir(config=None) -> Path:
    """解析 v3-core 数据根目录 — config.base_path 优先, 兜底 ~/.v3-core/profiles/default.

    Accepts V3Config / dict / None. 与 handbook._resolve_handbook_dir 同一模式.
    """
    base_path = None
    if config is not None:
        if isinstance(config, dict):
            base_path = config.get("basePath", "") or config.get("base_path", "") or None
        else:
            base_path = getattr(config, "base_path", "") or None
    if base_path:
        return Path(base_path)
    return Path.home() / ".v3-core" / "profiles" / "default"

logger = logging.getLogger("v3core.config")

# Pattern: ${env:VAR_NAME}
_ENV_RE = re.compile(r"\${env:(\w+)}")


def _prompts_map(cfg) -> dict:
    """从 cfg (V3Config | dict) 抽出 prompts dict, 兼容新旧两种形态.

    - V3Config 实例优先读 .prompts, 缺失字段则读 .prompts_extract (向后兼容).
    - dict 走 cfg.get("prompts", {}).
    - 永远返回新 dict, 不修改 cfg.
    """
    if cfg is None:
        return {}
    try:
        # V3Config 路径 — 有 prompts 字段就用，没有也安全降级
        prompts = getattr(cfg, "prompts", None)
        if isinstance(prompts, dict):
            out = dict(prompts)
            # 向后兼容: prompts_extract 单字段映射到 prompts["extract"]
            legacy = getattr(cfg, "prompts_extract", "") or ""
            if legacy and "extract" not in out:
                out["extract"] = legacy
            return out
        # dict 路径
        if isinstance(cfg, dict):
            legacy_dict = cfg.get("prompts") or {}
            if not isinstance(legacy_dict, dict):
                legacy_dict = {}
            return dict(legacy_dict)
    except Exception:
        return {}
    return {}


def _resolve_prompt(cfg, key: str, default: str) -> str:
    """从 config.yaml / V3Config 读取自定义 prompt, 缺失则回落到内置默认值.

    配置示例 (config.yaml):
        prompts:
          e1_system: "你的自定义系统 prompt..."
          extract: "/abs/path/to/prompt.md"     # 支持文件路径 (向后兼容 extract.py)
          extract: "inline prompt text"          # 也支持内联文本
          session_summary: "..."

    设计要点:
      1. 永远不回出错 — 配置异常时返回 default, 绝不破坏生产路径.
      2. 支持 file-path 形态 (历史 extract.py 用法): 若值是路径且文件存在,
         读文件内容作为 prompt. 这样老的 extract 配置继续生效.
      3. 默认 prompt 永远在源代码, 用户不配 = 行为不变.
    """
    try:
        prompts = _prompts_map(cfg)
        value = prompts.get(key)
        if not value:
            return default
        value = str(value).strip()
        if not value:
            return default
        # 文件路径兼容: 仅当字符串看起来是路径且文件存在时才读文件
        # (含换行/超长文本的 inline prompt 不会被误判成路径)
        if "\n" not in value and len(value) < 500 and value.endswith((
            ".md", ".txt", ".prompt", ".yaml", ".yml",
        )):
            try:
                from pathlib import Path
                p = Path(value)
                if p.exists() and p.is_file():
                    # 2026-08-26 P0 编码契约: utf-8-sig 兼容带 BOM 的 prompt 文件
                    # (Windows 记事本默认 UTF-8+BOM; 无 BOM 时 utf-8-sig 完全透明)
                    return p.read_text(encoding="utf-8-sig")
            except Exception:
                pass
        return value
    except Exception:
        return default

# Legacy default config — kept as a dict for the loading pipeline; converted to
# V3Config at the end via ``from_legacy_dict``.
_LEGACY_DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "cloud",
    "basePath": "",
    "storage": {
        "pg": {"host": "localhost", "port": 5433, "database": "v3embeddings",
               "user": "v3user", "password": ""},
        "embed": {"endpoint": "", "dim": 1024, "apiKey": "",
                  "proxy": "", "model": ""},
        "rerank": {"endpoint": "", "proxy": "", "timeout": 30, "apiKey": "",
                  # 2026-09-02 P1.3.1: recall_pool._rerank 在 deadline 剩余预算
                  # 小于该值时走受控 RRF 回退 (skip + log), 避免发起 remote HTTP.
                  "min_remaining_s": 1.5},
    },
    "tkg": {"enabled": False, "conflict_threshold": 0.85},
    "e1": {"enabled": True},
    "llm": {"provider": "", "model": "", "api_key": ""},
    "prefetch": {"enabled": True, "default_limit": 5, "dual_path": True,
                     "rrf_k": 60, "include_message_vector": True,
                     # 2026-09-03 P1.3.1: 长 term 时启用 QA snapshot 路径的阈值;
                     # 默认 5 — 覆盖 5-term term_frequency SQL tail (~6502ms),
                     # 普通 query (term 数 <5) 仍走 combined/parallel PG ILIKE 路径.
                     "qa_snapshot_min_terms": 5},
    "ingest": {"cron": "0 2 * * *",
               "live_buffer": {"batch": 8, "flush_sec": 30.0}},
    "prompts": {},  # 用户可覆盖内置 prompt: {"extract": "...", "e1_system": "...", ...}
    "half_life": 30,
}


def _resolve_env(obj: Any) -> Any:
    if isinstance(obj, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    if isinstance(obj, list):
        return [_resolve_env(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _resolve_env(v) for k, v in obj.items()}
    return obj


def _find_config(profile: str = "default", hermes_home: str = "") -> Path | None:
    """查找 v3-core config.yaml 候选路径.

    hermes_home 契约 (官方 MemoryProvider 协议):
      - 老路径 ~/.v3-core/profiles/<profile>/config.yaml 优先 (本机生产零变化)
      - 只有老路径不存在 (全新安装) 才用 hermes_home/.v3-core/profiles/<profile>/config.yaml
      - V3CORE_CONFIG 显式覆盖 > 老路径 > 新路径 > 全局 ~/.v3-core/config.yaml
    """
    candidates = [
        Path.home() / ".v3-core" / "profiles" / profile / "config.yaml",
        Path.home() / ".v3-core" / "config.yaml",
    ]
    # hermes_home 路径 (全新安装场景): 仅当老路径不存在时启用
    if hermes_home:
        candidates.insert(
            1,
            Path(hermes_home) / ".v3-core" / "profiles" / profile / "config.yaml",
        )
    v3c = os.environ.get("V3CORE_CONFIG", "")
    if v3c:
        candidates.insert(0, Path(v3c))
    for p in candidates:
        if p and p.exists():
            return p
    return None


def _find_env() -> Path | None:
    """找 .env 文件, 支持多环境 fallback.

    优先级:
    1. V3CORE_DOTENV 环境变量 (显式指定)
    2. V3CORE_HOME/.env (自定义 home)
    3. ~/.v3-core/.env (当前主机)
    4. ~/.hermes/.env (hermes 共享)
    5. WSL: /mnt/c/Users/<user>/.v3-core/.env
    """
    explicit = os.environ.get("V3CORE_DOTENV", "")
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
    v3home = os.environ.get("V3CORE_HOME", "")
    if v3home:
        p = Path(v3home) / ".env"
        if p.exists():
            return p
    for p in [Path.home() / ".v3-core" / ".env",
              Path.home() / ".hermes" / ".env"]:
        if p.exists():
            return p
    # WSL fallback: /mnt/c/Users/<user>/.v3-core/.env
    try:
        wsl_home = Path("/mnt/c/Users") / os.environ.get("USER", os.environ.get("USERNAME", "user")) / ".v3-core" / ".env"
        if wsl_home.exists():
            return wsl_home
    except Exception:
        pass
    return None


def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        # 2026-08-26 P0 编码契约: .env 可能被 Windows 编辑器存成 UTF-8+BOM,
        # BOM 会污染首个 key 名 (如 '\ufeffLLM_API_KEY') → setdefault 失效。
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip(chr(34)).strip(chr(39))
    except Exception as e:
        logger.warning("读 .env 失败: %s", e)
    return env


def _deep_merge(base: dict, overlay: dict) -> None:
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _deep_copy_dict(d: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in d.items():
        out[k] = _deep_copy_dict(v) if isinstance(v, dict) else v
    return out


def _normalize_base_path(bp: str) -> str:
    """WSL 下把 Windows 盘符路径转 /mnt/; 其他平台原样返回."""
    if bp and sys.platform != "win32" and len(bp) > 2 and bp[1] == ":":
        rest = bp[2:].replace("\\", "/").lstrip("/")
        return f"/mnt/{bp[0].lower()}/{rest}"
    return bp


def _load_legacy_dict(profile: str = "default", hermes_home: str = "") -> dict[str, Any]:
    """Legacy low-level loader — returns raw dict (for diagnostics / migration).

    Not normally called directly; use :func:`resolve_config`.

    hermes_home 契约: 老路径存在时继续用老路径 (本机生产零变化), 只有全新安装
    (老路径不存在) 时才用 hermes_home/.v3-core/profiles/<profile>/. 透传给 _find_config.
    """
    cfg = _deep_copy_dict(_LEGACY_DEFAULT_CONFIG)

    env_path = _find_env()
    if env_path:
        env_vars = _load_env_file(env_path)
        for k, v in env_vars.items():
            os.environ.setdefault(k, v)

    config_path = _find_config(profile, hermes_home=hermes_home)
    # Profile-scoped .env (v0.2 closing round): `hippocampus install` writes the
    # generated PG password AND the provider keys into <profile_dir>/.env, next
    # to the config.yaml that references them as ${env:...}. That file was never
    # one of `_find_env()`'s candidates, so a clean process resolved every
    # provider reference to an empty string: the first session after a fresh
    # install 401'd on embed / rerank / memory LLM, and `doctor --full` told the
    # user their key had been rejected — while the key sat unread in the profile.
    # `setdefault` keeps the existing precedence (explicit env, then the global
    # .env, then this profile's file) so nothing that already works changes.
    if config_path:
        _profile_env_path = Path(config_path).parent / ".env"
        if _profile_env_path.exists():
            for k, v in _load_env_file(_profile_env_path).items():
                os.environ.setdefault(k, v)
    if config_path:
        try:
            import yaml
            # 2026-08-26 P0 编码契约: config.yaml 必须用 utf-8-sig 读 —
            # Windows 记事本保存后带 BOM, 裸 utf-8 会让 yaml 首键变成 '\ufeffbasePath'
            # 直接解析错位 (用户用记事本改一次配置就炸)。无 BOM 时 sig 模式完全透明。
            with open(config_path, encoding="utf-8-sig") as f:
                yaml_cfg = yaml.safe_load(f)
            if yaml_cfg:
                _deep_merge(cfg, yaml_cfg)
        except ImportError:
            pass
        except Exception as e:
            logger.warning("config.yaml 解析失败: %s", e)

    cfg = _apply_pg_password_env(cfg)
    cfg = _resolve_env(cfg)
    bp = cfg.get("basePath", "")
    if isinstance(bp, str):
        cfg["basePath"] = _normalize_base_path(bp)
    return cfg


# Environment variable name documented in the public install contract.
# plugin.yaml `requires_env: [V3CORE_PG_PASSWORD]` means a hermes host that
# enforces that constraint will block plugin load when this is missing.
# This module-level constant is the single source of truth for that contract.
PG_PASSWORD_ENV_VAR = "V3CORE_PG_PASSWORD"


def _apply_pg_password_env(cfg: dict[str, Any]) -> dict[str, Any]:
    """Explicit-env mapping for the documented public-install contract.

    Behaviour (smallest change that satisfies the contract):

      * Read ``os.environ[PG_PASSWORD_ENV_VAR]``.
      * If the var is **absent** or **empty string** → return cfg unchanged
        (the existing fail-fast in ``resolve_config`` still triggers when the
        effective password is empty; this preserves current behaviour).
      * If the var is **non-empty** → it takes precedence over whatever YAML
        declared under ``storage.pg.password``:
          - empty YAML value  → fills in the env value
          - non-empty YAML    → env value overrides YAML
        This matches the documented precedence ("env var works") in the
        public install / plugin README.

    Secret-safety contract:
      * The value is NEVER logged, NEVER printed, NEVER included in error
        messages. ``logger.debug`` is intentionally NOT called with the value
        — only the env var name appears in any log line. Tests assert this
        via caplog.
    """
    env_val = os.environ.get(PG_PASSWORD_ENV_VAR, "")
    if not env_val:
        # Absent / empty env: do not touch cfg — existing fail-fast in
        # resolve_config() will raise if the effective password is empty.
        return cfg
    # Non-empty env: explicitly map to legacy storage.pg.password before
    # from_legacy_dict. We mutate in place — cfg is already a deep copy of
    # _LEGACY_DEFAULT_CONFIG that this function owns.
    storage = cfg.get("storage")
    if not isinstance(storage, dict):
        storage = {}
        cfg["storage"] = storage
    pg = storage.get("pg")
    if not isinstance(pg, dict):
        pg = {}
        storage["pg"] = pg
    pg["password"] = env_val
    return cfg


def resolve_config(profile: str = "default",
                   *,
                   hermes_home: str = "",
                   return_legacy: bool = False
                   ) -> V3Config | dict[str, Any]:
    """Load config from YAML + env, return typed config.

    Backward compat:
      - If ``return_legacy=True``, returns raw dict (the old shape) for any
        caller that hasn't migrated yet. Default is ``False`` (typed).
      - Tools / scripts that still do ``cfg.get("storage", {}).get("pg", {})``
        should set ``return_legacy=True`` during the migration window, or use
        ``V3Config.to_legacy_dict()``.

    hermes_home (官方 MemoryProvider 协议): 透传 _find_config / _load_legacy_dict.
      - 老路径 ~/.v3-core/profiles/<profile>/config.yaml 存在 → 继续用老路径
        (本机生产零变化, 不接受 hermes_home 覆盖).
      - 老路径不存在 + hermes_home 非空 → 用 hermes_home/.v3-core/profiles/<profile>/
        (全新安装场景).
      - hermes_home 默认 "", 行为与 v4.0.0 之前一致 (完全向后兼容).

    Returns ``V3Config`` instance.
    """
    legacy = _load_legacy_dict(profile, hermes_home=hermes_home)
    if return_legacy:
        return legacy
    cfg = from_legacy_dict(legacy)
    # fail-fast: PG 密码不能为空（防止默认密码落生产）
    if cfg.pg and not cfg.pg.password:
        raise RuntimeError(
            "V3CORE_PG_PASSWORD not set. "
            "Set environment variable V3CORE_PG_PASSWORD before starting."
        )
    return cfg
