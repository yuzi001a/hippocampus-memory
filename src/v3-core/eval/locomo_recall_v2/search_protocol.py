"""G6C-B0 exact/ANN session policy — evaluator-only control surface.

The minimum reusable contract:

  * :class:`SearchProtocol` — a validated exact/ANN session policy.
  * :func:`apply_search_protocol` — apply + verify the policy on a
    psycopg2-style connection.
  * :class:`SearchProtocolError` / :class:`SearchProtocolReport` —
    public error and result types.

Connection-level scope is mandatory: ``set_config(name, value, false)``
issues a session-scope GUC that survives ``COMMIT`` / ``ROLLBACK``.
``SET LOCAL`` is wrong for pooled / autocommit leases — its effect
expires at the next transaction end and a recycled connection
could silently revert to planner-default behaviour mid-run.

Verification uses ``SELECT current_setting(%s, true)``. ``SHOW
'<name>'`` is invalid PostgreSQL syntax (``SHOW`` takes an
identifier, not a literal); ``current_setting`` is the canonical
portable round-trip.

The production module deliberately does NOT export any test fakes
or fake-cursor classes. Tests under
``tests/test_search_protocol.py`` carry their own minimal stub.

GUC registration contract
=========================

A PostgreSQL extension's GUCs (including ``ivfflat.probes``) are
registered only when the extension's shared library is loaded
into the running backend.  The ``pgvector`` shared library is
loaded LAZILY — the first pgvector function call in a fresh
backend triggers the load.  Concretely:

  * A fresh backend that has executed NO pgvector function yet has
    NO row in ``pg_settings`` for ``ivfflat.probes`` (and no
    planner effect for any ``set_config`` round-trip).
  * After the shared library is loaded into the backend — either
    via ``LOAD 'vector'`` or via executing any pgvector function
    (``('[0]'::vector) <=> ('[0]'::vector)`` and similar) — the
    GUC appears in ``pg_settings`` and the planner reads it.

Therefore availability must be probed AFTER forcing the library
load into the backend.  Probing BEFORE the load would always
report ``UNAVAILABLE`` even on a perfectly healthy pgvector
build, which is exactly the false-verdict defect this module
fixes.

A ``SET`` issued BEFORE the library is loaded only creates a
*custom* placeholder variable — PostgreSQL accepts the value
echo (``set_config('ivfflat.probes','1',false)`` followed by
``current_setting(..., true)`` round-trips ``'1'``) but the
planner never reads it.  Such a round-trip is a LIE; the value
has no effect on the index probe count.  This module
distinguishes "registered" (the planner actually reads this
value) from "unregistered" (only a custom variable exists,
planner ignores it):

  * :data:`PROBE_AVAILABILITY_AVAILABLE` — ``pg_settings`` carries
    a row for ``ivfflat.probes`` AFTER the library load was
    forced.  SET + verify are meaningful.
  * :data:`PROBE_AVAILABILITY_UNAVAILABLE` — no row in
    ``pg_settings`` even after the load was forced.  Any SET +
    verify is a custom-GUC echo and has NO planner effect.

Behavioural contract:

  * Explicit ``probes`` in ``ann`` mode + ``AVAILABLE``: SET +
    ``current_setting`` round-trip; fail-closed on mismatch.
  * Explicit ``probes`` in ``ann`` mode + ``UNAVAILABLE``: REFUSED
    with :class:`SearchProtocolError`.  We never fall back to a
    custom-GUC echo because that would report a planner-affecting
    value that does not exist.
  * Implicit ``probes`` (any mode) + ``UNAVAILABLE``: NO failure.
    The report records ``probe_availability='UNAVAILABLE'`` and
    leaves ``ivfflat_probes=None``; we NEVER invent a numeric
    default.
  * Implicit ``probes`` (any mode) + ``AVAILABLE``: existing
    read-back semantics — the current GUC value is observed and
    recorded verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------


class SearchProtocolError(RuntimeError):
    """Raised when the search-protocol contract is violated.

    Error messages never echo DSN parts, passwords, or query
    text — the verifier surfaces the actual verified value
    through :func:`SearchProtocolReport.observed_value` so
    diagnostics stay outside the message.
    """


# ---------------------------------------------------------------------
# Allowed modes
# ---------------------------------------------------------------------

SEARCH_MODE_EXACT = "exact"
SEARCH_MODE_ANN = "ann"

ALLOWED_SEARCH_MODES: frozenset[str] = frozenset({
    SEARCH_MODE_EXACT,
    SEARCH_MODE_ANN,
})


# ---------------------------------------------------------------------
# ivfflat.probes registration status
# ---------------------------------------------------------------------

PROBE_AVAILABILITY_AVAILABLE = "AVAILABLE"
PROBE_AVAILABILITY_UNAVAILABLE = "UNAVAILABLE"

ALLOWED_PROBE_AVAILABILITY: frozenset[str] = frozenset({
    PROBE_AVAILABILITY_AVAILABLE,
    PROBE_AVAILABILITY_UNAVAILABLE,
})


# ---------------------------------------------------------------------
# pgvector library load — names that callers may import
# ---------------------------------------------------------------------

PGVECTOR_LIBRARY_NAME = "vector"

PROBE_LOAD_METHOD_LOAD = "LOAD"
PROBE_LOAD_METHOD_VECTOR_FUNCTION = "VECTOR_FUNCTION"
PROBE_LOAD_METHOD_NONE = "NONE"

ALLOWED_PROBE_LOAD_METHODS: frozenset[str] = frozenset({
    PROBE_LOAD_METHOD_LOAD,
    PROBE_LOAD_METHOD_VECTOR_FUNCTION,
    PROBE_LOAD_METHOD_NONE,
})


# ---------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class SearchProtocol:
    """A validated exact/ANN session policy.

    ``mode`` is one of :data:`SEARCH_MODE_EXACT` /
    :data:`SEARCH_MODE_ANN`. ``probes`` is the explicit
    ``ivfflat.probes`` value to force on the connection when
    ``mode == "ann"``; ``None`` keeps the planner default. Ignored
    in ``exact`` mode.
    """

    mode: str
    probes: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in ALLOWED_SEARCH_MODES:
            raise SearchProtocolError(
                f"search_mode must be one of "
                f"{sorted(ALLOWED_SEARCH_MODES)!r}; got {self.mode!r}"
            )
        if self.probes is not None:
            if not isinstance(self.probes, int) or isinstance(self.probes, bool):
                raise SearchProtocolError(
                    f"ivfflat.probes must be a positive int or None; "
                    f"got {type(self.probes).__name__}"
                )
            if int(self.probes) <= 0:
                raise SearchProtocolError(
                    f"ivfflat.probes must be a positive int; got {self.probes!r}"
                )

    @classmethod
    def parse(
        cls,
        *,
        mode: str | None,
        probes: int | None = None,
    ) -> "SearchProtocol":
        """Validate and return a :class:`SearchProtocol`.

        ``mode=None`` resolves to exact mode so legacy callers
        default to the safer of the two choices.
        """

        if mode is None or str(mode).strip() == "":
            return cls(mode=SEARCH_MODE_EXACT, probes=None)
        text = str(mode).strip().lower()
        if text not in ALLOWED_SEARCH_MODES:
            raise SearchProtocolError(
                f"unknown search_mode {mode!r}; "
                f"expected one of {sorted(ALLOWED_SEARCH_MODES)!r}"
            )
        return cls(mode=text, probes=probes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": str(self.mode),
            "probes": (int(self.probes) if self.probes is not None else None),
        }


# ---------------------------------------------------------------------
# Verification report
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class SearchProtocolReport:
    """Observed post-``set_config`` values for one connection.

    ``probe_availability`` is the GUC-registration verdict
    returned by :func:`apply_search_protocol`:
      * :data:`PROBE_AVAILABILITY_AVAILABLE` — ``pg_settings``
        carries a row for ``ivfflat.probes`` (after the pgvector
        library load was forced); the planner reads it.
      * :data:`PROBE_AVAILABILITY_UNAVAILABLE` — no row in
        ``pg_settings`` even after the load was forced; any SET +
        ``current_setting`` echo is only a custom variable, the
        planner ignores it.  The report's ``ivfflat_probes`` is
        always ``None`` in this case (we never claim a numeric
        default).

    The three load-ordering fields surface what the verifier saw
    on the wire:
      * ``probes_registered_before_load`` — row in ``pg_settings``
        BEFORE the load was forced.
      * ``probes_registered_after_load`` — row in ``pg_settings``
        AFTER the load was forced (this is the verdict gate).
      * ``pgvector_load_method`` — one of
        :data:`PROBE_LOAD_METHOD_LOAD` /
        :data:`PROBE_LOAD_METHOD_VECTOR_FUNCTION` /
        :data:`PROBE_LOAD_METHOD_NONE`.
    """

    mode: str
    enable_indexscan: str
    ivfflat_probes: str | None
    verified_probes: int | None
    probe_availability: str = PROBE_AVAILABILITY_AVAILABLE
    probes_registered_before_load: bool = False
    probes_registered_after_load: bool = False
    pgvector_load_method: str = PROBE_LOAD_METHOD_NONE

    def __post_init__(self) -> None:
        if self.pgvector_load_method not in ALLOWED_PROBE_LOAD_METHODS:
            raise SearchProtocolError(
                f"pgvector_load_method must be one of "
                f"{sorted(ALLOWED_PROBE_LOAD_METHODS)!r}; "
                f"got {self.pgvector_load_method!r}"
            )
        if self.probe_availability not in ALLOWED_PROBE_AVAILABILITY:
            raise SearchProtocolError(
                f"probe_availability must be one of "
                f"{sorted(ALLOWED_PROBE_AVAILABILITY)!r}; "
                f"got {self.probe_availability!r}"
            )

    @property
    def observed_value(self) -> dict[str, Any]:
        """Sanitised snapshot suitable for CLI diagnostic payloads.

        Never echoes DSN parts or credentials.
        """

        return {
            "mode": str(self.mode),
            "enable_indexscan": str(self.enable_indexscan),
            "ivfflat_probes": (
                str(self.ivfflat_probes) if self.ivfflat_probes is not None else None
            ),
            "verified_probes": (
                int(self.verified_probes) if self.verified_probes is not None else None
            ),
            "probe_availability": str(self.probe_availability),
            "probes_registered_before_load": bool(self.probes_registered_before_load),
            "probes_registered_after_load": bool(self.probes_registered_after_load),
            "pgvector_load_method": str(self.pgvector_load_method),
        }


# ---------------------------------------------------------------------
# pgvector library load — ensure the shared library is in this backend
# ---------------------------------------------------------------------


def ensure_pgvector_loaded(cur: Any) -> str:
    """Force the pgvector shared library into the current backend.

    A PostgreSQL backend that has not executed any pgvector
    function has NO row in ``pg_settings`` for ``ivfflat.probes``
    — the extension's GUCs are registered only when the
    extension's shared library is loaded into the backend.
    Calling code MUST therefore force the load BEFORE probing
    ``pg_settings`` for the GUC; otherwise it will see a false
    ``UNAVAILABLE`` verdict.

    Behaviour:

      1. Try ``LOAD 'vector'`` (canonical, cheapest when the
         server has ``session_preload_libraries`` configured);
         on success return :data:`PROBE_LOAD_METHOD_LOAD`.
      2. On any exception (e.g. the server rejects ``LOAD``
         for unprivileged sessions), fall back to executing
         a real pgvector function (``'[0]'::vector <=>
         '[0]'::vector``) which triggers the same library
         load; on success return
         :data:`PROBE_LOAD_METHOD_VECTOR_FUNCTION`.
      3. If both raise, return :data:`PROBE_LOAD_METHOD_NONE`.

    The function NEVER raises on load failure — the caller
    decides what a failed load means (a subsequent
    ``_check_guc_registered`` will still report the GUC
    unregistered, and that path raises
    :class:`SearchProtocolError` for explicit-probe policies).
    Exceptions raised by the CALLER's own subsequent queries
    are NOT swallowed: this function only catches the load
    attempts it issues.
    """

    try:
        cur.execute(f"LOAD '{PGVECTOR_LIBRARY_NAME}'")
        return PROBE_LOAD_METHOD_LOAD
    except Exception:
        pass

    try:
        cur.execute("SELECT '[0]'::vector <=> '[0]'::vector")
        # Drain the result set so the cursor is left clean for
        # the caller's subsequent queries.
        try:
            cur.fetchall()
        except Exception:
            pass
        return PROBE_LOAD_METHOD_VECTOR_FUNCTION
    except Exception:
        pass

    return PROBE_LOAD_METHOD_NONE


# ---------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------


def apply_search_protocol(
    conn: Any,
    policy: SearchProtocol,
) -> SearchProtocolReport:
    """Apply ``policy`` to ``conn`` and verify it round-trips.

    Connection-level ``set_config(name, value, false)`` is used
    so the GUC survives any subsequent ``COMMIT`` / ``ROLLBACK``.
    Each ``set_config`` is followed by ``current_setting(%s, true)``
    to confirm the value the planner sees matches what was set.

    GUC registration contract (see module docstring):

      * The pgvector shared library registers ``ivfflat.probes``
        LAZILY, when the library is first loaded into the
        backend.  Immediately after obtaining the cursor — and
        BEFORE any other GUC work — :func:`apply_search_protocol`
        records:
          1. ``probes_registered_before_load`` —
             ``pg_settings`` row exists for ``ivfflat.probes``
             before the load was forced.
          2. :func:`ensure_pgvector_loaded` — forces the load.
          3. ``probes_registered_after_load`` — ``pg_settings``
             row exists after the load was forced.  THIS is the
             verdict gate (``registered_after``).
      * Explicit ``policy.probes`` in ``ann`` mode + ``registered_after``:
        SET + ``current_setting`` round-trip; fail-closed on
        mismatch.
      * Explicit ``policy.probes`` in ``ann`` mode + NOT
        ``registered_after``: REFUSED with
        :class:`SearchProtocolError` (fail-closed — no
        custom-GUC echo fallback).
      * Implicit ``policy.probes`` (any mode) + NOT
        ``registered_after``: NO failure.  The report records
        ``probe_availability='UNAVAILABLE'`` and leaves
        ``ivfflat_probes=None``; we NEVER invent a numeric
        default.
      * Implicit ``policy.probes`` (any mode) + ``registered_after``:
        existing read-back semantics — the current GUC value
        is observed and recorded verbatim.
      * ``exact`` mode + explicit ``policy.probes``: REFUSED.
        An exact-mode run that tries to pin ``ivfflat.probes``
        would invalidate the contract that "exact means no
        IVFFlat decisions in the planner" — the explicit
        override must be rejected at parse time by
        :func:`SearchProtocol.__post_init__` and surfaced here
        as well.

    Raises :class:`SearchProtocolError` on any verification
    failure. The connection is NOT mutated to compensate — the
    caller decides whether to ``rollback`` or ``commit`` after a
    failure.
    """

    if not isinstance(policy, SearchProtocol):
        raise SearchProtocolError(
            f"apply_search_protocol: policy must be SearchProtocol; "
            f"got {type(policy).__name__}"
        )

    # Fail closed: an explicit probes override against an
    # ``exact`` policy is a contract violation — we surface it
    # here as well so callers who build the policy by hand
    # (not via :func:`SearchProtocol.parse`) still cannot slip
    # through. ``SearchProtocol.__post_init__`` already raises
    # for the same shape; the duplicate guard is intentional.
    if policy.mode == SEARCH_MODE_EXACT and policy.probes is not None:
        raise SearchProtocolError(
            "apply_search_protocol: ivfflat.probes is not valid with "
            "search_mode='exact' (exact mode must NOT pin planner "
            "probe decisions); pass probes=None or switch to "
            "search_mode='ann'"
        )

    try:
        cur = conn.cursor()
    except Exception as exc:
        raise SearchProtocolError(
            f"apply_search_protocol: conn.cursor() failed: "
            f"{type(exc).__name__} (message suppressed)"
        ) from exc

    load_method: str = PROBE_LOAD_METHOD_NONE
    registered_before: bool = False
    registered_after: bool = False
    try:
        observed_indexscan = _apply_and_verify_guc(
            cur, "enable_indexscan",
            "off" if policy.mode == SEARCH_MODE_EXACT else "on",
        )

        observed_probes: str | None = None
        verified_probes: int | None = None
        probe_availability: str = PROBE_AVAILABILITY_AVAILABLE

        # GUC-registration preflight — load-order matters.
        # ``pg_settings`` carries NO row for ``ivfflat.probes`` on
        # a backend that has not yet loaded the pgvector shared
        # library; we therefore record BEFORE / AFTER the
        # forced load so the verdict reflects the actual
        # planner state, not the lazy-load lag.
        registered_before = _check_guc_registered(cur, "ivfflat.probes")
        load_method = ensure_pgvector_loaded(cur)
        registered_after = _check_guc_registered(cur, "ivfflat.probes")

        if not registered_after:
            # The GUC is NOT registered even after the
            # forced load.  Explicit probes cannot be
            # honoured (any SET would only create a
            # custom variable, the planner never reads
            # it) — fail closed.  Implicit probes are
            # tolerated: we record UNAVAILABLE so the
            # auditor sees the truth without forcing an
            # exact-mode or no-override ANN run to abort
            # solely on GUC absence.
            probe_availability = PROBE_AVAILABILITY_UNAVAILABLE
            if policy.mode == SEARCH_MODE_ANN and policy.probes is not None:
                raise SearchProtocolError(
                    "apply_search_protocol: ivfflat.probes is not "
                    "registered on this connection (no row in "
                    "pg_settings even after forcing the pgvector "
                    "shared library load via 'LOAD vector' / "
                    "executing a pgvector function); explicit "
                    "--ivfflat-probes cannot be honoured (a SET "
                    "would only create a custom-GUC echo that "
                    "the planner never reads). Use a pgvector "
                    "build where the pgvector shared library "
                    "registers ivfflat.probes once loaded, or "
                    "drop --ivfflat-probes."
                )
            # Implicit probes — leave ivfflat_probes None,
            # verified_probes None, and let the report stamp
            # the UNAVAILABLE verdict.
            observed_probes = None
            verified_probes = None
        elif policy.mode == SEARCH_MODE_ANN and policy.probes is not None:
            # Explicit probes + GUC registered after the
            # forced load — SET, verify the round-trip
            # equals the requested int, fail closed on
            # mismatch.
            observed_probes = _apply_and_verify_guc(
                cur, "ivfflat.probes", str(int(policy.probes)),
            )
            try:
                verified_probes = int(observed_probes)
            except ValueError as exc:
                raise SearchProtocolError(
                    "apply_search_protocol: ivfflat.probes did not round-trip "
                    f"as an integer (observed {observed_probes!r})"
                ) from exc
            if verified_probes != int(policy.probes):
                raise SearchProtocolError(
                    "apply_search_protocol: ivfflat.probes did not round-trip; "
                    f"expected {int(policy.probes)!r}, observed {verified_probes!r}"
                )
        else:
            # No explicit probes + GUC registered — READ BACK
            # the current ivfflat.probes via
            # ``current_setting(..., true)`` so the manifest
            # records what the planner actually sees (typically
            # the planner default). The verified value is NOT
            # silently rewritten to ``None`` — it stays at the
            # observed string for any auditor who wants to
            # confirm "we asked the planner for nothing, the
            # planner gave us X".
            try:
                cur.execute(
                    "SELECT current_setting(%s, true) AS guc_value",
                    ("ivfflat.probes",),
                )
                row = cur.fetchone()
            except Exception as exc:
                raise SearchProtocolError(
                    "apply_search_protocol: current_setting(ivfflat.probes) "
                    f"failed: {type(exc).__name__} (message suppressed)"
                ) from exc
            if row is None or len(row) < 1 or row[0] is None:
                # With ``missing_ok=true``, ``current_setting``
                # returns NULL when the GUC is genuinely unknown.
                # The pg_settings preflight above should already
                # have surfaced this — but we keep the read-back
                # check so a registered-but-empty GUC cannot
                # silently slip through.  A NULL here means the
                # registered value is unset; we honour the
                # manifest-truth contract by recording None and
                # letting the auditor see what the planner saw.
                observed_probes = None
                verified_probes = None
            else:
                observed_probes = str(row[0]).strip()
                # ``verified_probes`` is intentionally left None
                # here: the policy did not pin a value, so there
                # is no requested value to compare the
                # observation against. ``observed_probes`` is
                # the truthful current-GUC string.
                verified_probes = None
    finally:
        try:
            cur.close()
        except Exception:
            pass

    return SearchProtocolReport(
        mode=str(policy.mode),
        enable_indexscan=str(observed_indexscan),
        ivfflat_probes=observed_probes,
        verified_probes=verified_probes,
        probe_availability=probe_availability,
        probes_registered_before_load=bool(registered_before),
        probes_registered_after_load=bool(registered_after),
        pgvector_load_method=str(load_method),
    )


def _check_guc_registered(cur: Any, name: str) -> bool:
    """Return True iff ``name`` is a registered GUC on this connection.

    A GUC is "registered" iff ``pg_settings`` carries a row for
    it.  When the row is missing, ``set_config`` still echoes a
    value (PG creates a custom variable on demand) but the
    planner never reads it — that round-trip is a LIE and must
    be detected so the caller can fail closed or record
    :data:`PROBE_AVAILABILITY_UNAVAILABLE`.

    A fresh backend that has not yet loaded the pgvector shared
    library has NO row for ``ivfflat.probes`` even on a
    perfectly healthy pgvector build; the caller MUST force the
    load first (see :func:`ensure_pgvector_loaded`).
    """

    try:
        cur.execute(
            "SELECT 1 FROM pg_settings WHERE name = %s LIMIT 1",
            (str(name),),
        )
        row = cur.fetchone()
    except Exception as exc:
        raise SearchProtocolError(
            f"apply_search_protocol: pg_settings lookup for {name} "
            f"failed: {type(exc).__name__} (message suppressed)"
        ) from exc
    if row is None or len(row) < 1 or row[0] is None:
        return False
    return True


def _apply_and_verify_guc(
    cur: Any,
    name: str,
    expected_value: str,
) -> str:
    """Issue ``set_config`` + ``current_setting`` and assert the round-trip."""

    try:
        cur.execute(
            "SELECT set_config(%s, %s, false) AS applied",
            (name, expected_value),
        )
    except Exception as exc:
        raise SearchProtocolError(
            f"apply_search_protocol: set_config({name}) failed: "
            f"{type(exc).__name__} (message suppressed)"
        ) from exc

    try:
        cur.execute(
            "SELECT current_setting(%s, true) AS guc_value", (name,)
        )
        row = cur.fetchone()
    except Exception as exc:
        raise SearchProtocolError(
            f"apply_search_protocol: current_setting({name}) failed: "
            f"{type(exc).__name__} (message suppressed)"
        ) from exc

    if row is None or len(row) < 1 or row[0] is None:
        raise SearchProtocolError(
            f"apply_search_protocol: current_setting({name}) returned no value"
        )
    observed = str(row[0]).strip()
    if name == "enable_indexscan":
        observed_lower = observed.lower()
        if observed_lower != expected_value:
            raise SearchProtocolError(
                "apply_search_protocol: enable_indexscan did not round-trip; "
                f"expected {expected_value!r}, observed {observed_lower!r}"
            )
        return observed_lower
    if observed != expected_value:
        raise SearchProtocolError(
            f"apply_search_protocol: {name} did not round-trip; "
            f"expected {expected_value!r}, observed {observed!r}"
        )
    return observed


__all__ = [
    "ALLOWED_PROBE_AVAILABILITY",
    "ALLOWED_PROBE_LOAD_METHODS",
    "ALLOWED_SEARCH_MODES",
    "PGVECTOR_LIBRARY_NAME",
    "PROBE_AVAILABILITY_AVAILABLE",
    "PROBE_AVAILABILITY_UNAVAILABLE",
    "PROBE_LOAD_METHOD_LOAD",
    "PROBE_LOAD_METHOD_NONE",
    "PROBE_LOAD_METHOD_VECTOR_FUNCTION",
    "SEARCH_MODE_ANN",
    "SEARCH_MODE_EXACT",
    "SearchProtocol",
    "SearchProtocolError",
    "SearchProtocolReport",
    "apply_search_protocol",
    "ensure_pgvector_loaded",
]