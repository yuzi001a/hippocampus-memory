# -*- coding: utf-8 -*-
"""Public-alpha env mapping contract — V3CORE_PG_PASSWORD -> legacy storage.pg.password.

Scope (public-alpha config contract; see CHANGELOG.md):

  The documented public install contract is that ``V3CORE_PG_PASSWORD`` env
  var satisfies the plugin.yaml ``requires_env`` constraint and the
  PG-password fail-fast in ``resolve_config``. Before this change,
  ``config._load_legacy_dict`` only loaded ``.env`` into ``os.environ`` and
  ``_resolve_env`` handled only explicit ``${env:VAR}`` placeholders — so a
  fresh Hermes provider isolated adapter (subprocess env, no .env, no
  ``${env:...}`` in YAML) saw ``cfg.pg.password == ""`` and raised
  RuntimeError even when ``os.environ['V3CORE_PG_PASSWORD']`` was set.

  This test module pins the four behaviours the public-alpha fix must
  guarantee:

    (1) env fills empty config  (PG_PASSWORD set, YAML password empty)
    (2) env overrides YAML      (PG_PASSWORD set, YAML password non-empty)
    (3) absent/empty env still raises the existing fail-fast
    (4) no secret value is emitted in error/log output

All tests use ``[REDACTED]`` or synthetic placeholder strings — no real
credentials ever enter the test domain. Assertions check booleans / object
identity / dict contents captured before any logging happens, and use
``caplog`` to verify the secret never appears in any log record.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from v3core import config as cfg_mod
from v3core.config import (
    PG_PASSWORD_ENV_VAR,
    _apply_pg_password_env,
    resolve_config,
    _load_legacy_dict,
)


# Synthetic placeholders — chosen so the substring is distinctive enough that
# any accidental leak (log line, exception text, repr) is caught by the
# negative assertions below.
_ENV_PWD = "[REDACTED]env-pwd-7c4f9a"
_YAML_PWD = "[REDACTED]yaml-pwd-3e2b18"


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _write_yaml(tmp_path: Path, profile: str, body: str) -> Path:
    """Write a config.yaml under a hermes_home sandbox profile.

    Returned path is the on-disk file; the test passes ``hermes_home=str(tmp_path)``
    to ``resolve_config`` so the loader picks this file up via the
    hermes_home fallback contract (老路径缺失 → hermes_home/.v3-core/...).
    """
    cfg_dir = tmp_path / ".v3-core" / "profiles" / profile
    cfg_dir.mkdir(parents=True, exist_ok=True)
    p = cfg_dir / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _isolated_env(monkeypatch, *, pg_password: str | None) -> None:
    """Drop any inherited V3CORE_PG_PASSWORD / hermes_home pollution.

    ``pg_password=None``  → ensure absent (delenv)
    ``pg_password=""``    → ensure empty string (setenv then keep)
    ``pg_password="..."`` → ensure that exact value
    Also clears V3CORE_CONFIG and V3CORE_DOTENV so resolve_config() cannot
    pick up an inherited config path from the dev shell.
    """
    if pg_password is None:
        monkeypatch.delenv(PG_PASSWORD_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(PG_PASSWORD_ENV_VAR, pg_password)
    # conftest fake HOME already isolates ~/, but V3CORE_CONFIG / V3CORE_DOTENV
    # are not affected by the sandbox — guard them explicitly.
    monkeypatch.delenv("V3CORE_CONFIG", raising=False)
    monkeypatch.delenv("V3CORE_DOTENV", raising=False)
    monkeypatch.delenv("V3CORE_HOME", raising=False)


# ─────────────────────────────────────────────────────────────────────────
# (1) env fills empty YAML password
# ─────────────────────────────────────────────────────────────────────────

def test_env_fills_empty_yaml_password(monkeypatch, tmp_path):
    """V3CORE_PG_PASSWORD set + YAML password absent/empty -> cfg.pg.password == env."""
    _isolated_env(monkeypatch, pg_password=_ENV_PWD)
    _write_yaml(tmp_path, "default", "mode: cloud\nstorage:\n  pg:\n    host: localhost\n")

    cfg = resolve_config(
        profile="default",
        hermes_home=str(tmp_path),
    )
    assert cfg.pg is not None
    assert cfg.pg.password == _ENV_PWD
    assert cfg.pg.host == "localhost"  # YAML unaffected


# ─────────────────────────────────────────────────────────────────────────
# (2) env overrides YAML password
# ─────────────────────────────────────────────────────────────────────────

def test_env_overrides_configured_password(monkeypatch, tmp_path):
    """V3CORE_PG_PASSWORD set + YAML password non-empty -> env wins."""
    _isolated_env(monkeypatch, pg_password=_ENV_PWD)
    _write_yaml(
        tmp_path,
        "default",
        "storage:\n  pg:\n    password: '{0}'\n".format(_YAML_PWD),
    )

    cfg = resolve_config(
        profile="default",
        hermes_home=str(tmp_path),
    )
    assert cfg.pg is not None
    assert cfg.pg.password == _ENV_PWD, (
        "env must override YAML password per documented precedence"
    )


def test_yaml_password_used_when_env_absent(monkeypatch, tmp_path):
    """V3CORE_PG_PASSWORD unset + YAML password present -> YAML wins (no behaviour change)."""
    _isolated_env(monkeypatch, pg_password=None)
    _write_yaml(
        tmp_path,
        "default",
        "storage:\n  pg:\n    password: '{0}'\n".format(_YAML_PWD),
    )

    cfg = resolve_config(
        profile="default",
        hermes_home=str(tmp_path),
    )
    assert cfg.pg is not None
    assert cfg.pg.password == _YAML_PWD


# ─────────────────────────────────────────────────────────────────────────
# (3) absent / empty env still raises the existing fail-fast
# ─────────────────────────────────────────────────────────────────────────

def test_absent_env_empty_yaml_raises(monkeypatch, tmp_path):
    """Env absent + YAML password empty -> RuntimeError (existing fail-fast)."""
    _isolated_env(monkeypatch, pg_password=None)
    _write_yaml(tmp_path, "default", "mode: cloud\n")  # no pg block at all

    with pytest.raises(RuntimeError) as exc_info:
        resolve_config(profile="default", hermes_home=str(tmp_path))
    msg = str(exc_info.value)
    assert PG_PASSWORD_ENV_VAR in msg
    assert _ENV_PWD not in msg  # defensive: synthetic env value not present


def test_empty_env_empty_yaml_raises(monkeypatch, tmp_path):
    """Env empty string + YAML password empty -> RuntimeError (no silent fallback)."""
    _isolated_env(monkeypatch, pg_password="")
    _write_yaml(tmp_path, "default", "storage:\n  pg:\n    host: localhost\n")

    with pytest.raises(RuntimeError) as exc_info:
        resolve_config(profile="default", hermes_home=str(tmp_path))
    msg = str(exc_info.value)
    assert PG_PASSWORD_ENV_VAR in msg
    assert _ENV_PWD not in msg


# ─────────────────────────────────────────────────────────────────────────
# (4) no secret value in error/log output
# ─────────────────────────────────────────────────────────────────────────

def test_no_secret_in_error_message(monkeypatch, tmp_path):
    """The fail-fast error must mention the var name, never the value."""
    _isolated_env(monkeypatch, pg_password=None)
    _write_yaml(tmp_path, "default", "mode: cloud\n")

    with pytest.raises(RuntimeError) as exc_info:
        resolve_config(profile="default", hermes_home=str(tmp_path))
    msg = str(exc_info.value)
    # Name present so user knows what to set.
    assert PG_PASSWORD_ENV_VAR in msg
    # No synthetic / plausible-secret fragments leaked through the message.
    assert "[REDACTED]" not in msg
    assert "env-pwd-7c4f9a" not in msg
    assert "yaml-pwd-3e2b18" not in msg
    # And no host/user/port leakage either.
    assert "v3user" not in msg
    assert "5433" not in msg


def test_no_secret_in_log_output(monkeypatch, tmp_path, caplog):
    """The PG password value must never appear in any log record."""
    _isolated_env(monkeypatch, pg_password=_ENV_PWD)
    _write_yaml(
        tmp_path,
        "default",
        "storage:\n  pg:\n    password: '{0}'\n".format(_YAML_PWD),
    )

    # Capture everything from the config module's logger at DEBUG so any
    # accidental debug/info/warning emission of the secret is caught.
    caplog.set_level(logging.DEBUG, logger="v3core.config")

    cfg = resolve_config(profile="default", hermes_home=str(tmp_path))
    assert cfg.pg.password == _ENV_PWD

    for record in caplog.records:
        rendered = record.getMessage()
        assert _ENV_PWD not in rendered, (
            "v3core.config logger leaked the env password value: "
            f"level={record.levelname} msg={rendered!r}"
        )
        assert _YAML_PWD not in rendered, (
            "v3core.config logger leaked the YAML password value: "
            f"level={record.levelname} msg={rendered!r}"
        )

    # Also inspect the formatted log text that pytest's caplog.text exposes
    # (covers handler-formatted output, e.g. exc_info formatting).
    assert _ENV_PWD not in caplog.text
    assert _YAML_PWD not in caplog.text


def test_no_secret_on_empty_env_with_yaml_password(monkeypatch, tmp_path, caplog):
    """Empty env + YAML password present: secret must not appear in logs either."""
    _isolated_env(monkeypatch, pg_password="")
    _write_yaml(
        tmp_path,
        "default",
        "storage:\n  pg:\n    password: '{0}'\n".format(_YAML_PWD),
    )
    caplog.set_level(logging.DEBUG, logger="v3core.config")

    cfg = resolve_config(profile="default", hermes_home=str(tmp_path))
    assert cfg.pg.password == _YAML_PWD

    assert _YAML_PWD not in caplog.text
    assert _ENV_PWD not in caplog.text


# ─────────────────────────────────────────────────────────────────────────
# Direct unit tests on the helper (no YAML / no resolve_config involvement)
# ─────────────────────────────────────────────────────────────────────────

def test_helper_no_op_when_env_absent(monkeypatch):
    """_apply_pg_password_env leaves cfg untouched when env is absent."""
    monkeypatch.delenv(PG_PASSWORD_ENV_VAR, raising=False)
    base = {
        "storage": {"pg": {"host": "h", "password": ""}},
        "mode": "cloud",
    }
    out = _apply_pg_password_env(base)
    assert out["storage"]["pg"]["password"] == ""
    assert out["storage"]["pg"]["host"] == "h"


def test_helper_no_op_when_env_empty(monkeypatch):
    """_apply_pg_password_env leaves cfg untouched when env is empty string."""
    monkeypatch.setenv(PG_PASSWORD_ENV_VAR, "")
    base = {"storage": {"pg": {"password": "yaml-value"}}}
    out = _apply_pg_password_env(base)
    assert out["storage"]["pg"]["password"] == "yaml-value"


def test_helper_fills_empty_password(monkeypatch):
    monkeypatch.setenv(PG_PASSWORD_ENV_VAR, _ENV_PWD)
    base = {"storage": {"pg": {"host": "h", "password": ""}}}
    out = _apply_pg_password_env(base)
    assert out["storage"]["pg"]["password"] == _ENV_PWD


def test_helper_overrides_existing_password(monkeypatch):
    monkeypatch.setenv(PG_PASSWORD_ENV_VAR, _ENV_PWD)
    base = {"storage": {"pg": {"password": _YAML_PWD}}}
    out = _apply_pg_password_env(base)
    assert out["storage"]["pg"]["password"] == _ENV_PWD


def test_helper_handles_missing_storage_and_pg_blocks(monkeypatch):
    """_apply_pg_password_env must create storage/pg blocks if YAML omits them."""
    monkeypatch.setenv(PG_PASSWORD_ENV_VAR, _ENV_PWD)
    base: dict = {"mode": "cloud"}
    out = _apply_pg_password_env(base)
    assert out["storage"]["pg"]["password"] == _ENV_PWD


# ─────────────────────────────────────────────────────────────────────────
# Legacy load boundary — confirm the helper is wired BEFORE _resolve_env
# ─────────────────────────────────────────────────────────────────────────

def test_legacy_load_applies_env_before_resolve(monkeypatch, tmp_path):
    """_load_legacy_dict must place env password into storage.pg.password
    (i.e. callable, return_legacy=True yields a dict that has the env value)."""
    _isolated_env(monkeypatch, pg_password=_ENV_PWD)
    _write_yaml(tmp_path, "default", "storage:\n  pg:\n    password: '{0}'\n".format(_YAML_PWD))

    legacy = _load_legacy_dict(profile="default", hermes_home=str(tmp_path))
    assert legacy["storage"]["pg"]["password"] == _ENV_PWD


# ─────────────────────────────────────────────────────────────────────────
# Regression — existing ${env:VAR} placeholder behaviour preserved
# ─────────────────────────────────────────────────────────────────────────

def test_legacy_env_placeholder_still_works(monkeypatch, tmp_path):
    """Pre-existing ${env:SOME_VAR} placeholder syntax must still resolve
    (no regression to the P0 contract on _resolve_env)."""
    monkeypatch.setenv("V3CORE_PG_PORT_OVERRIDE", "6543")
    _isolated_env(monkeypatch, pg_password=_ENV_PWD)
    _write_yaml(
        tmp_path,
        "default",
        "storage:\n  pg:\n    port: ${env:V3CORE_PG_PORT_OVERRIDE}\n",
    )

    cfg = resolve_config(profile="default", hermes_home=str(tmp_path))
    assert cfg.pg.port == 6543
    assert cfg.pg.password == _ENV_PWD


# ---------------------------------------------------------------------------
# Profile-scoped .env must be read back (2026-09-18)
# ---------------------------------------------------------------------------


def test_profile_scoped_env_is_loaded_for_referenced_secrets(tmp_path, monkeypatch):
    """`hippocampus install` writes the provider keys to <profile_dir>/.env.

    The config it writes references them as ${env:...}, and the engine's .env
    search did not include the profile directory — so a clean process resolved
    every provider key to "" and the first session after a fresh install 401'd,
    while `doctor --full` reported "the configured key was rejected". The profile
    .env must be read back (explicit env still wins).
    """
    prof = tmp_path / "profile"
    prof.mkdir()
    (prof / ".env").write_text("V3CORE_EMBED_API_KEY=profile-scoped-key\n",
                               encoding="utf-8")
    (prof / "config.yaml").write_text(
        "basePath: 'x'\n"
        "storage:\n"
        "  pg:\n"
        "    password: 'p'\n"
        "  embed:\n"
        "    endpoint: 'https://example.invalid/v1/embeddings'\n"
        "    model: 'BAAI/bge-m3'\n"
        "    api_key: '${env:V3CORE_EMBED_API_KEY}'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("V3CORE_EMBED_API_KEY", raising=False)
    monkeypatch.setenv("V3CORE_CONFIG", str(prof / "config.yaml"))
    monkeypatch.setenv("V3CORE_PG_PASSWORD", "p")

    from v3core import config as cfg_mod

    cfg = cfg_mod.resolve_config()
    assert cfg.embed.api_key == "profile-scoped-key"
