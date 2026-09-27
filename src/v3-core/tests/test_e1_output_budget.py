# -*- coding: utf-8 -*-
"""test_e1_output_budget.py — pin the per-call output budget on the E1 系统态势总览.

Why this file exists (P0 incident):

    The scheduled E1 job asked the model for a 300-500 字 "系统态势总览" while
    ``LLMClient.chat`` sent ``max_completion_tokens: 131072``. The model overran
    its output contract, escaped the chat template (``]<]minimax[>[`` +
    a fabricated assistant turn) and the write path accepted it — its only gate
    was non-emptiness — atomically replacing the previous good file, which is
    then injected verbatim into every new session.

    Two gaps are pinned here, on the *writer* side:
      A. ``chat()`` had no way to express "this answer is short" (only the
         module-level 131072).
      B. the situation-overview call site therefore could not bound the model.

The load-bearing assertion is the CAPTURED REQUEST BODY: the cap must be visible
in the exact ``json=`` payload that would go on the wire, not merely present in
the source. ``requests.post`` is monkeypatched inside ``v3core.llm`` and the REAL
``LLMClient.chat`` path is driven with the REAL situation-overview call
arguments (prompt + budget imported from ``v3core.e1``, not re-typed here).

Hermetic: no network, no provider, no API key, no PostgreSQL, no real profile.
All text is synthetic — no user private content is reproduced anywhere.
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
import types
from pathlib import Path

import pytest
import requests
import requests.models

import v3core.llm as llm_mod
import v3core.e1 as e1_mod


# ──────────────────────────────────────────────────────────────────────────────
# Helpers — canned response, capturing transport, redaction
# ──────────────────────────────────────────────────────────────────────────────

_FAKE_KEY = "test-key-not-a-real-credential"
_CANNED_CONTENT = "## 活跃主题\n- 合成测试文本（非真实数据）。\n\n## 当前关注与待办\n- 无。\n\n## 状态速记\n- 正常。"

# Synthetic situation-overview user payload — the shape e1.py builds
# (印前 1000 字 + 活跃主题列表), filled with placeholder text only.
_SYNTHETIC_USER = (
    "## 最新印前 1000 字\n\n（合成占位文本，用于测试，不含任何真实内容。）\n\n"
    "## 最近 7 天活跃主题\n\n- [2026-01-01] 合成主题 A: 占位描述"
)

_SECRET_KEYS = {"authorization", "api_key", "apikey", "x-api-key"}


def _redact(obj):
    """Blank credential-bearing values. NOTE: only exact secret key names —
    ``max_completion_tokens`` must survive, it is the evidence."""
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if str(k).lower() in _SECRET_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


def _canned_response(content: str = _CANNED_CONTENT) -> requests.models.Response:
    """A real ``requests.Response`` shaped like an OpenAI-compatible 200."""
    resp = requests.models.Response()
    resp.status_code = 200
    resp.encoding = "utf-8"
    resp.headers["Content-Type"] = "application/json"
    resp._content = json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 111, "completion_tokens": 42},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    return resp


class _CapturingPost:
    """``requests.post`` replacement: records the exact ``json=`` body."""

    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def __call__(self, url, json=None, headers=None, timeout=None, proxies=None):
        self.calls.append(
            {"url": url, "json": json, "headers": headers,
             "timeout": timeout, "proxies": proxies}
        )
        return self._response

    @property
    def body(self) -> dict:
        assert self.calls, "requests.post was never called — no body captured"
        return self.calls[-1]["json"]


def _minimax_client() -> llm_mod.LLMClient:
    # NOTE: a raw *dict* config is read as ``config["llm"]`` by
    # ``LLMClient._extract_llm_cfg`` — a FLAT dict yields provider="" and
    # api_key="", so ``chat()`` raised ``credential_missing`` before ever
    # reaching the monkeypatched transport. The nesting below is the shape the
    # real config loader produces; the key is a dummy and never leaves memory.
    return llm_mod.LLMClient({
        "llm": {
            "provider": "minimax",
            "model": "M3-test",
            "api_key": _FAKE_KEY,
            "base_url": "https://llm.invalid/v1",
        }
    })


def _print_captured(title: str, cap: _CapturingPost) -> None:
    print(f"\n----- {title} -----")
    print("POST url:", cap.calls[-1]["url"])
    print("headers:", json.dumps(_redact(cap.calls[-1]["headers"]), ensure_ascii=False))
    print("json body:", json.dumps(_redact(cap.body), ensure_ascii=False, indent=2))
    print(f"----- end {title} -----")


# ──────────────────────────────────────────────────────────────────────────────
# 1. The wire proof — bounded call (REAL situation-overview arguments)
# ──────────────────────────────────────────────────────────────────────────────


def test_bounded_situation_overview_cap_is_on_the_wire(monkeypatch):
    """Drive the real chat() path with the real E1 prompt + real E1 budget and
    capture the body that WOULD be posted."""
    cap = _CapturingPost(_canned_response())
    monkeypatch.setattr(llm_mod.requests, "post", cap)

    out = _minimax_client().chat(
        e1_mod.SITUATION_OVERVIEW_PROMPT,
        [{"role": "user", "content": _SYNTHETIC_USER}],
        temperature=0.3,
        max_output_tokens=e1_mod.SITUATION_OVERVIEW_MAX_OUTPUT_TOKENS,
    )

    _print_captured("bounded call (E1 态势总览)", cap)
    body = cap.body

    assert out == _CANNED_CONTENT, "canned provider response must round-trip"
    # The whole point: the small cap is IN the request body.
    assert body["max_completion_tokens"] == e1_mod.SITUATION_OVERVIEW_MAX_OUTPUT_TOKENS, (
        f"cap missing from the wire: max_completion_tokens="
        f"{body.get('max_completion_tokens')!r}"
    )
    assert body["max_completion_tokens"] == 768
    assert body["max_completion_tokens"] != 131072, (
        "the historical 131072 default leaked into a bounded call"
    )
    # The deprecated field must NOT be what minimax receives.
    assert "max_tokens" not in body, (
        f"minimax must send max_completion_tokens, body has max_tokens="
        f"{body.get('max_tokens')!r}"
    )
    # The real situation-overview arguments reached the wire unchanged.
    assert body["messages"][0] == {
        "role": "system", "content": e1_mod.SITUATION_OVERVIEW_PROMPT,
    }
    assert body["messages"][1] == {"role": "user", "content": _SYNTHETIC_USER}
    assert body["temperature"] == 0.3
    assert body["thinking"] == {"type": "disabled"}


def test_unbounded_call_still_sends_131072(monkeypatch):
    """``None`` (i.e. every pre-existing caller) must be byte-for-byte the old
    behaviour: same field, same 131072 literal."""
    cap = _CapturingPost(_canned_response())
    monkeypatch.setattr(llm_mod.requests, "post", cap)

    _minimax_client().chat(
        "system prompt", [{"role": "user", "content": "hi"}], temperature=0.0
    )

    _print_captured("unbounded call (no budget passed)", cap)
    body = cap.body
    assert body["max_completion_tokens"] == 131072, (
        f"default changed for callers that pass nothing: "
        f"{body.get('max_completion_tokens')!r}"
    )
    assert "max_tokens" not in body


def test_chat_signature_default_is_none():
    """The additive guarantee, stated on the signature itself."""
    param = inspect.signature(llm_mod.LLMClient.chat).parameters["max_output_tokens"]
    assert param.default is None


def test_direct_private_call_keeps_default(monkeypatch):
    """``_chat_minimax`` called directly (existing test style, 3 positional
    args) must still build the 131072 body."""
    cap = _CapturingPost(_canned_response())
    monkeypatch.setattr(llm_mod.requests, "post", cap)
    _minimax_client()._chat_minimax("s", [{"role": "user", "content": "hi"}], 0.0)
    assert cap.body["max_completion_tokens"] == 131072


# ──────────────────────────────────────────────────────────────────────────────
# 2. Provider mapping is internal — openai path uses "max_tokens"
# ──────────────────────────────────────────────────────────────────────────────


class _FakeCompletions:
    def __init__(self, sink):
        self._sink = sink

    def create(self, **kwargs):
        self._sink.append(kwargs)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content="ok", reasoning_content=None))],
            usage=types.SimpleNamespace(prompt_tokens=7, completion_tokens=3),
        )


def _install_fake_openai(monkeypatch, sink):
    fake_mod = types.ModuleType("openai")

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            self._kwargs = kwargs
            self.chat = types.SimpleNamespace(completions=_FakeCompletions(sink))

    fake_mod.OpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_mod)


def _openai_client() -> llm_mod.LLMClient:
    # See _minimax_client: dict configs are read as ``config["llm"]``.
    return llm_mod.LLMClient({
        "llm": {
            "provider": "openai",
            "model": "gpt-test",
            "api_key": _FAKE_KEY,
            "base_url": "https://llm.invalid/v1",
        }
    })


def test_openai_path_maps_budget_to_max_tokens(monkeypatch):
    sink: list[dict] = []
    _install_fake_openai(monkeypatch, sink)

    out = _openai_client().chat(
        e1_mod.SITUATION_OVERVIEW_PROMPT,
        [{"role": "user", "content": _SYNTHETIC_USER}],
        temperature=0.3,
        max_output_tokens=e1_mod.SITUATION_OVERVIEW_MAX_OUTPUT_TOKENS,
    )

    print("\n----- openai call kwargs (bounded) -----")
    print(json.dumps(_redact(sink[-1]), ensure_ascii=False, indent=2))
    assert out == "ok"
    assert sink[-1]["max_tokens"] == 768, (
        f"openai path must map the budget to max_tokens, got "
        f"{sink[-1].get('max_tokens')!r}"
    )
    assert "max_completion_tokens" not in sink[-1], (
        "the minimax field name leaked into the openai path"
    )


def test_openai_path_default_unchanged(monkeypatch):
    sink: list[dict] = []
    _install_fake_openai(monkeypatch, sink)
    _openai_client().chat("s", [{"role": "user", "content": "hi"}], temperature=0.0)
    assert sink[-1]["max_tokens"] == 131072
    assert sink[-1]["max_tokens"] == _openai_client()._max_tokens


# ──────────────────────────────────────────────────────────────────────────────
# 3. Validation — local, pre-network, and never silent
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [0, -1, -4096])
def test_rejects_non_positive_budget(bad):
    with pytest.raises(ValueError, match="max_output_tokens"):
        llm_mod.validate_output_tokens(bad)


def test_rejects_absurd_budget():
    with pytest.raises(ValueError, match="合理上限"):
        llm_mod.validate_output_tokens(1_000_001)


@pytest.mark.parametrize("bad", [True, False, 768.0, "768", [768], object()])
def test_rejects_non_int_budget(bad):
    with pytest.raises(TypeError, match="max_output_tokens"):
        llm_mod.validate_output_tokens(bad)


def test_accepts_none_and_valid_int():
    assert llm_mod.validate_output_tokens(None) is None
    assert llm_mod.validate_output_tokens(1) == 1
    assert llm_mod.validate_output_tokens(768) == 768
    assert llm_mod.validate_output_tokens(1_000_000) == 1_000_000


def test_bad_budget_fails_before_any_request(monkeypatch):
    """A caller bug must not become a network call."""
    cap = _CapturingPost(_canned_response())
    monkeypatch.setattr(llm_mod.requests, "post", cap)

    with pytest.raises(ValueError, match="max_output_tokens"):
        _minimax_client().chat(
            "s", [{"role": "user", "content": "hi"}], temperature=0.0,
            max_output_tokens=0,
        )
    assert cap.calls == [], "validation must fire BEFORE requests.post"


# ──────────────────────────────────────────────────────────────────────────────
# 4. The E1 call site actually forwards the constant (source-level pin)
# ──────────────────────────────────────────────────────────────────────────────


def _e1_situation_overview_chat_calls() -> list[ast.Call]:
    tree = ast.parse(Path(e1_mod.__file__).read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "chat"):
            continue
        if node.args and isinstance(node.args[0], ast.Name) \
                and node.args[0].id == "situation_overview_prompt":
            found.append(node)
    return found


def test_e1_call_site_passes_the_budget_constant():
    calls = _e1_situation_overview_chat_calls()
    assert len(calls) == 1, (
        f"expected exactly one situation-overview chat call, found {len(calls)}"
    )
    call = calls[0]
    kw = {k.arg: k.value for k in call.keywords}
    assert "max_output_tokens" in kw, (
        "the situation-overview call site does not pass max_output_tokens — "
        "the cap exists on the client but never reaches E1"
    )
    value = kw["max_output_tokens"]
    assert isinstance(value, ast.Name) and value.id == "SITUATION_OVERVIEW_MAX_OUTPUT_TOKENS", (
        f"budget must be the named module constant, got {ast.dump(value)}"
    )
    assert isinstance(kw.get("temperature"), ast.Constant) and kw["temperature"].value == 0.3


def test_e1_budget_constant_is_small_and_matches_the_pin():
    assert e1_mod.SITUATION_OVERVIEW_MAX_OUTPUT_TOKENS == 768
    assert llm_mod.validate_output_tokens(
        e1_mod.SITUATION_OVERVIEW_MAX_OUTPUT_TOKENS
    ) == 768
    # Order-of-magnitude claim: the cap must be ~2 orders below the default.
    assert e1_mod.SITUATION_OVERVIEW_MAX_OUTPUT_TOKENS * 100 < 131072


# ──────────────────────────────────────────────────────────────────────────────
# 5. The measurement behind 768 (skipped when no tokenizer is reachable)
# ──────────────────────────────────────────────────────────────────────────────


def _contract_ceiling_tokens() -> int | None:
    """Tokens for a 500-字 Chinese body under the conservative offline proxy."""
    try:
        import tiktoken
    except Exception:
        return None
    try:
        enc = tiktoken.get_encoding("cl100k_base")
    except Exception:  # no BPE file cached and no network
        return None
    body = ("系统近期持续推进记忆系统的检索链路重构，已完成分块索引与召回评估的对接，"
            "并在隔离环境验证了写入路径的原子性。安装器分支补齐了回滚与升级审计记录，"
            "测试主机已跑通验收用例，未发现新的越权访问面。运维侧完成了日志与凭据边界的收敛，"
            "下一步需要确认多版本共存时的导入顺序，避免旧包覆盖新包后仍被加载。"
            "整体链路健康，无阻塞项，最近一次同步距今约两天，无异常告警。")
    return len(enc.encode((body * 3)[:500]))


def test_cap_fits_a_full_length_summary():
    """768 must clear the worst-case 500-字 contract output with headroom."""
    n = _contract_ceiling_tokens()
    if n is None:
        pytest.skip("no offline tokenizer (tiktoken cl100k_base) available")
    print(f"\n500 字 Chinese body = {n} tokens (tiktoken cl100k_base)")
    cap = e1_mod.SITUATION_OVERVIEW_MAX_OUTPUT_TOKENS
    assert n < cap, f"cap {cap} cannot fit a 500 字 summary ({n} tokens)"
    assert cap / n >= 1.25, (
        f"cap {cap} leaves only {cap - n} tokens over the worst-case contract "
        f"output ({n}); too tight — a truncated file still passes the "
        f"non-emptiness write gate"
    )
