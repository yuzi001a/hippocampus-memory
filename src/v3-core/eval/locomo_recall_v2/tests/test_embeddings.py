# -*- coding: utf-8 -*-
"""Tests for :mod:`eval.locomo_recall_v2.embeddings`.

The test suite exercises the seven contract surfaces explicitly
called out in the task spec:

  1. **Semantics** — the production text transform
     (``qa_pairs`` = ``q + "\\n" + a``, ``conversation_stream``
     = ``content[:2000]``) is applied byte-for-byte and the
     resulting vector matches what ``v3core.embedding.embed_batch``
     returns.
  2. **Cache hit / invalidation** — repeated calls with the
     same identity hit the cache, a mismatch in any identity
     field invalidates the entry, and ``stats`` reflect the
     decision. Identity fields are stored in a sidecar
     ``.meta.json`` plus a compact ``.npz`` numerical store.
  3. **Mock batch behavior** — the provider is invoked exactly
     once per ``batch_size`` chunk (not once per row), the
     chunked vectors are mapped back to the original input
     order, and the ``expected_batch_count`` /
     ``provider_batch_count`` agree.
  4. **Dimension / profile validation** — a wrong-dim vector
     or a profile-fingerprint drift raises
     :class:`EmbedContractError` and bumps the appropriate
     stats counter. Zero vectors and wrong-dim vectors on
     disk also invalidate the cached entry.
  5. **Secret non-serialization** — the on-disk cache leaf
     MUST NOT carry ``api_key`` / ``apiKey`` / ``proxy`` /
     ``endpoint`` / ``_raw``, even if those keys are present
     in the embed_cfg the caller hands the module.
  6. **Compact numerical format** — the cache uses
     ``numpy``'s ``.npz`` (one file per cache directory),
     never per-vector JSON.
  7. **Stats report** — the returned stats expose
     ``corpus_items`` / ``corpus_chars`` (for the corpus
     entry point), ``query_items`` / ``query_chars`` (for
     the query entry point), and ``expected_batch_count`` /
     ``provider_batch_count`` that agree.

The tests use a ``monkeypatch`` of
:func:`v3core.embedding.embed_batch` so the suite never opens
a real network connection. ``build_embed_cfg`` is also
monkeypatched to a deterministic stub that returns the
canonical ``embed_cfg`` shape with a stable fingerprint so
the cache key is reproducible.

No credentials are ever supplied to the module. The
``embed_cfg`` stubs carry placeholder ``api_key`` values the
suite uses to verify those values are stripped before the
cache leaf is written.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import numpy as np
import pytest


# Path setup — mirror the locomo_recall_v2 test pattern.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


from eval.locomo_recall_v2 import embeddings as emb  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


# A canonical stub cfg that build_embed_cfg will accept. The
# production factory expects a dict with a ``storage.embed``
# sub-dict (or a flat ``embed`` sub-dict). We use the flat
# shape so the fixture is short and obvious.
_STUB_CFG: dict[str, Any] = {
    "embed": {
        "endpoint": "https://api.example.com/v1/embeddings",
        "model": "bge-m3",
        "dim": 8,
        "api_key": "SECRET_API_KEY_DO_NOT_LEAK",
        "proxy": "http://user:secret@proxy.internal:8080",
    }
}


_STUB_FP = "deadbeef0001"  # 12-char hex; matches v3core fingerprint shape


def _stub_embed_cfg_factory(cfg):
    """Drop-in replacement for ``v3core.embedding.build_embed_cfg``.

    Returns a deterministic dict the rest of the module can
    consume, with a stable ``_fingerprint`` so cache identity
    is reproducible. The returned ``embed_cfg`` ALSO carries
    the secret-bearing keys the suite uses to verify
    non-serialization.
    """
    if isinstance(cfg, dict):
        inner = cfg.get("embed", cfg)
    else:
        inner = getattr(cfg, "embed", cfg) or {}
    if isinstance(inner, dict):
        endpoint = (inner.get("endpoint") or "").rstrip("/")
        model = inner.get("model") or ""
        dim = int(inner.get("dim") or 1024)
        api_key = inner.get("api_key", "")
        proxy = inner.get("proxy", "")
    else:
        endpoint = ""
        model = ""
        dim = 1024
        api_key = ""
        proxy = ""
    if not endpoint or not model:
        raise ValueError("stub cfg requires endpoint and model")
    # Mimic the canonical factory's profile shape so the
    # module's identity-resolving code can read it.
    profile = emb._v3core_embedding.EmbedProfile(  # noqa: SLF001
        provider="custom",
        base_url="https://api.example.com",
        endpoint=endpoint,
        model=model,
        dim=dim,
        pooling="cls",
        normalization=True,
        request_format="openai",
    )
    return {
        "endpoint": endpoint,
        "dim": dim,
        "model": model,
        "api_key": api_key,
        "apiKey": api_key,
        "proxy": proxy,
        "_profile": profile,
        "_fingerprint": _STUB_FP,
        "_raw": cfg,
    }


@pytest.fixture
def patched_v3core(monkeypatch):
    """Patch the production embedder so no real HTTP happens.

    The fixture installs a deterministic ``build_embed_cfg``
    stub and an ``embed_batch`` stub that returns one
    deterministic vector per input text. The vector is
    ``[0.1 * (i + 1) for i in range(dim)]`` — every component
    is non-zero so the zero-vector guard is satisfied.
    """
    monkeypatch.setattr(
        emb._v3core_embedding,  # noqa: SLF001
        "build_embed_cfg",
        _stub_embed_cfg_factory,
    )

    # The batch stub records every call so the test can assert
    # the call count and the order of inputs.
    state: dict[str, Any] = {
        "calls": [],     # list[list[str]] — the texts argument
        "batches": 0,
    }

    def _fake_embed_batch(texts, embed_cfg, retries=3):
        state["calls"].append(list(texts))
        state["batches"] += 1
        dim = int(embed_cfg.get("dim") or 8)
        out: list[list[float]] = []
        for i, _t in enumerate(texts):
            vec = [0.1 * (j + 1 + i) for j in range(dim)]
            # Make every component non-zero.
            if all(x == 0.0 for x in vec):
                vec[0] = 0.1
            out.append(vec)
        return out

    monkeypatch.setattr(
        emb._v3core_embedding,  # noqa: SLF001
        "embed_batch",
        _fake_embed_batch,
    )
    return state


# ---------------------------------------------------------------------------
# 1. Semantics
# ---------------------------------------------------------------------------


def _here_tmp(name: str) -> str:
    """Return a unique tmp dir name for one test's cache."""
    import tempfile
    return tempfile.mkdtemp(prefix=f"locomo-emb-{name}-")


class TestTransformSemantics:
    """The module applies the production text rule byte-for-byte."""

    def test_qa_pairs_transform_is_question_newline_answer(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        results, stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=_here_tmp("qa-semantics-1"),
            dataset_sha="a" * 64,
        )
        # The provider saw the qa transform: "Q?\nA."
        assert patched_v3core["calls"][0][0] == "Q?\nA."
        assert len(results) == 1
        assert results[0].transform_id == emb.QA_PAIRS_TRANSFORM_ID
        assert results[0].cache_hit is False
        assert stats.misses == 1
        assert stats.hits == 0

    def test_qa_pairs_empty_answer_rejected(self, patched_v3core):
        # Production qa_pairs always carry a non-empty answer
        # (the dataset loader's contract). The qa_pairs row
        # identity validator refuses an empty answer so a
        # corrupted row cannot leak past the cache contract.
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="",
            ),
        ]
        with pytest.raises(emb.EmbedContractError) as exc:
            emb.prepare_corpus_embeddings(
                rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
                cache_root=_here_tmp("qa-semantics-2"),
                dataset_sha="a" * 64,
            )
        assert "answer" in str(exc.value).lower()

    def test_conversation_stream_transform_truncates_to_2000(self, patched_v3core):
        long_text = "x" * 5000
        rows = [
            emb.EmbedRow(row_id="cs|locomo|s|sk|0", text=long_text, answer=None),
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_CONVERSATION_STREAM,  # noqa: SLF001
            cache_root=_here_tmp("cs-semantics"),
            dataset_sha="a" * 64,
        )
        # The provider saw exactly 2000 chars (the production rule).
        sent = patched_v3core["calls"][0][0]
        assert len(sent) == 2000
        assert sent == "x" * 2000
        assert patched_v3core["calls"][0][0] == long_text[:2000]

    def test_query_transform_matches_qa_pairs(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|q0",
                text="What?",
                answer=None,
            ),
        ]
        emb.prepare_query_embeddings(
            rows, cfg=_STUB_CFG, cache_root=_here_tmp("q-semantics"),
            dataset_sha="a" * 64,
        )
        # Query rows have no answer → the qa transform
        # collapses to the question only.
        assert patched_v3core["calls"][0][0] == "What?"

    def test_query_one_text_per_case(self, patched_v3core):
        # The task spec is explicit: queries are ONE text per
        # case. The transform collapses the qa-pair shape
        # (question + answer) to just the question when no
        # answer is supplied. Multiple cases therefore yield
        # multiple independent texts.
        rows = [
            emb.EmbedRow(
                row_id=f"locomo|eval_v2|s|q{i}",
                text=f"What about {i}?",
                answer=None,
            )
            for i in range(3)
        ]
        results, _stats = emb.prepare_query_embeddings(
            rows, cfg=_STUB_CFG, cache_root=_here_tmp("q-many"),
            dataset_sha="a" * 64,
        )
        assert [r.text_hash for r in results]  # not empty
        texts = patched_v3core["calls"][0]
        assert len(texts) == 3
        assert texts == ["What about 0?", "What about 1?", "What about 2?"]

    def test_transform_id_constants_are_pinned(self):
        # The constants are the contract. A future production
        # rule change MUST bump the id and a corresponding test
        # must assert the new id.
        assert emb.QA_PAIRS_TRANSFORM_ID == "qa_pairs_v1::question+NL+answer"
        assert (
            emb.CONVERSATION_STREAM_TRANSFORM_ID
            == "conversation_stream_v1::content[:2000]"
        )


# ---------------------------------------------------------------------------
# 2. Cache hit / invalidation
# ---------------------------------------------------------------------------


class TestCacheHitInvalidation:
    def test_second_call_with_same_identity_hits_cache(self, patched_v3core):
        cache_root = _here_tmp("hit")
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        # First call: miss. Use a persistent stats object
        # so the test can verify the provider-call counter
        # across both calls.
        stats = emb.EmbedCacheStats()
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="0" * 64,
            stats=stats,
        )
        assert stats.misses == 1
        assert stats.hits == 0
        assert stats.provider_calls == 1
        # Second call: same identity → hit. Pass the same
        # stats object so the counters aggregate across calls.
        results, stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="0" * 64,
            stats=stats,
        )
        assert results[0].cache_hit is True
        # Across both calls: one miss (first) and one hit
        # (second). The provider was called exactly once.
        assert stats.misses == 1
        assert stats.hits == 1
        assert stats.provider_calls == 1
        assert stats.invalidations == 0

    def test_dataset_sha_change_invalidates_entry(self, patched_v3core):
        cache_root = _here_tmp("inv-ds")
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        sha1 = "a" * 64
        sha2 = "b" * 64
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha=sha1,
        )
        results, stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha=sha2,
        )
        assert results[0].cache_hit is False
        assert stats.hits == 0
        assert stats.misses == 1

    def test_transform_id_change_invalidates_entry(self, patched_v3core):
        cache_root = _here_tmp("inv-tx")
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root,
            dataset_sha="c" * 64,
            transform_id="qa_pairs_v1::question+NL+answer",
        )
        results, stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root,
            dataset_sha="c" * 64,
            transform_id="qa_pairs_v2::question+SPACE+answer",
        )
        assert results[0].cache_hit is False
        assert stats.misses == 1

    def test_model_change_invalidates_entry(self, patched_v3core, monkeypatch):
        cache_root = _here_tmp("inv-model")
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="d" * 64,
        )
        # Swap the model in the cfg and re-run. The new
        # embed_cfg still passes through the stub factory, so
        # the fingerprint stays at the same stub value — the
        # differentiator is the model name.
        cfg2 = {
            "embed": {
                "endpoint": "https://api.example.com/v1/embeddings",
                "model": "text-embedding-3-large",
                "dim": 8,
                "api_key": "SECRET_API_KEY_DO_NOT_LEAK",
                "proxy": "http://user:secret@proxy.internal:8080",
            }
        }
        results, stats = emb.prepare_corpus_embeddings(
            rows, cfg=cfg2, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="d" * 64,
        )
        assert results[0].cache_hit is False
        assert stats.misses == 1

    def test_text_change_invalidates_entry(self, patched_v3core):
        cache_root = _here_tmp("inv-text")
        row1 = emb.EmbedRow(
            row_id="locomo|eval_v2|s|session_1|d1>d2",
            text="Q?",
            answer="A.",
        )
        row2 = emb.EmbedRow(
            row_id="locomo|eval_v2|s|session_1|d1>d2",
            text="Q?",
            answer="A!",
        )
        emb.prepare_corpus_embeddings(
            [row1], cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="e" * 64,
        )
        results, stats = emb.prepare_corpus_embeddings(
            [row2], cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="e" * 64,
        )
        assert results[0].cache_hit is False
        assert stats.misses == 1

    def test_row_id_change_invalidates_entry(self, patched_v3core):
        cache_root = _here_tmp("inv-row")
        row1 = emb.EmbedRow(
            row_id="locomo|eval_v2|s|session_1|d1>d2",
            text="Q?",
            answer="A.",
        )
        row2 = emb.EmbedRow(
            row_id="locomo|eval_v2|s|session_1|d1>d3",
            text="Q?",
            answer="A.",
        )
        emb.prepare_corpus_embeddings(
            [row1], cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="f" * 64,
        )
        results, stats = emb.prepare_corpus_embeddings(
            [row2], cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="f" * 64,
        )
        assert results[0].cache_hit is False
        assert stats.misses == 1

    def test_corpus_and_query_have_separate_namespaces(self, patched_v3core):
        cache_root = _here_tmp("ns")
        # Two rows with the same row_id (legal because they
        # belong to different shapes) MUST live in different
        # cache directories. We force a query shape with the
        # same row_id as a corpus row and confirm both hit
        # their own namespace without cross-contamination.
        corpus_row = emb.EmbedRow(
            row_id="locomo|eval_v2|s|session_1|d1>d2",
            text="Q?",
            answer="A.",
        )
        query_row = emb.EmbedRow(
            row_id="locomo|eval_v2|s|session_1|d1>d2",
            text="Q?",
            answer=None,
        )
        ident = emb.describe_cache_identity(
            cache_root=cache_root,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            dataset_sha="9" * 64,
            transform_id=emb.QA_PAIRS_TRANSFORM_ID,
            cfg=_STUB_CFG,
        )
        corpus_dir = ident["cache_dir"]
        ident_q = emb.describe_cache_identity(
            cache_root=cache_root,
            kind=emb._KIND_QUERY,  # noqa: SLF001
            dataset_sha="9" * 64,
            transform_id=emb.QA_PAIRS_TRANSFORM_ID,
            cfg=_STUB_CFG,
        )
        query_dir = ident_q["cache_dir"]
        assert corpus_dir != query_dir

        # First write to the corpus namespace.
        emb.prepare_corpus_embeddings(
            [corpus_row], cfg=_STUB_CFG,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="9" * 64,
        )
        # Then write the query row to the query namespace.
        emb.prepare_query_embeddings(
            [query_row], cfg=_STUB_CFG,
            cache_root=cache_root, dataset_sha="9" * 64,
        )
        # The on-disk dirs MUST be distinct.
        assert os.path.isdir(corpus_dir)
        assert os.path.isdir(query_dir)
        assert corpus_dir != query_dir
        # Each dir carries the compact .npz + sidecar meta.
        corpus_files = sorted(os.listdir(corpus_dir))
        query_files = sorted(os.listdir(query_dir))
        assert "vectors.npz" in corpus_files
        assert "vectors.meta.json" in corpus_files
        assert "vectors.npz" in query_files
        assert "vectors.meta.json" in query_files


# ---------------------------------------------------------------------------
# 3. Mock batch behavior — real batch_size chunking
# ---------------------------------------------------------------------------


class TestMockBatchBehavior:
    def test_provider_is_called_once_for_all_misses(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id=f"locomo|eval_v2|s|session_1|d{i}>{i + 100}",
                text=f"Q{i}?",
                answer=f"A{i}.",
            )
            for i in range(5)
        ]
        results, stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=_here_tmp("batch-once"),
            dataset_sha="1" * 64,
        )
        # Default batch_size is 32, so 5 misses fit in one batch.
        assert patched_v3core["batches"] == 1
        assert patched_v3core["calls"][0] == [
            "Q0?\nA0.",
            "Q1?\nA1.",
            "Q2?\nA2.",
            "Q3?\nA3.",
            "Q4?\nA4.",
        ]
        # Expected / provider batch counts must agree.
        assert stats.expected_batch_count == 1
        assert stats.provider_batch_count == 1
        # Results in original order.
        assert [r.row_id for r in results] == [r.row_id for r in rows]

    def test_batch_size_2_chunks_five_misses_into_three_calls(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id=f"locomo|eval_v2|s|session_1|d{i}>{i + 100}",
                text=f"Q{i}?",
                answer=f"A{i}.",
            )
            for i in range(5)
        ]
        results, stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=_here_tmp("batch-2"),
            dataset_sha="a" * 64,
            batch_size=2,
        )
        # ceil(5/2) = 3 batches.
        assert patched_v3core["batches"] == 3
        assert stats.expected_batch_count == 3
        assert stats.provider_batch_count == 3
        # Chunks in original order, last chunk a partial.
        assert patched_v3core["calls"][0] == ["Q0?\nA0.", "Q1?\nA1."]
        assert patched_v3core["calls"][1] == ["Q2?\nA2.", "Q3?\nA3."]
        assert patched_v3core["calls"][2] == ["Q4?\nA4."]
        # Results in original order.
        assert [r.row_id for r in results] == [r.row_id for r in rows]

    def test_cache_hits_skip_provider(self, patched_v3core):
        cache_root = _here_tmp("batch-skip")
        rows = [
            emb.EmbedRow(
                row_id=f"locomo|eval_v2|s|session_1|d{i}>{i + 100}",
                text=f"Q{i}?",
                answer=f"A{i}.",
            )
            for i in range(3)
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="2" * 64,
        )
        assert patched_v3core["batches"] == 1
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="2" * 64,
        )
        # Second call: still one batch from the first call,
        # zero batches from the second.
        assert patched_v3core["batches"] == 1

    def test_partial_miss_batches_only_misses(self, patched_v3core):
        cache_root = _here_tmp("batch-partial")
        row1 = emb.EmbedRow(
            row_id="locomo|eval_v2|s|session_1|d1>d2",
            text="Q1?",
            answer="A1.",
        )
        row2 = emb.EmbedRow(
            row_id="locomo|eval_v2|s|session_1|d3>d4",
            text="Q3?",
            answer="A3.",
        )
        # First call: both miss → 1 batch with 2 texts.
        emb.prepare_corpus_embeddings(
            [row1, row2], cfg=_STUB_CFG,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="3" * 64,
        )
        # Second call: both hit → 0 batches.
        emb.prepare_corpus_embeddings(
            [row1, row2], cfg=_STUB_CFG,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="3" * 64,
        )
        assert patched_v3core["batches"] == 1

        # Third call: row1 hit, row2 mutates → 1 batch with 1 text.
        row2_mut = emb.EmbedRow(
            row_id="locomo|eval_v2|s|session_1|d3>d4",
            text="Q3?",
            answer="A3-mut.",
        )
        emb.prepare_corpus_embeddings(
            [row1, row2_mut], cfg=_STUB_CFG,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="3" * 64,
        )
        assert patched_v3core["batches"] == 2
        assert patched_v3core["calls"][1] == ["Q3?\nA3-mut."]

    def test_results_are_in_input_order(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id=f"locomo|eval_v2|s|session_1|d{i}>{i + 100}",
                text=f"Q{i}?",
                answer=f"A{i}.",
            )
            for i in range(4)
        ]
        results, _stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=_here_tmp("order"),
            dataset_sha="4" * 64,
        )
        assert [r.row_id for r in results] == [r.row_id for r in rows]
        # The stub returns one vector per text; the vectors
        # are NOT zero and are not all equal, so the test can
        # also assert that the returned vectors are the same
        # vectors the provider produced.
        for r in results:
            assert r.vector
            assert all(x != 0.0 for x in r.vector)

    def test_empty_input_returns_empty_results(self, patched_v3core):
        results, stats = emb.prepare_corpus_embeddings(
            [], cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=_here_tmp("empty"),
            dataset_sha="a" * 64,
        )
        assert results == []
        assert stats.lookups == 0
        assert stats.misses == 0
        assert stats.hits == 0
        assert patched_v3core["batches"] == 0
        # No misses → no expected or provider batches.
        assert stats.expected_batch_count == 0
        assert stats.provider_batch_count == 0

    def test_invalid_batch_size_rejected(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        with pytest.raises(emb.EmbedContractError) as exc:
            emb.prepare_corpus_embeddings(
                rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
                cache_root=_here_tmp("bad-batch"),
                dataset_sha="b" * 64,
                batch_size=0,
            )
        assert "batch_size" in str(exc.value)


# ---------------------------------------------------------------------------
# 4. Dimension / profile validation
# ---------------------------------------------------------------------------


class TestDimensionAndProfileValidation:
    def test_wrong_dim_from_provider_raises(self, monkeypatch, patched_v3core):
        # Replace the stub with one that returns a vector of
        # the wrong length. The module must raise
        # EmbedContractError and bump the dim_mismatches stat.
        def _bad_dim_batch(texts, embed_cfg, retries=3):
            return [[0.0] * (int(embed_cfg.get("dim") or 8) + 1) for _ in texts]

        monkeypatch.setattr(
            emb._v3core_embedding,  # noqa: SLF001
            "embed_batch",
            _bad_dim_batch,
        )
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        with pytest.raises(emb.EmbedContractError) as exc:
            emb.prepare_corpus_embeddings(
                rows, cfg=_STUB_CFG,
                kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
                cache_root=_here_tmp("bad-dim"),
                dataset_sha="5" * 64,
            )
        assert "dim" in str(exc.value).lower()

    def test_zero_vector_from_provider_rejected(self, monkeypatch, patched_v3core):
        def _zero_batch(texts, embed_cfg, retries=3):
            return [[0.0] * int(embed_cfg.get("dim") or 8) for _ in texts]

        monkeypatch.setattr(
            emb._v3core_embedding,  # noqa: SLF001
            "embed_batch",
            _zero_batch,
        )
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        with pytest.raises(emb.EmbedContractError) as exc:
            emb.prepare_corpus_embeddings(
                rows, cfg=_STUB_CFG,
                kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
                cache_root=_here_tmp("zero"),
                dataset_sha="6" * 64,
            )
        assert "zero" in str(exc.value).lower()

    def test_fingerprint_drift_raises(self, monkeypatch, patched_v3core):
        # Provider returns valid vectors but the embed_cfg
        # fingerprint the stub attached to the cfg is no
        # longer the one the module expected (e.g. a hot-swap
        # of the config between _resolve_profile and the
        # provider call). The module must catch this on the
        # cross-check inside _call_provider_batch.
        def _drifting_batch(texts, embed_cfg, retries=3):
            # Mutate the cfg fingerprint before returning —
            # the cross-check inside the module looks at the
            # cfg the provider was called with.
            embed_cfg["_fingerprint"] = "ffffffffffff"
            dim = int(embed_cfg.get("dim") or 8)
            return [[0.1] * dim for _ in texts]

        monkeypatch.setattr(
            emb._v3core_embedding,  # noqa: SLF001
            "embed_batch",
            _drifting_batch,
        )
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        with pytest.raises(emb.EmbedContractError) as exc:
            emb.prepare_corpus_embeddings(
                rows, cfg=_STUB_CFG,
                kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
                cache_root=_here_tmp("drift"),
                dataset_sha="7" * 64,
            )
        assert "fingerprint" in str(exc.value).lower()

    def test_zero_vector_on_disk_invalidates(self, monkeypatch, patched_v3core):
        # Cache a real vector, then corrupt the npz on disk so
        # the loaded vector is all-zero. The read path must
        # invalidate the entry, bump zero_vectors_rejected,
        # and re-issue a provider call.
        rows_sha = "f1" + "0" * 62
        cache_root = _here_tmp("zero-disk")
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha=rows_sha,
        )
        # Find the npz and zero out the stored vector.
        ident = emb.describe_cache_identity(
            cache_root=cache_root, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            dataset_sha=rows_sha, transform_id=emb.QA_PAIRS_TRANSFORM_ID,
            cfg=_STUB_CFG,
        )
        npz_path = os.path.join(ident["cache_dir"], "vectors.npz")
        assert os.path.isfile(npz_path)
        with np.load(npz_path, allow_pickle=True) as z:
            row_ids = z["row_ids"]
            text_hashes = z["text_hashes"]
            text_preview = z["texts_preview"]
            dim = z["vectors"].shape[1]
        bad = np.zeros((1, dim), dtype=np.float32)
        np.savez(
            npz_path, vectors=bad, row_ids=row_ids,
            text_hashes=text_hashes, texts_preview=text_preview,
        )
        # Second call — read must invalidate, provider must
        # re-issue.
        results, stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha=rows_sha,
        )
        assert results[0].cache_hit is False
        assert stats.zero_vectors_rejected == 1
        assert stats.invalidations == 1

    def test_wrong_dim_on_disk_invalidates(self, monkeypatch, patched_v3core):
        # Cache a real vector, then corrupt the npz so the
        # stored vector has the wrong dim. The read path
        # must invalidate the entry, bump dim_mismatches,
        # and re-issue a provider call.
        # Use a distinct sha from any other test to avoid
        # any chance of cross-test cache reuse.
        rows_sha = "e1" + "0" * 62
        cache_root = _here_tmp("dim-disk")
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha=rows_sha,
        )
        ident = emb.describe_cache_identity(
            cache_root=cache_root, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            dataset_sha=rows_sha, transform_id=emb.QA_PAIRS_TRANSFORM_ID,
            cfg=_STUB_CFG,
        )
        npz_path = os.path.join(ident["cache_dir"], "vectors.npz")
        with np.load(npz_path, allow_pickle=True) as z:
            row_ids = z["row_ids"]
            text_hashes = z["text_hashes"]
            text_preview = z["texts_preview"]
        # Replace vectors with a row of the WRONG dim.
        bad = np.ones((1, int(_STUB_CFG["embed"]["dim"]) + 4), dtype=np.float32)
        np.savez(
            npz_path, vectors=bad, row_ids=row_ids,
            text_hashes=text_hashes, texts_preview=text_preview,
        )
        results, stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha=rows_sha,
        )
        assert results[0].cache_hit is False
        assert stats.dim_mismatches == 1
        assert stats.invalidations == 1

    def test_invalid_kind_rejected(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        with pytest.raises(emb.EmbedContractError):
            emb.prepare_corpus_embeddings(
                rows, cfg=_STUB_CFG, kind="bogus_kind",
                cache_root=_here_tmp("bad-kind"),
            )

    def test_invalid_row_id_rejected_for_qa_pairs(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id="not a valid source id",
                text="Q?",
                answer="A.",
            ),
        ]
        with pytest.raises(emb.EmbedContractError):
            emb.prepare_corpus_embeddings(
                rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
                cache_root=_here_tmp("bad-rowid"),
                dataset_sha="8" * 64,
            )


# ---------------------------------------------------------------------------
# 5. Secret non-serialization
# ---------------------------------------------------------------------------


class TestSecretNonSerialization:
    def test_no_api_key_in_meta_sidecar(self, patched_v3core):
        cache_root = _here_tmp("secret-1")
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="a" * 64,
        )
        # Walk the on-disk cache and assert no secret is
        # present in any meta sidecar.
        for root, _dirs, files in os.walk(cache_root):
            for fn in files:
                if not fn.endswith(".meta.json"):
                    continue
                p = os.path.join(root, fn)
                with open(p, "r", encoding="utf-8") as f:
                    blob = f.read()
                assert "SECRET_API_KEY_DO_NOT_LEAK" not in blob
                payload = json.loads(blob)
                for forbidden in (
                    "api_key", "apiKey", "proxy", "endpoint", "_raw",
                ):
                    assert forbidden not in payload, (
                        f"meta sidecar at {p} contains forbidden key {forbidden!r}"
                    )

    def test_no_proxy_or_endpoint_in_meta_sidecar(self, patched_v3core):
        cache_root = _here_tmp("secret-2")
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="b" * 64,
        )
        for root, _dirs, files in os.walk(cache_root):
            for fn in files:
                if not fn.endswith(".meta.json"):
                    continue
                p = os.path.join(root, fn)
                with open(p, "r", encoding="utf-8") as f:
                    blob = f.read()
                assert "proxy.internal" not in blob
                assert "api.example.com" not in blob
                # The leaf carries only the model / dim /
                # fingerprint / transform / dataset sha — never
                # the full URL.
                assert "https://" not in blob

    def test_npz_does_not_carry_secrets(self, patched_v3core):
        # The compact .npz carries only the numeric vectors
        # and the SHA-256 hex strings (row_ids, text_hashes,
        # texts_preview). No endpoint, no proxy, no API key.
        cache_root = _here_tmp("secret-3")
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="c" * 64,
        )
        for root, _dirs, files in os.walk(cache_root):
            for fn in files:
                if not fn.endswith(".npz"):
                    continue
                p = os.path.join(root, fn)
                with np.load(p, allow_pickle=True) as z:
                    keys = set(z.files)
                    # The .npz carries exactly the four
                    # documented arrays — nothing else.
                    assert keys == {
                        "vectors", "row_ids", "text_hashes", "texts_preview",
                    }, f"unexpected npz keys at {p}: {keys}"
                    # No secret strings anywhere in the npz.
                    for k in keys:
                        if k == "vectors":
                            continue
                        joined = "".join(str(x) for x in z[k].tolist())
                        assert "SECRET_API_KEY" not in joined
                        assert "proxy.internal" not in joined
                        assert "api.example.com" not in joined

    def test_scrub_embed_cfg_drops_secrets(self):
        # The scrub helper is the single seam that enforces
        # the secret-free contract. Direct unit coverage of
        # the helper.
        cfg = {
            "endpoint": "https://api.example.com/v1/embeddings",
            "dim": 8,
            "model": "bge-m3",
            "api_key": "SECRET_API_KEY",
            "apiKey": "SECRET_API_KEY",
            "proxy": "http://user:pw@proxy:8080",
            "_profile": "profile-obj",
            "_fingerprint": "deadbeef0001",
            "_raw": {"sneaky": "raw_cfg"},
        }
        safe = emb._scrub_embed_cfg_for_cache(cfg)  # noqa: SLF001
        for forbidden in ("api_key", "apiKey", "proxy", "endpoint", "_raw"):
            assert forbidden not in safe
        # The identity-bearing fields stay.
        assert safe["_profile"] == "profile-obj"
        assert safe["_fingerprint"] == "deadbeef0001"
        assert safe["dim"] == 8
        assert safe["model"] == "bge-m3"


# ---------------------------------------------------------------------------
# 6. Compact numerical format / atomic write
# ---------------------------------------------------------------------------


class TestCompactFormatAndAtomicWrite:
    def test_cache_uses_npz_not_per_vector_json(self, patched_v3core):
        # The task contract: use numpy's .npz, NOT per-vector
        # JSON. After a successful prepare call there must be
        # a single .npz (plus a single .meta.json sidecar)
        # per cache directory — and ZERO per-vector .json
        # files.
        cache_root = _here_tmp("compact")
        rows = [
            emb.EmbedRow(
                row_id=f"locomo|eval_v2|s|session_1|d{i}>{i + 100}",
                text=f"Q{i}?",
                answer=f"A{i}.",
            )
            for i in range(7)
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="c" * 64,
        )
        npz_files = 0
        meta_files = 0
        per_vec_json = 0
        for _root, _dirs, files in os.walk(cache_root):
            for fn in files:
                if fn.endswith(".npz"):
                    npz_files += 1
                if fn.endswith(".meta.json"):
                    meta_files += 1
                if fn.endswith(".json") and not fn.endswith(".meta.json"):
                    per_vec_json += 1
        assert npz_files == 1
        assert meta_files == 1
        assert per_vec_json == 0

    def test_no_tmp_files_left_after_write(self, patched_v3core):
        # Atomic write writes to ``*.tmp`` then os.replace.
        # After the call returns there must be NO ``.tmp``
        # files left under the cache root.
        cache_root = _here_tmp("atomic")
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="a" * 64,
        )
        for _root, _dirs, files in os.walk(cache_root):
            for fn in files:
                assert ".tmp" not in fn, f"leftover tmp file: {fn}"

    def test_npz_stores_all_rows_in_one_archive(self, patched_v3core):
        cache_root = _here_tmp("all-rows")
        rows = [
            emb.EmbedRow(
                row_id=f"locomo|eval_v2|s|session_1|d{i}>{i + 100}",
                text=f"Q{i}?",
                answer=f"A{i}.",
            )
            for i in range(6)
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="c0" * 32,
            batch_size=2,  # three chunks → three separate
                           # writes, but the archive is
                           # merged each time.
        )
        ident = emb.describe_cache_identity(
            cache_root=cache_root, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            dataset_sha="c0" * 32, transform_id=emb.QA_PAIRS_TRANSFORM_ID,
            cfg=_STUB_CFG,
        )
        npz_path = os.path.join(ident["cache_dir"], "vectors.npz")
        with np.load(npz_path, allow_pickle=True) as z:
            assert z["vectors"].shape == (6, 8)
            assert list(z["row_ids"]) == [r.row_id for r in rows]
            assert len(z["text_hashes"]) == 6
            assert len(z["texts_preview"]) == 6

    def test_second_call_writes_no_npz(self, patched_v3core):
        # Cache-hit second call must not re-write the npz.
        cache_root = _here_tmp("no-rewrite")
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="c1" * 32,
        )
        ident = emb.describe_cache_identity(
            cache_root=cache_root, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            dataset_sha="c1" * 32, transform_id=emb.QA_PAIRS_TRANSFORM_ID,
            cfg=_STUB_CFG,
        )
        npz_path = os.path.join(ident["cache_dir"], "vectors.npz")
        mtime_1 = os.stat(npz_path).st_mtime_ns
        emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root, dataset_sha="c1" * 32,
        )
        mtime_2 = os.stat(npz_path).st_mtime_ns
        assert mtime_2 == mtime_1


# ---------------------------------------------------------------------------
# 7. Stats report — item counts, char totals, batch counts
# ---------------------------------------------------------------------------


class TestStatsReport:
    def test_corpus_items_and_chars_reported(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id=f"locomo|eval_v2|s|session_1|d{i}>{i + 100}",
                text=f"Q{i}?",
                answer=f"A{i}.",
            )
            for i in range(3)
        ]
        _, stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=_here_tmp("stats-corpus"),
            dataset_sha="c2" * 32,
        )
        assert stats.corpus_items == 3
        assert stats.query_items == 0
        # Each transform yields "Q{i}?\nA{i}." (6 UTF-8
        # bytes + len(int) bytes). Just assert the counter is
        # positive and matches the input.
        assert stats.corpus_chars > 0
        # Provider batch / expected batch must agree for the
        # default batch_size=32 (all 3 fits in one chunk).
        assert stats.expected_batch_count == 1
        assert stats.provider_batch_count == 1

    def test_query_items_and_chars_reported(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id=f"locomo|eval_v2|s|q{i}",
                text=f"What about {i}?",
                answer=None,
            )
            for i in range(4)
        ]
        _, stats = emb.prepare_query_embeddings(
            rows, cfg=_STUB_CFG,
            cache_root=_here_tmp("stats-query"),
            dataset_sha="c3" * 32,
        )
        assert stats.query_items == 4
        assert stats.corpus_items == 0
        assert stats.query_chars > 0
        assert stats.expected_batch_count == 1
        assert stats.provider_batch_count == 1

    def test_batch_counts_match_actual_calls(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id=f"locomo|eval_v2|s|session_1|d{i}>{i + 100}",
                text=f"Q{i}?",
                answer=f"A{i}.",
            )
            for i in range(10)
        ]
        _, stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=_here_tmp("stats-batches"),
            dataset_sha="b" * 64,
            batch_size=3,
        )
        # ceil(10/3) = 4.
        assert stats.expected_batch_count == 4
        assert stats.provider_batch_count == 4
        assert stats.provider_calls == 4
        assert stats.misses == 10

    def test_stats_to_dict_round_trip(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        _, stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=_here_tmp("stats-roundtrip"),
            dataset_sha="c4" * 32,
        )
        d = stats.to_dict()
        # The shape-tagged fields are present.
        assert "corpus_items" in d
        assert "query_items" in d
        assert "corpus_chars" in d
        assert "query_chars" in d
        assert "expected_batch_count" in d
        assert "provider_batch_count" in d
        assert d["corpus_items"] == 1


# ---------------------------------------------------------------------------
# 8. Identity resolution (helper coverage)
# ---------------------------------------------------------------------------


class TestIdentityResolution:
    def test_dataset_sha_required(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        with pytest.raises(emb.EmbedContractError):
            emb.prepare_corpus_embeddings(
                rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
                cache_root=_here_tmp("no-ds"),
            )

    def test_invalid_dataset_sha_format_rejected(self, patched_v3core):
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        with pytest.raises(emb.EmbedContractError):
            emb.prepare_corpus_embeddings(
                rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
                cache_root=_here_tmp("bad-ds"),
                dataset_sha="not-a-sha",
            )

    def test_dataset_object_cross_checked(self, patched_v3core):
        # Pass a real LoCoMoDataset with source_sha256 — the
        # module uses it as the canonical identity. A
        # mismatched explicit dataset_sha must be refused.
        from eval.locomo_recall_v2 import dataset as ds

        fake_dataset = ds.LoCoMoDataset(
            source_path="<memory>",
            source_sha256="c" * 64,
            source_bytes=0,
            samples=(),
            eval_rows=(),
            qa_pairs=(),
            category_counts={},
            message_count=0,
            qa_pair_count=0,
        )
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        with pytest.raises(emb.EmbedContractError):
            emb.prepare_corpus_embeddings(
                rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
                cache_root=_here_tmp("ds-cross"),
                dataset=fake_dataset,
                dataset_sha="d" * 64,
            )

    def test_dataset_object_accepted_when_consistent(self, patched_v3core):
        from eval.locomo_recall_v2 import dataset as ds

        fake_dataset = ds.LoCoMoDataset(
            source_path="<memory>",
            source_sha256="e" * 64,
            source_bytes=0,
            samples=(),
            eval_rows=(),
            qa_pairs=(),
            category_counts={},
            message_count=0,
            qa_pair_count=0,
        )
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d1>d2",
                text="Q?",
                answer="A.",
            ),
        ]
        results, _stats = emb.prepare_corpus_embeddings(
            rows, cfg=_STUB_CFG, kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=_here_tmp("ds-ok"),
            dataset=fake_dataset,
        )
        assert results[0].cache_hit is False


# ---------------------------------------------------------------------------
# 9. Row builders (helper coverage)
# ---------------------------------------------------------------------------


class TestRowBuilders:
    def test_build_qa_pair_rows_from_dataset_pair(self):
        from eval.locomo_recall_v2 import dataset as ds

        pair = ds.LoCoMoQAPair(
            sample_id="s",
            session_key="session_1",
            q_dia_id="d1",
            a_dia_id="d2",
            source_id="locomo|eval_v2|s|session_1|d1>d2",
            q_provenance={},
            a_provenance={},
            question="Q?",
            answer="A.",
            timestamp=None,
            q_text="Q?",
            a_text="A.",
            q_image={},
            a_image={},
        )
        rows = emb.build_qa_pair_rows([pair])
        assert len(rows) == 1
        assert rows[0].row_id == "locomo|eval_v2|s|session_1|d1>d2"
        assert rows[0].text == "Q?"
        assert rows[0].answer == "A."

    def test_build_qa_pair_rows_skips_invalid_source_id(self):
        # An invalid source_id is silently skipped (the
        # dataset loader's contract guarantees a valid id;
        # anything that reaches the embedding prep is
        # already a bug, but the helper should not crash).
        bad = type("FakePair", (), {})()
        bad.source_id = "not valid"
        bad.question = "Q?"
        bad.answer = "A."
        rows = emb.build_qa_pair_rows([bad])
        assert rows == []

    def test_build_conversation_stream_rows_synthesizes_id(self):
        from eval.locomo_recall_v2 import dataset as ds

        msg = ds.LoCoMoMessage(
            sample_id="s",
            session_key="session_1",
            index=7,
            speaker="speaker_1",
            dia_id="d7",
            text="hello",
            img_url=None,
            blip_caption=None,
            img_query=None,
            re_download=None,
            raw_session_dt="2023-05-08",
            session_dt_utc=None,
        )
        rows = emb.build_conversation_stream_rows([msg])
        assert len(rows) == 1
        assert rows[0].row_id == "cs|locomo|s|session_1|7"
        assert rows[0].text == "hello"
        assert rows[0].answer is None

    def test_build_query_rows_uses_eval_v2_namespace(self):
        from eval.locomo_recall_v2 import dataset as ds

        row = ds.LoCoMoEvalRow(
            sample_id="s",
            qa_id="q0",
            query_idx=0,
            question="What?",
            answer="A.",
            category="cat",
            evidence=("d1",),
            source_hash="h",
            qa_pair_source_id=None,
            qa_dia_id=None,
        )
        rows = emb.build_query_rows([row])
        assert len(rows) == 1
        assert rows[0].row_id == "locomo|eval_v2|s|q0"
        assert rows[0].text == "What?"
        assert rows[0].answer is None

# ---------------------------------------------------------------------------
# 9. Cache lookup cost
# ---------------------------------------------------------------------------


class TestCacheLookupIsIndexed:
    """A cache-hit pass must not read the archive once per row.

    Regression: ``_lookup_row`` re-read and linearly scanned the whole
    ``.npz`` for every row, so a full-corpus pass degraded to ~1 row/s
    (~30-60 s per 64-row chunk) even though the provider answers a
    64-text batch in under a second. The indexed path loads the leaf
    once per ``prepare_*`` call.
    """

    def test_cache_hit_pass_loads_npz_once(self, patched_v3core, monkeypatch):
        rows = [
            emb.EmbedRow(
                row_id="locomo|eval_v2|s|session_1|d%d>d%d" % (i, i + 1),
                text="Q%d?" % i,
                answer="A%d." % i,
            )
            for i in range(24)
        ]
        cache_root = _here_tmp("indexed-lookup")
        emb.prepare_corpus_embeddings(
            rows,
            cfg=_STUB_CFG,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root,
            dataset_sha="b" * 64,
            batch_size=8,
        )

        loads = {"n": 0}
        real_load = np.load

        def _counting_load(*args, **kwargs):
            loads["n"] += 1
            return real_load(*args, **kwargs)

        monkeypatch.setattr(emb.np, "load", _counting_load)
        results, stats = emb.prepare_corpus_embeddings(
            rows,
            cfg=_STUB_CFG,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root,
            dataset_sha="b" * 64,
            batch_size=8,
        )

        assert all(r.cache_hit for r in results)
        assert stats.hits == len(rows)
        assert stats.misses == 0
        assert patched_v3core["batches"] == 3  # first pass only
        assert loads["n"] == 1, (
            "np.load called %d times for %d cached rows; the indexed "
            "lookup must load the leaf once" % (loads["n"], len(rows))
        )

    def test_duplicate_rows_do_not_need_a_second_provider_call(
        self, patched_v3core
    ):
        row = emb.EmbedRow(
            row_id="locomo|eval_v2|s|session_1|d1>d2",
            text="Q?",
            answer="A.",
        )
        cache_root = _here_tmp("indexed-dedupe")
        results, stats = emb.prepare_corpus_embeddings(
            [row, row, row],
            cfg=_STUB_CFG,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root,
            dataset_sha="c" * 64,
            batch_size=8,
        )
        assert len(results) == 3
        # The whole batch is ONE provider chunk (the stub returns a
        # per-index vector, so vector equality is not asserted here —
        # production ``embed_batch`` dedupes by text and maps back).
        assert patched_v3core["batches"] == 1
        assert len(patched_v3core["calls"][0]) == 3

        # A second pass over the same rows is fully cached: the index
        # written by the first pass answers every duplicate.
        second, stats2 = emb.prepare_corpus_embeddings(
            [row, row, row],
            cfg=_STUB_CFG,
            kind=emb._KIND_QA_PAIRS,  # noqa: SLF001
            cache_root=cache_root,
            dataset_sha="c" * 64,
            batch_size=8,
        )
        assert patched_v3core["batches"] == 1  # no new provider call
        assert all(r.cache_hit for r in second)
        assert stats2.hits == 3

