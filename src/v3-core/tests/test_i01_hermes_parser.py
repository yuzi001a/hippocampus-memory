"""I01 — Hermes importer upgrade: F2 identity + exclusion rules.

Fixture: tests/fixtures/i01/hermes/state.db (synthetic rows on the real
schema — see fixtures README). Contract under test:

  * host="hermes", event_id = native messages.id, identity_kind="native".
  * Kept: user/assistant rows with real text — including steer rows,
    archived rows (active=0/compacted=1) and folded rows (0/0).
  * Excluded: system-generated pseudo messages (compaction summaries,
    [System note: …]), tool/other roles, empty content, display_kind in
    {hidden, auto_continue, async_delegation_complete, process_complete,
    model_switch, failed_turn}.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parent / "fixtures" / "i01" / "hermes"
DB = FIX / "state.db"


def _importer():
    mod = importlib.import_module("v3core.importers.hermes_sessions")
    return mod.HermesSessionImporter()


def test_hermes_exclusions_and_counts():
    imp = _importer()
    imp.reset_excluded_counts()
    items = list(imp.parse(DB))
    assert len(items) == 9
    counts = imp.excluded_counts
    assert counts.get("skipped_system_text", 0) == 2
    assert counts.get("skipped_roles", 0) == 1
    assert counts.get("skipped_empty", 0) == 1
    assert counts.get("skipped_display_kind", 0) == 5


def test_hermes_native_identity_and_host():
    imp = _importer()
    imp.reset_excluded_counts()
    items = list(imp.parse(DB))
    assert all(it.host == "hermes" for it in items)
    assert all(it.identity_kind == "native" for it in items)
    assert all(it.event_id and str(it.event_id).isdigit() for it in items)
    first = items[0]
    assert first.event_id == "1"
    assert first.source_ref == "sess-ginkgo-a"
    assert "银杏" in first.text


def test_hermes_keeps_steer_and_archived_rows():
    imp = _importer()
    imp.reset_excluded_counts()
    items = list(imp.parse(DB))
    texts = [it.text for it in items]
    assert any("先停一下" in t for t in texts)  # steer kept
    assert any("盘位表更新到第 3 版" in t for t in texts)  # archived kept
    roles = {it.role for it in items}
    assert roles == {"user", "assistant"}


def test_hermes_never_emits_system_pseudo_messages():
    imp = _importer()
    imp.reset_excluded_counts()
    items = list(imp.parse(DB))
    joined = "\n".join(it.text for it in items)
    assert "[CONTEXT COMPACTION" not in joined
    assert "[System note:" not in joined
    assert "ASYNC DELEGATION" not in joined
    assert "Background process" not in joined
    assert "not processed" not in joined
