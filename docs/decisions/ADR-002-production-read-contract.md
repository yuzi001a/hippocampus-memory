# ADR-002: Production read contract for reliability commands

**Status:** accepted (feature/reliability-recovery-v1)

## Context

The v0.2 upgrade CLI shipped with an unconditional production refusal and no supported
write path — a defect the operator hit exactly when they needed the tool. The
reliability commands (`health`, `diagnose`, `repair --dry-run`) exist to observe
production; if they inherit an unconditional refusal they are useless.

## Decision

1. `--allow-production-read` is the explicit opt-in for touching a production-boundary
   DSN (port 5433, or loopback host + database `v3embeddings`). It is a **read** flag:
   every query issued under it must be `SELECT`-only (locked by a recording-connection
   test that asserts every executed statement starts with `SELECT`).
2. Without the flag, DB-backed checks (`ST*`, `MW*`, `DM*`) degrade to `skip` with a
   summary telling the operator which flag to pass. Local checks (runtime, marker
   ledger, config) still run — the command is never a dead end.
3. Non-production targets need no flag (same semantics as bootstrap/upgrade).
4. No DDL/DML path exists in any reliability command. `repair --apply` is hard-disabled
   (ADR-003).

## Consequences

- Observation of production is always possible, never implicit.
- The "flag given but still refused" failure mode cannot recur: the flag IS the
  authorization for reads, and it works.
