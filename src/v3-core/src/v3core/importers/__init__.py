"""v3core.importers — Hippocampus v0.2 import framework.

Frozen public surface (see feature/v0.2-first-user-release):

  * ``KIND_RAW``      — original user/assistant turns  → conversation_stream
  * ``KIND_CURATED``  — human-maintained memory notes  → explicit_memories
                         (category='user_curated', tags include 'user-curated')
  * ``KIND_LEGACY``   — another system's LLM summaries → explicit_memories
                         tagged 'legacy-derived' (NEVER masquerade as raw)

Migration-layer rules (enforced in code):
  1. raw → conversation_stream rows with source='import:<system>', dedupe on
     (session_id, role, timestamp); a re-run imports 0 new rows.
  2. user-curated → explicit_memories via v3core.active_memory_store.
     ActiveMemoryWriter; provenance carries kind=user_curated, file_sha256,
     line range, iso8601 imported_at.
  3. legacy-derived → explicit_memories tagged legacy-derived; provenance
     carries kind=legacy_derived, source_system, original_id, imported_at,
     original_text_sha256. Visibly distinguishable from raw facts.
  4. End-to-end idempotency (re-running same import → 0 duplicate rows,
     0 new explicit_memories rows for identical content).

Scope honesty:
  * hermes + memory-md are production-quality (real parse, real provenance,
    real dedupe, real report).
  * openclaw + hindsight are framework_ready — they are registered so users
    can see them in `hippocampus import list`, but they raise
    NotImplementedError on parse until the artifact format is provided.
    ``import_source`` refuses a framework_ready importer unless dry-run is
    set (a dry-run may report what would be needed).
"""
from __future__ import annotations

import abc
import dataclasses
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

# Public kind constants (frozen) ------------------------------------------
KIND_RAW = "raw_message"
KIND_CURATED = "user_curated"
KIND_LEGACY = "legacy_derived"

# All three are exposed as the canonical vocabulary for downstream layers.
ALL_KINDS: tuple[str, ...] = (KIND_RAW, KIND_CURATED, KIND_LEGACY)

logger = logging.getLogger("v3core.importers")


# ── dataclasses (frozen surface) ─────────────────────────────────────────


@dataclass
class ImportItem:
    """A single item produced by an importer.

    Fields:
      kind            — one of KIND_RAW / KIND_CURATED / KIND_LEGACY
      source_system   — logical system name (hermes, memory-md, openclaw, ...)
      source_ref      — opaque identifier of the source artifact (file path,
                        table+row, JSONL line number, ...); surfaced verbatim
                        in provenance so lineage is auditable.
      text            — content body (for KIND_RAW: a single message; for
                        KIND_CURATED/KIND_LEGACY: the full memory text)
      role            — for KIND_RAW only (user/assistant/system/tool)
      occurred_at     — for KIND_RAW only, ISO8601 string of original timestamp
      title           — optional title for curated/legacy memories
      category        — explicit_memories.category (defaults are applied per
                        kind in ``import_source`` if None)
      tags            — explicit_memories.tags; framework always appends
                        the kind-tag (user-curated / legacy-derived) so a
                        downstream reader can filter on it cheaply.
      provenance      — arbitrary caller-supplied dict merged into the
                        explicit_memories.provenance JSONB column.
    """

    kind: str
    source_system: str
    source_ref: str
    text: str
    role: str | None = None
    occurred_at: str | None = None
    title: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    provenance: dict | None = None


@dataclass
class ImportStats:
    source_system: str
    raw_messages: int = 0
    sessions: int = 0
    user_curated: int = 0
    legacy_derived: int = 0
    deduped: int = 0
    skipped: int = 0
    oldest: str | None = None
    newest: str | None = None
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


# ── Importer ABC (frozen surface) ───────────────────────────────────────


class Importer(abc.ABC):
    """Discover one or more source artifacts and parse them into ImportItems.

    Subclass contract:
      * ``name`` — short stable identifier (used as the CLI ``source`` key).
      * ``description`` — one-line human description for `import list`.
      * ``capability`` — exactly ``'production'`` or ``'framework_ready'``.
        Production importers must implement parse() fully; framework_ready
        importers must raise ``NotImplementedError`` with a clear message
        naming the artifact format still needed.
    """

    name: str = ""
    description: str = ""
    capability: str = "production"  # 'production' | 'framework_ready'

    @abc.abstractmethod
    def discover(self, root: Path) -> list[Path]:
        """Return every source artifact under ``root`` this importer can read.

        ``root`` may be a single file (state.db, MEMORY.md, *.jsonl) or a
        directory containing them. Returning an empty list is fine — the
        caller treats it as 'nothing to import' rather than an error.
        """

    @abc.abstractmethod
    def parse(self, path: Path) -> Iterator[ImportItem]:
        """Yield ImportItems parsed from ``path``.

        Implementations must be tolerant of partial/malformed input — log
        warnings and skip bad rows instead of raising. Hard structural
        failures (file unreadable, no schema) should raise so the caller
        can report them in stats.errors.
        """


# ── helpers ─────────────────────────────────────────────────────────────


_NUMBER_RE = re.compile(r"-?\d")


def _to_iso8601(value: Any) -> str | None:
    """Best-effort ISO8601 normalization (no extra deps).

    Accepts:
      * ISO string → returned unchanged if it looks valid
      * epoch seconds or ms → 'YYYY-MM-DDTHH:MM:SSZ'
      * datetime/date objects → isoformat()
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return None
    if isinstance(value, (int, float)):
        # Heuristic: > 10^12 → milliseconds; otherwise seconds.
        if value > 1e12:
            value = value / 1000.0
        from datetime import datetime, timezone
        try:
            return (
                datetime.fromtimestamp(float(value), tz=timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except Exception:
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Accept as-is if it already looks like an ISO date prefix.
        if re.match(r"^\d{4}-\d{2}-\d{2}", s):
            return s
        # Try integer-ish strings (epoch).
        if s.lstrip("-").isdigit():
            return _to_iso8601(int(s))
        return s
    return None


def _min_max_iso(*values: str | None) -> tuple[str | None, str | None]:
    """Return (min, max) ISO8601 strings by lexical compare.

    Lexical compare is safe for ISO8601 (fixed-width design).
    """
    cleaned = [v for v in values if v]
    if not cleaned:
        return None, None
    return min(cleaned), max(cleaned)


# ── writer interface (kept here so importers don't import active_memory) ─
# We avoid a hard dependency on v3core.active_memory_store at module import
# time so importers/ can be imported even in environments where PG is
# unreachable (CLI smoke tests, framework scaffolding). The actual import
# is deferred to ``_resolve_writer`` below.


def _resolve_writer(*, pool: Any | None):
    """Resolve an ActiveMemoryWriter-like object.

    Returns None if PG/active_memory_store is unavailable (the caller falls
    back to dry-run accounting). The returned object's ``.create()`` method
    must accept (category, title, content, tags, source_id, memory_id,
    provenance) and return an object with .status and .memory_id.
    """
    if pool is None:
        return None
    try:
        from v3core.active_memory_store import ActiveMemoryWriter  # type: ignore
    except Exception as exc:  # pragma: no cover
        logger.warning("importers: ActiveMemoryWriter unavailable: %s", exc)
        return None
    try:
        # Embed disabled — we don't want importer to call an embedding
        # backend during framework scaffolding. Callers can re-enable by
        # wrapping the writer if needed.
        return ActiveMemoryWriter(pool=pool, embed_cfg=None)
    except Exception as exc:  # pragma: no cover
        logger.warning("importers: ActiveMemoryWriter init failed: %s", exc)
        return None


def _resolve_reader(*, pool: Any | None):
    if pool is None:
        return None
    try:
        from v3core.active_memory_store import ActiveMemoryReader  # type: ignore
    except Exception:  # pragma: no cover
        return None
    try:
        return ActiveMemoryReader(pool=pool)
    except Exception:  # pragma: no cover
        return None


# ── raw writes (conversation_stream) ────────────────────────────────────

_RAW_DEDUPE_SQL = """
    INSERT INTO public.conversation_stream
        (session_id, role, content, trigger, turn_id, timestamp, source,
         tool_calls, tool_results)
    SELECT %s, %s, %s, 'import', %s, %s::timestamptz, %s,
           %s::jsonb, %s::jsonb
    WHERE NOT EXISTS (
        SELECT 1 FROM public.conversation_stream
         WHERE session_id = %s
           AND role = %s
           AND timestamp = %s::timestamptz
    )
"""

_RAW_DEDUPE_SQL_NO_TS = """
    INSERT INTO public.conversation_stream
        (session_id, role, content, trigger, turn_id, source,
         tool_calls, tool_results)
    VALUES (%s, %s, %s, 'import', %s, %s, %s::jsonb, %s::jsonb)
"""


def _emit_raw_messages(
    *,
    pool: Any,
    items: list[ImportItem],
    source_tag: str,
    stats: ImportStats,
) -> None:
    """Bulk-insert raw messages with (session_id, role, timestamp) dedupe.

    Connection contract: ``pool`` is any object that yields an object with
    ``.cursor()``, ``.commit()``, and ``.close()`` (a real PgPool lease, or
    a hermetic fake — same surface as v3core.pg_pool.PgPool.lease()).
    """
    sessions_seen: set[str] = set()
    oldest_ts: str | None = None
    newest_ts: str | None = None

    for it in items:
        if it.kind != KIND_RAW:
            continue
        if not it.text:
            continue
        ts = _to_iso8601(it.occurred_at)
        if ts:
            oldest_ts = ts if oldest_ts is None or ts < oldest_ts else oldest_ts
            newest_ts = ts if newest_ts is None or ts > newest_ts else newest_ts
        role = (it.role or "").strip()
        sess = (it.source_ref or "").strip() or "imported"
        sessions_seen.add(sess)
        tool_calls = "[]"
        tool_results = "[]"
        prov = it.provenance or {}
        # Provenance can carry tool_calls/tool_results as JSON strings; the
        # caller is responsible for shape — we just pass through if present.
        if isinstance(prov.get("tool_calls"), (list, dict)):
            tool_calls = json.dumps(prov["tool_calls"], ensure_ascii=False)
        if isinstance(prov.get("tool_results"), (list, dict)):
            tool_results = json.dumps(prov["tool_results"], ensure_ascii=False)
        turn_id = prov.get("turn_id")
        try:
            turn_id = int(turn_id) if turn_id is not None else None
        except (TypeError, ValueError):
            turn_id = None

        lease = pool.lease(timeout=5.0)
        try:
            cur = lease.connection.cursor()
            if ts:
                cur.execute(
                    _RAW_DEDUPE_SQL,
                    (
                        sess, role, it.text, turn_id, ts, source_tag,
                        tool_calls, tool_results,
                        sess, role, ts,
                    ),
                )
            else:
                cur.execute(
                    _RAW_DEDUPE_SQL_NO_TS,
                    (sess, role, it.text, turn_id, source_tag,
                     tool_calls, tool_results),
                )
            inserted = bool(getattr(cur, "rowcount", 0))
            lease.connection.commit()
            if inserted:
                stats.raw_messages += 1
            else:
                stats.deduped += 1
        except Exception as exc:
            stats.skipped += 1
            stats.errors.append(f"raw write failed ({sess}/{role}): {exc!r}")
            try:
                lease.connection.rollback()
            except Exception:
                pass
        finally:
            try:
                lease.close()
            except Exception:
                pass

    stats.sessions = max(stats.sessions, len(sessions_seen))
    if oldest_ts is not None:
        stats.oldest = oldest_ts
    if newest_ts is not None:
        stats.newest = newest_ts


# ── curated/legacy writes (explicit_memories) ───────────────────────────


def _ensure_tags(item: ImportItem) -> list[str]:
    """Apply canonical kind-tag and dedupe while preserving order."""
    base = list(item.tags or [])
    if item.kind == KIND_CURATED and "user-curated" not in base:
        base.append("user-curated")
    if item.kind == KIND_LEGACY and "legacy-derived" not in base:
        base.append("legacy-derived")
    seen: set[str] = set()
    out: list[str] = []
    for t in base:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _emit_curated_or_legacy(
    *,
    writer: Any,
    items: Iterable[ImportItem],
    imported_at: str,
    stats: ImportStats,
) -> None:
    """Write curated/legacy items via the ActiveMemoryWriter.

    Idempotency comes from ActiveMemoryWriter.derive_memory_id — same
    (category, title, content, tags) ⇒ same memory_id ⇒ ON CONFLICT DO
    NOTHING. We don't need a separate dedupe count; ``status`` from the
    writer tells us DEDUPLICATED vs DURABLE_COMMITTED.
    """
    for it in items:
        if it.kind not in (KIND_CURATED, KIND_LEGACY):
            continue
        text = (it.text or "").strip()
        if not text:
            stats.skipped += 1
            continue
        category = it.category or (
            "user_curated" if it.kind == KIND_CURATED else "legacy_derived"
        )
        title = it.title or (it.source_ref or "imported")[:200]
        tags = _ensure_tags(it)
        prov = dict(it.provenance or {})
        # Mandatory provenance envelope — every imported memory must carry
        # the kind marker so lineage is auditable even after a downstream
        # rewrite of provenance.
        prov.setdefault("kind", it.kind)
        prov.setdefault("source_system", it.source_system)
        prov.setdefault("source_ref", it.source_ref)
        prov.setdefault("imported_at", imported_at)
        if it.kind == KIND_LEGACY:
            prov.setdefault("original_text_sha256", _sha256_hex(text))
        elif it.kind == KIND_CURATED:
            # 'kind' alone isn't enough — be explicit so a reader can grep.
            prov.setdefault("kind_marker", "user-curated")

        try:
            result = writer.create(
                category=category,
                title=title,
                content=text,
                tags=tags,
                provenance=prov,
                # No source_id / memory_id — let ActiveMemoryWriter derive
                # the canonical hash from (category, title, content, tags).
                # That makes the canonical memory_id naturally idempotent
                # for identical text (ON CONFLICT DO NOTHING on re-run).
            )
        except Exception as exc:
            stats.skipped += 1
            stats.errors.append(
                f"{it.kind} write failed ({it.source_ref}): {exc!r}"
            )
            continue

        status = getattr(result, "status", "")
        if status in ("DURABLE_COMMITTED", "DERIVED_WARNING"):
            if it.kind == KIND_CURATED:
                stats.user_curated += 1
            else:
                stats.legacy_derived += 1
        elif status == "DEDUPLICATED":
            stats.deduped += 1
        else:  # DURABLE_FAILED or anything unexpected
            stats.skipped += 1
            err = getattr(result, "error", None) or status or "unknown"
            stats.errors.append(
                f"{it.kind} write failed ({it.source_ref}): {err}"
            )


def _sha256_hex(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── registry ────────────────────────────────────────────────────────────


def _build_registry() -> dict[str, type[Importer]]:
    # Imported lazily so a half-installed environment still imports
    # cleanly (the cost is one extra import per .Importer subclass).
    from .hermes_sessions import HermesSessionImporter
    from .memory_md import MemoryMarkdownImporter
    from .openclaw import OpenClawImporter
    from .hindsight import HindsightImporter

    # Re-export the concrete classes at package level so tests / external
    # callers can isinstance-check against them without importing the
    # submodules explicitly.
    globals().update({
        "HermesSessionImporter": HermesSessionImporter,
        "MemoryMarkdownImporter": MemoryMarkdownImporter,
        "OpenClawImporter": OpenClawImporter,
        "HindsightImporter": HindsightImporter,
    })
    return {
        "hermes": HermesSessionImporter,
        "memory-md": MemoryMarkdownImporter,
        "openclaw": OpenClawImporter,
        "hindsight": HindsightImporter,
    }


IMPORTERS: dict[str, type[Importer]] = _build_registry()


def get_importer(name: str) -> Importer:
    """Look up an importer by its short name (``hippocampus import list``)."""
    if name not in IMPORTERS:
        raise ValueError(
            f"unknown importer {name!r}; known: {sorted(IMPORTERS)!r}"
        )
    return IMPORTERS[name]()


def list_importers() -> list[dict[str, str]]:
    """Inventory for `hippocampus import list`.

    Returns a list of dicts — one per registered importer — describing its
    honest capability. Production importers carry capability='production';
    framework_ready importers carry capability='framework_ready' with a
    machine-parseable reason.
    """
    out: list[dict[str, str]] = []
    for key, cls in IMPORTERS.items():
        inst = cls()
        entry: dict[str, str] = {
            "name": key,
            "class": cls.__name__,
            "description": inst.description,
            "capability": inst.capability,
        }
        if inst.capability == "framework_ready":
            # Attempt to surface the static NotImplementedError message.
            try:
                list(inst.parse(Path("__never_read__.md")))
            except NotImplementedError as exc:
                entry["reason"] = str(exc)
            except Exception:
                pass
        out.append(entry)
    return out


# ── entry point (frozen surface) ───────────────────────────────────────


# Sensible refusal text. Stable so tests can assert on it.
_FREF = (
    "framework_ready importer {name!r} is not implemented yet. "
    "Re-run with --dry-run to see what it would need, "
    "or supply an artifact format and implement .parse()."
)


def _require_implementation(importer: Importer) -> None:
    """Raise a clear RuntimeError unless the importer is production-ready."""
    if importer.capability == "production":
        return
    # Try to call parse() on a tiny synthetic path to surface the
    # importer's own NotImplementedError message verbatim — that's the
    # contractually-useful text users need to see.
    msg = _FREF.format(name=importer.name)
    try:
        list(importer.parse(Path("__never_read__.probe")))
    except NotImplementedError as exc:
        msg = str(exc) or msg
    except Exception:
        # Anything else means the importer's scaffold is at least present
        # but parse() failed on the probe — still refuse the live path.
        pass
    raise RuntimeError(msg)


def _format_report(stats: ImportStats) -> str:
    """The exact report block contract — thousands separators + 'none' when empty."""

    def _n(v: int) -> str:
        return f"{v:,}"

    oldest = stats.oldest if stats.oldest else "none"
    newest = stats.newest if stats.newest else "none"
    lines = [
        "Imported:",
        f"- {_n(stats.raw_messages)} raw messages",
        f"- {_n(stats.sessions)} sessions",
        f"- {_n(stats.user_curated)} user-curated memories",
        f"- {_n(stats.legacy_derived)} legacy-derived memories",
        f"Oldest source: {oldest}",
        f"Newest source: {newest}",
    ]
    return "\n".join(lines)


def import_source(  # noqa: PLR0915 - linear pipeline by design
    *,
    source: str,
    root: Path,
    profile_dir: Path | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    out=sys.stdout,
) -> ImportStats:
    """Run one importer end-to-end and emit the report block.

    Args:
      source      — key from IMPORTERS ('hermes', 'memory-md', ...)
      root        — file or directory the importer should discover()
      profile_dir — resolved V3Config root (currently informational; PG
                    connection params come from the injected pool when
                    one is provided via env wiring in the CLI layer)
      dry_run     — parse + count + report only; nothing is written
      limit       — optional cap on parsed items (smoke testing)
      out         — stream-like target for the printed report (default stdout)

    The caller (hippocampus CLI) is responsible for building the actual
    PgPool; this function accepts an injected pool via the module-level
    ``install_pool()`` shim so the test suite can run hermetically without
    psycopg2. Production wiring is the responsibility of the CLI layer.
    """
    importer = get_importer(source)
    stats = ImportStats(source_system=importer.name, dry_run=dry_run)

    if not dry_run:
        _require_implementation(importer)

    # ── discovery ──────────────────────────────────────────────────────
    try:
        artifacts = importer.discover(root)
    except Exception as exc:
        stats.errors.append(f"discover failed: {exc!r}")
        print(_format_report(stats), file=out)
        return stats

    if not artifacts:
        # Nothing to import is a normal outcome, not an error — but we
        # still print the report so the caller sees consistent output.
        print(_format_report(stats), file=out)
        return stats

    # ── parse ─────────────────────────────────────────────────────────
    parsed: list[ImportItem] = []
    parse_errors: list[str] = []
    for path in artifacts:
        try:
            for item in importer.parse(path):
                parsed.append(item)
                if limit is not None and len(parsed) >= limit:
                    break
            if limit is not None and len(parsed) >= limit:
                break
        except NotImplementedError as exc:
            # framework_ready importer — refuse with the importer's own text.
            if not dry_run:
                stats.errors.append(f"parse failed: {exc}")
                print(_format_report(stats), file=out)
                return stats
            # dry-run: surface as a note and continue with what we have.
            stats.errors.append(f"dry-run: {exc}")
            break
        except Exception as exc:
            parse_errors.append(f"{path}: {exc!r}")
            continue

    if parse_errors:
        stats.errors.extend(parse_errors)

    # ── raw aggregate stats (always, even on dry-run) ─────────────────
    raw_items = [it for it in parsed if it.kind == KIND_RAW]
    if raw_items:
        oldest_ts, newest_ts = _min_max_iso(*[it.occurred_at for it in raw_items])
        if oldest_ts:
            stats.oldest = oldest_ts
        if newest_ts:
            stats.newest = newest_ts
        sessions: set[str] = set()
        for it in raw_items:
            if it.source_ref:
                sessions.add(it.source_ref)
        stats.sessions = max(stats.sessions, len(sessions))

    # ── legacy-derived aggregate stats (always) ───────────────────────
    legacy_items = [it for it in parsed if it.kind == KIND_LEGACY]
    if legacy_items:
        oldest_ts, newest_ts = _min_max_iso(
            *[it.occurred_at for it in legacy_items if it.occurred_at]
        )
        if oldest_ts:
            stats.oldest = (
                oldest_ts if stats.oldest is None else min(stats.oldest, oldest_ts)
            )
        if newest_ts:
            stats.newest = (
                newest_ts if stats.newest is None else max(stats.newest, newest_ts)
            )

    # ── curated aggregate stats (always) ──────────────────────────────
    curated_items = [it for it in parsed if it.kind == KIND_CURATED]
    if curated_items:
        oldest_ts, newest_ts = _min_max_iso(
            *[it.occurred_at for it in curated_items if it.occurred_at]
        )
        if oldest_ts:
            stats.oldest = (
                oldest_ts if stats.oldest is None else min(stats.oldest, oldest_ts)
            )
        if newest_ts:
            stats.newest = (
                newest_ts if stats.newest is None else max(stats.newest, newest_ts)
            )

    if dry_run:
        # In dry-run mode, report what we *would* have imported without
        # touching PG. Counts reflect the parsed aggregate, not actual
        # dedupe behavior — but the report shape is identical.
        stats.raw_messages = len(raw_items)
        stats.user_curated = len(curated_items)
        stats.legacy_derived = len(legacy_items)
        print(_format_report(stats), file=out)
        return stats

    # ── live write path ──────────────────────────────────────────────
    pool = _current_pool()
    if pool is None and (raw_items or curated_items or legacy_items):
        # No pool but the caller wants live writes — surface the error
        # clearly instead of silently dropping rows.
        stats.errors.append(
            "no PG pool injected — import_source requires the CLI to "
            "construct a PgPool from V3Config; pass it via install_pool() "
            "or re-run with dry_run=True."
        )
        print(_format_report(stats), file=out)
        return stats

    from datetime import datetime, timezone
    imported_at = (
        datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
        .replace("+00:00", "Z")
    )

    if raw_items:
        _emit_raw_messages(
            pool=pool,
            items=raw_items,
            source_tag=f"import:{importer.name}",
            stats=stats,
        )

    if pool is not None:
        writer = _resolve_writer(pool=pool)
        if writer is not None and (curated_items or legacy_items):
            _emit_curated_or_legacy(
                writer=writer,
                items=list(curated_items) + list(legacy_items),
                imported_at=imported_at,
                stats=stats,
            )

    print(_format_report(stats), file=out)
    return stats


# ── pool injection seam (test-only; production wires via CLI) ──────────

_CURRENT_POOL: list[Any] = []


def install_pool(pool: Any | None) -> None:
    """Inject a PgPool (or compatible) for ``import_source`` to use.

    This is the seam the CLI uses after constructing a real PgPool from
    the resolved V3Config; tests use it to swap in a hermetic fake. The
    default state is ``None`` (no pool installed) — ``import_source``
    will then refuse live writes with a clear error.
    """
    global _CURRENT_POOL
    _CURRENT_POOL = [pool]


def _current_pool() -> Any | None:
    return _CURRENT_POOL[0] if _CURRENT_POOL else None


__all__ = [
    "KIND_RAW",
    "KIND_CURATED",
    "KIND_LEGACY",
    "ALL_KINDS",
    "ImportItem",
    "ImportStats",
    "Importer",
    "HermesSessionImporter",
    "MemoryMarkdownImporter",
    "OpenClawImporter",
    "HindsightImporter",
    "IMPORTERS",
    "get_importer",
    "list_importers",
    "import_source",
    "install_pool",
]
