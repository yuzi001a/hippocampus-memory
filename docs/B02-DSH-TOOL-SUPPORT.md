# B02 — DSH TOOL support contract

Status: `DONE` (tool level). Evidence below is from the public `v0.2.8` release asset
on a fresh disposable environment; the session transcripts are quoted verbatim.

**DSH SUPPORT LEVEL = TOOL.** DSH loads the existing v3-core MCP server through its
official third-party MCP client and the model calls the tools explicitly. There is
**no automatic recording and no automatic recall injection** on this path.

## Tested versions

| Component | Version |
|---|---|
| DSH | `@deepseek-ai/dsh@0.1.0-rc.6` |
| DSH headless bundle | `@deepseek-ai/dsh-headless@0.1.0-rc.6` |
| Official MCP client | `@deepseek-ai/dsh-mcp-client@0.1.0-rc.6` |
| Hippocampus | `v0.2.8` (`v3_core-4.0.0-py3-none-any.whl`, tag `v0.2.8`, main `d0b8904`) |
| Python / DB | CPython 3.11, `pgvector/pgvector:pg17` container, fresh bootstrap |
| Embedding provider | SiliconFlow `BAAI/bge-m3` (1024-d) — configured in the profile, live |

The verified prerequisite chain on this path is `v0.2.6` → `v0.2.7` → `v0.2.8`; those
three patch releases are what make the DSH tool path honest (see *What had to be fixed*
at the bottom). Older releases list the 13 tools and then fail on the first real call.

## Exact MCP configuration

DSH loads a patch file; the MCP server is the installed console script and the profile
is selected by `--profile`:

```yaml
# cordis.patch.yml
- insert:
    - id: mcp-v3mem
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        servers:
          - name: v3mem
            command: 'C:/path/to/venv/Scripts/v3-core.exe'
            args: ['mcp', '--profile', 'b02-canary']
```

Launch:

```bash
dsh.cmd --profile headless --patch cordis.patch.yml --json '<prompt>'
```

Environment rules (this is the part that bites):

- The profile lives under `<HERMES_HOME>/.v3-core/profiles/<profile>/` on a fresh
  install. Since `v0.2.6` the CLI/MCP entry points read `HERMES_HOME` from the
  environment, so the MCP child resolves the same profile as the installer wrote.
  Export `HERMES_HOME` into the DSH process, or pin `V3CORE_CONFIG=<profile>/config.yaml`.
- **Do not export provider credentials into the DSH child.** The profile's own `.env`
  (written by `hippocampus install --embed-key ...`) is the credential channel. An
  exported variable with a different name (`SILICONFLOW_API_KEY` etc.) is never read by
  the engine and the symptom is a provider `401` on every write.
- Keep the profile's `observer.enabled: false` and `e1.enabled: false` for B02.

## What a tool-level integration actually does

```text
DSH headless
  -> @deepseek-ai/dsh-mcp-client (official)
  -> stdio: v3-core mcp --profile b02-canary
  -> 13 tools (v3_store / v3_search / v3_get / ... )
```

Capability matrix (as verified on `v0.2.8`):

| Capability | Status | Evidence |
|---|---|---|
| 13 tools discovered by the official client | **PASS** | `tools/list` = 13, every session |
| `v3_store` durable write | **PASS** | `durable=true`, `durable_store=explicit_memories`, `status=DURABLE_COMMITTED`, `warnings=[]` |
| Embedding written for a stored record | **PASS** | DB readback `embedding IS NOT NULL`, `vector_dims=1024` (post-commit update) |
| `v3_search` semantic (vector) recall | **PASS** | reworded query → `cosine 0.5324` on the target record |
| `v3_get(target=hm)` full source read | **PASS** | exact stored text returned with `source=explicit_memories` |
| Cross-process persistence (new host process) | **PASS** | a third session, new DSH + new MCP child, retrieved and read the same record |
| Honest failure reporting | **PASS** | credential removed → `durable=true` but `status=DERIVED_WARNING` + `warnings=[... class=EMBEDDING_AUTH_FAILED ... 401 ...]` |
| `v3_prefetch` | topic cards only | designed that way; on a fresh DB it returns `[]` — use `v3_search` for records |
| Automatic recording / recall injection | **not implemented** | this is B04, explicitly out of scope |

## The verified flow (public `v0.2.8` asset, fresh env)

1. `hippocampus install --preset siliconflow --embed-key … --pg-port 55523 \
   --profile-dir <HERMES_HOME>/.v3-core/profiles/b02-canary --hermes-home <HERMES_HOME>`
   → `INSTALL OK`: database / embedding / rerank / memory LLM / hermes provider all
   `PASS`, plus the install's own write+readback+recall smoke.
2. Session A — `v3_store`, synthetic project fact:

```text
durable: true   durable_store: explicit_memories   status: DURABLE_COMMITTED
warnings: []    source_id: mem_f09a33…
DB readback:    B02_V026_FACT | embedding not null = true | vector_dims = 1024
```

3. Session B — new DSH session, **reworded** question
   (`"what is the response deadline for the most serious tickets, and what must be
   filed when responsibility changes hands?"` — no substring overlap with the record):

```text
v3_search → total 4
  #1 mem_f09a33…  B02_V026_FACT       cosine 0.5324   ← vector lane
  #2 yin:e1_seg…  行为基线             cosine 0.4078
  #3 mem_349b82…  first_run smoke      cosine 0.2913
v3_get(target=hm, source_id=mem_f09a33…) → full source (source=explicit_memories)
answer: "30 minutes acknowledgement" + "handover summary before every shift change"
```

4. Session C — DSH and the MCP child fully restarted, a different natural question:

```text
v3_search   → the same record retrieved
v3_get(hm)  → full source
answer:     "Thursday 09:30 Beijing time" (the record's rollout window)
```

`curl`-free, DSH-native, and the answer text comes from the read-back source.

## Known limitations of this path

- **`v3_prefetch` is topic-card recall.** It does not search explicit memories. On a
  fresh install (no topic cards) it returns `[]`. Records are reached through
  `v3_search` → `v3_get(target=hm)`.
- **The tools share one union-parameter signature.** A model can call `v3_get` with
  parameters that belong to another tool (e.g. `action=read`); the tool then falls back
  to `target=status` and looks like "the tool has no read path". Prompt the model with
  the exact parameter names (`target=hm`, `source_id=…`) or it will flail.
- **`v3_store`'s response reports `embedding: null`** on a healthy install. That is the
  pre-embed snapshot: the vector is written by a post-commit update. Verify with a read
  (`v3_get`), not with the write response.
- **`embedding_failures` does not exist in a fresh bootstrap** (registered as F3 in
  `docs/B01-FINDINGS.md`), so an install cannot show a failure ledger. Credential
  problems surface as `DERIVED_WARNING` + `EMBEDDING_AUTH_FAILED` in `warnings`, and in
  the MCP server's stderr as `embedding HTTP 401`.
- **`topic_recall: no data source (PG failed, SQLite not found)`** is logged by the
  topic-recall layer on a fresh install. It is a warning only: the record lanes
  (`v3_search` card path, `v3_get`) are unaffected. Tracked as a follow-up, not a
  blocker for TOOL support.

## Explicitly out of scope

- automatic event recording; automatic recall injection; a DSH native plugin; B04; M01;
  multi-agent shared memory; schema redesign; production deployment.

The B01 source-layer replay finding (`OUTBOX_REPLAY_SOURCE_AT_LEAST_ONCE`) remains open
and is the B03 precondition, not a B02 change.

## What had to be fixed to get here

Recorded so the constraint is not rediscovered:

| Release | Defect on this path | Fix |
|---|---|---|
| `v0.2.6` | The MCP/CLI entry points never read `HERMES_HOME`, so a fresh install's profile was invisible to the MCP child: 13 tools listed, first call failed with `V3CORE_PG_PASSWORD not set`. | `main 7a59e5d`: `_find_config` treats `HERMES_HOME` as the ambient default; `V3Core` records the same value. |
| `v0.2.7` | Core-internal leaves (topic recall, pools) resolved the **default** profile: a process booted on a non-default profile read another install's database (measured: foreign `topics=349`) and cached its topic matrix locally. | `main 8ebc431`: a bare `resolve_config()` now means the profile this process booted; the core binds it on init and releases it on teardown. |
| `v0.2.8` | `v3_search` → `search_cards` called `recall_pool` without the card-vector lane, so a reworded question over stored records returned 0 hits (keyword lane only). | `main d0b8904`: `search_cards` passes the same `include_card_vector=True` contract `prefetch` already used. |
