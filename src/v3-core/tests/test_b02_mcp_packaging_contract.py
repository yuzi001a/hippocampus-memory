"""B02 packaging contract: the public wheel must install a usable MCP SDK."""

from __future__ import annotations

import tomllib
from pathlib import Path


PROJECT = Path(__file__).parents[1]
PYPROJECT = PROJECT / "pyproject.toml"


def _project_dependencies() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def test_public_install_declares_mcp_sdk_compatible_with_fastmcp_1x():
    deps = _project_dependencies()
    assert "mcp>=1.0,<2" in deps, (
        "`v3-core mcp` imports mcp.server.fastmcp and must install the MCP 1.x API; "
        "an undeclared or unbounded dependency makes the clean public install unusable"
    )


def test_mcp_server_contract_does_not_claim_unbounded_latest_sdk():
    text = (PROJECT / "src" / "v3core" / "mcp_server.py").read_text(encoding="utf-8")
    assert "mcp>=1.0,<2" in text or "mcp 1." in text or "mcp SDK 1." in text
    assert "mcp>=1.0)." not in text, (
        "the runtime error must not tell users to install an unbounded latest SDK"
    )
