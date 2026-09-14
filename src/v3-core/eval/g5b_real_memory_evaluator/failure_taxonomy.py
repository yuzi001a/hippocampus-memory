"""Earliest-layer failure taxonomy for the G5b evaluator.

The taxonomy names the **earliest** pipeline layer where a scenario
failure is detected. A scenario that does not pass must be tagged
exactly one of these labels (UNKNOWN only when no other label
applies).

Layers (earliest → latest):

  NOT_STORED          — write path never committed a canonical row
                        (DURABLE_FAILED before durable commit,
                        pool unavailable, missing memory_id, etc.).
  NOT_INDEXED         — row is durable but embedding is missing or
                        embed_model is missing (post-commit embedding
                        step never landed).
  NOT_RETRIEVED       — row is durable and indexed, but reader
                        returned no candidates at all (any lane).
  RANKED_TOO_LOW      — expected memory present in candidates but its
                        rank is below the scenario's must_recall_rank
                        (e.g. outside top-K).
  WRONG_MEMORY        — candidate matches a non-expected memory that
                        violates must_not_recall (not the expected one).
  STALE_MEMORY        — candidate matches a memory whose status is no
                        longer 'active' (should have been archived) or
                        whose content/tags diverged from the canonical
                        payload we expected (e.g. provenance overwrite
                        regression).
  ARCHIVED_MEMORY_RETURNED — reader returned a memory whose status
                        is 'archived' (archive isolation violated).
  CONFLICT_RESOLUTION — the writer accepted a same-id different-payload
                        attempt (DURABLE_FAILED expected) OR a same-
                        payload different-id rewrite (DEDUPLICATED
                        expected) — conflict resolution violated.
  PIPELINE_ERROR      — any unexpected exception leaked from
                        ActiveMemoryWriter/Reader; recorded with the
                        repr-truncated error message.
  TIMEOUT             — lease deadline exceeded (PrefetchDeadlineExceeded).
  UNSUPPORTED         — the scenario requests a lane/feature that is
                        not yet implemented by the writer/reader
                        (e.g. multi-tag ranking, hybrid rerank) and we
                        explicitly tag the scenario UNSUPPORTED so it
                        does NOT count against the pass rate.
  UNKNOWN             — only when none of the above matches; treated
                        as a reviewer-visible bug.
"""
from __future__ import annotations

from typing import Final


NOT_STORED: Final[str] = "NOT_STORED"
NOT_INDEXED: Final[str] = "NOT_INDEXED"
NOT_RETRIEVED: Final[str] = "NOT_RETRIEVED"
RANKED_TOO_LOW: Final[str] = "RANKED_TOO_LOW"
WRONG_MEMORY: Final[str] = "WRONG_MEMORY"
STALE_MEMORY: Final[str] = "STALE_MEMORY"
ARCHIVED_MEMORY_RETURNED: Final[str] = "ARCHIVED_MEMORY_RETURNED"
CONFLICT_RESOLUTION: Final[str] = "CONFLICT_RESOLUTION"
PIPELINE_ERROR: Final[str] = "PIPELINE_ERROR"
TIMEOUT: Final[str] = "TIMEOUT"
UNSUPPORTED: Final[str] = "UNSUPPORTED"
UNKNOWN: Final[str] = "UNKNOWN"


ALL_LABELS: Final[tuple[str, ...]] = (
    NOT_STORED,
    NOT_INDEXED,
    NOT_RETRIEVED,
    RANKED_TOO_LOW,
    WRONG_MEMORY,
    STALE_MEMORY,
    ARCHIVED_MEMORY_RETURNED,
    CONFLICT_RESOLUTION,
    PIPELINE_ERROR,
    TIMEOUT,
    UNSUPPORTED,
    UNKNOWN,
)


# Scenarios tagged ``unsupported=True`` produce UNSUPPORTED (not
# counted in pass rate); they are still reported as
# ``pipeline_status="UNSUPPORTED"`` so reviewers see them.
def normalize(label: str) -> str:
    if not isinstance(label, str) or not label:
        return UNKNOWN
    return label if label in ALL_LABELS else UNKNOWN


__all__ = [
    "NOT_STORED",
    "NOT_INDEXED",
    "NOT_RETRIEVED",
    "RANKED_TOO_LOW",
    "WRONG_MEMORY",
    "STALE_MEMORY",
    "ARCHIVED_MEMORY_RETURNED",
    "CONFLICT_RESOLUTION",
    "PIPELINE_ERROR",
    "TIMEOUT",
    "UNSUPPORTED",
    "UNKNOWN",
    "ALL_LABELS",
    "normalize",
]