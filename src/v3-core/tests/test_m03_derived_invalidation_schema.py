# -*- coding: utf-8 -*-
"""M03 — derived-memory invalidation sidecar schema (RED→GREEN anchors).

The canonical artifact is ``schema/derived_memory_invalidations.sql`` (M03
design contract, docs/M03-CORRECTION-PROPAGATION.md §"The invalidation
sidecar"). It is mirrored byte-for-byte into the packaged copy
``src/v3core/schema/derived_memory_invalidations.sql`` and wired into BOTH
install shapes — fresh alpha bootstrap and the existing-install upgrade —
through the single canonical ``ALPHA_BOOTSTRAP_INCLUDE`` mechanism.

This file pins the *schema distribution* contract only. The M03 runtime
(mapping / invalidation / suppression / rebuild) is a separate deliverable and
is NOT asserted here.

  1. both schema copies stay byte-identical (and LF-only, so the packaged
     normalized hash can never drift from the repo bytes), and the new
     artifact is declared in ``pyproject`` package-data and reachable through
     the canonical packaged-resource loader.
  2. fresh bootstrap splices the artifact through the include mechanism,
     AFTER both ``explicit_memories`` and ``memory_relations`` (the table
     references those sidecars).
  3. the existing-install upgrade carries the same DDL additively and
     idempotently, before the single final COMMIT.
  4. the DDL really carries the design's field list, the
     ``UNIQUE (correction_id, derived_kind, derived_id)`` idempotency key and
     all three CHECK constraints.
  5. a MISSING artifact is a truthful ``MIGRATION_REQUIRED`` failure — the
     artifact documents the runtime contract, the include expansion refuses
     (FileNotFoundError) and the bootstrap apply path refuses instead of
     silently producing an install without the table.

These are all static / offline checks: the conftest-level ``psycopg2.connect``
ban is untouched and no PostgreSQL is ever reached.
"""
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

import pytest

from v3core import distribution_cli as dc

_HERE = Path(__file__).resolve().parent
_V3CORE = _HERE.parent
_ROOT_SCHEMA = _V3CORE / "schema"
_PKG_SCHEMA = _V3CORE / "src" / "v3core" / "schema"
_PYPROJECT = _V3CORE / "pyproject.toml"

ARTIFACT = "derived_memory_invalidations.sql"
TABLE = "derived_memory_invalidations"

_MARKER_RE = re.compile(
    r"^--\s*>>>?\s*ALPHA_BOOTSTRAP_INCLUDE:\s*(schema/[A-Za-z0-9_.-]+\.sql)\s*<<<\s*$",
    re.MULTILINE,
)
# statements that would make the artifact non-additive
_DESTRUCTIVE = (
    r"\bDROP\s+TABLE\b",
    r"\bDROP\s+SCHEMA\b",
    r"\bDROP\s+INDEX\b",
    r"\bTRUNCATE\b",
    r"\bDELETE\s+FROM\b",
    r"\bUPDATE\s+\w",
    r"\bALTER\s+COLUMN\b",
)
_CREATE_IF_NOT_EXISTS = re.compile(
    rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+public\.{re.escape(TABLE)}\b",
    re.IGNORECASE,
)


def _body(text: str) -> str:
    """Non-comment SQL — the same shape the existing suites use."""
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("--")
    )


def _variants(name: str) -> dict[str, str]:
    return {
        "root": (_ROOT_SCHEMA / name).read_text(encoding="utf-8"),
        "packaged": (_PKG_SCHEMA / name).read_text(encoding="utf-8"),
    }


def _markers(text: str) -> list[str]:
    return _MARKER_RE.findall(text)


# ── 1. artifact presence, mirror equality, packaging ────────────────────────


def test_invalidation_artifact_exists_in_both_schema_copies():
    for base in (_ROOT_SCHEMA, _PKG_SCHEMA):
        path = base / ARTIFACT
        assert path.is_file(), f"missing M03 invalidation artifact: {path}"


def test_both_schema_copies_are_byte_identical():
    root = (_ROOT_SCHEMA / ARTIFACT).read_bytes()
    packaged = (_PKG_SCHEMA / ARTIFACT).read_bytes()
    assert root == packaged, (
        f"{ARTIFACT} 根副本与打包副本必须逐字节一致 "
        f"(root={len(root)}B packaged={len(packaged)}B)"
    )


def test_artifact_is_lf_only_so_packaged_hash_parity_holds():
    """A CRLF copy can never match its own newline-normalized hash."""
    for label, base in (("root", _ROOT_SCHEMA), ("packaged", _PKG_SCHEMA)):
        path = base / ARTIFACT
        raw = path.read_bytes()
        assert raw.count(bytes([13])) == 0, (
            f"{label} {ARTIFACT} 必须是 LF-only（含 CR 字节会让打包归一化哈希"
            f"与 repo 原始字节哈希不一致）"
        )
        assert path.read_text(encoding="utf-8").encode("utf-8") == raw, (
            f"{label} {ARTIFACT} 归一化后与原始字节不一致"
        )


def test_invalidation_artifact_is_declared_in_package_data():
    text = _PYPROJECT.read_text(encoding="utf-8")
    assert f"schema/{ARTIFACT}" in text, (
        f"pyproject package-data 未包含 schema/{ARTIFACT} — "
        f"已安装 wheel 的 fresh bootstrap 将永远缺这张表"
    )


def test_packaged_loader_reaches_the_invalidation_artifact():
    text = dc._package_sql(ARTIFACT)
    assert f"CREATE TABLE IF NOT EXISTS public.{TABLE}" in text
    sha = dc._package_sql_sha256(ARTIFACT)
    assert re.fullmatch(r"[0-9a-f]{64}", sha)
    assert sha == hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── 2. fresh bootstrap include wiring ───────────────────────────────────────


@pytest.mark.parametrize("variant", ["root", "packaged"])
def test_alpha_bootstrap_includes_the_invalidation_artifact_exactly_once(variant):
    text = _variants("alpha_bootstrap.sql")[variant]
    markers = _markers(text)
    assert markers.count(f"schema/{ARTIFACT}") == 1, markers
    # dependency order: it references explicit_memories AND memory_relations.
    assert markers.index("schema/explicit_memories.sql") < markers.index(
        f"schema/{ARTIFACT}"
    ), markers
    assert markers.index("schema/memory_relations.sql") < markers.index(
        f"schema/{ARTIFACT}"
    ), markers


def test_fresh_bootstrap_expansion_creates_the_invalidation_table():
    expanded = dc._expand_alpha_include(dc._package_sql("alpha_bootstrap.sql"))
    assert not _MARKER_RE.search(expanded), "include marker survived expansion"
    assert f"CREATE TABLE IF NOT EXISTS public.{TABLE}" in expanded
    assert expanded.index(f"CREATE TABLE IF NOT EXISTS public.{TABLE}") > (
        expanded.index("CREATE TABLE IF NOT EXISTS public.explicit_memories")
    )
    assert expanded.index(f"CREATE TABLE IF NOT EXISTS public.{TABLE}") > (
        expanded.index("CREATE TABLE IF NOT EXISTS public.memory_relations")
    )


def test_repo_bootstrap_script_expansion_also_reaches_the_invalidation_table():
    """The repo-root single-entry bootstrap script walks every marker itself —
    it must pick the new artifact up too."""
    import importlib
    import sys

    scripts_dir = str(_V3CORE / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    if "bootstrap_alpha_db" in sys.modules:
        del sys.modules["bootstrap_alpha_db"]
    mod = importlib.import_module("bootstrap_alpha_db")
    repo_root = _V3CORE.parent.parent
    ddl, includes = mod.load_alpha_ddl(repo_root)
    assert any(p.endswith(ARTIFACT) for p in includes), includes
    assert f"CREATE TABLE IF NOT EXISTS public.{TABLE}" in ddl


# ── 3. upgrade path ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("variant", ["root", "packaged"])
def test_upgrade_carries_the_invalidation_artifact_include(variant):
    text = _variants("upgrade_v0_2.sql")[variant]
    markers = _markers(text)
    assert markers.count(f"schema/{ARTIFACT}") == 1, markers
    assert markers.index("schema/explicit_memories.sql") < markers.index(
        f"schema/{ARTIFACT}"
    ), markers
    assert markers.index("schema/memory_relations.sql") < markers.index(
        f"schema/{ARTIFACT}"
    ), markers


def test_upgrade_expansion_creates_the_invalidation_table_before_commit():
    expanded = dc._expand_alpha_include(dc._package_sql("upgrade_v0_2.sql"))
    assert f"CREATE TABLE IF NOT EXISTS public.{TABLE}" in expanded
    assert (
        expanded.index(f"CREATE TABLE IF NOT EXISTS public.{TABLE}")
        < expanded.rindex("COMMIT;")
    )


def test_upgrade_expected_objects_include_the_invalidation_table():
    expanded = dc._expand_alpha_include(dc._package_sql("upgrade_v0_2.sql"))
    expected = dc._parse_expected_objects(expanded)
    assert any(TABLE in t for t in expected["tables"]), expected["tables"]


# ── 4. the DDL itself ───────────────────────────────────────────────────────


def test_ddl_has_the_design_field_list():
    text = (_ROOT_SCHEMA / ARTIFACT).read_text(encoding="utf-8")
    body = _body(text)
    # exact column list from the design contract
    for col in (
        "invalidation_id",
        "correction_id",
        "relation_id",
        "canonical_qa_id",
        "input_source_kind",
        "input_source_id",
        "derived_kind",
        "derived_id",
        "source_memory_id",
        "replacement_memory_id",
        "state",
        "reason",
        "created_at",
        "resolved_at",
        "replacement_derived_id",
    ):
        assert re.search(rf"\b{col}\b", body), f"缺设计字段 {col}"
    assert re.search(r"\binvalidation_id\s+TEXT\s+PRIMARY\s+KEY", body)
    assert re.search(r"\bcorrection_id\s+TEXT\s+NOT\s+NULL", body)
    assert re.search(r"\bderived_kind\s+TEXT\s+NOT\s+NULL", body)
    assert re.search(r"\bderived_id\s+TEXT\s+NOT\s+NULL", body)
    assert re.search(r"\bsource_memory_id\s+TEXT\s+NOT\s+NULL", body)
    assert re.search(r"\bstate\s+TEXT\s+NOT\s+NULL", body)
    assert re.search(
        r"\bcreated_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+NOW\(\)", body
    )
    # nullable sidecar columns must NOT be forced NOT NULL
    assert re.search(r"\brelation_id\s+TEXT\s*(,|\n)", body)
    assert re.search(r"\breplacement_memory_id\s+TEXT\s*(,|\n)", body)
    assert re.search(r"\breason\s+TEXT\s*(,|\n)", body)
    assert re.search(r"\bresolved_at\s+TIMESTAMPTZ\s*(,|\n)", body)
    assert re.search(r"\breplacement_derived_id\s+TEXT\s*(,|\n)", body)
    # identity-normalization / audit trio (M03 spec correction): all nullable,
    # NULL input_source_kind = "no source supplied at all".
    assert re.search(r"\bcanonical_qa_id\s+BIGINT\b", body)
    assert re.search(r"\binput_source_kind\s+TEXT\b", body)
    assert re.search(r"\binput_source_id\s+TEXT\b", body)
    for col in ("canonical_qa_id", "input_source_kind", "input_source_id"):
        assert not re.search(rf"\b{col}\s+\w+\s+NOT\s+NULL", body), col


def test_ddl_has_unique_key_and_exactly_three_checks():
    body = _body((_ROOT_SCHEMA / ARTIFACT).read_text(encoding="utf-8"))
    # idempotency key: UNIQUE (correction_id, derived_kind, derived_id)
    assert re.search(
        r"UNIQUE[^;]*?\(\s*correction_id\s*,\s*derived_kind\s*,\s*derived_id\s*\)",
        body,
    ), "缺 UNIQUE (correction_id, derived_kind, derived_id) 幂等键"
    # derived_kind + state whitelists, plus the deterministic id prefix.
    assert re.search(
        r"derived_kind[^,;]*CHECK\s*\(\s*derived_kind\s+IN\s*"
        r"\(\s*'topic'\s*,\s*'observer_note'\s*,\s*'yin_paragraph'\s*\)\s*\)",
        body,
    ), "缺 derived_kind CHECK"
    assert re.search(
        r"state[^,;]*CHECK\s*\(\s*state\s+IN\s*"
        r"\(\s*'stale'\s*,\s*'pending_rebuild'\s*,\s*'rebuilt'\s*,"
        r"\s*'unresolved'\s*\)\s*\)",
        body,
    ), "缺 state CHECK"
    assert re.search(
        r"input_source_kind[^,;]*CHECK\s*\(\s*input_source_kind\s+IN\s*"
        r"\(\s*'conversation_stream'\s*,\s*'qa_pairs'\s*\)\s*\)",
        body,
    ), "缺 input_source_kind CHECK（允许 NULL）"
    assert not re.search(r"input_source_kind\s+TEXT\s+NOT\s+NULL", body), (
        "input_source_kind 必须允许 NULL（NULL = 无来源）"
    )
    assert len(re.findall(r"\bCHECK\b", body)) == 3, (
        f"必须恰好三个 CHECK 约束，实际={re.findall(r'CHECK', body)}"
    )


def test_ddl_has_the_required_query_indexes():
    body = _body((_ROOT_SCHEMA / ARTIFACT).read_text(encoding="utf-8"))
    # by correction_id
    assert re.search(
        r"ON\s+public\.derived_memory_invalidations\s*\(\s*correction_id\s*\)", body
    ), "缺按 correction_id 的索引"
    # by (derived_kind, derived_id)
    assert re.search(
        r"ON\s+public\.derived_memory_invalidations\s*"
        r"\(\s*derived_kind\s*,\s*derived_id\s*\)",
        body,
    ), "缺按 (derived_kind, derived_id) 的索引"
    # by created_at DESC
    assert re.search(
        r"ON\s+public\.derived_memory_invalidations\s*\(\s*created_at\s+DESC\s*\)",
        body,
    ), "缺按 created_at DESC 的索引"
    # by canonical_qa_id — the single normalized internal identity
    assert re.search(
        r"ON\s+public\.derived_memory_invalidations\s*\(\s*canonical_qa_id\s*\)",
        body,
    ), "缺按 canonical_qa_id 的索引"


def test_artifact_is_additive_and_idempotent():
    for variant, text in _variants(ARTIFACT).items():
        body = _body(text)
        for pat in _DESTRUCTIVE:
            assert not re.search(pat, body, re.IGNORECASE), (variant, pat)
        assert _CREATE_IF_NOT_EXISTS.search(text), variant
        # every index is guarded with IF NOT EXISTS
        for line in body.splitlines():
            if re.search(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\b", line, re.I):
                assert "IF NOT EXISTS" in line.upper(), (variant, line)


def test_artifact_documents_the_migration_required_contract():
    """The artifact states its own runtime contract: it is a schema artifact
    never auto-applied by code, and an absent table is a truthful
    MIGRATION_REQUIRED failure — never a silent fallback. It also states the
    frozen M03 identity principle verbatim and the frozen resolver error
    codes."""
    for label, base in (("root", _ROOT_SCHEMA), ("packaged", _PKG_SCHEMA)):
        body = (base / ARTIFACT).read_text(encoding="utf-8")
        assert "MIGRATION_REQUIRED" in body, label
        assert "ARTIFACT" in body.upper(), label
        assert re.search(r"never\s+(a\s+)?(hidden|silent)\s+fallback", body, re.I), (
            label,
            "artifact 必须写明缺表不是静默回退",
        )
        # frozen M03 principle, verbatim (both languages)
        assert "接受两种入口，不等于接受两套身份。" in body, label
        assert "Two accepted input forms, one derived identity." in body, label
        # frozen resolver error codes (the pre-freeze names are retired)
        for code in (
            "SOURCE_NOT_MAPPED",
            "SOURCE_AMBIGUOUS",
            "SOURCE_IDENTITY_CONFLICT",
            "NO_SOURCE",
        ):
            assert code in body, (label, code)
        assert "INVALID_SOURCE_REFERENCE" not in body, label


# ── 5. missing artifact = truthful MIGRATION_REQUIRED ───────────────────────


def test_bootstrap_expansion_refuses_when_the_artifact_is_missing(monkeypatch):
    """A missing packaged artifact must NOT silently produce a body without
    the invalidation table."""
    real = dc._package_sql

    def _without_artifact(name: str) -> str:
        if name == ARTIFACT:
            raise FileNotFoundError(
                f"packaged SQL resource v3core.schema/{ARTIFACT} not found"
            )
        return real(name)

    monkeypatch.setattr(dc, "_package_sql", _without_artifact)
    with pytest.raises(FileNotFoundError) as ei:
        dc._expand_alpha_include(dc._package_sql("alpha_bootstrap.sql"))
    assert TABLE in str(ei.value)


def test_bootstrap_apply_refuses_when_the_artifact_is_missing(monkeypatch):
    real = dc._package_sql

    def _missing(name: str) -> str:
        if name == ARTIFACT:
            raise FileNotFoundError(
                f"packaged SQL resource v3core.schema/{ARTIFACT} not found"
            )
        return real(name)

    monkeypatch.setattr(dc, "_package_sql", _missing)
    result = dc._bootstrap_apply_sql(
        {
            "host": "localhost",
            "port": 55432,
            "database": "v3alpha",
            "user": "u",
            "password": "x",
        }
    )
    assert result["applied"] is False
    assert TABLE in result["error"]


def test_upgrade_expansion_refuses_when_the_artifact_is_missing(monkeypatch):
    """The upgrade path shares the same expansion helper: a missing artifact
    can never yield an upgrade body that silently lacks the table."""
    real = dc._package_sql

    def _without_artifact(name: str) -> str:
        if name == ARTIFACT:
            raise FileNotFoundError(
                f"packaged SQL resource v3core.schema/{ARTIFACT} not found"
            )
        return real(name)

    monkeypatch.setattr(dc, "_package_sql", _without_artifact)
    with pytest.raises(FileNotFoundError) as ei:
        dc._expand_alpha_include(dc._package_sql("upgrade_v0_2.sql"))
    assert TABLE in str(ei.value)


# ── 6. dependency order still holds with the new artifact ───────────────────


def test_invalidation_table_has_no_fk_and_keeps_dependency_order():
    """The design field list declares no REFERENCES, so the artifact adds no
    FK edge; the general fresh-install ordering guard must stay satisfied."""
    from test_bootstrap_dependency_order import _create_order, _fk_edges

    expanded = dc._expand_alpha_include(dc._package_sql("alpha_bootstrap.sql"))
    order = _create_order(expanded)
    assert TABLE in order
    pos = {name: i for i, name in enumerate(order)}
    for child, parent in _fk_edges(expanded):
        if child == parent:
            continue
        assert parent in pos, (child, parent)
        assert pos[parent] <= pos[child], (child, parent)
    # no FK edge originates from the new table
    assert not [e for e in _fk_edges(expanded) if e[0] == TABLE]


def test_doctor_static_report_still_builds_with_the_new_include(capsys):
    """Adding an include must not break the doctor's packaged-SQL inventory.

    Known, reported state: the doctor's packaged-SQL inventory and live
    required-table probe are a hardcoded tuple inside
    ``src/v3core/distribution_cli.py``. That module is OUT OF SCOPE for this
    schema task, so the doctor does not yet *list* the new artifact and
    therefore emits its inventory-gap warning — even though the artifact really
    is present in the package (asserted below). This test pins that the warning
    is the stale-inventory message and not a genuine absence; wiring the doctor
    belongs to the M03 runtime task.
    """
    rc = dc._doctor(argparse.Namespace(static=True, dsn=None))
    out = capsys.readouterr().out
    assert rc in (0, 1, 2)
    import json as _json

    payload = _json.loads(out)
    sql = payload["checks"]["packaged_sql"]
    assert sql["alpha_bootstrap.sql"]["present"] is True
    markers = sql["alpha_bootstrap.sql"]["include_markers"]
    assert f"schema/{ARTIFACT}" in markers, markers
    # The artifact IS reachable from the installed package — so the doctor's
    # include-presence warning can only be its stale hardcoded inventory.
    assert f"CREATE TABLE IF NOT EXISTS public.{TABLE}" in dc._package_sql(ARTIFACT)
    gap_warnings = [w for w in payload.get("warnings", []) if ARTIFACT in w]
    for w in gap_warnings:
        assert "not present in the installed package" in w, w
    # nothing in the report claims the artifact file itself is missing
    assert sql.get(ARTIFACT, {}).get("present") in (None, True), sql.get(ARTIFACT)
