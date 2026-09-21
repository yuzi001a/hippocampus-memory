"""Repair pass for NULL embeddings — fills the holes the silent-swallow bug left behind.

DESIGN CONSTRAINTS (these are the contract, not preferences)
------------------------------------------------------------
1. **UPDATE-only.** No source table ever loses a row. The one DELETE is the canonical
   sidecar replace (`qa_embedding_chunks` for the single parent being repaired), which is
   exactly what the live `_flush_pending_qa` path already does — it is scoped to one
   parent and is required for correctness when a chunk count shrinks.
2. **Idempotent.** Re-running converges. Rows that already have a vector are never
   selected (`WHERE embedding IS NULL`), and sidecar writes are `ON CONFLICT DO UPDATE`.
3. **Resumable.** Because the selection is "still NULL", a run that dies part-way leaves a
   smaller remaining set. There is no cursor to corrupt.
4. **Dry-run by default.** `--apply` is required to write anything.
5. **Canonical pipeline only.** `qa_pairs` MUST go through
   `build_qa_embedding_representation` + `embed_batch` + `aggregate_parent_embedding` and
   write the sidecar. A bare `UPDATE qa_pairs SET embedding = <one vector>` would produce a
   parent vector that disagrees with what the live path would have produced, and would
   leave long rows without their chunk provenance. Tables with no canonical
   representation defined are **refused**, not guessed.
6. **Per-row failure markers.** A row that cannot be repaired gets a durable marker via
   `embed_for_write`, so it leaves the "unexplained NULL" set honestly instead of being
   silently skipped.
7. **Bounded batches.** One batch at a time, committed as it goes, so a failure does not
   roll back hours of work and memory stays flat.

This module deliberately does NOT import the ingest daemon: a repair pass must not start
writers or touch cursors.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────────────
# Canonical representations. A table appears here only when we can reproduce the EXACT
# text the live path embeds. Anything absent is refused rather than guessed.
# ─────────────────────────────────────────────────────────────────────────────

CANONICAL_TABLES: dict[str, dict] = {
    "qa_pairs": {
        "pk": "id",
        "vec_col": "embedding",
        "model_col": "embed_model",
        "text_col": "question",
        "mode": "qa_chunked",
        "note": "long-QA chunked pipeline + qa_embedding_chunks sidecar",
    },
    "topics": {
        "pk": "id",
        "vec_col": "embedding",
        "model_col": "embed_model",
        "text_col": "title",
        "mode": "plain",
        "note": "topic card text (title/summary/body/keywords)",
    },
    "conversation_stream": {
        "pk": "id",
        "vec_col": "embedding",
        # Production `conversation_stream` has NO embed_model column (verified against
        # information_schema) and no production writer sets one, so the repair UPDATE must
        # not name one — doing so fails the whole statement on the real schema.
        "model_col": None,
        "text_col": "content",
        "mode": "plain",
        "note": "live stream row text, first 2000 chars — same slice ingest._flush uses",
    },
    "observation_notes": {
        "pk": "id",
        "vec_col": "embedding",
        "model_col": "embed_model",
        "text_col": "content",
        "mode": "plain",
        "note": "印 note text",
    },
    "yin_paragraphs": {
        "pk": "id",
        "vec_col": "embedding",
        "model_col": "embed_model",
        "text_col": "content",
        "mode": "plain",
        # PK is a SERIAL assigned at INSERT, so the failure marker is keyed on the natural
        # composite key instead.
        "marker_key_sql": "(t.yin_version || '/' || t.section)",
        "note": ("印 paragraph text — two live writers, two canonical inputs: "
                 "e1.py segments (yin_version LIKE 'e1_seg_%' AND section LIKE 'E1/%') "
                 "embed content[:2000]; yin_pool.py sections embed "
                 "f\"{section}. {content[:1500]}\""),
    },
}


@dataclass
class BackfillStats:
    table: str = ""
    selected: int = 0
    repaired: int = 0
    already_ok: int = 0
    failed_retryable: int = 0
    failed_permanent: int = 0
    skipped_nonretryable: int = 0
    sidecar_written: int = 0
    elapsed_s: float = 0.0
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["elapsed_s"] = round(self.elapsed_s, 2)
        d["errors"] = self.errors[:20]
        return d


def _vec_literal(v) -> str:
    return "[" + ",".join(str(x) for x in v) + "]"


def _yin_writer_kind(row: dict) -> str:
    """Which live writer produced this ``yin_paragraphs`` row?

    ``yin_paragraphs`` is written by two code paths that embed DIFFERENT texts, so the
    canonical repair input depends on the writer, not just the table:

      * ``"e1"``      — e1.py ``_ingest_yin_segments`` via
                        ``pg_store.insert_effective(pool_role="yin_segment")``:
                        stores ``yin_version = "e1_seg_<ts>_<i>"`` (the segment source_id)
                        and ``section = "E1/" + title[:50]``. It embeds ``seg_text[:2000]``.
      * ``"yin_pool"`` — yin_pool.py ``_ingest_yin``: stores ``yin_version = <印 filename>``
                        and ``section = <section title>``. It embeds
                        ``f"{title}. {body[:1500]}"``.

    Both predicates are required: the ``section`` prefix alone could collide with a real 印
    section named "E1/...", and the ``yin_version`` prefix alone would misclassify if a 印
    file were ever named ``e1_seg_*``. Verified against the live database: writer-B rows are
    620/620 matched by both predicates, and all 589 remaining rows (8 of them currently
    NULL) are writer-A rows.
    """
    yin_version = row.get("yin_version") or ""
    section = row.get("section") or ""
    if yin_version.startswith("e1_seg_") and section.startswith("E1/"):
        return "e1"
    return "yin_pool"


def _row_text(table: str, row: dict) -> str:
    """Reproduce the EXACT text the live path embeds for this row."""
    if table == "qa_pairs":
        # The live path builds its own representation; this is only the short-path text.
        q = row.get("question") or ""
        a = row.get("answer") or ""
        return f"{q}\n{a}" if a else q
    if table == "topics":
        parts = [
            row.get("title") or "",
            row.get("summary") or "",
            (row.get("body") or "")[:800],
        ]
        kw = row.get("keywords") or []
        if isinstance(kw, str):
            try:
                kw = json.loads(kw)
            except Exception:
                kw = [kw]
        if isinstance(kw, list):
            parts.append(" ".join(str(x) for x in kw))
        return f"{parts[0]}. {parts[1]} {parts[2]} {parts[3]}".strip()
    if table == "conversation_stream":
        return (row.get("content") or "")[:2000]
    if table == "observation_notes":
        # 印 note text: the live path (pg_store.insert_effective, non-"yin_segment" role)
        # embeds the note content as-is, so bare `content` IS the canonical input here.
        return row.get("content") or ""
    if table == "yin_paragraphs":
        # 印 paragraph text — TWO live writers store TWO different embedding texts in the
        # SAME table, so the canonical input has to be discriminated per row.
        #
        #   Writer A — yin_pool.py (`_ingest_yin`, around line 124):
        #       sections = (title, body) split out of the 印 on "## "
        #       emb_text = f"{title}. {body[:1500]}"
        #       INSERT (yin_version, section, content) VALUES (..., title, body[:6000], ...)
        #       => reconstruct from the row as f"{section}. {content[:1500]}".
        #          Exact: content == body[:6000], so content[:1500] == body[:1500] whenever
        #          len(body) >= 1500, and content == body when len(body) < 1500.
        #
        #   Writer B — e1.py `_ingest_yin_segments` (around line 1027) via
        #              pg_store.insert_effective(pool_role="yin_segment"):
        #       seg_text = "## " + title + "\n\n" + body
        #       emb = _ce(seg_text[:2000], embed_cfg)
        #       stores section = "E1/" + title[:50], content = seg_text[:5000]
        #       => reconstruct from the row as content[:2000].
        #          Exact: content == seg_text[:5000], so content[:2000] == seg_text[:2000]
        #          whenever len(seg_text) >= 2000, and content == seg_text when it is shorter.
        #
        # Embedding bare `content` (the pre-fix behaviour) would produce a vector that no
        # live path ever writes, splitting the table's vectors across two semantic spaces.
        if _yin_writer_kind(row) == "e1":
            return (row.get("content") or "")[:2000]
        section = row.get("section") or ""
        return f"{section}. {(row.get('content') or '')[:1500]}"
    raise KeyError(table)


def _select_sql(spec: dict, include_nonretryable: bool, table: str) -> str:
    """Rows that still need a vector and are worth attempting.

    A row whose previous attempt failed permanently (bad config, unsupported input) is
    skipped unless explicitly included: retrying it on every run would burn the provider
    budget forever and pollute the log with a known-false signal.
    """
    skip = "" if include_nonretryable else """
       AND NOT EXISTS (
             SELECT 1 FROM public.embedding_failures f2
              WHERE f2.entity_table = %(table_literal)s
                AND f2.entity_id = t.{pk}::text
                AND f2.retryable = FALSE
                AND f2.resolved_at IS NULL)
    """.format(pk=spec["pk"])
    return """
        SELECT t.* FROM public.{table} t
         WHERE t.{vec} IS NULL
           AND COALESCE(btrim(t.{txt}), '') <> ''
           {skip}
         ORDER BY t.{pk}
         LIMIT %(limit)s
    """.format(table=table, vec=spec["vec_col"], txt=spec["text_col"],
               pk=spec["pk"], skip=skip)


def backfill_table(conn, table: str, *, embed_cfg: dict, apply: bool,
                   limit: int | None, include_nonretryable: bool = False) -> BackfillStats:
    from v3core.embed_failures import embed_for_write, resolve_embedding_failure

    if table not in CANONICAL_TABLES:
        raise SystemExit(
            f"refused: no canonical embedding representation defined for {table!r}. "
            f"Known: {sorted(CANONICAL_TABLES)}. "
            "Add one deliberately rather than guessing at the text to embed."
        )
    spec = CANONICAL_TABLES[table]
    st = BackfillStats(table=table)
    t0 = time.time()

    sql = _select_sql(spec, include_nonretryable, table)
    params: dict = {"table_literal": table, "limit": limit or 100000}
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    st.selected = len(rows)
    print(f"  selected {st.selected} NULL row(s) in {table} (mode={spec['mode']})")
    if not rows:
        st.elapsed_s = time.time() - t0
        return st

    if not apply:
        for r in rows[:5]:
            txt = _row_text(table, r)
            print(f"    [dry-run] {spec['pk']}={r.get(spec['pk'])} "
                  f"text_len={len(txt)} head={txt[:60]!r}")
        if len(rows) > 5:
            print(f"    [dry-run] ... and {len(rows) - 5} more")
        st.elapsed_s = time.time() - t0
        return st

    for r in rows:
        pk_val = r.get(spec["pk"])
        marker_id = pk_val
        if spec.get("marker_key_sql"):
            # natural/composite key: resolve it for the marker
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {spec['marker_key_sql']} FROM public.{table} t "
                    f"WHERE t.{spec['pk']} = %s", (pk_val,))
                got = cur.fetchone()
            marker_id = got[0] if got else pk_val

        if spec["mode"] == "qa_chunked":
            ok = _repair_qa_row(conn, r, spec, embed_cfg, st, marker_id)
        else:
            ok = _repair_plain_row(conn, r, spec, table, embed_cfg, st, marker_id)

        if ok:
            # Resolve the marker for the phase we actually repaired. A row repaired by the
            # backfill must leave the "unexplained NULL" set without erasing an unrelated
            # unresolved marker from another phase.
            resolve_embedding_failure(conn, entity_table=table, entity_id=str(marker_id),
                                      phase="backfill")

    conn.commit()
    st.elapsed_s = time.time() - t0
    return st


def _repair_plain_row(conn, row, spec, table, embed_cfg, st, marker_id) -> bool:
    from v3core.embedding import BATCH_EMBED_POLICY
    from v3core.embed_failures import embed_for_write

    txt = _row_text(table, row)
    out = embed_for_write(
        txt, embed_cfg,
        entity_table=table, entity_id=str(marker_id),
        phase="backfill", policy=BATCH_EMBED_POLICY, cache=False,
    )
    if not out.ok:
        if out.retryable:
            st.failed_retryable += 1
        else:
            st.failed_permanent += 1
        st.errors.append(f"{table}:{row.get(spec['pk'])} {out.status.value} {out.error_class}")
        return False
    with conn.cursor() as cur:
        if spec.get("model_col"):
            cur.execute(
                f"UPDATE public.{table} SET {spec['vec_col']}=%s::vector, "
                f"{spec['model_col']}=%s WHERE {spec['pk']}=%s",
                (_vec_literal(out.vector), out.model_fingerprint, row.get(spec["pk"])),
            )
        else:
            # table carries no model fingerprint column; store the vector only
            cur.execute(
                f"UPDATE public.{table} SET {spec['vec_col']}=%s::vector "
                f"WHERE {spec['pk']}=%s",
                (_vec_literal(out.vector), row.get(spec["pk"])),
            )
    st.repaired += 1
    return True


def _repair_qa_row(conn, row, spec, embed_cfg, st, marker_id) -> bool:
    """The chunked long-QA repair. Mirrors `_flush_pending_qa` exactly."""
    from v3core.embedding import BATCH_EMBED_POLICY, embed_batch
    from v3core.embed_chunks import (
        build_qa_embedding_representation, aggregate_parent_embedding,
        TokenizerUnavailableError,
    )

    q = row.get("question") or ""
    a = row.get("answer") or ""
    qa_id = row.get("id")
    short_text = f"{q}\n{a}" if a else q

    pending_chunks: list = []
    parent_vec = None
    phase = "embedding"
    try:
        rep = build_qa_embedding_representation(
            q, a, embed_cfg, short_text_for_emb=short_text)
        if rep.is_long:
            phase = "chunk_embedding"
            chunk_texts = [c.embed_text for c in rep.chunks]
            child_vecs = embed_batch(chunk_texts, embed_cfg, collision_safe_key=True)
            if len(child_vecs) != len(rep.chunks):
                raise RuntimeError(
                    f"chunk vector count mismatch: {len(child_vecs)}/{len(rep.chunks)}")
            # Only aggregate once EVERY child succeeded — a parent built from a partial
            # child set would be a vector that means nothing.
            parent_vec = aggregate_parent_embedding(child_vecs)
            pending_chunks = list(zip(rep.chunks, child_vecs))
        else:
            vecs = embed_batch([short_text], embed_cfg)
            parent_vec = vecs[0] if vecs else None
        if not parent_vec or not any(x != 0.0 for x in parent_vec):
            raise RuntimeError("embedding API returned an empty vector")
    except TokenizerUnavailableError as e:
        st.failed_retryable += 1
        st.errors.append(f"qa_pairs:{qa_id} tokenizer unavailable: {e}")
        _mark(conn, "qa_pairs", marker_id, e, phase="tokenizer")
        return False
    except Exception as e:
        st.failed_retryable += 1
        st.errors.append(f"qa_pairs:{qa_id} {type(e).__name__}: {str(e)[:120]}")
        _mark(conn, "qa_pairs", marker_id, e, phase=phase)
        return False

    # Persist: parent vector first, then the sidecar replace (canonical semantics).
    from v3core.pg_store import _resolve_embed_cfg
    fp, _ = _resolve_embed_cfg(embed_cfg)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE public.qa_pairs SET embedding=%s::vector, embed_model=%s WHERE id=%s",
            (_vec_literal(parent_vec), fp, qa_id),
        )
        if pending_chunks:
            # Canonical replace, scoped to THIS parent only — required when a chunk count
            # shrinks, otherwise stale higher-index chunks would survive and corrupt the
            # parent/child provenance.
            cur.execute("DELETE FROM public.qa_embedding_chunks WHERE qa_id=%s", (qa_id,))
            for chunk, child_vec in pending_chunks:
                cur.execute(
                    """
                    INSERT INTO public.qa_embedding_chunks
                        (qa_id, chunk_index, source_field, source_start, source_end,
                         source_sha256, token_count, representation_version, embedding,
                         embed_model, content, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,NOW())
                    ON CONFLICT (qa_id, chunk_index) DO UPDATE SET
                        source_field=EXCLUDED.source_field,
                        source_start=EXCLUDED.source_start,
                        source_end=EXCLUDED.source_end,
                        source_sha256=EXCLUDED.source_sha256,
                        token_count=EXCLUDED.token_count,
                        representation_version=EXCLUDED.representation_version,
                        embedding=EXCLUDED.embedding,
                        embed_model=EXCLUDED.embed_model,
                        content=EXCLUDED.content
                    """,
                    (qa_id, chunk.chunk_index, chunk.source_field,
                     chunk.source_start, chunk.source_end, chunk.source_sha256,
                     chunk.token_count, chunk.representation_version,
                     _vec_literal(child_vec), fp, chunk.text),
                )
                st.sidecar_written += 1
    st.repaired += 1
    return True


def _mark(conn, table, entity_id, exc, phase) -> None:
    """Record a durable failure marker so the row leaves the 'unexplained' set."""
    try:
        from v3core.embed_failures import record_embedding_failure
        record_embedding_failure(conn, entity_table=table, entity_id=str(entity_id),
                                 error=exc, phase=phase)
    except Exception as e:  # never let accounting break the repair run
        print(f"    WARN could not record marker for {table}:{entity_id}: {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Repair NULL embeddings (canonical pipeline only).")
    ap.add_argument("--table", required=True, choices=sorted(CANONICAL_TABLES))
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it this is a dry run.")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--include-nonretryable", action="store_true",
                    help="also retry rows that previously failed permanently")
    ap.add_argument("--out", default=None, help="write stats JSON here")
    args = ap.parse_args(argv)

    from v3core.pg_store import PGStore
    from v3core.config import resolve_config

    cfg = resolve_config()
    embed_cfg = cfg.get("embedding") or cfg.get("embed") or {}
    if not embed_cfg.get("model"):
        print("refused: no embedding model configured (fail-closed)", file=sys.stderr)
        return 2

    pg = PGStore(cfg)
    conn = getattr(pg, "_conn", None) or pg.conn
    print(f"backfill table={args.table} apply={args.apply} limit={args.limit}")
    st = backfill_table(conn, args.table, embed_cfg=embed_cfg, apply=args.apply,
                        limit=args.limit, include_nonretryable=args.include_nonretryable)
    print(json.dumps(st.as_dict(), indent=2, ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(st.as_dict(), fh, indent=2, ensure_ascii=False)
    if st.failed_permanent:
        print(f"VERDICT: BACKFILL_COMPLETED_WITH_PERMANENT_FAILURES ({st.failed_permanent})")
        return 1
    print("VERDICT: BACKFILL_OK" if args.apply else "VERDICT: DRY_RUN_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
