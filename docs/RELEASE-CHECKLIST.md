# Release Checklist — v3 Memory Plugin

> What must be true before a stable public release. Each item is
> grouped by area. The list mixes the
> **public-alpha gates** the current candidate already satisfies on
> the declared surface (Section A–E) with the **POST-ALPHA / STABLE
> readiness items** the alpha does **not** close by itself (Section
> P–S). Data safety and privacy/security gates are **not** weakened
> for the alpha — they remain hard gates for any release tag.
>
> Status legend used in this doc:
>
> - **CLOSED on declared surface** — the row in
>   [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
>   § 2 is PASS / EVIDENCE for the declared surface, and the gate's
>   corresponding area here is satisfied for that surface.
> - **OPEN (POST-ALPHA / STABLE)** — a non-blocking-for-alpha item
>   that the public-alpha tag does **not** close and that a stable
>   release must close before cutting a stable tag.
> - **OPEN (BLOCKING)** — a hard pre-tag gate that any release tag
>   (alpha or stable) must close before the tag is cut.
>
> Items that affect the supported surface or the public cut are
> recorded inline; design internals are not part of the public
> contract.

---

## A. Code readiness — public-alpha gates

- [x] **Focused acceptance scope on this HEAD** — **191 passed** when
      run file-by-file to avoid known order contamination. This does not
      claim full `pytest tests/` green. ([`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
      § 4)
- [x] **Conversation-stream / QA / LiveBuffer contract** — fresh export
      stranger smoke produced `conversation_stream` = 2; one direct
      `sync_turn` did not flush QA. QA pairing, retry, and restart-marker
      behavior passed in the focused ingest files. ([`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
      § 2.2)

## B. Documentation readiness — public-alpha gates

- [x] All documents in `docs/` cross-link to each other. The
      hardening pass added the initial set; verify nothing is
      orphaned before tag.
- [x] No document claims "verified" / "PASS" without an evidence
      pointer in [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
      § 2.
- [x] `README.md` does not name a production PG, production endpoint,
      production port, or any credential.
- [x] No document references personal absolute paths outside of
      placeholder templates.
- [x] No document references internal progress / lab notes that are
      out of scope for the public alpha cut.

## C. Security / privacy readiness — public-alpha gates

- [x] **No real credentials, no personal absolute paths, no production
      endpoints** in any committed file in this repo. Verify with:
      ```bash
      git grep -nE '(BEGIN RSA|sk-[A-Za-z0-9]{20,}|api[_-]?key|password|secret)' \
        -- ':!src/*/tests/**' ':!docs/archive/**' ':!*.dump'
      ```
      The exact list of `git grep` patterns must be reviewed for the
      final tag — the above is a starting point, not a complete policy.
- [x] **No fabricated security contact** — `SECURITY.md` does not
      promise a private channel that does not exist.
- [x] **Clean-history export** built and recorded — see
      [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
      § 5 "Privacy / security" row; the clean-export builder
      (`tools/build_public_export.py`) is part of the additive tooling
      in this candidate, **not** part of the public export.
- [x] **License consistency** — both subpackages declare
      `AGPL-3.0-or-later` in their `pyproject.toml` and ship a `LICENSE`
      file. The monorepo does not introduce a new top-level license.

## D. Install / upgrade / rollback readiness — public-alpha gates

- [x] **Fresh-install bring-up recipe** documented in
      [`docs/INSTALL.md`](INSTALL.md) and verified end-to-end on a
      clean disposable Windows 10 / Python 3.11.16 /
      `pgvector/pgvector:pg17` environment (the supported-surface
      evidence window). See
      [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
      § 1 + § 2.1.
- [x] **`pyahocorasick` Windows build story** documented for users
      without MSVC. See [`docs/KNOWN-LIMITATIONS.md`](KNOWN-LIMITATIONS.md) § 1.1.
- [x] **Upgrade path from a previous alpha tag** — N/A for the first
      public-alpha tag.

## E. Backup / restore readiness — public-alpha gates

- [x] **Dump/restore recipe** in
      [`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md) reproduced
      end-to-end against an empty disposable pgvector/pg17 container
      in the fresh export clean-history export stranger smoke (`pg_dump -Fc`
      self-contained; `pg_restore` reproduced table presence + row counts
      `raw` = 2, `QA` = 0, `explicit` = 1; post-restore write +
      keyword recall proof). See
      [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
      § 2.5.
- [x] **Schema-artifact story** (`src/v3-core/schema/explicit_memories.sql`
      + `src/v3-core/schema/alpha_bootstrap.sql`) applied by the
      explicit `bootstrap_alpha_db.py` CLI in
      [`docs/INSTALL.md`](INSTALL.md) § 6.

## P. POST-ALPHA / STABLE — code readiness

- [ ] **Independent-user fresh-install matrix** declared and run on
      multiple OS / Python combinations. The alpha evidence is one
      lab run on one environment; the stable release needs a
      broader matrix. ([`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
      § 5 "Installability")
- [x] **Focused preservation file re-validation** — the six source-ingest
      preservation files were run independently on this candidate to avoid
      known order contamination; all passed (9 + 4 + 13 + 19 + 15 + 12).
      `test_import_hermes_state.py` also passed 3 tests. The candidate
      records 191 focused passes in total. This is not a full-suite claim.
      ([`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
      § 4)
- [ ] **`pytest tests/` policy** — are the focused preservation
      files expected to be re-run on every PR, or only on candidate
      tags? Whatever the policy is, write it down somewhere users
      can find it.

## Q. POST-ALPHA / STABLE — documentation readiness

- [ ] **Repository visibility decision** made and recorded. Options:
  1. Publish a clean-history mirror as the public repo (alpha path).
  2. Rewrite private history (only if a concrete benefit is documented).
- [ ] **Tag object + peeled SHA + manifest** recorded for the public
      release. The intended convention is:
  - tag = `release/public-alpha-<YYYYMMDD>`
  - peeled SHA verified via `git rev-parse <tag>^{commit}`
  - release manifest under `docs/release-manifest-<tag>.md`
- [ ] **Release notes** drafted. The `CHANGELOG.md` is the user-facing
      entry; the release manifest is the internal record.

## R. POST-ALPHA / STABLE — security / privacy readiness

- [ ] **Credential rotation / invalidation receipt** for any
      credentials historically present in this repo, executed at the
      source provider. The pre-tag checklist records this as
      required before the public release; the alpha evidence does
      not include an actual rotation receipt — only the documented
      guidance in [`SECURITY.md`](../SECURITY.md) § 3 and
      [`docs/PRIVACY-DATA-FLOW.md`](PRIVACY-DATA-FLOW.md) § 8. Until
      the rotation is recorded, this row remains OPEN. This is **not**
      weakened for the alpha — a stable tag still requires it.

## S. POST-ALPHA / STABLE — install / backup readiness

- [ ] **Rollback package** documented. A staged rollback package is
      the historical reference; adapt it for the public-alpha
      candidate.
- [ ] **Role/grant story** consistent between source and target PG.
      Lab used `--no-owner`; production must decide.
- [ ] **Schema-artifact migration plan** (separate from the
      explicit bootstrap CLI) documented for ops.
- [ ] **Independent third-party fresh-install proof** on a
      third-party disposable PG, with the post-restore write + recall
      proof verified by someone other than the original lab author.

## T. POST-ALPHA / STABLE — tool surface stability

- [ ] `v3-core info` exit code is documented (0 = ok, 1 = pg fail, etc.).
- [ ] `v3_health` JSON shape is documented and stable enough to be a
      contract.
- [ ] `examples/config.example.yaml` round-trips: copy → fill →
      `v3-core info` reports `pg: OK`.
- [ ] `examples/.env.example` does not contain any real-looking
      credential.
- [ ] `examples/config.example.yaml` does not name any real provider
      — placeholder values only.

## U. What this checklist does NOT include

- Any new feature work (Recall V2, multi-writer, multi-agent).
- Any migration of historical data into the supported surface.
- Any production deployment to a customer-facing endpoint.
- Any change to the license (AGPL-3.0-or-later stays).
- Any change to the supported-surface scope in
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
  beyond what the next round of work explicitly adds.

## V. Cross-references

- [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
- [`docs/INSTALL.md`](INSTALL.md)
- [`docs/BACKUP-RESTORE.md`](BACKUP-RESTORE.md)
- [`docs/PRIVACY-DATA-FLOW.md`](PRIVACY-DATA-FLOW.md)
- [`docs/KNOWN-LIMITATIONS.md`](KNOWN-LIMITATIONS.md)
- [`docs/ARCHITECTURE-OVERVIEW.md`](ARCHITECTURE-OVERVIEW.md)
- [`SECURITY.md`](../SECURITY.md)
