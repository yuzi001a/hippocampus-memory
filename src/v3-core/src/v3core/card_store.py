"""卡库读写 — 碑卡 (b) 存储 (SQLite 后端)

替代原有的 filesystem-per-card 架构, 每张卡写 sqlite v3_cards.db 一行.
所有公开接口保持与旧版一致 (DeepStore 9 方法 + 模块级嵌入缓存函数).
"""

from __future__ import annotations
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import DeepCard, CardResult


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

logger = logging.getLogger("v3core.card_store")

# B2.4: local dedup cache (PG fallback)
_LOCAL_CARD_EMB: dict[str, list[float]] = {}
_DEDUP_THRESHOLD = 0.85


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    try:
        dot = sum(float(x) * float(y) for x, y in zip(a, b))
        na = sum(float(x) * float(x) for x in a) ** 0.5
        nb = sum(float(y) * float(y) for y in b) ** 0.5
    except (TypeError, ValueError):
        return 0.0
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _legacy_filename(category: str, title: str) -> str:
    """P2a (2026-09-09) helper — 旧版 ``write_card`` 的兼容入口保留.

    注意: 本函数**仍**使用 ``datetime.now()`` 派生时间戳后缀, 与 SOL 评审
    锁定的"内容感知稳定身份"语义**不一致** — 仅作为外部调用方在没有
    更明确的 caller intent 时的历史兼容兜底. ``V3Core.store_card`` 在
    caller 没传 source_id 时已**不再**调用本函数, 而是走
    :func:`derive_default_source_id` (sha256 of canonical JSON, 完全不依
    赖 datetime / wall-clock / embedding / 内部连接对象).
    """
    now = datetime.now()
    safe = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]+", "-", title)
    return f"b_{category}_{now.strftime('%Y-%m-%d')}_{str(int(now.timestamp()*1000))}-{safe}.md"


# P2a SOL-review A (2026-09-09) — caller 显式提供的内部 provenance 字段白
#  名单. 只挑 caller 已显式传过的字段 (Non-None / non-empty) — 不引入
#  datetime / wall-clock / embedding / 内部连接对象, 保证幂等.
_DEFAULT_IDENTITY_PROVENANCE_KEYS = (
    "source",
    "source_j_ids",
    "when",
    "where",
    "who",
    "why",
    "confidence",
    "observation_count",
)


def derive_default_source_id(
    category: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    **provenance,
) -> str:
    """P2a SOL-review A (2026-09-09): 确定性内容感知默认身份 helper.

    契约 (锁定, 不要改):
    * 稳定 — 同 ``(category, title, content, tags)`` + caller 已显式提供的
      内部 provenance → 同 source_id (sha256 of canonical JSON), 不读
      datetime / wall-clock / embedding / 连接对象.
    * 可区分 — 上述任一字段不同 → 不同 source_id. 防止 over-correction.
    * 格式 ``b_{category}_auto_{sha256}-{safe_title}.md``, 保留旧版
      ``_legacy_filename`` 的 ``b_<category>_<...>.md`` 前缀.
    """
    canonical: dict = {
        "category": category or "",
        "title": title or "",
        "content": content or "",
        "tags": list(tags or []),
    }
    for _key in _DEFAULT_IDENTITY_PROVENANCE_KEYS:
        if _key not in provenance:
            continue
        _v = provenance[_key]
        if _v is None:
            continue
        if isinstance(_v, str) and not _v.strip():
            continue
        if isinstance(_v, list) and len(_v) == 0:
            continue
        canonical[_key] = _v
    blob = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    safe_title = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]+", "-", title or "")[:24]
    safe_cat = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5-]+", "-", category or "card")
    return f"b_{safe_cat}_auto_{digest}-{safe_title}.md"


class DeepStore:
    """碑卡 SQLite 存储 (接口与旧版兼容)"""

    @staticmethod
    def _extract_base_path(config) -> str:
        """兼容 V3Config / dict / None."""
        if config is None:
            return ""
        if isinstance(config, dict):
            return config.get("basePath", "") or ""
        return getattr(config, "base_path", "") or ""

    def __init__(self, config=None):
        self._config = config
        base = self._extract_base_path(config)
        if not base:
            base = str(Path.home() / ".v3-core" / "profiles" / "default")
        self.base = Path(base)
        self._index_cache: dict | None = None
        self._index_mtime: float = 0
        # 延迟初始化 SqliteCardStore (保持 V3Core 懒加载语义)
        self._sqlite = None

    @property
    def sqlite(self):
        if self._sqlite is None:
            from .sqlite_store import SqliteCardStore
            self._sqlite = SqliteCardStore(self.base)
            self._sqlite.load_emb_cache()
            # 同步嵌入到内存在线去重缓存
            global _LOCAL_CARD_EMB
            _LOCAL_CARD_EMB.clear()
            _LOCAL_CARD_EMB.update(self._sqlite.dump_emb_to_dict())
        return self._sqlite

    def _cards_dir(self) -> Path:
        """保留兼容 — 返回旧路径, 但不再使用"""
        return self.base / "cards"

    # ── 状态 ────────────────────────────────────────────────

    def status(self, category: str | None = None) -> dict:
        """卡库统计 — SQLite GROUP BY"""
        try:
            return self.sqlite.status(category)
        except Exception as e:
            logger.warning("status 查询失败: %s", _safe_err(e)[:100])
            return {"total_cards": 0, "by_category": {}}

    # ── 写卡 ────────────────────────────────────────────────

    def write_card(self, category: str, filename: str, title: str, content: str,
                   tags: list[str] | None = None,
                   pg=None, embed_cfg: dict | None = None, **kwargs) -> str:
        """写一张碑卡 — SQLite INSERT OR REPLACE + 可选 PG upsert + 去重

        参数兼容旧版:
          filename: 文件名/卡片 ID (不带扩展名)
          pg:       PgEmbedStore 实例 (可选)
          embed_cfg: 嵌入配置 (可选)
          kwargs:   confidence, observation_count, last_verified_at,
                    source, source_j_ids, when, where, who, why
        """
        tags = tags or []
        source_id = filename  # filename = 卡片标识符

        # 旧版写文件逻辑不再需要 — 全部走 SQLite

        #         # 嵌入 + 去重 (逻辑保留, 数据源改为 SQLite)
        # 阶段1 (2026-08-20): embed_cfg 由调用方经 build_embed_cfg / safe_embed_cfg 构造;
        # call_embedding 现在 fail-closed, 缺 model/endpoint → raise ValueError
        emb = None
        rel_key = f"{category}/{source_id}"
        # A card's embedding is DERIVED state; the card itself is the asset. A failed
        # request must not be swallowed into an unexplained NULL — it gets a durable
        # marker so the card stays repairable. This path has no daemon-thread
        # guarantee, so it names the durable-write policy explicitly instead of
        # inheriting the 3s/0 realtime default.
        if embed_cfg is not None:
            from .embedding import DURABLE_WRITE_EMBED_POLICY
            from .embed_failures import embed_for_write
            _out = embed_for_write(
                content.replace("\n", " ")[:2000], embed_cfg,
                entity_table="topics", entity_id=source_id, phase="card_write",
                conn_factory=getattr(pg, "open_side_connection", None),
                policy=DURABLE_WRITE_EMBED_POLICY,
            )
            emb = _out.vector
            if not _out.ok:
                logger.warning(
                    "card embedding %s: source_id=%s class=%s retryable=%s "
                    "marker_recorded=%s — 卡本身已写入, embedding 待修复",
                    _out.status.value, source_id, _out.error_class,
                    _out.retryable, _out.marker_recorded,
                )

        # 去重: PG 通走 PG.search, 不通走本地 _LOCAL_CARD_EMB cosine
        skipped = False
        if emb and pg is not None and getattr(pg, "is_connected", lambda: False)():
            try:
                existing = pg.search(emb, kind="card", limit=3)
                for _hit in existing:
                    if _hit.get("cosine", 0) >= _DEDUP_THRESHOLD:
                        logger.info("去重跳过 [PG]: %s (cosine=%.3f, matched=%s)",
                                    title, _hit["cosine"], _hit.get("source_id", "?"))
                        skipped = True
                        break
            except Exception as _dedup_e:
                logger.warning("PG 去重失败, 回落本地: %s", str(_dedup_e)[:200])

        if not skipped and emb and _LOCAL_CARD_EMB:
            best_sim = 0.0
            best_key = None
            for _k, _v in _LOCAL_CARD_EMB.items():
                if _k == rel_key:
                    continue
                _sim = _cosine_similarity(emb, _v)
                if _sim > best_sim:
                    best_sim = _sim
                    best_key = _k
            if best_sim >= _DEDUP_THRESHOLD:
                logger.info("去重跳过 [local]: %s (cosine=%.3f, matched=%s)",
                            title, best_sim, best_key)
                skipped = True

        if skipped:
            if emb:
                _LOCAL_CARD_EMB[rel_key] = emb
            return f"{source_id} (dedup-skipped)"

        # 写入 SQLite
        try:
            self.sqlite.write_card(
                source_id=source_id,
                category=category,
                title=title,
                content=content,
                tags=tags,
                embedding=emb,
                confidence=float(kwargs.get("confidence", 0.3)),
                observation_count=int(kwargs.get("observation_count", 1)),
                last_verified_at=kwargs.get("last_verified_at"),
                source=kwargs.get("source", "extraction"),
                source_j_ids=kwargs.get("source_j_ids"),
                when_=kwargs.get("when", ""),
                where_=kwargs.get("where", ""),
                who=kwargs.get("who", ""),
                why=kwargs.get("why", ""),
            )
        except Exception as _sqlite_e:
            logger.warning("SQLite write_card 失败: %s", str(_sqlite_e)[:200])

        # 更新去重缓存
        if emb:
            _LOCAL_CARD_EMB[rel_key] = emb

        # PG upsert (非致命, 失败仅 warning) — 保留旧逻辑
        if pg is not None:
            try:
                if emb:
                    pg.insert_card(source_id, title, content[:3000], category, tags, emb,
                                   embed_cfg=embed_cfg)
                else:
                    pg.insert_card(source_id, title, content[:3000], category, tags)
            except ValueError:
                raise
            except Exception as _pg_e:
                logger.warning("PG upsert 失败 (非致命): %s", str(_pg_e)[:200])

        return source_id

    # ── P2a: 严格/runtime-backed 写入路径 ───────────────────

    def write_card_strict(self, category: str, title: str, content: str,
                          tags: list[str] | None = None,
                          source_id: str = "",
                          pg: Any = None,
                          embed_cfg: dict | None = None,
                          **kwargs) -> CardResult:
        """P2a (2026-09-09) 严格写入路径 — 真值优先 PG.

        契约 (与既有 ``write_card`` 并存, 互不破坏):

        * **PG 必连**: ``pg`` 必须存在且 ``is_connected()`` 真, 否则返回
          ``DURABLE_FAILED`` (success=False, durable=False), 不写 SQLite.
        * **PG 先写**: 用 caller 提供的 ``source_id`` 调 ``pg.insert_card``
          (其内部已用 ON CONFLICT (topic_id) DO UPDATE 兜住 retry —
          同一 source_id 重写不会产生第二条 canonical 行).
        * **回读验证**: 调 ``pg.get_card_by_source_id(source_id)`` 比对
          canonical content, 一致才认 ``durable=True``. 不一致 → 报
          ``DURABLE_FAILED``.
        * **PG 失败 → 真值失败**: 任何 PG 抛错 (insert / readback) → 返
          ``DURABLE_FAILED``, **不**写 SQLite, **不**声称 success.
        * **PG 真后才做派生**: SQLite / 嵌入缓存等本地副作用仅在 PG 真
          写入后才执行; 这些派生副作用的异常 → ``warnings`` + status
          ``DERIVED_WARNING``, **不**覆盖 durable 真值.
        * **保留既有 kwargs**: ``source/source_j_ids`` 等透传给 SQLite
          (老 write_card 契约保留). 不伪造 QA id. Lab topics schema
          缺 ``source_j_ids`` / ``metadata`` 列 — caller 提供的
          ``source_j_ids`` 由 SQLite 派生侧原值保留, PG canonical
          不强行塞进 ``note_ref``/``keywords``/``body`` (避免伪造
          provenance); 若 caller 实际传了 ``source_j_ids``, 在
          ``warnings`` 里登记一条 schema gap 提醒, 不抹掉 durable 真值.
        * **source_id**: caller 必须显式提供. ``V3Core.store_card`` 在
          caller 未提供时会按旧版 ``write_card`` 的 filename 语义生成
          一个唯一 source_id (``b_{category}_{date}_{ms}-{safe}.md``),
          保持与历史 caller 的写入命名规则一致. 本方法不再允许
          silent legacy fallback — 任何 ``source_id == ""`` 都被视作
          契约违反, 直接 ``DURABLE_FAILED``.
        * **retry dedup**: 若 PG 上已存在与本次 ``source_id`` 内容一致
          的 canonical 行, 写入走 idempotent upsert 路径, status 升级为
          ``DEDUPLICATED`` (durable=True), 区分"全新插入"与"retry 命中".
        """
        tags = tags or []
        warnings: list[str] = []

        # ---- 1. PG 必连 ----
        # 调用 ``is_connected()`` 是 runtime PG 探针 — 真实环境下可能抛
        # psycopg2.OperationalError / socket timeout / pool 失效等异常.
        # 任何 probe 异常都意味着 PG 不可达, 必须按 ``DURABLE_FAILED``
        # 兜住, 不写 SQLite, 不让 PG 探针崩溃绕过 strict 契约.
        if pg is None or not callable(getattr(pg, "is_connected", None)):
            return CardResult(
                success=False,
                durable=False,
                status="DURABLE_FAILED",
                durable_store="postgresql",
                source_id=source_id or "",
                error="postgres unavailable or not connected",
                warnings=warnings,
            )
        try:
            _pg_connected = bool(pg.is_connected())
        except Exception as _probe_e:
            # probe 异常 (连接失效 / 网络断 / driver bug) — 按
            # DURABLE_FAILED 出 receipt, 不掩盖为"PG 不通但仍可写".
            return CardResult(
                success=False,
                durable=False,
                status="DURABLE_FAILED",
                durable_store="postgresql",
                source_id=source_id or "",
                error=f"postgres probe failed: {_safe_err(_probe_e)}",
                warnings=warnings,
            )
        if not _pg_connected:
            return CardResult(
                success=False,
                durable=False,
                status="DURABLE_FAILED",
                durable_store="postgresql",
                source_id=source_id or "",
                error="postgres unavailable or not connected",
                warnings=warnings,
            )

        # ---- 2. source_id 契约: caller 必须显式提供 ----
        # 严格路径不允许 silent legacy fallback. 旧版 ``write_card`` 的
        # filename 生成逻辑已搬到 ``V3Core.store_card`` 入口处 (在传入
        # 本方法前, 用 ``derive_default_source_id`` 派生), 这里是最后一道闸:
        # 若仍为空, 立刻 ``DURABLE_FAILED``, 不写 SQLite, 不伪报成功.
        if not source_id or not str(source_id).strip():
            return CardResult(
                success=False,
                durable=False,
                status="DURABLE_FAILED",
                durable_store="postgresql",
                source_id="",
                error="strict write requires explicit source_id (caller must derive via deterministic request identity)",
                warnings=warnings,
            )

        # ---- 3. PG 先写 (insert/upsert) ----
        # 旧 retry 语义保留: 若 source_id 在 PG 上有 canonical 行且内容
        # 与本次写入一致, 这次是 retry (不是新插入). PG upsert ON
        # CONFLICT 会走 idempotent update 路径, 但 status 仍是
        # DEDUPLICATED (durable=True), 让上游审计能识别 retry 命中.
        # pre-read 失败 → 静默回退到"全新插入", 不当作 dedup, 不拖垮
        # strict 路径 (后续 post-readback 仍会强校验).
        is_dedup_retry = False
        try:
            get_fn_pre = getattr(pg, "get_card_by_source_id", None)
            if callable(get_fn_pre):
                _pre = get_fn_pre(source_id)
                if (
                    isinstance(_pre, dict)
                    and (_pre.get("content", "") or "") == content[:3000]
                ):
                    is_dedup_retry = True
        except Exception:
            is_dedup_retry = False

        # P2a SOL-review B (2026-09-09): 一次写卡算一次 ``emb``, 后续 PG /
        # SQLite / _LOCAL_CARD_EMB 三个 sink 共享同一变量, 不再各自从 kwargs
        # 拿 embedding (caller 不传时永远 None, 导致 SQLite / cache 与 PG 不
        # 一致). 配置契约错误 ``ValueError`` 仍然 fail-closed (raise, 由调用
        # 方按 DURABLE_FAILED 收尾); 非 ``ValueError`` 运行时失败 (网络 /
        # HTTP 5xx) 走派生侧降级 — PG 走无 emb 通道继续写, 但 ``warnings``
        # 必含可检索的 ``embedding degradation`` 文案, status 升级为
        # ``DERIVED_WARNING`` (durable=True / success=True).
        emb: list[float] | None = None
        try:
            # 与既有 legacy 写入路径一致: 有 embedding 走 embedding 通道,
            # 否则走无 embedding 通道. embed_cfg 由 V3Core 解析好传入.
            if embed_cfg is not None:
                # Same contract as write_card: a failed request must not become an
                # unexplained NULL. Two things change here — the durable-write policy
                # replaces the inherited 3s/0 realtime default, and the failure is
                # recorded DURABLY. An in-memory `warnings` entry is good UX but it is
                # not accounting: it dies with the process, so a repair pass could
                # never find the row.
                from .embedding import DURABLE_WRITE_EMBED_POLICY
                from .embed_failures import embed_for_write
                _out = embed_for_write(
                    content.replace("\n", " ")[:2000], embed_cfg,
                    entity_table="topics", entity_id=source_id, phase="card_write",
                    conn_factory=getattr(pg, "open_side_connection", None),
                    policy=DURABLE_WRITE_EMBED_POLICY,
                )
                emb = _out.vector
                if not _out.ok:
                    logger.warning(
                        "write_card_strict embedding %s (走无 emb 路径, 派生侧降级): "
                        "class=%s retryable=%s marker_recorded=%s",
                        _out.status.value, _out.error_class, _out.retryable,
                        _out.marker_recorded,
                    )
                    warnings.append(
                        f"embedding degradation: {_out.error_class} after "
                        f"{_out.attempts} attempt(s) — PG canonical row "
                        f"committed without embedding vector; SQLite / cache "
                        f"derived sinks also skipped embedding for consistency; "
                        f"durable failure marker recorded={_out.marker_recorded}"
                    )
                    emb = None
            if emb is not None:
                pg.insert_card(
                    source_id, title, content[:3000], category, tags, emb,
                    embed_cfg=embed_cfg,
                )
            else:
                pg.insert_card(
                    source_id, title, content[:3000], category, tags,
                )
        except Exception as _pg_ins_e:
            # PG insert/upsert 真失败 → 立刻返回 DURABLE_FAILED, 不写 SQLite
            return CardResult(
                success=False,
                durable=False,
                status="DURABLE_FAILED",
                durable_store="postgresql",
                source_id=source_id,
                error=f"postgres canonical write failed: {_safe_err(_pg_ins_e)}",
                warnings=warnings,
            )

        # ---- 4. PG 回读验证 ----
        canonical = None
        try:
            get_fn = getattr(pg, "get_card_by_source_id", None)
            if callable(get_fn):
                canonical = get_fn(source_id)
        except Exception as _pg_rb_e:
            return CardResult(
                success=False,
                durable=False,
                status="DURABLE_FAILED",
                durable_store="postgresql",
                source_id=source_id,
                error=f"postgres readback failed: {_safe_err(_pg_rb_e)}",
                warnings=warnings,
            )

        if not canonical or not isinstance(canonical, dict):
            return CardResult(
                success=False,
                durable=False,
                status="DURABLE_FAILED",
                durable_store="postgresql",
                source_id=source_id,
                error="postgres readback returned no canonical record",
                warnings=warnings,
            )

        # content 比对: 截断行为对齐 (PG 存 body / 我们写 content[:3000])
        expected_content = content[:3000]
        actual_content = canonical.get("content", "") or ""
        if actual_content != expected_content:
            return CardResult(
                success=False,
                durable=False,
                status="DURABLE_FAILED",
                durable_store="postgresql",
                source_id=source_id,
                error="postgres canonical content mismatch on readback",
                warnings=warnings,
            )

        # ---- 4.5. PG canonical provenance gap (lab schema no source_j_ids col) ----
        # 实测 topics 表缺 source_j_ids / metadata 列. caller 若传了
        # source_j_ids, SQLite 派生侧会原值保留; PG canonical 这条只把
        # category 落到 note_ref, keywords 接 tags, 不把 source_j_ids
        # 伪装进 body/note_ref/keywords. 在 warnings 里登记一条 gap 提示,
        # 不抹掉 durable 真值.
        caller_source_j_ids = kwargs.get("source_j_ids")
        if caller_source_j_ids:
            warnings.append(
                "pg canonical provenance gap: topics schema lacks "
                "source_j_ids/metadata columns; caller source_j_ids "
                "preserved on sqlite derived side only"
            )

        # ---- 5. PG 真后才做派生 (SQLite + 嵌入缓存) ----
        # P2a SOL-review B (2026-09-09): 把第 3 步算出的 ``emb`` 变量同时
        # 带过 SQLite derived write 和 ``_LOCAL_CARD_EMB`` 本地去重缓存,
        # 保持三个 sink 完全一致. 旧版 ``kwargs.get('embedding')`` 在 caller
        # 没传时永远 None, 导致 SQLite / cache 与 PG 不一致, 召回池去重
        # 键缺失. 同时, 若第 3 步登记了 ``embedding degradation`` warning,
        # 这里也升级 status 为 ``DERIVED_WARNING`` (PG 真值不抹, 仅派生
        # 侧降级提示).
        if warnings and any("embedding degradation" in w for w in warnings):
            derived_failed = True
        else:
            derived_failed = False
        try:
            self.sqlite.write_card(
                source_id=source_id,
                category=category,
                title=title,
                content=content,
                tags=tags,
                embedding=emb,
                confidence=float(kwargs.get("confidence", 0.3)),
                observation_count=int(kwargs.get("observation_count", 1)),
                last_verified_at=kwargs.get("last_verified_at"),
                source=kwargs.get("source", "extraction"),
                source_j_ids=kwargs.get("source_j_ids"),
                when_=kwargs.get("when", ""),
                where_=kwargs.get("where", ""),
                who=kwargs.get("who", ""),
                why=kwargs.get("why", ""),
            )
        except Exception as _sqlite_e:
            derived_failed = True
            warnings.append(f"sqlite derived write failed: {_safe_err(_sqlite_e)[:200]}")

        # 嵌入缓存更新 (派生) — 用第 3 步算出的 ``emb`` (函数级), 与
        # PG / SQLite 完全一致. `emb=None` 时不写缓存键 (避免与 PG
        # canonical embedding 列冲突).
        try:
            if emb:
                _LOCAL_CARD_EMB[f"{category}/{source_id}"] = emb
        except Exception as _emb_cache_e:
            derived_failed = True
            warnings.append(
                f"local embedding cache update failed: {_safe_err(_emb_cache_e)[:200]}"
            )

        # ---- 6. 返回 ----
        # P2a follow-up (2026-09-09) status precedence — locked-down rule:
        #   1. retry 命中 (PG 上已存在 source_id 且内容一致) → DEDUPLICATED
        #      (durable=True). 派生侧 warning 仍然保留在 ``warnings`` 里,
        #      绝不覆盖 retry 的语义信号; PG canonical 真值是这条 receipt
        #      的最强信号, 派生失败不能把它降级.
        #   2. 全新写入 + 派生侧 (SQLite / 嵌入缓存) 失败 →
        #      DERIVED_WARNING (durable=True / success=True, ``warnings``
        #      含派生侧详情).
        #   3. 全新写入干净 → DURABLE_COMMITTED.
        # 顺序检查: 先看 retry 信号, 再看 derived 失败, 最后才是干净全新.
        if is_dedup_retry:
            final_status = "DEDUPLICATED"
        elif derived_failed:
            final_status = "DERIVED_WARNING"
        else:
            final_status = "DURABLE_COMMITTED"
        # P2a (2026-09-09) strict CardResult.card: 保留 caller 提供的
        # 来源溯源 (source / source_j_ids / when / where / who / why /
        # confidence / observation_count). caller 没传 → 不在 card
        # 里出现, 绝不伪造 QA id 或杜撰 caller 没声明的 provenance.
        # 这些字段与 SQLite 派生侧 kwargs 同源 — 此处只回显 caller 实际
        # 传入的值, 不从 SQLite row 取 (写路径刚写完, 回读本身可能
        # 还没落盘或派生侧失败).
        card_payload: dict = {
            "category": category,
            "title": title,
            "source_id": source_id,
        }
        # 仅当 caller 显式提供了对应字段 (非 None / 非空) 时才放进
        # card_payload. 空字符串 / None 视为 "caller 没传", 不杜撰.
        for _key, _kwargs_key in (
            ("source", "source"),
            ("source_j_ids", "source_j_ids"),
            ("when", "when"),
            ("where", "where"),
            ("who", "who"),
            ("why", "why"),
            ("confidence", "confidence"),
            ("observation_count", "observation_count"),
        ):
            if _kwargs_key in kwargs:
                _v = kwargs[_kwargs_key]
                if _v is None:
                    continue
                # str 类型: 空串视为未传
                if isinstance(_v, str) and not _v.strip():
                    continue
                # list 类型: 空列表视为未传 (避免误把 None 当作 caller 显式声明的空值)
                if isinstance(_v, list) and len(_v) == 0:
                    continue
                card_payload[_key] = _v
        return CardResult(
            success=True,
            path=f"cards/{category}/{source_id}",
            card=card_payload,
            source_id=source_id,
            durable=True,
            durable_store="postgresql",
            status=final_status,
            warnings=warnings,
        )
    # ── 读卡 ────────────────────────────────────────────────

    def read_card(self, rel_path: str) -> DeepCard | None:
        """读一张卡 — 按 rel_path (category/filename 或 filename)"""
        try:
            # 解析 source_id
            rel = rel_path.replace("\\", "/")
            parts = rel.split("/")
            source_id = parts[-1].replace(".md", "")
            d = self.sqlite.read_card(source_id)
            if d is None:
                return None
            return self._dict_to_deepcard(d, rel)
        except Exception:
            return None

    def _parse_card(self, path: str, text: str) -> DeepCard:
        """保留兼容 — 新代码不走此路径, 仅供外部直接调用时兜底"""
        return DeepCard(
            filename=os.path.basename(path),
            category="", date="", title="", content=text,
            source="extraction", path=path,
        )

    def _dict_to_deepcard(self, d: dict, rel: str) -> DeepCard:
        """SQLite Row → DeepCard"""
        tags = d.get("tags", [])
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = []
        source_j_ids = d.get("source_j_ids", [])
        if isinstance(source_j_ids, str):
            try:
                source_j_ids = json.loads(source_j_ids)
            except (json.JSONDecodeError, TypeError):
                source_j_ids = []

        return DeepCard(
            filename=d.get("source_id", ""),
            category=d.get("category", ""),
            date=(d.get("created_at") or "")[:10],
            title=d.get("title", ""),
            content=d.get("content", ""),
            tags=tags,
            source=d.get("source", "extraction"),
            path=f"cards/{d.get('category', '')}/{d.get('source_id', '')}",
            when=d.get("when_", ""),
            where=d.get("where_", ""),
            who=d.get("who", ""),
            why=d.get("why", ""),
            source_j_ids=source_j_ids,
            confidence=float(d.get("confidence", 0.3)),
            observation_count=int(d.get("observation_count", 1)),
        )

    # ── 索引 ────────────────────────────────────────────────

    def rebuild_index(self) -> dict:
        """重建索引 — 从 SQLite 读取, 零文件扫描"""
        try:
            return self.sqlite.rebuild_index()
        except Exception as e:
            logger.warning("rebuild_index 失败: %s", _safe_err(e)[:100])
            return {"files": {}}

    def get_index(self) -> dict:
        """带缓存的索引 — 内存优先, SQLite 兜底"""
        if self._index_cache is not None:
            return self._index_cache
        # SQLite 查询
        try:
            self._index_cache = self.sqlite.get_index()
            return self._index_cache
        except Exception as e:
            logger.warning("get_index 失败: %s", _safe_err(e)[:100])
            return {"files": {}}

    def _invalidate_index(self) -> None:
        self._index_cache = None
        self._index_mtime = 0

    def _save_index_cache(self) -> None:
        """索引在 SQLite 里实时, 不需要单独持久化"""
        pass

    # ── 删除 ────────────────────────────────────────────────

    def delete_card(self, rel_path: str) -> bool:
        """删除一张碑卡 — SQLite DELETE + 索引失效"""
        if not rel_path:
            raise ValueError("rel_path 不能为空")
        parts = rel_path.replace("\\", "/").split("/")
        source_id = parts[-1].replace(".md", "")
        try:
            self.sqlite.delete_card(source_id)
            self._invalidate_index()
            return True
        except Exception as e:
            logger.warning("delete_card 失败: %s", _safe_err(e)[:100])
            return False


# ── 模块级嵌入缓存函数 (兼容旧 import) ─────────────────

def _load_emb_cache(base) -> None:
    """从 SQLite 加载去重缓存到内存 (兼容旧调用)"""
    try:
        from .sqlite_store import SqliteCardStore
        store = SqliteCardStore(Path(base))
        store.load_emb_cache()
    except Exception as _e:
        logger.warning("load_emb_cache 失败: %s", str(_e)[:100])


def _save_emb_cache(base) -> None:
    """嵌入已在 write_card 时实时写入 SQLite, 此方法不需操作"""
    pass