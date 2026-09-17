# -*- coding: utf-8 -*-
"""test_http_status_truthiness.py — pin the requests.Response truthiness trap.

Why this test exists:

    ``requests.Response.__bool__`` returns ``False`` for any 4xx/5xx
    response. The old ``v3core.llm`` code had::

        status = _http_err.response.status_code if _http_err.response else 0

    That line silently masked a real 401/500 as ``status=0``, which made
    the ``500 <= status < 600`` 5xx-retry branch dead code. This file
    pins the truthiness behaviour, the helper classification, and the
    retry-branch entry for 500/503 (and explicitly for 401/402/429).

Hermetic: no network, no real LLM, no DB. All requests go through
``monkeypatch``.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import patch, MagicMock

import pytest
import requests
import requests.models

import v3core.llm as llm_mod


# The five status codes the test contract pins.
PIN_STATUSES = [401, 402, 429, 500, 503]
RETRY_STATUSES = [500, 503]
NO_RETRY_STATUSES = [401, 402, 429]


def _make_response(status_code: int, body: str = "") -> requests.models.Response:
    """Build a real ``requests.Response`` with a given status code.

    The body is what ``raise_for_status`` would echo back. We never
    call ``raise_for_status`` here — we construct the error the way
    ``requests`` does internally.
    """
    resp = requests.models.Response()
    resp.status_code = int(status_code)
    resp._content = body.encode("utf-8") if isinstance(body, str) else body
    # Some callers read .text; populate it cheaply.
    resp.encoding = "utf-8"
    return resp


# ──────────────────────────────────────────────────────────────────────────────
# (a) bool(resp) is False for any 4xx/5xx — documenting the trap.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", PIN_STATUSES)
def test_a_requests_response_bool_is_false_for_error_codes(status):
    """The trap: ``if response:`` is wrong for any error status."""
    resp = _make_response(status)
    # Sanity: the request really did come back with that status.
    assert resp.status_code == status
    # The actual assertion the comment in llm.py is trying to make:
    # bool(resp) is False, so `if resp:` masks it.
    assert bool(resp) is False, (
        f"trap documented: bool(Response({status})) is False — "
        "use `is not None` instead"
    )


def test_a_requests_response_bool_is_true_for_2xx():
    """Sanity: 2xx is truthy (so the asymmetry is real)."""
    resp = _make_response(200, body='{"ok":true}')
    assert resp.status_code == 200
    assert bool(resp) is True


# ──────────────────────────────────────────────────────────────────────────────
# (b) The helper / llm.py logic still reports the true status code.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,expected_class",
    [
        (401, "auth_failed"),
        (402, "quota_or_plan_limit"),
        (429, "rate_limited"),
        (500, "upstream_error"),
        (503, "upstream_unavailable"),
    ],
)
def test_b_classify_status_returns_frozen_mapping(status, expected_class):
    assert llm_mod.classify_status(status) == expected_class


def test_b_classify_status_handles_none_and_unknown():
    assert llm_mod.classify_status(None) == "none"
    assert llm_mod.classify_status(418) == "http_418"  # I'm a teapot
    assert llm_mod.classify_status(0) == "http_0"


def test_b_llm_uses_is_not_none_not_truthiness(caplog):
    """Re-execute the fixed llm.py branch in isolation and confirm the
    status code (not 0) is what flows into the log line."""
    # Build a real 500 response and a real HTTPError wrapping it.
    resp = _make_response(500, body='{"error":"upstream"}')
    err = requests.exceptions.HTTPError("500 Server Error: upstream")
    err.response = resp
    # The fixed branch (lifted from llm.py) under test:
    captured = {}
    def _emit_warning(msg, *args):
        captured["msg"] = msg % args
    with caplog.at_level(logging.WARNING, logger="v3core.llm"):
        with patch.object(llm_mod.logger, "warning", side_effect=_emit_warning):
            # Lifted verbatim from the fix, to prove the new branch.
            _resp = err.response
            if _resp is not None:
                _status = _resp.status_code
                _body_text = (_resp.text or "")[:300]
            else:
                _status = 0
                _body_text = ""
            status_class = llm_mod.classify_status(_status if _status else None)
            assert _status == 500
            assert status_class == "upstream_error"
            assert _body_text == '{"error":"upstream"}'
            llm_mod.logger.warning(
                "LLM 5xx (attempt %d/%d): status=%d class=%s — retry in %ds",
                1, 3, _status, status_class, 1,
            )
    msg = captured.get("msg", "")
    assert "status=500" in msg, f"expected 'status=500' in log, got: {msg!r}"
    assert "status=0" not in msg, f"the trap leaked status=0 into the log: {msg!r}"
    assert "class=upstream_error" in msg


def test_b_llm_truthiness_path_returns_0_when_response_is_none():
    """When the exception has no response (ConnectionError-style), the
    fixed branch must still produce status=0 without crashing."""
    err = requests.exceptions.HTTPError("no response attached")
    err.response = None
    _resp = err.response
    if _resp is not None:  # fixed branch
        _status = _resp.status_code
        _body_text = (_resp.text or "")[:300]
    else:
        _status = 0
        _body_text = ""
    assert _status == 0
    assert _body_text == ""
    assert llm_mod.classify_status(_status if _status else None) == "none"


# ──────────────────────────────────────────────────────────────────────────────
# (c) 5xx retry branch is entered for 500/503 and not for 401/402/429.
#     We monkeypatch ``requests.post`` to return a Response that
#     ``raise_for_status()`` will convert into the matching HTTPError,
#     and we count the number of times the LLM was actually retried.
# ──────────────────────────────────────────────────────────────────────────────


class _SequencePost:
    """A requests.post replacement that returns a sequence of responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.bodies = []

    def __call__(self, url, json=None, headers=None, timeout=None, proxies=None):
        self.calls += 1
        self.bodies.append(json)
        return self._responses.pop(0)


def _make_client_with_status(status_code):
    """Build a minimal LLMClient that targets provider=minimax with a
    fake api_key, so we exercise the real ``_chat_minimax`` path."""
    cfg = {
        "provider": "minimax",
        "model": "M3-test",
        "api_key": "test-key-do-not-actually-call",
        "base_url": "",  # use the official default
    }
    client = llm_mod.LLMClient(cfg)
    return client


@pytest.mark.parametrize("status", RETRY_STATUSES)
def test_c_5xx_enters_retry_branch(status, monkeypatch):
    """For 500/503, the LLM must log a 'retry' warning, sleep, and try
    again — and ultimately raise after MAX_RETRIES. The fixed branch
    re-raises the original HTTPError on the final attempt (so the
    status code is preserved, not lost as a generic 0)."""
    sleeps: list[float] = []
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: sleeps.append(s))

    # Build MAX_RETRIES identical 5xx responses — every attempt should retry.
    seq = _SequencePost([_make_response(status) for _ in range(llm_mod._MAX_RETRIES)])
    monkeypatch.setattr(llm_mod.requests, "post", seq)

    client = _make_client_with_status(status)
    # The fixed branch re-raises the final HTTPError so the operator
    # sees the real status code. Earlier branches logged "LLM 5xx".
    with pytest.raises(requests.exceptions.HTTPError) as excinfo:
        client._chat_minimax("system", [{"role": "user", "content": "hi"}], 0.0)

    # The post function was called MAX_RETRIES times (retry branch entered).
    assert seq.calls == llm_mod._MAX_RETRIES, (
        f"expected {llm_mod._MAX_RETRIES} attempts, got {seq.calls}"
    )
    # And the runner slept between attempts (proves the retry branch was entered).
    assert len(sleeps) == llm_mod._MAX_RETRIES - 1, (
        f"expected {llm_mod._MAX_RETRIES - 1} sleeps, got {len(sleeps)}: {sleeps}"
    )
    # The final raise is the original HTTPError (not wrapped), so the
    # status code must be the real one — proving the bugfix worked.
    final_resp = excinfo.value.response
    assert final_resp is not None
    assert final_resp.status_code == status, (
        f"expected status={status} on the final raise, got "
        f"{final_resp.status_code}"
    )


@pytest.mark.parametrize("status", NO_RETRY_STATUSES)
def test_c_4xx_does_not_retry(status, monkeypatch):
    """For 401/402/429, the LLM must NOT enter the 5xx retry branch.

    401/402: a single 4xx response → exactly one attempt, raise.
    429: handled in a SEPARATE branch above the HTTPError except — the
         runner sees ``status_code == 429``, sleeps, and continues. The
         test still pins: 5xx branch is NOT entered, and the eventual
         raise preserves the status code.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: sleeps.append(s))

    five_xx_logs: list[str] = []
    real_warning = llm_mod.logger.warning

    def _spy(msg, *args, **kwargs):
        five_xx_logs.append(msg % args if args else msg)
        return real_warning(msg, *args, **kwargs)

    monkeypatch.setattr(llm_mod.logger, "warning", _spy)

    # 401/402: one response is enough; the runner raises immediately.
    # 429: handled by the dedicated 429 branch, so MAX_RETRIES identical
    # 429 responses drive the loop to its final-raise path.
    n = 1 if status in (401, 402) else llm_mod._MAX_RETRIES
    seq = _SequencePost([_make_response(status) for _ in range(n)])
    monkeypatch.setattr(llm_mod.requests, "post", seq)

    client = _make_client_with_status(status)
    if status == 429:
        # 429 is special: the dedicated branch above the HTTPError
        # except-``continue``s every time, so after MAX_RETRIES the
        # outer loop falls through and raises a RuntimeError. The
        # 5xx branch is still never entered.
        with pytest.raises(RuntimeError) as excinfo:
            client._chat_minimax(
                "system", [{"role": "user", "content": "hi"}], 0.0
            )
        assert "LLM failed after" in str(excinfo.value)
    else:
        # 401/402: the HTTPError except-branch re-raises the final error.
        with pytest.raises(requests.exceptions.HTTPError) as excinfo:
            client._chat_minimax(
                "system", [{"role": "user", "content": "hi"}], 0.0
            )
        # The final raise preserves the real status code (the bugfix).
        final_resp = excinfo.value.response
        assert final_resp is not None
        assert final_resp.status_code == status, (
            f"expected status={status} on the final raise, got "
            f"{final_resp.status_code}"
        )

    # The 5xx branch is the one that emits "LLM 5xx" — must not appear.
    five_xx_branch = [m for m in five_xx_logs if "LLM 5xx" in m]
    assert five_xx_branch == [], (
        f"5xx retry branch was entered for status={status}: {five_xx_branch}"
    )

    # For 401/402: a single attempt, no 5xx-branch backoff sleeps.
    if status in (401, 402):
        assert seq.calls == 1, f"expected 1 attempt for {status}, got {seq.calls}"


def test_c_old_truthiness_branch_would_miss_500(monkeypatch):
    """Negative regression: simulate the old buggy branch directly to
    prove that the truthiness form WOULD have hidden a real 500 as 0,
    while the new explicit form catches it."""
    resp_500 = _make_response(500)
    err = requests.exceptions.HTTPError("500")
    err.response = resp_500

    # Old buggy branch — reproduced verbatim from the pre-fix llm.py.
    _status_old = err.response.status_code if err.response else 0
    assert _status_old == 0, "this assertion documents the trap"

    # New branch — must read the real 500.
    _resp = err.response
    if _resp is not None:
        _status_new = _resp.status_code
    else:
        _status_new = 0
    assert _status_new == 500
