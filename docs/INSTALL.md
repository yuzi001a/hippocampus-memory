# Install Hippocampus v0.1-alpha

> **Audience:** first-time trial users on a Windows machine who want to
> evaluate the supported public-alpha surface against a disposable,
> isolated PostgreSQL/pgvector.
>
> **Scope:** build non-editable wheel + sdist artifacts for both
> `v3-core` and `v3-hermes-plugin` from this repo's source tree,
> install the actual built wheels into a fresh `venv`, point v3 at a
> disposable pgvector, run `hippocampus doctor --static`, then run the
> explicit `hippocampus bootstrap --target <DSN>` against that
> disposable database.
>
> **Hermes is a separate prerequisite, not a host package dependency
> here.** The sprint used current upstream
> [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent)
> installed with `uv sync`; this public repo does **not** claim a
> Hermes wheel/sdist exists. Install Hermes first using its official
> docs and only then install the `v3-hermes-plugin` artifact into the
> **same** Hermes host environment.
>
> **Out of scope:** connecting to any pre-existing production
> PostgreSQL, importing historical data, replacing a live deployment,
> running any historical migration script, or treating
> `src/v3-core/scripts/bootstrap_alpha_db.py` as a source-tree-only
> distribution path (it is source-tree / development-only). The
> supported alpha surface assumes a fresh environment.
>
> **Verification status:** this install recipe is the documented
> bring-up path. The clean-history public-tree E2E run on a fresh
> disposable Windows 10 / Python 3.11.16 / `pgvector/pgvector:pg17`
> environment is recorded in
> [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
> § 2.1 (PASS rows for clean-install + `pip check` + bootstrap
> idempotency). Remaining alpha limitations are listed in
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
git clone https://github.com/yuzi001a/hippocampus-memory
cd hippocampus-memory
```

This is the Hippocampus v0.1-alpha public repository. Do not assume
that a package name or internal version number has been renamed: the
compatible internal commands remain `v3-core` and `v3-core info`.

Verify the working tree is the public-alpha release:

```powershell
git log --oneline -1
# Records the HEAD the verifier used for the § 2 PASS rows.
```

---

## 3. Fresh venv (Windows)

Use a **fresh** virtualenv per evaluation. Mixing venvs across different
versions of v3-core or across different profile names is the most
common source of "it worked yesterday" bugs.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip wheel
```

Confirm you're inside the venv:

```powershell
where python
# Should resolve to ...\.venv\Scripts\python.exe
```

---

## 4. Build non-editable wheel + sdist artifacts

The documented distribution path is to **build artifacts first**, then
install the actual built wheels into a fresh venv — not `pip install`
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

These exact filenames are what step 5 installs. The sprint did **not**
push these to PyPI; there is no PyPI project to claim, and this
document does not direct you to install from a registry.

---

## 5. Install the actual built wheels (non-editable, fresh venv)

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
> `hippocampus bootstrap` (step 7).

---

## 6. Disposable pgvector container

Use Docker. The image is the official `pgvector/pgvector:pg17`
(matches the clean-history export E2E run; see
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
§ 1 "Environment in which the supported surface is verified"). Pick a
**local port that is not any production port** — values like `55432`
are placeholders for the disposable range. The packaged
`hippocampus bootstrap` (step 7) refuses port `5433` and local
`v3embeddings` unconditionally, so do not reuse either:

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

## 7. DB bootstrap (explicit, NOT automatic) — packaged command

> ⚠️ **This step is explicit.** The `v3core.active_memory_store`
> writer does **not** apply the schema artifact. You **must** run
> `hippocampus bootstrap --target <DSN>` against your disposable PG
> before the first write, or the writer will fail with a
> missing-table error.

The supported alpha run produced `7` tables on an empty tmpfs pg17 and
was idempotent on the second run (no errors). See
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
§ 2.1, "DB schema applied" row.

Set the password via env so it never appears on a CLI argument list.
The plugin manifest also reads `V3CORE_PG_PASSWORD` for `requires_env`,
so use the same env var for both bootstrap and runtime.

```powershell
$env:V3CORE_PG_PASSWORD = $pgPassword
$env:PGPASSWORD        = $pgPassword
```

Then run the packaged bootstrap command against the disposable target:

```powershell
hippocampus bootstrap --target "postgres://postgres@127.0.0.1:${pgPort}/v3embeddings_alpha"
```

What this does:

- Applies the packaged `src/v3-core/schema/alpha_bootstrap.sql`
  verbatim, which includes the canonical `explicit_memories.sql`
  artifact.
- Is idempotent: every DDL uses `IF NOT EXISTS` / `ADD COLUMN IF NOT
  EXISTS`. Running twice is a no-op.
- Refuses to run against a production-boundary DSN.

> 🛑 **Production-boundary refusal is unconditional.** Port `5433` and
> any loopback (`127.0.0.1` / `::1`) target whose database is
> `v3embeddings` are refused with no override flag. There is no
> `production override` switch. The documented disposable target above
> is the supported DSN shape. The legacy
> `src/v3-core/scripts/bootstrap_alpha_db.py` is retained only as a
> source-tree development helper and shares the same refusal policy;
> it is **not** the primary distribution path for this alpha.

---

## 7.5 `hippocampus doctor --static` — read-only install check

After `pip install` of the built wheels (step 5), run the
distribution-facing read-only sanity check:

```powershell
hippocampus doctor --static
```

Expected: a single JSON object on stdout with `status: ok` and
`checks.packaged_sql` listing `alpha_bootstrap.sql` and
`explicit_memories.sql` (both with sha256). The `--static` flag
skips config resolution so the command is safe in packaging / CI
contexts. Without `--static`, doctor also resolves the active
profile's config (read-only) and prints a secret-redacted summary.

`doctor --static` is the documented pre-bootstrap gate: do not run
`hippocampus bootstrap --target <DSN>` until this reports `status:
ok`.

---

## 8. Bootstrap v3 engine config

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

## 9. Smoke check the engine

```powershell
v3-core info
```

Expected: a short minimal status summary. This command is not the
provider-health contract and must not be interpreted as requiring a
literal `pg: OK` line; use the documented `v3_health` path when you
need per-provider health.

To exercise the durable path without a full Hermes host, you can also
run the lab recipe in [`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md)
against your disposable PG.

---

## 10. Plug into Hermes

If you have a current Hermes Agent host available, install the two
built artifacts into that same host environment. The reproducible host
path used for this sprint is a shallow clone of the current upstream
repository followed by `uv sync`; the upstream project is not claimed
as a wheel/sdist dependency here.

1. In the Hermes host config select:
   ```yaml
   memory:
     provider: deep_memory_v3
   ```
2. Ensure `V3CORE_PG_PASSWORD` is set before the host loads the plugin.
3. Start Hermes and verify `deep_memory_v3` is listed by the host memory
   provider surface. The provider entry point is
   `hermes_agent.memory_providers / deep_memory_v3 -> v3hermes:register`.
4. Use `v3_add` / `v3_get` through the host path; do not treat a direct
   `v3-core` call as full Hermes adapter acceptance.

The installed provider honors `HERMES_HOME` and profile selection before
initialization; do not point it at a production profile.

---

## 11. Tear down

```powershell
# Drop the disposable PG (destructive)
docker rm -f v3-pgvector-alpha

deactivate
Remove-Item -Recurse -Force .\.venv
```

---

## 12. What this install does not do

- It does **not** auto-apply the `public.explicit_memories` DDL. Run
  `hippocampus bootstrap` explicitly (step 7).
- It does **not** import historical data, replay old conversation
  streams, or run legacy migration scripts.
- It does **not** touch production environments, migrate historical
  data, or replace a live deployment.

---

## 13. Next steps

- [`docs/CONFIGURATION.md`](CONFIGURATION.md) — every config key, every
  env var, default behavior when a provider is unconfigured.
- [`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md) — the
  `pg_dump` + `pg_restore` recipe. The clean-export E2E run on empty
  tmpfs pg17 reproduced the row counts (`raw` = 2, `QA` = 0,
  `explicit` = 1) — see
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
  § 2.5.
- [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
  — what is and isn't promised, with each row's evidence pointer.
- [`docs/RELEASE-CHECKLIST.md`](RELEASE-CHECKLIST.md) —
  the alpha publication gates and post-alpha limitations.
