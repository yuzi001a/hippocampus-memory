# -*- coding: utf-8 -*-
"""A0 explicit-memory opt-in contract tests (v3-hermes-plugin side).

This file pins the **user/model-visible wording** of the v3-workflow
skill so that:

  * The skill text contains opt-in authorization language.
  * The skill text contains the negative-authorization list
    (开发经验 / 评审发现 / 调试笔记 / 任务状态 / 实现决策 / 今晚总结一下
    etc.).
  * The old misleading "手帐 category='shou_zhang' 自动注册" persistent
    framing is gone.
  * The skill still propagates the public.explicit_memories canonical
    store name so callers know where the durable write goes.

It also re-pins the same set of schema contracts via the public
``v3core.tools.*`` schema dicts so a future refactor that silently drops
the opt-in wording from one tree but keeps the other is caught.

No LLM intent classifier. No natural-language NLP. No PG/SQLite/embed.
Only schema dict introspection + skill text string containment.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# Make this worktree's v3core / v3hermes importable regardless of any
# production editable install (same pattern as test_a0_* in v3-core).
# Use the directory's parents: tests/ -> v3-hermes-plugin/ -> src/ -> repo,
# so the repo root is parents[2] from the directory (parents[3] would
# be one level too shallow — the directory loses the file-name level).
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]  # tests/ -> v3-hermes-plugin/ -> src/ -> repo
_WORKTREE_V3CORE_SRC = _REPO_ROOT / "src" / "v3-core" / "src"
SKILL_PATH = (
    _REPO_ROOT
    / "src"
    / "v3-hermes-plugin"
    / "src"
    / "v3hermes"
    / "skills"
    / "v3-workflow"
    / "SKILL.md"
)


@pytest.fixture
def worktree_v3core(monkeypatch):
    """Make this worktree's v3core resolvable for the duration of one test.

    Prepends ``<repo>/src/v3-core/src`` to ``sys.path`` so ``import
    v3core`` resolves to the worktree's source.  ``monkeypatch`` reverts
    the path when the test ends, so other tests in the same session are
    unaffected.
    """
    p = str(_WORKTREE_V3CORE_SRC)
    monkeypatch.syspath_prepend(p)
    # If a cached v3core was already imported (e.g. by an earlier test
    # that resolved production), invalidate the cache so this test sees
    # the worktree copy.
    for name in list(sys.modules):
        if name == "v3core" or name.startswith("v3core."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    from v3core.tools.api_add import V3_ADD_SCHEMA
    from v3core.tools.api_manage import V3_MANAGE_SCHEMA
    from v3core.tools.extract_tool import V3_EXTRACT_SCHEMA
    from v3core.tools.store import V3_STORE_SCHEMA
    from v3core.tools.write_tool import HM_WRITE_SCHEMA

    return {
        "V3_STORE_SCHEMA": V3_STORE_SCHEMA,
        "V3_ADD_SCHEMA": V3_ADD_SCHEMA,
        "HM_WRITE_SCHEMA": HM_WRITE_SCHEMA,
        "V3_EXTRACT_SCHEMA": V3_EXTRACT_SCHEMA,
        "V3_MANAGE_SCHEMA": V3_MANAGE_SCHEMA,
    }


def _read_skill() -> str:
    if not SKILL_PATH.exists():
        pytest.skip(f"v3-workflow SKILL.md not at {SKILL_PATH} (out of tree)")
    return SKILL_PATH.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────
# Skill wording
# ──────────────────────────────────────────────────────────────────────


def test_a0_skill_includes_opt_in_marker():
    text = _read_skill()
    assert "OPT-IN" in text or "opt-in" in text, (
        "v3-workflow SKILL.md must include 'OPT-IN' wording for "
        "explicit-memory write entry points"
    )


def test_a0_skill_includes_explicit_memory_phrase():
    text = _read_skill()
    assert "public.explicit_memories" in text, (
        "v3-workflow SKILL.md must reference public.explicit_memories so "
        "callers know the canonical durable store"
    )


def test_a0_skill_includes_negative_authorization_list():
    text = _read_skill()
    # Chinese negative-list phrases; require at least 3 of 6 to match.
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
        f"v3-workflow SKILL.md must list at least 3 negative-authorization "
        f"phrases; got {hits!r}"
    )


def test_a0_skill_excludes_old_shou_zhang_auto_register_framing():
    text = _read_skill()
    assert "shou_zhang' 自动注册" not in text, (
        "v3-workflow SKILL.md must NOT retain the misleading "
        "'手帐 category=shou_zhang 自动注册' persistent framing"
    )


def test_a0_skill_section_zero_one_exists():
    text = _read_skill()
    # New §0.1 explicit-memory opt-in contract subsection.
    assert re.search(r"^###\s*0\.1\s", text, re.MULTILINE), (
        "v3-workflow SKILL.md must contain a '### 0.1' subsection pinning "
        "the explicit-memory opt-in contract"
    )


def test_a0_skill_call_to_action_phrase():
    text = _read_skill()
    # The "stop and ask before writing" self-check must be present.
    assert "先问自己" in text or "不要写" in text, (
        "v3-workflow SKILL.md §0.1 must include a self-check call-to-action "
        "('first ask yourself' / 'do not write')"
    )


# ──────────────────────────────────────────────────────────────────────
# Cross-tree: re-pin schema descriptions from this side too
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "schema_key",
    ["V3_STORE_SCHEMA", "V3_ADD_SCHEMA", "HM_WRITE_SCHEMA",
     "V3_EXTRACT_SCHEMA", "V3_MANAGE_SCHEMA"],
)
def test_a0_cross_tree_schema_descriptions_present(worktree_v3core, schema_key):
    """Same opt-in wording must be present from the plugin's view.

    A silent regression where the v3-core tree loses the opt-in wording
    while the plugin tree keeps the skill wording is caught here.
    """
    schema = worktree_v3core[schema_key]
    name = schema_key.replace("_SCHEMA", "").lower().replace("hm_write", "hm_write")
    desc = schema.get("description")
    assert isinstance(desc, str) and desc, (
        f"{name} schema description must be a non-empty string"
    )
    desc_lower = desc.lower()
    opt_in_present = "opt-in" in desc_lower or "explicit-memory" in desc_lower
    assert opt_in_present, (
        f"{name} schema description must contain opt-in / explicit-memory "
        f"wording (cross-tree pin); actual: {desc[:300]}..."
    )


def test_a0_cross_tree_v3_extract_and_v3_manage_write_default_false(worktree_v3core):
    for schema_key, name in (
        ("V3_EXTRACT_SCHEMA", "v3_extract"),
        ("V3_MANAGE_SCHEMA", "v3_manage"),
    ):
        schema = worktree_v3core[schema_key]
        write = (schema.get("parameters") or {}).get("properties", {}).get(
            "write", {}
        )
        assert write.get("default") is False, (
            f"{name}.write default must be False (preview-only / non-durable)"
        )
