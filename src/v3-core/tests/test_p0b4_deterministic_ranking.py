# -*- coding: utf-8 -*-
"""P0-B4 — deterministic ranking across processes (PYTHONHASHSEED-independent).

Defect
------
Every per-lane rank map in ``recall_pool.recall_pool`` was built by sorting a
**set** of candidate ids with a single, score-only key::

    kw_rank = {sid: i + 1 for i, sid in enumerate(
        sorted(kw_ids, key=lambda s: hits[s].rrf_score, reverse=True))}

Python's ``sort`` is stable, so candidates whose primary score is *exactly*
equal keep whatever order the set happened to iterate in.  Set iteration order
for ``str`` elements depends on the per-process string hash, i.e. on
``PYTHONHASHSEED``.  A tied pair therefore receives an arbitrary rank, and the
rank feeds ``w / (K + rank)`` (RRF contribution), the exact-QA top-2 boost
slice, the post-fusion ``scored`` sort, the rerank top-k cut and the final
``limit`` — all of which are observable in the returned ranking.

Contract
--------
An identical candidate set with identical primary scores must produce an
identical ranking in every process.  This test drives the REAL
``recall_pool.recall_pool()`` fusion path — only the external data sources
(PG cursor / store / topic recaller / yin pool / snapshot seams) are stubbed —
with a minimal, frozen candidate set in which every physical lane contains at
least two candidates whose primary score is exactly equal.  It then runs the
scenario in four independent subprocesses under
``PYTHONHASHSEED=0/1/2/12345`` and compares:

* the candidate set and every candidate's wire ``kind``;
* every lane's raw rank / raw score / additive RRF contribution;
* the exact-QA-boost selection;
* every candidate's fused score;
* the complete final returned ranking (not just top-N).

Nothing about RRF weights, candidate generation, quotas, recency decay, rerank
parameters or the final limit is asserted to change — only that equal primary
scores always resolve to the same canonical order.

Process identity is asserted, never assumed: every child reports the
``__file__`` and SHA-256 of the ``recall_pool`` module it actually imported and
the parent compares that against its own copy.

The scenario is also time-independent by construction: no fixture candidate
carries a ``created_at``, so half-life decay never runs and the payload is a
pure function of the fixture.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


# Make the in-tree ``src`` importable regardless of how pytest was launched.
TESTS_DIR = Path(__file__).resolve().parent
REPO_SRC = TESTS_DIR.parents[0] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))


_CHILD_FLAG_ENV = "P0B4_DET_CHILD"
_CHILD_OUT_ENV = "P0B4_DET_OUT"

SEEDS = ("0", "1", "2", "12345")

# ── frozen fixture ────────────────────────────────────────────────────────
# Two long nonsense words: the QA row text is built from the *actual* token
# list at run time, so it always matches whatever ``build_query_tokens``
# returns and every member of a tie group scores exactly the same.
_QUERY = "determinismprobe anchorword"
# No fixture candidate carries a created_at, so half-life decay never runs and
# the payload is a pure function of the fixture (no wall-clock input at all).
_TS = ""

_Q_EMB = [0.11, 0.22, 0.33, 0.44]

# Frequency reported by the stubbed QA keyword lookup.  2 <= 3 makes the
# identical-text rows exact-QA boost eligible (so the top-2 boost slice is
# exercised) while the unmatched row is not.  It is also the count the QA
# frequency gate needs so the rare-weighting / boost-signal path stays live.
_TERM_FREQ = 2
_TOTAL_QA = 5

# Tie groups.  Every tuple below holds candidates that share one exactly equal
# primary score inside its lane.
_KW_TIED = (1001, 1002, 1003, 1004)   # identical QA text, highest kw score
_KW_LOW = (1005,)                     # no matching query term, lowest score

_QA_VEC = (2001, 2002)                                        # cosine tie
_VEC_TOPIC = ("tv1", "tv2")                                   # cosine tie
_MSG = ("msg_1", "msg_2")                                     # cosine tie
_EFF = ("eff_1", "eff_2")                                     # cosine tie
_TOPIC = ("tt1", "tt2")                                       # cosine tie
_YIN = (("v1", "s1"), ("v1", "s2"))                            # cosine tie
_NOTES = ((7, 1, "2026-01-01T00:00:00+00:00"),
          (8, 2, "2026-01-02T00:00:00+00:00"))               # cosine tie

_MSG_COSINE = 0.55
_EFF_COSINE = 0.50
_VEC_TOPIC_COSINE = 0.70
_TOPIC_COSINE = 0.66
_QA_VEC_COSINE = 0.62
_YIN_SCORE = 0.45
_NOTE_COSINE = 0.44

# The nine physical RRF rank maps a correct recall_pool must reach, as
# ``(canonical lane, physical path)`` exactly as recall_pool's own
# ``_lane_paths`` diagnostic block reports them.
EXPECTED_LANE_PATHS = (
    ("keyword", "keyword"),
    ("qa", "qa_keyword"),
    ("qa", "qa_vector"),
    ("vector", "vector"),
    ("vector", "message_vector"),
    ("vector", "effective_vector"),
    ("topic", "topic_vector"),
    ("vector", "yin_vector"),
    ("vector", "notes_vector"),
)

# Payload keys that must be bit-identical across every PYTHONHASHSEED.
_COMPARED_KEYS = (
    "candidate_ids",
    "kinds",
    "ordered_ids",
    "fusion_scores",
    "lane_evidence",
    "boosted",
    "module",
)


def _qa_rows(tokens: list[str]) -> list[tuple]:
    """Frozen QA keyword rows; the tie group's text is identical by design."""
    lowered = [str(t).lower() for t in tokens]
    text_a = " ".join(lowered + lowered)
    text_b = "zzz yyy xxx wvv unrelated filler"
    rows = [
        (rid, _TS, f"q-{rid} {text_a}", f"a-{rid} {text_a}")
        for rid in _KW_TIED
    ]
    rows += [
        (rid, _TS, f"q-{rid} {text_b}", f"a-{rid} {text_b}")
        for rid in _KW_LOW
    ]
    return rows


# ===========================================================================
# Stubs — external data sources only.  recall_pool's own ranking code is never
# replaced; every attribute patched here is restored before the assertions.
# ===========================================================================


class _StubCursor:
    """SQL-text dispatching stub cursor.  No DB, no network, no psycopg2."""

    def __init__(self, dispatch):
        self._dispatch = dispatch
        self._rows: list[tuple] = []
        self._one: tuple | None = None

    def execute(self, sql, params=None) -> None:
        _marker, rows, one = self._dispatch(str(sql or ""))
        self._rows = list(rows)
        self._one = one

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchone(self):
        # Mirrors the existing in-tree stub harness: the un-patched statements
        # return None, which recall_pool already treats as "no rows".
        if self._rows:
            return self._rows[0]
        return self._one

    def mogrify(self, sql, params=None) -> bytes:
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None

    def close(self) -> None:
        return None


class _StubPgConn:
    def __init__(self, dispatch):
        self._dispatch = dispatch

    def cursor(self):
        return _StubCursor(self._dispatch)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None


def _sql_dispatch(sql: str):
    """Return ``(marker, rows, fetchone_value)`` for one statement."""
    lowered = sql.lower()
    if "to_regclass" in lowered:
        return ("to_regclass", [], None)
    if "count(*)" in lowered and "qa_pairs" in lowered:
        # The corpus total gates the frequency-scaled quota and the exact-QA
        # boost threshold (``max(3, int(total*0.01))``).
        return ("qa-count", [], (_TOTAL_QA, 0))
    if "count(*)" in lowered and "topics" in lowered:
        return ("topics-version", [], (0, 0, "epoch"))
    if "observation_notes where embedding" in lowered:
        return (
            "obs-parent",
            [
                (oid, ver, f"note body {oid}", _NOTE_COSINE, created)
                for oid, ver, created in _NOTES
            ],
            None,
        )
    if "from qa_pairs where embedding" in lowered:
        return (
            "qa-vector",
            [
                (qid, _TS, f"v-q-{qid}", f"v-a-{qid}", _QA_VEC_COSINE)
                for qid in _QA_VEC
            ],
            None,
        )
    if "observation_embedding_chunks" in lowered:
        # No sidecar child candidates: the parent-only legacy merge runs.
        return ("obs-child", [], None)
    return ("default", [], None)


class _StubPg:
    """Minimal pool-backed store surface consumed by recall_pool."""

    def __init__(self):
        from v3core.pg_pool import PgPool

        def _factory():
            raise RuntimeError(
                "_StubPg._factory must never be called — the lease seam is stubbed"
            )

        self.pool = PgPool(_factory, max_connections=3, min_connections=0)

    def is_connected(self) -> bool:
        return True

    def lease(self, *a, **k):
        raise RuntimeError("_StubPg.lease must never be called")

    def search(self, emb, kind="", limit=0):
        if kind == "message":
            return [
                {
                    "source_id": sid,
                    "title": f"t {sid}",
                    "content_preview": f"p {sid}",
                    "content": f"c {sid}",
                    "cosine": _MSG_COSINE,
                    "created_at": "",
                }
                for sid in _MSG
            ]
        return []

    def search_effective(self, emb, limit=0):
        return [
            {
                "source_id": sid,
                "title": f"t {sid}",
                "content": f"c {sid}",
                "cosine": _EFF_COSINE,
                "created_at": "",
            }
            for sid in _EFF
        ]


class _StubTopicRecaller:
    """Stand-in for the card-vector block and the topic lane.

    recall_pool resolves the recaller twice with an identical call shape; the
    call index is what distinguishes the card-vector block (threshold kwarg)
    from the topic lane (no threshold).
    """

    def __init__(self, threshold_marker: bool):
        self._threshold_marker = threshold_marker

    def match(self, query, *args, threshold=None, top_k=0,
              query_embedding=None, deadline=None, **kwargs):
        if self._threshold_marker:
            ids, cosine = _VEC_TOPIC, _VEC_TOPIC_COSINE
        else:
            ids, cosine = _TOPIC, _TOPIC_COSINE
        return [
            (cosine, {"id": tid, "title": f"t {tid}",
                      "body": f"body {tid}", "summary": ""})
            for tid in ids
        ]


class _ModulePatcher:
    def __init__(self):
        self._saved: list[tuple[object, str, object]] = []

    def set(self, module, name, value) -> None:
        self._saved.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    def restore(self) -> None:
        for module, name, value in reversed(self._saved):
            setattr(module, name, value)
        self._saved.clear()


# ===========================================================================
# Scenario — the real recall_pool.recall_pool() fusion path
# ===========================================================================


def _build_payload() -> dict:
    import contextlib

    import v3core
    from v3core import recall_pool as rp
    from v3core import yin_pool
    from v3core.recall_v2 import (
        QueryContext,
        RecallTrace,
        build_default_query_plan,
    )
    from v3core.recall_v2.engine import LegacySink
    from v3core.tokenizer import build_query_tokens

    tokens = [t for t in build_query_tokens(_QUERY) if len(t) > 1] or [
        _QUERY.strip().lower()
    ]
    rows = _qa_rows(tokens)

    patcher = _ModulePatcher()
    try:
        @contextlib.contextmanager
        def _lease(pg, timeout=None, deadline=None):
            yield _StubPgConn(_sql_dispatch)

        patcher.set(rp, "_lease_pg_connection", _lease)
        patcher.set(rp, "_maybe_expand_query", lambda q, *a, **k: q)
        patcher.set(rp, "_fetch_facts", lambda *a, **k: {})
        patcher.set(rp, "_active_memory_reader_for", lambda *a, **k: None)
        patcher.set(rp, "_qa_snapshot_load", lambda *a, **k: None)
        patcher.set(rp, "_qa_snapshot_lookup", lambda *a, **k: ({}, []))
        patcher.set(rp, "_topic_snapshot_load", lambda *a, **k: None)
        patcher.set(rp, "_topic_snapshot_lookup", lambda *a, **k: ([], []))
        patcher.set(
            rp, "_combined_topic_rare_keyword_lookup", lambda *a, **k: []
        )

        # QA keyword lane: one frozen row list per requested term.
        # ``recall_pool`` passes ``(cursor, terms)`` positionally.
        def _combined_lookup(*a, **k):
            terms = list(k.get("terms") or (a[1] if len(a) > 1 else ()))
            freqs = {str(t).lower(): _TERM_FREQ for t in terms}
            return freqs, [list(rows) for _ in terms]

        patcher.set(rp, "_combined_qa_keyword_lookup", _combined_lookup)

        # Card-vector recaller first, topic-lane recaller second.
        _resolve_calls = {"n": 0}

        def _resolve(core, embed_cfg, pg, deadline=None):
            marker = _resolve_calls["n"] == 0
            _resolve_calls["n"] += 1
            return _StubTopicRecaller(threshold_marker=marker)

        patcher.set(rp, "_resolve_topic_recall", _resolve)

        # yin lane (recall_pool imports search_yin lazily inside the block).
        patcher.set(
            yin_pool,
            "search_yin",
            lambda emb, keyword="", pg=None, limit=0: [
                {"yin_version": ver, "section": sec,
                 "content": f"yin {sec}", "score": _YIN_SCORE}
                for ver, sec in _YIN
            ],
        )

        ctx = QueryContext(
            query_id="q-p0b4-det",
            query_text=_QUERY,
            deadline_monotonic=time.monotonic() + 60.0,
            budget_ms=60000,
            limit=25,
            max_chars=4000,
        )
        trace = RecallTrace(
            query_context=ctx,
            query_plan=build_default_query_plan(ctx),
        )

        hits, pg_fail = rp.recall_pool(
            _QUERY,
            card_index={},
            pg=_StubPg(),
            q_emb=list(_Q_EMB),
            include_keyword=True,
            include_card_vector=True,
            include_message_vector=True,
            include_effective=True,
            include_topic=True,
            include_yin=True,
            include_notes=True,
            rerank_top_n=20,
            rerank_cfg=None,
            limit=25,
            pg_was_connected=False,
            config={
                "recall": {"qa_per_term_min": 10, "qa_freq_limit": 40},
                "half_life": 30,
            },
            trace=LegacySink(trace),
        )
    finally:
        patcher.restore()

    ordered_ids = [str(h.source_id) for h in (hits or [])]
    fusion_scores = {str(h.source_id): h.rrf_score for h in (hits or [])}

    lane_evidence: dict[str, list] = {}
    boosted: list[str] = []
    kinds: dict[str, str] = {}
    for cid, snap in trace.candidate_snapshots.items():
        kinds[cid] = str(getattr(snap, "kind", "") or "")
        lane_evidence[cid] = sorted(
            [
                ev.lane,
                ev.path,
                ev.raw_rank,
                ev.raw_score,
                ev.rrf_contribution,
            ]
            for ev in snap.lane_evidence
            if ev.present
        )
        if any(
            str(getattr(getattr(evt, "event_type", None), "value", "")) == "SCORED"
            and str(getattr(evt, "note", "")) == "exact_qa_boost"
            for evt in snap.events
        ):
            boosted.append(cid)

    all_ids = sorted(set(lane_evidence) | set(ordered_ids))

    module_path = Path(rp.__file__).resolve()
    return {
        "candidate_ids": all_ids,
        "kinds": {k: kinds.get(k, "") for k in all_ids},
        "ordered_ids": ordered_ids,
        "fusion_scores": fusion_scores,
        "lane_evidence": lane_evidence,
        "boosted": sorted(boosted),
        "module": {
            "recall_pool_path": str(module_path),
            "recall_pool_sha256": hashlib.sha256(
                module_path.read_bytes()
            ).hexdigest(),
            "v3core_path": str(Path(v3core.__file__).resolve()),
        },
        "pg_fail": pg_fail,
    }


def _child_main() -> int:
    import traceback

    out = os.environ[_CHILD_OUT_ENV]
    try:
        payload = _build_payload()
    except BaseException:  # noqa: BLE001 - surface the real failure upward
        traceback.print_exc()
        return 3
    Path(out).write_text(
        json.dumps(payload, sort_keys=True, default=str), encoding="utf-8"
    )
    return 0


if os.environ.get(_CHILD_FLAG_ENV) == "1":
    raise SystemExit(_child_main())


# ===========================================================================
# Parent-side tests
# ===========================================================================


def _run_seed(seed: str, tmp_path: Path) -> dict:
    out = tmp_path / f"payload-seed-{seed}.json"
    env = dict(os.environ)
    env[_CHILD_FLAG_ENV] = "1"
    env[_CHILD_OUT_ENV] = str(out)
    env["PYTHONHASHSEED"] = seed
    env["PYTHONPATH"] = str(REPO_SRC)
    env["PYTHONNOUSERSITE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(TESTS_DIR / Path(__file__).name)],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, (
        f"seed PYTHONHASHSEED={seed} child failed rc={proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def payloads(tmp_path_factory) -> dict[str, dict]:
    tmp_path = tmp_path_factory.mktemp("p0b4-det")
    return {seed: _run_seed(seed, tmp_path) for seed in SEEDS}


def _module_identity() -> dict[str, str]:
    from v3core import recall_pool as rp

    module_path = Path(rp.__file__).resolve()
    return {
        "recall_pool_path": str(module_path),
        "recall_pool_sha256": hashlib.sha256(
            module_path.read_bytes()
        ).hexdigest(),
    }


def test_every_child_imported_this_tests_source_tree(payloads) -> None:
    """Provenance gate: same module file, same bytes, parent and children."""
    mine = _module_identity()
    for seed in SEEDS:
        got = payloads[seed]["module"]
        assert got["recall_pool_path"] == mine["recall_pool_path"], (
            f"seed {seed} imported recall_pool from {got['recall_pool_path']}, "
            f"the test process has {mine['recall_pool_path']}"
        )
        assert got["recall_pool_sha256"] == mine["recall_pool_sha256"], (
            f"seed {seed} recall_pool bytes differ from the test process copy"
        )


def test_fixture_reaches_every_rank_map_with_exactly_tied_scores(payloads) -> None:
    """Fixture validity guard — true on the pre-fix revision too.

    Without this, the cross-seed comparison below could pass vacuously
    because the fixture never reached a set-derived sort.
    """
    payload = payloads["0"]

    lanes_seen: set[tuple[str, str]] = set()
    tie_groups: dict[tuple[str, str], dict[object, list[str]]] = {}
    for cid, evidence in payload["lane_evidence"].items():
        for lane, path, _rank, raw_score, _contribution in evidence:
            key = (lane, path)
            lanes_seen.add(key)
            if raw_score is None:
                continue
            tie_groups.setdefault(key, {}).setdefault(raw_score, []).append(cid)

    missing = set(EXPECTED_LANE_PATHS) - lanes_seen
    assert not missing, (
        f"fixture never reached the physical rank maps {sorted(missing)}; "
        f"reached {sorted(lanes_seen)}"
    )

    for path in EXPECTED_LANE_PATHS:
        groups = tie_groups.get(path, {})
        biggest = max((len(ids) for ids in groups.values()), default=0)
        assert biggest >= 2, (
            f"lane path {path} has no exactly equal primary scores; "
            f"score groups = { {k: len(v) for k, v in groups.items()} }"
        )

    assert len(payload["boosted"]) == 2, (
        f"expected the exact-QA top-2 slice to fire, got {payload['boosted']!r}"
    )


def test_four_hash_seeds_produce_one_identical_ranking(payloads) -> None:
    """The regression itself: four hash seeds, one ranking."""
    base_seed = SEEDS[0]
    base = payloads[base_seed]
    for seed in SEEDS[1:]:
        other = payloads[seed]
        for key in _COMPARED_KEYS:
            assert other[key] == base[key], (
                f"PYTHONHASHSEED={seed} differs from PYTHONHASHSEED={base_seed} "
                f"on {key!r}:\n  {base_seed}={base[key]!r}\n  {seed}={other[key]!r}"
            )