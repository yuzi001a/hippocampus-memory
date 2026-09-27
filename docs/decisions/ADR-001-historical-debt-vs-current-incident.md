# ADR-001: Historical debt vs current incident

**Status:** accepted (feature/reliability-recovery-v1)

## Context

Production carries ~105 legacy NULL embeddings, ~453 historical empty answers and ~112
poisoned failure markers from earlier runtimes. A naive health rule (`NULL > 0 => fail`)
would keep the system permanently `unhealthy`, which destroys the signal we actually
need: "is something *happening* now?"

## Decision

1. A failure counts as **current** only when it happened at/after the effective boundary
   `max(now - window_hours, last_successful_embedding_at)`. With no known success the
   rolling window is the fallback.
2. Totals (debt) are always reported — as check evidence, section metrics, and
   `info`-level diagnoses (`HISTORICAL_EMBEDDING_DEBT`, `EMPTY_ANSWER_HISTORICAL_DEBT`).
3. Debt never lowers the overall verdict by itself. The same timestamp rule applies to
   poisoned markers (FA03): older than last success → ok (reported), newer → fail.
4. `MW01` stays fail when `recent_embedding_null > 0` — a new NULL *after* the last
   success is a current incident, full stop.

## Consequences

- `health` can be `healthy` while carrying debt; `diagnose` still shows the debt.
- A new failure surfaces immediately because its timestamp is later than the last
  success — the boundary moves forward with every successful write.
- Debt semantics live in one reducer (`failure/check` code) and one design section
  (DESIGN §6); tests lock all transfer cases (see test_reliability_health.py).
