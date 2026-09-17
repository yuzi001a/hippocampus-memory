# -*- coding: utf-8 -*-
"""test_rebuild_contract.py — pin the v3core.rebuild frozen contract.

What this test pins:

    1. ``estimate`` returns a dict with the required keys; when an
       explicit ``messages=int`` is supplied (and no price env is set),
       it includes a range plus a basis list — and reports
       ``est_cost_yuan = None`` with a "price unknown" basis line.
    2. ``run_rebuild`` processes batches, writes a checkpoint, and
       stops at the budget (``status == "budget_stopped"``).
    3. On a simulated ``KeyboardInterrupt`` mid-run,
       ``status == "interrupted"`` with ``processed`` equal to the
       number of completed batches; a resumed run continues and
       reaches ``"completed"``.
    4. When ``llm_fn`` raises, the runner reports ``status == "failed"``
       and never claims success.

Hermetic: no network, no real database, no real LLM. The default
``llm_fn`` argument is None (fail closed).
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
from pathlib import Path

import pytest

import v3core.rebuild as rebuild


# ──────────────────────────────────────────────────────────────────────────────
# estimate() — required keys, range present, basis list, cost math.
# ──────────────────────────────────────────────────────────────────────────────


def test_estimate_returns_required_keys_with_range(monkeypatch, tmp_path):
    """When the operator passes messages=int, estimate() must return the
    required keys PLUS the range variants, and a non-empty basis list."""
    # Strip any env that would change the math.
    for k in ("MINIMAX_CN_PRICE_INPUT", "MINIMAX_CN_PRICE_OUTPUT",
              "V3CORE_REBUILD_TOKENS_PER_QA",
              "V3CORE_REBUILD_TOKENS_PER_NOTE",
              "V3CORE_REBUILD_QA_PER_NOTE"):
        monkeypatch.delenv(k, raising=False)

    out = rebuild.estimate(
        profile_dir=tmp_path, dsn=None, batch_size=20, messages=1000,
    )
    # Required keys
    for k in ("messages", "est_input_tokens", "est_output_tokens",
              "est_cost_yuan", "basis"):
        assert k in out, f"estimate() missing required key: {k}"
    # Token range keys are always present and non-None.
    for k in ("est_input_tokens_range", "est_output_tokens_range"):
        assert k in out, f"estimate() missing range key: {k}"
        lo, hi = out[k]
        assert lo <= hi, f"range for {k} inverted: {lo} > {hi}"
    # Cost range is None when prices are unknown.
    assert "est_cost_yuan_range" in out
    assert out["est_cost_yuan_range"] is None
    # basis is a non-empty list of strings
    assert isinstance(out["basis"], list)
    assert all(isinstance(s, str) for s in out["basis"])
    assert out["basis"], "basis list must be non-empty"
    # No price env → cost is None
    assert out["est_cost_yuan"] is None
    assert any("price unknown" in s.lower() for s in out["basis"]), (
        "basis must say 'price unknown' when no env price is set; got: "
        f"{out['basis']}"
    )


def test_estimate_uses_sqlite_count_when_no_messages(monkeypatch, tmp_path):
    """When messages=None and a profile SQLite exists with rows, estimate()
    must use the row count (and include the source in the basis)."""
    monkeypatch.delenv("MINIMAX_CN_PRICE_INPUT", raising=False)
    monkeypatch.delenv("MINIMAX_CN_PRICE_OUTPUT", raising=False)

    # Build a fake profile SQLite with 42 rows in conversation_stream.
    db = tmp_path / "v3core.sqlite3"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE conversation_stream (id INTEGER PRIMARY KEY, content TEXT)")
    con.executemany(
        "INSERT INTO conversation_stream (content) VALUES (?)",
        [(f"m{i}",) for i in range(42)],
    )
    con.commit()
    con.close()

    out = rebuild.estimate(profile_dir=tmp_path, messages=None)
    assert out["messages"] == 42
    assert any("sqlite count" in s for s in out["basis"]), (
        f"basis should mention sqlite source: {out['basis']}"
    )


def test_estimate_falls_back_to_import_report(monkeypatch, tmp_path):
    """When no SQLite is present but an import_report.json exists, use it."""
    monkeypatch.delenv("MINIMAX_CN_PRICE_INPUT", raising=False)
    monkeypatch.delenv("MINIMAX_CN_PRICE_OUTPUT", raising=False)
    report = tmp_path / "import_report.json"
    report.write_text(json.dumps({"messages_count": 17}), encoding="utf-8")
    out = rebuild.estimate(profile_dir=tmp_path, messages=None)
    assert out["messages"] == 17
    assert any("import_report" in s for s in out["basis"]), out["basis"]


def test_estimate_raises_when_no_source(monkeypatch, tmp_path):
    """When no source is available, raise a clear RuntimeError."""
    monkeypatch.delenv("MINIMAX_CN_PRICE_INPUT", raising=False)
    monkeypatch.delenv("MINIMAX_CN_PRICE_OUTPUT", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        rebuild.estimate(profile_dir=tmp_path, messages=None)
    msg = str(excinfo.value)
    assert "cannot determine message count" in msg
    assert "import_report" in msg or "sqlite" in msg.lower()


def test_estimate_with_price_env_returns_real_cost(monkeypatch, tmp_path):
    """When the operator sets price env vars, cost math is real."""
    monkeypatch.setenv("MINIMAX_CN_PRICE_INPUT", "0.001")
    monkeypatch.setenv("MINIMAX_CN_PRICE_OUTPUT", "0.002")
    out = rebuild.estimate(profile_dir=tmp_path, messages=1000)
    assert out["est_cost_yuan"] is not None
    assert out["est_cost_yuan"] > 0
    assert out["price_input_per_1k"] == 0.001
    assert out["price_output_per_1k"] == 0.002


def test_estimate_basis_documents_constants(monkeypatch, tmp_path):
    """Every constant used must be named in the basis list — no magic."""
    monkeypatch.delenv("MINIMAX_CN_PRICE_INPUT", raising=False)
    monkeypatch.delenv("MINIMAX_CN_PRICE_OUTPUT", raising=False)
    out = rebuild.estimate(profile_dir=tmp_path, messages=200)
    basis_text = " | ".join(out["basis"])
    for needle in ("batch_size", "messages=", "tokens_per_qa_input",
                   "tokens_per_note_output", "qa_pairs_per_note",
                   "qa_pairs", "notes"):
        assert needle in basis_text, (
            f"basis must name {needle!r} so the operator sees the source; "
            f"got: {out['basis']}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# run_rebuild() — batched / checkpointed / budgeted / interruptible.
# ──────────────────────────────────────────────────────────────────────────────


class _ScriptedLlm:
    """A scripted llm_fn whose behaviour the test controls.

    Modes:
      * raise_after=N → raise KeyboardInterrupt after N successful calls
      * always_raise=exc → raise that exception on every call
      * ok → return a fixed-size synthetic note and record every call
    """

    def __init__(self, *, raise_after: int | None = None,
                 always_raise: BaseException | None = None,
                 note_chars: int = 100):
        self.raise_after = raise_after
        self.always_raise = always_raise
        self.note_chars = note_chars
        self.calls: int = 0
        self.system: list[str] = []
        self.batches: list[list] = []

    def __call__(self, system: str, batch: list):
        self.calls += 1
        self.system.append(system)
        self.batches.append(list(batch))
        if self.always_raise is not None:
            raise self.always_raise
        if self.raise_after is not None and self.calls > self.raise_after:
            raise KeyboardInterrupt(
                f"simulated interrupt after {self.raise_after} calls"
            )
        return "x" * self.note_chars


def _write_messages(profile_dir: Path, n: int) -> None:
    """Write a ``messages.jsonl`` with n synthetic messages so the
    default source has something to yield."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    with (profile_dir / "messages.jsonl").open("w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"id": i, "content": f"msg {i}"}) + "\n")


def test_run_rebuild_processes_batches_and_writes_checkpoint(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("MINIMAX_CN_PRICE_INPUT", raising=False)
    monkeypatch.delenv("MINIMAX_CN_PRICE_OUTPUT", raising=False)
    profile = tmp_path / "profile"
    _write_messages(profile, 50)  # 2.5 batches at batch_size=20
    llm = _ScriptedLlm(note_chars=200)

    out = io.StringIO()
    result = rebuild.run_rebuild(
        profile_dir=profile, batch_size=20, resume=True,
        llm_fn=llm, out=out,
    )

    assert result["status"] == "completed", result
    assert result["processed"] == 50
    # 50 / 20 = 3 batches (with the last partial)
    assert result["batches"] == 3
    assert llm.calls == 3
    # Checkpoint file exists and matches
    cp = profile / rebuild.CHECKPOINT_NAME
    assert cp.is_file()
    cp_data = json.loads(cp.read_text(encoding="utf-8"))
    assert cp_data["status"] == "completed"
    assert cp_data["processed"] == 50
    assert cp_data["batches_done"] == 3
    # The returned 'checkpoint' path matches the file we read.
    assert Path(result["checkpoint"]) == cp
    assert result["error"] is None


def test_run_rebuild_stops_at_budget(monkeypatch, tmp_path):
    """A budget just below the first batch's cost must stop the runner
    after exactly one successful batch (status='budget_stopped')."""
    # Use a very small price so the per-batch cost is easy to reason about.
    monkeypatch.setenv("MINIMAX_CN_PRICE_INPUT", "0.0001")
    monkeypatch.setenv("MINIMAX_CN_PRICE_OUTPUT", "0.0001")
    profile = tmp_path / "profile"
    _write_messages(profile, 100)  # 5 batches at batch_size=20
    # Use a big note so each batch spends a non-trivial amount of ¥.
    llm = _ScriptedLlm(note_chars=4000)  # ~1000 tokens per call

    out = io.StringIO()
    result = rebuild.run_rebuild(
        profile_dir=profile, batch_size=20, budget_yuan=0.0001,
        resume=True, llm_fn=llm, out=out,
    )

    # budget=0.0001 must be exhausted during the very first batch (the
    # first batch's cost is much larger than 0.0001 with the prices
    # above), so the runner reports budget_stopped after the first batch.
    assert result["status"] == "budget_stopped", result
    assert result["processed"] == 20  # one batch worth, exactly
    assert llm.calls == 1
    # Checkpoint is persisted and marked budget_stopped.
    cp = json.loads((profile / rebuild.CHECKPOINT_NAME).read_text(encoding="utf-8"))
    assert cp["status"] == "budget_stopped"


def test_run_rebuild_resume_after_interrupt(monkeypatch, tmp_path):
    """A KeyboardInterrupt mid-run must:
        * leave status='interrupted' (not 'failed' / 'completed')
        * leave processed equal to the number of completed batches
        * persist the checkpoint
        * a subsequent resume must continue and reach 'completed'

    The runner catches KeyboardInterrupt so the operator's parent loop
    is not surprised; the return value is the source of truth.
    """
    monkeypatch.delenv("MINIMAX_CN_PRICE_INPUT", raising=False)
    monkeypatch.delenv("MINIMAX_CN_PRICE_OUTPUT", raising=False)
    profile = tmp_path / "profile"
    _write_messages(profile, 100)  # 5 batches at batch_size=20

    # Raise after 2 successful calls (i.e. interrupt during the 3rd batch).
    llm = _ScriptedLlm(raise_after=2, note_chars=200)

    out = io.StringIO()
    result1 = rebuild.run_rebuild(
        profile_dir=profile, batch_size=20, resume=True,
        llm_fn=llm, out=out,
    )

    # First run: returns status='interrupted' (the runner catches
    # KeyboardInterrupt so the operator's parent loop is not surprised).
    assert result1["status"] == "interrupted", result1
    assert result1["error"] is None
    # 2 batches completed, 20 messages each.
    assert result1["processed"] == 40, result1
    assert result1["batches"] == 2

    # Checkpoint shows the partial state.
    cp = json.loads((profile / rebuild.CHECKPOINT_NAME).read_text(encoding="utf-8"))
    assert cp["status"] == "interrupted"
    assert cp["processed"] == 40
    assert cp["batches_done"] == 2
    assert cp["cursor"] == 40

    # Second run with a fresh llm_fn and resume=True: must continue and
    # reach 'completed'.
    llm2 = _ScriptedLlm(note_chars=200)
    out2 = io.StringIO()
    result2 = rebuild.run_rebuild(
        profile_dir=profile, batch_size=20, resume=True,
        llm_fn=llm2, out=out2,
    )
    assert result2["status"] == "completed", result2
    assert result2["processed"] == 100
    # 2 batches resumed + 3 new = 5 batches total
    assert llm2.calls == 3
    # The checkpoint on disk is now marked completed.
    cp2 = json.loads((profile / rebuild.CHECKPOINT_NAME).read_text(encoding="utf-8"))
    assert cp2["status"] == "completed"


def test_run_rebuild_returns_failed_when_llm_raises(monkeypatch, tmp_path):
    """An llm_fn exception must result in status='failed' and never
    'completed' / 'interrupted' / 'budget_stopped'."""
    monkeypatch.delenv("MINIMAX_CN_PRICE_INPUT", raising=False)
    monkeypatch.delenv("MINIMAX_CN_PRICE_OUTPUT", raising=False)
    profile = tmp_path / "profile"
    _write_messages(profile, 100)
    llm = _ScriptedLlm(always_raise=RuntimeError("simulated LLM boom"))

    out = io.StringIO()
    result = rebuild.run_rebuild(
        profile_dir=profile, batch_size=20, resume=True,
        llm_fn=llm, out=out,
    )

    assert result["status"] == "failed", result
    assert result["error"] and "simulated LLM boom" in result["error"]
    # The runner does not swallow the exception silently: it is recorded
    # in the returned dict and on the printed stream.
    printed = out.getvalue()
    assert "simulated LLM boom" in printed


def test_run_rebuild_requires_llm_fn(tmp_path):
    """The runner fails closed when llm_fn is None."""
    profile = tmp_path / "profile"
    profile.mkdir()
    with pytest.raises(RuntimeError) as excinfo:
        rebuild.run_rebuild(
            profile_dir=profile, batch_size=20, llm_fn=None,
        )
    assert "llm_fn" in str(excinfo.value).lower()
    assert "configure" in str(excinfo.value).lower()


def test_run_rebuild_rejects_invalid_batch_size(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    with pytest.raises(ValueError):
        rebuild.run_rebuild(profile_dir=profile, batch_size=0)


def test_run_rebuild_resume_false_starts_from_zero(monkeypatch, tmp_path):
    """When resume=False, the runner ignores the existing checkpoint
    and starts from cursor=0."""
    monkeypatch.delenv("MINIMAX_CN_PRICE_INPUT", raising=False)
    monkeypatch.delenv("MINIMAX_CN_PRICE_OUTPUT", raising=False)
    profile = tmp_path / "profile"
    _write_messages(profile, 40)
    # Write a stale checkpoint that says processed=40 (the whole corpus).
    profile.mkdir(parents=True, exist_ok=True)
    (profile / rebuild.CHECKPOINT_NAME).write_text(
        json.dumps({
            "cursor": 40, "processed": 40, "batches_done": 2,
            "tokens_in": 0, "tokens_out": 0, "cost_yuan": 0.0,
            "status": "completed", "started_at": "x", "updated_at": "x",
        }),
        encoding="utf-8",
    )
    llm = _ScriptedLlm(note_chars=100)
    out = io.StringIO()
    result = rebuild.run_rebuild(
        profile_dir=profile, batch_size=20, resume=False,
        llm_fn=llm, out=out,
    )
    # resume=False → start from zero, reprocess the whole 40 messages.
    assert result["processed"] == 40
    assert llm.calls == 2
