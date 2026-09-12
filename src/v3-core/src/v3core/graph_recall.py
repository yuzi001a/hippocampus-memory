"""图谱索引接入 — 召回加分项 (五合一架构第 3 步)

沿边找关联节点: 输入 topic_id 集合, 返回 1 跳关联 topic 的标题/摘要。
recall_pool 命中主卡后, 用关联提示补全上下文 (不改变主排序)。
"""
from __future__ import annotations
import logging

logger = logging.getLogger("v3core.graph_index")

# 边类型白名单 — 实际图谱边的类型 (2026-08-08 实测):
#   HAS_ENTRY: topic -> entry (主题含条目, 链式溯源主边)
#   MENTIONS_TOPIC: entry -> topic (条目提到主题, 反向关联)
#   OBSERVED_IN: 观察记录关联
# 关联召回用 MENTIONS_TOPIC (entry->topic 的交叉关联) + OBSERVED_IN
_REL_EDGE_TYPES = {"MENTIONS_TOPIC", "OBSERVED_IN"}


def fetch_related_topics(pg, topic_ids: list[str], limit: int = 5) -> list[dict]:
    """输入 topic_id 列表, 返回共现关联 topic 的 {topic_id, title}。

    2026-08-08: 五合一架构第 3 步 — 图谱做索引 (关联路径), 状态卡提供内容。
    2026-08-11: 改为两跳共现 — MENTIONS_TOPIC 边是 note→topic 单向,
       topic 之间无直接边; 正确路径: topic →(反向 MENTIONS_TOPIC)→ note
       →(正向 MENTIONS_TOPIC)→ 其他 topic (同一版印里被一起提炼 = 共现关联)。
    只走 REL 边; 找不到关联时返回 [] (尽力而为, 不阻塞主召回)。
    """
    if not pg or not topic_ids:
        return []
    try:
        conn = pg._connect() if hasattr(pg, "_connect") else pg
        if not conn:
            return []
        cur = conn.cursor()
        # 1. 找输入 topic 的节点 ID
        cur.execute(
            "SELECT node_id FROM v3_graph_nodes "
            "WHERE source_table='topics' AND source_id = ANY(%s) LIMIT %s",
            (list(topic_ids), max(len(topic_ids) * 2, 10)),
        )
        node_ids = [r[0] for r in cur.fetchall()]
        if not node_ids:
            return []
        # 2. 两跳共现: topic → note (反向 MENTIONS_TOPIC) → 其他 topic (正向)
        #    同一印里被一起提炼的主题 = 共现关联 (观察者候选召回的代码真值)
        cur.execute(
            "SELECT DISTINCT n2.source_id, n2.title "
            "FROM v3_graph_edges e1 "
            "JOIN v3_graph_edges e2 ON e2.from_node = e1.from_node "
            "JOIN v3_graph_nodes n2 ON n2.node_id = e2.to_node "
            "WHERE e1.edge_type = 'MENTIONS_TOPIC' "
            "  AND e2.edge_type = 'MENTIONS_TOPIC' "
            "  AND e1.to_node = ANY(%s) "
            "  AND n2.source_table = 'topics' "
            "  AND n2.source_id <> ALL(%s) "
            "LIMIT %s",
            (node_ids, list(topic_ids), max(int(limit), 1)),
        )
        rows = cur.fetchall()
        out = []
        for source_id, title in rows:
            out.append({"topic_id": source_id, "title": title or ""})
        # 3. 若共现为空, 兜底 OBSERVED_IN 一跳: topic 所在印覆盖的 QA 关联其他 topic
        #    (同一观察窗口内出现的主题, 间接共现)
        if not out:
            cur.execute(
                "SELECT DISTINCT n2.source_id, n2.title "
                "FROM v3_graph_edges e1 "
                "JOIN v3_graph_edges e2 ON e2.to_node = e1.from_node "
                "JOIN v3_graph_nodes n2 ON n2.node_id = e2.from_node "
                "WHERE e1.edge_type = 'OBSERVED_IN' "
                "  AND e2.edge_type = 'MENTIONS_TOPIC' "
                "  AND e1.to_node = ANY(%s) "
                "  AND n2.source_table = 'topics' "
                "  AND n2.source_id <> ALL(%s) "
                "LIMIT %s",
                (node_ids, list(topic_ids), max(int(limit), 1)),
            )
            rows = cur.fetchall()
            for source_id, title in rows:
                out.append({"topic_id": source_id, "title": title or ""})
        # 4. 用 topics 表真实标题替换节点 title (节点 title 是 topic_id 本身, 无信息量)
        if out:
            rel_ids = [o["topic_id"] for o in out]
            cur.execute(
                "SELECT topic_id, title FROM topics "
                "WHERE topic_id = ANY(%s) AND status='active'",
                (rel_ids,),
            )
            title_map = {r[0]: (r[1] or "") for r in cur.fetchall()}
            for o in out:
                o["title"] = title_map.get(o["topic_id"], o["title"])
        return out
    except Exception as e:
        logger.debug("fetch_related_topics failed: %s", str(e)[:120])
        return []


def format_related_hint(related: list[dict]) -> str:
    """格式化为注入提示 (短, 不占预算)"""
    if not related:
        return ""
    lines = ["", "【关联主题】(图谱索引, 可能相关)"] 
    for r in related[:5]:
        lines.append(f"- {r.get('title', '')}")
    return "\n".join(lines)
