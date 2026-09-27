"""Targeted contract tests for the long-observation derived index.

All tests are offline and synthetic. Production source is never embedded in
this module. The provider-facing integration tests live in the disposable E2E
harness and are separate from this deterministic contract suite.
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
    validate_observation_chunk_plan,
)

TARGET = 7680
CFG = {"max_input_tokens": 8192, "model": "test-model"}


def char_content(tokens: int) -> str:
    return "ab" * ((2 * tokens - 1 + 1) // 2)


def test_short_below_threshold_preserves_exact_content_and_no_children():
    text = "short observation — exact source"
    plan = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    assert plan.is_long is False
    assert plan.embed_text == text
    assert plan.chunks == []
    validate_observation_chunk_plan(text, plan, target_tokens=TARGET)


def test_exact_safe_threshold_is_short():
    text = char_content(TARGET)
    plan = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    assert plan.is_long is False
    assert plan.token_count == TARGET
    assert plan.chunks == []


def test_threshold_plus_one_is_long_and_safe():
    text = char_content(TARGET + 1)
    plan = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    assert plan.is_long is True
    assert all(c.token_count <= TARGET for c in plan.chunks)
    validate_observation_chunk_plan(text, plan, target_tokens=TARGET)


def test_10965_token_source_is_chunked_not_truncated():
    text = char_content(10965)
    plan = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    assert plan.is_long is True
    assert len(plan.chunks) == 2
    assert "".join(c.text for c in plan.chunks) == text
    assert plan.chunks[-1].source_end == len(text)


def test_20k_source_is_lossless():
    text = char_content(20500)
    plan = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    assert plan.is_long is True
    assert "".join(c.text for c in plan.chunks) == text
    assert plan.chunks[0].source_start == 0
    assert plan.chunks[-1].source_end == len(text)
    assert all(c.token_count <= TARGET for c in plan.chunks)


def test_chinese_text_coverage_and_unicode_offsets():
    text = "前缀🙂中文\n" * 5000 + "尾部-唯一标记"
    plan = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    validate_observation_chunk_plan(text, plan, target_tokens=TARGET)
    assert "尾部-唯一标记" in plan.chunks[-1].text


def test_english_text_coverage():
    text = ("the quick brown fox jumps over the lazy dog. " * 4000)
    plan = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    validate_observation_chunk_plan(text, plan, target_tokens=TARGET)


def test_mixed_unicode_and_whitespace_are_preserved():
    text = " \t中文🙂 English\n\r\n" * 2000
    plan = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    validate_observation_chunk_plan(text, plan, target_tokens=TARGET)
    assert "\r\n" in "".join(c.text for c in plan.chunks)


def test_empty_and_whitespace_sources_are_safe_to_plan():
    empty = plan_observation_chunks("", CFG, tokenizer_override="char_estimate")
    whitespace = plan_observation_chunks("   \n\t", CFG, tokenizer_override="char_estimate")
    assert empty.embed_text == ""
    assert empty.chunks == []
    assert whitespace.embed_text == "   \n\t"
    assert whitespace.chunks == []


def test_child_count_is_deterministic():
    text = char_content(20000)
    a = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    b = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    assert [(c.source_start, c.source_end) for c in a.chunks] == [
        (c.source_start, c.source_end) for c in b.chunks
    ]


def test_offsets_are_contiguous_and_span_hashes_validate():
    text = "头🙂" + char_content(9000) + "尾"
    plan = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    validate_observation_chunk_plan(text, plan, target_tokens=TARGET)
    for left, right in zip(plan.chunks, plan.chunks[1:]):
        assert left.source_end == right.source_start
        assert left.source_end > left.source_start
        assert left.source_sha256


def test_source_hash_drift_is_rejected():
    text = char_content(9000)
    plan = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    with pytest.raises(ObservationChunkPlanError):
        validate_observation_chunk_plan(text + "changed", plan, target_tokens=TARGET)


def test_gap_or_overlap_is_rejected():
    text = char_content(9000)
    plan = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    broken = replace(plan, chunks=[replace(plan.chunks[1], source_start=plan.chunks[1].source_start + 1)])
    with pytest.raises(ObservationChunkPlanError):
        validate_observation_chunk_plan(text, broken, target_tokens=TARGET)


def test_representation_version_is_persisted():
    plan = plan_observation_chunks(char_content(9000), CFG, tokenizer_override="char_estimate")
    assert plan.representation_version == OBSERVATION_REPRESENTATION_VERSION
    assert all(c.representation_version == OBSERVATION_REPRESENTATION_VERSION for c in plan.chunks)


def test_parent_aggregation_requires_all_children():
    plan = plan_observation_chunks(char_content(9000), CFG, tokenizer_override="char_estimate")
    state = build_observation_derived_state(plan, lambda _: [1.0, 0.0], "fp-test")
    assert len(state.child_embeddings) == len(plan.chunks)
    assert state.parent_embedding[0] == pytest.approx(1.0)
    assert state.model_fingerprint == "fp-test"


def test_partial_child_failure_never_returns_parent():
    plan = plan_observation_chunks(char_content(20000), CFG, tokenizer_override="char_estimate")
    calls = {"n": 0}

    def embed_one(_text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TimeoutError("synthetic child timeout")
        return [1.0, 0.0]

    with pytest.raises(ObservationDerivedStateError):
        build_observation_derived_state(plan, embed_one)


def test_retry_after_child_failure_can_succeed():
    plan = plan_observation_chunks(char_content(9000), CFG, tokenizer_override="char_estimate")
    failed = {"yes": True}

    def embed_one(_text):
        if failed["yes"]:
            failed["yes"] = False
            raise TimeoutError("one retry")
        return [0.0, 1.0]

    with pytest.raises(ObservationDerivedStateError):
        build_observation_derived_state(plan, embed_one)
    state = build_observation_derived_state(plan, embed_one)
    assert state.parent_embedding[1] == pytest.approx(1.0)


def test_idempotent_plan_rebuild_has_same_source_identity():
    text = char_content(10000)
    first = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    second = plan_observation_chunks(text, CFG, tokenizer_override="char_estimate")
    assert first.source_sha256 == second.source_sha256
    assert first.chunks == second.chunks


def test_recall_child_hit_returns_full_parent_and_max_score():
    full = "FULL-PARENT" + char_content(9000)
    merged = merge_observation_recall_hits([
        {"observation_id": 7, "observation_version": "v7", "kind": "child", "cosine": 0.91, "content": full},
        {"observation_id": 7, "observation_version": "v7", "kind": "child", "cosine": 0.72, "content": "FRAGMENT"},
        {"observation_id": 7, "observation_version": "v7", "kind": "parent", "cosine": 0.60, "content": full},
    ])
    assert len(merged) == 1
    assert merged[0]["content"] == full
    assert merged[0]["cosine"] == pytest.approx(0.91)
    assert set(merged[0]["source_kinds"]) == {"child", "parent"}


def test_duplicate_parent_candidates_dedupe_once():
    merged = merge_observation_recall_hits([
        {"observation_id": 1, "observation_version": "v1", "kind": "parent", "cosine": 0.5, "content": "p"},
        {"observation_id": 1, "observation_version": "v1", "kind": "child", "cosine": 0.8, "content": "p"},
        {"observation_id": 2, "observation_version": "v2", "kind": "parent", "cosine": 0.7, "content": "q"},
    ])
    assert [(x["observation_id"], x["cosine"]) for x in merged] == [(1, 0.8), (2, 0.7)]


def test_provider_dimension_is_preserved_by_aggregation():
    plan = plan_observation_chunks(char_content(9000), CFG, tokenizer_override="char_estimate")
    state = build_observation_derived_state(plan, lambda _: [1.0, 2.0, 3.0])
    assert len(state.parent_embedding) == 3
    assert all(abs(v) > 0 for v in state.parent_embedding)


def test_zero_child_vector_is_fail_closed():
    plan = plan_observation_chunks(char_content(9000), CFG, tokenizer_override="char_estimate")
    with pytest.raises(ObservationDerivedStateError):
        build_observation_derived_state(plan, lambda _: [0.0, 0.0])


def test_unknown_tokenizer_is_fail_closed_for_long_source():
    with pytest.raises(Exception):
        plan_observation_chunks(char_content(9000), {})
