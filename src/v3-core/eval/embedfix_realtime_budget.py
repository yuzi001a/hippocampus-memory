"""#15 — 8s realtime budget validation (read-only).

WHAT THIS MEASURES, AND WHAT IT DELIBERATELY DOES NOT
-----------------------------------------------------
The user-facing 8s budget covers the *complete turn / recall / injection* path. This
harness measures the two components of that path that can actually consume provider or
database time:

  1. the query embedding, under REALTIME_EMBED_POLICY (3s / 0) — the same budget the
     recall path uses, against the real provider;
  2. the vector recall SELECT, against the real production database, read-only.

It deliberately does NOT call `V3Core.prefetch()` end-to-end, because prefetch writes
`delta_run_state.json` into the resolved data dir — which is the LIVE production profile.
Production is read-only this round, so the end-to-end turn measurement belongs to the
canary, and this harness says so instead of quietly performing a production write.

Everything here is SELECT-only and provider-read-only. No INSERT/UPDATE/DELETE/DDL.

The question being answered: **does the durable 10s/2 policy enter any user-blocking
path?** Component 1 pins the recall budget to realtime 3s/0 and measures it; if the
realtime component already fits inside 8s with margin, a background 10s/2 policy cannot
invade the turn (and §5 of EMBEDDING-RELIABILITY.md shows no 10s/2 site is user-blocking).
"""
from __future__ import annotations

import statistics
import sys
import time

sys.path.insert(0, r"C:\Users\servi\workspace\wt-embedfix\src\v3-core\src")

QUERIES = [
    "记忆插件 印 主题卡 召回",
    "观察者 滚动 压缩 膨胀",
    "embedding 超时 静默 NULL",
    "向量模型 一致性 指纹",
    "qa_pairs 配对 合并",
    "冷启动 回放 游标",
    "主题卡 聚簇 合并",
    "注入预算 饱和点",
    "失效 回退 死 fallback",
    "图谱 共现 语义 消融",
    "Hermes gateway 重启",
    "备份 pg_dump 验证",
    "公文 版式 国标",
    "PT 保种 做种 下载",
    "视频 字幕 刮削",
    "股票 分析 报告",
    "Kanban 任务 编排",
    "子代理 派工 验收",
    "开机 自启 自动登录",
    "磁盘 迁移 硬件",
]
N = len(QUERIES)


def pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100.0) * len(xs) + 0.5)) - 1))
    return xs[k]


def main() -> int:
    from v3core.config import resolve_config
    from v3core.embedding import (call_embedding, safe_embed_cfg,
                                  REALTIME_EMBED_POLICY, EmbeddingCallError)

    cfg = resolve_config()
    ec = safe_embed_cfg(cfg)
    if ec is None:
        print("REFUSING: embed_cfg unavailable")
        return 2
    print(f"model={ec.get('model')}  fingerprint={ec.get('_fingerprint')}")
    print(f"policy=REALTIME {REALTIME_EMBED_POLICY.timeout}s / {REALTIME_EMBED_POLICY.retries} retries")
    print(f"samples={N} (query embedding) + {N} (vector recall SELECT)\n")

    # ── 1. query embedding under the realtime budget ────────────────────────
    emb_ms, emb_timeouts, emb_fail = [], 0, 0
    for q in QUERIES:
        t0 = time.perf_counter()
        try:
            v = call_embedding(q, ec, cache=False, policy=REALTIME_EMBED_POLICY)
            dt = (time.perf_counter() - t0) * 1000
            if v and any(x != 0.0 for x in v):
                emb_ms.append(dt)
            else:
                emb_fail += 1
        except EmbeddingCallError as e:
            dt = (time.perf_counter() - t0) * 1000
            cls = getattr(getattr(e, "error_class", None), "value", "?")
            if "TIMEOUT" in str(cls):
                emb_timeouts += 1
            else:
                emb_fail += 1
            print(f"  FAIL {dt:8.1f}ms class={cls} :: {str(e)[:90]}")
        except Exception as e:
            emb_fail += 1
            print(f"  FAIL {type(e).__name__} :: {str(e)[:90]}")

    print("=== 1. query embedding (REALTIME 3s/0) ===")
    if emb_ms:
        print(f"  n={len(emb_ms)}  p50={pct(emb_ms,50):.0f}ms  p95={pct(emb_ms,95):.0f}ms  "
              f"max={max(emb_ms):.0f}ms  mean={statistics.mean(emb_ms):.0f}ms")
    print(f"  provider timeouts={emb_timeouts}  other failures={emb_fail}")

    # ── 2. vector recall SELECT, read-only, production DB ───────────────────
    import psycopg2
    from v3core.pg_store import PgEmbedStore
    rec_ms = []
    try:
        # Let PgEmbedStore resolve its own connection parameters (V3Config exposes them as
        # `cfg.pg`, a dict-shaped config as `cfg["storage"]["pg"]`); probing for a dict here
        # was wrong for the dataclass config and silently skipped the measurement.
        store = PgEmbedStore(cfg)
        with store.lease() as conn:
            if conn is None:
                raise RuntimeError("lease() returned no connection")
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor() as cur:
                cur.execute("SELECT embedding FROM conversation_stream "
                            "WHERE embedding IS NOT NULL LIMIT 1")
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("no vector to probe")
                lit = row[0] if isinstance(row[0], str) else str(row[0])
                for _ in range(N):
                    t0 = time.perf_counter()
                    cur.execute(
                        "SELECT id FROM conversation_stream "
                        "WHERE embedding IS NOT NULL "
                        "ORDER BY embedding <=> %s::vector LIMIT 5", (lit,))
                    cur.fetchall()
                    rec_ms.append((time.perf_counter() - t0) * 1000)
        print("\n=== 2. vector recall SELECT (production, read-only) ===")
        print(f"  n={len(rec_ms)}  p50={pct(rec_ms,50):.0f}ms  p95={pct(rec_ms,95):.0f}ms  "
              f"max={max(rec_ms):.0f}ms")
    except Exception as e:
        print(f"\n=== 2. vector recall SELECT ===  NOT MEASURED: {type(e).__name__}: {str(e)[:140]}")

    # ── 3. verdict against the 8s budget ────────────────────────────────────
    print("\n=== 3. verdict ===")
    worst = (max(emb_ms) if emb_ms else 0) + (max(rec_ms) if rec_ms else 0)
    print(f"  worst observed realtime component sum = {worst:.0f}ms")
    print(f"  budget                                = 8000ms")
    print(f"  headroom                              = {8000-worst:.0f}ms")
    print(f"  durable 10s/2 on a user-blocking path = "
          f"{'YES — REDESIGN REQUIRED' if worst > 8000 else 'NO (see caller matrix)'}")
    print("\n  NOTE: the full turn path was NOT exercised end-to-end here, because "
          "prefetch() writes delta_run_state.json into the live profile and production is "
          "read-only this round. End-to-end turn timing belongs to the canary.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
