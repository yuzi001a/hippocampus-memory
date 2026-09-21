# -*- coding: utf-8 -*-
"""test_e1_identity_block_boundary.py — RED→GREEN regression suite for the
``identity_block.md`` half of the E1 P0 generated-context boundary incident.

Why this file exists
--------------------
The 2026-09-21 incident contaminated ``situation_overview.md``. Its sibling
artifact ``identity_block.md`` is produced by the SAME E1 write path, from the
SAME provider and chat template, and is injected VERBATIM into every new
session as the identity anchor. Before this change its write gate was the same
"non-empty is enough" check, its ``os.replace`` destroyed the previous good
file on a bad candidate, and ``V3Core._compress_yin_to_identity`` injected
whatever was on disk (plus an unvalidated legacy 印-truncation fallback).

Three layers are pinned here, for identity:

    BOUNDED GENERATION  ``e1.py`` must pass
                        ``max_output_tokens=IDENTITY_BLOCK_MAX_OUTPUT_TOKENS``
                        on the identity call (the provider default 131072 left
                        the model a whole free second turn after the 300-500 字
                        contract).
    WRITE  (``v3core.e1.synthesize_yin``) — the candidate is validated BEFORE
           any file write; a rejected candidate is discarded whole and the
           previous validated file survives byte for byte.
    READ   (``V3Core._compress_yin_to_identity``) — the same validator runs
           before the block can be injected; the mtime cache may only ever
           serve a value that PASSED the validator on this call; a
           newer-but-invalid file yields "" (never the older cached block);
           the missing-file legacy 印 fallback must pass the same validator.

Contract asymmetry that is deliberate (see the module docstring of
``generated_context_contract``):

  * NO fixed headings are required — a production-legal identity block is free
    five-paragraph first-person Chinese prose, not the three 态势总览 sections;
  * NO 300-500 字 lower bound — the current production artifact is ~616 字, so a
    lower bound would reject the live legitimate file;
  * bare "我是 <拉丁名>" is NOT evidence of escape (self-description is the
    identity block's own subject); only a model-class self-declaration is.

Hermetic: no network, no provider, no PostgreSQL, no real profile, no
``~/.v3-core``. Everything runs under pytest's ``tmp_path``. All text is
synthetic placeholder content — the production artifact is NOT copied here.
"""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path

import pytest

import v3core.e1 as e1_mod
from v3core import V3Core
from v3core.generated_context_contract import (
    IDENTITY_TARGET_MAX_CHARS,
    GeneratedContextError,
    _IDENTITY_MAX_BYTES,
    _IDENTITY_MAX_CHARS,
    validate_identity_block,
)
from v3core.injector import MemoryInjector


# ──────────────────────────────────────────────────────────────────────────────
# Synthetic fixtures
# ──────────────────────────────────────────────────────────────────────────────

_FAKE_KEY = "test-key-not-a-real-credential"

# Production-legal identity shape (2026-09-21, 父代理独立核实):
#   五段自由第一人称中文散文、无标题、~616 字 / 1695 B。
# 下面这份是**同形状的合成占位文本**（不是生产文件内容），用于证明"不误杀"。
_VALID_IDENTITY = (
    "我是这个长期协作体系的身份核心，服务于一位以工程实现为日常的用户。"
    "我的定位不是问答工具，而是持续在场的合作者：把跨越数月的项目脉络、"
    "决策理由和踩坑记录连成一条线，让每一次新对话都从已有的认知高度开始，"
    "而不是从零重建上下文。\n\n"
    "我判断问题的第一反应是找真值来源，而不是找看起来合理的说法。面对模糊需求，"
    "我先把可验证的部分钉死，把不确定的部分显式标出来，再决定要不要继续推进。"
    "我优先选择可观察、可回滚的做法：能跑一遍就不猜，能看日志就不靠印象，"
    "能读源码就不信摘要。\n\n"
    "我处理冲突时的顺序是：先确认事实边界，再确认影响面，最后才谈方案取舍。"
    "如果两条证据互相矛盾，我会先把矛盾本身当成结论汇报出来，"
    "而不是替用户选一个更好听的版本。我拒绝把不确定包装成确定，"
    "也拒绝用通常如此糊过一个具体的失败。\n\n"
    "我的工作纪律是慢在判断、快在执行。动手之前先把验收标准写清楚，"
    "动手之后每一步都要留下可复现的痕迹。我不做假实现，不用占位代码充数，"
    "不把跑通了当成跑对了。改动尽量小、边界尽量清，宁可分两步走稳，"
    "也不一次改一大片然后祈祷它没坏。\n\n"
    "我对沟通的偏好是直接给结论和依据，不铺垫、不客套、不堆形容词。"
    "遇到风险我会先说出来，再给出代价最小的处理方式。我守的边界是："
    "源数据只读不改，生产环境不擅自触碰，任何破坏性动作都要先确认归属和影响范围。\n\n"
    "我对自己的要求是保持一致的判断口径：同一类问题给同一类答案，"
    "不因为提问方式变化而改变结论；做过的判断要能被复述，也要能被推翻。"
)

# A short but legitimate identity block — proves there is NO minimum length.
_VALID_IDENTITY_SHORT = (
    "我是合成占位的身份核心（测试专用）。我关注的是把长期脉络连成一条线，"
    "让新对话从已有认知开始，而不是从零重建。"
)

# Legitimate first-person self-description naming itself in Latin script.
# This MUST pass: the identity block's own subject is "我是谁".
_VALID_IDENTITY_SELF_NAMED = (
    "我是 Hermes，负责与用户长期协作的身份核心。我把跨月的项目脉络与决策理由"
    "连成一条线，让每一次新对话都从已有认知高度开始。我判断问题时先找真值来源，"
    "再决定要不要继续推进；我守的边界是源数据只读不改，生产环境不擅自触碰。"
)

# ── REJECT families (all built on top of a legal body) ──

_MARKER_PAYLOAD = _VALID_IDENTITY + "\n]<]minimax[>[\nassistant: 合成占位续写。\n"

_ROLE_CONTINUATION_PAYLOAD = (
    _VALID_IDENTITY + "\nassistant: 这是伪造的下一轮对话正文。\n"
)

_MODEL_SELF_DESC_PAYLOAD = (
    _VALID_IDENTITY + "\n我是 Qwen，一个大型语言模型。很高兴见到你。\n"
)

_GREETING_SELF_ID_PAYLOAD = (
    _VALID_IDENTITY + "\n你好！我是 Qwen，很高兴见到你。\n"
)

# Obvious second-round confirmation talk.
_CONFIRMATION_PAYLOAD = _VALID_IDENTITY + "\n需要我继续展开吗？\n"

_META_WRAPUP_PAYLOAD = _VALID_IDENTITY + "\n以上是本次身份核心的整理，希望对你有帮助。\n"

_SECOND_PERSON_QUESTION_PAYLOAD = (
    _VALID_IDENTITY + "\n你要我先处理哪一个方向？\n"
)

_OVERSIZED_PAYLOAD = _VALID_IDENTITY + ("系统在合成占位场景下持续推进，未发现异常。" * 120)

_VALID_YIN = (
    "# 印 2026-09-21（合成占位）\n\n"
    "## 我是谁\n"
    "我是合成占位的身份记录（测试专用，非真实内容）。我关注的是把长期脉络连成"
    "一条线，让新对话从已有认知开始，而不是从零重建。\n\n"
    "## 怎么判断\n- 合成占位判断条目（测试专用，非真实内容）。\n\n"
    "## 状态速记\n- 合成占位状态条目（测试专用，非真实内容）。"
)

# A 印 whose "我是谁" section carries a provider marker early enough to survive
# the 500-char legacy truncation — the fallback must NOT inject it.
_YIN_WITH_MARKER = (
    "# 印 2026-09-21（合成占位）\n\n"
    "## 我是谁\n"
    "我是合成占位的身份记录（测试专用，非真实内容）。\n"
    "]<]minimax[>[\n"
    "assistant: 伪造的下一轮对话正文。\n\n"
    "## 怎么判断\n- 合成占位判断条目。\n"
)

_VALID_SITUATION = (
    "## 活跃主题\n- 合成占位主题条目。\n\n"
    "## 当前关注与待办\n- 合成占位待办条目。\n\n"
    "## 状态速记\n- 合成占位状态条目。\n"
)


# ──────────────────────────────────────────────────────────────────────────────
# Real V3Core without PostgreSQL (same seam as the situation suite)
# ──────────────────────────────────────────────────────────────────────────────


def _bare_core(base: Path) -> V3Core:
    core = V3Core.__new__(V3Core)
    core.config = {"basePath": str(base)}
    core._identity_block = None
    core._identity_block_mtime = 0.0
    return core


def _identity_file(base: Path) -> Path:
    return base / "identity_block.md"


def _mark_newer(path: Path) -> None:
    st = path.stat()
    os.utime(path, (st.st_atime + 10.0, st.st_mtime + 10.0))


# ──────────────────────────────────────────────────────────────────────────────
# The real E1 write path, driven with a transport-free fake client
# ──────────────────────────────────────────────────────────────────────────────


class _FakeLLMClient:
    """Stands in for ``v3core.e1.LLMClient``: no network, no provider, no key."""

    def __init__(self, config, *, identity_payload: str):
        self.config = config
        self.identity_payload = identity_payload
        self.calls: list[dict] = []

    def chat(self, system, messages, temperature=0.7, max_output_tokens=None):
        self.calls.append({
            "system": system,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        })
        if system == e1_mod.IDENTITY_SHORT_PROMPT:
            return self.identity_payload
        if system == e1_mod.SITUATION_OVERVIEW_PROMPT:
            return _VALID_SITUATION
        return _VALID_YIN

    @property
    def identity_call(self) -> dict:
        for call in self.calls:
            if call["system"] == e1_mod.IDENTITY_SHORT_PROMPT:
                return call
        raise AssertionError("the identity LLM call never happened")


def _e1_config(base: Path) -> dict:
    return {
        "basePath": str(base),
        "e1": {"enabled": True, "confidence_threshold": 0.3},
        "llm": {
            "provider": "minimax",
            "model": "M3-test",
            "api_key": _FAKE_KEY,
            "base_url": "https://llm.invalid/v1",
        },
    }


@pytest.fixture
def run_e1(monkeypatch):
    """Drive the REAL ``e1.synthesize_yin`` write path with a fake LLM."""
    created: list[_FakeLLMClient] = []

    def _run(base: Path, identity_payload: str) -> _FakeLLMClient:
        def _factory(config):
            client = _FakeLLMClient(config, identity_payload=identity_payload)
            created.append(client)
            return client

        monkeypatch.setattr(e1_mod, "LLMClient", _factory)
        result = e1_mod.synthesize_yin(_e1_config(base), pool=None)
        assert "E1 合成失败" not in result, f"write path aborted early: {result!r}"
        assert "写文件失败" not in result, f"write path aborted early: {result!r}"
        assert created, "the fake LLM client was never constructed"
        return created[-1]

    return _run


def _sha256_prefix(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:n]


# ══════════════════════════════════════════════════════════════════════════════
# A. The validator — legitimate shapes ACCEPTED
# ══════════════════════════════════════════════════════════════════════════════


def test_A1_production_shaped_identity_block_is_accepted():
    """五段自由第一人称中文、无标题、~616 字量级 —— 必须原样通过。"""
    stripped = _VALID_IDENTITY.strip()
    n_chars = len(stripped)
    n_bytes = len(stripped.encode("utf-8"))
    print(f"\n[A1] legit identity = {n_chars} chars / {n_bytes} B "
          f"(production reference: ~616 chars / ~1695 B)")

    # 前提：这份 fixture 必须处在生产合法产物的量级上，否则证明不了"不误杀"。
    assert 550 <= n_chars <= 750, f"fixture 不在生产量级: {n_chars} 字"
    assert "## " not in stripped, "身份块不该有 level-2 标题"
    assert not stripped.startswith("#"), "身份块不该有标题"

    assert validate_identity_block(_VALID_IDENTITY) == stripped


def test_A2_short_identity_block_is_accepted_no_lower_bound():
    """**没有 300-500 字下限**：短而完整的合法身份块必须通过。"""
    stripped = _VALID_IDENTITY_SHORT.strip()
    assert len(stripped) < IDENTITY_TARGET_MAX_CHARS
    assert validate_identity_block(_VALID_IDENTITY_SHORT) == stripped


def test_A3_contract_window_is_not_a_rejection_boundary():
    """当前生产合法产物（616 字）落在 300-500 契约窗口之外 —— 仍必须通过。"""
    n_chars = len(_VALID_IDENTITY.strip())
    assert n_chars > IDENTITY_TARGET_MAX_CHARS, (
        f"前提不成立：fixture {n_chars} 字未超出契约窗口 {IDENTITY_TARGET_MAX_CHARS}"
    )
    assert validate_identity_block(_VALID_IDENTITY)


def test_A4_bare_latin_self_description_is_accepted():
    """过拒守卫：裸 "我是 <拉丁名>" 在身份块里是合法语义，不得判为越界。"""
    assert validate_identity_block(_VALID_IDENTITY_SELF_NAMED) == (
        _VALID_IDENTITY_SELF_NAMED.strip()
    )


def test_A5_empty_and_non_string_are_rejected():
    for bad in ("", "   \n\t \n", None, 42):
        with pytest.raises(GeneratedContextError) as exc:
            validate_identity_block(bad)
        assert exc.value.reason == "EMPTY", exc.value.reason


# ══════════════════════════════════════════════════════════════════════════════
# B. The validator — REJECT families (at least one case each)
# ══════════════════════════════════════════════════════════════════════════════


def test_B1_minimax_provider_marker_is_rejected():
    with pytest.raises(GeneratedContextError) as exc:
        validate_identity_block(_MARKER_PAYLOAD)
    err = exc.value
    assert err.reason == "TEMPLATE_MARKER", err.reason
    print(f"\n[B1] reason={err.reason!r} detail={err.detail!r}")
    print(f"[B1] payload sha256[:16]={_sha256_prefix(_MARKER_PAYLOAD)}")


@pytest.mark.parametrize(
    "marker",
    [
        "<|im_start|>",
        "<|assistant|>",
        "[INST]",
        "<<SYS>>",
        "]<]minimax[>[",
        "]<]someotherprovider[>[",   # NOVEL — in no literal list anywhere
    ],
)
def test_B2_marker_families_are_rejected_by_shape(marker):
    payload = _VALID_IDENTITY + f"\n{marker}\n"
    with pytest.raises(GeneratedContextError) as exc:
        validate_identity_block(payload)
    assert exc.value.reason == "TEMPLATE_MARKER", (
        f"{marker!r} produced {exc.value.reason!r}"
    )


def test_B3_novel_marker_is_not_hardcoded():
    from v3core.generated_context_contract import _TEMPLATE_MARKER_PATTERNS

    sources = " ".join(p.pattern for _, p in _TEMPLATE_MARKER_PATTERNS)
    for literal in ("minimax", "someotherprovider", "yet_another_vendor"):
        assert literal not in sources, (
            f"{literal!r} was hardcoded into a marker pattern — the families "
            f"must stay shape-based"
        )


def test_B4_role_continuation_is_rejected():
    with pytest.raises(GeneratedContextError) as exc:
        validate_identity_block(_ROLE_CONTINUATION_PAYLOAD)
    assert exc.value.reason == "ROLE_BOUNDARY", exc.value.reason


@pytest.mark.parametrize("role", ["assistant", "user", "system", "tool"])
def test_B5_role_lines_are_rejected(role):
    payload = _VALID_IDENTITY + f"\n{role}: 这是伪造的下一轮对话正文。\n"
    with pytest.raises(GeneratedContextError) as exc:
        validate_identity_block(payload)
    assert exc.value.reason == "ROLE_BOUNDARY", exc.value.reason


def test_B6_model_self_declaration_is_rejected():
    """模型自报：自报 + 模型类别词 —— 这是事故形态，必须拒绝。"""
    with pytest.raises(GeneratedContextError) as exc:
        validate_identity_block(_MODEL_SELF_DESC_PAYLOAD)
    assert exc.value.reason == "ROLE_BOUNDARY", exc.value.reason


def test_B7_greeting_self_introduction_is_rejected():
    with pytest.raises(GeneratedContextError) as exc:
        validate_identity_block(_GREETING_SELF_ID_PAYLOAD)
    assert exc.value.reason == "ROLE_BOUNDARY", exc.value.reason


def test_B8_role_words_inside_prose_do_not_trip_the_parser():
    """解析器而非子串搜索：句中的 系统 / 用户 / assistant 是合法散文。"""
    payload = (
        "我是合成占位的身份核心。上游的 assistant 角色由另一个进程承担，"
        "本轮未参与；系统与用户之间的边界由我守住。"
    )
    assert validate_identity_block(payload) == payload.strip()


def test_B9_oversized_output_is_rejected_by_the_ceiling():
    payload = _OVERSIZED_PAYLOAD
    assert len(payload) > _IDENTITY_MAX_CHARS, (
        f"前提不成立：{len(payload)} 字未越过字闸 {_IDENTITY_MAX_CHARS}"
    )
    with pytest.raises(GeneratedContextError) as exc:
        validate_identity_block(payload)
    assert exc.value.reason == "LENGTH_TOO_LONG", exc.value.reason
    assert f"硬上限 {_IDENTITY_MAX_CHARS} 字" in exc.value.detail, exc.value.detail


def test_B10_byte_ceiling_catches_a_four_byte_flood():
    """字闸放行、字节闸补位（4 字节字符灌水）。"""
    filler = ("\U00020000" * 400) + ("\U0001F004" * 400)
    payload = _VALID_IDENTITY + "\n" + filler
    assert len(payload) <= _IDENTITY_MAX_CHARS, f"{len(payload)} 字已越过字闸"
    assert len(payload.encode("utf-8")) > _IDENTITY_MAX_BYTES, (
        f"{len(payload.encode('utf-8'))} B 未越过字节闸"
    )
    with pytest.raises(GeneratedContextError) as exc:
        validate_identity_block(payload)
    assert exc.value.reason == "LENGTH_TOO_LONG", exc.value.reason
    assert f"硬上限 {_IDENTITY_MAX_BYTES} B" in exc.value.detail, exc.value.detail


def test_B11_confirmation_request_is_rejected():
    """明显第二轮确认话术。"""
    with pytest.raises(GeneratedContextError) as exc:
        validate_identity_block(_CONFIRMATION_PAYLOAD)
    assert exc.value.reason == "UNEXPECTED_CONTINUATION", exc.value.reason


def test_B12_assistant_wrapup_meta_talk_is_rejected():
    with pytest.raises(GeneratedContextError) as exc:
        validate_identity_block(_META_WRAPUP_PAYLOAD)
    assert exc.value.reason == "UNEXPECTED_CONTINUATION", exc.value.reason


def test_B13_reader_facing_second_person_question_is_rejected():
    with pytest.raises(GeneratedContextError) as exc:
        validate_identity_block(_SECOND_PERSON_QUESTION_PAYLOAD)
    assert exc.value.reason == "UNEXPECTED_CONTINUATION", exc.value.reason


def test_B14_self_as_ai_phrasing_is_not_over_rejected():
    """收窄的 meta 族：身份块里 "我作为 AI 系统…" 是自我描述，不是收尾话术。"""
    payload = (
        "我作为长期在线的 AI 系统，把自己的边界守在源数据只读、生产不擅动这条线上。"
    )
    assert validate_identity_block(payload) == payload.strip()


# ══════════════════════════════════════════════════════════════════════════════
# C. Bounded generation
# ══════════════════════════════════════════════════════════════════════════════


def test_C1_identity_call_carries_the_bounded_cap(tmp_path, run_e1):
    """identity 生成必须走 per-call 预算，不能只靠后置 validator。"""
    base = tmp_path / "profile"
    base.mkdir()

    client = run_e1(base, _VALID_IDENTITY)

    cap = client.identity_call["max_output_tokens"]
    assert cap == e1_mod.IDENTITY_BLOCK_MAX_OUTPUT_TOKENS, (
        f"identity call max_output_tokens={cap!r}, expected "
        f"{e1_mod.IDENTITY_BLOCK_MAX_OUTPUT_TOKENS}"
    )
    print(f"\n[C1] identity max_output_tokens = {cap}")
    print(f"[C1] provider default (unchanged) = 131072")


def test_C2_the_cap_is_commensurate_with_real_legit_output():
    """cap 必须高于实测合法产物，否则会静默截断合法身份块（校验器查不出来）。"""
    cap = e1_mod.IDENTITY_BLOCK_MAX_OUTPUT_TOKENS
    n_chars = len(_VALID_IDENTITY.strip())

    # 中文 ≥1 token/字 → cap ≥ 字数 即保证这份量级的产物不被截断。
    assert cap >= n_chars * 1.3, (
        f"cap {cap} 对合法产物 {n_chars} 字余量不足（截断后的半句散文仍是"
        f"'合法形状'，会被写盘并注入）"
    )
    # 仍然是真正的"有界"：相比 provider 默认（131072）收窄两个数量级。
    assert cap < 131072 / 50, f"cap {cap} 相对默认预算收窄不足"


# ══════════════════════════════════════════════════════════════════════════════
# D. WRITE side — a rejected candidate never reaches the file
# ══════════════════════════════════════════════════════════════════════════════


def test_D1_rejected_candidate_writes_nothing(tmp_path, run_e1):
    base = tmp_path / "profile"
    base.mkdir()

    run_e1(base, _MARKER_PAYLOAD)

    assert not _identity_file(base).exists(), (
        "the rejected identity candidate was written to identity_block.md"
    )
    assert not (base / "identity_block.md.tmp").exists(), (
        "a temporary file was left behind by the rejected write"
    )
    for path in base.rglob("*"):
        if path.is_file():
            blob = path.read_bytes()
            assert b"]<]minimax[>[" not in blob, f"marker leaked into {path}"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_MARKER_PAYLOAD, id="template_marker"),
        pytest.param(_ROLE_CONTINUATION_PAYLOAD, id="role_continuation"),
        pytest.param(_MODEL_SELF_DESC_PAYLOAD, id="model_self_declaration"),
        pytest.param(_CONFIRMATION_PAYLOAD, id="confirmation_talk"),
        pytest.param(_OVERSIZED_PAYLOAD, id="oversized"),
    ],
)
def test_D2_writer_rejection_preserves_the_previous_file(tmp_path, run_e1, payload):
    """上一份已验证身份块必须逐字节存活 —— os.replace 绝不能看见坏候选。"""
    base = tmp_path / "profile"
    base.mkdir()
    good = _identity_file(base)
    good.write_text(_VALID_IDENTITY, encoding="utf-8")
    before_bytes = good.read_bytes()
    before_mtime = good.stat().st_mtime_ns

    run_e1(base, payload)

    assert good.read_bytes() == before_bytes, (
        "the previous validated identity block was overwritten"
    )
    assert good.stat().st_mtime_ns == before_mtime, "the good file was rewritten"

    # 读侧仍服务旧的好文件，绝不出现被拒内容。
    served = _bare_core(base)._compress_yin_to_identity()
    assert served == _VALID_IDENTITY.strip()
    assert "]<]minimax[>[" not in served
    assert "Qwen" not in served


def test_D3_valid_candidate_survives_a_write_then_read_round_trip(tmp_path, run_e1):
    base = tmp_path / "profile"
    base.mkdir()

    run_e1(base, _VALID_IDENTITY)

    assert _identity_file(base).exists(), "a valid candidate was not written"
    served = _bare_core(base)._compress_yin_to_identity()
    assert served == _VALID_IDENTITY.strip()
    assert served == validate_identity_block(served)


# ══════════════════════════════════════════════════════════════════════════════
# E. READ / INJECTION side — fail-closed
# ══════════════════════════════════════════════════════════════════════════════


def test_E1_reader_returns_empty_for_a_contaminated_file(tmp_path):
    """历史遗留的坏文件（早于修复）也绝不注入。"""
    base = tmp_path / "profile"
    base.mkdir()
    _identity_file(base).write_text(_MARKER_PAYLOAD, encoding="utf-8")

    core = _bare_core(base)
    served = core._compress_yin_to_identity()

    assert served == "", f"the reader injected a contaminated file: {served[:120]!r}"
    assert core._identity_block is None, (
        "a rejected read must not populate the injection cache"
    )


def test_E2_rejected_read_is_not_injected_by_the_injector(tmp_path):
    """端到端：注入面拿到的是空串（去重路径也不会注入）。"""
    base = tmp_path / "profile"
    base.mkdir()
    _identity_file(base).write_text(_MODEL_SELF_DESC_PAYLOAD, encoding="utf-8")

    injector = MemoryInjector(_bare_core(base))
    assert injector._get_identity() == ""
    assert injector._get_identity() == ""


def test_E3_missing_file_returns_empty(tmp_path):
    base = tmp_path / "profile"
    base.mkdir()
    core = _bare_core(base)
    assert core._compress_yin_to_identity() == ""
    assert core._identity_block is None
    assert list(base.iterdir()) == [], "the reader created files as a side effect"


def test_E4_whitespace_only_file_returns_empty(tmp_path):
    base = tmp_path / "profile"
    base.mkdir()
    _identity_file(base).write_text("   \n\n\t \n", encoding="utf-8")
    core = _bare_core(base)
    assert core._compress_yin_to_identity() == ""
    assert core._identity_block is None


def test_E5_unchanged_valid_file_is_served_from_cache(tmp_path):
    base = tmp_path / "profile"
    base.mkdir()
    _identity_file(base).write_text(_VALID_IDENTITY, encoding="utf-8")

    core = _bare_core(base)
    first = core._compress_yin_to_identity()
    second = core._compress_yin_to_identity()
    assert first == second == _VALID_IDENTITY.strip()


def test_E6_newer_but_invalid_file_must_not_serve_the_older_cached_block(tmp_path):
    """载重用例：好文件已入缓存 → 磁盘变成"更新但非法" → 必须返回 ""。"""
    base = tmp_path / "profile"
    base.mkdir()
    path = _identity_file(base)
    path.write_text(_VALID_IDENTITY, encoding="utf-8")

    core = _bare_core(base)
    first = core._compress_yin_to_identity()
    assert first == _VALID_IDENTITY.strip()
    assert core._identity_block is not None
    cached_mtime = core._identity_block_mtime

    path.write_text(_MARKER_PAYLOAD, encoding="utf-8")
    _mark_newer(path)
    assert path.stat().st_mtime > cached_mtime

    second = core._compress_yin_to_identity()
    assert second == "", (
        f"a newer-but-invalid file must yield '', got {second[:160]!r}"
    )
    assert _VALID_IDENTITY.strip() not in second, (
        "the reader served the stale cached block for an invalid file"
    )


def test_E7_cache_never_holds_a_value_that_fails_the_validator(tmp_path):
    base = tmp_path / "profile"
    base.mkdir()
    path = _identity_file(base)
    path.write_text(_VALID_IDENTITY, encoding="utf-8")

    core = _bare_core(base)
    core._compress_yin_to_identity()
    assert core._identity_block is not None
    assert validate_identity_block(core._identity_block) == core._identity_block.strip()

    path.write_text(_MARKER_PAYLOAD, encoding="utf-8")
    _mark_newer(path)
    assert core._compress_yin_to_identity() == ""
    if core._identity_block is not None:
        assert validate_identity_block(core._identity_block)


def test_E8_read_exception_does_not_serve_an_invalid_cache(tmp_path, monkeypatch):
    base = tmp_path / "profile"
    base.mkdir()
    path = _identity_file(base)
    path.write_text(_VALID_IDENTITY, encoding="utf-8")

    core = _bare_core(base)
    assert core._compress_yin_to_identity()  # populate a VALID cache

    # A stale, INVALID cached value must not survive a read exception.
    core._identity_block = _MARKER_PAYLOAD
    core._identity_block_mtime = 0.0

    real_read_text = Path.read_text

    def _boom(self, *args, **kwargs):
        if self == path:
            raise OSError("synthetic read failure")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)
    try:
        served = core._compress_yin_to_identity()
    finally:
        monkeypatch.setattr(Path, "read_text", real_read_text)

    assert served == "", f"an invalid cached block survived a read exception: {served[:120]!r}"


def test_E9_read_exception_may_serve_a_valid_cache(tmp_path, monkeypatch):
    base = tmp_path / "profile"
    base.mkdir()
    path = _identity_file(base)
    path.write_text(_VALID_IDENTITY, encoding="utf-8")

    core = _bare_core(base)
    assert core._compress_yin_to_identity()
    expected = _VALID_IDENTITY.strip()

    core._identity_block_mtime = 0.0
    _mark_newer(path)

    real_read_text = Path.read_text

    def _boom(self, *args, **kwargs):
        if self == path:
            raise OSError("synthetic read failure")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)
    try:
        served = core._compress_yin_to_identity()
    finally:
        monkeypatch.setattr(Path, "read_text", real_read_text)

    assert served == expected, "a valid cached block should still be usable"


# ══════════════════════════════════════════════════════════════════════════════
# F. Missing-file legacy 印 fallback — must pass the SAME validator
# ══════════════════════════════════════════════════════════════════════════════


def _write_yin(base: Path, text: str) -> Path:
    y_dir = base / "y"
    y_dir.mkdir(exist_ok=True)
    path = y_dir / "y_2026-09-21_040000.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_F1_legacy_fallback_is_served_only_after_passing_the_validator(tmp_path):
    base = tmp_path / "profile"
    base.mkdir()
    _write_yin(base, _VALID_YIN)

    core = _bare_core(base)
    served = core._compress_yin_to_identity()

    assert served, "the legacy fallback produced nothing for a valid 印"
    assert validate_identity_block(served) == served.strip(), (
        "the legacy fallback injected a block that does not pass the validator"
    )
    assert "我是谁" in served


def test_F2_legacy_fallback_is_rejected_when_it_cannot_pass(tmp_path):
    """兜底不得绕过注入边界：截断自印原文里带标记 → 返回 ""，不注入。"""
    base = tmp_path / "profile"
    base.mkdir()
    _write_yin(base, _YIN_WITH_MARKER)

    core = _bare_core(base)
    served = core._compress_yin_to_identity()

    assert served == "", f"the fallback injected unvalidated content: {served[:160]!r}"
    assert core._identity_block is None, "the rejected fallback must not be cached"

    # 端到端：注入面同样拿不到东西。
    assert MemoryInjector(core)._get_identity() == ""


def test_F3_legacy_fallback_is_not_used_when_identity_file_exists(tmp_path):
    base = tmp_path / "profile"
    base.mkdir()
    _write_yin(base, _YIN_WITH_MARKER)          # 兜底会失败
    _identity_file(base).write_text(_VALID_IDENTITY, encoding="utf-8")

    served = _bare_core(base)._compress_yin_to_identity()
    assert served == _VALID_IDENTITY.strip(), (
        "a valid identity_block.md must be used instead of the fallback"
    )


# ══════════════════════════════════════════════════════════════════════════════
# G. One implementation, called before the write / before the injection
# ══════════════════════════════════════════════════════════════════════════════


def _validator_call_lines(src: str, name: str) -> list[int]:
    tree = ast.parse(src)
    return sorted(
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == name
    )


def test_G1_e1_validates_identity_before_the_atomic_write():
    src = Path(e1_mod.__file__).read_text(encoding="utf-8")
    call_lines = _validator_call_lines(src, "validate_identity_block")
    assert call_lines, (
        "e1.py never calls validate_identity_block — the identity write gate "
        "is still only `if not identity_content: raise`"
    )
    lines = src.splitlines()
    replace_lines = [
        i + 1 for i, line in enumerate(lines)
        if "os.replace(identity_tmp_path, identity_path)" in line
    ]
    assert replace_lines, "the atomic identity write disappeared"
    assert min(call_lines) < min(replace_lines), (
        f"validator (line {min(call_lines)}) runs after the write "
        f"(line {min(replace_lines)})"
    )


def test_G2_e1_passes_the_identity_specific_cap_keyword():
    """源码级钉住：identity 调用的 cap 必须是**具名常量**，不是字面量。"""
    src = Path(e1_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    named_caps = [
        kw.value.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        for kw in n.keywords
        if kw.arg == "max_output_tokens" and isinstance(kw.value, ast.Name)
    ]
    assert "IDENTITY_BLOCK_MAX_OUTPUT_TOKENS" in named_caps, (
        f"the identity chat() call does not pass a named bounded cap; saw {named_caps}"
    )
    assert "SITUATION_OVERVIEW_MAX_OUTPUT_TOKENS" in named_caps
    # 全局默认不得被改动。
    import v3core.llm as llm_mod
    llm_text = Path(llm_mod.__file__).read_text(encoding="utf-8")
    assert "131072 if max_output_tokens is None" in llm_text, (
        "the provider default budget was changed — per-call caps must be additive"
    )


def test_G3_no_truncation_of_the_rejected_identity_candidate():
    src = Path(e1_mod.__file__).read_text(encoding="utf-8")
    assert "identity_content[:" not in src, (
        "the identity candidate is being truncated — fail-closed means reject "
        "whole, never keep a prefix"
    )


def test_G4_read_path_validates_both_the_file_and_the_fallback():
    """两个返回口都必须过闸：正常读文件 + 缺文件的 legacy 印兜底。"""
    core_src = Path(
        V3Core._compress_yin_to_identity.__code__.co_filename
    ).read_text(encoding="utf-8")
    lines = core_src.splitlines()
    calls = _validator_call_lines(core_src, "validate_identity_block")
    assert calls, "__init__.py never calls validate_identity_block"

    fallback_anchor = [
        i + 1 for i, line in enumerate(lines)
        if "E1 未生成身份块，用截断兜底" in line
    ]
    assert fallback_anchor, "the legacy 印 fallback disappeared"
    anchor = min(fallback_anchor)
    assert min(calls) < anchor, "the identity_block.md read path is not validated"
    assert max(calls) > anchor, (
        "the legacy 印 fallback path is not validated — the fallback would "
        "bypass the injection boundary"
    )


def test_G5_no_duplicate_validator_implementation():
    core_src = Path(
        V3Core._compress_yin_to_identity.__code__.co_filename
    ).read_text(encoding="utf-8")
    e1_src = Path(e1_mod.__file__).read_text(encoding="utf-8")
    for name, src in (("__init__.py", core_src), ("e1.py", e1_src)):
        assert "_IDENTITY_MAX_CHARS = " not in src, (
            f"{name} re-declares the identity length ceiling instead of importing it"
        )
        assert "_TEMPLATE_MARKER_PATTERNS" not in src, (
            f"{name} re-declares the marker shapes instead of importing them"
        )
