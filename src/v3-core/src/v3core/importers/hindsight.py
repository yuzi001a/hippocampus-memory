"""v3core.importers.hindsight — framework_ready importer stub.

Status: framework_ready (NOT production). The Hindsight export shape is
not yet frozen; until a sample artifact is provided this importer:

  * Registers itself so ``hippocampus import list`` surfaces it with
    ``capability='framework_ready'`` and a clear reason.
  * Refuses live imports via :func:`v3core.importers.import_source` (the
    framework will raise a RuntimeError quoting this stub's own message).
  * Allows ``--dry-run`` to inspect what the importer would need.
  * Discovers nothing — no canonical Hindsight artifact path yet, so any
    ``root`` returns an empty list rather than guessing.

Implementor's TODO (when the Hindsight format is supplied):
  * Replace ``parse()`` body with a real iterator over ``ImportItem`` of
    kind ``KIND_LEGACY`` (NEVER ``KIND_RAW`` — Hindsight stores LLM
    summaries, not raw user turns).
  * Fill provenance with the schema fields listed in
    ``_REQUIRED_PROVENANCE_FIELDS`` below.
  * Switch ``capability`` to ``"production"`` and remove this stub's
    refusal text from the docstring.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from . import ImportItem, Importer


_REQUIRED_PROVENANCE_FIELDS = (
    "kind",                 # literal "legacy_derived"
    "source_system",        # literal "hindsight"
    "original_id",          # Hindsight's stable memory id
    "imported_at",          # ISO8601 string
    "original_text_sha256", # sha256 of original text (lineage audit)
)


_NOT_IMPLEMENTED_MSG = (
    "hindsight importer is framework_ready — no canonical Hindsight "
    "artifact format has been wired in yet. Provide a sample export "
    "(path, schema, and one example record) and implement "
    "HindsightImporter.parse() to emit KIND_LEGACY items with provenance "
    f"keys {list(_REQUIRED_PROVENANCE_FIELDS)!r}. Until then the live "
    "import path is refused (use --dry-run to see what it would need)."
)


class HindsightImporter(Importer):
    name = "hindsight"
    description = (
        "Hindsight-derived LLM summaries → explicit_memories tagged "
        "legacy-derived (NEVER masquerades as raw)"
    )
    capability = "framework_ready"

    def discover(self, root: Path) -> list[Path]:
        return []

    def parse(self, path: Path) -> Iterator[ImportItem]:
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)


__all__ = ["HindsightImporter"]
