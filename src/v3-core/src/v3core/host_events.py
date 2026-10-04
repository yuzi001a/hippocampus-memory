# -*- coding: utf-8 -*-
"""Hermes host-event classifier + OOB payload extraction (P0-C1).

Pure, deterministic, dependency-free (stdlib ``re`` only). No LLM, no
embedding, no similarity, no I/O, no schema, no episode model. Adapter-layer
helpers consumed by :meth:`v3core.V3Core.sync_turn`.

Design contract
---------------
``docs/P0C1-HOST-EVENT-SEMANTICS.md`` §2 — the **classification order is part
of the contract** (first match wins). §2.1 lists the canonical prefixes; the
"legacy coverage" subsection of §2.1 lists the extra shapes that MUST keep the
legacy ``_injection_pattern`` skip-surface so that no formerly-skipped message
falls through to ``REAL_USER`` (which would open a new QA = production
regression).

Normalisation
-------------
Real Hermes host injections frequently carry a **leading timestamp / bracket
wrapper**, e.g.::

    Sat 2026-04-11 01:15 GMT+8] [Subagent Context] ...
    Mon 2026-10-04 12:00 GMT+8] [IMPORTANT: Background process ... completed
    Wed 2026-09-30 09:00 GMT+8] [ASYNC DELEGATION BATCH COMPLETE — deleg_…

The wrapper is stripped once by :func:`normalize_host_head` (same date branches
as the legacy ``_injection_pattern``) and all matching happens on the
normalised head. Only the wrapper is removed — the body is never touched.
"""
from __future__ import annotations

import re

# ── kind constants (contract, §2) ───────────────────────────────────────────
HOST_EVENT_REAL_USER = "REAL_USER"
HOST_EVENT_CONTINUATION = "CONTINUATION"
HOST_EVENT_ASYNC_BOUNDARY = "ASYNC_BOUNDARY"
HOST_EVENT_NEW_ROOT = "NEW_ROOT"
HOST_EVENT_CONTROL = "CONTROL"
HOST_EVENT_TERMINAL = "TERMINAL"
HOST_EVENT_NORMAL_ASSISTANT = "NORMAL_ASSISTANT"
HOST_EVENT_NORMAL_TOOL = "NORMAL_TOOL"

# ── §2.1 step 1 — tool roles ────────────────────────────────────────────────
_TOOL_ROLES = frozenset({"tool", "tool_call", "tool_result", "function"})

# ── §2.1 step 2 — display-only terminal rows ────────────────────────────────
_TERMINAL_DISPLAY_KINDS = frozenset({"failed_turn", "hidden"})
# Content-shaped terminal rows (display-only transcript boundaries, §7/§10):
# real assistant rows observed in Hermes state.db.
_TERMINAL_CONTENT_PREFIXES = (
    "Operation interrupted",
    "[This response was interrupted by a user correction.",
)

# ── §2.1 step 3 — out-of-band real user ─────────────────────────────────────
_REAL_USER_DISPLAY_KINDS = frozenset({"steer"})
_OOB_OPEN = "[OUT-OF-BAND USER MESSAGE"
_OOB_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"

# ── §2.1 step 4 — CONTINUATION (transparent structure) ──────────────────────
_CONTINUATION_PREFIXES = (
    "[CONTEXT COMPACTION",
    "[Your active task list was preserved across context compression",
    "[STILL IN PROGRESS",
    "[System note: Your previous turn was interrupted mid-run",
)

# ── §2.1 step 5 — ASYNC boundary ────────────────────────────────────────────
_ASYNC_PREFIXES = (
    "[ASYNC DELEGATION",
    "[IMPORTANT: Background process",
)
# "[IMPORTANT: N background processes…" / "…background subagent delegations…"
_ASYNC_NUMBERED_RE = re.compile(
    r"^\[IMPORTANT:\s+\d+\s+background\s+(?:processes|subagent\s+delegations)\b"
)

# ── §2.1 step 6 — NEW_ROOT ──────────────────────────────────────────────────
_NEW_ROOT_PREFIXES = (
    "[IMPORTANT: You are running as a scheduled cron job",
    "[IMPORTANT: The user has invoked the",
    "[cron:",  # legacy coverage: bracketed cron marker
)

# ── §2.1 step 8 — CONTROL (fail-closed; prefix/shape-exact, never heuristic) ─
# NB: JUDGE_PROMPT ("# 公文易 …") and ordinary prose must NOT match here — they
# stay REAL_USER (§10: the judge pairing is 1:1-correct today).
_CONTROL_PREFIXES = (
    "[System:",      # model-switch / cut-off / reasoning-only / tool-issue
    "[System note:",  # generic system note (the interrupted-mid-run form is
                      # already consumed by CONTINUATION at step 4)
    "[System]",       # legacy coverage
    "[System ",       # legacy coverage (e.g. "[System note: …")
    "You just executed tool calls",
    "You've reached the maximum number of tool-calling",
    "Your previous final response was rejected",
    # ── legacy coverage (§2.1 "legacy coverage") ──
    "[Subagent Context]",  # often timestamp-wrapped
    "[AGENT_RULES",
    "[Retry after",
    "[IMPORTANT",  # catch-all AFTER ASYNC/NEW_ROOT: any other [IMPORTANT: … is
                   # a host injection, never a human turn.
)
# Chinese full-width bracket forms 【IMPORTANT / 【ASYNC are content-dispatched.
_CJK_PREFIXES = ("【IMPORTANT", "【ASYNC")

# ── §2.1 step 8 — TITLE_GEN (metadata-only housekeeping → CONTROL) ──────────
_TITLE_GEN_HEAD = "### Task:"
_TITLE_GEN_MARKER = "Suggest 3-5 relevant follow-up questions"

# ── leading timestamp / bracket wrapper (legacy _injection_pattern branches) ─
_TS_ALT = (
    r"\d{1,2}\s+\w{3}\s+\d{4}(?:\s+\d{1,2}:\d{2})?(?:\s+GMT[+-]\d+)?"
    r"|\w{3}\s+\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2})?(?:\s+GMT[+-]\d+)?"
    r"|\w{3}\s+\d{1,2}(?:\s+\d{1,2}:\d{2})?"
    r"|\d{1,2}:\d{2}"
    r"|\d{4}-\d{2}-\d{2}"
)
# Strip the wrapper only when a real "[…" host head follows (lookahead), so an
# ordinary user sentence such as "abc 5 hello" is never mangled.
_LEADING_TS_WRAPPER = re.compile(r"^\s*\[?\s*(?:" + _TS_ALT + r")\s*\]?\s*(?=\[)")


def normalize_host_head(content: object) -> str:
    """Return ``content`` with a leading timestamp/bracket wrapper removed.

    Tolerates the real form ``Sat 2026-04-11 01:15 GMT+8] [Subagent Context]``
    (optional leading ``[``, timestamp, optional ``]``, whitespace, then the
    real ``[`` head). Idempotent; body untouched.
    """
    if content is None:
        return ""
    s = content if isinstance(content, str) else str(content)
    s = s.lstrip()
    m = _LEADING_TS_WRAPPER.match(s)
    if m:
        s = s[m.end():]
    return s


def _classify_cjk_bracket(head: str) -> str:
    """Content-dispatch a 【IMPORTANT / 【ASYNC full-width bracket injection.

    cron-job semantics → NEW_ROOT; background process / delegation semantics →
    ASYNC_BOUNDARY; anything else → CONTROL (fail-closed, never REAL_USER).
    """
    window = head[:400]
    if "scheduled cron job" in window or "has invoked the" in window:
        return HOST_EVENT_NEW_ROOT
    if (
        head.startswith("【ASYNC")
        or "background process" in window
        or "background subagent" in window
        or "delegation" in window
    ):
        return HOST_EVENT_ASYNC_BOUNDARY
    return HOST_EVENT_CONTROL


def classify_host_event(
    *,
    role: str,
    content: str,
    display_kind: str | None = None,
    session_source: str | None = None,
) -> str:
    """Classify one Hermes host row into a :data:`HOST_EVENT_*` kind.

    Order is the contract (§2.1); first match wins. Deterministic and pure.
    """
    role_l = (role or "").strip().lower()
    dk = (display_kind or "").strip().lower()
    head = normalize_host_head(content)

    # 1. tool roles → NORMAL_TOOL
    if role_l in _TOOL_ROLES:
        return HOST_EVENT_NORMAL_TOOL

    # 2. display-only terminal rows → TERMINAL (never append to pending["a"])
    if dk in _TERMINAL_DISPLAY_KINDS or head.startswith(_TERMINAL_CONTENT_PREFIXES):
        return HOST_EVENT_TERMINAL

    # 3. out-of-band real user → REAL_USER
    if dk in _REAL_USER_DISPLAY_KINDS or head.startswith(_OOB_OPEN):
        return HOST_EVENT_REAL_USER

    # 4. CONTINUATION (transparent) → CONTINUATION
    if head.startswith(_CONTINUATION_PREFIXES):
        return HOST_EVENT_CONTINUATION

    # 5. ASYNC boundary → ASYNC_BOUNDARY
    if head.startswith(_ASYNC_PREFIXES) or _ASYNC_NUMBERED_RE.match(head):
        return HOST_EVENT_ASYNC_BOUNDARY

    # 5b. legacy coverage: 【IMPORTANT / 【ASYNC content-dispatch
    if head.startswith(_CJK_PREFIXES):
        return _classify_cjk_bracket(head)

    # 6. NEW_ROOT → NEW_ROOT
    if head.startswith(_NEW_ROOT_PREFIXES):
        return HOST_EVENT_NEW_ROOT

    # 7. cron session user row without a real-user shape → NEW_ROOT (§9)
    if (session_source or "").strip().lower() == "cron" and role_l == "user":
        return HOST_EVENT_NEW_ROOT

    # 8. CONTROL → CONTROL
    if head.startswith(_CONTROL_PREFIXES):
        return HOST_EVENT_CONTROL
    if head.startswith(_TITLE_GEN_HEAD) and _TITLE_GEN_MARKER in head:
        return HOST_EVENT_CONTROL

    # 9. plain user → REAL_USER
    if role_l == "user":
        return HOST_EVENT_REAL_USER

    # 10. plain assistant → NORMAL_ASSISTANT
    if role_l == "assistant":
        return HOST_EVENT_NORMAL_ASSISTANT

    # 11. fail-closed → CONTROL (never invent a human turn)
    return HOST_EVENT_CONTROL


def extract_oob_payload(content: object) -> str:
    """Return the human payload inside an OUT-OF-BAND wrapper, wrapper stripped.

    ``[OUT-OF-BAND USER MESSAGE — …]\\n<the real message>\\n
    [/OUT-OF-BAND USER MESSAGE]`` → ``<the real message>``. Never returns the
    wrapper. If ``content`` is not an OOB wrapper it is returned unchanged
    (so callers can apply this unconditionally without corrupting other rows).
    """
    raw = "" if content is None else (content if isinstance(content, str) else str(content))
    head = normalize_host_head(raw)
    if not head.startswith(_OOB_OPEN):
        return raw
    open_end = head.find("]")
    if open_end == -1:
        return raw
    body = head[open_end + 1:]
    close_idx = body.rfind(_OOB_CLOSE)
    if close_idx != -1:
        body = body[:close_idx]
    return body.strip("\r\n")


__all__ = [
    "HOST_EVENT_REAL_USER",
    "HOST_EVENT_CONTINUATION",
    "HOST_EVENT_ASYNC_BOUNDARY",
    "HOST_EVENT_NEW_ROOT",
    "HOST_EVENT_CONTROL",
    "HOST_EVENT_TERMINAL",
    "HOST_EVENT_NORMAL_ASSISTANT",
    "HOST_EVENT_NORMAL_TOOL",
    "classify_host_event",
    "extract_oob_payload",
    "normalize_host_head",
]
