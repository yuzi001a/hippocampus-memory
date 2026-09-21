"""Offline dry-run: the CANONICAL long-QA plan for the 12 NULL parents.

Read-only. Builds each parent's canonical embedding representation with
`build_qa_embedding_representation` (the same function the live path and the
backfill both call) and reports the resulting chunk plan. It performs NO
embedding requests and writes NOTHING — it exists to answer "what would the
repair actually do, per parent", instead of the useless "12 API calls".

Output per parent: id, is_long, chunk count, total child chunks, per-chunk
(source_field, source_start, source_end, token_count, sha256, embed_text length)
plus the parent-level canonical input hash.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys

sys.path.insert(0, r"C:\Users\servi\workspace\wt-embedfix\src\v3-core\src")

from v3core.config import resolve_config
from v3core.embedding import safe_embed_cfg
from v3core.embed_chunks import build_qa_embedding_representation

ROWS = r"C:\Users\servi\AppData\Local\Temp\qa12.json"


def main() -> int:
    cfg = resolve_config()
    embed_cfg = safe_embed_cfg(cfg)
    if embed_cfg is None:
        print("REFUSING: embed_cfg unavailable (model/endpoint missing) — "
              "the tokenizer plan cannot be built faithfully")
        return 2
    fp = embed_cfg.get("_fingerprint") or ""
    print(f"embed model fingerprint = {fp}")
    print(f"model                   = {embed_cfg.get('model')}")
    print()

    parents = []
    with open(ROWS, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            parents.append((int(r["id"]), r["question"], r["answer"]))

    total_chunks = 0
    plan = []
    print(f"{'qa_id':>8} {'is_long':>8} {'chunks':>7} {'parent_input_sha':>17}  chunk breakdown")
    print("-" * 110)
    for qa_id, q, a in parents:
        short_text = f"{q}\n{a}" if a else q
        rep = build_qa_embedding_representation(q, a, embed_cfg, short_text_for_emb=short_text)
        chunks = list(rep.chunks)
        total_chunks += len(chunks)
        parent_sha = hashlib.sha256(
            "".join(c.source_sha256 for c in chunks).encode() if chunks
            else short_text.encode()
        ).hexdigest()[:16]
        detail = ", ".join(
            f"#{c.chunk_index}:{c.source_field}[{c.source_start}:{c.source_end}]"
            f" tok={c.token_count} sha={c.source_sha256[:8]} len={len(c.embed_text)}"
            for c in chunks
        ) or f"short-path only (len={len(short_text)})"
        print(f"{qa_id:>8} {str(rep.is_long):>8} {len(chunks):>7} {parent_sha:>17}  {detail[:150]}")
        plan.append({
            "qa_id": qa_id,
            "is_long": bool(rep.is_long),
            "chunk_count": len(chunks),
            "parent_input_sha": parent_sha,
            "representation_version": (chunks[0].representation_version if chunks else None),
            "chunks": [{
                "chunk_index": c.chunk_index,
                "source_field": c.source_field,
                "source_start": c.source_start,
                "source_end": c.source_end,
                "token_count": c.token_count,
                "source_sha256": c.source_sha256,
                "embed_text_len": len(c.embed_text),
            } for c in chunks],
        })

    print("-" * 110)
    print(f"PARENT QA COUNT        = {len(parents)}")
    print(f"ESTIMATED CHILD CHUNKS = {total_chunks}")
    print(f"ESTIMATED API CALLS    = {total_chunks} child + {len(parents)} parent aggregation"
          f" (aggregation is local) => {total_chunks} embedding requests")
    print(f"REPRESENTATION VERSION = {plan[0]['representation_version'] if plan else None}")
    out = r"C:\Users\servi\workspace\wt-embedfix\evidence\contamination-20260920\..\long-qa-plan.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"parents": len(parents), "total_child_chunks": total_chunks,
                   "model_fingerprint": fp, "plan": plan}, fh, ensure_ascii=False, indent=2)
    print(f"\nplan written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
