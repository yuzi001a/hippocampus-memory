"""Tool-scope helpers for the tools package.

The contextvar itself lives in ``v3core._tool_scope`` (leaf module: ``v3core.config``
reads it too, and must not import this package). Re-exported here so tool modules
keep a single import site.
"""
from __future__ import annotations

from .._tool_scope import current_scope, scoped

__all__ = ["current_scope", "scoped", "handbook_manager"]


def handbook_manager(cfg=None):
    """HandbookManager pinned to ``cfg`` when the constructor supports it."""
    from ..handbook import HandbookManager

    if cfg is not None:
        try:
            return HandbookManager(config=cfg)
        except TypeError:
            pass
    return HandbookManager()
