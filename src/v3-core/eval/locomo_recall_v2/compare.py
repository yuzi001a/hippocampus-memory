"""LoCoMo Recall v2 result differential (G6C-A).

Compares two JSONL result files by stable ``case_id`` and
reports old/new/delta per case, plus a flat list of changed /
missing / extra cases.

The contract:

  * Input files are line-delimited JSON (JSONL). Each line is
    one record with at least a string ``case_id`` field.
  * Comparison is by ``case_id`` set, **never** by row order.
    Two runs in different orders over the same case set are
    considered identical for ordering purposes.
  * Output is a deterministic :class:`DeltaReport` dataclass
    that round-trips through ``json.dumps``.
  * Old/new values are compared by JSON-equality (sorted-keys,
    tight separators). Two payloads that differ only in
    key-order or whitespace still compare equal.
  * ``missing_in_old`` = cases present in NEW but absent in OLD.
  * ``missing_in_new`` = cases present in OLD but absent in NEW.
  * ``extra_in_old`` / ``extra_in_new`` are aliases for the
    same sets — kept explicit so callers don't need to
    remember the direction.

Fail-closed semantics:

  * A malformed line raises :class:`IncompatibleResultSets`.
  * A missing ``case_id`` raises :class:`IncompatibleResultSets`.
  * Two records with the same ``case_id`` in one file raise
    :class:`IncompatibleResultSets` (the diff would be
    ambiguous).

This module is read-only with respect to the runner, the
loader, and the lab. It loads pre-existing JSONL files; it
never re-runs the adapter and never opens a connection.
"""
from __future__ import annotations

import dataclasses
import json
import os
from typing import Any, Iterable


class IncompatibleResultSets(ValueError):
    """Raised when two JSONL result files cannot be diffed."""


@dataclasses.dataclass(frozen=True)
class CaseDelta:
    """One case that exists in both OLD and NEW.

    ``old`` / ``new`` are the parsed JSON payloads (dicts).
    ``changed`` is the boolean diff result. ``changed_keys`` is
    the sorted list of top-level keys whose JSON value differs.
    """

    case_id: str
    old: Any
    new: Any
    changed: bool
    changed_keys: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class DeltaReport:
    """Full offline differential report.

    ``old_path`` / ``new_path`` are the input file paths.
    ``old_count`` / ``new_count`` are the parsed line counts.
    ``changed_cases`` are cases whose JSON value differs.
    ``unchanged_cases`` are cases whose JSON value matches.
    ``missing_in_new`` are cases present in OLD only.
    ``missing_in_old`` are cases present in NEW only.
    """

    old_path: str
    new_path: str
    old_count: int
    new_count: int
    changed_cases: tuple[CaseDelta, ...]
    unchanged_cases: tuple[str, ...]
    missing_in_new: tuple[str, ...]   # in OLD but not NEW
    missing_in_old: tuple[str, ...]   # in NEW but not OLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_path": self.old_path,
            "new_path": self.new_path,
            "old_count": self.old_count,
            "new_count": self.new_count,
            "changed_count": len(self.changed_cases),
            "unchanged_count": len(self.unchanged_cases),
            "missing_in_new": list(self.missing_in_new),
            "missing_in_old": list(self.missing_in_old),
            "changed_cases": [
                {
                    "case_id": c.case_id,
                    "changed_keys": list(c.changed_keys),
                    "old": c.old,
                    "new": c.new,
                }
                for c in self.changed_cases
            ],
            "unchanged_cases": list(self.unchanged_cases),
        }


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------


def _canonical(payload: Any) -> str:
    """Return a canonical JSON string for stable equality checks."""

    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _diff_keys(a: Any, b: Any) -> tuple[str, ...]:
    """Return the sorted list of top-level keys whose JSON value differs.

    Both arguments must be dicts (the loader rejects non-object
    records). The comparison is recursive by JSON-equality of
    the sub-payloads, so two dicts with re-ordered keys still
    count as "no difference".
    """

    if not (isinstance(a, dict) and isinstance(b, dict)):
        if _canonical(a) == _canonical(b):
            return ()
        return ("__value__",)
    keys = sorted(set(a) | set(b))
    diff: list[str] = []
    for k in keys:
        if _canonical(a.get(k)) != _canonical(b.get(k)):
            diff.append(k)
    return tuple(diff)


def parse_jsonl(path: str) -> dict[str, Any]:
    """Parse a JSONL file into a ``{case_id: payload}`` mapping.

    Raises :class:`IncompatibleResultSets` if:

      * The file is missing or unreadable.
      * Any line is malformed JSON.
      * Any record is missing a string ``case_id``.
      * Two records share the same ``case_id``.

    Blank lines (whitespace only) are skipped.
    """

    if not os.path.isfile(path):
        raise IncompatibleResultSets(
            f"JSONL file not found: {path!r}"
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except OSError as exc:
        raise IncompatibleResultSets(
            f"JSONL file unreadable: {path!r} (io error)"
        ) from exc

    out: dict[str, Any] = {}
    for lineno, line in enumerate(raw_lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise IncompatibleResultSets(
                f"JSONL parse error in {path!r} at line {lineno}: "
                f"{exc.msg} (line {exc.lineno} col {exc.colno})"
            ) from exc
        if not isinstance(obj, dict):
            raise IncompatibleResultSets(
                f"JSONL record must be a JSON object "
                f"({path!r} line {lineno}, got {type(obj).__name__})"
            )
        cid = obj.get("case_id")
        if not isinstance(cid, str) or not cid:
            raise IncompatibleResultSets(
                f"JSONL record missing string 'case_id' "
                f"({path!r} line {lineno})"
            )
        if cid in out:
            raise IncompatibleResultSets(
                f"JSONL duplicate case_id {cid!r} "
                f"({path!r} line {lineno})"
            )
        out[cid] = obj
    return out


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def compare_dicts(
    old_records: dict[str, Any],
    new_records: dict[str, Any],
    *,
    old_path: str = "<old>",
    new_path: str = "<new>",
) -> DeltaReport:
    """Diff two in-memory ``{case_id: payload}`` mappings.

    The function is order-independent: it operates on the
    ``case_id`` set, never on insertion order. Both inputs
    must be ``dict[str, Any]``; otherwise :class:`TypeError`
    propagates from the dict constructor.
    """

    old_ids = set(old_records)
    new_ids = set(new_records)
    missing_in_new = sorted(old_ids - new_ids)   # in OLD only
    missing_in_old = sorted(new_ids - old_ids)   # in NEW only
    common = sorted(old_ids & new_ids)

    changed: list[CaseDelta] = []
    unchanged: list[str] = []
    for cid in common:
        old = old_records[cid]
        new = new_records[cid]
        ck = _diff_keys(old, new)
        if ck:
            changed.append(CaseDelta(
                case_id=cid,
                old=old,
                new=new,
                changed=True,
                changed_keys=ck,
            ))
        else:
            unchanged.append(cid)

    return DeltaReport(
        old_path=old_path,
        new_path=new_path,
        old_count=len(old_records),
        new_count=len(new_records),
        changed_cases=tuple(changed),
        unchanged_cases=tuple(unchanged),
        missing_in_new=tuple(missing_in_new),
        missing_in_old=tuple(missing_in_old),
    )


def compare_files(old_path: str, new_path: str) -> DeltaReport:
    """Diff two JSONL files on disk."""

    old_records = parse_jsonl(old_path)
    new_records = parse_jsonl(new_path)
    return compare_dicts(
        old_records, new_records,
        old_path=os.path.abspath(old_path),
        new_path=os.path.abspath(new_path),
    )


__all__ = [
    "CaseDelta",
    "DeltaReport",
    "IncompatibleResultSets",
    "compare_dicts",
    "compare_files",
    "parse_jsonl",
]