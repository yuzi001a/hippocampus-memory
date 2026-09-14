# Configuration — v3 Memory Plugin

> Two config layers. Both must be present for the supported surface to work:
>
> 1. **Engine config** at the explicit profile directory you choose
>    (the engine does NOT auto-create one and does NOT default to a
>    particular user path). On Windows the canonical path is
>    `C:\Users\<you>\.v3-core\profiles\default\config.yaml`; the
>    `<you>` is your own username and the path must be absolute
>    (`~` is **not** expanded). Read by `v3-core` (and indirectly
>    by `v3-hermes-plugin`). Owns storage, embedding, LLM, rerank,
>    profile name.
> 2. **Host config** at the Hermes Agent `config.yaml` — selects
>    which memory provider to load. The v3 plugin only needs a
>    single entry: `memory.provider: deep_memory_v3`.
>
> **Explicit data root:** the engine resolves the profile directory
> from the **top-level `basePath` key only** in `config.yaml` (see
> `src/v3-core/src/v3core/config.py::_resolve_data_dir` and
> `src/v3-core/src/v3core/config_model.py::V3Config`). Trial users
> must set `basePath` to the absolute path of the profile directory
> they intend to use. The public-alpha contract has no alternate
> data-root override. The engine does **not** expand `~` inside this
> value on Windows.

The supported config defaults are all **fail-closed**: if a provider is not
configured, the engine does not transmit data to it.

---

## 1. Engine config file

Path: the absolute profile directory you chose, e.g.
`C:\Users\<you>\.v3-core\profiles\default\config.yaml` on Windows.
**The path must be absolute; the engine does not expand `~` and does
not default to a particular user path.** Set it explicitly via the
top-level `basePath` key (the only supported contract).

The shipped template is [`examples/config.example.yaml`](../examples/config.example.yaml).
Copy it, fill in the placeholders, never commit your real copy.

```yaml
# Profile name (used as the directory under <basePath>/).
profile: default

# Top-level data root. ABSOLUTE PATH REQUIRED. The engine
# does NOT expand `~`; does NOT default to ~/.v3-core or %USERPROFILE%.
# This is the ONLY path-style key the public-alpha contract recommends.
basePath: "C:\\Users\\<you>\\.v3-core\\profiles\\default"

storage:

  pg:
    # Required for any write/read/recall path.
    host: 127.0.0.1
    port: 55432           # ← disposable port; never a production port
    database: v3embeddings_alpha
    user: postgres
    password: ""   # set V3CORE_PG_PASSWORD; never commit a real value

  # Embedding is optional. Leave commented to use keyword-only recall.
  # embed:
  #   endpoint: "https://your-embedding-host.example/v1/embeddings"
  #   api_key:  ""
  #   model:    "your-embedding-model"
  #   timeout_s: 3
  #   retries:   0

# LLM is optional. Leave commented to disable observer/E1/topic synthesis.
# llm:
#   provider: "openai-compatible-provider-name"
#   base_url: "https://your-llm-host.example/v1"
#   api_key:  ""
#   model:    "your-chat-model"
#   timeout_s: 8
#   concurrency: 3

  # Rerank is optional. Leave commented when no endpoint is configured.
  # rerank:
  #   endpoint: "https://your-rerank-host.example/v1/rerank"
  #   api_key:  ""
  #   model:    "your-rerank-model"
```

> The config schema is owned by `v3core.config` / `v3core.config_model`. If
> a key is renamed upstream, the template in `examples/` is updated; this
> doc reflects the supported alpha keys.

---

## 2. Environment variables

Env vars take precedence over `config.yaml` and are how you keep secrets out
of the config file.

| Variable | Purpose | Engine actually reads it? |
|---|---|---|
| `V3CORE_PG_PASSWORD` | Required by the plugin manifest (`plugin.yaml` lists it under `requires_env`). Overrides `storage.pg.password` in `config.yaml`. | **Yes** (`src/v3-core/src/v3core/config.py::_apply_pg_password_env`). |
| `V3CORE_CONFIG` | Explicit override for the config file path. Used during Hermes provider isolated adapter init; see `v3core.config.resolve_config` for the resolution order. | **Yes** (config file path only — does **not** override the data root). |
| `V3CORE_DOTENV` | Explicit path to a `.env` file loaded into `os.environ` before resolution. | **Yes** (config load helper). |
| `V3CORE_HOME` | Directory whose `.env` file the loader checks as an explicit fallback. | **Yes** (env-file lookup only). |

The following env vars are **not** part of the public-alpha supported
contract and are not read by the engine runtime. The plugin's
historical `get_config_schema` tool surface mentions them as a hint to
the legacy `hermes memory setup` wizard, which is itself out of scope
for the alpha; do not rely on them as documented overrides:

- `V3CORE_EMBED_API_KEY`, `V3CORE_LLM_API_KEY`, `V3CORE_RERANK_API_KEY` —
  these are **not** honored by the runtime engine. API keys for the
  embed / llm / rerank providers come from `config.yaml` directly.
- `V3CORE_PROFILE` — only used in `stage3_eval/runner.py` (a lab
  recipe); the engine runtime does not honor it.

Secrets should live in the env (or in your host's secret manager). Do not
commit `.env` files containing real credentials.

A safe starter `.env` is at [`examples/.env.example`](../examples/.env.example).

---

## 3. Host (Hermes) config

The plugin is registered under the manifest name `deep_memory_v3`. In
Hermes' `config.yaml`:

```yaml
memory:
  provider: deep_memory_v3
```

The full plugin manifest is `src/v3-hermes-plugin/plugin.yaml`:

```yaml
name: deep_memory_v3
version: 4.0.0
kind: exclusive
provides_tools: [v3_add, v3_get, v3_update, v3_manage,
                  v3_store, v3_search, v3_status, v3_extract, v3_prefetch,
                  v3_topic_correct, v3_moc_overview, v3_moc_get,
                  v3_health]
pip_dependencies:
  - v3-hermes-plugin>=4.0.0
  - v3-core>=4.0.0
requires_env:
  - V3CORE_PG_PASSWORD
hooks:
  - on_session_switch
  - on_pre_compress
  - sync_turn
  - prefetch
  - system_prompt_block
  - on_delegation
```

If your Hermes host enforces `requires_env`, `V3CORE_PG_PASSWORD` **must**
be set before the plugin loads. There is no built-in fallback to a plaintext
config value for the plugin-level env requirement.

---

## 4. Provider behavior when unconfigured

| Provider | What still works | What is skipped |
|---|---|---|
| `storage.embed` unconfigured | Active-memory write (PG canonical), keyword recall, soft-archive, restore-from-dump | Vector recall lane, IVFFLAT lookup |
| `llm` unconfigured | All writes (including observer note chain), keyword/vector recall, soft-archive | Observer note synthesis, E1 yin synthesis, session summaries, topic-card extraction |
| `storage.rerank` unconfigured | All recall paths | Re-ranking of top-N hits (engine uses embedding-ordering fallback) |
| `storage.pg` unconfigured | **Nothing** — engine fails on first I/O | Everything |

The philosophy: **silently dropping a write is worse than visibly skipping
a derived step**. If you set up observability correctly
(`v3_health` reports `llm.degraded` when LLM is missing, see
`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md` § "Health"), the skip is visible.

---

## 5. Schema artifact (active memory)

The canonical active-memory table is `public.explicit_memories`. Its DDL
lives at `src/v3-core/schema/explicit_memories.sql` and is treated as a
**schema artifact only** — `v3core.active_memory_store` does **not**
auto-apply it. Ops owns the migration.

Key schema facts a user needs to know:

- `memory_id TEXT PRIMARY KEY` — canonical id
  (`mem_` + SHA256 of the sorted-key JSON of `{category, title, content,
  tags}`); explicit caller-supplied id wins unchanged.
- `status TEXT` — `active` or `archived`; hard delete is explicitly rejected.
- `embedding VECTOR(1024)` — populated post-commit by an injected embedder,
  **never** during the canonical write path. NULL is allowed if embedding is
  disabled or fails.
- `embed_model TEXT` — non-empty iff `embedding` is set (CHECK constraint).
- `created_at` / `updated_at TIMESTAMPTZ` — server-side `NOW()` defaults; the
  post-commit embedding UPDATE leaves `updated_at` untouched.

If you run the backup/restore recipe in
[`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md), the DDL is included in the
dump and reapplied by `pg_restore`.

---

## 6. Conversation-stream / QA tables

`v3-core` writes to additional tables (`conversation_stream`, `qa_pairs`,
plus the observer and topic-card tables) that are managed by the engine
itself. Their DDL is shipped as part of the `alpha_bootstrap.sql`
wrapper and is applied by the alpha DB bootstrap script (see
[`docs/INSTALL.md`](INSTALL.md) § 6), not by `v3core.active_memory_store`
on first use.

This doc does not enumerate every column of those tables — they are
implementation details. The supported surface only commits to the durability
behavior described in
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md).

---

## 7. Path and filename rules

- **Absolute paths only** in config. Windows does not expand `~` inside
  `pathlib.Path("~/...")` calls; use `$env:USERPROFILE`-style absolute paths.
- **No leading whitespace** in YAML keys; the engine's YAML loader is strict.
- **Profile names** are restricted to `[a-z0-9_-]+` (lowercase, digits,
  underscore, hyphen). Other characters are rejected at config load.

---

## 8. Resetting a profile

To wipe a profile's runtime state (NOT the PG; this is just the local
cache/journal files under the absolute `basePath` you chose):

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.v3-core\profiles\default\*"
```

Then re-copy the example config and re-fill it. The PG is untouched.

---

## 9. Safe configuration control plane

For routine configuration changes, prefer the `v3-core config` commands
instead of editing `config.yaml` by hand. The commands use the same
profile resolver and YAML fields as the runtime. Add `--profile <name>`
to each subcommand when you are not operating on `default`.

### Discover and validate (offline)

```powershell
v3-core config show --profile default
v3-core config validate --profile default
```

`show` prints the resolved profile and config path, database connection
fields, provider endpoint/model status, feature toggles, and
conservative readiness information. Passwords and API keys are shown
only as `CONFIGURED` or `NOT CONFIGURED`; endpoint query strings and
userinfo credentials are removed from display. `show` and `validate`
do not make network requests.

`validate` catches missing files, malformed YAML, unsupported section
shapes, wrong scalar types, invalid PostgreSQL ports, and non-printing
control characters such as U+0016. A diagnostic names the section/field
and code point, never the secret value.

### Change an endpoint or model

```powershell
v3-core config set-provider embedding --endpoint "https://embed.example/v1/embeddings" --model "your-embedding-model"
v3-core config set-provider rerank --endpoint "https://rerank.example/v1/rerank" --model "your-rerank-model"
v3-core config set-provider llm --endpoint "https://llm.example/v1" --model "your-chat-model"
```

For `llm`, `--endpoint` updates the existing runtime field `llm.base_url`;
it does not create a second `llm.endpoint` setting. Either `--endpoint`
or `--model` may be supplied. Known feature toggles can be changed
explicitly, for example:

```powershell
v3-core config set-toggle e1.enabled off
v3-core config set-toggle prefetch.enabled on
```

Each mutation validates a candidate file before replacing the original
with an atomic same-directory replacement. The command does not create
automatic `.bak`, `.old`, or timestamped plaintext credential backups.
Known-field edits use a minimal text mutation so comments, ordering,
unknown forward-compatible fields, and CRLF line endings are preserved
where possible.

### Rotate provider keys safely on Windows

A key is never accepted as a command-line argument. Without `--stdin`,
the CLI uses hidden interactive input. For clipboard input on Windows:

```powershell
Get-Clipboard -Raw |
    v3-core config set-key embedding rerank --stdin

v3-core config validate
v3-core config test embedding
v3-core config test rerank
```

The stdin path removes the expected trailing CR/LF, rejects empty
values and embedded control characters, does not echo the value, and
updates all explicitly selected targets in one atomic operation. It
does not infer that embedding and rerank should share a key; list both
targets deliberately. Only after the new provider tests pass should
you revoke the old provider key.

**stdin is decoded as strict UTF-8.** The CLI reads the raw stdin
bytes and runs `bytes.decode("utf-8", errors="strict")` before
normalising the trailing newline. A clipboard payload in a non-UTF-8
codepage (e.g. an OEM/ANSI codepage produced by an older tool, a
UTF-16 paste, or any source that emitted a stray UTF-8 BOM without a
valid byte sequence) will be rejected with `BAD_STDIN_ENCODING` and a
non-zero exit — the secret is never partially read, truncated, or
coerced to a wrong value. The set-key command does not attempt to
auto-detect the system codepage and does not support arbitrary legacy
encodings.

Legacy Windows PowerShell defaults to the active OEM codepage for the
pipeline output. If `Get-Clipboard -Raw` returns an OEM-encoded
payload, the strict UTF-8 decode will fail. Force the clipboard byte
stream to UTF-8 **before** piping it into `v3-core` so the secret
survives the round-trip intact:

```powershell
# Force PowerShell's stdout / pipeline encoding to UTF-8 for this
# shell session, then re-run the clipboard pipeline. This does NOT
# transcode the secret value; it only tells PowerShell to emit the
# clipboard bytes as UTF-8 instead of the OEM codepage.
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
chcp 65001 | Out-Null

Get-Clipboard -Raw |
    v3-core config set-key embedding rerank --stdin
```

Do **not** add the secret as a command-line argument (`--key <value>`)
to work around an encoding mismatch. Command-line arguments are
visible to other processes on the host (process listings, ETW, audit
logs); the stdin contract is the only supported channel for a
clipboard-supplied secret, and it expects valid UTF-8 bytes.

The control plane keeps API keys in the existing `config.yaml`
mechanism. It does **not** implement Windows Credential Manager,
macOS Keychain, Linux Secret Service, an encrypted secret database,
or another OS-native vault.

### Explicit provider and database tests

These commands are the only configuration commands that may contact a
provider or PostgreSQL:

```powershell
v3-core config test embedding
v3-core config test rerank
v3-core config test llm
v3-core config test postgres
v3-core config test all
```

Embedding, rerank, and LLM tests use the formal runtime clients with
synthetic input and do not write to the database. The PostgreSQL
test runs only `SELECT 1`; it does not migrate, bootstrap, create
tables, or write rows. `test all` skips optional providers that are
not configured; an explicitly requested unconfigured target returns
a failure so automation cannot mistake it for a healthy connection.
`CONFIGURED` in `show` means fields are present, not that a live test
has passed.

After writing your config, the engine also ships a basic status
command:

```powershell
v3-core info
```

This prints the engine version, the resolved profile/data dir, and a
per-provider connection report. A "FAIL" line for `pg` means your
credentials or network are wrong; "SKIP" means the provider is
intentionally unconfigured (fail-closed default).

---

## 10. What this doc does not promise

- It does not promise every config key the engine accepts. Some keys are
  reserved for internal use (`sql_log_echo`, `llmstatus_endpoint`, etc.).
- It does not promise that `v3-core info` output is stable across versions.
- It does not promise that the engine will not transmit data to a provider
  you did configure but whose endpoint you typo'd. Always double-check
  `endpoint` URLs before sending real data.
- It does not promise that the engine honors any path-style config key
  other than top-level `basePath`. Legacy/internal fallbacks in the
  resolver (e.g. last-resort `Path.home() / ".v3-core" / ...`) are
  implementation details, not part of the public-alpha supported
  contract — set `basePath` explicitly.
