from __future__ import annotations

from v3core.tools.import_ import _is_importable_qa


def test_normal_import_pair_over_24k_is_preserved_for_derived_chunking():
    question = "正常用户问题 " + ("q" * 30_000)
    answer = "正常助手回答 " + ("a" * 50_000)
    assert _is_importable_qa(question, answer) is True


def test_import_noise_and_tool_only_rules_remain():
    assert _is_importable_qa("heartbeat please", "real answer") is False
    assert _is_importable_qa("normal question", "(tool call only)") is False
