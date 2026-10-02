"""v3core.importers.dsh_sessions — DSH session importer (I01).

Targets the **current official format**: DSH `0.2.0-rc.2`
(`session.v4.jsonl.zstd`, upstream SHA 639ed015…). v3 files are tolerated
where field shapes are identical; anything else is reported, not guessed.

On-disk layout (bounded, host-specific):

    <DSH_HOME>/sessions/--<normalized-cwd>--/<session-id>/session.vN.jsonl.zstd

Physical format (session-persistence-jsonl): concatenated zstd frames
wrapping JSONL — line 0 is the header ``{"type":"session","version":4,…}``,
every later line one JSON event ``{type,seq,time,data,surfaceOp?}``.

Durable messages come from exactly two event types:

  * ``user/message``      — data is the UserMessage itself; keep only
    ``source.kind == "user"`` (human input). Hippocampus recall injections
    arrive with ``source.kind == "hippocampus"`` (B04 contract) and are
    excluded structurally, never by text matching.
  * ``assistant/message`` — data.message is the AssistantMessage; skip
    ``data.interrupted == true`` (not a completed answer).

Identity: session_id = header.id; event_id = native MessageId. Neither is
ever invented here. Timestamps: event ``time`` (epoch ms) → ISO8601.

Counted exclusions (``excluded_counts``): skipped_injected, skipped_roles,
skipped_interrupted, skipped_nontext, skipped_version.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from . import KIND_RAW, ImportItem, Importer, _to_iso8601

logger = logging.getLogger("v3core.importers.dsh_sessions")

try:  # zstandard is a hard requirement for DSH's default storage; degrade loudly
    import zstandard as _zstd  # type: ignore
except Exception:  # pragma: no cover
    _zstd = None  # type: ignore

_SUPPORTED_VERSIONS = (3, 4)
_MAX_JSONL_LINES = 2_000_000  # sanity bound; DSH sessions are append-only and small


class DshSessionImporter(Importer):
    name = "dsh"
    description = (
        "DeepSeek Harness session store (session.v4.jsonl.zstd, zstd JSONL) "
        "→ conversation_stream as raw_message"
    )
    capability = "production"

    # ── discovery ──────────────────────────────────────────────────────

    def discover(self, root: Path) -> list[Path]:
        root = Path(root)
        if root.is_file():
            return [root] if self._looks_like_session(root) else []
        if not root.is_dir():
            return []

        found: list[Path] = []
        # Real layout: <root>/--<cwd>--/<session-dir>/session.vN.jsonl.zstd
        # Tolerated:   <root>/<session-dir>/session.vN.jsonl.zstd
        #              <root>/session.vN.jsonl.zstd (flat / custom dir)
        patterns = (
            "--*--/*/session.v*.jsonl.zst*",
            "*/session.v*.jsonl.zst*",
            "session.v*.jsonl.zst*",
        )
        seen: set[Path] = set()
        for pat in patterns:
            for p in sorted(root.glob(pat)):
                if p in seen or not p.is_file():
                    continue
                if self._looks_like_session(p):
                    seen.add(p)
                    found.append(p)
        return found

    @staticmethod
    def _looks_like_session(p: Path) -> bool:
        name = p.name
        return name.startswith("session.v") and ".jsonl.zst" in name

    # ── parse ──────────────────────────────────────────────────────────

    def parse(self, path: Path) -> Iterator[ImportItem]:
        path = Path(path)
        if _zstd is None:
            raise RuntimeError(
                "dsh importer requires the 'zstandard' package to read "
                "session.v*.jsonl.zstd files — install it (pip install zstandard)"
            )

        dctx = _zstd.ZstdDecompressor()
        with open(path, "rb") as fh:
            # DSH appends checksummed zstd frames; read across ALL frames.
            try:
                reader = dctx.stream_reader(fh, read_across_frames=True)
            except TypeError:  # older python-zstandard without the kwarg
                reader = dctx.stream_reader(fh)
            with reader:
                data = reader.read()
        text = data.decode("utf-8", errors="replace")

        header: dict | None = None
        session_id: str | None = None

        for lineno, raw in enumerate(text.splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            if lineno > _MAX_JSONL_LINES:
                logger.warning("dsh importer: %s exceeds line bound; stopping", path)
                break
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "dsh importer: skipping malformed JSON at %s:%d", path, lineno
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
                        f"dsh importer: session format version {version!r} in "
                        f"{path.name} is not supported (tested: v3, v4)"
                    )
                continue

            if session_id is None:
                # No header yet — cannot attribute messages; skip defensively.
                continue

            etype = obj.get("type")
            if etype == "user/message":
                yield from self._item_from_user_message(
                    obj, path, session_id=session_id
                )
            elif etype == "assistant/message":
                yield from self._item_from_assistant_message(
                    obj, path, session_id=session_id
                )
            # All other event types are session plumbing (turn/step markers,
            # request envelopes, titles, logs) — not durable messages.

    # ── event mapping ──────────────────────────────────────────────────

    def _item_from_user_message(
        self, obj: dict, path: Path, *, session_id: str
    ) -> Iterator[ImportItem]:
        data = obj.get("data")
        if not isinstance(data, dict):
            return
        source = data.get("source")
        kind = (source or {}).get("kind") if isinstance(source, dict) else None
        if kind == "user":
            pass
        else:
            # hippocampus / runtime-context / skill-catalog / any other
            # injected role — never imported as a historical user fact.
            self._track_excluded("skipped_injected")
            return
        msg_id = str(data.get("id") or "").strip()
        text = _text_of_content(data.get("content"))
        if not text:
            # empty user content — nothing durable to import
            self._track_excluded("skipped_empty")
            return
        yield ImportItem(
            kind=KIND_RAW,
            source_system="dsh",
            source_ref=session_id,
            text=text,
            role="user",
            occurred_at=_to_iso8601(obj.get("time")),
            provenance={
                "session_id": session_id,
                "native_id": msg_id,
                "source_kind": kind,
                "seq": obj.get("seq"),
                "artifact": path.name,
            },
            host="dsh",
            event_id=msg_id or None,
            identity_kind="native" if msg_id else "import-derived",
        )

    def _item_from_assistant_message(
        self, obj: dict, path: Path, *, session_id: str
    ) -> Iterator[ImportItem]:
        data = obj.get("data")
        if not isinstance(data, dict):
            return
        if data.get("interrupted") is True:
            self._track_excluded("skipped_interrupted")
            return
        message = data.get("message")
        if not isinstance(message, dict):
            return
        msg_id = str(message.get("id") or "").strip()
        text = _text_of_content(message.get("content"))
        if not text:
            self._track_excluded("skipped_empty")
            return
        yield ImportItem(
            kind=KIND_RAW,
            source_system="dsh",
            source_ref=session_id,
            text=text,
            role="assistant",
            occurred_at=_to_iso8601(obj.get("time")),
            provenance={
                "session_id": session_id,
                "native_id": msg_id,
                "turn": data.get("turn"),
                "step": data.get("step"),
                "seq": obj.get("seq"),
                "artifact": path.name,
            },
            host="dsh",
            event_id=msg_id or None,
            identity_kind="native" if msg_id else "import-derived",
        )


def _text_of_content(content) -> str:
    """Join text blocks; non-text blocks are dropped (counted by caller)."""
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
        # non-text blocks (thinking/image/tool) deliberately skipped (v1)
    return "\n\n".join(parts)


__all__ = ["DshSessionImporter"]
