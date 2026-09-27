"""One effective config truth for the WHOLE core, not just for one tool call.

Proven leak this pins (found in the B02 embedding-401 round):

    a process booted on profile ``b02-canary`` (own DB, own basePath)
    loaded 349 topics that belong to the ``default`` profile, because the
    core-internal recall path resolves its config with a bare
    ``resolve_config()`` — and a bare call means profile ``"default"``.
    ``topic_recall._get_pg_conn()`` really connected to the default profile's
    database, and the foreign topic matrix was then cached into the booted
    profile's data dir.

The rule (host contract §6): the tool call, the search, the embedding and the
recall pool must all read ONE effective config — the profile the process
booted. A leaf may not quietly resolve ``default``.

These tests boot a REAL core on a non-default profile and assert what the bare
resolvers return. No PG server is required: the connect call is intercepted.
"""
from __future__ import annotations

import pathlib

import pytest

PROFILE = "b02-amb"
BOOTED_PG_PORT = 59998


def _write_profile(hh: pathlib.Path, base: pathlib.Path) -> None:
    pdir = hh / ".v3-core" / "profiles" / PROFILE
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "config.yaml").write_text(
        "basePath: {base}\n"
        "storage:\n"
        "  pg:\n"
        "    host: 127.0.0.1\n"
        "    port: {port}\n"
        "    database: ambprobe\n"
        "    user: v3user\n"
        "  embed:\n"
        "    endpoint: https://api.siliconflow.cn/v1/embeddings\n"
        "    model: BAAI/bge-m3\n"
        "    dim: 1024\n"
        "    api_key: ${{env:V3CORE_EMBED_API_KEY}}\n"
        "observer:\n"
        "  enabled: false\n"
        "e1:\n"
        "  enabled: false\n".format(base=str(base).replace("\\", "/"), port=BOOTED_PG_PORT),
        encoding="utf-8",
    )
    (pdir / ".env").write_text(
        "V3CORE_PG_PASSWORD=amb-probe-pw\n"
        "V3CORE_EMBED_API_KEY=amb-probe-embed-key-0123456789abcdef\n",
        encoding="utf-8",
    )


@pytest.fixture()
def booted_core(tmp_path, monkeypatch):
    hh = tmp_path / "hermes-home"
    _write_profile(hh, tmp_path / "data")
    monkeypatch.setenv("HERMES_HOME", str(hh))
    # Never touch a real server: record what the recall path would connect to.
    import psycopg2

    seen: dict = {}

    def _fake_connect(**kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(psycopg2, "connect", _fake_connect)

    from v3core import V3Core

    core = V3Core(profile=PROFILE, hermes_home=str(hh))
    core.initialize()
    try:
        yield hh, seen, tmp_path / "data"
    finally:
        try:
            core.shutdown(timeout=1.0)
        except Exception:  # noqa: BLE001
            pass


def test_bare_resolve_config_follows_the_booted_profile(booted_core):
    """``resolve_config()`` with no profile is AMBIENT, not 'default'."""
    from v3core.config import resolve_config

    bare = resolve_config()
    assert bare.pg.port == BOOTED_PG_PORT, (
        "a bare resolve_config() left the booted profile: "
        f"port={bare.pg.port} db={bare.pg.database}"
    )
    assert bare.pg.database == "ambprobe"


def test_bare_data_dir_follows_the_booted_profile(booted_core):
    """The recall cache dir must be the booted profile's data dir."""
    from v3core.config import _resolve_data_dir

    _, _, base = booted_core
    assert _resolve_data_dir().resolve() == base.resolve(), (
        f"data dir leaked away from the booted profile: {_resolve_data_dir()}"
    )


def test_recall_pool_reads_the_booted_database(booted_core):
    """``topic_recall._get_pg_conn()`` must target the booted profile's DB."""
    from v3core.topic_recall import _get_pg_conn

    _, seen, _ = booted_core
    _get_pg_conn()
    assert seen.get("port") == BOOTED_PG_PORT, (
        f"recall path resolved another profile's database: {seen}"
    )
    assert seen.get("dbname") == "ambprobe"


def test_no_booted_core_still_means_the_default_profile(tmp_path, monkeypatch):
    """Control: with nothing booted, a bare call keeps the legacy meaning.

    Nothing is bound, so 'default' is still resolved — it must NOT quietly pick
    up the temp profile that this test also wrote to disk.
    """
    hh = tmp_path / "hermes-home"
    _write_profile(hh, tmp_path / "data")
    monkeypatch.setenv("HERMES_HOME", str(hh))
    # The default profile in this sandbox ships no PG password; supply one so the
    # fail-fast contract does not mask what we are actually asserting.
    monkeypatch.setenv("V3CORE_PG_PASSWORD", "control-pw")

    from v3core.config import resolve_config

    cfg = resolve_config()
    base = str(getattr(cfg, "base_path", "") or getattr(cfg, "basePath", "")).replace("\\", "/").lower()
    assert "b02-amb" not in base, f"an unbound process resolved a foreign profile: {base}"
    assert cfg.pg.port != BOOTED_PG_PORT
