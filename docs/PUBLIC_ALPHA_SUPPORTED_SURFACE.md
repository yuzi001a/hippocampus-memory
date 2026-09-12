# Hippocampus v0.1-alpha — Public Alpha Supported Surface

> **The single contract for what the public-alpha release of v3 Memory
> Plugin promises to do, in what environment, and with what evidence.**
>
> If a capability is **not** listed in § 2, it is either experimental,
> internal-only, or **UNKNOWN** — even if a related code path exists. When
> in doubt, treat anything outside § 2 as not yet supported.
>
> This document is the supported-surface contract for the public
> alpha. The follow-up requirements the alpha does **not** close are
> recorded in [`docs/RELEASE-CHECKLIST.md`](RELEASE-CHECKLIST.md)
> and [`docs/KNOWN-LIMITATIONS.md`](KNOWN-LIMITATIONS.md).
>
> **Status legend (used in § 2 below):**
>
> - **PASS** — executed on the current HEAD in the environment of § 1
>   against this release and reproduced the claimed outcome.
> - **EVIDENCE** — code path is the documented contract; specific lab
>   numbers come from prior lab bases and the path is unchanged on this
>   HEAD, but a fresh end-to-end run on this exact HEAD was not
>   produced for this row. Read the "Evidence pointer" column to decide
>   whether the cited artifact is sufficient for your trial.
> - **UNKNOWN / NOT TESTED** — no fresh run report on this HEAD; treat
>   as not part of the supported surface until re-executed.
>
> Every row has a current evidence status. Rows that have not been
> re-executed on this HEAD are explicitly marked **UNKNOWN / NOT TESTED**
> below. Test counts and pass totals from earlier gates are not
> carried over as current claims.

---

## 1. Environment in which the supported surface is verified

All claims in § 2 are conditional on this environment. A claim is **not**
made outside this environment. The environment below is the one in
which the § 2 PASS rows were produced.

| Item | Value |
|---|---|
| OS | Windows 10 |
| Python | 3.11.16 |
| Install mode | Fresh `venv` + standard non-editable `pip install` from this repo's clean-export wheel/ directory (one fresh Git commit; see the export manifests) |
| PostgreSQL | `pgvector/pgvector:pg17` Docker container, **disposable** (not a production database) |
| Profile directory | Explicit, absolute path set by top-level `basePath` in `config.yaml` (engine does **not** expand `~`, does **not** default to a particular user path) |
| Required env | `V3CORE_PG_PASSWORD` (declared in `plugin.yaml` under `requires_env`) |
| Hermes host | A working Hermes Agent; the fresh venv used the highest available PyPI host package (`hermes-agent==0.19.0`) — `pip check` and core/plugin/tools imports PASS, plugin reports `get_tool_schemas` count = 13 |
| LLM provider | **Optional** (`llm` block). Not used in fresh export or the alpha's supported smoke; observer/E1/topic synthesis was not exercised. |
| Embedding provider | **Optional** (`storage.embed` block). Earlier alpha vector evidence used a local deterministic mock; fresh export left the block commented and exercised keyword-only recall. Production-host embed endpoints are **NOT TESTED** on this HEAD. |
| Rerank provider | **Optional** (`storage.rerank` block). Not exercised in this alpha's evidence. |
| Network | Only outbound to providers the user has explicitly configured |

Reproductions outside this environment are **not part of the alpha claim**.

---

## 2. Supported surface — per-capability status

Each row is a claim about a specific capability and is assigned the
status (PASS / EVIDENCE / UNKNOWN / NOT TESTED) that the current
evidence supports. "Evidence pointer" identifies the artifact that
backs the claim. Claims that have not been re-run on this HEAD are
**not** silently upgraded.

### 2.1. Initialization

| Capability | Status | Evidence pointer |
|---|---|---|
| Clean-export wheel + `pip install` of `v3-core` and `v3-hermes-plugin` in a fresh venv succeeds; `pip check` clean; `v3core`, `v3core.observer`, `v3core.e1`, plugin module, tools module imports succeed. | **PASS** | `src/v3-core/pyproject.toml`, `src/v3-hermes-plugin/pyproject.toml`; `tools/build_public_export.py` clean export (one fresh Git commit; see the export manifests); install recipe in [`docs/INSTALL.md`](INSTALL.md). |
| Plugin manifest `deep_memory_v3` registers under Hermes with **13** tools + `requires_env: [V3CORE_PG_PASSWORD]`. | **PASS** | `src/v3-hermes-plugin/plugin.yaml`; plugin reports `get_tool_schemas` count = 13. |
| `v3-core info` CLI exits 0 and prints the engine status summary. | **PASS** (smoke) | `v3core.__main__`. Fresh venv output was `Status: {'total_cards': 0, 'by_category': {}}`; provider health remains the separate `v3_health` contract. |
| Engine config can be loaded from the absolute profile directory you choose via top-level `basePath` (no `~` expansion, no implicit default). | **PASS** | `v3core.config`, `v3core.config_model`; `examples/config.example.yaml` ships `basePath:` at top level. |
| `V3CORE_PG_PASSWORD` env var is honored as the required credential. | **PASS** | `v3core.config` env-var resolution; `tests/test_config_pg_password_env.py`. |
| DB schema applied via `src/v3-core/scripts/bootstrap_alpha_db.py` (explicit, not auto-applied by `v3core.active_memory_store`); 7 tables created on an empty tmpfs pgvector/pg17; second run is idempotent (no errors). | **PASS** | `src/v3-core/scripts/bootstrap_alpha_db.py`, `src/v3-core/schema/alpha_bootstrap.sql`. |

### 2.2. Source ingest (`sync_turn`)

| Capability | Status | Evidence pointer |
|---|---|---|
| `sync_turn` writes durable rows to `conversation_stream` from an empty DB. Fresh export stranger smoke produced `conversation_stream` count = 2; one direct call intentionally did not flush a QA row. | **PASS** | Fresh export smoke; QA pairing is separately covered by `test_sync_turn_qa.py` (9 passed). |
| QA pairs are produced and inserted with stable per-session identity. | **PASS** | `tests/test_sync_turn_qa.py` (9 passed); fresh export's single direct sync did not invoke the separate pairing flush. |
| The durable ingest path is idempotent across retries (timestamped `NOT EXISTS` + LiveBuffer outbox); exact retry of an already-recorded turn dedupes. | **PASS** | `tests/test_sync_turn_qa.py` and the current focused ingest suite; exact-retry behavior is not inferred from the one-turn fresh export smoke. |
| After process shutdown + restart, ingest cursor/orphan recovery preserves durable rows. | **EVIDENCE** | Current focused ingest tests passed; fresh export smoke verified a new provider process can recall the committed active memory, but did not trigger an observer/E1 orphan cycle. |

### 2.3. Active memory (clean-boundary core)

| Capability | Status | Evidence pointer |
|---|---|---|
| `v3_store` / `v3_add` write to `public.explicit_memories` via `ActiveMemoryWriter.create(...)`; `DURABLE_COMMITTED` observed for a fresh write. | **PASS** | Fresh export `v3_store` returned `DURABLE_COMMITTED`; focused active-memory contracts cover the shared writer boundary. |
| Exact retry of an already-committed active-memory card dedupes (status `DEDUPLICATED`). | **PASS** | Same E2E; exact retry returned `DEDUPLICATED` against the existing row. |
| Status precedence in `write_card_strict`: `DEDUPLICATED` precedes `DERIVED_WARNING`. | **EVIDENCE** | `src/v3-core/src/v3core/card_store.py`. Path unchanged on this HEAD; not re-executed end-to-end with `DERIVED_WARNING` triggered in the alpha run. |
| Active-memory read: keyword lane + vector lane + RRF fusion on the existing `recall_pool` seam (`KW_RRF_WEIGHT=0.5`, `VEC_RRF_WEIGHT=1.0`); hit kind `active_memory`. | **PASS / EVIDENCE** | Fresh export keyword readback passed after store and after process restart; vector/RRF was exercised in the earlier disposable deterministic-embed run. |
| Soft archive: `v3_delete` with `hard=False` → `status='archived'` + `updated_at=NOW()`; preserved in PG. | **PASS** | Same E2E; archive action recorded and verified post-archive. |
| Hard delete: explicitly rejected (`hard_rejected=True`); no DELETE issued. | **EVIDENCE** | `src/v3-core/src/v3core/active_memory_store.py`. Hard-delete refusal is the documented contract; the alpha run did not trigger a hard-delete attempt. |
| Missing-table guard: `table_available=False` rather than silent fallback. | **EVIDENCE** | `src/v3-core/src/v3core/active_memory_store.py`. Path unchanged on this HEAD; not re-executed in the alpha run. |
| Passive paths (observer / E1 / topic_store / dedup) have no DML against `public.explicit_memories`. | **EVIDENCE** | `tests/test_active_memory_clean_boundary_red.py`, `tests/test_active_memory_boundary_contract_red.py`. Both boundary suites passed in the current focused run; live observer/E1 synthesis remains outside the alpha surface. |
| Cross-thread safety of the SQLite store layer (RLock + `check_same_thread=False`). | **EVIDENCE** | `src/v3-core/src/v3core/sqlite_store.py`. Fix carried over; not re-exercised in the alpha E2E. |

### 2.4. Recall (read paths)

| Capability | Status | Evidence pointer |
|---|---|---|
| `prefetch` hook returns a context block with keyword + vector lanes within the 8-second budget; zero LLM calls in the hot path. | **PASS (keyword + vector lanes), UNKNOWN (full 8s budget + zero-LLM assertion in a live Hermes host)** | `v3core.prefetch`, `v3core.recall_pool`. The local deterministic embed provider produced an active hit on both lanes; the full Hermes-mediated hook contract (8s budget, zero LLM in hot path) was not exercised against a live Hermes host on this HEAD. |
| `v3_search` / `v3_get` / `v3_status` return results for keyword and (if configured) vector queries against the active-memory table. | **EVIDENCE** | `v3-core` Python API + plugin tool handlers. Keyword + vector recall through the API were exercised in the alpha E2E; the full host tool surface (13 tools returning caller-side receipts) was not enumerated. |
| `v3_health` returns the per-provider status report (pg / embed / llm / rerank). | **UNKNOWN / NOT TESTED** | `v3core.llmstatus`, plugin tool handler. `v3_health` was not run end-to-end against live providers on this HEAD. |
| `v3_extract(write=True)` writes cards through the same `ActiveMemoryWriter`; per-card `durable/status/source/error/warnings` returned. | **UNKNOWN / NOT TESTED** | `src/v3-core/src/v3core/active_memory_store.py`. The LLM-driven extract path was not exercised on this HEAD — the alpha used a local deterministic embed mock and did not run `v3_extract` end-to-end. |
| Observer rolling imprint + E1 yin synthesis + topic-card extraction. | **UNKNOWN / NOT TESTED** | `v3core.observer`, `v3core.e1`, topic paths. These paths require the top-level `llm` provider; the alpha ran without an LLM provider. Derived state is **not** part of the supported surface for this alpha. |

### 2.5. Backup / restore

| Capability | Status | Evidence pointer |
|---|---|---|
| `pg_dump -Fc` produces a self-contained backup including the `vector` extension, the `public.explicit_memories` DDL, and all its indexes. | **PASS** | fresh export host backup `alpha.pgdump` was non-empty; `pg_restore --list` contained the three canonical table entries. |
| Restoring into an empty isolated pgvector container restores table presence + row counts (`raw` = 2, `QA` = 0, `explicit` = 1) matching the fresh export source state. | **PASS** | fresh export pg_restore into empty pgvector/pg17 reproduced `2|0|1`; post-restore write increased explicit rows to 2. |
| After restore, active-memory write + recall works on the restored instance (post-restore parity). | **PASS** | fresh export restore smoke returned `DURABLE_COMMITTED` for a new marker and keyword recall found it; direct SQL verified one restored marker row. |

### 2.6. Restart recovery

| Capability | Status | Evidence pointer |
|---|---|---|
| Restarting the engine mid-session does not corrupt the canonical active-memory table. | **PASS** | fresh export new-process smoke retained keyword readback and tool schema count = 13. |
| Restarting after a partial observer/E1 cycle leaves durable ingest rows intact; durable orphan recovery advances the cursor. | **EVIDENCE** | Focused ingest tests passed; fresh export intentionally ran without an LLM/observer cycle, so no fresh orphan count is claimed. |

### 2.7. Provider end-to-end (host-networked)

| Capability | Status | Evidence pointer |
|---|---|---|
| Full end-to-end plugin-mediated contract via Hermes with a host-networked LLM + embed provider. | **UNKNOWN / NOT TESTED** | The local Hermes provider adapter initialization/sync/store smoke was re-executed after the env + empty-embed fixes and passed with LLM disabled and local deterministic embeddings. A live host-networked LLM + embed provider was not used. |

---

## 3. Experimental / alpha-candidate (do not depend on)

These exist in the code but are **not** part of the supported surface for
this alpha. They may or may not work, may change behavior without notice,
and may not be backed by current lab evidence on this HEAD.

- Manual topic surgery (`v3_topic_correct` and any other `topic_*` write tools).
- Old `b_*` / `shou_*` / MOC paths (handbook / 手帐 / 旧卡).
- Legacy dedup against the historical SQLite mirror.
- SQLite active mirror (anything that writes user-visible memory content to the per-profile SQLite file as the canonical store — that role is PG-only in the supported surface).
- Historical SQLite ↔ PG migration scripts.
- Clustering of adapters / files into topics (`topic_cluster.py`).
- `v3_import_full` / `v3_import_seed` for arbitrary sources.
- Observer rolling imprint, E1 yin synthesis, topic-card extraction (`v3_extract(write=True)` LLM path) — the alpha ran without an LLM provider.

If you need one of these for your trial, treat it as **alpha-candidate**,
not as supported.

## 4. Explicit non-claims

These are explicitly **not** part of the alpha, regardless of any code
that may exist:

- **Recall V2** (typed provenance, temporal intent `normal / latest /
  time_range / historical`) — designed but **not implemented** in
  this alpha.
- **Multi-writer** coordination (no distributed lease implementation).
- **Multi-agent** routing / namespacing (only reserved by the schema).
- **Cloud-hosted service** (no managed offering exists).
- **Stable production deployment** of this technical preview — this
  release is not a stable production release.
- **Historical data migration** into the supported surface. The
  supported surface assumes a fresh environment.
- **Long-soak evidence** (`LONG_SOAK`) — PENDING / NON-BLOCKING; do
  not infer stability from the alpha E2E alone.
- **Full pytest green across the repository** — not claimed. The focused
  acceptance scope for this release was run file-by-file to avoid known
  order contamination: **191 passed** across bootstrap/config/embed/
  active-memory, source-ingest preservation, Hermes adapter/lifecycle/
  tool-schema, and import preservation. This number is not a full-suite
  result.
- **Host-networked provider end-to-end** — the local Hermes provider
  adapter smoke passed with LLM disabled and local deterministic
  embeddings. A live host-networked LLM + embed provider remains
  **UNKNOWN / NOT TESTED**.
- **A "no known issue" statement for the whole codebase** — the
  pre-tag checklist explicitly says "no known issue" is not a
  substitute for a read-back receipt.

## 5. What the public-alpha does not close

These are follow-up requirements the alpha does **not** close by
itself. Read [`docs/RELEASE-CHECKLIST.md`](RELEASE-CHECKLIST.md) for
the full follow-up list. Statuses here reflect the evidence in § 2 on
this HEAD, not historical carryover from earlier candidates.

| Area | Status for this alpha | Backing |
|---|---|---|
| Data safety (declared supported surface only) | **CLOSED on declared surface** — clean-export E2E from empty DB produced `DURABLE_COMMITTED`, exact retry returned `DEDUPLICATED`, restart-survivable cursor advance. Limited to the surface in § 2.2, § 2.3, § 2.6. | § 2.2, § 2.3, § 2.6; clean-export E2E run report. |
| Recovery (dump/restore into an empty isolated PG) | **CLOSED on declared surface** — fresh export pg_dump/pg_restore reproduced `raw` = 2, `QA` = 0, `explicit` = 1, then post-restore write + keyword recall succeeded. | § 2.5; fresh export pg_dump/pg_restore run. |
| Installability (fresh-install matrix) | **CLOSED on declared surface** — clean-export wheel (one fresh Git commit; see the export manifests) installed in fresh venv; `pip check` clean; core/plugin/tools imports PASS; plugin `get_tool_schemas` count = 13; alpha bootstrap on empty tmpfs pgvector/pg17 (7 tables, idempotent second run) PASS. | § 2.1; clean-export wheel install + bootstrap run. |
| Privacy / security | **PASS for this publication** — the clean-history public tree contains no real credentials, private endpoints, or local runtime state; provider credentials are supplied locally and are not bundled. | Clean-export builder + publication security/privacy scan; see `docs/RELEASE-CHECKLIST.md` § C. |
| Basic production usability | **PASS on declared alpha smoke slice** — fresh export stranger install covered info, sync_turn, v3_store, keyword readback, new-process restart, and restore write/recall. LLM/observer live paths, long-soak, and full suite remain outside the claim; focused acceptance was **191 passed**. | § 2.1–§ 2.6; fresh export install smoke. |

---

## 6. How to verify a supported-surface claim

For each row in § 2 you should be able to:

1. Read the file under "Evidence".
2. Run the corresponding recipe in [`docs/INSTALL.md`](INSTALL.md) /
   [`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md).
3. Confirm the same outcome the alpha run recorded.

If step 3 fails, the claim is **falsified** for your environment — please
open an issue with the failure mode rather than assuming the run is wrong.

---

## 7. Cross-references

- [`docs/INSTALL.md`](INSTALL.md) — how to set up the supported environment.
- [`docs/CONFIGURATION.md`](CONFIGURATION.md) — every supported config key
  + env var (top-level `basePath` is required).
- [`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md) — the supported
  backup/restore recipe.
- [`docs/PRIVACY-DATA-FLOW.md`](PRIVACY-DATA-FLOW.md) — what data goes
  where under the supported surface.
- [`docs/KNOWN-LIMITATIONS.md`](KNOWN-LIMITATIONS.md) — open issues you
  are likely to hit during a trial.
