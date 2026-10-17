# -*- coding: utf-8 -*-
"""P0-B3 RED — QA-keyword candidate *superset* admission (head cap C + tail quota T).

Contract pinned by this module (fails on the pre-P0-B3 tree by design; it is a
strict TDD RED module and MUST NOT be weakened to make it pass):

  For every QA-keyword candidate surface the per-term admission set is

      admitted(term) = head(term) + tail(term)

  where ``head`` is the *existing* per-term head cap C (unchanged, first C
  eligible rows by ``id`` ASC — the very rows today's code returns), and
  ``tail`` is at most ``T`` eligible rows immediately after the head, still in
  ``id`` ASC order.  The result is the head plus that bounded next-rank slice.
  Therefore:

    * head candidates are NEVER evicted or reordered (``admitted[:C] == head``);
    * ``admitted`` is a strict superset of the pre-P0-B3 result;
    * ``T = 0`` is byte-identical to the pre-P0-B3 result (backward compatible);
    * the tail is bounded (``len(admitted) <= C + T``) and duplicate-free;
    * the tail obeys the SAME eligibility as the head (``answer IS NOT NULL``
      and ``session_id NOT LIKE '%.trajectory%'``).

  The bounded tail quota ``T`` is threaded as the keyword argument
  ``tail_quota`` (keyword-only, like the neighbouring ``rare_limit`` /
  ``per_term_limit`` / ``freq_limit``) through:

    * ``recall_pool._qa_snapshot_lookup``            (Python snapshot helper)
    * ``recall_pool._combined_qa_keyword_lookup``    (single SQL round-trip)
    * ``recall_pool._qa_legacy_keyword_lookup``      (legacy fallback SQL path)
    * ``recall_pool._qa_keyword_cache_lookup``       (cache read/fill, forwards T)
    * ``recall_pool._qa_prefill_parallel_cache``     (cache params tuple)

  The quota is configured as ``prefetch.qa_tail_quota`` (default 3; explicit 0
  disables it).  The cache identity tuple built at the prefill seam must grow
  from ``(rare_limit, per_term_limit, freq_limit)`` to a 4-tuple carrying T,
  so changing T invalidates cached head-only rows.  Negative/malformed values
  use the safe default rather than being coerced through truthiness.

  No target-ID special cases, no ASC -> DESC replacement, no deletion of older
  candidates.  All ids below are synthetic.

Gated PG integration (Y400 only, opt-in):
  ``test_y400_combined_sql_tail_admission_red`` runs only when
  ``HIPPOCAMPUS_P0B3_TEST_DSN`` is set to the *isolated Y400* PostgreSQL
  (loopback host, port 55433).  It refuses production (5433) and every
  non-55433 port, and writes NO persistent rows: it shadows ``qa_pairs`` with a
  session-local ``TEMP TABLE`` (``SET search_path = pg_temp, public``) which is
  dropped when the connection closes.  When the env var is absent the test
  skips, so the rest of the module still runs.

RED execution remains with the parent (Y400, PG 55433).  This module is NOT run
on the authoring host.
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make the in-tree ``src`` importable regardless of how pytest was launched.
REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

from v3core import recall_pool  # noqa: E402


_TS = "2026-09-14T10:00:00+00:00"


def _row(rid, question, answer, session="sess"):
    """Build one snapshot row in the exact tuple layout ``_qa_snapshot_load`` emits.

    Layout: ``(id, timestamp, question, answer_raw, session_id, q_low, a_low)``.
    ``answer_raw`` preserves ``None`` so the ``answer IS NOT NULL`` eligibility
    check can be exercised.
    """
    return (
        rid,
        _TS,
        question,
        answer,
        session,
        question.lower(),
        (answer or "").lower(),
    )


def _alpha_snapshot():
    """One term ('alpha') over a controlled eligible/ineligible population.

    Matches (freq = 22): ids 1..22.
    Eligible (answer NOT NULL, session not *.trajectory*): ids 1..20.
    Ineligible: id 21 (answer IS NULL), id 22 (session ``sess.trajectory``).
    Non-matching: id 23.
    """
    rows = [_row(i, f"alpha fact {i}", f"answer {i}") for i in range(1, 21)]
    rows.append(_row(21, "alpha no answer", None))
    rows.append(_row(22, "alpha trajectory", "answer 22", session="sess.trajectory"))
    rows.append(_row(23, "beta only", "answer 23"))
    return {"rows": rows}


# With rare_limit=4, per_term_limit=3, freq_limit=5 and freq=22 (> 3):
#   C = max(per_term_limit, min(freq, freq_limit)) = max(3, min(22, 5)) = 5
#   head = ids 1..5, high-rank tail(T=3) = ids 18..20
_RARE_LIMIT = 4
_PER_TERM_LIMIT = 3
_FREQ_LIMIT = 5
_HEAD = [1, 2, 3, 4, 5]
_TAIL3 = [6, 7, 8]


def _lookup_alpha(tail_quota):
    """Call the snapshot helper for 'alpha' and return ``(freqs, ids)``."""
    freqs, rows_by_term = recall_pool._qa_snapshot_lookup(
        _alpha_snapshot(),
        ["alpha"],
        rare_limit=_RARE_LIMIT,
        per_term_limit=_PER_TERM_LIMIT,
        freq_limit=_FREQ_LIMIT,
        tail_quota=tail_quota,
    )
    return freqs, [row[0] for row in rows_by_term[0]]


class TestSnapshotHeadTailAdmission:
    """``_qa_snapshot_lookup`` must keep head cap C and add a bounded tail T."""

    def test_tail_zero_is_byte_identical_to_existing_head_only_admission(self):
        """T=0 must reproduce today's result exactly (head cap C, id ASC)."""
        freqs, ids = _lookup_alpha(0)
        assert ids == _HEAD
        assert freqs["alpha"] == 22

    def test_tail_quota_admits_bounded_tail_as_superset_of_head(self):
        freqs0, ids0 = _lookup_alpha(0)
        freqs3, ids3 = _lookup_alpha(3)

        # head is a preserved prefix — no head candidate is evicted or reordered
        assert ids3[: len(ids0)] == ids0
        # superset relation
        assert set(ids3) >= set(ids0)
        assert len(ids3) > len(ids0)
        # head + bounded tail, still id ASC
        assert ids3 == _HEAD + _TAIL3
        # frequency is a property of the corpus, not of the admission quota
        assert freqs3 == freqs0

    def test_tail_quota_never_evicts_head_for_any_t(self):
        _, ids0 = _lookup_alpha(0)
        for t in (1, 3, 100):
            _, ids = _lookup_alpha(t)
            assert ids[: len(ids0)] == ids0, f"head evicted at T={t}: {ids}"
            assert len(ids) <= len(ids0) + t, f"tail not bounded at T={t}: {ids}"
            assert len(ids) == len(set(ids)), f"duplicate rows at T={t}: {ids}"
        # T larger than the remaining eligible pool is clamped to that pool.
        _, ids100 = _lookup_alpha(100)
        assert ids100 == list(range(1, 21))

    def test_mid_ranks_41_through_80_survive_when_frequency_is_far_above_cap(self):
        """A superset tail must not evict the middle of the old head."""
        rows = [_row(i, f"alpha fact {i}", f"answer {i}") for i in range(1, 101)]
        freqs, rows_by_term = recall_pool._qa_snapshot_lookup(
            {"rows": rows}, ["alpha"],
            rare_limit=80, per_term_limit=80, freq_limit=80, tail_quota=3,
        )
        ids = [row[0] for row in rows_by_term[0]]

        assert freqs["alpha"] == 100
        assert ids == list(range(1, 84))
        assert ids[40:80] == list(range(41, 81)), "ranks 41-80 must remain admitted"
        assert ids[80:] == [81, 82, 83], "T must append only the next three ranks"

        # Production-shaped caps: injection uses C=64; the prefetch/rerank
        # window uses C=40. In either case T=3 is exactly C+1..C+3.
        for rare_limit, per_term_limit, freq_limit, head_cap in (
            (24, 10, 64, 64),
            (30, 30, 40, 40),
        ):
            freqs, by_term = recall_pool._qa_snapshot_lookup(
                {"rows": rows}, ["alpha"],
                rare_limit=rare_limit,
                per_term_limit=per_term_limit,
                freq_limit=freq_limit,
                tail_quota=3,
            )
            admitted = [row[0] for row in by_term[0]]
            assert freqs["alpha"] == 100
            assert admitted == list(range(1, head_cap + 4))
            assert admitted[head_cap:] == [head_cap + 1, head_cap + 2, head_cap + 3]

    def test_tail_respects_the_same_eligibility_as_the_head(self):
        _, ids3 = _lookup_alpha(3)
        assert 21 not in ids3, "tail admitted an answer IS NULL row"
        assert 22 not in ids3, "tail admitted a .trajectory row"
        assert 23 not in ids3, "tail admitted a non-matching row"

    def test_non_positive_tail_quota_is_bounded_to_no_tail(self):
        for t in (0, -1, -5):
            _, ids = _lookup_alpha(t)
            assert ids == _HEAD, f"T={t} produced tail rows: {ids}"

    def test_rare_term_keeps_rare_cap_and_still_gets_a_bounded_tail(self):
        # freq = 3 (<= 3) -> rare branch: C = max(rare_limit, 1).
        # With rare_limit=1: head = [21], tail(T=3) = [22, 23].
        rows = [_row(i, f"gamma {i}", f"g{i}") for i in (21, 22, 23)]
        snapshot = {"rows": rows}

        _, rows0 = recall_pool._qa_snapshot_lookup(
            snapshot, ["gamma"],
            rare_limit=1, per_term_limit=3, freq_limit=5, tail_quota=0,
        )
        _, rows3 = recall_pool._qa_snapshot_lookup(
            snapshot, ["gamma"],
            rare_limit=1, per_term_limit=3, freq_limit=5, tail_quota=3,
        )
        ids0 = [r[0] for r in rows0[0]]
        ids3 = [r[0] for r in rows3[0]]

        assert ids0 == [21], "rare-term head cap changed"
        assert ids3[: len(ids0)] == ids0
        assert ids3 == [21, 22, 23]


class TestTailQuotaConfig:
    """The quota is configurable through both raw and typed prefetch configs."""

    def test_zero_disables_and_invalid_values_fall_back_without_truthiness_coercion(self):
        from v3core.config_model import PrefetchConfig, from_legacy_dict

        cases = [
            ({}, 3),
            ({"prefetch": {"qa_tail_quota": 0}}, 0),
            ({"prefetch": {"qa_tail_quota": "4"}}, 4),
            ({"prefetch": {"qa_tail_quota": -1}}, 3),
            ({"prefetch": {"qa_tail_quota": "invalid"}}, 3),
            ({"prefetch": {"qa_tail_quota": True}}, 3),
            ({"prefetch": {"qa_tail_quota": False}}, 3),
        ]
        for raw, expected in cases:
            cfg = from_legacy_dict(raw)
            model_value = raw.get("prefetch", {}).get("qa_tail_quota", 3)
            model_expected = (
                3
                if model_value == "invalid" or isinstance(model_value, bool)
                else int(model_value)
            )
            assert cfg.prefetch.qa_tail_quota == model_expected
            assert recall_pool._resolve_qa_tail_quota(raw) == expected
            assert recall_pool._resolve_qa_tail_quota(cfg) == expected
            assert recall_pool._resolve_qa_tail_quota(cfg.prefetch) == expected

        raw_inf = {"prefetch": {"qa_tail_quota": float("inf")}}
        assert recall_pool._resolve_qa_tail_quota(raw_inf) == 3
        assert from_legacy_dict(raw_inf).prefetch.qa_tail_quota == 3
        assert recall_pool._resolve_qa_tail_quota(PrefetchConfig(qa_tail_quota=True)) == 3
        assert recall_pool._clamp_qa_tail_quota(float("inf")) == 0


class TestQaKeywordCacheCarriesTailQuota:
    """T must reach the cache read/fill and the cache identity tuple."""

    def _cache(self):
        return {
            "version": (10, 100),
            "params": (_RARE_LIMIT, _PER_TERM_LIMIT, _FREQ_LIMIT, 3),
            "terms": {},
            "lock": threading.RLock(),
        }

    def test_cache_lookup_forwards_tail_quota_to_combined_sql(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict = {}

        def _fake_combined(cursor, terms, *args, **kwargs):
            seen["tail_quota"] = kwargs.get("tail_quota")
            seen["terms"] = list(terms)
            return {str(t).lower(): 0 for t in terms}, [[] for _ in terms]

        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup", _fake_combined,
        )
        recall_pool._qa_keyword_cache_lookup(
            None, ["alpha"], self._cache(),
            pg=None,
            rare_limit=_RARE_LIMIT,
            per_term_limit=_PER_TERM_LIMIT,
            freq_limit=_FREQ_LIMIT,
            tail_quota=3,
        )
        assert seen["tail_quota"] == 3, (
            "cache read/fill did not forward the tail quota to the SQL lookup"
        )

    def test_cache_lookup_forwards_tail_quota_to_parallel_sql(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict = {}

        def _fake_parallel(pg, terms, *args, **kwargs):
            seen["tail_quota"] = kwargs.get("tail_quota")
            return {str(t).lower(): 0 for t in terms}, [[] for _ in terms]

        monkeypatch.setattr(
            recall_pool, "_combined_qa_keyword_lookup_parallel", _fake_parallel,
        )
        recall_pool._qa_keyword_cache_lookup(
            None, ["alpha"], self._cache(),
            pg=object(), parallel=True, parallel_workers=2,
            rare_limit=_RARE_LIMIT,
            per_term_limit=_PER_TERM_LIMIT,
            freq_limit=_FREQ_LIMIT,
            tail_quota=3,
        )
        assert seen["tail_quota"] == 3, (
            "cache read/fill did not forward the tail quota to the parallel SQL lookup"
        )

    def test_prefill_cache_params_include_tail_quota_and_invalidate_on_change(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The prefill cache identity tuple must carry T, and T-sensitivity must hold."""
        pool = SimpleNamespace(_max_connections=4)
        recorded: dict = {}
        real_cache_for_pool = recall_pool._qa_keyword_cache_for_pool

        def _recording_cache_for_pool(pool_arg, version, params):
            recorded["params"] = params
            return real_cache_for_pool(pool_arg, version, params)

        class _Cur:
            def execute(self, *a, **k):
                return None

            def fetchone(self):
                return (10, 100)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Conn:
            def cursor(self):
                return _Cur()

        @contextlib.contextmanager
        def _lease(pg, timeout=None, deadline=None):
            yield _Conn()

        monkeypatch.setattr(
            recall_pool, "_qa_keyword_cache_for_pool", _recording_cache_for_pool,
        )
        monkeypatch.setattr(
            recall_pool, "_qa_keyword_cache_lookup", lambda *a, **k: ({}, []),
        )
        monkeypatch.setattr(recall_pool, "_lease_pg_connection", _lease)

        terms = [f"t{i}" for i in range(8)]
        ok = recall_pool._qa_prefill_parallel_cache(
            None, pool, terms,
            rerank_top_n=30, limit=5,
            config={"prefetch": {"qa_tail_quota": 3}},
        )
        assert ok is True, "prefill seam did not reach the cache-params build"

        params = recorded.get("params")
        assert isinstance(params, tuple), params
        assert len(params) == 4, (
            "prefill cache identity tuple omits the tail quota: "
            f"expected a 4-tuple, got {params!r}"
        )
        assert isinstance(params[3], int) and params[3] >= 0, (
            f"tail quota element must be a non-negative int, got {params[3]!r}"
        )
        assert params[3] == 3, f"prefill ignored prefetch.qa_tail_quota: {params!r}"

        # A change in T (all other cache params equal) must invalidate the cache
        # so a stale head-only admission set is never served for a tail-enabled call.
        base = (params[0], params[1], params[2])
        c0 = real_cache_for_pool(pool, (10, 100), base + (0,))
        c0["terms"]["sentinel"] = (1, [])
        c3 = real_cache_for_pool(pool, (10, 100), base + (3,))
        assert "sentinel" not in c3["terms"], (
            "cache served a stale admission set across a tail-quota change"
        )


# ---------------------------------------------------------------------------
# Gated Y400-only PG integration (opt-in, no persistent rows).
# ---------------------------------------------------------------------------

_P0B3_DSN_ENV = "HIPPOCAMPUS_P0B3_TEST_DSN"
_P0B3_DSN = os.environ.get(_P0B3_DSN_ENV)
_P0B3_PORT = "55433"  # isolated Y400 test host
_P0B3_DBNAME = "p0b3_synthetic_20261006"
_P0B3_USER = "ranger"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


def _parse_dsn(dsn: str) -> dict:
    try:
        from psycopg2.extensions import parse_dsn as _parse
        parsed = _parse(dsn)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if v is not None}
    except Exception:
        pass
    out: dict = {}
    for token in dsn.split():
        if "=" not in token:
            continue
        k, _, v = token.partition("=")
        out[k.strip()] = v.strip()
    return out


def _dsn_is_y400_isolated(dsn) -> tuple:
    """Return ``(ok, reason)``.  Refuses production and non-Y400 targets."""
    if not dsn:
        return False, f"{_P0B3_DSN_ENV} not set"
    parsed = _parse_dsn(dsn)
    port = parsed.get("port", "")
    host = parsed.get("host", "")
    if port != _P0B3_PORT:
        return False, (
            f"port={port!r} is not the isolated Y400 port {_P0B3_PORT} "
            f"(production 5433 is never allowed)"
        )
    if host not in _LOOPBACK_HOSTS:
        return False, f"host={host!r} is not an explicit loopback address"
    dbname = parsed.get("dbname", "")
    if dbname != _P0B3_DBNAME:
        return False, f"dbname={dbname!r} is not the isolated P0-B3 test database"
    return True, ""


_P0B3_OK, _P0B3_REASON = _dsn_is_y400_isolated(_P0B3_DSN)


def _resolve_real_connect():
    """Resolve the real psycopg2 factory past the conftest P0-A connect guard."""
    try:
        from psycopg2._psycopg import connect as _real  # type: ignore
        return _real
    except Exception:
        pass
    try:
        from psycopg2._psycopg import _connect as _real  # type: ignore
        return _real
    except Exception:
        pass
    import psycopg2 as _psycopg2
    return _psycopg2.connect


class TestY400CombinedSqlTailAdmission:
    """Real-SQL proof of head + bounded tail, on the isolated Y400 PG only."""

    @pytest.mark.skipif(
        not _P0B3_OK,
        reason=f"P0-B3 Y400-only PG integration gated: {_P0B3_REASON}",
    )
    def test_y400_combined_sql_tail_admission_red(self) -> None:
        # Re-check at run time (defence in depth): never touch production.
        ok, reason = _dsn_is_y400_isolated(os.environ.get(_P0B3_DSN_ENV))
        if not ok:
            pytest.skip(reason)

        connect = _resolve_real_connect()
        conn = connect(_P0B3_DSN)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("SELECT current_database(), current_user, inet_server_port()")
            identity = cur.fetchone()
            expected_identity = (_P0B3_DBNAME, _P0B3_USER, int(_P0B3_PORT))
            assert identity == expected_identity, (
                f"refusing unexpected PG identity: {identity!r}"
            )
            # Shadow ``qa_pairs`` with a session-local TEMP table.  Nothing is
            # written to persistent storage; the temp table vanishes on close.
            cur.execute("SET search_path = pg_temp, public")
            cur.execute(
                "CREATE TEMP TABLE qa_pairs ("
                " id integer PRIMARY KEY, timestamp timestamptz,"
                " question text, answer text, session_id text)"
                " ON COMMIT PRESERVE ROWS"
            )
            rows = [
                (i, _TS, f"alpha fact {i}", f"answer {i}", "sess")
                for i in range(1, 21)
            ]
            rows.append((21, _TS, "alpha no answer", None, "sess"))
            rows.append((22, _TS, "alpha trajectory", "answer 22", "sess.trajectory"))
            rows.append((23, _TS, "beta only", "answer 23", "sess"))
            cur.executemany(
                "INSERT INTO qa_pairs (id, timestamp, question, answer, session_id)"
                " VALUES (%s, %s::timestamptz, %s, %s, %s)",
                rows,
            )

            freqs0, rows0 = recall_pool._combined_qa_keyword_lookup(
                cur, ["alpha"],
                rare_limit=_RARE_LIMIT,
                per_term_limit=_PER_TERM_LIMIT,
                freq_limit=_FREQ_LIMIT,
                tail_quota=0,
            )
            freqs3, rows3 = recall_pool._combined_qa_keyword_lookup(
                cur, ["alpha"],
                rare_limit=_RARE_LIMIT,
                per_term_limit=_PER_TERM_LIMIT,
                freq_limit=_FREQ_LIMIT,
                tail_quota=3,
            )

            ids0 = [r[0] for r in rows0[0]]
            ids3 = [r[0] for r in rows3[0]]

            # freq counts every matching row (question OR answer), regardless
            # of eligibility: ids 1..22.
            assert freqs0["alpha"] == 22, freqs0
            assert freqs3["alpha"] == 22, freqs3

            assert ids0 == _HEAD, f"T=0 SQL admission changed: {ids0}"
            assert ids3[: len(ids0)] == ids0, f"SQL head evicted: {ids3}"
            assert ids3 == _HEAD + _TAIL3, f"SQL head+tail wrong: {ids3}"
            assert len(ids3) == len(set(ids3))
            assert 21 not in ids3 and 22 not in ids3 and 23 not in ids3

            legacy_rows = recall_pool._qa_legacy_keyword_lookup(
                cur, "alpha", limit=len(_HEAD), tail_quota=3,
            )
            legacy_ids = [r[0] for r in legacy_rows]
            assert legacy_ids == _HEAD + _TAIL3, (
                f"legacy fallback must return the head plus next T ranks: {legacy_ids}"
            )

            # Match the inspected production head sizes, retaining T=3 only
            # after each unchanged head: injection C=64; prefetch C=40.
            cur.execute("TRUNCATE qa_pairs")
            prod_rows = [
                (i, _TS, f"alpha fact {i}", f"answer {i}", "sess")
                for i in range(1, 101)
            ]
            cur.executemany(
                "INSERT INTO qa_pairs (id, timestamp, question, answer, session_id)"
                " VALUES (%s, %s::timestamptz, %s, %s, %s)",
                prod_rows,
            )
            for rare, per_term, freq_limit, head in (
                (24, 16, 64, 64),
                (30, 30, 40, 40),
            ):
                freq0, rows0 = recall_pool._combined_qa_keyword_lookup(
                    cur, ["alpha"], rare_limit=rare,
                    per_term_limit=per_term, freq_limit=freq_limit,
                    tail_quota=0,
                )
                freq3, rows3 = recall_pool._combined_qa_keyword_lookup(
                    cur, ["alpha"], rare_limit=rare,
                    per_term_limit=per_term, freq_limit=freq_limit,
                    tail_quota=3,
                )
                ids0 = [row[0] for row in rows0[0]]
                ids3 = [row[0] for row in rows3[0]]
                assert freq0["alpha"] == freq3["alpha"] == 100
                assert ids0 == list(range(1, head + 1))
                assert ids3 == list(range(1, head + 4))
                assert ids3[:head] == ids0, "production-shaped T must preserve the head"
        finally:
            try:
                conn.close()
            except Exception:
                pass
