# RUNTIME-INTEGRITY — Hippocampus Runtime Integrity Contract

Status: design contract for `feature/runtime-integrity` (2026-09-19).
Source incident: the 2026-09-19 "shadowed venv runtime" case — production
processes loaded a legacy build from the Hermes `venv` while the approved
v0.2.1 wheel sat unused in `.hermes-runtime`, and every earlier verification
(standalone probes) reported success. This document defines what "the right
code is running" must mean, and the module that answers it.

## 1. The question this contract answers

> Is the Hippocampus code that production processes **actually load** the
> same content that was **approved by the Release**?

"It imported successfully in some Python" is **not** evidence. Neither is
"the version string says 4.0.0" (two different builds both said 4.0.0 on
2026-09-19). Evidence must come from the **live process environment** and
from **content**, not from names.

## 2. Four identity layers

### A. Distribution identity (approved artifact)
The thing a user *chose* to install: a GitHub Release asset.
Fields: `tag`, `release_commit` (optional), `wheel_filename`, `wheel_sha256`,
`source` (official_release | local_build | unknown).

### B. Installation identity (on-disk copies)
Every copy of `v3core` / `v3hermes` (plus their `dist-info`) reachable from
known roots. One `InstallCopy` per copy:
`root`, `package`, `import_path`, `version`, `install_type`,
`content_fingerprint`, `file_count`, `mtime`, `editable_target` (or null),
`state` (active | shadowed | inactive | duplicate).

### C. Resolution identity (import under a given environment)
Given an interpreter + environment (cwd, PYTHONPATH, VIRTUAL_ENV), which
files would `import v3core` / `import v3hermes` actually load?
`ResolutionResult`: `python_executable`, `pythonpath`, `cwd`,
`v3core_file`, `v3hermes_file`, `versions`, `content_fingerprint`,
`capabilities` (embed_chunks presence), `sys_path_head`.

### D. Live process identity (production reality)
For each discovered Hermes runtime process (serve, gateway, background
workers): `pid`, `ppid`, `executable`, `cmdline`, `cwd`, `PYTHONPATH`,
`VIRTUAL_ENV`, `started_at` — plus a **live-env resolution** (C) executed
with **that process's exact environment**. The environment is never
"cleaned" before probing; scrubbing it is what produced the false positive.

## 3. Verdict rule

`runtime_verified` is true **iff**

```
resolution(live process env).content_fingerprint ∈ {approved fingerprints}
```

with **no editable `.pth` active in that resolution path** and required
capabilities present.

Severity mapping:

| Situation | Verdict | Severity |
|---|---|---|
| Live resolution == approved content | `HEALTHY` | - |
| Live resolution != approved, but an approved copy exists elsewhere | `SHADOWED_APPROVED_INSTALL` | error |
| Live resolution != any approved content (no approved copy found) | `RUNTIME_RELEASE_MISMATCH` | error |
| Live resolution is an editable/source-tree install | `RUNTIME_EDITABLE_ACTIVE` | error |
| Multiple copies, all matching approved content | `DUPLICATE_ONLY` | info |
| A required process could not be probed | `RUNTIME_PROCESS_UNVERIFIED` | warn |

Directory names carry **no** authority: `venv` is not "wrong",
`.hermes-runtime` is not "right". Only content identity and actual
resolution decide.

## 4. Module layout

```
v3core/runtime_integrity/
    __init__.py      # public API re-exports (frozen surface)
    models.py        # dataclasses + enums + JSON serializers
    discovery.py     # environment roots + install copy enumeration
    resolver.py      # same-environment import probe (subprocess, stdlib-only payload)
    identity.py      # content fingerprints, wheel RECORD equivalence, capabilities
    shadow.py        # duplicate/shadow/editable conflict detection
    contract.py      # IntegrityReport assembly + verdict engine
```

Principles:
- Read-only. Never writes to any environment, DB, or process.
- Windows-first; stdlib-only in the subprocess probe payload.
- No new heavy dependencies: `psutil` is used when available (it is a
  Hermes-adjacent dependency on supported hosts); process scanning degrades
  to `wmic`/stdlib fallbacks when absent, reported as `degraded`.
- Every finding carries `evidence_scope`:
  `unit | integration | process_reproduced | live_process | production_readonly`.

## 5. Content identity (not versions)

`content_fingerprint(package_root)`:

```
sha256( "\n".join(sorted(f"{relpath}:{sha256(file)}" for code files)) )
```

- Code files = all package files except `__pycache__`, `*.pyc`, and the
  `dist-info` tree.
- The same function runs over **extracted wheel contents**, producing the
  approved fingerprint. v0.2.1 needs no new release: its identity is
  computable from the published wheel alone.
- `wheel_sha256` (of the `.whl` file itself) is recorded when the artifact
  is available; the fingerprint is the primary comparator because installed
  copies do not retain the original `.whl`.

Capability markers complement the fingerprint (cheap, human-readable):
presence of `embed_chunks.py`, `reliability/`, entry-point registration,
and the marker-format generation (legacy `embedding_error_fingerprint`
vs new `error_fingerprint`).

## 6. Duplicate & shadow detection

- "Duplicate" = more than one copy of the same distribution exists.
  - All approved → `DUPLICATE_ONLY` (info).
  - One approved + one non-approved, and the **non-approved one wins the
    live resolution** → `SHADOWED_APPROVED_INSTALL` (error) with
    `active_path`, `approved_path`, `precedence_reason`
    (e.g. `PYTHONPATH order: venv site-packages precedes runtime site-packages`).
- "Editable override" = an `__editable__*` finder or `.pth` redirect is
  reachable **and wins** for the live process → `RUNTIME_EDITABLE_ACTIVE`.
- The report must always answer **who shadows whom**, never merely
  "two installs found".

## 7. Live process discovery

Targets: processes whose command line contains `hermes_cli.main serve` /
`hermes_cli.main gateway run` (the two production roles), plus optional
`--extra-pid` probes. For each: env white-list read (`PYTHONPATH`,
`VIRTUAL_ENV`, `PATH` (presence only), plus executable/cwd/cmdline), then a
resolution probe per §2C.

Secret policy: only the white-listed variables are ever read into the
report, and `PATH` is reduced to a presence boolean + entry count. No other
environment variables are emitted, hashed, or stored.

## 8. CLI surface

```
hippocampus doctor --runtime [--json] [--deep] [--wheel PATH] [--tag TAG]
```

- `--runtime`: emit the runtime integrity section (human summary by
  default; deterministic JSON with `--json`).
- `--wheel PATH`: an explicit approved artifact (offline usage). Without
  it, the module reports `approved_source: unavailable` and downgrades
  content comparison to heuristic mode (fingerprint can still be computed
  and compared across copies; equality-to-approved cannot).
- Exit codes: 0 healthy, 1 informational/degraded (duplicates, unverified
  processes), 2 integrity violations (shadow/mismatch/editable-active).

Planned follow-on surfaces (design only this round): `install --plan`,
`upgrade --plan`, `uninstall --plan` consume the same module (§11-13 of the
round's task book).

## 9. Health / diagnose / repair integration

New health checks (additive; existing 27 checks unchanged):

- `RT05_runtime_duplicates` — duplicate_install_count, list.
- `RT06_runtime_active_install` — active copy classification + fingerprint.
- `RT07_runtime_approved_match` — active fingerprint vs approved (when an
  approved artifact is supplied; `unknown` otherwise).
- `RT08_runtime_shadow_detected` — shadow/editable conflict verdict.
- `RT09_runtime_live_processes` — per-process resolution summary; fail when
  a production process resolves to non-approved content.

Diagnose codes:
`RUNTIME_SHADOWED_INSTALL`, `RUNTIME_RELEASE_MISMATCH`,
`RUNTIME_DUPLICATE_INSTALL`, `RUNTIME_EDITABLE_ACTIVE`,
`RUNTIME_PROCESS_UNVERIFIED`.

Repair planner (dry-run only, `--apply` stays hard-disabled):
`ALIGN_ACTIVE_RUNTIME_TO_APPROVED_RELEASE` — targets the **actual loaded
environment**, never a hard-coded `.hermes-runtime`; includes restart
requirements (serve + gateway), rollback location, and `writes_database:
false`.

## 10. Performance & scope limits

- Discovery scans **known roots derived from live evidence** (executable,
  checkout, venv, `.hermes-runtime`, PYTHONPATH roots, site-packages,
  editable `.pth`). No whole-disk walks.
- Default fingerprinting hashes **selected critical files** (package
  `__init__`, capability markers, `embed_chunks.py` when present, entry
  modules); `--deep` hashes every file. Install/post-cutover verification
  (follow-on rounds) uses the full set.
- Per-process probes are one subprocess each; bounded by process count
  (normally 2-4).

## 11. What this contract does NOT do (this round)

- No installer execution, no `install --apply`.
- No production restart, no DB writes, no schema changes.
- No merging of `feature/reliability-recovery-v1` or this branch.
- No new release; v0.2.1 identity remains derivable from its published wheel.
