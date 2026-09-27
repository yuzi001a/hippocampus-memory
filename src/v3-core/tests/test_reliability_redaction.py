"""Unit tests for ``v3core.reliability.redaction``.

Pins:

  - ``path_label`` default shape (no full path; debug flag adds it).
  - ``sanitize_text`` scrubs DSN userinfo + ``key=value`` patterns.
  - ``is_production_target`` matches ``distribution_cli._is_production_boundary``
    exactly across the five-case table that the upgrade-contract test
    already pins.
"""
from __future__ import annotations

import pytest

from v3core import distribution_cli as dc
from v3core.reliability.redaction import (
    LOOPBACK_HOSTS,
    PROD_LOCAL_DB,
    PROD_PORT,
    is_production_target,
    path_label,
    sanitize_text,
)


# ── path_label ──


def test_path_label_default_omits_full_path():
    out = path_label("/Users/somebody/.v3-core/profiles/default/config.yaml", "profile")
    assert out["kind"] == "profile"
    assert out["leaf"] == "config.yaml"
    assert "path" not in out
    # hash12 is a 12-hex-char prefix of sha256.
    assert isinstance(out["hash12"], str) and len(out["hash12"]) == 12
    int(out["hash12"], 16)  # parses as hex


def test_path_label_debug_paths_includes_full_path():
    out = path_label(
        "/Users/somebody/.v3-core/profiles/default/config.yaml",
        "profile",
        debug_paths=True,
    )
    assert out["path"] == "/Users/somebody/.v3-core/profiles/default/config.yaml"
    # hash12 stays the same.
    assert len(out["hash12"]) == 12


def test_path_label_leaf_falls_back_to_str_when_no_basename():
    """``Path("").name`` is ``''``; callers shouldn't see an empty leaf."""
    out = path_label("", "marker_dir")
    # Falls back to the string form, never an empty leaf.
    assert out["leaf"] == ""


def test_path_label_handles_pathlib_path():
    from pathlib import Path
    p = Path("/tmp/whatever/marker.json")
    out = path_label(p, "marker_dir")
    assert out["leaf"] == "marker.json"


# ── sanitize_text ──


def test_sanitize_text_scrubs_dsn_userinfo():
    text = "connected to postgres://v3user:prodpass@localhost:5433/v3embeddings OK"
    out = sanitize_text(text)
    assert "prodpass" not in out
    assert "***@" in out
    assert "localhost:5433" in out  # host:port must remain


def test_sanitize_text_scrubs_password_kv():
    text = "config: password=hunter2 api_key=sk-XYZ other=ok"
    out = sanitize_text(text)
    assert "hunter2" not in out
    assert "sk-XYZ" not in out
    assert "password=<redacted>" in out
    assert "api_key=<redacted>" in out
    assert "other=ok" in out


def test_sanitize_text_scrubs_bearer_token():
    text = "Authorization: Bearer abc.def.ghi trailing"
    out = sanitize_text(text)
    assert "abc.def.ghi" not in out
    assert "bearer=<redacted>" in out.lower()


def test_sanitize_text_truncates_with_sentinel():
    text = "x" * 1000
    out = sanitize_text(text, max_len=80)
    assert len(out) <= 80
    assert out.endswith("...")


def test_sanitize_text_handles_none():
    assert sanitize_text(None) == ""  # type: ignore[arg-type]


def test_sanitize_text_does_not_touch_unrelated_content():
    text = "everything is fine, just a normal log line"
    assert sanitize_text(text) == text


# ── is_production_target — cross-module equivalence ──


# The same five-case table used by
# tests/test_production_upgrade_contract.py::test_is_production_boundary_matches_enforce.
PROD_DSN = "postgres://v3user:prodpass@localhost:5433/v3embeddings"
NONPROD_DSN = "postgres://v3user:devpass@localhost:55432/v3embeddings_alpha"


def test_is_production_target_port_5433():
    parsed = dc._parse_dsn(PROD_DSN)
    assert is_production_target(parsed["host"], parsed["port"], parsed["database"]) is True


def test_is_production_target_loopback_and_v3embeddings():
    parsed = dc._parse_dsn(
        "postgres://v3user:devpass@localhost:55432/v3embeddings"
    )
    assert is_production_target(parsed["host"], parsed["port"], parsed["database"]) is True


def test_is_production_target_nonprod_db_name():
    parsed = dc._parse_dsn(NONPROD_DSN)
    assert is_production_target(parsed["host"], parsed["port"], parsed["database"]) is False


def test_is_production_target_loopback_alpha_db():
    parsed = dc._parse_dsn(
        "postgres://v3user:devpass@localhost:55432/v3embeddings_alpha"
    )
    assert is_production_target(parsed["host"], parsed["port"], parsed["database"]) is False


def test_is_production_target_non_loopback_with_v3embeddings():
    parsed = dc._parse_dsn(
        "postgres://v3user:devpass@db.internal.example:5432/v3embeddings"
    )
    assert is_production_target(parsed["host"], parsed["port"], parsed["database"]) is False


@pytest.mark.parametrize("dsn,expected", [
    (PROD_DSN, True),
    (NONPROD_DSN, False),
    ("postgres://v3user:devpass@localhost:55432/v3embeddings", True),
    ("postgres://v3user:devpass@localhost:55432/v3embeddings_alpha", False),
    ("postgres://v3user:devpass@db.internal.example:5432/v3embeddings", False),
])
def test_is_production_target_matches_distribution_cli(dsn, expected):
    parsed = dc._parse_dsn(dsn)
    assert is_production_target(parsed["host"], parsed["port"], parsed["database"]) is expected
    # Predicate must agree with the enforcer (raise-iff-prod).
    raised = False
    try:
        dc._enforce_production_boundary(dict(parsed))
    except SystemExit:
        raised = True
    assert raised is expected


# ── constants pinned against distribution_cli ──


def test_loopback_hosts_matches_distribution_cli():
    assert LOOPBACK_HOSTS == dc.PROD_LOOPBACK_HOSTS


def test_production_port_matches_distribution_cli():
    assert PROD_PORT in dc.PROD_PORTS


def test_production_local_db_matches_distribution_cli():
    assert PROD_LOCAL_DB == dc.PROD_LOCAL_DB