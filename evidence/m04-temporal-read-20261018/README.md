# M04 — point-in-time read (`as_of`) acceptance

Date: 2026-10-18
Contract: `docs/M04-TEMPORAL-READ.md`
Branch: `feature/m04-temporal-read`

## What was run, where

| Step | Command | Result |
| --- | --- | --- |
| Unit + contract suite | `pytest tests/test_m01_memory_correction.py tests/test_m02_memory_correction_tools.py tests/test_m04_temporal_read.py` | **324 passed** (291 pre-existing + 33 new) |
| Isolated real PostgreSQL acceptance | `m04_temporal_e2e.py <src-dir>` | **36 / 36 checks, 0 failed**, exit 0 |
| Baseline comparison | same suite against a tree without the M04 changes | 291 passed, 0 failed → no pre-existing test moved |

Runs executed on a **dedicated isolated test machine** (host gate verified before
every remote step). The acceptance database is a throwaway database created from
scratch per run on the disposable PostgreSQL instance at `127.0.0.1:55433`; the
script refuses port 5433 (production) and any non-loopback host before sending a
single statement. Production was never read or written.

## What the real-PG suite covers

Edges are produced by the **real writer** (`ActiveMemoryWriter.correct`), so ids,
constraints and the `effective_at` column all come from production code. Only the
clock is controlled: after each correction the fixture rewrites `recorded_at`
(and the head's `created_at`) to fixed instants.

- A→B→C resolved in each of its three windows, plus the exact boundary instant
- `basis='recorded'` (default, echoed) vs `basis='effective'` giving *different*
  answers at the same `as_of`
- `effective_at` NULL on the edge the decision needs → `EFFECTIVE_TIME_INDETERMINATE`
  (no fallback to `recorded_at`), while the same chain answers normally under
  `basis='recorded'`
- withdrawal before / after its edge
- `as_of` earlier than the chain's first version → `NO_VERSION_AT_AS_OF` + `absent`
- future `as_of` → present state with `as_of_future=true`
- naive / garbage / empty `as_of`, `as_of`+`history`, `as_of`+`include_history`,
  `basis` without `as_of`, unknown `basis` → each `INVALID_REQUEST`
- cycle → `DURABLE_FAILED` (time filtering does not bypass integrity checks)
- unknown id → `NOT_FOUND`
- `as_of=None` and `mode='history'` → byte-identical shape/labels to before
- revision 4: a version archived **today** is still reported `active` at a past
  instant — the answer comes from the edge timeline, not from the status column
- 25 reads issued while a writer kept committing → no torn answer

## Honest limits

- The concurrency check is a **stress probe**: it can only observe a torn read,
  never prove its absence. The structural guarantee is that one `as_of` read uses
  exactly ONE `READ ONLY REPEATABLE READ` snapshot; that is pinned deterministically
  in `test_M04_as_of_read_uses_exactly_one_snapshot`.
- Interval queries (M04-2) are **not implemented** — deferred by decision.
- No performance claim is made and no index was added.
- The suite surfaced one **pre-existing** defect (not introduced by M04): the
  post-commit verification compared `effective_at` as strings, so a caller offset
  differing from the server session zone turned a correct commit into
  `DURABLE_FAILED`. Fixed narrowly in this branch; see `docs/M04-TEMPORAL-READ.md` §9.

## Files

- `m04_temporal_e2e.py` — the acceptance script (self-contained, no secrets and no
  host-specific paths; the password file is taken from `M04_PGPW_FILE`, and
  `M04_PG_HOST` / `M04_PG_PORT` / `M04_PG_USER` / `M04_PG_DB` override the
  documented defaults)
- `m04_e2e_report.json` — the run this receipt refers to (36 checks, verdict PASS)
