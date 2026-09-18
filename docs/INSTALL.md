# Install — v3 Memory Plugin (Public Alpha Hardening)

> **Audience:** first-time trial users on a Windows machine who want to
> evaluate the supported public-alpha surface against a disposable,
> isolated PostgreSQL/pgvector.
>
> **Scope:** build non-editable wheel + sdist artifacts for both
> `v3-core` and `v3-hermes-plugin` from this repo's source tree,
> install the built wheels into a fresh venv, point v3 at a disposable
> pgvector, run the explicit DB bootstrap via the packaged
> `hippocampus` console, and confirm the install succeeds.
>
> **Out of scope:** connecting to any pre-existing production
> PostgreSQL, importing historical data, replacing a live deployment,
> or running any historical migration script. The supported alpha
> surface assumes a fresh environment.
>
> **Verification status:** this install recipe is the documented
> bring-up path. The clean-history export E2E run on a fresh
> disposable Windows 10 / Python 3.11.16 / `pgvector/pgvector:pg17`
> environment is recorded in
> [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
> § 2.1 (PASS rows for clean-install + `pip check` + bootstrap
> idempotency). Pre-tag blockers the alpha does **not** close by
> itself are listed in
> [`docs/RELEASE-CHECKLIST.md`](RELEASE-CHECKLIST.md)
> § D (Install / upgrade / rollback readiness).

---

## 1. What you need

| Item | Why | Notes |
|---|---|---|
| **Windows 10/11** | Primary support target for this alpha. | The package itself is cross-platform Python. |
| **Python 3.10 or 3.11** | `pyproject.toml` requires `>=3.10`. | 3.12 also works for the engine. |
| **Docker Desktop** | Run a disposable `pgvector/pgvector:pg17` container. | Native Windows PG installs work too at the SQL level; the documented recipe uses a Docker container. |
| **Git** | Clone this repo. | — |
| **Network access to a model provider** | Embedding endpoint for recall; LLM endpoint for observer / E1 (optional but recommended). | All endpoints are user-supplied and configured in `config.yaml`; see [`docs/CONFIGURATION.md`](CONFIGURATION.md). |
| **~5 GB free disk** | Repo + venv + container image + disposable PG data. | — |

> ⚠️ **Hard rule:** do **not** point this install at any production
> PostgreSQL. The supported surface is a fresh disposable environment
> only. Production credentials, production endpoints, and production
> ports are out of scope for this document.

---

## 2. Clone

```powershell
git clone https://github.com/yuzi001a/hippocampus-memory.git
cd hippocampus-memory
```

This public repository is the canonical development and release source
for `v3-core` and `v3-hermes-plugin`. Do not assume any specific tag,
branch, or commit SHA from this document — every supported-surface
row in
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
§ 2 records the evidence under whatever `HEAD` the verifier checked
out, not a fixed internal identifier.

Verify the working tree is the public-alpha candidate:

```powershell
git log --oneline -1
# Records the HEAD the verifier used for the § 2 PASS rows.
```

---

## 3. Fresh venv (Windows)

Use a **fresh** virtualenv per evaluation. Mixing venvs across different
versions of v3-core or across different profile names is the most
common source of "it worked yesterday" bugs. The shipped workflow
uses [`uv`](https://docs.astral.sh/uv/) to create and manage the
venv; if `uv` is not yet on the host, install it once with the
official installer (`pip install uv`, `winget install astral-sh.uv`,
or `irm https://astral.sh/uv/install.ps1 | iex`) before continuing.

```powershell
uv venv --python 3.11 .venv
.\.venv\Scripts\Activate.ps1
```

Confirm you're inside the venv:

```powershell
where python
# Should resolve to ...\.venv\Scripts\python.exe
```

---

## 4. Build non-editable wheel + sdist artifacts

The documented distribution path is to **build artifacts first**, then
install the actual built wheels into the fresh venv — not `pip install`
straight from the source tree. From the repo root:

```powershell
uv build --wheel --sdist --out-dir .\dist\v3-core .\src\v3-core
uv build --wheel --sdist --out-dir .\dist\v3-hermes-plugin .\src\v3-hermes-plugin
```

This produces (paths relative to repo root):

- `dist\v3-core\v3_core-4.0.0-py3-none-any.whl`
- `dist\v3-core\v3_core-4.0.0.tar.gz`
- `dist\v3-hermes-plugin\v3_hermes_plugin-4.0.0-py3-none-any.whl`
- `dist\v3-hermes-plugin\v3_hermes_plugin-4.0.0.tar.gz`

These exact filenames are what step 6 installs. The sprint does **not**
push these to PyPI; there is no PyPI project to claim, and this
document does not direct you to install from a registry.

---

## 5. Disposable pgvector container

Use Docker. The image is the official `pgvector/pgvector:pg17`
(matches the clean-history export E2E run; see
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
§ 1 "Environment in which the supported surface is verified"). Pick a
**local port that is not any production port** — values like `55432`
are placeholders for the disposable range:

```powershell
$pgPort = 55432
$pgPassword = "<local-only-password>"   # ← replace, never reuse a real one

docker run --name v3-pgvector-alpha --rm -d `
  -e POSTGRES_PASSWORD=$pgPassword `
  -e POSTGRES_DB=v3embeddings_alpha `
  -p "${pgPort}:5432" `
  pgvector/pgvector:pg17
```

Verify the container is up and `vector` is installed:

```powershell
docker exec v3-pgvector-alpha psql -U postgres -d v3embeddings_alpha `
  -c "CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion FROM pg_extension WHERE extname='vector';"
```

You should see a version string (e.g. `0.7.x`).

> If you don't have Docker Desktop, a native Windows PostgreSQL 17
> install with `vector` works the same way at the SQL level. The
> documented recipe was produced against a Docker container; native
> installs are documented as compatible but are not the reference
> path.

---

## 6. Install the actual built wheels (non-editable, fresh venv)

```powershell
uv pip install .\dist\v3-core\v3_core-4.0.0-py3-none-any.whl
uv pip install .\dist\v3-hermes-plugin\v3_hermes_plugin-4.0.0-py3-none-any.whl
```

Then run `uv pip check` (pip-compatible) to confirm the two wheels
agree on every transitive dependency. Do not skip this step; a clean
`uv pip check` is part of the supported-surface evidence.

```powershell
uv pip check
```

What this does:

- Installs `v3-core` from the wheel (declares `psycopg2-binary`,
  `pgvector`, `pyyaml`, `numpy`, `requests`, `openai`, `jieba`,
  `pyahocorasick>=2.3.0`).
- Installs `v3-hermes-plugin` from the wheel (declares
  `v3-core>=4.0.0,<5.0.0`, `requests`, `pyyaml`).
- Installs two `v3-core` console scripts:
  - `v3-core` — preserved verbatim (e.g. `v3-core info`).
  - `hippocampus` — distribution-facing console (Gate 2). Provides
    `hippocampus doctor` (read-only install check) and
    `hippocampus bootstrap` (apply packaged SQL to an explicit
    target; refuses production-boundary DSNs unconditionally).
- Registers the Hermes memory provider entry point
  `hermes_agent.memory_providers / deep_memory_v3 →
  v3hermes:register` (matches the manifest `name: deep_memory_v3`).

> 💡 The `pyahocorasick` dependency is a C extension. On Windows this
> needs a working C compiler (e.g. the MSVC build tools that match
> your Python). If `pip install` fails on this step, see
> [`docs/KNOWN-LIMITATIONS.md`](KNOWN-LIMITATIONS.md) before retrying.
>
> The legacy `src/v3-core/scripts/bootstrap_alpha_db.py` script
> remains in the tree for source-tree development only. It is **not**
> the primary distribution path for this alpha — use
> `hippocampus bootstrap` (step 8).

---

## 7.5 `hippocampus doctor --static` — read-only install check

After `uv pip install` of the built wheels (step 6), run the
distribution-facing read-only sanity check before touching any
database:

```powershell
hippocampus doctor --static
```

Expected: a single JSON object on stdout with `command: doctor`,
`static: true`, and `checks.packaged_sql` listing **all four** packaged
SQL artifacts — `alpha_bootstrap.sql`, `explicit_memories.sql`,
`qa_embedding_chunks.sql`, `upgrade_v0_2.sql` — each with its `sha256`,
plus `include_markers` listing every artifact the bootstrap step will
splice into `alpha_bootstrap.sql` (`schema/explicit_memories.sql` and
`schema/qa_embedding_chunks.sql`). The `--static` flag skips config
resolution so the command is safe in packaging / CI contexts. Without
`--static`, doctor also resolves the active profile's config (read-only)
and prints a secret-redacted summary.

`doctor --static` is the documented pre-bootstrap gate: do not run
`hippocampus bootstrap --target <DSN>` until this reports `status:
ok`. It proves the **package** is complete; it says nothing about your
database. After `hippocampus bootstrap` (step 8), run the real
install verification instead:

```powershell
hippocampus doctor --full            # 16 checks: DB / auth / read / recall
hippocampus doctor --full --writes   # adds the gated write probe
```

---

## 8. DB bootstrap (explicit, NOT automatic) — packaged command

> ⚠️ **This step is explicit.** The `v3core.active_memory_store`
> writer does **not** apply the schema artifact. You **must** run
> `hippocampus bootstrap` against your disposable PG before the first
> write, or the writer will fail with a missing-table error.

The supported alpha run produced `7` tables on an empty tmpfs pg17 and
was idempotent on the second run (no errors). See
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
§ 2.1, "DB schema applied via `hippocampus bootstrap`" row.

```powershell
# Set the password via env so it never appears on a CLI argument list.
# The plugin manifest also reads V3CORE_PG_PASSWORD for `requires_env`,
# so use the same env var for both bootstrap and runtime.
$env:V3CORE_PG_PASSWORD = $pgPassword
$env:PGPASSWORD = $pgPassword

hippocampus bootstrap --target "postgres://postgres@127.0.0.1:${pgPort}/v3embeddings_alpha"
```

What this does:

- Applies the packaged `src/v3-core/src/v3core/schema/alpha_bootstrap.sql`
  verbatim, which inlines the canonical
  `src/v3-core/src/v3core/schema/explicit_memories.sql` artifact (the
  repo-root `src/v3-core/schema/*.sql` copies are the canonical
  source-of-truth and the package copies are byte-identical to them).
- Is idempotent: every DDL uses `IF NOT EXISTS` / `ADD COLUMN IF NOT
  EXISTS`. Running twice is a no-op.
- Refuses to run against a default production boundary DSN: port
  `5433` is unconditionally refused, and the combination of
  loopback host + database `v3embeddings` is also refused. There is
  no override flag; secrets never appear on argv.

> 🛑 **Never point this at a production PG.** The packaged
> `hippocampus bootstrap` command refuses the default production
> boundary DSN unconditionally; if you pass a different
> production-looking DSN, you accept the risk. The alpha contract
> assumes a disposable PG.

---

## 9. Bootstrap v3 engine config

`v3-hermes-plugin` reads engine config from the absolute path you
chose for the profile directory. The supported canonical key is the
**top-level `basePath`** in `config.yaml` (the engine resolves this in
`src/v3-core/src/v3core/config.py::_resolve_data_dir` and
`src/v3-core/src/v3core/config_model.py::V3Config`; it is the
**only** path-style key the alpha contract recommends). The simplest
bootstrap is:

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.v3-core\profiles\default"
Copy-Item .\examples\config.example.yaml "$env:USERPROFILE\.v3-core\profiles\default\config.yaml"
Copy-Item .\examples\.env.example "$env:USERPROFILE\.v3-core\profiles\default\.env"
notepad "$env:USERPROFILE\.v3-core\profiles\default\config.yaml"
```

Fill in:

- Top-level `basePath` to the absolute path of the profile directory
  you chose (e.g.
  `C:\Users\<you>\.v3-core\profiles\default`). **The path must be
  absolute; the engine does not expand `~` and does not default to a
  particular user path.** The public-alpha contract is explicit: set
  the top-level `basePath` to that absolute profile directory.
- `storage.pg.host` / `port` / `database` / `user` to match your
  disposable container (`127.0.0.1`, the disposable port you picked,
  `v3embeddings_alpha`, `postgres`).
- `storage.pg.password`: leave empty in `config.yaml` and set
  `V3CORE_PG_PASSWORD` in `.env` (the engine reads the env var and the
  plugin manifest declares it under `requires_env`). The
  `examples/config.example.yaml` ships with `password: ""` for this
  reason.
- Optional provider blocks (`storage.embed`, top-level `llm`,
  `storage.rerank`): leave them **omitted** in `config.yaml` to
  disable vector recall / LLM synthesis / rerank. The shipped example
  file has these blocks commented out — see
  [`examples/config.example.yaml`](../examples/config.example.yaml).
  If you uncomment one, fill `endpoint` / `model` with real values;
  do not leave placeholder URLs.

> Never put a real `config.yaml` or `.env` in version control. The
> example files in `examples/` contain placeholder values only.

---

## 10. Smoke check the engine

```powershell
v3-core info
```

Expected: a short status banner that prints the engine version and
reports the PG connection state. If PG is unreachable, the banner
will say so — that is expected during bring-up, not a build failure.

To exercise the durable path without a full Hermes host, you can also
run the lab recipe in [`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md)
against your disposable PG.

---

## 11. (Optional) Plug into Hermes

> **Hermes is a separate prerequisite, not a host package dependency
> here.** This repo does not claim a Hermes wheel/sdist exists. Install
> Hermes first using its official docs and only then install the
> `v3-hermes-plugin` artifact into the **same** Hermes host
> environment.

If you also have a Hermes Agent host available and want to exercise
the end-to-end hook contract:

1. Install the upstream Hermes host from its official source and
   verify with `hermes --version`. The reproducible host path used
   for the alpha sprint is a shallow clone of the current upstream
   repository followed by `uv sync`.
2. In your Hermes `config.yaml` add:
   ```yaml
   memory:
     provider: deep_memory_v3
   ```
3. Install the **same** `v3_hermes_plugin-4.0.0-py3-none-any.whl`
   from step 6 (Build → Install path) into the Hermes host
   environment. The plugin entry point is
   `hermes_agent.memory_providers / deep_memory_v3 →
   v3hermes:register`.
4. Start Hermes. (No automated setup wizard is provided; the
   `requires_env: [V3CORE_PG_PASSWORD]` constraint must be satisfied
   before the plugin loads.)
5. Have a conversation; observe `sync_turn` writes land in
   `conversation_stream` and active memories you `v3_add` are
   readable by a follow-up `v3_get` / `v3_search`.

The plugin manifest is `src/v3-hermes-plugin/plugin.yaml` (`name:
deep_memory_v3`, requires `V3CORE_PG_PASSWORD`). The installed
provider honors `HERMES_HOME` and profile selection before
initialization; do not point it at a production profile.

> ⚠️ **Hermes is a host dependency.** Without a working Hermes host,
> the plugin-mediated contract is not exercised. `v3-core` itself
> remains usable.

---

## 12. Tear down

```powershell
# Drop the disposable PG (destructive)
docker rm -f v3-pgvector-alpha

# Drop the venv if you don't want to keep it
deactivate
Remove-Item -Recurse -Force .\.venv
```

---

## 13. What this install does not do

- It does **not** auto-apply the `public.explicit_memories` DDL. Run
  `hippocampus bootstrap` explicitly (step 8). The
  `v3core.active_memory_store` writer does not apply DDL.
- It does **not** import historical data, replay old conversation
  streams, or run any of the legacy migration scripts that exist
  elsewhere in the repo history. Those are out of scope for the
  supported surface.
- It does **not** rotate, invalidate, or check any production
  credentials. See [`docs/PRIVACY-DATA-FLOW.md`](PRIVACY-DATA-FLOW.md)
  for what to do if you find historical credentials in the repo.

---

## 14. Next steps

- [`docs/CONFIGURATION.md`](CONFIGURATION.md) — every config key, every
  env var, default behavior when a provider is unconfigured.
- [`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md) — the
  `pg_dump` + `pg_restore` recipe. The clean-export E2E run on empty
  tmpfs pg17 reproduced the row counts (`raw` = 3, `QA` = 2,
  `explicit` = 1) — see
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
  § 2.5.
- [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
  — what is and isn't promised, with each row's evidence pointer.
- [`docs/RELEASE-CHECKLIST.md`](RELEASE-CHECKLIST.md) —
  the five blocking classes and their current pre-tag status.
