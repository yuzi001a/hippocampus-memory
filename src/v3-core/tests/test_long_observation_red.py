"""RED-to-GREEN contract tests for the long-observation index.

The original RED run is preserved at ``evidence/red-before-implementation.log``
(the module did not exist at that point). These tests now exercise the final
public planner/aggregation/dedupe seam rather than a placeholder import.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from v3core.observation_chunks import (
    OBSERVATION_REPRESENTATION_VERSION,
    ObservationChunkPlanError,
    ObservationDerivedStateError,
    build_observation_derived_state,
    merge_observation_recall_hits,
    plan_observation_chunks,
    stale_child_indexes,
    validate_observation_chunk_plan,
)

SAFE_TARGET = 7680


def chars_for_tokens(n_tokens: int) -> str:
    return "ab" * ((2 * n_tokens - 1 + 1) // 2)


def plan(text: str):
    return plan_observation_chunks(text, None, tokenizer_override="char_estimate")


def test_red1_10965_token_equivalent_takes_safe_chunked_path():
    text = chars_for_tokens(10965)
    rep = plan(text)
    assert rep.is_long
    assert rep.token_count == 10965
    assert len(rep.chunks) > 1
    assert all(c.token_count <= SAFE_TARGET for c in rep.chunks)
    assert all(c.representation_version == OBSERVATION_REPRESENTATION_VERSION for c in rep.chunks)


def test_red2_20k_source_is_lossless_and_contiguous():
    text = chars_for_tokens(20500)
    rep = plan(text)
    assert rep.is_long
    assert [c.chunk_index for c in rep.chunks] == list(range(len(rep.chunks)))
    assert rep.chunks[0].source_start == 0
    assert rep.chunks[-1].source_end == len(text)
    assert all(a.source_end == b.source_start for a, b in zip(rep.chunks, rep.chunks[1:]))
    assert "".join(c.text for c in rep.chunks) == text
    assert all(c.token_count <= SAFE_TARGET for c in rep.chunks)
    validate_observation_chunk_plan(text, rep, target_tokens=SAFE_TARGET)


def test_red3_tail_child_maps_to_full_parent_and_dedupes():
    text = chars_for_tokens(9000) + "TAIL-MARKER-7f3a-distinctive"
    rep = plan(text)
    tail = [c for c in rep.chunks if "TAIL-MARKER" in c.text]
    assert len(tail) == 1 and tail[0].chunk_index > 0
    merged = merge_observation_recall_hits([
        {"observation_id": 7, "observation_version": "v1", "kind": "child",
         "cosine": 0.62, "parent_content": text},
        {"observation_id": 7, "observation_version": "v1", "kind": "child",
         "cosine": 0.71, "parent_content": text},
    ])
    assert len(merged) == 1
    assert merged[0]["content"] == text
    assert merged[0]["cosine"] == pytest.approx(0.71)


def test_red4_partial_child_failure_has_no_parent():
    rep = plan(chars_for_tokens(10000))
    calls = 0

    def embed_one(_text):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("synthetic child timeout")
        return [1.0, 0.0]

    with pytest.raises(ObservationDerivedStateError):
        build_observation_derived_state(rep, embed_one)
    assert calls == 2


def test_red5_stale_child_index_is_scoped_to_parent():
    assert stale_child_indexes(3, [0, 1, 2, 3]) == [3]
    assert stale_child_indexes(3, [0, 1, 2]) == []
    assert stale_child_indexes(3, [0, 3, 4, 4]) == [3, 4]


def test_short_path_is_full_content_and_has_no_children():
    text = "短 observation / short parity ✅"
    rep = plan(text)
    assert not rep.is_long
    assert rep.embed_text == text
    assert rep.chunks == []
    state = build_observation_derived_state(rep, lambda value: [1.0, 2.0])
    assert state.child_embeddings == []
    assert state.parent_embedding == [1.0, 2.0]


def test_exact_target_and_target_plus_one_are_deterministic():
    exact = plan(chars_for_tokens(SAFE_TARGET))
    plus_one = plan(chars_for_tokens(SAFE_TARGET + 1))
    assert not exact.is_long
    assert plus_one.is_long
    assert len(plus_one.chunks) == 2
    assert [c.token_count for c in plus_one.chunks] == [SAFE_TARGET, 1]


def test_unicode_offsets_and_source_hash_are_exact():
    text = ("中文🙂é\n" * 4000) + "尾部-Ω"
    rep = plan(text)
    validate_observation_chunk_plan(text, rep, target_tokens=SAFE_TARGET)
    assert "".join(c.text for c in rep.chunks) == text
    assert rep.source_sha256
    assert all(c.source_sha256 for c in rep.chunks)


def test_empty_and_whitespace_plans_are_safe():
    empty = plan("")
    spaces = plan(" \t\n")
    assert empty.embed_text == ""
    assert empty.chunks == []
    assert spaces.embed_text == " \t\n"
    assert spaces.chunks == []


def test_source_mutation_is_rejected_before_derived_write():
    text = chars_for_tokens(9000)
    rep = plan(text)
    with pytest.raises(ObservationChunkPlanError):
        validate_observation_chunk_plan(text + "changed", rep, target_tokens=SAFE_TARGET)


def test_representation_version_drift_is_rejected():
    text = chars_for_tokens(9000)
    rep = plan(text)
    bad = replace(rep, representation_version="old-version")
    with pytest.raises(ObservationChunkPlanError):
        validate_observation_chunk_plan(text, bad, target_tokens=SAFE_TARGET)
