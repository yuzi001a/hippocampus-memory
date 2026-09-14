"""G5b evaluator scenarios — EXPANDED corpus (v2).

This module is the Track-E corpus expansion. It composes the v1
hand-authored 40 scenarios with 46 NEW paired / expansion cases,
yielding an 86-scenario synthetic corpus. The 46 added cases were
designed to fill coverage gaps and exercise the failure taxonomy
with paired contrastive shapes:

  * same query / different historical fact     (DISTRACTOR pairs)
  * same history / different query             (intent disambiguation)
  * relevant vs tempting distractor             (NEGATIVE_RECALL pairs)
  * active vs archived                          (ARCHIVE pairs)
  * old vs superseded                           (TEMPORAL_UPDATE pairs)
  * explicit vs passive-derived fixture         (scenarios w/ and w/o
                                                 explicit memory_id)
  * short vs longer history                     (LONG_DISTANCE expansion)
  * bare keyword vs multi-token query           (STABLE_FACT expansion)

The expansion does NOT change:
  * the evaluator algorithm / runtime semantics
  * the scenario schema
  * the failure taxonomy
  * the metrics module
  * Lane A / Lane B implementations

The v1 module (``scenarios_v1.py``) remains untouched and is
still importable; it continues to return exactly its 40 scenarios.
``scenarios_v2.all_scenarios()`` returns the union (40 + 46 = 86),
which sits in the 80-100 target band defined by Track E.

Category coverage of the 46 added scenarios:

  USER_PREFERENCE    (+4) — 8 total (terminal, font, notification, sleep)
  PROJECT_DECISION   (+4) — 8 total (lint, package, schema, embed)
  SESSION_CONTINUITY (+3) — 6 total (TODO carry, env-var carry, name)
  STABLE_FACT        (+4) — 8 total (locale, currency, language, region)
  CONFLICT_UPDATE    (+3) — 6 total (categorical, status, embed)
  DISTRACTOR         (+5) — 8 total (port, env, branch, region, alias)
  NEGATIVE_RECALL    (+4) — 7 total (lab/prod, draft/published, etc.)
  ARCHIVE            (+3) — 6 total (soft, hard-idempotent, dual)
  LONG_DISTANCE      (+4) — 7 total (long content, 30 fixtures, etc.)
  MULTI_RELEVANT     (+4) — 7 total (rank ties, subset answers)
  AMBIGUOUS          (+4) — 7 total (homonyms, tag clouds)
  TEMPORAL_UPDATE    (+4) — 8 total (deploy, schema, name, version)

Total added: 46 scenarios. Total corpus: 86 scenarios.

All fixtures are synthetic; no real names, IPs, or private data.
The ``derive_memory_id`` SHA-256 contract continues to apply.
"""
from __future__ import annotations

from .scenarios_v1 import SCENARIOS as _V1_SCENARIOS, CATEGORIES


# ── 50 NEW synthetic scenarios (paired / expansion) ───────────────────────

_EXPANDED_SCENARIOS = [
    # ─── USER_PREFERENCE (+4) ────────────────────────────────────────────
    {
        "scenario_id": "G5b-UP-101",
        "category": "USER_PREFERENCE",
        "description": "User prefers tmux over screen for terminal multiplexing.",
        "fixtures": [
            {
                "category": "preference",
                "title": "terminal multiplexer",
                "content": "User prefers tmux for terminal multiplexing; screen is not used.",
                "tags": ["terminal", "tmux"],
            },
            {
                "category": "preference",
                "title": "shell prompt",
                "content": "User prefers fish shell with starship prompt.",
                "tags": ["shell", "terminal"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "tmux terminal",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Pair of UP-001 family: terminal-domain preference vs unrelated shell.",
    },
    {
        "scenario_id": "G5b-UP-102",
        "category": "USER_PREFERENCE",
        "description": "User prefers JetBrains Mono font in the IDE.",
        "fixtures": [
            {
                "category": "preference",
                "title": "editor font choice",
                "content": "User prefers JetBrains Mono at 14pt in the IDE.",
                "tags": ["font", "ide"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "JetBrains Mono",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-UP-103",
        "category": "USER_PREFERENCE",
        "description": "User disables desktop notifications during deep work.",
        "fixtures": [
            {
                "category": "preference",
                "title": "notification policy",
                "content": "User disables desktop notifications during deep work blocks.",
                "tags": ["notification", "focus"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "disable notifications",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-UP-104",
        "category": "USER_PREFERENCE",
        "description": "User takes short Pomodoro-style breaks.",
        "fixtures": [
            {
                "category": "preference",
                "title": "break cadence",
                "content": "User takes a 5-minute break every 25 minutes (Pomodoro-style).",
                "tags": ["productivity", "break"],
            },
            {
                "category": "preference",
                "title": "preferred beverage",
                "content": "User prefers black coffee, no sugar.",
                "tags": ["beverage"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "Pomodoro break",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Pair with UP-003 (lo-fi music): both productivity-related preferences.",
    },

    # ─── PROJECT_DECISION (+4) ───────────────────────────────────────────
    {
        "scenario_id": "G5b-PD-101",
        "category": "PROJECT_DECISION",
        "description": "Project uses ruff for Python linting, not flake8.",
        "fixtures": [
            {
                "category": "decision",
                "title": "python linter",
                "content": "Project uses ruff for Python linting across all services.",
                "tags": ["lint", "python"],
            },
            {
                "category": "decision",
                "title": "dropped flake8",
                "content": "flake8 is deprecated; ruff is the only accepted linter for new code.",
                "tags": ["lint", "deprecated"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "ruff lint",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-PD-102",
        "category": "PROJECT_DECISION",
        "description": "Project uses uv for Python packaging; pip-tools retired.",
        "fixtures": [
            {
                "category": "decision",
                "title": "python packaging tool",
                "content": "Project standardizes on uv for dependency resolution and packaging.",
                "tags": ["packaging", "python", "decision"],
            },
            {
                "category": "decision",
                "title": "retired pip-tools",
                "content": "pip-tools is no longer used; uv replaces it for requirements management.",
                "tags": ["packaging", "deprecated"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "uv packaging",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-PD-103",
        "category": "PROJECT_DECISION",
        "description": "Project mandates JSON Schema for API contracts.",
        "fixtures": [
            {
                "category": "decision",
                "title": "api contract format",
                "content": "All public HTTP APIs publish JSON Schema (draft 2020-12) for request/response.",
                "tags": ["api", "schema", "decision"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "JSON Schema api",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-PD-104",
        "category": "PROJECT_DECISION",
        "description": "Project uses a canonical embedding model (identifier redacted in this public fixture; decision, not preference).",
        "fixtures": [
            {
                "category": "decision",
                "title": "canonical embedding model",
                "content": "Project uses a canonical embedding model across all retrieval paths (identifier redacted in this public fixture).",
                "tags": ["embedding", "model", "decision"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "canonical embedding model",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── SESSION_CONTINUITY (+3) ───────────────────────────────────────
    {
        "scenario_id": "G5b-SC-101",
        "category": "SESSION_CONTINUITY",
        "description": "Carry-over TODO item from a previous session.",
        "fixtures": [
            {
                "category": "session-note",
                "title": "carryover TODO from session 42",
                "content": "Carryover TODO from session 42: audit the rerank score clipping in metrics.py.",
                "tags": ["todo", "carryover"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "rerank score carryover",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "sessions": [
            {
                "session_id": "S-prior",
                "notes": "Prior session ended mid-audit; this note is the carryover anchor.",
                "conversation_turns": [
                    {"role": "user", "content": "I need to audit rerank score clipping later."},
                    {"role": "assistant", "content": "Noted — I'll persist it as a carryover TODO."},
                ],
            }
        ],
    },
    {
        "scenario_id": "G5b-SC-102",
        "category": "SESSION_CONTINUITY",
        "description": "Carry-over env var setting from a previous session.",
        "fixtures": [
            {
                "category": "session-note",
                "title": "carryover env var note",
                "content": "Carryover env note: G5B_EVAL_LIVE_PG must be unset unless explicitly opted in.",
                "tags": ["env", "carryover", "lab"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "G5B_EVAL_LIVE_PG",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "sessions": [
            {
                "session_id": "S-env",
                "turns": [
                    {"role": "user", "content": "Why did the lab PG refuse to connect last time?"},
                    {"role": "assistant", "content": "Because G5B_EVAL_LIVE_PG was unset — that's the guard."},
                ],
            }
        ],
        "notes": "Uses 'turns' alias to verify compatibility path still parses.",
    },
    {
        "scenario_id": "G5b-SC-103",
        "category": "SESSION_CONTINUITY",
        "description": "Carry-over naming convention from a prior session.",
        "fixtures": [
            {
                "category": "session-note",
                "title": "naming convention carryover",
                "content": "Carryover naming rule from session 21: scenario IDs use 'G5b-<CAT>-<NNN>'.",
                "tags": ["naming", "carryover"],
            },
            {
                "category": "session-note",
                "title": "unrelated carryover",
                "content": "Carryover: last week's on-call rotation is in the team wiki.",
                "tags": ["carryover", "wiki"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "scenario naming convention",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── STABLE_FACT (+4) ───────────────────────────────────────────────
    {
        "scenario_id": "G5b-SF-101",
        "category": "STABLE_FACT",
        "description": "Stable fact: project locale is en-US.",
        "fixtures": [
            {
                "category": "fact",
                "title": "project locale",
                "content": "Project default locale is en-US for all user-facing strings.",
                "tags": ["locale", "i18n"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "default locale",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-SF-102",
        "category": "STABLE_FACT",
        "description": "Stable fact: project currency is USD for billing.",
        "fixtures": [
            {
                "category": "fact",
                "title": "billing currency",
                "content": "Project billing currency is USD; no FX conversion is performed.",
                "tags": ["billing", "currency"],
            },
            {
                "category": "fact",
                "title": "billing locale",
                "content": "Billing date format is ISO 8601 (YYYY-MM-DD) across all environments.",
                "tags": ["billing", "format"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "billing USD",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-SF-103",
        "category": "STABLE_FACT",
        "description": "Stable fact: project documentation language is English.",
        "fixtures": [
            {
                "category": "fact",
                "title": "documentation language",
                "content": "All canonical documentation is written in English (en).",
                "tags": ["docs", "language"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "documentation language English",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Longer multi-token query to exercise keyword-lane token coverage.",
    },
    {
        "scenario_id": "G5b-SF-104",
        "category": "STABLE_FACT",
        "description": "Stable fact: project primary region is ap-shanghai.",
        "fixtures": [
            {
                "category": "fact",
                "title": "primary cloud region",
                "content": "Project primary cloud region is ap-shanghai; DR region is us-west-2.",
                "tags": ["region", "cloud"],
            },
            {
                "category": "fact",
                "title": "primary database",
                "content": "Primary database is PostgreSQL 15.",
                "tags": ["database", "version"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "ap-shanghai region",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── CONFLICT_UPDATE (+3) ───────────────────────────────────────────
    {
        "scenario_id": "G5b-CU-101",
        "category": "CONFLICT_UPDATE",
        "description": "Same memory_id, category changed → DURABLE_FAILED.",
        "fixtures": [
            {
                "category": "fact",
                "title": "region fact",
                "content": "Primary region is ap-shanghai.",
                "tags": ["region"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "primary region",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Runner manufactures same-id different-category attempt; asserts DURABLE_FAILED.",
    },
    {
        "scenario_id": "G5b-CU-102",
        "category": "CONFLICT_UPDATE",
        "description": "Same memory_id, status field change attempt → DURABLE_FAILED.",
        "fixtures": [
            {
                "category": "fact",
                "title": "audit log entry",
                "content": "Audit log entry: deploy at 03:14 UTC succeeded.",
                "tags": ["audit"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "audit log deploy",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Authoring variant: status is not a fixture field; conflict surfaces on payload drift.",
    },
    {
        "scenario_id": "G5b-CU-103",
        "category": "CONFLICT_UPDATE",
        "description": "Same memory_id, embedding-model drift attempt → DURABLE_FAILED.",
        "fixtures": [
            {
                "category": "fact",
                "title": "embedding model fact",
                "content": "Canonical embedding model is a redacted identifier (see canonical embedding model fixture).",
                "tags": ["embedding"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "canonical embedding model",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── DISTRACTOR (+5) ───────────────────────────────────────────────
    {
        "scenario_id": "G5b-DT-101",
        "category": "DISTRACTOR",
        "description": "Two ports share a name fragment; only one is the answer.",
        "fixtures": [
            {
                "category": "fact",
                "title": "lab pg port",
                "content": "Lab PostgreSQL listens on port 55462.",
                "tags": ["postgres", "lab"],
            },
            {
                "category": "fact",
                "title": "lab redis port",
                "content": "Lab Redis listens on port 55463.",
                "tags": ["redis", "lab"],
            },
            {
                "category": "fact",
                "title": "lab mongo port",
                "content": "Lab MongoDB listens on port 55464.",
                "tags": ["mongo", "lab"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "lab pg port",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Multi-distractor expansion of DT-001 family; tests tag-overlap disambiguation.",
    },
    {
        "scenario_id": "G5b-DT-102",
        "category": "DISTRACTOR",
        "description": "Same env var name, different environment target — only one is canonical.",
        "fixtures": [
            {
                "category": "fact",
                "title": "DATABASE_URL prod",
                "content": "DATABASE_URL in production points to the ap-shanghai primary.",
                "tags": ["env", "prod"],
            },
            {
                "category": "fact",
                "title": "DATABASE_URL staging",
                "content": "DATABASE_URL in staging points to the lab cluster.",
                "tags": ["env", "staging"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "production DATABASE_URL",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-DT-103",
        "category": "DISTRACTOR",
        "description": "Two branches share a naming pattern; the answer is uniquely tagged.",
        "fixtures": [
            {
                "category": "fact",
                "title": "branch release-2024",
                "content": "Branch release-2024-Q3 was cut on 2024-07-01.",
                "tags": ["branch", "history"],
            },
            {
                "category": "fact",
                "title": "branch release-2025",
                "content": "Branch release-2025-Q1 was cut on 2025-01-15.",
                "tags": ["branch", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "release branch current",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-DT-104",
        "category": "DISTRACTOR",
        "description": "Region tags overlap; only ap-shanghai is the answer for 'primary'.",
        "fixtures": [
            {
                "category": "fact",
                "title": "dr region",
                "content": "Disaster recovery region is us-west-2.",
                "tags": ["region", "dr"],
            },
            {
                "category": "fact",
                "title": "primary region",
                "content": "Primary region is ap-shanghai.",
                "tags": ["region", "primary"],
            },
            {
                "category": "fact",
                "title": "edge region",
                "content": "Edge cache region is ap-hongkong.",
                "tags": ["region", "edge"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "primary region",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-DT-105",
        "category": "DISTRACTOR",
        "description": "Alias pair: 'vNext' and 'next' point to different fixtures; only one matches.",
        "fixtures": [
            {
                "category": "decision",
                "title": "vNext release",
                "content": "vNext release is the codename for the 2026-Q3 service rollout.",
                "tags": ["release", "alias"],
            },
            {
                "category": "decision",
                "title": "next minor version",
                "content": "Next minor version is 2.4.0, planned for 2026-Q4.",
                "tags": ["release", "version"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "vNext release codename",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Tempting-distractor pair; 'next minor' has overlapping keyword 'next'.",
    },

    # ─── NEGATIVE_RECALL (+4) ───────────────────────────────────────────
    {
        "scenario_id": "G5b-NR-101",
        "category": "NEGATIVE_RECALL",
        "description": "Don't return the lab PG host when asked about production host.",
        "fixtures": [
            {
                "category": "fact",
                "title": "lab pg host",
                "content": "Lab PostgreSQL host is 10.10.55.10.",
                "tags": ["postgres", "lab"],
            },
            {
                "category": "fact",
                "title": "prod pg host",
                "content": "Production PostgreSQL host is 10.20.30.40.",
                "tags": ["postgres", "production"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "production postgres host",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Runner asserts lab memory is NOT returned when prod memory is the answer.",
    },
    {
        "scenario_id": "G5b-NR-102",
        "category": "NEGATIVE_RECALL",
        "description": "Don't return a draft policy when the published policy is the answer.",
        "fixtures": [
            {
                "category": "decision",
                "title": "draft retention policy",
                "content": "Draft retention policy: 7 days for staging logs.",
                "tags": ["policy", "draft"],
            },
            {
                "category": "decision",
                "title": "published retention policy",
                "content": "Published retention policy: 30 days for staging logs.",
                "tags": ["policy", "published"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "published retention policy",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-NR-103",
        "category": "NEGATIVE_RECALL",
        "description": "Don't return the deprecated framework when current framework is asked.",
        "fixtures": [
            {
                "category": "decision",
                "title": "deprecated framework note",
                "content": "Deprecated: Flask was used in the legacy monolith.",
                "tags": ["framework", "deprecated"],
            },
            {
                "category": "decision",
                "title": "current framework",
                "content": "Current service framework is FastAPI.",
                "tags": ["framework", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "current framework FastAPI",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-NR-104",
        "category": "NEGATIVE_RECALL",
        "description": "Don't return a competitor product fact when asked about the chosen one.",
        "fixtures": [
            {
                "category": "fact",
                "title": "competitor product fact",
                "content": "Competitor product X handles 10k req/s under load.",
                "tags": ["competitor"],
            },
            {
                "category": "fact",
                "title": "chosen product fact",
                "content": "Chosen product Y handles 8k req/s under the same load profile.",
                "tags": ["chosen"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "chosen product Y",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── ARCHIVE (+3) ──────────────────────────────────────────────────
    {
        "scenario_id": "G5b-AR-101",
        "category": "ARCHIVE",
        "description": "Archive a soft-superseded memory; the newer active memory remains.",
        "fixtures": [
            {
                "category": "fact",
                "title": "soft-superseded fact",
                "content": "Soft-superseded fact: team size was 6 engineers in 2023.",
                "tags": ["team", "history"],
            },
            {
                "category": "fact",
                "title": "current team size",
                "content": "Current team size is 12 engineers.",
                "tags": ["team", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "current team size",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "actions": [
            {"type": "archive", "fixture_index": 0},
        ],
        "notes": "Archive one fixture; reader must surface the current one and not the archived one.",
    },
    {
        "scenario_id": "G5b-AR-102",
        "category": "ARCHIVE",
        "description": "Hard archive idempotent — repeated hard-archive calls all rejected.",
        "fixtures": [
            {
                "category": "fact",
                "title": "immutable incident",
                "content": "Immutable incident log: page at 02:11 UTC, resolved at 02:34 UTC.",
                "tags": ["incident", "immutable"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "immutable incident log",
                "lane": "keyword",
                "must_recall": [],
                "expect_empty_recall": False,
            },
        ],
        "actions": [
            {"type": "archive_hard", "fixture_index": 0},
            {"type": "archive_hard", "fixture_index": 0},  # idempotent retry
            {"type": "archive_hard", "fixture_index": 0},  # third attempt
        ],
        "notes": "Pair with AR-002: 3x hard archive attempts all rejected.",
    },
    {
        "scenario_id": "G5b-AR-103",
        "category": "ARCHIVE",
        "description": "Dual archive: archive one, archive another, query → only non-archived returns.",
        "fixtures": [
            {
                "category": "decision",
                "title": "old release policy",
                "content": "Old release policy: monthly cadence.",
                "tags": ["release", "history"],
            },
            {
                "category": "decision",
                "title": "interim release policy",
                "content": "Interim release policy: biweekly cadence.",
                "tags": ["release", "interim"],
            },
            {
                "category": "decision",
                "title": "current release policy",
                "content": "Current release policy: weekly cadence with feature flags.",
                "tags": ["release", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "release policy",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "actions": [
            {"type": "archive", "fixture_index": 0},
            {"type": "archive", "fixture_index": 1},
        ],
        "notes": "Two of three fixtures archived; only the current one should remain visible.",
    },

    # ─── LONG_DISTANCE (+4) ────────────────────────────────────────────
    {
        "scenario_id": "G5b-LD-101",
        "category": "LONG_DISTANCE",
        "description": "Long content (~4KB) — reader must still surface the answer keyword.",
        "fixtures": [
            {
                "category": "fact",
                "title": "long postmortem",
                "content": (
                    "POSTMORTEM — " + ("y" * 4000) +
                    " — resolution: rolled back the schema migration."
                ),
                "tags": ["postmortem", "long"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "schema migration rollback",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Pair with LD-001 (3KB content); one notch longer.",
    },
    {
        "scenario_id": "G5b-LD-102",
        "category": "LONG_DISTANCE",
        "description": "30 background fixtures; the needle uses a unique token.",
        "fixtures": [
            {"category": "fact", "title": f"background {i}",
             "content": f"background fact {i} for the project's history.",
             "tags": ["bg"]}
            for i in range(29)
        ] + [
            {
                "category": "fact",
                "title": "needle token",
                "content": "needle token: the canonical review SLA is 24h.",
                "tags": ["needle", "sla"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "needle token SLA",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Pair with LD-002 (20 fixtures); doubles the corpus.",
    },
    {
        "scenario_id": "G5b-LD-103",
        "category": "LONG_DISTANCE",
        "description": "Many fixtures with long content; only the needle is short and unique.",
        "fixtures": [
            {
                "category": "fact",
                "title": f"long background {i}",
                "content": (
                    f"long background fact {i}: " + ("z" * 500) +
                    " — additional background detail."
                ),
                "tags": ["bg", "long"],
            }
            for i in range(10)
        ] + [
            {
                "category": "fact",
                "title": "needle short fact",
                "content": "needle short fact: env var AUTH_MODE=jwt is canonical.",
                "tags": ["needle", "env"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "AUTH_MODE jwt needle",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-LD-104",
        "category": "LONG_DISTANCE",
        "description": "Spread-across-categories: same answer token across many categories.",
        "fixtures": [
            {
                "category": cat,
                "title": f"cross-cat {cat} note",
                "content": f"cross-category {cat} note mentioning the needle term 'azalea-deploy'.",
                "tags": [cat, "needle"],
            }
            for cat in ("decision", "fact", "preference", "session-note")
        ] + [
            {
                "category": "decision",
                "title": "needle azalea",
                "content": "needle decision: 'azalea-deploy' is the codename for Q3 rollout.",
                "tags": ["decision", "needle", "azalea"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "azalea-deploy codename",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── MULTI_RELEVANT (+4) ────────────────────────────────────────────
    {
        "scenario_id": "G5b-MR-101",
        "category": "MULTI_RELEVANT",
        "description": "Three equally valid answers; ranking matters but all in top-K.",
        "fixtures": [
            {
                "category": "fact",
                "title": "db primary",
                "content": "Primary database is PostgreSQL.",
                "tags": ["db"],
            },
            {
                "category": "fact",
                "title": "db replica",
                "content": "Read replica database is PostgreSQL too (hot standby).",
                "tags": ["db", "replica"],
            },
            {
                "category": "fact",
                "title": "db analytics",
                "content": "Analytics database is PostgreSQL (separate cluster).",
                "tags": ["db", "analytics"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "postgres database",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Pair with MR-003 (mixed db engines); narrows to postgres-only.",
    },
    {
        "scenario_id": "G5b-MR-102",
        "category": "MULTI_RELEVANT",
        "description": "Subset of fixtures answers; ranking must surface them.",
        "fixtures": [
            {
                "category": "fact",
                "title": "team on-call rotation",
                "content": "Team on-call rotation cycles weekly among 4 engineers.",
                "tags": ["team", "oncall"],
            },
            {
                "category": "fact",
                "title": "team meetings",
                "content": "Team standup is daily at 10:00.",
                "tags": ["team", "meeting"],
            },
            {
                "category": "fact",
                "title": "unrelated project fact",
                "content": "Project uses Python 3.11.",
                "tags": ["runtime"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "team oncall meeting",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-MR-103",
        "category": "MULTI_RELEVANT",
        "description": "Two correct answers; one is older and one is current.",
        "fixtures": [
            {
                "category": "fact",
                "title": "team size 2025",
                "content": "Team size in 2025 is 12 engineers.",
                "tags": ["team", "size", "current"],
            },
            {
                "category": "fact",
                "title": "team size 2026",
                "content": "Team size in 2026 is 14 engineers.",
                "tags": ["team", "size", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "team size",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-MR-104",
        "category": "MULTI_RELEVANT",
        "description": "Three candidates; only two are truly correct, the third is a distractor.",
        "fixtures": [
            {
                "category": "decision",
                "title": "auth provider A",
                "content": "Auth provider A (Auth0) is used for the public API.",
                "tags": ["auth", "considered"],
            },
            {
                "category": "decision",
                "title": "auth provider B",
                "content": "Auth provider B (Keycloak) is used for the internal admin UI.",
                "tags": ["auth", "current"],
            },
            {
                "category": "decision",
                "title": "auth provider C",
                "content": "Auth provider C (Cognito) was piloted and not adopted.",
                "tags": ["auth", "rejected"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "auth provider",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── AMBIGUOUS (+4) ─────────────────────────────────────────────────
    {
        "scenario_id": "G5b-AMB-101",
        "category": "AMBIGUOUS",
        "description": "Two memories equally match a vague 'fast' query.",
        "fixtures": [
            {
                "category": "fact",
                "title": "fast cold start",
                "content": "Cold start is fast: under 800ms p95.",
                "tags": ["performance", "startup"],
            },
            {
                "category": "fact",
                "title": "fast build",
                "content": "Build is fast: under 90 seconds for the full CI pipeline.",
                "tags": ["performance", "ci"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "fast",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Pair with AMB-003 (fast build/fast tests); different facets of 'fast'.",
    },
    {
        "scenario_id": "G5b-AMB-102",
        "category": "AMBIGUOUS",
        "description": "Tag-cloud overlap: 'security' matches both auth and audit policies.",
        "fixtures": [
            {
                "category": "decision",
                "title": "auth policy",
                "content": "Auth policy: OAuth2 with PKCE for public clients.",
                "tags": ["security", "auth"],
            },
            {
                "category": "decision",
                "title": "audit policy",
                "content": "Audit policy: all admin actions logged immutably.",
                "tags": ["security", "audit"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "security",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-AMB-103",
        "category": "AMBIGUOUS",
        "description": "Two memories share the same homonym 'pipeline'.",
        "fixtures": [
            {
                "category": "fact",
                "title": "data pipeline",
                "content": "Data pipeline runs every 15 minutes.",
                "tags": ["data", "pipeline"],
            },
            {
                "category": "fact",
                "title": "ci pipeline",
                "content": "CI pipeline runs on every push to main.",
                "tags": ["ci", "pipeline"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "pipeline",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-AMB-104",
        "category": "AMBIGUOUS",
        "description": "Three memories share a single token; ranking is the only signal.",
        "fixtures": [
            {
                "category": "fact",
                "title": "deploy monday",
                "content": "Deploy happened on Monday with zero incidents.",
                "tags": ["deploy"],
            },
            {
                "category": "fact",
                "title": "deploy tuesday",
                "content": "Deploy happened on Tuesday with one minor rollback.",
                "tags": ["deploy"],
            },
            {
                "category": "fact",
                "title": "deploy wednesday",
                "content": "Deploy happened on Wednesday with full success.",
                "tags": ["deploy"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "deploy",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── TEMPORAL_UPDATE (+4) ───────────────────────────────────────────
    {
        "scenario_id": "G5b-TU-101",
        "category": "TEMPORAL_UPDATE",
        "description": "Older deploy tool superseded by newer; newer must rank first.",
        "fixtures": [
            {
                "category": "decision",
                "title": "old deploy tool",
                "content": "Old deploy tool was Capistrano (Ruby-based).",
                "tags": ["deploy", "history"],
            },
            {
                "category": "decision",
                "title": "current deploy tool",
                "content": "Current deploy tool is ArgoCD (GitOps).",
                "tags": ["deploy", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "current deploy tool",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-TU-102",
        "category": "TEMPORAL_UPDATE",
        "description": "Schema versioning: older schema supersedes, newer is canonical.",
        "fixtures": [
            {
                "category": "fact",
                "title": "old schema version",
                "content": "Old schema version is v3 (deprecated 2025-Q1).",
                "tags": ["schema", "history"],
            },
            {
                "category": "fact",
                "title": "new schema version",
                "content": "New schema version is v4 (current).",
                "tags": ["schema", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "new schema version",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-TU-103",
        "category": "TEMPORAL_UPDATE",
        "description": "Project name changed; only the new name should match 'current project'.",
        "fixtures": [
            {
                "category": "fact",
                "title": "old project name",
                "content": "Old project name was ProjectA (used until 2024-12).",
                "tags": ["name", "history"],
            },
            {
                "category": "fact",
                "title": "new project name",
                "content": "New project name is ProjectB (effective 2025-01).",
                "tags": ["name", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "current project name",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-TU-104",
        "category": "TEMPORAL_UPDATE",
        "description": "Two release versions; query asks specifically for v3 release date.",
        "fixtures": [
            {
                "category": "fact",
                "title": "v2 release date",
                "content": "v2 released on 2025-09-01.",
                "tags": ["release", "history"],
            },
            {
                "category": "fact",
                "title": "v3 release date",
                "content": "v3 released on 2026-03-15.",
                "tags": ["release", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "v3 release",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Pair with TU-004 (v2); same-shape query for a different version.",
    },
]


# ── composition ──────────────────────────────────────────────────────────


SCENARIOS = list(_V1_SCENARIOS) + list(_EXPANDED_SCENARIOS)


def all_scenarios() -> list[dict]:
    """Return the full v2 corpus (v1 ∪ expansion). 86 scenarios total.

    The runner loads each scenario via ``scenario_schema``; no
    algorithm/runner/lane change is implied by composing the lists.
    """
    return SCENARIOS


def v1_only() -> list[dict]:
    """Return the unmodified v1 subset (for callers that need the
    baseline 40 for backward-compatibility tests)."""
    return list(_V1_SCENARIOS)


def expansion_only() -> list[dict]:
    """Return just the 50 NEW v2 scenarios (not the v1 baseline)."""
    return list(_EXPANDED_SCENARIOS)


__all__ = [
    "SCENARIOS",
    "CATEGORIES",
    "all_scenarios",
    "v1_only",
    "expansion_only",
]
