"""A02 behaviour tests for the query embedding seam.

P2-1: a cold query must reach the semantic lane, and a repeated query must be served by the cache.
P2-2: a query past the provider window must be reduced token-safely (head 60% / tail 40%) while a
query inside the window stays byte-for-byte identical.

Deterministic by construction: token accounting uses the planner's explicit ``char_estimate``
backend (2 chars per token) and no HTTP call is ever made.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import v3core
import v3core.embedding as emb
import v3core.recall_pool as recall_pool_mod
from v3core import V3Core
from v3core.embed_chunks import (
    QUERY_IDENTITY_STRATEGY,
    QUERY_OMISSION_SEPARATOR,
    QUERY_TRUNCATION_STRATEGY,
    _get_token_counter,
    prepare_query_embedding_text,
    safe_token_target,
)
from v3core.embedding import build_embed_cfg, call_query_embedding

CONFIG = {
    "storage": {
        "embed": {
            "endpoint": "http://127.0.0.1:9/v1/embeddings",
            "model": "BAAI/bge-m3",
            "apiKey": "behaviour-test-placeholder",
        }
    }
}
EMBED_CFG = build_embed_cfg(CONFIG)
TARGET = safe_token_target(EMBED_CFG)
COUNTER = _get_token_counter(EMBED_CFG, tokenizer_override="char_estimate")


def marked_query(total_tokens: int, tag: str) -> str:
    """char_estimate payload of exactly ``total_tokens`` tokens (2 chars per token).

    The head window keeps roughly the first 60% of the characters and the tail window the last
    40%, so a long query's dropped region starts around 51%. Putting the MIDDLE marker at 58%
    places it inside that dropped region, while HEAD (0%) and TAIL (end) must survive.
    """
    total_chars = total_tokens * 2
    head, middle, tail = f"HEAD{tag}", f"MIDDLE{tag}", f"TAIL{tag}"
    filler = total_chars - len(head) - len(middle) - len(tail)
    assert filler >= 0, "fixture needs a larger token budget"
    pre_mid = int(total_chars * 0.58) - len(head)
    post_mid = filler - pre_mid
    assert pre_mid >= 0 and post_mid >= 0, "fixture too short for the marker layout"
    return head + ("m" * pre_mid) + middle + ("x" * post_mid) + tail


class _FakeResponse:
    def __init__(self, vector):
        self.status_code = 200
        self._vector = vector

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": [{"embedding": self._vector}]}


@pytest.fixture()
def transport(monkeypatch):
    """Count provider calls without opening a socket; each call returns a distinct vector."""
    calls: list[str] = []

    def _post(url, json=None, headers=None, proxies=None, timeout=None):
        calls.append(json["input"])
        return _FakeResponse([float(len(calls))] * 1024)

    monkeypatch.setattr(emb.requests, "post", _post, raising=True)
    monkeypatch.setattr(emb, "_EMBED_CACHE", {}, raising=False)
    return calls


# ── P2-2: representation contract ────────────────────────────────────────────

def test_short_query_is_byte_identical():
    query = "普通的短查询 with English 和标点，以及\n换行。"
    out = prepare_query_embedding_text(query, EMBED_CFG, tokenizer_override="char_estimate")
    assert out.text == query
    assert out.truncated is False
    assert out.strategy == QUERY_IDENTITY_STRATEGY
    assert out.prepared_tokens == out.original_tokens == COUNTER(query)


@pytest.mark.parametrize("delta", [-1, 0])
def test_boundary_at_or_below_target_is_unchanged(delta):
    query = marked_query(TARGET + delta, "B")
    out = prepare_query_embedding_text(query, EMBED_CFG, tokenizer_override="char_estimate")
    assert COUNTER(query) <= TARGET
    assert out.text == query
    assert out.truncated is False


def test_boundary_above_target_is_capped_and_keeps_both_ends():
    query = marked_query(TARGET + 1, "B")
    assert COUNTER(query) > TARGET
    out = prepare_query_embedding_text(query, EMBED_CFG, tokenizer_override="char_estimate")
    assert out.truncated is True
    assert out.strategy == QUERY_TRUNCATION_STRATEGY
    assert out.prepared_tokens <= TARGET
    assert COUNTER(out.text) <= TARGET
    assert out.text.startswith("HEADB")
    assert out.text.endswith("TAILB")
    assert QUERY_OMISSION_SEPARATOR in out.text


def test_9001_token_query_is_capped_keeps_both_ends_and_is_deterministic():
    query = marked_query(9001, "L")
    assert COUNTER(query) > TARGET
    first = prepare_query_embedding_text(query, EMBED_CFG, tokenizer_override="char_estimate")
    second = prepare_query_embedding_text(query, EMBED_CFG, tokenizer_override="char_estimate")
    assert first.text == second.text, "the same query must map to one stable representation"
    assert first.prepared_tokens <= TARGET
    assert COUNTER(first.text) <= TARGET
    assert first.text.startswith("HEADL")
    assert first.text.endswith("TAILL")
    assert "MIDDLEL" not in first.text, "the omitted middle must not survive"
    head_part, _, tail_part = first.text.partition(QUERY_OMISSION_SEPARATOR)
    assert len(head_part) > len(tail_part), "head must keep the larger share (60/40)"
    assert len(tail_part) > 0


def test_original_query_is_never_mutated():
    query = marked_query(9001, "I")
    snapshot = str(query)
    prepare_query_embedding_text(query, EMBED_CFG, tokenizer_override="char_estimate")
    assert query == snapshot


def test_source_paths_do_not_use_the_query_seam():
    """The seam is query-only: source embedding paths must not reference it."""
    src = Path(v3core.__file__).parent
    assert "prepare_query_embedding_text" in (src / "embed_chunks.py").read_text(encoding="utf-8")
    for source_module in ("ingest.py", "observer.py", "e1.py", "yin_pool.py"):
        text = (src / source_module).read_text(encoding="utf-8")
        assert "prepare_query_embedding_text" not in text, f"{source_module} must not cap source text"
        assert "call_query_embedding" not in text, f"{source_module} must not use the query seam"


# ── cache: cold / hot ────────────────────────────────────────────────────────

def test_cold_then_hot_long_query_uses_cache(transport):
    query = marked_query(9001, "C")
    v1 = call_query_embedding(query, EMBED_CFG, cache=True, tokenizer_override="char_estimate")
    assert len(transport) == 1, "the first (cold) call must reach the provider once"
    assert COUNTER(transport[0]) <= TARGET
    v2 = call_query_embedding(query, EMBED_CFG, cache=True, tokenizer_override="char_estimate")
    assert len(transport) == 1, "a repeated identical query must be served by the cache"
    assert v1 == v2


def test_short_query_cold_then_hot(transport):
    query = "冷查询 first call"
    call_query_embedding(query, EMBED_CFG, cache=True, tokenizer_override="char_estimate")
    assert transport == [query]
    call_query_embedding(query, EMBED_CFG, cache=True, tokenizer_override="char_estimate")
    assert transport == [query]


# ── P2-1: search_cards reaches the semantic lane ─────────────────────────────

def _bare_core():
    core = V3Core.__new__(V3Core)
    core.config = CONFIG
    core._pg = SimpleNamespace(is_connected=lambda: False)
    core._store = SimpleNamespace(get_index=lambda: {"files": {}}, sqlite=None)
    core._pg_was_connected = False
    return core


def test_search_cards_passes_q_emb_to_recall_pool(monkeypatch):
    seen = {}
    monkeypatch.setattr(v3core, "call_query_embedding",
                        lambda text, cfg, **kw: [0.5] * 1024, raising=True)

    def _recall(query, **kw):
        seen["query"] = query
        seen["q_emb"] = kw.get("q_emb")
        return [], {"lanes": {}}

    monkeypatch.setattr(recall_pool_mod, "recall_pool", _recall, raising=True)
    _bare_core().search_cards("冷启动的语义查询")
    assert seen["q_emb"] is not None, "cold query must reach recall_pool with a vector"


def test_search_cards_degrades_on_provider_failure(monkeypatch):
    def _boom(*a, **kw):
        raise emb.EmbeddingCallError("provider down")

    monkeypatch.setattr(v3core, "call_query_embedding", _boom, raising=True)
    seen = {}

    def _recall(query, **kw):
        seen["q_emb"] = kw.get("q_emb")
        return [], {"lanes": {}}

    monkeypatch.setattr(recall_pool_mod, "recall_pool", _recall, raising=True)
    result = _bare_core().search_cards("provider 挂了也要能搜")
    assert seen["q_emb"] is None
    assert result == []


def test_search_cards_reraises_value_error(monkeypatch):
    def _bad_cfg(*a, **kw):
        raise ValueError("embed endpoint 未配置")

    monkeypatch.setattr(v3core, "call_query_embedding", _bad_cfg, raising=True)
    monkeypatch.setattr(recall_pool_mod, "recall_pool", lambda *a, **kw: ([], {}), raising=True)
    with pytest.raises(ValueError):
        _bare_core().search_cards("配置错误必须上抛")
