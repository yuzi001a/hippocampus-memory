# -*- coding: utf-8 -*-
"""A0 explicit-memory opt-in contract tests (v3-core).

This file pins the **description / default** layer of the five canonical
explicit-memory write entry points. It does NOT test runtime behavior.

Scope of this file:
  * ``V3_STORE_SCHEMA``   — opt-in contract wording in ``description``.
  * ``V3_ADD_SCHEMA``     — same.
  * ``HM_WRITE_SCHEMA``   — same.
  * ``V3_EXTRACT_SCHEMA`` — opt-in wording + ``write.default == False``.
  * ``V3_MANAGE_SCHEMA``  — opt-in wording on ``action="extract"`` path
                            + ``write.default == False``.

No LLM intent classifier. No natural-language NLP. No PG/SQLite/embed.
Only schema dict introspection + skill/docs string containment.

Import strategy:
  We use ``AST/static source checks`` instead of importing v3core.
  The schema dicts are pure data (string fields + parameter dicts),
  and importing the tools package triggers the heavy v3core
  ``__init__`` side effects (PG pool bootstrap, observer import,
  hermes_v3 vendoring, etc.).  Reading the files as text and asserting
  on the resulting AST avoids that side-effect load while still
  pinning the exact schema descriptions and parameter defaults.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

# Repository layout:
#   <repo_root>/src/v3-core/src/v3core/tools/<file>.py
#   <repo_root>/src/v3-hermes-plugin/src/v3hermes/skills/v3-workflow/SKILL.md
#   <repo_root>/docs/WRITE-FLOW-CONTRACT.md
# Use the file's parents: tests/ -> v3-core/ -> src/ -> repo.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_V3CORE_SRC = _REPO_ROOT / "src" / "v3-core" / "src"
_WRITE_FLOW_DOC = _REPO_ROOT / "docs" / "WRITE-FLOW-CONTRACT.md"
_V3HERMES_SKILL = (
    _REPO_ROOT
    / "src"
    / "v3-hermes-plugin"
    / "src"
    / "v3hermes"
    / "skills"
    / "v3-workflow"
    / "SKILL.md"
)


def _read_schema_dicts(file_path: Path) -> dict:
    """Parse a tool-schema module as text and return top-level name->dict.

    Each tool module defines exactly one ``V3_*_SCHEMA`` /
    ``HM_WRITE_SCHEMA`` constant at module top-level.  We parse the
    source with ``ast.parse``, walk top-level ``Assign`` nodes whose
    target name starts with ``V3_`` or ``HM_WRITE_``, and extract the
    literal dict — including string concatenation (``a + b``) at any
    depth.  This avoids importing v3core (no PG pool bootstrap, no
    observer import, no hermes_v3 vendoring).
    """
    source = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    def _eval(node):
        # Recursively evaluate literal AST nodes into Python objects.
        # Handles: Constant, JoinedStr, FormattedValue, BinOp(Add of
        # strings), Dict, List, Tuple, Name(only top-level names that
        # already resolve), UnaryOp(USub for numbers), Call(int/float).
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                v = _eval(v)
                if isinstance(v, str):
                    parts.append(v)
                else:
                    parts.append(str(v))
            return "".join(parts)
        if isinstance(node, ast.FormattedValue):
            return _eval(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return _eval(node.left) + _eval(node.right)
        if isinstance(node, ast.Dict):
            return {_eval(k): _eval(v) for k, v in zip(node.keys, node.values)}
        if isinstance(node, (ast.List, ast.Tuple)):
            return [_eval(e) for e in node.elts]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -_eval(node.operand)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name in {"int", "float"} and len(node.args) == 1:
                return _eval(node.args[0])
        # Fall back: not a literal we recognise.
        return _UNRESOLVED

    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and (
                    tgt.id.startswith("V3_") or tgt.id.startswith("HM_")
                ):
                    val = _eval(node.value)
                    if val is not _UNRESOLVED:
                        out[tgt.id] = val
    return out


_UNRESOLVED = object()


# Parse the five schema dicts directly from source.
_SCHEMAS_BY_FILE = {
    "store.py": _read_schema_dicts(_V3CORE_SRC / "v3core" / "tools" / "store.py"),
    "api_add.py": _read_schema_dicts(_V3CORE_SRC / "v3core" / "tools" / "api_add.py"),
    "write_tool.py": _read_schema_dicts(
        _V3CORE_SRC / "v3core" / "tools" / "write_tool.py"
    ),
    "extract_tool.py": _read_schema_dicts(
        _V3CORE_SRC / "v3core" / "tools" / "extract_tool.py"
    ),
    "api_manage.py": _read_schema_dicts(
        _V3CORE_SRC / "v3core" / "tools" / "api_manage.py"
    ),
}

V3_STORE_SCHEMA = _SCHEMAS_BY_FILE["store.py"]["V3_STORE_SCHEMA"]
V3_ADD_SCHEMA = _SCHEMAS_BY_FILE["api_add.py"]["V3_ADD_SCHEMA"]
HM_WRITE_SCHEMA = _SCHEMAS_BY_FILE["write_tool.py"]["HM_WRITE_SCHEMA"]
V3_EXTRACT_SCHEMA = _SCHEMAS_BY_FILE["extract_tool.py"]["V3_EXTRACT_SCHEMA"]
V3_MANAGE_SCHEMA = _SCHEMAS_BY_FILE["api_manage.py"]["V3_MANAGE_SCHEMA"]


# Phrases that must appear in the positive authorization wording of every
# canonical explicit-memory write entry point. We allow either the
# canonical v3_store phrasing ("allowed ONLY when the user explicitly
# asks to remember / store / save / retain a specific durable item, or an
# explicitly authorized host workflow") OR the delegation phrasing
# ("user asks to remember", "explicit authorization", "host workflow")
# for v3_extract / v3_manage, which forward the contract via the
# opt-in / explicit-memory + host-workflow language.
POSITIVE_PHRASES_CANONICAL = (
    "OPT-IN",
    "explicit-memory",
    "allowed ONLY when the user explicitly asks to remember",
)

POSITIVE_PHRASES_DELEGATION = (
    "opt-in",
    "explicit-memory",
    "user asks to remember",
    "explicit authorization",
    "host workflow",
)

# Phrases that must appear in the negative-list wording (at least N of
# them — these are the "not authorization" categories enumerated in the
# branch's task contract).
NEGATIVE_PHRASES = (
    "dev experience",
    "reviewer findings",
    "debugging notes",
    "task status",
    "implementation decisions",
    "inferred preferences",
    "summarize tonight",
)


# Phrase that pin the canonical durable store name.
EXPLICIT_MEMORIES_PHRASE = "public.explicit_memories"


def _description(schema: dict) -> str:
    """Return the schema description (str) — fail loud if not a string."""
    desc = schema.get("description")
    assert isinstance(desc, str) and desc, (
        f"schema {schema.get('name')!r} must have a non-empty string description"
    )
    return desc


def _write_param(schema: dict) -> dict:
    """Return the ``write`` parameter dict from a schema, or {} if absent."""
    params = schema.get("parameters") or {}
    props = params.get("properties") or {}
    return props.get("write") or {}


@pytest.mark.parametrize(
    "schema, name",
    [
        (V3_STORE_SCHEMA, "v3_store"),
        (V3_ADD_SCHEMA, "v3_add"),
        (HM_WRITE_SCHEMA, "hm_write"),
        (V3_EXTRACT_SCHEMA, "v3_extract"),
        (V3_MANAGE_SCHEMA, "v3_manage"),
    ],
)
def test_a0_description_contains_opt_in_positive_phrases(schema, name):
    """Each entry-point description must carry the opt-in wording.

    v3_store / v3_add / hm_write describe the contract directly
    (canonical phrasing).  v3_extract / v3_manage delegate to it and
    inherit via ``follows the v3_store / v3_add opt-in contract`` plus
    ``user asks to remember`` / ``host workflow`` / ``explicit
    authorization``.  Either shape is acceptable — but every phrase in
    the canonical set OR every phrase in the delegation set must be
    present (no partial match).
    """
    desc = _description(schema)
    desc_lower = desc.lower()
    canonical_hit = all(p.lower() in desc_lower for p in POSITIVE_PHRASES_CANONICAL)
    delegation_hit = all(p.lower() in desc_lower for p in POSITIVE_PHRASES_DELEGATION)
    assert canonical_hit or delegation_hit, (
        f"{name} description must satisfy either canonical or delegation "
        f"opt-in phrasing.\n"
        f"  canonical missing: {[p for p in POSITIVE_PHRASES_CANONICAL if p.lower() not in desc_lower]!r}\n"
        f"  delegation missing: {[p for p in POSITIVE_PHRASES_DELEGATION if p.lower() not in desc_lower]!r}\n"
        f"  actual: {desc[:400]}..."
    )


@pytest.mark.parametrize(
    "schema, name",
    [
        (V3_STORE_SCHEMA, "v3_store"),
        (V3_ADD_SCHEMA, "v3_add"),
        (HM_WRITE_SCHEMA, "hm_write"),
        (V3_EXTRACT_SCHEMA, "v3_extract"),
        (V3_MANAGE_SCHEMA, "v3_manage"),
    ],
)
def test_a0_description_contains_negative_authorization_phrases(schema, name):
    """At least 4 of the 7 negative-list phrases must be present."""
    desc = _description(schema).lower()
    hits = [p for p in NEGATIVE_PHRASES if p in desc]
    assert len(hits) >= 4, (
        f"{name} description must list at least 4 negative-authorization "
        f"phrases; got {hits!r}"
    )


@pytest.mark.parametrize(
    "schema, name",
    [
        (V3_STORE_SCHEMA, "v3_store"),
        (V3_ADD_SCHEMA, "v3_add"),
        (HM_WRITE_SCHEMA, "hm_write"),
    ],
)
def test_a0_canonical_card_schemas_name_public_explicit_memories(schema, name):
    """The three "card-style" schemas must reference the canonical store."""
    desc = _description(schema)
    assert EXPLICIT_MEMORIES_PHRASE in desc, (
        f"{name} description must mention {EXPLICIT_MEMORIES_PHRASE!r} "
        f"so callers see which durable store they are writing to"
    )


def test_a0_v3_extract_write_default_is_false_and_described_as_opt_in():
    """v3_extract(write=...) must default to non-durable preview."""
    write = _write_param(V3_EXTRACT_SCHEMA)
    assert write.get("default") is False, (
        f"v3_extract.write default must be False (preview-only); "
        f"got {write.get('default')!r}"
    )
    desc = (write.get("description") or "").lower()
    assert "opt-in" in desc, (
        f"v3_extract.write description must say 'opt-in'; got {write.get('description')!r}"
    )
    assert "non-durable" in desc or "preview" in desc, (
        f"v3_extract.write description must mention preview/non-durable; "
        f"got {write.get('description')!r}"
    )


def test_a0_v3_manage_extract_write_default_is_false():
    """v3_manage(action='extract').write must default to False."""
    write = _write_param(V3_MANAGE_SCHEMA)
    assert write.get("default") is False, (
        f"v3_manage.write default must be False; got {write.get('default')!r}"
    )
    desc = (write.get("description") or "").lower()
    assert "opt-in" in desc, (
        f"v3_manage.write description must say 'opt-in'; got {write.get('description')!r}"
    )


def test_a0_v3_extract_description_explicitly_states_write_true_is_durable():
    """v3_extract description must warn that write=True is a canonical write."""
    desc = _description(V3_EXTRACT_SCHEMA).lower()
    assert "write=true" in desc, (
        "v3_extract description must explicitly explain write=True behavior"
    )
    assert "canonical" in desc or "durable" in desc, (
        "v3_extract description must call write=True a canonical/durable write"
    )


def test_a0_v3_manage_description_explicitly_delegates_to_v3_extract():
    """v3_manage(action='extract') must point at v3_extract so the contract
    inherits cleanly."""
    desc = _description(V3_MANAGE_SCHEMA).lower()
    assert "v3_extract" in desc, (
        "v3_manage description must reference v3_extract delegation"
    )
    assert "opt-in" in desc or "explicit-memory" in desc, (
        "v3_manage description must propagate opt-in / explicit-memory wording"
    )


def test_a0_no_runtime_behavior_change_to_handlers():
    """A0 must NOT alter the runtime call graph of the write handlers.

    The handler functions are defined in the same source files as the
    schema dicts we statically parsed above.  This test pins that the
    handler names still exist as top-level ``def`` statements in the
    source, so a future refactor that silently renames them fails loudly
    here.
    """
    import ast

    expected = {
        "store.py": ["handle_v3_store"],
        "api_add.py": ["handle_v3_add"],
        "write_tool.py": ["handle_hm_write"],
        "extract_tool.py": ["handle_v3_extract"],
        "api_manage.py": ["handle_v3_manage"],
    }
    for filename, fn_names in expected.items():
        src = (_V3CORE_SRC / "v3core" / "tools" / filename).read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        defined = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        missing = [n for n in fn_names if n not in defined]
        assert not missing, (
            f"{filename} must still define {fn_names}; missing {missing!r}"
        )


def test_a0_write_flow_contract_section_exists_and_pins_contract():
    """The branch's authoritative doc must contain a §A0 opt-in section."""
    if not _WRITE_FLOW_DOC.exists():
        pytest.skip(f"WRITE-FLOW-CONTRACT.md not found at {_WRITE_FLOW_DOC}")
    doc = _WRITE_FLOW_DOC.read_text(encoding="utf-8")

    # Must have the §A0 heading (allow flexible whitespace).
    assert re.search(r"^##\s*A0\.", doc, re.MULTILINE), (
        "WRITE-FLOW-CONTRACT.md must contain a `## A0.` section heading"
    )
    # Must mention opt-in, public.explicit_memories, and a denial phrase.
    needles = ("opt-in", EXPLICIT_MEMORIES_PHRASE)
    for needle in needles:
        assert needle in doc, (
            f"WRITE-FLOW-CONTRACT.md §A0 must contain {needle!r}"
        )
    # The Chinese negation phrases must also be present.
    cn_negatives = ("不允许", "禁止")
    assert any(p in doc for p in cn_negatives), (
        f"WRITE-FLOW-CONTRACT.md §A0 must contain at least one of {cn_negatives!r}"
    )


def test_a0_v3_workflow_skill_text_pins_opt_in_wording():
    """v3-workflow SKILL.md must reflect the opt-in framing."""
    if not _V3HERMES_SKILL.exists():
        pytest.skip(f"v3-workflow SKILL.md not at {_V3HERMES_SKILL} (out of tree)")
    text = _V3HERMES_SKILL.read_text(encoding="utf-8")

    # Positive: opt-in wording.
    assert "OPT-IN" in text or "opt-in" in text, (
        "v3-workflow SKILL.md must include opt-in wording"
    )
    # Negative: at least 3 of the 6 negative-list Chinese phrases.
    cn_neg = (
        "开发经验",
        "评审发现",
        "调试笔记",
        "任务状态",
        "实现决策",
        "今晚总结一下",
    )
    hits = [p for p in cn_neg if p in text]
    assert len(hits) >= 3, (
        f"v3-workflow SKILL.md must include at least 3 negative-list "
        f"phrases; got {hits!r}"
    )
    # The old misleading "手帐 category='shou_zhang' 自动注册" framing
    # must be gone — A0 replaces it with opt-in wording.
    assert "shou_zhang' 自动注册" not in text, (
        "v3-workflow SKILL.md must NOT retain the misleading "
        "'手帐 category=shou_zhang 自动注册' persistent framing"
    )
