"""session_policy — 零 A 会话四档处置的确定性判据。

关键约束（来自用户决定）：
  * 四档只决定"是否参与 QA/提炼"，**不删任何原始记录**；
  * 判据是结构信号，不是内容语义、不是长度阈值、不是中文关键词；
  * 核验语料得到的 1,635 / 1,528 / 2,546 / 3,090 只是那次语料的结果，
    不得写死成比例——这里没有任何常量比例；
  * 来源本身缺用户侧（chat_md）不能因为零 A 被判负价值，也不能补造指令。
"""
from __future__ import annotations

from v3core.session_policy import (
    KNOWN_INCOMPLETE_SOURCES,
    SessionSignals,
    classify_session,
    tier_counts,
)


def _sig(**kw):
    base = dict(session_id="s", host="hermes")
    base.update(kw)
    return SessionSignals(**base)


def test_human_question_with_answer_is_keep():
    assert classify_session(_sig(raw_n=10, human_user_n=3, qa_n=3))[0] == "keep"


def test_qa_without_human_question_is_downgrade_not_dropped():
    """自动化包装出来的问答要保留但降权，不能因为零 A 直接丢。"""
    tier, reason = classify_session(_sig(raw_n=20, human_user_n=0, qa_n=2))
    assert tier == "downgrade"
    assert reason == "no_human_question_but_qa_exists"


def test_assistant_only_session_keeps_raw():
    tier, reason = classify_session(_sig(raw_n=8, human_user_n=0, assistant_n=8, qa_n=0))
    assert tier == "raw_only"
    assert reason == "no_qa_but_substantive_assistant_output"


def test_machine_only_session_is_excluded_from_derivation():
    tier, _ = classify_session(_sig(raw_n=6, human_user_n=0, machine_n=6, assistant_n=0, qa_n=0))
    assert tier == "exclude"


def test_incomplete_source_is_never_judged_by_zero_a():
    """chat_md 的导出本身没带用户侧：只留原文 + 标记不完整。"""
    sig = _sig(raw_n=9, host="chat_md", human_user_n=0, assistant_n=9, qa_n=0)
    assert sig.source_incomplete is True
    tier, reason = classify_session(sig)
    assert tier == "raw_only"
    assert reason == "source_missing_user_side"


def test_no_hardcoded_ratio_only_counts():
    rows = [("keep", "a")] * 3 + [("exclude", "b")] * 97
    counts = tier_counts(rows)
    assert counts["by_tier"]["keep"] == 3
    assert counts["by_tier"]["exclude"] == 97
    assert counts["total"] == 100
    assert set(KNOWN_INCOMPLETE_SOURCES) == {"chat_md"}
