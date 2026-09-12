# Hippocampus

> **Hippocampus v0.1-alpha** — Technical Preview / Public Alpha.
> Hippocampus is the public name of the V3 memory runtime. The public
> repository is [`yuzi001a/hippocampus-memory`](https://github.com/yuzi001a/hippocampus-memory).

> **Status:** Technical Preview / Public Alpha. This release has
> current disposable-environment evidence for the supported install, database
> bootstrap, source ingest, active-memory write/recall/archive, restart, and
> backup/restore paths. Unsupported or untested paths remain explicitly marked
> **EXPERIMENTAL**, **INTERNAL**, **LEGACY**, or **UNKNOWN** below and in the
> supported-surface contract.

---

## What is this

Two coupled Python packages that turn a chat-driven AI agent into a
system with durable, queryable memory:

| Package | Role | Repo path |
|---|---|---|
| **v3-core** | Memory engine: PostgreSQL + pgvector storage adapter, embedding / keyword / rerank recall, observer note chain, E1 identity/yin synthesis, topic cards, journal/QA ingest, active-memory canonical writer. | `src/v3-core/` |
| **v3-hermes-plugin** | Adapter that registers `deep_memory_v3` as a memory provider for the Hermes Agent host. Wires 6 hooks (`sync_turn`, `prefetch`, `on_session_switch`, `on_pre_compress`, `on_delegation`, `system_prompt_block`) and exposes 13 tools. Requires a working Hermes host to actually run end-to-end. | `src/v3-hermes-plugin/` |

## Design principles

- **Source first:** raw conversation data and explicit active memories are
  durable sources; derived indexes and summaries are disposable and
  rebuildable.
- **Canonical active memory:** explicit memories have one durable
  PostgreSQL store instead of relying on a legacy mirror.
- **Fail-closed privacy:** an unconfigured provider receives no data;
  network providers are opt-in through local configuration.
- **Durability before derivation:** a source or explicit-memory write is
  reported separately from optional derived work.
- **Small supported surface:** this alpha documents only paths backed by
  current disposable-environment evidence.

## Who is this for

- **Trial users** who want to evaluate a long-term memory layer for a
  chat-based agent on their own machine, with their own disposable
  PostgreSQL/pgvector, their own API keys.
- **Developers** who want to inspect the supported surface, run the
  supplied focused test suites against a disposable pgvector instance, or
  audit the architecture before deciding whether to depend on it.

This is **not** for:

- Production deployments. The supported surface is a technical preview, not a stable release.
- Anyone who needs the historical SQLite-mirror / shou / MOC / Recall V2
  / manual topic-surgery flows — those are out of scope for the alpha
  contract.

## What works today (evidence-backed alpha surface)

The supported-surface contract — what passes on the current HEAD and
what is **UNKNOWN / NOT TESTED** — lives in
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md).
The remaining alpha limitations are listed in
[`docs/RELEASE-CHECKLIST.md`](docs/RELEASE-CHECKLIST.md).

The following rows are the current evidence on a fresh disposable
Windows 10 / Python 3.11 / `pgvector/pgvector:pg17` environment
(`clean-export E2E` in the table below refers to the clean-history
export's E2E run on an empty tmpfs pg17). Status matches
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md) § 2.

| Capability | Status (this HEAD) |
|---|---|
| Fresh `pip install` of `v3-core` + `v3-hermes-plugin`; `pip check` clean; core/plugin/tools imports PASS; plugin `get_tool_schemas` = 13. | **PASS** |
| `V3CORE_PG_PASSWORD` env honored as the required credential. | **PASS** |
| Explicit `bootstrap_alpha_db.py` → 7 tables on empty tmpfs pg17, idempotent on re-run. | **PASS** |
| `sync_turn` → `conversation_stream` durable rows from empty DB (`raw` = 2 in the fresh export stranger smoke). A single direct sync does not flush a QA row; QA pairing is covered by `test_sync_turn_qa.py` (9 passed). | **PASS** |
| Exact retry of an already-recorded turn → `DEDUPLICATED`. | **PASS** |
| New provider process after restart → active-memory keyword readback still finds the marker; orphan/cursor recovery is covered by the focused ingest tests. | **PASS / EVIDENCE** |
| `v3_store` / `v3_add` → `ActiveMemoryWriter` → `public.explicit_memories` (`DURABLE_COMMITTED` observed). | **PASS** |
| Active-memory keyword readback: **PASS** in fresh export smoke. Vector/RRF lane: **PASS (earlier disposable local deterministic embed run)**; fresh export intentionally left optional provider blocks commented. | **PASS / EVIDENCE** |
| Soft archive (`status='archived'`); hard delete explicitly rejected. | **PASS / EVIDENCE (hard-delete refusal not triggered in the run)** |
| `pg_dump -Fc` self-contained; restore into empty isolated pg17 reproduced `raw` = 2, `QA` = 0, `explicit` = 1; post-restore write + keyword recall proof. | **PASS** |
| `v3_health` per-provider status report end-to-end. | **UNKNOWN / NOT TESTED** |
| `v3_extract(write=True)` end-to-end (LLM-driven extract). | **UNKNOWN / NOT TESTED** |
| Observer rolling imprint + E1 yin synthesis + topic-card extraction. | **UNKNOWN / NOT TESTED** |
| Full end-to-end plugin-mediated contract via Hermes with a host-networked LLM + embed provider. | **UNKNOWN / NOT TESTED** |

**Experimental / alpha / unsupported (do not depend on):**

- Manual topic surgery (`v3_topic_correct`), old `b_*` / `shou_*` / MOC
  paths, legacy dedup, SQLite active mirror, historical SQLite↔PG
  migration.
- Recall V2 typed provenance / temporal intent — post-alpha, not present
  in this alpha.
- Multi-writer, multi-agent routing — not implemented.
- Long-soak evidence and any "no known issue" statement — see
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md) § 4
  for the explicit non-claims.

## Alpha and experimental boundary

- This is a **public-alpha release**, not a stable release. The
  supported-surface evidence in
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
  is the source of truth for what is currently claimed to work.
- Any capability marked **UNKNOWN / NOT TESTED** in § 2 of the
  supported-surface doc is **not** part of this alpha's evidence and
  must not be relied on; that includes the host-networked
  provider end-to-end path, `v3_health` per-provider reporting,
  `v3_extract(write=True)` LLM-driven extraction, and the observer /
  E1 / topic-card derivation paths.
- Remaining alpha limitations are listed in
  [`docs/RELEASE-CHECKLIST.md`](docs/RELEASE-CHECKLIST.md); the
  items the public-alpha explicitly does **not** close are in
  [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md).

## Install at a glance

The intended install path is: **Windows + fresh `venv` + local source
install + disposable PostgreSQL/pgvector container**. Full step-by-step:
[`docs/INSTALL.md`](docs/INSTALL.md). The component parameters and the
bootstrap CLI form below match `docs/INSTALL.md` § 4 and § 6 verbatim;
do not invent a second quickstart variant.

```powershell
# 1. Fresh venv
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip

# 2. Disposable pgvector (Docker; pick a non-production port).
#    Container is created with POSTGRES_PASSWORD and POSTGRES_DB set
#    so the engine bootstrap can target v3embeddings_alpha on first
#    use without needing an extra `createdb` step.
$pgPort = 55432
$pgPassword = "<local-only-password>"   # ← replace, never reuse a real one

docker run --name v3-pgvector-alpha --rm -d `
  -e POSTGRES_PASSWORD=$pgPassword `
  -e POSTGRES_DB=v3embeddings_alpha `
  -p "${pgPort}:5432" `
  pgvector/pgvector:pg17

# 3. Install both packages from this repo (local source install)
pip install .\src\v3-core
pip install .\src\v3-hermes-plugin

# 4. Copy examples/config.example.yaml and examples/.env.example to
#    your profile directory (the engine does NOT auto-create one).
#    The example file has a placeholder top-level `basePath`; replace
#    it with the absolute path you intend to use, e.g.:
#      basePath: "C:\Users\<you>\.v3-core\profiles\default"
#    The Windows engine does NOT expand `~` inside path-style keys.

# 5. Set the required env var BEFORE the first write/recall. The plugin
#    manifest (`plugin.yaml`) declares `requires_env: [V3CORE_PG_PASSWORD]`
#    and the engine also reads it for direct v3-core use. The bootstrap
#    CLI additionally reads PGPASSWORD for the same value:
$env:V3CORE_PG_PASSWORD = $pgPassword
$env:PGPASSWORD = $pgPassword

# 6. Apply the alpha DB bootstrap as an explicit step against the
#    disposable PG (the schema artifact is NOT auto-applied by
#    `v3core.active_memory_store`). The component parameters here
#    match docs/INSTALL.md § 6 --host / --port / --database / --user.
python .\src\v3-core\scripts\bootstrap_alpha_db.py `
  --host 127.0.0.1 --port $pgPort --database v3embeddings_alpha `
  --user postgres
# (password via PGPASSWORD / V3CORE_PG_PASSWORD env var; never on CLI)
```

> ⚠️ **DB bootstrap is an explicit command, not an automatic first-run
> step.** `v3core.active_memory_store` does **not** apply the schema
> artifact. The supported-surface bring-up requires you to run
> `src/v3-core/scripts/bootstrap_alpha_db.py` against your disposable PG
> before the first write.
>
> ⚠️ **Hermes is a host dependency.** `v3-hermes-plugin` is an adapter,
> not a memory engine on its own; the end-to-end hook contract requires
> a working Hermes Agent host with the plugin loaded under
> `deep_memory_v3`. Without Hermes you can still exercise `v3-core`
> directly (`v3-core info`, the Python API, focused tests) but the
> plugin-mediated contract is not exercised.

## External services

The plugin can run with the durable pipeline (write / read / recall,
soft-archive) on local-only PostgreSQL/pgvector. The default recall path
requires an embedding endpoint, and the observer / E1 paths require an
LLM endpoint. None are baked in; all are user-supplied:

| Purpose | What it does | What you must provide |
|---|---|---|
| Embedding | Vectorize text for the active-memory vector lane and topic cards. | Any OpenAI-compatible `/v1/embeddings` endpoint. The schema artifact uses `VECTOR(1024)`. |
| LLM (optional) | Observer note generation, session summaries, topic-card extraction, E1 yin synthesis. | Any OpenAI-compatible chat-completions endpoint. If unconfigured, the engine degrades to storage + keyword recall only — writes still land, but observer / E1 paths skip. |
| Rerank (optional) | Re-score top-N recall hits before injection. | Any OpenAI-compatible rerank endpoint. Falls back to embedding-only recall if absent. |

If you do not configure a provider, the engine does not transmit your
data anywhere — see
[`docs/PRIVACY-DATA-FLOW.md`](docs/PRIVACY-DATA-FLOW.md).

## Feedback

- **Bug reports:** [GitHub Issues](https://github.com/yuzi001a/hippocampus-memory/issues).
- **Feature requests:** [GitHub Issues](https://github.com/yuzi001a/hippocampus-memory/issues), marked
  `feature-request`.
- **Security-sensitive reports:** do not post secrets, credentials,
  DSNs, or private data in a public issue; see [`SECURITY.md`](SECURITY.md)
  for the current guidance.

## License

Both packages ship with `AGPL-3.0-or-later` in their subpackage
`LICENSE` files and declare the same in their `pyproject.toml`. This
monorepo does not relicense either package. See
[`src/v3-core/LICENSE`](src/v3-core/LICENSE) and the matching file under
[`src/v3-hermes-plugin/LICENSE`](src/v3-hermes-plugin/LICENSE).

## Documentation map

| Doc | Purpose |
|---|---|
| [`docs/INSTALL.md`](docs/INSTALL.md) | Step-by-step Windows install + disposable pgvector + local source install. |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | `config.yaml` keys, env vars, provider defaults, fail-closed behavior. |
| [`docs/BACKUP-RESTORE.md`](docs/BACKUP-RESTORE.md) | `pg_dump` + restore into an empty isolated pg17 (PASS on the alpha run; see § 2.5 of the supported-surface doc). |
| [`docs/PRIVACY-DATA-FLOW.md`](docs/PRIVACY-DATA-FLOW.md) | What data is local vs sent to which configured provider; what is **not** transmitted. |
| [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md) | The contract: per-row PASS / EVIDENCE / UNKNOWN / NOT TESTED, each with its evidence pointer. |
| [`docs/RELEASE-CHECKLIST.md`](docs/RELEASE-CHECKLIST.md) | The current alpha publication gates and post-alpha follow-up limitations. |
| [`docs/ARCHITECTURE-OVERVIEW.md`](docs/ARCHITECTURE-OVERVIEW.md) | Module map, write/read boundaries, where the canonical `public.explicit_memories` table lives. |
| [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md) | Open issues, deferred work, and the items the public-alpha does not close. |
| [`src/v3-core/README.md`](src/v3-core/README.md) | v3-core package README. |
| [`src/v3-hermes-plugin/README.md`](src/v3-hermes-plugin/README.md) | v3-hermes-plugin package README. |

## What this README deliberately does not claim

- It does not claim "production-ready", "stable", "GA", or "drops-in".
- It does not name a production PG host, a production API endpoint, or
  any credentials. None of those belong in this document.
- It does not upgrade capabilities that the supported-surface doc marks
  **UNKNOWN / NOT TESTED** (host-networked provider end-to-end,
  `v3_health`, `v3_extract(write=True)` LLM path, observer / E1 /
  topic-card derivation). Those rows are listed as UNKNOWN in § 2 of
  the supported-surface doc and § 4 of the same doc states the
  explicit non-claims.
- It does not promise that `pytest tests/` is fully green on this exact
  HEAD. The focused acceptance scope used for this release was run
  file-by-file to avoid order contamination: **191 passed** across
  bootstrap/config/embed/active-memory, source-ingest preservation,
  Hermes adapter/lifecycle/tool-schema, and import preservation. This is
  not a full-suite claim; see
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
  § 4 for the exact boundary.
- It does not promise a private security contact channel. See
  [`SECURITY.md`](SECURITY.md) for the current reporting guidance.
