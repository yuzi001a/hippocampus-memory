"""Pytest conftest for the v3-core test suite.

Adds the sibling v3-hermes-plugin source tree to sys.path so the
focused packaging tests can import both ``v3core`` (engine) and
``v3hermes`` (plugin) from the local source checkout without requiring
a full ``pip install``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent          # .../src/v3-core
_SRC_DIR = _THIS_DIR.parent                           # .../src
_V3HERMES_SRC = _SRC_DIR / "v3-hermes-plugin" / "src"

# Idempotent insert (preserve earlier entries).
for p in (str(_V3HERMES_SRC),):
    if p not in sys.path:
        sys.path.insert(0, p)
