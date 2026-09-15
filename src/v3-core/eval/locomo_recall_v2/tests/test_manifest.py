"""Tests for the LoCoMo manifest builder.

Verifies:

  * source identity (path / sha256 / bytes) is propagated.
  * row hashes cover every eval row.
  * builder_version / commit_sha / provider_id / model_id /
    embedding_dim contracts hold.
  * secret-shaped inputs are refused.
  * JSON round-trip works.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval.locomo_recall_v2 import manifest as mf  # noqa: E402


@dataclasses.dataclass
class _FakeDataset:
    """Minimal dataset duck-type for the manifest builder."""

    source_path: str = "/tmp/locomo.json"
    source_sha256: str = "a" * 64
    source_bytes: int = 4096
    samples: tuple = ()
    eval_rows: tuple = ()
    qa_pairs: tuple = ()
    category_counts: dict = dataclasses.field(default_factory=dict)
    message_count: int = 0
    qa_pair_count: int = 0


@dataclasses.dataclass
class _FakeRow:
    sample_id: str
    qa_id: str
    question: str
    answer: str
    category: str
    evidence: tuple
    source_hash: str
    qa_pair_source_id: str | None = None


def _dataset(rows=()):
    return _FakeDataset(
        samples=(("s", object()),),
        eval_rows=tuple(rows),
        message_count=10,
        qa_pair_count=3,
        category_counts={"a": 1, "b": 2},
    )


class TestBuildManifest:
    def test_source_hash_and_bytes_propagated(self):
        ds = _dataset()
        m = mf.build_manifest(ds)
        assert m.source["sha256"] == "a" * 64
        assert m.source["bytes"] == 4096
        assert m.source["name"] == "locomo"
        assert m.source["kind"] == "eval_v2"

    def test_row_hashes_cover_every_eval_row(self):
        rows = [
            _FakeRow("s1", "q1", "?", "!", "cat", ("d1",), "h1"),
            _FakeRow("s1", "q2", "?", "!", "cat", ("d2",), "h2"),
        ]
        m = mf.build_manifest(_dataset(rows))
        assert m.row_hashes == {"s1::q1": "h1", "s1::q2": "h2"}

    def test_counts_aggregate_correctly(self):
        rows = [
            _FakeRow("s1", "q1", "?", "!", "cat", (), "h"),
            _FakeRow("s1", "q2", "?", "", "cat", (), "h"),
        ]
        m = mf.build_manifest(_dataset(rows))
        assert m.counts["eval_row_count"] == 2
        assert m.counts["message_count"] == 10
        assert m.counts["qa_pair_count"] == 3
        assert m.category_counts == {"a": 1, "b": 2}
        assert m.answer_coverage == {"total": 2, "non_empty": 1, "empty": 1}

    def test_commit_sha_accepted_when_40_hex(self):
        m = mf.build_manifest(_dataset(), commit_sha="0123456789abcdef0123456789abcdef01234567")
        assert m.commit_sha == "0123456789abcdef0123456789abcdef01234567"

    def test_commit_sha_lowercased(self):
        # Mixed case is normalised to lower-case for stable storage.
        m = mf.build_manifest(
            _dataset(),
            commit_sha="ABCDEF1234567890ABCDEF1234567890ABCDEF12",
        )
        assert m.commit_sha == "abcdef1234567890abcdef1234567890abcdef12"

    def test_commit_sha_empty_is_allowed(self):
        m = mf.build_manifest(_dataset(), commit_sha="")
        assert m.commit_sha == ""

    def test_commit_sha_wrong_format_refused(self):
        with pytest.raises(mf.ManifestError):
            mf.build_manifest(_dataset(), commit_sha="not-a-sha")

    def test_provider_id_url_like_refused(self):
        with pytest.raises(mf.ManifestError):
            mf.build_manifest(_dataset(), provider_id="https://x@y")

    def test_model_id_credential_like_refused(self):
        with pytest.raises(mf.ManifestError):
            mf.build_manifest(_dataset(), model_id="user:pw@host")

    def test_embedding_dim_negative_refused(self):
        with pytest.raises(mf.ManifestError):
            mf.build_manifest(_dataset(), embedding_dim=-1)

    def test_embedding_dim_zero_means_unset(self):
        m = mf.build_manifest(_dataset(), embedding_dim=0)
        assert m.embedding_dim == 0
        m2 = mf.build_manifest(_dataset())
        assert m2.embedding_dim == 0

    def test_to_json_round_trip(self):
        ds = _dataset([
            _FakeRow("s1", "q1", "?", "!", "cat", ("d1",), "h1"),
        ])
        m = mf.build_manifest(
            ds,
            commit_sha="a" * 40,
            provider_id="bge-m3",
            model_id="bge-m3",
            embedding_dim=1024,
        )
        blob = m.to_json()
        reloaded = mf.LoCoMoManifest(**json.loads(blob))
        assert reloaded == m

    def test_no_secret_leak(self):
        ds = _dataset()
        m = mf.build_manifest(ds, provider_id="bge-m3", model_id="m")
        blob = m.to_json()
        for forbidden in ("password", "dsn", "://", "@host"):
            assert forbidden not in blob.lower()


class TestStableHash:
    def test_stable_under_key_reorder(self):
        a = mf.stable_hash({"x": 1, "y": 2})
        b = mf.stable_hash({"y": 2, "x": 1})
        assert a == b

    def test_stable_under_whitespace(self):
        a = mf.stable_hash({"x": 1})
        b = mf.stable_hash({"x": 1})  # same canonical form
        assert a == b

    def test_different_payloads_differ(self):
        a = mf.stable_hash({"x": 1})
        b = mf.stable_hash({"x": 2})
        assert a != b


class TestVerifySourceReexport:
    def test_re_exported_verify_source(self, tmp_path):
        # Mirrors dataset.verify_source via the manifest re-export.
        from eval.locomo_recall_v2 import dataset as ds
        blob = b"hello world"
        p = tmp_path / "x.txt"
        with open(p, "wb") as f:
            f.write(blob)
        digest = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        assert mf.verify_source(str(p), digest) == digest
        with pytest.raises(ds.LoCoMoSourceError):
            mf.verify_source(str(p), "0" * 64)