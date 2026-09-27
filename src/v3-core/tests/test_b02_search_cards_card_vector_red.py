"""RED regression: V3Core.search_cards must enable the card-vector lane (B02).

Root cause: ``V3Core.search_cards`` called ``recall_pool(...)`` without
``include_card_vector=True`` (``recall_pool`` defaults it to ``False``), so
``v3_search`` over ``explicit_memories`` only ran the keyword lane and never
the existing ``ActiveMemoryReader.search_vector`` lane.
``V3Core.prefetch`` already passes ``include_card_vector=True``.

This test does NOT grep source text: it runs the real ``search_cards()``
with fake core state + a fake ``recall_pool`` and asserts on the captured
call kwargs, plus proves a returned ``RecallHit(kind='active_memory')``
surfaces in the results.
"""
from __future__ import annotations

from types import SimpleNamespace

import v3core
import v3core.recall_pool as recall_pool_mod
from v3core import V3Core
from v3core.types import RecallHit

CONFIG = {
    "storage": {
        "embed": {
            "endpoint": "http://127.0.0.1:9/v1/embeddings",
            "model": "BAAI/bge-m3",
            "apiKey": "red-test-placeholder",
        }
    }
}

CANARY_SOURCE_ID = "am-canary-001"


def _bare_core() -> V3Core:
    """A Core with the attributes `search_cards` touches — no profile, no PG, no network."""
    core = V3Core.__new__(V3Core)
    core.config = CONFIG
    core._pg = SimpleNamespace(is_connected=lambda: False)
    core._store = SimpleNamespace(get_index=lambda: {"files": {}}, sqlite=None)
    core._pg_was_connected = False
    return core


def _install_fake(monkeypatch) -> dict:
    """Stub the embedding seam + recall_pool; capture recall_pool kwargs."""
    captured: dict = {}

    def _fake_recall_pool(query, **kwargs):
        captured.update(kwargs)
        captured["_query"] = query
        hit = RecallHit(
            source_id=CANARY_SOURCE_ID,
            title="B02 canary fact",
            content_preview="canary semantic content",
            category="explicit_memory",
            tags=[],
            cosine=0.99,
            rrf_score=9.9,
            kind="active_memory",
            created_at="",
        )
        return [hit], False

    def _fake_call_query_embedding(text, *args, **kwargs):
        return [0.0] * 8

    monkeypatch.setattr(v3core, "call_query_embedding",
                        _fake_call_query_embedding, raising=True)
    monkeypatch.setattr(recall_pool_mod, "recall_pool",
                        _fake_recall_pool, raising=True)
    return captured


def test_search_cards_enables_card_vector_lane(monkeypatch):
    """search_cards must call recall_pool with include_card_vector=True."""
    captured = _install_fake(monkeypatch)
    _bare_core().search_cards("canary semantic query")
    assert captured.get("include_card_vector") is True, (
        "search_cards called recall_pool without include_card_vector=True "
        f"(got {captured.get('include_card_vector')!r}); the active-memory "
        "vector lane (ActiveMemoryReader.search_vector) never runs and "
        "v3_search degrades to keyword-only"
    )


def test_search_cards_surfaces_active_memory_hit(monkeypatch):
    """A RecallHit(kind='active_memory') from recall_pool must reach results."""
    _install_fake(monkeypatch)
    out = _bare_core().search_cards("canary semantic query")
    assert any(r.get("source_id") == CANARY_SOURCE_ID for r in out), (
        f"active_memory hit {CANARY_SOURCE_ID} was dropped by search_cards; "
        f"got {out!r}"
    )
