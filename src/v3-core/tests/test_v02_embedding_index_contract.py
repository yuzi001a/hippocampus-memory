from __future__ import annotations


def test_upgrade_combines_sidecar_before_commit():
    from v3core.distribution_cli import _upgrade_load_combined_sql

    sql, evidence = _upgrade_load_combined_sql(include_qa_chunks_artifact=True)
    assert evidence["qa_embedding_chunks_sql_present"] is True
    assert evidence["qa_embedding_chunks_spliced_before_commit"] is True
    assert sql.index("CREATE TABLE IF NOT EXISTS public.qa_embedding_chunks") < sql.rindex("COMMIT;")
    statements = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    ).upper()
    assert "DROP TABLE" not in statements
    assert "TRUNCATE" not in statements
    assert "DELETE FROM" not in statements


def test_runtime_provenance_reports_path_and_config_without_secrets():
    from v3core import doctor_full

    doctor_full._PROVENANCE_CACHE = None
    report = doctor_full._runtime_provenance()
    assert "v3core_file" in report
    assert "distribution_version" in report
    assert "sys_path" in report
    assert "pth_files" in report
    assert "config_path" in report
    rendered = repr(report).lower()
    assert "api_key" not in rendered
    assert "password" not in rendered


def test_source_ingest_does_not_drop_normal_user_assistant_messages_for_provider_limits():
    """Provider limits belong to derived representations, not raw history."""
    from pathlib import Path

    import v3core

    src = Path(v3core.__file__).read_text(encoding="utf-8")
    assert "RIVER_MAX_MESSAGE_CHARS" not in src
    assert "len(content) > 24000" not in src
    assert "embed_chunks" in src


def test_fresh_bootstrap_reaches_the_current_schema_level():
    """A brand-new install must not fail its own doctor.

    `upgrade_v0_2.sql` creates `public.schema_versions` (the ledger
    `doctor --full` keys off) and carries its own BEGIN/COMMIT, so it cannot ride
    the ALPHA_BOOTSTRAP_INCLUDE marker. If the fresh bootstrap path stops applying
    it, every new user fails `doctor --full` with `schema_version: fail`.

    Real evidence for the fixed behaviour: `hippocampus bootstrap` then
    `doctor --full` against a brand-new database returns
    `ok=12 fail=0 skip=3 warn=1` with `schema_version: ok (v0.2 row present)`.
    """
    from pathlib import Path

    import v3core

    src = Path(v3core.__file__).parent.joinpath("distribution_cli.py").read_text(
        encoding="utf-8")
    body = src.split("def _bootstrap_apply_sql", 1)[1].split("\ndef ", 1)[0]
    assert '_package_sql("upgrade_v0_2.sql")' in body, (
        "fresh bootstrap must apply upgrade_v0_2.sql, otherwise schema_versions "
        "never exists and doctor --full fails on a clean install"
    )
    assert 'report["upgrade_applied"]' in body
