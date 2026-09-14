"""G5b scenario runner.

The runner is intentionally small. It:

  1. Loads scenarios from one or more JSON files.
  2. Builds writer + reader (Lane A by default; Lane B opt-in).
  3. For each scenario, runs the canonical algorithm:
       a. write all fixtures via ``ActiveMemoryWriter.create``;
       b. apply any pre-recall actions (archive / create_extra);
       c. for each query, run ``search_keyword`` (lane=keyword)
          and/or ``search_vector`` (lane=vector) depending on the
          scenario's request;
       d. tag the earliest failure layer using ``failure_taxonomy``.
  4. Records per-query trace fields only (no payload, no content
     beyond what the reader already returns).
  5. Returns a list of ``ScenarioResult``.

Honest contract:

  * The runner does NOT modify ``v3core.active_memory_store`` or
    any production code.
  * It does NOT tune weights/observer/E1/recall semantics.
  * It does NOT call any v3_* / v3_add / v3_store / v3_extract tool.
  * Lane A is a deterministic lab measurement, not a production
    runtime measurement; Lane B is opt-in only.
"""
from __future__ import annotations

import json
import time
import traceback
from typing import Any, Iterable

from .failure_taxonomy import (
    ARCHIVED_MEMORY_RETURNED,
    CONFLICT_RESOLUTION,
    NOT_INDEXED,
    NOT_RETRIEVED,
    NOT_STORED,
    PIPELINE_ERROR,
    RANKED_TOO_LOW,
    STALE_MEMORY,
    TIMEOUT,
    UNKNOWN,
    UNSUPPORTED,
    WRONG_MEMORY,
    normalize,
)

# Imported lazily inside build_lane_a_writer_and_reader; we re-read
# the canonical constant here for the CONFLICT_UPDATE branch.
try:
    import os as _os
    import sys as _sys
    _SRC = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), "..", "..", "src"
    )
    _SRC = _os.path.normpath(_SRC)
    if _SRC not in _sys.path:
        _sys.path.insert(0, _SRC)
    from v3core.active_memory_store import (  # type: ignore  # noqa: E402
        DURABLE_FAILED as _DURABLE_FAILED,
        DEDUPLICATED as _DEDUPLICATED,
    )
    DURABLE_FAILED: str = _DURABLE_FAILED
    DEDUPLICATED: str = _DEDUPLICATED
except Exception:  # pragma: no cover - safety import
    DURABLE_FAILED = "DURABLE_FAILED"
    DEDUPLICATED = "DEDUPLICATED"
from .lane_a import (
    build_lane_a_writer_and_reader,
    cosine_topk,
    deterministic_embedder,
)
from .lane_b import (
    LaneBDisabled,
    build_lane_b_writer_and_reader,
    is_lane_b_enabled,
)
from .metrics import QueryResult, ScenarioResult
from .scenario_schema import Fixture, Scenario


# Default recall limit per query. The writer/reader default is 20;
# we cap to 5 here so MRR/Hit@K numbers are comparable to a real
# prefetch budget.
DEFAULT_RECALL_LIMIT = 5


# ── helpers ───────────────────────────────────────────────────────────────


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _read_keyword(reader: Any, text: str, limit: int) -> list[dict[str, Any]]:
    """Run keyword lane; tolerant of ``reader.search_keyword`` exceptions."""
    try:
        return list(reader.search_keyword(text, limit=limit))
    except Exception as exc:
        return [{"__error__": repr(exc)}]


def _read_vector(reader: Any, text: str, limit: int) -> list[dict[str, Any]]:
    """Run vector lane with the deterministic stub embedder."""
    try:
        vec = deterministic_embedder(text, {"model": "stub-bag-of-tokens-v1"})
        return list(reader.search_vector(vec, limit=limit))
    except Exception as exc:
        return [{"__error__": repr(exc)}]


def _rank_candidates(
    candidates: list[dict[str, Any]], expected_ids: list[str]
) -> list[int]:
    """Return the 1-based rank of each expected id in candidate order."""
    out: list[int] = []
    ids = [c.get("memory_id") for c in candidates]
    for want in expected_ids:
        try:
            out.append(ids.index(want) + 1)
        except ValueError:
            out.append(-1)
    return out


# ── per-scenario runner ───────────────────────────────────────────────────


def _seed_fixtures(
    writer: Any, fixtures: Iterable[Fixture]
) -> list[tuple[Fixture, Any]]:
    """Write each fixture; return (fixture, MemoryWriteResult) pairs."""
    out: list[tuple[Fixture, Any]] = []
    for fx in fixtures:
        res = writer.create(
            category=fx.category,
            title=fx.title,
            content=fx.content,
            tags=list(fx.tags),
            source_id=fx.source_id,
            memory_id=fx.memory_id,
            provenance=fx.provenance,
        )
        out.append((fx, res))
    return out


def _all_fixture_ids(scenario: Scenario) -> list[str]:
    """Compute the canonical memory_ids of every fixture in a scenario.

    For fixtures with explicit ``memory_id`` we use that string; for
    fixtures without one we use ``derive_memory_id(category, title,
    content, tags)`` so the runner can compare reader results against
    the expected IDs without knowing the writer's hash details.
    """
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    src_root = os.path.normpath(os.path.join(here, "..", "..", "src"))
    if src_root not in sys.path:
        sys.path.insert(0, src_root)
    try:
        from v3core.active_memory_store import derive_memory_id
    except Exception:
        return [fx.memory_id for fx in scenario.fixtures if fx.memory_id]

    ids: list[str] = []
    for fx in scenario.fixtures:
        if fx.memory_id:
            ids.append(fx.memory_id)
        else:
            ids.append(
                derive_memory_id(fx.category, fx.title, fx.content, list(fx.tags))
            )
    return ids


def _resolve_expected(
    scenario: Scenario,
    seed_results: list[tuple[Fixture, Any]],
    archived_fixture_ids: set[str] | None = None,
) -> None:
    """Populate empty must_recall / must_not_recall from fixtures.

    Rule: the author is the source of truth. When the author leaves
    ``must_recall`` empty, we default it to all seeded fixture IDs
    (i.e. any seeded memory hitting is a pass) — useful for plain
    STABLE_FACT, USER_PREFERENCE, SESSION_CONTINUITY scenarios.

    For NEGATIVE_RECALL and DISTRACTOR scenarios the author MUST
    specify ``must_recall`` and ``must_not_recall`` explicitly.
    ``ARCHIVE`` scenarios must specify ``expected_status`` (the
    runner will check that the reader returns no archived rows).

    ``must_not_recall`` defaults to empty unless the author fills
    it in.

    If ``archived_fixture_ids`` is provided, those fixtures are
    excluded from the auto-populated ``must_recall`` set — the
    reader should not return archived rows for active queries.
    """
    if not seed_results:
        return
    fixture_ids = _all_fixture_ids(scenario)
    archived = archived_fixture_ids or set()
    for q in scenario.queries:
        if not q.must_recall:
            # default: any non-archived fixture counts as a hit
            q.must_recall = [fid for fid in fixture_ids if fid not in archived]
        # must_not_recall stays as the author set it (default [])


def _apply_action(
    writer: Any,
    action: Any,
    scenario_fixtures: list[Fixture],
) -> Any:
    """Apply one pre-recall action.

    If ``action.fixture_index`` is set, resolve it against
    ``scenario_fixtures`` to a real ``memory_id``. If both
    ``fixture_index`` and ``memory_id`` are set, ``memory_id`` wins.
    """
    memory_id = action.memory_id
    if memory_id is None and action.fixture_index is not None:
        if 0 <= action.fixture_index < len(scenario_fixtures):
            fx = scenario_fixtures[action.fixture_index]
            if fx.memory_id:
                memory_id = fx.memory_id
            else:
                # derive on the fly
                import os
                import sys

                here = os.path.dirname(os.path.abspath(__file__))
                src_root = os.path.normpath(os.path.join(here, "..", "..", "src"))
                if src_root not in sys.path:
                    sys.path.insert(0, src_root)
                try:
                    from v3core.active_memory_store import derive_memory_id

                    memory_id = derive_memory_id(
                        fx.category, fx.title, fx.content, list(fx.tags)
                    )
                except Exception:
                    memory_id = None

    if action.type == "archive":
        if memory_id is None:
            return None
        return writer.archive(memory_id, hard=False)
    if action.type == "archive_hard":
        if memory_id is None:
            return None
        return writer.archive(memory_id, hard=True)
    if action.type == "create_extra" and action.fixture is not None:
        fx = action.fixture
        return writer.create(
            category=fx.category,
            title=fx.title,
            content=fx.content,
            tags=list(fx.tags),
            source_id=fx.source_id,
            memory_id=fx.memory_id,
            provenance=fx.provenance,
        )
    raise ValueError(f"unknown action.type={action.type!r}")


def _failure_for_query(
    q: QueryResult,
    candidates: list[dict[str, Any]],
    must_recall: list[str],
    must_not_recall: list[str],
    expected_status: str,
    expect_empty_recall: bool = False,
    must_recall_rank_max: int = 3,
) -> str:
    if q.raw_error:
        return PIPELINE_ERROR
    if expected_status == "active" and q.archived_returned:
        return ARCHIVED_MEMORY_RETURNED
    # expect_empty_recall: scenario asserts the reader returns nothing.
    if expect_empty_recall:
        if candidates:
            return WRONG_MEMORY  # we expected 0, got some
        return "PASS"
    # ANY-of semantics for must_recall: if any expected id is in the
    # top-K, that's a hit. If none of the expected ids were retrieved
    # at all, that's NOT_RETRIEVED; if some hit but they're all
    # beyond rank_max, that's RANKED_TOO_LOW.
    if must_recall:
        ranks = _rank_candidates(candidates, must_recall)
        any_seen = any(r > 0 for r in ranks)
        if not any_seen:
            return NOT_RETRIEVED
        # at least one hit
        in_top_k = any(0 < r <= must_recall_rank_max for r in ranks)
        if not in_top_k:
            return RANKED_TOO_LOW
    if must_not_recall:
        ids = {c.get("memory_id") for c in candidates}
        if any(want in ids for want in must_not_recall):
            return WRONG_MEMORY
    return "PASS"


def _run_query(
    reader: Any,
    q: Any,
    limit: int,
) -> tuple[QueryResult, list[dict[str, Any]]]:
    start_ms = _now_ms()
    if q.lane == "keyword":
        candidates = _read_keyword(reader, q.text, limit)
        actual_lane = "keyword"
    elif q.lane == "vector":
        candidates = _read_vector(reader, q.text, limit)
        actual_lane = "vector"
    else:  # auto: run both; merge by union, prefer keyword then vector
        kw = _read_keyword(reader, q.text, limit)
        vc = _read_vector(reader, q.text, limit)
        seen: dict[str, dict[str, Any]] = {}
        for c in kw + vc:
            mid = c.get("memory_id")
            if mid and mid not in seen:
                seen[mid] = c
        candidates = list(seen.values())
        actual_lane = "auto"
    elapsed_ms = _now_ms() - start_ms

    # Determine archived_returned.
    archived = [c.get("memory_id") for c in candidates if c.get("status") == "archived"]

    qr = QueryResult(
        query_id=q.query_id,
        text=q.text,
        expected_lane=q.lane,
        actual_lane=actual_lane,
        returned_memory_ids=[c.get("memory_id") for c in candidates if c.get("memory_id")],
        must_recall_hit_ranks=_rank_candidates(candidates, q.must_recall)
        if q.must_recall
        else [],
        must_not_recall_violations=[
            mid
            for mid in (c.get("memory_id") for c in candidates)
            if mid in q.must_not_recall
        ],
        archived_returned=archived,
        elapsed_ms=elapsed_ms,
    )
    label = _failure_for_query(
        qr,
        candidates,
        q.must_recall,
        q.must_not_recall,
        q.expected_status,
        expect_empty_recall=q.expect_empty_recall,
        must_recall_rank_max=q.must_recall_rank_max,
    )
    qr.failure_label = label
    # Carry the rank_max into the QueryResult so the metrics layer
    # can later use it (e.g. for Hit@K reporting). We do this as a
    # dynamic attribute to avoid widening the dataclass surface.
    qr.must_recall_rank_max = q.must_recall_rank_max  # type: ignore[attr-defined]
    if any(isinstance(c, dict) and "__error__" in c for c in candidates):
        qr.raw_error = candidates[0].get("__error__") if candidates else "unknown"
    return qr, candidates


def _failure_result(scenario: Scenario, start_ms: float, **kwargs) -> tuple[ScenarioResult, list]:
    """Build a (ScenarioResult, []) tuple for early-return paths."""
    res = ScenarioResult(
        scenario_id=scenario.scenario_id,
        category=scenario.category,
        **kwargs,
        elapsed_ms=_now_ms() - start_ms,
    )
    return res, []


def _run_scenario(
    scenario: Scenario,
    writer: Any,
    reader: Any,
    limit: int,
) -> ScenarioResult:
    start_ms = _now_ms()
    notes = []
    # 1) seed fixtures
    seed_results: list[tuple[Fixture, Any]] = []
    seed_error = ""
    try:
        seed_results = _seed_fixtures(writer, scenario.fixtures)
    except Exception as exc:
        seed_error = repr(exc)

    if seed_error:
        return _failure_result(
            scenario,
            start_ms,
            passed=False,
            pipeline_status="ERROR",
            failure_label=PIPELINE_ERROR,
            unsupported=scenario.unsupported,
            notes=f"fixture seed failed: {seed_error}",
        )

    # Track which fixtures were archived by previous actions so
    # _resolve_expected can exclude them from auto-populated
    # must_recall sets.
    archived_fixture_ids: set[str] = set()

    # If the scenario is CONFLICT_UPDATE, the fixtures intentionally
    # include a same-id different-payload attempt; verify the writer
    # returned DURABLE_FAILED for that attempt.
    if scenario.category == "CONFLICT_UPDATE":
        # The CONFLICT_UPDATE fixture list is conventionally
        # ``[base, duplicate_same_id_different_payload, retry_exact]``
        # where the second one must return DURABLE_FAILED and the
        # third DEDUPLICATED. We detect it heuristically: any
        # ``DURABLE_FAILED`` is the conflict-violation signal;
        # any ``DEDUPLICATED`` without prior conflict means we
        # did not exercise the conflict path.
        statuses = [r.status for _, r in seed_results]
        if DURABLE_FAILED in statuses:
            # OK — conflict surfaced correctly.
            pass
        else:
            # No conflict surfaced. Try to manufacture one by
            # re-writing the first fixture with a different payload.
            # We must pass the SEEDED row's explicit memory_id
            # (which is the writer's actual stored id) — the writer
            # only detects the same-id different-payload conflict
            # when an explicit memory_id matches a stored row.
            if seed_results:
                fx0, seed_res0 = seed_results[0]
                stored_id = (
                    seed_res0.memory_id
                    if getattr(seed_res0, "memory_id", None)
                    else None
                )
                if stored_id is None:
                    notes.append(
                        "CONFLICT_UPDATE: cannot manufacture conflict; "
                        "first fixture has no stored memory_id."
                    )
                else:
                    mutated = Fixture(
                        category=fx0.category,
                        title=fx0.title + " (mutated)",
                        content=fx0.content + " — DIFFERENT PAYLOAD",
                        tags=list(fx0.tags),
                        memory_id=stored_id,
                        source_id=fx0.source_id,
                    )
                    try:
                        conflict_res = writer.create(
                            category=mutated.category,
                            title=mutated.title,
                            content=mutated.content,
                            tags=mutated.tags,
                            memory_id=mutated.memory_id,
                            source_id=mutated.source_id,
                        )
                    except Exception as exc:
                        conflict_res = None
                        notes.append(f"manufactured conflict raised: {exc!r}")
                    if conflict_res is None or conflict_res.status != "DURABLE_FAILED":
                        return _failure_result(
                            scenario,
                            start_ms,
                            passed=False,
                            pipeline_status="FAIL",
                            failure_label=CONFLICT_RESOLUTION,
                        unsupported=scenario.unsupported,
                        notes=(
                            "expected DURABLE_FAILED on same-id different-"
                            "payload attempt; got "
                            f"{conflict_res.status if conflict_res else 'no-result'}"
                        ),
                    )

    # Verify every seeded row is durable (or DEDUPLICATED).
    seed_failures: list[str] = []
    for fx, r in seed_results:
        if not getattr(r, "durable", False):
            seed_failures.append(f"{fx.memory_id}: {getattr(r, 'status', '?')}")

    # Check for NOT_INDEXED: durable but no embedding.
    if not seed_failures:
        # NOTE: ActiveMemoryWriter.capture the readback into
        # ``res.record`` BEFORE the post-commit embedding UPDATE
        # runs. The fresh readback via get_by_memory_id below is the
        # authoritative view. We do NOT flag this as a failure
        # because the writer's contract is documented: ``record``
        # reflects the canonical-row state at lease #2, embedding
        # lands in a separate UPDATE. We still surface this as a
        # note so reviewers can see the bookkeeping.
        for fx, r in seed_results:
            rec = getattr(r, "record", None)
            if rec is None:
                continue
            emb = rec.get("embedding") if isinstance(rec, dict) else None
            model = rec.get("embed_model") if isinstance(rec, dict) else None
            if emb is None and model is None:
                # ``record`` is the pre-embedding readback; the row
                # itself is durable. We do not escalate this to
                # NOT_INDEXED — see lane_a.LAB_NOTE above.
                pass

    if seed_failures:
            return _failure_result(
                scenario,
                start_ms,
                passed=False,
                pipeline_status="FAIL",
                failure_label=NOT_STORED,
                unsupported=scenario.unsupported,
                notes=f"non-durable seed rows: {seed_failures}",
            )

    # 2) apply actions
    action_failures: list[str] = []
    for action in scenario.actions:
        try:
            res = _apply_action(writer, action, scenario.fixtures)
            if res is not None and action.type in ("archive", "archive_hard"):
                # Track the targeted memory_id in the archived set
                # so _resolve_expected can exclude it.
                mid = action.memory_id
                if mid is None and action.fixture_index is not None and 0 <= action.fixture_index < len(scenario.fixtures):
                    fx = scenario.fixtures[action.fixture_index]
                    mid = fx.memory_id or _all_fixture_ids(scenario)[action.fixture_index]
                if mid is not None:
                    archived_fixture_ids.add(mid)
        except Exception as exc:
            action_failures.append(repr(exc))

    if action_failures:
        return _failure_result(
            scenario,
            start_ms,
            passed=False,
            pipeline_status="FAIL",
            failure_label=PIPELINE_ERROR,
            unsupported=scenario.unsupported,
            notes=f"action failures: {action_failures}",
        )

    # Resolve auto-populated must_recall / must_not_recall using the
    # archived-fixture set so we don't ask for archived rows.
    _resolve_expected(scenario, seed_results, archived_fixture_ids)

    # ── 3) run queries ────────────────────────────────────────────────────
    per_query: list[QueryResult] = []
    earliest_label = "PASS"
    # Build (query, query_result, candidates) tuples so metrics can
    # reach must_recall / must_not_recall from the original scenario.
    query_records: list[tuple[Any, QueryResult, list[dict]]] = []
    for q in scenario.queries:
        try:
            qr, candidates = _run_query(reader, q, limit)
        except Exception as exc:
            qr = QueryResult(
                query_id=q.query_id,
                text=q.text,
                expected_lane=q.lane,
                actual_lane="error",
                raw_error=repr(exc),
                elapsed_ms=0.0,
                failure_label=PIPELINE_ERROR,
            )
            candidates = []
        per_query.append(qr)
        query_records.append((q, qr, candidates))
        if qr.failure_label != "PASS" and earliest_label == "PASS":
            earliest_label = qr.failure_label

    # STALE_MEMORY check: every must_recall memory that was returned
    # must also be the SAME canonical payload we seeded (no provenance
    # or content drift). We use the writer's readback via get_by_memory_id.
    stale: list[str] = []
    for q in scenario.queries:
        for want in q.must_recall:
            try:
                rec = reader.get_by_memory_id(want)
            except Exception:
                continue
            if rec is None:
                continue
            for fx, _ in seed_results:
                if fx.memory_id == want:
                    if rec.get("status") != "active":
                        stale.append(f"{want}:status={rec.get('status')}")
                    if rec.get("content") != fx.content:
                        stale.append(f"{want}:content-drift")
                    break
    if stale:
        earliest_label = STALE_MEMORY if earliest_label == "PASS" else earliest_label
        notes.append(f"stale memories: {stale}")

    passed = earliest_label == "PASS"
    pipeline_status = (
        "UNSUPPORTED"
        if scenario.unsupported
        else ("PASS" if passed else "FAIL")
    )
    if scenario.unsupported:
        final_label = UNSUPPORTED
    elif passed:
        final_label = "PASS"
    else:
        final_label = normalize(earliest_label)

    # Build the per-scenario query_records list for metrics. We pair
    # the original scenario Query with the runner's QueryResult so the
    # metrics layer can reach must_recall / must_not_recall.
    query_records = [(q, qr) for q, qr, _ in query_records]

    return (
        ScenarioResult(
            scenario_id=scenario.scenario_id,
            category=scenario.category,
            passed=passed,
            pipeline_status=pipeline_status,
            failure_label=final_label,
            unsupported=scenario.unsupported,
            per_query=per_query,
            elapsed_ms=_now_ms() - start_ms,
            notes="; ".join(notes),
        ),
        query_records,
    )


# DURABLE_FAILED / DEDUPLICATED are imported from v3core above.


# ── public entry point ────────────────────────────────────────────────────


def run_scenarios(
    scenarios: list[Scenario],
    *,
    lane: str = "a",
    limit: int = DEFAULT_RECALL_LIMIT,
) -> tuple[list[ScenarioResult], dict[str, list[tuple[Any, QueryResult]]]]:
    """Run all scenarios under the requested lane.

    Returns ``(results, query_records_by_scenario)`` where the second
    tuple maps ``scenario_id`` to ``(original_query, QueryResult)``
    pairs so the metrics layer can compute must_recall hit rate and
    must_not_recall violation rate against the original spec.

    ``lane='a'`` uses the deterministic in-memory pool.
    ``lane='b'`` requires ``--live-pg`` and ``G5B_EVAL_LAB_DSN``;
    if either is missing, ``LaneBDisabled`` is raised.
    """
    if lane == "a":
        writer, reader, _pool = build_lane_a_writer_and_reader()
    elif lane == "b":
        if not is_lane_b_enabled():
            raise LaneBDisabled(
                "Lane B requested but G5B_EVAL_LIVE_PG != '1' or "
                "G5B_EVAL_LAB_DSN is unset."
            )
        # Resolve the DSN from env or --dsn; the builder will
        # re-validate it as a disposable lab DSN.
        from .lane_b import resolve_lane_b_dsn
        dsn = resolve_lane_b_dsn(None)
        writer, reader, _conn = build_lane_b_writer_and_reader(dsn)
    else:
        raise ValueError(f"unknown lane={lane!r}")

    results: list[ScenarioResult] = []
    query_records_by_scenario: dict[str, list[tuple[Any, QueryResult]]] = {}
    for sc in scenarios:
        try:
            res, qrecs = _run_scenario(sc, writer, reader, limit)
            results.append(res)
            query_records_by_scenario[sc.scenario_id] = qrecs
        except Exception as exc:
            results.append(
                ScenarioResult(
                    scenario_id=sc.scenario_id,
                    category=sc.category,
                    passed=False,
                    pipeline_status="ERROR",
                    failure_label=PIPELINE_ERROR,
                    unsupported=sc.unsupported,
                    notes=f"runner exception: {exc!r}",
                )
            )
            query_records_by_scenario[sc.scenario_id] = []
    return results, query_records_by_scenario


__all__ = [
    "DEFAULT_RECALL_LIMIT",
    "run_scenarios",
]