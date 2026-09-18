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


def test_river_size_guard_is_single_sourced_and_not_silent():
    """Static contract for the message-river size guard.

    The guard is a pre-existing product-data decision, so this test does NOT
    assert the bound is right — it asserts the two properties that made the
    behaviour dangerous: it must be single-sourced (no stray literals that drift
    from the constant) and it must not drop a message silently.

    Real end-to-end evidence for what a trip costs lives in
    reports/v0.2-first-user-release/chunk_canary.py (an oversized assistant
    answer never reaches conversation_stream, and the pending QA is flushed with
    an EMPTY answer plus a question-only embedding).
    """
    import re
    from pathlib import Path

    import v3core

    assert v3core.RIVER_MAX_MESSAGE_CHARS == 24000

    src = Path(v3core.__file__).read_text(encoding="utf-8")
    # the guard compares against the constant, never a bare literal
    assert "len(content) > RIVER_MAX_MESSAGE_CHARS" in src
    assert not re.search(r"len\(content\)\s*>\s*24000\b", src)

    # and the drop branch logs before it drops
    guard = src.split("len(content) > RIVER_MAX_MESSAGE_CHARS", 1)[1]
    branch = guard.split("continue", 1)[0]
    assert "logger.warning" in branch, "oversized-message drop must not be silent"


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
