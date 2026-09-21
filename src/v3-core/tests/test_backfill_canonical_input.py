# -*- coding: utf-8 -*-
"""``embedding_backfill._row_text`` 的 canonical embedding 输入回归测试。

不变量（这是本文件存在的唯一理由）：
    修复 pass 写回 `yin_paragraphs.embedding` 时，必须复现「活路径当时真正喂给
    embedding 模型的文本」。`yin_paragraphs` 有 **两个活写入者**，它们往同一张表写
    了两种不同的 embedding 文本，所以 canonical 输入必须按 writer 判别，不能只看表名：

      * Writer A — ``yin_pool.py`` 的 ``_ingest_yin``（约 124 行）：
            emb_text = f"{title}. {body[:1500]}"
            INSERT 存 section = title, content = body[:6000]
        行内可精确重建为 ``f"{section}. {content[:1500]}"``。
      * Writer B — ``e1.py`` 的 ``_ingest_yin_segments``（约 1027 行），经
        ``pg_store.insert_effective(pool_role="yin_segment")``：
            seg_text = "## " + title + "\\n\\n" + body
            emb = _ce(seg_text[:2000], embed_cfg)
            存 section = "E1/" + title[:50], content = seg_text[:5000]
        行内可精确重建为 ``content[:2000]``。

修复前 ``_row_text`` 对 ``yin_paragraphs`` 返回裸 ``content``：那是一个任何活路径都
从未产生过的向量，会让历史向量与新写向量落在两个不同的语义空间里（按 section 标题
的召回行为会与活写入的行不一致）。

本文件是纯单元测试：不连 PostgreSQL（测试域 conftest 本身也硬封禁 psycopg2.connect）、
不联网、除 tmp_path 外不写文件。
"""
from __future__ import annotations

import pytest

from v3core.tools.embedding_backfill import _row_text, _yin_writer_kind


# ── 夹具：两种 writer 的真实行形状 ─────────────────────────────────────────────

def _writer_a_row(section: str = "怎么判断", content: str = "怎么判断\n\n正文内容",
                  yin_version: str = "y_2026-09-20_113304.md") -> dict:
    """yin_pool.py 写的行（section = 段标题，content = body[:6000]）。"""
    return {"yin_version": yin_version, "section": section, "content": content}


def _writer_b_row(section: str = "E1/怎么判断", content: str = "## 怎么判断\n\n正文内容",
                  yin_version: str = "e1_seg_20260920_120000_1") -> dict:
    """e1.py 写的行（section = "E1/"+title，content = seg_text[:5000]）。"""
    return {"yin_version": yin_version, "section": section, "content": content}


# ── 判别器本身 ────────────────────────────────────────────────────────────────

def test_yin_writer_kind_discriminates_two_writers():
    """判别器按 (yin_version, section) 双谓词区分两个 writer。"""
    assert _yin_writer_kind(_writer_a_row()) == "yin_pool"
    assert _yin_writer_kind(_writer_b_row()) == "e1"


def test_yin_writer_kind_requires_both_predicates():
    """单凭任一谓词都不够：两个前缀必须同时命中，否则退回 yin_pool。

    只认 ``section`` 前缀会被真实的「E1/...」印段标题误判；只认 ``yin_version``
    前缀会被名叫 e1_seg_* 的印文件误判。两个都要求，才既覆盖生产 620 行又不会
    把 Writer A 的行错判成 Writer B（那会把它的 canonical 输入换成 content[:2000]）。
    """
    # 只有 section 像 E1，yin_version 不是 e1_seg_ → 仍是 yin_pool
    assert _yin_writer_kind(_writer_a_row(section="E1/怎么判断")) == "yin_pool"
    # 只有 yin_version 像 e1_seg_，section 不是 E1/ → 仍是 yin_pool
    assert _yin_writer_kind(
        _writer_a_row(yin_version="e1_seg_lookalike.md")) == "yin_pool"


# ── case 1：Writer A 的 canonical 输入必须带 "{section}. " 前缀 ────────────────

def test_case1_writer_a_returns_section_prefix_plus_content_prefix():
    """case 1：Writer A 行 → 精确等于 f"{section}. {content[:1500]}"。

    这是整个修复的核心：前缀 "{section}. " 就是活路径 emb_text 的开头，也是按标题
    召回时被匹配到的那部分文本。裸 content 会丢掉它。
    """
    section = "怎么判断"
    content = "怎么判断\n\n正文内容"
    row = _writer_a_row(section=section, content=content)

    got = _row_text("yin_paragraphs", row)

    expected = f"{section}. {content[:1500]}"
    assert got == expected
    # 前缀是本次修复的全部意义所在，单独断言一次
    assert got.startswith(f"{section}. ")


# ── case 2：Writer B 的 canonical 输入是 content[:2000]，且绝不能带前缀 ───────

def test_case2_writer_b_returns_content_slice_without_section_prefix():
    """case 2：Writer B 行 → 精确等于 content[:2000]，且不以 f"{section}. " 开头。

    Writer B 的 section 列是 "E1/" + title（一个存储用的限定名），从未出现在被
    embedding 的 seg_text 里；给它套上 "{section}. " 前缀就是伪造输入。
    """
    section = "E1/怎么判断"
    content = "## 怎么判断\n\n正文内容"
    row = _writer_b_row(section=section, content=content)

    got = _row_text("yin_paragraphs", row)

    assert got == content[:2000]
    assert not got.startswith(f"{section}. ")


# ── case 3：截断长度 ─────────────────────────────────────────────────────────

def test_case3_writer_a_truncates_content_at_1500_chars():
    """case 3a：Writer A 6000 字 content → 长度 == len(f"{section}. ") + 1500。

    活路径只 embed body[:1500]；多喂一个字都会得到一个活路径不会产生的向量。
    """
    section = "长段落"
    content = "A" * 6000
    row = _writer_a_row(section=section, content=content)

    got = _row_text("yin_paragraphs", row)

    assert len(got) == len(f"{section}. ") + 1500
    assert got == f"{section}. " + content[:1500]


def test_case3_writer_b_truncates_content_at_2000_chars():
    """case 3b：Writer B 4000 字 content → 恰好 2000 字。"""
    content = "B" * 4000
    row = _writer_b_row(content=content)

    got = _row_text("yin_paragraphs", row)

    assert len(got) == 2000
    assert got == content[:2000]


# ── case 4：短 content 边界（不得抛异常，全文保留在前缀之后） ────────────────

def test_case4_writer_a_short_content_is_kept_whole_after_prefix():
    """case 4：Writer A 的 content 短于 1500 时不抛异常，前缀后接完整 content。

    body < 1500 时 content == body，content[:1500] == content，重建仍然精确；
    切片绝不能因为短而变成空串或抛错。
    """
    section = "短段"
    content = "短" * 10
    assert len(content) < 1500
    row = _writer_a_row(section=section, content=content)

    got = _row_text("yin_paragraphs", row)

    assert got == f"{section}. {content}"
    assert got.endswith(content)


# ── case 5-7：拆分后其它分支的回归护栏 ───────────────────────────────────────

def test_case5_observation_notes_still_returns_full_content():
    """case 5：observation_notes 仍返回**完整** content（拆分未破坏它）。

    活路径 embed 整条 note 文本；这里用 30000 字内容证明没有引入任何截断
    （若误用了 yin 分支的 1500/2000 切片，本用例会立刻失败）。
    """
    content = "N" * 30000
    row = {"version": "v1", "content": content, "title": "note"}

    got = _row_text("observation_notes", row)

    assert len(got) == 30000
    assert got == content


def test_case6_conversation_stream_still_returns_2000_char_slice():
    """case 6：conversation_stream 仍返回 content[:2000]（与 ingest._flush 一致）。"""
    content = "S" * 5000
    row = {"id": 1, "content": content}

    got = _row_text("conversation_stream", row)

    assert got == content[:2000]
    assert len(got) == 2000


def test_case7_unknown_table_raises_keyerror():
    """case 7：未定义 canonical 表示的表仍然抛 KeyError（拒绝而非猜测）。"""
    with pytest.raises(KeyError):
        _row_text("not_a_canonical_table", {"content": "x"})


def test_case7b_yin_paragraphs_none_values_do_not_raise():
    """补充边界：writer 判别所需的列全为 NULL 时不得抛异常。

    生产库里 8 行当前 NULL 的 yin_paragraphs 行全是 Writer A 行，但列本身仍可能
    取到 None；None 必须安全降级为 yin_pool 分支的空字符串，而不是 TypeError。
    """
    got = _row_text("yin_paragraphs",
                    {"yin_version": None, "section": None, "content": None})

    assert got == ". "
    assert _yin_writer_kind({"yin_version": None, "section": None}) == "yin_pool"

# ── topics：D4 canonical 表示（完整拼接后再截断） ──────────────────────────────
#
# 活写入者 ``topic_store.upsert_topic`` 的真实语义是：
#     text = f"{title}. {summary} {(body or '')[:800]} {kw_str}".strip()
#     embed(text[:1000])
# 即：**先拼出完整 canonical text，再对最终结果截断**。
# 修复前 backfill 只把 body 截到 800，却漏掉了最后的 [:1000] —— 对生产库中
# 173 张超过 1000 字的卡，它会 embed 一个活路径从未产生过的字符串，把修复向量
# 写进第二个语义空间（召回行为与活写入的行不一致，且事后不可察）。


def _live_topic_text(title, summary, body, keywords) -> str:
    """活写入者 upsert_topic 的逐字复刻（截断前的完整文本）。

    title 也走 ``or ""``：DB 列可为 NULL，活路径同样不能把 None 拼进 f-string
    （否则会得到字面量 "None"）。这里与实现的容错保持一致。
    """
    kw_str = " ".join(str(x) for x in keywords) if isinstance(keywords, list) else ""
    return f"{title or ''}. {summary or ''} {(body or '')[:800]} {kw_str}".strip()


def _topic_row(title, summary, body, keywords):
    return {"id": 1, "title": title, "summary": summary, "body": body, "keywords": keywords}


def test_topics_final_text_under_1000_is_returned_unchanged():
    """final text < 1000：必须原样返回，且与活写入者逐字节一致。"""
    row = _topic_row("短标题", "短摘要", "短正文", ["a", "b"])

    got = _row_text("topics", row)

    full = _live_topic_text("短标题", "短摘要", "短正文", ["a", "b"])
    assert len(full) < 1000
    assert got == full


def test_topics_final_text_exactly_at_boundary():
    """final text 恰好 1000：截断是 no-op，不得吃掉字符。"""
    body = "B" * 796
    row = _topic_row("T", "S" * 200, body, [])

    got = _row_text("topics", row)

    full = _live_topic_text("T", "S" * 200, body, [])
    assert len(full) == 1000
    assert got == full
    assert len(got) == 1000


def test_topics_final_text_just_over_boundary():
    """final text 略超 1000：结果必须恰好是完整文本的前 1000 字符。"""
    body = "B" * 2000
    row = _topic_row("T", "S" * 200, body, [])

    got = _row_text("topics", row)

    full = _live_topic_text("T", "S" * 200, body, [])
    assert len(full) > 1000
    assert len(got) == 1000
    assert got == full[:1000]


def test_topics_long_card_is_byte_identical_to_live_writer():
    """>1000 的长卡：backfill 文本必须与活写入者 byte-identical。

    这是 D4 的核心回归 —— 修复前这里会返回未截断的完整文本（长度 > 1000），
    断言立刻失败。
    """
    row = _topic_row("长标题", "S" * 300, "B" * 3000, ["碑", "迹", "印"])

    got = _row_text("topics", row)

    full = _live_topic_text("长标题", "S" * 300, "B" * 3000, ["碑", "迹", "印"])
    assert len(full) > 1000
    assert got == full[:1000]
    assert got != full                      # 修复前这里会是 full（未截断）
    assert len(got) == 1000


def test_topics_truncation_applies_to_final_string_not_to_body():
    """截断作用在**最终字符串**上，不是 body 上。

    body 1200 字（>800），但拼出的完整文本只有 804 字（<1000）——
    若实现误把 [:1000] 作用在 body 上、或按 body 长度判断，本用例会失败。
    """
    row = _topic_row("T", "", "B" * 1200, [])

    got = _row_text("topics", row)

    full = _live_topic_text("T", "", "B" * 1200, [])
    assert len(full) < 1000
    assert got == full
    assert len(got) == len(full)


def test_topics_keywords_json_string_is_tolerated():
    """keywords 以 JSON 字符串到达时（旧行/中间层）仍需产出与活路径一致的结果。"""
    row = _topic_row("T", "S", "B" * 2000, '["碑", "迹"]')

    got = _row_text("topics", row)

    full = _live_topic_text("T", "S", "B" * 2000, ["碑", "迹"])
    assert got == full[:1000]


def test_topics_none_fields_do_not_raise():
    """全 NULL 字段必须安全降级为空串，而不是 TypeError。

    注意结果是 ``"."`` 而不是 ``". "``：活写入者最后会 ``.strip()``，尾随空格被去掉。
    """
    got = _row_text("topics", {"id": 1, "title": None, "summary": None,
                               "body": None, "keywords": None})

    assert got == "."
    assert got == _live_topic_text(None, None, None, None)
