from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

from v3core import rerank as rerank_module  # noqa: E402
from v3core.recall_pool import _rerank  # noqa: E402
from v3core.types import RecallHit  # noqa: E402


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def _install_results(monkeypatch: pytest.MonkeyPatch, results: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(
        rerank_module.requests,
        "post",
        lambda *args, **kwargs: _Response({"results": results}),
    )


def test_reorders_scores_by_backend_index(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_results(
        monkeypatch,
        [
            {"index": 2, "relevance_score": 0.3},
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.4},
        ],
    )

    scores = rerank_module.rerank("q", ["A", "B", "C"], {"endpoint": "https://rerank.invalid"})

    assert scores == [0.9, 0.4, 0.3]


def test_recall_pool_keeps_score_bound_to_the_indexed_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_results(
        monkeypatch,
        [
            {"index": 2, "relevance_score": 0.99},
            {"index": 0, "relevance_score": 0.01},
            {"index": 1, "relevance_score": 0.90},
        ],
    )
    hits = [
        RecallHit(source_id=source_id, title=source_id, content_preview=source_id)
        for source_id in ("A", "B", "C")
    ]

    ranked = _rerank("q", hits, top_n=3, rerank_cfg={"endpoint": "https://rerank.invalid"})

    assert [(hit.source_id, hit.cosine) for hit in ranked] == [
        ("C", 0.99),
        ("B", 0.90),
        ("A", 0.01),
    ]


@pytest.mark.parametrize(
    "results",
    [
        pytest.param(
            [
                {"index": 0, "relevance_score": 0.1},
                {"relevance_score": 0.2},
                {"index": 2, "relevance_score": 0.3},
            ],
            id="missing-index-field",
        ),
        pytest.param(
            [
                {"index": 0, "relevance_score": 0.1},
                {"index": 0, "relevance_score": 0.2},
                {"index": 2, "relevance_score": 0.3},
            ],
            id="duplicate-index",
        ),
        pytest.param(
            [
                {"index": 0, "relevance_score": 0.1},
                {"index": 1, "relevance_score": 0.2},
                {"index": 3, "relevance_score": 0.3},
            ],
            id="out-of-range-index",
        ),
        pytest.param(
            [
                {"index": 0, "relevance_score": 0.1},
                {"index": 2, "relevance_score": 0.3},
            ],
            id="partial-results",
        ),
    ],
)
def test_invalid_or_incomplete_index_contract_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    results: list[dict[str, Any]],
) -> None:
    _install_results(monkeypatch, results)

    scores = rerank_module.rerank("q", ["A", "B", "C"], {"endpoint": "https://rerank.invalid"})

    assert scores is None
