"""``repair --dry-run`` planner (DESIGN §9).

Pure mapping from a ``list[Diagnosis]`` to a deterministic
``list[RepairAction]``. **No data collection** — counts come from
``diagnosis.evidence`` only. **No second-PG pass**, no filesystem
mutation. The planner never touches a connection; tests inject a
recording fake PG and assert the planner's call graph is empty.

Hard contracts:

  * **Ten fields per action.** Every action carries the full §9 set
    (action_id, issue_code, target_count, risk, reversible,
    requires_provider, estimated_remote_calls, estimated_cost,
    writes_database, automatic_safe, reason) — ``estimated_cost`` may
    be ``None``.
  * **Deterministic order.** actions sorted by risk desc
    (high > medium > low) then action_id.
  * **Dedup by ``action_id``.** When multiple diagnoses map to the
    same ``action_id`` (e.g. recent+debt → REBUILD_EMBEDDING_FOR_QA_IDS),
    the issue_codes list is concatenated and ``target_count`` is the
    max of the contributing counts.
  * **LONG_QA_CHUNK_INCONSISTENT with orphan parents → NO_AUTOMATIC_REPAIR
    SOURCE_MISSING.** The data layer cannot reconstruct the parent row
    that the orphan children reference — needs a source-of-truth
    recovery check first.
  * **repair --apply is impossible.** ``repair_apply_disabled()`` is
    the only authorized entry point for a write — and it always
    raises ``NotImplementedError(REPAIR_APPLY_NOT_IMPLEMENTED_IN_V1)``.
"""
from __future__ import annotations

from typing import Any, Iterable

from .models import Diagnosis, RepairAction


# Hard-disabled sentinel — referenced by cli.py and the test suite.
REPAIR_APPLY_NOT_IMPLEMENTED = "REPAIR_APPLY_NOT_IMPLEMENTED_IN_V1"


# Risk ordering helpers.
_RISK_ORDER = {"high": 0, "medium": 1, "low": 2}


def repair_apply_disabled() -> None:
    """The only v1 entry point for *applying* a repair.

    Hard-disabled per DESIGN §9: ``repair --apply`` would touch the
    write pipeline, which is explicitly out-of-scope for this round.
    Callers should not exist; this function exists so that grep-locked
    tests can prove nothing reaches it.
    """
    raise NotImplementedError(REPAIR_APPLY_NOT_IMPLEMENTED)


# ── Mapping tables (DESIGN §9) ─────────────────────────────────────────


# action_id -> base risk / requires_provider / writes_database / reversible /
#              automatic_safe / reason template.
# ``issue_codes`` is the set of Diagnosis codes that map to the action;
# ``target_count_keys`` are the evidence fields to read for the count.
_ACTION_BASE: dict[str, dict[str, Any]] = {
    "RUN_SCHEMA_UPGRADE": {
        "risk": "low",
        "reversible": True,
        "requires_provider": False,
        "writes_database": True,
        "automatic_safe": False,  # gate: needs v0.2 upgrade artifact + DSN opt-in.
        "reason": "Run hippocampus upgrade --dry-run / --apply to add the "
                  "missing canonical objects; additive and idempotent.",
        "issue_codes": ("SCHEMA_VERSION_MISMATCH", "SCHEMA_TABLE_MISSING"),
        "target_count_keys": (),
        "estimated_cost_keys": (),
    },
    "RECREATE_MISSING_INDEX": {
        "risk": "low",
        "reversible": True,
        "requires_provider": False,
        "writes_database": True,
        "automatic_safe": False,
        "reason": "CREATE INDEX for the missing canonical index(es); "
                  "additive, non-destructive.",
        "issue_codes": ("SCHEMA_INDEX_MISSING",),
        "target_count_keys": ("missing", "missing_indexes"),
        "estimated_cost_keys": (),
    },
    "REBUILD_EMBEDDING_FOR_QA_IDS": {
        "risk": "medium",
        "reversible": False,
        "requires_provider": True,
        "writes_database": True,
        "automatic_safe": False,  # v1: provider calls are gated.
        "reason": "Re-embed the affected qa rows; targets the recent "
                  "incident count when applicable, otherwise the historical "
                  "debt count.",
        "issue_codes": (
            "RECENT_EMBEDDING_FAILURE",
            "HISTORICAL_EMBEDDING_DEBT",
        ),
        "target_count_keys": (
            "recent_null", "historical_null", "embedding_null_total",
        ),
        "estimated_cost_keys": ("target_count",),
    },
    "RETRY_PENDING_FAILURES": {
        "risk": "medium",
        "reversible": True,
        "requires_provider": True,
        "writes_database": True,
        "automatic_safe": False,
        "reason": "Re-dispatch the retryable failure markers; one "
                  "provider call per record. Bounded by the retryable+stale "
                  "count.",
        "issue_codes": (
            "RECENT_FAILURE_MARKER",
            "POISONED_FAILURE_MARKER",
            "RETRY_EXHAUSTED",
            "STALE_PENDING_MARKER",
            # Transient provider failures are retryable-class: the source
            # data is intact, only the provider call failed — the repair
            # path is "re-dispatch once the provider recovers" (diagnose
            # marks these repairable=True on purpose).
            "EMBED_PROVIDER_RATE_LIMIT",
            "EMBED_PROVIDER_TIMEOUT",
        ),
        "target_count_keys": (
            "current_active", "current_retrying", "current_poisoned",
            "total_poisoned", "stale", "malformed",
        ),
        "estimated_cost_keys": ("target_count",),
    },
    "REPAIR_EXPLICIT_MEMORY_EMBEDDING": {
        "risk": "low",
        "reversible": False,
        "requires_provider": True,
        "writes_database": True,
        "automatic_safe": False,
        "reason": "Re-embed the explicit_memories rows with NULL embedding.",
        "issue_codes": ("EXPLICIT_MEMORY_EMBEDDING_NULL",),
        "target_count_keys": ("explicit_embedding_null",),
        "estimated_cost_keys": ("target_count",),
    },
    "REBUILD_LONG_QA_CHILDREN": {
        "risk": "medium",
        "reversible": False,
        "requires_provider": False,
        "writes_database": True,
        "automatic_safe": False,
        "reason": "Repair or remove the inconsistent qa_embedding_chunks "
                  "rows.",
        "issue_codes": ("LONG_QA_CHUNK_INCONSISTENT",),
        "target_count_keys": (
            "parents_missing_parent_row", "child_null_embedding",
            "child_bad_offsets", "child_duplicate_keys",
        ),
        "estimated_cost_keys": (),
    },
    "FIX_PROVIDER_CREDENTIALS": {
        "risk": "low",
        "reversible": True,
        "requires_provider": False,
        "writes_database": False,  # a credentials update never writes the DB.
        "automatic_safe": False,   # manual-only by contract.
        "reason": "Rotate / repair the embedding provider credentials; "
                  "no DB writes.",
        "issue_codes": ("EMBED_PROVIDER_AUTH",),
        "target_count_keys": (),
        "estimated_cost_keys": (),
    },
    "MANUAL_REVIEW_EMPTY_ANSWER": {
        "risk": "low",
        "reversible": True,
        "requires_provider": False,
        "writes_database": False,
        "automatic_safe": False,
        "reason": "Manual review of empty-answer qa rows; needs source-of-"
                  "truth recovery check before any data mutation.",
        "issue_codes": ("EMPTY_ANSWER_RECENT", "EMPTY_ANSWER_HISTORICAL_DEBT"),
        "target_count_keys": ("empty_answer_recent", "empty_answer_total",
                              "historical_empty"),
        "estimated_cost_keys": (),
    },
    "NO_AUTOMATIC_REPAIR_SOURCE_MISSING": {
        "risk": "high",
        "reversible": False,
        "requires_provider": False,
        "writes_database": False,
        "automatic_safe": False,
        "reason": "Source of truth cannot be reconstructed from the current "
                  "data; automatic repair is unsafe. Manual operator review "
                  "required.",
        "issue_codes": (),  # injected by the planner on hard cases.
        "target_count_keys": (),
        "estimated_cost_keys": (),
    },
    "MANUAL_REVIEW_MALFORMED_MARKER": {
        "risk": "low",
        "reversible": False,
        "requires_provider": False,
        "writes_database": False,
        "automatic_safe": False,
        "reason": "Inspect the malformed marker files manually; the reader "
                  "could not parse them.",
        "issue_codes": ("MALFORMED_FAILURE_MARKER",),
        "target_count_keys": ("malformed", "current_malformed"),
        "estimated_cost_keys": (),
    },
    "ALIGN_ACTIVE_RUNTIME_TO_APPROVED_RELEASE": {
        "risk": "high",
        "reversible": True,
        "requires_provider": False,
        "writes_database": False,
        "automatic_safe": False,
        "reason": "Align the ACTUALLY LOADED environment with the approved "
                  "artifact: install approved wheels into the live env, "
                  "restart serve+gateway, re-verify via doctor --runtime. "
                  "Rollback: restore backup + restart. No DB writes.",
        "issue_codes": ("RUNTIME_SHADOWED_INSTALL", "RUNTIME_RELEASE_MISMATCH"),
        "target_count_keys": (),
        "estimated_cost_keys": (),
    },
}


# Pre-computed inverse lookup: issue_code -> action_id. Built once at import.
_CODE_TO_ACTION: dict[str, str] = {}
for _aid, _meta in _ACTION_BASE.items():
    for _c in _meta["issue_codes"]:
        # If a code collides (rare), keep the first; the planner also
        # checks ``LONG_QA_CHUNK_INCONSISTENT`` specially.
        _CODE_TO_ACTION.setdefault(_c, _aid)


# Codes that never produce an automatic action (manual / out-of-scope).
_MANUAL_ONLY_CODES = frozenset({
    "RUNTIME_EDITABLE_ACTIVE",
    "RUNTIME_UNKNOWN_IMPORT",
    "PG_UNREACHABLE",
    "PGVECTOR_MISSING",
    "OBSERVER_STALE",
    "DERIVED_LAYER_STALE",
    "PROVIDER_UNCONFIGURED",
})


# ── Helpers ────────────────────────────────────────────────────────────


def _evidence_count(evidence: dict[str, Any], keys: tuple[str, ...]) -> int:
    """Read the first non-zero matching count from evidence. Returns 0
    when no matching key is present, or when the value is not coercible
    to int."""
    for k in keys:
        v = evidence.get(k) if isinstance(evidence, dict) else None
        if v is None:
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            return iv
    return 0


def _max_evidence_count(evidence_list: Iterable[dict[str, Any]],
                        keys: tuple[str, ...]) -> int:
    """Return the maximum count across a list of evidence dicts."""
    best = 0
    for ev in evidence_list:
        best = max(best, _evidence_count(ev, keys))
    return best


def _has_orphan_parents(diagnosis: Diagnosis) -> bool:
    if diagnosis.code != "LONG_QA_CHUNK_INCONSISTENT":
        return False
    ev = diagnosis.evidence or {}
    try:
        return int(ev.get("parents_missing_parent_row", 0)) > 0
    except (TypeError, ValueError):
        return False


def _sort_key(a: RepairAction) -> tuple[int, str]:
    return (_RISK_ORDER.get(a.risk, 99), a.action_id)


# ── Public entry point ───────────────────────────────────────────────


def plan_repairs(diagnoses: list[Diagnosis]) -> list[RepairAction]:
    """Map a ``list[Diagnosis]`` to a deterministic ``list[RepairAction]``.

    The mapping is pure:

      * each Diagnosis contributes to exactly one action (its code's
        mapped action) **except** ``LONG_QA_CHUNK_INCONSISTENT`` with
        orphan parents, which routes to ``NO_AUTOMATIC_REPAIR_SOURCE_
        MISSING`` instead of ``REBUILD_LONG_QA_CHILDREN``;
      * actions are deduped by ``action_id``: ``issue_code`` becomes the
        joined ``[code1, code2, ...]`` of contributors; ``target_count``
        is the max of the contributing counts;
      * output is sorted by ``risk`` desc (high → low) then ``action_id``.

    ``repair_apply_disabled()`` is *not* called here — the planner is
    strictly read-only and produces a plan only.
    """
    # Group diagnoses by action_id (after the orphan-parent special case).
    by_action: dict[str, list[Diagnosis]] = {}
    for d in diagnoses:
        if d.code in _MANUAL_ONLY_CODES:
            # No automatic action — these issues are reported but never
            # promoted into the action list. Tests cover this branch.
            continue

        if _has_orphan_parents(d):
            action_id = "NO_AUTOMATIC_REPAIR_SOURCE_MISSING"
        else:
            action_id = _CODE_TO_ACTION.get(d.code)
            if action_id is None:
                # Unknown code — surface as a manual review with no target.
                action_id = "NO_AUTOMATIC_REPAIR_SOURCE_MISSING"

        by_action.setdefault(action_id, []).append(d)

    actions: list[RepairAction] = []
    for action_id, contribs in by_action.items():
        base = _ACTION_BASE.get(action_id)
        if base is None:
            # Defensive: unknown action_id (should never happen). Skip.
            continue

        # target_count: max across contributing evidence keys.
        target_count = _max_evidence_count(
            [d.evidence or {} for d in contribs],
            base["target_count_keys"],
        )

        # issue_code: union of contributing codes, deterministic order.
        seen: list[str] = []
        seen_set: set[str] = set()
        for d in contribs:
            if d.code not in seen_set:
                seen.append(d.code)
                seen_set.add(d.code)
        issue_code_str = ",".join(seen)

        # estimated_remote_calls: target_count for provider-bound actions
        # that have a numeric cost; None otherwise.
        estimated_calls: int | None = None
        if (
            base["requires_provider"]
            and "target_count" in base["estimated_cost_keys"]
            and target_count > 0
        ):
            estimated_calls = target_count
        elif not base["requires_provider"]:
            # Non-provider actions (schema, index, manual review) → no
            # remote calls. ``None`` keeps the field JSON-safe and
            # distinguishable from a zero-call estimate.
            estimated_calls = None

        # estimated_cost: dict when we have a numeric estimate, else None.
        estimated_cost: dict[str, Any] | None = None
        if estimated_calls is not None and estimated_calls > 0:
            # Per-call estimate placeholder — the planner is honest about
            # not knowing the real per-call USD without a live provider.
            estimated_cost = {
                "calls": estimated_calls,
                "per_call_usd_estimate": None,
                "currency": "USD",
                "note": "v1: estimate is call-count only; per-call cost is "
                        "provider-specific.",
            }

        # reason: if multiple contributors, append the contributing codes.
        if len(seen) > 1:
            reason = base["reason"] + " (combines: " + ",".join(seen) + ")"
        else:
            reason = base["reason"]

        actions.append(RepairAction(
            action_id=action_id,
            issue_code=issue_code_str,
            target_count=int(target_count),
            risk=base["risk"],
            reversible=bool(base["reversible"]),
            requires_provider=bool(base["requires_provider"]),
            estimated_remote_calls=estimated_calls,
            estimated_cost=estimated_cost,
            writes_database=bool(base["writes_database"]),
            automatic_safe=bool(base["automatic_safe"]),
            reason=sanitize_text(reason),
        ))

    actions.sort(key=_sort_key)
    return actions


# sanitize_text is imported lazily to avoid a top-level cycle if a
# future refactor pulls redaction into models. Local helper:
from .redaction import sanitize_text  # noqa: E402  (intentional bottom-of-file import)


__all__ = ["plan_repairs", "repair_apply_disabled", "REPAIR_APPLY_NOT_IMPLEMENTED"]