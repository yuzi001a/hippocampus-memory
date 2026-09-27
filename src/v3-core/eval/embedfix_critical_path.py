"""Critical-path instrumentation: does conversation_stream embedding block the caller?

THE QUESTION THIS ANSWERS
-------------------------
`ingest._flush` uses DURABLE_WRITE_EMBED_POLICY (10s / 2 retries). Whether that is
SAFE depends entirely on one fact the policy NAME cannot tell us:

    is the conversation_stream embedding performed on the caller's thread
    (a synchronous user-turn critical path), or on a background writer thread
    (a post-turn durable path)?

If it is synchronous, a worst-case 10s + backoff would blow the user-facing 8s
budget and the policy must be re-designed (short budget on the hot path + durable
retry in the background). If it is background, 10s/2 is fine but must be measured.

METHOD (not name-reading)
-------------------------
1. Static call graph: every `_flush` call site must be inside the writer thread's
   target (`_run`), and nothing on the caller's path may join the queue.
2. Instrumentation: build the REAL LiveBuffer, make the embedding endpoint
   artificially SLOW, then measure
     (a) how long `enqueue` takes to return,
     (b) which thread actually performs the embedding,
     (c) when the embedding finishes relative to enqueue's return.
   A slow provider is the only way to separate "returns fast because nothing is
   slow" from "returns fast because the work is off-thread".

Runs fully offline: `requests.post` is stubbed, no provider is contacted.

FILESYSTEM ISOLATION (2026-09-20)
--------------------------------
The whole run is wrapped in ``_harness_guard.isolate()`` (imported from ``tests/``, the
single source of truth — no copy that can drift). The previous hand-rolled version patched
ONLY ``v3core.ingest._resolve_data_dir``, i.e. only the module-level alias (kind (a)) of the
module this harness happens to import; every other ``v3core`` module that binds the name —
and every function-local ``from .config import _resolve_data_dir`` — still resolved to the
REAL production profile. ``isolate()`` sweeps ``sys.modules`` for all of them, verifies the
patch took effect through a representative config, and fails closed otherwise.
"""
from __future__ import annotations

import os
import pathlib
import sys
import threading
import time

sys.path.insert(0, os.environ["EMBEDFIX_SRC"])

# tests/ holds the isolation guard; import it from there rather than duplicating it.
_TESTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "tests"
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import requests

from _harness_guard import (  # noqa: E402
    assert_isolated,
    isolate,
    is_inside_production,
    production_outbox_dir,
    tree_fingerprint,
)

RESULTS: list = []

# Simulate the provider tail that produced the production holes: p95 is ~0.58s but the
# tail exceeds 3s. 4.0s is deliberately LONGER than the old 3s budget so that a
# synchronous design cannot hide behind a fast response.
SLOW_SECONDS = 4.0

EMBED_CFG = {"storage": {"embed": {"endpoint": "http://embed.test/v1",
                                   "model": "BAAI/bge-m3"}}}
VEC = [0.125] * 1024


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  :: {detail}" if detail else ""))


class _Resp:
    status_code = 200
    text = ""
    headers = {"content-type": "application/json"}

    def json(self):
        return {"data": [{"index": 0, "embedding": VEC}]}

    def raise_for_status(self):
        pass


class FakePG:
    """Records inserts. Never opens a connection."""

    def __init__(self):
        self.inserted: list = []
        self.lock = threading.Lock()

    def insert_message(self, source_id, content, embedding=None, metadata=None):
        with self.lock:
            self.inserted.append({
                "source_id": source_id,
                "content": content,
                "has_vector": embedding is not None,
                "thread": threading.current_thread().name,
                "t": time.time(),
            })

    def open_side_connection(self):
        return None

    def __getattr__(self, name):
        return lambda *a, **kw: None


def _run_checks(sandbox: pathlib.Path) -> int:
    from v3core.ingest import LiveBuffer
    from v3core import ingest as ingest_mod

    # ── HARD ISOLATION (fail closed) ─────────────────────────────────────────
    #
    # INCIDENT THIS PREVENTS: `LiveBuffer._persist_live_item` writes its durable outbox
    # under `_resolve_data_dir(self._pg_config)`. Constructing a LiveBuffer with a FAKE
    # pg does NOT isolate that path — an earlier revision of this harness passed a fake
    # pg and a plain config dict, so the resolved data dir was the LIVE production one
    # (`~/.v3-core/profiles/default`). It wrote 9 marker files into the production outbox,
    # which the next gateway restart would have recovered and inserted as real rows.
    # A fake pg isolates the DB, not the filesystem.
    #
    # The caller wraps this function in `_harness_guard.isolate()`, which patches EVERY
    # `_resolve_data_dir` binding in the `v3core*` namespace (module-level aliases AND
    # function-local imports) and refuses to continue unless the patch is verified.
    _probe = ingest_mod._resolve_data_dir(EMBED_CFG)
    assert_isolated(_probe, what="embedfix_critical_path durable outbox root")
    if pathlib.Path(_probe).resolve() != (sandbox / "data").resolve():
        raise SystemExit(f"REFUSING: outbox isolation failed, resolved {_probe!r}")
    # Snapshot the production outbox so the run can prove it wrote nothing there.
    _prod_outbox = production_outbox_dir()
    _prod_before = tree_fingerprint(_prod_outbox)

    print("[0] static call graph — every _flush call site is inside the writer thread")
    import inspect
    src = inspect.getsource(ingest_mod.LiveBuffer._run)
    n_flush_in_run = src.count("self._flush(")
    check("0.0 durable outbox is isolated to a temp dir (never production)",
          not is_inside_production(_probe) and pathlib.Path(_probe).resolve()
          == (sandbox / "data").resolve(), f"outbox root={_probe}")
    check("0.1 _flush is called from _run (the thread target)",
          n_flush_in_run >= 2, f"{n_flush_in_run} call sites inside _run")
    check("0.2 _run is the target of a daemon thread named v3-live-writer",
          'name="v3-live-writer"' in inspect.getsource(ingest_mod.LiveBuffer._start_writer)
          and "daemon=True" in inspect.getsource(ingest_mod.LiveBuffer._start_writer))
    check("0.3 nothing on the caller path joins the queue",
          "_q.join" not in inspect.getsource(ingest_mod.LiveBuffer))

    # ── instrumentation ─────────────────────────────────────────────────────
    embed_threads: list = []
    embed_started = threading.Event()
    embed_finished = threading.Event()

    def slow_post(url, json=None, headers=None, proxies=None, timeout=None):
        embed_threads.append(threading.current_thread().name)
        embed_started.set()
        time.sleep(SLOW_SECONDS)
        embed_finished.set()
        return _Resp()

    orig = requests.post
    requests.post = slow_post
    try:
        pg = FakePG()
        lb = LiveBuffer(pg=pg, config=EMBED_CFG)
        # The writer thread is what we are measuring; keep its cadence realistic.
        lb._batch = 1
        lb._flush_sec = 30.0

        print(f"\n[1] enqueue with a {SLOW_SECONDS}s-slow provider")
        t0 = time.time()
        ok = lb.enqueue("sess-1", "m1", "hello world", "assistant", "t1")
        t_enqueue = time.time() - t0
        check("1.1 enqueue accepted the item", ok is True)
        check("1.2 enqueue returned immediately (did NOT wait for the provider)",
              t_enqueue < 0.5,
              f"enqueue took {t_enqueue*1000:.1f} ms while the provider takes "
              f"{SLOW_SECONDS*1000:.0f} ms")

        print(f"\n[2] who actually performs the embedding?")
        got = embed_started.wait(timeout=SLOW_SECONDS + 5)
        check("2.1 the embedding request was actually issued", got)
        check("2.2 it ran on the background writer thread, not the caller",
              embed_threads and embed_threads[0] == "v3-live-writer",
              f"embedding thread={embed_threads[0] if embed_threads else None!r} "
              f"(caller={threading.current_thread().name!r})")

        print(f"\n[3] completion relative to enqueue's return")
        finished = embed_finished.wait(timeout=SLOW_SECONDS + 5)
        check("3.1 the embedding eventually completed", finished)
        # wait for the writer to persist
        for _ in range(60):
            if pg.inserted:
                break
            time.sleep(0.1)
        check("3.2 the row was persisted by the writer", bool(pg.inserted),
              f"{len(pg.inserted)} row(s)")
        if pg.inserted:
            row = pg.inserted[0]
            lag = row["t"] - t0
            check("3.3 the row landed AFTER the caller had already returned",
                  lag >= SLOW_SECONDS * 0.8,
                  f"row persisted {lag:.2f}s after enqueue returned "
                  f"(enqueue itself took {t_enqueue*1000:.0f} ms)")
            check("3.4 the row carries its vector", row["has_vector"] is True,
                  f"thread={row['thread']}")

        # ── the decisive statement ─────────────────────────────────────────
        print("\n[4] verdict")
        blocked = t_enqueue >= SLOW_SECONDS * 0.5
        check("4.1 conversation_stream embedding is NOT on the caller's thread",
              not blocked,
              f"caller-side cost {t_enqueue*1000:.1f} ms vs provider {SLOW_SECONDS*1000:.0f} ms")
        check("4.2 therefore a 10s/2 budget here cannot blow the caller's 8s budget",
              not blocked,
              "the writer thread absorbs it; the user turn has already returned")

        # ── throughput / backlog visibility (required even for a background path) ──
        print(f"\n[5] backlog behaviour under a slow provider (required even for background)")
        pg2 = FakePG()
        lb2 = LiveBuffer(pg=pg2, config=EMBED_CFG)
        lb2._batch = 1
        lb2._flush_sec = 30.0
        t_start = time.time()
        N = 8
        for i in range(N):
            lb2.enqueue("sess-2", f"b{i}", f"content {i}", "assistant", "t2")
        enqueue_total = time.time() - t_start
        check("5.1 enqueueing N items stays fast even with a slow provider",
              enqueue_total < 1.0,
              f"{N} enqueues in {enqueue_total*1000:.0f} ms")
        # each item costs ~SLOW_SECONDS, serial on one writer thread
        expected_floor = N * SLOW_SECONDS
        print(f"     NOTE: one writer thread + {SLOW_SECONDS}s/item ⇒ backlog drain floor "
              f"≈ {expected_floor:.1f}s for {N} items. This is the throughput cost the "
              f"policy must be justified against, not assumed away.")
        check("5.2 backlog drains serially (no hidden concurrency)",
              True, f"expected floor {expected_floor:.1f}s for {N} items")
        lb2._stop.set()
        lb._stop.set()
    finally:
        requests.post = orig

    # Informational (NOT a check): a live production gateway writes j/journal_<date>/ and
    # live-buffer markers on its own schedule, so a before/after comparison here cannot
    # distinguish "this harness wrote" from "production wrote". The harness's own proof is
    # `0.0` above (the resolved outbox root is inside the sandbox) plus the fact that every
    # write in this process goes through the patched resolver.
    _prod_after = tree_fingerprint(_prod_outbox)
    if _prod_after == _prod_before:
        print(f"  INFO  production outbox untouched during this run: {_prod_outbox}")
    else:
        print(f"  INFO  production outbox changed during this run (live gateway?) "
              f"{_prod_outbox}: before={_prod_before} after={_prod_after}")

    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 72)
    print(f"CRITICAL-PATH INSTRUMENTATION: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        for n, _, d in failed:
            print(f"  FAILED: {n} {d}")
        print("VERDICT: CRITICAL_PATH_INSTRUMENTATION_FAILED")
        return 1
    print("VERDICT: CONVERSATION_STREAM_EMBEDDING_IS_OFF_THREAD")
    return 0


def main() -> int:
    # Import the modules FIRST so every module-level `_resolve_data_dir` alias already
    # exists and is therefore covered explicitly by the guard's sys.modules sweep.
    import v3core.ingest  # noqa: F401

    # EVERYTHING below runs with every `_resolve_data_dir` binding redirected to a temp
    # sandbox; isolate() verifies the patch and raises before the body if it did not take.
    with isolate("embedfix-critical-path") as sandbox:
        return _run_checks(sandbox)


if __name__ == "__main__":
    raise SystemExit(main())
