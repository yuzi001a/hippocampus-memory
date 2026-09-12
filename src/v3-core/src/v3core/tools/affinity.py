"""共鸣度 — 读取碑卡数据计算关系量化"""

from __future__ import annotations
import json
import logging
import math
from datetime import datetime
from pathlib import Path


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

logger = logging.getLogger("v3core.affinity")

V3_AFFINITY_SCHEMA = {
    "name": "v3_affinity",
    "description": "[4-管理] 计算并展示共鸣度 — 基于碑卡数据的关系量化",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

LEVELS = [(60, "暗"), (200, "烛火"), (400, "灯"), (700, "焰"), (1000, "昼")]

def resolve_level(score: int):
    for i, (threshold, name) in enumerate(LEVELS):
        if score <= threshold:
            return name, i
    return "昼", 4


def compute_affinity(store) -> dict:
    idx = store.get_index()
    files = idx.get("files", {})
    if not files:
        return {"success": False, "error": "卡库为空", "score": 0, "level": "暗", "level_index": 0}

    cats = {}
    all_tags = set()
    timestamps = []
    for rel, meta in files.items():
        cat = meta.get("category") or "uncategorized"
        cats[cat] = cats.get(cat, 0) + 1
        for t in meta.get("tags") or []:
            if isinstance(t, str) and len(t.strip()) > 2:
                all_tags.add(t.strip())
        if meta.get("mtime"):
            timestamps.append(meta["mtime"])

    if timestamps:
        first_ts = min(timestamps)
        first_date = datetime.fromtimestamp(first_ts)
        span_days = max(1, int((max(timestamps) - min(timestamps)) / 86400))
    else:
        first_date = datetime.now()
        span_days = 1

    sc = cats.get("session_summary", 0) + cats.get("session_summaries", 0)
    lessons = cats.get("lessons", 0)
    decisions = cats.get("decisions", 0)
    projects = cats.get("projects", 0)
    creative = cats.get("creative", 0)
    memory = cats.get("memory", 0)
    evolution = cats.get("evolution", 0)
    rules = cats.get("rules", 0)

    c = min(100, int(math.log1p(span_days) * 15 + math.log1p(sc) * 8))
    g = min(100, int(math.log1p(lessons * 3 + decisions * 5) * 6))
    cr = min(100, int(math.log1p(projects * 8 + creative * 20) * 6))
    k = min(100, int(math.log1p(memory * 6 + evolution * 15 + rules * 3 + len(all_tags) * 0.3) * 6))
    total = min(1000, int((c + g + cr + k) * 2.5))
    level_name, level_idx = resolve_level(total)

    miles = []
    if span_days >= 30: miles.append("满月")
    elif span_days >= 14: miles.append("半月")
    if sc >= 100: miles.append("百次对话")
    elif sc >= 50: miles.append("第50次对话")
    elif sc >= 25: miles.append("第25次对话")
    elif sc >= 10: miles.append("第十次对话")
    if projects > 0: miles.append("有合作项目")
    if creative > 0: miles.append("有创作成果")
    if evolution > 5: miles.append("身份成形")

    return {
        "success": True, "score": total, "level": level_name,
        "level_index": level_idx,
        "dimensions": {"陪伴": c, "共进": g, "共创": cr, "知心": k},
        "milestones": miles,
        "since": first_date.strftime("%Y-%m-%d"),
        "span_days": span_days, "session_count": sc,
        "total_cards": len(files), "tags_count": len(all_tags),
    }


def _cache_path(store) -> Path:
    return store._cards_dir().parent / "affinity.json"


def load_cached(store):
    p = _cache_path(store)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def save_cache(store, data):
    try:
        _cache_path(store).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("save affinity cache failed: %s", e)


def render_text(data, prev=None):
    if not data.get("success"):
        return ""
    delta = ""
    if prev and prev.get("success"):
        d = data["score"] - prev["score"]
        if d > 0:
            delta = f"  +{d}"
        elif d < 0:
            delta = f"  {d}"
    d = data["dimensions"]
    s = data["score"]
    lv = data["level"]
    sd = data["span_days"]
    sc = data["session_count"]
    tc = data["total_cards"]
    tg = data["tags_count"]
    k1 = "陪伴"
    k2 = "共进"
    k3 = "共创"
    k4 = "知心"

    lines = [
        f"  {s} / {lv}{delta}",
        "",
        f"  {d.get(k1,0)} 陪伴 |  相识 {sd} 天, {sc} 次对话",
        f"  {d.get(k2,0)} 共进 |  {tc} 张碑卡积澱",
        f"  {d.get(k3,0)} 共创 |  {tg} 个话题标签",
        f"  {d.get(k4,0)} 知心 |  {tg} 个标签覆盖",
    ]
    if data["milestones"]:
        parts = "  ".join(f"* {m}" for m in data["milestones"])
        lines.append("")
        lines.append("  " + parts)
    return "\n".join(lines)


def handle_v3_affinity(args, **kw):
    try:
        from ..card_store import DeepStore
        from ..config import resolve_config
        cfg = resolve_config("default")
        store = DeepStore(cfg)
        prev = load_cached(store)
        data = compute_affinity(store)
        save_cache(store, data)
        data["render"] = render_text(data, prev)
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": _safe_err(e)[:200],
        }, ensure_ascii=False)
