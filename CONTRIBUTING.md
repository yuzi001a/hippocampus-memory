# Contributing to Hippocampus

> **Status of this document:** Public-alpha-honest. It describes how to
> file issues against the public-alpha surface. It does **not** invite
> large code patches, refactors, or "while you're at it" PRs — see § 4
> for the contribution scope.

---

## 1. Where to start

If you're a trial user:

1. Read [`README.md`](README.md).
2. Follow [`docs/INSTALL.md`](docs/INSTALL.md).
3. Read [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
   so you know what's claimed.
4. If something breaks, file an issue (see § 3).

If you're evaluating the engine itself:

1. Read [`docs/ARCHITECTURE-OVERVIEW.md`](docs/ARCHITECTURE-OVERVIEW.md).
2. Read [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
   for the supported-surface contract.
3. Skim [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md) for the
   items the alpha does **not** close.

## 2. Repository layout

```
hippocampus-memory/
├── README.md                      ← front door
├── CONTRIBUTING.md                ← this file
├── SECURITY.md                    ← security disclosure policy
├── CHANGELOG.md                   ← user-facing changes
├── docs/                          ← user-facing documentation
│   ├── INSTALL.md
│   ├── CONFIGURATION.md
│   ├── BACKUP-RESTORE.md
│   ├── PRIVACY-DATA-FLOW.md
│   ├── PUBLIC_ALPHA_SUPPORTED_SURFACE.md
│   ├── ARCHITECTURE-OVERVIEW.md
│   ├── KNOWN-LIMITATIONS.md
│   ├── RELEASE-CHECKLIST.md
│   └── ...                          (see the public docs index)
├── src/
│   ├── v3-core/                   ← engine package
│   │   ├── README.md
│   │   ├── pyproject.toml
│   │   ├── LICENSE                ← AGPL-3.0-or-later
│   │   ├── schema/
│   │   ├── src/v3core/
│   │   ├── tests/
│   └── v3-hermes-plugin/          ← Hermes adapter
│       ├── README.md
│       ├── plugin.yaml
│       ├── pyproject.toml
│       ├── LICENSE                ← AGPL-3.0-or-later
│       └── src/v3hermes/
└── examples/
    ├── config.example.yaml
    └── .env.example
```

## 3. Filing issues

Use the project's issue tracker. A good issue for the alpha surface:

- **Title:** one line, what broke (not "v3 broken").
- **Environment:** OS, Python version, `pip show v3-core v3-hermes-plugin`
  output (versions + locations), `pgvector` image tag.
- **What you ran:** paste the exact commands.
- **What you expected:** one sentence, citing the supported-surface doc
  you're relying on.
- **What you got:** actual output, exit codes, log excerpts.
- **Configuration:** redact secrets. Paste the **shape** of your
  `config.yaml` (key names + example values), not the contents.

> If you suspect a security issue, follow [`SECURITY.md`](SECURITY.md)
> instead of opening a public issue.

## 4. Contribution scope for the public-alpha cut

The alpha cut is **not** accepting broad code contributions. Specifically:

- **In scope** for issues / discussion: reproduction reports against the
  supported surface in
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md).
- **Out of scope** for the alpha cut:
  - New features (Recall V2, multi-writer, multi-agent, etc.).
  - Large refactors (changing the clean-boundary writer,
    changing the schema artifact, changing the plugin manifest kind,
    changing the license).
  - Historical data migration into the supported surface.
  - Cosmetic-only edits to the engine.

If you want to propose a new feature, file a GitHub issue marked
`feature-request`. New features are not part of the v0.1-alpha
supported-surface promise.

## 5. Coding conventions (for small fixes only)

If you're patching a typo in a doc or a one-line bug in the supported
surface:

- **License headers:** every source file under `src/v3-core/src/v3core/`
  and `src/v3-hermes-plugin/src/v3hermes/` already declares
  `AGPL-3.0-or-later`. Do not change that.
- **No new dependencies** without an entry in the relevant
  `pyproject.toml` and a "standard install verification" note in the PR.
- **No secrets** in any committed file (see [`SECURITY.md`](SECURITY.md)).
- **Path style:** absolute paths in any config-style file; the engine
  does not expand `~` on Windows.
- **Run `v3-core info`** locally against your disposable PG before opening
  the PR.

## 6. What this file does NOT promise

- It does not promise a PR review SLA.
- It does not promise that an alpha-cut issue will be fixed in a later
  alpha cut.
- It does not promise a roadmap for post-alpha features. Those are
  scoped in
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
  and
  [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md).

---

## 7. Canonical development repository

This repository is the **single canonical** location for normal
Hippocampus development, releases, and PyPI publication. Specifically:

- Normal runtime, packaging, tests, docs, Hermes adapter, evaluator,
  and other day-to-day work happens here, and is contributed through
  PRs against this repository.
- CI runs here. The product CI workflow
  (`.github/workflows/product-ci.yml`) and the packaging smoke
  workflow (`.github/workflows/distribution-packaging-smoke.yml`)
  execute against this repository on every pull request.
- Releases and PyPI artifacts are prepared from this repository.
- Issues and discussions about the supported surface
  (see [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md))
  belong here.

If you want to propose a change that affects the supported surface —
new schema, new tool, new provider, new packaging artifact, change to
the README / public docs, change to the test matrix — open an issue
or PR **here** and follow the rest of this file (issue template, PR
template, license headers, no secrets, etc.).

