"""v3_health - 三链路 + PG + embed + cron 健康检查"""
from __future__ import annotations
import contextlib, json, logging, time
from pathlib import Path
from typing import Any

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

logger = logging.getLogger("v3core.tools.health")
V3_HEALTH_SCHEMA = {
    "name": "v3_health",
    "description": "[3-读取] 系统健康检查 - 三链路(PG/j/y/b) + embed + cron 状态",
    "parameters": {"type": "object", "properties": {}},
}
CURRENT_TABLES = (
    "topics", "conversation_stream", "qa_pairs",
    "topic_entries", "observation_notes", "yin_paragraphs",
)

# ---- P0-E (2026-08-26): 观察者/指纹/E1 真实故障门禁 ----
# backlog 占比超此值且游标停滞 → stalled
_OBSERVER_BACKLOG_RATIO = 0.10
# 失败段达到此数 = 卡死信号(正常重试中的 1-2 条不算)
_FAILED_SEGMENTS_STALL = 5


def _segment_end(seg) -> int | None:
    """兼容读取 qa_range_end / source_end_id / end_id（int 或纯数字 str）；非 dict 或全缺 → None."""
    if not isinstance(seg, dict):
        return None
    for key in ("qa_range_end", "source_end_id", "end_id"):
        if key in seg:
            v = seg[key]
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                if v < 0:
                    return None
                return v
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    continue
                # 纯数字 str：允许可选 +/- 前缀
                if s.isdigit() or (s[0] in "+-" and s[1:].isdigit()):
                    try:
                        v_int = int(s)
                        if v_int < 0:
                            return None
                        return v_int
                    except Exception:
                        continue
                continue
            # 其他类型不认
            continue
    return None


def _classify_failed_segments(segments, cursor) -> dict:
    """分类 failed_segments：total/active/malformed/first（fail-closed）。"""
    container_malformed = segments is not None and not isinstance(segments, list)
    if not isinstance(segments, list):
        segments = []
    total = len(segments)
    malformed = 0
    active = 0
    first = None
    for seg in segments:
        end = _segment_end(seg)  # seg 非 dict 时返回 None
        is_malformed = end is None
        if is_malformed:
            malformed += 1
        # cursor 缺失（None）时所有段按 active 计（fail-closed）
        if cursor is None:
            active += 1
        else:
            if is_malformed:
                active += 1
            elif isinstance(cursor, int) and not isinstance(cursor, bool) and end > cursor:
                active += 1
    if total > 0:
        seg0 = segments[0]
        if isinstance(seg0, dict):
            end0 = _segment_end(seg0)
            malformed0 = end0 is None
            segment_id = seg0.get("segment_id")
            error_class = seg0.get("error_class")
            if error_class is None:
                error_class = seg0.get("error")
            reason = seg0.get("reason")
            if reason is None:
                # 兼容旧 error 字段
                reason = seg0.get("error")
            if reason is not None:
                reason = str(reason)[:80]
            first = {
                "segment_id": segment_id,
                "end": end0,
                "error_class": error_class,
                "reason": reason,
                "malformed": bool(malformed0),
            }
        else:
            # 首段非 dict → malformed
            first = {
                "segment_id": None,
                "end": None,
                "error_class": None,
                "reason": str(seg0)[:80] if seg0 is not None else None,
                "malformed": True,
            }
    return {"total": total, "active": active, "malformed": malformed, "first": first, "container_malformed": container_malformed}


class _PrefetchPgView:
    """Bounded PG view for prefetch/recall_pool compatibility in pool mode."""

    def __init__(self, conn: Any) -> None:
        self._connection = conn

    def is_connected(self) -> bool:
        return self._connection is not None and not getattr(self._connection, "closed", False)

    def _connect(self) -> Any:
        return self._connection


def _read_observer_state(cfg: dict) -> dict:
    """读 observer state 文件; 读不到返回 {'_error': ...} — 调用方按 fail-closed 处理."""
    try:
        base = Path(cfg.get("basePath", str(Path.home() / ".v3-core" / "profiles" / "default")))
        p = Path(base).parent.parent / "observer_state.json" if False else (
            Path(str(base)) / "observer_state.json")
        # basePath 形如 ~/.v3-core/profiles/default, observer state 与其同级
        if not p.exists():
            alt = Path(base).parent / "observer_state.json"
            if alt.exists():
                p = alt
        if not p.exists():
            return {"_error": f"observer_state.json not found under {base}"}
        import json as _json
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"_error": repr(e)[:120]}


def _observer_check(state: dict, qa_head=None, *, qa_count=None, qa_head_id=None, backlog=None, cursor=None) -> dict:
    """observer cursor/backlog/stalled 判定 — fail-closed，度量注入式。

    兼容旧调用：qa_head 作为 COUNT 语义的 qa_count 兼容值。
    新调用应通过 kwargs 注入 qa_count/qa_head_id/backlog/cursor。
    """
    effective_qa_count = qa_count if qa_count is not None else qa_head
    out: dict = {
        "cursor": None,
        "qa_head": effective_qa_count,
        "qa_count": effective_qa_count,
        "qa_head_id": qa_head_id,
        "backlog": backlog,
        "backlog_ratio": None,
        "failed_segments_total": 0,
        "active_failed_segments": 0,
        "malformed_segments": 0,
        "first_failed_segment": None,
        "stalled": True,
    }
    # 1. state 无效 / _error → stalled + error
    if not isinstance(state, dict) or "_error" in state:
        # 尝试保留 cursor 以便观测（若注入）
        cur_tmp = cursor if cursor is not None else (state.get("last_qa_id") if isinstance(state, dict) else None)
        out["cursor"] = cur_tmp
        out["error"] = str(state.get("_error", "unreadable")) if isinstance(state, dict) else repr(state)
        out["failed_segments"] = out["failed_segments_total"]
        return out
    # 2. cursor 判定（注入优先，否则读 state）
    cur = cursor if cursor is not None else state.get("last_qa_id")
    out["cursor"] = cur
    out["qa_head"] = effective_qa_count
    out["qa_count"] = effective_qa_count
    out["qa_head_id"] = qa_head_id
    out["backlog"] = backlog
    if not isinstance(cur, int) or isinstance(cur, bool):
        out["stall_reason"] = "cursor missing/unreadable"
        segs = state.get("failed_segments")
        segs = [] if segs is None else segs
        cls = _classify_failed_segments(segs, None)
        out["failed_segments_total"] = cls["total"]
        out["active_failed_segments"] = cls["active"]
        out["malformed_segments"] = cls["malformed"]
        out["first_failed_segment"] = cls["first"]
        out["failed_segments"] = cls["total"]
        out["backlog_ratio"] = None
        return out
    # 3. metrics 缺失 fail-closed；空表例外：qa_count==0 时 backlog 必 0，ratio=None，不触发阈值
    if effective_qa_count == 0:
        out["backlog"] = 0
        out["backlog_ratio"] = None
        out["qa_head"] = 0
        out["qa_count"] = 0
        out["qa_head_id"] = qa_head_id
    else:
        if backlog is None or effective_qa_count is None:
            out["stall_reason"] = "metrics unavailable (qa_count/backlog missing)"
            segs = state.get("failed_segments")
            segs = [] if segs is None else segs
            cls = _classify_failed_segments(segs, cur)
            out["failed_segments_total"] = cls["total"]
            out["active_failed_segments"] = cls["active"]
            out["malformed_segments"] = cls["malformed"]
            out["first_failed_segment"] = cls["first"]
            out["failed_segments"] = cls["total"]
            out["backlog_ratio"] = None
            return out
        # 4. backlog_ratio 判定（禁用 MAX-cursor，真实 backlog）
        try:
            ratio = backlog / effective_qa_count if effective_qa_count else 0.0
            ratio_r = round(ratio, 4)
            out["backlog_ratio"] = ratio_r
            # 同步 backlog/qa_count 已在 out 中
            if ratio_r > _OBSERVER_BACKLOG_RATIO:
                out["stall_reason"] = f"backlog {backlog} ({ratio_r:.0%}) > threshold"
                segs = state.get("failed_segments")
                segs = [] if segs is None else segs
                cls = _classify_failed_segments(segs, cur)
                out["failed_segments_total"] = cls["total"]
                out["active_failed_segments"] = cls["active"]
                out["malformed_segments"] = cls["malformed"]
                out["first_failed_segment"] = cls["first"]
                out["failed_segments"] = cls["total"]
                return out
        except Exception:
            out["stall_reason"] = "metrics unavailable (backlog ratio calc failed)"
            out["backlog_ratio"] = None
            segs = state.get("failed_segments")
            segs = [] if segs is None else segs
            cls = _classify_failed_segments(segs, cur)
            out["failed_segments_total"] = cls["total"]
            out["active_failed_segments"] = cls["active"]
            out["malformed_segments"] = cls["malformed"]
            out["first_failed_segment"] = cls["first"]
            out["failed_segments"] = cls["total"]
            return out
    # 5. failed_segments 活性判定（active=end>cursor，malformed计active）
    segs = state.get("failed_segments")
    segs = [] if segs is None else segs
    cls = _classify_failed_segments(segs, cur)
    out["failed_segments_total"] = cls["total"]
    out["active_failed_segments"] = cls["active"]
    out["malformed_segments"] = cls["malformed"]
    out["first_failed_segment"] = cls["first"]
    out["failed_segments"] = cls["total"]
    if cls["container_malformed"]:
        out["failed_segments_malformed_container"] = True
        out["stalled"] = True
        out["stall_reason"] = "malformed failed_segments container (not a list)"
        return out
    if cls["malformed"] > 0:
        out["stalled"] = True
        out["stall_reason"] = f"malformed failed_segments={cls['malformed']}"
        return out
    if cls["active"] >= _FAILED_SEGMENTS_STALL:
        out["stalled"] = True
        out["stall_reason"] = f"active_failed_segments={cls['active']} >= {_FAILED_SEGMENTS_STALL}"
        return out
    # 6. 否则健康
    out["stalled"] = False
    out.pop("stall_reason", None)
    return out


def handle_v3_health(args: dict, **kw) -> str:
    result = {"timestamp": time.time(), "checks": {}}
    cfg = {}
    pg = None
    counts = {}
    pool = kw.get("pool") or kw.get("pg_pool")
    if kw.get("runtime_context") and pool is None:
        return json.dumps({
            "success": False,
            "error": "Runtime-backed health requires its PgPool owner",
        }, ensure_ascii=False)

    @contextlib.contextmanager
    def _stage_conn():
        if pool is not None and pg is not None:
            with pg.lease() as leased_conn:
                yield leased_conn
        else:
            yield pg._connect() if pg is not None else None

    try:
        from ..config import resolve_config
        cfg = kw.get("effective_config")
        if cfg is None:
            cfg = resolve_config()
    except Exception:
        pass
    # 解析 base 路径 — j/ 和 y/ 段文件检查需要
    try:
        base = Path(cfg.get("basePath", str(Path.home() / ".v3-core" / "profiles" / "default")))
    except Exception:
        base = Path.home() / ".v3-core" / "profiles" / "default"
    # 1. PG：六张当前核心表必须全部可读
    try:
        from ..pg_store import PgEmbedStore
        if pool is not None:
            pg = PgEmbedStore(config=cfg, pool=pool)
        else:
            pg = PgEmbedStore(config=cfg)

        with _stage_conn() as conn:
            if conn:
                cur = conn.cursor()
                probed = []
                missing = []
                for table in CURRENT_TABLES:
                    try:
                        cur.execute(f"SELECT COUNT(*) FROM {table}")
                        row = cur.fetchone()
                        counts[table] = int(row[0]) if row else 0
                        probed.append(table)
                    except Exception:
                        missing.append(table)
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                pgc = cfg.get("storage", {}).get("pg", {}) if isinstance(cfg, dict) else {}
                result["checks"]["pg"] = {
                    "connected": not missing,
                    "host": pgc.get("host", ""),
                    "topics": counts.get("topics"),
                    "messages": counts.get("conversation_stream"),
                    "qa_pairs": counts.get("qa_pairs"),
                    "topic_entries": counts.get("topic_entries"),
                    "observation_notes": counts.get("observation_notes"),
                    "yin_paragraphs": counts.get("yin_paragraphs"),
                    "yin_current": counts.get("yin_paragraphs"),
                    "core_tables": counts,
                    "probed_tables": probed,
                    "missing_tables": missing,
                }
                if missing:
                    result["checks"]["pg"]["error"] = "missing/unreadable current tables: " + ", ".join(missing)
            else:
                result["checks"]["pg"] = {"connected": False, "missing_tables": list(CURRENT_TABLES), "error": "PG not connected"}
    except Exception as e:
        result["checks"]["pg"] = {"connected": False, "missing_tables": list(CURRENT_TABLES), "error": _safe_err(e)[:200]}
    # 2. 碑 b/ (SQLite) — 信息项，不作为当前主链路健康门槛
    try:
        from ..card_store import DeepStore
        store = DeepStore(config=cfg)
        st = store.status()
        result["checks"]["b"] = {"total_cards": st["total_cards"], "categories": st["by_category"]}
    except Exception as e:
        result["checks"]["b"] = {"error": _safe_err(e)[:200]}
    # 3. 迹 j/
    try:
        jd = base / "j"
        if jd.is_dir():
            flat_files = sorted(jd.glob("*.md"))
            subdirs = [d for d in jd.iterdir() if d.is_dir() and (d / "turns.md").exists()]
            journal_dirs = [d for d in jd.iterdir() if d.is_dir() and d.name.startswith("journal_")]
            journal_files = [f for d in journal_dirs for f in d.glob("*.md")]
            total_files = len(flat_files) + len(subdirs) + len(journal_files)
            if total_files > 0:
                all_times = ([f.stat().st_mtime for f in flat_files]
                             + [(d / "turns.md").stat().st_mtime for d in subdirs]
                             + [f.stat().st_mtime for f in journal_files])
                nt = max(all_times)
                do = (time.time() - nt) / 86400
                result["checks"]["j"] = {"sessions": total_files, "flat_files": len(flat_files), "subdirs": len(subdirs), "journal_files": len(journal_files), "newest": time.strftime("%Y-%m-%d %H:%M", time.localtime(nt)), "days_since_update": round(do, 1), "healthy": do < 1.0}
            else:
                result["checks"]["j"] = {"file_count": 0, "healthy": False}
        else:
            result["checks"]["j"] = {"error": "j/ not found"}
    except Exception as e:
        result["checks"]["j"] = {"error": _safe_err(e)[:200]}
    # 4. 印 y/
    try:
        yd = base / "y"
        if yd.is_dir():
            fc = len(list(yd.glob("*.md")))
            result["checks"]["y"] = {"file_count": fc, "healthy": fc > 0}
        else:
            result["checks"]["y"] = {"error": "y/ not found"}
    except Exception as e:
        result["checks"]["y"] = {"error": _safe_err(e)[:200]}
    # 5. Embed
    try:
        from ..embedding import call_embedding, safe_embed_cfg
        ec = safe_embed_cfg(cfg)
        ep = ec.get("endpoint", "") if ec is not None else ""
        ak = (ec.get("apiKey", "") or ec.get("api_key", "")) if ec is not None else ""
        result["checks"]["embed"] = {"configured": ec is not None, "endpoint": ep[:60] if ep else "(未配置)", "has_key": bool(ak)}
        if ec is not None:
            t0 = time.time()
            emb = call_embedding("health check", ec)
            dt = time.time() - t0
            result["checks"]["embed"]["reachable"] = bool(emb and len(emb) > 0)
            result["checks"]["embed"]["latency_ms"] = round(dt * 1000) if emb and len(emb) > 0 else None
    except Exception as e:
        result["checks"].setdefault("embed", {})["reachable"] = False
        result["checks"]["embed"]["error"] = _safe_err(e)[:200]
    # 6. Prefetch
    try:
        from ..prefetch import prefetch
        if pool is not None and pg is not None:
            if result["checks"].get("pg", {}).get("connected"):
                with pg.lease() as pf_conn:
                    pg_view = _PrefetchPgView(pf_conn)
                    hits = prefetch("health check", limit=1, pg=pg_view)
            else:
                hits = prefetch("health check", limit=1, pg=None)
        else:
            pg_conn = pg if result["checks"].get("pg", {}).get("connected") else None
            hits = prefetch("health check", limit=1, pg=pg_conn)
        result["checks"]["prefetch"] = {"working": True, "hit_count": len(hits)}
    except Exception as e:
        result["checks"]["prefetch"] = {"working": False, "error": _safe_err(e)[:200]}
    # 7. 一致性（旧 b/ 仅观测信息）
    bt = result["checks"].get("b", {}).get("total_cards", 0)
    pc = result["checks"].get("pg", {}).get("topics", 0)
    result["checks"]["consistency"] = {"b_cards": bt, "pg_topics": pc, "note": "b/ 为兼容信息项；PG topics 是当前主数据面"}
    # 8. 主题统计：核心表不完整时显式失败，不吞成健康
    if result["checks"].get("pg", {}).get("connected"):
        try:
            with _stage_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM topics WHERE status='active'"); topics = cur.fetchone()[0]
                    cur.execute("SELECT count(*) FROM topic_entries"); entries = cur.fetchone()[0]
                    cur.execute("SELECT count(*) FROM topic_entries WHERE timestamp >= NOW() - INTERVAL '24 hours'"); active = cur.fetchone()[0]
                result["checks"]["topic"] = {"topics": topics, "entries": entries, "source": "PG topics + topic_entries", "active_24h": active}
        except Exception as e:
            result["checks"]["topic"] = {"error": _safe_err(e)[:200]}
    else:
        result["checks"]["topic"] = {"error": "current topic data plane unavailable"}
    # 9. 证据链覆盖率（2026-08-22 G1C）：表在但链断 = 假健康，必须显式降级。
    #    指标: 直接 QA 来源覆盖率 / embedding 覆盖率 / 可展开原文覆盖率。
    #    source_qa_id 列可能尚未迁移（G1A 执行），列缺失时 reported as schema_pending，不算失败。
    evidence_chain_degraded = False
    if result["checks"].get("pg", {}).get("connected"):
        try:
            with _stage_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM topic_entries")
                    ec_total = int(cur.fetchone()[0])
                    cur.execute("SELECT count(*) FROM topic_entries WHERE embedding IS NOT NULL")
                    ec_with_emb = int(cur.fetchone()[0])
                    cur.execute("SELECT COALESCE(sum(char_length(COALESCE(question,'')||COALESCE(answer,''))),0) FROM topic_entries")
                    ec_chars = int(cur.fetchone()[0] or 0)
                coverage = {
                    "entries_total": ec_total,
                    "embedding_coverage": round(ec_with_emb / ec_total, 4) if ec_total else None,
                }
                # source_qa_id 覆盖率 — 列存在才统计
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT count(*) FROM topic_entries WHERE source_qa_id IS NOT NULL")
                    ec_sourced = int(cur.fetchone()[0])
                    cur.execute("SELECT count(*) FROM topic_entries te JOIN qa_pairs qp ON qp.id = te.source_qa_id")
                    ec_expandable = int(cur.fetchone()[0])
                    cur.close()
                    coverage["source_qa_coverage"] = round(ec_sourced / ec_total, 4) if ec_total else None
                    coverage["expandable_fulltext_coverage"] = round(ec_expandable / ec_total, 4) if ec_total else None
                    coverage["schema_pending"] = False
                except Exception as e_col:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    msg = str(e_col).lower()
                    if "source_qa_id" in msg and "does not exist" in msg:
                        coverage["schema_pending"] = True   # G1A 未执行 — 观测项非故障
                    else:
                        raise
                # 判定链断裂: 有 entry 但 embedding 覆盖率 < 50%（生产现状 19359/19628 无向量 → 必降级）
                if ec_total and ec_with_emb / ec_total < 0.5:
                    coverage["degraded"] = True
                    coverage["reason"] = (
                        f"embedding coverage {ec_with_emb}/{ec_total} below 0.5 — "
                        "evidence chain broken; G1A rebuild required"
                    )
                    evidence_chain_degraded = True
                result["checks"]["evidence_chain"] = coverage
        except Exception as e:
            result["checks"]["evidence_chain"] = {"error": _safe_err(e)[:200]}
            evidence_chain_degraded = True
    else:
        result["checks"]["evidence_chain"] = {"skipped": "pg not connected"}
    pg_ok = result["checks"].get("pg", {}).get("connected", False)
    prefetch_ok = result["checks"].get("prefetch", {}).get("working", False)
    topic_ok = not result["checks"].get("topic", {}).get("error")
    # ---- P0-E 三门禁 (2026-08-26): observer / fingerprint / E1, fail-closed ----
    # 10. Observer: cursor/backlog/stalled/failed_segments
    try:
        qa_count = counts.get("qa_pairs") if pg_ok else None
        if not pg_ok:
            obs_state: dict = {"_error": "pg not connected"}
        else:
            obs_state = _read_observer_state(cfg)
        cur_for_metrics = None
        qa_head_id = None
        backlog = None
        metrics_error = None
        if pg_ok and isinstance(obs_state, dict) and "_error" not in obs_state:
            cur_for_metrics = obs_state.get("last_qa_id")
            if isinstance(cur_for_metrics, int):
                try:
                    with _stage_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute("SELECT COALESCE(MAX(id), 0) FROM qa_pairs")
                            qa_head_id = int(cur.fetchone()[0])
                            cur.execute("SELECT COUNT(*) FROM qa_pairs WHERE id > %s", (cur_for_metrics,))
                            backlog = int(cur.fetchone()[0])
                except Exception as e:
                    metrics_error = _safe_err(e)[:160]
        obs = _observer_check(obs_state, qa_count, qa_count=qa_count, qa_head_id=qa_head_id,
                              backlog=backlog, cursor=cur_for_metrics)
        if metrics_error:
            obs["metrics_error"] = metrics_error
        result["checks"]["observer"] = obs
    except Exception as e:
        result["checks"]["observer"] = {"stalled": True, "error": _safe_err(e)[:160]}
    # 11. 指纹分布: topics 表向量非空但指纹空 = 旁路 → fail
    fingerprint_ok = False
    if pg_ok:
        try:
            with _stage_conn() as conn2:
                with conn2.cursor() as cur2:
                    cur2.execute(
                        "SELECT COALESCE(embed_model,''), count(*) FROM topics "
                        "WHERE embedding IS NOT NULL GROUP BY 1")
                    dist = {str(k): int(v) for k, v in cur2.fetchall()}
                dirty = sum(n for k, n in dist.items() if not k)
                result["checks"]["fingerprint"] = {
                    "models": dist, "dirty": dirty, "ok": dirty == 0 and len(dist) <= 1}
                fingerprint_ok = result["checks"]["fingerprint"]["ok"]
        except Exception as e:
            result["checks"]["fingerprint"] = {"ok": False, "error": _safe_err(e)[:160]}
    else:
        result["checks"]["fingerprint"] = {"ok": False, "error": "pg not connected"}
    # 12. E1 同窗重复印 — yin_paragraphs 是"版本×段落"结构, 每版固定 3 个 section
    #     (我是谁/怎么判断/行为基线), 同版 3 行是正常形态。真正的多写故障 =
    #     同一 (version, section) 出现 >1 行。
    e1_ok = False
    if pg_ok:
        try:
            with _stage_conn() as conn3:
                with conn3.cursor() as cur3:
                    cur3.execute(
                        "SELECT count(*) FROM (SELECT yin_version, section, count(*) AS c "
                        "FROM yin_paragraphs GROUP BY yin_version, section HAVING count(*) > 1) d")
                    dup = int(cur3.fetchone()[0])
                result["checks"]["e1"] = {"duplicate_section_rows": dup, "ok": dup == 0}
                e1_ok = dup == 0
        except Exception as e:
            result["checks"]["e1"] = {"ok": False, "error": _safe_err(e)[:160]}
    else:
        result["checks"]["e1"] = {"ok": False, "error": "pg not connected"}
    # 13. LLM
    llm_ok = False
    try:
        from ..llmstatus import build_llm_check
        llm_check = build_llm_check(cfg)
        llm_ok = not llm_check["degraded"]
        result["checks"]["llm"] = llm_check
    except Exception as e:
        result["checks"]["llm"] = {"degraded": True, "error": _safe_err(e)[:160]}
    observer_stalled = bool(result["checks"].get("observer", {}).get("stalled", True))
    result["healthy"] = bool(pg_ok and prefetch_ok and topic_ok
                             and fingerprint_ok and e1_ok and not observer_stalled and llm_ok)
    gate_summary = {
        "pg": "connected=" + str(pg_ok),
        "prefetch": "working=" + str(prefetch_ok),
        "topic": "ok=" + str(topic_ok),
        "fingerprint": "ok=" + str(fingerprint_ok),
        "e1": "ok=" + str(e1_ok),
        "observer_stalled": str(observer_stalled),
        "llm": "ok=" + str(llm_ok),
    }
    hard_fail = not result["healthy"]
    result["evidence_chain_degraded"] = evidence_chain_degraded
    result["summary"] = (
        ("异常: " + "; ".join(f"{k}={v}" for k, v in gate_summary.items()))
        if hard_fail
        else ("DEGRADED: evidence chain coverage below threshold (see checks.evidence_chain)"
              if evidence_chain_degraded else "OK"))
    return json.dumps(result, ensure_ascii=False)
