# Hippocampus × Hermes v0.22.2 — upgrade execution sheet

Prepared 2026-10-18. **Nothing in this document has been applied to production.**
Production is still v0.21.5 and stays that way until the upgrade is run deliberately.

---

## 1. Pinned versions and checksums

| Item | Value |
|---|---|
| Hermes target release | **v0.22.2** (latest stable) |
| Tag object | `7b77e29eedd85a8497ce2821448dac7964dc46f6` |
| Release commit | `d923a5eb487a17c0a68801bba9ef0f56c81040a8` |
| Hippocampus source HEAD | `10df437859f48760d85507a6d5582b730f176c5c` (main, after PR #51) |
| `v3_core-4.0.1-py3-none-any.whl` | sha256 `f3b2d43e270acec6104e8a36eea458683fdac2d3872a3de14f304c0fec2048fb` |
| `v3_hermes_plugin-4.0.1-py3-none-any.whl` | sha256 `a3e7049d700a07928ca50db81952231c16f4062b816ce4d3ba2a797875f5104b` |
| Host Python used for acceptance | 3.14.7 |

Wheels were built by `uv build --wheel` from a `git archive` of **clean main**
(no working-tree edits), then installed into a fresh venv with the real
v0.22.2 host. `uv pip check` → "All installed packages are compatible" (103 packages).

> **Hash scope:** the two sha256 values identify the exact files handed over for
> this upgrade. `uv build` is not bit-reproducible (archive entry timestamps), so
> a later rebuild from the same commit will produce **different** hashes. Compare
> against the delivered files; do not re-derive them from a fresh build.

**Production baseline being upgraded from:** Hermes v0.21.5 (2026.9.24),
upstream `24b9f0f8`, Python 3.11.16, **install method: git** (not pip), venv at
`C:\Users\servi\AppData\Local\hermes\hermes-agent\venv`.

---

## 2. What changed in this cycle

Four defects, each with a targeted regression test. The first three merged in
PR #49; the fourth (MCP) merged in PR #51 and is the reason the wheels are 4.0.1.

| # | Defect | Effect if unfixed on v0.22.2 |
|---|---|---|
| 1 | Summary worker started with a bare `threading.Thread` | Background jobs start with an empty context, so `HERMES_HOME`/profile resolves to the **default** profile: another profile's data, or the wrong data directory |
| 2 | Staleness guard re-read `_summary_generation` instead of the queued value | A job queued before a Runtime rebind runs against the **new** Runtime — the guard can never fire |
| 3 | `plugin.yaml` listed the plugin itself as a pip dependency (not on PyPI), missing `requests`/`pyyaml` | A manager acting on the manifest tries to fetch a package that cannot exist, and reports the provider unavailable for the wrong reason |
| 4 | `v3-core` declared `mcp>=1.0,<2` as a **core** requirement, though only the standalone `v3-core mcp` server imports it | Hermes v0.22.2 pins `mcp==2.0.0`, so the plugin manager's dependency merge is unsatisfiable and the provider is evicted at admission |

### MCP is now an optional extra

`v3-core` no longer requires the MCP SDK. It lives in the `mcp` extra:

```bash
pip install 'v3-core[mcp]'   # only for the standalone stdio MCP server
```

The plugin path (the 13 tools, hooks, prefetch/ingest) never imports `mcp`, so a
host that pins another MCP major is unaffected. If you run the standalone server,
give it its **own** venv — the same one the DSH client points at — rather than
the host's environment.

`spawn_context_thread` is used when the host provides it; older hosts (v0.21.5)
still get a working thread, so **the new wheels are backward compatible** — they
can be installed before, during, or after the Hermes upgrade.

---

## 3. Install / recover the plugin

Wheels are project-built. They are **not on PyPI**, and no install step should
try to fetch them from an index.

```powershell
$py = 'C:\Users\servi\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'
uv pip install --python $py `
  .\v3_core-4.0.1-py3-none-any.whl `
  .\v3_hermes_plugin-4.0.1-py3-none-any.whl
uv pip check --python $py
```

If the host rebuilds the venv (or `hermes update` replaces it), the entry point
disappears. It fails **loudly**, not silently:

```powershell
hermes memory status
# Provider: deep_memory_v3      <- configured
# Plugin:  NOT installed        <- venv was rebuilt; re-run the install above
```

Recovery is the same two `uv pip install` lines. After that `hermes memory status`
must print `Plugin: installed`, `Status: available`, and `deep_memory_v3 … active`.

---

## 4. Back up before upgrading (production)

1. **PostgreSQL** — full dump, then verify it restores:
   `docker exec v3-pgvector pg_dump -U v3user -d v3embeddings > <backup>\v3embeddings-<date>.sql`
2. **v3 data directory** — the provider's `base_path` (tombs/index/cache live there).
3. **Hermes home** — `~/AppData/Local/hermes/config.yaml`, `profiles/`, `.env`
   (secrets stay where they are; do not copy them into the workspace).
4. **venv freeze** — `uv pip freeze --python <venv>\Scripts\python.exe > <backup>\freeze-0.21.5.txt`.
5. **Current wheels** — keep the 4.0.1 pair (the 4.0.0 core wheel must not be installed on v0.22.2).
6. **Production git commit** — `24b9f0f8` is the rollback target for the host itself.

---

## 5. After the upgrade — read-only checks first

```powershell
hermes --version                 # expect v0.22.2, Python 3.11.16
hermes memory status              # Provider deep_memory_v3 / installed / available / active
```

Then, through the agent (all read-only):

- `v3_health` — reports PG / embed / cron chain state.
- `v3_status` — card counts by category.
- `v3_prefetch` on a known term — must return hits with `source_id` provenance.
- SQL read-only, if needed:
  `docker exec v3-pgvector psql -U v3user -d v3embeddings -c "select count(*) from conversation_stream;"`

## 6. Minimal real write / recall check (needs an explicit go-ahead)

These write to production and should only run once, deliberately:

1. One ordinary turn in a session, then confirm `conversation_stream` and
   `qa_pairs` grew, and that the new rows carry `source_id = qa_sync/<session>/<n>/…`.
2. Restart the agent process, then `v3_prefetch` the same subject — the earlier
   turn must still be reachable, with identical provenance.
3. Switch sessions and confirm the new session id is what lands in `session_id`
   (no cross-profile bleed).

---

## 7. Rollback

**Plugin only** (host already on v0.22.2, provider misbehaving):

```powershell
uv pip uninstall --python $py v3-hermes-plugin v3-core
uv pip install --python $py .\v3_core-4.0.1-py3-none-any.whl .\v3_hermes_plugin-4.0.1-py3-none-any.whl
hermes memory status
```

**Host** (v0.22.2 itself is the problem). This install is **git-based**, so a
pip pin is not the lever:

```powershell
git -C C:\Users\servi\AppData\Local\hermes\hermes-agent checkout 24b9f0f8
# then reinstall the venv exactly as hermes update does, or restore the venv copy
uv pip sync --python $py <backup>\freeze-0.21.5.txt
hermes --version                  # must print v0.21.5 again
```

The upgraded wheels run on v0.21.5 as well, so the plugin does not need to be
rolled back for the host to be restored.

---

## 8. Status of every check

### Passed (isolated, real v0.22.2 host, disposable PostgreSQL on 55433)

- 26 plugin tests in the isolated tree (11 host-contract, 3 manifest, plus A0).
- Product CI green on PR #49, including the Windows distribution-packaging gate.
- Provider **discovered, loaded, activated**: `deep_memory_v3 … active`.
- 13 tool schemas registered; `v3_status` **actually executed** through the host.
- 6 hooks present and callable: `on_session_switch`, `on_pre_compress`,
  `sync_turn`, `prefetch`, `system_prompt_block`, `on_delegation`.
- End-to-end chain, phase A (13/13): initialize → sync_turn receipt
  (`accepted=2`) → 6 stream rows + 3 QA pairs → vectors present (3/3) →
  session identity → prefetch → session switch → shutdown (`generation 1→3`).
- End-to-end chain, phase B, **new process** (6/6): rows survived, `source_id`
  identity intact (`qa_sync/e2e-sess-A/…`), read-back 321 chars, 13 tools.
- Background-thread context inheritance: the spawned worker sees the caller's
  `HERMES_HOME`; a bare thread provably does not.
- Install: zero third-party removals/downgrades; entry point survives the host's
  own dependency refresh.

### Deliberately not done

- **No production upgrade, no production write, no production config change.**
- No real model interaction in the isolated host — there are no isolated model
  credentials, so every model-side claim above is *mechanical*, not a live LLM run.
- LoCoMo / M03 canary / full historical evaluation were not re-run (out of scope
  per the task; their earlier results still stand).

### Open, accepted behaviour

- `on_pre_compress` under the **v1** contract returns a string and is
  best-effort; with a synthetic corpus it returned `""` (no high-signal
  snippets). Production has `compression.checkpoint_required` **absent**, so
  the fail-closed v2 path is not in play, and the provider correctly declares
  `pre_compress_checkpoint_api_version = 1`. It does **not** claim v2.
- If `checkpoint_required` is ever turned on, this provider will be reported as
  incapable by design — that must be treated as a decision, not a surprise.
