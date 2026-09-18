"""Health collector (DESIGN §4).

Drives the full RT01..RT04 / ST01..ST05 / MW01..MW06 / FA01..FA05 /
DM01..DM04 / PR01..PR03 check matrix and assembles a ``HealthReport``.

Key contracts:

  * **One connection per ``collect()`` call** — every PG query reuses the
    single ``pg_connect`` connection, except the deep-auth probes which
    open at most 3 ephemeral HTTP connections.
  * **All SQL must be SELECT-only** — enforced at test time via a recording
    fake connection (the upgrade-contract FakePg style). Non-SELECT
    statements are a contract violation.
  * **Production boundary** — when ``is_production_target(...)`` is true
    and ``allow_production_read=False``, every storage/memory_write/derived
    check reports ``skip`` with a pointer to ``--allow-production-read``.
    Failure-accounting (marker files) still runs because it never touches
    PG.
  * **All check evidence is JSON-safe** — paths are redacted through
    ``redaction.path_label``; DSN secrets never appear in output.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .failure_reader import (
    FailureReader,
    STATUS_MALFORMED,
    STATUS_POISONED,
    STATUS_RETRY_DUE,
    STATUS_RETRYABLE,
    STATUS_STALE_IN_FLIGHT,
    STATUS_STALE_PENDING_DB,
    STATUS_UNRESOLVED,
)
from .models import (
    CheckResult,
    HealthReport,
    OVERALL_HEALTHY,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_UNKNOWN,
    STATUS_WARN,
    aggregate_overall,
)
from .redaction import is_production_target, path_label, sanitize_text

logger = logging.getLogger(__name__)


# Contract constants from DESIGN §2 — exported for reuse by diagnose / cli.
REPORT_SCHEMA_VERSION = "1"
HEALTH_WINDOW_HOURS_DEFAULT = 24
STALE_MARKER_HOURS = 48
AUTH_TIMEOUT_SECONDS = 10

CANONICAL_TABLES: tuple[str, ...] = (
    "conversation_stream", "qa_pairs", "topics", "topic_entries",
    "observation_notes", "yin_paragraphs", "explicit_memories",
    "qa_embedding_chunks", "schema_versions",
)
CANONICAL_INDEXES: tuple[str, ...] = (
    "explicit_memories_embedding_ivfflat", "explicit_memories_status_active_idx",
    "explicit_memories_created_at_idx", "explicit_memories_tags_gin",
    "qa_embedding_chunks_qa_id_idx", "qa_embedding_chunks_embedding_ivfflat",
)
EXPECTED_SCHEMA_VERSION = "v0.2"

# Provider error classes — kept as a module constant so diagnose / repair
# can import the same names.
PROVIDER_ERR_401 = "provider_401"
PROVIDER_ERR_402 = "provider_402"
PROVIDER_ERR_429 = "provider_429"
PROVIDER_ERR_5XX = "provider_5xx"
PROVIDER_ERR_TIMEOUT = "provider_timeout"
PROVIDER_ERR_CONNECTION = "provider_connection"


# ── default config / auth runners (lazy imports so the module loads
#    cleanly even when doctor_full is missing — tests inject their own).

def _default_config_loader(*, profile: str = "default") -> dict[str, Any]:
    """Resolve the v3-core config to a legacy dict (used by PR01).

    Wrapped in a try/except so a missing config never crashes ``collect``;
    PR01 then reports fail with a sanitized detail.
    """
    try:
        from v3core import config as _config_mod
        cfg = _config_mod.resolve_config(profile, return_legacy=True)
        if not isinstance(cfg, dict):
            # Newer typed path: try to_legacy_dict().
            try:
                cfg = cfg.to_legacy_dict()
            except Exception:
                cfg = {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception as exc:
        logger.warning("config_loader failed: %s", sanitize_text(str(exc)))
        return {}


def _default_deep_auth_runner(cfg_view: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    """Lazy import ``v3core.doctor_full`` and run the three auth probes.

    Returns a single dict suitable to splice into PR03 evidence::

        {"embedding": <result>, "rerank": <result>, "llm": <result>}

    Never raises — every inner call is wrapped.
    """
    out: dict[str, Any] = {"embedding": None, "rerank": None, "llm": None}
    try:
        from v3core import doctor_full as _df
    except Exception as exc:
        for k in out:
            out[k] = {
                "status": STATUS_FAIL,
                "summary": "doctor_full unavailable: " + sanitize_text(str(exc)),
            }
        return out
    runners = (
        ("embedding", getattr(_df, "_check_auth_embedding", None)),
        ("rerank", getattr(_df, "_check_auth_rerank", None)),
        ("llm", getattr(_df, "_check_auth_llm", None)),
    )
    for key, fn in runners:
        if fn is None:
            out[key] = {"status": STATUS_SKIP, "summary": f"{key}_auth_runner_missing"}
            continue
        try:
            out[key] = fn(cfg_view, timeout)
        except Exception as exc:
            out[key] = {
                "status": STATUS_FAIL,
                "summary": sanitize_text(str(exc)),
            }
    return out


# ── HealthService ──


class HealthService:
    """Read-only collector. Inject ``pg_connect`` to keep tests offline."""

    def __init__(
        self,
        *,
        profile_dir: Path | None = None,
        base_path: Path | None = None,
        pg: Mapping[str, Any] | None = None,
        pg_connect: Callable[..., Any] | None = None,
        marker_dir: Path | None = None,
        module_file: str | Path | None = None,
        now: float | None = None,
        window_hours: int = HEALTH_WINDOW_HOURS_DEFAULT,
        allow_production_read: bool = False,
        deep: bool = False,
        debug_paths: bool = False,
        deep_auth_runner: Callable[..., dict[str, Any]] | None = None,
        config_loader: Callable[..., dict[str, Any]] | None = None,
    ):
        self.profile_dir = Path(profile_dir) if profile_dir is not None else None
        self.base_path = Path(base_path) if base_path is not None else None
        self.pg = dict(pg) if isinstance(pg, Mapping) else {}
        self.pg_connect = pg_connect
        self.marker_dir = Path(marker_dir) if marker_dir is not None else None
        self.module_file = (
            str(module_file) if module_file is not None else _default_module_file()
        )
        self.now = float(now) if now is not None else time.time()
        self.window_hours = int(window_hours)
        self.allow_production_read = bool(allow_production_read)
        self.deep = bool(deep)
        self.debug_paths = bool(debug_paths)
        self.deep_auth_runner = deep_auth_runner or _default_deep_auth_runner
        self.config_loader = config_loader or (
            lambda **kw: _default_config_loader()
        )

    # ── entry point ──
    def collect(self) -> HealthReport:
        t0 = time.time()
        # Establish the single DB connection up front; reuse it across
        # every PG check. If production boundary + no opt-in, we *don't*
        # even open the connection — ST/MW/DM checks will all skip.
        is_prod = is_production_target(
            self.pg.get("host"), self.pg.get("port"), self.pg.get("database")
        )
        allow_pg = (not is_prod) or self.allow_production_read

        conn = None
        conn_err: str | None = None
        if self.pg_connect is not None and self.pg:
            if not allow_pg:
                conn = None
            else:
                try:
                    kwargs = dict(self.pg)
                    kwargs["connect_timeout"] = 5
                    conn = self.pg_connect(**kwargs)
                except Exception as exc:
                    conn = None
                    conn_err = sanitize_text(str(exc))

        try:
            checks: list[CheckResult] = []
            sections: dict[str, dict[str, Any]] = {
                "runtime": {}, "storage": {}, "memory_write": {},
                "failure_accounting": {}, "derived_memory": {}, "providers": {},
            }

            self._collect_runtime(checks, sections)
            self._collect_storage(
                checks, sections, conn=conn, conn_err=conn_err, allow_pg=allow_pg
            )
            self._collect_memory_write(
                checks, sections, conn=conn, allow_pg=allow_pg
            )
            self._collect_failure_accounting(
                checks, sections,
                last_success_at=sections.get("memory_write", {}).get(
                    "last_embedding_success_at"
                ),
            )
            self._collect_derived_memory(
                checks, sections, conn=conn, allow_pg=allow_pg
            )
            self._collect_providers(checks, sections)

            # Metrics — totals + check count.
            metrics = self._build_metrics(checks, sections, conn=conn, allow_pg=allow_pg)
            sections["metrics"] = metrics

            overall = aggregate_overall(checks)
            generated_at = _iso_from_epoch(self.now)

            report = HealthReport(
                schema_version=REPORT_SCHEMA_VERSION,
                overall=overall,
                generated_at=generated_at,
                profile={
                    "profile_dir_label": path_label(
                        self.profile_dir or (self.base_path or "."), "profile",
                        debug_paths=self.debug_paths,
                    ),
                    "config_parsed": False,
                },
                runtime=sections["runtime"],
                storage=sections["storage"],
                memory_write=sections["memory_write"],
                failure_accounting=sections["failure_accounting"],
                derived_memory=sections["derived_memory"],
                providers=sections["providers"],
                metrics=metrics,
                checks=checks,
                window_hours=self.window_hours,
                deep=self.deep,
            )
            report.collection_seconds = time.time() - t0  # type: ignore[attr-defined]
            return report
        finally:
            # Close the single connection.
            if conn is not None:
                close = getattr(conn, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    # ── section collectors ──
    def _collect_runtime(self, checks: list[CheckResult], sections: dict[str, dict[str, Any]]) -> None:
        # RT01 import-source classification.
        kind = _classify_module_file(self.module_file)
        if kind == "editable":
            rt01_status = STATUS_FAIL
            rt01_summary = "v3core imported from editable install (path contains 'v3-memory-plugin')"
        elif kind == "unknown":
            rt01_status = STATUS_WARN
            rt01_summary = "v3core import path not recognized as site-packages or editable"
        else:
            rt01_status = STATUS_OK
            rt01_summary = "v3core imported from site-packages (shipped contract)"
        rt01 = CheckResult(
            check_id="RT01_import_source",
            section="runtime",
            status=rt01_status,
            summary=rt01_summary,
            evidence={
                "kind": kind,
                "label": path_label(self.module_file, "module_file", debug_paths=self.debug_paths),
            },
            duration_ms=0,
        )
        checks.append(rt01)

        # RT02 distribution — best-effort via importlib.metadata; never raise.
        rt02_status = STATUS_OK
        rt02_evidence: dict[str, Any] = {
            "v3_core_version": None,
            "v3_hermes_plugin_version": None,
            "entry_point_present": False,
        }
        try:
            from importlib import metadata as _md
            try:
                rt02_evidence["v3_core_version"] = _md.version("v3-core")
            except Exception:
                pass
            try:
                rt02_evidence["v3_hermes_plugin_version"] = _md.version("v3-hermes-plugin")
            except Exception:
                pass
            eps = _md.entry_points()
            for ep in eps:
                if ep.group and "v3" in ep.group:
                    rt02_evidence["entry_point_present"] = True
                    break
        except Exception as exc:
            rt02_status = STATUS_WARN
            rt02_evidence["error"] = sanitize_text(str(exc))

        if rt02_evidence["v3_core_version"] is None:
            rt02_status = STATUS_FAIL
        elif (
            rt02_evidence["v3_hermes_plugin_version"] is None
            or not rt02_evidence["entry_point_present"]
        ):
            rt02_status = STATUS_WARN

        checks.append(CheckResult(
            check_id="RT02_distribution",
            section="runtime",
            status=rt02_status,
            summary="v3-core / v3-hermes-plugin distribution metadata",
            evidence=rt02_evidence,
            duration_ms=0,
        ))

        # RT03 python — version + executable label.
        import sys
        py_evidence = {
            "executable_label": path_label(sys.executable, "python_executable", debug_paths=self.debug_paths),
            "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        }
        checks.append(CheckResult(
            check_id="RT03_python",
            section="runtime",
            status=STATUS_OK,
            summary=f"Python {py_evidence['version']}",
            evidence=py_evidence,
            duration_ms=0,
        ))

        # RT04 profile — config parsed via the injected loader.
        cfg = {}
        try:
            cfg = self.config_loader() or {}
        except Exception as exc:
            cfg = {"_load_error": sanitize_text(str(exc))}
        parsed = bool(cfg) and not cfg.get("_load_error")
        checks.append(CheckResult(
            check_id="RT04_profile",
            section="runtime",
            status=STATUS_OK if parsed else STATUS_WARN,
            summary="config parsed" if parsed else "config not parsed",
            evidence={
                "profile_label": path_label(
                    self.profile_dir or (self.base_path or "."), "profile",
                    debug_paths=self.debug_paths,
                ),
                "config_parsed": parsed,
                "base_path_label": path_label(
                    self.base_path or ".", "base_path", debug_paths=self.debug_paths
                ),
            },
            duration_ms=0,
        ))

        sections["runtime"] = {
            "import_source": rt01.evidence,
            "distribution": rt02_evidence,
            "python": py_evidence,
            "profile": {
                "parsed": parsed,
            },
        }

    def _collect_storage(
        self,
        checks: list[CheckResult],
        sections: dict[str, dict[str, Any]],
        *,
        conn: Any,
        conn_err: str | None,
        allow_pg: bool,
    ) -> None:
        # If not allowed to touch PG, every storage check is skip.
        if not allow_pg:
            for check_id, summary in (
                ("ST01_pg_reachable", "production read not authorized; pass --allow-production-read"),
                ("ST02_pgvector", "production read not authorized; pass --allow-production-read"),
                ("ST03_schema_ledger", "production read not authorized; pass --allow-production-read"),
                ("ST04_canonical_tables", "production read not authorized; pass --allow-production-read"),
                ("ST05_canonical_indexes", "production read not authorized; pass --allow-production-read"),
            ):
                checks.append(CheckResult(
                    check_id=check_id, section="storage",
                    status=STATUS_SKIP, summary=summary,
                    evidence={"skipped_reason": "production_boundary_no_opt_in"},
                    duration_ms=0,
                ))
            sections["storage"] = {"skipped_reason": "production_boundary_no_opt_in"}
            return

        if conn is None:
            # No connection (either no pg_connect or it raised). ST01 fails.
            checks.append(CheckResult(
                check_id="ST01_pg_reachable",
                section="storage",
                status=STATUS_FAIL,
                summary="postgres connection failed",
                evidence={"reachable": False, "error": conn_err or "no_connection"},
                duration_ms=0,
            ))
            for check_id in ("ST02_pgvector", "ST03_schema_ledger", "ST04_canonical_tables", "ST05_canonical_indexes"):
                checks.append(CheckResult(
                    check_id=check_id, section="storage",
                    status=STATUS_UNKNOWN, summary="dependent on ST01",
                    evidence={"dependent": "ST01_pg_reachable"},
                    duration_ms=0,
                ))
            sections["storage"] = {"reachable": False, "error": conn_err}
            return

        # ST01 pg_reachable — SELECT 1.
        st01_ev: dict[str, Any] = {"reachable": False}
        st01_status = STATUS_FAIL
        st01_summary = "postgres SELECT 1 failed"
        st01_t0 = time.time()
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT 1")
                row = cur.fetchone()
                if row and row[0] == 1:
                    st01_status = STATUS_OK
                    st01_summary = "postgres reachable"
                    st01_ev["reachable"] = True
            finally:
                close = getattr(cur, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        except Exception as exc:
            st01_ev["error"] = sanitize_text(str(exc))
        st01_ev["latency_ms"] = int((time.time() - st01_t0) * 1000)
        # Server version — best-effort, never fatal. Use a SELECT so the
        # dispatch's "all SQL is SELECT-only" contract holds.
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT version()")
                row = cur.fetchone()
                if row:
                    st01_ev["server_version"] = str(row[0])
            finally:
                close = getattr(cur, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        except Exception:
            pass
        checks.append(CheckResult(
            check_id="ST01_pg_reachable", section="storage",
            status=st01_status, summary=st01_summary,
            evidence=st01_ev, duration_ms=int((time.time() - st01_t0) * 1000),
        ))
        sections["storage"]["reachable"] = st01_ev["reachable"]

        if st01_status != STATUS_OK:
            for check_id in ("ST02_pgvector", "ST03_schema_ledger", "ST04_canonical_tables", "ST05_canonical_indexes"):
                checks.append(CheckResult(
                    check_id=check_id, section="storage",
                    status=STATUS_UNKNOWN, summary="dependent on ST01",
                    evidence={"dependent": "ST01_pg_reachable"},
                    duration_ms=0,
                ))
            return

        # ST02 pgvector.
        st02 = _scalar_query(
            conn,
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'",
            "pg_extension",
        )
        if st02 is None:
            checks.append(CheckResult(
                check_id="ST02_pgvector", section="storage",
                status=STATUS_FAIL, summary="pgvector extension not installed",
                evidence={"present": False}, duration_ms=0,
            ))
        else:
            checks.append(CheckResult(
                check_id="ST02_pgvector", section="storage",
                status=STATUS_OK, summary=f"pgvector {st02} present",
                evidence={"present": True, "version": st02}, duration_ms=0,
            ))

        # ST03 schema_ledger — does the v0.2 row exist?
        v02_present = _has_schema_version_row(conn, EXPECTED_SCHEMA_VERSION)
        if v02_present:
            checks.append(CheckResult(
                check_id="ST03_schema_ledger", section="storage",
                status=STATUS_OK, summary=f"schema_versions row '{EXPECTED_SCHEMA_VERSION}' present",
                evidence={"expected_version": EXPECTED_SCHEMA_VERSION, "present": True},
                duration_ms=0,
            ))
        else:
            checks.append(CheckResult(
                check_id="ST03_schema_ledger", section="storage",
                status=STATUS_FAIL,
                summary=f"schema_versions row '{EXPECTED_SCHEMA_VERSION}' missing",
                evidence={"expected_version": EXPECTED_SCHEMA_VERSION, "present": False},
                duration_ms=0,
            ))

        # ST04 canonical tables.
        missing = _missing_tables(conn, CANONICAL_TABLES)
        if not missing:
            checks.append(CheckResult(
                check_id="ST04_canonical_tables", section="storage",
                status=STATUS_OK, summary=f"all {len(CANONICAL_TABLES)} canonical tables present",
                evidence={"missing": [], "expected": list(CANONICAL_TABLES)},
                duration_ms=0,
            ))
        else:
            checks.append(CheckResult(
                check_id="ST04_canonical_tables", section="storage",
                status=STATUS_FAIL,
                summary=f"{len(missing)} canonical table(s) missing",
                evidence={"missing": list(missing), "expected": list(CANONICAL_TABLES)},
                duration_ms=0,
            ))

        # ST05 canonical indexes.
        missing_idx = _missing_indexes(conn, CANONICAL_INDEXES)
        if not missing_idx:
            checks.append(CheckResult(
                check_id="ST05_canonical_indexes", section="storage",
                status=STATUS_OK, summary=f"all {len(CANONICAL_INDEXES)} canonical indexes present",
                evidence={"missing": [], "expected": list(CANONICAL_INDEXES)},
                duration_ms=0,
            ))
        else:
            checks.append(CheckResult(
                check_id="ST05_canonical_indexes", section="storage",
                status=STATUS_WARN,
                summary=f"{len(missing_idx)} canonical index(es) missing",
                evidence={"missing": list(missing_idx), "expected": list(CANONICAL_INDEXES)},
                duration_ms=0,
            ))

        sections["storage"] = {
            "reachable": True,
            "missing_tables": list(missing),
            "missing_indexes": list(missing_idx),
        }

    def _collect_memory_write(
        self,
        checks: list[CheckResult],
        sections: dict[str, dict[str, Any]],
        *,
        conn: Any,
        allow_pg: bool,
    ) -> None:
        if not allow_pg or conn is None:
            for check_id in ("MW01_write_pipeline_recent", "MW02_embedding_debt",
                             "MW03_empty_answer", "MW04_last_writes",
                             "MW05_explicit_memory_embedding", "MW06_longqa_child_consistency"):
                checks.append(CheckResult(
                    check_id=check_id, section="memory_write",
                    status=STATUS_SKIP, summary="production read not authorized; pass --allow-production-read",
                    evidence={"skipped_reason": "production_boundary_no_opt_in"},
                    duration_ms=0,
                ))
            sections["memory_write"] = {"skipped_reason": "production_boundary_no_opt_in"}
            return

        # MW01 — current-incident detector.
        recent_qa, recent_ok, recent_null, recent_empty = _recent_qa_window(conn, self.window_hours)
        if recent_qa == 0:
            # No traffic in the window is "not applicable" (fresh / idle
            # install) — not an unknown state and never a failure.
            mw01_status = STATUS_SKIP
            mw01_summary = "no qa_pairs rows in the recent window — pipeline idle"
        elif recent_null > 0:
            mw01_status = STATUS_FAIL
            mw01_summary = f"{recent_null} recent qa row(s) with NULL embedding"
        else:
            mw01_status = STATUS_OK
            mw01_summary = f"{recent_qa} recent qa rows, embedding_ok={recent_ok}"
        checks.append(CheckResult(
            check_id="MW01_write_pipeline_recent", section="memory_write",
            status=mw01_status, summary=mw01_summary,
            evidence={
                "recent_qa": recent_qa,
                "recent_embedding_ok": recent_ok,
                "recent_embedding_null": recent_null,
                "recent_empty_answer": recent_empty,
                "window_hours": self.window_hours,
            },
            duration_ms=0,
        ))

        # MW02 — historical debt (ok; debt reported via diagnose).
        null_total, oldest_null, newest_null = _embedding_null_debt(conn)
        checks.append(CheckResult(
            check_id="MW02_embedding_debt", section="memory_write",
            status=STATUS_OK,
            summary=f"{null_total} historical rows with NULL embedding (debt)",
            evidence={
                "embedding_null_total": null_total,
                "oldest_null_created_at": oldest_null,
                "newest_null_created_at": newest_null,
            },
            duration_ms=0,
        ))

        # MW03 — empty answers.
        empty_total, empty_recent = _empty_answer_counts(conn, self.window_hours)
        if empty_recent > 0:
            mw03_status = STATUS_WARN
            mw03_summary = f"{empty_recent} recent empty-answer row(s)"
        else:
            mw03_status = STATUS_OK
            mw03_summary = f"{empty_total} empty-answer rows historical"
        checks.append(CheckResult(
            check_id="MW03_empty_answer", section="memory_write",
            status=mw03_status, summary=mw03_summary,
            evidence={"empty_answer_total": empty_total, "empty_answer_recent": empty_recent},
            duration_ms=0,
        ))

        # MW04 — last writes / ages.
        last_qa_at, last_emb_ok_at = _last_writes(conn)
        now = self.now
        age_qa = (now - _iso_to_epoch(last_qa_at)) if last_qa_at else None
        age_emb = (now - _iso_to_epoch(last_emb_ok_at)) if last_emb_ok_at else None
        if last_qa_at is None:
            # No writes yet — fresh install. Not applicable, not unknown.
            mw04_status = STATUS_SKIP
        else:
            mw04_status = STATUS_OK
        checks.append(CheckResult(
            check_id="MW04_last_writes", section="memory_write",
            status=mw04_status, summary="last write timestamps" if mw04_status == STATUS_OK else "no writes yet",
            evidence={
                "last_qa_created_at": last_qa_at,
                "last_embedding_success_at": last_emb_ok_at,
                "age_qa_seconds": age_qa,
                "age_embedding_seconds": age_emb,
            },
            duration_ms=0,
        ))

        # MW05 — explicit memory embedding presence.
        explicit_total, explicit_null, explicit_set = _explicit_memory_counts(conn)
        if explicit_null > 0:
            mw05_status = STATUS_WARN
            mw05_summary = f"{explicit_null} explicit memory row(s) with NULL embedding"
        else:
            mw05_status = STATUS_OK
            mw05_summary = f"{explicit_total} explicit memory rows, all with embeddings"
        checks.append(CheckResult(
            check_id="MW05_explicit_memory_embedding", section="memory_write",
            status=mw05_status, summary=mw05_summary,
            evidence={
                "explicit_total": explicit_total,
                "explicit_embedding_null": explicit_null,
                "explicit_embedding_set": explicit_set,
            },
            duration_ms=0,
        ))

        # MW06 — longqa child consistency.
        child_rows, distinct_parents, missing_parents, child_null, bad_offsets, dup_keys = (
            _longqa_child_stats(conn)
        )
        bad = (
            (missing_parents > 0)
            or (child_null > 0)
            or (bad_offsets > 0)
            or (dup_keys > 0)
        )
        if bad:
            mw06_status = STATUS_FAIL
            mw06_summary = "qa_embedding_chunks integrity violations"
        else:
            mw06_status = STATUS_OK
            mw06_summary = "qa_embedding_chunks consistent"
        checks.append(CheckResult(
            check_id="MW06_longqa_child_consistency", section="memory_write",
            status=mw06_status, summary=mw06_summary,
            evidence={
                "child_rows": child_rows,
                "distinct_parents": distinct_parents,
                "parents_missing_parent_row": missing_parents,
                "child_null_embedding": child_null,
                "child_bad_offsets": bad_offsets,
                "child_duplicate_keys": dup_keys,
            },
            duration_ms=0,
        ))

        sections["memory_write"] = {
            "recent_qa": recent_qa,
            "recent_embedding_null": recent_null,
            "embedding_null_total": null_total,
            "empty_answer_total": empty_total,
            "empty_answer_recent": empty_recent,
            "explicit_embedding_null": explicit_null,
            # Consumed by the failure-accounting "since last success" window.
            "last_qa_created_at": last_qa_at,
            "last_embedding_success_at": last_emb_ok_at,
        }

    def _collect_failure_accounting(
        self,
        checks: list[CheckResult],
        sections: dict[str, dict[str, Any]],
        *,
        last_success_at: str | None = None,
    ) -> None:
        reader = FailureReader(self.marker_dir, now=self.now)
        ledger = reader.read()
        records = ledger.get("records", []) or []
        # Strip 'records' from the section dict — it can be large; the
        # section is meant to summarize, not to re-emit per-marker data.
        ledger_summary = {k: v for k, v in ledger.items() if k != "records"}

        # "Since last success" boundary (DESIGN §6 — historical debt vs
        # current incident): a failure only counts as *current* when it
        # happened at/after the most recent successful embedding. With no
        # known success we fall back to the rolling window. Anything older
        # is historical debt and must not move the verdict — that is how
        # 112 old poisoned markers coexist with a healthy verdict.
        window_start = self.now - self.window_hours * 3600
        last_success_epoch = _iso_to_epoch(last_success_at) if last_success_at else None
        effective_since = (
            max(window_start, last_success_epoch)
            if last_success_epoch is not None
            else window_start
        )

        def _fail_epoch(rec: dict[str, Any]) -> float:
            ts = rec.get("last_failure_at") or rec.get("first_failure_at")
            epoch = _iso_to_epoch(ts) if ts else None
            if epoch is not None:
                return epoch
            try:
                return float(rec.get("file_mtime") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        active_broken = (
            STATUS_RETRY_DUE,
            STATUS_STALE_IN_FLIGHT,
            STATUS_STALE_PENDING_DB,
            STATUS_UNRESOLVED,
        )
        current_active = [
            r for r in records
            if r.get("status") in active_broken and _fail_epoch(r) >= effective_since
        ]
        current_retrying = [
            r for r in records
            if r.get("status") == STATUS_RETRYABLE and _fail_epoch(r) >= effective_since
        ]
        current_poisoned = [
            r for r in records
            if r.get("status") == STATUS_POISONED and _fail_epoch(r) >= effective_since
        ]
        current_malformed = [
            r for r in records
            if r.get("status") == STATUS_MALFORMED and _fail_epoch(r) >= effective_since
        ]

        ledger_summary["effective_since"] = _iso_from_epoch(effective_since)
        ledger_summary["since_last_success"] = last_success_epoch is not None
        ledger_summary["current_active"] = len(current_active)
        ledger_summary["current_retrying"] = len(current_retrying)
        ledger_summary["current_poisoned"] = len(current_poisoned)
        sections["failure_accounting"] = ledger_summary

        # FA01 ledger — composition summary; warns only on unreadable files.
        by_status = ledger.get("by_status", {})
        if by_status.get(STATUS_MALFORMED, 0) > 0:
            fa01_status = STATUS_WARN
        else:
            fa01_status = STATUS_OK
        checks.append(CheckResult(
            check_id="FA01_ledger", section="failure_accounting",
            status=fa01_status, summary="failure ledger composition",
            evidence=ledger_summary,
            duration_ms=0,
        ))

        # FA02 current failures — broken / unscheduled tickets since the
        # effective boundary. A ticket that is merely retrying is
        # self-healing and only warns.
        if current_active:
            fa02_status = STATUS_FAIL
            fa02_summary = f"{len(current_active)} current failure marker(s) need attention"
        elif current_retrying:
            fa02_status = STATUS_WARN
            fa02_summary = f"{len(current_retrying)} failure marker(s) retrying"
        else:
            fa02_status = STATUS_OK
            fa02_summary = "no current failure markers"
        checks.append(CheckResult(
            check_id="FA02_recent_failures", section="failure_accounting",
            status=fa02_status, summary=fa02_summary,
            evidence={
                "current_active": len(current_active),
                "current_retrying": len(current_retrying),
                "effective_since": _iso_from_epoch(effective_since),
                "active_by_status": {
                    k: [r.get("job_id") for r in current_active if r.get("status") == k]
                    for k in active_broken
                    if any(r.get("status") == k for r in current_active)
                },
            },
            duration_ms=0,
        ))

        # FA03 poisoned — anything poisoned since the last success is a
        # current incident; the older ones are isolated historical debt
        # (reported, but they never lower the verdict).
        total_poisoned = by_status.get(STATUS_POISONED, 0)
        if current_poisoned:
            fa03_status = STATUS_FAIL
            fa03_summary = f"{len(current_poisoned)} poisoned marker(s) since last success"
        elif total_poisoned > 0:
            fa03_status = STATUS_OK
            fa03_summary = (
                f"{total_poisoned} historical poisoned marker(s) (isolated), 0 current"
            )
        else:
            fa03_status = STATUS_OK
            fa03_summary = "no poisoned markers"
        checks.append(CheckResult(
            check_id="FA03_poisoned", section="failure_accounting",
            status=fa03_status, summary=fa03_summary,
            evidence={
                "total_poisoned": total_poisoned,
                "current_poisoned": len(current_poisoned),
                "by_error_class": ledger.get("by_error_class", {}),
            },
            duration_ms=0,
        ))

        # FA04 malformed — unreadable marker files (any age).
        malformed = by_status.get(STATUS_MALFORMED, 0)
        if malformed > 0:
            fa04_status = STATUS_WARN
            fa04_summary = f"{malformed} malformed marker file(s)"
        else:
            fa04_status = STATUS_OK
            fa04_summary = "no malformed markers"
        checks.append(CheckResult(
            check_id="FA04_malformed", section="failure_accounting",
            status=fa04_status, summary=fa04_summary,
            evidence={"malformed": malformed, "current_malformed": len(current_malformed)},
            duration_ms=0,
        ))

        # FA05 stale — in-flight tickets stuck beyond the stale threshold.
        stale = ledger.get("stale", 0)
        if stale > 0:
            fa05_status = STATUS_WARN
            fa05_summary = f"{stale} stale in_flight / pending_db marker(s)"
        else:
            fa05_status = STATUS_OK
            fa05_summary = "no stale markers"
        checks.append(CheckResult(
            check_id="FA05_stale", section="failure_accounting",
            status=fa05_status, summary=fa05_summary,
            evidence={"stale": stale},
            duration_ms=0,
        ))

    def _collect_derived_memory(
        self,
        checks: list[CheckResult],
        sections: dict[str, dict[str, Any]],
        *,
        conn: Any,
        allow_pg: bool,
    ) -> None:
        observer_path = (self.base_path / "observer_state.json") if self.base_path else None

        # Production-boundary + no opt-in: every DM check is skip.
        if not allow_pg or conn is None:
            for check_id in (
                "DM01_topics", "DM02_observer_cursor",
                "DM03_observation_notes", "DM04_derived_last",
            ):
                checks.append(CheckResult(
                    check_id=check_id, section="derived_memory",
                    status=STATUS_SKIP,
                    summary="production read not authorized; pass --allow-production-read",
                    evidence={"skipped_reason": "production_boundary_no_opt_in"},
                    duration_ms=0,
                ))
            sections["derived_memory"] = {"skipped_reason": "production_boundary_no_opt_in"}
            return

        # DM01 topics.
        topics_total, last_observer_ts = (None, None)
        topics_total, last_observer_ts = _topic_stats(conn)
        if topics_total is None:
            dm01_status = STATUS_UNKNOWN
            dm01_summary = "topics table not available"
            age_s = None
        else:
            age_s = (self.now - _iso_to_epoch(last_observer_ts)) if last_observer_ts else None
            qa_backlog = 0
            if age_s is not None and age_s > 7 * 24 * 3600:
                qa_backlog = _qa_backlog(conn)
            if age_s is not None and age_s > 7 * 24 * 3600 and qa_backlog > 0:
                dm01_status = STATUS_WARN
                dm01_summary = f"topics stale ({int(age_s/3600)}h) with qa backlog {qa_backlog}"
            else:
                dm01_status = STATUS_OK
                dm01_summary = "topics freshness ok"
        checks.append(CheckResult(
            check_id="DM01_topics", section="derived_memory",
            status=dm01_status, summary=dm01_summary,
            evidence={
                "topics_total": topics_total,
                "last_observer_ts": last_observer_ts,
                "age_seconds": age_s,
            },
            duration_ms=0,
        ))

        # DM02 observer_cursor.
        observer_file_exists = observer_path is not None and observer_path.exists()
        observer_parse_failed = False
        last_qa_id = None
        if observer_file_exists:
            try:
                state = json.loads(observer_path.read_text(encoding="utf-8"))
                last_qa_id = state.get("last_qa_id")
            except Exception:
                observer_parse_failed = True

        max_qa_id = _max_qa_id(conn)
        backlog = None
        if max_qa_id is not None and isinstance(last_qa_id, int):
            backlog = max(0, max_qa_id - last_qa_id)
        updated_at = (
            observer_path.stat().st_mtime if observer_file_exists else None
        )
        age_obs = (self.now - updated_at) if updated_at else None
        if observer_parse_failed:
            dm02_status = STATUS_UNKNOWN
            dm02_summary = "observer state unreadable (corrupt)"
        elif not observer_file_exists:
            # Fresh install: the observer has not run yet — not applicable.
            dm02_status = STATUS_SKIP
            dm02_summary = "observer state not created yet"
        elif max_qa_id is None:
            dm02_status = STATUS_SKIP
            dm02_summary = "no qa rows yet — observer cursor not applicable"
        elif last_qa_id is None:
            dm02_status = STATUS_UNKNOWN
            dm02_summary = "observer state has no cursor"
        else:
            age_days = (age_obs / 86400.0) if age_obs is not None else None
            if (backlog is not None and backlog > 100) or (
                age_days is not None and age_days > 7 and backlog and backlog > 0
            ):
                dm02_status = STATUS_WARN
                dm02_summary = "observer cursor stale"
            else:
                dm02_status = STATUS_OK
                dm02_summary = "observer cursor fresh"
        checks.append(CheckResult(
            check_id="DM02_observer_cursor", section="derived_memory",
            status=dm02_status, summary=dm02_summary,
            evidence={
                "last_qa_id": last_qa_id,
                "qa_head_id": max_qa_id,
                "backlog": backlog,
                "updated_at": _iso_from_epoch(updated_at) if updated_at else None,
                "age_seconds": age_obs,
            },
            duration_ms=0,
        ))

        # DM03 observation_notes — by freshness.
        max_obs = _max_observation_note(conn)
        if max_obs is None:
            # No observations yet (fresh / legacy install) — not applicable.
            dm03_status = STATUS_SKIP
            dm03_summary = "no observations yet"
            dm03_age = None
        else:
            dm03_age = (self.now - _iso_to_epoch(max_obs)) if max_obs else None
            if dm03_age is not None and dm03_age > 7 * 24 * 3600:
                dm03_status = STATUS_WARN
                dm03_summary = "observation_notes stale"
            else:
                dm03_status = STATUS_OK
                dm03_summary = "observation_notes fresh"
        checks.append(CheckResult(
            check_id="DM03_observation_notes", section="derived_memory",
            status=dm03_status, summary=dm03_summary,
            evidence={"last_created_at": max_obs, "age_seconds": dm03_age},
            duration_ms=0,
        ))

        # DM04 derived_last — yin_paragraphs + topics freshness.
        max_yin = _max_yin_paragraph(conn)
        max_topic_obs = last_observer_ts
        ages = []
        for ts in (max_yin, max_topic_obs):
            if ts:
                ages.append(self.now - _iso_to_epoch(ts))
        dm04_age = max(ages) if ages else None
        if dm04_age is None:
            # No derived rows yet — not applicable rather than unknown.
            dm04_status = STATUS_SKIP
            dm04_summary = "no derived rows"
        elif dm04_age > 7 * 24 * 3600:
            dm04_status = STATUS_WARN
            dm04_summary = "derived layer stale"
        else:
            dm04_status = STATUS_OK
            dm04_summary = "derived layer fresh"
        checks.append(CheckResult(
            check_id="DM04_derived_last", section="derived_memory",
            status=dm04_status, summary=dm04_summary,
            evidence={
                "last_yin_paragraph_at": max_yin,
                "last_topic_observer_at": max_topic_obs,
                "age_seconds": dm04_age,
            },
            duration_ms=0,
        ))

        sections["derived_memory"] = {
            "topics_total": topics_total,
            "observer_cursor_present": last_qa_id is not None,
            "backlog": backlog,
        }

    def _collect_providers(
        self,
        checks: list[CheckResult],
        sections: dict[str, dict[str, Any]],
    ) -> None:
        # PR01 — embed / rerank / llm configured.
        cfg: dict[str, Any] = {}
        try:
            cfg = self.config_loader() or {}
        except Exception as exc:
            cfg = {"_load_error": sanitize_text(str(exc))}
        embed_cfg = cfg.get("embed", {}) if isinstance(cfg, dict) else {}
        rerank_cfg = cfg.get("rerank", {}) if isinstance(cfg, dict) else {}
        llm_cfg = cfg.get("llm", {}) if isinstance(cfg, dict) else {}
        embed_ok = bool(embed_cfg.get("endpoint") or embed_cfg.get("api_key") or embed_cfg.get("model"))
        rerank_ok = bool(rerank_cfg.get("endpoint") or rerank_cfg.get("api_key") or rerank_cfg.get("model"))
        llm_ok = bool(llm_cfg.get("endpoint") or llm_cfg.get("api_key") or llm_cfg.get("model"))

        if not embed_ok:
            pr01_status = STATUS_FAIL
            pr01_summary = "embedding provider not configured"
        elif not (rerank_ok and llm_ok):
            pr01_status = STATUS_WARN
            pr01_summary = "rerank/llm not configured"
        else:
            pr01_status = STATUS_OK
            pr01_summary = "all providers configured"
        checks.append(CheckResult(
            check_id="PR01_configured", section="providers",
            status=pr01_status, summary=pr01_summary,
            evidence={
                "embed_configured": embed_ok,
                "rerank_configured": rerank_ok,
                "llm_configured": llm_ok,
            },
            duration_ms=0,
        ))

        # PR02 — recent provider errors from FA ledger.
        reader = FailureReader(self.marker_dir, now=self.now)
        ledger = reader.read()
        recent_by_class = ledger.get("recent_by_error_class", {})
        cred_classes = {PROVIDER_ERR_401, PROVIDER_ERR_402}
        transient_classes = {PROVIDER_ERR_429, PROVIDER_ERR_5XX, PROVIDER_ERR_TIMEOUT, PROVIDER_ERR_CONNECTION}
        cred_count = sum(int(recent_by_class.get(k, 0)) for k in cred_classes)
        transient_count = sum(int(recent_by_class.get(k, 0)) for k in transient_classes)
        if cred_count > 0:
            pr02_status = STATUS_FAIL
            pr02_summary = f"{cred_count} recent provider 401/402"
        elif transient_count > 0:
            pr02_status = STATUS_WARN
            pr02_summary = f"{transient_count} recent provider transient errors"
        else:
            pr02_status = STATUS_OK
            pr02_summary = "no recent provider errors"
        checks.append(CheckResult(
            check_id="PR02_failure_ledger", section="providers",
            status=pr02_status, summary=pr02_summary,
            evidence={"recent_provider_errors": recent_by_class},
            duration_ms=0,
        ))

        # PR03 — deep auth probes (only when --deep and runner provided).
        if self.deep and self.deep_auth_runner is not None:
            try:
                res = self.deep_auth_runner(cfg, timeout=AUTH_TIMEOUT_SECONDS)
            except Exception as exc:
                res = {"embedding": {"status": STATUS_FAIL, "summary": sanitize_text(str(exc))},
                       "rerank": {"status": STATUS_FAIL, "summary": sanitize_text(str(exc))},
                       "llm": {"status": STATUS_FAIL, "summary": sanitize_text(str(exc))}}
            statuses = [v.get("status") for v in res.values() if isinstance(v, dict)]
            if STATUS_FAIL in statuses:
                pr03_status = STATUS_FAIL
            elif STATUS_WARN in statuses:
                pr03_status = STATUS_WARN
            elif STATUS_SKIP in statuses:
                pr03_status = STATUS_SKIP
            else:
                pr03_status = STATUS_OK
            pr03_summary = "deep auth probes complete"
            checks.append(CheckResult(
                check_id="PR03_deep_auth", section="providers",
                status=pr03_status, summary=pr03_summary,
                evidence=res,
                duration_ms=0,
            ))
        else:
            checks.append(CheckResult(
                check_id="PR03_deep_auth", section="providers",
                status=STATUS_SKIP,
                summary="deep auth probes not requested",
                evidence={"skipped_reason": "deep_not_enabled"},
                duration_ms=0,
            ))

        sections["providers"] = {
            "configured": {"embed": embed_ok, "rerank": rerank_ok, "llm": llm_ok},
            "recent_provider_errors": recent_by_class,
        }

    def _build_metrics(
        self,
        checks: list[CheckResult],
        sections: dict[str, dict[str, Any]],
        *,
        conn: Any,
        allow_pg: bool,
    ) -> dict[str, Any]:
        m: dict[str, Any] = {
            "qa_pairs_total": None,
            "conversation_stream_total": None,
            "topics_total": None,
            "topic_entries_total": None,
            "observation_notes_total": None,
            "yin_paragraphs_total": None,
            "collection_seconds": 0.0,
            "check_count": len(checks),
        }
        if allow_pg and conn is not None:
            try:
                m["qa_pairs_total"] = _count_table(conn, "qa_pairs")
                m["conversation_stream_total"] = _count_table(conn, "conversation_stream")
                m["topics_total"] = _count_table(conn, "topics")
                m["topic_entries_total"] = _count_table(conn, "topic_entries")
                m["observation_notes_total"] = _count_table(conn, "observation_notes")
                m["yin_paragraphs_total"] = _count_table(conn, "yin_paragraphs")
            except Exception:
                # Count failures should never poison metrics; leave as None.
                pass
        # collection_seconds is attached on the report post-collect, but
        # we mirror a best-effort value here from the checks we've built
        # so the metric is non-None even when callers inspect early.
        m["collection_seconds"] = sum(c.duration_ms for c in checks) / 1000.0
        return m


# ── SQL helpers (every one is a SELECT) ──


def _scalar_query(conn: Any, sql: str, *params: Any) -> Any:
    """Run a SELECT that returns a single scalar value. Returns None on
    any error — callers translate None into the appropriate status."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql, params or None)
            row = cur.fetchone()
            if row is None:
                return None
            return row[0]
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        return None


def _has_schema_version_row(conn: Any, version: str) -> bool:
    """Returns True iff ``SELECT 1 FROM schema_versions WHERE version = %s``
    returns a row. Tolerates missing tables."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT 1 FROM public.schema_versions WHERE version = %s",
                (version,),
            )
            return cur.fetchone() is not None
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        return False


def _missing_tables(conn: Any, names: tuple[str, ...]) -> list[str]:
    """Detect which of ``names`` are absent in public. SELECT against
    information_schema.tables — no DDL."""
    missing: list[str] = []
    for n in names:
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = %s",
                    (n,),
                )
                present = cur.fetchone() is not None
            finally:
                close = getattr(cur, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        except Exception:
            present = False
        if not present:
            missing.append(n)
    return missing


def _missing_indexes(conn: Any, names: tuple[str, ...]) -> list[str]:
    missing: list[str] = []
    for n in names:
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT 1 FROM pg_indexes "
                    "WHERE schemaname = 'public' AND indexname = %s",
                    (n,),
                )
                present = cur.fetchone() is not None
            finally:
                close = getattr(cur, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        except Exception:
            present = False
        if not present:
            missing.append(n)
    return missing


def _recent_qa_window(conn: Any, window_hours: int) -> tuple[int, int, int, int]:
    """Return ``(recent_qa, recent_embedding_ok, recent_embedding_null, recent_empty)``
    over the last ``window_hours``."""
    sql = (
        "SELECT "
        "  COUNT(*) AS total, "
        "  COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS ok, "
        "  COUNT(*) FILTER (WHERE embedding IS NULL) AS null_emb, "
        "  COUNT(*) FILTER (WHERE answer IS NULL OR BTRIM(answer) = '') AS empty_ans "
        "FROM public.qa_pairs "
        "WHERE created_at >= (NOW() - (%s || ' hours')::interval)"
    )
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql, (int(window_hours),))
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        return (0, 0, 0, 0)
    if not row:
        return (0, 0, 0, 0)
    total, ok, null_emb, empty_ans = row
    return (int(total or 0), int(ok or 0), int(null_emb or 0), int(empty_ans or 0))


def _embedding_null_debt(conn: Any) -> tuple[int, str | None, str | None]:
    """Return ``(null_total, oldest_null_created_at, newest_null_created_at)``."""
    sql = (
        "SELECT COUNT(*), MIN(created_at), MAX(created_at) "
        "FROM public.qa_pairs WHERE embedding IS NULL"
    )
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        return (0, None, None)
    if not row:
        return (0, None, None)
    total, oldest, newest = row
    return (int(total or 0),
            _iso_str_or_none(oldest),
            _iso_str_or_none(newest))


def _empty_answer_counts(conn: Any, window_hours: int) -> tuple[int, int]:
    sql_total = (
        "SELECT COUNT(*) FROM public.qa_pairs "
        "WHERE answer IS NULL OR BTRIM(answer) = ''"
    )
    sql_recent = (
        "SELECT COUNT(*) FROM public.qa_pairs "
        "WHERE (answer IS NULL OR BTRIM(answer) = '') "
        "  AND created_at >= (NOW() - (%s || ' hours')::interval)"
    )
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql_total)
            total_row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        total = 0
    else:
        total = int((total_row or (0,))[0] or 0)
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql_recent, (int(window_hours),))
            recent_row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        recent = 0
    else:
        recent = int((recent_row or (0,))[0] or 0)
    return (total, recent)


def _last_writes(conn: Any) -> tuple[str | None, str | None]:
    sql_qa = "SELECT MAX(created_at) FROM public.qa_pairs"
    sql_emb = (
        "SELECT MAX(created_at) FROM public.qa_pairs "
        "WHERE embedding IS NOT NULL"
    )
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql_qa)
            row_qa = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        last_qa = None
    else:
        last_qa = _iso_str_or_none((row_qa or (None,))[0])
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql_emb)
            row_emb = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        last_emb = None
    else:
        last_emb = _iso_str_or_none((row_emb or (None,))[0])
    return (last_qa, last_emb)


def _explicit_memory_counts(conn: Any) -> tuple[int, int, int]:
    sql = (
        "SELECT COUNT(*), "
        "  COUNT(*) FILTER (WHERE embedding IS NULL), "
        "  COUNT(*) FILTER (WHERE embedding IS NOT NULL) "
        "FROM public.explicit_memories"
    )
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        return (0, 0, 0)
    if not row:
        return (0, 0, 0)
    total, null_, set_ = row
    return (int(total or 0), int(null_ or 0), int(set_ or 0))


def _longqa_child_stats(conn: Any) -> tuple[int, int, int, int, int, int]:
    """Best-effort stats for qa_embedding_chunks. Returns
    ``(child_rows, distinct_parents, parents_missing_parent_row,
    child_null_embedding, child_bad_offsets, child_duplicate_keys)``.

    Schema tolerance: if the table doesn't exist, every count is 0.
    """
    out = [0, 0, 0, 0, 0, 0]
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*), "
                "  COUNT(DISTINCT qa_id), "
                "  COUNT(*) FILTER (WHERE embedding IS NULL) "
                "FROM public.qa_embedding_chunks"
            )
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        return tuple(out)
    if not row:
        return tuple(out)
    child_rows, distinct_parents, child_null = row
    out[0] = int(child_rows or 0)
    out[1] = int(distinct_parents or 0)
    out[3] = int(child_null or 0)

    # Parents missing parent row — requires both tables; skip if absent.
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) FROM public.qa_embedding_chunks c "
                "LEFT JOIN public.qa_pairs p ON p.id = c.qa_id "
                "WHERE p.id IS NULL"
            )
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        out[2] = 0
    else:
        out[2] = int((row or (0,))[0] or 0)

    # Bad offsets: negative or non-monotonic per qa_id. Cheap approximation:
    # any row with start_offset < 0 OR end_offset <= start_offset.
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) FROM public.qa_embedding_chunks "
                "WHERE start_offset < 0 OR end_offset <= start_offset"
            )
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        out[4] = 0
    else:
        out[4] = int((row or (0,))[0] or 0)

    # Duplicate keys: same (qa_id, chunk_index) pair in >1 row.
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT qa_id, chunk_index FROM public.qa_embedding_chunks "
                "  GROUP BY qa_id, chunk_index HAVING COUNT(*) > 1"
                ") d"
            )
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        out[5] = 0
    else:
        out[5] = int((row or (0,))[0] or 0)

    return tuple(out)


def _topic_stats(conn: Any) -> tuple[int | None, str | None]:
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT COUNT(*), MAX(last_observer_ts) FROM public.topics")
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        return (None, None)
    if not row:
        return (None, None)
    total, last_ts = row
    return (int(total or 0), _iso_str_or_none(last_ts))


def _qa_backlog(conn: Any) -> int:
    """Approximate qa backlog: rows newer than the topics observer's last
    touch point. Caller only invokes when topics is fresh-stale."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) FROM public.qa_pairs q "
                "WHERE q.created_at > COALESCE("
                "  (SELECT MAX(last_observer_ts) FROM public.topics),"
                "  '1970-01-01'::timestamptz)"
            )
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        return 0
    return int((row or (0,))[0] or 0)


def _max_qa_id(conn: Any) -> int | None:
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT MAX(id) FROM public.qa_pairs")
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        return None
    if not row:
        return None
    val = row[0]
    return int(val) if val is not None else None


def _max_observation_note(conn: Any) -> str | None:
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT MAX(created_at) FROM public.observation_notes")
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        return None
    return _iso_str_or_none((row or (None,))[0])


def _max_yin_paragraph(conn: Any) -> str | None:
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT MAX(created_at) FROM public.yin_paragraphs")
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        return None
    return _iso_str_or_none((row or (None,))[0])


def _count_table(conn: Any, name: str) -> int:
    try:
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM public.{name}")
            row = cur.fetchone()
        finally:
            close = getattr(cur, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    except Exception:
        return 0
    return int((row or (0,))[0] or 0)


# ── module file / ISO helpers ──


def _default_module_file() -> str:
    try:
        import v3core
        return str(v3core.__file__ or "")
    except Exception:
        return ""


def _classify_module_file(path: str) -> str:
    """Classify the v3core __file__ path per DESIGN §4 RT01."""
    if not path:
        return "unknown"
    p = path.replace("\\", "/")
    # editable installs land under v3-memory-plugin/src/v3core or similar;
    # detect either substring.
    if "v3-memory-plugin" in p:
        return "editable"
    if "site-packages" in p:
        return "site_packages"
    return "unknown"


def _iso_from_epoch(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _iso_to_epoch(s: str | None) -> float | None:
    if not isinstance(s, str):
        return None
    ss = s.strip()
    if ss.endswith("Z"):
        ss = ss[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ss).timestamp()
    except Exception:
        return None


def _iso_str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, datetime):
        return v.isoformat()
    try:
        return str(v)
    except Exception:
        return None


__all__ = [
    "HealthService",
    "REPORT_SCHEMA_VERSION",
    "HEALTH_WINDOW_HOURS_DEFAULT",
    "STALE_MARKER_HOURS",
    "AUTH_TIMEOUT_SECONDS",
    "CANONICAL_TABLES",
    "CANONICAL_INDEXES",
    "EXPECTED_SCHEMA_VERSION",
]