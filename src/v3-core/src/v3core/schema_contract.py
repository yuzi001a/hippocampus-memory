"""Single source of truth for the PostgreSQL schema contract.

Why this module exists
----------------------
On 2026-10-19 a running install wrote ``qa_pairs`` rows that included
``merged_event_ids``; the database had never received that column, so every
flush failed (126 logged attempts). At the same time:

  * ``schema_versions`` held a single ``v0.2`` row, applied 2026-09-18;
  * ``hippocampus doctor`` reported the schema OK — its required-column list
    for ``qa_pairs`` simply did not mention the column;
  * ``reliability.health`` reported the canonical tables present — it checked
    nine tables and no columns at all.

Three surfaces each described "the schema" with a different hand-written list,
and the writer used a fourth set (the columns in its own INSERT). Nothing
compared them, so no surface could notice the drift.

This module derives ONE contract from the shipped DDL
(``alpha_bootstrap.sql`` plus its ``ALPHA_BOOTSTRAP_INCLUDE`` sidecars) and
every consumer reads it:

    distribution_cli   upgrade dry-run / apply plan, add-column diff
    doctor_full        _check_schema_version, _check_migration_state
    reliability.health ST03 / ST04 / ST06
    V3Core             write gate (schema_not_ready)

Coverage invariants (enforced by ``tests/test_schema_contract.py``):

  * every column declared in the DDL is either part of the frozen v0.2
    baseline (``V0_2_BASE_COLUMNS``) or brought by the additive upgrade — a new
    DDL column with no upgrade guard fails the gate instead of shipping a
    schema an existing install cannot reach;
  * every table declared in the DDL is also created by the upgrade;
  * the runtime write set (``WRITE_REQUIRED_COLUMNS``) is a subset of the DDL
    contract and of every diagnostic's required set, so a diagnostic can never
    be weaker than the writer it is supposed to protect.

Readiness vs. the ledger
------------------------
Structure decides readiness. A ``schema_versions`` row is bookkeeping: an
install can carry ``v0.2`` and still lack every later column, which is exactly
what happened. ``ledger_behind`` therefore never masks a structural gap, and a
structural gap is never excused by a ledger row.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Iterable, Mapping

#: Ledger version recorded by the upgrade once it has brought a database to
#: this contract. Bump together with a new DDL batch.
CONTRACT_VERSION = "v0.4"

#: Ledger rows a fully migrated install must carry, oldest first. An install
#: that has only ``v0.2`` is behind even if it happens to be structurally
#: complete (someone applied DDL by hand) — that is reported, not hidden.
LEDGER_REQUIRED_VERSIONS: tuple[str, ...] = ("v0.2", "v0.3", "v0.4")

BOOTSTRAP_ARTIFACT = "alpha_bootstrap.sql"
UPGRADE_ARTIFACT = "upgrade_v0_2.sql"
QA_CHUNKS_ARTIFACT = "qa_embedding_chunks.sql"

#: Tables that feature code probes for at runtime but that the shipped DDL
#: deliberately does not create. ``note_segments`` is the E1 sentence index
#: added in v1.4: the reader degrades on UndefinedTable/42P01 and the table is
#: built by the observation indexer, not by bootstrap. A missing optional table
#: degrades one feature; it does not make an install "not ready".
OPTIONAL_TABLES: tuple[str, ...] = ("note_segments",)

#: Columns a live durability path writes unconditionally. Losing any of these
#: turns every flush into a failed INSERT — the production failure mode — so a
#: missing one blocks writes instead of degrading a feature.
#:
#: Source: ``V3Core._flush_pending_qa`` (the two INSERT shapes with and without
#: an embedding) and ``importers.qa_pairing.emit_qa_pairs`` (the file-import
#: writer, which also writes ``chars``).
WRITE_REQUIRED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "qa_pairs": (
        "source_id", "session_id", "turn_id", "question", "answer",
        "chars", "timestamp", "source", "tool_calls", "tool_results",
        "merged_event_ids", "embedding", "embed_model", "created_at",
    ),
}

#: Tables whose absence blocks the QA write path entirely. Derived sidecars
#: (corrections, deliveries, chunk indexes, the failure ledger) report their own
#: truthful MIGRATION_REQUIRED and must not stop unrelated writes.
WRITE_CRITICAL_TABLES: tuple[str, ...] = ("qa_pairs",)

#: The column set a pre-upgrade ("v0.2") install is known to have. Frozen on
#: purpose: it is the *anchor* that lets the coverage test tell "this column
#: predates the upgrade" apart from "this column was added to bootstrap and
#: nobody wrote an upgrade guard for it". Sourced from the 2026-09-18 install
#: (production snapshot) where available; for the remaining core tables it is
#: the DDL minus every column the upgrade adds, which is the same definition.
V0_2_BASE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "qa_pairs": (
        "id", "session_id", "question", "answer", "timestamp", "embedding",
    ),
    "conversation_stream": (
        "id", "session_id", "role", "content", "trigger", "turn_id",
        "timestamp", "source", "embedding", "tool_calls", "tool_results",
    ),
    "topics": (
        "topic_id", "title", "summary", "body", "keywords", "embedding",
        "status", "created_at", "updated_at",
    ),
    "topic_entries": (
        "id", "topic_id", "seq", "question", "answer", "source", "timestamp",
        "embedding", "message_id", "confidence",
    ),
    "observation_notes": (
        "id", "version", "content", "source_qa_range", "prev_id", "links",
        "created_at", "embedding",
    ),
    "yin_paragraphs": (
        "id", "yin_version", "section", "content", "embedding", "created_at",
    ),
}

_INCLUDE_RE = re.compile(
    r"^--\s*>>>\s*ALPHA_BOOTSTRAP_INCLUDE:\s*(?:schema/)?(\S+)\s*<<<\s*$", re.M
)
_CREATE_TABLE_RE = re.compile(
    r"CREATE TABLE IF NOT EXISTS public\.(\w+)\s*\((.*?)\n\);", re.S | re.I
)
_ADD_COLUMN_RE = re.compile(
    r"ALTER TABLE public\.(\w+)\s+ADD COLUMN IF NOT EXISTS\s+(\w+)", re.S | re.I
)
_CREATE_INDEX_RE = re.compile(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", re.I)
_NON_COLUMN_LINE_RE = re.compile(r"^(PRIMARY|CONSTRAINT|UNIQUE|CHECK|FOREIGN|EXCLUDE)\b", re.I)
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$", re.I)


def _schema_dir() -> Any:
    return resources.files("v3core.schema")


def _read_artifact(name: str) -> str:
    return (_schema_dir() / name).read_text(encoding="utf-8")


def _expand_includes(text: str, *, _depth: int = 0) -> str:
    """Inline every ``ALPHA_BOOTSTRAP_INCLUDE`` marker, recursively."""
    if _depth > 8:  # pragma: no cover - defensive: cycle guard
        raise RuntimeError("ALPHA_BOOTSTRAP_INCLUDE nesting too deep")

    def _repl(match: re.Match[str]) -> str:
        body = _expand_includes(_read_artifact(match.group(1)), _depth=_depth + 1)
        return body

    return _INCLUDE_RE.sub(_repl, text)


def _column_names(body: str) -> list[str]:
    """Column names from one CREATE TABLE body.

    A column definition starts at the beginning of the body or immediately
    after a line ending in `,`. Everything else is a continuation — a
    multi-line ``CHECK (...)`` clause starts its second line with ``AND``, which
    a naive "first token is an identifier" rule would record as a column.
    """
    out: list[str] = []
    starts_a_definition = True
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        first = line.split()[0]
        if (
            starts_a_definition
            and _IDENT_RE.match(first)
            and not _NON_COLUMN_LINE_RE.match(line)
        ):
            out.append(first.rstrip(","))
        starts_a_definition = line.rstrip().endswith(",")
    return out


def _parse_creates(text: str) -> dict[str, list[str]]:
    """Tables created wholesale, with the columns their CREATE declares."""
    tables: dict[str, list[str]] = {}
    for match in _CREATE_TABLE_RE.finditer(text):
        name = match.group(1)
        bucket = tables.setdefault(name, [])
        for column in _column_names(match.group(2)):
            if column not in bucket:
                bucket.append(column)
    return tables


def _parse_alters(text: str) -> dict[str, list[str]]:
    """Columns added to a pre-existing table (ALTER ... ADD COLUMN)."""
    added: dict[str, list[str]] = {}
    for match in _ADD_COLUMN_RE.finditer(text):
        bucket = added.setdefault(match.group(1), [])
        if match.group(2) not in bucket:
            bucket.append(match.group(2))
    return added


def _parse_ddl(text: str) -> tuple[dict[str, list[str]], set[str]]:
    """Return (table -> ordered unique columns, index names)."""
    tables = _parse_creates(text)
    for table, columns in _parse_alters(text).items():
        bucket = tables.setdefault(table, [])
        for column in columns:
            if column not in bucket:
                bucket.append(column)
    return tables, set(_CREATE_INDEX_RE.findall(text))


@dataclass(frozen=True)
class TableSpec:
    """One table the shipped DDL declares."""

    name: str
    columns: tuple[str, ...]

    @property
    def optional(self) -> bool:
        return self.name in OPTIONAL_TABLES


@dataclass(frozen=True)
class SchemaContract:
    """The schema a fresh install has, plus what the upgrade must bring."""

    version: str
    tables: Mapping[str, TableSpec]
    indexes: tuple[str, ...]
    upgrade_created_tables: tuple[str, ...]
    upgrade_added_columns: Mapping[str, tuple[str, ...]]
    ledger_versions: tuple[str, ...]

    # -- derived views the consumers actually use ---------------------------
    def required_tables(self) -> tuple[str, ...]:
        return tuple(t for t in self.tables if t not in OPTIONAL_TABLES)

    def required_columns(self) -> dict[str, tuple[str, ...]]:
        return {t: self.tables[t].columns for t in self.required_tables()}

    def write_blocking_gaps(
        self, missing_tables: Iterable[str], missing_columns: Mapping[str, Iterable[str]]
    ) -> dict[str, list[str]]:
        """The subset of a probe result that makes the write path fail."""
        missing_t = set(missing_tables)
        out: dict[str, list[str]] = {}
        for table in WRITE_CRITICAL_TABLES:
            if table in missing_t:
                out[table] = ["<table missing>"]
                continue
            cols = [c for c in missing_columns.get(table, ())]
            if cols:
                out[table] = cols
        return out

    def digest(self) -> str:
        payload = "\n".join(
            f"{t}:" + ",".join(self.tables[t].columns) for t in sorted(self.tables)
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@lru_cache(maxsize=1)
def load_contract() -> SchemaContract:
    """Parse the shipped DDL once per process."""
    bootstrap = _expand_includes(_read_artifact(BOOTSTRAP_ARTIFACT))
    ddl_tables, ddl_indexes = _parse_ddl(bootstrap)

    upgrade = _expand_includes(_read_artifact(UPGRADE_ARTIFACT))
    # The apply path splices the qa chunk artifact before the final COMMIT, so
    # the effective upgrade body includes it (see
    # distribution_cli._upgrade_load_combined_sql).
    upgrade += "\n" + _read_artifact(QA_CHUNKS_ARTIFACT)
    upgrade_creates = _parse_creates(upgrade)
    upgrade_alters = _parse_alters(upgrade)
    # ``schema_versions`` is the migration ledger. Bootstrap does not create it
    # (a fresh install historically had no ledger), but every upgraded install
    # must have it — an install without it cannot be verified as migrated — so
    # it belongs to the contract even though only the upgrade declares it.
    for table, columns in upgrade_creates.items():
        if table in ddl_tables:
            continue
        ddl_tables[table] = list(columns)

    return SchemaContract(
        version=CONTRACT_VERSION,
        tables={
            name: TableSpec(name=name, columns=tuple(cols))
            for name, cols in ddl_tables.items()
        },
        indexes=tuple(sorted(ddl_indexes)),
        upgrade_created_tables=tuple(sorted(upgrade_creates)),
        upgrade_added_columns={
            table: tuple(cols) for table, cols in upgrade_alters.items()
        },
        ledger_versions=LEDGER_REQUIRED_VERSIONS,
    )


def columns_only_created_by(table: str, contract: SchemaContract | None = None) -> tuple[str, ...]:
    """Columns of ``table`` the upgrade CREATEs wholesale (no ALTER needed)."""
    contract = contract or load_contract()
    if table not in contract.upgrade_created_tables:
        return ()
    return contract.tables[table].columns if table in contract.tables else ()


@dataclass(frozen=True)
class SchemaStatus:
    """What one probe of a live database found."""

    ready: bool
    contract_version: str
    missing_tables: tuple[str, ...]
    missing_columns: Mapping[str, tuple[str, ...]]
    write_blocked: bool
    blocked_write_columns: Mapping[str, tuple[str, ...]]
    ledger_versions: tuple[str, ...]
    missing_ledger_versions: tuple[str, ...]
    ledger_table_present: bool
    probe_error: str = ""

    @property
    def ledger_behind(self) -> bool:
        return bool(self.missing_ledger_versions)

    def summary(self) -> str:
        if self.probe_error:
            return f"schema probe failed: {self.probe_error}"
        if self.ready:
            return f"schema contract {self.contract_version} satisfied"
        parts: list[str] = []
        if self.missing_tables:
            parts.append("missing tables: " + ", ".join(self.missing_tables))
        if self.missing_columns:
            parts.append(
                "missing columns: "
                + "; ".join(
                    f"{t}.{c}" for t, cols in sorted(self.missing_columns.items())
                    for c in cols
                )
            )
        if self.missing_ledger_versions:
            parts.append(
                "ledger behind: missing " + ", ".join(self.missing_ledger_versions)
            )
        return "; ".join(parts) or "schema contract not satisfied"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "contract_version": self.contract_version,
            "missing_tables": list(self.missing_tables),
            "missing_columns": {k: list(v) for k, v in self.missing_columns.items()},
            "write_blocked": self.write_blocked,
            "blocked_write_columns": {
                k: list(v) for k, v in self.blocked_write_columns.items()
            },
            "ledger_versions": list(self.ledger_versions),
            "missing_ledger_versions": list(self.missing_ledger_versions),
            "ledger_table_present": self.ledger_table_present,
            "ledger_behind": self.ledger_behind,
            "probe_error": self.probe_error,
            "summary": self.summary(),
        }


def _safe_rollback(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass


def probe(conn: Any, *, contract: SchemaContract | None = None) -> SchemaStatus:
    """Compare a live database against the contract. Never raises.

    One ``information_schema.columns`` scan covers every table and column; the
    ledger is read separately and a failure there never aborts the structural
    verdict (a missing ``schema_versions`` table is a normal pre-upgrade state).
    """
    contract = contract or load_contract()
    observed: dict[str, set[str]] = {}
    missing_tables: list[str] = []
    missing_columns: dict[str, tuple[str, ...]] = {}
    ledger: tuple[str, ...] = ()
    ledger_present = False
    error = ""

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            )
            for table_name, column_name in cur.fetchall():
                observed.setdefault(table_name, set()).add(column_name)
    except Exception as exc:  # noqa: BLE001
        _safe_rollback(conn)
        # A probe that could not run is *unknown*, not *blocked*. Blocking the
        # write path on an infrastructure error would turn a broken diagnostic
        # into a memory outage — the opposite of the incident's lesson. The
        # verdict stays not-ready (unknown is not healthy) and the error is
        # reported verbatim.
        return SchemaStatus(
            ready=False,
            contract_version=contract.version,
            missing_tables=(),
            missing_columns={},
            write_blocked=False,
            blocked_write_columns={},
            ledger_versions=(),
            missing_ledger_versions=contract.ledger_versions,
            ledger_table_present=False,
            probe_error=f"{type(exc).__name__}: {exc}",
        )

    for table, columns in contract.required_columns().items():
        present = observed.get(table)
        if not present:
            missing_tables.append(table)
            continue
        gap = tuple(c for c in columns if c not in present)
        if gap:
            missing_columns[table] = gap

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_versions")
            ledger = tuple(sorted({str(r[0]) for r in cur.fetchall()}))
            ledger_present = True
    except Exception:  # noqa: BLE001
        # A failed statement aborts the transaction; roll back so the caller's
        # next query starts clean (the 25P02 trap this repo has hit before).
        _safe_rollback(conn)
        ledger = ()
        ledger_present = False

    missing_ledger = tuple(v for v in contract.ledger_versions if v not in ledger)
    blocked = contract.write_blocking_gaps(missing_tables, missing_columns)
    return SchemaStatus(
        ready=not missing_tables and not missing_columns,
        contract_version=contract.version,
        missing_tables=tuple(missing_tables),
        missing_columns=dict(missing_columns),
        write_blocked=bool(blocked),
        blocked_write_columns=blocked,
        ledger_versions=ledger,
        missing_ledger_versions=missing_ledger,
        ledger_table_present=ledger_present,
        probe_error=error,
    )


def contract_manifest() -> dict[str, Any]:
    """Compact, JSON-able description for plans, doctor evidence and status."""
    contract = load_contract()
    return {
        "contract_version": contract.version,
        "digest": contract.digest(),
        "ledger_versions": list(contract.ledger_versions),
        "required_tables": list(contract.required_tables()),
        "required_columns": {
            t: list(cols) for t, cols in contract.required_columns().items()
        },
        "optional_tables": list(OPTIONAL_TABLES),
        "write_critical_tables": list(WRITE_CRITICAL_TABLES),
    }


__all__ = [
    "CONTRACT_VERSION",
    "LEDGER_REQUIRED_VERSIONS",
    "OPTIONAL_TABLES",
    "WRITE_REQUIRED_COLUMNS",
    "WRITE_CRITICAL_TABLES",
    "V0_2_BASE_COLUMNS",
    "SchemaContract",
    "SchemaStatus",
    "TableSpec",
    "contract_manifest",
    "load_contract",
    "probe",
]
