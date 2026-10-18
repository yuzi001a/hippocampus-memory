# -*- coding: utf-8 -*-
"""The manifest a host reads must describe the same package the wheel does.

`plugin.yaml` is what a plugin manager reads when it decides whether it can
install this provider's requirements. If it disagrees with the packaging
metadata -- or worse, lists the plugin itself, which does not exist on PyPI --
the manager ends up either under-installing or trying to fetch a package that
cannot exist, and reports the provider as unavailable for the wrong reason.

Reads the source tree, so it runs without the package being installed.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = _PLUGIN_ROOT / "src" / "v3hermes" / "plugin.yaml"
_PYPROJECT = _PLUGIN_ROOT / "pyproject.toml"


def _distribution_name(spec: str) -> str:
    """`v3-core>=4.0.0,<5.0.0` -> `v3-core` (PEP 503 normalised)."""
    name = re.split(r"[<>=!~\[;( ]", spec.strip(), maxsplit=1)[0]
    return name.lower().replace("_", "-")


def _pyproject_dependencies() -> list[str]:
    text = _PYPROJECT.read_text(encoding="utf-8")
    block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
    assert block, "pyproject.toml has no [project].dependencies list"
    return [m for m in re.findall(r'"([^"]+)"', block.group(1))]


def _manifest_dependencies() -> list[str]:
    manifest = yaml.safe_load(_MANIFEST.read_text(encoding="utf-8")) or {}
    return list(manifest.get("pip_dependencies") or [])


def test_manifest_dependencies_match_the_packaging_metadata():
    declared = {_distribution_name(s) for s in _manifest_dependencies()}
    packaged = {_distribution_name(s) for s in _pyproject_dependencies()}

    assert declared == packaged, (
        "plugin.yaml pip_dependencies and pyproject dependencies disagree: "
        f"manifest={sorted(declared)} packaging={sorted(packaged)}"
    )


def test_manifest_does_not_declare_itself_as_a_dependency():
    """The plugin is distributed as a local wheel; it is not on PyPI."""
    manifest = yaml.safe_load(_MANIFEST.read_text(encoding="utf-8")) or {}
    names = {_distribution_name(s) for s in (manifest.get("pip_dependencies") or [])}
    own = _distribution_name(manifest.get("name") or "")
    project_name = None
    text = _PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.M)
    if m:
        project_name = _distribution_name(m.group(1))

    assert "v3-hermes-plugin" not in names, (
        "plugin.yaml lists the plugin itself in pip_dependencies; a manager that "
        "acts on it would try to install a package that is not on PyPI"
    )
    assert own == "deep-memory-v3"  # manifest name is the provider id, not a dist
    assert project_name == "v3-hermes-plugin"


def test_declared_names_are_importable_runtime_requirements():
    """The declared set must be what the provider actually imports at runtime."""
    names = {_distribution_name(s) for s in _manifest_dependencies()}
    assert "v3-core" in names
    assert "pyyaml" in names  # v3hermes reads config via yaml
    assert "requests" in names  # v3core embeds/reranks over HTTP
