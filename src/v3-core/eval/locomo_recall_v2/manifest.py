"""LoCoMo Recall v2 manifest builder (G6C-A).

Builds a JSON-serializable manifest that records the source
identity, the row counts and row hashes, the provider/model
fingerprint, and the answer-coverage metrics. The manifest is
the deterministic contract the parent adapter uses to confirm
"this is the dataset we ran against".

The manifest is purely structural. It does NOT carry secrets:

  * No DSN, no password, no API key, no raw endpoint URL.
  * ``provider_id`` and ``model_id`` are short opaque strings
    (``bge-m3``, ``text-embedding-3-large``, …). Embedding
    dimension is the bare integer.
  * ``commit_sha`` is the 40-char hex digest when supplied.
    Empty string means "not provided by caller" — never
    invent one.

Two utility helpers:

  * :func:`verify_source` — SHA-256 source verification; same
    contract as :func:`dataset.verify_source` for symmetry, but
    kept here so manifest builders don't have to import the
    loader internals.
  * :func:`stable_hash` — deterministic SHA-256 over a JSON
    payload with ``sort_keys=True`` so two structurally identical
    dicts produce the same digest.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from typing import Any

# Re-export the SHA-256 verifier from :mod:`dataset` so callers
# can import a single helper namespace.
from .dataset import (  # noqa: F401  (re-export)
    sha256_file,
    verify_source,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Builder version. Bump when the manifest schema changes.
MANIFEST_BUILDER_VERSION = "locomo-recall-v2-manifest-0.1.0-dev"

#: 40-char git SHA-1 hex digest.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ManifestError(ValueError):
    """Raised when manifest construction receives an invalid payload."""


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class LoCoMoManifest:
    """JSON-serializable manifest for one LoCoMo Recall v2 build.

    Field semantics:

      * ``source``: source identity + bytes + sha256.
      * ``counts``: sample / session / message / QA-pair /
        eval-row totals.
      * ``category_counts``: per-category eval row counts.
      * ``answer_coverage``: per-eval-row answer coverage
        (non-empty, non-whitespace).
      * ``builder_version``: schema version string.
      * ``commit_sha``: 40-char hex digest when supplied; "" otherwise.
      * ``provider_id`` / ``model_id``: short opaque strings.
      * ``embedding_dim``: bare integer (e.g. 1024).
    """

    source: dict[str, Any]
    counts: dict[str, int]
    category_counts: dict[str, int]
    answer_coverage: dict[str, int]
    builder_version: str
    row_hashes: dict[str, str]
    commit_sha: str
    provider_id: str
    model_id: str
    embedding_dim: int

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def stable_hash(payload: Any) -> str:
    """Return the SHA-256 of a payload's canonical JSON form.

    The canonical form is:

      * ``sort_keys=True`` so key order doesn't matter.
      * ``ensure_ascii=False`` so non-ASCII characters survive
        (we're not computing a cryptographic signature; the
        digest is for stability, not security).
      * ``separators=(",", ":")`` to strip whitespace — two
        payloads that differ only in formatting still hash
        the same.

    Returns a 64-char lower-hex digest.
    """

    blob = json.dumps(
        payload, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _coerce_commit_sha(raw: str | None) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ManifestError(
            f"commit_sha must be a string when provided (got {type(raw).__name__})"
        )
    s = raw.strip().lower()
    if not s:
        return ""
    if not _COMMIT_SHA_RE.match(s):
        raise ManifestError(
            f"commit_sha must be a 40-char hex digest or empty (got {raw!r})"
        )
    return s


def _coerce_provider(provider_id: str | None) -> str:
    if provider_id is None:
        return ""
    if not isinstance(provider_id, str):
        raise ManifestError(
            f"provider_id must be a string (got {type(provider_id).__name__})"
        )
    s = provider_id.strip()
    # Refuse anything that looks like a URL with credentials.
    if "://" in s or "@" in s:
        raise ManifestError(
            "provider_id must not contain URL/credential-like characters"
        )
    return s


def _coerce_model_id(model_id: str | None) -> str:
    if model_id is None:
        return ""
    if not isinstance(model_id, str):
        raise ManifestError(
            f"model_id must be a string (got {type(model_id).__name__})"
        )
    s = model_id.strip()
    if "://" in s or "@" in s:
        raise ManifestError(
            "model_id must not contain URL/credential-like characters"
        )
    return s


def _coerce_embedding_dim(embedding_dim: int | None) -> int:
    if embedding_dim is None:
        return 0
    if not isinstance(embedding_dim, int) or isinstance(embedding_dim, bool):
        raise ManifestError(
            f"embedding_dim must be an int (got {type(embedding_dim).__name__})"
        )
    if embedding_dim < 0:
        raise ManifestError(f"embedding_dim must be non-negative (got {embedding_dim})")
    return embedding_dim


def _answer_coverage(rows: list[Any]) -> dict[str, int]:
    """Compute answer-coverage counters for one eval row list.

    Categories:

      * ``total`` — total eval rows.
      * ``non_empty`` — rows whose ``answer`` is non-empty
        after stripping whitespace.
      * ``empty`` — rows with empty/whitespace-only answer.
    """

    total = 0
    non_empty = 0
    for r in rows:
        total += 1
        ans = getattr(r, "answer", "") or ""
        if isinstance(ans, str) and ans.strip():
            non_empty += 1
    return {
        "total": total,
        "non_empty": non_empty,
        "empty": total - non_empty,
    }


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_manifest(
    dataset,
    *,
    commit_sha: str | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    embedding_dim: int | None = None,
) -> LoCoMoManifest:
    """Build a deterministic manifest for a LoCoMo :class:`LoCoMoDataset`.

    Parameters
    ----------
    dataset:
        The :class:`eval.locomo_recall_v2.dataset.LoCoMoDataset`
        to summarise. Any object with ``source_path``,
        ``source_sha256``, ``source_bytes``, ``samples``,
        ``eval_rows``, ``qa_pairs``, ``category_counts``,
        ``message_count``, ``qa_pair_count`` works.
    commit_sha:
        Optional 40-char git SHA-1. Empty / None means
        "unknown — caller did not pass one".
    provider_id, model_id:
        Optional short opaque strings. URL/credential-like
        content is refused.
    embedding_dim:
        Optional bare integer. ``0`` means "not configured".

    Raises
    ------
    ManifestError
        When a forbidden value is passed.

    Returns
    -------
    LoCoMoManifest
        A dataclass that round-trips through ``json.dumps`` /
        ``json.loads``.
    """

    commit = _coerce_commit_sha(commit_sha)
    pid = _coerce_provider(provider_id)
    mid = _coerce_model_id(model_id)
    edim = _coerce_embedding_dim(embedding_dim)

    source = {
        "name": "locomo",
        "kind": "eval_v2",
        "path": os.path.abspath(getattr(dataset, "source_path", "")),
        "sha256": getattr(dataset, "source_sha256", ""),
        "bytes": int(getattr(dataset, "source_bytes", 0)),
    }

    # Per-row hashes: keyed by ``(sample_id, qa_id)`` so two
    # builds with the same source agree row-by-row.
    row_hashes: dict[str, str] = {}
    for row in sorted(
        getattr(dataset, "eval_rows", []),
        key=lambda r: (getattr(r, "sample_id", ""), getattr(r, "qa_id", "")),
    ):
        sid = getattr(row, "sample_id", "")
        qid = getattr(row, "qa_id", "")
        row_hashes[f"{sid}::{qid}"] = getattr(row, "source_hash", "")

    samples = getattr(dataset, "samples", ())
    counts = {
        "sample_count": len(samples),
        "session_count": sum(
            len(getattr(s, "sessions", ())) for _sid, s in samples
        ),
        "message_count": int(getattr(dataset, "message_count", 0)),
        "qa_pair_count": int(getattr(dataset, "qa_pair_count", 0)),
        "eval_row_count": len(getattr(dataset, "eval_rows", ())),
    }

    cat_counts: dict[str, int] = dict(getattr(dataset, "category_counts", {}) or {})

    coverage = _answer_coverage(list(getattr(dataset, "eval_rows", [])))

    return LoCoMoManifest(
        source=source,
        counts=counts,
        category_counts=dict(sorted(cat_counts.items())),
        answer_coverage=coverage,
        builder_version=MANIFEST_BUILDER_VERSION,
        row_hashes=row_hashes,
        commit_sha=commit,
        provider_id=pid,
        model_id=mid,
        embedding_dim=edim,
    )


__all__ = [
    "MANIFEST_BUILDER_VERSION",
    "LoCoMoManifest",
    "ManifestError",
    "build_manifest",
    "sha256_file",
    "stable_hash",
    "verify_source",
]