# UPGRADE — Hippocampus runtime upgrade contract

Status: contract for `feature/runtime-integrity` (2026-09-19). Implements
the round's task-book §31: a standard v0.2.x → future-release upgrade must
never repeat the 2026-09-19 failure — a cutover that reported success while
the live processes kept loading a shadowed legacy build.

## The eight required steps

1. **Snapshot** the current live identity:
   `hippocampus doctor --runtime --json` → save the output.
2. **Obtain** the release: official GitHub Release asset only; verify the
   wheel SHA256 against the release manifest.
3. **Discover the target**: `hippocampus install --plan --wheel <wheel>`
   must report the *actual loaded environment* derived from live process
   resolution. Never assume a fixed directory.
4. **Backup** the target environment's packages (`v3core`, `v3hermes`,
   both `dist-info`) to a timestamped directory.
5. **Install** into the actual loaded environment:
   `uv pip install --python <live-env python> --force-reinstall --no-deps <core.whl> <plugin.whl>`.
6. **Restart every affected component** — both `serve` and `gateway` load
   the plugin; restarting one is not enough.
7. **Verify live provenance**: `hippocampus doctor --runtime --wheel <wheel>`
   must report `HEALTHY` (exit 0) *after* the restart. A warn exit means a
   stale process remains — the restart is incomplete.
8. **Rollback on failure**: restore the backup packages, restart, re-verify.

## Why each step exists

- **3 + 5**: `PYTHONPATH` precedence decides which copy the live processes
  load; a venv copy can shadow a staged wheel invisibly
  (see `RUNTIME-INTEGRITY.md` §2). Directory names carry no authority —
  `venv` is not "wrong", `.hermes-runtime` is not "right".
- **2 + 7**: version strings cannot distinguish builds (`4.0.0` appeared on
  both sides of the 2026-09-19 incident). Only content fingerprints computed
  under the live environment count.
- **7 (stale guard)**: even a correct disk fails verification while an old
  process still runs it. `doctor --runtime` reports `warn` with an explicit
  "restart required" note; do not treat that as a pass.
- **6**: the 2026-09-19 postmortem confirmed serve and gateway each load the
  `v3hermes` plugin; a restart plan that touches only one of them is wrong.

## Offline / air-gapped installs

`doctor --runtime` and `install --plan` accept `--wheel <path>`; nothing in
the verification path requires network access. Download the wheel once (from
the official Release), verify its SHA256, and pass the local path. The plan
reports `approved_source` so a local build is never silently treated as an
official release.

## Quick reference

```bash
# 1. snapshot
hippocampus doctor --runtime --json > before.json

# 3. plan (must show the actual loaded environment)
hippocampus install --plan --wheel v3_core-4.0.0-py3-none-any.whl

# 7. verify after restart (exit 0 required)
hippocampus doctor --runtime --wheel v3_core-4.0.0-py3-none-any.whl
```
