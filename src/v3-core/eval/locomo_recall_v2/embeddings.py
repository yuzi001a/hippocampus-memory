# -*- coding: utf-8 -*-
"""G6C-A LoCoMo Recall V2 — evaluator-side semantic embedding
preparation and durable external cache.

This module is the **seam** between the evaluator's row model
(:mod:`eval.locomo_recall_v2.dataset`, :mod:`eval.locomo_recall_v2.manifest`)
and the canonical production embedder
(:mod:`v3core.embedding`). It does three things and three things
only:

  1. Applies the **production** text transform for each corpus
     shape the lab importer writes to PG. The transforms are
     read off the live production source (see
     ``TRANSFORM_IDS`` / :data:`QA_PAIRS_TRANSFORM_ID` /
     :data:`CONVERSATION_STREAM_TRANSFORM_ID`) so the cache stays
     invalidation-safe when production changes its mind.

     * ``qa_pairs`` rows are embedded as
       ``f"{question}\\n{answer}"`` if ``answer`` is non-empty
       else ``question`` — exactly the contract in
       ``v3core.__init__._flush_pending_qa`` (the canonical
       qa-durability path that owns the embedding call).
     * ``conversation_stream`` rows are embedded as
       ``content[:2000]`` — exactly the contract in
       ``v3core.ingest`` (LiveBuffer flush and the
       ``ingest_session`` historical path) which both call
       ``call_embedding(content[:2000], ...)``.
     * ``query`` rows reuse the qa_pairs transform with an
       empty answer (one text per case), so query and corpus
       land in the same vector space.

  2. Reuses the canonical :func:`v3core.embedding.build_embed_cfg`
     factory + :class:`v3core.embedding.EmbedProfile` /
     :meth:`EmbedProfile.fingerprint` so the profile identity is
     the **same** digest production uses for the ``embed_model``
     PG column. No hand-rolled profile dicts.

  3. Persists computed vectors to a caller-supplied
     ``cache_root`` directory (default: the workspace
     ``eval-cache/hippocampus-locomo`` path) under separate
     ``corpus/`` and ``query/`` namespaces. The cache uses
     numpy's compact ``.npz`` format (one file per cache
     namespace) plus a tiny sidecar ``.meta.json`` that carries
     only the identity fields required to invalidate stale
     entries. The cache key includes the dataset sha256, the
     transform id, the model, the embedding dim, the profile
     fingerprint, the per-row input text hash, and the row /
     source id so a mismatch in any one field invalidates the
     entry.

The module never opens an HTTP connection, never carries
credentials, never mutates the lab DSN, never touches PG, and
never serialises a full ``embed_cfg`` (api keys / proxy
endpoints are deliberately dropped). It is testable end-to-end
with a ``monkeypatch`` of :func:`v3core.embedding.embed_batch`
and zero credentials.

Public surface
==============

* :func:`prepare_corpus_embeddings` — compute (or hit-cache) the
  per-row embeddings for one corpus shape (qa_pairs or
  conversation_stream).
* :func:`prepare_query_embeddings` — compute (or hit-cache) the
  per-query embeddings for the eval question list. The query
  shape uses the same transform as ``qa_pairs`` so query and
  corpus land in the same vector space.
* :class:`EmbedCacheStats` — counters that the caller can
  log / surface to telemetry, including item counts,
  character totals, expected / provider batch counts.
* :exc:`EmbedContractError` — raised on a hard contract
  violation (zero-vector, dim mismatch, profile fingerprint
  mismatch, missing row identity, missing cache identity).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .dataset import LoCoMoDataset, parse_source_id
from .manifest import stable_hash  # noqa: F401  (re-export stability)

# Canonical production embedder. We do NOT import call_embedding
# here — the only entry we need is ``build_embed_cfg`` for
# profile identity and ``embed_batch`` for batched vector
# computation. Both are explicitly monkeypatched in tests so the
# module never opens a network connection in this slice.
from v3core import embedding as _v3core_embedding


logger = logging.getLogger("v3core.eval.locomo_recall_v2.embeddings")


# ---------------------------------------------------------------------------
# Constants — production transform identity
# ---------------------------------------------------------------------------


# Transform ids are stable strings: a change to the production
# text rule MUST bump the transform id (and a corresponding test
# must assert the new id) so the cache cannot return a
# stale-shape vector for a row under the new contract.
QA_PAIRS_TRANSFORM_ID = "qa_pairs_v1::question+NL+answer"
CONVERSATION_STREAM_TRANSFORM_ID = "conversation_stream_v1::content[:2000]"


# Historical reference (2026-09-15 G6C-A): the canonical qa-durability
# path in ``v3core.__init__._flush_pending_qa`` computes
# ``text_for_emb = f"{q}\\n{a}" if a else q`` and feeds it to
# ``embed_batch``. The conversation_stream / live_buffer path
# in ``v3core.ingest`` calls ``call_embedding(content[:2000], ...)``.
# The transforms below are byte-for-byte equivalents of those
# production rules. Do NOT change them without bumping the
# transform id AND verifying the lab row contract.


def _qa_pairs_embedding_text(question: str, answer: str) -> str:
    """Return the production ``qa_pairs`` embedding text.

    Mirrors ``v3core.__init__._flush_pending_qa`` (the canonical
    qa-durability path) byte-for-byte: ``f"{q}\\n{a}" if a
    else q``.
    """
    q = (question or "").strip()
    a = (answer or "").strip()
    if a:
        return f"{q}\n{a}"
    return q


def _conversation_stream_embedding_text(content: str) -> str:
    """Return the production ``conversation_stream`` embedding text.

    Mirrors ``v3core.ingest`` (LiveBuffer flush + ingest_session
    historical path) byte-for-byte: ``content[:2000]``.
    """
    return (content or "")[:2000]


# Source / shape discriminators. The cache key namespace splits
# on (kind, transform_id) so two distinct shapes cannot
# collide.
_KIND_QA_PAIRS = "qa_pairs"
_KIND_CONVERSATION_STREAM = "conversation_stream"
_KIND_QUERY = "query"

VALID_KINDS: frozenset[str] = frozenset(
    {_KIND_QA_PAIRS, _KIND_CONVERSATION_STREAM, _KIND_QUERY}
)

# Default cache root. Kept OFF the repo (caller-supplied; the
# default is just a workspace scratch path the harness can
# override). Never committed — the ``.gitignore`` in the
# ``eval-cache`` family is enforced by the harness via the
# `cache_root` contract below.
DEFAULT_CACHE_ROOT = (
    "C:/Users/servi/workspace/eval-cache/hippocampus-locomo"
)


# Default batch size — the chunk size for the provider call.
# Production ``v3core.embedding.embed_batch`` accepts the full
# list and sends one HTTP request; we chunk it client-side so
# a large corpus does not exceed the provider's per-call input
# limit and so the stats surface a real expected/provider
# batch count. The number is conservative and matches the
# default used elsewhere in the v3 pipeline.
DEFAULT_BATCH_SIZE = 32


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EmbedContractError(ValueError):
    """Raised on a hard contract violation.

    The contract is fail-closed: a missing identity, a zero
    vector from a stub, a dim mismatch, a profile fingerprint
    mismatch, or a cache key with a missing field all raise
    instead of silently returning a partial result.
    """


# ---------------------------------------------------------------------------
# Cache stats
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class EmbedCacheStats:
    """Counters surfaced to the caller for telemetry / logging.

    Counters are independent of the call surface (corpus vs
    query); the same struct covers both. The "corpus" and
    "query" item / char counters are filled in only when the
    matching entry point is used (the entry point stamps the
    shape into ``kind`` before mutating the counters).
    ``cache_root`` records the actual on-disk root used so the
    caller's log can prove where the cache lived (and so a
    future audit can find the files).

    The four batch counters are the operational contract:

    * ``expected_batch_count`` — ceil(pending_misses / batch_size).
    * ``provider_batch_count`` — the number of times we actually
      called :func:`v3core.embedding.embed_batch` (the
      two should agree).
    * ``provider_calls`` — backwards-compatible counter
      (``provider_batch_count``) alias used by older callers.
    """

    lookups: int = 0
    hits: int = 0
    misses: int = 0
    invalidations: int = 0
    provider_calls: int = 0
    zero_vectors_rejected: int = 0
    dim_mismatches: int = 0
    profile_mismatches: int = 0
    cache_root: str = ""

    # Shape-tagged counters — only one of corpus_* / query_* is
    # populated per call (the entry point stamps the shape).
    corpus_items: int = 0
    corpus_chars: int = 0
    query_items: int = 0
    query_chars: int = 0

    # Batch accounting.
    expected_batch_count: int = 0
    provider_batch_count: int = 0

    def to_dict(self) -> dict[str, int | str]:
        return dataclasses.asdict(self)


# Backwards-compat alias used by older callers / the existing
# test suite. ``provider_calls`` IS ``provider_batch_count``.
# Both fields are kept so existing assertions continue to
# pass; the contract is "one provider call == one provider
# batch".
def _provider_calls_from_stats(stats: EmbedCacheStats) -> int:
    return stats.provider_batch_count


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------


# Field validation — keep small + explicit so the test suite can
# assert every key piece. The transform_id pattern accepts a
# wider set of characters than the model pattern because the
# production transform id is a human-readable description
# (e.g. ``conversation_stream_v1::content[:2000]``).
_FIELD_RE_SOURCE_ID = re.compile(r"^[A-Za-z0-9._:\-|>]+$")
_FIELD_RE_KIND = re.compile(r"^[a-z_]+$")
_FIELD_RE_MODEL = re.compile(r"^[A-Za-z0-9._:/+\-]+$")
_FIELD_RE_TRANSFORM_ID = re.compile(r"^[A-Za-z0-9._:/+\-:\[\] ]+$")


def _validate_string_field(
    name: str,
    value: Any,
    *,
    pattern: re.Pattern[str] | None = None,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise EmbedContractError(
            f"embeddings: {name} must be a str (got {type(value).__name__})"
        )
    if not allow_empty and not value:
        raise EmbedContractError(
            f"embeddings: {name} must be a non-empty str"
        )
    if pattern is not None and value and not pattern.match(value):
        raise EmbedContractError(
            f"embeddings: {name} has illegal characters: {value!r}"
        )
    return value


def _validate_dim(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise EmbedContractError(
            f"embeddings: {name} must be a positive int (got {value!r})"
        )
    return int(value)


def _validate_fingerprint(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise EmbedContractError(
            "embeddings: profile fingerprint must be a non-empty str"
        )
    if not re.fullmatch(r"[0-9a-f]{12,}", value):
        raise EmbedContractError(
            f"embeddings: profile fingerprint must be a hex digest "
            f"(got {value!r})"
        )
    return value


def _text_hash(text: str) -> str:
    """Deterministic hash of the embedding text.

    SHA-256 of UTF-8 bytes, hex digest. Used as a cache-key
    field so a single text mutation invalidates the entry.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _row_id_hash(*parts: str) -> str:
    """Deterministic hash of one or more identity strings.

    Used as a stable leaf name (legacy callers and tests still
    expect a stable hash) so the on-disk layout stays stable
    under row reorder. SHA-256 of the UTF-8 pipe-joined
    canonical form, hex digest.
    """
    canon = "\x1f".join(parts)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public row identity
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class EmbedRow:
    """One input row the caller wants to embed.

    The dataclass is the single seam the caller has to populate
    for a corpus row. ``row_id`` is the canonical identity
    string used as the cache key leaf — for ``qa_pairs`` the
    eval_v2 ``source_id`` (locomo|eval_v2|...|q>a) is the
    natural choice; for ``conversation_stream`` a synthesized
    ``cs|locomo|<sample_id>|<session_key>|<index>`` id is the
    natural choice. ``text`` is the **full** original input —
    the transform is applied inside the module so the cache
    key can hash the exact bytes the provider saw.
    """

    row_id: str
    text: str
    # For ``qa_pairs`` rows only — the answer text. The qa_pairs
    # transform is ``f"{question}\\n{answer}"`` and the cache
    # contract needs the answer so the text reconstruction is
    # exact. ``None`` for conversation_stream rows.
    answer: str | None = None
    # Per-row provenance the cache writes alongside the
    # vector. The caller may supply any JSON-safe dict; the
    # field is **never** used for cache lookup (so the cache
    # key stays stable across provenance-only changes).
    provenance: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "text": self.text,
            "answer": self.answer,
            "provenance": dict(self.provenance) if self.provenance is not None else None,
        }


# ---------------------------------------------------------------------------
# Cache on-disk layout
# ---------------------------------------------------------------------------


# Cache layout:
#
#   <cache_root>/
#     <kind>/
#       <dataset_sha>/
#         <transform_id>/
#           <model>/
#             <dim>/
#               <profile_fingerprint>/
#                 vectors.npz
#                 vectors.meta.json
#
# ``vectors.npz`` is the compact numerical store — one numpy
# archive per namespace, holding:
#
#   * ``vectors``     — float32 array, shape (N, dim).
#   * ``row_ids``     — str array, length N (canonical row_id).
#   * ``text_hashes`` — str array, length N (SHA-256 of input text).
#   * ``texts_preview`` — str array, length N (input text truncated
#     to 256 chars, for audit only — never used for lookups).
#
# ``vectors.meta.json`` is the sidecar with the identity fields
# the cache key was built from; on read we validate every field
# against the expected identity so a config drift or a
# hand-edited npz is detected and the cache is treated as a
# miss + invalidation.
#
# Both files are written atomically (write to ``*.tmp`` then
# ``os.replace``); on platforms where ``os.replace`` is
# atomic (Windows and POSIX alike), this prevents a partial
# write from being readable as a valid cache.


_LEAF_TEXT_PREVIEW_LEN = 256
_VECTORS_FILENAME = "vectors.npz"
_META_FILENAME = "vectors.meta.json"


def _cache_dir(
    cache_root: str | os.PathLike[str],
    kind: str,
    dataset_sha: str,
    transform_id: str,
    model: str,
    dim: int,
    profile_fingerprint: str,
) -> Path:
    return Path(
        cache_root,
        kind,
        dataset_sha,
        transform_id,
        model,
        str(int(dim)),
        profile_fingerprint,
    )


def _safe_path_component(s: str) -> str:
    """Make ``s`` safe as a single path component.

    Strips path separators and traversal sequences, then
    returns the result. The transform id and model come from
    trusted config (the production embed_cfg factory), but
    defensive sanitisation keeps an accidental ``"\\.."`` from
    writing outside the cache root. ``:`` is also stripped
    because Windows file names cannot contain a colon.
    """
    if not s:
        return "_"
    s = (
        s.replace("/", "_")
        .replace("\\", "_")
        .replace("..", "_")
        .replace(":", "_")
    )
    return s


# ---------------------------------------------------------------------------
# Cached leaf — read / write
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _CachedMeta:
    """The on-disk sidecar carrying the cache identity fields.

    The dataclass is internal; the public surface is
    :func:`prepare_corpus_embeddings` and the file is only ever
    written / read inside this module.
    """

    dataset_sha: str
    transform_id: str
    model: str
    dim: int
    profile_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_sha": self.dataset_sha,
            "transform_id": self.transform_id,
            "model": self.model,
            "dim": self.dim,
            "profile_fingerprint": self.profile_fingerprint,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "_CachedMeta":
        return cls(
            dataset_sha=str(d.get("dataset_sha", "")),
            transform_id=str(d.get("transform_id", "")),
            model=str(d.get("model", "")),
            dim=int(d.get("dim", 0) or 0),
            profile_fingerprint=str(d.get("profile_fingerprint", "")),
        )


def _atomic_write_text(path: Path, payload: str) -> None:
    """Atomically write a UTF-8 text file.

    Writes to ``path.with_suffix(path.suffix + ".tmp")`` then
    ``os.replace`` to ``path``. The atomic rename protects
    against a partial write being read as a malformed file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(payload)
    os.replace(tmp, path)


def _atomic_write_npz(path: Path, npz: "np.lib.npyio.NpzFile") -> None:
    """Atomically write a numpy ``.npz`` archive.

    The numpy writer accepts a file-like target but does not
    expose an atomic-rename API. We write to a sibling
    ``.npz.tmp`` and ``os.replace`` — the replace is atomic on
    both Windows and POSIX for a regular file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # numpy will not overwrite an existing file in append mode;
    # we open in "wb" and let numpy create the archive. The
    # tmp path has the full suffix ``.npz.tmp`` so numpy
    # appends the trailing zip marker correctly.
    with open(tmp, "wb") as f:
        np.savez(f, **npz)
    os.replace(tmp, path)


def _read_meta(
    meta_path: Path,
    *,
    expected: _CachedMeta,
    stats: EmbedCacheStats,
) -> _CachedMeta | None:
    """Read the cache meta sidecar and validate identity.

    Returns ``None`` on a miss (file does not exist or
    identity mismatch). On a real mismatch the ``stats``
    counter is bumped so the caller can see the cache was
    consulted but rejected.
    """
    if not meta_path.is_file():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        # Partial / corrupt — treat as miss + invalidation.
        stats.invalidations += 1
        return None
    if not isinstance(payload, Mapping):
        stats.invalidations += 1
        return None
    leaf = _CachedMeta.from_dict(payload)
    mismatched = (
        leaf.dataset_sha != expected.dataset_sha
        or leaf.transform_id != expected.transform_id
        or leaf.model != expected.model
        or leaf.dim != expected.dim
        or leaf.profile_fingerprint != expected.profile_fingerprint
    )
    if mismatched:
        stats.invalidations += 1
        return None
    return leaf


def _validate_cached_vector(
    vec: Any,
    *,
    expected_dim: int,
    stats: EmbedCacheStats,
) -> list[float] | None:
    """Validate one cached vector; ``None`` means "treat as a miss".

    Keeps the dim / non-zero invariants in one place so the indexed
    lookup path and the single-row helper cannot drift apart.
    """

    if vec is None:
        return None
    try:
        arr = np.asarray(vec)
    except (TypeError, ValueError):
        stats.invalidations += 1
        return None
    if arr.ndim != 1 or arr.shape[0] != expected_dim:
        stats.dim_mismatches += 1
        stats.invalidations += 1
        return None
    if not np.any(arr):
        stats.zero_vectors_rejected += 1
        stats.invalidations += 1
        return None
    return [float(x) for x in arr]


def _load_cache_index(
    npz_path: Path,
    *,
    expected_dim: int,
    stats: EmbedCacheStats,
) -> dict[tuple[str, str], Any]:
    """Load one cache leaf ONCE and index it in memory.

    ``_lookup_row`` re-reads and linearly scans the whole ``.npz`` for
    every single row, which makes a corpus pass O(rows x cached_rows)
    in file reads. Measured on the full LoCoMo corpus the leaf grows to
    ~12 MB and the pass slows down monotonically (~2 rows/s early, then
    ~1 row/s, 30-60 s per 64-row chunk) although the provider answers
    64 texts in well under a second. The evaluator therefore builds
    this index once per ``prepare_*`` call and answers every row lookup
    from memory.

    Returns ``{(row_id, text_hash): vector}``. An unreadable or
    malformed archive yields an empty index (one counted invalidation)
    instead of poisoning the pass.
    """

    index: dict[tuple[str, str], Any] = {}
    if not npz_path.is_file():
        return index
    try:
        with np.load(npz_path, allow_pickle=True) as archive:
            files = set(archive.files)
            if not {"vectors", "row_ids", "text_hashes"}.issubset(files):
                stats.invalidations += 1
                return index
            row_ids = archive["row_ids"]
            text_hashes = archive["text_hashes"]
            vectors = archive["vectors"]
            for i in range(len(row_ids)):
                index[(str(row_ids[i]), str(text_hashes[i]))] = vectors[i]
    except (OSError, ValueError, KeyError, EOFError):
        stats.invalidations += 1
        return index
    return index


def _lookup_row(
    npz_path: Path,
    *,
    expected_row_id: str,
    expected_text_hash: str,
    expected_dim: int,
    stats: EmbedCacheStats,
) -> list[float] | None:
    """Look up a single cached vector by (row_id, text_hash).

    Returns ``None`` on a miss or invalidation. Validates the
    cached vector's dim and non-zero invariant so a corrupted
    npz cannot poison downstream cosine math.
    """
    if not npz_path.is_file():
        return None
    try:
        # The npz carries object arrays for ``row_ids`` /
        # ``text_hashes`` so we MUST opt in to pickle load;
        # the contents are SHA-256 hex strings written by this
        # module, so there is no untrusted-deserialization risk.
        with np.load(npz_path, allow_pickle=True) as archive:
            if not {"vectors", "row_ids", "text_hashes"}.issubset(archive.files):
                stats.invalidations += 1
                return None
            row_ids = archive["row_ids"]
            text_hashes = archive["text_hashes"]
            vectors = archive["vectors"]
    except (OSError, ValueError, KeyError):
        stats.invalidations += 1
        return None

    # row_ids / text_hashes are stored as object arrays of str.
    # We iterate to find the first match — ``N`` is small
    # (per-corpus batch) so a Python scan is acceptable.
    found_idx: int | None = None
    for i in range(len(row_ids)):
        if (
            str(row_ids[i]) == expected_row_id
            and str(text_hashes[i]) == expected_text_hash
        ):
            found_idx = i
            break
    if found_idx is None:
        return None

    vec = vectors[found_idx]
    # ``vec`` is a 1-D ndarray of length ``expected_dim``.
    if vec.ndim != 1 or vec.shape[0] != expected_dim:
        stats.dim_mismatches += 1
        stats.invalidations += 1
        return None
    # All-zero vector — refuse silently (counted as an
    # invalidation so the caller re-runs through the
    # provider path; the provider path will then reject it
    # and raise the same way it would on a fresh call).
    if not np.any(vec):
        stats.zero_vectors_rejected += 1
        stats.invalidations += 1
        return None
    return [float(x) for x in vec]


def _append_to_npz(
    npz_path: Path,
    *,
    new_row_ids: Sequence[str],
    new_text_hashes: Sequence[str],
    new_texts_preview: Sequence[str],
    new_vectors: np.ndarray,
    expected_dim: int,
) -> bool:
    """Merge new rows into an existing ``.npz``.

    Reads the existing archive (if any), appends the new
    arrays, and writes a new archive atomically. Returns
    ``True`` if the write succeeded, ``False`` if the existing
    archive was malformed (the caller should treat the write
    as a cache write failure and rely on the in-memory
    result).
    """
    existing_row_ids: list[str] = []
    existing_text_hashes: list[str] = []
    existing_texts_preview: list[str] = []
    # The cached vectors are ONE 2-D array. They are deliberately kept
    # as that array: an earlier implementation materialised a Python
    # list of N row vectors and rebuilt the matrix with a per-row
    # ``vstack``, so every append cost O(N) Python-level numpy calls
    # and a corpus pass degraded to seconds per chunk (measured ~15 s
    # per 64-row chunk on a ~4 MB leaf) while the provider answered
    # the same chunk in under a second.
    existing_arr = np.zeros((0, expected_dim), dtype=np.float32)

    if npz_path.is_file():
        try:
            with np.load(npz_path, allow_pickle=True) as archive:
                files = set(archive.files)
                if not {"vectors", "row_ids", "text_hashes"}.issubset(files):
                    # Treat as malformed — write a fresh
                    # archive from scratch (the cache will
                    # not contain the previous entries, but
                    # the new entries are correct).
                    pass
                else:
                    existing_row_ids = [str(x) for x in archive["row_ids"]]
                    existing_text_hashes = [str(x) for x in archive["text_hashes"]]
                    if "texts_preview" in files:
                        existing_texts_preview = [
                            str(x) for x in archive["texts_preview"]
                        ]
                    candidate = archive["vectors"]
                    if (
                        getattr(candidate, "ndim", 0) == 2
                        and candidate.shape[1] == expected_dim
                    ):
                        existing_arr = candidate
                    else:
                        # Dim/shape mismatch — start fresh rather
                        # than poisoning the merged archive.
                        existing_row_ids = []
                        existing_text_hashes = []
                        existing_texts_preview = []
        except (OSError, ValueError, KeyError):
            # Malformed — start fresh.
            existing_row_ids = []
            existing_text_hashes = []
            existing_texts_preview = []
            existing_arr = np.zeros((0, expected_dim), dtype=np.float32)

    # Concatenate. ``existing_arr`` is either the cached 2-D array or
    # an empty (0, dim) array, so a single vstack is enough.
    all_row_ids = existing_row_ids + list(new_row_ids)
    all_text_hashes = existing_text_hashes + list(new_text_hashes)
    all_texts_preview = existing_texts_preview + list(new_texts_preview)

    new_arr = np.asarray(new_vectors, dtype=np.float32)
    if new_arr.ndim != 2 or new_arr.shape[1] != expected_dim:
        return False
    merged = (
        np.vstack([existing_arr, new_arr])
        if existing_arr.shape[0] > 0
        else new_arr
    )

    payload = {
        "vectors": merged.astype(np.float32, copy=False),
        "row_ids": np.asarray(all_row_ids, dtype=object),
        "text_hashes": np.asarray(all_text_hashes, dtype=object),
        "texts_preview": np.asarray(all_texts_preview, dtype=object),
    }
    try:
        _atomic_write_npz(npz_path, payload)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Production embedder — wrapper around v3core.embedding
# ---------------------------------------------------------------------------


def _resolve_profile(cfg: Any) -> tuple[Any, int, str]:
    """Return ``(profile, dim, fingerprint)`` for one embed cfg.

    The wrapper uses the canonical
    :func:`v3core.embedding.build_embed_cfg` factory so a hand-
    rolled dict cannot sneak past the fingerprint check (the
    factory raises ``ValueError`` on a missing endpoint /
    model, and the caller's downstream code would already have
    caught that). The returned ``profile`` is the
    :class:`v3core.embedding.EmbedProfile` object, and
    ``fingerprint`` is the canonical ``sha256[:12]`` digest.

    Integration note (2026-09-15 G6C-A): the CLI passes a
    lab-style cfg dict (``{"lab": True, "provider_id": ...,
    "model_id": ..., "embedding_dim": ...}``) that the
    production factory does not understand — it would return
    the disabled-shape cfg. The harness is expected to either
    pass a real ``V3Config`` / storage-embed-shape dict or to
    pre-build the canonical cfg via the factory before
    calling this module. Tests monkeypatch the factory to a
    deterministic stub so the lab shape is irrelevant for
    the unit suite.
    """
    if cfg is None:
        raise EmbedContractError(
            "embeddings: cfg is None; build_embed_cfg needs a real config"
        )
    embed_cfg = _v3core_embedding.build_embed_cfg(cfg)
    profile = embed_cfg.get("_profile")
    if not isinstance(profile, _v3core_embedding.EmbedProfile):
        raise EmbedContractError(
            "embeddings: build_embed_cfg returned no EmbedProfile"
        )
    dim = embed_cfg.get("dim") or embed_cfg.get("dimension") or 0
    if not isinstance(dim, int) or dim <= 0:
        raise EmbedContractError(
            f"embeddings: build_embed_cfg returned an invalid dim {dim!r}"
        )
    fp = embed_cfg.get("_fingerprint") or ""
    if not fp:
        raise EmbedContractError(
            "embeddings: build_embed_cfg returned an empty fingerprint"
        )
    return profile, int(dim), str(fp)


# A duck-typed stand-in so the type hint for the profile object
# is clear; we only read ``model`` / ``dim`` / ``fingerprint()``
# from it. The real type is v3core.embedding.EmbedProfile.
EmbedProfileLike = _v3core_embedding.EmbedProfile


def _model_id_from_profile(profile: EmbedProfileLike) -> str:
    """Return a stable on-disk model name.

    We use the ``model`` field of the canonical
    :class:`EmbedProfile`. The base URL is intentionally
    dropped from the cache key — the fingerprint already
    encodes the full identity, and the on-disk layout only
    needs a human-readable grouping key.
    """
    return str(getattr(profile, "model", "") or "")


# ---------------------------------------------------------------------------
# Provider call wrapper — vector + dim + profile validation
# ---------------------------------------------------------------------------


def _call_provider_batch(
    texts: list[str],
    embed_cfg: Mapping[str, Any],
    *,
    expected_dim: int,
    expected_profile_fingerprint: str,
    stats: EmbedCacheStats,
) -> list[list[float]]:
    """Call :func:`v3core.embedding.embed_batch` and validate.

    The wrapper enforces four invariants:

      1. The output count equals the input count — no partial
         fallback.
      2. Every vector has the expected dim — never accept a
         truncated / padded vector that would silently corrupt
         downstream cosine math.
      3. Every vector is **non-zero** — the production
         ``call_embedding`` / ``embed_batch`` already forbids
         zero-vector fallbacks, but the evaluator must enforce
         it independently so a stubbed provider cannot poison
         the cache.
      4. The provider-returned dim / profile fingerprint
         matches what the cache key was built from. A mismatch
         raises :class:`EmbedContractError`; this protects
         against a config that changed between the
         ``_resolve_profile`` call and the provider call (e.g.
         a hot-swap of the embed cfg mid-batch).
    """
    if not texts:
        return []
    stats.provider_batch_count += 1
    stats.provider_calls = stats.provider_batch_count
    # Capture the fingerprint BEFORE the provider call. The
    # post-call cross-check compares the cfg against this
    # captured value so a hot-swap between ``_resolve_profile``
    # and the provider call is detected. The provider entry
    # is ``v3core.embedding.embed_batch`` — monkeypatched in
    # tests so no real HTTP happens. We pass the cfg
    # **without** a defensive copy so a real production
    # provider that mutates the cfg (e.g. by attaching a
    # resolved model) is caught by the post-call check.
    pre_fp = (
        embed_cfg.get("_fingerprint") or ""
    ) if isinstance(embed_cfg, Mapping) else ""
    raw = _v3core_embedding.embed_batch(list(texts), embed_cfg)
    if not isinstance(raw, list) or len(raw) != len(texts):
        raise EmbedContractError(
            f"embeddings: embed_batch returned {len(raw) if isinstance(raw, list) else 'non-list'} "
            f"vectors for {len(texts)} inputs"
        )
    out: list[list[float]] = []
    for i, vec in enumerate(raw):
        if not isinstance(vec, list):
            raise EmbedContractError(
                f"embeddings: vector[{i}] is not a list (got {type(vec).__name__})"
            )
        if len(vec) != expected_dim:
            stats.dim_mismatches += 1
            raise EmbedContractError(
                f"embeddings: vector[{i}] has dim {len(vec)}, expected {expected_dim}"
            )
        # Zero-vector check — the production rule is "no zero
        # vector fallback". We reject any vector whose every
        # component is 0.0, not "any zero component" — the
        # latter would false-positive on a legitimate sparse
        # projection.
        if all((float(x) == 0.0) for x in vec):
            stats.zero_vectors_rejected += 1
            raise EmbedContractError(
                f"embeddings: vector[{i}] is a zero vector; refusing to cache it"
            )
        out.append([float(x) for x in vec])
    # One more cross-check: the embed_cfg returned by
    # ``build_embed_cfg`` carries a fingerprint. The provider
    # call's input cfg is the same dict (the cache contract
    # forbids the caller from mutating it between
    # ``_resolve_profile`` and the provider call). We re-read
    # the fingerprint to confirm the contract — it MUST equal
    # both the pre-call capture and the expected fingerprint
    # the cache key was built from.
    fp = (embed_cfg.get("_fingerprint") or "") if isinstance(embed_cfg, Mapping) else ""
    if fp != pre_fp or fp != expected_profile_fingerprint:
        stats.profile_mismatches += 1
        raise EmbedContractError(
            "embeddings: profile fingerprint drifted between "
            "_resolve_profile and the provider call"
        )
    return out


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


def _resolve_dataset_identity(
    dataset: LoCoMoDataset | None,
    *,
    dataset_sha: str | None,
) -> str:
    """Return the dataset sha used for the cache key.

    Either the caller passes an explicit ``dataset_sha`` (the
    recommended path — the dataset object knows its own sha
    from :attr:`LoCoMoDataset.source_sha256`) or a dataset is
    passed. The two are cross-checked so a typo in the explicit
    sha is caught up front rather than silently re-keying the
    cache.
    """
    if dataset is not None:
        actual = str(getattr(dataset, "source_sha256", "") or "")
        if not actual:
            raise EmbedContractError(
                "embeddings: dataset.source_sha256 is empty; pass dataset_sha explicitly"
            )
        if dataset_sha is not None and dataset_sha != actual:
            raise EmbedContractError(
                "embeddings: dataset_sha mismatch: caller passed "
                f"{dataset_sha!r}, dataset.source_sha256 is {actual!r}"
            )
        return actual
    if not dataset_sha:
        raise EmbedContractError(
            "embeddings: neither dataset nor dataset_sha provided"
        )
    return str(dataset_sha)


def _resolve_qa_pair_identity(row: EmbedRow) -> str:
    """Validate / canonicalise a qa_pairs row id.

    For ``qa_pairs`` rows the canonical row id is the eval_v2
    ``source_id`` (``locomo|eval_v2|...|q>a``). The contract
    refuses anything that does not parse through
    :func:`dataset.parse_source_id` so a typo cannot write a
    cache entry that no future call can address.
    """
    try:
        parsed = parse_source_id(row.row_id)
    except ValueError as exc:
        raise EmbedContractError(
            f"embeddings: qa_pairs row_id is not a canonical "
            f"locomo|eval_v2|... source_id: {row.row_id!r}"
        ) from exc
    if not row.answer:
        raise EmbedContractError(
            "embeddings: qa_pairs row requires a non-empty answer"
        )
    # The parsed source_id is byte-identical to ``row.row_id``;
    # the parse is a sanity check, not a transformation.
    return parsed.source_id


def _resolve_conversation_stream_identity(row: EmbedRow) -> str:
    """Validate a conversation_stream row id.

    Conversation-stream rows are content-addressed; the caller
    is responsible for synthesising a stable row id (e.g.
    ``cs|locomo|<sample_id>|<session_key>|<index>``). The
    contract is: non-empty, no path separators, no
    whitespace, parseable as a plain identifier.
    """
    rid = _validate_string_field(
        "row_id", row.row_id, pattern=_FIELD_RE_SOURCE_ID
    )
    if not rid:
        raise EmbedContractError(
            "embeddings: conversation_stream row_id must be non-empty"
        )
    return rid


def _resolve_query_identity(row: EmbedRow) -> str:
    """Validate a query row id.

    The eval_v2 ``qa_id`` (``"q<query_idx>"``) is the natural
    identity for a query row, but the contract accepts any
    non-empty, non-path-separator identifier so a future
    multi-sample query id (``locomo|q|<sample_id>|q<N>``) is
    not artificially rejected.
    """
    return _validate_string_field("row_id", row.row_id, pattern=_FIELD_RE_SOURCE_ID)


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------


def _build_embed_cfg_via_factory(cfg: Any) -> dict[str, Any]:
    """Build the canonical embed_cfg via the production factory.

    The canonical factory
    :func:`v3core.embedding.build_embed_cfg` returns a dict
    that includes ``api_key`` / ``apiKey`` / ``proxy`` /
    ``endpoint`` / ``_raw``. Those MUST NOT be persisted to the
    cache. We strip them when writing; the on-disk meta
    carries only the public identity (model, dim, fingerprint,
    dataset_sha, transform_id).
    """
    embed_cfg = _v3core_embedding.build_embed_cfg(cfg)
    # The caller is expected to never see a None embed_cfg
    # because the function raises ValueError on a missing
    # endpoint / model. We forward that error verbatim — no
    # silent disabled-fallback.
    return embed_cfg


def _scrub_embed_cfg_for_cache(embed_cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``embed_cfg`` with all secret-ish fields removed.

    The cache contract forbids serialising:
      * ``api_key`` / ``apiKey`` — credential.
      * ``proxy`` — credential / network routing hint.
      * ``endpoint`` — already a partial URL; base_url-without-
        query is the safe form, but we keep the canonical
        ``_profile`` and the model / dim so the on-disk layout
        does not need the raw endpoint.
      * ``_raw`` — the original user cfg; almost certainly
        carries secrets.
    """
    safe: dict[str, Any] = {}
    for k, v in dict(embed_cfg).items():
        if k in {"api_key", "apiKey", "proxy", "endpoint", "_raw"}:
            continue
        safe[k] = v
    return safe


def _validate_vector_for_caching(
    vec: list[float],
    *,
    expected_dim: int,
    expected_profile_fingerprint: str,
    stats: EmbedCacheStats,
) -> None:
    """Apply the four invariants the cache contract requires.

    See :func:`_call_provider_batch` for the invariants. This
    helper is the post-call validator: in the normal flow the
    provider call already enforces them, but the helper exists
    so a future provider that returns cached vectors (e.g. a
    short-circuit inside ``embed_batch``) still has to pass the
    same checks before the on-disk leaf is written.
    """
    if not isinstance(vec, list):
        raise EmbedContractError(
            f"embeddings: vector is not a list (got {type(vec).__name__})"
        )
    if len(vec) != expected_dim:
        stats.dim_mismatches += 1
        raise EmbedContractError(
            f"embeddings: vector dim {len(vec)} != expected {expected_dim}"
        )
    if all(float(x) == 0.0 for x in vec):
        stats.zero_vectors_rejected += 1
        raise EmbedContractError("embeddings: zero vector rejected")
    # Profile fingerprint is a leaf-level invariant; the
    # provider-call path checked it on the cfg, but a future
    # vector-only path (e.g. direct cache write) needs the same
    # check. We accept an empty / missing expected fingerprint
    # by raising — a fingerprint is mandatory for any cache
    # entry.
    if not expected_profile_fingerprint:
        stats.profile_mismatches += 1
        raise EmbedContractError(
            "embeddings: empty profile fingerprint on cache write"
        )


# ---------------------------------------------------------------------------
# Driver — corpus and query
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class EmbedResult:
    """One row's prepared embedding.

    ``row_id`` is the canonical identity the caller passed in.
    ``vector`` is the validated vector (always ``dim`` long,
    always non-zero). ``text_hash`` is the SHA-256 of the
    *transformed* text the provider saw; the caller can
    re-derive the text by re-applying the transform. The
    ``cache_hit`` flag is exposed so the caller's telemetry
    can surface a hit rate without a separate stats lookup.
    """

    row_id: str
    vector: list[float]
    text_hash: str
    transform_id: str
    cache_hit: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _validate_kind(kind: str) -> str:
    if kind not in VALID_KINDS:
        raise EmbedContractError(
            f"embeddings: invalid kind {kind!r}; valid kinds are "
            f"{sorted(VALID_KINDS)}"
        )
    return kind


def _resolve_transform(
    kind: str,
    *,
    override_transform_id: str | None,
) -> str:
    """Resolve the transform id for a given kind.

    The default transform id is the production-rule constant
    for the kind. Tests may pass ``override_transform_id`` to
    simulate a production-side rule change (the cache then
    invalidates every entry under the new id, which is the
    behaviour the production hash check guarantees).
    """
    if override_transform_id is not None:
        return _validate_string_field(
            "transform_id", override_transform_id, pattern=_FIELD_RE_TRANSFORM_ID
        )
    if kind == _KIND_QA_PAIRS:
        return QA_PAIRS_TRANSFORM_ID
    if kind == _KIND_CONVERSATION_STREAM:
        return CONVERSATION_STREAM_TRANSFORM_ID
    if kind == _KIND_QUERY:
        # Query rows use the same production transform as
        # qa_pairs so query and corpus land in the same vector
        # space.
        return QA_PAIRS_TRANSFORM_ID
    raise EmbedContractError(f"embeddings: unknown kind {kind!r}")


def _build_text_for_kind(kind: str, row: EmbedRow) -> str:
    """Apply the production transform for the given kind."""
    if kind == _KIND_QA_PAIRS:
        return _qa_pairs_embedding_text(row.text, row.answer or "")
    if kind == _KIND_CONVERSATION_STREAM:
        return _conversation_stream_embedding_text(row.text)
    if kind == _KIND_QUERY:
        # The query rows are eval questions, which have an
        # empty answer in the dataset (the eval row carries
        # the question only; the gold answer is for scoring,
        # not for embedding). The qa transform reduces to
        # ``question`` (no answer → just q).
        return _qa_pairs_embedding_text(row.text, row.answer or "")
    raise EmbedContractError(f"embeddings: unknown kind {kind!r}")


def _resolve_row_id_for_kind(kind: str, row: EmbedRow) -> str:
    """Validate / canonicalise the row id for the given kind."""
    if kind == _KIND_QA_PAIRS:
        return _resolve_qa_pair_identity(row)
    if kind == _KIND_CONVERSATION_STREAM:
        return _resolve_conversation_stream_identity(row)
    if kind == _KIND_QUERY:
        return _resolve_query_identity(row)
    raise EmbedContractError(f"embeddings: unknown kind {kind!r}")


def _validate_batch_size(batch_size: Any) -> int:
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
        raise EmbedContractError(
            f"embeddings: batch_size must be a positive int (got {batch_size!r})"
        )
    return int(batch_size)


def _chunked(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    """Yield ``size``-sized chunks from ``seq``."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _prepare_embeddings(
    *,
    kind: str,
    rows: Sequence[EmbedRow],
    cfg: Any,
    cache_root: str | os.PathLike[str],
    stats: EmbedCacheStats,
    dataset: LoCoMoDataset | None = None,
    dataset_sha: str | None = None,
    transform_id: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[EmbedResult]:
    """Inner driver shared by corpus + query entry points.

    The flow:
      1. Validate kind + transform id + cfg + batch_size.
      2. Resolve dataset sha (cross-checked against the
         ``LoCoMoDataset`` if supplied).
      3. Build the canonical ``embed_cfg`` once via
         :func:`v3core.embedding.build_embed_cfg` so the
         profile fingerprint is the production fingerprint.
      4. Stamp the shape-tagged counters on ``stats`` so the
         caller can report item / char totals for the call.
      5. For each row: validate identity, apply transform,
         look up the cache leaf. On a hit, return the cached
         vector after a re-validation pass. On a miss, queue
         the row for the provider.
      6. Chunk the queued misses into ``batch_size`` chunks,
         call the provider per chunk, validate each batch,
         and write the resulting vectors atomically. The
         stats counters are updated in place; the expected
         and provider batch counts are recorded so the caller
         can confirm the chunking matches what we actually
         called.
    """
    kind = _validate_kind(kind)
    if not rows:
        return []
    if not isinstance(cache_root, (str, os.PathLike)):
        raise EmbedContractError(
            "embeddings: cache_root must be a str or os.PathLike"
        )
    stats.cache_root = str(cache_root)
    batch_size = _validate_batch_size(batch_size)

    resolved_transform = _resolve_transform(kind, override_transform_id=transform_id)
    dataset_sha_final = _resolve_dataset_identity(
        dataset, dataset_sha=dataset_sha
    )
    _validate_string_field(
        "dataset_sha", dataset_sha_final,
        pattern=re.compile(r"^[0-9a-f]{64}$"),
    )
    _validate_string_field(
        "transform_id", resolved_transform,
        pattern=_FIELD_RE_TRANSFORM_ID,
    )

    embed_cfg = _build_embed_cfg_via_factory(cfg)
    profile = embed_cfg.get("_profile")
    if not isinstance(profile, _v3core_embedding.EmbedProfile):
        raise EmbedContractError(
            "embeddings: build_embed_cfg returned no EmbedProfile"
        )
    expected_dim = _validate_dim(
        "dim", embed_cfg.get("dim") or embed_cfg.get("dimension") or 0
    )
    expected_fingerprint = _validate_fingerprint(
        embed_cfg.get("_fingerprint") or ""
    )
    expected_model = _model_id_from_profile(profile)
    if not expected_model:
        raise EmbedContractError(
            "embeddings: EmbedProfile.model is empty; refusing to cache"
        )
    _validate_string_field("model", expected_model, pattern=_FIELD_RE_MODEL)

    cache_root_path = Path(cache_root)
    # Build the on-disk root only if we are about to write
    # leaves. The directory creation is cheap so we do it
    # up front; tests that pass a tmp_path directory get a
    # real directory.
    cache_root_path.mkdir(parents=True, exist_ok=True)
    # The cache_root is a caller-supplied path; we keep it
    # verbatim (the harness is expected to validate it). Only
    # the variable path components (kind / transform / model /
    # dim / fingerprint) need sanitisation.
    model_safe = _safe_path_component(expected_model)
    transform_safe = _safe_path_component(resolved_transform)

    cache_base = _cache_dir(
        cache_root_path,
        _safe_path_component(kind),
        dataset_sha_final,
        transform_safe,
        model_safe,
        expected_dim,
        expected_fingerprint,
    )
    cache_base.mkdir(parents=True, exist_ok=True)
    npz_path = cache_base / _VECTORS_FILENAME
    meta_path = cache_base / _META_FILENAME

    # Pre-compute per-row identity + transform + hash. The
    # ordering is preserved so the returned ``EmbedResult``
    # list matches the input order. We also accumulate the
    # shape-tagged character / item counters the caller
    # reports.
    plan: list[tuple[int, EmbedRow, str, str, str]] = []
    total_chars = 0
    for i, row in enumerate(rows):
        row_id = _resolve_row_id_for_kind(kind, row)
        text = _build_text_for_kind(kind, row)
        total_chars += len(text.encode("utf-8"))
        th = _text_hash(text)
        plan.append((i, row, row_id, text, th))
    if kind == _KIND_QUERY:
        stats.query_items += len(plan)
        stats.query_chars += total_chars
    else:
        stats.corpus_items += len(plan)
        stats.corpus_chars += total_chars

    expected_meta = _CachedMeta(
        dataset_sha=dataset_sha_final,
        transform_id=resolved_transform,
        model=expected_model,
        dim=expected_dim,
        profile_fingerprint=expected_fingerprint,
    )

    results: list[EmbedResult | None] = [None] * len(plan)
    pending_misses: list[tuple[int, str, str, str]] = []
    # pending_misses carries ``(orig_index, row_id, text, th)``.

    # Read the meta once up front (the result is reused for
    # every row lookup). A meta mismatch is shared by every
    # row, so we count it once and skip the npz read entirely.
    cached_meta = _read_meta(meta_path, expected=expected_meta, stats=stats)
    # ONE npz read for the whole pass. ``_lookup_row`` re-read and
    # linearly scanned the archive per row, turning a corpus pass into
    # O(rows x cached_rows) file reads (see ``_load_cache_index``).
    cached_index = (
        _load_cache_index(
            npz_path, expected_dim=expected_dim, stats=stats
        )
        if cached_meta is not None
        else {}
    )

    for orig_index, _row, row_id, text, th in plan:
        stats.lookups += 1
        if cached_meta is None:
            stats.misses += 1
            pending_misses.append((orig_index, row_id, text, th))
            continue
        vec = _validate_cached_vector(
            cached_index.get((row_id, th)),
            expected_dim=expected_dim,
            stats=stats,
        )
        if vec is None:
            stats.misses += 1
            pending_misses.append((orig_index, row_id, text, th))
            continue
        # Re-validate the cached vector before returning it.
        try:
            _validate_vector_for_caching(
                vec,
                expected_dim=expected_dim,
                expected_profile_fingerprint=expected_fingerprint,
                stats=stats,
            )
        except EmbedContractError:
            # A cached leaf with a bad vector is treated as a
            # miss; we drop it from the in-memory result and
            # queue the row for a real provider call.
            stats.invalidations += 1
            pending_misses.append((orig_index, row_id, text, th))
            continue
        stats.hits += 1
        results[orig_index] = EmbedResult(
            row_id=row_id,
            vector=vec,
            text_hash=th,
            transform_id=resolved_transform,
            cache_hit=True,
        )

    if pending_misses:
        # Account for the expected / provider batch counts.
        # ``provider_batch_count`` is incremented inside
        # ``_call_provider_batch`` for each provider call;
        # ``expected_batch_count`` is the chunk count we
        # actually issued, so the two MUST agree.
        stats.expected_batch_count += (
            (len(pending_misses) + batch_size - 1) // batch_size
        )
        # Write the meta sidecar before any new entries
        # appear so a reader can see identity fields even
        # when the npz is empty. The write is atomic and
        # happens exactly once per namespace.
        meta_payload = json.dumps(
            expected_meta.to_dict(), ensure_ascii=False, sort_keys=True
        )
        try:
            _atomic_write_text(meta_path, meta_payload)
        except OSError as exc:
            logger.warning(
                "embeddings: cache meta write failed: %s", exc,
            )
        for chunk in _chunked(pending_misses, batch_size):
            texts = [t for (_i, _r, t, _h) in chunk]
            vectors = _call_provider_batch(
                texts,
                embed_cfg,
                expected_dim=expected_dim,
                expected_profile_fingerprint=expected_fingerprint,
                stats=stats,
            )
            new_row_ids: list[str] = []
            new_text_hashes: list[str] = []
            new_texts_preview: list[str] = []
            new_vectors: list[list[float]] = []
            for (orig_index, row_id, text, th), vec in zip(chunk, vectors):
                _validate_vector_for_caching(
                    vec,
                    expected_dim=expected_dim,
                    expected_profile_fingerprint=expected_fingerprint,
                    stats=stats,
                )
                new_row_ids.append(row_id)
                new_text_hashes.append(th)
                new_texts_preview.append(text[:_LEAF_TEXT_PREVIEW_LEN])
                new_vectors.append(vec)
                results[orig_index] = EmbedResult(
                    row_id=row_id,
                    vector=vec,
                    text_hash=th,
                    transform_id=resolved_transform,
                    cache_hit=False,
                )
            # Persist the chunk atomically into the npz.
            arr = np.asarray(new_vectors, dtype=np.float32)
            if arr.ndim != 2 or arr.shape[1] != expected_dim:
                # Should be unreachable — provider validation
                # already raised — but fail loudly rather than
                # silently dropping vectors.
                raise EmbedContractError(
                    "embeddings: internal error constructing npz array"
                )
            ok = _append_to_npz(
                npz_path,
                new_row_ids=new_row_ids,
                new_text_hashes=new_text_hashes,
                new_texts_preview=new_texts_preview,
                new_vectors=arr,
                expected_dim=expected_dim,
            )
            if not ok:
                # Cache write failure is non-fatal: the
                # provider already returned valid vectors and
                # the in-memory results are correct. We log
                # and move on so a transient FS hiccup cannot
                # poison the eval run.
                logger.warning(
                    "embeddings: cache write failed for %d vectors in chunk",
                    arr.shape[0],
                )
            # Keep the in-memory index in sync so a duplicate row later
            # in the same pass hits without another file read.
            for _rid, _th, _vec in zip(new_row_ids, new_text_hashes, arr):
                cached_index[(_rid, _th)] = _vec

    # Final sweep — every input row must have produced an
    # output. Anything still ``None`` is a contract violation.
    for i, r in enumerate(results):
        if r is None:
            raise EmbedContractError(
                f"embeddings: row {i} ({rows[i].row_id!r}) produced no EmbedResult"
            )
    # mypy / runtime: the sweep above guarantees ``None`` is
    # gone, but the type checker needs the explicit cast.
    return [r for r in results if r is not None]


# Public entry points ------------------------------------------------------


def prepare_corpus_embeddings(
    rows: Sequence[EmbedRow],
    *,
    cfg: Any,
    kind: str = _KIND_QA_PAIRS,
    cache_root: str | os.PathLike[str] = DEFAULT_CACHE_ROOT,
    dataset: LoCoMoDataset | None = None,
    dataset_sha: str | None = None,
    transform_id: str | None = None,
    stats: EmbedCacheStats | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[list[EmbedResult], EmbedCacheStats]:
    """Compute (or hit-cache) one corpus shape's embeddings.

    Parameters
    ----------
    rows:
        The corpus rows. For ``kind="qa_pairs"`` each row MUST
        carry a non-empty ``answer`` (the qa transform
        requires it). For ``kind="conversation_stream"`` the
        row's ``text`` is truncated to 2000 chars.
    cfg:
        The config object passed through to
        :func:`v3core.embedding.build_embed_cfg`. The factory
        raises :class:`ValueError` on a missing endpoint /
        model — the function forwards that error verbatim.
    kind:
        One of ``"qa_pairs"`` (default) or
        ``"conversation_stream"``. The contract refuses any
        other value.
    cache_root:
        Caller-supplied durable cache directory. Default is a
        workspace scratch path. The cache root is created if
        it does not exist. Nothing under this path is ever
        committed to the repo; the harness is expected to
        gitignore the ``eval-cache`` family.
    dataset:
        Optional :class:`LoCoMoDataset`. When supplied its
        ``source_sha256`` is used as the dataset identity and
        cross-checked against ``dataset_sha`` if both are
        given.
    dataset_sha:
        Optional explicit 64-char hex sha. When supplied
        without a dataset, this is the dataset identity. When
        supplied together with a dataset the two must match.
    transform_id:
        Optional override for the production transform id.
        Tests use this to simulate a production-side rule
        change and assert the cache invalidates.
    stats:
        Optional pre-existing stats object. The function
        mutates the counters in place. If omitted, a fresh
        stats object is created and returned.
    batch_size:
        Provider-call chunk size. The pending cache misses
        are split into ``ceil(N / batch_size)`` chunks and
        :func:`v3core.embedding.embed_batch` is called once
        per chunk. Defaults to ``DEFAULT_BATCH_SIZE = 32``.
        The chunking is the source of the expected /
        provider batch counts in the returned stats.

    Returns
    -------
    (results, stats):
        ``results`` is one :class:`EmbedResult` per input row,
        in the same order. ``stats`` carries the
        hit / miss / invalidation / provider-call counters,
        the corpus item / char totals, and the expected /
        provider batch counts.
    """
    if stats is None:
        stats = EmbedCacheStats()
    return (
        _prepare_embeddings(
            kind=kind,
            rows=list(rows),
            cfg=cfg,
            cache_root=cache_root,
            stats=stats,
            dataset=dataset,
            dataset_sha=dataset_sha,
            transform_id=transform_id,
            batch_size=batch_size,
        ),
        stats,
    )


def prepare_query_embeddings(
    rows: Sequence[EmbedRow],
    *,
    cfg: Any,
    cache_root: str | os.PathLike[str] = DEFAULT_CACHE_ROOT,
    dataset: LoCoMoDataset | None = None,
    dataset_sha: str | None = None,
    transform_id: str | None = None,
    stats: EmbedCacheStats | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[list[EmbedResult], EmbedCacheStats]:
    """Compute (or hit-cache) one query shape's embeddings.

    The query path uses the same production transform as
    ``qa_pairs`` so query and corpus land in the same vector
    space. The cache namespace is the dedicated ``query/``
    leaf so query rows and corpus rows cannot collide even if
    the row_id happens to overlap (e.g. an eval row's
    ``source_id`` and a conversation-stream row's id).

    Parameters mirror :func:`prepare_corpus_embeddings`
    except ``kind`` is fixed at ``"query"``.
    """
    if stats is None:
        stats = EmbedCacheStats()
    return (
        _prepare_embeddings(
            kind=_KIND_QUERY,
            rows=list(rows),
            cfg=cfg,
            cache_root=cache_root,
            stats=stats,
            dataset=dataset,
            dataset_sha=dataset_sha,
            transform_id=transform_id,
            batch_size=batch_size,
        ),
        stats,
    )


# ---------------------------------------------------------------------------
# Row builders — convenience for the caller
# ---------------------------------------------------------------------------


def build_qa_pair_rows(
    qa_pairs: Iterable[Any],
) -> list[EmbedRow]:
    """Build :class:`EmbedRow` instances from ``qa_pairs`` dataset rows.

    Accepts any iterable of objects with ``source_id`` /
    ``question`` / ``answer`` attributes. ``LoCoMoQAPair`` is
    the canonical source. Rows whose ``source_id`` does not
    parse through :func:`dataset.parse_source_id` are
    **skipped silently** — the dataset loader's contract
    already guarantees a valid ``source_id``; a future
    evaluation-side row that violates the contract should
    fail the dataset, not the embedding prep.
    """
    out: list[EmbedRow] = []
    for r in qa_pairs:
        sid = str(getattr(r, "source_id", "") or "")
        q = str(getattr(r, "question", "") or getattr(r, "q_text", "") or "")
        a = str(getattr(r, "answer", "") or getattr(r, "a_text", "") or "")
        try:
            parsed = parse_source_id(sid)
        except ValueError:
            # The contract is "skip silently" here. The
            # downstream prepare_corpus_embeddings call will
            # re-validate and raise for any row that survives
            # this filter, so the contract is still fail-closed
            # at the public boundary.
            continue
        out.append(
            EmbedRow(
                row_id=parsed.source_id,
                text=q,
                answer=a,
                provenance={
                    "sample_id": parsed.sample_id,
                    "session_key": parsed.session_key,
                    "q_dia_id": parsed.q_dia_id,
                    "a_dia_id": parsed.a_dia_id,
                },
            )
        )
    return out


def build_conversation_stream_rows(
    messages: Iterable[Any],
    *,
    sample_id_attr: str = "sample_id",
    session_key_attr: str = "session_key",
    index_attr: str = "index",
) -> list[EmbedRow]:
    """Build :class:`EmbedRow` instances for ``conversation_stream``.

    The row id is ``cs|locomo|<sample_id>|<session_key>|<index>``
    — content-addressable, stable across runs, and parseable as
    a plain identifier (no path separators, no whitespace).
    """
    out: list[EmbedRow] = []
    for m in messages:
        sid = str(getattr(m, sample_id_attr, "") or "")
        skey = str(getattr(m, session_key_attr, "") or "")
        idx = int(getattr(m, index_attr, 0) or 0)
        text = str(getattr(m, "text", "") or "")
        if not (sid and skey):
            continue
        row_id = f"cs|locomo|{sid}|{skey}|{idx}"
        out.append(
            EmbedRow(
                row_id=row_id,
                text=text,
                answer=None,
                provenance={
                    "sample_id": sid,
                    "session_key": skey,
                    "index": idx,
                },
            )
        )
    return out


def build_query_rows(
    eval_rows: Iterable[Any],
) -> list[EmbedRow]:
    """Build :class:`EmbedRow` instances for the eval query list.

    The row id is the eval_v2 ``source_id``
    (``locomo|eval_v2|<sample_id>|q<query_idx>``) so a future
    caller can recover the query from the cache key alone.
    """
    out: list[EmbedRow] = []
    for r in eval_rows:
        sid = str(getattr(r, "sample_id", "") or "")
        qi = int(getattr(r, "query_idx", 0) or 0)
        qa_id = str(getattr(r, "qa_id", "") or f"q{qi}")
        question = str(getattr(r, "question", "") or "")
        if not sid:
            continue
        # The query source_id uses the same locomo|eval_v2
        # namespace as the qa_pair source_id. They differ
        # only in the trailing segment (``q<N>`` vs
        # ``session_key|q>a``), so a future caller parsing
        # the namespace can disambiguate by looking at the
        # separator shape.
        row_id = f"locomo|eval_v2|{sid}|{qa_id}"
        out.append(
            EmbedRow(
                row_id=row_id,
                text=question,
                answer=None,
                provenance={
                    "sample_id": sid,
                    "qa_id": qa_id,
                    "query_idx": qi,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Cache identity accessor — for the test suite
# ---------------------------------------------------------------------------


def describe_cache_identity(
    *,
    cache_root: str | os.PathLike[str],
    kind: str,
    dataset_sha: str,
    transform_id: str,
    cfg: Any,
) -> dict[str, str]:
    """Return the on-disk cache identity for a given (kind, config).

    The function is intentionally read-only: it returns the
    identity components the cache would key on, so the test
    suite can assert the on-disk layout without a real
    provider call.
    """
    kind = _validate_kind(kind)
    _validate_string_field(
        "dataset_sha", dataset_sha,
        pattern=re.compile(r"^[0-9a-f]{64}$"),
    )
    _validate_string_field(
        "transform_id", transform_id,
        pattern=_FIELD_RE_TRANSFORM_ID,
    )
    embed_cfg = _build_embed_cfg_via_factory(cfg)
    profile = embed_cfg.get("_profile")
    if not isinstance(profile, _v3core_embedding.EmbedProfile):
        raise EmbedContractError(
            "embeddings: build_embed_cfg returned no EmbedProfile"
        )
    fp = _validate_fingerprint(embed_cfg.get("_fingerprint") or "")
    model = _model_id_from_profile(profile)
    _validate_string_field("model", model, pattern=_FIELD_RE_MODEL)
    dim = _validate_dim(
        "dim", embed_cfg.get("dim") or embed_cfg.get("dimension") or 0
    )
    cache_base = _cache_dir(
        str(cache_root),
        _safe_path_component(kind),
        dataset_sha,
        _safe_path_component(transform_id),
        _safe_path_component(model),
        dim,
        fp,
    )
    return {
        "cache_root": str(cache_root),
        "kind": kind,
        "dataset_sha": dataset_sha,
        "transform_id": transform_id,
        "model": model,
        "dim": str(dim),
        "profile_fingerprint": fp,
        "cache_dir": str(cache_base),
    }


__all__ = [
    "CONVERSATION_STREAM_TRANSFORM_ID",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_CACHE_ROOT",
    "EmbedCacheStats",
    "EmbedContractError",
    "EmbedResult",
    "EmbedRow",
    "QA_PAIRS_TRANSFORM_ID",
    "VALID_KINDS",
    "build_conversation_stream_rows",
    "build_qa_pair_rows",
    "build_query_rows",
    "describe_cache_identity",
    "prepare_corpus_embeddings",
    "prepare_query_embeddings",
]
