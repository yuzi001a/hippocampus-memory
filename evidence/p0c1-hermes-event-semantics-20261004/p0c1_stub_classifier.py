# -*- coding: utf-8 -*-
"""DIAGNOSTIC ONLY (not part of the deliverable test suite).

The P0-C1 implementation is mid-flight: `v3core/__init__.py` already does
`from .host_events import ...` but `v3core/host_events.py` does not exist yet,
so every T1-T11 test aborts with ModuleNotFoundError *before* reaching its
assertion.  That masks the behavioural RED signal.

This pytest plugin pre-registers a minimal, contract-§2.1-faithful
`v3core.host_events` module in ``sys.modules`` so the REAL `sync_turn` can run
and the T1-T11 assertions are actually exercised.  It writes NOTHING into
`src/` — it only supplies the classifier contract so the adapter logic under
test is the real one.

Run:
  PYTHONPATH=<evidence dir> python -m pytest tests/test_p0c1_host_event_semantics.py \
      -p p0c1_stub_classifier -k "test_t" -q
"""
from __future__ import annotations

import re
import sys
import types

HOST_EVENT_REAL_USER = "REAL_USER"
HOST_EVENT_CONTINUATION = "CONTINUATION"
HOST_EVENT_ASYNC_BOUNDARY = "ASYNC_BOUNDARY"
HOST_EVENT_NEW_ROOT = "NEW_ROOT"
HOST_EVENT_CONTROL = "CONTROL"
HOST_EVENT_TERMINAL = "TERMINAL"
HOST_EVENT_NORMAL_ASSISTANT = "NORMAL_ASSISTANT"
HOST_EVENT_NORMAL_TOOL = "NORMAL_TOOL"

_TOOL_ROLES = {"tool", "tool_call", "tool_result", "function"}
_TERMINAL_KINDS = {"failed_turn", "hidden"}

_CONTINUATION = (
    "[CONTEXT COMPACTION",
    "[Your active task list was preserved across context compression",
    "[STILL IN PROGRESS",
    "[System note: Your previous turn was interrupted mid-run",
)
_ASYNC = (
    "[ASYNC DELEGATION",
    "[IMPORTANT: Background process",
    "[IMPORTANT: N background processes",
    "[IMPORTANT: N background subagent delegations",
)
_NEW_ROOT = (
    "[IMPORTANT: You are running as a scheduled cron job",
    "[IMPORTANT: The user has invoked the",
)
_CONTROL = (
    "[System:",
    "[System note:",
    "You just executed tool calls",
    "You've reached the maximum number of tool-calling",
    "Your previous final response was rejected",
)

# leading timestamp tolerance (mirrors _injection_pattern): strip ONLY a real
# leading timestamp wrapper, never a bare opening bracket before the prefix.
_TS_LEAD = re.compile(
    r"^\s*\[?(?:\d{1,2}\s+\w{3}\s+\d{4}(?:\s+\d{1,2}:\d{2})?(?:\s+GMT[+-]\d+)?"
    r"|\w{3}\s+\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2})?(?:\s+GMT[+-]\d+)?"
    r"|\w{3}\s+\d{1,2}(?:\s+\d{1,2}:\d{2})?|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2})"
    r"\s*\]?\s*"
)


def _head(content: str) -> str:
    s = content or ""
    try:
        m = _TS_LEAD.match(s)
        if m:
            return s[m.end():]
    except Exception:
        pass
    return s


def classify_host_event(*, role, content, display_kind=None, session_source=None):
    role = (role or "").strip().lower()
    if role in _TOOL_ROLES:
        return HOST_EVENT_NORMAL_TOOL
    if display_kind in _TERMINAL_KINDS:
        return HOST_EVENT_TERMINAL
    if display_kind == "steer" or (content or "").lstrip().startswith("[OUT-OF-BAND USER MESSAGE"):
        return HOST_EVENT_REAL_USER
    h = _head(content)
    if h.startswith(_CONTINUATION):
        return HOST_EVENT_CONTINUATION
    if h.startswith(_ASYNC):
        return HOST_EVENT_ASYNC_BOUNDARY
    if h.startswith(_NEW_ROOT):
        return HOST_EVENT_NEW_ROOT
    if session_source == "cron" and role == "user" and not h.startswith(("[", "#", "@file:")):
        return HOST_EVENT_NEW_ROOT
    if h.startswith(_CONTROL) or h.startswith("### Task:"):
        return HOST_EVENT_CONTROL
    if role == "user":
        return HOST_EVENT_REAL_USER
    if role == "assistant":
        return HOST_EVENT_NORMAL_ASSISTANT
    return HOST_EVENT_CONTROL


_OOB_RE = re.compile(
    r"\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*(.*?)\s*\[/OUT-OF-BAND USER MESSAGE\]",
    re.S,
)


def extract_oob_payload(content: str) -> str:
    m = _OOB_RE.search(content or "")
    return (m.group(1) if m else (content or "")).strip()


_mod = types.ModuleType("v3core.host_events")
for _n in (
    "HOST_EVENT_REAL_USER", "HOST_EVENT_CONTINUATION", "HOST_EVENT_ASYNC_BOUNDARY",
    "HOST_EVENT_NEW_ROOT", "HOST_EVENT_CONTROL", "HOST_EVENT_TERMINAL",
    "HOST_EVENT_NORMAL_ASSISTANT", "HOST_EVENT_NORMAL_TOOL",
):
    setattr(_mod, _n, globals()[_n])
_mod.classify_host_event = classify_host_event
_mod.extract_oob_payload = extract_oob_payload
sys.modules.setdefault("v3core.host_events", _mod)
