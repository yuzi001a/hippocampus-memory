"""Regression tests for the embedding write-reliability hotfix.

These pin the contract that replaced the silent-NULL hole:

    Embedding failure is allowed. Silent permanent memory loss is not.

The historical defect was, in `ingest.py`:

    try:
        ev = call_embedding(text, cfg)
    except ValueError:
        raise
    except Exception:
        ev = None          # ← an unexplained NULL went straight to the DB

Every test below fails against that original code, and the reason each one exists
is stated in its docstring so a future reader can tell whether it still matters.

T1 transient timeout → retry → success
T2 retries exhausted → source preserved + durable, retryable failure state
T3 missing model/endpoint → fail-closed config error, NOT a transient NULL
T4 401/403 → deterministic, must NOT be retried like a timeout
T5 realtime → keeps the short SLA (durable retry must not leak into the 8s budget)
"""

from __future__ import annotations

import json
import time

import pytest
import requests

from v3core.embedding import (
    EmbeddingCallError,
    EmbedErrorClass,
    EmbedPolicy,
    REALTIME_EMBED_POLICY,
    STREAM_PRIMARY_EMBED_POLICY,
    DURABLE_WRITE_EMBED_POLICY,
    BATCH_EMBED_POLICY,
    classify_embed_error,
    call_embedding,
)
from v3core.embed_failures import (
    EmbedOutcomeStatus,
    embed_for_write,
    record_embedding_failure,
    resolve_embedding_failure,
)

CFG = {
    "endpoint": "http://embed.test/v1/embeddings",
    "model": "BAAI/bge-m3",
    "apiKey": "test-key",
    "_fingerprint": "fp-test",
}

VEC = [0.1, 0.2, 0.3]


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {"data": [{"embedding": VEC}]}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


class _FakeConn:
    """Minimal psycopg2-shaped connection recording marker writes."""

    def __init__(self):
        self.executed: list[tuple[str, dict]] = []
        self.commits = 0
        self.closed = False
        self.fail_on_execute = False
        self._last = None

    def cursor(self):
        conn = self

        class _Cur:
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                if conn.fail_on_execute:
                    raise RuntimeError("simulated PG failure")
                conn.executed.append((sql, params))
                conn._last = params
                self.rowcount = 1

            def fetchone(self):
                return (0,)

            def fetchall(self):
                return []

        return _Cur()

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _post_sequence(monkeypatch, behaviours):
    """Make requests.post walk a scripted list of responses/exceptions."""
    calls = {"n": 0}

    def _fake_post(url, json=None, headers=None, proxies=None, timeout=None):
        i = calls["n"]
        calls["n"] += 1
        calls.setdefault("timeouts", []).append(timeout)
        b = behaviours[min(i, len(behaviours) - 1)]
        if isinstance(b, BaseException):
            raise b
        return b

    monkeypatch.setattr(requests, "post", _fake_post)
    return calls


# ─── T1 ─────────────────────────────────────────────────────────────────────

def test_t1_transient_timeout_then_success(monkeypatch):
    """A 3.5s-class timeout must be survivable on a durable write.

    The production provider's tail exceeds 3s. Before this hotfix a durable write
    inherited the realtime 3s/0 budget, so one slow response meant a permanent
    hole. With the durable policy the first attempt may time out and the second
    must still land the vector.
    """
    calls = _post_sequence(monkeypatch, [requests.Timeout("read timeout=10.0"), _Resp()])
    conn = _FakeConn()

    out = embed_for_write(
        "some durable content", CFG,
        entity_table="conversation_stream", entity_id="live/s/1",
        phase="live_ingest", conn=conn, policy=DURABLE_WRITE_EMBED_POLICY,
    )

    assert out.status is EmbedOutcomeStatus.OK
    assert out.vector == VEC
    assert calls["n"] == 2, "must have retried once"
    # The timeout budget must come from the durable policy, not the 3s default.
    assert all(t == DURABLE_WRITE_EMBED_POLICY.timeout for t in calls["timeouts"])
    # Success must not leave an unresolved marker behind.
    assert not [e for e in conn.executed if "INSERT INTO public.embedding_failures" in e[0]]


def test_t1b_durable_policy_budget_is_not_the_realtime_default():
    """The whole point of naming the policy: durable ≠ realtime budget."""
    assert DURABLE_WRITE_EMBED_POLICY.timeout > REALTIME_EMBED_POLICY.timeout
    assert DURABLE_WRITE_EMBED_POLICY.retries > REALTIME_EMBED_POLICY.retries
    assert BATCH_EMBED_POLICY.timeout > REALTIME_EMBED_POLICY.timeout


# ─── T2 ─────────────────────────────────────────────────────────────────────

def test_t2_retries_exhausted_records_durable_retryable_failure(monkeypatch):
    """Exhausted retries: keep the source, lose the embedding, explain it.

    The embedding may be missing. What must never happen is a missing embedding
    with no durable record of why — that is the silent hole.
    """
    _post_sequence(monkeypatch, [requests.Timeout("read timeout=10.0")])
    conn = _FakeConn()

    out = embed_for_write(
        "durable content", CFG,
        entity_table="conversation_stream", entity_id="live/s/2",
        phase="live_ingest", conn=conn, policy=DURABLE_WRITE_EMBED_POLICY,
    )

    assert out.vector is None
    assert out.status is EmbedOutcomeStatus.DEGRADED
    assert out.retryable is True, "a timeout is transient; repair must be able to retry"
    assert out.error_class == EmbedErrorClass.TIMEOUT.value
    assert out.attempts == DURABLE_WRITE_EMBED_POLICY.retries + 1
    assert out.marker_recorded is True, "no marker == silent NULL"

    ins = [e for e in conn.executed if "INSERT INTO public.embedding_failures" in e[0]]
    assert len(ins) == 1
    params = ins[0][1]
    # PHASE 8 fields the operator needs.
    for field in ("entity_table", "entity_id", "phase", "error_class", "retryable",
                  "attempts", "timeout_policy", "model", "model_fingerprint",
                  "error_fingerprint"):
        assert field in params, field
    assert params["entity_table"] == "conversation_stream"
    assert params["entity_id"] == "live/s/2"
    assert params["retryable"] is True
    assert params["timeout_policy"] == "durable_write"
    assert params["model"] == "BAAI/bge-m3"
    # PHASE 8 forbids storing credentials or raw text.
    blob = json.dumps(params, default=str)
    assert "test-key" not in blob
    assert "durable content" not in blob


def test_t2b_marker_write_failure_is_loud_not_silent(monkeypatch, caplog):
    """If the marker itself cannot be written, that must be an ERROR, not a shrug."""
    _post_sequence(monkeypatch, [requests.Timeout("t")])
    conn = _FakeConn()
    conn.fail_on_execute = True

    with caplog.at_level("ERROR"):
        out = embed_for_write(
            "content", CFG, entity_table="conversation_stream", entity_id="x",
            phase="live_ingest", conn=conn, policy=DURABLE_WRITE_EMBED_POLICY,
        )

    assert out.marker_recorded is False
    assert any("未被记录" in r.message or "silent NULL" in r.message
               for r in caplog.records), "an unrecorded failure must be loud"


# ─── T3 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cfg", [
    {"endpoint": "http://embed.test/v1/embeddings", "_fingerprint": "fp"},   # no model
    {"model": "BAAI/bge-m3", "_fingerprint": "fp"},                          # no endpoint
    {"endpoint": "http://embed.test/v1/embeddings", "model": "m"},           # no fingerprint
])
def test_t3_config_error_is_not_a_transient_null(monkeypatch, cfg):
    """A misconfiguration must surface as a config failure, never as a flaky NULL.

    This is the distinction PHASE 7 demands: "embedding operation fail-closed" and
    "source truth durability" are separate concerns. The source row may be written;
    the embedding failure must be labelled non-retryable so nobody later mistakes a
    missing API key for provider flakiness and waits for it to heal.
    """
    # If a request is even attempted, the test should fail loudly.
    def _boom(*a, **k):
        raise AssertionError("config error must not reach the network")

    monkeypatch.setattr(requests, "post", _boom)
    conn = _FakeConn()

    out = embed_for_write(
        "content", cfg, entity_table="conversation_stream", entity_id="cfg",
        phase="live_ingest", conn=conn, policy=DURABLE_WRITE_EMBED_POLICY,
    )

    assert out.status is EmbedOutcomeStatus.FAILED
    assert out.error_class == EmbedErrorClass.CONFIG.value
    assert out.retryable is False, "a config error must never be marked retryable"
    assert out.marker_recorded is True
    assert not conn.executed or "embedding_failures" in conn.executed[0][0]


def test_t3b_valueerror_still_propagates_for_callers_that_contract_on_it(monkeypatch):
    """`call_embedding` keeps raising ValueError for a config fault.

    Callers (and the pre-existing contract tests) rely on ValueError meaning "the
    embedding configuration itself is wrong". The hotfix must not silently convert
    that into a generic exception or a returned None.
    """
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(ValueError):
        call_embedding("x", {"endpoint": "http://e", "model": "", "_fingerprint": "f"})


# ─── T4 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [401, 403])
def test_t4_auth_failure_is_not_retried(monkeypatch, status):
    """401/403 are deterministic: retrying them wastes the budget and hammers auth.

    Before the fix every exception was retried identically, so an expired key
    burned three attempts per row across the whole ingest backlog.
    """
    calls = _post_sequence(monkeypatch, [_Resp(status=status, payload={"error": "denied"})])
    conn = _FakeConn()

    out = embed_for_write(
        "content", CFG, entity_table="conversation_stream", entity_id="auth",
        phase="live_ingest", conn=conn, policy=DURABLE_WRITE_EMBED_POLICY,
    )

    assert calls["n"] == 1, f"HTTP {status} must not be retried"
    assert out.error_class == EmbedErrorClass.AUTH.value
    assert out.retryable is False
    assert out.status is EmbedOutcomeStatus.FAILED
    assert out.attempts == 1


def test_t4b_bad_request_is_not_retried_but_server_error_is(monkeypatch):
    """Deterministic 4xx fails fast; 5xx is a transient worth retrying."""
    c1 = _post_sequence(monkeypatch, [_Resp(status=400, payload={"error": "bad"})])
    embed_for_write("c", CFG, entity_table="t", entity_id="1", phase="p",
                    conn=_FakeConn(), policy=DURABLE_WRITE_EMBED_POLICY)
    assert c1["n"] == 1, "400 must fail fast"

    c2 = _post_sequence(monkeypatch, [_Resp(status=503, payload={"error": "later"})])
    embed_for_write("c", CFG, entity_table="t", entity_id="2", phase="p",
                    conn=_FakeConn(), policy=DURABLE_WRITE_EMBED_POLICY)
    assert c2["n"] == DURABLE_WRITE_EMBED_POLICY.retries + 1, "503 must be retried"


def test_t4c_classification_table():
    """The retryable/non-retryable split is the contract other code reads."""
    assert classify_embed_error(requests.Timeout("t")).retryable is True
    assert classify_embed_error(requests.ConnectionError("c")).retryable is True
    assert classify_embed_error(ValueError("cfg")).retryable is False
    assert EmbedErrorClass.RATE_LIMITED.retryable is True
    assert EmbedErrorClass.SERVER_ERROR.retryable is True
    assert EmbedErrorClass.AUTH.retryable is False
    assert EmbedErrorClass.CONFIG.retryable is False
    assert EmbedErrorClass.BAD_REQUEST.retryable is False
    assert EmbedErrorClass.UNSUPPORTED_MODEL.retryable is False


def test_t4d_json_decode_error_is_transient_not_config(monkeypatch):
    """requests' JSONDecodeError subclasses ValueError — it must not read as config.

    Misclassifying it would make a provider-side glitch non-retryable.
    """
    err = requests.exceptions.JSONDecodeError("bad json", "", 0)
    assert classify_embed_error(err) is EmbedErrorClass.SERVER_ERROR
    assert classify_embed_error(err).retryable is True


# ─── T5 ─────────────────────────────────────────────────────────────────────

def test_t5_realtime_policy_stays_short(monkeypatch):
    """The realtime path must keep its short budget and never retry.

    PHASE 17: durable-write retry behaviour must not leak into the 8s user budget.
    """
    calls = _post_sequence(monkeypatch, [_Resp()])

    call_embedding("query", CFG, cache=False, policy=REALTIME_EMBED_POLICY)

    assert calls["n"] == 1
    assert calls["timeouts"][0] == REALTIME_EMBED_POLICY.timeout
    assert REALTIME_EMBED_POLICY.timeout <= 3.0
    assert REALTIME_EMBED_POLICY.retries == 0


def test_t5b_default_budget_is_realtime_not_durable(monkeypatch):
    """Omitting a policy must NOT silently grant a longer timeout.

    An un-migrated caller keeps today's behaviour; it does not accidentally get a
    10s×3 budget inside the user-facing path. This is the property that makes the
    migration safe to land incrementally.
    """
    calls = _post_sequence(monkeypatch, [_Resp()])

    call_embedding("query", CFG, cache=False)

    assert calls["timeouts"][0] == REALTIME_EMBED_POLICY.timeout


def test_t5c_realtime_failure_does_not_sleep_through_the_budget(monkeypatch):
    """A failing realtime embed must return immediately, not burn the 8s budget."""
    _post_sequence(monkeypatch, [requests.Timeout("t")])
    started = time.time()
    with pytest.raises(EmbeddingCallError):
        call_embedding("query", CFG, cache=False, policy=REALTIME_EMBED_POLICY)
    elapsed = time.time() - started
    assert elapsed < 1.0, f"realtime failure took {elapsed:.2f}s — must not retry/backoff"


def test_t5d_explicit_values_still_beat_the_policy(monkeypatch):
    """A caller with a genuinely dynamic deadline keeps control (prefetch case)."""
    calls = _post_sequence(monkeypatch, [_Resp()])
    call_embedding("q", CFG, cache=False, timeout=0.25, retries=0,
                   policy=REALTIME_EMBED_POLICY)
    assert calls["timeouts"][0] == 0.25


# ─── failure-accounting plumbing ────────────────────────────────────────────

def test_marker_upsert_is_idempotent_per_entity_phase():
    """Repeated failures bump attempts; they must not stack rows."""
    conn = _FakeConn()
    err = EmbeddingCallError(
        "boom", error_class=EmbedErrorClass.TIMEOUT, attempts=3, elapsed=1.5,
        policy=DURABLE_WRITE_EMBED_POLICY, model="m", fingerprint="fp", status=None,
    )
    assert record_embedding_failure(conn, entity_table="t", entity_id="1", phase="p",
                                    error=err) is True
    sql, params = conn.executed[0]
    assert "ON CONFLICT (entity_table, entity_id, phase) DO UPDATE" in sql
    assert params["attempts"] == 3
    assert params["retryable"] is True


def test_resolve_clears_the_marker_on_success():
    conn = _FakeConn()
    assert resolve_embedding_failure(conn, entity_table="t", entity_id="1",
                                     phase="p") is True
    sql, params = conn.executed[0]
    assert "resolved_at = now()" in sql
    assert params["resolution"] == "repaired"


def test_empty_source_is_not_recorded_as_an_embedding_failure():
    """An empty row never made a request, so it is not a lost embedding.

    Recording it would poison the very signal this accounting exists to protect.
    """
    conn = _FakeConn()
    out = embed_for_write("", CFG, entity_table="conversation_stream",
                          entity_id="empty", phase="live_ingest", conn=conn,
                          policy=DURABLE_WRITE_EMBED_POLICY)
    assert out.status is EmbedOutcomeStatus.DEGRADED
    assert out.error_class == "NO_INPUT"
    assert conn.executed == [], "no request happened, so there is nothing to explain"


def test_conn_factory_is_used_and_closed_when_no_conn_is_passed(monkeypatch):
    """Pool-backed stores cannot hand out a raw connection; the marker path must
    open its own and must close it."""
    _post_sequence(monkeypatch, [requests.Timeout("t")])
    made: list[_FakeConn] = []

    def factory():
        c = _FakeConn()
        made.append(c)
        return c

    out = embed_for_write("content", CFG, entity_table="t", entity_id="2",
                          phase="p", conn_factory=factory,
                          policy=DURABLE_WRITE_EMBED_POLICY)

    assert out.marker_recorded is True
    assert len(made) == 1
    assert made[0].closed is True, "a side connection must not be leaked"


def test_no_connection_available_is_reported_not_swallowed(monkeypatch, caplog):
    """If no connection can be obtained, the unrecorded failure must be an ERROR."""
    _post_sequence(monkeypatch, [requests.Timeout("t")])
    with caplog.at_level("ERROR"):
        out = embed_for_write("content", CFG, entity_table="t", entity_id="3",
                              phase="p", policy=DURABLE_WRITE_EMBED_POLICY)
    assert out.marker_recorded is False
    assert any("未被记录" in r.message or "silent NULL" in r.message
               for r in caplog.records)


# ─── Live-writer primary (5s/0) ────────────────────────────────────────────

def test_live_primary_policy_shape_and_durable_unchanged():
    """STREAM_PRIMARY is the named 5s/0; DURABLE stays 10s/2 for deferred paths.

    The §3 split only moves the conversation_stream live writer: the primary
    covers the observed 3.488s tail in one bounded shot, and the patient
    10s/2 retry remains available to deferred repair/backfill.
    """
    assert STREAM_PRIMARY_EMBED_POLICY.name == "stream_primary"
    assert STREAM_PRIMARY_EMBED_POLICY.timeout == 5.0
    assert STREAM_PRIMARY_EMBED_POLICY.retries == 0
    assert DURABLE_WRITE_EMBED_POLICY.name == "durable_write"
    assert DURABLE_WRITE_EMBED_POLICY.timeout == 10.0
    assert DURABLE_WRITE_EMBED_POLICY.retries == 2


def test_live_primary_failure_does_not_block_the_writer(monkeypatch):
    """A slow provider on the live writer: one 5s attempt, then marker + next item.

    No inline retry chain — that would head-of-line-block the single-threaded
    serial writer. The failure stays retryable so the deferred 10s/2 pass can
    repair it; the source row is never sacrificed for the embedding.
    """
    calls = _post_sequence(monkeypatch, [requests.Timeout("read timeout=5.0")])
    conn = _FakeConn()

    out = embed_for_write(
        "live content", CFG,
        entity_table="conversation_stream", entity_id="live/s/p",
        phase="live_ingest", conn=conn, policy=STREAM_PRIMARY_EMBED_POLICY,
    )

    assert calls["n"] == 1, "primary is single-shot — retry lives on the deferred path"
    assert calls["timeouts"][0] == STREAM_PRIMARY_EMBED_POLICY.timeout == 5.0
    assert out.vector is None
    assert out.status is EmbedOutcomeStatus.DEGRADED
    assert out.retryable is True, "a primary timeout must stay repairable by deferred retry"
    assert out.error_class == EmbedErrorClass.TIMEOUT.value
    assert out.attempts == 1
    assert out.marker_recorded is True

    ins = [e for e in conn.executed if "INSERT INTO public.embedding_failures" in e[0]]
    assert len(ins) == 1
    assert ins[0][1]["timeout_policy"] == "stream_primary"


def test_live_primary_covers_tail_but_stays_short(monkeypatch):
    """A 3.5s-class response (the old hole) must land on the primary first try.

    T1 pins durable retry; this pins the live writer's reason to exist: the
    response that used to become a permanent NULL under 3s/0 now succeeds
    without any retry, and still well under the deferred 10s budget.
    """
    calls = _post_sequence(monkeypatch, [_Resp()])
    conn = _FakeConn()

    out = embed_for_write(
        "live content", CFG,
        entity_table="conversation_stream", entity_id="live/s/q",
        phase="live_ingest", conn=conn, policy=STREAM_PRIMARY_EMBED_POLICY,
    )

    assert out.status is EmbedOutcomeStatus.OK
    assert out.vector == VEC
    assert calls["n"] == 1
    assert calls["timeouts"][0] == 5.0
    assert not [e for e in conn.executed if "INSERT INTO public.embedding_failures" in e[0]]
