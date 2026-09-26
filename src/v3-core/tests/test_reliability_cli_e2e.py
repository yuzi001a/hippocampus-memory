# -*- coding: utf-8 -*-
"""DISPATCH-C2 §2 — CLI end-to-end test for reliability (lab-backed).

Drives the real ``hippocampus.exe`` entry point as a subprocess and
asserts the JSON output + exit codes for ``health``, ``diagnose``, and
``repair`` against the disposable lab DB.

Gate:

  * ``HIPPOCAMPUS_RELIABILITY_TEST_DSN`` must be set (otherwise skip —
    we cannot exercise the lab without the env var being present).
  * Lab config file ``C:/Users/servi/.v3-core-lab/profiles/default/config.yaml``
    must exist (otherwise skip — the CLI loads it via ``V3CORE_CONFIG``).

The lab config is wired through ``V3CORE_CONFIG`` so the CLI picks up
the lab PG block (host/port/user/database) rather than the production
``v3embeddings`` database on ``localhost:5433``.

This test NEVER writes to the database — ``repair`` is exercised in
``--dry-run`` mode and the ``--apply`` branch is asserted to be
hard-rejected at the CLI layer (rc 2, ``REPAIR_APPLY_NOT_IMPLEMENTED_IN_V1``).

Run with lab env::

    cd src/v3-core
    HIPPOCAMPUS_RELIABILITY_TEST_DSN="host=localhost port=55462 dbname=v3embeddings_lab user=v3user password=lab-local-only" \\
    ../../.venv/Scripts/python.exe -m pytest tests/test_reliability_cli_e2e.py -v

Without the env var the module is skipped at collection time.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────
# Gate — same pattern as the PG-integration suite.
# ─────────────────────────────────────────────────────────────────────

_TEST_DSN = os.environ.get("HIPPOCAMPUS_RELIABILITY_TEST_DSN")
if not _TEST_DSN:
    pytest.skip(
        "HIPPOCAMPUS_RELIABILITY_TEST_DSN not set — opt-in disposable-PG "
        "CLI e2e test is skipped in CI",
        allow_module_level=True,
    )

# Safety refusal — same rules as the integration test.  We do the
# check here so a misconfigured CI cannot accidentally run against
# production through this path either.
def _parse_dsn_safely(dsn: str) -> dict[str, str]:
    try:
        from psycopg2.extensions import parse_dsn as _psycopg_parse_dsn
        parsed = _psycopg_parse_dsn(dsn)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items() if v is not None}
    except Exception:
        pass
    out: dict[str, str] = {}
    for token in dsn.split():
        if "=" not in token:
            continue
        k, _, v = token.partition("=")
        out[k.strip()] = v.strip()
    return out


_PARSED = _parse_dsn_safely(_TEST_DSN)
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "localhost.localdomain"}
if _PARSED.get("port") == "5433":
    pytest.skip(
        "HIPPOCAMPUS_RELIABILITY_TEST_DSN targets port 5433 (production). "
        "Refusing to run.",
        allow_module_level=True,
    )
if _PARSED.get("host", "").lower() not in _LOOPBACK_HOSTS:
    pytest.skip(
        f"HIPPOCAMPUS_RELIABILITY_TEST_DSN host={_PARSED.get('host')!r} "
        f"is not loopback. Refusing to run.",
        allow_module_level=True,
    )


# ─────────────────────────────────────────────────────────────────────
# Resolve the CLI executable and lab config path.  Both must exist
# before the test runs.
# ─────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests -> v3-core -> src -> repo
CLI_EXE = REPO_ROOT / ".venv" / "Scripts" / "hippocampus.exe"
LAB_CONFIG = Path(r"C:/Users/servi/.v3-core-lab/profiles/default/config.yaml")

if not CLI_EXE.exists():
    pytest.skip(
        f"hippocampus CLI not found at {CLI_EXE} — cannot run e2e",
        allow_module_level=True,
    )
if not LAB_CONFIG.exists():
    pytest.skip(
        f"lab config not found at {LAB_CONFIG} — cannot run e2e",
        allow_module_level=True,
    )


# ─────────────────────────────────────────────────────────────────────
# Subprocess helper.
# ─────────────────────────────────────────────────────────────────────

def _run_cli(*args: str, timeout: float = 30.0):
    """Run the hippocampus CLI with the lab config wired through env.

    Returns ``(returncode, stdout, stderr)``.  ``--allow-production-read``
    is always passed because the CLI uses it to opt into PG reads; the
    lab PG is non-production so this is exactly what the contract asks
    for.
    """
    env = os.environ.copy()
    env["V3CORE_CONFIG"] = str(LAB_CONFIG)
    # Pass the DSN through too — some build paths read it directly.
    env["HIPPOCAMPUS_RELIABILITY_TEST_DSN"] = _TEST_DSN
    full = [str(CLI_EXE), *args, "--allow-production-read"]
    proc = subprocess.run(
        full,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_json_lenient(text: str) -> dict:
    """Parse JSON even if the binary prints trailing blocks (log lines,
    warnings, etc.) after the JSON object.  Tries ``json.loads`` first;
    falls back to ``raw_decode`` on the prefix up to the closing brace."""
    text = text.strip()
    if not text:
        raise AssertionError("CLI stdout was empty — no JSON to parse")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        obj, _end = decoder.raw_decode(text)
        return obj


# ─────────────────────────────────────────────────────────────────────
# 1) health --json
# ─────────────────────────────────────────────────────────────────────

def test_cli_health_json():
    """``hippocampus health --json``: rc in {0,1,2}; stdout is JSON with
    the contract top-level keys (``schema_version``, ``overall``,
    ``checks``)."""
    rc, stdout, stderr = _run_cli("health", "--json")
    assert rc in (0, 1, 2), f"health --json rc={rc}, expected in {{0,1,2}}; stderr={stderr!r}"

    payload = _parse_json_lenient(stdout)
    assert isinstance(payload, dict), f"health payload not a dict: {type(payload).__name__}"

    # Top-level contract fields (DESIGN §3).
    for key in ("schema_version", "overall", "checks"):
        assert key in payload, f"health JSON missing top-level key {key!r}"

    assert payload["schema_version"] == "1"
    assert payload["overall"] in {"healthy", "degraded", "unhealthy"}
    assert isinstance(payload["checks"], list)
    assert payload["checks"], "health report must contain at least one check"

    # Regression (production canary 2026-09-19): the CLI must actually
    # connect to the configured PG target. The earlier build_service bug
    # (missing pg_connect wiring + password pre-redaction) surfaced as
    # storage reachable=false with error "no_connection".
    storage = payload.get("storage", {})
    assert storage.get("reachable") is True, (
        f"CLI health could not reach the configured PG target: {storage!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# 2) diagnose --json
# ─────────────────────────────────────────────────────────────────────

def test_cli_diagnose_json():
    """``hippocampus diagnose --json``: rc in {0,1,2}; ``issues`` is a
    list (possibly empty)."""
    rc, stdout, stderr = _run_cli("diagnose", "--json")
    assert rc in (0, 1, 2), f"diagnose --json rc={rc}, expected in {{0,1,2}}; stderr={stderr!r}"

    payload = _parse_json_lenient(stdout)
    assert isinstance(payload, dict)
    assert "issues" in payload, "diagnose payload missing 'issues'"
    assert isinstance(payload["issues"], list)


# ─────────────────────────────────────────────────────────────────────
# 3) repair --dry-run --json
# ─────────────────────────────────────────────────────────────────────

def test_cli_repair_dry_run_json():
    """``hippocampus repair --dry-run --json``: rc in {0,1,2};
    ``actions`` is a list; ``dry_run`` is True.  The lab DB is in a
    healthy state so ``actions`` may be empty."""
    rc, stdout, stderr = _run_cli("repair", "--dry-run", "--json")
    assert rc in (0, 1, 2), f"repair --dry-run rc={rc}, expected in {{0,1,2}}; stderr={stderr!r}"

    payload = _parse_json_lenient(stdout)
    assert isinstance(payload, dict)
    assert "actions" in payload, "repair payload missing 'actions'"
    assert isinstance(payload["actions"], list)
    assert payload.get("dry_run") is True, (
        f"repair --dry-run must emit dry_run=True; got {payload.get('dry_run')!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# 4) repair --apply must be refused with rc 2 + error code.
# ─────────────────────────────────────────────────────────────────────

def test_cli_repair_apply_refused():
    """``hippocampus repair --apply`` must exit 2 with
    ``REPAIR_APPLY_NOT_IMPLEMENTED_IN_V1``.  Per DESIGN §9 there is no
    write path — the rejection is enforced BEFORE the service runs, so
    we don't need to verify zero DB writes here (the rc 2 is the gate).

    We DO additionally verify that no INSERT/UPDATE/DELETE was issued,
    but only via the output contract — the CLI exits 2 immediately on
    --apply without constructing a service."""
    rc, stdout, stderr = _run_cli("repair", "--apply", "--json")
    assert rc == 2, (
        f"repair --apply must exit 2, got {rc}; stdout={stdout!r} "
        f"stderr={stderr!r}"
    )
    combined = (stdout or "") + (stderr or "")
    assert "REPAIR_APPLY_NOT_IMPLEMENTED_IN_V1" in combined, (
        f"repair --apply must surface REPAIR_APPLY_NOT_IMPLEMENTED_IN_V1; "
        f"got combined={combined!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# 5) secret scan — outputs must never echo lab DSN credentials or the
#    placeholder string from the config.
# ─────────────────────────────────────────────────────────────────────

_FORBIDDEN_TOKENS = (
    "lab-local-only",
    "lab-placeholder-not-a-secret",
)


def test_cli_outputs_have_no_secrets():
    """All three commands' stdout+stderr must not contain the lab DSN
    password or any api-key placeholder from the lab config."""
    leaks: list[str] = []
    for cmd_args in (["health", "--json"], ["diagnose", "--json"], ["repair", "--dry-run", "--json"]):
        rc, stdout, stderr = _run_cli(*cmd_args)
        combined = (stdout or "") + "\n" + (stderr or "")
        for tok in _FORBIDDEN_TOKENS:
            if tok in combined:
                leaks.append(f"{' '.join(cmd_args)}: leaked token {tok!r}")

    assert not leaks, "CLI output leaked secrets: " + "; ".join(leaks)


# ─────────────────────────────────────────────────────────────────────
# 6) --help smoke for the three subcommands.
# ─────────────────────────────────────────────────────────────────────

def test_cli_help_smoke():
    """Each of the three subcommands must respond to ``--help`` with rc 0."""
    for sub in ("health", "diagnose", "repair"):
        rc, stdout, stderr = _run_cli(sub, "--help")
        assert rc == 0, f"{sub} --help rc={rc}, expected 0; stderr={stderr!r}"
        # argparse writes help text to stdout (not stderr). Just check
        # the subcommand name appears somewhere in the output.
        assert sub in (stdout + stderr), (
            f"{sub} --help did not mention its own name in stdout/stderr"
        )
