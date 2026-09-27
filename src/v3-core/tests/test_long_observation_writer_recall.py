"""Writer + recall integration tests for the long-observation derived index (v1).

Scope: ``v3core.observer`` derived writer (short/long paths), the
``commit=False`` ledger option in ``v3core.embed_failures``, and the
parent/child union recall merge in ``v3core.recall_pool``.

No network, no PG, no production text. Embedding provider calls are faked via
``monkeypatch`` on ``v3core.embedding.embed_batch`` (the symbol the observer
imports at call time); the DB is a scripted fake. Token math uses the
deterministic ``char_estimate`` override (``tokens == ceil(len/2)``).
"""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from v3core.embed_failures import (  # noqa: E402
    record_embedding_failure,
    resolve_embedding_failure,
    resolve_embedding_failures_for_entity,
)
from v3core.embedding import (  # noqa: E402
    DURABLE_WRITE_EMBED_POLICY,
    EmbedErrorClass,
    EmbeddingCallError,
)
from v3core.observer import _backfill_note_embedding  # noqa: E402
from v3core.recall_pool import (  # noqa: E402
    _fetch_observation_child_candidates,
    _merge_observation_note_hits,
)

CFG = {"model": "test-model", "_fingerprint": "fp-test"}
OBS_VERSION = "v1-long-observation"


# ── fakes ──────────────────────────────────────────────────────────────────

class _FakeUndefinedTable(Exception):
    pgcode = "42P01"


class FakeCursor:
    """Scripted psycopg2-shaped cursor recording every statement."""

    def __init__(self, pg):
        self._pg = pg
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        for substr, exc in self._pg.fail_on:
            if substr in sql:
                raise exc
        self._pg.executed.append((sql, params))
        self.rowcount = 1

    def fetchone(self):
        sql, _ = self._pg.executed[-1]
        if "to_regclass" in sql:
            return (1,) if self._pg.sidecar_present else (None,)
        if "SELECT version" in sql:
            return (self._pg.note_version,)
        if "embedding_failures" in sql:
            return (0,)
        return (None,)

    def fetchall(self):
        sql, _ = self._pg.executed[-1]
        if "FROM observation_embedding_chunks c" in sql:
            return list(self._pg.child_rows)
        return []


class FakePG:
    """Minimal store wrapper: cursor()/commit()/rollback() like the real one."""

    def __init__(self, *, sidecar_present=True, note_version="v3"):
        self.executed: list = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_on: list = []
        self.sidecar_present = sidecar_present
        self.note_version = note_version
        self.child_rows: list = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _det_vec(text: str, dim: int = 4) -> list:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [float(b) / 255.0 + 0.01 for b in digest[:dim]]


class EmbedStub:
    """Fake embed_batch: deterministic vectors, scripted chunk failure."""

    def __init__(self, *, fail_at_call=None, fail_error=None):
        self.calls: list = []
        self.kwargs: list = []
        self.fail_at_call = fail_at_call
        self.fail_error = fail_error

    def __call__(self, texts, cfg, **kw):
        idx = len(self.calls)
        self.calls.append(list(texts))
        self.kwargs.append(kw)
        if self.fail_at_call is not None and idx == self.fail_at_call:
            raise self.fail_error
        return [_det_vec(t) for t in texts]


def _patch_embed(monkeypatch, stub: EmbedStub):
    import v3core.embedding as emb_mod

    monkeypatch.setattr(emb_mod, "embed_batch", stub)


def _marker_phases(pg: FakePG) -> list:
    out = []
    for sql, params in pg.executed:
        if "embedding_failures" in sql and isinstance(params, dict) and "phase" in params:
            out.append(params["phase"])
    return out


def _sqls(pg: FakePG, substr: str) -> list:
    return [(s, p) for s, p in pg.executed if substr in s]


# ── writer: short path ─────────────────────────────────────────────────────

def test_short_path_single_vector_no_sidecar(monkeypatch):
    """Short content keeps the one-request/full-content parent vector."""
    pg = FakePG()
    stub = EmbedStub()
    _patch_embed(monkeypatch, stub)
    content = "short observation content " * 10  # ~260 chars, far below target
    _backfill_note_embedding(pg, 11, content, cfg=dict(CFG),
                             tokenizer_override="char_estimate",
                             target_tokens=7680)
    assert len(stub.calls) == 1 and stub.calls[0] == [content]
    assert _sqls(pg, "INSERT INTO observation_embedding_chunks") == []
    updates = _sqls(pg, "UPDATE observation_notes SET embedding=")
    assert len(updates) == 1
    import json as _json

    assert updates[0][1][0] == _json.dumps(_det_vec(content))
    assert updates[0][1][1] == "fp-test"
    deletes = _sqls(pg, "DELETE FROM observation_embedding_chunks")
    assert len(deletes) == 1 and deletes[0][1] == (11, "v3")
    assert pg.commits == 1
    assert _marker_phases(pg) == []


def test_short_path_absent_sidecar_parent_only(monkeypatch):
    """Pre-migration schema: short note still gets its parent vector."""
    pg = FakePG(sidecar_present=False)
    stub = EmbedStub()
    _patch_embed(monkeypatch, stub)
    content = "short legacy-start note " * 8
    _backfill_note_embedding(pg, 12, content, cfg=dict(CFG),
                             tokenizer_override="char_estimate",
                             target_tokens=7680)
    assert len(stub.calls) == 1
    assert _sqls(pg, "DELETE FROM observation_embedding_chunks") == []
    assert len(_sqls(pg, "UPDATE observation_notes SET embedding=")) == 1
    assert pg.commits == 1


# ── writer: long path ──────────────────────────────────────────────────────

LONG_CONTENT = "ab" * 300 + "-TAIL-SECRET-DERIVED-9z"  # >300 tokens @char_estimate


def test_long_path_children_parent_one_transaction(monkeypatch):
    """Every planned child embedded; scoped replace + parent + resolve in 1 commit."""
    import json as _json

    from v3core.embed_chunks import aggregate_parent_embedding
    from v3core.observation_chunks import plan_observation_chunks

    pg = FakePG()
    stub = EmbedStub()
    _patch_embed(monkeypatch, stub)
    plan = plan_observation_chunks(LONG_CONTENT, dict(CFG),
                                   tokenizer_override="char_estimate",
                                   target_tokens=50)
    assert plan.is_long and len(plan.chunks) >= 3

    _backfill_note_embedding(pg, 11, LONG_CONTENT, cfg=dict(CFG),
                             tokenizer_override="char_estimate",
                             target_tokens=50)

    # one provider call per planned chunk, exact span texts, full-text keys
    assert len(stub.calls) == len(plan.chunks)
    for call, chunk in zip(stub.calls, plan.chunks):
        assert call == [chunk.text]
    assert all(k.get("collision_safe_key") is True for k in stub.kwargs)

    kinds = [s.split(" ", 1)[0] for s, _ in pg.executed
             if "observation_embedding_chunks" in s or "observation_notes" in s
             or "embedding_failures" in s]
    delete_at = next(i for i, (s, _) in enumerate(pg.executed)
                     if s.startswith("DELETE FROM observation_embedding_chunks"))
    first_insert_at = next(i for i, (s, _) in enumerate(pg.executed)
                           if s.startswith("INSERT INTO observation_embedding_chunks"))
    assert delete_at < first_insert_at  # stale 4→3 cleanup precedes replace

    inserts = _sqls(pg, "INSERT INTO observation_embedding_chunks")
    assert len(inserts) == len(plan.chunks)
    want_src = hashlib.sha256(LONG_CONTENT.encode()).hexdigest()
    seen_idx = []
    for (_, params), chunk in zip(inserts, plan.chunks):
        (nid, ver, idx, start, end, src_sha, span_sha,
         n_tokens, rep_ver, _vec, _fp, text) = params
        assert (nid, ver) == (11, "v3")
        assert (start, end) == (chunk.source_start, chunk.source_end)
        assert src_sha == want_src == plan.source_sha256
        assert span_sha == chunk.source_sha256 == hashlib.sha256(
            chunk.text.encode()).hexdigest()
        assert rep_ver == OBS_VERSION
        assert text == chunk.text
        seen_idx.append(idx)
    assert seen_idx == list(range(len(plan.chunks)))

    child_vecs = [_det_vec(c.text) for c in plan.chunks]
    want_parent = aggregate_parent_embedding(child_vecs)
    updates = _sqls(pg, "UPDATE observation_notes SET embedding=")
    assert len(updates) == 1
    assert updates[0][1][0] == _json.dumps(want_parent)
    assert updates[0][1][1] == "fp-test"

    resolves = [p for s, p in pg.executed
                if "embedding_failures" in s and "resolved_at = now()" in s]
    assert len(resolves) == 1
    assert resolves[0]["entity_table"] == "observation_notes"
    assert resolves[0]["entity_id"] == "11"
    assert pg.commits == 1 and pg.rollbacks == 0


def test_long_partial_child_failure_no_parent_vector(monkeypatch):
    """A failed child leaves no parent vector and a durable child marker."""
    pg = FakePG()
    stub = EmbedStub(
        fail_at_call=1,
        fail_error=EmbeddingCallError(
            "timeout", error_class=EmbedErrorClass.TIMEOUT, attempts=3,
            elapsed=0.5, policy=DURABLE_WRITE_EMBED_POLICY,
            model="test-model", fingerprint="fp-test"),
    )
    _patch_embed(monkeypatch, stub)
    _backfill_note_embedding(pg, 11, LONG_CONTENT, cfg=dict(CFG),
                             tokenizer_override="char_estimate",
                             target_tokens=50)
    assert _sqls(pg, "UPDATE observation_notes SET embedding=") == []
    assert _sqls(pg, "INSERT INTO observation_embedding_chunks") == []
    assert _sqls(pg, "DELETE FROM observation_embedding_chunks") == []
    assert _marker_phases(pg) == ["observation_long_child"]
    markers = _sqls(pg, "INSERT INTO public.embedding_failures")
    assert markers and markers[0][1]["error_class"] == "EMBEDDING_TIMEOUT"
    assert markers[0][1]["retryable"] is True
    blob = str(pg.executed)
    assert "-TAIL-SECRET-DERIVED-9z" not in blob  # no raw source in ledger
    assert "EMBEDDING_TIMEOUT" in blob


def test_long_sidecar_tx_failure_preserves_source(monkeypatch):
    """Derived-transaction failure rolls back; source-era state + marker remain."""
    pg = FakePG()
    pg.fail_on.append(("INSERT INTO observation_embedding_chunks",
                       RuntimeError("simulated sidecar failure")))
    stub = EmbedStub()
    _patch_embed(monkeypatch, stub)
    _backfill_note_embedding(pg, 11, LONG_CONTENT, cfg=dict(CFG),
                             tokenizer_override="char_estimate",
                             target_tokens=50)
    assert _sqls(pg, "UPDATE observation_notes SET embedding=") == []
    assert pg.rollbacks >= 1
    assert _marker_phases(pg) == ["observation_long_sidecar"]


def test_long_absent_sidecar_table_legacy_fallback(monkeypatch):
    """Pre-migration schema: long note keeps source durable, NULL parent, marker."""
    pg = FakePG(sidecar_present=False)
    stub = EmbedStub()
    _patch_embed(monkeypatch, stub)
    _backfill_note_embedding(pg, 11, LONG_CONTENT, cfg=dict(CFG),
                             tokenizer_override="char_estimate",
                             target_tokens=50)
    assert len(stub.calls) > 1  # children were still embedded outside the tx
    assert _sqls(pg, "INSERT INTO observation_embedding_chunks") == []
    assert _sqls(pg, "UPDATE observation_notes SET embedding=") == []
    assert _marker_phases(pg) == ["observation_long_sidecar"]


# ── ledger commit=False ────────────────────────────────────────────────────

def test_ledger_commit_false_joins_caller_transaction():
    pg = FakePG()
    err = RuntimeError("boom")
    assert record_embedding_failure(
        conn=pg, entity_table="observation_notes", entity_id="7",
        phase="observation_long_child", error=err, commit=False) is True
    assert pg.commits == 0
    assert resolve_embedding_failures_for_entity(
        conn=pg, entity_table="observation_notes", entity_id="7",
        commit=False) == 1
    assert pg.commits == 0
    assert resolve_embedding_failure(
        conn=pg, entity_table="observation_notes", entity_id="7",
        phase="observation_long_child", commit=False) is True
    assert pg.commits == 0


def test_ledger_commit_false_without_conn_records_nothing():
    assert record_embedding_failure(
        entity_table="t", entity_id="1", phase="p",
        error=RuntimeError("x"), commit=False) is False
    assert resolve_embedding_failures_for_entity(
        entity_table="t", entity_id="1", commit=False) == 0
    assert resolve_embedding_failure(
        entity_table="t", entity_id="1", phase="p", commit=False) is False


# ── recall: fetch + merge ──────────────────────────────────────────────────

def _parent_row(nid, ver, content, cos, ts):
    return (nid, ver, content, cos, ts)


def _child_row(nid, ver, content, ts, cos, sha=None, rep=OBS_VERSION):
    return (nid, ver, content, ts, cos,
            sha or hashlib.sha256(content.encode()).hexdigest(), rep)


def test_fetch_child_candidates_absent_table_returns_empty():
    pg = FakePG(sidecar_present=False)
    assert _fetch_observation_child_candidates(pg.cursor(), "[0.1]", 3) == []


def test_fetch_child_candidates_undefined_table_returns_empty():
    pg = FakePG()
    pg.fail_on.append(("observation_embedding_chunks", _FakeUndefinedTable("boom")))
    # to_regclass probe itself raising must also degrade to []
    pg2 = FakePG()
    pg2.fail_on.append(("to_regclass", _FakeUndefinedTable("boom")))
    assert _fetch_observation_child_candidates(pg2.cursor(), "[0.1]", 3) == []


def test_fetch_child_candidates_present_passes_vector_params():
    pg = FakePG()
    pg.child_rows = [_child_row(11, "v3", "content", datetime(2026, 9, 20, 1), 0.9)]
    rows = _fetch_observation_child_candidates(pg.cursor(), "[0.1,0.2]", 3)
    assert rows == pg.child_rows
    child_sqls = _sqls(pg, "FROM observation_embedding_chunks c")
    assert len(child_sqls) == 1
    assert child_sqls[0][1] == ("[0.1,0.2]", "[0.1,0.2]", 12)


def test_merge_child_hit_maps_to_full_parent_max_score():
    full = "FULL-PARENT-" + "ab" * 200
    parent = [_parent_row(11, "v3", full, 0.35, datetime(2026, 9, 20, 10))]
    child = [_child_row(11, "v3", full, datetime(2026, 9, 20, 10), 0.9)]
    hits = _merge_observation_note_hits(parent, child, limit=3)
    assert len(hits) == 1
    assert hits[0].content == full and "-TAIL" not in hits[0].content
    assert hits[0].cosine == pytest.approx(0.9)
    assert hits[0].source_id == "note:v3"


def test_merge_stale_sha_and_version_children_excluded():
    full = "FULL-PARENT-" + "cd" * 200
    parent = [_parent_row(11, "v3", full, 0.5, datetime(2026, 9, 20, 10))]
    stale = [_child_row(11, "v3", full, datetime(2026, 9, 20, 10), 0.99,
                        sha="0" * 64)]
    drifted = [_child_row(11, "v3", full, datetime(2026, 9, 20, 10), 0.99,
                          rep="v0.0-drift")]
    hits = _merge_observation_note_hits(parent, stale + drifted, limit=3)
    assert len(hits) == 1
    assert hits[0].cosine == pytest.approx(0.5)  # child lanes ignored


def test_merge_sibling_children_dedupe_to_one_parent():
    full = "FULL-PARENT-" + "ef" * 200
    parent = [_parent_row(11, "v3", full, 0.4, datetime(2026, 9, 20, 10))]
    sibs = [_child_row(11, "v3", full, datetime(2026, 9, 20, 10), 0.6 + 0.05 * i)
            for i in range(3)]
    hits = _merge_observation_note_hits(parent, sibs, limit=3)
    assert len(hits) == 1
    assert hits[0].cosine == pytest.approx(0.7)


def test_merge_newest_per_day_and_floor():
    old = "OLD-DAY——" + "gh" * 60
    new_same_day = "NEW-DAY-" + "ij" * 60
    weak = "WEAK-" + "kl" * 60
    parents = [
        _parent_row(1, "v1", old, 0.9, datetime(2026, 9, 18, 8)),
        _parent_row(2, "v2", new_same_day, 0.8, datetime(2026, 9, 20, 9)),
        _parent_row(3, "v2", new_same_day, 0.85, datetime(2026, 9, 20, 18)),
        _parent_row(4, "v4", weak, 0.29, datetime(2026, 9, 19, 8)),
    ]
    hits = _merge_observation_note_hits(parents, [], limit=3)
    assert [h.content for h in hits] == [old, new_same_day]  # newest wins day
    assert all(h.cosine >= 0.30 for h in hits)  # floor preserved


def test_merge_parent_only_parity_without_sidecar():
    full = "PARENT-ONLY-" + "mn" * 60
    parents = [_parent_row(5, "v5", full, 0.66, datetime(2026, 9, 20, 7))]
    hits = _merge_observation_note_hits(parents, [], limit=3)
    assert len(hits) == 1
    assert hits[0].content == full
    assert hits[0].content_preview == full[:500]
    assert hits[0].cosine == pytest.approx(0.66)
