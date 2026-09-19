# ADR-005: Runtime Integrity contract & the definition of "install success"

Date: 2026-09-19 · Status: accepted for `feature/runtime-integrity`

## Context

On 2026-09-18/19 the v0.2.1 cutover was declared successful based on
standalone-import probes. Production kept loading a 2026-09-17 build that
was shadowed via `PYTHONPATH` (`<checkout>;<checkout>\venv\Lib\site-packages`
precedes `.hermes-runtime`). The discrepancy was invisible until content
identity was examined under the live process environments. The same class of
false positive had also occurred in the earlier v0.2 deployment attempt.

## Decisions

1. **"Runtime verified" means**: `live process actual resolution == approved
   Release content` (content fingerprint), with no editable override active.
   Path names (`venv` vs `.hermes-runtime`) are not evidence of correctness.
2. **Never** use "a standalone Python imported the wheel successfully" as
   sufficient evidence of a cutover.
3. Deployment targets the **actual loaded environment**, determined from
   live process resolution — never a hard-coded directory.
4. Duplicate copies of the approved artifact are **informational** (info/warn);
   a shadowed approved copy under a non-approved active copy is an **error**.
   Diagnostics must answer *who shadows whom*, with a precedence reason.
5. **"One-click install success" (`INSTALL_VERIFIED`) requires all nine**:
   1. package obtained
   2. artifact verified (SHA256 vs release manifest)
   3. host discovered
   4. active environment discovered
   5. duplicate/shadow scan
   6. install completed
   7. required processes restarted
   8. real-process import verified (live-env probe)
   9. read-only memory smoke PASS

   `pip exit 0` is step 6 at best.
6. Provenance probes must run with the **same environment** as the live
   processes (`PYTHONPATH` et al.). Sanitized probes are rejected as
   evidence (`evidence_scope` distinguishes `process_reproduced` from
   `live_process`).
7. The deployment/doctor contract must detect duplicate installs, shadowed
   packages, editable overrides, and content-identity mismatches, and must
   fail closed. (Tracked as `DEPLOYMENT_RUNTIME_PROVENANCE_PROBE_DEFECT`.)

## Consequences

- New module `v3core.runtime_integrity` (discovery / resolver / identity /
  shadow / contract / plan).
- `hippocampus doctor --runtime` (human + JSON; exit 0/1/2).
- `hippocampus install --plan` (plan only; execution deferred and
  operator-gated).
- Health checks RT05–RT09; diagnose codes `RUNTIME_SHADOWED_INSTALL`,
  `RUNTIME_RELEASE_MISMATCH`, `RUNTIME_DUPLICATE_INSTALL`,
  `RUNTIME_EDITABLE_ACTIVE`, `RUNTIME_PROCESS_UNVERIFIED`; repair action
  `ALIGN_ACTIVE_RUNTIME_TO_APPROVED_RELEASE` (dry-run only).
- Upgrades follow `docs/UPGRADE.md` (eight steps).
