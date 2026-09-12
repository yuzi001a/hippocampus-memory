# v3-hermes-plugin

> **Hippocampus — v3-hermes Hermes Agent adapter.**
> Public Alpha release of this repo. See [`../../README.md`](../../README.md) for the front door and
> [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](../../docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
> for the supported-surface contract.

`v3-hermes-plugin` registers `v3-core` as a **memory provider** for the
Hermes Agent host. It wires the engine's hooks into the Hermes host,
exposes the 13 `v3_*` tools declared in `plugin.yaml`, and provides the
per-provider health report.

> ⚠️ **Hermes is a host dependency.** This adapter has no memory
> implementation of its own. To exercise the alpha surface end-to-end
> you need a working Hermes Agent host with the plugin loaded under
> `deep_memory_v3`. Without Hermes, you can still exercise `v3-core`
> directly — but the plugin-mediated hook contract is **not**
> exercised.

---

## What it does (alpha scope)

- Registers under the manifest name `deep_memory_v3`
  (`src/v3-hermes-plugin/plugin.yaml`).
- Exposes 13 tools to Hermes in 4 groups: unified entry (4),
  high-frequency (5), topic / handbook (3), health (1).
- Wires 6 hooks: `sync_turn`, `prefetch`, `on_session_switch`,
  `on_pre_compress`, `on_delegation`, `system_prompt_block`.
- Enforces the plugin-manifest `requires_env: [V3CORE_PG_PASSWORD]`
  constraint: the PG password must be present in env for the plugin
  to load under hosts that enforce `requires_env`.
- Routes active-memory writes (`v3_add`, `v3_store`,
  `v3_extract(write=True)`) through the canonical
  `v3core.active_memory_store.ActiveMemoryWriter` →
  `public.explicit_memories`.

## What it deliberately does NOT do (alpha scope)

- It does **not** ship its own memory engine. It is a thin adapter; the
  engine is `v3-core` and is installed as a dependency.
- It does **not** implement Recall V2, multi-writer, or multi-agent
  routing. See [`docs/KNOWN-LIMITATIONS.md`](../../docs/KNOWN-LIMITATIONS.md).
- It does **not** magically load any historical data. The supported
  surface assumes a fresh environment.
- It does **not** auto-apply the schema artifact. The trial user must
  run `src/v3-core/scripts/bootstrap_alpha_db.py` against their
  disposable PG before the first write.

---

## Install

```bash
pip install ./src/v3-hermes-plugin
```

This pulls in `v3-core>=4.0.0` as a dependency. The intended install
path is local-source `pip install` from this repo; see
[`docs/INSTALL.md`](../../docs/INSTALL.md) for the full bring-up recipe.

## Hermes configuration

In your Hermes `config.yaml`:

```yaml
memory:
  provider: deep_memory_v3
```

The plugin manifest is `src/v3-hermes-plugin/plugin.yaml`:

```yaml
name: deep_memory_v3
version: 4.0.0
kind: exclusive
provides_tools:
  - v3_add        # 统一入口 — write
  - v3_get        # 统一入口 — read
  - v3_update     # 统一入口 — organize
  - v3_manage     # 统一入口 — maintain
  - v3_store
  - v3_search
  - v3_status
  - v3_extract
  - v3_prefetch
  - v3_topic_correct      # experimental for this alpha
  - v3_moc_overview
  - v3_moc_get
  - v3_health
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

If your Hermes host enforces `requires_env`, you **must** set
`V3CORE_PG_PASSWORD` before the plugin loads. There is no built-in
fallback to a plaintext config value for this constraint.

## Engine configuration

`v3-hermes-plugin` reads engine config from the absolute profile
directory you set under top-level `basePath` in `config.yaml`. A
starter template with **placeholder values only** is at
[`examples/config.example.yaml`](../../examples/config.example.yaml);
a starter `.env` is at
[`examples/.env.example`](../../examples/.env.example). See
[`docs/CONFIGURATION.md`](../../docs/CONFIGURATION.md) for every key.

## Hooks (in detail)

| Hook | What it does | Network on hot path |
|---|---|---|
| `sync_turn` | Routes a finished turn through `V3Core.sync_turn` → ingest pipeline. Durable write to `conversation_stream` + QA pairing. | PG only |
| `prefetch` | Builds the recall context block within the 8s `PrefetchDeadline`. | PG; embed if configured |
| `on_session_switch` | Session bookkeeping on host-side switches. | None |
| `on_pre_compress` | Hook for host-side pre-compression. | None |
| `on_delegation` | Hook for delegation events. | None |
| `system_prompt_block` | Returns the identity / context block for the system prompt. | PG |

The prefetch hot path is **zero LLM** by design. LLM-dependent paths
(observer roll-forward, E1 yin synthesis, session summary, topic-card
extraction) run on background timers, not in the prefetch path.

## Tools (13 total)

See [`plugin.yaml`](plugin.yaml) for the canonical list. The historical
31-tool surface was narrowed to 13 in 2026-08-07. The internal
`v3core.TOOL_REGISTRY` still exposes the full 31; the plugin handler
narrows the public surface to what `plugin.yaml` declares.

The **topic / handbook** group (`v3_topic_correct`, `v3_moc_overview`,
`v3_moc_get`) includes `v3_topic_correct` which is **experimental** for
this alpha — see
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](../../docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md) § 3.

## Backup / restore

See [`docs/BACKUP-RESTORE.md`](../../docs/BACKUP-RESTORE.md). The
`backup_paths()` helper returns the local data root only; it is a hint,
not a complete backup. The supported recipe dumps PG explicitly.
**Status (this HEAD):** the clean-export E2E run on empty tmpfs pg17
reproduced the row counts (`raw` = 2, `QA` = 0, `explicit` = 1) — see
[`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](../../docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
§ 2.5. Remaining alpha limitations the alpha does not close by itself are listed
in [`docs/RELEASE-CHECKLIST.md`](../../docs/RELEASE-CHECKLIST.md)
§ E.

## Design philosophy (alpha framing)

Same as `v3-core`:

- **Source vs derived** — derived paths (observer, E1, topic, recall
  injection) must not mutate source (`conversation_stream` /
  `qa_pairs` / `public.explicit_memories`).
- **Privacy default** — unconfigured provider = no traffic to it.
- **Configuration externalized** — endpoints and credentials in
  `config.yaml` / `.env`, never in code.
- **No automatic schema bootstrap** — schema artifacts are shipped as
  files; ops applies them via `bootstrap_alpha_db.py`.

## License

AGPL-3.0-or-later — see [`LICENSE`](LICENSE). This package does not
relicense either subpackage.

---

> **Note:** this README replaces a previous version that referenced a
> `hermes memory setup` wizard and an auto-scaffolded config path. The
> alpha contract is the manual `config.yaml` + local-source `pip
> install` flow described in
> [`docs/INSTALL.md`](../../docs/INSTALL.md); the wizard behavior is
> preserved as historical context only.
