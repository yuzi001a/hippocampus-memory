# B01 findings — defects found by the B01 rounds

Found by the isolated real E2E (`eval/b01_host_contract_e2e.py`) and by building a
brand-new database from the installed wheel (`eval/fresh_bootstrap_e2e.py`).

Scope discipline: only what the current task authorises gets fixed. Everything
else is recorded here with evidence and a classification.

---

## F1 — packaged bootstrap failed on a brand-new database — **RESOLVED (v0.2.3)**

`classification = PRODUCT_DEFECT / P1-INSTALL-BLOCKER`
`code = FRESH_DB_BOOTSTRAP_DEPENDENCY_ORDER`

**Symptom** (deterministic, fresh empty PG + installed wheel):

```text
$ hippocampus bootstrap --dsn postgresql://…@127.0.0.1:55505/freshboot
{
  "result": {
    "applied": false,
    "error": "UndefinedTable('relation \"public.observation_notes\" does not exist')",
    "include_expanded": true,
    "sql_bytes": 25042
  }
}
```

**Root cause**: `alpha_bootstrap.sql` inlined `observation_embedding_chunks.sql`
before `observation_notes` was created. The sidecar carries

```sql
observation_id BIGINT NOT NULL REFERENCES public.observation_notes(id) ON DELETE CASCADE
```

and the include marker sat at the *qa* sidecar position (right after `qa_pairs`),
while `observation_notes` is created much later in the same file. Bootstrap
splices includes in place, so the FK target did not exist yet → `UndefinedTable`
→ the whole transaction rolled back → a brand-new install had *no* schema at all.

The artifact's own header ("Additive-only: no ALTER to observation_notes, safe to
apply first") is what invited the mis-ordering: it is safe to apply first only
once its FK target exists.

**Fix (ordering only)**: moved the include marker to after the `observation_notes`
block, in **both** copies of the artifact set:

| copy | role |
| --- | --- |
| `src/v3-core/schema/alpha_bootstrap.sql` | canonical source of truth (per INSTALL.md §8) |
| `src/v3-core/src/v3core/schema/alpha_bootstrap.sql` | the copy bootstrap actually applies |

```text
marker line 139 → 299   (observation_notes at 268)
diff per file: +2 / −1  (marker move + one blank line)
no schema change, no FK change, no migration change, no production change
```

**Evidence**

- RED (pre-fix, packaged path): `evidence/fresh-bootstrap-e2e.red.json`
  → `rc=1`, `undefinedtable=true`, `public_tables_before=0`
- GREEN: `evidence/fresh-bootstrap-e2e.json`
  → `rc=0`, `PACKAGED_FRESH_BOOTSTRAP=PASS`, FK
  `observation_embedding_chunks.observation_id → observation_notes.id` present,
  second bootstrap `rc=0` with tables and indexes unchanged (`schema_versions=1`)
- Fast guard (no PG): `tests/test_bootstrap_dependency_order.py` — expands the
  packaged SQL through the same helper bootstrap uses and asserts, statement by
  statement, that every FK target is created before the table referencing it.
  RED first: 2 failed before the fix, 7 passed after.
- B01 E2E now proves the normal path: `packaged_bootstrap_ok=true`,
  `fallback_used=false` (the old hand-ordered fallback is kept as a diagnostic
  path and now *fails the run* if it is ever needed again).

---

## F2 — outbox replay is at-least-once for `conversation_stream` — **KNOWN, NOT FIXED**

`classification = KNOWN_LIMITATION / B03_PRECONDITION`

A crash between the durable outbox write and the PG ack can give
`conversation_stream` a second row for one identity: the table has no unique key
on the event identity, and `_recover_live_pending()` replays un-acked items on
the next start.

Observed in the B01 E2E: `stream_u1 1 → 2`, `stream_hostB 1 → 2`,
`stream_rows_extra = 6` while `qa_rows_unchanged_after_replay = true`.

B01's contribution: the duplicate is now **detectable** (`event_status`, the
falsifiable `duplicate` ACK) and the derived layer stays idempotent (duplicates
skip QA derivation; `qa_pairs.source_id` + `ON CONFLICT DO NOTHING`).

Not fixed here: it needs a durable receipt / source-identity decision on the
source table, which is exactly what §11 defers. **Re-evaluate before pi (B03)
starts automatic event collection** — an automatic collector makes the window
far more likely to be hit than B02's explicit tool calls do.

---

## F3 — `embedding_failures.sql` is packaged but applied by nothing — **OPEN**

`classification = PRODUCT_DEFECT / P2 (fresh-install gap, not a hard blocker)`

- The artifact exists at `src/v3-core/schema/embedding_failures.sql`.
- It is **not** in `src/v3core/schema/` (the packaged set) and is **not** in
  `pyproject.toml`'s `package-data` list.
- No code path applies it: `hippocampus bootstrap` applies only
  `alpha_bootstrap.sql` (includes) + `upgrade_v0_2.sql`; grep finds no reference
  to the artifact name anywhere in `src/`.
- Consequence on a fresh install: `public.embedding_failures` does not exist
  (verified: `to_regclass('public.embedding_failures')` → NULL after a successful
  packaged bootstrap), while `v3core/embed_failures.py` INSERTs/UPDATEs it.
- Doctor does **not** require it (its `required_tables` list is the 7 alpha
  tables), so a fresh install still reports healthy — this is a reliability
  blind spot rather than an install blocker.

Not fixed here: adding an include marker is an artifact-coverage change, not the
ordering fix this task authorised. Candidate one-liner hotfix for a follow-up.

---

## F4 — the two schema copies disagree on line endings, and INSTALL.md claims otherwise — **OPEN**

`classification = DOC/REPO_HYGIENE`

`docs/INSTALL.md` §8 says the repo-root copies are canonical and "the package
copies are byte-identical to them". Measured at the B01/hotfix baseline:

| artifact | repo-root | packaged | byte-identical |
| --- | --- | --- | --- |
| `alpha_bootstrap.sql` | CRLF (332) | LF (0) | ✗ (content identical after normalisation) |
| `explicit_memories.sql` | CRLF (116) | LF (0) | ✗ |
| `qa_embedding_chunks.sql` | LF | LF | ✓ |
| `observation_embedding_chunks.sql` | LF | LF | ✓ |
| `upgrade_v0_2.sql` | LF | LF | ✓ |

Functionally harmless (SQL does not care), but the documented invariant is false,
and a byte-identity check cannot currently be used as a packaging gate. The
hotfix deliberately preserved each file's own convention instead of normalising
them (normalising would add a large no-op diff on top of a one-line ordering fix).
