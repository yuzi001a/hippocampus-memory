"""B02 source-read contract for explicit-memory ids returned by v3_search."""
from __future__ import annotations

import json

from v3core.tools.get_tool import handle_hm_get


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.cursor_obj = _Cursor(row)

    def cursor(self):
        return self.cursor_obj


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

    def get_message_context(self, source_id):
        return json.dumps({"success": False, "source_id": source_id})


def test_hm_get_reads_explicit_memory_source_id():
    source_id = "mem_b02_source_read"
    result = json.loads(
        handle_hm_get(
            {"source_id": source_id},
            core=_Core({
                "memory_id": source_id,
                "category": "system",
                "title": "B02_CANARY_FACT",
                "content": "The B02 canary fact survives a closed session.",
                "tags": ["B02_CANARY"],
                "provenance": {},
                "status": "active",
                "created_at": None,
                "updated_at": None,
            }),
        )
    )
    assert result["success"] is True
    assert result["source_id"] == source_id
    assert result["source"] == "explicit_memories"
    assert result["content"] == "The B02 canary fact survives a closed session."
    assert result["metadata"]["title"] == "B02_CANARY_FACT"
