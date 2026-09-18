from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from v3core import V3Core


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status) if status is not None else None


@pytest.fixture
def core(tmp_path: Path):
    obj = V3Core.__new__(V3Core)
    obj._qa_durability_lock = threading.RLock()
    obj._qa_pending_dir = lambda: tmp_path / "pending_qa"
    return obj


def _pending():
    return {"q": "question", "a": "answer", "q_msg_id": "m1", "q_turn": "t1"}


def _marker(obj, session="s1") -> Path:
    return next(obj._qa_pending_dir().glob("*.json"))


@pytest.mark.parametrize(
    ("error", "phase", "error_class", "retryable"),
    [
        (ProviderError("unauthorized", 401), "embedding", "provider_401", False),
        (ProviderError("rate limited", 429), "embedding", "provider_429", True),
        (ProviderError("server", 500), "embedding", "provider_5xx", True),
        (ProviderError("unavailable", 503), "embedding", "provider_5xx", True),
        (TimeoutError("timed out"), "embedding", "timeout", True),
        (ConnectionError("connection reset"), "embedding", "connection_error", True),
        (RuntimeError("tokenizer unavailable"), "tokenizer", "tokenizer_unavailable", True),
        (ValueError("provider input length exceeded"), "embedding", "provider_input_over_limit", False),
        (RuntimeError("child 2 failed"), "chunk_embedding", "chunk_embedding_failure", True),
        (RuntimeError("database write failed"), "db_write", "db_write_failure", True),
        (RuntimeError("database unavailable"), "db_unavailable", "db_unavailable", True),
    ],
)
def test_failure_classes_are_durable_and_restart_visible(
    core, error, phase, error_class, retryable
):
    pending = _pending()
    core._mark_qa_embedding_started("s1", pending)
    core._mark_qa_embedding_failure("s1", pending, error, phase=phase)

    data = json.loads(_marker(core).read_text(encoding="utf-8"))
    assert data["embedding_status"] in {"failed", "poisoned"}
    assert data["error_class"] == error_class
    assert data["error_phase"] == phase
    assert data["retryable"] is retryable
    assert data["embedding_attempts"] == 1
    assert data["failure_count"] == 1
    assert data["first_failure_at"]
    assert data["last_failure_at"]
    assert len(data["error_fingerprint"]) == 64
    assert "unauthorized" not in data["error_fingerprint"]

    # A new process reading the same marker sees the complete accounting state.
    restarted = json.loads(_marker(core).read_text(encoding="utf-8"))
    assert restarted["error_class"] == error_class
    assert restarted["retryable"] is retryable


def test_in_flight_marker_survives_provider_process_crash(core):
    pending = _pending()
    core._mark_qa_embedding_started("s1", pending)
    data = json.loads(_marker(core).read_text(encoding="utf-8"))
    assert data["embedding_status"] == "in_flight"
    assert data["embedding_attempts"] == 1
    assert data["embedding_attempt_started_at"]
    assert "error_class" not in data


def test_non_retryable_failure_is_quarantined_and_not_requeued(core):
    pending = _pending()
    core._mark_qa_embedding_started("s1", pending)
    core._mark_qa_embedding_failure(
        "s1", pending, ProviderError("bad key", 401), phase="embedding"
    )
    data = json.loads(_marker(core).read_text(encoding="utf-8"))
    assert data["embedding_status"] == "poisoned"
    assert data["embedding_next_retry_at"] is None
    assert data["retryable"] is False


def test_retryable_failure_reaches_poisoned_after_three_attempts(core):
    pending = _pending()
    for _ in range(3):
        core._mark_qa_embedding_started("s1", pending)
        core._mark_qa_embedding_failure(
            "s1", pending, ProviderError("temporary", 503), phase="embedding"
        )
    data = json.loads(_marker(core).read_text(encoding="utf-8"))
    assert data["embedding_attempts"] == 3
    assert data["failure_count"] == 3
    assert data["embedding_status"] == "poisoned"
    assert data["retryable"] is True
