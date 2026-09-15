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
# Constants
# ---------------------------------------------------------------------

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


def _make_disposable_base_path(cache_dir: str, dataset_sha: str) -> str:
    """Create a real empty disposable experiment directory.

    Returns the absolute path. Raises :class:`LabConfigRefused` if a
    pre-existing directory holds anything the evaluator did not create
    itself (the facade's own ``v3_cards.db`` may persist between runs,
    so it is explicitly allowed — that keeps reruns possible while
    still refusing to share an unrelated directory).
    """

    _ensure_dir(cache_dir)
    base_path = os.path.join(cache_dir, f"experiment-base-{dataset_sha[:12]}")
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
):
    """Load the dataset and apply case-limit / sample-filter.

    Returns ``(data, eval_rows, sample_ids_filter)``.
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
    return data, eval_rows, allowed


def _build_manifest(
    *,
    data,
    source_config_sha: str,
    embed_section: Mapping[str, Any],
    commit_sha: str,
    allowed_samples: set[str] | None,
    case_limit: int | None,
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
            "batch_size", "mode",
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
        "batch_size": src.get("batch_size"),
        "mode": str(src.get("mode", "dry-run") or "dry-run"),
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
      * ``batch_size`` (int | None)
      * ``mode`` (str; informational)

    Returns a :class:`StructuralResult`.
    """

    params = _coerce_dry_run_kwargs(args, **kwargs)
    (
        dataset_path, expected_sha256, source_config_path, dsn,
        output_dir, cache_dir, rerank, rerank_endpoint_env,
        write_rows, commit_sha, case_limit, sample_filter,
        _batch_size, _mode,
    ) = (
        params["dataset_path"], params["expected_sha256"],
        params["source_config_path"], params["dsn"],
        params["output_dir"], params["cache_dir"], params["rerank"],
        params["rerank_endpoint_env"], params["write_rows"],
        params["commit_sha"], params["case_limit"],
        params["sample_filter"], params["batch_size"], params["mode"],
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

    data, eval_rows, allowed_samples = _resolve_dataset_and_rows(
        dataset_path=dataset_path,
        expected_sha256=str(sha),
        case_limit=case_limit,
        sample_filter=parsed_filter,
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


def _make_lab_store(lab_cfg: Mapping[str, Any]) -> Any:
    """Instantiate the canonical PG store against the isolated lab.

    The store is given the isolated plain dict (never a production
    ``V3Config``); ``storage.pg`` carries explicit psycopg2 kwargs so
    the store cannot fall back to its built-in production defaults.
    """

    from v3core.pg_store import PgEmbedStore  # lazy: keep import cheap

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


def run_semantic(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the semantic stage.

    Thin orchestrator over the existing modules:

      1. Load dataset + apply filters.
      2. Open the disposable lab connection + bootstrap +
         import_rows.
      5. Build the provenance map from the freshly-imported
         rows (deterministic ordinal mapping).
      6. Read ``storage.embed`` from the YAML source config
         and call :func:`embeddings.prepare_query_embeddings` /
         :func:`embeddings.prepare_corpus_embeddings`.
      7. Build ``q_emb_by_case`` and run
         :func:`runner.run_case` sequentially.
      8. Map candidate IDs through the provenance map.
      9. Compute metrics + coverage; write the artifacts.
      10. ``--repeatability`` runs a second cached pass and
         writes ``repeatability.json``.
    """

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
    data, eval_rows, allowed_samples = _resolve_dataset_and_rows(
        dataset_path=str(args.dataset),
        expected_sha256=str(sha),
        case_limit=args.case_limit,
        sample_filter=sample_filter,
    )

    # Isolated lab config + a real empty disposable experiment
    # directory. The dict is the ONLY config the facade/engine ever
    # sees: ``storage.pg`` points at the disposable loopback lab and
    # ``storage.embed`` carries the borrowed ephemeral provider
    # credential (process memory only).
    base_path = _make_disposable_base_path(str(args.cache_dir), str(sha))
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

    conn = _connect_lab(str(args.dsn))
    try:
        _lab.bootstrap_schema(conn, _resolve_repo_root())
        _lab.import_rows(conn, rows_dict)
        qa_id_index, conv_prov_rows = _read_back_provenance(conn)
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

    pg_store = _make_lab_store(lab_cfg)
    cases = _build_runner_cases(data=data, eval_rows=eval_rows)
    records = _run_facade_pass(
        cases=cases,
        q_emb_by_case=q_emb_by_case,
        config=lab_cfg,
        pg=pg_store,
        expected_dim=DEFAULT_EMBEDDING_DIM,
    )
    records, id_mapping_audit = _map_record_rankings(records, provenance_map)

    # Fail closed: an objective run whose cases did not pass the facade
    # seam (missing/invalid query vector, trace_missing, engine error,
    # …) must NOT be written out as if it had measured retrieval
    # quality. The statuses are reported instead of being averaged away.
    bad_records = [
        rec for rec in records
        if str(getattr(rec, "status", "") or "") != "ok"
    ]
    if bad_records:
        first = bad_records[0]
        raise CLIError(
            f"semantic run aborted: {len(bad_records)}/{len(records)} case(s) "
            f"did not pass the facade seam; first case_id="
            f"{getattr(first, 'case_id', '?')!r} status="
            f"{getattr(first, 'status', '')!r} error="
            f"{str(getattr(first, 'error', '') or '')[:200]!r}"
        )

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
        # ``q_emb_by_case``.
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

    return summary


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
    run.add_argument("--repeatability", action="store_true",
                     help="Run a second cached pass and write repeatability.json.")
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