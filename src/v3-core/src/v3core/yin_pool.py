"""印段落池 — 2026-08-06 设计落地 Step 3c: E1 印进召回池。

E1 写印后切段入库 (PG yin_paragraphs): 每段 embedding — 向量+关键词可搜。
召回: search_yin() — prefetch/recall_pool 接入（用户搜"以前聊过什么"可命中印段落）。

表结构:
    yin_paragraphs (
        id SERIAL PRIMARY KEY,
        yin_version TEXT,        -- 印版本 (y_20260806T....md)
        section TEXT,            -- 段落标题 (## 节名)
        content TEXT,            -- 段落正文
        embedding vector(1024),
        created_at TIMESTAMP DEFAULT NOW()
    )
"""
import logging
import re
from ._deadline import PrefetchDeadlineExceeded

logger = logging.getLogger("v3core.yin_pool")

_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS yin_paragraphs (
    id SERIAL PRIMARY KEY,
    yin_version TEXT,
    section TEXT,
    content TEXT,
    embedding vector(1024),
    created_at TIMESTAMP DEFAULT NOW()
)
"""


def _get_conn(pg):
    """兼容 PgEmbedStore（有 _connect）或裸 psycopg2 连接。"""
    if pg is None:
        return None
    return pg._connect() if hasattr(pg, "_connect") else pg


def ensure_table(pg) -> bool:
    """确保 yin_paragraphs 表存在。"""
    conn = _get_conn(pg)
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(_SCHEMA_SQL)
        conn.commit()
        return True
    except Exception as e:
        logger.warning("yin_pool: 建表失败: %s", str(e)[:150])
        return False


def ingest_yin(yin_content: str, yin_version: str, pg, embed_cfg: dict | None = None) -> int:
    """E1 写印后调用: 切段 + embedding + 入库。返回写入段数。

    embed_cfg: ``build_embed_cfg(cfg)`` 工厂结果. 传了 embed_cfg 必须有合法 fingerprint
    (写到 ``embed_model`` 列). 未传 embed_cfg → 走历史行为 (无 fingerprint, 不发请求,
    embed_model 列由 DEFAULT '' 接住). 传了 embed_cfg 但缺 _fingerprint → fail-closed,
    立即 ValueError, 禁止静默伪造.
    """
    if not yin_content or not yin_content.strip():
        return 0
    conn = _get_conn(pg)
    if not conn:
        return 0
    ensure_table(pg)

    # 解析 fingerprint: 有 embed_cfg 即校验合法性.
    fp = ""
    if embed_cfg is not None:
        if not isinstance(embed_cfg, dict):
            raise ValueError(
                "embed_cfg 必须是 dict (来自 build_embed_cfg(cfg) 工厂), "
                f"实际类型={type(embed_cfg).__name__}"
            )
        fp = embed_cfg.get("_fingerprint") or ""
        if not fp:
            raise ValueError(
                "embed_cfg 缺 _fingerprint — 必须经 build_embed_cfg(cfg) 工厂构造, "
                "禁止手拼 dict"
            )

    # 切段: 按 ## 节切（保留节标题）
    sections = []
    matches = list(_SECTION_RE.finditer(yin_content))
    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(yin_content)
        section_title = m.group(1).strip()[:100]
        section_body = yin_content[start:end].strip()
        if len(section_body) < 30:  # 空节/太短跳过
            continue
        sections.append((section_title, section_body))

    if not sections:
        return 0

    # 先清掉同版本旧段（幂等: 同版本重写）
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM yin_paragraphs WHERE yin_version=%s", (yin_version,))
    except Exception as e:
        logger.warning("yin_pool: 清理旧段失败: %s", str(e)[:120])

    # embedding + 写入（每段一次 embedding 调用——失败跳过该段）
    written = 0
    for title, body in sections:
        emb_text = None
        if embed_cfg is not None:
            try:
                from .embedding import call_embedding
                ev = call_embedding(f"{title}. {body[:1500]}", embed_cfg)
                if ev:
                    emb_text = "[" + ",".join(str(x) for x in ev) + "]"
            except ValueError:
                # 配置错误不允许吞 — 必须冒泡
                raise
            except Exception as e:
                logger.debug("yin_pool: 段 embedding 失败: %s", str(e)[:100])
        try:
            with conn.cursor() as cur:
                if emb_text:
                    # 旧 schema 兼容: yin_paragraphs 缺 embed_model 列 → fallback
                    try:
                        cur.execute(
                            "INSERT INTO yin_paragraphs (yin_version, section, content, "
                            "embedding, embed_model) "
                            "VALUES (%s, %s, %s, %s::vector, %s)",
                            (yin_version, title, body[:6000], emb_text, fp),
                        )
                    except Exception as _e_ins:
                        from .pg_store import _is_undefined_column_error
                        if _is_undefined_column_error(_e_ins):
                            # 2026-08-22: 先 rollback 再兜底 — 防 InFailedSqlTransaction 死 fallback.
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            logger.warning(
                                "yin_pool: yin_paragraphs 缺 embed_model 列, 走旧 SQL "
                                "(DEFAULT '' 由迁移后 schema 接住): %s",
                                str(_e_ins)[:120]
                            )
                            cur.execute(
                                "INSERT INTO yin_paragraphs (yin_version, section, content, "
                                "embedding) "
                                "VALUES (%s, %s, %s, %s::vector)",
                                (yin_version, title, body[:6000], emb_text),
                            )
                        else:
                            raise
                else:
                    cur.execute(
                        "INSERT INTO yin_paragraphs (yin_version, section, content) "
                        "VALUES (%s, %s, %s)",
                        (yin_version, title, body[:6000]),
                    )
            written += 1
        except ValueError:
            raise
        except Exception as e:
            logger.debug("yin_pool: 段写入失败: %s", str(e)[:100])
    try:
        conn.commit()
    except Exception:
        pass
    return written


def search_yin(query_emb: list[float] | None, keyword: str = "", pg=None, limit: int = 3) -> list[dict]:
    """印段落召回: 向量（hnsw）+ 关键词兜底。返回 [{yin_version, section, content, score, matched_by}]。"""
    conn = _get_conn(pg)
    if not conn:
        return []
    out = []
    try:
        if query_emb:
            emb_str = "[" + ",".join(str(x) for x in query_emb) + "]"
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT yin_version, section, content, "
                    " 1 - (embedding <=> %s::vector) AS cosine "
                    " FROM yin_paragraphs WHERE embedding IS NOT NULL "
                    " ORDER BY embedding <=> %s::vector LIMIT %s",
                    (emb_str, emb_str, limit),
                )
                for row in cur.fetchall():
                    out.append({
                        "yin_version": row[0], "section": row[1],
                        "content": row[2], "score": round(float(row[3] or 0), 4),
                        "matched_by": "vector",
                    })
        if keyword and len(out) < limit:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT yin_version, section, content FROM yin_paragraphs "
                    "WHERE content ILIKE %s LIMIT %s",
                    (f"%{keyword}%", limit - len(out)),
                )
                for row in cur.fetchall():
                    out.append({
                        "yin_version": row[0], "section": row[1],
                        "content": row[2], "score": 1.0,
                        "matched_by": "keyword",
                    })
    except PrefetchDeadlineExceeded:
        raise
    except Exception as e:
        logger.warning("yin_pool: 召回失败: %s", str(e)[:150])
    return out
