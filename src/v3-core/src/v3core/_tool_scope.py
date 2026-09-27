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
_BOOTED_PROFILE = contextvars.ContextVar("v3_booted_profile", default=None)


def current_scope():
    """Booted config for the tool call in flight, or None when unbound."""
    return _SCOPE.get()


def current_booted_profile() -> str:
    """Profile this process booted, or "" when no core has been booted."""
    return _BOOTED_PROFILE.get() or ""


def bind_booted(profile: str, cfg=None):
    """Bind the core this process booted, for its whole lifetime.

    ``config.resolve_config()`` with no profile means "the config this process
    booted" — not the literal ``default`` profile. Without this, core-internal
    leaves (topic recall, pools, embed helpers) resolved the default profile
    inside a non-default core: a canary process loaded 349 topics belonging to
    another install's profile into its recall pool, straight out of PG.

    Returns tokens for :func:`unbind_booted`; callers that never unbind simply
    keep the process-level binding, which is the normal case (one core per
    process).
    """
    tokens = (_BOOTED_PROFILE.set(profile or ""),)
    if cfg is not None:
        tokens = tokens + (_SCOPE.set(cfg),)
    return tokens


def unbind_booted(tokens) -> None:
    for tok in reversed(tokens or ()):
        try:
            if tok.var is _BOOTED_PROFILE:
                _BOOTED_PROFILE.reset(tok)
            else:
                _SCOPE.reset(tok)
        except Exception:  # noqa: BLE001
            pass


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
