"""B02 packaging contract: the standalone MCP SDK must not block installation.

The MCP stdio server (`v3-core mcp`) is the only thing that imports the official
MCP SDK, and it uses the FastMCP 1.x API. Declaring that SDK as a *core*
requirement makes every install of the engine — including the memory-provider
wheel inside a host — carry a pin a host may already contradict. Hermes v0.22.2
pins `mcp==2.0.0`, so a core `mcp<2` requirement made the plugin member
unsatisfiable and the plugin manager disabled it.

The contract is therefore:
  * the core requirement set must NOT constrain mcp;
  * the `mcp` extra must exist and must pin the 1.x API the server actually uses;
  * the runtime error must tell the operator how to get it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT = Path(__file__).parents[1]
PYPROJECT = PROJECT / "pyproject.toml"


def _project() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def _core_names() -> set[str]:
    from packaging.requirements import Requirement

    return {Requirement(s).name.lower().replace("_", "-") for s in _project()["dependencies"]}


def test_mcp_sdk_is_not_a_core_requirement():
    assert "mcp" not in _core_names(), (
        "v3-core must not constrain the MCP SDK in [project].dependencies: only the "
        "standalone stdio server imports it, and a host that pins another MCP major "
        "(Hermes v0.22.2 pins mcp==2.0.0) then fails to resolve the plugin member"
    )


def test_mcp_extra_carries_the_fastmcp_1x_pin():
    extras = _project().get("optional-dependencies") or {}
    assert "mcp" in extras, "the standalone MCP server needs an installable extra"
    specs = [s.replace(" ", "") for s in extras["mcp"]]
    assert "mcp>=1.0,<2" in specs, (
        "the extra must own the FastMCP 1.x pin; v3core.mcp_server imports "
        f"mcp.server.fastmcp (got {specs})"
    )


def test_core_requirements_still_resolve_on_their_own():
    """Removing mcp must not have been done by removing the engine's own deps."""
    names = _core_names()
    for required in ("pyyaml", "numpy", "requests", "psycopg2-binary", "pgvector",
                     "jieba", "openai"):
        assert required in names, f"{required} disappeared from the core requirements"


def test_mcp_server_contract_does_not_claim_unbounded_latest_sdk():
    text = (PROJECT / "src" / "v3core" / "mcp_server.py").read_text(encoding="utf-8")
    assert "mcp>=1.0,<2" in text or "mcp 1." in text or "mcp SDK 1." in text
    assert "mcp>=1.0)." not in text, (
        "the runtime error must not tell users to install an unbounded latest SDK"
    )
