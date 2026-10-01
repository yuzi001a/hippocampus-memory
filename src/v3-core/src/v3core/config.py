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
import threading
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
    "TEST_MODE_ENV_VAR",
    "TestModeConfigError",
    "TEST_MODE_WORKER_EXIT_CODE",
]


# ══════════════════════════════════════════════════════════════════════════
# F2-A1/A2 — explicit TEST mode (opt-in, fail-closed)
# ══════════════════════════════════════════════════════════════════════════
#
# Why this exists. The isolated replay harness passed a test DSN and expected
# an isolated data root, but every resolver still had its production
# fallbacks live:
#
#   * ``_find_config()`` fell through to ``~/.v3-core/profiles/<p>/config.yaml``
#     and then the global ``~/.v3-core/config.yaml`` when ``V3CORE_CONFIG``
#     pointed at a file that no longer existed;
#   * ``_find_env()`` scanned ``~/.v3-core/.env`` and ``~/.hermes/.env``, so a
#     test process silently imported production PG/API secrets;
#   * ``_resolve_data_dir()`` with no argument returned
#     ``~/.v3-core/profiles/default`` — so ``topic_store`` wrote
#     ``v3_topic.db`` into the production profile.
#
# Setting ``V3CORE_TEST_MODE=1`` narrows ALL of that to exactly one source: the
# explicit, existing, readable YAML named by ``V3CORE_CONFIG``. Anything
# missing or invalid is a refusal, not a fallback.
#
# Contract:
#   1. opt-in only — with the flag unset, every historical behaviour is intact;
#   2. re-validated on EVERY call, never cached, so a config that was fine and
#      is then deleted still refuses (this is T1's actual sequence);
#   3. the refusal is a ``SystemExit`` subclass and deliberately NOT an
#      ``Exception``: ``serve()`` (:func:`v3core.serve.serve`) wraps core
#      construction in ``except Exception`` + ``sys.exit(1)``, and so do the
#      CLI and E1 paths. An Exception subclass would be swallowed there and the
#      process would keep going on the production profile — the exact defect
#      this flag exists to close;
#   4. validation happens before any production directory is read or created;
#   5. off the main thread, ``SystemExit`` alone is NOT enough: ``threading``
#      only unwinds the thread that raised, so a writer / HTTP worker that
#      loses its config would die silently while ``serve()`` kept its socket
#      open and a half-configured process kept running. A TEST-mode refusal
#      created off the main thread therefore terminates the whole isolated
#      process with :data:`TEST_MODE_WORKER_EXIT_CODE` instead of returning an
#      exception. Scoped to TEST mode by construction: every ``_refuse`` call
#      site sits inside a ``_test_mode_requested()`` branch, and production mode
#      never reaches this function at all.
TEST_MODE_ENV_VAR = "V3CORE_TEST_MODE"

#: Leading text of every refusal. Tests and the harness match on it.
TEST_MODE_REFUSAL_PREFIX = "V3CORE_TEST_MODE fail-closed"

#: Exit code used when a TEST-mode refusal is created off the main thread.
#: EX_SOFTWARE. Deliberately distinct from the F2 harness verdict codes
#: (0 accepted / 1 rejected / 2 no-evidence / 3 partial) and from the child
#: startup-refusal code 97, so a harness reading a child's rc tells "this
#: isolated process was torn down by a mid-run config loss" apart from both
#: "the child refused to boot" and "the run was rejected".
TEST_MODE_WORKER_EXIT_CODE = 70

#: Exit code used when a TEST-mode refusal is created off the main thread.
#: EX_SOFTWARE. Deliberately distinct from the F2 harness verdict codes
#: (0 accepted / 1 rejected / 2 no-evidence / 3 partial) and from the child
#: startup-refusal code 97, so a harness reading a child's rc tells "this
#: isolated process was torn down by a mid-run config loss" apart from both
#: "the child refused to boot" and "the run was rejected".
TEST_MODE_WORKER_EXIT_CODE = 70

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})


class TestModeConfigError(SystemExit):
    """Fail-closed refusal raised by every TEST-mode resolver.

    Subclasses ``SystemExit`` and NOT ``Exception`` on purpose: see the module
    comment. ``reason`` carries the same text as the exit message, so a caller
    that catches ``BaseException`` can report it without re-parsing.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _sanitize_refusal_detail(detail: str, limit: int = 300) -> str:
    """One-line, length-bounded form of a refusal detail, for the critical log.

    Refusal details embed filesystem paths and parser text. The critical line
    is the last thing a torn-down worker leaves behind, so it is collapsed to a
    single line and truncated. No secret reaches it: the refusals built here
    quote paths and exception strings only, never config values.
    """
    flat = " ".join(str(detail).split())
    if len(flat) > limit:
        flat = flat[:limit] + "…"
    return flat


def _terminate_test_process_off_thread(reason: str) -> None:
    """Announce an off-main-thread TEST refusal, then kill the whole process.

    ``os._exit`` skips atexit hooks and interpreter shutdown, so the critical
    line is emitted to the logger and to stderr and flushed first — otherwise
    the evidence of the refusal would die with the process.
    """
    message = (
        f"{TEST_MODE_REFUSAL_PREFIX}: off-main-thread TEST refusal in thread "
        f"{threading.current_thread().name!r} — the isolated process is no longer "
        f"configured and is being terminated with code "
        f"{TEST_MODE_WORKER_EXIT_CODE} (refusing to keep serving). {reason}"
    )
    try:
        logger = logging.getLogger("v3core.config")
        logger.critical(message)
        for handler in (*logger.handlers, *logging.getLogger().handlers):
            try:
                handler.flush()
            except Exception:  # noqa: BLE001 — a broken handler must not block
                pass
    except Exception:  # noqa: BLE001 — logging must never block the teardown
        pass
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(TEST_MODE_WORKER_EXIT_CODE)


def _refuse(detail: str) -> "TestModeConfigError":
    """Build (do not raise) a refusal so callers can attach their own context.

    TEST mode only — every caller sits inside a ``_test_mode_requested()``
    branch, so production never constructs a refusal and never reaches the
    process-exit path below.

    On the main thread this returns the exception unchanged, so the existing
    startup refusal (CLI / ``serve()`` / harness provenance collection, all of
    which report the message) behaves exactly as before. Off the main thread a
    returned exception is not a refusal at all: ``threading`` unwinds only the
    raising thread, so a writer or HTTP worker would vanish silently while
    ``serve()`` kept its socket open. There the whole isolated process is torn
    down instead, which is the only outcome that satisfies T1's HARD FAIL for
    a mid-run config loss.
    """
    reason = f"{TEST_MODE_REFUSAL_PREFIX}: {detail}"
    if threading.current_thread() is not threading.main_thread():
        _terminate_test_process_off_thread(_sanitize_refusal_detail(reason))
    return TestModeConfigError(reason)


def _test_mode_requested() -> bool:
    """True only for an explicit, recognised opt-in value.

    An unrecognised value (e.g. ``V3CORE_TEST_MODE=maybe``) is treated as NOT
    requested rather than guessing — production keeps its normal behaviour and
    a typo can never be mistaken for a test.
    """
    raw = (os.environ.get(TEST_MODE_ENV_VAR) or "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    logger = logging.getLogger("v3core.config")
    logger.warning(
        "%s=%r is not a recognised value (%s / %s) — treating TEST mode as OFF",
        TEST_MODE_ENV_VAR, raw, sorted(_TRUTHY), sorted(_FALSY))
    return False


def _test_config_path() -> Path:
    """The one and only config path in TEST mode — validated, not resolved.

    Existence, file-ness and readability are checked on every call. Returning a
    cached path after it was deleted is precisely the T1 bug, so there is no
    memoisation anywhere on this route.
    """
    raw = (os.environ.get("V3CORE_CONFIG") or "").strip()
    if not raw:
        raise _refuse(
            "V3CORE_CONFIG is not set. TEST mode requires an explicit config; "
            "there is no default-profile fallback. Unset V3CORE_TEST_MODE to "
            "run in production mode."
        )
    path = Path(raw)
    if not path.exists():
        raise _refuse(
            f"explicit V3CORE_CONFIG does not exist: {path}. "
            f"Refusing to fall back to any ambient or default config."
        )
    if not path.is_file():
        raise _refuse(f"explicit V3CORE_CONFIG is not a readable file: {path}")
    try:
        with open(path, "rb"):
            pass
    except OSError as exc:
        raise _refuse(
            f"explicit V3CORE_CONFIG is not readable: {path} ({exc.strerror or exc})"
        ) from None
    return path


def _read_config_mapping(path: Path) -> dict:
    """Parse a config file into a mapping, refusing on anything else."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is a hard dependency
        raise _refuse(f"PyYAML unavailable, cannot validate {path}: {exc}") from None
    try:
        # utf-8-sig: Windows editors write a BOM, which would otherwise make
        # the first key parse as '\\ufeffbasePath'.
        with open(path, encoding="utf-8-sig") as handle:
            doc = yaml.safe_load(handle)
    except OSError as exc:
        raise _refuse(
            f"explicit V3CORE_CONFIG could not be read: {path} "
            f"({exc.strerror or exc})"
        ) from None
    except yaml.YAMLError as exc:
        raise _refuse(f"explicit V3CORE_CONFIG is not valid YAML: {path} ({exc})") from None
    if doc is None:
        raise _refuse(f"explicit V3CORE_CONFIG is empty: {path}")
    if not isinstance(doc, dict):
        raise _refuse(
            f"explicit V3CORE_CONFIG must be a YAML mapping, got "
            f"{type(doc).__name__}: {path}"
        )
    return doc


def _test_base_path(path: Path) -> Path:
    """The validated ``basePath`` of the explicit TEST-mode config."""
    doc = _read_config_mapping(path)
    raw = doc.get("basePath") or doc.get("base_path") or ""
    if not isinstance(raw, str) or not raw.strip():
        raise _refuse(
            f"explicit V3CORE_CONFIG declares no usable basePath: {path}. "
            f"TEST mode has no default data root."
        )
    return Path(_normalize_base_path(raw.strip()))


def _validated_test_root() -> Path:
    """Validate the explicit TEST-mode config and return its data root.

    This is the single entry point every TEST-mode resolver calls. It reads
    nothing outside the explicit config, creates nothing, and refuses rather
    than degrading to any ambient value.
    """
    return _test_base_path(_test_config_path())


def _config_base_path(config) -> str | None:
    """``basePath`` / ``base_path`` of a V3Config | dict | None."""
    if config is None:
        return None
    if isinstance(config, dict):
        return config.get("basePath", "") or config.get("base_path", "") or None
    return getattr(config, "base_path", "") or None


def _resolve_data_dir(config=None) -> Path:
    """解析 v3-core 数据根目录 — config.base_path 优先, 兜底 ~/.v3-core/profiles/default.

    Accepts V3Config / dict / None. 与 handbook._resolve_handbook_dir 同一模式.

    B02: 当调用方没传 config 时，取本次工具调用绑定的 booted profile
    (tools._scope)；未绑定（CLI/后台管线）时保持历史 default 语义.

    F2-A2: under ``V3CORE_TEST_MODE`` this is fail-closed. The root always
    comes from the validated explicit config, a config object that disagrees
    with it is refused, and there is no default-profile fallback at all — the
    production fallback below is unreachable while the flag is on.
    """
    if _test_mode_requested():
        root = _validated_test_root()
        supplied = _config_base_path(config)
        if supplied:
            if Path(_normalize_base_path(str(supplied).strip())) != root:
                raise _refuse(
                    f"supplied config points at {supplied} but the explicit "
                    f"V3CORE_CONFIG declares {root}. A caller-supplied config "
                    f"may not re-route the isolated data root."
                )
        return root
    if config is None:
        try:
            from ._tool_scope import current_scope
            config = current_scope()
        except Exception:
            config = None
    base_path = _config_base_path(config)
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

    2026-09-27 (B02): 未显式传 hermes_home 时取 ``HERMES_HOME`` 环境变量。
    Installer 把全新安装的 profile 写在 ``<HERMES_HOME>/.v3-core/profiles/<profile>/``,
    而 install 侧 (distribution_cli / doctor_full / 官方 provider) 都显式传 hermes_home,
    运行时入口 (``v3-core mcp`` / CLI) 不传 —— 结果是同一个 profile 在直接进程里能解析、
    在 MCP 子进程里解析不到: 工具面照常列出 13 个工具, 第一次调用才失败 (静默半安装)。
    把 HERMES_HOME 作为环境默认后, 进程内所有 resolver 只有一份 effective config 真值。
    老路径仍优先, 既有安装零变化。

    F2-A1: under ``V3CORE_TEST_MODE`` the candidate list collapses to the one
    explicit ``V3CORE_CONFIG`` file, re-validated on every call (so a deleted
    config refuses instead of falling through to the home profile) and
    independent of ``profile`` / ``HERMES_HOME``. A test that lost its config
    must not silently boot on production.
    """
    if _test_mode_requested():
        return _test_config_path()
    if not hermes_home:
        hermes_home = os.environ.get("HERMES_HOME", "") or ""
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

    F2-A1: under ``V3CORE_TEST_MODE`` candidates 2-5 are removed, not merely
    deprioritised. ``_load_legacy_dict`` ``setdefault``s every key it finds
    into ``os.environ``, so a single ambient hit imports production
    ``V3CORE_PG_PASSWORD`` / provider keys into the test process — the test
    would then talk to production with a production credential it never
    declared. An explicit ``V3CORE_DOTENV`` is the only admitted source, and
    it must exist (a dangling value is a refusal, not a silent "no dotenv").
    """
    explicit = os.environ.get("V3CORE_DOTENV", "")
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        if _test_mode_requested():
            raise _refuse(
                f"explicit V3CORE_DOTENV does not exist: {p}. "
                f"TEST mode will not fall back to an ambient .env file."
            )
    if _test_mode_requested():
        return None
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

    F2-A1: under ``V3CORE_TEST_MODE`` an unparseable explicit config is a
    refusal rather than the ``logger.warning`` + defaults path — silently
    continuing here is what let a test boot with an empty ``basePath`` and
    then fall through to the production data root.
    """
    if _test_mode_requested():
        _validated_test_root()  # refuse before touching anything else
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
        if _test_mode_requested():
            # Already refused-validated by _validated_test_root() above; the
            # re-parse covers a config that changed under us, which is still
            # a refusal rather than a defaults fallback.
            _deep_merge(cfg, _read_config_mapping(Path(config_path)))
        else:
            try:
                import yaml
                # 2026-08-26 P0 编码契约: config.yaml 必须用 utf-8-sig 读 —
                # Windows 记事本保存后带 BOM, 裸 utf-8 会让 yaml 首键变成
                # '\ufeffbasePath' 直接解析错位 (用户用记事本改一次配置就炸)。
                # 无 BOM 时 sig 模式完全透明。
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


def _ambient_profile() -> str:
    """The profile this process booted ("" when nothing is booted).

    A bare ``resolve_config()`` must mean "the config this process is running
    on", not the literal ``default`` profile: core-internal leaves (topic
    recall, pools, embed helpers) are constructed by the booted core and have no
    profile argument to pass. Resolving ``default`` there made a non-default
    core read another profile's database — the B02 canary loaded 349 foreign
    topics into its recall pool that way.
    """
    try:
        from ._tool_scope import current_booted_profile

        return current_booted_profile()
    except Exception:  # noqa: BLE001
        return ""


def resolve_config(profile: str | None = None,
                   *,
                   hermes_home: str = "",
                   return_legacy: bool = False
                   ) -> V3Config | dict[str, Any]:
    """Load config from YAML + env, return typed config.

    ``profile=None`` (the default, i.e. a bare ``resolve_config()``) means the
    **ambient** profile: whatever core this process booted, falling back to
    ``default`` when nothing is booted. Pass an explicit name to pin one.

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
    if profile is None:
        profile = _ambient_profile() or "default"
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
