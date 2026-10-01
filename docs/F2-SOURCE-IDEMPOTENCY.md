# F2 — canonical source idempotency

Status: **ACCEPTANCE HOLD — closeout test gates in progress**. The complete
mission matrix f2-05 passed on the dedicated Windows test laptop. This is
isolated runtime evidence, not a production deployment or a completed merge.
Branch: `fix/f2-canonical-source-identity`; base:
`f11a03dc4d0a486905db70bfec2deb4143df37f3`.

## Contract and implementation

The canonical source database is the correctness authority. A repeated
`(host, session_id, event_id)` has exactly one `conversation_stream` row;
different hosts may legitimately reuse the same session/event identifiers.
The additive identity columns and partial unique index are present in both
root and packaged bootstrap/upgrade SQL copies. The insert uses PostgreSQL
conflict handling. Compatibility with the pre-identity schema rolls back an
aborted transaction before the legacy insert; unrelated database errors are
not classified as schema compatibility.

The durable pending/recovery payload retains host identity. Accepted markers
are namespaced by host; the actual target parent directory is created before
the atomic temporary-file replacement. A successful duplicate ACK remains a
valid recovery result, but does not replace the canonical-row readback.

## Isolation and harness

The tracked entrypoint is:

```text
python -m eval.f2_source_idempotency_e2e make
```

Run it from `src/v3-core`. It is opt-in and refuses hosts other than the
dedicated test laptop. Use a disposable loopback PostgreSQL target, a fresh
run root, isolated HOME/USERPROFILE, and an explicit temporary configuration.
Passwords are supplied only through process environment, never written into
generated YAML, source, reports, or command arguments.

In TEST mode an unreadable/missing explicit config, missing isolated data
root, or ambient/default fallback is a refusal. This includes no-argument
data-root resolution and the topic-store default path. The topic-store path
is lazily resolved. An off-main-thread TEST refusal terminates the isolated
process rather than leaving the server alive after only one worker exits.

Child identities are read from the OS and checked by PID, creation time and
command line before process-tree cleanup. Short-lived clients park behind a
unique per-spawn existence gate until the parent has captured and registered
their identity; the workload barrier is separate. An unverified child may
only be stopped using the parent's existing Popen handle, never a bare-PID
tree kill. Config cleanup follows proof that every child is dead.

## Verified mission matrix — f2-05

All five cases ran; no subset/old-label result is substituted for this run.

| Case | Required fault boundary | Result |
|---|---|---|
| W0 | crash before PG commit | PASS |
| W1 | PG committed before accepted marker | PASS |
| W2 | accepted durable while pending still survives | PASS |
| W3 | PG committed, caller uncertain, retry | PASS |
| W4 | repeated crash/recover, at least three cycles | PASS — 3/3 cycles |

Each case ended with one canonical source row, no duplicate QA, converged
transport and the isolated data root. W2 observed PG=1 plus both marker kinds
before kill, then accepted retained/pending removed after recovery. Each W4
recovery returned HTTP 200 with `ok=true` and a valid duplicate ACK.
All nine mandatory guards passed; sentinel changes and owned-child residuals
were zero; configuration deletion happened after child cleanup. Provider
requests were recorded as zero (record-only, not an extra F2 acceptance gate).

Frozen candidate: 338 files; ZIP SHA256:
`0ea0937c80bdd2e104cc9667d59b0ac72062f6ed40c1cb2a3f26b52566abbe6f`.
The parent read back the raw JSON and the remote file hashes. Raw logs and
historical/invalid runs are preserved in the owner-controlled incident
archive; they are not current acceptance evidence. Earlier production
isolation incidents remain historical facts, not retroactively erased by
this run. Development following the approved recovery did not deploy or
restart production.

## Closeout gates

- Real disposable-PG F2 targeted tests: **242 passed, zero failures/skips**.
- Local CI-shaped affected gate: **428 core passed, 5 intentional real-PG skips**
  (all five executed on the dedicated laptop); **102 evaluator / 12 plugin passed**.
- Compile/static checks and `git diff --check`: PASS.
- Parent post-exit check: owned residuals=0, remote frozen hash drift=0, config absent.
- Sanitized receipt: `evidence/f2-source-idempotency/f2-05-parent-acceptance.json`.
- Repository CI, commit/PR/merge: pending.

F2 is not DONE until these required gates are satisfied. Do not infer a
production canary, a full historical replay, or a whole-repository audit from
this evidence; those are explicitly outside the current mission.
