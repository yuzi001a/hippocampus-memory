"""Booted-profile scope for one tool call — leaf module, no v3core imports.

Lives at the package root so ``v3core.config`` can read the bound scope without
importing ``v3core.tools`` (which imports config back: keeps the graph acyclic).

The dispatcher (serve._dispatch_tool / tools.handle_tool_call / the Hermes
provider) knows the booted V3Core. Nested tool helpers do not receive **kw, so
they used to resolve the DEFAULT profile on their own — a non-default profile
silently read and wrote default-profile data. `scoped()` binds the booted config
for the duration of one tool call; helpers ask `current_scope()` first and only
fall back to resolve_config() when no scope is bound (legacy/direct calls).
"""
from __future__ import annotations

import contextlib
import contextvars

_SCOPE = contextvars.ContextVar("v3_tool_scope_cfg", default=None)


def current_scope():
    """Booted config for the tool call in flight, or None when unbound."""
    return _SCOPE.get()


@contextlib.contextmanager
def scoped(cfg):
    """Bind ``cfg`` for one tool call; unbound calls are left untouched."""
    if cfg is None:
        yield None
        return
    token = _SCOPE.set(cfg)
    try:
        yield cfg
    finally:
        _SCOPE.reset(token)
