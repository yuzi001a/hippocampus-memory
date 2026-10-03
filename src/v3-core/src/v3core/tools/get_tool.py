"""hm_get — unified read tool + hm_status health tool for v3-core (hippocampus) system.

hm_get accepts ANY source_id (message, card, handbook, file) and returns full content.
hm_status returns basic system health info.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path

from .memory_correction import (
    CANONICAL_FAILURE,
    INVALID_REQUEST,
    SCOPE_UNAVAILABLE,
)


try:
    from . import _safe_err
except (ImportError, AttributeError):
    try:
        from .. import _safe_err
    except (ImportError, AttributeError):
        def _safe_err(e, max_len=80):
            try:
                from . import _safe_err as _impl
            except ImportError:
                from .. import _safe_err as _impl
            globals()["_safe_err"] = _impl
            return _impl(e, max_len)

logger = logging.getLogger("v3core.tools.get")


#: Locked public read modes — anything else is a caller error.
READ_MODES = ("current", "history")


class _Invalid(ValueError):
    """A caller-supplied read parameter is not acceptable."""


def _explicit_history_requested(args: dict) -> bool:
    """Did the caller explicitly ask for version-history semantics?

    Only these two parameters count. A caller that supplies neither is
    asking for the legacy plain read, and that path must stay unchanged
    for every non-corrected active memory.
    """
    if args.get("include_history"):
        return True
    return args.get("mode") is not None


def _read_parameters(args: dict) -> tuple[str, bool]:
    """Validate the history parameters strictly and pick the read mode.

    An OMITTED parameter takes its documented default; an explicitly
    supplied ``null`` is a caller error — ``null`` is in neither the
    ``mode`` string enum nor the ``include_history`` boolean schema — so it
    fails before any lookup instead of collapsing into the omitted default.

    Both parameters are checked, never coerced: ``bool('false')`` is truthy
    and ``include_history=True`` must NOT silently switch the read to
    history mode. ``include_history`` only attaches the history to the
    mode that was actually requested (current by default), so the payload
    stays the CURRENT version instead of the archived one.
    """
    if "mode" not in args:
        mode = "current"
    else:
        mode = args["mode"]
        if not isinstance(mode, str) or mode not in READ_MODES:
            raise _Invalid(
                f"mode must be one of {list(READ_MODES)} (got {mode!r}); "
                f"include_history only attaches the history and never selects a mode"
            )
    if "include_history" not in args:
        include_history = False
    else:
        include_history = args["include_history"]
        if not isinstance(include_history, bool):
            raise _Invalid(
                f"include_history must be a boolean (got {include_history!r}); "
                f"a truthy string is not a request for history"
            )
    return mode, include_history


def _canonical_explicit_read(source_id: str, args: dict, core, kw: dict) -> str:
    """Read an explicit memory through the canonical version-aware reader.

    Used for an archived exact hit and for any explicit history request.
    A missing relation schema is a truthful migration-required failure —
    never a silent fall back to the archived raw payload.
    """
    from .memory_correction import (
        _as_result,
        read_version,
        render_canonical_for_source,
        resolve_scope,
    )

    try:
        mode, include_history = _read_parameters(args)
    except _Invalid as exc:
        return json.dumps(
            {
                "success": False,
                "source_id": source_id,
                "error_code": INVALID_REQUEST,
                "code": INVALID_REQUEST,
                "error": str(exc),
            },
            ensure_ascii=False,
        )
    # ``core`` is the ACTUAL booted/constructed core for this read. When the
    # caller's scope mapping carries no core — the host injected only a pool,
    # or handle_hm_get built the core itself — resolve against a COPY that
    # carries it. Otherwise the booted config / PG owner is lost and the read
    # silently falls back to the default profile (or trips an injected-runtime
    # guard). An explicitly supplied core / config / pool is never overwritten.
    scope = dict(kw)
    if scope.get("core") is None:
        scope["core"] = core
    try:
        pool, pg, cfg = resolve_scope(scope)
    except Exception as e:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "source_id": source_id,
                "error_code": SCOPE_UNAVAILABLE,
                "code": SCOPE_UNAVAILABLE,
                "error": f"memory version read unavailable: {_safe_err(e)}",
            },
            ensure_ascii=False,
        )
    try:
        raw = read_version(
            source_id,
            mode=mode,
            include_history=include_history,
            pool=pool,
            pg=pg,
            config=cfg,
        )
        result = _as_result(raw)
    except Exception as e:  # noqa: BLE001
        return json.dumps(
            {
                "success": False,
                "source_id": source_id,
                "error_code": CANONICAL_FAILURE,
                "code": CANONICAL_FAILURE,
                "error": f"memory version read failed: {_safe_err(e)}",
            },
            ensure_ascii=False,
        )
    if not result.get("success"):
        code = result.get("error_code") or result.get("code")
        payload = {
            "success": False,
            "source_id": source_id,
            "error": str(result.get("error") or code or "memory read failed"),
        }
        if code:
            # The canonical code is preserved, never dropped on this path.
            payload["error_code"] = code
            payload["code"] = code
        return json.dumps(payload, ensure_ascii=False)
    # Report the mode that was actually requested unless the canonical
    # reader stated one itself — never label a history read as 'current'.
    result.setdefault("mode", mode)
    return json.dumps(
        render_canonical_for_source(result, source_id), ensure_ascii=False
    )

# ── hm_get ──────────────────────────────────────────────────────────────────

HM_GET_SCHEMA = {
    "name": "hm_get",
    "description": (
        "[3-读取] Unified read tool — accepts any source_id (message, card, "
        "handbook, file) and returns full content. For an explicit memory id "
        "it returns the current version by default; pass mode='history' "
        "(optionally include_history) to read the original version and the "
        "full version history."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "Source ID to look up. Handles: handbook/xxx keys, PG message IDs, PG card source_ids, and file-based messages.",
            },
            "mode": {
                "type": "string",
                "enum": ["current", "history"],
                "description": (
                    "optional. Only meaningful for an explicit-memory source_id: "
                    "current (default) resolves the replacement chain to the "
                    "terminal current version; history returns the requested "
                    "original version with its full history. Omit to keep the "
                    "legacy plain read."
                ),
            },
            "include_history": {
                "type": "boolean",
                "description": (
                    "optional, must be a real boolean. Return the full ordered "
                    "version/edge history alongside the payload. It does NOT "
                    "change the mode: with the default mode=current the "
                    "payload stays the CURRENT version and the history is "
                    "attached to it."
                ),
            },
        },
        "required": ["source_id"],
    },
}


def handle_hm_get(args: dict, **kw) -> str:
    """Unified read entry point.

    Internal flow:
      1. If source_id starts with 'handbook/' -> HandbookManager.get()
      2. Else -> V3Core().get_message_context() (PG messages -> PG cards -> file fallback)
      3. If step 2 fails, try handbook lookup as supplementary fallback
      4. Return unified JSON result
    """
    try:
        source_id = args.get("source_id", "").strip()
        if not source_id:
            return json.dumps(
                {
                    "success": False,
                    "error_code": INVALID_REQUEST,
                    "code": INVALID_REQUEST,
                    "error": "source_id required",
                },
                ensure_ascii=False,
            )

        # History parameters are validated strictly BEFORE any lookup, so a
        # malformed mode / include_history fails as a caller error instead
        # of being coerced (``bool('false')`` is truthy) or silently
        # dropped. Nothing is written or read on this path.
        try:
            _read_parameters(args)
        except _Invalid as exc:
            return json.dumps(
                {
                    "success": False,
                    "source_id": source_id,
                    "error_code": INVALID_REQUEST,
                    "code": INVALID_REQUEST,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )

        # 1. Handbook path (explicit "handbook/" prefix)
        if source_id.startswith("handbook/"):
            key = source_id[len("handbook/"):]
            from ..handbook import HandbookManager
            from ._scope import current_scope, handbook_manager
            h = handbook_manager(current_scope())
            entry = h.get(key)
            if entry is None:
                return json.dumps(
                    {
                        "success": False,
                        "source_id": source_id,
                        "error": f"handbook entry not found: {key}",
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "success": True,
                    "source_id": source_id,
                    "content": entry.get("content", ""),
                    "source": "handbook",
                    "metadata": {
                        "title": entry.get("title", ""),
                        "tags": entry.get("tags", []),
                        "updated_at": entry.get("updated_at", ""),
                    },
                },
                ensure_ascii=False,
            )

        # 2. Active-memory source path: v3_search returns
        # ``explicit_memories.memory_id`` as source_id. Read it before the
        # historical message/card fallbacks so search -> source-read is one
        # coherent contract.

        # A DECLARED runtime scope must be complete BEFORE a default profile
        # would be resolved. Resolving the default profile here can silently
        # read it instead of the caller's declared runtime — an incomplete
        # injected runtime must fail closed instead. Only a declared runtime
        # is checked (presence, not truthiness, so an explicit
        # ``runtime_context=False`` / ``None`` / ``''`` counts); the legacy
        # no-runtime call keeps constructing the core exactly as before.
        #
        # A supplied ``core`` is itself a fully booted runtime, so it is NOT
        # by itself a refusal reason: its own ``.pg`` / ``.config`` are used
        # directly below. The guard therefore fires only when the caller
        # declares a runtime WITHOUT supplying a core, or when it declares an
        # explicitly empty ``effective_config`` (which would otherwise force
        # a default-profile resolution).
        _declared_runtime = (
            "runtime_context" in kw
            or "effective_config" in kw
            or kw.get("core") is not None
        )
        _explicit_empty_config = (
            "effective_config" in kw and kw.get("effective_config") is None
        )
        if _explicit_empty_config or (kw.get("core") is None and _declared_runtime):
            from .memory_correction import resolve_scope as _resolve_scope

            try:
                _resolve_scope(dict(kw))
            except Exception as e:  # noqa: BLE001
                return json.dumps(
                    {
                        "success": False,
                        "source_id": source_id,
                        "error_code": SCOPE_UNAVAILABLE,
                        "code": SCOPE_UNAVAILABLE,
                        "error": f"hm read scope unavailable: {_safe_err(e)}",
                    },
                    ensure_ascii=False,
                )

        from .. import V3Core

        core = kw.get("core") or V3Core(
            effective_config=kw.get("effective_config"),
            pg_pool=kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool"),
        )
        try:
            pg = getattr(core, "_pg", None)
            if pg is None:
                pg = getattr(core, "pg", None)
            if pg is not None:
                columns = (
                    "memory_id", "category", "title", "content", "tags",
                    "provenance", "status", "created_at", "updated_at",
                )
                with pg.lease() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT memory_id, category, title, content, tags,
                                   provenance, status, created_at, updated_at
                              FROM public.explicit_memories
                             WHERE memory_id = %s
                            """,
                            (source_id,),
                        )
                        row = cur.fetchone()
                if row:
                    record = dict(row) if hasattr(row, "keys") else dict(zip(columns, row))
                    # An explicit-memory exact hit. Two contracts:
                    #
                    #   * a plain read (no history parameters) of an
                    #     ACTIVE row keeps the existing raw payload. By
                    #     the canonical invariant an active row has no
                    #     outgoing corrects edge, so "current" and "raw"
                    #     are the same version here — the legacy shape
                    #     (source_id/content/metadata) stays byte-stable.
                    #   * an ARCHIVED row, or any explicit history
                    #     request, goes through the canonical reader so
                    #     a plain source inspection can never answer
                    #     with a superseded payload.
                    if _explicit_history_requested(args) or str(
                        record.get("status", "") or ""
                    ) != "active":
                        return _canonical_explicit_read(
                            source_id, args, core, kw
                        )
                    return json.dumps(
                        {
                            "success": True,
                            "source_id": source_id,
                            "content": record.get("content", ""),
                            "source": "explicit_memories",
                            "metadata": {
                                "title": record.get("title", ""),
                                "category": record.get("category", ""),
                                "tags": record.get("tags", []) or [],
                                "provenance": record.get("provenance", {}) or {},
                                "status": record.get("status", ""),
                                "created_at": str(record.get("created_at", "") or ""),
                                "updated_at": str(record.get("updated_at", "") or ""),
                            },
                        },
                        ensure_ascii=False,
                    )
        except Exception as e:
            logger.debug("explicit memory source read unavailable: %s", _safe_err(e))

        # 3. V3Core path (PG messages -> PG cards -> file fallback)

        raw = core.get_message_context(source_id)
        result = json.loads(raw)

        if result.get("success"):
            # Determine source type from response shape
            if "session_id" in result and result.get("session_id"):
                source = "file"
            elif result.get("metadata") and isinstance(result["metadata"], dict) and len(result["metadata"]) > 0:
                source = "message"
            else:
                source = "card"

            return json.dumps(
                {
                    "success": True,
                    "source_id": source_id,
                    "content": result.get("content", ""),
                    "source": source,
                    "metadata": result.get("metadata", {}),
                },
                ensure_ascii=False,
            )

        # 3. Fallback: try handbook lookup for bare keys
        from ..handbook import HandbookManager

        from ._scope import current_scope, handbook_manager
        h = handbook_manager(current_scope())
        entry = h.get(source_id)
        if entry is not None:
            return json.dumps(
                {
                    "success": True,
                    "source_id": source_id,
                    "content": entry.get("content", ""),
                    "source": "handbook",
                    "metadata": {
                        "title": entry.get("title", ""),
                        "tags": entry.get("tags", []),
                        "updated_at": entry.get("updated_at", ""),
                    },
                },
                ensure_ascii=False,
            )

        # 4. Fallback: try reading card file from disk
        try:
            from ..card_store import DeepStore
            from ..config import resolve_config
            _cfg = kw.get("effective_config")
            if _cfg is None:
                # A supplied core already carries its own booted config.
                _cfg = getattr(core, "config", None)
            if _cfg is None:
                if _declared_runtime:
                    # A runtime was declared but no config can be resolved
                    # without falling back to the DEFAULT profile, which the
                    # caller did not declare. Fail closed, same shape as the
                    # entry guard.
                    return json.dumps(
                        {
                            "success": False,
                            "source_id": source_id,
                            "error_code": SCOPE_UNAVAILABLE,
                            "code": SCOPE_UNAVAILABLE,
                            "error": "hm read scope unavailable: "
                            "effective config is missing",
                        },
                        ensure_ascii=False,
                    )
                _cfg = resolve_config()
            _store = DeepStore(_cfg)
            # Try with .md extension first, then without
            _card = _store.read_card(source_id + ".md")
            if _card is None:
                _card = _store.read_card(source_id)
            if _card is not None:
                return json.dumps(
                    {
                        "success": True,
                        "source_id": source_id,
                        "content": _card.content or "",
                        "source": "card",
                        "metadata": {
                            "title": _card.title or "",
                            "tags": _card.tags or [],
                            "category": _card.category or "",
                        },
                    },
                    ensure_ascii=False,
                )
        except Exception:
            pass

        # 5. All sources exhausted
        return json.dumps(
            {
                "success": False,
                "source_id": source_id,
                "error": f"source_id not found: {source_id}",
            },
            ensure_ascii=False,
        )

    except Exception as e:
        return json.dumps(
            {"success": False, "error": _safe_err(e)},
            ensure_ascii=False,
        )


# ── hm_status ────────────────────────────────────────────────────────────────

HM_STATUS_SCHEMA = {
    "name": "hm_status",
    "description": "[3-读取] 海马系统健康 status — check handbook entries, cards directory, and PG availability.",
    "parameters": {"type": "object", "properties": {}},
}


def handle_hm_status(args: dict, **kw) -> str:
    """Simple health status: handbook entries, cards directory, PG availability."""
    result = {"success": True, "checks": {}}

    # 1. Handbook
    try:
        from ..handbook import HandbookManager

        from ._scope import current_scope, handbook_manager
        h = handbook_manager(current_scope())
        entries = h.list_all()
        result["checks"]["handbook"] = {
            "available": True,
            "entry_count": len(entries),
        }
    except Exception as e:
        result["checks"]["handbook"] = {
            "available": False,
            "error": _safe_err(e)[:200],
        }

    # 2. Cards — SQLite 统计
    try:
        from ..sqlite_store import SqliteCardStore

        cfg = kw.get("effective_config")
        if isinstance(cfg, dict):
            configured_base = cfg.get("basePath", "")
        else:
            configured_base = getattr(cfg, "base_path", "") if cfg is not None else ""
        base = Path(configured_base or Path.home() / ".v3-core" / "profiles" / "default")
        store = SqliteCardStore(base)
        st = store.status()
        result["checks"]["cards"] = {
            "available": True,
            "total_cards": st["total_cards"],
            "categories": list(st["by_category"].keys()),
        }
    except Exception as e:
        result["checks"]["cards"] = {
            "available": False,
            "error": _safe_err(e)[:200],
        }

    # 3. PG availability — 当前数据面探针
    try:
        pool = kw.get("pool") if kw.get("pool") is not None else kw.get("pg_pool")
        if pool is not None:
            from ..config import resolve_config
            try:
                from .status import _probe_pg
            except (ImportError, AttributeError):
                from v3core.tools.status import _probe_pg

            cfg = kw.get("effective_config")
            if cfg is None:
                cfg = resolve_config()
            pg_cfg = (cfg.get("storage", {}) or {}).get("pg", {}) or {} if isinstance(cfg, dict) else {}
            pg_status = _probe_pg(pg_cfg, pool=pool)
            result["checks"]["pg"] = {
                "connected": bool(pg_status.get("pg_connected")),
                "counts": pg_status.get("table_counts", {}),
                "probed_tables": pg_status.get("probed_tables", []),
                "missing_tables": pg_status.get("missing_tables", []),
                "error": pg_status.get("error"),
            }
        else:
            if kw.get("runtime_context"):
                result["checks"]["pg"] = {
                    "connected": False,
                    "error": "Runtime-backed hm_status requires its PgPool owner",
                }
                return json.dumps(result, ensure_ascii=False)
            from ..config import resolve_config
            from ..pg_store import PgEmbedStore

            cfg = kw.get("effective_config")
            if cfg is None:
                cfg = resolve_config()
            pg = PgEmbedStore(config=cfg)
            conn = pg._connect()
            if conn:
                cur = conn.cursor()
                counts = {}
                probed = []
                missing = []
                current_tables = (
                    "topics", "conversation_stream", "qa_pairs",
                    "topic_entries", "observation_notes", "yin_paragraphs",
                )
                for table in current_tables:
                    try:
                        cur.execute(f"SELECT count(*) FROM {table}")
                        row = cur.fetchone()
                        counts[table] = int(row[0]) if row else 0
                        probed.append(table)
                    except Exception:
                        missing.append(table)
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                result["checks"]["pg"] = {
                    "connected": not missing,
                    "counts": counts,
                    "probed_tables": probed,
                    "missing_tables": missing,
                    "error": ("missing/unreadable current tables: " + ", ".join(missing)) if missing else None,
                }
            else:
                result["checks"]["pg"] = {"connected": False, "error": "PG not connected"}
    except Exception as e:
        result["checks"]["pg"] = {"connected": False, "error": _safe_err(e)[:200]}

    # 4. Overall health — handbook/cards 是信息段，不能掩盖 PG 断开。
    pg_check = result["checks"].get("pg", {})
    result["healthy"] = bool(pg_check.get("connected") and pg_check.get("probed_tables"))
    if not result["healthy"]:
        result["error"] = "PG current data plane unavailable"

    return json.dumps(result, ensure_ascii=False)
