# -*- coding: utf-8 -*-
"""Hermes host-contract adapter tests for V3HermesProvider.

What these pin
--------------
The host carries HERMES_HOME / profile isolation in a **ContextVar**, and
``hermes_constants.set_hermes_home_override`` deliberately never writes
``os.environ`` (one process serves several profiles).  A background job started
with a plain ``threading.Thread`` inherits nothing, so it silently resolves the
default profile.  ``agent.memory_provider.spawn_context_thread`` exists for this
and the host requires every memory-provider background job to use it.

These tests are written so they run in CI **without** Hermes installed: when the
host package is absent a faithful stub is installed in ``sys.modules`` first.
Against a real host the same tests exercise the true implementation, which is
where the contextvars propagation is actually proven.

Nothing here touches PostgreSQL, the network or the production home.
"""
from __future__ import annotations

import importlib
import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# Same worktree-resolution pattern as the sibling A0 test: make this tree's
# v3core importable regardless of any editable install.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]
_WORKTREE_V3CORE_SRC = _REPO_ROOT / "src" / "v3-core" / "src"
if str(_WORKTREE_V3CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_V3CORE_SRC))


# ── host surface ───────────────────────────────────────────────────────────

def _host_is_real() -> bool:
    try:
        import agent.memory_provider  # noqa: F401
    except Exception:
        return False
    return hasattr(importlib.import_module("agent.memory_provider"), "spawn_context_thread")


def _install_host_stub() -> None:
    """Minimal stand-in for ``agent.memory_provider`` (CI has no Hermes).

    Mirrors the REAL semantics: ``spawn_context_thread`` binds the spawner's
    contextvars, so a test that passes here would also pass against the host.
    """
    import contextvars

    if "agent" not in sys.modules:
        agent_pkg = type(sys)("agent")
        agent_pkg.__path__ = []  # namespace-ish; no filesystem lookup
        sys.modules["agent"] = agent_pkg

    module = type(sys)("agent.memory_provider")

    class MemoryProvider:  # noqa: D401 - stand-in for the host ABC
        pre_compress_checkpoint_api_version = 1

    def ctx_bound(fn):
        ctx = contextvars.copy_context()
        return lambda *a, **k: ctx.run(fn, *a, **k)

    def spawn_context_thread(target, *, name, daemon=True, args=(), kwargs=None):
        return threading.Thread(
            target=ctx_bound(target), args=args, kwargs=kwargs, name=name, daemon=daemon
        )

    module.MemoryProvider = MemoryProvider
    module.spawn_context_thread = spawn_context_thread
    module.ctx_bound = ctx_bound
    sys.modules["agent.memory_provider"] = module
    setattr(sys.modules["agent"], "memory_provider", module)


if not _host_is_real():
    _install_host_stub()

import v3hermes  # noqa: E402  (import after the host surface exists)
from v3core.runtime import RuntimeState  # noqa: E402


def _fresh_logger_records(caplog):
    caplog.set_level(logging.WARNING, logger="v3hermes")
    return caplog


# ── fakes ──────────────────────────────────────────────────────────────────

def _runtime(*, state=RuntimeState.ACTIVE, config=None, pool=None):
    return SimpleNamespace(
        state=state,
        effective_config=config if config is not None else {"profile": "fake"},
        pg_pool=pool if pool is not None else object(),
    )


def _provider(*, runtime=None, core=None, held=True):
    """A provider wired to fakes only — no Core, no pool, no filesystem."""
    provider = v3hermes.V3HermesProvider()
    provider._core = core if core is not None else SimpleNamespace(config={"c": 1})
    provider._runtime = runtime
    provider._runtime_held = held
    return provider


# ── 1. the caller's context survives into the background job ───────────────

def test_summary_worker_inherits_the_callers_contextvars():
    """A summary worker must see the ContextVar state of whoever queued it.

    This is the defect this round fixes: the worker was started with a bare
    ``threading.Thread``, so it began with an EMPTY context and would resolve
    the default HERMES_HOME/profile instead of the caller's.
    """
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("v3-test-marker")

    seen: list[object] = []
    done = threading.Event()

    def _work():
        seen.append(marker.get("<unset>"))
        done.set()

    token = marker.set("profile-B-home")
    try:
        thread = v3hermes._spawn_background_job(_work, name="v3-test")
        thread.start()
        assert done.wait(10), "background job never ran"
    finally:
        marker.reset(token)
    thread.join(timeout=10)

    assert seen == ["profile-B-home"]


def test_plain_thread_would_have_lost_the_context():
    """Control: the same probe on a bare thread shows what the fix prevents.

    Kept as an executable statement of the failure mode rather than a comment —
    if CPython ever changed thread-context inheritance, this test would say so
    instead of leaving the rationale silently stale.
    """
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("v3-test-control")
    seen: list[object] = []
    done = threading.Event()

    def _work():
        seen.append(marker.get("<unset>"))
        done.set()

    token = marker.set("profile-B-home")
    try:
        thread = threading.Thread(target=_work, name="v3-test-control", daemon=True)
        thread.start()
        assert done.wait(10)
    finally:
        marker.reset(token)
    thread.join(timeout=10)

    assert seen == ["<unset>"]


def test_two_profiles_do_not_share_context_through_the_spawner():
    """Property 3: concurrent jobs keep their OWN profile, not a shared one."""
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("v3-test-profiles")
    seen: list[tuple[str, str]] = []
    gate = threading.Barrier(2, timeout=10)

    def _make(tag: str):
        def _work():
            gate.wait()  # both jobs alive at the same time
            seen.append((tag, marker.get("<unset>")))
        return _work

    threads = []
    for tag, home in (("A", "/hermes/profiles/a"), ("B", "/hermes/profiles/b")):
        token = marker.set(home)
        try:
            threads.append(v3hermes._spawn_background_job(_make(tag), name=f"v3-{tag}"))
        finally:
            marker.reset(token)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert sorted(seen) == [
        ("A", "/hermes/profiles/a"),
        ("B", "/hermes/profiles/b"),
    ]


# ── 2. the host API is used, and absence is loud ───────────────────────────

def test_spawn_background_job_uses_the_host_api(monkeypatch):
    called = {}

    def _fake_spawn(target, *, name, daemon=True, args=(), kwargs=None):
        called["name"] = name
        called["daemon"] = daemon
        return threading.Thread(target=target, daemon=daemon)

    import agent.memory_provider as host

    monkeypatch.setattr(host, "spawn_context_thread", _fake_spawn)
    thread = v3hermes._spawn_background_job(lambda: None, name="v3-spy")

    assert called == {"name": "v3-spy", "daemon": True}
    assert thread.daemon is True


def test_spawn_background_job_warns_when_the_host_lacks_the_api(monkeypatch, caplog):
    """An old host must not break the provider — but the loss is not silent."""
    import agent.memory_provider as host

    def _boom(target, **_kw):
        raise ImportError("no spawn_context_thread here")

    monkeypatch.setattr(host, "spawn_context_thread", _boom)
    with _fresh_logger_records(caplog):
        thread = v3hermes._spawn_background_job(lambda: None, name="v3-fallback")

    assert isinstance(thread, threading.Thread)
    assert thread.daemon is True
    assert any("spawn_context_thread" in r.message % r.args if r.args else
               "spawn_context_thread" in r.message
               for r in caplog.records)


# ── 3. the worker uses the Runtime of its own generation ──────────────────

def test_summary_item_uses_the_current_runtime_and_pool(monkeypatch):
    """Property 2: the job resolves profile/config/pool from ITS runtime."""
    import v3core.session_summary as summary_mod

    captured = {}

    def _fake_summarize(session_id, messages=None, config=None, force=False, pool=None):
        captured["session_id"] = session_id
        captured["config"] = config
        captured["pool"] = type(pool).__name__
        return "ok"

    monkeypatch.setattr(summary_mod, "summarize_from_messages", _fake_summarize)

    pool = object()
    runtime = _runtime(config={"profile": "b"}, pool=pool)
    provider = _provider(runtime=runtime)
    with provider._summary_lock:
        provider._summary_generation = 3

    assert provider._run_summary_item("sess-b", [{"role": "user", "content": "hi"}]) is True
    assert captured["session_id"] == "sess-b"
    assert captured["config"] == {"profile": "b"}
    # The pool is handed over through the generation guard, never raw.
    assert captured["pool"] == "_SummaryPoolGuard"


# ── 4. a worker from a dead generation must not touch the Runtime ─────────

def test_stale_generation_never_reaches_the_runtime(monkeypatch):
    """Property 4: after shutdown/rebind, an old job is refused, not retried."""
    import v3core.session_summary as summary_mod

    touched = []
    monkeypatch.setattr(
        summary_mod, "summarize_from_messages",
        lambda *a, **k: touched.append(a),
    )

    runtime = _runtime()
    provider = _provider(runtime=runtime)
    with provider._summary_lock:
        provider._summary_generation = 5

    assert provider._run_summary_item("sess", [{"role": "user", "content": "x"}]) is False
    assert touched == []
    # And the guard itself refuses a lease once the generation has moved on.
    guard = v3hermes._SummaryPoolGuard(provider, 4, runtime, object())
    from v3core.runtime import StaleRuntimeError

    with pytest.raises(StaleRuntimeError):
        guard.lease()


def test_draining_runtime_refuses_new_summaries(monkeypatch):
    import v3core.session_summary as summary_mod

    touched = []
    monkeypatch.setattr(
        summary_mod, "summarize_from_messages",
        lambda *a, **k: touched.append(a),
    )
    runtime = _runtime(state=RuntimeState.DRAINING)
    provider = _provider(runtime=runtime)
    with provider._summary_lock:
        provider._summary_generation = 0

    assert provider._run_summary_item("sess", [{"role": "user", "content": "x"}]) is False
    assert touched == []


def test_shutdown_stops_accepting_and_bumps_the_generation():
    provider = _provider(runtime=_runtime(), held=False)
    provider._core = None  # exercise the clean shutdown path, no Core teardown
    before = provider._summary_generation

    provider.shutdown()

    assert provider._summary_accepting is False
    assert provider._summary_generation > before
    assert provider._summary_worker is None or not provider._summary_worker.is_alive()
