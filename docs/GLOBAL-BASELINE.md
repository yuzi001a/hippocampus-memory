# Global development baseline

> Single integration baseline for all follow-on development. Every later work stream
> (A02, Q01, A03, …) must branch from here instead of from historical feature branches.

- **Branch:** `integration/global-baseline-v1`
- **Integration HEAD:** `<filled at commit>`
- **Product-code HEAD:** `<filled at commit>`
- **Documentation HEAD:** `<filled at commit>`
- **Provenance rule:** after the product-code HEAD, only docs / evidence / CURRENT changed.
  Recorded explicitly as `product blobs unchanged after <sha>`.

## 1. What this baseline is

It unifies the line that is actually running in production with the reviewed
runtime-integrity line. Nothing was re-implemented and no experimental feature was carried in:
the merge is additive on the product surface (two new subpackages) plus argparse additions in
`distribution_cli.py`. All 14 architecture-critical product files are byte-identical to the
production line.

## 2. Included capabilities (with evidence)

| Capability | In baseline | Source | Acceptance evidence | User-visible effect | Known limits |
| --- | --- | --- | --- | --- | --- |
| P0 generated-context boundary (situation/identity validator, generation output cap, persistence/injection boundary) | yes | `hotfix/e1-generated-context-boundary` (`7808b89`), ancestor of the baseline | P0 preservation suite; production forward smoke | Generated context cannot leak across the persistence/injection boundary | Boundary applies to the generated-context path only |
| Embedding write reliability (write policies, durable failure ledger) | yes | `integration/p0-embedding-reliability` (`22606e4`) + `fix/embedding-write-reliability` (`b344b80`) | forward policy tests, ledger tests, production ledger `0 rows / 0 unresolved` | A provider failure is recorded instead of silently dropping a vector | Ledger records failures; automatic retry of non-retryable rows is explicit (`--include-nonretryable`) |
| Operator repair D1–D5 (`embedding_backfill` CLI) | yes | `fix/embedding-backfill-operator` (`9c4d191`) | CLI help/dry-run/apply runs; 1072-row production drain with per-batch verification | An operator can repair missing vectors without touching source text | Serial execution; ~53 rows/min |
| Long QA canonical representation + `qa_embedding_chunks` + child→parent recall | yes | `integration/p0-embedding-reliability` line; `schema/qa_embedding_chunks.sql` | 11 historical long QA rows repaired with per-field coverage/provenance checks; production QA lane merges child hits to the full parent | Long QA pairs stay retrievable even when the source exceeds the model window | QA child lane shares the same-day dedupe policy of the parent lane |
| Long Observation index (`observation_embedding_chunks` + planner + writer + child→parent recall + historical repair) | yes | `feature/long-observation-index-v1` (`425c42f`) | canary + 6-row historical repair; 12 sidecar rows; production canary PASS | Long observations are retrievable instead of permanently invisible | — |
| Embedding reliability recovery (whole-DB NULL drain) | yes | `feature/embedding-reliability-recovery` (`8d7a32f`) | `UNEXPLAINED_NONEMPTY_EMBEDDING_NULL = 0`; source hashes unchanged; recall smoke 8/8 | Every stored message/card/note has a vector | Historical drain is a production state, not a code requirement |
| Runtime integrity / deployment identity checks | yes (this merge) | `feature/runtime-integrity` (`a007d72`) | its own review `READY_FOR_REVIEW` with no release blockers; unit + integration tests in the baseline suite; `doctor --runtime` run against production | Operator can prove which code the live processes actually load | Reviewed, not yet shipped in a production release; `install --plan` / `uninstall --plan` are plan-only (no execution) |
| Reliability surface (`health` / `diagnose` / read-only repair planner) | yes (this merge) | `feature/reliability-recovery-v1` (`36c6e01`) | tests in the baseline suite; production `doctor` health run | One command surfaces storage / writer / derived-index health | `repair` is dry-run only; `--allow-production-read` is required for production SELECTs |
| Installer / migration / doctor for the derived-index sidecars | yes | `main` + this line | `distribution_cli` splice tests; production migration applied additively | Upgrades create the sidecar tables without touching existing tables | Additive only; rollback = code rollback, table retained |

## 3. Not included (future or unverified)

| Item | State | Why not in this baseline |
| --- | --- | --- |
| Query-path token cap (`QUERY_NEEDS_CAP` / `PROVEN_PRODUCTION_DEFECT`, P2) | designed, not implemented | Audit recorded it as P2 (0.08% of turns, graceful degradation). No code change was authorized. |
| `search_cards` inverted `_EMBED_CACHE` guard (P2) | recorded, not fixed | Same audit, recorded only; changing recall call sites needs its own RED/GREEN round. |
| Recall V2 (g6a/g6b/g6c lines) | not integrated | Separate contract/evaluator line; not part of the verified production line. |
| Q02 daily-dedupe policy change | not started | Design item; the current one-note-per-day behaviour is production behaviour and is preserved. |
| Correction semantics (M01+) | not started | Out of scope for this baseline. |
| CRL host integration / second host | not started | Out of scope. |
| Installer-execution v2 (defect fixes A1–A9) | `NOT_RELEASE_READY` | Separate line (`feature/installer-execution-v2`); the packaged installer defects are unfixed and only audited. |
| Long-observation artifacts on disk not under git | n/a | Production state and evidence, deliberately not a code dependency. |

## 4. Relationship between the historical branches

```text
main (0bd9e1e)
└── hotfix/e1-generated-context-boundary (7808b89)          [P0]
    └── integration/p0-embedding-integration (22606e4)      [embedding reliability]
        └── fix/embedding-backfill-operator (9c4d191)       [D1–D5]
            └── feature/long-observation-index-v1 (425c42f) [Long Observation]
                └── feature/embedding-reliability-recovery (8d7a32f)  [audit + NULL drain]
                    └── integration/global-baseline-v1            [this baseline]
                                  ^
feature/reliability-recovery-v1 (36c6e01)                         |
    └── feature/runtime-integrity (a007d72)  ─────────── merged ──┘
```

`feature/runtime-integrity` was **not** an ancestor of the recovery tip; it was a separate
line off `main` through `feature/reliability-recovery-v1`, and it is the one addition this
baseline makes. Provenance was proven by `merge-base` / `merge-base --is-ancestor` and by
blob comparison of the 14 architecture-critical files, not by commit messages.
