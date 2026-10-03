"""v3core.tools.memory_correction — thin tool-layer seam for M01/M02.

This module is deliberately *thin*. It owns no DML and no version
semantics of its own: the canonical boundary is
``v3core.active_memory_store`` (``ActiveMemoryWriter.correct`` /
``ActiveMemoryReader.read_version``), and every public call here forwards
into it. No SQL, no second active-memory system, no new tool.

What the tool layer *does* own (per
``docs/M01-M02-MEMORY-CORRECTION-DESIGN.md``):

  * fail-closed input validation — an explicit empty string, an invalid
    tag list, a naive timestamp or a conflicting ``memory_id`` /
    ``source_id`` pair is rejected, never silently defaulted;
  * ``authority='user_explicit'`` — a correction relation is only ever
    established by an explicitly authorized operation. Model inference,
    a merely newer statement, and every passive write path do NOT gain
    this authority automatically;
  * booted-profile injection — when the host injected a core /
    effective_config / pool / runtime_context, that exact scope is what
    the canonical class receives. A no-arg ``resolve_config()`` fallback
    is only used on the legacy in-process path where nothing was
    injected, because a silent fallback would read/write the *default*
    profile's database;
  * truthful receipts — the caller always learns which memory changed,
    which relation was written, and, on failure, the canonical code
    (``NOT_FOUND`` / ``CORRECTION_CONFLICT`` / ...). A failed lookup
    never degrades into "created a new standalone memory".

Effective time: ``effective_at`` records the audit valid-from instant.
M01 does *not* schedule future activation and does *not* implement
as-of reasoning; it is stored and replayed for exact-request identity
only.

Correction source: ``correction_source_id`` is stored literally as a
``conversation_stream.id`` (role=user). When the caller supplies none,
provenance says ``explicit_tool_request`` and no source is invented.
"""
from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("v3core.tools.memory_correction")

__all__ = [
    "CANONICAL_FAILURE",
    "CORRECTION_AUTHORITY",
    "CORRECTION_MODES",
    "INVALID_REQUEST",
    "READ_MODES",
    "SCOPE_UNAVAILABLE",
    "handle_correct_action",
    "handle_memory_target",
    "read_version",
    "render_canonical_for_source",
    "resolve_scope",
]

#: The only authority value a public correction may carry.
CORRECTION_AUTHORITY = "user_explicit"

#: Machine-readable receipt codes for tool-layer failures. A caller must be
#: able to branch on WHY a request failed without parsing prose. The
#: canonical writer's own codes (``NOT_FOUND`` / ``CORRECTION_CONFLICT`` /
#: ``MIGRATION_REQUIRED`` / ...) are copied through unchanged and never
#: replaced by these.
INVALID_REQUEST = "INVALID_REQUEST"
SCOPE_UNAVAILABLE = "SCOPE_UNAVAILABLE"
CANONICAL_FAILURE = "CANONICAL_FAILURE"

#: Locked public read modes. Anything else is a caller error.
READ_MODES = ("current", "history")

#: The frozen correction modes. An OMITTED mode means replace, so every
#: pre-existing caller keeps its exact wire contract.
CORRECTION_MODES = ("replace", "withdraw")

_DEFAULT_MODE = "current"

_DEFAULT_CORRECTION_MODE = "replace"

_HISTORY_KEYS = ("mode", "include_history")

#: Payload keys a withdraw must never carry — not even empty. A withdrawal
#: that "also" supplies a replacement is a caller error, never a silent
#: reinterpretation into a replace.
_WITHDRAW_FORBIDDEN_KEYS = (
    "replacement_content",
    "replacement_title",
    "replacement_tags",
)


# ── canonical seams ────────────────────────────────────────────────────────
#
# ``_writer_cls`` / ``_reader_cls`` are the two injection points the
# contract tests replace. They are resolved lazily at call time so the
# tool layer never depends on import order, and so the canonical store
# module can be mocked without monkeypatching a live class binding.

_WRITER_IMPORT = ("v3core.active_memory_store", "ActiveMemoryWriter")
_READER_IMPORT = ("v3core.active_memory_store", "ActiveMemoryReader")


def _resolve_seam(spec: tuple[str, str]):
    module_name, attr = spec
    import importlib

    return getattr(importlib.import_module(module_name), attr)


def _accepted_kwargs(cls: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop keywords the canonical class does not declare.

    The public seam contract is uniform (``pool`` / ``pg`` / ``config``);
    the canonical reader today takes only ``pool`` / ``pg`` / ``deadline``.
    Forwarding an undeclared keyword would raise ``TypeError`` on the real
    class, so narrow here instead — and never guess a replacement.
    """
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):  # pragma: no cover — exotic callables
        return dict(kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in params}


def _writer_cls(**kwargs: Any):
    """Construct the canonical writer for the injected scope."""
    cls = _resolve_seam(_WRITER_IMPORT)
    return cls(**_accepted_kwargs(cls, kwargs))


def _reader_cls(**kwargs: Any):
    """Construct the canonical reader for the injected scope."""
    cls = _resolve_seam(_READER_IMPORT)
    return cls(**_accepted_kwargs(cls, kwargs))


# ── scope resolution ───────────────────────────────────────────────────────


class _ScopeUnavailable(RuntimeError):
    """A declared runtime scope is incomplete.

    Raised instead of silently falling back to a no-arg ``resolve_config()``
    (the *default* profile's database). Subclasses ``RuntimeError`` so every
    pre-existing caller that catches ``RuntimeError`` keeps working.
    """


def resolve_scope(kw: dict) -> tuple[Any, Any, Any]:
    """Return ``(pool, pg, config)`` for the caller's booted scope.

    The booted core is consulted for ``pool`` / ``pg`` / ``config``
    INDEPENDENTLY: reading ``core.config`` only when no pool was supplied
    used to drop the core's config and fall through to
    ``resolve_config()`` — i.e. the *default* profile's database — whenever
    the host passed both a pool and a core.

    Fail closed for an injected runtime: when a core / effective_config /
    runtime_context was DECLARED — by presence, not by truthiness, so an
    explicit ``runtime_context=False`` / ``None`` still counts — a missing
    pool or a missing effective config is an error, never an implicit
    profile resolve. The legacy in-process path (nothing declared at all)
    keeps its old behaviour.
    """
    pool = kw.get("pool")
    if pool is None:
        pool = kw.get("pg_pool")
    cfg = kw.get("effective_config")
    pg = None
    core = kw.get("core")
    if core is not None:
        # Inherit from the booted core regardless of whether a pool was
        # supplied separately — each field is resolved on its own.
        pool = pool or getattr(core, "pg_pool", None) or getattr(core, "_pg_pool", None)
        if cfg is None:
            cfg = getattr(core, "config", None)
        pg = getattr(core, "_pg", None) or getattr(core, "pg", None)

    runtime = "runtime_context" in kw or core is not None or "effective_config" in kw
    if runtime:
        if pool is None and pg is None:
            raise _ScopeUnavailable(
                "runtime_context is set but the PgPool owner is missing; "
                "refusing to resolve a profile implicitly"
            )
        if cfg is None:
            raise _ScopeUnavailable(
                "runtime_context is set but the effective config is missing; "
                "refusing to resolve the default profile implicitly"
            )
    if cfg is None:
        from ..config import resolve_config

        cfg = resolve_config()
    return pool, pg, cfg


# ── validation ─────────────────────────────────────────────────────────────


class _Invalid(ValueError):
    """A caller-supplied correction input is not acceptable."""


def _required_text(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _Invalid(f"{key} is required and must be a non-empty string")
    return value


def _optional_text(args: dict, key: str) -> str | None:
    """Omitted -> inherit/None. Explicitly present but empty -> reject.

    An explicit empty string is a caller mistake, not a request to
    default — the two must never collapse into the same behaviour.
    """
    if key not in args:
        return None
    value = args[key]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _Invalid(
            f"{key} was supplied explicitly but empty; omit the key to inherit "
            f"the previous value"
        )
    return value


def _optional_tags(args: dict, key: str) -> list[str] | None:
    if key not in args:
        return None
    value = args[key]
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise _Invalid(f"{key} must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _Invalid(f"{key} must be a list of non-empty strings")
        out.append(item)
    return out


def _optional_timestamp(args: dict, key: str) -> str | None:
    value = _optional_text(args, key)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _Invalid(
            f"{key} must be an ISO-8601 timestamp with a timezone offset, "
            f"e.g. 2026-10-03T12:00:00+08:00 (got {value!r})"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _Invalid(
            f"{key} must carry a timezone offset; a naive local time is "
            f"ambiguous as an audit valid-from instant"
        )
    return value


def _correction_mode(args: dict) -> str:
    """Validate the frozen correction mode. OMITTED means replace.

    An explicitly supplied ``None`` is NOT an omission: ``null`` is not in
    the ``mode`` string enum, so it is a caller error (the schema never
    declares a null mode) and must fail before the writer is constructed.
    """
    if "mode" not in args:
        return _DEFAULT_CORRECTION_MODE
    mode = args["mode"]
    if not isinstance(mode, str) or mode not in CORRECTION_MODES:
        raise _Invalid(
            f"mode must be one of {list(CORRECTION_MODES)} (got {mode!r})"
        )
    return mode


def _reject_withdraw_payload(args: dict, mode: str) -> None:
    """A withdrawal MUST NOT create a replacement.

    Any replacement payload is a caller error — including an empty one.
    Silently ignoring ``replacement_content=""`` would reinterpret the
    request as a replace and write a row the caller never asked for.
    """
    if mode != "withdraw":
        return
    supplied = [key for key in _WITHDRAW_FORBIDDEN_KEYS if key in args]
    if supplied:
        raise _Invalid(
            f"mode='withdraw' must not carry a replacement payload; "
            f"remove {sorted(supplied)} (a withdrawal creates no replacement "
            f"and never reinterprets an empty value as a replace)"
        )


def _source_id_text(args: dict, key: str) -> str | None:
    """A source id is a literal ASCII decimal ``conversation_stream.id``.

    ``'+1'``, whitespace, ``'1.0'`` and non-ASCII digits are all rejected
    rather than repaired: the value is provenance and is never rewritten.
    """
    value = _optional_text(args, key)
    if value is None:
        return None
    if not value.isascii() or not value.isdigit():
        raise _Invalid(
            f"{key} must be ASCII decimal digits only, exactly as stored in "
            f"public.conversation_stream.id (got {value!r}); the literal is "
            f"never repaired and no source is ever invented"
        )
    return value


def _read_mode(args: dict) -> str:
    """Validate the locked read mode. OMITTED means current.

    An explicitly supplied ``None`` is a caller error — ``null`` is not in
    the ``mode`` string enum — and fails before any canonical read.
    """
    if "mode" not in args:
        return _DEFAULT_MODE
    mode = args["mode"]
    if not isinstance(mode, str) or mode not in READ_MODES:
        raise _Invalid(
            f"mode must be one of {list(READ_MODES)} (got {mode!r}); "
            f"there is no latest-text-wins mode"
        )
    return mode


def _include_history(args: dict) -> bool:
    """Validate ``include_history``. OMITTED means False.

    An explicitly supplied ``None`` is a caller error: ``null`` is not a
    boolean, so it must not collapse into the omitted default.
    """
    if "include_history" not in args:
        return False
    value = args["include_history"]
    if not isinstance(value, bool):
        raise _Invalid("include_history must be a boolean")
    return value


def _history_requested(args: dict) -> bool:
    """Did the caller explicitly ask for history on this call?

    Used by the ``hm`` forward path: legacy non-explicit reads must keep
    their exact existing behaviour, so history parameters are only
    forwarded (and only the canonical reader is used) when the caller
    actually supplied them.
    """
    if _include_history(args):
        return True
    return args.get("mode") is not None


# ── canonical calls ────────────────────────────────────────────────────────


def correct(memory_id: str, **kwargs: Any) -> Any:
    """Forward to the canonical ``ActiveMemoryWriter.correct``."""
    writer = _writer_cls(
        pool=kwargs.pop("pool", None),
        pg=kwargs.pop("pg", None),
        config=kwargs.pop("config", None),
    )
    return writer.correct(memory_id, **kwargs)


def read_version(memory_id: str, **kwargs: Any) -> Any:
    """Forward to the canonical ``ActiveMemoryReader.read_version``."""
    reader = _reader_cls(
        pool=kwargs.pop("pool", None),
        pg=kwargs.pop("pg", None),
        config=kwargs.pop("config", None),
    )
    return reader.read_version(memory_id, **kwargs)


#: Canonical receipt keys copied through verbatim. Nothing here is renamed,
#: defaulted or dropped — a field the writer did not send stays absent.
_RECEIPT_KEYS = (
    "status",
    "durable",
    "error_code",
    "old_memory_id",
    "new_memory_id",
    "old_status",
    "new_status",
    "current_memory_id",
    "deduplicated",
    "correction_source",
    "effective_at",
    "relation",
    "warnings",
)


def _as_result(raw: Any) -> dict[str, Any]:
    """Normalize the canonical return into a mapping, fail closed."""
    if isinstance(raw, dict):
        return dict(raw)
    if hasattr(raw, "to_dict") and callable(raw.to_dict):
        out = raw.to_dict()
        if isinstance(out, dict):
            return out
    if hasattr(raw, "success"):
        out = {
            "success": bool(getattr(raw, "success", False)),
            "error": str(getattr(raw, "error", "") or ""),
        }
        for key in ("old_memory_id", "new_memory_id", "code", "error_code",
                    "relation", "old_status", "new_status",
                    "deduplicated", "correction_source", "effective_at",
                    "current_memory_id", "warnings", "durable"):
            if hasattr(raw, key):
                out[key] = getattr(raw, key)
        return out
    raise RuntimeError(
        f"canonical seam returned an unusable result: {type(raw).__name__}"
    )


def _failure(result: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A truthful failure receipt.

    The canonical ``error_code`` is preserved (mirrored as ``code`` for the
    historical readers) and a real ``durable=True`` — an already-committed
    write whose readback verification failed — is never reported as a
    rollback. Statuses and ids that the writer did not send are NEVER
    invented.
    """
    payload: dict[str, Any] = {
        "success": False,
        "error": str(result.get("error") or result.get("error_code") or
                     result.get("code") or "correction failed"),
    }
    code = result.get("error_code") or result.get("code")
    if code:
        payload["error_code"] = code
        payload["code"] = code
    for key in _RECEIPT_KEYS:
        if key in ("error_code", "durable"):
            continue
        # A key the canonical writer actually sent is copied through even
        # when its value is None: ``new_status=None`` means "no successor
        # status", and dropping the key would hide that fact.
        if key in result:
            payload[key] = result[key]
            extra.pop(key, None)
        elif extra.get(key) is not None:
            payload[key] = extra.pop(key)
    # A real durability verdict is always reported, False included: the
    # caller must never have to guess whether the write landed. An UNKNOWN
    # verdict (canonical ``None``) stays ``None`` — coercing it to False
    # would claim a rollback the writer never asserted.
    if "durable" in result:
        payload["durable"] = result.get("durable")
    elif "durable" in extra:
        payload["durable"] = extra.pop("durable")
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


# ── v3_update(action='correct') ────────────────────────────────────────────


def handle_correct_action(args: dict, **kw) -> str:
    """Handle ``v3_update(action='correct')``.

    Every validation failure is closed *before* the canonical writer is
    constructed, so a rejected request cannot create or mutate any row.
    """
    try:
        memory_id = _required_text(args, "memory_id")
        mode = _correction_mode(args)
        # Only an EXPLICIT, validated mode travels on the wire. An omitted
        # mode relies on the canonical writer's own default (frozen legacy
        # wire contract), so a canonical stub with an exact signature never
        # receives a ``mode`` keyword the caller never supplied.
        mode_supplied = "mode" in args
        _reject_withdraw_payload(args, mode)
        if mode == "replace":
            replacement = _required_text(args, "replacement_content")
        else:
            replacement = None
        title = _optional_text(args, "replacement_title")
        tags = _optional_tags(args, "replacement_tags")
        reason = _optional_text(args, "correction_reason")
        source_id = _source_id_text(args, "correction_source_id")
        effective_at = _optional_timestamp(args, "effective_at")

        # ``source_id`` is a legacy alias on v3_update. On the correct
        # path it may only repeat the target identity — a differing pair
        # is an ambiguous request, never something to rewrite. The
        # comparison is the EXACT literal: no strip / case / unicode
        # normalization, because the alias is provenance-adjacent identity.
        alias = args.get("source_id")
        if alias is not None and isinstance(alias, str) and alias.strip():
            if alias != memory_id:
                raise _Invalid(
                    "source_id conflicts with memory_id; the correct action "
                    "takes exactly one target memory_id"
                )
        elif alias is not None and key_present(args, "source_id"):
            raise _Invalid(
                "source_id was supplied explicitly but empty; omit the key to "
                "target memory_id alone"
            )

        pool, pg, cfg = resolve_scope(kw)
    except _Invalid as exc:
        return json.dumps(
            {
                "success": False,
                "action": "correct",
                "error_code": INVALID_REQUEST,
                "code": INVALID_REQUEST,
                "error": str(exc),
            },
            ensure_ascii=False,
        )
    except _ScopeUnavailable as exc:
        return json.dumps(
            {
                "success": False,
                "action": "correct",
                "error_code": SCOPE_UNAVAILABLE,
                "code": SCOPE_UNAVAILABLE,
                "error": f"memory correction unavailable: {_short(exc)}",
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "action": "correct",
                "error_code": SCOPE_UNAVAILABLE,
                "code": SCOPE_UNAVAILABLE,
                "error": f"memory correction unavailable: {_short(exc)}",
            },
            ensure_ascii=False,
        )

    try:
        # A withdrawal sends NO replacement payload at all — the canonical
        # writer must not be handed a ``replacement_content`` key to
        # reinterpret. A replace always sends one.
        call: dict[str, Any] = {
            "replacement_title": title,
            "replacement_tags": tags,
            "correction_reason": reason,
            "correction_source_id": source_id,
            "effective_at": effective_at,
            "authority": CORRECTION_AUTHORITY,
            "pool": pool,
            "pg": pg,
            "config": cfg,
        }
        if mode_supplied:
            call["mode"] = mode
        if mode != "withdraw":
            call["replacement_content"] = replacement
        raw = correct(memory_id, **call)
        result = _as_result(raw)
    except Exception as exc:  # noqa: BLE001
        # No durability verdict is claimed here: the canonical writer never
        # answered, so this failure is machine-readable but carries no
        # ``durable`` key rather than inventing a rollback.
        return json.dumps(
            {
                "success": False,
                "action": "correct",
                "mode": mode,
                "authority": CORRECTION_AUTHORITY,
                "error_code": CANONICAL_FAILURE,
                "code": CANONICAL_FAILURE,
                "error": f"memory correction failed: {_short(exc)}",
            },
            ensure_ascii=False,
        )

    if not result.get("success"):
        return json.dumps(
            _failure(
                result, action="correct", mode=mode,
                authority=CORRECTION_AUTHORITY,
            ),
            ensure_ascii=False,
        )

    # Every receipt field is copied through verbatim. A missing status or
    # successor id is reported as absent/None — never defaulted to
    # 'active' or '', which would invent a version that does not exist.
    payload: dict[str, Any] = {
        "success": True,
        "action": "correct",
        "mode": mode,
        "authority": CORRECTION_AUTHORITY,
        "old_memory_id": result.get("old_memory_id") or memory_id,
        "old_status": result.get("old_status"),
        "deduplicated": bool(result.get("deduplicated", False)),
        "correction_source": result.get("correction_source")
        or {
            "provenance": "explicit_tool_request",
            "correction_source_id": source_id,
        },
        "effective_at": result.get("effective_at", effective_at),
    }
    for key in ("new_memory_id", "new_status", "current_memory_id",
                "relation", "status", "durable", "error_code"):
        if key in result:
            payload[key] = result[key]
    if result.get("warnings"):
        payload["warnings"] = list(result["warnings"])
    return json.dumps(payload, ensure_ascii=False)


def key_present(args: dict, key: str) -> bool:
    return key in args


def _short(exc: Exception, max_len: int = 200) -> str:
    from . import _safe_err

    return _safe_err(exc, max_len)


# ── v3_get(target='memory') ────────────────────────────────────────────────


def handle_memory_target(args: dict, **kw) -> str:
    """Handle ``v3_get(target='memory')`` through the canonical reader."""
    try:
        memory_id = _required_text(args, "memory_id")
        mode = _read_mode(args)
        include_history = _include_history(args)
        pool, pg, cfg = resolve_scope(kw)
    except _Invalid as exc:
        return json.dumps(
            {
                "success": False,
                "error_code": INVALID_REQUEST,
                "code": INVALID_REQUEST,
                "error": str(exc),
            },
            ensure_ascii=False,
        )
    except _ScopeUnavailable as exc:
        return json.dumps(
            {
                "success": False,
                "error_code": SCOPE_UNAVAILABLE,
                "code": SCOPE_UNAVAILABLE,
                "error": f"memory read unavailable: {_short(exc)}",
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "error_code": SCOPE_UNAVAILABLE,
                "code": SCOPE_UNAVAILABLE,
                "error": f"memory read unavailable: {_short(exc)}",
            },
            ensure_ascii=False,
        )

    try:
        raw = read_version(
            memory_id,
            mode=mode,
            include_history=include_history,
            pool=pool,
            pg=pg,
            config=cfg,
        )
        result = _as_result(raw)
    except Exception as exc:  # noqa: BLE001
        # A missing relation table is a truthful migration-required
        # failure, never a silent empty answer — and no durability verdict
        # is invented for a canonical read that never answered.
        return json.dumps(
            {
                "success": False,
                "memory_id": memory_id,
                "mode": mode,
                "error_code": CANONICAL_FAILURE,
                "code": CANONICAL_FAILURE,
                "error": f"memory read failed: {_short(exc)}",
            },
            ensure_ascii=False,
        )

    if not result.get("success"):
        return json.dumps(
            _failure(result, memory_id=memory_id, mode=mode),
            ensure_ascii=False,
        )
    payload = dict(result)
    payload.setdefault("mode", mode)
    payload.setdefault("memory_id", memory_id)
    payload.setdefault(
        "requested_memory_id", result.get("requested_memory_id") or memory_id
    )
    payload["success"] = True
    return json.dumps(payload, ensure_ascii=False)


#: Keys copied from the canonical row into the flat ``metadata`` block.
_METADATA_KEYS = (
    "title", "category", "tags", "provenance", "status",
    "created_at", "updated_at",
)


def _selected_canonical_row(result: dict[str, Any]) -> tuple[Any, bool]:
    """Pick the nested row the requested mode is about, and whether a
    payload exists to report.

    The canonical reader answer is NESTED — ``requested`` (the exact version
    the caller named), ``memory`` (the current terminal version) and
    ``history`` — so a renderer that reads a flat ``content`` silently
    answers ''. history answers with ``requested``; current answers with
    ``memory``. A withdrawn chain has ``memory=None``: nothing is reported
    as the current payload, and the requested row stays available under
    ``requested`` for the historical read only.
    """
    mode = result.get("mode") or _DEFAULT_MODE
    if mode == "history":
        row = result.get("requested")
        if not isinstance(row, dict):
            row = result.get("memory")
        return (row if isinstance(row, dict) else None), True
    current = result.get("memory")
    if isinstance(current, dict):
        return current, True
    # No current version: a withdrawn chain. Report the absence instead of
    # substituting the archived/requested payload.
    return None, False


def render_canonical_for_source(result: dict[str, Any], source_id: str) -> dict[str, Any]:
    """Shape a canonical reader result for the unified ``hm`` read path.

    The canonical result is nested (``requested`` / ``memory`` / ``history``);
    the legacy ``hm_get`` contract stays flat, so the SELECTED nested row is
    flattened here. Identity, history and the relation (including the
    server-side ``recorded_at``) are copied in full.

    A current read with no current memory (a withdrawn chain) has NO
    content: ``content`` is None and ``resolved_memory_id`` is None. The
    stale/archived payload is never substituted.
    """
    mode = result.get("mode") or _DEFAULT_MODE
    selected, has_payload = _selected_canonical_row(result)
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        metadata = (
            {key: selected[key] for key in _METADATA_KEYS if key in selected}
            if isinstance(selected, dict) else {}
        )

    payload: dict[str, Any] = {
        "success": True,
        "source_id": source_id,
        "content": (selected.get("content") if has_payload else None),
        "source": "explicit_memories",
        "metadata": metadata,
        "requested_memory_id": result.get("requested_memory_id") or source_id,
        # Chain identity is a fact about the chain, kept verbatim in both
        # modes; only the PAYLOAD follows the mode.
        "resolved_memory_id": result.get("resolved_memory_id"),
        "is_current": bool(result.get("is_current", False)),
        "mode": mode,
    }
    # The nested canonical blocks are carried through unchanged so a caller
    # always has the exact requested payload and the full chain.
    for key in ("memory", "requested", "history", "status", "error_code",
                "current_memory_id", "relations", "relation", "correction_source", "effective_at", "recorded_at"):
        if key in result:
            payload[key] = result[key]
    return payload
