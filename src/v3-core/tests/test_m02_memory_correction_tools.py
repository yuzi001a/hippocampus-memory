# -*- coding: utf-8 -*-
"""M02 — unified correction / read tool contracts (RED→GREEN anchors).

Design baseline: docs/M01-M02-MEMORY-CORRECTION-DESIGN.md §"M02 public surface".

Scope of this file (tool layer only; the canonical writer/reader in
``v3core.active_memory_store`` is owned by the M01 child):

  * ``v3_update(action='correct', ...)`` — schema contract, fail-closed
    input validation, ``authority='user_explicit'`` forwarding, booted
    profile injection, receipt shape, truthful propagation of a canonical
    failure (and specifically: a missing target never produces a new
    standalone memory).
  * ``v3_get(target='memory', ...)`` — canonical reader routing for
    ``mode='current'|'history'`` and ``include_history``.
  * legacy forward paths preserved: ``v3_get(target='hm')`` only forwards
    history parameters when the caller supplied them, and ``hm_get``
    keeps its exact legacy payload for an active exact hit with no
    history parameters (see tests/test_b02_active_memory_source_read.py).
  * no new tool / registry entry — the public surface stays single
    sourced on v3_update / v3_get / hm_get.

The canonical writer/reader classes are mocked at the constructor seam on
purpose (``memory_correction._writer_cls`` / ``._reader_cls``) so these
tests pin the *tool* contract without pretending to exercise the M01
store transaction. The store's own RED→GREEN suite is a separate file.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from v3core.active_memory_store import (
    ActiveMemoryWriter,
    _PgStoreLeaseAdapter,
    _acquire_lease,
)
from v3core.tools import TOOL_REGISTRY
from v3core.tools import get_tool as get_tool_mod
from v3core.tools import memory_correction as mc
from v3core.tools.api_delete import _build_writer_and_pool
from v3core.tools.api_get import V3_GET_SCHEMA, handle_v3_get
from v3core.tools.api_update import V3_UPDATE_SCHEMA, handle_v3_update
from v3core.tools.get_tool import HM_GET_SCHEMA, handle_hm_get

_HERE = Path(__file__).resolve().parent
_API_UPDATE_PY = _HERE.parent / "src" / "v3core" / "tools" / "api_update.py"
_API_GET_PY = _HERE.parent / "src" / "v3core" / "tools" / "api_get.py"
_MEMORY_CORRECTION_PY = _HERE.parent / "src" / "v3core" / "tools" / "memory_correction.py"


# ── seams ────────────────────────────────────────────────────────────────────


class _Seam:
    """A stand-in for the canonical writer/reader *instance*.

    Exposes the two canonical methods (``correct`` / ``read_version``),
    records the constructor kwargs (so the tests can assert which booted
    pool/config was injected) and every call's args/kwargs.
    """

    def __init__(self, result=None):
        self.ctor_kwargs: dict = {}
        self.calls: list[tuple] = []
        self._result = result if result is not None else {"success": True}

    def _record(self, args, kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result

    def correct(self, *args, **kwargs):
        return self._record(args, kwargs)

    def read_version(self, *args, **kwargs):
        return self._record(args, kwargs)

    @property
    def last_kwargs(self) -> dict:
        assert self.calls, "canonical seam was never invoked"
        return self.calls[-1][1]

    @property
    def last_args(self) -> tuple:
        assert self.calls, "canonical seam was never invoked"
        return self.calls[-1][0]


class _Exploding:
    """Constructing the canonical class on this path is a contract break."""

    def __init__(self, name: str):
        self._name = name

    def __call__(self, *a, **kw):
        raise AssertionError(f"{self._name} must not be constructed on this path")


def _install(cls_name: str, result=None) -> _Seam:
    seam = _Seam(result)

    def _factory(**ctor_kwargs):
        seam.ctor_kwargs = ctor_kwargs
        return seam

    setattr(mc, cls_name, _factory)
    return seam


def _writer(result=None) -> _Seam:
    return _install("_writer_cls", result)


def _payload(memory_id: str, content: str, *, status: str = "active") -> dict:
    """One canonical ``explicit_memories`` row as the reader returns it."""
    return {
        "memory_id": memory_id,
        "category": "decisions",
        "title": memory_id,
        "content": content,
        "tags": [],
        "provenance": {"source": "user"},
        "status": status,
        "created_at": "2026-10-03T04:00:00+00:00",
        "updated_at": "2026-10-03T04:05:06+00:00",
    }


def _reader(result=None) -> _Seam:
    return _install("_reader_cls", result)


def _forbid(cls_name: str) -> None:
    setattr(mc, cls_name, _Exploding(cls_name))


@pytest.fixture(autouse=True)
def _no_resolve_config(monkeypatch):
    """Booted-profile isolation: the tool layer may never call a no-arg
    ``resolve_config()`` when core/config/pool/runtime_context is injected.
    """
    import v3core.config as config_mod

    def _refuse(*a, **kw):
        raise AssertionError(
            "no-arg resolve_config() is forbidden on an injected tool path"
        )

    monkeypatch.setattr(config_mod, "resolve_config", _refuse)


@pytest.fixture(autouse=True)
def _restore_canonical_seams():
    """Restore the canonical writer/reader seam callables after every test.

    ``_install`` / ``_forbid`` mutate ``mc._writer_cls`` / ``mc._reader_cls``
    process-globally. Without this teardown a later suite (or a later test in
    this file) inherits a mock or an exploding seam — pure tests would poison
    every following test. The originals are saved once per test and put back
    in a ``finally``, so a failing test still restores them.
    """
    original_writer = mc._writer_cls
    original_reader = mc._reader_cls
    try:
        yield
    finally:
        mc._writer_cls = original_writer
        mc._reader_cls = original_reader


def _boot(**over):
    kw = {
        "effective_config": {"basePath": "C:/isolated/profile"},
        "pool": object(),
        "runtime_context": True,
    }
    kw.update(over)
    return kw


# ── schema contracts ────────────────────────────────────────────────────────


def test_v3_update_schema_exposes_correct_action_and_required_fields():
    props = V3_UPDATE_SCHEMA["parameters"]["properties"]
    assert "correct" in props["action"]["enum"]
    for key in (
        "memory_id",
        "replacement_content",
        "replacement_title",
        "replacement_tags",
        "correction_reason",
        "correction_source_id",
        "effective_at",
    ):
        assert key in props, f"v3_update schema is missing {key}"
    # Legacy actions unchanged.
    for legacy in ("organize", "dedup", "sync", "delete", "topic_maintain", "affinity"):
        assert legacy in props["action"]["enum"]
    # additionalProperties stays False — every correct input must be declared.
    assert V3_UPDATE_SCHEMA["parameters"]["additionalProperties"] is False


def test_v3_update_correct_description_limits_to_explicit_authority():
    props = V3_UPDATE_SCHEMA["parameters"]["properties"]
    blob = " ".join(
        [
            V3_UPDATE_SCHEMA["description"],
            props["action"]["description"],
            props["memory_id"]["description"],
            props["replacement_content"]["description"],
        ]
    ).lower()
    assert "correct" in blob
    # The authorization limit must be on the public contract (bilingual —
    # these schemas are read by Chinese and English hosts alike): an
    # inferred / merely newer statement is NOT authorization.
    assert "inference" in blob or "inferred" in blob
    assert "推断" in blob
    assert "user" in blob or "用户" in blob


def test_v3_get_schema_exposes_memory_target_and_history_parameters():
    props = V3_GET_SCHEMA["parameters"]["properties"]
    assert "memory" in props["target"]["enum"]
    for legacy in ("status", "search", "overview", "hand帐", "prefetch",
                   "message", "handbook", "hm"):
        assert legacy in props["target"]["enum"]
    assert "memory_id" in props
    assert "mode" in props
    assert "include_history" in props
    assert set(props["mode"]["enum"]) == {"current", "history"}


def test_no_new_independent_correction_tool_is_registered():
    """No new tool / registry entry — the public surface stays v3_* + hm_*.

    ``v3_topic_correct`` is a pre-existing *topic* tool and must stay; the
    contract is that M01/M02 did not add a second, separate correction
    tool next to v3_update.
    """
    names = set(TOOL_REGISTRY)
    pre_existing = {"v3_topic_correct"}
    new = {n for n in names if "correct" in n} - pre_existing
    assert not new, f"a new correction tool was registered: {sorted(new)}"
    assert not any(n.startswith("v4_") or n.endswith("_daemon") for n in names)
    assert pre_existing <= names, "pre-existing v3_topic_correct disappeared"
    # The three pre-existing entry points keep their handlers.
    assert TOOL_REGISTRY["v3_update"].handler is handle_v3_update
    assert TOOL_REGISTRY["v3_get"].handler is handle_v3_get
    assert TOOL_REGISTRY["hm_get"].handler is handle_hm_get


def test_effective_at_is_audit_valid_from_only_not_scheduling():
    """M01 does not schedule future activation or implement as-of reasoning.

    Only executable code is scanned: the module docstring *documents* that
    these are out of scope, so a naive whole-file substring scan would
    flag the disclaimer itself.
    """
    import ast

    source = _MEMORY_CORRECTION_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_ids.add(id(first.value))

    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_ids
    ]
    code_only = " ".join(literals).lower()
    for banned in ("as_of", "future_activation", "activate_at", "scheduler",
                   "cron", "sleep", "while true"):
        assert banned not in code_only, f"tool layer must not implement {banned!r}"


def test_tool_layer_performs_no_dml_against_explicit_memories():
    """The write boundary stays canonical: no scattered SQL in the tool layer.

    ``"explicit_memories"`` is legitimate as a read *source label* in a
    response payload (get_tool.py already returns exactly that), so the
    contract is about DML/SQL statements, not about the bare string.
    """
    import re

    for path in (_API_UPDATE_PY, _API_GET_PY, _MEMORY_CORRECTION_PY):
        text = path.read_text(encoding="utf-8")
        code = re.sub(r'"""[\s\S]*?"""', "", text)
        code = re.sub(r"'''[\s\S]*?'''", "", code)
        code = re.sub(r'"[^"\n]*"', '""', code)
        code = re.sub(r"'[^'\n]*'", "''", code)
        low = code.lower()
        for banned in ("insert into", "update public.", "delete from",
                       "alter table", "create table", "drop table",
                       "select "):
            assert banned not in low, f"{path.name} performs raw SQL: {banned}"


# ── v3_update(action='correct') ─────────────────────────────────────────────


def test_correct_requires_memory_id_and_never_constructs_the_writer():
    _forbid("_writer_cls")
    out = json.loads(handle_v3_update({"action": "correct"}, **_boot()))
    assert out["success"] is False
    assert "memory_id" in out["error"]


def test_correct_requires_non_empty_replacement_content():
    writer = _writer()
    for content in ("", "   "):
        out = json.loads(
            handle_v3_update(
                {"action": "correct", "memory_id": "mem_a", "replacement_content": content},
                **_boot(),
            )
        )
        assert out["success"] is False
        assert "replacement_content" in out["error"]
    assert writer.calls == [], "an explicit empty content must not reach the writer"


def test_correct_rejects_explicit_empty_title_and_invalid_tags():
    writer = _writer()
    base = {"action": "correct", "memory_id": "mem_a", "replacement_content": "B"}
    for extra, needle in (
        ({"replacement_title": ""}, "replacement_title"),
        ({"replacement_tags": ["ok", 3]}, "replacement_tags"),
        ({"replacement_tags": "not-a-list"}, "replacement_tags"),
        ({"correction_reason": ""}, "correction_reason"),
        ({"correction_source_id": ""}, "correction_source_id"),
    ):
        out = json.loads(handle_v3_update({**base, **extra}, **_boot()))
        assert out["success"] is False, extra
        assert needle in out["error"], (extra, out)
    assert writer.calls == []


def test_correct_requires_timezone_aware_effective_at():
    writer = _writer()
    base = {"action": "correct", "memory_id": "mem_a", "replacement_content": "B"}
    for bad in ("2026-10-03", "2026-10-03 12:00:00", "not-a-timestamp"):
        out = json.loads(handle_v3_update({**base, "effective_at": bad}, **_boot()))
        assert out["success"] is False, bad
        assert "effective_at" in out["error"], (bad, out)
    assert writer.calls == []
    # A timezone-aware timestamp is forwarded verbatim (audit valid-from).
    ok = json.loads(
        handle_v3_update({**base, "effective_at": "2026-10-03T12:00:00+08:00"}, **_boot())
    )
    assert ok["success"] is True
    assert writer.last_kwargs["effective_at"] == "2026-10-03T12:00:00+08:00"


def test_correct_refuses_conflicting_memory_and_source_ids():
    writer = _writer()
    out = json.loads(
        handle_v3_update(
            {
                "action": "correct",
                "memory_id": "mem_a",
                "source_id": "mem_b",
                "replacement_content": "B",
            },
            **_boot(),
        )
    )
    assert out["success"] is False
    assert "source_id" in out["error"]
    assert writer.calls == []


def test_correct_forwards_user_explicit_authority_and_booted_scope():
    writer = _writer({"success": True, "old_memory_id": "mem_a", "new_memory_id": "mem_b"})
    pool = object()
    cfg = {"basePath": "C:/isolated"}
    out = json.loads(
        handle_v3_update(
            {
                "action": "correct",
                "memory_id": "mem_a",
                "replacement_content": "B",
                "replacement_title": "t",
                "replacement_tags": ["x"],
                "correction_reason": "user said A is cancelled",
                "correction_source_id": "42",
            },
            effective_config=cfg,
            pool=pool,
            runtime_context=True,
        )
    )
    assert out["success"] is True
    assert writer.last_args == ("mem_a",)
    kwargs = writer.last_kwargs
    assert kwargs["authority"] == "user_explicit"
    assert kwargs["replacement_content"] == "B"
    assert kwargs["replacement_title"] == "t"
    assert kwargs["replacement_tags"] == ["x"]
    assert kwargs["correction_reason"] == "user said A is cancelled"
    assert kwargs["correction_source_id"] == "42"
    # Omitted optionals are omitted, never invented.
    assert kwargs["effective_at"] is None
    # The booted pool/config is exactly what the tool layer handed to the writer.
    assert writer.ctor_kwargs.get("pool") is pool
    assert writer.ctor_kwargs.get("config") is cfg


def test_correct_omitted_title_and_tags_are_none_so_the_canonical_writer_inherits():
    writer = _writer({"success": True})
    json.loads(
        handle_v3_update(
            {"action": "correct", "memory_id": "mem_a", "replacement_content": "B"},
            **_boot(),
        )
    )
    assert writer.last_kwargs["replacement_title"] is None
    assert writer.last_kwargs["replacement_tags"] is None
    assert writer.last_kwargs["correction_reason"] is None
    assert writer.last_kwargs["correction_source_id"] is None


def test_correct_receipt_reports_which_memory_changed():
    _writer(
        {
            "success": True,
            "old_memory_id": "mem_a",
            "new_memory_id": "mem_b",
            "relation": {"relation_type": "supersedes", "relation_id": "rel_1",
                         "recorded_at": "2026-10-03T04:05:06+00:00"},
            "old_status": "archived",
            "new_status": "active",
            "deduplicated": False,
        }
    )
    out = json.loads(
        handle_v3_update(
            {"action": "correct", "memory_id": "mem_a", "replacement_content": "B"},
            **_boot(),
        )
    )
    assert out["success"] is True
    assert out["action"] == "correct"
    assert out["authority"] == "user_explicit"
    assert out["old_memory_id"] == "mem_a"
    assert out["new_memory_id"] == "mem_b"
    # The persisted relation type is 'supersedes' (RELATION_TYPE_CORRECTS is a
    # compatibility SYMBOL alias only, never a third persisted type).
    assert out["relation"]["relation_type"] == "supersedes"
    # The receipt carries the server-side recorded_at verbatim.
    assert out["relation"]["recorded_at"] == "2026-10-03T04:05:06+00:00"
    assert out["old_status"] == "archived"
    assert out["new_status"] == "active"
    assert out["deduplicated"] is False
    assert out["correction_source"]["provenance"] in (
        "explicit_tool_request",
        "conversation_stream",
    )
    assert out["effective_at"] is None


def test_correct_idempotent_replay_is_surfaced_not_hidden():
    _writer({"success": True, "deduplicated": True, "old_memory_id": "mem_a",
             "new_memory_id": "mem_b"})
    out = json.loads(
        handle_v3_update(
            {"action": "correct", "memory_id": "mem_a", "replacement_content": "B"},
            **_boot(),
        )
    )
    assert out["success"] is True
    assert out["deduplicated"] is True


def test_correct_propagates_a_missing_target_truthfully():
    """NOT_FOUND must not degrade into a fresh standalone memory."""
    _writer({"success": False, "code": "NOT_FOUND", "error": "target memory not found"})
    out = json.loads(
        handle_v3_update(
            {"action": "correct", "memory_id": "mem_missing", "replacement_content": "B"},
            **_boot(),
        )
    )
    assert out["success"] is False
    assert out["code"] == "NOT_FOUND"
    assert "not found" in out["error"].lower()
    assert out.get("new_memory_id") in (None, "")


def test_correct_propagates_a_conflict_without_a_second_successor():
    _writer(
        {
            "success": False,
            "code": "CORRECTION_CONFLICT",
            "error": "target already corrected to mem_b",
            "current_memory_id": "mem_b",
        }
    )
    out = json.loads(
        handle_v3_update(
            {"action": "correct", "memory_id": "mem_a", "replacement_content": "C"},
            **_boot(),
        )
    )
    assert out["success"] is False
    assert out["code"] == "CORRECTION_CONFLICT"
    assert out["current_memory_id"] == "mem_b"


def test_correct_unexpected_canonical_shape_fails_closed():
    _writer(result=["not", "a", "mapping"])
    out = json.loads(
        handle_v3_update(
            {"action": "correct", "memory_id": "mem_a", "replacement_content": "B"},
            **_boot(),
        )
    )
    assert out["success"] is False
    assert out["error"]


def test_correct_without_any_injected_scope_still_reaches_the_canonical_writer(monkeypatch):
    """No injection at all is a legacy in-process call — allowed, but the
    writer still must receive user_explicit authority."""
    writer = _writer({"success": True})
    import v3core.config as config_mod

    monkeypatch.setattr(config_mod, "resolve_config", lambda *a, **kw: {"basePath": "x"})
    out = json.loads(
        handle_v3_update(
            {"action": "correct", "memory_id": "mem_a", "replacement_content": "B"}
        )
    )
    assert out["success"] is True
    assert writer.last_kwargs["authority"] == "user_explicit"


def test_correct_runtime_context_without_pool_fails_closed():
    _forbid("_writer_cls")
    out = json.loads(
        handle_v3_update(
            {"action": "correct", "memory_id": "mem_a", "replacement_content": "B"},
            effective_config={"basePath": "x"},
            pool=None,
            runtime_context=True,
        )
    )
    assert out["success"] is False
    assert "pool" in out["error"].lower()


def test_legacy_v3_update_actions_are_untouched():
    out = json.loads(handle_v3_update({"action": "definitely-not-an-action"}, **_boot()))
    assert out["success"] is False
    assert "unknown action" in out["error"]


# ── v3_get(target='memory') ─────────────────────────────────────────────────


def test_memory_target_requires_memory_id():
    _forbid("_reader_cls")
    out = json.loads(handle_v3_get({"target": "memory"}, **_boot()))
    assert out["success"] is False
    assert "memory_id" in out["error"]


def test_memory_target_defaults_to_current_mode_without_history():
    """The canonical reader answer is NESTED (``requested`` / ``memory`` /
    ``history``) — there is no flat top-level ``content``."""
    reader = _reader(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "current",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": "mem_b",
            "is_current": False,
            "requested": _payload("mem_a", "A", status="archived"),
            "memory": _payload("mem_b", "B", status="active"),
            "relation": {"relation_type": "supersedes", "from_memory_id": "mem_a",
                         "to_memory_id": "mem_b", "recorded_at": "2026-10-03T04:05:06+00:00"},
        }
    )
    out = json.loads(
        handle_v3_get({"target": "memory", "memory_id": "mem_a"}, **_boot())
    )
    assert out["success"] is True
    assert reader.last_args == ("mem_a",)
    assert reader.last_kwargs["mode"] == "current"
    assert reader.last_kwargs["include_history"] is False
    assert out["requested_memory_id"] == "mem_a"
    assert out["resolved_memory_id"] == "mem_b"
    assert out["is_current"] is False
    # Nested canonical payloads are copied through unchanged.
    assert out["memory"]["content"] == "B"
    assert out["requested"]["content"] == "A"
    assert out["relation"]["recorded_at"] == "2026-10-03T04:05:06+00:00"
    assert "content" not in out


def test_memory_target_forwards_history_mode_and_include_history():
    reader = _reader(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "history",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": "mem_b",
            "is_current": False,
            "requested": _payload("mem_a", "A", status="archived"),
            "memory": _payload("mem_b", "B", status="active"),
            "history": {
                "versions": [
                    {"memory_id": "mem_a", "label": "superseded", "is_current": False},
                    {"memory_id": "mem_b", "label": "current", "is_current": True},
                ],
                "edges": [],
                "current_memory_id": "mem_b",
            },
        }
    )
    out = json.loads(
        handle_v3_get(
            {"target": "memory", "memory_id": "mem_a", "mode": "history",
             "include_history": True},
            **_boot(),
        )
    )
    assert out["success"] is True
    assert reader.last_kwargs["mode"] == "history"
    assert reader.last_kwargs["include_history"] is True
    assert out["mode"] == "history"
    assert out["history"]["current_memory_id"] == "mem_b"
    assert [v["label"] for v in out["history"]["versions"]] == ["superseded", "current"]
    # EXACT requested payload is the historical answer.
    assert out["requested"]["content"] == "A"


def test_memory_target_rejects_an_unknown_mode():
    reader = _reader({"success": True})
    out = json.loads(
        handle_v3_get(
            {"target": "memory", "memory_id": "mem_a", "mode": "latest-wins"},
            **_boot(),
        )
    )
    assert out["success"] is False
    assert reader.calls == []


def test_memory_target_unknown_id_fails_truthfully():
    _reader({"success": False, "code": "NOT_FOUND", "error": "memory_id not found"})
    out = json.loads(
        handle_v3_get({"target": "memory", "memory_id": "mem_nope"}, **_boot())
    )
    assert out["success"] is False
    assert out["code"] == "NOT_FOUND"


def test_memory_target_canonical_exception_is_not_silently_empty():
    _reader(result=RuntimeError('relation "memory_relations" does not exist'))
    out = json.loads(
        handle_v3_get({"target": "memory", "memory_id": "mem_a"}, **_boot())
    )
    assert out["success"] is False
    assert "memory_relations" in out["error"]


def test_memory_target_uses_the_injected_booted_pool():
    reader = _reader({"success": True, "content": "B"})
    pool = object()
    cfg = {"basePath": "x"}
    json.loads(
        handle_v3_get(
            {"target": "memory", "memory_id": "mem_a"},
            effective_config=cfg,
            pool=pool,
            runtime_context=True,
        )
    )
    assert reader.ctor_kwargs.get("pool") is pool
    assert reader.ctor_kwargs.get("config") is cfg


def test_memory_target_runtime_context_without_pool_fails_closed():
    _forbid("_reader_cls")
    out = json.loads(
        handle_v3_get(
            {"target": "memory", "memory_id": "mem_a"},
            effective_config={"basePath": "x"},
            pool=None,
            runtime_context=True,
        )
    )
    assert out["success"] is False
    assert "pool" in out["error"].lower()


# ── legacy forward paths ────────────────────────────────────────────────────


def test_hm_target_forwards_only_supplied_history_parameters(monkeypatch):
    seen: list[dict] = []

    def _fake(args, **kw):
        seen.append(args)
        return json.dumps({"success": True, "source_id": args.get("source_id", "")})

    monkeypatch.setattr(get_tool_mod, "handle_hm_get", _fake)
    json.loads(handle_v3_get({"target": "hm", "source_id": "mem_a"}, **_boot()))
    assert seen[-1] == {"source_id": "mem_a"}

    json.loads(
        handle_v3_get(
            {"target": "hm", "source_id": "mem_a", "mode": "history",
             "include_history": True},
            **_boot(),
        )
    )
    assert seen[-1] == {
        "source_id": "mem_a",
        "mode": "history",
        "include_history": True,
    }


class _Row(dict):
    pass


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.queries.append(" ".join(str(sql).split()))
        return None

    def fetchone(self):
        return self.row

    def fetchall(self):
        return [self.row] if self.row is not None else []


class _Conn:
    def __init__(self, row):
        self._cur = _Cursor(row)

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def rollback(self):
        pass


class _Lease:
    def __init__(self, row):
        self.conn = _Conn(row)

    def __enter__(self):
        return self.conn

    def __exit__(self, *_):
        return False


class _Pg:
    def __init__(self, row):
        self.row = row

    def lease(self):
        return _Lease(self.row)


class _Core:
    def __init__(self, row):
        self.pg = _Pg(row)
        self.config = {"basePath": "x"}
        self._pg_pool = None

    def get_message_context(self, source_id):
        return json.dumps({"success": False, "source_id": source_id})


def _row(status="active"):
    return _Row(
        memory_id="mem_a",
        category="decisions",
        title="部署方案",
        content="部署方案采用 A。",
        tags=["deploy"],
        provenance={"source": "user"},
        status=status,
        created_at=None,
        updated_at=None,
    )


def test_hm_get_active_exact_hit_keeps_the_legacy_payload():
    """Regression pin for tests/test_b02_active_memory_source_read.py: an
    active exact hit with no history parameters keeps the legacy raw read
    (same content / metadata shape), and never reaches the canonical
    reader."""
    reader = _reader({"success": True, "content": "SHOULD NOT BE USED"})
    out = json.loads(handle_hm_get({"source_id": "mem_a"}, core=_Core(_row())))
    assert out["success"] is True
    assert out["source"] == "explicit_memories"
    assert out["content"] == "部署方案采用 A。"
    assert out["metadata"]["title"] == "部署方案"
    assert out["metadata"]["category"] == "decisions"
    assert out["metadata"]["status"] == "active"
    assert reader.calls == []


def test_hm_get_archived_exact_hit_resolves_current_instead_of_archived_payload():
    reader = _reader(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "current",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": "mem_b",
            "is_current": False,
            "requested": _payload("mem_a", "部署方案采用 A。", status="archived"),
            "memory": _payload("mem_b", "部署方案改用 B。", status="active"),
        }
    )
    out = json.loads(
        handle_hm_get({"source_id": "mem_a"}, core=_Core(_row(status="archived")))
    )
    assert out["success"] is True
    assert reader.last_args == ("mem_a",)
    assert reader.last_kwargs["mode"] == "current"
    # Plain source inspection must not answer with the superseded payload.
    assert out["content"] == "部署方案改用 B。"
    assert out["requested_memory_id"] == "mem_a"
    assert out["resolved_memory_id"] == "mem_b"
    assert out["requested"]["content"] == "部署方案采用 A。"


def test_hm_get_active_exact_hit_with_explicit_history_uses_canonical_reader():
    reader = _reader(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "history",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": "mem_b",
            "is_current": False,
            "requested": _payload("mem_a", "部署方案采用 A。", status="archived"),
            "memory": _payload("mem_b", "部署方案改用 B。", status="active"),
            "history": {
                "versions": [
                    {"memory_id": "mem_a", "label": "superseded", "is_current": False,
                     "status": "archived"},
                    {"memory_id": "mem_b", "label": "current", "is_current": True,
                     "status": "active"},
                ],
                "edges": [],
                "current_memory_id": "mem_b",
            },
        }
    )
    out = json.loads(
        handle_hm_get(
            {"source_id": "mem_a", "mode": "history", "include_history": True},
            core=_Core(_row()),
        )
    )
    assert out["success"] is True
    assert reader.last_kwargs["mode"] == "history"
    assert reader.last_kwargs["include_history"] is True
    assert out["mode"] == "history"
    # history mode answers with the EXACT requested payload, labelled.
    assert out["content"] == "部署方案采用 A。"
    assert out["requested"]["status"] == "archived"
    assert [v["memory_id"] for v in out["history"]["versions"]] == ["mem_a", "mem_b"]
    assert [v["label"] for v in out["history"]["versions"]] == ["superseded", "current"]


def test_hm_get_schema_declares_the_optional_history_parameters():
    props = HM_GET_SCHEMA["parameters"]["properties"]
    assert "mode" in props
    assert "include_history" in props
    assert HM_GET_SCHEMA["parameters"]["required"] == ["source_id"]


def test_hm_get_non_explicit_paths_are_unchanged():
    """A non explicit-memory source_id must still fall through to the
    legacy message / handbook / card chain, and never reach the canonical
    reader."""
    reader = _reader({"success": True, "content": "NOPE"})
    out = json.loads(handle_hm_get({"source_id": "msg_1234"}, core=_Core(None)))
    assert out["success"] is False
    assert "not found" in out["error"].lower()
    assert reader.calls == []


# ── frozen mode contract: withdraw ─────────────────────────────────────────
#
# ``correct(memory_id, replacement_content=None, *, mode='replace'|…)`` —
# an OMITTED mode still means replace for existing callers. Withdraw MUST
# NOT create a replacement and MUST reject any replacement payload
# (content/title/tags), even an empty one, rather than reinterpret it.


def _correct(args, **over):
    kw = {
        "effective_config": {"basePath": "C:/isolated/profile"},
        "pool": object(),
        "runtime_context": True,
    }
    kw.update(over)
    return json.loads(handle_v3_update({"action": "correct", **args}, **kw))


def test_v3_update_schema_declares_the_mode_enum_with_content_optional():
    props = V3_UPDATE_SCHEMA["parameters"]["properties"]
    assert set(props["mode"]["enum"]) == {"replace", "withdraw"}
    # ``replacement_content`` is required ONLY for replace, so it is no
    # longer a schema-level required field; required stays ["action"].
    assert V3_UPDATE_SCHEMA["parameters"]["required"] == ["action"]
    assert props["replacement_content"].get("required_for") == "mode=replace"
    # additionalProperties stays False — mode is the only new field.
    assert V3_UPDATE_SCHEMA["parameters"]["additionalProperties"] is False


def test_correct_withdraw_needs_no_replacement_and_creates_none():
    writer = _writer(
        {
            "success": True,
            "durable": True,
            "deduplicated": False,
            "old_memory_id": "mem_a",
            "new_memory_id": None,
            "old_status": "withdrawn",
            "new_status": None,
            "current_memory_id": None,
            "relation": {"relation_type": "withdraws", "from_memory_id": "mem_a",
                         "to_memory_id": None,
                         "recorded_at": "2026-10-03T04:05:06+00:00"},
        }
    )
    out = _correct({"memory_id": "mem_a", "mode": "withdraw"})
    assert out["success"] is True
    assert out["mode"] == "withdraw"
    kwargs = writer.last_kwargs
    # Withdraw forwards NO replacement payload at all.
    assert kwargs["mode"] == "withdraw"
    assert "replacement_content" not in kwargs
    assert kwargs.get("replacement_content") is None
    assert kwargs["authority"] == "user_explicit"
    # A withdrawn chain has no successor — never manufactured.
    assert out["new_memory_id"] is None
    assert out["new_status"] is None
    assert out.get("current_memory_id") is None
    assert out["relation"]["relation_type"] == "withdraws"
    assert out["relation"]["recorded_at"] == "2026-10-03T04:05:06+00:00"
    assert out["durable"] is True


def test_correct_withdraw_rejects_any_replacement_payload_even_empty():
    writer = _writer()
    for extra in (
        {"replacement_content": "B"},
        {"replacement_content": ""},
        {"replacement_title": ""},
        {"replacement_title": "t"},
        {"replacement_tags": []},
        {"replacement_tags": ["x"]},
    ):
        out = _correct({"memory_id": "mem_a", "mode": "withdraw", **extra})
        assert out["success"] is False, extra
        assert "withdraw" in out["error"].lower(), (extra, out)
    # None must never be re-interpreted as a request to replace.
    assert writer.calls == []


def test_correct_rejects_an_unknown_mode_without_touching_the_writer():
    writer = _writer()
    out = _correct({"memory_id": "mem_a", "replacement_content": "B", "mode": "merge"})
    assert out["success"] is False
    assert "mode" in out["error"]
    assert writer.calls == []


def test_correct_omitted_mode_keeps_the_legacy_replace_contract():
    """Frozen: an omitted mode means replace for every existing caller —
    but it relies on the canonical default instead of being put on the
    wire (only an explicit, validated mode is forwarded)."""
    writer = _writer({"success": True, "old_memory_id": "mem_a",
                      "new_memory_id": "mem_b"})
    out = _correct({"memory_id": "mem_a", "replacement_content": "B"})
    assert out["success"] is True
    assert "mode" not in writer.last_kwargs, (
        "an omitted mode must rely on the canonical default, not be forwarded"
    )
    assert writer.last_kwargs["replacement_content"] == "B"
    assert out["mode"] == "replace"


def test_correct_receipt_keeps_every_canonical_field_without_fakes():
    """No manufactured ``active`` / empty-string defaults; canonical
    error_code / durable / status survive both outcomes."""
    _writer(
        {
            "success": True,
            "status": "COMMITTED_WITH_WARNING",
            "durable": True,
            "deduplicated": False,
            "old_memory_id": "mem_a",
            "new_memory_id": None,
            "old_status": "archived",
            "new_status": None,
            "current_memory_id": "mem_c",
            "relation": {"relation_type": "supersedes", "to_memory_id": "mem_b"},
            "correction_source": {"provenance": "explicit_tool_request",
                                  "correction_source_id": None},
            "effective_at": "2026-10-03T12:00:00+08:00",
            "warnings": ["embedding failed after commit"],
        }
    )
    out = _correct({"memory_id": "mem_a", "replacement_content": "B"})
    assert out["success"] is True
    assert out["status"] == "COMMITTED_WITH_WARNING"
    assert out["durable"] is True
    assert out["new_status"] is None, "a nonexistent successor has no new_status"
    assert out["current_memory_id"] == "mem_c"
    assert out["warnings"] == ["embedding failed after commit"]
    assert out["effective_at"] == "2026-10-03T12:00:00+08:00"


def test_correct_failure_keeps_canonical_error_code_and_durable():
    _writer(
        {
            "success": False,
            "status": "NOT_FOUND",
            "error_code": "NOT_FOUND",
            "error": "mem_missing 不在 public.explicit_memories 中",
            "durable": False,
            "deduplicated": False,
            "old_memory_id": "mem_missing",
            "new_memory_id": None,
            "old_status": None,
            "new_status": None,
            "current_memory_id": None,
        }
    )
    out = _correct({"memory_id": "mem_missing", "replacement_content": "B"})
    assert out["success"] is False
    # ``_failure`` used to drop error_code / durable.
    assert out["error_code"] == "NOT_FOUND"
    assert out["code"] == "NOT_FOUND"
    assert out["durable"] is False
    assert out["new_status"] is None
    assert out.get("current_memory_id") is None


def test_correct_already_committed_readback_failure_is_durable_true():
    """A committed write whose post-commit verification failed is NOT a
    rollback: durability is true and success is false."""
    _writer(
        {
            "success": False,
            "status": "VERIFICATION_FAILED",
            "error_code": "DURABLE_FAILED",
            "error": "committed, but readback verification failed",
            "durable": True,
            "deduplicated": False,
            "old_memory_id": "mem_a",
            "new_memory_id": "mem_b",
            "old_status": "archived",
            "new_status": "active",
            "current_memory_id": "mem_b",
        }
    )
    out = _correct({"memory_id": "mem_a", "replacement_content": "B"})
    assert out["success"] is False
    assert out["durable"] is True
    assert out["error_code"] == "DURABLE_FAILED"
    assert out["new_memory_id"] == "mem_b"


def test_correct_source_must_be_ascii_decimal_digits():
    writer = _writer({"success": True})
    for bad in ("+1", " 1", "1 ", "1.0", "１", "1a", ""):
        out = _correct({"memory_id": "mem_a", "replacement_content": "B",
                        "correction_source_id": bad})
        assert out["success"] is False, bad
        assert "correction_source_id" in out["error"], (bad, out)
    assert writer.calls == []
    ok = _correct({"memory_id": "mem_a", "replacement_content": "B",
                   "correction_source_id": "42"})
    assert ok["success"] is True
    assert writer.last_kwargs["correction_source_id"] == "42"


# ── nested canonical rendering ─────────────────────────────────────────────


def test_render_canonical_for_source_flattens_the_nested_current_payload():
    rendered = mc.render_canonical_for_source(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "current",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": "mem_b",
            "is_current": False,
            "requested": _payload("mem_a", "A", status="archived"),
            "memory": _payload("mem_b", "B", status="active"),
            "relation": {"relation_type": "supersedes", "to_memory_id": "mem_b"},
        },
        "mem_a",
    )
    # Flat ``content`` comes from the CURRENT nested memory, not from ''.
    assert rendered["content"] == "B"
    assert rendered["source"] == "explicit_memories"
    assert rendered["metadata"]["title"] == "mem_b"
    assert rendered["metadata"]["status"] == "active"
    assert rendered["requested_memory_id"] == "mem_a"
    assert rendered["resolved_memory_id"] == "mem_b"
    assert rendered["relation"]["relation_type"] == "supersedes"


def test_render_canonical_for_source_history_uses_the_requested_payload():
    rendered = mc.render_canonical_for_source(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "history",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": "mem_c",
            "is_current": False,
            "requested": _payload("mem_a", "A", status="archived"),
            "memory": _payload("mem_c", "C", status="active"),
            "history": {
                "versions": [{"memory_id": "mem_a", "label": "superseded"},
                             {"memory_id": "mem_c", "label": "current"}],
                "edges": [],
                "current_memory_id": "mem_c",
            },
        },
        "mem_a",
    )
    assert rendered["mode"] == "history"
    assert rendered["content"] == "A"
    assert rendered["history"]["current_memory_id"] == "mem_c"
    assert len(rendered["history"]["versions"]) == 2


def test_render_canonical_for_source_withdrawn_never_leaks_stale_payload():
    """A withdrawn current read has NO current memory: content is absent /
    None, never the archived or requested payload."""
    rendered = mc.render_canonical_for_source(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "current",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": None,
            "is_current": False,
            "memory": None,
            "requested": _payload("mem_a", "A", status="withdrawn"),
            "relation": {"relation_type": "withdraws", "to_memory_id": None},
        },
        "mem_a",
    )
    assert rendered.get("content") in (None, "")
    assert rendered["memory"] is None
    assert rendered["resolved_memory_id"] is None
    assert rendered["is_current"] is False
    # The historical payload stays available and labelled.
    assert rendered["requested"]["content"] == "A"
    assert rendered["requested"]["status"] == "withdrawn"
    assert rendered["relation"]["relation_type"] == "withdraws"


def test_hm_get_withdrawn_archived_hit_renders_no_stale_content():
    reader = _reader(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "current",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": None,
            "is_current": False,
            "memory": None,
            "requested": _payload("mem_a", "A", status="withdrawn"),
            "relation": {"relation_type": "withdraws", "to_memory_id": None},
        }
    )
    out = json.loads(
        handle_hm_get({"source_id": "mem_a"}, core=_Core(_row(status="withdrawn")))
    )
    assert out["success"] is True
    assert reader.last_kwargs["mode"] == "current"
    assert out.get("content") in (None, "")
    assert out["resolved_memory_id"] is None
    assert out["is_current"] is False


def test_hm_get_archived_hit_without_current_never_substitutes_archived_payload():
    reader = _reader(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "current",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": None,
            "is_current": False,
            "memory": None,
            "requested": _payload("mem_a", "A", status="archived"),
            "relation": None,
        }
    )
    out = json.loads(
        handle_hm_get({"source_id": "mem_a"}, core=_Core(_row(status="archived")))
    )
    assert out["success"] is True
    assert out.get("content") in (None, "")
    assert out["resolved_memory_id"] is None
    assert out["is_current"] is False


# ── strict hm read parameters ──────────────────────────────────────────────


def test_hm_get_rejects_a_non_boolean_include_history():
    reader = _reader({"success": True})
    for bad in ("false", "true", 1, 0, "", [], "yes"):
        out = json.loads(
            handle_hm_get(
                {"source_id": "mem_a", "include_history": bad},
                core=_Core(_row()),
            )
        )
        assert out["success"] is False, bad
        assert "include_history" in out["error"], (bad, out)
    assert reader.calls == []


def test_hm_get_rejects_an_unknown_mode():
    reader = _reader({"success": True})
    for bad in ("latest-wins", "current,history", 1, True, ""):
        out = json.loads(
            handle_hm_get(
                {"source_id": "mem_a", "mode": bad}, core=_Core(_row())
            )
        )
        assert out["success"] is False, bad
        assert "mode" in out["error"], (bad, out)
    assert reader.calls == []


def test_hm_get_include_history_true_keeps_current_mode_and_current_content():
    """``include_history=True`` is NOT an implicit history mode: the payload
    is the CURRENT version, with history attached."""
    reader = _reader(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "current",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": "mem_b",
            "is_current": False,
            "requested": _payload("mem_a", "A", status="archived"),
            "memory": _payload("mem_b", "B", status="active"),
            "history": {
                "versions": [{"memory_id": "mem_a", "label": "superseded"},
                             {"memory_id": "mem_b", "label": "current"}],
                "edges": [],
                "current_memory_id": "mem_b",
            },
        }
    )
    out = json.loads(
        handle_hm_get(
            {"source_id": "mem_a", "include_history": True},
            core=_Core(_row(status="archived")),
        )
    )
    assert out["success"] is True
    assert reader.last_kwargs["mode"] == "current"
    assert reader.last_kwargs["include_history"] is True
    assert out["content"] == "B", "current content, not the archived payload"
    assert out["history"]["current_memory_id"] == "mem_b"
    assert out["mode"] == "current"


def test_hm_get_include_history_false_stays_a_plain_read():
    reader = _reader({"success": True})
    out = json.loads(
        handle_hm_get(
            {"source_id": "mem_a", "include_history": False},
            core=_Core(_row()),
        )
    )
    assert out["success"] is True
    # False is not a request for the canonical reader.
    assert reader.calls == []


def test_hm_get_explicit_history_mode_renders_the_requested_payload():
    reader = _reader(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "history",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": "mem_b",
            "is_current": False,
            "requested": _payload("mem_a", "A", status="archived"),
            "memory": _payload("mem_b", "B", status="active"),
            "history": {
                "versions": [{"memory_id": "mem_a", "label": "superseded"},
                             {"memory_id": "mem_b", "label": "current"}],
                "edges": [],
                "current_memory_id": "mem_b",
            },
        }
    )
    out = json.loads(
        handle_hm_get(
            {"source_id": "mem_a", "mode": "history", "include_history": True},
            core=_Core(_row()),
        )
    )
    assert out["success"] is True
    assert reader.last_kwargs["mode"] == "history"
    assert out["content"] == "A"
    assert out["mode"] == "history"
    assert [v["memory_id"] for v in out["history"]["versions"]] == ["mem_a", "mem_b"]


def test_hm_get_canonical_failure_keeps_the_error_code():
    _reader({"success": False, "error_code": "MIGRATION_REQUIRED",
             "error": 'relation "memory_relations" does not exist'})
    out = json.loads(
        handle_hm_get({"source_id": "mem_a", "mode": "history"},
                      core=_Core(_row()))
    )
    assert out["success"] is False
    assert out["error_code"] == "MIGRATION_REQUIRED"
    assert out["code"] == "MIGRATION_REQUIRED"
    assert "memory_relations" in out["error"]


# ── runtime scope fail-closed ──────────────────────────────────────────────


def _CoreWithPool(row, pool):
    """A core that carries a booted PgPool owner (config is set per test)."""
    core = _Core(row)
    core.pg_pool = pool
    core._pg_pool = pool
    return core


class _CoreWithPoolOnly:
    def __init__(self, pool):
        self.pg = _Pg(None)
        self.pg_pool = pool
        self._pg_pool = pool


def test_pool_plus_core_still_inherits_the_core_config():
    """A supplied pool must not mask ``core.config`` — the old code only
    read core when the pool was absent and fell through to resolve_config()."""
    reader = _reader({"success": True, "mode": "current", "memory": None,
                      "requested": _payload("mem_a", "A")})
    pool = object()
    core = _CoreWithPool(_row(), pool)
    core.config = {"basePath": "C:/from-core"}
    out = json.loads(
        handle_v3_get(
            {"target": "memory", "memory_id": "mem_a"},
            core=core,
            pool=pool,
            runtime_context=True,
        )
    )
    assert out["success"] is True
    assert reader.ctor_kwargs.get("pool") is pool
    # The core's own booted config wins over any implicit default profile.
    assert reader.ctor_kwargs.get("config") == {"basePath": "C:/from-core"}


def test_injected_core_without_pool_or_config_fails_closed():
    """An injected runtime must not silently resolve the default profile."""
    _forbid("_reader_cls")
    out = json.loads(
        handle_v3_get(
            {"target": "memory", "memory_id": "mem_a"},
            core=_CoreWithPoolOnly(object()),
            effective_config=None,
            runtime_context=True,
        )
    )
    assert out["success"] is False
    assert "effective config" in out["error"].lower()


def test_correct_injected_core_without_config_fails_closed():
    _forbid("_writer_cls")
    out = json.loads(
        handle_v3_update(
            {"action": "correct", "memory_id": "mem_a", "replacement_content": "B"},
            core=_CoreWithPoolOnly(object()),
            effective_config=None,
            pool=object(),
            runtime_context=True,
        )
    )
    assert out["success"] is False
    assert "effective config" in out["error"].lower()


def test_resolve_scope_inherits_core_scope_independently_of_pool():
    pool = object()
    core = _CoreWithPool(_row(), pool)
    core.config = {"basePath": "C:/from-core"}
    core.pg = object()
    got_pool, got_pg, got_cfg = mc.resolve_scope(
        {"core": core, "pool": pool, "runtime_context": True}
    )
    assert got_pool is pool
    assert got_cfg == {"basePath": "C:/from-core"}
    assert got_pg is core.pg


def test_resolve_scope_legacy_non_runtime_path_still_resolves():
    import v3core.config as config_mod

    original = config_mod.resolve_config
    try:
        config_mod.resolve_config = lambda *a, **kw: {"basePath": "legacy"}
        pool, pg, cfg = mc.resolve_scope({})
        assert cfg == {"basePath": "legacy"}
        assert pool is None
    finally:
        config_mod.resolve_config = original


# ── final M02 corrections: strict null / scope / durability / codes ────────
#
# Gaps closed in this pass (each new test is a focused pin, not a rewrite):
#   A) canonical unknown durability (``None``) survives ``_failure`` verbatim;
#   B) the canonical explicit read inherits the *constructed* core's scope;
#   C) an EXPLICIT null ``mode`` / ``include_history`` fails before lookup;
#   D) a declared ``runtime_context`` is detected by PRESENCE, not truthiness;
#   E) tool-layer failures carry a machine-readable ``error_code``;
#   F) an omitted correct ``mode`` is not forwarded on the wire.
# Plus regression pins for the three one-line parent fixes (literal alias,
# effective_config=None refusal, rendered current_memory_id/relations).

_MISSING = object()


class _ExactSignatureWriter:
    """``correct`` stub with the canonical signature and NO ``**kwargs``.

    A sentinel default proves whether a keyword was actually forwarded, and
    any stray keyword (e.g. a replacement on a withdraw) raises ``TypeError``
    instead of being silently swallowed.
    """

    def __init__(self, **ctor_kwargs):
        self.ctor_kwargs = ctor_kwargs
        self.calls: list[dict] = []

    def correct(
        self,
        memory_id,
        *,
        mode=_MISSING,
        replacement_content=_MISSING,
        replacement_title=_MISSING,
        replacement_tags=_MISSING,
        correction_reason=_MISSING,
        correction_source_id=_MISSING,
        effective_at=_MISSING,
        authority=_MISSING,
    ):
        self.calls.append(
            {
                "memory_id": memory_id,
                "mode": mode,
                "replacement_content": replacement_content,
                "replacement_title": replacement_title,
                "replacement_tags": replacement_tags,
                "correction_reason": correction_reason,
                "correction_source_id": correction_source_id,
                "effective_at": effective_at,
                "authority": authority,
            }
        )
        return {"success": True, "old_memory_id": memory_id}


def _exact_writer() -> dict:
    """Install ``_ExactSignatureWriter`` as the writer seam."""
    holder: dict = {}

    def _factory(**ctor_kwargs):
        inst = _ExactSignatureWriter(**ctor_kwargs)
        holder["inst"] = inst
        return inst

    mc._writer_cls = _factory
    holder["get"] = lambda: holder["inst"]
    return holder


class _BootedCore:
    """A core as ``handle_hm_get`` constructs it when no ``core`` was
    injected: it still owns the booted PG pool and the booted config."""

    def __init__(self, row, pool, cfg):
        self._pg = _Pg(row)
        self.pg = self._pg
        self.pg_pool = pool
        self._pg_pool = pool
        self.config = cfg

    def get_message_context(self, source_id):
        return json.dumps({"success": False, "source_id": source_id})


# ── A) canonical durability is preserved verbatim ──────────────────────────


def test_correct_failure_preserves_canonical_durability_verbatim():
    """False / True / None must survive ``_failure`` exactly — an unknown
    durability is NOT a rollback."""
    for durable in (False, True, None):
        _writer(
            {
                "success": False,
                "status": "READBACK",
                "error_code": "DURABLE_FAILED",
                "error": "durability verdict",
                "durable": durable,
                "current_memory_id": "mem_b",
                "relation": {"relation_type": "supersedes", "to_memory_id": "mem_b"},
            }
        )
        out = _correct({"memory_id": "mem_a", "replacement_content": "B"})
        assert out["success"] is False, durable
        assert out["durable"] is durable, (durable, out)
        assert out["status"] == "READBACK"
        assert out["error_code"] == "DURABLE_FAILED"
        assert out["code"] == "DURABLE_FAILED"
        assert out["current_memory_id"] == "mem_b"
        assert out["relation"]["relation_type"] == "supersedes"


# ── B) the constructed core's scope reaches the canonical reader ───────────


def test_hm_get_archived_hit_inherits_the_internally_constructed_core_scope(
    monkeypatch,
):
    """The core built inside ``handle_hm_get`` must be the scope the
    canonical read resolves against — never a default-profile fallback."""
    pool = object()
    cfg = {"basePath": "C:/booted"}
    booted = _BootedCore(_row(status="archived"), pool, cfg)
    monkeypatch.setattr("v3core.V3Core", lambda **kw: booted)
    reader = _reader(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "current",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": "mem_b",
            "is_current": False,
            "requested": _payload("mem_a", "A", status="archived"),
            "memory": _payload("mem_b", "B", status="active"),
        }
    )
    out = json.loads(handle_hm_get({"source_id": "mem_a"}))
    assert out["success"] is True, out
    assert reader.last_kwargs["mode"] == "current"
    assert reader.ctor_kwargs.get("pool") is pool
    assert reader.ctor_kwargs.get("config") is cfg
    assert reader.ctor_kwargs.get("pg") is booted._pg


def test_canonical_explicit_read_preserves_an_explicitly_supplied_scope():
    """An explicitly supplied core / pool / config is never overwritten by
    the core argument ``handle_hm_get`` happened to pass alongside it."""
    reader = _reader(
        {"success": True, "mode": "history", "requested": _payload("mem_a", "A")}
    )
    explicit_pool = object()
    explicit_core = _CoreWithPool(_row(), explicit_pool)
    explicit_core.config = {"basePath": "C:/explicit"}
    out = json.loads(
        get_tool_mod._canonical_explicit_read(
            "mem_a",
            {"source_id": "mem_a", "mode": "history"},
            object(),
            {"core": explicit_core, "pool": explicit_pool},
        )
    )
    assert out["success"] is True, out
    assert reader.ctor_kwargs.get("pool") is explicit_pool
    assert reader.ctor_kwargs.get("config") == {"basePath": "C:/explicit"}


# ── C) an EXPLICIT null is not an omitted parameter ────────────────────────


def test_memory_target_rejects_explicit_null_mode_and_include_history():
    reader = _reader({"success": True})
    for args in (
        {"target": "memory", "memory_id": "mem_a", "mode": None},
        {"target": "memory", "memory_id": "mem_a", "include_history": None},
        {"target": "memory", "memory_id": "mem_a", "mode": None,
         "include_history": None},
    ):
        out = json.loads(handle_v3_get(args, **_boot()))
        assert out["success"] is False, args
        assert out.get("error_code") == "INVALID_REQUEST", (args, out)
    assert reader.calls == []


def test_memory_target_rejects_non_string_mode_before_the_reader():
    reader = _reader({"success": True})
    for bad in (1, True, "", ["current"]):
        out = json.loads(
            handle_v3_get(
                {"target": "memory", "memory_id": "mem_a", "mode": bad}, **_boot()
            )
        )
        assert out["success"] is False, bad
        assert out.get("error_code") == "INVALID_REQUEST", (bad, out)
    assert reader.calls == []


def test_hm_get_rejects_explicit_null_history_parameters():
    reader = _reader({"success": True})
    for args in (
        {"source_id": "mem_a", "mode": None},
        {"source_id": "mem_a", "include_history": None},
        {"source_id": "mem_a", "mode": None, "include_history": None},
    ):
        out = json.loads(handle_hm_get(args, core=_Core(_row())))
        assert out["success"] is False, args
        assert out.get("error_code") == "INVALID_REQUEST", (args, out)
    assert reader.calls == []


def test_correct_rejects_explicit_null_mode_before_the_writer():
    writer = _writer()
    out = _correct({"memory_id": "mem_a", "replacement_content": "B", "mode": None})
    assert out["success"] is False
    assert out.get("error_code") == "INVALID_REQUEST"
    assert writer.calls == []


# ── D) a declared runtime_context is presence, not truthiness ──────────────


def test_resolve_scope_detects_declared_runtime_context_by_presence():
    import v3core.config as config_mod

    original = config_mod.resolve_config
    try:
        config_mod.resolve_config = lambda *a, **kw: {"basePath": "legacy"}
        for declared in (False, None, "", 0):
            with pytest.raises(RuntimeError):
                mc.resolve_scope({"runtime_context": declared})
        # Pure legacy: nothing declared at all still resolves.
        pool, pg, cfg = mc.resolve_scope({})
        assert cfg == {"basePath": "legacy"}
        assert pool is None
    finally:
        config_mod.resolve_config = original


def test_correct_declared_incomplete_runtime_context_fails_closed():
    _forbid("_writer_cls")
    out = json.loads(
        handle_v3_update(
            {"action": "correct", "memory_id": "mem_a", "replacement_content": "B"},
            runtime_context=False,
        )
    )
    assert out["success"] is False
    assert out.get("error_code") == "SCOPE_UNAVAILABLE", out


# ── E) machine-readable failure codes ──────────────────────────────────────


def test_correct_validation_failure_reports_invalid_request_code():
    _forbid("_writer_cls")
    out = json.loads(handle_v3_update({"action": "correct"}, **_boot()))
    assert out["success"] is False
    assert out["error_code"] == "INVALID_REQUEST"
    assert out["code"] == "INVALID_REQUEST"
    assert "memory_id" in out["error"]


def test_correct_injected_missing_owner_reports_scope_unavailable_code():
    _forbid("_writer_cls")
    out = json.loads(
        handle_v3_update(
            {"action": "correct", "memory_id": "mem_a", "replacement_content": "B"},
            effective_config={"basePath": "x"},
            pool=None,
            runtime_context=True,
        )
    )
    assert out["success"] is False
    assert out["error_code"] == "SCOPE_UNAVAILABLE"
    assert "pool" in out["error"].lower()


def test_correct_canonical_exception_is_machine_readable_without_fake_durability():
    _writer(result=RuntimeError("writer exploded"))
    out = _correct({"memory_id": "mem_a", "replacement_content": "B"})
    assert out["success"] is False
    assert out["error_code"] == "CANONICAL_FAILURE"
    assert "durable" not in out, (
        "a tool-layer failure must not claim a durability verdict"
    )


def test_memory_target_canonical_exception_is_machine_readable():
    _reader(result=RuntimeError('relation "memory_relations" does not exist'))
    out = json.loads(
        handle_v3_get({"target": "memory", "memory_id": "mem_a"}, **_boot())
    )
    assert out["success"] is False
    assert out["error_code"] == "CANONICAL_FAILURE"
    assert "durable" not in out


def test_hm_get_invalid_history_parameters_report_invalid_request():
    reader = _reader({"success": True})
    out = json.loads(
        handle_hm_get({"source_id": "mem_a", "mode": "latest-wins"}, core=_Core(_row()))
    )
    assert out["success"] is False
    assert out["error_code"] == "INVALID_REQUEST"
    assert reader.calls == []


def test_receipt_error_codes_add_no_public_input_fields():
    """The machine-readable codes are OUTPUT-only: no schema gains an input
    property and additionalProperties=False keeps holding."""
    assert V3_UPDATE_SCHEMA["parameters"]["additionalProperties"] is False
    for schema in (V3_UPDATE_SCHEMA, V3_GET_SCHEMA, HM_GET_SCHEMA):
        props = schema["parameters"]["properties"]
        for receipt_only in ("error_code", "code", "durable"):
            assert receipt_only not in props, receipt_only
    assert V3_UPDATE_SCHEMA["parameters"]["required"] == ["action"]
    assert V3_GET_SCHEMA["parameters"]["required"] == ["target"]
    assert HM_GET_SCHEMA["parameters"]["required"] == ["source_id"]


def test_canonical_existing_error_codes_are_unchanged():
    for code in ("NOT_FOUND", "CORRECTION_CONFLICT", "DURABLE_FAILED"):
        _writer({"success": False, "error_code": code, "error": "x", "durable": False})
        out = _correct({"memory_id": "mem_a", "replacement_content": "B"})
        assert out["error_code"] == code
        assert out["code"] == code
    for code in ("NOT_FOUND", "MIGRATION_REQUIRED"):
        _reader({"success": False, "error_code": code, "error": "x"})
        out = json.loads(
            handle_v3_get({"target": "memory", "memory_id": "mem_a"}, **_boot())
        )
        assert out["error_code"] == code
        assert out["code"] == code


# ── F) an omitted correct mode is not forwarded ────────────────────────────


def test_correct_omitted_mode_is_not_forwarded_on_the_wire():
    holder = _exact_writer()
    out = _correct({"memory_id": "mem_a", "replacement_content": "B"})
    assert out["success"] is True, out
    call = holder["get"]().calls[-1]
    assert call["mode"] is _MISSING, "an omitted mode must not be forwarded"
    assert call["replacement_content"] == "B"
    assert out["mode"] == "replace"


def test_correct_withdraw_forwards_explicit_mode_and_no_replacement():
    holder = _exact_writer()
    out = _correct({"memory_id": "mem_a", "mode": "withdraw"})
    assert out["success"] is True, out
    call = holder["get"]().calls[-1]
    assert call["mode"] == "withdraw"
    assert call["replacement_content"] is _MISSING


# ── regression pins for the parent's three one-line fixes ──────────────────


def test_correct_source_id_alias_uses_an_exact_literal_comparison():
    writer = _writer({"success": True})
    ok = _correct({"memory_id": "mem_a", "replacement_content": "B",
                   "source_id": "mem_a"})
    assert ok["success"] is True
    assert writer.last_kwargs["correction_source_id"] is None
    for alias in ("mem_a ", " mem_a", "MEM_A", "mem_a\n"):
        out = _correct(
            {"memory_id": "mem_a", "replacement_content": "B", "source_id": alias}
        )
        assert out["success"] is False, alias
        assert "source_id" in out["error"], (alias, out)
    assert len(writer.calls) == 1


def test_explicit_null_effective_config_refuses_the_profile_fallback():
    _forbid("_reader_cls")
    out = json.loads(
        handle_v3_get(
            {"target": "memory", "memory_id": "mem_a"},
            effective_config=None,
            pool=object(),
            runtime_context=True,
        )
    )
    assert out["success"] is False
    assert out["error_code"] == "SCOPE_UNAVAILABLE"
    assert "effective config" in out["error"].lower()


def test_render_canonical_for_source_preserves_current_memory_id_and_relations():
    rendered = mc.render_canonical_for_source(
        {
            "success": True,
            "status": "OK",
            "error_code": None,
            "mode": "current",
            "requested_memory_id": "mem_a",
            "resolved_memory_id": "mem_b",
            "is_current": False,
            "current_memory_id": "mem_b",
            "relations": [
                {"relation_type": "supersedes", "from_memory_id": "mem_a",
                 "to_memory_id": "mem_b"},
            ],
            "memory": _payload("mem_b", "B", status="active"),
            "requested": _payload("mem_a", "A", status="archived"),
        },
        "mem_a",
    )
    assert rendered["current_memory_id"] == "mem_b"
    assert rendered["relations"][0]["relation_type"] == "supersedes"
    assert rendered["content"] == "B"


# ── G) hm forward preserves every supplied literal (key presence) ───────────


def test_hm_forward_preserves_every_supplied_literal_including_none():
    from v3core.tools.api_get import _hm_forward

    assert _hm_forward({"source_id": "mem_a"}, "mem_a") == {"source_id": "mem_a"}
    assert _hm_forward({"source_id": "mem_a", "mode": None}, "mem_a") == {
        "source_id": "mem_a",
        "mode": None,
    }
    assert _hm_forward({"source_id": "mem_a", "include_history": None}, "mem_a") == {
        "source_id": "mem_a",
        "include_history": None,
    }
    assert _hm_forward({"source_id": "mem_a", "include_history": False}, "mem_a") == {
        "source_id": "mem_a",
        "include_history": False,
    }
    assert _hm_forward(
        {"source_id": "mem_a", "mode": "history", "include_history": True}, "mem_a"
    ) == {"source_id": "mem_a", "mode": "history", "include_history": True}


def test_v3_get_hm_explicit_null_history_parameters_fail_before_construction(monkeypatch):
    """An explicit null must reach hm_get as INVALID_REQUEST, not be dropped."""
    _forbid("_reader_cls")
    monkeypatch.setattr("v3core.V3Core", _Exploding("V3Core"))
    for args in (
        {"target": "hm", "source_id": "mem_a", "mode": None},
        {"target": "hm", "source_id": "mem_a", "include_history": None},
        {"target": "hm", "source_id": "mem_a", "mode": None, "include_history": None},
    ):
        out = json.loads(handle_v3_get(args, **_boot()))
        assert out["success"] is False, args
        assert out.get("error_code") == "INVALID_REQUEST", (args, out)


def test_v3_get_hm_omitted_history_parameters_keep_the_plain_active_shape():
    reader = _reader({"success": True, "content": "SHOULD NOT BE USED"})
    out = json.loads(
        handle_v3_get({"target": "hm", "source_id": "mem_a"}, core=_Core(_row()))
    )
    assert out["success"] is True
    assert out["source"] == "explicit_memories"
    assert out["content"] == "部署方案采用 A。"
    assert reader.calls == []


def test_v3_get_hm_supplied_false_include_history_stays_a_plain_read():
    reader = _reader({"success": True, "content": "NOPE"})
    out = json.loads(
        handle_v3_get(
            {"target": "hm", "source_id": "mem_a", "include_history": False},
            core=_Core(_row()),
        )
    )
    assert out["success"] is True
    assert out["source"] == "explicit_memories"
    assert out["content"] == "部署方案采用 A。"
    assert reader.calls == []


# ── H) hm declared-incomplete runtime fails closed before construction ──────


def test_hm_get_declared_incomplete_runtime_fails_closed_before_construction(monkeypatch):
    _forbid("_reader_cls")
    monkeypatch.setattr("v3core.V3Core", _Exploding("V3Core"))
    for kw in (
        {"runtime_context": False},
        {"runtime_context": None},
        {"runtime_context": ""},
        {"effective_config": None},
        {"runtime_context": True, "effective_config": {"basePath": "x"}},
        {"runtime_context": True, "pool": object()},
    ):
        out = json.loads(handle_hm_get({"source_id": "mem_a"}, **kw))
        assert out["success"] is False, kw
        assert out.get("error_code") == "SCOPE_UNAVAILABLE", (kw, out)
    # A declared-but-incomplete core is refused too.
    out = json.loads(
        handle_hm_get(
            {"source_id": "mem_a"},
            core=_CoreWithPoolOnly(object()),
            effective_config=None,
        )
    )
    assert out["success"] is False
    assert out.get("error_code") == "SCOPE_UNAVAILABLE", out


def test_v3_get_hm_declared_incomplete_runtime_fails_closed(monkeypatch):
    _forbid("_reader_cls")
    monkeypatch.setattr("v3core.V3Core", _Exploding("V3Core"))
    for kw in (
        {"runtime_context": False},
        {"runtime_context": None},
        {"runtime_context": ""},
        {"effective_config": None},
    ):
        out = json.loads(handle_v3_get({"target": "hm", "source_id": "mem_a"}, **kw))
        assert out["success"] is False, kw
        assert out.get("error_code") == "SCOPE_UNAVAILABLE", (kw, out)


def test_hm_get_complete_supplied_core_is_not_rejected_and_keeps_ownership():
    pool = object()
    core = _CoreWithPool(_row(), pool)
    core.config = {"basePath": "C:/booted"}
    reader = _reader({"success": True, "content": "NOPE"})
    out = json.loads(
        handle_hm_get(
            {"source_id": "mem_a"},
            core=core,
            pool=pool,
            effective_config={"basePath": "C:/booted"},
            runtime_context=True,
        )
    )
    assert out["success"] is True
    assert out["source"] == "explicit_memories"
    assert out["content"] == "部署方案采用 A。"
    assert reader.calls == []


def test_hm_get_legacy_empty_kw_still_constructs_the_core(monkeypatch):
    booted = _BootedCore(None, object(), {"basePath": "C:/booted"})
    seen: list[dict] = []

    def _fake_v3core(**kw):
        seen.append(kw)
        return booted

    monkeypatch.setattr("v3core.V3Core", _fake_v3core)
    _forbid("_reader_cls")
    out = json.loads(handle_hm_get({"source_id": "msg_1234"}))
    assert out["success"] is False
    assert seen, "the legacy no-runtime path must still construct the core"


# ── I) pure tests cannot poison later suites (seam teardown) ────────────────


def test_seam_teardown_restores_after_a_poisoning_test():
    mc._writer_cls = _Exploding("_writer_cls")
    mc._reader_cls = _Exploding("_reader_cls")


def test_canonical_seams_are_clean_at_test_start():
    for seam in (mc._writer_cls, mc._reader_cls):
        assert getattr(seam, "__name__", None) in ("_writer_cls", "_reader_cls")
# ── J) v3_delete writer construction: real pool vs PgEmbedStore lease ───────
#
# Regression for the run5 product defect: ``_build_writer_and_pool`` used to
# do ``pool = getattr(core, "_pg_pool", None) or pg``. When the core had no
# PgPool, that assigned the PgEmbedStore ITSELF as the pool, so the writer's
# ``_acquire_lease`` called ``pg.lease(...)`` directly. A
# ``@contextlib.contextmanager`` method returns a ``_GeneratorContextManager``,
# and ``ActiveMemoryWriter.archive()`` then raised
# ``AttributeError: '_GeneratorContextManager' object has no attribute 'close'``.
# The store must instead be handed over as ``pg`` (pool=None) so the existing
# ``_PgStoreLeaseAdapter`` wraps it into a real lease that HAS ``close()``.


class _DeleteCursor:
    def __init__(self, row):
        self.row = row
        self.executed: list = []

    def execute(self, sql, params=()):
        self.executed.append(" ".join(str(sql).split()))
        return None

    def fetchone(self):
        return self.row


class _DeleteConn:
    def __init__(self, row):
        self._row = row
        self.commits = 0

    def cursor(self):
        return _DeleteCursor(self._row)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class _DeleteLeaseStore:
    """PgEmbedStore-shaped store: ``lease()`` is a @contextmanager yielding a conn."""

    def __init__(self, conn):
        self._conn = conn
        self.leases = 0

    @contextmanager
    def lease(self, timeout=None):
        self.leases += 1
        yield self._conn


class _DeleteCore:
    """A core carrying a PgEmbedStore but NO PgPool (``_pg_pool`` is None)."""

    def __init__(self, store):
        self._pg = store
        self._pg_pool = None
        self.config = {"basePath": "C:/isolated"}


def _delete_core(monkeypatch, row=("mem_x", "active")):
    conn = _DeleteConn(row)
    store = _DeleteLeaseStore(conn)
    monkeypatch.setattr("v3core.V3Core", _DeleteCore)
    return _DeleteCore(store), store, conn


def test_delete_writer_hands_the_pg_store_as_pg_not_as_pool_when_no_pool_exists(
    monkeypatch,
):
    """The no-pool case must receive pool=None + pg=<PgEmbedStore>, so
    ``_acquire_lease`` goes through ``_PgStoreLeaseAdapter`` and the lease has
    the ``close()`` / ``connection`` surface ``archive()`` needs."""
    core, store, conn = _delete_core(monkeypatch)
    writer, reader, pg_ref = _build_writer_and_pool({"core": core})
    # A real pool only when one exists; otherwise the store is passed as pg.
    assert writer._pool is None
    assert writer._pg is store
    assert reader._pool is None
    assert pg_ref is store
    # ...so the lease is the adapter, never the raw _GeneratorContextManager
    # that the store's lease() returns.
    lease = _acquire_lease(writer._pool, writer._pg)
    assert isinstance(lease, _PgStoreLeaseAdapter)
    assert hasattr(lease, "close") and hasattr(lease, "connection")
    assert lease.connection is conn
    lease.close()
    lease.close()  # idempotent
    # End-to-end on the same construction: archive() must not die on close().
    result = writer.archive("mem_x")
    assert result.table_available is True
    assert result.found is True
    assert result.archived is True
    assert result.error is None


def test_delete_writer_negative_control_or_pg_substitution_yields_no_close(
    monkeypatch,
):
    """The pre-fix ``... or pg`` substitution IS the defect: the store becomes
    the "pool", so ``_acquire_lease`` returns a raw generator-context object
    with neither ``close`` nor ``connection`` and ``archive()`` fails."""
    core, store, conn = _delete_core(monkeypatch)
    substituted_pool = getattr(core, "_pg_pool", None) or store
    assert substituted_pool is store, "the defect assigns the store itself as pool"
    writer = ActiveMemoryWriter(pool=substituted_pool, pg=store, config=core.config)
    raw = _acquire_lease(writer._pool, writer._pg)
    assert not hasattr(raw, "close")
    assert not hasattr(raw, "connection")
    result = writer.archive("mem_x")
    assert result.found is False
    assert result.table_available is False
    assert "close" in (result.error or ""), result.error
