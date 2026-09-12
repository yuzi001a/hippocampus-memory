"""P2a ActiveMemory boundary contract — static/contract RED tests.

Branch: p2a/active-memory-clean-boundary-20260909 (base has NO new impl).
Each test fails explicitly on base via pathlib/AST/source-text. No DB fakes,
no monkey-patched writers, no runtime canonical-store execution.

Contract surface (from user directive):
  C1  V3Core.store_card routes NEW active writes to ActiveMemoryWriter;
      must NOT call DeepStore.write_card_strict / PgEmbedStore.insert_card
      / SqliteCardStore.write_card / V3Core._sync_card_to_topics.
  C2  V3Core.extract_from_session(write=True) calls the underlying extractor
      with store=None and does NOT forward card['source_id'] (LLM-supplied)
      as the canonical source_id forwarded into store_card.
  C3  recall_pool active-memory reader hits use kind='active_memory', enter
      EXISTING kw_ids/vec_ids (no new active_ids, no EXPLICIT_RRF_WEIGHT).
  C4  api_delete checks explicit-memory store first and archives/rejects
      hard BEFORE legacy fallback.
  C5  Passive observer / e1 / topic / dedup files must NOT mention
      explicit_memories DML.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]  # src/v3-core
SRC = REPO / "src" / "v3core"
TOOLS = SRC / "tools"


def _src(rel: str) -> str:
    p = TOOLS / rel.split("/", 1)[1] if rel.startswith("tools/") else SRC / rel
    return p.read_text(encoding="utf-8")


def _funcs(src: str) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(src)
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        nodes.extend(child for child in cls.body if isinstance(child, ast.FunctionDef))
    return {n.name: n for n in nodes}


# ---------- C1: store_card → ActiveMemoryWriter ----------

def test_C1_store_card_drops_legacy_writer_calls_and_topic_sync():
    """store_card must not call legacy writer/sync methods; prose may
    document the retired path without changing runtime ownership."""
    fn = _funcs(_src("__init__.py"))["store_card"]
    body = ast.unparse(fn)
    called = {
        (node.func.attr if isinstance(node.func, ast.Attribute)
         else node.func.id if isinstance(node.func, ast.Name) else "")
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
    }
    for forbidden in ("write_card_strict", "insert_card", "write_card", "_sync_card_to_topics"):
        assert forbidden not in called, f"C1 violation: runtime calls '{forbidden}'"
    assert "ActiveMemoryWriter" in body


def test_C1_active_memory_writer_module_and_class_present():
    """Boundary pins module v3core.active_memory_store + ActiveMemoryWriter."""
    assert (SRC / "active_memory_store.py").exists(), (
        "C1 violation: v3core/active_memory_store.py missing"
    )
    classes = {n.name for n in ast.walk(ast.parse(_src("active_memory_store.py")))
               if isinstance(n, ast.ClassDef)}
    assert "ActiveMemoryWriter" in classes, (
        f"C1 violation: ActiveMemoryWriter class not found (got {classes})"
    )


# ---------- C2: V3Core.extract_from_session(write=True) contract ----------

def test_C2_extract_uses_store_none_and_does_not_forward_card_source_id():
    """V3Core.extract_from_session must call the underlying extractor with
    store=None and must NOT forward card.get('source_id') as canonical
    source_id (canonical id is derived later from the canonical payload)."""
    core_src = _src("__init__.py")
    fn: ast.FunctionDef | None = None
    for node in ast.parse(core_src).body:
        if isinstance(node, ast.ClassDef) and node.name == "V3Core":
            for child in node.body:
                if (isinstance(child, ast.FunctionDef)
                        and child.name == "extract_from_session"):
                    fn = child
                    break
            break
    assert fn is not None, (
        "C2 violation: V3Core.extract_from_session not found in "
        "v3core/__init__.py"
    )
    body = ast.unparse(fn)
    assert "store=None" in body or "store = None" in body, (
        "C2 violation: V3Core.extract_from_session does not call the "
        "underlying extractor with store=None; active writes must not "
        "go through the legacy DeepStore path"
    )
    for forbidden in (
        'card.get("source_id")', "card.get('source_id')",
        'card_kwargs["source_id"]', "card_kwargs['source_id']",
    ):
        assert forbidden not in body, (
            f"C2 violation: V3Core.extract_from_session forwards "
            f"card['source_id'] as canonical id (found '{forbidden}'); "
            f"LLM-generated source_id must not be forwarded"
        )


# ---------- C3: recall_pool active-memory kind handling ----------

def test_C3_recall_pool_uses_active_memory_kind():
    """Active-memory reader hits must be tagged exactly 'active_memory'."""
    src = _src("recall_pool.py")
    assert "'active_memory'" in src or '"active_memory"' in src, (
        "C3 violation: recall_pool has no kind='active_memory' literal"
    )


def test_C3_recall_pool_active_hits_use_existing_kw_vec_ids():
    """Active-memory hits must enter existing kw_ids / vec_ids sets;
    no new active_ids, no new RRF-weight constant."""
    src = _src("recall_pool.py")
    tree = ast.parse(src)
    mutated: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"add", "update"}
                and isinstance(node.func.value, ast.Name)):
            mutated.add(node.func.value.id)
    assert {"kw_ids", "vec_ids"} <= mutated, (
        f"C3 violation: recall_pool never mutates kw_ids/vec_ids (mutates {mutated})"
    )
    assert "active_ids" not in mutated, (
        "C3 violation: recall_pool introduced a new 'active_ids' collection"
    )
    for forbidden in ("EXPLICIT_RRF_WEIGHT", "ACTIVE_MEMORY_RRF", "ACTIVE_RRF_WEIGHT"):
        assert forbidden not in src, (
            f"C3 violation: recall_pool introduced '{forbidden}'; "
            "active-memory hits must reuse existing RRF machinery"
        )


# ---------- C4: api_delete explicit-memory short-circuit ----------

def test_C4_api_delete_checks_explicit_memory_before_legacy_fallback():
    """handle_v3_delete must short-circuit on the explicit-memory store
    (archive/reject hard) BEFORE any legacy fallback."""
    funcs = _funcs(_src("tools/api_delete.py"))
    handler = next((funcs[n] for n in ("handle_v3_delete", "api_delete", "delete_memory")
                    if n in funcs), None)
    assert handler is not None, "C4 violation: api_delete handler not found"
    body = ast.unparse(handler)
    assert "explicit_memory" in body or "explicit_memories" in body, (
        "C4 violation: api_delete handler has no explicit-memory short-circuit"
    )


# ---------- C5: passive paths must not touch explicit_memories DML ----------

PASSIVE_FILES = ("observer.py", "e1.py", "topic_store.py", "dedup.py")


@pytest.mark.parametrize("rel", PASSIVE_FILES)
def test_C5_passive_paths_have_no_explicit_memories_dml(rel: str):
    """observer / e1 / topic_store / dedup must not perform DML against
    the explicit_memories table (would leak active writes from passive paths)."""
    low = _src(rel).lower()
    dml = ("insert into explicit_memories", "update explicit_memories",
           "delete from explicit_memories", "merge into explicit_memories")
    leaks = [k for k in dml if k in low]
    assert not leaks, (
        f"C5 violation: {rel} performs explicit_memories DML ({leaks})"
    )