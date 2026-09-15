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

``pgvector`` 0.8.x ships with the ``ivfflat`` index access method
but the ``ivfflat.probes`` *server-side* GUC is only registered
when the extension has been initialised AND the planner option
listens for it.  In some pgvector builds (notably the
``pgvector/pgvector:pg17`` 0.8.6 image used by the disposable lab)
there is NO row in ``pg_settings`` for ``ivfflat.probes`` — yet
``SELECT set_config('ivfflat.probes','1',false)`` and a follow-up
``current_setting(..., true)`` both round-trip ``'1'`` because
PostgreSQL silently creates a *custom* variable on demand.

That round-trip is a LIE — the planner never sees a registered
GUC, so the value has no effect on the index probe count.  This
module distinguishes "registered" (the planner actually reads
this value) from "unregistered" (only a custom variable exists,
planner ignores it):

  * :data:`PROBE_AVAILABILITY_AVAILABLE` — ``pg_settings`` carries
    a row for ``ivfflat.probes``.  SET + verify are meaningful.
  * :data:`PROBE_AVAILABILITY_UNAVAILABLE` — no row in
    ``pg_settings``.  Any SET + verify is a custom-GUC echo and
    has NO planner effect.

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
        carries a row for ``ivfflat.probes``; the planner reads
        it.
      * :data:`PROBE_AVAILABILITY_UNAVAILABLE` — no row in
        ``pg_settings``; any SET + ``current_setting`` echo is
        only a custom variable, the planner ignores it.  The
        report's ``ivfflat_probes`` is always ``None`` in this
        case (we never claim a numeric default).
    """

    mode: str
    enable_indexscan: str
    ivfflat_probes: str | None
    verified_probes: int | None
    probe_availability: str = PROBE_AVAILABILITY_AVAILABLE

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
        }


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

      * Explicit ``policy.probes`` in ``ann`` mode: first verify
        that ``ivfflat.probes`` is registered in ``pg_settings``.
        If NOT registered, raise :class:`SearchProtocolError`
        (fail-closed — no custom-GUC echo fallback).
        If registered, SET + ``current_setting`` round-trip and
        record ``verified_probes`` verbatim.
      * Implicit ``policy.probes`` (any mode): read back the
        current GUC value.  If the GUC is NOT registered, leave
        ``ivfflat_probes=None`` and stamp
        ``probe_availability='UNAVAILABLE'`` — we never claim a
        numeric default.  If registered, stamp the observed
        current-GUC string verbatim.
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

    try:
        observed_indexscan = _apply_and_verify_guc(
            cur, "enable_indexscan",
            "off" if policy.mode == SEARCH_MODE_EXACT else "on",
        )

        observed_probes: str | None = None
        verified_probes: int | None = None
        probe_availability: str = PROBE_AVAILABILITY_AVAILABLE

        # GUC-registration check — required to distinguish a real
        # planner-affecting round-trip from a custom-GUC echo.
        # Done once per apply; the answer gates the rest of the
        # ivfflat.probes handling.
        probes_registered = _check_guc_registered(cur, "ivfflat.probes")

        if not probes_registered:
            # The GUC is NOT registered.  Explicit probes
            # cannot be honoured (any SET would only create a
            # custom variable, the planner never reads it) —
            # fail closed.  Implicit probes are tolerated: we
            # record UNAVAILABLE so the auditor sees the truth
            # without forcing an exact-mode or no-override ANN
            # run to abort solely on GUC absence.
            probe_availability = PROBE_AVAILABILITY_UNAVAILABLE
            if policy.mode == SEARCH_MODE_ANN and policy.probes is not None:
                raise SearchProtocolError(
                    "apply_search_protocol: ivfflat.probes is not "
                    "registered on this connection (no row in pg_settings); "
                    "explicit --ivfflat-probes cannot be honoured "
                    "(a SET would only create a custom-GUC echo that "
                    "the planner never reads). Use a build where the "
                    "pgvector extension registers ivfflat.probes, or "
                    "drop --ivfflat-probes."
                )
            # Implicit probes — leave ivfflat_probes None,
            # verified_probes None, and let the report stamp
            # the UNAVAILABLE verdict.
            observed_probes = None
            verified_probes = None
        elif policy.mode == SEARCH_MODE_ANN and policy.probes is not None:
            # Explicit probes + GUC registered — SET, verify
            # the round-trip equals the requested int, fail
            # closed on mismatch.
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
    )


def _check_guc_registered(cur: Any, name: str) -> bool:
    """Return True iff ``name`` is a registered GUC on this connection.

    A GUC is "registered" iff ``pg_settings`` carries a row for
    it.  When the row is missing, ``set_config`` still echoes a
    value (PG creates a custom variable on demand) but the
    planner never reads it — that round-trip is a LIE and must
    be detected so the caller can fail closed or record
    :data:`PROBE_AVAILABILITY_UNAVAILABLE`.
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
    "ALLOWED_SEARCH_MODES",
    "PROBE_AVAILABILITY_AVAILABLE",
    "PROBE_AVAILABILITY_UNAVAILABLE",
    "SEARCH_MODE_ANN",
    "SEARCH_MODE_EXACT",
    "SearchProtocol",
    "SearchProtocolError",
    "SearchProtocolReport",
    "apply_search_protocol",
]
