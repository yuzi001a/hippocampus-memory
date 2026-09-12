from ._deadline import PrefetchDeadlineExceeded, coerce_deadline


def _maybe_expand_query(query, pool=None, deadline=None):
    # 短查询 -> glossary 本地匹配（实时链路 8s 预算内禁止 M3/LLM 实时调用）
    # 策略: <=6字查 topic 标题（PG ILIKE 优先, SQLite 兜底）；未命中返回原 query
    # 说明: 原 Step 2 M3 扩展已删除。M3 在生产实测 30s+，必炸 8s 实时预算；
    #       短查询无法命中的情况下，宁可让 recall_pool 用原 query 直查，
    #       也比拖到 Hermes 端超时更安全。
    from .config import _resolve_data_dir
    deadline = coerce_deadline(deadline)
    # P1.2-A1: deadline-aware guard. The glossary SQL path runs against
    # the same pool that prefetch's keyword/QA/yin/notes paths use, so
    # the same PrefetchDeadlineExceeded is raised on exhaustion. Caller
    # layers above MUST let this exception propagate (it is intentionally
    # NOT a ValueError/TypeError so the ordinary ``except Exception``
    # fallbacks elsewhere cannot swallow it accidentally).
    if deadline is not None:
        deadline.check()

    q = query.strip()
    if not q:  # 空字符串直接返回，避免掉进 glossary 触发 ILIKE '%%'
        return q
    if len(q) >= 6:
        return q

    # Step 1: glossary from topic titles (PG 优先, SQLite 兜底)
    try:
        _title = ''
        if pool is not None:
            # PG 路径 (pool lease 模式: 借出单个 bounded lease, 绝不调 psycopg2.connect)
            try:
                # P1.2-A1: 池 lease 同时绑 deadline, 等连接超时与 statement_timeout
                # 都受 deadline 约束; 池内的 QueryCanceled 也会被 recall_pool 翻译成
                # PrefetchDeadlineExceeded。deadline=None 时旧行为完全保持。
                if deadline is None:
                    _lease_context = pool.lease(timeout=3)
                else:
                    _lease_context = pool.lease(timeout=3, deadline=deadline)
                with _lease_context as _lease:
                    # 兼容 PgLease 与普通 conn — PgLease.connection 暴露 pinned conn。
                    _conn = getattr(_lease, "connection", _lease)
                    # Refresh per-connection statement_timeout if the lease
                    # carries one (PgLease.refresh_statement_timeout is the
                    # canonical hook). No-op for legacy conns.
                    refresh = getattr(_lease, "refresh_statement_timeout", None)
                    if deadline is not None and callable(refresh):
                        refresh()
                    with _conn.cursor() as _cur:
                        _cur.execute(
                            "SELECT title FROM topics WHERE status='active' AND title ILIKE %s LIMIT 1",
                            (f'%{q}%',)
                        )
                        _r = _cur.fetchone()
                        if _r:
                            _title = _r[0]
            except PrefetchDeadlineExceeded:
                raise
            except Exception:
                pass
        else:
            # PG 路径 (legacy direct connect 模式)
            try:
                import psycopg2
                from .config import resolve_config
                _cfg = resolve_config()
                if _cfg and _cfg.pg:
                    _pg = psycopg2.connect(
                        host=_cfg.pg.host, port=_cfg.pg.port,
                        dbname=_cfg.pg.database, user=_cfg.pg.user,
                        password=_cfg.pg.password,
                    )
                    with _pg.cursor() as _cur:
                        if deadline is not None:
                            deadline.check(context="glossary SQL")
                        _cur.execute(
                            "SELECT title FROM topics WHERE status='active' AND title ILIKE %s LIMIT 1",
                            (f'%{q}%',)
                        )
                        _r = _cur.fetchone()
                        if _r:
                            _title = _r[0]
                    _pg.close()
            except PrefetchDeadlineExceeded:
                raise
            except Exception:
                pass

        if not _title:
            # SQLite 兜底
            import os as _os, sqlite3 as _sqlite3
            _db = str(_resolve_data_dir() / 'v3_topic_full.db')  # 生产 db; 历史 8286.db 实验名已废弃
            if _os.path.exists(_db):
                _c = _sqlite3.connect(_db)
                _r = _c.execute("SELECT title FROM topic_blocks WHERE title LIKE ? LIMIT 1", (f'%{q}%',)).fetchone()
                if _r:
                    _title = _r[0]
                _c.close()

        if _title:
            import logging
            logging.getLogger(__name__).info("query_expand: glossary hit '%s' -> '%s'", q, _title)
            return _title
    except PrefetchDeadlineExceeded:
        raise
    except Exception:
        pass

    # glossary 未命中 → 返回原 query（实时链路禁用 M3 扩展）
    return q
