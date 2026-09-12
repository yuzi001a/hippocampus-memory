"""Session Summarizer v3 — 跨对话上下文恢复

基于旧版 deep_memory_v3/session_summarizer.py 的设计，
复用 Hermes state.db 结构化字段减少 LLM 调用。

输入: j/ 迹层原始对话
LLM: 精调 prompt → 标题 + 叙事摘要 + 关键决策 + 关键教训 + 待办
输出: 五段式结构化卡
落盘: b/session_summaries/ + effective_pool

双触发:
  - 主: on_session_end hook
  - 兜底: cron job
"""

from __future__ import annotations
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .card_store import DeepStore
from .llm import LLMClient
from .config import resolve_config, _resolve_prompt as _cfg_prompt


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

logger = logging.getLogger("v3core.session_summary")

SESSION_SUMMARY_DIR = "session_summaries"

# 旧版精调过的 prompt，保留完整
SESSION_PROMPT = """你是一个 session 摘要助手。你的任务是：读一段对话原文，写一篇叙事摘要。

【任务】
写一篇 200-400 字的叙事摘要，概括这段对话发生了什么。

要求：
1. **严格区分角色** — 叙事中必须清楚标明"用户做了什么/说了什么"和"助手做了什么/说了什么"。不要笼统写"拍了板"，要写"用户拍板决定"或"助手建议"。
2. 第一句点明用户在什么状态下开始这段对话。如果对话是 cron 系统触发（非真人操作），在开头就说明"cron 系统触发"，不要写成用户在操作。
3. 中间叙述关键事件：用户拍了什么板 / 助手干了什么 / 出了什么问题 / 解决了什么
4. 最后一句点明对话结束时是什么状态 / 待办是什么

【角色标注规范】
- 用户是指对话中 role=user 的消息。他是真人，做决策、提要求、纠正错误。
- 助手是指 AI 助手（对话中 role=assistant 的消息）。助手执行工具调用、分析、写作。
- 不要张冠李戴：用户的决策不要写成助手做的，助手干的事不要写成用户做的。
- cron 进程不是"用户"——如果对话由 cron 任务发起，开头要说明"cron 任务触发"。

【输出格式】
直接输出以下结构，不要代码块，不要多余内容：

## 标题
<一句话标题，12字内概括这次对话>

## 叙事摘要
<200-400 字的叙事>

## 关键决策
- <决策1>
...

## 关键教训
- <教训1>
...

## 待办
- <待办1>
...

【结构化输出契约】
- 五个段落顺序固定：## 标题 → ## 叙事摘要 → ## 关键决策 → ## 关键教训 → ## 待办。
- 标题 ≤12 字（与对话语言一致；对话若中文则中文标题）。
- 叙事摘要使用与对话原文相同的语言，硬约束 200-400 字（含标点）。
- 决策/教训/待办每段若干 "-" 开头 bullet，每条 1 行，长度合理不超 1 段。
- 输出整体使用对话原文相同语言（如对话为英文则全文英文）。
"""


def read_j_file(path: str | Path) -> str:
    """读取 j/ 迹文件, 返回对话原文 (去掉 tool 输出行)"""
    path = Path(path)
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace")
    if not content.strip():
        return ""
    lines = content.split("\n")
    clean = []
    for line in lines:
        s = line.strip()
        if s.startswith("[tool_result:") or s.startswith("[tool:"):
            continue
        if not s:
            continue
        clean.append(line)
    return "\n".join(clean)


def extract_session_meta_from_j(path: str | Path) -> dict:
    """从 j/ 文件名提取 session 元数据"""
    stem = Path(path).stem
    meta = {"session_date": "", "session_time": "", "session_id": stem}
    m = re.search(r"(?:j_)?(\d{4}-\d{2}-\d{2})_(\d{6})", stem)
    if m:
        meta["session_date"] = m.group(1)
        meta["session_time"] = m.group(2)
    else:
        m2 = re.search(r"(?:j_)?(\d{4}-\d{2}-\d{2})", stem)
        if m2:
            meta["session_date"] = m2.group(1)
    if not meta["session_date"]:
        try:
            mt = datetime.fromtimestamp(Path(path).stat().st_mtime)
            meta["session_date"] = mt.strftime("%Y-%m-%d")
            meta["session_time"] = mt.strftime("%H%M%S")
        except Exception:
            pass
    return meta


def _try_enrich_from_state_db(
    meta: dict,
    session_id: str | None,
    state_db_path: str | None,
) -> dict:
    """尝试从 Hermes state.db 补充结构化元数据。失败不影响主流程。"""
    if not state_db_path or not session_id:
        return meta
    try:
        db = sqlite3.connect(state_db_path)
        db.row_factory = sqlite3.Row
        c = db.execute(
            "SELECT source, model, message_count, tool_call_count, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
            "parent_session_id, title, started_at, ended_at, estimated_cost_usd "
            "FROM sessions WHERE parent_session_id = ? OR id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (session_id, session_id)
        )
        row = c.fetchone()
        db.close()
        if row:
            meta["hermes_source"] = row["source"]
            meta["hermes_model"] = row["model"]
            meta["hermes_msg_count"] = row["message_count"]
            meta["hermes_tokens_in"] = row["input_tokens"]
            meta["hermes_tokens_out"] = row["output_tokens"]
            meta["hermes_cost"] = row["estimated_cost_usd"]
            meta["hermes_title"] = row["title"]
    except Exception as e:
        logger.debug("state.db 补充失败 (非致命): %s", e)
    return meta


def summarize_j_file(
    j_path: str | Path,
    config: dict | None = None,
    state_db_path: str | None = None,
    session_id: str | None = None,
    pool: Any = None,
) -> str | None:
    """处理单个 j/ 迹文件，生成摘要并写入

    Returns: summary 文本 (成功), None (失败/跳过)
    """
    j_path = Path(j_path)
    if not j_path.exists():
        return None

    # 跳过 cron 任务触发的 session — 无真人对话，无总结必要
    if j_path.parent.name.startswith("cron_") or j_path.name.startswith("cron_"):
        return None

    raw_text = read_j_file(j_path)
    if not raw_text or len(raw_text) < 200:
        logger.info("跳过短文件: %s (%d)", j_path.name, len(raw_text or ""))
        return None

    cfg = config or resolve_config()
    meta = extract_session_meta_from_j(j_path)
    # j/<sid>/turns.md 格式：用目录名当 session_id
    if j_path.name == "turns.md" and j_path.parent.name:
        meta["session_id"] = j_path.parent.name
        m = re.search(r"(?:j_)?(\d{4}-\d{2}-\d{2})_(\d{6})", j_path.parent.name)
        if m:
            meta["session_date"] = m.group(1)
            meta["session_time"] = m.group(2)

    # 从 Hermes state.db 补充结构化字段（零 LLM 成本）
    meta = _try_enrich_from_state_db(meta, session_id, state_db_path)

    # 构造 LLM 提示（旧版精调 prompt + 结构化上下文）
    context_lines = []
    # 子目录格式: source 用 <sid>/turns.md 唯一定位
    if j_path.name == "turns.md" and j_path.parent and j_path.parent.name:
        meta["source"] = j_path.parent.name + "/turns.md"
    hermes_source = meta.get("hermes_source", "")
    hermes_model = meta.get("hermes_model", "")
    hermes_tokens = meta.get("hermes_tokens_in", 0)
    if hermes_source:
        context_lines.append(f"来源: {hermes_source}")
    if hermes_model:
        context_lines.append(f"模型: {hermes_model}")
    if hermes_tokens:
        context_lines.append(f"Token 消耗: {hermes_tokens} in / {meta.get('hermes_tokens_out', 0)} out")
    context_extra = "\n".join(context_lines)
    if context_extra:
        context_extra = "\n【结构化元数据】\n" + context_extra + "\n"

    user_payload = context_extra + "【对话原文】\n" + raw_text

    llm = LLMClient(cfg)
    system_prompt = _cfg_prompt(cfg, "session_summary", SESSION_PROMPT)
    result = llm.chat(
        system=system_prompt,
        messages=[{"role": "user", "content": user_payload}],
        temperature=0.3,
    )

    if not result or len(result) < 50:
        logger.warning("LLM 输出过短: %d chars", len(result or ""))
        return None

    summary = result.strip()

    # 从 LLM 输出提取语义标题
    session_title = None
    title_match = re.search(r"## 标题[\s\n]+(.+?)[\s\n]*## ", summary)
    if title_match:
        session_title = title_match.group(1).strip()[:40]
        # 去掉标题段，避免卡里重复
        summary = re.sub(r"## 标题[\s\S]+?(?=## )", "", summary)
    if not session_title:
        session_title = f"{(meta.get('session_date', '') or '')} {j_path.stem}"

    # 写 b/session_summaries/ → SQLite v3_cards.db
    store = DeepStore(cfg)

    source_id = f"session_{meta['session_date']}_{meta['session_time']}_{j_path.stem}"
    title = session_title or f"{meta.get('session_date', '')} {j_path.stem}"
    tags = ["session_summary", meta.get('session_date', '')]
    body_text = summary[:3000]

    # 写 SQLite (DeepStore → SqliteCardStore 走)
    try:
        from .embedding import call_embedding, safe_embed_cfg as _safe_embed_cfg

        # 生成嵌入 — 阶段1 (2026-08-20): 走 build_embed_cfg 工厂, 缺 model/endpoint → disabled
        embedding = None
        try:
            embed_cfg = _safe_embed_cfg(cfg)
            if embed_cfg is not None:
                head = summary[:500].replace("\n", " ")
                ev = call_embedding(head, embed_cfg)
                embedding = ev if ev and len(ev) > 0 else None
        except ValueError:
            raise
        except Exception:
            pass

        store.write_card(
            category="session_summary",
            filename=source_id,
            title=title,
            content=body_text,
            tags=tags,
            source=meta.get("source") or j_path.name,
            source_j_ids=[meta.get("session_id", "")],
        )

        # 手动写 embedding (write_card 里 embed 可能因 endpoint 走不通而失败)
        # P2a follow-up (2026-09-09): 旧版直接走 ``store.sqlite.conn.execute(...)``
        # 是 thread-affinity bypass — 当 caller 在另一个线程上同时持有
        # SqliteCardStore 实例并发起其他写入/读取, 这里会在没拿到
        # ``self._lock`` 的情况下读 ``conn`` 属性, 触发 CPython 的
        # ``check_same_thread`` 校验抛 ``sqlite3.ProgrammingError``. 改为
        # 走 SqliteCardStore 上专门加的 ``update_card_embedding`` 窄入口
        # (UPDATE + commit, 整个调用在 ``self._lock`` 内), 既消除 bypass,
        # 又不暴露通用 unlocked executor 给外部 caller.
        if embedding and hasattr(store, "sqlite"):
            try:
                store.sqlite.update_card_embedding(source_id, embedding)
            except Exception as _ue_e:
                # 派生侧降级 — 与 PG canonical 真值无关, 仅记录 warning
                # (主链已 try/except 包住, 此处双保险, 不掩盖派生失败).
                logger.warning(
                    "session_summary embedding backfill 失败 (非致命): %s",
                    _safe_err(_ue_e)[:200],
                )

        logger.info("已写入 session_summary (SQLite): %s", source_id)
    except ValueError:
        raise
    except Exception as e:
        logger.warning("SQLite 写入失败 (非致命): %s", _safe_err(e)[:200])
        # 保底: 写文件
        summary_dir = store.base / "cards" / SESSION_SUMMARY_DIR
        summary_dir.mkdir(parents=True, exist_ok=True)
        sf = f"session_{meta['session_date']}_{meta['session_time']}_{j_path.stem}.md"
        sp = summary_dir / sf
        sp.write_text(summary, encoding="utf-8")
        logger.info("已回退写文件: %s", sp.name)

    # 入 effective_pool (给 prefetch 召回到) — 保持原有逻辑不变
    try:
        from .pg_store import PgEmbedStore
        from .embedding import call_embedding, safe_embed_cfg as _safe_embed_cfg
        # 阶段1 (2026-08-20): 走 build_embed_cfg 工厂, 缺 model/endpoint → disabled 不发请求
        embed_cfg = _safe_embed_cfg(cfg)
        if pool is not None:
            pg = PgEmbedStore(config=cfg, pool=pool)
        else:
            pg = PgEmbedStore(config=cfg)
        sid = f"session_summary_{meta['session_date']}_{meta['session_time']}"
        ev = None
        if embed_cfg is not None:
            try:
                head = summary[:500].replace("\n", " ")
                ev = call_embedding(head, embed_cfg)
            except ValueError:
                raise
            except Exception as _emb_e:
                logger.debug("session_summary effective embedding skipped: %s", _safe_err(_emb_e)[:120])
        pg.insert_effective(
            source_id=sid,
            pool_role="session_summary",
            title=session_title,
            content=summary[:5000],
            embedding=ev,
            embed_cfg=embed_cfg,
            scope_id="all",
        )
        logger.info("已入 effective_pool (session_summary): %s", sid)

        # 也同步写入 PG topics —— 让 session_summary 能被向量召回到
        try:
            card_sid = f"session_summaries/{sp.name}"
            pg.insert_card(card_sid, session_title, summary[:3000], "session_summary", 
                          tags=["auto", meta.get('session_date', '')], embedding=ev,
                          embed_cfg=embed_cfg)
            logger.info("  topics 写入: %s", card_sid)
        except ValueError:
            raise
        except Exception as _card_e:
            logger.debug("topics 写入失败 (非致命): %s", _card_e)
    except ValueError:
        raise
    except Exception as e:
        logger.debug("effective_pool 写入失败 (非致命): %s", e)

    return summary


def summarize_from_messages(
    session_id: str,
    messages: list[dict] | None = None,
    config: dict | None = None,
    force: bool = False,
    pool: Any = None,
) -> str | None:
    """从实时消息直接生成 session 摘要（不依赖 j/ 文件系统）

    供 v3hermes sync_turn / on_pre_compress 调用。
    短 session（<5 条有效消息）跳过，除非 force=True。

    Args:
        session_id: session 标识
        messages: 来自 Hermes 钩子的消息列表
        config: v3-core 配置
        force: 强制生成（无视长度门槛）
        pool: 可选的 PostgreSQL 连接池

    Returns: summary 文本 (成功), None (失败/跳过)
    """
    cfg = config or resolve_config()
    if not messages or not session_id:
        logger.debug("summarize_from_messages 跳过: 参数不足")
        return None

    # 筛选 user/assistant 消息，去 tool 噪音
    dialogue = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role in ("user", "assistant") and content:
            label = "用户" if role == "user" else "助手"
            text = str(content)[:500]
            dialogue.append(f"{label}: {text}")
    raw_text = "\n".join(dialogue)
    if not raw_text.strip() or (len(raw_text) < 200 and not force):
        logger.info("summarize_from_messages 跳过短 session: %s (%d chars)", session_id, len(raw_text))
        return None

    # 调用 LLM 生成结构化摘要
    try:
        llm = LLMClient(cfg)
        system_prompt = _cfg_prompt(cfg, "session_summary", SESSION_PROMPT)
        result = llm.chat(
            system=system_prompt,
            messages=[{"role": "user", "content": f"【对话原文】\n{raw_text}"}],
            temperature=0.3,
        )
    except Exception as e:
        logger.warning("summarize_from_messages LLM 失败: %s", _safe_err(e)[:200])
        return None

    if not result or len(result) < 50:
        logger.warning("summarize_from_messages LLM 输出过短: %d chars", len(result or ""))
        return None

    summary = result.strip()

    # 提取标题
    session_title = None
    title_match = re.search(r"## 标题[\s\n]+(.+?)[\s\n]*## ", summary)
    if title_match:
        session_title = title_match.group(1).strip()[:40]
        summary = re.sub(r"## 标题[\s\S]+?(?=## )", "", summary)
    if not session_title:
        session_title = f"session_{session_id[:12]}"

    # 写入 PG topics + observation_notes
    if pool is not None:
        _write_summary_to_stores(cfg, session_id, session_title, summary, pool=pool)
    else:
        _write_summary_to_stores(cfg, session_id, session_title, summary)

    logger.info("summarize_from_messages ✅ %s: %s", session_id[:16], session_title)
    return summary


def _write_summary_to_stores(
    cfg,
    session_id: str,
    title: str,
    body_text: str,
    pool: Any = None,
) -> None:
    """写入 session summary 到 PG topics + observation_notes

    复用原有写入逻辑（去掉了 j/ 文件相关的元数据依赖）。
    """
    try:
        from .pg_store import PgEmbedStore
        from .embedding import call_embedding, safe_embed_cfg as _safe_embed_cfg

        # 阶段1 (2026-08-20): 走 build_embed_cfg 工厂
        embed_cfg = _safe_embed_cfg(cfg)

        if pool is not None:
            pg = PgEmbedStore(config=cfg, pool=pool)
        else:
            pg = PgEmbedStore(config=cfg)

        now_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        source_id = f"session_summary_{session_id[:12]}_{now_str}"

        # embedding；disabled 或请求失败都保持 None，不制造零向量
        ev = None
        if embed_cfg is not None:
            try:
                head = body_text[:500].replace("\n", " ")
                ev = call_embedding(head, embed_cfg)
            except ValueError:
                raise
            except Exception as _emb_e:
                logger.debug("session_summary PG embedding skipped: %s", _safe_err(_emb_e)[:120])

        # effective_pool — 让 prefetch 能召回
        try:
            pg.insert_effective(
                source_id=source_id,
                pool_role="session_summary",
                title=title,
                content=body_text[:5000],
                embedding=ev,
                embed_cfg=embed_cfg,
                scope_id="all",
            )
            logger.info("  effective_pool: %s", source_id)
        except ValueError:
            raise
        except Exception as e:
            logger.debug("effective_pool 写入失败 (非致命): %s", e)

        # PG topics — 让向量召回能找到 session summary
        try:
            card_sid = f"session_summaries/{source_id}"
            pg.insert_card(
                card_sid, title, body_text[:3000], "session_summary",
                tags=["auto", datetime.now().strftime("%Y-%m-%d")],
                embedding=ev,
                embed_cfg=embed_cfg,
            )
            logger.info("  topics: %s", card_sid)
        except ValueError:
            raise
        except Exception as e:
            logger.debug("topics 写入失败 (非致命): %s", e)

        pg.close()
    except ValueError:
        raise
    except Exception as e:
        logger.warning("_write_summary_to_stores 失败: %s", _safe_err(e)[:200])


def on_session_end_handler(
    session_id: str,
    messages: list[dict] | None = None,
    state_db_path: str | None = None,
    pool: Any = None,
):
    """on_session_end hook — 为最新的 j/ 文件生成摘要"""
    cfg = resolve_config()
    store = DeepStore(cfg)
    j_dir = store.base / "j"
    if not j_dir.exists():
        logger.info("j/ 目录不存在, 跳过 session_summary")
        return

    j_files = sorted(
        [f for f in j_dir.iterdir() if f.suffix == ".md"],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not j_files:
        return

    latest = j_files[0]
    # 检查是否已处理
    processed = _get_processed_summaries(store)
    if latest.name in processed:
        logger.debug("j/ %s 已有摘要, 跳过", latest.name)
        return
    # 距离上一次写入至少 15 秒，防竞争
    if datetime.now().timestamp() - latest.stat().st_mtime < 15:
        logger.debug("j/ %s 太新, 跳过", latest.name)
        return

    try:
        if pool is not None:
            summarize_j_file(latest, cfg, state_db_path, session_id, pool=pool)
        else:
            summarize_j_file(latest, cfg, state_db_path, session_id)
    except Exception as e:
        logger.error("on_session_end 处理失败: %s", e)


def _get_processed_summaries(store: DeepStore) -> set:
    """获取已生成摘要的 j/ 文件名集合 (去重)"""
    sd = store.base / "cards" / SESSION_SUMMARY_DIR
    if not sd.exists():
        return set()
    processed = set()
    for f in sd.iterdir():
        if f.suffix == ".md":
            try:
                c = f.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"source: (.+)", c)
                if m:
                    processed.add(m.group(1).strip())
            except Exception:
                pass
    return processed


def batch_summarize(limit: int = 5, pool: Any = None) -> int:
    """兜底: 扫描未提炼的 j/ 文件"""
    cfg = resolve_config()
    store = DeepStore(cfg)
    processed = _get_processed_summaries(store)
    j_dir = store.base / "j"
    if not j_dir.exists():
        return 0
    # 两种格式: j/*.md (旧) 或 j/<sid>/turns.md (新 j_writer)
    j_files = sorted(
        [f for f in j_dir.iterdir() if f.suffix == ".md" and f.name not in processed],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    # 新格式: j/<sid>/turns.md — 用 <sid>/turns.md 作为去重键
    for sub in j_dir.iterdir():
        if not sub.is_dir(): continue
        # 跳过 cron 任务触发的 session — 无真人对话，无总结必要
        if sub.name.startswith("cron_"):
            continue
        tm = sub / "turns.md"
        if tm.exists():
            sub_key = sub.name + "/turns.md"
            if sub_key not in processed and sub.name not in processed:
                j_files.append(tm)
    j_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    now = datetime.now().timestamp()
    count = 0
    for f in j_files:
        if now - f.stat().st_mtime < 30:
            continue
        try:
            is_subdir = f.name == "turns.md"
            sid = f.parent.name if is_subdir else None
            if pool is not None:
                r = summarize_j_file(f, cfg, session_id=sid, pool=pool)
            else:
                r = summarize_j_file(f, cfg, session_id=sid)
            if r:
                # 子目录格式: 标记为 <sid>/turns.md 防同名误判
                if is_subdir:
                    # 同步 processed 集用 sid 标记
                    pass
                count += 1
        except Exception as e:
            logger.error("批处理失败 %s: %s", f.name, e)
        if count >= limit:
            break
    logger.info("批处理完成: %d/%d", count, len(j_files))
    return count