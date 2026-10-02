"""I01 — auto discovery resolvers.

Contract: bounded, deterministic, read-only, host-specific. Precedence:
explicit override > env var > platform default > (memory-md) current dir.
No recursion outside the known host roots; no secrets.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def _discovery():
    return importlib.import_module("v3core.importers.discovery")


# ── resolver precedence ───────────────────────────────────────────────


def test_hermes_root_env_override_wins(tmp_path):
    d = _discovery()
    root, origin = d.resolve_host_root(
        "hermes", env={"HERMES_HOME": str(tmp_path / "hh")}, platform="win32"
    )
    assert root == tmp_path / "hh"
    assert "HERMES_HOME" in origin


def test_hermes_root_windows_default(tmp_path):
    d = _discovery()
    root, origin = d.resolve_host_root("hermes", env={}, platform="win32")
    assert root == Path.home() / "AppData" / "Local" / "hermes"
    assert origin == "default"


def test_hermes_root_posix_default():
    d = _discovery()
    root, origin = d.resolve_host_root("hermes", env={}, platform="posix")
    assert root == Path.home() / ".hermes"


def test_dsh_root_env_override(tmp_path):
    d = _discovery()
    root, origin = d.resolve_host_root(
        "dsh", env={"DSH_HOME": str(tmp_path / "d")}, platform="win32"
    )
    assert root == tmp_path / "d"


def test_dsh_root_default():
    d = _discovery()
    root, _ = d.resolve_host_root("dsh", env={}, platform="posix")
    assert root == Path.home() / ".dsh"


def test_pi_root_session_dir_env_wins(tmp_path):
    d = _discovery()
    root, origin = d.resolve_host_root(
        "pi",
        env={
            "PI_CODING_AGENT_SESSION_DIR": str(tmp_path / "sd"),
            "PI_CODING_AGENT_DIR": str(tmp_path / "ad"),
        },
        platform="posix",
    )
    assert root == tmp_path / "sd"
    assert "SESSION_DIR" in origin


def test_pi_root_agent_dir_env(tmp_path):
    d = _discovery()
    root, origin = d.resolve_host_root(
        "pi", env={"PI_CODING_AGENT_DIR": str(tmp_path / "ad")}, platform="posix"
    )
    assert root == tmp_path / "ad" / "sessions"
    assert "AGENT_DIR" in origin


def test_pi_root_default():
    d = _discovery()
    root, _ = d.resolve_host_root("pi", env={}, platform="posix")
    assert root == Path.home() / ".pi" / "agent" / "sessions"


# ── discover_host shape ───────────────────────────────────────────────


def test_dsh_discover_finds_fixture_sessions():
    d = _discovery()
    root = Path(__file__).resolve().parent / "fixtures" / "i01" / "dsh"
    src = d.discover_host("dsh", root=root)
    assert src.found
    assert src.host == "dsh"
    assert src.sessions == 2
    assert src.format == "jsonl.zstd"
    assert src.version == "v4"
    assert len(src.artifacts) == 2


def test_pi_discover_finds_fixture_session():
    d = _discovery()
    root = Path(__file__).resolve().parent / "fixtures" / "i01" / "pi"
    src = d.discover_host("pi", root=root)
    assert src.found
    assert src.sessions == 1
    assert src.format == "jsonl"
    assert src.version == "v3"
    assert len(src.artifacts) == 1


def test_hermes_discover_finds_state_db():
    d = _discovery()
    root = Path(__file__).resolve().parent / "fixtures" / "i01" / "hermes"
    src = d.discover_host("hermes", root=root)
    assert src.found
    assert src.format == "sqlite"
    assert any(p.name == "state.db" for p in src.artifacts)


def test_missing_root_reports_reason_not_crash(tmp_path):
    d = _discovery()
    src = d.discover_host("dsh", root=tmp_path / "nope")
    assert not src.found
    assert src.reason  # non-empty explanation
    assert src.artifacts == []


def test_unknown_host_raises():
    d = _discovery()
    with pytest.raises(ValueError):
        d.resolve_host_root("not-a-host", env={})


def test_discover_all_bounded_smoke(tmp_path):
    """discover_all with overrides only touches the given roots."""
    d = _discovery()
    root = Path(__file__).resolve().parent / "fixtures" / "i01" / "dsh"
    result = d.discover_all(
        hosts=["dsh"], overrides={"dsh": root}, env={}, platform="win32"
    )
    assert len(result) == 1
    assert result[0].found


def test_discover_source_fields_complete():
    """Every detected source carries the §5 field set."""
    d = _discovery()
    root = Path(__file__).resolve().parent / "fixtures" / "i01" / "pi"
    src = d.discover_host("pi", root=root)
    for field in (
        "host",
        "root",
        "found",
        "artifacts",
        "format",
        "version",
        "sessions",
        "messages",
        "oldest",
        "newest",
        "capability",
        "reason",
    ):
        assert hasattr(src, field), f"missing field {field}"
