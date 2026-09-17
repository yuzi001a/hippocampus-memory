"""v3core.importers.openclaw — framework_ready importer stub.

Status: framework_ready (NOT production). The artifact format OpenClaw
exports has not been finalized yet — once a sample artifact is available,
implement :meth:`OpenClawImporter.parse` and switch ``capability`` to
``"production"``.

Until then, this module:

  * Registers itself so ``hippocampus import list`` surfaces it with
    ``capability='framework_ready'`` and a clear reason.
  * Refuses live imports via :func:`v3core.importers.import_source` (the
    framework will raise a RuntimeError quoting this stub's own message).
  * Allows ``--dry-run`` to inspect what the importer would need.
  * Discovers nothing — there is no canonical artifact path yet, so any
    ``root`` returns an empty list rather than guessing.

Implementor's TODO (when the OpenClaw format is supplied):
  * Replace ``parse()`` body with a real iterator over ``ImportItem`` of
    kind ``KIND_LEGACY`` (NEVER ``KIND_RAW`` — OpenClaw generates LLM
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


# When implemented, every emitted item MUST populate these provenance keys.
_REQUIRED_PROVENANCE_FIELDS = (
    "kind",                 # literal "legacy_derived"
    "source_system",        # literal "openclaw"
    "original_id",          # OpenClaw's stable memory id
    "imported_at",          # ISO8601 string
    "original_text_sha256", # sha256 of original text (lineage audit)
)


_NOT_IMPLEMENTED_MSG = (
    "openclaw importer is framework_ready — no canonical OpenClaw artifact "
    "format has been wired in yet. Provide a sample export (path, schema, "
    "and one example record) and implement OpenClawImporter.parse() to emit "
    "KIND_LEGACY items with provenance keys "
    f"{list(_REQUIRED_PROVENANCE_FIELDS)!r}. Until then the live import path "
    "is refused (use --dry-run to see what it would need)."
)


class OpenClawImporter(Importer):
    name = "openclaw"
    description = (
        "OpenClaw-derived LLM summaries → explicit_memories tagged "
        "legacy-derived (NEVER masquerades as raw)"
    )
    capability = "framework_ready"

    def discover(self, root: Path) -> list[Path]:
        # Deliberately returns [] — there is no canonical OpenClaw
        # artifact path yet; an empty discovery prevents the framework
        # from accidentally importing something we don't understand.
        return []

    def parse(self, path: Path) -> Iterator[ImportItem]:
        # Hard failure with a clear message — the framework will surface
        # this verbatim via ``import_source`` when called without
        # ``--dry-run``. We never silently return empty, so a future
        # production import can never accidentally import nothing.
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)


__all__ = ["OpenClawImporter"]
