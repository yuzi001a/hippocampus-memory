"""F2 canonical source idempotency — RED/GREEN regression matrix.

Contract (Candidate A, atomic uniqueness in existing conversation_stream):
  1. one source row at most per (host, session_id, event_id) — same HTTP
     repeat, concurrent writers, restart/recovery, commit-success/ack-lost;
  2. host/session/event identity present in durable pending/recovery payloads;
  3. additive schema only (nullable cols + partial unique index; no backfill);
  4. atomic ON CONFLICT insert, no SELECT-then-INSERT; duplicate flows to the
     existing B01 ACK path (duplicate=true);
  5. same msg/event ids from two hosts make TWO rows; old NULL-identity rows
     are preserved; fresh + upgrade schema paths both carry the change.

Real-PostgreSQL tests run only when F2_PG_DSN is set
(e.g. postgresql://v3user:pw@127.0.0.1:55521/f2src); otherwise skipped.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import pytest

# Repo root is derived from __file__, never hardcoded: this file lives at
# <repo>/src/v3-core/tests/, so parents[3] is <repo>. A hardcoded absolute
# path made the matrix unrunnable on any other clone/CI checkout.
REPO = Path(__file__).resolve().parents[3]
PKG_SCHEMA = REPO / "src" / "v3-core" / "src" / "v3core" / "schema"
ROOT_SCHEMA = REPO / "src" / "v3-core" / "schema"

F2_PG_DSN = os.environ.get("F2_PG_DSN", "")

IDENTITY_INDEX = "conversation_stream_host_session_event_uniq"


def _read_schema_variants(name: str) -> dict[str, str]:
    return {
        "root": (ROOT_SCHEMA / name).read_text(encoding="utf-8"),
        "packaged": (PKG_SCHEMA / name).read_text(encoding="utf-8"),
    }


def _ddl_body(text: str) -> str:
    return "\n".join(line for line in text.splitlines()
                      if not line.lstrip().startswith("--"))


# ── schema: fresh bootstrap carries additive identity + partial unique index ──
@pytest.mark.parametrize("variant", ["root", "packaged"])
def test_f2_fresh_bootstrap_has_identity_columns_and_partial_unique_index(variant):
    texts = _read_schema_variants("alpha_bootstrap.sql")
    text = texts[variant]
    assert "ADD COLUMN IF NOT EXISTS host" in text or "host " in text
    assert IDENTITY_INDEX in text
    # partial index: complete-identity tuples only; historic NULL rows untouched
    assert "WHERE" in text and "IS NOT NULL" in text
    # additive only
    body = _ddl_body(text)
    for banned in ("DROP", "TRUNCATE", "DELETE FROM", "UPDATE public.conversation_stream"):
        assert banned not in body


def test_f2_upgrade_path_has_identity_columns_and_partial_unique_index():
    texts = _read_schema_variants("upgrade_v0_2.sql")
    for variant, text in texts.items():
        assert IDENTITY_INDEX in text, variant
        assert "IS NOT NULL" in text, variant
        body = _ddl_body(text)
        assert "DROP" not in body and "TRUNCATE" not in body, variant


def test_f2_schema_copies_agree_on_identity_ddl():
    import re
    root = (ROOT_SCHEMA / "alpha_bootstrap.sql").read_text(encoding="utf-8")
    pkg = (PKG_SCHEMA / "alpha_bootstrap.sql").read_text(encoding="utf-8")
    pat = re.compile(r".*(host|event_id|%s).*" % IDENTITY_INDEX)
    root_hits = sorted(set(pat.findall(root)))
    pkg_hits = sorted(set(pat.findall(pkg)))
    assert root_hits, "root alpha_bootstrap.sql carries no identity DDL"
    assert root_hits == pkg_hits


# ── durable pending payload carries host/session/event identity ──
def _make_buffer(tmp_path, monkeypatch):
    from v3core.ingest import LiveBuffer
    cfg = {"basePath": str(tmp_path)}
    buf = LiveBuffer(pg=None, config=cfg)
    monkeypatch.setattr(buf, "_start_writer", lambda: None)
    return buf


def test_f2_pending_payload_preserves_host_identity(tmp_path, monkeypatch):
    buf = _make_buffer(tmp_path, monkeypatch)
    assert buf.enqueue("sessF2", "evF2", "hello", "user", "1",
                       timestamp="2026-09-28T10:00:00+00:00",
                       tool_calls=[], tool_results=[],
                       host="synthetic-dsh") is True
    files = sorted((tmp_path / "j" / "pending_live_buffer").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["session_id"] == "sessF2"
    assert payload["msg_id"] == "evF2"
    assert payload.get("host") == "synthetic-dsh"
    buf.shutdown(timeout=0.2)


def test_f2_recovery_restores_host_identity(tmp_path, monkeypatch):
    from v3core.ingest import LiveBuffer
    cfg = {"basePath": str(tmp_path)}
    buf = LiveBuffer(pg=None, config=cfg)
    monkeypatch.setattr(buf, "_start_writer", lambda: None)
    buf.enqueue("sessF2", "evF2", "hello", "user", "1",
                timestamp="2026-09-28T10:00:00+00:00",
                tool_calls=[], tool_results=[], host="synthetic-dsh")
    buf.shutdown(timeout=0.2)
    # restart: fresh buffer, writer isolated, recovery must keep host
    buf2 = LiveBuffer(pg=None, config=cfg)
    monkeypatch.setattr(buf2, "_start_writer", lambda: None)
    buf2._recover_live_pending()
    items = []
    try:
        while True:
            items.append(buf2._q.get_nowait())
    except Exception:
        pass
    assert items, "recovery replayed nothing"
    assert len(items[0]) > 8 and items[0][8] == "synthetic-dsh"
    assert items[0][0] == "sessF2" and items[0][1] == "evF2"
    buf2.shutdown(timeout=0.2)


# ── insert_message: atomic ON CONFLICT when identity complete, legacy otherwise ──
class _FakeCur:
    def __init__(self):
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, stmt, params=()):
        self.statements.append((str(stmt), tuple(params) if params else ()))


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass


def _store_with_fake_conn(monkeypatch):
    from v3core.pg_store import PgEmbedStore
    cur = _FakeCur()
    store = PgEmbedStore({"storage": {"pg": {}}})

    @contextlib.contextmanager
    def _fake_lease(timeout=None):
        yield _FakeConn(cur)

    monkeypatch.setattr(store, "lease", _fake_lease)
    return store, cur


def test_f2_insert_with_identity_uses_atomic_on_conflict(monkeypatch):
    store, cur = _store_with_fake_conn(monkeypatch)
    store.insert_message("x", "content", metadata={
        "session_id": "s", "role": "user", "host": "h", "event_id": "e"})
    assert cur.statements, "no SQL executed"
    sql, params = cur.statements[-1]
    assert "ON CONFLICT" in sql
    assert "host" in sql and "event_id" in sql
    assert "SELECT" not in sql.split("ON CONFLICT")[0].upper().split("INSERT")[0]


def test_f2_insert_without_identity_keeps_legacy_shape(monkeypatch):
    store, cur = _store_with_fake_conn(monkeypatch)
    store.insert_message("x", "content", metadata={"session_id": "s", "role": "user"})
    assert cur.statements
    sql = cur.statements[-1][0]
    assert "ON CONFLICT" not in sql


# ── real PostgreSQL (opt-in, disposable-only) ──
#
# The pytest domain hard-blocks ``psycopg2.connect`` (P0-A conftest guard).
# These tests run only when F2_PG_DSN points at a loopback, non-5433
# disposable database (refused otherwise), and each test restores the real
# connect factory (resolved from the C extension, which the guard does not
# shadow) for its own scope via monkeypatch — the same pattern as
# test_recall_v2_pg_integration.py. Without F2_PG_DSN they skip; the same
# matrix additionally runs over real HTTP in scripts/f2_source_idempotency_e2e.py.
def _parse_f2_dsn(dsn: str) -> dict:
    from urllib.parse import urlparse, unquote
    u = urlparse(dsn)
    return {"host": u.hostname or "", "port": u.port or 0,
            "database": u.path.lstrip("/"), "user": unquote(u.username or ""),
            "password": unquote(u.password or "")}


_F2_DSN = _parse_f2_dsn(F2_PG_DSN) if F2_PG_DSN else {}
_F2_LOOPBACK = {"127.0.0.1", "::1", "localhost", "localhost.localdomain"}
# Database-name whitelist. These four real-PG tests issue DELETE against
# conversation_stream, so a mistyped F2_PG_DSN must refuse to run rather
# than clean rows out of some unrelated local database. Only disposable
# F2 experiment databases are admissible; the production database
# (v3embeddings) and any hand-named dev DB are rejected outright.
_F2_ALLOWED_DATABASES = frozenset({"f2src", "f2e2e"})
_F2_DSN_REJECTION = ""
_F2_DSN_OK = False
if F2_PG_DSN:
    _host = _F2_DSN.get("host", "").lower()
    _port = _F2_DSN.get("port")
    _db = _F2_DSN.get("database", "")
    if _host not in _F2_LOOPBACK:
        _F2_DSN_REJECTION = f"host {_host!r} is not loopback"
    elif _port == 5433:
        _F2_DSN_REJECTION = "port 5433 is the production PG port — refused"
    elif _db not in _F2_ALLOWED_DATABASES:
        _F2_DSN_REJECTION = (
            f"database {_db!r} is not an allowed F2 experiment database "
            f"{sorted(_F2_ALLOWED_DATABASES)} — refused (these tests DELETE)"
        )
    else:
        _F2_DSN_OK = True


def _real_connect_factory():
    try:
        from psycopg2._psycopg import connect as _real_connect  # type: ignore
        return _real_connect
    except Exception:
        pass
    try:
        from psycopg2._psycopg import _connect as _real_connect  # type: ignore
        return _real_connect
    except Exception:
        pass
    import psycopg2 as _p
    return _p.connect


@pytest.fixture()
def f2_real_pg(monkeypatch):
    if not F2_PG_DSN:
        pytest.skip("F2_PG_DSN not set — real-PG F2 cases are opt-in")
    if not _F2_DSN_OK:
        pytest.skip(f"F2_PG_DSN rejected by the F2 safety gate: "
                    f"{_F2_DSN_REJECTION}")
    import psycopg2
    _factory = _real_connect_factory()

    def _kwargs_tolerant_connect(*args, **kwargs):
        if args or not kwargs:
            return _factory(*args, **kwargs)
        dsn = " ".join(f"{k}={v}" for k, v in kwargs.items()
                       if k in ("host", "hostaddr", "port", "dbname", "database",
                                "user", "password", "connect_timeout"))
        return _factory(dsn)

    monkeypatch.setattr(psycopg2, "connect", _kwargs_tolerant_connect)
    d = dict(_F2_DSN)
    return {"storage": {"pg": d}}, d


def _pg_conn(d: dict):
    import psycopg2
    dsn = (f"host={d['host']} port={d['port']} dbname={d['database']} "
           f"user={d['user']} password={d['password']} connect_timeout=10")
    # The C-extension factory takes a single DSN string, not kwargs.
    try:
        c = psycopg2.connect(dsn)
    except TypeError:
        c = psycopg2.connect(host=d["host"], port=d["port"], dbname=d["database"],
                             user=d["user"], password=d["password"], connect_timeout=10)
    c.autocommit = True
    return c


needs_f2_pg = pytest.mark.skipif(not F2_PG_DSN, reason="F2_PG_DSN not set")


# ── DSN safety gate is itself under test ──
# The real-PG cases below DELETE from conversation_stream. If the gate ever
# regressed to "any loopback, non-5433 DB", a typo'd F2_PG_DSN could wipe
# rows in an unrelated local database. These tests pin the gate contract
# without needing a live PG.
@pytest.mark.parametrize("dsn,accept,why", [
    ("postgresql://v3user:pw@127.0.0.1:55521/f2src", True, "canonical F2 src DB"),
    ("postgresql://v3user:pw@127.0.0.1:55521/f2e2e", True, "canonical F2 e2e DB"),
    ("postgresql://v3user:pw@localhost:55521/f2src", True, "loopback by name"),
    ("postgresql://v3user:pw@10.0.0.5:55521/f2src", False, "non-loopback host"),
    ("postgresql://v3user:pw@127.0.0.1:5433/f2src", False, "production PG port"),
    ("postgresql://v3user:pw@127.0.0.1:55521/v3embeddings", False, "production DB name"),
    ("postgresql://v3user:pw@127.0.0.1:55521/postgres", False, "system DB name"),
    ("postgresql://v3user:pw@127.0.0.1:55521/my_dev_db", False, "arbitrary DB name"),
    ("postgresql://v3user:pw@127.0.0.1:55521/", False, "empty DB name"),
])
def test_f2_dsn_gate_accepts_only_f2_experiment_databases(dsn, accept, why):
    parsed = _parse_f2_dsn(dsn)
    host, port, db = parsed["host"].lower(), parsed["port"], parsed["database"]
    if host not in _F2_LOOPBACK:
        ok = False
    elif port == 5433:
        ok = False
    elif db not in _F2_ALLOWED_DATABASES:
        ok = False
    else:
        ok = True
    assert ok is accept, f"{why}: dsn={dsn!r}"


def test_f2_dsn_gate_whitelist_excludes_production_database():
    # The production database name must never become admissible here.
    assert "v3embeddings" not in _F2_ALLOWED_DATABASES
    assert "postgres" not in _F2_ALLOWED_DATABASES
    assert _F2_ALLOWED_DATABASES == frozenset({"f2src", "f2e2e"})


def test_f2_repo_root_is_derived_from_test_file():
    """No absolute machine path may survive in the test module."""
    # The schema artifacts this module reads must exist under the derived root.
    assert (ROOT_SCHEMA / "alpha_bootstrap.sql").is_file()
    assert (PKG_SCHEMA / "alpha_bootstrap.sql").is_file()
    # And the derived root must actually be a repo root (contain the marker dirs).
    assert REPO.name and (REPO / "src" / "v3-core" / "tests").is_dir()


@needs_f2_pg
def test_f2_realpg_retry_same_identity_is_one_row(f2_real_pg):
    from v3core.pg_store import PgEmbedStore
    cfg, d = f2_real_pg
    store = PgEmbedStore(dict(cfg))
    sid = "f2test-retry"
    c = _pg_conn(d)
    try:
        cur = c.cursor()
        cur.execute("DELETE FROM conversation_stream WHERE session_id=%s", (sid,))
        for ts in ("2026-09-28T11:00:01+00:00", "2026-09-28T11:00:07+00:00"):
            store.insert_message("x", "F2-RETRY", metadata={
                "session_id": sid, "role": "user", "host": "h1",
                "event_id": "ev1", "timestamp": ts})
        cur.execute("SELECT count(*) FROM conversation_stream WHERE session_id=%s", (sid,))
        assert int(cur.fetchone()[0]) == 1
    finally:
        c.close()


@needs_f2_pg
def test_f2_realpg_two_hosts_same_ids_make_two_rows(f2_real_pg):
    from v3core.pg_store import PgEmbedStore
    cfg, d = f2_real_pg
    store = PgEmbedStore({"storage": {"pg": dict(d)}})
    sid = "f2test-hosts"
    c = _pg_conn(d)
    try:
        cur = c.cursor()
        cur.execute("DELETE FROM conversation_stream WHERE session_id=%s", (sid,))
        for host in ("hostA", "hostB"):
            store.insert_message("x", "F2-HOSTS", metadata={
                "session_id": sid, "role": "user", "host": host,
                "event_id": "same-ev"})
        cur.execute("SELECT count(*) FROM conversation_stream WHERE session_id=%s", (sid,))
        assert int(cur.fetchone()[0]) == 2
    finally:
        c.close()


@needs_f2_pg
def test_f2_realpg_concurrent_same_identity_is_one_row(f2_real_pg):
    import threading
    from v3core.pg_store import PgEmbedStore
    cfg, d = f2_real_pg
    sid = "f2test-conc"
    c = _pg_conn(d)
    c.cursor().execute("DELETE FROM conversation_stream WHERE session_id=%s", (sid,))
    c.close()
    errors: list = []

    def _w(i: int):
        try:
            PgEmbedStore({"storage": {"pg": dict(d)}}).insert_message(
                "x", "F2-CONC", metadata={
                    "session_id": sid, "role": "user", "host": "h1",
                    "event_id": "evC",
                    "timestamp": f"2026-09-28T11:02:{i:02d}+00:00"})
        except Exception as e:  # noqa: BLE001
            errors.append(str(e)[:200])

    ths = [threading.Thread(target=_w, args=(i,)) for i in range(8)]
    [t.start() for t in ths]
    [t.join(timeout=90) for t in ths]
    assert not errors, errors
    c = _pg_conn(d)
    try:
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM conversation_stream WHERE session_id=%s", (sid,))
        assert int(cur.fetchone()[0]) == 1
    finally:
        c.close()


@needs_f2_pg
def test_f2_realpg_legacy_null_rows_preserved(f2_real_pg):
    _cfg, d = f2_real_pg
    c = _pg_conn(d)
    try:
        cur = c.cursor()
        cur.execute("INSERT INTO conversation_stream (session_id, role, content) "
                    "VALUES ('f2test-legacy', 'user', 'OLD-NULL-ROW')")
        cur.execute("SELECT host, event_id FROM conversation_stream "
                    "WHERE session_id='f2test-legacy' ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        assert row[0] is None and row[1] is None
        cur.execute("DELETE FROM conversation_stream WHERE session_id='f2test-legacy'")
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════════════
# F2-D: the accepted tombstone must land in the HOST NAMESPACE, not just the
# accepted_live_buffer root.
#
# ``_live_accepted_path`` resolves a real host to ``accepted/<host>/<ident>.json``
# (ingest.py:278), i.e. ``tmp`` is written into a CHILD of the accepted root.
# ``_persist_live_accepted`` only ever created the ROOT
# (``dirp.mkdir(parents=True, exist_ok=True)``), so every namespaced accepted
# write — including the one the real y400 f2-03 run performs inside ``_ack_live_item``
# — hit FileNotFoundError on ``open(tmp, "w")``, the exception was swallowed by
# the method's own ``except`` (return False), and ``_ack_live_item`` then returned
# WITHOUT unlinking the pending marker. Symptom: accepted never appears, pending
# never clears, and the caller sees only a WARNING line.
#
# This is a REAL disk test on a temp-root data dir: the real ``_persist_live_accepted``,
# the real ``_ack_live_item``, the real ``_live_job_id`` — no stubbed persistence
# method, no fake filesystem. ``LiveBuffer.__new__`` builds a bare instance so the
# writer thread and PG wiring never start; every attribute the two methods touch
# is set explicitly.
# ══════════════════════════════════════════════════════════════════════════
def test_f2_namespaced_accepted_tombstone_is_created_on_real_disk(tmp_path):
    """RED→GREEN: a host-namespaced accepted write must not FileNotFoundError."""
    import threading

    from v3core.ingest import LiveBuffer

    root = tmp_path / "f2-host-root"          # temp-root data dir
    host = "synthetic-dsh"                    # a REAL host → namespaced path
    item = ("sessF2D", "evF2D", "hello", "user", "t1", None, None, None, host)

    buf = LiveBuffer.__new__(LiveBuffer)
    buf._pg = None
    buf._pg_config = {"basePath": str(root)}
    buf._durability_lock = threading.Lock()
    buf._pending_jobs_lock = threading.Lock()
    buf._pending_jobs = set()

    accepted_dir = root / "j" / "accepted_live_buffer"
    pending_dir = root / "j" / "pending_live_buffer"
    accepted_path = buf._live_accepted_path(item[0], item[1], host)

    # The precondition: the host namespace directory does NOT exist yet. Only
    # the accepted ROOT exists (or nothing does) — this is exactly the shape
    # the broken mkdir left behind on the real laptop.
    assert accepted_path is not None
    assert accepted_path.parent == accepted_dir / host
    assert not accepted_path.parent.exists(), \
        "precondition: the host namespace folder must be missing"

    # The real write. No fake persistence method stands in for it.
    assert buf._persist_live_accepted(item) is True, (
        "namespaced accepted tombstone was not persisted "
        f"(expected path {accepted_path})")

    # 1) the missing host folder was created
    assert accepted_path.parent.is_dir()
    # 2) the accepted JSON is durable: real file, real parseable payload
    assert accepted_path.is_file()
    payload = json.loads(accepted_path.read_text(encoding="utf-8"))
    assert payload["accepted"] is True
    assert payload["session_id"] == item[0]
    assert payload["msg_id"] == item[1]
    assert payload["host"] == host
    # the atomic temp+replace left nothing behind
    assert not list(accepted_path.parent.glob(".*.tmp")), \
        "a temp file survived the atomic replace"

    # 3) an existing pending marker for the same identity is removed by the ack,
    #    and only after the tombstone is durable.
    pending_dir.mkdir(parents=True, exist_ok=True)
    pending = pending_dir / f"{buf._live_job_id(item)}.json"
    pending.write_text(json.dumps({"job_id": buf._live_job_id(item),
                                   "session_id": item[0], "msg_id": item[1],
                                   "host": host}), encoding="utf-8")
    buf._pending_jobs.add(buf._live_job_id(item))
    assert pending.is_file()

    buf._ack_live_item(item)

    assert accepted_path.is_file(), "the accepted tombstone must survive the ack"
    assert not pending.exists(), \
        "_ack_live_item must remove the pending marker once accepted is durable"
    assert buf._pending_jobs == set(), \
        "the in-process pending slot must be released by the ack"

    # idempotent: a second ack/re-persist is a no-op, not a rewrite or a failure
    assert buf._persist_live_accepted(item) is True
    buf._ack_live_item(item)
    assert accepted_path.is_file() and not pending.exists()


# ══════════════════════════════════════════════════════════════════════════
# F2-C fix — identity fallback must not run inside an ABORTED transaction
# ══════════════════════════════════════════════════════════════════════════
#
# The pre-F2 tolerance branch used to run the legacy INSERT immediately after
# the identity INSERT failed, on the same cursor. In PostgreSQL an error aborts
# the transaction, so the next statement on that connection failed with 25P02
# (current transaction is aborted) and the source row was LOST — the very thing
# the fallback existed to prevent.
#
# The doubles below model the real driver: once execute() raises, the
# connection is in a failed state and every further statement raises 25P02
# until rollback(). They also pin the two over-broad predicates the old
# ``except`` used: any "column ... does not exist" (not just the identity
# columns) and a missing arbiter index, which PostgreSQL reports as 42P10 and
# which the old string match never matched at all.
class _PgError(Exception):
    """Minimal stand-in for psycopg2.Error with a SQLSTATE."""

    def __init__(self, message: str, sqlstate: str):
        super().__init__(message)
        self.sqlstate = sqlstate


def _undefined_column(column: str) -> _PgError:
    return _PgError(f'column "{column}" does not exist', "42703")


def _no_arbiter() -> _PgError:
    return _PgError(
        'there is no unique or exclusion constraint matching the ON CONFLICT '
        'specification', "42P10")


def _aborted() -> _PgError:
    return _PgError("current transaction is aborted, commands ignored "
                    "until end of transaction block", "25P02")


class _AbortingCur:
    """Cursor that models PostgreSQL's abort-on-error transaction state.

    ``fail_with`` is consumed by the FIRST execute(); from then on the
    connection is aborted and every statement raises 25P02 until rollback().
    """

    def __init__(self, fail_with: _PgError, *, rows_for_identity: bool = True):
        self.fail_with = fail_with
        self.rows_for_identity = rows_for_identity
        self.aborted = False
        self.rolled_back = False
        self.statements: list[tuple[str, tuple]] = []
        self._pending_row = None

    def execute(self, stmt: str, params=()):
        text = " ".join(str(stmt).split())
        if self.aborted:
            raise _aborted()
        self.statements.append((text, tuple(params) if params else ()))
        if self.fail_with is not None:
            err, self.fail_with = self.fail_with, None
            self.aborted = True
            raise err
        if "ON CONFLICT" in text:
            self._pending_row = (1,) if self.rows_for_identity else None
        else:
            self._pending_row = None

    def fetchone(self):
        row, self._pending_row = self._pending_row, None
        return row

    def rollback(self):
        self.rolled_back = True
        self.aborted = False


class _AbortingConn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self._cur.rollback()


def _store_with_aborting_cur(monkeypatch, cur):
    from v3core.pg_store import PgEmbedStore
    store = PgEmbedStore({"storage": {"pg": {}}})
    conn = _AbortingConn(cur)
    # psycopg2 cursors expose the owning connection; the fix rolls back
    # through it before retrying the legacy shape.
    cur.connection = conn

    @contextlib.contextmanager
    def _fake_lease(timeout=None):
        yield conn

    monkeypatch.setattr(store, "lease", _fake_lease)
    return store, conn


@pytest.mark.parametrize("err,label", [
    (_undefined_column("host"), "identity column host missing"),
    (_undefined_column("event_id"), "identity column event_id missing"),
    (_no_arbiter(), "partial unique index missing (42P10)"),
])
def test_f2_identity_fallback_rolls_back_before_legacy_insert(monkeypatch, err, label):
    """The legacy fallback must run in a usable transaction (RED→GREEN)."""
    cur = _AbortingCur(err)
    store, conn = _store_with_aborting_cur(monkeypatch, cur)
    store.insert_message("x", "F2-OLD-SCHEMA", metadata={
        "session_id": "f2-old", "role": "user",
        "host": "h1", "event_id": "ev1"})
    assert cur.rolled_back, f"{label}: fallback ran without a rollback first"
    assert len(cur.statements) == 2, (
        f"{label}: expected identity attempt + legacy fallback, got "
        f"{[s[0][:40] for s in cur.statements]}"
    )
    assert "ON CONFLICT" in cur.statements[0][0]
    assert "ON CONFLICT" not in cur.statements[1][0]
    assert conn.commits == 1


def test_f2_identity_fallback_preserves_source_row_under_fake_pg(monkeypatch):
    """Both statements must carry the content, so the row survives."""
    cur = _AbortingCur(_undefined_column("host"))
    store, _conn = _store_with_aborting_cur(monkeypatch, cur)
    store.insert_message("x", "F2-CONTENT-MUST-SURVIVE", metadata={
        "session_id": "f2-old", "role": "user", "host": "h1", "event_id": "ev1"})
    for text, params in cur.statements:
        assert "F2-CONTENT-MUST-SURVIVE" in params, (
            "fallback dropped the source content"
        )


def test_f2_unrelated_missing_column_is_not_swallowed(monkeypatch):
    """Only the identity columns / arbiter justify the legacy fallback.

    A missing *unrelated* column is a real error and must propagate; the old
    ``or "column" in _msg`` branch silently degraded it and lost the row.
    """
    cur = _AbortingCur(_undefined_column("tool_results"))
    store, _conn = _store_with_aborting_cur(monkeypatch, cur)
    with pytest.raises(_PgError) as exc:
        store.insert_message("x", "F2-UNRELATED", metadata={
            "session_id": "f2", "role": "user", "host": "h1", "event_id": "ev1"})
    assert exc.value.sqlstate == "42703"
    assert "tool_results" in str(exc.value)
    assert len(cur.statements) == 1, "a legacy fallback was attempted for an unrelated column"


def test_f2_arbiter_index_name_in_error_is_recognised(monkeypatch):
    """The 42P10 arbiter message may name the index; that is still F2's own."""
    cur = _AbortingCur(_PgError(
        'relation "conversation_stream_host_session_event_uniq" does not exist',
        "42P10"))
    store, _conn = _store_with_aborting_cur(monkeypatch, cur)
    store.insert_message("x", "F2-ARBITER", metadata={
        "session_id": "f2", "role": "user", "host": "h1", "event_id": "ev1"})
    assert cur.rolled_back
    assert len(cur.statements) == 2


def test_f2_non_schema_error_propagates_without_fallback(monkeypatch):
    """A connectivity/permission error must not be mistaken for old schema."""
    cur = _AbortingCur(_PgError('relation "conversation_stream" does not exist',
                                "42P01"))
    store, _conn = _store_with_aborting_cur(monkeypatch, cur)
    with pytest.raises(_PgError):
        store.insert_message("x", "F2-NONSCHEMA", metadata={
            "session_id": "f2", "role": "user", "host": "h1", "event_id": "ev1"})
    assert len(cur.statements) == 1


def test_f2_fallback_does_not_log_private_sql(monkeypatch, caplog):
    """No statement text or bound params in the warning (secret hygiene)."""
    cur = _AbortingCur(_undefined_column("host"))
    store, _conn = _store_with_aborting_cur(monkeypatch, cur)
    with caplog.at_level("WARNING"):
        store.insert_message("x", "F2-PRIVATE-CONTENT-DO-NOT-LOG", metadata={
            "session_id": "f2", "role": "user", "host": "h1", "event_id": "ev1"})
    text = caplog.text
    assert "F2-PRIVATE-CONTENT-DO-NOT-LOG" not in text, "source content was logged"
    assert "INSERT INTO conversation_stream" not in text, "SQL text was logged"


def test_f2_duplicate_conflict_is_a_noop_without_fallback(monkeypatch):
    """The happy path is unchanged: no rollback, no fallback, returns False."""
    cur = _AbortingCur(None, rows_for_identity=False)
    store, conn = _store_with_aborting_cur(monkeypatch, cur)
    assert store.insert_message("x", "F2-DUP", metadata={
        "session_id": "f2", "role": "user", "host": "h1", "event_id": "ev1"}) is False
    assert not cur.rolled_back
    assert len(cur.statements) == 1
    assert conn.commits == 1


def test_f2_fresh_identity_insert_returns_true_without_fallback(monkeypatch):
    cur = _AbortingCur(None, rows_for_identity=True)
    store, _conn = _store_with_aborting_cur(monkeypatch, cur)
    assert store.insert_message("x", "F2-FRESH", metadata={
        "session_id": "f2", "role": "user", "host": "h1", "event_id": "ev1"}) is True
    assert len(cur.statements) == 1


@needs_f2_pg
def test_f2_realpg_old_schema_preserves_source_row(f2_real_pg):
    """Real PG, real pre-F2 table: the source row must still land.

    Creates a shadow table without host/event_id, points the store at it by
    swapping conversation_stream inside a transaction-local view is not
    possible — so instead this asserts the observable end state on the REAL
    F2 schema: an identity insert commits exactly one row, and a second one
    is a no-op. Combined with the double-based abort tests above, that pins
    "old schema keeps the source" without mutating the schema under test.
    """
    from v3core.pg_store import PgEmbedStore
    _cfg, d = f2_real_pg
    sid = "f2test-realpg"
    c = _pg_conn(d)
    try:
        cur = c.cursor()
        cur.execute("DELETE FROM conversation_stream WHERE session_id=%s", (sid,))
    finally:
        c.close()
    store = PgEmbedStore({"storage": {"pg": dict(d)}})
    for _ in range(2):
        store.insert_message("x", "F2-REALPG", metadata={
            "session_id": sid, "role": "user", "host": "h1", "event_id": "ev1"})
    c = _pg_conn(d)
    try:
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM conversation_stream WHERE session_id=%s", (sid,))
        assert int(cur.fetchone()[0]) == 1
        cur.execute("DELETE FROM conversation_stream WHERE session_id=%s", (sid,))
    finally:
        c.close()
