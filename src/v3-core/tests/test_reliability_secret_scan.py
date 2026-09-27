"""DISPATCH-C1 §3 — secret / privacy gate for the reliability layer.

Plants a known set of sentinel values in:

  * the configured pg dict (``password=``, ``apiKey=``)
  * pending marker q/a payloads (``SENTINEL_QA_XYZ``)
  * synthesized fake PG responses (no body content; row data is metadata only)

Then drives the full ``collect → diagnose → plan`` pipeline plus the
``handle_health`` CLI entry point and asserts that the JSON output never
echoes any sentinel.

The dispatch's gate contract (DESIGN §10):

  * Path labels in default JSON: ``{kind, leaf, hash12}`` — no full
    path; full path only with ``--debug-paths``.
  * Never emitted: DSN userinfo (``v3user:…``), passwords, API keys,
    bearer tokens, ``pending.q``/``pending.a`` contents, raw exception
    bodies beyond a sanitized summary.
  * The check runs against three surfaces: ``HealthReport.to_dict()``,
    ``diagnose`` issues, ``plan_repairs`` actions, and the CLI stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from v3core.reliability import cli as rel_cli
from v3core.reliability.diagnose import diagnose as run_diagnose
from v3core.reliability.failure_reader import FailureReader
from v3core.reliability.health import HealthService
from v3core.reliability.repair import plan_repairs
from v3core.reliability.redaction import sanitize_text


# ──────────────────────────────────────────────────────────────────────
# Sentinels — three classes of secret we plant.
# ──────────────────────────────────────────────────────────────────────

SENTINEL_PASSWORD = "SENTINEL_PASSWORD_XYZ"
SENTINEL_KEY = "SENTINEL_KEY_XYZ"
SENTINEL_QA = "SENTINEL_QA_XYZ"
SENTINEL_DSN = "v3user:lab-local-only"


# ──────────────────────────────────────────────────────────────────────
# Recording fake PG — emits minimal metadata, never full text bodies.
# ──────────────────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, pg: "SecretPg") -> None:
        self.pg = pg
        self._result: Any = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        normalized = " ".join((sql or "").split())
        self.pg.executed.append(normalized)
        s = sql.lower()
        if "from public.schema_versions" in s:
            self._result = (1,) if params and params[0] in self.pg.schema_versions else None
            return
        if "from information_schema.tables" in s:
            self._result = (1,) if params and params[0] in self.pg.tables else None
            return
        if "from pg_indexes" in s:
            self._result = (1,) if params and params[0] in self.pg.indexes else None
            return
        if "from pg_extension" in s and "extname" in s:
            self._result = (self.pg.vector_ext_version,)
            return
        if "from public.qa_pairs" in s:
            self._result = self.pg.qa_pairs_query(sql, params)
            return
        if "from public.explicit_memories" in s:
            self._result = self.pg.explicit_memories_row
            return
        if "from public.qa_embedding_chunks" in s:
            self._result = self.pg.qa_chunks_query(sql, params)
            return
        if "from public.topics" in s:
            self._result = self.pg.topics_row
            return
        if "from public.observation_notes" in s:
            self._result = self.pg.observation_notes_row
            return
        if "from public.yin_paragraphs" in s:
            self._result = self.pg.yin_row
            return
        if s.startswith("select count(*)") and "from public." in s:
            tbl = s.split("from public.", 1)[1].strip().split()[0].rstrip(";")
            self._result = (self.pg.table_counts.get(tbl, 0),)
            return
        if "max(id)" in s and "from public.qa_pairs" in s:
            self._result = (self.pg.max_qa_id,)
            return
        if s.startswith("select 1"):
            self._result = (1,)
            return
        if s.startswith("select version"):
            self._result = ("PostgreSQL 14.10",)
            return
        self._result = None

    def fetchone(self):
        return self._result

    def close(self):
        pass


class _FakeConn:
    def __init__(self, pg: "SecretPg") -> None:
        self.pg = pg

    def cursor(self):
        return _FakeCursor(self.pg)

    def close(self):
        pass


class SecretPg:
    """All metadata-only — never returns row body content."""

    def __init__(self) -> None:
        self.tables: set[str] = {
            "conversation_stream", "qa_pairs", "topics", "topic_entries",
            "observation_notes", "yin_paragraphs", "explicit_memories",
            "qa_embedding_chunks", "schema_versions",
        }
        self.indexes: set[str] = {
            "explicit_memories_embedding_ivfflat", "explicit_memories_status_active_idx",
            "explicit_memories_created_at_idx", "explicit_memories_tags_gin",
            "qa_embedding_chunks_qa_id_idx", "qa_embedding_chunks_embedding_ivfflat",
        }
        self.schema_versions: set[str] = {"v0.2"}
        self.vector_ext_version: str | None = "0.7.4"
        self.table_counts: dict[str, int] = {
            "qa_pairs": 13654, "conversation_stream": 4100, "topics": 18,
            "topic_entries": 612, "observation_notes": 87, "yin_paragraphs": 0,
        }
        self.explicit_memories_row: tuple = (10, 0, 10)
        self.topics_row: tuple = (18, None)
        self.observation_notes_row: tuple = (None,)
        self.yin_row: tuple = (None,)
        self.max_qa_id: int | None = 13654
        self.recent_qa: int = 4
        self.recent_embedding_ok: int = 4
        self.recent_embedding_null: int = 0
        self.recent_empty_answer: int = 0
        self.null_total: int = 0
        self.empty_total: int = 0
        self.empty_recent: int = 0
        self.last_qa_at: Any = _iso(1_700_000_000.0 - 3600)
        self.last_emb_at: Any = _iso(1_700_000_000.0 - 3600)
        self.qa_chunks = {"child_rows": 0, "distinct_parents": 0,
                          "missing_parents": 0, "child_null": 0,
                          "bad_offsets": 0, "dup_keys": 0}
        self.executed: list[str] = []

    def connect(self, *args, **kwargs):
        return _FakeConn(self)

    def qa_pairs_query(self, sql: str, params: Any) -> tuple:
        s = sql.lower()
        if "now() - " in s and "%s || ' hours')" in s and "count(*) filter" in s:
            return (self.recent_qa, self.recent_embedding_ok,
                    self.recent_embedding_null, self.recent_empty_answer)
        if "answer is null or btrim(answer) = ''" in s and "now() - " in s:
            return (self.empty_recent,)
        if "answer is null or btrim(answer) = ''" in s:
            return (self.empty_total,)
        if "count(*)" in s and "embedding is null" in s and "min(created_at)" in s:
            return (self.null_total, None, None)
        if "max(created_at)" in s and "embedding is not null" in s:
            return (self.last_emb_at,)
        if "max(created_at)" in s and "from public.qa_pairs" in s:
            return (self.last_qa_at,)
        return (0,)

    def qa_chunks_query(self, sql: str, params: Any) -> tuple:
        s = sql.lower()
        if "count(distinct qa_id)" in s:
            return (self.qa_chunks["child_rows"],
                    self.qa_chunks["distinct_parents"],
                    self.qa_chunks["child_null"])
        if "left join public.qa_pairs p" in s:
            return (self.qa_chunks["missing_parents"],)
        if "start_offset < 0" in s:
            return (self.qa_chunks["bad_offsets"],)
        if "having count(*) > 1" in s:
            return (self.qa_chunks["dup_keys"],)
        return (0,)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# ──────────────────────────────────────────────────────────────────────
# Marker / config fixture
# ──────────────────────────────────────────────────────────────────────


def _plant_marker_with_sentinel_qa(marker_dir: Path, *, now: float) -> Path:
    """Plant a single marker whose pending.q / pending.a contain
    SENTINEL_QA_XYZ so the failure ledger has the secret to leak.
    The FailureReader contract guarantees the secret never escapes —
    this test pins that."""
    marker_dir.mkdir(parents=True, exist_ok=True)
    p = marker_dir / "secret-marker.json"
    payload = {
        "job_id": "secret-job",
        "session_id": "s",
        "pending": {
            "q_msg_id": "qm-secret",
            "q_turn": 1,
            "q": f"what is the meaning of {SENTINEL_QA}?",
            "a": f"answer involves {SENTINEL_QA} too",
        },
        "embedding_status": "failed",
        "embedding_attempts": 1,
        "error_class": "provider_5xx",
        "last_failure_at": _iso(now - 60),
        "embedding_next_retry_at": now + 3600,
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(p, (now - 60, now - 60))
    return p


def _stub_config_with_sentinal() -> Callable[[], dict]:
    """Returns a config_loader whose embed config carries SENTINEL_KEY
    in the api_key field."""
    return lambda **kw: {
        "embed": {"endpoint": "https://example.com/embed",
                  "model": "m-1", "api_key": SENTINEL_KEY},
        "rerank": {"endpoint": "https://example.com/rerank",
                   "model": "r-1", "api_key": "rerank-x"},
        "llm": {"endpoint": "https://example.com/llm",
                 "model": "l-1", "api_key": "llm-x"},
    }


# ──────────────────────────────────────────────────────────────────────
# Sentinel-scanning helpers
# ──────────────────────────────────────────────────────────────────────


def _assert_no_sentinels(blob: str, *, context: str) -> None:
    """Assert the blob contains no planted secret value."""
    forbidden = [
        SENTINEL_PASSWORD, "lab-local-only",
        "v3user:lab-local-only",
    ]
    for tok in forbidden:
        assert tok not in blob, (
            f"{context}: forbidden token {tok!r} leaked into output"
        )
    # The API key sentinel is checked separately because the report
    # legitimately mentions "api_key" as a key name (in config
    # evidence). We require the *value* to be absent.
    assert SENTINEL_KEY not in blob, (
        f"{context}: api_key sentinel {SENTINEL_KEY!r} leaked into output"
    )
    # And the QA text sentinel — pending q/a must never escape the reader.
    assert SENTINEL_QA not in blob, (
        f"{context}: pending.q / pending.a sentinel {SENTINEL_QA!r} "
        f"leaked into output"
    )


# ──────────────────────────────────────────────────────────────────────
# Pipeline driver — runs collect → diagnose → plan, returns all three.
# ──────────────────────────────────────────────────────────────────────


def _build_pipeline(*, debug_paths: bool = False):
    """Plant the full sentinel world and return (report, issues, actions)."""
    now = 1_700_000_000.0
    pg = SecretPg()
    marker_dir = tempfile.TemporaryDirectory()
    md = Path(marker_dir.name) / "j" / "pending_qa"
    _plant_marker_with_sentinel_qa(md, now=now)

    svc = HealthService(
        pg={
            "host": "localhost", "port": 55432,
            "database": "v3embeddings_alpha",
            "user": SENTINEL_DSN.split(":")[0],
            "password": SENTINEL_PASSWORD,
        },
        pg_connect=pg.connect,
        marker_dir=md,
        now=now,
        window_hours=24,
        debug_paths=debug_paths,
        config_loader=_stub_config_with_sentinal(),
    )
    report = svc.collect()
    issues = run_diagnose(report)
    actions = plan_repairs(issues)
    return report, issues, actions, marker_dir


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────


def test_health_report_no_secrets():
    """The HealthReport JSON never echoes the planted sentinels."""
    report, _, _, _ = _build_pipeline()
    encoded = json.dumps(report.to_dict(), sort_keys=True, ensure_ascii=False)
    _assert_no_sentinels(encoded, context="HealthReport.to_dict()")


def test_diagnose_issues_no_secrets():
    """The diagnose issues JSON never echoes the planted sentinels."""
    _, issues, _, _ = _build_pipeline()
    encoded = json.dumps([i.to_dict() for i in issues],
                         sort_keys=True, ensure_ascii=False)
    _assert_no_sentinels(encoded, context="diagnose() issues")


def test_repair_actions_no_secrets():
    """The repair action list JSON never echoes the planted sentinels."""
    _, _, actions, _ = _build_pipeline()
    encoded = json.dumps([a.to_dict() for a in actions],
                         sort_keys=True, ensure_ascii=False)
    _assert_no_sentinels(encoded, context="plan_repairs() actions")


def test_path_labels_default_omit_full_path():
    """``path_label`` (default mode) emits ``{kind, leaf, hash12}`` and
    nothing else — no full path, even when sentinel directories are
    used."""
    report, _, _, _ = _build_pipeline(debug_paths=False)
    encoded = json.dumps(report.to_dict(), sort_keys=True, ensure_ascii=False)
    # Default mode: no `"path":` key with a real filesystem path. The
    # word may appear in the summary or as a JSON key, but the value
    # must never be a leaf-prefixed full path.
    # Concretely: the path_label hash is a 12-char hex string, never
    # a tempdir leaf name like "tmp_xxxx".
    import re
    leaves = re.findall(r'"leaf":\s*"([^"]+)"', encoded)
    # Leaves must be a basename only — never a tempdir parent (e.g.
    # contains a slash) and never the full sentinel marker filename.
    for leaf in leaves:
        assert "/" not in leaf and "\\" not in leaf, (
            f"path_label leaked full path into leaf: {leaf!r}"
        )
    # No "path" key appears at all in default mode.
    assert '"path":' not in encoded, (
        "default path_label output must not include the 'path' key"
    )


def test_path_labels_debug_paths_includes_full_path():
    """``--debug-paths`` mode *does* include the full path — the test
    proves the toggle works without breaking the secret-gate (the
    sentinel marker still has no q/a content)."""
    report, _, _, _ = _build_pipeline(debug_paths=True)
    encoded = json.dumps(report.to_dict(), sort_keys=True, ensure_ascii=False)
    # The "path" key now appears.
    assert '"path":' in encoded, (
        "debug_paths=True must include 'path' in path_label output"
    )
    # But secret-gate still holds.
    _assert_no_sentinels(encoded, context="debug_paths=True HealthReport")


def test_failure_reader_ledger_no_pending_text():
    """FailureReader ledger JSON never carries pending.q / pending.a
    contents — even when the marker bodies hold the sentinel."""
    report, _, _, marker_handle = _build_pipeline()
    # Run a fresh FailureReader directly against the same marker dir.
    md = list(Path(marker_handle.name).rglob("*.json"))
    md_parent = md[0].parent if md else None
    assert md_parent is not None and md_parent.exists()
    fr = FailureReader(md_parent, now=1_700_000_000.0)
    ledger = fr.read()
    encoded = json.dumps(ledger, sort_keys=True, ensure_ascii=False)
    _assert_no_sentinels(encoded, context="FailureReader.read() ledger")


def test_sanitize_text_strips_kv_patterns():
    """``sanitize_text`` (the emitter shared by health / diagnose /
    repair) drops the value of ``password=…`` and ``apiKey=…``
    patterns. This test pins that contract independently of the
    pipeline.

    Note: ``sanitize_text`` only handles a specific set of well-known
    patterns (``password=…``, ``apiKey=…``, ``token=…``, ``bearer …``,
    and ``scheme://user:pass@…`` DSNs with a scheme prefix). The
    full pipeline's secret gate is enforced by the other tests in this
    file — they verify the *output* never carries the planted values,
    which is the dispatch's contract.
    """
    blob = (
        f"connect user=v3user password={SENTINEL_PASSWORD} "
        f"apiKey={SENTINEL_KEY} token=abc123"
    )
    out = sanitize_text(blob, max_len=4096)
    # The password and api_key values are scrubbed.
    assert SENTINEL_PASSWORD not in out, (
        f"sanitize_text() left the password value in place: {out!r}"
    )
    assert SENTINEL_KEY not in out, (
        f"sanitize_text() left the api_key value in place: {out!r}"
    )
    # The key names are kept (only the value is redacted) — that's
    # the contract of sanitize_text, since callers may want to know
    # which field was redacted.
    assert "password=" in out
    assert "apikey=" in out.lower()


def test_cli_handle_health_stdout_no_secrets(monkeypatch):
    """The ``handle_health`` CLI's stdout is also scanned for sentinels.

    The handler is wired through ``_build_service``; we monkey-patch
    that to inject the same fake-backed service used by the in-process
    pipeline tests.
    """
    report, _, _, _ = _build_pipeline()
    fake_service = _FakeService(report)

    def _fake_build(args):
        return fake_service

    monkeypatch.setattr(rel_cli, "_build_service", _fake_build)

    buf: list[str] = []

    class _FakeStdout:
        def write(self, s):
            buf.append(s)
            return len(s)

        def flush(self):
            pass

    monkeypatch.setattr(rel_cli.sys, "stdout", _FakeStdout())

    args = argparse.Namespace(
        json=True, deep=False, allow_production_read=False,
        profile_dir=None, window_hours=24, debug_paths=False,
    )
    rc = rel_cli.handle_health(args)
    assert rc in (0, 1, 2)
    out = "".join(buf)
    _assert_no_sentinels(out, context="handle_health stdout (json=True)")


# ──────────────────────────────────────────────────────────────────────
# Local helpers (kept private to this module).
# ──────────────────────────────────────────────────────────────────────


class _FakeService:
    """A drop-in for ``HealthService`` for the CLI handler test.

    Matches the test_reliability_cli pattern: only ``.collect()`` is
    consulted by the handler.
    """

    def __init__(self, report) -> None:
        self._report = report
        self.collect_calls = 0

    def collect(self):
        self.collect_calls += 1
        return self._report
