# ADR: Long Observation Derived Index v1

- **Status:** Accepted for integration-candidate implementation
- **Scope:** `observation_notes` derived vector indexing only
- **Production effect tonight:** none; production schema and rows remain untouched

## Decision

Preserve the complete observation source and add an additive, rebuildable chunk sidecar for long observations. Keep the existing one-vector short path unchanged. Generate a parent vector for a long observation only after every tokenizer-safe child has been embedded successfully.

## Context

The production observer currently sends `observation_notes.content` as one embedding request. Six existing source rows are larger than both the 7,680-token safe target and the provider's 8,192-token hard window. The source rows are valid and complete; only the derived representation is impossible under the current single-request contract.

## Alternatives rejected

### Truncate the source

Rejected. It changes the source truth, loses tail facts, makes historical repair irreversible, and turns a derived-index limit into data loss.

### Store only a prefix vector and keep the rest unindexed

Rejected. It creates a misleading parent hit and makes distinctive tail queries permanently invisible.

### Put every child in the existing parent table

Rejected. It would change the parent table's source/recall semantics and create duplicate memories. The sidecar keeps child retrieval separate and maps every hit back to one full parent.

### Rebuild a second tokenizer implementation

Rejected. The existing long-QA tokenizer/offset logic is the tested boundary implementation. It is generalized once, with the QA API preserved, to prevent drift.

### Write partial parent vectors

Rejected. A mean of an incomplete child set is not a representation of the full source and makes later repair/state detection ambiguous.

## Consequences

### Positive

- source durability is independent of provider window limits and provider availability;
- distinctive tail content is searchable through child vectors;
- full observation content is returned after a child hit;
- stale child cleanup is deterministic and scoped;
- historical repair uses the same contract as live writing;
- old runtimes can ignore the additive table.

### Costs

- long observations create multiple provider calls;
- an additive table and vector index must be migrated before production long recall is enabled;
- parent aggregation and sidecar provenance require more write/recall code;
- unresolved child failures remain visible and require repair instead of being silently hidden.

## Release boundary

This ADR authorizes implementation and disposable/provider validation only. It does **not** authorize production migration, production deployment, production restart, or production historical repair. Those remain a later `DECISION_REQUIRED` action.
