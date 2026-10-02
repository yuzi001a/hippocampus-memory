"""v3core.importers.auto_import — the one-command import pipeline (I01).

``import_auto`` is the engine behind ``hippocampus import auto``:

    discover → parse → [dry-run: report only] →
    conversation_stream (F2 identity) → qa_pairs (deterministic pairing) →
    import_report.json

Guarantees:
  * dry-run = zero writes / zero LLM / zero embedding
  * one source failing never aborts the others (§18) → status PARTIAL
  * no LLM/embedding anywhere in the path (§10/§11)
  * report is machine-readable JSON written to <profile>/import_report.json
    on live runs only, overwritten (never appended)
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import sys
import time
from pathlib import Path
from typing import Mapping

from . import (
    KIND_CURATED,
    KIND_LEGACY,
    KIND_RAW,
    _current_pool,
    _emit_curated_or_legacy,
    _emit_raw_messages,
    _resolve_writer,
    get_importer,
)
from . import discovery as _discovery

DEFAULT_HOSTS: tuple[str, ...] = ("hermes", "dsh", "pi", "memory-md")
REPORT_FILENAME = "import_report.json"


def _now_iso() -> str:
    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def import_auto(
    *,
    hosts: list[str] | None = None,
    overrides: Mapping[str, Path] | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    dry_run: bool = False,
    profile_dir: Path | None = None,
    out=None,
) -> dict:
    """Run discovery + import for every requested host. Returns the report."""
    if out is None:
        out = sys.stdout
    started_wall = _now_iso()
    started = time.monotonic()

    requested = [str(h).strip() for h in (hosts or DEFAULT_HOSTS) if str(h).strip()]
    unknown = [h for h in requested if h not in _discovery.HOSTS]
    if unknown:
        raise ValueError(
            f"unknown host(s) {unknown!r}; known: {list(_discovery.HOSTS)!r}"
        )

    top_errors: list[str] = []
    pool = _current_pool()

    if not dry_run and pool is None:
        top_errors.append(
            "no PG pool injected — import_auto requires the CLI to construct "
            "a PgPool from V3Config; pass it via install_pool() or re-run "
            "with dry_run=True."
        )

    sources: list[dict] = []
    if not top_errors:
        detected = _discovery.discover_all(
            requested, overrides=overrides, env=env, platform=platform
        )
        for src in detected:
            sources.append(_process_source(src, pool=pool, dry_run=dry_run))

    totals = _totals(sources)
    duration = round(time.monotonic() - started, 3)

    if top_errors:
        status = "failed"
    elif any(s.get("errors") for s in sources):
        status = "partial"
    else:
        status = "ok"

    report = {
        "version": 1,
        "dry_run": dry_run,
        "status": status,
        "generated_at": started_wall,
        "started_at": started_wall,
        "finished_at": _now_iso(),
        "duration_s": duration,
        "hosts": requested,
        "sources": sources,
        "totals": totals,
        "errors": top_errors,
    }

    if not dry_run and profile_dir is not None and not top_errors:
        try:
            prof = Path(profile_dir)
            prof.mkdir(parents=True, exist_ok=True)
            (prof / REPORT_FILENAME).write_text(
                _json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # report failure must not fail the import
            report["errors"].append(f"report write failed: {exc!r}")

    _print_human(report, out)
    return report


# ── per-source pipeline ───────────────────────────────────────────────


def _process_source(src, *, pool, dry_run: bool) -> dict:
    entry: dict = {
        "host": src.host,
        "root": str(src.root) if src.root is not None else None,
        "resolved_from": src.resolved_from,
        "found": src.found,
        "reason": src.reason,
        "format": src.format,
        "version": src.version,
        "artifacts": len(src.artifacts),
        "sessions": src.sessions,
        "messages": 0,
        "raw_imported": 0,
        "qa_pairs_derived": 0,
        "user_curated_imported": 0,
        "duplicates_skipped": 0,
        "unsupported_skipped": 0,
        "skipped": {},
        "oldest": src.oldest,
        "newest": src.newest,
        "recall_ready": False,
        "errors": [],
    }

    if not src.found:
        return entry

    imp = get_importer(src.host)
    imp.reset_excluded_counts()

    parsed = []
    for artifact in src.artifacts:
        try:
            for item in imp.parse(artifact):
                parsed.append(item)
        except Exception as exc:
            entry["errors"].append(f"{artifact.name}: {exc!r}")

    raw_items = [it for it in parsed if it.kind == KIND_RAW and (it.text or "").strip()]
    curated_items = [it for it in parsed if it.kind in (KIND_CURATED, KIND_LEGACY)]

    entry["messages"] = len(raw_items)
    entry["skipped"] = dict(imp.excluded_counts)
    entry["unsupported_skipped"] = sum(imp.excluded_counts.values())

    # deterministic recall-ready derivation (no LLM — §9/§10)
    from . import qa_pairing as _qa

    pairs = _qa.pair_items(raw_items)

    # oldest/newest from raw items when discovery could not fill them
    occurred = [it.occurred_at for it in raw_items if it.occurred_at]
    if occurred:
        entry["oldest"] = entry["oldest"] or min(occurred)
        entry["newest"] = entry["newest"] or max(occurred)

    if dry_run:
        entry["qa_pairs_derived"] = len(pairs)
        entry["recall_ready"] = bool(raw_items) and bool(pairs)
        return entry

    # ── live writes ────────────────────────────────────────────────────
    stats = _Stats()
    if raw_items:
        _emit_raw_messages(
            pool=pool,
            items=raw_items,
            source_tag=f"import:{imp.name}",
            stats=stats,
        )
        if pairs:
            _qa.emit_qa_pairs(pool=pool, pairs=pairs, stats=stats)
        for e in stats.errors:
            entry["errors"].append(e)

    if curated_items:
        writer = _resolve_writer(pool=pool)
        if writer is not None:
            _emit_curated_or_legacy(
                writer=writer,
                items=curated_items,
                imported_at=_now_iso(),
                stats=stats,
            )
        else:
            entry["errors"].append(
                "curated write skipped: explicit-memory writer unavailable"
            )

    entry["raw_imported"] = stats.raw_messages
    entry["qa_pairs_derived"] = stats.qa_pairs_derived
    entry["duplicates_skipped"] = stats.deduped
    entry["user_curated_imported"] = stats.user_curated + stats.legacy_derived
    entry["recall_ready"] = stats.raw_messages > 0 and stats.qa_pairs_derived > 0
    return entry


class _Stats:
    """Minimal ImportStats-compatible recorder for the auto pipeline."""

    def __init__(self) -> None:
        self.source_system = ""
        self.raw_messages = 0
        self.sessions = 0
        self.user_curated = 0
        self.legacy_derived = 0
        self.deduped = 0
        self.skipped = 0
        self.oldest = None
        self.newest = None
        self.dry_run = False
        self.identity_derived = 0
        self.qa_pairs_derived = 0
        self.errors: list[str] = []


# ── report aggregation + human output ─────────────────────────────────


def _totals(sources: list[dict]) -> dict:
    def _sum(key: str) -> int:
        return sum(int(s.get(key) or 0) for s in sources)

    oldest = [s["oldest"] for s in sources if s.get("oldest")]
    newest = [s["newest"] for s in sources if s.get("newest")]
    return {
        "artifacts": _sum("artifacts"),
        "sessions": _sum("sessions"),
        "messages": _sum("messages"),
        "raw_imported": _sum("raw_imported"),
        "qa_pairs_derived": _sum("qa_pairs_derived"),
        "user_curated_imported": _sum("user_curated_imported"),
        "duplicates_skipped": _sum("duplicates_skipped"),
        "unsupported_skipped": _sum("unsupported_skipped"),
        "oldest": min(oldest) if oldest else None,
        "newest": max(newest) if newest else None,
        "recall_ready": any(bool(s.get("recall_ready")) for s in sources),
    }


def _print_human(report: dict, out) -> None:
    lines: list[str] = []
    dry = report["dry_run"]
    lines.append("Found:" if dry else "Import result:")
    for s in report["sources"]:
        if not s["found"]:
            lines.append(f"  {s['host']:<10} — not found ({s.get('reason')})")
            continue
        if dry:
            lines.append(
                f"  {s['host']:<10} {s['sessions']:>4} sessions "
                f"{s['messages']:>7} messages"
            )
        else:
            lines.append(
                f"  {s['host']:<10} {s['raw_imported']:>7} raw imported "
                f"{s['qa_pairs_derived']:>6} QA pairs"
                + ("" if not s.get("errors") else f"  [{len(s['errors'])} error(s)]")
            )
    t = report["totals"]
    if dry:
        lines.append("")
        lines.append("No source will be modified.")
        lines.append("No LLM will be called.")
    else:
        lines.append("")
        lines.append(f"Imported: {t['raw_imported']} raw messages, {t['sessions']} sessions")
        lines.append(
            f"Skipped: {t['duplicates_skipped']} duplicates, "
            f"{t['unsupported_skipped']} excluded rows"
        )
        lines.append(f"Recall-ready: {'YES' if t['recall_ready'] else 'NO'}")
        if t["oldest"]:
            lines.append(f"Oldest: {t['oldest']}")
            lines.append(f"Newest: {t['newest']}")
    if report["errors"]:
        lines.append("")
        lines.append("Errors:")
        for e in report["errors"]:
            lines.append(f"  - {e}")
    lines.append(f"Status: {report['status'].upper()}")
    print("\n".join(lines), file=out)


__all__ = ["import_auto", "DEFAULT_HOSTS", "REPORT_FILENAME"]
