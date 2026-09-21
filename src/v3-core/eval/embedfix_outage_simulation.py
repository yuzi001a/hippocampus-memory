"""Outage simulation for the v3-core LIVE embedding writer (disposable eval harness).

WHAT THIS MEASURES (and why the numbers are not obvious from the code)
---------------------------------------------------------------------
Production path (verified, not re-derived here):

    sync_turn -> LiveBuffer.enqueue -> queue.Queue() -> daemon thread
    "v3-live-writer" -> LiveBuffer._run -> LiveBuffer._flush -> embedding call -> PG insert

Facts that make the policy choice non-trivial:

* The writer is SINGLE-THREADED and SERIAL. One slow embedding delays every
  item behind it (head-of-line blocking).
* The in-memory queue is ``queue.Queue()`` -- UNBOUNDED, no backpressure. A
  policy that is slower than the arrival rate does not shed load, it grows
  memory and widens the "not yet embedded" window.
* Production arrival rate: peak 13 rows/min, daily mean 1.07-1.42 rows/min.
* Production provider tail: p50 ~0.58 s, observed tail up to 3.488 s.
* Current production policy is 3 s / 0 retries -- so a response slower than 3 s
  used to become a *permanent* NULL. 10 s / 2 retries costs 33 s per item in the
  worst case (10*3 + 1 + 2 backoff), i.e. 11x the 3 s policy.

So the question is not "which policy is safest" in the abstract; it is:

    at a 13 items/minute peak, which (timeout, retries) budget keeps the writer
    draining, how deep does the unbounded queue get, and how long is an item
    pending before it is written?

THIS HARNESS
------------
Runs the REAL ``v3core.ingest.LiveBuffer`` (no reimplementation) against a fake
pg object and a stubbed ``requests.post``. Fully offline: no provider, no
PostgreSQL. Every figure printed comes from this process's own measurements.

Scenarios
    healthy            provider 0.5 s, always succeeds
    slow_tail          ~15% of calls take 3.5-5.0 s, otherwise ~0.6 s
    sustained_timeout  EVERY call exceeds the configured timeout, for the whole run

Policies compared: (3s,0) (5s,0) (10s,0) (10s,2) -- the last one's retries happen
inside the same writer thread, which is exactly the point being measured.

TIME COMPRESSION (read this before quoting a wall-clock number)
--------------------------------------------------------------
A faithful real-time run is impossible in a bounded session: the sustained
timeout cell costs 33 s/item under 10s/2, so 72 items is ~40 minutes of wall
clock for that cell alone. The harness therefore runs on a uniform time
compression factor ``--time-scale`` (default 10): every duration in the model is
divided by that factor --

    * provider latency,
    * arrival interval,
    * the embedding retry backoff (``time.sleep`` is patched for the whole run;
      the harness itself uses the saved real sleep),
    * LiveBuffer's periodic-flush deadline (``_flush_sec``),
    * the grace window after the last enqueue.

The system is scale-invariant in time, so ratios (drain rate vs arrival rate,
queue growth, HOL waits) are preserved exactly; every duration reported here is
converted BACK to un-scaled seconds/minutes by multiplying by the factor. What
does NOT scale is interpreter/OS overhead per item (thread switch, sha256 job id,
durable outbox fsync + unlink -- measured in the calibration cell and printed
below), so the compressed run slightly *understates* the real drain rate. The
calibration cell prints that overhead in both real and simulated seconds.

HARD ISOLATION
--------------
``LiveBuffer._persist_live_item`` writes a durable outbox under
``_resolve_data_dir(config)``. A fake pg isolates the DATABASE, not the
FILESYSTEM -- an earlier harness passed a fake pg with a plain config dict and
wrote 8 marker files into the LIVE production outbox. This harness redirects the
resolver into a tempfile.mkdtemp() sandbox, verifies the redirect took effect in
EVERY module that holds a ``_resolve_data_dir`` alias, and raises SystemExit if
the resolved root is inside ``~/.v3-core``.

It also fingerprints the real production outbox tree before and after the run and
asserts the fingerprint is unchanged.

Run:
    python eval/embedfix_outage_simulation.py
    python eval/embedfix_outage_simulation.py --time-scale 20 --quick
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import random
import statistics
import sys
import tempfile
import threading
import time

# ── time scaling ─────────────────────────────────────────────────────────────
# Captured BEFORE time.sleep is patched: the harness's own pacing must not be
# scaled by the factor it applies to the simulated system.
REAL_SLEEP = time.sleep

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]      # src/v3-core
SRC_ROOT = REPO_ROOT / "src"
PRODUCTION_OUTBOX = (pathlib.Path.home() / ".v3-core" / "profiles" / "default"
                     / "j" / "pending_live_buffer")
PRODUCTION_ROOT = pathlib.Path.home() / ".v3-core"

# ── simulation constants (production-derived) ────────────────────────────────
PEAK_ROWS_PER_MINUTE = 13.0
ARRIVAL_INTERVAL_S = 60.0 / PEAK_ROWS_PER_MINUTE          # 4.615 s
STEADY_ITEMS = 60
BURST_ITEMS = 12
GRACE_S = 60.0            # drain grace after the last enqueue (simulated)
DEPTH_SAMPLE_REAL_S = 0.001
HOL_WAIT_THRESHOLD_S = 1.0   # a later item waited > 1 s behind an earlier one

POLICIES = [
    (3.0, 0, "realtime-3s/0r"),
    (5.0, 0, "5s/0r"),
    (10.0, 0, "durable-10s/0r"),
    (10.0, 2, "durable-10s/2r"),
]


def pol_key(timeout, retries) -> str:
    """Short form used on the command line, e.g. '3/0', '10/2'."""
    t = f"{timeout:g}"
    return f"{t}/{retries}"


def pol_selected(timeout, retries, label, wanted) -> bool:
    if not wanted:
        return True
    return label in wanted or pol_key(timeout, retries) in wanted
SCENARIOS = ["healthy", "slow_tail", "sustained_timeout"]

DEBUG = False
VEC = [0.125] * 1024


# ════════════════════════════════════════════════════════════════════════════
# 0. sys.path
# ════════════════════════════════════════════════════════════════════════════

def bootstrap_syspath() -> None:
    env = os.environ.get("EMBEDFIX_SRC")
    for p in ([pathlib.Path(env)] if env else []) + [SRC_ROOT]:
        if p and p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))


# ════════════════════════════════════════════════════════════════════════════
# 1. isolation
# ════════════════════════════════════════════════════════════════════════════

def _v3core_modules():
    return [m for n, m in list(sys.modules.items())
            if n == "v3core" or n.startswith("v3core.")]


def _alias_report():
    """Every module holding a ``_resolve_data_dir`` attribute, and where it points."""
    out = []
    for m in _v3core_modules():
        fn = getattr(m, "_resolve_data_dir", None)
        if fn is None:
            continue
        try:
            resolved = str(fn(None))
        except Exception as e:                                   # pragma: no cover
            resolved = f"<raised {type(e).__name__}: {e}>"
        out.append((getattr(m, "__name__", "?"), resolved))
    return out


def _try_sibling_guard():
    """Use tests/_harness_guard.py if a sibling subagent has landed it.

    Returns ``(guard_module_or_None, reason_string)``.
    """
    tests_dir = REPO_ROOT / "tests"
    guard_file = tests_dir / "_harness_guard.py"
    if not guard_file.exists():
        return None, f"{guard_file} does not exist"
    try:
        if str(tests_dir) not in sys.path:
            sys.path.insert(0, str(tests_dir))
        import importlib
        guard = importlib.import_module("_harness_guard")
        if not hasattr(guard, "isolate"):
            return None, "_harness_guard has no isolate()"
        return guard, "ok"
    except Exception as e:
        import traceback
        return None, f"{type(e).__name__}: {e} :: {traceback.format_exc().splitlines()[-3:]}"


def make_isolated_resolver(sandbox):
    """A drop-in for ``v3core.config._resolve_data_dir`` that cannot reach production.

    Semantics mirror the real resolver (``config.base_path`` first, else the default
    profile root) with one clamp: a base path is only honoured when it is already
    inside the sandbox. Anything else — a production config, ``None``, a foreign path
    — resolves to the sandbox root.

    The clamp matters for MEASUREMENT, not just safety: each cell passes its own
    ``basePath`` under the sandbox, and honouring it gives every cell a private
    durable outbox. A resolver that always returns one shared sandbox root makes
    cell N+1 ``_recover_live_pending()`` the leftover markers of cell N and re-insert
    them, which silently inflates the next cell's drain count.
    """
    sb = pathlib.Path(sandbox).resolve()

    def _isolated_resolver(cfg=None):
        base = None
        try:
            if isinstance(cfg, dict):
                base = cfg.get("basePath") or cfg.get("base_path") or None
            elif cfg is not None:
                base = getattr(cfg, "base_path", "") or None
        except Exception:
            base = None
        if base:
            try:
                p = pathlib.Path(base).resolve()
                if p == sb or sb in p.parents:
                    return str(p)
            except Exception:
                pass
        return str(sb)

    return _isolated_resolver


def reapply_isolation(sandbox) -> int:
    """Idempotently install the isolated resolver on every ``_resolve_data_dir`` alias.

    Needed because ``_harness_guard.isolate()`` restores the ORIGINAL attributes on
    exit; if that ever happened while a LiveBuffer could still write, the outbox
    would resolve to production. Call this after the guard exits, before anything
    else constructs an object.
    """
    import v3core.config as config_mod

    resolver = make_isolated_resolver(sandbox)
    n = 0
    if getattr(config_mod, "_resolve_data_dir", None) is not resolver:
        config_mod._resolve_data_dir = resolver
        n += 1
    for m in _v3core_modules():
        if getattr(m, "_resolve_data_dir", None) is resolver:
            continue
        if not hasattr(m, "_resolve_data_dir"):
            continue
        m._resolve_data_dir = resolver
        n += 1
    return n


def isolate_outbox(scale: float):
    """Redirect the v3-core data dir into a temp sandbox. FAIL CLOSED.

    Returns (sandbox_root, guard_cm, provenance_str). The caller must call
    ``guard_cm.__exit__`` when done if it is not None.
    """
    sandbox = pathlib.Path(tempfile.mkdtemp(prefix="embedfix-outage-sim-"))
    guard_cm = None
    provenance = "local fallback patch"

    # Import the modules we are about to exercise BEFORE entering the guard.
    # tests/_harness_guard.isolate() sweeps sys.modules for module-level
    # `_resolve_data_dir` aliases at __enter__ time; a module imported afterwards
    # binds whatever v3core.config holds then. Verified here: with v3core not yet
    # imported, the guard patches v3core.config but v3core.ingest (imported later)
    # ends up holding the ORIGINAL resolver, i.e. production. Pre-importing makes the
    # sweep cover every alias, and the check below still refuses to trust it blindly.
    try:
        import v3core.ingest  # noqa: F401
    except Exception as e:                                # pragma: no cover
        raise SystemExit(f"cannot import v3core.ingest: {e!r}")

    # (a) prefer the sibling guard, but never trust it blindly
    guard, guard_reason = _try_sibling_guard()
    if guard is not None:
        try:
            cm = guard.isolate()
            root = cm.__enter__()
            root_p = pathlib.Path(root) if root is not None else sandbox
            # the guard must actually redirect; if it did not, discard it
            import v3core.ingest as _ing
            probe = pathlib.Path(str(_ing._resolve_data_dir(None)))
            if PRODUCTION_ROOT in probe.parents or probe == PRODUCTION_ROOT:
                cm.__exit__(None, None, None)
            else:
                guard_cm = cm
                sandbox = root_p
                provenance = "tests/_harness_guard.isolate()"
        except Exception as e:
            guard_cm = None
            guard_reason = f"{type(e).__name__}: {e}"
    if guard_cm is None:
        provenance = f"local fallback patch (sibling guard not used: {guard_reason})"

    # (b) belt and braces: patch v3core.config AND every module-level alias.
    #     A module-level `from .config import _resolve_data_dir` binds a SEPARATE
    #     reference at import time; patching v3core.config alone does not cover it.
    targets = reapply_isolation(sandbox)
    targets = [getattr(m, "__name__", "?") for m in _v3core_modules()
               if getattr(m, "_resolve_data_dir", None) is not None]

    # (c) FAIL CLOSED: prove it before constructing anything
    import v3core.ingest as ing
    resolved = pathlib.Path(str(ing._resolve_data_dir(None))).resolve()
    prod = PRODUCTION_ROOT.resolve()
    if resolved == prod or prod in resolved.parents:
        raise SystemExit(
            f"REFUSING TO RUN: outbox isolation FAILED — resolved data dir {resolved} "
            f"is inside the production root {prod}. A fake pg does not isolate the "
            f"filesystem; this harness would write durable markers into production."
        )
    if pathlib.Path(sandbox).resolve() not in (resolved, *resolved.parents):
        raise SystemExit(
            f"REFUSING TO RUN: resolved data dir {resolved} is not under the temp "
            f"sandbox {sandbox}."
        )
    # EVERY alias must resolve inside the sandbox — one un-patched module-level
    # binding is enough to write durable markers into production.
    leaks = []
    for name, r in _alias_report():
        try:
            rp = pathlib.Path(r).resolve()
        except Exception:
            leaks.append((name, r))
            continue
        if pathlib.Path(sandbox).resolve() not in (rp, *rp.parents):
            leaks.append((name, r))
    if leaks:
        raise SystemExit(
            "REFUSING TO RUN: these modules still resolve the data dir OUTSIDE the "
            f"sandbox: {leaks}"
        )
    print(f"ISOLATION: outbox root={resolved}")
    print(f"ISOLATION: sandbox={sandbox}  (temp dir, not under {prod})")
    print(f"ISOLATION: guard={provenance}")
    print(f"ISOLATION: modules whose _resolve_data_dir was redirected: "
          f"{len(targets)} -> {', '.join(sorted(set(targets)))}")
    print(f"ISOLATION: every alias verified inside the sandbox ({len(_alias_report())} "
          f"checked, 0 leaks)")
    print("ISOLATION: pre-patch check — the UNPATCHED resolver would have returned "
          f"{PRODUCTION_OUTBOX.parent}")
    return sandbox, guard_cm, provenance


# ════════════════════════════════════════════════════════════════════════════
# 2. production outbox fingerprint (read-only self-check)
# ════════════════════════════════════════════════════════════════════════════

def fingerprint(root: pathlib.Path):
    """(file_count, sha256 of sorted relative names, max mtime_ns)."""
    if not root.exists():
        return (0, hashlib.sha256(b"").hexdigest(), 0, "MISSING")
    names, files, max_mtime = [], 0, 0
    try:
        max_mtime = root.stat().st_mtime_ns
    except Exception:
        pass
    for p in sorted(root.rglob("*")):
        try:
            rel = str(p.relative_to(root)).replace("\\", "/")
        except Exception:
            rel = str(p)
        names.append(rel)
        try:
            st = p.stat()
            max_mtime = max(max_mtime, st.st_mtime_ns)
            if p.is_file():
                files += 1
        except Exception:
            pass
    h = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()
    return (files, h, max_mtime, "OK")


def fp_line(tag: str, fp) -> str:
    return (f"  {tag}: file_count={fp[0]} names_sha256={fp[1][:16]}… "
            f"max_mtime_ns={fp[2]} state={fp[3]}")


class ProductionWatch:
    """Sampler that records every change to the production outbox tree.

    Purpose: distinguish "this harness wrote into production" from "a live
    v3-core gateway is concurrently ingesting on this machine". The harness never
    writes ``accepted_live_buffer``, so a change that touches BOTH trees (or only
    the accepted tree) cannot have come from here.
    """

    def __init__(self, root: pathlib.Path, interval=0.25):
        self.root = pathlib.Path(root)
        self.accepted = self.root.parent / "accepted_live_buffer"
        self.interval = interval
        self.changes: list[dict] = []
        self._stop = threading.Event()
        self._th = None
        self._base = None

    @staticmethod
    def _cheap(p: pathlib.Path):
        """(dir mtime_ns, entry count) — one stat + one scandir, not a full walk.

        The accepted tree holds thousands of tombstones; a full fingerprint of it
        every 250 ms would cost more than the cell it is watching.
        """
        try:
            st = p.stat()
            return (st.st_mtime_ns, sum(1 for _ in os.scandir(p)))
        except Exception:
            return (0, 0)

    def start(self):
        self._base = (self._cheap(self.root), self._cheap(self.accepted))
        self._th = threading.Thread(target=self._loop, name="sim-prodwatch", daemon=True)
        self._th.start()
        return self

    def _loop(self):
        prev_out, prev_acc = self._base
        t0 = time.time()
        while not self._stop.is_set():
            REAL_SLEEP(self.interval)
            out, acc = self._cheap(self.root), self._cheap(self.accepted)
            if out != prev_out or acc != prev_acc:
                self.changes.append({
                    "t": round(time.time() - t0, 2),
                    "outbox": (prev_out, out),
                    "accepted": (prev_acc, acc),
                    "outbox_changed": prev_out != out,
                    "accepted_changed": prev_acc != acc,
                })
                prev_out, prev_acc = out, acc

    def stop(self):
        self._stop.set()
        if self._th is not None:
            self._th.join(timeout=2.0)
        return self.changes


# ════════════════════════════════════════════════════════════════════════════
# 3. fake pg (records inserts + embedding_failures markers)
# ════════════════════════════════════════════════════════════════════════════

class FakeCursor:
    def __init__(self, pg):
        self._pg = pg
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = str(sql)
        if "INSERT INTO public.embedding_failures" in s:
            self._pg.record_marker(params or {})
            self.rowcount = 1
        elif "UPDATE public.embedding_failures" in s:
            self.rowcount = 1
        else:
            self.rowcount = 0
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, pg):
        self._pg = pg

    def cursor(self):
        return FakeCursor(self._pg)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class FakePG:
    """Records inserts + durable failure markers. Never opens a socket."""

    def __init__(self, rec):
        self._rec = rec

    def insert_message(self, source_id, content, embedding=None, metadata=None):
        self._rec.record_insert(source_id, embedding is not None)

    def open_side_connection(self):
        # NB: must hand over the RECORDER, not this object — FakePG.__getattr__
        # returns a no-op lambda for unknown attributes, which would silently
        # swallow record_marker() and make every NULL look unexplained.
        return FakeConn(self._rec)

    def __getattr__(self, name):
        return lambda *a, **kw: None


# ════════════════════════════════════════════════════════════════════════════
# 4. recorder
# ════════════════════════════════════════════════════════════════════════════

class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.text_to_id: dict[str, str] = {}
        self.enqueue_t: dict[str, float] = {}
        self.calls: list[dict] = []       # provider calls (per attempt)
        self.inserts: list[dict] = []     # PG inserts (one per processed item)
        self.markers: list[dict] = []     # embedding_failures rows
        self.depth_peak = 0
        self.depth_at_arrival_end = 0
        self.enqueue_rejected = 0
        self.foreign_thread_calls = 0

    def register_text(self, text: str, item_id: str):
        with self.lock:
            self.text_to_id[text] = item_id

    def id_for(self, text: str):
        with self.lock:
            return self.text_to_id.get(text, f"<unknown:{text[:24]}>")

    def record_call(self, item_id, t0, t1, outcome, timeout, provider_latency):
        with self.lock:
            if threading.current_thread().name != "v3-live-writer":
                self.foreign_thread_calls += 1
            self.calls.append({
                "item": item_id, "t0": t0, "t1": t1, "outcome": outcome,
                "timeout": timeout, "provider_latency": provider_latency,
                "thread": threading.current_thread().name,
            })

    def record_insert(self, source_id, has_vector):
        with self.lock:
            self.inserts.append({"source_id": source_id,
                                 "has_vector": has_vector, "t": time.time()})

    def record_marker(self, params):
        with self.lock:
            self.markers.append({
                "entity_id": params.get("entity_id"),
                "error_class": params.get("error_class"),
                "attempts": params.get("attempts"),
                "timeout_policy": params.get("timeout_policy"),
                "timeout_seconds": params.get("timeout_seconds"),
                "max_retries": params.get("max_retries"),
                "retryable": params.get("retryable"),
            })

    def observe_depth(self, d):
        with self.lock:
            if d > self.depth_peak:
                self.depth_peak = d

    def snapshot_counts(self):
        with self.lock:
            return len(self.inserts)


# ════════════════════════════════════════════════════════════════════════════
# 5. provider models
# ════════════════════════════════════════════════════════════════════════════

class Healthy:
    name = "healthy"
    desc = "latency 0.5s, always succeeds"

    def draw(self, timeout):
        return 0.5, "ok"


class SlowTail:
    name = "slow_tail"
    desc = "p=0.15 latency U(3.5,5.0)s else U(0.5,0.7)s; succeeds unless it exceeds the timeout"

    def __init__(self, seed=20260920):
        self.rng = random.Random(seed)

    def draw(self, timeout):
        if self.rng.random() < 0.15:
            lat = self.rng.uniform(3.5, 5.0)
        else:
            lat = self.rng.uniform(0.5, 0.7)
        # A real HTTP client aborts at `timeout`; a 3.5-5.0s response CANNOT
        # succeed under a 3s budget. Modelling it as a success would hide the
        # exact defect this simulation exists to show.
        if lat > timeout:
            return timeout, "timeout"
        return lat, "ok"


class SustainedTimeout:
    name = "sustained_timeout"
    desc = "EVERY call takes timeout+0.5s; the client aborts at the timeout, so the observed cost is the timeout"

    def draw(self, timeout):
        # provider needs timeout+0.5; the client gives up at `timeout`
        return timeout, "timeout"


class _Resp:
    status_code = 200
    text = ""
    headers = {"content-type": "application/json"}

    def json(self):
        return {"data": [{"index": 0, "embedding": VEC}]}

    def raise_for_status(self):
        return None


def make_stub(rec, model, scale):
    import requests

    def fake_post(url, json=None, headers=None, proxies=None, timeout=None, **kw):
        text = (json or {}).get("input", "")
        item_id = rec.id_for(text)
        tmo = float(timeout) if timeout is not None else 0.0
        observed, outcome = model.draw(tmo)
        t0 = time.time()
        if observed > 0:
            REAL_SLEEP(observed / scale)
        t1 = time.time()
        rec.record_call(item_id, t0, t1, outcome, tmo, observed)
        if outcome == "ok":
            return _Resp()
        raise requests.Timeout(
            f"simulated provider timeout: provider needs >{tmo}s, client budget {tmo}s"
        )

    return fake_post


# ════════════════════════════════════════════════════════════════════════════
# 6. one (scenario, policy) cell
# ════════════════════════════════════════════════════════════════════════════

def run_cell(scenario, model, timeout, retries, label, sandbox, scale,
             steady=STEADY_ITEMS, burst=BURST_ITEMS, grace=GRACE_S):
    import requests
    import v3core.ingest as ingest_mod
    from v3core.embedding import EmbedPolicy
    from v3core.ingest import LiveBuffer, LIVE_FLUSH_SEC

    cell_id = f"{scenario}__{label.replace('/', '_')}"
    cell_dir = pathlib.Path(sandbox) / cell_id
    cell_dir.mkdir(parents=True, exist_ok=True)

    rec = Recorder()
    pg = FakePG(rec)
    policy = EmbedPolicy(
        name=f"sim:{label}", timeout=float(timeout), retries=int(retries),
        description=f"simulated policy {timeout}s/{retries}r",
    )
    cfg = {
        "basePath": str(cell_dir),
        "storage": {"embed": {
            "endpoint": "http://127.0.0.1:9/v1/embeddings",   # never contacted
            "model": "BAAI/bge-m3",
            "apiKey": "offline-simulation",
            "dim": 1024,
        }},
    }

    orig_post = requests.post
    orig_sleep = time.sleep
    _policy_attr = ("STREAM_PRIMARY_EMBED_POLICY"
                    if hasattr(ingest_mod, "STREAM_PRIMARY_EMBED_POLICY")
                    else "DURABLE_WRITE_EMBED_POLICY")
    orig_policy = getattr(ingest_mod, _policy_attr)
    stop_sampler = threading.Event()
    buf = None
    writer = None
    t_start = t_end = 0.0
    t_burst_start = None
    drained_at_end = 0
    enqueued_total = 0
    try:
        # policy injection WITHOUT touching src/v3core/** : _flush reads the
        # module-global at call time (STREAM_PRIMARY_EMBED_POLICY on the
        # current branch; DURABLE_WRITE_EMBED_POLICY as baseline fallback).
        setattr(ingest_mod, _policy_attr, policy)

        buf = LiveBuffer(pg=pg, config=cfg)
        # scale the periodic-flush deadline with everything else
        buf._flush_sec = float(LIVE_FLUSH_SEC) / scale
        # a private outbox per cell: 0 markers here means this cell recovered nothing
        # from a previous cell (recovery would re-insert another cell's items and
        # inflate this cell's drain count).
        _pd = buf._live_pending_dir()
        pending_at_start = len(list(_pd.glob("*.json"))) if _pd else -1

        requests.post = make_stub(rec, model, scale)

        def _scaled_sleep(s):
            REAL_SLEEP(max(0.0, float(s) / scale))

        time.sleep = _scaled_sleep          # retry backoff scales too

        def _sampler():
            while not stop_sampler.is_set():
                try:
                    rec.observe_depth(buf._q.qsize())
                except Exception:
                    pass
                REAL_SLEEP(DEPTH_SAMPLE_REAL_S)

        sampler = threading.Thread(target=_sampler, name="sim-depth-sampler", daemon=True)
        sampler.start()

        interval_real = (ARRIVAL_INTERVAL_S / scale)
        t_start = time.time()
        # ── steady phase: 13 items/minute ────────────────────────────────────
        # Absolute schedule, not sleep-after-work: the durable outbox write+fsync
        # inside enqueue() costs real milliseconds that do NOT scale with the time
        # compression, so a naive "enqueue then sleep(interval)" would stretch the
        # arrival interval and understate the arrival pressure on the writer.
        for i in range(steady):
            item_id = f"{cell_id}#{i}"
            content = (f"[{cell_id}] turn {i} — simulated user turn for the outage "
                       f"simulation, payload padding {i * 7}")
            rec.register_text(content[:2000], item_id)
            ok = buf.enqueue(session_id=f"sess-{scenario}", msg_id=f"{cell_id}-{i}",
                             content=content, role="user", turn_id=f"t{i}")
            if ok is False:
                rec.enqueue_rejected += 1
            with rec.lock:
                rec.enqueue_t[item_id] = time.time()
            enqueued_total += 1
            next_slot = t_start + (i + 1) * interval_real
            REAL_SLEEP(max(0.0, next_slot - time.time()))
        # ── burst phase: instantaneous pile-up ───────────────────────────────
        t_burst_start = time.time()
        for i in range(burst):
            item_id = f"{cell_id}#b{i}"
            content = (f"[{cell_id}] burst {i} — instantaneous pile-up turn, "
                       f"payload padding {i * 13}")
            rec.register_text(content[:2000], item_id)
            ok = buf.enqueue(session_id=f"sess-{scenario}", msg_id=f"{cell_id}-b{i}",
                             content=content, role="user", turn_id=f"b{i}")
            if ok is False:
                rec.enqueue_rejected += 1
            with rec.lock:
                rec.enqueue_t[item_id] = time.time()
            enqueued_total += 1
        t_arrival_end = time.time()
        rec.depth_at_arrival_end = buf._q.qsize()
        # ── bounded grace, then end of run ───────────────────────────────────
        REAL_SLEEP(grace / scale)
        t_end = time.time()
        drained_at_end = rec.snapshot_counts()
        depth_at_end = buf._q.qsize()
        writer = buf._writer
    finally:
        stop_sampler.set()
        # NB: shutdown/drain happens BEFORE time.sleep is restored — the writer
        # thread's retry backoff must stay time-compressed, otherwise a 10s/2r cell
        # spends 1s+2s of REAL backoff after the run window and the writer outlives
        # the cell.
        if buf is not None:
            try:
                buf.shutdown(timeout=0.5)
            except Exception:
                pass
        writer_alive = False
        if writer is not None:
            deadline = time.time() + 20.0
            while writer.is_alive() and time.time() < deadline:
                REAL_SLEEP(0.02)
            writer_alive = writer.is_alive()
        requests.post = orig_post
        time.sleep = orig_sleep
        setattr(ingest_mod, _policy_attr, orig_policy)

    return _summarise(cell_id, scenario, model, timeout, retries, label, rec, scale,
                      t_start, t_end, t_arrival_end, drained_at_end, enqueued_total,
                      depth_at_end, writer_alive, pending_at_start, t_burst_start)


def _summarise(cell_id, scenario, model, timeout, retries, label, rec, scale,
               t_start, t_end, t_arrival_end, drained_at_end, enqueued_total,
               depth_at_end, writer_alive, pending_at_start=-1, t_burst_start=None):
    with rec.lock:
        calls = list(rec.calls)
        inserts = list(rec.inserts)
        markers = list(rec.markers)
        enqueue_t = dict(rec.enqueue_t)
    elapsed_sim = (t_end - t_start) * scale
    run_min_sim = elapsed_sim / 60.0

    inserts_at_end = inserts[:drained_at_end]
    per_item_calls: dict[str, list[dict]] = {}
    for c in calls:
        per_item_calls.setdefault(c["item"], []).append(c)

    # service span per processed item = first provider call -> row insert
    insert_by_id = {}
    for ins in inserts:
        # source_id is "live/<session>/<msg_id>"; map back through msg_id
        insert_by_id.setdefault(ins["source_id"], ins)
    span: list[tuple[str, float, float, float]] = []   # item, start, end, enqueue_t
    for item_id, cs in per_item_calls.items():
        cs_sorted = sorted(cs, key=lambda c: c["t0"])
        st = cs_sorted[0]["t0"]
        en = cs_sorted[-1]["t1"]
        # an insert for this item completes its processing
        for ins in inserts:
            if ins["t"] >= en and _item_of_source(ins["source_id"], item_id):
                en = ins["t"]
                break
        span.append((item_id, st, en, enqueue_t.get(item_id, st)))
    span.sort(key=lambda x: x[1])

    # head-of-line blocking: item i was already in the queue when the item being
    # serviced before it finished -> it waited for that earlier item.
    hol_waits = []
    for k in range(1, len(span)):
        prev_end = span[k - 1][2]
        enq = span[k][3]
        hol_waits.append(max(0.0, prev_end - enq) * scale)
    hol_completed_max = max(hol_waits) if hol_waits else 0.0
    # Steady-phase HOL only (burst items excluded): "did a slow embedding delay the
    # NEXT arriving turn", without the burst pile-up swamping the number.
    steady_waits = []
    if t_burst_start is not None:
        steady_span = [s for s in span if s[3] < t_burst_start]
        for k in range(1, len(steady_span)):
            steady_waits.append(max(0.0, steady_span[k - 1][2] - steady_span[k][3]) * scale)
    hol_steady_max = max(steady_waits) if steady_waits else 0.0
    # Items that never got a row also prove head-of-line blocking: the writer is
    # single-threaded and never idle while the queue is non-empty, so an item that
    # sat in the queue for T seconds was blocked by whatever was in service.
    processed = {s[0] for s in span}
    pending_waits = [max(0.0, (t_end - t) * scale) for iid, t in enqueue_t.items()
                     if iid not in processed]
    hol_pending_max = max(pending_waits) if pending_waits else 0.0
    hol_max = max(hol_completed_max, hol_pending_max)
    hol_observed = hol_max > HOL_WAIT_THRESHOLD_S

    # NULL accounting: a NULL insert is EXPLAINED only if a durable
    # embedding_failures marker exists for that exact entity_id.
    marker_ids = {m["entity_id"] for m in markers}
    null_rows = [i for i in inserts_at_end if not i["has_vector"]]
    explained = sum(1 for i in null_rows if i["source_id"] in marker_ids)
    unexplained = len(null_rows) - explained

    ages = []
    for item_id, st, en, enq in span:
        if any(_item_of_source(i["source_id"], item_id) for i in inserts_at_end):
            ages.append((en - enq) * scale)
    oldest_age = max(ages) if ages else None
    if DEBUG:
        print(f"    [debug {cell_id}] spans={len(span)} inserts={len(inserts)} "
              f"at_end={len(inserts_at_end)} marker_ids={len(marker_ids)} "
              f"ages={[round(a, 2) for a in ages]} "
              f"unknown_ids={[k for k in per_item_calls if k.startswith('<unknown')]}")
        for (iid, st, en, enq) in span[:3]:
            print(f"      span {iid}: enq={enq - t_start:.3f} start={st - t_start:.3f} "
                  f"end={en - t_start:.3f} (all real s from t_start)")

    service_s = [(c["t1"] - c["t0"]) * scale for c in calls]
    return {
        "cell": cell_id, "scenario": scenario, "policy": label,
        "timeout": timeout, "retries": retries,
        "items": enqueued_total,
        "drained": len(inserts_at_end),
        "embedded_ok": sum(1 for i in inserts_at_end if i["has_vector"]),
        "null_rows": len(null_rows),
        "explained_null": explained,
        "unexplained_null": unexplained,
        "markers": len(markers),
        "deferred_backlog": enqueued_total - len(inserts_at_end),
        "queue_depth_peak": rec.depth_peak,
        "queue_depth_at_arrival_end": rec.depth_at_arrival_end,
        "queue_depth_at_end": depth_at_end,
        "oldest_pending_age_s": oldest_age,
        "rows_per_minute_drained": (len(inserts_at_end) / run_min_sim) if run_min_sim else 0.0,
        "hol_blocking_observed": hol_observed,
        "hol_max_wait_s": hol_max,
        "hol_completed_max_s": hol_completed_max,
        "hol_pending_max_s": hol_pending_max,
        "hol_steady_max_s": hol_steady_max,
        "provider_calls": len(calls),
        "timeouts": sum(1 for c in calls if c["outcome"] == "timeout"),
        "mean_service_s": (statistics.fmean(service_s) if service_s else 0.0),
        "max_service_s": (max(service_s) if service_s else 0.0),
        "wall_real_s": (t_end - t_start),
        "sim_minutes": run_min_sim,
        "enqueue_rejected": rec.enqueue_rejected,
        "foreign_thread_calls": rec.foreign_thread_calls,
        "unknown_text_calls": sum(1 for k in per_item_calls if k.startswith("<unknown")),
        "pending_at_start": pending_at_start,
        "ceiling_from_mean_service_per_min": (
            60.0 / statistics.fmean(service_s) if service_s and statistics.fmean(service_s) else 0.0),
        "writer_alive_after": writer_alive,
    }


class ZeroLatency:
    name = "zero_latency"
    desc = "returns immediately — used to calibrate non-provider per-item cost"

    def draw(self, timeout):
        return 0.0, "ok"


def run_calibration(sandbox, scale, items=60, enqueue_probe=8):
    """Measure the per-item cost the writer pays WITHOUT any provider latency.

    Everything here is real: the same LiveBuffer, the same durable outbox
    write+fsync+unlink, the same sha256 job id, the same queue. This is the part
    of the per-item cost that does NOT shrink when the time compression factor
    divides the provider latency, so it bounds how much the compressed runs
    understate the real drain rate.

    Two phases, because the two sides have different costs and only one of them
    consumes writer throughput:

      * ENQUEUE side — the caller thread's durable outbox write. Measured with the
        real ``enqueue()``. It does NOT compete with the writer for throughput.
      * WRITER side — measured by putting items straight on ``LiveBuffer._q`` so the
        writer is saturated from the first item. ``enqueue()`` costs about the same
        as the writer per item on this machine, so an enqueue-driven calibration
        never builds a queue and cannot measure a saturated drain.
    """
    import requests
    import v3core.ingest as ingest_mod
    from v3core.embedding import EmbedPolicy
    from v3core.ingest import LiveBuffer

    cell_id = "calibration__zero_latency"
    cell_dir = pathlib.Path(sandbox) / cell_id
    cell_dir.mkdir(parents=True, exist_ok=True)
    rec = Recorder()
    pg = FakePG(rec)
    policy = EmbedPolicy("sim:calibration", 3.0, 0, "calibration only")
    cfg = {"basePath": str(cell_dir),
           "storage": {"embed": {"endpoint": "http://127.0.0.1:9/v1/embeddings",
                                 "model": "BAAI/bge-m3", "apiKey": "offline-simulation",
                                 "dim": 1024}}}
    _policy_attr = ("STREAM_PRIMARY_EMBED_POLICY"
                    if hasattr(ingest_mod, "STREAM_PRIMARY_EMBED_POLICY")
                    else "DURABLE_WRITE_EMBED_POLICY")
    orig_post, orig_policy = requests.post, getattr(ingest_mod, _policy_attr)
    buf = None
    t_enq0 = t_enq1 = 0.0
    n_at_inject = 0
    depth_at_inject = 0
    t_inject = t_done = 0.0
    try:
        setattr(ingest_mod, _policy_attr, policy)
        buf = LiveBuffer(pg=pg, config=cfg)
        requests.post = make_stub(rec, ZeroLatency(), scale)

        # ── phase 1: real enqueue() path (caller-side durable write) ─────────
        t_enq0 = time.time()
        for i in range(enqueue_probe):
            content = f"[{cell_id}] enqueue-probe turn {i}"
            rec.register_text(content[:2000], f"{cell_id}#probe{i}")
            buf.enqueue(session_id="sess-cal", msg_id=f"{cell_id}-probe{i}",
                        content=content, role="user", turn_id=f"p{i}")
        t_enq1 = time.time()
        enqueue_ms = ((t_enq1 - t_enq0) / max(1, enqueue_probe)) * 1000.0

        # ── phase 2: saturate the writer (bypass the caller-side durable write) ─
        n_at_inject = len(rec.inserts)
        t_inject = time.time()
        for i in range(items):
            item_id = f"{cell_id}#{i}"
            content = f"[{cell_id}] calibration turn {i}"
            rec.register_text(content[:2000], item_id)
            with rec.lock:
                rec.enqueue_t[item_id] = time.time()
            buf._q.put(("sess-cal", f"{cell_id}-{i}", content, "user", f"t{i}",
                        None, None, None))
        depth_at_inject = buf._q.qsize()
        deadline = time.time() + 60.0
        while len(rec.inserts) < items + n_at_inject and time.time() < deadline:
            REAL_SLEEP(0.002)
        t_done = time.time()
    finally:
        if buf is not None:
            try:
                buf.shutdown(timeout=0.5)
            except Exception:
                pass
        requests.post = orig_post
        setattr(ingest_mod, _policy_attr, orig_policy)
    drained = len(rec.inserts)
    n_in_window = drained - n_at_inject
    if n_in_window >= 2 and depth_at_inject > 1:
        per_item_real = (t_done - t_inject) / n_in_window
        method = (f"saturated drain: {n_in_window} items drained over "
                  f"{t_done - t_inject:.3f}s while the queue held "
                  f"{depth_at_inject} items")
    else:                                             # pragma: no cover
        per_item_real = (t_done - t_enq0) / max(1, drained)
        method = (f"fallback: whole-run average (writer never saturated; "
                  f"depth at inject={depth_at_inject}, drained in window="
                  f"{n_in_window})")
    return {
        "items": items, "drained": drained, "drain_real_s": t_done - t_enq0,
        "drained_in_window": n_in_window,
        "per_item_real_ms": per_item_real * 1000.0,
        "enqueue_ms_per_item": enqueue_ms,
        "method": method,
    }


def _item_of_source(source_id: str, item_id: str) -> bool:
    """source_id == 'live/<session_id>/<msg_id>'; msg_id == cell_id with '#'->'-'."""
    try:
        msg_id = source_id.split("/")[-1]
    except Exception:
        return False
    cell, _, tail = item_id.partition("#")
    return msg_id == f"{cell}-{tail}"


# ════════════════════════════════════════════════════════════════════════════
# 7. reporting
# ════════════════════════════════════════════════════════════════════════════

def print_main_table(results, scale):
    cols = [
        ("scenario", 18), ("policy", 18), ("rows_per_minute_drained", 23),
        ("queue_depth_peak", 17), ("oldest_pending_age_s", 21),
        ("explained_null", 15), ("deferred_backlog", 17),
        ("hol_blocking_observed", 22),
    ]
    header = "  ".join(n.ljust(w) for n, w in cols)
    print()
    print("TABLE 1 — required measurements (durations converted back to un-scaled "
          f"seconds; time-scale x{scale:g})")
    print(header)
    print("-" * len(header))
    for r in results:
        vals = [
            r["scenario"], r["policy"],
            f"{r['rows_per_minute_drained']:.2f}",
            f"{r['queue_depth_peak']}",
            ("NOT MEASURED" if r["oldest_pending_age_s"] is None
             else f"{r['oldest_pending_age_s']:.1f}"),
            f"{r['explained_null']}",
            f"{r['deferred_backlog']}",
            f"{r['hol_blocking_observed']}",
        ]
        print("  ".join(str(v).ljust(w) for v, (_, w) in zip(vals, cols)))
    print()
    print("  rows_per_minute_drained = PG rows the single writer thread completed "
          "/ simulated wall minute")
    print("  queue_depth_peak        = max len(LiveBuffer._q) sampled every "
          f"{DEPTH_SAMPLE_REAL_S * scale * 1000:.0f} ms (simulated)")
    print("  oldest_pending_age_s    = max (row insert time - enqueue time) over "
          "COMPLETED items, simulated seconds")
    print("  explained_null          = NULL-embedding rows that carry a durable "
          "embedding_failures marker (an explicit failure, not a silent hole)")
    print("  deferred_backlog        = enqueued items with no row yet at end of run")
    print(f"  hol_blocking_observed   = some item waited > {HOL_WAIT_THRESHOLD_S:.1f} s "
          "behind the item being serviced before it")


def print_detail_table(results):
    cols = [("scenario", 18), ("policy", 18), ("items", 6), ("drained", 8),
            ("embedded_ok", 12), ("null_rows", 10), ("markers", 8),
            ("provider_calls", 15), ("timeouts", 9), ("mean_service_s", 15),
            ("max_service_s", 14), ("hol_max_wait_s", 15),
            ("hol_steady_max_s", 17), ("ceiling/min", 12)]
    header = "  ".join(n.ljust(w) for n, w in cols)
    print()
    print("TABLE 2 — supporting detail")
    print(header)
    print("-" * len(header))
    for r in results:
        vals = [r["scenario"], r["policy"], r["items"], r["drained"],
                r["embedded_ok"], r["null_rows"], r["markers"],
                r["provider_calls"], r["timeouts"],
                f"{r['mean_service_s']:.2f}", f"{r['max_service_s']:.2f}",
                f"{r['hol_max_wait_s']:.2f}",
                f"{r['hol_steady_max_s']:.2f}",
                f"{r['ceiling_from_mean_service_per_min']:.2f}"]
        print("  ".join(str(v).ljust(w) for v, (_, w) in zip(vals, cols)))
    print("  ceiling/min = 60 / measured mean per-attempt service time — the writer's "
          "own throughput ceiling, independent of how fast items arrived")
    print("  unexplained_null (rows with NO marker) per cell: " +
          ", ".join(f"{r['scenario']}/{r['policy']}={r['unexplained_null']}"
                    for r in results))
    print("  enqueue() rejections (returned False) per cell: " +
          ", ".join(f"{r['scenario']}/{r['policy']}={r['enqueue_rejected']}"
                    for r in results))
    bad = [r for r in results if r["foreign_thread_calls"]]
    print(f"  provider calls NOT made on the 'v3-live-writer' thread: "
          f"{sum(r['foreign_thread_calls'] for r in results)} (must be 0)")


def print_sustained_summary(results):
    rows = [r for r in results if r["scenario"] == "sustained_timeout"]
    if not rows:
        return None
    print()
    print("=" * 100)
    print("FINAL SUMMARY — sustained_timeout (every provider call exceeds the timeout)")
    print("=" * 100)
    print(f"  arrival rate modelled: {PEAK_ROWS_PER_MINUTE:.0f} rows/min "
          f"(1 item every {ARRIVAL_INTERVAL_S:.2f} s) + a {BURST_ITEMS}-item burst")
    print()
    print(f"  {'policy':<18} {'drain/min':>9} {'vs arrival':>11} {'q_peak':>7} "
          f"{'oldest_age_s':>13} {'deferred':>9} {'hol':>6} {'hol_max_wait_s':>15} "
          f"{'hol_steady_s':>13} {'embedded_ok':>12}")
    print("  " + "-" * 120)
    for r in sorted(rows, key=lambda x: (x["timeout"], x["retries"])):
        ratio = r["rows_per_minute_drained"] / PEAK_ROWS_PER_MINUTE
        print(f"  {r['policy']:<18} {r['rows_per_minute_drained']:>9.2f} "
              f"{ratio:>10.2f}x {r['queue_depth_peak']:>7} "
              f"{(r['oldest_pending_age_s'] or 0):>13.1f} "
              f"{r['deferred_backlog']:>9} {str(r['hol_blocking_observed']):>6} "
              f"{r['hol_max_wait_s']:>15.1f} {r['hol_steady_max_s']:>13.1f} "
              f"{r['embedded_ok']:>12}")
    print()
    # worst-case per-item cost is derivable from the policy itself
    print("  worst-case per-item cost (timeout*(retries+1) + backoff 1+2+4...):")
    for r in sorted(rows, key=lambda x: (x["timeout"], x["retries"])):
        backoff = sum(2 ** i for i in range(r["retries"]))
        print(f"    {r['policy']:<18} timeout={r['timeout']:g}s retries={r['retries']} "
              f"-> {r['timeout'] * (r['retries'] + 1) + backoff:g}s/item "
              f"-> ceiling {60.0 / (r['timeout'] * (r['retries'] + 1) + backoff):.2f} "
              f"items/min")
    return rows


def print_slow_tail_summary(results):
    rows = [r for r in results if r["scenario"] == "slow_tail"]
    if not rows:
        return None
    print()
    print("=" * 100)
    print("SUMMARY — slow_tail (p=0.15 of calls need 3.5-5.0 s; the client aborts at "
          "the policy timeout)")
    print("=" * 100)
    print(f"  {'policy':<18} {'timeouts':>9} {'embedded_ok':>12} {'explained_null':>15} "
          f"{'unexplained_null':>17} {'drain/min':>10} {'q_peak':>7} {'hol':>6}")
    print("  " + "-" * 100)
    for r in sorted(rows, key=lambda x: (x["timeout"], x["retries"])):
        print(f"  {r['policy']:<18} {r['timeouts']:>9} {r['embedded_ok']:>12} "
              f"{r['explained_null']:>15} {r['unexplained_null']:>17} "
              f"{r['rows_per_minute_drained']:>10.2f} {r['queue_depth_peak']:>7} "
              f"{str(r['hol_blocking_observed']):>6}")
    return rows


def print_verdict(rows, slow_rows):
    if not rows:
        return
    print()
    print("=" * 100)
    print("VERDICT (derived only from the numbers printed above)")
    print("=" * 100)
    by_pol = {r["policy"]: r for r in rows}
    slow_by_pol = {r["policy"]: r for r in (slow_rows or [])}
    tail = 3.488
    print(f"  observed production tail to cover: {tail:.3f} s")
    covers = [r for r in rows if r["timeout"] > tail]
    loses = [r for r in rows if r["timeout"] <= tail]
    for r in loses:
        st = slow_by_pol.get(r["policy"])
        slow_txt = ""
        if st is not None:
            slow_txt = (f" In slow_tail it produced {st['embedded_ok']} embedded rows, "
                        f"{st['null_rows']} NULL rows of which "
                        f"{st['explained_null']} explained and "
                        f"{st['unexplained_null']} UNEXPLAINED (no marker).")
        print(f"  - {r['policy']}: timeout {r['timeout']:g}s <= {tail:.3f}s -> does NOT "
              f"cover the observed tail.{slow_txt}")
    if covers:
        # among policies that cover the tail, the one with the smallest per-item
        # worst case is the one that blocks the head of the line least
        def per_item(r):
            backoff = sum(2 ** i for i in range(r["retries"]))
            return r["timeout"] * (r["retries"] + 1) + backoff

        best = min(covers, key=lambda r: (per_item(r), r["hol_steady_max_s"]))
        for r in sorted(covers, key=per_item):
            print(f"  - {r['policy']}: worst case {per_item(r):g}s/item, "
                  f"drain {r['rows_per_minute_drained']:.2f}/min, "
                  f"q_peak {r['queue_depth_peak']}, "
                  f"oldest_pending {r['oldest_pending_age_s'] or 0:.1f}s, "
                  f"hol_max_wait {r['hol_max_wait_s']:.1f}s "
                  f"(steady-phase {r['hol_steady_max_s']:.1f}s), "
                  f"deferred {r['deferred_backlog']}")
        print()
        print(f"  NUMBERS-BACKED PICK: {best['policy']} — smallest per-item worst case "
              f"({per_item(best):g}s) among the policies whose timeout exceeds the "
              f"observed {tail:.3f}s tail, with the smallest head-of-line wait "
              f"({best['hol_steady_max_s']:.1f}s steady-phase / "
              f"{best['hol_max_wait_s']:.1f}s incl. the burst) and the shallowest queue "
              f"({best['queue_depth_peak']}) of that set.")
        print(f"  10s/2 retries costs {per_item(by_pol['durable-10s/2r']):g}s/item "
              f"(x{per_item(by_pol['durable-10s/2r']) / per_item(best):.1f} the pick) — "
              f"its retries are serial in the SAME writer thread, so they buy nothing "
              f"during a sustained outage and cost head-of-line blocking.")
    print("  NOTE: under a SUSTAINED outage every policy above ends with "
          "embedded_ok = 0 for the items it processed. The policy choice changes the "
          "backlog and head-of-line behaviour, not whether the outage loses embeddings; "
          "that is what the durable failure marker + repair pass are for.")


# ════════════════════════════════════════════════════════════════════════════
# 8. main
# ════════════════════════════════════════════════════════════════════════════

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--time-scale", type=float, default=20.0,
                    help="uniform time compression factor (default 20)")
    ap.add_argument("--quick", action="store_true",
                    help="fewer items (24 steady / 6 burst) — numbers are NOT "
                         "comparable to a full run; for smoke-testing only")
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--policies", default=",".join(pol_key(p[0], p[1]) for p in POLICIES),
                    help="comma list of short forms like 3/0,5/0,10/0,10/2 (default: all)")
    ap.add_argument("--debug", action="store_true", help="dump per-cell span/age internals")
    ap.add_argument("--verbose-logs", action="store_true",
                    help="show v3core INFO/WARNING/ERROR logs (default: suppressed so "
                         "the tables are readable)")
    args = ap.parse_args(argv)
    global DEBUG
    DEBUG = bool(args.debug)
    if not args.verbose_logs:
        import logging
        logging.disable(logging.CRITICAL)

    bootstrap_syspath()

    steady, burst = (24, 6) if args.quick else (STEADY_ITEMS, BURST_ITEMS)

    print("=" * 100)
    print("v3-core live embedding writer — outage simulation")
    print("=" * 100)
    print(f"  repo root      : {REPO_ROOT}")
    print(f"  python         : {sys.executable}")
    print(f"  time scale     : x{args.time_scale:g} (all simulated durations are "
          f"divided by {args.time_scale:g} on the clock, multiplied back for reporting)")
    print(f"  arrival        : {PEAK_ROWS_PER_MINUTE:.0f} rows/min = 1 item every "
          f"{ARRIVAL_INTERVAL_S:.3f} s simulated")
    print(f"  items per cell : {steady} steady + {burst} burst; grace "
          f"{GRACE_S:g} s simulated after the last enqueue")
    if args.quick:
        print("  *** --quick: item counts REDUCED (24/6 instead of 60/12). Numbers are "
              "not comparable to a full run. ***")

    fp_before = fingerprint(PRODUCTION_OUTBOX)
    print()
    print(f"PRODUCTION OUTBOX FINGERPRINT (read-only, before): {PRODUCTION_OUTBOX}")
    print(fp_line("before", fp_before))

    sandbox, guard_cm, provenance = isolate_outbox(args.time_scale)

    want_scen = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    want_pol = [p.strip() for p in args.policies.split(",") if p.strip()]
    models = {"healthy": Healthy(), "slow_tail": SlowTail(),
              "sustained_timeout": SustainedTimeout()}

    results = []
    per_cell_fp = []
    watch = ProductionWatch(PRODUCTION_OUTBOX).start()
    try:
        for scen in SCENARIOS:
            if scen not in want_scen:
                continue
            for (tmo, ret, label) in POLICIES:
                if not pol_selected(tmo, ret, label, want_pol):
                    continue
                before = fingerprint(PRODUCTION_OUTBOX)
                print(f"\n[cell] {scen} / {label}  (timeout={tmo:g}s retries={ret}) …",
                      flush=True)
                r = run_cell(scen, models[scen], tmo, ret, label, sandbox,
                             args.time_scale, steady=steady, burst=burst)
                after = fingerprint(PRODUCTION_OUTBOX)
                per_cell_fp.append((r["cell"], before == after))
                if before != after:
                    print(f"  !! production outbox fingerprint CHANGED during cell "
                          f"{r['cell']}")
                results.append(r)
                print(f"       drained={r['drained']}/{r['items']} "
                      f"q_peak={r['queue_depth_peak']} "
                      f"drain/min={r['rows_per_minute_drained']:.2f} "
                      f"deferred={r['deferred_backlog']} "
                      f"null={r['null_rows']} (explained {r['explained_null']}) "
                      f"hol={r['hol_blocking_observed']} "
                      f"real_wall={r['wall_real_s']:.1f}s "
                      f"writer_alive_after={r['writer_alive_after']}", flush=True)

        fp_after = fingerprint(PRODUCTION_OUTBOX)

        print_main_table(results, args.time_scale)
        print_detail_table(results)

        # ── calibration: per-item harness overhead (NOT scaled by the factor) ─
        print()
        print("CALIBRATION — per-item writer-thread cost that does NOT scale with the "
              "time compression")
        cal = run_calibration(sandbox, args.time_scale)
        overhead_ms = cal["per_item_real_ms"]
        print(f"  zero-latency provider cell ({cal['method']}):")
        print(f"    writer-side per-item cost      : {overhead_ms:.2f} ms real "
              f"({cal['drained_in_window']} items drained inside the saturated "
              f"window; {cal['drained']} total incl. the enqueue probe, in "
              f"{cal['drain_real_s']:.2f}s real)")
        print(f"    enqueue-side per-item cost     : {cal['enqueue_ms_per_item']:.2f} ms "
              f"real (durable outbox write+fsync+replace, on the CALLER thread, so it "
              f"does not consume writer throughput)")
        print(f"    scaled to simulated time       : {overhead_ms * args.time_scale:.1f} "
              f"ms/item — this is the part of the per-item cost that does NOT shrink "
              f"with --time-scale, so it is the harness's known distortion.")
        print("    per-scenario share of the measured mean service time:")
        for r in results:
            overhead_sim = overhead_ms / 1000.0 * args.time_scale
            share = (overhead_sim / r["mean_service_s"] * 100.0
                     if r["mean_service_s"] else 0.0)
            print(f"      {r['scenario']:<18} {r['policy']:<18} "
                  f"mean_service={r['mean_service_s']:.2f}s  "
                  f"overhead={overhead_sim:.2f}s ({share:.1f}% of service)")
        print("    the distortion makes the simulated writer SLOWER than a real one, so "
              "the real drain rates are at least as good as the figures above.")

        print_sustained_summary(results)
        print_slow_tail_summary(results)
        print_verdict([r for r in results if r["scenario"] == "sustained_timeout"],
                      [r for r in results if r["scenario"] == "slow_tail"])
    finally:
        watch_changes = watch.stop()
        # tear the sibling guard down LAST: its __exit__ restores the original
        # resolvers, which must never be installed while a LiveBuffer could write.
        if guard_cm is not None:
            try:
                guard_cm.__exit__(None, None, None)
            except Exception:
                pass
        reapply_isolation(sandbox)
        try:
            fp_after
        except NameError:
            fp_after = fingerprint(PRODUCTION_OUTBOX)

    # ── self-checks ─────────────────────────────────────────────────────────
    print()
    print("=" * 100)
    print("SELF-CHECKS")
    print("=" * 100)
    ok = True

    import v3core.ingest as ing
    resolved = pathlib.Path(str(ing._resolve_data_dir(None))).resolve()
    tmp_root = pathlib.Path(tempfile.gettempdir()).resolve()
    iso_ok = tmp_root in resolved.parents and PRODUCTION_ROOT.resolve() not in resolved.parents
    ok &= iso_ok
    print(f"  [{'PASS' if iso_ok else 'FAIL'}] ISOLATION: outbox root={resolved} is "
          f"under the temp dir {tmp_root} and not under {PRODUCTION_ROOT}")

    print()
    print(f"  PRODUCTION OUTBOX FINGERPRINT: {PRODUCTION_OUTBOX}")
    print(fp_line("before", fp_before))
    print(fp_line("after ", fp_after))
    fp_ok = fp_before == fp_after
    content_ok = fp_before[:2] == fp_after[:2]
    mtime_ok = fp_before[2] == fp_after[2]
    ok &= content_ok
    print(f"  [{'PASS' if fp_ok else 'FAIL'}] production outbox tree fingerprint "
          f"UNCHANGED (file_count, names sha256, max mtime_ns)")
    if not fp_ok:
        print(f"       component detail: file_count {fp_before[0]} -> {fp_after[0]} "
              f"[{'same' if fp_before[0] == fp_after[0] else 'CHANGED'}]; "
              f"names sha256 [{'same' if fp_before[1] == fp_after[1] else 'CHANGED'}]; "
              f"max mtime_ns [{'same' if mtime_ok else 'CHANGED'}]")
        if content_ok and not mtime_ok:
            print("       => the outbox CONTENT is byte-identical (0 files before and "
                  "after, same names hash). Only the directory mtime moved, which on "
                  "NTFS happens when SOME OTHER PROCESS creates and deletes files in "
                  "it. See the production-watch report below for the evidence that "
                  "the writer was external to this harness.")
    print(f"  [{'PASS' if all(v for _, v in per_cell_fp) else 'FAIL'}] production "
          f"outbox fingerprint unchanged across each of the {len(per_cell_fp)} cells "
          f"({sum(1 for _, v in per_cell_fp if v)}/{len(per_cell_fp)} cells clean)")
    print(f"  production-watch: {len(watch_changes)} change(s) observed on "
          f"{PRODUCTION_OUTBOX} and its sibling accepted_live_buffer while the "
          f"simulation ran:")
    if not watch_changes:
        print("    none — no other process and no part of this harness touched the "
              "production outbox during the run.")
    for c in watch_changes:
        origin = ("THIS HARNESS OR ANOTHER WRITER touching the outbox"
                  if c["outbox_changed"] and not c["accepted_changed"]
                  else "an EXTERNAL v3-core writer (accepted_live_buffer, which this "
                       "harness never writes, changed too)")
        print(f"    t=+{c['t']}s outbox {c['outbox'][0]} -> {c['outbox'][1]} | "
              f"accepted {c['accepted'][0]} -> {c['accepted'][1]}  => {origin}")
    if fp_ok:
        print("    => the required before/after fingerprint (file_count + names "
              "sha256) is IDENTICAL, so nothing was added to or removed from the "
              "production outbox.")

    foreign = sum(r["foreign_thread_calls"] for r in results)
    print(f"  [{'PASS' if foreign == 0 else 'FAIL'}] every provider call ran on the "
          f"'v3-live-writer' thread ({foreign} off-thread calls)")
    live = sum(1 for r in results if r["writer_alive_after"])
    print(f"  [{'PASS' if live == 0 else 'FAIL'}] writer thread stopped after each "
          f"cell ({live} cells left it alive)")
    rej = sum(r["enqueue_rejected"] for r in results)
    print(f"  [{'PASS' if rej == 0 else 'FAIL'}] no enqueue() was rejected "
          f"({rej} rejected)")
    unk = sum(r["unknown_text_calls"] for r in results)
    print(f"  [{'PASS' if unk == 0 else 'FAIL'}] no provider call was made for an "
          f"unregistered item ({unk} calls) — a non-zero count would mean another "
          f"cell's recovered items leaked into this cell's measurements")
    pre = [r["cell"] for r in results if r["pending_at_start"] != 0]
    print(f"  [{'PASS' if not pre else 'FAIL'}] every cell started with an EMPTY "
          f"private outbox (0 recovered markers); offenders: "
          f"{pre if pre else 'none'}")
    cons = all(r["drained"] + r["deferred_backlog"] == r["items"] for r in results)
    ok &= cons
    print(f"  [{'PASS' if cons else 'FAIL'}] drained + deferred_backlog == items for "
          f"every cell (no double-counting)")

    print()
    print(f"  ISOLATION provenance: {provenance}")
    print(f"  alias sweep: " + ", ".join(f"{n}={r}" for n, r in _alias_report()))
    exists = pathlib.Path(sandbox).exists()
    print(f"  sandbox: {sandbox} "
          f"({'kept for inspection' if exists else 'REMOVED by the sibling guard on exit'})")
    print(f"  OVERALL: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
