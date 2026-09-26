"""Marker-ledger reader (DESIGN §7).

The reliability layer treats the ``<base>/j/pending_qa/*.json`` markers as
the single source of truth for *what's currently failing in the write
pipeline*. This module reads every marker, normalizes both field
generations (``v1_legacy`` and ``v2``) into a common record shape, and
emits a JSON-safe *ledger* with by-status / by-error-class counts plus
duplicate detection.

Hard contracts:

  1. **Never emit ``pending.q`` / ``pending.a`` / ``tool_calls`` /
     ``tool_results`` contents.** The ledger is a privacy surface; the
     tests pin this. (We only ever read the *id* field of ``pending``.)
  2. **One exception during read = one ``malformed`` counter bump.**
     We don't crash the whole scan because of one bad file.
  3. **Both field generations are accepted.** v2 adds ``error_class`` /
     ``error_phase`` / ``error_fingerprint`` / ``provider_status`` /
     ``retryable`` / ``first_failure_at`` / ``last_failure_at`` /
     ``failure_count``; v1_legacy uses ``embedding_error_fingerprint``
     and only has ``embedding_status`` / ``embedding_attempts`` /
     ``embedding_next_retry_at``. The reader picks whichever is present.
  4. **File size capped** at ``max_file_bytes`` (default 5 MB) — anything
     larger is treated as malformed and skipped.

Marker schema tolerance matrix (priority 1->8 — first match wins):

  1. unparseable / missing job_id / missing pending -> ``malformed``
  2. ``embedding_status == "recovered"``               -> ``recovered``
  3. ``embedding_status == "poisoned"``                -> ``poisoned``
  4. ``embedding_status == "failed"``                  -> ``retryable`` (recent)
                                                          or ``retry_due``  (stale)
  5. ``embedding_status == "in_flight"``               -> ``stale_in_flight`` (age > STALE)
                                                          or ``in_flight``        (else)
  6. ``embedding_status == "embedding_succeeded_pending_db"``
                                                          -> ``stale_pending_db``  (age > STALE)
                                                          or ``pending_db``         (else)
  7. no status and ``embedding_attempts == 0``         -> ``pending``
  8. otherwise                                         -> ``unresolved``
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Default time-window knobs — overridable in the constructor for tests.
STALE_HOURS: float = 48.0
RECENT_HOURS: float = 24.0
DEFAULT_MAX_FILE_BYTES: int = 5_000_000


# Status names — re-exported for callers (diagnose, health).
STATUS_MALFORMED = "malformed"
STATUS_RECOVERED = "recovered"
STATUS_POISONED = "poisoned"
STATUS_RETRYABLE = "retryable"
STATUS_RETRY_DUE = "retry_due"
STATUS_STALE_IN_FLIGHT = "stale_in_flight"
STATUS_IN_FLIGHT = "in_flight"
STATUS_STALE_PENDING_DB = "stale_pending_db"
STATUS_PENDING_DB = "pending_db"
STATUS_PENDING = "pending"
STATUS_UNRESOLVED = "unresolved"


# All recognized normalized statuses — used by health.py to seed the
# by_status counter so callers see zero-valued buckets, not just keys
# that happened to appear in a scan.
ALL_STATUSES: tuple[str, ...] = (
    STATUS_MALFORMED,
    STATUS_RECOVERED,
    STATUS_POISONED,
    STATUS_RETRYABLE,
    STATUS_RETRY_DUE,
    STATUS_STALE_IN_FLIGHT,
    STATUS_IN_FLIGHT,
    STATUS_STALE_PENDING_DB,
    STATUS_PENDING_DB,
    STATUS_PENDING,
    STATUS_UNRESOLVED,
)


@dataclass
class _MarkerRecord:
    """Internal per-marker view. Field names mirror DESIGN §7 verbatim so
    the JSON contract is discoverable from this file alone."""

    job_id: str
    session_id: str | None
    status: str           # normalized
    raw_status: str | None
    error_class: str | None
    error_fingerprint: str | None
    provider_status: int | None
    retryable: bool | None
    attempts: int | None
    first_failure_at: str | None
    last_failure_at: str | None
    next_retry_at: str | None
    file_mtime: float
    age_seconds: float
    is_recent: bool
    marker_generation: str  # "v1_legacy" | "v2"


class FailureReader:
    """Read marker files and emit a JSON-safe ledger."""

    def __init__(
        self,
        marker_dir: Path | None,
        *,
        now: float | None = None,
        stale_hours: float = STALE_HOURS,
        recent_hours: float = RECENT_HOURS,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ):
        self.marker_dir = Path(marker_dir) if marker_dir is not None else None
        self.now = float(now) if now is not None else time.time()
        self.stale_seconds = float(stale_hours) * 3600.0
        self.recent_seconds = float(recent_hours) * 3600.0
        self.max_file_bytes = int(max_file_bytes)

    # public
    def read(self) -> dict[str, Any]:
        """Scan the marker directory and return the ledger dict.

        The ledger is JSON-safe: every value is a primitive, list, or
        dict of primitives. The ``records`` list holds the per-marker
        views; ``duplicate_job_ids`` lists only the ``pending.q_msg_id``
        strings (no question / answer content).
        """
        t0 = time.time()
        if self.marker_dir is None or not self.marker_dir.exists():
            return self._empty_ledger(scan_seconds=(time.time() - t0))

        paths = sorted(self.marker_dir.glob("*.json"))
        records: list[tuple[Path, dict[str, Any]]] = []
        for p in paths:
            rec = self._read_one(p)
            if rec is not None:
                records.append((p, rec))

        # Duplicate detection — same pending.q_msg_id in >1 live (non-malformed)
        # marker. We store only the id string, never the question / answer.
        id_to_paths: dict[str, list[str]] = {}
        for p, r in records:
            if r.get("status") == STATUS_MALFORMED:
                continue
            qid = self._peek_q_msg_id(p)
            if isinstance(qid, str) and qid:
                id_to_paths.setdefault(qid, []).append(p.name)

        dup_ids = sorted(qid for qid, names in id_to_paths.items() if len(names) > 1)

        # Build the aggregate buckets.
        by_status: dict[str, int] = {s: 0 for s in ALL_STATUSES}
        by_error_class: dict[str, int] = {}
        recent_by_status: dict[str, int] = {s: 0 for s in ALL_STATUSES}
        recent_by_error_class: dict[str, int] = {}
        recent_count = 0
        stale_count = 0
        newest_mtime: float | None = None
        oldest_failure_at: str | None = None
        newest_failure_at: str | None = None

        for _p, r in records:
            st = r["status"]
            by_status[st] = by_status.get(st, 0) + 1
            if r["is_recent"]:
                recent_count += 1
                recent_by_status[st] = recent_by_status.get(st, 0) + 1
                if r["error_class"]:
                    recent_by_error_class[r["error_class"]] = (
                        recent_by_error_class.get(r["error_class"], 0) + 1
                    )
            if st in (STATUS_STALE_IN_FLIGHT, STATUS_STALE_PENDING_DB):
                stale_count += 1
            ec = r["error_class"]
            if ec:
                by_error_class[ec] = by_error_class.get(ec, 0) + 1
            mt = r["file_mtime"]
            if newest_mtime is None or mt > newest_mtime:
                newest_mtime = mt
            lf = r["last_failure_at"]
            if lf:
                # ISO strings sort lexicographically when UTC-normalized.
                if oldest_failure_at is None or lf < oldest_failure_at:
                    oldest_failure_at = lf
                if newest_failure_at is None or lf > newest_failure_at:
                    newest_failure_at = lf

        # Drop the leading path companion — records list is the contract.
        records_only = [r for _p, r in records]

        return {
            "total": len(records_only),
            "by_status": by_status,
            "by_error_class": by_error_class,
            "recent_count": recent_count,
            "recent_by_status": recent_by_status,
            "recent_by_error_class": recent_by_error_class,
            "malformed": by_status.get(STATUS_MALFORMED, 0),
            "stale": stale_count,
            "duplicates": len(dup_ids),
            "duplicate_job_ids": dup_ids,
            "newest_mtime": newest_mtime,
            "oldest_failure_at": oldest_failure_at,
            "newest_failure_at": newest_failure_at,
            "marker_dir": str(self.marker_dir) if self.marker_dir is not None else None,
            "marker_dir_label": {
                "kind": "marker_dir",
                "leaf": self.marker_dir.name if self.marker_dir is not None else "",
                "hash12": "",
            },
            "scan_seconds": time.time() - t0,
            "records": records_only,
        }

    # internals
    def _empty_ledger(self, *, scan_seconds: float) -> dict[str, Any]:
        return {
            "total": 0,
            "by_status": {s: 0 for s in ALL_STATUSES},
            "by_error_class": {},
            "recent_count": 0,
            "recent_by_status": {s: 0 for s in ALL_STATUSES},
            "recent_by_error_class": {},
            "malformed": 0,
            "stale": 0,
            "duplicates": 0,
            "duplicate_job_ids": [],
            "newest_mtime": None,
            "oldest_failure_at": None,
            "newest_failure_at": None,
            "marker_dir": str(self.marker_dir) if self.marker_dir is not None else None,
            "marker_dir_label": {
                "kind": "marker_dir",
                "leaf": self.marker_dir.name if self.marker_dir is not None else "",
                "hash12": "",
            },
            "scan_seconds": float(scan_seconds),
            "records": [],
        }

    def _read_one(self, p: Path) -> dict[str, Any] | None:
        """Read one marker file. Any exception bumps malformed, returns None."""
        try:
            st = p.stat()
        except OSError:
            return self._malformed_record(p, file_mtime=None, reason="stat_error")

        if st.st_size > self.max_file_bytes:
            return self._malformed_record(p, file_mtime=st.st_mtime, reason="oversize")

        try:
            raw = p.read_bytes()
        except OSError as exc:
            return self._malformed_record(p, file_mtime=st.st_mtime, reason=f"read_error:{exc.__class__.__name__}")

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            return self._malformed_record(p, file_mtime=st.st_mtime, reason=f"json_error:{exc.msg}")

        if not isinstance(data, dict):
            return self._malformed_record(p, file_mtime=st.st_mtime, reason="not_object")

        job_id = data.get("job_id")
        pending = data.get("pending")
        if not isinstance(job_id, str) or not job_id:
            return self._malformed_record(p, file_mtime=st.st_mtime, reason="missing_job_id")
        if not isinstance(pending, dict):
            return self._malformed_record(p, file_mtime=st.st_mtime, reason="missing_pending")

        # Generation + field coalescing (v1_legacy / v2 compatibility).
        generation = "v2" if "error_class" in data else "v1_legacy"
        error_class = data.get("error_class")
        error_fingerprint = (
            data.get("error_fingerprint")
            or data.get("embedding_error_fingerprint")
        )
        provider_status = data.get("provider_status")
        retryable = data.get("retryable")
        attempts_raw = data.get("embedding_attempts")
        attempts = attempts_raw if isinstance(attempts_raw, int) else None
        first_failure_at = data.get("first_failure_at")
        last_failure_at_raw = data.get("last_failure_at")
        next_retry_at = data.get("embedding_next_retry_at")
        raw_status = data.get("embedding_status")
        session_id = data.get("session_id")

        # Recent window — defined by last_failure_at if parseable, else
        # fall back to file mtime. The status field only matters for
        # bucketing; the recency check is purely temporal.
        lf_epoch: float | None = None
        if isinstance(last_failure_at_raw, str):
            lf_epoch = _iso_to_epoch(last_failure_at_raw)
        if lf_epoch is None:
            lf_epoch = st.st_mtime
        age = self.now - st.st_mtime
        is_recent = lf_epoch >= (self.now - self.recent_seconds)

        # next_retry_at may be an epoch float (production markers write
        # time.time()-based values). Normalize once for both the record
        # and the status bucketing.
        nra_epoch: float | None = None
        if isinstance(next_retry_at, (int, float)) and not isinstance(next_retry_at, bool):
            nra_epoch = float(next_retry_at)
        elif isinstance(next_retry_at, str):
            try:
                nra_epoch = float(next_retry_at)
            except ValueError:
                nra_epoch = None

        # Normalize the status per DESIGN §7 priority 1->8.
        status = self._normalize_status(
            raw_status=raw_status,
            attempts=attempts,
            age_seconds=age,
            next_retry_at=nra_epoch,
        )

        return {
            "job_id": job_id,
            "session_id": session_id if isinstance(session_id, str) else None,
            "status": status,
            "raw_status": raw_status if isinstance(raw_status, str) else None,
            "error_class": error_class if isinstance(error_class, str) else None,
            "error_fingerprint": error_fingerprint if isinstance(error_fingerprint, str) else None,
            "provider_status": provider_status if isinstance(provider_status, int) else None,
            "retryable": retryable if isinstance(retryable, bool) else None,
            "attempts": attempts,
            "first_failure_at": first_failure_at if isinstance(first_failure_at, str) else None,
            "last_failure_at": _iso_from_mtime(lf_epoch),
            "next_retry_at": (
                float(next_retry_at)
                if isinstance(next_retry_at, (int, float)) and not isinstance(next_retry_at, bool)
                else next_retry_at if isinstance(next_retry_at, str) else None
            ),
            "file_mtime": st.st_mtime,
            "age_seconds": age,
            "is_recent": bool(is_recent),
            "marker_generation": generation,
        }

    def _normalize_status(
        self,
        *,
        raw_status: Any,
        attempts: int | None,
        age_seconds: float,
        next_retry_at: float | None = None,
    ) -> str:
        # Priority 1: malformed is decided in _read_one (before we get here).
        if raw_status == "recovered":
            return STATUS_RECOVERED
        if raw_status == "poisoned":
            return STATUS_POISONED
        if raw_status == "failed":
            # retryable vs retry_due — a scheduled retry still in the
            # future means the system is backing off and WILL retry
            # ("retryable"). Once the scheduled moment has passed, the
            # system owed a retry it has not performed ("retry_due").
            # Legacy markers without a schedule fall back to the age
            # heuristic: within the recent window = retryable, older =
            # retry_due.
            if next_retry_at is not None:
                return STATUS_RETRYABLE if next_retry_at > self.now else STATUS_RETRY_DUE
            if age_seconds < self.recent_seconds:
                return STATUS_RETRYABLE
            return STATUS_RETRY_DUE
        if raw_status == "in_flight":
            return STATUS_STALE_IN_FLIGHT if age_seconds > self.stale_seconds else STATUS_IN_FLIGHT
        if raw_status == "embedding_succeeded_pending_db":
            return STATUS_STALE_PENDING_DB if age_seconds > self.stale_seconds else STATUS_PENDING_DB
        # Priority 7: no status and attempts==0 -> pending.
        if raw_status is None and attempts == 0:
            return STATUS_PENDING
        # Priority 8: fallback.
        return STATUS_UNRESOLVED

    def _malformed_record(self, p: Path, *, file_mtime: float | None, reason: str) -> dict[str, Any]:
        return {
            "job_id": p.name,
            "session_id": None,
            "status": STATUS_MALFORMED,
            "raw_status": None,
            "error_class": None,
            "error_fingerprint": None,
            "provider_status": None,
            "retryable": None,
            "attempts": None,
            "first_failure_at": None,
            "last_failure_at": None,
            "next_retry_at": None,
            "file_mtime": file_mtime if file_mtime is not None else 0.0,
            "age_seconds": (self.now - file_mtime) if file_mtime else 0.0,
            "is_recent": False,
            "marker_generation": "v1_legacy",
            "malformed_reason": reason,
        }

    def _peek_q_msg_id(self, p: Path) -> str | None:
        """Read just the q_msg_id from a marker file. Used only for
        duplicate detection — no other pending field is touched."""
        try:
            raw = p.read_bytes()
            if len(raw) > self.max_file_bytes:
                return None
            data = json.loads(raw)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        pending = data.get("pending")
        if not isinstance(pending, dict):
            return None
        qid = pending.get("q_msg_id")
        return qid if isinstance(qid, str) else None


# helpers


def _iso_to_epoch(s: str) -> float | None:
    """Parse a subset of ISO8601 (the shape our writers emit) into epoch.
    Returns None if unparseable — caller falls back to mtime."""
    if not isinstance(s, str):
        return None
    ss = s.strip()
    if ss.endswith("Z"):
        ss = ss[:-1] + "+00:00"
    try:
        from datetime import datetime
        return datetime.fromisoformat(ss).timestamp()
    except Exception:
        return None


def _iso_from_mtime(epoch: float) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except Exception:  # pragma: no cover — extreme values
        return ""