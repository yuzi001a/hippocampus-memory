"""Durable, explainable accounting for embedding failures.

THE CONTRACT THIS MODULE ENFORCES
---------------------------------
    Embedding failure is allowed. Silent permanent memory loss is not.
    (Embedding 可以失败，但不能无声地变成永久失忆。)

The rule in practice:

* **Source truth first.** The original experience/row is written and kept. A
  missing derived embedding must never destroy, or block, the source record.
* **Derived embedding may fail.** Embeddings are a rebuildable index, not the
  asset. Losing one is recoverable; losing the source is not.
* **Failure must be explicit.** Any embedding left NULL *because a request was
  attempted and failed* gets a row in ``public.embedding_failures`` describing
  why, whether it is retryable, and how hard we tried. A NULL with no marker is
  the one outcome this module exists to make impossible.
* **Determinism is not transience.** A missing API key is not a timeout. It is
  recorded as non-retryable so a repair pass never grinds on it, and so nobody
  later mistakes it for provider flakiness.

WHAT "SUCCESS" MEANS FOR A CALLER
---------------------------------
``embed_for_write`` returns an :class:`EmbedOutcome`, never a bare list. Its
``status`` is one of:

* ``ok``        — vector obtained; nothing to record.
* ``degraded``  — source is durable, this embedding is missing, marker written,
                  and a repair pass may legitimately retry it.
* ``failed``    — source is durable, this embedding is missing, marker written,
                  and retrying is pointless (config/auth/model problem). This is
                  a *real* defect surfacing, not a transient blip.

Callers must not collapse these into a single falsy value — that is how a
configuration error gets mistaken for a slow provider and quietly becomes a
permanent hole.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum

from .embedding import (
    EmbeddingCallError,
    EmbedErrorClass,
    EmbedPolicy,
    DURABLE_WRITE_EMBED_POLICY,
    call_embedding,
    classify_embed_error,
)

logger = logging.getLogger(__name__)


class EmbedOutcomeStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass
class EmbedOutcome:
    """Result of an embedding attempt made as part of a durable write."""

    status: EmbedOutcomeStatus
    vector: list[float] | None
    error_class: str | None = None
    retryable: bool = False
    attempts: int = 0
    elapsed_ms: float = 0.0
    policy: str = ""
    marker_recorded: bool = False
    detail: str = ""
    # Which model produced (or was asked to produce) this vector. A durable write must
    # record this next to the vector; without it a repair pass cannot tell whether it is
    # about to mix two vector spaces, which is the failure mode the consistency redline
    # exists to prevent.
    model_fingerprint: str = ""

    @property
    def ok(self) -> bool:
        return self.status is EmbedOutcomeStatus.OK

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "error_class": self.error_class,
            "retryable": self.retryable,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
            "policy": self.policy,
            "marker_recorded": self.marker_recorded,
            "detail": self.detail,
            "model_fingerprint": self.model_fingerprint,
        }


def safe_error_fingerprint(text: str) -> str:
    """Short, stable fingerprint of an error message.

    Stored instead of the message itself: PHASE 8 forbids persisting raw provider
    responses, and error text can embed request content.
    """
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


#: SQL for the idempotent upsert. One live record per (entity, phase); repeated
#: failures bump ``attempts`` rather than stacking rows, and a later success
#: resolves the row instead of erasing the history.
_UPSERT_SQL = """
INSERT INTO public.embedding_failures (
    entity_table, entity_id, phase,
    error_class, retryable, provider_status,
    attempts, timeout_policy, timeout_seconds, max_retries, elapsed_ms,
    model, model_fingerprint, error_fingerprint,
    first_failed_at, last_failed_at, updated_at
) VALUES (
    %(entity_table)s, %(entity_id)s, %(phase)s,
    %(error_class)s, %(retryable)s, %(provider_status)s,
    %(attempts)s, %(timeout_policy)s, %(timeout_seconds)s, %(max_retries)s, %(elapsed_ms)s,
    %(model)s, %(model_fingerprint)s, %(error_fingerprint)s,
    now(), now(), now()
)
ON CONFLICT (entity_table, entity_id, phase) DO UPDATE SET
    error_class       = EXCLUDED.error_class,
    retryable         = EXCLUDED.retryable,
    provider_status   = EXCLUDED.provider_status,
    attempts          = public.embedding_failures.attempts + EXCLUDED.attempts,
    timeout_policy    = EXCLUDED.timeout_policy,
    timeout_seconds   = EXCLUDED.timeout_seconds,
    max_retries       = EXCLUDED.max_retries,
    elapsed_ms        = EXCLUDED.elapsed_ms,
    model             = EXCLUDED.model,
    model_fingerprint = EXCLUDED.model_fingerprint,
    error_fingerprint = EXCLUDED.error_fingerprint,
    last_failed_at    = now(),
    updated_at        = now(),
    resolved_at       = NULL,
    resolution        = NULL
"""

_RESOLVE_SQL = """
UPDATE public.embedding_failures
   SET resolved_at = now(),
       resolution  = %(resolution)s,
       updated_at  = now()
 WHERE entity_table = %(entity_table)s
   AND entity_id    = %(entity_id)s
   AND phase        = %(phase)s
   AND resolved_at IS NULL
"""


def _resolve_conn(conn, conn_factory):
    """Get a connection for the marker write, without disturbing pool semantics.

    ``PgEmbedStore`` may be pool-backed, in which case grabbing its raw ``_conn``
    is impossible by design (callers must ``lease()``). Failure accounting is a
    rare side-write, so it opens its own short-lived connection through the store
    rather than borrowing a lease it would have to hold across the embed call.
    """
    if conn is not None:
        return conn
    if conn_factory is None:
        return None
    try:
        return conn_factory()
    except Exception:
        logger.error(
            "无法为 embedding 失败标记建立连接 — 该失败将**未被记录**",
            exc_info=True,
        )
        return None


def record_embedding_failure(
    conn=None,
    *,
    entity_table: str,
    entity_id: str,
    phase: str,
    error: BaseException | EmbeddingCallError,
    policy: EmbedPolicy | None = None,
    model: str = "",
    model_fingerprint: str = "",
    conn_factory=None,
) -> bool:
    """Write (or bump) the durable failure marker for one entity.

    Returns True when the marker was persisted. Never raises: failure accounting
    must not be able to take down the very write path it is protecting — but a
    failure to record is logged loudly, because an unrecorded failure is exactly
    the silent hole this module exists to close.
    """
    if isinstance(error, EmbeddingCallError):
        error_class = error.error_class.value
        retryable = error.retryable
        attempts = error.attempts
        elapsed_ms = round(error.elapsed * 1000, 1)
        pol = error.policy
        status = error.status
        model = model or error.model
        model_fingerprint = model_fingerprint or error.fingerprint
        raw = str(error)
    else:
        # Use the SAME classifier as the live path. Defaulting to UNKNOWN here made the
        # durable marker disagree with the outcome the caller saw (marker said
        # EMBEDDING_UNKNOWN while the outcome said EMBEDDING_CONFIG_INVALID) — and a
        # repair pass reads the marker, not the in-memory outcome, so the mislabel would
        # have outlived the call.
        cls = classify_embed_error(error)
        error_class = cls.value
        retryable = cls.retryable
        attempts = 0
        elapsed_ms = 0.0
        pol = policy or DURABLE_WRITE_EMBED_POLICY
        status = None
        raw = f"{type(error).__name__}: {error}"

    own_conn = False
    if conn is None:
        conn = _resolve_conn(None, conn_factory)
        own_conn = conn is not None
    if conn is None:
        logger.error(
            "embedding 失败标记无法写入 (无可用连接) — 这是一次**未被记录**的失败, "
            "会产生 silent NULL: entity=%s/%s phase=%s class=%s",
            entity_table, entity_id, phase, error_class,
        )
        return False

    params = {
        "entity_table": entity_table,
        "entity_id": str(entity_id),
        "phase": phase,
        "error_class": error_class,
        "retryable": retryable,
        "provider_status": status,
        "attempts": attempts,
        "timeout_policy": pol.name,
        "timeout_seconds": float(pol.timeout),
        "max_retries": int(pol.retries),
        "elapsed_ms": elapsed_ms,
        "model": model or None,
        "model_fingerprint": model_fingerprint or None,
        "error_fingerprint": safe_error_fingerprint(raw),
    }
    try:
        with conn.cursor() as cur:
            cur.execute(_UPSERT_SQL, params)
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(
            "embedding 失败标记写入失败 — 这是一次**未被记录**的失败, 会产生 silent NULL: "
            "entity=%s/%s phase=%s class=%s",
            entity_table, entity_id, phase, error_class,
            exc_info=True,
        )
        return False
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def resolve_embedding_failure(conn=None, *, entity_table: str, entity_id: str,
                              phase: str, resolution: str = "repaired",
                              conn_factory=None) -> bool:
    """Mark a previously-recorded failure as resolved (e.g. by a repair pass)."""
    own_conn = False
    if conn is None:
        conn = _resolve_conn(None, conn_factory)
        own_conn = conn is not None
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(_RESOLVE_SQL, {
                "entity_table": entity_table,
                "entity_id": str(entity_id),
                "phase": phase,
                "resolution": resolution,
            })
            changed = cur.rowcount
        conn.commit()
        return changed > 0
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("embedding 失败标记 resolve 失败: %s/%s",
                       entity_table, entity_id, exc_info=True)
        return False
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def embed_for_write(
    text: str,
    embed_cfg: dict,
    *,
    entity_table: str,
    entity_id: str,
    phase: str,
    conn=None,
    conn_factory=None,
    policy: EmbedPolicy | None = None,
    cache: bool = False,
) -> EmbedOutcome:
    """Embed ``text`` as part of persisting one entity, with explicit failure state.

    This is the replacement for the historical::

        try:
            ev = call_embedding(text, cfg)
        except ValueError:
            raise
        except Exception:
            ev = None          # ← the silent hole

    Differences that matter:

    * The budget comes from an explicit, named ``policy`` — a durable write no
      longer inherits the 3s/0 realtime default just because a caller forgot.
    * A config error is NOT swallowed. It is recorded as non-retryable and
      reported as ``failed``, so it stays visible as a defect.
    * The outcome distinguishes "no vector, retry later" from "no vector, and
      retrying will never help". Both keep the source row.
    """
    pol = policy or DURABLE_WRITE_EMBED_POLICY
    started = time.time()

    # Resolve the model fingerprint through the SAME canonical resolver the durable writers
    # use, so the outcome and the row agree. Reading `_fingerprint` off the dict directly
    # (as an earlier revision did) yields "" whenever the caller passed a raw config, and a
    # vector whose provenance is "" cannot be audited for cross-model mixing.
    _fp = ""
    try:
        from .pg_store import _resolve_embed_cfg
        if embed_cfg:
            _fp, _ = _resolve_embed_cfg(embed_cfg)
    except Exception:
        _fp = (embed_cfg.get("_fingerprint") or "") if embed_cfg else ""

    if not text or not embed_cfg:
        # Nothing was requested, so no *request* failed: this is an empty source,
        # not a lost embedding. Recording it as an embedding failure would poison
        # the very signal this module protects.
        return EmbedOutcome(
            status=EmbedOutcomeStatus.DEGRADED, vector=None,
            error_class="NO_INPUT", retryable=False, policy=pol.name,
            detail="empty text or missing embed config — no request attempted",
        )

    stats: dict = {}
    try:
        vec = call_embedding(text, embed_cfg, cache=cache, policy=pol, stats=stats)
    except ValueError as exc:
        # fail-closed config error from _validate_for_call: must stay visible and
        # must never be downgraded to "transient". Re-raise for ValueError callers
        # who treat it as a hard configuration contract, but record it first.
        # Always ATTEMPT to record. Passing neither conn nor conn_factory must not
        # silently skip the marker — skipping is the exact silent hole this module
        # exists to close. `record_embedding_failure` logs an ERROR and returns
        # False when it cannot persist, so the failure stays loud either way.
        _recorded = record_embedding_failure(
            conn, conn_factory=conn_factory, entity_table=entity_table, entity_id=entity_id, phase=phase,
            error=exc, policy=pol,
            model=(embed_cfg.get("model") or "") if embed_cfg else "",
            model_fingerprint=_fp,
        )
        return EmbedOutcome(
            status=EmbedOutcomeStatus.FAILED, vector=None,
            error_class=EmbedErrorClass.CONFIG.value, retryable=False,
            attempts=0, elapsed_ms=round((time.time() - started) * 1000, 1),
            policy=pol.name, marker_recorded=_recorded,
            detail=f"embedding config invalid: {exc}", model_fingerprint=_fp,
        )
    except EmbeddingCallError as exc:
        recorded = record_embedding_failure(
            conn, conn_factory=conn_factory, entity_table=entity_table,
            entity_id=entity_id, phase=phase, error=exc, policy=pol,
        )
        status = (EmbedOutcomeStatus.DEGRADED if exc.retryable
                  else EmbedOutcomeStatus.FAILED)
        return EmbedOutcome(
            status=status, vector=None,
            error_class=exc.error_class.value, retryable=exc.retryable,
            attempts=exc.attempts, elapsed_ms=round(exc.elapsed * 1000, 1),
            policy=pol.name, marker_recorded=recorded,
            detail=f"{exc.error_class.value} after {exc.attempts} attempt(s)",
            model_fingerprint=exc.fingerprint or _fp,
        )
    except Exception as exc:  # unexpected — record as UNKNOWN, still not silent
        recorded = record_embedding_failure(
            conn, conn_factory=conn_factory, entity_table=entity_table,
            entity_id=entity_id, phase=phase, error=exc, policy=pol,
        )
        return EmbedOutcome(
            status=EmbedOutcomeStatus.DEGRADED, vector=None,
            error_class=EmbedErrorClass.UNKNOWN.value, retryable=True,
            elapsed_ms=round((time.time() - started) * 1000, 1),
            policy=pol.name, marker_recorded=recorded,
            detail=f"unexpected {type(exc).__name__}", model_fingerprint=_fp,
        )

    if conn is not None or conn_factory is not None:
        # A success clears any stale marker for this entity+phase.
        resolve_embedding_failure(
            conn, conn_factory=conn_factory, entity_table=entity_table,
            entity_id=entity_id, phase=phase, resolution="success",
        )
    return EmbedOutcome(
        status=EmbedOutcomeStatus.OK, vector=vec,
        attempts=int(stats.get("attempts", 1)),
        elapsed_ms=round(float(stats.get("elapsed", time.time() - started)) * 1000, 1),
        policy=pol.name, model_fingerprint=_fp,
    )


# ─── operator / evidence queries (read-only) ────────────────────────────────

#: NULL embeddings in ``table`` with NO failure marker — the primary production
#: success metric (target: 0 for newly created rows).
_UNEXPLAINED_NULL_SQL = """
SELECT COUNT(*) AS unexplained
  FROM public.%(table)s t
  LEFT JOIN public.embedding_failures f
         ON f.entity_table = %(table_literal)s
        AND f.entity_id    = %(key_expr)s
 WHERE t.%(vec_col)s IS NULL
   AND f.id IS NULL
   %(nonempty_clause)s
"""


def count_unexplained_nulls(conn, *, table: str, pk: str = "id",
                            vec_col: str = "embedding",
                            entity_key_sql: str | None = None,
                            text_col: str | None = None) -> int:
    """Count NULL vector values that have no durable explanation.

    This is the number that must stop growing. A NULL *with* a marker is an acceptable,
    repairable state; a NULL without one is the silent hole.

    ``text_col`` restricts the count to rows that actually have source text. A row whose
    source is empty was never supposed to have a vector, so counting it would inflate the
    metric with non-problems and, worse, train the reader to ignore the number. Leave it
    ``None`` to count every NULL regardless.

    ``entity_key_sql`` overrides the marker join key. Needed for tables whose marker key is
    a natural/composite key rather than the PK — e.g. ``yin_paragraphs``, where the marker
    is keyed on ``yin_version || '/' || section`` because the PK is a SERIAL assigned by
    the INSERT.
    """
    key_expr = entity_key_sql or f"t.{pk}::text"
    nonempty = (f"AND COALESCE(btrim(t.{text_col}), '') <> ''"
                if text_col else "")
    sql = _UNEXPLAINED_NULL_SQL % {
        "table": table, "vec_col": vec_col, "table_literal": "%s",
        "key_expr": key_expr, "nonempty_clause": nonempty,
    }
    with conn.cursor() as cur:
        cur.execute(sql, (table,))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def failure_summary(conn) -> dict:
    """Grouped failure accounting, for the reliability report."""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT entity_table, error_class, retryable, COUNT(*)
              FROM public.embedding_failures
             WHERE resolved_at IS NULL
             GROUP BY 1, 2, 3
             ORDER BY 4 DESC
            """
        )
        out["unresolved_by_entity_class"] = [
            {"entity_table": r[0], "error_class": r[1], "retryable": r[2], "count": r[3]}
            for r in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE resolved_at IS NULL)          AS unresolved,
                   COUNT(*) FILTER (WHERE resolved_at IS NULL AND retryable) AS retryable,
                   COUNT(*)                                              AS total
              FROM public.embedding_failures
            """
        )
        r = cur.fetchone()
        out["totals"] = {"unresolved": r[0], "retryable": r[1], "total": r[2]}
    return json.loads(json.dumps(out, default=str))
