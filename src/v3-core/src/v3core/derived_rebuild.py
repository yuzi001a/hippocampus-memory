# -*- coding: utf-8 -*-
"""v3core.derived_rebuild — M03 local, on-demand rebuild of ONE stale Topic.

Scope (docs/M03-CORRECTION-PROPAGATION.md §"Rebuild", §19-§21, §31-§32;
docs/M03-DERIVED-LINEAGE-MAP.md):

    CORRECTION FIRST  →  INVALIDATION SECOND  →  REBUILD THIRD (here)

A committed M01/M02 correction ``A -> B`` makes every derived artifact that was
built from A permanently stale (the M03 invalidation sidecar). This module is
the THIRD stage: it locally regenerates ONE stale Topic as a NEW artifact ``T2``
and records ``replacement_derived_id = T2`` on the old sidecar row.

WHAT IT IS NOT
--------------
* **Not a full historical rebuild.** Work is bounded to the sidecar rows the
  caller named (one ``correction_id`` / one ``invalidation_id``). No full-corpus
  re-embedding, no Observer rerun, no E1 rerun.
* **Not a raw-source rewrite.** ``conversation_stream`` / ``qa_pairs`` /
  ``explicit_memories`` are READ ONLY here and are never written. Nothing is
  ever DELETEd — the old topic ``T`` keeps its body verbatim and stays readable
  by id / history / source trace; it simply stays suppressed.
* **Not a second source resolver.** The correction state is read through the
  canonical M02 reader (:class:`v3core.active_memory_store.ActiveMemoryReader`,
  ``read_version`` → the chain walk), exactly as the invalidation layer does.

SUPPORTED DERIVED KINDS (stated up front, task book §"Scope for this round")
---------------------------------------------------------------------------
    topic          = complete  (this module)
    observer_note  = pending / manual → ``REBUILD_NOT_SUPPORTED``
    yin_paragraph  = pending / manual → ``REBUILD_NOT_SUPPORTED``

Observer notes and Yin paragraphs are still invalidated and suppressed by the
propagation / suppression layers; their missing rebuild is reported truthfully
here rather than hidden, and no architecture is rewritten for them.

GENERATION CONTEXT IS ASSEMBLED FROM THE CANONICAL CORRECTION STATE
------------------------------------------------------------------
The provider never sees "the old raw A alone". The context handed to it states,
explicitly and structurally:

    source_memory       A  state = "historical/superseded"
    replacement_memory  B  state = "current"
    correction          correction_id / relation_id / relation_type / A -> B

plus A's and B's real payloads (read from the canonical chain walk) and the
original source identity from the sidecar. Re-deriving A as ``current`` from the
old history is therefore structurally impossible: A is labelled superseded and
the current payload is B's.

REBUILT ARTIFACT IDENTITY (deterministic)
-----------------------------------------
::

    T2 = 't_' + md5(f"{T.topic_id}|rebuild|{correction_id}")[:8]

Deterministic so a repeated rebuild of the SAME correction can never collide
with ``T`` and can never produce a second artifact: the id is recomputable from
``(T, correction_id)`` alone, so a replay finds the same ``T2`` and reports
``REBUILD_DEDUPLICATED`` with zero new rows.

WHERE T2 CARRIES ITS LINEAGE
----------------------------
``public.topics`` has NO metadata / provenance column (M03-DERIVED-LINEAGE-MAP
§2.1), so T2's traceable chain is persisted in the fields it does have:

    topics.note_ref  = 'rebuild://<correction_id>'      (cross-ref convention,
                                                         mirrors 'obs://<id>')
    topics.body      = <generated body> + a trailing machine-readable marker
                       '<!-- m03-rebuild-lineage: <compact JSON> -->'

and the full chain is ALSO returned in the receipt (``lineage``):

    T2 → rebuild_from_correction → correction_id → A → B → original source(s)

SUPPRESSION IS PERMANENT; ``state`` IS LIFECYCLE ONLY
-----------------------------------------------------
Writing ``state='rebuilt'`` does NOT lift the suppression of ``T``. Suppression
is driven by the mere EXISTENCE of the sidecar row (see
``derived_invalidation.invalidated_ids``). ``T``'s content is still the
superseded derivation, so it stays suppressed forever; only ``T2`` — a different
id — is admissible:

    old T stale / new T2 active / replacement_derived_id = T2
    current recall: T2 allowed, T suppressed

FAIL-CLOSED AND HONEST
----------------------
* Provider unavailable (no configured credential / provider raised / empty
  output) → ``REBUILD_PROVIDER_UNAVAILABLE`` or ``REBUILD_FAILED``; the sidecar
  row is left EXACTLY as it was, NO new artifact is produced, NO text is
  invented, ``T`` stays suppressed, and the correction ``A -> B`` is never
  rolled back.
* Missing sidecar table → truthful ``MIGRATION_REQUIRED``.
* A withdrawal (no current replacement) → ``REBUILD_NO_REPLACEMENT``: there is
  nothing to rebuild FROM and no "not A" text is invented.
* ``dry_run=True`` is strictly read-only.

Transaction ownership mirrors ``derived_invalidation``: the new topic and the
sidecar update are written in ONE own transaction on a leased connection (never
inside an M02 correction transaction), with the autocommit baseline restored
before the lease is released.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from . import active_memory_store as _canonical
from .derived_invalidation import (
    DERIVED_KIND_OBSERVER_NOTE,
    DERIVED_KIND_TOPIC,
    DERIVED_KIND_YIN_PARAGRAPH,
    STATE_REBUILT,
    _INVALIDATION_TABLE,
    _is_missing_table,
    _lease,
)

logger = logging.getLogger("v3core.derived_rebuild")

__all__ = [
    "REBUILD_APPLIED",
    "REBUILD_DEDUPLICATED",
    "REBUILD_NOT_SUPPORTED",
    "REBUILD_PROVIDER_UNAVAILABLE",
    "REBUILD_FAILED",
    "REBUILD_NO_REPLACEMENT",
    "REBUILD_NO_INVALIDATION",
    "REBUILD_MIGRATION_REQUIRED",
    "REBUILD_INVALID_REQUEST",
    "REBUILD_STATE_APPLIED",
    "REBUILD_STATE_DEDUPLICATED",
    "REBUILD_STATE_PREVIEW",
    "REBUILD_STATE_PENDING",
    "REBUILD_STATE_NOT_SUPPORTED",
    "REBUILD_STATE_NONE",
    "REBUILDABLE_KINDS",
    "rebuilt_topic_id",
    "rebuild_topic",
]

# ── receipt status codes ──────────────────────────────────────────────────

REBUILD_APPLIED = "REBUILD_APPLIED"
REBUILD_DEDUPLICATED = "REBUILD_DEDUPLICATED"
REBUILD_NOT_SUPPORTED = "REBUILD_NOT_SUPPORTED"
REBUILD_PROVIDER_UNAVAILABLE = "REBUILD_PROVIDER_UNAVAILABLE"
REBUILD_FAILED = "REBUILD_FAILED"
REBUILD_NO_REPLACEMENT = "REBUILD_NO_REPLACEMENT"
REBUILD_NO_INVALIDATION = "REBUILD_NO_INVALIDATION"
REBUILD_MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
REBUILD_INVALID_REQUEST = "INVALID_REQUEST"

# ── receipt ``rebuild_state`` values (lifecycle of THIS call) ─────────────

REBUILD_STATE_APPLIED = "rebuilt"
REBUILD_STATE_DEDUPLICATED = "deduplicated"
REBUILD_STATE_PREVIEW = "preview"
REBUILD_STATE_PENDING = "pending"
REBUILD_STATE_NOT_SUPPORTED = "not_supported"
REBUILD_STATE_NONE = "none"

#: Only ``topic`` has a working rebuild this round (task book §"Scope").
REBUILDABLE_KINDS = (DERIVED_KIND_TOPIC,)

#: The derived kinds that are invalidated/suppressed but whose rebuild is
#: explicitly pending/manual — reported, never architected around.
_PENDING_KINDS = (DERIVED_KIND_OBSERVER_NOTE, DERIVED_KIND_YIN_PARAGRAPH)

_A_STATE = "historical/superseded"
_B_STATE = "current"

_TOPICS_TABLE = "public.topics"
_RELATION_TABLE = "public.memory_relations"

_SIDECAR_COLUMNS = (
    "invalidation_id", "correction_id", "relation_id", "derived_kind",
    "derived_id", "source_memory_id", "replacement_memory_id", "state",
    "canonical_qa_id", "input_source_kind", "input_source_id",
    "created_at", "resolved_at", "replacement_derived_id",
)

_TOPIC_COLUMNS = (
    "topic_id", "title", "summary", "body", "keywords", "note_ref", "status",
)

_LINEAGE_MARKER_PREFIX = "<!-- m03-rebuild-lineage: "
_LINEAGE_MARKER_SUFFIX = " -->"

_SYSTEM_PROMPT = (
    "You rebuild ONE derived Topic after an explicit user correction. "
    "The correction is authoritative: the memory labelled "
    "'historical/superseded' is NO LONGER the current answer, and the memory "
    "labelled 'current' IS. Never restate the superseded memory as current, "
    "never summarise it as the conclusion, and never invent facts that are "
    "not in the current memory or the original sources. Return the rebuilt "
    "topic as JSON with keys title / summary / body / keywords."
)


class _MissingSidecarTable(RuntimeError):
    """The M03 invalidation sidecar (or a required relation) is absent."""


class _ProviderUnavailable(RuntimeError):
    """No usable provider could be constructed / reached."""


class _RebuildStateError(RuntimeError):
    """The canonical correction state could not be read truthfully."""


# ── deterministic identity ────────────────────────────────────────────────


def rebuilt_topic_id(topic_id: Any, correction_id: Any) -> str:
    """``'t_' + md5('{topic_id}|rebuild|{correction_id}')[:8]``.

    Deterministic and recomputable from ``(T, correction_id)`` alone: it can
    never collide with ``T`` and a replay of the same correction always lands on
    the SAME ``T2`` (so a rebuild is idempotent by construction).
    """
    raw = f"{topic_id}|rebuild|{correction_id}"
    return "t_" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


# ── receipt ───────────────────────────────────────────────────────────────


def _empty_counts() -> dict:
    return {
        "requested": 0, "rebuilt": 0, "deduplicated": 0,
        "unsupported": 0, "pending": 0, "failed": 0,
    }


def _receipt(
    *,
    status: str,
    success: bool,
    rebuild_state: str,
    correction_id: Optional[str] = None,
    invalidation_id: Optional[str] = None,
    derived_kind: Optional[str] = None,
    derived_id: Optional[str] = None,
    source_memory_id: Optional[str] = None,
    replacement_memory_id: Optional[str] = None,
    new_derived_id: Optional[str] = None,
    generation_context: Optional[dict] = None,
    lineage: Optional[dict] = None,
    counts: Optional[dict] = None,
    results: Optional[list] = None,
    dry_run: bool = False,
    would_rebuild: bool = False,
    error: Optional[str] = None,
) -> dict:
    return {
        "status": status,
        "success": success,
        "rebuild_state": rebuild_state,
        "correction_id": correction_id,
        "invalidation_id": invalidation_id,
        "derived_kind": derived_kind,
        "derived_id": derived_id,
        "source_memory_id": source_memory_id,
        "replacement_memory_id": replacement_memory_id,
        "new_derived_id": new_derived_id,
        "replacement_derived_id": new_derived_id,
        "generation_context": generation_context,
        "lineage": lineage,
        "counts": counts if counts is not None else _empty_counts(),
        "results": results if results is not None else [],
        "dry_run": bool(dry_run),
        "would_rebuild": bool(would_rebuild),
        "error": error,
    }


def _result(
    *,
    status: str,
    success: bool,
    rebuild_state: str,
    row: Optional[dict] = None,
    new_derived_id: Optional[str] = None,
    generation_context: Optional[dict] = None,
    lineage: Optional[dict] = None,
    would_rebuild: bool = False,
    error: Optional[str] = None,
) -> dict:
    row = row or {}
    return {
        "status": status,
        "success": success,
        "rebuild_state": rebuild_state,
        "invalidation_id": row.get("invalidation_id"),
        "correction_id": row.get("correction_id"),
        "derived_kind": row.get("derived_kind"),
        "derived_id": row.get("derived_id"),
        "source_memory_id": row.get("source_memory_id"),
        "replacement_memory_id": row.get("replacement_memory_id"),
        "new_derived_id": new_derived_id,
        "generation_context": generation_context,
        "lineage": lineage,
        "would_rebuild": bool(would_rebuild),
        "error": error,
    }


# ── reads (sidecar / topic / correction edge) ─────────────────────────────


def _read_sidecar_rows(
    pg: Any, *, correction_id: Optional[str] = None,
    invalidation_id: Optional[str] = None,
) -> list:
    """Read the invalidation rows the caller named, verbatim.

    Raises :class:`_MissingSidecarTable` when the sidecar is absent so a
    missing migration can never be mistaken for "nothing to do".
    """
    clauses: list = []
    params: list = []
    if invalidation_id is not None:
        clauses.append("invalidation_id = %s")
        params.append(invalidation_id)
    if correction_id is not None:
        clauses.append("correction_id = %s")
        params.append(correction_id)
    if not clauses:
        return []
    sql = (
        f"SELECT {', '.join(_SIDECAR_COLUMNS)} FROM {_INVALIDATION_TABLE} "
        f"WHERE {' AND '.join(clauses)} ORDER BY derived_kind, derived_id"
    )
    try:
        with _lease(pg) as conn:
            cur = conn.cursor()
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    except Exception as exc:
        if _is_missing_table(exc):
            raise _MissingSidecarTable(str(exc)) from exc
        raise
    out: list = []
    for raw in rows:
        if isinstance(raw, dict):
            row = dict(raw)
        else:
            row = dict(zip(_SIDECAR_COLUMNS, raw))
        out.append(row)
    return out


def _read_topic(pg: Any, topic_id: str) -> Optional[dict]:
    """One real ``public.topics`` row, or None. Read only."""
    with _lease(pg) as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(_TOPIC_COLUMNS)} FROM {_TOPICS_TABLE} "
            f"WHERE topic_id = %s",
            (topic_id,),
        )
        raw = cur.fetchone()
    if raw is None:
        return None
    if isinstance(raw, dict):
        return dict(raw)
    return dict(zip(_TOPIC_COLUMNS, raw))


def _read_correction_edge(pg: Any, correction_id: str) -> Optional[dict]:
    with _lease(pg) as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT relation_id, relation_type, from_memory_id, to_memory_id "
            f"FROM {_RELATION_TABLE} WHERE correction_id = %s",
            (correction_id,),
        )
        raw = cur.fetchone()
    if raw is None:
        return None
    return {
        "relation_id": raw[0],
        "relation_type": raw[1],
        "from_memory_id": raw[2],
        "to_memory_id": raw[3],
    }


# ── canonical correction state ────────────────────────────────────────────


def _read_canonical_state(pg: Any, source_memory_id: str) -> dict:
    """The authoritative ``A -> B`` state, via the canonical M02 reader.

    Returns ``{'source': {...A...}, 'current': {...B...}, 'versions': [...]}``
    where each memory carries its canonical chain ``label``. A is labelled
    ``superseded`` and B ``current`` — the labels come from the ONE canonical
    chain walk (``read_version`` → ``read_chain``), never from a second
    traversal and never from text/embedding guessing.
    """
    reader = _canonical.ActiveMemoryReader(pool=None, pg=pg)
    version = reader.read_version(source_memory_id, mode="history")
    if not isinstance(version, dict) or not version.get("success"):
        raise _RebuildStateError(
            f"读取 {source_memory_id} 的纠正链失败: "
            f"{version.get('error') if isinstance(version, dict) else version!r}"
        )
    history = version.get("history") or {}
    versions = history.get("versions") or []
    by_id: dict = {}
    for item in versions:
        mid = item.get("memory_id")
        if mid is not None:
            by_id[str(mid)] = item

    requested = version.get("requested") or {}
    current_id = history.get("current_memory_id")
    current = by_id.get(str(current_id)) if current_id is not None else None

    source = by_id.get(str(source_memory_id))
    if source is None:
        # The requested row is always part of its own chain, but never assume:
        # fall back to the exact requested payload the reader returned.
        source = dict(requested)
        source.setdefault("memory_id", source_memory_id)
        source.setdefault("label", requested.get("status") or "archived")

    return {
        "source": source,
        "current": current,
        "current_memory_id": current_id,
        "versions": versions,
    }


def _read_memory_row(pg: Any, memory_id: str) -> Optional[dict]:
    """Exact single-row read through the canonical reader (read only)."""
    reader = _canonical.ActiveMemoryReader(pool=None, pg=pg)
    return reader.get_by_memory_id(memory_id)


# ── generation context ────────────────────────────────────────────────────


def _memory_view(item: Optional[dict], memory_id: str, state: str) -> dict:
    item = item or {}
    return {
        "memory_id": memory_id,
        "title": item.get("title"),
        "content": item.get("content"),
        "status": item.get("status"),
        "label": item.get("label"),
        "state": state,
    }


def _build_generation_context(
    *,
    topic_id: str,
    old_topic: Optional[dict],
    rebuilt_id: str,
    correction_id: str,
    edge: Optional[dict],
    source_view: dict,
    current_view: dict,
    sidecar_row: dict,
) -> dict:
    """The EXACT context handed to the provider.

    A is explicitly ``historical/superseded``; B is explicitly ``current``; the
    correction relation is explicit. The provider can therefore never be asked
    to re-derive A as current from the old raw history.
    """
    edge = edge or {}
    relation = {
        "correction_id": correction_id,
        "relation_id": edge.get("relation_id") or sidecar_row.get("relation_id"),
        "relation_type": edge.get("relation_type"),
        "from_memory_id": edge.get("from_memory_id") or sidecar_row.get(
            "source_memory_id"
        ),
        "to_memory_id": edge.get("to_memory_id") or sidecar_row.get(
            "replacement_memory_id"
        ),
    }
    canonical_qa_id = sidecar_row.get("canonical_qa_id")
    original_sources = {
        "canonical_qa_id": canonical_qa_id,
        "input_source_kind": sidecar_row.get("input_source_kind"),
        "input_source_id": sidecar_row.get("input_source_id"),
    }
    lineage = _build_lineage(
        topic_id=topic_id,
        rebuilt_id=rebuilt_id,
        correction_id=correction_id,
        relation=relation,
        source_memory_id=source_view.get("memory_id"),
        current_memory_id=current_view.get("memory_id"),
        original_sources=original_sources,
    )
    return {
        "task": "m03_rebuild_topic",
        "instruction": (
            "Rebuild this Topic from the CURRENT correction state. The memory "
            "labelled 'historical/superseded' is superseded and must not be "
            "presented as the current answer; the memory labelled 'current' is "
            "the authoritative one."
        ),
        "topic_id": topic_id,
        "old_topic": {
            "topic_id": topic_id,
            "title": (old_topic or {}).get("title"),
            "summary": (old_topic or {}).get("summary"),
            "body": (old_topic or {}).get("body"),
            "state": _A_STATE,
        },
        "correction": relation,
        "correction_relation": dict(relation),
        "source_memory": source_view,
        "replacement_memory": current_view,
        "original_sources": original_sources,
        "lineage": lineage,
    }


def _build_lineage(
    *,
    topic_id: str,
    rebuilt_id: str,
    correction_id: str,
    relation: dict,
    source_memory_id: Optional[str],
    current_memory_id: Optional[str],
    original_sources: dict,
) -> dict:
    """``T2 → rebuild from correction → correction_id → A → B → source(s)``."""
    qa = original_sources.get("canonical_qa_id")
    chain = [rebuilt_id, "rebuild_from_correction", correction_id,
             source_memory_id, current_memory_id]
    if qa is not None:
        chain.append(f"qa_pairs:{qa}")
    return {
        "derived_kind": DERIVED_KIND_TOPIC,
        "derived_id": topic_id,
        "rebuilt_derived_id": rebuilt_id,
        "rebuild_from_correction": correction_id,
        "relation_id": relation.get("relation_id"),
        "relation_type": relation.get("relation_type"),
        "source_memory_id": source_memory_id,
        "replacement_memory_id": current_memory_id,
        "canonical_qa_id": qa,
        "input_source_kind": original_sources.get("input_source_kind"),
        "input_source_id": original_sources.get("input_source_id"),
        "original_sources": [
            f"qa_pairs:{qa}" if qa is not None else None
        ],
        "chain": chain,
    }


def _render_context(context: dict) -> str:
    return json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)


# ── provider ──────────────────────────────────────────────────────────────


def _default_provider() -> Any:
    """The project's configured LLM path — no hardcoded endpoint/credential."""
    try:
        from .config import resolve_config
        from .llm import LLMClient

        return LLMClient(resolve_config())
    except Exception as exc:  # noqa: BLE001
        raise _ProviderUnavailable(
            f"无法构造默认 LLM provider（配置/凭据缺失）: {exc!r}"
        ) from exc


def _invoke_provider(provider: Any, context: dict) -> tuple:
    """``(title, summary, body, keywords, error_code, error_text)``.

    ``provider`` is injectable: a plain callable receives the context dict; an
    object exposing ``generate``/``chat`` is used through that method. The
    default (``None``) goes through the project's configured LLM client.
    """
    if provider is None:
        try:
            provider = _default_provider()
        except _ProviderUnavailable as exc:
            return None, None, None, [], REBUILD_PROVIDER_UNAVAILABLE, str(exc)

    try:
        if callable(provider):
            out = provider(context)
        elif hasattr(provider, "generate"):
            out = provider.generate(context)
        elif hasattr(provider, "chat"):
            out = provider.chat(
                _SYSTEM_PROMPT,
                [{"role": "user", "content": _render_context(context)}],
                temperature=0.3,
            )
        else:
            raise _ProviderUnavailable(
                f"provider 不可用: {type(provider).__name__} 既不可调用也无 "
                f"generate/chat"
            )
    except _ProviderUnavailable as exc:
        return None, None, None, [], REBUILD_PROVIDER_UNAVAILABLE, str(exc)
    except Exception as exc:  # noqa: BLE001
        return None, None, None, [], REBUILD_FAILED, f"provider 调用失败: {exc!r}"

    if isinstance(out, dict):
        title = out.get("title")
        summary = out.get("summary")
        body = out.get("body")
        if body is None:
            body = out.get("content")
        keywords = out.get("keywords") or []
    elif isinstance(out, str):
        title, summary, body, keywords = None, None, out, []
    else:
        return None, None, None, [], REBUILD_FAILED, (
            f"provider 返回了不可用的结果类型: {type(out).__name__}"
        )

    if not isinstance(body, str) or not body.strip():
        # Never invent text: an empty generation is a truthful failure.
        return None, None, None, [], REBUILD_FAILED, "provider 返回空内容"

    if not isinstance(title, str) or not title.strip():
        title = None
    if not isinstance(summary, str):
        summary = None
    if isinstance(keywords, (str, bytes)) or not isinstance(keywords, (list, tuple)):
        keywords = []
    keywords = [str(k) for k in keywords if isinstance(k, str) and k.strip()]
    return title, summary, body, keywords, None, None


# ── writes (ONE own transaction) ──────────────────────────────────────────


def _write_rebuilt(
    pg: Any, *, row: dict, new_topic_id: str, title: str, summary: str,
    body: str, keywords: list, note_ref: str,
) -> tuple:
    """Insert T2 and advance the sidecar row in ONE own transaction.

    Returns ``(inserted, sidecar_updated)``. ``ON CONFLICT (topic_id) DO
    NOTHING`` keeps the artifact write idempotent; the sidecar UPDATE records
    the replacement and stamps ``resolved_at``. Suppression of ``T`` is
    unaffected — it is driven by the row's existence, not by ``state``.
    """
    lease = _canonical._acquire_lease(*_canonical._lease_slots(pg))
    conn = lease.connection
    took_txn_ownership = False
    inserted = False
    updated = 0
    try:
        try:
            if getattr(conn, "autocommit", False) is True:
                conn.autocommit = False
                took_txn_ownership = True
            cur = conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.execute(
                    f"""
                    INSERT INTO {_TOPICS_TABLE}
                        (topic_id, title, summary, body, keywords, note_ref,
                         status, created_at, updated_at)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, 'active', NOW(), NOW())
                    ON CONFLICT (topic_id) DO NOTHING
                    RETURNING topic_id
                    """,
                    (new_topic_id, title, summary, body, list(keywords), note_ref),
                )
                inserted = cur.fetchone() is not None
                cur.execute(
                    f"""
                    UPDATE {_INVALIDATION_TABLE}
                       SET state = %s,
                           resolved_at = NOW(),
                           replacement_derived_id = %s
                     WHERE invalidation_id = %s
                    """,
                    (STATE_REBUILT, new_topic_id, row.get("invalidation_id")),
                )
                updated = cur.rowcount or 0
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception as rollback_exc:  # pragma: no cover
                    logger.warning(
                        "derived_rebuild 重建事务 rollback 失败: %r",
                        rollback_exc,
                    )
                raise
        finally:
            if took_txn_ownership:
                try:
                    conn.autocommit = True
                except Exception as restore_exc:  # pragma: no cover
                    logger.warning(
                        "恢复 autocommit 基线失败: %r", restore_exc,
                    )
            lease.close()
    except Exception as exc:
        if _is_missing_table(exc):
            raise _MissingSidecarTable(str(exc)) from exc
        raise
    return inserted, updated


# ── one row ───────────────────────────────────────────────────────────────


def _rebuild_one(
    pg: Any, row: dict, *, dry_run: bool, provider: Any,
) -> dict:
    kind = row.get("derived_kind")
    derived_id = str(row.get("derived_id"))

    if kind not in REBUILDABLE_KINDS:
        # observer_note / yin_paragraph are pending/manual this round. They are
        # reported truthfully and nothing is written.
        if kind in _PENDING_KINDS:
            reason = f"{kind} 的重建为 pending/manual（本轮仅支持 topic）"
        else:
            reason = f"derived_kind={kind!r} 不支持重建"
        return _result(
            status=REBUILD_NOT_SUPPORTED, success=False,
            rebuild_state=REBUILD_STATE_NOT_SUPPORTED, row=row, error=reason,
        )

    correction_id = row.get("correction_id")
    if not isinstance(correction_id, str) or not correction_id.strip():
        return _result(
            status=REBUILD_FAILED, success=False,
            rebuild_state=REBUILD_STATE_NONE, row=row,
            error="侧车行缺少 correction_id；无法定位纠正状态",
        )

    source_memory_id = row.get("source_memory_id")
    if not isinstance(source_memory_id, str) or not source_memory_id.strip():
        return _result(
            status=REBUILD_FAILED, success=False,
            rebuild_state=REBUILD_STATE_NONE, row=row,
            error="侧车行缺少 source_memory_id（A）；无法组装纠正上下文",
        )

    new_topic_id = rebuilt_topic_id(derived_id, correction_id)

    # ── idempotency: an already-rebuilt row returns the SAME artifact ──
    recorded = row.get("replacement_derived_id")
    if isinstance(recorded, str) and recorded.strip():
        return _result(
            status=REBUILD_DEDUPLICATED, success=True,
            rebuild_state=REBUILD_STATE_DEDUPLICATED, row=row,
            new_derived_id=recorded,
        )

    try:
        old_topic = _read_topic(pg, derived_id)
    except _MissingSidecarTable:
        raise
    except Exception as exc:  # noqa: BLE001
        return _result(
            status=REBUILD_FAILED, success=False,
            rebuild_state=REBUILD_STATE_NONE, row=row,
            error=f"读取派生件 {derived_id} 失败: {exc!r}",
        )
    try:
        existing = _read_topic(pg, new_topic_id)
    except Exception as exc:  # noqa: BLE001
        return _result(
            status=REBUILD_FAILED, success=False,
            rebuild_state=REBUILD_STATE_NONE, row=row,
            error=f"检查 {new_topic_id} 是否已存在失败: {exc!r}",
        )
    if existing is not None:
        return _result(
            status=REBUILD_DEDUPLICATED, success=True,
            rebuild_state=REBUILD_STATE_DEDUPLICATED, row=row,
            new_derived_id=new_topic_id,
        )

    # ── canonical correction state ──
    try:
        edge = _read_correction_edge(pg, correction_id)
    except _MissingSidecarTable:
        raise
    except Exception as exc:  # noqa: BLE001
        return _result(
            status=REBUILD_FAILED, success=False,
            rebuild_state=REBUILD_STATE_NONE, row=row,
            error=f"读取纠正关系失败: {exc!r}",
        )
    try:
        state = _read_canonical_state(pg, source_memory_id)
    except _RebuildStateError as exc:
        return _result(
            status=REBUILD_FAILED, success=False,
            rebuild_state=REBUILD_STATE_NONE, row=row,
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return _result(
            status=REBUILD_FAILED, success=False,
            rebuild_state=REBUILD_STATE_NONE, row=row,
            error=f"读取规范纠正状态失败: {exc!r}",
        )

    source_item = state.get("source")
    current_item = state.get("current")
    current_id = state.get("current_memory_id")
    if current_item is None and current_id is None:
        current_id = row.get("replacement_memory_id")
        if isinstance(current_id, str) and current_id.strip():
            try:
                current_item = _read_memory_row(pg, current_id)
            except Exception:  # noqa: BLE001
                current_item = None
    if not current_id or current_item is None:
        # A withdrawal (or a chain that now terminates with nothing current):
        # there is nothing to rebuild FROM and no "not A" text is invented.
        return _result(
            status=REBUILD_NO_REPLACEMENT, success=False,
            rebuild_state=REBUILD_STATE_PENDING, row=row,
            error="没有当前替代版本（撤回链）；无重建输入，绝不编造内容",
        )

    source_view = _memory_view(source_item, source_memory_id, _A_STATE)
    current_view = _memory_view(current_item, str(current_id), _B_STATE)
    context = _build_generation_context(
        topic_id=derived_id,
        old_topic=old_topic,
        rebuilt_id=new_topic_id,
        correction_id=correction_id,
        edge=edge,
        source_view=source_view,
        current_view=current_view,
        sidecar_row=row,
    )
    lineage = context["lineage"]

    if dry_run:
        return _result(
            status=REBUILD_APPLIED, success=True,
            rebuild_state=REBUILD_STATE_PREVIEW, row=row,
            new_derived_id=new_topic_id, generation_context=context,
            lineage=lineage, would_rebuild=True,
        )

    title, summary, body, keywords, error_code, error_text = _invoke_provider(
        provider, context
    )
    if error_code is not None:
        # The sidecar row is left EXACTLY as it was; no artifact is produced;
        # T stays suppressed; the correction is never rolled back.
        return _result(
            status=error_code, success=False,
            rebuild_state=REBUILD_STATE_PENDING, row=row,
            generation_context=context, lineage=lineage, error=error_text,
        )

    if title is None:
        title = (old_topic or {}).get("title") or derived_id
    if summary is None:
        summary = ""

    marker = (
        _LINEAGE_MARKER_PREFIX
        + json.dumps(lineage, ensure_ascii=False, sort_keys=True, default=str)
        + _LINEAGE_MARKER_SUFFIX
    )
    stored_body = f"{body.rstrip()}\n\n{marker}"
    note_ref = f"rebuild://{correction_id}"

    try:
        _write_rebuilt(
            pg, row=row, new_topic_id=new_topic_id, title=title,
            summary=summary, body=stored_body, keywords=keywords,
            note_ref=note_ref,
        )
    except _MissingSidecarTable:
        raise
    except Exception as exc:  # noqa: BLE001
        return _result(
            status=REBUILD_FAILED, success=False,
            rebuild_state=REBUILD_STATE_PENDING, row=row,
            generation_context=context, lineage=lineage,
            error=f"写入重建件/侧车失败: {exc!r}",
        )

    return _result(
        status=REBUILD_APPLIED, success=True,
        rebuild_state=REBUILD_STATE_APPLIED, row=row,
        new_derived_id=new_topic_id, generation_context=context,
        lineage=lineage,
    )


# ── public entry ──────────────────────────────────────────────────────────


def rebuild_topic(
    pg: Any, *, correction_id: Optional[str] = None,
    invalidation_id: Optional[str] = None, dry_run: bool = False,
    provider: Any = None,
) -> dict:
    """Locally rebuild the stale Topic(s) of one committed correction.

    Named by ``correction_id`` and/or ``invalidation_id``; only
    ``derived_kind='topic'`` is rebuilt — observer notes and Yin paragraphs
    truthfully return ``REBUILD_NOT_SUPPORTED``.

    A successful rebuild produces a NEW topic ``T2`` (never rewriting ``T``) and
    records ``state='rebuilt'`` / ``resolved_at`` / ``replacement_derived_id``
    on the old sidecar row. ``T`` stays suppressed forever: suppression is
    driven by the row's existence, not by ``state``.

    Provider unavailable or failing → ``REBUILD_PROVIDER_UNAVAILABLE`` /
    ``REBUILD_FAILED`` with the sidecar untouched, no artifact, no invented
    text and the correction left intact. ``dry_run=True`` writes nothing.
    """
    cid = correction_id if isinstance(correction_id, str) else None
    cid = cid if (cid and cid.strip()) else None
    iid = invalidation_id if isinstance(invalidation_id, str) else None
    iid = iid if (iid and iid.strip()) else None

    if cid is None and iid is None:
        return _receipt(
            status=REBUILD_INVALID_REQUEST, success=False,
            rebuild_state=REBUILD_STATE_NONE, dry_run=dry_run,
            error="correction_id 或 invalidation_id 必须提供其一（非空字符串）",
        )

    try:
        rows = _read_sidecar_rows(pg, correction_id=cid, invalidation_id=iid)
    except _MissingSidecarTable as exc:
        return _receipt(
            status=REBUILD_MIGRATION_REQUIRED, success=False,
            rebuild_state=REBUILD_STATE_NONE, correction_id=cid,
            invalidation_id=iid, dry_run=dry_run,
            error=f"{_INVALIDATION_TABLE} 不存在；请先应用 M03 侧车迁移（{exc}）",
        )
    except Exception as exc:  # noqa: BLE001
        return _receipt(
            status=REBUILD_FAILED, success=False,
            rebuild_state=REBUILD_STATE_PENDING, correction_id=cid,
            invalidation_id=iid, dry_run=dry_run,
            error=f"读取 {_INVALIDATION_TABLE} 失败: {exc!r}",
        )

    if iid is not None and cid is not None:
        rows = [r for r in rows if r.get("correction_id") == cid]
    if not rows:
        return _receipt(
            status=REBUILD_NO_INVALIDATION, success=True,
            rebuild_state=REBUILD_STATE_NONE, correction_id=cid,
            invalidation_id=iid, dry_run=dry_run, error=None,
        )

    results: list = []
    for row in rows:
        results.append(
            _rebuild_one(pg, row, dry_run=bool(dry_run), provider=provider)
        )

    counts = _empty_counts()
    counts["requested"] = len(results)
    for res in results:
        state = res.get("rebuild_state")
        if state == REBUILD_STATE_APPLIED:
            counts["rebuilt"] += 1
        elif state == REBUILD_STATE_DEDUPLICATED:
            counts["deduplicated"] += 1
        elif state == REBUILD_STATE_NOT_SUPPORTED:
            counts["unsupported"] += 1
        elif state == REBUILD_STATE_PENDING:
            counts["pending"] += 1
        elif state == REBUILD_STATE_PREVIEW:
            continue
        else:
            counts["failed"] += 1

    statuses = [r["status"] for r in results]
    if REBUILD_FAILED in statuses:
        overall = REBUILD_FAILED
    elif REBUILD_PROVIDER_UNAVAILABLE in statuses:
        overall = REBUILD_PROVIDER_UNAVAILABLE
    elif REBUILD_APPLIED in statuses:
        overall = REBUILD_APPLIED
    elif REBUILD_DEDUPLICATED in statuses:
        overall = REBUILD_DEDUPLICATED
    elif REBUILD_NO_REPLACEMENT in statuses:
        overall = REBUILD_NO_REPLACEMENT
    else:
        overall = REBUILD_NOT_SUPPORTED

    # The receipt's headline fields describe the rebuildable (topic) row when
    # one was requested; unsupported kinds are still listed in ``results``.
    head = next(
        (r for r in results if r.get("derived_kind") in REBUILDABLE_KINDS),
        results[0],
    )
    success = all(r["success"] for r in results)
    would = any(r.get("would_rebuild") for r in results)
    return _receipt(
        status=overall,
        success=success,
        rebuild_state=head["rebuild_state"],
        correction_id=head.get("correction_id") or cid,
        invalidation_id=head.get("invalidation_id") or iid,
        derived_kind=head.get("derived_kind"),
        derived_id=head.get("derived_id"),
        source_memory_id=head.get("source_memory_id"),
        replacement_memory_id=head.get("replacement_memory_id"),
        new_derived_id=head.get("new_derived_id"),
        generation_context=head.get("generation_context"),
        lineage=head.get("lineage"),
        counts=counts,
        results=results,
        dry_run=bool(dry_run),
        would_rebuild=would,
        error=head.get("error"),
    )
