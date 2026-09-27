# Design debt — OBSERVATION_SAME_DAY_DEDUPE_CAN_SUPPRESS_RELEVANT_PARENT_HIT

```text
classification = P2 / RECALL_POLICY_DESIGN
status         = RECORDED_ONLY (no ranking/dedupe code changed)
registered     = 2026-09-23
```

## Fact

The observation recall lane caps its output with the pre-existing newest-per-day rule
(`DISTINCT ON (created_at::date)` over `observation_notes`, one surviving observation per
calendar day). The long-observation child lane can lift a parent's score (child cosine >
that parent's own aggregate cosine), but it cannot lift the parent above a *different*
same-day observation whose score is higher. When that happens the relevant parent is
dropped from the recall result entirely — even though its sidecar children and parent
vector are correct.

## Observed instances

| instance | query | target | competing same-day observation | outcome |
|---|---|---|---|---|
| 759 head-region query | head slice of source | 759 parent 0.5737 (head child 0.5698) | 751 `v750-compressed` 0.5906 (2026-09-21) | 759 suppressed |
| 743 tail-only query | distinctive tail slice | 743 tail child 0.6776 (own parent 0.6753) | 754 `v752-compressed` 0.6805 (2026-09-21) | 743 suppressed by 0.0029 |

In the 743 case the child lane demonstrably worked (it raised 743 from 0.6753 to 0.6776)
and the child candidate was present and ranked first among that day's children; the loss
came purely from the one-note-per-day cap.

## Explicit non-findings

- This is **not** a long-observation indexing regression: sidecar structure, offsets,
  coverage, source/chunk hashes, representation version, parent vector, and the failure
  ledger were all verified correct for the affected rows.
- It does **not** block the historical repair: the repair writes the derived index; the
  suppression is a read-path policy interaction.
- It is **not** caused by the newly added child lane: the same cap applied before this
  work; the child lane only changes the score used for comparison.

## Follow-up comparison (not started)

Compare on a fixed query set before changing anything:

1. one-per-day (current)
2. top-N-per-day
3. strong-relevance exemption (e.g. allow a second same-day note above a score floor)
4. relevance-first, then diversity

No parameter tuning is to be done as a side effect of other work.
