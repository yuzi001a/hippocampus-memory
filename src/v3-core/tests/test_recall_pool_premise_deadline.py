# -*- coding: utf-8 -*-
"""Regression test: _chain_premise_lookup forwards deadline into _lease_pg_connection."""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v3core import recall_pool
from v3core._deadline import PrefetchDeadline


def test_chain_premise_lookup_forwards_deadline(monkeypatch):
    seen: dict = {}

    class _FakeCur:
        def execute(self, sql, params):
            return None

        def fetchall(self):
            return [("t1", "Topic 1", "Summary 1")]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _FakeConn:
        def cursor(self):
            return _FakeCur()

    @contextlib.contextmanager
    def fake_lease(pg, timeout=5, deadline=None):
        seen["deadline"] = deadline
        yield _FakeConn()

    monkeypatch.setattr(recall_pool, "_lease_pg_connection", fake_lease)
    dl = PrefetchDeadline(budget_s=5.0)
    hits = recall_pool._chain_premise_lookup(object(), ["t1"], 5, deadline=dl)
    assert len(hits) == 1
    assert hits[0]["source_id"] == "t1"
    assert seen["deadline"] is dl
