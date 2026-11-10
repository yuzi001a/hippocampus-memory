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
    _OrdinalCounter,
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
    batch_size: int = 500,
    on_session=None,
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
            sources.append(
                _process_source(
                    src,
                    pool=pool,
                    dry_run=dry_run,
                    batch_size=batch_size,
                    on_session=on_session,
                )
            )

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


def _process_source(src, *, pool, dry_run: bool, batch_size: int = 500, on_session=None) -> dict:
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
        "reopened_sessions": 0,
    }

    if not src.found:
        return entry

    imp = get_importer(src.host)
    imp.reset_excluded_counts()

    from . import _emit_curated_or_legacy, _emit_raw_messages_bulk, _resolve_writer
    from . import qa_pairing as _qa

    stats = _Stats()
    pending: dict[str, list] = {}
    order: list[str] = []
    reopened = 0
    seen_sessions: set[str] = set()
    items_seen = 0
    pairs_seen = 0
    curated_items: list = []
    ordinals = _OrdinalCounter()
    # A rebuild must not hold the whole corpus in memory — and neither may a
    # single huge session. Buffer caps are per session; at most
    # ``max_open_sessions`` sessions stay open, so peak retention is
    # ``max_open_sessions * max_session_items`` messages (plus one in flight).
    max_open_sessions = 3
    max_session_items = 2_000
    max_session_chars = 4_000_000
    # 硬上限：某些会话整段都是 assistant/tool 行，永远等不到"下一条是 user"的
    # 安全边界。到硬上限必须切，否则单会话内存无界。切了就如实记一笔，
    # 不静默截断答案链。
    max_session_hard_items = 8_000
    max_session_hard_chars = 16_000_000
    open_chars: dict[str, int] = {}
    forced_splits = 0

    def _safe_boundary(buf: list, nxt) -> bool:
        """Splitting here never separates a question from its answer."""
        if not buf or nxt is None:
            return True
        prev_role = (buf[-1].role or "").strip().lower()
        next_role = (nxt.role or "").strip().lower()
        return next_role == "user" and prev_role != "user"

    def _assign_identities(sid: str, items: list) -> list:
        """Give every host-carrying raw item its final event id **before**
        pairing, so the raw row and its QA pair agree on identity.

        Previously the raw writer invented ``derived:<host>:<sess>:<n>``
        while the pairing layer invented ``…:turn-<k>`` — for sources with
        no native event id (honcho, trajectory, old OpenClaw) the QA
        ``source_id`` therefore pointed at a message that never existed.
        ``ordinals`` is shared across segments and batches, so the number is
        a per-session ordinal, not a per-chunk one.
        """
        from dataclasses import replace

        out = []
        for it in items:
            host = (it.host or "").strip()
            if it.kind == KIND_RAW and host and not (it.event_id or "").strip():
                it = replace(it, event_id=f"derived:{host}:{sid}:{ordinals.next(host, sid)}")
                stats.identity_derived += 1
            out.append(it)
        return out

    def _flush_session(sid: str, *, keep_open: bool = False) -> None:
        nonlocal pairs_seen
        items = pending.pop(sid, []) if not keep_open else pending.get(sid, [])
        if not items:
            return
        if keep_open:
            pending[sid] = []
            open_chars[sid] = 0
        else:
            open_chars.pop(sid, None)
        items = _assign_identities(sid, items)
        pairs = _qa.pair_items(items)
        pairs_seen += len(pairs)
        if not dry_run:
            failed = _emit_raw_messages_bulk(
                pool=pool,
                items=items,
                source_tag=f"import:{imp.name}",
                stats=stats,
                batch_size=batch_size,
                ordinals=ordinals,
            )
            if failed:
                # Dependency contract: QA is derived from raw. If the raw
                # write failed, writing QA would leave orphans that point at
                # messages which do not exist. Withhold and say so.
                stats.skipped += len(pairs)
                stats.errors.append(
                    f"{sid}: {len(pairs)} QA pair(s) withheld — raw write failed "
                    f"for {failed} message(s)"
                )
            elif pairs:
                _qa.emit_qa_pairs_bulk(
                    pool=pool, pairs=pairs, stats=stats, batch_size=batch_size
                )
        if on_session is not None:
            try:
                on_session(sid, len(items), len(pairs))
            except Exception:  # progress callbacks never break an import
                pass

    for artifact in src.artifacts:
        try:
            for it in imp.parse(artifact):
                if it.kind != KIND_RAW:
                    # 用户维护的长期记忆（MEMORY.md / USER.md …）走
                    # explicit_memories，不能因为"不是 raw"就静默跳过。
                    if it.kind in (KIND_CURATED, KIND_LEGACY):
                        if (it.text or "").strip():
                            curated_items.append(it)
                    continue
                if not (it.text or "").strip():
                    continue
                sid = (it.source_ref or "").strip() or "imported"
                items_seen += 1
                if sid not in pending:
                    if sid in seen_sessions:
                        reopened += 1
                    seen_sessions.add(sid)
                    while len(pending) >= max_open_sessions and order:
                        _flush_session(order.pop(0))
                    pending[sid] = []
                    open_chars[sid] = 0
                    order.append(sid)
                buf = pending[sid]
                # Bound the buffer at a safe boundary: flush what we have
                # before appending a message that would exceed the cap.
                if buf and (
                    len(buf) >= max_session_items
                    or open_chars.get(sid, 0) >= max_session_chars
                ) and _safe_boundary(buf, it):
                    _flush_session(sid, keep_open=True)
                    buf = pending[sid]
                elif buf and (
                    len(buf) >= max_session_hard_items
                    or open_chars.get(sid, 0) >= max_session_hard_chars
                ):
                    # 没有安全边界也只能切：内存必须封顶。切点会让一条回答链
                    # 分成两段（后半段没有提问，只留 raw），所以必须可见。
                    forced_splits += 1
                    _flush_session(sid, keep_open=True)
                    buf = pending[sid]
                buf.append(it)
                open_chars[sid] = open_chars.get(sid, 0) + len(it.text or "")
        except Exception as exc:
            entry["errors"].append(f"{artifact.name}: {exc!r}")

    for sid in list(order):
        _flush_session(sid)

    if curated_items and not dry_run:
        writer = _resolve_writer(pool=pool)
        if writer is None:
            entry["errors"].append(
                f"{len(curated_items)} curated item(s) found but no ActiveMemoryWriter — skipped"
            )
        else:
            _emit_curated_or_legacy(
                writer=writer,
                items=curated_items,
                imported_at=_now_iso(),
                stats=stats,
            )
    entry["curated_found"] = len(curated_items)

    if forced_splits:
        entry["errors"].append(
            f"{forced_splits} segment(s) force-flushed at the hard cap "
            f"({max_session_hard_items} items / {max_session_hard_chars} chars): "
            "an answer chain that long is split, the tail stays raw-only"
        )
    entry["reopened_sessions"] = reopened
    if reopened:
        # A session that shows up again after being flushed can only mean the
        # source is not session-contiguous; its QA pairs may be incomplete.
        # Surface it as an error instead of letting the count shrink silently.
        entry["errors"].append(
            f"{reopened} session(s) reappeared after flush — QA pairs for those "
            "sessions may be incomplete (source not session-contiguous)"
        )
    entry["messages"] = items_seen
    entry["skipped"] = dict(imp.excluded_counts)
    entry["unsupported_skipped"] = sum(imp.excluded_counts.values())

    entry["qa_pairs_derived"] = pairs_seen if dry_run else stats.qa_pairs_derived
    if dry_run:
        entry["recall_ready"] = bool(items_seen) and bool(pairs_seen)
        return entry

    entry["raw_imported"] = stats.raw_messages
    entry["duplicates_skipped"] = stats.deduped
    entry["nul_sanitized"] = stats.nul_sanitized
    entry["user_curated_imported"] = stats.user_curated
    for e in stats.errors:
        entry["errors"].append(e)

    entry["oldest"] = stats.oldest or entry["oldest"]
    entry["newest"] = stats.newest or entry["newest"]
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
        self.nul_sanitized = 0
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
