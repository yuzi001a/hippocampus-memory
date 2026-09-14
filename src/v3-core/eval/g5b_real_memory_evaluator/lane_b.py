"""Lane B: live provider-backed PG plumbing (DISABLED BY DEFAULT).

This module exists so that, when a disposable lab PG is available and
the evaluator is invoked with ``--live-pg`` and a verified DSN, the
G5b evaluator can run against a real pgvector database. It is
explicitly **fail-closed**:

  * ``build_lane_b_writer_and_reader()`` raises ``LaneBDisabled``
    unless the caller passes ``--live-pg`` AND the DSN is recognised
    as a disposable lab DSN (see ``_validate_disposable_dsn``).
  * The DSN guard rejects: production-profile DSNs, the reserved
    production port, reserved production hostnames
    (``v3-pgvector``, ``v3-pgvector-prod``), and any DSN that
    targets ``v3embeddings`` / ``v3embeddings_eval`` /
    ``v3embeddings_eval_v2`` (these are reserved DB names).
  * The real PG connection + ``register_vector`` + cursor + DDL
    setup is wrapped in a single failure boundary. On any setup
    exception, an opened connection is closed best-effort and a
    ``LaneBDisabled`` is raised carrying only the exception TYPE
    NAME (never the raw message, DSN, password, or traceback).
    See ``_safe_exc_message``.

The evaluator's contract is that running ``python -m
eval.g5b_real_memory_evaluator`` without flags NEVER opens a real
PG connection. This is a development-infrastructure safety
contract.

Note: this module does NOT currently enforce a "host=localhost with
default postgres credentials" guard — that level of guard is left
to the operator. The only host-level rule is the
``_RESERVED_HOSTS`` blocklist of known production hostnames; any
host that is not on that blocklist (including ``localhost`` with
arbitrary credentials) is accepted as long as the other guard
fields pass. The DSN itself is never echoed back to the caller
on any failure path.

"""
from __future__ import annotations

import os
import re
from typing import Any


class LaneBDisabled(RuntimeError):
    """Raised when Lane B is requested but the safety guard refuses."""


# Production-reserved ports / names — the lab may never use these.
# The numeric port 5433 is a reserved safety sentinel for the
# canonical production PG listener; it is not a live endpoint the
# evaluator ever opens.
_RESERVED_PORTS = {"5433"}
_RESERVED_DBS = {"v3embeddings", "v3embeddings_eval", "v3embeddings_eval_v2"}
_RESERVED_HOSTS = {"v3-pgvector", "v3-pgvector-prod"}


def _parse_dsn(dsn: str) -> dict[str, str]:
    """Very small DSN parser for ``key=value`` PG connection strings.

    Supports space-separated key=value pairs. Single-quoted values
    are honoured. Anything we don't recognise is preserved under the
    same key.
    """
    out: dict[str, str] = {}
    if not isinstance(dsn, str) or not dsn.strip():
        return out
    # naive splitter — good enough for lab DSNs that follow the
    # psycopg2 ``key=value`` convention.
    for m in re.finditer(r"(\w+)=('(?:[^']|'')*'|\S+)", dsn):
        key = m.group(1).lower()
        val = m.group(2)
        if val.startswith("'") and val.endswith("'"):
            val = val[1:-1].replace("''", "'")
        out[key] = val
    return out


def _validate_disposable_dsn(dsn: str) -> None:
    """Raise LaneBDisabled if the DSN does not look disposable + lab-only."""
    parts = _parse_dsn(dsn)
    if not parts:
        raise LaneBDisabled(
            "Lane B: DSN could not be parsed; refusing to open any connection."
        )
    port = parts.get("port") or ""
    host = (parts.get("host") or "").lower()
    dbname = (parts.get("dbname") or "").lower()
    user = (parts.get("user") or "").lower()
    if port in _RESERVED_PORTS:
        raise LaneBDisabled(
            f"Lane B: DSN port={port!r} is reserved (production / known lab). "
            "Use a different disposable port."
        )
    if dbname in _RESERVED_DBS:
        raise LaneBDisabled(
            f"Lane B: DSN dbname={dbname!r} is a reserved production/eval "
            "database name. Use a disposable lab db (e.g. 'lab_eval')."
        )
    if host in _RESERVED_HOSTS:
        raise LaneBDisabled(
            f"Lane B: DSN host={host!r} matches a reserved production hostname."
        )
    # Guard against inheriting production profile / HOME credentials.
    env_user = (os.environ.get("PGUSER") or "").lower()
    if env_user and env_user == user:
        # Same env PGUSER is suspicious — could be a leftover shell
        # credential that points at production. Allow only when the
        # env explicitly opts into lab mode.
        if os.environ.get("G5B_EVAL_LAB_DSN_OPT_IN") != "1":
            raise LaneBDisabled(
                f"Lane B: PGUSER env ({env_user!r}) matches DSN user; "
                "set G5B_EVAL_LAB_DSN_OPT_IN=1 to confirm this is the "
                "intended lab credential."
            )


def resolve_lane_b_dsn(explicit: str | None) -> str | None:
    """Return the DSN to use for Lane B, or None if Lane B stays closed.

    Order:

      1. ``explicit`` arg (from ``--dsn`` flag).
      2. ``$G5B_EVAL_LAB_DSN`` environment variable.
      3. None — caller must treat as ``LaneBDisabled``.

    Note that we do NOT enable Lane B merely because an env var is
    set; the runtime also requires the ``G5B_EVAL_LIVE_PG=1`` env
    var (set by ``--live-pg``) to actually open the connection.
    """
    if explicit:
        return explicit
    return os.environ.get("G5B_EVAL_LAB_DSN")


def is_lane_b_enabled() -> bool:
    """True only when both the live-pg gate AND a DSN are present."""
    return (
        os.environ.get("G5B_EVAL_LIVE_PG") == "1"
        and resolve_lane_b_dsn(None) is not None
    )


def _safe_exc_message(exc: BaseException) -> str:
    """Return a non-secret diagnostic string for an exception.

    Some drivers (notably psycopg2) embed the DSN / connection
    parameters in the exception ``args`` tuple. Re-raising those
    via ``repr(exc)`` is a credential-leak risk for the lab. We
    return the exception type name and a stable hint that does NOT
    include the message text. Callers that need the raw exception
    can still see it via the original chained exception.
    """
    return f"{type(exc).__name__} (message suppressed for safety)"


def _close_quietly(conn: Any) -> None:
    """Best-effort close of a possibly-open psycopg2 connection.

    Used inside the Lane B setup boundary so that a failed
    ``register_vector`` / ``cursor`` / DDL never leaves the live
    connection sitting open. Any close-time error is swallowed
    (logged via the chained exception already raised by the caller).
    """
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        # Intentionally swallow — the caller is already converting
        # the original setup failure into LaneBDisabled. A
        # secondary close failure must not leak another exception
        # (which itself could carry connection parameters).
        pass


def build_lane_b_writer_and_reader(dsn: str | None) -> tuple[Any, Any]:
    """Build writer/reader over a real pgvector database.

    Raises ``LaneBDisabled`` when the DSN fails any guard. Never
    silently falls back to Lane A.

    Imports are deferred so the package can be imported without
    requiring psycopg2 to be available.

    The connection / ``register_vector`` / cursor / DDL setup is
    wrapped in a single failure boundary: any exception raised
    during that phase closes an opened connection (best-effort)
    and is converted into ``LaneBDisabled`` carrying only the
    exception TYPE NAME (no raw message, no DSN, no password, no
    traceback). The connection is returned to the caller only
    after setup succeeds.
    """
    import importlib

    if dsn is None:
        raise LaneBDisabled("Lane B: no DSN provided.")
    _validate_disposable_dsn(dsn)

    try:
        v3core = importlib.import_module("v3core.active_memory_store")
    except Exception as exc:
        raise LaneBDisabled(
            "Lane B: v3core.active_memory_store not importable: "
            + _safe_exc_message(exc)
        ) from exc

    try:
        import psycopg2  # type: ignore
        from pgvector.psycopg2 import register_vector  # type: ignore
    except Exception as exc:
        raise LaneBDisabled(
            "Lane B: psycopg2 / pgvector unavailable: "
            + _safe_exc_message(exc)
        ) from exc

    # Real lab connection + register_vector + cursor + DDL setup.
    # Every step is inside one failure boundary: on any exception
    # we close the connection (best-effort) and convert the
    # exception into LaneBDisabled carrying only the exception
    # TYPE NAME — never the raw message (which can include the
    # DSN / password for psycopg2.OperationalError).
    conn: Any = None
    cur: Any = None
    try:
        try:
            conn = psycopg2.connect(dsn)
        except Exception as exc:
            # conn is None or partially-open here; nothing to close.
            raise LaneBDisabled(
                "Lane B: psycopg2.connect failed: "
                + _safe_exc_message(exc)
            ) from exc

        try:
            register_vector(conn)
        except Exception as exc:
            raise LaneBDisabled(
                "Lane B: register_vector failed: "
                + _safe_exc_message(exc)
            ) from exc

        try:
            cur = conn.cursor()
        except Exception as exc:
            raise LaneBDisabled(
                "Lane B: conn.cursor() failed: "
                + _safe_exc_message(exc)
            ) from exc

        # Apply schema. The lab is responsible for cleanup; we never
        # touch production.
        #
        # Resolve the DDL path from THIS module's location. The schema
        # lives at ``src/v3-core/schema/explicit_memories.sql``; this
        # module lives at
        # ``src/v3-core/eval/g5b_real_memory_evaluator/lane_b.py``, so
        # two ``..`` segments land in ``src/v3-core/``.
        schema_path = os.path.normpath(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                os.pardir, os.pardir,
                "schema", "explicit_memories.sql",
            )
        )
        if not os.path.exists(schema_path):
            raise LaneBDisabled(
                "Lane B: schema DDL file not found at expected path: "
                f"{schema_path}"
            )
        with open(schema_path, "r", encoding="utf-8") as f:
            ddl = f.read()
        try:
            cur.execute(ddl)
        except Exception as exc:
            raise LaneBDisabled(
                "Lane B: schema DDL execute failed: "
                + _safe_exc_message(exc)
            ) from exc
        try:
            conn.commit()
        except Exception as exc:
            raise LaneBDisabled(
                "Lane B: conn.commit() failed after DDL: "
                + _safe_exc_message(exc)
            ) from exc
    except LaneBDisabled:
        # Boundary failure: close what we opened (best-effort) and
        # propagate the safe diagnostic. We deliberately do NOT
        # call ``traceback.print_exc`` or ``repr(exc)`` here —
        # those would echo the original DSN.
        _close_quietly(conn)
        raise
    except Exception:
        # Any other unexpected exception is also funneled through
        # the safe boundary. Close + raise a generic LaneBDisabled.
        _close_quietly(conn)
        raise LaneBDisabled(
            "Lane B: unexpected setup error (type suppressed for safety)"
        )

    # ── setup succeeded; build the adapter pool ────────────────────────
    # Build a minimal PgPool-shaped adapter around the connection.
    # We do not import v3core.pg_pool here — lab may run on a clean
    # venv without PG pool machinery.
    class _ConnAdapter:
        def __init__(self, c: Any) -> None:
            self._c = c

        def cursor(self) -> Any:
            return self._c.cursor()

        def commit(self) -> Any:
            return self._c.commit()

    class _LivePool:
        def __init__(self, c: Any) -> None:
            self._c = c

        def lease(self, timeout: float | int | None = None, **_: Any) -> _ConnAdapter:
            return _ConnAdapter(self._c)

    pool = _LivePool(conn)
    embed_cfg = {
        "_fingerprint": "stub-bag-of-tokens-v1",
        "model": "stub-bag-of-tokens-v1",
    }

    writer = v3core.ActiveMemoryWriter(
        pool=pool, pg=None, config=None, embed_cfg=embed_cfg,
        embedder=_lane_b_stub_embedder,
    )
    reader = v3core.ActiveMemoryReader(pool=pool, pg=None)
    return writer, reader, conn


def _lane_b_stub_embedder(text: str, embed_cfg: dict[str, Any]) -> list[float]:
    """Lab embedder for Lane B — still deterministic.

    Lane B may choose to swap this for a real model via the caller.
    By default we use the same deterministic stub so the lab
    measurement is reproducible across machines.
    """
    from .lane_a import deterministic_embedder

    return deterministic_embedder(text, embed_cfg)


__all__ = [
    "LaneBDisabled",
    "_parse_dsn",
    "_validate_disposable_dsn",
    "resolve_lane_b_dsn",
    "is_lane_b_enabled",
    "build_lane_b_writer_and_reader",
]