"""主题卡写入层两个断链的回归 (2026-11-12)。

背景: 派生层抽查发现两件事 —
  ① 主题卡 body 是滚动追加的, LLM 每轮会把上一版印里已有的事实换种说法再给一遍,
     代码原样 append → 同一件事在 3000 字上限以内被反复写 (实测某卡 13 段里 2 段同义)。
  ② new_facts 落 topic_entries 时 (source='obs:fact:{note_id}') 只复用 QA 向量,
     把已经解析出来的 [#N] 引用丢掉了 → 条目回不到具体 QA, 只能回到印。

契约:
  - 去重只做字面级判断 (归一化后包含), 不做语义裁判; 拿不准的一律保留。
  - 恰好一条 [#N] 引用 → 写 source_qa_id; 多条 → 不伪造单一来源, 写 source 后缀
    `:qa=12,37`; 无引用 → 保持空。
"""
from __future__ import annotations

from v3core import observer as obs


def _qa(ids):
    """构造 qa_rows (只用到 id 字段)。"""
    return [{"id": i} for i in ids]


# ── 归一化与去重 ──────────────────────────────────────────────────────────


def test_norm_fact_text_strips_space_and_punctuation():
    assert obs._norm_fact_text("BGE-M3 输出 1024 维。") == obs._norm_fact_text("bge-m3输出1024维")


def test_dedupe_drops_fact_already_in_body():
    body = "- 二进制位于 `/tmp/llama.cpp/build/bin/llama-embedding`，模型 274MB"
    facts = ["二进制位于 `/tmp/llama.cpp/build/bin/llama-embedding`，模型 274MB"]
    assert obs._dedupe_facts(body, facts) == []


def test_dedupe_drops_slight_rewording_contained_in_body():
    """标点/空格/大小写不同, 但归一化后仍被旧 body 包含 → 判重复。"""
    body = "## 操作知识\n- 数据库已统一为 vector(1024)，仍待重新上传生成向量。"
    facts = ["数据库已统一为 vector(1024) 仍待重新上传生成向量"]
    assert obs._dedupe_facts(body, facts) == []


def test_dedupe_keeps_genuinely_new_fact():
    body = "- 数据库已统一为 vector(1024)"
    facts = ["Honcho 改用 LaunchAgent 托管, RunAtLoad=true"]
    assert obs._dedupe_facts(body, facts) == facts


def test_dedupe_within_batch_keeps_first():
    facts = ["补丁存为 honcho-1024dim.patch", "补丁存为 honcho-1024dim.patch"]
    assert obs._dedupe_facts("", facts) == ["补丁存为 honcho-1024dim.patch"]


def test_dedupe_does_not_swallow_unrelated_short_fact():
    """短事实不能被长事实"包含关系"误杀到相反方向 — 保留先出现的, 后出现的不吞。"""
    facts = ["重启后 memory-core 为 loaded", "补丁已应用"]
    assert obs._dedupe_facts("", facts) == facts


# ── [#N] 引用落成可核实来源 ───────────────────────────────────────────────


def test_single_citation_maps_to_real_qa_id():
    """[#N] 是批内 1-based 序号, 不是全局 id — 第 7 条对应 qa_rows[6]。"""
    ids, suffix = obs._fact_refs_from_text("……[ #7 ]", _qa(list(range(101, 111))))
    assert ids == [107]
    assert suffix == ""


def test_multi_citation_keeps_set_and_no_single_claim():
    ids, suffix = obs._fact_refs_from_text("先报错 [#2]，后回退 [#4]", _qa([201, 202, 203, 204]))
    assert ids == [202, 204]
    assert suffix == ":qa=202,204"


def test_out_of_range_citation_ignored():
    ids, suffix = obs._fact_refs_from_text("越界引用 [#99]", _qa([1, 2, 3]))
    assert ids == []
    assert suffix == ""


def test_no_citation_stays_unresolved():
    ids, suffix = obs._fact_refs_from_text("没有引用的事实", _qa([1, 2]))
    assert ids == []
    assert suffix == ""


def test_no_qa_rows_returns_empty():
    assert obs._fact_refs_from_text("[#1]", None) == ([], "")
