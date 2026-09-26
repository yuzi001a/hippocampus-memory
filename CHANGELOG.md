# Changelog — Hippocampus

> **Pre-release history note:** the historical tool-surface framing and
> the SQLite-mirror-based card-store path are **not** the supported
> alpha contract. They are referenced in this changelog only as
> historical background, not as current behavior.

---

## [Unreleased] — integration baseline (not a release)

> **Scope of this entry:** the current tree is the unified development baseline
> (`integration/global-baseline-v1`, product-code HEAD `86dd126`). It is **not
> deployed and not released**: the last published entry remains `0.1-alpha`
> below. The packaged version string stays `4.0.0`.

### Added

- Runtime integrity / deployment identity surface: `hippocampus doctor --runtime
  --wheel <whl>` (content fingerprints of the live process environment),
  `install --plan`, `uninstall --plan`, plus the read-only `reliability`
  diagnose/repair command group. See `docs/RUNTIME-INTEGRITY.md` and
  `docs/reliability/`.

### Changed

- `README.md` install walkthrough now documents the runtime-integrity step that
  the packaged code implements.
- `README.md` bootstrap row and `docs/INSTALL.md` `doctor --static` contract now
  state the actual packaged SQL set: five artifacts, three bootstrap include
  markers, and a nine-table bootstrap schema (six core tables plus
  `explicit_memories` and the two derived-index sidecars).

### Documentation

- `docs/GLOBAL-BASELINE.md` — the single capability matrix for this baseline
  (what is included, what is deliberately not, with evidence and known limits).

## [0.1-alpha] — First Public Technical Preview

> **Scope of this entry:** Hippocampus v0.1-alpha is the first public
> technical preview. It carries the additive schema artifacts, the
> explicit bootstrap CLI, and the documented supported surface below.
> The release is intentionally not a stable production release; see
> [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
> and [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md) for the
> evidence boundary and non-claims.

### Added

- New user-facing docs at the repo root and under `docs/`:
  `README.md` (front door rewritten for the alpha), `docs/INSTALL.md`,
  `docs/CONFIGURATION.md`, `docs/BACKUP-RESTORE.md`,
  `docs/PRIVACY-DATA-FLOW.md`,
  `docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`,
  `docs/ARCHITECTURE-OVERVIEW.md`, `docs/KNOWN-LIMITATIONS.md`,
  `docs/RELEASE-CHECKLIST.md`, plus root `CONTRIBUTING.md`,
  `SECURITY.md`, and `CHANGELOG.md`.
- Starter config templates under `examples/`:
  `examples/config.example.yaml` and `examples/.env.example`. **All
  values are placeholders.**
- Explicit "first-screen" framing of what the alpha is, who it's for,
  what's supported, and what's experimental — replacing the previous
  product framing in `src/v3-core/README.md` and the wizard-centric
  framing in `src/v3-hermes-plugin/README.md`.
- Explicit documentation that **DB bootstrap is a command**, not an
  automatic first-run step: trial users run
  `src/v3-core/scripts/bootstrap_alpha_db.py` against their disposable
  PG before the first write. See
  [`docs/INSTALL.md`](docs/INSTALL.md) § 6.
- Explicit documentation that **the engine config `basePath` /
  data root is required**, not derived: trial users must set the
  **top-level `basePath`** key in `config.yaml` to the absolute
  profile directory; the engine does NOT auto-create the directory
  and does NOT expand `~` on Windows. The legacy/internal
  `Path.home() / ".v3-core" / ...` last-resort branch in the
  resolver is documented as an implementation detail, not a
  recommended contract.
- Explicit documentation that **Hermes is a host dependency** for
  `v3-hermes-plugin`: the plugin is a thin adapter and the
  end-to-end hook contract requires a working Hermes Agent host.

### Additive schema, scripts, and tooling (not in the original base)

The release adds the following **additive** artifacts, separate from
the engine source itself. None of them are auto-invoked by
`v3core.active_memory_store`; each is operator-callable.

- `src/v3-core/schema/alpha_bootstrap.sql` — additive wrapper
  DDL that bundles the canonical
  `src/v3-core/schema/explicit_memories.sql` artifact with the alpha
  working set (`qa_pairs`, `conversation_stream`, `topics`,
  `topic_entries`, `observation_notes`, `yin_paragraphs`) plus the
  `vector` extension. Idempotent (`IF NOT EXISTS` /
  `ADD COLUMN IF NOT EXISTS`).
- `src/v3-core/scripts/bootstrap_alpha_db.py` — single-entry CLI
  that applies the bootstrap SQL above. Refuses empty / production
  boundary DSNs by default; never hardcodes a password; emits
  machine-readable JSON status.
- `src/v3-core/scripts/backup_alpha.py` — alpha PostgreSQL
  backup helper. Stock `pg_dump` / `pg_restore` / `gzip` only;
  no auto-restore; refusal-list DSN policy mirrors
  `bootstrap_alpha_db.py`.
- `tools/build_public_export.py` — reproducible clean-history
  public-export builder. Reads a curated allowlist of relative
  paths from the source tree, copies them into a fresh output
  directory, writes two machine-verifiable manifests (tree +
  sha256), then initializes a brand-new Git repository with a
  single initial commit. Allowlist is mandatory; nothing outside
  the allowlist is exported.
- `tools/scan_release_tree.py` — local secret/privacy scanner for
  a release tree. Walks a directory, applies a curated set of
  pattern groups, and emits a machine-readable JSON report
  (including a `real_secret_count`). Values are redacted; no
  network calls.
- `tools/release.gitignore` — gitignore template for the
  clean-history export output directory.

### Release changes

- Reworked the first-screen README and the public-alpha documents so the
  supported surface, configuration, privacy, backup/restore, and known limits
  match the current tree.
- The candidate adds an additive, version-controlled PostgreSQL/pgvector bootstrap:
  `src/v3-core/schema/alpha_bootstrap.sql` plus the explicit
  `src/v3-core/scripts/bootstrap_alpha_db.py` entry point. It is not applied
  implicitly by runtime startup and does not recreate legacy tables.
- Added the standard `pg_dump` backup helper and focused bootstrap contract
  tests. The clean-history export builder and local secret scanner are private
  release-preparation tooling and are not part of the public export.
- The adapter exposes the existing 13 host tools; no new plugin tool was
  introduced.
- No production deployment, historical data migration, or production
  schema change is part of this release. Public publication is represented
  by the clean-history repository and the `v0.1-alpha` prerelease.

### Notes on env-var mapping

- The `V3CORE_PG_PASSWORD` env var is read by the engine runtime via
  `v3core.config._apply_pg_password_env` and is declared under
  `requires_env` in `src/v3-hermes-plugin/plugin.yaml`. This mapping
  is an engine-runtime feature predating this release; the change in
  this release is the docs + manifest declaration, not the engine
  resolution itself. Do not characterize this release as having
  *added* the env-var resolution.

### Evidence boundary

- Claims in the public documents are limited to the disposable Windows
  / Python 3.11 / pgvector evidence recorded for this release.
- The focused acceptance scope for this release is **191 passed**,
  run file-by-file to avoid known order contamination. Unsupported
  observer/LLM synthesis, host-networked providers, and historical
  evaluation paths remain labeled experimental, internal, legacy, or
  unknown.

### Not in this release

- No Recall V2, QueryPlan, temporal recall, multi-agent, multi-writer, or
  historical active-memory migration.
- No deletion of legacy subsystems or historical data is part of this
  release; the public tree is a separate allowlisted clean-history export.
- No package-index publication or production Hermes deployment. The GitHub
  prerelease is the public release channel for this technical preview.

### Known caveats

- The six source-ingest preservation files were re-validated
  independently on this HEAD to avoid known order contamination:
  `test_sync_turn_qa.py` (9), `test_sync_turn_timestamp_parse.py` (4),
  `test_live_buffer_pool_owner.py` (13),
  `test_compression_ingest_durability.py` (19),
  `test_pg_store_timestamp_normalization.py` (15), and
  `test_llm_observability.py` (12). The focused acceptance total also
  includes `test_import_hermes_state.py` (3).
- The fresh export stranger smoke produced `conversation_stream=2` and
  `qa_pairs=0` after one direct `sync_turn`; it intentionally did not
  trigger a separate QA flush or observer/E1 cycle. QA pairing and
  ingest preservation are covered by the current focused files listed
  above.
- Items still outside the public-alpha supported surface are recorded in
  [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md) § "What
  the public-alpha does not close" and in
  [`docs/RELEASE-CHECKLIST.md`](docs/RELEASE-CHECKLIST.md)
  § P–S (POST-ALPHA / STABLE).

---

## Historical context (preserved, not the current contract)

The following pre-alpha items are **not** part of the supported
public-alpha surface and are listed only for readers who have
historical context documents:

- A pre-alpha product matrix with tiered offline / BYOK / local
  stack labels. That product framing has been retired; the
  supported public-alpha surface is described in
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md).
- A 31-tool plugin surface. Narrowed to 13 in 2026-08-07; the
  supported alpha surface is the 13-tool surface.
- A `hermes memory setup` wizard with auto-config scaffolding. The
  supported alpha flow is the manual `config.yaml` + local-source
  `pip install` flow described in
  [`docs/INSTALL.md`](docs/INSTALL.md).
- A `b_*` / `shou_*` / SQLite-mirror active-memory path. The
  supported alpha surface writes only to `public.explicit_memories`
  via `v3core.active_memory_store.ActiveMemoryWriter`.
- Production infrastructure and credentials are outside this release; the
  supported surface assumes a fresh disposable container with locally
  supplied placeholder credentials.
