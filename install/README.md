# Hippocampus v0.2 — one-command Windows installer

The `install/` directory contains the **user-facing installer** for the
Hippocampus v0.2 First User Release. It is intentionally small: every byte
of real logic lives in `src/v3-core/src/v3core/first_run.py`, called from
the `hippocampus install` console subcommand. The PowerShell wrapper here
is a thin shell that only handles prereqs + argument passing.

---

## Quick start

From any PowerShell prompt:

```powershell
irm https://raw.githubusercontent.com/yuzi001a/hippocampus-memory/main/install/install.ps1 | iex
```

That single line:

1. Checks Python (3.10 / 3.11 / 3.12) on PATH.
2. Checks `uv` on PATH; installs it via the official installer if missing.
3. Warns early about a missing Docker daemon.
4. Delegates everything else to `hippocampus install`.
5. Prints a one-screen verdict block with `PASS / FAIL / SKIP` per step.

The install is **idempotent**: re-running never duplicates containers,
never overwrites an existing profile config without an explicit flag,
never double-edits the Hermes config.

---

## What it does (full ordered list)

| Step | Topic in verdict block | Behaviour |
| --- | --- | --- |
| 1 | `install` | Python / uv / Docker preflight; FAIL with an actionable message if Docker is missing |
| 2 | `database` | Start a disposable `pgvector/pgvector:pg17` container; verify `CREATE EXTENSION vector` works |
| 3 | `embedding` | Write the `storage.embed` block in the profile `config.yaml` (SKIP if no API key) |
| 4 | `rerank` | Write the `storage.rerank` block (SKIP if no API key) |
| 5 | `memory LLM` | Write the top-level `llm` block (SKIP if no key) |
| 6 | `hermes provider` | Edit Hermes' `config.yaml` to set `memory.provider: deep_memory_v3`, timestamped backup first |
| 7 | `restart persistence hint` | Final reminder to restart Hermes so the provider takes effect |
| 8 | (not in the block) | End-to-end smoke: write → readback → recall. Disabled with `-SkipSmoke`. |

Every step is reported individually as one of `PASS`, `FAIL`, `SKIP` plus
a one-line reason. The exit code is `0` only when every required step
really succeeded.

---

## Options

```powershell
irm https://raw.githubusercontent.com/yuzi001a/hippocampus-memory/main/install/install.ps1 |
    iex
```

Accepts these parameters (pipe them after `iex`):

| Parameter | Default | Description |
| --- | --- | --- |
| `-Preset` | `siliconflow` | `siliconflow` (Chinese-cloud default) or `custom` (no provider blocks) |
| `-PgPort` | `55432` | Local port for the disposable pg container. Port `5433` is refused unconditionally |
| `-ProfileDir` | `~/.v3-core/profiles/default` | Profile directory; absolute path required by the engine |
| `-EmbedKey` | _(empty)_ | Embedding API key. Never printed in any output |
| `-LlmKey` | _(empty)_ | Memory LLM API key. Never printed in any output |
| `-LlmBaseUrl` | _(preset default)_ | Override the LLM base URL |
| `-LlmModel` | _(preset default)_ | Override the LLM model name |
| `-SkipSmoke` | off | Skip the write+readback+recall smoke test |

Example:

```powershell
iex (irm https://raw.githubusercontent.com/yuzi001a/hippocampus-memory/main/install/install.ps1) `
    -Preset siliconflow `
    -PgPort 55432 `
    -LlmKey 'sk-...'
```

---

## Secrets

API keys and passwords are **never printed** in any output. They are
redacted to the first 4 characters + `...` in every line that names them,
including failure messages. The container password is generated locally,
stored only in `V3CORE_PG_PASSWORD` for the lifetime of the install
process, and never written to disk.

The profile `config.yaml` always contains empty `password: ""` and
`api_key: ""` fields. The engine reads the real values from
`V3CORE_PG_PASSWORD` / per-provider environment variables.

---

## Idempotency guarantees

| Action | Repeated run behaviour |
| --- | --- |
| `docker run` a fresh container | If a container named `hippocampus-pg` is already running, the installer reuses it. If it stopped, the installer restarts it. Never creates a duplicate. |
| Write profile `config.yaml` | If `config.yaml` exists, the installer prints `SKIP` and returns the existing path. Use `--overwrite-config` to force a replace (with timestamped backup). |
| Edit Hermes `config.yaml` | The installer writes a timestamped backup (`config.yaml.bak.YYYYMMDD-HHMMSS`) before each edit. When `memory.provider: deep_memory_v3` is already set, the installer prints `SKIP` and writes nothing. |
| Bootstrap schema (`hippocampus bootstrap`) | The SQL artifact uses `IF NOT EXISTS` everywhere; running twice is a no-op. |

---

## Production safety

The installer **never** writes to a real production PostgreSQL:

* The disposable port range (`55432`, configurable) is the only path it
  touches. Port `5433` is unconditionally refused.
* The combination `loopback host + database v3embeddings` is refused
  (the same rule the existing `hippocampus bootstrap` subcommand
  enforces).
* Docker port conflicts are checked: if the requested port is already
  bound, the installer either picks the next free port (when the
  existing listener is pgvector) or fails loudly with an actionable
  message (when it isn't).

---

## Uninstalling

```powershell
irm https://raw.githubusercontent.com/yuzi001a/hippocampus-memory/main/install/uninstall.ps1 | iex
```

This:

1. Stops and removes the `hippocampus-pg` container.
2. (Optional, on by default) restores Hermes' `config.yaml` from the most
   recent timestamped backup.
3. Leaves the profile directory in place (delete manually with
   `Remove-Item -Recurse -Force $env:USERPROFILE\.v3-core`).

Pass `-RemoveProfile -Yes` to also delete the profile directory.

---

## Verifying the install

After the verdict block ends with `install: SUCCESS`:

```powershell
# 1. Confirm the engine sees the right resources.
hippocampus doctor --static

# 2. Confirm a full lifecycle on the active profile.
hippocampus doctor        # resolves the active config; non-static; redacted summary
```

The smoke test already ran during install (unless `-SkipSmoke` was
passed) and persisted a marker memory into `public.explicit_memories`.
A direct recall probe should find it; you can also run
`v3-core info` to print the engine banner.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `FAIL: Docker is not installed` | Docker Desktop is missing | Install from https://www.docker.com/products/docker-desktop/ |
| `FAIL: container did not become ready within 15s` | Docker daemon hung or image pull failed | Re-run `docker info`; re-run `docker pull pgvector/pgvector:pg17` |
| `SKIP: memory LLM: — no llm_key supplied` | The installer was run without `-LlmKey` | Re-run with `-LlmKey 'sk-...'` |
| `port 55432 is already in use by something that is NOT a pgvector container` | A different process bound the port | Stop that process or pass `-PgPort <other>` |
| `V3CORE_PG_PASSWORD is not set` | Smoke ran outside the installer's env | Re-run the installer; it sets the env var for the install process |
| `existing_install: True` on a fresh system | A previous profile directory is being reused | Re-run; the installer will SKIP the write by default (use `--overwrite-config` to replace) |

---

## File map

| File | Purpose |
| --- | --- |
| `install/install.ps1` | Thin PowerShell wrapper — prereq checks + argument passing |
| `install/uninstall.ps1` | Reverses the install (container, Hermes restore, optional profile delete) |
| `install/README.md` | This file |
| `src/v3-core/src/v3core/first_run.py` | The real installer logic (frozen public API) |
| `src/v3-core/tests/test_first_run_install_contract.py` | Hermetic contract tests (no network, no Docker) |

If you change behavior, change `first_run.py` and the tests. The PowerShell
scripts are intentionally small wrappers.
