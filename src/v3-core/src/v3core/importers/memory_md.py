"""v3core.importers.memory_md — production memory-markdown importer.

A user-curated markdown importer. Recognized filenames (case-insensitive,
top-level of the directory the user points at):

  * MEMORY.md   — primary long-term memory notes
  * USER.md     — user profile / preferences
  * SOUL.md     — persona / tone
  * AGENTS.md   — agent directives / operating notes
  * any other *.md file (capped at a sensible limit; deep traversal
    disabled by default to keep the import surface predictable)

Each ``*.md`` file is split into per-section memories using ``##`` /
``###`` heading boundaries. The file's full content is also yielded as a
single memory so the user never silently loses content.

Provenance contract (per item):
  * kind            = KIND_CURATED
  * source_system   = "memory-md"
  * source_ref      = "<file_name>::<section_anchor or 'full'>"
  * category        = "user_curated"
  * tags            = ["user-curated", "<file-derived tag>"]
  * provenance      = {
        "kind": "user_curated",
        "source_system": "memory-md",
        "file": "<file_name>",
        "file_sha256": "<hex>",
        "line_start": <int>,
        "line_end":   <int>,
        "imported_at": "<iso8601>",
    }

Idempotency: ActiveMemoryWriter.derive_memory_id hashes
``(category, title, content, tags)`` — identical section text ⇒ identical
memory_id ⇒ ON CONFLICT DO NOTHING ⇒ DEDUPLICATED on re-run. Line range
in provenance is informational, not part of the canonical hash.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import KIND_CURATED, ImportItem, Importer

logger = logging.getLogger("v3core.importers.memory_md")


# Canonical filenames (case-insensitive). Listed explicitly so the user
# gets predictable tags; other *.md files are also imported but use a
# generic tag.
_CANONICAL_FILES = {
    "memory.md": "memory",
    "user.md": "user",
    "soul.md": "soul",
    "agents.md": "agents",
}


class MemoryMarkdownImporter(Importer):
    name = "memory-md"
    description = (
        "User-maintained markdown (MEMORY.md / USER.md / SOUL.md / AGENTS.md / *.md) "
        "→ explicit_memories as user_curated"
    )
    capability = "production"

    # ── discovery ──────────────────────────────────────────────────────

    def discover(self, root: Path) -> list[Path]:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".md":
            return [root]
        if not root.is_dir():
            return []
        found: list[Path] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix.lower() != ".md":
                continue
            found.append(entry)
        return found

    # ── parse ──────────────────────────────────────────────────────────

    def parse(self, path: Path) -> Iterator[ImportItem]:
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")

        file_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        imported_at = (
            datetime.now(tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        canonical_tag = _CANONICAL_FILES.get(path.name.lower(), "notes")
        lines = text.splitlines()
        sections = list(_split_sections(text))

        # Always yield the full file as one memory — guarantees no content
        # is lost even if a heading split fails.
        full_item = self._make_item(
            path=path,
            title=f"{path.stem} (full)",
            content=text.strip(),
            line_start=1,
            line_end=len(lines) or 1,
            canonical_tag=canonical_tag,
            file_sha=file_sha,
            imported_at=imported_at,
        )
        yield full_item

        # Plus one memory per heading section, for finer recall.
        for sec_title, sec_body, line_start, line_end in sections:
            if not sec_body.strip():
                continue
            yield self._make_item(
                path=path,
                title=sec_title or f"{path.stem} (section)",
                content=sec_body.strip(),
                line_start=line_start,
                line_end=line_end,
                canonical_tag=canonical_tag,
                file_sha=file_sha,
                imported_at=imported_at,
            )

    def _make_item(
        self,
        *,
        path: Path,
        title: str,
        content: str,
        line_start: int,
        line_end: int,
        canonical_tag: str,
        file_sha: str,
        imported_at: str,
    ) -> ImportItem:
        provenance = {
            "kind": "user_curated",
            "source_system": "memory-md",
            "file": path.name,
            "file_sha256": file_sha,
            "line_start": int(line_start),
            "line_end": int(line_end),
            "imported_at": imported_at,
        }
        return ImportItem(
            kind=KIND_CURATED,
            source_system=self.name,
            source_ref=f"{path.name}::{_slug(title)}",
            text=content,
            title=title[:200],
            category="user_curated",
            tags=["user-curated", canonical_tag],
            provenance=provenance,
        )


# ── heading splitter ────────────────────────────────────────────────────


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _split_sections(text: str) -> Iterator[tuple[str, str, int, int]]:
    """Yield (title, body, line_start, line_end) per heading section.

    The body of a section spans from the line after the heading to the
    start of the next heading (any level) or end of file. Lines are
    1-indexed to match what humans / editors show.
    """
    lines = text.splitlines()
    if not lines:
        return
    matches: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines, start=1):
        m = _HEADING_RE.match(line)
        if m:
            matches.append((i, len(m.group(1)), m.group(2).strip()))
    if not matches:
        return
    for idx, (line_no, _level, title) in enumerate(matches):
        body_start = line_no + 1
        body_end = (
            matches[idx + 1][0] - 1
            if idx + 1 < len(matches)
            else len(lines)
        )
        body = "\n".join(lines[body_start - 1:body_end])
        yield title, body, body_start, body_end


def _slug(title: str) -> str:
    """Stable, filesystem-safe slug from a heading title."""
    s = re.sub(r"\s+", "-", title.strip().lower())
    s = re.sub(r"[^a-z0-9._-]+", "", s)
    return s or "section"


__all__ = ["MemoryMarkdownImporter"]
