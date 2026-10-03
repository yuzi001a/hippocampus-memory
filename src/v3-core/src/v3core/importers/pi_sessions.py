"""v3core.importers.pi_sessions — pi session importer (I01).

Targets the **current official format**: pi `v1.0.0`
(tag a13d35a742c6…; entry tree with native 8-hex ids). Older versions
(1/2/3) share the message-entry shape and are tolerated; anything else is
reported, not guessed.

On-disk layout (bounded, host-specific):

    ~/.pi/agent/sessions/--<escaped-cwd>--/<timestamp>_<session-id>.jsonl

Format: plain JSONL. Line 0 = header
``{"type":"session","version":3,"id":…,"timestamp":…,"cwd":…}``; every
later line is an entry ``{type,id,parentId,timestamp,…}`` forming a tree
(``parentId`` chain; header ``parentSession`` when forked).

Durable messages come from ``type=="message"`` entries with
``message.role in {"user","assistant"}``. Hippocampus recall injections
arrive as ``custom_message`` entries with ``customType=="hippocampus-memory"``
(B03 contract) — excluded structurally. Non-message entries (custom,
compaction, branch summaries, model/thinking changes, usage) are skipped
and counted.

Identity: session_id = header.id; event_id = entry.id (native, verbatim).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from . import KIND_RAW, ImportItem, Importer, _to_iso8601

logger = logging.getLogger("v3core.importers.pi_sessions")

# B03 contract: pi-adapter marks its injections with this customType.
_MEMORY_CUSTOM_TYPE = "hippocampus-memory"
_SUPPORTED_VERSIONS = (1, 2, 3)


class PiSessionImporter(Importer):
    name = "pi"
    description = (
        "pi coding agent sessions (~/.pi/agent/sessions, JSONL entry tree) "
        "→ conversation_stream as raw_message"
    )
    capability = "production"

    # ── discovery ──────────────────────────────────────────────────────

    def discover(self, root: Path) -> list[Path]:
        root = Path(root)
        if root.is_file():
            return [root] if root.suffix.lower() == ".jsonl" else []
        if not root.is_dir():
            return []

        found: list[Path] = []
        # Real layout: <root>/--<cwd>--/<file>.jsonl
        patterns = ("--*--/*.jsonl", "*.jsonl")
        seen: set[Path] = set()
        for pat in patterns:
            for p in sorted(root.glob(pat)):
                if p in seen or not p.is_file():
                    continue
                seen.add(p)
                found.append(p)
        return found

    # ── parse ──────────────────────────────────────────────────────────

    def parse(self, path: Path) -> Iterator[ImportItem]:
        path = Path(path)
        header: dict | None = None
        session_id: str | None = None

        with open(path, "r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(
                        "pi importer: skipping malformed JSON at %s:%d", path, lineno
                    )
                    continue
                if not isinstance(obj, dict):
                    continue

                if header is None and obj.get("type") == "session":
                    header = obj
                    session_id = str(obj.get("id") or "").strip() or None
                    version = obj.get("version")
                    if version is not None and version not in _SUPPORTED_VERSIONS:
                        self._track_excluded("skipped_version")
                        raise ValueError(
                            f"pi importer: session format version {version!r} in "
                            f"{path.name} is not supported (tested: v1–v3)"
                        )
                    continue

                if session_id is None:
                    continue  # cannot attribute before the header

                if obj.get("type") != "message":
                    # non-message entries: custom, compaction, branch_summary,
                    # model/thinking changes, usage, session_info …
                    self._track_excluded("skipped_entries")
                    continue

                yield from self._item_from_message(
                    obj,
                    path,
                    session_id=session_id,
                    parent_session=(header or {}).get("parentSession"),
                    cwd=(header or {}).get("cwd"),
                )

    # ── message mapping ────────────────────────────────────────────────

    def _item_from_message(
        self,
        entry: dict,
        path: Path,
        *,
        session_id: str,
        parent_session=None,
        cwd=None,
    ) -> Iterator[ImportItem]:
        message = entry.get("message")
        if not isinstance(message, dict):
            self._track_excluded("skipped_entries")
            return
        role = message.get("role")
        if role == "custom":
            # Extension-injected message (Hippocampus recall, other custom
            # types) — never a historical user/assistant fact.
            self._track_excluded("skipped_injected")
            return
        if role not in ("user", "assistant"):
            self._track_excluded("skipped_roles")
            return

        text = _text_of_content(message.get("content"))
        if not text:
            self._track_excluded("skipped_empty")
            return

        entry_id = str(entry.get("id") or "").strip()
        provenance = {
            "session_id": session_id,
            "native_id": entry_id,
            "parent_id": entry.get("parentId"),
            "artifact": path.name,
            "cwd": cwd,
        }
        if isinstance(parent_session, str) and parent_session.strip():
            provenance["parent_session"] = parent_session.strip()

        yield ImportItem(
            kind=KIND_RAW,
            source_system="pi",
            source_ref=session_id,
            text=text,
            role=role,
            occurred_at=_to_iso8601(entry.get("timestamp")),
            provenance=provenance,
            host="pi",
            event_id=entry_id or None,
            identity_kind="native" if entry_id else "import-derived",
        )


def _text_of_content(content) -> str:
    """Join text blocks (user string or block list); non-text dropped."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t.strip())
    return "\n\n".join(parts)


__all__ = ["PiSessionImporter"]
