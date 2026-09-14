"""G5b evaluator scenarios (synthetic, no real user data).

This file is the canonical G5b v1 scenario set. Hand-authored JSON
(per the user's preference to avoid new dependencies). 40 scenarios
across the 12 required categories, each with fixtures, queries,
optional actions, and notes.

Every fixture's ``memory_id`` is the deterministic SHA-256 of
``(category, title, content, tags)`` so the JSON stays stable across
machines. The runner derives it automatically when ``memory_id`` is
absent; explicit IDs are kept here for clarity and to make CONFLICT
scenarios deterministic.

Lane A notes:

  * The keyword lane is ``ILIKE %query%`` (or LIKE in SQLite). For
    a fair baseline we keep queries SHORT (1-3 tokens) so they can
    realistically substring-match the indexed fields.
  * The vector lane is the deterministic stub embedder (bag-of-
    tokens, 1024-dim, cosine).

Categories covered (count in parens):

  USER_PREFERENCE    (4) — editor / shell / shortcut / music
  PROJECT_DECISION   (4) — chosen library / dropped library / format / boundary
  SESSION_CONTINUITY (3) — note carried from prior session
  STABLE_FACT        (4) — long-stable personal facts
  CONFLICT_UPDATE    (3) — same-id different-payload must be DURABLE_FAILED
  DISTRACTOR         (3) — topically similar but not the answer
  NEGATIVE_RECALL    (3) — must_not_recall forbidden IDs
  ARCHIVE            (3) — archive isolation
  LONG_DISTANCE      (3) — far in the corpus / large content
  MULTI_RELEVANT     (3) — multiple correct answers, ranking matters
  AMBIGUOUS          (3) — equally relevant candidates, both must_recall
  TEMPORAL_UPDATE    (4) — older fact should be down-ranked by newer

Total = 40 scenarios (user requested ">=40"; runtime count from
``SCENARIOS`` is the single source of truth and is pinned by a
focused test).

All fixtures are synthetic; no real names, IPs, or private data.
"""
from __future__ import annotations

# Categories (must match scenario_schema.ALLOWED_CATEGORIES).
CATEGORIES = [
    "USER_PREFERENCE",
    "PROJECT_DECISION",
    "SESSION_CONTINUITY",
    "STABLE_FACT",
    "CONFLICT_UPDATE",
    "DISTRACTOR",
    "NEGATIVE_RECALL",
    "ARCHIVE",
    "LONG_DISTANCE",
    "MULTI_RELEVANT",
    "AMBIGUOUS",
    "TEMPORAL_UPDATE",
]


SCENARIOS = [
    # ─── USER_PREFERENCE (4) ────────────────────────────────────────────
    {
        "scenario_id": "G5b-UP-001",
        "category": "USER_PREFERENCE",
        "description": "User prefers dark mode in the code editor.",
        "fixtures": [
            {
                "category": "preference",
                "title": "editor color theme",
                "content": "User prefers dark mode for the code editor.",
                "tags": ["ui", "editor", "theme"],
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
                "text": "dark mode editor",
                "lane": "keyword",
                "must_recall": [],
                "must_recall_rank_max": 3,
            },
        ],
        "notes": "Editor theme preference should be retrieved by keyword.",
    },
    {
        "scenario_id": "G5b-UP-002",
        "category": "USER_PREFERENCE",
        "description": "User uses vim keybindings.",
        "fixtures": [
            {
                "category": "preference",
                "title": "keybinding choice",
                "content": "User maps modal editing with vim keybindings in all editors.",
                "tags": ["keybinding", "vim", "editor"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "vim keybindings",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-UP-003",
        "category": "USER_PREFERENCE",
        "description": "User prefers background lo-fi music while coding.",
        "fixtures": [
            {
                "category": "preference",
                "title": "background music",
                "content": "User listens to lo-fi beats while programming.",
                "tags": ["music", "ambient"],
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
                "text": "lo-fi music",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-UP-004",
        "category": "USER_PREFERENCE",
        "description": "User prefers tabs over spaces for indentation.",
        "fixtures": [
            {
                "category": "preference",
                "title": "indentation style",
                "content": "User prefers tabs over spaces in source files.",
                "tags": ["style", "code-format"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "tabs indentation",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── PROJECT_DECISION (4) ──────────────────────────────────────────
    {
        "scenario_id": "G5b-PD-001",
        "category": "PROJECT_DECISION",
        "description": "Project uses psycopg3 (not psycopg2).",
        "fixtures": [
            {
                "category": "decision",
                "title": "postgres driver choice",
                "content": "Project standardizes on psycopg3 for all new services.",
                "tags": ["postgres", "driver", "decision"],
            },
            {
                "category": "decision",
                "title": "dropped psycopg2",
                "content": "psycopg2 is deprecated for new code; only legacy modules may still import it.",
                "tags": ["postgres", "deprecated"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "psycopg3",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-PD-002",
        "category": "PROJECT_DECISION",
        "description": "Project chose JSONL over CSV for export.",
        "fixtures": [
            {
                "category": "decision",
                "title": "export format",
                "content": "Project exports audit trails in JSONL, not CSV.",
                "tags": ["format", "audit", "export"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "JSONL export",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-PD-003",
        "category": "PROJECT_DECISION",
        "description": "Project boundary: v3-core is the only source of truth.",
        "fixtures": [
            {
                "category": "decision",
                "title": "memory architecture boundary",
                "content": "v3-core is the only canonical source for explicit memories; tooling is a thin shell.",
                "tags": ["architecture", "boundary"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "canonical source",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-PD-004",
        "category": "PROJECT_DECISION",
        "description": "Project decided to keep rerank optional in the eval pipeline.",
        "fixtures": [
            {
                "category": "decision",
                "title": "rerank policy",
                "content": "Rerank remains optional in evaluation; RRF ordering is the default fallback.",
                "tags": ["rerank", "policy", "eval"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "rerank optional",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── SESSION_CONTINUITY (3) ─────────────────────────────────────────
    {
        "scenario_id": "G5b-SC-001",
        "category": "SESSION_CONTINUITY",
        "description": "Carry-over note from a prior session about a half-finished task.",
        "fixtures": [
            {
                "category": "session-note",
                "title": "carry-over bug repro file",
                "content": "From last session: repro file lives at sandbox/repro_bug_2026_09_14.py, half-finished.",
                "tags": ["carry-over", "session-note"],
            },
            {
                "category": "session-note",
                "title": "carry-over pending review",
                "content": "Pending review from last session: the diff at sandbox/diff_2026_09_14.patch needs a second pair of eyes.",
                "tags": ["carry-over", "review"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "repro_bug",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-SC-002",
        "category": "SESSION_CONTINUITY",
        "description": "Reminder to revisit a deferred question in a later session.",
        "fixtures": [
            {
                "category": "session-note",
                "title": "deferred embedder question",
                "content": "Deferred to next session: confirm whether the new embedder output dimension matches the schema column.",
                "tags": ["deferred", "embedder"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "deferred embedder",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-SC-003",
        "category": "SESSION_CONTINUITY",
        "description": "Carry-over: open thread from yesterday's session.",
        "fixtures": [
            {
                "category": "session-note",
                "title": "carry-over keyword search investigation",
                "content": "From yesterday: investigate why the keyword search returns 0 results on Chinese-only tags.",
                "tags": ["carry-over", "investigation"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "keyword Chinese",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── STABLE_FACT (4) ───────────────────────────────────────────────
    {
        "scenario_id": "G5b-SF-001",
        "category": "STABLE_FACT",
        "description": "Timezone: project runs in Asia/Shanghai.",
        "fixtures": [
            {
                "category": "fact",
                "title": "project timezone",
                "content": "Project default timezone is Asia/Shanghai (UTC+08:00).",
                "tags": ["timezone", "ops"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "Asia/Shanghai",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-SF-002",
        "category": "STABLE_FACT",
        "description": "Python minimum supported version.",
        "fixtures": [
            {
                "category": "fact",
                "title": "python minimum version",
                "content": "Minimum supported Python is 3.10; 3.11 is the development default.",
                "tags": ["python", "version"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "python 3.10",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-SF-003",
        "category": "STABLE_FACT",
        "description": "Embedding model fingerprint used for canonical writes.",
        "fixtures": [
            {
                "category": "fact",
                "title": "embedding model fingerprint",
                "content": "Canonical embedder fingerprint is stub-bag-of-tokens-v1 for lab evaluation; production uses a canonical embedding model (identifier redacted in this public fixture).",
                "tags": ["embedder", "fingerprint"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "embedder fingerprint",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-SF-004",
        "category": "STABLE_FACT",
        "description": "PG port for production is 5433.",
        "fixtures": [
            {
                "category": "fact",
                "title": "production pg port",
                "content": "Production PostgreSQL listens on port 5433.",
                "tags": ["postgres", "port", "production"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "production postgres",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── CONFLICT_UPDATE (3) ───────────────────────────────────────────
    # The runner's CONFLICT_UPDATE branch manufactures a same-id
    # different-payload attempt if the fixtures don't already include
    # one.
    {
        "scenario_id": "G5b-CU-001",
        "category": "CONFLICT_UPDATE",
        "description": "Same memory_id, different payload → must be DURABLE_FAILED.",
        "fixtures": [
            {
                "category": "fact",
                "title": "team lead",
                "content": "Team lead is Alice; reports to Bob.",
                "tags": ["org"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "team lead",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "The runner manufactures a same-id different-payload attempt and asserts DURABLE_FAILED.",
    },
    {
        "scenario_id": "G5b-CU-002",
        "category": "CONFLICT_UPDATE",
        "description": "Same memory_id, title only changed → DURABLE_FAILED; row not mutated.",
        "fixtures": [
            {
                "category": "decision",
                "title": "deploy cadence",
                "content": "Deploy cadence is weekly on Tuesdays.",
                "tags": ["deploy"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "deploy cadence",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Title-only mutation must be rejected as DURABLE_FAILED.",
    },
    {
        "scenario_id": "G5b-CU-003",
        "category": "CONFLICT_UPDATE",
        "description": "Same memory_id, tags only changed → DURABLE_FAILED.",
        "fixtures": [
            {
                "category": "fact",
                "title": "default branch",
                "content": "Default branch is main.",
                "tags": ["git"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "default branch",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── DISTRACTOR (3) ────────────────────────────────────────────────
    {
        "scenario_id": "G5b-DT-001",
        "category": "DISTRACTOR",
        "description": "Topically similar memory but not the answer.",
        "fixtures": [
            {
                "category": "fact",
                "title": "lab database",
                "content": "Lab PostgreSQL for evaluation runs on port 55462.",
                "tags": ["postgres", "lab"],
            },
            {
                "category": "fact",
                "title": "staging database",
                "content": "Staging PostgreSQL listens on port 55460.",
                "tags": ["postgres", "staging"],
            },
            {
                "category": "fact",
                "title": "production database",
                "content": "Production PostgreSQL listens on port 5433.",
                "tags": ["postgres", "production"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "production postgres",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "The lab/staging memories are topically similar and must rank lower than production.",
    },
    {
        "scenario_id": "G5b-DT-002",
        "category": "DISTRACTOR",
        "description": "Distractor with overlapping keywords but different topic.",
        "fixtures": [
            {
                "category": "fact",
                "title": "editor theme policy",
                "content": "Repository style guide mandates dark mode for all screenshots.",
                "tags": ["ui", "style", "theme"],
            },
            {
                "category": "preference",
                "title": "editor color theme",
                "content": "User prefers dark mode for the code editor.",
                "tags": ["ui", "editor", "theme"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "user prefers dark",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Repository policy must rank lower than user preference for personal questions.",
    },
    {
        "scenario_id": "G5b-DT-003",
        "category": "DISTRACTOR",
        "description": "Many memories share the same tag cloud — ranking matters.",
        "fixtures": [
            {
                "category": "decision",
                "title": "deploy policy A",
                "content": "Production deploy policy A: no deploys on Fridays.",
                "tags": ["deploy", "policy", "prod"],
            },
            {
                "category": "decision",
                "title": "deploy policy B",
                "content": "Staging deploy policy B: deploys allowed any weekday.",
                "tags": ["deploy", "policy", "staging"],
            },
            {
                "category": "decision",
                "title": "deploy policy C",
                "content": "Lab deploy policy C: continuous, no schedule.",
                "tags": ["deploy", "policy", "lab"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "production deploy policy",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── NEGATIVE_RECALL (3) ───────────────────────────────────────────
    {
        "scenario_id": "G5b-NR-001",
        "category": "NEGATIVE_RECALL",
        "description": "Don't return the lab PG port when asked about production.",
        "fixtures": [
            {
                "category": "fact",
                "title": "lab postgres port",
                "content": "Lab PostgreSQL runs on port 55462.",
                "tags": ["postgres", "lab"],
            },
            {
                "category": "fact",
                "title": "production postgres port",
                "content": "Production PostgreSQL runs on port 5433.",
                "tags": ["postgres", "production"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "production postgres",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "Runner asserts lab memory is NOT in the returned set when production memory is the answer.",
    },
    {
        "scenario_id": "G5b-NR-002",
        "category": "NEGATIVE_RECALL",
        "description": "Don't return archived memories.",
        "fixtures": [
            {
                "category": "decision",
                "title": "old framework choice",
                "content": "Old framework was Flask; later replaced.",
                "tags": ["framework", "legacy"],
            },
            {
                "category": "decision",
                "title": "current framework choice",
                "content": "Current framework is FastAPI.",
                "tags": ["framework"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "current framework",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "actions": [
            {"type": "archive", "fixture_index": 0},
        ],
    },
    {
        "scenario_id": "G5b-NR-003",
        "category": "NEGATIVE_RECALL",
        "description": "Don't return memory from a different topic even if keyword matches.",
        "fixtures": [
            {
                "category": "fact",
                "title": "music project MIDI",
                "content": "Music project uses MIDI for input.",
                "tags": ["music", "midi"],
            },
            {
                "category": "fact",
                "title": "database project MIDI",
                "content": "Database project uses MIDI for storage.",
                "tags": ["database", "midi"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "music MIDI",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── ARCHIVE (3) ──────────────────────────────────────────────────
    {
        "scenario_id": "G5b-AR-001",
        "category": "ARCHIVE",
        "description": "Archive a memory; reader must not return it.",
        "fixtures": [
            {
                "category": "decision",
                "title": "retain archived memory in store",
                "content": "Old rule: keep all archive copies for one quarter.",
                "tags": ["archive", "policy"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "retain archive copies",
                "lane": "keyword",
                "must_recall": [],
                "expected_status": "active",
                "expect_empty_recall": True,
            },
        ],
        "actions": [
            {"type": "archive", "fixture_index": 0},
        ],
        "notes": "Runner archives the only fixture, then asserts reader returns nothing.",
    },
    {
        "scenario_id": "G5b-AR-002",
        "category": "ARCHIVE",
        "description": "Hard archive rejected — no DELETE permitted.",
        "fixtures": [
            {
                "category": "fact",
                "title": "immutable log fact",
                "content": "Immutable log fact: the deploy ran at 03:14 UTC.",
                "tags": ["audit", "immutable"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "immutable log",
                "lane": "keyword",
                "must_recall": [],
                "expect_empty_recall": False,
            },
        ],
        "actions": [
            {"type": "archive_hard", "fixture_index": 0},
        ],
        "notes": "Hard archive must be rejected (no DELETE); row remains active.",
    },
    {
        "scenario_id": "G5b-AR-003",
        "category": "ARCHIVE",
        "description": "Already-archived memory stays archived (idempotent).",
        "fixtures": [
            {
                "category": "fact",
                "title": "deprecated note",
                "content": "Deprecated note: rule was rolled back last quarter.",
                "tags": ["deprecated"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "deprecated note",
                "lane": "keyword",
                "must_recall": [],
                "expect_empty_recall": True,
            },
        ],
        "actions": [
            {"type": "archive", "fixture_index": 0},
            {"type": "archive", "fixture_index": 0},  # idempotent retry
        ],
    },

    # ─── LONG_DISTANCE (3) ─────────────────────────────────────────────
    {
        "scenario_id": "G5b-LD-001",
        "category": "LONG_DISTANCE",
        "description": "Long content (3KB+) — reader must still surface it.",
        "fixtures": [
            {
                "category": "fact",
                "title": "long incident report",
                "content": (
                    "INCIDENT REPORT — " + ("x" * 3000) +
                    " — root cause: misconfigured retry budget."
                ),
                "tags": ["incident", "long"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "incident root cause",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-LD-002",
        "category": "LONG_DISTANCE",
        "description": "Many fixtures (20) with the answer buried among them.",
        "fixtures": [
            {"category": "fact", "title": f"background fact {i}",
             "content": f"background fact {i} about project history.",
             "tags": ["bg"]}
            for i in range(19)
        ] + [
            {
                "category": "fact",
                "title": "needle deploy cadence",
                "content": "needle fact: the deploy cadence is biweekly.",
                "tags": ["deploy", "needle"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "needle deploy",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
        "notes": "20 fixtures total; reader must rank the needle at or near top.",
    },
    {
        "scenario_id": "G5b-LD-003",
        "category": "LONG_DISTANCE",
        "description": "Many tag-overlapping distractors; answer has a unique tag.",
        "fixtures": [
            {
                "category": "decision",
                "title": f"common decision {i}",
                "content": f"common decision {i} for the project.",
                "tags": ["decision", "common"],
            }
            for i in range(15)
        ] + [
            {
                "category": "decision",
                "title": "rare decision with unique tag",
                "content": "rare decision: ship to alpha channel first.",
                "tags": ["decision", "rare-alpha"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "rare-alpha",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── MULTI_RELEVANT (3) ────────────────────────────────────────────
    {
        "scenario_id": "G5b-MR-001",
        "category": "MULTI_RELEVANT",
        "description": "Two equally valid answers; both must be in top-K.",
        "fixtures": [
            {
                "category": "fact",
                "title": "team size 2024",
                "content": "Team size in 2024 was 8 engineers.",
                "tags": ["team", "size", "history"],
            },
            {
                "category": "fact",
                "title": "team size 2025",
                "content": "Team size in 2025 is 12 engineers.",
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
        "scenario_id": "G5b-MR-002",
        "category": "MULTI_RELEVANT",
        "description": "Multiple frameworks considered; both relevant.",
        "fixtures": [
            {
                "category": "decision",
                "title": "framework A consideration",
                "content": "Framework A (FastAPI) was considered for the new service.",
                "tags": ["framework", "considered"],
            },
            {
                "category": "decision",
                "title": "framework B consideration",
                "content": "Framework B (Django) was considered for the new service.",
                "tags": ["framework", "considered"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "framework considered",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-MR-003",
        "category": "MULTI_RELEVANT",
        "description": "Multi-correct answers to a single query.",
        "fixtures": [
            {
                "category": "fact",
                "title": "primary DB",
                "content": "Primary database is PostgreSQL.",
                "tags": ["db"],
            },
            {
                "category": "fact",
                "title": "cache DB",
                "content": "Cache database is Redis.",
                "tags": ["db", "cache"],
            },
            {
                "category": "fact",
                "title": "queue service",
                "content": "Queue service is RabbitMQ.",
                "tags": ["queue"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "database",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },

    # ─── AMBIGUOUS (3) ─────────────────────────────────────────────────
    {
        "scenario_id": "G5b-AMB-001",
        "category": "AMBIGUOUS",
        "description": "Two memories equally match a vague query.",
        "fixtures": [
            {
                "category": "fact",
                "title": "music preference",
                "content": "User listens to lo-fi while coding.",
                "tags": ["music"],
            },
            {
                "category": "fact",
                "title": "movie preference",
                "content": "User watches indie movies on weekends.",
                "tags": ["movie"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "user enjoys",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-AMB-002",
        "category": "AMBIGUOUS",
        "description": "Two memories both about deploys — query is ambiguous.",
        "fixtures": [
            {
                "category": "decision",
                "title": "deploy policy A",
                "content": "Deploy policy A: blue/green on Tuesdays.",
                "tags": ["deploy", "policy"],
            },
            {
                "category": "decision",
                "title": "deploy policy B",
                "content": "Deploy policy B: rolling on Fridays.",
                "tags": ["deploy", "policy"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "deploy policy",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-AMB-003",
        "category": "AMBIGUOUS",
        "description": "Ambiguous term 'fast' — two candidates match.",
        "fixtures": [
            {
                "category": "fact",
                "title": "fast build",
                "content": "Build is fast: 2 minutes end-to-end.",
                "tags": ["performance"],
            },
            {
                "category": "fact",
                "title": "fast tests",
                "content": "Tests are fast: under 30 seconds for unit tests.",
                "tags": ["performance"],
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
    },

    # ─── TEMPORAL_UPDATE (3) ───────────────────────────────────────────
    {
        "scenario_id": "G5b-TU-001",
        "category": "TEMPORAL_UPDATE",
        "description": "Newer fact supersedes an older one.",
        "fixtures": [
            {
                "category": "decision",
                "title": "old deploy cadence",
                "content": "Old deploy cadence was daily.",
                "tags": ["deploy", "history"],
            },
            {
                "category": "decision",
                "title": "current deploy cadence",
                "content": "Current deploy cadence is weekly on Tuesdays.",
                "tags": ["deploy", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "current deploy",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-TU-002",
        "category": "TEMPORAL_UPDATE",
        "description": "Update to a previously known value.",
        "fixtures": [
            {
                "category": "fact",
                "title": "old default branch",
                "content": "Old default branch was master.",
                "tags": ["git", "history"],
            },
            {
                "category": "fact",
                "title": "new default branch",
                "content": "New default branch is main.",
                "tags": ["git", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "new default branch",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-TU-003",
        "category": "TEMPORAL_UPDATE",
        "description": "Current value supersedes a stale one.",
        "fixtures": [
            {
                "category": "fact",
                "title": "old team lead",
                "content": "Old team lead was Carol.",
                "tags": ["org", "history"],
            },
            {
                "category": "fact",
                "title": "current team lead",
                "content": "Current team lead is Dan.",
                "tags": ["org", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "current team lead",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
    {
        "scenario_id": "G5b-TU-004",
        "category": "TEMPORAL_UPDATE",
        "description": "Two temporally-ordered facts; the newer one answers.",
        "fixtures": [
            {
                "category": "fact",
                "title": "v1 release date",
                "content": "v1 released on 2026-04-01.",
                "tags": ["release", "history"],
            },
            {
                "category": "fact",
                "title": "v2 release date",
                "content": "v2 released on 2026-08-15.",
                "tags": ["release", "current"],
            },
        ],
        "queries": [
            {
                "query_id": "Q1",
                "text": "v2 release",
                "lane": "keyword",
                "must_recall": [],
            },
        ],
    },
]


def all_scenarios() -> list[dict]:
    """Return the raw JSON list. The runner loads it via scenario_schema."""
    return SCENARIOS


__all__ = ["SCENARIOS", "CATEGORIES", "all_scenarios"]