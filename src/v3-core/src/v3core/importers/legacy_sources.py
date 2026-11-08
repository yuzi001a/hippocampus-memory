"""v3core.importers.legacy_sources — OpenClaw-era raw conversation importer.

Covers the source shapes that predate the Hermes ``state.db`` store. Every
artifact here holds **original turns**, not derived summaries — the older
``openclaw`` stub assumed "OpenClaw generates LLM summaries, not raw user
turns", which the conversation stores disprove (Honcho PG: 43k raw messages,
lcm.db, daily backups, trajectory event streams). Those are wired here as
``KIND_RAW``; the ``openclaw`` importer stays reserved for *derived* memory.

Supported shapes (auto-detected by content, not by filename):

  * ``normalized.jsonl[.gz]`` — the pipeline's own export contract:
    ``{source, host, session_id, event_id, ts, role, content}``
  * ``*.jsonl[.gz]`` — OpenClaw v3 session event stream (``type=message``)
  * ``*.jsonl`` — OpenClaw trajectory event stream (``session.started`` …)
  * ``lcm.db`` — OpenClaw SQLite conversation store
  * ``*.csv`` — Honcho messages export (``peer_name``/``content``/…)
  * ``*.sql`` — Honcho ``COPY public.messages`` pg_dump text export
  * ``*.md`` — chat_md / apr7 markdown transcripts (``- 👤`` / ``- 🤖``)

Identity contract (same as the Hermes importer, so dedupe keeps working):

  host       = source family (``honcho`` / ``lcm`` / ``trajectory`` / …)
  source_ref = ``<host>:<native session id>``
  event_id   = native message id when the artifact has one, else None
               (the writer then derives ``derived:<host>:<sess>:<n>``, which
               stays stable while parse order is deterministic)

Noise (cron / heartbeat / subagent injection / image descriptions / system
text) is classified per message and dropped — never silently: the drop counts
are logged once per artifact.
"""
from __future__ import annotations

import csv
import gzip
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import KIND_RAW, ImportItem, Importer

logger = logging.getLogger("v3core.importers.legacy_sources")

# ── noise model (ported from reimport/message_model.py) ────────────────────

_WRAPPER_RE = re.compile(
    r"(?:Sender|Conversation info) \(untrusted metadata\):\s*```json\s*\{.*?\}\s*```\s*",
    re.S,
)
_TS_PREFIX_RE = re.compile(
    r"^\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}\s+GMT[+-]\d+\]\s*"
)
_CRON_RE = re.compile(r"\[cron[:[]")
_HEARTBEAT_RE = re.compile(
    r"心跳|HEARTBEAT|HEARTBEAT_OK|heartbeat poll|OpenClaw heartbeat", re.I
)
_SUBAGENT_RE = re.compile(
    r"Subagent Context|子代理|ASYNC DELEGATION|\[IMPORTANT|OPENCLAW_INTERNAL_CONTEXT"
    r"|BEGIN_OPENCLAW_INTERNAL_CONTEXT|Inter-session message|Agent-to-agent|auto-cron"
)
_INJECTION_RE = re.compile(
    r"^\[?(?:\d{1,2}\s+\w{3}\s+\d{4}|\w{3}\s+\d{1,2}|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2})?"
    r"\s*\]?\s*\[(?:IMPORTANT|ASYNC DELEGATION|Subagent Context|OUT-OF-BAND|【IMPORTANT|【ASYNC)"
)
_SYSTEM_INJECT_RE = re.compile(
    r"^(?:Continue where you left off|Write a dream diary entry"
    r"|Agent-to-agent|You are running as a subagent"
    r"|Record a memory fragment|Read HEARTBEAT|Generate a daily summary"
    r"|This is an automated|System:|System \(untrusted\)|\[System:"
    r"|\[SYSTEM\]|\[system\]|\[CONTEXT COMPACTION|NO_REPLY"
    r"|\[Startup context loaded by runtime|\[Queued messages while agent was busy"
    r"|assistant turn failed before producing content)",
    re.IGNORECASE,
)
_IMAGE_DESC_RE = re.compile(
    r"\[The user attached an image|Detailed Description|This image shows", re.I
)
_PUA_RE = re.compile(r"[\ue000-\uf8ff]")  # GBK mis-decoding signature


def strip_ts_prefix(text: str) -> str:
    return _TS_PREFIX_RE.sub("", text, count=1)


def strip_wrapper(text: str) -> str:
    return _WRAPPER_RE.sub("", text)


def detect_mojibake(content: str) -> bool:
    return bool(_PUA_RE.search(content or ""))


def classify_noise(role: str, content: str) -> tuple[bool, str | None]:
    """Content-level noise classification. Returns ``(is_noise, reason)``."""
    if not content or not content.strip():
        return True, "empty"
    c = content.strip()
    if _CRON_RE.search(c):
        return True, "cron"
    if _HEARTBEAT_RE.search(c):
        return True, "heartbeat"
    c2 = strip_ts_prefix(c).strip()
    if _INJECTION_RE.match(c) or _SUBAGENT_RE.search(c) or _SYSTEM_INJECT_RE.match(c2):
        return True, "subagent"
    if role in ("system", "tool", "function", "tool_call", "tool_result"):
        return True, "system" if role == "system" else "tool"
    if c.startswith("HEARTBEAT_OK"):
        return True, "heartbeat"
    if _IMAGE_DESC_RE.search(c[:200]):
        return True, "image_desc"
    return False, None


def parse_ts(value: Any) -> datetime | None:
    """Tolerant timestamp parsing → aware UTC datetime (or None)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            t = float(value)
            if t > 1e11:  # milliseconds
                t /= 1000.0
            return datetime.fromtimestamp(t, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # epoch encoded as a string
    if re.fullmatch(r"\d{9,13}(\.\d+)?", s):
        return parse_ts(float(s))
    # normalise +HH → +HH00 for %z
    m = re.search(r"([+-]\d{2})$", s)
    if m:
        s = s[: m.start()] + m.group(1) + "00"
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ── item construction ──────────────────────────────────────────────────────


class _Dropped:
    """Per-artifact drop accounting (logged, never silent)."""

    __slots__ = ("counts",)

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def add(self, reason: str | None) -> None:
        key = reason or "unknown"
        self.counts[key] = self.counts.get(key, 0) + 1

    def summary(self) -> str:
        if not self.counts:
            return "dropped=0"
        return "dropped=" + ",".join(f"{k}:{v}" for k, v in sorted(self.counts.items()))


def _make_item(
    *,
    host: str,
    session: str,
    event_id: Any,
    text: str,
    role: str,
    dt: datetime | None,
    extra: dict[str, Any] | None = None,
) -> ImportItem:
    text = strip_wrapper(text).strip()
    # Idempotent namespacing: exports that already carry ``<host>:<session>``
    # must not end up as ``<host>:<host>:<session>``.
    prefix = f"{host}:"
    ref = session if session.startswith(prefix) else prefix + session
    prov: dict[str, Any] = {
        "session_id": ref,
        "native_session": session,
        "source_system": host,
    }
    if event_id not in (None, ""):
        prov["native_id"] = str(event_id)
    if extra:
        prov.update({k: v for k, v in extra.items() if v not in (None, "", [])})
    return ImportItem(
        kind=KIND_RAW,
        source_system=host,
        source_ref=ref,
        text=text,
        role=role,
        occurred_at=_iso(dt),
        provenance=prov,
        host=host,
        event_id=str(event_id) if event_id not in (None, "") else None,
        identity_kind="native" if event_id not in (None, "") else "import-derived",
    )


# ── format detection ───────────────────────────────────────────────────────

_SQLITE_MAGIC = b"SQLite format 3\x00"


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def _first_line(path: Path) -> str:
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as fh:  # type: ignore[operator]
            return fh.readline()
    except OSError:
        return ""


def detect_shape(path: Path) -> str:
    """Return one of: lcm_db, normalized_jsonl, trajectory, openclaw_jsonl,
    honcho_csv, honcho_sql, chat_md, unknown."""
    name = path.name.lower()
    if name.endswith((".db", ".sqlite", ".sqlite3")) or _is_sqlite(path):
        return "lcm_db" if _has_lcm_tables(path) else "sqlite_unknown"

    if name.endswith((".jsonl", ".ndjson", ".jsonl.gz", ".ndjson.gz")):
        head = _first_line(path)
        if "session.started" in head:
            return "trajectory"
        try:
            first = json.loads(head)
        except (json.JSONDecodeError, ValueError):
            return "unknown"
        if isinstance(first, dict):
            if {"session_id", "content", "role"} <= set(first):
                return "normalized_jsonl"
            if first.get("type") in ("message", "session", "model_change", "custom_message"):
                return "openclaw_jsonl"
        return "unknown"

    if name.endswith(".csv"):
        return "honcho_csv" if _csv_has_peer_header(path) else "unknown"

    if name.endswith((".sql", ".txt")):
        try:
            with open(path, "rt", encoding="utf-8", errors="replace") as fh:
                for _ in range(400):
                    line = fh.readline()
                    if not line:
                        break
                    if line.startswith("COPY public.messages"):
                        return "honcho_sql"
        except OSError:
            return "unknown"
        return "unknown"

    if name.endswith(".md"):
        try:
            with open(path, "rt", encoding="utf-8", errors="replace") as fh:
                sample = fh.read(20000)
        except OSError:
            return "unknown"
        if "- 👤" in sample or "- 🤖" in sample:
            return "chat_md"
        return "unknown"

    return "unknown"


def _has_lcm_tables(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return "messages" in names and ("conversations" in names or "message_parts" in names)


def _csv_has_peer_header(path: Path) -> bool:
    try:
        with open(path, "rt", encoding="utf-8", errors="replace") as fh:
            header = fh.readline().lower()
    except OSError:
        return False
    return "peer_name" in header and "content" in header


# ── importer ───────────────────────────────────────────────────────────────


class LegacySourcesImporter(Importer):
    name = "legacy_sources"
    description = (
        "OpenClaw-era raw conversation stores (honcho / lcm / trajectory / "
        "daily backups / markdown) → conversation_stream as raw_message"
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
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if detect_shape(p) != "unknown":
                found.append(p)
        return found

    # ── parsing ────────────────────────────────────────────────────────

    def parse(self, path: Path) -> Iterator[ImportItem]:
        path = Path(path)
        shape = detect_shape(path)
        dropped = _Dropped()
        handlers = {
            "lcm_db": self._parse_lcm_db,
            "normalized_jsonl": self._parse_normalized_jsonl,
            "trajectory": self._parse_trajectory,
            "openclaw_jsonl": self._parse_openclaw_jsonl,
            "honcho_csv": self._parse_honcho_csv,
            "honcho_sql": self._parse_honcho_sql,
            "chat_md": self._parse_chat_md,
        }
        handler = handlers.get(shape)
        if handler is None:
            raise ValueError(
                f"legacy_sources importer: unrecognized artifact {path.name!r} "
                f"(detected shape={shape!r})"
            )
        emitted = 0
        for item in handler(path, dropped):
            emitted += 1
            yield item
        logger.info(
            "legacy_sources: %s shape=%s emitted=%d %s",
            path.name,
            shape,
            emitted,
            dropped.summary(),
        )

    # ── normalized JSONL (pipeline export contract) ────────────────────

    def _iter_jsonl(self, path: Path) -> Iterator[dict[str, Any]]:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as fh:  # type: ignore[operator]
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj

    def _parse_normalized_jsonl(self, path: Path, dropped: _Dropped) -> Iterator[ImportItem]:
        for rec in self._iter_jsonl(path):
            if "session_id" not in rec or "content" not in rec:
                continue
            host = str(rec.get("host") or rec.get("source") or "legacy")
            role = str(rec.get("role") or "").strip() or "other"
            peer = rec.get("peer")
            if role == "other" and peer:
                # Exports may carry a pre-computed "other" role while the peer
                # name still tells us who it was (Feishu open_id, agent-*).
                role = self._honcho_role(str(peer))
            text = str(rec.get("content") or "")
            is_noise, reason = classify_noise(role, text)
            if is_noise:
                dropped.add(reason)
                continue
            yield _make_item(
                host=host,
                session=str(rec.get("session_id")),
                event_id=rec.get("event_id"),
                text=text,
                role=role,
                dt=parse_ts(rec.get("ts") or rec.get("timestamp")),
                extra={"seq": rec.get("seq")},
            )

    # ── lcm.db ─────────────────────────────────────────────────────────

    def _parse_lcm_db(self, path: Path, dropped: _Dropped) -> Iterator[ImportItem]:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            sess_col = "conversation_id" if "conversation_id" in cols else "session_id"
            ts_col = "created_at" if "created_at" in cols else "timestamp"
            id_col = "message_id" if "message_id" in cols else "id"
            seq_col = "seq" if "seq" in cols else "0"
            sql = (
                f"SELECT {id_col} AS mid, {sess_col} AS sess, role, content, "
                f"{ts_col} AS ts, {seq_col} AS seq FROM messages ORDER BY sess, seq, mid"
            )
            for row in conn.execute(sql):
                role = str(row["role"] or "").strip().lower()
                text = str(row["content"] or "")
                is_noise, reason = classify_noise(role, text)
                if is_noise:
                    dropped.add(reason)
                    continue
                yield _make_item(
                    host="lcm",
                    session=str(row["sess"]),
                    event_id=row["mid"],
                    text=text,
                    role=role,
                    dt=parse_ts(row["ts"]),
                    extra={"seq": row["seq"]},
                )
        finally:
            conn.close()

    # ── OpenClaw trajectory event stream ───────────────────────────────

    _TRAJ_USER_RE = re.compile(
        r"\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{4}-\d{2}-\d{2} \d{2}:\d{2} GMT[+-]\d+\] ([^\n]+)"
    )

    def _parse_trajectory(self, path: Path, dropped: _Dropped) -> Iterator[ImportItem]:
        session_key = ""
        for ev in self._iter_jsonl(path):
            etype = ev.get("type") or ev.get("event") or ""
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            if etype == "session.started":
                session_key = str(data.get("sessionKey") or data.get("sessionId") or "")
                if ":cron:" in session_key:
                    return  # whole session is a cron turn
            if etype == "prompt.submitted":
                prompt = str(data.get("prompt") or data.get("text") or "")
                for m in self._TRAJ_USER_RE.finditer(prompt):
                    line = m.group(1).strip()
                    is_noise, reason = classify_noise("user", line)
                    if is_noise:
                        dropped.add(reason)
                        continue
                    yield _make_item(
                        host="trajectory",
                        session=session_key or path.stem,
                        event_id=None,
                        text=line,
                        role="user",
                        dt=parse_ts(ev.get("ts") or ev.get("timestamp") or data.get("ts")),
                    )
            if etype == "model.completed":
                texts = data.get("assistantTexts") or []
                if isinstance(texts, str):
                    texts = [texts]
                for i, t in enumerate(texts):
                    body = str(t or "")
                    is_noise, reason = classify_noise("assistant", body)
                    if is_noise:
                        dropped.add(reason)
                        continue
                    yield _make_item(
                        host="trajectory",
                        session=session_key or path.stem,
                        event_id=f"{path.stem}:{i}",
                        text=body,
                        role="assistant",
                        dt=parse_ts(ev.get("ts") or ev.get("timestamp") or data.get("ts")),
                    )

    # ── OpenClaw v3 session event stream ───────────────────────────────

    def _parse_openclaw_jsonl(self, path: Path, dropped: _Dropped) -> Iterator[ImportItem]:
        session = path.stem
        idx = 0
        for ev in self._iter_jsonl(path):
            if ev.get("type") != "message":
                continue
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
            role = str(msg.get("role") or "").strip().lower()
            content = msg.get("content")
            parts: list[str] = []
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") in ("text", "output_text"):
                        parts.append(str(p.get("text") or ""))
            text = "\n".join(x for x in parts if x).strip()
            if not text:
                dropped.add("empty")
                continue
            is_noise, reason = classify_noise(role, text)
            if is_noise:
                dropped.add(reason)
                continue
            idx += 1
            yield _make_item(
                host="openclaw_jsonl",
                session=str(ev.get("sessionId") or msg.get("sessionId") or session),
                event_id=msg.get("id") or ev.get("id") or f"{path.stem}:{idx}",
                text=text,
                role=role or "other",
                dt=parse_ts(ev.get("timestamp") or msg.get("timestamp") or ev.get("ts")),
            )

    # ── Honcho CSV export ──────────────────────────────────────────────

    def _parse_honcho_csv(self, path: Path, dropped: _Dropped) -> Iterator[ImportItem]:
        with open(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
            for row in csv.DictReader(fh):
                role = self._honcho_role(row.get("peer_name") or "")
                text = row.get("content") or ""
                is_noise, reason = classify_noise(role, text)
                if is_noise:
                    dropped.add(reason)
                    continue
                yield _make_item(
                    host="honcho",
                    session=str(row.get("session_name") or row.get("session_id") or "honcho"),
                    event_id=row.get("id") or row.get("public_id"),
                    text=text,
                    role=role,
                    dt=parse_ts(row.get("created_at") or row.get("timestamp")),
                    extra={"peer": row.get("peer_name"), "seq": row.get("seq_in_session")},
                )

    # ── Honcho pg_dump COPY text export ────────────────────────────────

    _COPY_COLS = (
        "id",
        "public_id",
        "content",
        "metadata",
        "created_at",
        "peer_name",
        "workspace_name",
        "session_name",
        "token_count",
        "internal_metadata",
        "seq_in_session",
    )

    def _parse_honcho_sql(self, path: Path, dropped: _Dropped) -> Iterator[ImportItem]:
        header: list[str] = list(self._COPY_COLS)
        in_copy = False
        with open(path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not in_copy:
                    if line.startswith("COPY public.messages"):
                        in_copy = True
                        m = re.search(r"\((.*?)\)\s+FROM stdin", line)
                        if m:
                            header = [c.strip().strip('"') for c in m.group(1).split(",")]
                    continue
                if line.rstrip("\n") == "\\.":
                    break
                parts = line.rstrip("\n").split("\t")
                if len(parts) < len(header):
                    continue
                rec = dict(zip(header, parts))
                content = (
                    rec.get("content", "")
                    .replace("\\t", "\t")
                    .replace("\\n", "\n")
                    .replace("\\\\", "\\")
                )
                if content == "\\N":
                    content = ""
                role = self._honcho_role(rec.get("peer_name") or "")
                is_noise, reason = classify_noise(role, content)
                if is_noise:
                    dropped.add(reason)
                    continue
                yield _make_item(
                    host="honcho",
                    session=str(rec.get("session_name") or "honcho"),
                    event_id=rec.get("id") or rec.get("public_id"),
                    text=content,
                    role=role,
                    dt=parse_ts(rec.get("created_at")),
                    extra={"peer": rec.get("peer_name"), "seq": rec.get("seq_in_session")},
                )

    # ── markdown transcripts (chat_md / apr7) ──────────────────────────

    _MD_USER_RE = re.compile(r"^- (?:👤|🧑)\s+(.*)$")
    _MD_ASST_RE = re.compile(r"^- (?:🤖|🦾)\s+(.*)$")
    _MD_TS_RE = re.compile(r"^\*\*(\d{1,2}:\d{2})\*\*:?\s*(.*)$")
    _DATE_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})")
    _SECTION_TIME_RE = re.compile(r"^#{3,}\s*(\d{1,2}:\d{2})\b")
    _FRONT_DATE_RE = re.compile(r"^date:\s*([\d-]+)", re.M)

    def _parse_chat_md(self, path: Path, dropped: _Dropped) -> Iterator[ImportItem]:
        text = path.read_text(encoding="utf-8", errors="replace")
        m = self._FRONT_DATE_RE.search(text)
        cur_date = m.group(1) if m else None
        cur_clock: str | None = None
        idx = 0
        for raw_line in text.splitlines():
            line = raw_line.strip()
            dm = self._DATE_RE.match(line)
            if dm:
                cur_date = dm.group(1)
                continue
            sm = self._SECTION_TIME_RE.match(line)
            if sm:
                cur_clock = sm.group(1)
                continue
            mm = self._MD_USER_RE.match(line) or self._MD_ASST_RE.match(line)
            if not mm:
                continue
            role = "user" if self._MD_USER_RE.match(line) else "assistant"
            body = mm.group(1).strip()
            tm = self._MD_TS_RE.match(body)
            clock = None
            if tm:
                clock, body = tm.group(1), tm.group(2).strip()
            is_noise, reason = classify_noise(role, body)
            if is_noise:
                dropped.add(reason)
                continue
            idx += 1
            dt = None
            inline = _TS_PREFIX_RE.match(body)
            if inline and cur_date is None:
                # No frontmatter date — the line carries its own stamp.
                dt = parse_ts(body[: inline.end()].strip("[]"))
            if dt is None and cur_date:
                hhmm = clock or cur_clock or "00:00"
                dt = parse_ts(f"{cur_date}T{hhmm}:00")
            yield _make_item(
                host="chat_md",
                session=f"{path.stem}",
                event_id=f"{path.stem}:{idx}",
                text=strip_ts_prefix(body),
                role=role,
                dt=dt,
            )

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _honcho_role(peer: str) -> str:
        p = (peer or "").strip().lower()
        if p in ("owner", "user", "human", "test-peer") or p.startswith(("ou_", "fire")):
            # ``ou_*`` is a Feishu open_id — the human on the other end, not a bot.
            return "user"
        if p.startswith(("agent", "assistant", "bot")):
            return "assistant"
        return "other"


__all__ = ["LegacySourcesImporter", "classify_noise", "parse_ts", "detect_shape"]
