# -*- coding: utf-8 -*-
"""Guard contract for the M01/M02 integration driver (offline; never opens PG).

What this file covers
====================

The M01/M02 driver is a fail-closed, destructive harness: it creates three real
databases, runs real DDL, starts real ``v3core serve`` processes and writes rows
on the dedicated Y400 test laptop. Its guards are the only thing standing
between a mistyped ``--db-name`` and production, so they are tested HERE — with
fakes only, on any machine, with no database and no subprocess.

Covered:

* every refusal gate (hostname / PG host / PG port / db prefix / production
  database / identifier injection / non-empty run root), including the
  case-variant and whitespace traps that a ``.strip()`` would let through;
* :func:`assert_disposable_target` — the guard that every DDL/DML statement goes
  through, including the ``DROP`` the driver issues on its own disposable
  database;
* the DSN builder can never carry a password (argv / log safety);
* the public HTTP request builders emit exactly the bodies the real
  ``v3core.serve`` routes parse, and refuse empty identity;
* :func:`schema_copy_report` — the single-source schema contract, checked
  generically so a NEW artifact added by M01 is compared automatically;
* the receipt contract (design §13) rejects a bare ``success: true`` answer and
  accepts a full one;
* :func:`pick` / :func:`find_values` / :func:`scrub` / :func:`truncate` — the
  payload-navigation and secret-scrubbing helpers, including a real secret;
* the embedded derived probe compiles (it is shipped inside the driver as a
  string, so a syntax error there would only surface on the Y400 run);
* the derived probe carries no provider endpoint, key or model name;
* :func:`sanitize_env` — ambient model/embed credentials, endpoints and
  production-bypass switches are dropped rather than inherited;
* :func:`artifact_provenance_mismatches` / the installed-artifact fingerprint
  gate — a stale wheel inside the named venv is refused BEFORE any DB connect;
* :func:`timestamps_equal` — the receipt's ``recorded_at`` is compared to the
  independently-read DB value as an instant, not as a raw string;
* the canonical reader helpers (:func:`reader_current_absent`,
  :func:`reader_history_ids`, :func:`reader_requested`,
  :func:`reader_relation_chain`) used by the dedicated withdraw scenario;
* the nested mode-selected read closure: a current read answers with the
  ``memory`` row and a history read with the ``requested`` row plus the
  ``history.versions`` / ``history.edges`` chain — never a sibling borrowed by
  recursive key search, and never an empty / last-edge-only chain;
* the fresh-session recall classification (``embeddings_unavailable`` vs a real
  ``recall_missed_current_version``) and the verified server cleanup;
* the restored withdraw contract is actually exercised: a dedicated A'->B'->C'
  chain, exact retry reporting B archived / C current, withdraw with no
  replacement fields, idempotent retry, conflicting-replace refusal, no-current
  reads, full ordered history, and a fresh-process search that excludes the
  withdrawn target.

Not covered, deliberately: the behavioural checks. Those need the disposable
PostgreSQL and the real serve process — they are the driver's job, not this
file's. No test here connects to any database, and the v3-core pytest domain
hard-blocks ``psycopg2.connect`` anyway.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Locate the driver: this file lives at <repo>/src/v3-core/tests/, the driver at
# <repo>/evidence/m01-m02-memory-correction-20261003/. Walk up from the file and
# accept the first hit, so the test keeps working if the tree is relaid out.
_HERE = Path(__file__).resolve()
_EVIDENCE_REL = Path("evidence") / "m01-m02-memory-correction-20261003" \
    / "m01_m02_integration.py"
CANDIDATES = [_HERE.parents[i] / _EVIDENCE_REL for i in range(1, 5)]
# also try the documented Y400 integration root, for a standalone checkout
CANDIDATES.append(Path("C:/hp-testbed") / _EVIDENCE_REL)


def _load_driver():
    for path in CANDIDATES:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("m01_m02_integration_driver", path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            module.__DRIVER_PATH__ = path
            return module
    raise FileNotFoundError(
        "m01_m02_integration.py not found; looked in "
        + ", ".join(str(p) for p in CANDIDATES)
    )


D = _load_driver()


def _function_source(source: str, name: str) -> str:
    """Exact source of the (possibly nested) function ``name``.

    A line-index slice from ``def name(`` to the next ``    def `` is fragile:
    any sibling closure added later in ``main()`` widens the slice and makes a
    "this function emits no credential" assertion fail for the wrong reason.
    Extracting the real AST node keeps the assertion pinned to the function.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"function {name!r} not found in the driver")


# ══════════════════════════════════════════════════════════════════════════
# gates
# ══════════════════════════════════════════════════════════════════════════

class TestHostnameGate:
    def test_the_y400_test_laptop_is_admitted(self):
        assert D.gate_hostname("DESKTOP-EQP3OBU") == ""
        assert D.gate_hostname("desktop-eqp3obu") == ""  # case-insensitive

    @pytest.mark.parametrize("hostname", [
        "",                      # unknown host is never a pass
        "DESKTOP-MU2RQIU",      # the production machine
        "WORKSTATION-1",
        "DESKTOP-EQP3OB",       # one character short
        "DESKTOP-EQP3OBU-2",    # a longer name that merely contains the gate
    ])
    def test_every_other_host_is_refused(self, hostname):
        reason = D.gate_hostname(hostname)
        assert reason, f"{hostname!r} was admitted"
        assert D.HOST_GATE in reason or "determined" in reason


class TestPgTargetGate:
    def test_the_disposable_sandbox_is_admitted(self):
        assert D.gate_pg_target("127.0.0.1", 55432, "m01m02e2e_20261003a") == ""

    @pytest.mark.parametrize("host", [
        "localhost",     # resolves to the same box but is not the allowlisted literal
        "0.0.0.0",
        "192.168.1.10",
        "",
    ])
    def test_any_other_host_is_refused(self, host):
        assert D.gate_pg_target(host, 55432, "m01m02e2e_x")

    def test_production_port_is_refused_even_with_the_disposable_prefix(self):
        assert "production" in D.gate_pg_target("127.0.0.1", 5433, "m01m02e2e_x")

    @pytest.mark.parametrize("port", [5432, 5433, 5434, 54333, 0, -1])
    def test_every_other_port_is_refused(self, port):
        assert D.gate_pg_target("127.0.0.1", port, "m01m02e2e_x")

    def test_a_non_integer_port_is_refused_rather_than_crashing(self):
        assert D.gate_pg_target("127.0.0.1", "not-a-port", "m01m02e2e_x")

    @pytest.mark.parametrize("db", [
        "v3core", "v3embeddings", "postgres", "m01m02e2e",   # right prefix, wrong db
        "I01E2E_x",                                           # wrong prefix case
        "m01m02e2e_x; DROP DATABASE postgres",
        'm01m02e2e_x" --',
        "m01m02e2e_x\nDROP",
        "m01m02e2e_../other",
    ])
    def test_only_the_prefixed_disposable_namespace_is_admitted(self, db):
        assert D.gate_pg_target("127.0.0.1", 55432, db), f"{db!r} was admitted"


class TestRunRootGate:
    def test_a_fresh_empty_directory_is_admitted(self, tmp_path):
        assert D.gate_root(tmp_path, must_exist=True) == ""

    def test_a_missing_directory_is_refused(self, tmp_path):
        assert D.gate_root(tmp_path / "nope", must_exist=True)

    def test_a_non_empty_directory_is_refused(self, tmp_path):
        (tmp_path / "leftover.json").write_text("{}", encoding="utf-8")
        reason = D.gate_root(tmp_path, must_exist=True)
        assert "not empty" in reason
        assert "leftover.json" in reason   # the reason names what was found

    def test_a_file_is_not_a_root(self, tmp_path):
        target = tmp_path / "root-is-a-file"
        target.write_text("x", encoding="utf-8")
        assert D.gate_root(target, must_exist=True)


# ══════════════════════════════════════════════════════════════════════════
# run-root / evidence confinement (a production-home symlink must not do)
# ══════════════════════════════════════════════════════════════════════════

class TestLexicalContainment:
    def test_a_strict_descendant_is_inside(self, tmp_path):
        parent = tmp_path / "parent"
        assert D._is_lexically_inside(parent / "run", parent) is True
        assert D._is_lexically_inside(parent / "run" / "a" / "b", parent) is True

    def test_the_parent_itself_is_not_inside_itself(self, tmp_path):
        assert D._is_lexically_inside(tmp_path, tmp_path) is False

    def test_a_sibling_with_a_shared_string_prefix_is_refused(self, tmp_path):
        # ".../20261003-evil" merely starts with the string of ".../20261003".
        parent = tmp_path / "m01-m02-integration-20261003"
        assert D._is_lexically_inside(tmp_path / "m01-m02-integration-20261003-evil",
                                     parent) is False

    def test_an_unrelated_tree_is_refused(self, tmp_path):
        assert D._is_lexically_inside(tmp_path / "other", tmp_path / "parent") is False


class TestRunRootLocationGate:
    def test_a_root_under_the_mandated_parent_is_admitted(self, tmp_path, monkeypatch):
        parent = tmp_path / "m01-m02-integration-20261003"
        run = parent / "run-1"
        run.mkdir(parents=True)
        monkeypatch.setattr(D, "RUN_ROOT_PARENT", parent)
        assert D.gate_run_root_location(run, run / "out.json") == ""

    def test_a_root_outside_the_mandated_parent_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "RUN_ROOT_PARENT", tmp_path / "m01-m02-integration-20261003")
        outsider = tmp_path / "somewhere-else"
        outsider.mkdir()
        reason = D.gate_run_root_location(outsider, outsider / "out.json")
        assert reason, "a run root outside the mandated parent was admitted"
        assert "lexically inside" in reason

    def test_a_production_home_style_path_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setattr(D, "RUN_ROOT_PARENT", tmp_path / "m01-m02-integration-20261003")
        prod_home = tmp_path / "Users" / "prod" / "m01m02e2e-run"
        reason = D.gate_run_root_location(prod_home, prod_home / "out.json")
        assert reason, "a production-home run root was admitted"

    def test_evidence_outside_the_run_root_is_refused(self, tmp_path, monkeypatch):
        parent = tmp_path / "m01-m02-integration-20261003"
        run = parent / "run-1"
        run.mkdir(parents=True)
        monkeypatch.setattr(D, "RUN_ROOT_PARENT", parent)
        outside = parent / "elsewhere.json"
        reason = D.gate_run_root_location(run, outside)
        assert reason, "evidence outside the run root was admitted"
        assert "inside the run root" in reason

    def test_a_symlinked_run_root_that_escapes_is_refused(self, tmp_path, monkeypatch):
        if not hasattr(os, "symlink"):
            pytest.skip("symlinks unavailable on this platform")
        parent = tmp_path / "m01-m02-integration-20261003"
        parent.mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        link = parent / "linked-run"
        try:
            link.symlink_to(elsewhere, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation is not permitted here")
        monkeypatch.setattr(D, "RUN_ROOT_PARENT", parent)
        assert D.gate_run_root_location(link, link / "out.json"), \
            "a link routing out of the run root parent was admitted"


class TestJunctionContainment:
    """A directory junction/symlink must not defeat run-root confinement.

    A junction is a real reparse point, not a string prefix: its lexical parts
    look contained while the bytes actually live elsewhere. Containment
    therefore requires BOTH the lexical AND the resolved path to be a strict
    descendant. On Windows the link is created with ``cmd.exe mklink /J`` (no
    admin required) and MUST NOT be skipped; other platforms use a symlink.
    """

    @staticmethod
    def _link(link, target) -> bool:
        """Create a Windows directory junction (True) or a symlink (False)."""
        if os.name == "nt":
            rc = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
            assert rc == 0, (
                f"cmd.exe mklink /J failed (rc={rc!r}); Windows junction "
                "coverage must not be skipped")
            return True
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation is not permitted here")
        return False

    @staticmethod
    def _drop(link, is_junction: bool) -> None:
        if is_junction:
            os.rmdir(link)          # removes the reparse point, keeps the target
        else:
            link.unlink()

    def test_a_windows_directory_junction_that_escapes_is_refused(self, tmp_path, monkeypatch):
        parent = tmp_path / "m01-m02-integration-20261003"
        parent.mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        link = parent / "junction-run"
        is_junction = self._link(link, elsewhere)
        monkeypatch.setattr(D, "RUN_ROOT_PARENT", parent)
        try:
            reason = D.gate_run_root_location(link, link / "out.json")
            assert reason, (
                "a directory junction routing out of the run root parent was "
                f"admitted; its lexical parts look contained but the bytes "
                f"live in {elsewhere}")
        finally:
            self._drop(link, is_junction)

    def test_an_output_route_through_a_junction_is_refused(self, tmp_path, monkeypatch):
        parent = tmp_path / "m01-m02-integration-20261003"
        parent.mkdir(parents=True)
        run = parent / "run-1"
        run.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        out_junction = run / "linked-out"
        is_junction = self._link(out_junction, elsewhere)
        monkeypatch.setattr(D, "RUN_ROOT_PARENT", parent)
        try:
            reason = D.gate_run_root_location(run, out_junction / "out.json")
            assert reason, (
                "an --out path routed through a junction out of the run root "
                "was admitted")
        finally:
            self._drop(out_junction, is_junction)

    def test_a_junction_pointing_into_the_run_root_from_outside_is_refused(self, tmp_path, monkeypatch):
        parent = tmp_path / "m01-m02-integration-20261003"
        parent.mkdir(parents=True)
        run = parent / "run-1"
        run.mkdir()
        prod_home = tmp_path / "prod-home"
        prod_home.mkdir()
        link = prod_home / "looks-inside"
        is_junction = self._link(link, run)
        monkeypatch.setattr(D, "RUN_ROOT_PARENT", parent)
        try:
            reason = D.gate_run_root_location(link, link / "out.json")
            assert reason, (
                "a link under a production home that merely RESOLVES into the "
                "run root was admitted; the lexical route is not confined")
        finally:
            self._drop(link, is_junction)


class TestEnvArtifactGate:
    def test_a_file_inside_the_source_root_is_admitted(self, tmp_path):
        source = tmp_path / "src"
        (source / "v3core").mkdir(parents=True)
        init = source / "v3core" / "__init__.py"
        init.write_text("", encoding="utf-8")
        venv = tmp_path / "venv"
        (venv / "Scripts").mkdir(parents=True)
        python = venv / "Scripts" / "python.exe"
        python.write_text("", encoding="utf-8")
        assert D.gate_env_artifact(str(init), source, str(python)) == ""

    def test_a_file_in_the_named_venv_site_packages_is_admitted(self, tmp_path):
        source = tmp_path / "src"          # exists, but holds no v3core
        source.mkdir()
        sp = tmp_path / "venv" / "Lib" / "site-packages" / "v3core"
        sp.mkdir(parents=True)
        init = sp / "__init__.py"
        init.write_text("", encoding="utf-8")
        (tmp_path / "venv" / "Scripts").mkdir(parents=True)
        python = tmp_path / "venv" / "Scripts" / "python.exe"
        python.write_text("", encoding="utf-8")
        assert D.gate_env_artifact(str(init), source, str(python)) == ""

    def test_a_site_packages_in_ANOTHER_venv_is_refused(self, tmp_path):
        # This is the whole point: a generic "site-packages" substring test
        # would admit any venv on the machine, including a stale wheel.
        source = tmp_path / "src"
        source.mkdir()
        stale = tmp_path / "stale-venv" / "Lib" / "site-packages" / "v3core"
        stale.mkdir(parents=True)
        init = stale / "__init__.py"
        init.write_text("", encoding="utf-8")
        named = tmp_path / "fresh-venv"
        (named / "Scripts").mkdir(parents=True)
        python = named / "Scripts" / "python.exe"
        python.write_text("", encoding="utf-8")
        reason = D.gate_env_artifact(str(init), source, str(python))
        assert reason, "a venv the caller never named was admitted"
        assert "neither inside" in reason

    @pytest.mark.parametrize("resolved", [
        "",                                  # nothing reported at all
        "C:/definitely/not/here/v3core/__init__.py",   # reported but absent
    ])
    def test_an_unusable_resolution_is_refused(self, resolved, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        python = tmp_path / "venv" / "Scripts" / "python.exe"
        assert D.gate_env_artifact(resolved, source, str(python))


class TestDisposableTargetGuard:
    """Every destructive statement the driver issues goes through this guard."""

    def test_the_disposable_database_is_admitted(self):
        D.assert_disposable_target("m01m02e2e_20261003a")  # must not raise

    @pytest.mark.parametrize("db", ["v3embeddings", "postgres", "v3core", "M01M02E2E_x"])
    def test_a_non_disposable_database_raises_before_any_ddl(self, db):
        with pytest.raises(D.Refused) as excinfo:
            D.assert_disposable_target(db)
        assert "destructive" in str(excinfo.value)
        assert excinfo.value.code == 97

    def test_the_guard_is_independent_of_the_caller_supplied_host(self):
        # A caller cannot widen the guard by passing a different port: the guard
        # always evaluates the allowlisted sandbox port.
        D.assert_disposable_target("m01m02e2e_ok")
        with pytest.raises(D.Refused):
            D.assert_disposable_target("m01m02e2e_x/../../etc")


# ══════════════════════════════════════════════════════════════════════════
# credential / argv safety
# ══════════════════════════════════════════════════════════════════════════

class TestDsnSafety:
    def test_the_dsn_never_carries_a_password(self):
        dsn = D.dsn_without_password("127.0.0.1", 55432, "m01m02e2e_x", "f2e2e")
        assert dsn == "postgresql://f2e2e@127.0.0.1:55432/m01m02e2e_x"
        # Inspect the userinfo section only: the "postgresql://" scheme itself
        # contains a colon, which is not a credential separator.
        userinfo = dsn.split("//", 1)[1].split("@", 1)[0]
        assert ":" not in userinfo, f"userinfo {userinfo!r} looks like user:password"

    def test_the_dsn_has_no_password_segment_after_the_at_sign(self):
        dsn = D.dsn_without_password("127.0.0.1", 55432, "m01m02e2e_x", "f2e2e")
        after_at = dsn.split("@", 1)[1]
        assert "@" not in after_at, "more than one credential separator"
        assert after_at.startswith("127.0.0.1:55432/")

    def test_scrub_removes_a_real_secret_everywhere_it_appears(self):
        secret = "sup3r-s3cret-pg-pw"
        text = (
            f'{{"dsn": "postgresql://f2e2e:{secret}@127.0.0.1:55432/db", '
            f'"env": "PGPASSWORD={secret}"}}'
        )
        cleaned = D.scrub(text, secret)
        assert secret not in cleaned
        assert cleaned.count("***REDACTED***") == 2

    def test_scrub_is_a_noop_without_a_secret(self):
        assert D.scrub("nothing to hide", "") == "nothing to hide"

    def test_truncate_reports_that_it_cut(self):
        text, cut = D.truncate("x" * 100, limit=10)
        assert cut is True and len(text) < 100 and "truncated 90 chars" in text
        short, cut2 = D.truncate("short", limit=10)
        assert cut2 is False and short == "short"
        assert D.truncate(None)[0] == ""


# ══════════════════════════════════════════════════════════════════════════
# public HTTP request builders — must match the real serve routes
# ══════════════════════════════════════════════════════════════════════════

class TestPublicRequestBuilders:
    def test_tool_request_matches_serve__handle_tool(self):
        # serve._handle_tool reads body["name"] and body["args"].
        body = D.build_tool_request("v3_update", {"action": "correct", "memory_id": "m1"})
        assert set(body) == {"name", "args"}
        assert body["name"] == "v3_update"
        assert isinstance(body["args"], dict)

    @pytest.mark.parametrize("name,args", [
        ("", {}), ("v3_update", None), ("v3_update", []), (None, {}),
    ])
    def test_tool_request_refuses_an_unusable_identity(self, name, args):
        with pytest.raises(D.Refused):
            D.build_tool_request(name, args)

    def test_prefetch_request_matches_serve__handle_prefetch(self):
        body = D.build_prefetch_request("迁移项目现在采用什么部署方案？", "s1")
        assert body == {"query": "迁移项目现在采用什么部署方案？", "session_id": "s1"}

    def test_prefetch_request_refuses_an_empty_query(self):
        for q in ("", "   ", None):
            with pytest.raises(D.Refused):
                D.build_prefetch_request(q, "s1")

    def test_events_request_matches_bridge_contract_normalize_event(self):
        body = D.build_events_request(session_id="s1", event_id="e1", host="h",
                                      role="user", content="c", turn_id=3)
        assert body["session_id"] == "s1"
        assert body["event_id"] == "e1"
        assert body["host"] == "h"
        assert body["role"] == "user"
        assert body["turn_id"] == 3

    def test_events_request_refuses_a_missing_identity(self):
        for kwargs in (
            dict(session_id="", event_id="e", host="h", role="user", content="c", turn_id=1),
            dict(session_id="s", event_id="", host="h", role="user", content="c", turn_id=1),
            dict(session_id="s", event_id="e", host="h", role="user", content="", turn_id=1),
        ):
            with pytest.raises(D.Refused):
                D.build_events_request(**kwargs)


# ══════════════════════════════════════════════════════════════════════════
# single-source schema contract
# ══════════════════════════════════════════════════════════════════════════

class TestSchemaCopyReport:
    def test_identical_copies_are_in_sync(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "explicit_memories.sql").write_text("CREATE TABLE t();", encoding="utf-8")
        (b / "explicit_memories.sql").write_text("CREATE TABLE t();", encoding="utf-8")
        report = D.schema_copy_report([a, b])
        assert report["in_sync"] is True
        assert "explicit_memories.sql" in report["files"]

    def test_a_forked_copy_is_detected(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "memory_relations.sql").write_text("-- M01", encoding="utf-8")
        (b / "memory_relations.sql").write_text("-- M01 drift", encoding="utf-8")
        report = D.schema_copy_report([a, b])
        assert report["in_sync"] is False
        assert report["files"]["memory_relations.sql"]["matches"][str(b)] is False

    def test_a_missing_packaged_artifact_is_detected(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "upgrade_v0_2.sql").write_text("-- x", encoding="utf-8")
        report = D.schema_copy_report([a, b])
        assert report["in_sync"] is False

    def test_an_artifact_only_in_the_packaged_copy_is_detected(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "one.sql").write_text("-- x", encoding="utf-8")
        (b / "one.sql").write_text("-- x", encoding="utf-8")
        (b / "orphan.sql").write_text("-- y", encoding="utf-8")
        report = D.schema_copy_report([a, b])
        assert report["in_sync"] is False
        assert "orphan.sql" in report["only_in_packaged"][str(b)]

    def test_a_single_directory_cannot_prove_synchronization(self, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        (a / "one.sql").write_text("-- x", encoding="utf-8")
        report = D.schema_copy_report([a])
        assert report["in_sync"] is False
        assert "at least two" in report["reason"]

    def test_an_empty_schema_directory_cannot_pass(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        report = D.schema_copy_report([a, b])
        assert report["in_sync"] is False
        assert "no .sql artifacts" in report["reason"]


# ══════════════════════════════════════════════════════════════════════════
# receipt contract (design §13) and payload navigation
# ══════════════════════════════════════════════════════════════════════════

FULL_RECEIPT = {
    "success": True,
    "old_memory_id": "mem_a",
    "new_memory_id": "mem_b",
    "relation": "supersedes",
    "old_status": "archived",
    "new_status": "active",
    "correction_source": {"correction_source_id": 42, "provenance": "explicit_tool_request"},
    "deduplicated": False,
}


class TestReceiptContract:
    def test_a_full_receipt_satisfies_the_contract(self):
        assert D.receipt_contract_missing(FULL_RECEIPT) == []

    def test_a_bare_success_is_never_accepted(self):
        # The design forbids a bare ``success: true`` answer: the caller must be
        # able to tell WHICH rows changed.
        missing = D.receipt_contract_missing({"success": True})
        assert missing
        assert any("old_memory_id" in m for m in missing)
        assert any("new_memory_id" in m for m in missing)
        assert any("relation" in m for m in missing)
        assert any("deduplicated" in m for m in missing)

    def test_a_non_object_response_is_reported_not_crashed(self):
        assert D.receipt_contract_missing("success") == ["receipt: response is not an object"]
        assert D.receipt_contract_missing(None) == ["receipt: response is not an object"]

    def test_alias_keys_are_accepted(self):
        aliased = {
            "success": True,
            "superseded_memory_id": "mem_a",
            "replacement_memory_id": "mem_b",
            "edge_type": "supersedes",
            "from_status": "archived",
            "to_status": "active",
            "source": "explicit_tool_request",
            "already_applied": True,
        }
        assert D.receipt_contract_missing(aliased) == []

    def test_nested_receipt_keys_are_found(self):
        nested = {"success": True, "result": dict(FULL_RECEIPT)}
        assert D.receipt_contract_missing(nested) == []


class TestPayloadNavigation:
    def test_pick_prefers_the_first_present_alias(self):
        assert D.pick(FULL_RECEIPT, "old_memory_id", "from_memory_id") == "mem_a"
        assert D.pick(FULL_RECEIPT, "nope", "new_memory_id") == "mem_b"

    def test_pick_skips_empty_values(self):
        assert D.pick({"a": "", "b": "kept"}, "a", "b") == "kept"
        assert D.pick({"a": None, "b": []}, "a", "b") is None

    def test_pick_descends_into_nested_objects(self):
        assert D.pick({"outer": {"inner": "found"}}, "inner") == "found"

    def test_pick_never_raises_on_a_non_object(self):
        assert D.pick(None, "x") is None
        assert D.pick([1, 2], "x") is None

    def test_find_values_collects_every_occurrence(self):
        found = D.find_values(
            {"a": {"memory_id": "1"}, "b": [{"memory_id": "2"}, {"memory_id": "3"}]},
            "memory_id")
        assert sorted(found) == ["1", "2", "3"]

    def test_find_values_on_a_scalar_is_empty(self):
        assert D.find_values(None, "x") == []
        assert D.find_values(7, "x") == []


# ══════════════════════════════════════════════════════════════════════════
# the embedded derived probe
# ══════════════════════════════════════════════════════════════════════════

class TestEmbeddedDerivedProbe:
    def test_the_probe_source_compiles(self):
        compile(D.DERIVED_PROBE_SOURCE, "<derived_probe>", "exec")

    def test_the_probe_is_marked_derived_only(self):
        assert '"derived_only": True' in D.DERIVED_PROBE_SOURCE

    def test_the_probe_carries_no_provider_endpoint_key_or_model(self):
        text = D.DERIVED_PROBE_SOURCE
        for forbidden in ("api_key", "apiKey", "Authorization", "Bearer ",
                          "bge-m3", "minimax", "openai", "anthropic",
                          "http://", "https://"):
            assert forbidden not in text, f"derived probe mentions {forbidden!r}"

    def test_the_probe_calls_the_canonical_writer_api(self):
        ast.parse(D.DERIVED_PROBE_SOURCE)
        assert "ActiveMemoryWriter" in D.DERIVED_PROBE_SOURCE
        assert ".correct(" in D.DERIVED_PROBE_SOURCE
        # authority is never obtained implicitly: every positive probe passes it
        # explicitly, and the negative probes omit or falsify it.
        assert 'authority="user_explicit"' in D.DERIVED_PROBE_SOURCE
        assert 'authority="model_inference"' in D.DERIVED_PROBE_SOURCE

    def test_every_probe_the_driver_requires_is_actually_emitted(self):
        # The probe writes ``out["probes"][<name>] = {...}``, so the emitted set
        # is the key of a subscript whose base is itself the probes subscript.
        tree = ast.parse(D.DERIVED_PROBE_SOURCE)

        def is_probes_map(node) -> bool:
            return (isinstance(node, ast.Subscript)
                    and isinstance(node.slice, ast.Constant)
                    and node.slice.value == "probes")

        emitted = {
            node.slice.value for node in ast.walk(tree)
            if isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.value, ast.Subscript)
            and is_probes_map(node.value)
        }
        assert set(D.DERIVED_PROBE_KEYS) <= emitted, (
            "the driver requires probes the probe source never writes: "
            f"{sorted(set(D.DERIVED_PROBE_KEYS) - emitted)}")

    def test_the_two_calls_that_must_succeed_pass_authority_explicitly(self):
        # Without authority these calls are refused by the PERMISSION check, so
        # the probe would silently measure permission rejection instead of the
        # source-validation / embedding-durability path it claims to test.
        tree = ast.parse(D.DERIVED_PROBE_SOURCE)
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == "correct"
        ]
        def has_user_explicit(call) -> bool:
            """A ``authority=`` keyword pinned to the required literal.

            Checked structurally on the AST, not on the unparsed text: unparse
            normalises every string to single quotes, so a textual comparison
            against the double-quoted source spelling would be vacuous.
            """
            return any(
                kw.arg == "authority"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == "user_explicit"
                for kw in call.keywords)

        src = D.DERIVED_PROBE_SOURCE
        fake_call = [c for c in calls if "999999999" in ast.unparse(c)]
        assert fake_call, "the fabricated-source probe call is gone"
        for call in fake_call:
            assert has_user_explicit(call), (
                "the fabricated-source probe omits authority and would be refused "
                "for permissions rather than for the source id")
        embed_call = [c for c in calls if "M01M02_EMBED_REPLACEMENT" in ast.unparse(c)]
        assert embed_call, "the embedding-failure probe call is gone"
        for call in embed_call:
            assert has_user_explicit(call), (
                "the embedding-failure probe omits authority and would be refused "
                "before any durable correction exists")
        assert "source_specific" in src

    def test_the_fake_source_check_demands_a_source_specific_refusal(self):
        src = D.DERIVED_PROBE_SOURCE
        # A bare `not _ok(...)` would also be satisfied by a permission refusal,
        # a missing target, or a missing table.
        assert "SOURCE_REJECTION_CODES" in src
        assert "INVALID_CORRECTION_SOURCE" in src
        assert '"ok": refused and source_specific' in src


class TestDriverFixes:
    """The specific defects the parent review found, pinned structurally."""

    def _src(self) -> str:
        return Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")

    def test_relation_count_is_not_double_indexed(self):
        # relation_count() already returns an int; `[0][0]` on it would raise a
        # TypeError and take the whole run down at that check.
        src = self._src()
        assert "relation_count(\"from_memory_id=%s\", (mem_a,))[0][0]" not in src
        assert 'relation_count("from_memory_id=%s", (mem_a,)) == 1' in src

    def test_concurrent_case_resolves_its_futures(self):
        src = self._src()
        assert "fut.result(" in src, "concurrent_case still returns unresolved futures"
        # Returning the futures themselves made every tool_ok() check vacuous:
        # a Future is always truthy.
        assert "return {f\"t{i}\": fut for i, fut in enumerate(" not in src

    def test_concurrent_identical_requires_all_three_to_succeed(self):
        src = self._src()
        assert "oks == 3" in src
        assert "oks >= 1" not in src, "'at least one succeeded' still passes"

    def test_fresh_process_search_requires_a_non_empty_terminal_hit(self):
        src = self._src()
        assert "(not b_ids) or" not in src, (
            "an empty search result is still admitted as a pass")
        assert "fresh_process_search_finds_the_chain_terminal_version" in src

    def test_required_probes_and_servers_fail_instead_of_being_a_gap(self):
        src = self._src()
        assert "derived_probe_process_exited_clean" in src
        assert "derived_probe_reported_every_expected_probe" in src
        assert "server_b_health_ok" in src

    def test_the_correction_source_id_is_sent_as_a_string(self):
        src = self._src()
        assert '"correction_source_id": str(real_source_id)' in src
        assert '"correction_source_id": real_source_id,' not in src
        assert '"correction_source_id": 999999999' not in src, (
            "an int source id is rejected by argument validation, not by the "
            "source lookup it is meant to test")
        assert "BOGUS_SOURCE_ID" in src
        assert isinstance(D.BOGUS_SOURCE_ID, str)

    def test_the_upgrade_proof_starts_from_a_real_prior_install(self):
        src = self._src()
        # The prior install must exist BEFORE the first dry-run, and the
        # dry-run must be checked against a snapshot taken before it.
        assert "prior_install_has_data_and_no_relations_table" in src
        assert "upgrade_dry_run_writes_no_ddl" in src
        assert "CREATE EXTENSION IF NOT EXISTS vector" in src
        assert "explicit_memories.sql" in src
        first_dry = src.index('"upgrade-dry-run-1"')
        assert first_dry > src.index("prior_row_snapshot(db_upgrade)"), (
            "the prior-install snapshot must be taken before the first dry-run")
        assert 'check("upgrade_dry_run_writes_no_ddl", not pre_tables' not in src, (
            "the dry-run no-DDL check still asserts an empty database")

    def test_upgrade_parity_compares_the_real_schema_not_only_column_names(self):
        src = self._src()
        assert 'f"upgrade_schema_parity_{part}"' in src, (
            "the parity check is no longer generated per schema part")
        for part in ("columns", "indexes", "constraints"):
            assert f'"{part}"' in src or f"'{part}'" in src
        for marker in ("indexdef", "pg_get_constraintdef", "is_nullable",
                       "column_default"):
            assert marker in src, f"the fingerprint never reads {marker}"
        assert "pg_get_constraintdef" in src
        assert "indexdef" in src

    def test_server_stop_reports_a_failed_kill_instead_of_faking_cleanup(self):
        src = self._src()
        assert "alive_after_stop" in src
        assert "cleanup_failed" in src
        # Unconditional `self.proc = None` made an unkillable server look clean.
        stop_body = src[src.index("    def stop(self) -> dict:"):
                        src.index("    def alive(self) -> bool:")]
        assert stop_body.count("self.proc = None") == 1
        assert "still_alive" in stop_body

    def test_the_run_root_parent_is_the_documented_integration_root(self):
        assert D.RUN_ROOT_PARENT == Path("C:/hp-testbed/m01-m02-integration-20261003")

    def test_the_interpreter_gate_runs_before_any_root_is_created(self):
        src = self._src()
        gate = src.index('if not Path(python).is_file():')
        mkdirs = src.index("    for d in (home, project, runs, profiles, data_dir):")
        assert gate < mkdirs, (
            "the --env-python gate must run before the isolated roots are created")

    def test_raw_cli_streams_are_scrubbed_before_they_are_written(self):
        src = self._src()
        # Raw-then-redact would leave a secret-bearing file inside the run root.
        assert "out_full = scrub(proc.stdout or \"\", secret_value)" in src
        assert "err_full = scrub(proc.stderr or \"\", secret_value)" in src
        assert '(runs / f"{label}.stdout.json").write_text(out_full' in src
        assert '(runs / f"{label}.stderr.txt").write_text(err_full' in src
        assert '(runs / f"{label}.stdout.json").write_text(proc.stdout' not in src

    def test_truncation_is_flagged_and_the_full_payload_is_kept(self):
        src = self._src()
        for field in ("stdout_truncated", "stderr_truncated", "parsed_truncated"):
            assert field in src, f"{field} is missing — a truncated record is "
        assert "full_redacted_file" in src
        assert "response_truncated_in_report" in src
        assert "stdout_full_file" in src


class TestDriverShape:
    def test_the_driver_runs_only_on_the_test_laptop(self):
        assert D.HOST_GATE == "DESKTOP-EQP3OBU"
        assert D.PG_PORT_ALLOWLIST == (55432,)
        assert D.DB_PREFIX == "m01m02e2e_"

    def test_the_driver_never_hardcodes_a_password(self):
        source = Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")
        # `password=os.environ[...]` / `password=secret_value` are keyword arguments,
        # not credentials. A LITERAL is what must never appear: an assignment or
        # keyword whose value is a non-empty quoted string.
        literal = re.compile(r"""password\s*=\s*['"][^'"]+['"]""")
        assert not literal.search(source), literal.search(source).group(0)
        # The credential reaches the child processes ONLY through the env dict it
        # builds, and is read from the secrets file — never from argv, never from a
        # config file. (`os.environ["PGPASSWORD"]` reads belong to the embedded
        # probe, which runs in-process with the env the driver handed it.)
        assert 'env.update({' in source
        assert '"PGPASSWORD": secret_value' in source
        assert 'args.secrets' in source
        for forbidden in ('"--password"', "'--password'", "password_file="):
            assert forbidden not in source, f"the driver passes {forbidden!r}"

    def test_the_isolated_profile_config_contains_no_credential(self):
        source = Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")
        # The YAML writer (a nested closure) must not emit any key that could
        # carry the password — the credential is env-only by design.
        writer = _function_source(source, "write_config")
        assert "yaml.safe_dump" in writer or "write_text" in writer
        for forbidden in ("password:", "PGPASSWORD", "secret_value"):
            assert forbidden not in writer, f"write_config emits {forbidden!r}"

    def test_the_driver_scrubs_the_secret_before_writing_the_report(self):
        source = Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")
        assert "scrub(payload, secret_value)" in source

    def test_the_driver_records_raw_requests_and_responses(self):
        source = Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")
        for bucket in ('"tool_calls"', '"cli"', '"prefetch"', '"http"'):
            assert bucket in source, f"the report has no {bucket} evidence bucket"

    def test_the_driver_marks_its_own_coverage_gaps(self):
        source = Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")
        for topic in ("M03", "M04", "no model answered"):
            assert topic in source, f"no explicit coverage-gap note for {topic!r}"

    def test_the_driver_owns_a_readiness_bounded_wait_not_a_busy_loop(self):
        source = Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")
        assert "time.monotonic()" in source
        assert "time.sleep(0.5)" in source
        ready = D.READY_TIMEOUT_S
        assert 0 < ready <= 180, "the readiness wait must be bounded"

    def test_every_server_is_stopped_through_the_owned_registry(self):
        source = Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")
        assert "OWNED_SERVERS" in source
        assert "no_leftover_server_process" in source


# ══════════════════════════════════════════════════════════════════════════
# ambient environment sanitisation (no inherited provider / bypass env)
# ══════════════════════════════════════════════════════════════════════════

class TestEnvSanitization:
    """A disposable run must not inherit ambient creds, endpoints or bypasses."""

    def test_provider_and_embed_credentials_are_dropped(self):
        environ = {
            "PATH": "C:/Windows",
            "V3CORE_HOME": "C:/tmp/home",
            "OPENAI_API_KEY": "sk-x",
            "MINIMAX_API_KEY": "sk-y",
            "DEEPSEEK_API_KEY": "sk-z",
            "EMBED_ENDPOINT": "http://paid.example/v1",
            "RERANK_BASE_URL": "http://paid.example/rerank",
            "HTTPS_PROXY": "http://proxy:8080",
            "PGPASSWORD": "pgpw",
            "PGHOST": "127.0.0.1",
        }
        clean = D.sanitize_env(environ)
        for dropped in ("OPENAI_API_KEY", "MINIMAX_API_KEY", "DEEPSEEK_API_KEY",
                        "EMBED_ENDPOINT", "RERANK_BASE_URL", "HTTPS_PROXY",
                        "PGPASSWORD", "PGHOST"):
            assert dropped not in clean, f"{dropped} survived sanitize_env"
        assert clean["PATH"] == "C:/Windows"
        assert clean["V3CORE_HOME"] == "C:/tmp/home"

    def test_production_bypass_switches_are_cleared(self):
        environ = {
            "V3_HARNESS_ALLOW_LIVE_PROD_OUTBOX": "1",
            "V3CORE_ALLOW_PRODUCTION": "1",
            "V3CORE_GUARD_OVERRIDE": "1",
            "COMPUTERNAME": "DESKTOP-EQP3OBU",
        }
        assert D.sanitize_env(environ) == {"COMPUTERNAME": "DESKTOP-EQP3OBU"}

    def test_the_driver_uses_the_sanitizer_not_a_pg_only_filter(self):
        src = Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")
        assert "env = sanitize_env(os.environ)" in src
        assert "env = {k: v for k, v in os.environ.items()" not in src
        # the password is still supplied at runtime only
        assert '"PGPASSWORD": secret_value' in src


# ══════════════════════════════════════════════════════════════════════════
# installed-artifact provenance (a stale wheel in the named venv is refused)
# ══════════════════════════════════════════════════════════════════════════

class TestArtifactProvenanceGate:
    def test_matching_fingerprints_pass(self):
        fp = {"module:__init__.py": "a" * 64, "sql:memory_relations.sql": "b" * 64}
        assert D.artifact_provenance_mismatches(fp, dict(fp), list(fp)) == []

    def test_a_stale_installed_module_is_refused(self):
        expected = {"module:active_memory_store.py": "a" * 64}
        actual = {"module:active_memory_store.py": "c" * 64}
        problems = D.artifact_provenance_mismatches(
            expected, actual, ["module:active_memory_store.py"])
        assert problems and "active_memory_store.py" in problems[0]

    def test_a_missing_installed_artifact_is_refused(self):
        problems = D.artifact_provenance_mismatches(
            {"sql:memory_relations.sql": "b" * 64}, {}, ["sql:memory_relations.sql"])
        assert problems and "missing from the installed artifact" in problems[0]

    def test_a_missing_source_counterpart_is_refused(self):
        problems = D.artifact_provenance_mismatches(
            {}, {"module:__init__.py": "a" * 64}, ["module:__init__.py"])
        assert problems and "missing from the source-root" in problems[0]

    def test_the_required_modules_cover_entry_writer_tool_and_distribution(self):
        required = set(D.ARTIFACT_PROVENANCE_MODULES)
        for expected in ("__init__.py", "active_memory_store.py",
                         "tools/api_update.py", "tools/get_tool.py",
                         "tools/memory_correction.py", "tools/api_get.py",
                         "distribution_cli.py"):
            assert expected in required, f"{expected} is not fingerprinted"

    def test_the_modified_api_get_tool_is_fingerprinted(self):
        # tools/api_get.py is modified by this task, so its installed bytes
        # must be compared to the source root like every other touched module.
        assert "tools/api_get.py" in D.ARTIFACT_PROVENANCE_MODULES

    def test_a_stale_api_get_fingerprint_is_refused(self):
        key = "module:tools/api_get.py"
        problems = D.artifact_provenance_mismatches(
            {key: "a" * 64}, {key: "c" * 64}, [key])
        assert problems and "tools/api_get.py" in problems[0]

    def test_a_missing_api_get_fingerprint_is_refused(self):
        key = "module:tools/api_get.py"
        problems = D.artifact_provenance_mismatches({key: "a" * 64}, {}, [key])
        assert problems and "missing from the installed artifact" in problems[0]

    def test_the_provenance_gate_runs_before_any_psycopg2_connect(self):
        src = Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")
        assert "installed_artifact_matches_source_root" in src
        gate = src.index("collect_artifact_provenance(python, env, project, source)")
        connect = src.index("import psycopg2  # noqa: PLC0415")
        assert gate < connect, "provenance must be compared before any DB connect"


# ══════════════════════════════════════════════════════════════════════════
# timestamp equality (receipt recorded_at vs the DB value)
# ══════════════════════════════════════════════════════════════════════════

class TestTimestampEquality:
    def test_the_same_instant_in_two_renderings_is_equal(self):
        assert D.timestamps_equal("2026-10-03 12:00:00+08", "2026-10-03T12:00:00+08:00")
        assert D.timestamps_equal("2026-10-03T04:00:00Z", "2026-10-03 12:00:00+08:00")

    def test_different_instants_are_not_equal(self):
        assert not D.timestamps_equal("2026-10-03T12:00:00+08:00",
                                      "2026-10-03T12:00:01+08:00")

    def test_absent_or_unparseable_values_are_never_equal(self):
        assert not D.timestamps_equal(None, "2026-10-03T12:00:00+08:00")
        assert not D.timestamps_equal("", "")
        assert not D.timestamps_equal("not-a-time", "2026-10-03T12:00:00+08:00")


# ══════════════════════════════════════════════════════════════════════════
# canonical reader helpers used by the dedicated withdraw scenario
# ══════════════════════════════════════════════════════════════════════════

class TestReaderHelpers:
    def test_a_withdrawn_current_read_has_no_current(self):
        payload = {"success": True, "mode": "current", "memory": None,
                   "resolved_memory_id": None, "is_current": False,
                   "requested": {"memory_id": "c", "content": "old"}}
        ok, detail = D.reader_current_absent(payload)
        assert ok is True, detail

    def test_a_live_current_read_is_not_mistaken_for_no_current(self):
        payload = {"memory": {"memory_id": "b"}, "resolved_memory_id": "b",
                   "is_current": True}
        assert D.reader_current_absent(payload)[0] is False

    def test_is_current_must_be_explicitly_false(self):
        payload = {"memory": None, "resolved_memory_id": None}
        assert D.reader_current_absent(payload)[0] is False, \
            "a missing is_current was treated as no-current"

    def test_a_missing_memory_field_is_refused(self):
        # Only is_current=False is present: a malformed response that never
        # carries the canonical memory field must NOT be read as "no current".
        payload = {"success": True, "mode": "current", "is_current": False}
        assert D.reader_current_absent(payload)[0] is False, \
            "a response with no canonical 'memory' field was accepted"

    def test_an_explicit_null_memory_field_is_accepted(self):
        payload = {"success": True, "mode": "current", "memory": None,
                   "is_current": False, "resolved_memory_id": None,
                   "current_memory_id": None}
        ok, detail = D.reader_current_absent(payload)
        assert ok is True, detail

    def test_a_nested_diagnostic_memory_cannot_supply_the_canonical_field(self):
        # The parent's exact BAD shape: the canonical memory field is absent at
        # the top level; a nested diagnostic merely reports memory:null. That
        # nested report must NOT be read as a real no-current answer.
        payload = {"is_current": False, "current_memory_id": None,
                   "resolved_memory_id": None, "diagnostic": {"memory": None}}
        assert D.reader_current_absent(payload)[0] is False, \
            "a nested diagnostic.memory filled the absent canonical field"

    def test_is_current_must_be_top_level_not_nested(self):
        payload = {"memory": None, "resolved_memory_id": None,
                   "current_memory_id": None,
                   "diagnostic": {"is_current": False}}
        assert D.reader_current_absent(payload)[0] is False, \
            "a nested is_current was accepted in place of the top-level field"

    def test_canonical_fields_split_across_siblings_are_refused(self):
        # No sibling may splice its half of the shape onto another's half.
        payload = {"left": {"memory": None},
                   "right": {"resolved_memory_id": None,
                             "current_memory_id": None,
                             "is_current": False}}
        assert D.reader_current_absent(payload)[0] is False, \
            "sibling dicts were spliced into a fake no-current shape"

    def test_a_raw_envelope_cannot_fill_the_missing_top_level_memory(self):
        payload = {"_ok": False, "error": "undecodable",
                   "_envelope": {"ok": True, "result": {
                       "memory": None, "resolved_memory_id": None,
                       "current_memory_id": None, "is_current": False}}}
        assert D.reader_current_absent(payload)[0] is False, \
            "the raw _envelope supplied the missing top-level canonical memory"

    def test_a_good_flattened_payload_ignores_diagnostic_and_envelope(self):
        payload = {"success": True, "mode": "current", "memory": None,
                   "resolved_memory_id": None, "current_memory_id": None,
                   "is_current": False,
                   "diagnostic": {"memory": "irrelevant"},
                   "_envelope": {"result": {"memory": "junk"}}}
        ok, detail = D.reader_current_absent(payload)
        assert ok is True, detail

    def test_a_live_memory_with_false_is_current_is_refused(self):
        payload = {"memory": {"memory_id": "live"}, "is_current": False,
                   "resolved_memory_id": None, "current_memory_id": None}
        assert D.reader_current_absent(payload)[0] is False, \
            "a nonnull memory was accepted just because is_current was false"

    def test_a_nonnull_id_refuses_even_when_a_sibling_says_null(self):
        payload = {"memory": None, "is_current": False,
                   "resolved_memory_id": "still-there",
                   "sibling": {"resolved_memory_id": None}}
        assert D.reader_current_absent(payload)[0] is False, \
            "a supplied nonnull resolved id was masked by a sibling null"

    def test_history_ids_and_statuses_follow_the_version_list(self):
        payload = {"history": [{"memory_id": "a", "status": "archived"},
                               {"memory_id": "b", "status": "archived"},
                               {"memory_id": "c", "status": "withdrawn"}]}
        assert D.reader_history_ids(payload) == ["a", "b", "c"]
        assert D.reader_history_statuses(payload) == ["archived", "archived", "withdrawn"]

    def test_requested_payload_is_read_verbatim(self):
        payload = {"requested": {"memory_id": "c", "content": "exact"},
                   "memory": None}
        assert D.reader_requested(payload)["content"] == "exact"

    def test_relation_chain_maps_supersedes_and_withdraws(self):
        payload = {"relations": [
            {"relation_type": "supersedes", "from_memory_id": "a", "to_memory_id": "b"},
            {"relation_type": "withdraws", "from_memory_id": "c", "to_memory_id": None},
        ]}
        assert D.reader_relation_chain(payload) == [
            ("supersedes", "a", "b"), ("withdraws", "c", None)]

    def test_a_non_object_payload_never_raises(self):
        for payload in (None, "x", 7, [1, 2]):
            assert D.reader_history_ids(payload) == []
            assert D.reader_relation_chain(payload) == []
            assert D.reader_current_absent(payload)[0] is False


class TestExactOrderedRelationChain:
    """The E2E ordered-chain check must be EXACT, not a set/superset test."""

    EXPECTED = [("supersedes", "a", "b"), ("supersedes", "b", "c"),
                ("withdraws", "c", None)]

    def test_the_exact_ordered_chain_passes(self):
        assert D.relation_chain_matches_exact(list(self.EXPECTED), self.EXPECTED) is True

    def test_reverse_order_is_refused(self):
        assert D.relation_chain_matches_exact(
            list(reversed(self.EXPECTED)), self.EXPECTED) is False

    def test_adjacent_swap_is_refused(self):
        swapped = [self.EXPECTED[1], self.EXPECTED[0], self.EXPECTED[2]]
        assert D.relation_chain_matches_exact(swapped, self.EXPECTED) is False

    def test_a_duplicate_edge_is_refused(self):
        dup = [self.EXPECTED[0], self.EXPECTED[0], self.EXPECTED[1], self.EXPECTED[2]]
        assert D.relation_chain_matches_exact(dup, self.EXPECTED) is False

    def test_an_extra_edge_is_refused(self):
        extra = list(self.EXPECTED) + [("supersedes", "c", "d")]
        assert D.relation_chain_matches_exact(extra, self.EXPECTED) is False

    def test_a_missing_edge_is_refused(self):
        assert D.relation_chain_matches_exact(self.EXPECTED[:-1], self.EXPECTED) is False

    def test_a_reordered_set_would_be_accepted_by_superset_but_is_refused(self):
        reordered = [self.EXPECTED[2], self.EXPECTED[0], self.EXPECTED[1]]
        assert D.relation_chain_matches_exact(reordered, self.EXPECTED) is False


# ══════════════════════════════════════════════════════════════════════════
# the restored withdraw contract is really exercised by the driver
# ══════════════════════════════════════════════════════════════════════════

class TestWithdrawScenarioContract:
    def _src(self) -> str:
        return Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")

    @pytest.mark.parametrize("check_name", [
        "dedicated_scenario_target_committed",
        "dedicated_chain_a_to_b_succeeded",
        "dedicated_chain_a_to_b_supersedes",
        "dedicated_chain_b_to_c_succeeded",
        "dedicated_history_from_c_includes_full_chain",
        "dedicated_history_chain_is_ordered",
        "dedicated_history_from_c_preserves_exact_content",
        "dedicated_retry_first_correction_succeeds",
        "dedicated_retry_refers_original_b",
        "dedicated_retry_reports_b_archived_c_current",
        "dedicated_retry_creates_zero_changes",
        "dedicated_withdraw_c_succeeded",
        "dedicated_withdraw_creates_no_replacement",
        "dedicated_withdraw_edge_has_null_target",
        "dedicated_withdraw_exact_retry_idempotent",
        "dedicated_conflicting_replace_refused",
        "dedicated_current_read_a_has_no_current",
        "dedicated_current_read_c_has_no_current",
        "dedicated_history_after_withdraw_keeps_full_chain",
        "dedicated_history_after_withdraw_preserves_exact_content",
        "dedicated_history_relation_chain_ordered",
        "dedicated_scenario_raw_qa_unchanged",
        "dedicated_scenario_left_existing_rows_unchanged",
        "dedicated_scenario_wrote_exactly_its_own_rows",
        "fresh_process_search_excludes_withdrawn_target",
        "fresh_session_recall_excludes_withdrawn_payload",
    ])
    def test_the_scenario_check_is_present(self, check_name):
        assert check_name in self._src(), f"{check_name} is not asserted by the driver"

    def test_the_withdraw_call_carries_no_replacement_fields(self):
        src = self._src()
        start = src.index("wd_withdraw_args = {")
        block = src[start:src.index("}", start)]
        for forbidden in ("replacement_content", "replacement_title", "replacement_tags"):
            assert forbidden not in block, f"the withdraw request sends {forbidden}"

    def test_both_modes_are_forwarded_on_the_public_tool(self):
        src = self._src()
        assert '"mode": "withdraw"' in src
        assert '"mode": "replace"' in src

    def test_the_retry_asserts_b_archived_and_c_current(self):
        src = self._src()
        assert '"archived"' in src
        assert "current_memory_id" in src

    def test_the_dedicated_target_uses_its_own_unique_marker(self):
        src = self._src()
        assert "withdraw-dedicated" in src
        assert 'wd_a = f"m01m02e2e-wd-a-' in src

    def test_the_scenario_snapshots_rows_and_raw_before_and_after(self):
        src = self._src()
        assert "table_snapshot()" in src
        assert "wd_pre_rows" in src and "wd_post_rows" in src
        assert "wd_pre_raw" in src and "wd_post_raw" in src

    def test_the_ordered_chain_check_is_exact_not_a_set(self):
        src = self._src()
        assert "relation_chain_matches_exact(wd_rel_chain, wd_expected_edges)" in src, \
            "the dedicated check does not use the exact ordered predicate"
        assert "wd_expected_edges <=" not in src, \
            "a subset/superset comparison is still present"
        assert "set(wd_rel_chain)" not in src, \
            "the chain is still coerced to a set"


# ══════════════════════════════════════════════════════════════════════════
# the remaining parent-verified driver defects
# ══════════════════════════════════════════════════════════════════════════

class TestDriverFixesRound2:
    def _src(self) -> str:
        return Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")

    def test_a_nonzero_import_rc_refuses_before_connecting(self):
        src = self._src()
        assert 'artifact_reason = "" if which.returncode != 0' not in src
        assert "if which.returncode != 0:" in src
        rc_guard = src.index("if which.returncode != 0:")
        connect = src.index("import psycopg2  # noqa: PLC0415")
        assert rc_guard < connect, "the import rc guard must precede any DB connect"

    def test_packaged_presence_is_explicit_not_just_non_empty(self):
        src = self._src()
        assert 'check("packaged_relations_artifact_reachable", bool(packaged_map)' not in src
        assert "missing_required_packaged" in src
        assert "REQUIRED_PACKAGED_SQL" in src
        assert '"memory_relations.sql"' in src
        assert "missing_counterpart" in src

    def test_the_crash_report_scrubs_the_real_secret(self):
        src = self._src()
        assert 'scrub(str(exc)[:2000], "")' not in src
        assert "scrub(str(exc)[:2000], _ACTIVE_SECRET)" in src
        assert "scrub(traceback.format_exc()[-4000:], _ACTIVE_SECRET)" in src
        assert "_ACTIVE_SECRET = secret_value" in src

    def test_a_failed_derived_target_stops_instead_of_falling_back(self):
        src = self._src()
        assert "derived_target = mem_nosrc" not in src
        start = src.index('check("derived_probe_target_committed"')
        assert "return finish()" in src[start:start + 900], \
            "a failed derived target must STOP, not fall back"

    def test_the_relation_type_is_supersedes_not_corrects(self):
        src = self._src()
        assert 'edge["relation_type"] == "supersedes"' in src
        assert 'edge["relation_type"] == "corrects"' not in src
        assert 'check("edge_relation_type_supersedes"' in src

    def test_the_receipt_recorded_at_is_compared_to_the_database(self):
        src = self._src()
        assert "receipt_recorded_at_is_present_and_db_equal" in src
        assert "timestamps_equal(receipt_recorded_at, edge.get(ts_col))" in src

    def test_the_source_head_is_advisory_not_an_artifact_claim(self):
        src = self._src()
        assert '"source_head_advisory"' in src
        assert '"source_head_is_artifact_claim"] = False' in src

    def test_the_receipt_fixture_uses_the_restored_relation_type(self):
        assert FULL_RECEIPT["relation"] == "supersedes"


# ══════════════════════════════════════════════════════════════════════════
# isolated dotenv closure (the explicit V3CORE_DOTENV must EXIST and be EMPTY)
# ══════════════════════════════════════════════════════════════════════════

class TestIsolatedDotenvClosure:
    """The driver's isolated ``V3CORE_DOTENV`` must be a real, EMPTY file.

    The product (``v3core.config._find_env``) RAISES when an explicit
    ``V3CORE_DOTENV`` does not exist: a dangling value is a fail-closed refusal,
    not a silent no-dotenv. The driver therefore created ``profiles/.env.absent``
    — a path it deliberately never writes — and every server refused to start
    (``/health`` never came up). The driver must instead create an EMPTY file
    inside its isolated home BEFORE any server is launched: the explicit path
    satisfies the product's fail-closed contract, while emptiness guarantees no
    ambient credential can be imported from it.
    """

    def _src(self) -> str:
        return Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")

    def test_the_driver_no_longer_binds_a_dangling_dotenv(self):
        # (a) the old dangling literal must be gone entirely.
        src = self._src()
        assert ".env.absent" not in src, (
            "the driver still binds V3CORE_DOTENV to a path it never creates; "
            "the product refuses a dangling explicit dotenv")

    def test_the_helper_creates_an_empty_file_inside_the_given_home(self, tmp_path):
        # (b) real predicate, no server launched.
        home = tmp_path / "home"
        path = D.ensure_isolated_dotenv(home)
        assert path.is_file(), f"{path} was not created"
        assert path.stat().st_size == 0, f"{path} is not empty"
        assert D._is_lexically_inside(path, home), (
            f"the isolated dotenv {path} is not inside the isolated home {home}")
        assert path == D.isolated_dotenv_path(home)

    def test_the_dotenv_is_created_before_the_first_server_start(self):
        # (c) ordering: creation/ensure precedes the first server launch.
        src = self._src()
        create = src.index("isolated_dotenv = ensure_isolated_dotenv(home)")
        start = src.index("server_a.start()")
        assert create < start, (
            "the isolated dotenv must exist before the first server is started")

    def test_the_bound_dotenv_is_the_helper_result(self):
        # (c) the env value bound to V3CORE_DOTENV is exactly the helper result.
        tree = ast.parse(self._src())
        assigned = False
        bound = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and node.targets[0].id == "isolated_dotenv" \
                    and isinstance(node.value, ast.Call) \
                    and isinstance(node.value.func, ast.Name) \
                    and node.value.func.id == "ensure_isolated_dotenv":
                assigned = True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "update" and node.args \
                    and isinstance(node.args[0], ast.Dict):
                for k, v in zip(node.args[0].keys, node.args[0].values):
                    if isinstance(k, ast.Constant) and k.value == "V3CORE_DOTENV":
                        bound = ast.unparse(v)
        assert assigned, (
            "the driver never assigns isolated_dotenv from ensure_isolated_dotenv")
        assert bound == "str(isolated_dotenv)", (
            f"V3CORE_DOTENV is bound to {bound!r}, not the helper result")

    def test_a_nonexistent_explicit_dotenv_is_refused(self, tmp_path):
        # (d) negative control through the driver's own fail-closed check.
        missing = tmp_path / "home" / ".env.absent"
        reason = D.gate_isolated_dotenv(missing)
        assert reason, "a dangling explicit dotenv was admitted"
        assert "does not exist" in reason

    def test_an_existing_empty_dotenv_is_admitted(self, tmp_path):
        path = D.ensure_isolated_dotenv(tmp_path / "home")
        assert D.gate_isolated_dotenv(path) == ""

    def test_the_fail_closed_dotenv_gate_precedes_connect_and_server_start(self):
        # (3) refusal uses its own gate label and happens before any DB connect
        # and before any server start.
        src = self._src()
        gate = src.index('refuse("isolated_dotenv"')
        start = src.index("server_a.start()")
        connect = src.index("import psycopg2  # noqa: PLC0415")
        assert gate < start, "the dotenv gate must precede the first server start"
        assert gate < connect, "the dotenv gate must precede any DB connect"

    def test_the_report_states_the_dotenv_is_an_existing_empty_file(self):
        # (5) the report facts state what the code actually does.
        src = self._src()
        assert '"isolated_dotenv_path"' in src
        assert '"isolated_dotenv_exists"' in src
        assert '"isolated_dotenv_is_empty"' in src


# ══════════════════════════════════════════════════════════════════════════
# memory_row key mapping (the SQL text must never become the dict keys)
# ══════════════════════════════════════════════════════════════════════════

#: The canonical column names an explicit-memories row dict must expose, in
#: SELECT order. Pinned here so a mapping that drops a column, or that invents a
#: ``::``-suffixed key by splitting the SQL text, cannot satisfy the contract.
CANONICAL_MEMORY_ROW_COLUMNS = (
    "memory_id", "category", "title", "content", "tags",
    "provenance", "status", "created_at", "updated_at",
)

#: The correction verification block (design §5e/§5f). Every key it reads off a
#: ``memory_row`` result must be present in the mapped key set.
_CORRECTION_BLOCK_START = "    # 5e. independent SQL verification of the durable truth"
_CORRECTION_BLOCK_END = "    # 5g. raw / QA untouched by the correction"


def _driver_source() -> str:
    return Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")


def _memory_row_source() -> str:
    return _function_source(_driver_source(), "memory_row")


def _run_memory_row(fake_rows):
    """Run the REAL ``memory_row`` body against a recording fake ``pg_rows``.

    ``memory_row`` is a closure inside ``main()``, so it cannot be imported. Its
    exact AST segment is extracted from the driver and executed with a fake
    reader: the returned dict keys and the SQL text are the driver's own, not a
    re-implementation of them. The mapping constants are supplied when the
    driver exposes them (``getattr`` keeps the RED run honest: before the fix
    they do not exist and the buggy body still runs).
    """
    seen: dict = {}

    def fake_pg_rows(db, sql, params=()):
        seen["db"], seen["sql"], seen["params"] = db, sql, params
        return fake_rows

    namespace = {
        "pg_rows": fake_pg_rows,
        "db_name": "m01m02e2e_fake",
        "MEMORY_ROW_COLUMNS": getattr(D, "MEMORY_ROW_COLUMNS", ()),
        "MEMORY_ROW_KEYS": getattr(D, "MEMORY_ROW_KEYS", ()),
    }
    exec(compile(_memory_row_source(), "<memory_row>", "exec"), namespace)
    return namespace["memory_row"], seen


def _memory_row_row_vars(source: str) -> set:
    """Names assigned from a ``memory_row(...)`` result anywhere in the driver."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "memory_row" for n in ast.walk(node.value)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _correction_block_memory_row_keys() -> set:
    """Every column key the correction verification block reads off a row dict."""
    source = _driver_source()
    start = source.index(_CORRECTION_BLOCK_START)
    end = source.index(_CORRECTION_BLOCK_END)
    start_line = source[:start].count("\n") + 1
    end_line = source[:end].count("\n") + 1
    row_vars = _memory_row_row_vars(source)
    keys = set()

    def receiver_is_a_row(expr) -> bool:
        return any(
            (isinstance(n, ast.Name) and n.id in row_vars)
            or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "memory_row")
            for n in ast.walk(expr))

    for node in ast.walk(ast.parse(source)):
        lineno = getattr(node, "lineno", None)
        if lineno is None or not (start_line <= lineno < end_line):
            continue
        if (isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and receiver_is_a_row(node.value)):
            keys.add(node.slice.value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and receiver_is_a_row(node.func.value)):
            keys.add(node.args[0].value)
    return keys


class TestMemoryRowKeyMapping:
    """``memory_row`` keys come from a declared mapping, never from SQL text.

    The parent's run aborted with ``KeyError: 'provenance'`` because the dict
    keys were derived by splitting the SQL column list, so ``provenance::text``,
    ``created_at::text`` and ``updated_at::text`` leaked in as keys while
    ``row['provenance']`` / ``row['created_at']`` raised. These tests pin the
    canonical key set and guard the query so the fix cannot be satisfied by
    dropping the very columns the block reads.
    """

    def test_the_returned_keys_are_exactly_the_canonical_columns(self):
        row_fn, _ = _run_memory_row([tuple(range(len(CANONICAL_MEMORY_ROW_COLUMNS)))])
        row = row_fn("mem_a")
        assert row is not None
        assert list(row) == list(CANONICAL_MEMORY_ROW_COLUMNS)
        assert set(row) == set(CANONICAL_MEMORY_ROW_COLUMNS)

    def test_no_returned_key_carries_a_sql_cast_fragment(self):
        row_fn, _ = _run_memory_row([tuple(range(len(CANONICAL_MEMORY_ROW_COLUMNS)))])
        row = row_fn("mem_a")
        assert not [k for k in row if "::" in k], f"cast fragments leaked: {sorted(row)}"
        assert not [k for k in row if k.strip() != k], f"untrimmed key: {sorted(row)}"

    def test_the_mapped_key_set_matches_the_canonical_columns(self):
        assert tuple(D.MEMORY_ROW_KEYS) == CANONICAL_MEMORY_ROW_COLUMNS
        assert not [k for k in D.MEMORY_ROW_KEYS if "::" in k]

    def test_a_missing_row_still_returns_none_and_passes_the_id(self):
        row_fn, seen = _run_memory_row([])
        assert row_fn("missing") is None
        assert seen["params"] == ("missing",)

    def test_the_pre_fix_mapping_would_have_produced_a_cast_key(self):
        # Negative control: rebuild the SQL column list the driver now builds and
        # apply the OLD derivation (split the SQL text on commas). The pre-fix
        # mapping yields a ``::`` key, so the canonical access raises KeyError.
        cols = ", ".join(expr for _, expr in D.MEMORY_ROW_COLUMNS)
        legacy_keys = [c.strip() for c in cols.split(",")]
        leaked = [k for k in legacy_keys if "::" in k]
        assert leaked, (
            "the negative control is vacuous: the SQL carries no cast to leak, "
            f"so the pre-fix mapping could not produce a '::' key (keys={legacy_keys})")
        legacy_row = dict(zip(legacy_keys, range(len(legacy_keys))))
        with pytest.raises(KeyError):
            legacy_row["provenance"]
        # the fixed mapping exposes the canonical key for the same value
        row_fn, _ = _run_memory_row([tuple(range(len(legacy_keys)))])
        assert row_fn("mem_a")["provenance"] == legacy_row["provenance::text"]

    def test_every_key_the_correction_block_reads_is_in_the_mapping(self):
        accessed = _correction_block_memory_row_keys()
        assert accessed, "the static scan found no memory-row key access"
        missing = sorted(accessed - set(D.MEMORY_ROW_KEYS))
        assert not missing, (
            "the correction verification block reads memory-row keys the mapping "
            f"does not expose: {missing}; mapped keys are {list(D.MEMORY_ROW_KEYS)}")
        # pin the scan: these canonical keys really are read by that block
        assert {"provenance", "created_at", "status", "content"} <= accessed

    def test_the_query_still_selects_every_mapped_value(self):
        row_fn, seen = _run_memory_row([tuple(range(len(CANONICAL_MEMORY_ROW_COLUMNS)))])
        row_fn("mem_a")
        sql = seen["sql"]
        assert "FROM public.explicit_memories" in sql
        assert "WHERE memory_id=%s" in sql
        for name in CANONICAL_MEMORY_ROW_COLUMNS:
            assert name in sql, f"the query no longer reads the {name!r} value"
        for cast in ("provenance::text", "created_at::text", "updated_at::text"):
            assert cast in sql, f"the read cast {cast!r} was dropped"


# ══════════════════════════════════════════════════════════════════════════
# nested mode-selected reads (current -> the ``memory`` row; history -> the
# ``requested`` row + ``history.versions`` / ``history.edges``) and the
# fresh-session recall classification
# ══════════════════════════════════════════════════════════════════════════

#: A REAL current-mode payload, exactly as the product emits it
#: (``ActiveMemoryReader.read_version(mode='current')`` -> ``handle_memory_target``
#: -> the public ``/tool`` route). The caller named the ARCHIVED A, the chain
#: resolves to the current B, ``memory`` is B's row, and ``is_current`` is False
#: because that flag answers "is the REQUESTED id the current version".
CURRENT_MODE_PAYLOAD = {
    "success": True, "status": "OK", "mode": "current", "memory_id": "mem_a",
    "requested": {"memory_id": "mem_a", "status": "archived",
                  "content": "迁移项目部署方案采用 A。",
                  "title": "迁移项目部署方案", "category": "projects"},
    "memory": {"memory_id": "mem_b", "status": "active",
               "content": "迁移项目部署方案改用 B。",
               "title": "迁移项目部署方案", "category": "projects"},
    "resolved_memory_id": "mem_b", "is_current": False, "current_status": "active",
    "relation": {"relation_type": "supersedes", "from_memory_id": "mem_a",
                 "to_memory_id": "mem_b"},
}

#: A REAL history-mode payload on the withdrawn terminal C of an
#: A -> B -> C -> withdraw chain. The chain lives under ``history``
#: (``versions`` / ``edges`` / ``current_memory_id``); the top-level ``relation``
#: is only the REQUESTED row's single outgoing edge.
HISTORY_MODE_PAYLOAD = {
    "success": True, "status": "OK", "mode": "history", "memory_id": "mem_c",
    "memory": {"memory_id": "mem_c", "status": "archived", "content": "C"},
    "requested": {"memory_id": "mem_c", "status": "archived", "content": "C"},
    "current": None, "resolved_memory_id": None, "is_current": False,
    "current_status": "withdrawn",
    "relation": {"relation_type": "withdraws", "from_memory_id": "mem_c",
                 "to_memory_id": None},
    "history": {
        "versions": [
            {"memory_id": "mem_a", "label": "superseded", "is_current": False,
             "status": "archived", "content": "A"},
            {"memory_id": "mem_b", "label": "superseded", "is_current": False,
             "status": "archived", "content": "B"},
            {"memory_id": "mem_c", "label": "withdrawn", "is_current": False,
             "status": "archived", "content": "C"},
        ],
        "edges": [
            {"relation_type": "supersedes", "from_memory_id": "mem_a",
             "to_memory_id": "mem_b"},
            {"relation_type": "supersedes", "from_memory_id": "mem_b",
             "to_memory_id": "mem_c"},
            {"relation_type": "withdraws", "from_memory_id": "mem_c",
             "to_memory_id": None},
        ],
        "current_memory_id": None,
    },
}

#: The variables the driver binds from a ``v3_get`` read. A canonical field of
#: any of these must be read from the mode-selected nested row, never by a
#: recursive key search.
_CANONICAL_READ_VARS = {"cur", "cur_c", "hist", "hm",
                        "wd_hist", "wd_hist_after", "wd_cur_a", "wd_cur_c"}


def _v3_get_read_vars(source: str) -> set:
    """Names bound to a ``v3_get`` payload anywhere in the driver."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        for call in ast.walk(node.value):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "tool" and len(call.args) >= 2
                    and isinstance(call.args[1], ast.Constant)
                    and call.args[1].value == "v3_get"):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return names


def _pre_fix_history_entries(payload) -> list:
    """The PRE-FIX ``reader_history_entries``: top-level LIST keys only."""
    if not isinstance(payload, dict):
        return []
    for key in ("history", "versions", "version_history", "timeline"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return [item for item in value if isinstance(item, dict)]
    return []


class TestNestedModeReadClosure:
    """A canonical read takes its payload from the MODE-SELECTED nested row.

    ``read_version`` nests the answer — ``requested`` (the exact version the
    caller named), ``memory`` (the resolved current version) and ``history``
    (``versions`` / ``edges`` / ``current_memory_id``). Reading a canonical
    field by RECURSIVE key search therefore returns ``requested``'s value
    first, and reading the chain off the top level returns the requested row's
    single outgoing ``relation`` — which is how a current read that correctly
    answered with B was reported as A's payload, and a history read that
    carried the whole ordered chain was reported as empty / last-edge-only.
    """

    def _src(self) -> str:
        return Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")

    # ── (a) current mode: the memory row, not the requested row ───────────

    def test_a_current_read_takes_the_memory_row_not_the_requested_row(self):
        read = D.reader_current_read(CURRENT_MODE_PAYLOAD)
        assert read["mode"] == "current"
        assert read["has_current"] is True
        assert read["content"] == "迁移项目部署方案改用 B。"
        assert read["memory_id"] == "mem_b"
        assert read["status"] == "active"
        # the chain fields come from the SAME transport-decoded canonical object
        assert read["resolved_memory_id"] == "mem_b"
        assert read["current_status"] == "active"
        # ... and the requested row is reported as a fact, never as the payload
        assert read["requested_memory_id"] == "mem_a"
        assert read["requested_status"] == "archived"

    def test_the_documented_is_current_semantic_is_preserved(self):
        # is_current answers "is the REQUESTED id the current version": False for
        # the archived A that resolved to B. That is truthful, so a current-mode
        # check must assert the RESOLUTION (resolved id == the memory row's id),
        # never a fabricated True.
        read = D.reader_current_read(CURRENT_MODE_PAYLOAD)
        assert read["is_current"] is False
        assert read["resolved_memory_id"] == read["memory_id"] == "mem_b"

    def test_negative_control_the_pre_fix_recursive_pick_yields_the_requested_row(self):
        # the driver's pre-fix read was pick(payload, "content",
        # "replacement_content"), which finds requested.content first.
        assert D.pick(CURRENT_MODE_PAYLOAD, "content", "replacement_content") == \
            "迁移项目部署方案采用 A。"
        assert D.reader_current_read(CURRENT_MODE_PAYLOAD)["content"] == \
            "迁移项目部署方案改用 B。"

    def test_a_withdrawn_current_read_reports_no_payload_not_the_requested_one(self):
        payload = dict(CURRENT_MODE_PAYLOAD)
        payload.update({"memory": None, "resolved_memory_id": None,
                        "is_current": False, "current_status": "withdrawn"})
        read = D.reader_current_read(payload)
        assert read["has_current"] is False
        assert read["content"] is None
        assert read["memory_id"] == ""
        assert read["requested_memory_id"] == "mem_a"   # recorded, never the answer

    def test_the_rendered_flat_payload_is_read_literally(self):
        # the ``hm`` target flattens the mode-selected row into top-level content
        assert D.reader_rendered_content(
            {"content": "B", "requested": {"content": "A"}}) == (True, "B")
        # a withdrawn flattened read reports None: the sibling must not fill it
        assert D.reader_rendered_content(
            {"content": None, "requested": {"content": "A"}}) == (True, "")
        assert D.reader_rendered_content({"requested": {"content": "A"}}) == (False, "")
        assert D.reader_rendered_content(None) == (False, "")

    # ── (b) history mode: versions in order + the full chain ──────────────

    def test_a_history_read_yields_the_versions_in_order(self):
        assert D.reader_history_ids(HISTORY_MODE_PAYLOAD) == \
            ["mem_a", "mem_b", "mem_c"]
        assert D.reader_history_labels(HISTORY_MODE_PAYLOAD) == \
            ["superseded", "superseded", "withdrawn"]
        assert D.reader_history_statuses(HISTORY_MODE_PAYLOAD) == \
            ["archived", "archived", "archived"]
        assert D.reader_history_current_memory_id(HISTORY_MODE_PAYLOAD) == ""

    def test_the_history_is_current_flags_are_read_as_emitted(self):
        # a withdrawn chain has NO current version: every flag is False
        assert D.reader_history_is_current(HISTORY_MODE_PAYLOAD) == [False, False, False]
        live = dict(HISTORY_MODE_PAYLOAD)
        live["history"] = dict(HISTORY_MODE_PAYLOAD["history"])
        live["history"]["versions"] = [
            {"memory_id": "mem_a", "is_current": False},
            {"memory_id": "mem_b", "is_current": True},
        ]
        assert D.reader_history_is_current(live) == [False, True]

    def test_a_history_read_yields_the_full_ordered_chain_not_the_last_edge(self):
        assert D.reader_relation_chain(HISTORY_MODE_PAYLOAD) == [
            ("supersedes", "mem_a", "mem_b"),
            ("supersedes", "mem_b", "mem_c"),
            ("withdraws", "mem_c", None),
        ]
        assert D.reader_history_edges(HISTORY_MODE_PAYLOAD) == \
            HISTORY_MODE_PAYLOAD["history"]["edges"]

    def test_a_history_read_takes_its_payload_from_the_requested_row(self):
        assert D.reader_selected_row(HISTORY_MODE_PAYLOAD)["memory_id"] == "mem_c"
        assert D.reader_requested(HISTORY_MODE_PAYLOAD)["content"] == "C"

    def test_negative_control_the_pre_fix_history_reader_yields_an_empty_list(self):
        assert _pre_fix_history_entries(HISTORY_MODE_PAYLOAD) == []
        assert D.reader_history_ids(HISTORY_MODE_PAYLOAD) == \
            ["mem_a", "mem_b", "mem_c"]

    def test_negative_control_the_pre_fix_chain_reader_yields_only_the_last_edge(self):
        # the pre-fix chain reader collected the top-level ``relation`` — the
        # REQUESTED row's single outgoing edge — so only the withdraws edge ever
        # appeared for a chain that carries three.
        legacy = D.reader_relation_chain(
            {"relations": [HISTORY_MODE_PAYLOAD["relation"]]})
        assert legacy == [("withdraws", "mem_c", None)]
        assert D.reader_relation_chain(HISTORY_MODE_PAYLOAD) != legacy

    def test_a_read_with_no_mode_or_not_an_object_never_raises(self):
        for payload in (None, "x", 7, [1, 2], {}, {"content": "flat"}):
            assert D.reader_current_read(payload)["has_current"] is False
            assert D.reader_selected_row(payload) is None
            assert D.reader_history_ids(payload) == []
            assert D.reader_history_labels(payload) == []
            assert D.reader_relation_chain(payload) == []
            assert D.reader_history_current_memory_id(payload) == ""

    # ── (c) the static guard ──────────────────────────────────────────────

    def test_no_canonical_read_field_is_obtained_by_recursive_key_search(self):
        src = self._src()
        read_vars = _v3_get_read_vars(src)
        assert _CANONICAL_READ_VARS <= read_vars, (
            "the v3_get read bindings moved; re-point the guard: "
            f"{sorted(read_vars)}")
        offenders = []
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("pick", "find_values") and node.args):
                first = node.args[0]
                if isinstance(first, ast.Name) and first.id in read_vars:
                    offenders.append(
                        f"{node.func.id}({first.id}, ...) line {node.lineno}")
        assert not offenders, (
            "a canonical read field is still obtained by recursive key search, "
            f"which can return a sibling's value first: {offenders}")

    def test_the_new_readers_never_recurse_into_siblings(self):
        src = self._src()
        for name in ("reader_selected_row", "reader_current_read",
                     "reader_history_block", "reader_history_entries",
                     "reader_history_labels", "reader_history_edges",
                     "reader_history_current_memory_id", "reader_history_is_current",
                     "reader_relation_chain", "reader_rendered_content"):
            body = _function_source(src, name)
            for forbidden in ("pick(", "find_values(", "first_key_value("):
                assert forbidden not in body, f"{name} recurses via {forbidden!r}"

    def test_the_current_and_history_checks_read_through_the_nested_readers(self):
        src = self._src()
        for marker in ("cur_read = reader_current_read(cur)",
                       "cur_c_read = reader_current_read(cur_c)",
                       "hist_versions = reader_history_entries(hist)",
                       "hm_present, hm_content = reader_rendered_content(hm)",
                       "wd_hist_ids = reader_history_ids(wd_hist)"):
            assert marker in src, f"the read check does not use {marker!r}"

    # ── (d) the fresh-session recall classification ───────────────────────

    def test_the_recall_classification_separates_environment_from_a_miss(self):
        env_off = D.recall_environment(embedding_service_configured=False,
                                       rows_total=6, rows_with_embedding=0)
        assert env_off["vector_recall_available"] is False
        unavailable = D.classify_recall(
            "", current_content="C", superseded_contents=("A", "B"),
            embeddings_available=env_off["vector_recall_available"])
        assert unavailable["classification"] == D.RECALL_EMBEDDINGS_UNAVAILABLE
        missed = D.classify_recall(
            "", current_content="C", superseded_contents=("A", "B"),
            embeddings_available=True)
        assert missed["classification"] == D.RECALL_MISSED_CURRENT
        assert missed["classification"] != unavailable["classification"]
        recalled = D.classify_recall(
            "current is C", current_content="C", superseded_contents=("A", "B"),
            embeddings_available=False)
        assert recalled["classification"] == D.RECALL_CURRENT_RECALLED

    def test_a_superseded_payload_in_the_block_is_always_reported(self):
        got = D.classify_recall("A is the current plan", current_content="C",
                                superseded_contents=("A", "B"),
                                embeddings_available=False)
        assert got["classification"] == D.RECALL_EMBEDDINGS_UNAVAILABLE
        assert got["superseded_present"] == ["A"], (
            "a superseded payload inside the block was hidden by the "
            "environment classification")

    def test_embeddings_available_needs_both_a_service_and_embedded_rows(self):
        assert D.recall_environment(embedding_service_configured=True, rows_total=6,
                                    rows_with_embedding=6)["vector_recall_available"] is True
        # a configured service with no embedded row cannot serve a vector recall
        assert D.recall_environment(embedding_service_configured=True, rows_total=6,
                                    rows_with_embedding=0)["vector_recall_available"] is False
        # no service at all: nothing can be embedded, whatever the rows say
        assert D.recall_environment(embedding_service_configured=False, rows_total=6,
                                    rows_with_embedding=6)["vector_recall_available"] is False

    def test_the_embedding_service_fact_is_read_from_the_written_profile(self):
        assert D.embedding_service_configured_from_config(
            "storage:\n  embed:\n    endpoint: ''\n    model: ''\n"
            "  rerank:\n    endpoint: ''\n") is False
        assert D.embedding_service_configured_from_config(
            "storage:\n  embed:\n    endpoint: 'http://embed.example/v1'\n") is True
        assert D.embedding_service_configured_from_config("") is False

    def test_the_driver_classifies_the_recall_instead_of_silently_failing(self):
        src = self._src()
        assert "classify_recall(block" in src
        assert "recall_environment(" in src
        assert "fresh_session_recall_classified_embeddings_unavailable" in src
        assert "NOT EVALUABLE BY ENVIRONMENT" in src
        assert "fresh_session_recall_contains_current_version" in src
        assert 'report["checks"]["fresh_session_recall_contains_current_version"]' in src
        assert "vector_recall_available" in src

    # ── the server cleanup fix ────────────────────────────────────────────

    def test_server_b_is_stopped_before_the_residual_process_check(self):
        src = self._src()
        derived = src.index('check("derived_probe_target_committed"')
        stop_b = src.index("stop_b = server_b.stop()")
        residual = src.index('check("no_leftover_server_process"')
        assert derived < stop_b < residual, (
            "server B must stay alive for the derived probe, then be stopped "
            "before the residual-process check")

    def test_a_server_that_cannot_be_stopped_is_reported_not_hidden(self):
        src = self._src()
        assert 'stop_b.get("alive_after_stop") is not True' in src
        assert "server_b_shut_down_gracefully" in src
        assert "cleanup_failed" in src and "alive_after_stop" in src


# ══════════════════════════════════════════════════════════════════════════
# section-7 upgrade baseline fidelity + observed-string truthfulness
# ══════════════════════════════════════════════════════════════════════════

#: Status literals that must NEVER appear as a CONSTANT part of a section-7
#: upgrade check's observed string. A constant claim can contradict the raw
#: entry the run recorded — that is exactly how the literal
#: ``"upgrade --apply rc=0"`` reported success while the raw CLI entry recorded
#: ``returncode: 1``.
_SECTION7_FORBIDDEN_STATUS_LITERALS = (
    "rc=0", "rc=1", "applied=true", "applied=false", "ok=true", "ok=false",
    "unchanged", "stayed", "never appears", "succeeded",
)


def _check_call_source(source: str, name: str) -> str:
    """Exact source of the ``check(<name>, ...)`` call in the driver.

    The driver's ``check`` is a closure inside ``main()``, so it cannot be
    imported; extracting the real AST call and executing it against a recording
    ``check`` exercises the DRIVER's own predicate and observed-string
    construction — not a re-implementation of them.
    """
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "check" and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == name):
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"check({name!r}) is not called anywhere in the driver")


def _run_check_call(source: str, name: str, namespace: dict) -> dict:
    """Run the driver's real ``check(<name>, ...)`` call with fake inputs."""
    recorded: dict = {}

    def fake_check(check_name, ok, detail=""):
        recorded.update(name=check_name, ok=bool(ok), detail=detail)
        return bool(ok)

    ns = dict(namespace)
    ns["check"] = fake_check
    ns.setdefault("truncate", D.truncate)
    eval(compile(_check_call_source(source, name), f"<{name}>", "eval"), ns)
    return recorded


def _section7_upgrade_slice(source: str) -> str:
    """The section-7 upgrade proof: realistic baseline + minimal baseline."""
    start = source.index("prior_install_schema_artifact_is_shipped")
    end = source.index('doctor = run_cli(["doctor", "--static"]')
    return source[start:end]


def _section7_upgrade_check_calls(source: str):
    """(check_name, call_node) for the section-7 upgrade proof checks.

    Selected by LINE RANGE over the whole-file AST: slicing the source by string
    index would cut a string literal in half (the first check name is itself
    inside a ``check("...", ...)`` call), which is not parseable.
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    start_line = next(i for i, line in enumerate(lines, 1)
                      if "prior_install_schema_artifact_is_shipped" in line)
    end_line = next(i for i, line in enumerate(lines, 1)
                    if 'doctor = run_cli(["doctor", "--static"]' in line)
    calls = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "check" and node.args
                and start_line <= node.lineno < end_line):
            name_node = node.args[0]
            if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
                calls.append((name_node.value, node))
    return calls


class TestUpgradeObservedStringTruthfulness:
    """A check's observed string must be DERIVED, never a hardcoded status."""

    def _src(self) -> str:
        return Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")

    def test_a_nonzero_apply_rc_fails_and_reports_the_real_rc(self):
        # Negative control through the driver's own ``upgrade_applied`` call:
        # with rc=1 the check must FAIL and the observed string must carry the
        # REAL rc. The pre-fix literal "upgrade --apply rc=0" hid the failure
        # (the raw CLI entry recorded returncode=1 and applied=false).
        src = self._src()
        failed = _run_check_call(src, "upgrade_applied", {
            "apply_rc": 1, "apply_applied": False,
            "apply_error": 'UndefinedTable("public.qa_pairs")',
            # harmless extras so the PRE-FIX call body also evaluates (RED)
            "run_cli": lambda *a, **k: {"_rc": 1, "result": {"applied": False}},
            "upgrade_dsn": "postgresql://u@127.0.0.1:55432/m01m02e2e_x",
        })
        assert failed["ok"] is False, "a non-zero rc did not fail upgrade_applied"
        assert "rc=1" in failed["detail"], failed["detail"]
        assert "rc=0" not in failed["detail"], (
            f"the observed string claims rc=0 for a real rc=1: {failed['detail']!r}")

    def test_a_zero_apply_rc_with_applied_true_passes(self):
        src = self._src()
        passed = _run_check_call(src, "upgrade_applied", {
            "apply_rc": 0, "apply_applied": True, "apply_error": None,
        })
        assert passed["ok"] is True, passed["detail"]
        assert "rc=0" in passed["detail"]
        assert "applied=True" in passed["detail"]

    def test_section7_upgrade_checks_have_no_hardcoded_status_literals(self):
        src = self._src()
        assert '"upgrade --apply rc=0"' not in src, (
            "the hardcoded rc=0 observed literal is back")
        calls = _section7_upgrade_check_calls(src)
        assert {"upgrade_applied", "prior_install_has_data_and_no_relations_table",
                "minimal_baseline_upgrade_refused_atomically"} <= {
                    name for name, _ in calls}, (
            "the section-7 guard did not find the expected upgrade checks; "
            f"found {sorted(name for name, _ in calls)}")
        offenders: list[str] = []
        for check_name, node in calls:
            detail = node.args[2] if len(node.args) > 2 else None
            if detail is None:
                offenders.append(f"{check_name}: no observed string")
                continue
            if isinstance(detail, ast.Constant):
                offenders.append(
                    f"{check_name}: constant observed string {detail.value!r}")
                continue
            if isinstance(detail, ast.JoinedStr):
                if not any(isinstance(p, ast.FormattedValue) for p in detail.values):
                    offenders.append(
                        f"{check_name}: observed string derives from nothing")
                constant_text = "".join(
                    p.value for p in detail.values
                    if isinstance(p, ast.Constant) and isinstance(p.value, str)).lower()
                hits = [tok for tok in _SECTION7_FORBIDDEN_STATUS_LITERALS
                        if tok in constant_text]
                if hits:
                    offenders.append(
                        f"{check_name}: hardcoded status literal(s) {hits}")
        assert not offenders, (
            "section-7 upgrade checks carry hardcoded/underived observed "
            f"strings: {offenders}")


class TestRealisticUpgradeBaseline:
    """The upgrade proof starts from a REAL v0.2 install, not a toy database."""

    def _src(self) -> str:
        return Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")

    def test_the_baseline_construction_sequence_is_asserted_in_order(self):
        src = self._src()
        section = src[src.index("prior_install_schema_artifact_is_shipped"):
                      src.index("bootstrap_and_upgrade_relations_schema_parity")]
        sequence = [
            '"upgrade-prior-bootstrap"',                      # 1 bootstrap CLI
            "prior_install_bootstrapped_full_base_schema",    # 1 bootstrap check
            "DROP TABLE IF EXISTS {RELATION_TABLE}",          # 2 drop the sidecar
            "DELETE FROM public.schema_versions WHERE version='v0.3'",  # 3 v0.3 row
            "ARRAY['m01m02e2e-upgrade']",                     # 4 the prior row
            "pre_snapshot = prior_row_snapshot(db_upgrade)",  # 5 snapshot
            '"upgrade-dry-run-1"',                            # 6 dry-run
            '"upgrade-apply"',                                # 7 apply
            'check("upgrade_created_relations_table"',        # 8 verify
        ]
        positions = []
        for marker in sequence:
            idx = section.find(marker)
            assert idx != -1, (
                f"the realistic-baseline sequence no longer contains {marker!r} — "
                "a future edit must not silently go back to a one-table baseline")
            positions.append(idx)
        assert positions == sorted(positions), (
            "the realistic-baseline construction steps are out of order: "
            f"{list(zip(sequence, positions))}")

    def test_the_baseline_bootstraps_the_full_schema_then_removes_the_sidecar(self):
        section = _section7_upgrade_slice(self._src())
        # bootstrap the full base schema into the upgrade database...
        assert '["bootstrap", "--target", upgrade_dsn]' in section
        # ...then remove EXACTLY the M01 sidecar (table AND schema-version row)
        assert "DROP TABLE IF EXISTS {RELATION_TABLE}" in section
        assert "DELETE FROM public.schema_versions WHERE version='v0.3'" in section
        # and the baseline check verifies the facts against the real catalog
        assert "prior_install_has_data_and_no_relations_table" in section

    def test_the_baseline_check_reads_schema_versions_not_only_tables(self):
        section = _section7_upgrade_slice(self._src())
        assert "SELECT version FROM public.schema_versions" in section, (
            "the baseline check no longer proves the v0.3 row was removed")

    def test_the_minimal_baseline_refusal_is_a_separate_asserted_path(self):
        src = self._src()
        section = _section7_upgrade_slice(src)
        assert "minimal_baseline_is_explicit_memories_only" in section
        assert "minimal_baseline_upgrade_refused_atomically" in section
        assert '"upgrade-apply-minimal"' in section
        assert "CREATE EXTENSION IF NOT EXISTS vector" in section, (
            "the minimal baseline must still be built from explicit_memories.sql "
            "+ the vector extension")
        # a refusing result passes...
        refused = _run_check_call(src, "minimal_baseline_upgrade_refused_atomically", {
            "min_rc": 1, "min_applied": False, "min_relations_created": False,
            "min_versions_after": set(), "min_qa_cols": set(),
            "min_error": 'UndefinedTable("public.qa_pairs")',
        })
        assert refused["ok"] is True, refused["detail"]
        assert "rc=1" in refused["detail"]
        assert "applied=False" in refused["detail"]
        # ...and an APPLIED upgrade on the minimal baseline is NOT a refusal.
        applied = _run_check_call(src, "minimal_baseline_upgrade_refused_atomically", {
            "min_rc": 0, "min_applied": True, "min_relations_created": True,
            "min_versions_after": {"v0.3"}, "min_qa_cols": {"source_id"},
            "min_error": None,
        })
        assert applied["ok"] is False, (
            "an applied upgrade on the minimal baseline was accepted as a refusal")


# ══════════════════════════════════════════════════════════════════════════
# the history label check must match the chain's ACTUAL phase
#
# The dedicated A'->B'->C' history read happens BEFORE C' is withdrawn, so its
# terminal version is CURRENT; only the read taken AFTER the withdrawal may
# report the terminal as withdrawn. Both phases are asserted as an exact,
# index-aligned (ids, labels, statuses) triple.
# ══════════════════════════════════════════════════════════════════════════

#: A -> B -> C BEFORE C is withdrawn: terminal is current/active.
_PRE_WITHDRAW_LABELS = ["superseded", "superseded", "current"]
_PRE_WITHDRAW_STATUSES = ["archived", "archived", "active"]
#: A -> B -> C AFTER C is withdrawn: no current version, terminal withdrawn.
_POST_WITHDRAW_LABELS = ["superseded", "superseded", "withdrawn"]
_POST_WITHDRAW_STATUSES = ["archived", "archived", "archived"]
_PHASE_IDS = ["a", "b", "c"]


def _history_phase_namespace(ids, labels, statuses) -> dict:
    """Driver locals the two history-phase checks read, bound to fakes."""
    return {
        "wd_hist_ids": list(ids),
        "wd_hist_labels": list(labels),
        "wd_hist_statuses": list(statuses),
        "wd_hist_after_ids": list(ids),
        "wd_hist_after_labels": list(labels),
        "wd_hist_after_statuses": list(statuses),
        "wd_a": "a", "wd_b": "b", "wd_c": "c",
        # present for the RED (pre-repair) call shape; the repaired shape reads
        # the aligned locals above.
        "wd_hist_after": {},
        "reader_history_labels": D.reader_history_labels,
        "history_versions_match_exact":
            getattr(D, "history_versions_match_exact", None),
    }


class TestHistoryPhaseLabelExactness:
    """The pre-withdraw check asserts the CURRENT phase, the post one WITHDRAWN.

    ``_run_check_call`` executes the DRIVER's real ``check(<name>, ...)``
    predicate, so these assertions pin the driver's own phase predicate rather
    than a re-implementation of it.
    """

    def _src(self) -> str:
        return Path(D.__DRIVER_PATH__).read_text(encoding="utf-8")

    def _run(self, check_name, ids, labels, statuses) -> dict:
        return _run_check_call(
            self._src(), check_name,
            _history_phase_namespace(ids, labels, statuses))

    # ── the pre-withdraw read is CURRENT, never withdrawn ─────────────────

    def test_the_pre_withdraw_check_accepts_the_current_phase(self):
        recorded = self._run("dedicated_history_labels_are_accurate",
                             _PHASE_IDS, _PRE_WITHDRAW_LABELS,
                             _PRE_WITHDRAW_STATUSES)
        assert recorded["ok"] is True, recorded["detail"]

    def test_the_pre_withdraw_check_refuses_a_withdrawn_terminal(self):
        # C is still active here: a predicate that demands labels[-1] ==
        # 'withdrawn' would wrongly accept this withdrawn-terminal shape.
        recorded = self._run("dedicated_history_labels_are_accurate",
                             _PHASE_IDS,
                             ["superseded", "superseded", "withdrawn"],
                             ["archived", "archived", "archived"])
        assert recorded["ok"] is False, (
            "a pre-withdraw chain was accepted as withdrawn: " + recorded["detail"])

    def test_the_pre_withdraw_check_refuses_a_misaligned_status(self):
        recorded = self._run("dedicated_history_labels_are_accurate",
                             _PHASE_IDS, _PRE_WITHDRAW_LABELS,
                             ["archived", "active", "archived"])
        assert recorded["ok"] is False, recorded["detail"]

    def test_the_pre_withdraw_check_does_not_demand_a_withdrawn_label(self):
        source = _check_call_source(self._src(),
                                    "dedicated_history_labels_are_accurate")
        assert '"withdrawn"' not in source, (
            "the pre-withdraw check still expects a withdrawn terminal: " + source)

    # ── the post-withdraw read is WITHDRAWN, never still current ──────────

    def test_the_post_withdraw_check_accepts_the_withdrawn_phase(self):
        recorded = self._run("dedicated_history_after_withdraw_keeps_full_chain",
                             _PHASE_IDS, _POST_WITHDRAW_LABELS,
                             _POST_WITHDRAW_STATUSES)
        assert recorded["ok"] is True, recorded["detail"]

    def test_the_post_withdraw_check_refuses_a_still_current_terminal(self):
        # after the withdrawal the terminal must NOT still be current
        recorded = self._run("dedicated_history_after_withdraw_keeps_full_chain",
                             _PHASE_IDS, _PRE_WITHDRAW_LABELS,
                             _PRE_WITHDRAW_STATUSES)
        assert recorded["ok"] is False, (
            "a still-current chain was accepted after the withdrawal: "
            + recorded["detail"])

    def test_the_post_withdraw_check_refuses_a_misaligned_chain(self):
        recorded = self._run("dedicated_history_after_withdraw_keeps_full_chain",
                             _PHASE_IDS, _POST_WITHDRAW_LABELS,
                             ["archived", "archived", "active"])
        assert recorded["ok"] is False, recorded["detail"]

    def test_the_post_withdraw_check_is_not_a_bare_id_set(self):
        source = _check_call_source(
            self._src(), "dedicated_history_after_withdraw_keeps_full_chain")
        assert "set(" not in source, (
            "the post-withdraw check is still a set/superset test: " + source)
        assert '"withdrawn"' in source, (
            "the post-withdraw check does not assert the withdrawn terminal: "
            + source)
