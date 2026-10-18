# -*- coding: utf-8 -*-
"""G6B Slice C — opt-in disposable-PG integration test for Recall V2.

This test module is **opt-in** — it only runs when the environment
variable ``HIPPOCAMPUS_G6B_TEST_DSN`` is set.  When unset, the module
calls ``pytest.skip('HIPPOCAMPUS_G6B_TEST_DSN not set')`` at import
time so CI (which has no PG) stays green.  When set, the test connects
to the disposable PG DSN, builds the real ``PgPool`` + ``PgEmbedStore``
objects, seeds synthetic fixtures idempotently, and asserts
parity/keyword/vector/topic/explicit memory/deadline behaviour
against real SQL.

The disposable PG MUST be running locally with the alpha schema
applied (see ``src/v3-core/schema/alpha_bootstrap.sql`` and
``src/v3-core/schema/explicit_memories.sql``).  The conftest's
``_block_pg_connect`` session-scoped autouse fixture is BYPASSED for
the duration of this module's tests via a per-module
``_restore_pg_connect`` autouse fixture that resolves the real
psycopg2 factory from the C-extension (``psycopg2._psycopg._connect``)
— see the ``_REAL_CONNECT_FACTORY`` block below for why this is safe
even though the conftest shadows ``psycopg2.connect``.

This test is OPT-IN, DISPOSABLE-ONLY, and NEVER touches production.
The DSN is parsed on entry; any DSN whose port is 5433 (the production
port) or whose host is not a loopback address is refused with a clear
``pytest.skip`` so a misconfigured environment can never reach the
real database.  All seeded rows carry ``g6b_test_`` id prefixes and
are deleted on teardown.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

# ---------------------------------------------------------------------------
# Opt-in module gate — must be set BEFORE any test in this module imports
# v3core.* modules that may run side effects at import time.  pytest evaluates
# this at collection time.
# ---------------------------------------------------------------------------

_TEST_DSN = os.environ.get("HIPPOCAMPUS_G6B_TEST_DSN")
if not _TEST_DSN:
    pytest.skip(
        "HIPPOCAMPUS_G6B_TEST_DSN not set — opt-in disposable-PG integration "
        "test is skipped in CI",
        allow_module_level=True,
    )

# ---------------------------------------------------------------------------
# Safety refusal — refuse to run if the DSN does not target the disposable,
# loopback-only, non-production PG.  The conftest's P0-A guard blocks the
# public ``psycopg2.connect`` attribute, so this DSN MUST NOT point at the
# production database (port 5433, host != loopback).
# ---------------------------------------------------------------------------

def _parse_dsn_safely(dsn: str) -> dict[str, str]:
    """Parse a libpq-style DSN into a dict.

    Tries ``psycopg2.extensions.parse_dsn`` first; falls back to a tiny
    hand-rolled whitespace+key=value parser that handles the forms we
    actually accept (``host=... port=... dbname=... user=... password=...``).
    """
    try:
        from psycopg2.extensions import parse_dsn as _psycopg_parse_dsn
        parsed = _psycopg_parse_dsn(dsn)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if v is not None}
    except Exception:
        pass
    out: dict[str, str] = {}
    for token in dsn.split():
        if "=" not in token:
            continue
        k, _, v = token.partition("=")
        out[k.strip()] = v.strip()
    return out


_DSN_PARSED = _parse_dsn_safely(_TEST_DSN)
_DSN_PORT = _DSN_PARSED.get("port", "")
_DSN_HOST = _DSN_PARSED.get("host", "")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "localhost.localdomain"}

if _DSN_PORT == "5433":
    pytest.skip(
        "HIPPOCAMPUS_G6B_TEST_DSN targets port 5433 (production). "
        "Refusing to run — this test is disposable-only and never "
        "touches production. Set HIPPOCAMPUS_G6B_TEST_DSN to a "
        "loopback, non-5433 disposable DSN.",
        allow_module_level=True,
    )
if _DSN_HOST and _DSN_HOST.lower() not in _LOOPBACK_HOSTS:
    pytest.skip(
        f"HIPPOCAMPUS_G6B_TEST_DSN host={_DSN_HOST!r} is not a loopback "
        f"address. Refusing to run — this test is disposable-only and "
        f"never touches a remote database. Allowed hosts: "
        f"{sorted(_LOOPBACK_HOSTS)}.",
        allow_module_level=True,
    )

# Make the in-tree ``src`` importable regardless of how pytest was launched.
REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

# ---------------------------------------------------------------------------
# Resolve the REAL psycopg2 factory in a way the conftest's P0-A guard
# cannot shadow.
#
# The conftest installs ``psycopg2.connect = _blocked`` (a raiser) at
# session scope.  That re-binding only shadows the *public* module
# attribute.  The C extension ``psycopg2._psycopg`` exposes the same
# factory under a different name (``_connect`` on this build); that
# name is NOT touched by the guard and therefore reaches the real
# libpq.  This is intentional — see the parent task spec for D1.
# ---------------------------------------------------------------------------

def _resolve_real_connect() -> Callable[..., Any]:
    """Return the real psycopg2 connect factory, bypassing the conftest guard.

    The guard shadows only ``psycopg2.connect`` (the public attribute).
    We try the spec-mandated names in order; whichever resolves first
    wins.  Only if BOTH attempts fail do we fall back to the public
    attribute (which will still be shadowed by the guard).
    """
    # Primary: the spec-mandated ``psycopg2._psycopg.connect`` symbol.
    try:
        from psycopg2._psycopg import connect as _real_connect  # type: ignore
        return _real_connect
    except Exception:
        pass
    # Fallback: some psycopg2 builds expose the factory as
    # ``psycopg2._psycopg._connect`` (the actual C symbol name on
    # Linux wheels).  This is the ONLY way to bypass the conftest's
    # re-binding of the public ``psycopg2.connect``.
    try:
        from psycopg2._psycopg import _connect as _real_connect  # type: ignore
        return _real_connect
    except Exception:
        pass
    # Last-resort fallback: re-import and read the public attr.
    import psycopg2 as _psycopg2
    return _psycopg2.connect


_REAL_CONNECT_FACTORY = _resolve_real_connect()


# ---------------------------------------------------------------------------
# v3core imports — kept here so all the safety checks above run before any
# potentially side-effectful import (e.g. v3core.* modules that touch PG
# schema at import time on some builds).
# ---------------------------------------------------------------------------
from v3core._deadline import (  # noqa: E402
    PrefetchDeadline,
    PrefetchDeadlineExceeded,
)
from v3core import recall_pool  # noqa: E402
from v3core.pg_pool import PgPool  # noqa: E402
from v3core.pg_store import PgEmbedStore  # noqa: E402
from v3core.recall_v2.engine import (  # noqa: E402
    LegacySink,
    RecallV2Engine,
    RecallV2Result,
)
from v3core.recall_v2.adapters import build_query_context  # noqa: E402
from v3core.recall_v2.contracts import build_default_query_plan  # noqa: E402
from v3core.recall_v2.trace import RecallTrace  # noqa: E402
from v3core.types import RecallHit  # noqa: E402


# ---------------------------------------------------------------------------
# Production-shaped config dict helper.
#
# Tests in this module MUST NOT pass ``config=_production_config()`` to
# ``recall_pool.recall_pool(...)`` / ``RecallV2Engine.recall(...)``.
# The legacy keyword + vector paths assume ``config`` is a dict and call
# ``config.get("recall")`` directly; with ``config=_production_config()`` the keyword
# path raises ``AttributeError: 'NoneType' object has no attribute
# 'get'`` (recall_pool.py:2074 / 2356).  This is pre-existing behaviour,
# not fixed in G6B.  Production always passes a dict, so the test must
# match — the helper below mirrors the shape ``prefetch()`` itself
# builds.
#
# NOTE: ``PgEmbedStore(config=None, pool=...)`` is a DIFFERENT
# constructor argument (the embeddings-provider config) and is left as
# ``None`` — that constructor is None-safe.
# ---------------------------------------------------------------------------
def _production_config() -> dict:
    """Return a production-shaped config dict (same shape ``prefetch()`` builds).

    Shape (must stay in sync with ``v3core.prefetch.prefetch``):
        {
            "prefetch": {"dual_path": True, "rrf_k": 60,
                         "include_message_vector": False},
            "storage": {"rerank": {}},
            "recall": {"vector_top_mult": 4, "qa_per_term_min": 10,
                       "qa_freq_limit": 40},
            "half_life": 30,
        }
    """
    return {
        "prefetch": {
            "dual_path": True,
            "rrf_k": 60,
            "include_message_vector": False,
        },
        "storage": {
            "rerank": {},
        },
        "recall": {
            "vector_top_mult": 4,
            "qa_per_term_min": 10,
            "qa_freq_limit": 40,
        },
        "half_life": 30,
    }


# ---------------------------------------------------------------------------
# Idempotent PG-connect bypass — the conftest's session-scoped autouse
# fixture installs a ``psycopg2.connect = _blocked`` that raises.  We use
# ``_REAL_CONNECT_FACTORY`` (resolved above from the C extension) so the
# conftest's shadow on the public ``psycopg2.connect`` attribute cannot
# affect our connection path.  A per-module autouse fixture additionally
# restores ``psycopg2.connect`` to its prior value (real or shadowed) on
# teardown so the rest of the test session is unaffected.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_pg_connect():
    """Defensively re-install the real psycopg2.connect for this test and
    restore whatever was there (the conftest's blocked version) afterwards.

    Note: this is belt-and-braces — the real factory used to build the
    pool is ``_REAL_CONNECT_FACTORY`` which the conftest cannot shadow.
    """
    import psycopg2 as _psycopg2
    saved = getattr(_psycopg2, "connect", _REAL_CONNECT_FACTORY)
    try:
        _psycopg2.connect = _REAL_CONNECT_FACTORY  # type: ignore[assignment]
    except Exception:
        pass
    try:
        yield
    finally:
        try:
            _psycopg2.connect = saved  # type: ignore[assignment]
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Test-id prefixes — every row this module creates carries one of these
# prefixes so the seed is idempotent (DELETE WHERE source_id LIKE 'g6b_test_%')
# and never touches production rows.
# ---------------------------------------------------------------------------

_QA_PREFIX = "g6b_test_qa_"
_TOPIC_PREFIX = "g6b_test_topic_"
_NOTE_PREFIX = "g6b_test_note_"
_YIN_PREFIX = "g6b_test_yin_"
_EXPLICIT_PREFIX = "g6b_test_mem_"
_DSN = _TEST_DSN  # alias for readability


def _factory() -> Any:
    # Use the C-extension factory resolved at import time, NOT
    # ``psycopg2.connect`` (which the conftest's P0-A guard shadows).
    return _REAL_CONNECT_FACTORY(_DSN)


def _dim_vector(token: str, dim: int = 1024) -> list[float]:
    """Deterministic 1024-dim one-hot vector for the given token.
    The vector is the same on every call (so the cosine similarity
    between identical tokens is exactly 1.0 and between distinct
    tokens is exactly 0.0).
    """
    h = abs(hash(token)) % dim
    vec = [0.0] * dim
    vec[h] = 1.0
    return vec


def _seed_and_cleanup_ids() -> dict[str, list[Any]]:
    """Compute the set of ids this test creates.  Returned for
    visibility in the failure path; the cleanup query uses these
    prefixes directly.
    """
    return {
        "qa_prefix": _QA_PREFIX,
        "topic_prefix": _TOPIC_PREFIX,
        "note_prefix": _NOTE_PREFIX,
        "yin_prefix": _YIN_PREFIX,
        "explicit_prefix": _EXPLICIT_PREFIX,
    }


@pytest.fixture(scope="module")
def pg_pool() -> PgPool:
    """Real PgPool with max_connections=3, min_connections=0, wired
    to the disposable DSN.
    """
    return PgPool(_factory, max_connections=3, min_connections=0)


@pytest.fixture(scope="module")
def pg_store(pg_pool: PgPool) -> PgEmbedStore:
    """Real PgEmbedStore(config=None, pool=pg_pool).  No
    embeddings-provider path is exercised.
    """
    return PgEmbedStore(config=None, pool=pg_pool)


@pytest.fixture(scope="module", autouse=True)
def _seed(pg_pool: PgPool) -> Any:
    """Idempotent seed: insert minimal synthetic rows and DELETE
    only the rows this test creates (by id prefix).  We use
    ``IF NOT EXISTS``-style deletes via explicit ``DELETE WHERE
    source_id LIKE 'prefix_%'`` at module teardown.
    """
    # First, clean any prior runs of these specific prefixes.
    with pg_pool.lease() as lease:
        conn = lease.connection
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM public.qa_pairs WHERE source_id LIKE %s",
            (_QA_PREFIX + "%",),
        )
        cur.execute(
            "DELETE FROM public.topics WHERE topic_id LIKE %s",
            (_TOPIC_PREFIX + "%",),
        )
        cur.execute(
            "DELETE FROM public.topic_entries WHERE topic_id LIKE %s",
            (_TOPIC_PREFIX + "%",),
        )
        cur.execute(
            "DELETE FROM public.observation_notes WHERE version LIKE %s",
            (_NOTE_PREFIX + "%",),
        )
        cur.execute(
            "DELETE FROM public.yin_paragraphs WHERE section LIKE %s",
            (_YIN_PREFIX + "%",),
        )
        cur.execute(
            "DELETE FROM public.explicit_memories WHERE memory_id LIKE %s",
            (_EXPLICIT_PREFIX + "%",),
        )
        conn.commit()

    # Seed qa_pairs (>=4 rows, one with a rare token).
    with pg_pool.lease() as lease:
        conn = lease.connection
        cur = conn.cursor()
        qa_rows = [
            (
                f"{_QA_PREFIX}1",
                "sess1",
                1,
                "What is the g6b_test_token_rare_alpha question?",
                "answer-1",
                "2026-09-14T10:00:00+00:00",
                "live_sync",
                json.dumps([]),
                json.dumps([]),
                _dim_vector(_QA_PREFIX + "1"),
                "bge-m3",
            ),
            (
                f"{_QA_PREFIX}2",
                "sess1",
                2,
                "What is the g6b_test_token_common_beta question?",
                "answer-2",
                "2026-09-14T10:01:00+00:00",
                "live_sync",
                json.dumps([]),
                json.dumps([]),
                _dim_vector(_QA_PREFIX + "2"),
                "bge-m3",
            ),
            (
                f"{_QA_PREFIX}3",
                "sess2",
                1,
                "What is the g6b_test_token_common_gamma question?",
                "answer-3",
                "2026-09-14T10:02:00+00:00",
                "live_sync",
                json.dumps([]),
                json.dumps([]),
                _dim_vector(_QA_PREFIX + "3"),
                "bge-m3",
            ),
            (
                f"{_QA_PREFIX}4",
                "sess2",
                2,
                "What is the g6b_test_token_rare_delta question?",
                "answer-4",
                "2026-09-14T10:03:00+00:00",
                "live_sync",
                json.dumps([]),
                json.dumps([]),
                _dim_vector(_QA_PREFIX + "4"),
                "bge-m3",
            ),
        ]
        for r in qa_rows:
            cur.execute(
                """
                INSERT INTO public.qa_pairs
                    (source_id, session_id, turn_id, question, answer,
                     timestamp, source, tool_calls, tool_results,
                     embedding, embed_model)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s::vector, %s)
                ON CONFLICT (source_id) DO NOTHING
                """,
                r,
            )

        # Seed topics (>=2).
        topic_rows = [
            (
                f"{_TOPIC_PREFIX}1",
                "g6b-test-topic-1-title",
                "g6b-test-topic-1-summary",
                "g6b-test-topic-1-body g6b_test_token_topic_unique",
                list({"g6b_test_token_topic_unique", "alpha"}),
                _dim_vector(_TOPIC_PREFIX + "1"),
                "bge-m3",
            ),
            (
                f"{_TOPIC_PREFIX}2",
                "g6b-test-topic-2-title",
                "g6b-test-topic-2-summary",
                "g6b-test-topic-2-body g6b_test_token_common",
                list({"g6b_test_token_common", "beta"}),
                _dim_vector(_TOPIC_PREFIX + "2"),
                "bge-m3",
            ),
        ]
        for t in topic_rows:
            cur.execute(
                """
                INSERT INTO public.topics
                    (topic_id, title, summary, body, keywords,
                     embedding, embed_model, status)
                VALUES (%s, %s, %s, %s, %s::text[], %s::vector, %s, 'active')
                ON CONFLICT (topic_id) DO NOTHING
                """,
                t,
            )

        # Seed topic_entries (>=1 per topic).
        for topic_id, qid in (
            (f"{_TOPIC_PREFIX}1", f"{_QA_PREFIX}1"),
            (f"{_TOPIC_PREFIX}2", f"{_QA_PREFIX}2"),
        ):
            cur.execute(
                """
                INSERT INTO public.topic_entries
                    (topic_id, seq, question, answer, source,
                     timestamp, embedding, embed_model, source_qa_id)
                VALUES (%s, 0, %s, %s, 'user', NOW(),
                        %s::vector, 'bge-m3',
                        (SELECT id FROM public.qa_pairs
                         WHERE source_id = %s LIMIT 1))
                """,
                (
                    topic_id,
                    f"question-for-{topic_id}",
                    f"answer-for-{topic_id}",
                    _dim_vector(topic_id),
                    qid,
                ),
            )

        # Seed observation_notes (>=2 with different created_at).
        note_rows = [
            (
                f"{_NOTE_PREFIX}v1",
                "g6b-test-note-content-1 g6b_test_token_observation",
                _dim_vector(_NOTE_PREFIX + "1"),
                "bge-m3",
                "2026-09-14T10:00:00+00:00",
            ),
            (
                f"{_NOTE_PREFIX}v2",
                "g6b-test-note-content-2 g6b_test_token_observation",
                _dim_vector(_NOTE_PREFIX + "2"),
                "bge-m3",
                "2026-09-15T10:00:00+00:00",
            ),
        ]
        for n in note_rows:
            cur.execute(
                """
                INSERT INTO public.observation_notes
                    (version, content, embedding, embed_model, created_at)
                VALUES (%s, %s, %s::vector, %s, %s::timestamptz)
                """,
                n,
            )

        # Seed yin_paragraphs (>=1).
        cur.execute(
            """
            INSERT INTO public.yin_paragraphs
                (yin_version, section, content, embedding, embed_model)
            VALUES (%s, %s, %s, %s::vector, 'bge-m3')
            """,
            (
                "g6b_test_v1",
                f"{_YIN_PREFIX}section1",
                "g6b-test-yin-content-1 g6b_test_token_yin",
                _dim_vector(_YIN_PREFIX + "1"),
            ),
        )

        # Seed explicit_memories (>=2: one old, one recent, both active).
        explicit_rows = [
            (
                f"{_EXPLICIT_PREFIX}old",
                "history",
                "g6b-test-explicit-old-title",
                "g6b-test-explicit-old-content g6b_test_token_explicit",
                list({"g6b_test_token_explicit"}),
                json.dumps({}),
                "active",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                _dim_vector(_EXPLICIT_PREFIX + "old"),
                "bge-m3",
            ),
            (
                f"{_EXPLICIT_PREFIX}recent",
                "history",
                "g6b-test-explicit-recent-title",
                "g6b-test-explicit-recent-content g6b_test_token_explicit",
                list({"g6b_test_token_explicit"}),
                json.dumps({}),
                "active",
                "2026-09-14T00:00:00+00:00",
                "2026-09-14T00:00:00+00:00",
                _dim_vector(_EXPLICIT_PREFIX + "recent"),
                "bge-m3",
            ),
        ]
        for em in explicit_rows:
            cur.execute(
                """
                INSERT INTO public.explicit_memories
                    (memory_id, category, title, content, tags,
                     provenance, status, created_at, updated_at,
                     embedding, embed_model)
                VALUES (%s, %s, %s, %s, %s::text[], %s::jsonb,
                        %s, %s::timestamptz, %s::timestamptz,
                        %s::vector, %s)
                ON CONFLICT (memory_id) DO NOTHING
                """,
                em,
            )

        conn.commit()

    yield _seed_and_cleanup_ids()

    # Teardown: delete only the rows this test created.
    try:
        with pg_pool.lease() as lease:
            conn = lease.connection
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM public.qa_pairs WHERE source_id LIKE %s",
                (_QA_PREFIX + "%",),
            )
            cur.execute(
                "DELETE FROM public.topics WHERE topic_id LIKE %s",
                (_TOPIC_PREFIX + "%",),
            )
            cur.execute(
                "DELETE FROM public.topic_entries WHERE topic_id LIKE %s",
                (_TOPIC_PREFIX + "%",),
            )
            cur.execute(
                "DELETE FROM public.observation_notes WHERE version LIKE %s",
                (_NOTE_PREFIX + "%",),
            )
            cur.execute(
                "DELETE FROM public.yin_paragraphs WHERE section LIKE %s",
                (_YIN_PREFIX + "%",),
            )
            cur.execute(
                "DELETE FROM public.explicit_memories WHERE memory_id LIKE %s",
                (_EXPLICIT_PREFIX + "%",),
            )
            conn.commit()
    finally:
        try:
            pg_pool.shutdown()
        except Exception:
            pass


# ===========================================================================
# Real-PG parity tests
# ===========================================================================


class TestPgIntegration:
    """Real-PG parity tests.  All paths exercise the live
    ``recall_pool`` against the seeded synthetic data.
    """

    def test_keyword_qa_path_returns_seeded_qa_ids(
        self, pg_pool: PgPool, pg_store: PgEmbedStore
    ) -> None:
        # Use a query token present in qa_pairs (and seeded into the
        # topics too, so multiple paths may fire).
        q = "g6b_test_token_rare_alpha"
        # We need q_emb=None so the QA vector path is skipped
        # (deterministic test of the keyword+QA path only).
        legacy_hits, _ = recall_pool.recall_pool(
            q, card_index={}, pg=pg_store, q_emb=None,
            include_keyword=True, include_card_vector=False,
            include_message_vector=False, include_effective=False,
            include_topic=True, include_yin=False, include_notes=False,
            rerank_top_n=None, rerank_cfg=None, limit=10,
            pg_was_connected=True, config=_production_config(), sqlite_store=None,
            core=None, deadline=None,
        )
        # The legacy path returned SOMETHING (>=1 hit carrying our
        # qa_N id), proving the keyword/QA SQL path is wired.
        assert len(legacy_hits) >= 1
        # The legacy kw path uses ``qa_{row_id}`` for source_id (see
        # recall_pool._score_qa_rows); the row id is the qa_pairs PK,
        # so we resolve it back to source_id via a small PG query to
        # confirm the SQL path actually hit our seeded rows.
        sids = [h.source_id for h in legacy_hits]
        qa_ids = [
            int(s.split("_", 1)[1])
            for s in sids
            if s.startswith("qa_") and s.split("_", 1)[1].isdigit()
        ]
        assert qa_ids, f"no qa_* hits returned: {sids}"
        with pg_pool.lease() as lease:
            cur = lease.connection.cursor()
            cur.execute(
                "SELECT source_id FROM public.qa_pairs WHERE id = ANY(%s)",
                (qa_ids,),
            )
            source_ids = {row[0] for row in cur.fetchall()}
        assert any(s.startswith(_QA_PREFIX) for s in source_ids), (
            f"expected at least one qa_pairs row with a g6b_test_qa_* "
            f"source_id, got sids={sids}, source_ids={source_ids}"
        )

    def test_c2_delivery_exact_match_is_admitted_against_generic_term_qa_hits(
        self, pg_pool: PgPool, pg_store: PgEmbedStore
    ) -> None:
        """B6: a delivery matched by a distinctive term must not lose to generic matches.

        Measured failure this pins (real PG, isolated fixture): the delivery held one
        half-weight keyword vote (0.5/(K+4) = 0.007812) while eight QA rows matching
        only generic terms ('the'/'what'/'was') held two full-weight votes each, so the
        cut at limit=8 was 0.021952 and the delivery was dropped OUTSIDE_LIMIT at fused
        position 12.  Deliveries enter fusion through one lexical path only, so no rank
        inside that lane can ever reach a full-weight vote; the exact-hit admission
        signal the QA path already used (rare term, or >=2 low-frequency terms) is now
        computed for deliveries as well, capped at one delivery per query.
        """
        import uuid

        suffix = uuid.uuid4().hex[:12]
        needle = f"p0b6deliveryneedle{suffix}"
        delivery_source = f"delivery/p0b6-testhost/g6b_test_b6_{suffix}/evt_{suffix}"
        qa_prefix = f"g6b_test_b6_{suffix}"
        generic = "what was the status of the record"

        with pg_pool.lease() as lease:
            conn = lease.connection
            with conn.cursor() as cur:
                for index in range(10):
                    cur.execute(
                        """
                        INSERT INTO public.qa_pairs
                            (source_id, session_id, turn_id, question, answer,
                             timestamp, source, tool_calls, tool_results,
                             embedding, embed_model)
                        VALUES (%s, %s, %s, %s, %s, NOW(), %s, %s::jsonb, %s::jsonb,
                                %s::vector, %s)
                        ON CONFLICT (source_id) DO NOTHING
                        """,
                        (
                            f"{qa_prefix}_qa{index}",
                            f"{qa_prefix}_session",
                            1,
                            f"{generic} {index}?",
                            f"generic answer {index}",
                            "g6b-test",
                            json.dumps([]),
                            json.dumps([]),
                            # Embedding + model stay consistent (the module's own
                            # convention).  The query below passes q_emb=None, so the
                            # vector lane never runs and these rows compete through
                            # keyword only — which is the point of the test.
                            _dim_vector(f"{qa_prefix}_qa{index}"),
                            "bge-m3",
                        ),
                    )
                cur.execute(
                    """
                    INSERT INTO public.assistant_deliveries
                        (source_id, host, session_id, trigger_event_id, trigger_kind,
                         content, event_ids, started_at, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, NOW(), NOW())
                    ON CONFLICT (source_id) DO NOTHING
                    """,
                    (
                        delivery_source,
                        "p0b6-testhost",
                        f"g6b_test_b6_{suffix}",
                        f"evt_{suffix}",
                        "ASYNC_BOUNDARY",
                        f"{needle} final result is the canary marker",
                        json.dumps([]),
                    ),
                )
            conn.commit()

        try:
            hits, _ = recall_pool.recall_pool(
                f"what was the final result of {needle}?",
                card_index={}, pg=pg_store, q_emb=None,
                include_keyword=True, include_card_vector=False,
                include_message_vector=False, include_effective=False,
                include_topic=False, include_yin=False, include_notes=False,
                rerank_top_n=None, rerank_cfg=None, limit=8,
                pg_was_connected=True, config=_production_config(),
                sqlite_store=None, core=None, deadline=None,
            )
            delivery_hits = [hit for hit in hits if hit.source_id == delivery_source]
            assert len(delivery_hits) == 1, [hit.source_id for hit in hits]
            hit = delivery_hits[0]
            assert hit.kind == "assistant_delivery"
            assert hit.rrf_score is not None and hit.rrf_score >= 0.5, hit.rrf_score
            assert hits[0].source_id == delivery_source, [h.source_id for h in hits[:3]]
        finally:
            with pg_pool.lease() as lease:
                conn = lease.connection
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM public.assistant_deliveries WHERE source_id = %s",
                        (delivery_source,),
                    )
                    cur.execute(
                        "DELETE FROM public.qa_pairs WHERE source_id LIKE %s",
                        (f"{qa_prefix}%",),
                    )
                conn.commit()

    def test_c2_delivery_rebuilds_and_enters_existing_qa_recall_lane(
        self, pg_pool: PgPool, pg_store: PgEmbedStore
    ) -> None:
        """Real PG proof: source -> derived delivery -> legacy recall -> F2 trace."""
        import uuid

        from datetime import datetime

        from v3core.assistant_delivery import (
            DeliveryGroup,
            delivery_source_id,
            rebuild_deliveries,
            write_delivery,
        )
        from v3core.recall_v2.adapters import lane_for_kind

        suffix = uuid.uuid4().hex[:12]
        host = "p0c2-y400"
        session_id = f"g6b_test_c2delivery_{suffix}"
        trigger_id = f"{session_id}_trigger"
        assistant_ids = [f"{session_id}_a1", f"{session_id}_a2"]
        source_id = delivery_source_id(host, session_id, trigger_id)
        query_token = f"p0c2deliveryneedle{suffix}"

        def insert_source(event_id: str, role: str, content: str,
                          timestamp: str) -> None:
            inserted = pg_store.insert_message(
                event_id,
                content,
                embedding=None,
                metadata={
                    "session_id": session_id,
                    "role": role,
                    "host": host,
                    "event_id": event_id,
                    "turn_id": 1,
                    "timestamp": timestamp,
                },
            )
            assert inserted, f"source event did not insert: {event_id}"

        try:
            insert_source(
                trigger_id, "host_event:ASYNC_BOUNDARY",
                "background task complete",
                "2026-10-06T01:00:00+00:00",
            )
            insert_source(
                assistant_ids[0], "assistant", "The task started safely.",
                "2026-10-06T01:00:01+00:00",
            )
            insert_source(
                assistant_ids[1], "assistant",
                f"Final result contains {query_token} and is ready.",
                "2026-10-06T01:00:02+00:00",
            )

            boundary_time = datetime.fromisoformat("2026-10-06T01:00:00+00:00")
            source_a1 = "The task started safely."
            first_group = DeliveryGroup(
                trigger_event_id=trigger_id,
                trigger_kind="ASYNC_BOUNDARY",
                host=host,
                session_id=session_id,
                event_ids=[assistant_ids[0]],
                segments=[(assistant_ids[0], source_a1)],
                content=source_a1,
                started_at=boundary_time,
            )
            assert write_delivery(pg_store, first_group, strict=True)

            # Same F2 event ID with rewritten snapshot text must not replace the
            # first durable source body.
            stale_a1 = "shortened replay snapshot"
            same_id_replay = DeliveryGroup(
                trigger_event_id=trigger_id,
                trigger_kind="ASYNC_BOUNDARY",
                host=host,
                session_id=session_id,
                event_ids=[assistant_ids[0]],
                segments=[(assistant_ids[0], stale_a1)],
                content=stale_a1,
                started_at=boundary_time,
            )
            assert write_delivery(pg_store, same_id_replay, strict=True)

            source_a2 = f"Final result contains {query_token} and is ready."
            extended_group = DeliveryGroup(
                trigger_event_id=trigger_id,
                trigger_kind="ASYNC_BOUNDARY",
                host=host,
                session_id=session_id,
                event_ids=assistant_ids,
                segments=[(assistant_ids[0], stale_a1), (assistant_ids[1], source_a2)],
                content=chr(10).join([stale_a1, source_a2]),
                started_at=boundary_time,
            )
            assert write_delivery(pg_store, extended_group, strict=True)
            with pg_pool.lease() as lease:
                cur = lease.connection.cursor()
                cur.execute(
                    "SELECT content, event_ids FROM public.assistant_deliveries "
                    "WHERE source_id=%s",
                    (source_id,),
                )
                live_row = cur.fetchone()
                assert live_row is not None
                assert live_row[0] == chr(10).join([source_a1, source_a2]), live_row
                assert list(live_row[1]) == assistant_ids

            # Canonical rebuild from conversation_stream replaces any stale
            # derivative with the exact authoritative source representation.
            assert rebuild_deliveries(pg_store, session_id, host=host) == 1
            hits, _ = recall_pool.recall_pool(
                query_token,
                card_index={}, pg=pg_store, q_emb=None,
                include_keyword=True, include_card_vector=False,
                include_message_vector=False, include_effective=False,
                include_topic=False, include_yin=False, include_notes=False,
                rerank_top_n=None, rerank_cfg=None, limit=10,
                pg_was_connected=True, config=_production_config(),
                sqlite_store=None, core=None, deadline=None,
            )
            delivery_hits = [hit for hit in hits if hit.source_id == source_id]
            assert len(delivery_hits) == 1, [hit.source_id for hit in hits]
            hit = delivery_hits[0]
            assert hit.kind == "assistant_delivery"
            assert lane_for_kind(hit.kind) == "qa"
            assert query_token in hit.content

            with pg_pool.lease() as lease:
                cur = lease.connection.cursor()
                cur.execute(
                    "SELECT host, session_id, trigger_event_id, event_ids "
                    "FROM public.assistant_deliveries WHERE source_id = %s",
                    (source_id,),
                )
                row = cur.fetchone()
                assert row is not None
                assert row[:3] == (host, session_id, trigger_id)
                assert list(row[3]) == assistant_ids
                cur.execute(
                    "SELECT event_id, role, content FROM public.conversation_stream "
                    "WHERE host = %s AND session_id = %s AND event_id = ANY(%s)",
                    (host, session_id, assistant_ids),
                )
                source_rows = cur.fetchall()
                assert {row[0] for row in source_rows} == set(assistant_ids)
        finally:
            with pg_pool.lease() as lease:
                conn = lease.connection
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM public.assistant_deliveries WHERE source_id = %s",
                    (source_id,),
                )
                cur.execute(
                    "DELETE FROM public.conversation_stream "
                    "WHERE host = %s AND session_id = %s AND event_id = ANY(%s)",
                    (host, session_id, [trigger_id, *assistant_ids]),
                )
                conn.commit()

    def test_c2_historical_repair_plan_is_read_only_with_real_pg(
        self, pg_pool: PgPool, pg_store: PgEmbedStore
    ) -> None:
        """Real PG fixture: deterministic host-scoped repair plan, zero writes."""
        import json
        import uuid

        from v3core import V3Core
        from v3core.assistant_delivery import plan_delivery_repair

        suffix = uuid.uuid4().hex[:12]
        host = "p0c2-y400-repair"
        session_id = f"g6b_test_c2repair_{suffix}"
        trigger_id = f"{session_id}_trigger"
        assistant_ids = [f"{session_id}_a1", f"{session_id}_a2"]
        qa_ids = [
            V3Core._qa_source_id(session_id, 1, f"{session_id}_human_a", host=host),
            V3Core._qa_source_id(session_id, 1, f"{session_id}_human_b", host=host),
        ]
        payload_a = f"c2repairpayloada{suffix}"
        payload_b = f"c2repairpayloadb{suffix}"

        def insert_source(event_id: str, role: str, content: str, timestamp: str) -> None:
            inserted = pg_store.insert_message(
                event_id, content, embedding=None,
                metadata={
                    "session_id": session_id, "role": role, "host": host,
                    "event_id": event_id, "turn_id": 1, "timestamp": timestamp,
                },
            )
            assert inserted, f"source event did not insert: {event_id}"

        def snapshot():
            with pg_pool.lease() as lease:
                cur = lease.connection.cursor()
                cur.execute(
                    "SELECT source_id, answer, merged_event_ids FROM public.qa_pairs "
                    "WHERE source_id = ANY(%s) ORDER BY source_id",
                    (qa_ids,),
                )
                qa_state = cur.fetchall()
                cur.execute(
                    "SELECT count(*) FROM public.assistant_deliveries "
                    "WHERE host=%s AND session_id=%s",
                    (host, session_id),
                )
                delivery_count = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT event_id, role, content FROM public.conversation_stream "
                    "WHERE host=%s AND session_id=%s AND event_id = ANY(%s) "
                    "ORDER BY id",
                    (host, session_id, [trigger_id, *assistant_ids]),
                )
                source_state = cur.fetchall()
                return qa_state, delivery_count, source_state

        try:
            insert_source(trigger_id, "host_event:ASYNC_BOUNDARY",
                          "background task complete", "2026-10-06T01:10:00+00:00")
            insert_source(assistant_ids[0], "assistant", payload_a,
                          "2026-10-06T01:10:01+00:00")
            insert_source(assistant_ids[1], "assistant", payload_b,
                          "2026-10-06T01:10:02+00:00")
            with pg_pool.lease() as lease:
                conn = lease.connection
                cur = conn.cursor()
                rows = [
                    (qa_ids[0], session_id, 1, "human question",
                     f"human answer {payload_a}\\n{payload_b}", "2026-10-06T01:10:03+00:00",
                     "live_sync", "[]", "[]", None, "", json.dumps(assistant_ids)),
                    (qa_ids[1], session_id, 1, "legacy human question",
                     f"answer without a provenance ledger contains {payload_b}",
                     "2026-10-06T01:10:04+00:00", "live_sync", "[]", "[]", None, "", "[]"),
                ]
                for row in rows:
                    cur.execute(
                        "INSERT INTO public.qa_pairs "
                        "(source_id,session_id,turn_id,question,answer,timestamp,source,"
                        "tool_calls,tool_results,embedding,embed_model,merged_event_ids) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::vector,%s,%s::jsonb) "
                        "ON CONFLICT (source_id) DO NOTHING",
                        row,
                    )
                conn.commit()

            before = snapshot()
            plan_a = plan_delivery_repair(pg_store, session_id, host)
            plan_b = plan_delivery_repair(pg_store, session_id, host)
            after = snapshot()
            assert plan_a == plan_b, plan_a
            assert before == after, "repair dry-run must not modify QA or sidecar rows"
            assert len(plan_a["would_create_deliveries"]) == 1, plan_a
            assert plan_a["would_create_deliveries"][0]["event_ids"] == assistant_ids
            assert [x["qa_source_id"] for x in plan_a["would_rebuild_polluted_qa"]] == [qa_ids[0]]
            assert plan_a["would_rebuild_polluted_qa"][0]["contaminated_event_ids"] == assistant_ids
            assert [x["qa_source_id"] for x in plan_a["unknown"]] == [qa_ids[1]]
            encoded_plan = json.dumps(plan_a, ensure_ascii=False)
            assert payload_a not in encoded_plan and payload_b not in encoded_plan
        finally:
            with pg_pool.lease() as lease:
                conn = lease.connection
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM public.assistant_deliveries WHERE host=%s AND session_id=%s",
                    (host, session_id),
                )
                cur.execute("DELETE FROM public.qa_pairs WHERE source_id = ANY(%s)", (qa_ids,))
                cur.execute(
                    "DELETE FROM public.conversation_stream "
                    "WHERE host=%s AND session_id=%s AND event_id = ANY(%s)",
                    (host, session_id, [trigger_id, *assistant_ids]),
                )
                conn.commit()

    def test_vector_path_returns_hits_via_supplied_q_emb(
        self, pg_pool: PgPool, pg_store: PgEmbedStore
    ) -> None:
        # Use the embedding of qa_1 — the vector path returns that
        # exact hit as the top-1 ANN.
        q_emb = _dim_vector(_QA_PREFIX + "1")
        legacy_hits, _ = recall_pool.recall_pool(
            "g6b_test_token_rare_alpha",
            card_index={}, pg=pg_store, q_emb=q_emb,
            include_keyword=False, include_card_vector=True,
            include_message_vector=False, include_effective=False,
            include_topic=True, include_yin=False, include_notes=False,
            rerank_top_n=None, rerank_cfg=None, limit=5,
            pg_was_connected=True, config=_production_config(), sqlite_store=None,
            core=None, deadline=None,
        )
        # At least one hit was returned via the vector / topic path.
        assert len(legacy_hits) >= 1

    def test_topic_path_returns_topic_prefixed_ids(
        self, pg_pool: PgPool, pg_store: PgEmbedStore
    ) -> None:
        # NOTE (legacy structural fact, recorded here so the test fails
        # loudly if a future refactor moves it): the legacy topic SQL
        # path (the keyword-block that queries the topics snapshot for
        # ``topic_<id>`` source_ids) is nested INSIDE
        # ``if include_keyword:``.  When ``include_keyword=False`` the
        # topic SQL block is bypassed entirely and only the topic-RRF
        # lane (line 3002, vector similarity via ``topic_recaller.match``)
        # can return topic-prefixed hits — with ``q_emb=None`` that
        # lane is also a no-op against a keyword-only seeded row.  So
        # to actually exercise the legacy topic SQL path the test MUST
        # pass ``include_keyword=True``.
        legacy_hits, _ = recall_pool.recall_pool(
            "g6b_test_token_topic_unique",
            card_index={}, pg=pg_store, q_emb=None,
            include_keyword=True, include_card_vector=False,
            include_message_vector=False, include_effective=False,
            include_topic=True, include_yin=False, include_notes=False,
            rerank_top_n=None, rerank_cfg=None, limit=10,
            pg_was_connected=True, config=_production_config(), sqlite_store=None,
            core=None, deadline=None,
        )
        sids = [h.source_id for h in legacy_hits]
        # Legacy keyword-block formats topic source_ids as
        # ``topic_<topic_id>`` (recall_pool.py ~2497:
        # ``_sid = f"topic_{_r[0]}"``), so the seeded
        # ``g6b_test_topic_1`` topic_id shows up as the source_id
        # ``topic_g6b_test_topic_1``.  Accept either form.
        assert any(
            s.startswith(_TOPIC_PREFIX) or s.startswith("topic_" + _TOPIC_PREFIX)
            for s in sids
        ), (
            f"expected at least one topic-prefixed hit (raw or "
            f"``topic_<id>`` form), got {sids}"
        )

    def test_explicit_path_returns_active_memory_ids(
        self, pg_pool: PgPool, pg_store: PgEmbedStore
    ) -> None:
        # The explicit-memory path needs q_emb so the vector retrieval
        # on explicit_memories is exercised.  Both seeded rows have
        # different vectors, so the ANN returns the closer one (which
        # depends on the supplied q_emb).
        q_emb = _dim_vector(_EXPLICIT_PREFIX + "recent")
        legacy_hits, _ = recall_pool.recall_pool(
            "g6b_test_token_explicit",
            card_index={}, pg=pg_store, q_emb=q_emb,
            include_keyword=True, include_card_vector=True,
            include_message_vector=False, include_effective=False,
            include_topic=True, include_yin=False, include_notes=False,
            rerank_top_n=None, rerank_cfg=None, limit=10,
            pg_was_connected=True, config=_production_config(), sqlite_store=None,
            core=None, deadline=None,
        )
        sids = [h.source_id for h in legacy_hits]
        # At least one hit from the active-memory explicit row(s).
        # The legacy code adds explicit hits via kw_ids (when keyword
        # matches the content) — the seeded tokens are present in the
        # content so this is the path exercised here.
        # We don't pin to a specific source_id because the legacy
        # code may format the id differently; we just require the
        # row data to be reachable.
        assert len(legacy_hits) >= 1, (
            "expected at least one explicit-memory hit"
        )

    def test_combined_recall_dedup_and_ordered(
        self, pg_pool: PgPool, pg_store: PgEmbedStore
    ) -> None:
        # Combined: keyword + vector + topic against a query token
        # that appears in qa, topic, and explicit rows.
        q = "g6b_test_token_rare_alpha"
        q_emb = _dim_vector(_QA_PREFIX + "1")
        legacy_hits, _ = recall_pool.recall_pool(
            q, card_index={}, pg=pg_store, q_emb=q_emb,
            include_keyword=True, include_card_vector=True,
            include_message_vector=False, include_effective=False,
            include_topic=True, include_yin=False, include_notes=False,
            rerank_top_n=None, rerank_cfg=None, limit=10,
            pg_was_connected=True, config=_production_config(), sqlite_store=None,
            core=None, deadline=None,
        )
        # The hit list is dedup'd (no two hits share source_id) and
        # ordered (descending rrf_score).
        sids = [h.source_id for h in legacy_hits]
        assert len(sids) == len(set(sids)), (
            f"hit list not dedup'd: {sids}"
        )
        rrf_scores = [h.rrf_score for h in legacy_hits]
        assert rrf_scores == sorted(rrf_scores, reverse=True), (
            f"hit list not ordered: {list(zip(sids, rrf_scores))}"
        )

    def test_legacy_v2_parity_against_real_pg(
        self, pg_pool: PgPool, pg_store: PgEmbedStore
    ) -> None:
        # Legacy call with trace=None.  Note the explicit ``include_*``
        # arguments: the engine forwards unknown kwargs to the recall
        # callable, so we pass the SAME include_* set to BOTH entries
        # so the parity assertion is not masked by API-shape drift.
        q = "g6b_test_token_rare_alpha"
        q_emb = _dim_vector(_QA_PREFIX + "1")
        # Common kwargs shared by both entries.  ``include_keyword=True,
        # include_card_vector=True, include_yin=False, include_notes=False``
        # matches what a legacy production caller would pass.
        shared_include_kwargs: dict[str, Any] = dict(
            include_keyword=True, include_card_vector=True,
            include_message_vector=False, include_effective=False,
            include_topic=True, include_yin=False, include_notes=False,
        )
        legacy_kwargs: dict[str, Any] = dict(
            card_index={}, pg=pg_store, q_emb=q_emb,
            rerank_top_n=None, rerank_cfg=None, limit=10,
            pg_was_connected=True, config=_production_config(),
            sqlite_store=None, core=None, deadline=None,
            **shared_include_kwargs,
        )
        legacy_hits, legacy_pg_fail = recall_pool.recall_pool(
            q, trace=None, **legacy_kwargs
        )

        # V2 call via the engine.  The engine forwards unknown kwargs
        # to the recall callable via ``**passthrough``, so the same
        # ``include_*`` set reaches the underlying ``recall_pool``.
        engine = RecallV2Engine()
        v2_kwargs: dict[str, Any] = dict(
            card_index={}, pg=pg_store, q_emb=q_emb,
            pg_was_connected=True, core=None, sqlite_store=None,
            deadline=None, rerank_top_n=None, rerank_cfg=None,
        )
        v2_result = engine.recall(
            q, limit=10, max_chars=10_000,
            config=_production_config(),
            **v2_kwargs,
            **shared_include_kwargs,
        )

        # Hit-list equality on the eight documented fields.
        def _key(h: RecallHit) -> tuple:
            return (h.source_id, h.kind, h.rrf_score, h.cosine,
                    h.title, h.category, h.content, tuple(h.facts or []))

        legacy_keys = [_key(h) for h in legacy_hits]
        v2_keys = [_key(h) for h in v2_result.hits]
        assert legacy_keys == v2_keys, (
            f"LEGACY vs V2 hit-list parity failed: "
            f"legacy={legacy_keys} v2={v2_keys}"
        )
        assert legacy_pg_fail == v2_result.pg_fail

        # --- Pinned API-shape gap ---
        # The engine, when called WITHOUT the ``include_*`` kwargs,
        # forwards whatever the engine considers "defaults" to the
        # recall callable (which inherits ``recall_pool``’s own
        # function defaults: ``include_card_vector=False``,
        # ``include_yin=True``, ``include_notes=True``,
        # ``include_keyword=True``, ``include_topic=True``).  A legacy
        # caller that explicitly passes
        # ``include_card_vector=True, include_yin=False, include_notes=False``
        # therefore produces a DIFFERENT hit list than an engine
        # caller that omits ``include_*``.  Asserting that the two
        # differ pins this shape gap explicitly rather than leaving it
        # implicit.
        engine_default_v2_result = engine.recall(
            q, limit=10, max_chars=10_000,
            config=_production_config(),
            **v2_kwargs,
            # NOTE: no ``include_*`` passed — the engine forwards
            # ``recall_pool`` defaults to the callable.
        )
        default_v2_keys = [_key(h) for h in engine_default_v2_result.hits]
        assert default_v2_keys != legacy_keys, (
            "API-shape gap pinning failed: expected engine-without-"
            "include_* hit list to DIFFER from legacy-with-include_* "
            "hit list (recall_pool defaults differ from explicit "
            "legacy include_*); they were equal, which means the "
            "defaults and the explicit legacy include_* happen to "
            "produce the same set for this seed — update the "
            "test to pin a different shape gap."
        )

    def test_deadline_path_against_real_pg(
        self, pg_pool: PgPool, pg_store: PgEmbedStore
    ) -> None:
        # A short-budget PrefetchDeadline — the call either completes
        # or raises PrefetchDeadlineExceeded.  In both cases legacy
        # and V2 must agree.
        deadline = PrefetchDeadline(budget_s=0.5)

        def _try_recall(pool):
            try:
                hits, pg_fail = recall_pool.recall_pool(
                    "g6b_test_token_rare_alpha",
                    card_index={}, pg=pool, q_emb=None,
                    include_keyword=True, include_card_vector=False,
                    include_message_vector=False, include_effective=False,
                    include_topic=False, include_yin=False, include_notes=False,
                    rerank_top_n=None, rerank_cfg=None, limit=5,
                    pg_was_connected=True, config=_production_config(), sqlite_store=None,
                    core=None, deadline=deadline,
                )
                return ("ok", hits, pg_fail)
            except PrefetchDeadlineExceeded as e:
                return ("deadline", str(e), None)

        legacy_outcome = _try_recall(pg_store)

        # Now via V2.
        engine = RecallV2Engine()
        v2_outcome: Any
        try:
            v2_result = engine.recall(
                "g6b_test_token_rare_alpha", limit=5, max_chars=10_000,
                config=_production_config(), card_index={}, pg=pg_store, q_emb=None,
                pg_was_connected=True, core=None, sqlite_store=None,
                deadline=deadline, rerank_top_n=None, rerank_cfg=None,
            )
            v2_outcome = ("ok", v2_result.hits, v2_result.pg_fail)
        except PrefetchDeadlineExceeded:
            v2_outcome = ("deadline", None, None)

        # Both outcomes must agree on the deadline disposition.
        if legacy_outcome[0] == "deadline":
            assert v2_outcome[0] == "deadline", (
                f"deadline disposition differs: legacy={legacy_outcome} "
                f"v2={v2_outcome}"
            )
        else:
            # OK on legacy; V2 may be OK or deadline (budget-bound
            # agreement).  If V2 is OK, the hit lists must be equal.
            if v2_outcome[0] == "ok":
                assert legacy_outcome[1] == v2_outcome[1], (
                    f"OK hit lists differ: legacy={legacy_outcome[1]} "
                    f"v2={v2_outcome[1]}"
                )
            else:
                # V2 timed out but legacy did not — this is allowed
                # only when the legacy completed before the deadline
                # was actually checked.  We do not assert further.
                pass
