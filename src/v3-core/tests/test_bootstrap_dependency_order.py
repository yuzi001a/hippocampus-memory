"""FRESH_DB_BOOTSTRAP_DEPENDENCY_ORDER — ordering guard on the REAL expansion.

These tests do not grep a marker line number. They expand the packaged SQL
through the same helper bootstrap uses (`_expand_alpha_include`) and then
verify, statement by statement, that every foreign key target is created
before the table that references it.

Regression pin for the fresh-install blocker:
``UndefinedTable: relation "public.observation_notes" does not exist``.
"""
from __future__ import annotations

import re

import pytest

from v3core.distribution_cli import _expand_alpha_include, _package_sql

COMMENT_RE = re.compile(r"--[^\n]*")
CREATE_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?public\.(\w+)",
                             re.IGNORECASE)
REFERENCES_RE = re.compile(r"REFERENCES\s+public\.(\w+)", re.IGNORECASE)


def _strip_comments(sql: str) -> str:
    return COMMENT_RE.sub("", sql)


def _create_order(sql: str) -> list[str]:
    return [m.group(1) for m in CREATE_TABLE_RE.finditer(_strip_comments(sql))]


def _fk_edges(sql: str) -> list[tuple[str, str]]:
    """(referencing_table, referenced_table) pairs, in statement order."""
    clean = _strip_comments(sql)
    creates = list(CREATE_TABLE_RE.finditer(clean))
    edges: list[tuple[str, str]] = []
    for i, m in enumerate(creates):
        end = creates[i + 1].start() if i + 1 < len(creates) else len(clean)
        body = clean[m.end():end]
        for ref in REFERENCES_RE.finditer(body):
            edges.append((m.group(1), ref.group(1)))
    return edges


@pytest.fixture(scope="module")
def alpha_expanded() -> str:
    return _expand_alpha_include(_package_sql("alpha_bootstrap.sql"))


@pytest.fixture(scope="module")
def upgrade_expanded() -> str:
    return _expand_alpha_include(_package_sql("upgrade_v0_2.sql"))


# ── the general contract ───────────────────────────────────────────────────

def test_expansion_actually_splices_the_sidecars(alpha_expanded: str):
    """A 'fix' that dropped an include would leave the install without a table."""
    order = _create_order(alpha_expanded)
    assert "qa_embedding_chunks" in order
    assert "observation_embedding_chunks" in order


def test_every_fk_target_is_created_before_its_referencing_table(alpha_expanded: str):
    order = _create_order(alpha_expanded)
    pos = {name: i for i, name in enumerate(order)}
    violations = []
    for child, parent in _fk_edges(alpha_expanded):
        if child == parent:
            continue          # self-referential (observation_notes.prev_id)
        if parent not in pos:
            violations.append(f"{child} -> {parent} (target never created)")
        elif pos[parent] > pos[child]:
            violations.append(f"{child} -> {parent} (target created later)")
    assert not violations, f"dependency-order violations: {violations}"


# ── named regression pins for this defect ──────────────────────────────────

def test_observation_sidecar_spliced_after_observation_notes(alpha_expanded: str):
    order = _create_order(alpha_expanded)
    assert order.index("observation_notes") < order.index("observation_embedding_chunks"), (
        "long-observation sidecar has a FK to observation_notes and must be "
        "spliced AFTER the table is created"
    )


def test_qa_sidecar_spliced_after_qa_pairs(alpha_expanded: str):
    order = _create_order(alpha_expanded)
    assert order.index("qa_pairs") < order.index("qa_embedding_chunks")


def test_long_observation_fk_target_is_observation_notes(alpha_expanded: str):
    edges = _fk_edges(alpha_expanded)
    assert ("observation_embedding_chunks", "observation_notes") in edges


# ── doctor contract: a fresh bootstrap must produce these ──────────────────

DOCTOR_REQUIRED = (
    "explicit_memories", "qa_pairs", "topics", "topic_entries",
    "observation_notes", "conversation_stream", "yin_paragraphs",
)


def test_expansion_creates_every_doctor_required_table(alpha_expanded: str):
    order = set(_create_order(alpha_expanded))
    missing = [t for t in DOCTOR_REQUIRED if t not in order]
    assert not missing, f"fresh bootstrap would not create: {missing}"


# ── the upgrade artifact must not regress either ───────────────────────────

def test_upgrade_splices_observation_sidecar_after_its_alters(upgrade_expanded: str):
    clean = _strip_comments(upgrade_expanded)
    alters = [m.start() for m in re.finditer(r"ALTER\s+TABLE\s+public\.observation_notes",
                                             clean, re.IGNORECASE)]
    sidecar = clean.index("observation_embedding_chunks")
    assert alters, "upgrade artifact should still ALTER observation_notes"
    assert sidecar > max(alters), (
        "upgrade path must create the sidecar only after observation_notes exists"
    )
