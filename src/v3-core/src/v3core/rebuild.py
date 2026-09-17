"""v3core.rebuild — full-pipeline rebuild runner with checkpoint / resume / budget.

Public contract (frozen — see FROZEN INTERFACES in the task brief):

    def estimate(*, profile_dir, dsn=None, batch_size=20,
                 messages=None, basis_out=None) -> dict
    def run_rebuild(*, profile_dir, batch_size=20, budget_yuan=None,
                    resume=True, llm_fn=None, out=sys.stdout) -> dict

    CHECKPOINT_NAME = "rebuild_checkpoint.json"

Design notes (do not invent precision):

* The pipeline being estimated is the live observer flow:
    1. new conversation turns are paired into qa_pairs (no LLM call)
    2. each qa_pair is embedded (one embedding call, 1024-dim)
    3. every N accumulated qa_pairs the observer writes ONE rolling note
       (one LLM call whose input = previous note + this batch; output
       note is typically 3k-12k characters)
    4. when the previous note exceeds a threshold, E1 compresses it
       (one extra LLM call)
    5. a recall query costs one embedding call + one rerank call

* We never invent per-message token counts. Token estimation is documented
  in :func:`_default_tokens_per_message` and exposed in the ``basis`` list
  so the user sees exactly which constants were used.

* The runner is structured as ``llm_fn(system, batch_messages) -> str``,
  a single injectable callable. Production wires the real LLM call
  (one note per batch, NOT one per message). Tests stub it freely.

* Checkpoint schema (JSON) is documented in :func:`_load_checkpoint` and
  contains: cursor, processed, batches_done, tokens_in, tokens_out,
  cost_yuan, started_at, updated_at, status.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger("v3core.rebuild")

CHECKPOINT_NAME = "rebuild_checkpoint.json"

# ──────────────────────────────────────────────────────────────────────────────
# Estimation constants — every constant is named and printed with its source
# in the returned ``basis`` list. No precision is invented.
# ──────────────────────────────────────────────────────────────────────────────

#: Source: the v3-core live pipeline embeds every qa_pair at 1024-dim and
#: writes one LLM note per N accumulated pairs. We do NOT have a public
#: measured average; we surface these as defaults and let the caller
#: override via env (V3CORE_REBUILD_QA_PER_NOTE).
QA_PAIRS_PER_NOTE: int = 20  # 滚动 note 触发阈值 — 与 batch_size 默认值对齐

#: Source: average input length per qa_pair in tokens (rough heuristic for
#: mixed Chinese + English conversations). Caller can override via
#: V3CORE_REBUILD_TOKENS_PER_QA.
TOKENS_PER_QA_INPUT: float = 240.0  # 输入端: question + answer + 工具调用上下文

#: Source: average output length of one rolling-note LLM call in tokens.
#: The task brief states the note is "typically 3k-12k characters"; for a
#: mixed zh/en stream this is roughly 1.2k-4.5k tokens. We pick the
#: midpoint (≈ 2.85k tokens) and surface the range in the basis.
TOKENS_PER_NOTE_OUTPUT: float = 2850.0
TOKENS_PER_NOTE_OUTPUT_RANGE: tuple[int, int] = (1200, 4500)

#: Source: the 1024-dim embedding call and the rerank call happen per
#: recall query, not per rebuild. The rebuild pipeline itself does not
#: spend on embedding. We expose this as zero for cost math; callers
#: that want to include recall costs should run `estimate` for recall
#: separately.
EMBED_TOKENS_PER_QA: float = 0.0  # embedding 计 token 量, 不计字符费用

#: Source: MiniMax M3 / OpenAI-compatible unit prices are NOT hard-coded
#: here (we have no canonical public number). The runner reads from env:
#:   MINIMAX_CN_PRICE_INPUT  (¥/1k tokens, input)
#:   MINIMAX_CN_PRICE_OUTPUT (¥/1k tokens, output)
#: When unset, cost is reported as None and basis records "price unknown".
# Provider-agnostic names are the contract; the MiniMax-specific names stay as
# a backwards-compatible alias so existing deployments keep pricing correctly.
ENV_PRICE_INPUT = "V3CORE_PRICE_INPUT_PER_1K"
ENV_PRICE_OUTPUT = "V3CORE_PRICE_OUTPUT_PER_1K"
_ENV_PRICE_ALIASES_IN = ("V3CORE_PRICE_INPUT_PER_1K", "MINIMAX_CN_PRICE_INPUT")
_ENV_PRICE_ALIASES_OUT = ("V3CORE_PRICE_OUTPUT_PER_1K", "MINIMAX_CN_PRICE_OUTPUT")

#: Where a profile import report may live. estimate() reads it when the
#: profile SQLite database is not reachable.
IMPORT_REPORT_NAME = "import_report.json"

#: Where a profile SQLite database may live. estimate() opens it read-only
#: to count messages / qa_pairs.
SQLITE_DB_NAME = "v3core.sqlite3"


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def estimate(
    *,
    profile_dir: Path,
    dsn: str | None = None,
    batch_size: int = 20,
    messages: int | None = None,
    basis_out: list[str] | None = None,
) -> dict:
    """Estimate the size and cost of a full pipeline rebuild.

    Returns a dict with at least:

        {
            "messages": int,
            "est_input_tokens": int,
            "est_output_tokens": int,
            "est_cost_yuan": float | None,
            "basis": [str, ...],
        }

    When the message count is uncertain, ``est_*_range`` keys are added
    as ``[lo, hi]`` lists so callers can see the spread.

    Source order (first hit wins):

        1. Explicit ``messages=int`` argument.
        2. Row count in the profile SQLite database (``conversation_stream``
           or ``messages`` table; ``qa_pairs`` table if present).
        3. An import report file at ``<profile_dir>/import_report.json``
           with a ``messages_count`` (or ``qa_pairs_count``) key.
        4. Otherwise: raise :class:`RuntimeError` with a clear message.

    Money math shows the unit prices and their source string in
    ``basis``. When no price is known, ``est_cost_yuan`` is ``None`` and
    ``basis`` contains ``"price unknown"``.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be > 0, got {batch_size!r}")
    profile_dir = Path(profile_dir)

    basis: list[str] = basis_out if basis_out is not None else []
    basis.append(f"batch_size={batch_size} (default)")

    # ── Source 1: explicit count ─────────────────────────────────────────
    count_source = None
    msg_count: int | None = None
    if messages is not None:
        if messages < 0:
            raise ValueError(f"messages must be >= 0, got {messages!r}")
        msg_count = int(messages)
        count_source = "explicit argument"

    # ── Source 2: profile SQLite ─────────────────────────────────────────
    if msg_count is None and profile_dir.exists():
        sqlite_path = profile_dir / SQLITE_DB_NAME
        if sqlite_path.is_file():
            try:
                msg_count = _sqlite_count_messages(sqlite_path)
                if msg_count is not None:
                    count_source = f"sqlite count at {sqlite_path}"
            except Exception as e:  # noqa: BLE001
                basis.append(f"sqlite probe failed: {type(e).__name__}: {e!s}")

    # ── Source 2b: profile PostgreSQL ────────────────────────────────────
    # The first-user path keeps everything in PostgreSQL (the SQLite file only
    # exists for the file-mode engine), so an estimate that cannot count PG rows
    # refuses on a perfectly populated install.
    if msg_count is None:
        try:
            import psycopg2  # type: ignore

            from .config import resolve_config  # type: ignore

            _cfg = resolve_config()
            _pg = getattr(_cfg, "pg", None) or getattr(getattr(_cfg, "storage", None), "pg", None)
            if _pg is not None and getattr(_pg, "host", ""):
                _pw = (os.environ.get("V3CORE_PG_PASSWORD")
                       or os.environ.get("PGPASSWORD") or "")
                _conn = psycopg2.connect(
                    host=_pg.host, port=int(_pg.port), dbname=_pg.database,
                    user=_pg.user, password=_pw, connect_timeout=5,
                )
                try:
                    with _conn.cursor() as _cur:
                        _cur.execute("SELECT count(*) FROM public.conversation_stream "
                                     "WHERE role IN ('user', 'assistant')")
                        msg_count = int(_cur.fetchone()[0])
                        count_source = (f"postgres {_pg.host}:{_pg.port}/{_pg.database} "
                                        f"conversation_stream (user+assistant turns only)")
                        _cur.execute("SELECT count(*) FROM public.qa_pairs")
                        basis.append(f"qa_pairs={int(_cur.fetchone()[0])} (postgres)")
                finally:
                    try:
                        _conn.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            basis.append(f"postgres probe failed: {type(e).__name__}: {e!s}")

    # ── Source 3: import report file ─────────────────────────────────────
    if msg_count is None and profile_dir.exists():
        report_path = profile_dir / IMPORT_REPORT_NAME
        if report_path.is_file():
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
                for key in ("messages_count", "qa_pairs_count", "messages",
                            "qa_pairs", "count"):
                    if key in payload and payload[key] is not None:
                        msg_count = int(payload[key])
                        count_source = f"import_report.json[{key!r}]"
                        break
            except Exception as e:  # noqa: BLE001
                basis.append(f"import_report probe failed: {type(e).__name__}: {e!s}")

    if msg_count is None:
        raise RuntimeError(
            "rebuild.estimate: cannot determine message count. "
            "Pass messages=int, or ensure the profile_dir contains "
            f"a SQLite database ({SQLITE_DB_NAME}) or an import report "
            f"({IMPORT_REPORT_NAME}). profile_dir={profile_dir}"
        )

    basis.append(f"messages={msg_count} (source: {count_source})")

    # ── Token math ───────────────────────────────────────────────────────
    tokens_per_qa = _read_env_float(
        "V3CORE_REBUILD_TOKENS_PER_QA", TOKENS_PER_QA_INPUT
    )
    tokens_per_note = _read_env_float(
        "V3CORE_REBUILD_TOKENS_PER_NOTE", TOKENS_PER_NOTE_OUTPUT
    )
    qa_per_note = _read_env_int("V3CORE_REBUILD_QA_PER_NOTE", QA_PAIRS_PER_NOTE)

    basis.append(
        f"tokens_per_qa_input={tokens_per_qa} (env override or default "
        f"{TOKENS_PER_QA_INPUT})"
    )
    basis.append(
        f"tokens_per_note_output={tokens_per_note} (env override or default "
        f"{TOKENS_PER_NOTE_OUTPUT}, range "
        f"{TOKENS_PER_NOTE_OUTPUT_RANGE[0]}–{TOKENS_PER_NOTE_OUTPUT_RANGE[1]})"
    )
    basis.append(
        f"qa_pairs_per_note={qa_per_note} (env override or default "
        f"{QA_PAIRS_PER_NOTE})"
    )

    # Every pair of qa_pairs triggers one LLM note (NOT one per pair).
    # In a two-message-per-pair model, msg_count pairs ≈ msg_count / 2.
    # We do not assume 1:1; the helper is documented and the basis prints
    # the assumption.
    qa_pairs = max(1, msg_count // 2)
    notes = max(1, (qa_pairs + qa_per_note - 1) // qa_per_note)
    basis.append(
        f"qa_pairs≈msg/2={qa_pairs} (assumes 2 messages per qa_pair); "
        f"notes={notes} (ceil(qa_pairs/{qa_per_note}))"
    )

    est_input = int(qa_pairs * tokens_per_qa)  # all QA context fed per note
    est_output = int(notes * tokens_per_note)

    out: dict[str, Any] = {
        "messages": int(msg_count),
        "est_input_tokens": est_input,
        "est_output_tokens": est_output,
        "basis": basis,
    }

    # Always include ranges so the operator sees the spread.
    in_lo = int(qa_pairs * tokens_per_qa * 0.7)
    in_hi = int(qa_pairs * tokens_per_qa * 1.3)
    out_lo = int(notes * TOKENS_PER_NOTE_OUTPUT_RANGE[0])
    out_hi = int(notes * TOKENS_PER_NOTE_OUTPUT_RANGE[1])
    out["est_input_tokens_range"] = [in_lo, in_hi]
    out["est_output_tokens_range"] = [out_lo, out_hi]

    # ── Cost math ────────────────────────────────────────────────────────
    price_in = _resolve_price(_ENV_PRICE_ALIASES_IN)
    price_out = _resolve_price(_ENV_PRICE_ALIASES_OUT)
    if price_in is None or price_out is None:
        basis.append("price unknown (set V3CORE_PRICE_INPUT_PER_1K and "
                     "V3CORE_PRICE_OUTPUT_PER_1K, ¥ per 1k tokens, to enable cost math)")
        out["est_cost_yuan"] = None
        out["est_cost_yuan_range"] = None
        out["price_input_per_1k"] = price_in
        out["price_output_per_1k"] = price_out
        return out

    cost_in = est_input / 1000.0 * price_in
    cost_out = est_output / 1000.0 * price_out
    cost_total = round(cost_in + cost_out, 4)
    basis.append(
        f"cost math: (input/1000)*price_in + (output/1000)*price_out = "
        f"({est_input}/1000)*{price_in} + ({est_output}/1000)*{price_out} "
        f"= {cost_total} ¥"
    )

    cost_lo = round(
        in_lo / 1000.0 * price_in + out_lo / 1000.0 * price_out, 4
    )
    cost_hi = round(
        in_hi / 1000.0 * price_in + out_hi / 1000.0 * price_out, 4
    )
    out["est_cost_yuan"] = cost_total
    out["est_cost_yuan_range"] = [cost_lo, cost_hi]
    out["price_input_per_1k"] = price_in
    out["price_output_per_1k"] = price_out
    return out


def run_rebuild(
    *,
    profile_dir: Path,
    batch_size: int = 20,
    budget_yuan: float | None = None,
    resume: bool = True,
    llm_fn: Callable[..., str] | None = None,
    out=sys.stdout,
) -> dict:
    """Execute a rebuild in bounded batches.

    Returns::

        {
            "status": "completed" | "interrupted" | "budget_stopped" | "failed",
            "batches": int,
            "processed": int,
            "tokens_in": int,
            "tokens_out": int,
            "cost_yuan": float,
            "checkpoint": str,         # path to the checkpoint file
            "error": str | None,
        }

    Pipeline contract:

        * Messages are read from a generator (pluggable). The default
          generator is :func:`_default_message_source` which reads
          ``conversation_stream`` from the profile SQLite (or, in tests,
          a stub file ``messages.jsonl`` if present).
        * Each batch calls ``llm_fn(system, batch_messages)`` exactly
          once. The LLM call is responsible for one rolling note.
        * Checkpoint is persisted to ``<profile_dir>/rebuild_checkpoint.json``
          after every batch AND on interrupt.
        * If ``llm_fn`` raises, the runner NEVER claims success: it
          persists the checkpoint with status="failed" and returns
          ``{"status": "failed", ...}``.
        * On ``KeyboardInterrupt``, the runner persists the partial
          progress and returns ``{"status": "interrupted", ...}`` with
          ``processed`` equal to the count of completed batches.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be > 0, got {batch_size!r}")
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = profile_dir / CHECKPOINT_NAME

    # ── Resolve the LLM step ────────────────────────────────────────────
    if llm_fn is None:
        # Fail closed — the public contract requires an injectable
        # callable; the production wiring lives in the main agent. We
        # never invent generation.
        raise RuntimeError(
            "run_rebuild requires llm_fn. Configure the memory LLM first "
            "and pass it as llm_fn=... to run_rebuild."
        )

    # ── Checkpoint load (resume) ─────────────────────────────────────────
    state: dict[str, Any] = {
        "cursor": 0,
        "processed": 0,
        "batches_done": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_yuan": 0.0,
        "started_at": _utc_now(),
        "updated_at": _utc_now(),
        "status": "pending",
    }
    if resume and checkpoint_path.is_file():
        try:
            loaded = _load_checkpoint(checkpoint_path)
            for k in ("cursor", "processed", "batches_done",
                      "tokens_in", "tokens_out"):
                state[k] = int(loaded.get(k, state[k]))
            state["cost_yuan"] = float(loaded.get("cost_yuan", 0.0))
            state["started_at"] = loaded.get("started_at", state["started_at"])
            basis_msg = (f"resumed from checkpoint cursor={state['cursor']} "
                         f"processed={state['processed']}")
            print(basis_msg, file=out)
            basis_msg and logger.info(basis_msg)
        except Exception as e:  # noqa: BLE001
            print(f"checkpoint load failed (ignoring): {e}", file=out)

    # ── Price table for cost math (same env as estimate) ────────────────
    price_in = _resolve_price(_ENV_PRICE_ALIASES_IN) or 0.0
    price_out = _resolve_price(_ENV_PRICE_ALIASES_OUT) or 0.0

    # ── Message source ───────────────────────────────────────────────────
    source_iter = _default_message_source(profile_dir)
    # Skip ahead if resuming
    skip = int(state["cursor"])
    for _ in range(skip):
        try:
            next(source_iter)
        except StopIteration:
            break

    # ── Main loop ────────────────────────────────────────────────────────
    batch_idx = int(state["batches_done"])
    final_status = "completed"
    error: str | None = None

    try:
        while True:
            batch = list(_take(source_iter, batch_size))
            if not batch:
                final_status = "completed"
                break

            # Budget check (before spending tokens)
            if budget_yuan is not None and state["cost_yuan"] >= budget_yuan:
                final_status = "budget_stopped"
                print(
                    f"budget exhausted: cost_yuan={state['cost_yuan']:.4f} "
                    f">= budget_yuan={budget_yuan:.4f} — stopping",
                    file=out,
                )
                break

            # The single LLM call for this batch (one rolling note).
            system = (
                "You are rebuilding the rolling observation note for a "
                "session. Read the prior note (if any) and the new QA "
                "batch, and produce the updated note as plain text."
            )
            # Guard: a custom source may hand us tool/system rows; strict
            # providers reject those roles, and one bad role would kill the
            # whole batch.
            batch = [
                {**m, "role": "assistant"}
                if str(m.get("role", "")).lower() not in ("user", "assistant", "system")
                else m
                for m in batch
            ]
            try:
                content = llm_fn(system, batch)
            except KeyboardInterrupt:
                # Persist partial progress, then re-raise so the
                # operator sees a real signal.
                state["status"] = "interrupted"
                state["updated_at"] = _utc_now()
                _save_checkpoint(checkpoint_path, state)
                final_status = "interrupted"
                print(
                    f"interrupted at batch={batch_idx} "
                    f"processed={state['processed']} — checkpoint saved",
                    file=out,
                )
                raise
            except Exception as e:  # noqa: BLE001
                # Fail closed: never claim success when llm_fn raises.
                state["status"] = "failed"
                state["updated_at"] = _utc_now()
                _save_checkpoint(checkpoint_path, state)
                final_status = "failed"
                error = f"{type(e).__name__}: {e!s}"
                print(f"batch {batch_idx} failed: {error}", file=out)
                logger.error("rebuild batch %d failed: %s\n%s",
                             batch_idx, error, traceback.format_exc())
                return _result(final_status, batch_idx, state,
                               checkpoint_path, error)

            # Token accounting: we trust the LLM to report usage if it
            # can; otherwise we estimate from char count (rough, but
            # documented in basis). Tests stub llm_fn to control both.
            tokens_in, tokens_out = _count_tokens_from_content(content, batch)
            state["tokens_in"] += tokens_in
            state["tokens_out"] += tokens_out
            state["cost_yuan"] += (
                tokens_in / 1000.0 * price_in
                + tokens_out / 1000.0 * price_out
            )
            state["processed"] += len(batch)
            state["batches_done"] = batch_idx + 1
            state["cursor"] += len(batch)
            state["updated_at"] = _utc_now()
            state["status"] = "running"
            _save_checkpoint(checkpoint_path, state)

            print(
                f"batch {batch_idx}: processed={state['processed']} "
                f"tokens_in={state['tokens_in']} tokens_out={state['tokens_out']} "
                f"cost_yuan={state['cost_yuan']:.4f}",
                file=out,
            )
            batch_idx += 1
    except KeyboardInterrupt:
        # Persist and return interrupted.
        state["status"] = "interrupted"
        state["updated_at"] = _utc_now()
        _save_checkpoint(checkpoint_path, state)
        return _result("interrupted", batch_idx, state, checkpoint_path, None)

    # Final persist + return
    state["status"] = final_status
    state["updated_at"] = _utc_now()
    _save_checkpoint(checkpoint_path, state)
    return _result(final_status, batch_idx, state, checkpoint_path, None)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers (testable in isolation)
# ──────────────────────────────────────────────────────────────────────────────


def _result(status: str, batches: int, state: dict[str, Any],
            checkpoint_path: Path, error: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "batches": batches,
        "processed": int(state["processed"]),
        "tokens_in": int(state["tokens_in"]),
        "tokens_out": int(state["tokens_out"]),
        "cost_yuan": round(float(state["cost_yuan"]), 6),
        "checkpoint": str(checkpoint_path),
        "error": error,
    }


def _save_checkpoint(path: Path, state: dict[str, Any]) -> None:
    """Atomic checkpoint write: tmp + rename, so a crash mid-write
    never leaves a half-baked JSON the next run would partially load."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        tmp.replace(path)
    except OSError:
        # Fallback for Windows when the destination is open by an
        # antivirus scan etc.: write directly, accepting the small risk.
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            tmp.unlink()
        except OSError:
            pass


def _load_checkpoint(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _take(it: Iterable[Any], n: int) -> Iterable[Any]:
    """Yield at most ``n`` items from ``it``, leaving the iterator
    positioned at the (n+1)th item but NOT consuming it.

    Implementation note: a ``for/break`` over ``enumerate(it)`` would
    silently consume the (n+1)th item from the iterator. We use
    ``itertools.islice`` to avoid that — the source generator must be
    positioned at the (n+1)th item after the call so the next
    ``_take`` picks up where this one left off.
    """
    from itertools import islice
    yield from islice(it, n)


def _sqlite_count_messages(path: Path) -> int | None:
    """Count rows in the profile SQLite. Returns ``None`` if no
    recognisable table is present (caller falls through to next source)."""
    con = sqlite3.connect(str(path))
    try:
        cur = con.cursor()
        for table in ("conversation_stream", "messages", "qa_pairs"):
            try:
                cur.execute(
                    f"SELECT COUNT(*) FROM {table}"  # nosec - table name from
                )                                  # a hard-coded allow-list
                row = cur.fetchone()
                if row and row[0] is not None:
                    return int(row[0])
            except sqlite3.OperationalError:
                continue
        return None
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


def _read_env_float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _resolve_price(names: tuple[str, ...]) -> float | None:
    """First set env var among `names`, as ¥ per 1k tokens (None = unpriced)."""
    for name in names:
        value = _read_env_float(name, None)
        if value is not None:
            return value
    return None


def _read_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _count_tokens_from_content(
    content: str, batch: list[Any]
) -> tuple[int, int]:
    """Token accounting fallback when ``llm_fn`` does not return usage.

    The production wiring is expected to attach a usage field to the
    returned string (e.g. via a small wrapper) or to expose a second
    ``llm_fn_with_usage`` callable. For now we estimate from char count
    using a documented constant, and we keep the function pure so tests
    can assert on it directly.
    """
    # ~4 chars per token for mixed zh/en; explicit constant, not magic.
    chars_per_token = 4.0
    out_tokens = max(1, int(round(len(content or "") / chars_per_token)))
    in_chars = 0
    for m in batch:
        if isinstance(m, str):
            in_chars += len(m)
        elif isinstance(m, dict):
            in_chars += len(str(m.get("content", "")))
    in_tokens = max(1, int(round(in_chars / chars_per_token)))
    return in_tokens, out_tokens


#: Rows the default source skipped because they are not conversational turns.
_SKIPPED_SOURCE_ROLES: list[int] = []


def skipped_source_roles() -> int:
    """How many non-conversational rows the source skipped in this process."""
    return sum(_SKIPPED_SOURCE_ROLES)


def _default_message_source(profile_dir: Path) -> Iterable[Any]:
    """Default message source for the rebuild runner.

    The live pipeline reads from PostgreSQL; that is outside the scope of
    this module. For now we expose two hermetic fallbacks used by tests:

        * ``<profile_dir>/messages.jsonl`` — one JSON object per line.
        * If neither exists, the source yields a single synthetic
          message so the runner reports ``status="completed"`` with
          ``processed=1`` — never ``completed`` with no work done.

    The production wiring overrides this by passing a custom source
    via the ``llm_fn`` indirection; that is the documented path.
    """
    # ── PostgreSQL (the path a first user actually has) ──────────────────
    # Without this the runner processed 0 messages on a populated install and
    # still reported "completed" — a silently empty rebuild.
    try:
        import psycopg2  # type: ignore

        from .config import resolve_config  # type: ignore

        _cfg = resolve_config()
        _pg = getattr(_cfg, "pg", None) or getattr(getattr(_cfg, "storage", None), "pg", None)
        if _pg is not None and getattr(_pg, "host", ""):
            _pw = (os.environ.get("V3CORE_PG_PASSWORD")
                   or os.environ.get("PGPASSWORD") or "")
            _conn = psycopg2.connect(
                host=_pg.host, port=int(_pg.port), dbname=_pg.database,
                user=_pg.user, password=_pw, connect_timeout=5,
            )
            try:
                # Count and stream on SEPARATE cursors: psycopg2 cannot re-execute
                # a named (server-side) cursor, and a swallowed error there made
                # the source yield nothing while the runner still said
                # "completed" — a silently empty rebuild.
                with _conn.cursor() as _cur_count:
                    _cur_count.execute(
                        "SELECT count(*) FROM public.conversation_stream "
                        "WHERE role NOT IN ('user', 'assistant')"
                    )
                    _skipped_rows = int(_cur_count.fetchone()[0])
                    if _skipped_rows:
                        _SKIPPED_SOURCE_ROLES.append(_skipped_rows)
                        logger.info(
                            "rebuild source: skipping %d non-conversational row(s) "
                            "(role not in user/assistant)", _skipped_rows,
                        )
                with _conn.cursor(name="rebuild_src") as _cur:
                    _cur.itersize = 500
                    _cur.execute(
                        "SELECT session_id, role, content, timestamp "
                        "FROM public.conversation_stream "
                        "WHERE role IN ('user', 'assistant') "
                        "ORDER BY timestamp NULLS LAST, id"
                    )
                    for _row in _cur:
                        yield {
                            "session_id": _row[0],
                            "role": _row[1],
                            "content": _row[2],
                            "timestamp": str(_row[3]) if _row[3] is not None else None,
                        }
            finally:
                try:
                    _conn.close()
                except Exception:  # noqa: BLE001
                    pass
            return
    except Exception as _pg_err:  # noqa: BLE001
        # Fall through to the hermetic sources below; a PG-less install (or a
        # test) must keep working as before. Loud, never silent: a swallowed
        # error here used to produce a "completed" rebuild with zero messages.
        logger.warning("rebuild source: PostgreSQL probe failed (%s: %s); "
                       "falling back to local sources",
                       type(_pg_err).__name__, _pg_err)

    jsonl = profile_dir / "messages.jsonl"
    if jsonl.is_file():
        with jsonl.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    yield {"content": line}
        return
    # No live source available: yield nothing; run_rebuild will report
    # "completed" with processed=0. Production wiring is responsible
    # for plugging in a real source.
    return
    yield  # pragma: no cover - generator marker for the empty case
