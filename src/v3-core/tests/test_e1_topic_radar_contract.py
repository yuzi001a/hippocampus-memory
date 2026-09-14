"""Contract test: prove the E1 import target ``v3core.topic_radar`` exists.

This pins the historical bug where e1.py did ``from .topic_radar import
radar_scan`` but the module was omitted during the public migration,
yielding ``ModuleNotFoundError: No module named 'v3core.topic_radar'`` at
runtime. The test:

  * verifies the module resolves via importlib.util.find_spec;
  * imports radar_scan and asserts it returns the documented empty
    report for pg=None;
  * parses src/v3core/e1.py as text, locates the
    ``from .topic_radar import radar_scan`` line and asserts that the
    referenced module resolves to an existing file on disk.

No PG, no LLM, no network — purely structural.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest


REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))


def test_find_spec_resolves_topic_radar():
    spec = importlib.util.find_spec("v3core.topic_radar")
    assert spec is not None, "v3core.topic_radar must be importable"
    assert spec.origin is not None, "v3core.topic_radar must have a real file origin"


def test_topic_radar_file_sits_next_to_e1():
    spec = importlib.util.find_spec("v3core.topic_radar")
    radar_path = Path(spec.origin).resolve()
    e1_spec = importlib.util.find_spec("v3core.e1")
    e1_path = Path(e1_spec.origin).resolve()
    assert radar_path.parent == e1_path.parent, (
        f"topic_radar.py must live next to e1.py inside the package; "
        f"got radar={radar_path}, e1={e1_path}"
    )
    assert radar_path.exists()


def test_radar_scan_is_callable_and_empty_report_for_pg_none():
    from v3core.topic_radar import radar_scan

    assert callable(radar_scan)
    rep = radar_scan(pg=None)
    assert rep == {
        "duplicates": [],
        "related": [],
        "scanned": 0,
        "dup_total": 0,
        "rel_total": 0,
    }


def test_e1_topic_radar_import_target_is_resolvable():
    """The historical failure mode must be impossible while this file ships."""
    e1_path = REPO_SRC / "v3core" / "e1.py"
    text = e1_path.read_text(encoding="utf-8")
    match = re.search(r"from\s+\.topic_radar\s+import\s+radar_scan", text)
    assert match is not None, (
        "src/v3core/e1.py is expected to contain "
        "'from .topic_radar import radar_scan' — the migration target"
    )
    # Resolve the module the same way the e1.py import statement does.
    spec = importlib.util.find_spec("v3core.topic_radar")
    assert spec is not None and spec.origin is not None, (
        "e1.py imports 'from .topic_radar import radar_scan' but the "
        "module is missing — this is the historical regression we are "
        "fixing in this PR"
    )
    assert Path(spec.origin).is_file(), spec.origin
