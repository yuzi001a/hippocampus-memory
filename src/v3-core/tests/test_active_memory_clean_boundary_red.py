"""RED tests for v3core.active_memory_store clean boundary contract.

Anchors (must hold for GREEN):
  * Module: v3core.active_memory_store
  * derive_memory_id(category, title, content, tags) — canonical JSON of
    exactly four keys, ensure_ascii=False, sort_keys=True,
    separators=(",", ":"), UTF-8 SHA256, "mem_" + 64 hex.
  * ActiveMemoryWriter.create and ActiveMemoryReader.search_keyword /
    search_vector return dicts with memory_id, source_id, durable,
    status, warnings.
  * statuses: DURABLE_COMMITTED, DEDUPLICATED, DERIVED_WARNING, DURABLE_FAILED.
  * INSERT ... ON CONFLICT DO NOTHING + commit + fresh readback;
    embedding computed post-commit; full content persisted.

The base tree has no active_memory_store yet, so each test loads it
lazily and fails explicitly via pytest.fail when symbols are missing.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from typing import Any

import pytest


MODULE = "v3core.active_memory_store"
KEYS = {"category", "title", "content", "tags"}
STATUSES = {"DURABLE_COMMITTED", "DEDUPLICATED", "DERIVED_WARNING", "DURABLE_FAILED"}


def _load():
    try:
        return importlib.import_module(MODULE)
    except Exception as e:
        pytest.fail(f"missing module {MODULE!r}: {e!r}")


def _need(mod: Any, name: str):
    if not hasattr(mod, name):
        pytest.fail(f"{MODULE} missing symbol {name!r}")
    return getattr(mod, name)


def test_derive_memory_id_canonical_form():
    derive = _need(_load(), "derive_memory_id")
    mem = derive("ops", "thread affinity", "body", ["sqlite", "tls"])
    assert isinstance(mem, str) and mem.startswith("mem_")
    hexpart = mem[4:]
    assert len(hexpart) == 64 and all(c in "0123456789abcdef" for c in hexpart)
    payload = json.dumps(
        {"category": "ops", "title": "thread affinity",
         "content": "body", "tags": ["sqlite", "tls"]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    assert hexpart == hashlib.sha256(payload).hexdigest()
    assert set(json.loads(payload)) == KEYS


def test_derive_memory_id_tag_order_preserved_distinct_hashes_and_ascii_safe():
    derive = _need(_load(), "derive_memory_id")
    canon_xy = json.dumps(
        {"category": "c", "title": "t", "content": "中文 body", "tags": ["x", "y"]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    canon_yx = json.dumps(
        {"category": "c", "title": "t", "content": "中文 body", "tags": ["y", "x"]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    a = derive("c", "t", "中文 body", ["x", "y"])
    b = derive("c", "t", "中文 body", ["y", "x"])
    assert a == "mem_" + hashlib.sha256(canon_xy).hexdigest()
    assert b == "mem_" + hashlib.sha256(canon_yx).hexdigest()
    assert a != b, "reversed tag arrays must produce distinct canonical hashes"


def test_writer_reader_and_statuses_present():
    mod = _load()
    Writer = _need(mod, "ActiveMemoryWriter")
    Reader = _need(mod, "ActiveMemoryReader")
    assert callable(getattr(Writer, "create", None))
    assert callable(getattr(Reader, "search_keyword", None))
    assert callable(getattr(Reader, "search_vector", None))
    for name in STATUSES:
        assert hasattr(mod, name), f"missing status constant {name}"
