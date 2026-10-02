"""I01 — DSH session parser (real v4 fixtures from the installed CLI).

Fixtures: tests/fixtures/i01/dsh/ (see fixtures README for provenance).
Contract under test:

  * host="dsh"; session_id = header.id; event_id = native MessageId.
  * user/message with source.kind=="user" only (hippocampus / runtime-context
    / skill-catalog injections excluded structurally).
  * assistant/message kept unless data.interrupted is True.
  * text blocks only for v1; non-text blocks counted as skipped.
  * timestamps preserved (epoch ms → ISO8601).
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parent / "fixtures" / "i01" / "dsh"
S1 = FIX / (
    "--C--hp-testbed-b04-integration-20261002-runs-b04-integration-run-1-project--"
    "/session-60c82e3f-84fe-4f53-94c3-72779343a9a0/session.v4.jsonl.zstd"
)
S2 = FIX / (
    "--C--hp-testbed-b04-integration-20261002-runs-b04-integration-run-1-project--"
    "/session-9a48a9ec-0280-468e-b249-dd405f68ad93/session.v4.jsonl.zstd"
)


def _importer():
    mod = importlib.import_module("v3core.importers.dsh_sessions")
    return mod.DshSessionImporter()


def test_parse_real_single_turn_session():
    imp = _importer()
    imp.reset_excluded_counts()
    items = list(imp.parse(S1))
    assert len(items) == 2
    assert {it.role for it in items} == {"user", "assistant"}
    assert all(it.host == "dsh" for it in items)
    # native identity
    user = next(it for it in items if it.role == "user")
    assert user.event_id == "a8a64ad8-9380-47c1-a57f-287eb7c10ca2"
    assert user.identity_kind == "native"
    assert user.source_ref == "session-60c82e3f-84fe-4f53-94c3-72779343a9a0"
    assert "b04-isolated-marker-7f3a" in user.text
    # epoch-ms → ISO
    assert user.occurred_at and user.occurred_at.startswith("20")
    # injected user messages excluded (runtime-context, skill-catalog)
    assert imp.excluded_counts.get("skipped_injected", 0) == 2


def test_parse_session_with_hippocampus_injection_excluded():
    imp = _importer()
    imp.reset_excluded_counts()
    items = list(imp.parse(S2))
    # 2 real users ("Please confirm…", "Continue.") + 2 assistants
    assert len(items) == 4
    texts = [it.text for it in items if it.role == "user"]
    assert any("Continue." in t for t in texts)
    assert not any("hippocampus" in t.lower() for t in texts)
    # hippocampus + runtime-context + skill-catalog excluded
    assert imp.excluded_counts.get("skipped_injected", 0) == 3


def test_assistant_native_id_and_interrupted_field():
    imp = _importer()
    imp.reset_excluded_counts()
    items = list(imp.parse(S2))
    assistants = [it for it in items if it.role == "assistant"]
    assert len(assistants) == 2
    for a in assistants:
        assert a.event_id  # native MessageId present
        assert a.identity_kind == "native"
        assert "acknowledged b04-isolated-marker-7f3a" in a.text


def test_discover_finds_nested_layout():
    imp = _importer()
    found = imp.discover(FIX)
    assert len(found) == 2
    assert all(p.name == "session.v4.jsonl.zstd" for p in found)


def test_multi_frame_zstd_read_across_frames():
    """The real files are concatenated zstd frames — must read all of them."""
    imp = _importer()
    items = list(imp.parse(S2))
    # 34 raw lines decompress to 4 durable messages; a truncated read would
    # lose the later turns entirely.
    assert len(items) == 4
