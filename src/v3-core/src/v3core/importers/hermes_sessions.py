"""v3core.importers.hermes_sessions — production Hermes importer.

Supports three discoverable source shapes (liberal in what we accept):

  1. A directory containing ``state.db`` (the canonical Hermes store).
  2. A directory containing ``*.jsonl`` or ``*.json`` exports — each line
     (or array element) is one record with at least role/content.
  3. A single file path (state.db / .jsonl / .json / sqlite file).

Hermes ``state.db`` is introspected via ``PRAGMA table_info`` so a schema
variance (older/newer column names) does not silently break import. The
known schema (see src/v3core/adapter_hermes_state.py) has ``messages`` and
``sessions``; older/different columns are mapped by candidate names.

Provenance contract (per item):
  * source_system = "hermes"
  * source_ref    = "<artifact_path>::<session_id>"
  * role          = mapped from messages.role
  * occurred_at   = ISO8601 string from messages.timestamp / created_at
  * provenance    = {
        "session_id": ...,         # repeated for downstream convenience
        "turn_id":     ...,        # row order within session
        "tool_calls":  ...,        # JSON, when present
        "tool_results": ...,       # JSON, when present
        "schema_columns": [...],   # actual columns introspected
        "raw_id":       ...,       # original row id (sqlite) when known
    }

Dedupe (live writes): handled in v3core.importers._emit_raw_messages via
``INSERT ... WHERE NOT EXISTS (session_id, role, timestamp)``. A re-run
of the same Hermes import is therefore a no-op.

Counted once at write time:
  * raw_messages — number of new rows committed
  * sessions     — distinct session_id values seen
  * oldest/newest — min/max of parsed timestamps

This module does NOT touch PG itself — it only yields ImportItems. The
caller (``import_source``) owns the pool / lease lifecycle.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Iterator

from . import KIND_RAW, ImportItem, Importer

logger = logging.getLogger("v3core.importers.hermes_sessions")


# Column candidates — introspected first, mapped by name.
_MSG_COL_CANDIDATES = {
    "role": ("role", "speaker", "from"),
    "content": ("content", "text", "message", "body"),
    "timestamp": ("timestamp", "created_at", "ts", "created"),
    "session_id": ("session_id", "conversation_id", "conv_id", "thread_id"),
    "id": ("id", "rowid", "message_id"),
    "tool_calls": ("tool_calls", "tool_call", "toolcall_json"),
    "tool_results": ("tool_results", "tool_result"),
}

_SESSION_COL_CANDIDATES = {
    "id": ("id", "session_id"),
    "title": ("title", "name"),
}

# Filenames we recognize as the canonical Hermes store.
_STATE_DB_NAMES = ("state.db", "hermes_state.db", "sessions.db")
_JSONL_NAMES = (".jsonl", ".ndjson")
_JSON_NAMES = (".json",)


class HermesSessionImporter(Importer):
    name = "hermes"
    description = (
        "Hermes session store (state.db SQLite + .jsonl/.json exports) "
        "→ conversation_stream as raw_message"
    )
    capability = "production"

    # ── discovery ──────────────────────────────────────────────────────

    def discover(self, root: Path) -> list[Path]:
        root = Path(root)
        if root.is_file():
            return [root]
        if not root.is_dir():
            return []
        found: list[Path] = []
        # state.db first (canonical store) — sort by name to keep tests
        # deterministic.
        for name in _STATE_DB_NAMES:
            p = root / name
            if p.exists() and p.is_file():
                found.append(p)
        # Then JSONL / JSON exports at the top level. Recursive would be
        # too generous for v0.2 — top-level only.
        for entry in sorted(root.iterdir()):
            if not entry.is_file():
                continue
            suffix = entry.suffix.lower()
            if suffix in _JSONL_NAMES or suffix in _JSON_NAMES:
                if entry not in found:
                    found.append(entry)
        return found

    # ── parse ──────────────────────────────────────────────────────────

    def parse(self, path: Path) -> Iterator[ImportItem]:
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix == ".db" or path.name in _STATE_DB_NAMES:
            yield from self._parse_state_db(path)
        elif suffix in _JSONL_NAMES:
            yield from self._parse_jsonl(path)
        elif suffix in _JSON_NAMES:
            yield from self._parse_json(path)
        else:
            raise ValueError(
                f"hermes importer: unrecognized artifact {path.name!r}"
            )

    # ── sqlite path ────────────────────────────────────────────────────

    def _parse_state_db(self, path: Path) -> Iterator[ImportItem]:
        conn = sqlite3.connect(str(path))
        try:
            conn.row_factory = sqlite3.Row
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "messages" not in tables:
                # Could be a Hermes store that named the table differently
                # (very old / very new); try to locate by column shape.
                msg_table = self._locate_messages_table(conn, tables)
                sess_table = (
                    "sessions"
                    if "sessions" in tables
                    else self._locate_sessions_table(conn, tables)
                )
            else:
                msg_table = "messages"
                sess_table = "sessions" if "sessions" in tables else None

            if msg_table is None:
                raise ValueError(
                    f"hermes importer: no messages-like table in {path.name!r} "
                    f"(tables={sorted(tables)!r})"
                )

            msg_cols = self._table_columns(conn, msg_table)
            mapping = self._map_columns(msg_cols, _MSG_COL_CANDIDATES)
            if "content" not in mapping or "role" not in mapping:
                raise ValueError(
                    f"hermes importer: {msg_table!r} lacks role/content "
                    f"(cols={sorted(msg_cols)!r})"
                )

            sess_id_col = mapping.get("session_id")
            role_col = mapping["role"]
            content_col = mapping["content"]
            ts_col = mapping.get("timestamp")
            id_col = mapping.get("id")
            tc_col = mapping.get("tool_calls")
            tr_col = mapping.get("tool_results")

            select_cols = [role_col, content_col]
            if sess_id_col:
                select_cols.append(sess_id_col)
            if ts_col:
                select_cols.append(ts_col)
            if id_col:
                select_cols.append(id_col)
            if tc_col:
                select_cols.append(tc_col)
            if tr_col:
                select_cols.append(tr_col)

            order_clause = f"ORDER BY {id_col}" if id_col else ""
            sql = (
                f"SELECT {', '.join(select_cols)} "
                f"FROM {msg_table} {order_clause}"
            )
            cur = conn.execute(sql)
            for row in cur:
                role = row[role_col] if role_col else None
                content = row[content_col]
                if content is None or not str(content).strip():
                    continue
                session_id = (
                    row[sess_id_col] if sess_id_col else None
                ) or f"hermes:ungrouped:{path.name}"
                ts_raw = row[ts_col] if ts_col else None
                occurred = _coerce_ts(ts_raw)
                raw_id = row[id_col] if id_col else None
                tc_raw = row[tc_col] if tc_col else None
                tr_raw = row[tr_col] if tr_col else None
                provenance = {
                    "session_id": session_id,
                    "schema_columns": sorted(msg_cols),
                    "raw_id": raw_id,
                    "tool_calls": _coerce_json(tc_raw) if tc_raw else None,
                    "tool_results": _coerce_json(tr_raw) if tr_raw else None,
                    "artifact": path.name,
                }
                # Strip None-valued tool fields so the downstream writer
                # doesn't serialize 'null'.
                provenance = {k: v for k, v in provenance.items() if v not in (None, "")}
                # turn_id is row order within session — useful for downstream
                # consumers that key by (session_id, turn_id).
                provenance.setdefault("turn_id", raw_id)

                yield ImportItem(
                    kind=KIND_RAW,
                    source_system=self.name,
                    source_ref=str(session_id),
                    text=str(content),
                    role=str(role) if role is not None else "assistant",
                    occurred_at=occurred,
                    provenance=provenance,
                )
        finally:
            conn.close()

    def _locate_messages_table(
        self, conn: sqlite3.Connection, tables: set[str]
    ) -> str | None:
        """Find a messages-like table when schema diverges from canonical.

        A table is 'messages-like' if its columns match the candidate sets
        for both ``role`` and ``content`` (not necessarily by those exact
        names — Hermes has historically renamed either column).
        """
        role_set = {c.lower() for c in _MSG_COL_CANDIDATES["role"]}
        content_set = {c.lower() for c in _MSG_COL_CANDIDATES["content"]}
        for t in tables:
            cols = {c.lower() for c in self._table_columns(conn, t)}
            if cols & role_set and cols & content_set:
                return t
        return None

    def _locate_sessions_table(
        self, conn: sqlite3.Connection, tables: set[str]
    ) -> str | None:
        for t in tables:
            cols = {c.lower() for c in self._table_columns(conn, t)}
            if "title" in cols or "started_at" in cols:
                return t
        return None

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    @staticmethod
    def _map_columns(
        actual_cols: set[str], candidates: dict[str, tuple[str, ...]]
    ) -> dict[str, str]:
        """Return {logical: actual} for every logical key whose candidate set
        contains at least one column present in actual_cols."""
        out: dict[str, str] = {}
        lowered = {c.lower(): c for c in actual_cols}
        for logical, opts in candidates.items():
            for opt in opts:
                if opt.lower() in lowered:
                    out[logical] = lowered[opt.lower()]
                    break
        return out

    # ── jsonl path ─────────────────────────────────────────────────────

    def _parse_jsonl(self, path: Path) -> Iterator[ImportItem]:
        with open(path, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(
                        "hermes importer: skipping malformed JSON at %s:%d",
                        path, lineno,
                    )
                    continue
                yield from self._item_from_json_obj(obj, path, lineno=lineno)

    # ── json path ──────────────────────────────────────────────────────

    def _parse_json(self, path: Path) -> Iterator[ImportItem]:
        with open(path, "r", encoding="utf-8") as f:
            try:
                obj = json.load(f)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"hermes importer: JSON parse failed for {path.name}: {exc}"
                ) from exc
        if isinstance(obj, list):
            for i, item in enumerate(obj, start=1):
                yield from self._item_from_json_obj(item, path, lineno=i)
        elif isinstance(obj, dict):
            # Heuristics: top-level object could be one record or a
            # {"messages": [...]} / {"records": [...]} envelope.
            for key in ("messages", "records", "items", "data"):
                if key in obj and isinstance(obj[key], list):
                    for i, item in enumerate(obj[key], start=1):
                        yield from self._item_from_json_obj(
                            item, path, lineno=i
                        )
                    return
            # Otherwise: single record.
            yield from self._item_from_json_obj(obj, path, lineno=1)
        else:
            raise ValueError(
                f"hermes importer: JSON must be object or list, got {type(obj).__name__}"
            )

    @staticmethod
    def _item_from_json_obj(
        obj: dict, path: Path, *, lineno: int
    ) -> Iterator[ImportItem]:
        if not isinstance(obj, dict):
            return
        role = (
            obj.get("role")
            or obj.get("speaker")
            or obj.get("from")
            or "assistant"
        )
        text = (
            obj.get("content")
            or obj.get("text")
            or obj.get("message")
            or obj.get("body")
            or ""
        )
        if not str(text).strip():
            return
        session_id = (
            obj.get("session_id")
            or obj.get("conversation_id")
            or obj.get("conv_id")
            or obj.get("thread_id")
            or f"hermes:ungrouped:{path.name}"
        )
        ts_raw = (
            obj.get("timestamp")
            or obj.get("created_at")
            or obj.get("ts")
            or obj.get("created")
        )
        occurred = _coerce_ts(ts_raw)
        provenance = {
            "session_id": str(session_id),
            "artifact": path.name,
            "lineno": lineno,
            "tool_calls": obj.get("tool_calls") or obj.get("tool_call"),
            "tool_results": obj.get("tool_results") or obj.get("tool_result"),
            "raw_id": obj.get("id"),
            "turn_id": obj.get("turn_id") or obj.get("id"),
        }
        provenance = {k: v for k, v in provenance.items() if v not in (None, "")}
        yield ImportItem(
            kind=KIND_RAW,
            source_system="hermes",
            source_ref=str(session_id),
            text=str(text),
            role=str(role),
            occurred_at=occurred,
            provenance=provenance,
        )


# ── helpers ─────────────────────────────────────────────────────────────


def _coerce_ts(value) -> str | None:
    """Best-effort ISO8601 string for a SQLite column.

    SQLite columns can hold ISO strings, epoch ints/strings, or Python
    datetime objects when reading via sqlite3.Row. We accept all three.
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return None
    if isinstance(value, (int, float)):
        return _epoch_to_iso(value)
    s = str(value).strip()
    if not s:
        return None
    if s.lstrip("-").isdigit():
        try:
            return _epoch_to_iso(int(s))
        except Exception:
            return s
    # Already looks ISO-shaped.
    return s


def _epoch_to_iso(value: float) -> str:
    from datetime import datetime, timezone
    v = float(value)
    if v > 1e12:  # milliseconds
        v = v / 1000.0
    return (
        datetime.fromtimestamp(v, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _coerce_json(raw) -> object | None:
    """Pass-through if already structured; try parse if string; else None."""
    if raw is None:
        return None
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None
    return None


__all__ = ["HermesSessionImporter"]
