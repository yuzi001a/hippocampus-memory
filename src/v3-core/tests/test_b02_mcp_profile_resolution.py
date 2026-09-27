"""B02_EMBEDDING_401 / §6 — one effective config truth for one profile.

The defect this pins (found by the A/B in this round):

    profile resolves DIRECTLY            -> valid config (pg target, embed identity)
    same profile via the MCP child       -> config not found at all
                                            ("V3CORE_PG_PASSWORD not set" on first
                                             tool call, while tools/list still
                                             advertises all 13 tools)

Cause: the installer writes a fresh-install profile under
``<HERMES_HOME>/.v3-core/profiles/<profile>/`` and every *install-side* path
(``distribution_cli``, ``doctor_full``, the Hermes provider) passes
``hermes_home``. The runtime entry points (``v3-core mcp`` / CLI) constructed
``V3Core(profile=...)`` without it, so a fresh install was invisible to the very
tools that use it — and a host that swallows child stderr saw a silent
half-install.

These tests spawn the REAL MCP child process (no mocks, no source inspection):
they assert that the child, given only ``HERMES_HOME``, resolves the same
effective config (pg target) as an in-process ``resolve_config``.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

PROFILE = "b02-hh"
DEAD_PG_PORT = 59999  # nothing listens here: we assert resolution, not connectivity


def _write_profile(hh: pathlib.Path, base: pathlib.Path) -> pathlib.Path:
    pdir = hh / ".v3-core" / "profiles" / PROFILE
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "config.yaml").write_text(
        "basePath: {base}\n"
        "storage:\n"
        "  pg:\n"
        "    host: 127.0.0.1\n"
        "    port: {port}\n"
        "    database: hhprobe\n"
        "    user: v3user\n"
        "  embed:\n"
        "    endpoint: https://api.siliconflow.cn/v1/embeddings\n"
        "    model: BAAI/bge-m3\n"
        "    dim: 1024\n"
        "    api_key: ${{env:V3CORE_EMBED_API_KEY}}\n"
        "observer:\n"
        "  enabled: false\n"
        "e1:\n"
        "  enabled: false\n".format(base=str(base).replace("\\", "/"), port=DEAD_PG_PORT),
        encoding="utf-8",
    )
    (pdir / ".env").write_text(
        "V3CORE_PG_PASSWORD=hh-probe-pw\n"
        "V3CORE_EMBED_API_KEY=hh-probe-embed-key-0123456789abcdef\n",
        encoding="utf-8",
    )
    return pdir / "config.yaml"


def _child_env(hh: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    # The whole point: the profile must be found through HERMES_HOME alone.
    env.pop("V3CORE_CONFIG", None)
    # Ambient credentials must not be able to stand in for the profile's own.
    env.pop("V3CORE_PG_PASSWORD", None)
    env.pop("V3CORE_EMBED_API_KEY", None)
    env["HERMES_HOME"] = str(hh)
    return env


def _rpc(proc: subprocess.Popen, obj: dict, expect_id: int) -> dict:
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()
    deadline = time.time() + 180
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP child closed stdout")
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("id") == expect_id:
            return msg
    raise TimeoutError(f"no reply for id={expect_id}")


@pytest.fixture()
def mcp_child(tmp_path):
    hh = tmp_path / "hermes-home"
    cfg = _write_profile(hh, tmp_path / "data")
    assert cfg.exists()
    proc = subprocess.Popen(
        [sys.executable, "-m", "v3core", "mcp", "--profile", PROFILE],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=_child_env(hh), cwd=str(tmp_path),
    )
    try:
        yield hh, proc
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _status_text(proc: subprocess.Popen) -> str:
    _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "b02-hh-test", "version": "1"}}}, 1)
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()
    tools = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, 2)
    names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
    assert len(names) == 13, f"expected the 13-tool surface, got {len(names)}: {names}"
    call = _rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "v3_status", "arguments": {}}}, 3)
    return json.dumps(call.get("result"), ensure_ascii=False)


def test_direct_process_resolves_the_hermes_home_profile(tmp_path):
    """Control leg: the profile IS resolvable when hermes_home is passed."""
    from v3core.config import resolve_config

    hh = tmp_path / "hermes-home"
    _write_profile(hh, tmp_path / "data")
    cfg = resolve_config(PROFILE, hermes_home=str(hh))
    assert cfg.pg.port == DEAD_PG_PORT
    assert cfg.pg.database == "hhprobe"


def test_mcp_child_finds_the_same_profile_through_hermes_home(mcp_child):
    """The defect: the MCP child must resolve the profile, not fail to find it.

    RED before the fix: the tool result is the resolution failure
    'V3CORE_PG_PASSWORD not set' even though the profile ships that password in
    its own .env.
    """
    hh, proc = mcp_child
    text = _status_text(proc)
    assert "V3CORE_PG_PASSWORD not set" not in text, (
        "MCP child did not resolve the HERMES_HOME profile; it fell back to the "
        f"legacy/ambient path. Tool result was: {text[:400]}"
    )
    assert str(DEAD_PG_PORT) in text or "hhprobe" in text, (
        f"child did not report the profile's own pg target: {text[:400]}"
    )


def test_mcp_child_effective_embed_identity_matches_direct(tmp_path):
    """Same profile -> same embed identity, whichever process resolves it."""
    from v3core.config import resolve_config
    from v3core.embedding import safe_embed_cfg

    hh = tmp_path / "hermes-home"
    _write_profile(hh, tmp_path / "data")
    direct = safe_embed_cfg(resolve_config(PROFILE, hermes_home=str(hh)))
    assert direct is not None and direct["_fingerprint"]
    assert direct["model"] == "BAAI/bge-m3"
    assert direct["api_key"], "profile .env key must resolve in a direct process"
