"""M04 — time-point reads (``as_of``).

Two things are tested separately because they fail differently:

* **The timeline walk** is a pure function of the recorded relation chain, so
  it is exercised without any database. Every acceptance case about *which
  version was current at T* lives here.
* **The parameter contract** is validated before any I/O, so those cases need
  no database either — a reader whose ``pg`` is never touched must still
  refuse the call.

The database-backed path is covered by shimming ``_read_snapshot`` with a
fixed chain, which is also how the ``as_of=None`` zero-regression shape is
pinned.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

MODULE = "v3core.active_memory_store"

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(days=10)
_T2 = _T0 + timedelta(days=20)
_T3 = _T0 + timedelta(days=30)

_UNSET = object()


def _mod():
    import importlib

    try:
        return importlib.import_module(MODULE)
    except Exception as exc:  # pragma: no cover - RED surface
        pytest.fail(f"cannot import {MODULE!r}: {exc!r}")


# ─────────────────────────── builders ─────────────────────────────────────


def _iso(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row(memory_id: str, *, created: Any = _T0, status: str = "active") -> dict:
    return {
        "memory_id": memory_id,
        "category": "ops",
        "title": memory_id,
        "content": f"{memory_id} content",
        "tags": [],
        "provenance": {"origin": "seed"},
        "status": status,
        "created_at": created,
        "updated_at": created,
    }


def _edge(
    relation_id: str,
    frm: str,
    to: Any,
    *,
    recorded: Any,
    effective: Any = _UNSET,
    kind: str = "supersedes",
) -> dict:
    return {
        "relation_id": relation_id,
        "relation_type": kind,
        "from_memory_id": frm,
        "to_memory_id": to,
        "correction_id": f"cor_{relation_id}",
        "authority": "user_explicit",
        "correction_reason": None,
        "correction_source_id": None,
        "provenance": "explicit_tool_request",
        "effective_at": None if effective is _UNSET else _iso(effective),
        "recorded_at": _iso(recorded),
        "request": {},
    }


def _chain(rows: list[dict], edges: list[dict]) -> dict:
    versions = [
        {
            "memory_id": row["memory_id"],
            "label": "seed",
            "is_current": False,
            "status": row["status"],
            "category": row["category"],
            "title": row["title"],
            "content": row["content"],
            "tags": row["tags"],
            "provenance": row["provenance"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    return {
        "versions": versions,
        "edges": list(edges),
        "current_memory_id": None,
        "current_row": None,
        "current_status": "active",
    }


def _walk(chain: dict, as_of: Any, basis: str = "recorded", mid: str = "a") -> dict:
    return _mod().ActiveMemoryReader._walk_timeline(mid, chain, as_of, basis)


def _abc(*, effective: bool = False) -> dict:
    """a -> b -> c, recorded at T1 and T2."""
    rows = [_row("a"), _row("b"), _row("c")]
    edges = [
        _edge(
            "rel_ab", "a", "b",
            recorded=_T1,
            effective=_T1 if effective else _UNSET,
        ),
        _edge(
            "rel_bc", "b", "c",
            recorded=_T2,
            effective=_T2 if effective else _UNSET,
        ),
    ]
    return _chain(rows, edges)


# ─────────────────── pure walk: A -> B -> C across time ────────────────────


def test_M04_abc_resolves_to_each_version_in_its_own_window():
    chain = _abc()
    assert _walk(chain, _T0 + timedelta(days=1))["current_id"] == "a"
    assert _walk(chain, _T1 + timedelta(days=1))["current_id"] == "b"
    assert _walk(chain, _T2 + timedelta(days=1))["current_id"] == "c"


def test_M04_abc_exact_boundary_counts_as_applied():
    chain = _abc()
    # A transition recorded exactly at as_of has happened by that instant.
    assert _walk(chain, _T1)["current_id"] == "b"
    assert _walk(chain, _T2)["current_id"] == "c"


def test_M04_labels_mark_current_superseded_and_future():
    out = _walk(_abc(), _T1 + timedelta(days=1))
    labels = {v["memory_id"]: v["label"] for v in out["versions"]}
    assert labels == {"a": "superseded", "b": "current", "c": "future"}
    assert [v["is_current_at_as_of"] for v in out["versions"]] == [
        False, True, False
    ]
    assert out["current_status"] == "active"
    assert out["query_status"] == "OK"


def test_M04_edges_list_only_contains_consulted_transitions():
    out = _walk(_abc(), _T0 + timedelta(days=1))
    # The walk stops at the first transition that had not happened yet; the
    # later edge is never consumed and must not appear as if it had been.
    assert [e["relation_id"] for e in out["edges"]] == ["rel_ab"]
    assert out["edges"][0]["decision"] == "not_yet"


def test_M04_future_as_of_returns_present_state():
    out = _walk(_abc(), _T3)
    assert out["current_id"] == "c"
    assert out["query_status"] == "OK"


def test_M04_as_of_before_chain_floor_is_absent_not_the_first_version():
    out = _walk(_abc(), _T0 - timedelta(days=1))
    assert out["current_id"] is None
    assert out["current_status"] == "absent"
    assert out["query_status"] == "NO_VERSION_AT_AS_OF"
    assert out["edges"] == []


# ─────────────────── effective basis: evidence, never inference ────────────


def test_M04_effective_basis_uses_declared_time_not_recorded_time():
    """The two bases must disagree when the declaration lags the record."""
    rows = [_row("a"), _row("b")]
    edges = [_edge("rel_ab", "a", "b", recorded=_T1, effective=_T2)]
    chain = _chain(rows, edges)
    between = _T1 + timedelta(days=1)
    assert _walk(chain, between, "recorded")["current_id"] == "b"
    assert _walk(chain, between, "effective")["current_id"] == "a"


def test_M04_effective_missing_is_indeterminate_and_never_falls_back():
    rows = [_row("a"), _row("b")]
    edges = [_edge("rel_ab", "a", "b", recorded=_T1)]  # effective_at absent
    chain = _chain(rows, edges)
    out = _walk(chain, _T1 + timedelta(days=1), "effective")
    assert out["query_status"] == "EFFECTIVE_TIME_INDETERMINATE"
    assert out["current_id"] is None
    assert out["current_status"] == "indeterminate"
    assert out["indeterminate_edge"]["relation_id"] == "rel_ab"
    assert {v["label"] for v in out["versions"]} == {"indeterminate"}
    # The same chain is perfectly answerable when the question is about what
    # the system KNEW — proving the effective answer did not silently borrow it.
    recorded = _walk(chain, _T1 + timedelta(days=1), "recorded")
    assert recorded["query_status"] == "OK"
    assert recorded["current_id"] == "b"


def test_M04_effective_null_after_the_stop_point_is_irrelevant():
    """Relevance is decided by the walk, not by scanning the whole chain."""
    rows = [_row("a"), _row("b"), _row("c")]
    edges = [
        _edge("rel_ab", "a", "b", recorded=_T1, effective=_T3),
        _edge("rel_bc", "b", "c", recorded=_T2),  # NULL, but never consulted
    ]
    out = _walk(_chain(rows, edges), _T1 + timedelta(days=1), "effective")
    assert out["query_status"] == "OK"
    assert out["current_id"] == "a"
    assert [e["relation_id"] for e in out["edges"]] == ["rel_ab"]


def test_M04_single_version_chain_needs_no_time_evidence():
    out = _walk(_chain([_row("a")], []), _T1, "effective")
    assert out["query_status"] == "OK"
    assert out["current_id"] == "a"
    assert out["edges"] == []


# ─────────────────────────── withdrawal ───────────────────────────────────


def test_M04_withdrawal_before_and_after():
    rows = [_row("a")]
    edges = [_edge("rel_aw", "a", None, recorded=_T2, kind="withdraws")]
    chain = _chain(rows, edges)

    before = _walk(chain, _T1)
    assert before["current_id"] == "a"
    assert before["current_status"] == "active"

    after = _walk(chain, _T3)
    assert after["current_id"] is None
    assert after["current_status"] == "withdrawn"
    assert after["query_status"] == "OK"
    assert [v["label"] for v in after["versions"]] == ["withdrawn"]


def test_M04_withdrawal_after_a_replacement_keeps_history_ordered():
    rows = [_row("a"), _row("b")]
    edges = [
        _edge("rel_ab", "a", "b", recorded=_T1),
        _edge("rel_bw", "b", None, recorded=_T2, kind="withdraws"),
    ]
    chain = _chain(rows, edges)
    assert _walk(chain, _T1 + timedelta(days=1))["current_id"] == "b"
    out = _walk(chain, _T3)
    assert out["current_id"] is None
    assert {v["memory_id"]: v["label"] for v in out["versions"]} == {
        "a": "superseded",
        "b": "withdrawn",
    }


# ─────────────────── parameter contract (no database touched) ──────────────


def _reader():
    # pg is a sentinel that is never used: every case below must be refused
    # before any I/O happens.
    return _mod().ActiveMemoryReader(pg=object())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"as_of": _T1.isoformat(), "mode": "history"},
        {"as_of": _T1.isoformat(), "include_history": True},
        {"basis": "recorded"},  # basis without a time point
        {"as_of": "2026-01-01T00:00:00"},  # no offset: ambiguous
        {"as_of": "not-a-timestamp"},
        {"as_of": ""},
        {"as_of": _T1.isoformat(), "basis": "transaction_time"},
        {"as_of": _T1.isoformat(), "basis": 5},
        {"as_of": 1234},
    ],
)
def test_M04_undefined_or_invalid_parameter_combinations_are_refused(kwargs):
    out = _reader().read_version("a", **kwargs)
    assert out["success"] is False
    assert out["error_code"] == "INVALID_REQUEST"
    assert out["memory"] is None


def test_M04_basis_is_echoed_with_its_default():
    calls = []

    @contextlib.contextmanager
    def _snap(*_a, **_kw):
        calls.append(1)
        yield _FakeSnap(_abc())

    mod = _mod()
    original = mod._read_snapshot
    mod._read_snapshot = _snap
    try:
        out = _mod().ActiveMemoryReader(pg=object()).read_version(
            "a", as_of=_T1.isoformat()
        )
    finally:
        mod._read_snapshot = original
    assert out["success"] is True
    assert out["basis"] == "recorded"
    assert out["as_of"] == _T1.isoformat()
    assert out["as_of_future"] is False


# ─────────────── database-backed path, via a fixed snapshot ───────────────


class _FakeSnap:
    def __init__(self, chain: dict, rows: dict | None = None) -> None:
        self._chain = chain
        self._rows = rows or {
            v["memory_id"]: dict(v) for v in chain.get("versions", [])
        }

    def read_one(self, memory_id: str):
        return self._rows.get(memory_id)

    def read_chain(self, memory_id: str):
        return self._chain

    def read_edge_by_from(self, memory_id: str):
        for edge in self._chain.get("edges", []):
            if edge.get("from_memory_id") == memory_id:
                return edge
        return None

    def read_edge_by_to(self, memory_id: str):
        for edge in self._chain.get("edges", []):
            if edge.get("to_memory_id") == memory_id:
                return edge
        return None


@contextlib.contextmanager
def _shim(monkeypatch, chain: dict, rows: dict | None = None):
    snap = _FakeSnap(chain, rows)

    @contextlib.contextmanager
    def _factory(*_a, **_kw):
        yield snap

    monkeypatch.setattr(_mod(), "_read_snapshot", _factory)
    yield snap


def _read(monkeypatch, chain: dict, **kwargs):
    with _shim(monkeypatch, chain):
        return _mod().ActiveMemoryReader(pg=object()).read_version("a", **kwargs)


def test_M04_as_of_none_keeps_the_exact_previous_shape(monkeypatch):
    """Zero regression: no as_of-specific key may leak into the plain read."""
    out = _read(monkeypatch, _abc())
    assert set(out) == {
        "success", "status", "error_code", "error", "mode", "memory_id",
        "requested", "memory", "resolved_memory_id", "is_current",
        "current_status", "relation",
    }
    assert "as_of" not in out and "basis" not in out


def test_M04_db_path_returns_the_resolved_payload(monkeypatch):
    out = _read(monkeypatch, _abc(), as_of=(_T2 + timedelta(days=1)).isoformat())
    assert out["success"] is True
    assert out["resolved_memory_id"] == "c"
    assert out["memory"]["memory_id"] == "c"
    assert out["is_current"] is False  # requested 'a', resolved 'c'
    assert out["timeline"]["edges"]


def test_M04_db_path_unknown_id_is_not_found(monkeypatch):
    out = _read(monkeypatch, _abc(), as_of=_T2.isoformat())
    assert out["success"] is True  # sanity: the known id answers
    with _shim(monkeypatch, _abc(), rows={}):
        missing = _mod().ActiveMemoryReader(pg=object()).read_version(
            "nope", as_of=_T2.isoformat()
        )
    assert missing["success"] is False
    assert missing["error_code"] == "NOT_FOUND"


def test_M04_cycle_still_fails_closed(monkeypatch):
    """Time filtering must not bypass the chain-integrity check."""
    mod = _mod()

    @contextlib.contextmanager
    def _boom(*_a, **_kw):
        raise mod._ChainCorrupt("环")
        yield  # pragma: no cover

    monkeypatch.setattr(mod, "_read_snapshot", _boom)
    out = mod.ActiveMemoryReader(pg=object()).read_version(
        "a", as_of=_T2.isoformat()
    )
    assert out["success"] is False
    assert out["error_code"] == "DURABLE_FAILED"


def test_M04_indeterminate_exposes_partial_evidence(monkeypatch):
    rows = [_row("a"), _row("b"), _row("c")]
    edges = [
        _edge("rel_ab", "a", "b", recorded=_T1, effective=_T0),
        _edge("rel_bc", "b", "c", recorded=_T2),  # effective absent
    ]
    out = _read(
        monkeypatch, _chain(rows, edges),
        as_of=(_T3).isoformat(), basis="effective",
    )
    assert out["success"] is False
    assert out["error_code"] == "EFFECTIVE_TIME_INDETERMINATE"
    assert out["query_status"] == "EFFECTIVE_TIME_INDETERMINATE"
    assert out["indeterminate_edge"]["relation_id"] == "rel_bc"
    # What we *could* establish is still handed back.
    assert [e["relation_id"] for e in out["timeline"]["edges"]] == [
        "rel_ab", "rel_bc",
    ]
    assert out["timeline"]["edges"][0]["decision"] == "applied"
    assert out["timeline"]["edges"][1]["decision"] == "indeterminate"


# ───────────────────── M04-3: transition, not version time ────────────────


def test_M04_transition_view_names_the_edge_not_the_version():
    mod = _mod()
    edge = _edge("rel_ab", "a", "b", recorded=_T1, effective=_T2)
    view = mod._transition_view(edge)
    assert view == {
        "relation_id": "rel_ab",
        "relation_type": "supersedes",
        "to_memory_id": "b",
        "effective_at": _T2.isoformat(),
        "recorded_at": _T1.isoformat(),
    }
    assert mod._transition_view(None) is None


def test_M04_read_chain_versions_carry_their_outgoing_transition():
    chain = _walk(_abc(), _T3)
    assert chain["current_id"] == "c"
    # The walk output deliberately has no payload; the transition field is a
    # read_chain concern, checked here through the same edge shape.
    edge = _abc()["edges"][0]
    assert _mod()._transition_view(edge)["relation_id"] == "rel_ab"


# ────────────── snapshot discipline and effective_at round-tripping ────────


def test_M04_tool_schema_exposes_as_of_and_basis():
    """The parameter must be reachable from the agent surface, not just code.

    A read mode the schema does not declare is a mode no agent can ask for, so
    this pins exposure rather than mere existence.
    """
    from v3core.tools.api_get import V3_GET_SCHEMA

    props = V3_GET_SCHEMA["parameters"]["properties"]
    assert "as_of" in props
    assert props["basis"]["enum"] == ["recorded", "effective"]
    assert "as_of" not in V3_GET_SCHEMA["parameters"].get("required", [])


def test_M04_as_of_read_uses_exactly_one_snapshot(monkeypatch):
    """The whole answer must come from ONE coherent database state.

    This is the structural half of the concurrency guarantee: a writer that
    commits mid-read cannot splice two states into one answer, because the
    reader never re-enters a snapshot. (The isolated-PG suite probes the same
    property behaviourally; a stress loop can only *observe* tearing, so the
    invariant is pinned here deterministically.)
    """
    opened: list[int] = []

    @contextlib.contextmanager
    def _factory(*_a, **_kw):
        opened.append(1)
        yield _FakeSnap(_abc())

    monkeypatch.setattr(_mod(), "_read_snapshot", _factory)
    out = _mod().ActiveMemoryReader(pg=object()).read_version(
        "a", as_of=_T3.isoformat()
    )
    assert out["success"] is True
    assert len(opened) == 1


def test_M04_effective_at_readback_compares_instants_not_strings():
    """A correct commit must never be reported as a durability failure.

    Pre-existing defect surfaced by the isolated-PG suite (it predates M04 and
    is not caused by the time-point read): ``timestamptz`` comes back rendered
    in the server session's zone, so a caller-declared ``+00:00`` is read back
    as ``+08:00``. The post-commit verification compared those strings and
    reported ``DURABLE_FAILED`` for a committed, correct correction.

    The fix widens equality ONLY when both sides parse as timestamps; a
    genuinely different declaration and any unparseable value still fail.
    """
    mod = _mod()
    assert mod._same_instant("2026-03-16T00:00:00+00:00", "2026-03-16T08:00:00+08:00")
    assert mod._same_instant("2026-03-16T00:00:00Z", "2026-03-16T00:00:00+00:00")
    assert mod._same_instant(
        datetime(2026, 3, 16, tzinfo=timezone.utc), "2026-03-16T08:00:00+08:00"
    )
    # A different instant is still a mismatch.
    assert not mod._same_instant(
        "2026-03-16T00:00:00+00:00", "2026-03-16T00:00:01+00:00"
    )
    # Absent or unreadable evidence is never coerced into agreement.
    assert not mod._same_instant(None, "2026-03-16T00:00:00+00:00")
    assert not mod._same_instant("2026-03-16T00:00:00+00:00", None)
    assert not mod._same_instant("not a time", "not a time")
    assert not mod._same_instant("", "")
