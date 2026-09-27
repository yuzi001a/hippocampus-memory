"""MCP_TOOL_PROFILE_SCOPING_LOST — the /tool + MCP dispatcher must stay core-scoped.

`serve._dispatch_tool` is the shared dispatcher behind both the HTTP `/tool`
route and the stdio MCP server. For every tool except `v3_prefetch` and
`v3_get_message_context` it called `tools.handle_tool_call(name, args)` with no
core-scoped kwargs, so the tool implementations each fell back to a bare
`resolve_config()` — i.e. the **default** profile — instead of the profile the
host actually booted.

The Hermes provider states the contract explicitly (v3hermes/provider.py):
"其余工具也必须复用当前 Runtime-backed Core/Pool；不能让 handler 内部再无参
构造 V3Core 或重新 resolve 生产配置." and passes
`core=`, `effective_config=`, `pool=`, `pg_pool=`, `runtime_context=True`.

Observable consequence before the fix: an MCP server started with
`--profile b02-canary` answered `v3_status` with the default profile's PG
(`5433/v3embeddings`, 121 cards) instead of the booted profile's
(`55510/b02canary`, 0 cards).
"""

import json

import pytest

from v3core import serve


class FakeCore:
    """Minimal core exposing the scoped values a real V3Core owns."""

    def __init__(self, config, pool=None, pg_pool=None):
        self.config = config
        self.pg_pool = pool
        if pg_pool is not None:
            self._pg_pool = pg_pool


@pytest.fixture()
def captured(monkeypatch):
    """Capture the kwargs the dispatcher forwards into tools.handle_tool_call."""
    seen = {}

    def fake_handle_tool_call(name, args, **kw):
        seen["name"] = name
        seen["args"] = args
        seen["kw"] = kw
        return json.dumps({"success": True, "echo": name})

    from v3core import tools as tools_pkg
    monkeypatch.setattr(tools_pkg, "handle_tool_call", fake_handle_tool_call)
    return seen


def test_dispatcher_forwards_core_scoped_config(captured):
    cfg = {"storage": {"pg": {"database": "b02canary", "port": 55510}}}
    pool = object()
    core = FakeCore(config=cfg, pool=pool)

    serve._dispatch_tool(core, "v3_status", {})

    kw = captured["kw"]
    assert kw.get("effective_config") is cfg, (
        "dispatcher dropped effective_config → tool would re-resolve the default profile"
    )
    assert kw.get("pool") is pool
    assert kw.get("pg_pool") is pool
    assert kw.get("core") is core


def test_dispatcher_marks_runtime_backed_calls(captured):
    core = FakeCore(config={"storage": {}}, pool=object())
    serve._dispatch_tool(core, "v3_search", {"query": "x"})
    assert captured["kw"].get("runtime_context") is True, (
        "a pool-backed core must be marked runtime-backed so tools never fall back"
    )


def test_dispatcher_legacy_core_without_pool_is_not_marked_runtime(captured):
    """A core with no pool must keep the legacy fallback path (no hard failure)."""
    core = FakeCore(config={"storage": {}}, pool=None)
    serve._dispatch_tool(core, "v3_status", {})
    kw = captured["kw"]
    assert kw.get("effective_config") is core.config
    assert kw.get("runtime_context") is False
    assert kw.get("pool") is None


def test_dispatcher_survives_core_without_scoped_attrs(captured):
    """Bare fakes (tests, offline tooling) must not crash the dispatcher."""

    class Bare:
        pass

    serve._dispatch_tool(Bare(), "v3_status", {})
    assert captured["name"] == "v3_status"
    assert captured["kw"].get("effective_config") is None


def test_prefetch_route_still_uses_the_core(monkeypatch):
    """v3_prefetch keeps its direct core call (unchanged by this fix)."""
    called = {}

    class Core:
        config = {"storage": {}}
        pg_pool = None

        def prefetch(self, query, limit=5):
            called["q"] = query
            called["limit"] = limit
            return []

    from v3core import prefetch as prefetch_mod
    monkeypatch.setattr(prefetch_mod, "format_prefetch", lambda r, f: "ok")
    out = serve._dispatch_tool(Core(), "v3_prefetch", {"query": "hello", "limit": 3})
    assert out == "ok"
    assert called == {"q": "hello", "limit": 3}
