"""Tests for the JSONL differential comparator.

Covers case-ID keyed comparison, missing/extra/unchanged
case handling, fail-closed behaviour on malformed lines, and
order-independence.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval.locomo_recall_v2 import compare as cmp  # noqa: E402


def _write_jsonl(path: str, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _case(cid: str, **fields) -> dict:
    base = {"case_id": cid}
    base.update(fields)
    return base


class TestCompareFiles:
    def test_identical_files_have_no_changes(self, tmp_path):
        a = tmp_path / "old.jsonl"
        b = tmp_path / "new.jsonl"
        rows = [
            _case("c1", score=0.9, hit=True),
            _case("c2", score=0.1, hit=False),
        ]
        _write_jsonl(str(a), rows)
        _write_jsonl(str(b), rows)
        rep = cmp.compare_files(str(a), str(b))
        assert rep.old_count == 2
        assert rep.new_count == 2
        assert rep.changed_cases == ()
        assert rep.unchanged_cases == ("c1", "c2")
        assert rep.missing_in_new == ()
        assert rep.missing_in_old == ()

    def test_changed_cases_are_reported(self, tmp_path):
        a = tmp_path / "old.jsonl"
        b = tmp_path / "new.jsonl"
        _write_jsonl(str(a), [
            _case("c1", score=0.9, hit=True),
            _case("c2", score=0.1, hit=False),
        ])
        _write_jsonl(str(b), [
            _case("c1", score=0.7, hit=True),     # changed
            _case("c2", score=0.1, hit=False),   # unchanged
        ])
        rep = cmp.compare_files(str(a), str(b))
        assert len(rep.changed_cases) == 1
        cd = rep.changed_cases[0]
        assert cd.case_id == "c1"
        assert "score" in cd.changed_keys

    def test_missing_in_new_and_missing_in_old(self, tmp_path):
        a = tmp_path / "old.jsonl"
        b = tmp_path / "new.jsonl"
        _write_jsonl(str(a), [
            _case("c1", x=1),
            _case("c2", x=2),
            _case("c3", x=3),
        ])
        _write_jsonl(str(b), [
            _case("c1", x=1),
            _case("c3", x=3),
            _case("c4", x=4),
        ])
        rep = cmp.compare_files(str(a), str(b))
        assert rep.missing_in_new == ("c2",)   # in OLD only
        assert rep.missing_in_old == ("c4",)   # in NEW only

    def test_order_independence(self, tmp_path):
        a = tmp_path / "old.jsonl"
        b = tmp_path / "new.jsonl"
        _write_jsonl(str(a), [
            _case("a", x=1),
            _case("b", x=2),
        ])
        _write_jsonl(str(b), [
            _case("b", x=2),
            _case("a", x=1),
        ])
        rep = cmp.compare_files(str(a), str(b))
        assert rep.changed_cases == ()
        assert rep.unchanged_cases == ("a", "b")

    def test_key_order_in_payload_does_not_count_as_change(self, tmp_path):
        # Two records with the same case_id but different key
        # ordering inside the dict must compare equal — JSON
        # has no canonical key order.
        a = tmp_path / "old.jsonl"
        b = tmp_path / "new.jsonl"
        with open(a, "w", encoding="utf-8") as f:
            f.write('{"case_id":"c1","score":0.9,"hit":true}\n')
        with open(b, "w", encoding="utf-8") as f:
            f.write('{"hit":true,"score":0.9,"case_id":"c1"}\n')
        rep = cmp.compare_files(str(a), str(b))
        assert rep.changed_cases == ()

    def test_blank_lines_ignored(self, tmp_path):
        a = tmp_path / "old.jsonl"
        b = tmp_path / "new.jsonl"
        with open(a, "w", encoding="utf-8") as f:
            f.write('{"case_id":"c1","x":1}\n\n   \n{"case_id":"c2","x":2}\n')
        with open(b, "w", encoding="utf-8") as f:
            f.write('{"case_id":"c1","x":1}\n{"case_id":"c2","x":2}\n')
        rep = cmp.compare_files(str(a), str(b))
        assert rep.old_count == 2
        assert rep.new_count == 2

    def test_malformed_line_refused(self, tmp_path):
        a = tmp_path / "old.jsonl"
        b = tmp_path / "new.jsonl"
        with open(a, "w", encoding="utf-8") as f:
            f.write('not json\n')
        _write_jsonl(str(b), [_case("c1", x=1)])
        with pytest.raises(cmp.IncompatibleResultSets):
            cmp.compare_files(str(a), str(b))

    def test_missing_case_id_refused(self, tmp_path):
        a = tmp_path / "old.jsonl"
        b = tmp_path / "new.jsonl"
        with open(a, "w", encoding="utf-8") as f:
            f.write('{"x":1}\n')
        _write_jsonl(str(b), [_case("c1", x=1)])
        with pytest.raises(cmp.IncompatibleResultSets):
            cmp.compare_files(str(a), str(b))

    def test_duplicate_case_id_in_one_file_refused(self, tmp_path):
        a = tmp_path / "old.jsonl"
        b = tmp_path / "new.jsonl"
        _write_jsonl(str(a), [
            _case("c1", x=1),
            _case("c1", x=2),
        ])
        _write_jsonl(str(b), [_case("c1", x=1)])
        with pytest.raises(cmp.IncompatibleResultSets):
            cmp.compare_files(str(a), str(b))

    def test_missing_file_refused(self, tmp_path):
        a = tmp_path / "old.jsonl"
        b = tmp_path / "new.jsonl"
        _write_jsonl(str(a), [_case("c1", x=1)])
        with pytest.raises(cmp.IncompatibleResultSets):
            cmp.compare_files(str(a), str(b / "nope.jsonl"))

    def test_to_dict_round_trip(self, tmp_path):
        a = tmp_path / "old.jsonl"
        b = tmp_path / "new.jsonl"
        _write_jsonl(str(a), [
            _case("c1", x=1),
            _case("c2", x=2),
        ])
        _write_jsonl(str(b), [
            _case("c1", x=1),
            _case("c2", x=3),
        ])
        rep = cmp.compare_files(str(a), str(b))
        d = rep.to_dict()
        # JSON-serializable.
        blob = json.dumps(d)
        reloaded = json.loads(blob)
        assert reloaded["old_count"] == 2
        assert reloaded["new_count"] == 2
        assert reloaded["changed_count"] == 1
        assert reloaded["missing_in_new"] == []
        assert reloaded["missing_in_old"] == []

    def test_compare_dicts_in_memory(self):
        rep = cmp.compare_dicts(
            {"c1": _case("c1", x=1)},
            {"c1": _case("c1", x=2)},
        )
        assert len(rep.changed_cases) == 1
        assert rep.changed_cases[0].changed_keys == ("x",)

    def test_nested_value_change_detected(self):
        rep = cmp.compare_dicts(
            {"c1": _case("c1", payload={"a": 1, "b": [1, 2]})},
            {"c1": _case("c1", payload={"a": 1, "b": [1, 3]})},
        )
        assert rep.changed_cases[0].changed_keys == ("payload",)

    def test_no_row_order_dependency(self):
        # Different insertion order; same case set; same content.
        rep = cmp.compare_dicts(
            {"c2": _case("c2", x=2), "c1": _case("c1", x=1)},
            {"c1": _case("c1", x=1), "c2": _case("c2", x=2)},
        )
        assert rep.changed_cases == ()
        assert rep.unchanged_cases == ("c1", "c2")