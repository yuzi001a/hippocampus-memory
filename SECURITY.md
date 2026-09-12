# Security — Hippocampus v0.1-alpha

> **Scope:** disclosure policy + the security-relevant facts a user or
> reviewer needs to know about the public-alpha release. It does **not**
> enumerate every code path; for that see
> [`docs/PRIVACY-DATA-FLOW.md`](docs/PRIVACY-DATA-FLOW.md).

---

## 1. Reporting a vulnerability

**Do not open a public issue for security-sensitive reports.**

This preview does not publish a verified private security reporting
channel. Do not paste credentials, secrets, DSNs, or real user data into a
public report (see § 3 for what to redact). Non-sensitive reproducible
issues may use the project's public issue tracker; security-sensitive
details must be withheld until a verified private channel is available.

In your report:

- Describe the vulnerability and the impact (data exposure, RCE, etc.).
- Include a minimal reproduction (commands, config shape — redact
  secrets).
- If known, suggest a fix or workaround.
- Allow reasonable time for a response before public disclosure.

> This document deliberately does **not** promise a private contact
> channel, response SLA, or coordinated-disclosure timeline. It also
> does **not** promise a bug-bounty program. See § 7.

## 2. Supported versions

Only the public-alpha release identified by the reviewed export
manifest is in scope for this preview. Historical development lines may
not be patched. This document does not promise a long-term support branch
or maintenance window.

## 3. Handling secrets

The public tree is intended to contain no real credentials. If you
discover a credential in a checkout, log, issue, or configuration:

1. **Do not publish it.** Treat it as compromised by default.
2. **Rotate or invalidate it at the source provider** (PG, embedding
   API, LLM API, rerank API, or another service). The plugin's
   `requires_env: [V3CORE_PG_PASSWORD]` does not auto-rotate secrets.
3. **Do not paste it into a public issue or chat.** This repository
   does not promise a private security channel; withhold sensitive
   details until a verified private channel is available.
4. Remove the secret from local logs and temporary files after the
   provider-side disposition is complete.

## 4. Threat-model boundaries

For the alpha surface, the in-scope threats and how the engine handles
them are:

| Threat | In scope? | Where it's addressed (code-grounded) |
|---|---|---|
| Credentials in plaintext on disk | Yes | `docs/CONFIGURATION.md` § "Environment variables"; env vars take precedence over `config.yaml`. The plugin manifest (`src/v3-hermes-plugin/plugin.yaml`) declares `requires_env: [V3CORE_PG_PASSWORD]`, so the plugin load is gated on that env var under hosts that enforce `requires_env`. |
| Provider endpoint typo leaking data to the wrong host | Partial | User-config endpoint; no automatic allowlist. Double-check URLs. |
| Log leakage of DSN / prompt / API key | Yes | The engine's `_safe_err` helpers (`src/v3-core/src/v3core/embedding.py`, `src/v3-core/src/v3core/llm.py`) sanitize common leak sites; `v3_health` redacts provider errors. Do not rely on a specific coverage number — redact secrets yourself before sharing logs. |
| Memory contents leaking between users / profiles | Partial | Per-profile `data_dir` separation; per-profile PG database is the user's responsibility. |
| Network exfiltration by the engine itself | Yes | This doc + the `requires_env` plugin manifest; see also `docs/PRIVACY-DATA-FLOW.md`. The engine does not open listening sockets; outbound traffic is limited to the providers you configured plus PG. |
| Long-term identity (E1 yin) carrying private data to LLM | Yes | The E1 prompt is configurable; if your LLM is a third party, treat E1 prompts as data you send. |

Out of scope for this doc (but worth being aware of):

- Side-channel attacks against your PostgreSQL host (configure PG TLS
  yourself).
- Denial-of-service against your embedding / LLM provider (rate-limit
  your own keys).
- Physical access to the host where v3-core runs.

## 5. Hardening checklist (per trial environment)

- [ ] PG access is restricted to the disposable container (no public bind).
- [ ] `V3CORE_PG_PASSWORD` (and any other secrets) is in an env file that
      is `chmod 600` / ACL'd to your user only.
- [ ] `config.yaml` and `.env` are not committed to the repo.
- [ ] The provider `endpoint` URLs are exactly what you intended — verify
      character-by-character; a typo is the easiest way to leak data to
      the wrong host.
- [ ] Logs from the engine are not piped to a public log aggregator.
- [ ] If you used a public Docker image for `pgvector`, pin the tag
      (e.g. `pgvector/pgvector:pg17`) — not `:latest`.

## 6. Disclosure timeline

This alpha does not publish a private security channel, response SLA,
or coordinated-disclosure timeline. Do not post security-sensitive
details publicly; use a verified private channel only if one is made
available separately.

## 7. What this file does NOT cover

- Bug-bounty program (none at this time).
- SLA for fixes during the alpha cut (best-effort).
- Compliance with specific regulations (GDPR, HIPAA, etc.) — that is the
  deployer's responsibility, not the engine's.
- A private security contact channel (none advertised; see § 1).
