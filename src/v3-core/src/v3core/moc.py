"""shou zhang (MOC) management — single-file JSON overview + section architecture

Design:
- Single overview.json (not 157 individual .md files)
- Every entry has a -> pointer to the actual card (source_id)
- v3_store auto-registers cards on write (category='shou_zhang')
- No empty-shell entries possible
- sync() scans cards/shou_zhang/ for missing entries
- s/system_state.md stays as free-form journal (unchanged)
"""
from __future__ import annotations
import json
import logging
import re
from pathlib import Path
from datetime import datetime
from typing import Any

logger = logging.getLogger("v3core.moc")

NL = chr(10)

# Regex to parse card frontmatter — works for both old manual format and YAML dump
_FM_TITLE_RE = re.compile(r"^title:\s*(.+)$", re.MULTILINE)
_FM_CAT_RE = re.compile(r"^category:\s*(.+)$", re.MULTILINE)
_FM_DATE_RE = re.compile(r"^date:\s*(.+)$", re.MULTILINE)


def _parse_frontmatter(filepath: Path) -> dict:
    """Read frontmatter from a card .md file, return {title, category, date}."""
    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    fm = text[3:end].strip()
    title = ""
    cat = ""
    date = ""
    m = _FM_TITLE_RE.search(fm)
    if m:
        title = m.group(1).strip()
    # If no title: field, try first H1 in body (old plugin format)
    if not title:
        body = text[end + 3:].strip()
        h1 = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        if h1:
            title = h1.group(1).strip()
    m = _FM_CAT_RE.search(fm)
    if m:
        cat = m.group(1).strip()
    m = _FM_DATE_RE.search(fm)
    if m:
        date = m.group(1).strip()
    return {"title": title, "category": cat, "date": date}


class MOCManager:
    """Single-file shou zhang index. Every entry has a pointer to its card."""

    def __init__(self, base_path: str | Path):
        self._moc_dir = Path(base_path) / "moc"
        self._moc_dir.mkdir(parents=True, exist_ok=True)
        self._idx_path = self._moc_dir / "overview.json"
        self._entries: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if self._idx_path.exists():
            try:
                data = json.loads(self._idx_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("entries", [])
            except Exception:
                return []
        return []

    def _save(self):
        self._idx_path.write_text(
            json.dumps(
                {"entries": self._entries, "updated_at": datetime.now().isoformat()},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    def _find(self, key: str) -> dict | None:
        for e in self._entries:
            if e.get("key") == key or e.get("pointer") == key:
                return e
        return None

    def overview(self) -> str:
        if not self._entries:
            return "shou zhang is empty"
        lines = ["## shou zhang overview"]
        for e in self._entries:
            ptr = f" -> {e['pointer']}" if e.get("pointer") else ""
            cat = f"[{e.get('category','')}]" if e.get("category") else ""
            lines.append(f"- {e['title']} {cat} {e.get('date','')}{ptr}")
        return NL.join(lines)

    def get(self, key: str) -> str:
        e = self._find(key)
        if not e:
            return f"shou zhang entry '{key}' not found"
        body = f"# {e['title']}\n\n"
        if e.get("date"):
            body += f"{e['date']}\n\n"
        if e.get("summary"):
            body += f"{e['summary']}\n\n"
        if e.get("pointer"):
            body += f"-> {e['pointer']}\n"
        return body

    def register(self, title: str, category: str = "", pointer: str = "",
                 date: str = "", key: str = "", summary: str = "") -> str:
        if not key:
            key = title.strip().replace(" ", "-").replace("/", "-")[:80]
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        existing = self._find(key) or self._find(pointer)
        if existing:
            return f"Already registered: {key}"
        entry = {
            "key": key,
            "title": title,
            "category": category,
            "date": date,
            "pointer": pointer,
            "summary": summary or "",
            "created_at": datetime.now().isoformat(),
        }
        self._entries.append(entry)
        self._save()
        return f"Registered '{title}' in shou zhang"





    def sync(self, cards_root: Path | None = None) -> str:
        """Scan cards/shou_zhang/ for unregistered cards and add them to MOC.
        
        Args:
            cards_root: Path to cards/ directory. If None, skip (no scanning possible).
        """
        if cards_root is None:
            return json.dumps({
                "success": True,
                "message": "No cards_root provided, skipped scan",
                "synced": len(self._entries),
                "added": 0,
            }, ensure_ascii=False)

        sz_dir = Path(cards_root) / "shou_zhang"
        if not sz_dir.is_dir():
            return json.dumps({
                "success": True,
                "message": f"shou_zhang dir not found: {sz_dir}",
                "synced": len(self._entries),
                "added": 0,
            }, ensure_ascii=False)

        existing_pointers = {e.get("pointer", "") for e in self._entries}
        added = 0
        errors = 0

        for fpath in sorted(sz_dir.glob("*.md")):
            rel = f"shou_zhang/{fpath.name}"
            if rel in existing_pointers:
                continue
            fm = _parse_frontmatter(fpath)
            title = fm.get("title") or fpath.stem
            cat = fm.get("category") or "shou_zhang"
            date = fm.get("date") or ""
            self.register(title=title, category=cat, pointer=rel, date=date)
            added += 1

        return json.dumps({
            "success": True,
            "message": f"Scanned {sz_dir}, added {added} entries",
            "synced": len(self._entries),
            "added": added,
            "errors": errors,
        }, ensure_ascii=False)
