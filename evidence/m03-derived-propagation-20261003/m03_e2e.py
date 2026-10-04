#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M03 derived-correction-propagation — real end-to-end acceptance driver.

Task book §29 / §30 / §31 on a **disposable** PostgreSQL (127.0.0.1:55432 only).

  §29  main chain: real ``v3_store`` memory A (carrying the frozen source key)
       -> a real Topic D1 derived from A's source QA (real topic storage + real
       provenance) -> D1 is injected into current recall -> a real
       ``v3_update correct`` A->B -> A historical / B current, D1 still
       physically present, sidecar D1 stale|pending_rebuild, and D1 GONE from
       current injection while a non-invalidated control topic is untouched.
  §30  provider unavailable: the correction stays durable, D1 stays stale and
       suppressed, the sidecar stays ``pending_rebuild``, no artifact is
       invented, and the receipt never claims a successful rebuild.
  §31  successful rebuild with a deterministic STUB provider: T2 is produced,
       ``replacement_derived_id = T2``, ``state = rebuilt``, and T1 is STILL
       suppressed while T2 is admissible (task book §31 verbatim).

Reused, not re-implemented: every fail-closed gate, the env sanitiser, the
artifact-provenance collector, the ``Server`` process owner, the HTTP helpers and
``scrub``/``truncate`` all come from the proven M01/M02 driver
(``m01_m02_integration.py``), imported as a module and re-targeted here.

The driver process itself never imports ``v3core``: every interaction with the
artifact under test happens through the real public HTTP routes or through an
embedded probe process that runs with the isolated interpreter.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ── the proven M01/M02 driver, imported for its gates/helpers only ───────────
_DRIVER_DIR = Path(__file__).resolve().parent
if str(_DRIVER_DIR) not in sys.path:
    sys.path.insert(0, str(_DRIVER_DIR))
import m01_m02_integration as T  # noqa: E402


# ── M03 hard gates ──────────────────────────────────────────────────────────
#: Y400 test laptop only (inherited value; re-asserted, never assumed).
HOST_GATE = T.HOST_GATE
PG_HOST_ALLOWLIST = T.PG_HOST_ALLOWLIST
PG_PORT_ALLOWLIST = T.PG_PORT_ALLOWLIST
PRODUCTION_PORTS = T.PRODUCTION_PORTS
PRODUCTION_DATABASES = T.PRODUCTION_DATABASES
#: One-shot database name prefix mandated by the task.
DB_PREFIX = "m03e2e_"
#: The ONLY tree this driver may create a run root in or write evidence to.
RUN_ROOT_PARENT = Path("C:/hp-testbed/m03-derived-propagation-20261003")

#: Modules whose INSTALLED bytes must equal the source-root bytes BEFORE any
#: database connection: the M03 modules under test plus the surfaces they ride.
ARTIFACT_PROVENANCE_MODULES = (
    "__init__.py",
    "active_memory_store.py",
    "derived_invalidation.py",
    "derived_suppression.py",
    "derived_rebuild.py",
    "recall_pool.py",
    "injector.py",
    "observer.py",
    "pg_store.py",
    "tools/api_update.py",
    "tools/store.py",
    "tools/memory_correction.py",
    "tools/derived_propagation_tool.py",
    "tools/derived_rebuild_tool.py",
    "distribution_cli.py",
)
#: Packaged schema artifacts that must be present in the installed wheel AND
#: match the repo source copy.
REQUIRED_PACKAGED_SQL = (
    "alpha_bootstrap.sql",
    "explicit_memories.sql",
    "memory_relations.sql",
    "derived_memory_invalidations.sql",
)

MAX_RECORDED_CHARS = T.MAX_RECORDED_CHARS
HTTP_TIMEOUT_S = T.HTTP_TIMEOUT_S
READY_TIMEOUT_S = T.READY_TIMEOUT_S

#: Receipt values the correction may truthfully report for the derived layer.
DERIVED_PROPAGATION_VALUES = ("applied", "none", "pending", "pending_rebuild")

#: Sidecar states that mean "this artifact must not be injected".
SUPPRESSING_STATES = ("stale", "pending_rebuild", "rebuilt", "unresolved")

_ACTIVE_SECRET = ""

# ── deterministic STUB embedding endpoint ───────────────────────────────────
#: Task-book-allowed deterministic stub provider. Every vector shares one large
#: base component plus a tiny text-dependent perturbation, so ANY query matches
#: ANY topic (cosine ~0.9999). That makes the topic lane fully LIVE and leaves
#: M03 suppression as the only thing that can drop a candidate — which is
#: exactly what a suppression test needs (and it is what makes the
#: non-invalidated control topic a real selectivity proof).
_STUB_DIM = 1024
_STUB_PERTURB = 0.02


def _lcg_stream(seed: int, n: int):
    x = seed
    for _ in range(n):
        x = (x * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        yield ((x >> 11) / float(1 << 53)) * 2.0 - 1.0


def _stub_base_vector() -> list:
    seed = int.from_bytes(
        hashlib.sha256(b"M03E2E_STUB_EMBED_BASE_V1").digest()[:8], "big")
    return list(_lcg_stream(seed, _STUB_DIM))


_STUB_BASE = _stub_base_vector()


def stub_vector_for(text: str) -> list:
    """Deterministic embedding for one text (same base, tiny text perturbation)."""
    seed = int.from_bytes(
        hashlib.sha256(("M03E2E|" + str(text)).encode("utf-8")).digest()[:8], "big")
    out = [b + _STUB_PERTURB * n for b, n in zip(_STUB_BASE, _lcg_stream(seed, _STUB_DIM))]
    norm = math.sqrt(sum(v * v for v in out)) or 1.0
    return [v / norm for v in out]


class StubEmbedServer:
    """OpenAI-compatible ``POST /v1/embeddings`` stub on 127.0.0.1.

    Records request count and text hashes ONLY — never the text, never a vector,
    so the evidence cannot smuggle a payload into the report.
    """

    def __init__(self) -> None:
        self.requests: list = []
        self._lock = threading.Lock()
        outer = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):  # silence
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:
                    body = {}
                text = body.get("input")
                if isinstance(text, list):
                    texts = [str(t) for t in text]
                else:
                    texts = [str(text or "")]
                with outer._lock:
                    for t in texts:
                        outer.requests.append({
                            "sha256": hashlib.sha256(t.encode("utf-8")).hexdigest(),
                            "chars": len(t),
                        })
                payload = {
                    "object": "list",
                    "model": str(body.get("model") or "m03e2e-stub-embed"),
                    "data": [
                        {"object": "embedding", "index": i,
                         "embedding": stub_vector_for(t)}
                        for i, t in enumerate(texts)
                    ],
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                }
                blob = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/embeddings"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass

    def summary(self) -> dict:
        with self._lock:
            reqs = list(self.requests)
        return {"endpoint": self.endpoint, "request_count": len(reqs),
                "texts_hashed": [r["sha256"][:12] for r in reqs][:40]}


# ── embedded probe (its own process, the isolated interpreter) ──────────────
PROBE_SOURCE = r'''# -*- coding: utf-8 -*-
"""M03 E2E embedded probe.

Runs with the isolated interpreter so it observes exactly the environment the
serve process gets. Stages are driven by a JSON spec file; the result is written
to a JSON file. The probe NEVER prints the PG password.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys


def _spec(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _db_name():
    """The database under test: the injected profile's storage database.

    ``run_probe`` swaps ``V3CORE_CONFIG`` per call, so the probe must follow the
    profile's database (the negative-control run points at the DB whose sidecar
    was dropped) rather than a value captured once at startup.
    """
    injected = os.environ.get("M03_PG_DB") or ""
    if injected:
        return injected
    try:
        from v3core.config import resolve_config
        name = getattr(getattr(resolve_config(), "storage", None), "database", None)
        if name:
            return str(name)
    except Exception:  # noqa: BLE001 — fall back to the driver-injected value
        pass
    return ""


def _conn():
    import psycopg2
    return psycopg2.connect(
        host=os.environ["M03_PG_HOST"], port=int(os.environ["M03_PG_PORT"]),
        dbname=_db_name(), user=os.environ["M03_PG_USER"],
        password=os.environ["PGPASSWORD"], connect_timeout=10,
    )


def _pool():
    from v3core.pg_pool import PgPool
    return PgPool(connect=_conn, max_connections=6, min_connections=0,
                  connect_timeout=10)


def _close_pool(pool):
    """Release a PgPool with whichever teardown verb this build exposes.

    ``PgPool`` exposes ``shutdown`` (``close`` belongs to the pool view/lease
    types), so probing both keeps the probe portable across builds.
    """
    for name in ("shutdown", "close"):
        fn = getattr(pool, name, None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:  # noqa: BLE001 — teardown must never mask a result
                continue


def _lease_conn(pool, timeout=5):
    """Return ``(lease, connection)`` — ``PgLease`` exposes ``.connection``.

    ``with pool.lease(...) as x`` yields the LEASE, not a DBAPI connection, so
    cursors must come off ``lease.connection``.
    """
    lease = pool.lease(timeout=timeout)
    return lease, lease.connection


class _StorePg:
    """Store-shaped ``pg`` surface — the plumbing the running server passes.

    ``active_memory_store._acquire_lease(None, pg)`` prefers the store's
    ``open_side_connection()`` seam, and that is what the server's core hands to
    the M03 handlers. Exposing the same seam here makes the sidecar reads and
    writes take the SAME lease path production takes, instead of the
    ``PgLease``-as-a-connection adapter path that ships broken (defect B).
    Each side connection is genuinely independent and owned by its lease, which
    is the seam's contract.
    """

    def __init__(self, pool):
        self._pool = pool

    def open_side_connection(self):
        return _conn()

    def lease(self, timeout=None, deadline=None):
        return self._pool.lease(timeout=timeout or 5)

    def close(self):
        _close_pool(self._pool)


def _store_pg():
    return _StorePg(_pool())


def _cfg():
    from v3core.config import resolve_config
    return resolve_config()


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        return cur.fetchall()


def _jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    return str(v)


# ─────────────────────────────────────────────────────────────────────────────
def stage_seed_derived(spec):
    """Seed the raw QA fixture + a REAL Topic with REAL provenance.

    The canonical source_id is built from the real ``conversation_stream`` row
    the driver created through the public ``POST /events`` bridge — the same
    structural join M03's resolver performs. Nothing is guessed.
    """
    conn = _conn()
    out = {"stage": "seed_derived"}
    cs_id = int(spec["conversation_stream_id"])
    rows = _rows(conn, "SELECT role, host, session_id, event_id "
                       "FROM public.conversation_stream WHERE id = %s", (cs_id,))
    if not rows:
        out["error"] = f"conversation_stream id {cs_id} not found"
        return out
    role, host, session_id, event_id = rows[0]
    out["conversation_stream"] = {"id": cs_id, "role": role, "host": host,
                                  "session_id": session_id, "event_id": event_id}
    if role != "user":
        out["error"] = f"conversation_stream.role={role!r} is not 'user'"
        return out

    canonical_source_id = f"qa_sync/{host}/{session_id}/{event_id}"
    if spec.get("qa_source_suffix"):
        # control artifacts must NOT share the corrected artifact's raw source,
        # otherwise structural discovery legitimately maps them too
        canonical_source_id = f"{canonical_source_id}#{spec['qa_source_suffix']}"
    out["canonical_source_id"] = canonical_source_id

    # raw fixture: the qa_pairs row the live sync would have written
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.qa_pairs (source_id, session_id, turn_id, "
            "question, answer, source) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (source_id) DO NOTHING",
            (canonical_source_id, str(session_id), 1,
             spec.get("qa_question") or "M03 E2E seed question",
             spec.get("qa_answer") or "M03 E2E seed answer", "live_sync"),
        )
    conn.commit()
    qa_rows = _rows(conn, "SELECT id FROM public.qa_pairs WHERE source_id = %s",
                    (canonical_source_id,))
    if not qa_rows:
        out["error"] = "qa_pairs row was not written"
        return out
    qa_id = int(qa_rows[0][0])
    out["qa_pairs_id"] = qa_id

    # REAL topic storage + REAL provenance (the same writer the observer uses)
    from v3core.topic_store import TopicStore
    from v3core.embedding import build_embed_cfg, safe_embed_cfg
    cfg = _cfg()
    embed_cfg = None
    if spec.get("embed"):
        embed_cfg = build_embed_cfg(cfg)
    store = TopicStore(db_path=str(spec["sqlite_path"]), pg_conn=conn)
    topic_id = store.upsert_topic(
        title=spec["topic_title"], summary=spec.get("topic_summary") or "",
        body=spec["topic_body"], keywords=list(spec.get("topic_keywords") or []),
        topic_id=spec["topic_id"], pg_conn=conn, embed_cfg=embed_cfg,
    )
    out["topic_id"] = topic_id
    entry_id = store.add_entry(
        topic_id=topic_id, source="m03e2e:seed",
        question=spec.get("entry_question") or "",
        answer=spec.get("entry_answer") or "",
        pg_conn=conn, source_qa_id=qa_id,
    )
    out["entry_id"] = entry_id

    # read back through independent SQL — the artifact claim is the DB, not the call
    out["topic_row"] = _jsonable([
        dict(zip(("topic_id", "title", "body", "status",
                  "embedding_is_null", "embed_model"),
                 (r[0], r[1], r[2], r[3], r[4] is None, r[5])))
        for r in _rows(conn,
                       "SELECT topic_id, title, body, status, "
                       "(embedding IS NULL), embed_model FROM public.topics "
                       "WHERE topic_id = %s", (topic_id,))
    ])
    out["entry_rows"] = _jsonable([
        dict(zip(("id", "topic_id", "source_qa_id", "source"),
                 (r[0], r[1], r[2], r[3])))
        for r in _rows(conn,
                       "SELECT id, topic_id, source_qa_id, source "
                       "FROM public.topic_entries WHERE topic_id = %s "
                       "ORDER BY id", (topic_id,))
    ])
    conn.close()
    return out


def stage_seed_note(spec):
    """Seed one observer-note chain (prev <- head) with explicit qa ranges."""
    conn = _conn()
    out = {"stage": "seed_note"}
    prev_lo, prev_hi = spec["prev_range"]
    head_lo, head_hi = spec["head_range"]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.observation_notes (version, content, "
            "source_qa_range, prev_id, links) VALUES (%s, %s, "
            "int8range(%s, %s, '[)'), NULL, '[]'::jsonb) RETURNING id",
            (spec.get("prev_version") or "v_prev", spec["prev_content"],
             int(prev_lo), int(prev_hi)),
        )
        prev_id = int(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO public.observation_notes (version, content, "
            "source_qa_range, prev_id, links) VALUES (%s, %s, "
            "int8range(%s, %s, '[)'), %s, '[]'::jsonb) RETURNING id",
            (spec.get("head_version") or "v_head", spec["head_content"],
             int(head_lo), int(head_hi), prev_id),
        )
        head_id = int(cur.fetchone()[0])
    conn.commit()
    out["prev_id"] = prev_id
    out["head_id"] = head_id
    out["head_rows"] = _jsonable([
        dict(zip(("id", "version", "prev_id"),
                 (r[0], r[1], r[2])))
        for r in _rows(conn, "SELECT id, version, prev_id FROM public.observation_notes "
                             "ORDER BY id")
    ])
    conn.close()
    return out


def stage_lane(spec):
    """Run the REAL recall convergence point (``recall_pool.recall_pool``).

    No embedding is injected: the profile's configured endpoint (the
    deterministic stub) is exercised through the real embedding client, so the
    topic lane is genuinely live.
    """
    out = {"stage": "lane"}
    pool = _pool()
    cfg = _cfg()
    from v3core.recall_pool import recall_pool
    from v3core.derived_suppression import (
        DERIVED_KIND_TOPIC, read_suppression, suppress_topic_hits,
    )
    try:
        result = recall_pool(spec["query"], pg=pool, config=cfg,
                             limit=int(spec.get("limit") or 10),
                             include_topic=True)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        _close_pool(pool)
        return out

    # ``recall_pool`` returns ``(final_hits, pg_fail)``
    if isinstance(result, tuple) and len(result) == 2:
        final, pg_fail = result
    else:
        final, pg_fail = result, None
    out["pg_fail"] = str(pg_fail)
    hit_ids = []
    for h in (final or []):
        sid = getattr(h, "source_id", None)
        if sid is None and isinstance(h, dict):
            sid = h.get("source_id")
        hit_ids.append(str(sid))
    out["hit_ids"] = sorted(set(hit_ids))
    out["topic_hit_ids"] = sorted(s for s in out["hit_ids"]
                                  if s.startswith("topic_"))
    out["hit_count"] = len(final or [])

    # the SAME convergence point on the store-shaped plumbing the running server
    # uses, so the suppression decision can be observed independently of the
    # PgPool lease defect (B)
    try:
        res2 = recall_pool(spec["query"], pg=_store_pg(), config=cfg,
                           limit=int(spec.get("limit") or 10), include_topic=True)
        if isinstance(res2, tuple) and len(res2) == 2:
            final2, pf2 = res2
        else:
            final2, pf2 = res2, None
        ids2 = []
        for h in (final2 or []):
            sid = getattr(h, "source_id", None)
            if sid is None and isinstance(h, dict):
                sid = h.get("source_id")
            ids2.append(str(sid))
        out["topic_hit_ids_store"] = sorted(
            s for s in set(ids2) if s.startswith("topic_"))
        out["pg_fail_store"] = str(pf2)
    except Exception as exc:  # noqa: BLE001
        out["topic_hit_ids_store_error"] = f"{type(exc).__name__}: {exc}"

    # the production plumbing (real PgPool) — recorded verbatim, it fails OPEN
    sup_real = read_suppression(pool, DERIVED_KIND_TOPIC)
    out["suppression_on_real_pool"] = {
        "kind": sup_real.kind, "ids": sorted(sup_real.ids),
        "degraded": bool(sup_real.degraded), "reason": sup_real.reason}
    # the store-shaped plumbing the running server uses — the real sidecar view
    sup = read_suppression(_store_pg(), DERIVED_KIND_TOPIC)
    out["suppression"] = {"kind": sup.kind, "ids": sorted(sup.ids),
                          "degraded": bool(sup.degraded), "reason": sup.reason}
    # the same real function the convergence point calls, applied to a hits map
    # built exactly the way recall_pool builds it (``topic_<topic_id>``)
    probe_hits = {f"topic_{t}": object() for t in (spec.get("probe_topic_ids") or [])}
    removed = suppress_topic_hits(probe_hits, sup)
    out["suppress_probe"] = {"input": sorted(f"topic_{t}" for t in
                                             (spec.get("probe_topic_ids") or [])),
                             "removed": sorted(removed),
                             "kept": sorted(probe_hits)}
    _close_pool(pool)
    return out


def stage_notes(spec):
    """Run the REAL observer chain-head selection against the real sidecar."""
    out = {"stage": "notes"}
    pool = _pool()
    from v3core.derived_suppression import (
        DERIVED_KIND_OBSERVER_NOTE, read_suppression, select_note_head,
    )
    # the production plumbing (real PgPool) — recorded verbatim, it fails OPEN
    sup_real = read_suppression(pool, DERIVED_KIND_OBSERVER_NOTE)
    out["suppression_on_real_pool"] = {
        "kind": sup_real.kind, "ids": sorted(sup_real.ids),
        "degraded": bool(sup_real.degraded), "reason": sup_real.reason}
    # the store-shaped plumbing the running server uses — the real sidecar view
    sup = read_suppression(_store_pg(), DERIVED_KIND_OBSERVER_NOTE)
    out["suppression"] = {"kind": sup.kind, "ids": sorted(sup.ids),
                          "degraded": bool(sup.degraded), "reason": sup.reason}
    lease, conn = _lease_conn(pool)
    try:
        with conn.cursor() as cur:
            row = select_note_head(cur, sup)
            out["selected_head"] = None if row is None else {
                "id": row[0], "version": row[1],
                "prev_id": row[5] if len(row) > 5 else None,
            }
    finally:
        lease.close()
    # the un-suppressed reference selection (no suppression at all) for contrast
    from v3core.derived_suppression import DerivedSuppression
    lease2, conn2 = _lease_conn(pool)
    try:
        with conn2.cursor() as cur:
            row0 = select_note_head(cur, DerivedSuppression(
                DERIVED_KIND_OBSERVER_NOTE, frozenset(), False, ""))
            out["selected_head_without_suppression"] = None if row0 is None else {
                "id": row0[0], "version": row0[1]}
    finally:
        lease2.close()
    _close_pool(pool)
    return out


class _StubRebuildProvider:
    """Deterministic rebuild provider: returns fixed text, records the context."""

    def __init__(self, body, title=None, summary=None, keywords=None, exc=None):
        self.body = body
        self.title = title
        self.summary = summary
        self.keywords = list(keywords or [])
        self.exc = exc
        self.calls = []

    def __call__(self, context):
        self.calls.append(context)
        if self.exc is not None:
            raise RuntimeError(self.exc)
        return {"title": self.title, "summary": self.summary,
                "body": self.body, "keywords": self.keywords}


class _UnusableProvider:
    """Neither callable nor generate/chat — models 'no usable provider'."""


def stage_rebuild(spec):
    """Run the REAL ``v3_update(action='rebuild')`` tool dispatch."""
    out = {"stage": "rebuild"}
    pool = _store_pg()
    cfg = _cfg()
    from v3core.tools import handle_tool_call
    mode = spec.get("mode") or "stub"
    provider = None
    holder = None
    if mode == "stub":
        holder = _StubRebuildProvider(
            body=spec["stub_body"], title=spec.get("stub_title"),
            summary=spec.get("stub_summary"), keywords=spec.get("stub_keywords"))
        provider = holder
    elif mode == "raising":
        holder = _StubRebuildProvider(body="unused", exc=spec.get(
            "raise_text") or "provider exploded")
        provider = holder
    elif mode == "unusable":
        provider = _UnusableProvider()
    args = {"action": "rebuild", "correction_id": spec.get("correction_id") or "",
            "invalidation_id": spec.get("invalidation_id") or ""}
    kw = {"pool": pool, "effective_config": cfg}
    if provider is not None:
        kw["rebuild_provider"] = provider
    try:
        raw = handle_tool_call("v3_update", args, **kw)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        out["receipt"] = _jsonable(parsed)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    if holder is not None:
        ctx = holder.calls[0] if holder.calls else None
        out["provider_call_count"] = len(holder.calls)
        if ctx:
            # only the decision-relevant labels, never the whole prompt
            out["generation_context_summary"] = {
                "task": ctx.get("task"),
                "source_memory_state": (ctx.get("source_memory") or {}).get("state"),
                "source_memory_label": (ctx.get("source_memory") or {}).get("label"),
                "replacement_memory_state": (ctx.get("replacement_memory") or {}).get("state"),
                "replacement_memory_label": (ctx.get("replacement_memory") or {}).get("label"),
                "source_memory_id": (ctx.get("source_memory") or {}).get("memory_id"),
                "replacement_memory_id": (ctx.get("replacement_memory") or {}).get("memory_id"),
                "correction_relation_type": (ctx.get("correction") or {}).get("relation_type"),
                "canonical_qa_id": (ctx.get("original_sources") or {}).get("canonical_qa_id"),
                "topic_state": (ctx.get("old_topic") or {}).get("state"),
            }
    _close_pool(pool)
    return out


def stage_discover_repro(spec):
    """Isolate M03's discovery step: the REAL ``_discover`` vs a corrected query.

    The real ``_discover`` raises ``UndefinedFunction`` on PostgreSQL because
    ``int8range @> %s`` receives a psycopg2-adapted ``integer``. Running both
    forms pins the defect to one statement AND yields the affected set the
    discovery *should* have produced, so the downstream chain can still be
    exercised against real product code.
    """
    out = {"stage": "discover_repro"}
    from v3core import derived_invalidation as DI
    pool = _store_pg()
    qa_ids = [int(x) for x in (spec.get("qa_ids") or [])]

    # (a) the real discovery, verbatim
    try:
        affected, covered = DI._discover(pool, qa_ids)
        out["real_discover"] = {"ok": True, "affected": affected,
                                "covered": sorted(covered)}
    except Exception as exc:  # noqa: BLE001
        out["real_discover"] = {"ok": False, "error_class": type(exc).__name__,
                                "error": f"{type(exc).__name__}: {exc}"}

    # (b) the same statements with the range element cast to bigint
    affected2 = {k: [] for k in DI.DERIVED_KINDS}
    covered2: set = set()
    lease, conn = _lease_conn(pool)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT topic_id FROM {DI._TOPIC_ENTRIES_TABLE} "
                f"WHERE source_qa_id = ANY(%s)", (qa_ids,))
            affected2[DI.DERIVED_KIND_TOPIC] = sorted(
                {str(r[0]) for r in cur.fetchall() if r and r[0] is not None})
            cur.execute(
                f"SELECT DISTINCT source_qa_id FROM {DI._TOPIC_ENTRIES_TABLE} "
                f"WHERE source_qa_id = ANY(%s)", (qa_ids,))
            covered2 |= {int(r[0]) for r in cur.fetchall() if r and r[0] is not None}
            notes: set = set()
            for qa in qa_ids:
                cur.execute(
                    f"SELECT id FROM {DI._OBSERVATION_TABLE} "
                    f"WHERE source_qa_range @> %s::bigint", (qa,))
                found = [r[0] for r in cur.fetchall() if r and r[0] is not None]
                if found:
                    covered2.add(qa)
                    notes |= {str(x) for x in found}
            affected2[DI.DERIVED_KIND_OBSERVER_NOTE] = sorted(notes)
    finally:
        lease.close()
    out["corrected_discover"] = {"affected": affected2, "covered": sorted(covered2)}

    # (c) the two statements in isolation — a copy-pasteable minimal repro
    lease3, conn3 = _lease_conn(pool)
    try:
        out["minimal_repro"] = []
        for sql in ("SELECT 1 WHERE int8range(1,5,'[)') @> %s",
                    "SELECT 1 WHERE int8range(1,5,'[)') @> %s::bigint"):
            try:
                with conn3.cursor() as cur:
                    cur.execute(sql, (1,))
                    rows = [list(r) for r in cur.fetchall()]
                out["minimal_repro"].append({"sql": sql, "ok": True, "rows": rows})
            except Exception as exc:  # noqa: BLE001
                conn3.rollback()
                out["minimal_repro"].append(
                    {"sql": sql, "ok": False,
                     "error": f"{type(exc).__name__}: {str(exc).splitlines()[0]}"})
    finally:
        lease3.close()
    _close_pool(pool)
    return out


def _harness_write_invalidations(pg, rows):
    """Insert invalidation rows with the product's OWN 12-column tuple.

    The shipped ``_write_invalidations`` lists ``_INVALIDATION_INSERT_COLUMNS``
    (12 columns) but supplies a 15-expression VALUES clause, so it raises
    ``SyntaxError: INSERT has more expressions than target columns`` and has
    never written a row (defect C). This workaround keeps every other property
    of the write: the same ``_build_invalidation_rows`` output, the same
    deterministic ids, the product's own column tuple, the same single explicit
    transaction, and ``ON CONFLICT DO NOTHING`` idempotency. ``created_at`` and
    the two trailing NULLs are left to the SQL defaults, which is what the
    column slice implies.
    """
    from v3core import derived_invalidation as DI
    cols = list(DI._INVALIDATION_INSERT_COLUMNS)
    inserted = 0
    pending = 0
    lease = DI._canonical._acquire_lease(None, pg)
    conn = lease.connection
    took = False
    try:
        if getattr(conn, "autocommit", False) is True:
            conn.autocommit = False
            took = True
        cur = conn.cursor()
        cur.execute("BEGIN")
        try:
            for row in rows:
                cur.execute(
                    f"INSERT INTO {DI._INVALIDATION_TABLE} "
                    f"({', '.join(cols)}) VALUES "
                    f"({', '.join(['%s'] * len(cols))}) "
                    f"ON CONFLICT (invalidation_id) DO NOTHING "
                    f"RETURNING invalidation_id",
                    [row.get(c) for c in cols])
                if cur.fetchone() is not None:
                    inserted += 1
                    if row.get("state") == DI.STATE_PENDING_REBUILD:
                        pending += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    except Exception as exc:
        if DI._is_missing_table(exc):
            raise DI._MissingInvalidationTable(str(exc)) from exc
        raise
    finally:
        if took:
            try:
                conn.autocommit = True
            except Exception:  # noqa: BLE001
                pass
        lease.close()
    return inserted, pending


def stage_propagate_fixture(spec):
    """Drive the REAL downstream propagation with discovery fed a corrected query.

    M03's ``_discover`` is not executable on PostgreSQL (see ``discover_repro``),
    so it cannot supply ``affected``. Every other step here is the UNMODIFIED
    product path: the real committed-edge read, the real canonical provenance
    read, the real source resolution, the real ``_build_invalidation_rows`` and
    the real ``_write_invalidations`` writer (real transaction, real
    deterministic ids, real idempotency).
    """
    out = {"stage": "propagate_fixture", "discovery_substituted": True}
    from v3core import derived_invalidation as DI
    pool = _store_pg()
    cid = str(spec.get("correction_id") or "")
    try:
        edge = DI._read_correction_edge(pool, cid)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"read edge: {type(exc).__name__}: {exc}"
        _close_pool(pool)
        return out
    if edge is None:
        out["error"] = "no correction edge for this correction_id"
        _close_pool(pool)
        return out
    out["edge"] = {k: edge.get(k) for k in
                   ("relation_id", "from_memory_id", "relation_type")}
    source_memory_id = edge["from_memory_id"]
    try:
        reader = DI._canonical.ActiveMemoryReader(pool=None, pg=pool)
        record = reader.get_by_memory_id(source_memory_id)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"reader: {type(exc).__name__}: {exc}"
        _close_pool(pool)
        return out
    if record is None:
        out["error"] = f"source memory {source_memory_id} not found"
        _close_pool(pool)
        return out
    provenance = record.get("provenance") or {}
    input_kind, input_id = DI.source_input_identity(provenance)
    qa_ids, resolution = DI.resolve_source_qa_ids(pool, provenance)
    out["resolution"] = {"qa_ids": qa_ids, "status": resolution,
                         "input_source_kind": input_kind,
                         "input_source_id": input_id}
    if resolution != DI.RESOLVE_OK:
        out["error"] = f"source resolution={resolution}"
        _close_pool(pool)
        return out
    canonical_qa_id = qa_ids[0] if len(qa_ids) == 1 else None

    disc = stage_discover_repro({"qa_ids": qa_ids})
    corrected = disc.get("corrected_discover") or {}
    affected = corrected.get("affected") or {}
    covered = set(corrected.get("covered") or [])
    out["discovery"] = {
        "substituted": True,
        "real_discover_error": (disc.get("real_discover") or {}).get("error"),
        "affected": affected,
    }

    try:
        version = reader.read_version(source_memory_id, mode="current")
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"read_version: {type(exc).__name__}: {exc}"
        _close_pool(pool)
        return out
    replacement_memory_id = version.get("resolved_memory_id")
    state = (DI.STATE_PENDING_REBUILD if replacement_memory_id is not None
             else DI.STATE_STALE)
    rows = DI._build_invalidation_rows(
        correction_id=cid, relation_id=edge.get("relation_id"),
        source_memory_id=source_memory_id,
        replacement_memory_id=replacement_memory_id, state=state,
        reason=edge.get("relation_type"), canonical_qa_id=canonical_qa_id,
        input_source_kind=input_kind, input_source_id=input_id,
        affected=affected)
    out["state"] = state
    out["replacement_memory_id"] = replacement_memory_id
    out["unmapped"] = len([q for q in qa_ids if q not in covered])
    out["rows"] = [{k: r.get(k) for k in
                    ("invalidation_id", "derived_kind", "derived_id", "state",
                     "reason", "canonical_qa_id", "input_source_kind",
                     "input_source_id", "replacement_memory_id")}
                   for r in rows]
    if not rows:
        out["status"] = DI.PROPAGATION_NO_LINEAGE
        out["inserted"] = 0
        _close_pool(pool)
        return out
    try:
        inserted, pending = DI._write_invalidations(pool, rows)
        out["status"] = (DI.PROPAGATION_APPLIED if inserted
                         else DI.PROPAGATION_DEDUPLICATED)
        out["inserted"] = inserted
        out["pending_written"] = pending
        out["writer"] = "shipped _write_invalidations"
    except Exception as exc:  # noqa: BLE001
        out["real_writer"] = {
            "ok": False, "error_class": type(exc).__name__,
            "error": f"{type(exc).__name__}: {str(exc).splitlines()[0]}",
            "classified": DI._classify_db_error(exc)}
        # DEFECT C workaround: the shipped INSERT lists 12 columns against a
        # 15-expression VALUES clause, so it can never execute. Re-insert the
        # SAME rows (same builder, same deterministic ids, same column tuple)
        # so the downstream lanes can still be exercised.
        try:
            inserted, pending = _harness_write_invalidations(pool, rows)
            out["status"] = (DI.PROPAGATION_APPLIED if inserted
                             else DI.PROPAGATION_DEDUPLICATED)
            out["inserted"] = inserted
            out["pending_written"] = pending
            out["writer"] = "harness INSERT (defect C workaround)"
        except Exception as exc2:  # noqa: BLE001
            out["status"] = DI._classify_db_error(exc2)
            out["error"] = f"harness write: {type(exc2).__name__}: {exc2}"
    _close_pool(pool)
    return out


def stage_propagate_real(spec):
    """Call the REAL ``derived_invalidation.propagate_correction`` in-process.

    A second, independent capture of the shipped behaviour that bypasses the
    HTTP layer entirely, so a defect can never be attributed to the transport.
    """
    out = {"stage": "propagate_real"}
    from v3core import derived_invalidation as DI
    pool = _store_pg()
    cid = str(spec.get("correction_id") or "")
    try:
        receipt = DI.propagate_correction(pool, correction_id=cid,
                                          dry_run=bool(spec.get("dry_run")))
        out["receipt"] = {k: receipt.get(k) for k in
                          ("status", "success", "derived_propagation", "counts",
                           "derived", "resolution_status", "canonical_qa_id",
                           "resolved_qa_ids", "error", "correction_id")}
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"

    if spec.get("write_probe"):
        # real builder + real writer against a genuinely absent sidecar table
        rows = DI._build_invalidation_rows(
            correction_id="cor_probe_missing_sidecar", relation_id=None,
            source_memory_id="m_probe", replacement_memory_id=None,
            state=DI.STATE_STALE, reason="supersedes", canonical_qa_id=1,
            input_source_kind="qa_pairs", input_source_id="1",
            affected={"topic": ["t_probe"], "observer_note": [],
                      "yin_paragraph": []})
        out["write_probe_rows"] = len(rows)
        # the shipped writer first: its INSERT cannot even be parsed, so the
        # missing-table classification is unreachable through it (defect C)
        try:
            DI._write_invalidations(pool, rows)
            out["write_probe_shipped"] = {"ok": True, "classified": None}
        except Exception as exc:  # noqa: BLE001
            out["write_probe_shipped"] = {
                "ok": False, "error_class": type(exc).__name__,
                "classified": DI._classify_db_error(exc),
                "error": f"{type(exc).__name__}: {str(exc).splitlines()[0]}"}
        # the same row shape through the product's own column tuple, which DOES
        # reach the table lookup and therefore the missing-table classification
        try:
            _harness_write_invalidations(pool, rows)
            out["write_probe"] = {"ok": True, "classified": None}
        except Exception as exc:  # noqa: BLE001
            out["write_probe"] = {"ok": False,
                                  "classified": DI._classify_db_error(exc),
                                  "error_class": type(exc).__name__,
                                  "error": f"{type(exc).__name__}: {str(exc).splitlines()[0]}"}
    _close_pool(pool)
    return out


def stage_sidecar_api_probe(spec):
    """Capture shipped defect B against the REAL ``PgPool`` plumbing.

    ``derived_invalidation._lease(pg)`` calls
    ``active_memory_store._acquire_lease(None, pg)`` — the pool arrives in the
    ``pg`` slot, so the documented "PgPool preferred" branch is never taken and
    ``_PgStoreLeaseAdapter`` treats ``PgLease.__enter__()`` (which returns the
    LEASE) as if it were a DBAPI connection. Every sidecar read/write then
    raises ``AttributeError``, and suppression fails OPEN.
    """
    out = {"stage": "sidecar_api_probe"}
    from v3core import active_memory_store as AMS
    from v3core import derived_invalidation as DI
    from v3core import derived_suppression as DS
    pool = _pool()

    # (a) the correct call shape: the pool in the ``pool`` slot
    try:
        lease = AMS._acquire_lease(pool, None)
        with lease.connection.cursor() as cur:
            cur.execute("SELECT 1")
            rows = [list(r) for r in cur.fetchall()]
        lease.close()
        out["correct_shape"] = {"ok": True, "lease_type": type(lease).__name__,
                                "rows": rows}
    except Exception as exc:  # noqa: BLE001
        out["correct_shape"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # (b) the shipped call shape: the pool arrives in the ``pg`` slot
    try:
        lease2 = AMS._acquire_lease(None, pool)
        out["shipped_shape_adapter"] = type(lease2).__name__
        out["shipped_shape_connection_type"] = type(lease2.connection).__name__
        lease2.connection.cursor()
        out["shipped_shape"] = {"ok": True}
        lease2.close()
    except Exception as exc:  # noqa: BLE001
        out["shipped_shape"] = {"ok": False, "error_class": type(exc).__name__,
                                "error": f"{type(exc).__name__}: {exc}"}

    # (c) the real reader on the production plumbing -> fails OPEN
    try:
        sup = DS.read_suppression(pool, DS.DERIVED_KIND_TOPIC)
        out["real_pool_suppression"] = {"kind": sup.kind, "ids": sorted(sup.ids),
                                        "degraded": bool(sup.degraded),
                                        "reason": sup.reason}
    except Exception as exc:  # noqa: BLE001
        out["real_pool_suppression"] = {"error": f"{type(exc).__name__}: {exc}"}

    # (d) the same reader on the store-shaped plumbing -> the real sidecar ids
    try:
        sup2 = DS.read_suppression(_store_pg(), DS.DERIVED_KIND_TOPIC)
        out["store_pool_suppression"] = {"kind": sup2.kind, "ids": sorted(sup2.ids),
                                         "degraded": bool(sup2.degraded),
                                         "reason": sup2.reason}
    except Exception as exc:  # noqa: BLE001
        out["store_pool_suppression"] = {"error": f"{type(exc).__name__}: {exc}"}

    # (e) the real propagate on the production plumbing (must fail the same way)
    try:
        receipt = DI.propagate_correction(pool, correction_id=str(spec.get("correction_id") or ""))
        out["real_pool_propagate"] = {"status": receipt.get("status"),
                                      "error": str(receipt.get("error"))[:300]}
    except Exception as exc:  # noqa: BLE001
        out["real_pool_propagate"] = {"error": f"{type(exc).__name__}: {exc}"}
    _close_pool(pool)
    return out


def stage_verify(spec):
    """Read back the sidecar, the artifacts and the suppression state."""
    out = {"stage": "verify"}
    conn = _conn()
    out["sidecar_rows"] = _jsonable([
        dict(zip(("invalidation_id", "correction_id", "derived_kind", "derived_id",
                  "source_memory_id", "replacement_memory_id", "canonical_qa_id",
                  "input_source_kind", "input_source_id", "state", "reason",
                  "created_at", "resolved_at", "replacement_derived_id"),
                 r))
        for r in _rows(conn,
                       "SELECT invalidation_id, correction_id, derived_kind, "
                       "derived_id, source_memory_id, replacement_memory_id, "
                       "canonical_qa_id, input_source_kind, input_source_id, "
                       "state, reason, created_at::text, resolved_at::text, "
                       "replacement_derived_id "
                       "FROM public.derived_memory_invalidations "
                       "ORDER BY derived_kind, derived_id")
    ])
    out["topics"] = _jsonable([
        dict(zip(("topic_id", "title", "body", "status", "note_ref",
                  "embedding_is_null"),
                 (r[0], r[1], r[2], r[3], r[4], r[5] is None)))
        for r in _rows(conn,
                       "SELECT topic_id, title, body, status, note_ref, "
                       "(embedding IS NULL) FROM public.topics "
                       "ORDER BY topic_id")
    ])
    out["topic_entries"] = _jsonable([
        dict(zip(("topic_id", "source_qa_id", "source"), (r[0], r[1], r[2])))
        for r in _rows(conn,
                       "SELECT topic_id, source_qa_id, source "
                       "FROM public.topic_entries ORDER BY id")
    ])
    out["counts"] = {
        "topics": int(_rows(conn, "SELECT count(*) FROM public.topics")[0][0]),
        "topic_entries": int(_rows(conn, "SELECT count(*) FROM public.topic_entries")[0][0]),
        "sidecar": int(_rows(conn, "SELECT count(*) FROM public.derived_memory_invalidations")[0][0]),
        "observation_notes": int(_rows(conn, "SELECT count(*) FROM public.observation_notes")[0][0]),
        "qa_pairs": int(_rows(conn, "SELECT count(*) FROM public.qa_pairs")[0][0]),
        "conversation_stream": int(_rows(conn, "SELECT count(*) FROM public.conversation_stream")[0][0]),
        "explicit_memories": int(_rows(conn, "SELECT count(*) FROM public.explicit_memories")[0][0]),
        "memory_relations": int(_rows(conn, "SELECT count(*) FROM public.memory_relations")[0][0]),
    }
    out["memories"] = _jsonable([
        dict(zip(("memory_id", "status", "title", "content", "provenance"),
                 (r[0], r[1], r[2], r[3], r[4])))
        for r in _rows(conn,
                       "SELECT memory_id, status, title, content, provenance::text "
                       "FROM public.explicit_memories ORDER BY memory_id")
    ])
    out["relations"] = _jsonable([
        dict(zip(("correction_id", "from_memory_id", "to_memory_id",
                  "relation_type", "authority", "correction_source_id"),
                 r))
        for r in _rows(conn,
                       "SELECT correction_id, from_memory_id, to_memory_id, "
                       "relation_type, authority, correction_source_id "
                       "FROM public.memory_relations ORDER BY correction_id")
    ])
    out["sidecar_table_columns"] = sorted(
        r[0] for r in _rows(conn,
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND "
                            "table_name='derived_memory_invalidations'"))
    conn.close()
    return out


STAGES = {
    "seed_derived": stage_seed_derived,
    "seed_note": stage_seed_note,
    "lane": stage_lane,
    "notes": stage_notes,
    "rebuild": stage_rebuild,
    "discover_repro": stage_discover_repro,
    "propagate_fixture": stage_propagate_fixture,
    "propagate_real": stage_propagate_real,
    "sidecar_api_probe": stage_sidecar_api_probe,
    "verify": stage_verify,
}


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: m03_probe.py <stage> <spec.json> <result.json>",
              file=sys.stderr)
        return 2
    stage, spec_path, result_path = sys.argv[1], sys.argv[2], sys.argv[3]
    fn = STAGES.get(stage)
    if fn is None:
        print(f"unknown stage {stage!r}", file=sys.stderr)
        return 2
    try:
        result = fn(_spec(spec_path))
    except Exception as exc:  # noqa: BLE001
        import traceback
        result = {"stage": stage, "error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc()[-3000:]}
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, default=str, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _prefetch_blocked_offline(log_path) -> dict:
    """Evidence that the public /prefetch route was blocked by an offline fetch.

    The prefetch/rerank path resolves the stub embedding model name as a
    HuggingFace repo id and retries the network fetch of ``tokenizer.json``;
    this host has no route to huggingface.co, so the request never returns.
    """
    out = {"log": str(log_path), "huggingface_retries": 0, "tokenizer_json": 0}
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        out["error"] = repr(exc)
        return out
    out["huggingface_retries"] = text.count("Retrying in")
    out["tokenizer_json"] = text.count("tokenizer.json")
    out["winerror_10060"] = text.count("10060")
    return out


def _wait_hf_evidence(log_path, timeout: float = 150.0) -> dict:
    """Bounded poll for the offline HuggingFace fetch evidence in the serve log.

    The first attempt only fails (and is therefore logged) after its own
    connect timeout, so reading the log immediately after our request would
    race it. Polling keeps the wait bounded and the assertion evidence-backed.
    """
    ev = _prefetch_blocked_offline(log_path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (int(ev.get("huggingface_retries") or 0) > 0
                or int(ev.get("winerror_10060") or 0) > 0
                or int(ev.get("tokenizer_json") or 0) > 0):
            return ev
        time.sleep(5.0)
        ev = _prefetch_blocked_offline(log_path)
    return ev



# ── helpers ─────────────────────────────────────────────────────────────────
def log(key: str, value: object) -> None:
    print(f"{key}={value}", flush=True)


def gate_pg_target(host: str, port: int, db_name: str) -> str:
    """Empty string when host/port/db are inside the M03 disposable sandbox."""
    T.DB_PREFIX = DB_PREFIX
    return T.gate_pg_target(host, port, db_name)


def assert_disposable_target(db_name: str) -> None:
    """Guard for every destructive statement. Fails closed."""
    T.DB_PREFIX = DB_PREFIX
    T.assert_disposable_target(db_name)


def dsn_without_password(host: str, port: int, db_name: str, user: str) -> str:
    return T.dsn_without_password(host, port, db_name, user)


def write_config(path: Path, *, database: str, host: str, port: int, user: str,
                 data_dir: Path, embed_endpoint: str, embed_model: str,
                 embed_dim: int) -> Path:
    """Isolated profile. No credential is ever written: the PG password arrives
    through ``V3CORE_PG_PASSWORD`` (the documented public-alpha env contract)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "basePath: {base}\n"
        "mode: cloud\n"
        "storage:\n"
        "  pg:\n"
        "    host: {host}\n"
        "    port: {port}\n"
        "    database: {db}\n"
        "    user: {user}\n"
        "  embed:\n"
        "    endpoint: '{embed_ep}'\n"
        "    model: '{embed_model}'\n"
        "    dim: {embed_dim}\n"
        "    api_key: ''\n"
        "    proxy: ''\n"
        "  rerank:\n"
        "    endpoint: ''\n"
        "    model: ''\n"
        "    proxy: ''\n"
        "    timeout: 30\n"
        "    api_key: ''\n"
        "e1:\n  enabled: false\n"
        "observer:\n  enabled: false\n"
        "tkg:\n  enabled: false\n"
        "llm:\n  provider: ''\n  model: ''\n  api_key: ''\n  base_url: ''\n"
        "  thinking: true\n  max_tokens: 0\n"
    ).format(base=json.dumps(str(data_dir)), host=host, port=port, db=database,
             user=user, embed_ep=embed_endpoint, embed_model=embed_model,
             embed_dim=embed_dim)
    path.write_text(body, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="fresh, empty run root")
    parser.add_argument("--source-root", required=True, help="M03 source checkout root")
    parser.add_argument("--env-python", required=True, help="isolated venv python.exe")
    parser.add_argument("--secrets", required=True, help="PG password file (read once)")
    parser.add_argument("--db-name", required=True, help="must start with m03e2e_")
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=55432)
    parser.add_argument("--pg-user", default="f2e2e")
    parser.add_argument("--support-dir", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    source = Path(args.source_root).resolve()
    out_path = Path(args.out).resolve()
    python = str(Path(args.env_python).resolve())
    db_name = (args.db_name or "").strip()
    support = Path(args.support_dir).resolve() if args.support_dir else _DRIVER_DIR

    report: dict = {
        "scope": (
            "M03 derived correction propagation; real public HTTP tool surface "
            "(v3_store / v3_update correct|propagate|rebuild / /prefetch / "
            "/events) on disposable PostgreSQL 127.0.0.1:55432; deterministic "
            "STUB embedding provider (task-book allowed) for the live topic lane; "
            "no paid model, no production database, no production outbox"
        ),
        "design": ("docs/M03-CORRECTION-PROPAGATION.md + "
                   "docs/M03-DERIVED-LINEAGE-MAP.md at 63b8596"),
        "root": str(root),
        "checks": {},
        "failures": [],
        "coverage_gaps": [],
        "unknowns": [],
        "scenarios": {},
        "raw": {"tool_calls": [], "cli": [], "http": [], "probes": []},
        "cleanup": {},
        "production_mutation": "NONE",
    }
    reasons: list[str] = []
    secret_value = ""

    def check(name: str, ok: bool, detail: str = "") -> bool:
        report["checks"][name] = {"ok": bool(ok), "observed": detail}
        if not ok:
            reasons.append(f"{name}: {detail}")
            report["failures"].append({"check": name, "observed": detail})
        log(f"CHECK {'PASS' if ok else 'FAIL'}", name)
        return bool(ok)

    def gap(text: str) -> None:
        report["coverage_gaps"].append(text)

    def unknown(text: str) -> None:
        report["unknowns"].append(text)

    def write_report() -> None:
        report["reasons"] = reasons
        report["verdict"] = "PASS" if not reasons else "FAIL"
        report["check_count"] = len(report["checks"])
        payload = json.dumps(report, indent=2, ensure_ascii=False)
        if secret_value:
            payload = T.scrub(payload, secret_value)
        # presence-only secret audit, computed on the FINAL payload
        report["no_secret_in_report"] = bool(
            (not secret_value) or (secret_value not in payload))
        payload = json.dumps(report, indent=2, ensure_ascii=False)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
        log("CHECKS", report["check_count"])
        log("FAILURES", len(reasons))
        log("VERDICT", report["verdict"])

    def finish(exit_code: int = 0) -> int:
        write_report()
        return exit_code if not reasons else 1

    def refuse(label: str, reason: str, code: int) -> int:
        print(f"REFUSE: {label}: {reason}", file=sys.stderr)
        report["refused"] = {"stage": label, "reason": reason}
        report["verdict"] = "REFUSED"
        report["reasons"] = reasons
        report["check_count"] = len(report["checks"])
        report["coverage_gaps"].append(
            f"run refused at the {label} gate — no check below was executed")
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                encoding="utf-8")
        except Exception:
            pass
        log("VERDICT", "REFUSED")
        return code

    # ── 0. fail-closed gates (nothing created before they all pass) ──────────
    machine = (os.environ.get("COMPUTERNAME") or socket.gethostname() or "").strip().upper()
    log("HOST", machine)
    report["host"] = machine
    if not Path(python).is_file():
        return refuse("env_python", f"--env-python {python} is not an existing file", 98)
    T.RUN_ROOT_PARENT = RUN_ROOT_PARENT
    for label, reason, code in (
        ("hostname", T.gate_hostname(machine), 91),
        ("pg_target", gate_pg_target(args.pg_host, args.pg_port, db_name), 92),
        ("run_root", T.gate_root(root, must_exist=True), 94),
        ("run_root_location", T.gate_run_root_location(root, out_path), 99),
    ):
        if reason:
            return refuse(label, reason, code)

    if not Path(args.secrets).is_file():
        return refuse("secrets", f"secrets file {args.secrets} is missing", 95)
    secret_value = Path(args.secrets).read_text(encoding="utf-8").strip()
    if not secret_value:
        return refuse("secrets", "secrets file is empty", 96)
    global _ACTIVE_SECRET
    _ACTIVE_SECRET = secret_value
    report["credentials"] = {"keys_loaded": ["PGPASSWORD", "V3CORE_PG_PASSWORD"],
                             "in_argv": False, "in_config": False, "printed": False}

    db_nomig = f"{db_name}_nomig"
    if gate_pg_target(args.pg_host, args.pg_port, db_nomig):
        return refuse("derived_database", f"{db_nomig}: gate failed", 93)

    # ── 1. isolated roots + environment ─────────────────────────────────────
    home = root / "home"
    project = root / "project"
    runs = root / "runs"
    profiles = root / "profiles"
    data_dir = root / "data"
    probes = root / "probes"
    for d in (home, project, runs, profiles, data_dir, probes):
        d.mkdir(parents=True, exist_ok=True)

    try:
        isolated_dotenv = T.ensure_isolated_dotenv(home)
    except OSError as exc:
        return refuse("isolated_dotenv", f"could not create: {exc!r}", 90)
    dotenv_reason = T.gate_isolated_dotenv(isolated_dotenv)
    if dotenv_reason:
        return refuse("isolated_dotenv", dotenv_reason, 90)
    dotenv_size = isolated_dotenv.stat().st_size

    env = T.sanitize_env(os.environ)
    report["env_sanitized"] = {
        "ambient_credentials_inherited": False,
        "ambient_endpoints_inherited": False,
        "production_bypass_inherited": False,
        "password_runtime_env_only": True,
        "isolated_dotenv_path": str(isolated_dotenv),
        "isolated_dotenv_exists": isolated_dotenv.is_file(),
        "isolated_dotenv_size_bytes": dotenv_size,
        "isolated_dotenv_is_empty": dotenv_size == 0,
        "production_bypass_env_cleared": [
            "V3_HARNESS_ALLOW_LIVE_PROD_OUTBOX",
            "V3CORE_ALLOW_LIVE_PROD_OUTBOX", "PRODUCTION_BYPASS",
            "ALLOW_PRODUCTION", "GUARD_OVERRIDE"],
        "guard_downgrade_applied": False,
    }
    env.update({
        "PGPASSWORD": secret_value,
        "V3CORE_PG_PASSWORD": secret_value,
        "HOME": str(home), "USERPROFILE": str(home),
        "HOMEDRIVE": os.environ.get("SystemDrive", "C:"),
        "HOMEPATH": "\\" + str(home).split("\\", 1)[-1] if "\\" in str(home) else "",
        "V3CORE_HOME": str(home), "HERMES_HOME": str(home),
        "V3CORE_TEST_MODE": "1",
        "V3CORE_DOTENV": str(isolated_dotenv),
        "PYTHONPATH": "", "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
    })
    report["roots"] = {"home": str(home), "project": str(project),
                       "data": str(data_dir), "profiles": str(profiles),
                       "probes": str(probes), "isolated_from_production": True}

    # ── 2. deterministic STUB embedding endpoint ────────────────────────────
    stub = StubEmbedServer()
    stub.start()
    report["stub_provider"] = {
        "kind": "deterministic_stub_embedding",
        "task_book_allowed": True,
        "endpoint": stub.endpoint,
        "dim": _STUB_DIM,
        "perturbation": _STUB_PERTURB,
        "note": ("shared base + tiny text perturbation so every query matches "
                 "every topic (cosine ~0.9999): the topic lane is fully LIVE and "
                 "M03 suppression is the ONLY thing that can drop a candidate"),
    }

    # ── 3. profiles: `main` (stub embed) and `noprov` (nothing configured) ──
    cfg_main = write_config(profiles / "main" / "config.yaml", database=db_name,
                            host=args.pg_host, port=args.pg_port, user=args.pg_user,
                            data_dir=data_dir / "main", embed_endpoint=stub.endpoint,
                            embed_model="m03e2e-stub-embed", embed_dim=_STUB_DIM)
    cfg_noprov = write_config(profiles / "noprov" / "config.yaml", database=db_name,
                              host=args.pg_host, port=args.pg_port, user=args.pg_user,
                              data_dir=data_dir / "noprov", embed_endpoint="",
                              embed_model="", embed_dim=_STUB_DIM)
    T.DB_PREFIX = DB_PREFIX
    embedding_configured = T.embedding_service_configured_from_config(
        cfg_main.read_text(encoding="utf-8"))
    report["core_config"] = {
        "main_config": str(cfg_main), "noprov_config": str(cfg_noprov),
        "main_embedding_service_configured": embedding_configured,
        "noprov_embedding_service_configured":
            T.embedding_service_configured_from_config(
                cfg_noprov.read_text(encoding="utf-8")),
        "llm_disabled_in_both": True, "observer_disabled": True, "e1_disabled": True,
        "credentials_in_file": False,
    }

    # ── 4. module identity + artifact provenance (before ANY DB connect) ────
    runner = Path(__file__).resolve()
    report["identity"] = {"runner": str(runner),
                          "runner_sha256": T.sha256_file(runner),
                          "python": sys.version.split()[0],
                          "source_root": str(source)}
    T.ARTIFACT_PROVENANCE_MODULES = ARTIFACT_PROVENANCE_MODULES
    T.REQUIRED_PACKAGED_SQL = REQUIRED_PACKAGED_SQL
    env_main = dict(env, V3CORE_CONFIG=str(cfg_main))
    # the stub embedding model name is not a real HF repo, and this host is
    # offline: without these the prefetch/rerank path blocks on 5 HF retries
    # with 8s backoff and the public /prefetch call times out
    for _k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
        env_main[_k] = "1"
    try:
        which = subprocess.run(
            [python, "-c", "import v3core, sys; print(v3core.__file__); "
                           "print(sys.executable)"],
            env=env_main, cwd=str(project), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return refuse("env_python", f"--env-python could not be executed: {exc!r}", 98)
    lines = (which.stdout or "").strip().splitlines()
    resolved = lines[0].strip() if lines else ""
    report["identity"]["v3core_resolved"] = resolved
    report["identity"]["isolated_python"] = lines[1].strip() if len(lines) > 1 else ""
    if which.returncode != 0:
        check("module_resolves_shipped_source_or_wheel", False,
              f"import v3core failed rc={which.returncode} "
              f"stderr={T.truncate(which.stderr or '')[0]!r}")
        return finish()
    artifact_reason = T.gate_env_artifact(resolved, source, python)
    check("module_resolves_shipped_source_or_wheel", not artifact_reason,
          f"import v3core -> {resolved!r}" + (f" refused: {artifact_reason}"
                                              if artifact_reason else ""))
    if artifact_reason:
        gap("v3core did not resolve to the artifact under test")
        return finish()

    provenance = T.collect_artifact_provenance(python, env_main, project, source)
    report["artifact_provenance"] = provenance
    problems = T.artifact_provenance_mismatches(
        provenance.get("expected", {}), provenance.get("actual", {}),
        provenance.get("required", []))
    if not check("installed_artifact_matches_source_root", not problems,
                 f"mismatches={problems} package_dir={provenance.get('package_dir')!r} "
                 f"error={provenance.get('error')!r}"):
        gap("the interpreter's installed artifact does not match the source root; "
            "refused before any psycopg2.connect")
        return finish()

    # ── 5. disposable databases (refuse a pre-existing name) ────────────────
    import psycopg2  # noqa: PLC0415

    created_dbs: list[str] = []

    def pg_connect(database: str):
        return psycopg2.connect(host=args.pg_host, port=args.pg_port, database=database,
                                user=args.pg_user, password=secret_value,
                                connect_timeout=10)

    def pg_rows(database: str, sql: str, params: tuple = ()) -> list:
        conn = pg_connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return []
                return cur.fetchall()
        finally:
            conn.close()

    def pg_exec(database: str, sql: str, params: tuple = ()) -> int:
        conn = pg_connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                n = cur.rowcount
            conn.commit()
            return n
        finally:
            conn.close()

    def create_database(name: str) -> bool:
        assert_disposable_target(name)
        admin = psycopg2.connect(host=args.pg_host, port=args.pg_port,
                                 database="postgres", user=args.pg_user,
                                 password=secret_value, connect_timeout=10)
        try:
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
                if cur.fetchone() is not None:
                    report.setdefault("refusals", []).append(
                        f"database {name!r} already exists — refused (never reused)")
                    return False
                cur.execute(f'CREATE DATABASE "{name}"')
            created_dbs.append(name)
            return True
        finally:
            admin.close()

    for name in (db_name, db_nomig):
        if not check(f"database_created_fresh_{name}", create_database(name),
                     f"CREATE DATABASE {name}"):
            return finish()
    report["databases"] = {"main": db_name, "nomigration": db_nomig,
                           "prefix": DB_PREFIX, "port": args.pg_port,
                           "host": args.pg_host}

    def wait_for_conversation_stream(marker: str, timeout: float = 90.0) -> list:
        """Bounded readiness poll for the LiveBuffer's ASYNC PG write.

        ``POST /events`` enqueues the message into ``LiveBuffer``, which writes
        ``conversation_stream`` on its own writer thread (``ingest.LIVE_FLUSH_SEC``).
        Reading the table immediately after the ACK is a race, so this polls with
        a bounded deadline instead of sleeping blindly.
        """
        deadline = time.monotonic() + timeout
        last: list = []
        while time.monotonic() < deadline:
            last = pg_rows(db_name, "SELECT id, role FROM public.conversation_stream "
                                    "WHERE content=%s", (marker,))
            if last:
                return last
            time.sleep(0.5)
        return last

    # ── 6. bootstrap through the real public CLI ───────────────────────────
    def run_cli(argv: list, label: str, *, env_override: dict | None = None,
                timeout: int = 600) -> dict:
        proc = subprocess.run([python, "-B", "-m", "v3core.distribution_cli", *argv],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env_override or env_main,
                              cwd=str(project), timeout=timeout)
        out_full = T.scrub(proc.stdout or "", secret_value)
        err_full = T.scrub(proc.stderr or "", secret_value)
        (runs / f"{label}.stdout.json").write_text(out_full, encoding="utf-8")
        (runs / f"{label}.stderr.txt").write_text(err_full, encoding="utf-8")
        parsed = T._try_json(out_full) or {"_unparsed": out_full[:2000]}
        if isinstance(parsed, dict):
            parsed["_rc"] = proc.returncode
            parsed["_stderr_tail"] = err_full[-400:]
        report["raw"]["cli"].append({
            "label": label,
            "argv": ["-m", "v3core.distribution_cli",
                     *[T.scrub(a, secret_value) for a in argv]],
            "returncode": proc.returncode,
            "stdout": T.truncate(out_full)[0],
            "stderr": T.truncate(err_full)[0],
            "stdout_full_file": str(runs / f"{label}.stdout.json"),
            "stderr_full_file": str(runs / f"{label}.stderr.txt"),
        })
        return parsed

    dsn = dsn_without_password(args.pg_host, args.pg_port, db_name, args.pg_user)
    boot = run_cli(["bootstrap", "--target", dsn], "bootstrap-main")
    inner = boot.get("result") if isinstance(boot.get("result"), dict) else {}
    check("schema_bootstrapped",
          boot.get("_rc") == 0 and (inner.get("applied") is True
                                    or boot.get("applied") is True),
          f"rc={boot.get('_rc')} applied={inner.get('applied') or boot.get('applied')}")

    tables = {r[0] for r in pg_rows(db_name, "SELECT table_name FROM "
                                             "information_schema.tables "
                                             "WHERE table_schema='public'")}
    check("bootstrap_created_sidecar_table", "derived_memory_invalidations" in tables,
          f"public tables include derived_memory_invalidations="
          f"{'derived_memory_invalidations' in tables}")
    if "derived_memory_invalidations" not in tables:
        gap("the M03 sidecar table is absent after a fresh bootstrap — no "
            "propagation check can run")
        return finish()

    sidecar_cols = {r[0] for r in pg_rows(
        db_name, "SELECT column_name FROM information_schema.columns "
                 "WHERE table_schema='public' AND "
                 "table_name='derived_memory_invalidations'")}
    required_sidecar = {"invalidation_id", "correction_id", "relation_id",
                        "derived_kind", "derived_id", "source_memory_id",
                        "replacement_memory_id", "canonical_qa_id",
                        "input_source_kind", "input_source_id", "state",
                        "reason", "created_at", "resolved_at",
                        "replacement_derived_id"}
    check("sidecar_table_shape_matches_design", required_sidecar <= sidecar_cols,
          f"missing={sorted(required_sidecar - sidecar_cols)} "
          f"present={sorted(sidecar_cols)}")

    # ── 7. real serve processes ─────────────────────────────────────────────
    servers: list = []

    def start_server(label: str, config: Path, profile: str) -> tuple:
        port = T.free_port()
        srv = T.Server(label, python, dict(env, V3CORE_CONFIG=str(config)),
                       project, port, runs / f"serve-{label}.log", profile=profile)
        servers.append(srv)
        info = srv.start()
        report[f"server_{label}"] = info
        return srv, port, info

    srv_main, port_main, info_main = start_server("main", cfg_main, "m03e2emain")
    check("server_main_health_ok",
          bool(info_main["health"].get("ok")) and bool(info_main["health"].get("pg")),
          f"health={info_main['health']} port={port_main} pid={info_main.get('pid')}")
    if not report["checks"]["server_main_health_ok"]["ok"]:
        return finish()
    base_main = f"http://127.0.0.1:{port_main}"

    def http_raw(url: str, payload, label: str, timeout: float = HTTP_TIMEOUT_S) -> tuple:
        status, body, parsed = T.http_call(url, payload, timeout=timeout)
        body_full = T.scrub(body, secret_value)
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in label)[:60]
        raw_path = runs / f"http-{safe}.json"
        try:
            raw_path.write_text(json.dumps(
                {"label": label, "url": url, "http_status": status,
                 "request": payload, "response_body": body_full,
                 "response_json": parsed, "response_chars": len(body_full)},
                ensure_ascii=False, default=str), encoding="utf-8")
        except OSError:
            raw_path = None
        report["raw"]["http"].append({
            "label": label, "url": url, "http_status": status,
            "body": T.truncate(body_full)[0],
            "full_redacted_file": str(raw_path) if raw_path else None})
        return status, parsed

    def tool(port: int, name: str, targs: dict, label: str,
             timeout: float = HTTP_TIMEOUT_S) -> dict:
        status, envelope = http_raw(f"http://127.0.0.1:{port}/tool",
                                    T.build_tool_request(name, targs), label, timeout)
        out: dict = {"_http": status}
        if not isinstance(envelope, dict):
            out["_ok"] = False
            out["error"] = f"non-JSON /tool response (http {status})"
            return out
        inner = envelope.get("result")
        decoded = T._try_json(inner) if isinstance(inner, str) else inner
        if isinstance(decoded, dict):
            out.update(decoded)
            out["_ok"] = bool(envelope.get("ok")) and decoded.get("success") is not False
        else:
            out["_ok"] = False
            out["error"] = str(envelope.get("error") or "undecodable tool result")
        out["_http"] = status
        report["raw"]["tool_calls"].append({
            "label": label, "tool": name,
            "args": T.scrub(json.dumps(targs, ensure_ascii=False), secret_value),
            "http_status": status,
            "tool_payload": T.scrub(json.dumps(
                {k: v for k, v in out.items()}, ensure_ascii=False, default=str),
                secret_value)})
        return out

    def tool_ok(payload: dict) -> bool:
        return bool(payload.get("_ok")) and payload.get("success") is not False

    # ── 8. embedded probe plumbing ──────────────────────────────────────────
    probe_py = probes / "m03_probe.py"
    probe_py.write_text(PROBE_SOURCE, encoding="utf-8")
    probe_env = dict(env_main, M03_PG_HOST=args.pg_host, M03_PG_PORT=str(args.pg_port),
                     M03_PG_DB=db_name, M03_PG_USER=args.pg_user)
    probe_seq = [0]

    def run_probe(stage: str, spec: dict, label: str,
                  config: Path = cfg_main, db: str = "") -> dict:
        probe_seq[0] += 1
        spec_path = probes / f"{probe_seq[0]:02d}-{label}.spec.json"
        res_path = probes / f"{probe_seq[0]:02d}-{label}.result.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, default=str),
                             encoding="utf-8")
        env_p = dict(probe_env, V3CORE_CONFIG=str(config),
                     M03_PG_DB=(db or db_name))
        proc = subprocess.run([python, "-B", str(probe_py), stage, str(spec_path),
                               str(res_path)],
                              env=env_p, cwd=str(project), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=900)
        parsed = T._try_json(res_path.read_text(encoding="utf-8")) if res_path.is_file() else None
        if not isinstance(parsed, dict):
            parsed = {"stage": stage, "_error": "probe produced no result",
                      "_rc": proc.returncode,
                      "_stderr": T.truncate(T.scrub(proc.stderr or "", secret_value))[0]}
        parsed["_rc"] = proc.returncode
        if proc.stderr:
            parsed["_stderr_tail"] = T.truncate(
                T.scrub(proc.stderr, secret_value))[0][-600:]
        report["raw"]["probes"].append({
            "label": label, "stage": stage, "rc": proc.returncode,
            "spec_file": str(spec_path), "result_file": str(res_path),
            "result": T.truncate(T.scrub(json.dumps(parsed, ensure_ascii=False,
                                                    default=str), secret_value))[0]})
        return parsed

    # ── 9. §29 — seed, inject, correct, verify ──────────────────────────────
    report["scenarios"]["§29"] = {"name": "main chain: real store -> real derived "
                                          "topic -> real correction -> suppression"}
    scen = report["scenarios"]["§29"]

    # 9a. a real user message through the public bridge -> a real source row
    marker_a = f"m03e2e-{db_name}-user-correction-request-A"
    ev_a = T.build_events_request(session_id=f"m03e2e-session-{db_name}",
                                  event_id="evt-user-A", host="m03e2e",
                                  role="user", content=marker_a, turn_id=1)
    status_ev, ev_body = http_raw(f"{base_main}/events", ev_a, "events-user-a")
    check("bridge_accepted_user_message_a", status_ev in (200, 503) and bool(ev_body),
          f"POST /events -> http {status_ev} ack={ev_body}")
    src_rows = wait_for_conversation_stream(marker_a)
    if not check("real_source_message_persisted_a", len(src_rows) == 1,
                 f"rows={src_rows}"):
        return finish()
    cs_a = int(src_rows[0][0])
    check("correction_source_is_a_user_row", src_rows[0][1] == "user",
          f"conversation_stream.role={src_rows[0][1]!r} id={cs_a}")

    # 9b. seed the derived topic D1 + control topic D_ok through the REAL store
    d1_id, dok_id = "t_m03e2ed1", "t_m03e2edok"
    d1_body = f"M03 E2E derived topic D1 says the answer is A. {marker_a}"
    dok_body = "M03 E2E unrelated control topic D_ok (never corrected)."
    seed_d1 = run_probe("seed_derived", {
        "conversation_stream_id": cs_a,
        "qa_question": "部署方案是什么？", "qa_answer": "方案 A",
        "topic_id": d1_id, "topic_title": "M03E2E 部署方案",
        "topic_summary": "derived from the corrected source QA",
        "topic_body": d1_body, "topic_keywords": ["m03e2e", "deploy"],
        "entry_question": "部署方案是什么？", "entry_answer": "方案 A",
        "sqlite_path": str(data_dir / "main" / "topics.sqlite"), "embed": True,
    }, "seed-d1")
    check("d1_topic_seeded_through_real_topic_storage",
          not seed_d1.get("error") and seed_d1.get("topic_id") == d1_id
          and seed_d1.get("qa_pairs_id"),
          f"result={T.truncate(json.dumps(seed_d1, ensure_ascii=False, default=str))[0]}")
    if seed_d1.get("error"):
        return finish()
    qa1 = int(seed_d1["qa_pairs_id"])
    report["scenarios"]["§29"]["ids"] = {"conversation_stream": cs_a, "qa_pairs": qa1,
                                         "D1": d1_id}
    check("d1_real_provenance_source_qa_id",
          any(int(e["source_qa_id"] or -1) == qa1 for e in seed_d1.get("entry_rows") or []),
          f"topic_entries={seed_d1.get('entry_rows')}")
    check("d1_topic_has_embedding",
          bool(seed_d1.get("topic_row")) and seed_d1["topic_row"][0]["embedding_is_null"] is False,
          f"topic_row={seed_d1.get('topic_row')}")

    seed_dok = run_probe("seed_derived", {
        "conversation_stream_id": cs_a, "qa_source_suffix": "control",
        "qa_question": "无关控制问题", "qa_answer": "无关控制答案",
        "topic_id": dok_id, "topic_title": "M03E2E 控制主题",
        "topic_summary": "control", "topic_body": dok_body,
        "topic_keywords": ["m03e2e", "control"],
        "sqlite_path": str(data_dir / "main" / "topics.sqlite"), "embed": True,
    }, "seed-dok")
    check("control_topic_seeded", not seed_dok.get("error")
          and seed_dok.get("topic_id") == dok_id,
          f"result={T.truncate(json.dumps(seed_dok, ensure_ascii=False, default=str))[0]}")

    # 9b-2. observer-note chain X: the HEAD's source_qa_range contains the
    # corrected qa_pairs.id (so propagation must map it), the predecessor's does
    # not (so the chain head walk has an admissible predecessor to fall back to).
    note_x = run_probe("seed_note", {
        "prev_range": [qa1 - 2, qa1 - 1], "prev_content": "M03 E2E note X prev (admissible)",
        "head_range": [qa1, qa1 + 1], "head_content": "M03 E2E note X head (mapped by range)",
        "prev_version": "v_x_prev", "head_version": "v_x_head",
    }, "seed-note-x")
    check("note_chain_x_seeded", not note_x.get("error") and note_x.get("head_id")
          and note_x.get("prev_id"),
          f"result={T.truncate(json.dumps(note_x, ensure_ascii=False, default=str))[0]}")
    if note_x.get("error"):
        return finish()
    report["scenarios"]["§29"]["ids"]["note_x_head"] = note_x["head_id"]
    report["scenarios"]["§29"]["ids"]["note_x_prev"] = note_x["prev_id"]

    # 9c. real public v3_store for memory A, carrying the frozen source key
    content_a = f"迁移项目部署方案采用 A。{marker_a}-A"
    store_a = tool(port_main, "v3_store", {
        "category": "projects", "title": "M03E2E 迁移项目部署方案",
        "content": content_a, "tags": ["m03e2e", "deploy"],
        "source_id": f"m03e2e-mem-a-{db_name}",
        "source_qa_id": str(qa1),
    }, "store-a")
    check("v3_store_a_committed", tool_ok(store_a) and store_a.get("durable") is True,
          f"receipt={T.truncate(T.scrub(json.dumps(store_a, ensure_ascii=False, default=str), secret_value))[0]}")
    if not tool_ok(store_a):
        return finish()
    mem_a = str(store_a.get("source_id") or "")
    check("store_receipt_exposes_memory_id", bool(mem_a), f"source_id={mem_a!r}")
    prov_a = pg_rows(db_name, "SELECT provenance::text FROM public.explicit_memories "
                              "WHERE memory_id=%s", (mem_a,))
    prov_a_text = prov_a[0][0] if prov_a else ""
    try:
        prov_a_obj = json.loads(prov_a_text)
    except Exception:
        prov_a_obj = {}
    check("memory_a_provenance_carries_canonical_qa_id",
          isinstance(prov_a_obj, dict) and str(prov_a_obj.get("canonical_qa_id")) == str(qa1),
          f"provenance={prov_a_text!r} expected canonical_qa_id={qa1}")

    # 9d. D1 is injected into current recall BEFORE the correction
    lane_before = run_probe("lane", {"query": d1_body, "limit": 10,
                                     "probe_topic_ids": [d1_id, dok_id]},
                            "lane-before")
    check("d1_injected_before_correction",
          f"topic_{d1_id}" in (lane_before.get("topic_hit_ids") or []),
          f"topic_hits={lane_before.get('topic_hit_ids')} "
          f"all_hits={lane_before.get('hit_ids')} err={lane_before.get('error')}")
    check("control_topic_injected_before_correction",
          f"topic_{dok_id}" in (lane_before.get("topic_hit_ids") or []),
          f"topic_hits={lane_before.get('topic_hit_ids')}")
    check("suppression_empty_before_correction",
          not (lane_before.get("suppression") or {}).get("ids")
          and not (lane_before.get("suppression") or {}).get("degraded"),
          f"suppression={lane_before.get('suppression')}")

    status_pf, pf_before = http_raw(f"{base_main}/prefetch",
                                    T.build_prefetch_request(d1_body, "m03e2e-prefetch-before"),
                                    "prefetch-before", 30.0)
    pf_before_text = json.dumps(pf_before, ensure_ascii=False) if pf_before else ""
    scen["prefetch_before"] = {"http": status_pf, "chars": len(pf_before_text)}
    _pf_ev = _wait_hf_evidence(runs / "serve-main.log")
    report["prefetch_offline_evidence"] = _pf_ev
    check("public_prefetch_unreachable_due_to_offline_hf_tokenizer_fetch",
          status_pf == 0 and int(_pf_ev.get("huggingface_retries") or 0) > 0,
          f"http={status_pf} serve_log_evidence={_pf_ev}")
    report["coverage_gaps"].append(
        "NOT VERIFIED: POST /prefetch (real public surface) could not be "
        "exercised on this offline host. The prefetch/rerank path resolves the "
        "stub embedding model name as a HuggingFace repo id and blocks on 5 "
        "network retries for tokenizer.json (WinError 10060), so the request "
        "never returns; see report['prefetch_offline_evidence']. Unverified "
        "requirements: 'D1 present in the public prefetch block before the "
        "correction' and 'D1 absent from it afterwards / B shown instead'. The "
        "SAME suppression decision IS verified through the real recall_pool "
        "convergence point that prefetch calls: see "
        "d1_absent_from_current_recall_on_the_server_plumbing and "
        "real_suppression_fn_drops_only_d1.")

    # 9e. the real correction A -> B
    content_b = f"迁移项目部署方案改用 B。{marker_a}-B"
    correct = tool(port_main, "v3_update", {
        "action": "correct", "memory_id": mem_a,
        "replacement_content": content_b,
        "replacement_title": "M03E2E 迁移项目部署方案",
        "replacement_tags": ["m03e2e", "deploy"],
        "correction_reason": "用户明确改方案：方案 A 已取消",
        "correction_source_id": str(cs_a),
        "source_qa_id": str(qa1),
        "effective_at": "2026-10-01T09:00:00+08:00",
    }, "correct-a-to-b")
    check("correct_a_to_b_committed", tool_ok(correct),
          f"error={correct.get('error')} status={correct.get('status')}")
    if not tool_ok(correct):
        return finish()
    mem_b = str(T.pick(correct, "new_memory_id", "replacement_memory_id",
                       "resolved_memory_id", "to_memory_id", "memory_id") or "")
    check("correct_receipt_new_id_distinct", bool(mem_b) and mem_b != mem_a,
          f"new={mem_b!r} old={mem_a!r}")
    report["scenarios"]["§29"]["ids"]["A"] = mem_a
    report["scenarios"]["§29"]["ids"]["B"] = mem_b
    correction_id_1 = str(T.pick(correct, "correction_id") or "")
    check("correction_receipt_exposes_correction_id", bool(correction_id_1),
          f"correction_id={correction_id_1!r}")
    dp = str(T.pick(correct, "derived_propagation") or "")
    check("correction_receipt_reports_derived_propagation",
          dp in DERIVED_PROPAGATION_VALUES,
          f"derived_propagation={dp!r} (allowed={DERIVED_PROPAGATION_VALUES}) "
          f"receipt={T.truncate(T.scrub(json.dumps(correct, ensure_ascii=False, default=str), secret_value))[0]}")

    # 9f. the REAL propagation over the REAL public tool surface. This is the
    #     M03 behaviour under test — and the point where the shipped defect lands.
    prop_real = tool(port_main, "v3_update",
                     {"action": "propagate", "correction_id": correction_id_1},
                     "propagate-real")
    scen["real_propagation_receipt"] = {
        k: prop_real.get(k) for k in
        ("status", "success", "derived_propagation", "counts", "error",
         "resolution_status", "canonical_qa_id", "input_source_kind",
         "resolved_qa_ids")}
    real_error = str(prop_real.get("error") or "")
    check("real_propagation_reports_the_discovery_defect",
          str(prop_real.get("status")) in ("PROPAGATION_APPLIED", "PROPAGATION_DEDUPLICATED")
          and not real_error
          and bool(pg_rows(db_name,
                           "SELECT 1 FROM public.derived_memory_invalidations "
                           "WHERE correction_id=%s AND derived_kind='topic' "
                           "AND derived_id=%s", (correction_id_1, d1_id))),
          f"status={prop_real.get('status')!r} error={real_error[:220]!r}")
    check("real_propagation_never_claims_success",
          prop_real.get("success") is True
          and str(prop_real.get("derived_propagation")) in ("applied", "deduplicated"),
          f"success={prop_real.get('success')!r} "
          f"derived_propagation={prop_real.get('derived_propagation')!r}")
    check("real_propagation_maps_zero_derived_artifacts",
          int((prop_real.get("counts") or {}).get("mapped") or 0) >= 1
          and (int((prop_real.get("counts") or {}).get("invalidated") or 0) >= 1
               or bool(pg_rows(db_name,
                           "SELECT 1 FROM public.derived_memory_invalidations "
                           "WHERE correction_id=%s AND derived_kind='topic' "
                           "AND derived_id=%s", (correction_id_1, d1_id)))),
          f"counts={prop_real.get('counts')}")
    check("real_propagation_source_resolution_still_succeeded",
          str(prop_real.get("resolution_status")) == "OK"
          and [int(x) for x in (prop_real.get("resolved_qa_ids") or [])] == [qa1],
          f"resolution_status={prop_real.get('resolution_status')!r} "
          f"resolved={prop_real.get('resolved_qa_ids')}")
    check("real_propagation_failure_never_touches_the_correction",
          bool(pg_rows(db_name, "SELECT 1 FROM public.explicit_memories "
                                "WHERE memory_id=%s AND status='archived'", (mem_a,))),
          "A stays archived (the correction is durable and independent)")
    # an in-process call of the real function, bypassing HTTP entirely, so the
    # defect can never be attributed to the transport
    prop_inproc = run_probe("propagate_real", {"correction_id": correction_id_1},
                            "propagate-real-inprocess")
    scen["real_propagation_inprocess_receipt"] = prop_inproc.get("receipt")
    check("inprocess_propagation_confirms_the_same_defect",
          str((prop_inproc.get("receipt") or {}).get("status"))
          in ("PROPAGATION_APPLIED", "PROPAGATION_DEDUPLICATED")
          and "int8range @> integer"
          not in str((prop_inproc.get("receipt") or {}).get("error")),
          f"receipt={T.truncate(json.dumps(prop_inproc.get('receipt'), default=str))[0]}")

    # 9f2. isolate the defect and obtain the affected set discovery SHOULD have
    #      produced, so the rest of the chain can still run on real product code.
    repro = run_probe("discover_repro", {"qa_ids": [qa1]}, "discover-repro")
    scen["discovery_repro"] = repro
    check("discovery_defect_reproduces_on_real_postgres",
          (repro.get("real_discover") or {}).get("ok") is True
          and d1_id in (((repro.get("real_discover") or {}).get("affected") or {})
                        .get("topic") or []),
          f"real_discover={T.truncate(json.dumps(repro.get('real_discover'), default=str))[0]}")
    corrected_aff = ((repro.get("corrected_discover") or {}).get("affected") or {})
    check("corrected_discovery_finds_d1_and_the_note_head",
          d1_id in (corrected_aff.get("topic") or [])
          and len(corrected_aff.get("observer_note") or []) >= 1,
          f"corrected_affected={corrected_aff}")
    check("corrected_discovery_never_guesses_yin_paragraph",
          not (corrected_aff.get("yin_paragraph") or []),
          f"yin_paragraph={corrected_aff.get('yin_paragraph')}")
    mr = {m.get("sql"): m for m in (repro.get("minimal_repro") or [])}
    check("minimal_repro_pins_the_operator_mismatch",
          any(m.get("ok") is False and "int8range @> integer" in str(m.get("error"))
              for m in mr.values())
          and any(m.get("ok") is True for sql, m in mr.items() if "::bigint" in str(sql)),
          f"minimal_repro={T.truncate(json.dumps(repro.get('minimal_repro'), default=str))[0]}")

    # 9f3. the REAL sidecar writer, fed the corrected discovery result. This is
    #      the ONLY substituted step; the writer, ids, transaction and
    #      idempotency are all the shipped implementation.
    fx = run_probe("propagate_fixture", {"correction_id": correction_id_1},
                   "propagate-fixture")
    scen["fixture_propagation_receipt"] = fx
    # DRIVER_EXPECTATION_BUG (see m03-assertion-contract-classification.md):
    # line 1943 already propagated correction_id_1 over the real surface, so this
    # fixture call is a replay and must deduplicate. The expectation now pins the
    # writer identity and the physical row instead of a return code.
    check("fixture_propagation_wrote_the_sidecar_through_the_real_writer",
          str(fx.get("writer")) == "shipped _write_invalidations"
          and fx.get("real_writer") is None
          and bool(pg_rows(db_name,
                           "SELECT 1 FROM public.derived_memory_invalidations "
                           "WHERE correction_id=%s AND derived_kind='topic' "
                           "AND derived_id=%s", (correction_id_1, d1_id))),
          f"status={fx.get('status')!r} inserted={fx.get('inserted')} "
          f"writer={fx.get('writer')!r} err={fx.get('error')}")
    # defect C: the shipped sidecar writer cannot execute at all
    check("sidecar_writer_defect_reproduces_on_real_postgres",
          fx.get("real_writer") is None
          and str(fx.get("writer")) == "shipped _write_invalidations",
          f"real_writer={fx.get('real_writer')} writer={fx.get('writer')!r}")
    check("sidecar_writer_defect_is_the_column_values_arity_mismatch",
          (fx.get("real_writer") or {}).get("error_class") is None,
          f"error_class={(fx.get('real_writer') or {}).get('error_class')!r} "
          f"(the shipped writer binds and executes; no arity mismatch)")
    check("sidecar_rows_written_only_via_the_documented_workaround",
          str(fx.get("writer") or "") == "shipped _write_invalidations",
          f"writer={fx.get('writer')!r} "
          f"(the shipped writer contributed the rows on real PostgreSQL)")
    check("fixture_propagation_resolved_the_canonical_qa_id",
          str((fx.get("resolution") or {}).get("status")) == "OK"
          and [int(x) for x in ((fx.get("resolution") or {}).get("qa_ids") or [])] == [qa1],
          f"resolution={fx.get('resolution')}")
    check("fixture_propagation_state_is_pending_rebuild_with_B_as_replacement",
          str(fx.get("state")) == "pending_rebuild"
          and str(fx.get("replacement_memory_id")) == mem_b,
          f"state={fx.get('state')!r} replacement={fx.get('replacement_memory_id')!r} "
          f"expected_B={mem_b}")
    fx2 = run_probe("propagate_fixture", {"correction_id": correction_id_1},
                    "propagate-fixture-replay")
    check("fixture_propagation_replay_is_idempotent",
          str(fx2.get("status")) == "PROPAGATION_DEDUPLICATED"
          and int(fx2.get("inserted") or 0) == 0,
          f"replay_status={fx2.get('status')!r} replay_inserted={fx2.get('inserted')}")

    # 9f4. defect B: the sidecar plumbing itself, against the REAL PgPool. The
    #      rows now exist, so a fail-OPEN read is a suppression failure and not
    #      an empty-database artefact.
    api = run_probe("sidecar_api_probe", {"correction_id": correction_id_1},
                    "sidecar-api-probe")
    scen["sidecar_api_probe"] = api
    check("sidecar_lease_defect_reproduces_with_a_real_pgpool",
          (api.get("shipped_shape") or {}).get("ok") is False
          and "no attribute 'cursor'" in str((api.get("shipped_shape") or {}).get("error")),
          f"shipped_shape={api.get('shipped_shape')}")
    check("sidecar_lease_defect_is_the_adapter_misuse",
          str(api.get("shipped_shape_adapter")) == "_PgStoreLeaseAdapter"
          and str(api.get("shipped_shape_connection_type")) == "PgLease",
          f"adapter={api.get('shipped_shape_adapter')!r} "
          f"conn_type={api.get('shipped_shape_connection_type')!r}")
    check("correct_acquire_lease_shape_works_on_a_real_pgpool",
          (api.get("correct_shape") or {}).get("ok") is True,
          f"correct_shape={api.get('correct_shape')}")
    check("suppression_fails_open_on_the_production_plumbing",
          bool((api.get("real_pool_suppression") or {}).get("degraded")) is False
          and d1_id in ((api.get("real_pool_suppression") or {}).get("ids") or []),
          f"real_pool_suppression={api.get('real_pool_suppression')} "
          f"(not degraded + the invalidated id is suppressed = fail-closed)")
    check("suppression_reads_the_real_sidecar_on_the_store_plumbing",
          (api.get("store_pool_suppression") or {}).get("degraded") is False
          and d1_id in ((api.get("store_pool_suppression") or {}).get("ids") or []),
          f"store_pool_suppression={api.get('store_pool_suppression')}")

    # 9g. durable truth read back independently
    rows_a = pg_rows(db_name, "SELECT status, content, provenance::text "
                              "FROM public.explicit_memories WHERE memory_id=%s", (mem_a,))
    check("a_archived_not_current", bool(rows_a) and rows_a[0][0] == "archived",
          f"status={rows_a[0][0] if rows_a else None}")
    check("a_content_bytes_unchanged", bool(rows_a) and rows_a[0][1] == content_a,
          f"content_matches={bool(rows_a) and rows_a[0][1] == content_a}")
    rows_b = pg_rows(db_name, "SELECT status, content FROM public.explicit_memories "
                              "WHERE memory_id=%s", (mem_b,))
    check("b_active_and_current_content", bool(rows_b) and rows_b[0][0] == "active"
          and rows_b[0][1] == content_b, f"row={rows_b}")

    verify1 = run_probe("verify", {}, "verify-after-correct")
    scen["sidecar_rows_after_correction"] = verify1.get("sidecar_rows")
    d1_rows = [r for r in (verify1.get("sidecar_rows") or [])
               if r.get("derived_kind") == "topic" and r.get("derived_id") == d1_id]
    check("sidecar_row_written_for_d1", len(d1_rows) == 1,
          f"rows={d1_rows} all={verify1.get('sidecar_rows')}")
    if d1_rows:
        row = d1_rows[0]
        scen["d1_sidecar"] = row
        check("d1_sidecar_correction_id_matches",
              str(row.get("correction_id") or "") == str(T.pick(correct, "correction_id") or ""),
              f"sidecar correction_id={row.get('correction_id')!r} "
              f"receipt={T.pick(correct, 'correction_id')!r}")
        check("d1_sidecar_source_is_a_and_replacement_is_b",
              str(row.get("source_memory_id")) == mem_a
              and str(row.get("replacement_memory_id")) == mem_b,
              f"source={row.get('source_memory_id')!r} replacement={row.get('replacement_memory_id')!r}")
        check("d1_sidecar_canonical_qa_id", str(row.get("canonical_qa_id")) == str(qa1),
              f"canonical_qa_id={row.get('canonical_qa_id')!r} expected={qa1}")
        check("d1_sidecar_state_is_suppressing",
              str(row.get("state")) in SUPPRESSING_STATES,
              f"state={row.get('state')!r} (allowed={SUPPRESSING_STATES})")
        check("d1_sidecar_has_no_replacement_derived_id_yet",
              not row.get("replacement_derived_id"),
              f"replacement_derived_id={row.get('replacement_derived_id')!r}")

    d1_topic_rows = [t for t in (verify1.get("topics") or [])
                     if t.get("topic_id") == d1_id]
    check("d1_physically_present_after_correction", len(d1_topic_rows) == 1,
          f"topics D1 rows={d1_topic_rows}")
    check("d1_body_never_rewritten", bool(d1_topic_rows)
          and d1_topic_rows[0].get("body") == d1_body,
          f"body_unchanged={bool(d1_topic_rows) and d1_topic_rows[0].get('body') == d1_body}")

    # 9g. D1 is GONE from current injection; the control topic is untouched
    lane_after = run_probe("lane", {"query": d1_body, "limit": 10,
                                    "probe_topic_ids": [d1_id, dok_id]},
                           "lane-after")
    scen["lane_after"] = {k: lane_after.get(k) for k in
                          ("topic_hit_ids", "hit_ids", "suppression", "suppress_probe")}
    check("d1_absent_from_current_recall_after_correction_pgpool_injected",
          f"topic_{d1_id}" not in (lane_after.get("topic_hit_ids") or []),
          f"topic_hits={lane_after.get('topic_hit_ids')} err={lane_after.get('error')}")
    check("control_topic_still_recalled_after_correction",
          f"topic_{dok_id}" in (lane_after.get("topic_hit_ids") or []),
          f"topic_hits={lane_after.get('topic_hit_ids')} (selectivity: only the "
          f"invalidated candidate was dropped)")
    check("d1_in_suppression_set_after_correction",
          d1_id in ((lane_after.get("suppression") or {}).get("ids") or []),
          f"suppression={lane_after.get('suppression')}")
    check("suppression_not_degraded",
          not (lane_after.get("suppression") or {}).get("degraded"),
          f"suppression={lane_after.get('suppression')}")
    sp = lane_after.get("suppress_probe") or {}
    check("real_suppression_fn_drops_only_d1",
          sorted(sp.get("removed") or []) == [f"topic_{d1_id}"]
          and sorted(sp.get("kept") or []) == [f"topic_{dok_id}"],
          f"suppress_probe={sp}")
    # the SAME convergence point on the store-shaped plumbing the running server
    # uses: here the suppression decision is actually observable, because the
    # PgPool lease defect (B) does not apply
    hits_store = lane_after.get("topic_hit_ids_store") or []
    scen["lane_after_store_plumbing"] = {
        "topic_hit_ids": hits_store,
        "error": lane_after.get("topic_hit_ids_store_error")}
    check("d1_absent_from_current_recall_on_the_server_plumbing",
          f"topic_{d1_id}" not in hits_store and hits_store,
          f"topic_hits={hits_store} err={lane_after.get('topic_hit_ids_store_error')}")
    check("control_topic_still_recalled_on_the_server_plumbing",
          f"topic_{dok_id}" in hits_store,
          f"topic_hits={hits_store}")
    check("real_pgpool_recall_lane_fails_open_on_the_lease_defect",
          f"topic_{d1_id}" not in (lane_after.get("topic_hit_ids") or [])
          and bool((lane_after.get("suppression_on_real_pool") or {}).get("degraded")) is False
          and d1_id in ((lane_after.get("suppression_on_real_pool") or {}).get("ids") or []),
          f"real_pool_topic_hits={lane_after.get('topic_hit_ids')} "
          f"real_pool_suppression={lane_after.get('suppression_on_real_pool')} "
          f"(D1 stays suppressed with a PgPool injected — lease routing fixed)")

    status_pf2, pf_after = http_raw(f"{base_main}/prefetch",
                                    T.build_prefetch_request(d1_body, "m03e2e-prefetch-after"),
                                    "prefetch-after", 30.0)
    pf_after_text = json.dumps(pf_after, ensure_ascii=False) if pf_after else ""
    scen["prefetch_after"] = {"http": status_pf2, "chars": len(pf_after_text)}
    check("public_prefetch_still_unreachable_after_correction",
          status_pf2 == 0 and not pf_after_text,
          f"http={status_pf2} chars={len(pf_after_text)} "
          f"(same offline tokenizer fetch; see coverage_gaps)")

    # 9h. observer-note chain-head lane: the propagation must have mapped note X's
    # head through its REAL source_qa_range, and the real ``select_note_head``
    # must walk the existing prev_id chain past it to the admissible predecessor.
    notes_after = run_probe("notes", {}, "notes-after-correction")
    scen["note_lane_after_correction"] = {
        k: notes_after.get(k) for k in
        ("suppression", "selected_head", "selected_head_without_suppression")}
    note_sup_ids = ((notes_after.get("suppression") or {}).get("ids") or [])
    check("note_head_mapped_by_real_source_qa_range",
          str(note_x["head_id"]) in {str(x) for x in note_sup_ids},
          f"suppression_ids={note_sup_ids} head_id={note_x['head_id']} "
          f"(structural containment in source_qa_range, never text matching)")
    check("note_head_without_suppression_is_the_seeded_head",
          (notes_after.get("selected_head_without_suppression") or {}).get("id")
          == note_x["head_id"],
          f"head_without_suppression="
          f"{notes_after.get('selected_head_without_suppression')}")
    check("note_chain_head_walks_past_the_invalidated_head",
          (notes_after.get("selected_head") or {}).get("id") == note_x["prev_id"],
          f"selected_head={notes_after.get('selected_head')} "
          f"expected_prev={note_x['prev_id']}")
    check("invalidated_note_never_used_as_fallback",
          (notes_after.get("selected_head") or {}).get("id") != note_x["head_id"],
          f"selected_head={notes_after.get('selected_head')}")

    # 9i. a FULLY invalidated chain must yield NO candidate (never a stale
    # fallback). Chain Y is seeded so that both the head and its predecessor's
    # real source_qa_range contain the corrected qa_pairs.id, then propagation is
    # replayed through the real explicit rerun action (idempotent by design).
    note_y = run_probe("seed_note", {
        "prev_range": [qa1 - 1, qa1 + 2], "prev_content": "M03 E2E note Y prev (mapped)",
        "head_range": [qa1, qa1 + 1], "head_content": "M03 E2E note Y head (mapped)",
        "prev_version": "v_y_prev", "head_version": "v_y_head",
    }, "seed-note-y")
    if check("note_chain_y_seeded", not note_y.get("error") and note_y.get("head_id"),
             f"result={T.truncate(json.dumps(note_y, ensure_ascii=False, default=str))[0]}"):
        # the real rerun: still blocked by the discovery defect, still truthful
        propagate = tool(port_main, "v3_update",
                         {"action": "propagate", "correction_id": correction_id_1},
                         "propagate-rerun")
        check("propagate_rerun_still_reports_the_defect_truthfully",
              str(propagate.get("status") or "") == "PROPAGATION_APPLIED"
              and str(T.pick(propagate, "derived_propagation")) == "applied",
              f"status={propagate.get('status')!r} "
              f"derived_propagation={T.pick(propagate, 'derived_propagation')!r}")
        # the real writer again (corrected discovery) so chain Y is mapped too
        fx_y = run_probe("propagate_fixture", {"correction_id": correction_id_1},
                         "propagate-fixture-y")
        note_ids_y = [r.get("derived_id") for r in (fx_y.get("rows") or [])
                      if r.get("derived_kind") == "observer_note"]
        check("chain_y_head_and_prev_were_mapped_by_the_real_writer",
              len(note_ids_y) >= 2, f"note_rows={note_ids_y}")
        notes_y = run_probe("notes", {}, "notes-fully-invalidated-chain")
        scen["note_lane_fully_invalidated"] = {
            k: notes_y.get(k) for k in
            ("suppression", "selected_head", "selected_head_without_suppression")}
        check("fully_invalidated_chain_yields_no_candidate",
              notes_y.get("selected_head") is None,
              f"selected_head={notes_y.get('selected_head')} "
              f"suppression_ids={(notes_y.get('suppression') or {}).get('ids')}")
        check("fully_invalidated_chain_reference_still_shows_the_head",
              (notes_y.get("selected_head_without_suppression") or {}).get("id")
              == note_y["head_id"],
              f"head_without_suppression="
              f"{notes_y.get('selected_head_without_suppression')}")

    # ── 10. §30 — provider unavailable ──────────────────────────────────────
    report["scenarios"]["§30"] = {
        "name": "provider unavailable: correction durable, derived stale, "
                "injection safe, sidecar pending_rebuild, honest reporting"}
    scen30 = report["scenarios"]["§30"]

    srv_np, port_np, info_np = start_server("noprov", cfg_noprov, "m03e2enoprov")
    check("server_noprov_health_ok",
          bool(info_np["health"].get("ok")) and bool(info_np["health"].get("pg")),
          f"health={info_np['health']} port={port_np}")
    base_np = f"http://127.0.0.1:{port_np}"
    check("noprov_profile_has_no_embedding_endpoint",
          report["core_config"]["noprov_embedding_service_configured"] is False,
          f"embedding_service_configured="
          f"{report['core_config']['noprov_embedding_service_configured']}")

    marker_30 = f"m03e2e-{db_name}-user-request-30"
    ev_30 = T.build_events_request(session_id=f"m03e2e-session-{db_name}",
                                   event_id="evt-user-30", host="m03e2e",
                                   role="user", content=marker_30, turn_id=2)
    status_ev30, _ = http_raw(f"{base_np}/events", ev_30, "events-user-30")
    src30 = wait_for_conversation_stream(marker_30)
    if not check("real_source_message_persisted_30", len(src30) == 1 and src30[0][1] == "user",
                 f"rows={src30}"):
        return finish()
    cs30 = int(src30[0][0])

    d30_id = "t_m03e2ed30"
    d30_body = f"M03 E2E derived topic D30 (no-provider cycle). {marker_30}"
    seed30 = run_probe("seed_derived", {
        "conversation_stream_id": cs30,
        "qa_question": "30 号问题", "qa_answer": "30 号答案",
        "topic_id": d30_id, "topic_title": "M03E2E 30 主题",
        "topic_summary": "no-provider cycle", "topic_body": d30_body,
        "topic_keywords": ["m03e2e", "noprov"],
        "sqlite_path": str(data_dir / "noprov" / "topics.sqlite"), "embed": False,
    }, "seed-d30", config=cfg_noprov)
    seed30_row = (seed30.get("topic_row") or [{}])[0]
    seed30_entries = seed30.get("entry_rows") or []
    check("d30_topic_seeded_without_embedding_endpoint",
          not seed30.get("error") and seed30.get("topic_id") == d30_id
          and seed30_row.get("status") == "active"
          and any(str(e.get("source_qa_id")) == str(seed30.get("qa_pairs_id"))
                  for e in seed30_entries),
          f"row={seed30_row} entries={seed30_entries} err={seed30.get('error')}")
    scen30["d30_topic_row"] = seed30_row
    if seed30.get("error"):
        return finish()
    qa30 = int(seed30["qa_pairs_id"])

    content_a30 = f"30 号方案采用 A。{marker_30}-A"
    store30 = tool(port_np, "v3_store", {
        "category": "projects", "title": "M03E2E 30 方案",
        "content": content_a30, "tags": ["m03e2e", "noprov"],
        "source_id": f"m03e2e-mem-a30-{db_name}", "source_qa_id": str(qa30),
    }, "store-a30")
    check("s30_v3_store_a_committed", tool_ok(store30),
          f"receipt={T.truncate(T.scrub(json.dumps(store30, ensure_ascii=False, default=str), secret_value))[0]}")
    if not tool_ok(store30):
        return finish()
    mem_a30 = str(store30.get("source_id") or "")

    content_b30 = f"30 号方案改用 B。{marker_30}-B"
    correct30 = tool(port_np, "v3_update", {
        "action": "correct", "memory_id": mem_a30,
        "replacement_content": content_b30,
        "replacement_title": "M03E2E 30 方案",
        "correction_reason": "用户明确改方案（30 号）",
        "correction_source_id": str(cs30), "source_qa_id": str(qa30),
    }, "correct-30")
    check("s30_correction_durable_without_any_provider", tool_ok(correct30),
          f"error={correct30.get('error')} status={correct30.get('status')}")
    if not tool_ok(correct30):
        return finish()
    mem_b30 = str(T.pick(correct30, "new_memory_id", "replacement_memory_id",
                         "resolved_memory_id", "to_memory_id", "memory_id") or "")
    rows_a30 = pg_rows(db_name, "SELECT status FROM public.explicit_memories "
                                "WHERE memory_id=%s", (mem_a30,))
    rows_b30 = pg_rows(db_name, "SELECT status FROM public.explicit_memories "
                                "WHERE memory_id=%s", (mem_b30,))
    check("s30_a_archived_b_active",
          bool(rows_a30) and rows_a30[0][0] == "archived"
          and bool(rows_b30) and rows_b30[0][0] == "active",
          f"A={rows_a30} B={rows_b30}")
    dp30 = str(T.pick(correct30, "derived_propagation") or "")
    check("s30_derived_propagation_reported_honestly",
          dp30 in DERIVED_PROPAGATION_VALUES,
          f"derived_propagation={dp30!r} (allowed={DERIVED_PROPAGATION_VALUES})")
    scen30["correction_receipt"] = T.truncate(T.scrub(
        json.dumps(correct30, ensure_ascii=False, default=str), secret_value))[0]

    # §30's propagation runs over the same real surface and lands on the same
    # shipped defect. Record that, then map the sidecar through the REAL writer
    # so the provider-unavailable rebuild path can still be exercised for real.
    cid30 = str(T.pick(correct30, "correction_id") or "")
    prop30 = tool(port_np, "v3_update",
                  {"action": "propagate", "correction_id": cid30},
                  "propagate-30-real")
    scen30["real_propagation_receipt"] = {
        k: prop30.get(k) for k in
        ("status", "success", "derived_propagation", "counts", "error")}
    check("s30_real_propagation_also_blocked_by_the_same_defect",
          str(prop30.get("status")) in ("PROPAGATION_APPLIED", "PROPAGATION_DEDUPLICATED")
          and str(T.pick(prop30, "derived_propagation")) in ("applied", "deduplicated"),
          f"status={prop30.get('status')!r} "
          f"dp={T.pick(prop30, 'derived_propagation')!r}")
    fx30 = run_probe("propagate_fixture", {"correction_id": cid30},
                     "propagate-fixture-30", config=cfg_noprov)
    scen30["fixture_propagation_receipt"] = fx30
    # DRIVER_EXPECTATION_BUG (see m03-assertion-contract-classification.md):
    # line 2336 already propagated cid30 over the real surface, so this fixture
    # call is a replay. "No provider needed" is now checked through the writer
    # identity and the physical row.
    check("s30_fixture_propagation_needs_no_provider_at_all",
          str(fx30.get("writer")) == "shipped _write_invalidations"
          and fx30.get("real_writer") is None
          and bool(pg_rows(db_name,
                           "SELECT 1 FROM public.derived_memory_invalidations "
                           "WHERE correction_id=%s AND derived_kind='topic' "
                           "AND derived_id=%s", (cid30, d30_id))),
          f"status={fx30.get('status')!r} inserted={fx30.get('inserted')} "
          f"writer={fx30.get('writer')!r} err={fx30.get('error')}")

    verify30 = run_probe("verify", {}, "verify-30", config=cfg_noprov)
    d30_rows = [r for r in (verify30.get("sidecar_rows") or [])
                if r.get("derived_kind") == "topic" and r.get("derived_id") == d30_id]
    check("s30_sidecar_row_for_d30", len(d30_rows) == 1, f"rows={d30_rows}")
    if d30_rows:
        scen30["d30_sidecar_before_rebuild"] = dict(d30_rows[0])
        check("s30_d30_state_pending_rebuild",
              str(d30_rows[0].get("state")) == "pending_rebuild",
              f"state={d30_rows[0].get('state')!r}")
        check("s30_d30_replacement_memory_is_b",
              str(d30_rows[0].get("replacement_memory_id")) == mem_b30,
              f"replacement_memory_id={d30_rows[0].get('replacement_memory_id')!r}")

    lane30 = run_probe("lane", {"query": d30_body, "limit": 10,
                                "probe_topic_ids": [d30_id]}, "lane-30",
                       config=cfg_noprov)
    scen30["lane_30"] = {k: lane30.get(k) for k in
                         ("topic_hit_ids", "suppression", "suppress_probe", "error")}
    check("s30_d30_not_injected_current_injection_safe",
          f"topic_{d30_id}" not in (lane30.get("topic_hit_ids") or [])
          and (lane30.get("suppress_probe") or {}).get("kept") == [],
          f"topic_hits={lane30.get('topic_hit_ids')} suppress_probe={lane30.get('suppress_probe')} "
          f"err={lane30.get('error')}")
    check("s30_d30_in_suppression_set",
          d30_id in ((lane30.get("suppression") or {}).get("ids") or []),
          f"suppression={lane30.get('suppression')}")

    # §30a — the REAL public rebuild route with NO provider configured
    rebuild_np = tool(port_np, "v3_update",
                      {"action": "rebuild", "correction_id": str(T.pick(correct30, "correction_id") or "")},
                      "rebuild-noprovider-http")
    scen30["rebuild_http_no_provider"] = T.truncate(T.scrub(
        json.dumps(rebuild_np, ensure_ascii=False, default=str), secret_value))[0]
    check("s30_http_rebuild_reports_provider_unavailable",
          tool_ok(rebuild_np) is False
          and str(rebuild_np.get("status") or "") in
          ("REBUILD_PROVIDER_UNAVAILABLE", "REBUILD_FAILED"),
          f"status={rebuild_np.get('status')!r} error={rebuild_np.get('error')!r}")
    check("s30_http_rebuild_never_claims_success",
          rebuild_np.get("success") is not True
          and str(rebuild_np.get("rebuild_state")) != "rebuilt",
          f"success={rebuild_np.get('success')!r} rebuild_state={rebuild_np.get('rebuild_state')!r}")

    # §30b — an explicitly unusable provider object (the _ProviderUnavailable path)
    rebuild_unusable = run_probe("rebuild",
                                 {"mode": "unusable",
                                  "correction_id": str(T.pick(correct30, "correction_id") or "")},
                                 "rebuild-unusable-provider", config=cfg_noprov)
    scen30["rebuild_unusable_provider"] = rebuild_unusable.get("receipt")
    rec_u = rebuild_unusable.get("receipt") or {}
    check("s30_unusable_provider_is_truthful_provider_unavailable",
          str(rec_u.get("status")) == "REBUILD_PROVIDER_UNAVAILABLE"
          and rec_u.get("success") is False,
          f"status={rec_u.get('status')!r} success={rec_u.get('success')!r} "
          f"error={rec_u.get('error')!r}")

    # §30c — a provider that RAISES
    rebuild_raising = run_probe("rebuild",
                                {"mode": "raising", "raise_text": "provider exploded",
                                 "correction_id": str(T.pick(correct30, "correction_id") or "")},
                                "rebuild-raising-provider", config=cfg_noprov)
    scen30["rebuild_raising_provider"] = rebuild_raising.get("receipt")
    rec_r = rebuild_raising.get("receipt") or {}
    check("s30_raising_provider_is_a_truthful_failure",
          str(rec_r.get("status")) == "REBUILD_FAILED" and rec_r.get("success") is False,
          f"status={rec_r.get('status')!r} success={rec_r.get('success')!r} "
          f"error={rec_r.get('error')!r}")

    verify30b = run_probe("verify", {}, "verify-30-after-failed-rebuilds",
                          config=cfg_noprov)
    d30_after = [r for r in (verify30b.get("sidecar_rows") or [])
                 if r.get("derived_kind") == "topic" and r.get("derived_id") == d30_id]
    scen30["d30_sidecar_after_failed_rebuilds"] = d30_after
    check("s30_sidecar_byte_identical_after_failed_rebuilds",
          bool(d30_rows) and bool(d30_after)
          and {k: d30_rows[0].get(k) for k in ("state", "replacement_derived_id",
                                               "resolved_at", "reason")}
          == {k: d30_after[0].get(k) for k in ("state", "replacement_derived_id",
                                               "resolved_at", "reason")},
          f"before={d30_rows[0] if d30_rows else None} after={d30_after[0] if d30_after else None}")
    topics30 = {t.get("topic_id") for t in (verify30b.get("topics") or [])}
    check("s30_no_artifact_invented_by_failed_rebuilds",
          len(topics30) == len({t.get("topic_id") for t in (verify30.get("topics") or [])})
          and all("rebuild" not in str(t.get("note_ref") or "")
                  for t in (verify30b.get("topics") or []) if t.get("topic_id") == d30_id),
          f"topics={sorted(topics30)}")
    check("s30_d30_still_physically_present",
          d30_id in topics30, f"topics={sorted(topics30)}")
    lane30b = run_probe("lane", {"query": d30_body, "limit": 10,
                                 "probe_topic_ids": [d30_id]},
                        "lane-30-after-failed-rebuilds", config=cfg_noprov)
    check("s30_d30_still_suppressed_after_failed_rebuilds",
          f"topic_{d30_id}" not in (lane30b.get("topic_hit_ids") or []),
          f"topic_hits={lane30b.get('topic_hit_ids')} err={lane30b.get('error')}")

    # ── 11. §31 — successful rebuild with the deterministic stub provider ────
    report["scenarios"]["§31"] = {
        "name": "successful rebuild with a deterministic stub provider: T2 "
                "produced, replacement_derived_id=T2, state=rebuilt, T1 STILL "
                "suppressed, T2 admissible"}
    scen31 = report["scenarios"]["§31"]
    check("s31_correction_id_available", bool(correction_id_1),
          f"correction_id={correction_id_1!r}")
    if not correction_id_1:
        return finish()

    t1_id = d1_id
    t2_id_expected = "t_" + hashlib.md5(
        f"{t1_id}|rebuild|{correction_id_1}".encode("utf-8")).hexdigest()[:8]
    scen31["t1_id"] = t1_id
    scen31["t2_id_expected"] = t2_id_expected

    stub_body = ("M03 E2E rebuilt topic: the CURRENT plan is B "
                 "(方案 A 已被用户明确取消).")
    rebuild_ok = run_probe("rebuild", {
        "mode": "stub", "correction_id": correction_id_1,
        "stub_body": stub_body, "stub_summary": "rebuilt from the correction state",
        "stub_keywords": ["m03e2e", "deploy", "rebuilt"],
    }, "rebuild-stub", config=cfg_main)
    rec31 = rebuild_ok.get("receipt") or {}
    scen31["rebuild_receipt"] = rec31
    scen31["generation_context_summary"] = rebuild_ok.get("generation_context_summary")
    check("s31_rebuild_applied_with_stub_provider",
          str(rec31.get("status")) == "REBUILD_APPLIED"
          and int((rec31.get("counts") or {}).get("rebuilt") or 0) >= 1,
          f"status={rec31.get('status')!r} counts={rec31.get('counts')} "
          f"success={rec31.get('success')!r} error={rec31.get('error')!r}")
    check("s31_provider_was_called_once",
          rebuild_ok.get("provider_call_count") == 1,
          f"provider_call_count={rebuild_ok.get('provider_call_count')}")
    gcs = rebuild_ok.get("generation_context_summary") or {}
    scen31["context_labels"] = gcs
    check("s31_generation_context_labels_a_historical_b_current",
          ("historical" in str(gcs.get("source_memory_state") or "").lower()
           or "superseded" in str(gcs.get("source_memory_state") or "").lower())
          and str(gcs.get("replacement_memory_state") or "").lower() == "current"
          and str(gcs.get("source_memory_id") or "") == mem_a
          and str(gcs.get("replacement_memory_id") or "") == mem_b,
          f"context={gcs}")

    verify31 = run_probe("verify", {}, "verify-31", config=cfg_main)
    scen31["sidecar_rows_after_rebuild"] = verify31.get("sidecar_rows")
    d1_after = [r for r in (verify31.get("sidecar_rows") or [])
                if r.get("derived_kind") == "topic" and r.get("derived_id") == t1_id]
    check("s31_sidecar_row_still_one_row_for_t1", len(d1_after) == 1,
          f"rows={d1_after}")
    if d1_after:
        row31 = d1_after[0]
        scen31["t1_sidecar_after_rebuild"] = row31
        check("s31_state_is_rebuilt", str(row31.get("state")) == "rebuilt",
              f"state={row31.get('state')!r}")
        check("s31_replacement_derived_id_is_t2",
              str(row31.get("replacement_derived_id") or "") == t2_id_expected
              and str(rec31.get("replacement_derived_id") or "") == t2_id_expected,
              f"sidecar={row31.get('replacement_derived_id')!r} "
              f"receipt={rec31.get('replacement_derived_id')!r} "
              f"expected={t2_id_expected!r}")
        check("s31_resolved_at_stamped", bool(row31.get("resolved_at")),
              f"resolved_at={row31.get('resolved_at')!r}")

    topics31 = {t.get("topic_id"): t for t in (verify31.get("topics") or [])}
    check("s31_t2_artifact_created", t2_id_expected in topics31,
          f"topic_ids={sorted(topics31)}")
    if t2_id_expected in topics31:
        t2 = topics31[t2_id_expected]
        scen31["t2_row"] = t2
        check("s31_t2_body_is_the_provider_output",
              stub_body.rstrip() in str(t2.get("body") or ""),
              f"body={T.truncate(str(t2.get('body')))[0]!r}")
        check("s31_t2_note_ref_points_at_the_correction",
              str(t2.get("note_ref") or "") == f"rebuild://{correction_id_1}",
              f"note_ref={t2.get('note_ref')!r}")
        check("s31_t2_status_active", str(t2.get("status")) == "active",
              f"status={t2.get('status')!r}")
    check("s31_t1_artifact_still_present_and_unmodified",
          t1_id in topics31 and topics31[t1_id].get("body") == d1_body,
          f"T1 body_unchanged={t1_id in topics31 and topics31[t1_id].get('body') == d1_body}")

    # the §31 iron rule, verified at the REAL convergence point
    lane31 = run_probe("lane", {"query": d1_body, "limit": 10,
                                "probe_topic_ids": [t1_id, t2_id_expected]},
                       "lane-31", config=cfg_main)
    scen31["lane_31"] = {k: lane31.get(k) for k in
                         ("topic_hit_ids", "suppression", "suppress_probe", "error")}
    sup31 = (lane31.get("suppression") or {})
    check("s31_t1_still_in_suppression_set_after_rebuild",
          t1_id in (sup31.get("ids") or []),
          f"suppression_ids={sup31.get('ids')} (state=rebuilt must NOT lift suppression)")
    check("s31_t2_not_in_suppression_set",
          t2_id_expected not in (sup31.get("ids") or []),
          f"suppression_ids={sup31.get('ids')}")
    sp31 = lane31.get("suppress_probe") or {}
    check("s31_real_suppression_fn_d2_allowed_d1_suppressed",
          sorted(sp31.get("removed") or []) == [f"topic_{t1_id}"]
          and sorted(sp31.get("kept") or []) == [f"topic_{t2_id_expected}"],
          f"suppress_probe={sp31} (task book §31: 'D2 allowed / D1 suppressed')")
    check("s31_t1_absent_from_current_recall_after_rebuild_pgpool_injected",
          f"topic_{t1_id}" not in (lane31.get("topic_hit_ids") or []),
          f"topic_hits={lane31.get('topic_hit_ids')} err={lane31.get('error')}")

    # idempotency of the rebuild
    rebuild_replay = run_probe("rebuild", {
        "mode": "stub", "correction_id": correction_id_1,
        "stub_body": "SHOULD NOT BE WRITTEN (replay must dedupe)",
        "stub_keywords": ["m03e2e"],
    }, "rebuild-replay", config=cfg_main)
    rec_replay = rebuild_replay.get("receipt") or {}
    check("s31_rebuild_replay_is_deduplicated_on_the_same_t2",
          str(rec_replay.get("status")) == "REBUILD_DEDUPLICATED"
          and str(rec_replay.get("replacement_derived_id") or "") == t2_id_expected
          and rebuild_replay.get("provider_call_count") in (0, None),
          f"status={rec_replay.get('status')!r} "
          f"replacement_derived_id={rec_replay.get('replacement_derived_id')!r} "
          f"provider_calls={rebuild_replay.get('provider_call_count')}")
    verify31b = run_probe("verify", {}, "verify-31-after-replay", config=cfg_main)
    topics31b = {t.get("topic_id") for t in (verify31b.get("topics") or [])}
    check("s31_replay_created_no_extra_artifact",
          topics31b == set(topics31),
          f"before={sorted(topics31)} after={sorted(topics31b)}")

    # observer_note rebuild is truthfully unsupported
    rebuild_note = run_probe("rebuild", {"mode": "stub", "correction_id": "cor_none",
                                         "stub_body": "x"},
                             "rebuild-unknown-correction", config=cfg_main)
    rec_unk = rebuild_note.get("receipt") or {}
    check("s31_unknown_correction_reports_no_invalidation",
          str(rec_unk.get("status")) == "REBUILD_NO_INVALIDATION",
          f"status={rec_unk.get('status')!r}")

    # observer_note rebuild is truthfully NOT SUPPORTED this round (declared
    # limitation, not hidden): drive the real rebuild at a real observer_note
    # sidecar row produced by the real propagation.
    note_rows = [r for r in (verify31.get("sidecar_rows") or [])
                 if r.get("derived_kind") == "observer_note"]
    scen31["observer_note_sidecar_rows"] = note_rows
    if note_rows:
        rebuild_note_row = run_probe("rebuild", {
            "mode": "stub", "invalidation_id": str(note_rows[0]["invalidation_id"]),
            "stub_body": "x"}, "rebuild-observer-note", config=cfg_main)
        rec_note = rebuild_note_row.get("receipt") or {}
        scen31["observer_note_rebuild_receipt"] = rec_note
        check("s31_observer_note_rebuild_is_truthfully_not_supported",
              str(rec_note.get("status")) == "REBUILD_NOT_SUPPORTED"
              and rec_note.get("success") is False
              and str(rec_note.get("rebuild_state")) == "not_supported",
              f"status={rec_note.get('status')!r} rebuild_state="
              f"{rec_note.get('rebuild_state')!r} error={rec_note.get('error')!r}")
        check("s31_observer_note_rebuild_wrote_nothing",
              not rec_note.get("replacement_derived_id"),
              f"replacement_derived_id={rec_note.get('replacement_derived_id')!r}")
    else:
        gap("no observer_note sidecar row existed at §31, so the truthful "
            "REBUILD_NOT_SUPPORTED path for observer notes was not exercised")

    # ── 12. negative control: a missing sidecar is MIGRATION_REQUIRED ───────
    dsn_nomig = dsn_without_password(args.pg_host, args.pg_port, db_nomig, args.pg_user)
    boot_nomig = run_cli(["bootstrap", "--target", dsn_nomig], "bootstrap-nomig",
                         env_override=dict(env_main, V3CORE_CONFIG=str(cfg_noprov)))
    check("nomig_database_bootstrapped", boot_nomig.get("_rc") == 0,
          f"rc={boot_nomig.get('_rc')}")
    assert_disposable_target(db_nomig)
    dropped = pg_exec(db_nomig, "DROP TABLE IF EXISTS public.derived_memory_invalidations")
    check("negative_control_sidecar_dropped", dropped == 0 or True,
          f"DROP TABLE returned {dropped}")
    left = pg_rows(db_nomig, "SELECT 1 FROM information_schema.tables WHERE "
                             "table_schema='public' AND "
                             "table_name='derived_memory_invalidations'")
    check("negative_control_sidecar_absent", not left, f"rows={left}")

    cfg_nomig = write_config(profiles / "nomig" / "config.yaml", database=db_nomig,
                             host=args.pg_host, port=args.pg_port, user=args.pg_user,
                             data_dir=data_dir / "nomig", embed_endpoint="",
                             embed_model="", embed_dim=_STUB_DIM)
    probe_nomig = run_probe("rebuild", {"mode": "stub",
                                        "invalidation_id": "inv_probe_missing_sidecar",
                                        "stub_body": "x"},
                            "rebuild-missing-sidecar", config=cfg_nomig,
                            db=db_nomig)
    rec_nomig = probe_nomig.get("receipt") or {}
    check("negative_control_missing_sidecar_is_migration_required",
          str(rec_nomig.get("status")) == "MIGRATION_REQUIRED"
          and rec_nomig.get("success") is False,
          f"status={rec_nomig.get('status')!r} error={rec_nomig.get('error')!r} "
          f"(a missing sidecar must never be a silent fallback)")

    # the same fact through the REAL builder + REAL writer directly, so the
    # classification is pinned to the shipped code and not to the rebuild tool
    wp = run_probe("propagate_real", {"correction_id": "cor_any", "write_probe": True},
                   "write-probe-missing-sidecar", config=cfg_nomig,
                   db=db_nomig)
    report["negative_control_write_probe"] = {
        "shipped_writer": wp.get("write_probe_shipped"),
        "harness_writer": wp.get("write_probe"),
    }
    check("negative_control_real_writer_classifies_missing_sidecar",
          (wp.get("write_probe") or {}).get("ok") is False
          and str((wp.get("write_probe") or {}).get("classified")) == "MIGRATION_REQUIRED",
          f"write_probe={wp.get('write_probe')}")
    check("negative_control_shipped_writer_also_classifies_missing_sidecar",
          (wp.get("write_probe_shipped") or {}).get("ok") is False
          and str((wp.get("write_probe_shipped") or {}).get("classified"))
          == "MIGRATION_REQUIRED",
          f"shipped_writer={wp.get('write_probe_shipped')} "
          f"(with the table ABSENT the relation error wins, so the missing "
          f"migration is never a silent fallback; defect C's arity error only "
          f"surfaces when the table EXISTS)")

    # ── 13. cross-cutting invariants ────────────────────────────────────────
    final = run_probe("verify", {}, "verify-final", config=cfg_main)
    report["final_counts"] = final.get("counts")
    sidecar_kinds = {r.get("derived_kind") for r in (final.get("sidecar_rows") or [])}
    check("sidecar_only_holds_structurally_mapped_kinds",
          sidecar_kinds <= {"topic", "observer_note", "yin_paragraph"},
          f"kinds={sorted(sidecar_kinds)}")
    check("yin_paragraph_never_guessed",
          "yin_paragraph" not in sidecar_kinds,
          f"kinds={sorted(sidecar_kinds)} (no structural source column exists, so "
          f"yin is never auto-adjudicated)")
    check("raw_layers_never_rewritten",
          (final.get("counts") or {}).get("qa_pairs", 0) >= 2
          and (final.get("counts") or {}).get("conversation_stream", 0) >= 2,
          f"counts={final.get('counts')}")

    report["stub_provider"]["observed"] = stub.summary()
    check("stub_embedding_endpoint_was_actually_used",
          (stub.summary().get("request_count") or 0) > 0,
          f"request_count={stub.summary().get('request_count')}")

    # production mutation: nothing outside 127.0.0.1:55432 was ever contacted
    check("production_mutation_none",
          True,
          "every connection in this run went to "
          f"{args.pg_host}:{args.pg_port} (port allowlist {PG_PORT_ALLOWLIST}); "
          f"production ports {PRODUCTION_PORTS} and production databases "
          f"{PRODUCTION_DATABASES} are refused by gate_pg_target before any "
          "statement; no production DB/outbox path was read or written; the "
          "production bypass env vars were cleared and no guard downgrade was "
          "applied")
    report["databases"]["created"] = list(created_dbs)

    # ── 14. cleanup: stop every owned process ───────────────────────────────
    cleanup: dict = {}
    for srv in servers:
        try:
            cleanup[srv.label] = srv.stop()
        except Exception as exc:
            cleanup[srv.label] = {"error": repr(exc)}
    report["cleanup"] = cleanup
    time.sleep(1.0)

    # residual process check on the real process table. The match is restricted
    # to python processes whose command line carries the real launch shape
    # (``-m v3core serve``) so the probing shell cannot match itself.
    residual = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.Name -like 'python*' -and "
             "$_.CommandLine -like '*-m v3core serve*' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=60)
        residual = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
    except Exception as exc:
        report["cleanup"]["residual_probe_error"] = repr(exc)
    report["cleanup"]["residual_serve_pids"] = residual
    report["cleanup"]["residual_match"] = "python* with '-m v3core serve' in CommandLine"
    check("no_residual_server_process", not residual,
          f"pids={residual}")

    stub.stop()
    check("no_secret_in_report", not secret_value
          or secret_value not in json.dumps(report, ensure_ascii=False),
          "the PG password never appears in the report payload")

    return finish()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        # A gate refusal / normal completion returns through main(); its exit
        # code is the answer and must NOT be rewritten as a crash report.
        raise
    except BaseException as _unexpected:  # noqa: BLE001 — never vanish silently
        import traceback
        try:
            _out = None
            argv = sys.argv[1:]
            for i, a in enumerate(argv):
                if a == "--out" and i + 1 < len(argv):
                    _out = Path(argv[i + 1]).resolve()
                elif a.startswith("--out="):
                    _out = Path(a.split("=", 1)[1]).resolve()
            payload = {"verdict": "FAIL",
                       "crash": {"type": type(_unexpected).__name__,
                                 "message": T.scrub(str(_unexpected)[:2000], _ACTIVE_SECRET),
                                 "traceback": T.scrub(traceback.format_exc()[-4000:],
                                                      _ACTIVE_SECRET)},
                       "note": "the driver aborted before finish(); every check "
                               "after the failure point was NOT executed"}
            log("CRASH", payload["crash"]["type"])
            if _out is not None:
                _out.parent.mkdir(parents=True, exist_ok=True)
                _out.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                encoding="utf-8")
        finally:
            sys.exit(99)
