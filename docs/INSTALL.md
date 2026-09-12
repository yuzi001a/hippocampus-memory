# Install Hippocampus v0.1-alpha

> **Audience:** first-time trial users on a Windows machine who want to
> evaluate the supported public-alpha surface against a disposable,
> isolated PostgreSQL/pgvector.
>
> **Scope:** install both `v3-core` and `v3-hermes-plugin` from this
> repo's source tree, point v3 at a disposable pgvector, run the
> explicit DB bootstrap, and confirm the install succeeds on a fresh
> `venv` with a non-editable local-source `pip install`.
>
> **Out of scope:** connecting to any pre-existing production
> PostgreSQL, importing historical data, replacing a live deployment,
> or running any historical migration script. The supported alpha
> surface assumes a fresh environment.
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

## 4. Disposable pgvector container

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

## 5. Local-source `pip install` (non-editable)

The documented install path is **non-editable** local-source `pip
install`:

```powershell
pip install .\src\v3-core
pip install .\src\v3-hermes-plugin
```

What this does:

- Builds `v3-core` from `src/v3-core/pyproject.toml` (declares
  `psycopg2-binary`, `pgvector`, `pyyaml`, `numpy`, `requests`,
  `openai`, `jieba`, `pyahocorasick>=2.3.0`).
- Builds `v3-hermes-plugin` from
  `src/v3-hermes-plugin/pyproject.toml` (declares `v3-core>=4.0.0`,
  `requests`, `pyyaml`).
- Installs a `v3-core` console script (`v3-core info`).

> 💡 The `pyahocorasick` dependency is a C extension. On Windows this
> needs a working C compiler (e.g. the MSVC build tools that match
> your Python). If `pip install` fails on this step, see
> [`docs/KNOWN-LIMITATIONS.md`](KNOWN-LIMITATIONS.md) before retrying.

---

## 6. DB bootstrap (explicit, NOT automatic)

> ⚠️ **This step is explicit.** The `v3core.active_memory_store`
> writer does **not** apply the schema artifact. You **must** run
> `bootstrap_alpha_db.py` against your disposable PG before the first
> write, or the writer will fail with a missing-table error.

The supported alpha run produced `7` tables on an empty tmpfs pg17 and
was idempotent on the second run (no errors). See
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
§ 2.1, "DB schema applied via `bootstrap_alpha_db.py`" row.

```powershell
# Set the password via env so it never appears on a CLI argument list.
# The plugin manifest also reads V3CORE_PG_PASSWORD for `requires_env`,
# so use the same env var for both bootstrap and runtime.
$env:V3CORE_PG_PASSWORD = $pgPassword
$env:PGPASSWORD = $pgPassword

python .\src\v3-core\scripts\bootstrap_alpha_db.py `
  --host 127.0.0.1 `
  --port $pgPort `
  --database v3embeddings_alpha `
  --user postgres
```

What this does:

- Applies `src/v3-core/schema/alpha_bootstrap.sql` verbatim, which
  includes the canonical `explicit_memories.sql` artifact.
- Is idempotent: every DDL uses `IF NOT EXISTS` / `ADD COLUMN IF NOT
  EXISTS`. Running twice is a no-op.
- Refuses to run against a default production boundary DSN by
  default; the placeholder DSN above is the documented disposable one.

> 🛑 **Never point this at a production PG.** The script refuses the
> default production boundary DSN by default; if you pass a different
> production-looking DSN, you accept the risk. The alpha contract
> assumes a disposable PG.

---

## 7. Bootstrap v3 engine config

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

## 8. Smoke check the engine

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

## 9. (Optional) Plug into Hermes

If you also have a Hermes Agent host available and want to exercise
the end-to-end hook contract:

1. In your Hermes `config.yaml` add:
   ```yaml
   memory:
     provider: deep_memory_v3
   ```
2. Point Hermes at the venv you built in step 3 (Hermes loads the
   plugin package from the active Python).
3. Start Hermes. (No automated setup wizard is provided; the
   `requires_env: [V3CORE_PG_PASSWORD]` constraint must be satisfied
   before the plugin loads.)
4. Have a conversation; observe `sync_turn` writes land in
   `conversation_stream` and active memories you `v3_store` are
   readable by a follow-up `v3_search`.

The plugin manifest is `src/v3-hermes-plugin/plugin.yaml` (`name:
deep_memory_v3`, 6 hooks, requires `V3CORE_PG_PASSWORD`).

> ⚠️ **Hermes is a host dependency.** Without a working Hermes host,
> the plugin-mediated contract is not exercised. `v3-core` itself
> remains usable.

---

## 10. Tear down

```powershell
# Drop the disposable PG (destructive)
docker rm -f v3-pgvector-alpha

# Drop the venv if you don't want to keep it
deactivate
Remove-Item -Recurse -Force .\.venv
```

---

## 11. What this install does not do

- It does **not** auto-apply the `public.explicit_memories` DDL. You
  run `bootstrap_alpha_db.py` explicitly (step 6). The
  `v3core.active_memory_store` writer does not apply DDL.
- It does **not** import historical data, replay old conversation
  streams, or run any of the legacy migration scripts that exist
  elsewhere in the repo history. Those are out of scope for the
  supported surface.
- It does **not** touch production environments, migrate historical
  data, or replace a live deployment.

---

## 12. Next steps

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
