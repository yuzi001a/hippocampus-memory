"""``diagnose`` classifier (DESIGN §8).

Pure function from a ``HealthReport`` to a deterministic ``list[Diagnosis]``.

Hard contracts (test-locked):

  * **No second data pass.** Only ``report.checks`` is consulted; the
    classifier never re-queries the DB / filesystem.
  * **Severity sort.** ``error`` > ``warning`` > ``info``; ties broken
    alphabetically by ``code`` (deterministic).
  * **Historical debt vs current incident (双轨).** A row count from
    MW02/MW03 is split into a ``recent`` half and a historical half;
    only the recent half promotes the overall verdict. Locked by test:

        - ``embedding_null_total>0 and recent==0`` →
              ``HISTORICAL_EMBEDDING_DEBT (info)`` only — never
              ``RECENT_EMBEDDING_FAILURE``.
        - ``recent_null>0`` → ``RECENT_EMBEDDING_FAILURE (error)``
              AND the debt info is preserved when ``total > recent``.
        - MW03 empty-answer behaves the same way.

  * **Skip = no issue, unknown = warn.** A check marked ``skip`` never
    produces a diagnosis; ``unknown`` (a check that could not be read)
    falls through to the relevant code's warning level when one exists.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from .models import (
    CheckResult,
    Diagnosis,
    STATUS_FAIL,
    STATUS_SKIP,
    STATUS_UNKNOWN,
    STATUS_WARN,
)
from .redaction import sanitize_text


# ── Severity ordering helpers ──────────────────────────────────────────

_SEV_ORDER = {"error": 0, "warning": 1, "info": 2}


def _sort_key(d: Diagnosis) -> tuple[int, str]:
    """Stable, deterministic sort key: severity asc (error first),
    then code alphabetically."""
    return (_SEV_ORDER.get(d.severity, 99), d.code)


def _find(report, check_id: str) -> CheckResult | None:
    for c in report.checks:
        if c.check_id == check_id:
            return c
    return None


def _ev(check: CheckResult | None) -> dict[str, Any]:
    return (check.evidence if check is not None else {}) or {}


def _evidence_int(evidence: dict[str, Any], *keys: str) -> int:
    """Read the first matching evidence key, coercing to int (0 on missing).
    ``check.evidence`` is JSON-safe already."""
    for k in keys:
        v = evidence.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0
    return 0


# ── Issue builders ─────────────────────────────────────────────────────


def _diag(
    code: str,
    severity: str,
    scope: str,
    summary: str,
    evidence: dict[str, Any],
    repairable: bool,
) -> Diagnosis:
    return Diagnosis(
        code=code,
        severity=severity,
        scope=scope,
        summary=sanitize_text(summary),
        evidence=evidence,
        repairable=bool(repairable),
    )


def _no_diagnosis() -> Diagnosis | None:
    return None


# ── Section classifiers ───────────────────────────────────────────────


def _classify_runtime(report) -> list[Diagnosis]:
    out: list[Diagnosis] = []
    rt01 = _find(report, "RT01_import_source")
    if rt01 is None or rt01.status == STATUS_SKIP:
        return out
    kind = _ev(rt01).get("kind")
    if rt01.status == STATUS_FAIL and kind == "editable":
        out.append(_diag(
            "RUNTIME_EDITABLE_ACTIVE", "error", "runtime",
            "v3core loaded from an editable install — production contract is wheel-only",
            {"kind": kind}, repairable=False,
        ))
    elif rt01.status == STATUS_WARN and kind == "unknown":
        out.append(_diag(
            "RUNTIME_UNKNOWN_IMPORT", "warning", "runtime",
            "v3core import source not recognized",
            {"kind": kind}, repairable=False,
        ))
    elif rt01.status == STATUS_UNKNOWN:
        out.append(_diag(
            "RUNTIME_UNKNOWN_IMPORT", "warning", "runtime",
            "v3core import source unreadable",
            {"kind": kind}, repairable=False,
        ))
    return out


def _classify_storage(report) -> list[Diagnosis]:
    out: list[Diagnosis] = []
    st01 = _find(report, "ST01_pg_reachable")
    st02 = _find(report, "ST02_pgvector")
    st03 = _find(report, "ST03_schema_ledger")
    st04 = _find(report, "ST04_canonical_tables")
    st05 = _find(report, "ST05_canonical_indexes")

    if st01 is not None and st01.status == STATUS_FAIL:
        out.append(_diag(
            "PG_UNREACHABLE", "error", "storage",
            "postgres unreachable", _ev(st01), repairable=False,
        ))
    if st02 is not None and st02.status == STATUS_FAIL:
        out.append(_diag(
            "PGVECTOR_MISSING", "error", "storage",
            "pgvector extension missing", _ev(st02), repairable=False,
        ))
    if st03 is not None and st03.status == STATUS_FAIL:
        out.append(_diag(
            "SCHEMA_VERSION_MISMATCH", "error", "storage",
            "schema_versions ledger missing expected row",
            _ev(st03), repairable=True,
        ))
    if st04 is not None and st04.status == STATUS_FAIL:
        out.append(_diag(
            "SCHEMA_TABLE_MISSING", "error", "storage",
            "canonical tables missing",
            _ev(st04), repairable=True,
        ))
    if st05 is not None and st05.status == STATUS_WARN:
        out.append(_diag(
            "SCHEMA_INDEX_MISSING", "warning", "storage",
            "canonical indexes missing",
            _ev(st05), repairable=True,
        ))
    elif st05 is not None and st05.status == STATUS_UNKNOWN:
        out.append(_diag(
            "SCHEMA_INDEX_MISSING", "warning", "storage",
            "canonical indexes status unreadable",
            _ev(st05), repairable=True,
        ))
    return out


def _classify_memory_write(report) -> list[Diagnosis]:
    """MW01..MW06 — current-incident vs historical-debt 双轨."""
    out: list[Diagnosis] = []

    mw01 = _find(report, "MW01_write_pipeline_recent")
    mw02 = _find(report, "MW02_embedding_debt")
    mw03 = _find(report, "MW03_empty_answer")
    mw05 = _find(report, "MW05_explicit_memory_embedding")
    mw06 = _find(report, "MW06_longqa_child_consistency")

    mw01_ev = _ev(mw01)
    mw02_ev = _ev(mw02)
    mw03_ev = _ev(mw03)
    mw05_ev = _ev(mw05)
    mw06_ev = _ev(mw06)

    recent_qa = _evidence_int(mw01_ev, "recent_qa")
    recent_null = _evidence_int(mw01_ev, "recent_embedding_null")
    recent_empty = _evidence_int(mw01_ev, "recent_empty_answer")
    null_total = _evidence_int(mw02_ev, "embedding_null_total")
    empty_total = _evidence_int(mw03_ev, "empty_answer_total")
    empty_recent = _evidence_int(mw03_ev, "empty_answer_recent")

    # ── MW01: current incident → RECENT_EMBEDDING_FAILURE (error)
    #   + keep the debt info when total > recent.
    if mw01 is not None and mw01.status == STATUS_FAIL and recent_null > 0:
        out.append(_diag(
            "RECENT_EMBEDDING_FAILURE", "error", "memory_write",
            f"{recent_null} recent qa row(s) with NULL embedding",
            {"recent_null": recent_null, "recent_qa": recent_qa,
             "window_hours": _evidence_int(mw01_ev, "window_hours")},
            repairable=True,
        ))
        # Debt info preserved (双轨) when total > recent (i.e. legacy NULLs exist).
        historical_null_debt = max(0, null_total - recent_null)
        if historical_null_debt > 0:
            out.append(_diag(
                "HISTORICAL_EMBEDDING_DEBT", "info", "memory_write",
                f"{historical_null_debt} historical qa row(s) with NULL embedding (debt)",
                {"embedding_null_total": null_total,
                 "recent_null": recent_null,
                 "historical_null": historical_null_debt,
                 "oldest_null_created_at": mw02_ev.get("oldest_null_created_at"),
                 "newest_null_created_at": mw02_ev.get("newest_null_created_at")},
                repairable=True,
            ))

    # ── MW02: historical debt only (no recent incident) → info.
    elif null_total > 0 and recent_null == 0:
        out.append(_diag(
            "HISTORICAL_EMBEDDING_DEBT", "info", "memory_write",
            f"{null_total} historical qa row(s) with NULL embedding (debt)",
            {"embedding_null_total": null_total,
             "recent_null": 0,
             "oldest_null_created_at": mw02_ev.get("oldest_null_created_at"),
             "newest_null_created_at": mw02_ev.get("newest_null_created_at")},
            repairable=True,
        ))

    # ── MW03: empty-answer 双轨.
    if recent_empty > 0:
        # New incident in window.
        out.append(_diag(
            "EMPTY_ANSWER_RECENT", "warning", "memory_write",
            f"{recent_empty} recent qa row(s) with empty answer",
            {"empty_answer_recent": recent_empty,
             "window_hours": _evidence_int(mw01_ev, "window_hours")},
            repairable=False,
        ))
        historical_empty_debt = max(0, empty_total - recent_empty)
        if historical_empty_debt > 0:
            out.append(_diag(
                "EMPTY_ANSWER_HISTORICAL_DEBT", "info", "memory_write",
                f"{historical_empty_debt} historical qa row(s) with empty answer (debt)",
                {"empty_answer_total": empty_total,
                 "empty_answer_recent": recent_empty,
                 "historical_empty": historical_empty_debt},
                repairable=False,
            ))
    elif empty_total > 0 and empty_recent == 0:
        # Pure historical debt (双轨：info, needs-source check).
        out.append(_diag(
            "EMPTY_ANSWER_HISTORICAL_DEBT", "info", "memory_write",
            f"{empty_total} historical qa row(s) with empty answer (debt)",
            {"empty_answer_total": empty_total, "empty_answer_recent": 0},
            repairable=False,
        ))

    # ── MW05: explicit memory NULL embeddings → warning.
    explicit_null = _evidence_int(mw05_ev, "explicit_embedding_null")
    if mw05 is not None and mw05.status == STATUS_WARN and explicit_null > 0:
        out.append(_diag(
            "EXPLICIT_MEMORY_EMBEDDING_NULL", "warning", "memory_write",
            f"{explicit_null} explicit memory row(s) with NULL embedding",
            mw05_ev, repairable=True,
        ))
    elif mw05 is not None and mw05.status == STATUS_UNKNOWN and explicit_null == 0:
        out.append(_diag(
            "EXPLICIT_MEMORY_EMBEDDING_NULL", "warning", "memory_write",
            "explicit memory embedding status unreadable",
            mw05_ev, repairable=True,
        ))

    # ── MW06: long-QA chunk integrity.
    if mw06 is not None and mw06.status == STATUS_FAIL:
        parents_missing = _evidence_int(mw06_ev, "parents_missing_parent_row")
        child_null = _evidence_int(mw06_ev, "child_null_embedding")
        bad_offsets = _evidence_int(mw06_ev, "child_bad_offsets")
        dup_keys = _evidence_int(mw06_ev, "child_duplicate_keys")
        # Orphan parents / null child embeddings are the most severe.
        severity = "error" if (parents_missing > 0 or child_null > 0) else "warning"
        out.append(_diag(
            "LONG_QA_CHUNK_INCONSISTENT", severity, "memory_write",
            "qa_embedding_chunks integrity violation",
            {"parents_missing_parent_row": parents_missing,
             "child_null_embedding": child_null,
             "child_bad_offsets": bad_offsets,
             "child_duplicate_keys": dup_keys,
             "child_rows": _evidence_int(mw06_ev, "child_rows"),
             "distinct_parents": _evidence_int(mw06_ev, "distinct_parents")},
            repairable=True,
        ))
    return out


def _classify_failure_accounting(report) -> list[Diagnosis]:
    out: list[Diagnosis] = []
    fa02 = _find(report, "FA02_recent_failures")
    fa03 = _find(report, "FA03_poisoned")
    fa04 = _find(report, "FA04_malformed")
    fa05 = _find(report, "FA05_stale")

    fa02_ev = _ev(fa02)
    fa03_ev = _ev(fa03)
    fa04_ev = _ev(fa04)
    fa05_ev = _ev(fa05)

    current_active = _evidence_int(fa02_ev, "current_active")
    current_retrying = _evidence_int(fa02_ev, "current_retrying")
    total_poisoned = _evidence_int(fa03_ev, "total_poisoned")
    current_poisoned = _evidence_int(fa03_ev, "current_poisoned")
    malformed = _evidence_int(fa04_ev, "malformed")
    stale = _evidence_int(fa05_ev, "stale")

    # FA02 — recent failures.
    if fa02 is not None and fa02.status == STATUS_FAIL and current_active > 0:
        out.append(_diag(
            "RECENT_FAILURE_MARKER", "error", "failure_accounting",
            f"{current_active} current failure marker(s) need attention",
            fa02_ev, repairable=True,
        ))
    elif fa02 is not None and fa02.status == STATUS_WARN and current_retrying > 0:
        out.append(_diag(
            "RETRY_EXHAUSTED", "warning", "failure_accounting",
            f"{current_retrying} failure marker(s) retrying (self-healing)",
            fa02_ev, repairable=True,
        ))

    # FA03 — poisoned 双轨.
    if current_poisoned > 0:
        out.append(_diag(
            "POISONED_FAILURE_MARKER", "error", "failure_accounting",
            f"{current_poisoned} poisoned marker(s) since last success",
            {"total_poisoned": total_poisoned, "current_poisoned": current_poisoned,
             "by_error_class": fa03_ev.get("by_error_class", {})},
            repairable=True,
        ))
    elif total_poisoned > 0:
        # Historical — isolated, reported as info.
        out.append(_diag(
            "POISONED_FAILURE_MARKER", "info", "failure_accounting",
            f"{total_poisoned} historical poisoned marker(s) (isolated)",
            {"total_poisoned": total_poisoned, "current_poisoned": 0,
             "historical": True,
             "by_error_class": fa03_ev.get("by_error_class", {})},
            repairable=True,
        ))

    # FA04 — malformed.
    if fa04 is not None and (fa04.status == STATUS_WARN or fa04.status == STATUS_UNKNOWN) and malformed > 0:
        out.append(_diag(
            "MALFORMED_FAILURE_MARKER", "warning", "failure_accounting",
            f"{malformed} malformed marker file(s)",
            fa04_ev, repairable=False,
        ))

    # FA05 — stale.
    if fa05 is not None and (fa05.status == STATUS_WARN or fa05.status == STATUS_UNKNOWN) and stale > 0:
        out.append(_diag(
            "STALE_PENDING_MARKER", "warning", "failure_accounting",
            f"{stale} stale in_flight / pending_db marker(s)",
            fa05_ev, repairable=True,
        ))
    return out


def _classify_derived_memory(report) -> list[Diagnosis]:
    out: list[Diagnosis] = []
    dm01 = _find(report, "DM01_topics")
    dm02 = _find(report, "DM02_observer_cursor")
    dm03 = _find(report, "DM03_observation_notes")
    dm04 = _find(report, "DM04_derived_last")

    if dm02 is not None and dm02.status == STATUS_WARN:
        out.append(_diag(
            "OBSERVER_STALE", "warning", "derived_memory",
            "observer cursor stale", _ev(dm02), repairable=False,
        ))
    elif dm02 is not None and dm02.status == STATUS_UNKNOWN:
        out.append(_diag(
            "OBSERVER_STALE", "warning", "derived_memory",
            "observer state unreadable (corrupt)", _ev(dm02), repairable=False,
        ))

    for chk, code, summary in (
        (dm01, "DERIVED_LAYER_STALE", "topics freshness stale"),
        (dm03, "DERIVED_LAYER_STALE", "observation_notes stale"),
        (dm04, "DERIVED_LAYER_STALE", "derived layer stale"),
    ):
        if chk is not None and (chk.status == STATUS_WARN or chk.status == STATUS_UNKNOWN):
            out.append(_diag(code, "warning", "derived_memory", summary,
                              _ev(chk), repairable=False))
    return out


def _classify_providers(report) -> list[Diagnosis]:
    out: list[Diagnosis] = []
    pr01 = _find(report, "PR01_configured")
    pr02 = _find(report, "PR02_failure_ledger")

    pr01_ev = _ev(pr01)
    pr02_ev = _ev(pr02)
    recent_provider_errors = pr02_ev.get("recent_provider_errors") or {}
    if not isinstance(recent_provider_errors, dict):
        recent_provider_errors = {}

    embed_cfg = bool(pr01_ev.get("embed_configured"))
    rerank_cfg = bool(pr01_ev.get("rerank_configured"))
    llm_cfg = bool(pr01_ev.get("llm_configured"))

    # PR01.
    if pr01 is not None and pr01.status == STATUS_FAIL and not embed_cfg:
        out.append(_diag(
            "PROVIDER_UNCONFIGURED", "error", "providers",
            "embedding provider not configured",
            pr01_ev, repairable=False,
        ))
    elif pr01 is not None and pr01.status == STATUS_WARN and (not rerank_cfg or not llm_cfg):
        out.append(_diag(
            "PROVIDER_UNCONFIGURED", "warning", "providers",
            "rerank/llm provider not configured",
            pr01_ev, repairable=False,
        ))
    elif pr01 is not None and pr01.status == STATUS_UNKNOWN:
        out.append(_diag(
            "PROVIDER_UNCONFIGURED", "warning", "providers",
            "provider config unreadable",
            pr01_ev, repairable=False,
        ))

    # PR02.
    cred_classes = {"provider_401", "provider_402"}
    transient_classes = {"provider_429", "provider_5xx",
                         "provider_timeout", "provider_connection"}
    cred_count = sum(int(recent_provider_errors.get(k, 0)) for k in cred_classes)
    transient_count = sum(int(recent_provider_errors.get(k, 0)) for k in transient_classes)
    if cred_count > 0:
        out.append(_diag(
            "EMBED_PROVIDER_AUTH", "error", "providers",
            f"{cred_count} recent provider 401/402",
            {"recent_provider_errors": {k: int(v)
                                        for k, v in recent_provider_errors.items() if v}},
            repairable=False,
        ))
    if transient_count > 0:
        # Pick the most actionable code based on which class is leading.
        if int(recent_provider_errors.get("provider_429", 0)) > 0:
            code = "EMBED_PROVIDER_RATE_LIMIT"
            summary = "recent provider 429"
        else:
            code = "EMBED_PROVIDER_TIMEOUT"
            summary = "recent provider timeout/connection"
        out.append(_diag(
            code, "warning", "providers",
            f"{summary} ({transient_count} events)",
            {"recent_provider_errors": {k: int(v)
                                        for k, v in recent_provider_errors.items() if v}},
            repairable=True,
        ))
    return out


# ── Public entry point ───────────────────────────────────────────────


def diagnose(report) -> list[Diagnosis]:
    """Classify a ``HealthReport`` into a deterministic ``list[Diagnosis]``.

    The function is pure: it never touches the DB, filesystem, or network.
    The output is sorted deterministically — ``error`` first, then
    ``warning``, then ``info``; ties broken alphabetically by ``code``.

    Empty reports (no checks / all ``skip``) return ``[]``.
    """
    if not getattr(report, "checks", None):
        return []

    candidates: list[Diagnosis] = []
    candidates.extend(_classify_runtime(report))
    candidates.extend(_classify_storage(report))
    candidates.extend(_classify_memory_write(report))
    candidates.extend(_classify_failure_accounting(report))
    candidates.extend(_classify_derived_memory(report))
    candidates.extend(_classify_providers(report))

    candidates.sort(key=_sort_key)
    return candidates


__all__ = ["diagnose"]