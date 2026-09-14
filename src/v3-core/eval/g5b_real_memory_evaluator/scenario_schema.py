"""Scenario schema (data classes + JSON loader) for the G5b evaluator.

A scenario is a small JSON document that drives the evaluator:

  {
    "scenario_id": "G5b-UP-001",
    "category": "USER_PREFERENCE",
    "description": "User prefers dark mode for editor.",
    "fixtures": [
      {
        "memory_id": "mem_<sha>",          # optional; derived if missing
        "category": "preference",
        "title": "editor color theme",
        "content": "User prefers dark mode for the editor.",
        "tags": ["ui", "theme"],
        "source_id": "mem_<sha>"            # optional alias; if set must match memory_id
      },
      ...
    ],
    "explicit_memories": [                 # optional alias of ``fixtures``;
                                           # accepted for compatibility with
                                           # datasets that name the field after
                                           # the underlying PG table. When both
                                           # are present, ``fixtures`` wins
                                           # and a duplicate is reported via
                                           # ScenarioLoadError to avoid silent
                                           # double-counting.
      {...same shape as a fixture...}
    ],
    "queries": [
      {
        "query_id": "Q1",
        "text": "what theme does the user prefer in the editor?",
        "must_recall": ["mem_<sha>"],
        "must_not_recall": ["mem_<other-sha>"],
        "must_recall_rank_max": 3,         # optional; default 3
        "expected_status": "active"        # optional; default "active"
      }
    ],
    "actions": [
      # optional pre-recall actions to mutate state
      {"type": "archive", "memory_id": "mem_..."},
      {"type": "create_extra", "category": "...", "title": "...", "content": "...", "tags": [...]}
    ],
    "sessions": [                          # optional; conversation sessions
                                           # associated with this scenario
      {
        "session_id": "S1",
        "conversation_turns": [           # primary field name for turns
          {"role": "user",      "content": "..."},
          {"role": "assistant", "content": "..."}
        ],
        "turns": [...],                    # accepted alias of
                                           # ``conversation_turns``; when
                                           # both are present
                                           # ``conversation_turns`` wins
                                           # and a duplicate triggers
                                           # ScenarioLoadError
        "notes": "..."
      }
    ],
    "expected_lane_results": {              # optional; lane expectations
      "keyword": {"any_of": ["mem_..."]},
      "vector": {"any_of": ["mem_..."]}
    },
    "notes": "..."                          # free-form reviewer notes
  }

Compatibility fields (this version):

  * ``explicit_memories`` is accepted as an alias of ``fixtures``.
    Both lists are parsed with the same shape. ``fixtures`` wins when
    both are present; a duplicate is reported via ``ScenarioLoadError``
    so authors do not silently double-seed.
  * ``sessions`` is an optional list of session blocks. Each session
    has either ``conversation_turns`` (primary) or ``turns`` (alias).
    ``conversation_turns`` wins when both are present; ``turns`` is
    provided for older hand-authored scenarios that used the shorter
    name.
  * ``fixtures``, ``queries``, and ``actions`` remain the canonical
    fields and continue to work as in v1.

Categories (must be one of):

  USER_PREFERENCE, PROJECT_DECISION, SESSION_CONTINUITY, STABLE_FACT,
  CONFLICT_UPDATE, DISTRACTOR, NEGATIVE_RECALL, ARCHIVE,
  LONG_DISTANCE, MULTI_RELEVANT, AMBIGUOUS, TEMPORAL_UPDATE.

This module only validates + loads scenarios; it never executes them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable


ALLOWED_CATEGORIES: tuple[str, ...] = (
    "USER_PREFERENCE",
    "PROJECT_DECISION",
    "SESSION_CONTINUITY",
    "STABLE_FACT",
    "CONFLICT_UPDATE",
    "DISTRACTOR",
    "NEGATIVE_RECALL",
    "ARCHIVE",
    "LONG_DISTANCE",
    "MULTI_RELEVANT",
    "AMBIGUOUS",
    "TEMPORAL_UPDATE",
)


# ── dataclasses ───────────────────────────────────────────────────────────


@dataclass
class Fixture:
    """One explicit-memory row the scenario seeds into the store."""

    category: str
    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    memory_id: str | None = None
    source_id: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class Query:
    """One read query the scenario asserts over."""

    query_id: str
    text: str
    must_recall: list[str] = field(default_factory=list)
    must_not_recall: list[str] = field(default_factory=list)
    must_recall_rank_max: int = 3
    expected_status: str = "active"
    lane: str = "auto"  # one of: keyword | vector | auto
    expect_empty_recall: bool = False  # if True, runner expects 0 candidates


@dataclass
class Action:
    """A pre-recall mutation against the store.

    Use ``fixture_index`` to reference the i-th fixture in the parent
    scenario (preferred when memory_id is auto-derived). Use
    ``memory_id`` for explicit IDs. If both are set, memory_id wins.
    """

    type: str  # archive | create_extra | archive_hard
    memory_id: str | None = None
    fixture: Fixture | None = None
    fixture_index: int | None = None


@dataclass
class ConversationTurn:
    """One message turn in a conversation session.

    ``role`` must be a non-empty string (``"user"``, ``"assistant"``,
    or any other role string the author wants to record). ``content``
    must be a non-empty string. ``meta`` is an optional free-form
    object for author notes.
    """

    role: str
    content: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    """A conversation session associated with a scenario.

    Sessions are an OPTIONAL part of a scenario record — they let the
    author pin one or more multi-turn conversations that motivated the
    fixtures/queries. The runner does not execute session content
    today; sessions are persisted on the Scenario object so future
    lanes can use them. ``conversation_turns`` is the primary field
    name; ``turns`` is accepted as a backward-compatible alias.
    """

    session_id: str
    conversation_turns: list[ConversationTurn] = field(default_factory=list)
    notes: str = ""


@dataclass
class Scenario:
    """One top-level scenario record."""

    scenario_id: str
    category: str
    description: str
    fixtures: list[Fixture] = field(default_factory=list)
    queries: list[Query] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    notes: str = ""
    unsupported: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


# ── loading ───────────────────────────────────────────────────────────────


class ScenarioLoadError(ValueError):
    """Raised when a scenario JSON fails schema validation."""


def _as_str_list(v: Any, field_name: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise ScenarioLoadError(
            f"{field_name} must be a list[str]; got {type(v).__name__}"
        )
    return list(v)


def _fixture_from_obj(obj: Any, idx: int) -> Fixture:
    if not isinstance(obj, dict):
        raise ScenarioLoadError(f"fixtures[{idx}] must be an object")
    for fld in ("category", "title", "content"):
        if not isinstance(obj.get(fld), str) or not obj[fld]:
            raise ScenarioLoadError(
                f"fixtures[{idx}].{fld} must be a non-empty string"
            )
    tags = _as_str_list(obj.get("tags"), f"fixtures[{idx}].tags")
    memory_id = obj.get("memory_id")
    source_id = obj.get("source_id")
    if memory_id is not None and not isinstance(memory_id, str):
        raise ScenarioLoadError(
            f"fixtures[{idx}].memory_id must be a string when present"
        )
    if source_id is not None and not isinstance(source_id, str):
        raise ScenarioLoadError(
            f"fixtures[{idx}].source_id must be a string when present"
        )
    provenance = obj.get("provenance") or {}
    if not isinstance(provenance, dict):
        raise ScenarioLoadError(
            f"fixtures[{idx}].provenance must be an object when present"
        )
    return Fixture(
        category=obj["category"],
        title=obj["title"],
        content=obj["content"],
        tags=tags,
        memory_id=memory_id,
        source_id=source_id,
        provenance=provenance,
    )


def _query_from_obj(obj: Any, idx: int) -> Query:
    if not isinstance(obj, dict):
        raise ScenarioLoadError(f"queries[{idx}] must be an object")
    text = obj.get("text")
    qid = obj.get("query_id") or f"Q{idx + 1}"
    if not isinstance(text, str) or not text:
        raise ScenarioLoadError(f"queries[{idx}].text must be a non-empty string")
    lane = obj.get("lane", "auto")
    if lane not in ("keyword", "vector", "auto"):
        raise ScenarioLoadError(
            f"queries[{idx}].lane must be one of keyword|vector|auto"
        )
    rank_max = obj.get("must_recall_rank_max", 3)
    if not isinstance(rank_max, int) or rank_max < 1:
        raise ScenarioLoadError(
            f"queries[{idx}].must_recall_rank_max must be a positive int"
        )
    expected_status = obj.get("expected_status", "active")
    if expected_status not in ("active", "archived", "any"):
        raise ScenarioLoadError(
            f"queries[{idx}].expected_status must be active|archived|any"
        )
    return Query(
        query_id=str(qid),
        text=text,
        must_recall=_as_str_list(obj.get("must_recall"), f"queries[{idx}].must_recall"),
        must_not_recall=_as_str_list(
            obj.get("must_not_recall"), f"queries[{idx}].must_not_recall"
        ),
        must_recall_rank_max=rank_max,
        expected_status=expected_status,
        lane=lane,
        expect_empty_recall=bool(obj.get("expect_empty_recall", False)),
    )


def _action_from_obj(obj: Any, idx: int) -> Action:
    if not isinstance(obj, dict):
        raise ScenarioLoadError(f"actions[{idx}] must be an object")
    typ = obj.get("type")
    if typ not in ("archive", "create_extra", "archive_hard"):
        raise ScenarioLoadError(
            f"actions[{idx}].type must be archive|create_extra|archive_hard"
        )
    memory_id = obj.get("memory_id")
    if memory_id is not None and not isinstance(memory_id, str):
        raise ScenarioLoadError(
            f"actions[{idx}].memory_id must be a string when present"
        )
    fixture_index = obj.get("fixture_index")
    if fixture_index is not None and (
        not isinstance(fixture_index, int) or fixture_index < 0
    ):
        raise ScenarioLoadError(
            f"actions[{idx}].fixture_index must be a non-negative int"
        )
    fixture = None
    if typ == "create_extra":
        fixture = _fixture_from_obj(obj, idx)
    return Action(
        type=typ,
        memory_id=memory_id,
        fixture=fixture,
        fixture_index=fixture_index,
    )


def _turn_from_obj(obj: Any, sidx: int, tidx: int) -> ConversationTurn:
    if not isinstance(obj, dict):
        raise ScenarioLoadError(
            f"sessions[{sidx}].turns[{tidx}] must be an object"
        )
    role = obj.get("role")
    content = obj.get("content")
    if not isinstance(role, str) or not role:
        raise ScenarioLoadError(
            f"sessions[{sidx}].turns[{tidx}].role must be a non-empty string"
        )
    if not isinstance(content, str) or not content:
        raise ScenarioLoadError(
            f"sessions[{sidx}].turns[{tidx}].content must be a non-empty string"
        )
    meta = obj.get("meta") or {}
    if not isinstance(meta, dict):
        raise ScenarioLoadError(
            f"sessions[{sidx}].turns[{tidx}].meta must be an object when present"
        )
    return ConversationTurn(role=role, content=content, meta=meta)


def _session_from_obj(obj: Any, idx: int) -> Session:
    if not isinstance(obj, dict):
        raise ScenarioLoadError(f"sessions[{idx}] must be an object")
    sid = obj.get("session_id")
    if not isinstance(sid, str) or not sid:
        raise ScenarioLoadError(
            f"sessions[{idx}].session_id must be a non-empty string"
        )
    has_turns = "conversation_turns" in obj
    has_alias = "turns" in obj
    if has_turns and has_alias:
        raise ScenarioLoadError(
            f"sessions[{idx}] specifies both 'conversation_turns' and "
            "the 'turns' alias; choose one to avoid silent ambiguity"
        )
    if has_turns:
        turns_raw = obj.get("conversation_turns")
    else:
        turns_raw = obj.get("turns", [])
    if turns_raw is None:
        turns_raw = []
    if not isinstance(turns_raw, list):
        raise ScenarioLoadError(
            f"sessions[{idx}] turn list must be an array (conversation_turns|turns)"
        )
    turns = [_turn_from_obj(t, idx, i) for i, t in enumerate(turns_raw)]
    notes = obj.get("notes") or ""
    if not isinstance(notes, str):
        raise ScenarioLoadError(
            f"sessions[{idx}].notes must be a string when present"
        )
    return Session(session_id=sid, conversation_turns=turns, notes=notes)


def scenario_from_dict(obj: dict[str, Any]) -> Scenario:
    """Validate one scenario JSON object and return a Scenario dataclass."""
    if not isinstance(obj, dict):
        raise ScenarioLoadError("scenario must be a JSON object")
    for fld in ("scenario_id", "category"):
        if not isinstance(obj.get(fld), str) or not obj[fld]:
            raise ScenarioLoadError(f"{fld} must be a non-empty string")
    sid = obj["scenario_id"]
    cat = obj["category"]
    if cat not in ALLOWED_CATEGORIES:
        raise ScenarioLoadError(
            f"scenario {sid!r} has unknown category {cat!r}; "
            f"expected one of {ALLOWED_CATEGORIES}"
        )
    desc = obj.get("description") or ""
    has_fixtures = "fixtures" in obj
    has_explicit_memories = "explicit_memories" in obj
    if has_fixtures and has_explicit_memories:
        raise ScenarioLoadError(
            f"scenario {sid!r} specifies both 'fixtures' and "
            "'explicit_memories'; choose one to avoid silent double-seeding"
        )
    if has_fixtures:
        fixtures_raw = obj.get("fixtures") or []
    else:
        fixtures_raw = obj.get("explicit_memories") or []
    fixtures = [
        _fixture_from_obj(x, i)
        for i, x in enumerate(fixtures_raw)
    ]
    queries = [
        _query_from_obj(x, i)
        for i, x in enumerate(obj.get("queries") or [])
    ]
    if not queries:
        raise ScenarioLoadError(
            f"scenario {sid!r} must have at least one query"
        )
    actions = [
        _action_from_obj(x, i)
        for i, x in enumerate(obj.get("actions") or [])
    ]
    sessions = [
        _session_from_obj(x, i)
        for i, x in enumerate(obj.get("sessions") or [])
    ]
    unsupported = bool(obj.get("unsupported", False))
    notes = obj.get("notes") or ""
    return Scenario(
        scenario_id=sid,
        category=cat,
        description=desc,
        fixtures=fixtures,
        queries=queries,
        actions=actions,
        sessions=sessions,
        notes=notes,
        unsupported=unsupported,
        raw=obj,
    )


def load_scenarios(path: str) -> list[Scenario]:
    """Load a JSON file containing a list of scenarios (or one scenario).

    Returns an empty list for an empty list; raises ScenarioLoadError
    on any schema violation.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        return [scenario_from_dict(x) for x in raw]
    if isinstance(raw, dict) and "scenarios" in raw and isinstance(raw["scenarios"], list):
        return [scenario_from_dict(x) for x in raw["scenarios"]]
    if isinstance(raw, dict):
        return [scenario_from_dict(raw)]
    raise ScenarioLoadError(
        f"scenario file must be a list or an object; got {type(raw).__name__}"
    )


def count_by_category(scenarios: Iterable[Scenario]) -> dict[str, int]:
    out: dict[str, int] = {c: 0 for c in ALLOWED_CATEGORIES}
    for s in scenarios:
        out[s.category] = out.get(s.category, 0) + 1
    return out


__all__ = [
    "ALLOWED_CATEGORIES",
    "ScenarioLoadError",
    "Fixture",
    "Query",
    "Action",
    "ConversationTurn",
    "Session",
    "Scenario",
    "scenario_from_dict",
    "load_scenarios",
    "count_by_category",
]