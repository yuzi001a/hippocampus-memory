"""Public-alpha hardening 嵌入块区分契约.

聚焦修复: 区分"完全未配置 embed / 默认空 embed 块" 与"显式配置但
不完整的 embed 块". 历史上 ``_LEGACY_DEFAULT_CONFIG.storage.embed`` 始终存在
(空 endpoint/model + dim=1024), 走 ``from_legacy_dict`` 后 V3Config 总带一个
空的 ``EmbedConfig`` 实例 → ``_embed_section_present`` 误判为 True → ``build_embed_cfg``
raise ``ValueError("embed endpoint 未配置")``. 公共文档承诺 embedding 可选 /
keyword-only 模式, 期望"未配 embed"应返回 disabled.

新契约:
  * ``V3Config(embed=None)`` → safe_embed_cfg 返回 None (disabled, 不抛)
  * ``V3Config(embed=EmbedConfig())`` 默认空块 (endpoint='', model='') → disabled
  * dict 形态 legacy 默认空 embed 块 → disabled
  * 显式 endpoint 但无 model → fail-closed, 抛 ValueError ("model 未配置")
  * 完整 endpoint + model → 返回带 _fingerprint 的 cfg

不连真实 HTTP / 不连 PG / 不读 config.yaml. 单文件, pytest 一次跑完.
"""
from __future__ import annotations

import os
import sys

import pytest

# 让 pytest 在不同 cwd 下都能 import v3core — 与既有 RED 测试一致
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from v3core.config_model import EmbedConfig, V3Config
from v3core.embedding import (
    _embed_section_present,
    build_embed_cfg,
    safe_embed_cfg,
)


# ── 1. V3Config(embed=None) disabled ─────────────────────────────────────


class TestV3ConfigEmbedNoneDisabled:
    """V3Config(embed=None) 视为完全未配置 → safe_embed_cfg 返回 None."""

    def test_embed_section_present_returns_false(self):
        cfg = V3Config(embed=None)
        assert _embed_section_present(cfg) is False

    def test_safe_embed_cfg_returns_none(self):
        cfg = V3Config(embed=None)
        assert safe_embed_cfg(cfg) is None

    def test_build_embed_cfg_returns_disabled(self):
        """build_embed_cfg 对 None embed 也走 disabled 路径 (返回空 cfg 不抛)."""
        cfg = V3Config(embed=None)
        d = build_embed_cfg(cfg)
        assert d["endpoint"] == ""
        assert d["model"] == ""
        assert d["_fingerprint"] == ""


# ── 2. legacy/dict default empty embed block disabled ────────────────────


class TestLegacyDefaultEmptyEmbedDisabled:
    """``_LEGACY_DEFAULT_CONFIG.storage.embed`` 形态 (endpoint='', dim=1024,
    apiKey='', proxy='', model='') 是默认空块, 不应抛 ValueError."""

    def test_legacy_default_dict_storage_embed_section_not_present(self):
        cfg = {"storage": {"embed": {"endpoint": "", "dim": 1024,
                                       "apiKey": "", "proxy": "", "model": ""}}}
        assert _embed_section_present(cfg) is False

    def test_legacy_default_dict_storage_embed_safe_embed_cfg_none(self):
        cfg = {"storage": {"embed": {"endpoint": "", "dim": 1024,
                                       "apiKey": "", "proxy": "", "model": ""}}}
        assert safe_embed_cfg(cfg) is None

    def test_v3config_with_default_embedconfig_section_not_present(self):
        """``V3Config(embed=EmbedConfig())`` (默认构造) 与 legacy default 等价."""
        cfg = V3Config(embed=EmbedConfig())  # all defaults
        assert _embed_section_present(cfg) is False

    def test_v3config_with_default_embedconfig_safe_embed_cfg_none(self):
        cfg = V3Config(embed=EmbedConfig())
        assert safe_embed_cfg(cfg) is None

    def test_top_level_empty_embed_dict_disabled(self):
        """top-level ``{"embed": {}}`` 也应 disabled."""
        cfg = {"embed": {}}
        assert _embed_section_present(cfg) is False
        assert safe_embed_cfg(cfg) is None


# ── 3. explicit endpoint without model raises (fail-closed) ──────────────


class TestExplicitPartialRaisesFailClosed:
    """用户显式给了 endpoint 但缺 model → 必须 raise ValueError, 禁止伪装为
    disabled. 这一行为是 fail-closed 契约的核心, 不能因为新 disabled 路径而
    退化."""

    def test_v3config_endpoint_only_raises(self):
        cfg = V3Config(embed=EmbedConfig(endpoint="http://x.invalid", dim=1024,
                                          model=""))
        assert _embed_section_present(cfg) is True
        with pytest.raises(ValueError, match="model 未配置"):
            safe_embed_cfg(cfg)

    def test_dict_endpoint_only_raises(self):
        cfg = {"storage": {"embed": {"endpoint": "http://x.invalid", "dim": 1024,
                                       "model": ""}}}
        assert _embed_section_present(cfg) is True
        with pytest.raises(ValueError, match="model 未配置"):
            safe_embed_cfg(cfg)

    def test_dict_model_only_raises_endpoint_missing(self):
        """对称: 用户给了 model 但缺 endpoint → 同样 fail-closed."""
        cfg = {"storage": {"embed": {"model": "BAAI/bge-m3", "dim": 1024}}}
        assert _embed_section_present(cfg) is True
        with pytest.raises(ValueError, match="endpoint"):
            safe_embed_cfg(cfg)

    def test_explicit_complete_endpoint_model_does_not_raise(self):
        """endpoint 非空且 model 非空 → 不抛, 返回 cfg (正向对照)."""
        cfg = V3Config(embed=EmbedConfig(endpoint="http://x.invalid", dim=1024,
                                          model="BAAI/bge-m3"))
        d = safe_embed_cfg(cfg)
        assert d is not None
        assert d["model"] == "BAAI/bge-m3"


# ── 4. complete endpoint+model returns valid config ───────────────────────


class TestCompleteConfigReturnsCfg:
    """完整 endpoint + model → safe_embed_cfg 返回带 _fingerprint 的 cfg."""

    def test_v3config_complete_returns_validated(self):
        cfg = V3Config(embed=EmbedConfig(endpoint="http://x.invalid", dim=1024,
                                          model="BAAI/bge-m3"))
        assert _embed_section_present(cfg) is True
        d = safe_embed_cfg(cfg)
        assert d is not None
        assert d["endpoint"] == "http://x.invalid"
        assert d["model"] == "BAAI/bge-m3"
        assert len(d["_fingerprint"]) == 12

    def test_dict_complete_returns_validated(self):
        cfg = {"storage": {"embed": {"endpoint": "http://x.invalid",
                                       "model": "BAAI/bge-m3", "dim": 1024}}}
        d = safe_embed_cfg(cfg)
        assert d is not None
        assert d["model"] == "BAAI/bge-m3"
        assert d["_fingerprint"] != ""

    def test_top_level_embed_dict_complete(self):
        """top-level embed dict (兼容 V3Config 平铺) 也应正常工作."""
        cfg = {"embed": {"endpoint": "http://x.invalid", "model": "BAAI/bge-m3"}}
        d = safe_embed_cfg(cfg)
        assert d is not None
        assert d["model"] == "BAAI/bge-m3"


# ── 5. cross-checks against existing safe_embed_cfg contract ──────────────


class TestSafeEmbedCfgContractRegression:
    """回归 guard: 老的 ``safe_embed_cfg(None)`` / ``safe_embed_cfg({})`` 行为
    必须保留 (既已存在的测试不应被破坏)."""

    def test_none_returns_none(self):
        assert safe_embed_cfg(None) is None

    def test_empty_dict_returns_none(self):
        assert safe_embed_cfg({}) is None

    def test_none_dict_no_embed_returns_none(self):
        """dict 完全无 embed 键 → disabled (历史行为)."""
        cfg = {"foo": "bar", "llm": {"provider": "x", "model": "y"}}
        assert safe_embed_cfg(cfg) is None


# ── 6. provider-config smoke (no network) ────────────────────────────────


class TestProviderConfigSmokeNoNetwork:
    """模拟一份"用户没配 embed"的 provider config 走 resolve_config → safe_embed_cfg
    整条链路, 不连真实网络 / 不读真实 YAML, 验证最终落到 disabled (不抛)."""

    def test_resolve_config_without_embed_yaml_returns_disabled(self, monkeypatch, tmp_path):
        """最小可复现: 写一份仅含 basePath / pg 的 yaml, 不写 embed 块,
        resolve_config() 后 safe_embed_cfg 不应抛."""
        from v3core import config as _config

        # 隔离 env, 让 _find_config 找不到生产 config.yaml
        monkeypatch.setenv("V3CORE_CONFIG", "")
        monkeypatch.setenv("V3CORE_DOTENV", "")
        monkeypatch.setenv("V3CORE_PG_PASSWORD", "smoke-test-password")

        cfg_yaml = tmp_path / "config.yaml"
        cfg_yaml.write_text(
            "mode: cloud\n"
            "basePath: ''\n"
            "storage:\n"
            "  pg:\n"
            "    host: localhost\n"
            "    port: 5433\n"
            "    database: v3embeddings\n"
            "    user: v3user\n"
            "    password: ''\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("V3CORE_CONFIG", str(cfg_yaml))

        # resolve_config 现在必须返回 V3Config 且 embed=EmbedConfig() 默认空块
        resolved = _config.resolve_config("default")
        assert isinstance(resolved, V3Config)
        # 用户没写 embed → 默认空 EmbedConfig (来自 _LEGACY_DEFAULT_CONFIG 合并)
        assert resolved.embed is not None
        assert resolved.embed.endpoint == ""
        assert resolved.embed.model == ""

        # 关键: safe_embed_cfg 不应抛, 应返回 None (disabled)
        assert safe_embed_cfg(resolved) is None
        # 验证 build_embed_cfg 同样 disabled
        d = build_embed_cfg(resolved)
        assert d["_fingerprint"] == ""
        assert d["endpoint"] == ""

        # 验证 get_embed_fingerprint / get_embed_profile 也走 disabled 路径
        from v3core.embedding import get_embed_fingerprint, get_embed_profile
        assert get_embed_fingerprint(resolved) == ""
        prof = get_embed_profile(resolved)
        assert prof.model == ""