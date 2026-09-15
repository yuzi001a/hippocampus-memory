"""Tests for the LoCoMo eval_v2 loader.

These tests do NOT touch PG, the network, or any provider.
They build synthetic LoCoMo JSON in a tmp_path, run the
loader, and assert on the in-memory dataset.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

# Make ``src/v3-core`` importable so ``eval.locomo_recall_v2``
# resolves without installing the package.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
for _p in (_PKG_ROOT, os.path.join(_PKG_ROOT, "src"), _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval.locomo_recall_v2 import dataset as ds  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_locomo(path: str, payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    with open(path, "wb") as f:
        f.write(blob)
    return hashlib.sha256(blob).hexdigest()


def _sample_basic() -> dict:
    """Two samples, each with two numeric sessions, evidence + qa."""

    s1_session_1_msgs = [
        {"dia_id": "d_1", "speaker": "speaker_1", "text": "Hello there"},
        {"dia_id": "d_2", "speaker": "speaker_2", "text": "Hi! How are you?"},
        {"dia_id": "d_3", "speaker": "speaker_1", "text": "Doing well, thanks."},
        {"dia_id": "d_4", "speaker": "speaker_2", "text": "Glad to hear."},
    ]
    s1_session_2_msgs = [
        {"dia_id": "d_5", "speaker": "speaker_1", "text": "Friday plans?"},
        {"dia_id": "d_6", "speaker": "speaker_2", "text": "Hiking, want to come?"},
    ]
    s2_session_1_msgs = [
        {"dia_id": "d_7", "speaker": "speaker_1", "text": "Project update"},
        {"dia_id": "d_8", "speaker": "speaker_2", "text": "Sprint done?"},
    ]
    return {
        "samples": [
            {
                "sample_id": "sample-1",
                "conversation": {
                    "session_1": {
                        "date_time": "2024-01-01 10:00:00",
                        "messages": s1_session_1_msgs,
                    },
                    "session_2": {
                        "date_time": "2024-01-02 09:00:00",
                        "messages": s1_session_2_msgs,
                    },
                },
                "qa": [
                    {
                        "id": "d_3",
                        "question": "How are you?",
                        "answer": "Doing well, thanks.",
                        "category": "social",
                        "evidence": ["d_3"],
                    },
                    {
                        "id": "d_5",
                        "question": "Friday plans?",
                        "answer": "Hiking.",
                        "category": "plans",
                        "evidence": ["d_5", "d_6"],
                    },
                ],
            },
            {
                "sample_id": "sample-2",
                "conversation": {
                    "session_1": {
                        "date_time": "2024-02-01 11:00:00",
                        "messages": s2_session_1_msgs,
                    },
                },
                "qa": [
                    {
                        "id": "d_8",
                        "question": "Sprint status?",
                        "answer": "Done.",
                        "category": "work",
                        "evidence": ["d_8"],
                    },
                ],
            },
        ]
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVerifySource:
    def test_sha256_matches(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        assert ds.verify_source(str(p), digest) == digest

    def test_sha256_mismatch_raises(self, tmp_path):
        p = tmp_path / "locomo.json"
        _write_locomo(str(p), _sample_basic())
        with pytest.raises(ds.LoCoMoSourceError):
            ds.verify_source(str(p), "0" * 64)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ds.LoCoMoSourceError):
            ds.verify_source(str(tmp_path / "nope.json"), "a" * 64)

    def test_invalid_expected_format(self, tmp_path):
        p = tmp_path / "locomo.json"
        _write_locomo(str(p), _sample_basic())
        with pytest.raises(ds.LoCoMoSourceError):
            ds.verify_source(str(p), "not-a-digest")


class TestLoaderIdentity:
    def test_load_returns_sorted_samples(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        ids = [sid for sid, _ in d.samples]
        assert ids == sorted(ids)

    def test_session_ordering_is_numeric(self, tmp_path):
        # Sessions out of order in the JSON should be sorted numerically.
        raw = {
            "samples": [
                {
                    "sample_id": "s",
                    "conversation": {
                        "session_10": {"date_time": "2024-01-10 10:00:00",
                                        "messages": [
                                            {"dia_id": "d_10", "speaker": "speaker_1",
                                             "text": "x"},
                                            {"dia_id": "d_11", "speaker": "speaker_2",
                                             "text": "y"},
                                        ]},
                        "session_2":  {"date_time": "2024-01-02 10:00:00",
                                        "messages": [
                                            {"dia_id": "d_2", "speaker": "speaker_1",
                                             "text": "a"},
                                            {"dia_id": "d_3", "speaker": "speaker_2",
                                             "text": "b"},
                                        ]},
                    },
                    "qa": [],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        d = ds.load_locomo(str(p), digest)
        _sample, = d.samples
        sid, sample = _sample
        keys = [k for k, _ in sample.sessions]
        assert keys == ["session_2", "session_10"]

    def test_qa_pair_index_stable_source_id(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        # We expect: sample-1 session_1: (d_1>d_2), (d_3>d_4)
        #            sample-1 session_2: (d_5>d_6)
        #            sample-2 session_1: (d_7>d_8)
        pair_ids = [pair.source_id for pair in d.qa_pairs]
        assert "locomo|eval_v2|sample-1|session_1|d_1>d_2" in pair_ids
        assert "locomo|eval_v2|sample-1|session_1|d_3>d_4" in pair_ids
        assert "locomo|eval_v2|sample-1|session_2|d_5>d_6" in pair_ids
        assert "locomo|eval_v2|sample-2|session_1|d_7>d_8" in pair_ids

    def test_qa_pair_provenance_carries_both_dia_ids(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        first = d.qa_pairs[0]
        assert first.q_provenance["dia_id"] == "d_1"
        assert first.a_provenance["dia_id"] == "d_2"
        assert first.q_provenance["sample_id"] == "sample-1"
        assert first.q_provenance["session_key"] == "session_1"

    def test_messages_carry_image_fields(self, tmp_path):
        raw = {
            "samples": [
                {
                    "sample_id": "img-sample",
                    "conversation": {
                        "session_1": {
                            "date_time": "2024-01-01 10:00:00",
                            "messages": [
                                {"dia_id": "d_img_1", "speaker": "speaker_1",
                                 "text": "Look at this",
                                 "img_url": "images/foo.png",
                                 "blip_caption": "a picture of a cat",
                                 "query": "what is in the image",
                                 "re-download": False},
                                {"dia_id": "d_img_2", "speaker": "speaker_2",
                                 "text": "Nice!"},
                            ],
                        },
                    },
                    "qa": [],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        d = ds.load_locomo(str(p), digest)
        _sid, sample = d.samples[0]
        _skey, msgs = sample.sessions[0]
        m1 = msgs[0]
        assert m1.img_url == "images/foo.png"
        assert m1.blip_caption == "a picture of a cat"
        assert m1.img_query == "what is in the image"
        assert m1.re_download is False
        # Non-image message keeps None fields.
        assert msgs[1].img_url is None
        assert msgs[1].blip_caption is None
        assert msgs[1].img_query is None
        assert msgs[1].re_download is None

    def test_message_text_not_truncated(self, tmp_path):
        # The loader must NEVER silently truncate. We set a
        # 1024-byte cap and feed a 2048-byte text — the load
        # must fail closed, not return a truncated message.
        long_text = "x" * 2048
        raw = {
            "samples": [
                {
                    "sample_id": "big",
                    "conversation": {
                        "session_1": {
                            "date_time": "2024-01-01 10:00:00",
                            "messages": [
                                {"dia_id": "d_big", "speaker": "speaker_1",
                                 "text": long_text},
                            ],
                        },
                    },
                    "qa": [],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        with pytest.raises(ds.LoCoMoInputLimitExceeded):
            ds.load_locomo(str(p), digest, max_message_bytes=1024)

    def test_session_datetime_parsed_utc(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        _sid, sample = d.samples[0]
        _skey, msgs = sample.sessions[0]
        m = msgs[0]
        assert m.raw_session_dt == "2024-01-01 10:00:00"
        # The UTC timestamp is timezone-aware and equals
        # 2024-01-01T10:00:00Z.
        from datetime import datetime, timezone
        assert m.session_dt_utc == datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)


class TestEvalRows:
    def test_eval_row_preserves_question_answer_category_evidence(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        er = next(r for r in d.eval_rows if r.qa_id == "d_5")
        assert er.question == "Friday plans?"
        assert er.answer == "Hiking."
        assert er.category == "plans"
        assert er.evidence == ("d_5", "d_6")
        assert er.source_hash  # non-empty SHA-256

    def test_eval_row_source_hash_stable(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d1 = ds.load_locomo(str(p), digest)
        d2 = ds.load_locomo(str(p), digest)
        h1 = {r.qa_id: r.source_hash for r in d1.eval_rows}
        h2 = {r.qa_id: r.source_hash for r in d2.eval_rows}
        assert h1 == h2

    def test_eval_row_qa_pair_source_id_link(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        # qa id d_5 → evidence d_5+d_6 → pair d_5>d_6 in session_2.
        er = next(r for r in d.eval_rows if r.qa_id == "d_5")
        assert er.qa_pair_source_id == "locomo|eval_v2|sample-1|session_2|d_5>d_6"

    def test_linked_qa_pair_carries_eval_question_answer(self, tmp_path):
        # When an eval row links to a pair via ``id == q_dia_id``,
        # the pair must back-fill its ``question`` / ``answer``
        # text and ``timestamp`` from the eval row.
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        linked = next(
            pair for pair in d.qa_pairs
            if pair.source_id == "locomo|eval_v2|sample-1|session_2|d_5>d_6"
        )
        assert linked.question == "Friday plans?"
        assert linked.answer == "Hiking."
        from datetime import datetime, timezone
        assert linked.timestamp == datetime(2024, 1, 2, 9, 0, tzinfo=timezone.utc)
        # Unlinked pairs keep empty text + sentinel UTC.
        unlinked = next(
            pair for pair in d.qa_pairs
            if pair.source_id == "locomo|eval_v2|sample-1|session_1|d_1>d_2"
        )
        assert unlinked.question == ""
        assert unlinked.answer == ""

    def test_category_counts(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        assert d.category_counts == {"plans": 1, "social": 1, "work": 1}


class TestEvidenceMap:
    def test_evidence_map_contains_all_messages(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        emap = ds.build_evidence_map(d)
        # Every preserved message must appear.
        for m in ds.iter_messages(d):
            assert (m.sample_id, m.session_key, m.dia_id) in emap

    def test_evidence_map_pairs_q_and_a(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        emap = ds.build_evidence_map(d)
        # d_1, d_2 are covered by pair d_1>d_2 in sample-1/session_1.
        pair_id = "locomo|eval_v2|sample-1|session_1|d_1>d_2"
        assert emap[("sample-1", "session_1", "d_1")] == [pair_id]
        assert emap[("sample-1", "session_1", "d_2")] == [pair_id]

    def test_unmapped_dia_ids_kept_explicit(self, tmp_path):
        # d_5 is part of pair d_5>d_6, but d_6 is also covered.
        # We construct a fixture where one message is NOT in any
        # pair: an odd-length session.
        raw = {
            "samples": [
                {
                    "sample_id": "s",
                    "conversation": {
                        "session_1": {
                            "date_time": "2024-01-01 10:00:00",
                            "messages": [
                                {"dia_id": "a", "speaker": "speaker_1", "text": "x"},
                                {"dia_id": "b", "speaker": "speaker_2", "text": "y"},
                                {"dia_id": "c", "speaker": "speaker_1",
                                 "text": "trailing unpaired"},
                            ],
                        }
                    },
                    "qa": [],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        d = ds.load_locomo(str(p), digest)
        emap = ds.build_evidence_map(d)
        assert emap[("s", "session_1", "a")] == [
            "locomo|eval_v2|s|session_1|a>b"
        ]
        assert emap[("s", "session_1", "b")] == [
            "locomo|eval_v2|s|session_1|a>b"
        ]
        assert emap[("s", "session_1", "c")] == []   # unmapped → explicit

    def test_resolve_gold_evidence_no_fuzzy_match(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        emap = ds.build_evidence_map(d)
        er = next(r for r in d.eval_rows if r.qa_id == "d_5")
        gold = ds.resolve_gold_evidence(d, er, emap)
        assert gold.source_ids == ("locomo|eval_v2|sample-1|session_2|d_5>d_6",)
        assert gold.unmapped_dia_ids == ()

    def test_resolve_gold_evidence_records_unmapped(self, tmp_path):
        raw = {
            "samples": [
                {
                    "sample_id": "s",
                    "conversation": {
                        "session_1": {
                            "date_time": "2024-01-01 10:00:00",
                            "messages": [
                                {"dia_id": "a", "speaker": "speaker_1", "text": "x"},
                                {"dia_id": "b", "speaker": "speaker_2", "text": "y"},
                            ],
                        }
                    },
                    "qa": [
                        {
                            "id": "q-bad",
                            "question": "?",
                            "answer": "!",
                            "category": "x",
                            "evidence": ["a", "ghost_dia"],
                        }
                    ],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        d = ds.load_locomo(str(p), digest)
        emap = ds.build_evidence_map(d)
        er = next(iter(d.eval_rows))
        gold = ds.resolve_gold_evidence(d, er, emap)
        # Only ``a`` maps; ``ghost_dia`` is unmapped.
        assert gold.source_ids == ("locomo|eval_v2|s|session_1|a>b",)
        assert gold.unmapped_dia_ids == ("ghost_dia",)


class TestSourceID:
    def test_make_source_id_format(self):
        sid = ds.make_source_id("s", "session_1", "q", "a")
        assert sid == "locomo|eval_v2|s|session_1|q>a"

    def test_trailing_unpaired_message_not_promoted(self, tmp_path):
        raw = {
            "samples": [
                {
                    "sample_id": "s",
                    "conversation": {
                        "session_1": {
                            "date_time": "2024-01-01 10:00:00",
                            "messages": [
                                {"dia_id": "a", "speaker": "speaker_1", "text": "x"},
                                {"dia_id": "b", "speaker": "speaker_2", "text": "y"},
                                {"dia_id": "c", "speaker": "speaker_1", "text": "z"},
                            ],
                        }
                    },
                    "qa": [],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        d = ds.load_locomo(str(p), digest)
        assert d.qa_pair_count == 1
        assert d.qa_pairs[0].source_id == "locomo|eval_v2|s|session_1|a>b"


class TestLoaderShape:
    def test_top_level_list_accepted(self, tmp_path):
        raw = _sample_basic()["samples"]   # list form
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        d = ds.load_locomo(str(p), digest)
        assert len(d.samples) == 2

    def test_malformed_session_raises(self, tmp_path):
        raw = {
            "samples": [
                {
                    "sample_id": "s",
                    "conversation": {
                        "session_1": {"messages": [
                            "not a dict",
                        ]},
                    },
                    "qa": [],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        with pytest.raises(ds.LoCoMoSourceError):
            ds.load_locomo(str(p), digest)

    def test_missing_dia_id_raises(self, tmp_path):
        raw = {
            "samples": [
                {
                    "sample_id": "s",
                    "conversation": {
                        "session_1": {
                            "date_time": "2024-01-01 10:00:00",
                            "messages": [
                                {"speaker": "speaker_1", "text": "x"},
                                {"speaker": "speaker_2", "text": "y"},
                            ],
                        }
                    },
                    "qa": [],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        with pytest.raises(ds.LoCoMoSourceError):
            ds.load_locomo(str(p), digest)


# ---------------------------------------------------------------------------
# Authoritative historical old-shape fixtures + parse + build_import_rows
# ---------------------------------------------------------------------------


def _sample_old_shape() -> dict:
    """Authoritative historical LoCoMo old-shape dump.

    Two samples with list-valued ``session_N`` and sibling
    ``session_N_date_time`` keys. QA rows have NO ``id`` field —
    the stable identity is the within-sample ``query_idx``.
    Native datetime strings ("1:56 pm on 8 May, 2023"). Image
    fields use the historical names ``img_url`` /
    ``blip_caption`` / ``query`` / ``re-download``.
    """

    return {
        "samples": [
            {
                "sample_id": "sample-old-A",
                "session_1_date_time": "1:56 pm on 8 May, 2023",
                "session_2_date_time": "8 May, 2023",
                "conversation": {
                    "session_1": [
                        {"dia_id": "A1", "speaker": "speaker_1",
                         "text": "Morning!", "img_url": "img-A1.png",
                         "blip_caption": "sunrise",
                         "query": "what is in the picture",
                         "re-download": False},
                        {"dia_id": "A2", "speaker": "speaker_2",
                         "text": "Hey, how's it going?"},
                        {"dia_id": "A3", "speaker": "speaker_1",
                         "text": "Good, you?"},
                        {"dia_id": "A4", "speaker": "speaker_2",
                         "text": "Great."},
                    ],
                    "session_2": [
                        {"dia_id": "A5", "speaker": "speaker_1",
                         "text": "Lunch?"},
                        {"dia_id": "A6", "speaker": "speaker_2",
                         "text": "Sure, at noon."},
                    ],
                },
                "qa": [
                    {"question": "How did A start the day?",
                     "answer": "Morning!",
                     "category": "social",
                     "evidence": ["A1", "A2"]},
                    {"question": "What was the lunch plan?",
                     "answer": "At noon.",
                     "category": "plans",
                     "evidence": ["A5", "A6"]},
                ],
            },
            {
                "sample_id": "sample-old-B",
                "session_1_date_time": "May 9, 2023 1:56 pm",
                "conversation": {
                    "session_1": [
                        {"dia_id": "B1", "speaker": "speaker_1",
                         "text": "Project update"},
                        {"dia_id": "B2", "speaker": "speaker_2",
                         "text": "Sprint done?"},
                        {"dia_id": "B3", "speaker": "speaker_1",
                         "text": "Almost done."},
                    ],
                },
                "qa": [
                    {"question": "Sprint status?",
                     "answer": "Almost done.",
                     "category": "work",
                     "evidence": ["B2", "B3"]},
                ],
            },
        ]
    }


class TestOldShapeFixture:
    def test_old_shape_loads_without_error(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_old_shape())
        d = ds.load_locomo(str(p), digest)
        # Two samples: 4 + 2 + 3 = 9 preserved messages (including
        # the odd B3 tail).
        assert d.message_count == 9
        # (4-msg session → 2 pairs) + (2-msg session → 1 pair) +
        # (3-msg session → 1 pair) = 4 QA pairs.
        assert d.qa_pair_count == 4

    def test_old_shape_datetime_parsed_not_sentinel(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_old_shape())
        d = ds.load_locomo(str(p), digest)
        from datetime import datetime, timezone
        # The first session's native string "1:56 pm on 8 May, 2023"
        # must parse to 2023-05-08 13:56 UTC — NOT the 1970 sentinel.
        for sid, sample in d.samples:
            for skey, msgs in sample.sessions:
                m = msgs[0]
                assert m.session_dt_utc != datetime(1970, 1, 1, tzinfo=timezone.utc), (
                    f"sample {sid!r} session {skey!r} parsed to sentinel"
                )
                # All messages in the same session share the parsed UTC.
                for mm in msgs:
                    assert mm.session_dt_utc == m.session_dt_utc

    def test_old_shape_naive_treated_as_utc(self, tmp_path):
        # "8 May, 2023" is date-only — must parse to UTC midnight
        # and NOT be silently guessed as local time.
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_old_shape())
        d = ds.load_locomo(str(p), digest)
        from datetime import datetime, timezone
        for sid, sample in d.samples:
            for skey, msgs in sample.sessions:
                m = msgs[0]
                if skey == "session_2" and sid == "sample-old-A":
                    assert m.session_dt_utc == datetime(
                        2023, 5, 8, tzinfo=timezone.utc
                    )

    def test_old_shape_query_idx_stable(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_old_shape())
        d = ds.load_locomo(str(p), digest)
        # Within-sample order is preserved.
        a_rows = [r for r in d.eval_rows if r.sample_id == "sample-old-A"]
        assert [r.query_idx for r in a_rows] == [0, 1]
        # qa_id is derived from query_idx when the source omits id.
        assert [r.qa_id for r in a_rows] == ["q0", "q1"]
        b_rows = [r for r in d.eval_rows if r.sample_id == "sample-old-B"]
        assert [r.query_idx for r in b_rows] == [0]
        assert b_rows[0].qa_id == "q0"

    def test_old_shape_image_fields_preserved(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_old_shape())
        d = ds.load_locomo(str(p), digest)
        for sid, sample in d.samples:
            for _skey, msgs in sample.sessions:
                for m in msgs:
                    if m.dia_id == "A1":
                        assert m.img_url == "img-A1.png"
                        assert m.blip_caption == "sunrise"
                        assert m.img_query == "what is in the picture"
                        assert m.re_download is False

    def test_old_shape_session_sort_numeric(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_old_shape())
        d = ds.load_locomo(str(p), digest)
        for _sid, sample in d.samples:
            keys = [k for k, _ in sample.sessions]
            # numeric order; ``session_1`` precedes ``session_2``.
            assert keys == sorted(keys, key=lambda k: int(k.split("_")[-1]))


class TestBuildImportRows:
    def test_import_rows_three_lists_present(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_old_shape())
        d = ds.load_locomo(str(p), digest)
        rows = ds.build_import_rows(d)
        assert set(rows) == {"qa_pairs", "conversation_stream", "eval_queries"}

    def test_import_rows_qa_pairs_full_text(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_old_shape())
        d = ds.load_locomo(str(p), digest)
        rows = ds.build_import_rows(d)
        # qa_pairs: every QA pair has full question/answer text and
        # an explicit canonical source_id.
        assert rows["qa_pairs"], "qa_pairs must be non-empty"
        # Collect every q-text / a-text seen across pairs; every
        # preserved message must appear at least once.
        all_q_text = [r["question"] for r in rows["qa_pairs"]]
        all_a_text = [r["answer"] for r in rows["qa_pairs"]]
        joined = "\n".join(all_q_text + all_a_text)
        # Image caption / query / url markers survive.
        assert "Morning!" in joined
        assert "Hey, how's it going?" in joined
        assert "Lunch?" in joined
        assert "Sure, at noon." in joined
        for r in rows["qa_pairs"]:
            assert r["source_id"].startswith("locomo|eval_v2|")
            assert ">" in r["source_id"]
            assert r["session_id"].startswith("locomo-eval_v2-")
            assert isinstance(r["turn_id"], int)
            assert r["source"] == "locomo"
            assert r["embedding"] is None
            assert r["embed_model"] == ""
            # tool_calls carries both q_provenance and a_provenance.
            tc = r["tool_calls"][0]
            assert "q_provenance" in tc and "a_provenance" in tc
            assert tc["q_provenance"]["dia_id"] in {"A1", "A3", "A5", "B1"}
            assert tc["a_provenance"]["dia_id"] in {"A2", "A4", "A6", "B2"}

    def test_import_rows_conversation_stream_includes_odd_tail(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_old_shape())
        d = ds.load_locomo(str(p), digest)
        rows = ds.build_import_rows(d)
        cs = rows["conversation_stream"]
        # 9 preserved messages (including the odd B3 tail).
        assert len(cs) == 9
        # Every row carries a tool_calls list with provenance.
        for r in cs:
            assert r["trigger"] == "locomo_eval_v2"
            assert r["source"] == "locomo_eval_v2"
            assert isinstance(r["turn_id"], int)
            assert r["embedding"] is None
            assert isinstance(r["tool_calls"], list) and r["tool_calls"]
            prov = r["tool_calls"][0]
            assert prov["sample_id"]
            assert prov["session_key"]
            assert prov["dia_id"]
            assert prov["speaker"]
        # Role mapping: speaker_1 → user, speaker_2 → assistant.
        roles = [r["role"] for r in cs]
        assert "user" in roles and "assistant" in roles

    def test_import_rows_image_provenance_present(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_old_shape())
        d = ds.load_locomo(str(p), digest)
        rows = ds.build_import_rows(d)
        cs = rows["conversation_stream"]
        # The A1 row carries an image provenance dict.
        a1 = next(r for r in cs
                  if r["tool_calls"][0]["dia_id"] == "A1")
        assert a1["tool_calls"][0]["image"]["present"] is True
        assert a1["tool_calls"][0]["image"]["img_url"] == "img-A1.png"

    def test_import_rows_eval_queries_explicit_source_id(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_old_shape())
        d = ds.load_locomo(str(p), digest)
        rows = ds.build_import_rows(d)
        eq = rows["eval_queries"]
        # 3 eval questions total.
        assert len(eq) == 3
        # Each row carries the full question/answer text and the
        # explicit canonical source_id.
        for r in eq:
            assert r["question"]
            assert r["answer"]
            assert r["source_id"] == (
                f"locomo|eval_v2|{r['sample_id']}|q{r['query_idx']}"
            )
            assert r["qa_id"] == f"q{r['query_idx']}"
            assert r["source_hash"]

    def test_import_rows_sample_ids_subset(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_old_shape())
        d = ds.load_locomo(str(p), digest)
        rows = ds.build_import_rows(d, sample_ids={"sample-old-A"})
        # Only sample-old-A messages / pairs / eval rows.
        assert all(
            r["session_id"].startswith("locomo-eval_v2-sample-old-A-")
            for r in rows["conversation_stream"]
        )
        assert all(r["sample_id"] == "sample-old-A" for r in rows["eval_queries"])


class TestEvidenceMap:
    def test_evidence_map_contains_all_messages(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        emap = ds.build_evidence_map(d)
        # Every preserved message must appear.
        for m in ds.iter_messages(d):
            assert (m.sample_id, m.session_key, m.dia_id) in emap

    def test_evidence_map_pairs_q_and_a(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        emap = ds.build_evidence_map(d)
        # d_1, d_2 are covered by pair d_1>d_2 in sample-1/session_1.
        pair_id = "locomo|eval_v2|sample-1|session_1|d_1>d_2"
        assert emap[("sample-1", "session_1", "d_1")] == [pair_id]
        assert emap[("sample-1", "session_1", "d_2")] == [pair_id]

    def test_unmapped_dia_ids_kept_explicit(self, tmp_path):
        # d_5 is part of pair d_5>d_6, but d_6 is also covered.
        # We construct a fixture where one message is NOT in any
        # pair: an odd-length session.
        raw = {
            "samples": [
                {
                    "sample_id": "s",
                    "conversation": {
                        "session_1": {
                            "date_time": "2024-01-01 10:00:00",
                            "messages": [
                                {"dia_id": "a", "speaker": "speaker_1", "text": "x"},
                                {"dia_id": "b", "speaker": "speaker_2", "text": "y"},
                                {"dia_id": "c", "speaker": "speaker_1",
                                 "text": "trailing unpaired"},
                            ],
                        }
                    },
                    "qa": [],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        d = ds.load_locomo(str(p), digest)
        emap = ds.build_evidence_map(d)
        assert emap[("s", "session_1", "a")] == [
            "locomo|eval_v2|s|session_1|a>b"
        ]
        assert emap[("s", "session_1", "b")] == [
            "locomo|eval_v2|s|session_1|a>b"
        ]
        assert emap[("s", "session_1", "c")] == []   # unmapped → explicit

    def test_resolve_gold_evidence_no_fuzzy_match(self, tmp_path):
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), _sample_basic())
        d = ds.load_locomo(str(p), digest)
        emap = ds.build_evidence_map(d)
        er = next(r for r in d.eval_rows if r.qa_id == "d_5")
        gold = ds.resolve_gold_evidence(d, er, emap)
        assert gold.source_ids == ("locomo|eval_v2|sample-1|session_2|d_5>d_6",)
        assert gold.unmapped_dia_ids == ()

    def test_resolve_gold_evidence_records_unmapped(self, tmp_path):
        raw = {
            "samples": [
                {
                    "sample_id": "s",
                    "conversation": {
                        "session_1": {
                            "date_time": "2024-01-01 10:00:00",
                            "messages": [
                                {"dia_id": "a", "speaker": "speaker_1", "text": "x"},
                                {"dia_id": "b", "speaker": "speaker_2", "text": "y"},
                            ],
                        }
                    },
                    "qa": [
                        {
                            "id": "q-bad",
                            "question": "?",
                            "answer": "!",
                            "category": "x",
                            "evidence": ["a", "ghost_dia"],
                        }
                    ],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        d = ds.load_locomo(str(p), digest)
        emap = ds.build_evidence_map(d)
        er = next(iter(d.eval_rows))
        gold = ds.resolve_gold_evidence(d, er, emap)
        # Only ``a`` maps; ``ghost_dia`` is unmapped.
        assert gold.source_ids == ("locomo|eval_v2|s|session_1|a>b",)
        assert gold.unmapped_dia_ids == ("ghost_dia",)
        assert gold.unresolved == ()

    def test_resolve_gold_evidence_preserves_compound_unresolved(self, tmp_path):
        # A compound / malformed evidence entry (whitespace,
        # embedded comma) must surface as ``unresolved`` verbatim
        # and NEVER be split heuristically.
        raw = {
            "samples": [
                {
                    "sample_id": "s",
                    "conversation": {
                        "session_1": {
                            "date_time": "2024-01-01 10:00:00",
                            "messages": [
                                {"dia_id": "a", "speaker": "speaker_1", "text": "x"},
                                {"dia_id": "b", "speaker": "speaker_2", "text": "y"},
                            ],
                        }
                    },
                    "qa": [
                        {
                            "id": "q-bad",
                            "question": "?",
                            "answer": "!",
                            "category": "x",
                            "evidence": ["a b", "c, d"],
                        }
                    ],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        d = ds.load_locomo(str(p), digest)
        emap = ds.build_evidence_map(d)
        er = next(iter(d.eval_rows))
        gold = ds.resolve_gold_evidence(d, er, emap)
        assert gold.unresolved == ("a b", "c, d")
        # The canonical ``a`` is still picked up via the fallback
        # scan because the constrained session lookup is gated on
        # the row's qa_pair_source_id being non-None; this row
        # has qa_id="q-bad" so no link exists.
        assert gold.source_ids == ()


class TestSourceID:
    def test_make_source_id_format(self):
        sid = ds.make_source_id("s", "session_1", "q", "a")
        assert sid == "locomo|eval_v2|s|session_1|q>a"

    def test_parse_source_id_round_trip(self):
        sid = ds.make_source_id("sample-X", "session_3", "q_1", "a_2")
        parsed = ds.parse_source_id(sid)
        assert parsed.sample_id == "sample-X"
        assert parsed.session_key == "session_3"
        assert parsed.q_dia_id == "q_1"
        assert parsed.a_dia_id == "a_2"
        assert parsed.source_id == sid

    def test_parse_source_id_rejects_non_canonical(self):
        with pytest.raises(ValueError):
            ds.parse_source_id("locomo|not_eval|s|session_1|q>a")
        with pytest.raises(ValueError):
            ds.parse_source_id("locomo|eval_v2|s|session_1|q")  # no >a
        with pytest.raises(ValueError):
            ds.parse_source_id("locomo|eval_v2|s|session_1|q>a|b")  # extra
        with pytest.raises(ValueError):
            ds.parse_source_id("")
        with pytest.raises(ValueError):
            ds.parse_source_id(None)  # type: ignore[arg-type]
        # Field containing the pipe separator is rejected.
        with pytest.raises(ValueError):
            ds.parse_source_id("locomo|eval_v2|s|session_1|p|pe>a")
        # Field containing the literal ``>`` separator is rejected.
        with pytest.raises(ValueError):
            ds.parse_source_id("locomo|eval_v2|s|session_1|q>>a")

    def test_evidence_lookup_with_session_key(self):
        emap = {
            ("s", "session_1", "a"): ["src-a"],
            ("s", "session_2", "a"): ["src-a2"],
        }
        assert ds.evidence_lookup(emap, "s", "session_1", "a") == ["src-a"]
        assert ds.evidence_lookup(emap, "s", "session_2", "a") == ["src-a2"]

    def test_evidence_lookup_fallback_scans_all_sessions(self):
        emap = {
            ("s", "session_1", "a"): ["src-a1"],
            ("s", "session_2", "a"): ["src-a2"],
        }
        # No session_key → union across every session.
        assert ds.evidence_lookup(emap, "s", None, "a") == ["src-a1", "src-a2"]

    def test_evidence_lookup_unknown_returns_empty(self):
        emap = {("s", "session_1", "a"): ["src-a"]}
        assert ds.evidence_lookup(emap, "s", "session_1", "ghost") == []

    def test_trailing_unpaired_message_not_promoted(self, tmp_path):
        raw = {
            "samples": [
                {
                    "sample_id": "s",
                    "conversation": {
                        "session_1": {
                            "date_time": "2024-01-01 10:00:00",
                            "messages": [
                                {"dia_id": "a", "speaker": "speaker_1", "text": "x"},
                                {"dia_id": "b", "speaker": "speaker_2", "text": "y"},
                                {"dia_id": "c", "speaker": "speaker_1", "text": "z"},
                            ],
                        }
                    },
                    "qa": [],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        d = ds.load_locomo(str(p), digest)
        assert d.qa_pair_count == 1
        assert d.qa_pairs[0].source_id == "locomo|eval_v2|s|session_1|a>b"
        # But the odd c is still preserved as a conversation message.
        for _sid, sample in d.samples:
            for _skey, msgs in sample.sessions:
                dia_ids = [m.dia_id for m in msgs]
                assert dia_ids == ["a", "b", "c"]


class TestLoaderShape:
    def test_top_level_list_accepted(self, tmp_path):
        raw = _sample_basic()["samples"]   # list form
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        d = ds.load_locomo(str(p), digest)
        assert len(d.samples) == 2

    def test_malformed_session_raises(self, tmp_path):
        raw = {
            "samples": [
                {
                    "sample_id": "s",
                    "conversation": {
                        "session_1": {"messages": [
                            "not a dict",
                        ]},
                    },
                    "qa": [],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        with pytest.raises(ds.LoCoMoSourceError):
            ds.load_locomo(str(p), digest)

    def test_missing_dia_id_raises(self, tmp_path):
        raw = {
            "samples": [
                {
                    "sample_id": "s",
                    "conversation": {
                        "session_1": {
                            "date_time": "2024-01-01 10:00:00",
                            "messages": [
                                {"speaker": "speaker_1", "text": "x"},
                                {"speaker": "speaker_2", "text": "y"},
                            ],
                        }
                    },
                    "qa": [],
                }
            ]
        }
        p = tmp_path / "locomo.json"
        digest = _write_locomo(str(p), raw)
        with pytest.raises(ds.LoCoMoSourceError):
            ds.load_locomo(str(p), digest)