"""I01 — pi session parser (real v1.0.0 fixture, SDK-generated).

Contract under test:

  * host="pi"; session_id = header.id; event_id = entry.id (8-hex native).
  * Only role in {user, assistant} message entries.
  * custom_message entries (customType hippocampus-memory) excluded.
  * plain `custom` entries (non-message) skipped.
  * timestamps preserved (entry ISO + message ms).
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parent / "fixtures" / "i01" / "pi"
SESSION = FIX / (
    "--C--work--migration-project--"
    "/2026-10-02T17-31-41-982Z_01a0fdab-fc1d-74a7-80f2-31beea48a420.jsonl"
)


def _importer():
    mod = importlib.import_module("v3core.importers.pi_sessions")
    return mod.PiSessionImporter()


def test_parse_real_session():
    imp = _importer()
    imp.reset_excluded_counts()
    items = list(imp.parse(SESSION))
    assert len(items) == 6  # 3 user + 3 assistant
    roles = [it.role for it in items]
    assert roles.count("user") == 3
    assert roles.count("assistant") == 3
    assert all(it.host == "pi" for it in items)


def test_native_identity_preserved():
    imp = _importer()
    imp.reset_excluded_counts()
    items = list(imp.parse(SESSION))
    first_user = items[0]
    assert first_user.role == "user"
    assert first_user.event_id == "9921d832"  # native entry id, verbatim
    assert first_user.identity_kind == "native"
    assert (
        first_user.source_ref == "01a0fdab-fc1d-74a7-80f2-31beea48a420"
    )  # header.id
    assert "存储迁移" in first_user.text  # first user message in the fixture


def test_hippocampus_custom_message_excluded():
    imp = _importer()
    imp.reset_excluded_counts()
    items = list(imp.parse(SESSION))
    assert not any("hippocampus" in (it.text or "").lower() for it in items)
    assert not any("[长期记忆]" in (it.text or "") for it in items)
    assert imp.excluded_counts.get("skipped_injected", 0) == 1


def test_non_message_entries_skipped():
    imp = _importer()
    imp.reset_excluded_counts()
    list(imp.parse(SESSION))
    # the i01-fixture-marker custom entry
    assert imp.excluded_counts.get("skipped_entries", 0) == 1


def test_assistant_content_blocks_to_text():
    imp = _importer()
    imp.reset_excluded_counts()
    items = list(imp.parse(SESSION))
    answers = [it.text for it in items if it.role == "assistant"]
    assert any("盘位核对表-2026.xlsx" in a for a in answers)
    assert any("冻结在这一版" in a for a in answers)


def test_discover_finds_cwd_encoded_layout():
    imp = _importer()
    found = imp.discover(FIX)
    assert len(found) == 1
    assert found[0].name.endswith(".jsonl")
