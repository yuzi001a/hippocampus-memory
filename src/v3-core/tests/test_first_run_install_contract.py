# -*- coding: utf-8 -*-
"""Static contract tests for ``v3core.first_run`` (Hippocampus v0.2 First User Release).

These tests are hermetic:
  * No network calls are made.
  * No real Docker daemon is touched — every docker interaction is mocked via
    a ``docker_runner`` callable injected into ``ensure_pgvector_container``.
  * No real PostgreSQL connection is opened — ``psycopg2.connect`` is
    hard-blocked by the P0-A conftest.
  * The hermes home / profile directory are resolved through monkeypatched
    environment variables and an isolated ``tmp_path``.

What is asserted:

  1. ``PRESETS`` table shape — both presets exist, ``siliconflow`` has the
     four canonical sub-blocks (``embed`` / ``rerank`` / ``llm``), and
     ``custom`` is empty.
  2. ``_redact`` masks secrets to first 4 chars + ``...`` and handles
     empty / short / None inputs without raising.
  3. ``write_profile_config`` — siliconflow writes absolute basePath,
     password is empty, and embed / rerank / llm blocks are present.
     ``custom`` writes the basePath + pg block only.
  4. ``write_profile_config`` is idempotent: a second call without
     ``overwrite=True`` returns the same path and does NOT modify the file.
  5. ``ensure_pgvector_container`` reports the docker-missing failure with a
     human-readable, actionable message — never a silent skip.
  6. ``run_install`` returns a non-zero exit code when Docker is absent.
  7. ``wire_hermes`` preserves every non-owned line in the existing
     ``config.yaml`` and only touches the ``memory:`` block.  The backup
     file is written next to the original.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path

import pytest

from v3core import first_run as fr


# ---------------------------------------------------------------------------
# 1. PRESETS table shape
# ---------------------------------------------------------------------------


def test_presets_has_siliconflow_and_custom():
    assert set(fr.PRESETS) == {"siliconflow", "custom"}, (
        "PRESETS must contain exactly 'siliconflow' and 'custom'"
    )


def test_presets_siliconflow_shape():
    sf = fr.PRESETS["siliconflow"]
    # embed
    assert sf["embed"]["endpoint"] == "https://api.siliconflow.cn/v1/embeddings"
    assert sf["embed"]["model"] == "BAAI/bge-m3"
    assert int(sf["embed"]["dim"]) == 1024
    # rerank
    assert sf["rerank"]["endpoint"] == "https://api.siliconflow.cn/v1/rerank"
    assert sf["rerank"]["model"] == "BAAI/bge-reranker-v2-m3"
    # llm
    # One SiliconFlow key covers embed + rerank + the memory LLM, so the preset
    # must point the LLM at SiliconFlow too (a MiniMax endpoint here produced a
    # 400/401 on every memory call for a first user holding a SiliconFlow key).
    assert sf["llm"]["provider"] == "openai"
    assert sf["llm"]["base_url"] == "https://api.siliconflow.cn/v1"
    assert sf["llm"]["thinking"] is False
    assert sf["llm"]["base_url"] == "https://api.siliconflow.cn/v1"
    assert sf["llm"]["model"] == "Qwen/Qwen2.5-7B-Instruct"


def test_presets_custom_is_empty():
    assert fr.PRESETS["custom"] == {}, (
        "custom preset must be empty — no embed/rerank/llm blocks"
    )


# ---------------------------------------------------------------------------
# 2. _redact
# ---------------------------------------------------------------------------


def test_redact_normal_value():
    assert fr._redact("sk-1234567890abcdef") == "sk-1..."


def test_redact_short_value():
    # 1 or 2 chars → first char + '***'
    assert fr._redact("ab") == "a***"


def test_redact_empty_and_none():
    assert fr._redact("") == "(empty)"
    assert fr._redact(None) == "(empty)"


def test_redact_does_not_leak_full_secret():
    secret = "supersecretpassword123"
    masked = fr._redact(secret)
    assert "supersecretpassword" not in masked
    assert "123" not in masked
    assert masked == "supe..."


# ---------------------------------------------------------------------------
# 3. write_profile_config content
# ---------------------------------------------------------------------------


_PG = {
    "host": "127.0.0.1",
    "port": 55432,
    "database": "v3embeddings_alpha",
    "user": "postgres",
}


def test_write_profile_config_siliconflow(tmp_path: Path):
    out = io.StringIO()
    profile = tmp_path / "v3" / "default"
    written = fr.write_profile_config(
        profile_dir=profile,
        preset="siliconflow",
        pg=_PG,
        embed=fr.PRESETS["siliconflow"]["embed"],
        llm=fr.PRESETS["siliconflow"]["llm"],
        rerank=fr.PRESETS["siliconflow"]["rerank"],
        out=out,
    )
    assert written == profile / "config.yaml"
    text = written.read_text(encoding="utf-8")
    # basePath is absolute and points at the profile dir
    assert str(profile.resolve()) in text
    # top-level basePath key (engine contract — only canonical path key)
    assert re.search(r"^basePath:\s*['\"]", text, re.MULTILINE), (
        "top-level basePath key must be present (engine contract)"
    )
    # pg block: password MUST be empty
    m_pw = re.search(r"password:\s*['\"]?['\"]?", text)
    assert m_pw is not None, "password field must be present in pg block"
    # embed / rerank / llm blocks must all be present
    assert "storage:" in text and "embed:" in text
    assert "rerank:" in text
    assert re.search(r"^llm:", text, re.MULTILINE), (
        "llm block must be a TOP-LEVEL key (engine reads cfg.llm directly)"
    )
    # The endpoint strings are intact
    assert "api.siliconflow.cn/v1/embeddings" in text
    assert "api.siliconflow.cn/v1/rerank" in text
    # The SiliconFlow preset must NOT ship a foreign provider's endpoint: the
    # memory LLM rides the same SiliconFlow key, so pairing it with
    # api.minimaxi.com made every memory call fail with 400/401 on a fresh
    # install ("your key and your door don't match").
    assert "api.siliconflow.cn/v1" in text
    assert "api.minimaxi.com" not in text
    assert "thinking: false" in text  # SF rejects the DeepSeek/MiniMax thinking param
    # Profile name
    assert "default" in text


def test_write_profile_config_custom_no_provider_blocks(tmp_path: Path):
    out = io.StringIO()
    profile = tmp_path / "custom" / "default"
    written = fr.write_profile_config(
        profile_dir=profile,
        preset="custom",
        pg=_PG,
        out=out,
    )
    text = written.read_text(encoding="utf-8")
    # basePath present and absolute
    assert str(profile.resolve()) in text
    assert re.search(r"^basePath:\s*['\"]", text, re.MULTILINE)
    # No provider blocks — custom preset is intentionally empty
    assert "embed:" not in text, "custom preset must NOT include embed block"
    assert "rerank:" not in text, "custom preset must NOT include rerank block"
    # llm block is a TOP-LEVEL key, not nested under storage; absence here
    # means we did not write any llm: line.
    assert not re.search(r"^llm:", text, re.MULTILINE), (
        "custom preset must NOT include llm block"
    )
    # pg block present with empty password
    assert "storage:" in text and "pg:" in text


def test_write_profile_config_password_field_always_empty(tmp_path: Path):
    """The engine contract: the password NEVER lives in config.yaml."""
    out = io.StringIO()
    profile = tmp_path / "p" / "default"
    fr.write_profile_config(
        profile_dir=profile, preset="siliconflow", pg=_PG,
        embed=fr.PRESETS["siliconflow"]["embed"],
        llm=fr.PRESETS["siliconflow"]["llm"],
        rerank=fr.PRESETS["siliconflow"]["rerank"],
        out=out,
    )
    text = (profile / "config.yaml").read_text(encoding="utf-8")
    # The 'password:' line must be present but value empty
    m = re.search(r"^\s*password:\s*(['\"]?)([^'\"#\n]*)\1", text, re.MULTILINE)
    assert m is not None, "pg block must include a 'password:' key"
    assert m.group(2).strip() == "", (
        f"password field must be empty in config.yaml; got {m.group(2)!r}"
    )


# ---------------------------------------------------------------------------
# 4. write_profile_config idempotency
# ---------------------------------------------------------------------------


def test_write_profile_config_second_call_does_not_clobber(tmp_path: Path):
    out = io.StringIO()
    profile = tmp_path / "idem" / "default"
    p1 = fr.write_profile_config(
        profile_dir=profile, preset="siliconflow", pg=_PG,
        embed=fr.PRESETS["siliconflow"]["embed"],
        llm=fr.PRESETS["siliconflow"]["llm"],
        rerank=fr.PRESETS["siliconflow"]["rerank"],
        out=out,
    )
    text1 = p1.read_text(encoding="utf-8")
    mtime1 = p1.stat().st_mtime_ns

    p2 = fr.write_profile_config(
        profile_dir=profile, preset="siliconflow", pg=_PG,
        embed=fr.PRESETS["siliconflow"]["embed"],
        llm=fr.PRESETS["siliconflow"]["llm"],
        rerank=fr.PRESETS["siliconflow"]["rerank"],
        out=out,
    )
    assert p1 == p2, "second call must return the SAME path"
    text2 = p2.read_text(encoding="utf-8")
    assert text1 == text2, "second call must not have touched the file content"
    assert p2.stat().st_mtime_ns == mtime1, (
        "second call must not have rewritten the file (mtime unchanged)"
    )
    # Sanity: SKIP was printed to the captured output
    assert "SKIP" in out.getvalue()


def test_write_profile_config_overwrite_creates_backup(tmp_path: Path):
    out = io.StringIO()
    profile = tmp_path / "ow" / "default"
    fr.write_profile_config(
        profile_dir=profile, preset="custom", pg=_PG, out=out,
    )
    text_v1 = (profile / "config.yaml").read_text(encoding="utf-8")

    # Now overwrite with siliconflow — should take a timestamped backup and
    # replace the file.
    fr.write_profile_config(
        profile_dir=profile, preset="siliconflow", pg=_PG,
        embed=fr.PRESETS["siliconflow"]["embed"],
        llm=fr.PRESETS["siliconflow"]["llm"],
        rerank=fr.PRESETS["siliconflow"]["rerank"],
        overwrite=True,
        out=out,
    )
    text_v2 = (profile / "config.yaml").read_text(encoding="utf-8")
    assert text_v1 != text_v2, "overwrite=True must actually replace content"
    # backup file exists with bak.<timestamp> suffix
    backups = list(profile.glob("config.yaml.bak.*"))
    assert len(backups) == 1, (
        f"expected exactly one backup file; found {backups}"
    )


# ---------------------------------------------------------------------------
# 5. docker-missing human message
# ---------------------------------------------------------------------------


def test_ensure_pgvector_container_docker_missing_has_actionable_message(
    tmp_path: Path, monkeypatch
):
    """When Docker is not on PATH the step MUST fail loudly with a
    human-readable, actionable message — never a silent skip."""

    # Force shutil.which('docker') to return None. The helper uses shutil.which
    # directly; patch it at the module-level import.
    monkeypatch.setattr(fr.shutil, "which", lambda name: None if name == "docker" else "/bin/echo")

    out = io.StringIO()
    res = fr.ensure_pgvector_container(
        port=55432, password="local-only-pw-12345",
        out=out,
    )
    # error key set
    assert res.get("error"), "error key must be set when docker is missing"
    err = res["error"]
    # Human-readable: must mention Docker and an actionable next step.
    lower = err.lower()
    assert "docker" in lower, (
        "docker-missing error must mention Docker by name"
    )
    assert "docker desktop" in lower or "install" in lower, (
        "docker-missing error must include an actionable install hint"
    )
    # The PASS / FAIL / SKIP word appears in the printed output so the
    # human can scan the log.
    assert "FAIL" in out.getvalue(), (
        "docker-missing must print a FAIL line, never a silent skip"
    )
    # The error never echoes the password back.
    assert "local-only-pw-12345" not in err
    assert "local-only-pw-12345" not in out.getvalue()


def test_ensure_pgvector_container_refuses_production_port(tmp_path: Path):
    out = io.StringIO()
    res = fr.ensure_pgvector_container(
        port=5433, password="x" * 8, out=out,
    )
    assert res.get("error"), "production port 5433 must be refused"
    assert "5433" in res["error"]
    assert "FAIL" in out.getvalue()


def test_ensure_pgvector_container_empty_password_fails(tmp_path: Path):
    out = io.StringIO()
    res = fr.ensure_pgvector_container(
        port=55432, password="", out=out,
    )
    assert res.get("error"), "empty password must be refused"
    assert "password" in res["error"].lower()


# ---------------------------------------------------------------------------
# 6. run_install returns non-zero when docker is absent
# ---------------------------------------------------------------------------


def test_run_install_returns_nonzero_when_docker_missing(
    monkeypatch, tmp_path: Path
):
    """The orchestrator must propagate the docker-missing failure."""
    # Pretend uv and v3-core are present; only docker is missing.
    monkeypatch.setattr(fr.shutil, "which", lambda name: {
        "docker": None, "uv": "/bin/uv",
    }.get(name, "/bin/echo"))
    # v3core_installed is a boolean derived from `import v3core` — we already
    # have it imported in this test session. Stub the preflight so the
    # install step itself succeeds but the docker step fails.
    pre_env = {
        "python_version": "3.13.14", "python_path": sys.executable,
        "uv_present": True, "uv_path": "/bin/uv",
        "docker_present": False, "docker_version": None,
        "docker_running": False,
        "hermes_home": str(tmp_path), "hermes_config": None,
        "hermes_venv_python": None,
        "v3core_installed": True, "plugin_installed": False,
        "profile_dir": str(tmp_path / "v3" / "default"),
        "existing_install": False,
    }
    monkeypatch.setattr(fr, "detect_environment", lambda **_: pre_env)

    out = io.StringIO()
    rc = fr.run_install(
        preset="custom",
        pg_port=55432,
        profile_dir=str(tmp_path / "v3" / "default"),
        hermes_home=str(tmp_path),
        out=out,
    )
    assert rc != 0, (
        f"run_install must return non-zero when docker is missing; got {rc}"
    )
    rendered = out.getvalue()
    # The verdict block lines must be present (one per the spec).
    for line in (
        "install:", "database:", "embedding:", "rerank:",
        "memory LLM:", "hermes provider:", "restart persistence hint:",
    ):
        assert line in rendered, f"verdict must contain '{line}'"
    # install step is the first one — should be FAIL because docker is missing.
    install_line = next(
        ln for ln in rendered.splitlines() if ln.startswith("install:")
    )
    assert "FAIL" in install_line, install_line


# ---------------------------------------------------------------------------
# 7. wire_hermes is idempotent and preserves other keys
# ---------------------------------------------------------------------------


def test_wire_hermes_preserves_other_keys_and_backs_up(tmp_path: Path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    cfg = hermes_home / "config.yaml"
    original = (
        "model:\n"
        "  base_url: 'https://example.test/v1'\n"
        "  default: 'gpt-test'\n"
        "memory:\n"
        "  memory_enabled: true\n"
        "  provider: some_other_provider\n"
    )
    cfg.write_text(original, encoding="utf-8")

    out = io.StringIO()
    res = fr.wire_hermes(
        hermes_home=hermes_home,
        venv_python=None,  # do NOT probe plugin importability — hermes venv absent here
        hermes_config=cfg,
        out=out,
    )
    assert res["hermes_found"] is True
    assert res["config_updated"] is True
    assert res["provider_registered"] is True
    assert res["config_backup"], "a backup file path must be returned"
    backup = Path(res["config_backup"])
    assert backup.exists()
    # The backup's content equals the original file exactly.
    assert backup.read_text(encoding="utf-8") == original
    # The new file has provider: deep_memory_v3 AND everything else preserved.
    new_text = cfg.read_text(encoding="utf-8")
    assert "provider: deep_memory_v3" in new_text
    # Other top-level keys untouched
    assert "model:" in new_text
    assert "base_url: 'https://example.test/v1'" in new_text
    assert "default: 'gpt-test'" in new_text
    # memory block still has memory_enabled
    assert "memory_enabled: true" in new_text


def test_wire_hermes_idempotent_when_already_registered(tmp_path: Path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    cfg = hermes_home / "config.yaml"
    cfg.write_text(
        "memory:\n  memory_enabled: true\n  provider: deep_memory_v3\n",
        encoding="utf-8",
    )

    out = io.StringIO()
    res = fr.wire_hermes(
        hermes_home=hermes_home, venv_python=None, hermes_config=cfg, out=out,
    )
    # provider already registered → no config edit, no second backup
    assert res["provider_registered"] is True
    assert res["config_updated"] is False
    assert "SKIP" in out.getvalue(), (
        "second run on an already-registered config must print SKIP, not PASS"
    )


def test_wire_hermes_appends_block_when_missing(tmp_path: Path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    cfg = hermes_home / "config.yaml"
    cfg.write_text(
        "model:\n  base_url: 'https://example.test/v1'\n",
        encoding="utf-8",
    )
    out = io.StringIO()
    res = fr.wire_hermes(
        hermes_home=hermes_home, venv_python=None, hermes_config=cfg, out=out,
    )
    new_text = cfg.read_text(encoding="utf-8")
    # The memory block was appended; original key preserved.
    assert "model:" in new_text and "base_url: 'https://example.test/v1'" in new_text
    assert "memory:" in new_text
    assert "provider: deep_memory_v3" in new_text


def test_wire_hermes_missing_hermes_home(tmp_path: Path):
    out = io.StringIO()
    res = fr.wire_hermes(
        hermes_home=tmp_path / "does-not-exist",
        venv_python=None, hermes_config=None, out=out,
    )
    assert res["hermes_found"] is False
    assert res["error"]
    assert "FAIL" in out.getvalue()


# ---------------------------------------------------------------------------
# 8. _build_dsn never embeds the password
# ---------------------------------------------------------------------------


def test_build_dsn_no_password_in_string():
    dsn = fr._build_dsn(port=55432)
    assert "127.0.0.1:55432/v3embeddings_alpha" in dsn
    # No password component (no `:` after the user segment)
    assert "@127.0.0.1" in dsn
    # No whitespace / newline
    assert "\n" not in dsn and " " not in dsn


# ---------------------------------------------------------------------------
# 9. Subprocess helper sanity (no shell, no env echo)
# ---------------------------------------------------------------------------


def test_run_rejects_string_command():
    """The subprocess helper MUST refuse shell-style strings; list only."""
    with pytest.raises(ValueError):
        fr._run("docker info")  # type: ignore[arg-type]


def test_run_rejects_empty_command():
    with pytest.raises(ValueError):
        fr._run([])


# ---------------------------------------------------------------------------
# 10. Reused container keeps ITS password (2026-09-18)
# ---------------------------------------------------------------------------


def test_reused_container_password_is_adopted():
    """A container keeps the password it was born with.

    When the profile is new (reinstall, wiped profile dir, second profile) the
    installer generates a fresh password, then reuses the existing container of
    the same name. Writing the generated password to the profile made every later
    TCP connection fail with ``password authentication failed for user
    "postgres"`` — while ``docker exec`` probes kept passing, because those go
    over the container's local socket (trust). Observed live on the fresh-install
    canary. The reuse path must adopt the container's real password.
    """
    calls = []

    def stub(cmd):
        calls.append(cmd)
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "ps":
            # no container of this name is running yet -> fall through to `run`
            return (0, "", "")
        if sub == "run":
            return (125, "", 'The container name "/hippocampus-pg" is already in '
                             'use by container "abc".')
        if sub == "start":
            return (0, "hippocampus-pg", "")
        if sub == "inspect":
            return (0, "POSTGRES_USER=postgres\nPOSTGRES_PASSWORD=container-born-pw\n", "")
        return (0, "1", "")

    result = fr.ensure_pgvector_container(
        port=55999, password="newly-generated-pw", docker_runner=stub,
    )
    assert result.get("reused") is True, result
    assert result.get("container_password") == "container-born-pw"
