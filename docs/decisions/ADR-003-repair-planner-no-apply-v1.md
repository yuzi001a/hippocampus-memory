# ADR-003: Repair planner is dry-run only (v1)

**Status:** accepted (feature/reliability-recovery-v1)

## Context

It is tempting to let a "repair" command fix the problems it finds. But the problems in
this domain include data-loss-adjacent operations (re-embedding, marker deletion, answer
reconstruction) whose blast radius is the user's remembered past. A repair engine is only
trustworthy once its proposals have been reviewed against real incidents.

## Decision

1. v1 ships exactly one repair surface: `hippocampus repair --dry-run` — deterministic
   planning from diagnosis evidence, **zero writes** (locked by tests: recording
   connection + planner never opens its own connection).
2. `repair --apply` exists as a subcommand but is hard-disabled: exit 2 with
   `REPAIR_APPLY_NOT_IMPLEMENTED_IN_V1`. There is no hidden path (grep-locked).
3. Every planned action declares its risk envelope up front: `risk`, `reversible`,
   `requires_provider`, `estimated_remote_calls`, `writes_database`, `automatic_safe`,
   `target_count`, `reason`. Plan first, decide later.
4. Source-preservation is a planner rule: when exact source is unavailable, the plan
   must say `NO_AUTOMATIC_REPAIR_SOURCE_MISSING` — never guess, never summarize, never
   reconstruct from derived memory.

## Consequences

- Operators get "what would happen" before anything can happen.
- A future `--apply` needs its own contract (plan hash binding, confirmation) before it
  ships — copying the audit discipline of the v0.2.1 production upgrade path.
