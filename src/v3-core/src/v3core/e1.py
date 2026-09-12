"""E1 印合成 — 每日 04:00 cron: 从近 24h 卡片合成新印 (y/ 层)"""
from __future__ import annotations
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .card_store import DeepStore
from .config import resolve_config, _resolve_prompt as _cfg_prompt
from .llm import LLMClient
from .pg_store import _is_undefined_column_error


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

logger = logging.getLogger("v3core.e1")


class _PgLeaseConnection:
    """Pool-owned PostgreSQL connection handle wrapping a PgLease.

    Delegates database operations (cursor, commit, rollback, etc.) to the
    leased physical connection. Calling `.close()` releases the lease back
    to the pool (which resets the session and returns the physical connection
    to the pool's idle queue) without closing the underlying physical connection.
    Implements `_connect()` for duck-typing compatibility with helpers expecting
    an object with a `_connect()` method.
    """

    __slots__ = ("_lease", "_closed")

    def __init__(self, lease: Any) -> None:
        object.__setattr__(self, "_lease", lease)
        object.__setattr__(self, "_closed", False)

    @property
    def _connection(self) -> Any:
        return self._lease.connection

    def _connect(self) -> Any:
        """Compatibility helper for callers checking hasattr(pg, '_connect')."""
        return self

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        return self._lease.connection.cursor(*args, **kwargs)

    def commit(self) -> None:
        self._lease.connection.commit()

    def rollback(self) -> None:
        self._lease.connection.rollback()

    def close(self) -> None:
        if not self._closed:
            object.__setattr__(self, "_closed", True)
            try:
                self._lease.close()
            except Exception:
                pass

    def __enter__(self) -> "_PgLeaseConnection":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        self.close()
        return False

    def __getattr__(self, name: str) -> Any:
        if name in ("_lease", "_closed"):
            return object.__getattribute__(self, name)
        return getattr(self._lease.connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_lease", "_closed"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._lease.connection, name, value)

    def __repr__(self) -> str:
        return f"<_PgLeaseConnection closed={self._closed} connection={self._lease.connection!r}>"


def _get_cfg_pg_conn(config: Any = None, pool: Any = None) -> Optional[Any]:
    """从 pool (优先) 或 V3Config/dict 获取 PG 连接，失败返回 None"""
    if pool is not None:
        try:
            lease = pool.lease(timeout=5)
            return _PgLeaseConnection(lease)
        except Exception as e:
            logger.debug("[e1] Pool lease failed: %s", _safe_err(e)[:100])
            return None
    try:
        import psycopg2
        if hasattr(config, "pg") and not isinstance(config, dict):
            pg_cfg = config.pg
            return psycopg2.connect(
                host=pg_cfg.host, port=pg_cfg.port,
                dbname=pg_cfg.database, user=pg_cfg.user,
                password=pg_cfg.password,
            )
        if isinstance(config, dict):
            pg_cfg = config.get("pg")
            if not pg_cfg:
                storage_cfg = config.get("storage") or {}
                if isinstance(storage_cfg, dict):
                    pg_cfg = storage_cfg.get("pg")
            if pg_cfg:
                return psycopg2.connect(
                    host=pg_cfg.get("host", "localhost"),
                    port=pg_cfg.get("port", 5433),
                    dbname=pg_cfg.get("database", "v3embeddings"),
                    user=pg_cfg.get("user", "v3user"),
                    password=pg_cfg.get("password", ""),
                )
    except Exception as e:
        logger.debug("[e1] PG connect failed: %s", _safe_err(e)[:100])
    return None


def _get_cfg_pg_conn_compat(config: Any = None, pool: Any = None) -> Optional[Any]:
    """Keep legacy monkeypatched one-argument seams intact when no pool is used."""
    if pool is None:
        return _get_cfg_pg_conn(config)
    return _get_cfg_pg_conn(config, pool=pool)


def _legacy_config(config: Any | None = None) -> dict[str, Any]:
    """把 typed/legacy 配置统一成旧 dict 形态，避免压缩路径误调 V3Config.get。"""
    if config is None:
        return resolve_config(return_legacy=True)
    if isinstance(config, dict):
        return config
    converter = getattr(config, "to_legacy_dict", None)
    if callable(converter):
        return converter()
    return {}


def _observer_setting(config: dict[str, Any], key: str, default: Any) -> Any:
    """读取 observer 子配置，同时兼容迁移期顶层旧键。"""
    observer_cfg = config.get("observer") or {}
    if isinstance(observer_cfg, dict) and key in observer_cfg:
        return observer_cfg[key]
    return config.get(key, default)


def _safe_notes_table(notes_table: str) -> str:
    """允许配置表名但拒绝把任意字符串拼进 SQL。"""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", notes_table):
        raise ValueError(f"非法 notes_table: {notes_table!r}")
    return notes_table


def _extract_e1_cfg(config) -> tuple[bool, str, float]:
    """兼容 V3Config / dict. 返回 (e1_enabled, llm_api_key, confidence_threshold)."""
    if hasattr(config, "e1") and hasattr(config, "llm") and not isinstance(config, dict):
        threshold = getattr(config.e1, "confidence_threshold", 0.3)
        return bool(config.e1.enabled), config.llm.api_key, float(threshold)
    if isinstance(config, dict):
        e1_block = config.get("e1", {}) or {}
        llm_block = config.get("llm", {}) or {}
        threshold = e1_block.get("confidence_threshold", 0.3)
        return bool(e1_block.get("enabled", True)), llm_block.get("api_key", ""), float(threshold)
    return True, "", 0.3


def _read_card_confidence(card_path: Path, store: DeepStore) -> float:
    """读取卡的 confidence 值 — 从 SQLite v3_cards.db.cards.confidence 列查

    v3-core 当前架构下, 碑卡元数据全部存在 SQLite 单文件 v3_cards.db,
    .meta.json sidecar 与 markdown frontmatter 都不再作为权威来源.
    Fallback 保留: 极端迁移场景 (旧卡未导入 SQLite) 时, 退化 .meta.json sidecar
    与 frontmatter; 都没有则默认 0.3.
    """
    # 1. SQLite (权威) — source_id = file stem (去掉 .md 扩展名)
    try:
        source_id = card_path.stem
        row = store.sqlite.read_card(source_id)
        if row is not None:
            v = row.get("confidence")
            if v is not None:
                return float(v)
    except Exception as e:
        logger.debug("[e1] SQLite 读 confidence 失败 (退化 .meta.json): %s", _safe_err(e)[:100])

    # 2. .meta.json sidecar (兼容迁移期 / 旧卡未导入)
    meta_file = card_path.with_suffix(".meta.json")
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            v = meta.get("confidence")
            if v is not None:
                return float(v)
        except Exception:
            pass

    # 3. markdown frontmatter fallback (兼容老卡)
    try:
        if card_path.exists():
            text = card_path.read_text(encoding="utf-8")
            parts = text.split("---", 2)
            if len(parts) >= 2:
                import yaml as _y
                try:
                    meta = _y.safe_load(parts[1]) or {}
                    v = meta.get("confidence")
                    if v is not None:
                        return float(v)
                except Exception:
                    pass
    except Exception:
        pass
    return 0.3  # 旧卡默认 0.3（可配置阈值）

_THINK_BLOCK_RE = re.compile(r"<think>[\s\S]*?</think>")

# Generic terms excluded from entity/fact extraction (signal: carries no actionable info)
E1_DENY_LIST = [
    "今天", "昨天", "明天", "前天", "后天", "早上", "下午", "晚上",
    "那个", "这个", "哪个", "什么", "怎么", "为什么", "如何",
    "事情", "东西", "地方", "时候", "问题", "情况", "方式", "方法",
    "原因", "结果", "目的", "手段",
    "做", "说", "想", "看", "知道", "觉得", "认为", "感觉",
    "之前", "之后", "现在", "然后", "而且", "但是", "因为", "所以",
    "一般", "基本", "主要", "很多", "一些", "部分", "一些", "大量",
    "时候", "情况", "事情", "方面", "角度",
]

E1_SYSTEM_PROMPT = """你是一个身份提炼引擎。读下面的主题卡（记录用户的注意力分布和思考模式），输出你的"身份三问"：

## 我是谁
- 从主题卡揭示的注意力分布判断：用户在哪些领域持续投入大量思考？关注什么层面的问题？
- 从跨主题的行为模式判断：用户是什么样的人——他怎么分配精力、他重视什么、他厌恶什么？

## 怎么判断
- 从各主题的执行方式中抽象用户的默认问题判断路径
- 他面对一个陌生问题时，第一反应是什么（调研→源码→实验/讨论→验证）？
- 他在做技术决策时，优先考虑什么（可溯源/可回滚/最小污染/全链路完整）？

## 行为基线
- 从各主题的操作模式中提炼用户的工作纪律：编码习惯、沟通偏好、安全边界
- 哪些约束条件反复出现（数据完整性、路径一致性、隔离污染、避免假实现）？
- 他对质量验收的硬性标准是什么（生产路径真正接入、行为可观察、回归有输出）？

要求：每个维度 3-5 条核心判断。每条标注引用对应的主题卡标题作为来源证据。
不写项目进展清单，不写经验教训列表，不写叙事——只写身份特征和判断模式。

【结构化输出契约】
- 输出语言：与输入主题卡语言保持一致（一般为中文）。
- 章节顺序：严格三段 "## 我是谁" / "## 怎么判断" / "## 行为基线"，顺序不可调换。
- 每段 3-5 条 bullet，每条以"-" 开头。
- 每条末尾需引用对应主题卡标题作为来源证据（格式：(来源: <主题卡标题>) 或 [来源: ...]）。
- 不要叙事、不要时间线、不要项目进展。"""

IDENTITY_SHORT_PROMPT = """你是一个身份核心提炼引擎。根据下面这份最新的印，输出 300-500 字的第一人称身份核心。

必须覆盖三个维度：
1. 我是谁：我与用户的关系本质、角色定位与长期关注；
2. 怎么判断：我的决策偏好、优先级和面对问题时的第一反应；
3. 行为基线：我的工作纪律、快慢取舍、沟通方式与安全边界。

只提炼稳定的身份边界和判断模式，不写项目进展，不叙事，不写时间线，不添加印中不存在的属性。
语气直接，使用第一人称，使用与输入相同的语言（一般为中文），正文严格控制在 300-500 字。

【结构化输出契约】
- 第一人称单数（"我是..."、"我做..."），不要用第三方视角。
- 三个维度按顺序 1→2→3 组织，可分三段（换行分隔），不要编号列表。
- 字数硬约束：300-500 字（含标点）。"""

# 手帐机制 B: E1 写印后顺带生成"系统态势总览", 写入 situation_overview.md,
# prefetch 固定注入（毫秒级纯读 + mtime 缓存）。
SITUATION_OVERVIEW_PROMPT = """你是系统态势摘要引擎。读下面这份最新印的前 1000 字 + 最近 7 天的活跃主题，
输出 300-500 字的"系统态势总览" — 这是给"下一次对话开始时的我"的速览锚点。

必须覆盖三个维度，按以下顺序组织：

## 活跃主题
- 最近 7 天注意力投入最多的 3-5 个领域（按输入的主题列表判断）
- 每条一句话说明主题 + 当前进展（不要罗列，只挑真正活跃的）

## 当前关注与待办
- 当前正在推进的项目/任务/系统改造（基于印与主题列表）
- 任何阻塞、风险、或下一步明确动作

## 状态速记
- 系统健康度（手帐/插件/PG 链路是否正常）
- 时间锚点（"距上次 E1 已 X 天"之类）

要求：
- 简洁、面向决策，不要展开细节
- 用第三人称描述（"系统当前..."/"近期重点..."），不是印那种第一人称
- 使用与输入相同的语言（一般为中文），300-500 字
- 不添加输入中不存在的事实；输入为空时如实写"暂无新数据"
- 不要 Markdown 列表符号 + 加粗泛滥，最多三行 - 开头的要点

【结构化输出契约】
- 三段顺序固定："## 活跃主题" → "## 当前关注与待办" → "## 状态速记"。
- 字数 300-500 字（含标点）。
- 第三人称（"系统"/"近期"/"上次"，不要用"我"）。"""

CATEGORY_INSTRUCTIONS = {
    "decisions": "从这些决策卡中提炼架构脉络和关键权衡，讲为什么做这个决策",
    "lessons": "从这些教训卡中提炼踩坑模式和应对原则，抽象到规律层",
    "projects": "从这些项目卡中提炼进展状态、当前阻塞和下一步方向",
    "session_summaries": "这些已是对话摘要，请进一步抽象到认知模式和规律层",
    "system": "从这些系统卡中提炼配置变更、服务状态和架构变化",
    "topic": "这些主题卡记录了用户在不同领域的注意力投入和思考模式。从中提炼身份特征：他持续关注什么领域、他怎么判断和处理问题、他的工作纪律和偏好是什么。按三段组织输出：我是谁 / 怎么判断 / 行为基线。每条标注来自的主题卡标题。",
}

def synthesize_yin(
    config: dict | None = None,
    dry_run: bool = False,
    sim_now: "datetime | None" = None,  # 2026-08-24 G1A-R: 模拟时钟回放 — now 与卡片增量上界
    pool: Any = None,
    on_topics_commit=None,
) -> str:
    """E1 合成 — 读卡片 + 上一份印 → LLM → 写新 y/ 印

    Args:
        config: v3-core 配置 (V3Config 或 dict, None = resolve_config())
        dry_run: True = 只构造 prompt 不调 LLM
        sim_now: 模拟时钟回放
        pool: PgPool 实例 (可选)

    Returns:
        状态消息
    """
    cfg = config or resolve_config()
    # 解析可配置化的 prompt — 用户在 config.yaml 的 prompts.<key> 可覆盖
    e1_system_prompt = _cfg_prompt(cfg, "e1_system", E1_SYSTEM_PROMPT)
    identity_short_prompt = _cfg_prompt(cfg, "e1_identity_short", IDENTITY_SHORT_PROMPT)
    situation_overview_prompt = _cfg_prompt(cfg, "e1_situation_overview", SITUATION_OVERVIEW_PROMPT)
    e1_enabled, llm_api_key, confidence_threshold = _extract_e1_cfg(cfg)
    if not e1_enabled:
        return "E1 已禁用 (config.yaml e1.enabled=false)"

    import os as _e1_os
    _e1_key = llm_api_key or _e1_os.environ.get("MINIMAX_CN_API_KEY", "") or _e1_os.environ.get("MINIMAX_API_KEY", "")
    if not _e1_key:
        return "E1 跳过: LLM api_key 未配置"

    store = DeepStore(cfg)
    base_path = store.base

    # ── 读 E1 状态 ──
    state_file = base_path / "state.json"
    last_e1 = datetime.fromtimestamp(0)
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            last_e1_str = (state.get("processed", {}) or {}).get("lastE1Run")
            if last_e1_str:
                last_e1 = datetime.fromisoformat(last_e1_str)
        except Exception:
            pass

    now = sim_now or datetime.now()  # 2026-08-24 G1A-R: 回放时用模拟时钟

    # ── 读上一份印 ──
    y_dir = base_path / "y"
    previous_yin = "(尚无上一份印)"
    if y_dir.exists():
        y_files = sorted(y_dir.glob("y_*.md"), reverse=True)
        if y_files:
            try:
                # 2026-08-09: 去掉 [:4000] 截断 — 上一份印全文喂入 (用户红线: 禁止默认截断,
                # 截断 = 印的后段(怎么判断/行为基线) 对 E1 合成不可见 → 每轮 E1 都缺上下文)
                _raw_yin = y_files[0].read_text(encoding="utf-8")
                _raw_yin = _THINK_BLOCK_RE.sub("", _raw_yin).strip()
                previous_yin = _raw_yin
            except Exception:
                pass

    # ── 2026-08-06 Step 3a: 雷达候选 → LLM 合并判断 → merge（先整理再读卡）──
    # 失败不阻塞 E1 主流程（雷达/LLM 任何一步挂了都跳过合并, 继续生成印）
    # on_topics_commit fires only after the early PG lease/connection is closed.
    merged = False
    try:
        _pg_early = _get_cfg_pg_conn_compat(cfg, pool)
        if _pg_early is not None:
            try:
                from .topic_radar import radar_scan
                from .topic_store import TopicStore
                _radar = radar_scan(_pg_early, max_dups=20)
                if _radar["duplicates"]:
                    _store = TopicStore()
                    _m, _s, _log = _merge_duplicate_topics(
                        cfg, _store, _pg_early, _radar["duplicates"]
                    )
                    if _m:
                        merged = True
                    logger.info(
                        "[e1] 主题卡整理: 扫描=%d 重复候选=%d merged=%d skipped=%d\n%s",
                        _radar["scanned"], _radar["dup_total"], _m, _s, _log[:600],
                    )
            finally:
                try:
                    _pg_early.close()
                except Exception:
                    pass
    except Exception as e:
        logger.warning("[e1] 雷达合并跳过 (不阻塞): %s", _safe_err(e)[:150])
        merged = False
    if merged and callable(on_topics_commit):
        try:
            on_topics_commit()
        except Exception as _e_cb:
            logger.warning("[e1] on_topics_commit 失败 (非阻塞): %s", _safe_err(_e_cb)[:150])

    # ── 读 last_e1 之后的新卡片 ──
    # 首选 PG topics 表 (增量提炼新写路径); PG 失败/无数据时降级 SQLite v3_cards.db
    new_cards_text = "(本次无新卡片)"
    new_count = 0
    filtered_count = 0
    category_source_ids: dict[str, list[str]] = {}

    # --- PG 读取分支 (首选) ---
    pg_topics_text = "(本次无新卡片)"
    pg_topic_count = 0
    try:
        _pg = _get_cfg_pg_conn_compat(cfg, pool)
        if _pg is not None:
            _pg_rows = []
            try:
                with _pg.cursor() as _cur:
                    # 全量 active topic 卡（生产实测 557 张 / 370KB, M3 44s 处理完成）
                    # 首次全量读，后续只读上次 E1 之后新增的 topic（增量织入）
                    _sql = """
                        SELECT title, COALESCE(body, ''), created_at
                        FROM topics
                        WHERE status = 'active' AND body IS NOT NULL AND length(body) > 50
                    """
                    _params: list[Any] = []
                    # Windows: 不能用 last_e1.timestamp() > 0 判定——fromtimestamp(0).timestamp() 抛 OSError
                    _has_last_e1 = last_e1.year > 1970 or (last_e1.year == 1970 and last_e1.month > 1)
                    if _has_last_e1:
                        # 有上次 E1 记录 → 增量模式
                        _sql += " AND created_at > %s"
                        _params.append(last_e1)
                    if sim_now is not None:
                        # 2026-08-24 G1A-R: 回放模式 — 增量上界=模拟 now, 防止"看到未来卡"
                        _sql += " AND created_at <= %s"
                        _params.append(sim_now)
                    _sql += " ORDER BY length(body) DESC"
                    _cur.execute(_sql, _params)
                    _pg_rows = _cur.fetchall()
            finally:
                try:
                    _pg.close()
                except Exception:
                    pass
            if _pg_rows:
                _lines = []
                for title, body, created_at in _pg_rows:
                    preview = (body or "")[:300].replace("\n", " ")
                    dt = (str(created_at or ""))[:10]
                    _lines.append(f"- [{dt}] {title}: {preview}")
                    category_source_ids.setdefault("topic", []).append(f"topic/{title}")
                if _lines:
                    inst = CATEGORY_INSTRUCTIONS.get("topic", "")
                    topic_block = "\n".join(_lines)
                    if inst:
                        pg_topics_text = f"### topic\n> {inst}\n{topic_block}"
                    else:
                        pg_topics_text = f"### topic\n{topic_block}"
                    pg_topic_count = len(_pg_rows)
                    logger.info("[e1] 加载 %d 张 topic 卡 (PG)", pg_topic_count)
    except Exception as e:
        logger.warning("[e1] PG topic 卡读取失败 (降级 SQLite): %s", _safe_err(e)[:100])

    # --- SQLite 读取分支 (兑底, 仅在 PG 没读到数据时启用) ---
    if pg_topic_count == 0:
        try:
            cards_db = base_path / "v3_cards.db"
            if cards_db.exists():
                import sqlite3 as _e1_s3
                _c = _e1_s3.connect(str(cards_db))
                # 全量 topic（与 PG 分支一致）
                _sql = '''SELECT title, content, tags, updated_at FROM cards
                    WHERE category = 'topic' AND content IS NOT NULL AND length(content) > 50'''
                _params_sqlite: list[Any] = []
                if _has_last_e1:
                    _sql += ' AND updated_at > ?'
                    _params_sqlite.append(last_e1.isoformat())
                _sql += ' ORDER BY length(content) DESC'
                topic_rows = _c.execute(_sql, _params_sqlite).fetchall()
                _c.close()
                if topic_rows:
                    topic_lines = []
                    for title, content, tags, updated_at in topic_rows:
                        preview = content[:300].replace("\n", " ")
                        dt = (updated_at or "")[:10]
                        topic_lines.append(f"- [{dt}] {title}: {preview}")
                        source_id = f"topic/{title}"
                        category_source_ids.setdefault("topic", []).append(source_id)
                    if topic_lines:
                        inst = CATEGORY_INSTRUCTIONS.get("topic", "")
                        topic_block = "\n".join(topic_lines)
                        if inst:
                            new_cards_text = f"### topic\n> {inst}\n{topic_block}"
                        else:
                            new_cards_text = f"### topic\n{topic_block}"
                        new_count = len(topic_rows)
                        logger.info("[e1] 加载 %d 张 topic 卡 (SQLite 兑底)", new_count)
        except Exception as e:
            logger.warning("[e1] SQLite topic 卡读取失败 (不阻塞): %s", _safe_err(e)[:100])
    else:
        # PG 读到了, 直接用 PG 结果
        new_cards_text = pg_topics_text
        new_count = pg_topic_count

    # ── 2026-08-06 Step 3b: 读观察者最新印（打通 E1 ↔ 观察者衔接）──
    # E1 印生成时带上观察者滚动印的叙事上下文（最新版, 截断 4000 字）
    observer_note_text = "(无观察者印)"
    try:
        _pg_obs = _get_cfg_pg_conn_compat(cfg, pool)
        if _pg_obs is not None:
            try:
                with _pg_obs.cursor() as _cur:
                    # 2026-08-24 G1A-R: 参数化查询时 LIKE 通配符 % 必须写成 %%
                    # (psycopg2 把裸 % 当格式占位符; 项目先例见 _select_latest_observer_note)
                    _obs_sql = (
                        "SELECT content FROM observation_notes "
                        "WHERE version LIKE 'v%' "
                        + ("AND created_at <= %s " if sim_now is not None else "")  # 回放不偷看未来印
                        + "ORDER BY id DESC LIMIT 1"
                    )
                    _obs_params = None if sim_now is None else (sim_now,)
                    if _obs_params:
                        _cur.execute(_obs_sql.replace("version LIKE 'v%'", "version LIKE 'v%%'"), _obs_params)
                    else:
                        _cur.execute(_obs_sql)
                    _row = _cur.fetchone()
                    if _row and _row[0]:
                        _note = _row[0]
                        # 2026-08-09: 去掉 [:4000] 截断 — 观察者印全文喂入 (用户红线: 禁止默认截断)
                        observer_note_text = _note
            finally:
                try:
                    _pg_obs.close()
                except Exception:
                    pass
    except Exception as e:
        logger.warning("[e1] 读观察者印失败 (不阻塞): %s", _safe_err(e)[:120])

    # ── 构建 user payload ──
    user_payload = (
        f"## 时间上下文\n"
        f"Current Date: {now.strftime('%Y-%m-%d %H:%M')} (当前时间)\n"
        f"Observation Period: {last_e1.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d')} (这期间产生的卡片)\n\n"
        f"## 观察者最新印（记忆叙事上下文）\n\n{observer_note_text}\n\n"
        f"## 上一份印\n\n{previous_yin}\n\n"
        f"## 最近 {new_count} 张新卡片\n\n{new_cards_text}\n\n"
        f"---\n"
        f"按上面每类卡片的指令分别提炼，输出结构化摘要。\n"
        f"注意：卡片中出现的'昨天/上周/N天前'等相对时间，应基于该卡片产生时的实际时间理解，"
        f"而非基于当前时间。Current Date 仅用于你撰写叙事时的时间锚点。"
    )


    if dry_run:
        return (
            f"=== E1 DRY RUN ===\n"
            f"上次 E1: {last_e1.isoformat()}\n"
            f"新卡片数: {new_count}\n"
            f"置信度过滤: {filtered_count} 张 (threshold={confidence_threshold})\n"
            f"user_payload 长度: {len(user_payload)} 字符\n"
            f"--- user_payload 前 300 字 ---\n{user_payload[:300]}"
        )

    # ── 调 LLM ──
    try:
        client = LLMClient(cfg)
        y_content = client.chat(e1_system_prompt, [
            {"role": "user", "content": user_payload},
        ], temperature=0.3)
    except Exception as e:
        return f"E1 合成失败: {_safe_err(e)}"

    y_content = _THINK_BLOCK_RE.sub("", y_content).strip()
    if not y_content:
        return "E1 合成失败: LLM 返回空"

    # ── 注入 premise 注释 ──
    # 印的每个段落来自哪些分类的 b 卡，记录为 HTML 注释（对人不可见，对程序可解析）。
    # chain_recall 阶段可用 premise IDs 做精确匹配召回 (双路径: 向量相似度 + premise 精确匹配)。
    # Premise 注释插在 ## 段标题之后、段内容之前 — 这样 _ingest_yin_segments 按
    # \n## 切分时, 每个 section 的 body 都会包含属于自己的 premise 注释, 让 premise
    # 元信息随 segment 向量一同入库 (供链式召回做精确匹配).
    SECTION_CATEGORY_MAP = {
        "关键决策": ["decisions"],
        "关键教训": ["lessons"],
        "关键项目进展": ["projects"],
        "叙事": ["session_summaries", "system", "shou_zhang"],
        "关注领域": ["topic"],
    }
    enriched_lines: list[str] = []
    for line in y_content.split("\n"):
        enriched_lines.append(line)
        stripped = line.strip()
        for section_name, cats in SECTION_CATEGORY_MAP.items():
            if stripped == f"## {section_name}":
                premise_ids: list[str] = []
                for cat in cats:
                    premise_ids.extend(category_source_ids.get(cat, []))
                if premise_ids:
                    premise_str = ", ".join(premise_ids)
                    enriched_lines.append(f"<!-- premise: {premise_str} -->")
                break
    y_content = "\n".join(enriched_lines)

    # ── 写新印 ──
    # 写文件名也用模拟时钟 (2026-08-24 G1A-R: 回放产物按事件日期命名, sorted顺序=时间序)
    ts = now.strftime("%Y-%m-%d_%H%M%S")
    y_dir.mkdir(parents=True, exist_ok=True)
    target = y_dir / f"y_{ts}.md"

    header = (
        "> [v3 印层 / 自我叙事]\n"
        "> 这份印记录了我对自我的持续理解——"
        "基于最近经历整合而成的叙事，不是事实清单。\n"
        "> 同一事实与 earlier 印冲突时，以本印为准。\n\n---\n\n"
    )

    try:
        target.write_text(header + y_content, encoding="utf-8")
        _ingest_yin_segments(y_content, cfg, ts, pool=pool)

        # ── 2026-08-06 Step 3c: 印进召回池（切段入库 yin_paragraphs）──
        # 失败不阻塞（印已写文件; 入库失败下次 E1 重新切段）
        try:
            from .yin_pool import ingest_yin
            from .embedding import safe_embed_cfg as _safe_embed_cfg
            # 阶段1 (2026-08-20): 走 build_embed_cfg 工厂; 缺 model/endpoint → 不传 cfg
            _ecfg = _safe_embed_cfg(cfg)
            _pg_yin = _get_cfg_pg_conn_compat(cfg, pool)
            if _pg_yin is not None:
                try:
                    _ingested = ingest_yin(
                        y_content, target.name, _pg_yin, _ecfg
                    )
                    logger.info("[e1] 印段落入库 yin_paragraphs: %d 段 (%s)", _ingested, target.name)
                finally:
                    try:
                        _pg_yin.close()
                    except Exception:
                        pass
        except ValueError:
            raise
        except Exception as e:
            logger.warning("[e1] 印入库失败 (不阻塞): %s", _safe_err(e)[:150])

        # ── PG 双写 ──
        try:
            pg_conn = _get_cfg_pg_conn_compat(cfg, pool)
            if pg_conn:
                try:
                    with pg_conn.cursor() as cur:
                        # 旧版 is_current 清 false
                        cur.execute("UPDATE yin SET is_current=false WHERE is_current=true")
                        # 新版本写入
                        version_str = ts  # e.g. "2026-07-14_141119"
                        title_line = ""
                        for line in y_content.split("\n"):
                            if line.startswith("# "):
                                title_line = line[2:].strip()
                                break
                        cur.execute(
                            "INSERT INTO yin (version, title, content, created_at, is_current) "
                            "VALUES (%s, %s, %s, %s, true) "
                            "ON CONFLICT DO NOTHING",
                            (version_str, title_line, y_content, now)
                        )
                        # 也写 yin_history
                        cur.execute(
                            "INSERT INTO yin_history (version, title, content, created_at, is_current) "
                            "VALUES (%s, %s, %s, %s, true) ON CONFLICT DO NOTHING",
                            (version_str, title_line, y_content, now)
                        )
                    pg_conn.commit()
                    logger.info("[e1] yin 双写 PG: version=%s", version_str)
                finally:
                    try:
                        pg_conn.close()
                    except Exception:
                        pass
        except Exception as pg_e:
            logger.warning("[e1] yin PG 双写失败（文件已存，不阻塞）: %s", str(pg_e)[:150])
    except Exception as e:
        return f"E1 写文件失败: {_safe_err(e)}"

    # ── 写时生成身份短板 ──
    # 印已成功写文件并尝试 PG 双写；身份提炼失败不能回滚或阻塞 E1 主流程。
    try:
        identity_content = client.chat(identity_short_prompt, [
            # 2026-08-09: 去掉 [:4000] 截断 — 完整印喂身份提炼 (用户红线: 禁止默认截断)
            {"role": "user", "content": y_content},
        ], temperature=0.3)
        identity_content = _THINK_BLOCK_RE.sub("", identity_content).strip()
        if not identity_content:
            raise ValueError("LLM 返回空身份块")

        identity_path = base_path / "identity_block.md"
        identity_tmp_path = base_path / "identity_block.md.tmp"
        try:
            identity_tmp_path.write_text(identity_content, encoding="utf-8")
            os.replace(identity_tmp_path, identity_path)
        finally:
            if identity_tmp_path.exists():
                identity_tmp_path.unlink()
        logger.info("[e1] 身份短板已原子写入 (%d 字)", len(identity_content))
    except Exception as identity_e:
        logger.warning("[e1] 身份短板生成失败（印已存，不阻塞）: %s", str(identity_e)[:200])

    # ── 手帐机制 B：写时生成系统态势总览 ──
    # 独立 try/except，任何失败不阻塞 E1 主流程（与身份短板同模式）。
    # prefetch 走 _read_situation_overview() 纯读 + mtime 缓存，毫秒级返回。
    try:
        # 拉最近 7 天活跃主题（PG 失败 → 空列表，绝不阻塞）
        _sit_active_topics: list[str] = []
        try:
            _sit_pg = _get_cfg_pg_conn_compat(cfg, pool)
            if _sit_pg is not None:
                _sit_rows = []
                try:
                    with _sit_pg.cursor() as _sit_cur:
                        # 2026-08-24 G1A-R: 回放模式 — 7天窗口锚定模拟时钟而非PG墙钟
                        _sit_sql = """
                            SELECT title, COALESCE(summary, ''), updated_at
                            FROM topics
                            WHERE status = 'active'
                              AND updated_at > %s::timestamptz - interval '7 days'
                            ORDER BY updated_at DESC
                            LIMIT 15
                        """
                        _sit_cur.execute(_sit_sql, ((sim_now or datetime.now(timezone.utc)),))
                        _sit_rows = _sit_cur.fetchall()
                finally:
                    try:
                        _sit_pg.close()
                    except Exception:
                        pass
                for _st, _ss, _sa in _sit_rows:
                    _sit_active_topics.append(f"- [{(_sa or '')[:10]}] {_st}: {(_ss or '')[:120]}")
        except Exception as _sit_pg_e:
            logger.debug("[e1] 态势总览 PG 读取失败（用空列表兜底）: %s", str(_sit_pg_e)[:100])

        # 构建 LLM 输入：印前 1000 字 + 活跃主题列表
        _sit_user = (
            "## 最新印前 1000 字\n\n"
            + y_content[:1000]
            + "\n\n## 最近 7 天活跃主题\n\n"
            + ("\n".join(_sit_active_topics) if _sit_active_topics else "（暂无近 7 天新主题）")
        )

        _sit_content = client.chat(situation_overview_prompt, [
            {"role": "user", "content": _sit_user},
        ], temperature=0.3)
        _sit_content = _THINK_BLOCK_RE.sub("", _sit_content).strip()
        if not _sit_content:
            raise ValueError("LLM 返回空态势总览")

        _sit_path = base_path / "situation_overview.md"
        _sit_tmp = base_path / "situation_overview.md.tmp"
        try:
            _sit_tmp.write_text(_sit_content, encoding="utf-8")
            os.replace(_sit_tmp, _sit_path)
        finally:
            if _sit_tmp.exists():
                _sit_tmp.unlink()
        logger.info(
            "[e1] 态势总览已原子写入 (%d 字, %d 个活跃主题)",
            len(_sit_content), len(_sit_active_topics),
        )
    except Exception as _sit_e:
        logger.warning("[e1] 态势总览生成失败（印/身份已存，不阻塞）: %s", str(_sit_e)[:200])

    # ── 更新状态 ──
    try:
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
        else:
            state = {"processed": {}}
        # 存 UTC aware isoformat — topics.created_at 是 timestamptz(UTC),
        # naive 本地时间会导致增量比较错位 8h（2026-08-01 实测 E1 读 0 张）
        state.setdefault("processed", {})["lastE1Run"] = (
            (sim_now or datetime.now(timezone.utc)).isoformat()  # 2026-08-24 G1A-R: 回放时写模拟时钟
        )
        state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("[e1] 状态更新失败 (不阻塞): %s", _safe_err(e)[:200])

    return f"E1 完成: {new_count} 张卡片 (过滤 {filtered_count}) → {target.name}"

def merge_topic_pg(pg, source_id: str, target_id: str,
                   new_title: str = '', new_summary: str = '', new_body: str = '') -> tuple[bool, str]:
    """纯 PG 主题卡合并（2026-08-06 生产真值在 PG topics — SQLite 双写断链后不用）。

    观察者重建后 SQLite topic_blocks 只剩 80 张（PG 688 是真值）— merge 直接操作 PG:
    entries 迁移 → target 更新（body 拼接保留双方）→ source 清理。
    """
    try:
        conn = pg._connect() if hasattr(pg, "_connect") else pg
        if not conn:
            return False, "PG 连接失败"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, summary, body, keywords FROM topics WHERE topic_id=%s",
                (target_id,),
            )
            tgt = cur.fetchone()
            cur.execute(
                "SELECT title, summary, body, keywords FROM topics WHERE topic_id=%s",
                (source_id,),
            )
            src = cur.fetchone()
            if not tgt:
                return False, f"target 不存在: {target_id}"
            if not src:
                return False, f"source 不存在: {source_id}"
            if source_id == target_id:
                return False, "source == target"

            # 1. entries 迁移（统计 + 更新）
            cur.execute("SELECT count(*) FROM topic_entries WHERE topic_id=%s", (source_id,))
            cnt = cur.fetchone()[0]
            cur.execute(
                "UPDATE topic_entries SET topic_id=%s WHERE topic_id=%s",
                (target_id, source_id),
            )
            # 2. target 更新（body 拼接保留双方; keywords 合并; embedding 保留 target 原值）
            body = new_body or ((tgt[2] or "") + "\n\n---\n" + (src[2] or "")).strip()
            summary = new_summary or tgt[1] or ""
            title = new_title or tgt[0] or ""
            kw = []
            try:
                import json as _json
                for k in (tgt[3], src[3]):
                    if isinstance(k, str):
                        kw.extend(_json.loads(k))
                    elif isinstance(k, (list, tuple)):
                        kw.extend(k)
            except Exception:
                pass
            kw = list(dict.fromkeys(x for x in kw if x))  # 去重保序
            cur.execute(
                "UPDATE topics SET title=%s, summary=%s, body=%s, keywords=%s WHERE topic_id=%s",
                (title, summary, body, kw, target_id),
            )
            # 3. source 清理（topics + entries 残留）
            cur.execute("DELETE FROM topics WHERE topic_id=%s", (source_id,))
            cur.execute("DELETE FROM topic_entries WHERE topic_id=%s", (source_id,))
        conn.commit()
        return True, f"merged {source_id} → {target_id} (entries moved: {cnt})"
    except Exception as e:
        return False, str(e)[:180]


def _merge_duplicate_topics(cfg, store, pg, radar_pairs, top_n: int = 20):
    """雷达候选 → LLM 批量判断 → merge 执行（2026-08-06 Step 3a 落地）。

    输入: radar_scan(pg) 的 duplicates 前 top_n 对
    流程: 标题对 → LLM 一次调用判断是否同一主题 → merge=true 的执行 store.merge_topic
    返回: (merged_count, skipped_count, 日志字符串)
    """
    if not radar_pairs:
        return 0, 0, "无候选"
    pairs = radar_pairs[:top_n]
    pair_lines = []
    for idx, p in enumerate(pairs, 1):
        pair_lines.append(
            f"{idx}. A[{p['source_id']}] {p['source_title']} ||| "
            f"B[{p['target_id']}] {p['target_title']} (cos={p['cosine']})"
        )
    prompt = (
        "判断以下主题卡对是否属于同一主题（语义重复、应合并为一张卡）。\n"
        "规则: 标题高度相似 / 同一话题的不同表述 / 同一工作不同阶段 = merge=true; "
        "不同话题、不同侧面、需要各自保留 = merge=false。\n"
        "只输出 JSON 数组，如 [{\"i\": 1, \"merge\": true}, {\"i\": 2, \"merge\": false}]，不要其他文字。\n\n"
        + "\n".join(pair_lines)
    )
    try:
        from .llm import LLMClient
        client = LLMClient(cfg)
        raw = client.chat(
            "你是记忆主题卡整理器。判断主题卡对是否重复。",
            [{"role": "user", "content": prompt}],
            temperature=0.1,
        )
    except Exception as e:
        return 0, 0, f"LLM 判断失败: {_safe_err(e)}"

    # 容错解析 JSON
    decisions = {}
    try:
        import re as _re
        m = _re.search(r"\[[\s\S]*\]", raw or "")
        if m:
            arr = json.loads(m.group(0))
            for item in arr:
                if isinstance(item, dict) and "i" in item:
                    decisions[int(item.get("i"))] = bool(item.get("merge"))
    except Exception as e:
        logger.warning("[e1] 合并判断 JSON 解析失败, 全部跳过: %s", _safe_err(e)[:120])
        return 0, len(pairs), f"JSON 解析失败: {_safe_err(e)[:80]}"

    merged, skipped = 0, 0
    log_parts = []
    for idx, p in enumerate(pairs, 1):
        if decisions.get(idx):
            ok, msg = merge_topic_pg(
                pg, p["source_id"], p["target_id"],
                # 结构性合并: 保留双方内容（body 拼接由 merge 内做）
            )
            if ok:
                merged += 1
                log_parts.append(f"  merged: {p['source_id']} → {p['target_id']} ({p['source_title'][:20]} ⇔ {p['target_title'][:20]})")
            else:
                skipped += 1
                log_parts.append(f"  merge 失败: {msg}")
        else:
            skipped += 1
    return merged, skipped, "\n".join(log_parts)


def _match_topics_by_embedding(seg_emb, top_k: int = 3, threshold: float = 0.55, pool: Any = None):
    """段落向量 → 主题卡 cosine 匹配 top-k（返回 topic_id 列表）

    主题卡 embedding 从 PG topics 表加载（一次查询全量，秒级）。
    threshold 兜底: cosine 低于阈值不关联（宁可少关联不错关联）。
    """
    try:
        import numpy as _np
        from .pg_store import PgEmbedStore as _Pg
        from .config import resolve_config as _rc
        cfg = _rc()
        pg = _Pg(cfg, pool=pool)
        rows = []
        try:
            with pg.lease() as conn:
                if conn is not None:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT topic_id, embedding FROM topics "
                            "WHERE status='active' AND embedding IS NOT NULL"
                        )
                        rows = cur.fetchall()
        finally:
            try:
                pg.close()
            except Exception:
                pass
        if not rows:
            return []
        seg_arr = _np.asarray(seg_emb, dtype=_np.float32)
        seg_norm = float(_np.linalg.norm(seg_arr))
        if seg_norm < 1e-10:
            return []
        scored = []
        for tid, emb_vec in rows:
            try:
                if isinstance(emb_vec, str):
                    import json as _j
                    emb_vec = _j.loads(emb_vec)
                t_arr = _np.asarray(emb_vec, dtype=_np.float32)
            except (ValueError, TypeError):
                continue
            tnorm = float(_np.linalg.norm(t_arr))
            if tnorm < 1e-10:
                continue
            cos = float(_np.dot(seg_arr, t_arr) / (seg_norm * tnorm))
            scored.append((cos, tid))
        scored.sort(reverse=True)
        return [tid for cos, tid in scored[:top_k] if cos >= threshold]
    except Exception:
        return []


def _ingest_yin_segments(y_content, config, ts, pool: Any = None):
    try:
        from .embedding import call_embedding as _ce
        from .pg_store import PgEmbedStore as _Pg
    except ImportError:
        return
    # 阶段1 (2026-08-20): 改走 build_embed_cfg 工厂, 缺 model/endpoint → disabled 不抛
    from .embedding import safe_embed_cfg as _safe_embed_cfg
    embed_cfg = _safe_embed_cfg(config)
    if embed_cfg is None:
        return
    sections = [s for s in y_content.strip().split("\n") if s.startswith("## ")]
    if not sections:
        return
    try:
        pg = _Pg(config, pool=pool)
        try:
            for i, title_line in enumerate(sections):
                title = title_line[3:].strip()
                if i < len(sections) - 1:
                    body = y_content.split(title_line)[1].split("\n## ")[0].strip()
                else:
                    body = y_content.split(title_line)[1].strip()
                # Strip any TRAILING HTML premise comment lines that belong to the
                # NEXT section but got included by the split-on-\n## above.
                # (The current section's own premise comment lives at the start
                # of body, so it stays — and gets embedded into the segment vector
                # so chain_recall can find it via _parse_premise_ids later.)
                import re as _e1_re
                body = _e1_re.sub(r"(?:\n?<!--\s*premise:[^>]*-->\s*)+$", "", body).strip()
                if len(body) < 100:
                    continue
                seg_text = "## " + title + "\n\n" + body
                emb = _ce(seg_text[:2000], embed_cfg)
                if emb:
                    # 段落 → 主题卡关联: 段向量 vs 主题卡矩阵 cosine top-3
                    # (主题卡 embedding 在 PG topics 表, 加载一次复用)
                    topic_ids = _match_topics_by_embedding(emb, pool=pool)
                    pg.insert_effective(
                        source_id="e1_seg_" + ts + "_" + str(i+1),
                        pool_role="yin_segment",
                        title="E1/" + title[:50],
                        content=seg_text[:5000],
                        embedding=emb,
                        embed_cfg=embed_cfg,
                        scope_id="all",
                        metadata={"topic_ids": topic_ids} if topic_ids else None,
                    )
        finally:
            try:
                pg.close()
            except Exception:
                pass
    except ValueError:
        raise
    except Exception:
        pass


# ────────────────────────────────────────────────────────────
# E1 第②件事: 压缩观察者印 (2026-08-09 落地, 用户设计意图)
# 设计: E1 两件事 = ①读 topic 卡写 e1 印(身份层) ②读观察者印压缩写回(防膨胀)
# 之前只有①, ②断链 → 观察者印膨胀到 7-8 万字无压缩 → 早期信息被挤出
# ────────────────────────────────────────────────────────────
# 2026-08-22 重写 (两端 prompt 失同步修复): writer (observer.py 8/20 v4) 已改
# 「按需输出/长度按变化量伸缩」, 本压缩端仍停留在旧哲学「保留全部关键信息 +
# 8 节一个不能少」— 实测 v354 (12014 字) 三次压缩均输出 ~11.4K (95%), 根因是
# 规则自相矛盾: 强制保留的内容合计 > 长度上限, 模型只能弃长度保内容。
# 新哲学与 writer 对齐: 压缩 = 去冗余重写 (折叠无变化卡/合并稳定条目),
# 长度是与保留规则同级的硬约束并给出明确取舍顺序。
# 配置化: 走 _resolve_prompt (prompts.e1_observer_compress 可覆盖, 用户不配 = 内置默认)。
# 2026-08-22 v2 (v354 实测 10 组对照实验定稿): M3 对「压缩 X」指令系统性弱遵从
# (复述率 82-100%), 唯一稳定达标配方 = Hermes 内置上下文压缩同款三要素:
#   ① checkpoint summarization 任务框架 (不是"压缩"措辞)
#   ② 对话轮次形态输入 (note_to_dialog_turns 转换, 不是散文体)
#   ③ 每节条数硬上限 (离散约束, 比总字数软引导遵从性高)
# 单次调用仍有方差 (60%-88% 乱跳), 由调用侧迭代循环兜底 (超限→上轮输出转对话形态再压)。
_OBS_COMPRESS_PROMPT = """You are a summarization agent creating a context checkpoint. Treat the turns below as source material. Produce only the structured summary.

Hard limits per section (exceeding them makes the output invalid):
- 我的故事: max 1 paragraph <=200 字
- 关键决策: max 5 bullets
- 进行中状态: max 6 bullets, one line each
- 教训与踩坑: max 6 bullets, one line each
- 操作知识: max 7 bullets, one line each (paths/IDs/commands verbatim)
- 主题变化: max 3 bullets
- 系统状态摘要: one line per card, total <=600 字
- 关系记忆: all facts kept, <=150 字

Write in Chinese. Keep dates (YYYY-MM-DD). Total output must be <=4000 字."""


def _note_to_dialog_turns(note_content: str) -> str:
    """把散文体观察者印转成对话轮次形态 (M3 checkpoint 遵从性实验结论)。

    M3 对「对对话记录写检查点摘要」任务遵从性高, 对「压缩散文体」弱遵从
    (2026-08-22 v354 十组对照实测: 散文体 82-100% 复述率 vs 对话形态 60-69%)。
    纯代码转换, 零 LLM 成本。
    """
    import re as _re
    parts = _re.split(r"\n(?=#{2,4} )", note_content)
    turns = []
    for _i, sec in enumerate(parts):
        if not sec.strip():
            continue
        lines = sec.split("\n", 1)
        title = lines[0].lstrip("#").strip()[:40]
        body = lines[1].strip() if len(lines) > 1 else ""
        turns.append(f"--- TURN {_i + 1} [{title}] ---\n{body}")
    return "\n\n".join(turns)


def _iterative_compress_note(
    client,
    base_prompt: str,
    note_content: str,
    max_chars: int,
    max_attempts: int,
):
    """迭代压缩循环: 压缩→校验→超限则把上轮输出转对话形态+加严指令再压。

    返回 (compressed_text | "", last_err)。单次调用方差大 (60%-88% 实测),
    循环兜底; 全部失败返回空串 (调用侧 fail-closed 不写盘)。
    """
    cur_input = _note_to_dialog_turns(note_content)
    compressed = ""
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        try:
            _out = client.chat(base_prompt, [
                {"role": "user", "content": cur_input},
            ], temperature=0.2)
        except Exception as _e_llm:
            last_err = f"attempt={attempt} LLM 调用异常: {_safe_err(_e_llm)[:120]}"
            logger.warning("e1: 迭代压缩 %s", last_err)
            continue
        _out = (_out or "").strip()
        _ok, _reason = _validate_compressed_observer_note(
            compressed_text=_out, max_chars=max_chars,
        )
        if _ok:
            return _out, ""
        last_err = f"attempt={attempt} {_reason}"
        logger.warning("e1: 迭代压缩 %s — 下轮以上轮输出为输入加压", last_err)
        # 超限输出转回对话形态, 附带加严指令再压
        import re as _re2
        olines = _re2.split(r"\n(?=#{2,4} )", _out)
        oturns = []
        for _j, sec in enumerate(olines):
            if not sec.strip():
                continue
            ls = sec.split("\n", 1)
            title = ls[0].lstrip("#").strip()[:40]
            body = ls[1].strip() if len(ls) > 1 else ""
            oturns.append(f"--- TURN {_j + 1} [{title}] ---\n{body}")
        cur_input = (
            "\n\n".join(oturns)
            + f"\n\n[NOTE: previous checkpoint was {len(_out)} chars — still too long. "
              f"Cut every section to HALF its current length. Merge aggressively.]"
        )
    return compressed, last_err



# ─────────────────────────────────────────────────────────────────────────
# 闭锁观察者印链选择器 (2026-08-21 闭锁):
#   任何「最新观察者印」读路径 (observer 1.5 段膨胀检查 / observer 2 段 prev_text /
#   e1.compress_observer_note / recall_for_new_session) 都必须显式排除
#   session_summary_* 行, 只接受 version LIKE 'v%' (含 v*-compressed)。
#   之前 ORDER BY id DESC LIMIT 1 在生产把 id=356 (session_summary) 当观察者印,
#   导致压缩链永远打不中目标。
# ─────────────────────────────────────────────────────────────────────────
_LATEST_OBSERVER_NOTE_SELECT = (
    "SELECT id, version, content, source_qa_range, prev_id, links "
    "FROM observation_notes WHERE version LIKE 'v%' "
    "ORDER BY id DESC LIMIT 1"
)


def _select_latest_observer_note(
    pg,
    *,
    notes_table: str = "observation_notes",
    target_note_id: int | None = None,
) -> tuple | None:
    """读取观察者链中最新的 note，排除 session_summary_*。"""
    table = _safe_notes_table(str(notes_table))
    if table == "observation_notes" and target_note_id is None:
        sql = _LATEST_OBSERVER_NOTE_SELECT
        params: tuple[Any, ...] = ()
    else:
        sql = (
            "SELECT id, version, content, source_qa_range, prev_id, links "
            f"FROM {table} WHERE version LIKE 'v%'"
        )
        params = ()
        if target_note_id is not None:
            sql += " AND id=%s"
            params = (int(target_note_id),)
        sql += " ORDER BY id DESC LIMIT 1"
    with pg.cursor() as cur:
        if params:
            # psycopg2 参数化 SQL 中的 LIKE 通配符必须写成 %%，否则会被
            # 当作格式占位符；无参数分支直接执行原 SQL，保持查询可读。
            cur.execute(sql.replace("version LIKE 'v%'", "version LIKE 'v%%'"), params)
        else:
            cur.execute(sql)
        return cur.fetchone()


# ─────────────────────────────────────────────────────────────────────────
# 压缩结果校验器 (2026-08-21 闭锁):
#   纯校验, 不修改任何文本, 不截断。失败只返 (False, reason)。
#   任何 INSERT / COMMIT 之前必须经此函数过一遍。
# ─────────────────────────────────────────────────────────────────────────
def _validate_compressed_observer_note(
    *,
    compressed_text: str,
    max_chars: int,
    min_chars: int = 500,
) -> tuple[bool, str]:
    """纯校验, 不修改任何文本。

    Returns:
        (accepted, reason): True 表示通过, False + reason 表示拒绝 (含关键词
        "失败"/"超长"/"上限" 之一, 让 caller 的失败分支匹配关键词即可识别)。
    """
    txt = (compressed_text or "").strip()
    if not txt:
        return False, f"E1 压缩观察者印失败: 空输出 (max_chars={max_chars})"
    n = len(txt)
    if n > max_chars:
        return False, (
            f"E1 压缩观察者印失败: 压缩结果超上限 "
            f"({n} 字 > max_chars={max_chars}, 拒绝入库, 不截断)"
        )
    if n < min_chars:
        return False, (
            f"E1 压缩观察者印失败: 压缩结果过短 ({n} 字 < min={min_chars}, 拒绝入库)"
        )
    return True, "ok"


class E1CompressionFailure(Exception):
    """E1 压缩失败 — 严格路径用。

    严格路径 (compress_observer_note_strict) 不允许静默兜底: 一旦抛出本异常,
    caller (observer.py 1.5 段) 必须把整批观察短路:
      - 不调 synthesize_yin (E1 主印后续)
      - 不写本轮 observation_note
      - 不推进 cursor
      - 必须 _save_failed_segment(error_class="e1_compression_*") 让原始 QA 保留待重试
    """


def compress_observer_note(
    config: dict | None = None,
    *,
    dry_run: bool = False,
    target_ratio: float = 0.5,
    notes_table: str = "observation_notes",
    target_note_id: int | None = None,
    pool: Any = None,
) -> str:
    """读观察者最新印 → LLM 压缩 → 写回新版本 (防膨胀)。

    兼容旧 caller；observer 的生产热路径使用
    :func:`compress_observer_note_strict`，以获得明确的失败信号。
    """
    cfg = _legacy_config(config)
    if notes_table == "observation_notes":
        notes_table = str(cfg.get("notes_table", notes_table))
    notes_table = _safe_notes_table(notes_table)
    max_chars = int(_observer_setting(
        cfg, "compression_max_chars", cfg.get("observer_compression_max_chars", 8000)
    ))
    max_attempts = max(1, int(_observer_setting(
        cfg, "compression_max_attempts",
        cfg.get("observer_compression_max_attempts", cfg.get("compression_retry_attempts", 3)),
    )))

    pg = None
    try:
        from .config import _resolve_data_dir
        from pgvector.psycopg2 import register_vector

        pg = _get_cfg_pg_conn_compat(cfg, pool)
        if pg is None:
            return "E1 压缩观察者印失败: PG 不可用"
        table_sql = _safe_notes_table(notes_table)
        row = _select_latest_observer_note(
            pg, notes_table=notes_table, target_note_id=target_note_id
        )
        if not row or not row[2]:
            return "E1 压缩观察者印: 无印可压缩"

        note_id, note_version, note_content = row[0], row[1], row[2]
        orig_len = len(note_content)
        if orig_len < 4000:
            return f"E1 压缩观察者印: 印 {note_id} 仅 {orig_len} 字, 无需压缩"

        if dry_run:
            return (
                f"E1 压缩观察者印 DRY RUN: id={note_id} version={note_version} "
                f"len={orig_len} → 目标 {int(orig_len * target_ratio)} 字 (max_chars={max_chars})"
            )

        # 调 LLM 压缩 — 重试受 max_attempts 控制; 任一 attempt 出错 / 空 / 超限都计失败,
        # 不写盘, 不 commit, 不返回成功字符串.
        # 2026-08-22: prompt 走 _resolve_prompt 配置化 (prompts.e1_observer_compress
        # 可覆盖, 支持内联文本/文件路径; 用户不配 = 内置默认), 对齐 8/3 prompt 全配置化规范。
        from .llm import LLMClient
        _compress_prompt = _cfg_prompt(cfg, "e1_observer_compress", _OBS_COMPRESS_PROMPT)
        client = LLMClient(cfg)
        # 2026-08-22 v2: 迭代压缩循环 (checkpoint 框架 + 对话形态输入 + 条数上限 prompt;
        # 超限时上轮输出转对话形态加压再压)。fail-closed 语义不变: 全败不写盘。
        compressed, last_err = _iterative_compress_note(
            client, _compress_prompt, note_content, max_chars, max_attempts,
        )
        if not compressed:
            return (
                f"E1 压缩观察者印失败: 重试 {max_attempts} 次仍未通过校验 "
                f"(orig_id={note_id} orig_len={orig_len} max_chars={max_chars}); "
                f"最近错误: {last_err[:160]}"
            )

        # 校验通过, 进入写盘阶段. 任一步骤异常 → fail-closed, 不 commit.
        _qa_range_orig = row[3]  # int8range 原样继承
        new_version = f"{note_version}-compressed" if note_version else "compressed"
        # 阶段1.5 (2026-08-20): e1._OBS_COMPRESS 写入时显式写 embed_model 列 (留 '' 占位,
        # 由随后 _backfill_note_embedding UPDATE 成合法 fingerprint). 这样即使 backfill 失败,
        # 行也已存在 (DEFAULT '' 接住); 旧 schema 缺列时 INSERT 仍走 fallback.
        # 闭锁 (2026-08-21): links 必须显式带 {"kind": "e1_compressed", "from_id": note_id}
        # + 附带 source_qa_range / prev_id 元数据供审计 / 失败段重试查询.
        links_payload = [{
            "kind": "e1_compressed",
            "from_id": note_id,
            "source_qa_range": (
                _qa_range_orig.lower if _qa_range_orig is not None else None,
                _qa_range_orig.upper if _qa_range_orig is not None else None,
            ),
            "prev_id": int(row[4]) if row[4] is not None else None,
            "accepted": True,
            "accepted_length": len(compressed),
            "max_chars": max_chars,
            "attempts_used": max_attempts,  # v2: 迭代循环内部计数, 这里记上限供审计
        }]
        try:
            with pg.cursor() as cur:
                if _qa_range_orig is not None:
                    try:
                        cur.execute(
                            f"INSERT INTO {table_sql} (version, content, source_qa_range, "
                            "prev_id, links, embed_model) "
                            "VALUES (%s, %s, %s::int8range, %s, %s::jsonb, %s) RETURNING id",
                            (new_version, compressed, _qa_range_orig, note_id,
                             json.dumps(links_payload, ensure_ascii=False), "")
                        )
                    except Exception as _e_ins:
                        if _is_undefined_column_error(_e_ins):
                            cur.execute(
                                f"INSERT INTO {table_sql} (version, content, source_qa_range, "
                                "prev_id, links) "
                                "VALUES (%s, %s, %s::int8range, %s, %s::jsonb) RETURNING id",
                                (new_version, compressed, _qa_range_orig, note_id,
                                 json.dumps(links_payload, ensure_ascii=False))
                            )
                        else:
                            raise
                else:
                    try:
                        cur.execute(
                            f"INSERT INTO {table_sql} (version, content, prev_id, links, "
                            "embed_model) "
                            "VALUES (%s, %s, %s, %s::jsonb, %s) RETURNING id",
                            (new_version, compressed, note_id,
                             json.dumps(links_payload, ensure_ascii=False), "")
                        )
                    except Exception as _e_ins:
                        if _is_undefined_column_error(_e_ins):
                            cur.execute(
                                f"INSERT INTO {table_sql} (version, content, prev_id, links) "
                                "VALUES (%s, %s, %s, %s::jsonb) RETURNING id",
                                (new_version, compressed, note_id,
                                 json.dumps(links_payload, ensure_ascii=False))
                            )
                        else:
                            raise
                new_id_row = cur.fetchone()
                if not new_id_row or new_id_row[0] is None:
                    raise RuntimeError("compressed note INSERT 未返回新 id")
                new_id = int(new_id_row[0])
            # 不在 INSERT 后提交。embedding 回填和 accepted 校验必须与 INSERT 共用一个事务；
            # 任一步失败都由外层 rollback，不能留下半成品 compressed row。
        except Exception as _e_write:
            # 写盘失败 → 不 commit, 不返回成功字符串
            try:
                pg.rollback()
            except Exception:
                pass
            return (
                f"E1 压缩观察者印失败: 写盘异常 {_safe_err(_e_write)[:160]} "
                f"(orig_id={note_id} max_chars={max_chars})"
            )

        # embedding 回填与 INSERT 共用同一事务。回填函数默认会 commit；这里显式
        # commit=False，只有确认 embedding 非空后才由本函数一次性提交。
        try:
            from .observer import _backfill_note_embedding
            from .embedding import safe_embed_cfg
            _ec = safe_embed_cfg(cfg)
            _backfill_note_embedding(
                pg, new_id, compressed,
                notes_table=table_sql,
                cfg=_ec,
                commit=False,
            )
            with pg.cursor() as cur:
                cur.execute(
                    f"SELECT embedding IS NOT NULL FROM {table_sql} WHERE id=%s",
                    (new_id,),
                )
                _embedding_row = cur.fetchone()
            if not _embedding_row or not bool(_embedding_row[0]):
                raise RuntimeError("compressed note embedding 回填后仍为空")
            pg.commit()
        except Exception as _e_emb:
            try:
                pg.rollback()
            except Exception:
                pass
            return (
                f"E1 压缩观察者印失败: embedding/事务校验异常 "
                f"{_safe_err(_e_emb)[:160]} (orig_id={note_id}, 未提交)"
            )
        return (
            f"E1 压缩观察者印: id={note_id} ({orig_len}字) → 新 id={new_id} "
            f"({len(compressed)}字, {len(compressed)/orig_len:.0%}, max_chars={max_chars}), "
            f"观察者下轮承接压缩版"
        )
    except ValueError:
        # embedding 配置/指纹契约错误必须保持 fail-closed，不能转成成功字符串。
        raise
    except Exception as e:
        return f"E1 压缩观察者印失败: {_safe_err(e)[:200]}"
    finally:
        if pg is not None:
            try:
                pg.close()
            except Exception:
                pass


def compress_observer_note_strict(
    config: dict | None = None,
    *,
    dry_run: bool = False,
    notes_table: str = "observation_notes",
    target_note_id: int | None = None,
    pool: Any = None,
) -> dict[str, Any]:
    """严格路径 — observer 写本轮前调用。

    与 compress_observer_note 的区别:
      - 失败不返字符串, 直接抛 E1CompressionFailure (含具体 reason).
      - 任何 fail-closed 情况 (空 / 超限 / LLM 异常 / 解析失败 / embedding 失败) 都不返
        假成功, 不 commit, 不写 accepted 压缩行.
      - 主写入成功 + embedding 回填成功 → 返 dict {accepted, new_id, source_id, prev_id,
        accepted_length, source_qa_range, max_chars}.

    observer.py 1.5 段必须 try / except E1CompressionFailure: _save_failed_segment +
    short-circuit return. 严禁 fallback 到 compress_observer_note (那会让 caller 拿不到
    明确失败信号, 继续走 synthesize_yin / 写本轮印 / 推进 cursor — 闭环断裂).

    实现要点: 委托给 compress_observer_note (compat path, 仍可让外部 caller 调),
    再把字符串结果解析成 dict 状态. 这样:
      - 严格路径共用 compress_observer_note 的 PG / LLM / 校验 / 写盘 / embedding
        路径, 不会出现两套逻辑漂移.
      - monkeypatch `compress_observer_note` 时, 严格路径会拿到 fake_compress 返的
        失败字符串并 raise E1CompressionFailure, 让 observer 1.5 段短路 (满足
        test_observer_e1_compression_closed_loop_red.py 的 monkeypatch 契约).
    """
    cfg = _legacy_config(config)
    max_chars = int(_observer_setting(
        cfg, "compression_max_chars", cfg.get("observer_compression_max_chars", 8000)
    ))
    max_attempts = max(1, int(_observer_setting(
        cfg, "compression_max_attempts",
        cfg.get("observer_compression_max_attempts", cfg.get("compression_retry_attempts", 3)),
    )))

    if dry_run:
        return {
            "accepted": False,
            "reason": "dry_run",
            "max_chars": max_chars,
            "max_attempts": max_attempts,
        }

    # 委托给 compress_observer_note (compat path) — 拿字符串结果
    # 内部走 PG → LLM → 校验 → 写盘 → embedding 全链路, 任一失败字符串含
    # "失败"/"超长"/"上限" 关键词.
    result_str = compress_observer_note(
        cfg,
        dry_run=False,
        notes_table=notes_table,
        target_note_id=target_note_id,
        pool=pool,
    ) or ""

    # 解析结果 — 失败关键词任一出现即视为失败
    _failure_markers = ("失败", "超长", "上限", "too long", "exceed")
    if any(m in result_str for m in _failure_markers):
        raise E1CompressionFailure(
            f"E1 压缩观察者印严格路径失败: {result_str[:300]}"
        )

    # 无印可压缩 / 无需压缩 — 这些都是 accepted=False, 不抛
    if "无印可压缩" in result_str:
        return {
            "accepted": False,
            "reason": "no_source",
            "max_chars": max_chars,
        }
    if "无需压缩" in result_str:
        # 兼容路径原文: "印 {id} 仅 {n} 字, 无需压缩"
        return {
            "accepted": False,
            "reason": "not_needed",
            "max_chars": max_chars,
        }

    # 成功路径: 原文 "E1 压缩观察者印: id=353 (10000字) → 新 id=354 (4500字, 45%, max_chars=8000), 观察者下轮承接压缩版"
    # 解析 source_id / new_id / lengths
    import re as _re
    _m = _re.search(
        r"id=(?P<sid>\d+)\s*\((?P<slen>\d+)字\)\s*→\s*新\s*id=(?P<nid>\d+)\s*\((?P<alen>\d+)字",
        result_str,
    )
    if not _m:
        # 成功但解析失败 — 视为非确定, raise 让 caller 走 failed_segment
        raise E1CompressionFailure(
            f"E1 压缩观察者印严格路径无法解析成功字符串: {result_str[:200]}"
        )
    sid = int(_m.group("sid"))
    nid = int(_m.group("nid"))
    slen = int(_m.group("slen"))
    alen = int(_m.group("alen"))

    return {
        "accepted": True,
        "source_id": sid,
        "new_id": nid,
        "source_length": slen,
        "accepted_length": alen,
        "max_chars": max_chars,
        "max_attempts": max_attempts,
    }