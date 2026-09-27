# Hippocampus Reliability & Recovery — Design (v1, feature/reliability-recovery-v1)

> Status: DESIGN (implementation spec for this feature branch). Not a release document.
> Scope: three new CLI surfaces — `health`, `diagnose`, `repair --dry-run` — plus the failure
> accounting reader they share. **No `repair --apply` in v1. No production writes of any kind.**

## 0. Goals / non-goals

Goals (from the round dispatch):
- Answer "is it healthy right now?" (`health`), "why not?" (`diagnose`), "what would a repair do?"
  (`repair --dry-run`) — deterministically, read-only, machine-readable, secret-safe.
- **Separate historical debt from current incident** (105 legacy NULL embeddings / 453 empty
  answers must not keep production permanently "unhealthy").
- Turn the scattered truth sources (PG counts, marker files, observer state, runtime paths) into
  a single formal façade with stable JSON + exit codes.

Non-goals: repair apply, schema changes, UI, benchmark runs, altering the write pipeline.

## 1. Module layout

```
src/v3-core/src/v3core/reliability/
  __init__.py        # public API re-exports + contract constants
  models.py          # dataclasses + to_dict(): CheckResult, HealthReport, Diagnosis, RepairAction
  redaction.py       # path labels, secret scrub
  failure_reader.py  # FailureReader — marker ledger (the formal "failure accounting" reader)
  health.py          # HealthService — collects all sections into a HealthReport
  diagnose.py        # Diagnoser — HealthReport -> list[Diagnosis]
  repair.py          # RepairPlanner — list[Diagnosis] -> list[RepairAction] (dry-run only)
  cli.py             # thin handlers: handle_health/handle_diagnose/handle_repair(args) -> exit code
```

`distribution_cli.py` gains three subcommands (`health`, `diagnose`, `repair`) that lazily import
`v3core.reliability.cli` and dispatch (same lazy-import pattern as install/import/rebuild).

## 2. Contract constants

```python
REPORT_SCHEMA_VERSION = "1"
HEALTH_WINDOW_HOURS_DEFAULT = 24      # "recent" window for current-incident classification
STALE_MARKER_HOURS = 48               # in_flight / pending_db markers older than this = stale
AUTH_TIMEOUT_SECONDS = 10             # --deep provider probes

CANONICAL_TABLES = (                  # v0.2.1 contract
  "conversation_stream", "qa_pairs", "topics", "topic_entries", "observation_notes",
  "yin_paragraphs", "explicit_memories", "qa_embedding_chunks", "schema_versions",
)
CANONICAL_INDEXES = (
  "explicit_memories_embedding_ivfflat", "explicit_memories_status_active_idx",
  "explicit_memories_created_at_idx", "explicit_memories_tags_gin",
  "qa_embedding_chunks_qa_id_idx", "qa_embedding_chunks_embedding_ivfflat",
)
EXPECTED_SCHEMA_VERSION = "v0.2"

# Production boundary — MUST match distribution_cli semantics (port 5433, or
# loopback host + database "v3embeddings"). Locked by a cross-module test.
```

## 3. Data model (models.py)

All dataclasses with `.to_dict()` (pure JSON-safe; keys fixed, additive-only evolution).

```python
@dataclass CheckResult:
    check_id: str; section: str; status: str        # ok|warn|fail|skip|unknown
    summary: str; evidence: dict; duration_ms: int

@dataclass HealthReport:
    schema_version: str = "1"
    overall: str                                     # healthy|degraded|unhealthy
    generated_at: str                                # ISO8601 UTC
    profile: dict                                    # resolved profile info (labels, no secrets)
    runtime: dict; storage: dict; memory_write: dict
    failure_accounting: dict; derived_memory: dict
    providers: dict; metrics: dict
    checks: list[CheckResult]
    window_hours: int; deep: bool

@dataclass Diagnosis:
    code: str; severity: str                         # info|warning|error
    scope: str                                       # runtime|storage|memory_write|failure_accounting|derived_memory|providers
    summary: str; evidence: dict; repairable: bool

@dataclass RepairAction:
    action_id: str; issue_code: str; target_count: int
    risk: str                                        # low|medium|high
    reversible: bool; requires_provider: bool
    estimated_remote_calls: int | None; estimated_cost: dict | None
    writes_database: bool; automatic_safe: bool; reason: str
```

Type policy (schema stability): timestamps ISO8601 strings; durations seconds (float);
counts int; "unknown" is always `None` or an explicit enum string — never mixed types.

## 4. Section specs (health.py)

Each check emits a `CheckResult` with `evidence` carrying the machine fields. Data sources:
PG (injectable `pg_connect`), profile dir files, marker dir, `importlib.metadata`.

### runtime (local, no IO beyond metadata)
- `RT01_import_source` — classify `v3core.__file__`: `site_packages` | `editable` | `unknown`.
  evidence: `{kind, label}` where label = redacted path label. fail when kind == "editable"
  (a wheel runtime is the shipped contract); warn when unknown.
- `RT02_distribution` — evidence `{v3_core_version, v3_hermes_plugin_version, entry_point_present}`.
  fail if v3-core missing; warn if plugin/entry-point missing.
- `RT03_python` — evidence `{executable_label, version}`. Always ok unless unresolvable.
- `RT04_profile` — evidence `{profile_label, config_parsed, base_path_label}`. fail if config
  unparseable when a profile was expected.

### storage (PG; requires read permission)
- `ST01_pg_reachable` — `SELECT 1` with short connect_timeout. fail on error;
  evidence `{reachable, server_version, latency_ms}`.
- `ST02_pgvector` — `SELECT extversion FROM pg_extension WHERE extname='vector'`. fail when absent.
- `ST03_schema_ledger` — `schema_versions` rows; fail when the `v0.2` row is absent.
- `ST04_canonical_tables` — presence of CANONICAL_TABLES; fail listing missing.
- `ST05_canonical_indexes` — presence of CANONICAL_INDEXES; warn listing missing.

When production boundary and no `--allow-production-read`: storage checks report `skip` with
summary "production read not authorized; pass --allow-production-read".

### memory_write (PG)
- `MW01_write_pipeline_recent` — window query on `qa_pairs.created_at`:
  `{recent_qa, recent_embedding_ok, recent_embedding_null, recent_empty_answer}`.
  fail if `recent_embedding_null > 0`; unknown if `recent_qa == 0` (no traffic);
  ok otherwise. **This is the current-incident detector.**
- `MW02_embedding_debt` — `{embedding_null_total, oldest_null_created_at, newest_null_created_at}`.
  Always `warn`-free: status ok (debt is reported in evidence; diagnose turns it into an info issue).
  (Design note: debt must never force the overall verdict — see §6.)
- `MW03_empty_answer` — `{empty_answer_total, empty_answer_recent}`; warn if recent > 0; ok otherwise.
- `MW04_last_writes` — `{last_qa_created_at, last_embedding_success_at}` + ages. ok; unknown only
  when table empty.
- `MW05_explicit_memory_embedding` — `{explicit_total, explicit_embedding_null, explicit_embedding_set}`. warn if null>0 and set>0? No: explicit memories may be written without embedding
  when embedding disabled — evidence only, warn when `explicit_embedding_null>0`.
- `MW06_longqa_child_consistency` — over `qa_embedding_chunks`:
  `{child_rows, distinct_parents, parents_missing_parent_row, child_null_embedding,
    child_bad_offsets, child_duplicate_keys}` — each counter must be 0 or the check fails
  (integrity violations). Empty table = ok.

### failure_accounting (marker dir; local IO — FailureReader)
- `FA01_ledger` — totals by normalized status; evidence = ledger summary. fail if `malformed>0`?
  No: malformed markers are `warn` (see FA04); FA01 itself is ok/warn per composition.
- `FA02_recent_failures` — failures with `last_failure_at` (or mtime fallback) inside window.
  fail when any recent `failed/poisoned/unresolved/stale`; ok when zero.
- `FA03_poisoned` — total + recent poisoned; warn when total>0 (historical isolation items),
  fail when recent>0. evidence includes `by_error_class`.
- `FA04_malformed` — unparseable marker files; warn when >0.
- `FA05_stale` — stale in_flight/pending_db markers; warn when >0.

### derived_memory (PG + observer_state.json)
- `DM01_topics` — `{topics_total, last_observer_ts, age_seconds}`. unknown when absent; warn when
  age > 7d AND qa backlog exists (heuristic); else ok.
- `DM02_observer_cursor` — observer_state.json `last_qa_id` vs `max(qa_pairs.id)`:
  `{last_qa_id, qa_head_id, backlog, updated_at, age_seconds}`. warn when backlog > 100 or
  updated_at stale (> 7d) while backlog > 0; never fail (contract not proven).
- `DM03_observation_notes` — max created_at age. unknown/warn as DM01.
- `DM04_derived_last` — max(yin_paragraphs.created_at), max(topics.last_observer_ts) ages. same policy.
All freshness checks expose `age_seconds`; none hard-fail in v1 (§12 of the round dispatch).

### providers (config + optional --deep)
- `PR01_configured` — `{embed_configured, rerank_configured, llm_configured}` (endpoint+key
  presence; no secrets). fail if embed missing (write pipeline needs it); warn if rerank/llm missing.
- `PR02_failure_ledger` — from FA ledger: `{recent_provider_errors: {class: count}}`
  (classes provider_401/402/429/5xx/timeout/connection...). fail when a recent
  provider_401/402 (credentials/plan) exists; warn for transient classes; ok otherwise.
- `PR03_deep_auth` (**only with --deep**) — three bounded live probes; reuse doctor_full's
  `_do_auth_check` machinery (import, don't duplicate HTTP code). skip when not deep.

### metrics
- `{qa_pairs_total, conversation_stream_total, topics_total, topic_entries_total,
   observation_notes_total, yin_paragraphs_total, collection_seconds, check_count}`.

## 5. Aggregation & exit codes

Aggregation: any `fail` → `unhealthy`; else any `warn` → `degraded`; else any `unknown` →
`degraded`; else `healthy`. `skip` never affects the verdict.

**Skip vs unknown semantics (locked during A-round acceptance):**
- `skip` = *not applicable right now* — no traffic yet, fresh install, deep probes not requested,
  production read not authorized. It must never degrade the verdict.
- `unknown` = *we tried to read it and could not* — corrupt state files, tables unreadable,
  data missing that should exist. This lowers the verdict (degraded), so genuine blind spots
  surface instead of hiding behind "healthy".
- Every check must pick the correct one; "no data" is `skip`, "broken read" is `unknown`.

Exit codes (fixed; mirrored in tests + docs):
- `health`: 0 healthy | 1 degraded | 2 unhealthy or collection hard-failure
- `diagnose`: 0 no active issue (info-only or empty) | 1 active issue (severity>=warning) | 2 diagnose failed
- `repair --dry-run`: 0 no repair candidates | 1 repair candidates exist | 2 planner failed / unsafe

## 6. Historical debt vs current incident (the core contract)

- **Current incident** := failures inside the *effective window*. The effective boundary is
  `max(now - window_hours, last_embedding_success_at)` — i.e. a failure only counts as current
  when it happened **at/after the most recent successful embedding**. With no known success the
  rolling window is the fallback. This is what lets 112 old poisoned markers (pre-restart) and
  105 legacy NULL embeddings coexist with a `healthy` verdict while a *new* failure surfaces
  immediately (its timestamp is later than the last success).
- **Historical debt** := totals minus current. Reported as evidence/metrics and as `info`-level
  diagnoses (`HISTORICAL_EMBEDDING_DEBT`, `EMPTY_ANSWER_HISTORICAL_DEBT`). **Never lowers the
  overall verdict by itself.**
- Locked behaviors (tests):
  1. `embedding_null_total=105, recent_null=0` → memory_write ok; overall may be `healthy`;
     diagnose emits `HISTORICAL_EMBEDDING_DEBT (info)` only.
  2. `recent_null>0` → `MW01` fail + diagnose `RECENT_EMBEDDING_FAILURE (error)`.
  3. Same for empty answers (`recent>0` → warn + `EMPTY_ANSWER_RECENT`).
  4. Poisoned markers older than the last success → FA03 ok (reported); newer → FA03 fail.
  5. NEVER write `NULL>0 => fail`.

## 7. FailureReader spec (failure_reader.py)

Marker dir: `<base_path>/j/pending_qa/*.json` (base = resolved profile dir).

Marker schema tolerance — **two field generations exist in production**:
- legacy (`v1_legacy`): `embedding_status, embedding_attempts, embedding_error_fingerprint,
  embedding_next_retry_at, job_id, pending{q,a,q_msg_id,q_ts,q_turn}, session_id, version`
- new (`v2`): adds `error_class, error_phase, error_fingerprint, provider_status, retryable,
  first_failure_at, last_failure_at, failure_count`

Normalization (priority order):
1. unparseable / missing job_id / missing pending → `malformed`
2. `embedding_status == "recovered"` (future/foreign) → `recovered`
3. `embedding_status == "poisoned"` → `poisoned`
4. `embedding_status == "failed"` → `retryable` while `embedding_next_retry_at` is still in the
   future; `retry_due` once it has passed (the system owed a retry). Legacy markers without a
   schedule fall back to the age heuristic (within the recent window = retryable, older = due).
5. `embedding_status == "in_flight"` → `stale_in_flight` when age>STALE else `in_flight`
6. `embedding_status == "embedding_succeeded_pending_db"` → `stale_pending_db` when age>STALE
   else `pending_db`
7. no status and attempts==0 → `pending`
8. otherwise → `unresolved`

Record fields: `job_id, session_id, status(normalized), raw_status, error_class,
error_fingerprint, provider_status, retryable, attempts, first_failure_at, last_failure_at,
next_retry_at, file_mtime, age_seconds, is_recent, marker_generation`.

Ledger: `{total, by_status, by_error_class, recent_count, recent_by_status, recent_by_error_class,
malformed, stale, newest_mtime, oldest_failure_at, newest_failure_at, marker_dir_label,
scan_seconds}`.

**Never read or emit `pending.q` / `pending.a` contents.** Duplicate detection: same
`pending.q_msg_id` appearing in >1 live marker → counted as `duplicates` (warn evidence).
All file reads are bounded (JSON only, size cap 5 MB per file).

## 8. Diagnose codes (diagnose.py)

| code | severity | trigger | repairable |
|---|---|---|---|
| RUNTIME_EDITABLE_ACTIVE | error | RT01 kind==editable | false (manual ops procedure) |
| RUNTIME_UNKNOWN_IMPORT | warning | RT01 kind==unknown | false |
| PG_UNREACHABLE | error | ST01 fail | false (environment) |
| PGVECTOR_MISSING | error | ST02 fail | false (ops) |
| SCHEMA_VERSION_MISMATCH | error | ST03 fail | true (RUN_SCHEMA_UPGRADE) |
| SCHEMA_TABLE_MISSING | error | ST04 fail | true (RUN_SCHEMA_UPGRADE) |
| SCHEMA_INDEX_MISSING | warning | ST05 warn | true (RECREATE_MISSING_INDEX) |
| RECENT_EMBEDDING_FAILURE | error | MW01 fail | true (REBUILD_EMBEDDING_FOR_QA_IDS) |
| HISTORICAL_EMBEDDING_DEBT | info | MW02 total>0 and recent==0 | true (future deterministic re-embed) |
| EMPTY_ANSWER_RECENT | warning | MW03 recent>0 | false (manual review) |
| EMPTY_ANSWER_HISTORICAL_DEBT | info | MW03 total>0 and recent==0 | false (needs-source check) |
| EXPLICIT_MEMORY_EMBEDDING_NULL | warning | MW05 null>0 | true (REPAIR_EXPLICIT_MEMORY_EMBEDDING) |
| LONG_QA_CHUNK_INCONSISTENT | error/warning | MW06 counters>0 (error on orphan parents / null child embeddings; warn on offset/key anomalies) | true (REBUILD_LONG_QA_CHILDREN) |
| RECENT_FAILURE_MARKER | error | FA02 fail | true (RETRY_PENDING_FAILURES) |
| POISONED_FAILURE_MARKER | warning (error if recent>0) | FA03 | true (RETRY_PENDING_FAILURES, bounded) |
| RETRY_EXHAUSTED | warning | retry_due markers older than window | true (RETRY_PENDING_FAILURES) |
| MALFORMED_FAILURE_MARKER | warning | FA04 | false (manual) |
| STALE_PENDING_MARKER | warning | FA05 | true (RETRY_PENDING_FAILURES) |
| EMBED_PROVIDER_AUTH | error | recent provider_401/402 markers or deep auth 401/402 | false (credentials) |
| EMBED_PROVIDER_RATE_LIMIT | warning | recent provider_429 | true (retry) |
| EMBED_PROVIDER_TIMEOUT | warning | recent timeout/connection classes | true (retry) |
| OBSERVER_STALE | warning | DM02 warn | false |
| DERIVED_LAYER_STALE | warning | DM01/03/04 warn | false |
| PROVIDER_UNCONFIGURED | warning/error | PR01 (embed missing = error) | false |

Sources = the check evidence; the classifier only consumes the HealthReport (no second PG pass).

## 9. Repair planner (repair.py)

Deterministic mapping Diagnosis → RepairAction (no new data collection; counts come from evidence):

| action_id | issue_code(s) | notes |
|---|---|---|
| RUN_SCHEMA_UPGRADE | SCHEMA_VERSION_MISMATCH, SCHEMA_TABLE_MISSING | points at `hippocampus upgrade` (the audited path); risk low; reversible non-destructive |
| RECREATE_MISSING_INDEX | SCHEMA_INDEX_MISSING | additive index creation |
| REBUILD_EMBEDDING_FOR_QA_IDS | RECENT_EMBEDDING_FAILURE, HISTORICAL_EMBEDDING_DEBT | target_count = recent-null (incident) or debt total (debt); requires_provider=true; writes_database=true; automatic_safe=false in v1 |
| RETRY_PENDING_FAILURES | RECENT_FAILURE_MARKER, POISONED_FAILURE_MARKER, RETRY_EXHAUSTED, STALE_PENDING_MARKER | target_count = retryable+stale; requires_provider=true |
| REPAIR_EXPLICIT_MEMORY_EMBEDDING | EXPLICIT_MEMORY_EMBEDDING_NULL | |
| REBUILD_LONG_QA_CHILDREN | LONG_QA_CHUNK_INCONSISTENT | |
| FIX_PROVIDER_CREDENTIALS | EMBED_PROVIDER_AUTH | manual action; automatic_safe=false; writes_database=false |
| MANUAL_REVIEW_EMPTY_ANSWER | EMPTY_ANSWER_RECENT, EMPTY_ANSWER_HISTORICAL_DEBT | needs exact source recovery check |
| NO_AUTOMATIC_REPAIR_SOURCE_MISSING | (any debt with source unverifiable) | explicit "cannot guess" marker |
| MANUAL_REVIEW_MALFORMED_MARKER | MALFORMED_FAILURE_MARKER | |

Every action carries the §17 field set. `repair --dry-run` performs **zero writes** — enforced by
tests that inject a recording pg_connect and assert no non-SELECT statement ever executes, plus a
filesystem guard.

`repair --apply` is hard-disabled: the subcommand exists but exits 2 with
`REPAIR_APPLY_NOT_IMPLEMENTED_IN_V1` (no hidden path — grep-locked by test).

## 10. Secret / privacy gate

- Paths in default JSON = labels: `{"kind": "site_packages|profile|marker_dir|...", "leaf": "<basename>",
  "hash12": sha256(path)[:12]}`; full paths only with `--debug-paths`.
- Never emitted: DSNs, passwords, api keys, bearer tokens, `pending.q`/`pending.a`, prompts,
  tool payloads, raw exception bodies beyond the classified `_safe_err`-style summary.
- `redaction.sanitize(text)` shared by all emitters; a dedicated leakage test scans health /
  diagnose / repair outputs for planted secrets (password, sk- token, DSN userinfo, marker q/a).

## 11. Performance budget

- Default `health --json` (warm): target < 2 s wall; ≤ 12 PG round-trips; marker scan < 150 ms at
  200 markers. Recent-window queries use `created_at` range predicates; totals use `count(*)`
  (fine at current scale) — **a `qa_pairs.created_at` index is recorded as a PROPOSAL in docs,
  not applied** (production schema is frozen this round).
- `--deep` adds at most 3 bounded HTTP probes (10 s timeout each, no retries).
- No query may scan `conversation_stream`/`qa_pairs` row-by-row in Python.

## 12. Test plan (pyramid)

- Unit (offline): models serialization; aggregation; diagnose classifier over synthetic
  HealthReports; repair mapping; redaction; failure_reader over crafted marker fixtures
  (all 7+ statuses, both field generations, malformed, duplicate, oversized);
  CLI arg parsing + exit-code mapping (fake services).
- Integration (disposable PG): storage/memory_write/derived sections against a real
  production-shaped DB (existing canary recipe); observer_state fixtures.
- Fault matrix (lab): A–I classes from the round dispatch — each with
  fault → health → diagnose → repair assertions (see docs/RELIABILITY-MATRIX.md).
- CLI end-to-end: run `hippocampus health/diagnose/repair --dry-run --json` against the lab DB;
  exit codes; secret scan of stdout/stderr.
- Perf: timed health run + query count via recording connection (asserts budget).

## 13. Hard out-of-scope (unless a later round authorizes)

repair apply; production DDL/DML; historical data repair; schema changes (index proposals only);
UI; benchmark.
