from __future__ import annotations

from v3core.embed_chunks import (
    TokenizerUnavailableError,
    aggregate_parent_embedding,
    build_qa_embedding_representation,
)


def test_short_qa_keeps_exact_representation_without_tokenizer_io():
    cfg = {"model": "model-that-is-not-needed-for-short", "max_input_tokens": 8192}
    rep = build_qa_embedding_representation("问题", "短答案", cfg)
    assert rep.is_long is False
    assert rep.chunks == []
    assert rep.embed_text == "问题\n短答案"


def test_long_char_fallback_is_explicit_and_lossless():
    question = "q"
    answer = "中段事实 " * 2500
    cfg = {"model": "offline-test", "max_input_tokens": 128, "chunk_safety_margin": 16}
    rep = build_qa_embedding_representation(
        question,
        answer,
        cfg,
        tokenizer_override="char_estimate",
    )
    assert rep.is_long is True
    assert rep.chunks
    q = "".join(c.text for c in rep.chunks if c.source_field == "question")
    a = "".join(c.text for c in rep.chunks if c.source_field == "answer")
    assert q == question
    assert a == answer
    assert max(c.token_count for c in rep.chunks) <= 112


def test_aggregate_rejects_empty_and_zero_vectors():
    import pytest

    with pytest.raises(ValueError):
        aggregate_parent_embedding([])
    with pytest.raises(ValueError):
        aggregate_parent_embedding([[0.0, 0.0]])


def test_long_without_tokenizer_fails_closed():
    import pytest

    with pytest.raises(TokenizerUnavailableError):
        build_qa_embedding_representation(
            "q",
            "x" * 20000,
            {"model": "definitely-not-a-real-model", "max_input_tokens": 128},
        )
