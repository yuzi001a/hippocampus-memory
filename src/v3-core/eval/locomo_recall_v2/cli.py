# -*- coding: utf-8 -*-
"""G6C-A LoCoMo Recall V2 — durable thin CLI entry point.

The CLI is a thin orchestrator that wires the existing
:mod:`eval.locomo_recall_v2.*` modules into two explicit
stages (``dry-run`` and ``semantic``). It does NOT
re-implement retrieval, embedding, or PG access — every step
delegates to an existing module API:

* SHA verification / dataset parsing / row building →
  :mod:`eval.locomo_recall_v2.dataset`
* Sanitised manifest →
  :mod:`eval.locomo_recall_v2.manifest`
* Lab DSN guard / schema bootstrap / row import /
  provenance map →
  :mod:`eval.locomo_recall_v2.lab`
* Embedding preparation (cached) →
  :mod:`eval.locomo_recall_v2.embeddings`
* Per-case facade invocation →
  :mod:`eval.locomo_recall_v2.runner.run_case`
* Retrieval metrics + coverage →
  :mod:`eval.locomo_recall_v2.metrics`
* Repeatability diff →
  :mod:`eval.locomo_recall_v2.compare`

What the CLI never does
=======================

* It NEVER touches production source code, production
  configuration, production PG ``5433``, or any provider /
  Hermes runtime. The lab DSN guard refuses every reserved
  production surface before this module reads the dataset.
* It NEVER imports a private LoCoMo build script — the
  evaluator-only loader re-implements the historical rules
  verbatim.
* It NEVER invents retrieval SQL. The retrieval path is the
  canonical production-shaped facade in
  :mod:`eval.locomo_recall_v2.adapter.run_case`, called once
  per case via :mod:`eval.locomo_recall_v2.runner.run_case`.
* It NEVER serialises a credential. The isolated lab config
  exposes ``storage.embed`` as the borrowed ephemeral
  provider config (env-var NAMES only); no raw key, raw
  endpoint, or raw password is ever written to disk.
* It NEVER accepts an empty ``basePath`` — the disposable
  experiment directory is created on disk under
  ``--cache-dir`` and validated to be real, empty, and
  disposable before the lab config is built.

Stages
======

* ``dry-run`` (default) — verify dataset SHA-256, hash the
  source config, build the deterministic import rows, build
  the sanitised manifest (with the source-config SHA),
  validate the disposable DSN, create a real empty
  disposable experiment directory, build the isolated lab
  config, optionally write ``import-rows.json``. Zero
  provider calls. Zero PG calls. Zero HTTP.
* ``semantic`` — additionally open the disposable lab
  connection, bootstrap the schema, import the lab rows,
  prepare query embeddings via
  :func:`embeddings.prepare_query_embeddings` and per-row
  corpus vectors via
  :func:`embeddings.prepare_corpus_embeddings`, build the
  per-case ``q_emb_by_case``, run the production-shaped
  facade sequentially via :func:`runner.run_case`, map
  candidate IDs through the fresh-lab ordinal provenance map
  (:func:`lab.build_provenance_map` +
  :func:`lab.map_candidate_ids`), compute metrics + coverage,
  and write the bounded artifacts. With ``--repeatability``,
  a second cached pass produces ``repeatability.json`` with
  metric / latency deltas — no provider calls on the second
  run.

Outputs
=======

``--mode dry-run`` writes only:

    benchmark-manifest.json
    import-rows.json   (when --write-rows)

``--mode semantic`` writes:

    benchmark-manifest.json
    semantic-canary-results.jsonl
    full-objective-results.jsonl
    metrics.json
    coverage-audit.json
    latency.csv
    embedding-cache-manifest.json
    fingerprint.json                 (provider/model/dim/profile; endpoint stripped)
    repeatability.json               (when --repeatability)

No ``secrets`` / ``context`` blocks are emitted.

Exit codes
==========

* ``0`` — success.
* ``1`` — user / input error (bad SHA, refused DSN, forbidden
  YAML key, missing file, refused ``basePath``).
* ``2`` — integration gap (a cross-module seam is missing).
* ``3`` — runtime / unhandled error.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import dataclasses
import hashlib
import json
import logging
import os
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

import yaml

from . import compare as _compare
from . import dataset as _dataset
from . import embeddings as _embeddings
from . import lab as _lab
from . import manifest as _manifest
from . import metrics as _metrics
from . import runner as _runner
from .search_protocol import (
    SEARCH_MODE_ANN,
    SEARCH_MODE_EXACT,
    ALLOWED_SEARCH_MODES,
    PROBE_AVAILABILITY_AVAILABLE,
    PROBE_AVAILABILITY_UNAVAILABLE,
    PROBE_LOAD_METHOD_LOAD,
    PROBE_LOAD_METHOD_NONE,
    PROBE_LOAD_METHOD_VECTOR_FUNCTION,
    SearchProtocol,
    SearchProtocolError,
    apply_search_protocol,
    ensure_pgvector_loaded,
)
from .session_pool import (
    EvaluatorPoolClosed,
    EvaluatorPoolProtocolError,
    EvaluatorSessionPool,
    EvaluatorPoolState,
)
from .explain_capture import (
    capture_explain,
    ExplainCaptureError,
    ExplainCaptureReport,
    CLASS_SEQ_SCAN,
    CLASS_IVFFLAT_INDEX_SCAN,
)


__all__ = [
    "CLIError",
    "IntegrationTODOError",
    "LabConfigRefused",
    "SourceConfigRefused",
    "StructuralResult",
    "build_isolated_lab_config",
    "load_source_config_embed_section",
    "make_argument_parser",
    "main",
    "redact_secrets_in_payload",
    "run_dry_run",
    "run_semantic",
    "_run_semantic_sample_isolated",
]


# ---------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------

EXIT_OK = 0
EXIT_INPUT_ERROR = 1
EXIT_INTEGRATION_GAP = 2
EXIT_RUNTIME_ERROR = 3


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------


class CLIError(ValueError):
    """Base error for the CLI surface."""


class LabConfigRefused(CLIError):
    """Raised when the isolated lab config is unsafe."""


class SourceConfigRefused(CLIError):
    """Raised when the read-only YAML source config is malformed
    or carries a forbidden key."""


class IntegrationTODOError(CLIError):
    """raised when a cross-module API is not available.

    Exit code 2 — the parent driver treats this distinctly
    from a real user / input error.
    """


@dataclasses.dataclass(frozen=True)
class StructuralResult:
    """Public, frozen result of :func:`run_dry_run`.

    The dataclass replaces the previous dict-shaped summary
    so test code can rely on typed attributes:

      * ``dataset_sha256`` — the verified LoCoMo source SHA.
      * ``source_config_sha256`` — the YAML source-config SHA.
      * ``sample_count`` — number of samples in the source.
      * ``session_count`` — total sessions across all samples.
      * ``message_count`` — total messages across all samples.
      * ``qa_pair_count`` — total QA pairs across all samples.
      * ``eval_row_count`` — number of eval rows after
        ``case_limit`` / ``sample_filter`` narrowing.
      * ``manifest`` — the sanitised manifest dict (NOT the
        on-disk JSON; ``manifest_path`` carries that).
      * ``manifest_path`` — absolute on-disk manifest path.
      * ``lab_config`` — the plain-dict isolated lab config.
      * ``base_path`` — the real empty disposable experiment
        directory used for ``lab_config["basePath"]``.
    """

    dataset_sha256: str
    source_config_sha256: str
    sample_count: int
    session_count: int
    message_count: int
    qa_pair_count: int
    eval_row_count: int
    manifest: Mapping[str, Any]
    manifest_path: str
    lab_config: Mapping[str, Any]
    base_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": "dry-run",
            "ok": True,
            "dataset_sha256": str(self.dataset_sha256),
            "source_config_sha256": str(self.source_config_sha256),
            "sample_count": int(self.sample_count),
            "session_count": int(self.session_count),
            "message_count": int(self.message_count),
            "qa_pair_count": int(self.qa_pair_count),
            "eval_row_count": int(self.eval_row_count),
            "manifest_path": str(self.manifest_path),
            "lab_config_keys": sorted(self.lab_config.keys()),
            "base_path": str(self.base_path),
        }


# ---------------------------------------------------------------------
# G6C-B0 default policy
# ---------------------------------------------------------------------
#
# The CLI defaults are:
#
#   * ``--search-mode`` defaults to ``"ann"`` — the historical
#     G6C-A / alpha production path uses ANN with the planner
#     default probes. A CLI invocation that does NOT pass
#     ``--search-mode`` therefore stays backward-compatible with
#     G6C-A. The B0 deterministic-baseline experiment commands
#     pass ``--search-mode exact`` explicitly so the runbook
#     contract is "the explicit value is what we are measuring".
#
#   * ``--index-build-phase`` defaults to
#     ``"bootstrap_before_import"`` — the historical alpha path
#     builds the IVFFlat indexes inside the bootstrap
#     transaction. Mode C (deferred) requires an explicit
#     ``--index-build-phase after_import``.
#
#   * ``--ivfflat-probes`` defaults to ``None`` — the planner
#     default probes are used. The B0 explicit-probe experiment
#     commands pass ``--search-mode ann --ivfflat-probes N``.
#
#   * ``--run-analyze`` defaults to ``False`` — the historical
#     alpha path does not issue ANALYZE. The B0 experiment
#     commands pass ``--run-analyze`` so the planner has fresh
#     statistics before the recall engine runs.
DEFAULT_SEARCH_MODE = SEARCH_MODE_ANN  # re-exported from search_protocol via import below
DEFAULT_INDEX_BUILD_PHASE = "bootstrap_before_import"


def _resolve_cli_policy_kwargs(
    *,
    search_mode: str | None,
    ivfflat_probes: int | None,
    index_build_phase: str | None,
    run_analyze: bool | None,
) -> dict[str, Any]:
    """Resolve the G6C-B0 CLI policy kwargs with explicit defaults.

    The CLI never inherits a silent fallback from
    :func:`SearchProtocol.parse`. The historical alpha behaviour
    is ``search_mode='ann'``; if the caller does not pass
    ``--search-mode``, we substitute ``"ann"`` here so the
    manifest records the actual mode the run used. The
    manifest then stamps ``requested_search_mode`` so a
    downstream auditor can confirm the requested value and the
    planner-verified value agree.
    """

    resolved_mode = (
        str(search_mode).strip().lower()
        if search_mode is not None and str(search_mode).strip() != ""
        else DEFAULT_SEARCH_MODE
    )
    resolved_phase = (
        str(index_build_phase).strip().lower()
        if index_build_phase is not None and str(index_build_phase).strip() != ""
        else DEFAULT_INDEX_BUILD_PHASE
    )
    resolved_probes: int | None
    if ivfflat_probes is None:
        resolved_probes = None
    else:
        try:
            resolved_probes = int(ivfflat_probes)
        except (TypeError, ValueError) as exc:
            raise CLIError(
                f"cli: --ivfflat-probes must be a positive integer "
                f"(got {ivfflat_probes!r})"
            ) from exc
        if resolved_probes <= 0:
            raise CLIError(
                f"cli: --ivfflat-probes must be positive (got {resolved_probes!r})"
            )
    resolved_analyze = bool(run_analyze) if run_analyze is not None else False

    return {
        "search_mode": resolved_mode,
        "ivfflat_probes": resolved_probes,
        "index_build_phase": resolved_phase,
        "run_analyze": resolved_analyze,
    }

# Keys that NEVER belong in the YAML source config. The list
# is deliberately conservative: any key that looks
# credential-shaped, or that points at a non-loopback /
# production surface, is rejected before the YAML is parsed.
_FORBIDDEN_SOURCE_CONFIG_KEYS = frozenset({
    "password", "passwd",
    "secret", "token", "access_token", "private_key",
    "production_db_dsn", "production_endpoint",
    "production_pg_dsn", "production_dsn",
})

# Regex for catching secrets in free-text values (DSN strings,
# endpoint URLs). Used by :func:`redact_secrets_in_payload`
# only — the manifest module already enforces its own
# redaction at the source.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(password\s*=\s*)([^\s'\"]+)", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*=\s*)([^\s'\"]+)", re.IGNORECASE),
    re.compile(r"(token\s*=\s*)([^\s'\"]+)", re.IGNORECASE),
    re.compile(r"(secret\s*=\s*)([^\s'\"]+)", re.IGNORECASE),
)

# Dict keys whose VALUE is a credential. ``redact_secrets_in_payload``
# masks the value outright when it sees one of these keys, so a bare
# ``{"api_key": "<value>"}`` can never survive a redaction pass.
_CREDENTIAL_KEY_NAMES = frozenset({
    "api_key", "apikey", "api-key",
    "password", "passwd",
    "secret", "token", "access_token", "private_key",
})

# Hard-coded identity for the evaluator. The manifest
# already stamps this; the CLI only overrides it when the
# source YAML carries a non-empty ``storage.embed.model``.
DEFAULT_PROVIDER_ID = "siliconflow"
DEFAULT_MODEL_ID = "BAAI/bge-m3"
DEFAULT_EMBEDDING_DIM = 1024

# Maximum allowed size of any single YAML config we read. A
# 64 KiB cap is far above any reasonable config and prevents
# a caller from accidentally pointing the CLI at a giant
# file that we would slurp into memory.
_MAX_SOURCE_CONFIG_BYTES = 64 * 1024

logger = logging.getLogger("v3core.eval.locomo_recall_v2.cli")


# ---------------------------------------------------------------------
# Helpers — pure
# ---------------------------------------------------------------------


def redact_secrets_in_payload(payload: Any) -> Any:
    """Return a deep-copied ``payload`` with secret values masked.

    Walks dicts, lists and tuples recursively. Two rules apply:

      * the **value** of any dict key that is credential-shaped
        (``api_key`` / ``apikey`` / ``password`` / ``passwd`` /
        ``secret`` / ``token`` / ``access_token`` /
        ``private_key``) is replaced with ``"***"`` regardless of
        its type — a bare ``{"api_key": "<value>"}`` must not
        survive a redaction pass;
      * strings are scanned against a small set of well-known
        credential regexes (``password=…`` / ``api_key=…`` /
        ``token=…`` / ``secret=…``) and matching assignments are
        masked.

    The original payload is NOT mutated.
    """

    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            key = str(k)
            if key.strip().lower() in _CREDENTIAL_KEY_NAMES:
                out[key] = "***"
                continue
            out[key] = redact_secrets_in_payload(v)
        return out
    if isinstance(payload, (list, tuple)):
        return [redact_secrets_in_payload(v) for v in payload]
    if isinstance(payload, str):
        out_s = payload
        for pat in _SECRET_VALUE_PATTERNS:
            out_s = pat.sub(lambda m: f"{m.group(1)}***", out_s)
        return out_s
    return payload


def _ensure_dir(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def _file_sha256(path: str) -> str:
    """Return the SHA-256 hex digest of a file's bytes."""

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _strip_endpoint_from_profile(profile_dict: Mapping[str, Any]) -> dict[str, Any]:
    """Drop endpoint / base_url / api_key / apiKey / proxy /
    _raw from a profile-shaped dict.

    The CLI never echoes the endpoint URL through the
    ``fingerprint.json`` output.
    """

    safe: dict[str, Any] = {}
    for k, v in dict(profile_dict).items():
        if k in {"endpoint", "base_url", "api_key", "apiKey", "proxy", "_raw"}:
            continue
        safe[k] = v
    return safe


def _write_json_atomic(path: str, payload: Any) -> None:
    """Atomically write ``payload`` as JSON to ``path``."""

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(
            redact_secrets_in_payload(payload),
            ensure_ascii=False, indent=2, sort_keys=True, default=str,
        ))
        f.write("\n")
    os.replace(tmp, path)


def _write_jsonl_atomic(
    path: str, records: Iterable[dict[str, Any]]
) -> int:
    """Write ``records`` as line-delimited JSON to ``path``."""

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    n = 0
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for rec in records:
            f.write(json.dumps(
                redact_secrets_in_payload(rec),
                ensure_ascii=False, sort_keys=True, default=str,
            ))
            f.write("\n")
            n += 1
    os.replace(tmp, path)
    return n


def _write_csv_atomic(
    path: str, header: Sequence[str], rows: Sequence[Sequence[Any]]
) -> None:
    """Write ``rows`` to ``path`` as a deterministic CSV."""

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(list(header))
        for row in rows:
            w.writerow(list(row))
    os.replace(tmp, path)


# ---------------------------------------------------------------------
# YAML source-config loader (read-only; storage.embed only)
# ---------------------------------------------------------------------


def load_source_config_embed_section(path: str) -> dict[str, Any]:
    """Load the ``storage.embed`` sub-dict from a YAML config.

    Read-only. ONLY the ``storage.embed`` sub-dict is
    consumed; benign unrelated top-level keys
    (``basePath``, ``pg``, ``rerank``, ``prompts``, etc.)
    are ignored. Top-level keys whose name matches a
    credential-shaped pattern (``password`` / ``secret`` /
    ``token`` / ``api_key`` / ``apiKey`` / ``private_key`` /
    ``production_*`` etc.) cause :class:`SourceConfigRefused`.
    The nested ``storage.embed`` section keeps the same
    safety contract: exact ``api_key`` / ``apiKey`` is
    allowed in-memory; every other credential-shaped key
    (including ``password``, ``secret``, ``token``, and any
    substring match) is refused. The function returns a
    copy-defensive dict so callers can mutate freely.
    """

    if not isinstance(path, str) or not path.strip():
        raise SourceConfigRefused("source-config path must be a non-empty string")
    if not os.path.isfile(path):
        raise SourceConfigRefused(f"source config file not found: {path!r}")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise SourceConfigRefused(
            f"source config file unreadable: {path!r} ({type(exc).__name__})"
        ) from exc
    if size > _MAX_SOURCE_CONFIG_BYTES:
        raise SourceConfigRefused(
            f"source config file too large: {size} bytes > cap {_MAX_SOURCE_CONFIG_BYTES}"
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise SourceConfigRefused(
            f"source config file is not valid YAML: {path!r} ({exc})"
        ) from exc
    except OSError as exc:
        raise SourceConfigRefused(
            f"source config file unreadable: {path!r} ({type(exc).__name__})"
        ) from exc

    if raw is None:
        raise SourceConfigRefused(f"source config file is empty: {path!r}")
    if not isinstance(raw, dict):
        raise SourceConfigRefused(
            f"source config must be a YAML mapping (got {type(raw).__name__})"
        )

    # Top-level policy: ignore benign unrelated keys, refuse
    # only credential-shaped names. The CLI consumes ONLY
    # ``storage.embed``; every other top-level key (including
    # the ``storage`` mapping itself) is ignored unless its
    # name is credential-shaped.
    for k in raw.keys():
        if not isinstance(k, str):
            raise SourceConfigRefused(
                f"source config top-level key must be a string "
                f"(got {type(k).__name__})"
            )
        kl = k.lower()
        if kl in _FORBIDDEN_SOURCE_CONFIG_KEYS:
            raise SourceConfigRefused(
                f"source config top-level key {k!r} is forbidden"
            )
        if any(
            tok in kl for tok in (
                "password", "secret", "token", "api_key", "apikey",
            )
        ):
            raise SourceConfigRefused(
                f"source config top-level key {k!r} looks credential-like; "
                "refused (only storage.embed is consumed)"
            )
        # Benign unrelated top-level keys (e.g. ``basePath``,
        # ``pg``, ``rerank``, ``prompts``, ``foo``) are
        # silently ignored — the CLI reads them read-only and
        # consumes only ``storage.embed``.

    storage = raw.get("storage")
    if not isinstance(storage, dict):
        raise SourceConfigRefused(
            f"source config must contain a 'storage' mapping; "
            f"got {type(storage).__name__}"
        )
    embed = storage.get("embed")
    if embed is None:
        # An empty embed section is allowed — the semantic
        # stage will then refuse to make provider calls
        # (IntegrationTODOError).
        return {}
    if not isinstance(embed, dict):
        raise SourceConfigRefused(
            f"source config 'storage.embed' must be a mapping; "
            f"got {type(embed).__name__}"
        )

    # Refuse credential-shaped keys at any nesting depth.
    _assert_safe_dict(
        embed,
        forbidden=_FORBIDDEN_SOURCE_CONFIG_KEYS,
        where=f"source config {path!r} storage.embed",
        exc_cls=SourceConfigRefused,
    )
    return {k: v for k, v in embed.items() if isinstance(k, str)}


def _assert_safe_dict(
    cfg: Any, *, forbidden: set[str], where: str, exc_cls: type[CLIError]
) -> None:
    """Refuse a dict that carries forbidden keys.

    Used by :func:`load_source_config_embed_section` and
    :func:`build_isolated_lab_config` to enforce the strict
    safety contract.

    The exact lowercase keys ``api_key`` / ``apikey`` are
    accepted ONLY when ``where`` indicates ``storage.embed``
    (the source-config embed section or the isolated lab
    storage.embed section). Every other credential-shaped
    name — ``password``, ``secret``, ``token``,
    ``access_token``, ``private_key``, ``production_*``, and
    any key that merely contains the substring ``api_key`` or
    ``apikey`` (e.g. ``my_api_key``) — is still refused
    everywhere, including inside ``storage.embed``.
    """

    if not isinstance(cfg, dict):
        raise exc_cls(
            f"{where}: expected a dict (got {type(cfg).__name__})"
        )
    is_storage_embed = "storage.embed" in where
    for k in cfg.keys():
        if not isinstance(k, str):
            raise exc_cls(
                f"{where}: key must be a string (got {type(k).__name__})"
            )
        kl = k.lower()
        if kl in forbidden:
            raise exc_cls(
                f"{where}: forbidden config key {k!r}"
            )
        if any(tok in kl for tok in (
            "password", "secret", "token",
        )):
            raise exc_cls(
                f"{where}: config key {k!r} looks credential-like; refused"
            )
        # api_key / apikey: substring match is rejected everywhere
        # EXCEPT the exact lowercase forms ``api_key`` and
        # ``apikey`` when we are validating a storage.embed section.
        if "api_key" in kl or "apikey" in kl:
            if is_storage_embed and kl in {"api_key", "apikey"}:
                continue
            raise exc_cls(
                f"{where}: config key {k!r} looks credential-like; refused"
            )


# ---------------------------------------------------------------------
# Real empty disposable experiment directory
# ---------------------------------------------------------------------


# Files the evaluator itself creates inside the disposable experiment
# directory (the facade's sqlite cursor store). Anything else in there
# means the directory is NOT ours → refuse to reuse it.
_EVALUATOR_OWNED_BASE_FILES = frozenset({
    "v3_cards.db",
    "v3_cards.db-wal",
    "v3_cards.db-shm",
    "v3_cards.db-journal",
})


def _unexpected_base_entries(path: str) -> list[str]:
    """Return base-dir entries that the evaluator did not create itself."""

    try:
        entries = os.listdir(path)
    except OSError as exc:
        raise LabConfigRefused(
            f"base_path is not readable: {path!r} ({type(exc).__name__})"
        ) from exc
    return [e for e in entries if e not in _EVALUATOR_OWNED_BASE_FILES]


def _make_disposable_base_path(
    cache_dir: str, dataset_sha: str, scope: str | None = None,
) -> str:
    """Create a real empty disposable experiment directory.

    Returns the absolute path. Raises :class:`LabConfigRefused` if a
    pre-existing directory holds anything the evaluator did not create
    itself (the facade's own ``v3_cards.db`` may persist between runs,
    so it is explicitly allowed — that keeps reruns possible while
    still refusing to share an unrelated directory).

    The optional ``scope`` keyword lets the sample-isolated
    orchestrator derive a per-sample directory under the SAME
    ``cache_dir`` while the legacy default (``scope=None``) keeps
    today's path byte-identical: ``<cache_dir>/experiment-base-<sha12>``.
    When ``scope`` is set the path becomes
    ``<cache_dir>/experiment-base-<sha12>-<slug>`` where ``slug``
    is sanitised with the same ``[^a-z0-9]+`` rule used for the
    per-sample database name.
    """

    _ensure_dir(cache_dir)
    if scope is None or str(scope).strip() == "":
        dir_name = f"experiment-base-{dataset_sha[:12]}"
    else:
        slug = re.sub(r"[^a-z0-9]+", "_", str(scope).strip().lower()).strip("_")
        if not slug:
            raise LabConfigRefused(
                f"disposable base_path scope must produce a non-empty "
                f"slug after sanitisation (got {scope!r})"
            )
        dir_name = f"experiment-base-{dataset_sha[:12]}-{slug}"
    base_path = os.path.join(cache_dir, dir_name)
    if os.path.isdir(base_path):
        unexpected = _unexpected_base_entries(base_path)
        if unexpected:
            raise LabConfigRefused(
                f"experiment base_path already populated with foreign "
                f"entries: {base_path!r} -> {unexpected[:5]}"
            )
    else:
        os.makedirs(base_path, exist_ok=True)
    return base_path


# ---------------------------------------------------------------------
# Isolated plain-dict lab config
# ---------------------------------------------------------------------


def _dsn_to_pg_kwargs(parts: Mapping[str, str]) -> dict[str, Any]:
    """Convert validated DSN parts into psycopg2 kwargs.

    ``v3core.pg_store.PgEmbedStore._get_pg_config`` reads a dict
    config's ``storage.pg`` section as psycopg2 kwargs —
    ``host`` / ``port`` / ``database`` / ``user`` / ``password``.
    Any other shape (for example a bare ``dsn`` key) is silently
    ignored by the store, which then falls back to its own
    defaults (``localhost:5433`` / ``v3embeddings``) — i.e. the
    *production* endpoint. That fallback must be structurally
    impossible here, so every required field is spelled out and a
    missing one is a hard refusal rather than a silent default.
    """

    host = str(parts.get("host") or "").strip()
    database = str(parts.get("dbname") or "").strip()
    user = str(parts.get("user") or "").strip()
    port_raw = str(parts.get("port") or "").strip()
    if not host or not database or not user:
        raise LabConfigRefused(
            "lab DSN must spell out host, dbname and user so the "
            "isolated store can never fall back to a default endpoint"
        )
    if not port_raw:
        raise LabConfigRefused(
            "lab DSN must carry an explicit port so the isolated store "
            "can never fall back to the default port"
        )
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise LabConfigRefused(
            f"lab DSN port is not numeric: {port_raw!r}"
        ) from exc
    if port <= 0:
        raise LabConfigRefused(f"lab DSN port must be positive: {port}")
    return {
        "host": host,
        "port": port,
        "database": database,
        "user": user,
        "password": str(parts.get("password") or ""),
    }


def build_isolated_lab_config(
    *,
    base_path: str,
    dsn: str,
    output_dir: str,
    cache_dir: str,
    embed_cfg: Mapping[str, Any],
    rerank: bool = False,
    rerank_endpoint_env: str = "",
) -> dict[str, Any]:
    """Build a plain-dict isolated lab config — never a ``V3Config``.

    The dict is the single seam between the CLI and the
    embedding-preparation / runner modules. ``basePath`` is
    a real empty disposable experiment directory created by
    the CLI itself; ``storage.pg`` is the loopback lab DSN;
    ``storage.embed`` is the borrowed ephemeral provider
    config; rerank is disabled unless ``--rerank`` is set.
    """

    if not isinstance(base_path, str) or not base_path.strip():
        raise LabConfigRefused(
            "base_path must be a non-empty string pointing at a real "
            "disposable experiment directory"
        )
    if not os.path.isdir(base_path):
        raise LabConfigRefused(
            f"base_path must be a real existing directory; got {base_path!r}"
        )
    unexpected = _unexpected_base_entries(base_path)
    if unexpected:
        raise LabConfigRefused(
            f"base_path must be a disposable evaluator directory: "
            f"{base_path!r} already contains foreign entries "
            f"{unexpected[:5]} (refusing to share a non-disposable directory)"
        )

    if not isinstance(dsn, str) or not dsn.strip():
        raise LabConfigRefused("dsn must be a non-empty string")
    try:
        dsn_parts = _lab.validate_disposable_dsn(dsn)
    except _lab.LabDSNRefused as exc:
        raise LabConfigRefused(f"lab DSN refused: {exc}") from exc
    pg_kwargs = _dsn_to_pg_kwargs(dsn_parts)

    if not isinstance(output_dir, str) or not output_dir.strip():
        raise LabConfigRefused("output_dir must be a non-empty string")
    if not isinstance(cache_dir, str) or not cache_dir.strip():
        raise LabConfigRefused("cache_dir must be a non-empty string")

    if not isinstance(embed_cfg, Mapping):
        raise LabConfigRefused(
            "embed_cfg must be a mapping; pass the storage.embed sub-dict"
        )
    _assert_safe_dict(
        dict(embed_cfg),
        forbidden=_FORBIDDEN_SOURCE_CONFIG_KEYS,
        where="isolated lab config storage.embed",
        exc_cls=LabConfigRefused,
    )

    return {
        "lab": True,
        # A real empty disposable experiment directory, NOT
        # an empty string and NOT a fallback. Production
        # memory-store writes are never allowed.
        "basePath": str(base_path),
        "storage": {
            "pg": pg_kwargs,
            "embed": {k: v for k, v in dict(embed_cfg).items()},
        },
        # Rerank disabled by default; controlled by --rerank.
        "rerank_enabled": bool(rerank),
        # Env-var NAME only — the embedding-preparation module
        # resolves it at call time and never serialises the
        # resolved value.
        "rerank_endpoint_env": str(rerank_endpoint_env or ""),
        "output_dir": str(output_dir),
        "cache_dir": str(cache_dir),
    }


# ---------------------------------------------------------------------
# Dataset + manifest wiring (thin)
# ---------------------------------------------------------------------


def _resolve_dataset_and_rows(
    *,
    dataset_path: str,
    expected_sha256: str,
    case_limit: int | None,
    sample_filter: Sequence[str] | None,
    case_ids: Sequence[str] | None = None,
):
    """Load the dataset and apply case-limit / sample-filter / case-id.

    Returns ``(data, eval_rows, sample_ids_filter)``.

    ``case_ids`` is the G6C-B0 case-level diagnostic slice: a
    list of ``"<sample_id>|<query_idx>"`` ids (the runner's
    canonical case_id shape) that must match exactly an eval
    row's ``(sample_id, query_idx)`` pair. Unknown ids raise
    :class:`CLIError` so a typo in the runbook can never
    silently re-run an empty slice. Corpus import is unchanged
    — the import path still receives the FULL corpus for the
    selected sample scope, only the runner's case list narrows.
    """

    data = _dataset.load_locomo(dataset_path, expected_sha256)
    allowed: set[str] | None = (
        set(sample_filter) if sample_filter else None
    )
    if allowed is not None:
        allowed &= {sid for sid, _ in data.samples}
        if not allowed:
            raise CLIError(
                "sample-filter resolved to zero samples; refusing to run"
            )
    eval_rows = data.eval_rows
    # Apply the sample filter FIRST so ``case_limit`` counts cases
    # inside the requested samples (a canary such as
    # ``--sample-id conv-26 --max-cases 8`` must mean "the first 8
    # conv-26 rows", not "the first 8 rows overall, filtered after").
    if allowed is not None:
        eval_rows = tuple(er for er in eval_rows if er.sample_id in allowed)
    if case_limit is not None:
        if not isinstance(case_limit, int) or case_limit <= 0:
            raise CLIError("case-limit must be a positive integer")
        eval_rows = eval_rows[:case_limit]

    if case_ids:
        # Build the canonical case_id → eval-row index from the
        # post-sample-filter / post-case-limit slice. This means a
        # case_id outside the selected scope is unknown and we
        # refuse closed rather than silently dropping it.
        canonical = {f"{er.sample_id}|{er.query_idx}": er for er in eval_rows}
        unknown = [cid for cid in case_ids if cid not in canonical]
        if unknown:
            preview = ", ".join(unknown[:5])
            raise CLIError(
                "case-id filter contains unknown case_id(s) outside the "
                f"selected sample / case-limit scope: {preview!r}; "
                "refusing to run."
            )
        # Preserve the caller's order so the diff tool can replay
        # an exact slice. We dedupe but keep the FIRST occurrence
        # of each id.
        seen: set[str] = set()
        ordered: list[str] = []
        for cid in case_ids:
            if cid not in seen:
                seen.add(cid)
                ordered.append(cid)
        eval_rows = tuple(canonical[cid] for cid in ordered)
    return data, eval_rows, allowed


def _build_manifest(
    *,
    data,
    source_config_sha: str,
    embed_section: Mapping[str, Any],
    commit_sha: str,
    allowed_samples: set[str] | None,
    case_limit: int | None,
    search_mode: str | None = None,
    ivfflat_probes: int | None = None,
    index_build_phase: str | None = None,
    run_analyze: bool | None = None,
    case_ids: Sequence[str] | None = None,
    verified_search_mode: str | None = None,
    verified_enable_indexscan: str | None = None,
    verified_ivfflat_probes: int | None = None,
) -> dict[str, Any]:
    """Build the sanitised manifest via :mod:`manifest`."""

    provider_id = str(
        embed_section.get("provider_id") or DEFAULT_PROVIDER_ID
    ).strip() or DEFAULT_PROVIDER_ID
    model_id = str(
        embed_section.get("model") or embed_section.get("model_id") or DEFAULT_MODEL_ID
    ).strip() or DEFAULT_MODEL_ID
    embedding_dim_raw = embed_section.get("dim") or embed_section.get("dimension")
    try:
        embedding_dim = (
            int(embedding_dim_raw) if embedding_dim_raw else DEFAULT_EMBEDDING_DIM
        )
        if embedding_dim <= 0:
            embedding_dim = DEFAULT_EMBEDDING_DIM
    except (TypeError, ValueError):
        embedding_dim = DEFAULT_EMBEDDING_DIM

    manifest_obj = _manifest.build_manifest(
        data,
        commit_sha=commit_sha or None,
        provider_id=provider_id,
        model_id=model_id,
        embedding_dim=embedding_dim,
    )
    manifest_dict = manifest_obj.to_dict()
    manifest_dict["source_config_sha256"] = source_config_sha
    if allowed_samples is not None:
        manifest_dict["filtered_sample_ids"] = sorted(allowed_samples)
    if case_limit is not None:
        manifest_dict["case_limit"] = int(case_limit)
    # G6C-B0 determinism contract: the manifest must record the
    # requested AND the verified protocol so a downstream auditor
    # can compare the two — a "verified" field that disagrees
    # with the requested field is a signal the run was started
    # with one policy and finished with another, and the runbook
    # marks that as a contract violation.
    if search_mode is not None:
        manifest_dict["requested_search_mode"] = str(search_mode)
    if verified_search_mode is not None:
        manifest_dict["verified_search_mode"] = str(verified_search_mode)
    if verified_enable_indexscan is not None:
        manifest_dict["verified_enable_indexscan"] = str(verified_enable_indexscan)
    if ivfflat_probes is not None:
        manifest_dict["requested_ivfflat_probes"] = int(ivfflat_probes)
    if verified_ivfflat_probes is not None:
        manifest_dict["verified_ivfflat_probes"] = int(verified_ivfflat_probes)
    if index_build_phase is not None:
        manifest_dict["index_build_phase"] = str(index_build_phase)
    if run_analyze is not None:
        manifest_dict["run_analyze"] = bool(run_analyze)
    if case_ids is not None:
        manifest_dict["requested_case_ids"] = list(case_ids)
    return redact_secrets_in_payload(manifest_dict)


# ---------------------------------------------------------------------
# Dry-run stage
# ---------------------------------------------------------------------


def _coerce_dry_run_kwargs(
    args: argparse.Namespace | Mapping[str, Any] | None = None,
    /,
    **overrides: Any,
) -> dict[str, Any]:
    """Normalise the run_dry_run parameter shape.

    Accepts:

      * an :class:`argparse.Namespace` (legacy ``args=`` call);
      * a mapping of documented kwarg names;
      * direct keyword overrides.

    Resolves the backward-compatible aliases:

      * ``dataset_path`` and the legacy ``dataset`` attribute.
      * ``expected_sha256`` and the legacy ``expected_sha256`` attr.
      * ``source_config_path`` and the legacy ``source_config`` attr.

    Surfaces invalid kwarg names as :class:`TypeError` so the
    caller sees a loud failure rather than a silent fallback.
    """

    if args is None:
        src: Mapping[str, Any] = overrides
    elif isinstance(args, argparse.Namespace):
        src = {k: getattr(args, k) for k in (
            "dataset", "dataset_path",
            "expected_sha256",
            "source_config", "source_config_path",
            "dsn", "output_dir", "cache_dir",
            "rerank", "rerank_endpoint_env",
            "write_rows", "commit_sha",
            "case_limit", "sample_filter", "sample_ids",
            "case_ids",
            "batch_size", "mode",
            "search_mode", "ivfflat_probes",
            "index_build_phase", "run_analyze",
        ) if hasattr(args, k)}
        src.update(overrides)
    elif isinstance(args, Mapping):
        src = dict(args)
        src.update(overrides)
    else:
        raise TypeError(
            "run_dry_run: first positional must be argparse.Namespace, "
            "Mapping, or None; got "
            f"{type(args).__name__}"
        )

    dataset_path = src.get("dataset_path")
    if dataset_path is None:
        dataset_path = src.get("dataset")
    if not dataset_path:
        raise TypeError(
            "run_dry_run: missing required argument 'dataset_path' "
            "(alias: 'dataset')"
        )

    expected_sha256 = src.get("expected_sha256")
    if not expected_sha256:
        raise TypeError(
            "run_dry_run: missing required argument 'expected_sha256'"
        )

    source_config_path = src.get("source_config_path")
    if source_config_path is None:
        source_config_path = src.get("source_config")
    if not source_config_path:
        raise TypeError(
            "run_dry_run: missing required argument 'source_config_path' "
            "(alias: 'source_config')"
        )

    raw_filter = src.get("sample_filter")
    extra_ids = src.get("sample_ids")
    merged_filter: Any = raw_filter
    if extra_ids:
        # ``--sample-id`` action='append' produces either a flat
        # string list or a list of comma-separated strings.
        for chunk in extra_ids:
            if chunk is None:
                continue
            for piece in str(chunk).split(","):
                piece = piece.strip()
                if not piece:
                    continue
                if isinstance(merged_filter, str) or merged_filter is None:
                    base = merged_filter or ""
                    merged_filter = (
                        f"{base},{piece}" if base else piece
                    )
                else:
                    merged_filter = list(merged_filter) + [piece]

    return {
        "dataset_path": str(dataset_path),
        "expected_sha256": str(expected_sha256),
        "source_config_path": str(source_config_path),
        "dsn": str(src.get("dsn", "") or ""),
        "output_dir": str(src.get("output_dir", "") or ""),
        "cache_dir": str(src.get("cache_dir", "") or ""),
        "rerank": bool(src.get("rerank", False)),
        "rerank_endpoint_env": str(
            src.get("rerank_endpoint_env", "") or ""
        ),
        "write_rows": bool(src.get("write_rows", False)),
        "commit_sha": str(src.get("commit_sha", "") or ""),
        "case_limit": src.get("case_limit"),
        "sample_filter": merged_filter,
        "case_ids": src.get("case_ids"),
        "batch_size": src.get("batch_size"),
        "mode": str(src.get("mode", "dry-run") or "dry-run"),
        # G6C-B0 knobs — passed through verbatim; the validator
        # functions in :mod:`search_protocol` / :mod:`lab` are
        # the canonical fail-closed gate.
        "search_mode": src.get("search_mode"),
        "ivfflat_probes": src.get("ivfflat_probes"),
        "index_build_phase": src.get("index_build_phase"),
        "run_analyze": src.get("run_analyze"),
    }


def run_dry_run(
    args: argparse.Namespace | Mapping[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> StructuralResult:
    """Execute the dry-run stage — no PG, no provider, no HTTP.

    Accepts either an :class:`argparse.Namespace` (legacy shape)
    or direct keyword arguments. The documented keyword names
    are:

      * ``dataset_path`` (alias: ``dataset``)
      * ``expected_sha256``
      * ``source_config_path`` (alias: ``source_config``)
      * ``dsn``
      * ``output_dir``
      * ``cache_dir``
      * ``rerank`` (bool)
      * ``rerank_endpoint_env`` (str)
      * ``write_rows`` (bool)
      * ``commit_sha`` (str)
      * ``case_limit`` (int | None)
      * ``sample_filter`` (sequence of str | str | None)
      * ``case_ids`` (sequence of str | str | None) — G6C-B0
        case-level diagnostic slice (``"<sample_id>|<query_idx>"``).
      * ``batch_size`` (int | None)
      * ``mode`` (str; informational)
      * ``search_mode`` (str | None) — ``"exact"`` / ``"ann"``;
        validated by :mod:`search_protocol`.
      * ``ivfflat_probes`` (int | None) — explicit ``ivfflat.probes``
        override for ``search_mode='ann'``. Refused (fail-closed)
        when ``search_mode != 'ann'``.
      * ``index_build_phase`` (str | None) —
        ``"bootstrap_before_import"`` (default) /
        ``"after_import"`` (G6C-B0 Mode C).
      * ``run_analyze`` (bool | None) — when truthy, the
        ``semantic`` stage will issue ``ANALYZE`` on the canonical
        corpus tables after import (and again after deferred
        index creation when ``index_build_phase='after_import'``).

    Returns a :class:`StructuralResult`.
    """

    params = _coerce_dry_run_kwargs(args, **kwargs)
    (
        dataset_path, expected_sha256, source_config_path, dsn,
        output_dir, cache_dir, rerank, rerank_endpoint_env,
        write_rows, commit_sha, case_limit, sample_filter,
        case_ids, _batch_size, _mode,
        search_mode, ivfflat_probes, index_build_phase, run_analyze,
    ) = (
        params["dataset_path"], params["expected_sha256"],
        params["source_config_path"], params["dsn"],
        params["output_dir"], params["cache_dir"], params["rerank"],
        params["rerank_endpoint_env"], params["write_rows"],
        params["commit_sha"], params["case_limit"],
        params["sample_filter"], params["case_ids"],
        params["batch_size"], params["mode"],
        params["search_mode"], params["ivfflat_probes"],
        params["index_build_phase"], params["run_analyze"],
    )

    # Validate the G6C-B0 knobs as early as possible so the
    # dry-run stage fails closed before the manifest is
    # written. We delegate to the module validators so the
    # contracts are single-sourced.
    from .search_protocol import SearchProtocol, SearchProtocolError as _SPerr
    from .lab import ALLOWED_INDEX_BUILD_PHASES
    try:
        sp_policy = SearchProtocol.parse(mode=search_mode, probes=ivfflat_probes)
    except _SPerr as exc:
        raise CLIError(f"cli: invalid search_mode / ivfflat_probes: {exc}") from exc
    if index_build_phase is not None and str(index_build_phase) not in ALLOWED_INDEX_BUILD_PHASES:
        raise CLIError(
            f"cli: invalid --index-build-phase {index_build_phase!r}; "
            f"expected one of {sorted(ALLOWED_INDEX_BUILD_PHASES)!r}"
        )
    if ivfflat_probes is not None and str(sp_policy.mode) != "ann":
        raise CLIError(
            "cli: --ivfflat-probes is only valid with --search-mode ann; "
            f"got search_mode={sp_policy.mode!r}"
        )

    sha = _dataset.verify_source(dataset_path, expected_sha256)
    source_config_sha = _file_sha256(source_config_path)
    embed_section = load_source_config_embed_section(source_config_path)

    if isinstance(sample_filter, str):
        parsed_filter = tuple(
            s.strip() for s in sample_filter.split(",") if s.strip()
        ) or None
    elif sample_filter is None:
        parsed_filter = None
    else:
        parsed_filter = tuple(str(s).strip() for s in sample_filter if str(s).strip()) or None

    # Normalise case_ids: ``--case-id foo --case-id bar`` arrives
    # as a list; legacy ``--case-id a,b`` as a single string. We
    # accept either shape and trim whitespace.
    if isinstance(case_ids, str):
        parsed_case_ids = tuple(
            c.strip() for c in case_ids.split(",") if c.strip()
        ) or None
    elif case_ids is None:
        parsed_case_ids = None
    else:
        parsed_case_ids = tuple(str(c).strip() for c in case_ids if str(c).strip()) or None

    data, eval_rows, allowed_samples = _resolve_dataset_and_rows(
        dataset_path=dataset_path,
        expected_sha256=str(sha),
        case_limit=case_limit,
        sample_filter=parsed_filter,
        case_ids=parsed_case_ids,
    )

    _dataset.build_evidence_map(data)
    rows = _dataset.build_import_rows(
        data,
        sample_ids=allowed_samples,
    )

    manifest_payload = _build_manifest(
        data=data,
        source_config_sha=source_config_sha,
        embed_section=embed_section,
        commit_sha=commit_sha,
        allowed_samples=allowed_samples,
        case_limit=case_limit,
        search_mode=sp_policy.mode,
        ivfflat_probes=sp_policy.probes,
        index_build_phase=index_build_phase,
        run_analyze=bool(run_analyze) if run_analyze is not None else None,
        case_ids=parsed_case_ids,
    )

    base_path = _make_disposable_base_path(
        cache_dir, str(sha)
    )
    lab_cfg = build_isolated_lab_config(
        base_path=base_path,
        dsn=dsn,
        output_dir=output_dir,
        cache_dir=cache_dir,
        embed_cfg=embed_section,
        rerank=rerank,
        rerank_endpoint_env=rerank_endpoint_env,
    )

    _ensure_dir(output_dir)
    manifest_path = os.path.join(output_dir, "benchmark-manifest.json")
    _write_json_atomic(manifest_path, manifest_payload)

    if write_rows:
        rows_path = os.path.join(output_dir, "import-rows.json")
        rows_payload = {
            "counts": {k: len(v) for k, v in rows.items()},
            "qa_pairs": list(rows["qa_pairs"]),
            "conversation_stream": list(rows["conversation_stream"]),
            "eval_queries": list(rows["eval_queries"]),
        }
        _write_json_atomic(rows_path, rows_payload)

    return StructuralResult(
        dataset_sha256=str(sha),
        source_config_sha256=source_config_sha,
        sample_count=len(data.samples),
        session_count=sum(len(s.sessions) for _sid, s in data.samples),
        message_count=int(data.message_count),
        qa_pair_count=int(data.qa_pair_count),
        eval_row_count=int(len(eval_rows)),
        manifest=dict(manifest_payload),
        manifest_path=str(manifest_path),
        lab_config=dict(lab_cfg),
        base_path=str(lab_cfg["basePath"]),
    )


# ---------------------------------------------------------------------
# Semantic stage
# ---------------------------------------------------------------------


def _connect_lab(dsn: str):
    """Open a single psycopg2 connection to the disposable DSN."""

    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise IntegrationTODOError(
            "psycopg2 is required for the semantic stage; install it "
            "or use --mode dry-run"
        ) from exc
    try:
        return psycopg2.connect(dsn)
    except Exception as exc:
        raise LabConfigRefused(
            f"semantic stage: cannot open DSN: {type(exc).__name__}"
        ) from exc


def _preflight_probe_availability(
    dsn: str,
    sp_policy: SearchProtocol,
) -> dict[str, Any]:
    """Disposable-DSN preflight — ivfflat.probes GUC registration.

    Opens ONE short-lived connection to the disposable lab DSN
    and asks PostgreSQL whether ``ivfflat.probes`` is registered
    in ``pg_settings`` AFTER forcing the pgvector shared
    library into the backend.  A registered GUC means the
    planner actually reads it; an unregistered one means any
    ``set_config`` only creates a custom variable the planner
    ignores — a fake round-trip.

    Load-order matters: the pgvector shared library registers
    ``ivfflat.probes`` LAZILY, when the library is first loaded
    into a backend.  A fresh backend that has not executed any
    pgvector function has NO row in ``pg_settings`` for
    ``ivfflat.probes`` even on a perfectly healthy pgvector
    build.  This preflight therefore:

      1. Reads the registration BEFORE the forced load.
      2. Forces the load via
         :func:`search_protocol.ensure_pgvector_loaded` (tries
         ``LOAD 'vector'`` first, then falls back to executing
         a real pgvector function).
      3. Reads the registration AGAIN.

    The verdict (``probe_availability``) is the AFTER-load
    read; the BEFORE-load read and the chosen load method are
    surfaced alongside as evidence fields so the manifest can
    carry the load-order truth.

    Behavioural contract:

      * Explicit ``sp_policy.probes`` + ``UNAVAILABLE`` (after
        load): REFUSED.  Raise :class:`CLIError` so
        ``bootstrap_schema`` / ``import_rows`` never run.  A run
        that would only echo a custom variable cannot measure
        ANN probe count and is a contract violation.  The error
        message explains the library-load ordering and that a
        failed load of ``'vector'`` left the GUC unregistered.
      * Explicit probes + ``AVAILABLE``: no failure; return a
        verdict dict so the manifest can stamp the available
        status.
      * Implicit probes + ``UNAVAILABLE``: no failure (the
        planner-default path still works); return the verdict
        so the auditor sees ``probe_availability='UNAVAILABLE'``.
      * Implicit probes + ``AVAILABLE``: verdict is
        ``AVAILABLE``; existing semantics unchanged.

    The preflight reuses :func:`_connect_lab` so the disposable
    DSN guard (lab vs production) is the single source of truth.
    """

    conn = None
    registered_before: bool = False
    registered_after: bool = False
    load_method: str = PROBE_LOAD_METHOD_NONE
    try:
        conn = _connect_lab(dsn)
        try:
            cur = conn.cursor()
        except Exception as exc:
            raise CLIError(
                "cli: preflight probe-availability cursor failed: "
                f"{type(exc).__name__}"
            ) from exc
        try:
            try:
                # BEFORE the forced load — a fresh backend
                # typically has NO row in pg_settings for
                # ivfflat.probes until the shared library is
                # loaded.
                cur.execute(
                    "SELECT 1 FROM pg_settings WHERE name = %s LIMIT 1",
                    ("ivfflat.probes",),
                )
                row_before = cur.fetchone()
            except Exception as exc:
                raise CLIError(
                    "cli: preflight probe-availability pg_settings "
                    f"lookup (before load) failed: {type(exc).__name__}"
                ) from exc
            registered_before = bool(
                row_before is not None
                and len(row_before) >= 1
                and row_before[0] is not None
            )

            # Force the pgvector shared library into this
            # backend so a lazy-load lag cannot produce a
            # false UNAVAILABLE verdict.
            load_method = ensure_pgvector_loaded(cur)

            try:
                # AFTER the forced load — this is the verdict
                # gate.
                cur.execute(
                    "SELECT 1 FROM pg_settings WHERE name = %s LIMIT 1",
                    ("ivfflat.probes",),
                )
                row_after = cur.fetchone()
            except Exception as exc:
                raise CLIError(
                    "cli: preflight probe-availability pg_settings "
                    f"lookup (after load) failed: {type(exc).__name__}"
                ) from exc
            registered_after = bool(
                row_after is not None
                and len(row_after) >= 1
                and row_after[0] is not None
            )
        finally:
            try:
                cur.close()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    availability = (
        PROBE_AVAILABILITY_AVAILABLE
        if registered_after
        else PROBE_AVAILABILITY_UNAVAILABLE
    )

    # Fail-closed: explicit probes require a registered GUC.
    if (
        availability == PROBE_AVAILABILITY_UNAVAILABLE
        and str(sp_policy.mode).strip().lower() == SEARCH_MODE_ANN
        and sp_policy.probes is not None
    ):
        raise CLIError(
            "cli: --ivfflat-probes N requires ivfflat.probes to be "
            "registered in pg_settings on the disposable lab; "
            "even after forcing the pgvector shared library load "
            "(via 'LOAD vector' / executing a pgvector function) "
            "the preflight found NO row. A failed load of 'vector' "
            "left the GUC unregistered, so a SET would only create "
            "a custom-GUC echo that the planner never reads. The "
            "explicit override is refused BEFORE any "
            "bootstrap_schema / import_rows runs. Use a pgvector "
            "build whose shared library registers ivfflat.probes "
            "once loaded, or drop --ivfflat-probes."
        )

    availability_before = (
        PROBE_AVAILABILITY_AVAILABLE
        if registered_before
        else PROBE_AVAILABILITY_UNAVAILABLE
    )

    return {
        "probe_availability": availability,
        "probe_availability_source": (
            "pg_settings_after_library_load"
            if registered_after
            else "pg_settings_absent_after_library_load"
        ),
        "probe_availability_preflight": "disposable_dsn",
        "probe_availability_before_load": availability_before,
        "probe_availability_after_load": availability,
        "pgvector_load_method": load_method,
    }


def _resolve_repo_root() -> str:
    """Locate the repository root that holds ``src/v3-core/schema``.

    ``lab.bootstrap_schema`` composes ``<repo_root>/src/v3-core/schema``,
    so this must return the worktree root, not the package's own
    directory. The search walks upward from this module and returns the
    first ancestor that actually contains the alpha bootstrap DDL;
    a packaged/installed copy falls back to the schema locate behaviour
    of the lab module itself (which fails closed with a precise error).
    """

    try:
        from . import _REPO_ROOT_FOR_LAB  # type: ignore[attr-defined]
        return _REPO_ROOT_FOR_LAB
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = here
    for _ in range(8):
        candidate = os.path.dirname(candidate)
        if not candidate or candidate == os.path.dirname(candidate):
            break
        if os.path.isfile(os.path.join(
            candidate, "src", "v3-core", "schema", "alpha_bootstrap.sql"
        )):
            return candidate
    return os.path.normpath(os.path.join(here, "..", "..", "..", ".."))


def _parse_query_row_key(row_id: str) -> tuple[str, int] | None:
    """Parse a query row id (``locomo|eval_v2|<sample_id>|q<N>``).

    The query namespace has FOUR segments — it is not a QA-pair
    ``source_id`` (five segments with a ``q>a`` tail), so
    :func:`dataset.parse_source_id` (the strict QA-pair inverse) must
    not be used here; doing so silently yields an empty mapping.
    """

    parts = str(row_id or "").split("|")
    if len(parts) != 4 or parts[0] != "locomo" or parts[1] != "eval_v2":
        return None
    sample_id, tail = parts[2], parts[3]
    if not sample_id or not tail.startswith("q"):
        return None
    try:
        return sample_id, int(tail[1:])
    except ValueError:
        return None


def _build_q_emb_by_case(
    *,
    query_rows: Sequence[_embeddings.EmbedRow],
    query_results: Sequence[_embeddings.EmbedResult],
) -> dict[str, list[float]]:
    """Index query vectors by the runner's canonical ``case_id``.

    ``case_id`` is ``"<sample_id>|<query_idx>"`` — the same stable
    key the adapter/runner use — so the objective per-case mapping
    lines up with the engine's per-case lookup without a second
    translation layer.
    """

    out: dict[str, list[float]] = {}
    for qres, qrow in zip(query_results, query_rows):
        parsed = _parse_query_row_key(qrow.row_id)
        if parsed is None:
            continue
        sample_id, query_idx = parsed
        out[f"{sample_id}|{query_idx}"] = list(qres.vector)
    return out


def _run_query_embed_pass(
    *,
    eval_rows: Sequence[Any],
    embed_section: Mapping[str, Any],
    cache_dir: str,
    dataset_sha: str,
    batch_size: int,
) -> tuple[
    Sequence[_embeddings.EmbedRow],
    Sequence[_embeddings.EmbedResult],
    Any,
]:
    """Thin seam around the query-embedding preparation pass.

    Wraps :func:`embeddings.build_query_rows` and
    :func:`embeddings.prepare_query_embeddings` so the
    semantic stage has a single monkeypatch point tests can
    use to surface :class:`IntegrationTODOError`. Real semantic
    runs call the embeddings module directly — there is no
    fabrication here.

    Returns ``(query_rows, query_results, query_stats)``.
    """

    embed_factory_input = {"embed": dict(embed_section)}
    query_rows = _embeddings.build_query_rows(eval_rows)
    query_results, query_stats = _embeddings.prepare_query_embeddings(
        list(query_rows),
        cfg=embed_factory_input,
        cache_root=cache_dir,
        dataset_sha=dataset_sha,
        batch_size=int(batch_size),
    )
    return query_rows, query_results, query_stats


def _corpus_embed_rows(
    rows: Sequence[dict[str, Any]], *, kind: str
) -> list[_embeddings.EmbedRow]:
    """Build ``EmbedRow`` objects aligned 1:1 with the import rows.

    The evaluator embeds exactly the text production stores in the
    corresponding columns, and the row order here is the very order
    :func:`dataset.build_import_rows` produced (and, for the
    ``conversation_stream`` family, the ``(sample asc, session asc,
    index asc)`` order shared with :func:`dataset.iter_messages`), so
    the returned ``result[i].vector`` belongs to ``rows[i]``.
    """

    out: list[_embeddings.EmbedRow] = []
    for row in rows:
        if kind == "qa_pairs":
            out.append(_embeddings.EmbedRow(
                row_id=str(row.get("source_id") or ""),
                text=str(row.get("question") or ""),
                answer=str(row.get("answer") or ""),
                provenance=None,
            ))
            continue
        if kind == "conversation_stream":
            prov = {}
            tcs = row.get("tool_calls")
            if isinstance(tcs, list) and tcs and isinstance(tcs[0], Mapping):
                prov = dict(tcs[0])
            sid = str(prov.get("sample_id") or "")
            skey = str(prov.get("session_key") or "")
            try:
                idx = int(prov.get("index") or 0)
            except (TypeError, ValueError):
                idx = 0
            out.append(_embeddings.EmbedRow(
                row_id=f"cs|locomo|{sid}|{skey}|{idx}",
                text=str(row.get("content") or ""),
                answer=None,
                provenance=prov or None,
            ))
            continue
        raise CLIError(f"unknown corpus kind {kind!r}")
    return out


def _prepare_corpus_vectors(
    *,
    rows: list[dict[str, Any]],
    kind: str,
    embed_cfg_input: Mapping[str, Any],
    cache_dir: str,
    dataset_sha: str,
    batch_size: int,
) -> Any:
    """Prepare the corpus vectors for one import-row family.

    Mutates ``rows`` in place by writing the provider vector into each
    row's ``embedding`` cell — the importer then stores a real
    ``vector(1024)`` instead of ``NULL``, which is what makes the
    vector lanes non-inert. Fails closed when the provider returns a
    different number of vectors than rows.
    """

    embed_rows = _corpus_embed_rows(rows, kind=kind)
    if len(embed_rows) != len(rows):
        raise CLIError(
            f"corpus embed-row mismatch for {kind}: "
            f"{len(embed_rows)} rows vs {len(rows)} import rows"
        )
    results, stats = _embeddings.prepare_corpus_embeddings(
        embed_rows,
        cfg=embed_cfg_input,
        kind=kind,
        cache_root=cache_dir,
        dataset_sha=dataset_sha,
        batch_size=int(batch_size),
    )
    if len(results) != len(rows):
        raise CLIError(
            f"corpus vector count mismatch for {kind}: "
            f"{len(results)} vectors vs {len(rows)} import rows"
        )
    for i, (res, row) in enumerate(zip(results, rows)):
        if str(res.row_id) != str(embed_rows[i].row_id):
            raise CLIError(
                f"corpus vector identity mismatch for {kind} at index {i}: "
                f"{res.row_id!r} != {embed_rows[i].row_id!r}"
            )
        row["embedding"] = [float(x) for x in res.vector]
    return stats


def _read_back_provenance(
    conn: Any,
) -> tuple[dict[int, str], list[dict[str, Any]]]:
    """Read back the assigned ids + provenance for the lab rows.

    ``qa_pairs`` / ``conversation_stream`` are BIGSERIAL-keyed, so the
    production-shaped candidate ids the engine emits (``qa_<id>`` and
    ``<id>``) can only be resolved after the import. This is
    import-bookkeeping (ids + the row's own ``tool_calls`` provenance),
    not retrieval SQL: no ranking, no candidate selection, nothing
    that could influence what the engine returns.
    """

    qa_id_index: dict[int, str] = {}
    conv_rows: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute("SELECT id, source_id FROM public.qa_pairs")
        for row in cur.fetchall():
            try:
                qa_id_index[int(row[0])] = str(row[1])
            except (TypeError, ValueError):
                continue
        cur.execute("SELECT id, tool_calls FROM public.conversation_stream")
        for row in cur.fetchall():
            try:
                rid = int(row[0])
            except (TypeError, ValueError):
                continue
            conv_rows.append({"id": rid, "tool_calls": row[1] or []})
    return qa_id_index, conv_rows


def _make_lab_store(
    lab_cfg: Mapping[str, Any],
    *,
    pool: Any | None = None,
) -> Any:
    """Instantiate the canonical PG store against the isolated lab.

    The store is given the isolated plain dict (never a production
    ``V3Config``); ``storage.pg`` carries explicit psycopg2 kwargs so
    the store cannot fall back to its built-in production defaults.

    When ``pool`` is supplied (the G6C-B0 wiring path), the store
    forwards it to ``PgEmbedStore(pool=...)`` so every leased
    connection goes through the evaluator-only session pool —
    which means the strict exact/ANN search protocol is applied
    and verified on every lease, not just the first one.
    """

    from v3core.pg_store import PgEmbedStore  # lazy: keep import cheap

    if pool is not None:
        return PgEmbedStore(config=dict(lab_cfg), pool=pool)
    return PgEmbedStore(config=dict(lab_cfg))


def _build_runner_cases(
    *, data, eval_rows
) -> list[dict[str, Any]]:
    """Build the case dict list :func:`runner.run_case` consumes."""

    evidence_map = _dataset.build_evidence_map(data)
    cases: list[dict[str, Any]] = []
    for er in eval_rows:
        gold = _dataset.resolve_gold_evidence(data, er, evidence_map)
        cases.append({
            "sample_id": str(er.sample_id),
            "query_idx": int(er.query_idx),
            "question": str(er.question),
            "answer": str(er.answer),
            "gold_evidence_dia_ids": tuple(er.evidence),
            "gold_source_ids": list(gold.source_ids),
            # All three gold-evidence states are forwarded separately:
            # mapped source ids (headline denominator), unmapped dia
            # ids (evidence exists but no pair maps it) and unresolved
            # entries (compound/malformed, never split heuristically).
            "unmapped_dia_ids": list(gold.unmapped_dia_ids),
            "unresolved_evidence": list(gold.unresolved),
            "category": str(er.category),
        })
    return cases


def _run_facade_pass(
    *,
    cases: Sequence[dict[str, Any]],
    q_emb_by_case: Mapping[str, Sequence[float]],
    config: Mapping[str, Any] | None = None,
    pg: Any = None,
    expected_dim: int = 1024,
) -> list[Any]:
    """Run the production-shaped facade sequentially.

    The runner is invoked once per case via
    :func:`runner.run_case` so every per-case adapter
    invocation is the canonical production facade path. The
    isolated lab config and the lab-bound PG store are forwarded
    so the engine reads the disposable corpus (never a default /
    production endpoint), and the adapter runs in ``objective``
    mode so every case must carry its own non-zero query vector —
    a degenerate vector aborts the batch instead of silently
    degrading to a keyword-only run.
    """

    out: list[Any] = []
    for case in cases:
        out.append(
            _runner.run_case(
                sample_id=str(case["sample_id"]),
                query_idx=int(case["query_idx"]),
                question=str(case["question"]),
                gold_answer=str(case["answer"]),
                gold_evidence_dia_ids=tuple(case["gold_evidence_dia_ids"]),
                gold_source_ids=tuple(case["gold_source_ids"]),
                unmapped_dia_ids=tuple(case.get("unmapped_dia_ids") or ()),
                unresolved_evidence=tuple(case["unresolved_evidence"]),
                category=str(case["category"]),
                limit=5,
                config=config,
                pg=pg,
                adapter_mode=_runner.MODE_OBJECTIVE,
                q_emb_by_case=q_emb_by_case,
                expected_dim=int(expected_dim),
            )
        )
    return out


def _map_record_rankings(
    records: Sequence[Any],
    provenance_map: Mapping[str, Any],
) -> tuple[list[Any], dict[str, int]]:
    """Map ranked / selected / candidate / injected IDs through the
    provenance map so metrics are computed over canonical LoCoMo
    ``source_id`` strings.

    ``CaseRecord`` is a **frozen** dataclass, so a new record is built
    per case with :func:`dataclasses.replace`. (The first version of
    this helper used ``setattr`` inside a ``try/except: pass`` and the
    ``FrozenInstanceError`` was swallowed — every ranking silently
    stayed in raw ``qa_<id>`` form and the metrics could never match a
    gold ``source_id``.)

    Returns ``(mapped_records, audit_counts)``. The audit counts sum the
    per-case resolution tallies so a run can report how many ranking
    entries actually resolved to a canonical id.
    """

    qa_map = provenance_map.get("qa_id_to_source_id", {})
    conv_map = provenance_map.get("conv_id_to_source_id", {})
    topic_map = provenance_map.get("topic_id_to_source_id", {})

    out: list[Any] = []
    audit: dict[str, int] = {
        "input": 0, "resolved_qa": 0, "resolved_conv": 0,
        "resolved_topic": 0, "unresolved": 0, "unknown_shape": 0,
    }

    for rec in records:
        ranked_raw = list(getattr(rec, "ranked_source_ids", ()) or ())
        selected_raw = list(getattr(rec, "selected_source_ids", ()) or ())
        candidates_raw = list(getattr(rec, "candidate_source_ids", ()) or ())
        injected_raw = list(getattr(rec, "injected_source_ids", ()) or ())

        resolved = _lab.map_candidate_ids(
            list(ranked_raw) + list(selected_raw)
            + list(candidates_raw) + list(injected_raw),
            qa_id_to_source_id=qa_map,
            conv_id_to_source_id=conv_map,
            topic_id_to_source_id=topic_map,
        )
        for key, value in (resolved.get("counts") or {}).items():
            audit[key] = audit.get(key, 0) + int(value or 0)

        def _map_list(lst: Sequence[str]) -> tuple[str, ...]:
            mapped: list[str] = []
            seen: set[str] = set()
            for cid in lst:
                sid = resolved["resolved"].get(cid)
                if sid is None or sid in seen:
                    continue
                seen.add(sid)
                mapped.append(sid)
            return tuple(mapped)

        out.append(dataclasses.replace(
            rec,
            ranked_source_ids=_map_list(ranked_raw),
            selected_source_ids=_map_list(selected_raw),
            candidate_source_ids=_map_list(candidates_raw),
            injected_source_ids=_map_list(injected_raw),
        ))

    return out, audit


def _resolve_fingerprint(
    embed_section: Mapping[str, Any]
) -> dict[str, Any]:
    """Run the canonical ``build_embed_cfg`` factory and return
    a sanitised fingerprint dict (endpoint stripped)."""

    try:
        from v3core import embedding as _v3_emb
        canonical = _v3_emb.build_embed_cfg({"embed": dict(embed_section)})
    except ValueError as exc:
        raise IntegrationTODOError(
            f"semantic stage: source config storage.embed is incomplete "
            f"(factory raised {type(exc).__name__}: {exc})"
        ) from exc
    profile = canonical.get("_profile") if isinstance(canonical, dict) else None
    if profile is None:
        raise IntegrationTODOError(
            "semantic stage: build_embed_cfg returned no EmbedProfile"
        )
    return _strip_endpoint_from_profile({
        "provider": str(getattr(profile, "provider", "") or ""),
        "model": str(getattr(profile, "model", "") or ""),
        "dim": int(getattr(profile, "dim", 0) or 0),
        "pooling": str(getattr(profile, "pooling", "") or ""),
        "normalization": bool(getattr(profile, "normalization", False)),
        "request_format": str(getattr(profile, "request_format", "") or ""),
        "fingerprint": str(canonical.get("_fingerprint", "") or ""),
    })


def _latency_rows(
    records: Sequence[Any],
) -> tuple[list[str], list[list[Any]]]:
    """Build (header, rows) for ``latency.csv``.

    ``CaseRecord`` exposes ``elapsed_ms``; a legacy ``latency_ms``
    attribute is still honoured so external duck-typed records work.
    """

    def _ms(rec: Any) -> float:
        for attr in ("elapsed_ms", "latency_ms"):
            value = getattr(rec, attr, None)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return 0.0

    return (
        ["case_id", "latency_ms", "status"],
        [
            [
                str(getattr(rec, "case_id", "") or ""),
                _ms(rec),
                str(getattr(rec, "status", "") or ""),
            ]
            for rec in records
        ],
    )


def _read_latency_csv(path: str) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                out[str(row.get("case_id", ""))] = float(
                    row.get("latency_ms", 0.0) or 0.0
                )
    except OSError:
        pass
    return out


def _compare_repeatability(
    *,
    baseline_jsonl: str,
    repeat_jsonl: str,
    metrics_baseline: _metrics.MetricSnapshot,
    metrics_repeat: _metrics.MetricSnapshot,
    baseline_csv: str,
    repeat_csv: str,
) -> dict[str, Any]:
    """Compare two JSONL runs by ``case_id`` and report
    metric / latency deltas without invoking the provider.
    """

    delta_dict = _compare.compare_files(baseline_jsonl, repeat_jsonl).to_dict()
    base_d = metrics_baseline.to_dict()
    repeat_d = metrics_repeat.to_dict()

    def _f(k: str):
        return base_d.get(k)

    def _r(k: str):
        return repeat_d.get(k)

    def _delta(k: str):
        b, r = _f(k), _r(k)
        if b is None or r is None:
            return None
        try:
            return float(r) - float(b)
        except (TypeError, ValueError):
            return None

    metric_deltas = {
        "hit_at_1_delta": _delta("hit_at_1"),
        "hit_at_5_delta": _delta("hit_at_5"),
        "hit_at_k_delta": _delta("hit_at_k"),
        "mrr_delta": _delta("mrr"),
        "mean_relevant_rank_delta": _delta("mean_relevant_rank"),
    }

    base_lat = _read_latency_csv(baseline_csv)
    rep_lat = _read_latency_csv(repeat_csv)
    per_case_latency: list[dict[str, Any]] = []
    deltas: list[float] = []
    for case_id in sorted(set(base_lat) | set(rep_lat)):
        b = base_lat.get(case_id)
        r = rep_lat.get(case_id)
        if b is None or r is None:
            per_case_latency.append({
                "case_id": case_id, "baseline_ms": b,
                "repeat_ms": r, "delta_ms": None,
            })
            continue
        d = float(r) - float(b)
        deltas.append(d)
        per_case_latency.append({
            "case_id": case_id, "baseline_ms": float(b),
            "repeat_ms": float(r), "delta_ms": float(d),
        })

    summary_latency = {
        "cases_compared": len(per_case_latency),
        "max_abs_delta_ms": (
            max((abs(x) for x in deltas), default=None)
        ),
        "mean_delta_ms": (
            float(sum(deltas) / len(deltas)) if deltas else None
        ),
    }

    return {
        "delta": delta_dict,
        "metric_deltas": metric_deltas,
        "per_case_latency": per_case_latency,
        "summary": summary_latency,
        "metric_status_baseline": str(base_d.get("status", "")),
        "metric_status_repeat": str(repeat_d.get("status", "")),
    }


def run_semantic(
    args: argparse.Namespace,
    *,
    _records_out: list | None = None,
) -> dict[str, Any]:
    """Execute the semantic stage.

    Thin orchestrator over the existing modules:

      1. Load dataset + apply filters (including ``--case-id``).
      2. Resolve the G6C-B0 policy (search mode / probes /
         index-build phase / analyze) with explicit defaults.
      3. Open the disposable lab connection, bootstrap with the
         requested ``index_build_phase``, import_rows, then
         (Mode C / ``--run-analyze``) defer ``create_vector_indexes``
         and ``analyze_tables`` to AFTER import.
      4. Build the canonical ``EvaluatorSessionPool`` whose
         every leased connection runs through
         :func:`apply_search_protocol` (session-scope
         ``set_config`` + ``current_setting`` round-trip).
      5. Build ``q_emb_by_case`` and run :func:`runner.run_case`
         sequentially through the pool-backed store.
      6. Run ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` once
         against a representative query and stamp the
         sanitised classification in the manifest.
      7. Map candidate IDs through the provenance map.
      8. Compute metrics + coverage; write the artifacts.
      9. ``--repeatability`` runs a second cached pass and
         writes ``repeatability.json``.

    The private ``_records_out`` keyword lets the sample-isolated
    orchestrator re-use this function without reconstructing
    records from JSONL: when a list is supplied, the in-memory
    ``CaseRecord`` objects that are also serialised into
    ``full-objective-results.jsonl`` are appended to it just
    before the function returns. ``main()`` never passes this
    kwarg — the regular CLI path keeps today's return shape.
    """

    # ----- G6C-B0 policy resolution -----
    resolved_policy = _resolve_cli_policy_kwargs(
        search_mode=getattr(args, "search_mode", None),
        ivfflat_probes=getattr(args, "ivfflat_probes", None),
        index_build_phase=getattr(args, "index_build_phase", None),
        run_analyze=getattr(args, "run_analyze", None),
    )
    search_mode = resolved_policy["search_mode"]
    ivfflat_probes = resolved_policy["ivfflat_probes"]
    index_build_phase = resolved_policy["index_build_phase"]
    run_analyze = resolved_policy["run_analyze"]

    sp_policy = SearchProtocol.parse(mode=search_mode, probes=ivfflat_probes)
    if index_build_phase not in _lab.ALLOWED_INDEX_BUILD_PHASES:
        raise CLIError(
            f"cli: invalid --index-build-phase {index_build_phase!r}; "
            f"expected one of {sorted(_lab.ALLOWED_INDEX_BUILD_PHASES)!r}"
        )

    # Fail closed: ``search_mode='exact'`` + an explicit
    # ``--ivfflat-probes`` is a contradictory contract — exact
    # mode MUST NOT pin planner probe decisions. The seam
    # closes here (BEFORE any DB bootstrap / import_rows /
    # corpus-vector prep) so the lab never sees a half-applied
    # ``ivfflat.probes`` against an exact-mode run. Without
    # this guard the contradiction would only surface at
    # ``apply_search_protocol`` lease-verify time, AFTER the
    # lab has already been touched — corrupting the disposable
    # tree before the run aborts.
    if (
        str(sp_policy.mode).strip().lower() == SEARCH_MODE_EXACT
        and ivfflat_probes is not None
    ):
        raise CLIError(
            "cli: --ivfflat-probes is not valid with "
            "--search-mode 'exact' (exact mode must NOT pin "
            "planner probe decisions); pass --ivfflat-probes "
            "with --search-mode 'ann' or omit --ivfflat-probes"
        )

    # GUC-registration preflight — runs against the disposable
    # DSN BEFORE bootstrap_schema / import_rows / corpus
    # vectors so an explicit --ivfflat-probes against an
    # unregistered GUC fails BEFORE the lab is touched.  For
    # implicit probes an unregistered GUC is recorded as
    # UNAVAILABLE rather than refused, so exact-mode runs and
    # no-override ANN runs are not broken by a pgvector build
    # that omits the GUC.
    probe_preflight = _preflight_probe_availability(
        str(args.dsn), sp_policy
    )

    sha = _dataset.verify_source(args.dataset, args.expected_sha256)
    embed_section = load_source_config_embed_section(args.source_config)
    source_config_sha = _file_sha256(args.source_config)

    sample_filter_str = str(args.sample_filter or "")
    if getattr(args, "sample_ids", None):
        extra = ",".join(
            str(s) for s in args.sample_ids if str(s).strip()
        )
        if extra:
            sample_filter_str = (
                f"{sample_filter_str},{extra}"
                if sample_filter_str.strip() else extra
            )
    sample_filter = (
        tuple(
            s.strip()
            for s in sample_filter_str.split(",")
            if s.strip()
        )
        if sample_filter_str.strip()
        else None
    )

    # Normalise ``--case-id`` (repeatable) into a tuple. The CLI
    # accepts both a comma-separated string and repeated ``--case-id``
    # flags; the normaliser turns either shape into the canonical
    # ``tuple[str, ...]``.
    raw_case_ids = getattr(args, "case_ids", None)
    if isinstance(raw_case_ids, str):
        case_ids = tuple(
            c.strip() for c in raw_case_ids.split(",") if c.strip()
        ) or None
    elif raw_case_ids is None:
        case_ids = None
    else:
        case_ids = tuple(str(c).strip() for c in raw_case_ids if str(c).strip()) or None

    data, eval_rows, allowed_samples = _resolve_dataset_and_rows(
        dataset_path=str(args.dataset),
        expected_sha256=str(sha),
        case_limit=args.case_limit,
        sample_filter=sample_filter,
        case_ids=case_ids,
    )

    # Isolated lab config + a real empty disposable experiment
    # directory. The dict is the ONLY config the facade/engine ever
    # sees: ``storage.pg`` points at the disposable loopback lab and
    # ``storage.embed`` carries the borrowed ephemeral provider
    # credential (process memory only).
    base_path = _make_disposable_base_path(
        str(args.cache_dir),
        str(sha),
        scope=getattr(args, "sample_isolated_base_scope", None),
    )
    lab_cfg = build_isolated_lab_config(
        base_path=base_path,
        dsn=str(args.dsn),
        output_dir=str(args.output_dir),
        cache_dir=str(args.cache_dir),
        embed_cfg=embed_section,
        rerank=False,
        rerank_endpoint_env="",
    )
    embed_cfg_input: dict[str, Any] = {"embed": dict(embed_section)}
    dataset_sha = str(getattr(data, "source_sha256", "") or sha)

    rows_dict = _dataset.build_import_rows(
        data,
        sample_ids=allowed_samples,
    )

    # Corpus vectors FIRST, so the importer writes real
    # ``vector(1024)`` values and the QA/message vector lanes are not
    # inert (a NULL embedding corpus makes the semantic run
    # byte-identical to the zero-vector canary).
    qa_stats = _prepare_corpus_vectors(
        rows=rows_dict["qa_pairs"],
        kind="qa_pairs",
        embed_cfg_input=embed_cfg_input,
        cache_dir=str(args.cache_dir),
        dataset_sha=dataset_sha,
        batch_size=int(args.batch_size),
    )
    cs_stats = _prepare_corpus_vectors(
        rows=rows_dict["conversation_stream"],
        kind="conversation_stream",
        embed_cfg_input=embed_cfg_input,
        cache_dir=str(args.cache_dir),
        dataset_sha=dataset_sha,
        batch_size=int(args.batch_size),
    )

    # Bootstrap + import. The bootstrap honours the requested
    # ``index_build_phase``: ``bootstrap_before_import`` builds
    # the IVFFlat indexes inside the bootstrap transaction;
    # ``after_import`` strips them and the helper below
    # re-creates them after ``import_rows`` returns.
    conn = _connect_lab(str(args.dsn))
    vector_indexes_applied: dict[str, str] = {}
    analyze_records: dict[str, str] = {}
    try:
        _lab.bootstrap_schema(
            conn,
            _resolve_repo_root(),
            index_build_phase=index_build_phase,
        )
        _lab.import_rows(conn, rows_dict)
        qa_id_index, conv_prov_rows = _read_back_provenance(conn)
        if index_build_phase == _lab.INDEX_BUILD_PHASE_AFTER_IMPORT:
            # Defer IVFFlat rebuild until after import (Mode C).
            vector_indexes_applied = _lab.create_vector_indexes(conn)
        if run_analyze:
            analyze_records = _lab.analyze_tables(conn)
            if index_build_phase == _lab.INDEX_BUILD_PHASE_AFTER_IMPORT:
                # Mode C: ANALYZE again after the deferred rebuild
                # so the planner sees fresh stats for the new
                # index lists.
                analyze_records = _lab.analyze_tables(conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    provenance_map = _lab.build_provenance_map(
        qa_pairs_rows=rows_dict.get("qa_pairs", ()) or (),
        conversation_stream_rows=conv_prov_rows,
        qa_id_index=qa_id_index,
    )

    query_rows, query_results, query_stats = _run_query_embed_pass(
        eval_rows=eval_rows,
        embed_section=embed_section,
        cache_dir=str(args.cache_dir),
        dataset_sha=dataset_sha,
        batch_size=int(args.batch_size),
    )

    q_emb_by_case = _build_q_emb_by_case(
        query_rows=query_rows,
        query_results=query_results,
    )

    # ----- G6C-B0 evaluator-only session pool -----
    # Wrap the disposable DSN's connection factory in an
    # :class:`EvaluatorSessionPool` so every lease re-applies the
    # search protocol. ``PgEmbedStore(pool=...)`` then forwards
    # every lease through the pool — ``TopicRecall`` constructed
    # in production paths by ``PgEmbedStore`` via its pool reads
    # vectors through the SAME pool, never a production-direct
    # fallback.
    pool_factory = _make_pool_factory(str(args.dsn))
    eval_pool = EvaluatorSessionPool(
        factory=pool_factory,
        policy=sp_policy,
        max_connections=2,
        min_connections=0,
    )

    pg_store = _make_lab_store(lab_cfg, pool=eval_pool)

    # Verify-once: lease the pool to confirm the protocol
    # round-trips BEFORE we hand it to the recall engine. The
    # verified ``enable_indexscan`` + ``probes`` are stamped in
    # the manifest so the auditor can confirm the requested and
    # verified values agree.
    verified_report_dict: dict[str, Any] = {}
    try:
        with_lease = eval_pool.lease()
        try:
            verified_report = apply_search_protocol(
                with_lease.connection, sp_policy
            )
            verified_report_dict = verified_report.observed_value
        finally:
            try:
                with_lease.close()
            except Exception:
                pass
    except EvaluatorPoolProtocolError as exc:
        eval_pool.close()
        raise CLIError(
            f"semantic: session-pool protocol verification failed: {exc}"
        ) from exc

    cases = _build_runner_cases(data=data, eval_rows=eval_rows)

    # The facade / repeatability window runs inside the
    # topic-recall data-dir guard so any ``TopicRecall``
    # constructed lazily by the recall engine reads /
    # writes only inside the disposable ``base_path`` —
    # never the production ``~/.v3-core/profiles/default``
    # tree. The guard restores the production resolver in
    # ``finally`` so a crash never leaves the patch dangling.
    explain_reports: dict[str, dict[str, Any]] = {}
    planner_evidence_failed: list[str] = []
    with _topic_recall_lab_data_dir_guard(base_path):
        records = _run_facade_pass(
            cases=cases,
            q_emb_by_case=q_emb_by_case,
            config=lab_cfg,
            pg=pg_store,
            expected_dim=DEFAULT_EMBEDDING_DIM,
        )
        records, id_mapping_audit = _map_record_rankings(records, provenance_map)

        # Fail closed: an objective run whose cases did not
        # pass the facade seam (missing/invalid query vector,
        # trace_missing, engine error, …) must NOT be written
        # out as if it had measured retrieval quality. The
        # statuses are reported instead of being averaged
        # away.
        bad_records = [
            rec for rec in records
            if str(getattr(rec, "status", "") or "") != "ok"
        ]
        if bad_records:
            first = bad_records[0]
            raise CLIError(
                f"semantic run aborted: {len(bad_records)}/{len(records)} case(s) "
                "did not pass the facade seam; first case_id="
                f"{getattr(first, 'case_id', '?')!r} status="
                f"{getattr(first, 'status', '')!r} error="
                f"{str(getattr(first, 'error', '') or '')[:200]!r}"
            )

        # ----- Planner evidence — fail-closed -----
        # We MUST capture BOTH ``qa_pairs`` and
        # ``conversation_stream`` vector plans with the
        # ``WHERE embedding IS NOT NULL`` guard so an empty
        # lab (a real failure mode of a fresh schema) cannot
        # silently masquerade as a healthy index-scan. Each
        # capture is tree-walk classified by ``capture_explain``.
        # If EITHER required plan cannot be captured, the
        # entire run fails closed — a "best-effort swallow"
        # here would let the manifest record "planner was
        # consulted" while the planner evidence was actually
        # absent.
        first_q = next(iter(q_emb_by_case.values()), None)
        if eval_rows and first_q is not None:
            explain_targets: tuple[tuple[str, str], ...] = (
                (
                    "semantic/qa_pairs/ann_top5",
                    "SELECT id FROM public.qa_pairs "
                    "WHERE embedding IS NOT NULL "
                    "ORDER BY embedding <=> %s::vector LIMIT 5",
                ),
                (
                    "semantic/conversation_stream/ann_top5",
                    "SELECT id FROM public.conversation_stream "
                    "WHERE embedding IS NOT NULL "
                    "ORDER BY embedding <=> %s::vector LIMIT 5",
                ),
            )
            for label, explain_sql in explain_targets:
                try:
                    lease = eval_pool.lease()
                except (
                    EvaluatorPoolClosed, EvaluatorPoolProtocolError
                ) as exc:
                    planner_evidence_failed.append(
                        f"{label}: lease failed ({exc})"
                    )
                    continue
                try:
                    report = capture_explain(
                        lease.connection,
                        sql=explain_sql,
                        query_label=label,
                        query_vector=first_q,
                    )
                    explain_reports[label] = report.to_dict()
                except ExplainCaptureError as exc:
                    planner_evidence_failed.append(
                        f"{label}: capture_explain failed ({exc})"
                    )
                finally:
                    try:
                        lease.close()
                    except Exception:
                        pass

            if planner_evidence_failed:
                # Fail-closed: a missing required plan record
                # is a contract violation. We do NOT best-effort
                # swallow and we do NOT write a partial manifest.
                raise CLIError(
                    "semantic: planner evidence is incomplete — "
                    f"{len(planner_evidence_failed)} of "
                    f"{len(explain_targets)} required plan captures failed: "
                    f"{planner_evidence_failed!r}"
                )

    # Below this point we are OUTSIDE the data-dir guard so
    # any subsequent file writes cannot accidentally touch
    # the production resolver snapshot. The guard was active
    # for the full facade / repeatability window, then
    # restored.

    snap = _metrics.compute_metrics(
        records,
        hit_at_k_limit=5,
        ranking_limit=5,
        engine_invocations=len(records),
    )

    _ensure_dir(str(args.output_dir))
    baseline_jsonl = os.path.join(args.output_dir, "full-objective-results.jsonl")
    canary_jsonl = os.path.join(args.output_dir, "semantic-canary-results.jsonl")
    payload_records = [rec.to_dict() for rec in records]
    _write_jsonl_atomic(baseline_jsonl, payload_records)
    _write_jsonl_atomic(canary_jsonl, payload_records)

    metrics_path = os.path.join(args.output_dir, "metrics.json")
    _write_json_atomic(metrics_path, snap.to_dict())

    coverage_path = os.path.join(args.output_dir, "coverage-audit.json")
    _write_json_atomic(coverage_path, {
        "coverage": snap.coverage.to_dict(),
        "snapshot_status": str(snap.status),
        "questions_total": int(snap.questions_total),
        "engine_invocations": int(snap.coverage.engine_invocations),
        "ranking_limit": int(snap.ranking_limit),
        "mapped_gold_count": int(snap.mapped_gold_count),
        "unmapped_gold_count": int(snap.unmapped_gold_count),
        "unresolved_gold_count": int(snap.unresolved_gold_count),
    })

    latency_header, latency_rows = _latency_rows(records)
    latency_csv = os.path.join(args.output_dir, "latency.csv")
    _write_csv_atomic(latency_csv, latency_header, latency_rows)

    cache_manifest_path = os.path.join(
        args.output_dir, "embedding-cache-manifest.json"
    )
    _write_json_atomic(cache_manifest_path, {
        "cache_root": str(args.cache_dir),
        "dataset_sha256": dataset_sha,
        "batch_size": int(args.batch_size),
        "query": query_stats.to_dict(),
        "corpus": {
            "qa_pairs": qa_stats.to_dict(),
            "conversation_stream": cs_stats.to_dict(),
        },
    })

    fingerprint_payload = _resolve_fingerprint(embed_section)
    fingerprint_path = os.path.join(args.output_dir, "fingerprint.json")
    _write_json_atomic(fingerprint_path, fingerprint_payload)

    # ----- G6C-B0 manifest augmentation -----
    # Stamp the requested AND verified search protocol,
    # index-build phase, analyze + EXPLAIN records. The
    # ``requested_*`` fields are what the operator asked for;
    # the ``verified_*`` fields are what the planner actually
    # saw after the session-pool round-trip. A mismatch is a
    # contract violation.
    #
    # ``verified_ivfflat_probes`` is the manifest-truth
    # field — it carries the OBSERVED current
    # ``ivfflat.probes`` GUC value when no explicit override
    # was set, and the requested int when an explicit override
    # was set + verified. The field is never silently
    # rewritten to ``None`` for an absent override.
    observed_probes_str = (
        verified_report_dict.get("ivfflat_probes")
        or verified_report_dict.get("observed_probes")
    )
    verified_probes_int = verified_report_dict.get("verified_probes")
    if verified_probes_int is not None:
        # Explicit override — verified int wins (it must
        # equal the requested value).
        verified_ivfflat_probes_out = int(verified_probes_int)
    elif observed_probes_str is not None and str(observed_probes_str).strip():
        # No override — observed GUC string is recorded
        # verbatim (manifest truth).
        verified_ivfflat_probes_out = str(observed_probes_str).strip()
    else:
        verified_ivfflat_probes_out = None

    # Exact-mode proof — auditor can confirm the planner
    # truly refused to use the index in exact mode.
    exact_mode_proof: dict[str, Any] = {
        "requested_mode": str(sp_policy.mode),
        "verified_enable_indexscan": str(
            verified_report_dict.get("enable_indexscan", "") or ""
        ) or None,
        "verified_ivfflat_probes": verified_ivfflat_probes_out,
    }
    if str(sp_policy.mode) == SEARCH_MODE_EXACT:
        exact_mode_proof["contract"] = (
            "exact mode requires enable_indexscan='off' AND probes must be "
            "None; failure on either invariant fails closed in "
            "apply_search_protocol."
        )

    g6c_b0_manifest = {
        "requested_search_mode": str(sp_policy.mode),
        "requested_ivfflat_probes": (
            int(sp_policy.probes) if sp_policy.probes is not None else None
        ),
        "verified_search_mode": str(verified_report_dict.get("mode", "")) or None,
        "verified_enable_indexscan": str(
            verified_report_dict.get("enable_indexscan", "")
        ) or None,
        "verified_ivfflat_probes": verified_ivfflat_probes_out,
        # Disposable-DSN preflight verdict — surfaces the
        # GUC-registration truth separately from the
        # round-trip value so an UNAVAILABLE GUC is never
        # silently reported as a registered probe count.
        "probe_availability": probe_preflight.get("probe_availability"),
        "probe_availability_source": probe_preflight.get(
            "probe_availability_source"
        ),
        "probe_availability_preflight": probe_preflight.get(
            "probe_availability_preflight"
        ),
        # Load-ordering evidence — the pgvector shared
        # library registers ``ivfflat.probes`` LAZILY, so the
        # BEFORE / AFTER pg_settings reads and the chosen
        # load method are part of the manifest.
        "probe_availability_before_load": probe_preflight.get(
            "probe_availability_before_load"
        ),
        "probe_availability_after_load": probe_preflight.get(
            "probe_availability_after_load"
        ),
        "pgvector_load_method": probe_preflight.get(
            "pgvector_load_method"
        ),
        "index_build_phase": str(index_build_phase),
        "vector_indexes_applied_after_import": vector_indexes_applied or None,
        "analyze_applied": analyze_records or None,
        # Two-table planner evidence — ``qa_pairs`` AND
        # ``conversation_stream``.  Either missing is a
        # contract violation (fail-closed).
        "planner_explain": explain_reports or None,
        "planner_explain_required": (
            "semantic/qa_pairs/ann_top5",
            "semantic/conversation_stream/ann_top5",
        ),
        "exact_mode_proof": exact_mode_proof,
        "session_pool_state": eval_pool.state().to_dict(),
    }
    g6c_b0_manifest_path = os.path.join(
        args.output_dir, "g6c-b0-protocol.json"
    )
    _write_json_atomic(g6c_b0_manifest_path, g6c_b0_manifest)

    summary: dict[str, Any] = {
        "ok": True,
        "stage": "semantic",
        "dataset_sha256": str(sha),
        "source_config_sha256": source_config_sha,
        "case_count": len(records),
        "status": str(snap.status),
        "hit_at_5": snap.hit_at_5,
        "mrr": snap.mrr,
        "ranking_limit": int(snap.ranking_limit),
        "rerank": "disabled",
        "lab_endpoint": (
            f"{lab_cfg['storage']['pg'].get('host')}:"
            f"{lab_cfg['storage']['pg'].get('port')}/"
            f"{lab_cfg['storage']['pg'].get('database')}"
        ),
        "search_mode": str(sp_policy.mode),
        "ivfflat_probes": (
            int(sp_policy.probes) if sp_policy.probes is not None else None
        ),
        "index_build_phase": str(index_build_phase),
        "run_analyze": bool(run_analyze),
        "case_ids": list(case_ids) if case_ids else None,
        "protocol_manifest": g6c_b0_manifest_path,
        "corpus": {
            "qa_pairs": {
                "rows": len(rows_dict["qa_pairs"]),
                "items": int(qa_stats.corpus_items),
                "chars": int(qa_stats.corpus_chars),
                "provider_batches": int(qa_stats.provider_batch_count),
                "cache_hits": int(qa_stats.hits),
                "cache_misses": int(qa_stats.misses),
            },
            "conversation_stream": {
                "rows": len(rows_dict["conversation_stream"]),
                "items": int(cs_stats.corpus_items),
                "chars": int(cs_stats.corpus_chars),
                "provider_batches": int(cs_stats.provider_batch_count),
                "cache_hits": int(cs_stats.hits),
                "cache_misses": int(cs_stats.misses),
            },
        },
        "query": {
            "items": int(query_stats.query_items),
            "chars": int(query_stats.query_chars),
            "provider_batches": int(query_stats.provider_batch_count),
            "cache_hits": int(query_stats.hits),
            "cache_misses": int(query_stats.misses),
        },
        "provenance": dict(provenance_map.get("counts") or {}),
        "id_mapping": dict(id_mapping_audit),
        "jsonl_baseline": baseline_jsonl,
        "jsonl_canary": canary_jsonl,
        "metrics": metrics_path,
        "coverage_audit": coverage_path,
        "latency_csv": latency_csv,
        "cache_manifest": cache_manifest_path,
        "fingerprint": fingerprint_path,
    }

    if bool(args.repeatability):
        # Second cached pass — no provider calls. The
        # embedding-preparation module hits the cache; the
        # facade is re-exercised once per case with the same
        # ``q_emb_by_case``. The facade pass MUST also run
        # inside the topic-recall data-dir guard: the recall
        # engine lazily constructs ``TopicRecall`` during this
        # pass too, and without the guard the constructor
        # would resolve ``_cache_dir`` against the production
        # ``~/.v3-core/profiles/default`` tree — silently
        # picking up the host machine's cached SQLite /
        # ``topic_matrix.npz`` even though the FIRST facade
        # pass was correctly isolated.
        with _topic_recall_lab_data_dir_guard(base_path):
            records_repeat = _run_facade_pass(
                cases=cases,
                q_emb_by_case=q_emb_by_case,
                config=lab_cfg,
                pg=pg_store,
                expected_dim=DEFAULT_EMBEDDING_DIM,
            )
        _map_ok_records, _ = _map_record_rankings(records_repeat, provenance_map)
        records_repeat = _map_ok_records
        snap_repeat = _metrics.compute_metrics(
            records_repeat,
            hit_at_k_limit=5,
            ranking_limit=5,
            engine_invocations=len(records_repeat),
        )
        repeat_jsonl = os.path.join(
            args.output_dir, "full-objective-results.repeat.jsonl"
        )
        repeat_canary_jsonl = os.path.join(
            args.output_dir, "semantic-canary-results.repeat.jsonl"
        )
        _write_jsonl_atomic(
            repeat_jsonl, [rec.to_dict() for rec in records_repeat]
        )
        _write_jsonl_atomic(
            repeat_canary_jsonl, [rec.to_dict() for rec in records_repeat]
        )
        header_r, rows_r = _latency_rows(records_repeat)
        latency_repeat_csv = os.path.join(args.output_dir, "latency.repeat.csv")
        _write_csv_atomic(latency_repeat_csv, header_r, rows_r)

        repeat_payload = _compare_repeatability(
            baseline_jsonl=baseline_jsonl,
            repeat_jsonl=repeat_jsonl,
            metrics_baseline=snap,
            metrics_repeat=snap_repeat,
            baseline_csv=latency_csv,
            repeat_csv=latency_repeat_csv,
        )
        repeatability_path = os.path.join(args.output_dir, "repeatability.json")
        _write_json_atomic(repeatability_path, repeat_payload)
        summary["repeatability"] = repeatability_path

    # The pool is owned by this function — close it on every
    # return path so the disposable lab connection is fully
    # released. Errors raised ABOVE this point must NOT leak
    # the pool alive.
    try:
        eval_pool.close()
    except Exception:
        pass

    # Append the in-memory record objects to the optional sink
    # (sample-isolated orchestrator only — ``main()`` never
    # passes the kwarg). The records here are the SAME objects
    # already serialised into ``full-objective-results.jsonl`` /
    # ``semantic-canary-results.jsonl`` above, so no JSONL
    # round-trip is needed for aggregation.
    if _records_out is not None:
        _records_out.extend(records)

    return summary


def _make_pool_factory(dsn: str) -> Any:
    """Build the zero-argument connection factory used by
    :class:`EvaluatorSessionPool`.

    The factory must NOT take arguments so the wrapper can
    call it once per physical connection. Tests can substitute
    a fake factory by monkey-patching this helper.
    """

    def _factory() -> Any:
        try:
            import psycopg2  # type: ignore[import-not-found]
        except ImportError as exc:
            raise EvaluatorPoolClosed(
                "evaluator session pool: psycopg2 is required to open the "
                "disposable lab connection"
            ) from exc
        return psycopg2.connect(dsn)

    return _factory


@contextlib.contextmanager
def _topic_recall_lab_data_dir_guard(lab_base_path: str):
    """Evaluator-only context guard — patches the data-dir
    resolver used by :mod:`v3core.topic_recall` (and the
    shared :mod:`v3core.config._resolve_data_dir`) so the
    disposable lab ``basePath`` is the ONLY place the topic
    cache / SQLite fallback looks during the facade /
    repeatability window.

    Why this lives here (evaluator-only):

      * ``TopicRecall`` is constructed lazily inside the
        recall engine from the disposable lab config; its
        ``_cache_dir`` is computed at ``__init__`` time from
        ``_resolve_data_dir``.  Without this guard, the
        constructor would point at ``~/.v3-core/profiles/default``
        (the production profile) and silently fall back to
        ``v3_topic_full.db`` / ``topic_matrix.npz`` from the
        host machine — corrupting the determinism contract
        even though every other lab DSN / vector path is
        correct.
      * The patch is in-process only and is restored in
        ``finally``.  It does NOT mutate the product module
        on disk, so the next evaluator run starts from a
        clean slate even if a previous run crashed.
      * The ``lab_base_path`` is the same
        ``_make_disposable_base_path`` directory the lab
        config was built against, so any topic-cache file
        written here lives under the same disposable tree
        the lab config validator already verified is empty.

    Parameters
    ----------
    lab_base_path:
        Absolute path to the disposable experiment directory
        (``lab_cfg['basePath']``).  The resolver is replaced
        with a function returning ``Path(lab_base_path)``.
    """

    if not lab_base_path or not isinstance(lab_base_path, str):
        raise CLIError(
            "topic-recall data-dir guard: lab_base_path must be a "
            "non-empty string (got "
            f"{type(lab_base_path).__name__})"
        )

    from pathlib import Path as _Path

    # Import lazily so the package stays importable without
    # the production v3core tree on the path.  The CLI is the
    # only caller that wires the pool-backed store, so the
    # import cost lives here.
    try:
        import v3core.topic_recall as _topic_recall
        import v3core.config as _v3config
    except Exception as exc:
        raise CLIError(
            "topic-recall data-dir guard: failed to import "
            f"v3core modules ({type(exc).__name__}); refusing "
            "to continue without an isolated cache directory"
        ) from exc

    original_resolve = getattr(_v3config, "_resolve_data_dir", None)
    if not callable(original_resolve):
        raise CLIError(
            "topic-recall data-dir guard: v3core.config._resolve_data_dir "
            "missing or not callable; the production module shape has "
            "changed — refusing to patch blindly"
        )

    def _lab_resolver(config: Any = None) -> _Path:
        """Lab-only resolver — always returns the disposable path.

        The ``config`` argument is intentionally ignored: the
        lab is built around the disposable ``base_path`` that
        the caller passed in, NOT the production config object
        a caller may also pass.  This guarantees the topic
        cache / SQLite fallback cannot escape the lab tree
        even if a caller hands in a production-shaped
        ``V3Config``.
        """

        return _Path(lab_base_path)

    # Patch BOTH the module attribute and the bound symbol
    # inside ``v3core.topic_recall`` — ``topic_recall``
    # imports ``_resolve_data_dir`` as a name, so the symbol
    # rebind is required to redirect calls that go through
    # the local binding (e.g. ``_cache_dir`` assignment at
    # construction time, ``DB_PATH`` lambda).
    snapshot: dict[str, Any] = {
        "config_module": original_resolve,
        "topic_module": getattr(
            _topic_recall, "_resolve_data_dir", original_resolve
        ),
    }
    try:
        _v3config._resolve_data_dir = _lab_resolver
        _topic_recall._resolve_data_dir = _lab_resolver
        try:
            yield
        finally:
            # Restore in reverse order — must succeed even if
            # the caller's code raised.  ``snapshot`` keeps
            # the original callable for both module-level
            # attribute and ``topic_recall``-module symbol.
            _v3config._resolve_data_dir = snapshot["config_module"]
            _topic_recall._resolve_data_dir = snapshot["topic_module"]
    except Exception:
            # Belt-and-braces: re-raise after restore so the
            # caller sees the original exception and the patch is
            # never left dangling on a half-completed run.
            _v3config._resolve_data_dir = snapshot["config_module"]
            _topic_recall._resolve_data_dir = snapshot["topic_module"]
            raise


# ---------------------------------------------------------------------
# G6C-B0.1 sample-isolated orchestrator
# ---------------------------------------------------------------------


# Reserved-db collision guard (mirrors ``lab._RESERVED_DBS``).  We
# import the constant instead of duplicating it so a future edit to
# the production-side guard is picked up here automatically.
def _sample_isolated_reserved_dbs() -> set[str]:
    return set(getattr(_lab, "_RESERVED_DBS", set()))


def _sample_isolated_slug(sample_id: str) -> str:
    """Deterministic per-sample slug.

    The same slug is used for the per-sample database name AND the
    per-sample ``basePath`` scope so a debugger can correlate them.
    ``[^a-z0-9]+`` collapses any non-alphanumeric run (including
    ``-``, ``.``) into a single ``_``; leading / trailing underscores
    are stripped so the final name is clean.
    """

    base = re.sub(r"[^a-z0-9]+", "_", str(sample_id).strip().lower()).strip("_")
    if not base:
        raise CLIError(
            f"sample id must produce a non-empty slug after "
            f"sanitisation (got {sample_id!r})"
        )
    return base


def _sample_isolated_db_name(
    base_dbname: str, sample_id: str,
) -> str:
    """Derive a per-sample database name from the base DSN dbname.

    Refused when:
      * it collides with ``lab._RESERVED_DBS``;
      * it equals the base ``dbname`` (a separate DB must carry a
        distinct name, otherwise the orchestrator would touch the
        base database).

    Returns ``"<base_dbname>_iso_<slug>"``.
    """

    base = str(base_dbname).strip()
    slug = _sample_isolated_slug(sample_id)
    candidate = f"{base}_iso_{slug}"
    if candidate == base:
        raise CLIError(
            f"sample-isolated db name {candidate!r} equals the base "
            f"dbname; refusing to alias"
        )
    reserved = _sample_isolated_reserved_dbs()
    if candidate.lower() in {n.lower() for n in reserved}:
        raise CLIError(
            f"sample-isolated db name {candidate!r} collides with a "
            f"reserved production/eval database name; refusing"
        )
    return candidate


def _replace_dbname_in_dsn(dsn: str, new_dbname: str) -> str:
    """Return ``dsn`` with the ``dbname=`` key replaced by ``new_dbname``.

    Preserves every other key (host, port, user, password) and their
    quoting verbatim so the per-sample DSN is byte-identical to the
    base DSN except for the database name.  Raises :class:`CLIError`
    when the base DSN carries no ``dbname=`` key (the upstream
    lab guard already requires it, but we double-check here so an
    unsafe orchestrator cannot silently fall back).
    """

    if not re.search(r"(?i)\bdbname\s*=", dsn):
        raise CLIError(
            "sample-isolated orchestrator: base DSN has no dbname= "
            "key; refusing to derive a per-sample DSN"
        )
    return re.sub(
        r"(?i)\bdbname\s*=\s*('(?:[^']|'')*'|\S+)",
        f"dbname={new_dbname}",
        dsn,
        count=1,
    )


def _drop_database(
    *, server_dsn: str, dbname: str, log: list[dict[str, Any]],
) -> None:
    """Drop ``dbname`` on the disposable server, never touching the
    base database.

    Opens a maintenance connection (``dbname=postgres`` /
    ``dbname=template1`` / … — anything that parses) with
    autocommit so ``CREATE DATABASE`` / ``DROP DATABASE`` run
    outside a transaction. The ``dbname`` is dropped with
    ``DROP DATABASE IF EXISTS`` so a previous failure that left
    the database lying around does not poison this run.

    The caller-supplied ``log`` list is appended to so a failure
    here is surfaced to the aggregate summary rather than
    swallowed silently.
    """

    if not dbname:
        raise CLIError(
            "_drop_database: empty dbname refused"
        )
    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError as exc:
        log.append({
            "database": str(dbname),
            "dropped": False,
            "error": f"psycopg2 unavailable: {exc}",
        })
        raise
    # Maintenance DSN — same server, but ``dbname=postgres`` (or
    # whatever the user's original base DSN already carries; we
    # fall back to ``postgres`` only as a last resort and refuse
    # the call if BOTH the base dbname and ``postgres`` collide
    # with a reserved name).
    maintenance_dsn = _replace_dbname_in_dsn(server_dsn, "postgres")
    conn = None
    try:
        conn = psycopg2.connect(maintenance_dsn)
        conn.autocommit = True
        # Identifier-quote via SQL-standard ``"`` so the literal
        # never reaches the wire as raw SQL.  The DSN guard has
        # already restricted us to a known-clean loopback server
        # so this is sufficient.
        safe = dbname.replace('"', '""')
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{safe}"')
        log.append({"database": str(dbname), "dropped": True, "error": None})
    except Exception as exc:
        log.append({
            "database": str(dbname),
            "dropped": False,
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _create_database(
    *, server_dsn: str, dbname: str,
) -> None:
    """Create ``dbname`` freshly on the disposable server.

    Uses the same maintenance connection pattern as
    :func:`_drop_database`.  The database is created only when it
    does NOT already exist, so a leftover from a previous crashed
    run does not silently overwrite real evidence; if it DOES
    exist (a different process is racing us, or a prior run left
    it lying around), we DROP-then-CREATE so every per-sample pass
    starts from a guaranteed-empty schema.  Any failure raises
    :class:`CLIError` so the orchestrator's ``finally`` path
    tears down the database the caller asked for.
    """

    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CLIError(
            f"_create_database: psycopg2 unavailable: {exc}"
        ) from exc
    maintenance_dsn = _replace_dbname_in_dsn(server_dsn, "postgres")
    conn = None
    try:
        conn = psycopg2.connect(maintenance_dsn)
        conn.autocommit = True
        safe = dbname.replace('"', '""')
        with conn.cursor() as cur:
            # Best-effort drop first so a leftover does not corrupt
            # the new pass.  ``DROP DATABASE IF EXISTS`` is silent
            # when the database is missing, so this is also a no-op
            # on the common path.
            cur.execute(f'DROP DATABASE IF EXISTS "{safe}"')
            cur.execute(f'CREATE DATABASE "{safe}"')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _copy_args(args: argparse.Namespace) -> argparse.Namespace:
    """Return a shallow copy of ``args`` safe for in-place mutation.

    Each top-level attribute that this orchestrator rewrites gets
    a fresh value; everything else is copied verbatim so the
    downstream ``run_semantic`` sees the same dataset / source
    config / search mode / probes / etc. that the caller chose.
    """

    return argparse.Namespace(**vars(args))


def _resolve_sample_iter(
    *, dataset_path: str, expected_sha256: str,
    sample_filter: tuple[str, ...] | None,
) -> tuple[Any, list[str]]:
    """Return ``(data, ordered_sample_ids)``.

    Order is the loader's stable iteration order (sorted by
    ``sample_id``).  ``sample_filter`` is honoured exactly the way
    ``_resolve_dataset_and_rows`` honours it — a sample id that
    is not in the dataset is silently dropped so a caller that
    passes ``--sample-filter s1,s2`` never sees ``s1`` repeated
    in the orchestrator output.
    """

    data = _dataset.load_locomo(dataset_path, expected_sha256)
    all_sids = [str(sid) for sid, _ in data.samples]
    if sample_filter:
        allowed = set(sample_filter)
        ordered = [sid for sid in all_sids if sid in allowed]
        if not ordered:
            raise CLIError(
                f"sample-isolated orchestrator: --sample-filter "
                f"{list(sample_filter)!r} resolved to zero samples; "
                f"refusing to run"
            )
    else:
        ordered = all_sids
    return data, ordered


def _recompute_metrics(records: Sequence[Any]) -> dict[str, Any]:
    """Recompute metrics over ``records`` with the production limits.

    This is the orchestrator's independent recomputation: every
    per-sample metric in the aggregate artifact is also re-derived
    here so a test can confirm ``compute_metrics(records)`` agrees
    with the per-sample ``run_semantic`` snapshot.
    """

    snap = _metrics.compute_metrics(
        list(records),
        hit_at_k_limit=5,
        ranking_limit=5,
        engine_invocations=len(records),
    )
    return snap.to_dict()


def _per_sample_metric_summary(
    records: Sequence[Any],
) -> dict[str, Any]:
    """Per-sample metric block for the ``per-sample-metrics.json``
    aggregate artifact.
    """

    snap_dict = _recompute_metrics(records)
    candidate_total = 0
    for rec in records:
        try:
            candidate_total += len(
                list(getattr(rec, "candidate_source_ids", ()) or ())
            )
        except Exception:
            continue
    return {
        "sample_id": str(getattr(records[0], "sample_id", "")
                        if records else ""),
        "question_count": int(snap_dict.get("questions_total", 0)),
        "hit_at_1": snap_dict.get("hit_at_1"),
        "hit_at_5": snap_dict.get("hit_at_5"),
        "mrr": snap_dict.get("mrr"),
        "mean_relevant_rank": snap_dict.get("mean_relevant_rank"),
        "status": str(snap_dict.get("status", "")),
        "mapped_gold_count": int(snap_dict.get("mapped_gold_count", 0)),
        "unmapped_gold_count": int(
            snap_dict.get("unmapped_gold_count", 0)
        ),
        "unresolved_gold_count": int(
            snap_dict.get("unresolved_gold_count", 0)
        ),
        "candidate_total": int(candidate_total),
    }


def _run_semantic_sample_isolated(args: argparse.Namespace) -> dict[str, Any]:
    """Sample-isolated orchestrator.

    For every sample in the selected scope (dataset order):

      1. derive a per-sample disposable database name;
      2. DROP-then-CREATE that database freshly on the same
         disposable server as ``args.dsn``;
      3. run **one full lab pass** whose corpus is that sample's
         rows only (``build_import_rows(data, sample_ids={sid})``)
         and whose eval cases are all of that sample's questions;
      4. DROP that sample's database in a ``finally`` block;
      5. collect the per-sample records.

    The aggregate artifact set is then assembled in canonical
    dataset order: ``full-objective-results.jsonl`` /
    ``semantic-canary-results.jsonl`` carry the concatenated
    ``records``; ``metrics.json`` / ``coverage-audit.json`` /
    ``latency.csv`` carry the aggregated metrics; the per-sample
    metrics land in ``per-sample-metrics.json``; the corpus-
    isolation evidence lands in ``corpus-isolation.json``;
    ``g6c-b0-protocol.json`` gains a ``corpus_isolation`` block.

    ``run_semantic`` is re-used end-to-end — this orchestrator
    does NOT re-implement bootstrap, import, recall, or metrics.
    """

    sample_filter = _coerce_sample_filter(getattr(args, "sample_filter", ""))
    ordered = _resolve_sample_iter(
        dataset_path=str(args.dataset),
        expected_sha256=str(args.expected_sha256),
        sample_filter=sample_filter,
    )
    data, sample_ids = ordered
    if not sample_ids:
        raise CLIError(
            "sample-isolated orchestrator: no samples to process; "
            "refusing to run"
        )

    # Validate the base DSN BEFORE we touch anything else so a bad
    # DSN never creates an empty per-sample database.
    base_parts = _lab.validate_disposable_dsn(str(args.dsn))
    base_dbname = str(base_parts.get("dbname") or "").strip()
    if not base_dbname:
        raise CLIError(
            "sample-isolated orchestrator: base DSN has no dbname; "
            "refusing to derive per-sample databases"
        )

    # Resolve the per-sample database names up-front so a refusal
    # happens BEFORE we touch the server.  This also surfaces
    # duplicate-slug collisions (which the loader cannot produce,
    # but a caller passing a hand-crafted dataset could).
    plan: list[dict[str, Any]] = []
    for sid in sample_ids:
        slug = _sample_isolated_slug(sid)
        dbname = _sample_isolated_db_name(base_dbname, sid)
        plan.append({
            "sample_id": str(sid),
            "slug": slug,
            "database": dbname,
        })

    # Resolve the per-sample case-id slice ONCE up-front so the
    # inner per-sample ``run_semantic`` calls can copy the slice
    # cheaply.  Empty / unknown ``--case-id`` values still go
    # through ``_resolve_dataset_and_rows`` per-sample so the
    # ``CLIError`` "unknown case_id" guard fires.
    raw_case_ids = getattr(args, "case_ids", None)
    if isinstance(raw_case_ids, str):
        global_case_ids = tuple(
            c.strip() for c in raw_case_ids.split(",") if c.strip()
        ) or None
    elif raw_case_ids is None:
        global_case_ids = None
    else:
        global_case_ids = tuple(
            str(c).strip() for c in raw_case_ids if str(c).strip()
        ) or None

    # Decide which per-sample directories we use.  ``scope=<slug>``
    # keeps each sample's ``basePath`` distinct (the test suite
    # asserts on this) while staying under the same ``cache_dir``.
    _ensure_dir(str(args.output_dir))

    aggregate_records: list[Any] = []
    per_sample_metrics: list[dict[str, Any]] = []
    per_sample_lab: dict[str, dict[str, Any]] = {}
    drop_log: list[dict[str, Any]] = []

    overall_status = "ok"
    overall_error: str | None = None

    for step in plan:
        sid = step["sample_id"]
        slug = step["slug"]
        dbname = step["database"]
        sample_dsn = _replace_dbname_in_dsn(str(args.dsn), dbname)
        sample_output_dir = os.path.join(
            str(args.output_dir), "per-sample", slug,
        )
        sample_base_path = _make_disposable_base_path(
            str(args.cache_dir),
            str(getattr(data, "source_sha256", "") or args.expected_sha256),
            scope=slug,
        )
        per_sample_case_ids: tuple[str, ...] | None
        if global_case_ids:
            per_sample_case_ids = tuple(
                cid for cid in global_case_ids
                if cid.startswith(f"{sid}|")
            )
            # An explicit case-id set that survives the per-sample
            # filter must NOT be empty (the inner resolver would
            # silently narrow the corpus to a zero-row slice); we
            # raise the same ``CLIError`` it would raise, here in
            # the orchestrator so the per-sample DB is never
            # created for an empty case list.
            if not per_sample_case_ids:
                raise CLIError(
                    f"sample-isolated orchestrator: --case-id slice "
                    f"{list(global_case_ids)!r} contains no ids for "
                    f"sample {sid!r}; refusing to run an empty pass"
                )
        else:
            per_sample_case_ids = None

        sub_args = _copy_args(args)
        sub_args.dsn = sample_dsn
        sub_args.output_dir = sample_output_dir
        sub_args.sample_ids = [sid]
        sub_args.sample_filter = ""
        sub_args.case_ids = per_sample_case_ids
        sub_args.repeatability = False
        sub_args.sample_isolated = False
        sub_args.case_limit = None
        # Per-sample inner ``run_semantic`` MUST derive its
        # disposable base path under a per-sample scope so the
        # ten per-sample passes do NOT share one directory under
        # the same ``cache_dir`` — sharing the same base_path
        # would route TopicRecall / card-store state through one
        # shared directory while the PG corpora are isolated,
        # which is a real cross-sample contamination channel.
        sub_args.sample_isolated_base_scope = slug

        # CREATE-then-run-then-DROP.  The drop lives in ``finally``
        # so a sample that fails still has its DB destroyed.  A
        # failed drop is recorded in ``drop_log`` and surfaced in
        # the aggregate summary (never swallowed), but it must
        # NOT mask the original error — we re-raise the original
        # when the drop itself raises.
        sample_records: list[Any] = []
        try:
            try:
                _create_database(
                    server_dsn=str(args.dsn), dbname=dbname,
                )
            except Exception:
                # If CREATE fails, the database may still exist as a
                # half-built leftover; try a best-effort drop so the
                # next per-sample pass is not poisoned.  The drop's
                # own failure (if any) is reported in ``drop_log``
                # but never raised here — the original CREATE error
                # is the one the caller sees.
                try:
                    _drop_database(
                        server_dsn=str(args.dsn),
                        dbname=dbname,
                        log=drop_log,
                    )
                except Exception:
                    pass
                raise

            sample_summary = run_semantic(
                sub_args, _records_out=sample_records,
            )
        finally:
            try:
                _drop_database(
                    server_dsn=str(args.dsn),
                    dbname=dbname,
                    log=drop_log,
                )
            except Exception:
                # The per-sample teardown failure is logged but
                # never replaces the original error.  ``drop_log``
                # is surfaced verbatim in the aggregate summary.
                pass

        aggregate_records.extend(sample_records)

        # Per-sample corpus rows are read from the per-sample
        # ``run_semantic`` summary so the corpus-isolation
        # evidence reports the imported row counts, not the eval-
        # case JSONL line count.  The JSONL line count is kept
        # under a correctly-named field so an auditor can still
        # see how many eval cases produced the JSONL.
        per_sample_jsonl = os.path.join(
            sample_output_dir, "full-objective-results.jsonl",
        )
        qa_pairs_rows = _resolve_per_sample_corpus_rows(
            sample_summary, "qa_pairs",
        )
        conversation_stream_rows = _resolve_per_sample_corpus_rows(
            sample_summary, "conversation_stream",
        )
        per_sample_lab[sid] = {
            "database": str(dbname),
            "slug": str(slug),
            "base_path": str(sample_base_path),
            "qa_pairs_rows": int(qa_pairs_rows),
            "conversation_stream_rows": int(conversation_stream_rows),
            "evidence_written_case_count": _count_corpus_rows(
                per_sample_jsonl
            ),
            "session_count": _count_per_sample_sessions(
                data, expected_sample_id=sid,
            ),
            "case_count": int(len(sample_records)),
            "output_dir": str(sample_output_dir),
            "summary": {
                k: v for k, v in sample_summary.items()
                if k in {"case_count", "status", "hit_at_5", "mrr"}
            },
            "drop_log": [
                e for e in drop_log if e.get("database") == dbname
            ],
        }

        per_sample_metrics.append(
            _per_sample_metric_summary(sample_records)
        )

    # ---------- Aggregate artifacts ----------

    _ensure_dir(str(args.output_dir))

    # Aggregate records stay in canonical dataset order.
    aggregate_records_sorted = list(aggregate_records)

    aggregate_jsonl = os.path.join(
        str(args.output_dir), "full-objective-results.jsonl",
    )
    aggregate_canary_jsonl = os.path.join(
        str(args.output_dir), "semantic-canary-results.jsonl",
    )
    _write_jsonl_atomic(
        aggregate_jsonl,
        [rec.to_dict() for rec in aggregate_records_sorted],
    )
    _write_jsonl_atomic(
        aggregate_canary_jsonl,
        [rec.to_dict() for rec in aggregate_records_sorted],
    )

    aggregate_snap = _metrics.compute_metrics(
        aggregate_records_sorted,
        hit_at_k_limit=5,
        ranking_limit=5,
        engine_invocations=len(aggregate_records_sorted),
    )
    aggregate_snap_dict = aggregate_snap.to_dict()
    metrics_path = os.path.join(str(args.output_dir), "metrics.json")
    _write_json_atomic(metrics_path, aggregate_snap_dict)

    coverage_path = os.path.join(
        str(args.output_dir), "coverage-audit.json",
    )
    _write_json_atomic(coverage_path, {
        "coverage": aggregate_snap_dict.get("coverage", {}),
        "snapshot_status": str(aggregate_snap_dict.get("status", "")),
        "questions_total": int(
            aggregate_snap_dict.get("questions_total", 0)
        ),
        "engine_invocations": int(
            aggregate_snap_dict.get("questions_total", 0)
        ),
        "ranking_limit": int(
            aggregate_snap_dict.get("ranking_limit", 5)
        ),
        "mapped_gold_count": int(
            aggregate_snap_dict.get("mapped_gold_count", 0)
        ),
        "unmapped_gold_count": int(
            aggregate_snap_dict.get("unmapped_gold_count", 0)
        ),
        "unresolved_gold_count": int(
            aggregate_snap_dict.get("unresolved_gold_count", 0)
        ),
    })

    latency_header, latency_rows = _latency_rows(aggregate_records_sorted)
    latency_csv = os.path.join(str(args.output_dir), "latency.csv")
    _write_csv_atomic(latency_csv, latency_header, latency_rows)

    # Embedding-cache manifest: the per-sample passes reused the
    # SAME cache directory, so the manifest is the union of the
    # per-sample manifests (the test asserts this stays stable
    # across the orchestrator).  We re-derive it from the aggregate
    # so the JSON shape is identical to the single-lab case.
    cache_manifest_path = os.path.join(
        str(args.output_dir), "embedding-cache-manifest.json",
    )
    _write_json_atomic(cache_manifest_path, {
        "cache_root": str(args.cache_dir),
        "dataset_sha256": str(
            getattr(data, "source_sha256", "") or args.expected_sha256
        ),
        "batch_size": int(args.batch_size),
        "scope": "sample_isolated",
        "per_sample": sorted(per_sample_lab.keys()),
    })

    # Fingerprint: the source-config is the same as in the
    # single-lab path, so we re-derive it once.
    embed_section = load_source_config_embed_section(
        str(args.source_config)
    )
    fingerprint_payload = _resolve_fingerprint(embed_section)
    fingerprint_path = os.path.join(
        str(args.output_dir), "fingerprint.json",
    )
    _write_json_atomic(fingerprint_path, fingerprint_payload)

    # Per-sample metrics artifact.
    per_sample_metrics_path = os.path.join(
        str(args.output_dir), "per-sample-metrics.json",
    )
    _write_json_atomic(per_sample_metrics_path, {
        "mode": "sample_isolated",
        "samples": [p["sample_id"] for p in per_sample_metrics],
        "per_sample": {
            p["sample_id"]: p for p in per_sample_metrics
        },
    })

    # Corpus-isolation evidence.
    corpus_isolation_path = os.path.join(
        str(args.output_dir), "corpus-isolation.json",
    )
    _write_json_atomic(corpus_isolation_path, {
        "mode": "sample_isolated",
        "samples": [p["sample_id"] for p in plan],
        "per_sample": {
            p["sample_id"]: {
                "database": p["database"],
                "slug": p["slug"],
                "base_path": str(per_sample_lab[p["sample_id"]]["base_path"]),
                "qa_pairs_rows": int(
                    per_sample_lab[p["sample_id"]]["qa_pairs_rows"]
                ),
                "conversation_stream_rows": int(
                    per_sample_lab[p["sample_id"]]["conversation_stream_rows"]
                ),
                "evidence_written_case_count": int(
                    per_sample_lab[p["sample_id"]][
                        "evidence_written_case_count"
                    ]
                ),
                "session_count": int(
                    per_sample_lab[p["sample_id"]]["session_count"]
                ),
                "case_count": int(
                    per_sample_lab[p["sample_id"]]["case_count"]
                ),
            } for p in plan
        },
    })

    # Aggregate ``g6c-b0-protocol.json``: the orchestrator does
    # NOT re-run the search protocol (the per-sample passes do);
    # this block stamps the corpus_isolation evidence so the run
    # auditor can confirm what each per-sample DB contained.
    g6c_b0_manifest_path = os.path.join(
        str(args.output_dir), "g6c-b0-protocol.json",
    )
    g6c_b0_manifest = {
        "corpus_isolation": {
            "mode": "sample_isolated",
            "samples": [p["sample_id"] for p in plan],
            "per_sample_lab": {
                sid: {
                    "database": per_sample_lab[sid]["database"],
                    "qa_pairs_rows": int(
                        per_sample_lab[sid]["qa_pairs_rows"]
                    ),
                    "conversation_stream_rows": int(
                        per_sample_lab[sid]["conversation_stream_rows"]
                    ),
                    "evidence_written_case_count": int(
                        per_sample_lab[sid]["evidence_written_case_count"]
                    ),
                    "case_count": int(per_sample_lab[sid]["case_count"]),
                } for sid in (p["sample_id"] for p in plan)
            },
        },
        "drop_log": drop_log,
    }
    _write_json_atomic(g6c_b0_manifest_path, g6c_b0_manifest)

    # Aggregate case_count MUST equal the sum of per-sample case
    # counts AND the line count of the aggregate JSONL — the
    # orchestrator enforces this as a structural invariant so the
    # test suite can rely on it without re-reading either side.
    per_sample_total = sum(
        int(per_sample_lab[p["sample_id"]]["case_count"]) for p in plan
    )
    if per_sample_total != len(aggregate_records_sorted):
        raise CLIError(
            "sample-isolated orchestrator: aggregate case_count "
            f"({len(aggregate_records_sorted)}) does not equal the "
            f"sum of per-sample case counts ({per_sample_total}); "
            "refusing to write inconsistent artifacts"
        )

    return {
        "ok": True,
        "stage": "semantic",
        "mode": "sample_isolated",
        "dataset_sha256": str(
            getattr(data, "source_sha256", "") or args.expected_sha256
        ),
        "source_config_sha256": _file_sha256(str(args.source_config)),
        "case_count": int(len(aggregate_records_sorted)),
        "per_sample_total_case_count": int(per_sample_total),
        "sample_count": int(len(plan)),
        "status": str(aggregate_snap_dict.get("status", "")),
        "hit_at_5": aggregate_snap_dict.get("hit_at_5"),
        "mrr": aggregate_snap_dict.get("mrr"),
        "ranking_limit": int(aggregate_snap_dict.get("ranking_limit", 5)),
        "samples": [p["sample_id"] for p in plan],
        "per_sample": per_sample_metrics,
        "per_sample_lab": per_sample_lab,
        "drop_log": drop_log,
        "metrics": metrics_path,
        "coverage_audit": coverage_path,
        "latency_csv": latency_csv,
        "cache_manifest": cache_manifest_path,
        "fingerprint": fingerprint_path,
        "jsonl_baseline": aggregate_jsonl,
        "jsonl_canary": aggregate_canary_jsonl,
        "per_sample_metrics": per_sample_metrics_path,
        "corpus_isolation": corpus_isolation_path,
        "protocol_manifest": g6c_b0_manifest_path,
        "overall_status": overall_status,
        "overall_error": overall_error,
    }


def _coerce_sample_filter(raw: Any) -> tuple[str, ...] | None:
    """Return ``raw`` as a tuple of trimmed sample ids, or ``None``.

    Accepts the same shapes the single-lab CLI accepts (None / str /
    iterable of str) so the sample-isolated orchestrator does not
    duplicate the normaliser.
    """

    if raw is None:
        return None
    if isinstance(raw, str):
        return tuple(
            s.strip() for s in raw.split(",") if s.strip()
        ) or None
    return tuple(str(s).strip() for s in raw if str(s).strip()) or None


def _resolve_per_sample_corpus_rows(
    sample_summary: Mapping[str, Any], kind: str,
) -> int:
    """Read the imported corpus row count for ``kind`` from ``sample_summary``.

    ``sample_summary`` is the dict returned by a per-sample
    :func:`run_semantic` call.  The corpus row count for one
    family lives at ``sample_summary["corpus"][kind]["rows"]``.
    The orchestrator reads this as the TRUE imported row count —
    NOT the eval-case JSONL line count, which is reported
    separately under ``evidence_written_case_count``.

    A missing nested key (e.g. an older ``run_semantic``
    implementation that did not surface ``corpus``) fails closed
    with :class:`CLIError` rather than silently writing ``0`` or
    a fallback; a fabricated zero would mislead the run auditor
    into believing the per-sample DB was empty when it was in
    fact unmeasured.
    """

    if not isinstance(sample_summary, Mapping):
        raise CLIError(
            "sample-isolated orchestrator: per-sample run_semantic "
            "summary is missing or not a mapping; refusing to "
            "fabricate a corpus row count for "
            f"{kind!r}"
        )
    corpus_block = sample_summary.get("corpus")
    if not isinstance(corpus_block, Mapping):
        raise CLIError(
            "sample-isolated orchestrator: per-sample run_semantic "
            f"summary is missing 'corpus' (for kind={kind!r}); "
            "refusing to fabricate a corpus row count"
        )
    kind_block = corpus_block.get(kind)
    if not isinstance(kind_block, Mapping):
        raise CLIError(
            "sample-isolated orchestrator: per-sample run_semantic "
            f"summary is missing 'corpus.{kind}'; refusing to "
            "fabricate a corpus row count"
        )
    rows = kind_block.get("rows")
    if not isinstance(rows, int) or isinstance(rows, bool):
        raise CLIError(
            "sample-isolated orchestrator: per-sample run_semantic "
            f"summary 'corpus.{kind}.rows' must be an int "
            f"(got {rows!r}); refusing to fabricate a corpus row "
            "count"
        )
    if rows < 0:
        raise CLIError(
            "sample-isolated orchestrator: per-sample run_semantic "
            f"summary 'corpus.{kind}.rows' must be non-negative "
            f"(got {rows!r})"
        )
    return rows


def _count_corpus_rows(jsonl_path: str) -> int:
    """Return the line count of ``jsonl_path`` if it exists, else 0.

    A 0-line result is the truth for a per-sample pass that crashed
    before ``run_semantic`` finished writing the JSONL; the
    aggregate ``corpus-isolation.json`` therefore records this
    value under the correctly-named ``evidence_written_case_count``
    field — it is NOT a corpus row count and is NOT exposed under
    ``qa_pairs_rows`` / ``conversation_stream_rows``.  Tests
    that want to assert the per-sample case count must use the
    ``case_count`` field, not this row count.
    """

    if not jsonl_path or not os.path.isfile(jsonl_path):
        return 0
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f if _.strip())
    except OSError:
        return 0


def _count_per_sample_sessions(
    data: Any, expected_sample_id: str,
) -> int:
    """Distinct session count for ``expected_sample_id`` in ``data``.

    The recall engine does not surface the per-case session set on
    ``CaseRecord`` (only ranked / selected / candidate / injected
    source ids), so we read the canonical LoCoMo dataset for this
    sample and count its ``sessions`` tuple.  ``0`` means the
    dataset did not contain the requested sample — the orchestrator
    surfaces that honestly rather than fabricating a number.
    """

    for sid, sample in data.samples:
        if str(sid) != str(expected_sample_id):
            continue
        return int(len(sample.sessions))
    return 0


# ---------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------


def make_argument_parser() -> argparse.ArgumentParser:
    """Build the argparse parser.

    Every flag maps 1:1 to a documented seam so the runbook
    can quote the exact flag list. No silent defaults that
    hide production surfaces — ``--dsn`` is required because
    a missing DSN would default to a non-loopback host.
    """

    p = argparse.ArgumentParser(
        prog="eval.locomo_recall_v2",
        description=(
            "G6C-A LoCoMo Recall V2 durable evaluator entry point. "
            "Default subcommand: run."
        ),
    )
    sub = p.add_subparsers(dest="command", required=False)

    run = sub.add_parser("run", help="Execute one evaluator cycle")

    run.add_argument("--dataset", required=True,
                     help="Path to the LoCoMo JSON file.")
    run.add_argument("--expected-sha256", required=True,
                     help="64-char hex SHA-256 of the dataset file.")
    run.add_argument("--dsn", required=True,
                     help="Disposable loopback DSN (validated by lab).")
    run.add_argument("--output-dir", required=True,
                     help="Directory for benchmark-manifest.json / metrics.json / JSONL.")
    run.add_argument("--cache-dir", required=True,
                     help="Directory for the ephemeral embed cache + experiment basePath.")
    run.add_argument("--source-config", required=True,
                     help="Path to a YAML source config. ONLY storage.embed is read.")
    run.add_argument("--batch-size", type=int, default=32,
                     help="Provider-call chunk size. Default: 32.")
    run.add_argument("--rerank", action="store_true",
                     help="Enable rerank. Default: disabled.")
    run.add_argument("--rerank-endpoint-env", default="",
                     help="Env-var NAME for the rerank endpoint. Required when --rerank.")
    run.add_argument("--mode", default="dry-run",
                     choices=("dry-run", "semantic"),
                     help="Which stage to run. Default: dry-run.")
    run.add_argument("--case-limit", dest="case_limit", type=int, default=None,
                     help="Optional cap on the number of eval cases.")
    run.add_argument(
        "--max-cases", dest="case_limit", type=int, default=None,
        help=argparse.SUPPRESS,
    )
    run.add_argument(
        "--sample-filter", dest="sample_filter", default="",
        help="Optional comma-separated list of sample_ids to include.",
    )
    run.add_argument(
        "--sample-id", dest="sample_ids", action="append",
        default=None,
        help="Repeatable sample-id filter; merged into --sample-filter.",
    )
    run.add_argument(
        "--case-id", dest="case_ids", action="append",
        default=None,
        help=(
            "Repeatable case-level filter; one or more "
            "<sample_id>|<query_idx> ids. Unknown ids fail closed. "
            "Corpus import stays at the selected sample scope."
        ),
    )
    run.add_argument(
        "--search-mode", dest="search_mode",
        choices=tuple(sorted(ALLOWED_SEARCH_MODES)),
        default=None,
        help=(
            "Exact or ANN session policy. Default when omitted: "
            "'ann' (the historical G6C-A / alpha behaviour). "
            "B0 deterministic-baseline experiments pass 'exact' "
            "explicitly so the manifest records the requested "
            "value."
        ),
    )
    run.add_argument(
        "--ivfflat-probes", dest="ivfflat_probes", type=int, default=None,
        help=(
            "Optional explicit ivfflat.probes override. Valid only "
            "with --search-mode ann. Verified on every lease."
        ),
    )
    run.add_argument(
        "--index-build-phase", dest="index_build_phase",
        choices=("bootstrap_before_import", "after_import"),
        default=None,
        help=(
            "When IVFFlat CREATE INDEX statements are issued. "
            "Default: bootstrap_before_import. Mode C (deferred) "
            "requires --index-build-phase after_import."
        ),
    )
    run.add_argument(
        "--run-analyze", dest="run_analyze", action="store_true",
        help=(
            "Issue ANALYZE on the canonical corpus tables after "
            "import (Mode B) and again after deferred index "
            "creation (Mode C)."
        ),
    )
    run.add_argument("--repeatability", action="store_true",
                     help="Run a second cached pass and write repeatability.json.")
    run.add_argument(
        "--sample-isolated", dest="sample_isolated",
        action="store_true", default=False,
        help=(
            "G6C-B0.1 sample-isolated orchestrator: for every sample "
            "in scope, DROP+CREATE a fresh disposable database whose "
            "corpus is that sample's rows only (all sessions retained) "
            "and aggregate the per-sample passes into the canonical "
            "artifact set. Incompatible with --case-limit and "
            "--repeatability."
        ),
    )
    run.add_argument("--commit-sha", default="",
                     help="40-char git SHA to stamp the manifest with.")
    run.add_argument("--write-rows", action="store_true",
                     help="Write import-rows.json to --output-dir for inspection.")

    return p


# ---------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Top-level CLI entry point.

    ``argv`` is exposed so tests / parent drivers can drive
    the CLI without spawning a subprocess. When ``argv`` is
    None we read ``sys.argv[1:]``.
    """

    parser = make_argument_parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return EXIT_INPUT_ERROR

    if int(args.batch_size or 32) <= 0:
        print(
            f"cli: invalid --batch-size {args.batch_size}; must be positive",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR
    if bool(args.rerank) and not str(args.rerank_endpoint_env or "").strip():
        print(
            "cli: --rerank requires --rerank-endpoint-env to be set "
            "(env-var NAME only, never a raw URL or key)",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR

    # ----- G6C-B0.1 sample-isolated guards (fail closed) -----
    # The orchestrator cannot honor a global --case-limit because
    # the per-sample passes see only their own cases — re-applying
    # the cap silently would change the meaning of every slice.
    if bool(getattr(args, "sample_isolated", False)) and (
        getattr(args, "case_limit", None) is not None
    ):
        print(
            "cli: --case-limit and --sample-isolated are incompatible; "
            "a global cap has no unambiguous per-sample meaning. "
            "Drop --case-limit or --sample-isolated.",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR
    # The B0.1 gate is two independent full runs, not an in-run
    # second pass — repeatability + sample-isolated is refused.
    if bool(getattr(args, "sample_isolated", False)) and bool(
        getattr(args, "repeatability", False)
    ):
        print(
            "cli: --repeatability and --sample-isolated are "
            "incompatible; the B0.1 gate is two independent full "
            "runs. Drop --repeatability or --sample-isolated.",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR

    # Stage 1: dry-run — always runs.
    try:
        dry_summary = run_dry_run(args)
    except _dataset.LoCoMoSourceError as exc:
        print(f"cli: dry-run failed — source: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except SourceConfigRefused as exc:
        print(f"cli: dry-run failed — source-config: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except LabConfigRefused as exc:
        print(f"cli: dry-run failed — lab config refused: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except FileNotFoundError as exc:
        print(f"cli: dry-run failed — file not found: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except CLIError as exc:
        print(f"cli: dry-run failed — {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except Exception as exc:
        print(f"cli: dry-run failed — {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    if str(args.mode) == "dry-run":
        print(json.dumps(
            dry_summary.to_dict(),
            ensure_ascii=False, sort_keys=True, indent=2,
        ))
        return EXIT_OK

    # Stage 2: semantic. Touches the disposable lab PG and
    # the provider (via the embedding-preparation module).
    try:
        if bool(getattr(args, "sample_isolated", False)):
            result = _run_semantic_sample_isolated(args)
        else:
            result = run_semantic(args)
    except SourceConfigRefused as exc:
        print(f"cli: semantic failed — source-config: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except LabConfigRefused as exc:
        print(f"cli: semantic failed — lab config refused: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except _lab.LabDSNRefused as exc:
        print(f"cli: semantic failed — lab refused DSN — {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except IntegrationTODOError as exc:
        print(f"cli: integration gap — {exc}", file=sys.stderr)
        return EXIT_INTEGRATION_GAP
    except CLIError as exc:
        print(f"cli: semantic failed — {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except Exception as exc:
        print(f"cli: semantic failed — {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())