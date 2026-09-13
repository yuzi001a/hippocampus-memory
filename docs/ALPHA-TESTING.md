# Public Alpha testing guide

Hippocampus v0.1-alpha needs long-running, real-world use more than synthetic praise.

If you already use Hermes or another coding agent across multiple sessions, this guide gives you a small test plan that can produce useful feedback without pretending the experimental parts are stable.

## Who this is for

The most useful testers are people who already feel one or more of these problems:

- the agent repeats mistakes from previous sessions;
- important project decisions have to be re-explained;
- a small `MEMORY.md` or equivalent file has become overloaded;
- old conclusions keep coming back after they have been superseded;
- session search can find history, but the agent does not naturally use the right part of it;
- you care about keeping the original source separate from later summaries or conclusions.

If you only need a few stable preferences to survive across sessions, Hermes built-in memory may already be the simpler answer.

## Important alpha boundary

The safest public-alpha path today is explicit memory plus durable source ingest. Do **not** design your test around an assumption that Observer/E1 will automatically and reliably turn every conversation into good long-term memory. Those automatic-memory paths are still experimental.

Before testing, read:

- [`PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
- [`KNOWN-LIMITATIONS.md`](KNOWN-LIMITATIONS.md)
- [`INSTALL.md`](INSTALL.md)

## Suggested 3-7 day test

Use Hippocampus on one real project rather than a toy conversation. Keep the scope small enough that you can still tell what the correct answer should have been.

During the test, intentionally create these five cases.

### 1. A durable project decision

Store a decision that should still be true several sessions later.

Example:

> Retrieval changes must preserve SQLite/PostgreSQL parity.

A later session should be able to recover that constraint when it becomes relevant.

### 2. A superseded decision

Record an older decision, then later replace it in reality with a new one.

The important question is not merely whether both can be found. Record whether the agent treats the obsolete one as current, distinguishes history from current state, or becomes confused.

Temporal truth and supersession remain areas where the project still has work to do, so failures here are especially useful.

### 3. A stable user or workflow preference

Store something that should remain valid across sessions, such as a preferred testing style or a rule about how a project should be changed.

Check whether the memory is recalled only when relevant, rather than becoming noise in unrelated tasks.

### 4. A source-vs-interpretation case

Choose one conversation where the agent could plausibly summarize you incorrectly.

Later, inspect whether you can still get back to the source material instead of being trapped inside the derived interpretation. This is one of the architectural reasons Hippocampus preserves source data separately from derived memory.

### 5. Restart durability

Restart the relevant process and verify that important explicit memories still read back correctly. If you are comfortable doing so in a disposable environment, also exercise the documented PostgreSQL backup/restore path.

## What to record

You do not need a benchmark harness. For each notable case, capture:

- what you expected the agent to remember;
- what was actually recalled;
- whether the returned memory was current, stale, duplicated, or irrelevant;
- whether the answer could be traced back to a source;
- whether the problem appeared only after a restart or new session;
- the exact Hippocampus commit/version and Hermes version;
- the configured LLM / embedding / rerank setup, if relevant.

A **bad recall** is often more useful than a successful one. Please do not clean up the story before reporting it.

## Good feedback examples

Useful:

> I changed the project's deployment target on day 2. On day 4 the agent retrieved both the old and new target and treated the old one as current. Here is the query and the two returned memories.

Useful:

> After restart, the explicit memory was still present, but the new session did not recall it for a semantically related question until I used the exact keyword.

Less useful:

> Memory seems bad.

Less useful:

> It worked once with a preference example.

## Privacy

Do not post API keys, DSNs, private repository contents, personal chat logs, or sensitive user data in a public issue.

A minimal reproduction with redacted text is enough. If the bug depends on private data, describe the shape of the data and the observed behavior rather than publishing the data itself.

## Reporting feedback

Use the repository's **Alpha memory feedback** issue template when possible. For general bugs unrelated to memory quality, a normal GitHub issue is fine.

If you are unsure whether something is a bug, an unsupported alpha path, or a design question, report it anyway and say what you expected. The distinction can be made afterwards.

## What success means for this alpha

The goal is not to prove that Hippocampus "has solved memory". The useful outcome of the public alpha is much smaller:

1. the supported durable paths work on machines other than the author's;
2. real long-running sessions reveal where recall becomes noisy, stale, or misleading;
3. installation friction becomes concrete rather than hypothetical;
4. the next architectural changes are driven by observed failures instead of imagined edge cases.

That is enough for a good alpha.
