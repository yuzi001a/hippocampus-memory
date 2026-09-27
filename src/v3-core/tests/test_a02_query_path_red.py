"""RED contract tests for the A02 query-path defects — now GREEN after the A02 fix.

Companion to the A01 audit (`evidence/embedding-input-window-audit-v2.final.json`). Two defects
were classified P2 and are the whole scope of A02:

**Defect 1 — `SEARCH_CARDS_EMBED_CACHE_INVERTED_CONDITION`**
`V3Core.search_cards` computed a query embedding only when the query string was *already* in
`_EMBED_CACHE` (`v3core/__init__.py:1979`), so a cold query reached `recall_pool` with `q_emb=None`
and lost the semantic lane. The same guard had already been removed from the prefetch path.

**Defect 2 — `QUERY_NEEDS_CAP`**
Query-bearing embedding call sites handed the raw query string to the provider. The provider window
is 8192 tokens (empirical: `HTTP 400 code=20015` at 9001 synthetic tokens, 8001 OK) while the longest
real production query is 9134 tokens, so such a query failed closed and the caller degraded to
keyword-only recall. Query text is a retrieval expression, not durable source: the fix is a
token-safe representation (head 60% / tail 40%) behind a single query-only seam.

These two tests were RED before the A02 fix and are GREEN after it, with the same assertions — the
fix was made to satisfy them, never the other way round.
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

    monkeypatch.setattr(v3core, "call_query_embedding", _fake_call_embedding, raising=True)
    monkeypatch.setattr(recall_pool_mod, "recall_pool",
                        lambda *a, **kw: ([], {"lanes": {}}), raising=True)
    monkeypatch.setattr(v3core, "_EMBED_CACHE", {}, raising=False)
    return calls


def test_red1_search_cards_cold_query_must_attempt_query_embedding(captured):
    """A cold query must still get a semantic embedding (defect 1)."""
    query = "一个从未缓存过的新查询" + "z" * 32
    _bare_core().search_cards(query)
    assert captured == [query], (
        "search_cards skipped the query embedding because the query was not already in "
        "_EMBED_CACHE (inverted condition, __init__.py:1979); the semantic lane is lost on every "
        "cold query"
    )


def test_red2_no_query_bearing_call_site_passes_the_raw_query(captured):
    """Defect 2 (structural): query embeddings must go through the one query seam.

    Rejected here:

      * ``call_embedding(<query-derived text>)`` — neither the raw query nor a private ``query[:N]``
        slice may be handed to the provider directly;
      * ``prepared_query…`` text passed to ``call_embedding`` would hide the cap from this guard.

    ``call_embedding`` remains legitimate for durable source text.
    """
    source = (Path(v3core.__file__).parent / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    def _is_query_derived(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id == "query" or node.id.startswith("prepared_query")
        if isinstance(node, ast.Subscript):
            return _is_query_derived(node.value)
        return False

    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == "call_embedding" and _is_query_derived(node.args[0]):
            offenders.append(node.lineno)

    assert not offenders, (
        "query text is handed to the provider outside the query seam: "
        f"v3core/__init__.py lines {sorted(offenders)}. Query embeddings must call "
        "call_query_embedding (token-safe representation), never call_embedding with a query "
        "argument."
    )
