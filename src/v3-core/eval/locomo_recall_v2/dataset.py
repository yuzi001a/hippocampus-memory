"""LoCoMo eval_v2 loader (G6C-A dataset slice).

External-source-only. Reads the published LoCoMo JSON from disk,
verifies SHA-256, and emits a deterministic in-memory representation
that preserves every conversation message and every eval row.

The loader intentionally re-implements the tested ``eval_v2`` rules
from the historical build script without importing the private
repo at runtime. Public worktrees never see the private module.

Eval_v2 rules preserved here:

  * Source verification via SHA-256 before any parsing.
  * Numeric session ordering — sessions sort by integer index.
  * Both the **authoritative historical old-shape** LoCoMo dump
    (sibling ``session_N`` list + ``session_N_date_time`` string,
    no per-message ``id``, native ``"1:56 pm on 8 May, 2023"``
    datetime strings, image fields named ``img_url`` /
    ``blip_caption`` / ``query`` / ``re-download``) AND the newer
    dict-with-``date_time`` + ``messages`` shape are accepted. The
    two shapes are detected per session.
  * Raw session datetime string + a parsed UTC
    :class:`datetime.datetime` are kept side-by-side.
  * Every conversation message survives with ``sample_id``,
    ``session_key``, ``index`` (within-session), ``speaker``,
    ``dia_id`` (the original message id), ``text`` (full,
    **never truncated**), the original image fields (preserved
    verbatim under the historical names: ``img_url`` /
    ``blip_caption`` / ``query`` / ``re-download``), and the
    raw session datetime.
  * Same-session adjacent stride-2 QA pairing:
    pairs ``(q_dia_id, a_dia_id)`` per session in original order.
  * Stable source_id:
    ``locomo|eval_v2|sample_id|session_key|q_dia_id>a_dia_id``.
  * Provenance for both ``q_dia_id`` and ``a_dia_id``.
  * No text truncation. If an explicit input-limit is exceeded
    the loader fails closed — it never silently drops text.
  * Eval rows preserve ``question`` / ``answer`` / ``category`` /
    ``evidence`` and a per-row ``source_hash``. Where the source
    omits ``id`` (the old historical shape), the row's stable
    identity is the within-sample ``query_idx`` (``q0``, ``q1``,
    ``q2`` …) — the loader emits ``qa_id = "q{query_idx}"`` for
    downstream use.
  * ``LoCoMoQAPair`` carries the actual question / answer text
    and a parsed UTC timestamp, so the lab importer can build
    qa_pairs deterministically from the dataset without
    re-resolving eval evidence.

Two deterministic helpers:

  * :func:`build_evidence_map` — maps every ``(sample_id,
    session_key, dia_id)`` to the list of source_ids whose QA
    pair contains that message. Unmapped dia IDs are kept
    explicit (a present key with an empty list).
  * :func:`resolve_gold_evidence` — resolves the gold evidence
    list for one eval row purely via the deterministic map;
    no fuzzy text matching. Compound / malformed evidence
    strings (whitespace, embedded punctuation, anything that is
    not a single canonical dia_id) are preserved verbatim and
    surface as ``unresolved``.

Public row-model conversion:

  * :func:`build_import_rows` — builds the three deterministic
    row lists ``qa_pairs``, ``conversation_stream`` and
    ``eval_queries`` ready for :func:`lab.import_rows`.
    ``sample_ids`` may narrow to a subset.

Only the standard library is used. No provider / network calls.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import os
import re
from collections import defaultdict
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LoCoMoSourceError(ValueError):
    """Raised when the LoCoMo source file is missing, malformed, or fails SHA-256."""


class LoCoMoInputLimitExceeded(ValueError):
    """Raised when the loader's explicit input-limit is exceeded.

    The contract is fail-closed: the loader never silently drops
    bytes. Callers that want truncation must opt in explicitly.
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class LoCoMoMessage:
    """One conversation message, preserved verbatim from the source.

    ``text`` is the **full** original message text. The loader
    does NOT truncate; see :class:`LoCoMoInputLimitExceeded`.

    Image fields use the historical LoCoMo names: ``img_url``,
    ``blip_caption``, ``query`` (the image-level query text), and
    ``re-download`` (a per-message re-download marker). They are
    preserved verbatim under these names so downstream consumers
    can recover the exact historical layout. ``None`` for any
    field that the source did not set.
    """

    sample_id: str
    session_key: str            # e.g. "session_1"
    index: int                  # within-session message index, 0-based
    speaker: str                # "speaker_1" / "speaker_2" / etc.
    dia_id: str                 # original message id from the source
    text: str
    # Preserved image fields (may be None for non-image messages).
    # Historical LoCoMo field names.
    img_url: str | None
    blip_caption: str | None
    img_query: str | None       # source "query"
    re_download: bool | None    # source "re-download"
    raw_session_dt: str         # raw "date_time" string from the source
    session_dt_utc: _dt.datetime  # parsed UTC datetime


@dataclasses.dataclass(frozen=True)
class LoCoMoQAPair:
    """One same-session adjacent stride-2 QA pair.

    The pair is stable across runs: ``q_dia_id`` and ``a_dia_id``
    are preserved verbatim, and the derived ``source_id`` follows
    the contract format.

    The pair carries the **full** text of both messages (not
    truncated) plus their image-field provenance, so the lab
    importer can build import rows without re-resolving the
    message graph. ``question`` / ``answer`` are populated only
    for pairs that are the canonical answer for an eval question
    (i.e. ``q_dia_id == eval_row.qa_dia_id``); pairs that are
    not the canonical answer for any eval question carry empty
    strings and a UTC sentinel timestamp. ``q_text`` / ``a_text``
    always carry the actual full message text from the source —
    callers can fall back to them when ``question`` / ``answer``
    are empty.
    """

    sample_id: str
    session_key: str
    q_dia_id: str
    a_dia_id: str
    source_id: str              # locomo|eval_v2|sample_id|session_key|q_dia_id>a_dia_id
    q_provenance: dict[str, Any]  # {"sample_id", "session_key", "dia_id", "index"}
    a_provenance: dict[str, Any]
    question: str               # actual question text (eval-linked only)
    answer: str                 # actual answer text   (eval-linked only)
    timestamp: _dt.datetime     # parsed UTC for eval-linked only
    q_text: str                 # full text of the q message
    a_text: str                 # full text of the a message
    q_image: dict[str, Any]     # image provenance for q message (empty if none)
    a_image: dict[str, Any]     # image provenance for a message (empty if none)


@dataclasses.dataclass(frozen=True)
class LoCoMoEvalRow:
    """One eval row, preserved with its evidence and a row source_hash.

    Where the historical source lacks an ``id`` field (the
    old-shape dump), the loader derives a stable ``qa_id``
    using the within-sample ``query_idx`` — ``"q0"``, ``"q1"``,
    ``"q2"``, … The ``query_idx`` is the 0-based position of the
    eval row in its sample's ``qa`` list. The full question /
    answer text and the canonical ``qa_pair`` link are preserved
    so the lab importer can build import rows without
    re-resolving evidence.
    """

    sample_id: str
    qa_id: str                  # "q{query_idx}" for old-shape, source id otherwise
    query_idx: int              # 0-based within-sample index; stable identity
    question: str
    answer: str
    category: str
    evidence: tuple[str, ...]   # original evidence list (tuple for hashability)
    source_hash: str            # SHA-256 of the row's normalised JSON payload
    qa_pair_source_id: str | None  # the paired QA source_id (if mapped)
    qa_dia_id: str | None       # canonical dia_id used to link the pair (if any)


@dataclasses.dataclass(frozen=True)
class LoCoMoDataset:
    """Top-level loader output."""

    source_path: str
    source_sha256: str
    source_bytes: int
    samples: tuple[tuple[str, "LoCoMoSample"], ...]  # sorted by sample_id
    eval_rows: tuple[LoCoMoEvalRow, ...]                          # sorted by (sample_id, qa_id)
    qa_pairs: tuple[LoCoMoQAPair, ...]                            # sorted for stability
    category_counts: dict[str, int]
    message_count: int
    qa_pair_count: int


@dataclasses.dataclass(frozen=True)
class LoCoMoSample:
    """One sample (one conversation), with its ordered sessions."""

    sample_id: str
    sessions: tuple[tuple[str, tuple[LoCoMoMessage, ...]], ...]  # sorted by session_key


# ---------------------------------------------------------------------------
# SHA-256 verification
# ---------------------------------------------------------------------------


def sha256_file(path: str) -> str:
    """Return the SHA-256 hex digest of a file's bytes.

    Reads the file in 64 KiB chunks so the loader stays safe
    for large LoCoMo dumps. Raises :class:`LoCoMoSourceError`
    on missing / unreadable files.
    """

    if not os.path.isfile(path):
        raise LoCoMoSourceError(f"LoCoMo source file not found: {path!r}")
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
    except OSError as exc:
        raise LoCoMoSourceError(
            f"LoCoMo source file unreadable: {path!r} (io error)"
        ) from exc
    return h.hexdigest()


def verify_source(path: str, expected_sha256: str) -> str:
    """Verify a LoCoMo source file against an expected SHA-256.

    Returns the computed digest; raises :class:`LoCoMoSourceError`
    if the file is missing or the digest does not match.
    """

    if not expected_sha256 or not isinstance(expected_sha256, str):
        raise LoCoMoSourceError(
            "verify_source: expected_sha256 must be a non-empty string"
        )
    expected = expected_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise LoCoMoSourceError(
            "verify_source: expected_sha256 must be a 64-char hex digest"
        )
    actual = sha256_file(path)
    if actual != expected:
        raise LoCoMoSourceError(
            f"LoCoMo SHA-256 mismatch for {path!r}: "
            f"expected {expected!r}, got {actual!r}"
        )
    return actual


# ---------------------------------------------------------------------------
# Stable source_id derivation
# ---------------------------------------------------------------------------


def make_source_id(sample_id: str, session_key: str, q_dia_id: str, a_dia_id: str) -> str:
    """Return the canonical eval_v2 source_id for a QA pair.

    Format (verbatim):
        ``locomo|eval_v2|<sample_id>|<session_key>|<q_dia_id>><a_dia_id>``

    The literal ">" separator mirrors the contract; surrounding
    pipes make the field unambiguous when nested into larger
    composite ids.
    """

    return f"locomo|eval_v2|{sample_id}|{session_key}|{q_dia_id}>{a_dia_id}"


def make_eval_query_source_id(sample_id: str, query_idx: int) -> str:
    """Return the canonical eval_v2 source_id for one eval question.

    Format (verbatim):
        ``locomo|eval_v2|<sample_id>|q<query_idx>``

    Used by ``build_import_rows`` so the ``eval_queries`` table
    carries a stable idempotency key.
    """

    return f"locomo|eval_v2|{sample_id}|q{int(query_idx)}"


# Canonical source_id regex — see :func:`make_source_id` for the
# emitting format. The capture groups match sample_id /
# session_key / q_dia_id / a_dia_id with the constraint that
# none of the four fields may contain the literal pipe ``|`` or
# the literal ``>`` separator. (A dia_id is a short alphanumeric
# token in the historical source; we accept any non-empty string
# without ``|`` or ``>`` to stay strictly reverse-compatible.)
_SOURCE_ID_RE = re.compile(
    r"^locomo\|eval_v2\|([^|>]+)\|([^|>]+)\|([^|>]+)>([^|>]+)$"
)


@dataclasses.dataclass(frozen=True)
class ParsedSourceID:
    """Result of :func:`parse_source_id` — the four canonical fields.

    ``sample_id`` / ``session_key`` / ``q_dia_id`` / ``a_dia_id``
    are returned verbatim (no transformation); ``source_id`` is
    the canonical string that was parsed.
    """

    sample_id: str
    session_key: str
    q_dia_id: str
    a_dia_id: str
    source_id: str


def parse_source_id(source_id: str) -> ParsedSourceID:
    """Parse a canonical ``locomo|eval_v2|...`` source_id.

    Accepts the QA-pair format produced by :func:`make_source_id`
    — ``locomo|eval_v2|<sample_id>|<session_key>|<q_dia_id>><a_dia_id>``
    — and returns the four canonical fields.

    The parser is the strict inverse of :func:`make_source_id`:
    it refuses anything that is not a single canonical emission.
    Illegal formats raise :class:`ValueError` (fail-closed) so
    callers can rely on the returned object without defensive
    checks.

    Constraints:

      * The input MUST be a non-empty ``str``.
      * The shape MUST be exactly five pipe-separated segments
        ending in ``<q_dia_id>><a_dia_id>`` (one ``>`` separator).
      * None of the four data fields may contain ``|`` or ``>``.
      * Empty fields are refused (they would round-trip to a
        different canonical emission).
    """

    if not isinstance(source_id, str) or not source_id:
        raise ValueError(
            "parse_source_id: source_id must be a non-empty string"
        )
    m = _SOURCE_ID_RE.match(source_id)
    if not m:
        raise ValueError(
            f"parse_source_id: not a canonical locomo|eval_v2 QA-pair "
            f"source_id: {source_id!r}"
        )
    sample_id, session_key, q_dia_id, a_dia_id = m.group(1), m.group(2), m.group(3), m.group(4)
    if not (sample_id and session_key and q_dia_id and a_dia_id):
        raise ValueError(
            f"parse_source_id: source_id has empty field: {source_id!r}"
        )
    return ParsedSourceID(
        sample_id=sample_id,
        session_key=session_key,
        q_dia_id=q_dia_id,
        a_dia_id=a_dia_id,
        source_id=source_id,
    )


def make_session_id(sample_id: str, session_key: str) -> str:
    """Return the canonical lab ``session_id`` for one session.

    Format (verbatim):
        ``locomo-eval_v2-<sample_id>-<session_key>``
    """

    return f"locomo-eval_v2-{sample_id}-{session_key}"


# ---------------------------------------------------------------------------
# Datetime parsing
# ---------------------------------------------------------------------------


# Naive LoCoMo datetime strings. Per the historical eval_v2 rule,
# naive LoCoMo date strings are treated as UTC — the loader never
# guesses a local timezone.
_NAIVE_DT_SENTINEL = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)

# Historical native LoCoMo formats. Examples seen in the
# authoritative source:
#   "1:56 pm on 8 May, 2023"
#   "8 May 2023, 1:56 pm"
#   "8 May, 2023"             (date only)
#   "May 8, 2023"             (date only)
#   "2023-05-08 13:56:00"     (older ISO-ish form)
#   "2023-05-08"              (ISO date)
# Newer dumps add an explicit ISO with timezone.
_NATIVE_DATETIME_PATTERNS: tuple[str, ...] = (
    # "1:56 pm on 8 May, 2023"  (with the comma before year)
    "%I:%M %p on %d %B, %Y",
    # "1:56 pm on 8 May 2023"   (no comma)
    "%I:%M %p on %d %B %Y",
    # "8 May 2023, 1:56 pm"     (year-then-time)
    "%d %B %Y, %I:%M %p",
    # "May 8, 2023 1:56 pm"     (US ordering)
    "%B %d, %Y %I:%M %p",
    # "May 8 2023 1:56 pm"
    "%B %d %Y %I:%M %p",
    # date-only forms
    "%d %B, %Y",
    "%d %B %Y",
    "%B %d, %Y",
    "%B %d %Y",
    # legacy ISO-ish forms
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def _parse_utc(raw: str) -> _dt.datetime:
    """Parse a LoCoMo session ``date_time`` string into UTC.

    The historical eval_v2 rule is "naive LoCoMo date strings are
    treated as UTC". The loader tries a fixed ordered list of
    native LoCoMo shapes; on any match, the result is tagged
    UTC. ISO / ISO-with-tz strings are accepted via
    :func:`_dt.datetime.fromisoformat` (with ``Z`` → ``+00:00``).
    Failure raises :class:`LoCoMoSourceError` so the contract
    stays fail-closed — we never guess a local timezone.

    A missing or empty raw string maps to a Naive-1970 UTC
    sentinel so the loader never silently drops the timestamp;
    the caller can detect the sentinel via ``raw_session_dt``.
    """

    if not isinstance(raw, str) or not raw.strip():
        return _NAIVE_DT_SENTINEL

    text = raw.strip()
    # ISO 8601 — try first because it is unambiguous and lets the
    # caller pass either ``...Z`` or an explicit offset.
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            dt = _dt.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        else:
            dt = dt.astimezone(_dt.timezone.utc)
        return dt

    last_err: Exception | None = None
    for fmt in _NATIVE_DATETIME_PATTERNS:
        try:
            dt = _dt.datetime.strptime(text, fmt)
        except ValueError as exc:
            last_err = exc
            continue
        # Naive historical LoCoMo strings are treated as UTC.
        return dt.replace(tzinfo=_dt.timezone.utc)

    raise LoCoMoSourceError(
        f"LoCoMo: unparseable session date_time {raw!r} "
        f"(last error: {last_err!s})"
    )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


# Optional explicit cap on the byte size of any single message's
# ``text`` field. The loader fails closed if a single message
# exceeds this — it never silently drops bytes.
DEFAULT_MAX_MESSAGE_BYTES = 1_000_000  # 1 MiB per message


def _coerce_image_str(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (int, float)):
        return str(raw)
    # Anything else (bool / list / dict) we treat as not-present.
    return None


def _coerce_image_bool(raw: Any) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        if raw.strip().lower() in ("true", "1", "yes", "y", "t"):
            return True
        if raw.strip().lower() in ("false", "0", "no", "n", "f", ""):
            return False
    return None


def _load_messages(
    sample_id: str,
    session_key: str,
    session_dt_raw: str,
    raw_msgs: list[Any],
    *,
    max_message_bytes: int,
) -> list[LoCoMoMessage]:
    out: list[LoCoMoMessage] = []
    parsed_dt = _parse_utc(session_dt_raw)
    for i, raw in enumerate(raw_msgs):
        if not isinstance(raw, dict):
            raise LoCoMoSourceError(
                f"LoCoMo: session message must be an object "
                f"(sample_id={sample_id!r}, session_key={session_key!r}, "
                f"index={i})"
            )
        dia_id = raw.get("dia_id")
        if not isinstance(dia_id, str) or not dia_id:
            raise LoCoMoSourceError(
                f"LoCoMo: message missing 'dia_id' "
                f"(sample_id={sample_id!r}, session_key={session_key!r}, "
                f"index={i})"
            )
        speaker = raw.get("speaker") or ""
        text = raw.get("text")
        if text is None:
            # Some LoCoMo images carry only ``blip_caption`` with no text.
            # We preserve the empty string in that case.
            text = ""
        if not isinstance(text, str):
            raise LoCoMoSourceError(
                f"LoCoMo: message 'text' must be a string "
                f"(sample_id={sample_id!r}, session_key={session_key!r}, "
                f"dia_id={dia_id!r})"
            )
        # Fail-closed truncation check.
        encoded = text.encode("utf-8", errors="strict")
        if max_message_bytes > 0 and len(encoded) > max_message_bytes:
            raise LoCoMoInputLimitExceeded(
                f"LoCoMo message exceeds max_message_bytes="
                f"{max_message_bytes} (sample_id={sample_id!r}, "
                f"session_key={session_key!r}, dia_id={dia_id!r}, "
                f"bytes={len(encoded)})"
            )
        # Image fields — historical LoCoMo uses ``img_url`` /
        # ``blip_caption`` / ``query`` / ``re-download``. We
        # capture them under those names verbatim so callers
        # can recover the exact historical layout. None for
        # any field that the source did not set.
        img_url = _coerce_image_str(raw.get("img_url"))
        blip_caption = _coerce_image_str(raw.get("blip_caption"))
        img_query = _coerce_image_str(raw.get("query"))
        re_download = _coerce_image_bool(raw.get("re-download"))
        out.append(LoCoMoMessage(
            sample_id=sample_id,
            session_key=session_key,
            index=i,
            speaker=str(speaker),
            dia_id=dia_id,
            text=text,
            img_url=img_url,
            blip_caption=blip_caption,
            img_query=img_query,
            re_download=re_download,
            raw_session_dt=str(session_dt_raw or ""),
            session_dt_utc=parsed_dt,
        ))
    return out


def _qa_pair_index(
    sample_id: str,
    session_key: str,
    messages: list[LoCoMoMessage],
) -> list[LoCoMoQAPair]:
    """Build the same-session adjacent stride-2 QA pair list.

    Pair ``i``: ``q = messages[2*i]``, ``a = messages[2*i + 1]``.
    A trailing unpaired message is NOT promoted to a QA pair —
    the historical eval_v2 contract was adjacent-pairs only.
    Text / timestamp fields default to empty / sentinel UTC; the
    eval-row linker back-fills them in :func:`load_locomo`.

    Each pair carries the full text of both messages (never
    truncated) plus their image-field provenance so the lab
    importer can build import-ready rows.
    """

    out: list[LoCoMoQAPair] = []
    i = 0
    while i + 1 < len(messages):
        q = messages[i]
        a = messages[i + 1]
        q_dia = q.dia_id
        a_dia = a.dia_id
        q_image = _image_provenance(q)
        a_image = _image_provenance(a)
        out.append(LoCoMoQAPair(
            sample_id=sample_id,
            session_key=session_key,
            q_dia_id=q_dia,
            a_dia_id=a_dia,
            source_id=make_source_id(sample_id, session_key, q_dia, a_dia),
            q_provenance={
                "sample_id": sample_id,
                "session_key": session_key,
                "dia_id": q_dia,
                "index": q.index,
            },
            a_provenance={
                "sample_id": sample_id,
                "session_key": session_key,
                "dia_id": a_dia,
                "index": a.index,
            },
            question="",
            answer="",
            timestamp=_NAIVE_DT_SENTINEL,
            q_text=q.text,
            a_text=a.text,
            q_image=q_image,
            a_image=a_image,
        ))
        i += 2
    return out


def _sorted_sessions(sample: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    """Sort one sample's sessions numerically by their trailing integer.

    Keys like ``"session_1"`` / ``"session_2"`` / … sort by the
    integer. Non-numeric keys sort lexicographically and come
    AFTER the numeric ones (so ``session_X`` lands at the tail
    rather than between ``session_1`` and ``session_2``).
    """

    raw_sessions = sample.get("conversation") or {}
    if not isinstance(raw_sessions, dict):
        raise LoCoMoSourceError(
            f"LoCoMo: sample {sample.get('sample_id')!r} 'conversation' "
            f"must be an object"
        )
    # Old-shape sources keep session_N_date_time beside session_N;
    # those sibling metadata keys are not sessions themselves.
    raw_sessions = {
        k: v for k, v in raw_sessions.items()
        if k.startswith("session_")
        and not re.fullmatch(r"session_\d+_date_time", k)
    }

    def _sort_key(item: tuple[str, list[Any]]) -> tuple[int, int | str, str]:
        key = item[0]
        m = re.match(r"^session_(\d+)$", key)
        if m:
            return (0, int(m.group(1)), key)
        return (1, 0, key)

    return sorted(raw_sessions.items(), key=_sort_key)


# Sibling key for the old-shape LoCoMo session datetime. The
# session key passed in is the bare ``session_N``; we extract
# the trailing integer to build the sibling key
# ``session_N_date_time``.
_OLD_SHAPE_SESSION_KEY_RE = re.compile(r"^session_(\d+)$")


def _extract_session_dt(
    sample_id: str,
    sample_raw: dict[str, Any],
    session_key: str,
    session_payload: Any,
) -> str:
    """Return the raw ``date_time`` string for one session.

    Two shapes are accepted:

      1. Newer dump — ``session_N`` value is an object with
         ``"date_time"`` and ``"messages"`` keys.
      2. Authoritative historical dump — ``session_N`` value is
         a bare list of messages, and the sibling key
         ``session_N_date_time`` (e.g. ``session_1_date_time``)
         holds the native datetime string.

    The loader prefers shape (1) when both are present. The
    sibling key MUST be a string when used.
    """

    if isinstance(session_payload, dict):
        return str(session_payload.get("date_time") or "")
    # Bare list shape — look up the sibling ``_date_time`` key.
    m = _OLD_SHAPE_SESSION_KEY_RE.match(session_key)
    if not m:
        return ""
    sibling_key = f"session_{m.group(1)}_date_time"
    conversation = sample_raw.get("conversation") or {}
    sibling = conversation.get(sibling_key)
    if sibling is None:
        # Accept a top-level sibling too for small synthetic fixtures.
        sibling = sample_raw.get(sibling_key)
    if not isinstance(sibling, str):
        raise LoCoMoSourceError(
            f"LoCoMo: sample {sample_id!r} {sibling_key!r} must be a string "
            f"when session {session_key!r} is a bare list"
        )
    return sibling


def _normalise_session_payload(
    sample_id: str,
    sample_raw: dict[str, Any],
    session_key: str,
    raw_msgs: Any,
) -> tuple[str, list[Any]]:
    """Normalise one session's payload to ``(session_dt_raw, msgs_list)``.

    Two shapes are accepted:

      * Dict with optional ``date_time`` / ``messages`` (newer dump).
      * Bare list of message dicts (authoritative historical dump).
        The datetime is read from the sibling
        ``session_N_date_time`` key on the sample.

    Anything else raises :class:`LoCoMoSourceError`.
    """

    if isinstance(raw_msgs, dict):
        return str(raw_msgs.get("date_time") or ""), list(raw_msgs.get("messages") or [])
    if isinstance(raw_msgs, list):
        return _extract_session_dt(sample_id, sample_raw, session_key, raw_msgs), list(raw_msgs)
    raise LoCoMoSourceError(
        f"LoCoMo: session {session_key!r} must be a list or "
        f"object with 'messages'"
    )


def _eval_rows_for_sample(
    sample_id: str,
    qa_list: list[Any],
    qa_pairs_by_qdia: dict[str, LoCoMoQAPair],
    session_dt_lookup: dict[tuple[str, str], _dt.datetime],
) -> list[LoCoMoEvalRow]:
    """Build the eval rows for one sample.

    ``qa_pairs_by_qdia`` indexes each pair by its ``q_dia_id``.
    Where the historical dump omits ``id``, we assign a stable
    ``qa_id = "q{query_idx}"`` (the 0-based within-sample index)
    so downstream consumers have a deterministic identity even
    without a source-supplied id.

    The session datetime for each row is looked up via
    ``session_dt_lookup[(sample_id, qa_id_session_key)]`` —
    populated by the caller.
    """
    out: list[LoCoMoEvalRow] = []
    if not isinstance(qa_list, list):
        raise LoCoMoSourceError(
            f"LoCoMo: sample {sample_id!r} 'qa' must be a list"
        )
    for query_idx, raw in enumerate(qa_list):
        if not isinstance(raw, dict):
            raise LoCoMoSourceError(
                f"LoCoMo: sample {sample_id!r} contains non-object qa row"
            )
        # Old-shape dumps have no ``id``. The stable identity is
        # the within-sample ``query_idx``. Newer dumps MAY have
        # an ``id``; we still derive a stable ``qa_id`` and use
        # the supplied ``id`` only to link the QA pair.
        supplied_id = raw.get("id")
        if isinstance(supplied_id, str) and supplied_id:
            qa_id = supplied_id
        else:
            qa_id = f"q{query_idx}"
        question = raw.get("question")
        answer = raw.get("answer")
        category = raw.get("category") or ""
        evidence_raw = raw.get("evidence") or []
        if not isinstance(evidence_raw, list):
            raise LoCoMoSourceError(
                f"LoCoMo: sample {sample_id!r} qa_id={qa_id!r} "
                f"'evidence' must be a list"
            )
        evidence = tuple(str(x) for x in evidence_raw)
        # Link to the QA pair when the supplied id matches a
        # pair's q_dia_id. (Old-shape rows with no ``id`` link
        # by the eval row's positional identity instead — see
        # :func:`load_locomo`.)
        pair = qa_pairs_by_qdia.get(qa_id) if isinstance(supplied_id, str) and supplied_id else None
        qa_pair_source_id = pair.source_id if pair else None
        qa_dia_id: str | None = qa_id if pair else None
        # source_hash is the SHA-256 of the row's normalised payload.
        # We deliberately sort the evidence tuple so equivalent
        # orderings hash to the same digest. ``query_idx`` is
        # included so the row has a stable position-independent
        # identity for old-shape dumps.
        payload = {
            "sample_id": sample_id,
            "qa_id": qa_id,
            "query_idx": int(query_idx),
            "question": str(question or ""),
            "answer": str(answer or ""),
            "category": str(category),
            "evidence": sorted(evidence),
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        out.append(LoCoMoEvalRow(
            sample_id=sample_id,
            qa_id=qa_id,
            query_idx=int(query_idx),
            question=str(question or ""),
            answer=str(answer or ""),
            category=str(category),
            evidence=evidence,
            source_hash=digest,
            qa_pair_source_id=qa_pair_source_id,
            qa_dia_id=qa_dia_id,
        ))
    return out


def load_locomo(
    source_path: str,
    expected_sha256: str,
    *,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> LoCoMoDataset:
    """Load the LoCoMo JSON, verify SHA-256, and return a deterministic dataset.

    Parameters
    ----------
    source_path:
        Path to the LoCoMo JSON file on disk.
    expected_sha256:
        64-char hex digest that the file MUST match before any
        parsing is performed.
    max_message_bytes:
        Hard ceiling on a single message's UTF-8 byte size.
        The loader fails closed if any message exceeds it; it
        never silently truncates. ``0`` disables the limit.

    Raises
    ------
    LoCoMoSourceError
        On missing file, SHA-256 mismatch, or malformed JSON.
    LoCoMoInputLimitExceeded
        When a single message exceeds ``max_message_bytes``.
    """

    actual_sha = verify_source(source_path, expected_sha256)
    file_bytes = os.path.getsize(source_path)

    try:
        with open(source_path, "r", encoding="utf-8") as f:
            raw_root = json.load(f)
    except json.JSONDecodeError as exc:
        raise LoCoMoSourceError(
            f"LoCoMo: parse error at line {exc.lineno} col {exc.colno}: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise LoCoMoSourceError(
            f"LoCoMo: unreadable source {source_path!r} (io error)"
        ) from exc

    # Accept either a list-of-samples or an object with a
    # ``samples``/``data`` key. Public LoCoMo dumps have used
    # both shapes over time.
    if isinstance(raw_root, list):
        raw_samples = raw_root
    elif isinstance(raw_root, dict):
        if isinstance(raw_root.get("samples"), list):
            raw_samples = raw_root["samples"]
        elif isinstance(raw_root.get("data"), list):
            raw_samples = raw_root["data"]
        else:
            raise LoCoMoSourceError(
                "LoCoMo: top-level JSON must be a list of samples or an "
                "object with a 'samples'/'data' list"
            )
    else:
        raise LoCoMoSourceError(
            "LoCoMo: top-level JSON must be a list or object"
        )

    samples_out: list[LoCoMoSample] = []
    all_qa_pairs: list[LoCoMoQAPair] = []
    all_eval_rows: list[LoCoMoEvalRow] = []
    message_count = 0
    category_counts: dict[str, int] = defaultdict(int)

    for raw_sample in raw_samples:
        if not isinstance(raw_sample, dict):
            raise LoCoMoSourceError(
                "LoCoMo: each sample must be a JSON object"
            )
        sample_id = raw_sample.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id:
            raise LoCoMoSourceError(
                "LoCoMo: each sample must carry a string 'sample_id'"
            )

        sorted_sessions = _sorted_sessions(raw_sample)
        session_msgs: list[tuple[str, tuple[LoCoMoMessage, ...]]] = []
        qa_pairs_for_sample: list[LoCoMoQAPair] = []
        session_dt_lookup: dict[tuple[str, str], _dt.datetime] = {}
        for session_key, raw_session_payload in sorted_sessions:
            session_dt_raw, msgs_in = _normalise_session_payload(
                sample_id, raw_sample, session_key, raw_session_payload
            )
            msgs = _load_messages(
                sample_id=sample_id,
                session_key=session_key,
                session_dt_raw=session_dt_raw,
                raw_msgs=msgs_in,
                max_message_bytes=max_message_bytes,
            )
            session_msgs.append((session_key, tuple(msgs)))
            pairs = _qa_pair_index(sample_id, session_key, msgs)
            qa_pairs_for_sample.extend(pairs)
            session_dt_lookup[(sample_id, session_key)] = (
                msgs[0].session_dt_utc if msgs else _NAIVE_DT_SENTINEL
            )

        # Build a q_dia_id -> LoCoMoQAPair map for this sample.
        # The eval_v2 convention is that eval row ``id`` equals
        # the pair's ``q_dia_id``.
        qa_pairs_by_qdia: dict[str, LoCoMoQAPair] = {}
        for qa in qa_pairs_for_sample:
            qa_pairs_by_qdia[qa.q_dia_id] = qa

        eval_rows = _eval_rows_for_sample(
            sample_id=sample_id,
            qa_list=raw_sample.get("qa") or [],
            qa_pairs_by_qdia=qa_pairs_by_qdia,
            session_dt_lookup=session_dt_lookup,
        )
        for er in eval_rows:
            category_counts[er.category] += 1

        # Back-fill QA-pair text / timestamp for pairs that an
        # eval row links to. The historical eval_v2 convention is
        # ``eval row.id == pair.q_dia_id``. We populate the pair
        # with the eval row's actual question / answer text and
        # the session's parsed UTC. Pairs without a link keep
        # empty text + sentinel UTC. We walk every per-session
        # message list and look up the link by ``q_dia_id``.
        if eval_rows:
            pair_index: dict[str, int] = {
                p.source_id: i for i, p in enumerate(qa_pairs_for_sample)
            }
            session_msgs_by_key: dict[str, tuple[LoCoMoMessage, ...]] = {
                skey: msgs for skey, msgs in session_msgs
            }
            updated_pairs: list[LoCoMoQAPair] = list(qa_pairs_for_sample)
            for er in eval_rows:
                if er.qa_pair_source_id is None:
                    continue
                idx = pair_index.get(er.qa_pair_source_id)
                if idx is None:
                    continue
                pair = updated_pairs[idx]
                sess_msgs = session_msgs_by_key.get(pair.session_key) or ()
                ts = sess_msgs[0].session_dt_utc if sess_msgs else _NAIVE_DT_SENTINEL
                updated_pairs[idx] = dataclasses.replace(
                    pair,
                    question=er.question,
                    answer=er.answer,
                    timestamp=ts,
                )
            qa_pairs_for_sample = updated_pairs

        samples_out.append(LoCoMoSample(
            sample_id=sample_id,
            sessions=tuple(session_msgs),
        ))
        all_qa_pairs.extend(qa_pairs_for_sample)
        all_eval_rows.extend(eval_rows)
        # Message count tracks every conversation message preserved,
        # including any trailing unpaired odd tail.
        for _key, msgs in session_msgs:
            message_count += len(msgs)

    # Sort for stable iteration order.
    samples_out.sort(key=lambda s: s.sample_id)
    all_qa_pairs.sort(key=lambda p: (p.sample_id, p.session_key, p.source_id))
    all_eval_rows.sort(key=lambda r: (r.sample_id, r.query_idx, r.qa_id))

    return LoCoMoDataset(
        source_path=os.path.abspath(source_path),
        source_sha256=actual_sha,
        source_bytes=file_bytes,
        samples=tuple((s.sample_id, s) for s in samples_out),
        eval_rows=tuple(all_eval_rows),
        qa_pairs=tuple(all_qa_pairs),
        category_counts=dict(category_counts),
        message_count=message_count,
        qa_pair_count=len(all_qa_pairs),
    )


# ---------------------------------------------------------------------------
# Deterministic evidence map
# ---------------------------------------------------------------------------


def build_evidence_map(dataset: LoCoMoDataset) -> dict[tuple[str, str, str], list[str]]:
    """Build a deterministic (sample_id, session_key, dia_id) → source_ids map.

    For every conversation message, we record the list of
    ``LoCoMoQAPair.source_id`` values whose pair contains that
    message (either ``q_dia_id`` or ``a_dia_id``). Messages that
    are NOT covered by any pair still appear in the map with an
    empty list — unmapped dia IDs are kept explicit so the caller
    can audit coverage without surprise.

    The output is sorted: keys sorted lexicographically; values
    sorted lexicographically. Two calls over the same dataset
    return structurally identical dicts.
    """

    out: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for sid, sample in dataset.samples:
        for session_key, messages in sample.sessions:
            for m in messages:
                out[(sid, session_key, m.dia_id)] = []
    for pair in dataset.qa_pairs:
        out[(pair.sample_id, pair.session_key, pair.q_dia_id)].append(pair.source_id)
        out[(pair.sample_id, pair.session_key, pair.a_dia_id)].append(pair.source_id)
    # Sort for determinism.
    return {
        k: sorted(set(v))
        for k, v in sorted(out.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2]))
    }


def evidence_lookup(
    evidence_map: dict[tuple[str, str, str], list[str]],
    sample_id: str,
    session_key: str | None,
    dia_id: str,
) -> list[str]:
    """Deterministically resolve the source_ids covering ``dia_id``.

    Resolution order:

      1. If ``session_key`` is provided AND
         ``(sample_id, session_key, dia_id)`` is in the map →
         return its value (possibly empty).
      2. Otherwise scan every session_key for the same
         ``(sample_id, dia_id)`` and union the values.

    The lookup never fuzzy-matches. Compound / malformed dia_ids
    (whitespace, embedded punctuation, anything that does not
    match a real conversation message) produce an empty list —
    callers can detect the miss and surface it as ``unresolved``.
    """

    if session_key is not None:
        key = (sample_id, session_key, dia_id)
        if key in evidence_map:
            return list(evidence_map[key])
    # Fallback: scan every session_key for this sample + dia_id.
    union: list[str] = []
    for (sid, _skey, did), sources in evidence_map.items():
        if sid == sample_id and did == dia_id:
            union.extend(sources)
    # Deduplicate + sort for determinism.
    return sorted(set(union))


@dataclasses.dataclass(frozen=True)
class GoldEvidence:
    """Deterministic gold evidence for one eval row.

    ``source_ids`` is the sorted, deduplicated list of QA pair
    ``source_id`` values whose pair covers at least one
    ``dia_id`` in the row's evidence. ``unmapped_dia_ids`` is
    the sorted list of dia_ids that were NOT covered by any
    pair (kept explicit so callers can audit coverage without
    surprise). ``unresolved`` is the list of evidence entries
    that are NOT a single canonical dia_id (compound or
    malformed strings) — they are preserved verbatim rather
    than split heuristically.
    """

    source_ids: tuple[str, ...]
    unmapped_dia_ids: tuple[str, ...]
    unresolved: tuple[str, ...]


# Evidence entries that look like compound / malformed strings —
# contain whitespace, internal commas or other separators. These
# surface as ``unresolved`` rather than being split heuristically.
_NON_CANONICAL_RE = re.compile(r"[\s,;]")


def _looks_non_canonical(entry: str) -> bool:
    if not entry:
        return True
    if _NON_CANONICAL_RE.search(entry):
        return True
    return False


def resolve_gold_evidence(
    dataset: LoCoMoDataset,
    eval_row: LoCoMoEvalRow,
    evidence_map: dict[tuple[str, str, str], list[str]],
) -> GoldEvidence:
    """Resolve the gold evidence list for one eval row, deterministically.

    Strategy:

      1. For each ``dia_id`` in ``eval_row.evidence``:
         - If the entry is compound or malformed (whitespace /
           embedded separators), preserve it verbatim in
           ``unresolved`` and skip the map lookup.
         - Otherwise look up ``(sample_id, session_key, dia_id)``
           in the evidence map, constrained first by the
           row's linked pair's session_key when known. If no
           match is found in the constrained session, scan
           every session for the same ``(sample_id, dia_id)``.
      2. Return a :class:`GoldEvidence` whose ``source_ids``
         are the deduplicated, sorted list of source_ids that
         cover at least one evidence dia_id. Unmapped dia IDs
         are kept explicit in ``unmapped_dia_ids``.

    No fuzzy text matching is performed. The contract is that
    the gold evidence resolution is a pure function of the
    deterministic evidence map.
    """

    found: set[str] = set()
    unmapped: list[str] = []
    unresolved: list[str] = []
    constrained_session: str | None = None
    if eval_row.qa_pair_source_id is not None:
        for pair in dataset.qa_pairs:
            if pair.source_id == eval_row.qa_pair_source_id:
                constrained_session = pair.session_key
                break

    for entry in eval_row.evidence:
        if _looks_non_canonical(entry):
            unresolved.append(entry)
            continue
        sources = evidence_lookup(
            evidence_map,
            eval_row.sample_id,
            constrained_session,
            entry,
        )
        if sources:
            found.update(sources)
        else:
            unmapped.append(entry)

    return GoldEvidence(
        source_ids=tuple(sorted(found)),
        unmapped_dia_ids=tuple(sorted(unmapped)),
        unresolved=tuple(sorted(unresolved)),
    )


# ---------------------------------------------------------------------------
# Import-row builders (consumed by ``lab.import_rows``)
# ---------------------------------------------------------------------------


# Source tag written to the lab ``conversation_stream.source`` column.
_CONVERSATION_SOURCE_TAG = "locomo_eval_v2"
# Trigger tag written to ``conversation_stream.trigger``.
_CONVERSATION_TRIGGER_TAG = "locomo_eval_v2"


def _message_role(message: LoCoMoMessage) -> str:
    """Map a LoCoMo ``speaker`` to the lab ``role`` column.

    Mapping:

      * ``speaker_1`` / ``speaker_2`` / … → ``user`` / ``assistant`` /
        ``other`` based on the 1-based speaker index parity. The
        exact mapping is documented and deterministic — two calls
        over the same message return the same role.
      * Anything else → ``other``.
    """
    m = re.match(r"^speaker_(\d+)$", message.speaker or "")
    if not m:
        return "other"
    n = int(m.group(1))
    if n == 1:
        return "user"
    if n == 2:
        return "assistant"
    # speaker_3 and beyond → other; the alternate-speaker pattern
    # has not been standardised in the public schema.
    return "other"


def _image_provenance(message: LoCoMoMessage) -> dict[str, Any]:
    """Build a JSON-compatible image provenance dict for a message.

    Returns an empty dict when the message carries no image
    fields; otherwise every preserved historical image field is
    included. Always emits ``present`` so downstream readers can
    distinguish "no image" from "image with all-empty fields".
    """
    has_any = any(
        v is not None for v in (
            message.img_url,
            message.blip_caption,
            message.img_query,
            message.re_download,
        )
    )
    if not has_any:
        return {"present": False}
    return {
        "present": True,
        "img_url": message.img_url,
        "blip_caption": message.blip_caption,
        "query": message.img_query,
        "re_download": message.re_download,
    }


def build_import_rows(
    dataset: LoCoMoDataset,
    sample_ids: Iterable[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the three deterministic import row lists.

    Returns a dict with three keys:

      * ``qa_pairs`` — one row per preserved QA pair (every
        adjacent same-session stride-2 pair, including pairs
        whose eval link is unknown). ``source_id`` matches the
        contract format ``locomo|eval_v2|<sample_id>|<session_key>|
        <q_dia_id>><a_dia_id>``. ``session_id`` is
        ``locomo-eval_v2-<sample_id>-<session_key>``.
      * ``conversation_stream`` — one row per preserved message
        (including any odd trailing unpaired message), in
        ``(sample_id asc, session_key asc, index asc)`` order.
        ``tool_calls`` / ``tool_results`` carry provenance
        including ``sample_id`` / ``session_key`` / ``dia_id`` /
        ``speaker`` and the preserved image fields.
      * ``eval_queries`` — one row per eval question. ``source_id``
        is ``locomo|eval_v2|<sample_id>|q<query_idx>`` and is the
        table's unique idempotency key. ``evidence`` is preserved
        verbatim (compound / malformed entries are kept; they are
        NOT split heuristically).

    ``sample_ids`` optionally narrows the build to a subset of
    sample_ids (the loader's stable iteration order is preserved).
    Empty / unknown sample_ids are silently skipped.
    """

    allowed: set[str] | None = set(sample_ids) if sample_ids is not None else None

    qa_pairs: list[dict[str, Any]] = []
    conversation_stream: list[dict[str, Any]] = []
    eval_queries: list[dict[str, Any]] = []

    # Build a per-pair timestamp / question / answer lookup from
    # the eval rows. Each eval row has ``qa_pair_source_id``; for
    # that source_id we record the row's UTC timestamp and the
    # actual question / answer text. Pairs without an eval link
    # carry the parsed session UTC and empty text.
    pair_question: dict[str, str] = {}
    pair_answer: dict[str, str] = {}
    pair_timestamp: dict[str, _dt.datetime] = {}

    for sid, sample in dataset.samples:
        if allowed is not None and sid not in allowed:
            continue
        session_id_for: dict[str, str] = {}
        for session_key, _msgs in sample.sessions:
            session_id_for[session_key] = make_session_id(sid, session_key)

        for pair in dataset.qa_pairs:
            if pair.sample_id != sid:
                continue
            pair_question[pair.source_id] = pair.question
            pair_answer[pair.source_id] = pair.answer
            pair_timestamp[pair.source_id] = pair.timestamp

        # Conversation stream — one row per preserved message,
        # including any odd trailing unpaired message.
        for session_key, messages in sample.sessions:
            session_id = session_id_for[session_key]
            turn_id = 0
            for m in messages:
                tool_results: list[dict[str, Any]] = []
                provenance: dict[str, Any] = {
                    "sample_id": sid,
                    "session_key": session_key,
                    "dia_id": m.dia_id,
                    "speaker": m.speaker,
                    "index": m.index,
                }
                img_prov = _image_provenance(m)
                if img_prov.get("present"):
                    provenance["image"] = img_prov
                tool_calls: list[dict[str, Any]] = [provenance]
                # Raw session datetime for diagnostic reuse.
                conversation_stream.append({
                    "session_id": session_id,
                    "role": _message_role(m),
                    "content": m.text,
                    "trigger": _CONVERSATION_TRIGGER_TAG,
                    "turn_id": int(turn_id),
                    "timestamp": m.session_dt_utc,
                    "source": _CONVERSATION_SOURCE_TAG,
                    "embedding": None,
                    "tool_calls": tool_calls,
                    "tool_results": tool_results,
                })
                turn_id += 1

    # qa_pairs — every pair in the dataset, ordered stably. The
    # question / answer text uses the linked eval row when
    # available (eval-linked pair); otherwise falls back to the
    # pair's q-message text and a-message text so the row is
    # never blank. Image-field markers are preserved in
    # ``tool_calls`` so they survive into the lab.
    for pair in dataset.qa_pairs:
        if allowed is not None and pair.sample_id not in allowed:
            continue
        session_id = make_session_id(pair.sample_id, pair.session_key)
        question_text = pair.question or pair.q_text
        answer_text = pair.answer or pair.a_text
        tool_calls = [
            {
                "q_provenance": pair.q_provenance,
                "a_provenance": pair.a_provenance,
                "q_image": pair.q_image,
                "a_image": pair.a_image,
            }
        ]
        tool_results: list[dict[str, Any]] = []
        qa_pairs.append({
            "source_id": pair.source_id,
            "session_id": session_id,
            "turn_id": 0,  # one row per pair; the lab pairs table is keyed by source_id
            "question": question_text,
            "answer": answer_text,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "timestamp": pair.timestamp,
            "source": "locomo",
            "embedding": None,
            "embed_model": "",
            "created_at": pair.timestamp,
        })

    # eval_queries — one row per eval question, sorted by (sample_id, query_idx).
    for er in sorted(dataset.eval_rows, key=lambda r: (r.sample_id, r.query_idx)):
        if allowed is not None and er.sample_id not in allowed:
            continue
        source_id = make_eval_query_source_id(er.sample_id, er.query_idx)
        eval_queries.append({
            "source_id": source_id,
            "sample_id": er.sample_id,
            "qa_id": er.qa_id,
            "query_idx": er.query_idx,
            "category": er.category,
            "question": er.question,
            "answer": er.answer,
            "evidence": list(er.evidence),
            "source_hash": er.source_hash,
        })

    return {
        "qa_pairs": qa_pairs,
        "conversation_stream": conversation_stream,
        "eval_queries": eval_queries,
    }


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def iter_messages(dataset: LoCoMoDataset) -> Iterable[LoCoMoMessage]:
    """Yield every preserved conversation message, deterministic order.

    Order: ``(sample_id asc, session_key asc, index asc)``.
    """

    for _sid, sample in dataset.samples:
        for _session_key, messages in sample.sessions:
            yield from messages


def iter_pairs(dataset: LoCoMoDataset) -> Iterable[LoCoMoQAPair]:
    """Yield every QA pair in stable order.

    Order: ``(sample_id asc, session_key asc, source_id asc)``.
    """

    yield from dataset.qa_pairs


def iter_eval_rows(dataset: LoCoMoDataset) -> Iterable[LoCoMoEvalRow]:
    """Yield every eval row in stable order.

    Order: ``(sample_id asc, query_idx asc, qa_id asc)``.
    """

    yield from dataset.eval_rows


__all__ = [
    "DEFAULT_MAX_MESSAGE_BYTES",
    "GoldEvidence",
    "LoCoMoDataset",
    "LoCoMoEvalRow",
    "LoCoMoInputLimitExceeded",
    "LoCoMoMessage",
    "LoCoMoQAPair",
    "LoCoMoSample",
    "LoCoMoSourceError",
    "ParsedSourceID",
    "build_evidence_map",
    "build_import_rows",
    "evidence_lookup",
    "iter_messages",
    "iter_pairs",
    "iter_eval_rows",
    "load_locomo",
    "make_eval_query_source_id",
    "make_session_id",
    "make_source_id",
    "parse_source_id",
    "resolve_gold_evidence",
    "sha256_file",
    "verify_source",
]