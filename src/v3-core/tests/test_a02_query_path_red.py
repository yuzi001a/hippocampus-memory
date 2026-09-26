"""RED contract tests for the A02 query-path defects — recorded, not yet fixed.

Companion to the A01 audit (`evidence/embedding-input-window-audit-v2.final.json`). Two defects
were classified P2 and are the whole scope of A02:

**Defect 1 — `SEARCH_CARDS_EMBED_CACHE_INVERTED_CONDITION`**
`V3Core.search_cards` computes a query embedding only when the query string is *already* in
`_EMBED_CACHE` (`v3core/__init__.py:1979`). A cold query therefore reaches `recall_pool` with
`q_emb=None` and loses the semantic lane outright. The same guard was already removed from the
prefetch path (`v3core/__init__.py:2474`), so prefetch and the tool surface disagree today.

**Defect 2 — `QUERY_NEEDS_CAP`**
Query-bearing embedding call sites hand the raw query string to the provider. The provider window
is 8192 tokens (empirical: `HTTP 400 code=20015` at 9001 synthetic tokens, 8001 OK), while the
longest real production query is 9134 tokens. An over-window query fails `call_embedding`
(fail-closed, deterministic 400 → no retry) and the caller silently degrades to keyword-only
recall. Query text is a retrieval expression, not durable source: the agreed direction is a
token-safe query cap with head+tail retention — never chunked queries, never silent truncation of
a durable source (A02 design gate).

Both tests are RED on the A01 baseline by design. The RED run log is preserved as evidence; they
become GREEN when the A02 fix lands. Do not weaken an assertion to make the baseline green — that
would erase the defect this file exists to pin.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import v3core
import v3core.recall_pool as recall_pool_mod
from v3core import V3Core

CONFIG = {
    "storage": {
        "embed": {
            "endpoint": "http://127.0.0.1:9/v1/embeddings",
            "model": "BAAI/bge-m3",
            "apiKey": "red-test-placeholder",
        }
    }
}


def _bare_core() -> V3Core:
    """A Core with the attributes `search_cards` touches — no profile, no PG, no network.

    ``pg`` / ``store`` are lazy read-only properties, so they are backed by fakes through their
    private attributes rather than assigned.
    """
    core = V3Core.__new__(V3Core)
    core.config = CONFIG
    core._pg = SimpleNamespace(is_connected=lambda: False)
    core._store = SimpleNamespace(get_index=lambda: {"files": {}}, sqlite=None)
    core._pg_was_connected = False
    return core


@pytest.fixture()
def captured(monkeypatch):
    """Capture the text handed to the query embedding seam; stub the recall pool."""
    calls: list[str] = []

    def _fake_call_embedding(text, *args, **kwargs):
        calls.append(text)
        return [0.0] * 1024

    monkeypatch.setattr(v3core, "call_embedding", _fake_call_embedding, raising=True)
    monkeypatch.setattr(recall_pool_mod, "recall_pool",
                        lambda *a, **kw: ([], {"lanes": {}}), raising=True)
    monkeypatch.setattr(v3core, "_EMBED_CACHE", {}, raising=False)
    return calls


def test_red1_search_cards_cold_query_must_attempt_query_embedding(captured):
    """A cold query must still get a semantic embedding (defect 1, RED)."""
    query = "一个从未缓存过的新查询" + "z" * 32
    _bare_core().search_cards(query)
    assert captured == [query], (
        "search_cards skipped the query embedding because the query was not already in "
        "_EMBED_CACHE (inverted condition, __init__.py:1979); the semantic lane is lost on every "
        "cold query"
    )


def test_red2_no_query_bearing_call_site_passes_the_raw_query(captured):
    """Defect 2 (structural): every query-bearing embedding call must pass a capped expression.

    The provider window is 8192 tokens; a raw pasted query can exceed it and the call fails closed
    into keyword-only recall. This test pins the *call sites* rather than a cap size, so the A02
    design keeps its freedom (cap constant, head+tail helper, or a dedicated seam).
    """
    source = (Path(v3core.__file__).parent / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "call_embedding" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name) and first.id == "query":
            offenders.append(node.lineno)

    assert not offenders, (
        "query embedding call sites hand the raw `query` to the provider without a token-safe "
        f"cap: v3core/__init__.py lines {sorted(offenders)}. The window is 8192 tokens and the "
        "longest real production query is 9134 tokens."
    )
