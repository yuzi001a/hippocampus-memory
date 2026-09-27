"""Tokenizer-aware long-QA embedding representation (v0.2 closing round).

Source of record: docs/QA-EMBEDDING-INDEX-DESIGN.md.

Contract summary:
- Short QA: existing exact representation
  (question + "\\n" + answer) is **unchanged** when its tokenizer count is
  within the safe limit. The parent ``qa_pairs.embedding`` row is the
  whole-QA representation and the existing recall path reads it.
- Long QA: token-safe non-overlapping chunks are persisted in the additive
  ``qa_embedding_chunks`` sidecar; the parent ``qa_pairs.embedding`` is
  recomputed as the L2-normalized mean of the successful child vectors so
  the existing recall path never sees an over-limit single request.
- The tokenizer is the configured embedding model by default
  (BAAI/bge-m3 in production). Operators may override via a callable or a
  sentinel. **No silent truncation. No unsafe request. Fail closed** when
  the tokenizer is unavailable and no override was supplied.
- The safe chunk target is the configured provider limit minus an explicit
  safety margin (default 512 tokens below the 8192 ``max_input_tokens``).

The chunker is **pure** — it returns metadata and chunk text only; it
does NOT issue embedding requests. Callers feed each ``chunk["embed_text"]``
to the existing ``call_embedding`` / ``embed_batch`` and aggregate. This
keeps the existing cache + retry contract intact and lets the QA write path
preserve its current fail-closed behavior on provider errors.

Only the *long* path goes through this module. The short path still calls
``call_embedding`` (or ``embed_batch``) directly and is byte-identical to
the previous contract.

Tokenizer backend selection (smallest-compat-first):
  1. Callable override: use as-is. (Unit-test / harness seam.)
  2. ``tokenizers.Tokenizer.from_pretrained(model)`` — the raw `tokenizers`
     package is the canonical runtime dependency for production BGE; this
     is the default and matches the model the embedding provider serves.
  3. ``transformers.AutoTokenizer.from_pretrained(model, use_fast=True)``
     — alternate backend when only the higher-level wrapper is present.
  4. ``char_estimate`` sentinel — opt-in heuristic for unit tests /
     offline analysis. Never default; never auto-selected when a model
     is configured.

The split algorithm is linear in the field length: it encodes the entire
field once with ``return_offsets_mapping=True`` and partitions the
resulting (tokens, offsets) pair into contiguous chunks of ≤ target
tokens. No re-tokenization per probe, no exponential backoff that
re-scans the prefix. A 40k-char answer tokenizes once, then is sliced.
"""
from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


# ── Module constants ──────────────────────────────────────────────────────────

# Bump this when the chunk split logic / field ordering / aggregation
# semantics changes in a way that requires existing rows to be re-derived.
# Persisted on every chunk row so the repair planner can detect drift.
REPRESENTATION_VERSION = "v0.2.0-long-qa"

# Default safe target below the configured provider limit.
# Production BAAI/bge-m3 ships with max_input_tokens=8192; we leave a 512
# token safety margin so a chunk never gets rejected by the upstream
# provider for borderline length (special tokens, BOS, padding, etc.).
DEFAULT_MAX_INPUT_TOKENS = 8192
DEFAULT_SAFETY_MARGIN = 512


class TokenizerUnavailableError(RuntimeError):
    """Raised when the configured tokenizer cannot be loaded.

    The long-QA path is fail-closed: rather than sending an unsafe
    over-limit request or silently truncating the source, we surface this
    as a durable error. Callers MUST mark the QA row for retry and MUST
    NOT mark it fully durable.
    """


# ── Public dataclass ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EmbedChunk:
    """One non-overlapping derived span of either ``question`` or ``answer``.

    ``source_field`` is ``"question"`` or ``"answer"``; ``source_start`` /
    ``source_end`` are character offsets into the original QA field. The
    ``text`` is the verbatim slice of the original (no rewriting, no
    truncation). ``embed_text`` is the exact string sent to the embedding
    provider (typically ``text`` as-is, but kept separate so future
    augmentation strategies can wrap without losing the source span).
    """

    chunk_index: int
    source_field: str  # "question" | "answer"
    source_start: int
    source_end: int
    source_sha256: str
    token_count: int
    text: str
    embed_text: str
    representation_version: str = REPRESENTATION_VERSION


@dataclass(frozen=True)
class LongQARepresentation:
    """Result of :func:`build_qa_embedding_representation` for the long path.

    ``embedding`` is the L2-normalized mean of child vectors (parent
    vector). ``chunks`` are the per-chunk metadata to persist into the
    additive ``qa_embedding_chunks`` sidecar. ``is_long`` is True so
    callers can branch on which path they took.
    """

    is_long: bool
    embedding: List[float]
    chunks: List[EmbedChunk] = field(default_factory=list)
    representation_version: str = REPRESENTATION_VERSION
    embed_text: str = ""
    # True iff every child provider call succeeded; the caller is expected
    # to compute this when it iterates the chunks. Default True is
    # compatible with the short path which has no children.
    all_children_succeeded: bool = True


# ── Safe target resolution ───────────────────────────────────────────────────


def safe_token_target(
    embed_cfg: Optional[Dict[str, Any]],
    *,
    default_max: int = DEFAULT_MAX_INPUT_TOKENS,
    safety_margin: int = DEFAULT_SAFETY_MARGIN,
) -> int:
    """Return the per-chunk token target.

    Lookup is tolerant: we accept a ``max_input_tokens`` override on the
    embed_cfg dict, fall back to ``default_max`` (8192), and always apply
    ``safety_margin``. The result is clamped to ``>= 1`` so a misconfigured
    ``safety_margin`` larger than ``default_max`` still produces a positive
    target (caller responsibility — we surface the value, not a refusal).

    The contract is documented in docs/QA-EMBEDDING-INDEX-DESIGN.md §3.
    """
    max_tokens = default_max
    if isinstance(embed_cfg, dict):
        raw = embed_cfg.get("max_input_tokens")
        if raw is None:
            raw = embed_cfg.get("max_seq_len")
        if isinstance(raw, (int, float)) and raw > 0:
            try:
                max_tokens = int(raw)
            except (TypeError, ValueError):
                max_tokens = default_max
    try:
        margin = int(safety_margin)
    except (TypeError, ValueError):
        margin = DEFAULT_SAFETY_MARGIN
    target = int(max_tokens) - int(margin)
    if target < 1:
        return 1
    return target


# ── Tokenizer backend selection ──────────────────────────────────────────────


def _load_tokenizers_lib(model_name: str) -> Any:
    """Load the raw ``tokenizers`` package tokenizer for ``model_name``.

    Production runtime declares ``tokenizers`` (the HuggingFace Rust-based
    binding) but does NOT necessarily ship ``transformers``. The raw
    ``tokenizers.Tokenizer.from_pretrained`` is the supported runtime
    path for BAAI/bge-m3; the ``encode(..., return_offsets_mapping=True)``
    contract is stable across minor versions we depend on.

    Returns the tokenizer object on success, ``None`` on any failure so
    the long-QA path can fall through to the next backend.
    """
    if not model_name:
        return None
    try:
        from tokenizers import Tokenizer  # type: ignore
    except Exception:
        return None
    try:
        return Tokenizer.from_pretrained(model_name)
    except Exception:
        return None


# ── Tokenizer cache ──────────────────────────────────────────────────────────
#
# 2026-09-18 v0.2 closing round: ``Tokenizer.from_pretrained`` is cached
# per model name so the QA write path never re-downloads / re-loads the
# model on every short QA. Short QA flushes don't go through the
# long-QA path at all (no tokenizer call), but the long path's
# per-QA ``_encode_field_offsets`` would otherwise pay the load cost on
# every chunking invocation. The cache is keyed by model name and is
# thread-safe under a single ``threading.Lock`` because the v3-core
# flush path is single-threaded per process for the long path; the lock
# is here for future parallel-flush work and to make the contract
# explicit.
#
# Cache misses run the canonical resolution order:
#   1. ``tokenizers.Tokenizer.from_pretrained`` (raw binding, BGE-
#      canonical production path).
#   2. ``transformers.AutoTokenizer.from_pretrained`` (alternate).
# Either is acceptable; the first that succeeds is what subsequent
# short-QA / long-QA calls reuse.

_TOKENIZER_CACHE: Dict[str, Any] = {}
_TOKENIZER_CACHE_LOCK = threading.Lock()


def _load_or_get_tokenizer(model_name: str) -> Any:
    """Return a cached tokenizer for ``model_name`` or load + cache it.

    Returns ``None`` if neither backend can load the tokenizer; the
    caller then surfaces :class:`TokenizerUnavailableError`.
    """
    if not model_name:
        return None
    cached = _TOKENIZER_CACHE.get(model_name)
    if cached is not None:
        return cached
    with _TOKENIZER_CACHE_LOCK:
        cached = _TOKENIZER_CACHE.get(model_name)
        if cached is not None:
            return cached
        tokenizer = _load_tokenizers_lib(model_name)
        if tokenizer is None:
            tokenizer = _load_auto_tokenizer(model_name)
        if tokenizer is not None:
            _TOKENIZER_CACHE[model_name] = tokenizer
        return tokenizer


def _reset_tokenizer_cache() -> None:
    """Test seam: clear the module-level tokenizer cache.

    Production callers MUST NOT invoke this; it exists so unit tests can
    exercise the cache miss / cache hit / model-swap transitions without
    leaking state between cases.
    """
    with _TOKENIZER_CACHE_LOCK:
        _TOKENIZER_CACHE.clear()


def _load_auto_tokenizer(model_name: str) -> Any:
    """Alternate backend: ``transformers.AutoTokenizer(use_fast=True)``.

    Returns the tokenizer on success, ``None`` on any failure so the
    long-QA path can fall through to ``char_estimate`` (only when the
    caller explicitly opted in via ``tokenizer_override="char_estimate"``;
    otherwise the path is fail-closed).
    """
    if not model_name:
        return None
    try:
        from transformers import AutoTokenizer  # type: ignore
    except Exception:
        return None
    try:
        return AutoTokenizer.from_pretrained(model_name, use_fast=True)
    except Exception:
        return None


def _char_count_estimator() -> Callable[[str], int]:
    """Conservative character-based estimator for opt-in use only.

    2 chars per token is the empirical worst-case observed for BGE on
    mixed CJK content; pure English is closer to 4. We intentionally use
    2 so chunks stay safely below the real provider limit even if the
    production tokenizer is unavailable in this environment.

    This estimator is **never** the default. Callers must opt in by
    passing ``tokenizer_override="char_estimate"``.
    """
    def _estimate(text: str) -> int:
        if not text:
            return 0
        return max((len(text) + 1) // 2, 1)
    return _estimate


def _normalize_offsets(
    encoding: Any,
    expected_text: str,
) -> Tuple[List[int], List[Tuple[int, int]], List[bool]]:
    """Return ``(ids, offsets, is_real)`` for ``encoding`` matching ``expected_text``.

    ``is_real[i]`` is True iff token ``i`` corresponds to at least one
    character of ``expected_text`` (its offset span is non-empty). The
    splitter uses this flag to skip special tokens (BOS/EOS/PAD) that the
    tokenizer prepends/appends and which would otherwise inflate the
    target token count without contributing source characters.

    Normalizes the encoding result across the two backends:
      - ``tokenizers``: ``encoding.ids`` + ``encoding.offsets``.
      - ``transformers``: tokenizer call returns
          ``BatchEncoding`` whose ``encodings[0].ids`` and
          ``encodings[0].offsets`` give the same shape.

    Raises ``TokenizerUnavailableError`` if the encoding shape cannot be
    interpreted or if the offsets don't cover the field, so silent data
    loss cannot happen.
    """
    ids: Optional[Sequence[int]] = None
    offsets: Optional[Sequence[Tuple[int, int]]] = None
    try:
        ids = list(encoding.ids)  # type: ignore[attr-defined]
        offsets = list(encoding.offsets)  # type: ignore[attr-defined]
    except Exception:
        pass
    if ids is None or offsets is None:
        # transformers BatchEncoding path
        try:
            inner = encoding.encodings  # type: ignore[attr-defined]
            if inner:
                ids = list(inner[0].ids)
                offsets = list(inner[0].offsets)
        except Exception:
            pass
    if ids is None or offsets is None:
        raise TokenizerUnavailableError(
            "tokenizer returned an encoding shape this helper cannot "
            "interpret (no .ids / .offsets / .encodings)"
        )
    if len(ids) != len(offsets):
        raise TokenizerUnavailableError(
            f"tokenizer encoding id/offset length mismatch: "
            f"{len(ids)} ids vs {len(offsets)} offsets"
        )
    is_real: List[bool] = []
    n_chars = len(expected_text)
    for i, (s, e) in enumerate(offsets):
        if s < 0 or e < s or s > n_chars or e > n_chars:
            raise TokenizerUnavailableError(
                f"tokenizer offset {i} outside source bounds ({s}/{e}, chars={n_chars})"
            )
        # BGE/XLM-R wordpieces may legitimately overlap (e.g. multiple
        # tokens sharing the first Chinese character). EOS/BOS/PAD often
        # use (0, 0). Neither case is source loss; chunking counts tokens
        # but slices the original source by the union of offsets.
        is_real.append(e > s)
    return list(ids), [(int(s), int(e)) for s, e in offsets], is_real


def _make_counter_for_field(
    encoder: Any,
    expected_text: str,
) -> Callable[[str], int]:
    """Build a callable that counts tokens for any substring of ``expected_text``.

    The full field is encoded **once**; per-chunk token counts are then
    derived by slicing the cached (ids, offsets) pair without re-running
    the tokenizer. The result is a callable suitable for the
    ``count_tokens`` interface used by the splitter.

    For long fields, this turns what would be O(n²) (re-tokenizing every
    growing prefix) into O(n) (one full tokenization + O(chunks) slicing).
    """
    encoding = _c_wrapper_call(encoder, expected_text)
    ids, offsets = _normalize_offsets(encoding, expected_text)
    n_tokens = len(ids)

    def _count(substring: str) -> int:
        if not substring:
            return 0
        # Find the token index range that covers ``substring`` exactly.
        # Because the offsets came from tokenizing ``expected_text`` and
        # ``substring`` is a prefix-aligned slice, we can binary-search
        # the offsets; linear scan is also acceptable. We linearize over
        # the token list once per chunk, but never re-tokenize.
        if substring == expected_text:
            return n_tokens
        # Locate start: first offset whose end >= 1 (i.e. first token
        # that produced a non-empty span).
        start_idx = 0
        end_idx = n_tokens
        target_end = len(substring)
        # Linear walk — n_tokens is bounded by target_tokens so this is
        # at most a few thousand comparisons even on long fields.
        for i, (_s, e) in enumerate(offsets):
            if e >= target_end:
                end_idx = i + 1
                break
        else:
            end_idx = n_tokens
        # ``start_idx`` is 0 for prefix-aligned slices; if the substring
        # is not prefix-aligned, we still recover correctness because the
        # caller uses our split which IS prefix-aligned.
        return end_idx - start_idx

    return _count


def _c_wrapper_call(encoder: Any, text: str) -> Any:
    """Invoke the tokenizer's encode-with-offsets and return the encoding.

    Tolerates the two backends' slightly different ``return_offsets_mapping``
    argument spelling (transformers bool, tokenizers bool).
    """
    if not text:
        # Encode an empty string to keep ids/offsets shape consistent.
        text = ""
    # Fast path: ``tokenizers.Tokenizer.encode``
    encode_fn = getattr(encoder, "encode", None)
    if callable(encode_fn):
        try:
            return encode_fn(text, return_offsets_mapping=True)
        except TypeError:
            # Some transformers wrappers take the kwarg differently
            return encode_fn(text)
        except Exception:
            pass
    # transformers callable path: __call__(text, return_offsets_mapping=True)
    try:
        return encoder(text, return_offsets_mapping=True)
    except TypeError:
        return encoder(text)
    except Exception as e:
        raise TokenizerUnavailableError(
            f"tokenizer encode call failed: {e!r}"
        ) from e


def _get_token_counter(
    embed_cfg: Optional[Dict[str, Any]],
    *,
    tokenizer_override: Optional[Any],
) -> Callable[[str], int]:
    """Resolve the token-counting strategy for the long-QA path.

    Resolution order (smallest compatibility-preserving choice first):

      1. ``tokenizer_override`` (callable): use as-is. Operators who want
         a deterministic unit-test path inject a callable. No I/O.
      2. ``tokenizer_override`` ("char_estimate" sentinel): opt-in
         conservative heuristic. NOT default; only when the caller
         explicitly asks for it (used by tests / offline analysis).
      3. No override, ``embed_cfg["model"]`` set: attempt to load the
         raw ``tokenizers.Tokenizer.from_pretrained`` first (the
         canonical production backend for BAAI/bge-m3). Fall back to
         ``transformers.AutoTokenizer`` if the raw binding is absent.
         If both backends fail to load, **fail closed** with
         :class:`TokenizerUnavailableError` — we MUST NOT send an unsafe
         over-limit request just because the tokenizer couldn't load.
      4. No override and no model: fail closed.

    Note: the returned counter is a closure that depends on the encoded
    text, so callers that need per-field counts should go through
    :func:`_make_counter_for_field` instead. This function returns a
    safe global "encode + count" callable for the short-text probe path.
    """
    if callable(tokenizer_override):
        counter = tokenizer_override
        return lambda text: max(int(counter(text) or 0), 0)

    if isinstance(tokenizer_override, str):
        sentinel = tokenizer_override.strip().lower()
        if sentinel == "char_estimate":
            return _char_count_estimator()

    # No override: try the real tokenizers in order.
    model_name = ""
    if isinstance(embed_cfg, dict):
        model_name = str(
            embed_cfg.get("tokenizer") or embed_cfg.get("model") or ""
        ).strip()
    if not model_name:
        raise TokenizerUnavailableError(
            "long-QA representation requires either a tokenizer override "
            "or embed_cfg['model']; both are empty"
        )

    tokenizer = _load_or_get_tokenizer(model_name)
    if tokenizer is None:
        raise TokenizerUnavailableError(
            f"tokenizer for model {model_name!r} could not be loaded "
            "(neither `tokenizers.Tokenizer.from_pretrained` nor "
            "`transformers.AutoTokenizer.from_pretrained` succeeded) "
            "— refusing to send an unsafe over-limit request"
        )

    def _encode_count(text: str) -> int:
        if not text:
            return 0
        try:
            encoding = _c_wrapper_call(tokenizer, text)
        except TokenizerUnavailableError:
            raise
        except Exception as e:
            raise TokenizerUnavailableError(
                f"tokenizer for model {model_name!r} raised during encode: "
                f"{e!r}"
            ) from e
        _ids, _offsets, is_real = _normalize_offsets(encoding, text)
        # ``is_real`` excludes special tokens (BOS/EOS/PAD) whose offsets
        # are (0,0). The provider-visible token count matches what the
        # splitter sees — no inflation from specials.
        return sum(1 for r in is_real if r)

    return _encode_count


# ── Span helpers ─────────────────────────────────────────────────────────────


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_field_with_offsets(
    field_name: str,
    field_text: str,
    *,
    offsets: Sequence[Tuple[int, int]],
    is_real: Sequence[bool],
    target_tokens: int,
    starting_chunk_index: int,
) -> List[EmbedChunk]:
    """Slice one QA field into token-safe non-overlapping spans.

    Algorithm (linear in field length, gap-preserving, coverage-complete):
      1. The caller already encoded ``field_text`` once and produced
         ``offsets`` (start, end) pairs covering the whole field, with
         ``is_real[i]`` True iff token ``i`` corresponds to at least one
         character of ``expected_text``.
      2. We walk the offset list once. The chunk's token count grows as
         we extend past consecutive tokens, including special tokens
         (those whose ``is_real`` is False); the chunk's source span
         extends from the FIRST real token's ``start`` to the LAST real
         token's ``end``. Special tokens contribute to the token count
         (matching what the provider will see) but do NOT extend the
         source slice. Whitespace / character gaps between non-adjacent real
         tokens ARE preserved because the slice is over the original text.
      3. A single real token larger than the target is still emitted as
         its own 1-real-token chunk so the caller can observe the
         boundary rather than silently dropping content.
      4. **Coverage enforcement**: if the encoding's first real token
         does not start at character 0, any leading characters are
         prepended to the first chunk (the embedding provider still
         receives them as part of the chunk text). If the last real
         token does not end at the field length, any trailing
         characters are appended to the last chunk. This guarantees the
         union of all chunk ``text`` fields equals ``field_text`` byte-
         for-byte — leading whitespace, trailing whitespace, and any
         characters the tokenizer mapped to (0,0) specials are all
         preserved without re-tokenization.

    Spans are non-overlapping character slices of the original field
    text. No rewriting, no truncation (every char — including leading,
    trailing, and inter-token whitespace — is emitted in exactly one
    chunk).
    """
    if not field_text or not offsets:
        return []

    n_tokens = len(offsets)
    n_chars = len(field_text)
    # Find first / last real token to anchor the coverage expansion.
    first_real_idx: Optional[int] = None
    last_real_idx: Optional[int] = None
    for k in range(n_tokens):
        if is_real[k]:
            if first_real_idx is None:
                first_real_idx = k
            last_real_idx = k
    # Compute the chunking walk; this produces spans that may not cover
    # [0, n_chars) if the tokenizer left leading/trailing chars
    # unmapped. We patch the boundaries below to enforce full coverage.
    out: List[EmbedChunk] = []
    next_index = starting_chunk_index
    i = 0
    while i < n_tokens:
        # Skip leading specials (they don't contribute source chars and
        # don't count toward the chunk budget here — they would inflate
        # the per-chunk token count past the provider-visible limit).
        # We re-emit specials as part of the first real-token chunk
        # via the coverage expansion below.
        if not is_real[i]:
            i += 1
            continue
        first_real = i
        chunk_start_token = first_real
        chunk_start_char = offsets[first_real][0]
        chunk_end_token = chunk_start_token + 1
        # Find the last real token's end. Initialize to the first real
        # token's end; extend as we add tokens.
        chunk_end_char = offsets[first_real][1]
        # Extend while staying under target_tokens. Special tokens count
        # toward the token budget but don't move chunk_end_char.
        j = first_real + 1
        while j < n_tokens:
            tentative_count = (j + 1) - chunk_start_token
            if tentative_count > target_tokens:
                break
            if is_real[j]:
                chunk_end_char = offsets[j][1]
            chunk_end_token = j + 1
            j += 1
        if chunk_end_char > n_chars:
            chunk_end_char = n_chars
        if chunk_end_token == chunk_start_token:
            chunk_end_token = min(first_real + 1, n_tokens)
            chunk_end_char = offsets[chunk_end_token - 1][1]
        span_text = field_text[chunk_start_char:chunk_end_char]
        token_count = chunk_end_token - chunk_start_token
        out.append(
            EmbedChunk(
                chunk_index=next_index,
                source_field=field_name,
                source_start=chunk_start_char,
                source_end=chunk_end_char,
                source_sha256=_sha256_hex(span_text),
                token_count=token_count,
                text=span_text,
                embed_text=span_text,
            )
        )
        next_index += 1
        i = chunk_end_token
        # Gap-preservation across chunks: if there is a NEXT real token
        # (i.e. ``i`` lands at or before a real token), extend the
        # previous chunk's end_char to cover the gap between this
        # chunk's last real-token end and the next real-token start. We
        # never silently drop any source characters — leading /
        # trailing whitespace, inter-token whitespace, and word-piece
        # joins must all survive the split.
        if i < n_tokens:
            # Find the next real token at or after ``i``.
            for k in range(i, n_tokens):
                if is_real[k]:
                    next_real_start = offsets[k][0]
                    if next_real_start > chunk_end_char:
                        # Extend the just-emitted chunk to cover the gap.
                        new_text = field_text[out[-1].source_start:next_real_start]
                        last = out[-1]
                        out[-1] = EmbedChunk(
                            chunk_index=last.chunk_index,
                            source_field=last.source_field,
                            source_start=last.source_start,
                            source_end=next_real_start,
                            source_sha256=_sha256_hex(new_text),
                            token_count=last.token_count,
                            text=new_text,
                            embed_text=new_text,
                        )
                        chunk_end_char = next_real_start
                    break

    # ── Coverage enforcement ─────────────────────────────────────────────
    # The provider returns offsets over the actual characters that were
    # tokenized. If the field's first / last characters are mapped to
    # specials (BOS/EOS etc.), the splitter's first/last chunk may not
    # cover the full [0, n_chars) range. We never silently drop those
    # characters — they are prepended/appended to the first/last chunk
    # respectively. This preserves the byte-exact source span.
    if not out:
        # All-special edge case (no real tokens). We still emit ONE
        # chunk covering the entire field so coverage is byte-exact.
        # Token count = n_tokens (specials count toward the budget per
        # the splitter's contract).
        out.append(
            EmbedChunk(
                chunk_index=next_index,
                source_field=field_name,
                source_start=0,
                source_end=n_chars,
                source_sha256=_sha256_hex(field_text),
                token_count=n_tokens,
                text=field_text,
                embed_text=field_text,
            )
        )
        return out
    if first_real_idx is not None and last_real_idx is not None:
        first_char = offsets[first_real_idx][0]
        if first_char > 0:
            # Prepend leading chars to the first chunk.
            head = field_text[:first_char]
            new_text = head + out[0].text
            first = out[0]
            out[0] = EmbedChunk(
                chunk_index=first.chunk_index,
                source_field=first.source_field,
                source_start=0,
                source_end=first.source_end,
                source_sha256=_sha256_hex(new_text),
                token_count=first.token_count,
                text=new_text,
                embed_text=new_text,
            )
        last_char = offsets[last_real_idx][1]
        if last_char < n_chars:
            tail = field_text[last_char:]
            new_text = out[-1].text + tail
            last = out[-1]
            new_end = n_chars
            out[-1] = EmbedChunk(
                chunk_index=last.chunk_index,
                source_field=last.source_field,
                source_start=last.source_start,
                source_end=new_end,
                source_sha256=_sha256_hex(new_text),
                token_count=last.token_count,
                text=new_text,
                embed_text=new_text,
            )
    else:
        # Either no real tokens at all (handled above with ``not out``) or
        # only specials at one boundary. The walk above already covers
        # all tokens; if any real token was processed but its offset
        # starts after 0 or ends before n_chars, the gap / tail would
        # be silent-dropped. Patch the first/last real-token span onto
        # the first/last emitted chunk.
        if first_real_idx is None and last_real_idx is None:
            pass  # already handled by the ``not out`` branch
        elif first_real_idx is None:
            # Only specials at start; walk produced real content but the
            # first chunk starts at offsets[last_real_idx] (or later).
            # Patch: prepend leading chars.
            first_char = 0  # nothing to prepend
            tail_text = field_text[offsets[last_real_idx][1]:]
            if tail_text:
                new_text = out[-1].text + tail_text
                last = out[-1]
                out[-1] = EmbedChunk(
                    chunk_index=last.chunk_index,
                    source_field=last.source_field,
                    source_start=last.source_start,
                    source_end=n_chars,
                    source_sha256=_sha256_hex(new_text),
                    token_count=last.token_count,
                    text=new_text,
                    embed_text=new_text,
                )
        elif last_real_idx is None:
            # Only specials at end.
            first_char = offsets[first_real_idx][0]
            if first_char > 0:
                head = field_text[:first_char]
                new_text = head + out[0].text
                first = out[0]
                out[0] = EmbedChunk(
                    chunk_index=first.chunk_index,
                    source_field=first.source_field,
                    source_start=0,
                    source_end=first.source_end,
                    source_sha256=_sha256_hex(new_text),
                    token_count=first.token_count,
                    text=new_text,
                    embed_text=new_text,
                )
    return out


def _encode_field_offsets(
    field_text: str,
    embed_cfg: Optional[Dict[str, Any]],
    *,
    tokenizer_override: Optional[Any],
) -> Tuple[List[Tuple[int, int]], List[bool]]:
    """Encode ``field_text`` once and return ``(offsets, is_real)``.

    ``offsets[i] = (start_char, end_char)`` for token ``i``. ``is_real[i]``
    is True iff token ``i`` corresponds to at least one source character
    (the (0,0) special-token slots are flagged False).

    The token count seen by the splitter is the number of True entries
    in ``is_real``; the per-chunk source span is computed from the
    FIRST and LAST real token positions only, so special tokens inflate
    the budget without extending the source slice.
    """
    # Resolution order matches _get_token_counter: callable / char
    # estimate / real tokenizer. Each backend produces its own (offsets,
    # is_real) shape.
    if callable(tokenizer_override):
        return _callable_offsets_for(field_text, tokenizer_override)
    if isinstance(tokenizer_override, str) and tokenizer_override.strip().lower() == "char_estimate":
        return _char_estimate_offsets(field_text)
    # Real tokenizer.
    model_name = ""
    if isinstance(embed_cfg, dict):
        model_name = str(
            embed_cfg.get("tokenizer") or embed_cfg.get("model") or ""
        ).strip()
    if not model_name:
        raise TokenizerUnavailableError(
            "long-QA representation requires either a tokenizer override "
            "or embed_cfg['model']; both are empty"
        )
    tokenizer = _load_or_get_tokenizer(model_name)
    if tokenizer is None:
        raise TokenizerUnavailableError(
            f"tokenizer for model {model_name!r} could not be loaded"
        )
    try:
        encoding = _c_wrapper_call(tokenizer, field_text)
    except Exception as e:
        raise TokenizerUnavailableError(
            f"tokenizer for model {model_name!r} raised during encode: "
            f"{e!r}"
        ) from e
    _ids, offsets, is_real = _normalize_offsets(encoding, field_text)
    return list(offsets), list(is_real)


def _callable_offsets_for(
    field_text: str,
    counter: Callable[[str], int],
) -> Tuple[List[Tuple[int, int]], List[bool]]:
    """Approximate offsets for a callable counter (unit-test seam only).

    Token boundaries cannot be recovered from a black-box callable, so we
    approximate each token as one character. This is intentionally
    lossy: callable backends are unit-test seams, not production paths.
    Real production deployments use the cached ``tokenizers`` backend.
    """
    n = len(field_text)
    offsets: List[Tuple[int, int]] = [(i, i + 1) for i in range(n)]
    is_real = [True] * n
    return offsets, is_real


def _char_estimate_offsets(
    field_text: str,
) -> Tuple[List[Tuple[int, int]], List[bool]]:
    """Char-estimate backend offsets: 2 chars per token.

    Used only when the caller explicitly opts in via
    ``tokenizer_override="char_estimate"``.
    """
    n = len(field_text)
    offsets: List[Tuple[int, int]] = []
    is_real: List[bool] = []
    i = 0
    while i < n:
        end = min(n, i + 2)
        offsets.append((i, end))
        is_real.append(True)
        i = end
    return offsets, is_real


# ── Public API ───────────────────────────────────────────────────────────────


# ── Generic single-field span API ──────────────────────────────────────────


def split_text_into_token_safe_spans(
    field_name: str,
    field_text: str,
    embed_cfg: Optional[Dict[str, Any]],
    *,
    tokenizer_override: Optional[Any] = None,
    target_tokens: Optional[int] = None,
    starting_chunk_index: int = 0,
) -> List[EmbedChunk]:
    """Split one free-text field into token-safe non-overlapping spans.

    Thin seam over :func:`_encode_field_offsets` +
    :func:`_split_field_with_offsets` — no tokenizer algorithm is
    duplicated here. The long-QA path
    (:func:`split_into_token_safe_chunks`) and the long-observation
    planner (``v3core.observation_chunks``) share this entry so both
    stay byte-identical in split semantics.
    """
    if target_tokens is None:
        target_tokens = safe_token_target(embed_cfg)
    if not (field_text or ""):
        return []
    offsets, is_real = _encode_field_offsets(
        field_text, embed_cfg, tokenizer_override=tokenizer_override
    )
    return _split_field_with_offsets(
        field_name,
        field_text,
        offsets=offsets,
        is_real=is_real,
        target_tokens=target_tokens,
        starting_chunk_index=starting_chunk_index,
    )


def split_into_token_safe_chunks(
    question: str,
    answer: str,
    embed_cfg: Optional[Dict[str, Any]],
    *,
    tokenizer_override: Optional[Any] = None,
    target_tokens: Optional[int] = None,
) -> List[EmbedChunk]:
    """Return non-overlapping token-safe chunks covering ``question`` and ``answer``.

    Each :class:`EmbedChunk` covers a contiguous character span of the
    original field; the union of all chunks equals the concatenation of
    ``question + answer``.  Source content is **never truncated or
    rewritten**.

    Raises :class:`TokenizerUnavailableError` when the tokenizer cannot
    be resolved (no override + no model + no transformers, or model fetch
    failure). The caller must treat this as a durable failure.
    """
    q = question or ""
    a = answer or ""
    if target_tokens is None:
        target_tokens = safe_token_target(embed_cfg)

    chunks: List[EmbedChunk] = []
    if q:
        offsets, is_real = _encode_field_offsets(
            q, embed_cfg, tokenizer_override=tokenizer_override
        )
        chunks.extend(
            _split_field_with_offsets(
                "question",
                q,
                offsets=offsets,
                is_real=is_real,
                target_tokens=target_tokens,
                starting_chunk_index=0,
            )
        )
    if a:
        offsets, is_real = _encode_field_offsets(
            a, embed_cfg, tokenizer_override=tokenizer_override
        )
        # Answer chunks continue the global chunk_index sequence so the
        # (qa_id, chunk_index) UNIQUE invariant stays dense and ordered.
        starting = len(chunks)
        chunks.extend(
            _split_field_with_offsets(
                "answer",
                a,
                offsets=offsets,
                is_real=is_real,
                target_tokens=target_tokens,
                starting_chunk_index=starting,
            )
        )
    return chunks


def aggregate_parent_embedding(child_vecs: List[List[float]]) -> List[float]:
    """L2-normalized mean of successful child vectors.

    The mean is computed component-wise. The result is then L2-normalized
    so the parent vector lives on the same unit hypersphere the BGE
    provider returns.

    Failure semantics (2026-09-18, v0.2 closing round):
      * ``ValueError`` if ``child_vecs`` is empty — the caller did not
        collect any successful child vectors; we refuse to fabricate a
        placeholder. The QA path that called us MUST mark the row as a
        durable failure (retryable marker), not as fully durable.
      * ``ValueError`` if any child vector is zero / all-zero — consistent
        with the ``call_embedding`` / ``embed_batch`` no-zero-vector
        contract; a zero vector means the upstream pipeline rejected the
        request and never produced a real embedding, so aggregating it
        would silently produce a garbage parent. Surface the failure.
      * ``ValueError`` on mixed dimensions.

    The mean and normalization are pure-Python / NumPy-free to keep the
    helper import-cheap (the ``embedding`` module is already a hot path).
    """
    if not child_vecs:
        raise ValueError(
            "aggregate_parent_embedding: child_vecs is empty — refusing to "
            "fabricate a placeholder parent vector; the QA row must be "
            "marked as a retryable failure, not fully durable"
        )
    dim = len(child_vecs[0])
    if dim == 0:
        raise ValueError(
            "aggregate_parent_embedding: first child vector has zero "
            "dimension; refusing to fabricate a placeholder"
        )
    accum = [0.0] * dim
    for vec_idx, vec in enumerate(child_vecs):
        if len(vec) != dim:
            # Caller passed mixed dimensions — surface as ValueError
            # rather than silently truncating.
            raise ValueError(
                f"aggregate_parent_embedding: child vector {vec_idx} dim "
                f"mismatch (expected {dim}, got {len(vec)}); refusing to "
                f"aggregate"
            )
        # Reject zero / all-zero vectors — consistent with embed_batch's
        # no-zero-vector contract. A zero vector means the upstream
        # pipeline never produced a real embedding; aggregating it would
        # silently bias the parent.
        is_zero = True
        for i, v in enumerate(vec):
            fv = float(v)
            accum[i] += fv
            if fv != 0.0:
                is_zero = False
        if is_zero:
            raise ValueError(
                f"aggregate_parent_embedding: child vector {vec_idx} is "
                f"all-zero; refusing to aggregate a zero vector (violates "
                "the no-zero-vector contract). The upstream embed call "
                "must be retried or marked as a durable failure."
            )
    n = float(len(child_vecs))
    mean = [v / n for v in accum]
    norm = math.sqrt(sum(v * v for v in mean))
    if norm <= 0.0:
        # Degenerate: every child was zero (already rejected above for the
        # first zero child, but if n children all contributed ~0 we may
        # still hit norm=0). Surface as a failure.
        raise ValueError(
            "aggregate_parent_embedding: mean vector has zero L2 norm — "
            "no usable child vectors; refusing to fabricate a placeholder"
        )
    return [v / norm for v in mean]


def build_qa_embedding_representation(
    question: str,
    answer: str,
    embed_cfg: Optional[Dict[str, Any]],
    tokenizer_name: Optional[str] = None,
    *,
    short_text_for_emb: Optional[str] = None,
    tokenizer_override: Optional[Any] = None,
    target_tokens: Optional[int] = None,
) -> LongQARepresentation:
    """High-level entry: pick short or long path based on the tokenizer count.

    ``short_text_for_emb`` is the exact string the existing short path
    sends today (``question + "\\n" + answer``). When its token count is
    at or below the safe target we return ``is_long=False`` and an empty
    ``chunks`` list — the caller re-uses the existing single-text
    embedding path unchanged.

    When ``short_text_for_emb`` exceeds the safe target, we split the QA
    into token-safe non-overlapping chunks and return
    ``is_long=True`` with empty ``embedding`` and a populated
    ``chunks`` list. The caller is expected to embed each chunk,
    aggregate the parent via :func:`aggregate_parent_embedding`, and
    persist the chunks to the additive ``qa_embedding_chunks`` table.

    Fail-closed: :class:`TokenizerUnavailableError` propagates.

    ``tokenizer_name`` is accepted for API symmetry / future use; today
    it is shadowed by ``tokenizer_override``. Kept as a keyword so the
    call site doesn't churn when the tokenizer selection becomes config
    driven.
    """
    # Silence unused-arg lint for an explicit API parameter; the
    # override path is the canonical seam today.
    _ = tokenizer_name
    short_text = short_text_for_emb if short_text_for_emb is not None else (
        f"{question or ''}\n{answer or ''}" if answer else (question or "")
    )
    if target_tokens is None:
        target_tokens = safe_token_target(embed_cfg)
    # Fast proof gate for ordinary short QA: a tokenizer cannot emit more
    # tokens than UTF-8 bytes for the model family used here, and this gate
    # leaves four special-token slots. This is NOT a token estimate and is
    # never used to split or send a borderline/long request. It keeps the
    # historical short-QA path independent of a HuggingFace tokenizer cache.
    if len(short_text.encode("utf-8")) + 4 <= target_tokens:
        return LongQARepresentation(
            is_long=False,
            embedding=[],
            chunks=[],
            embed_text=short_text,
        )
    counter = _get_token_counter(embed_cfg, tokenizer_override=tokenizer_override)
    try:
        short_tokens = int(counter(short_text) or 0)
    except Exception as e:
        raise TokenizerUnavailableError(
            f"tokenizer raised during short-text probe: {e!r}"
        ) from e
    if short_tokens <= target_tokens:
        return LongQARepresentation(
            is_long=False,
            embedding=[],
            chunks=[],
            embed_text=short_text,
        )
    chunks = split_into_token_safe_chunks(
        question or "",
        answer or "",
        embed_cfg,
        tokenizer_override=tokenizer_override,
        target_tokens=target_tokens,
    )
    return LongQARepresentation(
        is_long=True,
        embedding=[],
        chunks=chunks,
    )

# ── Query embedding representation (A02 / P2-2) ─────────────────────────────
#
# A retrieval query is a *representation* the caller may cap; durable source text is not (source
# lives on the chunked derived-index path). This helper is the single place where a query may be
# reduced, so no call site invents its own `[:N]` slice.

QUERY_TRUNCATION_STRATEGY = "head60_tail40"
QUERY_IDENTITY_STRATEGY = "identity"
QUERY_HEAD_RATIO = 0.6
QUERY_TAIL_RATIO = 0.4
QUERY_OMISSION_SEPARATOR = "\n"


@dataclass(frozen=True)
class QueryEmbeddingText:
    """Result of :func:`prepare_query_embedding_text` — counts only, never content."""

    text: str
    original_tokens: int
    prepared_tokens: int
    truncated: bool
    strategy: str


def prepare_query_embedding_text(
    query: str,
    embed_cfg: Optional[Dict[str, Any]],
    *,
    tokenizer_override: Optional[Any] = None,
    target_tokens: Optional[int] = None,
) -> QueryEmbeddingText:
    """Token-safe representation for a *retrieval query* embedding call.

    Contract:

    * ``token_count(query) <= target`` → the text is returned **byte-for-byte** (no strip, no
      normalisation, no marker, no re-encoding). Hard contract.
    * ``> target`` → head ≈ 60% / tail ≈ 40% of the budget, joined by a minimal non-semantic
      separator, original order preserved, the middle omitted, and the join re-counted. If it still
      exceeds the target the budgets shrink deterministically at the same 60/40 ratio until it fits
      — never by probing the provider for a 400.
    * Never used for durable source text (messages / conversation_stream / observation / QA / yin /
      topic / explicit memory). Those keep their canonical slice + chunked derived index.

    Token accounting comes from the shared planning helpers; this function adds no tokenizer of its
    own.
    """
    original = query if isinstance(query, str) else ("" if query is None else str(query))
    target = max(1, int(target_tokens if target_tokens is not None
                       else safe_token_target(embed_cfg)))
    counter = _get_token_counter(embed_cfg, tokenizer_override=tokenizer_override)
    original_tokens = int(counter(original))

    if original_tokens <= target:
        return QueryEmbeddingText(
            text=original,
            original_tokens=original_tokens,
            prepared_tokens=original_tokens,
            truncated=False,
            strategy=QUERY_IDENTITY_STRATEGY,
        )

    offsets, _is_real = _encode_field_offsets(original, embed_cfg,
                                              tokenizer_override=tokenizer_override)
    n_tokens = len(offsets) or 1
    sep_tokens = max(0, int(counter(QUERY_OMISSION_SEPARATOR)))
    budget = target - sep_tokens

    def _head_only(limit_tokens: int) -> str:
        limit = max(1, min(limit_tokens, n_tokens))
        return original[: offsets[limit - 1][1]]

    if budget < 2:
        # Degenerate target (misconfigured margin): a deterministic head window that still respects
        # the cap beats emitting something over the window.
        head_only = _head_only(target)
        return QueryEmbeddingText(
            text=head_only,
            original_tokens=original_tokens,
            prepared_tokens=int(counter(head_only)),
            truncated=True,
            strategy=QUERY_TRUNCATION_STRATEGY,
        )

    head_budget = min(max(1, int(budget * QUERY_HEAD_RATIO)), n_tokens - 1)
    tail_budget = min(max(1, budget - int(budget * QUERY_HEAD_RATIO)), n_tokens - head_budget)
    prepared = ""
    prepared_tokens = 0

    for _attempt in range(8):
        head_text = original[: offsets[head_budget - 1][1]]
        tail_text = original[offsets[n_tokens - tail_budget][0]:]
        prepared = f"{head_text}{QUERY_OMISSION_SEPARATOR}{tail_text}"
        prepared_tokens = int(counter(prepared))
        if prepared_tokens <= target:
            break
        shrink = (prepared_tokens - target) + 1
        shrink_head = max(1, int(shrink * QUERY_HEAD_RATIO))
        shrink_tail = max(1, shrink - shrink_head) if shrink > 1 else 0
        new_head = max(1, head_budget - shrink_head)
        new_tail = max(1, tail_budget - shrink_tail)
        if new_head == head_budget and new_tail == tail_budget:
            break
        head_budget, tail_budget = new_head, new_tail

    if prepared_tokens > target:
        # Final clamp: unreachable with production targets (margin 512); kept so the cap holds even
        # under a pathological configuration.
        prepared = _head_only(target)
        prepared_tokens = int(counter(prepared))

    return QueryEmbeddingText(
        text=prepared,
        original_tokens=original_tokens,
        prepared_tokens=prepared_tokens,
        truncated=True,
        strategy=QUERY_TRUNCATION_STRATEGY,
    )
