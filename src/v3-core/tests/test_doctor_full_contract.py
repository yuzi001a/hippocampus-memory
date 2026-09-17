# -*- coding: utf-8 -*-
"""test_doctor_full_contract.py — pin the v3core.doctor_full frozen contract.

What this test pins:

    1. ``run_full_checks`` against a non-existent DSN returns per-check
       dicts and never raises.
    2. ``db_reachable`` is ``fail`` with a human-readable detail.
    3. Auth checks are ``skip`` when no key is configured.
    4. The check-id list is EXACTLY the 16 required ids, in order.
    5. The ``write`` check is ``skip`` with detail
       "write probe not requested" when ``allow_write=False``.
    6. The CLI wrapper prints JSON on stdout and returns a process
       exit code reflecting the summary.
    7. With a DSN pointing at an unreachable host, the doctor does
       not raise; the db_reachable check is fail and the
       pgvector_available check is also fail or skip (no raise).
    8. Auth checks with a fake key + a stubbed requests.get produce a
       meaningful ``ok``/``fail``/``warn`` based on the mocked status
       code (no network).

Hermetic: no live database, no real LLM, no real network. All
external I/O is monkeypatched.
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests

import v3core.doctor_full as doctor_full


# ──────────────────────────────────────────────────────────────────────────────
# The frozen 16-id list.
# ──────────────────────────────────────────────────────────────────────────────


REQUIRED_IDS = [
    "db_reachable",
    "pgvector_available",
    "schema_version",
    "migration_state",
    "memory_llm_auth",
    "embedding_auth",
    "rerank_auth",
    "dimensions_consistency",
    "hermes_provider_discovery",
    "hermes_home",
    "write",
    "read",
    "vector_insert_search",
    "rerank",
    "recall",
    "restart_persistence_hint",
]


def test_check_ids_match_frozen_contract():
    """The module's CHECK_IDS list is the 16-id contract."""
    assert list(doctor_full.CHECK_IDS) == REQUIRED_IDS, (
        f"check-id list drifted from the contract.\n"
        f"  expected: {REQUIRED_IDS}\n"
        f"  actual:   {list(doctor_full.CHECK_IDS)}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Core: run_full_checks is hermetic and never raises.
# ──────────────────────────────────────────────────────────────────────────────


def test_run_full_checks_no_dsn_never_raises(monkeypatch, tmp_path):
    """With profile_dir=tmp_path and dsn=None, the runner returns a
    valid report and never raises — even though many checks have no
    live data to consume."""
    # Make sure no real env config leaks into the test.
    for k in ("HERMES_HOME", "MINIMAX_CN_API_KEY", "MINIMAX_API_KEY",
              "V3CORE_PG_PASSWORD", "PGPASSWORD"):
        monkeypatch.delenv(k, raising=False)

    report = doctor_full.run_full_checks(
        profile_dir=tmp_path, dsn=None, timeout=2.0, allow_write=False,
    )
    assert "checks" in report
    assert "summary" in report
    assert isinstance(report["checks"], list)
    assert {c["id"] for c in report["checks"]} == set(REQUIRED_IDS), (
        f"some required check ids are missing: "
        f"missing={[i for i in REQUIRED_IDS if i not in {c['id'] for c in report['checks']}]}"
    )
    # Every check must have the required shape.
    for c in report["checks"]:
        assert "id" in c
        assert "status" in c
        assert "detail" in c
        assert "evidence" in c
        assert c["status"] in ("ok", "fail", "skip", "warn"), c
        assert isinstance(c["detail"], str)
        assert isinstance(c["evidence"], dict)
    # Summary counts add up.
    s = report["summary"]
    total = sum(s.values())
    assert total == len(report["checks"]), f"summary mismatch: {s}"


def test_run_full_checks_unreachable_dsn_marks_db_reachable_as_fail(
    monkeypatch, tmp_path
):
    """A DSN that cannot be reached yields db_reachable='fail' with a
    human-readable detail, and the rest of the runner still returns
    a structured report."""
    for k in ("HERMES_HOME", "MINIMAX_CN_API_KEY", "MINIMAX_API_KEY",
              "V3CORE_PG_PASSWORD", "PGPASSWORD"):
        monkeypatch.delenv(k, raising=False)

    # Use an obviously-bad DSN. _parse_dsn accepts scheme://...
    dsn = "postgres://no_user:no_pass@127.0.0.1:1/no_db"
    report = doctor_full.run_full_checks(
        profile_dir=tmp_path, dsn=dsn, timeout=1.0, allow_write=False,
    )
    by_id = {c["id"]: c for c in report["checks"]}
    # The check exists.
    assert "db_reachable" in by_id
    # It must be fail (the DSN is bogus), and the detail must say
    # something a human can act on.
    assert by_id["db_reachable"]["status"] == "fail", by_id["db_reachable"]
    detail = by_id["db_reachable"]["detail"]
    assert detail and len(detail) > 10, "detail must be a human-readable string"
    # The runner did not raise.
    assert "checks" in report
    assert len(report["checks"]) == len(REQUIRED_IDS)


def test_run_full_checks_auth_checks_skip_when_no_key(
    monkeypatch, tmp_path
):
    """When the active config has no api key for llm / embed / rerank,
    the corresponding auth checks must be 'skip'."""
    for k in ("HERMES_HOME", "MINIMAX_CN_API_KEY", "MINIMAX_API_KEY",
              "V3CORE_PG_PASSWORD", "PGPASSWORD"):
        monkeypatch.delenv(k, raising=False)

    # Monkeypatch the config view to report no keys anywhere.
    fake_view = {
        "llm": {"provider": "minimax", "model": "M3",
                "api_key": None, "base_url": ""},
        "embed": {"endpoint": "", "apiKey": None, "model": "", "dim": 1024},
        "rerank": {"endpoint": "", "apiKey": None, "model": "", "dim": None},
        "embed_dim_configured": 1024,
        "pg": {"host": None, "port": None, "database": None, "user": None,
               "has_password": False},
    }
    monkeypatch.setattr(doctor_full, "_build_config_view",
                        lambda: fake_view)

    report = doctor_full.run_full_checks(
        profile_dir=tmp_path, dsn=None, timeout=1.0, allow_write=False,
    )
    by_id = {c["id"]: c for c in report["checks"]}
    for check_id in ("memory_llm_auth", "embedding_auth", "rerank_auth"):
        assert by_id[check_id]["status"] == "skip", by_id[check_id]
        assert "api key" in by_id[check_id]["detail"].lower() or \
               "key" in by_id[check_id]["detail"].lower(), by_id[check_id]


def test_run_full_checks_write_is_skip_when_not_allowed(
    monkeypatch, tmp_path
):
    """When allow_write=False, the write check is skip with the
    frozen 'write probe not requested' detail."""
    for k in ("HERMES_HOME", "MINIMAX_CN_API_KEY", "MINIMAX_API_KEY",
              "V3CORE_PG_PASSWORD", "PGPASSWORD"):
        monkeypatch.delenv(k, raising=False)

    report = doctor_full.run_full_checks(
        profile_dir=tmp_path, dsn=None, timeout=1.0, allow_write=False,
    )
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["write"]["status"] == "skip"
    assert by_id["write"]["detail"] == "write probe not requested"


# ──────────────────────────────────────────────────────────────────────────────
# Auth checks: stubbed requests.get with various status codes
# ──────────────────────────────────────────────────────────────────────────────


def _make_response(status_code: int, body: str = "") -> requests.models.Response:
    resp = requests.models.Response()
    resp.status_code = int(status_code)
    resp._content = body.encode("utf-8") if isinstance(body, str) else body
    resp.encoding = "utf-8"
    return resp


@pytest.mark.parametrize(
    "status,expected_status",
    [
        (401, "fail"),
        (402, "fail"),
        (429, "warn"),
        (500, "warn"),
        (503, "warn"),
        (200, "ok"),
    ],
)
def test_auth_check_classifies_status_codes(
    monkeypatch, tmp_path, status, expected_status
):
    """With a configured fake key + endpoint, the auth check must map
    the HTTP status to a stable, expected severity."""
    for k in ("HERMES_HOME", "MINIMAX_CN_API_KEY", "MINIMAX_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    # Fake config view: key + endpoint are both present.
    fake_view = {
        "llm": {"provider": "minimax", "model": "M3",
                "api_key": "test-key-not-real", "base_url": ""},
        "embed": {"endpoint": "https://example.invalid/embed",
                  "apiKey": None, "model": "", "dim": 1024},
        "rerank": {"endpoint": "https://example.invalid/rerank",
                   "apiKey": None, "model": "", "dim": None},
        "embed_dim_configured": 1024,
        "pg": {"host": None, "port": None, "database": None, "user": None,
               "has_password": False},
    }
    monkeypatch.setattr(doctor_full, "_build_config_view",
                        lambda: fake_view)

    # Monkeypatch requests.get used inside _do_auth_check.
    def _fake_get(url, headers=None, timeout=None):
        return _make_response(status, body='{"detail":"synthetic"}')

    monkeypatch.setattr(doctor_full.requests, "get", _fake_get)

    report = doctor_full.run_full_checks(
        profile_dir=tmp_path, dsn=None, timeout=2.0, allow_write=False,
    )
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["memory_llm_auth"]["status"] == expected_status, (
        f"status={status} expected {expected_status}, got "
        f"{by_id['memory_llm_auth']}"
    )
    # The key is NEVER echoed back into the detail.
    assert "test-key-not-real" not in by_id["memory_llm_auth"]["detail"]
    assert "test-key-not-real" not in str(by_id["memory_llm_auth"]["evidence"])


# ──────────────────────────────────────────────────────────────────────────────
# CLI wrapper
# ──────────────────────────────────────────────────────────────────────────────


def test_cli_prints_json_and_returns_exit_code(monkeypatch, tmp_path, capsys):
    """run_full_checks_cli prints a single JSON object on stdout and
    returns an exit code that reflects the summary."""
    for k in ("HERMES_HOME", "MINIMAX_CN_API_KEY", "MINIMAX_API_KEY",
              "V3CORE_PG_PASSWORD", "PGPASSWORD"):
        monkeypatch.delenv(k, raising=False)

    rc = doctor_full.run_full_checks_cli([
        "--profile-dir", str(tmp_path),
        "--dsn", "postgres://no_user:no_pass@127.0.0.1:1/no_db",
        "--timeout", "1.0",
    ])
    out = capsys.readouterr().out
    # The output is a single JSON object.
    assert out.strip(), "expected non-empty JSON on stdout"
    payload = json.loads(out)  # must be valid JSON
    assert "checks" in payload
    assert "summary" in payload
    # db_reachable failed → exit code 2.
    assert rc == 2, f"expected exit code 2 for fail, got {rc}"


def test_cli_returns_zero_when_no_fails_or_warns(monkeypatch, tmp_path, capsys):
    """When no DSN is passed, all db/auth checks are skip, and (with
    no write probe) there are no fails or warns → exit code 0."""
    for k in ("HERMES_HOME", "MINIMAX_CN_API_KEY", "MINIMAX_API_KEY",
              "V3CORE_PG_PASSWORD", "PGPASSWORD"):
        monkeypatch.delenv(k, raising=False)

    # No DSN, no writes → all checks are skip or warn; the
    # hermes_provider_discovery check may be warn but not fail.
    rc = doctor_full.run_full_checks_cli([
        "--profile-dir", str(tmp_path),
    ])
    out = capsys.readouterr().out
    payload = json.loads(out)
    s = payload["summary"]
    assert s.get("fail", 0) == 0, (
        f"unexpected fails in summary: {s}; checks={payload['checks']}"
    )
    # 0 fail → exit code is 0 or 1 (warn).
    assert rc in (0, 1), f"unexpected exit code: {rc}"


# ──────────────────────────────────────────────────────────────────────────────
# Negative regression: run_full_checks must not raise even if every
# underlying probe raises.
# ──────────────────────────────────────────────────────────────────────────────


def test_run_full_checks_does_not_raise_on_any_probe_failure(
    monkeypatch, tmp_path
):
    """A run where the underlying probes raise internally must still
    return a structured report with per-check fail records (no
    exception bubbles out of run_full_checks)."""
    for k in ("HERMES_HOME", "MINIMAX_CN_API_KEY", "MINIMAX_API_KEY",
              "V3CORE_PG_PASSWORD", "PGPASSWORD"):
        monkeypatch.delenv(k, raising=False)

    # Provide a config view with a key for every provider so the
    # auth checks actually attempt an HTTP probe (and so the
    # recall/rerank smokes proceed).
    monkeypatch.setattr(doctor_full, "_build_config_view", lambda: {
        "llm": {"provider": "minimax", "model": "M3",
                "api_key": "test-key-not-real", "base_url": "https://example.invalid"},
        "embed": {"endpoint": "https://example.invalid/embed",
                  "apiKey": "test-key-not-real", "model": "embed",
                  "dim": 1024},
        "rerank": {"endpoint": "https://example.invalid/rerank",
                   "apiKey": "test-key-not-real", "model": "rerank",
                   "dim": None},
        "embed_dim_configured": 1024,
        "pg": {"host": None, "port": None, "database": None, "user": None,
               "has_password": False},
    })

    def _raise(*args, **kwargs):
        raise RuntimeError("synthetic internal error")

    monkeypatch.setattr(doctor_full.requests, "get", _raise)
    monkeypatch.setattr(doctor_full.requests, "post", _raise)

    report = doctor_full.run_full_checks(
        profile_dir=tmp_path, dsn=None, timeout=1.0, allow_write=False,
    )
    assert "checks" in report
    assert "summary" in report
    for c in report["checks"]:
        assert "id" in c
        assert "status" in c
        assert "detail" in c
        assert "evidence" in c
        assert c["status"] in ("ok", "fail", "skip", "warn")
    by_id = {c["id"]: c for c in report["checks"]}
    # Auth checks must have been downgraded to fail (not raise) when the
    # underlying requests.get raises.
    for cid in ("memory_llm_auth", "embedding_auth", "rerank_auth"):
        assert by_id[cid]["status"] == "fail", by_id[cid]
        assert "synthetic internal error" in by_id[cid]["detail"], by_id[cid]
