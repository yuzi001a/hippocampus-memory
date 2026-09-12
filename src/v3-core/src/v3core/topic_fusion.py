"""topic_fusion.py — 主题体融合

把 topic_block.body 从原始片段拼接 → LLM 合成的连贯短文
每个主题一次 LLM 调用，输入所有条目内容，输出结构化摘要
"""
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from .embedding import call_embedding
from .topic_store import TopicStore
from .config import resolve_config, _resolve_prompt as _cfg_prompt, _resolve_data_dir


try:
    from . import _safe_err
except (ImportError, AttributeError):
    try:
        from .. import _safe_err
    except (ImportError, AttributeError):
        def _safe_err(e, max_len=80):
            try:
                from . import _safe_err as _impl
            except ImportError:
                from .. import _safe_err as _impl
            globals()["_safe_err"] = _impl
            return _impl(e, max_len)

logger = logging.getLogger("v3core.topic_fusion")

# 匹配 LLM 原始 Q/A 对话标记（行首或换行后紧跟这些标记 + 空白）。
# 当 LLM 没合成散文、直接把 prompt 里注入的条目格式原样回吐时会命中。
_HAS_RAW_DIALOGUE_RE = re.compile(
    r'(?:^|\n)(?:Q:|A:|用户:|助手:|User:|Assistant:|Human:)\s'
)


def _has_raw_dialogue(s: str) -> bool:
    """检查 LLM 输出是否包含原始 Q/A 对话标记（未合成散文）"""
    return bool(_HAS_RAW_DIALOGUE_RE.search(s))

DB_PATH = lambda: str(_resolve_data_dir() / 'v3_topic_full.db')
MIN_ENTRIES = 1  # 至少几条条目才融合


def _load_llm_cfg():
    """从 .env 加载 LLM 配置 — 不隐式指向任何厂商，要求显式配置"""
    env_path = os.path.expanduser('~/AppData/Local/hermes/.env')
    if not os.path.exists(env_path):
        return None
    api_key = ""
    for line in open(env_path, encoding='utf-8'):
        stripped = line.strip()
        if stripped.startswith('LLM_API_KEY='):
            api_key = stripped.split('=', 1)[1]
            break
        if stripped.startswith('MINIMAX_CN_API_KEY='):
            api_key = stripped.split('=', 1)[1]
            break
    if not api_key:
        return None
    # endpoint/model 从环境变量读，留空则由调用方决定（调用方会通过 base_url 兜底）
    return {
        'endpoint': os.environ.get('LLM_BASE_URL', ''),
        'model': os.environ.get('LLM_MODEL', ''),
        'api_key': api_key,
        'proxy': os.environ.get('V3CORE_LLM_PROXY', ''),
    }


def _call_llm(messages: list, llm_cfg: dict) -> str:
    """调 LLM 生成文本（OpenAI 兼容协议）"""
    import requests
    headers = {
        'Authorization': f'Bearer {llm_cfg["api_key"]}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': llm_cfg['model'],
        'messages': messages,
        'max_tokens': 800,
        'temperature': 0.3,
    }
    resp = requests.post(
        f'{llm_cfg["endpoint"]}/chat/completions',
        headers=headers,
        json=payload,
        timeout=120,  # 大 topic 需要更长时间
    )
    data = resp.json()
    return data['choices'][0]['message']['content']


FUSION_PROMPT = """你是一个主题总结专家。给你一个主题的名称、摘要，和一组来自对话的相关条目（每条有时间标签），请你把它们融合成一段连贯、简洁的短文。

要求：
1. 保持事实准确，不添加未出现的信息
2. 按时间顺序组织，在文中体现状态变化和演进脉络
3. 用两段式结构：讨论历程（含关键转折）→ 当前状态/结论
4. 与输入条目保持相同语言，简洁，不要啰嗦
5. 输出纯文本，不要 markdown 标记
6. ⚠️ 关键约束：只从条目中提取明确陈述的事实。如果条目只说了"正在做某事"，不要推断"已完成"。状态变化只写条目中明确记录的。不确定的不要写。

主题名称：{title}
主题摘要：{summary}

带时间戳的条目内容：
{entries}

融合后的短文（包含时间线和当前状态）：

【结构化输出契约】
- 输出语言与输入条目一致（条目中文则中文，英文则英文）。
- 两段式：第①段讨论历程+关键转折，第②段当前状态/结论。
- 不要 markdown 标记（无 #、- *）。
- 不超过 600 字。"""


def fuse_topic(store: TopicStore, topic_id: int, title: str, summary: str,
               entries: list, llm_cfg: dict, cfg=None) -> Optional[str]:
    """融合一个主题的全部条目 → 连贯短文"""
    if len(entries) < MIN_ENTRIES:
        return None

    # 构建条目文本（question + answer + 时间戳 都喂给 LLM）
    entry_texts = []
    for i, e in enumerate(entries[:15]):  # 最多15条
        q = (e.get('question', '') or '')[:300]
        a = (e.get('answer', '') or '')[:300]
        ts = e.get('timestamp', '') or ''
        if ts:
            ts = str(ts)[:19]  # 截断到 YYYY-MM-DD HH:MM:SS
        label = ts if ts else f"条目{i+1}"
        entry_texts.append(f"--- {label} ---\n用户: {q}\n助手: {a}")

    entries_text = '\n'.join(entry_texts)

    # 优先用 cfg 配置的 prompt (覆盖 FUSION_PROMPT)，未配则走默认
    fusion_template = _cfg_prompt(cfg, "topic_fusion", FUSION_PROMPT)
    prompt = fusion_template.format(
        title=title,
        summary=summary or '(无摘要)',
        entries=entries_text,
    )

    try:
        result = _call_llm([
            {'role': 'user', 'content': prompt}
        ], llm_cfg)
        # 清洗：去除 LLM 内部推理过程
        result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL).strip()
        # 也清理 <｜end▁of▁thinking｜>_json 等可能的溢出
        if '<｜end▁of▁thinking｜>_json' in result:
            result = result.split(' response_json')[0].strip()
        # 检查原始 Q/A 对话标记（LLM 未合成，直接返回了条目格式）
        if _has_raw_dialogue(result):
            logger.info("fuse_topic %s returned raw dialogue markers, treating as failure", title[:20])
            return None
        # 纯 think / 纯空白场景：返回 None 而不是空串，让调用方的 `if fused:` 自然跳过
        if not result:
            logger.info("fuse_topic %s returned empty after think-strip (纯 think 块场景)", title[:20])
            return None
        return result
    except Exception as e:
        logger.warning("fuse_topic %s failed: %s", title[:20], _safe_err(e)[:80])
        return None


def fuse_all_topics(dry_run: bool = False, max_topics: int = 51, config: dict | None = None) -> dict:
    """融合全部主题的 body

    返回 {topic_id: 是否成功}
    """
    # 尝试拿 cfg 用于 prompt 覆盖；拿不到也不影响主流程 (fallback 到默认 prompt)
    cfg = config if config is not None else None
    llm_cfg = _load_llm_cfg()
    if not llm_cfg:
        logger.error("LLM_API_KEY (or MINIMAX_CN_API_KEY fallback) not found in .env")
        return {}

    store = TopicStore(DB_PATH())
    topics = store.get_all_topics()
    results = {}

    for i, t in enumerate(topics):
        tid = t['id']
        title = t['title']
        summary = t.get('summary', '')

        # 读条目（含时间戳）
        entries = store.conn.execute(
            'SELECT question, answer, timestamp FROM topic_entries WHERE topic_id=?',
            (tid,)
        ).fetchall()
        entry_list = [{'question': e[0] or '', 'answer': e[1] or '', 'timestamp': e[2]} for e in entries]

        if len(entry_list) < MIN_ENTRIES:
            results[tid] = 'skip'
            continue

        if dry_run:
            results[tid] = 'preview'
            entries_total = len(entry_list)
            print(f'[{i+1}/{len(topics)}] {title[:30]:30s} ({entries_total}条) — dry_run')
            continue

        print(f'[{i+1}/{len(topics)}] {title[:30]:30s} ({len(entry_list)}条) — 融合中...')

        fused = fuse_topic(store, tid, title, summary, entry_list, llm_cfg, cfg)
        if fused:
            store.conn.execute(
                'UPDATE topic_blocks SET body=?, updated_at=datetime("now") WHERE id=?',
                (fused[:3000], tid)
            )
            store.conn.commit()
            # 也更新 PG topics 表（prefetch/recall 读的地方）
            # 双重保护：既检查 fused.strip() 非空，也用 SQL 排除已存在的空串，
            # 防止 LLM 偶尔返回空 think-only 时把已有 body 覆盖成 ''
            if not fused.strip():
                logger.warning("  ⚠️ %s fused is empty/whitespace, 跳过 PG 同步", title[:20])
            else:
                try:
                    pg_cur = store.pg_conn.cursor()
                    pg_cur.execute(
                        "UPDATE topics SET body=%s, updated_at=NOW() WHERE topic_id=%s AND body IS NOT NULL AND body != ''",
                        (fused[:3000], tid)
                    )
                    store.pg_conn.commit()
                    if pg_cur.rowcount > 0:
                        logger.info("  ↳ PG topics 已同步")
                except Exception:
                    pass  # PG 没有此 topic 或不可达，不阻塞
            results[tid] = True
            logger.info("  ✅ %s fused: %s", title[:20], fused[:60])
        else:
            results[tid] = False
            logger.warning("  ❌ %s fusion failed", title[:20])

        # 限流：每秒1个请求
        time.sleep(1.1)

        if i >= max_topics - 1:
            break

    fused_count = sum(1 for v in results.values() if v is True)
    logger.info("fuse_all_topics done: %d/%d fused", fused_count, len(results))
    return results


if __name__ == '__main__':
    import sys
    dry = '--dry-run' in sys.argv
    fuse_all_topics(dry_run=dry)
