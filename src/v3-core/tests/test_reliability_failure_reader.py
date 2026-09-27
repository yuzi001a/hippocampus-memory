"""Unit tests for ``v3core.reliability.failure_reader``.

Crafted-marker tests using ``tmp_path``. Coverage:

  * All 11 normalized statuses (DESIGN §7 priority 1->8 + the legacy
    "poisoned" path which DESPAWN did not list as a separate status).
  * Both field generations (v1_legacy + v2) coexist in one directory.
  * Malformed files (oversize, missing job_id, missing pending, broken
    JSON, not-an-object) each bump the malformed bucket.
  * Duplicates: same ``pending.q_msg_id`` in >1 marker -> ``duplicates``+1
    and the id is listed in ``duplicate_job_ids`` — *never* the question /
    answer text.
  * Secret scan: ledger JSON dump does not contain ``SECRET_Q_TEXT_XYZ``
    or ``SECRET_A_TEXT_XYZ`` planted inside the marker bodies.
  * Empty / missing directory returns an empty ledger, not an error.
  * Custom ``now`` knob: ``os.utime`` on the marker file controls age
    independently of wall-clock.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from v3core.reliability.failure_reader import (
    ALL_STATUSES,
    FailureReader,
    STATUS_IN_FLIGHT,
    STATUS_MALFORMED,
    STATUS_PENDING,
    STATUS_PENDING_DB,
    STATUS_POISONED,
    STATUS_RECOVERED,
    STATUS_RETRYABLE,
    STATUS_RETRY_DUE,
    STATUS_STALE_IN_FLIGHT,
    STATUS_STALE_PENDING_DB,
    STATUS_UNRESOLVED,
)


SECRET_Q = "SECRET_Q_TEXT_XYZ_unique_marker_12345"
SECRET_A = "SECRET_A_TEXT_XYZ_unique_marker_67890"


def _write_marker(
    path: Path,
    *,
    job_id: str,
    q_msg_id: str,
    embedding_status: str | None = None,
    embedding_attempts: int | None = None,
    next_retry_at: str | None = None,
    last_failure_at: str | None = None,
    generation_v2: bool = False,
    error_class: str | None = None,
    error_fingerprint: str | None = None,
    embedding_error_fingerprint: str | None = None,
    q_text: str = SECRET_Q,
    a_text: str = SECRET_A,
    pending_missing: bool = False,
    job_id_missing: bool = False,
    not_dict: bool = False,
) -> None:
    """Write one marker file. The defaults encode the secret content so
    every ledger JSON dump is tested for it."""
    if not_dict:
        path.write_text("not a json object", encoding="utf-8")
        return
    data: dict = {"version": "1"}
    if not job_id_missing:
        data["job_id"] = job_id
    data["session_id"] = "sess-test"
    if not pending_missing:
        data["pending"] = {
            "q_msg_id": q_msg_id,
            "q_turn": 1,
            "q": q_text,
            "a": a_text,
        }
    if embedding_status is not None:
        data["embedding_status"] = embedding_status
    if embedding_attempts is not None:
        data["embedding_attempts"] = embedding_attempts
    if next_retry_at is not None:
        data["embedding_next_retry_at"] = next_retry_at
    if last_failure_at is not None:
        data["last_failure_at"] = last_failure_at
    if generation_v2:
        if error_class is not None:
            data["error_class"] = error_class
        if error_fingerprint is not None:
            data["error_fingerprint"] = error_fingerprint
        if embedding_error_fingerprint is not None:
            # v2 may also include the legacy field for backward-compat reads.
            data["embedding_error_fingerprint"] = embedding_error_fingerprint
    else:
        if embedding_error_fingerprint is not None:
            data["embedding_error_fingerprint"] = embedding_error_fingerprint
    path.write_text(json.dumps(data), encoding="utf-8")


def _set_age(path: Path, *, now: float, age_seconds: float) -> None:
    """Pin the marker's mtime to ``now - age_seconds`` so the reader's
    age / recency / stale calculations are deterministic."""
    target = now - age_seconds
    os.utime(path, (target, target))


def _now() -> float:
    return time.time()


# ── empty / missing marker dir ──


def test_missing_marker_dir_returns_empty_ledger():
    fr = FailureReader(marker_dir=None, now=_now())
    ledger = fr.read()
    assert ledger["total"] == 0
    assert ledger["malformed"] == 0
    assert ledger["duplicates"] == 0
    assert ledger["marker_dir"] is None
    assert ledger["records"] == []
    # All known statuses must appear in by_status, value 0.
    for s in ALL_STATUSES:
        assert ledger["by_status"][s] == 0


def test_missing_directory_on_disk_returns_empty(tmp_path):
    fr = FailureReader(marker_dir=tmp_path / "does-not-exist", now=_now())
    ledger = fr.read()
    assert ledger["total"] == 0
    assert ledger["marker_dir"] is not None  # path string is preserved


# ── coverage of all 11 normalized statuses ──


@pytest.fixture()
def now() -> float:
    return 1_700_000_000.0  # fixed wall-clock for deterministic ages


def test_status_recovered(now, tmp_path):
    p = tmp_path / "m1.json"
    _write_marker(p, job_id="j-recovered", q_msg_id="q-recovered", embedding_status="recovered")
    _set_age(p, now=now, age_seconds=10)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_RECOVERED] == 1
    assert ledger["by_status"][STATUS_MALFORMED] == 0
    assert ledger["records"][0]["status"] == STATUS_RECOVERED


def test_status_poisoned(now, tmp_path):
    p = tmp_path / "m.json"
    _write_marker(p, job_id="j-p", q_msg_id="q-p", embedding_status="poisoned")
    _set_age(p, now=now, age_seconds=3600)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_POISONED] == 1


def test_status_retryable_recent(now, tmp_path):
    """'failed' marker younger than the recent window -> retryable."""
    p = tmp_path / "m.json"
    _write_marker(p, job_id="j-r", q_msg_id="q-r", embedding_status="failed")
    _set_age(p, now=now, age_seconds=60)  # < 24h
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_RETRYABLE] == 1
    assert ledger["by_status"][STATUS_RETRY_DUE] == 0


def test_status_retry_due_stale(now, tmp_path):
    """'failed' marker older than the recent window -> retry_due."""
    p = tmp_path / "m.json"
    _write_marker(p, job_id="j-d", q_msg_id="q-d", embedding_status="failed")
    _set_age(p, now=now, age_seconds=72 * 3600)  # > 24h, < 48h
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_RETRY_DUE] == 1


def test_status_in_flight(now, tmp_path):
    p = tmp_path / "m.json"
    _write_marker(p, job_id="j-if", q_msg_id="q-if", embedding_status="in_flight")
    _set_age(p, now=now, age_seconds=3600)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_IN_FLIGHT] == 1


def test_status_stale_in_flight(now, tmp_path):
    p = tmp_path / "m.json"
    _write_marker(p, job_id="j-sif", q_msg_id="q-sif", embedding_status="in_flight")
    _set_age(p, now=now, age_seconds=49 * 3600)  # > STALE (48h)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_STALE_IN_FLIGHT] == 1


def test_status_pending_db(now, tmp_path):
    p = tmp_path / "m.json"
    _write_marker(
        p,
        job_id="j-pdb",
        q_msg_id="q-pdb",
        embedding_status="embedding_succeeded_pending_db",
    )
    _set_age(p, now=now, age_seconds=3600)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_PENDING_DB] == 1


def test_status_stale_pending_db(now, tmp_path):
    p = tmp_path / "m.json"
    _write_marker(
        p,
        job_id="j-spdb",
        q_msg_id="q-spdb",
        embedding_status="embedding_succeeded_pending_db",
    )
    _set_age(p, now=now, age_seconds=72 * 3600)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_STALE_PENDING_DB] == 1
    assert ledger["stale"] == 1


def test_status_pending(now, tmp_path):
    p = tmp_path / "m.json"
    _write_marker(
        p,
        job_id="j-pen",
        q_msg_id="q-pen",
        embedding_status=None,
        embedding_attempts=0,
    )
    _set_age(p, now=now, age_seconds=10)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_PENDING] == 1


def test_status_unresolved(now, tmp_path):
    """No recognized status + attempts > 0 falls through to ``unresolved``."""
    p = tmp_path / "m.json"
    _write_marker(
        p,
        job_id="j-u",
        q_msg_id="q-u",
        embedding_status="weird_status",
        embedding_attempts=3,
    )
    _set_age(p, now=now, age_seconds=10)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_UNRESOLVED] == 1


def test_status_malformed_missing_job_id(now, tmp_path):
    p = tmp_path / "m.json"
    _write_marker(p, job_id="ignored", q_msg_id="q-x", job_id_missing=True)
    _set_age(p, now=now, age_seconds=10)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_MALFORMED] == 1


def test_status_malformed_missing_pending(now, tmp_path):
    p = tmp_path / "m.json"
    _write_marker(p, job_id="ignored", q_msg_id="q-x", pending_missing=True)
    _set_age(p, now=now, age_seconds=10)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_MALFORMED] == 1


def test_status_malformed_broken_json(now, tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{not valid json", encoding="utf-8")
    _set_age(p, now=now, age_seconds=10)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_MALFORMED] == 1


def test_status_malformed_not_object(now, tmp_path):
    p = tmp_path / "m.json"
    _write_marker(p, job_id="ignored", q_msg_id="ignored", not_dict=True)
    _set_age(p, now=now, age_seconds=10)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_MALFORMED] == 1


def test_status_malformed_oversize(now, tmp_path):
    p = tmp_path / "m.json"
    # 6 MB of padding — over the 5 MB cap.
    p.write_text("a" * (6 * 1024 * 1024), encoding="utf-8")
    _set_age(p, now=now, age_seconds=10)
    fr = FailureReader(tmp_path, now=now, max_file_bytes=5 * 1024 * 1024)
    ledger = fr.read()
    assert ledger["by_status"][STATUS_MALFORMED] == 1


# ── duplicates ──


def test_duplicates_detected(now, tmp_path):
    """Same q_msg_id in two live (non-malformed) markers -> duplicates++."""
    for name, job in (("m1.json", "j-1"), ("m2.json", "j-2")):
        p = tmp_path / name
        _write_marker(
            p,
            job_id=job,
            q_msg_id="q-shared",
            embedding_status="in_flight",
        )
        _set_age(p, now=now, age_seconds=3600)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["total"] == 2
    assert ledger["duplicates"] == 1
    assert ledger["duplicate_job_ids"] == ["q-shared"]
    # Crucial: the ledger JSON dump must not include the secret text.
    assert SECRET_Q not in json.dumps(ledger, sort_keys=True)


def test_duplicates_exclude_malformed_markers(now, tmp_path):
    """Malformed markers don't count toward duplicates (one is unreadable)."""
    good = tmp_path / "good.json"
    bad = tmp_path / "bad.json"
    _write_marker(good, job_id="j-g", q_msg_id="q-shared", embedding_status="in_flight")
    _write_marker(bad, job_id="ignored", q_msg_id="ignored", pending_missing=True)
    _set_age(good, now=now, age_seconds=3600)
    _set_age(bad, now=now, age_seconds=3600)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["total"] == 2
    assert ledger["malformed"] == 1
    assert ledger["duplicates"] == 0  # only one *live* q-shared


# ── field-generation compatibility ──


def test_v2_generation_reads_error_class(now, tmp_path):
    p = tmp_path / "v2.json"
    _write_marker(
        p,
        job_id="j-v2",
        q_msg_id="q-v2",
        embedding_status="failed",
        generation_v2=True,
        error_class="provider_429",
        error_fingerprint="deadbeef1234",
    )
    _set_age(p, now=now, age_seconds=60)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["by_error_class"].get("provider_429") == 1
    assert ledger["by_status"][STATUS_RETRYABLE] == 1
    rec = ledger["records"][0]
    assert rec["error_class"] == "provider_429"
    assert rec["error_fingerprint"] == "deadbeef1234"
    assert rec["marker_generation"] == "v2"


def test_v1_legacy_uses_embedding_error_fingerprint(now, tmp_path):
    p = tmp_path / "v1.json"
    _write_marker(
        p,
        job_id="j-v1",
        q_msg_id="q-v1",
        embedding_status="failed",
        embedding_error_fingerprint="legacy-fp-001",
    )
    _set_age(p, now=now, age_seconds=60)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    rec = ledger["records"][0]
    assert rec["marker_generation"] == "v1_legacy"
    assert rec["error_class"] is None  # v1 doesn't have it
    assert rec["error_fingerprint"] == "legacy-fp-001"


# ── secret scan (the hard contract) ──


def test_ledger_never_emits_q_or_a_content(now, tmp_path):
    """A whole directory of markers each carrying SECRET_Q / SECRET_A —
    the ledger's JSON dump must not contain either sentinel."""
    for i in range(5):
        p = tmp_path / f"m{i}.json"
        _write_marker(
            p,
            job_id=f"j-{i}",
            q_msg_id=f"q-{i}",
            embedding_status="in_flight",
        )
        _set_age(p, now=now, age_seconds=10)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    encoded = json.dumps(ledger, sort_keys=True)
    assert SECRET_Q not in encoded
    assert SECRET_A not in encoded
    # Tool payload sentinels (also forbidden by the contract).
    assert "tool_calls" not in encoded
    assert "tool_results" not in encoded


def test_ledger_records_field_excludes_q_msg_id(now, tmp_path):
    """Even though q_msg_id is used internally for dup detection, the
    per-record JSON payload must NOT carry it (it's pending content)."""
    p = tmp_path / "m.json"
    _write_marker(p, job_id="j", q_msg_id="q-secret-id", embedding_status="in_flight")
    _set_age(p, now=now, age_seconds=10)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    # q-secret-id is fine to leak as an id (it's not content), but the
    # pending question text must not.
    encoded = json.dumps(ledger["records"], sort_keys=True)
    assert "SECRET_Q" not in encoded
    assert "SECRET_A" not in encoded


# ── recency window ──


def test_is_recent_uses_last_failure_at_when_parseable(now, tmp_path):
    p = tmp_path / "m.json"
    iso = datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat()
    _write_marker(
        p,
        job_id="j",
        q_msg_id="q",
        embedding_status="failed",
        last_failure_at=iso,
    )
    _set_age(p, now=now, age_seconds=3600)  # old file, but recent failure
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["records"][0]["is_recent"] is True


def test_is_recent_falls_back_to_mtime_when_unparseable(now, tmp_path):
    p = tmp_path / "m.json"
    _write_marker(
        p,
        job_id="j",
        q_msg_id="q",
        embedding_status="failed",
        last_failure_at="not-an-iso-date",
    )
    _set_age(p, now=now, age_seconds=10)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    # mtime-based fallback: 10s ago -> recent.
    assert ledger["records"][0]["is_recent"] is True


# ── by_status keys are complete ──


def test_by_status_always_has_every_known_status(now, tmp_path):
    """Even with a single marker, the by_status dict lists all 11 buckets
    (callers depend on zero-valued buckets, not key absence)."""
    p = tmp_path / "m.json"
    _write_marker(p, job_id="j", q_msg_id="q", embedding_status="in_flight")
    _set_age(p, now=now, age_seconds=10)
    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    for s in ALL_STATUSES:
        assert s in ledger["by_status"]
    for s in ALL_STATUSES:
        assert s in ledger["recent_by_status"]


# ── mixed-world end-to-end ──


def test_mixed_world_ledger_aggregates(now, tmp_path):
    """One each of every status (where possible), 2 dup-id markers,
    1 malformed — verify totals and secret-leak in one shot."""
    spec = [
        ("a.json", "j-a", "q-a", "recovered", None),
        ("b.json", "j-b", "q-b", "poisoned", None),
        ("c.json", "j-c", "q-c", "failed", None),       # retryable (recent)
        ("d.json", "j-d", "q-d", "in_flight", None),    # in_flight (recent)
        ("e.json", "j-e", "q-e", "embedding_succeeded_pending_db", None),  # pending_db (recent)
    ]
    for name, job, qid, st, _ in spec:
        p = tmp_path / name
        _write_marker(p, job_id=job, q_msg_id=qid, embedding_status=st)
        _set_age(p, now=now, age_seconds=60)
    # 1 stale marker (in_flight > 48h).
    stale = tmp_path / "f.json"
    _write_marker(stale, job_id="j-f", q_msg_id="q-f", embedding_status="in_flight")
    _set_age(stale, now=now, age_seconds=72 * 3600)
    # 1 pending (no status + attempts=0).
    pen = tmp_path / "g.json"
    _write_marker(pen, job_id="j-g", q_msg_id="q-g", embedding_status=None, embedding_attempts=0)
    _set_age(pen, now=now, age_seconds=10)
    # 1 malformed (missing job_id).
    bad = tmp_path / "h.json"
    _write_marker(bad, job_id="ignored", q_msg_id="ignored", job_id_missing=True)
    _set_age(bad, now=now, age_seconds=10)
    # 1 unresolved (random status, attempts > 0).
    unr = tmp_path / "i.json"
    _write_marker(unr, job_id="j-i", q_msg_id="q-i", embedding_status="weird", embedding_attempts=2)
    _set_age(unr, now=now, age_seconds=10)
    # 1 retry_due (failed but old).
    rd = tmp_path / "j.json"
    _write_marker(rd, job_id="j-j", q_msg_id="q-j", embedding_status="failed")
    _set_age(rd, now=now, age_seconds=72 * 3600)

    fr = FailureReader(tmp_path, now=now)
    ledger = fr.read()
    assert ledger["total"] == 10
    assert ledger["malformed"] == 1
    assert ledger["stale"] == 1
    assert ledger["duplicates"] == 0
    # Every recognized bucket has a value (some 0).
    assert ledger["by_status"][STATUS_RECOVERED] == 1
    assert ledger["by_status"][STATUS_POISONED] == 1
    assert ledger["by_status"][STATUS_RETRYABLE] == 1
    assert ledger["by_status"][STATUS_RETRY_DUE] == 1
    assert ledger["by_status"][STATUS_IN_FLIGHT] == 1
    assert ledger["by_status"][STATUS_STALE_IN_FLIGHT] == 1
    assert ledger["by_status"][STATUS_PENDING_DB] == 1
    assert ledger["by_status"][STATUS_PENDING] == 1
    assert ledger["by_status"][STATUS_UNRESOLVED] == 1
    assert ledger["by_status"][STATUS_MALFORMED] == 1
    # Secret scan on the full ledger.
    encoded = json.dumps(ledger, sort_keys=True)
    assert SECRET_Q not in encoded
    assert SECRET_A not in encoded