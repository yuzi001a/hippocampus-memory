# B02 — DSH TOOL support contract

Status: `DOING`

## Locked runtime

- DSH: `@deepseek-ai/dsh@0.1.0-rc.6`
- DSH headless bundle: `@deepseek-ai/dsh-headless@0.1.0-rc.6`
- Official MCP client: `@deepseek-ai/dsh-mcp-client@0.1.0-rc.6`
- Hippocampus patch line: `v0.2.4` (the B02 prerequisite release; release only after
  the prerequisite PR merges and required checks pass)

## Official path

DSH loads the existing v3-core MCP server through its official third-party MCP
client. The DSH layer is only the tool host:

```text
DSH headless
  -> @deepseek-ai/dsh-mcp-client
  -> stdio: v3-core mcp --profile b02-canary
  -> the existing 13 v3 tools
```

The server command is not an HTTP adapter and no DSH-native plugin is added.
The support level is explicitly **DSH SUPPORT LEVEL = TOOL**.

## Profile requirements

The selected v3-core profile must point to the intended isolated database. Keep
these two switches explicit for B02:

```yaml
observer:
  enabled: false
e1:
  enabled: false
```

B02 does not enable automatic recording or automatic recall injection. `e1` is
separate from `observer`; disabling only `observer` is insufficient because an
E1 scheduler can still perform a background synthesis attempt.

The MCP server must receive the same effective profile/config as the core. The
HTTP `/tool` and stdio MCP dispatchers therefore forward the profile-scoped
config and pool rather than resolving the default profile inside each tool.

## Supported first version

The real DSH flow is:

1. install the public patch release into a clean venv;
2. bootstrap a new empty PostgreSQL+pgvector database;
3. configure the MCP server in DSH;
4. discover the 13 tools;
5. explicitly call `v3_store` with a synthetic fact;
6. close that DSH session;
7. start a new DSH session;
8. call `v3_search`, then `v3_get(target=hm, source_id=...)`;
9. use the exact source-read content in the current answer;
10. call `v3_get(target=status)`.

`v3_get(target=hm)` now reads the `memory_id` returned by `v3_search` from
`explicit_memories` before trying the historical message/card/handbook paths.

## Explicitly out of scope

- automatic event recording;
- automatic recall injection;
- a DSH native plugin;
- B04;
- M01;
- multi-agent shared memory;
- schema redesign or production deployment.

The B01 source-layer replay finding remains open and is a B03 precondition,
not a B02 change.
