"""Tokenizer-aware long-observation planner (pure layer, no I/O).

Source of record: ``docs/LONG-OBSERVATION-INDEX.md`` (representation
contract §3, planner §5, aggregation §6, recall §8).

Contract summary:
- Short observation: the exact ``observation_notes.content`` stays the
  single-request representation. No sidecar children. The writer keeps
  its existing one-request/full-content path byte-identical.
- Long observation: the generic text-span seam in
  :mod:`v3core.embed_chunks` (``split_text_into_token_safe_spans``,
  itself over ``_encode_field_offsets`` + ``_split_field_with_offsets``)
  plans non-overlapping, coverage-complete ``content`` spans with
  ``source_field='content'``. Each planned span becomes one provider
  call; the parent vector is the L2-normalized mean via the existing
  :func:`aggregate_parent_embedding`. **No silent truncation. No unsafe
  request. Fail closed.**
- Representation version: ``v1-long-observation`` — distinct from the
  QA ``v0.2.0-long-qa`` line so the repair planner can detect drift
  per index family.
- Empty source is a short plan with empty ``embed_text``: no embedding
  request is planned, no vector is invented.

This module is pure: it never issues embedding requests, never touches
the DB. :func:`build_observation_derived_state` takes an injected
``embed_one`` callable so unit tests stay deterministic without a
provider.
"""
from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from v3core.embed_chunks import (
    EmbedChunk,
    TokenizerUnavailableError,
    aggregate_parent_embedding,
    safe_token_target,
    split_text_into_token_safe_spans,
    _get_token_counter,
)

# Bump when the observation split / ordering / aggregation semantics
# changes in a way that requires existing rows to be re-derived.
# Persisted on every plan and every sidecar chunk row.
OBSERVATION_REPRESENTATION_VERSION = "v1-long-observation"

# Single-field identity used for every observation chunk row / span.
OBSERVATION_SOURCE_FIELD = "content"


class ObservationChunkPlanError(ValueError):
    """A planned observation chunk set failed fail-closed validation.

    Raised by :func:`validate_observation_chunk_plan` on gaps, overlap,
    source-hash drift, version drift, or per-chunk token overflow — and
    by :func:`build_observation_derived_state` on any child failure or
    child-count mismatch. Callers MUST treat this as a durable failure
    (retryable marker), never as a partially durable parent.
    """


class ObservationDerivedStateError(RuntimeError):
    """A long-observation parent vector could not be derived CLP-closed.

    Raised by :func:`build_observation_derived_state` when any child
    ``embed_one`` call fails (raises, or returns no usable vector) or
    when the collected child count differs from the plan. The helper
    never returns a partial parent: either every planned child has a
    valid vector and the aggregated parent is returned, or this error
    is raised and nothing derived is returned.
    """


@dataclass(frozen=True)
class ObservationChunkPlan:
    """Result of :func:`plan_observation_chunks`.

    ``embed_text`` is always the exact short-path string (the complete
    ``content`` verbatim) so the writer's short path stays byte-identical.
    ``token_count`` is the measured tokenizer count of the full content;
    it is ``None`` only when the fast UTF-8-bytes short proof fired and
    no tokenizer probe was needed. ``chunks`` is empty for short plans.
    """

    is_long: bool
    source_sha256: str
    chunks: List[EmbedChunk] = field(default_factory=list)
    representation_version: str = OBSERVATION_REPRESENTATION_VERSION
    embed_text: str = ""
    token_count: Optional[int] = None


@dataclass(frozen=True)
class ObservationDerivedState:
    """Fully derived long/short observation embedding state.

    ``child_embeddings`` is empty for the short path (single direct
    vector). ``parent_embedding`` is the aggregated parent for the long
    path (via :func:`aggregate_parent_embedding`) or the direct vector
    for the short path.
    """

    parent_embedding: List[float]
    child_embeddings: List[List[float]] = field(default_factory=list)
    model_fingerprint: str = ""
    representation_version: str = OBSERVATION_REPRESENTATION_VERSION


def _sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def plan_observation_chunks(
    content: str,
    embed_cfg: Optional[Dict[str, Any]],
    tokenizer_override: Optional[Any] = None,
    target_tokens: Optional[int] = None,
) -> ObservationChunkPlan:
    """Plan the short-vs-long embedding representation for ``content``.

    Short path (``is_long=False``): ``content`` fits the safe token
    target — return the exact ``content`` as ``embed_text`` with no
    chunks. Long path (``is_long=True``): return non-overlapping,
    coverage-complete ``content`` spans, each at or below the target,
    stamped with :data:`OBSERVATION_REPRESENTATION_VERSION`.

    Fail-closed: :class:`TokenizerUnavailableError` propagates when the
    tokenizer cannot be resolved for a borderline/long source.
    """
    text = content or ""
    if target_tokens is None:
        target_tokens = safe_token_target(embed_cfg)
    source_sha = _sha256_hex(text)
    # Fast proof gate for ordinary short observations: a tokenizer
    # cannot emit more tokens than UTF-8 bytes for the model family
    # used here, leaving four special-token slots. NOT a token estimate
    # and never used to split or send a borderline/long request.
    if len(text.encode("utf-8")) + 4 <= target_tokens:
        return ObservationChunkPlan(
            is_long=False,
            source_sha256=source_sha,
            chunks=[],
            embed_text=text,
            token_count=None,
        )
    counter = _get_token_counter(embed_cfg, tokenizer_override=tokenizer_override)
    try:
        full_tokens = int(counter(text) or 0)
    except TokenizerUnavailableError:
        raise
    except Exception as e:
        raise TokenizerUnavailableError(
            f"tokenizer raised during observation short-text probe: {e!r}"
        ) from e
    if full_tokens <= target_tokens:
        return ObservationChunkPlan(
            is_long=False,
            source_sha256=source_sha,
            chunks=[],
            embed_text=text,
            token_count=full_tokens,
        )
    raw = split_text_into_token_safe_spans(
        OBSERVATION_SOURCE_FIELD,
        text,
        embed_cfg,
        tokenizer_override=tokenizer_override,
        target_tokens=target_tokens,
        starting_chunk_index=0,
    )
    chunks = [
        dataclasses.replace(c, representation_version=OBSERVATION_REPRESENTATION_VERSION)
        for c in raw
    ]
    return ObservationChunkPlan(
        is_long=True,
        source_sha256=source_sha,
        chunks=chunks,
        embed_text=text,
        token_count=full_tokens,
    )


def validate_observation_chunk_plan(
    content: str,
    plan: ObservationChunkPlan,
    target_tokens: Optional[int] = None,
    *,
    embed_cfg: Optional[Dict[str, Any]] = None,
) -> None:
    """Fail-closed validation of a planned chunk set against ``content``.

    Raises :class:`ObservationChunkPlanError` on version drift,
    source-hash drift, ``embed_text`` drift, gaps, overlap, uncovered
    head/tail, per-chunk span-hash mismatch, or any chunk above the
    safe token target. Returns ``None`` when the plan is exact.
    """
    text = content or ""
    if plan.representation_version != OBSERVATION_REPRESENTATION_VERSION:
        raise ObservationChunkPlanError(
            f"representation version drift: plan carries "
            f"{plan.representation_version!r}, expected "
            f"{OBSERVATION_REPRESENTATION_VERSION!r}"
        )
    if plan.source_sha256 != _sha256_hex(text):
        raise ObservationChunkPlanError(
            "source_sha256 does not match the current content hash — "
            "the source moved under the plan; refusing a stale derived set"
        )
    if plan.embed_text != text:
        raise ObservationChunkPlanError(
            "plan embed_text is not the exact current content — refusing"
        )
    if not plan.is_long:
        if plan.chunks:
            raise ObservationChunkPlanError(
                "short plan must not carry chunks — refusing"
            )
        return
    if not plan.chunks:
        raise ObservationChunkPlanError(
            "long plan carries no chunks — refusing to derive a parent "
            "from an empty child set"
        )
    if target_tokens is None:
        target_tokens = safe_token_target(embed_cfg)
    n_chars = len(text)
    prev_end = 0
    parts: List[str] = []
    for pos, chunk in enumerate(plan.chunks):
        if chunk.chunk_index != pos:
            raise ObservationChunkPlanError(
                f"chunk_index not dense at position {pos} "
                f"(got {chunk.chunk_index}) — refusing"
            )
        if chunk.representation_version != OBSERVATION_REPRESENTATION_VERSION:
            raise ObservationChunkPlanError(
                f"chunk {pos} version drift "
                f"({chunk.representation_version!r}) — refusing"
            )
        if chunk.source_field != OBSERVATION_SOURCE_FIELD:
            raise ObservationChunkPlanError(
                f"chunk {pos} source_field {chunk.source_field!r} is not "
                f"{OBSERVATION_SOURCE_FIELD!r} — refusing"
            )
        if chunk.token_count > target_tokens:
            raise ObservationChunkPlanError(
                f"chunk {pos} token_count {chunk.token_count} exceeds "
                f"target {target_tokens} — refusing an unsafe span"
            )
        if chunk.token_count < 1:
            raise ObservationChunkPlanError(
                f"chunk {pos} has non-positive token_count — refusing"
            )
        s, e = int(chunk.source_start), int(chunk.source_end)
        if not (0 <= s <= e <= n_chars):
            raise ObservationChunkPlanError(
                f"chunk {pos} span [{s}, {e}) outside source bounds "
                f"(chars={n_chars}) — refusing"
            )
        if s != prev_end:
            raise ObservationChunkPlanError(
                f"chunk {pos} starts at {s}, expected contiguous "
                f"{prev_end} — gap/overlap; refusing"
            )
        span = text[s:e]
        if chunk.text != span:
            raise ObservationChunkPlanError(
                f"chunk {pos} text does not equal content[{s}:{e}] — "
                "refusing a rewritten span"
            )
        if chunk.source_sha256 != _sha256_hex(span):
            raise ObservationChunkPlanError(
                f"chunk {pos} span hash mismatch — refusing"
            )
        parts.append(span)
        prev_end = e
    if prev_end != n_chars:
        raise ObservationChunkPlanError(
            f"chunk coverage ends at {prev_end}, source has {n_chars} "
            "chars — uncovered tail; refusing"
        )
    if "".join(parts) != text:
        raise ObservationChunkPlanError(
            "concatenated chunk text does not equal the source — refusing"
        )


def _hit_field(hit: Any, *names: str, default: Any = None) -> Any:
    """Read ``names[0..]`` from a Mapping hit or an attribute object."""
    if isinstance(hit, Mapping):
        for name in names:
            if name in hit:
                return hit[name]
        return default
    for name in names:
        if hasattr(hit, name):
            return getattr(hit, name)
    return default


def merge_observation_recall_hits(
    candidates: Sequence[Any],
) -> List[Dict[str, Any]]:
    """Merge parent/child observation candidates into one hit per parent.

    Groups ``candidates`` by ``(observation_id, observation_version)``.
    The merged hit carries ``cosine = max(parent/child candidate
    scores)``, ``content`` is always the full parent content (preferred
    from a parent-kind candidate, else the first non-empty candidate
    content), and ``source_kinds`` preserves which lanes contributed
    (e.g. ``['child', 'parent']``). One dict per parent; deterministic
    order by ``(-cosine, observation_id, observation_version)``.
    """
    groups: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    order: List[Tuple[Any, Any]] = []
    for hit in candidates or []:
        obs_id = _hit_field(hit, "observation_id", "id", "note_id")
        obs_version = _hit_field(hit, "observation_version", "version")
        key = (obs_id, obs_version)
        score = _hit_field(hit, "cosine", "score", "similarity", default=None)
        try:
            score_f = float(score) if score is not None else float("-inf")
        except (TypeError, ValueError):
            score_f = float("-inf")
        kind = str(
            _hit_field(hit, "kind", "source", "source_kind", "lane", default="unknown")
        )
        content = _hit_field(
            hit, "parent_content", "content", "text", default=""
        )
        entry = groups.get(key)
        if entry is None:
            entry = {
                "observation_id": obs_id,
                "observation_version": obs_version,
                "cosine": score_f,
                "content": content or "",
                "source_kinds": [],
                "candidate_count": 0,
            }
            groups[key] = entry
            order.append(key)
        if score_f > entry["cosine"]:
            entry["cosine"] = score_f
        # Prefer the full parent content: a parent-lane candidate wins;
        # otherwise keep the first non-empty content seen.
        if kind == "parent" and content:
            entry["content"] = content
        elif not entry["content"] and content:
            entry["content"] = content
        if kind not in entry["source_kinds"]:
            entry["source_kinds"].append(kind)
        entry["candidate_count"] += 1
    merged = [groups[k] for k in order]
    for entry in merged:
        entry["source_kinds"] = sorted(entry["source_kinds"])
    merged.sort(
        key=lambda e: (
            -(e["cosine"] if e["cosine"] != float("-inf") else float("inf")),
            str(e["observation_id"]),
            str(e["observation_version"]),
        )
    )
    return merged


def stale_child_indexes(planned_count: int, stored_indexes: Sequence[int]) -> List[int]:
    """Return stored dense indexes no longer present in a new plan.

    Persistence still uses scoped ``DELETE`` + full insert; this helper is a
    small auditable contract used by tests and operator diagnostics.
    """
    return sorted({int(i) for i in stored_indexes if int(i) >= int(planned_count)})


def build_observation_derived_state(
    plan: ObservationChunkPlan,
    embed_one: Callable[[str], Sequence[float]],
    model_fingerprint: str = "",
) -> ObservationDerivedState:
    """Derive the full embedding state for ``plan`` via ``embed_one``.

    All-child gate: for a long plan every planned chunk is embedded in
    order and the parent is aggregated with the existing
    :func:`aggregate_parent_embedding`. Any child failure — ``embed_one``
    raising, or returning no usable vector — raises
    :class:`ObservationDerivedStateError` and nothing partial is
    returned. A collected child count differing from the plan likewise
    raises. For a short plan a single ``embed_one(plan.embed_text)``
    call produces the direct parent vector (empty ``embed_text`` plans
    no request and raises instead of inventing a vector).
    """
    if not callable(embed_one):
        raise ObservationDerivedStateError(
            "embed_one is not callable — refusing to derive"
        )
    if not plan.is_long:
        if not plan.embed_text:
            raise ObservationDerivedStateError(
                "short plan has empty embed_text — no request is planned "
                "and no vector will be invented"
            )
        try:
            vec = list(embed_one(plan.embed_text))
        except ObservationDerivedStateError:
            raise
        except Exception as e:
            raise ObservationDerivedStateError(
                f"short-path embed_one failed: {e!r}"
            ) from e
        if not vec:
            raise ObservationDerivedStateError(
                "short-path embed_one returned no vector — refusing"
            )
        return ObservationDerivedState(
            parent_embedding=list(vec),
            child_embeddings=[],
            model_fingerprint=model_fingerprint,
            representation_version=OBSERVATION_REPRESENTATION_VERSION,
        )
    if not plan.chunks:
        raise ObservationDerivedStateError(
            "long plan carries no chunks — refusing to fabricate a parent"
        )
    child_vecs: List[List[float]] = []
    for chunk in plan.chunks:
        try:
            vec = list(embed_one(chunk.embed_text))
        except ObservationDerivedStateError:
            raise
        except Exception as e:
            raise ObservationDerivedStateError(
                f"child {chunk.chunk_index} embed_one failed: {e!r}"
            ) from e
        if not vec:
            raise ObservationDerivedStateError(
                f"child {chunk.chunk_index} returned no vector — "
                "refusing a partial parent"
            )
        child_vecs.append(list(vec))
    if len(child_vecs) != len(plan.chunks):
        raise ObservationDerivedStateError(
            f"child count mismatch: planned {len(plan.chunks)}, collected "
            f"{len(child_vecs)} — refusing a partial parent"
        )
    try:
        parent = aggregate_parent_embedding(child_vecs)
    except Exception as e:
        raise ObservationDerivedStateError(
            f"parent aggregation failed over {len(child_vecs)} children: {e!r}"
        ) from e
    return ObservationDerivedState(
        parent_embedding=list(parent),
        child_embeddings=child_vecs,
        model_fingerprint=model_fingerprint,
        representation_version=OBSERVATION_REPRESENTATION_VERSION,
    )
