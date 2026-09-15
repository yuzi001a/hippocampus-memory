"""LoCoMo Recall v2 test suite (G6C-A).

Tests cover:

  * ``test_dataset.py`` — loader identity, provenance, evidence
    mapping, no-truncation, helper determinism.
  * ``test_manifest.py`` — manifest source hash, row hashes,
    commit_sha / provider_id / model_id / embedding_dim
    contracts, secret-leak refusal.
  * ``test_lab.py`` — reserved-port 5433 + non-loopback DSN
    refusal, static schema safety (no DROP/TRUNCATE/DELETE),
    import_rows SQL shape with a fake connection.
  * ``test_compare.py`` — JSONL case-ID comparison, missing/
    extra/unchanged cases, fail-closed on malformed lines.

Tests must not require PG or any provider. psycopg2 is mocked
out where the lab SQL shape is exercised.
"""
from __future__ import annotations

__all__: list[str] = []