"""G5b GOLD REGRESSION subset — Track-E deliverable.

A small, stable, hand-picked subset of the v2 corpus that every
G5b evaluator regression run must execute. The gold subset is the
fast feedback loop: when a code change regresses an assertion that
the gold subset codifies, CI must catch it before the full 86-scenario
corpus runs.

Contract
--------

* ``GOLD_IDS`` is the canonical list of 25 scenario IDs that belong
  to the gold regression subset. The IDs are an exact subset of the
  v2 corpus and span all 12 required categories.

* ``all_gold_scenarios()`` returns the parsed ``Scenario`` objects
  corresponding to those IDs, in the order they appear in ``GOLD_IDS``.
  Missing IDs raise ``ValueError`` (a hard contract violation).

* ``gold_count()`` returns the integer count (25).

* ``gold_by_category()`` returns a per-category histogram of the gold
  IDs; tests assert that every required category is represented.

Selection criteria
------------------

The 25 IDs were chosen to cover the 12 categories (≥1 per category),
mix v1 stable cases with v2 expansion cases (so we don't depend on
unproven shapes), exercise the full failure-taxonomy surface, and
keep the suite fast to run (<1s on the lab fake pool).

Per-category gold composition:

  USER_PREFERENCE     x2  (UP-001, UP-101)
  PROJECT_DECISION    x2  (PD-001, PD-101)
  SESSION_CONTINUITY  x2  (SC-001, SC-101)
  STABLE_FACT         x2  (SF-001, SF-101)
  CONFLICT_UPDATE     x2  (CU-001, CU-101)
  DISTRACTOR          x2  (DT-001, DT-101)
  NEGATIVE_RECALL     x2  (NR-001, NR-101)
  ARCHIVE             x3  (AR-001, AR-002, AR-101)
  LONG_DISTANCE       x2  (LD-001, LD-102)
  MULTI_RELEVANT      x2  (MR-001, MR-101)
  AMBIGUOUS           x2  (AMB-001, AMB-101)
  TEMPORAL_UPDATE     x2  (TU-001, TU-101)

Total: 2+2+2+2+2+2+2+3+2+2+2+2 = 25 scenarios.

Failure-taxonomy surface exercised by the gold subset:

  PASS / NOT_RETRIEVED / RANKED_TOO_LOW / ARCHIVED_MEMORY_RETURNED
  CONFLICT_RESOLUTION / PIPELINE_ERROR (via forced schema load)
  STALE_MEMORY / WRONG_MEMORY / NOT_STORED / NOT_INDEXED / TIMEOUT /
  UNSUPPORTED / UNKNOWN (asserted by absence-of-error in metrics)

Stability rules
---------------

* The IDs are frozen. Adding/removing an ID requires bumping the
  module version (GOLD_VERSION) AND updating this docstring +
  the regression tests.
* The gold subset MUST stay a subset of the v2 corpus; missing IDs
  are an immediate regression.
"""
from __future__ import annotations

from .scenario_schema import scenario_from_dict
from .scenarios.scenarios_v2 import all_scenarios


GOLD_VERSION = "g5b-gold-v1"


# Frozen list — do not edit without bumping GOLD_VERSION.
GOLD_IDS: tuple[str, ...] = (
    # USER_PREFERENCE x2
    "G5b-UP-001",
    "G5b-UP-101",
    # PROJECT_DECISION x2
    "G5b-PD-001",
    "G5b-PD-101",
    # SESSION_CONTINUITY x2
    "G5b-SC-001",
    "G5b-SC-101",
    # STABLE_FACT x2
    "G5b-SF-001",
    "G5b-SF-101",
    # CONFLICT_UPDATE x2
    "G5b-CU-001",
    "G5b-CU-101",
    # DISTRACTOR x2
    "G5b-DT-001",
    "G5b-DT-101",
    # NEGATIVE_RECALL x2
    "G5b-NR-001",
    "G5b-NR-101",
    # ARCHIVE x3 (extra coverage on the archive failure path)
    "G5b-AR-001",
    "G5b-AR-002",
    "G5b-AR-101",
    # LONG_DISTANCE x2
    "G5b-LD-001",
    "G5b-LD-102",
    # MULTI_RELEVANT x2
    "G5b-MR-001",
    "G5b-MR-101",
    # AMBIGUOUS x2
    "G5b-AMB-001",
    "G5b-AMB-101",
    # TEMPORAL_UPDATE x2
    "G5b-TU-001",
    "G5b-TU-101",
)


def gold_count() -> int:
    """Return the number of scenarios in the gold subset (25)."""
    return len(GOLD_IDS)


def gold_by_category() -> dict[str, int]:
    """Return a histogram of gold IDs grouped by their scenario category.

    Built by walking the v2 corpus and looking each ID up. The keys
    are the 12 allowed category names; values are the per-category
    counts. A category with no gold entry is reported as 0.
    """
    from .scenario_schema import ALLOWED_CATEGORIES

    counts: dict[str, int] = {c: 0 for c in ALLOWED_CATEGORIES}
    corpus_by_id = {s["scenario_id"]: s for s in all_scenarios()}
    for sid in GOLD_IDS:
        scn = corpus_by_id.get(sid)
        if scn is None:
            continue
        cat = scn["category"]
        counts[cat] = counts.get(cat, 0) + 1
    return counts


def all_gold_scenarios() -> list:
    """Return the parsed ``Scenario`` objects for the gold subset.

    The order matches ``GOLD_IDS``. Raises ``ValueError`` if any
    gold ID is missing from the v2 corpus — a hard regression.
    """
    corpus_by_id = {s["scenario_id"]: s for s in all_scenarios()}
    missing = [sid for sid in GOLD_IDS if sid not in corpus_by_id]
    if missing:
        raise ValueError(
            f"{GOLD_VERSION}: {len(missing)} gold ID(s) missing from "
            f"v2 corpus: {missing[:5]}{'...' if len(missing) > 5 else ''}"
        )
    return [
        scenario_from_dict(corpus_by_id[sid]) for sid in GOLD_IDS
    ]


__all__ = [
    "GOLD_VERSION",
    "GOLD_IDS",
    "gold_count",
    "gold_by_category",
    "all_gold_scenarios",
]
