# I01 host-format fixtures — provenance

All fixtures are **real formats** produced by the actual host tooling or
exported from a live install. Nothing here is a hand-designed "looks like
DSH/pi" JSON.

## dsh/ — real DSH v4 sessions (`session.v4.jsonl.zstd`)

- Produced by: DSH `0.2.0-rc.2` (npm `@deepseek-ai/dsh@0.2.0-rc.2`,
  upstream master `639ed015397290b3745d163aafe02ffee4aa3f84`) running the
  real CLI on the Y400 test machine (B04 integration run, 2026-10-02).
- Layout mirrors the real on-disk layout:
  `<root>/--<normalized-cwd>--/<session-id>/session.v4.jsonl.zstd`
- Physical format: concatenated zstd frames wrapping JSONL — header line
  `{"type":"session","version":4,…}` + one JSON event per line.
- `session-60c82e3f-…`: single turn, no Hippocampus injection.
- `session-9a48a9ec-…`: two turns; contains a real Hippocampus injection
  as `user/message` with `source.kind="hippocampus"` (must be excluded),
  plus `runtime-context` / `skill-catalog` injected user messages (excluded).
- SHA-256:
  - `session-60c82e3f…/session.v4.jsonl.zstd` = `2180d8607712cf5175af02bda221ae85473458c1ac45a13e6b9c061dfe34ba43`
  - `session-9a48a9ec…/session.v4.jsonl.zstd` = `f5ca000e1981381aad8e6d90332ddfacf708e266b92a6f7fae74b3176f585efe`

## pi/ — real pi v1.0.0 session (`.jsonl`)

- Produced by: `@earendil-works/pi-coding-agent@1.0.0`
  (tag `v1.0.0` = commit `a13d35a742c6ef8462812a28fbe1d8c8b7431c32`) via the
  official SDK (`SessionManager.create(cwd, customDir)` + `appendMessage`),
  run with a local Node.js — no model call involved.
- Layout mirrors the real layout: `<sessions>/--<escaped-cwd>--/<file>.jsonl`
- Contains: 3 user/assistant turns, one `custom_message` with
  `customType:"hippocampus-memory"` (must be excluded), one plain `custom`
  entry (must be skipped), and a branch-free `parentId` chain.
- SHA-256: `c50b2ca4a6ee33a8e373a2b5cbe9e8e8af4c4d97427600e97cb0ef1bcc45b4a9`

Regenerate with `node` + the SDK (see `docs/I01-HISTORY-IMPORT-DESIGN.md` §6).

## hermes/ — synthetic `state.db` on the real schema

- Produced by: `make_fixture.py` (this directory). Schema introspected from
  a live Hermes store (2026-10-03); rows are synthetic and cover every
  exclusion class: compaction summaries, system notes, tool rows, empty
  content, `display_kind` in {hidden, auto_continue,
  async_delegation_complete, process_complete, model_switch, failed_turn},
  plus kept rows (steer, archived `active=0/compacted=1`, folded `0/0`).
- Regenerate: `python make_fixture.py`
- SHA-256: `aeae29fd73039e4245cea813675ade08c1915de81f8433ade5aadf43b3f7a2de`
